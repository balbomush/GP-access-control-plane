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
  if (ipv6) ipv6.checked = Boolean(settings.enable_ipv6);
  const engineSelect = el('settings-discovery-engine');
  if (engineSelect) engineSelect.value = settings.discovery_engine || 'blockcheck2';
  const finderEngine = el('finder-discovery-engine');
  if (finderEngine && !state.settingsTouched) finderEngine.value = settings.discovery_engine || 'blockcheck2';
  const bsPreset = el('bs-strategy-preset');
  if (bsPreset) bsPreset.value = settings.strategy_preset || '';
  const bsRepMode = el('bs-repeats-mode');
  if (bsRepMode) bsRepMode.value = settings.repeats_mode || 'fast';
  const bsAdaptive = el('bs-adaptive');
  if (bsAdaptive) bsAdaptive.checked = settings.bs_adaptive !== false;
  if (debugStdout) debugStdout.checked = Boolean(settings.debug_stdout);
  if (curlMax) curlMax.value = String(settings.curl_parallelism_max || 10);
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
  syncEngineUi();
}
function renderReleaseInfo(){
  const version = (state.status || {}).version || '-';
  const current = el('settings-release-current');
  if (current) current.textContent = `v${String(version).replace(/^v/, '')}`;
  const stable = el('settings-release-stable');
  const prerelease = el('settings-release-prerelease');
  const stableLink = el('settings-release-stable-link');
  const prereleaseLink = el('settings-release-prerelease-link');
  const result = el('settings-release-result');
  const selectedRelease = state.releaseStable;
  if (stable) stable.textContent = releaseVersionLabel(state.releaseStable);
  if (prerelease) prerelease.textContent = releaseVersionLabel(state.releasePrerelease);
  if (stableLink && state.releaseStable && state.releaseStable.url) stableLink.href = state.releaseStable.url;
  if (prereleaseLink && state.releasePrerelease && state.releasePrerelease.url) prereleaseLink.href = state.releasePrerelease.url;
  if (!selectedRelease) {
    if (result) {
      result.hidden = true;
      result.textContent = '';
    }
    return;
  }
  if (result) {
    result.hidden = false;
    if (selectedRelease.checked) {
      const update = selectedRelease.update_available ? 'Доступно обновление.' : 'Текущая версия не старее найденной.';
      const published = selectedRelease.published_at ? ` Опубликовано: ${friendlyDate(selectedRelease.published_at)}.` : '';
      const body = selectedRelease.body ? `

${String(selectedRelease.body).slice(0, 1200)}` : '';
      result.textContent = `${update} Канал: ${selectedRelease.channel}. Версия: ${selectedRelease.available_version || '-'}.${published}${body}`;
    } else {
      result.textContent = `Не удалось проверить релизы: ${selectedRelease.error || 'нет ответа GitHub'}. Ссылки на страницу релизов оставлены.`;
    }
  }
}
function releaseVersionLabel(release){
  if (state.releaseChecking && !release) return 'Проверяется...';
  if (!release) return 'Не проверялось';
  if (!release.checked) return 'Ошибка проверки';
  const suffix = release.update_available ? ' доступно' : ' актуально';
  return `${release.available_version || '-'} · ${suffix}`;
}
function currentSettingsFromForm(){
  const current = state.settings || {};
  const timeouts = runTimeoutSettings();
  return {
    enable_ipv6: Boolean(el('settings-enable-ipv6')?.checked),
    discovery_engine: el('settings-discovery-engine')?.value || selectedDiscoveryEngine(),
    debug_stdout: Boolean(el('settings-debug-stdout')?.checked),
    curl_parallelism_max: Number(el('settings-curl-max')?.value || 10),
    curl_parallelism_default: Number(current.curl_parallelism_default || 4),
    ...timeouts,
  };
}
const RUN_SETTING_PAYLOAD_KEYS = Object.freeze([
  'curl_parallelism_default',
  'curl_parallelism_max',
  'curl_max_time',
  'curl_max_time_quic',
  'curl_max_time_doh',
  'enable_ipv6',
  'debug_stdout',
  'discovery_engine'
]);
function runSettingsPayloadFromSettings(payload){
  const source = payload || {};
  const result = {};
  RUN_SETTING_PAYLOAD_KEYS.forEach((key) => {
    if (Object.prototype.hasOwnProperty.call(source, key)) result[key] = source[key];
  });
  return result;
}
async function fetchSettingsPayload(){
  const runSettings = await getJson(apiEndpoint('core', 'runSettings'));
  return { settings: runSettings || {} };
}
async function saveRunSettingsPayload(payload){
  const data = await postJson(apiEndpoint('core', 'saveRunSettings'), { settings: runSettingsPayloadFromSettings(payload) });
  return { settings: { ...(state.settings || {}), ...(data || {}) } };
}
async function saveSettingsPayload(payload){
  const runSettings = await postJson(apiEndpoint('core', 'saveRunSettings'), { settings: runSettingsPayloadFromSettings(payload) });
  return { settings: runSettings || {} };
}
async function saveLaunchTimeoutDefaultsNow(){
  const payload = currentSettingsFromForm();
  try {
    const data = await saveRunSettingsPayload(payload);
    state.settings = data.settings || { ...(state.settings || {}), ...payload };
    state.settingsTouched = false;
    renderRunLaunchSummary();
  } catch (_error) {
    // Best-effort persistence: the run payload already contains the selected timeout values.
  }
}
async function saveSettings(){
  try {
    const data = await saveSettingsPayload(currentSettingsFromForm());
    state.settings = data.settings || {};
    state.settingsTouched = false;
    renderSettings();
    setMessage('Настройки сохранены', 'good');
  } catch (error) {
    setMessage(`Ошибка сохранения настроек: ${error.message}`, 'bad');
  }
}
async function checkReleases(options = {}){
  const silent = Boolean(options.silent);
  state.releaseChecking = true;
  renderReleaseInfo();
  try {
    const data = await getJson(apiEndpoint('service', 'releasesAvailable'));
    rememberReleasePayload(data || {}, 'stable');
    state.releaseChecked = true;
    renderReleaseInfo();
    if (!silent) setMessage('Обновления проверены', 'good');
  } catch (error) {
    if (!silent) setMessage(`Ошибка проверки релизов: ${error.message}`, 'bad');
  } finally {
    state.releaseChecking = false;
    renderReleaseInfo();
  }
}
function releaseComparableVersion(value){
  return String(value || '').replace(/^v/, '').trim();
}
function normalizeServiceRelease(item, currentVersion){
  const availableVersion = String(item.available_version || item.version || item.ref || '').trim();
  const checked = Boolean(availableVersion) && !item.error;
  return {
    ...item,
    channel: item.channel || '',
    available_version: availableVersion,
    update_available: checked && releaseComparableVersion(availableVersion) !== releaseComparableVersion(currentVersion),
    checked,
    url: item.url || ''
  };
}
function rememberReleasePayload(data, selectedChannel){
  if (Array.isArray((data || {}).releases)) {
    const currentVersion = (data.current || {}).version || (state.status || {}).version || '';
    const releases = data.releases.map((item) => normalizeServiceRelease(item || {}, currentVersion));
    const stable = releases.find((item) => item.channel === 'stable');
    const prerelease = releases.find((item) => item.channel === 'prerelease');
    if (stable) state.releaseStable = stable;
    if (prerelease) state.releasePrerelease = prerelease;
    state.releaseInfo = (selectedChannel === 'prerelease' ? state.releasePrerelease : state.releaseStable) || state.releaseInfo;
    return;
  }
  const releases = (data || {}).releases || {};
  if (releases.stable) state.releaseStable = releases.stable;
  if (releases.prerelease) state.releasePrerelease = releases.prerelease;
  state.releaseInfo = (data || {}).release || state.releaseInfo;
  if (state.releaseInfo && state.releaseInfo.channel === 'stable') state.releaseStable = state.releaseInfo;
  if (state.releaseInfo && state.releaseInfo.channel === 'prerelease') state.releasePrerelease = state.releaseInfo;
}
