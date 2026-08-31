from __future__ import annotations

import hashlib
import json
import mimetypes
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .. import __version__, core_api, service_api
from ..auth import AuthenticationError, PasswordValidationError, change_password, health_payload, login, require_bearer_token
from ..backups import (
    create_post_run_snapshot,
    create_snapshot_if_idle,
    delete_snapshot_if_idle,
    import_snapshot_archive,
    restore_snapshot_if_idle,
    snapshot_file_path,
)
from ..config import AppConfig
from ..domain_sources import (
    builtin_preset_sources,
    fetch_v2fly_category_local,
    fetch_v2fly_revision,
    import_v2fly_preset,
    list_v2fly_categories_local,
    parse_v2fly_domains,
    parse_v2fly_revision,
    prepare_v2fly_local_storage,
    preview_v2fly_preset,
    read_v2fly_catalog_cache,
    read_v2fly_group_manifest,
    write_v2fly_catalog_cache,
)
from ..blockchecks_backend import export_nfconf, run_blockchecks_discovery, stop_blockchecks
from ..discovery_engine import campaign_lock_busy_message, is_blockchecks_job, normalize_engine
from ..jobs import JobRunner
from ..releases import release_channel_info
from ..resource_budget import (
    BACKUP_STREAM_CHUNK_BYTES,
    BACKUP_UPLOAD_MAX_BYTES,
    JSON_REQUEST_MAX_BYTES,
)
from ..settings import (
    DEFAULT_SETTINGS,
    read_run_settings,
    read_service_settings,
    read_settings,
    save_run_settings,
    save_settings,
)
from ..state import active_job_lock_payload, has_active_runtime, now_iso, read_state, update_state
from ..storage import (
    delete_custom_preset,
    delete_user_presets,
    read_custom_preset_index,
    read_custom_presets,
    read_preset_domains_page,
    read_system_preset_index,
    read_system_presets,
    save_custom_preset,
    save_custom_presets,
    save_system_preset,
    set_preset_domain_enabled,
    is_storage_unavailable_error as _is_storage_unavailable_error,
)
from ..strategy_finder import (
    candidate_storage_version,
    close_stale_running_runs,
    domain_sets,
    latest_log_tail,
    read_candidate_domain_index,
    read_candidate_page,
    read_runs,
    run_multi_domain_discovery,
    run_standard_discovery,
)
from ..zapret2 import (
    check_install_cached,
    cleanup_nft_blockcheck_tables,
    recover_quarantined_process_run,
    recover_registered_process_runs,
)
from .errors import error_payload, normalize_error_payload
from .docs import (
    OPENAPI_JSON_CONTENT_TYPE,
    SWAGGER_HTML_CONTENT_TYPE,
    SWAGGER_PATHS,
    openapi_json_bytes,
    swagger_ui_html,
)
from .routes import JSON_GET_ROUTE_PATHS, JSON_HEAD_ROUTE_PATHS, JSON_POST_ROUTE_PATHS, route_for


MAX_BACKUP_UPLOAD_BYTES = BACKUP_UPLOAD_MAX_BYTES
MAX_JSON_REQUEST_BYTES = JSON_REQUEST_MAX_BYTES
NDJSON_CONTENT_TYPE = "application/x-ndjson; charset=utf-8"

_core_strategy_discovery_job_payload = core_api.strategy_discovery_job_payload
_EVENT_CURSOR_LOCK = threading.Lock()
_EVENT_CURSOR_STATE: dict[str, dict[str, Any]] = {}
_ROOT_MANAGED_DISCOVERY_NAMES = frozenset(
    {"zapret-standard-discovery", "zapret-multi-domain-discovery"}
)


class RequestBodyTooLarge(ValueError):
    pass


class RuntimeBusyError(RuntimeError):
    pass


def index_html() -> str:
    from .ui import index_html as _index_html

    return _index_html()


def _clean_install_vault_public_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep the HTTP contract limited to non-secret vault metadata."""
    return {
        "vault_id": str(payload.get("vault_id") or ""),
        "created_at": str(payload.get("created_at") or ""),
        "schema_version": str(payload.get("schema_version") or ""),
        "archive_sha256": str(payload.get("archive_sha256") or ""),
        "archive_size_bytes": int(payload.get("archive_size_bytes") or 0),
        "verification": str(payload.get("verification") or ""),
        "pending": bool(payload.get("pending")),
    }


def _clean_install_vault_create_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Return only public creation metadata from the protected vault handoff."""
    return {
        "vault_id": str(payload.get("vault_id") or ""),
        "archive_sha256": str(payload.get("archive_sha256") or ""),
        "archive_size_bytes": int(payload.get("archive_size_bytes") or 0),
        "schema_version": str(payload.get("schema_version") or ""),
        "semantic_manifest": payload.get("semantic_manifest") or {},
    }


def _clean_install_vault_restore_response(payload: dict[str, Any], vault_id: str) -> dict[str, Any]:
    """Expose only completion flags from the local restore."""
    verification = payload.get("verification") if isinstance(payload.get("verification"), dict) else {}
    cleanup = payload.get("cleanup") if isinstance(payload.get("cleanup"), dict) else {}
    readiness = payload.get("storage_status") if isinstance(payload.get("storage_status"), dict) else {}
    return {
        "completed": bool(payload.get("completed")),
        "vault_id": str(payload.get("vault_id") or vault_id),
        "verification": {"verified": bool(verification.get("verified"))},
        "storage_status": {"ready": bool(readiness.get("ready"))},
        "cleanup": {"source_deleted": bool(cleanup.get("source_deleted"))},
    }


