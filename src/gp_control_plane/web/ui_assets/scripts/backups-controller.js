/* backups: explicit callbacks and a bounded view, no session/run owner. */
function createBackupsController({ view, apiClient, apiEndpoint, apiUrl, authFetch: requestBinary, currentSessionEpoch, el, ensureCandidateViewLoaded, esc, friendlyTime, getJson: requestJson, invalidateCandidateCaches, postJson: requestPost, refresh, renderMetrics, requestHeaders, requestUrl, sessionIsCurrent, setMessage, statusBadge }) {
  const state = view;
  const lifetime = new UiLifetime();
  const getJson=(url,options)=>requestJson(url,lifetime.requestOptions(options));
  const postJson=(url,payload)=>requestPost(url,payload,lifetime.requestOptions());
  const authFetch=(url,options)=>requestBinary(url,lifetime.requestOptions(options));

  const setTimeout = lifetime.setTimeout.bind(lifetime);
  const clearTimeout = lifetime.clearTimeout.bind(lifetime);
  const requestAnimationFrame = lifetime.requestAnimationFrame.bind(lifetime);

function renderBackups(){
  const rows = state.backups || [];
  const countNode = el('backups-count');
  if (countNode) countNode.textContent = String(rows.length);
  const updatedNode = el('backups-updated-at');
  if (updatedNode) {
    const updated = friendlyTime(state.backupsUpdatedAt);
    updatedNode.textContent = updated ? `Список обновлен ${updated}` : '';
  }
  const target = el('backups-table');
  if (!target) return;
  if (state.backupsLoading && !state.backupsLoaded) {
    target.innerHTML = '<div class="loading-skeleton" aria-label="Загрузка бекапов"></div>';
    return;
  }
  if (!rows.length) {
    target.innerHTML = `<div class="empty">${state.backupsLoaded ? 'Бекапов пока нет' : 'Откройте вкладку, чтобы загрузить бекапы'}</div>`;
    return;
  }
  target.innerHTML = rows.map((item) => backupCard(item)).join('');
  renderMetrics();
}

function backupCard(item){
  const id = String(item.id || '');
  return `<article class="backup-card l-stack">
    <div class="domain-header">
      <div>
        <h3>${esc(id)}</h3>
        <div class="helper-text">${esc(item.created_at || '-')}</div>
      </div>
      ${item.checksum_ok ? '' : statusBadge('checksum fail', 'bad')}
    </div>
    <div class="backup-meta">
      <div>Размер: ${esc(formatBytes(item.size_bytes || 0))}</div>
      <div>Стратегий: ${esc(item.strategy_count || 0)}</div>
    </div>
    <div class="backup-card-actions l-cluster">
      <button class="backup-archive-link" data-backup-download="${esc(id)}" type="button">Download archive</button>
      <button class="secondary" data-backup-restore="${esc(id)}" type="button">Восстановить из бекапа</button>
      <div class="action-consequence">Восстановление заменит текущие данные данными из этого бекапа.</div>
    </div>
    <div class="backup-danger-zone">
      <div class="backup-section-title">Необратимое удаление</div>
      <button class="secondary danger" data-backup-delete="${esc(id)}" type="button">Удалить бекап</button>
      <div class="action-consequence">Архив и его файлы будут удалены без возможности восстановления.</div>
    </div>
  </article>`;
}

function normalizeBackupSnapshot(item){
  const snapshotId = String(item.id || item.snapshot_id || '').trim();
  const counts = item.entity_counts || {};
  return {
    ...item,
    id: snapshotId,
    checksum_ok: Object.prototype.hasOwnProperty.call(item, 'checksum_ok') ? Boolean(item.checksum_ok) : item.checksum === 'ok',
    strategy_count: Number(item.strategy_count ?? counts.strategies ?? 0),
    preset_count: Number(item.preset_count ?? counts.domain_lists ?? 0)
  };
}

function backupListFromPayload(data){
  const items = Array.isArray((data || {}).snapshots) ? data.snapshots : (Array.isArray((data || {}).backups) ? data.backups : []);
  return items.map((item) => normalizeBackupSnapshot(item || {})).filter((item) => item.id);
}

function backupDownloadUrl(snapshot){
  const params = new URLSearchParams();
  params.set('snapshot_id', snapshot);
  return requestUrl(apiUrl('core', 'backupsDownloadArchive', params));
}

async function downloadBackup(url, snapshotId){
  const viewRequest=lifetime.capture(null);
  const id = String(snapshotId || '').trim();
  const epoch = currentSessionEpoch();
  try {
    const { blob, response } = await lifetime.result(apiClient.blob(url), viewRequest);
    if (!sessionIsCurrent(epoch)) return;
    const disposition = response.headers.get('Content-Disposition') || '';
    const filenameMatch = /filename="?([^";]+)"?/i.exec(disposition);
    const filename = filenameMatch ? filenameMatch[1] : `gp-backup-${id || 'archive'}.zip`;
    const objectUrl = URL.createObjectURL(blob);
    const releaseUrl = lifetime.ownCleanup(()=>URL.revokeObjectURL(objectUrl));
    const link = document.createElement('a');
    link.href = objectUrl;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(releaseUrl, 0);
  } catch (error) {
    if(!lifetime.isCurrent(viewRequest)) return;
    if (!sessionIsCurrent(epoch)) return;
    setMessage(`Archive download failed: ${error.message}`, 'bad');
  }
}

