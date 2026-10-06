/* candidates: explicit callbacks and a bounded view, no session/run owner. */
function createCandidatesController({ view, CANDIDATE_PAGE_LIMIT, DOMAIN_PAGE_LIMIT, STRATEGY_LIST_LIMIT, apiUrl, badge, el, esc, friendlyTime, getJson: requestJson, listLoadMore, markDomainPresetCustom, parseDomains, presetDomains, renderMetrics, renderPresetSelect, renderRunLaunchSummary, setActiveTab, setMessage, setText, showToast, uniqueDomains , refresh, renderPresetEditorPreview, renderPresetNewPreview}) {
  const state = view;
  const selection = createCandidateSelectionModel({uniqueDomains});
  const lifetime = new UiLifetime();
  const getJson=(url,options)=>requestJson(url,lifetime.requestOptions(options));

  const setTimeout = lifetime.setTimeout.bind(lifetime);
  const clearTimeout = lifetime.clearTimeout.bind(lifetime);
  const requestAnimationFrame = lifetime.requestAnimationFrame.bind(lifetime);
  let candidateRequestSeq = 0;
  let domainIndexRequestSeq = 0;
  let candidateRefreshTimer = null;
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
  el('candidates-table').innerHTML = `<div class="candidate-groups l-stack">${groups.map((domainGroup) => {
    const expanded = Boolean(state.openCandidateDomains[domainGroup.domain]);
    const open = expanded ? ' open' : '';
    const protocolBadges = domainGroup.protocols.map((item) => {
      return badge(`${item.protocol}: ${item.count}`, item.protocol === 'quic' ? 'warn' : 'good');
    }).join('');
    return `<details class="domain-group l-stack" data-domain="${esc(domainGroup.domain)}"${open}>
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
  el('candidates-table').innerHTML = `<div class="candidate-groups l-stack">${groups.map((protocolGroup) => {
    const domains = selectedDomains;
    const expanded = state.openCommonProtocols[protocolGroup.protocol] !== false;
    const loadedTotal = uniqueStrategyArgs(protocolGroup.rows).length;
    const remoteTotal = groups.length === 1 ? Number(state.candidateTotal || loadedTotal) : loadedTotal;
    const hasRemoteMore = groups.length === 1 && Boolean(state.candidateHasMore);
    return `<details class="domain-group l-stack" data-common-protocol="${esc(protocolGroup.protocol)}"${expanded ? ' open' : ''}>
      <summary class="domain-header">
        <div class="domain-title">${esc(protocolGroup.protocol)}</div>
        <div class="domain-meta">
          ${badge(`${loadedTotal} из ${remoteTotal} стратегий`, '')}${domains.length ? badge(`${domains.length} доменов`, 'good') : ''}
        </div>
      </summary>
      <div class="protocol-group l-stack">
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
    return `<section class="protocol-group l-stack">
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

function candidateDomains(...args){ return selection.candidateDomains(...args); }

function commonSeen(...args){ return selection.commonSeen(...args); }

function commonDomains(...args){ return selection.commonDomains(...args); }

function candidateAllDomains(...args){ return selection.candidateAllDomains(...args); }

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

function candidateResultModeLabel(...args){ return selection.candidateResultModeLabel(...args); }

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

function rowTargetCoverage(...args){ return selection.rowTargetCoverage(...args); }

function resultPickScore(...args){ return selection.resultPickScore(...args); }

function buildCandidateResult(mode){ return selection.buildCandidateResult(mode,candidateResultTargets(),commonCandidateResultRows()); }

function candidateResultText(...args){ return selection.candidateResultText(...args); }

function resetCandidateResult(){
  state.candidateResultRequested = false;
  renderCandidateResult();
}

async function buildCandidateResultNow(){
  const viewRequest=lifetime.capture(null);
  state.candidateResultRequested = true;
  if (state.candidateView !== 'common') state.candidateView = 'common';
  const selectedDomains = selectedCommonDomains();
  const loaded = prepareCommonCandidateState();
  renderCandidatesOnly();
  if (selectedDomains.length >= 2 && !loaded) {
    await lifetime.result(refreshCandidates(true), viewRequest);
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
  body.innerHTML = `<div class="candidate-result-grid l-grid">
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
    <button class="secondary" data-action="export-candidate-result" type="button"${strategies.length ? '' : ' disabled'}>Экспорт TXT</button>
    <button class="secondary" data-action="use-candidate-result-domains" type="button">Повторить подбор</button>
    <button class="secondary" data-action="open-candidate-result" type="button">Открыть детали</button>
  </div>`;
}

async function copyCandidateResult(){
  const viewRequest=lifetime.capture(null);
  const result = buildCandidateResult(state.candidateResultMode || 'balance');
  const text = candidateResultText(result);
  if (!text) {
    setMessage('В итоговом наборе нет стратегий для копирования', 'warn');
    return;
  }
  try {
    await lifetime.result(navigator.clipboard.writeText(text), viewRequest);
    setMessage('Итоговый набор скопирован', 'good');
  } catch (error) {
    if(!lifetime.isCurrent(viewRequest)) return;
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
  el('common-domains').value = current.join('\n');
  input.value = '';
  hideCommonDomainSuggestions();
  updateEditorLineNumbers('common-domains');
  markDomainPresetCustom('common');
  state.candidateResultRequested = false;
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

function normalizeStrategyArg(...args){ return selection.normalizeStrategyArg(...args); }

function uniqueStrategyRows(...args){ return selection.uniqueStrategyRows(...args); }

function uniqueStrategyArgs(...args){ return selection.uniqueStrategyArgs(...args); }

function strategyComplexity(...args){ return selection.strategyComplexity(...args); }

function strategyDomainCoverage(...args){ return selection.strategyDomainCoverage(...args); }

function strategyDisplayFamilyKey(...args){ return selection.strategyDisplayFamilyKey(...args); }

function bestFamilyRow(...args){ return selection.bestFamilyRow(...args); }

function strategyFamilyGroups(...args){ return selection.strategyFamilyGroups(...args); }

function strategyListState(key, rows){
  const groups = strategyFamilyGroups(rows);
  const all = groups.flatMap((group) => group.rows.map((row) => String(row.args || '').trim()).filter(Boolean));
  const expanded = Boolean(state.expandedStrategyLists[key]);
  let remaining = expanded ? Number.MAX_SAFE_INTEGER : STRATEGY_LIST_LIMIT;
  const visibleGroups = [];
  groups.forEach((group) => {
    if (remaining <= 0) return;
    const rowsToShow = group.rows.slice(0, remaining);
    remaining -= rowsToShow.length;
    visibleGroups.push({ ...group, rows: rowsToShow, hidden: Math.max(0, group.rows.length - rowsToShow.length) });
  });
  const visibleCount = visibleGroups.reduce((sum, group) => sum + group.rows.length, 0);
  return { all, groups, visibleGroups, visibleCount, expanded, hidden: Math.max(0, all.length - visibleCount) };
}

function lineNumbers(count){
  return Array.from({ length: count }, (_item, index) => String(index + 1)).join('\n');
}

function updateEditorLineNumbers(id){
  const field = el(id);
  const gutter = document.querySelector(`[data-line-numbers-for="${id}"]`);
  if (!field || !gutter) return;
  const count = Math.max(1, String(field.value || '').split('\n').length);
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
  const remoteMore = Boolean(opts.hasRemoteMore);
  const loadedTotal = Number(opts.loadedTotal || list.all.length);
  const remoteTotal = Number(opts.remoteTotal || loadedTotal);
  const remoteText = remoteMore ? ` Загружено ${loadedTotal}${remoteTotal ? ` из ${remoteTotal}` : ''}; оставшиеся догружаются по кнопке.` : '';
  const meta = `Показано ${list.visibleCount} из ${list.all.length} уникальных стратегий в ${list.groups.length} семействах. Дубликаты строк скрыты.${list.hidden ? ` Скрыто до раскрытия: ${list.hidden}.` : ''}${remoteText}`;
  const remoteAttr = remoteMore ? ' data-strategy-remote-more="true"' : '';
  const toggle = list.all.length > STRATEGY_LIST_LIMIT || remoteMore
    ? `<button class="secondary" data-strategy-list-toggle="${esc(key)}"${remoteAttr} type="button"${opts.loading ? ' disabled' : ''}>${strategyToggleLabel(list, opts)}</button>`
    : '';
  return `<div class="strategy-editor" data-strategy-list="${esc(key)}">
    <div class="strategy-editor-head">
      <div class="strategy-editor-title">
        <label>${esc(title)}</label>
        <div class="strategy-editor-meta">${esc(meta)}</div>
      </div>
      ${toggle}
    </div>
    <div class="strategy-family-list">${list.visibleGroups.map((group, index) => strategyFamilyGroup(key, group, index)).join('')}</div>
  </div>`;
}

function strategyFamilyGroup(parentKey, group, index){
  const lines = group.rows.map((row) => String(row.args || '').trim()).filter(Boolean);
  const lineCount = Math.max(lines.length, 1);
  const rowsAttr = Math.min(Math.max(lineCount, 4), 14);
  const best = group.best || {};
  const hidden = Number(group.hidden || 0);
  const reason = [
    group.familyReason ? `семейство: ${group.familyReason}` : '',
    hidden ? `скрыто вариантов: ${hidden}` : ''
  ].filter(Boolean).join(' · ');
  const key = `${parentKey}:family:${index}:${group.key}`;
  return `<details class="strategy-family" open>
    <summary class="strategy-family-summary">
      <div class="strategy-family-head">
        ${badge(group.family || 'other', '')}
        ${badge(`${group.rows.length + hidden} вариантов`, group.rows.length + hidden > 1 ? 'warn' : '')}
      </div>
      <div class="strategy-family-reason">${esc(reason || 'семейство определено по аргументам стратегии')}</div>
    </summary>
    <div class="code-editor">
      <pre class="line-numbers" aria-hidden="true">${esc(lineNumbers(lineCount))}</pre>
      <textarea class="strategy-code" data-strategy-code-key="${esc(key)}" readonly spellcheck="false" rows="${rowsAttr}">${esc(lines.join('\n'))}</textarea>
    </div>
  </details>`;
}

function strategyToggleLabel(list, options){
  const opts = options || {};
  if (opts.loading) return 'Загружается...';
  if (opts.hasRemoteMore) return opts.remoteLabel || 'Загрузить еще стратегии домена';
  if (list.expanded) return `Свернуть до ${STRATEGY_LIST_LIMIT}`;
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

async function refreshDomainIndex(reset = true){
  const viewRequest=lifetime.capture(null);
  const requestId = ++domainIndexRequestSeq;
  const offset = reset ? 0 : state.candidateDomainOffset;
  state.candidateLoading = true;
  renderCandidatesOnly();
  try {
    const params = new URLSearchParams();
    params.set('limit', String(DOMAIN_PAGE_LIMIT));
    params.set('offset', String(Math.max(0, offset || 0)));
    const data = await lifetime.result(getJson(apiUrl('web', 'candidateDomainIndexPage', params)), viewRequest);
    if (requestId !== domainIndexRequestSeq) return;
    const rows = data.domains || [];
    state.candidateDomains = reset ? rows : [...state.candidateDomains, ...rows];
    state.candidateDomainTotal = Number(data.total || 0);
    state.candidateDomainStrategyTotal = Number(data.strategy_total || 0);
    state.candidateDomainOffset = Number(data.offset || offset) + rows.length;
    state.candidateDomainHasMore = Boolean(data.has_more);
    if (state.candidateDomainTotal > 0) state.lastCandidateDomainTotal = state.candidateDomainTotal;
    if (state.candidateDomainStrategyTotal > 0) state.lastCandidateDomainStrategyTotal = state.candidateDomainStrategyTotal;
    rememberCandidateVersion(data.version || null);
    updateTestedDomains(data.tested_domains);
    state.candidateDomainsLoaded = true;
    state.candidateUpdatedAt = new Date().toISOString();
    state.candidateLoading = false;
    renderCandidatesOnly();
  } catch (error) {
    if(!lifetime.isCurrent(viewRequest)) return;
    if (requestId !== domainIndexRequestSeq) return;
    state.candidateLoading = false;
    renderCandidatesOnly();
    setMessage(`Ошибка загрузки доменов: ${error.message}`, 'bad');
  }
}

async function refreshDomainStrategies(domain, reset){
  const viewRequest=lifetime.capture(null);
  const key = String(domain || '').trim();
  if (!key) return;
  const current = state.domainStrategies[key] || { candidates: [], total: 0, hasMore: false, loaded: false };
  const offset = reset ? 0 : current.candidates.length;
  try {
    const data = await lifetime.result(getJson(apiUrl('web', 'strategyCandidatesPage', candidateParams(offset, { view: 'domain', domain: key }))), viewRequest);
    const rows = data.candidates || [];
    state.domainStrategies[key] = {
      candidates: reset ? rows : [...current.candidates, ...rows],
      total: Number(data.total || 0),
      hasMore: Boolean(data.has_more),
      loaded: true,
      loadingMore: false,
      version: data.version || state.candidateKnownVersion
    };
    rememberCandidateVersion(data.version || null);
    updateTestedDomains(data.tested_domains);
    renderCandidatesOnly();
  } catch (error) {
    if(!lifetime.isCurrent(viewRequest)) return;
    setMessage(`Ошибка загрузки стратегий домена: ${error.message}`, 'bad');
  }
}

async function loadMoreDomainStrategies(domain){
  const viewRequest=lifetime.capture(null);
  const key = String(domain || '').trim();
  if (!key) return;
  const current = state.domainStrategies[key] || { candidates: [], total: 0, hasMore: false, loaded: false };
  if (current.loadingMore || !current.hasMore) return;
  const candidates = Array.isArray(current.candidates) ? current.candidates.slice() : [];
  let total = Number(current.total || candidates.length);
  state.domainStrategies[key] = { ...current, candidates, total, hasMore: Boolean(current.hasMore), loaded: true, loadingMore: true };
  renderCandidatesOnly();
  try {
    const data = await lifetime.result(getJson(apiUrl('web', 'strategyCandidatesPage', candidateParams(candidates.length, { view: 'domain', domain: key }))), viewRequest);
    const rows = data.candidates || [];
    const nextCandidates = rows.length ? [...candidates, ...rows] : candidates;
    total = Number(data.total || total || nextCandidates.length);
    const hasMore = rows.length ? Boolean(data.has_more) : false;
    updateTestedDomains(data.tested_domains);
    rememberCandidateVersion(data.version || null);
    state.domainStrategies[key] = { candidates: nextCandidates, total, hasMore, loaded: true, loadingMore: false, version: state.candidateKnownVersion };
    renderCandidatesOnly();
  } catch (error) {
    if(!lifetime.isCurrent(viewRequest)) return;
    state.domainStrategies[key] = { candidates, total, hasMore: Boolean(current.hasMore), loaded: true, loadingMore: false, version: state.candidateKnownVersion };
    setMessage(`Ошибка загрузки следующей страницы стратегий домена: ${error.message}`, 'bad');
    renderCandidatesOnly();
  }
}

async function loadMoreCommonStrategies(){
  const viewRequest=lifetime.capture(null);
  if (state.commonLoadingMore || !state.candidateHasMore) return;
  const domains = selectedCommonDomains();
  if (domains.length < 2) return;
  const queryKey = currentCandidateQueryKey({ view: 'common', domains });
  const candidates = Array.isArray(state.candidates) ? state.candidates.slice() : [];
  state.commonLoadingMore = true;
  renderCandidatesOnly();
  try {
    const data = await lifetime.result(getJson(apiUrl('web', 'strategyCandidatesPage', candidateParams(candidates.length, { view: 'common', domains }))), viewRequest);
    if (state.candidateQueryKey !== queryKey) {
      state.commonLoadingMore = false;
      return;
    }
    const rows = data.candidates || [];
    const nextCandidates = rows.length ? [...candidates, ...rows] : candidates;
    state.candidates = nextCandidates;
    state.candidateTotal = Number(data.total || state.candidateTotal || nextCandidates.length);
    state.candidateOffset = Number(data.offset || candidates.length) + rows.length;
    state.candidateHasMore = rows.length ? Boolean(data.has_more) : false;
    rememberCandidateVersion(data.version || null);
    updateTestedDomains(data.tested_domains);
    state.candidatesLoaded = true;
    state.commonLoadingMore = false;
    storeCommonCandidateCache(queryKey);
    renderCandidatesOnly();
  } catch (error) {
    if(!lifetime.isCurrent(viewRequest)) return;
    setMessage(`Ошибка загрузки следующей страницы общих стратегий: ${error.message}`, 'bad');
    state.commonLoadingMore = false;
    renderCandidatesOnly();
  }
}

async function refreshCandidates(reset){
  const viewRequest=lifetime.capture(null);
  const requestId = ++candidateRequestSeq;
  const offset = reset ? 0 : state.candidates.length;
  const queryKey = currentCandidateQueryKey();
  state.commonLoadingMore = false;
  state.candidateLoading = true;
  renderCandidatesOnly();
  try {
    const data = await lifetime.result(getJson(apiUrl('web', 'strategyCandidatesPage', candidateParams(offset))), viewRequest);
    if (requestId !== candidateRequestSeq) return;
    const rows = data.candidates || [];
    state.candidates = reset ? rows : [...state.candidates, ...rows];
    state.candidateTotal = Number(data.total || 0);
    state.candidateOffset = Number(data.offset || 0);
    state.candidateHasMore = Boolean(data.has_more);
    rememberCandidateVersion(data.version || null);
    updateTestedDomains(data.tested_domains);
    state.candidatesLoaded = true;
    state.candidateQueryKey = queryKey;
    state.candidateUpdatedAt = new Date().toISOString();
    state.candidateLoading = false;
    if (queryKey.startsWith('common:')) storeCommonCandidateCache(queryKey);
    renderCandidatesOnly();
  } catch (error) {
    if(!lifetime.isCurrent(viewRequest)) return;
    if (requestId !== candidateRequestSeq) return;
    state.candidateLoading = false;
    renderCandidatesOnly();
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
      state.candidateResultRequested = false;
      prepareCommonCandidateState();
      renderCandidatesOnly();
      if (selectedCommonDomains().length >= 2) refreshCandidates(true);
    }
  }, 350);
}
  function dispose(){ lifetime.dispose(); candidateRefreshTimer=null; }
function handleClickPart0(domainSummary,event){
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
    return true;
  }
 return false;
}

function handleClickPart1(button){
if (button.dataset.commonDomainSuggestion) {
    chooseCommonDomainSuggestion(button.dataset.commonDomainSuggestion);
    return true;
  }
 return false;
}

function handleClickPart2(button){
if (button.dataset.candidateView) {
    setCandidateView(button.dataset.candidateView);
    return true;
  }
 return false;
}

function handleClickPart3(button){
if (button.dataset.candidateResultMode) {
    state.candidateResultMode = button.dataset.candidateResultMode;
    renderCandidateResult();
    return true;
  }
 return false;
}

function handleClickPart4(button){
if (button.dataset.action === 'build-candidate-result') {
    buildCandidateResultNow();
    return true;
  }
 return false;
}

function handleClickPart5(button){
if (button.dataset.action === 'copy-candidate-result') {
    copyCandidateResult();
    return true;
  }
 return false;
}

function handleClickPart6(button){
if (button.dataset.action === 'export-candidate-result') {
    exportCandidateResult();
    return true;
  }
 return false;
}

function handleClickPart7(button){
if (button.dataset.action === 'use-candidate-result-domains') {
    useCandidateResultDomains();
    return true;
  }
 return false;
}

function handleClickPart8(button){
if (button.dataset.action === 'open-candidate-result') {
    openCandidateResultDetails();
    return true;
  }
 return false;
}

function handleClickPart9(button){
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
 return false;
}

function handleClickPart10(button){
if (button.dataset.action === 'load-more-candidates') {
    refreshCandidates(false);
    return true;
  }
 return false;
}

function handleClickPart11(button){
if (button.dataset.action === 'load-more-candidate-domains') {
    refreshDomainIndex(false);
    return true;
  }
 return false;
}

function handleClickPart12(button){
if (button.dataset.action === 'add-common-domain') {
    addCommonDomain();
    return true;
  }
 return false;
}

function handleClickPart13(button){
if (button.dataset.strategyListToggle) {
    const key = button.dataset.strategyListToggle;
    const domain = domainFromStrategyListKey(key);
    const common = isCommonStrategyListKey(key);
    const remoteMore = button.dataset.strategyRemoteMore === 'true';
    if (remoteMore && common && state.candidateHasMore) {
      state.expandedStrategyLists[key] = true;
      loadMoreCommonStrategies();
      return true;
    }
    if (remoteMore && domain && (state.domainStrategies[domain] || {}).hasMore) {
      state.expandedStrategyLists[key] = true;
      loadMoreDomainStrategies(domain);
      return true;
    }
    const currentlyExpanded = Boolean(state.expandedStrategyLists[key]);
    state.expandedStrategyLists[key] = !currentlyExpanded;
    renderCandidates();
    return true;
  }
 return false;
}

function handleInputPart14(event){
if (event.target && event.target.id === 'preset-editor-domains') {
    updateEditorLineNumbers('preset-editor-domains');
    renderPresetEditorPreview(null);
  }
 return false;
}

function handleInputPart15(event){
if (event.target && event.target.id === 'preset-new-domains') {
    updateEditorLineNumbers('preset-new-domains');
    renderPresetNewPreview(null);
  }
 return false;
}

function handleInputPart16(event){
if (event.target && event.target.id === 'finder-domains') {
    updateEditorLineNumbers('finder-domains');
    state.domainsTouched = true;
    markDomainPresetCustom('finder');
    if (state.candidateView === 'common') scheduleCandidateRefresh();
  }
 return false;
}

function handleInputPart17(event){
if (event.target && event.target.id === 'common-domains') {
    updateEditorLineNumbers('common-domains');
    markDomainPresetCustom('common');
    scheduleCandidateRefresh();
    renderCommonDomainSuggestions();
  }
 return false;
}

function handleInputPart18(event){
if (event.target && event.target.id === 'common-domain-add') {
    renderCommonDomainSuggestions();
  }
 return false;
}

function handleScrollPart19(event){
if (event.target && event.target.matches && event.target.matches('.strategy-code, .line-numbered-textarea')) {
    const gutter = event.target.previousElementSibling;
    if (gutter) gutter.scrollTop = event.target.scrollTop;
    if (event.target.matches('.strategy-code')) {
      const key = strategyEditorScrollKey(event.target);
      if (key) state.strategyEditorScrolls[key] = event.target.scrollTop;
    }
  }
 return false;
}

function handleKeydownPart20(event){
if (event.target && event.target.id === 'common-domain-add' && event.key === 'Enter') {
    event.preventDefault();
    addCommonDomain();
  }
 return false;
}

function handleKeydownPart21(event){
if (event.target && event.target.id === 'common-domain-add' && event.key === 'Escape') {
    hideCommonDomainSuggestions();
  }
 return false;
}

function handleFocusinPart22(event){
if (event.target && event.target.id === 'common-domain-add') {
    renderCommonDomainSuggestions();
  }
 return false;
}

function handleFocusoutPart23(event){
if (event.target && event.target.id === 'common-domain-add') {
    setTimeout(hideCommonDomainSuggestions, 120);
  }
 return false;
}

function handleTogglePart24(details){
if (details.matches('details.domain-group[data-common-protocol]')) {
    if (state.openCommonProtocols[details.dataset.commonProtocol] !== details.open) {
      state.openCommonProtocols[details.dataset.commonProtocol] = details.open;
      renderCandidates();
    }
  }
 return false;
}
  // Internal DOM dispatch and exported calls share one stale-action boundary.
  buildCandidateResultNow = lifetime.action(buildCandidateResultNow);
  copyCandidateResult = lifetime.action(copyCandidateResult);
  refreshDomainIndex = lifetime.action(refreshDomainIndex);
  refreshDomainStrategies = lifetime.action(refreshDomainStrategies);
  loadMoreDomainStrategies = lifetime.action(loadMoreDomainStrategies);
  loadMoreCommonStrategies = lifetime.action(loadMoreCommonStrategies);
  refreshCandidates = lifetime.action(refreshCandidates);
  return { handleClickPart0, handleClickPart1, handleClickPart2, handleClickPart3, handleClickPart4, handleClickPart5, handleClickPart6, handleClickPart7, handleClickPart8, handleClickPart9, handleClickPart10, handleClickPart11, handleClickPart12, handleClickPart13, handleInputPart14, handleInputPart15, handleInputPart16, handleInputPart17, handleInputPart18, handleScrollPart19, handleKeydownPart20, handleKeydownPart21, handleFocusinPart22, handleFocusoutPart23, handleTogglePart24, renderCandidates, renderDomainCandidates, renderCommonCandidates, candidateDomainPager, candidatePager, domainStrategyContent, filteredCandidates, candidateDomains, commonSeen, commonDomains, candidateAllDomains, testedDomains, updateTestedDomains, candidateResultModeLabel, candidateResultTargets, commonCandidateResultRows, rowTargetCoverage, resultPickScore, buildCandidateResult, candidateResultText, resetCandidateResult, buildCandidateResultNow, renderCandidateResult, copyCandidateResult, exportCandidateResult, useCandidateResultDomains, openCandidateResultDetails, filterTestedDomains, selectedCommonDomains, commonDomainSuggestions, renderCommonDomainSuggestions, hideCommonDomainSuggestions, chooseCommonDomainSuggestion, commonCandidateKey, currentCandidateQueryKey, candidateVersionKey, sameCandidateVersion, candidateCacheValid, rememberCandidateVersion, invalidateCandidateCaches, syncCandidateVersion, loadCommonCandidateCache, storeCommonCandidateCache, prepareCommonCandidateState, dynamicCommonRows, renderCommonControls, addCommonDomain, candidateGroups, protocolGroups, normalizeStrategyArg, uniqueStrategyRows, uniqueStrategyArgs, strategyComplexity, strategyDomainCoverage, strategyDisplayFamilyKey, bestFamilyRow, strategyFamilyGroups, strategyListState, lineNumbers, updateEditorLineNumbers, updateAllEditorLineNumbers, strategyEditorScrollKey, rememberStrategyEditorScrolls, restoreStrategyEditorScrolls, strategyEditor, strategyFamilyGroup, strategyToggleLabel, domainFromStrategyListKey, isCommonStrategyListKey, renderCandidatesOnly, ensureCandidateViewLoaded, setCandidateView, candidateParams, refreshDomainIndex, refreshDomainStrategies, loadMoreDomainStrategies, loadMoreCommonStrategies, refreshCandidates, scheduleCandidateRefresh, dispose };
}
