from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.cli import build_parser


class CliSafetyTests(unittest.TestCase):
    def test_mvp_commands_are_present(self) -> None:
        parser = build_parser()

        self.assertEqual(parser.parse_args(["validate"]).command, "validate")
        self.assertEqual(parser.parse_args(["sync", "--pull-only"]).command, "sync")
        self.assertEqual(parser.parse_args(["render", "--dry-run"]).command, "render")
        self.assertEqual(parser.parse_args(["healthcheck", "--direct-only"]).command, "healthcheck")
        self.assertEqual(parser.parse_args(["evidence", "write", "--no-push"]).evidence_command, "write")
        self.assertEqual(parser.parse_args(["strategy-finder", "domains"]).finder_command, "domains")
        self.assertEqual(
            parser.parse_args(["strategy-finder", "standard-discovery", "--domain", "youtube.com"]).finder_command,
            "standard-discovery",
        )
        self.assertEqual(
            parser.parse_args(["strategy-finder", "custom-verification", "--candidate-id", "tls-test"]).finder_command,
            "custom-verification",
        )
        self.assertEqual(parser.parse_args(["web"]).command, "web")

    def test_config_argument_can_be_after_command(self) -> None:
        from gp_control_plane.cli import _normalize_argv

        self.assertEqual(
            _normalize_argv(["validate", "--config", "configs/orchestrator.example.yaml"]),
            ["--config", "configs/orchestrator.example.yaml", "validate"],
        )
        self.assertEqual(
            _normalize_argv(["render", "--dry-run", "--config=configs/orchestrator.example.yaml"]),
            ["--config=configs/orchestrator.example.yaml", "render", "--dry-run"],
        )

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