function formatBytes(value){
  const bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 Б';
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} КБ`;
  return `${(bytes / 1024 / 1024).toFixed(1)} МБ`;
}

async function refreshBackups(){
  const viewRequest=lifetime.capture("refreshBackups");
  const epoch = currentSessionEpoch();
  state.backupsLoading = true;
  renderBackups();
  try {
    const data = await lifetime.result(getJson(apiEndpoint('core', 'backupsList')), viewRequest);
    if (!sessionIsCurrent(epoch)) return;
    state.backups = backupListFromPayload(data);
    state.backupsLoaded = true;
    state.backupsUpdatedAt = new Date().toISOString();
    state.backupsLoading = false;
    renderBackups();
  } catch (error) {
    if(!lifetime.isCurrent(viewRequest)) return;
    if (!sessionIsCurrent(epoch)) return;
    state.backupsLoading = false;
    renderBackups();
    setMessage(`Ошибка загрузки сохранений: ${error.message}`, 'bad');
  }
}

async function createBackup(){
  const viewRequest=lifetime.capture(null);
  try {
    const data = await lifetime.result(postJson(apiEndpoint('core', 'backupsCreate'), {}), viewRequest);
    if (data.queued) {
      setMessage('Подбор идет. Бекап можно создать после остановки или завершения', 'warn');
    } else if (data.created || data.snapshot_id) {
      setMessage('Бекап создан', 'good');
    }
    await lifetime.result(refreshBackups(), viewRequest);
  } catch (error) {
    if(!lifetime.isCurrent(viewRequest)) return;
    if (isRuntimeBusyError(error)) {
      setMessage(backupBusyMessage('create'), 'warn');
      return;
    }
    setMessage(`Ошибка создания бекапа: ${error.message}`, 'bad');
  }
}

async function restoreBackup(snapshotId){
  const viewRequest=lifetime.capture(null);
  const id = String(snapshotId || '').trim();
  if (!id) return;
  const ok = window.confirm(`Восстановить данные из бекапа ${id}? Будут заменены найденные стратегии и связи стратегия-домен. Пользовательские пресеты не меняются.`);
  if (!ok) return;
  try {
    const data = await lifetime.result(postJson(apiEndpoint('core', 'backupsRestore'), { snapshot_id: id }), viewRequest);
    if (data.queued) {
      setMessage('Подбор идет. Восстановление можно выполнить после остановки или завершения', 'warn');
      return;
    }
    if (data.accepted || data.restored) {
      setMessage('Бекап восстановлен', 'good');
      invalidateCandidateCaches();
      await lifetime.result(refresh(), viewRequest);
      if (state.activeTab === 'candidates') ensureCandidateViewLoaded();
    }
  } catch (error) {
    if(!lifetime.isCurrent(viewRequest)) return;
    if (isRuntimeBusyError(error)) {
      setMessage(backupBusyMessage('restore'), 'warn');
      return;
    }
    setMessage(`Ошибка восстановления бекапа: ${error.message}`, 'bad');
  }
}

async function deleteBackup(snapshotId){
  const viewRequest=lifetime.capture(null);
  const id = String(snapshotId || '').trim();
  if (!id) return;
  const ok = window.confirm(`Удалить бекап ${id}? Архив и файлы бекапа будут удалены.`);
  if (!ok) return;
  try {
    const data = await lifetime.result(postJson(apiEndpoint('core', 'backupsDelete'), { snapshot_id: id }), viewRequest);
    if (data.queued) {
      setMessage('Подбор идет. Бекап можно удалить после остановки или завершения', 'warn');
      return;
    }
    if (data.deleted) {
      setMessage('Бекап удален', 'good');
      await lifetime.result(refreshBackups(), viewRequest);
    }
  } catch (error) {
    if(!lifetime.isCurrent(viewRequest)) return;
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
  const viewRequest=lifetime.capture(null);
  const epoch = currentSessionEpoch();
  const input = el('backup-upload-file');
  const file = input && input.files ? input.files[0] : null;
  if (!file) {
    setMessage('Выберите ZIP-архив бекапа', 'warn');
    return;
  }
  let response = null;
  try {
    response = await lifetime.result(authFetch(apiEndpoint('core', 'backupsUpload'), {
      method: 'POST',
      headers: requestHeaders({ 'Content-Type': 'application/zip' }),
      credentials: 'same-origin',
      body: file
    }), viewRequest);
    if (!sessionIsCurrent(epoch)) return;
    let data;
    try {
      data = await lifetime.result(response.json(), viewRequest);
    } catch (error) {
    if(!lifetime.isCurrent(viewRequest)) return;
      if (response.ok) throw error;
      data = {};
    }
    apiClient.assertResponseCurrent(response);
    if (!sessionIsCurrent(epoch)) return;
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
    await lifetime.result(refreshBackups(), viewRequest);
  } catch (error) {
    if(!lifetime.isCurrent(viewRequest)) return;
    if (!sessionIsCurrent(epoch)) return;
    setMessage(`Ошибка загрузки бекапа: ${error.message}`, 'bad');
  } finally {
if(lifetime.isCurrent(viewRequest)){
    apiClient.releaseResponse(response);
  }
}
}
  function dispose(){ lifetime.dispose();  }
function handleClickPart0(button){
if (button.dataset.backupDownload) {
    const snapshotId = button.dataset.backupDownload;
    downloadBackup(backupDownloadUrl(snapshotId), snapshotId);
    return true;
  }
 return false;
}

function handleClickPart1(button){
if (button.dataset.action === 'refresh-backups') {
    refreshBackups();
    return true;
  }
 return false;
}

function handleClickPart2(button){
if (button.dataset.action === 'create-backup') {
    createBackup();
    return true;
  }
 return false;
}

function handleClickPart3(button){
if (button.dataset.backupRestore) {
    restoreBackup(button.dataset.backupRestore);
    return true;
  }
 return false;
}

function handleClickPart4(button){
if (button.dataset.backupDelete) {
    deleteBackup(button.dataset.backupDelete);
    return true;
  }
 return false;
}

function handleClickPart5(button){
if (button.dataset.action === 'upload-backup') {
    uploadBackup();
    return true;
  }
 return false;
}
  return { handleClickPart0, handleClickPart1, handleClickPart2, handleClickPart3, handleClickPart4, handleClickPart5, renderBackups, backupCard, normalizeBackupSnapshot, backupListFromPayload, backupDownloadUrl, downloadBackup: lifetime.action(downloadBackup), formatBytes, refreshBackups: lifetime.action(refreshBackups), createBackup: lifetime.action(createBackup), restoreBackup: lifetime.action(restoreBackup), deleteBackup: lifetime.action(deleteBackup), isRuntimeBusyError, backupBusyMessage, uploadBackup: lifetime.action(uploadBackup), dispose };
}
