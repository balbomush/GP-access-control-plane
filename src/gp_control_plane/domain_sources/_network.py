"""gp_control_plane.domain_sources._network — moved from storage.py (split)."""
from __future__ import annotations

import io
import json
import logging
import subprocess
import tarfile
from collections.abc import Callable
from typing import Any
from urllib.request import urlopen

from gp_control_plane.domain_sources._constants import (
    _EXPECTED_V2FLY_ARCHIVE_FALLBACK_ERRORS,
    _EXPECTED_V2FLY_REVISION_FALLBACK_ERRORS,
    V2FLY_ARCHIVE_URL,
    V2FLY_BASE_URL,
    V2FLY_CONTENTS_URL,
    V2FLY_GIT_URL,
    V2FLY_REVISION_URL,
)
from gp_control_plane.domain_sources._parse import (
    _clean_categories,
    _clean_category,
    parse_v2fly_rules,
)

log = logging.getLogger(__name__)
def fetch_v2fly_archive() -> bytes:
    with urlopen(V2FLY_ARCHIVE_URL, timeout=60) as response:  # noqa: S310
        return response.read()


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
        log.warning("v2fly revision fetch failed; falling back")
        pass
    with urlopen(V2FLY_REVISION_URL, timeout=15) as response:  # noqa: S310
        return response.read().decode("utf-8", errors="replace")


def fetch_v2fly_category_index() -> str:
    try:
        return fetch_v2fly_category_index_from_archive()
    except _EXPECTED_V2FLY_ARCHIVE_FALLBACK_ERRORS:
        log.warning("v2fly archive fetch failed; returning empty")
        pass
    with urlopen(V2FLY_CONTENTS_URL, timeout=30) as response:  # noqa: S310
        return response.read().decode("utf-8", errors="replace")


def fetch_v2fly_category_index_from_archive() -> str:
    archive = fetch_v2fly_archive()
    files = _extract_v2fly_data_files(archive)
    items: list[dict[str, str]] = []
    for name in sorted(files):
        items.append({"name": name, "type": "file"})
    return json.dumps(items)


def _extract_v2fly_data_files(archive: bytes) -> dict[str, str]:
    files: dict[str, str] = {}
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
            extracted = tar.extractfile(member)
            if extracted is None:
                continue
            files[name] = extracted.read().decode("utf-8", errors="replace")
    return files


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

