"""api_server HTTP handler primitives — moved from api_server.py (package split)."""

from __future__ import annotations

import json
import mimetypes
from http import HTTPStatus
from pathlib import Path
from typing import Any

from gp_control_plane import core_api
from gp_control_plane.auth import AuthenticationError, require_bearer_token
from gp_control_plane.backups import snapshot_file_path
from gp_control_plane.resource_budget import (
    BACKUP_STREAM_CHUNK_BYTES,
    BACKUP_UPLOAD_MAX_BYTES,
    JSON_REQUEST_MAX_BYTES,
)
from gp_control_plane.storage import (
    is_storage_unavailable_error as _is_storage_unavailable_error,
)
from gp_control_plane.web.api_server._errors import RequestBodyTooLarge
from gp_control_plane.web.api_server._helpers import _multipart_file_bytes, _query_one
from gp_control_plane.web.api_server._pages import index_html
from gp_control_plane.web.docs import (
    OPENAPI_JSON_CONTENT_TYPE,
    SWAGGER_HTML_CONTENT_TYPE,
    openapi_json_bytes,
    swagger_ui_html,
)
from gp_control_plane.web.errors import error_payload, normalize_error_payload
from gp_control_plane.web.routes import route_for

MAX_BACKUP_UPLOAD_BYTES = BACKUP_UPLOAD_MAX_BYTES
MAX_JSON_REQUEST_BYTES = JSON_REQUEST_MAX_BYTES
NDJSON_CONTENT_TYPE = "application/x-ndjson; charset=utf-8"


class HttpMixin:
    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError):
            return
        except Exception as exc:
            if _is_storage_unavailable_error(exc):
                self._storage_unavailable()
                return
            raise

    def log_message(self, _format: str, *args: Any) -> None:
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
            data = openapi_json_bytes(core_only=not self.ui_enabled)
        except OSError:
            self._json({"error": "openapi contract is not available"}, status=HTTPStatus.NOT_FOUND)
            return
        self._bytes(data, OPENAPI_JSON_CONTENT_TYPE, cache_control="no-store")

    def _head_openapi_json(self) -> None:
        try:
            data = openapi_json_bytes(core_only=not self.ui_enabled)
        except OSError:
            self._head(HTTPStatus.NOT_FOUND, "application/json; charset=utf-8", 0)
            return
        self._head(HTTPStatus.OK, OPENAPI_JSON_CONTENT_TYPE, len(data))

    def _require_stream_authorization(self, authorization: str | None) -> None:
        require_bearer_token(self.config.output.state_dir, authorization)

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
            require_bearer_token(self.config.output.state_dir, self.headers.get("Authorization"))
        except Exception as exc:
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

    def _download_backup(self, query: dict[str, list[str]]) -> None:
        snapshot_id = _query_one(query, "snapshot")
        file_name = _query_one(query, "file") or "archive"
        try:
            path = snapshot_file_path(self.config.output.state_dir, snapshot_id, file_name)
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

    def _stream_strategy_candidates_export(self, query: dict[str, list[str]]) -> None:
        try:
            iterator = core_api.iter_strategy_candidates_export_lines(self.config, query)
            first_line = next(iterator)
        except ValueError as exc:
            self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        except StopIteration:
            first_line = None
        except Exception as exc:
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
        except Exception as exc:
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
