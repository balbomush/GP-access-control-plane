/* Pure existing selection/grouping algorithms, explicit query results only. */
function createCandidateSelectionModel({uniqueDomains}){
function candidateDomains(row){
  const seen = Array.isArray(row.seen) ? row.seen : [];
  return [...new Set(seen.map((item) => String(item.domain || '').trim()).filter(Boolean))];
}

function commonSeen(row){
  return Array.isArray(row.common_seen) ? row.common_seen : [];
}

function commonDomains(row){
  return [...new Set(commonSeen(row).flatMap((item) => Array.isArray(item.domains) ? item.domains : []).map((item) => String(item || '').trim()).filter(Boolean))];
}

function candidateAllDomains(row){
  return [...new Set([...candidateDomains(row), ...commonDomains(row)])];
}

function candidateResultModeLabel(mode){
  return {
    coverage: 'Максимум покрытия',
    minimal: 'Минимум стратегий',
    balance: 'Баланс'
  }[mode] || 'Баланс';
}

function rowTargetCoverage(row, targets){
  const domains = new Set(candidateAllDomains(row));
  return targets.filter((domain) => domains.has(domain));
}

function resultPickScore(row, uncoveredRequired, uncoveredDesired, mode){
  const requiredGain = rowTargetCoverage(row, [...uncoveredRequired]).length;
  const desiredGain = rowTargetCoverage(row, [...uncoveredDesired]).length;
  const complexity = strategyComplexity(row);
  if (mode === 'coverage') return (requiredGain + desiredGain) * 10000 + strategyDomainCoverage(row) * 10 - complexity;
  if (mode === 'minimal') return (requiredGain + desiredGain) * 10000 - complexity * 5;
  return requiredGain * 100000 + desiredGain * 1000 - complexity;
}

function buildCandidateResult(mode, targets, rows){
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
 return { candidateDomains, commonSeen, commonDomains, candidateAllDomains, candidateResultModeLabel, rowTargetCoverage, resultPickScore, buildCandidateResult, candidateResultText, normalizeStrategyArg, uniqueStrategyRows, uniqueStrategyArgs, strategyComplexity, strategyDomainCoverage, strategyDisplayFamilyKey, bestFamilyRow, strategyFamilyGroups };
}
