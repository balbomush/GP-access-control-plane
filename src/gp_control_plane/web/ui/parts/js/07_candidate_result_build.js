function buildCandidateResult(mode){
  const targets = candidateResultTargets();
  const rows = commonCandidateResultRows();
  const uncoveredRequired = new Set(targets.required);
  const uncoveredDesired = new Set(targets.desired);
  const selected = [];
  const remaining = rows.slice();
  while ((uncoveredRequired.size || uncoveredDesired.size) && remaining.length) {
    remaining.sort((a, b) => resultPickScore(b, uncoveredRequired, uncoveredDesired, mode) - resultPickScore(a, uncoveredRequired, uncoveredDesired, mode));
    const best = remaining.shift();
    if (!best) break;
    const requiredHit = rowTargetCoverage(best, [...uncoveredRequired]);
    const desiredHit = rowTargetCoverage(best, [...uncoveredDesired]);
    if (!requiredHit.length && !desiredHit.length) continue;
    selected.push({ row: best, requiredHit, desiredHit });
    requiredHit.forEach((domain) => uncoveredRequired.delete(domain));
    desiredHit.forEach((domain) => uncoveredDesired.delete(domain));
    if (mode === 'minimal' && !uncoveredRequired.size && !uncoveredDesired.size) break;
  }
  const coveredRequired = targets.required.filter((domain) => !uncoveredRequired.has(domain));
  const coveredDesired = targets.desired.filter((domain) => !uncoveredDesired.has(domain));
  const modeLabel = candidateResultModeLabel(mode);
  const targetCount = targets.required.length + targets.desired.length;
  const reason = !targetCount
    ? 'Нет обязательных или желательных доменов для расчета итогового набора.'
    : selected.length
    ? `${modeLabel}: покрыто ${coveredRequired.length}/${targets.required.length} обязательных и ${coveredDesired.length}/${targets.desired.length} желательных доменов по загруженным стратегиям.`
    : 'Нет загруженных стратегий, которые покрывают выбранные домены.';
  return {
    required_coverage: { covered: coveredRequired.length, total: targets.required.length },
    desired_coverage: { covered: coveredDesired.length, total: targets.desired.length },
    uncovered_required: [...uncoveredRequired],
    uncovered_desired: [...uncoveredDesired],
    strategy_set: selected.map((item) => ({
      args: String(item.row.args || '').trim(),
      protocol: String(item.row.protocol || '-'),
      domains: uniqueDomains([...item.requiredHit, ...item.desiredHit])
    })),
    reason,
    mode: modeLabel,
    loaded_rows: rows.length,
    targets
  };
}
function candidateResultText(result){
  const lines = (result.strategy_set || []).map((item) => item.args).filter(Boolean);
  return lines.join('\n');
}
function resetCandidateResult(){
  state.candidateResultRequested = false;
  renderCandidateResult();
}
async function buildCandidateResultNow(){
  state.candidateResultRequested = true;
  if (state.candidateView !== 'common') state.candidateView = 'common';
  const selectedDomains = selectedCommonDomains();
  const loaded = prepareCommonCandidateState();
  renderCandidatesOnly();
  if (selectedDomains.length >= 2 && !loaded) {
    await refreshCandidates(true);
  }
}
function renderCandidateResult(){
  const panel = document.querySelector('.candidate-result-panel');
  const body = el('candidate-result-body');
  const source = el('candidate-result-source');
  if (panel) panel.hidden = state.candidateView !== 'common';
  if (!body) return;
  const mode = state.candidateResultMode || 'balance';
  document.querySelectorAll('[data-candidate-result-mode]').forEach((button) => {
    const active = button.dataset.candidateResultMode === mode;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', active ? 'true' : 'false');
    button.tabIndex = active ? 0 : -1;
  });
  body.setAttribute('aria-labelledby', `candidate-result-mode-${mode}`);
  if (state.candidateView !== 'common') return;
  if (!state.candidateResultRequested) {
    if (source) source.textContent = 'Выберите домены для пересечения и соберите итоговый набор.';
    body.innerHTML = '<div class="empty">Нажмите «Собрать итоговый набор» после выбора доменов.</div>';
    return;
  }
  const selectedDomains = selectedCommonDomains();
  if (selectedDomains.length < 2) {
    if (source) source.textContent = 'Для итогового набора нужны минимум два протестированных домена.';
    body.innerHTML = '<div class="empty">Выберите минимум два домена в пресете доменов для пересечения.</div>';
    return;
  }
  const result = buildCandidateResult(mode);
  const rows = Number(result.loaded_rows || 0);
  const requiredTotal = Number(result.required_coverage.total || 0);
  const desiredTotal = Number(result.desired_coverage.total || 0);
  if (source) {
    source.textContent = `Расчет по загруженным общим стратегиям: ${rows}. Обязательные: ${requiredTotal}. Желательные: ${desiredTotal}.`;
  }
  if (!rows) {
    body.innerHTML = '<div class="empty">Для выбранного пересечения пока нет загруженных общих стратегий.</div>';
    return;
  }
  const strategies = result.strategy_set || [];
  const strategiesHtml = strategies.length
    ? `<div class="candidate-result-strategies">${strategies.map((item) => `<div class="candidate-result-strategy">
        <code>${esc(item.args || '-')}</code>
        <div class="candidate-result-domains">${esc(item.protocol || '-')} · ${esc((item.domains || []).join(', ') || '-')}</div>
      </div>`).join('')}</div>`
    : '<div class="empty">По загруженным стратегиям нет покрытия выбранных доменов.</div>';
  body.innerHTML = `<div class="candidate-result-grid">
    <div class="candidate-result-cell">
      <div class="candidate-result-label">mode</div>
      <div class="candidate-result-value">${esc(result.mode)}</div>
    </div>
    <div class="candidate-result-cell">
      <div class="candidate-result-label">required_coverage</div>
      <div class="candidate-result-value">${result.required_coverage.covered} / ${result.required_coverage.total}</div>
    </div>
    <div class="candidate-result-cell">
      <div class="candidate-result-label">desired_coverage</div>
      <div class="candidate-result-value">${result.desired_coverage.covered} / ${result.desired_coverage.total}</div>
    </div>
    <div class="candidate-result-cell">
      <div class="candidate-result-label">strategy_set</div>
      <div class="candidate-result-value">${strategies.length}</div>
    </div>
  </div>
  <div class="helper-text">${esc(result.reason)}</div>
  <details class="candidate-result-details" open>
    <summary>Детали итогового набора</summary>
    <div class="helper-text">uncovered_required: ${esc(result.uncovered_required.join(', ') || '-')}</div>
    <div class="helper-text">uncovered_desired: ${esc(result.uncovered_desired.join(', ') || '-')}</div>
    ${strategiesHtml}
  </details>
  <div class="candidate-result-actions">
    <button class="secondary" data-action="copy-candidate-result" type="button"${strategies.length ? '' : ' disabled'}>Скопировать для zapret2</button>
    <button class="secondary" data-action="export-nfconf" type="button">Экспорт nfqws2 (bc-nfconf)</button>
    <button class="secondary" data-action="export-candidate-result" type="button"${strategies.length ? '' : ' disabled'}>Экспорт TXT</button>
    <button class="secondary" data-action="use-candidate-result-domains" type="button">Повторить подбор</button>
    <button class="secondary" data-action="open-candidate-result" type="button">Открыть детали</button>
  </div>`;
  syncEngineUi();
}
async function copyCandidateResult(){
  const result = buildCandidateResult(state.candidateResultMode || 'balance');
  const text = candidateResultText(result);
  if (!text) {
    setMessage('В итоговом наборе нет стратегий для копирования', 'warn');
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
    setMessage('Итоговый набор скопирован', 'good');
  } catch (error) {
    setMessage(`Не удалось скопировать итоговый набор: ${error.message}`, 'bad');
  }
}
function exportCandidateResult(){
  const result = buildCandidateResult(state.candidateResultMode || 'balance');
  const text = candidateResultText(result);
  if (!text) {
    setMessage('В итоговом наборе нет стратегий для экспорта', 'warn');
    return;
  }
  const blob = new Blob([text + '\n'], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = 'gp-candidate-result.txt';
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
function useCandidateResultDomains(){
  const result = buildCandidateResult(state.candidateResultMode || 'balance');
  const domains = uniqueDomains([...(result.targets.required || []), ...(result.targets.desired || [])]);
  if (!domains.length) {
    setMessage('Нет доменов для повторного запуска', 'warn');
    return;
  }
  el('finder-domains').value = domains.join('\n');
  state.domainsTouched = true;
  markDomainPresetCustom('finder');
  updateEditorLineNumbers('finder-domains');
  renderRunLaunchSummary();
  setActiveTab('finder');
  setMessage('Домены итогового набора перенесены в форму запуска. Старт выполните вручную.', 'good');
}
function openCandidateResultDetails(){
  const details = document.querySelector('.candidate-result-details');
  if (details) details.open = true;
}
