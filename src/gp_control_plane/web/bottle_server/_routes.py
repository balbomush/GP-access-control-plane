"""bottle_server._routes — Bottle app creation and main routing setup."""

from __future__ import annotations

import json
from http import HTTPStatus
from typing import Any

from gp_control_plane.auth import (
    AuthenticationError,
    require_bearer_token,
)
from gp_control_plane.config import AppConfig
from gp_control_plane.resource_budget import JSON_REQUEST_MAX_BYTES
from gp_control_plane.state import active_job_lock_payload
from gp_control_plane.storage import is_storage_unavailable_error
from gp_control_plane.web.api_server import _http as _api_http
from gp_control_plane.web.api_server._errors import RequestBodyTooLarge
from gp_control_plane.web.bottle_server._routes_core import register_core_routes
from gp_control_plane.web.bottle_server._routes_web import register_web_routes
from gp_control_plane.web.errors import error_payload, normalize_error_payload
from gp_control_plane.web.routes import route_for
from gp_control_plane.web.vendor.bottle import Bottle, HTTPResponse, request


def create_bottle_app(
    config: AppConfig,
    runner: Any,
    *,
    runtime_role: str = "monolith",
    ui_enabled: bool = True,
) -> Bottle:
    """Create and configure Bottle WSGI application."""
    app = Bottle()

    def _authorize(path: str) -> None:
        if not path.startswith("/api/"):
            return
        route = route_for(request.method, path)
        if route and not route.auth_required:
            return
        auth_header = request.get_header("Authorization")
        require_bearer_token(config.output.state_dir, auth_header)

    def _json(payload: dict[str, Any], status: int = 200) -> HTTPResponse:
        norm = normalize_error_payload(payload, HTTPStatus(status))
        data = json.dumps(norm, ensure_ascii=False, separators=(",", ":"))
        return HTTPResponse(
            body=data,
            status=status,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )

    def _get_query_dict() -> dict[str, list[str]]:
        return {k: request.query.getall(k) for k in request.query}

    def _request_json() -> dict[str, Any]:
        try:
            length = int(request.get_header("Content-Length") or "0")
        except ValueError:
            length = 0
        max_json = getattr(_api_http, "MAX_JSON_REQUEST_BYTES", JSON_REQUEST_MAX_BYTES)
        if length > max_json:
            raise RequestBodyTooLarge("request body is too large")
        try:
            raw = request.body.read(length or max_json).decode("utf-8")
            if not raw.strip():
                return {}
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, RequestBodyTooLarge):
                raise
            return {}

    def _ensure_idle() -> None:
        if active_job_lock_payload(config.output.state_dir, cleanup_stale=True):
            raise RuntimeError("service action is blocked while another job is running")

    @app.hook("before_request")
    def before_request_hook() -> None:
        if request.method == "OPTIONS":
            return
        try:
            _authorize(request.path)
        except AuthenticationError:
            err_data = json.dumps(
                error_payload("authentication_required", "A Bearer token is required."),
                ensure_ascii=False,
            )
            raise HTTPResponse(
                body=err_data,
                status=401,
                headers={"Content-Type": "application/json; charset=utf-8", "WWW-Authenticate": "Bearer"},
            ) from None
        except Exception as exc:  # noqa: BLE001
            if is_storage_unavailable_error(exc):
                err_data = json.dumps(
                    error_payload("storage_unavailable", "Storage is temporarily unavailable."),
                    ensure_ascii=False,
                )
                raise HTTPResponse(
                    body=err_data,
                    status=530,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                ) from None
            raise

    @app.error(404)
    def error404(err: Any) -> HTTPResponse:
        return _json({"error": "not found"}, 404)

    @app.error(500)
    def error500(err: Any) -> HTTPResponse:
        exc = getattr(err, "exception", None)
        if exc and is_storage_unavailable_error(exc):
            return HTTPResponse(
                body=json.dumps(
                    error_payload("storage_unavailable", "Storage is temporarily unavailable."),
                    ensure_ascii=False,
                ),
                status=503,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
        return _json(error_payload("internal_error", str(exc or "Internal Server Error")), 500)

    register_core_routes(
        app,
        config,
        runner,
        runtime_role=runtime_role,
        ui_enabled=ui_enabled,
        json_fn=_json,
        get_query_fn=_get_query_dict,
        req_json_fn=_request_json,
        ensure_idle_fn=_ensure_idle,
    )

    register_web_routes(
        app,
        config,
        ui_enabled=ui_enabled,
        json_fn=_json,
        get_query_fn=_get_query_dict,
        req_json_fn=_request_json,
    )

    return app
