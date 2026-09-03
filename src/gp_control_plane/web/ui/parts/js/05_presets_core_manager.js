function loadCustomPresets(){
  try {
    const parsed = JSON.parse(localStorage.getItem(CUSTOM_PRESETS_KEY) || '{}');
    return {
      finder: parsed && typeof parsed.finder === 'object' && parsed.finder ? parsed.finder : {},
      common: parsed && typeof parsed.common === 'object' && parsed.common ? parsed.common : {}
    };
  } catch (_error) {
    return { finder: {}, common: {} };
  }
}
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
