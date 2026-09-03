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
function hideFieldRow(id, hidden){
  const node = el(id);
  if (!node) return;
  const row = node.closest('.checkbox-row') || node.closest('.field') || node.parentElement;
  if (row) row.hidden = hidden;
}
function engineIsBlockchecks(){
  return selectedDiscoveryEngine() === 'blockchecks';
}
function discoveryEngineReady(status){
  const zapret = (status || {}).zapret2 || {};
  if (engineIsBlockchecks()){
    const pre = state.bsPreflight;
    if (pre && typeof pre.ready === 'boolean') return pre.ready;
    return zapret.ready !== undefined ? Boolean(zapret.ready) : Boolean(zapret.nfqws2_found);
  }
  return zapretCompactStatus(zapret).ready;
}
async function refreshBsPreflight(){
  if (!engineIsBlockchecks()) {
    state.bsPreflight = null;
    return;
  }
  try {
    const data = await getJson(apiEndpoint('core', 'preflight'));
    state.bsPreflight = (data && data.ready !== undefined) ? data : null;
  } catch (err) {
    state.bsPreflight = null;
  }
  renderMetrics();
  renderRunLaunchSummary();
}
function syncEngineUi(){
  const active = engineIsBlockchecks();
  const bsOptions = el('bs-options');
  if (bsOptions) bsOptions.hidden = !active;
  BS_ONLY_HIDDEN_IDS.forEach((id) => hideFieldRow(id, active));
  document.querySelectorAll('[data-action="export-nfconf"]').forEach((button) => {
    button.disabled = !active;
    button.title = active ? '' : 'Экспорт nfqws2 (bc-nfconf) доступен при движке blockcheckS';
  });
  if (active && !state.bsPreflight) refreshBsPreflight();
}
function discoveryOptions(){
  const timeouts = runTimeoutSettings();
  const engine = selectedDiscoveryEngine();
  return {
    enable_http: el('enable-http').checked,
    enable_tls12: el('enable-tls12').checked,
    enable_tls13: el('enable-tls13').checked,
    include_quic: el('include-quic').checked,
    enable_ipv6: el('enable-ipv6').checked,
    scan_level: el('scan-level').value || 'standard',
    repeats: repeatsValue(),
    repeat_parallel: el('repeat-parallel').checked,
    skip_dnscheck: el('skip-dnscheck').checked,
    skip_ipblock: el('skip-ipblock').checked,
    discovery_engine: engine,
    ...timeouts,
    ...(engine === 'blockchecks' ? {
      strategy_preset: (el('bs-strategy-preset') || {}).value || '',
      repeats_mode: (el('bs-repeats-mode') || {}).value || 'fast',
      bs_adaptive: (el('bs-adaptive') || { checked: true }).checked !== false,
      bs_pair_mode: (el('bs-run-mode') || { value: 'tcp' }).value === 'pair'
    } : {})
  };
}
function selectedFinderPresetSummary(){
  const value = el('finder-preset-select')?.value || CUSTOM_SELECT_VALUE;
  if (value === CUSTOM_SELECT_VALUE) return 'ручной список';
  const [kind, name] = String(value || '').split(':');
  if (kind === 'system') {
    return `${systemPresetLabel('finder', name)} (${systemPresetCount('finder', name)})`;
  }
  if (kind === 'custom') {
    return `Пользовательский: ${name} (${customPresetCount('finder', name)})`;
  }
  if (kind === 'builtin') {
    const preset = builtInPresets('finder').find((item) => item.key === name);
    return preset ? `${preset.label} (${uniqueDomainCount(preset.domains)})` : name || '-';
  }
  return value || '-';
}
function selectedRunModeLabel(){
  return selectedRunMode() === 'multi' ? 'Все домены на одной стратегии' : 'Домены по очереди';
}
function protocolSummary(options){
  const protocols = [];
  if (options.enable_http) protocols.push('HTTP');
  if (options.enable_tls12) protocols.push('TLS 1.2');
  if (options.enable_tls13) protocols.push('TLS 1.3');
  if (options.include_quic) protocols.push('QUIC');
  return protocols.join(' + ') || 'не выбран';
}
function runLaunchReadiness(domains, options){
  const status = state.status || {};
  const ready = discoveryEngineReady(status);
  if (isBusy()) return { text: 'Идет подбор', tone: 'warn' };
  if (!ready) return { text: 'Требуется настройка', tone: 'warn' };
  if (!domains.length) return { text: 'Нужны домены', tone: 'warn' };
  if (!hasEnabledProtocol(options)) return { text: 'Нужен протокол', tone: 'warn' };
  return { text: 'Готово к старту', tone: 'good' };
}
function runLaunchSummaryItems(){
  const domains = finderDomains();
  const options = discoveryOptions();
  const settings = state.settings || {};
  const mode = selectedRunMode();
  const limit = timeoutSecondsOrNull();
  const isBs = engineIsBlockchecks();
  const checks = [
    options.skip_dnscheck ? 'DNS: пропуск' : 'DNS: проверять',
    options.skip_ipblock ? 'IP/port: пропуск' : 'IP/port: проверять'
  ].join(', ');
  const repeats = `${options.repeats} · ${options.repeat_parallel ? 'параллельно' : 'последовательно'}`;
  const curl = mode === 'multi' ? `${curlParallelism()} параллельно` : 'не применяется';
  const timeouts = runTimeoutSettings();
  const timeoutText = isBs
    ? `HTTP/TLS ${timeouts.curl_max_time}с`
    : `HTTP/TLS ${timeouts.curl_max_time}с · QUIC ${timeouts.curl_max_time_quic}с · DoH ${timeouts.curl_max_time_doh}с`;
  const protocolText = isBs
    ? (options.enable_tls13 && !options.enable_tls12 ? 'TLS 1.3 (IPv4)' : 'TLS 1.2 (IPv4)')
    : protocolSummary(options);
  const engineItems = isBs ? [
    ['Стратегии (пресет)', options.strategy_preset || 'конфиги BS по умолчанию'],
    ['Режим повторов', options.repeats_mode || 'fast'],
    ['Адаптивная очередь', options.bs_adaptive ? 'вкл' : 'выкл']
  ] : [];
  return {
    readiness: runLaunchReadiness(domains, options),
    items: [
      ['Домены запуска', `${domains.length}`],
      ['Обязательные', `${systemPresetCount('finder', 'required')}`],
      ['Желательные', `${systemPresetCount('finder', 'desired')}`],
      ['Источник', selectedFinderPresetSummary()],
      ['Режим', selectedRunModeLabel()],
      ['Проверочные запросы', curl],
      ['Протоколы', protocolText],
      ['IP-режим', isBs ? 'IPv4' : (options.enable_ipv6 ? 'IPv4 + IPv6' : 'IPv4')],
      ['Глубина', scanLevelLabel(options.scan_level || 'standard')],
      ['DNS/IP-check', checks],
      ['Повторы', repeats],
      ['Лимит времени', limit ? formatDuration(limit) : 'без лимита'],
      ['Таймауты', timeoutText],
      ...engineItems
    ]
  };
}
function renderRunLaunchSummary(){
  const grid = el('run-launch-summary-grid');
  const badgeNode = el('run-launch-readiness');
  if (!grid || !badgeNode) return;
  const summary = runLaunchSummaryItems();
  badgeNode.textContent = summary.readiness.text;
  badgeNode.className = `badge ${summary.readiness.tone}`;
  grid.innerHTML = summary.items.map(([label, value]) => `<div class="run-launch-summary-item">
    <div class="run-launch-summary-label">${esc(label)}</div>
    <div class="run-launch-summary-value">${esc(value)}</div>
  </div>`).join('');
}
function collectRunPreferences(){
  const timeoutHours = Number(el('finder-timeout-hours')?.value || 6);
  return {
    domains: selectedFinderDomains(),
    domain_preset: el('finder-preset-select')?.value || CUSTOM_SELECT_VALUE,
    discovery_profile: el('discovery-profile-select')?.value || CUSTOM_SELECT_VALUE,
    run_mode: selectedRunMode(),
    curl_parallelism: curlParallelism(),
    ...discoveryOptions(),
    limit_time_enabled: Boolean(el('limit-time-enabled')?.checked),
    timeout_hours: Number.isFinite(timeoutHours) ? timeoutHours : 6
  };
}
function useRunPreferencesOnce(){
  if (state.runPreferencesApplied || !state.runPreferences) return;
  const prefs = state.runPreferences || {};
  state.loadingRunPreferences = true;
  try {
    const domains = Array.isArray(prefs.domains) ? uniqueDomains(prefs.domains) : [];
    const presetSelect = el('finder-preset-select');
    const presetValue = String(prefs.domain_preset || 'system:required');
    if (presetSelect && [...presetSelect.options].some((option) => option.value === presetValue)) {
      presetSelect.value = presetValue;
    }
    if (domains.length) {
      el('finder-domains').value = domains.join('\n');
      state.domainsTouched = presetSelect?.value === CUSTOM_SELECT_VALUE;
      state.domainsInitialized = true;
    } else if (presetSelect && presetSelect.value !== CUSTOM_SELECT_VALUE) {
      const presetDomainsList = uniqueDomains(presetDomains('finder', presetSelect.value));
      if (presetDomainsList.length) {
        el('finder-domains').value = presetDomainsList.join('\n');
        state.domainsTouched = false;
        state.domainsInitialized = true;
      }
    }
    updateEditorLineNumbers('finder-domains');

    const discoverySelect = el('discovery-profile-select');
    if (discoverySelect) {
      const value = String(prefs.discovery_profile || 'standard');
      discoverySelect.value = [...discoverySelect.options].some((option) => option.value === value) ? value : 'standard';
    }
    const runMode = String(prefs.run_mode || 'standard') === 'multi' ? 'multi' : 'standard';
    const runModeInput = document.querySelector(`input[name="run-mode"][value="${runMode}"]`);
    if (runModeInput) runModeInput.checked = true;
    el('curl-parallelism').value = String(prefs.curl_parallelism || 4);
    el('enable-http').checked = Boolean(prefs.enable_http);
    el('enable-tls12').checked = Boolean(prefs.enable_tls12 ?? true);
    el('enable-tls13').checked = Boolean(prefs.enable_tls13);
    el('include-quic').checked = Boolean(prefs.include_quic ?? true);
    el('enable-ipv6').checked = Boolean(prefs.enable_ipv6);
    el('scan-level').value = prefs.scan_level || 'standard';
    el('repeats').value = String(prefs.repeats || 1);
    el('repeat-parallel').checked = Boolean(prefs.repeat_parallel);
    el('skip-dnscheck').checked = Boolean(prefs.skip_dnscheck ?? true);
    el('skip-ipblock').checked = Boolean(prefs.skip_ipblock ?? true);
    el('limit-time-enabled').checked = Boolean(prefs.limit_time_enabled);
    el('finder-timeout-hours').value = String(prefs.timeout_hours || 6);
    el('run-curl-max-time').value = String((state.settings || {}).curl_max_time || 2);
    el('run-curl-max-time-quic').value = String((state.settings || {}).curl_max_time_quic || 2);
    el('run-curl-max-time-doh').value = String((state.settings || {}).curl_max_time_doh || 2);
    syncTimeLimitUi();
    renderDiscoveryProfileNote();
    renderRunModeNote();
  } finally {
    state.loadingRunPreferences = false;
    state.runPreferencesApplied = true;
  }
}
async function saveRunPreferencesNow(){
  if (!state.runPreferencesApplied || state.loadingRunPreferences || state.savingRunPreferences) return;
  state.savingRunPreferences = true;
  const payload = collectRunPreferences();
  try {
    const data = await postJson(apiEndpoint('web', 'runPreferences'), { run_preferences: payload });
    state.runPreferences = (data || {}).run_preferences || payload;
  } catch (_error) {
    // Best-effort persistence: the run itself must not fail because UI state was not saved.
  } finally {
    state.savingRunPreferences = false;
  }
}
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
  'create-clean-install-vault',
  'preset-editor-save',
  'preset-editor-delete',
  'preset-new-save',
  'v2fly-load-categories',
  'v2fly-preview',
  'v2fly-import'
]);
function isRunLaunchSummaryControl(target){
  if (!target) return false;
  if (target.name === 'run-mode') return true;
  return RUN_LAUNCH_SUMMARY_CONTROL_IDS.has(String(target.id || ''));
}
function markDiscoveryProfileCustom(){
  if (state.loadingDiscoveryProfile) return;
  renderDiscoveryProfileNote();
}
function useDiscoveryProfile(profile){
  if (!profile) return;
  state.loadingDiscoveryProfile = true;
  try {
    el('scan-level').value = profile.scan_level || 'standard';
    renderDiscoveryProfileNote();
  } finally {
    state.loadingDiscoveryProfile = false;
  }
}
function renderDiscoveryProfileNote(){
  const note = el('discovery-profile-note');
  if (!note) return;
  const select = el('discovery-profile-select');
  const profile = select ? (state.discoveryProfiles || {})[select.value] : null;
  const scanLevel = String(profile?.scan_level || el('scan-level')?.value || 'standard');
  const title = profileTitle(scanLevel, profile);
  const details = {
    quick: 'меньше комбинаций, быстрее первичная проверка.',
    standard: 'основной режим для обычного подбора.',
    force: 'больше комбинаций, работает дольше.'
  }[scanLevel] || 'настройки изменены вручную.';
  note.textContent = `${title}: ${details}`;
}
function selectedRunMode(){
  return document.querySelector('input[name="run-mode"]:checked')?.value || 'standard';
}
function renderRunModeNote(){
  const note = el('run-mode-note');
  if (!note) return;
  const mode = selectedRunMode();
  const curlField = el('multi-curl-field');
  if (curlField) curlField.hidden = mode !== 'multi';
  if (mode === 'multi') {
    note.textContent = 'Режим “Все домены на одной стратегии”: одна стратегия запускается один раз, затем домены проверяются параллельно.';
    return;
  }
  note.textContent = 'Обычный режим: штатная проверка стратегий проходит по своему порядку.';
}
function profileTitle(name, profile){
  return String((profile && profile.title) || name || '-');
}
function renderDiscoveryProfiles(){
  const select = el('discovery-profile-select');
  if (!select) return;
  const current = select.value;
  const profiles = state.discoveryProfiles || {};
  const names = Object.keys(profiles).sort((a, b) => profileTitle(a, profiles[a]).localeCompare(profileTitle(b, profiles[b])));
  select.innerHTML = names.map((name) => `<option value="${esc(name)}">${esc(profileTitle(name, profiles[name]))}</option>`).join('');
  if (current && profiles[current]) select.value = current;
  else if (profiles.standard) select.value = 'standard';
  else if (names.length) select.value = names[0];
  renderDiscoveryProfileNote();
}
function hasEnabledProtocol(options){
  return Boolean(options.enable_http || options.enable_tls12 || options.enable_tls13 || options.include_quic);
}
function parseDomains(raw){
  return [...new Set(String(raw || '').split(/[,\s]+/).map((item) => item.trim()).filter(Boolean))];
}
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
