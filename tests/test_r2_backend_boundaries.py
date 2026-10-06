from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from gp_control_plane import discovery_parsing, discovery_progress, storage
from gp_control_plane.repositories import primitives
from gp_control_plane.strategy_finder import read_candidate_page, read_runs_page


class R2BackendBoundariesTests(unittest.TestCase):
    def test_text_and_progress_calculations_require_no_runtime_observation(self):
        stdout = "* script : standard/10-test.sh\n- curl_test_https_tls12 youtube.com : nfqws --dpi-desync=split2\n* SUMMARY\ncurl_test_https_tls12 ipv4 youtube.com : nfqws2 --dpi-desync=split2\n"
        with patch("builtins.open", side_effect=AssertionError("pure calculation opened a file")):
            parsed = discovery_parsing.parse_blockcheck_stdout(stdout)
            observations = discovery_progress.stdout_observations(stdout)
            plan = {"total": 8, "source": "test", "script_order": ["standard/10-test.sh"], "scripts": {"standard/10-test.sh": 8}}
            result = discovery_progress._progress_from_counts(
                run={"status": "running"}, **observations, attempt_plan=plan,
                observed_script_order=[], observed_elapsed_seconds=12,
            )
            self.assertEqual(1, result["attempted"])
            self.assertEqual(12, result["elapsed_seconds"])
            self.assertEqual(8, result["attempt_total"])
            self.assertTrue(parsed["candidates"])
            clock = datetime(2026, 10, 6, 12, 0, 10, tzinfo=timezone.utc)
            self.assertEqual(10, discovery_progress.elapsed_seconds("2026-10-06T12:00:00Z", clock))
            self.assertEqual(0, discovery_progress.elapsed_seconds("2026-10-06T12:00:20Z", clock))
            self.assertIsNone(discovery_progress.elapsed_seconds("malformed", clock))

    def test_connection_based_candidate_operation_does_not_commit_or_reconnect(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            with storage.connect(state) as conn:
                # Rollback must undo every part of the existing atomic operation.
                conn.execute("SAVEPOINT operation")
                storage.upsert_candidate_event_conn(conn, candidate_id="r2-rollback", protocol="tls", args="--dpi-desync=split2", status="candidate", run_id="r2-run", domain="youtube.com", domains=[], test="curl_test_https_tls12", ip_version="4", common=False, seen_at="2026-10-06")
                self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM strategies WHERE id='r2-rollback'").fetchone()[0])
                conn.execute("ROLLBACK TO operation")
                conn.execute("RELEASE operation")
                for table in ("strategies", "strategy_domain_results", "domains"):
                    self.assertEqual(0, conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            self.assertEqual([], read_candidate_page(state)["candidates"])

    def test_preset_replacement_failure_rolls_back_previous_domains(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            storage.save_custom_preset(state, scope="finder", name="r2", domains=["youtube.com", "discord.com"], updated_at="before")
            original = primitives._upsert_domain_conn
            def fail_second(conn, name, **kwargs):
                if name == "example.org":
                    raise RuntimeError("controlled second domain failure")
                return original(conn, name, **kwargs)
            with patch.object(primitives, "_upsert_domain_conn", side_effect=fail_second):
                with self.assertRaisesRegex(RuntimeError, "controlled second"):
                    storage.save_custom_preset(state, scope="finder", name="r2", domains=["google.com", "example.org"], updated_at="after")
            self.assertEqual(["youtube.com", "discord.com"], storage.read_custom_presets(state)["finder"]["r2"])
            storage.append_run(state, {"id": "r2-history", "status": "running", "timestamp": "1"})
            storage.append_run(state, {"id": "r2-history", "status": "stopped", "timestamp": "2"})
            self.assertEqual("stopped", read_runs_page(state)["runs"][0]["status"])

    def test_repository_imports_never_return_to_storage_facade(self):
        root = Path(storage.__file__).parent
        for file in (root / "repositories").glob("*.py"):
            for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotEqual("storage", node.module, file.name)
                    self.assertFalse(any(alias.name == "storage" for alias in node.names), file.name)
        for module in (discovery_parsing, discovery_progress):
            source = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
            imports = {n.module for n in ast.walk(source) if isinstance(n, ast.ImportFrom)}
            self.assertFalse(imports & {"storage", "strategy_finder", "zapret2", "jobs"})
