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
button:disabled { opacity: .55; cursor: default; }
.button-row { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.fill-row { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
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
  .status-grid, .button-row, .fill-row { grid-template-columns: 1fr; }
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
      <div class="metric">
        <div class="metric-label">Кандидаты</div>
        <div class="metric-value" id="metric-candidates">0</div>
        <div class="metric-note" id="metric-candidates-note">найдено blockcheck2</div>
      </div>
      <div class="metric">
        <div class="metric-label">Последний запуск</div>
        <div class="metric-value" id="metric-last-run">-</div>
        <div class="metric-note" id="metric-last-run-note">-</div>
      </div>
    </section>

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
            <h2>Найденные стратегии</h2>
            <span class="badge" id="candidates-count">0</span>
          </div>
          <div id="candidates-table"></div>
        </section>

        <section class="panel">
          <div class="panel-header">
            <h2>История запусков</h2>
            <span class="badge" id="finder-runs-count">0</span>
          </div>
          <div id="finder-runs-table"></div>
        </section>

        <section class="panel">
          <div class="panel-header">
            <h2>Живой лог</h2>
            <span class="badge" id="finder-log-status">-</span>
          </div>
          <pre id="finder-log">Лога пока нет</pre>
        </section>
      </div>
    </div>
  </main>
</div>
<script>
const state = { status: null, jobs: [], candidates: [], finderRuns: [], finderLog: null, domainSets: null };
const finderJobs = new Set(['zapret-standard-discovery', 'zapret-custom-verification']);
const jobNames = {
  'zapret-standard-discovery': 'Поиск стратегий',
  'zapret-custom-verification': 'Проверка кандидата'
};
const statusTone = { success: 'good', failed: 'bad', running: 'warn', queued: 'warn', timeout: 'warn' };

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
  setText('metric-candidates-note', `${verifiedCount()} с повторной проверкой`);
  setText('metric-last-run', run ? (run.status || '-') : '-');
  setText('metric-last-run-note', run ? friendlyDate(run.timestamp) : 'запусков еще не было');
  const jobBadge = el('job-badge');
  jobBadge.textContent = busy ? 'В работе' : 'Свободна';
  jobBadge.className = busy ? 'badge warn' : 'badge good';
  document.querySelectorAll('button[data-action="standard-discovery"], button[data-candidate-verify]').forEach((button) => {
    button.disabled = busy;
  });
}
function verifiedCount(){
  return state.candidates.filter((item) => Array.isArray(item.verifications) && item.verifications.length > 0).length;
}
function candidateRate(row){
  const list = Array.isArray(row.verifications) ? row.verifications : [];
  if (!list.length) return 'не проверялась';
  const last = list[list.length - 1] || {};
  const rate = Math.round(Number(last.success_rate || 0) * 100);
  return `${rate}% (${last.success || 0}/${last.total || 0})`;
}
function renderCandidates(){
  setText('candidates-count', String(state.candidates.length));
  table('candidates-table', [
    {label: 'ID', render: (row) => esc(row.id || '-')},
    {label: 'Протокол', render: (row) => badge(row.protocol || '-', row.protocol === 'quic' ? 'warn' : 'good')},
    {label: 'Проверка', render: (row) => esc(candidateRate(row))},
    {label: 'Стратегия', render: (row) => `<code>nfqws2 ${esc(row.args || '')}</code>`},
    {label: 'Действие', render: (row) => `<button class="secondary" data-candidate-verify="${esc(row.id)}">Проверить</button>`}
  ], state.candidates, 'Кандидатов пока нет');
}
function renderRuns(){
  setText('finder-runs-count', String(state.finderRuns.length));
  table('finder-runs-table', [
    {label: 'Время', render: (row) => esc(friendlyDate(row.timestamp))},
    {label: 'Тип', render: (row) => esc(jobNames[row.kind] || row.kind || '-')},
    {label: 'Статус', render: (row) => badge(row.status || '-', statusTone[row.status] || '')},
    {label: 'Домены', render: (row) => esc((row.domains || []).join(', '))},
    {label: 'Кандидаты', render: (row) => badge(String(row.candidate_count ?? 0), Number(row.candidate_count || 0) > 0 ? 'good' : '')},
    {label: 'Лог', render: (row) => `<span title="${esc(row.stdout_log)}">${esc(shortPath(row.stdout_log))}</span>`}
  ], state.finderRuns.slice().reverse().slice(0, 12), 'Запусков пока не было');
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
  el('finder-log').textContent = parts.join('\\n\\n') || 'Лога пока нет';
}
function renderJobs(){
  const jobs = state.jobs.filter((job) => finderJobs.has(job.name)).slice().reverse().slice(0, 8);
  setText('jobs-count', String(jobs.length));
  table('jobs-table', [
    {label: 'Время', render: (row) => esc(friendlyDate(row.timestamp))},
    {label: 'Задание', render: (row) => esc(jobNames[row.name] || row.name || '-')},
    {label: 'Статус', render: (row) => badge(row.status || '-', statusTone[row.status] || '')},
    {label: 'Детали', render: (row) => esc(row.error || (row.result ? JSON.stringify(row.result) : '-'))}
  ], jobs, 'Заданий подбора пока не было');
}
function renderAll(){
  if (!el('finder-domains').value && state.domainSets) fillDomains('critical');
  renderMetrics();
  renderCandidates();
  renderRuns();
  renderLog();
  renderJobs();
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
    state.jobs = jobs.jobs || [];
    state.candidates = candidates.candidates || [];
    state.finderRuns = finderRuns.runs || [];
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
document.addEventListener('click', (event) => {
  const button = event.target.closest('button');
  if (!button) return;
  if (button.dataset.action === 'refresh') refresh();
  if (button.dataset.fill) fillDomains(button.dataset.fill);
  if (button.dataset.action === 'standard-discovery') {
    startJob('/api/jobs/zapret-standard-discovery', {
      domains: finderDomains(),
      include_quic: el('include-quic').checked,
      timeout_seconds: timeoutSeconds()
    }, 'Поиск стратегий');
  }
  if (button.dataset.candidateVerify) {
    startJob('/api/jobs/zapret-custom-verification', {
      candidate_id: button.dataset.candidateVerify,
      domains: finderDomains(),
      include_quic: el('include-quic').checked,
      timeout_seconds: timeoutSeconds()
    }, 'Проверка кандидата');
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