def serve(config: AppConfig, host: str, port: int, *, ui_enabled: bool = True) -> None:
    _recover_runtime_before_serve(config)
    close_stale_running_runs(config.output.state_dir)
    runner = JobRunner(config.output.state_dir, on_idle=lambda: create_post_run_snapshot(config.output.state_dir))
    runtime_role = "monolith" if ui_enabled else "core"
    web_install_enabled = True if ui_enabled else None

    class Handler(BaseHTTPRequestHandler):
        def handle_one_request(self) -> None:
            try:
                super().handle_one_request()
            except (ConnectionAbortedError, ConnectionResetError):
                return
            except Exception as exc:  # noqa: BLE001
                if _is_storage_unavailable_error(exc):
                    self._storage_unavailable()
                    return
                raise

        def do_GET(self) -> None:  # noqa: N802
            parsed_url = urlparse(self.path)
            path = parsed_url.path
            if not self._authorize_api(path):
                return
            query = parse_qs(parsed_url.query)
            if path == "/":
                if ui_enabled:
                    self._html()
                else:
                    self._json({"error": "web ui is disabled in core mode"}, status=HTTPStatus.NOT_FOUND)
            elif path == "/openapi.json":
                self._openapi_json()
            elif path in SWAGGER_PATHS:
                self._swagger()
            elif path == "/api/core/strategy-candidates/export":
                self._stream_strategy_candidates_export(config, query)
            elif path in JSON_GET_ROUTE_PATHS:
                self._dispatch_json_get(path, query)
            elif path == "/api/core/backups/download-archive":
                core_query = {"snapshot": [_query_one(query, "snapshot_id")], "file": ["archive"]}
                self._download_backup(config, core_query)
            elif path == "/api/web/events/stream":
                if not ui_enabled:
                    self._not_found()
                    return
                self._events()
            else:
                self._not_found()

        def _json_get_routes(self, query: dict[str, list[str]]) -> dict[str, Any]:
            return {
                "/api/health": health_payload,
                "/api/core/status": lambda: core_api.status_payload(config),
                "/api/core/strategy-discovery/current-run-progress": lambda: core_api.current_progress_payload(config),
                "/api/core/strategy-discovery/current-run-latest-log": lambda: _current_run_latest_log_payload(config, query),
                "/api/core/strategy-discovery/preflight": lambda: core_api.preflight_payload(config),
                "/api/core/presets/domain-lists": lambda: core_api.domain_lists_payload(config),
                "/api/core/presets/v2fly/categories": lambda: core_api.v2fly_categories_payload(config, query),
                "/api/core/presets/v2fly/category-domains": lambda: core_api.v2fly_category_domains_payload(config, query),
                "/api/core/backups/list": lambda: core_api.backups_list_payload(config),
                "/api/core/clean-install-vaults/list": lambda: {
                    "vaults": [
                        _clean_install_vault_public_metadata(item)
                        for item in (core_api.clean_install_vault_list_payload(config).get("vaults") or [])
                        if isinstance(item, dict)
                    ]
                },
                "/api/core/clean-install-vaults/status": lambda: _clean_install_vault_public_metadata(
                    core_api.clean_install_vault_status_payload(config, query)
                ),
                "/api/core/run-settings": lambda: core_api.run_settings_payload(read_run_settings(config)),
                "/api/core/runs/history": lambda: core_api.runs_history_payload(config, query),
                "/api/core/runs/latest-log": lambda: _latest_log_payload(config, query),
                "/api/core/strategy-candidates": lambda: core_api.strategy_candidates_payload(config, query),
                "/api/core/events": lambda: _events_response_payload(config, query, stream="core"),
                "/api/service/status": lambda: service_api.service_status_payload(
                    config,
                    current_version=__version__,
                    runtime_role=runtime_role,
                    web_enabled=web_install_enabled,
                ),
                "/api/service/releases/available": lambda: service_api.available_releases_payload(
                    read_service_settings(config), current_version=__version__
                ),
                "/api/service/v2fly/local-storage-status": lambda: service_api.v2fly_storage_status_payload(config),
            }

        def _dispatch_json_get(self, path: str, query: dict[str, list[str]]) -> None:
            if path.startswith("/api/web/"):
                if not ui_enabled:
                    self._not_found()
                    return
                try:
                    self._json(web_json_get_payload(config, path, query))
                except Exception as exc:  # noqa: BLE001
                    if _is_storage_unavailable_error(exc):
                        self._storage_unavailable()
                        return
                    self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                self._json(self._json_get_routes(query)[path]())
            except Exception as exc:  # noqa: BLE001
                if _is_storage_unavailable_error(exc):
                    self._storage_unavailable()
                    return
                if path == "/api/core/clean-install-vaults/status" and isinstance(exc, FileNotFoundError):
                    self._json(error_payload("not_found", "Clean-install vault was not found."), status=HTTPStatus.NOT_FOUND)
                    return
                if path in {"/api/core/clean-install-vaults/list", "/api/core/clean-install-vaults/status"}:
                    self._json(error_payload("invalid_request", str(exc)), status=HTTPStatus.BAD_REQUEST)
                    return
                if path in {"/api/core/presets/v2fly/category-domains", "/api/core/strategy-candidates"}:
                    self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                    return
                raise

        def do_HEAD(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if not self._authorize_api(path):
                return
            if path == "/":
                if ui_enabled:
                    data = index_html().encode("utf-8")
                    self._head(HTTPStatus.OK, "text/html; charset=utf-8", len(data))
                else:
                    self._head(HTTPStatus.NOT_FOUND, "application/json; charset=utf-8", 0)
            elif path == "/openapi.json":
                self._head_openapi_json()
            elif path in SWAGGER_PATHS:
                data = swagger_ui_html().encode("utf-8")
                self._head(HTTPStatus.OK, SWAGGER_HTML_CONTENT_TYPE, len(data))
            elif path == "/api/core/strategy-candidates/export":
                self._head(HTTPStatus.OK, NDJSON_CONTENT_TYPE, 0)
            elif path == "/api/web/events/stream" and ui_enabled:
                self._head(HTTPStatus.OK, "text/event-stream; charset=utf-8", 0)
            elif path.startswith("/api/web/") and not ui_enabled:
                self._head(HTTPStatus.NOT_FOUND, "application/json; charset=utf-8", 0)
            elif path in JSON_HEAD_ROUTE_PATHS:
                self._head(HTTPStatus.OK, "application/json; charset=utf-8", 0)
            else:
                self._head(HTTPStatus.NOT_FOUND, "application/json; charset=utf-8", 0)

        def do_POST(self) -> None:  # noqa: N802
            parsed_url = urlparse(self.path)
            path = parsed_url.path
            if not self._authorize_api(path):
                return
            if path == "/api/core/backups/upload":
                try:
                    if has_active_runtime(config.output.state_dir):
                        raise RuntimeBusyError()
                    imported = import_snapshot_archive(config.output.state_dir, self._request_upload_bytes())
                    self._json(core_api.backup_snapshot_payload(imported.get("snapshot") or {}), status=HTTPStatus.CREATED)
                except Exception as exc:  # noqa: BLE001
                    if _is_storage_unavailable_error(exc):
                        self._storage_unavailable()
                        return
                    if isinstance(exc, RuntimeBusyError):
                        self._json({"error": "runtime_busy"}, status=HTTPStatus.CONFLICT)
                    elif isinstance(exc, RequestBodyTooLarge):
                        self._json({"error": str(exc)}, status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                    else:
                        self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            if path not in JSON_POST_ROUTE_PATHS:
                self._not_found()
                return
            try:
                payload = self._request_json()
            except RequestBodyTooLarge as exc:
                self._json({"error": str(exc)}, status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            post_routes = self._json_post_routes(payload)
            if path.startswith("/api/web/") and not ui_enabled:
                self._not_found()
                return
            self._dispatch_json_post(post_routes[path])

        def _json_post_routes(self, payload: dict[str, Any]) -> dict[str, Any]:
            def stop_current_run() -> tuple[dict[str, Any], HTTPStatus]:
                if payload.get("dry_run"):
                    state = read_state(config.output.state_dir)
                    return (
                        {
                            "accepted": True,
                            "status": "dry_run",
                            "run_id": str(state.get("current_run_id") or ""),
                        },
                        HTTPStatus.ACCEPTED,
                    )
                job = runner.cancel_active()
                return core_api.action_accepted_payload(job), HTTPStatus.ACCEPTED

            def start_strategy_discovery() -> tuple[dict[str, Any], HTTPStatus]:
                incoming = dict(payload)
                nested = incoming.get("settings") if isinstance(incoming.get("settings"), dict) else {}
                if "discovery_engine" not in nested:
                    incoming["settings"] = {
                        **nested,
                        "discovery_engine": read_run_settings(config).get("discovery_engine"),
                    }
                name, core_payload = core_api.strategy_discovery_job_payload(incoming)
                if is_blockchecks_job(name) and campaign_lock_busy_message():
                    raise RuntimeBusyError()
                func = lambda stop, run_id: _job_discovery(config, name, core_payload, stop, run_id)
                cancel_hook = stop_blockchecks if is_blockchecks_job(name) else cleanup_nft_blockcheck_tables
                job = runner.start(
                    name,
                    func,
                    cancel_hook=cancel_hook,
                )
                return core_api.run_accepted_payload(job), HTTPStatus.ACCEPTED

            def export_blockchecks_nfconf() -> tuple[dict[str, Any], HTTPStatus]:
                raw_dir = payload.get("out_dir")
                out_dir = Path(str(raw_dir)) if raw_dir else None
                limit = int(payload.get("limit") or 5)
                return export_nfconf(out_dir=out_dir, limit=limit), HTTPStatus.OK

            def create_core_backup() -> tuple[dict[str, Any], HTTPStatus]:
                created = create_snapshot_if_idle(config.output.state_dir)
                if created.get("queued"):
                    raise RuntimeBusyError()
                return core_api.backup_snapshot_payload(created.get("snapshot") or {}), HTTPStatus.CREATED

            def restore_core_backup() -> tuple[dict[str, Any], HTTPStatus]:
                snapshot_id = core_api.payload_snapshot_id(payload)
                restored = restore_snapshot_if_idle(config.output.state_dir, snapshot_id)
                if restored.get("queued"):
                    raise RuntimeBusyError()
                return {"accepted": True, "status": "success", "snapshot_id": snapshot_id}, HTTPStatus.ACCEPTED

            def delete_core_backup() -> tuple[dict[str, Any], HTTPStatus]:
                snapshot_id = core_api.payload_snapshot_id(payload)
                deleted = delete_snapshot_if_idle(config.output.state_dir, snapshot_id)
                if deleted.get("queued"):
                    raise RuntimeBusyError()
                return {"deleted": 1}, HTTPStatus.OK

            def create_clean_install_vault() -> tuple[dict[str, Any], HTTPStatus]:
                if payload:
                    raise ValueError("clean-install vault create does not accept request fields")
                created = core_api.clean_install_vault_create_payload(config, payload)
                return _clean_install_vault_create_response(created), HTTPStatus.CREATED

            def restore_clean_install_vault() -> tuple[dict[str, Any], HTTPStatus]:
                allowed = {"vault_id", "confirm_restore"}
                unknown = sorted(str(key) for key in payload if str(key) not in allowed)
                if unknown:
                    raise ValueError(f"unsupported clean-install vault restore fields: {', '.join(unknown)}")
                # Preserve the raw JSON value for strict core validation:
                # whitespace or a non-string vault_id must not be normalized
                # into an otherwise acceptable identifier at this boundary.
                restored = core_api.clean_install_vault_restore_payload(config, payload)
                public_response = _clean_install_vault_restore_response(restored, "")
                if not (
                    public_response["completed"]
                    and public_response["verification"]["verified"]
                    and public_response["storage_status"]["ready"]
                    and public_response["cleanup"]["source_deleted"]
                ):
                    raise RuntimeError("clean-install vault restore did not complete; source retained")
                return public_response, HTTPStatus.OK

            def ensure_service_action_idle() -> None:
                if active_job_lock_payload(config.output.state_dir, cleanup_stale=True):
                    raise RuntimeError("service action is blocked while another job is running")

            def v2fly_check_updates() -> tuple[dict[str, Any], HTTPStatus]:
                ensure_service_action_idle()
                return service_api.v2fly_check_updates_payload(config), HTTPStatus.OK

            def v2fly_update_local_storage() -> tuple[dict[str, Any], HTTPStatus]:
                if not payload.get("dry_run"):
                    ensure_service_action_idle()
                return service_api.v2fly_update_local_storage_payload(config, payload), HTTPStatus.OK

            return {
                "/api/auth/login": (
                    lambda: (login(config.output.state_dir, payload), HTTPStatus.OK),
                    HTTPStatus.UNAUTHORIZED,
                    HTTPStatus.BAD_REQUEST,
                ),
                "/api/auth/change-password": (
                    lambda: (
                        change_password(config.output.state_dir, payload, self.headers.get("Authorization")),
                        HTTPStatus.OK,
                    ),
                    HTTPStatus.UNAUTHORIZED,
                    HTTPStatus.BAD_REQUEST,
                ),
                "/api/core/strategy-discovery/stop-current-run": (stop_current_run, HTTPStatus.CONFLICT, HTTPStatus.CONFLICT),
                "/api/core/strategy-discovery/start-run": (start_strategy_discovery, HTTPStatus.CONFLICT, HTTPStatus.BAD_REQUEST),
                "/api/core/strategy-discovery/export-nfconf": (
                    export_blockchecks_nfconf,
                    HTTPStatus.CONFLICT,
                    HTTPStatus.BAD_REQUEST,
                ),
                "/api/core/presets/save-domain-list": (
                    lambda: (core_api.save_domain_list_payload(config, payload), HTTPStatus.OK),
                    HTTPStatus.BAD_REQUEST,
                    HTTPStatus.BAD_REQUEST,
                ),
                "/api/core/presets/delete-user-domain-list": (
                    lambda: (core_api.delete_user_domain_list_payload(config, payload), HTTPStatus.OK),
                    HTTPStatus.BAD_REQUEST,
                    HTTPStatus.BAD_REQUEST,
                ),
                "/api/core/backups/create": (create_core_backup, HTTPStatus.CONFLICT, HTTPStatus.CONFLICT),
                "/api/core/backups/restore": (restore_core_backup, HTTPStatus.CONFLICT, HTTPStatus.CONFLICT),
                "/api/core/backups/delete": (delete_core_backup, HTTPStatus.CONFLICT, HTTPStatus.CONFLICT),
                "/api/core/clean-install-vaults/create": (
                    create_clean_install_vault,
                    HTTPStatus.CONFLICT,
                    HTTPStatus.BAD_REQUEST,
                ),
                "/api/core/clean-install-vaults/restore": (
                    restore_clean_install_vault,
                    HTTPStatus.CONFLICT,
                    HTTPStatus.BAD_REQUEST,
                ),
                "/api/core/run-settings/save": (
                    lambda: (core_api.run_settings_payload(save_run_settings(config, payload.get("settings") or payload)), HTTPStatus.OK),
                    HTTPStatus.BAD_REQUEST,
                    HTTPStatus.BAD_REQUEST,
                ),
                "/api/service/v2fly/check-updates": (
                    v2fly_check_updates,
                    HTTPStatus.CONFLICT,
                    HTTPStatus.BAD_REQUEST,
                ),
                "/api/service/v2fly/update-local-storage": (
                    v2fly_update_local_storage,
                    HTTPStatus.CONFLICT,
                    HTTPStatus.BAD_REQUEST,
                ),
                "/api/web/run-preferences": (
                    lambda: web_json_post_response(config, "/api/web/run-preferences", payload),
                    HTTPStatus.BAD_REQUEST,
                    HTTPStatus.BAD_REQUEST,
                ),
                "/api/web/presets/save": (
                    lambda: web_json_post_response(config, "/api/web/presets/save", payload),
                    HTTPStatus.BAD_REQUEST,
                    HTTPStatus.BAD_REQUEST,
                ),
                "/api/web/presets/delete-user-lists": (
                    lambda: web_json_post_response(config, "/api/web/presets/delete-user-lists", payload),
                    HTTPStatus.BAD_REQUEST,
                    HTTPStatus.BAD_REQUEST,
                ),
            }

        def _dispatch_json_post(self, route: Any) -> None:
            handler, error_status, value_error_status = route
            try:
                payload, status = handler()
            except Exception as exc:  # noqa: BLE001
                if _is_storage_unavailable_error(exc):
                    self._storage_unavailable()
                    return
                if isinstance(exc, AuthenticationError):
                    self._auth_error(exc)
                    return
                if isinstance(exc, PasswordValidationError):
                    self._json(error_payload("invalid_request", str(exc)), status=HTTPStatus.BAD_REQUEST)
                    return
                if isinstance(exc, RuntimeBusyError):
                    self._json({"error": "runtime_busy"}, status=HTTPStatus.CONFLICT)
                    return
                if isinstance(exc, ValueError):
                    self._json({"error": str(exc)}, status=value_error_status)
                    return
                self._json({"error": str(exc)}, status=error_status)
                return
            self._json(payload, status=status)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _html(self) -> None:
            data = index_html().encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _swagger(self) -> None:
            self._bytes(swagger_ui_html().encode("utf-8"), SWAGGER_HTML_CONTENT_TYPE, cache_control="no-store")

        def _openapi_json(self) -> None:
            try:
                data = openapi_json_bytes(core_only=not ui_enabled)
            except OSError:
                self._json({"error": "openapi contract is not available"}, status=HTTPStatus.NOT_FOUND)
                return
            self._bytes(data, OPENAPI_JSON_CONTENT_TYPE, cache_control="no-store")

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
                    for event_name, payload in _event_payloads(config).items():
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
            self.wfile.write(f"event: {event_name}\n".encode("utf-8"))
            self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
            self.wfile.flush()

        @staticmethod
        def _require_stream_authorization(authorization: str | None) -> None:
            require_bearer_token(config.output.state_dir, authorization)

        def _json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            response = normalize_error_payload(payload, status)
            data = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorize_api(self, path: str) -> bool:
            if not path.startswith("/api/"):
                return True
            route = route_for(self.command, path)
            if route and not route.auth_required:
                return True
            try:
                require_bearer_token(config.output.state_dir, self.headers.get("Authorization"))
            except Exception as exc:  # noqa: BLE001
                if _is_storage_unavailable_error(exc):
                    self._storage_unavailable()
                    return False
                if isinstance(exc, AuthenticationError):
                    self._auth_error(exc)
                    return False
                raise
            return True

        def _storage_unavailable(self) -> None:
            self._json(error_payload("storage_unavailable", "Storage is temporarily unavailable."), HTTPStatus.SERVICE_UNAVAILABLE)

        def _auth_error(self, error: AuthenticationError) -> None:
            del error
            data = json.dumps(
                error_payload("authentication_required", "A Bearer token is required."),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("WWW-Authenticate", "Bearer")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()

        def _request_upload_bytes(self) -> bytes:
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError as exc:
                raise ValueError("invalid upload size") from exc
            if length <= 0:
                raise ValueError("empty backup upload")
            if length > MAX_BACKUP_UPLOAD_BYTES:
                raise RequestBodyTooLarge("backup upload is too large")
            content_type = self.headers.get("Content-Type", "")
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("incomplete backup upload")
            if content_type.startswith("application/zip") or content_type.startswith("application/octet-stream"):
                return body
            if content_type.startswith("multipart/form-data"):
                marker = "boundary="
                if marker not in content_type:
                    raise ValueError("multipart boundary is missing")
                boundary = content_type.split(marker, 1)[1].strip().strip('"')
                return _multipart_file_bytes(body, boundary)
            raise ValueError("expected zip upload")

        def _download_backup(self, config: AppConfig, query: dict[str, list[str]]) -> None:
            snapshot_id = _query_one(query, "snapshot")
            file_name = _query_one(query, "file") or "archive"
            try:
                path = snapshot_file_path(config.output.state_dir, snapshot_id, file_name)
            except Exception as exc:  # noqa: BLE001
                if _is_storage_unavailable_error(exc):
                    self._storage_unavailable()
                    return
                self._not_found()
                return
            self._file(path, download_name=path.name, content_type="application/zip")

        def _file(self, path: Path, download_name: str, *, content_type: str | None = None) -> None:
            content_type = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(path.stat().st_size))
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
            self.end_headers()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(BACKUP_STREAM_CHUNK_BYTES), b""):
                    self.wfile.write(chunk)

        def _stream_strategy_candidates_export(self, config: AppConfig, query: dict[str, list[str]]) -> None:
            try:
                iterator = core_api.iter_strategy_candidates_export_lines(config, query)
                first_line = next(iterator)
            except ValueError as exc:
                self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            except StopIteration:
                first_line = None
            except Exception as exc:  # noqa: BLE001
                if _is_storage_unavailable_error(exc):
                    self._storage_unavailable()
                    return
                raise
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", NDJSON_CONTENT_TYPE)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                if first_line is not None:
                    self.wfile.write(first_line)
                    self.wfile.flush()
                for line in iterator:
                    self.wfile.write(line)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                return
            except Exception as exc:  # noqa: BLE001
                if _is_storage_unavailable_error(exc):
                    self.close_connection = True
                    return
                raise

        def _bytes(self, data: bytes, content_type: str, *, cache_control: str | None = None) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            if cache_control:
                self.send_header("Cache-Control", cache_control)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _head(self, status: HTTPStatus, content_type: str, content_length: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            if content_type.startswith("text/html"):
                self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(content_length))
            self.end_headers()

        def _head_openapi_json(self) -> None:
            try:
                data = openapi_json_bytes(core_only=not ui_enabled)
            except OSError:
                self._head(HTTPStatus.NOT_FOUND, "application/json; charset=utf-8", 0)
                return
            self._head(HTTPStatus.OK, OPENAPI_JSON_CONTENT_TYPE, len(data))

        def _not_found(self) -> None:
            self._json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

        def _request_json(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError as exc:
                raise ValueError("invalid request body size") from exc
            if length <= 0:
                return {}
            if length > MAX_JSON_REQUEST_BYTES:
                raise RequestBodyTooLarge("request body is too large")
            raw = self.rfile.read(length).decode("utf-8")
            if not raw.strip():
                return {}
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("request body must be a JSON object")
            return parsed

    server = ThreadingHTTPServer((host, port), Handler)
    mode = "web UI" if ui_enabled else "core API"
    print(f"GP control plane {mode} listening on http://{host}:{port}")
    server.serve_forever()


def serve_core(config: AppConfig, host: str = "127.0.0.1", port: int = 8081) -> None:
    from .core_server import serve_core as _serve_core

    _serve_core(config, host=host, port=port)


def serve_web_proxy(config: AppConfig, host: str, port: int, *, core_url: str) -> None:
    from .proxy import serve_web_proxy as _serve_web_proxy

    _serve_web_proxy(config, host=host, port=port, core_url=core_url)


def web_json_get_payload(config: AppConfig, path: str, query: dict[str, list[str]]) -> dict[str, Any]:
    routes = {
        "/api/web/run-preferences": lambda: {"run_preferences": read_run_preferences(config)},
        "/api/web/runs/history-page": lambda: _runs_page_payload(config, query),
        "/api/web/candidate-domain-index-page": lambda: _candidate_domain_index_payload(config, query),
        "/api/web/strategy-candidates-page": lambda: _candidate_page_payload(config, query),
        "/api/web/presets": lambda: _web_presets_payload(config, query),
        "/api/web/presets/domains": lambda: _preset_domains_payload(config, query),
        "/api/web/events": lambda: _events_response_payload(config, query, stream="web"),
    }
    if path not in routes:
        raise KeyError(path)
    return routes[path]()


def web_json_post_response(config: AppConfig, path: str, payload: dict[str, Any]) -> tuple[dict[str, Any], HTTPStatus]:
    if path == "/api/web/run-preferences":
        return {"run_preferences": save_run_preferences(config, payload.get("run_preferences") or payload)}, HTTPStatus.OK
    if path == "/api/web/presets/save":
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
        return _web_presets_payload(config, {"include_domains": ["1"]}), HTTPStatus.OK
    if path == "/api/web/presets/delete-user-lists":
        names = _payload_string_list(payload, "names")
        if not names and payload.get("name"):
            names = [str(payload.get("name") or "")]
        metadata = delete_user_presets(
            config.output.state_dir,
            scope=str(payload.get("scope") or ""),
            names=names,
        )
        return _web_presets_payload(config, {"include_domains": ["1"]}) | {"metadata": metadata}, HTTPStatus.OK
    raise KeyError(path)


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


DEFAULT_DISCOVERY_PROFILES = {
    "quick": {
        "name": "quick",
        "title": "Быстрый",
        "enable_http": False,
        "enable_tls12": True,
        "enable_tls13": False,
        "include_quic": True,
        "enable_ipv6": False,
        "scan_level": "quick",
        "repeats": 1,
        "repeat_parallel": False,
        "skip_dnscheck": True,
        "skip_ipblock": True,
        "curl_parallelism": 4,
        "limit_time_enabled": False,
        "timeout_hours": 6,
    },
    "standard": {
        "name": "standard",
        "title": "Стандартный",
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
        "curl_parallelism": 4,
        "limit_time_enabled": False,
        "timeout_hours": 6,
    },
    "force": {
        "name": "force",
        "title": "Глубокий",
        "enable_http": True,
        "enable_tls12": True,
        "enable_tls13": True,
        "include_quic": True,
        "enable_ipv6": False,
        "scan_level": "force",
        "repeats": 1,
        "repeat_parallel": False,
        "skip_dnscheck": False,
        "skip_ipblock": False,
        "curl_parallelism": 4,
        "limit_time_enabled": False,
        "timeout_hours": 6,
    },
}


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
    timeout_hours_raw = raw.get("timeout_hours")
    try:
        timeout_hours = float(timeout_hours_raw)
    except (TypeError, ValueError):
        timeout_hours = 6.0
    timeout_hours = max(0.1, min(24.0, timeout_hours))
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
        "timeout_hours": timeout_hours,
    }


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


def read_discovery_profiles(config: AppConfig) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for name, profile in DEFAULT_DISCOVERY_PROFILES.items():
        merged[name] = _normalize_discovery_profile(name, profile)
    return dict(sorted(merged.items()))


def save_discovery_profiles(config: AppConfig, payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    update_state(config.output.state_dir, lambda state: state | {"discovery_profiles": {}})
    return read_discovery_profiles(config)


def _normalize_discovery_profile(name: str, raw: dict[str, Any]) -> dict[str, Any]:
    scan_level = str(raw.get("scan_level") or "standard")
    if scan_level not in {"quick", "standard", "force"}:
        scan_level = "standard"
    return {
        "name": name,
        "title": str(raw.get("title") or name),
        "enable_http": _payload_bool(raw, "enable_http", False),
        "enable_tls12": _payload_bool(raw, "enable_tls12", True),
        "enable_tls13": _payload_bool(raw, "enable_tls13", False),
        "include_quic": _payload_bool(raw, "include_quic", True),
        "enable_ipv6": _payload_bool(raw, "enable_ipv6", False),
        "scan_level": scan_level,
        "repeats": _bounded_int(raw.get("repeats"), default=1, minimum=1, maximum=10),
        "repeat_parallel": _payload_bool(raw, "repeat_parallel", False),
        "skip_dnscheck": _payload_bool(raw, "skip_dnscheck", True),
        "skip_ipblock": _payload_bool(raw, "skip_ipblock", True),
        "curl_parallelism": _minimum_int(raw.get("curl_parallelism"), default=4, minimum=1),
        "limit_time_enabled": _payload_bool(raw, "limit_time_enabled", False),
        "timeout_hours": _bounded_int(raw.get("timeout_hours"), default=6, minimum=1, maximum=24),
    }


def _profile_name(value: Any) -> str:
    name = str(value or "").strip().lower()
    allowed = []
    for char in name:
        if char.isalnum() or char in {"-", "_"}:
            allowed.append(char)
    return "".join(allowed)[:64]


def _recover_runtime_before_serve(config: AppConfig) -> None:
    state = read_state(config.output.state_dir)
    if str(state.get("current_run_status") or "") == "quarantined":
        run_id = str(state.get("current_run_id") or "").strip()
        if not run_id:
            raise RuntimeError("quarantined runtime has no run id")
        recover_quarantined_process_run(run_id)
        _clear_stale_current_run(config, recovered_quarantine_run_id=run_id)
        return
    recovered = recover_registered_process_runs()
    if _requires_verified_root_recovery(state) and not recovered:
        raise RuntimeError("managed runtime recovery could not be verified")
    _clear_stale_current_run(config)


def _requires_verified_root_recovery(state: dict[str, Any]) -> bool:
    return (
        bool(str(state.get("current_run_id") or "").strip())
        and str(state.get("current_run_name") or "") in _ROOT_MANAGED_DISCOVERY_NAMES
        and str(state.get("current_run_status") or "") in {"queued", "running", "stopping"}
    )


def _clear_stale_current_run(config: AppConfig, *, recovered_quarantine_run_id: str = "") -> None:
    state = read_state(config.output.state_dir)
    if not state.get("current_run_id"):
        return
    if str(state.get("current_run_status") or "") == "quarantined":
        if not recovered_quarantine_run_id or recovered_quarantine_run_id != str(state.get("current_run_id") or ""):
            return
    if active_job_lock_payload(config.output.state_dir, cleanup_stale=True):
        return

    def clear_current_run(current: dict[str, Any]) -> dict[str, Any]:
        current["current_run_id"] = None
        current["current_run_name"] = None
        current["current_run_status"] = None
        return current

    update_state(config.output.state_dir, clear_current_run)


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


def _runs_page_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    return core_api.runs_history_page_payload(config, query)


def _presets_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "metadata": read_custom_preset_index(config.output.state_dir),
        "system_metadata": read_system_preset_index(config.output.state_dir),
        "system": read_system_presets(config.output.state_dir),
    }
    if _query_bool(query, "include_domains", False):
        payload["custom"] = read_custom_presets(config.output.state_dir)
    else:
        payload["custom"] = {"finder": {}, "common": {}}
    return payload


def _web_presets_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    return _presets_payload(config, query) | {
        "domain_sets": domain_sets(),
        "builtin": builtin_preset_sources(),
    }


def _release_info_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    settings = read_service_settings(config)
    channel = _query_str(query, "channel", str(settings.get("update_channel") or "stable"))
    stable = release_channel_info(current_version=__version__, channel="stable")
    prerelease = release_channel_info(current_version=__version__, channel="prerelease")
    selected = prerelease if channel == "prerelease" else stable
    return {"release": selected, "releases": {"stable": stable, "prerelease": prerelease}}


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


def _v2fly_categories_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    return list_v2fly_categories_local(
        config.output.state_dir,
        query=_query_str(query, "query", ""),
        limit=_query_int(query, "limit", 2000),
    )


def _v2fly_preview_payload(config: AppConfig, payload: dict[str, Any]) -> dict[str, Any]:
    state_dir = config.output.state_dir
    return preview_v2fly_preset(
        state_dir,
        scope=str(payload.get("scope") or "finder"),
        name=str(payload.get("name") or ""),
        categories=_payload_string_list(payload, "categories"),
        domains=_payload_string_list(payload, "domains"),
        fetcher=lambda category: fetch_v2fly_category_local(state_dir, category),
    )


def _v2fly_import_payload(config: AppConfig, payload: dict[str, Any]) -> dict[str, Any]:
    state_dir = config.output.state_dir
    return import_v2fly_preset(
        state_dir,
        scope=str(payload.get("scope") or "finder"),
        name=str(payload.get("name") or ""),
        categories=_payload_string_list(payload, "categories"),
        domains=_payload_string_list(payload, "domains"),
        fetcher=lambda category: fetch_v2fly_category_local(state_dir, category),
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


def _query_str(query: dict[str, list[str]], key: str, default: str) -> str:
    values = query.get(key) or []
    return values[0] if values else default


def _query_int(query: dict[str, list[str]], key: str, default: int) -> int:
    raw = _query_str(query, key, str(default))
    try:
        return int(raw)
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


def _multipart_file_bytes(body: bytes, boundary: str) -> bytes:
    delimiter = ("--" + boundary).encode("utf-8")
    for part in body.split(delimiter):
        if b"Content-Disposition:" not in part or b"filename=" not in part:
            continue
        header, sep, payload = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        payload = payload.rstrip(b"\r\n")
        if payload.endswith(b"--"):
            payload = payload[:-2].rstrip(b"\r\n")
        if payload:
            return payload
    raise ValueError("backup file is missing")


def _job_discovery(
    config: AppConfig, name: str, payload: dict[str, Any], stop_event: Any, run_id: str = ""
) -> dict[str, Any]:
    if is_blockchecks_job(name):
        kind = "multi-domain-discovery" if "multi-domain" in name else "standard-discovery"
        return _job_blockchecks_discovery(config, payload, stop_event, run_id, kind=kind)
    if name == "zapret-multi-domain-discovery":
        return _job_zapret_multi_domain_discovery(config, payload, stop_event, run_id)
    return _job_zapret_standard_discovery(config, payload, stop_event, run_id)


def _job_blockchecks_discovery(
    config: AppConfig, payload: dict[str, Any], stop_event: Any, run_id: str, *, kind: str
) -> dict[str, Any]:
    domains = _payload_domains(payload)
    settings = read_run_settings(config)
    max_parallelism = _minimum_int(settings.get("curl_parallelism_max"), default=10, minimum=1)
    return run_blockchecks_discovery(
        domains,
        config.output.state_dir,
        timeout_seconds=_payload_timeout_seconds(payload, default=0),
        include_quic=_payload_bool(payload, "include_quic", True),
        enable_http=_payload_bool(payload, "enable_http", False),
        enable_tls12=_payload_bool(payload, "enable_tls12", True),
        enable_tls13=_payload_bool(payload, "enable_tls13", False),
        enable_ipv6=_payload_bool(payload, "enable_ipv6", bool(settings.get("enable_ipv6"))),
        scan_level=str(payload.get("scan_level") or "standard"),
        repeats=_payload_int(payload, "repeats", 1),
        repeat_parallel=_payload_bool(payload, "repeat_parallel", False),
        skip_dnscheck=_payload_bool(payload, "skip_dnscheck", True),
        skip_ipblock=_payload_bool(payload, "skip_ipblock", True),
        curl_max_time=_minimum_int(payload.get("curl_max_time", settings.get("curl_max_time")), default=2, minimum=1),
        curl_parallelism=_bounded_int(
            payload.get("curl_parallelism"),
            default=int(settings.get("curl_parallelism_default") or 4),
            minimum=1,
            maximum=max_parallelism,
        ),
        debug_stdout=_payload_bool(payload, "debug_stdout", bool(settings.get("debug_stdout"))),
        stop_event=stop_event,
        run_id=run_id,
        kind=kind,
    )


def _job_zapret_standard_discovery(
    config: AppConfig, payload: dict[str, Any], stop_event: Any, run_id: str = ""
) -> dict[str, Any]:
    domains = _payload_domains(payload)
    settings = read_run_settings(config)
    return run_standard_discovery(
        domains,
        config.output.state_dir,
        timeout_seconds=_payload_timeout_seconds(payload, default=0),
        include_quic=_payload_bool(payload, "include_quic", True),
        enable_http=_payload_bool(payload, "enable_http", False),
        enable_tls12=_payload_bool(payload, "enable_tls12", True),
        enable_tls13=_payload_bool(payload, "enable_tls13", False),
        enable_ipv6=_payload_bool(payload, "enable_ipv6", bool(settings.get("enable_ipv6"))),
        scan_level=str(payload.get("scan_level") or "standard"),
        repeats=_payload_int(payload, "repeats", 1),
        repeat_parallel=_payload_bool(payload, "repeat_parallel", False),
        skip_dnscheck=_payload_bool(payload, "skip_dnscheck", True),
        skip_ipblock=_payload_bool(payload, "skip_ipblock", True),
        curl_max_time=_minimum_int(payload.get("curl_max_time", settings.get("curl_max_time")), default=2, minimum=1),
        curl_max_time_quic=_minimum_int(
            payload.get("curl_max_time_quic", settings.get("curl_max_time_quic")), default=2, minimum=1
        ),
        curl_max_time_doh=_minimum_int(
            payload.get("curl_max_time_doh", settings.get("curl_max_time_doh")), default=2, minimum=1
        ),
        debug_stdout=_payload_bool(payload, "debug_stdout", bool(settings.get("debug_stdout"))),
        stop_event=stop_event,
        run_id=run_id,
    )


def _job_zapret_multi_domain_discovery(
    config: AppConfig, payload: dict[str, Any], stop_event: Any, run_id: str = ""
) -> dict[str, Any]:
    domains = _payload_domains(payload)
    settings = read_run_settings(config)
    max_parallelism = _minimum_int(settings.get("curl_parallelism_max"), default=10, minimum=1)
    return run_multi_domain_discovery(
        domains,
        config.output.state_dir,
        timeout_seconds=_payload_timeout_seconds(payload, default=0),
        include_quic=_payload_bool(payload, "include_quic", True),
        enable_http=_payload_bool(payload, "enable_http", False),
        enable_tls12=_payload_bool(payload, "enable_tls12", True),
        enable_tls13=_payload_bool(payload, "enable_tls13", False),
        enable_ipv6=_payload_bool(payload, "enable_ipv6", bool(settings.get("enable_ipv6"))),
        scan_level=str(payload.get("scan_level") or "standard"),
        repeats=_payload_int(payload, "repeats", 1),
        repeat_parallel=_payload_bool(payload, "repeat_parallel", False),
        skip_dnscheck=_payload_bool(payload, "skip_dnscheck", True),
        skip_ipblock=_payload_bool(payload, "skip_ipblock", True),
        curl_max_time=_minimum_int(payload.get("curl_max_time", settings.get("curl_max_time")), default=2, minimum=1),
        curl_max_time_quic=_minimum_int(
            payload.get("curl_max_time_quic", settings.get("curl_max_time_quic")), default=2, minimum=1
        ),
        curl_max_time_doh=_minimum_int(
            payload.get("curl_max_time_doh", settings.get("curl_max_time_doh")), default=2, minimum=1
        ),
        curl_parallelism=_bounded_int(payload.get("curl_parallelism"), default=int(settings.get("curl_parallelism_default") or 4), minimum=1, maximum=max_parallelism),
        debug_stdout=_payload_bool(payload, "debug_stdout", bool(settings.get("debug_stdout"))),
        stop_event=stop_event,
        run_id=run_id,
    )

def _payload_domains(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("domains") or []
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    if not isinstance(raw, list):
        raw = []
    return [str(domain).strip() for domain in raw if str(domain).strip()]


def _payload_timeout_seconds(payload: dict[str, Any], default: int) -> int:
    if "timeout_seconds" not in payload or payload.get("timeout_seconds") is None:
        return default
    try:
        seconds = int(payload.get("timeout_seconds"))
    except (TypeError, ValueError):
        return default
    return max(0, seconds)


def _payload_int(payload: dict[str, Any], key: str, default: int) -> int:
    try:
        return int(payload.get(key, default))
    except (TypeError, ValueError):
        return default


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


def _payload_bool(payload: dict[str, Any], key: str, default: bool) -> bool:
    if key not in payload:
        return default
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)
