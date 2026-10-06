/* Application shell: construction, session, tabs, common notifications and wiring. */
const uiControllers = Object.create(null);
const shellViewLifetime = new UiLifetime();
const applicationListeners = [];
function listen(target,type,callback,options){
  target.addEventListener(type,callback,options);
  applicationListeners.push(()=>target.removeEventListener(type,callback,options));
}
function teardownViews(){
  Object.values(uiControllers).forEach(controller=>controller.dispose());
  shellViewLifetime.dispose();clearTimeout(toastTimer);clearInitialSystemStatusRetry();
}
function scopedView(source, keys){
  const view = Object.create(null);
  for(const key of keys) Object.defineProperty(view,key,{enumerable:true,get:()=>source[key],set:value=>{source[key]=value;}});
  return Object.seal(view);
}

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
const state = { status: null, statusLoading: false, settings: null, settingsTouched: false, runPreferences: null, runPreferencesApplied: false, savingRunPreferences: false, releaseInfo: null, releaseStable: null, releasePrerelease: null, releaseChecked: false, releaseChecking: false, loadingDiscoveryProfile: false, loadingDomainPreset: false, loadingRunPreferences: false, discoveryProfiles: DISCOVERY_PROFILES, candidates: [], candidateTotal: 0, candidateOffset: 0, candidateHasMore: false, candidateVersion: null, candidateKnownVersion: null, candidateQueryKey: '', commonCandidateCache: {}, commonLoadingMore: false, candidateDomains: [], candidateDomainTotal: 0, candidateDomainStrategyTotal: 0, candidateDomainOffset: 0, candidateDomainHasMore: false, candidateDomainsLoaded: false, lastCandidateDomainTotal: 0, lastCandidateDomainStrategyTotal: 0, testedDomains: [], candidatesLoaded: false, candidateResultMode: 'balance', candidateResultRequested: false, domainStrategies: {}, finderRuns: [], finderRunTotal: 0, finderRunOffset: 0, finderRunHasMore: false, finderRunsLoaded: false, finderRunsLoading: false, finderLog: null, acknowledgedRun: null, startRequestInFlight: false, runGeneration: 0, domainSets: null, domainSources: null, v2flyPreview: null, v2flyCategories: null, v2flyCategorySource: '', v2flyCatalogUpdateLoading: false, backups: [], backupsLoaded: false, activeTab: 'finder', candidateView: 'domain', customPresets: loadCustomPresets(), customPresetMeta: { finder: {}, common: {} }, systemPresets: { finder: {}, common: {} }, systemPresetMeta: { finder: {}, common: {} }, presetManager: { scope: 'finder', name: '', query: '', domains: [], total: 0, hasMore: false, loading: false, loaded: false }, openCandidateDomains: {}, openCommonProtocols: {}, openRunDomains: {}, expandedStrategyLists: {}, strategyEditorScrolls: {}, domainsInitialized: false, domainsTouched: false, formMessage: 'Готово', formMessageTone: '' };
const jobNames = {
  'zapret-standard-discovery': 'Поиск стратегий',
  'zapret-multi-domain-discovery': 'Все домены на одной стратегии',
  'standard-discovery': 'Поиск стратегий',
  'multi-domain-discovery': 'Все домены на одной стратегии'
};
const statusTone = { success: 'good', failed: 'bad', error: 'bad', running: 'warn', queued: 'queue', stopping: 'warn', stopped: 'warn', timeout: 'warn' };
const AUTH_TOKEN_KEY = 'gp-control-plane-auth-token';
const BOOTSTRAP_TIMEOUT_MS = 15000;
const INITIAL_SYSTEM_STATUS_RETRY_DELAY_MS = 750;
const INITIAL_SYSTEM_STATUS_RETRY_LIMIT = 3;
let toastTimer = null;
let refreshInFlight = false;
let bootstrapState = 'idle';


let realtimeConnected = false;
let apiClient = null;
let sessionController = null;
let realtimeController = null;
let runState = null;
let logDirty = false;



state.candidateLoading = false;
state.candidateUpdatedAt = '';
state.backupsLoading = false;
state.backupsUpdatedAt = '';

