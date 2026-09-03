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
