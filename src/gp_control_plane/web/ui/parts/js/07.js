}
function progressLiveElapsedSeconds(progress){
  if (progress.elapsed_seconds == null) return null;
  const base = Math.max(0, Number(progress.elapsed_seconds || 0));
  const receivedAt = Number(progress.received_at_ms || 0);
  if (!isBusy() || !receivedAt) return base;
  return base + Math.max(0, Math.floor((Date.now() - receivedAt) / 1000));
}
function progressLiveEtaSeconds(progress){
  if (progress.eta_seconds == null) return null;
  const base = Math.max(0, Number(progress.eta_seconds || 0));
  const baseElapsed = Math.max(0, Number(progress.elapsed_seconds || 0));
  const liveElapsed = progressLiveElapsedSeconds(progress);
  if (liveElapsed == null) return base;
  return Math.max(0, base - Math.max(0, liveElapsed - baseElapsed));
}
function etaModeLabel(progress){
  const status = String(progress.eta_status || '');
  const progressStatus = String(progress.progress_status || '');
  if (status === 'sample') return 'по live-скорости';
  if (status === 'calculating') return 'сбор выборки';
  if (status === 'elapsed_average') return 'по среднему времени попытки';
  if (status === 'underestimated' || progressStatus === 'underestimated') return 'уточняется';
  if (status === 'complete') return 'завершено';
  if (status === 'estimated') return 'по таймауту';
  return status || '-';
}
function etaStatusText(status){
  if (status === 'calculating') return 'рассчитывается';
  if (status === 'underestimated') return 'уточняется';
  return '-';
}
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
function v2flyCategoryName(category){
  if (typeof category === 'string') return category;
  if (category && typeof category === 'object') return String(category.name || category.id || '').trim();
  return '';
}
function v2flyAllCategories(){
  const categories = (state.v2flyCategories || {}).categories;
  return Array.isArray(categories) ? categories.map(v2flyCategoryName).filter(Boolean) : [];
}
function v2flyCategoryQuery(){
  return String(el('v2fly-category-search')?.value || '').trim().toLowerCase();
}
function v2flyExactCategory(){
  const query = v2flyCategoryQuery();
  if (!query) return '';
  return v2flyAllCategories().includes(query) ? query : '';
}
function v2flyCategories(){
  const category = v2flyExactCategory();
  return category ? [category] : [];
}
function clearV2flyDomains(){
  const domains = el('v2fly-domains');
  if (!domains) return;
  domains.value = '';
  updateEditorLineNumbers('v2fly-domains');
}
function suggestV2flyPresetName(){
  const nameInput = el('v2fly-preset-name');
  if (!nameInput) return;
  const current = String(nameInput.value || '').trim();
  if (current && !current.startsWith('v2fly-')) return;
  const categories = v2flyCategories();
  if (!categories.length) return;
  nameInput.value = `v2fly-${categories.slice(0, 3).join('-')}`.slice(0, 80);
}
function v2flyPayload(){
  return {
    scope: 'finder',
    name: String(el('v2fly-preset-name')?.value || '').trim(),
    categories: v2flyCategories(),
    domains: parseDomains(el('v2fly-domains')?.value || '')
  };
}
function renderV2flyPreview(){
  const target = el('v2fly-preview-result');
  if (!target) return;
  const preview = state.v2flyPreview;
  target.classList.toggle('bad', Boolean(preview && preview.error));
  if (!preview) {
    target.textContent = 'Список не проверялся.';
    return;
  }
  if (preview.loading) {
    target.textContent = preview.message || 'Загружаю домены выбранной группы...';
    return;
  }
  if (preview.error) {
    target.textContent = preview.message || 'Ошибка v2fly.';
    return;
  }
  const added = Array.isArray(preview.added) ? preview.added.length : 0;
  const removed = Array.isArray(preview.removed) ? preview.removed.length : 0;
  const skipped = preview.skipped && typeof preview.skipped === 'object'
    ? Object.values(preview.skipped).reduce((sum, value) => sum + Number(value || 0), 0)
    : 0;
  const coverageNote = preview.coverage_note ? 'Публично известный проверяемый набор, не гарантия полного покрытия сервиса.' : '';
  target.innerHTML = [
    `<div><strong>${esc(preview.preset || '-')}</strong>: ${esc(preview.count || 0)} доменов</div>`,
    `<div>Добавится: ${esc(added)}, уйдет: ${esc(removed)}, без изменений: ${esc(preview.unchanged_count || 0)}</div>`,
    skipped ? `<div>Часть правил не добавлена автоматически: ${esc(skipped)}</div>` : '',
    coverageNote ? `<div>${esc(coverageNote)}</div>` : ''
  ].join('');
}
function setV2flyLocalError(message){
  state.v2flyPreview = { error: true, message };
  renderV2flyPreview();
}
function renderV2flyCategoryCatalog(){
  const target = el('v2fly-category-status');
  const data = state.v2flyCategories || {};
  const categories = v2flyAllCategories();
  const query = v2flyCategoryQuery();
  const visible = query ? categories.filter((category) => category.includes(query)) : categories;
  const options = el('v2fly-category-options');
  if (options) options.innerHTML = visible.slice(0, 500).map((category) => `<option value="${esc(category)}"></option>`).join('');
  const matchList = el('v2fly-category-matches');
  const exact = v2flyExactCategory();
  if (matchList) {
    const matches = visible.slice(0, 24);
    matchList.innerHTML = matches.length
      ? matches.map((category) => `<button class="secondary category-match${category === exact ? ' active' : ''}" type="button" data-action="v2fly-select-category" data-category="${esc(category)}">${esc(category)}</button>`).join('')
      : '';
  }
  const button = document.querySelector('[data-action="v2fly-load-categories"]');
  const loading = state.v2flyCategorySource === 'loading';
  if (button) {
    button.disabled = loading;
    button.textContent = loading ? 'Читаю каталог' : 'Перечитать каталог';
    button.title = 'Перечитывает локальный каталог групп v2fly. Каталог скачивается при установке или обновлении сервиса.';
  }
  if (!target) return;
  if (loading) {
    target.textContent = 'Читаю локальный каталог v2fly...';
    return;
  }
  if (!categories.length) {
    target.textContent = data.error_message ? `Локальный каталог v2fly недоступен: ${data.error_message}` : 'Локальный каталог v2fly еще не подготовлен. Он скачивается при установке или обновлении сервиса.';
    return;
  }
  const selected = exact || '';
  const queryText = query ? ` Найдено по вводу: ${visible.length}.` : '';
  const selectText = selected ? ` Выбрано: ${selected}.` : (query ? ' Выберите точную группу из подсказок ниже.' : '');
  target.textContent = `Локальный каталог готов: ${data.all_count || categories.length} групп.${queryText}${selectText}`;
}
function presetManagerMeta(scope){
  return (state.customPresetMeta && state.customPresetMeta[scope]) || {};
}
function renderPresetManager(){
  const nameSelect = el('preset-manager-name');
  if (!nameSelect) return;
  const manager = state.presetManager;
  const scope = 'finder';
  const entries = managerPresetEntries();
  const names = entries.map((item) => item.name);
  if (!manager.name || !names.includes(manager.name)) manager.name = names[0] || '';
  const entry = manager.name ? managerPresetEntry(manager.name) : null;
  const isStoredUser = manager.name ? hasCustomPreset(scope, manager.name) : false;
  const isSystem = entry && entry.kind === 'system';
  const sourceScope = isStoredUser ? customPresetSourceScope(scope, manager.name) : scope;
  manager.scope = sourceScope;
  nameSelect.innerHTML = entries.length
    ? entries.map((item) => `<option value="${esc(item.name)}">${esc(item.label)} (${esc(item.count)})</option>`).join('')
    : '<option value="">Нет списков</option>';
  nameSelect.value = manager.name || '';
  const meta = isSystem ? systemPresetMeta(sourceScope, manager.name) : (isStoredUser ? presetManagerMeta(sourceScope)[manager.name] : null);
  const count = meta ? `${meta.enabled_count || 0}/${meta.total_count || 0}` : (entry ? `${entry.count}/${entry.count}` : '0');
  setText('preset-manager-count', count);
  const deleteButton = document.querySelector('button[data-action="preset-editor-delete"]');
  if (deleteButton) deleteButton.disabled = !isStoredUser || isSystem;
  const note = el('preset-manager-note');
  if (!manager.name) {
    note.textContent = 'Списков пока нет. Создайте список в подборе или импортируйте его из v2fly.';
    return;
  }
  const updated = meta && meta.updated_at ? ` · обновлено ${friendlyDate(meta.updated_at)}` : '';
  if (isSystem) {
    note.textContent = `Системный список "${entry.label}" всегда существует. Домены можно менять до пустого списка, удалить сам список нельзя. Доменов: ${meta ? meta.enabled_count : entry?.count || 0}${updated}.`;
    return;
  }
  note.textContent = `Редактируется список "${manager.name}". Доменов: ${meta ? meta.enabled_count : entry?.count || 0}${updated}${isStoredUser ? '' : ' · готовый список станет редактируемым после сохранения'}.`;
}
function renderPresetEditorPreview(preview){
  const target = el('preset-editor-preview');
  if (!target) return;
  if (!preview) {
    target.textContent = 'Изменения еще не проверялись.';
    return;
  }
  target.innerHTML = [
    `<div><strong>${esc(preview.name)}</strong>: ${esc(preview.total)} уникальных доменов</div>`,
    `<div>Добавится: ${esc(preview.added)}, удалится: ${esc(preview.removed)}, без изменений: ${esc(preview.unchanged)}</div>`
  ].join('');
}
function presetEditorDomains(){
  return uniqueDomains(parseDomains(el('preset-editor-domains')?.value || ''));
}
function presetEditorScope(){
  return 'finder';
}
function presetEditorName(){
  return String(el('preset-manager-name')?.value || '').trim();
}
function presetEditorKind(){
  const entry = managerPresetEntry(presetEditorName());
  return entry && entry.kind === 'system' ? 'system' : 'user';
}
async function loadPresetEditorFromSelection(options){
  const opts = options || {};
  const scope = presetEditorScope();
  const name = el('preset-manager-name')?.value || state.presetManager.name || '';
  if (!name) {
    if (!opts.silent) setMessage('Выберите список', 'warn');
    return;
  }
  try {
    const domains = await fetchAllPresetDomains(scope, name);
    const domainsInput = el('preset-editor-domains');
    if (domainsInput) {
      domainsInput.value = domains.join('\n');
      updateEditorLineNumbers('preset-editor-domains');
    }
    renderPresetEditorPreview({ name, total: domains.length, added: 0, removed: 0, unchanged: domains.length });
    if (!opts.silent) setMessage('Список загружен в редактор', 'good');
  } catch (error) {
    if (!opts.silent) setMessage(`Ошибка загрузки списка в редактор: ${error.message}`, 'bad');
  }
}
async function buildPresetEditorPreview(){
  const scope = presetEditorScope();
  const name = presetEditorName();
  const kind = presetEditorKind();
  const domains = presetEditorDomains();
  if (!name || (!domains.length && kind !== 'system')) {
    setMessage(kind === 'system' ? 'Выберите список' : 'Выберите список и оставьте хотя бы один домен', 'warn');
    return null;
  }
