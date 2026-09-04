"""api_server SSE handler + event payload builders — moved from api_server.py."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from http import HTTPStatus
from pathlib import Path
from typing import Any

from gp_control_plane import __version__, core_api
from gp_control_plane.auth import AuthenticationError
from gp_control_plane.config import AppConfig
from gp_control_plane.engine_common import (
    candidate_storage_version,
    latest_log_tail,
    read_runs,
)
from gp_control_plane.settings import read_run_settings, read_settings
from gp_control_plane.state import now_iso, read_state
from gp_control_plane.storage import (
    is_storage_unavailable_error as _is_storage_unavailable_error,
)
from gp_control_plane.storage import (
    read_custom_preset_index,
    read_system_preset_index,
)
from gp_control_plane.web.api_server._helpers import (
    _bounded_int,
    _query_int,
    _query_one,
    _query_str,
)
from gp_control_plane.web.api_server._preferences import read_run_preferences
from gp_control_plane.zapret2 import check_install_cached

_EVENT_CURSOR_LOCK = threading.Lock()
_EVENT_CURSOR_STATE: dict[str, dict[str, Any]] = {}


class EventsMixin:
    def _events(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        authorization = self.headers.get("Authorization")
        previous: dict[str, str] = {}
        heartbeat_at = 0.0
        while True:
            try:
                self._require_stream_authorization(authorization)
                for event_name, payload in _event_payloads(self.config).items():
                    try:
                        fingerprint = _event_fingerprint(payload)
                        if previous.get(event_name) == fingerprint:
                            continue
                        previous[event_name] = fingerprint
                        self._require_stream_authorization(authorization)
                        self._event(event_name, payload)
                    except (TypeError, ValueError) as exc:
                        self._require_stream_authorization(authorization)
                        self._event(
                            "event-error",
                            {
                                "event": event_name,
                                "error": "serialization",
                                "message": str(exc),
                            },
                        )
                now = time.monotonic()
                if now - heartbeat_at >= 15:
                    self._require_stream_authorization(authorization)
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    heartbeat_at = now
                time.sleep(1)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                return
            except AuthenticationError:
                self.close_connection = True
                return
            except Exception as exc:  # noqa: BLE001
                if _is_storage_unavailable_error(exc):
                    self._event(
                        "event-error",
                        {
                            "error": "storage_unavailable",
                            "message": "Storage is temporarily unavailable.",
                        },
                    )
                    self.close_connection = True
                    return
                try:
                    self._require_stream_authorization(authorization)
                    self._event("event-error", {"error": "event-loop", "message": str(exc)})
                except (AuthenticationError, BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    self.close_connection = True
                    return
                except Exception:  # noqa: BLE001
                    self.close_connection = True
                    return
                time.sleep(1)

    def _event(self, event_name: str, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        self.wfile.write(f"event: {event_name}\n".encode())
        self.wfile.write(f"data: {data}\n\n".encode())
        self.wfile.flush()


def web_event_changes(config: AppConfig, previous_fingerprints: dict[str, str]) -> list[tuple[str, dict[str, Any]]]:
    changes: list[tuple[str, dict[str, Any]]] = []
    for event_name, payload in _web_event_payloads(config).items():
        fingerprint = _event_fingerprint(payload)
        if previous_fingerprints.get(event_name) == fingerprint:
            continue
        previous_fingerprints[event_name] = fingerprint
        changes.append((event_name, payload))
    return changes


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
        "paths": {
            "state_dir": str(config.output.state_dir),
        },
        "zapret2": check_install_cached(),
    }


def _event_payloads(config: AppConfig) -> dict[str, dict[str, Any]]:
    return _web_event_payloads(config)


def _web_event_payloads(config: AppConfig) -> dict[str, dict[str, Any]]:
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


def _core_event_payloads(config: AppConfig) -> dict[str, dict[str, Any]]:
    state_dir = config.output.state_dir
    status_event = dict(core_api.status_payload(config))
    status_event.pop("updated_at", None)
    run_settings_event = {"version": _event_fingerprint(read_run_settings(config))}
    domain_lists_event = {
        "version": _event_fingerprint(
            {
                "custom": read_custom_preset_index(state_dir),
                "system": read_system_preset_index(state_dir),
            }
        )
    }
    candidates_event = {"version": candidate_storage_version(state_dir)}
    return {
        "core.status": status_event,
        "strategy-discovery.progress": core_api.current_progress_payload(config),
        "strategy-discovery.log": _log_event_payload(state_dir),
        "strategy-candidates": candidates_event,
        "run-settings": run_settings_event,
        "domain-lists": domain_lists_event,
    }


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


def _optional_path(value: Any) -> Path | None:
    text = str(value or "").strip()
    return Path(text) if text else None


def _path_version(path: Path | None) -> dict[str, int]:
    if path is None or not path.exists():
        return {"size": 0, "mtime_ns": 0}
    stat = path.stat()
    return {"size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}


def _event_fingerprint(payload: Any) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:16]


def _latest_log_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    return latest_log_tail(
        config.output.state_dir,
        run_id=_query_one(query, "run_id"),
        stdout_from_size=_query_int(query, "stdout_size", -1),
        stdout_log_match=_query_one(query, "stdout_log"),
        stderr_from_size=_query_int(query, "stderr_size", -1),
        stderr_log_match=_query_one(query, "stderr_log"),
    )


def _current_run_latest_log_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    state = read_state(config.output.state_dir)
    return latest_log_tail(
        config.output.state_dir,
        run_id=str(state.get("current_run_id") or ""),
        stdout_from_size=_query_int(query, "stdout_size", -1),
        stdout_log_match=_query_one(query, "stdout_log"),
        stderr_from_size=_query_int(query, "stderr_size", -1),
        stderr_log_match=_query_one(query, "stderr_log"),
    )


def _events_response_payload(config: AppConfig, query: dict[str, list[str]], *, stream: str) -> dict[str, Any]:
    payloads = _core_event_payloads(config) if stream == "core" else _web_event_payloads(config)
    events = []
    created_at = now_iso()
    after_id = _query_one(query, "after_id")
    after_sequence = _event_sequence(stream, after_id)
    limit = _bounded_int(_query_str(query, "limit", "100"), default=100, minimum=1, maximum=500)
    for event_type, payload in payloads.items():
        event_id = _event_cursor(stream, event_type, payload)
        if _event_sequence(stream, event_id) <= after_sequence:
            continue
        events.append({"event_id": event_id, "type": event_type, "created_at": created_at, "payload": payload})
        if len(events) >= limit:
            break
    return {"events": events, "next_after_id": str(events[-1]["event_id"]) if events else after_id}


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


def _event_sequence(stream: str, event_id: str) -> int:
    prefix = f"{stream}:"
    if not event_id.startswith(prefix):
        return 0
    raw_sequence = event_id[len(prefix) :]
    try:
        return int(raw_sequence)
    except ValueError:
        return 0
