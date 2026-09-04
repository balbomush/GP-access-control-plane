"""api_server GET/HEAD routing handler — moved from api_server.py (package split)."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, urlparse

from gp_control_plane import __version__, core_api, service_api
from gp_control_plane.auth import health_payload
from gp_control_plane.bs_engine import bs_triage_domain
from gp_control_plane.discovery_engine import (
    check_blockchecks_install,
    normalize_engine,
)
from gp_control_plane.settings import read_run_settings, read_service_settings
from gp_control_plane.storage import (
    is_storage_unavailable_error as _is_storage_unavailable_error,
)
from gp_control_plane.web.api_server._events import (
    _current_run_latest_log_payload,
    _events_response_payload,
    _latest_log_payload,
)
from gp_control_plane.web.api_server._helpers import _query_one
from gp_control_plane.web.api_server._http import NDJSON_CONTENT_TYPE
from gp_control_plane.web.api_server._jobs import _clean_install_vault_public_metadata
from gp_control_plane.web.api_server._pages import index_html
from gp_control_plane.web.api_server._payloads import web_json_get_payload
from gp_control_plane.web.docs import (
    SWAGGER_HTML_CONTENT_TYPE,
    SWAGGER_PATHS,
    swagger_ui_html,
)
from gp_control_plane.web.errors import error_payload
from gp_control_plane.web.routes import JSON_GET_ROUTE_PATHS, JSON_HEAD_ROUTE_PATHS


class GetMixin:
    def do_GET(self) -> None:
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        if not self._authorize_api(path):
            return
        query = parse_qs(parsed_url.query)
        if path == "/":
            if self.ui_enabled:
                self._html()
            else:
                self._json({"error": "web ui is disabled in core mode"}, status=HTTPStatus.NOT_FOUND)
        elif path == "/openapi.json":
            self._openapi_json()
        elif path in SWAGGER_PATHS:
            self._swagger()
        elif path == "/api/core/strategy-candidates/export":
            self._stream_strategy_candidates_export(query)
        elif path in JSON_GET_ROUTE_PATHS:
            self._dispatch_json_get(path, query)
        elif path == "/api/core/backups/download-archive":
            core_query = {"snapshot": [_query_one(query, "snapshot_id")], "file": ["archive"]}
            self._download_backup(core_query)
        elif path == "/api/web/events/stream":
            if not self.ui_enabled:
                self._not_found()
                return
            self._events()
        else:
            self._not_found()

    def _json_get_routes(self, query: dict[str, list[str]]) -> dict[str, Any]:
        return {
            "/api/health": health_payload,
            "/api/core/status": lambda: core_api.status_payload(self.config),
            "/api/core/strategy-discovery/current-run-progress": lambda: core_api.current_progress_payload(self.config),
            "/api/core/strategy-discovery/current-run-latest-log": lambda: _current_run_latest_log_payload(self.config, query),
            "/api/core/strategy-discovery/preflight": lambda: (
                check_blockchecks_install()
                if normalize_engine(read_run_settings(self.config).get("discovery_engine")) == "blockchecks"
                else core_api.preflight_payload(self.config)
            ),
            "/api/core/strategy-discovery/triage": lambda: bs_triage_domain(_query_one(query, "domain")),
            "/api/core/presets/domain-lists": lambda: core_api.domain_lists_payload(self.config),
            "/api/core/presets/v2fly/categories": lambda: core_api.v2fly_categories_payload(self.config, query),
            "/api/core/presets/v2fly/category-domains": lambda: core_api.v2fly_category_domains_payload(self.config, query),
            "/api/core/backups/list": lambda: core_api.backups_list_payload(self.config),
            "/api/core/clean-install-vaults/list": lambda: {
                "vaults": [
                    _clean_install_vault_public_metadata(item)
                    for item in (core_api.clean_install_vault_list_payload(self.config).get("vaults") or [])
                    if isinstance(item, dict)
                ]
            },
            "/api/core/clean-install-vaults/status": lambda: _clean_install_vault_public_metadata(
                core_api.clean_install_vault_status_payload(self.config, query)
            ),
            "/api/core/run-settings": lambda: core_api.run_settings_payload(read_run_settings(self.config)),
            "/api/core/runs/history": lambda: core_api.runs_history_payload(self.config, query),
            "/api/core/runs/latest-log": lambda: _latest_log_payload(self.config, query),
            "/api/core/strategy-candidates": lambda: core_api.strategy_candidates_payload(self.config, query),
            "/api/core/strategy-pairs": lambda: core_api.strategy_pairs_payload(self.config, query),
            "/api/core/events": lambda: _events_response_payload(self.config, query, stream="core"),
            "/api/service/status": lambda: service_api.service_status_payload(
                self.config,
                current_version=__version__,
                runtime_role=self.runtime_role,
                web_enabled=self.web_install_enabled,
            ),
            "/api/service/releases/available": lambda: service_api.available_releases_payload(
                read_service_settings(self.config), current_version=__version__
            ),
            "/api/service/v2fly/local-storage-status": lambda: service_api.v2fly_storage_status_payload(self.config),
        }

    def _dispatch_json_get(self, path: str, query: dict[str, list[str]]) -> None:
        if path.startswith("/api/web/"):
            if not self.ui_enabled:
                self._not_found()
                return
            try:
                self._json(web_json_get_payload(self.config, path, query))
            except Exception as exc:  # noqa: BLE001
                if _is_storage_unavailable_error(exc):
                    self._storage_unavailable()
                    return
                self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        try:
            self._json(self._json_get_routes(query)[path]())
        except Exception as exc:
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

    def do_HEAD(self) -> None:
        path = urlparse(self.path).path
        if not self._authorize_api(path):
            return
        if path == "/":
            if self.ui_enabled:
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
        elif path == "/api/web/events/stream" and self.ui_enabled:
            self._head(HTTPStatus.OK, "text/event-stream; charset=utf-8", 0)
        elif path.startswith("/api/web/") and not self.ui_enabled:
            self._head(HTTPStatus.NOT_FOUND, "application/json; charset=utf-8", 0)
        elif path in JSON_HEAD_ROUTE_PATHS:
            self._head(HTTPStatus.OK, "application/json; charset=utf-8", 0)
        else:
            self._head(HTTPStatus.NOT_FOUND, "application/json; charset=utf-8", 0)
