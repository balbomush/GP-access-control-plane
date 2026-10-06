/* releases: explicit callbacks and a bounded view, no session/run owner. */
function createReleasesController({ view, apiEndpoint, el, friendlyDate, getJson: requestJson, setMessage }) {
  const state = view;
  const lifetime = new UiLifetime();
  const getJson=(url,options)=>requestJson(url,lifetime.requestOptions(options));

  const setTimeout = lifetime.setTimeout.bind(lifetime);
  const clearTimeout = lifetime.clearTimeout.bind(lifetime);
  const requestAnimationFrame = lifetime.requestAnimationFrame.bind(lifetime);

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

async function checkReleases(options = {}){
  const viewRequest=lifetime.capture("checkReleases");
  const silent = Boolean(options.silent);
  state.releaseChecking = true;
  renderReleaseInfo();
  try {
    const data = await lifetime.result(getJson(apiEndpoint('service', 'releasesAvailable')), viewRequest);
    rememberReleasePayload(data || {}, 'stable');
    state.releaseChecked = true;
    renderReleaseInfo();
    if (!silent) setMessage('Обновления проверены', 'good');
  } catch (error) {
    if(!lifetime.isCurrent(viewRequest)) return;
    if (!silent) setMessage(`Ошибка проверки релизов: ${error.message}`, 'bad');
  } finally {
if(lifetime.isCurrent(viewRequest)){
    state.releaseChecking = false;
    renderReleaseInfo();
  }
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
  function dispose(){ lifetime.dispose();  }
function handleClickPart0(button){
if (button.dataset.action === 'check-releases') {
    checkReleases();
    return true;
  }
 return false;
}
  // Internal DOM dispatch and exported calls share one stale-action boundary.
  checkReleases = lifetime.action(checkReleases);
  return { handleClickPart0, renderReleaseInfo, releaseVersionLabel, checkReleases, releaseComparableVersion, normalizeServiceRelease, rememberReleasePayload, dispose };
}
