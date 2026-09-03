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
    blockchecks_state_dir,
    bs_run_env,
    build_bs_scan_argv,
    campaign_lock_busy_message,
    discovery_job_name,
    scan_level_to_bs,
)
from gp_control_plane.storage import connect


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
        self.assertEqual("8", argv[argv.index("--curl-parallel") + 1])
        self.assertEqual("4", argv[argv.index("--parallel") + 1])
        self.assertIn("--protocol", argv)
        self.assertEqual("tls12", argv[argv.index("--protocol") + 1])
        self.assertIn("--parallel-repeats", argv)
        self.assertIn("--skip-dns-audit", argv)
        self.assertEqual(["-d", "youtube.com", "-d", "discord.com"], argv[-4:])

    def test_build_bs_scan_argv_bs_knobs(self) -> None:
        fake = Path(tempfile.mkdtemp()) / "bs"
        fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        with mock.patch("gp_control_plane.discovery_engine.resolve_bs_binary", return_value=str(fake)):
            argv = build_bs_scan_argv(
                domains=["youtube.com"],
                scan_level="standard",
                repeats=3,
                repeat_parallel=False,
                curl_max_time=2,
                timeout_seconds=0,
                curl_parallelism=2,
                skip_dnscheck=False,
                strategy_preset="gp-verified",
                repeats_mode="stable",
                adaptive=False,
                debug=True,
                protocol="tls13",
                skip_ipblock=True,
            )
        self.assertEqual("tls13", argv[argv.index("--protocol") + 1])
        self.assertIn("-M", argv)
        self.assertEqual("gp-verified", argv[argv.index("-M") + 1])
        self.assertIn("--repeats-mode", argv)
        self.assertEqual("stable", argv[argv.index("--repeats-mode") + 1])
        self.assertIn("--no-adaptive", argv)
        self.assertIn("--debug", argv)
        self.assertIn("--skip-ip-block", argv)
        self.assertNotIn("--tcp-sources", argv)

    def test_build_bs_scan_argv_uses_domains_file_instead_of_dash_d(self) -> None:
        fake = Path(tempfile.mkdtemp()) / "bs"
        fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        with mock.patch("gp_control_plane.discovery_engine.resolve_bs_binary", return_value=str(fake)):
            argv = build_bs_scan_argv(
                domains=["a.com", "b.com"],
                scan_level="standard",
                repeats=1,
                repeat_parallel=False,
                curl_max_time=2,
                timeout_seconds=0,
                curl_parallelism=2,
                skip_dnscheck=True,
                domains_file="/tmp/doms.txt",
            )
        self.assertIn("--domains-file", argv)
        self.assertEqual("/tmp/doms.txt", argv[argv.index("--domains-file") + 1])
        self.assertNotIn("-d", argv)

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
            fixed_run_id = "fixed-run"
            run_db = bs_state / "bs-runs" / f"{fixed_run_id}.db"
            run_db.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(run_db)
            conn.executescript(
                """
                CREATE TABLE strategies (
                    id INTEGER PRIMARY KEY, name TEXT, config_path TEXT, proto TEXT
                );
                CREATE TABLE tcp_results (
                    id INTEGER PRIMARY KEY, domain TEXT, strategy_id INTEGER,
                    status TEXT, bridge_applied INTEGER
                );
                INSERT INTO strategies(id, name, config_path, proto) VALUES
                    (1, 'slug_a', 'fake:blob=stun:repeats=6:tcp_ts=-1000', 'tcp'),
                    (2, 'slug_b', 'fake:blob=stun:repeats=3:tcp_ts=-1000', 'tcp');
                INSERT INTO tcp_results(id, domain, strategy_id, status, bridge_applied) VALUES
                    (1, 'youtube.com', 1, 'PASS', 1),
                    (2, 'reddit.com', 2, 'THROTTLED', 1),
                    (3, 'yahoo.com', 1, 'PASS', 0),
                    (4, 'example.com', 1, 'FAIL', 1);
                """
            )
            conn.commit()
            conn.close()
            fake_bs = root / "bs-bin"
            fake_bs.write_text("#!/bin/sh\necho '[1/10] pass=2'\nexit 0\n", encoding="utf-8")
            fake_bs.chmod(fake_bs.stat().st_mode | stat.S_IEXEC)
            with (
                mock.patch("gp_control_plane.discovery_engine.resolve_bs_binary", return_value=str(fake_bs)),
                mock.patch("gp_control_plane.blockchecks_backend.resolve_bs_binary", return_value=str(fake_bs)),
                mock.patch("gp_control_plane.blockchecks_backend.blockchecks_state_dir", return_value=bs_state),
                mock.patch("gp_control_plane.blockchecks_backend._discovery_run_id", return_value=fixed_run_id),
                mock.patch("gp_control_plane.discovery_engine.campaign_lock_busy_message", return_value=None),
                mock.patch("gp_control_plane.blockchecks_backend.campaign_lock_busy_message", return_value=None),
            ):
                run = run_blockchecks_discovery(
                    ["youtube.com", "reddit.com"],
                    gp_state,
                    timeout_seconds=0,
                    stop_event=threading.Event(),
                )
            self.assertEqual("success", run["status"])
            self.assertEqual(fixed_run_id, run["id"])
            self.assertEqual(str(run_db), run["bs_db"])
            self.assertIn("--db", run["bs_argv"])
            stdout = Path(run["stdout_log"]).read_text(encoding="utf-8")
            self.assertNotIn("!!!!! AVAILABLE !!!!!", stdout)
            self.assertEqual(2, run["candidate_count"])
            with connect(gp_state) as gp_conn:
                strategies = gp_conn.execute(
                    "SELECT args FROM strategies ORDER BY args"
                ).fetchall()
            args = {row["args"] for row in strategies}
            self.assertIn("fake:blob=stun:repeats=6:tcp_ts=-1000", args)
            self.assertIn("fake:blob=stun:repeats=3:tcp_ts=-1000", args)
            self.assertNotIn("slug_a", args)
            self.assertNotIn("slug_b", args)
            progress = json.loads(Path(run["progress_log"]).read_text(encoding="utf-8"))
            self.assertIn("attempted", progress)
            self.assertIn("attempt_total", progress)
            self.assertIn("effective_attempt_total", progress)
            self.assertIn("successful", progress)
            self.assertIn("elapsed_seconds", progress)


    def test_blockchecks_state_dir_appends_app_dir_on_override(self) -> None:
        with mock.patch.dict(os.environ, {"BLOCKCHECKS_STATE_HOME": "/tmp/xdg-state"}, clear=False):
            self.assertEqual(
                str(blockchecks_state_dir()),
                os.path.join("/tmp/xdg-state", "blockcheckS"),
            )

    def test_bs_run_env_hands_off_zapret_root_without_xdg_override(self) -> None:
        with mock.patch.dict(os.environ, {"ZAPRET_DIR": "/opt/zapret2"}, clear=False):
            env = bs_run_env()
        self.assertEqual("/opt/zapret2", env["BLOCKCHECKS_ZAPRET2"])
        self.assertEqual("/opt/zapret2", env["ZAPRET2_ROOT"])
        self.assertNotIn("BLOCKCHECKS_STATE_HOME", env)

    def test_build_bs_scan_argv_accepts_db_and_strategy_preset(self) -> None:
        fake = Path(tempfile.mkdtemp()) / "bs"
        fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        with mock.patch("gp_control_plane.discovery_engine.resolve_bs_binary", return_value=str(fake)):
            argv = build_bs_scan_argv(
                domains=["youtube.com"],
                scan_level="standard",
                repeats=3,
                repeat_parallel=False,
                curl_max_time=2,
                timeout_seconds=0,
                curl_parallelism=2,
                skip_dnscheck=True,
                db_path="/tmp/run.db",
                strategy_preset="gp-verified",
            )
        self.assertIn("--db", argv)
        self.assertEqual("/tmp/run.db", argv[argv.index("--db") + 1])
        self.assertIn("-M", argv)
        self.assertEqual("gp-verified", argv[argv.index("-M") + 1])


