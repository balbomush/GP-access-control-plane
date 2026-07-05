from __future__ import annotations

import hashlib
import json
import mimetypes
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
from ..domain_sources import builtin_preset_sources, import_v2fly_preset, list_v2fly_categories_cached, preview_v2fly_preset
from ..jobs import JobRunner
from ..release_update import queue_release_update, release_update_plan, release_update_status
from ..releases import release_channel_info
from ..state import now_iso, read_state, write_state
from ..storage import (
    delete_custom_preset,
    read_custom_preset_index,
    read_custom_presets,
    read_preset_domains_page,
    save_custom_preset,
    save_custom_presets,
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
    run_multi_domain_discovery,
    run_standard_discovery,
    stop_active_blockcheck_runtime,
)
from ..zapret2 import check_install_cached


def serve(config: AppConfig, host: str, port: int) -> None:
    _clear_stale_current_job(config)
    close_stale_running_runs(config.output.state_dir)
    runner = JobRunner(config.output.state_dir, on_idle=lambda: create_snapshot_if_idle(config.output.state_dir))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed_url = urlparse(self.path)
            path = parsed_url.path
            query = parse_qs(parsed_url.query)
            if path == "/":
                self._html()
            elif path == "/api/status":
                self._json(status_payload(config))
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
            elif path == "/api/discovery-profiles":
                self._json({"profiles": read_discovery_profiles(config)})
            elif path == "/api/diagnostics":
                self._json(diagnostics_payload(config.output.state_dir))
            elif path == "/api/strategy-finder/domains":
                self._json(domain_sets())
            elif path == "/api/strategy-finder/candidate-domains":
                self._json(_candidate_domain_index_payload(config, query))
            elif path == "/api/strategy-finder/candidates":
                self._json(_candidate_page_payload(config, query))
            elif path == "/api/strategy-finder/runs":
                self._json({"runs": read_runs(config.output.state_dir)})
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
                data = index_html().encode("utf-8")
                self._head(HTTPStatus.OK, "text/html; charset=utf-8", len(data))
            elif path == "/api/events":
                self._head(HTTPStatus.OK, "text/event-stream; charset=utf-8", 0)
            elif path in {
                "/api/status",
                "/api/settings",
                "/api/run-preferences",
                "/api/releases",
                "/api/releases/update-plan",
                "/api/releases/update",
                "/api/discovery-profiles",
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
                "/api/domain-sources",
                "/api/domain-sources/v2fly/categories",
            }:
                self._head(HTTPStatus.OK, "application/json; charset=utf-8", 0)
            else:
                self._head(HTTPStatus.NOT_FOUND, "application/json; charset=utf-8", 0)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
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
                    domains = _payload_string_list(payload, "domains")
                    save_custom_preset(
                        config.output.state_dir,
                        scope=scope,
                        name=name,
                        domains=domains,
                        updated_at=now_iso(),
                    )
                    self._json(
                        {
                            "custom": {scope: {name: domains}, "finder" if scope == "common" else "common": {}},
                            "metadata": read_custom_preset_index(config.output.state_dir),
                        }
                    )
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
                    self._json({"custom": {"finder": {}, "common": {}}, "metadata": metadata})
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
                    self._json(
                        {
                            "domain": result,
                            "custom": {"finder": {}, "common": {}},
                            "metadata": read_custom_preset_index(config.output.state_dir),
                        }
                    )
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
            if path == "/api/discovery-profiles":
                self._json({"profiles": save_discovery_profiles(config, payload.get("profiles") or payload)})
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

        def _html(self) -> None:
            data = index_html().encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

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
                    for event_name, payload in _event_payloads(config).items():
                        fingerprint = _event_fingerprint(payload)
                        if previous.get(event_name) == fingerprint:
                            continue
                        previous[event_name] = fingerprint
                        self._event(event_name, payload)
                    now = time.monotonic()
                    if now - heartbeat_at >= 15:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        heartbeat_at = now
                    time.sleep(1)
                except (BrokenPipeError, ConnectionResetError):
                    return

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
            length = int(self.headers.get("Content-Length") or "0")
            content_type = self.headers.get("Content-Type", "")
            body = self.rfile.read(length)
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
    print(f"GP control plane web UI listening on http://{host}:{port}")
    server.serve_forever()


