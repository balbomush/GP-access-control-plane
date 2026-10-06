/* presets: explicit callbacks and a bounded view, no session/run owner. */
function createPresetsController({ view, CUSTOM_PRESETS_KEY, CUSTOM_SELECT_VALUE, apiEndpoint, apiUrl, currentSessionEpoch, defaultDomains, el, esc, filterTestedDomains, friendlyDate, getJson: requestJson, hasCompleteSystemStatus, isBusy, parseDomains, postJson: requestPost, prepareCommonCandidateState, refreshCandidates, renderCandidates, renderCandidatesOnly, renderRunLaunchSummary, resetCandidateResult, selectedCommonDomains, sessionIsCurrent, setMessage, setText, showToast, statusMarkup, testedDomains, uniqueDomainCount, uniqueDomains, updateEditorLineNumbers }) {
  const state = view;
  const lifetime = new UiLifetime();
  const getJson=(url,options)=>requestJson(url,lifetime.requestOptions(options));
  const postJson=(url,payload)=>requestPost(url,payload,lifetime.requestOptions());

  const setTimeout = lifetime.setTimeout.bind(lifetime);
  const clearTimeout = lifetime.clearTimeout.bind(lifetime);
  const requestAnimationFrame = lifetime.requestAnimationFrame.bind(lifetime);

function persistCustomPresets(){
  localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify(state.customPresets));
}

function mergeCustomPresets(remote, metadata){
  const result = { finder: {}, common: {} };
  for (const scope of ['finder', 'common']) {
    result[scope] = {
      ...((remote && typeof remote[scope] === 'object') ? remote[scope] : {}),
      ...((state.customPresets && typeof state.customPresets[scope] === 'object') ? state.customPresets[scope] : {})
    };
  }
  state.customPresets = result;
  state.customPresetMeta = normalizeCustomPresetMeta(metadata, state.customPresets);
  localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify(state.customPresets));
}

function mergeSystemPresets(remote, metadata){
  const result = { finder: {}, common: {} };
  for (const scope of ['finder', 'common']) {
    result[scope] = (remote && typeof remote[scope] === 'object' && remote[scope]) ? remote[scope] : {};
  }
  state.systemPresets = result;
  state.systemPresetMeta = normalizePresetMeta(metadata, state.systemPresets, 'system');
}

function normalizeCustomPresetMeta(metadata, presets){
  return normalizePresetMeta(metadata, presets, 'user');
}

function normalizePresetMeta(metadata, presets, fallbackKind){
  const result = { finder: {}, common: {} };
  for (const scope of ['finder', 'common']) {
    const remote = metadata && typeof metadata[scope] === 'object' ? metadata[scope] : {};
    Object.entries(remote).forEach(([name, meta]) => {
      result[scope][name] = {
        name,
        kind: (meta && meta.kind) || fallbackKind,
        label: (meta && meta.label) || name,
        enabled_count: Number((meta && meta.enabled_count) || 0),
        total_count: Number((meta && meta.total_count) || 0),
        updated_at: (meta && meta.updated_at) || ''
      };
    });
    Object.entries((presets && presets[scope]) || {}).forEach(([name, domains]) => {
      if (!result[scope][name]) {
        const count = uniqueDomainCount(domains);
        result[scope][name] = { name, kind: fallbackKind, label: name, enabled_count: count, total_count: count, updated_at: '' };
      }
    });
  }
  return result;
}

function customPresetNames(target){
  const scopes = presetScopesForTarget(target);
  return [...new Set([
    ...scopes.flatMap((scope) => Object.keys((state.customPresetMeta && state.customPresetMeta[scope]) || {})),
    ...scopes.flatMap((scope) => Object.keys((state.customPresets && state.customPresets[scope]) || {}))
  ])].filter((name) => !hasSystemPreset(target, name)).sort((a, b) => a.localeCompare(b));
}

function presetScopesForTarget(target){
  return target === 'common' ? ['common', 'finder'] : ['finder', 'common'];
}

function customPresetSourceScope(target, name){
  for (const scope of presetScopesForTarget(target)) {
    if ((state.customPresetMeta[scope] || {})[name] || (state.customPresets[scope] || {})[name]) return scope;
  }
  return target || 'finder';
}

function customPresetCount(target, name){
  const scope = customPresetSourceScope(target, name);
  const meta = (state.customPresetMeta[scope] || {})[name];
  if (meta) return Number(meta.enabled_count || 0);
  return uniqueDomainCount((state.customPresets[scope] || {})[name] || []);
}

function hasCustomPreset(target, name){
  const scope = customPresetSourceScope(target, name);
  return Boolean((state.customPresetMeta[scope] || {})[name] || (state.customPresets[scope] || {})[name]);
}

function systemPresetNames(target){
  return [...new Set([
    ...Object.keys((state.systemPresetMeta && state.systemPresetMeta[target]) || {}),
    ...Object.keys((state.systemPresets && state.systemPresets[target]) || {})
  ])].sort((a, b) => systemPresetLabel(target, a).localeCompare(systemPresetLabel(target, b)));
}