if __name__ == "__main__":
    unittest.main()


class ConfigHarvestTests(unittest.TestCase):
    def test_expand_config_candidate_args_splits_desync_cores(self) -> None:
        from gp_control_plane.blockchecks_backend import _expand_config_candidate_args, _harvest_passes

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            conf = root / "champ.conf"
            conf.write_text(
                "--lua-desync=fake:blob=google:repeats=8:tcp_ts=-1000\n"
                "--lua-desync=hostfakesplit:host=www.google.com:tcp_ts=-1000\n",
                encoding="utf-8",
            )
            expanded = _expand_config_candidate_args(str(conf))
            self.assertEqual(2, len(expanded))
            self.assertIn("fake:blob=google:repeats=8:tcp_ts=-1000", expanded)
            self.assertIn("hostfakesplit:host=www.google.com:tcp_ts=-1000", expanded)
            self.assertNotIn(str(conf), expanded)

            gp_state = root / "gp"
            gp_state.mkdir()
            run_db = root / "run.db"
            conn = sqlite3.connect(run_db)
            conn.executescript(
                """
                CREATE TABLE strategies (
                    id INTEGER PRIMARY KEY, name TEXT, config_path TEXT, proto TEXT
                );
                CREATE TABLE tcp_results (
                    id INTEGER PRIMARY KEY, domain TEXT, strategy_id INTEGER,
                    status TEXT, bridge_applied INTEGER
                );
                INSERT INTO strategies(id, name, config_path, proto) VALUES
                    (1, 'champ', ?, 'tcp');
                INSERT INTO tcp_results(id, domain, strategy_id, status, bridge_applied) VALUES
                    (1, 'youtube.com', 1, 'PASS', 1);
                """.replace("?", f"'{conf.as_posix()}'")
            )
            conn.commit()
            conn.close()
            _harvest_passes(gp_state, "run", "standard-discovery", set(), run_db)
            with connect(gp_state) as gp_conn:
                args = {row["args"] for row in gp_conn.execute("SELECT args FROM strategies")}
            self.assertIn("fake:blob=google:repeats=8:tcp_ts=-1000", args)
            self.assertIn("hostfakesplit:host=www.google.com:tcp_ts=-1000", args)
            self.assertNotIn(conf.as_posix(), args)


