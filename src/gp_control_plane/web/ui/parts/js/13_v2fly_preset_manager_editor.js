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
    target.textContent = preview.message || 'Ошибка v2fly.';
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
  const button = document.querySelector('[data-action="v2fly-load-categories"]');
  const loading = state.v2flyCategorySource === 'loading';
  if (button) {
    button.disabled = loading;
    button.textContent = loading ? 'Читаю каталог' : 'Перечитать каталог';
    button.title = 'Перечитывает локальный каталог групп v2fly. Каталог скачивается при установке или обновлении сервиса.';
  }
  if (!target) return;
  if (loading) {
    target.textContent = 'Читаю локальный каталог v2fly...';
    return;
  }
  if (!categories.length) {
    target.textContent = data.error_message ? `Локальный каталог v2fly недоступен: ${data.error_message}` : 'Локальный каталог v2fly еще не подготовлен. Он скачивается при установке или обновлении сервиса.';
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
  const opts = options || {};
  const scope = presetEditorScope();
  const name = el('preset-manager-name')?.value || state.presetManager.name || '';
  if (!name) {
    if (!opts.silent) setMessage('Выберите список', 'warn');
    return;
  }
  try {
    const domains = await fetchAllPresetDomains(scope, name);
    const domainsInput = el('preset-editor-domains');
    if (domainsInput) {
      domainsInput.value = domains.join('\n');
      updateEditorLineNumbers('preset-editor-domains');
    }
    renderPresetEditorPreview({ name, total: domains.length, added: 0, removed: 0, unchanged: domains.length });
    if (!opts.silent) setMessage('Список загружен в редактор', 'good');
  } catch (error) {
    if (!opts.silent) setMessage(`Ошибка загрузки списка в редактор: ${error.message}`, 'bad');
  }
}
async function buildPresetEditorPreview(){
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
    current = await fetchAllPresetDomains(scope, name);
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
  try {
    const preview = await buildPresetEditorPreview();
    if (!preview) return;
    const domains = presetEditorDomains();
    const data = await postJson(apiEndpoint('web', 'presetSave'), { scope: preview.scope, name: preview.name, kind: preview.kind, domains });
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
    setMessage(`Ошибка сохранения списка: ${error.message}`, 'bad');
  }
}
async function deletePresetEditor(){
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
    const data = await postJson(apiEndpoint('web', 'presetDeleteUserLists'), { scope, name });
    if (state.customPresets[scope]) delete state.customPresets[scope][name];
    mergePresetResponse(data);
    localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify(state.customPresets));
    state.presetManager.name = '';
    renderPresetSelects();
    renderPresetManager();
    await loadPresetEditorFromSelection({ silent: true });
    setMessage('Пользовательский список удален', 'good');
  } catch (error) {
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
  target.textContent = message || 'Новый список еще не сохранялся.';
  target.classList.toggle('bad', tone === 'bad');
}
async function savePresetNew(){
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
    const data = await postJson(apiEndpoint('web', 'presetSave'), { scope, name, domains });
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
    await loadPresetEditorFromSelection({ silent: true });
    renderPresetNewPreview(`Список сохранен: ${name}, доменов ${domains.length}.`, 'good');
    setMessage('Новый список сохранен', 'good');
  } catch (error) {
    renderPresetNewPreview(`Ошибка сохранения: ${error.message}`, 'bad');
    setMessage(`Ошибка сохранения нового списка: ${error.message}`, 'bad');
  }
}
async function exportPresetEditor(){
  try {
    let domains = presetEditorDomains();
    const scope = presetEditorScope();
    const name = presetEditorName() || el('preset-manager-name')?.value || 'domains';
    if (!domains.length && name) domains = await fetchAllPresetDomains(scope, name);
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
    setMessage(`Ошибка экспорта списка: ${error.message}`, 'bad');
  }
}
async function loadV2flyCategories(refreshCatalog){
  state.v2flyCategorySource = 'loading';
  renderV2flyCategoryCatalog();
  try {
    const params = new URLSearchParams();
    params.set('limit', '5000');
    const data = await getJson(apiUrl('core', 'v2flyCategories', params));
    state.v2flyCategories = data;
    state.v2flyCategorySource = (data.storage && data.storage.source) || data.source || '';
    renderV2flyCategoryCatalog();
  } catch (error) {
    state.v2flyCategories = { categories: [], error_message: error.message };
    state.v2flyCategorySource = '';
    renderV2flyCategoryCatalog();
    setV2flyLocalError(`Не удалось прочитать локальный каталог v2fly: ${error.message}`);
  }
}
async function fetchV2flyCategoryDomains(categories){
  let domains = [];
  for (const category of categories) {
    const params = new URLSearchParams();
    params.set('category', category);
    const data = await getJson(apiUrl('core', 'v2flyCategoryDomains', params));
    domains = domains.concat(Array.isArray(data.domains) ? data.domains : []);
  }
  return uniqueDomains(domains);
}
async function buildV2flyClientPreview(payload, domains){
  const cleanDomains = uniqueDomains(domains);
  let existing = [];
  if (payload.name && hasCustomPreset('finder', payload.name)) {
    existing = await fetchAllPresetDomains('finder', payload.name);
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
  const payload = v2flyPayload();
  if (!payload.name) {
    setV2flyLocalError('Укажите название пресета.');
    return;
  }
  if (!v2flyAllCategories().length) {
    setV2flyLocalError('Локальный каталог v2fly не подготовлен. Повторите установку или обновление сервиса.');
    return;
  }
  if (!payload.categories.length) {
    setV2flyLocalError('Выберите точное название группы v2fly из подсказок.');
    return;
  }
  state.v2flyPreview = { loading: true, message: 'Загружаю домены выбранной группы...' };
  renderV2flyPreview();
  try {
    const domains = await fetchV2flyCategoryDomains(payload.categories);
    const preview = await buildV2flyClientPreview(payload, domains);
    state.v2flyPreview = preview;
    if (Array.isArray(preview.domains)) {
      el('v2fly-domains').value = preview.domains.join('\n');
      updateEditorLineNumbers('v2fly-domains');
    }
    renderV2flyPreview();
    setMessage('Список v2fly проверен', 'good');
  } catch (error) {
    setV2flyLocalError(`Ошибка проверки v2fly: ${error.message}`);
  }
}
async function importV2flyPreset(){
  const payload = v2flyPayload();
  if (!payload.name) {
    setV2flyLocalError('Укажите название пресета.');
    return;
  }
  if (!v2flyAllCategories().length) {
    setV2flyLocalError('Локальный каталог v2fly не подготовлен. Повторите установку или обновление сервиса.');
    return;
  }
  if (!payload.categories.length) {
    setV2flyLocalError('Выберите точное название группы v2fly из подсказок.');
    return;
  }
  state.v2flyPreview = { loading: true, message: 'Сохраняю доменный пресет...' };
  renderV2flyPreview();
  try {
    const domains = payload.domains.length ? payload.domains : await fetchV2flyCategoryDomains(payload.categories);
    const preview = await buildV2flyClientPreview(payload, domains);
    const data = await postJson(apiEndpoint('web', 'presetSave'), { scope: 'finder', name: payload.name, domains: preview.domains });
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
    await loadPresetEditorFromSelection({ silent: true });
    setMessage(`Пресет сохранен: ${preview.count || 0} доменов`, 'good');
  } catch (error) {
    setV2flyLocalError(`Ошибка сохранения v2fly: ${error.message}`);
  }
}
