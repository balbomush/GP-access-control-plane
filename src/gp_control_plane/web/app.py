from __future__ import annotations

import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .. import __version__
from ..backups import create_snapshot_if_idle, import_snapshot_archive, list_snapshots, restore_snapshot_if_idle, snapshot_file_path
from ..config import AppConfig
from ..diagnostics import diagnostics_payload
from ..domain_sources import builtin_preset_sources, import_v2fly_preset, preview_v2fly_preset
from ..jobs import JobRunner
from ..state import now_iso, read_state, write_state
from ..storage import read_custom_presets, save_custom_presets
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
            elif path == "/api/settings":
                self._json({"settings": read_settings(config)})
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
                self._json(latest_log_tail(config.output.state_dir))
            elif path == "/api/backups":
                self._json(list_snapshots(config.output.state_dir))
            elif path == "/api/presets":
                self._json({"custom": read_custom_presets(config.output.state_dir)})
            elif path == "/api/domain-sources":
                self._json({"builtin": builtin_preset_sources()})
            elif path == "/api/backups/download":
                self._download_backup(config, query)
            else:
                self._not_found()

        def do_HEAD(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/":
                data = index_html().encode("utf-8")
                self._head(HTTPStatus.OK, "text/html; charset=utf-8", len(data))
            elif path in {
                "/api/status",
                "/api/settings",
                "/api/discovery-profiles",
                "/api/diagnostics",
                "/api/strategy-finder/domains",
                "/api/strategy-finder/candidate-domains",
                "/api/strategy-finder/candidates",
                "/api/strategy-finder/runs",
                "/api/strategy-finder/latest-log",
                "/api/backups",
                "/api/presets",
                "/api/domain-sources",
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
            if path == "/api/presets":
                saved = save_custom_presets(config.output.state_dir, payload.get("custom") or payload, now_iso())
                self._json({"custom": saved})
                return
            if path == "/api/settings":
                self._json({"settings": save_settings(config, payload.get("settings") or payload)})
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
                ),
                "/api/jobs/zapret-multi-domain-discovery": (
                    "zapret-multi-domain-discovery",
                    lambda stop: _job_zapret_multi_domain_discovery(config, payload, stop),
                ),
            }
            if path not in jobs:
                self._not_found()
                return
            name, func = jobs[path]
            try:
                job = runner.start(name, func)
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
.main {
  max-width: 1240px;
  margin: 0 auto;
  padding: 20px 24px 32px;
  display: grid;
  gap: 16px;
}
.status-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
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
.layout {
  display: grid;
  grid-template-columns: minmax(0, 460px) minmax(0, 1fr);
  gap: 16px;
  align-items: start;
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
.backup-restore-panel {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
  align-items: end;
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
  grid-template-columns: minmax(180px, .35fr) minmax(0, 1fr);
  gap: 12px;
  align-items: start;
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
  .status-grid, .button-row, .fill-row, .preset-grid, .preset-actions, .domain-picker-row, .backup-restore-panel, .backup-downloads { grid-template-columns: 1fr; }
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
      <button class="secondary" data-action="refresh" title="Обновляет статус, историю, лог и список найденных кандидатов.">Обновить</button>
    </div>
  </header>
  <main class="main">
    <section class="status-grid" aria-label="Сводка">
      <div class="metric">
        <div class="metric-label">zapret2</div>
        <div class="metric-value" id="metric-zapret">Загрузка</div>
        <div class="metric-note" id="metric-zapret-note">-</div>
      </div>
      <div class="metric">
        <div class="metric-label">Задание</div>
        <div class="metric-value" id="metric-job">-</div>
        <div class="metric-note" id="metric-job-note">-</div>
      </div>
      <button class="metric metric-button" data-tab="candidates" type="button">
        <div class="metric-label">Кандидаты</div>
        <div class="metric-value" id="metric-candidates">0</div>
        <div class="metric-note" id="metric-candidates-note">найдено blockcheck2</div>
      </button>
      <div class="metric">
        <div class="metric-label">Последний запуск</div>
        <div class="metric-value" id="metric-last-run">-</div>
        <div class="metric-note" id="metric-last-run-note">-</div>
      </div>
    </section>

    <nav class="tabs" role="tablist" aria-label="Разделы">
      <button class="tab-button active" data-tab="finder" type="button">Подбор</button>
      <button class="tab-button" data-tab="candidates" type="button">Кандидаты</button>
      <button class="tab-button" data-tab="terminal" type="button">Терминал</button>
      <button class="tab-button" data-tab="backups" type="button">Бекапы</button>
      <button class="tab-button" data-tab="settings" type="button">Настройки</button>
    </nav>

    <section class="tab-page active" data-tab-page="finder">
    <div class="layout">
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
              <div class="preset-grid">
                <div class="field">
                  <label for="finder-preset-select">Пресет доменов</label>
                  <select id="finder-preset-select"></select>
                </div>
                <div class="field">
                  <label for="finder-preset-name">Название для сохранения</label>
                  <input id="finder-preset-name" autocomplete="off" placeholder="мой список">
                </div>
              </div>
              <div class="preset-actions">
                <button class="secondary" data-preset-use="finder" title="Подставляет выбранный пресет в список доменов подбора." type="button">Применить пресет</button>
                <button class="secondary" data-preset-save="finder" title="Сохраняет текущий список доменов как пользовательский пресет или перезаписывает выбранный пользовательский пресет." type="button">Сохранить пресет</button>
                <button class="secondary danger" data-preset-delete="finder" title="Удаляет выбранный пользовательский пресет. Встроенные пресеты не удаляются." type="button">Удалить пресет</button>
              </div>
            </div>
            <div class="preset-panel">
              <div class="preset-grid">
                <div class="field">
                  <label for="discovery-profile-select">Профиль подбора</label>
                  <select id="discovery-profile-select"></select>
                </div>
                <div class="field">
                  <label for="discovery-profile-name">Название для сохранения</label>
                  <input id="discovery-profile-name" autocomplete="off" placeholder="мой-профиль">
                </div>
              </div>
              <div class="preset-actions">
                <button class="secondary" data-action="use-discovery-profile" title="Применяет сохраненные настройки blockcheck2, curl и лимита времени к форме подбора." type="button">Применить профиль</button>
                <button class="secondary" data-action="save-discovery-profile" title="Сохраняет текущие настройки подбора как пользовательский профиль." type="button">Сохранить профиль</button>
                <button class="secondary danger" data-action="delete-discovery-profile" title="Удаляет выбранный пользовательский профиль. Встроенные профили не удаляются." type="button">Удалить профиль</button>
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
            <div class="field">
              <label for="curl-parallelism">Параллельных curl в режиме стратегия -> домены</label>
              <input id="curl-parallelism" type="number" min="1" max="10" step="1" value="4">
              <div class="helper-text">Для экспериментального режима: одна стратегия запускается один раз, затем выбранные домены проверяются параллельными curl. Остальные настройки blockcheck2 ниже также применяются. Если включить параллельные повторы, реальное число curl может быть: этот лимит × повторы.</div>
            </div>
            <div class="preset-panel finder-options-panel">
              <div class="helper-text">Настройки blockcheck2, которые реально влияют на подбор стратегий.</div>
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
              <div class="preset-grid">
                <div class="field">
                  <label for="scan-level">Уровень поиска</label>
                  <select id="scan-level">
                    <option value="quick">quick</option>
                    <option value="standard" selected>standard</option>
                    <option value="force">force</option>
                  </select>
                </div>
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
            </div>
            <div class="button-row run-actions">
              <button data-action="standard-discovery" title="Запускает штатный blockcheck2: домены проверяются обычным порядком скрипта.">Обычный поиск: домены по очереди</button>
              <button class="secondary" data-action="multi-domain-discovery" title="Экспериментальный режим: одна стратегия запускается один раз, затем параллельно проверяется на выбранных доменах.">Эксперимент: стратегия сразу по доменам</button>
              <button class="secondary" data-action="refresh" title="Обновляет статус, историю, лог и список найденных кандидатов.">Обновить данные</button>
              <button class="secondary danger" data-action="stop-current" title="Останавливает текущий подбор и сохраняет уже найденные успешные стратегии." disabled>Остановить текущий запуск</button>
            </div>
            <div class="message" id="message">Готово</div>
          </div>
        </section>
      </div>

      <div class="stack">
        <section class="panel">
          <div class="panel-header">
            <h2>История запусков</h2>
            <span class="badge" id="finder-runs-count">0</span>
          </div>
          <div id="finder-runs-table"></div>
        </section>

      </div>
    </div>
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
          <div class="preset-actions">
            <button class="secondary" data-preset-use="common" title="Подставляет выбранный пресет в фильтр общих стратегий. Непротестированные домены будут пропущены." type="button">Применить пресет</button>
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

    <section class="tab-page backups-page" data-tab-page="backups">
      <section class="panel">
        <div class="panel-header">
          <h2>Бекапы</h2>
          <span class="badge" id="backups-count">0</span>
        </div>
        <div class="button-row">
          <button class="secondary" data-action="refresh-backups" type="button">Обновить список</button>
          <button data-action="create-backup" type="button">Создать бекап сейчас</button>
          <label class="secondary file-button" for="backup-upload-file">Выбрать ZIP</label>
          <input id="backup-upload-file" type="file" accept=".zip,application/zip" hidden>
          <button class="secondary" data-action="upload-backup" type="button">Загрузить бекап</button>
        </div>
        <div class="helper-text">Бекап создается только когда подбор не запущен. Хранятся последние 5 успешных копий.</div>
        <div class="backup-restore-panel">
          <div class="field">
            <label for="backup-restore-select">Бекап для восстановления</label>
            <select id="backup-restore-select"></select>
          </div>
          <button class="secondary danger" data-action="restore-selected-backup" type="button">Восстановить выбранный бекап</button>
        </div>
        <div id="backups-table" class="backup-list"></div>
      </section>
    </section>

    <section class="tab-page settings-page" data-tab-page="settings">
      <section class="panel">
        <div class="panel-header">
          <h2>Настройки</h2>
          <span class="badge" id="settings-version">-</span>
        </div>
        <div class="preset-panel">
          <div class="preset-grid">
            <label class="checkbox-row">
              <input id="settings-enable-ipv6" type="checkbox">
              IPv6-проверки
            </label>
            <label class="checkbox-row">
              <input id="settings-debug-stdout" type="checkbox">
              Подробный debug-лог stdout
            </label>
            <div class="field">
              <label for="settings-curl-default">Параллельных curl по умолчанию</label>
              <input id="settings-curl-default" type="number" min="1" max="10" value="4">
            </div>
            <div class="field">
              <label for="settings-curl-max">Максимум параллельных curl</label>
              <input id="settings-curl-max" type="number" min="1" max="10" value="10">
            </div>
            <div class="field">
              <label for="settings-update-channel">Канал обновлений</label>
              <select id="settings-update-channel">
                <option value="stable">Последний стабильный релиз</option>
                <option value="prerelease">Предрелизы</option>
              </select>
            </div>
          </div>
          <div class="button-row">
            <button data-action="save-settings" type="button">Сохранить настройки</button>
            <a class="button-link secondary" id="settings-stable-link" href="https://github.com/balbomush/GP-access-control-plane/releases/latest" target="_blank" rel="noreferrer">Открыть последний релиз</a>
            <a class="button-link secondary" id="settings-prerelease-link" href="https://github.com/balbomush/GP-access-control-plane/releases" target="_blank" rel="noreferrer">Открыть предрелизы</a>
          </div>
          <div class="helper-text">Подробный stdout нужен только для диагностики: он пишет больший debug-лог. Обычный runtime-log остается компактным.</div>
          <div class="helper-text">Обновление из web UI будет выполняться только когда подбор не запущен. До этого шага используются ссылки на релизы.</div>
        </div>
        <div class="preset-panel">
          <div class="panel-header">
            <h2>Источники доменов</h2>
            <span class="badge">v2fly</span>
          </div>
          <div class="preset-grid">
            <div class="field">
              <label for="v2fly-preset-name">Название пресета</label>
              <input id="v2fly-preset-name" autocomplete="off" placeholder="v2fly-youtube">
            </div>
            <div class="field">
              <label for="v2fly-scope">Где использовать</label>
              <select id="v2fly-scope">
                <option value="finder">Подбор</option>
                <option value="common">Общие стратегии</option>
              </select>
            </div>
          </div>
          <div class="field">
            <label for="v2fly-categories">Категории v2fly/domain-list-community</label>
            <textarea id="v2fly-categories" class="line-numbered-textarea" autocomplete="off" spellcheck="false" placeholder="youtube&#10;google&#10;discord"></textarea>
          </div>
          <div class="button-row">
            <button class="secondary" data-action="v2fly-preview" type="button">Проверить список</button>
            <button data-action="v2fly-import" type="button">Сохранить в пресет</button>
          </div>
          <div class="source-preview" id="v2fly-preview-result">Список не проверялся.</div>
          <div class="helper-text">Берутся только доменные правила v2fly: domain/full и явные доменные строки. keyword/regexp/include не превращаются в домены автоматически.</div>
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
const state = { status: null, settings: null, settingsTouched: false, discoveryProfiles: {}, candidates: [], candidateTotal: 0, candidateOffset: 0, candidateHasMore: false, candidateVersion: null, candidateKnownVersion: null, candidateQueryKey: '', commonCandidateCache: {}, commonLoadingAll: false, candidateDomains: [], candidateDomainTotal: 0, candidateDomainStrategyTotal: 0, candidateDomainsLoaded: false, testedDomains: [], candidatesLoaded: false, domainStrategies: {}, finderRuns: [], finderLog: null, domainSets: null, domainSources: null, v2flyPreview: null, backups: [], backupsLoaded: false, activeTab: 'finder', candidateView: 'domain', customPresets: loadCustomPresets(), openCandidateDomains: {}, openCommonProtocols: {}, openRunDomains: {}, expandedStrategyLists: {}, strategyEditorScrolls: {}, domainsInitialized: false, domainsTouched: false };
const jobNames = {
  'zapret-standard-discovery': 'Поиск стратегий',
  'zapret-multi-domain-discovery': 'Стратегия -> домены',
  'standard-discovery': 'Поиск стратегий',
  'multi-domain-discovery': 'Стратегия -> домены'
};
const statusTone = { success: 'good', failed: 'bad', running: 'warn', queued: 'warn', stopping: 'warn', stopped: 'warn', timeout: 'warn' };
let toastTimer = null;
let refreshInFlight = false;
let candidateRefreshTimer = null;
let candidateRequestSeq = 0;
let domainIndexRequestSeq = 0;

function el(id){ return document.getElementById(id); }
function esc(value){
  return String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[char]));
}
function setText(id, value){ el(id).textContent = value; }
function setMessage(text, tone){
  const node = el('message');
  node.textContent = text;
  node.className = 'message' + (tone ? ' ' + tone : '');
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
  if (tabName === 'terminal') scrollLogToBottom();
  if (tabName === 'candidates') ensureCandidateViewLoaded();
  if (tabName === 'backups' && !state.backupsLoaded) refreshBackups();
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
function currentDiscoveryProfileFromForm(){
  const timeoutHours = Number(el('finder-timeout-hours').value || 6);
  return {
    ...discoveryOptions(),
    curl_parallelism: curlParallelism(),
    limit_time_enabled: el('limit-time-enabled').checked,
    timeout_hours: Number.isFinite(timeoutHours) ? Math.max(1, Math.min(24, Math.round(timeoutHours))) : 6
  };
}
function useDiscoveryProfile(profile){
  if (!profile) return;
  el('enable-http').checked = Boolean(profile.enable_http);
  el('enable-tls12').checked = Boolean(profile.enable_tls12);
  el('enable-tls13').checked = Boolean(profile.enable_tls13);
  el('include-quic').checked = Boolean(profile.include_quic);
  el('enable-ipv6').checked = Boolean(profile.enable_ipv6);
  el('scan-level').value = profile.scan_level || 'standard';
  el('repeats').value = String(profile.repeats || 1);
  el('repeat-parallel').checked = Boolean(profile.repeat_parallel);
  el('skip-dnscheck').checked = Boolean(profile.skip_dnscheck);
  el('skip-ipblock').checked = Boolean(profile.skip_ipblock);
  el('curl-parallelism').value = String(profile.curl_parallelism || 4);
  el('limit-time-enabled').checked = Boolean(profile.limit_time_enabled);
  el('finder-timeout-hours').value = String(profile.timeout_hours || 6);
  el('time-limit-field').hidden = !el('limit-time-enabled').checked;
  state.settingsTouched = true;
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
  const nameInput = el('discovery-profile-name');
  if (nameInput && select.value && !nameInput.value) {
    nameInput.value = select.value;
  }
}
async function persistDiscoveryProfiles(profiles){
  const data = await postJson('/api/discovery-profiles', { profiles });
  state.discoveryProfiles = data.profiles || {};
  renderDiscoveryProfiles();
}
async function saveDiscoveryProfile(){
  const name = String(el('discovery-profile-name')?.value || '').trim();
  if (!name) {
    setMessage('Укажите название профиля подбора', 'warn');
    return;
  }
  const profiles = { ...(state.discoveryProfiles || {}) };
  profiles[name] = { ...currentDiscoveryProfileFromForm(), title: name };
  try {
    await persistDiscoveryProfiles(profiles);
    const savedName = Object.keys(state.discoveryProfiles || {}).find((key) => profileTitle(key, state.discoveryProfiles[key]) === name) || name;
    el('discovery-profile-select').value = savedName;
    setMessage('Профиль подбора сохранен', 'good');
  } catch (error) {
    setMessage(`Ошибка сохранения профиля: ${error.message}`, 'bad');
  }
}
async function deleteDiscoveryProfile(){
  const select = el('discovery-profile-select');
  const name = select ? select.value : '';
  if (!name || ['balanced', 'deep'].includes(name)) {
    setMessage('Встроенный профиль удалить нельзя', 'warn');
    return;
  }
  const profiles = { ...(state.discoveryProfiles || {}) };
  delete profiles[name];
  try {
    await persistDiscoveryProfiles(profiles);
    setMessage('Профиль подбора удален', 'good');
  } catch (error) {
    setMessage(`Ошибка удаления профиля: ${error.message}`, 'bad');
  }
}
function useSelectedDiscoveryProfile(){
  const select = el('discovery-profile-select');
  const profile = select ? (state.discoveryProfiles || {})[select.value] : null;
  if (!profile) {
    setMessage('Профиль подбора не выбран', 'warn');
    return;
  }
  useDiscoveryProfile(profile);
  setMessage('Профиль подбора применен', 'good');
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
function mergeCustomPresets(remote){
  const result = { finder: {}, common: {} };
  for (const scope of ['finder', 'common']) {
    result[scope] = {
      ...((remote && typeof remote[scope] === 'object') ? remote[scope] : {}),
      ...((state.customPresets && typeof state.customPresets[scope] === 'object') ? state.customPresets[scope] : {})
    };
  }
  state.customPresets = result;
  localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify(state.customPresets));
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
  groups.push({
    label: 'Диагностика',
    presets: [
      make('diagnostic', 'Диагностика')
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
  if (scope === 'custom') return state.customPresets[target]?.[key] || [];
  return [];
}
function renderPresetSelect(target){
  const select = el(`${target}-preset-select`);
  if (!select) return;
  const previous = select.value;
  const customEntries = Object.entries(state.customPresets[target] || {}).sort(([a], [b]) => a.localeCompare(b));
  const customGroup = customEntries.length
    ? `<optgroup label="Персональные">${customEntries.map(([name, domains]) => `<option value="custom:${esc(name)}">${esc(name)} (${uniqueDomainCount(domains)})</option>`).join('')}</optgroup>`
    : '';
  const builtInGroups = presetGroups(target).map((group) => {
    const options = group.presets.map((preset) => `<option value="builtin:${esc(preset.key)}">${esc(preset.label)} (${uniqueDomainCount(preset.domains)})</option>`).join('');
    return `<optgroup label="${esc(group.label)}">${options}</optgroup>`;
  }).join('');
  select.innerHTML = `${customGroup}${builtInGroups}`;
  if ([...select.options].some((option) => option.value === previous)) select.value = previous;
}
function renderPresetSelects(){
  renderPresetSelect('finder');
  renderPresetSelect('common');
}
function usePreset(target){
  const domains = presetDomains(target, el(`${target}-preset-select`).value);
  const finalDomains = target === 'common' ? filterTestedDomains(domains) : domains;
  el(`${target}-domains`).value = uniqueDomains(finalDomains).join('\\n');
  updateEditorLineNumbers(`${target}-domains`);
  if (target === 'finder') state.domainsTouched = true;
  if (target === 'common') {
    prepareCommonCandidateState();
    renderCandidatesOnly();
    if (selectedCommonDomains().length >= 2) refreshCandidates(true);
  }
  else renderCandidates();
}
function presetNameForSave(target){
  const nameInput = el(`${target}-preset-name`);
  const explicit = nameInput ? nameInput.value.trim() : '';
  if (explicit) return explicit;
  const selected = el(`${target}-preset-select`).value || '';
  if (selected.startsWith('custom:')) return selected.slice('custom:'.length);
  return '';
}
function savePreset(target){
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
  state.customPresets[target][name] = domains;
  persistCustomPresets();
  renderPresetSelect(target);
  el(`${target}-preset-select`).value = `custom:${name}`;
  showToast('Пресет сохранен', 'good');
  if (target === 'common') refreshCandidates(true);
  else renderCandidates();
}
function deletePreset(target){
  const selected = el(`${target}-preset-select`).value || '';
  if (!selected.startsWith('custom:')) {
    showToast('Встроенные пресеты нельзя удалить', 'warn');
    return;
  }
  const name = selected.slice('custom:'.length);
  delete state.customPresets[target][name];
  persistCustomPresets();
  renderPresetSelect(target);
  showToast('Пресет удален', 'good');
  if (target === 'common') refreshCandidates(true);
}
function renderMetrics(){
  const status = state.status || {};
  const board = status.state || {};
  const zapret = status.zapret2 || {};
  const ready = Boolean(zapret.nfqws2_found && zapret.blockcheck_found);
  const rootReady = Boolean(zapret.root_helper_ready);
  const busy = isBusy();
  const jobStatus = board.current_job_status || (busy ? 'running' : '');
  const run = latestRun();
  setText('metric-zapret', ready ? 'Готов' : 'Не готов');
  setText('metric-zapret-note', `nfqws2: ${zapret.nfqws2_found ? 'да' : 'нет'}, blockcheck: ${zapret.blockcheck_found ? 'да' : 'нет'}, root-helper: ${rootReady ? 'да' : 'нет'}`);
  setText('metric-job', busy ? runStatusLabel(jobStatus) : 'Свободна');
  setText('metric-job-note', busy ? `${board.current_job_name || 'job'} · ID ${board.current_job}` : `Обновлено ${new Date().toLocaleTimeString('ru-RU')}`);
  const candidateMetric = state.candidateView === 'domain' ? state.candidateDomainStrategyTotal : (state.candidateTotal || state.candidates.length);
  const loadedMetric = state.candidateView === 'domain' ? `${state.candidateDomains.length} доменов` : `${state.candidates.length} стратегий`;
  setText('metric-candidates', String(candidateMetric));
  setText('metric-candidates-note', state.candidatesLoaded || state.candidateDomainsLoaded ? `загружено ${loadedMetric}` : 'открыть список');
  setText('metric-last-run', run ? (run.status || '-') : '-');
  setText('metric-last-run-note', run ? friendlyDate(run.timestamp) : 'запусков еще не было');
  const jobBadge = el('job-badge');
  jobBadge.textContent = busy ? 'В работе' : 'Свободна';
  jobBadge.className = busy ? 'badge warn' : 'badge good';
  document.querySelectorAll('button[data-action="standard-discovery"], button[data-action="multi-domain-discovery"]').forEach((button) => {
    button.disabled = busy;
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
  const loadedNote = loaded ? `Показано ${activeRows.length} из ${total}` : 'Список загружается по запросу';
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
      ${runField('Итог', runSummary(row))}
    </div>
    ${runDomains(row, domainKey)}
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
    stopping: 'стоп',
    stopped: 'стоп',
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
function isDiscoveryRun(row){
  return row.kind === 'standard-discovery' || row.kind === 'multi-domain-discovery';
}
function runMode(row){
  return row.kind === 'multi-domain-discovery' ? 'стратегия -> домены' : 'обычный';
}
function runSummary(row){
  const count = runCandidateCount(row);
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
  renderBackupRestorePanel(rows);
  const target = el('backups-table');
  if (!target) return;
  if (!rows.length) {
    target.innerHTML = `<div class="empty">${state.backupsLoaded ? 'Бекапов пока нет' : 'Откройте вкладку, чтобы загрузить бекапы'}</div>`;
    return;
  }
  target.innerHTML = rows.map((item) => backupCard(item)).join('');
}
function renderBackupRestorePanel(rows){
  const select = el('backup-restore-select');
  const button = document.querySelector('[data-action="restore-selected-backup"]');
  if (!select || !button) return;
  const previous = select.value;
  select.innerHTML = rows.length
    ? rows.map((item) => {
      const id = String(item.id || '');
      const label = `${id} · ${item.created_at || '-'}`;
      return `<option value="${esc(id)}">${esc(label)}</option>`;
    }).join('')
    : '<option value="">Бекапов нет</option>';
  if (rows.some((item) => String(item.id || '') === previous)) select.value = previous;
  button.disabled = !rows.length;
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
    <div class="backup-downloads">
      <div class="backup-download-block backup-archive">
        <div class="backup-section-title">Архив</div>
        <a class="backup-archive-link" href="${backupDownloadUrl(id, 'archive')}">Скачать архив</a>
      </div>
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
    const scriptParts = [`Файл ${progress.script_index || 0} из ${progress.script_total}`];
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
  const eta = etaMs ? `Время считается как оставшиеся попытки × ${etaMs} мс${parallelText}. ` : '';
  const fallback = Number(progress.summary_fallbacks || 0) > 0 ? `Fallback из summary: ${progress.summary_fallbacks}. ` : '';
  setText('progress-note', `${current}${total}${under}${eta}${fallback}Прогресс считается по live-логу blockcheck2.`);
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
  setText('settings-version', `v${(state.status || {}).version || '-'}`);
  const ipv6 = el('settings-enable-ipv6');
  const debugStdout = el('settings-debug-stdout');
  const curlDefault = el('settings-curl-default');
  const curlMax = el('settings-curl-max');
  const channel = el('settings-update-channel');
  if (ipv6) ipv6.checked = Boolean(settings.enable_ipv6);
  if (debugStdout) debugStdout.checked = Boolean(settings.debug_stdout);
  if (curlDefault) curlDefault.value = String(settings.curl_parallelism_default || 4);
  if (curlMax) curlMax.value = String(settings.curl_parallelism_max || 10);
  if (channel) channel.value = settings.update_channel || 'stable';
  const stableLink = el('settings-stable-link');
  const prereleaseLink = el('settings-prerelease-link');
  if (stableLink && settings.stable_release_url) stableLink.href = settings.stable_release_url;
  if (prereleaseLink && settings.prerelease_url) prereleaseLink.href = settings.prerelease_url;
  if (!state.settingsTouched) {
    const curlInput = el('curl-parallelism');
    if (curlInput) {
      curlInput.max = String(settings.curl_parallelism_max || 10);
      curlInput.value = String(settings.curl_parallelism_default || 4);
    }
    const finderIpv6 = el('enable-ipv6');
    if (finderIpv6) finderIpv6.checked = Boolean(settings.enable_ipv6);
  }
  renderDiscoveryProfiles();
  renderV2flyPreview();
}
function currentSettingsFromForm(){
  return {
    enable_ipv6: Boolean(el('settings-enable-ipv6')?.checked),
    debug_stdout: Boolean(el('settings-debug-stdout')?.checked),
    curl_parallelism_default: Number(el('settings-curl-default')?.value || 4),
    curl_parallelism_max: Number(el('settings-curl-max')?.value || 10),
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
function v2flyCategories(){
  return parseDomains(el('v2fly-categories')?.value || '');
}
function v2flyPayload(){
  return {
    scope: el('v2fly-scope')?.value || 'finder',
    name: String(el('v2fly-preset-name')?.value || '').trim(),
    categories: v2flyCategories()
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
  const added = Array.isArray(preview.added) ? preview.added.length : 0;
  const removed = Array.isArray(preview.removed) ? preview.removed.length : 0;
  const sources = Array.isArray(preview.sources) ? preview.sources.map((source) => `${source.category}: ${source.domains}`).join(', ') : '-';
  target.innerHTML = [
    `<div><strong>${esc(preview.preset || '-')}</strong>: ${esc(preview.count || 0)} доменов</div>`,
    `<div>Добавится: ${esc(added)}, уйдет: ${esc(removed)}, без изменений: ${esc(preview.unchanged_count || 0)}</div>`,
    `<div>Категории: ${esc(sources)}</div>`
  ].join('');
}
async function previewV2flyPreset(){
  const payload = v2flyPayload();
  if (!payload.name || !payload.categories.length) {
    setMessage('Укажите название пресета и хотя бы одну категорию v2fly', 'warn');
    return;
  }
  try {
    const data = await postJson('/api/domain-sources/v2fly/preview', payload);
    state.v2flyPreview = data;
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
  try {
    const data = await postJson('/api/domain-sources/v2fly/import', payload);
    state.v2flyPreview = data;
    mergeCustomPresets((data || {}).custom || {});
    renderPresetSelects();
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
  if (!state.domainsInitialized && !state.domainsTouched && !el('finder-domains').value.trim() && state.domainSets) {
    const domains = [...new Set(defaultDomains('critical'))];
    el('finder-domains').value = domains.join('\\n');
    state.domainsInitialized = true;
  }
  renderPresetSelects();
  renderMetrics();
  if (!opts.skipCandidates) renderCandidates();
  renderRuns();
  renderLog();
  renderBackups();
  renderSettings();
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
    renderCandidatesOnly();
  } catch (error) {
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
    if (queryKey.startsWith('common:')) storeCommonCandidateCache(queryKey);
    renderCandidatesOnly();
  } catch (error) {
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
    state.status = status;
    syncCandidateVersion(status.candidate_version || null);
    state.settings = (settings || {}).settings || status.settings || {};
    state.finderRuns = latestById(finderRuns.runs || []);
    state.finderLog = finderLog;
    state.domainSets = domainSets;
    state.discoveryProfiles = (discoveryProfiles || {}).profiles || {};
    state.domainSources = domainSources;
    mergeCustomPresets((presets || {}).custom || {});
    renderAll({ skipCandidates: true });
    if (state.activeTab === 'candidates') ensureCandidateViewLoaded();
  } catch (error) {
    setMessage(`Ошибка обновления: ${error.message}`, 'bad');
  } finally {
    refreshInFlight = false;
  }
}
async function refreshBackups(){
  try {
    const data = await getJson('/api/backups');
    state.backups = data.snapshots || [];
    state.backupsLoaded = true;
    renderBackups();
  } catch (error) {
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
  if (button.dataset.action === 'use-discovery-profile') {
    useSelectedDiscoveryProfile();
    return;
  }
  if (button.dataset.action === 'save-discovery-profile') {
    saveDiscoveryProfile();
    return;
  }
  if (button.dataset.action === 'delete-discovery-profile') {
    deleteDiscoveryProfile();
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
  if (button.dataset.action === 'restore-selected-backup') {
    restoreBackup(el('backup-restore-select')?.value || '');
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
  if (button.dataset.presetUse) {
    usePreset(button.dataset.presetUse);
    return;
  }
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
  if (button.dataset.action === 'standard-discovery') {
    const options = discoveryOptions();
    if (!hasEnabledProtocol(options)) {
      setMessage('Выберите хотя бы один протокол для проверки', 'bad');
      return;
    }
    const payload = {
      domains: finderDomains(),
      ...options
    };
    const timeout = timeoutSecondsOrNull();
    if (timeout !== null) payload.timeout_seconds = timeout;
    startJob('/api/jobs/zapret-standard-discovery', payload, 'Поиск стратегий');
  }
  if (button.dataset.action === 'multi-domain-discovery') {
    const options = discoveryOptions();
    if (!hasEnabledProtocol(options)) {
      setMessage('Выберите хотя бы один протокол для проверки', 'bad');
      return;
    }
    const payload = {
      domains: finderDomains(),
      ...options,
      curl_parallelism: curlParallelism()
    };
    const timeout = timeoutSecondsOrNull();
    if (timeout !== null) payload.timeout_seconds = timeout;
    startJob('/api/jobs/zapret-multi-domain-discovery', payload, 'Стратегия -> домены');
  }
  if (button.dataset.action === 'stop-current') stopCurrentJob();
});
document.addEventListener('input', (event) => {
  if (event.target && ['curl-parallelism', 'enable-ipv6'].includes(event.target.id)) {
    state.settingsTouched = true;
  }
  if (event.target && String(event.target.id || '').startsWith('settings-')) {
    state.settingsTouched = true;
  }
  if (event.target && String(event.target.id || '').startsWith('v2fly-')) {
    state.v2flyPreview = null;
    renderV2flyPreview();
  }
  if (event.target && event.target.id === 'finder-domains') {
    updateEditorLineNumbers('finder-domains');
    state.domainsTouched = true;
    if (state.candidateView === 'common') scheduleCandidateRefresh();
  }
  if (event.target && event.target.id === 'common-domains') {
    updateEditorLineNumbers('common-domains');
    scheduleCandidateRefresh();
    renderCommonDomainSuggestions();
  }
  if (event.target && event.target.id === 'common-domain-add') {
    renderCommonDomainSuggestions();
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
  if (event.target && String(event.target.id || '').startsWith('settings-')) {
    state.settingsTouched = true;
  }
  if (event.target && String(event.target.id || '').startsWith('v2fly-')) {
    state.v2flyPreview = null;
    renderV2flyPreview();
  }
  if (event.target && event.target.id === 'limit-time-enabled') {
    el('time-limit-field').hidden = !event.target.checked;
  }
  if (event.target && (event.target.id === 'finder-preset-select' || event.target.id === 'common-preset-select')) {
    const target = event.target.id.startsWith('finder') ? 'finder' : 'common';
    const value = event.target.value || '';
    const nameInput = el(`${target}-preset-name`);
    if (nameInput) nameInput.value = value.startsWith('custom:') ? value.slice('custom:'.length) : '';
  }
  if (event.target && event.target.id === 'discovery-profile-select') {
    const profile = (state.discoveryProfiles || {})[event.target.value];
    const nameInput = el('discovery-profile-name');
    if (nameInput) nameInput.value = profileTitle(event.target.value, profile);
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
setInterval(refresh, 5000);
</script>
</body></html>
"""


def status_payload(config: AppConfig) -> dict[str, Any]:
    return {
        "version": __version__,
        "state": read_state(config.output.state_dir),
        "settings": read_settings(config),
        "candidate_version": candidate_storage_version(config.output.state_dir),
        "paths": {
            "state_dir": str(config.output.state_dir),
        },
        "zapret2": check_install_cached(),
    }


DEFAULT_SETTINGS = {
    "curl_parallelism_default": 4,
    "curl_parallelism_max": 10,
    "enable_ipv6": False,
    "debug_stdout": False,
    "update_channel": "stable",
}


DEFAULT_DISCOVERY_PROFILES = {
    "balanced": {
        "name": "balanced",
        "title": "Balanced",
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
    "deep": {
        "name": "deep",
        "title": "Deep",
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


def _normalize_settings(raw: dict[str, Any]) -> dict[str, Any]:
    max_parallelism = _bounded_int(raw.get("curl_parallelism_max"), default=10, minimum=1, maximum=10)
    default_parallelism = _bounded_int(raw.get("curl_parallelism_default"), default=4, minimum=1, maximum=max_parallelism)
    channel = str(raw.get("update_channel") or "stable")
    if channel not in {"stable", "prerelease"}:
        channel = "stable"
    return {
        "curl_parallelism_default": default_parallelism,
        "curl_parallelism_max": max_parallelism,
        "enable_ipv6": bool(raw.get("enable_ipv6")),
        "debug_stdout": bool(raw.get("debug_stdout")),
        "update_channel": channel,
        "stable_release_url": "https://github.com/balbomush/GP-access-control-plane/releases/latest",
        "prerelease_url": "https://github.com/balbomush/GP-access-control-plane/releases",
    }


def read_discovery_profiles(config: AppConfig) -> dict[str, dict[str, Any]]:
    state = read_state(config.output.state_dir)
    stored = state.get("discovery_profiles") if isinstance(state.get("discovery_profiles"), dict) else {}
    merged: dict[str, dict[str, Any]] = {}
    for name, profile in DEFAULT_DISCOVERY_PROFILES.items():
        merged[name] = _normalize_discovery_profile(name, profile)
    for raw_name, raw_profile in stored.items():
        name = _profile_name(raw_name)
        if not name or not isinstance(raw_profile, dict):
            continue
        merged[name] = _normalize_discovery_profile(name, raw_profile)
    return dict(sorted(merged.items()))


def save_discovery_profiles(config: AppConfig, payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    profiles: dict[str, dict[str, Any]] = {}
    source = payload if isinstance(payload, dict) else {}
    for raw_name, raw_profile in source.items():
        name = _profile_name(raw_name)
        if not name or not isinstance(raw_profile, dict):
            continue
        if name in DEFAULT_DISCOVERY_PROFILES:
            continue
        profiles[name] = _normalize_discovery_profile(name, raw_profile)
    state = read_state(config.output.state_dir)
    state["discovery_profiles"] = profiles
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


def _v2fly_preview_payload(config: AppConfig, payload: dict[str, Any]) -> dict[str, Any]:
    return preview_v2fly_preset(
        config.output.state_dir,
        scope=str(payload.get("scope") or "finder"),
        name=str(payload.get("name") or ""),
        categories=_payload_string_list(payload, "categories"),
    )


def _v2fly_import_payload(config: AppConfig, payload: dict[str, Any]) -> dict[str, Any]:
    return import_v2fly_preset(
        config.output.state_dir,
        scope=str(payload.get("scope") or "finder"),
        name=str(payload.get("name") or ""),
        categories=_payload_string_list(payload, "categories"),
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
