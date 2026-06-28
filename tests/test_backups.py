from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.backups import (
    create_snapshot,
    create_snapshot_if_idle,
    import_snapshot_archive,
    list_snapshots,
    restore_snapshot,
    restore_snapshot_if_idle,
    snapshot_archive_path,
)
from gp_control_plane.state import write_state
from gp_control_plane.storage import read_custom_presets, save_custom_presets
from gp_control_plane.strategy_finder import parse_blockcheck_stdout, read_candidate_page, upsert_candidates


class BackupTests(unittest.TestCase):
    def test_snapshot_exports_strategies_and_keeps_last_five(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            parsed = parse_blockcheck_stdout(
                """
* SUMMARY
curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload=tls_client_hello --lua-desync=fake
"""
            )
            upsert_candidates(state_dir, parsed, {"id": "run-1"})

            for index in range(6):
                result = create_snapshot(state_dir)
                self.assertTrue(result["created"], index)

            snapshots = list_snapshots(state_dir)["snapshots"]

            self.assertEqual(len(snapshots), 5)
            self.assertTrue(all(item["checksum_ok"] for item in snapshots))
            latest_id = snapshots[0]["id"]
            archive = snapshot_archive_path(state_dir, latest_id)
            self.assertTrue(archive.is_file())
            strategy_file = state_dir.parent / "backups" / "snapshots" / latest_id / "strategies" / "strategies.ndjson"
            rows = [json.loads(line) for line in strategy_file.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[0]["protocol"], "tls")

    def test_snapshot_if_idle_skips_while_job_running(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            write_state(state_dir, {"current_job": "job-1", "last_error": None})

            result = create_snapshot_if_idle(state_dir)

            self.assertFalse(result["created"])
            self.assertTrue(result["queued"])
            self.assertEqual(list_snapshots(state_dir)["snapshots"], [])

    def test_custom_presets_are_not_exported_to_minimal_backup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            saved = save_custom_presets(
                state_dir,
                {"finder": {"mine": ["youtube.com", "discord.com"]}, "common": {}},
                "2026-06-25T00:00:00Z",
            )

            self.assertEqual(saved["finder"]["mine"], ["youtube.com", "discord.com"])
            self.assertEqual(read_custom_presets(state_dir)["finder"]["mine"], ["youtube.com", "discord.com"])

            result = create_snapshot(state_dir)
            snapshot_id = result["snapshot"]["id"]
            snapshot_path = state_dir.parent / "backups" / "snapshots" / snapshot_id

            self.assertFalse((snapshot_path / "presets").exists())
            self.assertTrue((snapshot_path / "domains" / "domains.ndjson").exists())

    def test_restore_snapshot_replaces_strategies_and_preserves_presets(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            first = parse_blockcheck_stdout(
                """
* SUMMARY
curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload=tls_client_hello --lua-desync=fake
"""
            )
            upsert_candidates(state_dir, first, {"id": "run-1"})
            save_custom_presets(
                state_dir,
                {"finder": {"old": ["youtube.com"]}, "common": {}},
                "2026-06-25T00:00:00Z",
            )
            snapshot_id = create_snapshot(state_dir)["snapshot"]["id"]
            second = parse_blockcheck_stdout(
                """
* SUMMARY
curl_test_https_tls12 ipv4 discord.com : nfqws2 --payload=tls_client_hello --lua-desync=multisplit
"""
            )
            upsert_candidates(state_dir, second, {"id": "run-2"})
            save_custom_presets(
                state_dir,
                {"finder": {"new": ["discord.com"]}, "common": {}},
                "2026-06-25T01:00:00Z",
            )

            result = restore_snapshot(state_dir, snapshot_id)
            page = read_candidate_page(state_dir, domain="youtube.com", limit=10)
            discord_page = read_candidate_page(state_dir, domain="discord.com", limit=10)
            presets = read_custom_presets(state_dir)

            self.assertTrue(result["restored"])
            self.assertEqual(page["total"], 1)
            self.assertEqual(discord_page["total"], 0)
            self.assertEqual(presets["finder"], {"new": ["discord.com"]})

    def test_snapshot_excludes_derived_strategy_stats(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            parsed = parse_blockcheck_stdout(
                """
* SUMMARY
curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload=tls_client_hello --lua-desync=fake
"""
            )
            upsert_candidates(state_dir, parsed, {"id": "run-1"})
            snapshot_id = create_snapshot(state_dir)["snapshot"]["id"]
            snapshot_path = state_dir.parent / "backups" / "snapshots" / snapshot_id
            strategy_file = snapshot_path / "strategies" / "strategies.ndjson"
            link_file = snapshot_path / "strategies" / "strategy-domain-links.ndjson"
            strategy = json.loads(strategy_file.read_text(encoding="utf-8").splitlines()[0])
            link = json.loads(link_file.read_text(encoding="utf-8").splitlines()[0])

            self.assertNotIn("seen_count", strategy)
            self.assertNotIn("common_seen_count", strategy)
            self.assertNotIn("seen_count", link)
            self.assertFalse((snapshot_path / "strategies" / "strategy-stats.ndjson").exists())

    def test_import_snapshot_archive_restores_uploaded_zip(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            parsed = parse_blockcheck_stdout(
                """
* SUMMARY
curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload=tls_client_hello --lua-desync=fake
"""
            )
            upsert_candidates(state_dir, parsed, {"id": "run-1"})
            snapshot_id = create_snapshot(state_dir)["snapshot"]["id"]
            archive = snapshot_archive_path(state_dir, snapshot_id)
            target_state = Path(raw) / "target-state"

            result = import_snapshot_archive(target_state, archive.read_bytes())

            self.assertTrue(result["imported"])
            self.assertEqual(result["snapshot"]["id"], snapshot_id)

    def test_restore_snapshot_if_idle_skips_while_job_running(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            parsed = parse_blockcheck_stdout(
                """
* SUMMARY
curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload=tls_client_hello --lua-desync=fake
"""
            )
            upsert_candidates(state_dir, parsed, {"id": "run-1"})
            snapshot_id = create_snapshot(state_dir)["snapshot"]["id"]
            write_state(state_dir, {"current_job": "job-1", "last_error": None})

            result = restore_snapshot_if_idle(state_dir, snapshot_id)

            self.assertFalse(result["restored"])
            self.assertTrue(result["queued"])


if __name__ == "__main__":
    unittest.main()
