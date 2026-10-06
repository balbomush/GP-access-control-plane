/* settings: explicit callbacks and a bounded view, no session/run owner. */
function createSettingsController({ view, DISCOVERY_PROFILES, RUN_SETTING_PAYLOAD_KEYS, apiEndpoint, currentSessionEpoch, el, formatDuration, getJson: requestJson, postJson: requestPost, renderDiscoveryProfiles, renderPresetManager, renderReleaseInfo, renderRunLaunchSummary, renderRunModeNote, renderV2flyCategoryCatalog, renderV2flyPreview, runTimeoutSettings, sessionIsCurrent, setMessage , RUN_TIMEOUT_CONTROL_IDS}) {
  const state = view;
  const lifetime = new UiLifetime();
  let settingsDraftDirty = false;
  let settingsDraftRevision = 0;
  function handleDraftInput(event){
    if(event.target && ["settings-enable-ipv6","settings-debug-stdout","settings-curl-max"].includes(event.target.id)){settingsDraftDirty=true;settingsDraftRevision+=1;}
  }
  const getJson=(url,options)=>requestJson(url,lifetime.requestOptions(options));
  const postJson=(url,payload)=>requestPost(url,payload,lifetime.requestOptions());

  const setTimeout = lifetime.setTimeout.bind(lifetime);
  const clearTimeout = lifetime.clearTimeout.bind(lifetime);
  const requestAnimationFrame = lifetime.requestAnimationFrame.bind(lifetime);

function renderRunSettingsSummary(settings){
  const target = el('progress-metrics');
  if (!target) return;
  if (!settings || !Object.keys(settings).length) {
    target.textContent = 'Настройки запуска появятся после старта подбора.';
    return;
  }
  const protocols = [];
  if (settings.enable_http) protocols.push('HTTP');
  if (settings.enable_tls12) protocols.push('TLS 1.2');
  if (settings.enable_tls13) protocols.push('TLS 1.3');
  if (settings.enable_quic) protocols.push('QUIC');
  const domainCount = Number(settings.domain_count || 0);
  const mode = settings.kind === 'multi-domain-discovery' ? 'все домены на одной стратегии' : 'обычный';
  const ipMode = settings.enable_ipv6 ? 'IPv4+IPv6' : 'IPv4';
  const scan = scanLevelLabel(settings.scan_level || 'standard');
  const repeats = Number(settings.repeats || 1);
  const repeatMode = settings.repeat_parallel ? 'повторы параллельно' : 'повторы последовательно';
  const curl = settings.curl_parallelism ? `проверочных запросов: ${settings.curl_parallelism}` : '';
  const limit = Number(settings.timeout_seconds || 0) > 0 ? `лимит: ${formatDuration(Number(settings.timeout_seconds || 0))}` : 'без лимита';
  const checks = [
    settings.skip_dnscheck ? 'без DNS-проверки' : 'с DNS-проверкой',
    settings.skip_ipblock ? 'без IP-проверки' : 'с IP-проверкой',
  ].join(', ');
  const timeouts = `таймауты HTTP/TLS ${settings.curl_max_time || 2}с, QUIC ${settings.curl_max_time_quic || 2}с, DoH ${settings.curl_max_time_doh || 2}с`;
  target.textContent = [
    `доменов: ${domainCount}`,
    `режим: ${mode}`,
    `протоколы: ${protocols.join('+') || '-'}`,
    ipMode,
    `глубина: ${scan}`,
    `повторы: ${repeats}`,
    repeatMode,
    curl,
    checks,
    limit,
    timeouts,
  ].filter(Boolean).join(' · ');
}

function scanLevelLabel(value){
  const profile = DISCOVERY_PROFILES[String(value || 'standard')];
  return profile ? profile.title : String(value || '-');
}

function renderSettings(){
  const settings = state.settings || {};
  const ipv6 = el('settings-enable-ipv6');
  const debugStdout = el('settings-debug-stdout');
  const curlMax = el('settings-curl-max');
  const runCurlMaxTime = el('run-curl-max-time');
  const runCurlMaxTimeQuic = el('run-curl-max-time-quic');
  const runCurlMaxTimeDoh = el('run-curl-max-time-doh');
  if (!settingsDraftDirty) {
    if (ipv6) ipv6.checked = Boolean(settings.enable_ipv6);
    if (debugStdout) debugStdout.checked = Boolean(settings.debug_stdout);
    if (curlMax) curlMax.value = String(settings.curl_parallelism_max || 10);
  }
  renderReleaseInfo();
  if (!state.settingsTouched && !state.runPreferencesApplied) {
    const curlInput = el('curl-parallelism');
    if (curlInput) {
      curlInput.max = String(settings.curl_parallelism_max || 10);
      curlInput.value = String(settings.curl_parallelism_default || 4);
    }
    const finderIpv6 = el('enable-ipv6');
    if (finderIpv6) finderIpv6.checked = Boolean(settings.enable_ipv6);
    if (runCurlMaxTime) runCurlMaxTime.value = String(settings.curl_max_time || 2);
    if (runCurlMaxTimeQuic) runCurlMaxTimeQuic.value = String(settings.curl_max_time_quic || 2);
    if (runCurlMaxTimeDoh) runCurlMaxTimeDoh.value = String(settings.curl_max_time_doh || 2);
  } else {
    renderRunModeNote();
  }
  renderDiscoveryProfiles();
  renderV2flyCategoryCatalog();
  renderV2flyPreview();
  renderPresetManager();
}

function currentSettingsFromForm(){
  const current = state.settings || {};
  const timeouts = runTimeoutSettings();
  return {
    enable_ipv6: Boolean(el('settings-enable-ipv6')?.checked),
    debug_stdout: Boolean(el('settings-debug-stdout')?.checked),
    curl_parallelism_max: Number(el('settings-curl-max')?.value || 10),
    curl_parallelism_default: Number(current.curl_parallelism_default || 4),
    ...timeouts,
  };
}

function runSettingsPayloadFromSettings(payload){
  const source = payload || {};
  const result = {};
  RUN_SETTING_PAYLOAD_KEYS.forEach((key) => {
    if (Object.prototype.hasOwnProperty.call(source, key)) result[key] = source[key];
  });
  return result;
}

async function fetchSettingsPayload(options){
  const viewRequest=lifetime.capture(null);
  const runSettings = await lifetime.result(getJson(apiEndpoint('core', 'runSettings'), options), viewRequest);
  return { settings: runSettings || {} };
}

async function saveRunSettingsPayload(payload){
  const viewRequest=lifetime.capture(null);
  const data = await lifetime.result(postJson(apiEndpoint('core', 'saveRunSettings'), { settings: runSettingsPayloadFromSettings(payload) }), viewRequest);
  return { settings: { ...(state.settings || {}), ...(data || {}) } };
}

async function saveSettingsPayload(payload){
  const viewRequest=lifetime.capture(null);
  const runSettings = await lifetime.result(postJson(apiEndpoint('core', 'saveRunSettings'), { settings: runSettingsPayloadFromSettings(payload) }), viewRequest);
  return { settings: runSettings || {} };
}

async function saveLaunchTimeoutDefaultsNow(epoch = currentSessionEpoch()){
  const viewRequest=lifetime.capture(null);
  if (!sessionIsCurrent(epoch)) return false;
  const payload = currentSettingsFromForm();
  try {
    const data = await lifetime.result(saveRunSettingsPayload(payload), viewRequest);
    if (!sessionIsCurrent(epoch)) return false;
    state.settings = data.settings || { ...(state.settings || {}), ...payload };
    state.settingsTouched = false;
    renderRunLaunchSummary();
    return true;
  } catch (_error) {
    if(!lifetime.isCurrent(viewRequest)) return;
    // Best-effort persistence: the run payload already contains the selected timeout values.
    return false;
  }
}

async function saveSettings(){
  const viewRequest=lifetime.capture("saveSettings");
  const draftRevision = settingsDraftRevision;
  const epoch = currentSessionEpoch();
  try {
    const data = await lifetime.result(saveSettingsPayload(currentSettingsFromForm()), viewRequest);
    if (!sessionIsCurrent(epoch)) return false;
    state.settings = data.settings || {};
    if (draftRevision === settingsDraftRevision) settingsDraftDirty = false;
    state.settingsTouched = false;
    renderSettings();
    setMessage('Настройки сохранены', 'good');
    return true;
  } catch (error) {
    if(!lifetime.isCurrent(viewRequest)) return;
    if (!sessionIsCurrent(epoch)) return false;
    setMessage(`Ошибка сохранения настроек: ${error.message}`, 'bad');
    return false;
  }
}
  function dispose(){ lifetime.dispose(); settingsDraftDirty=false;settingsDraftRevision+=1; }
function handleClickPart0(button){
if (button.dataset.action === 'save-settings') {
    saveSettings();
    return true;
  }
 return false;
}

function handleInputPart1(event){
if (event.target && ['curl-parallelism', 'enable-ipv6'].includes(event.target.id)) {
    state.settingsTouched = true;
  }
 return false;
}

function handleInputPart2(event){
if (event.target && RUN_TIMEOUT_CONTROL_IDS.has(event.target.id)) {
    state.settingsTouched = true;
  }
 return false;
}

function handleInputPart3(event){
if (event.target && String(event.target.id || '').startsWith('settings-')) {
    state.settingsTouched = true;
  }
 return false;
}

function handleChangePart4(event){
if (event.target && ['curl-parallelism', 'enable-ipv6'].includes(event.target.id)) {
    state.settingsTouched = true;
  }
 return false;
}

function handleChangePart5(event){
if (event.target && RUN_TIMEOUT_CONTROL_IDS.has(event.target.id)) {
    state.settingsTouched = true;
  }
 return false;
}

function handleChangePart6(event){
if (event.target && String(event.target.id || '').startsWith('settings-')) {
    state.settingsTouched = true;
  }
 return false;
}
  return { handleDraftInput, handleClickPart0, handleInputPart1, handleInputPart2, handleInputPart3, handleChangePart4, handleChangePart5, handleChangePart6, renderRunSettingsSummary, scanLevelLabel, renderSettings, currentSettingsFromForm, runSettingsPayloadFromSettings, fetchSettingsPayload: lifetime.action(fetchSettingsPayload), saveRunSettingsPayload: lifetime.action(saveRunSettingsPayload), saveSettingsPayload: lifetime.action(saveSettingsPayload), saveLaunchTimeoutDefaultsNow: lifetime.action(saveLaunchTimeoutDefaultsNow), saveSettings: lifetime.action(saveSettings), dispose };
}
