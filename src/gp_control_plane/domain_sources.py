from __future__ import annotations

import re
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.request import urlopen

from .storage import read_custom_presets, save_custom_preset


V2FLY_BASE_URL = "https://raw.githubusercontent.com/v2fly/domain-list-community/master/data"
V2FLY_CONTENTS_URL = "https://api.github.com/repos/v2fly/domain-list-community/contents/data?ref=master"
_COVERAGE_NOTE = "publicly known verifiable domain set; not a guarantee of full service coverage"
_CATEGORY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")
_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
_FALLBACK_V2FLY_CATEGORIES = [
    "amazon",
    "cloudflare",
    "discord",
    "facebook",
    "google",
    "instagram",
    "meta",
    "telegram",
    "youtube",
]


def builtin_preset_sources() -> dict[str, dict[str, str]]:
    return {
        "critical": _manual_source("critical"),
        "coverage": _manual_source("coverage"),
        "diagnostic": _manual_source("diagnostic"),
        "google-youtube": _manual_source("google-youtube"),
        "discord": _manual_source("discord"),
        "cloudflare": _manual_source("cloudflare"),
        "amazon-aws": _manual_source("amazon-aws"),
    }


def list_v2fly_categories(
    query: str = "",
    *,
    limit: int = 80,
    fetcher: Callable[[], str] | None = None,
) -> dict[str, Any]:
    source = "github"
    try:
        text = fetcher() if fetcher else fetch_v2fly_category_index()
        categories = parse_v2fly_category_index(text)
    except Exception:  # noqa: BLE001
        source = "fallback"
        categories = list(_FALLBACK_V2FLY_CATEGORIES)
    needle = str(query or "").strip().lower()
    if needle:
        categories = [category for category in categories if needle in category]
    clean_limit = max(1, min(int(limit or 80), 500))
    return {
        "source": source,
        "query": needle,
        "total": len(categories),
        "categories": categories[:clean_limit],
        "has_more": len(categories) > clean_limit,
        "limit": clean_limit,
    }


def fetch_v2fly_category_index() -> str:
    with urlopen(V2FLY_CONTENTS_URL, timeout=30) as response:  # noqa: S310
        return response.read().decode("utf-8", errors="replace")


def parse_v2fly_category_index(text: str) -> list[str]:
    try:
        payload = json.loads(text or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError("invalid v2fly category index") from exc
    if not isinstance(payload, list):
        raise ValueError("invalid v2fly category index")
    categories: list[str] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "file":
            continue
        try:
            clean = _clean_category(str(item.get("name") or ""))
        except ValueError:
            continue
        if clean not in seen:
            seen.add(clean)
            categories.append(clean)
    return sorted(categories)


def preview_v2fly_preset(
    state_dir: Path,
    *,
    scope: str,
    name: str,
    categories: list[str],
    fetcher: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    clean_scope = _clean_scope(scope)
    clean_name = _clean_name(name)
    collected = collect_v2fly_domains(categories, fetcher=fetcher)
    existing = read_custom_presets(state_dir).get(clean_scope, {}).get(clean_name, [])
    existing_set = set(existing)
    incoming_set = set(collected["domains"])
    return {
        "scope": clean_scope,
        "preset": clean_name,
        "coverage_note": _COVERAGE_NOTE,
        "categories": collected["categories"],
        "sources": collected["sources"],
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
    fetcher: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    preview = preview_v2fly_preset(state_dir, scope=scope, name=name, categories=categories, fetcher=fetcher)
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


def collect_v2fly_domains(
    categories: list[str],
    *,
    fetcher: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    clean_categories = _clean_categories(categories)
    if not clean_categories:
        raise ValueError("at least one v2fly category is required")
    fetch = fetcher or fetch_v2fly_category
    domains: list[str] = []
    seen: set[str] = set()
    sources: list[dict[str, Any]] = []
    for category in clean_categories:
        text = fetch(category)
        parsed = parse_v2fly_domains(text)
        for domain in parsed:
            if domain not in seen:
                seen.add(domain)
                domains.append(domain)
        sources.append(
            {
                "category": category,
                "url": f"{V2FLY_BASE_URL}/{category}",
                "domains": len(parsed),
            }
        )
    return {"categories": clean_categories, "domains": domains, "sources": sources}


def fetch_v2fly_category(category: str) -> str:
    clean = _clean_category(category)
    with urlopen(f"{V2FLY_BASE_URL}/{clean}", timeout=30) as response:  # noqa: S310
        return response.read().decode("utf-8", errors="replace")


def parse_v2fly_domains(text: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw_line in str(text or "").splitlines():
        domain = _domain_from_v2fly_line(raw_line)
        if domain and domain not in seen:
            seen.add(domain)
            result.append(domain)
    return result


def normalize_domain(value: str) -> str:
    domain = str(value or "").strip().lower()
    if not domain:
        return ""
    domain = domain.split()[0]
    domain = domain.split("@", 1)[0].strip()
    domain = domain.removeprefix("*.").removeprefix(".").rstrip(".")
    if not domain or "/" in domain or ":" in domain:
        return ""
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        return ""
    return domain if _DOMAIN_RE.match(domain) else ""


def _domain_from_v2fly_line(raw_line: str) -> str:
    line = raw_line.split("#", 1)[0].strip()
    if not line:
        return ""
    lowered = line.lower()
    if lowered.startswith(("include:", "regexp:", "keyword:", "geosite:")):
        return ""
    for prefix in ("domain:", "full:"):
        if lowered.startswith(prefix):
            return normalize_domain(line[len(prefix) :])
    if "." in line:
        return normalize_domain(line)
    return ""


def _clean_categories(categories: list[str]) -> list[str]:
    result: list[str] = []
    for category in categories:
        clean = _clean_category(category)
        if clean and clean not in result:
            result.append(clean)
    return result


def _clean_category(category: str) -> str:
    clean = str(category or "").strip().lower()
    clean = clean.removeprefix("data/").strip("/")
    if not clean or ".." in clean or "/" in clean or not _CATEGORY_RE.match(clean):
        raise ValueError(f"invalid v2fly category: {category}")
    return clean


def _clean_scope(scope: str) -> str:
    clean = str(scope or "finder").strip()
    if clean not in {"finder", "common"}:
        raise ValueError("scope must be finder or common")
    return clean


def _clean_name(name: str) -> str:
    clean = str(name or "").strip()
    if not clean:
        raise ValueError("preset name is required")
    return clean


def _manual_source(key: str) -> dict[str, str]:
    return {
        "type": "manual",
        "source": "gp-control-plane built-in preset",
        "coverage_note": _COVERAGE_NOTE,
        "key": key,
        "url": "src/gp_control_plane/strategy_finder.py",
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