function systemPresetMeta(target, name){
  return ((state.systemPresetMeta && state.systemPresetMeta[target]) || {})[name] || null;
}

function systemPresetLabel(target, name){
  const meta = systemPresetMeta(target, name);
  return (meta && meta.label) || name;
}

function systemPresetCount(target, name){
  const meta = systemPresetMeta(target, name);
  if (meta) return Number(meta.enabled_count || 0);
  return uniqueDomainCount((state.systemPresets[target] || {})[name] || []);
}

function hasSystemPreset(target, name){
  return Boolean(systemPresetMeta(target, name) || (state.systemPresets[target] || {})[name]);
}

function mergePresetResponse(data){
  const payload = data || {};
  mergeCustomPresets(payload.custom || {}, payload.metadata || {});
  mergeSystemPresets(payload.system || {}, payload.system_metadata || {});
  if (payload.domain_sets && typeof payload.domain_sets === 'object') state.domainSets = payload.domain_sets;
  if (payload.builtin && typeof payload.builtin === 'object') state.domainSources = { builtin: payload.builtin };
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
  const viewRequest=lifetime.capture(null);
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
  const viewRequest=lifetime.capture(null);
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
    const data = await lifetime.result(getJson(apiUrl('web', 'presetDomains', params)), viewRequest);
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
  const viewRequest=lifetime.capture(`usePreset:${target}`);
  const selected = el(`${target}-preset-select`).value;
  let domains = presetDomains(target, selected);
  if (selected.startsWith('custom:') || selected.startsWith('system:')) {
    const isSystem = selected.startsWith('system:');
    const cleanName = selected.slice((isSystem ? 'system:' : 'custom:').length);
    setMessage(isSystem ? 'Загружается системный список доменов' : 'Загружается пользовательский список доменов', 'warn');
    try {
      domains = await lifetime.result(fetchAllPresetDomains(target, cleanName), viewRequest);
    } catch (error) {
    if(!lifetime.isCurrent(viewRequest)) return;
      setMessage(`Ошибка загрузки списка: ${error.message}`, 'bad');
      return;
    }
  }
  if (el(`${target}-preset-select`).value !== selected) return;
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
if(lifetime.isCurrent(viewRequest)){
    state.loadingDomainPreset = false;
  }
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
  const viewRequest=lifetime.capture(null);
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
    const data = await lifetime.result(postJson(apiEndpoint('web', 'presetSave'), { scope: target, name, domains }), viewRequest);
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
    if(!lifetime.isCurrent(viewRequest)) return;
    showToast(`Ошибка сохранения пресета: ${error.message}`, 'bad');
  }
}

async function deletePreset(target){
  const viewRequest=lifetime.capture(null);
  const selected = el(`${target}-preset-select`).value || '';
  if (!selected.startsWith('custom:')) {
    showToast('Этот пресет удалить нельзя', 'warn');
    return;
  }
  const name = selected.slice('custom:'.length);
  try {
    const data = await lifetime.result(postJson(apiEndpoint('web', 'presetDeleteUserLists'), { scope: target, name }), viewRequest);
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
    if(!lifetime.isCurrent(viewRequest)) return;
    showToast(`Ошибка удаления пресета: ${error.message}`, 'bad');
  }
}

function v2flyCategoryName(category){
  if (typeof category === 'string') return category;
  if (category && typeof category === 'object') return String(category.name || category.id || '').trim();
  return '';
}

function v2flyAllCategories(){
  const categories = (state.v2flyCategories || {}).categories;
  return Array.isArray(categories) ? categories.map(v2flyCategoryName).filter(Boolean) : [];
}

function v2flyCategoryQuery(){
  return String(el('v2fly-category-search')?.value || '').trim().toLowerCase();
}

function v2flyExactCategory(){
  const query = v2flyCategoryQuery();
  if (!query) return '';
  return v2flyAllCategories().includes(query) ? query : '';
}

function v2flyCategories(){
  const category = v2flyExactCategory();
  return category ? [category] : [];
}

function clearV2flyDomains(){
  const domains = el('v2fly-domains');
  if (!domains) return;
  domains.value = '';
  updateEditorLineNumbers('v2fly-domains');
}

function suggestV2flyPresetName(){
  const nameInput = el('v2fly-preset-name');
  if (!nameInput) return;
  const current = String(nameInput.value || '').trim();
  if (current && !current.startsWith('v2fly-')) return;
  const categories = v2flyCategories();
  if (!categories.length) return;
  nameInput.value = `v2fly-${categories.slice(0, 3).join('-')}`.slice(0, 80);
}

function v2flyPayload(){
  return {
    scope: 'finder',
    name: String(el('v2fly-preset-name')?.value || '').trim(),
    categories: v2flyCategories(),
    domains: parseDomains(el('v2fly-domains')?.value || '')
  };
}