def index_html() -> str:
    return """<!doctype html>
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
.time-limit-field[hidden] { display: none; }
.preset-panel,
.common-filter-panel {
  display: grid;
  gap: 10px;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 12px;
  background: var(--surface-soft);
}
.common-filter-panel[hidden] { display: none; }
.preset-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr);
  gap: 8px;
}
.finder-control-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}
.finder-control-grid .field-wide {
  grid-column: 1 / -1;
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
.preset-domain-list {
  display: grid;
  gap: 6px;
}
.preset-domain-row {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 8px;
  align-items: center;
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 8px 10px;
  background: var(--surface-code);
  font-family: var(--mono);
  font-size: 13px;
}
.preset-domain-row.disabled {
  opacity: .58;
}
.preset-domain-row input {
  width: 18px;
  min-height: 18px;
}
.preset-domain-name {
  overflow-wrap: anywhere;
}
.helper-text {
  color: var(--text-soft);
  font-size: 12px;
  line-height: 1.4;
}
.candidate-summary {
  color: var(--text-soft);
  font-size: 13px;
  white-space: nowrap;
  text-align: right;
  margin-bottom: 10px;
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
.terminal-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
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
.run-diagnostic-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 8px;
}
.run-diagnostic-chip {
  max-width: 100%;
  background: #0d1726;
  border: 1px solid var(--line);
  border-radius: 999px;
  color: var(--text);
  overflow-wrap: anywhere;
  padding: 4px 9px;
}
.run-diagnostic-chip.warn { border-color: var(--warn); color: var(--warn); }
.run-diagnostic-chip.bad { border-color: var(--danger); color: var(--danger); }
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
  .status-grid, .button-row, .fill-row, .preset-grid, .preset-actions, .domain-picker-row, .backup-downloads, .release-grid, .category-toolbar { grid-template-columns: 1fr; }
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
        <div class="subtitle">Raspberry Pi · blockcheck2 · live-лог</div>
      </div>
      <span class="topbar-version" id="app-version-badge">v-</span>
    </div>
  </header>
  <main class="main">
    <section class="status-grid" aria-label="Сводка">
      <div class="metric">
        <div class="metric-label">zapret2</div>
        <div class="metric-value" id="metric-zapret">Загрузка</div>
        <div class="metric-note" id="metric-zapret-note">-</div>
      </div>
      <button class="metric metric-button" data-tab="terminal" id="metric-job-card" type="button">
        <div class="metric-label">Задание</div>
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
      <button class="tab-button active" data-tab="finder" type="button">Подбор</button>
      <button class="tab-button" data-tab="history" type="button">История</button>
      <button class="tab-button" data-tab="candidates" type="button">Кандидаты</button>
      <button class="tab-button" data-tab="terminal" type="button">Терминал</button>
      <button class="tab-button" data-tab="lists" type="button">Списки и профили</button>
      <button class="tab-button" data-tab="settings" type="button">Настройки</button>
    </nav>

    <section class="tab-page active" data-tab-page="finder">
    <div class="layout finder-layout">
      <div class="stack">
        <section class="panel">
          <div class="panel-header">
            <h2>Запуск поиска</h2>
            <span class="badge" id="job-badge">Свободна</span>
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
              <div class="finder-control-grid">
                <div class="field">
                  <label for="discovery-profile-select">Профиль подбора</label>
                  <select id="discovery-profile-select"></select>
                  <div class="helper-text" id="discovery-profile-note">Глубина поиска blockcheck2: quick, standard или force.</div>
                </div>
                <div class="field">
                  <label for="settings-preset-select">Пресет настроек</label>
                  <select id="settings-preset-select">
                    <option value="cautious">Осторожный</option>
                    <option value="normal" selected>Обычный</option>
                    <option value="accelerated">Ускоренный</option>
                    <option value="custom">изменено</option>
                  </select>
                  <div class="helper-text" id="settings-preset-note">Задает параметры запуска: curl, повторы и DNS/IP checks.</div>
                </div>
              </div>
            </div>
            <div class="preset-panel">
              <div class="field">
                <label>Режим поиска</label>
                <div class="segmented-control" id="run-mode-control">
                  <label class="segment-option tooltip-button" data-tooltip="Запускает штатный blockcheck2: домены проверяются обычным порядком скрипта. Хороший режим для базовой совместимости.">
                    <input type="radio" name="run-mode" value="standard" checked>
                    Домены по очереди
                  </label>
                  <label class="segment-option tooltip-button" data-tooltip="Одна стратегия запускается один раз, затем все выбранные домены проверяются параллельными curl. Удобно быстрее понять, какие домены покрывает одна стратегия.">
                    <input type="radio" name="run-mode" value="multi">
                    Все домены на одной стратегии
                  </label>
                </div>
              </div>
              <div class="helper-text" id="run-mode-note">Обычный режим: штатный blockcheck2 проверяет домены по своему порядку.</div>
              <div class="field multi-curl-field" id="multi-curl-field" hidden>
                <label for="curl-parallelism">Параллельных curl</label>
                <input id="curl-parallelism" type="number" min="1" max="10" step="1" value="4">
                <div class="helper-text">Работает только в режиме `Все домены на одной стратегии`: одна стратегия проверяет несколько доменов параллельно.</div>
              </div>
            </div>
            <label class="checkbox-row">
              <input id="limit-time-enabled" type="checkbox">
              <span>Ограничить время поиска</span>
            </label>
            <div class="field time-limit-field" id="time-limit-field" hidden>
              <label for="finder-timeout-hours">Лимит поиска, часов</label>
              <input id="finder-timeout-hours" type="number" min="0.1" max="24" step="0.5" value="6">
            </div>
            <div class="preset-panel finder-options-panel">
              <div class="helper-text">Основные проверки blockcheck2, которые реально влияют на подбор стратегий.</div>
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
              <input id="scan-level" type="hidden" value="standard">
            </div>
            <details class="preset-panel">
              <summary class="domain-header">
                <span class="domain-title">Дополнительно</span>
                <span class="helper-text">повторы, DNS/IP checks, IPv6</span>
              </summary>
              <div class="preset-grid">
                <div class="field">
                  <label for="repeats">Повторы проверки стратегии</label>
                  <input id="repeats" type="number" min="1" max="10" step="1" value="1">
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
            <div class="button-row run-actions">
              <button class="tooltip-button" data-action="run-selected-discovery" data-tooltip="Запускает выбранный выше режим поиска с текущими доменами, профилем подбора и пресетом настроек." type="button">Запустить выбранный режим</button>
              <button class="secondary danger tooltip-button" data-action="stop-current" data-tooltip="Останавливает текущий подбор и сохраняет уже найденные успешные стратегии." type="button" disabled>Остановить текущий запуск</button>
            </div>
            <div class="message" id="message">Готово</div>
          </div>
        </section>
      </div>
    </div>
    </section>

    <section class="tab-page history-page" data-tab-page="history">
      <section class="panel">
        <div class="panel-header">
          <h2>История запусков</h2>
          <span class="badge" id="finder-runs-count">0</span>
        </div>
        <div id="finder-runs-table"></div>
      </section>
    </section>

    <section class="tab-page candidates-page" data-tab-page="candidates">
      <section class="panel">
        <div class="panel-header">
          <h2>Найденные стратегии</h2>
          <span class="badge" id="candidates-count">0</span>
        </div>
        <div class="candidate-summary" id="candidate-summary">-</div>
        <div class="candidate-tabs" role="tablist" aria-label="Вид кандидатов">
          <button class="subtab-button active" data-candidate-view="domain" type="button">По доменам</button>
          <button class="subtab-button" data-candidate-view="common" type="button">Общие стратегии</button>
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
        </div>
        <div id="candidates-table"></div>
      </section>
    </section>

    <section class="tab-page terminal-page" data-tab-page="terminal">
      <section class="panel terminal-panel">
        <div class="panel-header">
          <h2>Терминал</h2>
          <div class="terminal-actions">
            <span class="badge" id="finder-log-status">-</span>
            <button class="secondary danger" data-action="stop-current" title="Останавливает текущий подбор и сохраняет уже найденные успешные стратегии." disabled>Остановить</button>
          </div>
        </div>
        <div class="progress-panel">
          <div class="progress-bar" aria-label="Прогресс подбора">
            <div class="progress-fill" id="progress-fill"></div>
          </div>
          <div class="progress-grid">
            <div class="progress-cell">
              <div class="progress-label">Проверено попыток</div>
              <div class="progress-value" id="progress-attempted">-</div>
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
          </div>
          <div class="progress-note" id="progress-note">Прогресс оценочный: blockcheck2 не отдает общий счетчик стратегий до старта.</div>
          <div class="progress-note" id="progress-metrics">Метрики появятся после старта подбора.</div>
        </div>
        <pre id="finder-log">Лога пока нет</pre>
      </section>
    </section>

    <section class="tab-page lists-page" data-tab-page="lists">
      <section class="panel">
        <div class="panel-header">
          <h2>Списки и профили</h2>
        </div>
        <div class="settings-stack">
        <div class="preset-panel settings-preset-manager-panel">
          <div class="panel-header">
            <h2>Доменные пресеты</h2>
            <span class="badge" id="preset-manager-count">0</span>
          </div>
          <div class="preset-grid">
            <div class="field">
              <label for="preset-manager-name">Пользовательский список</label>
              <select id="preset-manager-name"></select>
            </div>
          </div>
          <div class="domain-picker-row">
            <input id="preset-manager-query" autocomplete="off" placeholder="Найти домен в списке">
            <button class="secondary" data-action="preset-manager-refresh" type="button">Показать домены</button>
          </div>
          <div class="helper-text" id="preset-manager-note">Выберите пользовательский список, чтобы посмотреть домены. Большие списки загружаются порциями.</div>
          <div id="preset-manager-list" class="preset-domain-list"></div>
          <div class="field">
            <label for="preset-editor-name">Редактор списка</label>
            <input id="preset-editor-name" autocomplete="off" placeholder="Название списка">
          </div>
          <div class="field">
            <div class="code-editor text-editor">
              <pre class="line-numbers" data-line-numbers-for="preset-editor-domains" aria-hidden="true">1</pre>
              <textarea id="preset-editor-domains" class="line-numbered-textarea" autocomplete="off" spellcheck="false" placeholder="youtube.com&#10;discord.com"></textarea>
            </div>
          </div>
          <div class="button-row">
            <button class="secondary" data-action="preset-editor-load" type="button">Загрузить выбранный список</button>
            <button class="secondary" data-action="preset-editor-preview" type="button">Показать изменения</button>
            <button data-action="preset-editor-save" type="button">Сохранить список</button>
            <button class="secondary" data-action="preset-editor-export" type="button">Скачать TXT</button>
          </div>
          <div class="source-preview" id="preset-editor-preview">Изменения еще не проверялись.</div>
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
              <button class="secondary" data-action="v2fly-load-categories" type="button" title="Скачивает свежий каталог групп v2fly только если в источнике появился новый commit.">Обновить каталог</button>
            </div>
          </div>
          <div class="v2fly-catalog-status" id="v2fly-category-status">Каталог групп загрузится автоматически при открытии вкладки.</div>
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

    <section class="tab-page settings-page" data-tab-page="settings">
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
            <div class="setting-note">Включает расширенную запись stdout blockcheck2 в debug-файл. Обычный терминал остается компактным; debug нужен только для диагностики и может увеличить запись на диск.</div>
            <div class="field">
              <label for="settings-default-settings-preset">Пресет настроек по умолчанию</label>
              <select id="settings-default-settings-preset">
                <option value="cautious">Осторожный</option>
                <option value="normal" selected>Обычный</option>
                <option value="accelerated">Ускоренный</option>
              </select>
              <div class="setting-note">Выставляет стартовые параметры на вкладке `Подбор`; вручную их можно менять перед запуском.</div>
            </div>
            <div class="field">
              <label for="settings-curl-max">Максимум параллельных curl</label>
              <input id="settings-curl-max" type="number" min="1" max="10" value="10">
              <div class="setting-note">Верхняя граница, выше которой UI не даст запустить параллельные curl.</div>
            </div>
            <div class="field">
              <label for="settings-curl-max-time">Таймаут HTTP/TLS, сек</label>
              <input id="settings-curl-max-time" type="number" min="1" step="1" value="2">
              <div class="setting-note">Дефолт `CURL_MAX_TIME` для новых запусков.</div>
            </div>
            <div class="field">
              <label for="settings-curl-max-time-quic">Таймаут QUIC, сек</label>
              <input id="settings-curl-max-time-quic" type="number" min="1" step="1" value="2">
              <div class="setting-note">Дефолт `CURL_MAX_TIME_QUIC` для новых запусков.</div>
            </div>
            <div class="field">
              <label for="settings-curl-max-time-doh">Таймаут DoH, сек</label>
              <input id="settings-curl-max-time-doh" type="number" min="1" step="1" value="2">
              <div class="setting-note">Дефолт `CURL_MAX_TIME_DOH` для новых запусков.</div>
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
            <h2>Бекапы</h2>
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
        </div>
      </section>
    </section>
  </main>
  <div class="toast" id="toast" role="status" aria-live="polite" hidden></div>
</div>
<script>
const CUSTOM_PRESETS_KEY = 'gp-control-plane-domain-presets-v1';
const STRATEGY_LIST_LIMIT = 200;
const CANDIDATE_PAGE_LIMIT = 200;
const CUSTOM_SELECT_VALUE = 'custom';
const state = { status: null, settings: null, settingsTouched: false, runPreferences: null, runPreferencesApplied: false, runPreferencesTimer: null, savingRunPreferences: false, releaseInfo: null, releaseStable: null, releasePrerelease: null, releaseUpdate: null, releaseChecked: false, releaseChecking: false, loadingDiscoveryProfile: false, loadingSettingsPreset: false, loadingDomainPreset: false, loadingRunPreferences: false, discoveryProfiles: {}, candidates: [], candidateTotal: 0, candidateOffset: 0, candidateHasMore: false, candidateVersion: null, candidateKnownVersion: null, candidateQueryKey: '', commonCandidateCache: {}, commonLoadingAll: false, candidateDomains: [], candidateDomainTotal: 0, candidateDomainStrategyTotal: 0, candidateDomainsLoaded: false, testedDomains: [], candidatesLoaded: false, domainStrategies: {}, finderRuns: [], finderLog: null, domainSets: null, domainSources: null, v2flyPreview: null, v2flyCategories: null, v2flyCategorySource: '', backups: [], backupsLoaded: false, activeTab: 'finder', candidateView: 'domain', customPresets: loadCustomPresets(), customPresetMeta: { finder: {}, common: {} }, presetManager: { scope: 'finder', name: '', query: '', domains: [], total: 0, hasMore: false, loading: false, loaded: false }, openCandidateDomains: {}, openCommonProtocols: {}, openRunDomains: {}, expandedStrategyLists: {}, strategyEditorScrolls: {}, domainsInitialized: false, domainsTouched: false, formMessage: 'Готово', formMessageTone: '' };
const jobNames = {
  'zapret-standard-discovery': 'Поиск стратегий',
  'zapret-multi-domain-discovery': 'Все домены на одной стратегии',
  'standard-discovery': 'Поиск стратегий',
  'multi-domain-discovery': 'Все домены на одной стратегии'
};
const SETTINGS_PRESETS = {
  cautious: {
    title: 'Осторожный',
    note: 'Меньше параллельных curl, DNS/IP checks не пропускаются. Медленнее, но аккуратнее для диагностики.',
    curl_parallelism: 2,
    repeats: 1,
    repeat_parallel: false,
    skip_dnscheck: false,
    skip_ipblock: false
  },
  normal: {
    title: 'Обычный',
    note: 'Баланс скорости и проверки: curl 4, DNS/IP checks пропускаются, как в текущем рабочем сценарии.',
    curl_parallelism: 4,
    repeats: 1,
    repeat_parallel: false,
    skip_dnscheck: true,
    skip_ipblock: true
  },
  accelerated: {
    title: 'Ускоренный',
    note: 'Больше параллельных curl. Сильнее всего ускоряет режим “Все домены на одной стратегии”.',
    curl_parallelism: 10,
    repeats: 1,
    repeat_parallel: false,
    skip_dnscheck: true,
    skip_ipblock: true
  }
};
const statusTone = { success: 'good', failed: 'bad', running: 'warn', queued: 'warn', stopping: 'warn', stopped: 'warn', timeout: 'warn' };
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
  const response = await fetch(url);
  if (!response.ok) throw new Error(await response.text());
  return await response.json();
}
async function postJson(url, payload){
  const response = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload || {})
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
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
function syncActiveTabUi(){
  document.querySelectorAll('.tab-button[data-tab]').forEach((button) => {
    const active = button.dataset.tab === state.activeTab;
    button.classList.toggle('active', active);
  });
  document.querySelectorAll('[data-tab-page]').forEach((page) => {
    page.classList.toggle('active', page.dataset.tabPage === state.activeTab);
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
  if (tabName === 'lists' && !state.v2flyCategories) loadV2flyCategories();
  if (tabName === 'settings') {
    if (!state.releaseChecked && !state.releaseChecking) checkReleases({ silent: true });
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
  if (!raw) return defaultDomains('critical');
  return parseDomains(raw);
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
function discoveryOptions(){
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
    skip_ipblock: el('skip-ipblock').checked
  };
}
function collectRunPreferences(){
  const timeoutHours = Number(el('finder-timeout-hours')?.value || 6);
  return {
    domains: selectedFinderDomains(),
    domain_preset: el('finder-preset-select')?.value || CUSTOM_SELECT_VALUE,
    discovery_profile: el('discovery-profile-select')?.value || CUSTOM_SELECT_VALUE,
    settings_preset: el('settings-preset-select')?.value || CUSTOM_SELECT_VALUE,
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
    const presetValue = String(prefs.domain_preset || 'builtin:critical');
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
      discoverySelect.value = [...discoverySelect.options].some((option) => option.value === value) ? value : CUSTOM_SELECT_VALUE;
    }
    const settingsSelect = el('settings-preset-select');
    if (settingsSelect) {
      const value = String(prefs.settings_preset || 'normal');
      settingsSelect.value = [...settingsSelect.options].some((option) => option.value === value) ? value : CUSTOM_SELECT_VALUE;
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
    el('time-limit-field').hidden = !el('limit-time-enabled').checked;
    renderDiscoveryProfileNote();
    renderSettingsPresetNote();
  } finally {
    state.loadingRunPreferences = false;
    state.runPreferencesApplied = true;
  }
}
function scheduleRunPreferencesSave(){
  if (!state.runPreferencesApplied || state.loadingRunPreferences) return;
  if (state.runPreferencesTimer) clearTimeout(state.runPreferencesTimer);
  state.runPreferencesTimer = setTimeout(() => {
    saveRunPreferencesNow();
  }, 350);
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
function isRunPreferenceControl(target){
  if (!target) return false;
  if (target.name === 'run-mode') return true;
  return [
    'finder-domains',
    'finder-preset-select',
    'discovery-profile-select',
    'settings-preset-select',
    'curl-parallelism',
    'enable-http',
    'enable-tls12',
    'enable-tls13',
    'include-quic',
    'enable-ipv6',
    'scan-level',
    'repeats',
    'repeat-parallel',
    'skip-dnscheck',
    'skip-ipblock',
    'limit-time-enabled',
    'finder-timeout-hours'
  ].includes(target.id);
}
const DISCOVERY_PROFILE_CONTROL_IDS = new Set(['scan-level']);
const SETTINGS_PRESET_CONTROL_IDS = new Set([
  'curl-parallelism',
  'repeats',
  'repeat-parallel',
  'skip-dnscheck',
  'skip-ipblock',
  'limit-time-enabled',
  'finder-timeout-hours'
]);
function markDiscoveryProfileCustom(){
  if (state.loadingDiscoveryProfile) return;
  const select = el('discovery-profile-select');
  if (select && select.value !== CUSTOM_SELECT_VALUE) select.value = CUSTOM_SELECT_VALUE;
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
  const profile = select && select.value !== CUSTOM_SELECT_VALUE ? (state.discoveryProfiles || {})[select.value] : null;
  const scanLevel = String(profile?.scan_level || el('scan-level')?.value || 'standard');
  const title = profileTitle(scanLevel, profile);
  const details = {
    quick: 'меньше комбинаций, быстрее первичная проверка.',
    standard: 'основной режим для обычного подбора.',
    force: 'больше комбинаций, работает дольше.'
  }[scanLevel] || 'настройки изменены вручную.';
  note.textContent = profile ? `${title}: ${details}` : `Custom: ${details}`;
}
function selectedSettingsPreset(){
  return el('settings-preset-select')?.value || 'normal';
}
function settingPresetTitle(value){
  if (value === CUSTOM_SELECT_VALUE) return 'изменено';
  return (SETTINGS_PRESETS[value] || SETTINGS_PRESETS.normal).title;
}
function markSettingsPresetCustom(){
  if (state.loadingSettingsPreset) return;
  const select = el('settings-preset-select');
  if (select && select.value !== CUSTOM_SELECT_VALUE) select.value = CUSTOM_SELECT_VALUE;
  state.settingsTouched = true;
  renderSettingsPresetNote();
}
function setSettingsPreset(value, options){
  const presetKey = SETTINGS_PRESETS[value] ? value : 'normal';
  const preset = SETTINGS_PRESETS[presetKey];
  const opts = options || {};
  const max = Number((state.settings || {}).curl_parallelism_max || 10);
  state.loadingSettingsPreset = true;
  try {
    const select = el('settings-preset-select');
    if (select) select.value = presetKey;
    const curl = el('curl-parallelism');
    if (curl) {
      curl.max = String(max);
      curl.value = String(Math.max(1, Math.min(max, Number(preset.curl_parallelism || 4))));
    }
    el('repeats').value = String(preset.repeats || 1);
    el('repeat-parallel').checked = Boolean(preset.repeat_parallel);
    el('skip-dnscheck').checked = Boolean(preset.skip_dnscheck);
    el('skip-ipblock').checked = Boolean(preset.skip_ipblock);
    renderSettingsPresetNote();
    if (!opts.fromSettings) state.settingsTouched = true;
  } finally {
    state.loadingSettingsPreset = false;
  }
}
function renderSettingsPresetNote(){
  const preset = SETTINGS_PRESETS[selectedSettingsPreset()];
  const presetNote = el('settings-preset-note');
  if (presetNote) presetNote.textContent = preset ? preset.note : 'Настройки изменены вручную.';
  const note = el('run-mode-note');
  if (!note) return;
  const mode = selectedRunMode();
  const curlField = el('multi-curl-field');
  if (curlField) curlField.hidden = mode !== 'multi';
  const modeText = mode === 'multi'
    ? 'Режим “Все домены на одной стратегии”: одна стратегия запускается один раз, затем домены проверяются параллельно.'
    : 'Обычный режим: штатный blockcheck2 проверяет домены по своему порядку.';
  const presetText = preset ? ` Пресет настроек: ${preset.note}` : ' Настройки изменены вручную.';
  note.textContent = modeText + presetText;
}
function selectedRunMode(){
  return document.querySelector('input[name="run-mode"]:checked')?.value || 'standard';
}
function renderRunModeNote(){
  renderSettingsPresetNote();
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
  select.innerHTML = `<option value="${CUSTOM_SELECT_VALUE}">Custom</option>` + names.map((name) => `<option value="${esc(name)}">${esc(profileTitle(name, profiles[name]))}</option>`).join('');
  if (current && profiles[current]) select.value = current;
  else if (!current && profiles.standard) select.value = 'standard';
  else if (current === CUSTOM_SELECT_VALUE) select.value = CUSTOM_SELECT_VALUE;
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
    headers: {'Content-Type': 'application/json'},
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
function normalizeCustomPresetMeta(metadata, presets){
  const result = { finder: {}, common: {} };
  for (const scope of ['finder', 'common']) {
    const remote = metadata && typeof metadata[scope] === 'object' ? metadata[scope] : {};
    Object.entries(remote).forEach(([name, meta]) => {
      result[scope][name] = {
        name,
        label: (meta && meta.label) || name,
        enabled_count: Number((meta && meta.enabled_count) || 0),
        total_count: Number((meta && meta.total_count) || 0),
        updated_at: (meta && meta.updated_at) || ''
      };
    });
    Object.entries((presets && presets[scope]) || {}).forEach(([name, domains]) => {
      if (!result[scope][name]) {
        const count = uniqueDomainCount(domains);
        result[scope][name] = { name, label: name, enabled_count: count, total_count: count, updated_at: '' };
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
  ])].sort((a, b) => a.localeCompare(b));
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
function mergePresetResponse(data){
  mergeCustomPresets((data || {}).custom || {}, (data || {}).metadata || {});
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
    groups.push({
      label: 'Протестированные',
      presets: [{ key: 'tested', label: 'Все протестированные', domains: testedDomains() }]
    });
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
function renderPresetSelect(target){
  const select = el(`${target}-preset-select`);
  if (!select) return;
  const previous = select.value;
  const customEntries = customPresetNames(target);
  const customGroup = customEntries.length
    ? `<optgroup label="Персональные">${customEntries.map((name) => `<option value="custom:${esc(name)}">${esc(name)} (${customPresetCount(target, name)})</option>`).join('')}</optgroup>`
    : '';
  const builtInGroups = presetGroups(target).map((group) => {
    const options = group.presets.map((preset) => `<option value="builtin:${esc(preset.key)}">${esc(preset.label)} (${uniqueDomainCount(preset.domains)})</option>`).join('');
    return `<optgroup label="${esc(group.label)}">${options}</optgroup>`;
  }).join('');
  select.innerHTML = `<option value="${CUSTOM_SELECT_VALUE}">Custom</option>${customGroup}${builtInGroups}`;
  if ([...select.options].some((option) => option.value === previous)) select.value = previous;
  else if (!previous && target === 'common' && [...select.options].some((option) => option.value === 'builtin:tested')) select.value = 'builtin:tested';
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
}
async function fetchAllPresetDomains(target, name){
  const sourceScope = customPresetSourceScope(target, name);
  const cached = (state.customPresets[sourceScope] || {})[name] || [];
  const expected = customPresetCount(sourceScope, name);
  if (expected > 0 && cached.length && cached.length >= expected) return uniqueDomains(cached);
  let offset = 0;
  let hasMore = true;
  let domains = [];
  let guard = 0;
  while (hasMore && guard < 1000) {
    const params = new URLSearchParams();
    params.set('scope', sourceScope);
    params.set('name', name);
    params.set('kind', 'user');
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
  state.customPresets[sourceScope][name] = uniqueDomains(domains);
  localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify(state.customPresets));
  return state.customPresets[sourceScope][name];
}
async function usePreset(target){
  const selected = el(`${target}-preset-select`).value;
  let domains = presetDomains(target, selected);
  if (selected.startsWith('custom:')) {
    const name = selected.slice('custom:'.length);
    setMessage('Загружается пользовательский список доменов', 'warn');
    try {
      domains = await fetchAllPresetDomains(target, name);
    } catch (error) {
      setMessage(`Ошибка загрузки пользовательского списка: ${error.message}`, 'bad');
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
      prepareCommonCandidateState();
      renderCandidatesOnly();
      if (selectedCommonDomains().length >= 2) refreshCandidates(true);
    }
    else renderCandidates();
    if (target === 'finder') scheduleRunPreferencesSave();
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
    if (target === 'common') refreshCandidates(true);
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
    const data = await postJson('/api/presets/delete', { scope: target, name });
    delete state.customPresets[target][name];
    mergePresetResponse(data);
    localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify(state.customPresets));
    renderPresetSelect(target);
    renderPresetManager();
    showToast('Пресет удален', 'good');
    if (target === 'common') refreshCandidates(true);
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
        {label: 'nfqws2', ok: Boolean(zapret.nfqws2_found), message: zapret.nfqws2_found ? 'найден' : 'не найден'},
        {label: 'blockcheck2', ok: Boolean(zapret.blockcheck_found), message: zapret.blockcheck_found ? 'найден' : 'не найден'},
        {label: 'root-helper', ok: Boolean(zapret.root_helper_ready), message: zapret.root_helper_ready ? 'готов' : (zapret.root_helper_error || 'не готов')}
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
function testedDomainCount(){
  const domains = new Set(Array.isArray(state.testedDomains) ? state.testedDomains : []);
  (state.candidateDomains || []).forEach((item) => {
    if (item && item.domain) domains.add(String(item.domain));
  });
  return Math.max(Number(state.candidateDomainTotal || 0), domains.size);
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
  const progress = (state.finderLog && state.finderLog.progress) || {};
  const phase = progress.phase_label || phaseLabel(progress.phase || '');
  const version = (state.status || {}).version || '-';
  setText('app-version-badge', `v${version}`);
  const zapretValue = el('metric-zapret');
  if (zapretValue) {
    zapretValue.innerHTML = `<span class="compact-status ${ready ? 'ok' : 'bad'}"><span class="compact-status-mark">${ready ? '✓' : '!'}</span><span>${zapretCompact.ok}/${zapretCompact.total || 5}</span></span>`;
    zapretValue.title = zapretCompact.tooltip;
  }
  const zapretNote = el('metric-zapret-note');
  if (zapretNote) {
    zapretNote.textContent = ready ? 'готово' : 'есть проблема';
    zapretNote.title = zapretCompact.tooltip;
  }
  setText('metric-job', busy ? runStatusLabel(jobStatus) : 'Свободна');
  const jobCard = el('metric-job-card');
  if (jobCard) jobCard.className = jobStatusClass(jobStatus, busy);
  const jobDetails = [];
  if (busy) {
    jobDetails.push(phase ? `этап: ${phase}` : (jobNames[board.current_job_name] || board.current_job_name || 'идет поиск'));
  } else {
    jobDetails.push(`обновлено ${new Date().toLocaleTimeString('ru-RU')}`);
  }
  if (state.formMessage) jobDetails.push(state.formMessage);
  setText('metric-job-note', jobDetails.join(' · '));
  const testedCount = testedDomainCount();
  setText('metric-candidates', String(testedCount));
  setText('metric-candidates-note', state.candidateDomainsLoaded ? `загружено ${state.candidateDomains.length} доменов` : 'открыть список');
  const jobBadge = el('job-badge');
  jobBadge.textContent = busy ? 'В работе' : 'Свободна';
  jobBadge.className = busy ? 'badge warn' : 'badge good';
  document.querySelectorAll('button[data-action="run-selected-discovery"]').forEach((button) => {
    button.disabled = busy;
  });
  document.querySelectorAll('button[data-action="update-from-release"]').forEach((button) => {
    button.disabled = busy;
    button.dataset.tooltip = busy
      ? 'Обновление можно запускать только когда подбор стратегий остановлен.'
      : 'Устанавливает выбранный канал обновления только если подбор не запущен. Перед обновлением создается бекап.';
  });
  document.querySelectorAll('button[data-action="stop-current"]').forEach((button) => {
    button.disabled = !busy;
  });
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
  });
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
  }).join('')}</div>`;
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
          <div>${badge('COMMON', 'good')} ${domains.length ? esc(domains.join(', ')) : 'домены из запуска blockcheck2'}</div>
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
  }).join('')}</div>`;
}
function candidatePager(){
  if (!state.candidateHasMore) return '';
  return `<div class="button-row"><button class="secondary" data-action="load-more-candidates" type="button">Показать еще ${CANDIDATE_PAGE_LIMIT}</button></div>`;
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
  state.candidateDomainsLoaded = false;
  state.domainStrategies = {};
  state.commonCandidateCache = {};
  state.testedDomains = [];
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
function uniqueStrategyArgs(rows){
  const seen = new Set();
  const result = [];
  rows.forEach((row) => {
    const raw = String(row.args || '').trim();
    const normalized = normalizeStrategyArg(raw);
    if (!normalized || seen.has(normalized)) return;
    seen.add(normalized);
    result.push(raw);
  });
  return result;
}
function strategyListState(key, rows){
  const all = uniqueStrategyArgs(rows);
  const expanded = Boolean(state.expandedStrategyLists[key]);
  const visible = expanded ? all : all.slice(0, STRATEGY_LIST_LIMIT);
  return { all, visible, expanded, hidden: Math.max(0, all.length - visible.length) };
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
  const lines = list.visible;
  const lineCount = Math.max(lines.length, 1);
  const rowsAttr = Math.min(Math.max(lineCount, 6), 18);
  const remoteMore = Boolean(opts.hasRemoteMore);
  const loadedTotal = Number(opts.loadedTotal || list.all.length);
  const remoteTotal = Number(opts.remoteTotal || loadedTotal);
  const remoteText = remoteMore ? ` Загружено ${loadedTotal}${remoteTotal ? ` из ${remoteTotal}` : ''}; оставшиеся догружаются по кнопке.` : '';
  const meta = `Показано ${lines.length} из ${list.all.length} уникальных стратегий. Дубликаты строк скрыты.${list.hidden ? ` Скрыто до раскрытия: ${list.hidden}.` : ''}${remoteText}`;
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
    <div class="code-editor">
      <pre class="line-numbers" aria-hidden="true">${esc(lineNumbers(lineCount))}</pre>
      <textarea class="strategy-code" data-strategy-code-key="${esc(key)}" readonly spellcheck="false" rows="${rowsAttr}">${esc(lines.join('\\n'))}</textarea>
    </div>
  </div>`;
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
  setText('finder-runs-count', String(rows.length));
  const visible = rows.slice().reverse().slice(0, 12);
  if (!visible.length) {
    el('finder-runs-table').innerHTML = '<div class="empty">Запусков поиска пока не было</div>';
    return;
  }
  el('finder-runs-table').innerHTML = `<div class="run-history">${visible.map(renderRunCard).join('')}</div>`;
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
    success: 'готово',
    failed: 'ошибка',
    running: 'идет',
    queued: 'очередь',
    stopping: 'останавливается',
    stopped: 'остановлено',
    timeout: 'таймаут'
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
function runDiagnosticsSummary(row){
  const skipped = Number(row.domain_skipped_count || 0);
  const dominant = row.dominant_failure || {};
  if (dominant.label) return `${dominant.label}: ${dominant.count || 0}`;
  if (skipped) return `пропущено строк: ${skipped}`;
  const diagnostics = Array.isArray(row.domain_diagnostics) ? row.domain_diagnostics : [];
  if (diagnostics.length) return diagnostics.map((item) => item.label || item.status).filter(Boolean).slice(0, 2).join(', ');
  return '-';
}
function runDiagnostics(row){
  const skipped = Array.isArray(row.domain_skipped) ? row.domain_skipped : [];
  const diagnostics = Array.isArray(row.domain_diagnostics) ? row.domain_diagnostics : [];
  const curlSummary = row.curl_diagnostics_summary || {};
  if (!skipped.length && !diagnostics.length && !Object.keys(curlSummary).length) return '';
  const skippedItems = skipped.slice(0, 20).map((item) => diagnosticChip(`${item.raw || '-'}: ${item.label || item.status || '-'}`, 'bad')).join('');
  const domainItems = diagnostics.slice(0, 30).map((item) => {
    const codes = item.codes && Object.keys(item.codes).length ? `, curl ${Object.keys(item.codes).join('/')}` : '';
    const tone = ['dns_error', 'invalid_domain', 'tls_sni_problem'].includes(item.status) ? 'bad' : 'warn';
    return diagnosticChip(`${item.domain || '-'}: ${item.label || item.status || '-'}${codes}`, tone);
  }).join('');
  const codeItems = Object.entries(curlSummary).map(([code, count]) => diagnosticChip(`curl code=${code}: ${count}`, 'warn')).join('');
  return `<details class="run-diagnostics">
    <summary>Диагностика доменов</summary>
    <div class="run-diagnostic-list">${skippedItems}${domainItems}${codeItems}</div>
    <div class="run-diagnostic-note">Коды curl показывают причину провала проверки: DNS, timeout, TLS/SNI, QUIC/connect или некорректную строку.</div>
  </details>`;
}
function diagnosticChip(text, tone){
  return `<span class="run-diagnostic-chip ${esc(tone || '')}">${esc(text)}</span>`;
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
    truthyOption(options.skip_ipblock, row.skip_ipblock) ? 'без IP-check' : 'с IP-check',
  ].join(', ');
  const ipv6 = truthyOption(options.enable_ipv6, row.enable_ipv6) ? ', IPv6' : '';
  const debugLog = truthyOption(row.debug_stdout, false) ? ', debug-log' : '';
  const curl = row.kind === 'multi-domain-discovery' ? `, curl ${row.curl_parallelism || 4}` : '';
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
  };
  if (row.timeout_seconds) payload.timeout_seconds = Number(row.timeout_seconds);
  if (row.kind === 'multi-domain-discovery') payload.curl_parallelism = Number(row.curl_parallelism || 4);
  return payload;
}
function repeatRun(runKey){
  const row = state.finderRuns.find((item) => runDomainKey(item) === runKey || String(item.id || '') === runKey);
  if (!row) {
    setMessage('Запуск не найден в истории', 'bad');
    return;
  }
  const payload = runPayload(row);
  const multi = row.kind === 'multi-domain-discovery';
  startJob(
    multi ? '/api/jobs/zapret-multi-domain-discovery' : '/api/jobs/zapret-standard-discovery',
    payload,
    multi ? 'Повтор стратегии -> домены' : 'Повтор обычного поиска'
  );
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
  renderProgress(log.progress || {});
  renderRuntimeMetrics(log.metrics || {});
  if (state.activeTab === 'terminal') scrollLogToBottom();
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
}
function backupCard(item){
  const id = String(item.id || '');
  const files = Array.isArray(item.files) ? item.files : [];
  const visibleFiles = files.filter((file) => !String(file.path || '').endsWith('checksums.sha256') && String(file.path || '') !== 'manifest.yaml');
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
  return `/api/backups/download?snapshot=${encodeURIComponent(snapshot)}&file=${encodeURIComponent(file)}`;
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
  const attempted = Number(progress.attempted ?? 0);
  const attemptTotal = Number(progress.attempt_total ?? 0);
  const effectiveTotal = Number(progress.effective_attempt_total || attemptTotal || 0);
  setText('progress-attempted', effectiveTotal ? `${attempted} / ${effectiveTotal}` : String(progress.attempted ?? 0));
  setText('progress-successful', String(progress.successful ?? 0));
  setText('progress-phase', progress.phase_label || phaseLabel(progress.phase || ''));
  if (progress.script_total) {
    const completedFiles = Math.max(0, Math.min(Number(progress.script_total || 0), Number(progress.script_index || 0) - 1));
    const scriptParts = [`Файл ${progress.script_index || 0} из ${progress.script_total}`, `завершено: ${completedFiles}`];
    if (progress.current_script_attempt_total) {
      scriptParts.push(`попыток в файле: ${progress.current_script_attempted || 0} из ${progress.current_script_attempt_total}`);
    }
    setText('progress-scripts', scriptParts.join(', '));
  } else {
    setText('progress-scripts', '-');
  }
  setText('progress-eta', progress.eta_seconds == null ? etaStatusText(progress.eta_status) : formatDuration(Number(progress.eta_seconds)));
  const current = progress.current_script ? `Текущий файл: ${progress.current_script}. ` : '';
  const total = attemptTotal ? `Всего попыток рассчитано по файлам zapret2: ${attemptTotal}. ` : '';
  const under = progress.progress_status === 'underestimated' ? 'План попыток оказался меньше фактического вывода blockcheck2, время уточняется по live-данным. ' : '';
  const parallelism = Number(progress.eta_parallelism || 1);
  const parallelText = parallelism > 1 ? `, параллельных curl: ${parallelism}` : '';
  const etaMs = progress.eta_status === 'sample' ? progress.eta_ms_per_attempt : progress.eta_estimate_ms_per_attempt;
  const etaMode = `Режим ETA: ${etaModeLabel(progress)}. `;
  const eta = etaMs ? `Время считается как оставшиеся попытки × ${etaMs} мс${parallelText}. ` : '';
  const fallback = Number(progress.summary_fallbacks || 0) > 0 ? `Fallback из summary: ${progress.summary_fallbacks}. ` : '';
  setText('progress-note', `${current}${total}${under}${etaMode}${eta}${fallback}Прогресс считается по live-логу blockcheck2.`);
}
function etaModeLabel(progress){
  const status = String(progress.eta_status || '');
  const progressStatus = String(progress.progress_status || '');
  if (status === 'sample') return 'по live-скорости';
  if (status === 'calculating') return 'сбор выборки';
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
function renderRuntimeMetrics(metrics){
  const target = el('progress-metrics');
  if (!target) return;
  if (!metrics || !Object.keys(metrics).length) {
    target.textContent = 'Метрики появятся после старта подбора.';
    return;
  }
  const processes = metrics.processes || {};
  const system = metrics.system || {};
  const cpu = system.cpu_percent || {};
  const memory = system.memory || {};
  const files = metrics.files || {};
  const memFree = memory.MemAvailable ? `RAM свободно: ${Math.round(Number(memory.MemAvailable) / 1024)} МБ` : '';
  const cpuText = cpu.busy == null ? '' : `CPU: ${cpu.busy}%`;
  const ioText = cpu.iowait == null ? '' : `iowait: ${cpu.iowait}%`;
  const procText = `curl: ${processes.curl || 0}, nfqws2: ${processes.nfqws2 || 0}, blockcheck2: ${processes.blockcheck2 || 0}`;
  const walText = files.sqlite_wal ? `SQLite WAL: ${formatBytes(files.sqlite_wal)}` : '';
  target.textContent = [procText, cpuText, ioText, memFree, walText].filter(Boolean).join(' · ');
}
function renderSettings(){
  const settings = state.settings || {};
  const ipv6 = el('settings-enable-ipv6');
  const debugStdout = el('settings-debug-stdout');
  const defaultSettingsPreset = el('settings-default-settings-preset');
  const curlMax = el('settings-curl-max');
  const curlMaxTime = el('settings-curl-max-time');
  const curlMaxTimeQuic = el('settings-curl-max-time-quic');
  const curlMaxTimeDoh = el('settings-curl-max-time-doh');
  const channel = el('settings-update-channel');
  if (ipv6) ipv6.checked = Boolean(settings.enable_ipv6);
  if (debugStdout) debugStdout.checked = Boolean(settings.debug_stdout);
  if (defaultSettingsPreset) defaultSettingsPreset.value = SETTINGS_PRESETS[settings.settings_preset_default] ? settings.settings_preset_default : 'normal';
  if (curlMax) curlMax.value = String(settings.curl_parallelism_max || 10);
  if (curlMaxTime) curlMaxTime.value = String(settings.curl_max_time || 2);
  if (curlMaxTimeQuic) curlMaxTimeQuic.value = String(settings.curl_max_time_quic || 2);
  if (curlMaxTimeDoh) curlMaxTimeDoh.value = String(settings.curl_max_time_doh || 2);
  if (channel) channel.value = settings.update_channel || 'stable';
  renderReleaseInfo();
  if (!state.settingsTouched && !state.runPreferencesApplied) {
    const curlInput = el('curl-parallelism');
    if (curlInput) curlInput.max = String(settings.curl_parallelism_max || 10);
    setSettingsPreset(settings.settings_preset_default || 'normal', { fromSettings: true });
    const finderIpv6 = el('enable-ipv6');
    if (finderIpv6) finderIpv6.checked = Boolean(settings.enable_ipv6);
  } else {
    renderSettingsPresetNote();
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
  return {
    enable_ipv6: Boolean(el('settings-enable-ipv6')?.checked),
    debug_stdout: Boolean(el('settings-debug-stdout')?.checked),
    settings_preset_default: el('settings-default-settings-preset')?.value || 'normal',
    curl_parallelism_max: Number(el('settings-curl-max')?.value || 10),
    curl_max_time: Number(el('settings-curl-max-time')?.value || 2),
    curl_max_time_quic: Number(el('settings-curl-max-time-quic')?.value || 2),
    curl_max_time_doh: Number(el('settings-curl-max-time-doh')?.value || 2),
    update_channel: el('settings-update-channel')?.value || 'stable'
  };
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
function v2flyCategories(){
  const category = String(el('v2fly-category-search')?.value || '').trim().toLowerCase();
  return category ? [category] : [];
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
  if (!preview) {
    target.textContent = 'Список не проверялся.';
    return;
  }
  if (preview.loading) {
    target.textContent = preview.message || 'Загружаю домены выбранной группы...';
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
function renderV2flyCategoryCatalog(){
  const target = el('v2fly-category-status');
  const data = state.v2flyCategories || {};
  const categories = data.categories || [];
  const query = String(el('v2fly-category-search')?.value || '').trim().toLowerCase();
  const visible = query ? categories.filter((category) => category.includes(query)) : categories;
  const options = el('v2fly-category-options');
  if (options) options.innerHTML = visible.slice(0, 500).map((category) => `<option value="${esc(category)}"></option>`).join('');
  const button = document.querySelector('[data-action="v2fly-load-categories"]');
  const loading = state.v2flyCategorySource === 'loading';
  if (button) {
    const canRefresh = Boolean(data.can_refresh || data.update_available);
    button.disabled = loading || !canRefresh;
    button.textContent = loading ? 'Проверяю каталог' : (data.update_available ? 'Обновить каталог' : 'Каталог актуален');
    button.title = data.update_available
      ? 'Скачивает свежий каталог групп v2fly: в источнике найден новый commit.'
      : 'Кнопка станет активной, когда backend найдет новый commit в v2fly/domain-list-community.';
  }
  if (!target) return;
  if (loading) {
    target.textContent = 'Проверяю локальный каталог и доступные обновления...';
    return;
  }
  if (!categories.length) {
    target.textContent = data.revision_error ? `Каталог пока недоступен: ${data.revision_error}` : 'Каталог пока не загружен.';
    return;
  }
  const selected = v2flyCategories()[0] || '';
  const updateText = data.update_available ? ' Доступно обновление каталога.' : '';
  const queryText = query ? ` Найдено по вводу: ${visible.length}.` : '';
  target.textContent = `Каталог готов: ${data.all_count || categories.length} групп.${queryText}${selected ? ` Выбрано: ${selected}.` : ''}${updateText}`;
}
function presetManagerMeta(scope){
  return (state.customPresetMeta && state.customPresetMeta[scope]) || {};
}
function renderPresetManager(){
  const nameSelect = el('preset-manager-name');
  const list = el('preset-manager-list');
  if (!nameSelect || !list) return;
  const manager = state.presetManager;
  const scope = 'finder';
  const names = customPresetNames(scope);
  if (!manager.name || !names.includes(manager.name)) manager.name = names[0] || '';
  const sourceScope = manager.name ? customPresetSourceScope(scope, manager.name) : scope;
  manager.scope = sourceScope;
  nameSelect.innerHTML = names.length
    ? names.map((name) => `<option value="${esc(name)}">${esc(name)} (${customPresetCount(scope, name)})</option>`).join('')
    : '<option value="">Нет пользовательских списков</option>';
  nameSelect.value = manager.name || '';
  const meta = manager.name ? presetManagerMeta(sourceScope)[manager.name] : null;
  const count = meta ? `${meta.enabled_count || 0}/${meta.total_count || 0}` : '0';
  setText('preset-manager-count', count);
  const query = el('preset-manager-query');
  if (query && query.value !== manager.query) query.value = manager.query || '';
  const note = el('preset-manager-note');
  if (!manager.name) {
    note.textContent = 'Пользовательских списков пока нет. Создайте список в подборе или импортируйте его из v2fly.';
    list.innerHTML = '';
    return;
  }
  const loaded = Array.isArray(manager.domains) ? manager.domains : [];
  const updated = meta && meta.updated_at ? ` · обновлено ${friendlyDate(meta.updated_at)}` : '';
  note.textContent = manager.loading
    ? 'Загрузка списка...'
    : `Показано ${loaded.length} из ${manager.total || 0}. Активно ${meta ? meta.enabled_count : 0}${updated}`;
  if (!manager.loaded && !manager.loading) {
    list.innerHTML = '<div class="empty">Нажмите “Показать домены”, чтобы загрузить список.</div>';
    return;
  }
  if (manager.loading && !loaded.length) {
    list.innerHTML = '<div class="loading-skeleton" aria-label="Загрузка списка доменов"></div>';
    return;
  }
  const rows = loaded.map((item) => `
    <label class="preset-domain-row ${item.enabled ? '' : 'disabled'}">
      <input type="checkbox" data-preset-domain-toggle="${esc(item.domain)}" ${item.enabled ? 'checked' : ''}>
      <span class="preset-domain-name">${esc(item.domain)}</span>
    </label>
  `).join('');
  const more = manager.hasMore
    ? `<button class="secondary" data-action="preset-manager-load-more" type="button">Показать еще 200</button>`
    : '';
  list.innerHTML = rows || '<div class="empty">По этому фильтру домены не найдены.</div>';
  if (more) list.insertAdjacentHTML('beforeend', `<div class="button-row">${more}</div>`);
}
async function refreshPresetManager(reset){
  const manager = state.presetManager;
  const scope = 'finder';
  const name = el('preset-manager-name')?.value || manager.name || '';
  const query = String(el('preset-manager-query')?.value || '').trim();
  const sourceScope = name ? customPresetSourceScope(scope, name) : scope;
  if (!name) {
    manager.scope = sourceScope;
    manager.name = '';
    manager.query = query;
    manager.domains = [];
    manager.total = 0;
    manager.hasMore = false;
    manager.loaded = false;
    renderPresetManager();
    return;
  }
  const offset = reset ? 0 : (manager.domains || []).length;
  Object.assign(manager, { scope: sourceScope, name, query, loading: true });
  renderPresetManager();
  try {
    const params = new URLSearchParams();
    params.set('scope', sourceScope);
    params.set('name', name);
    params.set('kind', 'user');
    params.set('include_disabled', '1');
    params.set('limit', '200');
    params.set('offset', String(offset));
    if (query) params.set('query', query);
    const data = await getJson(`/api/presets/domains?${params.toString()}`);
    const rows = Array.isArray(data.domains) ? data.domains : [];
    manager.domains = reset ? rows : [...(manager.domains || []), ...rows];
    manager.total = Number(data.total || 0);
    manager.hasMore = Boolean(data.has_more);
    manager.loading = false;
    manager.loaded = true;
    renderPresetManager();
  } catch (error) {
    manager.loading = false;
    renderPresetManager();
    setMessage(`Ошибка загрузки списка доменов: ${error.message}`, 'bad');
  }
}
async function togglePresetDomain(domain, enabled){
  const manager = state.presetManager;
  if (!manager.name || !domain) return;
  try {
    const data = await postJson('/api/presets/domain-enabled', {
      scope: manager.scope || 'finder',
      name: manager.name,
      domain,
      enabled
    });
    mergePresetResponse(data);
    manager.domains = (manager.domains || []).map((item) => item.domain === domain ? { ...item, enabled } : item);
    if (state.customPresets[manager.scope || 'finder']) delete state.customPresets[manager.scope || 'finder'][manager.name];
    localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify(state.customPresets));
    renderPresetSelects();
    renderPresetManager();
    showToast(enabled ? 'Домен включен' : 'Домен выключен', 'good');
  } catch (error) {
    showToast(`Ошибка изменения домена: ${error.message}`, 'bad');
    refreshPresetManager(true);
  }
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
  return String(el('preset-editor-name')?.value || el('preset-manager-name')?.value || '').trim();
}
async function loadPresetEditorFromSelection(){
  const scope = presetEditorScope();
  const name = el('preset-manager-name')?.value || state.presetManager.name || '';
  if (!name) {
    setMessage('Выберите пользовательский список', 'warn');
    return;
  }
  try {
    const domains = await fetchAllPresetDomains(scope, name);
    const nameInput = el('preset-editor-name');
    const domainsInput = el('preset-editor-domains');
    if (nameInput) nameInput.value = name;
    if (domainsInput) {
      domainsInput.value = domains.join('\\n');
      updateEditorLineNumbers('preset-editor-domains');
    }
    renderPresetEditorPreview(null);
    setMessage('Список загружен в редактор', 'good');
  } catch (error) {
    setMessage(`Ошибка загрузки списка в редактор: ${error.message}`, 'bad');
  }
}
async function buildPresetEditorPreview(){
  const scope = presetEditorScope();
  const name = presetEditorName();
  const domains = presetEditorDomains();
  if (!name || !domains.length) {
    setMessage('Укажите название списка и хотя бы один домен', 'warn');
    return null;
  }
  let current = [];
  if ((state.customPresetMeta[scope] || {})[name]) {
    current = await fetchAllPresetDomains(scope, name);
  }
  const currentSet = new Set(current);
  const nextSet = new Set(domains);
  const added = domains.filter((domain) => !currentSet.has(domain));
  const removed = current.filter((domain) => !nextSet.has(domain));
  const preview = {
    scope,
    name,
    total: domains.length,
    added: added.length,
    removed: removed.length,
    unchanged: domains.length - added.length
  };
  renderPresetEditorPreview(preview);
  return preview;
}
async function previewPresetEditor(){
  try {
    await buildPresetEditorPreview();
    setMessage('Изменения списка посчитаны', 'good');
  } catch (error) {
    setMessage(`Ошибка проверки списка: ${error.message}`, 'bad');
  }
}
async function savePresetEditor(){
  try {
    const preview = await buildPresetEditorPreview();
    if (!preview) return;
    const domains = presetEditorDomains();
    const data = await postJson('/api/presets/save', { scope: preview.scope, name: preview.name, domains });
    mergePresetResponse(data);
    state.customPresets[preview.scope][preview.name] = domains;
    localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify(state.customPresets));
    state.presetManager.scope = preview.scope;
    state.presetManager.name = preview.name;
    state.presetManager.domains = [];
    state.presetManager.total = 0;
    state.presetManager.hasMore = false;
    state.presetManager.loaded = false;
    renderPresetSelects();
    renderPresetManager();
    await refreshPresetManager(true);
    setMessage('Список сохранен', 'good');
  } catch (error) {
    setMessage(`Ошибка сохранения списка: ${error.message}`, 'bad');
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
    params.set('check', '1');
    if (refreshCatalog) params.set('refresh', '1');
    const data = await getJson(`/api/domain-sources/v2fly/categories?${params.toString()}`);
    state.v2flyCategories = data;
    state.v2flyCategorySource = data.source || '';
    renderV2flyCategoryCatalog();
    const tone = data.source === 'fallback' || data.revision_error ? 'warn' : 'good';
    const suffix = data.update_available ? ', есть обновление' : '';
    setMessage(`Каталог v2fly готов: ${data.all_count || data.total || 0} групп${suffix}`, tone);
  } catch (error) {
    state.v2flyCategories = { categories: [] };
    state.v2flyCategorySource = '';
    renderV2flyCategoryCatalog();
    setMessage(`Ошибка загрузки каталога v2fly: ${error.message}`, 'bad');
  }
}
async function previewV2flyPreset(){
  const payload = v2flyPayload();
  if (!payload.name || !payload.categories.length) {
    setMessage('Укажите название пресета и хотя бы одну категорию v2fly', 'warn');
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
    setMessage(`Ошибка проверки v2fly: ${error.message}`, 'bad');
  }
}
async function importV2flyPreset(){
  const payload = v2flyPayload();
  if (!payload.name || !payload.categories.length) {
    setMessage('Укажите название пресета и хотя бы одну категорию v2fly', 'warn');
    return;
  }
  state.v2flyPreview = { loading: true, message: 'Сохраняю доменный пресет...' };
  renderV2flyPreview();
  try {
    const data = await postJson('/api/domain-sources/v2fly/import', payload);
    state.v2flyPreview = data;
    mergePresetResponse(data);
    renderPresetSelects();
    renderPresetManager();
    if (Array.isArray(data.domains)) {
      el('v2fly-domains').value = data.domains.join('\\n');
      updateEditorLineNumbers('v2fly-domains');
    }
    renderV2flyPreview();
    setMessage(`Пресет сохранен: ${data.count || 0} доменов`, 'good');
  } catch (error) {
    setMessage(`Ошибка сохранения v2fly: ${error.message}`, 'bad');
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
    const domains = [...new Set(defaultDomains('critical'))];
    el('finder-domains').value = domains.join('\\n');
    state.domainsInitialized = true;
  }
  renderMetrics();
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
async function refreshDomainIndex(){
  const requestId = ++domainIndexRequestSeq;
  state.candidateLoading = true;
  renderCandidatesOnly();
  try {
    const params = new URLSearchParams();
    const data = await getJson(`/api/strategy-finder/candidate-domains?${params.toString()}`);
    if (requestId !== domainIndexRequestSeq) return;
    state.candidateDomains = data.domains || [];
    state.candidateDomainTotal = Number(data.total || 0);
    state.candidateDomainStrategyTotal = Number(data.strategy_total || 0);
    rememberCandidateVersion(data.version || null);
    state.testedDomains = Array.isArray(data.tested_domains) ? data.tested_domains : state.testedDomains;
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
    state.testedDomains = Array.isArray(data.tested_domains) ? data.tested_domains : state.testedDomains;
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
      state.testedDomains = Array.isArray(data.tested_domains) ? data.tested_domains : state.testedDomains;
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
      state.testedDomains = Array.isArray(data.tested_domains) ? data.tested_domains : state.testedDomains;
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
    state.testedDomains = Array.isArray(data.tested_domains) ? data.tested_domains : state.testedDomains;
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
  const settingsChanged = previousSettings !== JSON.stringify(state.settings || {});
  if (settingsChanged) renderSettings();
  return settingsChanged;
}
async function refreshRuns(){
  try {
    const finderRuns = await getJson('/api/strategy-finder/runs');
    state.finderRuns = latestById(finderRuns.runs || []);
    renderRuns();
    renderMetrics();
  } catch (error) {
    setMessage(`РћС€РёР±РєР° РѕР±РЅРѕРІР»РµРЅРёСЏ РёСЃС‚РѕСЂРёРё: ${error.message}`, 'bad');
  }
}
async function refreshLog(incremental = false){
  try {
    const previous = state.finderLog;
    const payload = await getJson(latestLogUrl(incremental));
    state.finderLog = incremental ? mergeLogPayload(previous, payload) : payload;
    logDirty = false;
    renderLog();
    renderMetrics();
  } catch (error) {
    setMessage(`РћС€РёР±РєР° РѕР±РЅРѕРІР»РµРЅРёСЏ Р»РѕРіР°: ${error.message}`, 'bad');
  }
}
async function refreshPresets(){
  try {
    const presets = await getJson('/api/presets');
    mergeCustomPresets((presets || {}).custom || {}, (presets || {}).metadata || {});
    renderPresetSelects();
    renderPresetManager();
  } catch (error) {
    setMessage(`РћС€РёР±РєР° РѕР±РЅРѕРІР»РµРЅРёСЏ РїСЂРµСЃРµС‚РѕРІ: ${error.message}`, 'bad');
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
    const [status, finderRuns, finderLog, domainSets, presets, settings, discoveryProfiles, domainSources] = await Promise.all([
      getJson('/api/status'),
      getJson('/api/strategy-finder/runs'),
      getJson('/api/strategy-finder/latest-log'),
      getJson('/api/strategy-finder/domains'),
      getJson('/api/presets'),
      getJson('/api/settings'),
      getJson('/api/discovery-profiles'),
      getJson('/api/domain-sources')
    ]);
    mergeStatusPayload(status);
    state.settings = (settings || {}).settings || status.settings || {};
    state.finderRuns = latestById(finderRuns.runs || []);
    state.finderLog = finderLog;
    state.domainSets = domainSets;
    state.discoveryProfiles = (discoveryProfiles || {}).profiles || {};
    state.domainSources = domainSources;
    mergeCustomPresets((presets || {}).custom || {}, (presets || {}).metadata || {});
    renderAll({ skipCandidates: true });
    if (!state.candidateDomainsLoaded) refreshDomainIndex();
    else if (state.activeTab === 'candidates') ensureCandidateViewLoaded();
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
      headers: { 'Content-Type': 'application/zip' },
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
  } catch (error) {
    setMessage(error.message, 'bad');
    await refresh();
  }
}
async function startSelectedDiscovery(){
  const options = discoveryOptions();
  if (!hasEnabledProtocol(options)) {
    setMessage('Выберите хотя бы один протокол для проверки', 'bad');
    return;
  }
  const mode = selectedRunMode();
  const payload = {
    domains: finderDomains(),
    ...options
  };
  const timeout = timeoutSecondsOrNull();
  if (timeout !== null) payload.timeout_seconds = timeout;
  await saveRunPreferencesNow();
  if (mode === 'multi') {
    payload.curl_parallelism = curlParallelism();
    await startJob('/api/jobs/zapret-multi-domain-discovery', payload, 'Все домены на одной стратегии');
    return;
  }
  await startJob('/api/jobs/zapret-standard-discovery', payload, 'Поиск стратегий');
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
  if (button.dataset.action === 'v2fly-preview') {
    previewV2flyPreset();
    return;
  }
  if (button.dataset.action === 'v2fly-import') {
    importV2flyPreset();
    return;
  }
  if (button.dataset.action === 'preset-manager-refresh') {
    refreshPresetManager(true);
    return;
  }
  if (button.dataset.action === 'preset-manager-load-more') {
    refreshPresetManager(false);
    return;
  }
  if (button.dataset.action === 'preset-editor-load') {
    loadPresetEditorFromSelection();
    return;
  }
  if (button.dataset.action === 'preset-editor-preview') {
    previewPresetEditor();
    return;
  }
  if (button.dataset.action === 'preset-editor-save') {
    savePresetEditor();
    return;
  }
  if (button.dataset.action === 'preset-editor-export') {
    exportPresetEditor();
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
  if (event.target && SETTINGS_PRESET_CONTROL_IDS.has(event.target.id)) {
    markSettingsPresetCustom();
  }
  if (event.target && String(event.target.id || '').startsWith('settings-')) {
    state.settingsTouched = true;
  }
  if (event.target && event.target.id === 'settings-update-channel') {
    renderReleaseInfo();
  }
  if (event.target && String(event.target.id || '').startsWith('v2fly-')) {
    if (event.target.id === 'v2fly-category-search') {
      suggestV2flyPresetName();
      renderV2flyCategoryCatalog();
    }
    if (event.target.id === 'v2fly-domains') updateEditorLineNumbers('v2fly-domains');
    state.v2flyPreview = null;
    renderV2flyPreview();
  }
  if (event.target && event.target.id === 'preset-manager-query') {
    state.presetManager.query = String(event.target.value || '').trim();
  }
  if (event.target && event.target.id === 'preset-editor-domains') {
    updateEditorLineNumbers('preset-editor-domains');
    renderPresetEditorPreview(null);
  }
  if (event.target && event.target.id === 'preset-editor-name') {
    renderPresetEditorPreview(null);
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
  if (isRunPreferenceControl(event.target)) {
    scheduleRunPreferencesSave();
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
  if (event.target && SETTINGS_PRESET_CONTROL_IDS.has(event.target.id)) {
    markSettingsPresetCustom();
  }
  if (event.target && String(event.target.id || '').startsWith('settings-')) {
    state.settingsTouched = true;
  }
  if (event.target && String(event.target.id || '').startsWith('v2fly-')) {
    if (event.target.id === 'v2fly-category-search') {
      suggestV2flyPresetName();
      renderV2flyCategoryCatalog();
    }
    if (event.target.id === 'v2fly-domains') updateEditorLineNumbers('v2fly-domains');
    state.v2flyPreview = null;
    renderV2flyPreview();
  }
  if (event.target && event.target.dataset && event.target.dataset.presetDomainToggle) {
    togglePresetDomain(event.target.dataset.presetDomainToggle, Boolean(event.target.checked));
  }
  if (event.target && event.target.id === 'limit-time-enabled') {
    el('time-limit-field').hidden = !event.target.checked;
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
    state.presetManager.domains = [];
    state.presetManager.total = 0;
    state.presetManager.hasMore = false;
    state.presetManager.loaded = false;
    renderPresetManager();
  }
  if (event.target && event.target.id === 'discovery-profile-select') {
    if (event.target.value === CUSTOM_SELECT_VALUE) {
      markDiscoveryProfileCustom();
    } else {
      const profile = (state.discoveryProfiles || {})[event.target.value];
      useDiscoveryProfile(profile);
    }
  }
  if (event.target && event.target.id === 'settings-preset-select') {
    if (event.target.value === CUSTOM_SELECT_VALUE) {
      markSettingsPresetCustom();
    } else {
      setSettingsPreset(event.target.value);
    }
  }
  if (event.target && event.target.name === 'run-mode') {
    renderRunModeNote();
  }
  if (event.target && DISCOVERY_PROFILE_CONTROL_IDS.has(event.target.id)) {
    markDiscoveryProfileCustom();
  }
  if (isRunPreferenceControl(event.target)) {
    scheduleRunPreferencesSave();
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


def status_payload(config: AppConfig) -> dict[str, Any]:
    return {
        "version": __version__,
        "state": read_state(config.output.state_dir),
        "settings": read_settings(config),
        "run_preferences": read_run_preferences(config),
        "release_update": release_update_status(config.output.state_dir, current_version=__version__),
        "candidate_version": candidate_storage_version(config.output.state_dir),
        "paths": {
            "state_dir": str(config.output.state_dir),
        },
        "zapret2": check_install_cached(),
    }


def _event_payloads(config: AppConfig) -> dict[str, dict[str, Any]]:
    status = status_payload(config)
    status_event = {
        key: status[key]
        for key in ("version", "state", "settings", "run_preferences", "release_update", "paths", "zapret2")
        if key in status
    }
    return {
        "status": status_event,
        "runs": _runs_event_payload(config.output.state_dir),
        "log": _log_event_payload(config.output.state_dir),
        "candidates": {"version": status.get("candidate_version") or {}},
        "settings": {"version": _event_fingerprint(status.get("settings") or {})},
        "presets": {"version": _event_fingerprint(read_custom_preset_index(config.output.state_dir))},
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
    "settings_preset_default": "normal",
    "update_channel": "stable",
}


DEFAULT_RUN_PREFERENCES = {
    "domains": [],
    "domain_preset": "builtin:critical",
    "discovery_profile": "standard",
    "settings_preset": "normal",
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
    settings_preset = str(raw.get("settings_preset") or "normal")
    if settings_preset not in {"cautious", "normal", "accelerated", "custom"}:
        settings_preset = "normal"
    timeout_hours_raw = raw.get("timeout_hours")
    try:
        timeout_hours = float(timeout_hours_raw)
    except (TypeError, ValueError):
        timeout_hours = 6.0
    timeout_hours = max(0.1, min(24.0, timeout_hours))
    return {
        "domains": _clean_domain_list(raw.get("domains") or []),
        "domain_preset": str(raw.get("domain_preset") or "builtin:critical")[:160],
        "discovery_profile": discovery_profile,
        "settings_preset": settings_preset,
        "run_mode": run_mode,
        "curl_parallelism": _bounded_int(raw.get("curl_parallelism"), default=4, minimum=1, maximum=10),
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
    max_parallelism = _bounded_int(raw.get("curl_parallelism_max"), default=10, minimum=1, maximum=10)
    settings_preset_default = str(raw.get("settings_preset_default") or "normal")
    if settings_preset_default not in {"cautious", "normal", "accelerated"}:
        settings_preset_default = "normal"
    preset_defaults = {"cautious": 2, "normal": 4, "accelerated": 10}
    default_source = (
        raw.get("curl_parallelism_default")
        if "settings_preset_default" not in raw and raw.get("curl_parallelism_default") is not None
        else preset_defaults[settings_preset_default]
    )
    default_parallelism = _bounded_int(default_source, default=preset_defaults[settings_preset_default], minimum=1, maximum=max_parallelism)
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
        "settings_preset_default": settings_preset_default,
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
        "curl_parallelism": _bounded_int(raw.get("curl_parallelism"), default=4, minimum=1, maximum=10),
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
        limit=_query_int(query, "limit", 200),
        offset=_query_int(query, "offset", 0),
        query=_query_str(query, "query", ""),
        view=_query_str(query, "view", "domain"),
        domains=_query_domains(query, "domains"),
        domain=_query_str(query, "domain", ""),
    )


def _candidate_domain_index_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    return read_candidate_domain_index(
        config.output.state_dir,
        query=_query_str(query, "query", ""),
    )


def _presets_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    payload: dict[str, Any] = {"metadata": read_custom_preset_index(config.output.state_dir)}
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
    return list_v2fly_categories_cached(
        config.output.state_dir,
        query=_query_str(query, "query", ""),
        limit=_query_int(query, "limit", 2000),
        refresh=_query_bool(query, "refresh", False),
        check_update=_query_bool(query, "check", True),
    )


def _v2fly_preview_payload(config: AppConfig, payload: dict[str, Any]) -> dict[str, Any]:
    return preview_v2fly_preset(
        config.output.state_dir,
        scope=str(payload.get("scope") or "finder"),
        name=str(payload.get("name") or ""),
        categories=_payload_string_list(payload, "categories"),
        domains=_payload_string_list(payload, "domains"),
    )


def _v2fly_import_payload(config: AppConfig, payload: dict[str, Any]) -> dict[str, Any]:
    return import_v2fly_preset(
        config.output.state_dir,
        scope=str(payload.get("scope") or "finder"),
        name=str(payload.get("name") or ""),
        categories=_payload_string_list(payload, "categories"),
        domains=_payload_string_list(payload, "domains"),
    )


def _query_str(query: dict[str, list[str]], key: str, default: str) -> str:
    values = query.get(key) or []
    return values[0] if values else default


def _query_int(query: dict[str, list[str]], key: str, default: int) -> int:
    raw = _query_str(query, key, str(default))
    try:
        return int(raw)
    except ValueError:
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
        curl_max_time=_minimum_int(settings.get("curl_max_time"), default=2, minimum=1),
        curl_max_time_quic=_minimum_int(settings.get("curl_max_time_quic"), default=2, minimum=1),
        curl_max_time_doh=_minimum_int(settings.get("curl_max_time_doh"), default=2, minimum=1),
        debug_stdout=_payload_bool(payload, "debug_stdout", bool(settings.get("debug_stdout"))),
        stop_event=stop_event,
    )


def _job_zapret_multi_domain_discovery(config: AppConfig, payload: dict[str, Any], stop_event: Any) -> dict[str, Any]:
    domains = _payload_domains(payload)
    settings = read_settings(config)
    max_parallelism = _bounded_int(settings.get("curl_parallelism_max"), default=10, minimum=1, maximum=10)
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
        curl_max_time=_minimum_int(settings.get("curl_max_time"), default=2, minimum=1),
        curl_max_time_quic=_minimum_int(settings.get("curl_max_time_quic"), default=2, minimum=1),
        curl_max_time_doh=_minimum_int(settings.get("curl_max_time_doh"), default=2, minimum=1),
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
