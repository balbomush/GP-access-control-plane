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
function buildCandidateResult(mode){
  const targets = candidateResultTargets();
  const rows = commonCandidateResultRows();
  const uncoveredRequired = new Set(targets.required);
  const uncoveredDesired = new Set(targets.desired);
  const selected = [];
  const remaining = rows.slice();
  while ((uncoveredRequired.size || uncoveredDesired.size) && remaining.length) {
    remaining.sort((a, b) => resultPickScore(b, uncoveredRequired, uncoveredDesired, mode) - resultPickScore(a, uncoveredRequired, uncoveredDesired, mode));
    const best = remaining.shift();
    if (!best) break;
    const requiredHit = rowTargetCoverage(best, [...uncoveredRequired]);
    const desiredHit = rowTargetCoverage(best, [...uncoveredDesired]);
    if (!requiredHit.length && !desiredHit.length) continue;
    selected.push({ row: best, requiredHit, desiredHit });
    requiredHit.forEach((domain) => uncoveredRequired.delete(domain));
    desiredHit.forEach((domain) => uncoveredDesired.delete(domain));
    if (mode === 'minimal' && !uncoveredRequired.size && !uncoveredDesired.size) break;
  }
  const coveredRequired = targets.required.filter((domain) => !uncoveredRequired.has(domain));
  const coveredDesired = targets.desired.filter((domain) => !uncoveredDesired.has(domain));
  const modeLabel = candidateResultModeLabel(mode);
  const targetCount = targets.required.length + targets.desired.length;
  const reason = !targetCount
    ? 'Нет обязательных или желательных доменов для расчета итогового набора.'
    : selected.length
    ? `${modeLabel}: покрыто ${coveredRequired.length}/${targets.required.length} обязательных и ${coveredDesired.length}/${targets.desired.length} желательных доменов по загруженным стратегиям.`
    : 'Нет загруженных стратегий, которые покрывают выбранные домены.';
  return {
    required_coverage: { covered: coveredRequired.length, total: targets.required.length },
    desired_coverage: { covered: coveredDesired.length, total: targets.desired.length },
    uncovered_required: [...uncoveredRequired],
    uncovered_desired: [...uncoveredDesired],
    strategy_set: selected.map((item) => ({
      args: String(item.row.args || '').trim(),
      protocol: String(item.row.protocol || '-'),
      domains: uniqueDomains([...item.requiredHit, ...item.desiredHit])
    })),
    reason,
    mode: modeLabel,
    loaded_rows: rows.length,
    targets
  };
}
function candidateResultText(result){
  const lines = (result.strategy_set || []).map((item) => item.args).filter(Boolean);
  return lines.join('\n');
}
function resetCandidateResult(){
  state.candidateResultRequested = false;
  renderCandidateResult();
}
async function buildCandidateResultNow(){
  state.candidateResultRequested = true;
  if (state.candidateView !== 'common') state.candidateView = 'common';
  const selectedDomains = selectedCommonDomains();
  const loaded = prepareCommonCandidateState();
  renderCandidatesOnly();
  if (selectedDomains.length >= 2 && !loaded) {
    await refreshCandidates(true);
  }
}
function renderCandidateResult(){
  const panel = document.querySelector('.candidate-result-panel');
  const body = el('candidate-result-body');
  const source = el('candidate-result-source');
  if (panel) panel.hidden = state.candidateView !== 'common';
  if (!body) return;
  const mode = state.candidateResultMode || 'balance';
  document.querySelectorAll('[data-candidate-result-mode]').forEach((button) => {
    const active = button.dataset.candidateResultMode === mode;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', active ? 'true' : 'false');
    button.tabIndex = active ? 0 : -1;
  });
  body.setAttribute('aria-labelledby', `candidate-result-mode-${mode}`);
  if (state.candidateView !== 'common') return;
  if (!state.candidateResultRequested) {
    if (source) source.textContent = 'Выберите домены для пересечения и соберите итоговый набор.';
    body.innerHTML = '<div class="empty">Нажмите «Собрать итоговый набор» после выбора доменов.</div>';
    return;
  }
  const selectedDomains = selectedCommonDomains();
  if (selectedDomains.length < 2) {
    if (source) source.textContent = 'Для итогового набора нужны минимум два протестированных домена.';
    body.innerHTML = '<div class="empty">Выберите минимум два домена в пресете доменов для пересечения.</div>';
    return;
  }
  const result = buildCandidateResult(mode);
  const rows = Number(result.loaded_rows || 0);
  const requiredTotal = Number(result.required_coverage.total || 0);
  const desiredTotal = Number(result.desired_coverage.total || 0);
  if (source) {
    source.textContent = `Расчет по загруженным общим стратегиям: ${rows}. Обязательные: ${requiredTotal}. Желательные: ${desiredTotal}.`;
  }
  if (!rows) {
    body.innerHTML = '<div class="empty">Для выбранного пересечения пока нет загруженных общих стратегий.</div>';
    return;
  }
  const strategies = result.strategy_set || [];
  const strategiesHtml = strategies.length
    ? `<div class="candidate-result-strategies">${strategies.map((item) => `<div class="candidate-result-strategy">
        <code>${esc(item.args || '-')}</code>
        <div class="candidate-result-domains">${esc(item.protocol || '-')} · ${esc((item.domains || []).join(', ') || '-')}</div>
      </div>`).join('')}</div>`
    : '<div class="empty">По загруженным стратегиям нет покрытия выбранных доменов.</div>';
  body.innerHTML = `<div class="candidate-result-grid">
    <div class="candidate-result-cell">
      <div class="candidate-result-label">mode</div>
      <div class="candidate-result-value">${esc(result.mode)}</div>
    </div>
    <div class="candidate-result-cell">
      <div class="candidate-result-label">required_coverage</div>
      <div class="candidate-result-value">${result.required_coverage.covered} / ${result.required_coverage.total}</div>
    </div>
    <div class="candidate-result-cell">
      <div class="candidate-result-label">desired_coverage</div>
      <div class="candidate-result-value">${result.desired_coverage.covered} / ${result.desired_coverage.total}</div>
    </div>
    <div class="candidate-result-cell">
      <div class="candidate-result-label">strategy_set</div>
      <div class="candidate-result-value">${strategies.length}</div>
    </div>
  </div>
  <div class="helper-text">${esc(result.reason)}</div>
  <details class="candidate-result-details" open>
    <summary>Детали итогового набора</summary>
    <div class="helper-text">uncovered_required: ${esc(result.uncovered_required.join(', ') || '-')}</div>
    <div class="helper-text">uncovered_desired: ${esc(result.uncovered_desired.join(', ') || '-')}</div>
    ${strategiesHtml}
  </details>
  <div class="candidate-result-actions">
    <button class="secondary" data-action="copy-candidate-result" type="button"${strategies.length ? '' : ' disabled'}>Скопировать для zapret2</button>
    <button class="secondary" data-action="export-nfconf" type="button">Экспорт nfqws2 (bc-nfconf)</button>
    <button class="secondary" data-action="export-candidate-result" type="button"${strategies.length ? '' : ' disabled'}>Экспорт TXT</button>
    <button class="secondary" data-action="use-candidate-result-domains" type="button">Повторить подбор</button>
    <button class="secondary" data-action="open-candidate-result" type="button">Открыть детали</button>
  </div>`;
  syncEngineUi();
}
async function copyCandidateResult(){
  const result = buildCandidateResult(state.candidateResultMode || 'balance');
  const text = candidateResultText(result);
  if (!text) {
    setMessage('В итоговом наборе нет стратегий для копирования', 'warn');
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
    setMessage('Итоговый набор скопирован', 'good');
  } catch (error) {
    setMessage(`Не удалось скопировать итоговый набор: ${error.message}`, 'bad');
  }
}
function exportCandidateResult(){
  const result = buildCandidateResult(state.candidateResultMode || 'balance');
  const text = candidateResultText(result);
  if (!text) {
    setMessage('В итоговом наборе нет стратегий для экспорта', 'warn');
    return;
  }
  const blob = new Blob([text + '\n'], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = 'gp-candidate-result.txt';
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
function useCandidateResultDomains(){
  const result = buildCandidateResult(state.candidateResultMode || 'balance');
  const domains = uniqueDomains([...(result.targets.required || []), ...(result.targets.desired || [])]);
  if (!domains.length) {
    setMessage('Нет доменов для повторного запуска', 'warn');
    return;
  }
  el('finder-domains').value = domains.join('\n');
  state.domainsTouched = true;
  markDomainPresetCustom('finder');
  updateEditorLineNumbers('finder-domains');
  renderRunLaunchSummary();
  setActiveTab('finder');
  setMessage('Домены итогового набора перенесены в форму запуска. Старт выполните вручную.', 'good');
}
function openCandidateResultDetails(){
  const details = document.querySelector('.candidate-result-details');
  if (details) details.open = true;
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
  return Object.keys(value).sort().map((key) => `${key}:${JSON.stringify(value[key])}`).join('|');
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
  state.candidateDomainOffset = 0;
  state.candidateDomainHasMore = false;
  state.candidateDomainsLoaded = false;
  state.domainStrategies = {};
  state.commonCandidateCache = {};
  state.testedDomains = [];
  state.openCandidateDomains = {};
  state.openCommonProtocols = {};
  state.expandedStrategyLists = {};
  state.strategyEditorScrolls = {};
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