function renderV2flyPreview(){
  const target = el('v2fly-preview-result');
  if (!target) return;
  const preview = state.v2flyPreview;
  target.classList.toggle('bad', Boolean(preview && preview.error));
  if (!preview) {
    target.textContent = 'Список не проверялся.';
    return;
  }
  if (preview.loading) {
    target.textContent = preview.message || 'Загружаю домены выбранной группы...';
    return;
  }
  if (preview.error) {
    target.innerHTML = statusMarkup(preview.message || 'Ошибка v2fly.', 'bad');
    return;
  }
  const added = Array.isArray(preview.added) ? preview.added.length : 0;
  const removed = Array.isArray(preview.removed) ? preview.removed.length : 0;
  const skipped = preview.skipped && typeof preview.skipped === 'object'
    ? Object.values(preview.skipped).reduce((sum, value) => sum + Number(value || 0), 0)
    : 0;
  const coverageNote = preview.coverage_note ? 'Публично известный проверяемый набор, не гарантия полного покрытия сервиса.' : '';
  target.innerHTML = [
    `<div><strong>${esc(preview.preset || '-')}</strong>: ${esc(preview.count || 0)} доменов</div>`,
    `<div>Добавится: ${esc(added)}, уйдет: ${esc(removed)}, без изменений: ${esc(preview.unchanged_count || 0)}</div>`,
    skipped ? `<div>Часть правил не добавлена автоматически: ${esc(skipped)}</div>` : '',
    coverageNote ? `<div>${esc(coverageNote)}</div>` : ''
  ].join('');
}

function setV2flyLocalError(message){
  state.v2flyPreview = { error: true, message };
  renderV2flyPreview();
}

function renderV2flyCategoryCatalog(){
  const target = el('v2fly-category-status');
  const data = state.v2flyCategories || {};
  const categories = v2flyAllCategories();
  const query = v2flyCategoryQuery();
  const visible = query ? categories.filter((category) => category.includes(query)) : categories;
  const options = el('v2fly-category-options');
  if (options) options.innerHTML = visible.slice(0, 500).map((category) => `<option value="${esc(category)}"></option>`).join('');
  const matchList = el('v2fly-category-matches');
  const exact = v2flyExactCategory();
  if (matchList) {
    const matches = visible.slice(0, 24);
    matchList.innerHTML = matches.length
      ? matches.map((category) => `<button class="secondary category-match${category === exact ? ' active' : ''}" type="button" data-action="v2fly-select-category" data-category="${esc(category)}">${esc(category)}</button>`).join('')
      : '';
  }
  const reloadButton = document.querySelector('[data-action="v2fly-load-categories"]');
  const loading = state.v2flyCategorySource === 'loading';
  const controlsBlocked = isBusy() || !hasCompleteSystemStatus();
  const updateButton = document.querySelector('[data-action="v2fly-update-local-storage"]');
  if (reloadButton) {
    reloadButton.disabled = loading || state.v2flyCatalogUpdateLoading || controlsBlocked;
    reloadButton.textContent = loading ? 'Читаю каталог' : 'Перечитать каталог';
    reloadButton.title = 'Перечитывает уже загруженный локальный каталог групп v2fly.';
  }
  if (updateButton) {
    updateButton.disabled = state.v2flyCatalogUpdateLoading || controlsBlocked;
    updateButton.textContent = state.v2flyCatalogUpdateLoading ? 'Загружаю каталог' : 'Загрузить/обновить каталог v2fly';
    updateButton.title = 'Загружает или обновляет локальный каталог групп v2fly.';
  }
  if (!target) return;
  if (state.v2flyCatalogUpdateLoading) {
    target.textContent = 'Загружаю и подготавливаю локальный каталог v2fly...';
    return;
  }
  if (loading) {
    target.textContent = 'Читаю локальный каталог v2fly...';
    return;
  }
  if (!categories.length) {
    target.textContent = data.error_message ? `Локальный каталог v2fly недоступен: ${data.error_message}` : 'Локальный каталог v2fly еще не подготовлен. Нажмите «Загрузить/обновить каталог v2fly».';
    return;
  }
  const selected = exact || '';
  const queryText = query ? ` Найдено по вводу: ${visible.length}.` : '';
  const selectText = selected ? ` Выбрано: ${selected}.` : (query ? ' Выберите точную группу из подсказок ниже.' : '');
  target.textContent = `Локальный каталог готов: ${data.all_count || categories.length} групп.${queryText}${selectText}`;
}

function presetManagerMeta(scope){
  return (state.customPresetMeta && state.customPresetMeta[scope]) || {};
}

