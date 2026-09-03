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
    setMessage(`Ошибка загрузки следующей страницы общих стратегий: ${error.message}`, 'bad');
    state.commonLoadingMore = false;
    renderCandidatesOnly();
  }
}
async function refreshCandidates(reset){
  const requestId = ++candidateRequestSeq;
  const offset = reset ? 0 : state.candidates.length;
  const queryKey = currentCandidateQueryKey();
  state.commonLoadingMore = false;
  state.candidateLoading = true;
  renderCandidatesOnly();
  try {
    const data = await getJson(apiUrl('web', 'strategyCandidatesPage', candidateParams(offset)));
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
function trimTextLines(text, maxLines){
  const lines = String(text || '').split('\n');
  if (lines.length <= maxLines) return lines.join('\n');
  return lines.slice(lines.length - maxLines).join('\n');
}
function appendLogText(base, addition){
  const left = String(base || '');
  const right = String(addition || '');
  if (!left || !right || left.endsWith('\n') || right.startsWith('\n')) return left + right;
  return `${left}\n${right}`;
}
function latestLogUrl(incremental){
  const busy = isBusy();
  const base = busy ? apiEndpoint('core', 'currentRunLatestLog') : apiEndpoint('core', 'latestLog');
  if (!incremental || !state.finderLog || !state.finderLog.stdout_log) {
    return base;
  }
  const params = new URLSearchParams();
  params.set('stdout_log', state.finderLog.stdout_log || '');
  params.set('stdout_size', String(state.finderLog.stdout_size || 0));
  params.set('stderr_log', state.finderLog.stderr_log || '');
  params.set('stderr_size', String(state.finderLog.stderr_size || 0));
  return `${base}?${params.toString()}`;
}
function mergeLogPayload(previous, next){
  if (!previous || !next) return next;
  if (next.progress) next.progress.received_at_ms = Date.now();
  const sameRun = previous.run_id && next.run_id && previous.run_id === next.run_id;
  const sameStdout = sameRun && previous.stdout_log && previous.stdout_log === next.stdout_log;
  const sameStderr = sameRun && previous.stderr_log && previous.stderr_log === next.stderr_log;
  if (sameStdout && next.stdout_append) {
    next.stdout_tail = trimTextLines(appendLogText(previous.stdout_tail, next.stdout_append), 200);
  }
  if (sameStderr && next.stderr_append) {
    next.stderr_tail = trimTextLines(appendLogText(previous.stderr_tail, next.stderr_append), 200);
  }
  if (sameStdout && !next.stdout_tail && !next.stdout_append) next.stdout_tail = previous.stdout_tail || '';
  if (sameStderr && !next.stderr_tail && !next.stderr_append) next.stderr_tail = previous.stderr_tail || '';
  return next;
}
function mergeStatusPayload(status){
  if (!status) return false;
  const previousSettings = JSON.stringify(state.settings || {});
  state.status = status;
  if (status.candidate_version) syncCandidateVersion(status.candidate_version);
  if (status.settings) state.settings = status.settings;
  if (status.run_preferences) state.runPreferences = status.run_preferences;
  renderMetrics();
  renderLiveRun();
  renderEvents();
  syncEngineUi();
  const settingsChanged = previousSettings !== JSON.stringify(state.settings || {});
  if (settingsChanged) renderSettings();
  return settingsChanged;
}
async function refreshRuns(reset = true){
  const offset = reset ? 0 : state.finderRunOffset;
  state.finderRunsLoading = true;
  renderRuns();
  try {
    const finderRuns = await getJson(apiUrl('web', 'runHistoryPage', runParams(offset)));
    mergeRunPage(finderRuns, reset);
    renderRuns();
    renderMetrics();
  } catch (error) {
    state.finderRunsLoading = false;
    renderRuns();
    setMessage(`Ошибка обновления истории: ${error.message}`, 'bad');
  }
}
async function refreshLog(incremental = false){
  try {
    const previous = state.finderLog;
    const payload = await getJson(latestLogUrl(incremental));
    if (payload.progress) payload.progress.received_at_ms = Date.now();
    state.finderLog = incremental ? mergeLogPayload(previous, payload) : payload;
    logDirty = false;
    renderLog();
    renderMetrics();
  } catch (error) {
    setMessage(`Ошибка обновления лога: ${error.message}`, 'bad');
  }
}
async function refreshPresets(){
  try {
    const presets = await getJson(apiEndpoint('web', 'presets'));
    mergePresetResponse(presets);
    renderPresetSelects();
    renderPresetManager();
  } catch (error) {
    setMessage(`Ошибка обновления пресетов: ${error.message}`, 'bad');
  }
}
