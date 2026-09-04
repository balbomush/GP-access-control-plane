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
function parseSseEvent(frame){
  let event = 'message';
  const data = [];
  for (const line of frame.split(/\r?\n/)) {
    if (!line || line.startsWith(':')) continue;
    const separator = line.indexOf(':');
    const field = separator < 0 ? line : line.slice(0, separator);
    const value = separator < 0 ? '' : line.slice(separator + 1).replace(/^ /, '');
    if (field === 'event') event = value;
    if (field === 'data') data.push(value);
  }
  return { event, data: data.join('\n') };
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
async function readRealtimeStream(response, signal){
  if (!response.body) throw new Error('SSE stream is unavailable');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  try {
    while (!signal.aborted) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      const frames = buffer.split(/\r?\n\r?\n/);
      buffer = frames.pop() || '';
      for (const frame of frames) {
        const parsed = parseSseEvent(frame);
        if (parsed.data) handleRealtimeEvent(parsed.event, parsed.data);
      }
    }
  } finally {
    reader.cancel().catch((cancelError) => {
      if (cancelError && cancelError.name !== 'AbortError') {
        console.warn('Realtime stream cancel failed', cancelError);
      }
    });
  }
}
function scheduleRealtimeReconnect(){
  if (!authToken() || realtimeReconnectTimer) return;
  const delay = realtimeReconnectDelay;
  realtimeReconnectDelay = Math.min(realtimeReconnectDelay * 2, 30000);
  realtimeReconnectTimer = setTimeout(() => {
    realtimeReconnectTimer = null;
    startRealtimeEvents();
  }, delay);
}
async function connectRealtimeEvents(controller){
  try {
    const response = await authFetch(apiEndpoint('web', 'eventsStream'), {
      headers: { Accept: 'text/event-stream' },
      signal: controller.signal
    });
    if (!response.ok) throw new Error(response.statusText || 'SSE connection failed');
    if (controller.signal.aborted) return;
    realtimeConnected = true;
    realtimeReconnectDelay = 1000;
    await readRealtimeStream(response, controller.signal);
  } catch (error) {
    if (!controller.signal.aborted) console.warn('Realtime connection stopped', error);
  } finally {
    if (realtimeSource === controller) realtimeSource = null;
    realtimeConnected = false;
    if (!controller.signal.aborted) scheduleRealtimeReconnect();
  }
}
function startRealtimeEvents(options){
  const alreadyStopped = Boolean(options && options.alreadyStopped);
  if (!alreadyStopped) stopRealtimeEvents();
  if (!authToken()) return;
  const controller = new AbortController();
  realtimeSource = controller;
  connectRealtimeEvents(controller);
}
function startRealtimeFallback(){
  if (realtimeFallbackTimer) clearInterval(realtimeFallbackTimer);
  realtimeFallbackTimer = setInterval(() => {
    if (!realtimeConnected) refresh({ light: true, silent: true });
  }, 30000);
}
function refreshRequestMap(light){
  const bootstrap = !light || !state.status;
  const requests = {
    status: getJson(apiEndpoint('core', 'status')),
    finderRuns: getJson(apiUrl('web', 'runHistoryPage', runParams(0))),
    finderLog: getJson(apiEndpoint('core', 'latestLog'))
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
  if (refreshInFlight) return;
  refreshInFlight = true;
  const light = Boolean(options.light);
  const { bootstrap, requests } = refreshRequestMap(light);
  const keys = Object.keys(requests);
  try {
    const settled = await Promise.allSettled(keys.map((key) => requests[key]));
    const results = Object.fromEntries(keys.map((key, index) => [key, settled[index]]));
    const status = settledValue(results, 'status');
    if (status) mergeStatusPayload(status);
    const settings = settledValue(results, 'settings');
    if (settings) state.settings = (settings || {}).settings || (status || {}).settings || state.settings || {};
    const finderRuns = settledValue(results, 'finderRuns');
    if (finderRuns) mergeRunPage(finderRuns, true);
    const finderLog = settledValue(results, 'finderLog');
    if (finderLog) {
      if (finderLog.progress) finderLog.progress.received_at_ms = Date.now();
      state.finderLog = finderLog;
    }
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
    if (!options.silent) setMessage(`Ошибка обновления: ${error.message}`, 'bad');
  } finally {
    refreshInFlight = false;
  }
}
async function refreshBackups(){
  state.backupsLoading = true;
  renderBackups();
  try {
    const data = await getJson(apiEndpoint('core', 'backupsList'));
    state.backups = backupListFromPayload(data);
    state.backupsLoaded = true;
    state.backupsUpdatedAt = new Date().toISOString();
    state.backupsLoading = false;
    renderBackups();
  } catch (error) {
    state.backupsLoading = false;
    renderBackups();
    setMessage(`Ошибка загрузки сохранений: ${error.message}`, 'bad');
  }
}
async function refreshCleanInstallVaults(){
  state.cleanInstallVaultsLoading = true;
  renderCleanInstallVaults();
  try {
    const data = await getJson(apiEndpoint('core', 'cleanInstallVaultsList'));
    state.cleanInstallVaults = cleanInstallVaultListFromPayload(data);
    state.cleanInstallVaultsLoaded = true;
    state.cleanInstallVaultsUpdatedAt = new Date().toISOString();
  } catch (error) {
    setMessage(`Ошибка загрузки vault: ${error.message}`, 'bad');
  } finally {
    state.cleanInstallVaultsLoading = false;
    renderCleanInstallVaults();
  }
}
async function createCleanInstallVault(){
  try {
    const data = await postJson(apiEndpoint('core', 'cleanInstallVaultsCreate'), {});
    if (!data.vault_id) throw new Error('Сервер не вернул идентификатор vault');
    setMessage('Vault создан. После чистой установки выберите его и явно подтвердите восстановление с удалением источника.', 'good');
    await refreshCleanInstallVaults();
  } catch (error) {
    if (isRuntimeBusyError(error)) {
      setMessage(backupBusyMessage('create'), 'warn');
      return;
    }
    setMessage(`Ошибка создания vault: ${error.message}`, 'bad');
  }
}
async function restoreCleanInstallVault(vaultId){
  const id = String(vaultId || '').trim();
  if (!id) return;
  const confirmed = window.confirm(`Восстановить данные из vault ${id} и удалить источник после проверки? Операция продолжится только после проверки данных и SQLite.`);
  if (!confirmed) return;
  try {
    const data = await postJson(apiEndpoint('core', 'cleanInstallVaultsRestore'), {
      vault_id: id,
      confirm_restore: true
    });
    if (!data.completed || !data.verification?.verified || !data.storage_status?.ready || !data.cleanup?.source_deleted) {
      throw new Error('Восстановление не подтвердило данные, SQLite и удаление исходного vault');
    }
    setMessage('Данные восстановлены, проверены, а исходный vault удален.', 'good');
    invalidateCandidateCaches();
    await Promise.all([refresh(), refreshCleanInstallVaults()]);
  } catch (error) {
    if (isRuntimeBusyError(error)) {
      setMessage(backupBusyMessage('restore'), 'warn');
      return;
    }
    setMessage(`Восстановление vault не завершено: ${error.message}. Исходный vault сохранен.`, 'bad');
  }
}
async function createBackup(){
  try {
    const data = await postJson(apiEndpoint('core', 'backupsCreate'), {});
    if (data.queued) {
      setMessage('Подбор идет. Бекап можно создать после остановки или завершения', 'warn');
    } else if (data.created || data.snapshot_id) {
      setMessage('Бекап создан', 'good');
    }
    await refreshBackups();
  } catch (error) {
    if (isRuntimeBusyError(error)) {
      setMessage(backupBusyMessage('create'), 'warn');
      return;
    }
    setMessage(`Ошибка создания бекапа: ${error.message}`, 'bad');
  }
}
async function restoreBackup(snapshotId){
  const id = String(snapshotId || '').trim();
  if (!id) return;
  const ok = window.confirm(`Восстановить данные из бекапа ${id}? Будут заменены найденные стратегии и связи стратегия-домен. Пользовательские пресеты не меняются.`);
  if (!ok) return;
  try {
    const data = await postJson(apiEndpoint('core', 'backupsRestore'), { snapshot_id: id });
    if (data.queued) {
      setMessage('Подбор идет. Восстановление можно выполнить после остановки или завершения', 'warn');
      return;
    }
    if (data.accepted || data.restored) {
      setMessage('Бекап восстановлен', 'good');
      invalidateCandidateCaches();
      await refresh();
      if (state.activeTab === 'candidates') ensureCandidateViewLoaded();
    }
  } catch (error) {
    if (isRuntimeBusyError(error)) {
      setMessage(backupBusyMessage('restore'), 'warn');
      return;
    }
    setMessage(`Ошибка восстановления бекапа: ${error.message}`, 'bad');
  }
}
async function deleteBackup(snapshotId){
  const id = String(snapshotId || '').trim();
  if (!id) return;
  const ok = window.confirm(`Удалить бекап ${id}? Архив и файлы бекапа будут удалены.`);
  if (!ok) return;
  try {
    const data = await postJson(apiEndpoint('core', 'backupsDelete'), { snapshot_id: id });
    if (data.queued) {
      setMessage('Подбор идет. Бекап можно удалить после остановки или завершения', 'warn');
      return;
    }
    if (data.deleted) {
      setMessage('Бекап удален', 'good');
      await refreshBackups();
    }
  } catch (error) {
    if (isRuntimeBusyError(error)) {
      setMessage(backupBusyMessage('delete'), 'warn');
      return;
    }
    setMessage(`Ошибка удаления бекапа: ${error.message}`, 'bad');
  }
}
function isRuntimeBusyError(error){
  return Boolean(error && error.status === 409 && (error.code === 'runtime_busy' || error.message === 'runtime_busy'));
}
function backupBusyMessage(action){
  if (action === 'restore') return 'Подбор идет. Восстановление можно выполнить после остановки или завершения';
  if (action === 'delete') return 'Подбор идет. Бекап можно удалить после остановки или завершения';
  if (action === 'upload') return 'Подбор идет. Загрузку бекапа можно выполнить после остановки или завершения';
  return 'Подбор идет. Бекап можно создать после остановки или завершения';
}
async function uploadBackup(){
  const input = el('backup-upload-file');
  const file = input && input.files ? input.files[0] : null;
  if (!file) {
    setMessage('Выберите ZIP-архив бекапа', 'warn');
    return;
  }
  try {
    const response = await authFetch(apiEndpoint('core', 'backupsUpload'), {
      method: 'POST',
      headers: requestHeaders({ 'Content-Type': 'application/zip' }),
      credentials: 'same-origin',
      body: file
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const apiError = data && typeof data.error === 'object' ? data.error : {};
      if (response.status === 409 && apiError.code === 'runtime_busy') {
        setMessage(backupBusyMessage('upload'), 'warn');
        return;
      }
      throw new Error(apiError.message || data.message || response.statusText);
    }
    setMessage('Бекап загружен и проверен', 'good');
    input.value = '';
    await refreshBackups();
  } catch (error) {
    setMessage(`Ошибка загрузки бекапа: ${error.message}`, 'bad');
  }
}
async function startJob(url, payload, text){
  try {
    setMessage(`${text} запущено`, 'warn');
    const response = await postJson(url, payload || {});
    const runId = response?.run_id || '';
    setMessage(runId ? `Задание ${runId} добавлено` : `${text} принято к выполнению`, 'good');
    await refresh();
    return response;
  } catch (error) {
    setMessage(error.message, 'bad');
    await refresh();
    return null;
  }
}
function selectedCoreProtocols(options){
  const protocols = [];
  if (options.enable_http || options.enable_tls12 || options.enable_tls13) protocols.push('tcp');
  if (options.include_quic) protocols.push('quic');
  return protocols;
}
async function exportNfconfNow(){
  try {
    const result = await postJson(apiEndpoint('core', 'exportNfconf'), { limit: 5 });
    const paths = (result.paths || []).join(', ');
    const previewEl = el('export-nfconf-preview');
    if (previewEl && Array.isArray(result.files) && result.files.length) {
      previewEl.innerHTML = result.files.map((file) => `
        <div class="nfconf-file-block" style="margin-bottom: 12px; border: 1px solid var(--border-color, #ccc); border-radius: 4px; padding: 8px;">
          <strong>${esc(file.filename)}</strong> <span class="helper-text">(${esc(file.path)})</span>
          <pre class="strategy-code" style="max-height: 200px; overflow-y: auto; background: var(--bg-alt, #f5f5f5); padding: 8px; margin-top: 4px;"><code>${esc(file.content)}</code></pre>
        </div>
      `).join('');
      const block = el('export-nfconf-block');
      if (block) block.open = true;
    }
    setMessage(paths ? `nfconf: ${paths}` : `nfconf записан в ${result.out_dir || '-'}`, 'good');
  } catch (error) {
    setMessage(error.message, 'bad');
  }
}
async function runTriageNow(){
  const domains = finderDomains();
  if (!domains.length) {
    setMessage('Укажите хотя бы один домен для проверки Triage', 'bad');
    return;
  }
  const target = domains[0];
  setMessage(`Запуск triage для домена ${target}…`, 'good');
  try {
    const data = await getJson(`${apiEndpoint('core', 'triage')}?domain=${encodeURIComponent(target)}`);
    if (data && data.status === 'ok') {
      const info = JSON.stringify(data.checks || data.output || data);
      setMessage(`Triage ${target}: ${info}`, 'good');
    } else {
      setMessage(`Triage ${target} ошибка: ${(data && data.message) || 'неизвестный сбой'}`, 'bad');
    }
  } catch (error) {
    setMessage(`Triage ошибка: ${error.message}`, 'bad');
  }
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
  await saveLaunchTimeoutDefaultsNow();
  await saveRunPreferencesNow();
  const title = mode === 'multi' ? 'Все домены на одной стратегии' : 'Поиск стратегий';
  await startJob(apiEndpoint('core', 'startStrategyDiscoveryRun'), payload, title);
}
async function stopCurrentJob(){
  try {
    await postJson(apiEndpoint('core', 'stopCurrentStrategyDiscoveryRun'), {});
    setMessage('Остановка подбора запрошена', 'warn');
    await refresh();
  } catch (error) {
    setMessage(error.message, 'bad');
    await refresh();
  }
}
