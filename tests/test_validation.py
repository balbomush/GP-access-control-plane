from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.config import AppConfig, HealthcheckConfig, LocalConfig, OutputConfig, RepoConfig
from gp_control_plane.validation import validate_all


EMPTY_RULES = "version: 1\nrules: []\n"


def write_rules_repo(root: Path, *, zapret: str = "", vpn: str = "", direct: str = "") -> None:
    stable = root / "stable"
    stable.mkdir(parents=True)
    (stable / "zapret.yaml").write_text(zapret or EMPTY_RULES, encoding="utf-8")
    (stable / "vpn.yaml").write_text(vpn or EMPTY_RULES, encoding="utf-8")
    (stable / "direct.yaml").write_text(direct or EMPTY_RULES, encoding="utf-8")


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


class ValidationTests(unittest.TestCase):
    def test_valid_empty_rules(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            rules_repo = tmp / "rules"
            strategies_repo = tmp / "strategies"
            strategies_repo.mkdir()
            write_rules_repo(rules_repo)

            self.assertEqual(validate_all(config_for(tmp, rules_repo, strategies_repo)), [])

    def test_duplicate_ids_fail(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            rules_repo = tmp / "rules"
            strategies_repo = tmp / "strategies"
            strategies_repo.mkdir()
            write_rules_repo(
                rules_repo,
                zapret="""
version: 1
rules:
  - id: same-id
    match:
      domain: one.example
    route: zapret
""",
                vpn="""
version: 1
rules:
  - id: same-id
    match:
      domain: two.example
    route: vpn
""",
            )

            errors = validate_all(config_for(tmp, rules_repo, strategies_repo))
            self.assertTrue(any("duplicate rule id" in error for error in errors))

    def test_invalid_route_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            rules_repo = tmp / "rules"
            strategies_repo = tmp / "strategies"
            strategies_repo.mkdir()
            write_rules_repo(
                rules_repo,
                zapret="""
version: 1
rules:
  - id: bad-route
    match:
      domain: bad.example
    route: tunnel
""",
            )

            errors = validate_all(config_for(tmp, rules_repo, strategies_repo))
            self.assertTrue(any("route must be one of" in error for error in errors))

    def test_stable_conflict_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            rules_repo = tmp / "rules"
            strategies_repo = tmp / "strategies"
            strategies_repo.mkdir()
            match = """
    match:
      domain: conflict.example
"""
            write_rules_repo(
                rules_repo,
                zapret=f"""
version: 1
rules:
  - id: conflict-zapret
{match}    route: zapret
""",
                direct=f"""
version: 1
rules:
  - id: conflict-direct
{match}    route: direct
""",
            )

            errors = validate_all(config_for(tmp, rules_repo, strategies_repo))
            self.assertTrue(any("route conflict" in error for error in errors))

    def test_secret_like_fields_fail(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            rules_repo = tmp / "rules"
            strategies_repo = tmp / "strategies"
            strategies_repo.mkdir()
            write_rules_repo(rules_repo)
            evidence = rules_repo / "evidence"
            evidence.mkdir()
            (evidence / "bad.yaml").write_text("token: abc\n", encoding="utf-8")

            errors = validate_all(config_for(tmp, rules_repo, strategies_repo))
            self.assertTrue(any("secret-like field" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
