const CUSTOM_PRESETS_KEY = 'gp-control-plane-domain-presets-v1';
const STRATEGY_LIST_LIMIT = 200;
const LIST_PAGE_LIMIT = 50;
const CANDIDATE_PAGE_LIMIT = LIST_PAGE_LIMIT;
const DOMAIN_PAGE_LIMIT = LIST_PAGE_LIMIT;
const RUN_PAGE_LIMIT = LIST_PAGE_LIMIT;
const CUSTOM_SELECT_VALUE = 'custom';
const DISCOVERY_PROFILES = {
  quick: { name: 'quick', title: 'Быстрый', scan_level: 'quick' },
  standard: { name: 'standard', title: 'Стандартный', scan_level: 'standard' },
  force: { name: 'force', title: 'Глубокий', scan_level: 'force' }
};
const state = { status: null, settings: null, settingsTouched: false, runPreferences: null, runPreferencesApplied: false, savingRunPreferences: false, releaseInfo: null, releaseStable: null, releasePrerelease: null, releaseChecked: false, releaseChecking: false, loadingDiscoveryProfile: false, loadingDomainPreset: false, loadingRunPreferences: false, discoveryProfiles: DISCOVERY_PROFILES, candidates: [], candidateTotal: 0, candidateOffset: 0, candidateHasMore: false, candidateVersion: null, candidateKnownVersion: null, candidateQueryKey: '', commonCandidateCache: {}, commonLoadingMore: false, candidateDomains: [], candidateDomainTotal: 0, candidateDomainStrategyTotal: 0, candidateDomainOffset: 0, candidateDomainHasMore: false, candidateDomainsLoaded: false, lastCandidateDomainTotal: 0, lastCandidateDomainStrategyTotal: 0, testedDomains: [], candidatesLoaded: false, candidateResultMode: 'balance', candidateResultRequested: false, domainStrategies: {}, finderRuns: [], finderRunTotal: 0, finderRunOffset: 0, finderRunHasMore: false, finderRunsLoaded: false, finderRunsLoading: false, finderLog: null, domainSets: null, domainSources: null, v2flyPreview: null, v2flyCategories: null, v2flyCategorySource: '', backups: [], backupsLoaded: false, cleanInstallVaults: [], cleanInstallVaultsLoaded: false, activeTab: 'finder', candidateView: 'domain', customPresets: loadCustomPresets(), customPresetMeta: { finder: {}, common: {} }, systemPresets: { finder: {}, common: {} }, systemPresetMeta: { finder: {}, common: {} }, presetManager: { scope: 'finder', name: '', query: '', domains: [], total: 0, hasMore: false, loading: false, loaded: false }, openCandidateDomains: {}, openCommonProtocols: {}, openRunDomains: {}, expandedStrategyLists: {}, strategyEditorScrolls: {}, domainsInitialized: false, domainsTouched: false, formMessage: 'Готово', formMessageTone: '' };
const jobNames = {
  'zapret-standard-discovery': 'Поиск стратегий',
  'zapret-multi-domain-discovery': 'Все домены на одной стратегии',
  'blockchecks-standard-discovery': 'Поиск стратегий (blockcheckS)',
  'blockchecks-multi-domain-discovery': 'Все домены на одной стратегии (blockcheckS)',
  'standard-discovery': 'Поиск стратегий',
  'multi-domain-discovery': 'Все домены на одной стратегии'
};
const statusTone = { success: 'good', failed: 'bad', error: 'bad', running: 'warn', queued: 'warn', stopping: 'warn', stopped: 'warn', timeout: 'warn' };
const AUTH_TOKEN_KEY = 'gp-control-plane-auth-token';
let toastTimer = null;
let refreshInFlight = false;
let realtimeSource = null;
let realtimeConnected = false;
let realtimeFallbackTimer = null;
let realtimeReconnectTimer = null;
let realtimeReconnectDelay = 1000;
let logDirty = false;
let candidateRefreshTimer = null;
let candidateRequestSeq = 0;
let domainIndexRequestSeq = 0;
state.candidateLoading = false;
state.candidateUpdatedAt = '';
state.backupsLoading = false;
state.backupsUpdatedAt = '';
state.cleanInstallVaultsLoading = false;
state.cleanInstallVaultsUpdatedAt = '';