function renderPresetManager(){
  const nameSelect = el('preset-manager-name');
  if (!nameSelect) return;
  const manager = state.presetManager;
  const scope = 'finder';
  const entries = managerPresetEntries();
  const names = entries.map((item) => item.name);
  if (!manager.name || !names.includes(manager.name)) manager.name = names[0] || '';
  const entry = manager.name ? managerPresetEntry(manager.name) : null;
  const isStoredUser = manager.name ? hasCustomPreset(scope, manager.name) : false;
  const isSystem = entry && entry.kind === 'system';
  const sourceScope = isStoredUser ? customPresetSourceScope(scope, manager.name) : scope;
  manager.scope = sourceScope;
  nameSelect.innerHTML = entries.length
    ? entries.map((item) => `<option value="${esc(item.name)}">${esc(item.label)} (${esc(item.count)})</option>`).join('')
    : '<option value="">Нет списков</option>';
  nameSelect.value = manager.name || '';
  const meta = isSystem ? systemPresetMeta(sourceScope, manager.name) : (isStoredUser ? presetManagerMeta(sourceScope)[manager.name] : null);
  const count = meta ? `${meta.enabled_count || 0}/${meta.total_count || 0}` : (entry ? `${entry.count}/${entry.count}` : '0');
  setText('preset-manager-count', count);
  const deleteButton = document.querySelector('button[data-action="preset-editor-delete"]');
  if (deleteButton) deleteButton.disabled = !isStoredUser || isSystem;
  const note = el('preset-manager-note');
  if (!manager.name) {
    note.textContent = 'Списков пока нет. Создайте список в подборе или импортируйте его из v2fly.';
    return;
  }
  const updated = meta && meta.updated_at ? ` · обновлено ${friendlyDate(meta.updated_at)}` : '';
  if (isSystem) {
    note.textContent = `Системный список "${entry.label}" всегда существует. Домены можно менять до пустого списка, удалить сам список нельзя. Доменов: ${meta ? meta.enabled_count : entry?.count || 0}${updated}.`;
    return;
  }
  note.textContent = `Редактируется список "${manager.name}". Доменов: ${meta ? meta.enabled_count : entry?.count || 0}${updated}${isStoredUser ? '' : ' · готовый список станет редактируемым после сохранения'}.`;
}

function renderPresetEditorPreview(preview){
  const target = el('preset-editor-preview');
  if (!target) return;
  if (!preview) {
    target.textContent = 'Изменения еще не проверялись.';
    return;
  }
  target.classList.toggle('bad', Boolean(preview.error));
  if (preview.error) {
    target.innerHTML = statusMarkup(preview.message || 'Ошибка списка.', 'bad');
    return;
  }
  target.innerHTML = [
    `<div><strong>${esc(preview.name)}</strong>: ${esc(preview.total)} уникальных доменов</div>`,
    `<div>Добавится: ${esc(preview.added)}, удалится: ${esc(preview.removed)}, без изменений: ${esc(preview.unchanged)}</div>`
  ].join('');
}

function presetEditorDomains(){
  return uniqueDomains(parseDomains(el('preset-editor-domains')?.value || ''));
}

function presetEditorScope(){
  return 'finder';
}

function presetEditorName(){
  return String(el('preset-manager-name')?.value || '').trim();
}

function presetEditorKind(){
  const entry = managerPresetEntry(presetEditorName());
  return entry && entry.kind === 'system' ? 'system' : 'user';
}

async function loadPresetEditorFromSelection(options){
  const viewRequest=lifetime.capture("loadPresetEditorFromSelection");
  const opts = options || {};
  const scope = presetEditorScope();
  const name = el('preset-manager-name')?.value || state.presetManager.name || '';
  if (!name) {
    if (!opts.silent) setMessage('Выберите список', 'warn');
    return;
  }
  try {
    const domains = await lifetime.result(fetchAllPresetDomains(scope, name), viewRequest);
    const domainsInput = el('preset-editor-domains');
    if (domainsInput) {
      domainsInput.value = domains.join('\n');
      updateEditorLineNumbers('preset-editor-domains');
    }
    renderPresetEditorPreview({ name, total: domains.length, added: 0, removed: 0, unchanged: domains.length });
    if (!opts.silent) setMessage('Список загружен в редактор', 'good');
  } catch (error) {
    if(!lifetime.isCurrent(viewRequest)) return;
    if (!opts.silent) setMessage(`Ошибка загрузки списка в редактор: ${error.message}`, 'bad');
  }
}

async function buildPresetEditorPreview(){
  const viewRequest=lifetime.capture("buildPresetEditorPreview");
  const scope = presetEditorScope();
  const name = presetEditorName();
  const kind = presetEditorKind();
  const domains = presetEditorDomains();
  if (!name || (!domains.length && kind !== 'system')) {
    setMessage(kind === 'system' ? 'Выберите список' : 'Выберите список и оставьте хотя бы один домен', 'warn');
    return null;
  }
  let current = [];
  if (hasCustomPreset(scope, name) || hasSystemPreset(scope, name) || managerPresetEntry(name)) {
    current = await lifetime.result(fetchAllPresetDomains(scope, name), viewRequest);
  }
  const currentSet = new Set(current);
  const nextSet = new Set(domains);
  const added = domains.filter((domain) => !currentSet.has(domain));
  const removed = current.filter((domain) => !nextSet.has(domain));
  const preview = {
    scope,
    name,
    kind,
    total: domains.length,
    added: added.length,
    removed: removed.length,
    unchanged: domains.length - added.length
  };
  renderPresetEditorPreview(preview);
  return preview;
}

