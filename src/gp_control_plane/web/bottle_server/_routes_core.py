"""bottle_server._routes_core — Core and Service API route handlers."""

from __future__ import annotations

import json
from typing import Any

from gp_control_plane import __version__, core_api, service_api
from gp_control_plane.auth import (
    PasswordValidationError,
    change_password,
    health_payload,
    login,
)
from gp_control_plane.backups import (
    create_snapshot_if_idle,
    delete_snapshot_if_idle,
    import_snapshot_archive,
    restore_snapshot_if_idle,
    snapshot_file_path,
)
from gp_control_plane.bs_engine import bs_triage_domain, export_nfconf, stop_blockchecks
from gp_control_plane.config import AppConfig
from gp_control_plane.discovery_engine import (
    campaign_lock_busy_message,
    check_blockchecks_install,
    is_blockchecks_job,
    normalize_engine,
)
from gp_control_plane.resource_budget import (
    BACKUP_UPLOAD_MAX_BYTES,
)
from gp_control_plane.settings import read_run_settings, read_service_settings, save_run_settings
from gp_control_plane.state import has_active_runtime
from gp_control_plane.web.api_server import _http as _api_http
from gp_control_plane.web.api_server import _post as _api_post
from gp_control_plane.web.api_server._errors import RequestBodyTooLarge
from gp_control_plane.web.api_server._events import (
    _current_run_latest_log_payload,
    _events_response_payload,
    _latest_log_payload,
)
from gp_control_plane.web.api_server._helpers import _query_one
from gp_control_plane.web.api_server._http import NDJSON_CONTENT_TYPE
from gp_control_plane.web.api_server._jobs import (
    _clean_install_vault_create_response,
    _clean_install_vault_public_metadata,
    _clean_install_vault_restore_response,
    _job_discovery,
)
from gp_control_plane.web.errors import error_payload, raise_storage_unavailable
from gp_control_plane.web.vendor.bottle import Bottle, HTTPResponse, request


