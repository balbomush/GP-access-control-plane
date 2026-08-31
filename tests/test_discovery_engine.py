from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane import core_api
from gp_control_plane.blockchecks_backend import run_blockchecks_discovery
from gp_control_plane.discovery_engine import (
    build_bs_scan_argv,
    campaign_lock_busy_message,
    discovery_job_name,
    scan_level_to_bs,
)
from gp_control_plane.storage import connect
from gp_control_plane.strategy_finder import candidate_total as finder_candidate_total


class DiscoveryEngineFlagMapTests(unittest.TestCase):
    def test_scan_level_maps_gp_to_blockchecks(self) -> None:
        self.assertEqual("single", scan_level_to_bs("quick"))
        self.assertEqual("fast", scan_level_to_bs("standard"))
        self.assertEqual("full", scan_level_to_bs("force"))
        self.assertEqual("fast", scan_level_to_bs("unknown"))

    def test_job_names_split_engines(self) -> None:
        self.assertEqual("zapret-standard-discovery", discovery_job_name("blockcheck2", "standard"))
        self.assertEqual("zapret-multi-domain-discovery", discovery_job_name("blockcheck2", "multi_domain"))
        self.assertEqual("blockchecks-standard-discovery", discovery_job_name("blockchecks", "standard"))
        self.assertEqual("blockchecks-multi-domain-discovery", discovery_job_name("bs", "multi"))

    def test_start_run_payload_selects_blockchecks_job(self) -> None:
        name, payload = core_api.strategy_discovery_job_payload(
            {
                "mode": "standard",
                "domains": ["discord.com"],
                "protocols": ["tcp"],
                "settings": {"discovery_engine": "blockchecks", "scan_level": "quick"},
            }
        )
        self.assertEqual("blockchecks-standard-discovery", name)
        self.assertEqual("blockchecks", payload["discovery_engine"])
        self.assertEqual("quick", payload["scan_level"])

    def test_build_bs_scan_argv_caps_and_never_starts_full(self) -> None:
        fake = Path(tempfile.mkdtemp()) / "bs"
        fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        with mock.patch("gp_control_plane.discovery_engine.resolve_bs_binary", return_value=str(fake)):
            argv = build_bs_scan_argv(
                domains=["youtube.com", "discord.com"],
                scan_level="force",
                repeats=6,
                repeat_parallel=True,
                curl_max_time=2,
                timeout_seconds=0,
                curl_parallelism=30,
                skip_dnscheck=True,
            )
        self.assertEqual(str(fake), argv[0])
        self.assertEqual("scan", argv[1])
        self.assertNotEqual("full", argv[1])
        self.assertIn("--scan-level", argv)
        self.assertEqual("full", argv[argv.index("--scan-level") + 1])
        self.assertIn("--max", argv)
        self.assertEqual("400", argv[argv.index("--max") + 1])
        self.assertIn("--curl-parallel", argv)
        self.assertEqual("30", argv[argv.index("--curl-parallel") + 1])
        self.assertEqual("4", argv[argv.index("--parallel") + 1])
        self.assertIn("--parallel-repeats", argv)
        self.assertIn("--skip-dns-audit", argv)
        self.assertEqual(["-d", "youtube.com", "-d", "discord.com"], argv[-4:])

    def test_campaign_lock_reports_busy_for_live_pid(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            lock = state / "run.lock"
            lock.write_text(
                json.dumps({"pid": os.getpid(), "command": "bs full", "argv": ["full"]}),
                encoding="utf-8",
            )
            with mock.patch("gp_control_plane.discovery_engine.blockchecks_state_dir", return_value=state):
                message = campaign_lock_busy_message()
            self.assertIsNotNone(message)
            self.assertIn("bs full", str(message))

    def test_harvest_pass_applied_without_available_markers(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            gp_state = root / "gp"
            bs_state = root / "bs"
            gp_state.mkdir()
            bs_state.mkdir()
            db = bs_state / "state.db"
            conn = sqlite3.connect(db)
            conn.executescript(
                """
                CREATE TABLE strategies (id INTEGER PRIMARY KEY, name TEXT, proto TEXT);
                CREATE TABLE tcp_results (
                    domain TEXT,
                    strategy_id INTEGER,
                    status TEXT,
                    bridge_applied INTEGER
                );
                INSERT INTO strategies(id, name, proto) VALUES
                    (1, 'fake:blob=stun:repeats=6:tcp_ts=-1000', 'tcp');
                INSERT INTO tcp_results(domain, strategy_id, status, bridge_applied) VALUES
                    ('discord.com', 1, 'PASS', 1),
                    ('youtube.com', 1, 'PASS', 0),
                    ('example.com', 1, 'FAIL', 1);
                """
            )
            conn.commit()
            conn.close()
            fake_bs = root / "bs-bin"
            fake_bs.write_text("#!/bin/sh\necho '[1/10] pass=1'\nexit 0\n", encoding="utf-8")
            fake_bs.chmod(fake_bs.stat().st_mode | stat.S_IEXEC)
            with (
                mock.patch("gp_control_plane.discovery_engine.resolve_bs_binary", return_value=str(fake_bs)),
                mock.patch("gp_control_plane.blockchecks_backend.resolve_bs_binary", return_value=str(fake_bs)),
                mock.patch("gp_control_plane.blockchecks_backend.blockchecks_state_dir", return_value=bs_state),
                mock.patch("gp_control_plane.discovery_engine.campaign_lock_busy_message", return_value=None),
                mock.patch("gp_control_plane.blockchecks_backend.campaign_lock_busy_message", return_value=None),
            ):
                run = run_blockchecks_discovery(
                    ["discord.com"],
                    gp_state,
                    timeout_seconds=0,
                    stop_event=threading.Event(),
                )
            self.assertEqual("success", run["status"])
            stdout = Path(run["stdout_log"]).read_text(encoding="utf-8")
            self.assertNotIn("!!!!! AVAILABLE !!!!!", stdout)
            self.assertEqual(1, finder_candidate_total(gp_state))
            with connect(gp_state) as gp_conn:
                row = gp_conn.execute("SELECT protocol, args FROM strategies").fetchone()
            self.assertEqual("tls", row["protocol"])
            self.assertIn("fake:blob=stun", row["args"])
            self.assertEqual(1, run["candidate_count"])


if __name__ == "__main__":
    unittest.main()
