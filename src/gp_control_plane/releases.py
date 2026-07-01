from __future__ import annotations

import json
import re
from typing import Any, Callable
from urllib.request import Request, urlopen


RELEASES_API_URL = "https://api.github.com/repos/balbomush/GP-access-control-plane/releases"
RELEASES_PAGE_URL = "https://github.com/balbomush/GP-access-control-plane/releases"
LATEST_RELEASE_URL = "https://github.com/balbomush/GP-access-control-plane/releases/latest"
_VERSION_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def release_channel_info(
    *,
    current_version: str,
    channel: str,
    fetcher: Callable[[], str] | None = None,
) -> dict[str, Any]:
    clean_channel = channel if channel in {"stable", "prerelease"} else "stable"
    try:
        raw = fetcher() if fetcher else fetch_github_releases()
        releases = parse_github_releases(raw)
        selected = _select_release(releases, clean_channel)
        if not selected:
            raise ValueError("no release found for selected channel")
        available_version = str(selected.get("tag_name") or "").strip()
        release_url = str(selected.get("html_url") or RELEASES_PAGE_URL)
        return {
            "channel": clean_channel,
            "current_version": current_version,
            "available_version": available_version or "-",
            "available_name": str(selected.get("name") or available_version or ""),
            "published_at": str(selected.get("published_at") or ""),
            "url": release_url,
            "checked": True,
            "source": "github",
            "update_available": _version_tuple(available_version) > _version_tuple(current_version),
            "error": "",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "channel": clean_channel,
            "current_version": current_version,
            "available_version": "-",
            "available_name": "",
            "published_at": "",
            "url": LATEST_RELEASE_URL if clean_channel == "stable" else RELEASES_PAGE_URL,
            "checked": False,
            "source": "fallback",
            "update_available": False,
            "error": str(exc),
        }


def fetch_github_releases() -> str:
    request = Request(RELEASES_API_URL, headers={"Accept": "application/vnd.github+json", "User-Agent": "gp-control-plane"})
    with urlopen(request, timeout=20) as response:  # noqa: S310
        return response.read().decode("utf-8", errors="replace")


def parse_github_releases(text: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(text or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError("invalid GitHub releases response") from exc
    if not isinstance(payload, list):
        raise ValueError("invalid GitHub releases response")
    releases: list[dict[str, Any]] = []
    for item in payload:
        if isinstance(item, dict) and not item.get("draft"):
            releases.append(item)
    return releases


def _select_release(releases: list[dict[str, Any]], channel: str) -> dict[str, Any] | None:
    for release in releases:
        prerelease = bool(release.get("prerelease"))
        if channel == "prerelease" and prerelease:
            return release
        if channel == "stable" and not prerelease:
            return release
    return None


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = _VERSION_RE.match(str(value or "").strip())
    if not match:
        return (0, 0, 0)
    return tuple(int(part or 0) for part in match.groups())
