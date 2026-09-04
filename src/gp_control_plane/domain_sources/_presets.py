"""gp_control_plane.domain_sources._presets — moved from storage.py (split)."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from gp_control_plane.domain_sources._constants import _COVERAGE_NOTE, V2FLY_BASE_URL
from gp_control_plane.domain_sources._network import collect_v2fly_domains
from gp_control_plane.domain_sources._parse import (
    _clean_name,
    _clean_scope,
    _manual_v2fly_domains,
    _utc_now,
)
from gp_control_plane.storage import read_custom_presets, save_custom_preset


def preview_v2fly_preset(
    state_dir: Path,
    *,
    scope: str,
    name: str,
    categories: list[str],
    domains: list[str] | None = None,
    fetcher: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    clean_scope = _clean_scope(scope)
    clean_name = _clean_name(name)
    collected = _manual_v2fly_domains(categories, domains) if domains else collect_v2fly_domains(categories, fetcher=fetcher)
    existing = read_custom_presets(state_dir).get(clean_scope, {}).get(clean_name, [])
    existing_set = set(existing)
    incoming_set = set(collected["domains"])
    return {
        "scope": clean_scope,
        "preset": clean_name,
        "coverage_note": _COVERAGE_NOTE,
        "categories": collected["categories"],
        "sources": collected["sources"],
        "skipped": collected.get("skipped", {}),
        "domains": collected["domains"],
        "count": len(collected["domains"]),
        "existing_count": len(existing),
        "added": [domain for domain in collected["domains"] if domain not in existing_set],
        "removed": [domain for domain in existing if domain not in incoming_set],
        "unchanged_count": len(existing_set & incoming_set),
    }


def import_v2fly_preset(
    state_dir: Path,
    *,
    scope: str,
    name: str,
    categories: list[str],
    domains: list[str] | None = None,
    fetcher: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    preview = preview_v2fly_preset(
        state_dir,
        scope=scope,
        name=name,
        categories=categories,
        domains=domains,
        fetcher=fetcher,
    )
    source = {
        "type": "v2fly/domain-list-community",
        "base_url": V2FLY_BASE_URL,
        "categories": preview["categories"],
        "updated_at": _utc_now(),
    }
    custom = save_custom_preset(
        state_dir,
        scope=preview["scope"],
        name=preview["preset"],
        domains=preview["domains"],
        updated_at=source["updated_at"],
        source=source,
    )
    return {**preview, "custom": custom, "source": source}
