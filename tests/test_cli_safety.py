from __future__ import annotations

import sys
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.cli import build_parser


class CliSafetyTests(unittest.TestCase):
    def test_current_commands_are_present(self) -> None:
        parser = build_parser()

        self.assertEqual(parser.parse_args(["zapret2", "check-install"]).zapret_command, "check-install")
        self.assertEqual(parser.parse_args(["strategy-finder", "domains"]).finder_command, "domains")
        self.assertEqual(parser.parse_args(["strategy-finder", "candidates"]).finder_command, "candidates")
        self.assertEqual(
            parser.parse_args(["strategy-finder", "standard-discovery", "--domain", "youtube.com"]).finder_command,
            "standard-discovery",
        )
        self.assertEqual(
            parser.parse_args(["strategy-finder", "multi-domain-discovery", "--domain", "youtube.com"]).finder_command,
            "multi-domain-discovery",
        )
        self.assertEqual(
            parser.parse_args(
                ["strategy-finder", "multi-domain-discovery", "--domain", "youtube.com", "--curl-parallelism", "6"]
            ).curl_parallelism,
            6,
        )
        self.assertEqual(
            parser.parse_args(["strategy-finder", "multi-domain-discovery", "--domain", "youtube.com"]).curl_parallelism,
            4,
        )
        self.assertEqual(parser.parse_args(["storage", "status"]).storage_command, "status")
        self.assertEqual(
            parser.parse_args(["domain-sources", "prepare-v2fly"]).domain_sources_command,
            "prepare-v2fly",
        )
        standard_args = parser.parse_args(
            [
                "strategy-finder",
                "standard-discovery",
                "--domain",
                "youtube.com",
                "--enable-http",
                "--no-tls12",
                "--enable-tls13",
                "--scan-level",
                "force",
                "--repeats",
                "3",
                "--repeat-parallel",
                "--no-skip-dnscheck",
                "--no-skip-ipblock",
            ]
        )
        self.assertTrue(standard_args.enable_http)
        self.assertTrue(standard_args.no_tls12)
        self.assertTrue(standard_args.enable_tls13)
        self.assertEqual(standard_args.scan_level, "force")
        self.assertEqual(standard_args.repeats, 3)
        self.assertTrue(standard_args.repeat_parallel)
        self.assertTrue(standard_args.no_skip_dnscheck)
        self.assertTrue(standard_args.no_skip_ipblock)
        self.assertEqual(parser.parse_args(["web"]).command, "web")

    def test_future_commands_are_absent(self) -> None:
        parser = build_parser()
        removed_commands = [
            ["validate"],
            ["sync", "--pull-only"],
            ["render", "--dry-run"],
            ["healthcheck", "--direct-only"],
            ["evidence", "write", "--no-push"],
            ["zapret2", "list-local"],
            ["zapret2", "run-check", "--domain", "youtube.com", "--strategy", "strategy"],
            ["strategy-finder", "custom-verification", "--candidate-id", "tls-test"],
        ]

        for command in removed_commands:
            with self.subTest(command=command), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit):
                    parser.parse_args(command)

    def test_state_dir_argument_can_be_after_command(self) -> None:
        from gp_control_plane.cli import _normalize_argv

        self.assertEqual(
            _normalize_argv(["web", "--state-dir", "/tmp/gp-state"]),
            ["--state-dir", "/tmp/gp-state", "web"],
        )
        self.assertEqual(
            _normalize_argv(["strategy-finder", "domains", "--state-dir=/tmp/gp-state"]),
            ["--state-dir=/tmp/gp-state", "strategy-finder", "domains"],
        )

    def test_config_argument_is_removed(self) -> None:
        parser = build_parser()

        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["--config", "configs/orchestrator.example.yaml", "web"])

    def test_forbidden_router_operations_are_not_in_source(self) -> None:
        source_root = Path(__file__).resolve().parents[1] / "src" / "gp_control_plane"
        forbidden = ("apply", "restart", "ssh", "rci", "keenetic")
        hits: list[str] = []
        for path in source_root.rglob("*.py"):
            text = path.read_text(encoding="utf-8").lower()
            for word in forbidden:
                if word in text:
                    hits.append(f"{path.name}:{word}")

        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
