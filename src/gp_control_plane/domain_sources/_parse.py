"""gp_control_plane.domain_sources._parse — moved from storage.py (split)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable
import json
from gp_control_plane.domain_sources._constants import _CATEGORY_RE, _COVERAGE_NOTE, _DOMAIN_RE


def builtin_preset_sources() -> dict[str, dict[str, str]]:
    return {
        "critical": _manual_source("critical"),
        "coverage": _manual_source("coverage"),
        "google-youtube": _manual_source("google-youtube"),
        "discord": _manual_source("discord"),
        "cloudflare": _manual_source("cloudflare"),
        "amazon-aws": _manual_source("amazon-aws"),
    }


def parse_v2fly_revision(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:80]
    if isinstance(payload, dict):
        value = payload.get("sha")
        if isinstance(value, str) and value.strip():
            return value.strip()
        commit = payload.get("commit")
        if isinstance(commit, dict):
            tree = commit.get("tree")
            if isinstance(tree, dict) and isinstance(tree.get("sha"), str):
                return tree["sha"].strip()
    return ""


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


def parse_v2fly_domains(text: str) -> list[str]:
    return parse_v2fly_rules(text)["domains"]


def parse_v2fly_rules(text: str) -> dict[str, Any]:
    result: list[str] = []
    includes: list[str] = []
    skipped = {"include": 0, "keyword": 0, "regexp": 0, "geosite": 0, "invalid": 0}
    seen: set[str] = set()
    for raw_line in str(text or "").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        lowered = line.lower()
        if lowered.startswith("include:"):
            try:
                category = _clean_category(line.split(":", 1)[1])
            except ValueError:
                skipped["include"] += 1
                continue
            if category not in includes:
                includes.append(category)
            continue
        if lowered.startswith("keyword:"):
            skipped["keyword"] += 1
            continue
        if lowered.startswith("regexp:"):
            skipped["regexp"] += 1
            continue
        if lowered.startswith("geosite:"):
            skipped["geosite"] += 1
            continue
        domain = _domain_from_v2fly_line(raw_line)
        if domain and domain not in seen:
            seen.add(domain)
            result.append(domain)
        elif line and "." in line and not domain:
            skipped["invalid"] += 1
    return {"domains": result, "includes": includes, "skipped": skipped}


def _manual_v2fly_domains(categories: list[str], domains: list[str] | None) -> dict[str, Any]:
    clean_categories = _clean_categories(categories)
    clean_domains: list[str] = []
    seen: set[str] = set()
    skipped = {"include": 0, "keyword": 0, "regexp": 0, "geosite": 0, "invalid": 0}
    for raw_domain in domains or []:
        domain = normalize_domain(raw_domain)
        if not domain:
            skipped["invalid"] += 1
            continue
        if domain not in seen:
            seen.add(domain)
            clean_domains.append(domain)
    if not clean_domains:
        raise ValueError("preset must contain at least one domain")
    return {
        "categories": clean_categories,
        "domains": clean_domains,
        "sources": [{"category": "edited-list", "url": "web-ui", "domains": len(clean_domains), "includes": 0}],
        "skipped": skipped,
    }


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
        "url": "src/gp_control_plane/engine_common/_constants.py",
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