const API_ENDPOINTS = Object.freeze({
  core: Object.freeze({
    status: '/api/core/status',
    startStrategyDiscoveryRun: '/api/core/strategy-discovery/start-run',
    stopCurrentStrategyDiscoveryRun: '/api/core/strategy-discovery/stop-current-run',
    backupsList: '/api/core/backups/list',
    backupsCreate: '/api/core/backups/create',
    backupsRestore: '/api/core/backups/restore',
    backupsDelete: '/api/core/backups/delete',
    backupsDownloadArchive: '/api/core/backups/download-archive',
    backupsUpload: '/api/core/backups/upload',
    runSettings: '/api/core/run-settings',
    saveRunSettings: '/api/core/run-settings/save',
    latestLog: '/api/core/runs/latest-log',
    v2flyCategories: '/api/core/presets/v2fly/categories',
    v2flyCategoryDomains: '/api/core/presets/v2fly/category-domains'
  }),
  service: Object.freeze({
    releasesAvailable: '/api/service/releases/available',
    v2flyLocalStorageStatus: '/api/service/v2fly/local-storage-status',
    v2flyUpdateLocalStorage: '/api/service/v2fly/update-local-storage'
  }),
  web: Object.freeze({
    status: '/api/web/status',
    runPreferences: '/api/web/run-preferences',
    runHistoryPage: '/api/web/runs/history-page',
    candidateDomainIndexPage: '/api/web/candidate-domain-index-page',
    strategyCandidatesPage: '/api/web/strategy-candidates-page',
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
function statusLabel(tone){
  return ({ good: 'Готово', queue: 'В очереди', pending: 'Проверяем', unknown: 'Нет статуса', warn: 'Внимание', bad: 'Ошибка' })[tone] || 'Статус';
}
function setStatusContent(node, text, tone){
  node.replaceChildren();
  const marker = document.createElement('span');
  marker.className = `status-marker ${tone || ''}`;
  marker.setAttribute('aria-hidden', 'true');
  const label = document.createElement('span');
  label.className = 'status-label';
  label.textContent = statusLabel(tone);
  const reason = document.createElement('span');
  reason.className = 'status-reason';
  reason.textContent = text || '';
  node.append(marker, label, reason);
}
function statusMarkup(text, tone){
  return `<span class="status-marker ${esc(tone || '')}" aria-hidden="true"></span><span class="status-label">${esc(statusLabel(tone))}</span><span class="status-reason">${esc(text)}</span>`;
}
function setBadge(node, text, tone){
  node.className = `badge status-badge ${tone || ''}`;
  setStatusContent(node, text, tone);
}
function setMessage(text, tone){
  const node = el('message');
  state.formMessage = text || '';
  state.formMessageTone = tone || '';
  node.className = 'message' + (tone ? ' ' + tone : '');
  setStatusContent(node, text, tone);
  renderMetrics();
}
function showToast(text, tone){
  const node = el('toast');
  if (toastTimer) clearTimeout(toastTimer);
  node.className = 'toast' + (tone ? ' ' + tone : '');
  setStatusContent(node, text, tone);
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
// A9 compatibility adapters: callers retain their established arguments and errors.
async function getJson(url, options){ return apiClient.getJson(url, options); }
async function postJson(url, payload, options){ return apiClient.postJson(url, payload, options); }
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
function currentSessionEpoch(){ return sessionController ? sessionController.epoch() : 0; }
function sessionIsCurrent(epoch){ return !sessionController || sessionController.isCurrent(epoch); }
function storeAuthToken(payload){
  const token = String((payload || {}).access_token || (payload || {}).token || '').trim();
  if (!token) throw new Error('The server did not return an authorization token');
  localStorage.setItem(AUTH_TOKEN_KEY, token);
  return token;
}
function showLogin(message){
  teardownViews();
  bootstrapState = 'idle';
  el('app-shell')?.remove();
  el('boot-screen').hidden = true;
  el('login-screen').hidden = false;
  setLoginError(message);
  shellViewLifetime.requestAnimationFrame(() => el('login-username')?.focus());
}
function setLoginError(message){
  const node = el('login-error');
  if (!message) {
    node.replaceChildren();
    return;
  }
  setStatusContent(node, message, 'bad');
}
function mountApplication(){
  if (el('app-shell')) return;
  const template = el('app-shell-template');
  document.body.append(template.content.cloneNode(true));
}
function showApplication(){
  mountApplication();
  el('login-screen').hidden = true;
  el('boot-screen').hidden = true;
  el('app-shell').hidden = false;
}
function showBoot(state){
  bootstrapState = state;
  el('login-screen').hidden = true;
  el('app-shell')?.remove();
  const screen = el('boot-screen');
  const message = el('boot-message');
  const retry = el('boot-retry');
  screen.hidden = false;
  retry.hidden = state !== 'failed';
  retry.disabled = state !== 'failed';
  message.textContent = state === 'failed'
    ? 'Не удалось загрузить интерфейс. Попробуйте ещё раз.'
    : 'Загрузка интерфейса…';
}
function stopRealtimeEvents(){
  realtimeController?.disposeStream();
}
function renewRealtimeEvents(){
  realtimeController?.renew();
}function stopRealtimeFallback(){ realtimeController?.dispose(); }
function handleUnauthorized(){
  sessionController?.unauthorized();
}
function logout(){
  sessionController?.logout();
}
async function authFetch(url, options){
  // Raw callers consume their own body, so keep the request lifetime alive until
  // they explicitly release it below.
  return apiClient.request(url, { ...(options || {}), keepSessionSignal: true });
}
async function startAuthenticatedUi(){
  return sessionController.bootstrap(loadBootstrapPayload, applyBootstrapPayload);
}
async function loadBootstrapPayload(signal){
  let timeoutId = null;
  try {
    const requests = Promise.all([
      getJson(apiEndpoint('web', 'status'), { signal }),
      getJson(apiUrl('web', 'runHistoryPage', runParams(0)), { signal }),
      getJson(apiEndpoint('core', 'latestLog'), { signal }),
      getJson(apiEndpoint('web', 'presets'), { signal }),
      fetchSettingsPayload({ signal })
    ]);
    const timeout = new Promise((_, reject) => {
      timeoutId = setTimeout(() => {
        const error = new Error('Bootstrap timed out');
        error.name = 'BootstrapTimeoutError';
        reject(error);
      }, BOOTSTRAP_TIMEOUT_MS);
    });
    return await Promise.race([requests, timeout]);
  } finally {
    if (timeoutId !== null) clearTimeout(timeoutId);
  }
}
function applyBootstrapPayload([status, finderRuns, finderLog, presets, settings]){
  state.statusLoading = false;
  clearInitialSystemStatusRetry();
  // Rendering waits for SessionController.ready(), after the shell is mounted.
  runState.mergeStatus(status);
  state.settings = (settings || {}).settings || (status || {}).settings || {};
  if (status && status.run_preferences) state.runPreferences = status.run_preferences;
  if (status && status.candidate_version) syncCandidateVersion(status.candidate_version);
  mergeRunPage(finderRuns, true);
  runState.acceptLog(finderLog, false, mergeLogPayload);
  mergePresetResponse(presets);
}
async function submitLogin(event){
  event.preventDefault();
  const errorNode = el('login-error');
  const form = el('login-form');
  const button = form.querySelector('button[type="submit"]');
  setLoginError('');
  button.disabled = true;
  sessionController.login({ username: el('login-username').value, password: el('login-password').value }, (error) => setLoginError(error.message || 'Unable to sign in')).finally(() => { button.disabled = false; });
}
async function changePassword(){
  const form = el('change-password-form');
  const submitButton = form.querySelector('[type="submit"]');
  const status = el('change-password-status');
  const currentPasswordInput = el('settings-current-password');
  const newPasswordInput = el('settings-new-password');
  const currentPassword = currentPasswordInput.value;
  const newPassword = newPasswordInput.value;
  form.setAttribute('aria-busy', 'true');
  submitButton.disabled = true;
  status.textContent = 'Пароль изменяется…';
  try {
    await sessionController.changePassword({
      current_password: currentPassword,
      new_password: newPassword
    }, () => { status.textContent = 'Не удалось изменить пароль. Проверьте текущий пароль и повторите попытку.'; });
  } finally {
    // Successful password change removes the shell. These captured nodes stay safe to clear.
    currentPasswordInput.value = '';
    newPasswordInput.value = '';
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
  return `<span class="badge">${esc(text)}</span>`;
}
function statusBadge(text, tone){
  return `<span class="badge status-badge ${esc(tone || '')}">${statusMarkup(text, tone)}</span>`;
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
  el(targetId).innerHTML = `<div class="table-wrap l-stack"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
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
  return `<div class="button-row list-load-more l-action-grid"><button class="secondary" data-action="${esc(action)}" type="button"${disabled}>${label}</button></div>`;
}
function runParams(...args){return uiControllers["history"].runParams(...args);}
function mergeRunPage(...args){return uiControllers["history"].mergeRunPage(...args);}
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
  }
}
function latestRun(){
  return state.finderRuns.length ? state.finderRuns[state.finderRuns.length - 1] : null;
}
function currentRun(){
  return runState.current();
}
function isBusy(){
  return runState.busy();
}
function isStartRequestInFlight(){
  return runState.startInFlight();
}
function runIdForRow(row){
  return String((row || {}).run_id || (row || {}).id || '');
}
function isTerminalRunStatus(status){
  return ['success', 'failed', 'error', 'stopped', 'timeout'].includes(String(status || '').toLowerCase());
}
function acknowledgeRun(runId){
  return runState.acknowledge(runId);
}
function acknowledgedRunIsCurrent(generation){
  return runState.isGenerationCurrent(generation);
}
function convergeAcknowledgedRunFromHistory(rows){
  return runState.convergeHistory(rows);
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
function defaultDomains(...args){ return uiControllers["finder"].defaultDomains(...args); }
function uniqueDomains(...args){ return uiControllers["finder"].uniqueDomains(...args); }
function uniqueDomainCount(...args){ return uiControllers["finder"].uniqueDomainCount(...args); }
function fillDomains(...args){ return uiControllers["finder"].fillDomains(...args); }
function finderDomains(...args){ return uiControllers["finder"].finderDomains(...args); }
function selectedFinderDomains(...args){ return uiControllers["finder"].selectedFinderDomains(...args); }
function timeoutSecondsOrNull(...args){ return uiControllers["finder"].timeoutSecondsOrNull(...args); }
function syncTimeLimitUi(...args){ return uiControllers["finder"].syncTimeLimitUi(...args); }
function curlParallelism(...args){ return uiControllers["finder"].curlParallelism(...args); }
function repeatsValue(...args){ return uiControllers["finder"].repeatsValue(...args); }
function minimumInputSeconds(...args){ return uiControllers["finder"].minimumInputSeconds(...args); }
function runTimeoutSettings(...args){ return uiControllers["finder"].runTimeoutSettings(...args); }
function discoveryOptions(...args){ return uiControllers["finder"].discoveryOptions(...args); }
function selectedFinderPresetSummary(...args){ return uiControllers["finder"].selectedFinderPresetSummary(...args); }
function selectedRunModeLabel(...args){ return uiControllers["finder"].selectedRunModeLabel(...args); }
function protocolSummary(...args){ return uiControllers["finder"].protocolSummary(...args); }
function runLaunchReadiness(...args){ return uiControllers["finder"].runLaunchReadiness(...args); }
function runLaunchSummaryItems(...args){ return uiControllers["finder"].runLaunchSummaryItems(...args); }
function renderRunLaunchSummary(...args){ return uiControllers["finder"].renderRunLaunchSummary(...args); }
function collectRunPreferences(...args){ return uiControllers["finder"].collectRunPreferences(...args); }
function useRunPreferencesOnce(...args){ return uiControllers["finder"].useRunPreferencesOnce(...args); }
function saveRunPreferencesNow(...args){ return uiControllers["finder"].saveRunPreferencesNow(...args); }
const DISCOVERY_PROFILE_CONTROL_IDS = new Set(['scan-level']);
const RUN_TIMEOUT_CONTROL_IDS = new Set(['run-curl-max-time', 'run-curl-max-time-quic', 'run-curl-max-time-doh']);
const RUN_LAUNCH_SUMMARY_CONTROL_IDS = new Set([
  'finder-domains',
  'finder-preset-select',
  'curl-parallelism',
  'enable-http',
  'enable-tls12',
  'enable-tls13',
  'include-quic',
  'enable-ipv6',
  'discovery-profile-select',
  'scan-level',
  'repeats',
  'repeat-parallel',
  'skip-dnscheck',
  'skip-ipblock',
  'limit-time-enabled',
  'finder-timeout-hours',
  'run-curl-max-time',
  'run-curl-max-time-quic',
  'run-curl-max-time-doh'
]);
const MUTATING_ACTIONS = new Set([
  'save-settings',
  'create-backup',
  'upload-backup',
  'preset-editor-save',
  'preset-editor-delete',
  'preset-new-save',
  'v2fly-load-categories',
  'v2fly-update-local-storage',
  'v2fly-preview',
  'v2fly-import'
]);
function isRunLaunchSummaryControl(...args){ return uiControllers["finder"].isRunLaunchSummaryControl(...args); }
function markDiscoveryProfileCustom(...args){ return uiControllers["finder"].markDiscoveryProfileCustom(...args); }
function useDiscoveryProfile(...args){ return uiControllers["finder"].useDiscoveryProfile(...args); }
function renderDiscoveryProfileNote(...args){ return uiControllers["finder"].renderDiscoveryProfileNote(...args); }
function selectedRunMode(...args){ return uiControllers["finder"].selectedRunMode(...args); }
function renderRunModeNote(...args){ return uiControllers["finder"].renderRunModeNote(...args); }
function profileTitle(...args){ return uiControllers["finder"].profileTitle(...args); }
function renderDiscoveryProfiles(...args){ return uiControllers["finder"].renderDiscoveryProfiles(...args); }
function hasEnabledProtocol(...args){ return uiControllers["finder"].hasEnabledProtocol(...args); }
function parseDomains(...args){ return uiControllers["finder"].parseDomains(...args); }
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
function persistCustomPresets(...args){ return uiControllers["presets"].persistCustomPresets(...args); }
function mergeCustomPresets(...args){ return uiControllers["presets"].mergeCustomPresets(...args); }
function mergeSystemPresets(...args){ return uiControllers["presets"].mergeSystemPresets(...args); }
function normalizeCustomPresetMeta(...args){ return uiControllers["presets"].normalizeCustomPresetMeta(...args); }
function normalizePresetMeta(...args){ return uiControllers["presets"].normalizePresetMeta(...args); }
function customPresetNames(...args){ return uiControllers["presets"].customPresetNames(...args); }
function presetScopesForTarget(...args){ return uiControllers["presets"].presetScopesForTarget(...args); }
function customPresetSourceScope(...args){ return uiControllers["presets"].customPresetSourceScope(...args); }
function customPresetCount(...args){ return uiControllers["presets"].customPresetCount(...args); }
function hasCustomPreset(...args){ return uiControllers["presets"].hasCustomPreset(...args); }
function systemPresetNames(...args){ return uiControllers["presets"].systemPresetNames(...args); }
function systemPresetMeta(...args){ return uiControllers["presets"].systemPresetMeta(...args); }
function systemPresetLabel(...args){ return uiControllers["presets"].systemPresetLabel(...args); }
function systemPresetCount(...args){ return uiControllers["presets"].systemPresetCount(...args); }
function hasSystemPreset(...args){ return uiControllers["presets"].hasSystemPreset(...args); }
function mergePresetResponse(...args){ return uiControllers["presets"].mergePresetResponse(...args); }
function builtInPresets(...args){ return uiControllers["presets"].builtInPresets(...args); }
function presetGroups(...args){ return uiControllers["presets"].presetGroups(...args); }
function presetDomains(...args){ return uiControllers["presets"].presetDomains(...args); }
function managerPresetEntries(...args){ return uiControllers["presets"].managerPresetEntries(...args); }
function managerPresetEntry(...args){ return uiControllers["presets"].managerPresetEntry(...args); }
function renderPresetSelect(...args){ return uiControllers["presets"].renderPresetSelect(...args); }
function renderPresetSelects(...args){ return uiControllers["presets"].renderPresetSelects(...args); }
function markDomainPresetCustom(...args){ return uiControllers["presets"].markDomainPresetCustom(...args); }
function fetchAllPresetDomains(...args){ return uiControllers["presets"].fetchAllPresetDomains(...args); }
function fetchStoredPresetDomains(...args){ return uiControllers["presets"].fetchStoredPresetDomains(...args); }
function usePreset(...args){ return uiControllers["presets"].usePreset(...args); }
function presetNameForSave(...args){ return uiControllers["presets"].presetNameForSave(...args); }
function savePreset(...args){ return uiControllers["presets"].savePreset(...args); }
function deletePreset(...args){ return uiControllers["presets"].deletePreset(...args); }
function statusCheck(...args){ return uiControllers["status-view"].statusCheck(...args); }
function zapretDiagnostics(...args){ return uiControllers["status-view"].zapretDiagnostics(...args); }
function zapretDiagnosticItems(...args){ return uiControllers["status-view"].zapretDiagnosticItems(...args); }
function zapretCompactStatus(...args){ return uiControllers["status-view"].zapretCompactStatus(...args); }
function hasCompleteSystemStatus(...args){ return uiControllers["status-view"].hasCompleteSystemStatus(...args); }
function clearInitialSystemStatusRetry(...args){ return uiControllers["status-view"].clearInitialSystemStatusRetry(...args); }
function scheduleInitialSystemStatusRetry(...args){ return uiControllers["status-view"].scheduleInitialSystemStatusRetry(...args); }
function initialSystemStatusState(...args){ return uiControllers["status-view"].initialSystemStatusState(...args); }
function testedDomainCount(...args){ return uiControllers["status-view"].testedDomainCount(...args); }
function nextActionStatus(...args){ return uiControllers["status-view"].nextActionStatus(...args); }
function metricJobNoteText(...args){ return uiControllers["status-view"].metricJobNoteText(...args); }
function jobStatusClass(...args){ return uiControllers["status-view"].jobStatusClass(...args); }
function renderMetrics(...args){ return uiControllers["status-view"].renderMetrics(...args); }
function renderCandidates(...args){ return uiControllers["candidates"].renderCandidates(...args); }
function renderDomainCandidates(...args){ return uiControllers["candidates"].renderDomainCandidates(...args); }
function renderCommonCandidates(...args){ return uiControllers["candidates"].renderCommonCandidates(...args); }
function candidateDomainPager(...args){ return uiControllers["candidates"].candidateDomainPager(...args); }
function candidatePager(...args){ return uiControllers["candidates"].candidatePager(...args); }
function domainStrategyContent(...args){ return uiControllers["candidates"].domainStrategyContent(...args); }
function filteredCandidates(...args){ return uiControllers["candidates"].filteredCandidates(...args); }
function candidateDomains(...args){ return uiControllers["candidates"].candidateDomains(...args); }
function commonSeen(...args){ return uiControllers["candidates"].commonSeen(...args); }
function commonDomains(...args){ return uiControllers["candidates"].commonDomains(...args); }
function candidateAllDomains(...args){ return uiControllers["candidates"].candidateAllDomains(...args); }
function testedDomains(...args){ return uiControllers["candidates"].testedDomains(...args); }
function updateTestedDomains(...args){ return uiControllers["candidates"].updateTestedDomains(...args); }
function candidateResultModeLabel(...args){ return uiControllers["candidates"].candidateResultModeLabel(...args); }
function candidateResultTargets(...args){ return uiControllers["candidates"].candidateResultTargets(...args); }
function commonCandidateResultRows(...args){ return uiControllers["candidates"].commonCandidateResultRows(...args); }
function rowTargetCoverage(...args){ return uiControllers["candidates"].rowTargetCoverage(...args); }
function resultPickScore(...args){ return uiControllers["candidates"].resultPickScore(...args); }
function buildCandidateResult(...args){ return uiControllers["candidates"].buildCandidateResult(...args); }
function candidateResultText(...args){ return uiControllers["candidates"].candidateResultText(...args); }
function resetCandidateResult(...args){ return uiControllers["candidates"].resetCandidateResult(...args); }
function buildCandidateResultNow(...args){ return uiControllers["candidates"].buildCandidateResultNow(...args); }
function renderCandidateResult(...args){ return uiControllers["candidates"].renderCandidateResult(...args); }
function copyCandidateResult(...args){ return uiControllers["candidates"].copyCandidateResult(...args); }
function exportCandidateResult(...args){ return uiControllers["candidates"].exportCandidateResult(...args); }
function useCandidateResultDomains(...args){ return uiControllers["candidates"].useCandidateResultDomains(...args); }
function openCandidateResultDetails(...args){ return uiControllers["candidates"].openCandidateResultDetails(...args); }
function filterTestedDomains(...args){ return uiControllers["candidates"].filterTestedDomains(...args); }
function selectedCommonDomains(...args){ return uiControllers["candidates"].selectedCommonDomains(...args); }
function commonDomainSuggestions(...args){ return uiControllers["candidates"].commonDomainSuggestions(...args); }
function renderCommonDomainSuggestions(...args){ return uiControllers["candidates"].renderCommonDomainSuggestions(...args); }
function hideCommonDomainSuggestions(...args){ return uiControllers["candidates"].hideCommonDomainSuggestions(...args); }
function chooseCommonDomainSuggestion(...args){ return uiControllers["candidates"].chooseCommonDomainSuggestion(...args); }
function commonCandidateKey(...args){ return uiControllers["candidates"].commonCandidateKey(...args); }
function currentCandidateQueryKey(...args){ return uiControllers["candidates"].currentCandidateQueryKey(...args); }
function candidateVersionKey(...args){ return uiControllers["candidates"].candidateVersionKey(...args); }
function sameCandidateVersion(...args){ return uiControllers["candidates"].sameCandidateVersion(...args); }
function candidateCacheValid(...args){ return uiControllers["candidates"].candidateCacheValid(...args); }
function rememberCandidateVersion(...args){ return uiControllers["candidates"].rememberCandidateVersion(...args); }
function invalidateCandidateCaches(...args){ return uiControllers["candidates"].invalidateCandidateCaches(...args); }
function syncCandidateVersion(...args){ return uiControllers["candidates"].syncCandidateVersion(...args); }
function loadCommonCandidateCache(...args){ return uiControllers["candidates"].loadCommonCandidateCache(...args); }
function storeCommonCandidateCache(...args){ return uiControllers["candidates"].storeCommonCandidateCache(...args); }
function prepareCommonCandidateState(...args){ return uiControllers["candidates"].prepareCommonCandidateState(...args); }
function dynamicCommonRows(...args){ return uiControllers["candidates"].dynamicCommonRows(...args); }
function renderCommonControls(...args){ return uiControllers["candidates"].renderCommonControls(...args); }
function addCommonDomain(...args){ return uiControllers["candidates"].addCommonDomain(...args); }
function candidateGroups(...args){ return uiControllers["candidates"].candidateGroups(...args); }
function protocolGroups(...args){ return uiControllers["candidates"].protocolGroups(...args); }
function normalizeStrategyArg(...args){ return uiControllers["candidates"].normalizeStrategyArg(...args); }
function uniqueStrategyRows(...args){ return uiControllers["candidates"].uniqueStrategyRows(...args); }
function uniqueStrategyArgs(...args){ return uiControllers["candidates"].uniqueStrategyArgs(...args); }
function strategyComplexity(...args){ return uiControllers["candidates"].strategyComplexity(...args); }
function strategyDomainCoverage(...args){ return uiControllers["candidates"].strategyDomainCoverage(...args); }
function strategyDisplayFamilyKey(...args){ return uiControllers["candidates"].strategyDisplayFamilyKey(...args); }
function bestFamilyRow(...args){ return uiControllers["candidates"].bestFamilyRow(...args); }
function strategyFamilyGroups(...args){ return uiControllers["candidates"].strategyFamilyGroups(...args); }
function strategyListState(...args){ return uiControllers["candidates"].strategyListState(...args); }
function lineNumbers(...args){ return uiControllers["candidates"].lineNumbers(...args); }
function updateEditorLineNumbers(...args){ return uiControllers["candidates"].updateEditorLineNumbers(...args); }
function updateAllEditorLineNumbers(...args){ return uiControllers["candidates"].updateAllEditorLineNumbers(...args); }
function strategyEditorScrollKey(...args){ return uiControllers["candidates"].strategyEditorScrollKey(...args); }
function rememberStrategyEditorScrolls(...args){ return uiControllers["candidates"].rememberStrategyEditorScrolls(...args); }
function restoreStrategyEditorScrolls(...args){ return uiControllers["candidates"].restoreStrategyEditorScrolls(...args); }
function strategyEditor(...args){ return uiControllers["candidates"].strategyEditor(...args); }
function strategyFamilyGroup(...args){ return uiControllers["candidates"].strategyFamilyGroup(...args); }
function strategyToggleLabel(...args){ return uiControllers["candidates"].strategyToggleLabel(...args); }
function domainFromStrategyListKey(...args){ return uiControllers["candidates"].domainFromStrategyListKey(...args); }
function isCommonStrategyListKey(...args){ return uiControllers["candidates"].isCommonStrategyListKey(...args); }
function renderRuns(...args){ return uiControllers["history"].renderRuns(...args); }
function runPager(...args){ return uiControllers["history"].runPager(...args); }
function renderRunCard(...args){ return uiControllers["history"].renderRunCard(...args); }
function runDomainKey(...args){ return uiControllers["history"].runDomainKey(...args); }
function runCardClass(...args){ return uiControllers["history"].runCardClass(...args); }
function runField(...args){ return uiControllers["history"].runField(...args); }
function runStatusLabel(...args){ return uiControllers["history"].runStatusLabel(...args); }
function runPhaseText(...args){ return uiControllers["history"].runPhaseText(...args); }
function phaseLabel(...args){ return uiControllers["history"].phaseLabel(...args); }
function runDomains(...args){ return uiControllers["history"].runDomains(...args); }
function runDomainChips(...args){ return uiControllers["history"].runDomainChips(...args); }
function diagnosticShortLabel(...args){ return uiControllers["history"].diagnosticShortLabel(...args); }
function diagnosticExplanation(...args){ return uiControllers["history"].diagnosticExplanation(...args); }
function curlCodeLabel(...args){ return uiControllers["history"].curlCodeLabel(...args); }
function curlCodeDetails(...args){ return uiControllers["history"].curlCodeDetails(...args); }
function runDiagnosticsSummary(...args){ return uiControllers["history"].runDiagnosticsSummary(...args); }
function runDiagnostics(...args){ return uiControllers["history"].runDiagnostics(...args); }
function diagnosticTableRow(...args){ return uiControllers["history"].diagnosticTableRow(...args); }
function isDiscoveryRun(...args){ return uiControllers["history"].isDiscoveryRun(...args); }
function runMode(...args){ return uiControllers["history"].runMode(...args); }
function runSummary(...args){ return uiControllers["history"].runSummary(...args); }
function runCandidateCount(...args){ return uiControllers["history"].runCandidateCount(...args); }
function runSettingsText(...args){ return uiControllers["history"].runSettingsText(...args); }
function truthyOption(...args){ return uiControllers["history"].truthyOption(...args); }
function runPayload(...args){ return uiControllers["history"].runPayload(...args); }
function fillRunFormFromPayload(...args){ return uiControllers["history"].fillRunFormFromPayload(...args); }
function repeatRun(...args){ return uiControllers["history"].repeatRun(...args); }
function runProgressText(...args){ return uiControllers["history"].runProgressText(...args); }
function renderLog(...args){ return uiControllers["terminal"].renderLog(...args); }
function renderStderrDiagnostics(...args){ return uiControllers["terminal"].renderStderrDiagnostics(...args); }
function renderBackups(...args){ return uiControllers["backups"].renderBackups(...args); }
function backupCard(...args){ return uiControllers["backups"].backupCard(...args); }
function normalizeBackupSnapshot(...args){ return uiControllers["backups"].normalizeBackupSnapshot(...args); }
function backupListFromPayload(...args){ return uiControllers["backups"].backupListFromPayload(...args); }
function backupDownloadUrl(...args){ return uiControllers["backups"].backupDownloadUrl(...args); }
function downloadBackup(...args){ return uiControllers["backups"].downloadBackup(...args); }function formatBytes(...args){ return uiControllers["backups"].formatBytes(...args); }
function renderProgress(...args){ return uiControllers["terminal"].renderProgress(...args); }
function progressAttemptText(...args){ return uiControllers["terminal"].progressAttemptText(...args); }
function progressStrategyText(...args){ return uiControllers["terminal"].progressStrategyText(...args); }
function interruptedRunWarning(...args){ return uiControllers["terminal"].interruptedRunWarning(...args); }
function liveRunStatusText(...args){ return uiControllers["terminal"].liveRunStatusText(...args); }
function liveRunCells(...args){ return uiControllers["terminal"].liveRunCells(...args); }
function latestImportantLogMessage(...args){ return uiControllers["terminal"].latestImportantLogMessage(...args); }
function renderLiveRun(...args){ return uiControllers["terminal"].renderLiveRun(...args); }
function eventRows(...args){ return uiControllers["status-view"].eventRows(...args); }
function diagnosticsText(...args){ return uiControllers["status-view"].diagnosticsText(...args); }
function copyDiagnostics(...args){ return uiControllers["status-view"].copyDiagnostics(...args); }
function renderEvents(...args){ return uiControllers["status-view"].renderEvents(...args); }
function progressLiveElapsedSeconds(...args){ return uiControllers["terminal"].progressLiveElapsedSeconds(...args); }
function progressLiveEtaSeconds(...args){ return uiControllers["terminal"].progressLiveEtaSeconds(...args); }
function etaModeLabel(...args){ return uiControllers["terminal"].etaModeLabel(...args); }
function etaStatusText(...args){ return uiControllers["terminal"].etaStatusText(...args); }
function renderRunSettingsSummary(...args){ return uiControllers["settings"].renderRunSettingsSummary(...args); }
function scanLevelLabel(...args){ return uiControllers["settings"].scanLevelLabel(...args); }
function renderSettings(...args){ return uiControllers["settings"].renderSettings(...args); }
function renderReleaseInfo(...args){ return uiControllers["releases"].renderReleaseInfo(...args); }
function releaseVersionLabel(...args){ return uiControllers["releases"].releaseVersionLabel(...args); }
function currentSettingsFromForm(...args){ return uiControllers["settings"].currentSettingsFromForm(...args); }
const RUN_SETTING_PAYLOAD_KEYS = Object.freeze([
  'curl_parallelism_default',
  'curl_parallelism_max',
  'curl_max_time',
  'curl_max_time_quic',
  'curl_max_time_doh',
  'enable_ipv6',
  'debug_stdout'
]);
function runSettingsPayloadFromSettings(...args){ return uiControllers["settings"].runSettingsPayloadFromSettings(...args); }
function fetchSettingsPayload(...args){ return uiControllers["settings"].fetchSettingsPayload(...args); }
function saveRunSettingsPayload(...args){ return uiControllers["settings"].saveRunSettingsPayload(...args); }
function saveSettingsPayload(...args){ return uiControllers["settings"].saveSettingsPayload(...args); }
function saveLaunchTimeoutDefaultsNow(...args){ return uiControllers["settings"].saveLaunchTimeoutDefaultsNow(...args); }
function saveSettings(...args){ return uiControllers["settings"].saveSettings(...args); }
function checkReleases(...args){ return uiControllers["releases"].checkReleases(...args); }
function releaseComparableVersion(...args){ return uiControllers["releases"].releaseComparableVersion(...args); }
function normalizeServiceRelease(...args){ return uiControllers["releases"].normalizeServiceRelease(...args); }
function rememberReleasePayload(...args){ return uiControllers["releases"].rememberReleasePayload(...args); }
function v2flyCategoryName(...args){ return uiControllers["presets"].v2flyCategoryName(...args); }
function v2flyAllCategories(...args){ return uiControllers["presets"].v2flyAllCategories(...args); }
function v2flyCategoryQuery(...args){ return uiControllers["presets"].v2flyCategoryQuery(...args); }
function v2flyExactCategory(...args){ return uiControllers["presets"].v2flyExactCategory(...args); }
function v2flyCategories(...args){ return uiControllers["presets"].v2flyCategories(...args); }
function clearV2flyDomains(...args){ return uiControllers["presets"].clearV2flyDomains(...args); }
function suggestV2flyPresetName(...args){ return uiControllers["presets"].suggestV2flyPresetName(...args); }
function v2flyPayload(...args){ return uiControllers["presets"].v2flyPayload(...args); }
function renderV2flyPreview(...args){ return uiControllers["presets"].renderV2flyPreview(...args); }
function setV2flyLocalError(...args){ return uiControllers["presets"].setV2flyLocalError(...args); }
function renderV2flyCategoryCatalog(...args){ return uiControllers["presets"].renderV2flyCategoryCatalog(...args); }
function presetManagerMeta(...args){ return uiControllers["presets"].presetManagerMeta(...args); }
function renderPresetManager(...args){ return uiControllers["presets"].renderPresetManager(...args); }
function renderPresetEditorPreview(...args){ return uiControllers["presets"].renderPresetEditorPreview(...args); }
function presetEditorDomains(...args){ return uiControllers["presets"].presetEditorDomains(...args); }
function presetEditorScope(...args){ return uiControllers["presets"].presetEditorScope(...args); }
function presetEditorName(...args){ return uiControllers["presets"].presetEditorName(...args); }
function presetEditorKind(...args){ return uiControllers["presets"].presetEditorKind(...args); }
function loadPresetEditorFromSelection(...args){ return uiControllers["presets"].loadPresetEditorFromSelection(...args); }
function buildPresetEditorPreview(...args){ return uiControllers["presets"].buildPresetEditorPreview(...args); }
function savePresetEditor(...args){ return uiControllers["presets"].savePresetEditor(...args); }
function deletePresetEditor(...args){ return uiControllers["presets"].deletePresetEditor(...args); }
function presetNewName(...args){ return uiControllers["presets"].presetNewName(...args); }
function presetNewDomains(...args){ return uiControllers["presets"].presetNewDomains(...args); }
function renderPresetNewPreview(...args){ return uiControllers["presets"].renderPresetNewPreview(...args); }
function savePresetNew(...args){ return uiControllers["presets"].savePresetNew(...args); }
function exportPresetEditor(...args){ return uiControllers["presets"].exportPresetEditor(...args); }
function loadV2flyCategories(...args){ return uiControllers["presets"].loadV2flyCategories(...args); }
function updateV2flyLocalStorage(...args){ return uiControllers["presets"].updateV2flyLocalStorage(...args); }
function fetchV2flyCategoryDomains(...args){ return uiControllers["presets"].fetchV2flyCategoryDomains(...args); }
function buildV2flyClientPreview(...args){ return uiControllers["presets"].buildV2flyClientPreview(...args); }
function previewV2flyPreset(...args){ return uiControllers["presets"].previewV2flyPreset(...args); }
function importV2flyPreset(...args){ return uiControllers["presets"].importV2flyPreset(...args); }
function formatDuration(seconds){
  if (!Number.isFinite(seconds)) return '-';
  if (seconds <= 0) return '0 мин';
  const minutes = Math.ceil(seconds / 60);
  if (minutes < 60) return `${minutes} мин`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} ч ${rest} мин` : `${hours} ч`;
}
function scrollLogToBottom(...args){ return uiControllers["terminal"].scrollLogToBottom(...args); }
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
function renderCandidatesOnly(...args){ return uiControllers["candidates"].renderCandidatesOnly(...args); }
function ensureCandidateViewLoaded(...args){ return uiControllers["candidates"].ensureCandidateViewLoaded(...args); }
function setCandidateView(...args){ return uiControllers["candidates"].setCandidateView(...args); }
function candidateParams(...args){ return uiControllers["candidates"].candidateParams(...args); }
function refreshDomainIndex(...args){ return uiControllers["candidates"].refreshDomainIndex(...args); }
function refreshDomainStrategies(...args){ return uiControllers["candidates"].refreshDomainStrategies(...args); }
function loadMoreDomainStrategies(...args){ return uiControllers["candidates"].loadMoreDomainStrategies(...args); }
function loadMoreCommonStrategies(...args){ return uiControllers["candidates"].loadMoreCommonStrategies(...args); }
function refreshCandidates(...args){ return uiControllers["candidates"].refreshCandidates(...args); }
function scheduleCandidateRefresh(...args){ return uiControllers["candidates"].scheduleCandidateRefresh(...args); }
function trimTextLines(...args){ return uiControllers["terminal"].trimTextLines(...args); }
function appendLogText(...args){ return uiControllers["terminal"].appendLogText(...args); }
function latestLogUrl(...args){ return uiControllers["terminal"].latestLogUrl(...args); }
function mergeLogPayload(...args){ return uiControllers["terminal"].mergeLogPayload(...args); }
function mergeStatusPayload(...args){return uiControllers["status-view"].mergeStatusPayload(...args);}
function refreshRuns(...args){ return uiControllers["history"].refreshRuns(...args); }
function refreshLog(...args){ return uiControllers["terminal"].refreshLog(...args); }
function refreshPresets(...args){ return uiControllers["presets"].refreshPresets(...args); }
function handleCandidateEvent(payload){
  const version = payload && payload.version ? payload.version : null;
  if (version) syncCandidateVersion(version);
  renderMetrics();
  if (state.activeTab === 'candidates') ensureCandidateViewLoaded();
}
function handleLogEvent(){
  logDirty = true;
  if (state.activeTab === 'terminal' || isBusy()) refreshLog(true);
}
function handleStatusEvent(payload){
  mergeStatusPayload(payload);
}
function sseJson(data){
  try { return JSON.parse(data || '{}'); }
  catch (_error) { return {}; }
}
function handleRealtimeEvent(event, data){
  if (event === 'status') handleStatusEvent(sseJson(data));
  if (event === 'runs') refreshRuns();
  if (event === 'log') handleLogEvent();
  if (event === 'candidates') handleCandidateEvent(sseJson(data));
  if (event === 'settings' && state.status) renderSettings();
  if (event === 'presets') refreshPresets();
}
function startRealtimeEvents(){ realtimeController?.start(); }
function startRealtimeFallback(){ realtimeController?._startFallback(); }
function refreshRequestMap(light){
  const bootstrap = !light || !hasCompleteSystemStatus();
  const requests = {
    status: getJson(apiEndpoint('web', 'status')),
    finderRuns: getJson(apiUrl('web', 'runHistoryPage', runParams(0))),
    finderLog: getJson(latestLogUrl(false))
  };
  if (bootstrap) {
    requests.presets = getJson(apiEndpoint('web', 'presets'));
    requests.settings = fetchSettingsPayload();
  }
  return { bootstrap, requests };
}
function settledValue(results, key){
  const result = results[key];
  return result && result.status === 'fulfilled' ? result.value : null;
}
function refreshFailureMessages(results){
  return Object.entries(results)
    .filter(([, result]) => result.status === 'rejected')
    .map(([key, result]) => `${key}: ${result.reason && result.reason.message ? result.reason.message : String(result.reason || 'unknown')}`);
}
async function refresh(options = {}){
  const epoch = currentSessionEpoch();
  if (refreshInFlight) return;
  refreshInFlight = true;
  if (!hasCompleteSystemStatus()) state.statusLoading = true;
  const light = Boolean(options.light);
  const logRequest = runState.captureLogRequest();
  const { bootstrap, requests } = refreshRequestMap(light);
  const keys = Object.keys(requests);
  try {
    const settled = await Promise.allSettled(keys.map((key) => requests[key]));
    if (!sessionIsCurrent(epoch)) return;
    const results = Object.fromEntries(keys.map((key, index) => [key, settled[index]]));
    const finderRuns = settledValue(results, 'finderRuns');
    // History is identity evidence for a distinct server-observed run. Apply it
    // before status so post-terminal stale data remains rejected while a fresh
    // external run is allowed through the same refresh.
    if (finderRuns) mergeRunPage(finderRuns, true);
    const status = settledValue(results, 'status');
    if (hasCompleteSystemStatus(status)) {
      state.statusLoading = false;
      clearInitialSystemStatusRetry();
      mergeStatusPayload(status);
    } else if (!hasCompleteSystemStatus()) {
      state.statusLoading = false;
      if (status) mergeStatusPayload(status);
      scheduleInitialSystemStatusRetry();
    }
    const settings = settledValue(results, 'settings');
    if (settings) state.settings = (settings || {}).settings || (status || {}).settings || state.settings || {};
    const finderLog = settledValue(results, 'finderLog');
    if (finderLog) runState.acceptLog(finderLog, false, mergeLogPayload, logRequest);
    const presets = settledValue(results, 'presets');
    if (presets) mergePresetResponse(presets);
    if (bootstrap) renderAll({ skipCandidates: true });
    else {
      renderRuns();
      renderLog();
      renderMetrics();
      renderEvents();
    }
    if (state.activeTab === 'candidates') ensureCandidateViewLoaded();
    const failures = refreshFailureMessages(results);
    if (failures.length && !options.silent) {
      const prefix = failures.length === keys.length ? 'Ошибка обновления' : 'Частичная ошибка обновления';
      setMessage(`${prefix}: ${failures.slice(0, 3).join('; ')}`, failures.length === keys.length ? 'bad' : 'warn');
    }
  } catch (error) {
    if (!sessionIsCurrent(epoch)) return;
    if (!options.silent) setMessage(`Ошибка обновления: ${error.message}`, 'bad');
  } finally {
    refreshInFlight = false;
  }
}
async function refreshAcknowledgedRun(generation){
  const epoch = currentSessionEpoch();
  const logRequest = runState.captureLogRequest();
  const requests = {
    status: getJson(apiEndpoint('web', 'status')),
    finderRuns: getJson(apiUrl('web', 'runHistoryPage', runParams(0))),
    finderLog: getJson(latestLogUrl(false))
  };
  const keys = Object.keys(requests);
  const settled = await Promise.allSettled(keys.map((key) => requests[key]));
  if (!sessionIsCurrent(epoch)) return;
  // Establish whether this response batch was for the accepted generation
  // before its matching history is allowed to converge it to terminal.  That
  // convergence must not discard the final log returned by the same batch.
  const generationWasCurrent = acknowledgedRunIsCurrent(generation);
  if (!generationWasCurrent) return;
  const results = Object.fromEntries(keys.map((key, index) => [key, settled[index]]));
  const finderRuns = settledValue(results, 'finderRuns');
  if (finderRuns) mergeRunPage(finderRuns, true);
  const status = settledValue(results, 'status');
  if (status) mergeStatusPayload(status);
  const finderLog = settledValue(results, 'finderLog');
  if (finderLog && runState.acceptLog(finderLog, true, mergeLogPayload, logRequest)) {
    logDirty = false;
  }
  renderRuns();
  renderLog();
  renderMetrics();
  renderEvents();
}
function refreshBackups(...args){ return uiControllers["backups"].refreshBackups(...args); }
function createBackup(...args){ return uiControllers["backups"].createBackup(...args); }
function restoreBackup(...args){ return uiControllers["backups"].restoreBackup(...args); }
function deleteBackup(...args){ return uiControllers["backups"].deleteBackup(...args); }
function isRuntimeBusyError(...args){ return uiControllers["backups"].isRuntimeBusyError(...args); }
function backupBusyMessage(...args){ return uiControllers["backups"].backupBusyMessage(...args); }
function uploadBackup(...args){ return uiControllers["backups"].uploadBackup(...args); }
function startJob(...args){ return uiControllers["finder"].startJob(...args); }
function selectedCoreProtocols(...args){ return uiControllers["finder"].selectedCoreProtocols(...args); }
function coreStrategyDiscoveryPayload(...args){ return uiControllers["finder"].coreStrategyDiscoveryPayload(...args); }
function startSelectedDiscovery(...args){ return uiControllers["finder"].startSelectedDiscovery(...args); }
function stopCurrentJob(...args){ return uiControllers["finder"].stopCurrentJob(...args); }
listen(document,'submit', (event) => {
  if (event.target && event.target.id === 'login-form') {
    submitLogin(event);
    return;
  }
  if (event.target && event.target.id === 'change-password-form') {
    event.preventDefault();
    changePassword();
  }
});listen(document,'click', (event) => {
  const domainSummary = event.target.closest('details.domain-group[data-domain] > summary');
  if(uiControllers["candidates"].handleClickPart0(domainSummary,event)) return;
const button = event.target.closest('button');
  if (!button) return;
  const action = button.dataset.action || '';
  if(uiControllers["backups"].handleClickPart0(button)) return;
  if (action === 'logout') {
    logout();
    return;
  }
  const protectedMutation = MUTATING_ACTIONS.has(action) || Boolean(button.dataset.backupRestore) || Boolean(button.dataset.backupDelete);
  if (protectedMutation && !requireNoActiveRun()) return;
  if(uiControllers["candidates"].handleClickPart1(button)) return;
  if(uiControllers["history"].handleClickPart0(button)) return;
  if (button.dataset.tab) setActiveTab(button.dataset.tab);
  if(uiControllers["candidates"].handleClickPart2(button)) return;
  if(uiControllers["candidates"].handleClickPart3(button)) return;
  if (button.dataset.action === 'open-log') {
    setActiveTab('terminal');
    const raw = document.querySelector('.raw-log-panel');
    if (raw) raw.open = true;
    return;
  }
  if (button.dataset.action === 'open-candidates') {
    setActiveTab('candidates');
    return;
  }
  if(uiControllers["history"].handleClickPart1(button)) return;
  if(uiControllers["status-view"].handleClickPart0(button)) return;
  if(uiControllers["candidates"].handleClickPart4(button)) return;
  if(uiControllers["candidates"].handleClickPart5(button)) return;
  if(uiControllers["candidates"].handleClickPart6(button)) return;
  if(uiControllers["candidates"].handleClickPart7(button)) return;
  if(uiControllers["candidates"].handleClickPart8(button)) return;
  if(uiControllers["candidates"].handleClickPart9(button)) return;
  if(uiControllers["backups"].handleClickPart1(button)) return;
  if(uiControllers["backups"].handleClickPart2(button)) return;
  if(uiControllers["settings"].handleClickPart0(button)) return;
  if(uiControllers["releases"].handleClickPart0(button)) return;
  if(uiControllers["presets"].handleClickPart0(button)) return;
  if(uiControllers["presets"].handleClickPart1(button)) return;
  if(uiControllers["presets"].handleClickPart2(button)) return;
  if(uiControllers["presets"].handleClickPart3(button)) return;
  if(uiControllers["presets"].handleClickPart4(button)) return;
  if(uiControllers["presets"].handleClickPart5(button)) return;
  if(uiControllers["presets"].handleClickPart6(button)) return;
  if(uiControllers["presets"].handleClickPart7(button)) return;
  if(uiControllers["presets"].handleClickPart8(button)) return;
  if(uiControllers["backups"].handleClickPart3(button)) return;
  if(uiControllers["backups"].handleClickPart4(button)) return;
  if(uiControllers["backups"].handleClickPart5(button)) return;
  if(uiControllers["candidates"].handleClickPart10(button)) return;
  if(uiControllers["candidates"].handleClickPart11(button)) return;
  if(uiControllers["history"].handleClickPart2(button)) return;
  if(uiControllers["finder"].handleClickPart0(button)) return;
  if(uiControllers["presets"].handleClickPart9(button)) return;
  if(uiControllers["presets"].handleClickPart10(button)) return;
  if(uiControllers["candidates"].handleClickPart12(button)) return;
  if(uiControllers["candidates"].handleClickPart13(button)) return;
  if(uiControllers["finder"].handleClickPart1(button)) return;
  if(uiControllers["finder"].handleClickPart2(button)) return;
});
listen(document,'input', (event) => {
  uiControllers.settings.handleDraftInput(event);
  uiControllers.presets.handleDraftInput(event);
  if(uiControllers["settings"].handleInputPart1(event)) return;
  if(uiControllers["settings"].handleInputPart2(event)) return;
  if(uiControllers["settings"].handleInputPart3(event)) return;
  if(uiControllers["presets"].handleInputPart11(event)) return;
  if(uiControllers["candidates"].handleInputPart14(event)) return;
  if(uiControllers["candidates"].handleInputPart15(event)) return;
  if(uiControllers["candidates"].handleInputPart16(event)) return;
  if(uiControllers["candidates"].handleInputPart17(event)) return;
  if(uiControllers["finder"].handleInputPart3(event)) return;
  if(uiControllers["candidates"].handleInputPart18(event)) return;
  if(uiControllers["finder"].handleInputPart4(event)) return;
});
listen(document,'scroll', (event) => {
  if(uiControllers["candidates"].handleScrollPart19(event)) return;
}, true);
listen(document,'change', (event) => {
  uiControllers.settings.handleDraftInput(event);
  uiControllers.presets.handleDraftInput(event);
  if(uiControllers["settings"].handleChangePart4(event)) return;
  if(uiControllers["settings"].handleChangePart5(event)) return;
  if(uiControllers["settings"].handleChangePart6(event)) return;
  if(uiControllers["presets"].handleChangePart12(event)) return;
  if(uiControllers["finder"].handleChangePart5(event)) return;
  if(uiControllers["presets"].handleChangePart13(event)) return;
  if(uiControllers["presets"].handleChangePart14(event)) return;
  if(uiControllers["finder"].handleChangePart6(event)) return;
  if(uiControllers["finder"].handleChangePart7(event)) return;
  if(uiControllers["finder"].handleChangePart8(event)) return;
  if(uiControllers["finder"].handleChangePart9(event)) return;
});
listen(document,'keydown', (event) => {
  if (handleTabControlKeydown(event)) return;
  if(uiControllers["candidates"].handleKeydownPart20(event)) return;
  if(uiControllers["candidates"].handleKeydownPart21(event)) return;
});
listen(document,'focusin', (event) => {
  if(uiControllers["candidates"].handleFocusinPart22(event)) return;
});
listen(document,'focusout', (event) => {
  if(uiControllers["candidates"].handleFocusoutPart23(event)) return;
});
listen(document,'toggle', (event) => {
  const details = event.target;
  if (!details || !details.matches) return;
  if(uiControllers["candidates"].handleTogglePart24(details)) return;
  if(uiControllers["history"].handleTogglePart3(details)) return;
}, true);
function initializeUiControllers(){
  runState = new RunState(state, { runId: runIdForRow, terminal: isTerminalRunStatus });
  apiClient = new ApiClient({
    getToken: authToken,
    getEpoch: currentSessionEpoch,
    isEpochCurrent: sessionIsCurrent,
    getSignal: () => sessionController ? sessionController.signal() : null,
    onUnauthorized: handleUnauthorized
  });
  realtimeController = new RealtimeController({
    api: apiClient,
    url: () => apiEndpoint('web', 'eventsStream'),
    onEvent: handleRealtimeEvent,
    onConnection: (connected) => { realtimeConnected = connected; },
    fallback: () => refresh({ light: true, silent: true }),
    isActive: () => Boolean(authToken())
  });
  sessionController = new SessionController({
    api: apiClient,
    token: { get: authToken, store: storeAuthToken, clear: () => localStorage.removeItem(AUTH_TOKEN_KEY) },
    realtime: realtimeController,
    ui: {
      begin: () => { teardownViews(); runState.resetForSession(); },
      boot: showBoot,
      login: showLogin,
      load: loadBootstrapPayload,
      apply: applyBootstrapPayload,
      ready: () => {
        showApplication();
        renderAll({ skipCandidates: true });
        bootstrapState = 'ready';
      }
    }
  });
  uiControllers["finder"] = createFinderController({view: scopedView(state, ["discoveryProfiles","domainSets","domainsInitialized","domainsTouched","loadingDiscoveryProfile","loadingRunPreferences","runPreferences","runPreferencesApplied","savingRunPreferences","settings","status"]), CUSTOM_SELECT_VALUE, RUN_LAUNCH_SUMMARY_CONTROL_IDS, acknowledgeRun, acknowledgedRunIsCurrent, apiEndpoint, builtInPresets, currentSessionEpoch, customPresetCount, el, esc, formatDuration, hasCompleteSystemStatus, isBusy, isStartRequestInFlight, postJson, presetDomains, refresh, refreshAcknowledgedRun, renderAll, renderMetrics, runState, saveLaunchTimeoutDefaultsNow, scanLevelLabel, sessionIsCurrent, setBadge, setMessage, systemPresetCount, systemPresetLabel, testedDomains, updateEditorLineNumbers, zapretCompactStatus, DISCOVERY_PROFILE_CONTROL_IDS });
  uiControllers["presets"] = createPresetsController({view: scopedView(state, ["candidateResultRequested","customPresetMeta","customPresets","domainSets","domainSources","domainsTouched","loadingDomainPreset","presetManager","systemPresetMeta","systemPresets","v2flyCatalogUpdateLoading","v2flyCategories","v2flyCategorySource","v2flyPreview"]), CUSTOM_PRESETS_KEY, CUSTOM_SELECT_VALUE, apiEndpoint, apiUrl, currentSessionEpoch, defaultDomains, el, esc, filterTestedDomains, friendlyDate, getJson, hasCompleteSystemStatus, isBusy, parseDomains, postJson, prepareCommonCandidateState, refreshCandidates, renderCandidates, renderCandidatesOnly, renderRunLaunchSummary, resetCandidateResult, selectedCommonDomains, sessionIsCurrent, setMessage, setText, showToast, statusMarkup, testedDomains, uniqueDomainCount, uniqueDomains, updateEditorLineNumbers });
  uiControllers["status-view"] = createStatusViewController({view: scopedView(state, ["candidateDomainTotal","candidateDomains","candidateDomainsLoaded","candidateLoading","finderLog","lastCandidateDomainTotal","runPreferences","settings","status","statusLoading","testedDomains","v2flyCatalogUpdateLoading"]), INITIAL_SYSTEM_STATUS_RETRY_DELAY_MS, INITIAL_SYSTEM_STATUS_RETRY_LIMIT, currentRun, el, esc, friendlyTime, interruptedRunWarning, isBusy, isStartRequestInFlight, mutatingBlockedMessage, refresh, runStatusLabel, setBadge, setMessage, setText, statusBadge, runState, syncCandidateVersion, renderLiveRun, renderSettings });
  uiControllers["candidates"] = createCandidatesController({view: scopedView(state, ["activeTab","candidateDomainHasMore","candidateDomainOffset","candidateDomainStrategyTotal","candidateDomainTotal","candidateDomains","candidateDomainsLoaded","candidateHasMore","candidateKnownVersion","candidateLoading","candidateOffset","candidateQueryKey","candidateResultMode","candidateResultRequested","candidateTotal","candidateUpdatedAt","candidateVersion","candidateView","candidates","candidatesLoaded","commonCandidateCache","commonLoadingMore","domainStrategies","domainsTouched","expandedStrategyLists","lastCandidateDomainStrategyTotal","lastCandidateDomainTotal","openCandidateDomains","openCommonProtocols","strategyEditorScrolls","testedDomains"]), CANDIDATE_PAGE_LIMIT, DOMAIN_PAGE_LIMIT, STRATEGY_LIST_LIMIT, apiUrl, badge, el, esc, friendlyTime, getJson, listLoadMore, markDomainPresetCustom, parseDomains, presetDomains, renderMetrics, renderPresetSelect, renderRunLaunchSummary, setActiveTab, setMessage, setText, showToast, uniqueDomains, refresh, renderPresetEditorPreview, renderPresetNewPreview });
  uiControllers["history"] = createHistoryController({view: scopedView(state, ["domainsTouched","finderRunHasMore","finderRunOffset","finderRunTotal","finderRuns","finderRunsLoading","openRunDomains","settings"]), apiUrl, badge, curlParallelism, currentSessionEpoch, el, esc, formatDuration, friendlyDate, getJson, listLoadMore, markDomainPresetCustom, renderDiscoveryProfileNote, renderMetrics, renderRunLaunchSummary, renderRunModeNote, sessionIsCurrent, setActiveTab, setMessage, setText, statusBadge, statusTone, syncTimeLimitUi, uniqueDomains, updateEditorLineNumbers, latestRun, RUN_PAGE_LIMIT, runState, latestById, renderLiveRun, renderEvents });
  uiControllers["terminal"] = createTerminalController({logDirty:{get value(){return logDirty;},set value(v){logDirty=v;}},view: scopedView(state, ["acknowledgedRun","activeTab","finderLog"]), apiEndpoint, apiUrl, currentRun, currentSessionEpoch, el, esc, formatDuration, getJson, isBusy, latestRun, phaseLabel, renderEvents, renderMetrics, renderRunSettingsSummary, runIdForRow, runState, runStatusLabel, sessionIsCurrent, setBadge, setMessage, setText, statusBadge, statusTone });
  uiControllers["backups"] = createBackupsController({view: scopedView(state, ["activeTab","backups","backupsLoaded","backupsLoading","backupsUpdatedAt"]), apiClient, apiEndpoint, apiUrl, authFetch, currentSessionEpoch, el, ensureCandidateViewLoaded, esc, friendlyTime, getJson, invalidateCandidateCaches, postJson, refresh, renderMetrics, requestHeaders, requestUrl, sessionIsCurrent, setMessage, statusBadge });
  uiControllers["settings"] = createSettingsController({view: scopedView(state, ["runPreferencesApplied","settings","settingsTouched"]), DISCOVERY_PROFILES, RUN_SETTING_PAYLOAD_KEYS, apiEndpoint, currentSessionEpoch, el, formatDuration, getJson, postJson, renderDiscoveryProfiles, renderPresetManager, renderReleaseInfo, renderRunLaunchSummary, renderRunModeNote, renderV2flyCategoryCatalog, renderV2flyPreview, runTimeoutSettings, sessionIsCurrent, setMessage, RUN_TIMEOUT_CONTROL_IDS });
  uiControllers["releases"] = createReleasesController({view: scopedView(state, ["releaseChecked","releaseChecking","releaseInfo","releasePrerelease","releaseStable","status"]), apiEndpoint, el, friendlyDate, getJson, setMessage });
}
initializeUiControllers();
el('boot-retry').addEventListener('click', () => {
  if (bootstrapState === 'failed') startAuthenticatedUi();
});
if (authToken()) startAuthenticatedUi();
else showLogin();

function disposeApplication(){ realtimeController?.dispose();teardownViews();applicationListeners.splice(0).forEach(remove=>remove()); }
listen(window,"pagehide",disposeApplication);
