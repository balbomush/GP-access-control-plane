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
            jobs: dict[str, Any] = {
                "/api/jobs/validate": ("validate", lambda: _job_validate(config)),
                "/api/jobs/sync-pull-only": ("sync-pull-only", lambda: _job_sync(config)),
                "/api/jobs/render-dry-run": ("render-dry-run", lambda: _job_render(config)),
                "/api/jobs/healthcheck-direct": ("healthcheck-direct", lambda: _job_healthcheck(config, payload)),
                "/api/jobs/zapret-strategy-check": (
                    "zapret-strategy-check",
                    lambda: _job_zapret_strategy_check(config, payload),
                ),
                "/api/jobs/zapret-standard-discovery": (
                    "zapret-standard-discovery",
                    lambda: _job_zapret_standard_discovery(config, payload),
                ),
                "/api/jobs/zapret-custom-verification": (
                    "zapret-custom-verification",
                    lambda: _job_zapret_custom_verification(config, payload),
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
<title>GP Control Plane</title>
<style>
:root {
  color-scheme: light;
  font-family: Inter, "Segoe UI", Arial, sans-serif;
  background: #eef1f4;
  color: #18212a;
  --surface: #ffffff;
  --surface-soft: #f8fafb;
  --line: #d8e0e7;
  --line-strong: #bdc9d4;
  --text-soft: #60707f;
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
  min-height: 96px;
  display: grid;
  align-content: space-between;
  gap: 8px;
}
.metric-label { color: var(--text-soft); font-size: 12px; text-transform: uppercase; }
.metric-value { font-size: 20px; font-weight: 700; line-height: 1.25; overflow-wrap: anywhere; }
.metric-note { color: var(--text-soft); font-size: 12px; overflow-wrap: anywhere; }
.layout {
  display: grid;
  grid-template-columns: minmax(300px, 420px) minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}
.stack { display: grid; gap: 16px; }
.panel {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 16px;
}
.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}
h2 { font-size: 16px; line-height: 1.3; margin: 0; letter-spacing: 0; }
.actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.form-grid { display: grid; gap: 10px; }
.field { display: grid; gap: 6px; min-width: 0; }
label { color: var(--text-soft); font-size: 12px; font-weight: 600; }
input, select {
  width: 100%;
  min-width: 0;
  min-height: 38px;
  border: 1px solid var(--line-strong);
  border-radius: 6px;
  padding: 0 10px;
  background: #ffffff;
  color: #18212a;
  font-size: 14px;
}
input:focus, select:focus {
  outline: 2px solid #b7cdf5;
  border-color: var(--blue);
}
button {
  min-height: 38px;
  border: 1px solid var(--blue);
  background: var(--blue);
  color: #ffffff;
  border-radius: 6px;
  padding: 0 12px;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
}
button:hover { background: var(--blue-strong); border-color: var(--blue-strong); }
button.secondary { background: #ffffff; color: var(--blue); }
button.secondary:hover { background: #edf4ff; }
button:disabled { opacity: .55; cursor: default; }
.button-row { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.message {
  min-height: 36px;
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 9px 10px;
  background: var(--surface-soft);
  color: var(--text-soft);
  font-size: 13px;
  margin-top: 10px;
}
.message.good { background: var(--green-soft); color: var(--green); border-color: #b8dfca; }
.message.warn { background: var(--amber-soft); color: var(--amber); border-color: #eed09a; }
.message.bad { background: var(--red-soft); color: var(--red); border-color: #f0b9b5; }
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
.table-wrap { overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 10px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
th { color: var(--text-soft); font-size: 12px; font-weight: 700; background: var(--surface-soft); }
tr:last-child td { border-bottom: 0; }
code {
  display: block;
  max-width: 420px;
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
.path-list { display: grid; gap: 8px; }
.path-item {
  display: grid;
  grid-template-columns: 96px minmax(0, 1fr);
  gap: 10px;
  font-size: 13px;
}
.path-key { color: var(--text-soft); }
.path-value {
  font-family: Consolas, "SFMono-Regular", monospace;
  overflow-wrap: anywhere;
  color: #24313c;
}
.raw {
  margin-top: 12px;
  border-top: 1px solid var(--line);
  padding-top: 12px;
}
summary { cursor: pointer; color: var(--text-soft); font-size: 13px; }
pre {
  margin: 10px 0 0;
  max-height: 360px;
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
  .status-grid, .actions, .button-row { grid-template-columns: 1fr; }
  h1 { font-size: 22px; }
  .metric-value { font-size: 18px; }
  button { width: 100%; }
  .path-item { grid-template-columns: 1fr; gap: 2px; }
}
</style>
</head>
<body>
<div class="shell">
  <header class="topbar">
    <div class="topbar-inner">
      <div class="brand">
        <h1>Панель доступа</h1>
        <div class="subtitle">Локальный control plane на Raspberry Pi</div>
      </div>
      <button class="secondary" data-action="refresh">Обновить</button>
    </div>
  </header>
  <main class="main">
    <section class="status-grid" aria-label="Сводка">
      <div class="metric">
        <div class="metric-label">Плата</div>
        <div class="metric-value" id="metric-board">Загрузка</div>
        <div class="metric-note" id="metric-board-note">-</div>
      </div>
      <div class="metric">
        <div class="metric-label">Правила</div>
        <div class="metric-value" id="metric-rules">-</div>
        <div class="metric-note" id="metric-rules-note">direct / zapret / vpn</div>
      </div>
      <div class="metric">
        <div class="metric-label">zapret2</div>
        <div class="metric-value" id="metric-zapret">-</div>
        <div class="metric-note" id="metric-zapret-note">nfqws2 и blockcheck</div>
      </div>
      <div class="metric">
        <div class="metric-label">Последняя ошибка</div>
        <div class="metric-value" id="metric-error">-</div>
        <div class="metric-note" id="metric-error-note">-</div>
      </div>
    </section>

    <div class="layout">
      <div class="stack">
        <section class="panel">
          <div class="panel-header">
            <h2>Управление</h2>
            <span class="badge" id="job-badge">Свободна</span>
          </div>
          <div class="actions">
            <button data-job="/api/jobs/validate">Проверить</button>
            <button data-job="/api/jobs/sync-pull-only">Синхронизировать</button>
            <button data-job="/api/jobs/render-dry-run">Собрать dry-run</button>
            <button data-action="healthcheck-default">Проверить доступ</button>
          </div>
          <div class="message" id="message">Готово</div>
        </section>

        <section class="panel">
          <div class="panel-header">
            <h2>Проверка домена</h2>
          </div>
          <div class="form-grid">
            <div class="field">
              <label for="domain">Домен</label>
              <input id="domain" placeholder="youtube.com" autocomplete="off">
            </div>
            <div class="field">
              <label for="strategy">Стратегия zapret</label>
              <select id="strategy"></select>
            </div>
            <div class="field">
              <label for="timeout">Таймаут, сек</label>
              <input id="timeout" type="number" min="5" max="300" step="5" value="60">
            </div>
            <div class="button-row">
              <button data-action="healthcheck-domain">Прямой доступ</button>
              <button data-action="strategy-check">Проверить стратегию</button>
            </div>
          </div>
        </section>

        <section class="panel">
          <div class="panel-header">
            <h2>Подбор стратегий</h2>
          </div>
          <div class="form-grid">
            <div class="field">
              <label for="finder-domains">Домены для подбора</label>
              <input id="finder-domains" placeholder="youtube.com googlevideo.com discord.com discordcdn.com" autocomplete="off">
            </div>
            <div class="field">
              <label for="finder-timeout">Таймаут поиска, сек</label>
              <input id="finder-timeout" type="number" min="300" max="43200" step="300" value="21600">
            </div>
            <div class="button-row">
              <button data-action="standard-discovery">Standard discovery</button>
              <button data-action="custom-verification">Custom verification</button>
            </div>
          </div>
        </section>

        <section class="panel">
          <div class="panel-header">
            <h2>Пути</h2>
          </div>
          <div class="path-list" id="paths"></div>
        </section>
      </div>

      <div class="stack">
        <section class="panel">
          <div class="panel-header">
            <h2>Найденные стратегии</h2>
            <span class="badge" id="candidates-count">0</span>
          </div>
          <div id="candidates-table"></div>
        </section>

        <section class="panel">
          <div class="panel-header">
            <h2>Запуски подбора</h2>
            <span class="badge" id="finder-runs-count">0</span>
          </div>
          <div id="finder-runs-table"></div>
        </section>

        <section class="panel">
          <div class="panel-header">
            <h2>Текущий лог подбора</h2>
            <span class="badge" id="finder-log-status">-</span>
          </div>
          <pre id="finder-log">Лога пока нет</pre>
        </section>

        <section class="panel">
          <div class="panel-header">
            <h2>Стратегии zapret</h2>
            <span class="badge" id="strategies-count">0</span>
          </div>
          <div id="strategies-table"></div>
        </section>

        <section class="panel">
          <div class="panel-header">
            <h2>Журнал заданий</h2>
            <span class="badge" id="jobs-count">0</span>
          </div>
          <div id="jobs-table"></div>
        </section>

        <section class="panel">
          <div class="panel-header">
            <h2>Проверки доступности</h2>
            <span class="badge" id="healthchecks-count">0</span>
          </div>
          <div id="healthchecks-table"></div>
        </section>

        <section class="panel raw">
          <details>
            <summary>Технические данные</summary>
            <pre id="raw">Loading...</pre>
          </details>
        </section>
      </div>
    </div>
  </main>
</div>
<script>
const state = { status: null, rules: [], strategies: [], jobs: [], healthchecks: [], candidates: [], finderRuns: [], finderLog: null, domainSets: null, selectedCandidateId: null };
const jobNames = {
  'validate': 'Проверка',
  'sync-pull-only': 'Синхронизация',
  'render-dry-run': 'Сборка dry-run',
  'healthcheck-direct': 'Прямой доступ',
  'zapret-strategy-check': 'Проверка стратегии',
  'zapret-standard-discovery': 'Поиск стратегий',
  'zapret-custom-verification': 'Проверка candidate'
};
const statusTone = { success: 'good', failed: 'bad', running: 'warn', queued: 'warn' };

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
function routeCounts(){
  const counts = { direct: 0, zapret: 0, vpn: 0 };
  state.rules.forEach((rule) => { if (counts[rule.route] !== undefined) counts[rule.route] += 1; });
  return counts;
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
function renderStatus(){
  const status = state.status || {};
  const board = status.state || {};
  const counts = routeCounts();
  const finderInput = el('finder-domains');
  if (finderInput && !finderInput.value && state.domainSets && Array.isArray(state.domainSets.critical)) {
    finderInput.value = state.domainSets.critical.join(' ');
  }
  const currentJob = Boolean(board.current_job);
  setText('metric-board', currentJob ? 'Занята' : 'Свободна');
  setText('metric-board-note', currentJob ? `Задание ${board.current_job}` : `Обновлено ${new Date().toLocaleTimeString('ru-RU')}`);
  setText('metric-rules', String(state.rules.length));
  setText('metric-rules-note', `${counts.direct} direct / ${counts.zapret} zapret / ${counts.vpn} vpn`);
  const zapret = status.zapret2 || {};
  const zapretReady = Boolean(zapret.nfqws2_found && zapret.blockcheck_found);
  setText('metric-zapret', zapretReady ? 'Готов' : 'Не найден');
  setText('metric-zapret-note', `nfqws2: ${zapret.nfqws2_found ? 'да' : 'нет'}, blockcheck: ${zapret.blockcheck_found ? 'да' : 'нет'}`);
  setText('metric-error', board.last_error ? 'Есть' : 'Нет');
  setText('metric-error-note', board.last_error || 'Ошибок не было');
  const jobBadge = el('job-badge');
  jobBadge.textContent = currentJob ? 'В работе' : 'Свободна';
  jobBadge.className = currentJob ? 'badge warn' : 'badge good';
  document.querySelectorAll('button[data-job], button[data-action="healthcheck-default"], button[data-action="healthcheck-domain"], button[data-action="strategy-check"], button[data-action="standard-discovery"], button[data-action="custom-verification"]').forEach((button) => {
    button.disabled = currentJob;
  });
  renderPaths(status);
  el('raw').textContent = JSON.stringify({status: state.status, rules: state.rules, strategies: state.strategies, candidates: state.candidates, finderRuns: state.finderRuns}, null, 2);
}
function renderPaths(status){
  const repos = status.repos || {};
  const paths = status.paths || {};
  const items = [
    ['rules', repos.rules],
    ['strategies', repos.strategies],
    ['rendered', paths.rendered_dir],
    ['evidence', paths.evidence_dir],
    ['state', paths.state_dir]
  ];
  el('paths').innerHTML = items.map(([key, value]) => (
    `<div class="path-item"><div class="path-key">${esc(key)}</div><div class="path-value" title="${esc(value)}">${esc(shortPath(value))}</div></div>`
  )).join('');
}
function renderStrategies(){
  setText('strategies-count', String(state.strategies.length));
  const select = el('strategy');
  const previous = select.value;
  select.innerHTML = state.strategies.map((item) => `<option value="${esc(item.path)}">${esc(item.id)} (${esc(item.status)})</option>`).join('');
  if (previous) select.value = previous;
  table('strategies-table', [
    {label: 'ID', render: (row) => esc(row.id)},
    {label: 'Статус', render: (row) => badge(row.status || 'unknown', row.status === 'stable' ? 'good' : 'warn')},
    {label: 'Путь', render: (row) => `<span title="${esc(row.path)}">${esc(shortPath(row.path))}</span>`}
  ], state.strategies, 'Стратегий пока нет');
}
function renderCandidates(){
  setText('candidates-count', String(state.candidates.length));
  table('candidates-table', [
    {label: 'ID', render: (row) => `<button class="secondary" data-candidate="${esc(row.id)}" title="Выбрать для custom verification">${esc(row.id === state.selectedCandidateId ? '✓ ' : '')}${esc(row.id)}</button>`},
    {label: 'Protocol', render: (row) => badge(row.protocol || '-', row.protocol === 'quic' ? 'warn' : 'good')},
    {label: 'Проверка', render: (row) => esc(candidateRate(row))},
    {label: 'Строка для копирования', render: (row) => `<code title="${esc(row.args)}">nfqws2 ${esc(row.args)}</code>`}
  ], state.candidates, 'Найденных candidate-стратегий пока нет');
}
function renderFinderRuns(){
  setText('finder-runs-count', String(state.finderRuns.length));
  table('finder-runs-table', [
    {label: 'Время', render: (row) => esc(friendlyDate(row.timestamp))},
    {label: 'Тип', render: (row) => esc(row.kind || '-')},
    {label: 'Статус', render: (row) => badge(row.status || '-', row.status === 'success' ? 'good' : row.status === 'running' ? 'warn' : 'bad')},
    {label: 'Домены', render: (row) => esc((row.domains || []).join(', '))},
    {label: 'Candidates', render: (row) => badge(String(row.candidate_count ?? 0), Number(row.candidate_count || 0) > 0 ? 'good' : 'warn')},
    {label: 'Лог', render: (row) => `<span title="${esc(row.stdout_log)}">${esc(shortPath(row.stdout_log))}</span>`}
  ], state.finderRuns.slice().reverse().slice(0, 10), 'Запусков подбора пока не было');
}
function renderFinderLog(){
  const log = state.finderLog || {};
  const status = log.status || '-';
  const badgeNode = el('finder-log-status');
  badgeNode.textContent = status;
  badgeNode.className = 'badge ' + (status === 'success' ? 'good' : status === 'running' ? 'warn' : status === '-' ? '' : 'bad');
  const parts = [];
  if (log.stdout_tail) parts.push(log.stdout_tail);
  if (log.stderr_tail) parts.push('--- stderr ---\n' + log.stderr_tail);
  el('finder-log').textContent = parts.join('\n\n') || 'Лога пока нет';
}
function candidateRate(row){
  const list = Array.isArray(row.verifications) ? row.verifications : [];
  if (!list.length) return 'не проверялась';
  const last = list[list.length - 1] || {};
  const rate = Math.round(Number(last.success_rate || 0) * 100);
  return `${rate}% (${last.success || 0}/${last.total || 0})`;
}
function renderJobs(){
  setText('jobs-count', String(state.jobs.length));
  table('jobs-table', [
    {label: 'Время', render: (row) => esc(friendlyDate(row.timestamp))},
    {label: 'Задание', render: (row) => esc(jobNames[row.name] || row.name || '-')},
    {label: 'Статус', render: (row) => badge(row.status || '-', statusTone[row.status] || '')},
    {label: 'Детали', render: (row) => esc(row.error || (row.result ? JSON.stringify(row.result) : '-'))}
  ], state.jobs.slice().reverse().slice(0, 12), 'Заданий пока не было');
}
function renderHealthchecks(){
  setText('healthchecks-count', String(state.healthchecks.length));
  table('healthchecks-table', [
    {label: 'Время', render: (row) => esc(friendlyDate(row.timestamp))},
    {label: 'Проверено', render: (row) => esc(row.checked ?? 0)},
    {label: 'Успешно', render: (row) => badge(String(row.success ?? 0), Number(row.success || 0) === Number(row.checked || 0) ? 'good' : 'warn')},
    {label: 'Отчет', render: (row) => `<span title="${esc(row.report)}">${esc(shortPath(row.report))}</span>`}
  ], state.healthchecks.slice().reverse().slice(0, 10), 'Проверок пока не было');
}
async function refresh(){
  try {
    const [status, rules, strategies, jobs, healthchecks, candidates, finderRuns, finderLog, domainSets] = await Promise.all([
      getJson('/api/status'),
      getJson('/api/rules'),
      getJson('/api/strategies'),
      getJson('/api/jobs'),
      getJson('/api/healthchecks'),
      getJson('/api/strategy-finder/candidates'),
      getJson('/api/strategy-finder/runs'),
      getJson('/api/strategy-finder/latest-log'),
      getJson('/api/strategy-finder/domains')
    ]);
    state.status = status;
    state.rules = rules.rules || [];
    state.strategies = strategies.strategies || [];
    state.jobs = jobs.jobs || [];
    state.healthchecks = healthchecks.healthchecks || [];
    state.candidates = candidates.candidates || [];
    state.finderRuns = finderRuns.runs || [];
    state.finderLog = finderLog;
    state.domainSets = domainSets;
    renderStatus();
    renderStrategies();
    renderCandidates();
    renderFinderRuns();
    renderFinderLog();
    renderJobs();
    renderHealthchecks();
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
function selectedDomain(){
  return el('domain').value.trim();
}
function finderDomains(){
  return el('finder-domains').value.split(/[,\\s]+/).map((item) => item.trim()).filter(Boolean);
}
document.addEventListener('click', (event) => {
  const button = event.target.closest('button');
  if (!button) return;
  if (button.dataset.candidate) {
    state.selectedCandidateId = button.dataset.candidate;
    renderCandidates();
    setMessage(`Candidate ${button.dataset.candidate} выбран для custom verification`, 'good');
    return;
  }
  if (button.dataset.action === 'refresh') refresh();
  if (button.dataset.job) startJob(button.dataset.job, {}, button.textContent.trim());
  if (button.dataset.action === 'healthcheck-default') startJob('/api/jobs/healthcheck-direct', {}, 'Проверка доступа');
  if (button.dataset.action === 'healthcheck-domain') {
    const domain = selectedDomain();
    startJob('/api/jobs/healthcheck-direct', domain ? {domains: [domain]} : {}, 'Проверка домена');
  }
  if (button.dataset.action === 'strategy-check') {
    const domain = selectedDomain();
    const strategy = el('strategy').value;
    if (!domain) {
      setMessage('Укажите домен', 'warn');
      return;
    }
    if (!strategy) {
      setMessage('Выберите стратегию', 'warn');
      return;
    }
    startJob('/api/jobs/zapret-strategy-check', {
      domain: domain,
      strategy_path: strategy,
      timeout_seconds: Number(el('timeout').value || 60)
    }, 'Проверка стратегии');
  }
  if (button.dataset.action === 'standard-discovery') {
    const domains = finderDomains();
    startJob('/api/jobs/zapret-standard-discovery', {
      domains: domains,
      include_quic: true,
      timeout_seconds: Number(el('finder-timeout').value || 900)
    }, 'Поиск стратегий');
  }
  if (button.dataset.action === 'custom-verification') {
    if (!state.selectedCandidateId) {
      setMessage('Выберите candidate в таблице найденных стратегий', 'warn');
      return;
    }
    startJob('/api/jobs/zapret-custom-verification', {
      candidate_id: state.selectedCandidateId,
      domains: finderDomains(),
      include_quic: true,
      timeout_seconds: Number(el('finder-timeout').value || 300)
    }, 'Custom verification');
  }
});
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


def _job_zapret_standard_discovery(config: AppConfig, payload: dict[str, Any]) -> dict[str, Any]:
    domains = _payload_domains(payload)
    return run_standard_discovery(
        domains,
        config.output.state_dir,
        timeout_seconds=int(payload.get("timeout_seconds") or 21600),
        include_quic=bool(payload.get("include_quic", True)),
    )


def _job_zapret_custom_verification(config: AppConfig, payload: dict[str, Any]) -> dict[str, Any]:
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
