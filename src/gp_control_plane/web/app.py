from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import mimetypes
import os
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .. import __version__
from ..backups import (
    create_snapshot_if_idle,
    delete_snapshot_if_idle,
    import_snapshot_archive,
    list_snapshots,
    restore_snapshot_if_idle,
    restore_snapshot_preview,
    snapshot_file_path,
)
from ..config import AppConfig
from ..diagnostics import diagnostics_payload
from ..domain_sources import (
    builtin_preset_sources,
    fetch_v2fly_category_local,
    import_v2fly_preset,
    list_v2fly_categories_local,
    preview_v2fly_preset,
)
from ..jobs import JobRunner
from ..release_update import queue_release_update, release_update_plan, release_update_status
from ..releases import release_channel_info
from ..state import now_iso, read_state, write_state
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
)
from ..strategy_finder import (
    candidate_storage_version,
    close_stale_running_runs,
    domain_sets,
    latest_log_tail,
    read_candidate_domain_index,
    read_candidate_page,
    read_runs,
    read_runs_page,
    run_multi_domain_discovery,
    run_standard_discovery,
    stop_active_blockcheck_runtime,
)
from ..zapret2 import check_install_cached


MAX_BACKUP_UPLOAD_BYTES = 512 * 1024 * 1024
AUTH_OFF_VALUES = {"0", "false", "no", "off", "disabled"}
AUTH_ON_VALUES = {"1", "true", "yes", "on", "enabled", "required"}
AUTH_TOKEN_HEADER = "X-GP-Token"
AUTH_QUERY_PARAM = "gp_token"
SENSITIVE_GET_PATHS = {
    "/api/backups/download",
    "/api/backups/restore-preview",
    "/api/releases/update-plan",
}
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
OPENAPI_JSON_CONTENT_TYPE = "application/json; charset=utf-8"
SWAGGER_HTML_CONTENT_TYPE = "text/html; charset=utf-8"
SWAGGER_PATHS = {"/swagger", "/swagger/"}


def openapi_json_path() -> Path:
    return Path(__file__).resolve().parents[3] / "openapi.json"


def openapi_json_bytes() -> bytes:
    return openapi_json_path().read_bytes()


def swagger_ui_html() -> str:
    return """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>GP Control Plane API</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
  <style>
    body { margin: 0; background: #f8fafc; }
    .topbar { display: none; }
    .swagger-ui .info { margin: 24px 0; }
    .swagger-ui .scheme-container { box-shadow: none; border: 1px solid #e2e8f0; }
    .swagger-ui .wrapper { max-width: 1280px; }
    .offline {
      margin: 24px;
      padding: 16px 18px;
      border: 1px solid #fecaca;
      border-radius: 8px;
      background: #fff1f2;
      color: #7f1d1d;
      font: 14px/1.5 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .offline a { color: #991b1b; font-weight: 600; }
  </style>
</head>
<body>
  <div id="swagger-ui"></div>
  <noscript>
    <div class="offline">Для Swagger UI нужен JavaScript. Raw OpenAPI доступен по адресу <a href="/openapi.json">/openapi.json</a>.</div>
  </noscript>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-standalone-preset.js"></script>
  <script>
    window.addEventListener('load', () => {
      if (!window.SwaggerUIBundle || !window.SwaggerUIStandalonePreset) {
        document.getElementById('swagger-ui').innerHTML =
          '<div class="offline">Не удалось загрузить Swagger UI. Raw OpenAPI доступен по адресу <a href="/openapi.json">/openapi.json</a>.</div>';
        return;
      }
      window.ui = SwaggerUIBundle({
        url: '/openapi.json',
        dom_id: '#swagger-ui',
        deepLinking: true,
        displayRequestDuration: true,
        docExpansion: 'list',
        defaultModelsExpandDepth: 1,
        presets: [
          SwaggerUIBundle.presets.apis,
          SwaggerUIStandalonePreset
        ],
        layout: 'BaseLayout'
      });
    });
  </script>
</body>
</html>
"""


def web_auth_config() -> dict[str, Any]:
    token = str(os.environ.get("GP_WEB_TOKEN") or "").strip()
    raw_mode = str(os.environ.get("GP_WEB_AUTH") or "auto").strip().lower()
    if raw_mode in AUTH_OFF_VALUES:
        enabled = False
        mode = "off"
    elif raw_mode in AUTH_ON_VALUES:
        enabled = True
        mode = "on"
    else:
        enabled = bool(token)
        mode = "auto"
    configured = bool(token)
    if not enabled:
        status = "disabled"
    elif configured:
        status = "enabled"
    else:
        status = "missing-token"
    return {
        "enabled": enabled,
        "configured": configured,
        "mode": mode,
        "status": status,
        "token": token,
        "header": AUTH_TOKEN_HEADER,
        "query_param": AUTH_QUERY_PARAM,
    }


def _web_auth_public_payload(web_auth: dict[str, Any] | None = None, *, include_token: bool = False) -> dict[str, Any]:
    auth = web_auth or web_auth_config()
    payload = {
        "enabled": bool(auth.get("enabled")),
        "configured": bool(auth.get("configured")),
        "mode": str(auth.get("mode") or "auto"),
        "status": str(auth.get("status") or "disabled"),
        "header": AUTH_TOKEN_HEADER,
        "query_param": AUTH_QUERY_PARAM,
    }
    if include_token and payload["enabled"] and payload["configured"]:
        payload["token"] = str(auth.get("token") or "")
    else:
        payload["token"] = ""
    return payload


def serve(config: AppConfig, host: str, port: int, *, ui_enabled: bool = True) -> None:
    _clear_stale_current_job(config)
    close_stale_running_runs(config.output.state_dir)
    runner = JobRunner(config.output.state_dir, on_idle=lambda: create_snapshot_if_idle(config.output.state_dir))
    web_auth = web_auth_config()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed_url = urlparse(self.path)
            path = parsed_url.path
            query = parse_qs(parsed_url.query)
            if path in SENSITIVE_GET_PATHS and not self._authorize_web_request(path, query, web_auth, check_origin=False):
                return
            if path == "/":
                if ui_enabled:
                    self._html(web_auth)
                else:
                    self._json({"error": "web ui is disabled in core mode"}, status=HTTPStatus.NOT_FOUND)
            elif path == "/openapi.json":
                self._openapi_json()
            elif path in SWAGGER_PATHS:
                self._swagger()
            elif path == "/api/status":
                self._json(status_payload(config, web_auth=web_auth))
            elif path == "/api/events":
                self._events()
            elif path == "/api/settings":
                self._json({"settings": read_settings(config)})
            elif path == "/api/run-preferences":
                self._json({"run_preferences": read_run_preferences(config)})
            elif path == "/api/releases":
                self._json(_release_info_payload(config, query))
            elif path == "/api/releases/update-plan":
                self._json(_release_update_plan_payload(config, query))
            elif path == "/api/diagnostics":
                self._json(diagnostics_payload(config.output.state_dir))
            elif path == "/api/strategy-finder/domains":
                self._json(domain_sets())
            elif path == "/api/strategy-finder/candidate-domains":
                self._json(_candidate_domain_index_payload(config, query))
            elif path == "/api/strategy-finder/candidates":
                self._json(_candidate_page_payload(config, query))
            elif path == "/api/strategy-finder/runs":
                self._json(_runs_page_payload(config, query))
            elif path == "/api/strategy-finder/latest-log":
                self._json(_latest_log_payload(config, query))
            elif path == "/api/backups":
                self._json(list_snapshots(config.output.state_dir))
            elif path == "/api/backups/restore-preview":
                snapshot_id = _query_one(query, "snapshot")
                try:
                    self._json(restore_snapshot_preview(config.output.state_dir, snapshot_id))
                except Exception as exc:  # noqa: BLE001
                    self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            elif path == "/api/presets":
                self._json(_presets_payload(config, query))
            elif path == "/api/presets/domains":
                self._json(_preset_domains_payload(config, query))
            elif path == "/api/domain-sources":
                self._json({"builtin": builtin_preset_sources()})
            elif path == "/api/domain-sources/v2fly/categories":
                self._json(_v2fly_categories_payload(config, query))
            elif path == "/api/backups/download":
                self._download_backup(config, query)
            else:
                self._not_found()

        def do_HEAD(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/":
                if ui_enabled:
                    data = index_html(web_auth).encode("utf-8")
                    self._head(HTTPStatus.OK, "text/html; charset=utf-8", len(data))
                else:
                    self._head(HTTPStatus.NOT_FOUND, "application/json; charset=utf-8", 0)
            elif path == "/openapi.json":
                self._head_openapi_json()
            elif path in SWAGGER_PATHS:
                data = swagger_ui_html().encode("utf-8")
                self._head(HTTPStatus.OK, SWAGGER_HTML_CONTENT_TYPE, len(data))
            elif path == "/api/events":
                self._head(HTTPStatus.OK, "text/event-stream; charset=utf-8", 0)
            elif path in {
                "/api/status",
                "/api/settings",
                "/api/run-preferences",
                "/api/releases",
                "/api/releases/update-plan",
                "/api/releases/update",
                "/api/diagnostics",
                "/api/strategy-finder/domains",
                "/api/strategy-finder/candidate-domains",
                "/api/strategy-finder/candidates",
                "/api/strategy-finder/runs",
                "/api/strategy-finder/latest-log",
                "/api/backups",
                "/api/backups/restore-preview",
                "/api/presets",
                "/api/presets/domains",
                "/api/presets/save",
                "/api/presets/delete",
                "/api/presets/delete-users-lists",
                "/api/domain-sources",
                "/api/domain-sources/v2fly/categories",
            }:
                self._head(HTTPStatus.OK, "application/json; charset=utf-8", 0)
            else:
                self._head(HTTPStatus.NOT_FOUND, "application/json; charset=utf-8", 0)

        def do_POST(self) -> None:  # noqa: N802
            parsed_url = urlparse(self.path)
            path = parsed_url.path
            query = parse_qs(parsed_url.query)
            if not self._authorize_web_request(path, query, web_auth, check_origin=True):
                return
            if path == "/api/backups/upload":
                try:
                    self._json(import_snapshot_archive(config.output.state_dir, self._request_upload_bytes()), status=HTTPStatus.ACCEPTED)
                except Exception as exc:  # noqa: BLE001
                    self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                payload = self._request_json()
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            if path == "/api/jobs/stop-current":
                try:
                    job = runner.cancel_active()
                except Exception as exc:  # noqa: BLE001
                    self._json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
                    return
                self._json({"job": job}, status=HTTPStatus.ACCEPTED)
                return
            if path == "/api/backups/create":
                try:
                    self._json(create_snapshot_if_idle(config.output.state_dir), status=HTTPStatus.ACCEPTED)
                except Exception as exc:  # noqa: BLE001
                    self._json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
                return
            if path == "/api/backups/restore":
                try:
                    snapshot_id = str(payload.get("snapshot") or "").strip()
                    if not snapshot_id:
                        raise ValueError("snapshot is required")
                    self._json(restore_snapshot_if_idle(config.output.state_dir, snapshot_id), status=HTTPStatus.ACCEPTED)
                except Exception as exc:  # noqa: BLE001
                    self._json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
                return
            if path == "/api/backups/delete":
                try:
                    snapshot_id = str(payload.get("snapshot") or "").strip()
                    if not snapshot_id:
                        raise ValueError("snapshot is required")
                    self._json(delete_snapshot_if_idle(config.output.state_dir, snapshot_id), status=HTTPStatus.ACCEPTED)
                except Exception as exc:  # noqa: BLE001
                    self._json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
                return
            if path == "/api/presets":
                saved = save_custom_presets(config.output.state_dir, payload.get("custom") or payload, now_iso())
                self._json({"custom": saved, "metadata": read_custom_preset_index(config.output.state_dir)})
                return
            if path == "/api/presets/save":
                try:
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
                    self._json(_presets_payload(config, {"include_domains": ["1"]}))
                except Exception as exc:  # noqa: BLE001
                    self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            if path == "/api/presets/delete-users-lists":
                try:
                    names = _payload_string_list(payload, "names")
                    if not names and payload.get("name"):
                        names = [str(payload.get("name") or "")]
                    metadata = delete_user_presets(
                        config.output.state_dir,
                        scope=str(payload.get("scope") or ""),
                        names=names,
                    )
                    self._json(_presets_payload(config, {"include_domains": ["1"]}) | {"metadata": metadata})
                except Exception as exc:  # noqa: BLE001
                    self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            if path == "/api/presets/delete":
                try:
                    metadata = delete_custom_preset(
                        config.output.state_dir,
                        scope=str(payload.get("scope") or ""),
                        name=str(payload.get("name") or ""),
                    )
                    self._json(_presets_payload(config, {"include_domains": ["1"]}) | {"metadata": metadata})
                except Exception as exc:  # noqa: BLE001
                    self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            if path == "/api/presets/domain-enabled":
                try:
                    result = set_preset_domain_enabled(
                        config.output.state_dir,
                        scope=str(payload.get("scope") or ""),
                        name=str(payload.get("name") or ""),
                        domain=str(payload.get("domain") or ""),
                        enabled=bool(payload.get("enabled")),
                        updated_at=now_iso(),
                        kind=str(payload.get("kind") or "user"),
                    )
                    self._json(_presets_payload(config, {"include_domains": ["1"]}) | {"domain": result})
                except Exception as exc:  # noqa: BLE001
                    self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            if path == "/api/settings":
                self._json({"settings": save_settings(config, payload.get("settings") or payload)})
                return
            if path == "/api/run-preferences":
                self._json({"run_preferences": save_run_preferences(config, payload.get("run_preferences") or payload)})
                return
            if path == "/api/releases/update":
                try:
                    self._json(_queue_release_update_payload(config, payload), status=HTTPStatus.ACCEPTED)
                except Exception as exc:  # noqa: BLE001
                    self._json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
                return
            if path == "/api/domain-sources/v2fly/preview":
                try:
                    self._json(_v2fly_preview_payload(config, payload))
                except Exception as exc:  # noqa: BLE001
                    self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            if path == "/api/domain-sources/v2fly/import":
                try:
                    self._json(_v2fly_import_payload(config, payload), status=HTTPStatus.ACCEPTED)
                except Exception as exc:  # noqa: BLE001
                    self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            jobs: dict[str, Any] = {
                "/api/jobs/zapret-standard-discovery": (
                    "zapret-standard-discovery",
                    lambda stop: _job_zapret_standard_discovery(config, payload, stop),
                    stop_active_blockcheck_runtime,
                ),
                "/api/jobs/zapret-multi-domain-discovery": (
                    "zapret-multi-domain-discovery",
                    lambda stop: _job_zapret_multi_domain_discovery(config, payload, stop),
                    stop_active_blockcheck_runtime,
                ),
            }
            if path not in jobs:
                self._not_found()
                return
            name, func, cancel_hook = jobs[path]
            try:
                job = runner.start(name, func, cancel_hook=cancel_hook)
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
                return
            self._json({"job": job.__dict__}, status=HTTPStatus.ACCEPTED)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _html(self, web_auth: dict[str, Any]) -> None:
            data = index_html(web_auth).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            if web_auth.get("enabled") and web_auth.get("configured"):
                self.send_header("Set-Cookie", f"{AUTH_QUERY_PARAM}={web_auth.get('token')}; Path=/; SameSite=Strict")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _swagger(self) -> None:
            self._bytes(swagger_ui_html().encode("utf-8"), SWAGGER_HTML_CONTENT_TYPE, cache_control="no-store")

        def _openapi_json(self) -> None:
            try:
                data = openapi_json_bytes()
            except OSError as exc:
                self._json({"error": "openapi contract is not available", "detail": str(exc)}, status=HTTPStatus.NOT_FOUND)
                return
            self._bytes(data, OPENAPI_JSON_CONTENT_TYPE, cache_control="no-store")

        def _events(self) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            previous: dict[str, str] = {}
            heartbeat_at = 0.0
            while True:
                try:
                    for event_name, payload in _event_payloads(config, web_auth=web_auth).items():
                        try:
                            fingerprint = _event_fingerprint(payload)
                            if previous.get(event_name) == fingerprint:
                                continue
                            previous[event_name] = fingerprint
                            self._event(event_name, payload)
                        except (TypeError, ValueError) as exc:
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
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        heartbeat_at = now
                    time.sleep(1)
                except (BrokenPipeError, ConnectionResetError):
                    return
                except Exception as exc:  # noqa: BLE001
                    try:
                        self._event("event-error", {"error": "event-loop", "message": str(exc)})
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    time.sleep(1)

        def _event(self, event_name: str, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            self.wfile.write(f"event: {event_name}\n".encode("utf-8"))
            self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
            self.wfile.flush()

        def _json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _request_upload_bytes(self) -> bytes:
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError as exc:
                raise ValueError("invalid upload size") from exc
            if length <= 0:
                raise ValueError("empty backup upload")
            if length > MAX_BACKUP_UPLOAD_BYTES:
                raise ValueError("backup upload is too large")
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
            except Exception:
                self._not_found()
                return
            self._file(path, download_name=path.name)

        def _file(self, path: Path, download_name: str) -> None:
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(path.stat().st_size))
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
            self.end_headers()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    self.wfile.write(chunk)

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
                size = openapi_json_path().stat().st_size
            except OSError:
                self._head(HTTPStatus.NOT_FOUND, "application/json; charset=utf-8", 0)
                return
            self._head(HTTPStatus.OK, OPENAPI_JSON_CONTENT_TYPE, size)

        def _not_found(self) -> None:
            self._json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

        def _authorize_web_request(
            self,
            path: str,
            query: dict[str, list[str]],
            web_auth: dict[str, Any],
            *,
            check_origin: bool,
        ) -> bool:
            if not web_auth.get("enabled"):
                return True
            if not web_auth.get("configured"):
                self._json({"error": "web auth token is not configured"}, status=HTTPStatus.SERVICE_UNAVAILABLE)
                return False
            if check_origin and not self._origin_allowed():
                self._json({"error": "origin is not allowed"}, status=HTTPStatus.FORBIDDEN)
                return False
            expected = str(web_auth.get("token") or "")
            supplied = self.headers.get(AUTH_TOKEN_HEADER, "")
            if not supplied:
                authorization = self.headers.get("Authorization", "")
                if authorization.lower().startswith("bearer "):
                    supplied = authorization[7:].strip()
            if not supplied:
                supplied = _query_one(query, AUTH_QUERY_PARAM)
            if not supplied or not hmac.compare_digest(supplied, expected):
                self._json({"error": "web auth token is required"}, status=HTTPStatus.UNAUTHORIZED)
                return False
            return True

        def _origin_allowed(self) -> bool:
            origin = self.headers.get("Origin", "")
            if not origin:
                return True
            host = self.headers.get("Host", "").lower()
            if not host:
                return False
            parsed = urlparse(origin)
            return parsed.netloc.lower() == host

        def _request_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0:
                return {}
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
    serve(config, host=host, port=port, ui_enabled=False)


def serve_web_proxy(config: AppConfig, host: str, port: int, *, core_url: str) -> None:
    web_auth = web_auth_config()
    core = urlparse(core_url)
    if core.scheme not in {"http", "https"} or not core.hostname:
        raise ValueError("core_url must be an http(s) URL with host")
    core_port = core.port or (443 if core.scheme == "https" else 80)
    core_base = core.path.rstrip("/")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self._route()

        def do_HEAD(self) -> None:  # noqa: N802
            self._route()

        def do_POST(self) -> None:  # noqa: N802
            self._route()

        def _route(self) -> None:
            path = urlparse(self.path).path
            if path == "/" and self.command in {"GET", "HEAD"}:
                data = index_html(web_auth).encode("utf-8")
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
                    chunk = reader(64 * 1024)
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
            if web_auth.get("enabled") and web_auth.get("configured"):
                self.send_header("Set-Cookie", f"{AUTH_QUERY_PARAM}={web_auth.get('token')}; Path=/; SameSite=Strict")
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

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"GP control plane web UI proxy listening on http://{host}:{port}; core={core_url}")
    server.serve_forever()


def index_html(web_auth: dict[str, Any] | None = None) -> str:
    html = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GP Strategy Finder</title>
<style>
:root {
  color-scheme: dark;
  font-family: Inter, "Segoe UI", Arial, sans-serif;
  background: #161c27;
  color: #e6edf3;
  --surface: #1b2434;
  --surface-soft: #202b3d;
  --surface-code: #0f1623;
  --surface-code-gutter: #151d2b;
  --line: rgba(255, 255, 255, .08);
  --line-strong: #3a4658;
  --text-soft: #949b9f;
  --blue: #0097dc;
  --blue-strong: #5cc8ff;
  --green: #22c55e;
  --green-soft: rgba(34, 197, 94, .14);
  --amber: #f59e0b;
  --amber-soft: rgba(245, 158, 11, .14);
  --red: #ef4444;
  --red-soft: rgba(239, 68, 68, .14);
  --code-text: #d7e0ea;
  --code-muted: #6f7a89;
}
* { box-sizing: border-box; }
body { margin: 0; min-width: 320px; }
.shell { min-height: 100vh; }
.topbar { background: var(--surface); border-bottom: 1px solid var(--line); }
.topbar-inner {
  max-width: 1240px;
  margin: 0 auto;
  padding: 18px 24px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}
