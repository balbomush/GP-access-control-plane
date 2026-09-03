    const nextCandidates = rows.length ? [...candidates, ...rows] : candidates;
    state.candidates = nextCandidates;
    state.candidateTotal = Number(data.total || state.candidateTotal || nextCandidates.length);
    state.candidateOffset = Number(data.offset || candidates.length) + rows.length;
    state.candidateHasMore = rows.length ? Boolean(data.has_more) : false;
    rememberCandidateVersion(data.version || null);
    updateTestedDomains(data.tested_domains);
    state.candidatesLoaded = true;
    state.commonLoadingMore = false;
    storeCommonCandidateCache(queryKey);
    renderCandidatesOnly();
  } catch (error) {
    setMessage(`Ошибка загрузки следующей страницы общих стратегий: ${error.message}`, 'bad');
    state.commonLoadingMore = false;
    renderCandidatesOnly();
  }
}
async function refreshCandidates(reset){
  const requestId = ++candidateRequestSeq;
  const offset = reset ? 0 : state.candidates.length;
  const queryKey = currentCandidateQueryKey();
  state.commonLoadingMore = false;
  state.candidateLoading = true;
  renderCandidatesOnly();
  try {
    const data = await getJson(apiUrl('web', 'strategyCandidatesPage', candidateParams(offset)));
    if (requestId !== candidateRequestSeq) return;
    const rows = data.candidates || [];
    state.candidates = reset ? rows : [...state.candidates, ...rows];
    state.candidateTotal = Number(data.total || 0);
    state.candidateOffset = Number(data.offset || 0);
    state.candidateHasMore = Boolean(data.has_more);
    rememberCandidateVersion(data.version || null);
    updateTestedDomains(data.tested_domains);
    state.candidatesLoaded = true;
    state.candidateQueryKey = queryKey;
    state.candidateUpdatedAt = new Date().toISOString();
    state.candidateLoading = false;
    if (queryKey.startsWith('common:')) storeCommonCandidateCache(queryKey);
    renderCandidatesOnly();
  } catch (error) {
    if (requestId !== candidateRequestSeq) return;
    state.candidateLoading = false;
    renderCandidatesOnly();
    setMessage(`Ошибка загрузки кандидатов: ${error.message}`, 'bad');
  }
}
function scheduleCandidateRefresh(){
  if (candidateRefreshTimer) clearTimeout(candidateRefreshTimer);
  candidateRefreshTimer = setTimeout(() => {
    candidateRefreshTimer = null;
    if (state.candidateView === 'domain') {
      state.domainStrategies = {};
      state.openCandidateDomains = {};
      refreshDomainIndex();
    } else {
      state.candidateResultRequested = false;
      prepareCommonCandidateState();
      renderCandidatesOnly();
      if (selectedCommonDomains().length >= 2) refreshCandidates(true);
    }
  }, 350);
}
function trimTextLines(text, maxLines){
  const lines = String(text || '').split('\n');
  if (lines.length <= maxLines) return lines.join('\n');
  return lines.slice(lines.length - maxLines).join('\n');
}
function appendLogText(base, addition){
  const left = String(base || '');
  const right = String(addition || '');
  if (!left || !right || left.endsWith('\n') || right.startsWith('\n')) return left + right;
  return `${left}\n${right}`;
}
function latestLogUrl(incremental){
  const busy = isBusy();
  const base = busy ? apiEndpoint('core', 'currentRunLatestLog') : apiEndpoint('core', 'latestLog');
  if (!incremental || !state.finderLog || !state.finderLog.stdout_log) {
    return base;
  }
  const params = new URLSearchParams();
  params.set('stdout_log', state.finderLog.stdout_log || '');
  params.set('stdout_size', String(state.finderLog.stdout_size || 0));
  params.set('stderr_log', state.finderLog.stderr_log || '');
  params.set('stderr_size', String(state.finderLog.stderr_size || 0));
  return `${base}?${params.toString()}`;
}
function mergeLogPayload(previous, next){
  if (!previous || !next) return next;
  if (next.progress) next.progress.received_at_ms = Date.now();
  const sameRun = previous.run_id && next.run_id && previous.run_id === next.run_id;
  const sameStdout = sameRun && previous.stdout_log && previous.stdout_log === next.stdout_log;
  const sameStderr = sameRun && previous.stderr_log && previous.stderr_log === next.stderr_log;
  if (sameStdout && next.stdout_append) {
    next.stdout_tail = trimTextLines(appendLogText(previous.stdout_tail, next.stdout_append), 200);
  }
  if (sameStderr && next.stderr_append) {
    next.stderr_tail = trimTextLines(appendLogText(previous.stderr_tail, next.stderr_append), 200);
  }
  if (sameStdout && !next.stdout_tail && !next.stdout_append) next.stdout_tail = previous.stdout_tail || '';
  if (sameStderr && !next.stderr_tail && !next.stderr_append) next.stderr_tail = previous.stderr_tail || '';
  return next;
}
function mergeStatusPayload(status){
  if (!status) return false;
  const previousSettings = JSON.stringify(state.settings || {});
  state.status = status;
  if (status.candidate_version) syncCandidateVersion(status.candidate_version);
  if (status.settings) state.settings = status.settings;
  if (status.run_preferences) state.runPreferences = status.run_preferences;
  renderMetrics();
  renderLiveRun();
  renderEvents();
  syncEngineUi();
  const settingsChanged = previousSettings !== JSON.stringify(state.settings || {});
  if (settingsChanged) renderSettings();
  return settingsChanged;
}
async function refreshRuns(reset = true){
  const offset = reset ? 0 : state.finderRunOffset;
  state.finderRunsLoading = true;
  renderRuns();
  try {
    const finderRuns = await getJson(apiUrl('web', 'runHistoryPage', runParams(offset)));
    mergeRunPage(finderRuns, reset);
    renderRuns();
    renderMetrics();
  } catch (error) {
    state.finderRunsLoading = false;
    renderRuns();
    setMessage(`Ошибка обновления истории: ${error.message}`, 'bad');
  }
}
async function refreshLog(incremental = false){
  try {
    const previous = state.finderLog;
    const payload = await getJson(latestLogUrl(incremental));
    if (payload.progress) payload.progress.received_at_ms = Date.now();
    state.finderLog = incremental ? mergeLogPayload(previous, payload) : payload;
    logDirty = false;
    renderLog();
    renderMetrics();
  } catch (error) {
    setMessage(`Ошибка обновления лога: ${error.message}`, 'bad');
  }
}
async function refreshPresets(){
  try {
    const presets = await getJson(apiEndpoint('web', 'presets'));
    mergePresetResponse(presets);
    renderPresetSelects();
    renderPresetManager();
  } catch (error) {
    setMessage(`Ошибка обновления пресетов: ${error.message}`, 'bad');
  }
}
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
    reader.cancel().catch(() => {});
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
