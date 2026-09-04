"""api_server POST routing handler — moved from api_server.py (package split)."""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from gp_control_plane import core_api, service_api
from gp_control_plane.auth import (
    AuthenticationError,
    PasswordValidationError,
    change_password,
    login,
)
from gp_control_plane.backups import (
    create_snapshot_if_idle,
    delete_snapshot_if_idle,
    import_snapshot_archive,
    restore_snapshot_if_idle,
)
from gp_control_plane.bs_engine import export_nfconf, stop_blockchecks
from gp_control_plane.discovery_engine import (
    campaign_lock_busy_message,
    is_blockchecks_job,
)
from gp_control_plane.settings import read_run_settings, save_run_settings
from gp_control_plane.state import (
    active_job_lock_payload,
    has_active_runtime,
    read_state,
)
from gp_control_plane.storage import (
    is_storage_unavailable_error as _is_storage_unavailable_error,
)
from gp_control_plane.web.api_server._errors import (
    RequestBodyTooLarge,
    RuntimeBusyError,
)
from gp_control_plane.web.api_server._jobs import (
    _clean_install_vault_create_response,
    _clean_install_vault_restore_response,
    _job_discovery,
)
from gp_control_plane.web.api_server._payloads import web_json_post_response
from gp_control_plane.web.errors import error_payload
from gp_control_plane.web.routes import JSON_POST_ROUTE_PATHS
from gp_control_plane.zapret2 import cleanup_nft_blockcheck_tables


class PostMixin:
    def do_POST(self) -> None:
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        if not self._authorize_api(path):
            return
        if path == "/api/core/backups/upload":
            try:
                if has_active_runtime(self.config.output.state_dir):
                    raise RuntimeBusyError()
                imported = import_snapshot_archive(self.config.output.state_dir, self._request_upload_bytes())
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
        if path.startswith("/api/web/") and not self.ui_enabled:
            self._not_found()
            return
        self._dispatch_json_post(post_routes[path])

    def _json_post_routes(self, payload: dict[str, Any]) -> dict[str, Any]:
        def stop_current_run() -> tuple[dict[str, Any], HTTPStatus]:
            if payload.get("dry_run"):
                state = read_state(self.config.output.state_dir)
                return (
                    {
                        "accepted": True,
                        "status": "dry_run",
                        "run_id": str(state.get("current_run_id") or ""),
                    },
                    HTTPStatus.ACCEPTED,
                )
            job = self.runner.cancel_active()
            return core_api.action_accepted_payload(job), HTTPStatus.ACCEPTED

        def start_strategy_discovery() -> tuple[dict[str, Any], HTTPStatus]:
            incoming = dict(payload)
            nested = incoming.get("settings") if isinstance(incoming.get("settings"), dict) else {}
            if "discovery_engine" not in nested:
                incoming["settings"] = {
                    **nested,
                    "discovery_engine": read_run_settings(self.config).get("discovery_engine"),
                }
            name, core_payload = core_api.strategy_discovery_job_payload(incoming)
            if is_blockchecks_job(name) and campaign_lock_busy_message():
                raise RuntimeBusyError()
            func = lambda stop, run_id: _job_discovery(self.config, name, core_payload, stop, run_id)
            cancel_hook = stop_blockchecks if is_blockchecks_job(name) else cleanup_nft_blockcheck_tables
            job = self.runner.start(
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
            created = create_snapshot_if_idle(self.config.output.state_dir)
            if created.get("queued"):
                raise RuntimeBusyError()
            return core_api.backup_snapshot_payload(created.get("snapshot") or {}), HTTPStatus.CREATED

        def restore_core_backup() -> tuple[dict[str, Any], HTTPStatus]:
            snapshot_id = core_api.payload_snapshot_id(payload)
            restored = restore_snapshot_if_idle(self.config.output.state_dir, snapshot_id)
            if restored.get("queued"):
                raise RuntimeBusyError()
            return {"accepted": True, "status": "success", "snapshot_id": snapshot_id}, HTTPStatus.ACCEPTED

        def delete_core_backup() -> tuple[dict[str, Any], HTTPStatus]:
            snapshot_id = core_api.payload_snapshot_id(payload)
            deleted = delete_snapshot_if_idle(self.config.output.state_dir, snapshot_id)
            if deleted.get("queued"):
                raise RuntimeBusyError()
            return {"deleted": 1}, HTTPStatus.OK

        def create_clean_install_vault() -> tuple[dict[str, Any], HTTPStatus]:
            if payload:
                raise ValueError("clean-install vault create does not accept request fields")
            created = core_api.clean_install_vault_create_payload(self.config, payload)
            return _clean_install_vault_create_response(created), HTTPStatus.CREATED

        def restore_clean_install_vault() -> tuple[dict[str, Any], HTTPStatus]:
            allowed = {"vault_id", "confirm_restore"}
            unknown = sorted(str(key) for key in payload if str(key) not in allowed)
            if unknown:
                raise ValueError(f"unsupported clean-install vault restore fields: {', '.join(unknown)}")
            # Preserve the raw JSON value for strict core validation:
            # whitespace or a non-string vault_id must not be normalized
            # into an otherwise acceptable identifier at this boundary.
            restored = core_api.clean_install_vault_restore_payload(self.config, payload)
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
            if active_job_lock_payload(self.config.output.state_dir, cleanup_stale=True):
                raise RuntimeError("service action is blocked while another job is running")

        def v2fly_check_updates() -> tuple[dict[str, Any], HTTPStatus]:
            ensure_service_action_idle()
            return service_api.v2fly_check_updates_payload(self.config), HTTPStatus.OK

        def v2fly_update_local_storage() -> tuple[dict[str, Any], HTTPStatus]:
            if not payload.get("dry_run"):
                ensure_service_action_idle()
            return service_api.v2fly_update_local_storage_payload(self.config, payload), HTTPStatus.OK

        return {
            "/api/auth/login": (
                lambda: (login(self.config.output.state_dir, payload), HTTPStatus.OK),
                HTTPStatus.UNAUTHORIZED,
                HTTPStatus.BAD_REQUEST,
            ),
            "/api/auth/change-password": (
                lambda: (
                    change_password(self.config.output.state_dir, payload, self.headers.get("Authorization")),
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
                lambda: (core_api.save_domain_list_payload(self.config, payload), HTTPStatus.OK),
                HTTPStatus.BAD_REQUEST,
                HTTPStatus.BAD_REQUEST,
            ),
            "/api/core/presets/delete-user-domain-list": (
                lambda: (core_api.delete_user_domain_list_payload(self.config, payload), HTTPStatus.OK),
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
                lambda: (core_api.run_settings_payload(save_run_settings(self.config, payload.get("settings") or payload)), HTTPStatus.OK),
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
                lambda: web_json_post_response(self.config, "/api/web/run-preferences", payload),
                HTTPStatus.BAD_REQUEST,
                HTTPStatus.BAD_REQUEST,
            ),
            "/api/web/presets/save": (
                lambda: web_json_post_response(self.config, "/api/web/presets/save", payload),
                HTTPStatus.BAD_REQUEST,
                HTTPStatus.BAD_REQUEST,
            ),
            "/api/web/presets/delete-user-lists": (
                lambda: web_json_post_response(self.config, "/api/web/presets/delete-user-lists", payload),
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