def register_core_routes(
    app: Bottle,
    config: AppConfig,
    runner: Any,
    *,
    runtime_role: str,
    ui_enabled: bool,
    json_fn: Any,
    get_query_fn: Any,
    req_json_fn: Any,
    ensure_idle_fn: Any,
) -> None:
    """Register Core and Service API endpoints on Bottle app."""
    _json = json_fn
    _get_query_dict = get_query_fn
    _request_json = req_json_fn
    _ensure_idle = ensure_idle_fn

    @app.route("/api/health", method=["GET", "HEAD"])
    def api_health() -> HTTPResponse:
        return _json(health_payload())

    @app.route("/api/auth/login", method="POST")
    def api_login() -> HTTPResponse:
        try:
            payload = _request_json()
            res = login(config.output.state_dir, payload)
            return _json(res, 200)
        except Exception as exc:  # noqa: BLE001
            raise_storage_unavailable(exc)
            return _json({"error": str(exc)}, 400)

    @app.route("/api/auth/change-password", method="POST")
    def api_change_password() -> HTTPResponse:
        try:
            payload = _request_json()
            auth_h = request.get_header("Authorization")
            res = change_password(config.output.state_dir, payload, auth_h)
            return _json(res, 200)
        except PasswordValidationError as exc:
            return _json(error_payload("invalid_request", str(exc)), 400)
        except Exception as exc:  # noqa: BLE001
            raise_storage_unavailable(exc)
            return _json({"error": str(exc)}, 400)

    @app.route("/api/core/strategy-discovery/start-run", method="POST")
    def core_start_run() -> HTTPResponse:
        try:
            payload = _request_json()
            incoming = dict(payload)
            nested = incoming.get("settings") if isinstance(incoming.get("settings"), dict) else {}
            if "discovery_engine" not in nested:
                incoming["settings"] = {
                    **nested,
                    "discovery_engine": read_run_settings(config).get("discovery_engine"),
                }
            name, core_payload = core_api.strategy_discovery_job_payload(incoming)
            if is_blockchecks_job(name) and campaign_lock_busy_message():
                return _json(error_payload("conflict", "Campaign lock is held"), 409)
            func = lambda stop, run_id: _job_discovery(config, name, core_payload, stop, run_id)
            cancel_hook = (lambda: stop_blockchecks()) if is_blockchecks_job(name) else (lambda: _api_post.cleanup_nft_blockcheck_tables())
            job = runner.start(name, func, cancel_hook=cancel_hook)
            return _json(core_api.run_accepted_payload(job), 202)
        except RequestBodyTooLarge as exc:
            return _json(error_payload("request_too_large", str(exc)), 413)
        except Exception as exc:  # noqa: BLE001
            raise_storage_unavailable(exc)
            if "already running" in str(exc) or "lock" in str(exc) or "quarantined" in str(exc):
                return _json(error_payload("conflict", str(exc)), 409)
            return _json({"error": str(exc)}, 400)

    @app.route("/api/core/strategy-discovery/stop-current-run", method="POST")
    def core_stop_run() -> HTTPResponse:
        try:
            payload = _request_json()
            if payload.get("dry_run"):
                from gp_control_plane.state import read_state
                state = read_state(config.output.state_dir)
                return _json(
                    {
                        "accepted": True,
                        "status": "dry_run",
                        "run_id": str(state.get("current_run_id") or ""),
                    },
                    202,
                )
            job = runner.cancel_active()
            return _json(core_api.action_accepted_payload(job), 202)
        except RequestBodyTooLarge as exc:
            return _json(error_payload("request_too_large", str(exc)), 413)
        except Exception as exc:  # noqa: BLE001
            raise_storage_unavailable(exc)
            return _json({"error": str(exc)}, 409)

    @app.route("/api/core/strategy-discovery/export-nfconf", method="POST")
    def core_export_nfconf() -> HTTPResponse:
        try:
            payload = _request_json()
            raw_dir = payload.get("out_dir")
            out_dir = raw_dir if raw_dir else None
            limit = int(payload.get("limit") or 5)
            return _json(export_nfconf(out_dir=out_dir, limit=limit), 200)
        except RequestBodyTooLarge as exc:
            return _json(error_payload("request_too_large", str(exc)), 413)
        except Exception as exc:  # noqa: BLE001
            raise_storage_unavailable(exc)
            return _json({"error": str(exc)}, 400)

    @app.route("/api/core/status", method="GET")
    def core_status() -> HTTPResponse:
        return _json(core_api.status_payload(config))

    @app.route("/api/core/strategy-discovery/current-run-progress", method="GET")
    def core_progress() -> HTTPResponse:
        return _json(core_api.current_progress_payload(config))

    @app.route("/api/core/strategy-discovery/current-run-latest-log", method="GET")
    def core_run_log() -> HTTPResponse:
        return _json(_current_run_latest_log_payload(config, _get_query_dict()))

    @app.route("/api/core/strategy-discovery/preflight", method="GET")
    def core_preflight() -> HTTPResponse:
        if normalize_engine(read_run_settings(config).get("discovery_engine")) == "blockchecks":
            return _json(check_blockchecks_install())
        return _json(core_api.preflight_payload(config))

    @app.route("/api/core/strategy-discovery/triage", method="GET")
    def core_triage() -> HTTPResponse:
        domain = request.query.get("domain", "")
        return _json(bs_triage_domain(domain))

    @app.route("/api/core/presets/domain-lists", method="GET")
    def core_presets() -> HTTPResponse:
        return _json(core_api.domain_lists_payload(config))

    @app.route("/api/core/presets/save-domain-list", method="POST")
    def core_save_domain_list() -> HTTPResponse:
        try:
            payload = _request_json()
            return _json(core_api.save_domain_list_payload(config, payload), 200)
        except RequestBodyTooLarge as exc:
            return _json(error_payload("request_too_large", str(exc)), 413)
        except Exception as exc:  # noqa: BLE001
            raise_storage_unavailable(exc)
            return _json({"error": str(exc)}, 400)

    @app.route("/api/core/presets/delete-user-domain-list", method="POST")
    def core_delete_user_domain_list() -> HTTPResponse:
        try:
            payload = _request_json()
            return _json(core_api.delete_user_domain_list_payload(config, payload), 200)
        except RequestBodyTooLarge as exc:
            return _json(error_payload("request_too_large", str(exc)), 413)
        except Exception as exc:  # noqa: BLE001
            raise_storage_unavailable(exc)
            return _json({"error": str(exc)}, 400)

    @app.route("/api/core/presets/v2fly/categories", method="GET")
    def core_v2fly_cats() -> HTTPResponse:
        return _json(core_api.v2fly_categories_payload(config, _get_query_dict()))

    @app.route("/api/core/presets/v2fly/category-domains", method="GET")
    def core_v2fly_cat_doms() -> HTTPResponse:
        try:
            return _json(core_api.v2fly_category_domains_payload(config, _get_query_dict()))
        except Exception as exc:  # noqa: BLE001
            raise_storage_unavailable(exc)
            return _json({"error": str(exc)}, 400)

    @app.route("/api/core/backups/list", method="GET")
    def core_backups_list() -> HTTPResponse:
        return _json(core_api.backups_list_payload(config))

    @app.route("/api/core/backups/create", method="POST")
    def core_backup_create() -> HTTPResponse:
        try:
            created = create_snapshot_if_idle(config.output.state_dir)
            if created.get("queued"):
                return _json(error_payload("runtime_busy", "Backup mutations are blocked while another job is running."), 409)
            return _json(core_api.backup_snapshot_payload(created.get("snapshot") or {}), 201)
        except RequestBodyTooLarge as exc:
            return _json(error_payload("request_too_large", str(exc)), 413)
        except Exception as exc:  # noqa: BLE001
            raise_storage_unavailable(exc)
            return _json(error_payload("runtime_busy", str(exc)), 409)

    @app.route("/api/core/backups/restore", method="POST")
    def core_backup_restore() -> HTTPResponse:
        try:
            payload = _request_json()
            snapshot_id = core_api.payload_snapshot_id(payload)
            restored = restore_snapshot_if_idle(config.output.state_dir, snapshot_id)
            if restored.get("queued"):
                return _json(error_payload("runtime_busy", "Backup mutations are blocked while another job is running."), 409)
            return _json({"accepted": True, "status": "success", "snapshot_id": snapshot_id}, 202)
        except RequestBodyTooLarge as exc:
            return _json(error_payload("request_too_large", str(exc)), 413)
        except Exception as exc:  # noqa: BLE001
            raise_storage_unavailable(exc)
            return _json(error_payload("runtime_busy", str(exc)), 409)

    @app.route("/api/core/backups/delete", method="POST")
    def core_backup_delete() -> HTTPResponse:
        try:
            payload = _request_json()
            snapshot_id = core_api.payload_snapshot_id(payload)
            deleted = delete_snapshot_if_idle(config.output.state_dir, snapshot_id)
            if deleted.get("queued"):
                return _json(error_payload("runtime_busy", "Backup mutations are blocked while another job is running."), 409)
            return _json({"deleted": 1}, 200)
        except RequestBodyTooLarge as exc:
            return _json(error_payload("request_too_large", str(exc)), 413)
        except Exception as exc:  # noqa: BLE001
            raise_storage_unavailable(exc)
            return _json(error_payload("runtime_busy", str(exc)), 409)

    @app.route("/api/core/backups/download-archive", method="GET")
    def core_backup_download() -> HTTPResponse:
        query_dict = _get_query_dict()
        snapshot_id = _query_one(query_dict, "snapshot_id") or _query_one(query_dict, "snapshot")
        try:
            file_path = snapshot_file_path(config.output.state_dir, snapshot_id, "archive")
            if not file_path.is_file():
                return _json(error_payload("not_found", "Snapshot archive was not found."), 404)
            data = file_path.read_bytes()
            return HTTPResponse(
                body=data,
                status=200,
                headers={
                    "Content-Type": "application/zip",
                    "Content-Disposition": f'attachment; filename="{file_path.name}"',
                    "Content-Length": str(len(data)),
                },
            )
        except Exception as exc:  # noqa: BLE001
            raise_storage_unavailable(exc)
            return _json(error_payload("not_found", "Snapshot archive was not found."), 404)

    @app.route("/api/core/backups/upload", method="POST")
    def core_backup_upload() -> HTTPResponse:
        try:
            if has_active_runtime(config.output.state_dir):
                return _json(error_payload("runtime_busy", "Backup mutations are blocked while another job is running."), 409)
            content_length = int(request.get_header("Content-Length") or "0")
            max_upload = getattr(_api_http, "MAX_BACKUP_UPLOAD_BYTES", BACKUP_UPLOAD_MAX_BYTES)
            if content_length > max_upload:
                return _json(error_payload("request_too_large", "request body is too large"), 413)
            body = request.body.read(max_upload + 1)
            if len(body) > max_upload:
                return _json(error_payload("request_too_large", "request body is too large"), 413)
            imported = import_snapshot_archive(config.output.state_dir, body)
            return _json(core_api.backup_snapshot_payload(imported.get("snapshot") or {}), 201)
        except Exception as exc:  # noqa: BLE001
            raise_storage_unavailable(exc)
            return _json({"error": str(exc)}, 400)

    @app.route("/api/core/clean-install-vaults/list", method="GET")
    def core_vaults_list() -> HTTPResponse:
        vaults = [
            _clean_install_vault_public_metadata(item)
            for item in (core_api.clean_install_vault_list_payload(config).get("vaults") or [])
            if isinstance(item, dict)
        ]
        return _json({"vaults": vaults})

    @app.route("/api/core/clean-install-vaults/create", method="POST")
    def core_vault_create() -> HTTPResponse:
        try:
            payload = _request_json()
            if payload:
                return _json({"error": "clean-install vault create does not accept request fields"}, 400)
            created = core_api.clean_install_vault_create_payload(config, payload)
            return _json(_clean_install_vault_create_response(created), 201)
        except RequestBodyTooLarge as exc:
            return _json(error_payload("request_too_large", str(exc)), 413)
        except Exception as exc:  # noqa: BLE001
            raise_storage_unavailable(exc)
            return _json({"error": str(exc)}, 400)

    @app.route("/api/core/clean-install-vaults/status", method="GET")
    def core_vault_status() -> HTTPResponse:
        try:
            res = core_api.clean_install_vault_status_payload(config, _get_query_dict())
            return _json(_clean_install_vault_public_metadata(res))
        except FileNotFoundError:
            return _json(error_payload("not_found", "Clean-install vault was not found."), 404)
        except Exception as exc:  # noqa: BLE001
            raise_storage_unavailable(exc)
            return _json(error_payload("invalid_request", str(exc)), 400)

    @app.route("/api/core/clean-install-vaults/restore", method="POST")
    def core_vault_restore() -> HTTPResponse:
        try:
            payload = _request_json()
            restored = core_api.clean_install_vault_restore_payload(config, payload)
            public_resp = _clean_install_vault_restore_response(restored, "")
            return _json(public_resp, 200)
        except RequestBodyTooLarge as exc:
            return _json(error_payload("request_too_large", str(exc)), 413)
        except Exception as exc:  # noqa: BLE001
            raise_storage_unavailable(exc)
            return _json(error_payload("invalid_request", str(exc)), 400)

    @app.route("/api/core/run-settings", method="GET")
    def core_run_settings() -> HTTPResponse:
        return _json(core_api.run_settings_payload(read_run_settings(config)))

    @app.route("/api/core/run-settings/save", method="POST")
    def core_run_settings_save() -> HTTPResponse:
        try:
            payload = _request_json()
            settings_data = payload.get("settings") if isinstance(payload.get("settings"), dict) else payload
            res = save_run_settings(config, settings_data)
            return _json(core_api.run_settings_payload(res), 200)
        except RequestBodyTooLarge as exc:
            return _json(error_payload("request_too_large", str(exc)), 413)
        except Exception as exc:  # noqa: BLE001
            raise_storage_unavailable(exc)
            return _json({"error": str(exc)}, 400)

    @app.route("/api/core/runs/history", method="GET")
    def core_history() -> HTTPResponse:
        return _json(core_api.runs_history_payload(config, _get_query_dict()))

    @app.route("/api/core/runs/latest-log", method="GET")
    def core_latest_log() -> HTTPResponse:
        return _json(_latest_log_payload(config, _get_query_dict()))

    @app.route("/api/core/strategy-candidates", method="GET")
    def core_candidates() -> HTTPResponse:
        try:
            return _json(core_api.strategy_candidates_payload(config, _get_query_dict()))
        except Exception as exc:  # noqa: BLE001
            raise_storage_unavailable(exc)
            return _json({"error": str(exc)}, 400)

    @app.route("/api/core/strategy-candidates/export", method=["GET", "HEAD"])
    def core_candidates_export() -> HTTPResponse:
        query = _get_query_dict()
        if request.method == "HEAD":
            return HTTPResponse(status=200, headers={"Content-Type": NDJSON_CONTENT_TYPE, "Content-Length": "0"})
        try:
            from gp_control_plane.storage import is_storage_unavailable_error
            lines = list(core_api.iter_strategy_candidates_export_lines(config, query))
            return HTTPResponse(
                body=b"".join(lines),
                status=200,
                headers={"Content-Type": NDJSON_CONTENT_TYPE},
            )
        except Exception as exc:  # noqa: BLE001
            from gp_control_plane.storage import is_storage_unavailable_error
            if is_storage_unavailable_error(exc):
                return HTTPResponse(
                    body=json.dumps(
                        error_payload("storage_unavailable", "Storage is temporarily unavailable."),
                        ensure_ascii=False,
                    ),
                    status=503,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                )
            return _json(error_payload("internal_error", str(exc)), 500)

    @app.route("/api/core/strategy-pairs", method="GET")
    def core_pairs() -> HTTPResponse:
        return _json(core_api.strategy_pairs_payload(config, _get_query_dict()))

    @app.route("/api/core/events", method="GET")
    def core_events() -> HTTPResponse:
        return _json(_events_response_payload(config, _get_query_dict(), stream="core"))

    @app.route("/api/service/status", method="GET")
    def service_status() -> HTTPResponse:
        return _json(
            service_api.service_status_payload(
                config,
                current_version=__version__,
                runtime_role=runtime_role,
                web_enabled=ui_enabled,
            )
        )

    @app.route("/api/service/releases/available", method="GET")
    def service_releases() -> HTTPResponse:
        return _json(
            service_api.available_releases_payload(
                read_service_settings(config), current_version=__version__
            )
        )

    @app.route("/api/service/v2fly/local-storage-status", method="GET")
    def service_v2fly_status() -> HTTPResponse:
        return _json(service_api.v2fly_storage_status_payload(config))

    @app.route("/api/service/v2fly/check-updates", method="POST")
    def service_v2fly_check_updates() -> HTTPResponse:
        try:
            _ensure_idle()
            return _json(service_api.v2fly_check_updates_payload(config), 200)
        except Exception as exc:  # noqa: BLE001
            raise_storage_unavailable(exc)
            if isinstance(exc, RuntimeError) and "blocked" in str(exc):
                return _json(error_payload("conflict", str(exc)), 409)
            return _json({"error": str(exc)}, 400)

    @app.route("/api/service/v2fly/update-local-storage", method="POST")
    def service_v2fly_update() -> HTTPResponse:
        try:
            payload = _request_json()
            if not payload.get("dry_run"):
                _ensure_idle()
            return _json(service_api.v2fly_update_local_storage_payload(config, payload), 200)
        except RequestBodyTooLarge as exc:
            return _json(error_payload("request_too_large", str(exc)), 413)
        except Exception as exc:  # noqa: BLE001
            raise_storage_unavailable(exc)
            if isinstance(exc, RuntimeError) and "blocked" in str(exc):
                return _json(error_payload("conflict", str(exc)), 409)
            return _json({"error": str(exc)}, 400)
