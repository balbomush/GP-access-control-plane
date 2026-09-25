"""Core-owned query, command and event operations for existing Web views."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from .. import __version__, core_api
from ..config import AppConfig
from ..domain_sources import builtin_preset_sources
from ..settings import read_settings
from ..state import now_iso, read_state, update_state
from ..storage import (
    delete_user_presets,
    read_custom_preset_index,
    read_custom_presets,
    read_preset_domains_page,
    read_system_preset_index,
    read_system_presets,
    save_custom_preset,
    save_system_preset,
)
from ..strategy_finder import (
    candidate_storage_version,
    domain_sets,
    read_candidate_domain_index,
    read_candidate_page,
    read_runs,
)
from ..zapret2 import check_install_cached


_EVENT_CURSOR_LOCK = threading.Lock()
_EVENT_CURSOR_STATE: dict[str, dict[str, Any]] = {}

DEFAULT_RUN_PREFERENCES = {
    "domains": [],
    "domain_preset": "system:required",
    "discovery_profile": "standard",
    "run_mode": "standard",
    "curl_parallelism": 4,
    "enable_http": False,
    "enable_tls12": True,
    "enable_tls13": False,
    "include_quic": True,
    "enable_ipv6": False,
    "scan_level": "standard",
    "repeats": 1,
    "repeat_parallel": False,
    "skip_dnscheck": True,
    "skip_ipblock": True,
    "limit_time_enabled": False,
    "timeout_hours": 6,
}


def get_payload(config: AppConfig, external_path: str, query: dict[str, list[str]]) -> dict[str, Any]:
    """Return an unchanged Web-view DTO from the Core-owned product state."""
    routes = {
        "/api/web/status": lambda: status_payload(config),
        "/api/web/run-preferences": lambda: {"run_preferences": read_run_preferences(config)},
        "/api/web/runs/history-page": lambda: core_api.runs_history_page_payload(config, query),
        "/api/web/candidate-domain-index-page": lambda: _candidate_domain_index_payload(config, query),
        "/api/web/strategy-candidates-page": lambda: _candidate_page_payload(config, query),
        "/api/web/presets": lambda: _web_presets_payload(config, query),
        "/api/web/presets/domains": lambda: _preset_domains_payload(config, query),
        "/api/web/events": lambda: events_snapshot_payload(config, query),
    }
    try:
        return routes[external_path]()
    except KeyError as error:
        raise KeyError(external_path) from error


def post_response(config: AppConfig, external_path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Apply an existing Web view command using only Core-owned storage."""
    if external_path == "/api/web/run-preferences":
        return {"run_preferences": save_run_preferences(config, payload.get("run_preferences") or payload)}
    if external_path == "/api/web/presets/save":
        scope = str(payload.get("scope") or "")
        name = str(payload.get("name") or "")
        kind = str(payload.get("kind") or "user")
        domains = _payload_string_list(payload, "domains")
        if kind == "system":
            save_system_preset(
                config.output.state_dir,
                scope=scope,
                name=name,
                domains=domains,
                updated_at=now_iso(),
            )
        else:
            save_custom_preset(
                config.output.state_dir,
                scope=scope,
                name=name,
                domains=domains,
                updated_at=now_iso(),
            )
        return _web_presets_payload(config, {"include_domains": ["1"]})
    if external_path == "/api/web/presets/delete-user-lists":
        names = _payload_string_list(payload, "names")
        if not names and payload.get("name"):
            names = [str(payload.get("name") or "")]
        metadata = delete_user_presets(
            config.output.state_dir,
            scope=str(payload.get("scope") or ""),
            names=names,
        )
        return _web_presets_payload(config, {"include_domains": ["1"]}) | {"metadata": metadata}
    raise KeyError(external_path)


def status_payload(config: AppConfig) -> dict[str, Any]:
    settings = read_settings(config)
    run_preferences = read_run_preferences(config)
    state = read_state(config.output.state_dir)
    if isinstance(state, dict):
        state = {**state, "settings": settings, "run_preferences": run_preferences}
    return {
        "version": __version__,
        "state": state,
        "settings": settings,
        "run_preferences": run_preferences,
        "candidate_version": candidate_storage_version(config.output.state_dir),
        "paths": {"state_dir": str(config.output.state_dir)},
        "zapret2": check_install_cached(),
    }


def event_payloads(config: AppConfig) -> dict[str, dict[str, Any]]:
    status = status_payload(config)
    status_event = {
        key: status[key]
        for key in ("version", "state", "settings", "run_preferences", "paths", "zapret2")
        if key in status
    }
    return {
        "status": status_event,
        "runs": _runs_event_payload(config.output.state_dir),
        "log": _log_event_payload(config.output.state_dir),
        "candidates": {"version": status.get("candidate_version") or {}},
        "settings": {"version": _event_fingerprint(status.get("settings") or {})},
        "presets": {
            "version": _event_fingerprint(
                {
                    "custom": read_custom_preset_index(config.output.state_dir),
                    "system": read_system_preset_index(config.output.state_dir),
                }
            )
        },
    }


