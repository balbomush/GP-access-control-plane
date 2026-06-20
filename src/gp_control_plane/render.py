from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import simpleyaml
from .config import AppConfig
from .models import Rule, StrategySelection
from .rules import extract_hostlist, load_optional_rules, load_stable_rules, merge_rules
from .strategies import load_selected_strategy


def render_dry_run(config: AppConfig) -> dict[str, Any]:
    shared = load_stable_rules(config.repos.rules)
    local = load_optional_rules(config.local.overrides)
    devices = load_optional_rules(config.local.devices)
    rules = merge_rules(shared, local, devices)
    strategy = load_selected_strategy(config.local.selected_strategy)

    out = config.output.rendered_dir
    out.mkdir(parents=True, exist_ok=True)
    _write_routing_json(out / "routing.json", rules)
    _write_hostlist(out / "dpi-hostlist.txt", rules)
    if strategy:
        _copy_strategy(out / "selected-zapret-strategy", strategy)
    manifest = _manifest(config, rules, strategy)
    simpleyaml.dump_file(out / "manifest.yaml", manifest)
    return manifest


def _write_routing_json(path: Path, rules: list[Rule]) -> None:
    data = {
        "version": 1,
        "final": "direct",
        "rules": [rule.to_mapping() for rule in rules],
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_hostlist(path: Path, rules: list[Rule]) -> None:
    entries = extract_hostlist(rules)
    path.write_text("\n".join(entries) + ("\n" if entries else ""), encoding="utf-8")


def _copy_strategy(target: Path, strategy: StrategySelection) -> None:
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(strategy.path / "metadata.yaml", target / "metadata.yaml")
    shutil.copy2(strategy.nfqws2_config, target / "nfqws2.conf")


def _manifest(config: AppConfig, rules: list[Rule], strategy: StrategySelection | None) -> dict[str, Any]:
    return {
        "version": 1,
        "mode": "dry-run",
        "rules_count": len(rules),
        "routes": {
            "direct": sum(1 for rule in rules if rule.route == "direct"),
            "zapret": sum(1 for rule in rules if rule.route == "zapret"),
            "vpn": sum(1 for rule in rules if rule.route == "vpn"),
        },
        "selected_strategy": strategy.metadata.get("id") if strategy else None,
        "sources": {
            "rules_repo": str(config.repos.rules),
            "rules_commit": git_commit(config.repos.rules),
            "strategies_repo": str(config.repos.strategies),
            "strategies_commit": git_commit(config.repos.strategies),
        },
    }


def git_commit(repo: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            text=True,
            capture_output=True,
        )
    except Exception:  # noqa: BLE001
        return None
    value = result.stdout.strip()
    return value if value and "HEAD" not in value else None
