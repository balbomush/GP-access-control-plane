from __future__ import annotations

from pathlib import Path
from typing import Any

from . import simpleyaml
from .models import Rule


ROUTE_FILES = {
    "zapret": "zapret.yaml",
    "vpn": "vpn.yaml",
    "direct": "direct.yaml",
}


def load_rule_file(path: Path, expected_route: str | None = None) -> list[Rule]:
    data = simpleyaml.load_file(path)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: root must be a mapping")
    if data.get("version") != 1:
        raise ValueError(f"{path}: version must be 1")
    raw_rules = data.get("rules")
    if not isinstance(raw_rules, list):
        raise ValueError(f"{path}: rules must be a list")
    result = []
    for raw in raw_rules:
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: each rule must be a mapping")
        rule = Rule.from_mapping(raw, origin=str(path))
        if expected_route and rule.route != expected_route:
            raise ValueError(f"{path}: rule {rule.id}: route must be {expected_route}")
        result.append(rule)
    return result


def load_stable_rules(rules_repo: Path) -> list[Rule]:
    rules: list[Rule] = []
    for route, filename in ROUTE_FILES.items():
        rules.extend(load_rule_file(rules_repo / "stable" / filename, expected_route=route))
    return rules


def load_optional_rules(path: Path) -> list[Rule]:
    if not path.exists():
        return []
    return load_rule_file(path)


def merge_rules(shared: list[Rule], local: list[Rule], devices: list[Rule] | None = None) -> list[Rule]:
    merged: dict[str, Rule] = {}
    for rule in shared:
        merged[rule.match_key()] = rule
    for rule in local:
        merged[rule.match_key()] = rule
    for rule in devices or []:
        merged[rule.match_key()] = rule
    return sorted(merged.values(), key=lambda r: (-r.priority, r.route, r.id))


def extract_hostlist(rules: list[Rule]) -> list[str]:
    entries: list[str] = []
    seen: set[str] = set()
    for rule in rules:
        if rule.route != "zapret":
            continue
        for value in _hostlist_values(rule.match):
            if value not in seen:
                entries.append(value)
                seen.add(value)
    return entries


def _hostlist_values(match: dict[str, Any]) -> list[str]:
    if isinstance(match.get("domain"), str):
        return [match["domain"]]
    if isinstance(match.get("domain_suffix"), str):
        return [match["domain_suffix"].lstrip(".")]
    if isinstance(match.get("domains"), list):
        return [str(v).lstrip(".") for v in match["domains"]]
    if isinstance(match.get("service"), str):
        return [f"# service:{match['service']}"]
    if isinstance(match.get("category"), str):
        return [f"# category:{match['category']}"]
    return []
