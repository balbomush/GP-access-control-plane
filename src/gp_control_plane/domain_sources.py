from __future__ import annotations

import re
import json
import io
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import URLError
from urllib.request import urlopen

from .storage import read_custom_presets, save_custom_preset


V2FLY_BASE_URL = "https://raw.githubusercontent.com/v2fly/domain-list-community/master/data"
V2FLY_CONTENTS_URL = "https://api.github.com/repos/v2fly/domain-list-community/contents/data?ref=master"
V2FLY_REVISION_URL = "https://api.github.com/repos/v2fly/domain-list-community/commits/master"
V2FLY_GIT_URL = "https://github.com/v2fly/domain-list-community.git"
V2FLY_ARCHIVE_URL = "https://codeload.github.com/v2fly/domain-list-community/tar.gz/refs/heads/master"
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
_EXPECTED_V2FLY_SOURCE_ERRORS = (
    OSError,
    TimeoutError,
    URLError,
    subprocess.SubprocessError,
    tarfile.TarError,
    ValueError,
)
_EXPECTED_V2FLY_REVISION_FALLBACK_ERRORS = (
    OSError,
    TimeoutError,
    subprocess.SubprocessError,
)
_EXPECTED_V2FLY_ARCHIVE_FALLBACK_ERRORS = (
    OSError,
    TimeoutError,
    URLError,
    tarfile.TarError,
)


def builtin_preset_sources() -> dict[str, dict[str, str]]:
    return {
        "critical": _manual_source("critical"),
        "coverage": _manual_source("coverage"),
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
    errors: list[dict[str, str]] = []
    try:
        text = fetcher() if fetcher else fetch_v2fly_category_index()
        categories = parse_v2fly_category_index(text)
    except _EXPECTED_V2FLY_SOURCE_ERRORS as exc:
        source = "fallback"
        errors.append(_v2fly_error("catalog", exc))
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
        "errors": errors,
        "error_kind": errors[0]["kind"] if errors else "",
        "error_message": _format_v2fly_errors(errors),
    }


def list_v2fly_categories_cached(
    state_dir: Path,
    query: str = "",
    *,
    limit: int = 2000,
    refresh: bool = False,
    check_update: bool = True,
    index_fetcher: Callable[[], str] | None = None,
    revision_fetcher: Callable[[], str] | None = None,
) -> dict[str, Any]:
    cache, cache_error = _read_v2fly_catalog_cache(state_dir)
    errors: list[dict[str, str]] = []
    if cache_error:
        errors.append(cache_error)
    remote_revision = ""
    checked_at = _utc_now()
    if check_update or refresh or not cache:
        try:
            remote_revision = parse_v2fly_revision(revision_fetcher() if revision_fetcher else fetch_v2fly_revision())
        except _EXPECTED_V2FLY_SOURCE_ERRORS as exc:
            errors.append(_v2fly_error("revision", exc))

    local_revision = str((cache or {}).get("revision") or "")
    update_available = bool(cache and remote_revision and local_revision and remote_revision != local_revision)
    should_refresh = not cache or (refresh and (update_available or not local_revision))
    if should_refresh:
        try:
            text = index_fetcher() if index_fetcher else fetch_v2fly_category_index()
            categories = parse_v2fly_category_index(text)
            cache = {
                "source": "github",
                "revision": remote_revision or local_revision,
                "checked_at": checked_at,
                "categories": categories,
            }
            try:
                write_v2fly_catalog_cache(state_dir, cache)
            except OSError as exc:
                errors.append(_v2fly_error("cache_write", exc))
            local_revision = str(cache.get("revision") or "")
            update_available = False
        except _EXPECTED_V2FLY_SOURCE_ERRORS as exc:
            errors.append(_v2fly_error("catalog_refresh", exc))
            if not cache:
                cache = {
                    "source": "fallback",
                    "revision": "",
                    "checked_at": checked_at,
                    "categories": list(_FALLBACK_V2FLY_CATEGORIES),
                }

    categories = list((cache or {}).get("categories") or [])
    source = str((cache or {}).get("source") or "cache")
    if remote_revision and cache and local_revision == remote_revision:
        cache = {**cache, "checked_at": checked_at}
        try:
            write_v2fly_catalog_cache(state_dir, cache)
        except OSError as exc:
            errors.append(_v2fly_error("cache_write", exc))
    needle = str(query or "").strip().lower()
    filtered = [category for category in categories if needle in category] if needle else categories
    clean_limit = max(1, min(int(limit or 2000), 5000))
    error_message = _format_v2fly_errors(errors)
    return {
        "source": source,
        "query": needle,
        "total": len(filtered),
        "all_count": len(categories),
        "categories": filtered[:clean_limit],
        "has_more": len(filtered) > clean_limit,
        "limit": clean_limit,
        "cached": bool(cache and source != "fallback"),
        "revision": local_revision,
        "remote_revision": remote_revision,
        "checked_at": str((cache or {}).get("checked_at") or checked_at),
        "update_available": update_available,
        "can_refresh": (not cache) or source == "fallback" or update_available,
        "revision_error": error_message,
        "cache_error": _format_v2fly_errors([error for error in errors if error["kind"] == "cache"]),
        "error_kind": errors[0]["kind"] if errors else "",
        "error_message": error_message,
        "errors": errors,
    }


