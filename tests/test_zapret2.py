from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.strategies import load_strategy_dir
from gp_control_plane.zapret2 import _blockcheck_nft_tables, _cleanup_blockcheck_processes, _run_blockcheck, blockcheck_env


class Zapret2Tests(unittest.TestCase):
    def test_blockcheck_env_uses_strategy_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            strategy = Path(raw) / "strategy"
            strategy.mkdir()
            (strategy / "metadata.yaml").write_text(
                """
version: 1
id: s1
status: example
protocols:
  - tls
files:
  nfqws2_config: nfqws2.conf
blockcheck:
  test: custom
  ip_versions: 4
  skip_dnscheck: true
  checks:
    http: false
    https_tls12: true
    https_tls13: false
    http3: false
  lists:
    https_tls12: nfqws2.conf
""",
                encoding="utf-8",
            )
            (strategy / "nfqws2.conf").write_text("--payload tls_client_hello\n", encoding="utf-8")

            env = blockcheck_env("youtube.com", load_strategy_dir(strategy))

            self.assertEqual(env["BATCH"], "1")
            self.assertEqual(env["DOMAINS"], "youtube.com")
            self.assertEqual(env["IPVS"], "4")
            self.assertEqual(env["TEST"], "custom")
            self.assertEqual(env["SKIP_DNSCHECK"], "1")
            self.assertEqual(env["ENABLE_HTTP"], "0")
            self.assertEqual(env["ENABLE_HTTPS_TLS12"], "1")
            self.assertEqual(env["ENABLE_HTTPS_TLS13"], "0")
            self.assertEqual(env["ENABLE_HTTP3"], "0")
            self.assertEqual(env["LIST_HTTPS_TLS12"], str((strategy / "nfqws2.conf").resolve()))

    def test_run_blockcheck_returns_completed_process(self) -> None:
        result = _run_blockcheck(
            [sys.executable, "-c", "import sys; print('ok'); print('err', file=sys.stderr)"],
            os.environ.copy(),
            timeout=5,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "ok")
        self.assertEqual(result.stderr.strip(), "err")

    def test_run_blockcheck_raises_on_timeout(self) -> None:
        with self.assertRaises(subprocess.TimeoutExpired):
            _run_blockcheck(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                os.environ.copy(),
                timeout=1,
            )

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