class DnsPinsTests(unittest.TestCase):
    def test_list_bs_dns_pins_reads_provider_hosts(self) -> None:
        from gp_control_plane.blockchecks_backend import list_bs_dns_pins

        with tempfile.TemporaryDirectory() as raw:
            data = Path(raw) / "blockcheckS" / "data_block" / "providers" / "isp_x"
            data.mkdir(parents=True)
            hosts = data / "hosts"
            hosts.write_text("# 8.8.8.8  youtube.com\n# 1.1.1.1  discord.com\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"XDG_DATA_HOME": raw}, clear=False):
                payload = list_bs_dns_pins()
            self.assertEqual(1, len(payload["providers"]))
            prov = payload["providers"][0]
            self.assertEqual("isp_x", prov["provider"])
            self.assertEqual(hosts.as_posix(), prov["path"])
            self.assertIn("# 8.8.8.8  youtube.com", prov["lines"])


class PairUdpTests(unittest.TestCase):
    def test_build_pair_argv_uses_pair_command_without_preset(self) -> None:
        fake = Path(tempfile.mkdtemp()) / "bs"
        fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        with mock.patch("gp_control_plane.discovery_engine.resolve_bs_binary", return_value=str(fake)):
            argv = build_bs_scan_argv(
                domains=["discord.com"],
                scan_level="standard",
                repeats=1,
                repeat_parallel=False,
                curl_max_time=2,
                timeout_seconds=0,
                curl_parallelism=2,
                skip_dnscheck=True,
                strategy_preset="gp-verified",
                pair_mode=True,
            )
        self.assertEqual("pair", argv[1])
        self.assertNotIn("-M", argv)
        self.assertIn("--tcp-sources", argv)

    def test_harvest_udp_maps_strategies_to_domain(self) -> None:
        from gp_control_plane.blockchecks_backend import _harvest_udp

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            gp_state = root / "gp"
            gp_state.mkdir()
            run_db = root / "u.db"
            conn = sqlite3.connect(run_db)
            conn.executescript(
                """
                CREATE TABLE strategies (id INTEGER PRIMARY KEY, name TEXT, config_path TEXT, proto TEXT);
                CREATE TABLE udp_results (
                    id INTEGER PRIMARY KEY, strategy_id INTEGER, target TEXT, status TEXT
                );
                INSERT INTO strategies(id, name, config_path, proto) VALUES
                    (1, 'slug_u', 'fake:blob=discord_udp:repeats=6', 'udp');
                INSERT INTO udp_results(id, strategy_id, target, status) VALUES
                    (1, 1, '35.217.5.42:50006', 'PASS');
                """
            )
            conn.commit()
            conn.close()
            _harvest_udp(gp_state, "run", "standard-discovery", set(), run_db, "discord.com")
            with connect(gp_state) as gp_conn:
                rows = gp_conn.execute("SELECT protocol, args FROM strategies").fetchall()
                links = gp_conn.execute(
                    "SELECT domain_id FROM strategy_domain_results"
                ).fetchall()
            self.assertEqual([("udp", "fake:blob=discord_udp:repeats=6")], [(r["protocol"], r["args"]) for r in rows])
            self.assertEqual(1, len(links))


class PairHarvestTests(unittest.TestCase):
    def test_harvest_pairs_maps_labels_to_args(self) -> None:
        from gp_control_plane.blockchecks_backend import _harvest_pairs

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            gp_state = root / "gp"
            gp_state.mkdir()
            run_db = root / "p.db"
            conn = sqlite3.connect(run_db)
            conn.executescript(
                """
                CREATE TABLE strategies (
                    id INTEGER PRIMARY KEY, name TEXT, config_path TEXT, proto TEXT
                );
                CREATE TABLE pair_results (
                    id INTEGER PRIMARY KEY,
                    tcp_strategy TEXT, udp_strategy TEXT, domain TEXT,
                    overall TEXT, tcp_ms REAL, gateway_ms REAL, udp_ms REAL
                );
                INSERT INTO strategies(id, name, config_path, proto) VALUES
                    (1, 'slug_t', 'fake:blob=stun:repeats=6', 'tcp'),
                    (2, 'slug_u', 'fake:blob=discord_udp:repeats=6', 'udp');
                INSERT INTO pair_results(id, tcp_strategy, udp_strategy, domain, overall,
                                         tcp_ms, gateway_ms, udp_ms) VALUES
                    (1, 'slug_t', 'slug_u', 'discord.com', 'PASS', 100.0, 50.0, 30.0);
                """
            )
            conn.commit()
            conn.close()
            _harvest_pairs(gp_state, "run", run_db, "discord.com")
            with connect(gp_state) as gp_conn:
                rows = gp_conn.execute(
                    "SELECT tcp_args, udp_args, domain, overall FROM strategy_pairs"
                ).fetchall()
            self.assertEqual(
                [("fake:blob=stun:repeats=6", "fake:blob=discord_udp:repeats=6", "discord.com", "PASS")],
                [(r["tcp_args"], r["udp_args"], r["domain"], r["overall"]) for r in rows],
            )