const API_ENDPOINTS = Object.freeze({
  core: Object.freeze({
    status: '/api/core/status',
    startStrategyDiscoveryRun: '/api/core/strategy-discovery/start-run',
    preflight: '/api/core/strategy-discovery/preflight',
    currentRunLatestLog: '/api/core/strategy-discovery/current-run-latest-log',
    exportNfconf: '/api/core/strategy-discovery/export-nfconf',
    stopCurrentStrategyDiscoveryRun: '/api/core/strategy-discovery/stop-current-run',
    backupsList: '/api/core/backups/list',
    backupsCreate: '/api/core/backups/create',
    backupsRestore: '/api/core/backups/restore',
    backupsDelete: '/api/core/backups/delete',
    backupsDownloadArchive: '/api/core/backups/download-archive',
    backupsUpload: '/api/core/backups/upload',
    cleanInstallVaultsCreate: '/api/core/clean-install-vaults/create',
    cleanInstallVaultsList: '/api/core/clean-install-vaults/list',
    cleanInstallVaultsStatus: '/api/core/clean-install-vaults/status',
    cleanInstallVaultsRestore: '/api/core/clean-install-vaults/restore',
    runSettings: '/api/core/run-settings',
    saveRunSettings: '/api/core/run-settings/save',
    latestLog: '/api/core/runs/latest-log',
    v2flyCategories: '/api/core/presets/v2fly/categories',
    v2flyCategoryDomains: '/api/core/presets/v2fly/category-domains',
    strategyPairs: '/api/core/strategy-pairs'
  }),
  service: Object.freeze({
    releasesAvailable: '/api/service/releases/available',
    v2flyLocalStorageStatus: '/api/service/v2fly/local-storage-status'
  }),
  web: Object.freeze({
    runPreferences: '/api/web/run-preferences',
    runHistoryPage: '/api/web/runs/history-page',
    candidateDomainIndexPage: '/api/web/candidate-domain-index-page',
    strategyCandidatesPage: '/api/web/strategy-candidates-page',
    bsDnsPins: '/api/web/bs-dns-pins',
    presets: '/api/web/presets',
    presetDomains: '/api/web/presets/domains',
    presetSave: '/api/web/presets/save',
    presetDeleteUserLists: '/api/web/presets/delete-user-lists',
    events: '/api/web/events',
    eventsStream: '/api/web/events/stream'
  })
});

