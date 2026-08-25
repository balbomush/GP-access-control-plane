from __future__ import annotations

import os
from typing import Any

from . import __version__
from .config import AppConfig
from .domain_sources import (
    fetch_v2fly_revision,
    parse_v2fly_revision,
    prepare_v2fly_local_storage,
    read_v2fly_group_manifest,
    write_v2fly_catalog_cache,
)
from .releases import release_channel_info
from .state import now_iso, read_state
from .v2fly_payloads import v2fly_storage_status_payload


def v2fly_check_updates_payload(config: AppConfig) -> dict[str, Any]:
    manifest, _error = read_v2fly_group_manifest(config.output.state_dir)
    local_revision = str((manifest or {}).get("revision") or "")
    remote_revision = parse_v2fly_revision(fetch_v2fly_revision())
    checked_at = now_iso()
    categories = list((manifest or {}).get("categories") or [])
    write_v2fly_catalog_cache(
        config.output.state_dir,
        {
            "source": "v2fly/domain-list-community",
            "revision": local_revision,
            "remote_revision": remote_revision,
            "checked_at": checked_at,
            "categories": categories,
            "update_available": bool(remote_revision and remote_revision != local_revision),
        },
    )
    storage = v2fly_storage_status_payload(config)
    return {"status": "success", "operation_id": "v2fly-check-updates", "storage": storage}


def v2fly_update_local_storage_payload(config: AppConfig, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if (payload or {}).get("dry_run"):
        return {
            "status": "dry_run",
            "operation_id": "v2fly-update-local-storage",
            "storage": v2fly_storage_status_payload(config),
        }
    result = prepare_v2fly_local_storage(config.output.state_dir, revision_fetcher=fetch_v2fly_revision)
    return {
        "status": "success",
        "operation_id": "v2fly-update-local-storage",
        "storage": v2fly_storage_status_payload(config),
        "result": result,
    }


def available_releases_payload(settings: dict[str, Any], *, current_version: str = __version__) -> dict[str, Any]:
    stable = release_channel_info(current_version=current_version, channel="stable")
    prerelease = release_channel_info(current_version=current_version, channel="prerelease")
    return {
        "current": {
            "version": current_version,
            "installed_ref": str(settings.get("installed_ref") or ""),
            "commit": "",
        },
        "releases": [_release_item_payload(stable), _release_item_payload(prerelease)],
        "stable_release_url": str(settings.get("stable_release_url") or ""),
        "prerelease_url": str(settings.get("prerelease_url") or ""),
    }


def service_status_payload(
    config: AppConfig,
    *,
    current_version: str = __version__,
    runtime_role: str = "core",
    web_enabled: bool | None = None,
) -> dict[str, Any]:
    state = read_state(config.output.state_dir)
    v2fly = v2fly_storage_status_payload(config)
    mode = _runtime_mode(runtime_role)
    resolved_web_enabled = _web_install_enabled() if web_enabled is None else web_enabled
    return {
        "state": "error" if state.get("last_error") else "active",
        "mode": mode,
        "services": {
            "core": _core_service_status(mode),
            "web": _web_service_status(mode, resolved_web_enabled),
        },
        "version": _installed_version_payload(state, current_version=current_version),
        "data_state": _data_state_payload(config, v2fly),
        "updated_at": now_iso(),
    }


def _release_item_payload(item: dict[str, Any]) -> dict[str, Any]:
    version = str(item.get("available_version") or "")
    return {
        "version": version,
        "channel": str(item.get("channel") or ""),
        "ref": version,
        "url": str(item.get("url") or ""),
        "published_at": str(item.get("published_at") or ""),
    }


def _runtime_mode(runtime_role: str) -> str:
    role = str(runtime_role or "").strip().lower()
    if role in {"core", "monolith", "web_proxy"}:
        return role
    return "unknown"


def _core_service_status(mode: str) -> dict[str, Any]:
    if mode == "monolith":
        state = "embedded"
        role = "current"
    elif mode == "core":
        state = "active"
        role = "current"
    else:
        state = "unknown"
        role = "unknown"
    return {
        "name": os.environ.get("GP_CORE_SERVICE_NAME", "gp-control-plane-core.service"),
        "state": state,
        "role": role,
        "required": True,
    }


def _web_service_status(mode: str, web_enabled: bool) -> dict[str, Any]:
    if mode == "monolith":
        state = "embedded"
        role = "current"
    elif not web_enabled:
        state = "disabled"
        role = "not_installed"
    else:
        state = "unknown"
        role = "proxy"
    return {
        "name": os.environ.get("GP_SERVICE_NAME", "gp-control-plane-web.service"),
        "state": state,
        "role": role,
        "required": bool(web_enabled),
    }


def _web_install_enabled() -> bool:
    raw = os.environ.get("GP_INSTALL_WEB", "on").strip().lower()
    return raw not in {"0", "false", "no", "off", "disabled"}


def _installed_version_payload(state: dict[str, Any], *, current_version: str) -> dict[str, str]:
    settings = state.get("settings") if isinstance(state.get("settings"), dict) else {}
    return {
        "version": current_version,
        "installed_ref": str(settings.get("installed_ref") or os.environ.get("GP_INSTALLED_REF") or ""),
        "commit": str(os.environ.get("GP_INSTALLED_COMMIT") or ""),
    }


def _data_state_payload(config: AppConfig, v2fly: dict[str, Any]) -> dict[str, Any]:
    v2fly_state = str(v2fly.get("state") or "unknown")
    return {
        "state": "ready" if v2fly_state == "ready" else v2fly_state,
        "state_dir": str(config.output.state_dir),
        "v2fly": {
            "state": v2fly_state,
            "source_commit": str(v2fly.get("source_commit") or ""),
            "group_count": int(v2fly.get("group_count") or 0),
        },
    }
