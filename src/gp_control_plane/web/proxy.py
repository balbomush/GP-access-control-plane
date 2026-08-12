from __future__ import annotations

import http.client
import json
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..config import AppConfig
from ..auth import AuthenticationError, require_bearer_token
from ..resource_budget import BACKUP_UPLOAD_MAX_BYTES, JSON_REQUEST_MAX_BYTES, PROXY_STREAM_CHUNK_BYTES
from ..storage import is_storage_unavailable_error
from .errors import error_payload, normalize_error_payload
from .docs import (
    OPENAPI_JSON_CONTENT_TYPE,
    SWAGGER_HTML_CONTENT_TYPE,
    SWAGGER_PATHS,
    openapi_json_bytes,
    swagger_ui_html,
)
from .routes import UPLOAD_ROUTE_PATHS, route_for
from .ui import index_html
from . import api_server as api_runtime


PROXY_SKIP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


PROXY_CORE_NAMESPACES = frozenset({"auth", "core", "service"})

def serve_web_proxy(config: AppConfig, host: str, port: int, *, core_url: str) -> None:
    core = urlparse(core_url)
    if core.scheme not in {"http", "https"} or not core.hostname:
        raise ValueError("core_url must be an http(s) URL with host")
    core_port = core.port or (443 if core.scheme == "https" else 80)
    core_base = core.path.rstrip("/")

    class Handler(BaseHTTPRequestHandler):
        def handle_one_request(self) -> None:
            try:
                super().handle_one_request()
            except (ConnectionAbortedError, ConnectionResetError):
                return

        def do_GET(self) -> None:  # noqa: N802
            self._route()

        def do_HEAD(self) -> None:  # noqa: N802
            self._route()

        def do_POST(self) -> None:  # noqa: N802
            self._route()

        def _route(self) -> None:
            path = urlparse(self.path).path
            if path == "/" and self.command in {"GET", "HEAD"}:
                data = index_html().encode("utf-8")
                if self.command == "HEAD":
                    self._head(HTTPStatus.OK, "text/html; charset=utf-8", len(data))
                else:
                    self._html(data)
                return
            if path in SWAGGER_PATHS and self.command in {"GET", "HEAD"}:
                data = swagger_ui_html().encode("utf-8")
                if self.command == "HEAD":
                    self._head(HTTPStatus.OK, SWAGGER_HTML_CONTENT_TYPE, len(data))
                else:
                    self._bytes(data, SWAGGER_HTML_CONTENT_TYPE, cache_control="no-store")
                return
            if path == "/openapi.json" and self.command in {"GET", "HEAD"}:
                try:
                    data = openapi_json_bytes()
                except OSError:
                    self._json({"error": "openapi contract is not available"}, status=HTTPStatus.NOT_FOUND)
                    return
                if self.command == "HEAD":
                    self._head(HTTPStatus.OK, OPENAPI_JSON_CONTENT_TYPE, len(data))
                else:
                    self._bytes(data, OPENAPI_JSON_CONTENT_TYPE, cache_control="no-store")
                return
            if not self._authorize_api(path):
                return
            if path.startswith("/api/web/"):
                self._serve_web_api()
                return
            if self._is_core_proxy_path(self.command, path):
                self._proxy_to_core()
                return
            if path.startswith("/api/"):
                self._api_not_found()
                return
            self._json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

        @staticmethod
        def _is_core_proxy_path(method: str, path: str) -> bool:
            route = route_for(method, path)
            if not route:
                return False
            return route.namespace in PROXY_CORE_NAMESPACES or (
                route.namespace == "openapi" and path != "/openapi.json"
            )

        def _serve_web_api(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            if path == "/api/web/events/stream":
                if self.command == "HEAD":
                    self._head(HTTPStatus.OK, "text/event-stream; charset=utf-8", 0)
                elif self.command == "GET":
                    self._events()
                else:
                    self._json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
                return
            if self.command == "HEAD":
                route = route_for("HEAD", path)
                if route and route.namespace == "web":
                    self._head(HTTPStatus.OK, "application/json; charset=utf-8", 0)
                else:
                    self._head(HTTPStatus.NOT_FOUND, "application/json; charset=utf-8", 0)
                return
            if self.command == "GET":
                route = route_for("GET", path)
                if not route or route.namespace != "web":
                    self._json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
                    return
                try:
                    self._json(api_runtime.web_json_get_payload(config, path, query))
                except KeyError:
                    self._json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
                except Exception as exc:  # noqa: BLE001
                    if is_storage_unavailable_error(exc):
                        self._storage_unavailable()
                        return
                    self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            if self.command == "POST":
                route = route_for("POST", path)
                if not route or route.namespace != "web":
                    self._json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
                    return
                try:
                    payload = self._request_json()
                    response, status = api_runtime.web_json_post_response(config, path, payload)
                except api_runtime.RequestBodyTooLarge as exc:
                    self._json({"error": str(exc)}, status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                except KeyError:
                    self._json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
                except Exception as exc:  # noqa: BLE001
                    if is_storage_unavailable_error(exc):
                        self._storage_unavailable()
                        return
                    self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                else:
                    self._json(response, status=status)
                return
            self._json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

        def _proxy_to_core(self) -> None:
            parsed = urlparse(self.path)
            target = f"{core_base}{parsed.path}"
            if parsed.query:
                target = f"{target}?{parsed.query}"
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                self._json({"error": "invalid content length"}, status=HTTPStatus.BAD_REQUEST)
                return
            limit = self._request_body_limit(parsed.path)
            if length > limit:
                self._json(
                    {"error": "request body is too large"},
                    status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    close_connection=True,
                )
                self._discard_request_body()
                return
            body = self.rfile.read(length) if length > 0 else None
            headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() not in PROXY_SKIP_HEADERS and key.lower() != "host"
            }
            headers["Host"] = self.headers.get("Host") or core.netloc
            headers["X-Forwarded-Host"] = self.headers.get("Host") or ""
            headers["X-Forwarded-Proto"] = "http"
            headers["Connection"] = "close"
            connection_class = http.client.HTTPSConnection if core.scheme == "https" else http.client.HTTPConnection
            connection = connection_class(core.hostname, core_port, timeout=30)
            try:
                connection.request(self.command, target, body=body, headers=headers)
                response = connection.getresponse()
            except OSError as exc:
                connection.close()
                self._json(
                    {"error": "core api is unavailable", "detail": str(exc)},
                    status=HTTPStatus.BAD_GATEWAY,
                )
                return
            try:
                self.send_response(response.status, response.reason)
                for key, value in response.getheaders():
                    if key.lower() not in PROXY_SKIP_HEADERS:
                        self.send_header(key, value)
                self.end_headers()
                if self.command == "HEAD":
                    response.read()
                    return
                reader = getattr(response, "read1", response.read)
                while True:
                    chunk = reader(PROXY_STREAM_CHUNK_BYTES)
                    if not chunk:
                        return
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                return
            finally:
                connection.close()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _html(self, data: bytes) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

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

        def _json(
            self,
            payload: dict[str, Any],
            status: HTTPStatus = HTTPStatus.OK,
            *,
            close_connection: bool = False,
        ) -> None:
            response = normalize_error_payload(payload, status)
            data = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            if close_connection:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()

        def _authorize_api(self, path: str) -> bool:
            if not path.startswith("/api/"):
                return True
            route = route_for(self.command, path)
            if route and not route.auth_required:
                return True
            try:
                require_bearer_token(config.output.state_dir, self.headers.get("Authorization"))
            except Exception as exc:  # noqa: BLE001
                if is_storage_unavailable_error(exc):
                    self._storage_unavailable()
                    return False
                if isinstance(exc, AuthenticationError):
                    self._auth_error(exc)
                    return False
                raise
            return True

        def _storage_unavailable(self) -> None:
            self._json(
                error_payload("storage_unavailable", "Storage is temporarily unavailable."),
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )

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
        def _api_not_found(self) -> None:
            self._json({"error": "not found"}, status=HTTPStatus.NOT_FOUND, close_connection=True)
            self._discard_request_body()

        def _discard_request_body(self) -> None:
            try:
                remaining = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                return
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, PROXY_STREAM_CHUNK_BYTES))
                if not chunk:
                    return
                remaining -= len(chunk)

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
                    for event_name, payload in api_runtime.web_event_changes(config, previous):
                        self._require_stream_authorization(authorization)
                        self._event(event_name, payload)
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
                    if is_storage_unavailable_error(exc):
                        try:
                            self._event(
                                "event-error",
                                {
                                    "error": "storage_unavailable",
                                    "message": "Storage is temporarily unavailable.",
                                },
                            )
                        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                            pass
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

        def _request_json(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError as exc:
                raise ValueError("invalid request body size") from exc
            if length <= 0:
                return {}
            if length > JSON_REQUEST_MAX_BYTES:
                raise api_runtime.RequestBodyTooLarge("request body is too large")
            raw = self.rfile.read(length).decode("utf-8")
            if not raw.strip():
                return {}
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("request body must be a JSON object")
            return parsed

        @staticmethod
        def _request_body_limit(path: str) -> int:
            if path in UPLOAD_ROUTE_PATHS:
                return BACKUP_UPLOAD_MAX_BYTES
            return JSON_REQUEST_MAX_BYTES

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"GP control plane web UI proxy listening on http://{host}:{port}; core={core_url}")
    server.serve_forever()

