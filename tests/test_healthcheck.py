from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.healthcheck import check_domains_direct


class HealthcheckTests(unittest.TestCase):
    def test_dns_failure_is_reported_not_raised(self) -> None:
        def resolver(_: str) -> list[str]:
            raise RuntimeError("dns down")

        results = check_domains_direct(["blocked.example"], resolver=resolver)

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertIn("dns:", results[0].error or "")

    def test_probe_can_be_mocked_for_success(self) -> None:
        results = check_domains_direct(
            ["ok.example"],
            resolver=lambda _: ["203.0.113.10"],
            https_probe=lambda _domain, _address, _timeout: (True, True, 12, None),
        )

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].ok)
        self.assertEqual(results[0].latency_ms, 12)


if __name__ == "__main__":
    unittest.main()
