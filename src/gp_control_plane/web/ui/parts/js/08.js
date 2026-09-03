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
function formatDuration(seconds){
  if (!Number.isFinite(seconds)) return '-';
  if (seconds <= 0) return '0 мин';
  const minutes = Math.ceil(seconds / 60);
  if (minutes < 60) return `${minutes} мин`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} ч ${rest} мин` : `${hours} ч`;
}
function scrollLogToBottom(){
  const logNode = el('finder-log');
  if (!logNode) return;
  requestAnimationFrame(() => {
    logNode.scrollTop = logNode.scrollHeight;
  });
}
function renderAll(options){
  const opts = options || {};
  renderPresetSelects();
  renderSettings();
  useRunPreferencesOnce();
  if (!state.domainsInitialized && !state.domainsTouched && !el('finder-domains').value.trim() && state.domainSets) {
    const selected = el('finder-preset-select')?.value || 'system:required';
    const domains = uniqueDomains(presetDomains('finder', selected));
    el('finder-domains').value = domains.join('\n');
    state.domainsInitialized = true;
  }
  renderMetrics();
  renderRunLaunchSummary();
  if (!opts.skipCandidates) renderCandidates();
  renderRuns();
  renderLog();
  renderBackups();
  updateAllEditorLineNumbers();
  syncActiveTabUi();
}
function renderCandidatesOnly(){
  renderMetrics();
  renderCandidates();
  updateEditorLineNumbers('common-domains');
}
async function refreshBsDnsPins(force = false){
  const now = Date.now();
  if (!force && state.bsDnsPinsAt && now - state.bsDnsPinsAt < 20000) return;
  const box = el('bs-dns-pins-content');
  if (!box) return;
  state.bsDnsPinsAt = now;
  try {
    const data = await getJson(apiUrl('web', 'bsDnsPins'));
    const providers = Array.isArray(data.providers) ? data.providers : [];
    if (!providers.length) {
      box.textContent = 'Файлов hosts пока нет — нужен запуск blockcheckS с DNS/DoH-пинами (domain→IP против hijack).';
      return;
    }
    const NL = String.fromCharCode(10);
    const parts = [];
    for (const provider of providers) {
      parts.push(`# ${provider.provider} - ${provider.path}
${(provider.lines || []).join(NL)}`);
    }
    box.textContent = parts.join(String.fromCharCode(10, 10));
  } catch (error) {
    box.textContent = `Не удалось загрузить DNS-pins: ${error.message}`;
  }
}
async function refreshStrategyPairs(force = false){
  const now = Date.now();
  if (!force && state.strategyPairsAt && now - state.strategyPairsAt < 20000) return;
  const box = el('strategy-pairs-content');
  if (!box) return;
  state.strategyPairsAt = now;
  try {
    const data = await getJson(apiEndpoint('core', 'strategyPairs'));
    const pairs = Array.isArray(data.pairs) ? data.pairs : [];
    if (!pairs.length) {
      box.textContent = 'Рабочих пар нет — нужен запуск blockcheckS в режиме TCP + UDP/пары на UDP-блокнутом домене.';
      return;
    }
    const parts = [];
    for (const p of pairs) {
      parts.push(`tcp: ${p.tcp_args}
udp: ${p.udp_args}
${p.domain} - ${p.overall} (tcp ${p.tcp_ms}ms / udp ${p.udp_ms}ms)`);
    }
    box.textContent = parts.join(String.fromCharCode(10, 10));
  } catch (error) {
    box.textContent = 'Не удалось загрузить пары: ' + error.message;
  }
}
function ensureCandidateViewLoaded(){
  refreshBsDnsPins();
  refreshStrategyPairs();
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
  const requestId = ++domainIndexRequestSeq;
  const offset = reset ? 0 : state.candidateDomainOffset;
  state.candidateLoading = true;
  renderCandidatesOnly();
  try {
    const params = new URLSearchParams();
    params.set('limit', String(DOMAIN_PAGE_LIMIT));
    params.set('offset', String(Math.max(0, offset || 0)));
    const data = await getJson(apiUrl('web', 'candidateDomainIndexPage', params));
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
    if (requestId !== domainIndexRequestSeq) return;
    state.candidateLoading = false;
    renderCandidatesOnly();
    setMessage(`Ошибка загрузки доменов: ${error.message}`, 'bad');
  }
}
async function refreshDomainStrategies(domain, reset){
  const key = String(domain || '').trim();
  if (!key) return;
  const current = state.domainStrategies[key] || { candidates: [], total: 0, hasMore: false, loaded: false };
  const offset = reset ? 0 : current.candidates.length;
  try {
    const data = await getJson(apiUrl('web', 'strategyCandidatesPage', candidateParams(offset, { view: 'domain', domain: key })));
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
    setMessage(`Ошибка загрузки стратегий домена: ${error.message}`, 'bad');
  }
}
async function loadMoreDomainStrategies(domain){
  const key = String(domain || '').trim();
  if (!key) return;
  const current = state.domainStrategies[key] || { candidates: [], total: 0, hasMore: false, loaded: false };
  if (current.loadingMore || !current.hasMore) return;
  const candidates = Array.isArray(current.candidates) ? current.candidates.slice() : [];
  let total = Number(current.total || candidates.length);
  state.domainStrategies[key] = { ...current, candidates, total, hasMore: Boolean(current.hasMore), loaded: true, loadingMore: true };
  renderCandidatesOnly();
  try {
    const data = await getJson(apiUrl('web', 'strategyCandidatesPage', candidateParams(candidates.length, { view: 'domain', domain: key })));
    const rows = data.candidates || [];
    const nextCandidates = rows.length ? [...candidates, ...rows] : candidates;
    total = Number(data.total || total || nextCandidates.length);
    const hasMore = rows.length ? Boolean(data.has_more) : false;
    updateTestedDomains(data.tested_domains);
    rememberCandidateVersion(data.version || null);
    state.domainStrategies[key] = { candidates: nextCandidates, total, hasMore, loaded: true, loadingMore: false, version: state.candidateKnownVersion };
    renderCandidatesOnly();
  } catch (error) {
    state.domainStrategies[key] = { candidates, total, hasMore: Boolean(current.hasMore), loaded: true, loadingMore: false, version: state.candidateKnownVersion };
    setMessage(`Ошибка загрузки следующей страницы стратегий домена: ${error.message}`, 'bad');
    renderCandidatesOnly();
  }
}
async function loadMoreCommonStrategies(){
  if (state.commonLoadingMore || !state.candidateHasMore) return;
  const domains = selectedCommonDomains();
  if (domains.length < 2) return;
  const queryKey = currentCandidateQueryKey({ view: 'common', domains });
  const candidates = Array.isArray(state.candidates) ? state.candidates.slice() : [];
  state.commonLoadingMore = true;
  renderCandidatesOnly();
  try {
    const data = await getJson(apiUrl('web', 'strategyCandidatesPage', candidateParams(candidates.length, { view: 'common', domains })));
    if (state.candidateQueryKey !== queryKey) {
      state.commonLoadingMore = false;
      return;
    }
    const rows = data.candidates || [];
