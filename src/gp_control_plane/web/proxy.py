from __future__ import annotations

import http.client
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from ..config import AppConfig
from ..resource_budget import BACKUP_UPLOAD_MAX_BYTES, JSON_REQUEST_MAX_BYTES, PROXY_STREAM_CHUNK_BYTES
from .docs import (
    OPENAPI_JSON_CONTENT_TYPE,
    SWAGGER_HTML_CONTENT_TYPE,
    SWAGGER_PATHS,
    openapi_json_bytes,
    swagger_ui_html,
)
from .routes import UPLOAD_ROUTE_PATHS
from .ui import index_html


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
            except ConnectionResetError:
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
            if path == "/openapi.json" and self.command in {"GET", "HEAD"}:
                self._openapi_json()
                return
            if path in SWAGGER_PATHS and self.command in {"GET", "HEAD"}:
                data = swagger_ui_html().encode("utf-8")
                if self.command == "HEAD":
                    self._head(HTTPStatus.OK, SWAGGER_HTML_CONTENT_TYPE, len(data))
                else:
                    self._bytes(data, SWAGGER_HTML_CONTENT_TYPE, cache_control="no-store")
                return
            if path.startswith("/api/"):
                self._proxy_to_core()
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
                self._json({"error": "request body is too large"}, status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
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
            except (BrokenPipeError, ConnectionResetError):
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

        def _openapi_json(self) -> None:
            try:
                data = openapi_json_bytes()
            except OSError as exc:
                self._json({"error": "openapi contract is not available", "detail": str(exc)}, status=HTTPStatus.NOT_FOUND)
                return
            if self.command == "HEAD":
                self._head(HTTPStatus.OK, OPENAPI_JSON_CONTENT_TYPE, len(data))
            else:
                self._bytes(data, OPENAPI_JSON_CONTENT_TYPE, cache_control="no-store")

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

        def _json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        @staticmethod
        def _request_body_limit(path: str) -> int:
            if path in UPLOAD_ROUTE_PATHS:
                return BACKUP_UPLOAD_MAX_BYTES
            return JSON_REQUEST_MAX_BYTES

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"GP control plane web UI proxy listening on http://{host}:{port}; core={core_url}")
    server.serve_forever()

