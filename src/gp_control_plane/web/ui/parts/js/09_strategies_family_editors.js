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