def event_changes(config: AppConfig, previous_fingerprints: dict[str, str]) -> list[tuple[str, dict[str, Any]]]:
    changes: list[tuple[str, dict[str, Any]]] = []
    for event_name, payload in event_payloads(config).items():
        fingerprint = _event_fingerprint(payload)
        if previous_fingerprints.get(event_name) == fingerprint:
            continue
        previous_fingerprints[event_name] = fingerprint
        changes.append((event_name, payload))
    return changes


def events_snapshot_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    events = []
    created_at = now_iso()
    after_id = _query_one(query, "after_id")
    after_sequence = _event_sequence("web", after_id)
    limit = _bounded_int(_query_str(query, "limit", "100"), default=100, minimum=1, maximum=500)
    for event_type, payload in event_payloads(config).items():
        event_id = _event_cursor("web", event_type, payload)
        if _event_sequence("web", event_id) <= after_sequence:
            continue
        events.append({"event_id": event_id, "type": event_type, "created_at": created_at, "payload": payload})
        if len(events) >= limit:
            break
    return {"events": events, "next_after_id": str(events[-1]["event_id"]) if events else after_id}


def read_run_preferences(config: AppConfig) -> dict[str, Any]:
    state = read_state(config.output.state_dir)
    stored = state.get("run_preferences") if isinstance(state.get("run_preferences"), dict) else {}
    return _normalize_run_preferences({**DEFAULT_RUN_PREFERENCES, **stored})


def save_run_preferences(config: AppConfig, payload: dict[str, Any]) -> dict[str, Any]:
    preferences = _normalize_run_preferences(
        {**read_run_preferences(config), **(payload if isinstance(payload, dict) else {})}
    )
    update_state(config.output.state_dir, lambda state: state | {"run_preferences": preferences})
    return preferences


def _candidate_page_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    return read_candidate_page(
        config.output.state_dir,
        limit=_query_int(query, "limit", 50),
        offset=_query_int(query, "offset", 0),
        query=_query_str(query, "query", ""),
        view=_query_str(query, "view", "domain"),
        domains=_query_domains(query, "domains"),
        domain=_query_str(query, "domain", ""),
        fragmentation_classes=_query_domains(query, "fragmentation_class"),
    )


def _candidate_domain_index_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    return read_candidate_domain_index(
        config.output.state_dir,
        limit=_query_int(query, "limit", 50),
        offset=_query_int(query, "offset", 0),
        query=_query_str(query, "query", ""),
        fragmentation_classes=_query_domains(query, "fragmentation_class"),
    )


def _web_presets_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "metadata": read_custom_preset_index(config.output.state_dir),
        "system_metadata": read_system_preset_index(config.output.state_dir),
        "system": read_system_presets(config.output.state_dir),
    }
    if _query_bool(query, "include_domains", False):
        payload["custom"] = read_custom_presets(config.output.state_dir)
    else:
        payload["custom"] = {"finder": {}, "common": {}}
    return payload | {"domain_sets": domain_sets(), "builtin": builtin_preset_sources()}


def _preset_domains_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    return read_preset_domains_page(
        config.output.state_dir,
        scope=_query_str(query, "scope", ""),
        name=_query_str(query, "name", ""),
        kind=_query_str(query, "kind", "user"),
        query=_query_str(query, "query", ""),
        limit=_query_int(query, "limit", 200),
        offset=_query_int(query, "offset", 0),
        include_disabled=_query_bool(query, "include_disabled", True),
    )


def _runs_event_payload(state_dir: Path) -> dict[str, Any]:
    runs = read_runs(state_dir, limit=20)
    compact = [
        {
            "id": item.get("id"),
            "status": item.get("status"),
            "phase": item.get("phase"),
            "timestamp": item.get("timestamp"),
            "candidate_count": item.get("candidate_count"),
            "common_candidate_count": item.get("common_candidate_count"),
            "progress": item.get("progress"),
        }
        for item in runs
    ]
    return {"count": len(runs), "version": _event_fingerprint(compact)}


def _log_event_payload(state_dir: Path) -> dict[str, Any]:
    for run in reversed(read_runs(state_dir, limit=20)):
        stdout_log = Path(str(run.get("stdout_log") or ""))
        if not stdout_log.is_file():
            continue
        stderr_log_raw = str(run.get("stderr_log") or "")
        stderr_log = Path(stderr_log_raw) if stderr_log_raw else None
        return {
            "run_id": run.get("id"),
            "status": run.get("status"),
            "stdout": _path_version(stdout_log),
            "stderr": _path_version(stderr_log) if stderr_log else {"size": 0, "mtime_ns": 0},
            "progress": _path_version(_optional_path(run.get("progress_log"))),
            "metrics": _path_version(_optional_path(run.get("metrics_log"))),
        }
    return {"run_id": None, "status": None, "stdout": {"size": 0, "mtime_ns": 0}}