async function savePresetEditor(){
  const viewRequest=lifetime.capture(null);
  try {
    const preview = await lifetime.result(buildPresetEditorPreview(), viewRequest);
    if (!preview) return;
    const domains = presetEditorDomains();
    const data = await lifetime.result(postJson(apiEndpoint('web', 'presetSave'), { scope: preview.scope, name: preview.name, kind: preview.kind, domains }), viewRequest);
    mergePresetResponse(data);
    if (preview.kind === 'system') {
      if (!state.systemPresets[preview.scope]) state.systemPresets[preview.scope] = {};
      state.systemPresets[preview.scope][preview.name] = domains;
    } else {
      if (!state.customPresets[preview.scope]) state.customPresets[preview.scope] = {};
      state.customPresets[preview.scope][preview.name] = domains;
      localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify(state.customPresets));
    }
    state.presetManager.scope = preview.scope;
    state.presetManager.name = preview.name;
    renderPresetSelects();
    renderPresetManager();
    setMessage('Список сохранен', 'good');
  } catch (error) {
    if(!lifetime.isCurrent(viewRequest)) return;
    setMessage(`Ошибка сохранения списка: ${error.message}`, 'bad');
  }
}

async function deletePresetEditor(){
  const viewRequest=lifetime.capture(null);
  const scope = presetEditorScope();
  const name = presetEditorName();
  const entry = managerPresetEntry(name);
  if (!name || !entry) {
    setMessage('Выберите пользовательский список', 'warn');
    return;
  }
  if (entry.kind !== 'user') {
    setMessage('Системные и готовые списки удалить нельзя', 'warn');
    return;
  }
  try {
    const data = await lifetime.result(postJson(apiEndpoint('web', 'presetDeleteUserLists'), { scope, name }), viewRequest);
    if (state.customPresets[scope]) delete state.customPresets[scope][name];
    mergePresetResponse(data);
    localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify(state.customPresets));
    state.presetManager.name = '';
    renderPresetSelects();
    renderPresetManager();
    await lifetime.result(loadPresetEditorFromSelection({ silent: true }), viewRequest);
    setMessage('Пользовательский список удален', 'good');
  } catch (error) {
    if(!lifetime.isCurrent(viewRequest)) return;
    setMessage(`Ошибка удаления списка: ${error.message}`, 'bad');
  }
}

function presetNewName(){
  return String(el('preset-new-name')?.value || '').trim();
}

function presetNewDomains(){
  return uniqueDomains(parseDomains(el('preset-new-domains')?.value || ''));
}

function renderPresetNewPreview(message, tone){
  const target = el('preset-new-preview');
  if (!target) return;
  target.classList.toggle('bad', tone === 'bad');
  if (tone === 'bad') {
    target.innerHTML = statusMarkup(message || 'Ошибка списка.', 'bad');
    return;
  }
  target.textContent = message || 'Новый список еще не сохранялся.';
}

async function savePresetNew(){
  const viewRequest=lifetime.capture(null);
  const scope = 'finder';
  const name = presetNewName();
  const domains = presetNewDomains();
  if (!name || !domains.length) {
    renderPresetNewPreview('Укажите название нового списка и хотя бы один домен.', 'bad');
    setMessage('Укажите название нового списка и хотя бы один домен', 'warn');
    return;
  }
  if (hasSystemPreset(scope, name)) {
    renderPresetNewPreview('Это имя занято системным списком.', 'bad');
    setMessage('Это имя занято системным списком', 'warn');
    return;
  }
  try {
    const data = await lifetime.result(postJson(apiEndpoint('web', 'presetSave'), { scope, name, domains }), viewRequest);
    mergePresetResponse(data);
    if (!state.customPresets[scope]) state.customPresets[scope] = {};
    state.customPresets[scope][name] = domains;
    localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify(state.customPresets));
    state.presetManager.scope = scope;
    state.presetManager.name = name;
    const nameInput = el('preset-new-name');
    const domainsInput = el('preset-new-domains');
    if (nameInput) nameInput.value = '';
    if (domainsInput) {
      domainsInput.value = '';
      updateEditorLineNumbers('preset-new-domains');
    }
    renderPresetSelects();
    renderPresetManager();
    await lifetime.result(loadPresetEditorFromSelection({ silent: true }), viewRequest);
    renderPresetNewPreview(`Список сохранен: ${name}, доменов ${domains.length}.`, 'good');
    setMessage('Новый список сохранен', 'good');
  } catch (error) {
    if(!lifetime.isCurrent(viewRequest)) return;
    renderPresetNewPreview(`Ошибка сохранения: ${error.message}`, 'bad');
    setMessage(`Ошибка сохранения нового списка: ${error.message}`, 'bad');
  }
}

