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
  const zapret = status.zapret2 || {};
  const zapretCompact = zapretCompactStatus(zapret);
  const ready = discoveryEngineReady(status);
  const busy = isBusy();
  const jobStatus = currentRun()?.status || (busy ? 'running' : '');
  const version = (state.status || {}).version || '-';
  const action = nextActionStatus(ready, busy, jobStatus, status);
  setText('app-version-badge', `v${version}`);
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
  const mutatingSelectors = [
    'button[data-action="save-settings"]',
    'button[data-action="create-backup"]',
    'button[data-action="upload-backup"]',
    'button[data-action="create-clean-install-vault"]',
    'button[data-clean-install-vault-restore]',
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
    const active = button.dataset.candidateView === state.candidateView;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', active ? 'true' : 'false');
    button.tabIndex = active ? 0 : -1;
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
          loading: Boolean(state.commonLoadingMore),
          loadedTotal,
          remoteTotal,
          remoteLabel: 'Загрузить еще общие стратегии'
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
        loading: Boolean(data.loadingMore),
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
