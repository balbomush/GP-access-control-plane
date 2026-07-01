from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.zapret2 import (
    BLOCKCHECK_ENV_KEYS,
    _blockcheck_nft_tables,
    _cleanup_blockcheck_processes,
    _stop_process_group,
    check_install,
    check_install_cached,
    clear_install_check_cache,
    root_command,
    root_helper_status,
)


class Zapret2Tests(unittest.TestCase):
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
            command = root_command(["/opt/zapret2/blockcheck2.sh"], env=env, pass_env_keys=BLOCKCHECK_ENV_KEYS)

        self.assertEqual(
            command,
            [
                "/usr/bin/sudo",
                "-n",
                "/helper/gp-root-helper",
                "run-env",
                "BATCH=1",
                "DOMAINS=youtube.com",
                "ENABLE_HTTP3=1",
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

    def test_blockcheck_nft_tables_extracts_only_temporary_tables(self) -> None:
        output = """
table inet blockcheck1460063
table ip filter
table inet blockcheckabc
table inet blockcheck42
"""

        self.assertEqual(_blockcheck_nft_tables(output), [("inet", "blockcheck1460063"), ("inet", "blockcheck42")])

    def test_cleanup_blockcheck_processes_kills_remaining_pids(self) -> None:
        pgrep_outputs = iter(
            [
                subprocess.CompletedProcess(["pgrep"], 0, "101\n", ""),
                subprocess.CompletedProcess(["pgrep"], 0, "102\n", ""),
                subprocess.CompletedProcess(["pgrep"], 1, "", ""),
                subprocess.CompletedProcess(["pgrep"], 0, "102\n", ""),
            ]
        )
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            if command[0] == "pgrep":
                return next(pgrep_outputs)
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            mock.patch("gp_control_plane.zapret2.shutil.which", side_effect=lambda name: name if name in {"pgrep", "sudo"} else None),
            mock.patch("gp_control_plane.zapret2.Path.is_file", return_value=True),
            mock.patch("gp_control_plane.zapret2._root_helper_path", return_value="/helper/gp-root-helper"),
            mock.patch("gp_control_plane.zapret2.subprocess.run", side_effect=fake_run),
            mock.patch("gp_control_plane.zapret2.time.sleep"),
        ):
            _cleanup_blockcheck_processes()

        self.assertIn(["kill", "-TERM", "101", "102"], calls)
        self.assertIn(["sudo", "-n", "/helper/gp-root-helper", "kill", "TERM", "101", "102"], calls)
        self.assertIn(["kill", "-KILL", "102"], calls)
        self.assertIn(["sudo", "-n", "/helper/gp-root-helper", "kill", "KILL", "102"], calls)


if __name__ == "__main__":
    unittest.main()
