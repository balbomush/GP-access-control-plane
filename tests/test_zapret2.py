from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.zapret2 import _blockcheck_nft_tables, _cleanup_blockcheck_processes, _stop_process_group, check_install


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
            mock.patch("gp_control_plane.zapret2.subprocess.run", side_effect=fake_run),
            mock.patch("gp_control_plane.zapret2.time.sleep"),
        ):
            _cleanup_blockcheck_processes()

        self.assertIn(["kill", "-TERM", "101", "102"], calls)
        self.assertIn(["sudo", "-n", "kill", "-TERM", "101", "102"], calls)
        self.assertIn(["kill", "-KILL", "102"], calls)
        self.assertIn(["sudo", "-n", "kill", "-KILL", "102"], calls)


if __name__ == "__main__":
    unittest.main()
