from __future__ import annotations

from typing import Any

from .config import AppConfig
from .domain_sources import list_v2fly_categories_local, read_v2fly_catalog_cache


def v2fly_storage_status_payload(config: AppConfig, raw: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = raw or list_v2fly_categories_local(config.output.state_dir, limit=1)
    state = "ready" if str(payload.get("data_status") or "") == "local" else "missing"
    if payload.get("error_kind") and state != "ready":
        state = "error"
    cache = read_v2fly_catalog_cache(config.output.state_dir) or {}
    last_update_check = {}
    if cache.get("checked_at") or cache.get("remote_revision") or cache.get("update_available") is not None:
        last_update_check = {
            "checked_at": str(cache.get("checked_at") or ""),
            "has_updates": bool(cache.get("update_available")),
            "remote_revision": str(cache.get("remote_revision") or cache.get("revision") or ""),
        }
    return {
        "state": state,
        "source_repo": "v2fly/domain-list-community",
        "source_ref": "master",
        "source_commit": str(payload.get("revision") or ""),
        "prepared_at": str(payload.get("checked_at") or ""),
        "group_count": int(payload.get("all_count") or payload.get("total") or 0),
        "archive_sha256": "",
        "catalog_sha256": "",
        "last_update_check": last_update_check,
    }