.brand { display: grid; gap: 4px; min-width: 0; }
h1 { font-size: 24px; line-height: 1.2; margin: 0; letter-spacing: 0; }
.subtitle { color: var(--text-soft); font-size: 13px; }
.topbar-version {
  flex: 0 0 auto;
  border: 1px solid var(--line-strong);
  border-radius: 999px;
  padding: 5px 10px;
  color: var(--text);
  background: var(--surface);
  font-size: 12px;
  font-weight: 700;
}
.topbar-badges { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
.topbar-auth.off { border-color: rgba(245, 158, 11, .65); color: #ffd166; }
.topbar-auth.on { border-color: rgba(34, 197, 94, .55); color: #9ff0bd; }
.main {
  max-width: 1240px;
  margin: 0 auto;
  padding: 20px 24px 32px;
  display: grid;
  gap: 16px;
}
.status-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}
.metric {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 14px;
  min-height: 94px;
  display: grid;
  align-content: space-between;
  gap: 8px;
}
.metric-label { color: var(--text-soft); font-size: 12px; text-transform: uppercase; }
.metric-value { font-size: 20px; font-weight: 700; line-height: 1.25; overflow-wrap: anywhere; }
.metric-note { color: var(--text-soft); font-size: 12px; overflow-wrap: anywhere; }
.metric-button {
  width: 100%;
  text-align: left;
  background: var(--surface);
  border-color: var(--line);
  color: inherit;
  white-space: normal;
  cursor: pointer;
}
.metric-button:hover {
  background: var(--surface-soft);
  border-color: var(--line-strong);
}
.metric-status-running,
.metric-status-queued,
.metric-status-stopping {
  border-color: rgba(255, 174, 66, .65);
}
.metric-status-success {
  border-color: rgba(83, 221, 133, .65);
}
.metric-status-stopped,
.metric-status-timeout {
  border-color: rgba(255, 174, 66, .65);
}
.metric-status-failed {
  border-color: rgba(255, 76, 86, .75);
}
.status-checks {
  display: grid;
  gap: 4px;
}
.status-check {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  color: var(--text-soft);
}
.status-check::before {
  content: "";
  width: 12px;
  height: 12px;
  border-radius: 3px;
  border: 1px solid var(--line-strong);
  background: var(--surface-code);
  flex: 0 0 auto;
}
.status-check.ok::before {
  border-color: rgba(83, 221, 133, .8);
  background: rgba(83, 221, 133, .18);
  box-shadow: inset 0 0 0 2px var(--surface);
}
.status-check.fail::before {
  border-color: rgba(255, 76, 86, .8);
  background: rgba(255, 76, 86, .16);
}
.status-check-body {
  display: grid;
  gap: 2px;
  min-width: 0;
}
.status-check-label {
  color: var(--text);
  font-size: 12px;
  font-weight: 700;
}
.status-check-message {
  font-size: 11px;
  line-height: 1.25;
  overflow-wrap: anywhere;
}
.stderr-diagnostics {
  display: grid;
  gap: 8px;
  margin: 12px 0;
}
.stderr-diagnostic {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 10px 12px;
  background: var(--surface-soft);
  color: var(--text-soft);
}
.stderr-diagnostic.warn {
  border-color: #eed09a;
  background: var(--amber-soft);
  color: var(--amber);
}
.stderr-diagnostic-title {
  font-weight: 700;
  color: var(--text);
  margin-bottom: 4px;
}
.stderr-diagnostic.warn .stderr-diagnostic-title {
  color: var(--amber);
}
.layout {
  display: grid;
  grid-template-columns: minmax(0, 460px) minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}
.finder-layout {
  grid-template-columns: minmax(0, 1fr);
}
.stack { display: grid; gap: 16px; min-width: 0; }
.panel {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 16px;
  min-width: 0;
}
.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}
h2 { font-size: 16px; line-height: 1.3; margin: 0; letter-spacing: 0; }
.form-grid { display: grid; gap: 10px; }
.field { display: grid; gap: 6px; min-width: 0; }
[hidden] { display: none !important; }
label { color: var(--text-soft); font-size: 12px; font-weight: 600; }
input, textarea, select {
  width: 100%;
  min-width: 0;
  border: 1px solid var(--line-strong);
  border-radius: 6px;
  padding: 9px 10px;
  background: var(--surface-code);
  color: #e6edf3;
  font-size: 14px;
}
input { min-height: 38px; }
select { min-height: 38px; }
textarea { min-height: 118px; resize: vertical; line-height: 1.45; }
input:focus, textarea:focus, select:focus {
  outline: 2px solid rgba(0, 151, 220, .55);
  border-color: var(--blue);
}
.checkbox-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 38px;
}
.checkbox-row input { width: 18px; min-height: 18px; }
button {
  min-height: 38px;
  min-width: 0;
  border: 1px solid var(--blue);
  background: var(--blue);
  color: #ffffff;
  border-radius: 6px;
  padding: 8px 12px;
  font-size: 14px;
  line-height: 1.25;
  font-weight: 600;
  cursor: pointer;
  white-space: normal;
  overflow-wrap: anywhere;
}
button:hover { background: var(--blue-strong); border-color: var(--blue-strong); }
button:focus-visible,
a:focus-visible,
summary:focus-visible,
label.file-button:focus-visible {
  outline: 3px solid rgba(120, 211, 255, .78);
  outline-offset: 2px;
}
button.secondary { background: var(--surface-soft); color: var(--blue-strong); }
button.secondary:hover { background: #243149; }
a.button-link {
  min-height: 44px;
  border: 1px solid var(--blue);
  background: var(--blue);
  color: #ffffff;
  border-radius: 6px;
  padding: 8px 12px;
  font-size: 14px;
  line-height: 1.25;
  font-weight: 600;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  text-align: center;
  text-decoration: none;
}
a.button-link.secondary { background: var(--surface-soft); color: var(--blue-strong); }
a.button-link:hover { background: var(--blue-strong); border-color: var(--blue-strong); }
a.button-link.secondary:hover { background: #243149; }
label.file-button {
  min-height: 44px;
  border: 1px solid var(--blue);
  background: var(--surface-soft);
  color: var(--blue-strong);
  border-radius: 6px;
  padding: 8px 12px;
  font-size: 14px;
  line-height: 1.25;
  font-weight: 600;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  text-align: center;
}
label.file-button:hover { background: #243149; }
button.danger { border-color: var(--red); background: var(--red); color: #ffffff; }
button.danger:hover { border-color: #8f1d14; background: #8f1d14; }
button.secondary.danger { background: var(--surface-soft); color: var(--red); }
button.secondary.danger:hover { background: var(--red-soft); }
button:disabled { opacity: .55; cursor: default; }
.button-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 8px;
  align-items: stretch;
}
.button-row button {
  min-height: 44px;
}
.run-actions button {
  min-height: 54px;
}
.tooltip-button {
  position: relative;
}
.tooltip-button[data-tooltip]:hover::after,
.tooltip-button[data-tooltip]:focus-visible::after {
  content: attr(data-tooltip);
  position: absolute;
  z-index: 40;
  left: 50%;
  bottom: calc(100% + 10px);
  transform: translateX(-50%);
  width: min(340px, 90vw);
  padding: 10px 12px;
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  background: var(--surface-code);
  color: var(--text);
  box-shadow: 0 16px 34px rgba(0, 0, 0, .28);
  white-space: normal;
  text-align: left;
  font-size: 12px;
  line-height: 1.35;
}
.tooltip-button[data-tooltip]:hover::before,
.tooltip-button[data-tooltip]:focus-visible::before {
  content: "";
  position: absolute;
  z-index: 41;
  left: 50%;
  bottom: calc(100% + 4px);
  transform: translateX(-50%);
  border: 6px solid transparent;
  border-top-color: var(--surface-code);
}
.fill-row { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
.preset-panel,
.common-filter-panel {
  display: grid;
  gap: 10px;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 12px;
  background: var(--surface-soft);
}
details.preset-panel > summary {
  position: relative;
  cursor: pointer;
  list-style: none;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding-right: 118px;
}
details.preset-panel > summary::-webkit-details-marker {
  display: none;
}
details.preset-panel > summary::before {
  content: "";
  position: absolute;
  right: 92px;
  top: 50%;
  width: 8px;
  height: 8px;
  border-right: 2px solid var(--blue-strong);
  border-bottom: 2px solid var(--blue-strong);
  transform: translateY(-65%) rotate(45deg);
}
details.preset-panel[open] > summary::before {
  transform: translateY(-35%) rotate(225deg);
}
details.preset-panel > summary::after {
  content: "Раскрыть";
  position: absolute;
  right: 12px;
  top: 50%;
  transform: translateY(-50%);
  color: var(--blue-strong);
  font-size: 12px;
  font-weight: 800;
}
details.preset-panel[open] > summary::after {
  content: "Свернуть";
}
details.preset-panel > summary:hover,
details.preset-panel > summary:focus-visible {
  border-color: var(--blue-strong);
  background: var(--surface);
}
.time-limit-panel {
  display: grid;
  gap: 8px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
  padding: 10px;
}
.time-limit-title {
  color: var(--text);
  font-size: 13px;
  font-weight: 800;
}
.time-limit-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(140px, 180px);
  gap: 10px;
  align-items: end;
}
.time-limit-panel.disabled {
  background: var(--surface-soft);
}
.time-limit-field input:disabled {
  opacity: .65;
  cursor: not-allowed;
}
.common-filter-panel[hidden] { display: none; }
.strategy-family-list {
  display: grid;
  gap: 8px;
}
.strategy-family {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface-code);
  padding: 8px;
}
.strategy-family-summary {
  display: grid;
  gap: 6px;
  cursor: pointer;
}
.strategy-family-head {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
.strategy-family-reason {
  color: var(--text-soft);
  font-size: 12px;
  line-height: 1.35;
}
.strategy-family .code-editor {
  margin-top: 8px;
}
.preset-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr);
  gap: 8px;
}
.common-filter-panel .preset-grid {
  grid-template-columns: 1fr;
}
.protocol-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px 10px;
}
.preset-actions,
.domain-picker-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 8px;
}
.domain-picker-row { grid-template-columns: minmax(0, 1fr) auto; }
.common-domain-picker {
  position: relative;
}
.common-domain-picker input {
  width: 100%;
}
.common-domain-suggestions {
  position: absolute;
  z-index: 20;
  left: 0;
  right: 0;
  top: calc(100% + 6px);
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  background: var(--surface-code);
  box-shadow: 0 16px 34px rgba(0, 0, 0, .28);
  overflow: hidden;
}
.common-domain-suggestions[hidden] {
  display: none;
}
.domain-suggestion {
  display: block;
  width: 100%;
  min-height: 0;
  padding: 9px 10px;
  border: 0;
  border-bottom: 1px solid var(--line);
  border-radius: 0;
  background: transparent;
  color: var(--text);
  font-family: var(--mono);
  font-size: 13px;
  text-align: left;
}
.domain-suggestion:last-child {
  border-bottom: 0;
}
.domain-suggestion:hover,
.domain-suggestion:focus {
  background: rgba(0, 151, 220, .18);
  color: #ffffff;
}
.domain-suggestion-empty {
  padding: 9px 10px;
  color: var(--text-soft);
  font-size: 13px;
}
.source-preview {
  display: grid;
  gap: 6px;
  padding: 10px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--surface-code);
  color: var(--text-soft);
  font-size: 13px;
}
.source-preview strong { color: #e6edf3; }
.settings-stack {
  display: grid;
  gap: 12px;
}
.setting-note {
  color: var(--text-soft);
  font-size: 12px;
  line-height: 1.4;
  margin-top: -2px;
}
.preset-grid > .setting-note {
  grid-column: 1 / -1;
}
.segmented-control {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}
.segment-option {
  min-height: 46px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  border: 1px solid var(--line-strong);
  border-radius: 6px;
  background: var(--surface-code);
  color: var(--text-soft);
  opacity: .62;
  font-size: 13px;
  font-weight: 700;
  text-align: center;
  cursor: pointer;
}
.segment-option input {
  width: auto;
  min-width: 0;
  accent-color: var(--blue);
}
.segment-option:has(input:checked) {
  border-color: var(--blue);
  background: var(--blue);
  color: #ffffff;
  opacity: 1;
  box-shadow: 0 0 0 1px rgba(120, 211, 255, .35) inset;
}
.settings-card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 8px;
}
.settings-card {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface-code);
  padding: 10px;
  display: grid;
  gap: 6px;
}
.settings-card-title {
  color: var(--text);
  font-weight: 700;
}
.release-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}
.release-card {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 10px;
  background: var(--surface-code);
  display: grid;
  gap: 6px;
}
.release-card strong { font-size: 16px; }
.release-version-link {
  color: var(--text);
  text-decoration: none;
  width: fit-content;
}
.release-version-link:hover strong { color: var(--accent); }
.compact-status {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
.compact-status-mark {
  color: var(--green);
  font-weight: 800;
}
.compact-status.bad .compact-status-mark {
  color: var(--red);
}
.release-log {
  white-space: pre-wrap;
  max-height: 220px;
  overflow: auto;
  font-family: var(--mono);
}
.category-toolbar {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
}
.category-match-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.category-match {
  width: auto;
  min-height: 32px;
  padding: 6px 10px;
  border-radius: 999px;
  background: var(--surface-code);
}
.category-match.active {
  background: var(--blue);
  color: #ffffff;
  border-color: var(--blue);
}
.v2fly-catalog-status {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 10px;
  background: var(--surface-code);
  min-height: 42px;
  display: flex;
  align-items: center;
  color: var(--muted);
}
.source-preview.bad {
  border-color: var(--danger);
  color: var(--danger);
  background: var(--red-soft);
}
.helper-text {
  color: var(--text-soft);
  font-size: 12px;
  line-height: 1.4;
}
.run-launch-summary {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface-soft);
  padding: 12px;
  display: grid;
  gap: 10px;
}
.run-launch-summary-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.run-launch-summary-title {
  font-size: 13px;
  font-weight: 700;
}
.run-launch-summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 8px;
}
.run-launch-summary-item {
  min-width: 0;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
  padding: 8px;
  display: grid;
  gap: 3px;
}
.run-launch-summary-label {
  color: var(--text-soft);
  font-size: 11px;
  line-height: 1.25;
}
.run-launch-summary-value {
  color: var(--text);
  font-size: 12px;
  line-height: 1.3;
  font-weight: 700;
  overflow-wrap: anywhere;
}
.candidate-summary {
  color: var(--text-soft);
  font-size: 13px;
  white-space: nowrap;
  text-align: right;
  margin-bottom: 10px;
}
.candidate-result-panel {
  display: grid;
  gap: 10px;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 12px;
  background: var(--surface-soft);
}
.candidate-result-head {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 12px;
  align-items: start;
}
.candidate-result-head h3 {
  margin: 0 0 4px;
  font-size: 15px;
  letter-spacing: 0;
}
.candidate-result-modes {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}
.candidate-result-toolbar {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}
.candidate-result-body {
  display: grid;
  gap: 10px;
}
.candidate-result-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 8px;
}
.candidate-result-cell {
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--surface);
  padding: 9px 10px;
}
.candidate-result-label {
  color: var(--text-soft);
  font-size: 11px;
  font-weight: 800;
  text-transform: uppercase;
}
.candidate-result-value {
  margin-top: 4px;
  color: var(--text);
  font-size: 14px;
  line-height: 1.35;
  overflow-wrap: anywhere;
}
.candidate-result-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.candidate-result-strategies {
  display: grid;
  gap: 6px;
}
.candidate-result-strategy {
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--surface-code);
  padding: 8px;
  display: grid;
  gap: 5px;
}
.candidate-result-strategy code {
  color: var(--code-text);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.candidate-result-domains {
  color: var(--text-soft);
  font-size: 12px;
}
.candidate-tabs {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.subtab-button {
  min-height: 34px;
  background: var(--surface-soft);
  color: var(--blue-strong);
  border-color: var(--line-strong);
}
.subtab-button.active {
  background: var(--blue);
  color: #ffffff;
  border-color: var(--blue);
}
.candidate-groups {
  display: grid;
  gap: 14px;
}
.backup-list {
  display: grid;
  gap: 12px;
}
.backup-upload-panel {
  margin: 12px 0;
  border-style: dashed;
}
.backup-card {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface-soft);
  padding: 12px;
  display: grid;
  gap: 10px;
}
.backup-meta {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 8px;
  color: var(--text-soft);
  font-size: 13px;
}
.backup-downloads {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 12px;
  align-items: start;
}
.backup-card-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.backup-download-block {
  display: grid;
  gap: 8px;
}
.backup-section-title {
  color: var(--text-soft);
  font-size: 11px;
  font-weight: 800;
  letter-spacing: .04em;
  text-transform: uppercase;
}
.backup-archive-link {
  color: #ffffff;
  border: 1px solid var(--blue);
  border-radius: 6px;
  padding: 10px 12px;
  text-decoration: none;
  background: var(--blue);
  font-size: 13px;
  font-weight: 800;
  text-align: center;
}
.backup-archive-link:hover {
  background: var(--blue-strong);
  border-color: var(--blue-strong);
}
.backup-file-links {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.backup-file-links a {
  color: var(--blue-strong);
  border: 1px solid var(--line-strong);
  border-radius: 6px;
  padding: 6px 8px;
  text-decoration: none;
  background: var(--surface-code);
  font-size: 13px;
}
.backup-file-links a:hover {
  border-color: var(--blue);
}
.domain-group {
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
  background: var(--surface);
}
.domain-group[open] .domain-header { border-bottom: 1px solid var(--line); }
.domain-group:not([open]) > :not(summary) { display: none; }
.domain-group summary { cursor: pointer; list-style: none; }
.domain-group summary::-webkit-details-marker { display: none; }
.domain-header {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 14px;
  background: var(--surface-soft);
}
.domain-title {
  font-weight: 700;
  overflow-wrap: anywhere;
}
.domain-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.protocol-group {
  display: grid;
  gap: 10px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--line);
  background: var(--surface);
}
.protocol-group:last-child { border-bottom: 0; }
.protocol-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.domain-strategy-box {
  display: grid;
  gap: 8px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--line);
  background: var(--surface);
}
.strategy-editor {
  display: grid;
  gap: 8px;
}
.strategy-editor-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.strategy-editor-title {
  display: grid;
  gap: 2px;
  min-width: 0;
}
.strategy-editor-meta {
  color: var(--text-soft);
  font-size: 12px;
  overflow-wrap: anywhere;
}
.code-editor {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  max-height: 360px;
  overflow: hidden;
  border: 1px solid var(--line-strong);
  border-radius: 6px;
  background: var(--surface-code);
}
.line-numbers,
.strategy-code,
.line-numbered-textarea {
  margin: 0;
  min-height: 150px;
  max-height: 360px;
  font-family: Menlo, Monaco, Consolas, "Andale Mono", "Ubuntu Mono", "Courier New", monospace;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre;
}
.line-numbers {
  min-width: 46px;
  overflow: hidden;
  padding: 10px 10px 10px 8px;
  border-right: 1px solid var(--line);
  background: var(--surface-code-gutter);
  color: var(--code-muted);
  text-align: right;
  user-select: none;
}
.strategy-code,
.line-numbered-textarea {
  width: 100%;
  min-width: 0;
  overflow: auto;
  border: 0;
  border-radius: 0;
  padding: 10px 12px;
  resize: vertical;
  background: var(--surface-code);
  color: var(--code-text);
  tab-size: 2;
}
.text-editor {
  max-height: 260px;
}
.text-editor .line-numbers,
.text-editor .line-numbered-textarea {
  min-height: 118px;
  max-height: 260px;
}
.strategy-code:focus,
.line-numbered-textarea:focus {
  outline: 2px solid rgba(0, 151, 220, .55);
  outline-offset: -2px;
}
.tabs {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  border-bottom: 1px solid var(--line);
}
.tab-button {
  background: var(--surface-soft);
  color: var(--blue);
  border-color: var(--line-strong);
  border-bottom-left-radius: 0;
  border-bottom-right-radius: 0;
}
.tab-button.active {
  background: var(--blue);
  color: #ffffff;
  border-color: var(--blue);
}
.tab-page { display: none; min-width: 0; }
.tab-page.active { display: block; }
.terminal-panel pre {
  max-height: none;
  min-height: 420px;
  height: calc(100vh - 260px);
}
.raw-log-panel {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface-soft);
  overflow: hidden;
}
.raw-log-panel summary {
  padding: 10px 12px;
  cursor: pointer;
  color: var(--text-soft);
  font-size: 12px;
  font-weight: 800;
  list-style: none;
}
.raw-log-panel summary::-webkit-details-marker { display: none; }
.raw-log-panel pre {
  border-radius: 0;
  border-left: 0;
  border-right: 0;
  border-bottom: 0;
}
.terminal-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.live-run-panel,
.events-panel {
  display: grid;
  gap: 10px;
  margin-bottom: 12px;
}
.live-run-card,
.event-card {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 12px;
  background: var(--surface-soft);
  display: grid;
  gap: 10px;
}
.live-run-header,
.event-header {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.live-run-title,
.event-title {
  font-weight: 800;
  color: var(--text);
}
.live-run-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(132px, 1fr));
  gap: 8px;
}
.live-run-cell {
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 8px;
  background: var(--surface);
}
.live-run-label {
  color: var(--text-soft);
  font-size: 11px;
  font-weight: 800;
  text-transform: uppercase;
}
.live-run-value {
  margin-top: 4px;
  font-size: 14px;
  font-weight: 700;
  line-height: 1.35;
  overflow-wrap: anywhere;
}
.live-run-actions,
.event-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.event-card.warn {
  border-color: #eed09a;
  background: var(--amber-soft);
}
.event-card.bad {
  border-color: #f0b9b5;
  background: var(--red-soft);
}
.event-meta {
  color: var(--text-soft);
  font-size: 12px;
}
.event-card.warn .event-meta,
.event-card.bad .event-meta {
  color: inherit;
}
.progress-panel {
  display: grid;
  gap: 10px;
  margin-bottom: 12px;
}
.progress-bar {
  height: 12px;
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--surface-soft);
}
.progress-fill {
  height: 100%;
  width: 0%;
  background: var(--blue);
  transition: width .2s ease;
}
.progress-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
}
.progress-cell {
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 9px 10px;
  background: var(--surface);
}
.progress-label {
  color: var(--text-soft);
  font-size: 12px;
}
.progress-value {
  margin-top: 4px;
  font-size: 15px;
  font-weight: 700;
  overflow-wrap: anywhere;
}
.progress-note {
  color: var(--text-soft);
  font-size: 12px;
}
.mutating-disabled-note {
  color: var(--amber);
  font-size: 12px;
}
.message {
  min-height: 36px;
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 9px 10px;
  background: var(--surface-soft);
  color: var(--text-soft);
  font-size: 13px;
}
.message.good { background: var(--green-soft); color: var(--green); border-color: #b8dfca; }
.message.warn { background: var(--amber-soft); color: var(--amber); border-color: #eed09a; }
.message.bad { background: var(--red-soft); color: var(--red); border-color: #f0b9b5; }
.toast {
  position: fixed;
  top: max(14px, env(safe-area-inset-top));
  left: 50%;
  z-index: 9999;
  max-width: min(420px, calc(100vw - 36px));
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 11px 14px;
  background: var(--surface);
  color: #e6edf3;
  box-shadow: 0 10px 30px rgba(23, 33, 43, .16);
  font-size: 13px;
  line-height: 1.4;
  opacity: 0;
  transform: translate(-50%, -10px);
  transition: opacity .16s ease, transform .16s ease;
  pointer-events: none;
}
.toast.show {
  opacity: 1;
  transform: translate(-50%, 0);
}
.toast.good { background: var(--green-soft); color: var(--green); border-color: #b8dfca; }
.toast.warn { background: var(--amber-soft); color: var(--amber); border-color: #eed09a; }
.toast.bad { background: var(--red-soft); color: var(--red); border-color: #f0b9b5; }
.badge {
  display: inline-flex;
  align-items: center;
  min-height: 24px;
  max-width: 100%;
  border-radius: 999px;
  padding: 0 9px;
  font-size: 12px;
  font-weight: 700;
  border: 1px solid var(--line);
  background: var(--surface-soft);
  color: #e6edf3;
  overflow: hidden;
  text-overflow: ellipsis;
}
.badge.good { background: var(--green-soft); color: var(--green); border-color: #b8dfca; }
.badge.warn { background: var(--amber-soft); color: var(--amber); border-color: #eed09a; }
.badge.bad { background: var(--red-soft); color: var(--red); border-color: #f0b9b5; }
.table-wrap { overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; min-width: 0; max-width: 100%; }
table { width: 100%; min-width: 760px; border-collapse: collapse; font-size: 13px; table-layout: auto; }
th, td { padding: 10px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; overflow-wrap: anywhere; }
th { color: var(--text-soft); font-size: 12px; font-weight: 700; background: var(--surface-soft); }
tr:last-child td { border-bottom: 0; }
.run-history {
  display: grid;
  gap: 10px;
}
.run-card {
  border: 1px solid var(--line);
  border-left: 4px solid var(--line-strong);
  border-radius: 8px;
  overflow: hidden;
  background: var(--surface);
  box-shadow: 0 1px 0 rgba(255, 255, 255, .03);
}
.run-card:nth-child(even) { background: #1d2738; }
.run-card-status-success { border-left-color: var(--green); }
.run-card-status-running,
.run-card-status-queued { border-left-color: var(--blue); }
.run-card-status-stopping,
.run-card-status-stopped,
.run-card-status-timeout { border-left-color: var(--amber); }
.run-card-status-failed { border-left-color: var(--red); }
.run-card-kind-multi .run-card-main {
  background: linear-gradient(90deg, rgba(0, 151, 220, .07), transparent 42%);
}
.run-card-main {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(112px, 1fr));
  gap: 10px;
  align-items: start;
  padding: 12px;
}
.run-card-actions {
  display: flex;
  justify-content: flex-end;
  padding: 0 12px 12px;
}
.run-field {
  display: grid;
  gap: 4px;
  min-width: 0;
}
.run-field-label {
  color: var(--text-soft);
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
}
.run-field-value {
  min-width: 0;
  font-size: 13px;
  line-height: 1.35;
  overflow-wrap: anywhere;
}
.run-status {
  justify-self: start;
  white-space: nowrap;
}
.run-domains {
  border-top: 1px solid var(--line);
  background: var(--surface-soft);
}
.run-domains summary {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto auto;
  gap: 8px;
  align-items: center;
  min-width: 0;
  padding: 10px 12px;
  cursor: pointer;
  list-style: none;
}
.run-domains summary::-webkit-details-marker { display: none; }
.run-domains-preview {
  min-width: 0;
  color: #e6edf3;
  font-size: 12px;
  line-height: 1.35;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.run-domains-count {
  color: var(--text-soft);
  font-size: 12px;
  white-space: nowrap;
}
.run-domains-arrow {
  width: 0;
  height: 0;
  border-top: 5px solid transparent;
  border-bottom: 5px solid transparent;
  border-left: 6px solid var(--blue-strong);
  transition: transform .16s ease;
}
.run-domains:not(.run-domains-expandable) .run-domains-arrow {
  visibility: hidden;
}
.run-domains[open] .run-domains-arrow {
  transform: rotate(90deg);
}
.run-domains .run-domain-list {
  display: none;
  flex-wrap: wrap;
  gap: 6px;
  min-width: 0;
  padding: 0 12px 12px;
}
.run-domains[open] .run-domain-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.run-domain-chip {
  max-width: 100%;
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 4px 8px;
  background: var(--surface);
  color: #e6edf3;
  font-size: 12px;
  line-height: 1.25;
  overflow-wrap: anywhere;
}
.run-diagnostics {
  border-top: 1px solid var(--line);
  color: var(--muted);
  padding: 10px 14px;
}
.run-diagnostics summary {
  cursor: pointer;
  font-weight: 800;
  list-style: none;
}
.run-diagnostics summary::-webkit-details-marker { display: none; }
.run-diagnostic-table-wrap {
  margin-top: 8px;
  overflow-x: auto;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface-code);
}
.run-diagnostic-table {
  width: 100%;
  min-width: 680px;
  border-collapse: collapse;
  font-size: 12px;
}
.run-diagnostic-table th,
.run-diagnostic-table td {
  padding: 8px 10px;
  border-bottom: 1px solid var(--line);
  text-align: left;
  vertical-align: top;
}
.run-diagnostic-table th {
  color: var(--text-soft);
  font-size: 11px;
  font-weight: 800;
  letter-spacing: .04em;
  text-transform: uppercase;
}
.run-diagnostic-table tr:last-child td { border-bottom: 0; }
.run-diagnostic-target {
  font-family: Consolas, "SFMono-Regular", monospace;
  overflow-wrap: anywhere;
}
.run-diagnostic-status {
  display: inline-block;
  border: 1px solid var(--line-strong);
  border-radius: 999px;
  padding: 3px 8px;
  color: var(--text);
  white-space: nowrap;
}
.run-diagnostic-status.warn { border-color: var(--warn); color: var(--warn); }
.run-diagnostic-status.bad { border-color: var(--danger); color: var(--danger); }
.run-diagnostic-details {
  color: var(--text);
  line-height: 1.35;
}
.run-diagnostic-tech {
  margin-top: 4px;
  color: var(--text-soft);
}
.run-diagnostic-tech summary {
  cursor: pointer;
  font-weight: 700;
}
.run-diagnostic-tech div {
  margin-top: 4px;
  font-family: Consolas, "SFMono-Regular", monospace;
  overflow-wrap: anywhere;
}
.run-diagnostic-note { margin-top: 8px; }
code {
  display: block;
  max-width: 100%;
  font-family: Consolas, "SFMono-Regular", monospace;
  font-size: 12px;
  color: var(--code-text);
  white-space: normal;
  overflow-wrap: anywhere;
}
.empty {
  min-height: 92px;
  display: grid;
  place-items: center;
  border: 1px dashed var(--line-strong);
  border-radius: 8px;
  color: var(--text-soft);
  font-size: 13px;
  text-align: center;
  padding: 16px;
}
.loading-skeleton {
  min-height: 160px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background:
    linear-gradient(90deg, transparent, rgba(255,255,255,0.06), transparent),
    linear-gradient(180deg, var(--surface), var(--surface-strong));
  background-size: 220px 100%, 100% 100%;
  background-repeat: no-repeat;
  animation: skeleton-sweep 1.2s ease-in-out infinite;
}
@keyframes skeleton-sweep {
  from { background-position: -240px 0, 0 0; }
  to { background-position: calc(100% + 240px) 0, 0 0; }
}
pre {
  margin: 0;
  max-height: 470px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 12px;
  line-height: 1.45;
  background: var(--surface-code);
  color: #d7e0ea;
  border-radius: 8px;
  padding: 12px;
}
@media (max-width: 960px) {
  .status-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .layout { grid-template-columns: 1fr; }
}
@media (max-width: 560px) {
  .topbar-inner, .main { padding-left: 14px; padding-right: 14px; }
  .topbar-inner { align-items: stretch; flex-direction: column; }
  .status-grid, .button-row, .fill-row, .preset-grid, .preset-actions, .domain-picker-row, .backup-downloads, .release-grid, .category-toolbar, .time-limit-row { grid-template-columns: 1fr; }
  .protocol-grid { grid-template-columns: 1fr; }
  .progress-grid { grid-template-columns: 1fr; }
  .tabs { display: grid; grid-template-columns: 1fr; }
  .candidate-summary { white-space: normal; }
  .domain-header, .protocol-header { align-items: stretch; flex-direction: column; }
  h1 { font-size: 22px; }
  .metric-value { font-size: 18px; }
  button { width: 100%; }
}
</style>
</head>
<body>
<div class="shell">
  <header class="topbar">
    <div class="topbar-inner">
      <div class="brand">
        <h1>Подбор стратегий zapret2</h1>
        <div class="subtitle">Raspberry Pi · проверка стратегий · live-лог</div>
      </div>
      <div class="topbar-badges">
        <span class="topbar-version" id="app-version-badge">v-</span>
        <span class="topbar-version topbar-auth" id="web-auth-badge">auth</span>
      </div>
    </div>
  </header>
  <main class="main">
    <section class="status-grid" aria-label="Сводка">
      <div class="metric">
        <div class="metric-label">Система</div>
        <div class="metric-value" id="metric-zapret">Загрузка</div>
        <div class="metric-note" id="metric-zapret-note">-</div>
      </div>
      <button class="metric metric-button" data-tab="terminal" id="metric-job-card" type="button">
        <div class="metric-label">Подбор</div>
        <div class="metric-value" id="metric-job">-</div>
        <div class="metric-note" id="metric-job-note">-</div>
      </button>
      <button class="metric metric-button" data-tab="candidates" type="button">
        <div class="metric-label">Домены</div>
        <div class="metric-value" id="metric-candidates">0</div>
        <div class="metric-note" id="metric-candidates-note">протестировано</div>
      </button>
    </section>

    <nav class="tabs" role="tablist" aria-label="Разделы">
      <button class="tab-button active" id="tab-finder" role="tab" aria-selected="true" aria-controls="tab-panel-finder" data-tab="finder" type="button">Подбор</button>
      <button class="tab-button" id="tab-history" role="tab" aria-selected="false" aria-controls="tab-panel-history" data-tab="history" type="button">История</button>
      <button class="tab-button" id="tab-candidates" role="tab" aria-selected="false" aria-controls="tab-panel-candidates" data-tab="candidates" type="button">Кандидаты</button>
      <button class="tab-button" id="tab-terminal" role="tab" aria-selected="false" aria-controls="tab-panel-terminal" data-tab="terminal" type="button">Терминал</button>
      <button class="tab-button" id="tab-lists" role="tab" aria-selected="false" aria-controls="tab-panel-lists" data-tab="lists" type="button">Списки и профили</button>
      <button class="tab-button" id="tab-settings" role="tab" aria-selected="false" aria-controls="tab-panel-settings" data-tab="settings" type="button">Настройки</button>
    </nav>

    <section class="tab-page active" id="tab-panel-finder" role="tabpanel" aria-labelledby="tab-finder" data-tab-page="finder">
    <div class="layout finder-layout">
      <div class="stack">
        <section class="panel">
          <div class="panel-header">
            <h2>Запуск поиска</h2>
            <span class="badge" id="job-badge">Можно запускать</span>
          </div>
          <div class="form-grid">
            <div class="field">
              <label for="finder-domains">Домены</label>
              <div class="code-editor text-editor">
                <pre class="line-numbers" data-line-numbers-for="finder-domains" aria-hidden="true">1</pre>
                <textarea id="finder-domains" class="line-numbered-textarea" autocomplete="off" spellcheck="false"></textarea>
              </div>
            </div>
            <div class="preset-panel">
              <div class="field">
                <label for="finder-preset-select">Пресет доменов</label>
                <select id="finder-preset-select"></select>
              </div>
              <div class="helper-text">Пресет применяется сразу при выборе. Если вручную изменить список доменов, выбранное значение станет `Custom` и ручной список не будет перезаписан.</div>
            </div>
            <div class="preset-panel">
              <div class="field">
                <label>Режим поиска</label>
                <div class="segmented-control" id="run-mode-control">
                  <label class="segment-option tooltip-button" data-tooltip="Запускает штатную проверку стратегий: домены проверяются обычным порядком. Хороший режим для базовой совместимости.">
                    <input type="radio" name="run-mode" value="standard" checked>
                    Домены по очереди
                  </label>
                  <label class="segment-option tooltip-button" data-tooltip="Одна стратегия запускается один раз, затем все выбранные домены проверяются параллельными проверочными запросами. Удобно быстрее понять, какие домены покрывает одна стратегия.">
                    <input type="radio" name="run-mode" value="multi">
                    Все домены на одной стратегии
                  </label>
                </div>
              </div>
              <div class="helper-text" id="run-mode-note">Обычный режим: штатная проверка стратегий проходит по своему порядку.</div>
              <div class="field multi-curl-field" id="multi-curl-field" hidden>
                <label for="curl-parallelism">Параллельных проверочных запросов</label>
                <input id="curl-parallelism" type="number" min="1" step="1" value="4">
                <div class="helper-text">Работает только в режиме `Все домены на одной стратегии`: одна стратегия проверяет несколько доменов параллельно.</div>
              </div>
            </div>
            <div class="preset-panel finder-options-panel">
              <div class="helper-text">Основные проверки, которые реально влияют на подбор стратегий.</div>
              <div class="protocol-grid">
                <label class="checkbox-row">
                  <input id="enable-http" type="checkbox">
                  <span>HTTP</span>
                </label>
                <label class="checkbox-row">
                  <input id="enable-tls12" type="checkbox" checked>
                  <span>TLS 1.2</span>
                </label>
                <label class="checkbox-row">
                  <input id="enable-tls13" type="checkbox">
                  <span>TLS 1.3</span>
                </label>
                <label class="checkbox-row">
                  <input id="include-quic" type="checkbox" checked>
                  <span>HTTP3 / QUIC</span>
                </label>
                <label class="checkbox-row">
                  <input id="enable-ipv6" type="checkbox">
                  <span>IPv6</span>
                </label>
              </div>
            </div>
            <details class="preset-panel">
              <summary class="domain-header">
                <span class="domain-title">Расширенные параметры</span>
                <span class="helper-text">глубина, повторы, DNS/IP-check, лимиты и timeout</span>
              </summary>
              <div class="field scan-level-field">
                <label for="discovery-profile-select">Глубина проверки стратегий</label>
                <select id="discovery-profile-select"></select>
                <input id="scan-level" type="hidden" value="standard">
                <div class="helper-text" id="discovery-profile-note">Технический профиль проверки: quick, standard или force.</div>
              </div>
              <div class="time-limit-panel disabled" id="time-limit-panel">
                <div class="time-limit-title">Ограничение времени поиска</div>
                <div class="time-limit-row">
                  <label class="checkbox-row">
                    <input id="limit-time-enabled" type="checkbox">
                    <span>Ограничить время поиска</span>
                  </label>
                  <div class="field time-limit-field" id="time-limit-field" aria-disabled="true">
                    <label for="finder-timeout-hours">Лимит поиска, часов</label>
                    <input id="finder-timeout-hours" type="number" min="0.1" max="24" step="0.5" value="6" disabled>
                  </div>
                </div>
              </div>
              <div class="preset-grid">
                <div class="field">
                  <label for="repeats">Повторы проверки стратегии</label>
                  <input id="repeats" type="number" min="1" max="10" step="1" value="1">
                </div>
                <div class="field">
                  <label for="run-curl-max-time">Timeout HTTP/TLS, сек</label>
                  <input id="run-curl-max-time" type="number" min="1" step="1" value="2">
                </div>
                <div class="field">
                  <label for="run-curl-max-time-quic">Timeout QUIC, сек</label>
                  <input id="run-curl-max-time-quic" type="number" min="1" step="1" value="2">
                </div>
                <div class="field">
                  <label for="run-curl-max-time-doh">Timeout DoH, сек</label>
                  <input id="run-curl-max-time-doh" type="number" min="1" step="1" value="2">
                </div>
              </div>
              <label class="checkbox-row">
                <input id="repeat-parallel" type="checkbox">
                <span>Запускать повторы параллельно</span>
              </label>
              <label class="checkbox-row">
                <input id="skip-dnscheck" type="checkbox" checked>
                <span>Пропустить DNS-проверку перед подбором</span>
              </label>
              <label class="checkbox-row">
                <input id="skip-ipblock" type="checkbox" checked>
                <span>Пропустить проверку IP/port-блокировки</span>
              </label>
            </details>
            <section class="run-launch-summary" aria-label="Сводка параметров запуска">
              <div class="run-launch-summary-header">
                <div class="run-launch-summary-title">Параметры запуска</div>
                <span class="badge" id="run-launch-readiness">-</span>
              </div>
              <div class="run-launch-summary-grid" id="run-launch-summary-grid"></div>
            </section>
            <div class="button-row run-actions">
              <button class="tooltip-button" data-action="run-selected-discovery" data-tooltip="Запускает выбранный выше режим поиска с текущими доменами, глубиной проверки и параметрами." type="button">Запустить выбранный режим</button>
              <button class="secondary danger tooltip-button" data-action="stop-current" data-tooltip="Останавливает текущий подбор и сохраняет уже найденные успешные стратегии." type="button" disabled>Остановить текущий запуск</button>
            </div>
            <div class="message" id="message">Готово</div>
          </div>
        </section>
      </div>
    </div>
    </section>

    <section class="tab-page history-page" id="tab-panel-history" role="tabpanel" aria-labelledby="tab-history" data-tab-page="history">
      <section class="panel">
        <div class="panel-header">
          <h2>История запусков</h2>
          <span class="badge" id="finder-runs-count">0</span>
        </div>
        <div id="finder-runs-table"></div>
      </section>
    </section>

    <section class="tab-page candidates-page" id="tab-panel-candidates" role="tabpanel" aria-labelledby="tab-candidates" data-tab-page="candidates">
      <section class="panel">
        <div class="panel-header">
          <h2>Найденные стратегии</h2>
          <span class="badge" id="candidates-count">0</span>
        </div>
        <div class="candidate-summary" id="candidate-summary">-</div>
        <div class="candidate-tabs" role="tablist" aria-label="Вид кандидатов">
          <button class="subtab-button active" id="candidate-view-domain" role="tab" aria-selected="true" aria-controls="candidates-table" data-candidate-view="domain" type="button">По доменам</button>
          <button class="subtab-button" id="candidate-view-common" role="tab" aria-selected="false" aria-controls="candidates-table" data-candidate-view="common" type="button">Общие стратегии</button>
        </div>
        <div class="common-filter-panel" id="common-controls" hidden>
          <div class="preset-grid">
            <div class="field">
              <label for="common-preset-select">Пресет доменов для пересечения</label>
              <select id="common-preset-select"></select>
            </div>
          </div>
          <div class="field">
            <label for="common-domains">Домены для поиска общих стратегий</label>
            <div class="code-editor text-editor">
              <pre class="line-numbers" data-line-numbers-for="common-domains" aria-hidden="true">1</pre>
              <textarea id="common-domains" class="line-numbered-textarea" autocomplete="off" spellcheck="false" placeholder="discord.com&#10;discordcdn.com"></textarea>
            </div>
          </div>
          <div class="domain-picker-row">
            <div class="common-domain-picker">
              <input id="common-domain-add" list="tested-domain-options" autocomplete="off" placeholder="Начните вводить протестированный домен">
              <div id="common-domain-suggestions" class="common-domain-suggestions" role="listbox" hidden></div>
            </div>
            <button class="secondary" data-action="add-common-domain" title="Добавляет домен в фильтр общих стратегий, если по нему уже есть кандидаты." type="button">Добавить домен</button>
          </div>
          <datalist id="tested-domain-options"></datalist>
          <div class="helper-text" id="common-domain-note">Выберите минимум два протестированных домена.</div>
          <section class="candidate-result-panel" aria-label="Итоговый набор общих стратегий">
            <div class="candidate-result-head">
              <div>
                <h3>Итоговый набор общих стратегий</h3>
                <div class="helper-text" id="candidate-result-source">Выберите домены для пересечения и соберите итоговый набор.</div>
              </div>
              <div class="candidate-result-toolbar">
                <button data-action="build-candidate-result" type="button">Собрать итоговый набор</button>
                <div class="candidate-result-modes" role="tablist" aria-label="Режим итогового набора">
                  <button class="subtab-button" id="candidate-result-mode-coverage" role="tab" aria-selected="false" aria-controls="candidate-result-body" data-candidate-result-mode="coverage" type="button">Максимум покрытия</button>
                  <button class="subtab-button" id="candidate-result-mode-minimal" role="tab" aria-selected="false" aria-controls="candidate-result-body" data-candidate-result-mode="minimal" type="button">Минимум стратегий</button>
                  <button class="subtab-button active" id="candidate-result-mode-balance" role="tab" aria-selected="true" aria-controls="candidate-result-body" data-candidate-result-mode="balance" type="button">Баланс</button>
                </div>
              </div>
            </div>
            <div id="candidate-result-body" class="candidate-result-body" role="tabpanel" aria-live="polite" aria-labelledby="candidate-result-mode-balance">
              <div class="empty">Нажмите «Собрать итоговый набор» после выбора доменов.</div>
            </div>
          </section>
        </div>
        <div id="candidates-table"></div>
      </section>
    </section>

    <section class="tab-page terminal-page" id="tab-panel-terminal" role="tabpanel" aria-labelledby="tab-terminal" data-tab-page="terminal">
      <section class="panel terminal-panel">
        <div class="panel-header">
          <h2>Терминал</h2>
          <div class="terminal-actions">
            <span class="badge" id="finder-log-status">-</span>
            <button class="secondary danger" data-action="stop-current" title="Останавливает текущий подбор и сохраняет уже найденные успешные стратегии." disabled>Остановить</button>
          </div>
        </div>
        <section class="live-run-panel" id="live-run-panel" aria-label="Текущий подбор" aria-live="polite"></section>
        <div class="progress-panel">
          <div class="progress-bar" id="progress-bar" role="progressbar" aria-label="Прогресс подбора" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">
            <div class="progress-fill" id="progress-fill"></div>
          </div>
          <div class="progress-grid">
            <div class="progress-cell">
              <div class="progress-label">Проверено попыток</div>
              <div class="progress-value" id="progress-attempted">-</div>
            </div>
            <div class="progress-cell">
              <div class="progress-label">Стратегии</div>
              <div class="progress-value" id="progress-strategies">-</div>
            </div>
            <div class="progress-cell">
              <div class="progress-label">Найдено стратегий</div>
              <div class="progress-value" id="progress-successful">-</div>
            </div>
            <div class="progress-cell">
              <div class="progress-label">Этап</div>
              <div class="progress-value" id="progress-phase">-</div>
            </div>
            <div class="progress-cell">
              <div class="progress-label">Текущий файл</div>
              <div class="progress-value" id="progress-scripts">-</div>
            </div>
            <div class="progress-cell">
              <div class="progress-label">Осталось</div>
              <div class="progress-value" id="progress-eta">-</div>
            </div>
            <div class="progress-cell">
              <div class="progress-label">Прошло</div>
              <div class="progress-value" id="progress-elapsed">-</div>
            </div>
          </div>
          <div class="progress-note" id="progress-note">расчитанное среднее время попытки: -</div>
          <div class="progress-note" id="progress-metrics">Настройки запуска появятся после старта подбора.</div>
        </div>
        <section class="events-panel" id="events-panel" aria-label="Ошибки и предупреждения"></section>
        <div id="stderr-diagnostics" class="stderr-diagnostics" hidden></div>
        <details class="raw-log-panel">
          <summary>Raw log / debug</summary>
          <pre id="finder-log">Лога пока нет</pre>
        </details>
      </section>
    </section>

    <section class="tab-page lists-page" id="tab-panel-lists" role="tabpanel" aria-labelledby="tab-lists" data-tab-page="lists">
      <section class="panel">
        <div class="panel-header">
          <h2>Списки и профили</h2>
        </div>
        <div class="settings-stack">
        <div class="preset-panel domain-preset-manager-panel">
          <div class="panel-header">
            <h2>Доменные пресеты</h2>
            <span class="badge" id="preset-manager-count">0</span>
          </div>
          <div class="field">
            <label for="preset-manager-name">Список</label>
            <select id="preset-manager-name"></select>
          </div>
          <div class="helper-text" id="preset-manager-note">Выберите список, отредактируйте домены и сохраните изменения.</div>
          <div class="field">
            <div class="code-editor text-editor">
              <pre class="line-numbers" data-line-numbers-for="preset-editor-domains" aria-hidden="true">1</pre>
              <textarea id="preset-editor-domains" class="line-numbered-textarea" autocomplete="off" spellcheck="false" placeholder="youtube.com&#10;discord.com"></textarea>
            </div>
          </div>
          <div class="button-row">
            <button data-action="preset-editor-save" type="button">Сохранить список</button>
            <button class="secondary" data-action="preset-editor-export" type="button">Скачать TXT</button>
            <button class="secondary danger" data-action="preset-editor-delete" type="button" disabled>Удалить пользовательский список</button>
          </div>
          <div class="source-preview" id="preset-editor-preview">Изменения еще не проверялись.</div>
          <details class="preset-create-panel">
            <summary>Создать новый список</summary>
            <div class="field">
              <label for="preset-new-name">Название списка</label>
              <input id="preset-new-name" autocomplete="off" placeholder="my-domains">
            </div>
            <div class="field">
              <div class="code-editor text-editor">
                <pre class="line-numbers" data-line-numbers-for="preset-new-domains" aria-hidden="true">1</pre>
                <textarea id="preset-new-domains" class="line-numbered-textarea" autocomplete="off" spellcheck="false" placeholder="youtube.com&#10;discord.com"></textarea>
              </div>
            </div>
            <div class="button-row">
              <button data-action="preset-new-save" type="button">Сохранить новый список</button>
            </div>
            <div class="source-preview" id="preset-new-preview">Новый список еще не сохранялся.</div>
          </details>
        </div>
        <div class="preset-panel settings-domain-source-panel">
          <div class="panel-header">
            <h2>Импорт из v2fly</h2>
            <span class="badge">domain-list-community</span>
          </div>
          <div class="preset-grid">
            <div class="field">
              <label for="v2fly-preset-name">Название пресета</label>
              <input id="v2fly-preset-name" autocomplete="off" placeholder="v2fly-youtube">
            </div>
          </div>
          <div class="field">
            <label for="v2fly-category-search">Каталог групп v2fly</label>
            <div class="category-toolbar">
              <input id="v2fly-category-search" list="v2fly-category-options" autocomplete="off" placeholder="Начните вводить название группы: youtube, google, discord">
              <datalist id="v2fly-category-options"></datalist>
              <button class="secondary" data-action="v2fly-load-categories" type="button" title="Перечитывает локальный каталог групп v2fly. Каталог скачивается при установке или обновлении сервиса.">Перечитать каталог</button>
            </div>
          </div>
          <div class="v2fly-catalog-status" id="v2fly-category-status">Каталог групп загрузится автоматически при открытии вкладки.</div>
          <div class="category-match-list" id="v2fly-category-matches"></div>
          <div class="field">
            <label for="v2fly-domains">Итоговые домены пресета</label>
            <div class="code-editor text-editor">
              <pre class="line-numbers" data-line-numbers-for="v2fly-domains" aria-hidden="true">1</pre>
              <textarea id="v2fly-domains" class="line-numbered-textarea" autocomplete="off" spellcheck="false" placeholder="После проверки здесь появятся домены. Список можно отредактировать перед сохранением."></textarea>
            </div>
          </div>
          <div class="button-row">
            <button class="secondary" data-action="v2fly-preview" type="button">Проверить и развернуть список</button>
            <button data-action="v2fly-import" type="button">Сохранить пресет</button>
          </div>
          <div class="source-preview" id="v2fly-preview-result">Список не проверялся.</div>
        </div>
        </div>
      </section>
    </section>

    <section class="tab-page settings-page" id="tab-panel-settings" role="tabpanel" aria-labelledby="tab-settings" data-tab-page="settings">
      <section class="panel">
        <div class="panel-header">
          <h2>Настройки</h2>
        </div>
        <div class="settings-stack">
        <div class="preset-panel settings-discovery-panel">
          <div class="panel-header">
            <h2>Параметры подбора</h2>
          </div>
          <div class="preset-grid">
            <label class="checkbox-row">
              <input id="settings-enable-ipv6" type="checkbox">
              IPv6-проверки
            </label>
            <label class="checkbox-row">
              <input id="settings-debug-stdout" type="checkbox">
              Подробный debug-лог stdout
            </label>
            <div class="setting-note">Включает расширенную запись stdout проверки стратегий в debug-файл. Обычный терминал остается компактным; debug нужен только для диагностики и может увеличить запись на диск.</div>
            <div class="field">
              <label for="settings-curl-max">Максимум параллельных проверочных запросов</label>
              <input id="settings-curl-max" type="number" min="1" value="10">
              <div class="setting-note">Верхняя граница для запуска параллельных проверочных запросов. Можно ставить любое число от 1, если плата и сеть справляются.</div>
            </div>
          </div>
          <div class="button-row">
            <button data-action="save-settings" type="button">Сохранить настройки</button>
          </div>
        </div>
        <div class="preset-panel settings-release-panel">
          <div class="panel-header">
            <h2>Релизы и обновления</h2>
          </div>
          <div class="release-grid">
            <div class="release-card">
              <span class="helper-text">Текущая версия</span>
              <strong id="settings-release-current">v-</strong>
            </div>
            <div class="release-card">
              <span class="helper-text">Стабильный релиз</span>
              <a id="settings-release-stable-link" class="release-version-link" href="https://github.com/balbomush/GP-access-control-plane/releases/latest" target="_blank" rel="noreferrer">
                <strong id="settings-release-stable">Не проверялось</strong>
              </a>
            </div>
            <div class="release-card">
              <span class="helper-text">Alpha / prerelease</span>
              <a id="settings-release-prerelease-link" class="release-version-link" href="https://github.com/balbomush/GP-access-control-plane/releases" target="_blank" rel="noreferrer">
                <strong id="settings-release-prerelease">Не проверялось</strong>
              </a>
            </div>
          </div>
          <div class="release-card">
            <label for="settings-update-channel">Канал установки</label>
            <select id="settings-update-channel">
              <option value="stable">Стабильные релизы</option>
              <option value="prerelease">Предрелизы</option>
            </select>
            <div class="setting-note">Stable ставит последний стабильный релиз. Предрелизы ставят последнюю alpha/prerelease-версию.</div>
          </div>
          <div class="button-row">
            <button class="secondary" data-action="check-releases" type="button">Проверить обновления</button>
            <button class="secondary tooltip-button" data-action="update-from-release" data-tooltip="Устанавливает выбранный канал обновления только если подбор не запущен. Перед обновлением создается бекап." type="button">Установить выбранное обновление</button>
            <button class="secondary" data-action="toggle-update-log" type="button">Показать лог обновления</button>
          </div>
          <div class="source-preview" id="settings-release-result" hidden></div>
          <pre class="source-preview release-log" id="settings-release-log" hidden></pre>
        </div>
        <div class="preset-panel settings-backups-panel">
          <div class="panel-header">
            <h2>Бекапы и восстановление</h2>
            <span class="badge" id="backups-count">0</span>
          </div>
          <div class="button-row">
            <button class="secondary" data-action="refresh-backups" type="button">Обновить список</button>
            <button data-action="create-backup" type="button">Создать бекап сейчас</button>
          </div>
          <div class="helper-text">Бекап создается только когда подбор не запущен. Хранятся последние 5 успешных копий.</div>
          <div class="helper-text" id="backups-updated-at"></div>
          <div class="preset-panel backup-upload-panel">
            <div class="panel-header">
              <h2>Загрузка ZIP-бекапа</h2>
            </div>
            <div class="button-row">
              <label class="secondary file-button" for="backup-upload-file">Выбрать ZIP</label>
              <input id="backup-upload-file" type="file" accept=".zip,application/zip" hidden>
              <button class="secondary" data-action="upload-backup" type="button">Загрузить бекап</button>
            </div>
            <div class="helper-text">Загруженный архив появится в списке ниже. Восстановление выполняется только из карточки конкретного бекапа.</div>
          </div>
          <div id="backups-table" class="backup-list"></div>
        </div>
        <div class="preset-panel settings-danger-panel">
          <div class="panel-header">
            <h2>Опасные действия</h2>
          </div>
          <div class="helper-text" id="mutating-lock-note">Восстановление, удаление данных, обновления и изменение настроек недоступны во время активного подбора.</div>
        </div>
        </div>
      </section>
    </section>
  </main>
  <div class="toast" id="toast" role="status" aria-live="polite" hidden></div>
</div>
<script>
const CUSTOM_PRESETS_KEY = 'gp-control-plane-domain-presets-v1';
const STRATEGY_LIST_LIMIT = 200;
const LIST_PAGE_LIMIT = 50;
const CANDIDATE_PAGE_LIMIT = LIST_PAGE_LIMIT;
const DOMAIN_PAGE_LIMIT = LIST_PAGE_LIMIT;
const RUN_PAGE_LIMIT = LIST_PAGE_LIMIT;
const CUSTOM_SELECT_VALUE = 'custom';
const WEB_AUTH = __WEB_AUTH_JSON__;
const DISCOVERY_PROFILES = {
  quick: { name: 'quick', title: 'Быстрый', scan_level: 'quick' },
  standard: { name: 'standard', title: 'Стандартный', scan_level: 'standard' },
  force: { name: 'force', title: 'Глубокий', scan_level: 'force' }
};
const state = { status: null, settings: null, settingsTouched: false, runPreferences: null, runPreferencesApplied: false, savingRunPreferences: false, releaseInfo: null, releaseStable: null, releasePrerelease: null, releaseUpdate: null, releaseChecked: false, releaseChecking: false, loadingDiscoveryProfile: false, loadingDomainPreset: false, loadingRunPreferences: false, discoveryProfiles: DISCOVERY_PROFILES, candidates: [], candidateTotal: 0, candidateOffset: 0, candidateHasMore: false, candidateVersion: null, candidateKnownVersion: null, candidateQueryKey: '', commonCandidateCache: {}, commonLoadingAll: false, candidateDomains: [], candidateDomainTotal: 0, candidateDomainStrategyTotal: 0, candidateDomainOffset: 0, candidateDomainHasMore: false, candidateDomainsLoaded: false, lastCandidateDomainTotal: 0, lastCandidateDomainStrategyTotal: 0, testedDomains: [], candidatesLoaded: false, candidateResultMode: 'balance', candidateResultRequested: false, domainStrategies: {}, finderRuns: [], finderRunTotal: 0, finderRunOffset: 0, finderRunHasMore: false, finderRunsLoaded: false, finderRunsLoading: false, finderLog: null, domainSets: null, domainSources: null, v2flyPreview: null, v2flyCategories: null, v2flyCategorySource: '', backups: [], backupsLoaded: false, activeTab: 'finder', candidateView: 'domain', customPresets: loadCustomPresets(), customPresetMeta: { finder: {}, common: {} }, systemPresets: { finder: {}, common: {} }, systemPresetMeta: { finder: {}, common: {} }, presetManager: { scope: 'finder', name: '', query: '', domains: [], total: 0, hasMore: false, loading: false, loaded: false }, openCandidateDomains: {}, openCommonProtocols: {}, openRunDomains: {}, expandedStrategyLists: {}, strategyEditorScrolls: {}, domainsInitialized: false, domainsTouched: false, formMessage: 'Готово', formMessageTone: '' };
const jobNames = {
  'zapret-standard-discovery': 'Поиск стратегий',
  'zapret-multi-domain-discovery': 'Все домены на одной стратегии',
  'standard-discovery': 'Поиск стратегий',
  'multi-domain-discovery': 'Все домены на одной стратегии'
};
const statusTone = { success: 'good', failed: 'bad', error: 'bad', running: 'warn', queued: 'warn', stopping: 'warn', stopped: 'warn', timeout: 'warn' };
let toastTimer = null;
let refreshInFlight = false;
let realtimeSource = null;
let realtimeConnected = false;
let realtimeFallbackTimer = null;
let logDirty = false;
let candidateRefreshTimer = null;
let candidateRequestSeq = 0;
let domainIndexRequestSeq = 0;
state.candidateLoading = false;
state.candidateUpdatedAt = '';
state.backupsLoading = false;
state.backupsUpdatedAt = '';

function el(id){ return document.getElementById(id); }
function esc(value){
  return String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[char]));
}
function setText(id, value){ el(id).textContent = value; }
function setMessage(text, tone){
  const node = el('message');
  state.formMessage = text || '';
  state.formMessageTone = tone || '';
  node.textContent = text;
  node.className = 'message' + (tone ? ' ' + tone : '');
  renderMetrics();
}
function showToast(text, tone){
  const node = el('toast');
  if (toastTimer) clearTimeout(toastTimer);
  node.textContent = text;
  node.className = 'toast' + (tone ? ' ' + tone : '');
  node.hidden = false;
  requestAnimationFrame(() => node.classList.add('show'));
  toastTimer = setTimeout(() => {
    node.classList.remove('show');
    toastTimer = setTimeout(() => {
      node.hidden = true;
      toastTimer = null;
    }, 180);
  }, 2000);
}
async function getJson(url){
  const response = await fetch(url, { headers: authHeaders(), credentials: 'same-origin' });
  if (!response.ok) throw new Error(await response.text());
  return await response.json();
}
async function postJson(url, payload){
  const response = await fetch(url, {
    method: 'POST',
    headers: authHeaders({'Content-Type': 'application/json'}),
    credentials: 'same-origin',
    body: JSON.stringify(payload || {})
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}
function authHeaders(headers){
  const result = { ...(headers || {}) };
  if (WEB_AUTH.enabled && WEB_AUTH.token) result[WEB_AUTH.header || 'X-GP-Token'] = WEB_AUTH.token;
  return result;
}
function authUrl(url){
  if (!WEB_AUTH.enabled || !WEB_AUTH.token) return url;
  const parsed = new URL(url, window.location.origin);
  parsed.searchParams.set(WEB_AUTH.query_param || 'gp_token', WEB_AUTH.token);
  return parsed.pathname + parsed.search + parsed.hash;
}
function friendlyDate(value){
  if (!value) return '-';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('ru-RU');
}
function friendlyTime(value){
  if (!value) return '';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? '' : parsed.toLocaleTimeString('ru-RU');
}
function shortPath(value){
  if (!value) return '-';
  const parts = String(value).split(/[\\\\/]/).filter(Boolean);
  return parts.length > 3 ? '...' + parts.slice(-3).join('/') : String(value);
}
function badge(text, tone){
  return `<span class="badge ${esc(tone || '')}">${esc(text)}</span>`;
}
function table(targetId, columns, rows, emptyText){
  if (!rows.length) {
    el(targetId).innerHTML = `<div class="empty">${esc(emptyText)}</div>`;
    return;
  }
  const head = columns.map((column) => `<th>${esc(column.label)}</th>`).join('');
  const body = rows.map((row) => '<tr>' + columns.map((column) => {
    const value = column.render ? column.render(row) : esc(row[column.key]);
    return `<td>${value}</td>`;
  }).join('') + '</tr>').join('');
  el(targetId).innerHTML = `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}
function latestById(rows){
  const byId = new Map();
  rows.forEach((row, index) => {
    byId.set(row.id || `row-${index}`, row);
  });
  return Array.from(byId.values()).sort((a, b) => String(a.timestamp || '').localeCompare(String(b.timestamp || '')));
}
function listLoadMore(action, hasMore, loading){
  if (!hasMore) return '';
  const label = loading ? 'Загружается...' : 'Загрузить еще';
  const disabled = loading ? ' disabled' : '';
  return `<div class="button-row list-load-more"><button class="secondary" data-action="${esc(action)}" type="button"${disabled}>${label}</button></div>`;
}
function runParams(offset){
  const params = new URLSearchParams();
  params.set('limit', String(RUN_PAGE_LIMIT));
  params.set('offset', String(Math.max(0, offset || 0)));
  return params;
}
function mergeRunPage(payload, reset){
  const rows = latestById((payload || {}).runs || []);
  state.finderRuns = reset ? rows : latestById([...rows, ...state.finderRuns]);
  state.finderRunTotal = Number((payload || {}).total || state.finderRuns.length);
  state.finderRunOffset = Number((payload || {}).offset || 0) + ((payload || {}).runs || []).length;
  state.finderRunHasMore = Boolean((payload || {}).has_more);
  state.finderRunsLoaded = true;
  state.finderRunsLoading = false;
}
function syncActiveTabUi(){
  document.querySelectorAll('.tab-button[data-tab]').forEach((button) => {
    const active = button.dataset.tab === state.activeTab;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', active ? 'true' : 'false');
    button.tabIndex = active ? 0 : -1;
  });
  document.querySelectorAll('[data-tab-page]').forEach((page) => {
    const active = page.dataset.tabPage === state.activeTab;
    page.classList.toggle('active', active);
    page.hidden = !active;
  });
}
function setActiveTab(tabName){
  state.activeTab = tabName;
  syncActiveTabUi();
  if (tabName === 'terminal') {
    if (logDirty) refreshLog();
    scrollLogToBottom();
  }
  if (tabName === 'candidates') ensureCandidateViewLoaded();
  if (tabName === 'lists') {
    if (!state.v2flyCategories) loadV2flyCategories();
    loadPresetEditorFromSelection({ silent: true });
  }
  if (tabName === 'settings') {
    if (!mutatingBlocked() && !state.releaseChecked && !state.releaseChecking) checkReleases({ silent: true });
    if (!state.backupsLoaded) refreshBackups();
  }
}
function latestRun(){
  return state.finderRuns.length ? state.finderRuns[state.finderRuns.length - 1] : null;
}
function isBusy(){
  const board = (state.status || {}).state || {};
  return Boolean(board.current_job);
}
function mutatingBlocked(){
  return isBusy();
}
function mutatingBlockedMessage(){
  return 'Идет подбор. Дождитесь завершения или остановите текущий подбор перед изменениями.';
}
function requireNoActiveRun(){
  if (!mutatingBlocked()) return true;
  setMessage(mutatingBlockedMessage(), 'warn');
  showToast(mutatingBlockedMessage(), 'warn');
  return false;
}
function defaultDomains(kind){
  const sets = state.domainSets || {};
  if (kind === 'all') {
    return Object.values(sets).flat();
  }
  if (kind === 'tested') return testedDomains();
  return sets[kind] || [];
}
function uniqueDomains(domains){
  return [...new Set((Array.isArray(domains) ? domains : []).map((domain) => String(domain || '').trim()).filter(Boolean))];
}
function uniqueDomainCount(domains){
  return uniqueDomains(domains).length;
}
function fillDomains(kind){
  const domains = uniqueDomains(defaultDomains(kind));
  el('finder-domains').value = domains.join('\\n');
  updateEditorLineNumbers('finder-domains');
  state.domainsTouched = true;
}
function finderDomains(){
  const raw = el('finder-domains').value.trim();
  return raw ? parseDomains(raw) : [];
}
function selectedFinderDomains(){
  const raw = el('finder-domains').value.trim();
  if (!raw) return [];
  return parseDomains(raw);
}
function timeoutSecondsOrNull(){
  if (!el('limit-time-enabled').checked) return null;
  const hours = Number(el('finder-timeout-hours').value || 6);
  return Math.max(60, Math.round(hours * 3600));
}
function syncTimeLimitUi(){
  const enabled = Boolean(el('limit-time-enabled')?.checked);
  const input = el('finder-timeout-hours');
  const field = el('time-limit-field');
  const panel = el('time-limit-panel');
  if (input) input.disabled = !enabled;
  if (field) field.setAttribute('aria-disabled', enabled ? 'false' : 'true');
  if (panel) panel.classList.toggle('disabled', !enabled);
}
function curlParallelism(){
  const value = Number(el('curl-parallelism').value || 4);
  const max = Number((state.settings || {}).curl_parallelism_max || 10);
  if (!Number.isFinite(value)) return 4;
  return Math.max(1, Math.min(max, Math.round(value)));
}
function repeatsValue(){
  const value = Number(el('repeats').value || 1);
  if (!Number.isFinite(value)) return 1;
  return Math.max(1, Math.min(10, Math.round(value)));
}
function minimumInputSeconds(id, fallback){
  const node = el(id);
  const value = Number(node?.value || fallback || 2);
  if (!Number.isFinite(value)) return Math.max(1, Math.round(Number(fallback || 2)));
  return Math.max(1, Math.round(value));
}
function runTimeoutSettings(){
  const settings = state.settings || {};
  return {
    curl_max_time: minimumInputSeconds('run-curl-max-time', settings.curl_max_time || 2),
    curl_max_time_quic: minimumInputSeconds('run-curl-max-time-quic', settings.curl_max_time_quic || 2),
    curl_max_time_doh: minimumInputSeconds('run-curl-max-time-doh', settings.curl_max_time_doh || 2)
  };
}
function discoveryOptions(){
  const timeouts = runTimeoutSettings();
  return {
    enable_http: el('enable-http').checked,
    enable_tls12: el('enable-tls12').checked,
    enable_tls13: el('enable-tls13').checked,
    include_quic: el('include-quic').checked,
    enable_ipv6: el('enable-ipv6').checked,
    scan_level: el('scan-level').value || 'standard',
    repeats: repeatsValue(),
    repeat_parallel: el('repeat-parallel').checked,
    skip_dnscheck: el('skip-dnscheck').checked,
    skip_ipblock: el('skip-ipblock').checked,
    ...timeouts
  };
}
function selectedFinderPresetSummary(){
  const value = el('finder-preset-select')?.value || CUSTOM_SELECT_VALUE;
  if (value === CUSTOM_SELECT_VALUE) return 'ручной список';
  const [kind, name] = String(value || '').split(':');
  if (kind === 'system') {
    return `${systemPresetLabel('finder', name)} (${systemPresetCount('finder', name)})`;
  }
  if (kind === 'custom') {
    return `Пользовательский: ${name} (${customPresetCount('finder', name)})`;
  }
  if (kind === 'builtin') {
    const preset = builtInPresets('finder').find((item) => item.key === name);
    return preset ? `${preset.label} (${uniqueDomainCount(preset.domains)})` : name || '-';
  }
  return value || '-';
}
function selectedRunModeLabel(){
  return selectedRunMode() === 'multi' ? 'Все домены на одной стратегии' : 'Домены по очереди';
}
function protocolSummary(options){
  const protocols = [];
  if (options.enable_http) protocols.push('HTTP');
  if (options.enable_tls12) protocols.push('TLS 1.2');
  if (options.enable_tls13) protocols.push('TLS 1.3');
  if (options.include_quic) protocols.push('QUIC');
  return protocols.join(' + ') || 'не выбран';
}
function runLaunchReadiness(domains, options){
  const status = state.status || {};
  const ready = zapretCompactStatus(status.zapret2 || {}).ready;
  if (isBusy()) return { text: 'Идет подбор', tone: 'warn' };
  if (!ready) return { text: 'Требуется настройка', tone: 'warn' };
  if (!domains.length) return { text: 'Нужны домены', tone: 'warn' };
  if (!hasEnabledProtocol(options)) return { text: 'Нужен протокол', tone: 'warn' };
  return { text: 'Готово к старту', tone: 'good' };
}
function runLaunchSummaryItems(){
  const domains = finderDomains();
  const options = discoveryOptions();
  const settings = state.settings || {};
  const mode = selectedRunMode();
  const limit = timeoutSecondsOrNull();
  const checks = [
    options.skip_dnscheck ? 'DNS: пропуск' : 'DNS: проверять',
    options.skip_ipblock ? 'IP/port: пропуск' : 'IP/port: проверять'
  ].join(', ');
  const repeats = `${options.repeats} · ${options.repeat_parallel ? 'параллельно' : 'последовательно'}`;
  const curl = mode === 'multi' ? `${curlParallelism()} параллельно` : 'не применяется';
  const timeouts = runTimeoutSettings();
  const timeoutText = `HTTP/TLS ${timeouts.curl_max_time}с · QUIC ${timeouts.curl_max_time_quic}с · DoH ${timeouts.curl_max_time_doh}с`;
  return {
    readiness: runLaunchReadiness(domains, options),
    items: [
      ['Домены запуска', `${domains.length}`],
      ['Обязательные', `${systemPresetCount('finder', 'required')}`],
      ['Желательные', `${systemPresetCount('finder', 'desired')}`],
      ['Источник', selectedFinderPresetSummary()],
      ['Режим', selectedRunModeLabel()],
      ['Проверочные запросы', curl],
      ['Протоколы', protocolSummary(options)],
      ['IP-режим', options.enable_ipv6 ? 'IPv4 + IPv6' : 'IPv4'],
      ['Глубина', scanLevelLabel(options.scan_level || 'standard')],
      ['DNS/IP-check', checks],
      ['Повторы', repeats],
      ['Лимит времени', limit ? formatDuration(limit) : 'без лимита'],
      ['Таймауты', timeoutText]
    ]
  };
}
function renderRunLaunchSummary(){
  const grid = el('run-launch-summary-grid');
  const badgeNode = el('run-launch-readiness');
  if (!grid || !badgeNode) return;
  const summary = runLaunchSummaryItems();
  badgeNode.textContent = summary.readiness.text;
  badgeNode.className = `badge ${summary.readiness.tone}`;
  grid.innerHTML = summary.items.map(([label, value]) => `<div class="run-launch-summary-item">
    <div class="run-launch-summary-label">${esc(label)}</div>
    <div class="run-launch-summary-value">${esc(value)}</div>
  </div>`).join('');
}
function collectRunPreferences(){
  const timeoutHours = Number(el('finder-timeout-hours')?.value || 6);
  return {
    domains: selectedFinderDomains(),
    domain_preset: el('finder-preset-select')?.value || CUSTOM_SELECT_VALUE,
    discovery_profile: el('discovery-profile-select')?.value || CUSTOM_SELECT_VALUE,
    run_mode: selectedRunMode(),
    curl_parallelism: curlParallelism(),
    ...discoveryOptions(),
    limit_time_enabled: Boolean(el('limit-time-enabled')?.checked),
    timeout_hours: Number.isFinite(timeoutHours) ? timeoutHours : 6
  };
}
function useRunPreferencesOnce(){
  if (state.runPreferencesApplied || !state.runPreferences) return;
  const prefs = state.runPreferences || {};
  state.loadingRunPreferences = true;
  try {
    const domains = Array.isArray(prefs.domains) ? uniqueDomains(prefs.domains) : [];
    const presetSelect = el('finder-preset-select');
    const presetValue = String(prefs.domain_preset || 'system:required');
    if (presetSelect && [...presetSelect.options].some((option) => option.value === presetValue)) {
      presetSelect.value = presetValue;
    }
    if (domains.length) {
      el('finder-domains').value = domains.join('\\n');
      state.domainsTouched = presetSelect?.value === CUSTOM_SELECT_VALUE;
      state.domainsInitialized = true;
    } else if (presetSelect && presetSelect.value !== CUSTOM_SELECT_VALUE) {
      const presetDomainsList = uniqueDomains(presetDomains('finder', presetSelect.value));
      if (presetDomainsList.length) {
        el('finder-domains').value = presetDomainsList.join('\\n');
        state.domainsTouched = false;
        state.domainsInitialized = true;
      }
    }
    updateEditorLineNumbers('finder-domains');

    const discoverySelect = el('discovery-profile-select');
    if (discoverySelect) {
      const value = String(prefs.discovery_profile || 'standard');
      discoverySelect.value = [...discoverySelect.options].some((option) => option.value === value) ? value : 'standard';
    }
    const runMode = String(prefs.run_mode || 'standard') === 'multi' ? 'multi' : 'standard';
    const runModeInput = document.querySelector(`input[name="run-mode"][value="${runMode}"]`);
    if (runModeInput) runModeInput.checked = true;
    el('curl-parallelism').value = String(prefs.curl_parallelism || 4);
    el('enable-http').checked = Boolean(prefs.enable_http);
    el('enable-tls12').checked = Boolean(prefs.enable_tls12 ?? true);
    el('enable-tls13').checked = Boolean(prefs.enable_tls13);
    el('include-quic').checked = Boolean(prefs.include_quic ?? true);
    el('enable-ipv6').checked = Boolean(prefs.enable_ipv6);
    el('scan-level').value = prefs.scan_level || 'standard';
    el('repeats').value = String(prefs.repeats || 1);
    el('repeat-parallel').checked = Boolean(prefs.repeat_parallel);
    el('skip-dnscheck').checked = Boolean(prefs.skip_dnscheck ?? true);
    el('skip-ipblock').checked = Boolean(prefs.skip_ipblock ?? true);
    el('limit-time-enabled').checked = Boolean(prefs.limit_time_enabled);
    el('finder-timeout-hours').value = String(prefs.timeout_hours || 6);
    el('run-curl-max-time').value = String((state.settings || {}).curl_max_time || 2);
    el('run-curl-max-time-quic').value = String((state.settings || {}).curl_max_time_quic || 2);
    el('run-curl-max-time-doh').value = String((state.settings || {}).curl_max_time_doh || 2);
    syncTimeLimitUi();
    renderDiscoveryProfileNote();
    renderRunModeNote();
  } finally {
    state.loadingRunPreferences = false;
    state.runPreferencesApplied = true;
  }
}
async function saveRunPreferencesNow(){
  if (!state.runPreferencesApplied || state.loadingRunPreferences || state.savingRunPreferences) return;
  state.savingRunPreferences = true;
  const payload = collectRunPreferences();
  try {
    const data = await postJson('/api/run-preferences', { run_preferences: payload });
    state.runPreferences = (data || {}).run_preferences || payload;
  } catch (_error) {
    // Best-effort persistence: the run itself must not fail because UI state was not saved.
  } finally {
    state.savingRunPreferences = false;
  }
}
const DISCOVERY_PROFILE_CONTROL_IDS = new Set(['scan-level']);
const RUN_TIMEOUT_CONTROL_IDS = new Set(['run-curl-max-time', 'run-curl-max-time-quic', 'run-curl-max-time-doh']);
const RUN_LAUNCH_SUMMARY_CONTROL_IDS = new Set([
  'finder-domains',
  'finder-preset-select',
  'curl-parallelism',
  'enable-http',
  'enable-tls12',
  'enable-tls13',
  'include-quic',
  'enable-ipv6',
  'discovery-profile-select',
  'scan-level',
  'repeats',
  'repeat-parallel',
  'skip-dnscheck',
  'skip-ipblock',
  'limit-time-enabled',
  'finder-timeout-hours',
  'run-curl-max-time',
  'run-curl-max-time-quic',
  'run-curl-max-time-doh'
]);
const MUTATING_ACTIONS = new Set([
  'save-settings',
  'check-releases',
  'update-from-release',
  'create-backup',
  'upload-backup',
  'preset-editor-save',
  'preset-editor-delete',
  'preset-new-save',
  'v2fly-load-categories',
  'v2fly-preview',
  'v2fly-import'
]);
function isRunLaunchSummaryControl(target){
  if (!target) return false;
  if (target.name === 'run-mode') return true;
  return RUN_LAUNCH_SUMMARY_CONTROL_IDS.has(String(target.id || ''));
}
function markDiscoveryProfileCustom(){
  if (state.loadingDiscoveryProfile) return;
  renderDiscoveryProfileNote();
}
function useDiscoveryProfile(profile){
  if (!profile) return;
  state.loadingDiscoveryProfile = true;
  try {
    el('scan-level').value = profile.scan_level || 'standard';
    renderDiscoveryProfileNote();
  } finally {
    state.loadingDiscoveryProfile = false;
  }
}
function renderDiscoveryProfileNote(){
  const note = el('discovery-profile-note');
  if (!note) return;
  const select = el('discovery-profile-select');
  const profile = select ? (state.discoveryProfiles || {})[select.value] : null;
  const scanLevel = String(profile?.scan_level || el('scan-level')?.value || 'standard');
  const title = profileTitle(scanLevel, profile);
  const details = {
    quick: 'меньше комбинаций, быстрее первичная проверка.',
    standard: 'основной режим для обычного подбора.',
    force: 'больше комбинаций, работает дольше.'
  }[scanLevel] || 'настройки изменены вручную.';
  note.textContent = `${title}: ${details}`;
}
function selectedRunMode(){
  return document.querySelector('input[name="run-mode"]:checked')?.value || 'standard';
}
function renderRunModeNote(){
  const note = el('run-mode-note');
  if (!note) return;
  const mode = selectedRunMode();
  const curlField = el('multi-curl-field');
  if (curlField) curlField.hidden = mode !== 'multi';
  if (mode === 'multi') {
    note.textContent = 'Режим “Все домены на одной стратегии”: одна стратегия запускается один раз, затем домены проверяются параллельно.';
    return;
  }
  note.textContent = 'Обычный режим: штатная проверка стратегий проходит по своему порядку.';
}
function profileTitle(name, profile){
  return String((profile && profile.title) || name || '-');
}
function renderDiscoveryProfiles(){
  const select = el('discovery-profile-select');
  if (!select) return;
  const current = select.value;
  const profiles = state.discoveryProfiles || {};
  const names = Object.keys(profiles).sort((a, b) => profileTitle(a, profiles[a]).localeCompare(profileTitle(b, profiles[b])));
  select.innerHTML = names.map((name) => `<option value="${esc(name)}">${esc(profileTitle(name, profiles[name]))}</option>`).join('');
  if (current && profiles[current]) select.value = current;
  else if (profiles.standard) select.value = 'standard';
  else if (names.length) select.value = names[0];
  renderDiscoveryProfileNote();
}
function hasEnabledProtocol(options){
  return Boolean(options.enable_http || options.enable_tls12 || options.enable_tls13 || options.include_quic);
}
function parseDomains(raw){
  return [...new Set(String(raw || '').split(/[,\\s]+/).map((item) => item.trim()).filter(Boolean))];
}
function loadCustomPresets(){
  try {
    const parsed = JSON.parse(localStorage.getItem(CUSTOM_PRESETS_KEY) || '{}');
    return {
      finder: parsed && typeof parsed.finder === 'object' && parsed.finder ? parsed.finder : {},
      common: parsed && typeof parsed.common === 'object' && parsed.common ? parsed.common : {}
    };
  } catch (_error) {
    return { finder: {}, common: {} };
  }
}
function persistCustomPresets(){
  localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify(state.customPresets));
  fetch('/api/presets', {
    method: 'POST',
    headers: authHeaders({'Content-Type': 'application/json'}),
    credentials: 'same-origin',
    body: JSON.stringify({custom: state.customPresets})
  }).catch(() => {});
}
function mergeCustomPresets(remote, metadata){
  const result = { finder: {}, common: {} };
  for (const scope of ['finder', 'common']) {
    result[scope] = {
      ...((remote && typeof remote[scope] === 'object') ? remote[scope] : {}),
      ...((state.customPresets && typeof state.customPresets[scope] === 'object') ? state.customPresets[scope] : {})
    };
  }
  state.customPresets = result;
  state.customPresetMeta = normalizeCustomPresetMeta(metadata, state.customPresets);
  localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify(state.customPresets));
}
function mergeSystemPresets(remote, metadata){
  const result = { finder: {}, common: {} };
  for (const scope of ['finder', 'common']) {
    result[scope] = (remote && typeof remote[scope] === 'object' && remote[scope]) ? remote[scope] : {};
  }
  state.systemPresets = result;
  state.systemPresetMeta = normalizePresetMeta(metadata, state.systemPresets, 'system');
}
function normalizeCustomPresetMeta(metadata, presets){
  return normalizePresetMeta(metadata, presets, 'user');
}
function normalizePresetMeta(metadata, presets, fallbackKind){
  const result = { finder: {}, common: {} };
  for (const scope of ['finder', 'common']) {
    const remote = metadata && typeof metadata[scope] === 'object' ? metadata[scope] : {};
    Object.entries(remote).forEach(([name, meta]) => {
      result[scope][name] = {
        name,
        kind: (meta && meta.kind) || fallbackKind,
        label: (meta && meta.label) || name,
        enabled_count: Number((meta && meta.enabled_count) || 0),
        total_count: Number((meta && meta.total_count) || 0),
        updated_at: (meta && meta.updated_at) || ''
      };
    });
    Object.entries((presets && presets[scope]) || {}).forEach(([name, domains]) => {
      if (!result[scope][name]) {
        const count = uniqueDomainCount(domains);
        result[scope][name] = { name, kind: fallbackKind, label: name, enabled_count: count, total_count: count, updated_at: '' };
      }
    });
  }
  return result;
}
function customPresetNames(target){
  const scopes = presetScopesForTarget(target);
  return [...new Set([
    ...scopes.flatMap((scope) => Object.keys((state.customPresetMeta && state.customPresetMeta[scope]) || {})),
    ...scopes.flatMap((scope) => Object.keys((state.customPresets && state.customPresets[scope]) || {}))
  ])].filter((name) => !hasSystemPreset(target, name)).sort((a, b) => a.localeCompare(b));
}
function presetScopesForTarget(target){
  return target === 'common' ? ['common', 'finder'] : ['finder', 'common'];
}
function customPresetSourceScope(target, name){
  for (const scope of presetScopesForTarget(target)) {
    if ((state.customPresetMeta[scope] || {})[name] || (state.customPresets[scope] || {})[name]) return scope;
  }
  return target || 'finder';
}
function customPresetCount(target, name){
  const scope = customPresetSourceScope(target, name);
  const meta = (state.customPresetMeta[scope] || {})[name];
  if (meta) return Number(meta.enabled_count || 0);
  return uniqueDomainCount((state.customPresets[scope] || {})[name] || []);
}
function hasCustomPreset(target, name){
  const scope = customPresetSourceScope(target, name);
  return Boolean((state.customPresetMeta[scope] || {})[name] || (state.customPresets[scope] || {})[name]);
}
function systemPresetNames(target){
  return [...new Set([
    ...Object.keys((state.systemPresetMeta && state.systemPresetMeta[target]) || {}),
    ...Object.keys((state.systemPresets && state.systemPresets[target]) || {})
  ])].sort((a, b) => systemPresetLabel(target, a).localeCompare(systemPresetLabel(target, b)));
}
function systemPresetMeta(target, name){
  return ((state.systemPresetMeta && state.systemPresetMeta[target]) || {})[name] || null;
}
function systemPresetLabel(target, name){
  const meta = systemPresetMeta(target, name);
  return (meta && meta.label) || name;
}
function systemPresetCount(target, name){
  const meta = systemPresetMeta(target, name);
  if (meta) return Number(meta.enabled_count || 0);
  return uniqueDomainCount((state.systemPresets[target] || {})[name] || []);
}
function hasSystemPreset(target, name){
  return Boolean(systemPresetMeta(target, name) || (state.systemPresets[target] || {})[name]);
}
function mergePresetResponse(data){
  mergeCustomPresets((data || {}).custom || {}, (data || {}).metadata || {});
  mergeSystemPresets((data || {}).system || {}, (data || {}).system_metadata || {});
}
function builtInPresets(target){
  const groups = presetGroups(target);
  const presets = groups.flatMap((group) => group.presets);
  return presets;
}
function presetGroups(target){
  const sets = state.domainSets || {};
  const make = (key, label) => ({ key, label, domains: defaultDomains(key) });
  const groups = [];
  if (target === 'common') {
    const tested = testedDomains();
    if (tested.length) {
      groups.push({
        label: 'Протестированные',
        presets: [{ key: 'tested', label: 'Все протестированные', domains: tested }]
      });
    }
  }
  groups.push({
    label: 'Обязательные',
    presets: [
      make('critical', 'Критичные')
    ].filter((preset) => preset.domains.length)
  });
  groups.push({
    label: 'Сервисы',
    presets: [
      make('google-youtube', 'Google / YouTube'),
      make('discord', 'Discord'),
      make('cloudflare', 'Cloudflare'),
      make('amazon-aws', 'Amazon / AWS')
    ].filter((preset) => preset.domains.length)
  });
  groups.push({
    label: 'Готовые наборы',
    presets: [
      make('coverage', 'Покрытие'),
      { key: 'all', label: 'Все встроенные', domains: defaultDomains('all') }
    ].filter((preset) => preset.domains.length)
  });
  const known = new Set(groups.flatMap((group) => group.presets.map((preset) => preset.key)));
  const other = Object.keys(sets)
    .filter((key) => !known.has(key))
    .sort()
    .map((key) => make(key, key))
    .filter((preset) => preset.domains.length);
  if (other.length) groups.push({ label: 'Другие', presets: other });
  if (target === 'common') {
    return groups.filter((group) => group.presets.length);
  }
  return groups.filter((group) => group.presets.length);
}
function presetDomains(target, value){
  const [scope, key] = String(value || '').split(':');
  if (scope === 'system') {
    return state.systemPresets[target]?.[key] || [];
  }
  if (scope === 'builtin') {
    const preset = builtInPresets(target).find((item) => item.key === key);
    return preset ? preset.domains : [];
  }
  if (scope === 'custom') {
    const sourceScope = customPresetSourceScope(target, key);
    return state.customPresets[sourceScope]?.[key] || [];
  }
  return [];
}
function managerPresetEntries(){
  const target = 'finder';
  const system = systemPresetNames(target).map((name) => ({
    name,
    label: systemPresetLabel(target, name),
    count: systemPresetCount(target, name),
    kind: 'system'
  }));
  const custom = customPresetNames(target).map((name) => ({
    name,
    label: name,
    count: customPresetCount(target, name),
    kind: 'user'
  })).filter((item) => !hasSystemPreset(target, item.name));
  const seen = new Set([...system, ...custom].map((item) => item.name));
  const builtin = presetGroups(target)
    .flatMap((group) => group.presets.map((preset) => ({
      name: preset.key,
      label: preset.label,
      count: uniqueDomainCount(preset.domains),
      kind: 'builtin'
    })))
    .filter((item) => item.count > 0 && !seen.has(item.name));
  return [...system, ...custom, ...builtin].sort((a, b) => {
    const rank = { system: 0, user: 1, builtin: 2 };
    const diff = (rank[a.kind] ?? 9) - (rank[b.kind] ?? 9);
    if (diff) return diff;
    return a.label.localeCompare(b.label);
  });
}
function managerPresetEntry(name){
  return managerPresetEntries().find((item) => item.name === name) || null;
}
function renderPresetSelect(target){
  const select = el(`${target}-preset-select`);
  if (!select) return;
  const previous = select.value;
  const systemEntries = systemPresetNames(target);
  const systemGroup = systemEntries.length
    ? `<optgroup label="Системные">${systemEntries.map((name) => `<option value="system:${esc(name)}">${esc(systemPresetLabel(target, name))} (${systemPresetCount(target, name)})</option>`).join('')}</optgroup>`
    : '';
  const customEntries = customPresetNames(target);
  const customGroup = customEntries.length
    ? `<optgroup label="Персональные">${customEntries.map((name) => `<option value="custom:${esc(name)}">${esc(name)} (${customPresetCount(target, name)})</option>`).join('')}</optgroup>`
    : '';
  const builtInGroups = presetGroups(target).map((group) => {
    const options = group.presets.map((preset) => `<option value="builtin:${esc(preset.key)}">${esc(preset.label)} (${uniqueDomainCount(preset.domains)})</option>`).join('');
    return `<optgroup label="${esc(group.label)}">${options}</optgroup>`;
  }).join('');
  select.innerHTML = `<option value="${CUSTOM_SELECT_VALUE}">Custom</option>${systemGroup}${customGroup}${builtInGroups}`;
  if ([...select.options].some((option) => option.value === previous)) select.value = previous;
  else if (target === 'common') select.value = CUSTOM_SELECT_VALUE;
  else if (!previous && [...select.options].some((option) => option.value === 'system:required')) select.value = 'system:required';
  else if (!previous && [...select.options].some((option) => option.value === 'builtin:critical')) select.value = 'builtin:critical';
  else select.value = CUSTOM_SELECT_VALUE;
}
function renderPresetSelects(){
  renderPresetSelect('finder');
  renderPresetSelect('common');
}
function markDomainPresetCustom(target){
  if (state.loadingDomainPreset) return;
  const select = el(`${target}-preset-select`);
  if (select && select.value !== CUSTOM_SELECT_VALUE) select.value = CUSTOM_SELECT_VALUE;
  const nameInput = el(`${target}-preset-name`);
  if (nameInput) nameInput.value = 'custom';
  if (target === 'common') resetCandidateResult();
}
async function fetchAllPresetDomains(target, name){
  if (hasSystemPreset(target, name)) {
    const cached = (state.systemPresets[target] || {})[name] || [];
    const expected = systemPresetCount(target, name);
    if (expected === 0) return [];
    if (cached.length && cached.length >= expected) return uniqueDomains(cached);
    return fetchStoredPresetDomains(target, name, 'system');
  }
  if (!hasCustomPreset(target, name)) {
    const builtin = builtInPresets(target).find((item) => item.key === name);
    if (builtin) return uniqueDomains(builtin.domains);
  }
  const sourceScope = customPresetSourceScope(target, name);
  const cached = (state.customPresets[sourceScope] || {})[name] || [];
  const expected = customPresetCount(sourceScope, name);
  if (expected > 0 && cached.length && cached.length >= expected) return uniqueDomains(cached);
  return fetchStoredPresetDomains(sourceScope, name, 'user');
}
async function fetchStoredPresetDomains(sourceScope, name, kind){
  let offset = 0;
  let hasMore = true;
  let domains = [];
  let guard = 0;
  while (hasMore && guard < 1000) {
    const params = new URLSearchParams();
    params.set('scope', sourceScope);
    params.set('name', name);
    params.set('kind', kind || 'user');
    params.set('include_disabled', '0');
    params.set('limit', '500');
    params.set('offset', String(offset));
    const data = await getJson(`/api/presets/domains?${params.toString()}`);
    const rows = Array.isArray(data.domains) ? data.domains : [];
    domains = domains.concat(rows.map((row) => row.domain).filter(Boolean));
    hasMore = Boolean(data.has_more);
    offset += rows.length;
    if (!rows.length) break;
    guard += 1;
  }
  const cleanDomains = uniqueDomains(domains);
  if (kind === 'system') {
    if (!state.systemPresets[sourceScope]) state.systemPresets[sourceScope] = {};
    state.systemPresets[sourceScope][name] = cleanDomains;
    return state.systemPresets[sourceScope][name];
  }
  if (!state.customPresets[sourceScope]) state.customPresets[sourceScope] = {};
  state.customPresets[sourceScope][name] = cleanDomains;
  localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify(state.customPresets));
  return state.customPresets[sourceScope][name];
}
async function usePreset(target){
  const selected = el(`${target}-preset-select`).value;
  let domains = presetDomains(target, selected);
  if (selected.startsWith('custom:') || selected.startsWith('system:')) {
    const isSystem = selected.startsWith('system:');
    const cleanName = selected.slice((isSystem ? 'system:' : 'custom:').length);
    setMessage(isSystem ? 'Загружается системный список доменов' : 'Загружается пользовательский список доменов', 'warn');
    try {
      domains = await fetchAllPresetDomains(target, cleanName);
    } catch (error) {
      setMessage(`Ошибка загрузки списка: ${error.message}`, 'bad');
      return;
    }
  }
  const finalDomains = target === 'common' ? filterTestedDomains(domains) : domains;
  state.loadingDomainPreset = true;
  try {
    el(`${target}-domains`).value = uniqueDomains(finalDomains).join('\\n');
    updateEditorLineNumbers(`${target}-domains`);
    if (target === 'finder') state.domainsTouched = true;
    if (target === 'common') {
      state.candidateResultRequested = false;
      prepareCommonCandidateState();
      renderCandidatesOnly();
      if (selectedCommonDomains().length >= 2) refreshCandidates(true);
    }
    else {
      renderCandidates();
      renderRunLaunchSummary();
    }
  } finally {
    state.loadingDomainPreset = false;
  }
}
function presetNameForSave(target){
  const nameInput = el(`${target}-preset-name`);
  const explicit = nameInput ? nameInput.value.trim() : '';
  if (explicit) return explicit;
  const selected = el(`${target}-preset-select`).value || '';
  if (selected.startsWith('custom:')) return selected.slice('custom:'.length);
  return '';
}
async function savePreset(target){
  const name = presetNameForSave(target);
  if (!name) {
    showToast('Укажите название пользовательского пресета', 'warn');
    return;
  }
  const domains = uniqueDomains(parseDomains(el(`${target}-domains`).value));
  if (!domains.length) {
    showToast('В пресете должен быть хотя бы один домен', 'warn');
    return;
  }
  try {
    const data = await postJson('/api/presets/save', { scope: target, name, domains });
    mergePresetResponse(data);
    state.customPresets[target][name] = domains;
    localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify(state.customPresets));
    renderPresetSelect(target);
    el(`${target}-preset-select`).value = `custom:${name}`;
    renderPresetManager();
    showToast('Пресет сохранен', 'good');
    if (target === 'common') {
      state.candidateResultRequested = false;
      refreshCandidates(true);
    }
    else renderCandidates();
  } catch (error) {
    showToast(`Ошибка сохранения пресета: ${error.message}`, 'bad');
  }
}
async function deletePreset(target){
  const selected = el(`${target}-preset-select`).value || '';
  if (!selected.startsWith('custom:')) {
    showToast('Этот пресет удалить нельзя', 'warn');
    return;
  }
  const name = selected.slice('custom:'.length);
  try {
    const data = await postJson('/api/presets/delete-users-lists', { scope: target, name });
    delete state.customPresets[target][name];
    mergePresetResponse(data);
    localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify(state.customPresets));
    renderPresetSelect(target);
    renderPresetManager();
    showToast('Пресет удален', 'good');
    if (target === 'common') {
      state.candidateResultRequested = false;
      refreshCandidates(true);
    }
  } catch (error) {
    showToast(`Ошибка удаления пресета: ${error.message}`, 'bad');
  }
}
function statusCheck(label, ok, message){
  const safeMessage = String(message || '');
  return `<div class="status-check ${ok ? 'ok' : 'fail'}" title="${esc(safeMessage)}">
    <span class="status-check-body">
      <span class="status-check-label">${esc(label)}</span>
      ${safeMessage ? `<span class="status-check-message">${esc(safeMessage)}</span>` : ''}
    </span>
  </div>`;
}
function zapretDiagnostics(zapret){
  return zapretDiagnosticItems(zapret).map((item) => statusCheck(item.label || item.id || '-', Boolean(item.ok), item.message || '')).join('');
}
function zapretDiagnosticItems(zapret){
  const diagnostics = Array.isArray(zapret.diagnostics) && zapret.diagnostics.length
    ? zapret.diagnostics
    : [
        {label: 'движок применения стратегии', ok: Boolean(zapret.nfqws2_found), message: zapret.nfqws2_found ? 'найден' : 'не найден'},
        {label: 'проверка стратегий', ok: Boolean(zapret.blockcheck_found), message: zapret.blockcheck_found ? 'найдена' : 'не найдена'},
        {label: 'служба с повышенными правами', ok: Boolean(zapret.root_helper_ready), message: zapret.root_helper_ready ? 'готова' : (zapret.root_helper_error || 'не готова')}
      ];
  return diagnostics;
}
function zapretCompactStatus(zapret){
  const diagnostics = zapretDiagnosticItems(zapret);
  const total = diagnostics.length || 0;
  const ok = diagnostics.filter((item) => Boolean(item.ok)).length;
  const ready = total > 0 && ok === total;
  const tooltip = diagnostics.map((item) => {
    const mark = item.ok ? 'OK' : 'FAIL';
    return `${mark} ${item.label || item.id || '-'}: ${item.message || ''}`;
  }).join('\\n');
  return { ok, total, ready, tooltip };
}
function renderWebAuthStatus(){
  const auth = (state.status || {}).web_auth || WEB_AUTH || {};
  const node = el('web-auth-badge');
  if (!node) return;
  const enabled = Boolean(auth.enabled);
  const configured = Boolean(auth.configured);
  node.textContent = enabled && configured ? 'auth on' : 'auth off';
  node.className = `topbar-version topbar-auth ${enabled && configured ? 'on' : 'off'}`;
  node.title = enabled && configured
    ? 'Web API защищен локальным токеном'
    : 'Web API работает без локального токена';
}
function testedDomainCount(){
  const domains = new Set(Array.isArray(state.testedDomains) ? state.testedDomains : []);
  (state.candidateDomains || []).forEach((item) => {
    if (item && item.domain) domains.add(String(item.domain));
  });
  const current = Math.max(Number(state.candidateDomainTotal || 0), domains.size);
  if (current > 0) {
    state.lastCandidateDomainTotal = current;
    return current;
  }
  if (Number(state.lastCandidateDomainTotal || 0) > 0 && (isBusy() || state.candidateLoading || !state.candidateDomainsLoaded)) {
    return Number(state.lastCandidateDomainTotal || 0);
  }
  return current;
}
function nextActionStatus(ready, busy, jobStatus, status){
  const stateBoard = (status || {}).state || {};
  const normalized = String(jobStatus || '').toLowerCase();
  if (busy) {
    return normalized === 'stopping'
      ? { text: 'Останавливается', tone: 'warn' }
      : { text: 'Идет подбор', tone: 'warn' };
  }
  if (normalized === 'failed' || normalized === 'error' || stateBoard.last_error) {
    return { text: 'Есть ошибка', tone: 'bad' };
  }
  if (!ready) return { text: 'Требуется настройка', tone: 'warn' };
  return { text: 'Можно запускать', tone: 'good' };
}
function metricJobNoteText(ready, busy, jobStatus, status){
  return nextActionStatus(ready, busy, jobStatus, status).text;
}
function jobStatusClass(status, busy){
  const normalized = busy ? String(status || 'running').toLowerCase() : 'idle';
  const safe = normalized.replace(/[^a-z0-9_-]/g, '') || 'idle';
  return `metric metric-button metric-status-${safe}`;
}
function renderMetrics(){
  const status = state.status || {};
  const board = status.state || {};
  const zapret = status.zapret2 || {};
  const zapretCompact = zapretCompactStatus(zapret);
  const ready = zapretCompact.ready;
  const busy = isBusy();
  const jobStatus = board.current_job_status || (busy ? 'running' : '');
  const version = (state.status || {}).version || '-';
  const action = nextActionStatus(ready, busy, jobStatus, status);
  setText('app-version-badge', `v${version}`);
  renderWebAuthStatus();
  const zapretValue = el('metric-zapret');
  if (zapretValue) {
    zapretValue.innerHTML = `<span class="compact-status ${ready ? 'ok' : 'bad'}"><span class="compact-status-mark">${ready ? '✓' : '!'}</span><span>${ready ? 'Готова' : 'Проблема'}</span></span>`;
    zapretValue.title = zapretCompact.tooltip;
  }
  const zapretNote = el('metric-zapret-note');
  if (zapretNote) {
    zapretNote.textContent = ready ? 'службы готовы' : 'проверьте систему';
    zapretNote.title = zapretCompact.tooltip;
  }
  setText('metric-job', busy ? runStatusLabel(jobStatus) : 'Свободно');
  const jobCard = el('metric-job-card');
  if (jobCard) jobCard.className = jobStatusClass(jobStatus, busy);
  setText('metric-job-note', metricJobNoteText(ready, busy, jobStatus, status));
  const testedCount = testedDomainCount();
  setText('metric-candidates', String(testedCount));
  setText('metric-candidates-note', state.candidateDomainsLoaded ? `загружено ${state.candidateDomains.length} доменов` : 'открыть список');
  const jobBadge = el('job-badge');
  jobBadge.textContent = action.text;
  jobBadge.className = `badge ${action.tone}`;
  document.querySelectorAll('button[data-action="run-selected-discovery"]').forEach((button) => {
    button.disabled = busy;
  });
  document.querySelectorAll('button[data-action="update-from-release"], button[data-action="check-releases"]').forEach((button) => {
    button.disabled = busy;
    if (button.dataset.action === 'update-from-release') {
      button.dataset.tooltip = busy
        ? 'Обновление можно запускать только когда подбор стратегий остановлен.'
        : 'Устанавливает выбранный канал обновления только если подбор не запущен. Перед обновлением создается бекап.';
    }
  });
  const mutatingSelectors = [
    'button[data-action="save-settings"]',
    'button[data-action="create-backup"]',
    'button[data-action="upload-backup"]',
    'button[data-backup-restore]',
    'button[data-backup-delete]',
    'button[data-action="preset-editor-save"]',
    'button[data-action="preset-editor-delete"]',
    'button[data-action="preset-new-save"]',
    'button[data-action="v2fly-preview"]',
    'button[data-action="v2fly-import"]',
    'button[data-action="v2fly-load-categories"]'
  ].join(', ');
  document.querySelectorAll(mutatingSelectors).forEach((button) => {
    button.disabled = busy;
    if (busy && !button.dataset.tooltip) button.dataset.tooltip = mutatingBlockedMessage();
    if (!busy && button.dataset.tooltip === mutatingBlockedMessage()) delete button.dataset.tooltip;
  });
  document.querySelectorAll('button[data-action="stop-current"]').forEach((button) => {
    button.disabled = !busy;
  });
  const lockNote = el('mutating-lock-note');
  if (lockNote) {
    lockNote.textContent = busy
      ? mutatingBlockedMessage()
      : 'Восстановление, удаление данных, обновления и изменение настроек недоступны во время активного подбора.';
    lockNote.className = busy ? 'mutating-disabled-note' : 'helper-text';
  }
}
function renderCandidates(){
  rememberStrategyEditorScrolls();
  const isDomainView = state.candidateView === 'domain';
  const rows = isDomainView ? [] : filteredCandidates();
  const commonRows = dynamicCommonRows(rows);
  const activeRows = isDomainView ? state.candidateDomains : commonRows;
  const total = isDomainView ? state.candidateDomainTotal : (state.candidateTotal || state.candidates.length);
  setText('candidates-count', String(isDomainView ? state.candidateDomainStrategyTotal : total));
  const selectedDomains = selectedCommonDomains();
  const commonNote = state.candidateView === 'common' && selectedDomains.length >= 2 ? ` · общие для ${selectedDomains.length} доменов` : '';
  const loaded = isDomainView ? state.candidateDomainsLoaded : state.candidatesLoaded;
  const updated = friendlyTime(state.candidateUpdatedAt);
  const updatedNote = updated ? ` · обновлено ${updated}` : '';
  const loadedNote = state.candidateLoading
    ? 'Загружается...'
    : (loaded ? `Показано ${activeRows.length} из ${total}${updatedNote}` : 'Список загружается по запросу');
  setText('candidate-summary', `${loadedNote}${commonNote}`);
  document.querySelectorAll('[data-candidate-view]').forEach((button) => {
    button.classList.toggle('active', button.dataset.candidateView === state.candidateView);
    button.setAttribute('aria-selected', button.dataset.candidateView === state.candidateView ? 'true' : 'false');
  });
  renderCandidateResult();
  renderCommonControls();
  if (state.candidateView === 'common') {
    renderCommonCandidates(commonRows);
  } else {
    renderDomainCandidates();
  }
  restoreStrategyEditorScrolls();
}
function renderDomainCandidates(){
  const groups = state.candidateDomains || [];
  if (state.candidateLoading && !state.candidateDomainsLoaded) {
    el('candidates-table').innerHTML = '<div class="loading-skeleton" aria-label="Загрузка кандидатов"></div>';
    return;
  }
  if (!groups.length) {
    el('candidates-table').innerHTML = `<div class="empty">${state.candidateDomainsLoaded ? 'По фильтру ничего не найдено' : 'Откройте вкладку или обновите список, чтобы загрузить домены'}</div>`;
    return;
  }
  el('candidates-table').innerHTML = `<div class="candidate-groups">${groups.map((domainGroup) => {
    const expanded = Boolean(state.openCandidateDomains[domainGroup.domain]);
    const open = expanded ? ' open' : '';
    const protocolBadges = domainGroup.protocols.map((item) => {
      return badge(`${item.protocol}: ${item.count}`, item.protocol === 'quic' ? 'warn' : 'good');
    }).join('');
    return `<details class="domain-group" data-domain="${esc(domainGroup.domain)}"${open}>
      <summary class="domain-header">
        <div class="domain-title">${esc(domainGroup.domain)}</div>
        <div class="domain-meta">
          ${badge(`${domainGroup.strategy_count} стратегий`, '')}${protocolBadges}
        </div>
      </summary>
      ${expanded ? `<div class="domain-strategy-box">
        ${domainStrategyContent(domainGroup.domain)}
      </div>` : ''}
    </details>`;
  }).join('')}</div>${candidateDomainPager()}`;
}
function renderCommonCandidates(rows){
  const selectedDomains = selectedCommonDomains();
  if (state.candidateLoading && !state.candidatesLoaded) {
    el('candidates-table').innerHTML = '<div class="loading-skeleton" aria-label="Загрузка кандидатов"></div>';
    return;
  }
  if (selectedDomains.length < 2) {
    el('candidates-table').innerHTML = `<div class="empty">Выберите минимум два домена во вкладке Подбор, чтобы увидеть стратегии, найденные сразу для всех выбранных доменов.</div>`;
    return;
  }
  const groups = protocolGroups(rows);
  if (!groups.length) {
    el('candidates-table').innerHTML = `<div class="empty">${state.candidatesLoaded ? 'Общих стратегий для выбранных доменов пока нет. Если подбор остановлен, сюда попадут уже сохраненные стратегии, которые встречаются у каждого выбранного домена.' : 'Кандидатов пока нет'}</div>`;
    return;
  }
  el('candidates-table').innerHTML = `<div class="candidate-groups">${groups.map((protocolGroup) => {
    const domains = selectedDomains;
    const expanded = state.openCommonProtocols[protocolGroup.protocol] !== false;
    const loadedTotal = uniqueStrategyArgs(protocolGroup.rows).length;
    const remoteTotal = groups.length === 1 ? Number(state.candidateTotal || loadedTotal) : loadedTotal;
    const hasRemoteMore = groups.length === 1 && Boolean(state.candidateHasMore);
    return `<details class="domain-group" data-common-protocol="${esc(protocolGroup.protocol)}"${expanded ? ' open' : ''}>
      <summary class="domain-header">
        <div class="domain-title">${esc(protocolGroup.protocol)}</div>
        <div class="domain-meta">
          ${badge(`${loadedTotal} из ${remoteTotal} стратегий`, '')}${domains.length ? badge(`${domains.length} доменов`, 'good') : ''}
        </div>
      </summary>
      <div class="protocol-group">
        <div class="protocol-header">
        <div>${badge('COMMON', 'good')} ${domains.length ? esc(domains.join(', ')) : 'домены из проверки стратегий'}</div>
        </div>
        ${expanded ? strategyEditor(`common:${protocolGroup.protocol}:${domains.join('|')}`, protocolGroup.rows, 'Общие стратегии', {
          hasRemoteMore,
          loading: Boolean(state.commonLoadingAll),
          loadedTotal,
          remoteTotal,
          remoteLabel: 'Показать все общие стратегии'
        }) : ''}
      </div>
    </details>`;
  }).join('')}</div>${candidatePager()}`;
}
function candidateDomainPager(){
  return listLoadMore('load-more-candidate-domains', state.candidateDomainHasMore, state.candidateLoading);
}
function candidatePager(){
  return listLoadMore('load-more-candidates', state.candidateHasMore, state.candidateLoading);
}
function domainStrategyContent(domain){
  const data = state.domainStrategies[domain] || {};
  if (!data.loaded) return '<div class="empty">Стратегии домена загружаются</div>';
  const rows = data.candidates || [];
  if (!rows.length) return '<div class="empty">Для домена нет загруженных стратегий</div>';
  const groups = protocolGroups(rows);
  const grouped = groups.map((protocolGroup) => {
    const key = `domain:${domain}:${protocolGroup.protocol}`;
    const total = uniqueStrategyArgs(protocolGroup.rows).length;
    return `<section class="protocol-group">
      <div class="protocol-header">
        <div>${badge(protocolGroup.protocol, protocolGroup.protocol === 'quic' ? 'warn' : 'good')}</div>
        <div class="helper-text">${total} стратегий</div>
      </div>
      ${strategyEditor(key, protocolGroup.rows, `Стратегии ${protocolGroup.protocol}`, {
        hasRemoteMore: Boolean(data.hasMore),
        loading: Boolean(data.loadingAll),
        loadedTotal: rows.length,
        remoteTotal: Number(data.total || rows.length)
      })}
    </section>`;
  }).join('');
  return grouped;
}
function filteredCandidates(){
  return state.candidates;
}
function candidateDomains(row){
  const seen = Array.isArray(row.seen) ? row.seen : [];
  return [...new Set(seen.map((item) => String(item.domain || '').trim()).filter(Boolean))];
}
function commonSeen(row){
  return Array.isArray(row.common_seen) ? row.common_seen : [];
}
function commonDomains(row){
  return [...new Set(commonSeen(row).flatMap((item) => Array.isArray(item.domains) ? item.domains : []).map((item) => String(item || '').trim()).filter(Boolean))];
}
function candidateAllDomains(row){
  return [...new Set([...candidateDomains(row), ...commonDomains(row)])];
}
function testedDomains(){
  if (Array.isArray(state.testedDomains) && state.testedDomains.length) return state.testedDomains;
  return [...new Set(state.candidates.flatMap((row) => candidateAllDomains(row)))].sort((a, b) => a.localeCompare(b));
}
function updateTestedDomains(domains){
  if (!Array.isArray(domains)) return false;
  const next = uniqueDomains(domains);
  const previous = Array.isArray(state.testedDomains) ? state.testedDomains : [];
  const changed = next.length !== previous.length || next.some((domain, index) => domain !== previous[index]);
  state.testedDomains = next;
  if (changed) renderPresetSelect('common');
  return changed;
}
function candidateResultModeLabel(mode){
  return {
    coverage: 'Максимум покрытия',
    minimal: 'Минимум стратегий',
    balance: 'Баланс'
  }[mode] || 'Баланс';
}
function candidateResultTargets(){
  const required = uniqueDomains(presetDomains('finder', 'system:required'));
  const desired = uniqueDomains(presetDomains('finder', 'system:desired')).filter((domain) => !required.includes(domain));
  return {
    required,
    desired
  };
}
function commonCandidateResultRows(){
  return uniqueStrategyRows(Array.isArray(state.candidates) ? state.candidates : []);
}
function rowTargetCoverage(row, targets){
  const domains = new Set(candidateAllDomains(row));
  return targets.filter((domain) => domains.has(domain));
}
function resultPickScore(row, uncoveredRequired, uncoveredDesired, mode){
  const requiredGain = rowTargetCoverage(row, [...uncoveredRequired]).length;
  const desiredGain = rowTargetCoverage(row, [...uncoveredDesired]).length;
  const complexity = strategyComplexity(row);
  if (mode === 'coverage') return (requiredGain + desiredGain) * 10000 + strategyDomainCoverage(row) * 10 - complexity;
  if (mode === 'minimal') return (requiredGain + desiredGain) * 10000 - complexity * 5;
  return requiredGain * 100000 + desiredGain * 1000 - complexity;
}
function buildCandidateResult(mode){
  const targets = candidateResultTargets();
  const rows = commonCandidateResultRows();
  const uncoveredRequired = new Set(targets.required);
  const uncoveredDesired = new Set(targets.desired);
  const selected = [];
  const remaining = rows.slice();
  while ((uncoveredRequired.size || uncoveredDesired.size) && remaining.length) {
    remaining.sort((a, b) => resultPickScore(b, uncoveredRequired, uncoveredDesired, mode) - resultPickScore(a, uncoveredRequired, uncoveredDesired, mode));
    const best = remaining.shift();
    if (!best) break;
    const requiredHit = rowTargetCoverage(best, [...uncoveredRequired]);
    const desiredHit = rowTargetCoverage(best, [...uncoveredDesired]);
    if (!requiredHit.length && !desiredHit.length) continue;
    selected.push({ row: best, requiredHit, desiredHit });
    requiredHit.forEach((domain) => uncoveredRequired.delete(domain));
    desiredHit.forEach((domain) => uncoveredDesired.delete(domain));
    if (mode === 'minimal' && !uncoveredRequired.size && !uncoveredDesired.size) break;
  }
  const coveredRequired = targets.required.filter((domain) => !uncoveredRequired.has(domain));
  const coveredDesired = targets.desired.filter((domain) => !uncoveredDesired.has(domain));
  const modeLabel = candidateResultModeLabel(mode);
  const targetCount = targets.required.length + targets.desired.length;
  const reason = !targetCount
    ? 'Нет обязательных или желательных доменов для расчета итогового набора.'
    : selected.length
    ? `${modeLabel}: покрыто ${coveredRequired.length}/${targets.required.length} обязательных и ${coveredDesired.length}/${targets.desired.length} желательных доменов по загруженным стратегиям.`
    : 'Нет загруженных стратегий, которые покрывают выбранные домены.';
  return {
    required_coverage: { covered: coveredRequired.length, total: targets.required.length },
    desired_coverage: { covered: coveredDesired.length, total: targets.desired.length },
    uncovered_required: [...uncoveredRequired],
    uncovered_desired: [...uncoveredDesired],
    strategy_set: selected.map((item) => ({
      args: String(item.row.args || '').trim(),
      protocol: String(item.row.protocol || '-'),
      domains: uniqueDomains([...item.requiredHit, ...item.desiredHit])
    })),
    reason,
    mode: modeLabel,
    loaded_rows: rows.length,
    targets
  };
}
function candidateResultText(result){
  const lines = (result.strategy_set || []).map((item) => item.args).filter(Boolean);
  return lines.join('\\n');
}
function resetCandidateResult(){
  state.candidateResultRequested = false;
  renderCandidateResult();
}
async function buildCandidateResultNow(){
  state.candidateResultRequested = true;
  if (state.candidateView !== 'common') state.candidateView = 'common';
  const selectedDomains = selectedCommonDomains();
  const loaded = prepareCommonCandidateState();
  renderCandidatesOnly();
  if (selectedDomains.length >= 2 && !loaded) {
    await refreshCandidates(true);
  }
}
function renderCandidateResult(){
  const panel = document.querySelector('.candidate-result-panel');
  const body = el('candidate-result-body');
  const source = el('candidate-result-source');
  if (panel) panel.hidden = state.candidateView !== 'common';
  if (!body) return;
  const mode = state.candidateResultMode || 'balance';
  document.querySelectorAll('[data-candidate-result-mode]').forEach((button) => {
    const active = button.dataset.candidateResultMode === mode;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  body.setAttribute('aria-labelledby', `candidate-result-mode-${mode}`);
  if (state.candidateView !== 'common') return;
  if (!state.candidateResultRequested) {
    if (source) source.textContent = 'Выберите домены для пересечения и соберите итоговый набор.';
    body.innerHTML = '<div class="empty">Нажмите «Собрать итоговый набор» после выбора доменов.</div>';
    return;
  }
  const selectedDomains = selectedCommonDomains();
  if (selectedDomains.length < 2) {
    if (source) source.textContent = 'Для итогового набора нужны минимум два протестированных домена.';
    body.innerHTML = '<div class="empty">Выберите минимум два домена в пресете доменов для пересечения.</div>';
    return;
  }
  const result = buildCandidateResult(mode);
  const rows = Number(result.loaded_rows || 0);
  const requiredTotal = Number(result.required_coverage.total || 0);
  const desiredTotal = Number(result.desired_coverage.total || 0);
  if (source) {
    source.textContent = `Расчет по загруженным общим стратегиям: ${rows}. Обязательные: ${requiredTotal}. Желательные: ${desiredTotal}.`;
  }
  if (!rows) {
    body.innerHTML = '<div class="empty">Для выбранного пересечения пока нет загруженных общих стратегий.</div>';
    return;
  }
  const strategies = result.strategy_set || [];
  const strategiesHtml = strategies.length
    ? `<div class="candidate-result-strategies">${strategies.map((item) => `<div class="candidate-result-strategy">
        <code>${esc(item.args || '-')}</code>
        <div class="candidate-result-domains">${esc(item.protocol || '-')} · ${esc((item.domains || []).join(', ') || '-')}</div>
      </div>`).join('')}</div>`
    : '<div class="empty">По загруженным стратегиям нет покрытия выбранных доменов.</div>';
  body.innerHTML = `<div class="candidate-result-grid">
    <div class="candidate-result-cell">
      <div class="candidate-result-label">mode</div>
      <div class="candidate-result-value">${esc(result.mode)}</div>
    </div>
    <div class="candidate-result-cell">
      <div class="candidate-result-label">required_coverage</div>
      <div class="candidate-result-value">${result.required_coverage.covered} / ${result.required_coverage.total}</div>
    </div>
    <div class="candidate-result-cell">
      <div class="candidate-result-label">desired_coverage</div>
      <div class="candidate-result-value">${result.desired_coverage.covered} / ${result.desired_coverage.total}</div>
    </div>
    <div class="candidate-result-cell">
      <div class="candidate-result-label">strategy_set</div>
      <div class="candidate-result-value">${strategies.length}</div>
    </div>
  </div>
  <div class="helper-text">${esc(result.reason)}</div>
  <details class="candidate-result-details" open>
    <summary>Детали итогового набора</summary>
    <div class="helper-text">uncovered_required: ${esc(result.uncovered_required.join(', ') || '-')}</div>
    <div class="helper-text">uncovered_desired: ${esc(result.uncovered_desired.join(', ') || '-')}</div>
    ${strategiesHtml}
  </details>
  <div class="candidate-result-actions">
    <button class="secondary" data-action="copy-candidate-result" type="button"${strategies.length ? '' : ' disabled'}>Скопировать для zapret2</button>
    <button class="secondary" data-action="export-candidate-result" type="button"${strategies.length ? '' : ' disabled'}>Экспорт TXT</button>
    <button class="secondary" data-action="use-candidate-result-domains" type="button">Повторить подбор</button>
    <button class="secondary" data-action="open-candidate-result" type="button">Открыть детали</button>
  </div>`;
}
async function copyCandidateResult(){
  const result = buildCandidateResult(state.candidateResultMode || 'balance');
  const text = candidateResultText(result);
  if (!text) {
    setMessage('В итоговом наборе нет стратегий для копирования', 'warn');
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
    setMessage('Итоговый набор скопирован', 'good');
  } catch (error) {
    setMessage(`Не удалось скопировать итоговый набор: ${error.message}`, 'bad');
  }
}
function exportCandidateResult(){
  const result = buildCandidateResult(state.candidateResultMode || 'balance');
  const text = candidateResultText(result);
  if (!text) {
    setMessage('В итоговом наборе нет стратегий для экспорта', 'warn');
    return;
  }
  const blob = new Blob([text + '\\n'], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = 'gp-candidate-result.txt';
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
function useCandidateResultDomains(){
  const result = buildCandidateResult(state.candidateResultMode || 'balance');
  const domains = uniqueDomains([...(result.targets.required || []), ...(result.targets.desired || [])]);
  if (!domains.length) {
    setMessage('Нет доменов для повторного запуска', 'warn');
    return;
  }
  el('finder-domains').value = domains.join('\\n');
  state.domainsTouched = true;
  markDomainPresetCustom('finder');
  updateEditorLineNumbers('finder-domains');
  renderRunLaunchSummary();
  setActiveTab('finder');
  setMessage('Домены итогового набора перенесены в форму запуска. Старт выполните вручную.', 'good');
}
function openCandidateResultDetails(){
  const details = document.querySelector('.candidate-result-details');
  if (details) details.open = true;
}
function filterTestedDomains(domains){
  const tested = new Set(testedDomains());
  return [...new Set(domains)].filter((domain) => tested.has(domain));
}
function selectedCommonDomains(){
  const node = el('common-domains');
  if (!node) return [];
  return filterTestedDomains(parseDomains(node.value));
}
function commonDomainSuggestions(query){
  const needle = String(query || '').trim().toLowerCase();
  if (!needle) return [];
  const selected = new Set(parseDomains(el('common-domains').value));
  return testedDomains()
    .filter((domain) => !selected.has(domain))
    .filter((domain) => domain.toLowerCase().includes(needle))
    .sort((a, b) => {
      const aStarts = a.toLowerCase().startsWith(needle);
      const bStarts = b.toLowerCase().startsWith(needle);
      if (aStarts !== bStarts) return aStarts ? -1 : 1;
      return a.localeCompare(b);
    })
    .slice(0, 8);
}
function renderCommonDomainSuggestions(){
  const input = el('common-domain-add');
  const target = el('common-domain-suggestions');
  if (!input || !target || state.candidateView !== 'common') return;
  const value = String(input.value || '');
  const rows = commonDomainSuggestions(value);
  if (!value.trim()) {
    target.hidden = true;
    target.innerHTML = '';
    return;
  }
  target.hidden = false;
  target.innerHTML = rows.length
    ? rows.map((domain) => `<button class="domain-suggestion" data-common-domain-suggestion="${esc(domain)}" type="button" role="option">${esc(domain)}</button>`).join('')
    : '<div class="domain-suggestion-empty">Совпадений среди протестированных доменов нет</div>';
}
function hideCommonDomainSuggestions(){
  const target = el('common-domain-suggestions');
  if (!target) return;
  target.hidden = true;
}
function chooseCommonDomainSuggestion(domain){
  const input = el('common-domain-add');
  if (!input) return;
  input.value = domain;
  hideCommonDomainSuggestions();
  input.focus();
}
function commonCandidateKey(){
  return selectedCommonDomains().join('|');
}
function currentCandidateQueryKey(options){
  const opts = options || {};
  if (opts.view === 'domain') return `domain:${opts.domain || ''}`;
  if ((opts.view || state.candidateView) === 'common') {
    const domains = Array.isArray(opts.domains) ? opts.domains : selectedCommonDomains();
    return `common:${domains.join('|')}`;
  }
  return String(opts.view || state.candidateView || 'domain');
}
function candidateVersionKey(version){
  const value = version || {};
  return `${Number(value.size || 0)}:${Number(value.mtime_ns || 0)}`;
}
function sameCandidateVersion(left, right){
  return candidateVersionKey(left) === candidateVersionKey(right);
}
function candidateCacheValid(cached){
  if (!cached) return false;
  if (!state.candidateKnownVersion || !cached.version) return true;
  return sameCandidateVersion(cached.version, state.candidateKnownVersion);
}
function rememberCandidateVersion(version){
  if (!version) return;
  state.candidateKnownVersion = version;
  state.candidateVersion = version;
}
function invalidateCandidateCaches(){
  state.candidates = [];
  state.candidateTotal = 0;
  state.candidateOffset = 0;
  state.candidateHasMore = false;
  state.candidatesLoaded = false;
  state.candidateDomains = [];
  state.candidateDomainTotal = 0;
  state.candidateDomainStrategyTotal = 0;
  state.candidateDomainOffset = 0;
  state.candidateDomainHasMore = false;
  state.candidateDomainsLoaded = false;
  state.domainStrategies = {};
  state.commonCandidateCache = {};
  state.testedDomains = [];
  state.openCandidateDomains = {};
  state.openCommonProtocols = {};
  state.expandedStrategyLists = {};
  state.strategyEditorScrolls = {};
}
function syncCandidateVersion(version){
  if (!version) return;
  if (state.candidateKnownVersion && !sameCandidateVersion(state.candidateKnownVersion, version)) {
    invalidateCandidateCaches();
  }
  rememberCandidateVersion(version);
}
function loadCommonCandidateCache(key){
  const cached = state.commonCandidateCache[key];
  if (!candidateCacheValid(cached)) return false;
  state.candidates = cached.candidates.slice();
  state.candidateTotal = cached.total;
  state.candidateOffset = cached.offset;
  state.candidateHasMore = cached.hasMore;
  state.candidateVersion = cached.version;
  state.testedDomains = cached.testedDomains.slice();
  state.candidatesLoaded = true;
  state.candidateQueryKey = key;
  return true;
}
function storeCommonCandidateCache(key){
  if (!key) return;
  state.commonCandidateCache[key] = {
    candidates: state.candidates.slice(),
    total: state.candidateTotal,
    offset: state.candidateOffset,
    hasMore: state.candidateHasMore,
    version: state.candidateVersion,
    testedDomains: Array.isArray(state.testedDomains) ? state.testedDomains.slice() : []
  };
}
function prepareCommonCandidateState(){
  const key = `common:${commonCandidateKey()}`;
  if (state.candidateQueryKey === key) return state.candidatesLoaded;
  if (loadCommonCandidateCache(key)) return true;
  state.candidates = [];
  state.candidateTotal = 0;
  state.candidateOffset = 0;
  state.candidateHasMore = false;
  state.candidatesLoaded = false;
  state.candidateQueryKey = key;
  return false;
}
function dynamicCommonRows(rows){
  const selectedDomains = selectedCommonDomains();
  if (selectedDomains.length < 2) return [];
  return rows;
}
function renderCommonControls(){
  const controls = el('common-controls');
  if (!controls) return;
  controls.hidden = state.candidateView !== 'common';
  const domains = testedDomains();
  const datalist = el('tested-domain-options');
  if (datalist) {
    datalist.innerHTML = domains.map((domain) => `<option value="${esc(domain)}"></option>`).join('');
  }
  const raw = parseDomains(el('common-domains').value);
  const tested = new Set(domains);
  const selected = raw.filter((domain) => tested.has(domain));
  const skipped = raw.filter((domain) => !tested.has(domain));
  const parts = [`Протестировано доменов: ${domains.length}. Выбрано для пересечения: ${selected.length}.`];
  if (skipped.length) parts.push(`Будут пропущены без кандидатов: ${skipped.join(', ')}.`);
  if (selected.length < 2) parts.push('Нужно минимум два протестированных домена.');
  setText('common-domain-note', parts.join(' '));
  renderCommonDomainSuggestions();
}
function addCommonDomain(){
  const input = el('common-domain-add');
  const domain = String(input.value || '').trim();
  if (!domain) return;
  const tested = new Set(testedDomains());
  if (!tested.has(domain)) {
    showToast('По этому домену еще нет найденных стратегий', 'warn');
    return;
  }
  const current = parseDomains(el('common-domains').value);
  if (!current.includes(domain)) current.push(domain);
  el('common-domains').value = current.join('\\n');
  input.value = '';
  hideCommonDomainSuggestions();
  updateEditorLineNumbers('common-domains');
  markDomainPresetCustom('common');
  state.candidateResultRequested = false;
  prepareCommonCandidateState();
  renderCandidatesOnly();
  if (selectedCommonDomains().length >= 2) refreshCandidates(true);
}
function candidateGroups(rows){
  const domainMap = new Map();
  rows.forEach((row) => {
    const domains = candidateDomains(row);
    (domains.length ? domains : ['unknown']).forEach((domain) => {
      if (!domainMap.has(domain)) domainMap.set(domain, new Map());
      const protocol = String(row.protocol || 'unknown');
      const protocolMap = domainMap.get(domain);
      if (!protocolMap.has(protocol)) protocolMap.set(protocol, []);
      protocolMap.get(protocol).push(row);
    });
  });
  return Array.from(domainMap.entries())
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([domain, protocolMap]) => ({
      domain,
      protocols: Array.from(protocolMap.entries())
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([protocol, protocolRows]) => ({ protocol, rows: protocolRows }))
    }));
}
function protocolGroups(rows){
  const protocolMap = new Map();
  rows.forEach((row) => {
    const protocol = String(row.protocol || 'unknown');
    if (!protocolMap.has(protocol)) protocolMap.set(protocol, []);
    protocolMap.get(protocol).push(row);
  });
  return Array.from(protocolMap.entries())
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([protocol, protocolRows]) => ({ protocol, rows: protocolRows }));
}
function normalizeStrategyArg(value){
  return String(value || '').trim().replace(/\\s+/g, ' ');
}
function uniqueStrategyRows(rows){
  const seen = new Set();
  const result = [];
  rows.forEach((row) => {
    const raw = String(row.args || '').trim();
    const normalized = normalizeStrategyArg(raw);
    if (!normalized || seen.has(normalized)) return;
    seen.add(normalized);
    result.push(row);
  });
  return result;
}
function uniqueStrategyArgs(rows){
  return uniqueStrategyRows(rows).map((row) => String(row.args || '').trim());
}
function strategyComplexity(row){
  return String(row.args || '').split(/\\s+/).filter(Boolean).length;
}
function strategyDomainCoverage(row){
  return candidateAllDomains(row).length;
}
function strategyDisplayFamilyKey(row){
  const protocol = String(row.protocol || 'unknown');
  const family = String(row.family || 'other');
  return `${protocol}:${family}`;
}
function bestFamilyRow(rows){
  return rows.slice().sort((a, b) => {
    const coverage = strategyDomainCoverage(b) - strategyDomainCoverage(a);
    if (coverage) return coverage;
    const familyRank = Number(a.family_rank || 900) - Number(b.family_rank || 900);
    if (familyRank) return familyRank;
    return strategyComplexity(a) - strategyComplexity(b);
  })[0] || {};
}
function strategyFamilyGroups(rows){
  const groups = new Map();
  uniqueStrategyRows(rows).forEach((row) => {
    const key = strategyDisplayFamilyKey(row);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
  });
  return Array.from(groups.entries()).map(([key, items]) => {
    const best = bestFamilyRow(items);
    return {
      key,
      family: String(best.family || 'other'),
      familyRank: Number(best.family_rank || 900),
      familyReason: String(best.family_reason || ''),
      best,
      rows: items
    };
  }).sort((a, b) => {
    const rank = a.familyRank - b.familyRank;
    if (rank) return rank;
    return a.family.localeCompare(b.family);
  });
}
function strategyListState(key, rows){
  const groups = strategyFamilyGroups(rows);
  const all = groups.flatMap((group) => group.rows.map((row) => String(row.args || '').trim()).filter(Boolean));
  const expanded = Boolean(state.expandedStrategyLists[key]);
  let remaining = expanded ? Number.MAX_SAFE_INTEGER : STRATEGY_LIST_LIMIT;
  const visibleGroups = [];
  groups.forEach((group) => {
    if (remaining <= 0) return;
    const rowsToShow = group.rows.slice(0, remaining);
    remaining -= rowsToShow.length;
    visibleGroups.push({ ...group, rows: rowsToShow, hidden: Math.max(0, group.rows.length - rowsToShow.length) });
  });
  const visibleCount = visibleGroups.reduce((sum, group) => sum + group.rows.length, 0);
  return { all, groups, visibleGroups, visibleCount, expanded, hidden: Math.max(0, all.length - visibleCount) };
}
function lineNumbers(count){
  return Array.from({ length: count }, (_item, index) => String(index + 1)).join('\\n');
}
function updateEditorLineNumbers(id){
  const field = el(id);
  const gutter = document.querySelector(`[data-line-numbers-for="${id}"]`);
  if (!field || !gutter) return;
  const count = Math.max(1, String(field.value || '').split('\\n').length);
  gutter.textContent = lineNumbers(count);
  gutter.scrollTop = field.scrollTop;
}
function updateAllEditorLineNumbers(){
  updateEditorLineNumbers('finder-domains');
  updateEditorLineNumbers('common-domains');
}
function strategyEditorScrollKey(field){
  return field?.dataset?.strategyCodeKey || field?.closest?.('[data-strategy-list]')?.dataset?.strategyList || '';
}
function rememberStrategyEditorScrolls(){
  const field = document.activeElement && document.activeElement.matches && document.activeElement.matches('.strategy-code')
    ? document.activeElement
    : null;
  const key = strategyEditorScrollKey(field);
  if (key) state.strategyEditorScrolls[key] = field.scrollTop;
}
function restoreStrategyEditorScrolls(){
  requestAnimationFrame(() => {
    document.querySelectorAll('.strategy-code').forEach((field) => {
      const key = strategyEditorScrollKey(field);
      if (!key || state.strategyEditorScrolls[key] == null) return;
      const scrollTop = Math.min(Number(state.strategyEditorScrolls[key] || 0), Math.max(0, field.scrollHeight - field.clientHeight));
      field.scrollTop = scrollTop;
      const gutter = field.previousElementSibling;
      if (gutter) gutter.scrollTop = scrollTop;
    });
  });
}
function strategyEditor(key, rows, title, options){
  const opts = options || {};
  const list = strategyListState(key, rows);
  const remoteMore = Boolean(opts.hasRemoteMore);
  const loadedTotal = Number(opts.loadedTotal || list.all.length);
  const remoteTotal = Number(opts.remoteTotal || loadedTotal);
  const remoteText = remoteMore ? ` Загружено ${loadedTotal}${remoteTotal ? ` из ${remoteTotal}` : ''}; оставшиеся догружаются по кнопке.` : '';
  const meta = `Показано ${list.visibleCount} из ${list.all.length} уникальных стратегий в ${list.groups.length} семействах. Дубликаты строк скрыты.${list.hidden ? ` Скрыто до раскрытия: ${list.hidden}.` : ''}${remoteText}`;
  const toggle = list.all.length > STRATEGY_LIST_LIMIT || remoteMore
    ? `<button class="secondary" data-strategy-list-toggle="${esc(key)}" type="button"${opts.loading ? ' disabled' : ''}>${strategyToggleLabel(list, opts)}</button>`
    : '';
  return `<div class="strategy-editor" data-strategy-list="${esc(key)}">
    <div class="strategy-editor-head">
      <div class="strategy-editor-title">
        <label>${esc(title)}</label>
        <div class="strategy-editor-meta">${esc(meta)}</div>
      </div>
      ${toggle}
    </div>
    <div class="strategy-family-list">${list.visibleGroups.map((group, index) => strategyFamilyGroup(key, group, index)).join('')}</div>
  </div>`;
}
function strategyFamilyGroup(parentKey, group, index){
  const lines = group.rows.map((row) => String(row.args || '').trim()).filter(Boolean);
  const lineCount = Math.max(lines.length, 1);
  const rowsAttr = Math.min(Math.max(lineCount, 4), 14);
  const best = group.best || {};
  const hidden = Number(group.hidden || 0);
  const reason = [
    group.familyReason ? `семейство: ${group.familyReason}` : '',
    hidden ? `скрыто вариантов: ${hidden}` : ''
  ].filter(Boolean).join(' · ');
  const key = `${parentKey}:family:${index}:${group.key}`;
  return `<details class="strategy-family" open>
    <summary class="strategy-family-summary">
      <div class="strategy-family-head">
        ${badge(group.family || 'other', '')}
        ${badge(`${group.rows.length + hidden} вариантов`, group.rows.length + hidden > 1 ? 'warn' : '')}
      </div>
      <div class="strategy-family-reason">${esc(reason || 'семейство определено по аргументам стратегии')}</div>
    </summary>
    <div class="code-editor">
      <pre class="line-numbers" aria-hidden="true">${esc(lineNumbers(lineCount))}</pre>
      <textarea class="strategy-code" data-strategy-code-key="${esc(key)}" readonly spellcheck="false" rows="${rowsAttr}">${esc(lines.join('\\n'))}</textarea>
    </div>
  </details>`;
}
function strategyToggleLabel(list, options){
  const opts = options || {};
  if (opts.loading) return 'Загружается...';
  if (list.expanded) return `Свернуть до ${STRATEGY_LIST_LIMIT}`;
  if (opts.hasRemoteMore) return opts.remoteLabel || 'Показать все стратегии домена';
  return `Показать все ${list.all.length}`;
}
function domainFromStrategyListKey(key){
  const text = String(key || '');
  if (!text.startsWith('domain:')) return '';
  const rest = text.slice('domain:'.length);
  const protocolSeparator = rest.lastIndexOf(':');
  return protocolSeparator >= 0 ? rest.slice(0, protocolSeparator) : rest;
}
function isCommonStrategyListKey(key){
  return String(key || '').startsWith('common:');
}
function renderRuns(){
  const rows = state.finderRuns.filter((row) => isDiscoveryRun(row));
  setText('finder-runs-count', String(state.finderRunTotal || rows.length));
  const visible = rows.slice().reverse();
  if (!visible.length) {
    el('finder-runs-table').innerHTML = '<div class="empty">Запусков поиска пока не было</div>';
    return;
  }
  el('finder-runs-table').innerHTML = `<div class="run-history">${visible.map(renderRunCard).join('')}</div>${runPager()}`;
}
function runPager(){
  return listLoadMore('load-more-runs', state.finderRunHasMore, state.finderRunsLoading);
}
function renderRunCard(row){
  const count = runCandidateCount(row);
  const status = row.status || '-';
  const domainKey = runDomainKey(row);
  return `<article class="run-card ${esc(runCardClass(row))}">
    <div class="run-card-main">
      ${runField('Время', friendlyDate(row.timestamp))}
      ${runField('Режим', runMode(row))}
      <div class="run-field">
        <div class="run-field-label">Статус</div>
        <div class="run-field-value run-status">${badge(runStatusLabel(status), statusTone[status] || '')}</div>
      </div>
      ${runField('Этап', runPhaseText(row))}
      <div class="run-field">
        <div class="run-field-label">Стратегии</div>
        <div class="run-field-value">${badge(String(count), count > 0 ? 'good' : '')}</div>
      </div>
      ${runField('Попытки', runProgressText(row))}
      ${runField('Настройки', runSettingsText(row))}
      ${runField('Диагностика', runDiagnosticsSummary(row))}
      ${runField('Итог', runSummary(row))}
    </div>
    ${runDomains(row, domainKey)}
    ${runDiagnostics(row)}
    <div class="run-card-actions">
      <button class="secondary" data-run-repeat="${esc(domainKey)}" type="button">Повторить с этими настройками</button>
    </div>
  </article>`;
}
function runDomainKey(row){
  return String(row.id || `${row.timestamp || ''}:${(row.domains || []).join('|')}`);
}
function runCardClass(row){
  const status = String(row.status || 'unknown').toLowerCase().replace(/[^a-z0-9_-]/g, '') || 'unknown';
  const kind = row.kind === 'multi-domain-discovery' ? 'multi' : 'standard';
  return `run-card-status-${status} run-card-kind-${kind}`;
}
function runField(label, value){
  return `<div class="run-field">
    <div class="run-field-label">${esc(label)}</div>
    <div class="run-field-value">${esc(value || '-')}</div>
  </div>`;
}
function runStatusLabel(status){
  const labels = {
    success: 'Завершено',
    failed: 'Ошибка',
    error: 'Ошибка',
    running: 'Идет подбор',
    queued: 'Запускается',
    stopping: 'Останавливается',
    stopped: 'Остановлено',
    timeout: 'Таймаут',
    idle: 'Свободно'
  };
  return labels[status] || status || '-';
}
function runPhaseText(row){
  const progress = row.progress || {};
  return progress.phase_label || phaseLabel(row.phase || progress.phase || '');
}
function phaseLabel(phase){
  const labels = {
    checking_vpn: 'проверка VPN',
    checking_zapret: 'проверка zapret',
    checking_domain: 'проверка доступности домена',
    strategy_discovery: 'подбор стратегий',
    strategy_summary: 'суммаризация стратегий',
    saving_results: 'сохранение результатов',
    complete: 'завершено'
  };
  return labels[phase] || phase || '-';
}
function runDomains(row, domainKey){
  const domains = Array.isArray(row.domains) ? row.domains.map((domain) => String(domain || '').trim()).filter(Boolean) : [];
  const preview = domains.length ? domains.join(', ') : '-';
  const count = domains.length ? `${domains.length} доменов` : 'нет доменов';
  const expandable = domains.length > 1;
  const open = expandable && Boolean(state.openRunDomains[domainKey]);
  return `<details class="run-domains ${expandable ? 'run-domains-expandable' : ''}" data-run-domains="${esc(domainKey)}"${open ? ' open' : ''}>
    <summary>
      <span class="run-field-label">Домены</span>
      <span class="run-domains-preview" title="${esc(preview)}">${esc(preview)}</span>
      <span class="run-domains-count">${esc(count)}</span>
      <span class="run-domains-arrow" aria-hidden="true"></span>
    </summary>
    <div class="run-domain-list">${runDomainChips(domains)}</div>
  </details>`;
}
function runDomainChips(domains){
  if (!domains.length) return '<span class="run-domain-chip">-</span>';
  return domains.map((domain) => `<span class="run-domain-chip">${esc(domain)}</span>`).join('');
}
function diagnosticShortLabel(status, fallback){
  const labels = {
    invalid_domain: 'некорректная строка',
    dns_error: 'DNS не дал адрес',
    tls_sni_problem: 'TLS/SNI не совпал',
    ssl_connect_error: 'TLS-соединение сорвалось',
    quic_connect_error: 'QUIC/connect не установился',
    timeout: 'проверка не дождалась ответа',
    needs_discovery: 'нужен подбор стратегии',
    curl_error: 'проверочный запрос вернул ошибку',
    direct_available: 'прямой доступ есть'
  };
  return labels[status] || fallback || status || '-';
}
function diagnosticExplanation(item, row){
  const status = item.status || '';
  const found = runCandidateCount(row) > 0;
  const explanations = {
    invalid_domain: 'Строка не похожа на домен, поэтому проверка стратегий не может проверить ее как сайт.',
    dns_error: 'DNS не вернул адрес. Это проблема разрешения имени до проверки стратегии.',
    tls_sni_problem: 'Проверочный запрос получил сертификат не для этого домена. Такое бывает при SNI/TLS-проверках, DPI или особенностях service-доменов.',
    ssl_connect_error: 'TLS-соединение оборвалось до нормального ответа сервера.',
    quic_connect_error: 'QUIC или connect-проверка не смогла установить соединение.',
    timeout: found
      ? 'Часть проверок не успела ответить за таймаут. Это не отменяет найденные стратегии: успешные проверки уже сохранены отдельно.'
      : 'Домен не ответил за заданный таймаут. Увеличьте таймаут или проверьте доступность домена отдельно.',
    needs_discovery: 'Для домена не найден прямой рабочий вариант, нужен подбор стратегии.',
    curl_error: 'Проверочный запрос вернул ошибку, которую нужно смотреть в технических деталях.',
    direct_available: 'Домен открывался напрямую, стратегия для него может быть не нужна.'
  };
  return explanations[status] || item.message || 'Подробности доступны в технических деталях.';
}
function curlCodeLabel(code){
  const labels = {
    '3': 'некорректная строка',
    '6': 'DNS не дал адрес',
    '7': 'соединение не установилось',
    '28': 'таймаут',
    '35': 'TLS/SSL сбой',
    '60': 'TLS/SNI не совпал'
  };
  return labels[String(code)] || 'проверочный запрос вернул ошибку';
}
function curlCodeDetails(codes){
  if (!codes || !Object.keys(codes).length) return '';
  return Object.entries(codes)
    .map(([code, count]) => `curl ${code}: ${curlCodeLabel(code)}, ${count} раз`)
    .join('; ');
}
function runDiagnosticsSummary(row){
  const skipped = Number(row.domain_skipped_count || 0);
  const dominant = row.dominant_failure || {};
  if (dominant.status || dominant.label) return `${diagnosticShortLabel(dominant.status, dominant.label)}: ${dominant.count || 0}`;
  if (skipped) return `пропущено строк: ${skipped}`;
  const diagnostics = Array.isArray(row.domain_diagnostics) ? row.domain_diagnostics : [];
  if (diagnostics.length) return diagnostics.map((item) => diagnosticShortLabel(item.status, item.label)).filter(Boolean).slice(0, 2).join(', ');
  return '-';
}
function runDiagnostics(row){
  const skipped = Array.isArray(row.domain_skipped) ? row.domain_skipped : [];
  const diagnostics = Array.isArray(row.domain_diagnostics) ? row.domain_diagnostics : [];
  const curlSummary = row.curl_diagnostics_summary || {};
  if (!skipped.length && !diagnostics.length && !Object.keys(curlSummary).length) return '';
  const skippedItems = skipped.slice(0, 20).map((item) => diagnosticTableRow({
    type: 'строка',
    target: item.raw || '-',
    details: item.message || 'Строка пропущена до запуска проверки.',
    tech: item.status || '-',
    tone: 'bad'
  })).join('');
  const domainItems = diagnostics.map((item) => {
    const tone = ['dns_error', 'invalid_domain', 'tls_sni_problem'].includes(item.status) ? 'bad' : 'warn';
    return diagnosticTableRow({
      type: 'домен',
      target: item.domain || '-',
      details: diagnosticExplanation(item, row),
      tech: [diagnosticShortLabel(item.status, item.label), curlCodeDetails(item.codes)].filter(Boolean).join('; '),
      tone
    });
  }).join('');
  const codeItems = Object.entries(curlSummary).map(([code, count]) => diagnosticTableRow({
    type: 'сводка',
    target: 'все проверки',
    details: `Всего таких ошибок в запуске: ${count}.`,
    tech: `curl ${code}: ${count} раз`,
    tone: 'warn'
  })).join('');
  return `<details class="run-diagnostics">
    <summary>Диагностика доменов</summary>
    <div class="run-diagnostic-table-wrap">
      <table class="run-diagnostic-table">
        <thead>
          <tr>
            <th>Тип</th>
            <th>Домен / строка</th>
            <th>Пояснение</th>
          </tr>
        </thead>
        <tbody>${skippedItems}${domainItems}${codeItems}</tbody>
      </table>
    </div>
    <div class="run-diagnostic-note">Если стратегия найдена, отдельные ошибки в диагностике означают провал части проверок, а не отмену сохраненных успешных стратегий.</div>
  </details>`;
}
function diagnosticTableRow(item){
  const tech = item.tech
    ? `<details class="run-diagnostic-tech"><summary>технически</summary><div>${esc(item.tech)}</div></details>`
    : '';
  return `<tr>
    <td>${esc(item.type || '-')}</td>
    <td class="run-diagnostic-target">${esc(item.target || '-')}</td>
    <td><div class="run-diagnostic-details">${esc(item.details || '-')}</div>${tech}</td>
  </tr>`;
}
function isDiscoveryRun(row){
  return row.kind === 'standard-discovery' || row.kind === 'multi-domain-discovery';
}
function runMode(row){
  return row.kind === 'multi-domain-discovery' ? 'все домены на одной стратегии' : 'обычный';
}
function runSummary(row){
  const count = runCandidateCount(row);
  const phase = row.phase || (row.progress || {}).phase || '';
  if (row.status === 'stopping') return 'останавливается';
  if (phase === 'saving_results' && row.status === 'failed') return `ошибка сохранения, код: ${row.returncode ?? '-'}`;
  if (phase === 'saving_results') return 'сохраняются результаты';
  if (row.status === 'running') return 'идет поиск';
  if (row.status === 'timeout') return `остановлено по лимиту, найдено: ${count}`;
  if (row.status === 'stopped') return count > 0 ? `остановлено, сохранено: ${count}` : 'остановлено, кандидатов нет';
  if (row.status === 'success') return count > 0 ? `найдено: ${count}` : 'завершено, кандидатов нет';
  if (row.status === 'failed') return `ошибка, код: ${row.returncode ?? '-'}`;
  return count > 0 ? `найдено: ${count}` : '-';
}
function runCandidateCount(row){
  return Number(row.candidate_count || 0) + Number(row.common_candidate_count || 0);
}
function runSettingsText(row){
  const options = row.discovery_options || {};
  const protocols = [];
  if (truthyOption(options.enable_http, row.enable_http)) protocols.push('HTTP');
  if (truthyOption(options.enable_tls12, row.enable_tls12 ?? row.enable_tls)) protocols.push('TLS 1.2');
  if (truthyOption(options.enable_tls13, row.enable_tls13)) protocols.push('TLS 1.3');
  if (truthyOption(options.enable_quic, row.include_quic ?? row.enable_quic)) protocols.push('QUIC');
  const scan = options.scan_level || row.scan_level || 'standard';
  const repeats = Number(options.repeats || row.repeats || 1);
  const repeatParallel = truthyOption(options.repeat_parallel, row.repeat_parallel) ? ', параллельные повторы' : '';
  const skip = [
    truthyOption(options.skip_dnscheck, row.skip_dnscheck) ? 'без DNS' : 'с DNS',
    truthyOption(options.skip_ipblock, row.skip_ipblock) ? 'без IP-проверки' : 'с IP-проверкой',
  ].join(', ');
  const ipv6 = truthyOption(options.enable_ipv6, row.enable_ipv6) ? ', IPv6' : '';
  const debugLog = truthyOption(row.debug_stdout, false) ? ', debug-log' : '';
  const curl = row.kind === 'multi-domain-discovery' ? `, проверочных запросов ${row.curl_parallelism || 4}` : '';
  const limit = row.timeout_seconds ? `, лимит ${formatDuration(Number(row.timeout_seconds || 0))}` : ', без лимита';
  return `${protocols.join('+') || '-'} · ${scan} · повт. ${repeats}${repeatParallel} · ${skip}${ipv6}${debugLog}${curl}${limit}`;
}
function truthyOption(primary, fallback){
  const value = primary === undefined || primary === null ? fallback : primary;
  return Boolean(value);
}
function runPayload(row){
  const options = row.discovery_options || {};
  const payload = {
    domains: uniqueDomains(row.domains || []),
    enable_http: truthyOption(options.enable_http, row.enable_http),
    enable_tls12: truthyOption(options.enable_tls12, row.enable_tls12 ?? row.enable_tls),
    enable_tls13: truthyOption(options.enable_tls13, row.enable_tls13),
    include_quic: truthyOption(options.enable_quic, row.include_quic ?? row.enable_quic),
    enable_ipv6: truthyOption(options.enable_ipv6, row.enable_ipv6),
    scan_level: options.scan_level || row.scan_level || 'standard',
    repeats: Number(options.repeats || row.repeats || 1),
    repeat_parallel: truthyOption(options.repeat_parallel, row.repeat_parallel),
    skip_dnscheck: truthyOption(options.skip_dnscheck, row.skip_dnscheck),
    skip_ipblock: truthyOption(options.skip_ipblock, row.skip_ipblock),
    debug_stdout: truthyOption(row.debug_stdout, false),
    curl_max_time: Number(options.curl_max_time || row.curl_max_time || (state.settings || {}).curl_max_time || 2),
    curl_max_time_quic: Number(options.curl_max_time_quic || row.curl_max_time_quic || (state.settings || {}).curl_max_time_quic || 2),
    curl_max_time_doh: Number(options.curl_max_time_doh || row.curl_max_time_doh || (state.settings || {}).curl_max_time_doh || 2),
  };
  if (row.timeout_seconds) payload.timeout_seconds = Number(row.timeout_seconds);
  if (row.kind === 'multi-domain-discovery') payload.curl_parallelism = Number(row.curl_parallelism || 4);
  return payload;
}
function fillRunFormFromPayload(row, payload){
  const data = payload || runPayload(row);
  const domains = uniqueDomains(data.domains || []);
  el('finder-domains').value = domains.join('\\n');
  state.domainsTouched = true;
  markDomainPresetCustom('finder');
  updateEditorLineNumbers('finder-domains');
  const multi = row && row.kind === 'multi-domain-discovery';
  const modeInput = document.querySelector(`input[name="run-mode"][value="${multi ? 'multi' : 'standard'}"]`);
  if (modeInput) modeInput.checked = true;
  el('curl-parallelism').value = String(data.curl_parallelism || curlParallelism());
  el('enable-http').checked = Boolean(data.enable_http);
  el('enable-tls12').checked = Boolean(data.enable_tls12);
  el('enable-tls13').checked = Boolean(data.enable_tls13);
  el('include-quic').checked = Boolean(data.include_quic);
  el('enable-ipv6').checked = Boolean(data.enable_ipv6);
  el('scan-level').value = data.scan_level || 'standard';
  const profileSelect = el('discovery-profile-select');
  if (profileSelect && [...profileSelect.options].some((option) => option.value === (data.scan_level || 'standard'))) {
    profileSelect.value = data.scan_level || 'standard';
  }
  el('repeats').value = String(data.repeats || 1);
  el('repeat-parallel').checked = Boolean(data.repeat_parallel);
  el('skip-dnscheck').checked = Boolean(data.skip_dnscheck);
  el('skip-ipblock').checked = Boolean(data.skip_ipblock);
  el('run-curl-max-time').value = String(data.curl_max_time || 2);
  el('run-curl-max-time-quic').value = String(data.curl_max_time_quic || 2);
  el('run-curl-max-time-doh').value = String(data.curl_max_time_doh || 2);
  const timeout = Number(data.timeout_seconds || 0);
  el('limit-time-enabled').checked = timeout > 0;
  syncTimeLimitUi();
  if (timeout > 0) el('finder-timeout-hours').value = String(Math.max(0.1, Math.round((timeout / 3600) * 10) / 10));
  renderDiscoveryProfileNote();
  renderRunModeNote();
  renderRunLaunchSummary();
  setActiveTab('finder');
  setMessage('Параметры прошлого подбора перенесены в форму запуска. Проверьте сводку и запустите вручную.', 'good');
}
function repeatRun(runKey){
  const row = state.finderRuns.find((item) => runDomainKey(item) === runKey || String(item.id || '') === runKey);
  if (!row) {
    setMessage('Запуск не найден в истории', 'bad');
    return;
  }
  const payload = runPayload(row);
  fillRunFormFromPayload(row, payload);
}
function runProgressText(row){
  const progress = row.progress || {};
  const attempted = Number(progress.attempted || 0);
  const total = Number(progress.effective_attempt_total || progress.attempt_total || 0);
  if (total) return `${attempted} из ${total}`;
  if (attempted) return String(attempted);
  return '-';
}
function renderLog(){
  const log = state.finderLog || {};
  const status = log.status || '-';
  const badgeNode = el('finder-log-status');
  badgeNode.textContent = status;
  badgeNode.className = 'badge ' + (statusTone[status] || '');
  const parts = [];
  if (log.stdout_tail) parts.push(log.stdout_tail);
  if (log.stderr_tail) parts.push('--- stderr ---\\n' + log.stderr_tail);
  const logNode = el('finder-log');
  logNode.textContent = parts.join('\\n\\n') || 'Лога пока нет';
  renderStderrDiagnostics(log.stderr_diagnostics || []);
  renderProgress(log.progress || {});
  renderRunSettingsSummary(log.run_settings || {});
  renderLiveRun();
  renderEvents();
  if (state.activeTab === 'terminal') scrollLogToBottom();
}
function renderStderrDiagnostics(items){
  const target = el('stderr-diagnostics');
  if (!target) return;
  const rows = Array.isArray(items) ? items : [];
  if (!rows.length) {
    target.hidden = true;
    target.innerHTML = '';
    return;
  }
  target.hidden = false;
  target.innerHTML = rows.map((item) => {
    const severity = item.severity === 'warning' ? 'warn' : '';
    return `<div class="stderr-diagnostic ${severity}">
      <div class="stderr-diagnostic-title">${esc(item.label || item.status || 'Диагностика stderr')}</div>
      <div>${esc(item.message || '')}</div>
    </div>`;
  }).join('');
}
function renderBackups(){
  const rows = state.backups || [];
  const countNode = el('backups-count');
  if (countNode) countNode.textContent = String(rows.length);
  const updatedNode = el('backups-updated-at');
  if (updatedNode) {
    const updated = friendlyTime(state.backupsUpdatedAt);
    updatedNode.textContent = updated ? `Список обновлен ${updated}` : '';
  }
  const target = el('backups-table');
  if (!target) return;
  if (state.backupsLoading && !state.backupsLoaded) {
    target.innerHTML = '<div class="loading-skeleton" aria-label="Загрузка бекапов"></div>';
    return;
  }
  if (!rows.length) {
    target.innerHTML = `<div class="empty">${state.backupsLoaded ? 'Бекапов пока нет' : 'Откройте вкладку, чтобы загрузить бекапы'}</div>`;
    return;
  }
  target.innerHTML = rows.map((item) => backupCard(item)).join('');
  renderMetrics();
}
function backupCard(item){
  const id = String(item.id || '');
  const files = Array.isArray(item.files) ? item.files : [];
  const visibleFiles = files.filter((file) => !String(file.path || '').endsWith('checksums.sha256') && String(file.path || '') !== 'manifest.json');
  return `<article class="backup-card">
    <div class="domain-header">
      <div>
        <h3>${esc(id)}</h3>
        <div class="helper-text">${esc(item.created_at || '-')}</div>
      </div>
      ${badge(item.checksum_ok ? 'checksum ok' : 'checksum fail', item.checksum_ok ? 'good' : 'bad')}
    </div>
    <div class="backup-meta">
      <div>Размер: ${esc(formatBytes(item.size_bytes || 0))}</div>
      <div>Стратегий: ${esc(item.strategy_count || 0)}</div>
    </div>
    <div class="backup-card-actions">
      <a class="backup-archive-link" href="${backupDownloadUrl(id, 'archive')}">Скачать архив</a>
      <button class="secondary danger" data-backup-restore="${esc(id)}" type="button">Восстановить из бекапа</button>
      <button class="secondary danger" data-backup-delete="${esc(id)}" type="button">Удалить бекап</button>
    </div>
    <div class="backup-downloads">
      <div class="backup-download-block">
        <div class="backup-section-title">Файлы бекапа</div>
        <div class="backup-file-links">
          ${visibleFiles.map((file) => `<a href="${backupDownloadUrl(id, file.path)}">${esc(file.path)}</a>`).join('')}
        </div>
      </div>
    </div>
  </article>`;
}
function backupDownloadUrl(snapshot, file){
  return authUrl(`/api/backups/download?snapshot=${encodeURIComponent(snapshot)}&file=${encodeURIComponent(file)}`);
}
function formatBytes(value){
  const bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 Б';
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} КБ`;
  return `${(bytes / 1024 / 1024).toFixed(1)} МБ`;
}
function renderProgress(progress){
  const percent = Number(progress.percent || 0);
  const safePercent = Math.max(0, Math.min(100, Number.isFinite(percent) ? percent : 0));
  el('progress-fill').style.width = `${safePercent}%`;
  const bar = el('progress-bar');
  if (bar) {
    bar.setAttribute('aria-valuenow', String(Math.round(safePercent)));
    bar.setAttribute('aria-valuetext', `${Math.round(safePercent)}%`);
  }
  const attempted = Number(progress.attempted ?? 0);
  const attemptTotal = Number(progress.attempt_total ?? 0);
  const effectiveTotal = Number(progress.effective_attempt_total || attemptTotal || 0);
  setText('progress-attempted', effectiveTotal ? `${attempted} / ${effectiveTotal}` : String(progress.attempted ?? 0));
  const strategyChecked = Number(progress.strategy_checked ?? 0);
  const strategyTotal = Number(progress.strategy_total ?? 0);
  setText('progress-strategies', strategyTotal ? `${strategyChecked} / ${strategyTotal}` : '-');
  setText('progress-successful', String(progress.successful ?? 0));
  setText('progress-phase', progress.phase_label || phaseLabel(progress.phase || ''));
  setText('progress-scripts', progress.current_script || '-');
  const elapsed = progressLiveElapsedSeconds(progress);
  setText('progress-elapsed', elapsed == null ? '-' : formatDuration(elapsed));
  const eta = progressLiveEtaSeconds(progress);
  setText('progress-eta', eta == null ? etaStatusText(progress.eta_status) : formatDuration(eta));
  const etaMs = progress.eta_ms_per_attempt || progress.eta_estimate_ms_per_attempt;
  setText('progress-note', `расчитанное среднее время попытки: ${etaMs ? `${etaMs} мс` : '-'}`);
}
function progressAttemptText(progress){
  const attempted = Number(progress.attempted ?? 0);
  const total = Number(progress.effective_attempt_total || progress.attempt_total || 0);
  return total ? `${attempted} / ${total}` : String(progress.attempted ?? 0);
}
function progressStrategyText(progress){
  const checked = Number(progress.strategy_checked ?? 0);
  const total = Number(progress.strategy_total ?? 0);
  return total ? `${checked} / ${total}` : '-';
}
function interruptedRunWarning(){
  if (isBusy()) return '';
  const row = latestRun();
  if (!row) return '';
  const status = String(row.status || '').toLowerCase();
  if (!['running', 'queued', 'stopping'].includes(status)) return '';
  return 'Предыдущий подбор был прерван перезагрузкой';
}
function liveRunStatusText(){
  const board = (state.status || {}).state || {};
  if (isBusy()) return runStatusLabel(board.current_job_status || 'running');
  const interrupted = interruptedRunWarning();
  if (interrupted) return 'Остановлено';
  const row = latestRun();
  return row ? runStatusLabel(row.status || 'idle') : 'Свободно';
}
function liveRunCells(progress){
  const elapsed = progressLiveElapsedSeconds(progress);
  const eta = progressLiveEtaSeconds(progress);
  return [
    ['Статус', liveRunStatusText()],
    ['Этап', progress.phase_label || phaseLabel(progress.phase || '')],
    ['Попытки', progressAttemptText(progress)],
    ['Стратегии', progressStrategyText(progress)],
    ['Найдено', String(progress.successful ?? 0)],
    ['Текущий файл', progress.current_script || '-'],
    ['Прошло', elapsed == null ? '-' : formatDuration(elapsed)],
    ['Осталось', eta == null ? etaStatusText(progress.eta_status) : formatDuration(eta)]
  ];
}
function latestImportantLogMessage(){
  const log = state.finderLog || {};
  const diagnostics = Array.isArray(log.stderr_diagnostics) ? log.stderr_diagnostics : [];
  if (diagnostics.length) return diagnostics[0].message || diagnostics[0].label || diagnostics[0].status || '';
  const stderr = String(log.stderr_tail || '').trim().split('\\n').filter(Boolean);
  return stderr.length ? stderr[stderr.length - 1] : '';
}
function renderLiveRun(){
  const target = el('live-run-panel');
  if (!target) return;
  const log = state.finderLog || {};
  const progress = log.progress || {};
  const interrupted = interruptedRunWarning();
  const important = interrupted || latestImportantLogMessage();
  const tone = isBusy() ? 'warn' : (interrupted ? 'warn' : '');
  target.innerHTML = `<article class="live-run-card">
    <div class="live-run-header">
      <div class="live-run-title">Текущий подбор</div>
      ${badge(liveRunStatusText(), tone)}
    </div>
    <div class="live-run-grid">
      ${liveRunCells(progress).map(([label, value]) => `<div class="live-run-cell">
        <div class="live-run-label">${esc(label)}</div>
        <div class="live-run-value">${esc(value || '-')}</div>
      </div>`).join('')}
    </div>
    <div class="helper-text">${important ? esc(important) : 'Ошибок и предупреждений в текущем срезе нет.'}</div>
    <div class="live-run-actions">
      <button class="secondary danger" data-action="stop-current" type="button"${isBusy() ? '' : ' disabled'}>Остановить</button>
      <button class="secondary" data-action="open-log" type="button">Открыть лог</button>
      <button class="secondary" data-action="open-candidates" type="button">Открыть результаты</button>
    </div>
  </article>`;
}
function eventRows(){
  const rows = [];
  const now = new Date().toISOString();
  const stateBoard = (state.status || {}).state || {};
  const interrupted = interruptedRunWarning();
  if (interrupted) {
    rows.push({
      severity: 'warning',
      time: now,
      title: interrupted,
      source: 'История запуска',
      message: 'Активный подбор не восстанавливается после перезагрузки. Откройте последний лог или повторите запуск вручную.'
    });
  }
  if (stateBoard.last_error) {
    rows.push({
      severity: 'error',
      time: now,
      title: 'Ошибка сервиса',
      source: 'status',
      message: String(stateBoard.last_error)
    });
  }
  const release = state.releaseUpdate || {};
  const releaseStatus = String(release.status || '').toLowerCase();
  if (releaseStatus === 'failed') {
    rows.push({
      severity: 'error',
      time: now,
      title: 'Ошибка обновления',
      source: 'release update',
      message: release.error || release.log_tail || 'Обновление завершилось ошибкой.'
    });
  }
  const log = state.finderLog || {};
  const diagnostics = Array.isArray(log.stderr_diagnostics) ? log.stderr_diagnostics : [];
  diagnostics.slice(0, 3).forEach((item) => {
    rows.push({
      severity: item.severity === 'warning' ? 'warning' : 'error',
      time: now,
      title: item.label || item.status || 'Диагностика подбора',
      source: 'latest-log',
      message: item.message || ''
    });
  });
  if (!rows.length && String(log.stderr_tail || '').trim()) {
    rows.push({
      severity: 'warning',
      time: now,
      title: 'Последний stderr',
      source: 'latest-log',
      message: String(log.stderr_tail || '').trim().split('\\n').slice(-1)[0]
    });
  }
  return rows;
}
function diagnosticsText(){
  const rows = eventRows();
  const log = state.finderLog || {};
  const parts = rows.map((row) => `[${row.severity}] ${row.title}: ${row.message}`);
  if (log.stderr_tail) parts.push(`stderr:\n${log.stderr_tail}`);
  if ((state.status || {}).state?.last_error) parts.push(`last_error: ${(state.status || {}).state.last_error}`);
  return parts.join('\\n\\n') || 'Ошибок и предупреждений нет.';
}
async function copyDiagnostics(){
  const text = diagnosticsText();
  try {
    await navigator.clipboard.writeText(text);
    setMessage('Диагностика скопирована', 'good');
  } catch (error) {
    setMessage(`Не удалось скопировать диагностику: ${error.message}`, 'bad');
  }
}
function renderEvents(){
  const target = el('events-panel');
  if (!target) return;
  const rows = eventRows();
  if (!rows.length) {
    target.innerHTML = `<article class="event-card">
      <div class="event-header">
        <div class="event-title">Ошибки и предупреждения</div>
        ${badge('нет активных событий', 'good')}
      </div>
      <div class="event-meta">Текущий срез не содержит значимых ошибок.</div>
    </article>`;
    return;
  }
  target.innerHTML = rows.map((row) => {
    const tone = row.severity === 'error' ? 'bad' : 'warn';
    return `<article class="event-card ${tone}">
      <div class="event-header">
        <div class="event-title">${esc(row.title)}</div>
        ${badge(row.severity === 'error' ? 'Ошибка' : 'Предупреждение', tone)}
      </div>
      <div>${esc(row.message || '-')}</div>
      <div class="event-meta">${esc(friendlyTime(row.time) || '-')} · ${esc(row.source || '-')}</div>
      <div class="event-actions">
        <button class="secondary" data-action="repeat-last-run" type="button">Повторить</button>
        <button class="secondary" data-action="open-log" type="button">Открыть лог</button>
        <button class="secondary" data-action="copy-diagnostics" type="button">Скопировать диагностику</button>
      </div>
    </article>`;
  }).join('');
}
function progressLiveElapsedSeconds(progress){
  if (progress.elapsed_seconds == null) return null;
  const base = Math.max(0, Number(progress.elapsed_seconds || 0));
  const receivedAt = Number(progress.received_at_ms || 0);
  if (!isBusy() || !receivedAt) return base;
  return base + Math.max(0, Math.floor((Date.now() - receivedAt) / 1000));
}
function progressLiveEtaSeconds(progress){
  if (progress.eta_seconds == null) return null;
  const base = Math.max(0, Number(progress.eta_seconds || 0));
  const baseElapsed = Math.max(0, Number(progress.elapsed_seconds || 0));
  const liveElapsed = progressLiveElapsedSeconds(progress);
  if (liveElapsed == null) return base;
  return Math.max(0, base - Math.max(0, liveElapsed - baseElapsed));
}
function etaModeLabel(progress){
  const status = String(progress.eta_status || '');
  const progressStatus = String(progress.progress_status || '');
  if (status === 'sample') return 'по live-скорости';
  if (status === 'calculating') return 'сбор выборки';
  if (status === 'elapsed_average') return 'по среднему времени попытки';
  if (status === 'underestimated' || progressStatus === 'underestimated') return 'уточняется';
  if (status === 'complete') return 'завершено';
  if (status === 'estimated') return 'по таймауту';
  return status || '-';
}
function etaStatusText(status){
  if (status === 'calculating') return 'рассчитывается';
  if (status === 'underestimated') return 'уточняется';
  return '-';
}
function renderRunSettingsSummary(settings){
  const target = el('progress-metrics');
  if (!target) return;
  if (!settings || !Object.keys(settings).length) {
    target.textContent = 'Настройки запуска появятся после старта подбора.';
    return;
  }
  const protocols = [];
  if (settings.enable_http) protocols.push('HTTP');
  if (settings.enable_tls12) protocols.push('TLS 1.2');
  if (settings.enable_tls13) protocols.push('TLS 1.3');
  if (settings.enable_quic) protocols.push('QUIC');
  const domainCount = Number(settings.domain_count || 0);
  const mode = settings.kind === 'multi-domain-discovery' ? 'все домены на одной стратегии' : 'обычный';
  const ipMode = settings.enable_ipv6 ? 'IPv4+IPv6' : 'IPv4';
  const scan = scanLevelLabel(settings.scan_level || 'standard');
  const repeats = Number(settings.repeats || 1);
  const repeatMode = settings.repeat_parallel ? 'повторы параллельно' : 'повторы последовательно';
  const curl = settings.curl_parallelism ? `проверочных запросов: ${settings.curl_parallelism}` : '';
  const limit = Number(settings.timeout_seconds || 0) > 0 ? `лимит: ${formatDuration(Number(settings.timeout_seconds || 0))}` : 'без лимита';
  const checks = [
    settings.skip_dnscheck ? 'без DNS-проверки' : 'с DNS-проверкой',
    settings.skip_ipblock ? 'без IP-проверки' : 'с IP-проверкой',
  ].join(', ');
  const timeouts = `таймауты HTTP/TLS ${settings.curl_max_time || 2}с, QUIC ${settings.curl_max_time_quic || 2}с, DoH ${settings.curl_max_time_doh || 2}с`;
  target.textContent = [
    `доменов: ${domainCount}`,
    `режим: ${mode}`,
    `протоколы: ${protocols.join('+') || '-'}`,
    ipMode,
    `глубина: ${scan}`,
    `повторы: ${repeats}`,
    repeatMode,
    curl,
    checks,
    limit,
    timeouts,
  ].filter(Boolean).join(' · ');
}
function scanLevelLabel(value){
  const profile = DISCOVERY_PROFILES[String(value || 'standard')];
  return profile ? profile.title : String(value || '-');
}
function renderSettings(){
  const settings = state.settings || {};
  const ipv6 = el('settings-enable-ipv6');
  const debugStdout = el('settings-debug-stdout');
  const curlMax = el('settings-curl-max');
  const runCurlMaxTime = el('run-curl-max-time');
  const runCurlMaxTimeQuic = el('run-curl-max-time-quic');
  const runCurlMaxTimeDoh = el('run-curl-max-time-doh');
  const channel = el('settings-update-channel');
  if (ipv6) ipv6.checked = Boolean(settings.enable_ipv6);
  if (debugStdout) debugStdout.checked = Boolean(settings.debug_stdout);
  if (curlMax) curlMax.value = String(settings.curl_parallelism_max || 10);
  if (channel) channel.value = settings.update_channel || 'stable';
  renderReleaseInfo();
  if (!state.settingsTouched && !state.runPreferencesApplied) {
    const curlInput = el('curl-parallelism');
    if (curlInput) {
      curlInput.max = String(settings.curl_parallelism_max || 10);
      curlInput.value = String(settings.curl_parallelism_default || 4);
    }
    const finderIpv6 = el('enable-ipv6');
    if (finderIpv6) finderIpv6.checked = Boolean(settings.enable_ipv6);
    if (runCurlMaxTime) runCurlMaxTime.value = String(settings.curl_max_time || 2);
    if (runCurlMaxTimeQuic) runCurlMaxTimeQuic.value = String(settings.curl_max_time_quic || 2);
    if (runCurlMaxTimeDoh) runCurlMaxTimeDoh.value = String(settings.curl_max_time_doh || 2);
  } else {
    renderRunModeNote();
  }
  renderDiscoveryProfiles();
  renderV2flyCategoryCatalog();
  renderV2flyPreview();
  renderPresetManager();
}
function renderReleaseInfo(){
  const version = (state.status || {}).version || '-';
  const current = el('settings-release-current');
  if (current) current.textContent = `v${String(version).replace(/^v/, '')}`;
  const stable = el('settings-release-stable');
  const prerelease = el('settings-release-prerelease');
  const stableLink = el('settings-release-stable-link');
  const prereleaseLink = el('settings-release-prerelease-link');
  const result = el('settings-release-result');
  const log = el('settings-release-log');
  const selectedChannel = el('settings-update-channel')?.value || (state.settings || {}).update_channel || 'stable';
  const selectedRelease = selectedChannel === 'prerelease' ? state.releasePrerelease : state.releaseStable;
  if (stable) stable.textContent = releaseVersionLabel(state.releaseStable);
  if (prerelease) prerelease.textContent = releaseVersionLabel(state.releasePrerelease);
  if (stableLink && state.releaseStable && state.releaseStable.url) stableLink.href = state.releaseStable.url;
  if (prereleaseLink && state.releasePrerelease && state.releasePrerelease.url) prereleaseLink.href = state.releasePrerelease.url;
  if (log) {
    const tail = state.releaseUpdate && state.releaseUpdate.log_tail ? state.releaseUpdate.log_tail : '';
    log.textContent = tail || 'Лог обновления пока пуст. Он появится после постановки обновления в очередь и работы helper-скрипта.';
  }
  if (!selectedRelease) {
    if (result) {
      result.hidden = true;
      result.textContent = '';
    }
    return;
  }
  if (result) {
    result.hidden = false;
    if (state.releaseUpdate) {
      const queued = state.releaseUpdate;
      const snapshot = queued.snapshot && queued.snapshot.id ? queued.snapshot.id : (queued.snapshot || {});
      const status = queued.status || 'queued';
      const installed = queued.installed_version ? ` Установленная версия: v${String(queued.installed_version).replace(/^v/, '')}.` : '';
      const verified = queued.verified ? ' Проверка версии: успешно.' : (status === 'success' ? ' Проверка версии: завершена.' : ' Проверка версии: ожидается после установки.');
      const log = queued.log_path ? ` Лог: ${queued.log_path}.` : '';
      const error = queued.error ? ` Ошибка: ${queued.error}.` : '';
      const rollback = status === 'failed' && queued.rollback_instruction ? ` Откат: ${queued.rollback_instruction}` : '';
      const statusText = {
        queued: 'Обновление поставлено в очередь',
        running: 'Обновление выполняется',
        success: 'Обновление завершено и версия проверена',
        failed: 'Обновление завершилось ошибкой'
      }[status] || `Статус обновления: ${status}`;
      result.textContent = `${statusText}. Перед обновлением создан бекап: ${snapshot || '-'}.${installed}${verified}${log}${error}${rollback}`;
      return;
    }
    if (selectedRelease.checked) {
      const update = selectedRelease.update_available ? 'Доступно обновление.' : 'Текущая версия не старее найденной.';
      const published = selectedRelease.published_at ? ` Опубликовано: ${friendlyDate(selectedRelease.published_at)}.` : '';
      const body = selectedRelease.body ? `\n\n${String(selectedRelease.body).slice(0, 1200)}` : '';
      result.textContent = `${update} Канал: ${selectedRelease.channel}. Версия: ${selectedRelease.available_version || '-'}.${published}${body}`;
    } else {
      result.textContent = `Не удалось проверить релизы: ${selectedRelease.error || 'нет ответа GitHub'}. Ссылки на страницу релизов оставлены.`;
    }
  }
}
function releaseVersionLabel(release){
  if (state.releaseChecking && !release) return 'Проверяется...';
  if (!release) return 'Не проверялось';
  if (!release.checked) return 'Ошибка проверки';
  const suffix = release.update_available ? ' доступно' : ' актуально';
  return `${release.available_version || '-'} · ${suffix}`;
}
function currentSettingsFromForm(){
  const current = state.settings || {};
  const timeouts = runTimeoutSettings();
  return {
    enable_ipv6: Boolean(el('settings-enable-ipv6')?.checked),
    debug_stdout: Boolean(el('settings-debug-stdout')?.checked),
    curl_parallelism_max: Number(el('settings-curl-max')?.value || 10),
    curl_parallelism_default: Number(current.curl_parallelism_default || 4),
    ...timeouts,
    update_channel: el('settings-update-channel')?.value || current.update_channel || 'stable'
  };
}
async function saveLaunchTimeoutDefaultsNow(){
  const payload = currentSettingsFromForm();
  try {
    const data = await postJson('/api/settings', { settings: payload });
    state.settings = data.settings || { ...(state.settings || {}), ...payload };
    state.settingsTouched = false;
    renderRunLaunchSummary();
  } catch (_error) {
    // Best-effort persistence: the run payload already contains the selected timeout values.
  }
}
async function saveSettings(){
  try {
    const data = await postJson('/api/settings', { settings: currentSettingsFromForm() });
    state.settings = data.settings || {};
    state.settingsTouched = false;
    renderSettings();
    setMessage('Настройки сохранены', 'good');
  } catch (error) {
    setMessage(`Ошибка сохранения настроек: ${error.message}`, 'bad');
  }
}
async function checkReleases(options = {}){
  const silent = Boolean(options.silent);
  const channel = el('settings-update-channel')?.value || 'stable';
  state.releaseChecking = true;
  renderReleaseInfo();
  try {
    const data = await getJson(`/api/releases?channel=${encodeURIComponent(channel)}`);
    rememberReleasePayload(data || {});
    state.releaseChecked = true;
    renderReleaseInfo();
    if (!silent) setMessage('Обновления проверены', 'good');
  } catch (error) {
    if (!silent) setMessage(`Ошибка проверки релизов: ${error.message}`, 'bad');
  } finally {
    state.releaseChecking = false;
    renderReleaseInfo();
  }
}
function rememberReleasePayload(data){
  const releases = (data || {}).releases || {};
  if (releases.stable) state.releaseStable = releases.stable;
  if (releases.prerelease) state.releasePrerelease = releases.prerelease;
  state.releaseInfo = (data || {}).release || state.releaseInfo;
  if (state.releaseInfo && state.releaseInfo.channel === 'stable') state.releaseStable = state.releaseInfo;
  if (state.releaseInfo && state.releaseInfo.channel === 'prerelease') state.releasePrerelease = state.releaseInfo;
}
async function updateFromRelease(){
  const channel = el('settings-update-channel')?.value || 'stable';
  try {
    const planData = await getJson(`/api/releases/update-plan?channel=${encodeURIComponent(channel)}`);
    const plan = (planData || {}).plan || {};
    state.releaseInfo = plan.release || state.releaseInfo;
    if (state.releaseInfo && state.releaseInfo.channel === 'stable') state.releaseStable = state.releaseInfo;
    if (state.releaseInfo && state.releaseInfo.channel === 'prerelease') state.releasePrerelease = state.releaseInfo;
    renderReleaseInfo();
    if (!plan.can_update) {
      setMessage(`Обновление не готово: ${plan.blocked_reason || 'нет доступного обновления'}`, 'warn');
      return;
    }
    const steps = Array.isArray(plan.steps) ? plan.steps.join('\\n- ') : '';
    const confirmed = window.confirm(`Запустить обновление приложения из выбранного канала?\\n\\nБудет выполнено:\\n- ${steps}`);
    if (!confirmed) return;
    const data = await postJson('/api/releases/update', { channel });
    state.releaseUpdate = (data || {}).update || null;
    state.releaseInfo = state.releaseUpdate ? state.releaseUpdate.release : state.releaseInfo;
    if (state.releaseInfo && state.releaseInfo.channel === 'stable') state.releaseStable = state.releaseInfo;
    if (state.releaseInfo && state.releaseInfo.channel === 'prerelease') state.releasePrerelease = state.releaseInfo;
    renderReleaseInfo();
    setMessage('Обновление поставлено в очередь. Сервис может кратко пропасть и подняться снова.', 'good');
  } catch (error) {
    setMessage(`Обновление не запущено: ${error.message}`, 'bad');
  }
}
function toggleUpdateLog(){
  const log = el('settings-release-log');
  if (!log) return;
  log.hidden = !log.hidden;
}
function v2flyAllCategories(){
  const categories = (state.v2flyCategories || {}).categories;
  return Array.isArray(categories) ? categories : [];
}
function v2flyCategoryQuery(){
  return String(el('v2fly-category-search')?.value || '').trim().toLowerCase();
}
function v2flyExactCategory(){
  const query = v2flyCategoryQuery();
  if (!query) return '';
  return v2flyAllCategories().includes(query) ? query : '';
}
function v2flyCategories(){
  const category = v2flyExactCategory();
  return category ? [category] : [];
}
function clearV2flyDomains(){
  const domains = el('v2fly-domains');
  if (!domains) return;
  domains.value = '';
  updateEditorLineNumbers('v2fly-domains');
}
function suggestV2flyPresetName(){
  const nameInput = el('v2fly-preset-name');
  if (!nameInput) return;
  const current = String(nameInput.value || '').trim();
  if (current && !current.startsWith('v2fly-')) return;
  const categories = v2flyCategories();
  if (!categories.length) return;
  nameInput.value = `v2fly-${categories.slice(0, 3).join('-')}`.slice(0, 80);
}
function v2flyPayload(){
  return {
    scope: 'finder',
    name: String(el('v2fly-preset-name')?.value || '').trim(),
    categories: v2flyCategories(),
    domains: parseDomains(el('v2fly-domains')?.value || '')
  };
}
function renderV2flyPreview(){
  const target = el('v2fly-preview-result');
  if (!target) return;
  const preview = state.v2flyPreview;
  target.classList.toggle('bad', Boolean(preview && preview.error));
  if (!preview) {
    target.textContent = 'Список не проверялся.';
    return;
  }
  if (preview.loading) {
    target.textContent = preview.message || 'Загружаю домены выбранной группы...';
    return;
  }
  if (preview.error) {
    target.textContent = preview.message || 'Ошибка v2fly.';
    return;
  }
  const added = Array.isArray(preview.added) ? preview.added.length : 0;
  const removed = Array.isArray(preview.removed) ? preview.removed.length : 0;
  const skipped = preview.skipped && typeof preview.skipped === 'object'
    ? Object.values(preview.skipped).reduce((sum, value) => sum + Number(value || 0), 0)
    : 0;
  const coverageNote = preview.coverage_note ? 'Публично известный проверяемый набор, не гарантия полного покрытия сервиса.' : '';
  target.innerHTML = [
    `<div><strong>${esc(preview.preset || '-')}</strong>: ${esc(preview.count || 0)} доменов</div>`,
    `<div>Добавится: ${esc(added)}, уйдет: ${esc(removed)}, без изменений: ${esc(preview.unchanged_count || 0)}</div>`,
    skipped ? `<div>Часть правил не добавлена автоматически: ${esc(skipped)}</div>` : '',
    coverageNote ? `<div>${esc(coverageNote)}</div>` : ''
  ].join('');
}
function setV2flyLocalError(message){
  state.v2flyPreview = { error: true, message };
  renderV2flyPreview();
}
function renderV2flyCategoryCatalog(){
  const target = el('v2fly-category-status');
  const data = state.v2flyCategories || {};
  const categories = v2flyAllCategories();
  const query = v2flyCategoryQuery();
  const visible = query ? categories.filter((category) => category.includes(query)) : categories;
  const options = el('v2fly-category-options');
  if (options) options.innerHTML = visible.slice(0, 500).map((category) => `<option value="${esc(category)}"></option>`).join('');
  const matchList = el('v2fly-category-matches');
  const exact = v2flyExactCategory();
  if (matchList) {
    const matches = visible.slice(0, 24);
    matchList.innerHTML = matches.length
      ? matches.map((category) => `<button class="secondary category-match${category === exact ? ' active' : ''}" type="button" data-action="v2fly-select-category" data-category="${esc(category)}">${esc(category)}</button>`).join('')
      : '';
  }
  const button = document.querySelector('[data-action="v2fly-load-categories"]');
  const loading = state.v2flyCategorySource === 'loading';
  if (button) {
    button.disabled = loading;
    button.textContent = loading ? 'Читаю каталог' : 'Перечитать каталог';
    button.title = 'Перечитывает локальный каталог групп v2fly. Каталог скачивается при установке или обновлении сервиса.';
  }
  if (!target) return;
  if (loading) {
    target.textContent = 'Читаю локальный каталог v2fly...';
    return;
  }
  if (!categories.length) {
    target.textContent = data.error_message ? `Локальный каталог v2fly недоступен: ${data.error_message}` : 'Локальный каталог v2fly еще не подготовлен. Он скачивается при установке или обновлении сервиса.';
    return;
  }
  const selected = exact || '';
  const queryText = query ? ` Найдено по вводу: ${visible.length}.` : '';
  const selectText = selected ? ` Выбрано: ${selected}.` : (query ? ' Выберите точную группу из подсказок ниже.' : '');
  target.textContent = `Локальный каталог готов: ${data.all_count || categories.length} групп.${queryText}${selectText}`;
}
function presetManagerMeta(scope){
  return (state.customPresetMeta && state.customPresetMeta[scope]) || {};
}
function renderPresetManager(){
  const nameSelect = el('preset-manager-name');
  if (!nameSelect) return;
  const manager = state.presetManager;
  const scope = 'finder';
  const entries = managerPresetEntries();
  const names = entries.map((item) => item.name);
  if (!manager.name || !names.includes(manager.name)) manager.name = names[0] || '';
  const entry = manager.name ? managerPresetEntry(manager.name) : null;
  const isStoredUser = manager.name ? hasCustomPreset(scope, manager.name) : false;
  const isSystem = entry && entry.kind === 'system';
  const sourceScope = isStoredUser ? customPresetSourceScope(scope, manager.name) : scope;
  manager.scope = sourceScope;
  nameSelect.innerHTML = entries.length
    ? entries.map((item) => `<option value="${esc(item.name)}">${esc(item.label)} (${esc(item.count)})</option>`).join('')
    : '<option value="">Нет списков</option>';
  nameSelect.value = manager.name || '';
  const meta = isSystem ? systemPresetMeta(sourceScope, manager.name) : (isStoredUser ? presetManagerMeta(sourceScope)[manager.name] : null);
  const count = meta ? `${meta.enabled_count || 0}/${meta.total_count || 0}` : (entry ? `${entry.count}/${entry.count}` : '0');
  setText('preset-manager-count', count);
  const deleteButton = document.querySelector('button[data-action="preset-editor-delete"]');
  if (deleteButton) deleteButton.disabled = !isStoredUser || isSystem;
  const note = el('preset-manager-note');
  if (!manager.name) {
    note.textContent = 'Списков пока нет. Создайте список в подборе или импортируйте его из v2fly.';
    return;
  }
  const updated = meta && meta.updated_at ? ` · обновлено ${friendlyDate(meta.updated_at)}` : '';
  if (isSystem) {
    note.textContent = `Системный список "${entry.label}" всегда существует. Домены можно менять до пустого списка, удалить сам список нельзя. Доменов: ${meta ? meta.enabled_count : entry?.count || 0}${updated}.`;
    return;
  }
  note.textContent = `Редактируется список "${manager.name}". Доменов: ${meta ? meta.enabled_count : entry?.count || 0}${updated}${isStoredUser ? '' : ' · готовый список станет редактируемым после сохранения'}.`;
}
function renderPresetEditorPreview(preview){
  const target = el('preset-editor-preview');
  if (!target) return;
  if (!preview) {
    target.textContent = 'Изменения еще не проверялись.';
    return;
  }
  target.innerHTML = [
    `<div><strong>${esc(preview.name)}</strong>: ${esc(preview.total)} уникальных доменов</div>`,
    `<div>Добавится: ${esc(preview.added)}, удалится: ${esc(preview.removed)}, без изменений: ${esc(preview.unchanged)}</div>`
  ].join('');
}
function presetEditorDomains(){
  return uniqueDomains(parseDomains(el('preset-editor-domains')?.value || ''));
}
function presetEditorScope(){
  return 'finder';
}
function presetEditorName(){
  return String(el('preset-manager-name')?.value || '').trim();
}
function presetEditorKind(){
  const entry = managerPresetEntry(presetEditorName());
  return entry && entry.kind === 'system' ? 'system' : 'user';
}
async function loadPresetEditorFromSelection(options){
  const opts = options || {};
  const scope = presetEditorScope();
  const name = el('preset-manager-name')?.value || state.presetManager.name || '';
  if (!name) {
    if (!opts.silent) setMessage('Выберите список', 'warn');
    return;
  }
  try {
    const domains = await fetchAllPresetDomains(scope, name);
    const domainsInput = el('preset-editor-domains');
    if (domainsInput) {
      domainsInput.value = domains.join('\\n');
      updateEditorLineNumbers('preset-editor-domains');
    }
    renderPresetEditorPreview({ name, total: domains.length, added: 0, removed: 0, unchanged: domains.length });
    if (!opts.silent) setMessage('Список загружен в редактор', 'good');
  } catch (error) {
    if (!opts.silent) setMessage(`Ошибка загрузки списка в редактор: ${error.message}`, 'bad');
  }
}
async function buildPresetEditorPreview(){
  const scope = presetEditorScope();
  const name = presetEditorName();
  const kind = presetEditorKind();
  const domains = presetEditorDomains();
  if (!name || (!domains.length && kind !== 'system')) {
    setMessage(kind === 'system' ? 'Выберите список' : 'Выберите список и оставьте хотя бы один домен', 'warn');
    return null;
  }
  let current = [];
  if (hasCustomPreset(scope, name) || hasSystemPreset(scope, name) || managerPresetEntry(name)) {
    current = await fetchAllPresetDomains(scope, name);
  }
  const currentSet = new Set(current);
  const nextSet = new Set(domains);
  const added = domains.filter((domain) => !currentSet.has(domain));
  const removed = current.filter((domain) => !nextSet.has(domain));
  const preview = {
    scope,
    name,
    kind,
    total: domains.length,
    added: added.length,
    removed: removed.length,
    unchanged: domains.length - added.length
  };
  renderPresetEditorPreview(preview);
  return preview;
}
async function savePresetEditor(){
  try {
    const preview = await buildPresetEditorPreview();
    if (!preview) return;
    const domains = presetEditorDomains();
    const data = await postJson('/api/presets/save', { scope: preview.scope, name: preview.name, kind: preview.kind, domains });
    mergePresetResponse(data);
    if (preview.kind === 'system') {
      if (!state.systemPresets[preview.scope]) state.systemPresets[preview.scope] = {};
      state.systemPresets[preview.scope][preview.name] = domains;
    } else {
      if (!state.customPresets[preview.scope]) state.customPresets[preview.scope] = {};
      state.customPresets[preview.scope][preview.name] = domains;
      localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify(state.customPresets));
    }
    state.presetManager.scope = preview.scope;
    state.presetManager.name = preview.name;
    renderPresetSelects();
    renderPresetManager();
    setMessage('Список сохранен', 'good');
  } catch (error) {
    setMessage(`Ошибка сохранения списка: ${error.message}`, 'bad');
  }
}
async function deletePresetEditor(){
  const scope = presetEditorScope();
  const name = presetEditorName();
  const entry = managerPresetEntry(name);
  if (!name || !entry) {
    setMessage('Выберите пользовательский список', 'warn');
    return;
  }
  if (entry.kind !== 'user') {
    setMessage('Системные и готовые списки удалить нельзя', 'warn');
    return;
  }
  try {
    const data = await postJson('/api/presets/delete-users-lists', { scope, name });
    if (state.customPresets[scope]) delete state.customPresets[scope][name];
    mergePresetResponse(data);
    localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify(state.customPresets));
    state.presetManager.name = '';
    renderPresetSelects();
    renderPresetManager();
    await loadPresetEditorFromSelection({ silent: true });
    setMessage('Пользовательский список удален', 'good');
  } catch (error) {
    setMessage(`Ошибка удаления списка: ${error.message}`, 'bad');
  }
}
function presetNewName(){
  return String(el('preset-new-name')?.value || '').trim();
}
function presetNewDomains(){
  return uniqueDomains(parseDomains(el('preset-new-domains')?.value || ''));
}
function renderPresetNewPreview(message, tone){
  const target = el('preset-new-preview');
  if (!target) return;
  target.textContent = message || 'Новый список еще не сохранялся.';
  target.classList.toggle('bad', tone === 'bad');
}
async function savePresetNew(){
  const scope = 'finder';
  const name = presetNewName();
  const domains = presetNewDomains();
  if (!name || !domains.length) {
    renderPresetNewPreview('Укажите название нового списка и хотя бы один домен.', 'bad');
    setMessage('Укажите название нового списка и хотя бы один домен', 'warn');
    return;
  }
  if (hasSystemPreset(scope, name)) {
    renderPresetNewPreview('Это имя занято системным списком.', 'bad');
    setMessage('Это имя занято системным списком', 'warn');
    return;
  }
  try {
    const data = await postJson('/api/presets/save', { scope, name, domains });
    mergePresetResponse(data);
    if (!state.customPresets[scope]) state.customPresets[scope] = {};
    state.customPresets[scope][name] = domains;
    localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify(state.customPresets));
    state.presetManager.scope = scope;
    state.presetManager.name = name;
    const nameInput = el('preset-new-name');
    const domainsInput = el('preset-new-domains');
    if (nameInput) nameInput.value = '';
    if (domainsInput) {
      domainsInput.value = '';
      updateEditorLineNumbers('preset-new-domains');
    }
    renderPresetSelects();
    renderPresetManager();
    await loadPresetEditorFromSelection({ silent: true });
    renderPresetNewPreview(`Список сохранен: ${name}, доменов ${domains.length}.`, 'good');
    setMessage('Новый список сохранен', 'good');
  } catch (error) {
    renderPresetNewPreview(`Ошибка сохранения: ${error.message}`, 'bad');
    setMessage(`Ошибка сохранения нового списка: ${error.message}`, 'bad');
  }
}
async function exportPresetEditor(){
  try {
    let domains = presetEditorDomains();
    const scope = presetEditorScope();
    const name = presetEditorName() || el('preset-manager-name')?.value || 'domains';
    if (!domains.length && name) domains = await fetchAllPresetDomains(scope, name);
    if (!domains.length) {
      setMessage('Нет доменов для экспорта', 'warn');
      return;
    }
    const blob = new Blob([domains.join('\\n') + '\\n'], { type: 'text/plain;charset=utf-8' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `${name.replace(/[^a-z0-9._-]+/gi, '-') || 'domains'}.txt`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(link.href);
    setMessage('TXT сформирован', 'good');
  } catch (error) {
    setMessage(`Ошибка экспорта списка: ${error.message}`, 'bad');
  }
}
async function loadV2flyCategories(refreshCatalog){
  state.v2flyCategorySource = 'loading';
  renderV2flyCategoryCatalog();
  try {
    const params = new URLSearchParams();
    params.set('limit', '5000');
    const data = await getJson(`/api/domain-sources/v2fly/categories?${params.toString()}`);
    state.v2flyCategories = data;
    state.v2flyCategorySource = data.source || '';
    renderV2flyCategoryCatalog();
  } catch (error) {
    state.v2flyCategories = { categories: [], error_message: error.message };
    state.v2flyCategorySource = '';
    renderV2flyCategoryCatalog();
    setV2flyLocalError(`Не удалось прочитать локальный каталог v2fly: ${error.message}`);
  }
}
async function previewV2flyPreset(){
  const payload = v2flyPayload();
  if (!payload.name) {
    setV2flyLocalError('Укажите название пресета.');
    return;
  }
  if (!v2flyAllCategories().length) {
    setV2flyLocalError('Локальный каталог v2fly не подготовлен. Повторите установку или обновление сервиса.');
    return;
  }
  if (!payload.categories.length) {
    setV2flyLocalError('Выберите точное название группы v2fly из подсказок.');
    return;
  }
  state.v2flyPreview = { loading: true, message: 'Загружаю домены выбранной группы...' };
  renderV2flyPreview();
  try {
    const data = await postJson('/api/domain-sources/v2fly/preview', payload);
    state.v2flyPreview = data;
    if (Array.isArray(data.domains)) {
      el('v2fly-domains').value = data.domains.join('\\n');
      updateEditorLineNumbers('v2fly-domains');
    }
    renderV2flyPreview();
    setMessage('Список v2fly проверен', 'good');
  } catch (error) {
    setV2flyLocalError(`Ошибка проверки v2fly: ${error.message}`);
  }
}
async function importV2flyPreset(){
  const payload = v2flyPayload();
  if (!payload.name) {
    setV2flyLocalError('Укажите название пресета.');
    return;
  }
  if (!v2flyAllCategories().length) {
    setV2flyLocalError('Локальный каталог v2fly не подготовлен. Повторите установку или обновление сервиса.');
    return;
  }
  if (!payload.categories.length) {
    setV2flyLocalError('Выберите точное название группы v2fly из подсказок.');
    return;
  }
  state.v2flyPreview = { loading: true, message: 'Сохраняю доменный пресет...' };
  renderV2flyPreview();
  try {
    const data = await postJson('/api/domain-sources/v2fly/import', payload);
    state.v2flyPreview = data;
    mergePresetResponse(data);
    if (data.preset) {
      state.presetManager.scope = 'finder';
      state.presetManager.name = data.preset;
    }
    renderPresetSelects();
    renderPresetManager();
    if (Array.isArray(data.domains)) {
      el('v2fly-domains').value = data.domains.join('\\n');
      updateEditorLineNumbers('v2fly-domains');
    }
    renderV2flyPreview();
    if (data.preset) await loadPresetEditorFromSelection({ silent: true });
    setMessage(`Пресет сохранен: ${data.count || 0} доменов`, 'good');
  } catch (error) {
    setV2flyLocalError(`Ошибка сохранения v2fly: ${error.message}`);
  }
}
function formatDuration(seconds){
  if (!Number.isFinite(seconds)) return '-';
  if (seconds <= 0) return '0 мин';
  const minutes = Math.ceil(seconds / 60);
  if (minutes < 60) return `${minutes} мин`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} ч ${rest} мин` : `${hours} ч`;
}
function scrollLogToBottom(){
  const logNode = el('finder-log');
  if (!logNode) return;
  requestAnimationFrame(() => {
    logNode.scrollTop = logNode.scrollHeight;
  });
}
function renderAll(options){
  const opts = options || {};
  renderPresetSelects();
  renderSettings();
  useRunPreferencesOnce();
  if (!state.domainsInitialized && !state.domainsTouched && !el('finder-domains').value.trim() && state.domainSets) {
    const selected = el('finder-preset-select')?.value || 'system:required';
    const domains = uniqueDomains(presetDomains('finder', selected));
    el('finder-domains').value = domains.join('\\n');
    state.domainsInitialized = true;
  }
  renderMetrics();
  renderRunLaunchSummary();
  if (!opts.skipCandidates) renderCandidates();
  renderRuns();
  renderLog();
  renderBackups();
  updateAllEditorLineNumbers();
  syncActiveTabUi();
}
function renderCandidatesOnly(){
  renderMetrics();
  renderCandidates();
  updateEditorLineNumbers('common-domains');
}
function ensureCandidateViewLoaded(){
  if (state.candidateView === 'domain') {
    if (!state.candidateDomainsLoaded) refreshDomainIndex();
    return;
  }
  const selectedDomains = selectedCommonDomains();
  const loaded = prepareCommonCandidateState();
  if (selectedDomains.length < 2) return;
  if (!loaded) refreshCandidates(true);
}
function setCandidateView(view){
  state.candidateView = view;
  if (view === 'common') prepareCommonCandidateState();
  renderCandidatesOnly();
  ensureCandidateViewLoaded();
}
function candidateParams(offset, options){
  const params = new URLSearchParams();
  params.set('limit', String(CANDIDATE_PAGE_LIMIT));
  params.set('offset', String(Math.max(0, offset || 0)));
  params.set('view', state.candidateView);
  if (options && options.view) params.set('view', options.view);
  if (options && options.domain) params.set('domain', options.domain);
  if ((options && options.view === 'common') || (!options && state.candidateView === 'common')) {
    const domains = Array.isArray(options?.domains) ? options.domains : selectedCommonDomains();
    if (domains.length) params.set('domains', domains.join(','));
  }
  return params;
}
async function refreshDomainIndex(reset = true){
  const requestId = ++domainIndexRequestSeq;
  const offset = reset ? 0 : state.candidateDomainOffset;
  state.candidateLoading = true;
  renderCandidatesOnly();
  try {
    const params = new URLSearchParams();
    params.set('limit', String(DOMAIN_PAGE_LIMIT));
    params.set('offset', String(Math.max(0, offset || 0)));
    const data = await getJson(`/api/strategy-finder/candidate-domains?${params.toString()}`);
    if (requestId !== domainIndexRequestSeq) return;
    const rows = data.domains || [];
    state.candidateDomains = reset ? rows : [...state.candidateDomains, ...rows];
    state.candidateDomainTotal = Number(data.total || 0);
    state.candidateDomainStrategyTotal = Number(data.strategy_total || 0);
    state.candidateDomainOffset = Number(data.offset || offset) + rows.length;
    state.candidateDomainHasMore = Boolean(data.has_more);
    if (state.candidateDomainTotal > 0) state.lastCandidateDomainTotal = state.candidateDomainTotal;
    if (state.candidateDomainStrategyTotal > 0) state.lastCandidateDomainStrategyTotal = state.candidateDomainStrategyTotal;
    rememberCandidateVersion(data.version || null);
    updateTestedDomains(data.tested_domains);
    state.candidateDomainsLoaded = true;
    state.candidateUpdatedAt = new Date().toISOString();
    state.candidateLoading = false;
    renderCandidatesOnly();
  } catch (error) {
    if (requestId !== domainIndexRequestSeq) return;
    state.candidateLoading = false;
    renderCandidatesOnly();
    setMessage(`Ошибка загрузки доменов: ${error.message}`, 'bad');
  }
}
async function refreshDomainStrategies(domain, reset){
  const key = String(domain || '').trim();
  if (!key) return;
  const current = state.domainStrategies[key] || { candidates: [], total: 0, hasMore: false, loaded: false };
  const offset = reset ? 0 : current.candidates.length;
  try {
    const data = await getJson(`/api/strategy-finder/candidates?${candidateParams(offset, { view: 'domain', domain: key }).toString()}`);
    const rows = data.candidates || [];
    state.domainStrategies[key] = {
      candidates: reset ? rows : [...current.candidates, ...rows],
      total: Number(data.total || 0),
      hasMore: Boolean(data.has_more),
      loaded: true,
      loadingAll: false,
      version: data.version || state.candidateKnownVersion
    };
    rememberCandidateVersion(data.version || null);
    updateTestedDomains(data.tested_domains);
    renderCandidatesOnly();
  } catch (error) {
    setMessage(`Ошибка загрузки стратегий домена: ${error.message}`, 'bad');
  }
}
async function loadAllDomainStrategies(domain){
  const key = String(domain || '').trim();
  if (!key) return;
  const current = state.domainStrategies[key] || { candidates: [], total: 0, hasMore: false, loaded: false };
  if (current.loadingAll) return;
  let candidates = Array.isArray(current.candidates) ? current.candidates.slice() : [];
  let total = Number(current.total || candidates.length);
  let hasMore = Boolean(current.hasMore);
  state.domainStrategies[key] = { ...current, candidates, total, hasMore, loaded: true, loadingAll: true };
  renderCandidates();
  try {
    let guard = 0;
    while (hasMore && guard < 1000) {
      const data = await getJson(`/api/strategy-finder/candidates?${candidateParams(candidates.length, { view: 'domain', domain: key }).toString()}`);
      const rows = data.candidates || [];
      total = Number(data.total || total || candidates.length);
      hasMore = Boolean(data.has_more);
      if (!rows.length) {
        hasMore = false;
        break;
      }
      candidates = [...candidates, ...rows];
      updateTestedDomains(data.tested_domains);
      rememberCandidateVersion(data.version || null);
      guard += 1;
    }
    state.domainStrategies[key] = { candidates, total, hasMore, loaded: true, loadingAll: false, version: state.candidateKnownVersion };
    renderCandidatesOnly();
  } catch (error) {
    state.domainStrategies[key] = { candidates, total, hasMore, loaded: true, loadingAll: false, version: state.candidateKnownVersion };
    setMessage(`Ошибка загрузки всех стратегий домена: ${error.message}`, 'bad');
    renderCandidatesOnly();
  }
}
async function loadAllCommonStrategies(){
  if (state.commonLoadingAll || !state.candidateHasMore) return;
  const domains = selectedCommonDomains();
  if (domains.length < 2) return;
  const queryKey = currentCandidateQueryKey({ view: 'common', domains });
  let candidates = Array.isArray(state.candidates) ? state.candidates.slice() : [];
  let total = Number(state.candidateTotal || candidates.length);
  let hasMore = Boolean(state.candidateHasMore);
  state.commonLoadingAll = true;
  renderCandidatesOnly();
  try {
    let guard = 0;
    while (hasMore && guard < 1000) {
      const data = await getJson(`/api/strategy-finder/candidates?${candidateParams(candidates.length, { view: 'common', domains }).toString()}`);
      if (state.candidateQueryKey !== queryKey) {
        state.commonLoadingAll = false;
        return;
      }
      const rows = data.candidates || [];
      total = Number(data.total || total || candidates.length);
      hasMore = Boolean(data.has_more);
      if (!rows.length) {
        hasMore = false;
        break;
      }
      candidates = [...candidates, ...rows];
      updateTestedDomains(data.tested_domains);
      rememberCandidateVersion(data.version || null);
      guard += 1;
    }
    if (state.candidateQueryKey !== queryKey) {
      state.commonLoadingAll = false;
      return;
    }
    state.candidates = candidates;
    state.candidateTotal = total;
    state.candidateOffset = Math.max(0, candidates.length - CANDIDATE_PAGE_LIMIT);
    state.candidateHasMore = hasMore;
    state.candidatesLoaded = true;
    state.commonLoadingAll = false;
    storeCommonCandidateCache(queryKey);
    renderCandidatesOnly();
  } catch (error) {
    setMessage(`Ошибка загрузки всех общих стратегий: ${error.message}`, 'bad');
    state.commonLoadingAll = false;
    renderCandidatesOnly();
  }
}
async function refreshCandidates(reset){
  const requestId = ++candidateRequestSeq;
  const offset = reset ? 0 : state.candidates.length;
  const queryKey = currentCandidateQueryKey();
  state.commonLoadingAll = false;
  state.candidateLoading = true;
  renderCandidatesOnly();
  try {
    const data = await getJson(`/api/strategy-finder/candidates?${candidateParams(offset).toString()}`);
    if (requestId !== candidateRequestSeq) return;
    const rows = data.candidates || [];
    state.candidates = reset ? rows : [...state.candidates, ...rows];
    state.candidateTotal = Number(data.total || 0);
    state.candidateOffset = Number(data.offset || 0);
    state.candidateHasMore = Boolean(data.has_more);
    rememberCandidateVersion(data.version || null);
    updateTestedDomains(data.tested_domains);
    state.candidatesLoaded = true;
    state.candidateQueryKey = queryKey;
    state.candidateUpdatedAt = new Date().toISOString();
    state.candidateLoading = false;
    if (queryKey.startsWith('common:')) storeCommonCandidateCache(queryKey);
    renderCandidatesOnly();
  } catch (error) {
    if (requestId !== candidateRequestSeq) return;
    state.candidateLoading = false;
    renderCandidatesOnly();
    setMessage(`Ошибка загрузки кандидатов: ${error.message}`, 'bad');
  }
}
function scheduleCandidateRefresh(){
  if (candidateRefreshTimer) clearTimeout(candidateRefreshTimer);
  candidateRefreshTimer = setTimeout(() => {
    candidateRefreshTimer = null;
    if (state.candidateView === 'domain') {
      state.domainStrategies = {};
      state.openCandidateDomains = {};
      refreshDomainIndex();
    } else {
      state.candidateResultRequested = false;
      prepareCommonCandidateState();
      renderCandidatesOnly();
      if (selectedCommonDomains().length >= 2) refreshCandidates(true);
    }
  }, 350);
}
function trimTextLines(text, maxLines){
  const lines = String(text || '').split('\\n');
  if (lines.length <= maxLines) return lines.join('\\n');
  return lines.slice(lines.length - maxLines).join('\\n');
}
function appendLogText(base, addition){
  const left = String(base || '');
  const right = String(addition || '');
  if (!left || !right || left.endsWith('\\n') || right.startsWith('\\n')) return left + right;
  return `${left}\\n${right}`;
}
function latestLogUrl(incremental){
  if (!incremental || !state.finderLog || !state.finderLog.stdout_log) {
    return '/api/strategy-finder/latest-log';
  }
  const params = new URLSearchParams();
  params.set('stdout_log', state.finderLog.stdout_log || '');
  params.set('stdout_size', String(state.finderLog.stdout_size || 0));
  params.set('stderr_log', state.finderLog.stderr_log || '');
  params.set('stderr_size', String(state.finderLog.stderr_size || 0));
  return `/api/strategy-finder/latest-log?${params.toString()}`;
}
function mergeLogPayload(previous, next){
  if (!previous || !next) return next;
  if (next.progress) next.progress.received_at_ms = Date.now();
  const sameRun = previous.run_id && next.run_id && previous.run_id === next.run_id;
  const sameStdout = sameRun && previous.stdout_log && previous.stdout_log === next.stdout_log;
  const sameStderr = sameRun && previous.stderr_log && previous.stderr_log === next.stderr_log;
  if (sameStdout && next.stdout_append) {
    next.stdout_tail = trimTextLines(appendLogText(previous.stdout_tail, next.stdout_append), 200);
  }
  if (sameStderr && next.stderr_append) {
    next.stderr_tail = trimTextLines(appendLogText(previous.stderr_tail, next.stderr_append), 200);
  }
  if (sameStdout && !next.stdout_tail && !next.stdout_append) next.stdout_tail = previous.stdout_tail || '';
  if (sameStderr && !next.stderr_tail && !next.stderr_append) next.stderr_tail = previous.stderr_tail || '';
  return next;
}
function mergeStatusPayload(status){
  if (!status) return false;
  const previousSettings = JSON.stringify(state.settings || {});
  state.status = status;
  state.releaseUpdate = status.release_update || state.releaseUpdate;
  if (status.candidate_version) syncCandidateVersion(status.candidate_version);
  if (status.settings) state.settings = status.settings;
  if (status.run_preferences) state.runPreferences = status.run_preferences;
  renderMetrics();
  renderLiveRun();
  renderEvents();
  const settingsChanged = previousSettings !== JSON.stringify(state.settings || {});
  if (settingsChanged) renderSettings();
  return settingsChanged;
}
async function refreshRuns(reset = true){
  const offset = reset ? 0 : state.finderRunOffset;
  state.finderRunsLoading = true;
  renderRuns();
  try {
    const finderRuns = await getJson(`/api/strategy-finder/runs?${runParams(offset).toString()}`);
    mergeRunPage(finderRuns, reset);
    renderRuns();
    renderMetrics();
  } catch (error) {
    state.finderRunsLoading = false;
    renderRuns();
    setMessage(`Ошибка обновления истории: ${error.message}`, 'bad');
  }
}
async function refreshLog(incremental = false){
  try {
    const previous = state.finderLog;
    const payload = await getJson(latestLogUrl(incremental));
    if (payload.progress) payload.progress.received_at_ms = Date.now();
    state.finderLog = incremental ? mergeLogPayload(previous, payload) : payload;
    logDirty = false;
    renderLog();
    renderMetrics();
  } catch (error) {
    setMessage(`Ошибка обновления лога: ${error.message}`, 'bad');
  }
}
async function refreshPresets(){
  try {
    const presets = await getJson('/api/presets');
    mergePresetResponse(presets);
    renderPresetSelects();
    renderPresetManager();
  } catch (error) {
    setMessage(`Ошибка обновления пресетов: ${error.message}`, 'bad');
  }
}
function handleCandidateEvent(payload){
  const version = payload && payload.version ? payload.version : null;
  if (version) syncCandidateVersion(version);
  renderMetrics();
  if (state.activeTab === 'candidates') ensureCandidateViewLoaded();
}
function handleLogEvent(){
  logDirty = true;
  if (state.activeTab === 'terminal' || isBusy()) refreshLog(true);
}
function handleStatusEvent(payload){
  mergeStatusPayload(payload);
}
function startRealtimeEvents(){
  if (!('EventSource' in window)) {
    realtimeConnected = false;
    return;
  }
  if (realtimeSource) realtimeSource.close();
  realtimeSource = new EventSource('/api/events');
  realtimeSource.addEventListener('open', () => {
    realtimeConnected = true;
  });
  realtimeSource.addEventListener('error', () => {
    realtimeConnected = false;
  });
  realtimeSource.addEventListener('status', (event) => handleStatusEvent(JSON.parse(event.data || '{}')));
  realtimeSource.addEventListener('runs', () => refreshRuns());
  realtimeSource.addEventListener('log', () => handleLogEvent());
  realtimeSource.addEventListener('candidates', (event) => handleCandidateEvent(JSON.parse(event.data || '{}')));
  realtimeSource.addEventListener('settings', () => {
    if (state.status) renderSettings();
  });
  realtimeSource.addEventListener('presets', () => refreshPresets());
}
function startRealtimeFallback(){
  if (realtimeFallbackTimer) clearInterval(realtimeFallbackTimer);
  realtimeFallbackTimer = setInterval(() => {
    if (!realtimeConnected) refresh();
  }, 30000);
}
async function refresh(){
  if (refreshInFlight) return;
  refreshInFlight = true;
  try {
    const [status, finderRuns, finderLog, domainSets, presets, settings, domainSources] = await Promise.all([
      getJson('/api/status'),
      getJson(`/api/strategy-finder/runs?${runParams(0).toString()}`),
      getJson('/api/strategy-finder/latest-log'),
      getJson('/api/strategy-finder/domains'),
      getJson('/api/presets'),
      getJson('/api/settings'),
      getJson('/api/domain-sources')
    ]);
    mergeStatusPayload(status);
    state.settings = (settings || {}).settings || status.settings || {};
    mergeRunPage(finderRuns, true);
    if (finderLog && finderLog.progress) finderLog.progress.received_at_ms = Date.now();
    state.finderLog = finderLog;
    state.domainSets = domainSets;
    state.domainSources = domainSources;
    mergePresetResponse(presets);
    renderAll({ skipCandidates: true });
    if (state.activeTab === 'candidates') ensureCandidateViewLoaded();
  } catch (error) {
    setMessage(`Ошибка обновления: ${error.message}`, 'bad');
  } finally {
    refreshInFlight = false;
  }
}
async function refreshBackups(){
  state.backupsLoading = true;
  renderBackups();
  try {
    const data = await getJson('/api/backups');
    state.backups = data.snapshots || [];
    state.backupsLoaded = true;
    state.backupsUpdatedAt = new Date().toISOString();
    state.backupsLoading = false;
    renderBackups();
  } catch (error) {
    state.backupsLoading = false;
    renderBackups();
    setMessage(`Ошибка загрузки сохранений: ${error.message}`, 'bad');
  }
}
async function createBackup(){
  try {
    const data = await postJson('/api/backups/create', {});
    if (data.queued) {
      setMessage('Подбор идет. Бекап можно создать после остановки или завершения', 'warn');
    } else if (data.created) {
      setMessage('Бекап создан', 'good');
    }
    await refreshBackups();
  } catch (error) {
    setMessage(`Ошибка создания бекапа: ${error.message}`, 'bad');
  }
}
async function restoreBackup(snapshotId){
  const id = String(snapshotId || '').trim();
  if (!id) return;
  const ok = window.confirm(`Восстановить данные из бекапа ${id}? Будут заменены найденные стратегии и связи стратегия-домен. Пользовательские пресеты не меняются.`);
  if (!ok) return;
  try {
    const data = await postJson('/api/backups/restore', { snapshot: id });
    if (data.queued) {
      setMessage('Подбор идет. Восстановление можно выполнить после остановки или завершения', 'warn');
      return;
    }
    if (data.restored) {
      setMessage('Бекап восстановлен', 'good');
      invalidateCandidateCaches();
      await refresh();
      if (state.activeTab === 'candidates') ensureCandidateViewLoaded();
    }
  } catch (error) {
    setMessage(`Ошибка восстановления бекапа: ${error.message}`, 'bad');
  }
}
async function deleteBackup(snapshotId){
  const id = String(snapshotId || '').trim();
  if (!id) return;
  const ok = window.confirm(`Удалить бекап ${id}? Архив и файлы бекапа будут удалены.`);
  if (!ok) return;
  try {
    const data = await postJson('/api/backups/delete', { snapshot: id });
    if (data.queued) {
      setMessage('Подбор идет. Бекап можно удалить после остановки или завершения', 'warn');
      return;
    }
    if (data.deleted) {
      setMessage('Бекап удален', 'good');
      await refreshBackups();
    }
  } catch (error) {
    setMessage(`Ошибка удаления бекапа: ${error.message}`, 'bad');
  }
}
async function uploadBackup(){
  const input = el('backup-upload-file');
  const file = input && input.files ? input.files[0] : null;
  if (!file) {
    setMessage('Выберите ZIP-архив бекапа', 'warn');
    return;
  }
  try {
    const response = await fetch('/api/backups/upload', {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/zip' }),
      credentials: 'same-origin',
      body: file
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || response.statusText);
    setMessage('Бекап загружен и проверен', 'good');
    input.value = '';
    await refreshBackups();
  } catch (error) {
    setMessage(`Ошибка загрузки бекапа: ${error.message}`, 'bad');
  }
}
async function startJob(url, payload, text){
  try {
    setMessage(`${text} запущено`, 'warn');
    const response = await postJson(url, payload || {});
    setMessage(`Задание ${response.job.id} добавлено`, 'good');
    await refresh();
    return response;
  } catch (error) {
    setMessage(error.message, 'bad');
    await refresh();
    return null;
  }
}
async function startSelectedDiscovery(){
  const options = discoveryOptions();
  if (!hasEnabledProtocol(options)) {
    setMessage('Выберите хотя бы один протокол для проверки', 'bad');
    return;
  }
  const mode = selectedRunMode();
  const domains = finderDomains();
  if (!domains.length) {
    setMessage('Добавьте хотя бы один домен для подбора', 'bad');
    return;
  }
  const payload = {
    domains,
    ...options
  };
  const timeout = timeoutSecondsOrNull();
  if (timeout !== null) payload.timeout_seconds = timeout;
  await saveLaunchTimeoutDefaultsNow();
  await saveRunPreferencesNow();
  let response = null;
  if (mode === 'multi') {
    payload.curl_parallelism = curlParallelism();
    response = await startJob('/api/jobs/zapret-multi-domain-discovery', payload, 'Все домены на одной стратегии');
  } else {
    response = await startJob('/api/jobs/zapret-standard-discovery', payload, 'Поиск стратегий');
  }
}
async function stopCurrentJob(){
  try {
    await postJson('/api/jobs/stop-current', {});
    setMessage('Остановка подбора запрошена', 'warn');
    await refresh();
  } catch (error) {
    setMessage(error.message, 'bad');
    await refresh();
  }
}
document.addEventListener('click', (event) => {
  const domainSummary = event.target.closest('details.domain-group[data-domain] > summary');
  if (domainSummary) {
    event.preventDefault();
    const details = domainSummary.parentElement;
    const domain = details.dataset.domain;
    const nextOpen = !Boolean(state.openCandidateDomains[domain]);
    state.openCandidateDomains[domain] = nextOpen;
    const cachedDomain = state.domainStrategies[domain] || {};
    if (nextOpen && (!cachedDomain.loaded || !candidateCacheValid(cachedDomain))) {
      state.domainStrategies[domain] = { candidates: [], total: 0, hasMore: false, loaded: false, loading: true };
      renderCandidates();
      refreshDomainStrategies(domain, true);
    } else {
      renderCandidates();
    }
    return;
  }
  const button = event.target.closest('button');
  if (!button) return;
  const action = button.dataset.action || '';
  const protectedMutation = MUTATING_ACTIONS.has(action) || Boolean(button.dataset.backupRestore) || Boolean(button.dataset.backupDelete);
  if (protectedMutation && !requireNoActiveRun()) return;
  if (button.dataset.commonDomainSuggestion) {
    chooseCommonDomainSuggestion(button.dataset.commonDomainSuggestion);
    return;
  }
  if (button.dataset.runRepeat) {
    repeatRun(button.dataset.runRepeat);
    return;
  }
  if (button.dataset.tab) setActiveTab(button.dataset.tab);
  if (button.dataset.candidateView) {
    setCandidateView(button.dataset.candidateView);
    return;
  }
  if (button.dataset.candidateResultMode) {
    state.candidateResultMode = button.dataset.candidateResultMode;
    renderCandidateResult();
    return;
  }
  if (button.dataset.action === 'open-log') {
    setActiveTab('terminal');
    const raw = document.querySelector('.raw-log-panel');
    if (raw) raw.open = true;
    return;
  }
  if (button.dataset.action === 'open-candidates') {
    setActiveTab('candidates');
    return;
  }
  if (button.dataset.action === 'repeat-last-run') {
    const row = latestRun();
    if (row) repeatRun(runDomainKey(row));
    else setMessage('В истории пока нет запуска для повтора', 'warn');
    return;
  }
  if (button.dataset.action === 'copy-diagnostics') {
    copyDiagnostics();
    return;
  }
  if (button.dataset.action === 'build-candidate-result') {
    buildCandidateResultNow();
    return;
  }
  if (button.dataset.action === 'copy-candidate-result') {
    copyCandidateResult();
    return;
  }
  if (button.dataset.action === 'export-candidate-result') {
    exportCandidateResult();
    return;
  }
  if (button.dataset.action === 'use-candidate-result-domains') {
    useCandidateResultDomains();
    return;
  }
  if (button.dataset.action === 'open-candidate-result') {
    openCandidateResultDetails();
    return;
  }
  if (button.dataset.action === 'refresh') {
    invalidateCandidateCaches();
    refresh();
    if (state.activeTab === 'candidates') {
      if (state.candidateView === 'domain') {
        refreshDomainIndex();
      } else {
        refreshCandidates(true);
      }
    }
  }
  if (button.dataset.action === 'refresh-backups') {
    refreshBackups();
    return;
  }
  if (button.dataset.action === 'create-backup') {
    createBackup();
    return;
  }
  if (button.dataset.action === 'save-settings') {
    saveSettings();
    return;
  }
  if (button.dataset.action === 'check-releases') {
    checkReleases();
    return;
  }
  if (button.dataset.action === 'update-from-release') {
    updateFromRelease();
    return;
  }
  if (button.dataset.action === 'toggle-update-log') {
    toggleUpdateLog();
    return;
  }
  if (button.dataset.action === 'v2fly-load-categories') {
    loadV2flyCategories(true);
    return;
  }
  if (button.dataset.action === 'v2fly-select-category') {
    const category = button.dataset.category || '';
    const input = el('v2fly-category-search');
    if (input) input.value = category;
    state.v2flyPreview = null;
    clearV2flyDomains();
    suggestV2flyPresetName();
    renderV2flyCategoryCatalog();
    renderV2flyPreview();
    return;
  }
  if (button.dataset.action === 'v2fly-preview') {
    previewV2flyPreset();
    return;
  }
  if (button.dataset.action === 'v2fly-import') {
    importV2flyPreset();
    return;
  }
  if (button.dataset.action === 'preset-editor-save') {
    savePresetEditor();
    return;
  }
  if (button.dataset.action === 'preset-editor-delete') {
    deletePresetEditor();
    return;
  }
  if (button.dataset.action === 'preset-editor-export') {
    exportPresetEditor();
    return;
  }
  if (button.dataset.action === 'preset-new-save') {
    savePresetNew();
    return;
  }
  if (button.dataset.backupRestore) {
    restoreBackup(button.dataset.backupRestore);
    return;
  }
  if (button.dataset.backupDelete) {
    deleteBackup(button.dataset.backupDelete);
    return;
  }
  if (button.dataset.action === 'upload-backup') {
    uploadBackup();
    return;
  }
  if (button.dataset.action === 'load-more-candidates') {
    refreshCandidates(false);
    return;
  }
  if (button.dataset.action === 'load-more-candidate-domains') {
    refreshDomainIndex(false);
    return;
  }
  if (button.dataset.action === 'load-more-runs') {
    refreshRuns(false);
    return;
  }
  if (button.dataset.fill) fillDomains(button.dataset.fill);
  if (button.dataset.presetSave) {
    savePreset(button.dataset.presetSave);
    return;
  }
  if (button.dataset.presetDelete) {
    deletePreset(button.dataset.presetDelete);
    return;
  }
  if (button.dataset.action === 'add-common-domain') {
    addCommonDomain();
    return;
  }
  if (button.dataset.strategyListToggle) {
    const key = button.dataset.strategyListToggle;
    const domain = domainFromStrategyListKey(key);
    const common = isCommonStrategyListKey(key);
    const currentlyExpanded = Boolean(state.expandedStrategyLists[key]);
    state.expandedStrategyLists[key] = !currentlyExpanded;
    renderCandidates();
    if (!currentlyExpanded && common && state.candidateHasMore) {
      loadAllCommonStrategies();
      return;
    }
    if (!currentlyExpanded && domain && (state.domainStrategies[domain] || {}).hasMore) {
      loadAllDomainStrategies(domain);
    }
    return;
  }
  if (button.dataset.action === 'run-selected-discovery') startSelectedDiscovery();
  if (button.dataset.action === 'stop-current') stopCurrentJob();
});
document.addEventListener('input', (event) => {
  if (event.target && ['curl-parallelism', 'enable-ipv6'].includes(event.target.id)) {
    state.settingsTouched = true;
  }
  if (event.target && RUN_TIMEOUT_CONTROL_IDS.has(event.target.id)) {
    state.settingsTouched = true;
  }
  if (event.target && String(event.target.id || '').startsWith('settings-')) {
    state.settingsTouched = true;
  }
  if (event.target && event.target.id === 'settings-update-channel') {
    renderReleaseInfo();
  }
  if (event.target && String(event.target.id || '').startsWith('v2fly-')) {
    if (event.target.id === 'v2fly-category-search') {
      clearV2flyDomains();
      suggestV2flyPresetName();
      renderV2flyCategoryCatalog();
    }
    if (event.target.id === 'v2fly-domains') updateEditorLineNumbers('v2fly-domains');
    state.v2flyPreview = null;
    renderV2flyPreview();
  }
  if (event.target && event.target.id === 'preset-editor-domains') {
    updateEditorLineNumbers('preset-editor-domains');
    renderPresetEditorPreview(null);
  }
  if (event.target && event.target.id === 'preset-new-domains') {
    updateEditorLineNumbers('preset-new-domains');
    renderPresetNewPreview(null);
  }
  if (event.target && event.target.id === 'finder-domains') {
    updateEditorLineNumbers('finder-domains');
    state.domainsTouched = true;
    markDomainPresetCustom('finder');
    if (state.candidateView === 'common') scheduleCandidateRefresh();
  }
  if (event.target && event.target.id === 'common-domains') {
    updateEditorLineNumbers('common-domains');
    markDomainPresetCustom('common');
    scheduleCandidateRefresh();
    renderCommonDomainSuggestions();
  }
  if (event.target && DISCOVERY_PROFILE_CONTROL_IDS.has(event.target.id)) {
    markDiscoveryProfileCustom();
  }
  if (event.target && event.target.id === 'common-domain-add') {
    renderCommonDomainSuggestions();
  }
  if (isRunLaunchSummaryControl(event.target)) {
    renderRunLaunchSummary();
  }
});
document.addEventListener('scroll', (event) => {
  if (event.target && event.target.matches && event.target.matches('.strategy-code, .line-numbered-textarea')) {
    const gutter = event.target.previousElementSibling;
    if (gutter) gutter.scrollTop = event.target.scrollTop;
    if (event.target.matches('.strategy-code')) {
      const key = strategyEditorScrollKey(event.target);
      if (key) state.strategyEditorScrolls[key] = event.target.scrollTop;
    }
  }
}, true);
document.addEventListener('change', (event) => {
  if (event.target && ['curl-parallelism', 'enable-ipv6'].includes(event.target.id)) {
    state.settingsTouched = true;
  }
  if (event.target && RUN_TIMEOUT_CONTROL_IDS.has(event.target.id)) {
    state.settingsTouched = true;
  }
  if (event.target && String(event.target.id || '').startsWith('settings-')) {
    state.settingsTouched = true;
  }
  if (event.target && String(event.target.id || '').startsWith('v2fly-')) {
    if (event.target.id === 'v2fly-category-search') {
      clearV2flyDomains();
      suggestV2flyPresetName();
      renderV2flyCategoryCatalog();
    }
    if (event.target.id === 'v2fly-domains') updateEditorLineNumbers('v2fly-domains');
    state.v2flyPreview = null;
    renderV2flyPreview();
  }
  if (event.target && event.target.id === 'limit-time-enabled') {
    syncTimeLimitUi();
    markDiscoveryProfileCustom();
  }
  if (event.target && (event.target.id === 'finder-preset-select' || event.target.id === 'common-preset-select')) {
    const target = event.target.id.startsWith('finder') ? 'finder' : 'common';
    const value = event.target.value || '';
    const nameInput = el(`${target}-preset-name`);
    if (nameInput) nameInput.value = value === CUSTOM_SELECT_VALUE ? 'custom' : (value.startsWith('custom:') ? value.slice('custom:'.length) : '');
    if (value !== CUSTOM_SELECT_VALUE) usePreset(target);
  }
  if (event.target && event.target.id === 'preset-manager-name') {
    state.presetManager.name = event.target.value || '';
    renderPresetManager();
    loadPresetEditorFromSelection({ silent: true });
  }
  if (event.target && event.target.id === 'discovery-profile-select') {
    const profile = (state.discoveryProfiles || {})[event.target.value];
    useDiscoveryProfile(profile);
  }
  if (event.target && event.target.name === 'run-mode') {
    renderRunModeNote();
  }
  if (event.target && DISCOVERY_PROFILE_CONTROL_IDS.has(event.target.id)) {
    markDiscoveryProfileCustom();
  }
  if (isRunLaunchSummaryControl(event.target)) {
    renderRunLaunchSummary();
  }
});
document.addEventListener('keydown', (event) => {
  if (event.target && event.target.id === 'common-domain-add' && event.key === 'Enter') {
    event.preventDefault();
    addCommonDomain();
  }
  if (event.target && event.target.id === 'common-domain-add' && event.key === 'Escape') {
    hideCommonDomainSuggestions();
  }
});
document.addEventListener('focusin', (event) => {
  if (event.target && event.target.id === 'common-domain-add') {
    renderCommonDomainSuggestions();
  }
});
document.addEventListener('focusout', (event) => {
  if (event.target && event.target.id === 'common-domain-add') {
    setTimeout(hideCommonDomainSuggestions, 120);
  }
});
document.addEventListener('toggle', (event) => {
  const details = event.target;
  if (!details || !details.matches) return;
  if (details.matches('details.domain-group[data-common-protocol]')) {
    if (state.openCommonProtocols[details.dataset.commonProtocol] !== details.open) {
      state.openCommonProtocols[details.dataset.commonProtocol] = details.open;
      renderCandidates();
    }
  }
  if (details.matches('details.run-domains[data-run-domains]')) {
    state.openRunDomains[details.dataset.runDomains] = details.open;
  }
}, true);
refresh();
startRealtimeEvents();
startRealtimeFallback();
</script>
</body></html>
"""
    return html.replace(
        "__WEB_AUTH_JSON__",
        json.dumps(_web_auth_public_payload(web_auth, include_token=True), ensure_ascii=False, separators=(",", ":")),
    )


def status_payload(config: AppConfig, web_auth: dict[str, Any] | None = None) -> dict[str, Any]:
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
        "release_update": release_update_status(config.output.state_dir, current_version=__version__),
        "candidate_version": candidate_storage_version(config.output.state_dir),
        "paths": {
            "state_dir": str(config.output.state_dir),
        },
        "zapret2": check_install_cached(),
        "web_auth": _web_auth_public_payload(web_auth, include_token=False),
    }


def _event_payloads(config: AppConfig, web_auth: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    status = status_payload(config, web_auth=web_auth)
    status_event = {
        key: status[key]
        for key in ("version", "state", "settings", "run_preferences", "release_update", "paths", "zapret2", "web_auth")
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
        stdout_from_size=_query_int(query, "stdout_size", -1),
        stdout_log_match=_query_one(query, "stdout_log"),
        stderr_from_size=_query_int(query, "stderr_size", -1),
        stderr_log_match=_query_one(query, "stderr_log"),
    )


DEFAULT_SETTINGS = {
    "curl_parallelism_default": 4,
    "curl_parallelism_max": 10,
    "curl_max_time": 2,
    "curl_max_time_quic": 2,
    "curl_max_time_doh": 2,
    "enable_ipv6": False,
    "debug_stdout": False,
    "update_channel": "stable",
}


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


def read_settings(config: AppConfig) -> dict[str, Any]:
    state = read_state(config.output.state_dir)
    stored = state.get("settings") if isinstance(state.get("settings"), dict) else {}
    return _normalize_settings({**DEFAULT_SETTINGS, **stored})


def save_settings(config: AppConfig, payload: dict[str, Any]) -> dict[str, Any]:
    settings = _normalize_settings({**read_settings(config), **(payload if isinstance(payload, dict) else {})})
    state = read_state(config.output.state_dir)
    state["settings"] = settings
    write_state(config.output.state_dir, state)
    return settings


def read_run_preferences(config: AppConfig) -> dict[str, Any]:
    state = read_state(config.output.state_dir)
    stored = state.get("run_preferences") if isinstance(state.get("run_preferences"), dict) else {}
    return _normalize_run_preferences({**DEFAULT_RUN_PREFERENCES, **stored})


def save_run_preferences(config: AppConfig, payload: dict[str, Any]) -> dict[str, Any]:
    preferences = _normalize_run_preferences(
        {**read_run_preferences(config), **(payload if isinstance(payload, dict) else {})}
    )
    state = read_state(config.output.state_dir)
    state["run_preferences"] = preferences
    write_state(config.output.state_dir, state)
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


def _normalize_settings(raw: dict[str, Any]) -> dict[str, Any]:
    max_parallelism = _minimum_int(raw.get("curl_parallelism_max"), default=10, minimum=1)
    default_parallelism = _bounded_int(raw.get("curl_parallelism_default"), default=4, minimum=1, maximum=max_parallelism)
    channel = str(raw.get("update_channel") or "stable")
    if channel not in {"stable", "prerelease"}:
        channel = "stable"
    return {
        "curl_parallelism_default": default_parallelism,
        "curl_parallelism_max": max_parallelism,
        "curl_max_time": _minimum_int(raw.get("curl_max_time"), default=2, minimum=1),
        "curl_max_time_quic": _minimum_int(raw.get("curl_max_time_quic"), default=2, minimum=1),
        "curl_max_time_doh": _minimum_int(raw.get("curl_max_time_doh"), default=2, minimum=1),
        "enable_ipv6": bool(raw.get("enable_ipv6")),
        "debug_stdout": bool(raw.get("debug_stdout")),
        "update_channel": channel,
        "stable_release_url": "https://github.com/balbomush/GP-access-control-plane/releases/latest",
        "prerelease_url": "https://github.com/balbomush/GP-access-control-plane/releases",
    }


def read_discovery_profiles(config: AppConfig) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for name, profile in DEFAULT_DISCOVERY_PROFILES.items():
        merged[name] = _normalize_discovery_profile(name, profile)
    return dict(sorted(merged.items()))


def save_discovery_profiles(config: AppConfig, payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    state = read_state(config.output.state_dir)
    state["discovery_profiles"] = {}
    write_state(config.output.state_dir, state)
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


def _clear_stale_current_job(config: AppConfig) -> None:
    state = read_state(config.output.state_dir)
    if not state.get("current_job"):
        return
    state["current_job"] = None
    state["current_job_name"] = None
    state["current_job_status"] = None
    write_state(config.output.state_dir, state)


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
    return read_runs_page(
        config.output.state_dir,
        limit=_query_int(query, "limit", 50),
        offset=_query_int(query, "offset", 0),
    )


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


def _release_info_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    settings = read_settings(config)
    channel = _query_str(query, "channel", str(settings.get("update_channel") or "stable"))
    stable = release_channel_info(current_version=__version__, channel="stable")
    prerelease = release_channel_info(current_version=__version__, channel="prerelease")
    selected = prerelease if channel == "prerelease" else stable
    return {"release": selected, "releases": {"stable": stable, "prerelease": prerelease}}


def _release_update_plan_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    settings = read_settings(config)
    channel = _query_str(query, "channel", str(settings.get("update_channel") or "stable"))
    return {"plan": release_update_plan(config.output.state_dir, channel=channel)}


def _queue_release_update_payload(config: AppConfig, payload: dict[str, Any]) -> dict[str, Any]:
    settings = read_settings(config)
    channel = str(payload.get("channel") or settings.get("update_channel") or "stable")
    return {"update": queue_release_update(config.output.state_dir, channel=channel, install_dir=Path.cwd())}


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


def _job_zapret_standard_discovery(config: AppConfig, payload: dict[str, Any], stop_event: Any) -> dict[str, Any]:
    domains = _payload_domains(payload)
    settings = read_settings(config)
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
    )


def _job_zapret_multi_domain_discovery(config: AppConfig, payload: dict[str, Any], stop_event: Any) -> dict[str, Any]:
    domains = _payload_domains(payload)
    settings = read_settings(config)
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
