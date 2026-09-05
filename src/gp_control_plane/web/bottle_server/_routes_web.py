"""bottle_server._routes_web — Web UI page, docs, and UI API route handlers."""

from __future__ import annotations

from typing import Any

from gp_control_plane.auth import AuthenticationError, require_bearer_token
from gp_control_plane.config import AppConfig
from gp_control_plane.web.api_server._errors import RequestBodyTooLarge
from gp_control_plane.web.api_server._payloads import (
    web_json_get_payload,
    web_json_post_response,
)
from gp_control_plane.web.api_server._preferences import (
    read_run_preferences,
    save_run_preferences,
)
from gp_control_plane.web.bottle_server._sse import stream_web_events
from gp_control_plane.web.docs import (
    OPENAPI_JSON_CONTENT_TYPE,
    SWAGGER_HTML_CONTENT_TYPE,
    openapi_json_bytes,
    swagger_ui_html,
)
from gp_control_plane.web.errors import error_payload, raise_storage_unavailable
from gp_control_plane.web.ui_bottle import bottle_index_html
from gp_control_plane.web.vendor.bottle import Bottle, HTTPResponse, request, response


def register_web_routes(
    app: Bottle,
    config: AppConfig,
    *,
    ui_enabled: bool,
    json_fn: Any,
    get_query_fn: Any,
    req_json_fn: Any,
) -> None:
    """Register Web UI and Web API endpoints on Bottle app."""
    _json = json_fn
    _get_query_dict = get_query_fn
    _request_json = req_json_fn

    @app.route("/", method=["GET", "HEAD"])
    def root_page() -> HTTPResponse:
        if not ui_enabled:
            return _json({"error": "web ui is disabled in core mode"}, 404)
        html = bottle_index_html()
        data = html.encode("utf-8")
        if request.method == "HEAD":
            return HTTPResponse(
                body=b"",
                status=200,
                headers={
                    "Content-Type": "text/html; charset=utf-8",
                    "Cache-Control": "no-store",
                    "Content-Length": str(len(data)),
                },
            )
        return HTTPResponse(
            body=data,
            status=200,
            headers={"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"},
        )

    @app.route("/openapi.json", method=["GET", "HEAD"])
    def openapi_route() -> HTTPResponse:
        data = openapi_json_bytes(core_only=not ui_enabled)
        if request.method == "HEAD":
            return HTTPResponse(
                body=b"",
                status=200,
                headers={"Content-Type": OPENAPI_JSON_CONTENT_TYPE, "Content-Length": str(len(data))},
            )
        return HTTPResponse(
            body=data,
            status=200,
            headers={"Content-Type": OPENAPI_JSON_CONTENT_TYPE, "Cache-Control": "no-store"},
        )

    @app.route("/swagger", method=["GET", "HEAD"])
    @app.route("/swagger/", method=["GET", "HEAD"])
    def swagger_route() -> HTTPResponse:
        html = swagger_ui_html()
        data = html.encode("utf-8")
        if request.method == "HEAD":
            return HTTPResponse(
                body=b"",
                status=200,
                headers={"Content-Type": SWAGGER_HTML_CONTENT_TYPE, "Content-Length": str(len(data))},
            )
        return HTTPResponse(
            body=data,
            status=200,
            headers={"Content-Type": SWAGGER_HTML_CONTENT_TYPE, "Cache-Control": "no-store"},
        )

    @app.route("/api/web/run-preferences", method=["GET", "POST"])
    def web_run_preferences() -> HTTPResponse:
        if not ui_enabled:
            return _json({"error": "not found"}, 404)
        if request.method == "GET":
            try:
                return _json({"run_preferences": read_run_preferences(config)})
            except Exception as exc:  # noqa: BLE001
                raise_storage_unavailable(exc)
                return _json({"error": str(exc)}, 400)
        try:
            payload = _request_json()
            return _json({"run_preferences": save_run_preferences(config, payload.get("run_preferences") or payload)})
        except RequestBodyTooLarge as exc:
            return _json(error_payload("request_too_large", str(exc)), 413)
        except Exception as exc:  # noqa: BLE001
            raise_storage_unavailable(exc)
            return _json({"error": str(exc)}, 400)

    @app.route("/api/web/events/stream", method="GET")
    @app.route("/api/web/events/stream", method="HEAD")
    def web_events_stream() -> Any:
        if not ui_enabled:
            return _json({"error": "not found"}, 404)
        if request.method == "HEAD":
            return HTTPResponse(
                body=b"",
                status=200,
                headers={"Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-store", "Content-Length": "0"},
            )
        auth_header = request.get_header("Authorization")
        try:
            require_bearer_token(config.output.state_dir, auth_header)
        except AuthenticationError:
            return _json(error_payload("authentication_required", "A Bearer token is required."), 401)
        response.content_type = "text/event-stream; charset=utf-8"
        response.set_header("Cache-Control", "no-store")
        return stream_web_events(config, auth_header)

    @app.route("/api/web/<endpoint:path>", method=["GET", "POST"])
    def web_api_generic(endpoint: str) -> HTTPResponse:
        if not ui_enabled:
            return _json({"error": "not found"}, 404)
        path = f"/api/web/{endpoint}"
        query = _get_query_dict()
        if request.method == "GET":
            try:
                return _json(web_json_get_payload(config, path, query))
            except KeyError:
                return _json({"error": "not found"}, 404)
            except Exception as exc:  # noqa: BLE001
                raise_storage_unavailable(exc)
                return _json({"error": str(exc)}, 400)
        try:
            payload = _request_json()
            res, status = web_json_post_response(config, path, payload)
            return _json(res, status.value if hasattr(status, "value") else status)
        except RequestBodyTooLarge as exc:
            return _json(error_payload("request_too_large", str(exc)), 413)
        except KeyError:
            return _json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            raise_storage_unavailable(exc)
            return _json({"error": str(exc)}, 400)
