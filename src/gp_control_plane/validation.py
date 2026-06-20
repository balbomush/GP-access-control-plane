from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .config import AppConfig
from .models import Rule
from .rules import load_optional_rules, load_stable_rules
from .strategies import load_selected_strategy


SECRET_MARKERS = (
    "token",
    "secret",
    "password",
    "private_key",
    "private-key",
    "endpoint",
    "mac",
    "local_ip",
    "local-ip",
)


def validate_all(config: AppConfig) -> list[str]:
    errors: list[str] = []
    shared: list[Rule] = []
    try:
        shared = load_stable_rules(config.repos.rules)
    except Exception as exc:  # noqa: BLE001
        errors.append(str(exc))
    for path in (
        config.local.overrides,
        config.local.devices,
    ):
        try:
            load_optional_rules(path)
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
    errors.extend(validate_rule_set(shared))
    for path in (config.repos.rules, config.repos.strategies):
        errors.extend(scan_secret_like_yaml(path))
    try:
        load_selected_strategy(config.local.selected_strategy)
    except Exception as exc:  # noqa: BLE001
        errors.append(str(exc))
    return errors


def validate_rule_set(rules: Iterable[Rule]) -> list[str]:
    errors: list[str] = []
    ids: dict[str, str] = {}
    routes_by_match: dict[str, Rule] = {}
    for rule in rules:
        if rule.id in ids:
            errors.append(f"duplicate rule id {rule.id!r}: {ids[rule.id]} and {rule.origin}")
        ids[rule.id] = rule.origin
        existing = routes_by_match.get(rule.match_key())
        if existing and existing.route != rule.route:
            errors.append(
                f"route conflict for match {rule.match}: "
                f"{existing.id}={existing.route} and {rule.id}={rule.route}"
            )
        routes_by_match[rule.match_key()] = rule
    return errors


def scan_secret_like_yaml(root: Path) -> list[str]:
    errors: list[str] = []
    if not root.exists():
        return errors
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".yaml", ".yml", ".json"}:
            text = path.read_text(encoding="utf-8", errors="ignore")
            for marker in SECRET_MARKERS:
                if marker in path.name.lower():
                    errors.append(f"{path}: secret-like filename is not allowed in shared repositories")
                    break
            errors.extend(_scan_text_values(path, text))
    return errors


def _scan_text_values(path: Path, text: str) -> list[str]:
    errors = []
    lower = text.lower()
    for marker in SECRET_MARKERS:
        if f"{marker}:" in lower or f"{marker}_" in lower:
            errors.append(f"{path}: secret-like field {marker!r} is not allowed")
    return errors


def assert_no_secret_like_mapping(data: Any, path: str = "$") -> None:
    if isinstance(data, dict):
        for key, value in data.items():
            key_text = str(key).lower()
            if any(marker in key_text for marker in SECRET_MARKERS):
                raise ValueError(f"secret-like key at {path}.{key}")
            assert_no_secret_like_mapping(value, f"{path}.{key}")
    elif isinstance(data, list):
        for index, value in enumerate(data):
            assert_no_secret_like_mapping(value, f"{path}[{index}]")
