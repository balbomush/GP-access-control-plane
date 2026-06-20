from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.config import AppConfig, HealthcheckConfig, LocalConfig, OutputConfig, RepoConfig
from gp_control_plane.render import render_dry_run


EMPTY_RULES = "version: 1\nrules: []\n"


def write_rules_repo(root: Path, *, zapret: str = "", vpn: str = "", direct: str = "") -> None:
    stable = root / "stable"
    stable.mkdir(parents=True)
    (stable / "zapret.yaml").write_text(zapret or EMPTY_RULES, encoding="utf-8")
    (stable / "vpn.yaml").write_text(vpn or EMPTY_RULES, encoding="utf-8")
    (stable / "direct.yaml").write_text(direct or EMPTY_RULES, encoding="utf-8")


def write_strategy(root: Path) -> Path:
    strategy = root / "examples" / "s1"
    strategy.mkdir(parents=True)
    (strategy / "metadata.yaml").write_text(
        """
version: 1
id: s1
status: example
files:
  nfqws2_config: nfqws2.conf
""",
        encoding="utf-8",
    )
    (strategy / "nfqws2.conf").write_text("# strategy\n", encoding="utf-8")
    return strategy


def config_for(tmp: Path, rules_repo: Path, strategies_repo: Path) -> AppConfig:
    local = tmp / "site-local-config"
    return AppConfig(
        repos=RepoConfig(rules=rules_repo, strategies=strategies_repo),
        local=LocalConfig(
            overrides=local / "local-overrides.yaml",
            devices=local / "devices.yaml",
            selected_strategy=local / "selected-strategy.yaml",
        ),
        output=OutputConfig(
            rendered_dir=tmp / "build" / "rendered",
            evidence_dir=tmp / "build" / "evidence",
            state_dir=tmp / "build" / "state",
        ),
        healthcheck=HealthcheckConfig(),
    )


class RenderTests(unittest.TestCase):
    def test_render_outputs_routing_hostlist_strategy_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            rules_repo = tmp / "rules"
            strategies_repo = tmp / "strategies"
            strategy = write_strategy(strategies_repo)
            write_rules_repo(
                rules_repo,
                zapret="""
version: 1
rules:
  - id: google-youtube-zapret
    match:
      domain: youtube.com
    route: zapret
""",
                vpn="""
version: 1
rules:
  - id: vpn-one
    match:
      domain: vpn.example
    route: vpn
""",
            )
            local = tmp / "site-local-config"
            local.mkdir()
            (local / "selected-strategy.yaml").write_text(f"strategy_path: {strategy.as_posix()}\n", encoding="utf-8")

            manifest = render_dry_run(config_for(tmp, rules_repo, strategies_repo))
            rendered = tmp / "build" / "rendered"
            routing = json.loads((rendered / "routing.json").read_text(encoding="utf-8"))

            self.assertEqual(routing["final"], "direct")
            self.assertEqual({rule["route"] for rule in routing["rules"]}, {"zapret", "vpn"})
            self.assertEqual((rendered / "dpi-hostlist.txt").read_text(encoding="utf-8"), "youtube.com\n")
            self.assertTrue((rendered / "selected-zapret-strategy" / "metadata.yaml").exists())
            self.assertEqual(manifest["selected_strategy"], "s1")
            self.assertIn("rules_commit", manifest["sources"])
            self.assertIn("strategies_commit", manifest["sources"])

    def test_local_override_and_device_rule_order(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            rules_repo = tmp / "rules"
            strategies_repo = tmp / "strategies"
            strategies_repo.mkdir()
            write_rules_repo(
                rules_repo,
                direct="""
version: 1
rules:
  - id: shared-direct
    match:
      domain: override.example
    route: direct
""",
            )
            local = tmp / "site-local-config"
            local.mkdir()
            (local / "local-overrides.yaml").write_text(
                """
version: 1
rules:
  - id: local-vpn
    match:
      domain: override.example
    route: vpn
""",
                encoding="utf-8",
            )
            (local / "devices.yaml").write_text(
                """
version: 1
rules:
  - id: device-direct
    match:
      domain: override.example
    route: direct
""",
                encoding="utf-8",
            )

            render_dry_run(config_for(tmp, rules_repo, strategies_repo))
            routing = json.loads((tmp / "build" / "rendered" / "routing.json").read_text(encoding="utf-8"))

            self.assertEqual(len(routing["rules"]), 1)
            self.assertEqual(routing["rules"][0]["id"], "device-direct")
            self.assertEqual(routing["rules"][0]["route"], "direct")


if __name__ == "__main__":
    unittest.main()
