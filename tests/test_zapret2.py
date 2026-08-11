from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.zapret2 import (
    BLOCKCHECK_ENV_KEYS,
    _blockcheck_nft_tables,
    _signal_process_group,
    signal_registered_process_run,
    _stop_process_group,
    check_install,
    check_install_cached,
    clear_install_check_cache,
    root_command,
    root_helper_status,
)
from gp_control_plane.core_api import preflight_payload


class Zapret2Tests(unittest.TestCase):
    def test_root_helper_env_whitelist_matches_backend_keys(self) -> None:
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        text = helper.read_text(encoding="utf-8")
        match = re.search(r"case \"\$key\" in\s+([^)]+)\)", text)
        self.assertIsNotNone(match)
        helper_keys = set(match.group(1).strip().split("|"))

        self.assertEqual(helper_keys, set(BLOCKCHECK_ENV_KEYS))

    def test_check_install_reports_available_paths(self) -> None:
        def fake_which(name: str) -> str | None:
            return {"nfqws2": "/usr/bin/nfqws2", "blockcheck2.sh": "/usr/bin/blockcheck2.sh"}.get(name)

        with mock.patch("gp_control_plane.zapret2.shutil.which", side_effect=fake_which):
            result = check_install()

        self.assertTrue(result["nfqws2_found"])
        self.assertEqual(result["nfqws2_path"], "/usr/bin/nfqws2")
        self.assertTrue(result["blockcheck_found"])
        self.assertEqual(result["blockcheck_path"], "/usr/bin/blockcheck2.sh")
        self.assertFalse(result["root_helper_ready"])
        self.assertFalse(result["ready"])
        self.assertTrue(any(item["id"] == "root-helper" and not item["ok"] for item in result["diagnostics"]))

    def test_check_install_reports_russian_message_for_missing_nfqws2(self) -> None:
        with mock.patch("gp_control_plane.zapret2.shutil.which", return_value=None):
            result = check_install()

        diagnostics = {str(item["id"]): item for item in result["diagnostics"]}
        self.assertEqual(
            diagnostics["nfqws2"]["message"],
            "не найден в PATH; установите zapret2 или проверьте ссылку на nfqws2",
        )

    def test_unavailable_root_helper_has_russian_message_and_raw_diagnostic_reason(self) -> None:
        cases = (
            ("root-helper not found at /helper/gp-root-helper", {"sudo": "/usr/bin/sudo"}, False),
            ("sudo command not found", {}, True),
        )
        for raw_reason, commands, helper_exists in cases:
            with (
                self.subTest(raw_reason=raw_reason),
                mock.patch("gp_control_plane.zapret2._is_root", return_value=False),
                mock.patch("gp_control_plane.zapret2._root_helper_path", return_value="/helper/gp-root-helper"),
                mock.patch("gp_control_plane.zapret2.Path.is_file", return_value=helper_exists),
                mock.patch("gp_control_plane.zapret2.os.access", return_value=True),
                mock.patch("gp_control_plane.zapret2.shutil.which", side_effect=lambda name: commands.get(name)),
            ):
                result = check_install()

            diagnostics = {str(item["id"]): item for item in result["diagnostics"]}
            root_helper = diagnostics["root-helper"]
            self.assertEqual(root_helper["message"], "служба с повышенными правами недоступна; запустите Linux-установщик")
            self.assertEqual(root_helper["details"], {"reason": raw_reason})
            self.assertEqual(result["root_helper_error"], raw_reason)

    def test_preflight_preserves_root_helper_diagnostic_details(self) -> None:
        zapret = {
            "diagnostics": [
                {
                    "id": "root-helper",
                    "ok": False,
                    "message": "служба с повышенными правами недоступна; запустите Linux-установщик",
                    "details": {"reason": "sudo command not found"},
                }
            ]
        }
        with mock.patch("gp_control_plane.core_api.check_install_cached", return_value=zapret):
            preflight = preflight_payload(mock.sentinel.config)

        self.assertEqual(preflight["checks"][0]["message"], "служба с повышенными правами недоступна; запустите Linux-установщик")
        self.assertEqual(preflight["checks"][0]["details"], {"reason": "sudo command not found"})

    def test_check_install_reports_human_diagnostics(self) -> None:
        def fake_which(name: str) -> str | None:
            return {
                "nfqws2": "/usr/bin/nfqws2",
                "blockcheck2.sh": "/usr/bin/blockcheck2.sh",
                "curl": "/usr/bin/curl",
                "nft": "/usr/sbin/nft",
                "sudo": "/usr/bin/sudo",
            }.get(name)

        with (
            mock.patch("gp_control_plane.zapret2._is_root", return_value=False),
            mock.patch("gp_control_plane.zapret2._root_helper_path", return_value="/helper/gp-root-helper"),
            mock.patch("gp_control_plane.zapret2.Path.is_file", return_value=True),
            mock.patch("gp_control_plane.zapret2.os.access", return_value=True),
            mock.patch("gp_control_plane.zapret2.shutil.which", side_effect=fake_which),
            mock.patch("gp_control_plane.zapret2.subprocess.run", return_value=subprocess.CompletedProcess(["check"], 0, "", "")),
        ):
            result = check_install()

        self.assertTrue(result["ready"])
        diagnostics = {str(item["id"]): item for item in result["diagnostics"]}
        self.assertTrue(diagnostics["nfqws2"]["ok"])
        self.assertTrue(diagnostics["blockcheck"]["ok"])
        self.assertTrue(diagnostics["root-helper"]["ok"])
        self.assertTrue(diagnostics["curl"]["ok"])
        self.assertTrue(diagnostics["nft"]["ok"])
        self.assertIn("/usr/bin/nfqws2", str(diagnostics["nfqws2"]["message"]))

    def test_root_helper_status_uses_sudo_non_interactively(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            mock.patch("gp_control_plane.zapret2._is_root", return_value=False),
            mock.patch("gp_control_plane.zapret2._root_helper_path", return_value="/helper/gp-root-helper"),
            mock.patch("gp_control_plane.zapret2.Path.is_file", return_value=True),
            mock.patch("gp_control_plane.zapret2.os.access", return_value=True),
            mock.patch("gp_control_plane.zapret2.shutil.which", return_value="/usr/bin/sudo"),
            mock.patch("gp_control_plane.zapret2.subprocess.run", side_effect=fake_run),
        ):
            status = root_helper_status()

        self.assertTrue(status["ready"])
        self.assertEqual(calls[0], ["/usr/bin/sudo", "-n", "/helper/gp-root-helper", "check"])

    def test_check_install_cached_reuses_root_helper_result(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        clear_install_check_cache()
        try:
            with (
                mock.patch("gp_control_plane.zapret2._is_root", return_value=False),
                mock.patch("gp_control_plane.zapret2._root_helper_path", return_value="/helper/gp-root-helper"),
                mock.patch("gp_control_plane.zapret2.Path.is_file", return_value=True),
                mock.patch("gp_control_plane.zapret2.os.access", return_value=True),
                mock.patch("gp_control_plane.zapret2.shutil.which", return_value="/usr/bin/sudo"),
                mock.patch("gp_control_plane.zapret2.subprocess.run", side_effect=fake_run),
            ):
                first = check_install_cached(ttl_seconds=60)
                second = check_install_cached(ttl_seconds=60)
        finally:
            clear_install_check_cache()

        self.assertTrue(first["root_helper_ready"])
        self.assertEqual(second["root_helper_path"], "/helper/gp-root-helper")
        self.assertEqual(len(calls), 1)

    def test_root_command_wraps_blockcheck_with_helper_and_env(self) -> None:
        env = {"BATCH": "1", "DOMAINS": "youtube.com", "ENABLE_HTTP3": "1", "IGNORED": "x"}

        with (
            mock.patch("gp_control_plane.zapret2._is_root", return_value=False),
            mock.patch("gp_control_plane.zapret2.require_root_helper_ready"),
            mock.patch("gp_control_plane.zapret2._root_helper_path", return_value="/helper/gp-root-helper"),
            mock.patch("gp_control_plane.zapret2.shutil.which", return_value="/usr/bin/sudo"),
        ):
            command = root_command(
                ["/opt/zapret2/blockcheck2.sh"], env=env, pass_env_keys=BLOCKCHECK_ENV_KEYS, run_id="run-owned"
            )

        self.assertEqual(
            command,
            [
                "/usr/bin/sudo",
                "-n",
                "/helper/gp-root-helper",
                "run-owned-env",
                "run-owned",
                "BATCH=1",
                "DOMAINS=youtube.com",
                "ENABLE_HTTP3=1",
                "--",
                "/opt/zapret2/blockcheck2.sh",
            ],
        )

    def test_root_command_wraps_multidomain_with_helper_owned_runner(self) -> None:
        env = {"BATCH": "1", "DOMAINS": "youtube.com", "GP_MD_CURL_PARALLELISM": "30"}

        with (
            mock.patch("gp_control_plane.zapret2._is_root", return_value=False),
            mock.patch("gp_control_plane.zapret2.require_root_helper_ready"),
            mock.patch("gp_control_plane.zapret2._root_helper_path", return_value="/helper/gp-root-helper"),
            mock.patch("gp_control_plane.zapret2.shutil.which", return_value="/usr/bin/sudo"),
        ):
            command = root_command(
                ["/opt/zapret2/blockcheck2.sh"],
                env=env,
                pass_env_keys=BLOCKCHECK_ENV_KEYS,
                helper_command="run-multidomain",
                run_id="run-owned",
            )

        self.assertEqual(
            command,
            [
                "/usr/bin/sudo",
                "-n",
                "/helper/gp-root-helper",
                "run-multidomain-owned-env",
                "run-owned",
                "BATCH=1",
                "DOMAINS=youtube.com",
                "GP_MD_CURL_PARALLELISM=30",
                "--",
                "/opt/zapret2/blockcheck2.sh",
            ],
        )

    def test_stop_process_group_terminates_process(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=hasattr(os, "setsid"))
        try:
            _stop_process_group(process)
            self.assertIsNotNone(process.returncode)
        finally:
            if process.poll() is None:
                process.kill()

    def test_managed_stop_signals_registered_run_and_local_supervisor_for_term_and_kill(self) -> None:
        process = mock.Mock(pid=12345)
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired(["blockcheck2.sh"], 5), None]

        with (
            mock.patch("gp_control_plane.zapret2._is_root", return_value=False),
            mock.patch("gp_control_plane.zapret2.signal_registered_process_run") as signal_registered,
            mock.patch("gp_control_plane.zapret2.os.killpg", create=True) as killpg,
            mock.patch("gp_control_plane.zapret2.signal.SIGTERM", "term-signal"),
            mock.patch("gp_control_plane.zapret2.signal.SIGKILL", "kill-signal", create=True),
        ):
            _stop_process_group(process, run_id="managed-run")

        self.assertEqual(
            signal_registered.call_args_list,
            [mock.call("managed-run", "TERM"), mock.call("managed-run", "KILL")],
        )
        self.assertEqual(
            killpg.call_args_list,
            [mock.call(12345, "term-signal"), mock.call(12345, "kill-signal")],
        )
        self.assertEqual(process.wait.call_count, 2)

    def test_managed_signal_still_signals_local_supervisor_when_helper_fails(self) -> None:
        process = mock.Mock(pid=12345)

        with (
            mock.patch("gp_control_plane.zapret2._is_root", return_value=False),
            mock.patch(
                "gp_control_plane.zapret2.signal_registered_process_run",
                side_effect=RuntimeError("root-helper rejected registered process signal"),
            ) as signal_registered,
            mock.patch("gp_control_plane.zapret2.os.killpg", create=True) as killpg,
            mock.patch("gp_control_plane.zapret2.signal.SIGTERM", "term-signal"),
        ):
            with self.assertRaisesRegex(RuntimeError, "root-helper rejected"):
                _signal_process_group("TERM", process, run_id="managed-run")

        signal_registered.assert_called_once_with("managed-run", "TERM")
        killpg.assert_called_once_with(12345, "term-signal")

    def test_stop_process_group_propagates_timeout_after_kill_wait(self) -> None:
        process = mock.Mock(pid=12345)
        process.poll.return_value = None
        timeout = subprocess.TimeoutExpired(["blockcheck2.sh"], 5)
        process.wait.side_effect = [timeout, timeout]

        with mock.patch("gp_control_plane.zapret2._signal_process_group") as signal_process_group:
            with self.assertRaises(subprocess.TimeoutExpired):
                _stop_process_group(process)

        self.assertEqual(
            signal_process_group.call_args_list,
            [mock.call("TERM", process, None), mock.call("KILL", process, None)],
        )
        self.assertEqual(process.wait.call_count, 2)

    def test_blockcheck_nft_tables_extracts_only_temporary_tables(self) -> None:
        output = """
table inet blockcheck1460063
table ip filter
table inet blockcheckabc
table inet blockcheck42
"""

        self.assertEqual(_blockcheck_nft_tables(output), [("inet", "blockcheck1460063"), ("inet", "blockcheck42")])

    def test_registered_process_signal_uses_only_the_run_record(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            mock.patch("gp_control_plane.zapret2._is_root", return_value=False),
            mock.patch("gp_control_plane.zapret2._root_helper_path", return_value="/helper/gp-root-helper"),
            mock.patch("gp_control_plane.zapret2.Path.is_file", return_value=True),
            mock.patch("gp_control_plane.zapret2.shutil.which", return_value="/usr/bin/sudo"),
            mock.patch("gp_control_plane.zapret2.subprocess.run", side_effect=fake_run),
        ):
            signal_registered_process_run("a" * 32, "TERM")

        self.assertEqual(calls, [["/usr/bin/sudo", "-n", "/helper/gp-root-helper", "signal-run", "a" * 32, "TERM"]])

    def test_immediate_stop_waits_for_helper_owned_record_before_signalling(self) -> None:
        run_id = "immediate-stop-race"
        calls: list[list[str]] = []
        responses = [
            subprocess.CompletedProcess([], 126, "", "gp-root-helper: registered process is stale or invalid\n"),
            subprocess.CompletedProcess([], 0, "", ""),
        ]

        def fake_helper(command: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return responses.pop(0)

        with (
            mock.patch("gp_control_plane.zapret2._is_root", return_value=False),
            mock.patch("gp_control_plane.zapret2._run_root_helper", side_effect=fake_helper),
            mock.patch("gp_control_plane.zapret2.time.sleep") as sleep,
        ):
            signal_registered_process_run(run_id, "TERM")

        self.assertEqual(calls, [["signal-run", run_id, "TERM"], ["signal-run", run_id, "TERM"]])
        sleep.assert_called_once()

    def test_signal_registered_process_stops_retrying_at_the_record_wait_deadline(self) -> None:
        failure = subprocess.CompletedProcess([], 126, "", "gp-root-helper: registered process is stale or invalid\n")
        with (
            mock.patch("gp_control_plane.zapret2._is_root", return_value=False),
            mock.patch("gp_control_plane.zapret2._run_root_helper", return_value=failure) as helper,
            mock.patch("gp_control_plane.zapret2.ROOT_HELPER_RECORD_WAIT_SECONDS", 0.1),
            mock.patch("gp_control_plane.zapret2.time.monotonic", side_effect=[10.0, 10.1]),
            mock.patch("gp_control_plane.zapret2.time.sleep") as sleep,
        ):
            with self.assertRaisesRegex(RuntimeError, "stale or invalid"):
                signal_registered_process_run("timeout-race", "TERM")

        self.assertEqual(helper.call_count, 1)
        sleep.assert_not_called()

    def test_root_helper_exposes_no_direct_pid_registration_or_unscoped_cleanup(self) -> None:
        helper = (Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh").read_text(encoding="utf-8")

        self.assertIn('  run-owned)', helper)
        self.assertIn('  run-owned-env)', helper)
        self.assertIn('  signal-run)', helper)
        self.assertIn('    signal_registered_process_run "$@"', helper)
        self.assertNotIn('  register-run)', helper)
        self.assertNotIn('  unregister-run)', helper)
        self.assertNotIn('"kill")', helper)
        self.assertNotIn('"killpg")', helper)
        self.assertNotIn("pgrep", helper)

    def test_root_helper_creates_the_only_signalable_record_and_rejects_direct_registration(self) -> None:
        if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0 or not shutil.which("setsid"):
            self.skipTest("requires a root Linux test environment with setsid")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "blockcheck2.sh"
            target.write_text("#!/bin/sh\ntrap 'exit 0' TERM\nsleep 30\n", encoding="utf-8")
            target.chmod(0o700)
            registry = root / "runs"
            env = {**os.environ, "ZAPRET_DIR": str(root), "GP_ROOT_HELPER_RUN_DIR": str(registry)}
            run_id = "helper-owned-run"
            managed = subprocess.Popen(["sh", str(helper), "run-owned", run_id, str(target)], env=env)
            try:
                record = registry / run_id
                for _ in range(50):
                    if record.exists():
                        break
                    time.sleep(0.05)
                self.assertTrue(record.exists())
                self.assertEqual(record.read_text(encoding="utf-8").split()[0], "helper-v1")

                rejected = subprocess.run(
                    ["sh", str(helper), "register-run", "foreign-pid", str(os.getpid()), str(os.getpgrp()), "1"],
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(rejected.returncode, 126)
                self.assertTrue(record.exists())

                stopped = subprocess.run(
                    ["sh", str(helper), "signal-run", run_id, "TERM"], env=env, text=True, capture_output=True, check=False
                )
                self.assertEqual(stopped.returncode, 0, stopped.stderr)
                self.assertEqual(managed.wait(timeout=5), 0)
                self.assertFalse(record.exists())

                stale_id = "stale-owned-run"
                stale = subprocess.Popen(["sh", str(helper), "run-owned", stale_id, str(target)], env=env)
                try:
                    stale_record = registry / stale_id
                    for _ in range(50):
                        if stale_record.exists():
                            break
                        time.sleep(0.05)
                    self.assertTrue(stale_record.exists())
                    version, pid, pgid, _marker = stale_record.read_text(encoding="utf-8").split()
                    stale_record.write_text(f"{version} {pid} {pgid} stale-marker\n", encoding="utf-8")

                    stale_signal = subprocess.run(
                        ["sh", str(helper), "signal-run", stale_id, "TERM"],
                        env=env,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(stale_signal.returncode, 126)
                    time.sleep(0.1)
                    self.assertIsNone(stale.poll())
                    self.assertFalse(stale_record.exists())
                finally:
                    if stale.poll() is None:
                        stale.terminate()
                    stale.wait(timeout=5)
            finally:
                if managed.poll() is None:
                    subprocess.run(["sh", str(helper), "signal-run", run_id, "KILL"], env=env, check=False)
                    managed.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
