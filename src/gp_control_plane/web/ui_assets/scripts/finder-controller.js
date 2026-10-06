/* finder: explicit callbacks and a bounded view, no session/run owner. */
function createFinderController({ view, CUSTOM_SELECT_VALUE, RUN_LAUNCH_SUMMARY_CONTROL_IDS, acknowledgeRun, acknowledgedRunIsCurrent, apiEndpoint, builtInPresets, currentSessionEpoch, customPresetCount, el, esc, formatDuration, hasCompleteSystemStatus, isBusy, isStartRequestInFlight, postJson: requestPost, presetDomains, refresh, refreshAcknowledgedRun, renderAll, renderMetrics, runState, saveLaunchTimeoutDefaultsNow, scanLevelLabel, sessionIsCurrent, setBadge, setMessage, systemPresetCount, systemPresetLabel, testedDomains, updateEditorLineNumbers, zapretCompactStatus , DISCOVERY_PROFILE_CONTROL_IDS}) {
  const state = view;
  const lifetime = new UiLifetime();
  const postJson=(url,payload)=>requestPost(url,payload,lifetime.requestOptions());

  const setTimeout = lifetime.setTimeout.bind(lifetime);
  const clearTimeout = lifetime.clearTimeout.bind(lifetime);
  const requestAnimationFrame = lifetime.requestAnimationFrame.bind(lifetime);

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

function discoveryOptions(){
  const timeouts = runTimeoutSettings();
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
    ...timeouts
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
  if (!hasCompleteSystemStatus()) return { text: 'Проверяем систему', tone: 'pending' };
  const status = state.status || {};
  const ready = zapretCompactStatus(status.zapret2 || {}).ready;
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
  const checks = [
    options.skip_dnscheck ? 'DNS: пропуск' : 'DNS: проверять',
    options.skip_ipblock ? 'IP/port: пропуск' : 'IP/port: проверять'
  ].join(', ');
  const repeats = `${options.repeats} · ${options.repeat_parallel ? 'параллельно' : 'последовательно'}`;
  const curl = mode === 'multi' ? `${curlParallelism()} параллельно` : 'не применяется';
  const timeouts = runTimeoutSettings();
  const timeoutText = `HTTP/TLS ${timeouts.curl_max_time}с · QUIC ${timeouts.curl_max_time_quic}с · DoH ${timeouts.curl_max_time_doh}с`;
  return {
    readiness: runLaunchReadiness(domains, options),
    items: [
      ['Домены запуска', `${domains.length}`],
      ['Обязательные', `${systemPresetCount('finder', 'required')}`],
      ['Желательные', `${systemPresetCount('finder', 'desired')}`],
      ['Источник', selectedFinderPresetSummary()],
      ['Режим', selectedRunModeLabel()],
      ['Проверочные запросы', curl],
      ['Протоколы', protocolSummary(options)],
      ['IP-режим', options.enable_ipv6 ? 'IPv4 + IPv6' : 'IPv4'],
      ['Глубина', scanLevelLabel(options.scan_level || 'standard')],
      ['DNS/IP-check', checks],
      ['Повторы', repeats],
      ['Лимит времени', limit ? formatDuration(limit) : 'без лимита'],
      ['Таймауты', timeoutText]
    ]
  };
}

function renderRunLaunchSummary(){
  const grid = el('run-launch-summary-grid');
  const badgeNode = el('run-launch-readiness');
  if (!grid || !badgeNode) return;
  const summary = runLaunchSummaryItems();
  setBadge(badgeNode, summary.readiness.text, summary.readiness.tone);
  grid.innerHTML = summary.items.map(([label, value]) => `<div class="run-launch-summary-item l-stack">
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

async function saveRunPreferencesNow(epoch = currentSessionEpoch()){
  const viewRequest=lifetime.capture(null);
  if (!sessionIsCurrent(epoch) || !state.runPreferencesApplied || state.loadingRunPreferences || state.savingRunPreferences) return false;
  state.savingRunPreferences = true;
  const payload = collectRunPreferences();
  try {
    const data = await lifetime.result(postJson(apiEndpoint('web', 'runPreferences'), { run_preferences: payload }), viewRequest);
    if (!sessionIsCurrent(epoch)) return false;
    state.runPreferences = (data || {}).run_preferences || payload;
    return true;
  } catch (_error) {
    if(!lifetime.isCurrent(viewRequest)) return;
    // Best-effort persistence: the run itself must not fail because UI state was not saved.
    return false;
  } finally {
if(lifetime.isCurrent(viewRequest)){
    if (sessionIsCurrent(epoch)) state.savingRunPreferences = false;
  }
}
}

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

async function startJob(url, payload, text, expectedEpoch){
  const viewRequest=lifetime.capture(null);
  const epoch = expectedEpoch === undefined ? currentSessionEpoch() : expectedEpoch;
  if (!sessionIsCurrent(epoch) || isBusy() || isStartRequestInFlight()) return null;
  runState.setStartInFlight(true);
  renderMetrics();
  try {
    setMessage(`Отправляем запрос на запуск: ${text}`, 'warn');
    const response = await lifetime.result(postJson(url, payload || {}), viewRequest);
    if (!sessionIsCurrent(epoch)) return null;
    const runId = response?.run_id || '';
    if (!runId) throw new Error('Сервер не вернул идентификатор принятого запуска');
    const generation = acknowledgeRun(runId);
    runState.setStartInFlight(false);
    setMessage(`Запуск подтверждён: ${runId}`, 'good');
    renderAll({ skipCandidates: true });
    refreshAcknowledgedRun(generation).catch((error) => {
      if (acknowledgedRunIsCurrent(generation)) setMessage(`Ошибка обновления принятого запуска: ${error.message}`, 'bad');
    });
    return response;
  } catch (error) {
    if(!lifetime.isCurrent(viewRequest)) return;
    if (!sessionIsCurrent(epoch)) return null;
    runState.rejectAccepted();
    runState.setStartInFlight(false);
    setMessage(error.message, 'bad');
    renderMetrics();
    return null;
  }
}

function selectedCoreProtocols(options){
  const protocols = [];
  if (options.enable_http || options.enable_tls12 || options.enable_tls13) protocols.push('tcp');
  if (options.include_quic) protocols.push('quic');
  return protocols;
}

function coreStrategyDiscoveryPayload(mode, domains, options, timeout){
  const payload = {
    mode: mode === 'multi' ? 'multi_domain' : 'standard',
    domains,
    protocols: selectedCoreProtocols(options),
    settings: { ...options }
  };
  if (mode === 'multi') payload.curl_parallelism = curlParallelism();
  if (timeout !== null) payload.timeout_seconds = timeout;
  return payload;
}

async function startSelectedDiscovery(){
  const viewRequest=lifetime.capture(null);
  if (isBusy() || isStartRequestInFlight()) return;
  const epoch = currentSessionEpoch();
  const options = discoveryOptions();
  if (!hasEnabledProtocol(options)) {
    setMessage('Выберите хотя бы один протокол для проверки', 'bad');
    return;
  }
  const mode = selectedRunMode();
  const domains = finderDomains();
  if (!domains.length) {
    setMessage('Добавьте хотя бы один домен для подбора', 'bad');
    return;
  }
  const timeout = timeoutSecondsOrNull();
  const payload = coreStrategyDiscoveryPayload(mode, domains, options, timeout);
  await lifetime.result(saveLaunchTimeoutDefaultsNow(epoch), viewRequest);
  if (!sessionIsCurrent(epoch)) return;
  await lifetime.result(saveRunPreferencesNow(epoch), viewRequest);
  if (!sessionIsCurrent(epoch)) return;
  const title = mode === 'multi' ? 'Все домены на одной стратегии' : 'Поиск стратегий';
  await lifetime.result(startJob(apiEndpoint('core', 'startStrategyDiscoveryRun'), payload, title, epoch), viewRequest);
}

async function stopCurrentJob(){
  const viewRequest=lifetime.capture(null);
  const epoch = currentSessionEpoch();
  try {
    await lifetime.result(postJson(apiEndpoint('core', 'stopCurrentStrategyDiscoveryRun'), {}), viewRequest);
    if (!sessionIsCurrent(epoch)) return;
    setMessage('Остановка подбора запрошена', 'warn');
    await lifetime.result(refresh(), viewRequest);
  } catch (error) {
    if(!lifetime.isCurrent(viewRequest)) return;
    if (!sessionIsCurrent(epoch)) return;
    setMessage(error.message, 'bad');
    await lifetime.result(refresh(), viewRequest);
  }
}
  function dispose(){ lifetime.dispose();  }
function handleClickPart0(button){
if (button.dataset.fill) fillDomains(button.dataset.fill);
 return false;
}

function handleClickPart1(button){
if (button.dataset.action === 'run-selected-discovery') startSelectedDiscovery();
 return false;
}

function handleClickPart2(button){
if (button.dataset.action === 'stop-current') stopCurrentJob();
 return false;
}

function handleInputPart3(event){
if (event.target && DISCOVERY_PROFILE_CONTROL_IDS.has(event.target.id)) {
    markDiscoveryProfileCustom();
  }
 return false;
}

function handleInputPart4(event){
if (isRunLaunchSummaryControl(event.target)) {
    renderRunLaunchSummary();
  }
 return false;
}

function handleChangePart5(event){
if (event.target && event.target.id === 'limit-time-enabled') {
    syncTimeLimitUi();
    markDiscoveryProfileCustom();
  }
 return false;
}

function handleChangePart6(event){
if (event.target && event.target.id === 'discovery-profile-select') {
    const profile = (state.discoveryProfiles || {})[event.target.value];
    useDiscoveryProfile(profile);
  }
 return false;
}

function handleChangePart7(event){
if (event.target && event.target.name === 'run-mode') {
    renderRunModeNote();
  }
 return false;
}

function handleChangePart8(event){
if (event.target && DISCOVERY_PROFILE_CONTROL_IDS.has(event.target.id)) {
    markDiscoveryProfileCustom();
  }
 return false;
}

function handleChangePart9(event){
if (isRunLaunchSummaryControl(event.target)) {
    renderRunLaunchSummary();
  }
 return false;
}
  return { handleClickPart0, handleClickPart1, handleClickPart2, handleInputPart3, handleInputPart4, handleChangePart5, handleChangePart6, handleChangePart7, handleChangePart8, handleChangePart9, defaultDomains, uniqueDomains, uniqueDomainCount, fillDomains, finderDomains, selectedFinderDomains, timeoutSecondsOrNull, syncTimeLimitUi, curlParallelism, repeatsValue, minimumInputSeconds, runTimeoutSettings, discoveryOptions, selectedFinderPresetSummary, selectedRunModeLabel, protocolSummary, runLaunchReadiness, runLaunchSummaryItems, renderRunLaunchSummary, collectRunPreferences, useRunPreferencesOnce, saveRunPreferencesNow: lifetime.action(saveRunPreferencesNow), isRunLaunchSummaryControl, markDiscoveryProfileCustom, useDiscoveryProfile, renderDiscoveryProfileNote, selectedRunMode, renderRunModeNote, profileTitle, renderDiscoveryProfiles, hasEnabledProtocol, parseDomains, startJob: lifetime.action(startJob), selectedCoreProtocols, coreStrategyDiscoveryPayload, startSelectedDiscovery: lifetime.action(startSelectedDiscovery), stopCurrentJob: lifetime.action(stopCurrentJob), dispose };
}
