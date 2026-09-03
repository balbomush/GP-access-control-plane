}
function builtInPresets(target){
  const groups = presetGroups(target);
  const presets = groups.flatMap((group) => group.presets);
  return presets;
}
function presetGroups(target){
  const sets = state.domainSets || {};
  const make = (key, label) => ({ key, label, domains: defaultDomains(key) });
  const groups = [];
  if (target === 'common') {
    const tested = testedDomains();
    if (tested.length) {
      groups.push({
        label: 'Протестированные',
        presets: [{ key: 'tested', label: 'Все протестированные', domains: tested }]
      });
    }
  }
  groups.push({
    label: 'Обязательные',
    presets: [
      make('critical', 'Критичные')
    ].filter((preset) => preset.domains.length)
  });
  groups.push({
    label: 'Сервисы',
    presets: [
      make('google-youtube', 'Google / YouTube'),
      make('discord', 'Discord'),
      make('cloudflare', 'Cloudflare'),
      make('amazon-aws', 'Amazon / AWS')
    ].filter((preset) => preset.domains.length)
  });
  groups.push({
    label: 'Готовые наборы',
    presets: [
      make('coverage', 'Покрытие'),
      { key: 'all', label: 'Все встроенные', domains: defaultDomains('all') }
    ].filter((preset) => preset.domains.length)
  });
  const known = new Set(groups.flatMap((group) => group.presets.map((preset) => preset.key)));
  const other = Object.keys(sets)
    .filter((key) => !known.has(key))
    .sort()
    .map((key) => make(key, key))
    .filter((preset) => preset.domains.length);
  if (other.length) groups.push({ label: 'Другие', presets: other });
  if (target === 'common') {
    return groups.filter((group) => group.presets.length);
  }
  return groups.filter((group) => group.presets.length);
}
function presetDomains(target, value){
  const [scope, key] = String(value || '').split(':');
  if (scope === 'system') {
    return state.systemPresets[target]?.[key] || [];
  }
  if (scope === 'builtin') {
    const preset = builtInPresets(target).find((item) => item.key === key);
    return preset ? preset.domains : [];
  }
  if (scope === 'custom') {
    const sourceScope = customPresetSourceScope(target, key);
    return state.customPresets[sourceScope]?.[key] || [];
  }
  return [];
}
function managerPresetEntries(){
  const target = 'finder';
  const system = systemPresetNames(target).map((name) => ({
    name,
    label: systemPresetLabel(target, name),
    count: systemPresetCount(target, name),
    kind: 'system'
  }));
  const custom = customPresetNames(target).map((name) => ({
    name,
    label: name,
    count: customPresetCount(target, name),
    kind: 'user'
  })).filter((item) => !hasSystemPreset(target, item.name));
  const seen = new Set([...system, ...custom].map((item) => item.name));
  const builtin = presetGroups(target)
    .flatMap((group) => group.presets.map((preset) => ({
      name: preset.key,
      label: preset.label,
      count: uniqueDomainCount(preset.domains),
      kind: 'builtin'
    })))
    .filter((item) => item.count > 0 && !seen.has(item.name));
  return [...system, ...custom, ...builtin].sort((a, b) => {
    const rank = { system: 0, user: 1, builtin: 2 };
    const diff = (rank[a.kind] ?? 9) - (rank[b.kind] ?? 9);
    if (diff) return diff;
    return a.label.localeCompare(b.label);
  });
}
function managerPresetEntry(name){
  return managerPresetEntries().find((item) => item.name === name) || null;
}
function renderPresetSelect(target){
  const select = el(`${target}-preset-select`);
  if (!select) return;
  const previous = select.value;
  const systemEntries = systemPresetNames(target);
  const systemGroup = systemEntries.length
    ? `<optgroup label="Системные">${systemEntries.map((name) => `<option value="system:${esc(name)}">${esc(systemPresetLabel(target, name))} (${systemPresetCount(target, name)})</option>`).join('')}</optgroup>`
    : '';
  const customEntries = customPresetNames(target);
  const customGroup = customEntries.length
    ? `<optgroup label="Персональные">${customEntries.map((name) => `<option value="custom:${esc(name)}">${esc(name)} (${customPresetCount(target, name)})</option>`).join('')}</optgroup>`
    : '';
  const builtInGroups = presetGroups(target).map((group) => {
    const options = group.presets.map((preset) => `<option value="builtin:${esc(preset.key)}">${esc(preset.label)} (${uniqueDomainCount(preset.domains)})</option>`).join('');
    return `<optgroup label="${esc(group.label)}">${options}</optgroup>`;
  }).join('');
  select.innerHTML = `<option value="${CUSTOM_SELECT_VALUE}">Custom</option>${systemGroup}${customGroup}${builtInGroups}`;
  if ([...select.options].some((option) => option.value === previous)) select.value = previous;
  else if (target === 'common') select.value = CUSTOM_SELECT_VALUE;
  else if (!previous && [...select.options].some((option) => option.value === 'system:required')) select.value = 'system:required';
  else if (!previous && [...select.options].some((option) => option.value === 'builtin:critical')) select.value = 'builtin:critical';
  else select.value = CUSTOM_SELECT_VALUE;
}
function renderPresetSelects(){
  renderPresetSelect('finder');
  renderPresetSelect('common');
}
function markDomainPresetCustom(target){
  if (state.loadingDomainPreset) return;
  const select = el(`${target}-preset-select`);
  if (select && select.value !== CUSTOM_SELECT_VALUE) select.value = CUSTOM_SELECT_VALUE;
  const nameInput = el(`${target}-preset-name`);
  if (nameInput) nameInput.value = 'custom';
  if (target === 'common') resetCandidateResult();
}
async function fetchAllPresetDomains(target, name){
  if (hasSystemPreset(target, name)) {
    const cached = (state.systemPresets[target] || {})[name] || [];
    const expected = systemPresetCount(target, name);
    if (expected === 0) return [];
    if (cached.length && cached.length >= expected) return uniqueDomains(cached);
    return fetchStoredPresetDomains(target, name, 'system');
  }
  if (!hasCustomPreset(target, name)) {
    const builtin = builtInPresets(target).find((item) => item.key === name);
    if (builtin) return uniqueDomains(builtin.domains);
  }
  const sourceScope = customPresetSourceScope(target, name);
  const cached = (state.customPresets[sourceScope] || {})[name] || [];
  const expected = customPresetCount(sourceScope, name);
  if (expected > 0 && cached.length && cached.length >= expected) return uniqueDomains(cached);
  return fetchStoredPresetDomains(sourceScope, name, 'user');
}
async function fetchStoredPresetDomains(sourceScope, name, kind){
  let offset = 0;
  let hasMore = true;
  let domains = [];
  let guard = 0;
  while (hasMore && guard < 1000) {
    const params = new URLSearchParams();
    params.set('scope', sourceScope);
    params.set('name', name);
    params.set('kind', kind || 'user');
    params.set('include_disabled', '0');
    params.set('limit', '500');
    params.set('offset', String(offset));
    const data = await getJson(apiUrl('web', 'presetDomains', params));
    const rows = Array.isArray(data.domains) ? data.domains : [];
    domains = domains.concat(rows.map((row) => row.domain).filter(Boolean));
    hasMore = Boolean(data.has_more);
    offset += rows.length;
    if (!rows.length) break;
    guard += 1;
  }
  const cleanDomains = uniqueDomains(domains);
  if (kind === 'system') {
    if (!state.systemPresets[sourceScope]) state.systemPresets[sourceScope] = {};
    state.systemPresets[sourceScope][name] = cleanDomains;
    return state.systemPresets[sourceScope][name];
  }
  if (!state.customPresets[sourceScope]) state.customPresets[sourceScope] = {};
  state.customPresets[sourceScope][name] = cleanDomains;
  localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify(state.customPresets));
  return state.customPresets[sourceScope][name];
}
async function usePreset(target){
  const selected = el(`${target}-preset-select`).value;
  let domains = presetDomains(target, selected);
  if (selected.startsWith('custom:') || selected.startsWith('system:')) {
    const isSystem = selected.startsWith('system:');
    const cleanName = selected.slice((isSystem ? 'system:' : 'custom:').length);
    setMessage(isSystem ? 'Загружается системный список доменов' : 'Загружается пользовательский список доменов', 'warn');
    try {
      domains = await fetchAllPresetDomains(target, cleanName);
    } catch (error) {
      setMessage(`Ошибка загрузки списка: ${error.message}`, 'bad');
      return;
    }
  }
  const finalDomains = target === 'common' ? filterTestedDomains(domains) : domains;
  state.loadingDomainPreset = true;
  try {
    el(`${target}-domains`).value = uniqueDomains(finalDomains).join('\n');
    updateEditorLineNumbers(`${target}-domains`);
    if (target === 'finder') state.domainsTouched = true;
    if (target === 'common') {
      state.candidateResultRequested = false;
      prepareCommonCandidateState();
      renderCandidatesOnly();
      if (selectedCommonDomains().length >= 2) refreshCandidates(true);
    }
    else {
      renderCandidates();
      renderRunLaunchSummary();
    }
  } finally {
    state.loadingDomainPreset = false;
  }
}
function presetNameForSave(target){
  const nameInput = el(`${target}-preset-name`);
  const explicit = nameInput ? nameInput.value.trim() : '';
  if (explicit) return explicit;
  const selected = el(`${target}-preset-select`).value || '';
  if (selected.startsWith('custom:')) return selected.slice('custom:'.length);
  return '';
}
async function savePreset(target){
  const name = presetNameForSave(target);
  if (!name) {
    showToast('Укажите название пользовательского пресета', 'warn');
    return;
  }
  const domains = uniqueDomains(parseDomains(el(`${target}-domains`).value));
  if (!domains.length) {
    showToast('В пресете должен быть хотя бы один домен', 'warn');
    return;
  }
  try {
    const data = await postJson(apiEndpoint('web', 'presetSave'), { scope: target, name, domains });
    mergePresetResponse(data);
    state.customPresets[target][name] = domains;
    localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify(state.customPresets));
    renderPresetSelect(target);
    el(`${target}-preset-select`).value = `custom:${name}`;
    renderPresetManager();
    showToast('Пресет сохранен', 'good');
    if (target === 'common') {
      state.candidateResultRequested = false;
      refreshCandidates(true);
    }
    else renderCandidates();
  } catch (error) {
    showToast(`Ошибка сохранения пресета: ${error.message}`, 'bad');
  }
}
async function deletePreset(target){
  const selected = el(`${target}-preset-select`).value || '';
  if (!selected.startsWith('custom:')) {
    showToast('Этот пресет удалить нельзя', 'warn');
    return;
  }
  const name = selected.slice('custom:'.length);
  try {
    const data = await postJson(apiEndpoint('web', 'presetDeleteUserLists'), { scope: target, name });
    delete state.customPresets[target][name];
    mergePresetResponse(data);
    localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify(state.customPresets));
    renderPresetSelect(target);
    renderPresetManager();
    showToast('Пресет удален', 'good');
    if (target === 'common') {
      state.candidateResultRequested = false;
      refreshCandidates(true);
    }
  } catch (error) {
    showToast(`Ошибка удаления пресета: ${error.message}`, 'bad');
  }
}
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
