from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.strategy_safety import analyze_strategy, normalize_strategy_args


class StrategySafetyTests(unittest.TestCase):
    def test_fake_without_position_is_position_free(self) -> None:
        analysis = analyze_strategy("tls", "--payload=tls_client_hello --lua-desync=fake")

        self.assertEqual(analysis.fragmentation_class, "position_free")
        self.assertTrue(analysis.fragmentation_safe)
        self.assertEqual(analysis.family, "fake")
        self.assertIn("fake", analysis.family_key)

    def test_numeric_split_position_is_risky(self) -> None:
        analysis = analyze_strategy("tls", "--payload=tls_client_hello --lua-desync=multisplit:pos=1")

        self.assertEqual(analysis.fragmentation_class, "position_risky")
        self.assertFalse(analysis.fragmentation_safe)
        self.assertEqual(analysis.family, "multidisorder")
        self.assertIn("numeric", analysis.fragmentation_reason)

    def test_named_tls_marker_is_position_safe(self) -> None:
        analysis = analyze_strategy(
            "tls",
            "--payload=tls_client_hello --lua-desync=multidisorder:pos=sniext+1,host+1",
        )

        self.assertEqual(analysis.fragmentation_class, "position_safe")
        self.assertTrue(analysis.fragmentation_safe)
        self.assertEqual(analysis.family, "multidisorder")

    def test_quic_family_is_not_tied_to_tls_position(self) -> None:
        analysis = analyze_strategy("quic", "--filter-udp=443 --dpi-desync=fake")

        self.assertEqual(analysis.fragmentation_class, "position_free")
        self.assertEqual(analysis.family, "udp/quic")
        self.assertTrue(analysis.fragmentation_safe)

    def test_unknown_arguments_remain_explainable(self) -> None:
        analysis = analyze_strategy("tls", "--payload=tls_client_hello --new-unknown-option=1")

        self.assertEqual(analysis.fragmentation_class, "unknown")
        self.assertEqual(analysis.family, "other")
        self.assertIn("no known", analysis.family_reason)

    def test_normalize_strategy_args_collapses_quoted_whitespace(self) -> None:
        self.assertEqual(
            normalize_strategy_args(' --payload "tls_client_hello"   --lua-desync=fake '),
            "--payload tls_client_hello --lua-desync=fake",
        )


if __name__ == "__main__":
    unittest.main()
