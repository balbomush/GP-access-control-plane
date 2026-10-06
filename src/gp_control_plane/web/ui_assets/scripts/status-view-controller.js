/* status-view: explicit callbacks and a bounded view, no session/run owner. */
function createStatusViewController({ view, INITIAL_SYSTEM_STATUS_RETRY_DELAY_MS, INITIAL_SYSTEM_STATUS_RETRY_LIMIT, currentRun, el, esc, friendlyTime, interruptedRunWarning, isBusy, isStartRequestInFlight, mutatingBlockedMessage, refresh, runStatusLabel, setBadge, setMessage, setText, statusBadge , runState, syncCandidateVersion, renderLiveRun, renderSettings}) {
  const state = view;
  const lifetime = new UiLifetime();

  const setTimeout = lifetime.setTimeout.bind(lifetime);
  const clearTimeout = lifetime.clearTimeout.bind(lifetime);
  const requestAnimationFrame = lifetime.requestAnimationFrame.bind(lifetime);
  let initialSystemStatusRetryTimer = null;
  let initialSystemStatusRetryCount = 0;
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
  }).join('\n');
  return { ok, total, ready, tooltip };
}

function hasCompleteSystemStatus(status = state.status){
  return Boolean(status && typeof status === 'object' && status.zapret2 && typeof status.zapret2 === 'object');
}

function clearInitialSystemStatusRetry(){
  if (initialSystemStatusRetryTimer) clearTimeout(initialSystemStatusRetryTimer);
  initialSystemStatusRetryTimer = null;
  initialSystemStatusRetryCount = 0;
}

function scheduleInitialSystemStatusRetry(){
  if (hasCompleteSystemStatus() || initialSystemStatusRetryTimer || initialSystemStatusRetryCount >= INITIAL_SYSTEM_STATUS_RETRY_LIMIT) return;
  initialSystemStatusRetryCount += 1;
  initialSystemStatusRetryTimer = setTimeout(() => {
    initialSystemStatusRetryTimer = null;
    refresh({ silent: true });
  }, INITIAL_SYSTEM_STATUS_RETRY_DELAY_MS);
}

function initialSystemStatusState(){
  if (hasCompleteSystemStatus()) return 'known';
  if (state.statusLoading || initialSystemStatusRetryTimer) return 'pending';
  return 'unknown';
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
  const hasSystemStatus = hasCompleteSystemStatus();
  const systemStatusState = initialSystemStatusState();
  const status = state.status || {};
  const zapret = status.zapret2 || {};
  const zapretCompact = zapretCompactStatus(zapret);
  const ready = zapretCompact.ready;
  const busy = isBusy();
  const jobStatus = currentRun()?.status || (busy ? 'running' : '');
  const version = (state.status || {}).version || '-';
  const action = hasSystemStatus
    ? nextActionStatus(ready, busy, jobStatus, status)
    : systemStatusState === 'pending'
      ? { text: 'состояние системы', tone: 'pending' }
      : { text: 'повторите обновление', tone: 'unknown' };
  setText('app-version-badge', `v${version}`);
  const zapretValue = el('metric-zapret');
  if (zapretValue) {
    if (!hasSystemStatus) {
      const pending = systemStatusState === 'pending';
      zapretValue.innerHTML = `<span class="compact-status ${pending ? 'pending' : 'unknown'}"><span class="compact-status-mark">${pending ? '…' : '—'}</span><span>${pending ? 'Проверяем' : 'Нет статуса'}</span></span>`;
      zapretValue.title = pending ? 'Получаем состояние системы' : 'Не удалось получить состояние системы';
    } else {
      zapretValue.innerHTML = `<span class="compact-status ${ready ? 'ok' : 'bad'}"><span class="compact-status-mark">${ready ? '✓' : '!'}</span><span>${ready ? 'Готова' : 'Проблема'}</span></span>`;
      zapretValue.title = zapretCompact.tooltip;
    }
  }
  const zapretNote = el('metric-zapret-note');
  if (zapretNote) {
    zapretNote.textContent = !hasSystemStatus
      ? (systemStatusState === 'pending' ? 'получаем состояние' : 'обновите страницу')
      : (ready ? 'службы готовы' : 'проверьте систему');
    zapretNote.title = hasSystemStatus ? zapretCompact.tooltip : zapretValue?.title || '';
  }
  setText('metric-job', !hasSystemStatus ? 'Ожидание' : (busy ? runStatusLabel(jobStatus) : 'Свободно'));
  const jobCard = el('metric-job-card');
  if (jobCard) jobCard.className = hasSystemStatus ? jobStatusClass(jobStatus, busy) : 'metric metric-button';
  setText('metric-job-note', !hasSystemStatus ? 'проверяем систему' : metricJobNoteText(ready, busy, jobStatus, status));
  const testedCount = testedDomainCount();
  setText('metric-candidates', String(testedCount));
  setText('metric-candidates-note', state.candidateDomainsLoaded ? `загружено ${state.candidateDomains.length} доменов` : 'открыть список');
  const jobBadge = el('job-badge');
  setBadge(jobBadge, action.text, action.tone);
  const controlsBlocked = busy || !hasSystemStatus;
  const startControlsBlocked = controlsBlocked || isStartRequestInFlight();
  document.querySelectorAll('button[data-action="run-selected-discovery"]').forEach((button) => {
    button.disabled = controlsBlocked;
    if (startControlsBlocked) button.disabled = true;
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
    'button[data-action="v2fly-load-categories"]',
    'button[data-action="v2fly-update-local-storage"]'
  ].join(', ');
  document.querySelectorAll(mutatingSelectors).forEach((button) => {
    const v2flyCatalogAction = button.dataset.action === 'v2fly-load-categories' || button.dataset.action === 'v2fly-update-local-storage';
    button.disabled = (v2flyCatalogAction && state.v2flyCatalogUpdateLoading) || controlsBlocked;
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
      message: String(log.stderr_tail || '').trim().split('\n').slice(-1)[0]
    });
  }
  return rows;
}