async function exportPresetEditor(){
  const viewRequest=lifetime.capture(null);
  try {
    let domains = presetEditorDomains();
    const scope = presetEditorScope();
    const name = presetEditorName() || el('preset-manager-name')?.value || 'domains';
    if (!domains.length && name) domains = await lifetime.result(fetchAllPresetDomains(scope, name), viewRequest);
    if (!domains.length) {
      setMessage('Нет доменов для экспорта', 'warn');
      return;
    }
    const blob = new Blob([domains.join('\n') + '\n'], { type: 'text/plain;charset=utf-8' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `${name.replace(/[^a-z0-9._-]+/gi, '-') || 'domains'}.txt`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(link.href);
    setMessage('TXT сформирован', 'good');
  } catch (error) {
    if(!lifetime.isCurrent(viewRequest)) return;
    setMessage(`Ошибка экспорта списка: ${error.message}`, 'bad');
  }
}

async function loadV2flyCategories(refreshCatalog, { throwOnError = false } = {}){
  const viewRequest=lifetime.capture("loadV2flyCategories");
  state.v2flyCategorySource = 'loading';
  renderV2flyCategoryCatalog();
  try {
    const params = new URLSearchParams();
    params.set('limit', '5000');
    const data = await lifetime.result(getJson(apiUrl('core', 'v2flyCategories', params)), viewRequest);
    state.v2flyCategories = data;
    state.v2flyCategorySource = (data.storage && data.storage.source) || data.source || '';
    renderV2flyCategoryCatalog();
  } catch (error) {
    if(!lifetime.isCurrent(viewRequest)) return;
    state.v2flyCategories = { categories: [], error_message: error.message };
    state.v2flyCategorySource = '';
    renderV2flyCategoryCatalog();
    setV2flyLocalError(`Не удалось прочитать локальный каталог v2fly: ${error.message}`);
    if (throwOnError) throw error;
  }
}

async function updateV2flyLocalStorage(){
  const viewRequest=lifetime.capture(null);
  if (state.v2flyCatalogUpdateLoading) return;
  state.v2flyCatalogUpdateLoading = true;
  renderV2flyCategoryCatalog();
  try {
    const result = await lifetime.result(postJson(apiEndpoint('service', 'v2flyUpdateLocalStorage'), {}), viewRequest);
    await lifetime.result(loadV2flyCategories(true, { throwOnError: true }), viewRequest);
    const groups = Number(result?.storage?.group_count || result?.result?.categories?.length || v2flyAllCategories().length || 0);
    const revisionWarning = String(result?.result?.revision_warning || '').trim();
    if (revisionWarning) {
      setMessage(`Каталог v2fly готов: ${groups} групп, но ревизия источника не подтверждена: ${revisionWarning}`, 'warn');
    } else {
      setMessage(`Каталог v2fly обновлен: ${groups} групп`, 'good');
    }
  } catch (error) {
    if(!lifetime.isCurrent(viewRequest)) return;
    const message = error.message || 'Неизвестная ошибка';
    setV2flyLocalError(`Не удалось загрузить каталог v2fly: ${message}`);
    setMessage(`Ошибка загрузки v2fly: ${message}`, 'bad');
  } finally {
if(lifetime.isCurrent(viewRequest)){
    state.v2flyCatalogUpdateLoading = false;
    renderV2flyCategoryCatalog();
  }
}
}

async function fetchV2flyCategoryDomains(categories){
  const viewRequest=lifetime.capture(null);
  let domains = [];
  for (const category of categories) {
    const params = new URLSearchParams();
    params.set('category', category);
    const data = await lifetime.result(getJson(apiUrl('core', 'v2flyCategoryDomains', params)), viewRequest);
    domains = domains.concat(Array.isArray(data.domains) ? data.domains : []);
  }
  return uniqueDomains(domains);
}

async function buildV2flyClientPreview(payload, domains){
  const viewRequest=lifetime.capture(null);
  const cleanDomains = uniqueDomains(domains);
  let existing = [];
  if (payload.name && hasCustomPreset('finder', payload.name)) {
    existing = await lifetime.result(fetchAllPresetDomains('finder', payload.name), viewRequest);
  }
  const existingSet = new Set(existing);
  const incomingSet = new Set(cleanDomains);
  return {
    scope: 'finder',
    preset: payload.name,
    kind: 'user',
    coverage_note: true,
    categories: payload.categories,
    sources: {},
    skipped: {},
    domains: cleanDomains,
    count: cleanDomains.length,
    existing_count: existing.length,
    added: cleanDomains.filter((domain) => !existingSet.has(domain)),
    removed: existing.filter((domain) => !incomingSet.has(domain)),
    unchanged_count: existing.filter((domain) => incomingSet.has(domain)).length
  };
}

async function previewV2flyPreset(){
  const viewRequest=lifetime.capture("previewV2flyPreset");
  const payload = v2flyPayload();
  if (!payload.name) {
    setV2flyLocalError('Укажите название пресета.');
    return;
  }
  if (!v2flyAllCategories().length) {
    setV2flyLocalError('Локальный каталог v2fly не подготовлен. Нажмите «Загрузить/обновить каталог v2fly».');
    return;
  }
  if (!payload.categories.length) {
    setV2flyLocalError('Выберите точное название группы v2fly из подсказок.');
    return;
  }
  state.v2flyPreview = { loading: true, message: 'Загружаю домены выбранной группы...' };
  renderV2flyPreview();
  try {
    const domains = await lifetime.result(fetchV2flyCategoryDomains(payload.categories), viewRequest);
    const preview = await lifetime.result(buildV2flyClientPreview(payload, domains), viewRequest);
    state.v2flyPreview = preview;
    if (Array.isArray(preview.domains)) {
      el('v2fly-domains').value = preview.domains.join('\n');
      updateEditorLineNumbers('v2fly-domains');
    }
    renderV2flyPreview();
    setMessage('Список v2fly проверен', 'good');
  } catch (error) {
    if(!lifetime.isCurrent(viewRequest)) return;
    setV2flyLocalError(`Ошибка проверки v2fly: ${error.message}`);
  }
}

async function importV2flyPreset(){
  const viewRequest=lifetime.capture(null);
  const payload = v2flyPayload();
  if (!payload.name) {
    setV2flyLocalError('Укажите название пресета.');
    return;
  }
  if (!v2flyAllCategories().length) {
    setV2flyLocalError('Локальный каталог v2fly не подготовлен. Нажмите «Загрузить/обновить каталог v2fly».');
    return;
  }
  if (!payload.categories.length) {
    setV2flyLocalError('Выберите точное название группы v2fly из подсказок.');
    return;
  }
  state.v2flyPreview = { loading: true, message: 'Сохраняю доменный пресет...' };
  renderV2flyPreview();
  try {
    const domains = payload.domains.length ? payload.domains : await lifetime.result(fetchV2flyCategoryDomains(payload.categories), viewRequest);
    const preview = await lifetime.result(buildV2flyClientPreview(payload, domains), viewRequest);
    const data = await lifetime.result(postJson(apiEndpoint('web', 'presetSave'), { scope: 'finder', name: payload.name, domains: preview.domains }), viewRequest);
    state.v2flyPreview = preview;
    mergePresetResponse(data);
    if (!state.customPresets.finder) state.customPresets.finder = {};
    state.customPresets.finder[payload.name] = preview.domains;
    localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify(state.customPresets));
    state.presetManager.scope = 'finder';
    state.presetManager.name = payload.name;
    renderPresetSelects();
    renderPresetManager();
    if (Array.isArray(preview.domains)) {
      el('v2fly-domains').value = preview.domains.join('\n');
      updateEditorLineNumbers('v2fly-domains');
    }
    renderV2flyPreview();
    await lifetime.result(loadPresetEditorFromSelection({ silent: true }), viewRequest);
    setMessage(`Пресет сохранен: ${preview.count || 0} доменов`, 'good');
  } catch (error) {
    if(!lifetime.isCurrent(viewRequest)) return;
    setV2flyLocalError(`Ошибка сохранения v2fly: ${error.message}`);
  }
}

async function refreshPresets(){
  const viewRequest=lifetime.capture(null);
  const epoch = currentSessionEpoch();
  try {
    const presets = await lifetime.result(getJson(apiEndpoint('web', 'presets')), viewRequest);
    if (!sessionIsCurrent(epoch)) return;
    mergePresetResponse(presets);
    renderPresetSelects();
    renderPresetManager();
  } catch (error) {
    if(!lifetime.isCurrent(viewRequest)) return;
    if (!sessionIsCurrent(epoch)) return;
    setMessage(`Ошибка обновления пресетов: ${error.message}`, 'bad');
  }
}
  function dispose(){ lifetime.dispose();  }
function handleClickPart0(button){
if (button.dataset.action === 'v2fly-load-categories') {
    loadV2flyCategories(true);
    return true;
  }
 return false;
}

function handleClickPart1(button){
if (button.dataset.action === 'v2fly-update-local-storage') {
    updateV2flyLocalStorage();
    return true;
  }
 return false;
}

function handleClickPart2(button){
if (button.dataset.action === 'v2fly-select-category') {
    const category = button.dataset.category || '';
    const input = el('v2fly-category-search');
    if (input) input.value = category;
    state.v2flyPreview = null;
    clearV2flyDomains();
    suggestV2flyPresetName();
    renderV2flyCategoryCatalog();
    renderV2flyPreview();
    return true;
  }
 return false;
}

function handleClickPart3(button){
if (button.dataset.action === 'v2fly-preview') {
    previewV2flyPreset();
    return true;
  }
 return false;
}

function handleClickPart4(button){
if (button.dataset.action === 'v2fly-import') {
    importV2flyPreset();
    return true;
  }
 return false;
}

function handleClickPart5(button){
if (button.dataset.action === 'preset-editor-save') {
    savePresetEditor();
    return true;
  }
 return false;
}

function handleClickPart6(button){
if (button.dataset.action === 'preset-editor-delete') {
    deletePresetEditor();
    return true;
  }
 return false;
}

function handleClickPart7(button){
if (button.dataset.action === 'preset-editor-export') {
    exportPresetEditor();
    return true;
  }
 return false;
}

function handleClickPart8(button){
if (button.dataset.action === 'preset-new-save') {
    savePresetNew();
    return true;
  }
 return false;
}

function handleClickPart9(button){
if (button.dataset.presetSave) {
    savePreset(button.dataset.presetSave);
    return true;
  }
 return false;
}

function handleClickPart10(button){
if (button.dataset.presetDelete) {
    deletePreset(button.dataset.presetDelete);
    return true;
  }
 return false;
}

function handleInputPart11(event){
if (event.target && String(event.target.id || '').startsWith('v2fly-')) {
    if (event.target.id === 'v2fly-category-search') {
      clearV2flyDomains();
      suggestV2flyPresetName();
      renderV2flyCategoryCatalog();
    }
    if (event.target.id === 'v2fly-domains') updateEditorLineNumbers('v2fly-domains');
    state.v2flyPreview = null;
    renderV2flyPreview();
  }
 return false;
}

function handleChangePart12(event){
if (event.target && String(event.target.id || '').startsWith('v2fly-')) {
    if (event.target.id === 'v2fly-category-search') {
      clearV2flyDomains();
      suggestV2flyPresetName();
      renderV2flyCategoryCatalog();
    }
    if (event.target.id === 'v2fly-domains') updateEditorLineNumbers('v2fly-domains');
    state.v2flyPreview = null;
    renderV2flyPreview();
  }
 return false;
}

function handleChangePart13(event){
if (event.target && (event.target.id === 'finder-preset-select' || event.target.id === 'common-preset-select')) {
    const target = event.target.id.startsWith('finder') ? 'finder' : 'common';
    const value = event.target.value || '';
    const nameInput = el(`${target}-preset-name`);
    if (nameInput) nameInput.value = value === CUSTOM_SELECT_VALUE ? 'custom' : (value.startsWith('custom:') ? value.slice('custom:'.length) : '');
    if (value !== CUSTOM_SELECT_VALUE) usePreset(target);
  }
 return false;
}

function handleChangePart14(event){
if (event.target && event.target.id === 'preset-manager-name') {
    state.presetManager.name = event.target.value || '';
    renderPresetManager();
    loadPresetEditorFromSelection({ silent: true });
  }
 return false;
}
  return { handleClickPart0, handleClickPart1, handleClickPart2, handleClickPart3, handleClickPart4, handleClickPart5, handleClickPart6, handleClickPart7, handleClickPart8, handleClickPart9, handleClickPart10, handleInputPart11, handleChangePart12, handleChangePart13, handleChangePart14, persistCustomPresets, mergeCustomPresets, mergeSystemPresets, normalizeCustomPresetMeta, normalizePresetMeta, customPresetNames, presetScopesForTarget, customPresetSourceScope, customPresetCount, hasCustomPreset, systemPresetNames, systemPresetMeta, systemPresetLabel, systemPresetCount, hasSystemPreset, mergePresetResponse, builtInPresets, presetGroups, presetDomains, managerPresetEntries, managerPresetEntry, renderPresetSelect, renderPresetSelects, markDomainPresetCustom, fetchAllPresetDomains: lifetime.action(fetchAllPresetDomains), fetchStoredPresetDomains: lifetime.action(fetchStoredPresetDomains), usePreset: lifetime.action(usePreset), presetNameForSave, savePreset: lifetime.action(savePreset), deletePreset: lifetime.action(deletePreset), v2flyCategoryName, v2flyAllCategories, v2flyCategoryQuery, v2flyExactCategory, v2flyCategories, clearV2flyDomains, suggestV2flyPresetName, v2flyPayload, renderV2flyPreview, setV2flyLocalError, renderV2flyCategoryCatalog, presetManagerMeta, renderPresetManager, renderPresetEditorPreview, presetEditorDomains, presetEditorScope, presetEditorName, presetEditorKind, loadPresetEditorFromSelection: lifetime.action(loadPresetEditorFromSelection), buildPresetEditorPreview: lifetime.action(buildPresetEditorPreview), savePresetEditor: lifetime.action(savePresetEditor), deletePresetEditor: lifetime.action(deletePresetEditor), presetNewName, presetNewDomains, renderPresetNewPreview, savePresetNew: lifetime.action(savePresetNew), exportPresetEditor: lifetime.action(exportPresetEditor), loadV2flyCategories: lifetime.action(loadV2flyCategories), updateV2flyLocalStorage: lifetime.action(updateV2flyLocalStorage), fetchV2flyCategoryDomains: lifetime.action(fetchV2flyCategoryDomains), buildV2flyClientPreview: lifetime.action(buildV2flyClientPreview), previewV2flyPreset: lifetime.action(previewV2flyPreset), importV2flyPreset: lifetime.action(importV2flyPreset), refreshPresets: lifetime.action(refreshPresets), dispose };
}
