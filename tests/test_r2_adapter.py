"""Test-only adapter: Standard/Multi, existing candidates/run DTOs, cancel and failure.

This fixture performs no network/process/root operation and is never packaged.
It demonstrates substitution through DiscoveryService, with the actual JobRunner
and current downstream parsing, repositories, Core history and candidate queries.
Real blockcheck2 remains covered separately by the final isolated Pi5 wheel.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from gp_control_plane import core_api
from gp_control_plane.application.discovery import DiscoveryService
from gp_control_plane.config import AppConfig, OutputConfig
from gp_control_plane.jobs import JobRunner
from gp_control_plane.state import now_iso, read_state
from gp_control_plane.storage import append_run
from gp_control_plane.strategy_finder import parse_blockcheck_stdout, upsert_candidates


class FixtureAdapter:
    capabilities = frozenset({"standard", "multi_domain"})

    def __init__(self, state):
        self.state = state
        self.entered = threading.Event()
        self.failure = False
        self.wait_for_stop = False
        self.calls = []
        self.cancelled = []

    def execute(self, spec, stop_event, run_id):
        self.calls.append((spec.name, run_id))
        self.entered.set()
        if self.failure:
            raise RuntimeError("fixture engine failed")
        status = "success"
        if self.wait_for_stop:
            if not stop_event.wait(5):
                raise RuntimeError("fixture did not receive cancellation")
            self.cancelled.append(run_id)
            status = "stopped"
        parsed = parse_blockcheck_stdout("* SUMMARY\ncurl_test_https_tls12 ipv4 youtube.com : nfqws2 --dpi-desync=split2\n")
        upsert_candidates(self.state, parsed, {"id": run_id, "domains": list(spec.payload["domains"])})
        run = {"id": run_id, "kind": "standard-discovery", "status": status, "timestamp": now_iso(), "domains": list(spec.payload["domains"]), "candidate_count": len(parsed["candidates"])}
        append_run(self.state, run)
        return run


class R2AdapterTests(unittest.TestCase):
    def wait_idle(self, state):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            value = read_state(state)
            if value.get("current_run_id") is None and value.get("last_run_status"):
                return value
            time.sleep(.01)
        self.fail("real JobRunner did not reach terminal state")

    def test_adapter_admission_success_error_stop_history_and_downstream(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=state))
            adapter = FixtureAdapter(state)
            runner = JobRunner(state)
            service = DiscoveryService(config, runner, execute_standard=adapter.execute, execute_multi_domain=adapter.execute, cancel_hook=lambda: None)
            self.assertEqual({"standard", "multi_domain"}, adapter.capabilities)
            for mode in ("standard", "multi_domain"):
                admitted = service.start_payload({"mode": mode, "domains": ["youtube.com", "discord.com"], "protocols": ["tcp"]})
                self.assertEqual("success", self.wait_idle(state)["last_run_status"])
                history = service.history({"limit": ["20"]})["runs"]
                self.assertTrue(any(row["run_id"] == admitted.run_id and row["status"] == "success" for row in history))
                candidates = core_api.strategy_candidates_payload(config, {"domains": ["youtube.com"]})["candidates"]
                self.assertTrue(candidates)
                self.assertEqual("--dpi-desync=split2", candidates[0]["args"])
            adapter.failure = True
            failed = service.start_payload({"mode": "standard", "domains": ["youtube.com"]})
            self.assertEqual("failed", self.wait_idle(state)["last_run_status"])
            self.assertTrue(any(row["run_id"] == failed.run_id and row["status"] == "failed" for row in service.history()["runs"]))
            adapter.failure = False
            adapter.wait_for_stop = True
            adapter.entered.clear()
            active = service.start_payload({"mode": "standard", "domains": ["youtube.com"]})
            self.assertTrue(adapter.entered.wait(2))
            self.assertEqual(active.run_id, service.status()["current_run"]["run_id"])
            with self.assertRaisesRegex(RuntimeError, "run already running"):
                service.start_payload({"mode": "multi_domain", "domains": ["discord.com"]})
            self.assertEqual(active.run_id, service.cancel(dry_run=False)["run_id"])
            self.assertEqual("stopped", self.wait_idle(state)["last_run_status"])
            self.assertEqual([active.run_id], adapter.cancelled)
            self.assertEqual(4, len(adapter.calls))
            self.assertTrue(any(row["run_id"] == active.run_id and row["status"] == "stopped" for row in service.history()["runs"]))
