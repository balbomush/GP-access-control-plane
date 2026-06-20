from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.strategies import load_strategy_dir
from gp_control_plane.zapret2 import blockcheck_env


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


if __name__ == "__main__":
    unittest.main()
