/* history: explicit callbacks and a bounded view, no session/run owner. */
function createHistoryController({ view, apiUrl, badge, curlParallelism, currentSessionEpoch, el, esc, formatDuration, friendlyDate, getJson: requestJson, listLoadMore, markDomainPresetCustom, renderDiscoveryProfileNote, renderMetrics, renderRunLaunchSummary, renderRunModeNote, sessionIsCurrent, setActiveTab, setMessage, setText, statusBadge, statusTone, syncTimeLimitUi, uniqueDomains, updateEditorLineNumbers , latestRun, RUN_PAGE_LIMIT, runState, latestById, renderLiveRun, renderEvents}) {
  const state = view;
  const lifetime = new UiLifetime();
  const getJson=(url,options)=>requestJson(url,lifetime.requestOptions(options));

  const setTimeout = lifetime.setTimeout.bind(lifetime);
  const clearTimeout = lifetime.clearTimeout.bind(lifetime);
  const requestAnimationFrame = lifetime.requestAnimationFrame.bind(lifetime);

function renderRuns(){
  const rows = state.finderRuns.filter((row) => isDiscoveryRun(row));
  setText('finder-runs-count', String(state.finderRunTotal || rows.length));
  const visible = rows.slice().reverse();
  if (!visible.length) {
    el('finder-runs-table').innerHTML = '<div class="empty">Запусков поиска пока не было</div>';
    return;
  }
  el('finder-runs-table').innerHTML = `<div class="run-history l-stack">${visible.map(renderRunCard).join('')}</div>${runPager()}`;
}

function runPager(){
  return listLoadMore('load-more-runs', state.finderRunHasMore, state.finderRunsLoading);
}

function renderRunCard(row){
  const count = runCandidateCount(row);
  const status = row.status || '-';
  const domainKey = runDomainKey(row);
  return `<article class="run-card l-stack ${esc(runCardClass(row))}">
    <div class="run-card-main">
      ${runField('Время', friendlyDate(row.timestamp))}
      ${runField('Режим', runMode(row))}
      <div class="run-field run-field-status">
        <div class="run-field-label">Статус</div>
        <div class="run-field-value run-status">${statusBadge(runStatusLabel(status), statusTone[status] || '')}</div>
      </div>
      <div class="run-field run-field-phase">
        <div class="run-field-label">Этап</div>
        <div class="run-field-value">${esc(runPhaseText(row) || '-')}</div>
      </div>
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
    <div class="run-card-actions l-cluster">
      <button class="secondary" data-run-repeat="${esc(domainKey)}" type="button">Повторить с этими настройками</button>
    </div>
  </article>`;
}

function runDomainKey(row){
  return String(row.run_id || `${row.timestamp || ''}:${(row.domains || []).join('|')}`);
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
    queued: 'В очереди',
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

async function refreshRuns(reset = true){
  const viewRequest=lifetime.capture(null);
  const epoch = currentSessionEpoch();
  const offset = reset ? 0 : state.finderRunOffset;
  state.finderRunsLoading = true;
  renderRuns();
  try {
    const finderRuns = await lifetime.result(getJson(apiUrl('web', 'runHistoryPage', runParams(offset))), viewRequest);
    if (!sessionIsCurrent(epoch)) return;
    mergeRunPage(finderRuns, reset);
    renderRuns();
    renderMetrics();
  } catch (error) {
    if(!lifetime.isCurrent(viewRequest)) return;
    if (!sessionIsCurrent(epoch)) return;
    state.finderRunsLoading = false;
    renderRuns();
    setMessage(`Ошибка обновления истории: ${error.message}`, 'bad');
  }
}
  function dispose(){ lifetime.dispose();  }
function handleClickPart0(button){
if (button.dataset.runRepeat) {
    repeatRun(button.dataset.runRepeat);
    return true;
  }
 return false;
}

function handleClickPart1(button){
if (button.dataset.action === 'repeat-last-run') {
    const row = latestRun();
    if (row) repeatRun(runDomainKey(row));
    else setMessage('В истории пока нет запуска для повтора', 'warn');
    return true;
  }
 return false;
}

function handleClickPart2(button){
if (button.dataset.action === 'load-more-runs') {
    refreshRuns(false);
    return true;
  }
 return false;
}

function handleTogglePart3(details){
if (details.matches('details.run-domains[data-run-domains]')) {
    state.openRunDomains[details.dataset.runDomains] = details.open;
  }
 return false;
}
function runParams(offset){
  const params = new URLSearchParams();
  params.set('limit', String(RUN_PAGE_LIMIT));
  params.set('offset', String(Math.max(0, offset || 0)));
  return params;
}

function mergeRunPage(payload, reset){
  const rows = runState.mergeHistory(payload, reset, latestById);
  // A status-before-runs SSE sequence can establish a live current run while
  // merging history.  History normally only drives the table/metrics, so make
  // the status-derived consumers agree immediately instead of waiting for a
  // later log or status event.  Bootstrap has no mounted shell yet and renders
  // all views when ready.
  if (runState.consumeHistoryStatusPromotion() && el('app-shell')) {
    renderMetrics();
    renderLiveRun();
    renderEvents();
  }
  return rows;
}
  return { runParams, mergeRunPage, handleClickPart0, handleClickPart1, handleClickPart2, handleTogglePart3, renderRuns, runPager, renderRunCard, runDomainKey, runCardClass, runField, runStatusLabel, runPhaseText, phaseLabel, runDomains, runDomainChips, diagnosticShortLabel, diagnosticExplanation, curlCodeLabel, curlCodeDetails, runDiagnosticsSummary, runDiagnostics, diagnosticTableRow, isDiscoveryRun, runMode, runSummary, runCandidateCount, runSettingsText, truthyOption, runPayload, fillRunFormFromPayload, repeatRun, runProgressText, refreshRuns: lifetime.action(refreshRuns), dispose };
}
