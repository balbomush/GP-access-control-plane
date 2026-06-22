from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.strategy_finder import candidate_id_for, parse_blockcheck_stdout, read_candidates, upsert_candidates


class StrategyFinderTests(unittest.TestCase):
    def test_parse_blockcheck_summary_extracts_candidates(self) -> None:
        stdout = """
noise
* SUMMARY
curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload tls_client_hello --lua-desync=fake
curl_test_http3 ipv4 discord.com : nfqws2 --filter-udp=443 --dpi-desync=fake
curl_test_https_tls12 ipv4 web.telegram.org : nfqws2 not working
"""

        parsed = parse_blockcheck_stdout(stdout)

        self.assertEqual(len(parsed["candidates"]), 2)
        self.assertEqual(parsed["candidates"][0]["protocol"], "tls")
        self.assertEqual(parsed["candidates"][1]["protocol"], "quic")
        self.assertEqual(parsed["not_working"][0]["domain"], "web.telegram.org")

    def test_upsert_candidates_persists_unique_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            parsed = {
                "candidates": [
                    {
                        "domain": "youtube.com",
                        "test": "curl_test_https_tls12",
                        "ip_version": "4",
                        "protocol": "tls",
                        "args": "--payload tls_client_hello --lua-desync=fake",
                    }
                ]
            }
            run = {"id": "run1"}

            upsert_candidates(state_dir, parsed, run)
            upsert_candidates(state_dir, parsed, run)
            candidates = read_candidates(state_dir)

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["id"], candidate_id_for("tls", "--payload tls_client_hello --lua-desync=fake"))
            self.assertEqual(len(candidates[0]["seen"]), 2)


if __name__ == "__main__":
    unittest.main()