function diagnosticsText(){
  const rows = eventRows();
  const log = state.finderLog || {};
  const parts = rows.map((row) => `[${row.severity}] ${row.title}: ${row.message}`);
  if (log.stderr_tail) parts.push(`stderr:
${log.stderr_tail}`);
  if ((state.status || {}).state?.last_error) parts.push(`last_error: ${(state.status || {}).state.last_error}`);
  return parts.join('\n\n') || 'Ошибок и предупреждений нет.';
}

async function copyDiagnostics(){
  const viewRequest=lifetime.capture(null);
  const text = diagnosticsText();
  try {
    await lifetime.result(navigator.clipboard.writeText(text), viewRequest);
    setMessage('Диагностика скопирована', 'good');
  } catch (error) {
    if(!lifetime.isCurrent(viewRequest)) return;
    setMessage(`Не удалось скопировать диагностику: ${error.message}`, 'bad');
  }
}

function renderEvents(){
  const target = el('events-panel');
  if (!target) return;
  const rows = eventRows();
  if (!rows.length) {
    target.innerHTML = `<article class="event-card l-stack">
      <div class="event-header">
        <div class="event-title">Ошибки и предупреждения</div>
        ${statusBadge('нет активных событий', 'good')}
      </div>
      <div class="event-meta">Текущий срез не содержит значимых ошибок.</div>
    </article>`;
    return;
  }
  target.innerHTML = rows.map((row) => {
    const tone = row.severity === 'error' ? 'bad' : 'warn';
    return `<article class="event-card l-stack ${tone}">
      <div class="event-header">
        <div class="event-title">${esc(row.title)}</div>
        ${statusBadge(row.severity === 'error' ? 'Ошибка' : 'Предупреждение', tone)}
      </div>
      <div>${esc(row.message || '-')}</div>
      <div class="event-meta">${esc(friendlyTime(row.time) || '-')} · ${esc(row.source || '-')}</div>
      <div class="event-actions l-cluster">
        <button class="secondary" data-action="repeat-last-run" type="button">Повторить</button>
        <button class="secondary" data-action="open-log" type="button">Открыть лог</button>
        <button class="secondary" data-action="copy-diagnostics" type="button">Скопировать диагностику</button>
      </div>
    </article>`;
  }).join('');
}
  function dispose(){ lifetime.dispose(); initialSystemStatusRetryTimer=null; }
function handleClickPart0(button){
if (button.dataset.action === 'copy-diagnostics') {
    copyDiagnostics();
    return true;
  }
 return false;
}
function mergeStatusPayload(status){
  if (!status) return false;
  const previousSettings = JSON.stringify(state.settings || {});
  runState.mergeStatus(status);
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
  return { mergeStatusPayload, handleClickPart0, statusCheck, zapretDiagnostics, zapretDiagnosticItems, zapretCompactStatus, hasCompleteSystemStatus, clearInitialSystemStatusRetry, scheduleInitialSystemStatusRetry, initialSystemStatusState, testedDomainCount, nextActionStatus, metricJobNoteText, jobStatusClass, renderMetrics, eventRows, diagnosticsText, copyDiagnostics: lifetime.action(copyDiagnostics), renderEvents, dispose };
}