def _normalize_run_preferences(raw: dict[str, Any]) -> dict[str, Any]:
    run_mode = str(raw.get("run_mode") or "standard")
    if run_mode not in {"standard", "multi"}:
        run_mode = "standard"
    scan_level = str(raw.get("scan_level") or "standard")
    if scan_level not in {"quick", "standard", "force"}:
        scan_level = "standard"
    discovery_profile = str(raw.get("discovery_profile") or scan_level)
    if discovery_profile not in {"quick", "standard", "force", "custom"}:
        discovery_profile = scan_level if scan_level in {"quick", "standard", "force"} else "custom"
    try:
        timeout_hours = float(raw.get("timeout_hours"))
    except (TypeError, ValueError):
        timeout_hours = 6.0
    return {
        "domains": _clean_domain_list(raw.get("domains") or []),
        "domain_preset": str(raw.get("domain_preset") or "system:required")[:160],
        "discovery_profile": discovery_profile,
        "run_mode": run_mode,
        "curl_parallelism": _minimum_int(raw.get("curl_parallelism"), default=4, minimum=1),
        "enable_http": bool(raw.get("enable_http")),
        "enable_tls12": bool(raw.get("enable_tls12", True)),
        "enable_tls13": bool(raw.get("enable_tls13")),
        "include_quic": bool(raw.get("include_quic", True)),
        "enable_ipv6": bool(raw.get("enable_ipv6")),
        "scan_level": scan_level,
        "repeats": _bounded_int(raw.get("repeats"), default=1, minimum=1, maximum=10),
        "repeat_parallel": bool(raw.get("repeat_parallel")),
        "skip_dnscheck": bool(raw.get("skip_dnscheck", True)),
        "skip_ipblock": bool(raw.get("skip_ipblock", True)),
        "limit_time_enabled": bool(raw.get("limit_time_enabled")),
        "timeout_hours": max(0.1, min(24.0, timeout_hours)),
    }


def _event_cursor(stream: str, event_type: str, payload: dict[str, Any]) -> str:
    fingerprint = _event_fingerprint(payload)
    with _EVENT_CURSOR_LOCK:
        stream_state = _EVENT_CURSOR_STATE.setdefault(stream, {"next": 0, "events": {}})
        event_state = stream_state["events"].get(event_type)
        if event_state and event_state.get("fingerprint") == fingerprint:
            return str(event_state["event_id"])
        stream_state["next"] = int(stream_state.get("next") or 0) + 1
        event_id = f"{stream}:{stream_state['next']:012d}"
        stream_state["events"][event_type] = {"fingerprint": fingerprint, "event_id": event_id}
        return event_id


def _event_fingerprint(payload: Any) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:16]


def _event_sequence(stream: str, event_id: str) -> int:
    prefix = f"{stream}:"
    if not event_id.startswith(prefix):
        return 0
    try:
        return int(event_id[len(prefix) :])
    except ValueError:
        return 0


def _optional_path(value: Any) -> Path | None:
    text = str(value or "").strip()
    return Path(text) if text else None


def _path_version(path: Path | None) -> dict[str, int]:
    if path is None or not path.exists():
        return {"size": 0, "mtime_ns": 0}
    stat = path.stat()
    return {"size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}


def _clean_domain_list(value: Any) -> list[str]:
    items = value if isinstance(value, list) else str(value or "").replace(",", "\n").splitlines()
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        domain = str(item or "").strip().lower()
        if not domain or domain in seen:
            continue
        seen.add(domain)
        result.append(domain)
        if len(result) >= 5000:
            break
    return result


def _query_str(query: dict[str, list[str]], key: str, default: str) -> str:
    values = query.get(key) or []
    return values[0] if values else default


def _query_int(query: dict[str, list[str]], key: str, default: int) -> int:
    try:
        return int(_query_str(query, key, str(default)))
    except (TypeError, ValueError):
        return default


def _query_bool(query: dict[str, list[str]], key: str, default: bool) -> bool:
    raw = _query_str(query, key, "1" if default else "0").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _query_domains(query: dict[str, list[str]], key: str) -> list[str]:
    values = query.get(key) or []
    domains: list[str] = []
    for value in values:
        domains.extend(item.strip() for item in value.split(",") if item.strip())
    return domains


def _query_one(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key) or []
    return str(values[0]).strip() if values else ""


def _payload_string_list(payload: dict[str, Any], key: str) -> list[str]:
    raw = payload.get(key) or []
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _minimum_int(value: Any, default: int, minimum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, number)