def read_v2fly_catalog_cache(state_dir: Path) -> dict[str, Any] | None:
    cache, _ = _read_v2fly_catalog_cache(state_dir)
    return cache


def _read_v2fly_catalog_cache(state_dir: Path) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    path = v2fly_catalog_cache_path(state_dir)
    if not path.exists():
        return None, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return None, _v2fly_error("cache_read", exc)
    except json.JSONDecodeError as exc:
        return None, _v2fly_error("cache_read", exc)
    if not isinstance(payload, dict):
        return None, _v2fly_error("cache_read", ValueError("invalid v2fly catalog cache"))
    categories: list[str] = []
    seen: set[str] = set()
    for raw in payload.get("categories") or []:
        try:
            clean = _clean_category(str(raw))
        except ValueError:
            continue
        if clean not in seen:
            seen.add(clean)
            categories.append(clean)
    if not categories:
        return None, _v2fly_error("cache_read", ValueError("empty v2fly catalog cache"))
    return {
        "source": str(payload.get("source") or "cache"),
        "revision": str(payload.get("revision") or ""),
        "checked_at": str(payload.get("checked_at") or ""),
        "categories": sorted(categories),
    }, None


def write_v2fly_catalog_cache(state_dir: Path, payload: dict[str, Any]) -> None:
    path = v2fly_catalog_cache_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def _v2fly_error(stage: str, exc: BaseException) -> dict[str, str]:
    return {
        "stage": stage,
        "kind": _v2fly_error_kind(stage, exc),
        "message": _v2fly_error_message(exc),
    }


def _v2fly_error_kind(stage: str, exc: BaseException) -> str:
    if stage.startswith("cache"):
        return "cache"
    if isinstance(exc, (json.JSONDecodeError, ValueError, tarfile.TarError)):
        return "format"
    if isinstance(exc, (OSError, TimeoutError, URLError, subprocess.SubprocessError)):
        return "network"
    return "unexpected"


def _v2fly_error_message(exc: BaseException) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return " ".join(text.split())


def _format_v2fly_errors(errors: list[dict[str, str]]) -> str:
    if not errors:
        return ""
    return "; ".join(f"{error['stage']}: {error['message']}" for error in errors)


def v2fly_catalog_cache_path(state_dir: Path) -> Path:
    return state_dir / "domain-sources" / "v2fly-catalog.json"


def fetch_v2fly_revision() -> str:
    try:
        completed = subprocess.run(
            ["git", "ls-remote", V2FLY_GIT_URL, "refs/heads/master"],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        parts = completed.stdout.strip().split()
        revision = parts[0] if parts else ""
        if revision:
            return revision
    except _EXPECTED_V2FLY_REVISION_FALLBACK_ERRORS:
        pass
    with urlopen(V2FLY_REVISION_URL, timeout=15) as response:  # noqa: S310
        return response.read().decode("utf-8", errors="replace")


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


def fetch_v2fly_category_index() -> str:
    try:
        return fetch_v2fly_category_index_from_archive()
    except _EXPECTED_V2FLY_ARCHIVE_FALLBACK_ERRORS:
        pass
    with urlopen(V2FLY_CONTENTS_URL, timeout=30) as response:  # noqa: S310
        return response.read().decode("utf-8", errors="replace")


def fetch_v2fly_category_index_from_archive() -> str:
    with urlopen(V2FLY_ARCHIVE_URL, timeout=60) as response:  # noqa: S310
        archive = response.read()
    items: list[dict[str, str]] = []
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            parts = member.name.split("/")
            if len(parts) != 3 or parts[1] != "data":
                continue
            try:
                name = _clean_category(parts[2])
            except ValueError:
                continue
            items.append({"name": name, "type": "file"})
    return json.dumps(items)


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
    seen_categories: set[str] = set()
    sources: list[dict[str, Any]] = []
    skipped = {"include": 0, "keyword": 0, "regexp": 0, "geosite": 0, "invalid": 0}

    def visit(category: str, depth: int) -> None:
        if category in seen_categories:
            return
        if depth > 8:
            skipped["include"] += 1
            return
        seen_categories.add(category)
        text = fetch(category)
        parsed = parse_v2fly_rules(text)
        for domain in parsed["domains"]:
            if domain not in seen:
                seen.add(domain)
                domains.append(domain)
        for key, value in parsed["skipped"].items():
            skipped[key] = skipped.get(key, 0) + int(value)
        sources.append(
            {
                "category": category,
                "url": f"{V2FLY_BASE_URL}/{category}",
                "domains": len(parsed["domains"]),
                "includes": len(parsed["includes"]),
            }
        )
        for included in parsed["includes"]:
            visit(included, depth + 1)

    for category in clean_categories:
        visit(category, 0)
    return {"categories": sorted(seen_categories), "domains": domains, "sources": sources, "skipped": skipped}


def fetch_v2fly_category(category: str) -> str:
    clean = _clean_category(category)
    with urlopen(f"{V2FLY_BASE_URL}/{clean}", timeout=30) as response:  # noqa: S310
        return response.read().decode("utf-8", errors="replace")


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
        "url": "src/gp_control_plane/strategy_finder.py",
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
