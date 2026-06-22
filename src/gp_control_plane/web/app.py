from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..config import AppConfig
from ..githubsync import pull_only
from ..healthcheck import check_domains_direct, write_report
from ..jobs import JobRunner
from ..render import render_dry_run
from ..rules import extract_hostlist, load_stable_rules
from ..state import append_jsonl, now_iso, read_jsonl, read_state, write_state
from ..strategy_finder import (
    domain_sets,
    find_candidate,
    latest_log_tail,
    read_candidates,
    read_runs,
    run_custom_verification,
    run_standard_discovery,
)
from ..strategies import list_local_strategies
from ..validation import validate_all
from ..zapret2 import check_install, run_check


def serve(config: AppConfig, host: str, port: int) -> None:
    runner = JobRunner(config.output.state_dir)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/":
                self._html()
            elif path == "/api/status":
                self._json(status_payload(config))
            elif path == "/api/rules":
                self._json({"rules": [rule.to_mapping() for rule in load_stable_rules(config.repos.rules)]})
            elif path == "/api/strategies":
                self._json({"strategies": strategy_payload(config)})
            elif path == "/api/jobs":
                self._json({"jobs": read_jsonl(config.output.state_dir / "jobs.jsonl")})
            elif path == "/api/healthchecks":
                self._json({"healthchecks": read_jsonl(config.output.state_dir / "healthchecks.jsonl")})
            elif path == "/api/strategy-finder/domains":
                self._json(domain_sets())
            elif path == "/api/strategy-finder/candidates":
                self._json({"candidates": read_candidates(config.output.state_dir)})
            elif path == "/api/strategy-finder/runs":
                self._json({"runs": read_runs(config.output.state_dir)})
            elif path == "/api/strategy-finder/latest-log":
                self._json(latest_log_tail(config.output.state_dir))
            else:
                self._not_found()

        def do_HEAD(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/":
                data = index_html().encode("utf-8")
                self._head(HTTPStatus.OK, "text/html; charset=utf-8", len(data))
            elif path in {
                "/api/status",
                "/api/rules",
                "/api/strategies",
                "/api/jobs",
                "/api/healthchecks",
                "/api/strategy-finder/domains",
                "/api/strategy-finder/candidates",
                "/api/strategy-finder/runs",
                "/api/strategy-finder/latest-log",
            }:
                self._head(HTTPStatus.OK, "application/json; charset=utf-8", 0)
            else:
                self._head(HTTPStatus.NOT_FOUND, "application/json; charset=utf-8", 0)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
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
            jobs: dict[str, Any] = {
                "/api/jobs/validate": ("validate", lambda _stop: _job_validate(config)),
                "/api/jobs/sync-pull-only": ("sync-pull-only", lambda _stop: _job_sync(config)),
                "/api/jobs/render-dry-run": ("render-dry-run", lambda _stop: _job_render(config)),
                "/api/jobs/healthcheck-direct": ("healthcheck-direct", lambda _stop: _job_healthcheck(config, payload)),
                "/api/jobs/zapret-strategy-check": (
                    "zapret-strategy-check",
                    lambda _stop: _job_zapret_strategy_check(config, payload),
                ),
                "/api/jobs/zapret-standard-discovery": (
                    "zapret-standard-discovery",
                    lambda stop: _job_zapret_standard_discovery(config, payload, stop),
                ),
                "/api/jobs/zapret-custom-verification": (
                    "zapret-custom-verification",
                    lambda stop: _job_zapret_custom_verification(config, payload, stop),
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
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _head(self, status: HTTPStatus, content_type: str, content_length: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
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
  color-scheme: light;
  font-family: Inter, "Segoe UI", Arial, sans-serif;
  background: #eef1f4;
  color: #17212b;
  --surface: #ffffff;
  --surface-soft: #f7f9fb;
  --line: #d8e0e7;
  --line-strong: #bcc9d5;
  --text-soft: #607282;
  --blue: #2166d1;
  --blue-strong: #174ea6;
  --green: #197a4a;
  --green-soft: #e8f5ee;
  --amber: #9a5b00;
  --amber-soft: #fff2d9;
  --red: #b42318;
  --red-soft: #fde8e7;
}
* { box-sizing: border-box; }
body { margin: 0; min-width: 320px; }
.shell { min-height: 100vh; }
.topbar { background: #ffffff; border-bottom: 1px solid var(--line); }
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
  grid-template-columns: minmax(0, 430px) minmax(0, 1fr);
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
input, textarea {
  width: 100%;
  min-width: 0;
  border: 1px solid var(--line-strong);
  border-radius: 6px;
  padding: 9px 10px;
  background: #ffffff;
  color: #17212b;
  font-size: 14px;
}
input { min-height: 38px; }
textarea { min-height: 118px; resize: vertical; line-height: 1.45; }
input:focus, textarea:focus {
  outline: 2px solid #b7cdf5;
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
  padding: 0 12px;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
button:hover { background: var(--blue-strong); border-color: var(--blue-strong); }
button.secondary { background: #ffffff; color: var(--blue); }
button.secondary:hover { background: #edf4ff; }
button.danger { border-color: var(--red); background: var(--red); color: #ffffff; }
button.danger:hover { border-color: #8f1d14; background: #8f1d14; }
button.secondary.danger { background: #ffffff; color: var(--red); }
button.secondary.danger:hover { background: var(--red-soft); }
button:disabled { opacity: .55; cursor: default; }
.button-row { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
.fill-row { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
.candidate-toolbar {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 10px;
  align-items: end;
  margin-bottom: 12px;
}
.candidate-summary {
  color: var(--text-soft);
  font-size: 13px;
  white-space: nowrap;
}
.candidate-tabs {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.subtab-button {
  min-height: 34px;
  background: #ffffff;
  color: var(--blue);
  border-color: var(--line-strong);
}
.subtab-button.active {
  background: var(--blue);
  color: #ffffff;
  border-color: var(--blue);
}
.copy-fallback {
  display: grid;
  gap: 8px;
  margin-bottom: 12px;
}
.copy-fallback[hidden] { display: none; }
.copy-fallback textarea {
  min-height: 160px;
  max-height: 260px;
}
.candidate-groups {
  display: grid;
  gap: 14px;
}
.domain-group {
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
  background: #ffffff;
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
}
.protocol-group:last-child { border-bottom: 0; }
.protocol-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.strategy-list {
  display: grid;
  gap: 8px;
}
.strategy-item {
  display: grid;
  gap: 4px;
  padding: 10px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #ffffff;
}
.strategy-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.strategy-index {
  color: var(--text-soft);
  font-size: 12px;
  font-weight: 700;
}
.tabs {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  border-bottom: 1px solid var(--line);
}
.tab-button {
  background: #ffffff;
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
  background: #ffffff;
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
  background: #ffffff;
  color: #17212b;
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
  color: #2a3744;
  overflow: hidden;
  text-overflow: ellipsis;
}
.badge.good { background: var(--green-soft); color: var(--green); border-color: #b8dfca; }
.badge.warn { background: var(--amber-soft); color: var(--amber); border-color: #eed09a; }
.badge.bad { background: var(--red-soft); color: var(--red); border-color: #f0b9b5; }
.table-wrap { overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; min-width: 0; max-width: 100%; }
table { width: 100%; border-collapse: collapse; font-size: 13px; table-layout: fixed; }
th, td { padding: 10px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; overflow-wrap: anywhere; }
th { color: var(--text-soft); font-size: 12px; font-weight: 700; background: var(--surface-soft); }
tr:last-child td { border-bottom: 0; }
code {
  display: block;
  max-width: 100%;
  font-family: Consolas, "SFMono-Regular", monospace;
  font-size: 12px;
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
  background: #111827;
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
  .status-grid, .button-row, .fill-row, .candidate-toolbar { grid-template-columns: 1fr; }
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
      <button class="secondary" data-action="refresh">Обновить</button>
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
              <textarea id="finder-domains" autocomplete="off" spellcheck="false"></textarea>
            </div>
            <div class="fill-row">
              <button class="secondary" data-fill="critical">Критичные</button>
              <button class="secondary" data-fill="coverage">Покрытие</button>
              <button class="secondary" data-fill="all">Все</button>
            </div>
            <div class="field">
              <label for="finder-timeout-hours">Лимит поиска, часов</label>
              <input id="finder-timeout-hours" type="number" min="0.1" max="24" step="0.5" value="6">
            </div>
            <label class="checkbox-row">
              <input id="include-quic" type="checkbox" checked>
              <span>Проверять QUIC/HTTP3</span>
            </label>
            <div class="button-row">
              <button data-action="standard-discovery">Запустить поиск</button>
              <button class="secondary" data-action="refresh">Обновить</button>
              <button class="secondary danger" data-action="stop-current" disabled>Остановить</button>
            </div>
            <div class="message" id="message">Готово</div>
          </div>
        </section>

        <section class="panel">
          <div class="panel-header">
            <h2>Задания подбора</h2>
            <span class="badge" id="jobs-count">0</span>
          </div>
          <div id="jobs-table"></div>
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
        <div class="candidate-toolbar">
          <div class="field">
            <label for="candidate-filter">Фильтр по домену, протоколу или аргументам</label>
            <input id="candidate-filter" autocomplete="off" placeholder="youtube.com, quic, multisplit">
          </div>
          <div class="candidate-summary" id="candidate-summary">-</div>
        </div>
        <div class="candidate-tabs" role="tablist" aria-label="Вид кандидатов">
          <button class="subtab-button active" data-candidate-view="domain" type="button">По доменам</button>
          <button class="subtab-button" data-candidate-view="common" type="button">Общие стратегии</button>
        </div>
        <div class="copy-fallback" id="copy-fallback" hidden>
          <label for="copy-fallback-text">Группа для ручного копирования</label>
          <textarea id="copy-fallback-text" readonly spellcheck="false"></textarea>
        </div>
        <div id="candidates-table"></div>
      </section>
      <section class="panel">
        <div class="panel-header">
          <h2>Запуски с находками</h2>
          <span class="badge" id="candidate-runs-count">0</span>
        </div>
        <div id="candidate-runs-table"></div>
      </section>
    </section>

    <section class="tab-page terminal-page" data-tab-page="terminal">
      <section class="panel terminal-panel">
        <div class="panel-header">
          <h2>Терминал</h2>
          <div class="terminal-actions">
            <span class="badge" id="finder-log-status">-</span>
            <button class="secondary danger" data-action="stop-current" disabled>Остановить</button>
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
              <div class="progress-label">Текущий файл</div>
              <div class="progress-value" id="progress-scripts">-</div>
            </div>
            <div class="progress-cell">
              <div class="progress-label">Осталось</div>
              <div class="progress-value" id="progress-eta">-</div>
            </div>
          </div>
          <div class="progress-note" id="progress-note">Прогресс оценочный: blockcheck2 не отдает общий счетчик стратегий до старта.</div>
        </div>
        <pre id="finder-log">Лога пока нет</pre>
      </section>
    </section>
  </main>
  <div class="toast" id="toast" role="status" aria-live="polite" hidden></div>
</div>
<script>
const state = { status: null, jobs: [], candidates: [], finderRuns: [], finderLog: null, domainSets: null, activeTab: 'finder', candidateView: 'domain', candidateFilter: '', candidateCopyGroups: {}, openCandidateDomains: {} };
const finderJobs = new Set(['zapret-standard-discovery', 'zapret-custom-verification']);
const jobNames = {
  'zapret-standard-discovery': 'Поиск стратегий',
  'zapret-custom-verification': 'Проверка кандидата',
  'standard-discovery': 'Поиск стратегий',
  'custom-verification': 'Проверка кандидата'
};
const statusTone = { success: 'good', failed: 'bad', running: 'warn', queued: 'warn', stopping: 'warn', stopped: 'warn', timeout: 'warn' };
let toastTimer = null;

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
function setActiveTab(tabName){
  state.activeTab = tabName;
  document.querySelectorAll('.tab-button[data-tab]').forEach((button) => {
    const active = button.dataset.tab === tabName;
    button.classList.toggle('active', active);
  });
  document.querySelectorAll('[data-tab-page]').forEach((page) => {
    page.classList.toggle('active', page.dataset.tabPage === tabName);
  });
  if (tabName === 'terminal') scrollLogToBottom();
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
    return [...(sets.critical || []), ...(sets.coverage || []), ...(sets.diagnostic || [])];
  }
  return sets[kind] || [];
}
function fillDomains(kind){
  const domains = [...new Set(defaultDomains(kind))];
  el('finder-domains').value = domains.join('\\n');
}
function finderDomains(){
  const raw = el('finder-domains').value.trim();
  if (!raw) return defaultDomains('critical');
  return raw.split(/[,\\s]+/).map((item) => item.trim()).filter(Boolean);
}
function timeoutSeconds(){
  const hours = Number(el('finder-timeout-hours').value || 6);
  return Math.max(60, Math.round(hours * 3600));
}
function renderMetrics(){
  const status = state.status || {};
  const board = status.state || {};
  const zapret = status.zapret2 || {};
  const ready = Boolean(zapret.nfqws2_found && zapret.blockcheck_found);
  const busy = isBusy();
  const run = latestRun();
  setText('metric-zapret', ready ? 'Готов' : 'Не готов');
  setText('metric-zapret-note', `nfqws2: ${zapret.nfqws2_found ? 'да' : 'нет'}, blockcheck: ${zapret.blockcheck_found ? 'да' : 'нет'}`);
  setText('metric-job', busy ? 'В работе' : 'Свободна');
  setText('metric-job-note', busy ? `ID ${board.current_job}` : `Обновлено ${new Date().toLocaleTimeString('ru-RU')}`);
  setText('metric-candidates', String(state.candidates.length));
  setText('metric-candidates-note', 'перейти к списку');
  setText('metric-last-run', run ? (run.status || '-') : '-');
  setText('metric-last-run-note', run ? friendlyDate(run.timestamp) : 'запусков еще не было');
  const jobBadge = el('job-badge');
  jobBadge.textContent = busy ? 'В работе' : 'Свободна';
  jobBadge.className = busy ? 'badge warn' : 'badge good';
  document.querySelectorAll('button[data-action="standard-discovery"]').forEach((button) => {
    button.disabled = busy;
  });
  document.querySelectorAll('button[data-action="stop-current"]').forEach((button) => {
    button.disabled = !busy;
  });
}
function renderCandidates(){
  const rows = filteredCandidates();
  const domainRows = rows.filter((row) => candidateDomains(row).length);
  const commonRows = rows.filter((row) => commonSeen(row).length);
  const activeRows = state.candidateView === 'common' ? commonRows : domainRows;
  setText('candidates-count', String(state.candidates.length));
  setText('candidate-summary', `Показано ${activeRows.length} из ${state.candidates.length}`);
  document.querySelectorAll('[data-candidate-view]').forEach((button) => {
    button.classList.toggle('active', button.dataset.candidateView === state.candidateView);
  });
  state.candidateCopyGroups = {};
  if (state.candidateView === 'common') {
    renderCommonCandidates(commonRows);
  } else {
    renderDomainCandidates(domainRows);
  }
  renderCandidateRuns();
}
function renderDomainCandidates(rows){
  const groups = candidateGroups(rows);
  if (!groups.length) {
    el('candidates-table').innerHTML = `<div class="empty">${state.candidates.length ? 'По фильтру ничего не найдено' : 'Кандидатов по доменам пока нет'}</div>`;
    return;
  }
  el('candidates-table').innerHTML = `<div class="candidate-groups">${groups.map((domainGroup) => {
    const total = domainGroup.protocols.reduce((sum, item) => sum + item.rows.length, 0);
    const open = state.candidateFilter || state.openCandidateDomains[domainGroup.domain] ? ' open' : '';
    return `<details class="domain-group" data-domain="${esc(domainGroup.domain)}"${open}>
      <summary class="domain-header">
        <div class="domain-title">${esc(domainGroup.domain)}</div>
        <div class="domain-meta">${badge(`${total} стратегий`, '')}</div>
      </summary>
      ${domainGroup.protocols.map((protocolGroup) => {
        const copyKey = registerCopyText(protocolGroup.rows.map((row) => row.args || '').filter(Boolean).join('\\n'));
        return `<div class="protocol-group">
          <div class="protocol-header">
            <div>${badge(protocolGroup.protocol, protocolGroup.protocol === 'quic' ? 'warn' : 'good')} ${badge(`${protocolGroup.rows.length} стратегий`, '')}</div>
            <button class="secondary" data-copy-candidate-group="${copyKey}" type="button">Копировать группу</button>
          </div>
          <div class="strategy-list">
            ${protocolGroup.rows.map((row, index) => strategyItem(row, index)).join('')}
          </div>
        </div>`;
      }).join('')}
    </details>`;
  }).join('')}</div>`;
}
function renderCommonCandidates(rows){
  const groups = protocolGroups(rows);
  if (!groups.length) {
    el('candidates-table').innerHTML = `<div class="empty">${state.candidates.length ? 'Общих стратегий пока нет. Они появятся, если blockcheck2 напечатает блок COMMON.' : 'Кандидатов пока нет'}</div>`;
    return;
  }
  el('candidates-table').innerHTML = `<div class="candidate-groups">${groups.map((protocolGroup) => {
    const copyKey = registerCopyText(protocolGroup.rows.map((row) => row.args || '').filter(Boolean).join('\\n'));
    const domains = [...new Set(protocolGroup.rows.flatMap((row) => commonDomains(row)))];
    return `<details class="domain-group" open>
      <summary class="domain-header">
        <div class="domain-title">${esc(protocolGroup.protocol)}</div>
        <div class="domain-meta">${badge(`${protocolGroup.rows.length} стратегий`, '')}${domains.length ? badge(`${domains.length} доменов`, 'good') : ''}</div>
      </summary>
      <div class="protocol-group">
        <div class="protocol-header">
          <div>${badge('COMMON', 'good')} ${domains.length ? esc(domains.join(', ')) : 'домены из запуска blockcheck2'}</div>
          <button class="secondary" data-copy-candidate-group="${copyKey}" type="button">Копировать группу</button>
        </div>
        <div class="strategy-list">
          ${protocolGroup.rows.map((row, index) => strategyItem(row, index)).join('')}
        </div>
      </div>
    </details>`;
  }).join('')}</div>`;
}
function filteredCandidates(){
  const query = state.candidateFilter.trim().toLowerCase();
  if (!query) return state.candidates;
  return state.candidates.filter((row) => {
    const haystack = [
      row.id,
      row.protocol,
      row.args,
      ...candidateDomains(row),
      ...commonDomains(row)
    ].join(' ').toLowerCase();
    return haystack.includes(query);
  });
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
function registerCopyText(text){
  const key = `copy-${Object.keys(state.candidateCopyGroups).length}`;
  state.candidateCopyGroups[key] = text;
  return key;
}
function strategyItem(row, index){
  const copyKey = registerCopyText(row.args || '');
  return `<div class="strategy-item">
    <div class="strategy-header">
      <div class="strategy-index">Стратегия ${index + 1}</div>
      <button class="secondary" data-copy-candidate-strategy="${copyKey}" type="button">Копировать</button>
    </div>
    <code>${esc(row.args || '')}</code>
  </div>`;
}
function renderCandidateRuns(){
  const rows = state.finderRuns
    .filter((row) => Number(row.candidate_count || 0) > 0 || Number(row.common_candidate_count || 0) > 0)
    .slice()
    .reverse()
    .slice(0, 12);
  setText('candidate-runs-count', String(rows.length));
  table('candidate-runs-table', [
    {label: 'Время', render: (row) => esc(friendlyDate(row.timestamp))},
    {label: 'Статус', render: (row) => badge(row.status || '-', statusTone[row.status] || '')},
    {label: 'Домены', render: (row) => esc((row.domains || []).join(', '))},
    {label: 'Стратегии', render: (row) => badge(String(Number(row.candidate_count || 0) + Number(row.common_candidate_count || 0)), 'good')},
    {label: 'Итог', render: (row) => esc(runSummary(row))}
  ], rows, 'Запусков с найденными стратегиями пока нет');
}
async function copyText(text){
  if (!text) return false;
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return true;
  }
  const area = document.createElement('textarea');
  area.value = text;
  area.readOnly = true;
  area.style.position = 'fixed';
  area.style.top = '0';
  area.style.left = '0';
  area.style.width = '1px';
  area.style.height = '1px';
  area.style.opacity = '0';
  document.body.appendChild(area);
  area.focus({preventScroll: true});
  area.select();
  area.setSelectionRange(0, area.value.length);
  const ok = document.execCommand('copy');
  document.body.removeChild(area);
  return ok;
}
function showCopyFallback(text){
  const panel = el('copy-fallback');
  const field = el('copy-fallback-text');
  field.value = text;
  panel.hidden = false;
  field.focus();
  field.select();
}
function hideCopyFallback(){
  const panel = el('copy-fallback');
  if (panel) panel.hidden = true;
}
function renderRuns(){
  setText('finder-runs-count', String(state.finderRuns.length));
  table('finder-runs-table', [
    {label: 'Время', render: (row) => esc(friendlyDate(row.timestamp))},
    {label: 'Тип', render: (row) => esc(jobNames[row.kind] || row.kind || '-')},
    {label: 'Статус', render: (row) => badge(row.status || '-', statusTone[row.status] || '')},
    {label: 'Домены', render: (row) => esc((row.domains || []).join(', '))},
    {label: 'Кандидаты', render: (row) => badge(String(row.candidate_count ?? 0), Number(row.candidate_count || 0) > 0 ? 'good' : '')},
    {label: 'Итог', render: (row) => esc(runSummary(row))}
  ], state.finderRuns.slice().reverse().slice(0, 12), 'Запусков пока не было');
}
function runSummary(row){
  const count = Number(row.candidate_count || 0);
  if (row.status === 'running') return 'идет поиск';
  if (row.status === 'timeout') return `остановлено по лимиту, найдено: ${count}`;
  if (row.status === 'stopped') return count > 0 ? `остановлено, сохранено: ${count}` : 'остановлено, кандидатов нет';
  if (row.status === 'success') return count > 0 ? `найдено: ${count}` : 'завершено, кандидатов нет';
  if (row.status === 'failed') return `ошибка, код: ${row.returncode ?? '-'}`;
  return count > 0 ? `найдено: ${count}` : '-';
}
function jobSummary(row){
  if (row.status === 'queued') return 'ожидает запуска';
  if (row.status === 'running') return 'выполняется';
  if (row.status === 'failed') return conciseError(row.error);
  const result = row.result || {};
  if (result.status) return runSummary(result);
  if (row.status === 'success') return 'завершено';
  return '-';
}
function effectiveJobStatus(row){
  const result = row.result || {};
  return result.status || row.status || '-';
}
function conciseError(value){
  const text = String(value || '').replace(/\\s+/g, ' ').trim();
  if (!text) return 'ошибка без деталей';
  if (text.includes('timed out')) return 'остановлено по таймауту';
  return text.length > 120 ? `${text.slice(0, 117)}...` : text;
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
  scrollLogToBottom();
}
function renderProgress(progress){
  const percent = Number(progress.percent || 0);
  const safePercent = Math.max(0, Math.min(100, Number.isFinite(percent) ? percent : 0));
  el('progress-fill').style.width = `${safePercent}%`;
  const attempted = Number(progress.attempted ?? 0);
  const attemptTotal = Number(progress.attempt_total ?? 0);
  setText('progress-attempted', attemptTotal ? `${attempted} / ${attemptTotal}` : String(progress.attempted ?? 0));
  setText('progress-successful', String(progress.successful ?? 0));
  if (progress.script_total) {
    const scriptParts = [`Файл ${progress.script_index || 0} из ${progress.script_total}`];
    if (progress.current_script_attempt_total) {
      scriptParts.push(`попыток в файле: ${progress.current_script_attempted || 0} из ${progress.current_script_attempt_total}`);
    }
    setText('progress-scripts', scriptParts.join(', '));
  } else {
    setText('progress-scripts', '-');
  }
  setText('progress-eta', progress.eta_seconds == null ? '-' : formatDuration(Number(progress.eta_seconds)));
  const current = progress.current_script ? `Текущий файл: ${progress.current_script}. ` : '';
  const total = attemptTotal ? `Всего попыток рассчитано по файлам zapret2: ${attemptTotal}. ` : '';
  const eta = progress.eta_estimate_ms_per_attempt ? `Время считается как оставшиеся попытки × ${progress.eta_estimate_ms_per_attempt} мс. ` : '';
  setText('progress-note', `${current}${total}${eta}Прогресс считается по live-логу blockcheck2.`);
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
function renderJobs(){
  const jobs = state.jobs.filter((job) => finderJobs.has(job.name)).slice().reverse().slice(0, 8);
  setText('jobs-count', String(jobs.length));
  table('jobs-table', [
    {label: 'Время', render: (row) => esc(friendlyDate(row.timestamp))},
    {label: 'Задание', render: (row) => esc(jobNames[row.name] || row.name || '-')},
    {label: 'Статус', render: (row) => {
      const status = effectiveJobStatus(row);
      return badge(status, statusTone[status] || '');
    }},
    {label: 'Итог', render: (row) => esc(jobSummary(row))}
  ], jobs, 'Заданий подбора пока не было');
}
function renderAll(){
  if (!el('finder-domains').value && state.domainSets) fillDomains('critical');
  renderMetrics();
  renderCandidates();
  renderRuns();
  renderLog();
  renderJobs();
  setActiveTab(state.activeTab);
}
async function refresh(){
  try {
    const [status, jobs, candidates, finderRuns, finderLog, domainSets] = await Promise.all([
      getJson('/api/status'),
      getJson('/api/jobs'),
      getJson('/api/strategy-finder/candidates'),
      getJson('/api/strategy-finder/runs'),
      getJson('/api/strategy-finder/latest-log'),
      getJson('/api/strategy-finder/domains')
    ]);
    state.status = status;
    state.jobs = latestById(jobs.jobs || []);
    state.candidates = candidates.candidates || [];
    state.finderRuns = latestById(finderRuns.runs || []);
    state.finderLog = finderLog;
    state.domainSets = domainSets;
    renderAll();
  } catch (error) {
    setMessage(`Ошибка обновления: ${error.message}`, 'bad');
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
    setMessage('Остановка подбора запрошена', 'warn');
    await postJson('/api/jobs/stop-current', {});
    await refresh();
  } catch (error) {
    setMessage(error.message, 'bad');
    await refresh();
  }
}
document.addEventListener('click', (event) => {
  const button = event.target.closest('button');
  if (!button) return;
  if (button.dataset.tab) setActiveTab(button.dataset.tab);
  if (button.dataset.candidateView) {
    state.candidateView = button.dataset.candidateView;
    renderCandidates();
    return;
  }
  if (button.dataset.action === 'refresh') refresh();
  if (button.dataset.fill) fillDomains(button.dataset.fill);
  if (button.dataset.copyCandidateGroup || button.dataset.copyCandidateStrategy) {
    const copyKey = button.dataset.copyCandidateGroup || button.dataset.copyCandidateStrategy;
    const groupText = state.candidateCopyGroups[copyKey] || '';
    const single = Boolean(button.dataset.copyCandidateStrategy);
    copyText(groupText).then((ok) => {
      if (ok) {
        hideCopyFallback();
        showToast(single ? 'Стратегия скопирована' : 'Группа стратегий скопирована', 'good');
      } else {
        showCopyFallback(groupText);
        showToast('Не удалось скопировать автоматически. Текст выделен на странице, нажмите Ctrl+C.', 'warn');
      }
    }).catch((error) => showToast(`Не удалось скопировать: ${error.message}`, 'bad'));
    return;
  }
  if (button.dataset.action === 'standard-discovery') {
    startJob('/api/jobs/zapret-standard-discovery', {
      domains: finderDomains(),
      include_quic: el('include-quic').checked,
      timeout_seconds: timeoutSeconds()
    }, 'Поиск стратегий');
  }
  if (button.dataset.action === 'stop-current') stopCurrentJob();
});
document.addEventListener('input', (event) => {
  if (event.target && event.target.id === 'candidate-filter') {
    state.candidateFilter = event.target.value;
    renderCandidates();
  }
});
document.addEventListener('toggle', (event) => {
  const details = event.target;
  if (!details || !details.matches || !details.matches('details.domain-group[data-domain]')) return;
  state.openCandidateDomains[details.dataset.domain] = details.open;
}, true);
refresh();
setInterval(refresh, 5000);
</script>
</body></html>
"""


def status_payload(config: AppConfig) -> dict[str, Any]:
    return {
        "state": read_state(config.output.state_dir),
        "repos": {
            "rules": str(config.repos.rules),
            "strategies": str(config.repos.strategies),
        },
        "paths": {
            "rendered_dir": str(config.output.rendered_dir),
            "evidence_dir": str(config.output.evidence_dir),
            "state_dir": str(config.output.state_dir),
        },
        "zapret2": check_install(),
    }


def strategy_payload(config: AppConfig) -> list[dict[str, str]]:
    items = []
    for path in list_local_strategies(config.repos.strategies):
        metadata_path = path / "metadata.yaml"
        strategy_id = path.name
        status = "unknown"
        if metadata_path.exists():
            for line in metadata_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("id:"):
                    strategy_id = line.split(":", 1)[1].strip()
                elif line.startswith("status:"):
                    status = line.split(":", 1)[1].strip()
        items.append({"id": strategy_id, "status": status, "path": str(path)})
    return items


def _job_validate(config: AppConfig) -> dict[str, Any]:
    errors = validate_all(config)
    state = read_state(config.output.state_dir)
    state["last_validate_at"] = now_iso()
    state["last_error"] = "; ".join(errors) if errors else None
    write_state(config.output.state_dir, state)
    if errors:
        raise RuntimeError("; ".join(errors))
    return {"errors": []}


def _job_sync(config: AppConfig) -> dict[str, Any]:
    pull_only([config.repos.rules, config.repos.strategies])
    state = read_state(config.output.state_dir)
    state["last_sync_at"] = now_iso()
    write_state(config.output.state_dir, state)
    return {"synced": True}


def _job_render(config: AppConfig) -> dict[str, Any]:
    manifest = render_dry_run(config)
    state = read_state(config.output.state_dir)
    state["last_render_at"] = now_iso()
    state["selected_strategy"] = manifest.get("selected_strategy")
    write_state(config.output.state_dir, state)
    return manifest


def _job_healthcheck(config: AppConfig, payload: dict[str, Any]) -> dict[str, Any]:
    raw_domains = payload.get("domains") or []
    if isinstance(raw_domains, str):
        raw_domains = [raw_domains]
    domains = [str(domain).strip() for domain in raw_domains if str(domain).strip()]
    if not domains:
        domains = [entry for entry in extract_hostlist(load_stable_rules(config.repos.rules)) if not entry.startswith("#")]
    results = check_domains_direct(domains, timeout_seconds=config.healthcheck.timeout_seconds)
    report = config.output.state_dir / "healthchecks" / f"{now_iso().replace(':', '')}.yaml"
    write_report(report, results)
    append_jsonl(
        config.output.state_dir / "healthchecks.jsonl",
        {
            "timestamp": now_iso(),
            "report": str(report),
            "checked": len(results),
            "success": sum(1 for result in results if result.ok),
        },
    )
    return {"report": str(report), "checked": len(results)}


def _job_zapret_standard_discovery(config: AppConfig, payload: dict[str, Any], stop_event: Any) -> dict[str, Any]:
    domains = _payload_domains(payload)
    return run_standard_discovery(
        domains,
        config.output.state_dir,
        timeout_seconds=int(payload.get("timeout_seconds") or 21600),
        include_quic=bool(payload.get("include_quic", True)),
        stop_event=stop_event,
    )


def _job_zapret_custom_verification(config: AppConfig, payload: dict[str, Any], stop_event: Any) -> dict[str, Any]:
    candidate_id = str(payload.get("candidate_id") or "").strip()
    if not candidate_id:
        raise ValueError("candidate_id is required")
    candidate = find_candidate(config.output.state_dir, candidate_id)
    domains = _payload_domains(payload)
    return run_custom_verification(
        candidate,
        domains,
        config.output.state_dir,
        timeout_seconds=int(payload.get("timeout_seconds") or 3600),
        include_quic=bool(payload.get("include_quic", True)),
        stop_event=stop_event,
    )


def _job_zapret_strategy_check(config: AppConfig, payload: dict[str, Any]) -> dict[str, Any]:
    domain = str(payload.get("domain") or "").strip()
    strategy_path = str(payload.get("strategy_path") or "").strip()
    if not domain:
        raise ValueError("domain is required")
    if not strategy_path:
        raise ValueError("strategy_path is required")
    result = run_check(domain, Path(strategy_path), timeout_seconds=int(payload.get("timeout_seconds") or 60))
    out_dir = config.output.state_dir / "zapret-checks"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_iso().replace(":", "")
    (out_dir / f"{stamp}.stdout.log").write_text(result.stdout, encoding="utf-8")
    (out_dir / f"{stamp}.stderr.log").write_text(result.stderr, encoding="utf-8")
    return {
        "domain": domain,
        "strategy_path": strategy_path,
        "returncode": result.returncode,
        "stdout_log": str(out_dir / f"{stamp}.stdout.log"),
        "stderr_log": str(out_dir / f"{stamp}.stderr.log"),
    }


def _payload_domains(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("domains") or []
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    if not isinstance(raw, list):
        raw = []
    return [str(domain).strip() for domain in raw if str(domain).strip()]
