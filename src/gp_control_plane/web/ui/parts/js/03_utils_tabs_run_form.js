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
  const value = Number(node?.value || fallback || 2);
  if (!Number.isFinite(value)) return Math.max(1, Math.round(Number(fallback || 2)));
  return Math.max(1, Math.round(value));
}
function runTimeoutSettings(){
  const settings = state.settings || {};
  return {
    curl_max_time: minimumInputSeconds('run-curl-max-time', settings.curl_max_time || 2),
    curl_max_time_quic: minimumInputSeconds('run-curl-max-time-quic', settings.curl_max_time_quic || 2),
    curl_max_time_doh: minimumInputSeconds('run-curl-max-time-doh', settings.curl_max_time_doh || 2)
  };
}
function selectedDiscoveryEngine(){
  const finder = el('finder-discovery-engine');
  if (finder && finder.value) return finder.value;
  const settings = el('settings-discovery-engine');
  if (settings && settings.value) return settings.value;
  return String((state.settings || {}).discovery_engine || 'blockcheck2');
}
const BS_ONLY_HIDDEN_IDS = ['enable-http', 'include-quic', 'enable-ipv6', 'run-curl-max-time-quic', 'run-curl-max-time-doh'];
