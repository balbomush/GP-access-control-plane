  if (skipped.length) parts.push(`Будут пропущены без кандидатов: ${skipped.join(', ')}.`);
  if (selected.length < 2) parts.push('Нужно минимум два протестированных домена.');
  setText('common-domain-note', parts.join(' '));
  renderCommonDomainSuggestions();
}
function addCommonDomain(){
  const input = el('common-domain-add');
  const domain = String(input.value || '').trim();
  if (!domain) return;
  const tested = new Set(testedDomains());
  if (!tested.has(domain)) {
    showToast('По этому домену еще нет найденных стратегий', 'warn');
    return;
  }
  const current = parseDomains(el('common-domains').value);
  if (!current.includes(domain)) current.push(domain);
  el('common-domains').value = current.join('\n');
  input.value = '';
  hideCommonDomainSuggestions();
  updateEditorLineNumbers('common-domains');
  markDomainPresetCustom('common');
  state.candidateResultRequested = false;
  prepareCommonCandidateState();
  renderCandidatesOnly();
  if (selectedCommonDomains().length >= 2) refreshCandidates(true);
}
function candidateGroups(rows){
  const domainMap = new Map();
  rows.forEach((row) => {
    const domains = candidateDomains(row);
    (domains.length ? domains : ['unknown']).forEach((domain) => {
      if (!domainMap.has(domain)) domainMap.set(domain, new Map());
      const protocol = String(row.protocol || 'unknown');
      const protocolMap = domainMap.get(domain);
      if (!protocolMap.has(protocol)) protocolMap.set(protocol, []);
      protocolMap.get(protocol).push(row);
    });
  });
  return Array.from(domainMap.entries())
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([domain, protocolMap]) => ({
      domain,
      protocols: Array.from(protocolMap.entries())
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([protocol, protocolRows]) => ({ protocol, rows: protocolRows }))
    }));
}
function protocolGroups(rows){
  const protocolMap = new Map();
  rows.forEach((row) => {
    const protocol = String(row.protocol || 'unknown');
    if (!protocolMap.has(protocol)) protocolMap.set(protocol, []);
    protocolMap.get(protocol).push(row);
  });
  return Array.from(protocolMap.entries())
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([protocol, protocolRows]) => ({ protocol, rows: protocolRows }));
}
function normalizeStrategyArg(value){
  return String(value || '').trim().replace(/\s+/g, ' ');
}
function uniqueStrategyRows(rows){
  const seen = new Set();
  const result = [];
  rows.forEach((row) => {
    const raw = String(row.args || '').trim();
    const normalized = normalizeStrategyArg(raw);
    if (!normalized || seen.has(normalized)) return;
    seen.add(normalized);
    result.push(row);
  });
  return result;
}
function uniqueStrategyArgs(rows){
  return uniqueStrategyRows(rows).map((row) => String(row.args || '').trim());
}
function strategyComplexity(row){
  return String(row.args || '').split(/\s+/).filter(Boolean).length;
}
function strategyDomainCoverage(row){
  return candidateAllDomains(row).length;
}
function strategyDisplayFamilyKey(row){
  const protocol = String(row.protocol || 'unknown');
  const family = String(row.family || 'other');
  return `${protocol}:${family}`;
}
function bestFamilyRow(rows){
  return rows.slice().sort((a, b) => {
    const coverage = strategyDomainCoverage(b) - strategyDomainCoverage(a);
    if (coverage) return coverage;
    const familyRank = Number(a.family_rank || 900) - Number(b.family_rank || 900);
    if (familyRank) return familyRank;
    return strategyComplexity(a) - strategyComplexity(b);
  })[0] || {};
}
function strategyFamilyGroups(rows){
  const groups = new Map();
  uniqueStrategyRows(rows).forEach((row) => {
    const key = strategyDisplayFamilyKey(row);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
  });
  return Array.from(groups.entries()).map(([key, items]) => {
    const best = bestFamilyRow(items);
    return {
      key,
      family: String(best.family || 'other'),
      familyRank: Number(best.family_rank || 900),
      familyReason: String(best.family_reason || ''),
      best,
      rows: items
    };
  }).sort((a, b) => {
    const rank = a.familyRank - b.familyRank;
    if (rank) return rank;
    return a.family.localeCompare(b.family);
  });
}
function strategyListState(key, rows){
  const groups = strategyFamilyGroups(rows);
  const all = groups.flatMap((group) => group.rows.map((row) => String(row.args || '').trim()).filter(Boolean));
  const expanded = Boolean(state.expandedStrategyLists[key]);
  let remaining = expanded ? Number.MAX_SAFE_INTEGER : STRATEGY_LIST_LIMIT;
  const visibleGroups = [];
  groups.forEach((group) => {
    if (remaining <= 0) return;
    const rowsToShow = group.rows.slice(0, remaining);
    remaining -= rowsToShow.length;
    visibleGroups.push({ ...group, rows: rowsToShow, hidden: Math.max(0, group.rows.length - rowsToShow.length) });
  });
  const visibleCount = visibleGroups.reduce((sum, group) => sum + group.rows.length, 0);
  return { all, groups, visibleGroups, visibleCount, expanded, hidden: Math.max(0, all.length - visibleCount) };
}
function lineNumbers(count){
  return Array.from({ length: count }, (_item, index) => String(index + 1)).join('\n');
}
function updateEditorLineNumbers(id){
  const field = el(id);
  const gutter = document.querySelector(`[data-line-numbers-for="${id}"]`);
  if (!field || !gutter) return;
  const count = Math.max(1, String(field.value || '').split('\n').length);
  gutter.textContent = lineNumbers(count);
  gutter.scrollTop = field.scrollTop;
}
function updateAllEditorLineNumbers(){
  updateEditorLineNumbers('finder-domains');
  updateEditorLineNumbers('common-domains');
}
function strategyEditorScrollKey(field){
  return field?.dataset?.strategyCodeKey || field?.closest?.('[data-strategy-list]')?.dataset?.strategyList || '';
}
function rememberStrategyEditorScrolls(){
  const field = document.activeElement && document.activeElement.matches && document.activeElement.matches('.strategy-code')
    ? document.activeElement
    : null;
  const key = strategyEditorScrollKey(field);
  if (key) state.strategyEditorScrolls[key] = field.scrollTop;
}
function restoreStrategyEditorScrolls(){
  requestAnimationFrame(() => {
    document.querySelectorAll('.strategy-code').forEach((field) => {
      const key = strategyEditorScrollKey(field);
      if (!key || state.strategyEditorScrolls[key] == null) return;
      const scrollTop = Math.min(Number(state.strategyEditorScrolls[key] || 0), Math.max(0, field.scrollHeight - field.clientHeight));
      field.scrollTop = scrollTop;
      const gutter = field.previousElementSibling;
      if (gutter) gutter.scrollTop = scrollTop;
    });
  });
}
function strategyEditor(key, rows, title, options){
  const opts = options || {};
  const list = strategyListState(key, rows);
  const remoteMore = Boolean(opts.hasRemoteMore);
  const loadedTotal = Number(opts.loadedTotal || list.all.length);
  const remoteTotal = Number(opts.remoteTotal || loadedTotal);
  const remoteText = remoteMore ? ` Загружено ${loadedTotal}${remoteTotal ? ` из ${remoteTotal}` : ''}; оставшиеся догружаются по кнопке.` : '';
  const meta = `Показано ${list.visibleCount} из ${list.all.length} уникальных стратегий в ${list.groups.length} семействах. Дубликаты строк скрыты.${list.hidden ? ` Скрыто до раскрытия: ${list.hidden}.` : ''}${remoteText}`;
  const remoteAttr = remoteMore ? ' data-strategy-remote-more="true"' : '';
  const toggle = list.all.length > STRATEGY_LIST_LIMIT || remoteMore
    ? `<button class="secondary" data-strategy-list-toggle="${esc(key)}"${remoteAttr} type="button"${opts.loading ? ' disabled' : ''}>${strategyToggleLabel(list, opts)}</button>`
    : '';
  return `<div class="strategy-editor" data-strategy-list="${esc(key)}">
    <div class="strategy-editor-head">
      <div class="strategy-editor-title">
        <label>${esc(title)}</label>
        <div class="strategy-editor-meta">${esc(meta)}</div>
      </div>
      ${toggle}
    </div>
    <div class="strategy-family-list">${list.visibleGroups.map((group, index) => strategyFamilyGroup(key, group, index)).join('')}</div>
  </div>`;
}
function strategyFamilyGroup(parentKey, group, index){
  const lines = group.rows.map((row) => String(row.args || '').trim()).filter(Boolean);
  const lineCount = Math.max(lines.length, 1);
  const rowsAttr = Math.min(Math.max(lineCount, 4), 14);
  const best = group.best || {};
  const hidden = Number(group.hidden || 0);
  const reason = [
    group.familyReason ? `семейство: ${group.familyReason}` : '',
    hidden ? `скрыто вариантов: ${hidden}` : ''
  ].filter(Boolean).join(' · ');
  const key = `${parentKey}:family:${index}:${group.key}`;
  return `<details class="strategy-family" open>
    <summary class="strategy-family-summary">
      <div class="strategy-family-head">
        ${badge(group.family || 'other', '')}
        ${badge(`${group.rows.length + hidden} вариантов`, group.rows.length + hidden > 1 ? 'warn' : '')}
      </div>
      <div class="strategy-family-reason">${esc(reason || 'семейство определено по аргументам стратегии')}</div>
    </summary>
    <div class="code-editor">
      <pre class="line-numbers" aria-hidden="true">${esc(lineNumbers(lineCount))}</pre>
      <textarea class="strategy-code" data-strategy-code-key="${esc(key)}" readonly spellcheck="false" rows="${rowsAttr}">${esc(lines.join('\n'))}</textarea>
    </div>
  </details>`;
}
function strategyToggleLabel(list, options){
  const opts = options || {};
  if (opts.loading) return 'Загружается...';
  if (opts.hasRemoteMore) return opts.remoteLabel || 'Загрузить еще стратегии домена';
  if (list.expanded) return `Свернуть до ${STRATEGY_LIST_LIMIT}`;
  return `Показать все ${list.all.length}`;
}
function domainFromStrategyListKey(key){
  const text = String(key || '');
  if (!text.startsWith('domain:')) return '';
  const rest = text.slice('domain:'.length);
  const protocolSeparator = rest.lastIndexOf(':');
  return protocolSeparator >= 0 ? rest.slice(0, protocolSeparator) : rest;
}
function isCommonStrategyListKey(key){
  return String(key || '').startsWith('common:');
}
function renderRuns(){
  const rows = state.finderRuns.filter((row) => isDiscoveryRun(row));
  setText('finder-runs-count', String(state.finderRunTotal || rows.length));
  const visible = rows.slice().reverse();
  if (!visible.length) {
    el('finder-runs-table').innerHTML = '<div class="empty">Запусков поиска пока не было</div>';
    return;
  }
  el('finder-runs-table').innerHTML = `<div class="run-history">${visible.map(renderRunCard).join('')}</div>${runPager()}`;
}
function runPager(){
  return listLoadMore('load-more-runs', state.finderRunHasMore, state.finderRunsLoading);
}
function renderRunCard(row){
  const count = runCandidateCount(row);
  const status = row.status || '-';
  const domainKey = runDomainKey(row);
  return `<article class="run-card ${esc(runCardClass(row))}">
    <div class="run-card-main">
      ${runField('Время', friendlyDate(row.timestamp))}
      ${runField('Движок', String(row.discovery_engine || '').startsWith('blockchecks') ? 'blockcheckS' : 'blockcheck2')}
      ${runField('Режим', runMode(row))}
      <div class="run-field">
        <div class="run-field-label">Статус</div>
        <div class="run-field-value run-status">${badge(runStatusLabel(status), statusTone[status] || '')}</div>
      </div>
      ${runField('Этап', runPhaseText(row))}
      <div class="run-field">
        <div class="run-field-label">Стратегии</div>
        <div class="run-field-value">${badge(String(count), count > 0 ? 'good' : '')}</div>
      </div>
      ${runField('Попытки', runProgressText(row))}
      ${runField('Настройки', runSettingsText(row))}
      ${runField('Диагностика', runDiagnosticsSummary(row))}
      ${runField('Итог', runSummary(row))}
    </div>
    ${runDomains(row, domainKey)}
    ${runDiagnostics(row)}
    <div class="run-card-actions">
      <button class="secondary" data-run-repeat="${esc(domainKey)}" type="button">Повторить с этими настройками</button>
    </div>
  </article>`;
}
function runDomainKey(row){
  return String(row.run_id || `${row.timestamp || ''}:${(row.domains || []).join('|')}`);
}
function runCardClass(row){
  const status = String(row.status || 'unknown').toLowerCase().replace(/[^a-z0-9_-]/g, '') || 'unknown';
  const kind = row.kind === 'multi-domain-discovery' ? 'multi' : 'standard';
  return `run-card-status-${status} run-card-kind-${kind}`;
}
function runField(label, value){
  return `<div class="run-field">
    <div class="run-field-label">${esc(label)}</div>
    <div class="run-field-value">${esc(value || '-')}</div>
  </div>`;
}
function runStatusLabel(status){
  const labels = {
    success: 'Завершено',
    failed: 'Ошибка',
    error: 'Ошибка',
    running: 'Идет подбор',
    queued: 'Запускается',
    stopping: 'Останавливается',
    stopped: 'Остановлено',
    timeout: 'Таймаут',
    idle: 'Свободно'
  };
  return labels[status] || status || '-';
}
function runPhaseText(row){
  const progress = row.progress || {};
  return progress.phase_label || phaseLabel(row.phase || progress.phase || '');
}
function phaseLabel(phase){
  const labels = {
    checking_vpn: 'проверка VPN',
    checking_zapret: 'проверка zapret',
    checking_domain: 'проверка доступности домена',
    strategy_discovery: 'подбор стратегий',
    strategy_summary: 'суммаризация стратегий',
    saving_results: 'сохранение результатов',
    complete: 'завершено'
  };
  return labels[phase] || phase || '-';
}
function runDomains(row, domainKey){
  const domains = Array.isArray(row.domains) ? row.domains.map((domain) => String(domain || '').trim()).filter(Boolean) : [];
  const preview = domains.length ? domains.join(', ') : '-';
  const count = domains.length ? `${domains.length} доменов` : 'нет доменов';
  const expandable = domains.length > 1;
  const open = expandable && Boolean(state.openRunDomains[domainKey]);
  return `<details class="run-domains ${expandable ? 'run-domains-expandable' : ''}" data-run-domains="${esc(domainKey)}"${open ? ' open' : ''}>
    <summary>
      <span class="run-field-label">Домены</span>
      <span class="run-domains-preview" title="${esc(preview)}">${esc(preview)}</span>
      <span class="run-domains-count">${esc(count)}</span>
      <span class="run-domains-arrow" aria-hidden="true"></span>
    </summary>
    <div class="run-domain-list">${runDomainChips(domains)}</div>
  </details>`;
}
function runDomainChips(domains){
  if (!domains.length) return '<span class="run-domain-chip">-</span>';
  return domains.map((domain) => `<span class="run-domain-chip">${esc(domain)}</span>`).join('');
}
function diagnosticShortLabel(status, fallback){
  const labels = {
    invalid_domain: 'некорректная строка',
    dns_error: 'DNS не дал адрес',
    tls_sni_problem: 'TLS/SNI не совпал',
    ssl_connect_error: 'TLS-соединение сорвалось',
    quic_connect_error: 'QUIC/connect не установился',
    timeout: 'проверка не дождалась ответа',
    needs_discovery: 'нужен подбор стратегии',
    curl_error: 'проверочный запрос вернул ошибку',
    direct_available: 'прямой доступ есть'
  };
  return labels[status] || fallback || status || '-';
}
function diagnosticExplanation(item, row){
  const status = item.status || '';
  const found = runCandidateCount(row) > 0;
  const explanations = {
    invalid_domain: 'Строка не похожа на домен, поэтому проверка стратегий не может проверить ее как сайт.',
    dns_error: 'DNS не вернул адрес. Это проблема разрешения имени до проверки стратегии.',
    tls_sni_problem: 'Проверочный запрос получил сертификат не для этого домена. Такое бывает при SNI/TLS-проверках, DPI или особенностях service-доменов.',
    ssl_connect_error: 'TLS-соединение оборвалось до нормального ответа сервера.',
    quic_connect_error: 'QUIC или connect-проверка не смогла установить соединение.',
    timeout: found
      ? 'Часть проверок не успела ответить за таймаут. Это не отменяет найденные стратегии: успешные проверки уже сохранены отдельно.'
      : 'Домен не ответил за заданный таймаут. Увеличьте таймаут или проверьте доступность домена отдельно.',
    needs_discovery: 'Для домена не найден прямой рабочий вариант, нужен подбор стратегии.',
    curl_error: 'Проверочный запрос вернул ошибку, которую нужно смотреть в технических деталях.',
    direct_available: 'Домен открывался напрямую, стратегия для него может быть не нужна.'
  };
  return explanations[status] || item.message || 'Подробности доступны в технических деталях.';
}
function curlCodeLabel(code){
  const labels = {
    '3': 'некорректная строка',
    '6': 'DNS не дал адрес',
    '7': 'соединение не установилось',
    '28': 'таймаут',
    '35': 'TLS/SSL сбой',
    '60': 'TLS/SNI не совпал'
  };
  return labels[String(code)] || 'проверочный запрос вернул ошибку';
}
function curlCodeDetails(codes){
  if (!codes || !Object.keys(codes).length) return '';
  return Object.entries(codes)
    .map(([code, count]) => `curl ${code}: ${curlCodeLabel(code)}, ${count} раз`)
    .join('; ');
}
function runDiagnosticsSummary(row){
  const skipped = Number(row.domain_skipped_count || 0);
  const dominant = row.dominant_failure || {};
  if (dominant.status || dominant.label) return `${diagnosticShortLabel(dominant.status, dominant.label)}: ${dominant.count || 0}`;
  if (skipped) return `пропущено строк: ${skipped}`;
  const diagnostics = Array.isArray(row.domain_diagnostics) ? row.domain_diagnostics : [];
  if (diagnostics.length) return diagnostics.map((item) => diagnosticShortLabel(item.status, item.label)).filter(Boolean).slice(0, 2).join(', ');
  return '-';
}
function runDiagnostics(row){
  const skipped = Array.isArray(row.domain_skipped) ? row.domain_skipped : [];
  const diagnostics = Array.isArray(row.domain_diagnostics) ? row.domain_diagnostics : [];
  const curlSummary = row.curl_diagnostics_summary || {};
  if (!skipped.length && !diagnostics.length && !Object.keys(curlSummary).length) return '';
  const skippedItems = skipped.slice(0, 20).map((item) => diagnosticTableRow({
    type: 'строка',
    target: item.raw || '-',
    details: item.message || 'Строка пропущена до запуска проверки.',
    tech: item.status || '-',
    tone: 'bad'
  })).join('');
  const domainItems = diagnostics.map((item) => {
    const tone = ['dns_error', 'invalid_domain', 'tls_sni_problem'].includes(item.status) ? 'bad' : 'warn';
    return diagnosticTableRow({
      type: 'домен',
      target: item.domain || '-',
      details: diagnosticExplanation(item, row),
      tech: [diagnosticShortLabel(item.status, item.label), curlCodeDetails(item.codes)].filter(Boolean).join('; '),
      tone
    });
  }).join('');
  const codeItems = Object.entries(curlSummary).map(([code, count]) => diagnosticTableRow({
    type: 'сводка',
    target: 'все проверки',
    details: `Всего таких ошибок в запуске: ${count}.`,
    tech: `curl ${code}: ${count} раз`,
    tone: 'warn'
  })).join('');
  return `<details class="run-diagnostics">
    <summary>Диагностика доменов</summary>
    <div class="run-diagnostic-table-wrap">
      <table class="run-diagnostic-table">
        <thead>
          <tr>
            <th>Тип</th>
            <th>Домен / строка</th>
            <th>Пояснение</th>
          </tr>
        </thead>
        <tbody>${skippedItems}${domainItems}${codeItems}</tbody>
      </table>
    </div>
    <div class="run-diagnostic-note">Если стратегия найдена, отдельные ошибки в диагностике означают провал части проверок, а не отмену сохраненных успешных стратегий.</div>
  </details>`;
}
function diagnosticTableRow(item){
  const tech = item.tech
    ? `<details class="run-diagnostic-tech"><summary>технически</summary><div>${esc(item.tech)}</div></details>`
    : '';
  return `<tr>
    <td>${esc(item.type || '-')}</td>
    <td class="run-diagnostic-target">${esc(item.target || '-')}</td>
    <td><div class="run-diagnostic-details">${esc(item.details || '-')}</div>${tech}</td>
  </tr>`;
}
function isDiscoveryRun(row){
  return row.kind === 'standard-discovery' || row.kind === 'multi-domain-discovery';
}
function runMode(row){
  return row.kind === 'multi-domain-discovery' ? 'все домены на одной стратегии' : 'обычный';
}
function runSummary(row){
  const count = runCandidateCount(row);
  const phase = row.phase || (row.progress || {}).phase || '';
  if (row.status === 'stopping') return 'останавливается';
  if (phase === 'saving_results' && row.status === 'failed') return `ошибка сохранения, код: ${row.returncode ?? '-'}`;
  if (phase === 'saving_results') return 'сохраняются результаты';
  if (row.status === 'running') return 'идет поиск';
  if (row.status === 'timeout') return `остановлено по лимиту, найдено: ${count}`;
  if (row.status === 'stopped') return count > 0 ? `остановлено, сохранено: ${count}` : 'остановлено, кандидатов нет';
  if (row.status === 'success') return count > 0 ? `найдено: ${count}` : 'завершено, кандидатов нет';
  if (row.status === 'failed') return `ошибка, код: ${row.returncode ?? '-'}`;
  return count > 0 ? `найдено: ${count}` : '-';
}
function runCandidateCount(row){
  return Number(row.candidate_count || 0) + Number(row.common_candidate_count || 0);
}
function runSettingsText(row){
