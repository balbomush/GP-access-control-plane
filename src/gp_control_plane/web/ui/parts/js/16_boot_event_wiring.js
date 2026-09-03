document.addEventListener('submit', (event) => {
  if (event.target && event.target.id === 'login-form') {
    submitLogin(event);
    return;
  }
  if (event.target && event.target.id === 'change-password-form') {
    event.preventDefault();
    changePassword();
  }
});document.addEventListener('click', (event) => {
  const domainSummary = event.target.closest('details.domain-group[data-domain] > summary');
  if (domainSummary) {
    event.preventDefault();
    const details = domainSummary.parentElement;
    const domain = details.dataset.domain;
    const nextOpen = !Boolean(state.openCandidateDomains[domain]);
    state.openCandidateDomains[domain] = nextOpen;
    const cachedDomain = state.domainStrategies[domain] || {};
    if (nextOpen && (!cachedDomain.loaded || !candidateCacheValid(cachedDomain))) {
      state.domainStrategies[domain] = { candidates: [], total: 0, hasMore: false, loaded: false, loading: true };
      renderCandidates();
      refreshDomainStrategies(domain, true);
    } else {
      renderCandidates();
    }
    return;
  }
const button = event.target.closest('button');
  if (!button) return;
  const action = button.dataset.action || '';
  if (button.dataset.backupDownload) {
    const snapshotId = button.dataset.backupDownload;
    downloadBackup(backupDownloadUrl(snapshotId), snapshotId);
    return;
  }
  if (action === 'logout') {
    logout();
    return;
  }
  const protectedMutation = MUTATING_ACTIONS.has(action) || Boolean(button.dataset.backupRestore) || Boolean(button.dataset.backupDelete) || Boolean(button.dataset.cleanInstallVaultRestore);
  if (protectedMutation && !requireNoActiveRun()) return;
  if (button.dataset.commonDomainSuggestion) {
    chooseCommonDomainSuggestion(button.dataset.commonDomainSuggestion);
    return;
  }
  if (button.dataset.runRepeat) {
    repeatRun(button.dataset.runRepeat);
    return;
  }
  if (button.dataset.tab) setActiveTab(button.dataset.tab);
  if (button.dataset.candidateView) {
    setCandidateView(button.dataset.candidateView);
    return;
  }
  if (button.dataset.candidateResultMode) {
    state.candidateResultMode = button.dataset.candidateResultMode;
    renderCandidateResult();
    return;
  }
  if (button.dataset.action === 'open-log') {
    setActiveTab('terminal');
    const raw = document.querySelector('.raw-log-panel');
    if (raw) raw.open = true;
    return;
  }
  if (button.dataset.action === 'open-candidates') {
    setActiveTab('candidates');
    return;
  }
  if (button.dataset.action === 'repeat-last-run') {
    const row = latestRun();
    if (row) repeatRun(runDomainKey(row));
    else setMessage('В истории пока нет запуска для повтора', 'warn');
    return;
  }
  if (button.dataset.action === 'copy-diagnostics') {
    copyDiagnostics();
    return;
  }
  if (button.dataset.action === 'build-candidate-result') {
    buildCandidateResultNow();
    return;
  }
  if (button.dataset.action === 'copy-candidate-result') {
    copyCandidateResult();
    return;
  }
  if (button.dataset.action === 'export-nfconf') {
    exportNfconfNow();
    return;
  }
  if (button.dataset.action === 'export-candidate-result') {
    exportCandidateResult();
    return;
  }
  if (button.dataset.action === 'use-candidate-result-domains') {
    useCandidateResultDomains();
    return;
  }
  if (button.dataset.action === 'open-candidate-result') {
    openCandidateResultDetails();
    return;
  }
  if (button.dataset.action === 'refresh') {
    invalidateCandidateCaches();
    refresh();
    if (state.activeTab === 'candidates') {
      if (state.candidateView === 'domain') {
        refreshDomainIndex();
      } else {
        refreshCandidates(true);
      }
    }
  }
  if (button.dataset.action === 'refresh-backups') {
    refreshBackups();
    return;
  }
  if (button.dataset.action === 'refresh-clean-install-vaults') {
    refreshCleanInstallVaults();
    return;
  }
  if (button.dataset.action === 'create-backup') {
    createBackup();
    return;
  }
  if (button.dataset.action === 'create-clean-install-vault') {
    createCleanInstallVault();
    return;
  }
  if (button.dataset.action === 'save-settings') {
    saveSettings();
    return;
  }
  if (button.dataset.action === 'check-releases') {
    checkReleases();
    return;
  }
  if (button.dataset.action === 'v2fly-load-categories') {
    loadV2flyCategories(true);
    return;
  }
  if (button.dataset.action === 'v2fly-select-category') {
    const category = button.dataset.category || '';
    const input = el('v2fly-category-search');
    if (input) input.value = category;
    state.v2flyPreview = null;
    clearV2flyDomains();
    suggestV2flyPresetName();
    renderV2flyCategoryCatalog();
    renderV2flyPreview();
    return;
  }
  if (button.dataset.action === 'v2fly-preview') {
    previewV2flyPreset();
    return;
  }
  if (button.dataset.action === 'v2fly-import') {
    importV2flyPreset();
    return;
  }
  if (button.dataset.action === 'preset-editor-save') {
    savePresetEditor();
    return;
  }
  if (button.dataset.action === 'preset-editor-delete') {
    deletePresetEditor();
    return;
  }
  if (button.dataset.action === 'preset-editor-export') {
    exportPresetEditor();
    return;
  }
  if (button.dataset.action === 'preset-new-save') {
    savePresetNew();
    return;
  }
  if (button.dataset.backupRestore) {
    restoreBackup(button.dataset.backupRestore);
    return;
  }
  if (button.dataset.cleanInstallVaultRestore) {
    restoreCleanInstallVault(button.dataset.cleanInstallVaultRestore);
    return;
  }
  if (button.dataset.backupDelete) {
    deleteBackup(button.dataset.backupDelete);
    return;
  }
  if (button.dataset.action === 'upload-backup') {
    uploadBackup();
    return;
  }
  if (button.dataset.action === 'load-more-candidates') {
    refreshCandidates(false);
    return;
  }
  if (button.dataset.action === 'load-more-candidate-domains') {
    refreshDomainIndex(false);
    return;
  }
  if (button.dataset.action === 'load-more-runs') {
    refreshRuns(false);
    return;
  }
  if (button.dataset.fill) fillDomains(button.dataset.fill);
  if (button.dataset.presetSave) {
    savePreset(button.dataset.presetSave);
    return;
  }
  if (button.dataset.presetDelete) {
    deletePreset(button.dataset.presetDelete);
    return;
  }
  if (button.dataset.action === 'add-common-domain') {
    addCommonDomain();
    return;
  }
  if (button.dataset.strategyListToggle) {
    const key = button.dataset.strategyListToggle;
    const domain = domainFromStrategyListKey(key);
    const common = isCommonStrategyListKey(key);
    const remoteMore = button.dataset.strategyRemoteMore === 'true';
    if (remoteMore && common && state.candidateHasMore) {
      state.expandedStrategyLists[key] = true;
      loadMoreCommonStrategies();
      return;
    }
    if (remoteMore && domain && (state.domainStrategies[domain] || {}).hasMore) {
      state.expandedStrategyLists[key] = true;
      loadMoreDomainStrategies(domain);
      return;
    }
    const currentlyExpanded = Boolean(state.expandedStrategyLists[key]);
    state.expandedStrategyLists[key] = !currentlyExpanded;
    renderCandidates();
    return;
  }
  if (button.dataset.action === 'run-selected-discovery') startSelectedDiscovery();
  if (button.dataset.action === 'stop-current') stopCurrentJob();
});
document.addEventListener('input', (event) => {
  if (event.target && ['curl-parallelism', 'enable-ipv6'].includes(event.target.id)) {
    state.settingsTouched = true;
  }
  if (event.target && RUN_TIMEOUT_CONTROL_IDS.has(event.target.id)) {
    state.settingsTouched = true;
  }
  if (event.target && String(event.target.id || '').startsWith('settings-')) {
    state.settingsTouched = true;
  }
  if (event.target && String(event.target.id || '').startsWith('v2fly-')) {
    if (event.target.id === 'v2fly-category-search') {
      clearV2flyDomains();
      suggestV2flyPresetName();
      renderV2flyCategoryCatalog();
    }
    if (event.target.id === 'v2fly-domains') updateEditorLineNumbers('v2fly-domains');
    state.v2flyPreview = null;
    renderV2flyPreview();
  }
  if (event.target && event.target.id === 'preset-editor-domains') {
    updateEditorLineNumbers('preset-editor-domains');
    renderPresetEditorPreview(null);
  }
  if (event.target && event.target.id === 'preset-new-domains') {
    updateEditorLineNumbers('preset-new-domains');
    renderPresetNewPreview(null);
  }
  if (event.target && event.target.id === 'finder-domains') {
    updateEditorLineNumbers('finder-domains');
    state.domainsTouched = true;
    markDomainPresetCustom('finder');
    if (state.candidateView === 'common') scheduleCandidateRefresh();
  }
  if (event.target && event.target.id === 'common-domains') {
    updateEditorLineNumbers('common-domains');
    markDomainPresetCustom('common');
    scheduleCandidateRefresh();
    renderCommonDomainSuggestions();
  }
  if (event.target && DISCOVERY_PROFILE_CONTROL_IDS.has(event.target.id)) {
    markDiscoveryProfileCustom();
  }
  if (event.target && event.target.id === 'common-domain-add') {
    renderCommonDomainSuggestions();
  }
  if (isRunLaunchSummaryControl(event.target)) {
    renderRunLaunchSummary();
  }
});
document.addEventListener('scroll', (event) => {
  if (event.target && event.target.matches && event.target.matches('.strategy-code, .line-numbered-textarea')) {
    const gutter = event.target.previousElementSibling;
    if (gutter) gutter.scrollTop = event.target.scrollTop;
    if (event.target.matches('.strategy-code')) {
      const key = strategyEditorScrollKey(event.target);
      if (key) state.strategyEditorScrolls[key] = event.target.scrollTop;
    }
  }
}, true);
document.addEventListener('change', (event) => {
  if (event.target && ['curl-parallelism', 'enable-ipv6'].includes(event.target.id)) {
    state.settingsTouched = true;
  }
  if (event.target && RUN_TIMEOUT_CONTROL_IDS.has(event.target.id)) {
    state.settingsTouched = true;
  }
  if (event.target && String(event.target.id || '').startsWith('settings-')) {
    state.settingsTouched = true;
  }
  if (event.target && event.target.id === 'finder-discovery-engine') {
    const settingsEngine = el('settings-discovery-engine');
    if (settingsEngine) settingsEngine.value = event.target.value;
    syncEngineUi();
    renderRunLaunchSummary();
  }
  if (event.target && event.target.id === 'settings-discovery-engine') {
    const finderEngine = el('finder-discovery-engine');
    if (finderEngine) finderEngine.value = event.target.value;
    syncEngineUi();
    renderRunLaunchSummary();
  }
  if (event.target && String(event.target.id || '').startsWith('v2fly-')) {
    if (event.target.id === 'v2fly-category-search') {
      clearV2flyDomains();
      suggestV2flyPresetName();
      renderV2flyCategoryCatalog();
    }
    if (event.target.id === 'v2fly-domains') updateEditorLineNumbers('v2fly-domains');
    state.v2flyPreview = null;
    renderV2flyPreview();
  }
  if (event.target && event.target.id === 'limit-time-enabled') {
    syncTimeLimitUi();
    markDiscoveryProfileCustom();
  }
  if (event.target && (event.target.id === 'finder-preset-select' || event.target.id === 'common-preset-select')) {
    const target = event.target.id.startsWith('finder') ? 'finder' : 'common';
    const value = event.target.value || '';
    const nameInput = el(`${target}-preset-name`);
    if (nameInput) nameInput.value = value === CUSTOM_SELECT_VALUE ? 'custom' : (value.startsWith('custom:') ? value.slice('custom:'.length) : '');
    if (value !== CUSTOM_SELECT_VALUE) usePreset(target);
  }
  if (event.target && event.target.id === 'preset-manager-name') {
    state.presetManager.name = event.target.value || '';
    renderPresetManager();
    loadPresetEditorFromSelection({ silent: true });
  }
  if (event.target && event.target.id === 'discovery-profile-select') {
    const profile = (state.discoveryProfiles || {})[event.target.value];
    useDiscoveryProfile(profile);
  }
  if (event.target && event.target.name === 'run-mode') {
    renderRunModeNote();
  }
  if (event.target && DISCOVERY_PROFILE_CONTROL_IDS.has(event.target.id)) {
    markDiscoveryProfileCustom();
  }
  if (isRunLaunchSummaryControl(event.target)) {
    renderRunLaunchSummary();
  }
});
document.addEventListener('keydown', (event) => {
  if (handleTabControlKeydown(event)) return;
  if (event.target && event.target.id === 'common-domain-add' && event.key === 'Enter') {
    event.preventDefault();
    addCommonDomain();
  }
  if (event.target && event.target.id === 'common-domain-add' && event.key === 'Escape') {
    hideCommonDomainSuggestions();
  }
});
document.addEventListener('focusin', (event) => {
  if (event.target && event.target.id === 'common-domain-add') {
    renderCommonDomainSuggestions();
  }
});
document.addEventListener('focusout', (event) => {
  if (event.target && event.target.id === 'common-domain-add') {
    setTimeout(hideCommonDomainSuggestions, 120);
  }
});
document.addEventListener('toggle', (event) => {
  const details = event.target;
  if (!details || !details.matches) return;
  if (details.matches('details.domain-group[data-common-protocol]')) {
    if (state.openCommonProtocols[details.dataset.commonProtocol] !== details.open) {
      state.openCommonProtocols[details.dataset.commonProtocol] = details.open;
      renderCandidates();
    }
  }
  if (details.matches('details.run-domains[data-run-domains]')) {
    state.openRunDomains[details.dataset.runDomains] = details.open;
  }
}, true);
if (authToken()) startAuthenticatedUi();
else showLogin();