function el(id){ return document.getElementById(id); }
function esc(value){
  return String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[char]));
}
function setText(id, value){ el(id).textContent = value; }
function setMessage(text, tone){
  const node = el('message');
  state.formMessage = text || '';
  state.formMessageTone = tone || '';
  node.textContent = text;
  node.className = 'message' + (tone ? ' ' + tone : '');
  renderMetrics();
}
function showToast(text, tone){
  const node = el('toast');
  if (toastTimer) clearTimeout(toastTimer);
  node.textContent = text;
  node.className = 'toast' + (tone ? ' ' + tone : '');
  node.hidden = false;
  requestAnimationFrame(() => node.classList.add('show'));
  toastTimer = setTimeout(() => {
    node.classList.remove('show');
    toastTimer = setTimeout(() => {
      node.hidden = true;
      toastTimer = null;
    }, 180);
  }, 2000);
}
async function getJson(url){
  const response = await authFetch(url);
  if (!response.ok) throw new Error(await response.text());
  return await response.json();
}
async function postJson(url, payload){
  const response = await authFetch(url, {
    method: 'POST',
    headers: requestHeaders({'Content-Type': 'application/json'}),
    body: JSON.stringify(payload || {})
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const apiError = data && typeof data.error === 'object' ? data.error : {};
    const error = new Error(apiError.message || data.message || response.statusText);
    error.status = response.status;
    error.code = apiError.code || '';
    error.details = apiError.details || {};
    error.data = data;
    throw error;
  }
  return data;
}
function authToken(){
  return localStorage.getItem(AUTH_TOKEN_KEY) || '';
}
function requestHeaders(headers){
  const token = authToken();
  return {
    ...(headers || {}),
    ...(token ? { Authorization: `Bearer ${token}` } : {})
  };
}
function requestUrl(url){
  return url;
}
function storeAuthToken(payload){
  const token = String((payload || {}).access_token || (payload || {}).token || '').trim();
  if (!token) throw new Error('The server did not return an authorization token');
  localStorage.setItem(AUTH_TOKEN_KEY, token);
  return token;
}
function showLogin(message){
  el('app-shell').hidden = true;
  el('login-screen').hidden = false;
  el('login-error').textContent = message || '';
  requestAnimationFrame(() => el('login-username').focus());
}
function showApplication(){
  el('login-screen').hidden = true;
  el('app-shell').hidden = false;
}
function stopRealtimeEvents(){
  if (realtimeReconnectTimer) clearTimeout(realtimeReconnectTimer);
  realtimeReconnectTimer = null;
  if (realtimeSource) realtimeSource.abort();
  realtimeSource = null;
  realtimeConnected = false;
}
function renewRealtimeEvents(){
  stopRealtimeEvents();
  realtimeReconnectDelay = 1000;
  startRealtimeEvents({ alreadyStopped: true });
}function stopRealtimeFallback(){
  if (realtimeFallbackTimer) clearInterval(realtimeFallbackTimer);
  realtimeFallbackTimer = null;
}
function handleUnauthorized(){
  if (!authToken()) return;
  localStorage.removeItem(AUTH_TOKEN_KEY);
  stopRealtimeEvents();
  stopRealtimeFallback();
  showLogin('Your session has expired. Sign in again.');
}
function logout(){
  localStorage.removeItem(AUTH_TOKEN_KEY);
  stopRealtimeEvents();
  stopRealtimeFallback();
  showLogin();
}
async function authFetch(url, options){
  const request = options || {};
  const response = await fetch(url, {
    ...request,
    headers: requestHeaders(request.headers),
    credentials: 'same-origin'
  });
  if (response.status === 401) handleUnauthorized();
  return response;
}
function startAuthenticatedUi(){
  showApplication();
  refresh();
  startRealtimeEvents();
  startRealtimeFallback();
}
async function submitLogin(event){
  event.preventDefault();
  const errorNode = el('login-error');
  const form = el('login-form');
  const button = form.querySelector('button[type="submit"]');
  errorNode.textContent = '';
  button.disabled = true;
  try {
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ username: el('login-username').value, password: el('login-password').value })
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const apiError = data && typeof data.error === 'object' ? data.error : {};
      throw new Error(apiError.message || data.message || 'Unable to sign in');
    }
    storeAuthToken(data);
    startAuthenticatedUi();
  } catch (error) {
    errorNode.textContent = error.message || 'Unable to sign in';
  } finally {
    button.disabled = false;
  }
}
async function changePassword(){
  const form = el('change-password-form');
  const submitButton = form.querySelector('[type="submit"]');
  const status = el('change-password-status');
  const currentPassword = el('settings-current-password').value;
  const newPassword = el('settings-new-password').value;
  form.setAttribute('aria-busy', 'true');
  submitButton.disabled = true;
  status.textContent = 'Пароль изменяется…';
  try {
    await postJson('/api/auth/change-password', {
      current_password: currentPassword,
      new_password: newPassword
    });
    logout();
  } catch (error) {
    status.textContent = 'Не удалось изменить пароль. Проверьте текущий пароль и повторите попытку.';
  } finally {
    el('settings-current-password').value = '';
    el('settings-new-password').value = '';
    submitButton.disabled = false;
    form.removeAttribute('aria-busy');
  }
}function apiEndpoint(namespace, name){
  const group = API_ENDPOINTS[namespace] || {};
  const endpoint = group[name];
  if (!endpoint) throw new Error(`Unknown API endpoint: ${namespace}.${name}`);
  return endpoint;
}
function apiUrl(namespace, name, params){
  const endpoint = apiEndpoint(namespace, name);
  if (!params) return endpoint;
  const query = params instanceof URLSearchParams ? params.toString() : String(params || '');
  return query ? `${endpoint}?${query}` : endpoint;
}
function friendlyDate(value){
  if (!value) return '-';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('ru-RU');
}
function friendlyTime(value){
  if (!value) return '';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? '' : parsed.toLocaleTimeString('ru-RU');
}
function shortPath(value){
  if (!value) return '-';
  const parts = String(value).split(/[\\/]/).filter(Boolean);
  return parts.length > 3 ? '...' + parts.slice(-3).join('/') : String(value);
}
function badge(text, tone){
  return `<span class="badge ${esc(tone || '')}">${esc(text)}</span>`;
}
function table(targetId, columns, rows, emptyText){
  if (!rows.length) {
    el(targetId).innerHTML = `<div class="empty">${esc(emptyText)}</div>`;
    return;
  }
  const head = columns.map((column) => `<th>${esc(column.label)}</th>`).join('');
  const body = rows.map((row) => '<tr>' + columns.map((column) => {
    const value = column.render ? column.render(row) : esc(row[column.key]);
    return `<td>${value}</td>`;
  }).join('') + '</tr>').join('');
  el(targetId).innerHTML = `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}
function latestById(rows){
  const byId = new Map();
  rows.forEach((row, index) => {
    byId.set(row.id || `row-${index}`, row);
  });
  return Array.from(byId.values()).sort((a, b) => String(a.timestamp || '').localeCompare(String(b.timestamp || '')));
}
function listLoadMore(action, hasMore, loading){
  if (!hasMore) return '';
  const label = loading ? 'Загружается...' : 'Загрузить еще';
  const disabled = loading ? ' disabled' : '';
  return `<div class="button-row list-load-more"><button class="secondary" data-action="${esc(action)}" type="button"${disabled}>${label}</button></div>`;
}
function runParams(offset){
  const params = new URLSearchParams();
  params.set('limit', String(RUN_PAGE_LIMIT));
  params.set('offset', String(Math.max(0, offset || 0)));
  return params;
}
function mergeRunPage(payload, reset){
  const rows = latestById((payload || {}).runs || []);
  state.finderRuns = reset ? rows : latestById([...rows, ...state.finderRuns]);
  state.finderRunTotal = Number((payload || {}).total || state.finderRuns.length);
  state.finderRunOffset = Number((payload || {}).offset || 0) + ((payload || {}).runs || []).length;
  state.finderRunHasMore = Boolean((payload || {}).has_more);
  state.finderRunsLoaded = true;
  state.finderRunsLoading = false;
}
function syncActiveTabUi(){
  document.querySelectorAll('.tab-button[data-tab]').forEach((button) => {
    const active = button.dataset.tab === state.activeTab;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', active ? 'true' : 'false');
    button.tabIndex = active ? 0 : -1;
  });
  document.querySelectorAll('[data-tab-page]').forEach((page) => {
    const active = page.dataset.tabPage === state.activeTab;
    page.classList.toggle('active', active);
    page.hidden = !active;
  });
}
const TAB_NAVIGATION_KEYS = new Set(['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End']);
function tabControlsForButton(button){
  const tablist = button.closest('[role="tablist"]');
  if (!tablist) return [];
  return Array.from(tablist.querySelectorAll('[role="tab"]')).filter((item) => !item.disabled);
}
function activateTabControl(button){
  if (!button) return false;
  if (button.dataset.tab) {
    setActiveTab(button.dataset.tab);
    return true;
  }
  if (button.dataset.candidateView) {
    setCandidateView(button.dataset.candidateView);
    return true;
  }
  if (button.dataset.candidateResultMode) {
    state.candidateResultMode = button.dataset.candidateResultMode;
    renderCandidateResult();
    return true;
  }
  return false;
}
function handleTabControlKeydown(event){
  const button = event.target.closest('[role="tab"]');
  if (!button || !TAB_NAVIGATION_KEYS.has(event.key)) return false;
  const controls = tabControlsForButton(button);
  const index = controls.indexOf(button);
  if (index < 0) return false;
  let nextIndex = index;
  if (event.key === 'Home') nextIndex = 0;
  else if (event.key === 'End') nextIndex = controls.length - 1;
  else if (event.key === 'ArrowRight' || event.key === 'ArrowDown') nextIndex = (index + 1) % controls.length;
  else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') nextIndex = (index - 1 + controls.length) % controls.length;
  const nextButton = controls[nextIndex];
  if (!nextButton) return false;
  event.preventDefault();
  activateTabControl(nextButton);
  nextButton.focus();
  return true;
}
function setActiveTab(tabName){
  state.activeTab = tabName;
  syncActiveTabUi();
  if (tabName === 'terminal') {
    if (logDirty) refreshLog();
    scrollLogToBottom();
  }
  if (tabName === 'candidates') ensureCandidateViewLoaded();
  if (tabName === 'lists') {
    if (!state.v2flyCategories) loadV2flyCategories();
    loadPresetEditorFromSelection({ silent: true });
  }
  if (tabName === 'settings') {
    if (!mutatingBlocked() && !state.releaseChecked && !state.releaseChecking) checkReleases({ silent: true });
    if (!state.backupsLoaded) refreshBackups();
    if (!state.cleanInstallVaultsLoaded) refreshCleanInstallVaults();
  }
}
function latestRun(){
  return state.finderRuns.length ? state.finderRuns[state.finderRuns.length - 1] : null;
}
function currentRun(){
  const run = (state.status || {}).current_run;
  return run && typeof run === 'object' && run.run_id ? run : null;
}
function isBusy(){
  return Boolean(currentRun());
}
function mutatingBlocked(){
  return isBusy();
}
function mutatingBlockedMessage(){
  return 'Идет подбор. Дождитесь завершения или остановите текущий подбор перед изменениями.';
}
function requireNoActiveRun(){
  if (!mutatingBlocked()) return true;
  setMessage(mutatingBlockedMessage(), 'warn');
  showToast(mutatingBlockedMessage(), 'warn');
  return false;
}
function defaultDomains(kind){
  const sets = state.domainSets || {};
  if (kind === 'all') {
    return Object.values(sets).flat();
  }
  if (kind === 'tested') return testedDomains();
  return sets[kind] || [];
}
function uniqueDomains(domains){
  return [...new Set((Array.isArray(domains) ? domains : []).map((domain) => String(domain || '').trim()).filter(Boolean))];
}
function uniqueDomainCount(domains){
  return uniqueDomains(domains).length;
}
function fillDomains(kind){
  const domains = uniqueDomains(defaultDomains(kind));
  el('finder-domains').value = domains.join('\n');
  updateEditorLineNumbers('finder-domains');
  state.domainsTouched = true;
}
function finderDomains(){
  const raw = el('finder-domains').value.trim();
  return raw ? parseDomains(raw) : [];
}
function selectedFinderDomains(){
  const raw = el('finder-domains').value.trim();
  if (!raw) return [];
  return parseDomains(raw);
}
function timeoutSecondsOrNull(){
  if (!el('limit-time-enabled').checked) return null;
  const hours = Number(el('finder-timeout-hours').value || 6);
  return Math.max(60, Math.round(hours * 3600));
}
function syncTimeLimitUi(){
  const enabled = Boolean(el('limit-time-enabled')?.checked);
  const input = el('finder-timeout-hours');
  const field = el('time-limit-field');
  const panel = el('time-limit-panel');
  if (input) input.disabled = !enabled;
  if (field) field.setAttribute('aria-disabled', enabled ? 'false' : 'true');
  if (panel) panel.classList.toggle('disabled', !enabled);
}
function curlParallelism(){
  const value = Number(el('curl-parallelism').value || 4);
  const max = Number((state.settings || {}).curl_parallelism_max || 10);
  if (!Number.isFinite(value)) return 4;
  return Math.max(1, Math.min(max, Math.round(value)));
}
function repeatsValue(){
  const value = Number(el('repeats').value || 1);
  if (!Number.isFinite(value)) return 1;
  return Math.max(1, Math.min(10, Math.round(value)));
}
function minimumInputSeconds(id, fallback){
  const node = el(id);
