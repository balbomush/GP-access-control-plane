  const options = row.discovery_options || {};
  const isBs = String(row.discovery_engine || '').startsWith('blockchecks');
  const protocols = [];
  if (truthyOption(options.enable_http, row.enable_http)) protocols.push('HTTP');
  if (truthyOption(options.enable_tls12, row.enable_tls12 ?? row.enable_tls)) protocols.push('TLS 1.2');
  if (truthyOption(options.enable_tls13, row.enable_tls13)) protocols.push('TLS 1.3');
  if (truthyOption(options.enable_quic, row.include_quic ?? row.enable_quic)) protocols.push('QUIC');
  if (isBs && options.protocol) protocols.push(options.protocol === 'tls13' ? 'TLS 1.3' : 'TLS 1.2');
  const scan = options.scan_level || row.scan_level || 'standard';
  const repeats = Number(options.repeats || row.repeats || 1);
  const repeatParallel = truthyOption(options.repeat_parallel, row.repeat_parallel) ? ', параллельные повторы' : '';
  const skip = [
    truthyOption(options.skip_dnscheck, row.skip_dnscheck) ? 'без DNS' : 'с DNS',
    truthyOption(options.skip_ipblock, row.skip_ipblock) ? 'без IP-проверки' : 'с IP-проверкой',
  ].join(', ');
  const ipv6 = truthyOption(options.enable_ipv6, row.enable_ipv6) ? ', IPv6' : '';
  const debugLog = truthyOption(row.debug_stdout, false) ? ', debug-log' : '';
  const bsExtras = isBs
    ? `${options.strategy_preset ? ', пресет ' + options.strategy_preset : ''}` +
      `${options.repeats_mode ? ', повторы ' + options.repeats_mode : ''}` +
      `${options.adaptive !== false ? ', AQ вкл' : ', AQ выкл'}`
    : '';
  const curl = row.kind === 'multi-domain-discovery' ? `, проверочных запросов ${row.curl_parallelism || 4}` : '';
  const limit = row.timeout_seconds ? `, лимит ${formatDuration(Number(row.timeout_seconds || 0))}` : ', без лимита';
  return `${protocols.join('+') || '-'} · ${scan} · повт. ${repeats}${repeatParallel} · ${skip}${ipv6}${debugLog}${bsExtras}${curl}${limit}`;
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
  el('finder-domains').value = domains.join('\n');
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
  const row = state.finderRuns.find((item) => runDomainKey(item) === runKey);
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
  if (log.stderr_tail) parts.push('--- stderr ---\n' + log.stderr_tail);
  const logNode = el('finder-log');
  logNode.textContent = parts.join('\n\n') || 'Лога пока нет';
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
function renderCleanInstallVaults(){
  const rows = Array.isArray(state.cleanInstallVaults) ? state.cleanInstallVaults : [];
  const countNode = el('clean-install-vault-count');
  if (countNode) countNode.textContent = String(rows.length);
  const updatedNode = el('clean-install-vault-updated-at');
  if (updatedNode) {
    const updated = friendlyTime(state.cleanInstallVaultsUpdatedAt);
    updatedNode.textContent = updated ? `Статус обновлен ${updated}` : '';
  }
  const target = el('clean-install-vaults');
  if (!target) return;
  if (state.cleanInstallVaultsLoading && !state.cleanInstallVaultsLoaded) {
    target.innerHTML = '<div class="loading-skeleton" aria-label="Загрузка vault"></div>';
    return;
  }
  if (!rows.length) {
    target.innerHTML = `<div class="empty">${state.cleanInstallVaultsLoaded ? 'Pending vault для чистой установки нет' : 'Откройте вкладку, чтобы загрузить статус vault'}</div>`;
    return;
  }
  target.innerHTML = rows.map((item) => {
    const id = String(item.vault_id || '');
    return `<article class="backup-card">
      <div class="domain-header">
        <div>
          <h3>${esc(id)}</h3>
          <div class="helper-text">${esc(item.created_at || '-')}</div>
        </div>
        ${badge(item.pending ? 'ожидает clean install' : 'не ожидает', item.pending ? 'warn' : 'bad')}
      </div>
      <div class="backup-meta">
        <div>Schema: ${esc(item.schema_version || '-')}</div>
        <div>Размер: ${esc(formatBytes(item.archive_size_bytes || 0))}</div>
        <div>SHA-256: <code>${esc(item.archive_sha256 || '-')}</code></div>
        <div>Проверка: ${esc(item.verification || '-')}</div>
      </div>
      <div class="helper-text">После явного подтверждения восстановление проверит данные и SQLite. При успехе исходный vault будет удален автоматически.</div>
      <div class="backup-card-actions">
        <button class="secondary danger" data-clean-install-vault-restore="${esc(id)}" type="button">Восстановить и удалить vault</button>
      </div>
    </article>`;
  }).join('');
}
function cleanInstallVaultListFromPayload(data){
  const items = Array.isArray((data || {}).vaults) ? data.vaults : [];
  return items.map((item) => ({
    vault_id: String((item || {}).vault_id || '').trim(),
    created_at: String((item || {}).created_at || ''),
    schema_version: String((item || {}).schema_version || ''),
    archive_sha256: String((item || {}).archive_sha256 || ''),
    archive_size_bytes: Number((item || {}).archive_size_bytes || 0),
    verification: String((item || {}).verification || ''),
    pending: Boolean((item || {}).pending)
  })).filter((item) => item.vault_id);
}
function backupCard(item){
  const id = String(item.id || '');
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
      <button class="backup-archive-link" data-backup-download="${esc(id)}" type="button">Download archive</button>
      <button class="secondary danger" data-backup-restore="${esc(id)}" type="button">Восстановить из бекапа</button>
      <button class="secondary danger" data-backup-delete="${esc(id)}" type="button">Удалить бекап</button>
    </div>
  </article>`;
}
function normalizeBackupSnapshot(item){
  const snapshotId = String(item.id || item.snapshot_id || '').trim();
  const counts = item.entity_counts || {};
  return {
    ...item,
    id: snapshotId,
    checksum_ok: Object.prototype.hasOwnProperty.call(item, 'checksum_ok') ? Boolean(item.checksum_ok) : item.checksum === 'ok',
    strategy_count: Number(item.strategy_count ?? counts.strategies ?? 0),
    preset_count: Number(item.preset_count ?? counts.domain_lists ?? 0)
  };
}
function backupListFromPayload(data){
  const items = Array.isArray((data || {}).snapshots) ? data.snapshots : (Array.isArray((data || {}).backups) ? data.backups : []);
  return items.map((item) => normalizeBackupSnapshot(item || {})).filter((item) => item.id);
}
function backupDownloadUrl(snapshot){
  const params = new URLSearchParams();
  params.set('snapshot_id', snapshot);
  return requestUrl(apiUrl('core', 'backupsDownloadArchive', params));
}
async function downloadBackup(url, snapshotId){
  const id = String(snapshotId || '').trim();
  try {
    const response = await authFetch(url);
    if (!response.ok) throw new Error((await response.text()) || response.statusText);
    const blob = await response.blob();
    const disposition = response.headers.get('Content-Disposition') || '';
    const filenameMatch = /filename="?([^";]+)"?/i.exec(disposition);
    const filename = filenameMatch ? filenameMatch[1] : `gp-backup-${id || 'archive'}.zip`;
    const objectUrl = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = objectUrl;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
  } catch (error) {
    setMessage(`Archive download failed: ${error.message}`, 'bad');
  }
}function formatBytes(value){
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
