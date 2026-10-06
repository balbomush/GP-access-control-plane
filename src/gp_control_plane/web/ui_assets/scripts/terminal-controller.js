/* terminal: explicit callbacks and a bounded view, no session/run owner. */
function createTerminalController({ view, logDirty, apiEndpoint, apiUrl, currentRun, currentSessionEpoch, el, esc, formatDuration, getJson: requestJson, isBusy, latestRun, phaseLabel, renderEvents, renderMetrics, renderRunSettingsSummary, runIdForRow, runState, runStatusLabel, sessionIsCurrent, setBadge, setMessage, setText, statusBadge, statusTone }) {
  const state = view;
  const lifetime = new UiLifetime();
  const getJson=(url,options)=>requestJson(url,lifetime.requestOptions(options));

  const setTimeout = lifetime.setTimeout.bind(lifetime);
  const clearTimeout = lifetime.clearTimeout.bind(lifetime);
  const requestAnimationFrame = lifetime.requestAnimationFrame.bind(lifetime);

function renderLog(){
  const log = state.finderLog || {};
  const acknowledged = state.acknowledgedRun;
  const status = log.status || (acknowledged ? acknowledged.status : '-');
  const badgeNode = el('finder-log-status');
  setBadge(badgeNode, status, statusTone[status] || '');
  const parts = [];
  if (log.stdout_tail) parts.push(log.stdout_tail);
  if (log.stderr_tail) parts.push('--- stderr ---\n' + log.stderr_tail);
  const logNode = el('finder-log');
  logNode.textContent = parts.join('\n\n') || (acknowledged ? 'Запуск подтверждён, ожидаем вывод' : 'Лога пока нет');
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
    return `<div class="stderr-diagnostic l-stack ${severity}">
      <div class="stderr-diagnostic-title">${esc(item.label || item.status || 'Диагностика stderr')}</div>
      <div>${esc(item.message || '')}</div>
    </div>`;
  }).join('');
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
  if (isBusy()) return runStatusLabel(currentRun()?.status || 'running');
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
  const stderr = String(log.stderr_tail || '').trim().split('\n').filter(Boolean);
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
  target.innerHTML = `<article class="live-run-card l-stack">
    <div class="live-run-header">
      <div class="live-run-title">Текущий подбор</div>
      ${statusBadge(liveRunStatusText(), tone)}
    </div>
    <div class="live-run-grid l-grid">
      ${liveRunCells(progress).map(([label, value]) => `<div class="live-run-cell">
        <div class="live-run-label">${esc(label)}</div>
        <div class="live-run-value">${esc(value || '-')}</div>
      </div>`).join('')}
    </div>
    <div class="helper-text">${important ? esc(important) : 'Ошибок и предупреждений в текущем срезе нет.'}</div>
    <div class="live-run-actions l-cluster">
      <button class="secondary danger" data-action="stop-current" type="button"${isBusy() ? '' : ' disabled'}>Остановить</button>
      <button class="secondary" data-action="open-log" type="button">Открыть лог</button>
      <button class="secondary" data-action="open-candidates" type="button">Открыть результаты</button>
      <div class="action-consequence">Найденные стратегии сохранятся после остановки.</div>
    </div>
  </article>`;
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

function scrollLogToBottom(){
  const logNode = el('finder-log');
  if (!logNode) return;
  requestAnimationFrame(() => {
    logNode.scrollTop = logNode.scrollHeight;
  });
}

function trimTextLines(text, maxLines){
  const lines = String(text || '').split('\n');
  if (lines.length <= maxLines) return lines.join('\n');
  return lines.slice(lines.length - maxLines).join('\n');
}

function appendLogText(base, addition){
  const left = String(base || '');
  const right = String(addition || '');
  if (!left || !right || left.endsWith('\n') || right.startsWith('\n')) return left + right;
  return `${left}\n${right}`;
}

function latestLogUrl(incremental){
  const acknowledgedRunId = state.acknowledgedRun ? runIdForRow(state.acknowledgedRun) : '';
  const params = new URLSearchParams();
  if (acknowledgedRunId) params.set('run_id', acknowledgedRunId);
  if (!incremental || !state.finderLog || !state.finderLog.stdout_log || (state.acknowledgedRun && runIdForRow(state.finderLog) !== state.acknowledgedRun.run_id)) {
    return acknowledgedRunId ? apiUrl('core', 'latestLog', params) : apiEndpoint('core', 'latestLog');
  }
  params.set('stdout_log', state.finderLog.stdout_log || '');
  params.set('stdout_size', String(state.finderLog.stdout_size || 0));
  params.set('stderr_log', state.finderLog.stderr_log || '');
  params.set('stderr_size', String(state.finderLog.stderr_size || 0));
  return apiUrl('core', 'latestLog', params);
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
  // Snapshot reconciliation can provide an already reconstructed tail instead
  // of an append.  Apply the same established per-stream window used above so
  // a larger same-offset reply cannot bypass the 200-line live-tail bound.
  if (sameStdout && next.stdout_tail) next.stdout_tail = trimTextLines(next.stdout_tail, 200);
  if (sameStderr && next.stderr_tail) next.stderr_tail = trimTextLines(next.stderr_tail, 200);
  return next;
}

async function refreshLog(incremental = false){
  const viewRequest=lifetime.capture(null);
  const epoch = currentSessionEpoch();
  const logRequest = runState.captureLogRequest();
  try {
    const payload = await lifetime.result(getJson(latestLogUrl(incremental)), viewRequest);
    if (!sessionIsCurrent(epoch)) return;
    if (!runState.acceptLog(payload, incremental, mergeLogPayload, logRequest)) return;
    logDirty.value = false;
    renderLog();
    renderMetrics();
  } catch (error) {
    if(!lifetime.isCurrent(viewRequest)) return;
    if (!sessionIsCurrent(epoch)) return;
    setMessage(`Ошибка обновления лога: ${error.message}`, 'bad');
  }
}
  function dispose(){ lifetime.dispose();  }
  // Internal DOM dispatch and exported calls share one stale-action boundary.
  refreshLog = lifetime.action(refreshLog);
  return { renderLog, renderStderrDiagnostics, renderProgress, progressAttemptText, progressStrategyText, interruptedRunWarning, liveRunStatusText, liveRunCells, latestImportantLogMessage, renderLiveRun, progressLiveElapsedSeconds, progressLiveEtaSeconds, etaModeLabel, etaStatusText, scrollLogToBottom, trimTextLines, appendLogText, latestLogUrl, mergeLogPayload, refreshLog, dispose };
}
