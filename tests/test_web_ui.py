from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.config import AppConfig, OutputConfig
from gp_control_plane.state import read_state, write_state
from gp_control_plane.web import app as web_app
from gp_control_plane.web.app import index_html, serve


class WebUiTests(unittest.TestCase):
    def test_index_html_is_focused_on_strategy_finder_only(self) -> None:
        html = index_html()

        self.assertIn("Подбор стратегий zapret2", html)
        self.assertIn("Raspberry Pi · проверка стратегий · live-лог", html)
        self.assertNotIn("Raspberry Pi · blockcheck2 · live-лог", html)
        self.assertIn('<div class="metric-label">Система</div>', html)
        self.assertIn('<div class="metric-label">Подбор</div>', html)
        self.assertNotIn('<div class="metric-label">zapret2</div>', html)
        self.assertNotIn('<div class="metric-label">Задание</div>', html)
        self.assertIn("nextActionStatus", html)
        self.assertIn("Можно запускать", html)
        self.assertIn("Требуется настройка", html)
        self.assertIn("Есть ошибка", html)
        self.assertIn("Запуск поиска", html)
        self.assertIn("Найденные стратегии", html)
        self.assertIn("История запусков", html)
        self.assertIn("data-tab=\"history\"", html)
        self.assertIn("data-tab-page=\"history\"", html)
        self.assertIn("data-tab=\"candidates\"", html)
        self.assertIn("data-tab=\"terminal\"", html)
        self.assertNotIn("data-tab=\"backups\"", html)
        self.assertNotIn("data-tab-page=\"backups\"", html)
        self.assertIn("data-tab=\"settings\"", html)
        self.assertIn("data-tab-page=\"settings\"", html)
        self.assertIn("Бекапы", html)
        self.assertIn("settings-backups-panel", html)
        self.assertIn("Создать бекап сейчас", html)
        self.assertIn("backups-table", html)
        self.assertIn("refreshBackups", html)
        self.assertIn("/api/backups", html)
        self.assertIn("/api/backups/create", html)
        self.assertIn("/api/backups/restore", html)
        self.assertIn("/api/backups/delete", html)
        self.assertIn("/api/backups/download", html)
        self.assertIn("backup-upload-panel", html)
        self.assertIn("backup-downloads", html)
        self.assertIn("backup-archive-link", html)
        self.assertIn("backup-file-links", html)
        self.assertIn("backup-upload-file", html)
        self.assertIn("/api/backups/upload", html)
        self.assertIn("app-version-badge", html)
        self.assertIn("web-auth-badge", html)
        self.assertIn("WEB_AUTH", html)
        self.assertIn("authHeaders", html)
        self.assertIn("X-GP-Token", html)
        self.assertIn("gp_token", html)
        self.assertNotIn("settings-version", html)
        self.assertIn("settings-enable-ipv6", html)
        self.assertIn("settings-debug-stdout", html)
        self.assertIn("settings-discovery-panel", html)
        self.assertIn("Подробный debug-лог stdout", html)
        self.assertIn("может увеличить запись на диск", html)
        self.assertIn("settings-release-panel", html)
        self.assertIn("settings-release-current", html)
        self.assertIn("settings-release-stable", html)
        self.assertIn("settings-release-prerelease", html)
        self.assertIn("release-version-link", html)
        self.assertIn("settings-release-log", html)
        self.assertIn("data-action=\"check-releases\"", html)
        self.assertIn("data-action=\"update-from-release\"", html)
        self.assertIn("data-action=\"toggle-update-log\"", html)
        self.assertIn("/api/releases", html)
        self.assertIn("/api/releases/update-plan", html)
        self.assertIn("/api/releases/update", html)
        self.assertIn("releaseUpdate", html)
        self.assertIn("checkReleases({ silent: true })", html)
        self.assertIn("Обновление поставлено в очередь", html)
        self.assertNotIn("Релизы еще не проверялись. Обновление из UI", html)
        self.assertIn("Установить выбранное обновление", html)
        self.assertIn("debug_stdout", html)
        self.assertIn("/api/settings", html)
        self.assertNotIn("/api/discovery-profiles", html)
        self.assertIn("discovery-profile-select", html)
        self.assertIn("DISCOVERY_PROFILES", html)
        self.assertNotIn("settings-preset-select", html)
        self.assertNotIn("settings-preset-note", html)
        self.assertIn("/api/run-preferences", html)
        self.assertIn("runPreferences", html)
        self.assertIn("useRunPreferencesOnce", html)
        self.assertIn("saveRunPreferencesNow", html)
        self.assertNotIn("scheduleRunPreferencesSave", html)
        self.assertNotIn("settings-default-settings-preset", html)
        self.assertNotIn("SETTINGS_PRESETS", html)
        self.assertNotIn("setSettingsPreset", html)
        self.assertIn("run-selected-discovery", html)
        self.assertIn("Все домены на одной стратегии", html)
        self.assertIn("useDiscoveryProfile", html)
        self.assertNotIn("discovery-profile-name", html)
        self.assertNotIn("saveDiscoveryProfile", html)
        self.assertNotIn("deleteDiscoveryProfile", html)
        self.assertIn("/api/domain-sources", html)
        self.assertIn("/api/domain-sources/v2fly/categories", html)
        self.assertIn("/api/domain-sources/v2fly/preview", html)
        self.assertIn("/api/domain-sources/v2fly/import", html)
        self.assertIn("v2fly-category-search", html)
        self.assertIn("v2fly-category-status", html)
        self.assertIn("v2fly-category-matches", html)
        self.assertIn("v2fly-domains", html)
        self.assertIn("suggestV2flyPresetName", html)
        self.assertIn("data-action=\"v2fly-load-categories\"", html)
        self.assertIn("data-action=\"v2fly-select-category\"", html)
        self.assertIn("v2fly-preset-name", html)
        self.assertIn("v2fly-preview-result", html)
        self.assertIn("не гарантия полного покрытия сервиса", html)
        self.assertIn("renderV2flyCategoryCatalog", html)
        self.assertIn("loadV2flyCategories", html)
        self.assertIn("previewV2flyPreset", html)
        self.assertIn("importV2flyPreset", html)
        self.assertIn("setV2flyLocalError", html)
        self.assertIn("function clearV2flyDomains", html)
        self.assertIn("clearV2flyDomains();", html)
        self.assertIn("Локальный каталог v2fly", html)
        self.assertNotIn("params.set('check'", html)
        self.assertNotIn("params.set('refresh'", html)
        self.assertNotIn("Ошибка проверки v2fly: ${error.message}`, 'bad'", html)
        self.assertNotIn("Ошибка сохранения v2fly: ${error.message}`, 'bad'", html)
        self.assertIn("state.presetManager.name = data.preset;", html)
        self.assertIn("if (data.preset) await loadPresetEditorFromSelection({ silent: true });", html)
        self.assertNotIn("v2fly-scope", html)
        self.assertNotIn("v2fly-categories", html)
        self.assertNotIn("v2fly-category-list", html)
        self.assertNotIn("data-v2fly-category", html)
        self.assertNotIn("domain/full", html)
        self.assertIn("uniqueDomainCount", html)
        self.assertIn("Файлы бекапа", html)
        self.assertIn("Восстановить из бекапа", html)
        self.assertIn("Удалить бекап", html)
        self.assertIn("data-backup-restore", html)
        self.assertIn("data-backup-delete", html)
        self.assertNotIn("backup-restore-select", html)
        self.assertNotIn("backup-restore-preview", html)
        self.assertNotIn("data-action=\"restore-selected-backup\"", html)
        self.assertIn("restoreBackup", html)
        self.assertIn("deleteBackup", html)
        self.assertNotIn("refreshBackupRestorePreview", html)
        self.assertNotIn("renderBackupRestorePreview", html)
        self.assertIn("/api/presets", html)
        self.assertIn("/api/presets/save", html)
        self.assertIn("/api/presets/delete-users-lists", html)
        self.assertIn("/api/presets/domains", html)
        self.assertIn("domain-preset-manager-panel", html)
        self.assertNotIn("profiles-manager-panel", html)
        self.assertNotIn("settings-presets-manager-panel", html)
        self.assertNotIn("preset-manager-scope", html)
        self.assertIn("preset-manager-name", html)
        self.assertNotIn("preset-manager-query", html)
        self.assertNotIn("preset-domain-list", html)
        self.assertNotIn("preset-domain-row", html)
        self.assertNotIn("preset-editor-name", html)
        self.assertIn("preset-editor-domains", html)
        self.assertIn("preset-editor-preview", html)
        self.assertIn("preset-new-name", html)
        self.assertIn("preset-new-domains", html)
        self.assertIn("data-action=\"preset-new-save\"", html)
        self.assertIn("data-action=\"preset-editor-delete\"", html)
        self.assertIn("savePresetNew", html)
        self.assertIn("deletePresetEditor", html)
        self.assertIn("Создать новый список", html)
        self.assertIn("system:required", html)
        self.assertIn("systemPresets", html)
        self.assertIn("systemPresetMeta", html)
        self.assertIn("fetchAllPresetDomains", html)
        self.assertIn("function hasCustomPreset(target, name)", html)
        self.assertIn("function managerPresetEntries()", html)
        self.assertIn("function managerPresetEntry(name)", html)
        self.assertIn("const builtin = builtInPresets(target).find((item) => item.key === name);", html)
        self.assertIn("if (builtin) return uniqueDomains(builtin.domains);", html)
        self.assertNotIn("refreshPresetManager", html)
        self.assertNotIn("togglePresetDomain", html)
        self.assertIn("loadPresetEditorFromSelection", html)
        self.assertNotIn("previewPresetEditor", html)
        self.assertIn("savePresetEditor", html)
        self.assertIn("exportPresetEditor", html)
        self.assertIn("customPresetMeta", html)
        self.assertNotIn("Показать домены", html)
        self.assertNotIn("Показать изменения", html)
        self.assertIn("Скачать TXT", html)
        self.assertIn("candidateGroups(rows)", html)
        self.assertIn("data-candidate-view=\"domain\"", html)
        self.assertIn("data-candidate-view=\"common\"", html)
        self.assertIn("domain-group", html)
        self.assertIn("protocol-group", html)
        self.assertIn("domain-strategy-box", html)
        self.assertIn("strategy-editor", html)
        self.assertIn("strategy-code", html)
        self.assertIn("line-numbers", html)
        self.assertIn("line-numbered-textarea", html)
        self.assertIn("text-editor", html)
        self.assertIn("STRATEGY_LIST_LIMIT", html)
        self.assertIn("expandedStrategyLists", html)
        self.assertIn("strategyEditorScrolls", html)
        self.assertIn("strategyListState", html)
        self.assertIn("normalizeStrategyArg", html)
        self.assertIn("FRAGMENTATION_CLASSES", html)
        self.assertIn("candidate-hide-risky", html)
        self.assertIn("data-fragmentation-class=\"position_risky\"", html)
        self.assertIn("fragmentationBadge", html)
        self.assertIn("strategyFamilyGroups", html)
        self.assertIn("strategy-family-list", html)
        self.assertIn("strategy-family-reason", html)
        self.assertIn("appendCandidateFilters", html)
        self.assertIn("loadAllDomainStrategies", html)
        self.assertIn("loadAllCommonStrategies", html)
        self.assertIn("Показать все общие стратегии", html)
        self.assertIn("domainFromStrategyListKey", html)
        self.assertIn("isCommonStrategyListKey", html)
        self.assertIn("Показать все стратегии домена", html)
        self.assertIn("data-strategy-list-toggle", html)
        self.assertIn("data-strategy-code-key", html)
        self.assertIn("rememberStrategyEditorScrolls", html)
        self.assertIn("restoreStrategyEditorScrolls", html)
        self.assertIn("strategyEditorScrollKey", html)
        self.assertIn("updateEditorLineNumbers", html)
        self.assertIn("data-line-numbers-for=\"finder-domains\"", html)
        self.assertIn("data-line-numbers-for=\"common-domains\"", html)
        self.assertIn("color-scheme: dark", html)
        self.assertIn("#161c27", html)
        self.assertIn("#1b2434", html)
        self.assertIn("#0097dc", html)
        self.assertIn("dynamicCommonRows", html)
        self.assertIn("selectedFinderDomains", html)
        self.assertIn("selectedCommonDomains", html)
        self.assertIn("candidateKnownVersion", html)
        self.assertIn("candidateCacheValid", html)
        self.assertIn("syncCandidateVersion", html)
        self.assertIn("invalidateCandidateCaches", html)
        self.assertIn("common-controls", html)
        self.assertIn(".common-filter-panel .preset-grid", html)
        self.assertIn("common-domains", html)
        self.assertIn("tested-domain-options", html)
        self.assertIn("common-domain-add", html)
        self.assertIn("common-domain-suggestions", html)
        self.assertIn("data-common-domain-suggestion", html)
        self.assertIn("commonDomainSuggestions", html)
        self.assertIn("renderCommonDomainSuggestions", html)
        self.assertIn("finder-preset-select", html)
        self.assertIn("common-preset-select", html)
        self.assertIn("CUSTOM_SELECT_VALUE", html)
        self.assertIn("markDomainPresetCustom", html)
        self.assertIn("markDiscoveryProfileCustom", html)
        self.assertIn("discovery-profile-note", html)
        self.assertIn("multi-curl-field", html)
        self.assertIn("zapretCompactStatus", html)
        self.assertIn("compact-status", html)
        self.assertIn("optgroup label=\"Персональные\"", html)
        self.assertIn("label: 'Обязательные'", html)
        self.assertIn("label: 'Сервисы'", html)
        self.assertIn("label: 'Готовые наборы'", html)
        self.assertNotIn("label: 'Диагностика'", html)
        self.assertIn("label: 'Протестированные'", html)
        self.assertNotIn("data-preset-use=\"finder\"", html)
        self.assertNotIn("data-preset-use=\"common\"", html)
        self.assertNotIn("data-action=\"use-discovery-profile\"", html)
        self.assertNotIn("data-preset-save=\"common\"", html)
        self.assertNotIn("data-preset-delete=\"common\"", html)
        self.assertIn("CUSTOM_PRESETS_KEY", html)
        self.assertIn("localStorage", html)
        self.assertIn("testedDomains()", html)
        self.assertIn("domainsTouched", html)
        self.assertIn("domainsInitialized", html)
        self.assertIn("id=\"toast\"", html)
        self.assertIn("showToast", html)
        self.assertIn("progress-fill", html)
        self.assertIn("progress-attempted", html)
        self.assertIn("progress-strategies", html)
        self.assertIn("progress-successful", html)
        self.assertIn("progress-phase", html)
        self.assertIn("progress-elapsed", html)
        self.assertIn("progress-metrics", html)
        self.assertIn("phaseLabel", html)
        self.assertIn("renderRunSettingsSummary", html)
        self.assertIn("run_settings", html)
        self.assertIn("attempt_total", html)
        self.assertIn("strategy_total", html)
        self.assertIn("eta_estimate_ms_per_attempt", html)
        self.assertIn("расчитанное среднее время попытки", html)
        self.assertNotIn("curl: ${processes.curl", html)
        self.assertIn("progressLiveElapsedSeconds(progress)", html)
        self.assertIn("progressLiveEtaSeconds(progress)", html)
        self.assertIn("runCandidateCount(row)", html)
        self.assertIn("runProgressText(row)", html)
        self.assertIn("data-action=\"run-selected-discovery\"", html)
        self.assertNotIn("data-action=\"multi-domain-discovery\"", html)
        self.assertNotIn("data-action=\"standard-discovery\"", html)
        self.assertIn("/api/jobs/zapret-multi-domain-discovery", html)
        self.assertIn("curl-parallelism", html)
        self.assertNotIn('id="curl-parallelism" type="number" min="1" max="10"', html)
        self.assertNotIn("id=\"settings-curl-max\" type=\"number\" min=\"1\" max=\"10\"", html)
        self.assertIn("Можно ставить любое число от 1", html)
        self.assertIn("value=\"4\"", html)
        self.assertIn("curlParallelism()", html)
        self.assertIn("curl_parallelism", html)
        self.assertIn("enable-http", html)
        self.assertIn("enable-tls12", html)
        self.assertIn("enable-tls13", html)
        self.assertIn("include-quic", html)
        self.assertIn("scan-level", html)
        self.assertIn("repeats", html)
        self.assertIn("repeat-parallel", html)
        self.assertIn("skip-dnscheck", html)
        self.assertIn("skip-ipblock", html)
        self.assertIn("Расширенные параметры", html)
        self.assertIn("run-curl-max-time", html)
        self.assertIn("run-curl-max-time-quic", html)
        self.assertIn("run-curl-max-time-doh", html)
        self.assertIn("runTimeoutSettings()", html)
        self.assertIn("RUN_TIMEOUT_CONTROL_IDS", html)
        self.assertIn("saveLaunchTimeoutDefaultsNow", html)
        self.assertNotIn("settings-curl-max-time", html)
        self.assertNotIn("settings-curl-max-time-quic", html)
        self.assertNotIn("settings-curl-max-time-doh", html)
        self.assertIn("discoveryOptions()", html)
        self.assertIn("hasEnabledProtocol", html)
        self.assertIn("enable_http", html)
        self.assertIn("enable_tls12", html)
        self.assertIn("enable_tls13", html)
        self.assertIn("scan_level", html)
        self.assertIn("repeat_parallel", html)
        self.assertIn("skip_dnscheck", html)
        self.assertIn("skip_ipblock", html)
        self.assertNotIn("Пресет настроек", html)
        self.assertIn("limit-time-enabled", html)
        self.assertIn("time-limit-field", html)
        self.assertIn("timeoutSecondsOrNull", html)
        self.assertIn("data-tooltip=\"Запускает штатную проверку стратегий", html)
        self.assertIn("data-tooltip=\"Одна стратегия запускается один раз", html)
        self.assertIn("grid-template-columns: minmax(0, 460px) minmax(0, 1fr)", html)
        self.assertIn("grid-template-columns: minmax(0, 1fr)", html)
        self.assertNotIn(".finder-layout {\n  grid-template-columns: minmax(0, 520px);\n  max-width: 560px;\n}", html)
        self.assertIn("grid-template-columns: repeat(auto-fit, minmax(180px, 1fr))", html)
        self.assertIn("grid-template-columns: repeat(auto-fit, minmax(150px, 1fr))", html)
        self.assertIn("class=\"button-row run-actions\"", html)
        self.assertIn("min-width: 760px", html)
        self.assertIn("table-layout: auto", html)
        self.assertIn(".run-history", html)
        self.assertIn(".run-card-main", html)
        self.assertIn(".run-card-actions", html)
        self.assertIn(".run-card-status-success", html)
        self.assertIn(".run-card-status-timeout", html)
        self.assertIn(".run-card-kind-multi", html)
        self.assertIn("runSettingsText", html)
        self.assertIn("runPayload", html)
        self.assertIn("repeatRun", html)
        self.assertIn("data-run-repeat", html)
        self.assertIn("Повторить с этими настройками", html)
        self.assertIn(".run-domain-list", html)
        self.assertIn(".run-domain-chip", html)
        self.assertIn(".run-domains-preview", html)
        self.assertIn(".run-domains-count", html)
        self.assertIn(".run-domains-arrow", html)
        self.assertIn(".run-domains[open]", html)
        self.assertIn("openRunDomains", html)
        self.assertIn("data-run-domains", html)
        self.assertIn("renderRunCard(row)", html)
        self.assertIn("runCardClass(row)", html)
        self.assertIn("runDomainKey(row)", html)
        self.assertIn("служба с повышенными правами", html)
        self.assertIn("metric-job-card", html)
        self.assertIn("/api/events", html)
        self.assertIn("new EventSource('/api/events')", html)
        self.assertIn("startRealtimeEvents", html)
        self.assertIn("startRealtimeFallback", html)
        self.assertIn("stdout_size", html)
        self.assertIn("mergeLogPayload", html)
        self.assertIn("30000", html)
        self.assertNotIn("setInterval(refresh, 5000)", html)
        self.assertIn("statusCheck", html)
        self.assertIn("zapretDiagnostics", html)
        self.assertIn("status-check-message", html)
        self.assertIn("testedDomainCount", html)
        self.assertIn("lastCandidateDomainTotal", html)
        self.assertNotIn("metric-last-run", html)
        self.assertNotIn("Последний запуск", html)
        self.assertNotIn("data-action=\"refresh\"", html)
        self.assertNotIn("Обновить данные", html)
        self.assertIn("runStatusLabel(status)", html)
        self.assertIn("metricJobNoteText(ready, busy, jobStatus, status)", html)
        self.assertNotIn("jobDetails.push", html)
        self.assertNotIn("этап: ${phase}", html)
        self.assertIn("etaModeLabel(progress)", html)
        self.assertIn("loading-skeleton", html)
        self.assertIn("candidateLoading", html)
        self.assertIn("candidateUpdatedAt", html)
        self.assertIn("backupsLoading", html)
        self.assertIn("backupsUpdatedAt", html)
        self.assertIn("renderWebAuthStatus", html)
        self.assertIn("friendlyTime", html)
        self.assertIn("backups-updated-at", html)
        self.assertIn("останавливается", html)
        self.assertIn("остановлено", html)
        self.assertIn("сохраняются результаты", html)
        self.assertIn("ошибка сохранения", html)
        self.assertIn("runDomains(row, domainKey)", html)
        self.assertIn("runDomainChips(domains)", html)
        self.assertIn("runDiagnosticsSummary(row)", html)
        self.assertIn("runDiagnostics(row)", html)
        self.assertIn(".run-diagnostics", html)
        self.assertIn(".run-diagnostic-table", html)
        self.assertIn("diagnosticTableRow", html)
        self.assertIn("diagnosticShortLabel", html)
        self.assertIn("diagnosticExplanation", html)
        self.assertIn("curlCodeLabel", html)
        self.assertIn("curlCodeDetails", html)
        self.assertIn(".run-diagnostic-tech", html)
        self.assertIn("технически", html)
        self.assertIn("Это не отменяет найденные стратегии", html)
        self.assertNotIn(".run-diagnostic-chip", html)
        self.assertNotIn("Коды curl показывают", html)
        self.assertIn("details.run-domains[data-run-domains]", html)
        self.assertIn("white-space: nowrap", html)
        self.assertIn("isDiscoveryRun(row)", html)
        self.assertIn("runMode(row)", html)
        self.assertIn("data-action=\"stop-current\"", html)
        self.assertIn("Терминал", html)
        self.assertIn("scrollLogToBottom", html)
        self.assertIn("state.activeTab === 'terminal'", html)
        self.assertIn("runSummary(row)", html)
        self.assertIn("latestById", html)
        self.assertNotIn("Задания подбора", html)
        self.assertNotIn("Запуски с находками", html)
        self.assertNotIn("candidate-runs-table", html)
        self.assertNotIn("jobs-table", html)
        self.assertNotIn("table('finder-runs-table'", html)
        self.assertNotIn("jobSummary(row)", html)
        self.assertNotIn("effectiveJobStatus(row)", html)
        self.assertNotIn("{label: 'Лог'", html)
        self.assertNotIn("{label: 'Детали'", html)
        self.assertNotIn("JSON.stringify(row.result)", html)
        self.assertNotIn("data-candidate-verify", html)
        self.assertNotIn("candidateCopyGroups", html)
        self.assertNotIn("registerCopyText", html)
        self.assertNotIn("strategy-textarea", html)
        self.assertNotIn("strategyText", html)
        self.assertNotIn("strategyItem", html)
        self.assertNotIn("strategy-item", html)
        self.assertNotIn("data-copy-scope", html)
        self.assertNotIn("data-copy-candidate-id", html)
        self.assertNotIn("copyTextForButton", html)
        self.assertNotIn("copy-fallback", html)
        self.assertNotIn("showCopyFallback", html)
        self.assertNotIn("Копировать группу", html)
        self.assertNotIn("Копировать стратегию", html)
        self.assertNotIn("Копировать домен", html)
        self.assertNotIn("candidate-message", html)
        self.assertNotIn("setCandidateMessage", html)
        self.assertNotIn("<code>nfqws2", html)
        self.assertNotIn("{label: 'ID'", html)
        self.assertNotIn("{label: 'Найдено'", html)
        self.assertNotIn("Синхронизировать", html)
        self.assertNotIn("dry-run", html)
        self.assertNotIn("Браузер заблокировал буфер", html)
        self.assertNotIn("Проверка домена", html)
        self.assertNotIn("Проверки доступности", html)
        self.assertNotIn("Технические данные", html)
        self.assertNotIn("Фильтр по домену", html)
        self.assertNotIn("Показать еще стратегии этого домена", html)
        self.assertNotIn("data-domain-load-more", html)
        self.assertNotIn("/api/rules", html)
        self.assertNotIn("/api/strategies", html)
        self.assertNotIn("/api/healthchecks", html)
        self.assertNotIn("/api/jobs/validate", html)
        self.assertNotIn("/api/jobs/sync-pull-only", html)
        self.assertNotIn("/api/jobs/render-dry-run", html)
        self.assertNotIn("/api/jobs/healthcheck-direct", html)
        self.assertNotIn("/api/jobs/zapret-strategy-check", html)
        self.assertNotIn("/api/jobs/zapret-custom-verification", html)

    def test_launch_summary_panel_is_next_to_start_actions(self) -> None:
        html = index_html()

        summary_start = html.index('class="run-launch-summary"')
        actions_start = html.index('class="button-row run-actions"')
        self.assertLess(summary_start, actions_start)
        self.assertIn('aria-label="Сводка параметров запуска"', html)
        self.assertIn("run-launch-readiness", html)
        self.assertIn("run-launch-summary-grid", html)
        self.assertIn("Параметры запуска", html)

    def test_launch_summary_shows_result_affecting_parameters(self) -> None:
        html = index_html()

        for label in (
            "Домены запуска",
            "Обязательные",
            "Желательные",
            "Источник",
            "Режим",
            "Проверочные запросы",
            "Протоколы",
            "IP-режим",
            "Глубина",
            "DNS/IP-check",
            "Повторы",
            "Лимит времени",
            "Таймауты",
        ):
            self.assertIn(label, html)
        self.assertIn("selectedFinderPresetSummary", html)
        self.assertIn("selectedRunModeLabel", html)
        self.assertIn("protocolSummary(options)", html)
        self.assertIn("curlParallelism()", html)
        self.assertIn("timeoutSecondsOrNull()", html)

    def test_launch_summary_has_explicit_readiness_states(self) -> None:
        html = index_html()

        self.assertIn("runLaunchReadiness", html)
        self.assertIn("Готово к старту", html)
        self.assertIn("Требуется настройка", html)
        self.assertIn("Нужны домены", html)
        self.assertIn("Нужен протокол", html)
        self.assertIn("Идет подбор", html)
        self.assertIn("hasEnabledProtocol(options)", html)

    def test_curl_parallelism_field_is_scoped_to_multi_domain_mode(self) -> None:
        html = index_html()

        self.assertIn("[hidden] { display: none !important; }", html)
        self.assertIn('<div class="field multi-curl-field" id="multi-curl-field" hidden>', html)
        field_start = html.index('id="multi-curl-field" hidden')
        field_end = html.index("</div>", html.index("Работает только в режиме", field_start))
        field_html = html[field_start:field_end]

        self.assertIn('id="curl-parallelism"', field_html)
        self.assertIn("Все домены на одной стратегии", field_html)
        self.assertIn("curlField.hidden = mode !== 'multi';", html)

    def test_discovery_profile_is_advanced_blockcheck_scan_level_control(self) -> None:
        html = index_html()

        self.assertNotIn("Профиль подбора", html)
        self.assertIn("Глубина проверки стратегий", html)
        self.assertNotIn("Уровень поиска blockcheck2", html)
        options_start = html.index('<div class="preset-panel finder-options-panel">')
        options_end = html.index('<details class="preset-panel">', options_start)
        options_html = html[options_start:options_end]
        advanced_start = options_end
        advanced_end = html.index("</details>", advanced_start)
        advanced_html = html[advanced_start:advanced_end]

        self.assertIn('id="enable-http"', options_html)
        self.assertIn('id="enable-tls12"', options_html)
        self.assertIn('id="include-quic"', options_html)
        self.assertNotIn('id="discovery-profile-select"', options_html)
        self.assertNotIn('id="scan-level"', options_html)
        self.assertNotIn('id="repeats"', options_html)
        self.assertIn('id="discovery-profile-select"', advanced_html)
        self.assertIn('id="scan-level"', advanced_html)
        self.assertIn('id="repeats"', advanced_html)
        self.assertIn('id="limit-time-enabled"', advanced_html)
        self.assertIn('id="run-curl-max-time"', advanced_html)
        self.assertIn('id="run-curl-max-time-quic"', advanced_html)
        self.assertIn('id="run-curl-max-time-doh"', advanced_html)
        render_start = html.index("function renderDiscoveryProfiles()")
        render_end = html.index("function hasEnabledProtocol", render_start)
        render_html = html[render_start:render_end]
        self.assertNotIn('<option value="${CUSTOM_SELECT_VALUE}">Custom</option>', render_html)
        self.assertNotIn("event.target.value === CUSTOM_SELECT_VALUE", html[html.index("if (event.target && event.target.id === 'discovery-profile-select')"):html.index("if (event.target && event.target.name === 'run-mode')")])

    def test_index_script_has_no_raw_newlines_inside_quoted_strings(self) -> None:
        html = index_html()
        marker_start = "<script>"
        marker_end = "</script>"
        start = html.index(marker_start) + len(marker_start)
        end = html.index(marker_end, start)
        script = html[start:end]

        errors: list[int] = []
        line = 1
        in_string: str | None = None
        in_template = False
        in_regex = False
        in_regex_class = False
        in_line_comment = False
        in_block_comment = False
        escaped = False
        last_significant: str | None = None
        index = 0
        while index < len(script):
            char = script[index]
            next_char = script[index + 1] if index + 1 < len(script) else ""
            if char == "\n":
                line += 1
                in_line_comment = False
            if in_line_comment:
                index += 1
                continue
            if in_block_comment:
                if char == "*" and next_char == "/":
                    in_block_comment = False
                    index += 2
                    continue
                index += 1
                continue
            if in_regex:
                if char in "\r\n":
                    errors.append(line)
                    in_regex = False
                    in_regex_class = False
                elif escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == "[":
                    in_regex_class = True
                elif char == "]":
                    in_regex_class = False
                elif char == "/" and not in_regex_class:
                    in_regex = False
                    last_significant = "/"
                index += 1
                continue
            if in_string:
                if char in "\r\n":
                    errors.append(line)
                    in_string = None
                elif escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == in_string:
                    in_string = None
                index += 1
                continue
            if in_template:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == "`":
                    in_template = False
                index += 1
                continue
            if char == "/" and next_char == "/":
                in_line_comment = True
                index += 2
                continue
            if char == "/" and next_char == "*":
                in_block_comment = True
                index += 2
                continue
            if char == "/" and (last_significant is None or last_significant in "([{=,:;!&|?"):
                in_regex = True
                escaped = False
                in_regex_class = False
                index += 1
                continue
            if char in ("'", '"'):
                in_string = char
            elif char == "`":
                in_template = True
            if not char.isspace():
                last_significant = char
            index += 1

        self.assertEqual([], errors[:10], f"Raw newline inside JS string literal near lines: {errors[:10]}")

    def test_index_html_does_not_contain_mojibake_markers(self) -> None:
        html = index_html()

        for marker in ("\u0420\u045b", "\u0420\u0451", "\u0421\u0403"):
            self.assertNotIn(marker, html)

        self.assertIn("Ошибка обновления истории", html)
        self.assertIn("Ошибка обновления лога", html)
        self.assertIn("Ошибка обновления пресетов", html)

    def test_head_root_returns_ok_for_curl_i(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            port = _free_port()
            thread = threading.Thread(target=serve, args=(config, "127.0.0.1", port), daemon=True)
            thread.start()
            time.sleep(0.1)

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("HEAD", "/")
            response = connection.getresponse()
            response.read()
            connection.close()

            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), "text/html; charset=utf-8")
            self.assertEqual(response.getheader("Cache-Control"), "no-store")

    def test_serve_clears_stale_current_job_on_start(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            write_state(config.output.state_dir, {"current_job": "stale-job", "last_error": None})
            port = _free_port()
            thread = threading.Thread(target=serve, args=(config, "127.0.0.1", port), daemon=True)
            thread.start()
            time.sleep(0.1)

            self.assertIsNone(read_state(config.output.state_dir)["current_job"])

    def test_diagnostics_endpoint_returns_runtime_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            port = _free_port()
            thread = threading.Thread(target=serve, args=(config, "127.0.0.1", port), daemon=True)
            thread.start()
            time.sleep(0.1)

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", "/api/diagnostics")
            response = connection.getresponse()
            body = response.read().decode("utf-8")
            connection.close()

            self.assertEqual(response.status, 200)
            self.assertIn('"process"', body)
            self.assertIn('"files"', body)

    def test_status_endpoint_returns_candidate_version_and_release_update(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            log_path = tmp / "update.log"
            log_path.write_text("installed_version=0.3.1\nstatus=success\n", encoding="utf-8")
            write_state(
                tmp / "state",
                {
                    "release_update": {
                        "status": "queued",
                        "target_ref": "v0.3.1",
                        "log_path": str(log_path),
                    }
                },
            )
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            port = _free_port()
            thread = threading.Thread(target=serve, args=(config, "127.0.0.1", port), daemon=True)
            thread.start()
            time.sleep(0.1)

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", "/api/status")
            response = connection.getresponse()
            body = response.read().decode("utf-8")
            connection.close()

            self.assertEqual(response.status, 200)
            self.assertIn('"candidate_version"', body)
            self.assertIn('"size"', body)
            self.assertIn('"mtime_ns"', body)
            self.assertIn('"release_update"', body)
            self.assertIn('"status":"success"', body)

    def test_web_auth_token_protects_mutating_api_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            port = _free_port()
            with mock.patch.dict(os.environ, {"GP_WEB_AUTH": "on", "GP_WEB_TOKEN": "secret-token"}):
                thread = threading.Thread(target=serve, args=(config, "127.0.0.1", port), daemon=True)
                thread.start()
                time.sleep(0.1)

                body = json.dumps({"settings": {"curl_parallelism_max": 17}})
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request("POST", "/api/settings", body=body, headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                response.read()
                connection.close()
                self.assertEqual(response.status, 401)

                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request(
                    "POST",
                    "/api/settings",
                    body=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-GP-Token": "secret-token",
                        "Origin": f"http://127.0.0.1:{port}",
                    },
                )
                response = connection.getresponse()
                saved = response.read().decode("utf-8")
                connection.close()

            self.assertEqual(response.status, 200)
            self.assertIn('"curl_parallelism_max":17', saved)

    def test_web_auth_rejects_cross_origin_mutating_api(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            port = _free_port()
            with mock.patch.dict(os.environ, {"GP_WEB_AUTH": "on", "GP_WEB_TOKEN": "secret-token"}):
                thread = threading.Thread(target=serve, args=(config, "127.0.0.1", port), daemon=True)
                thread.start()
                time.sleep(0.1)

                body = json.dumps({"settings": {"curl_parallelism_max": 17}})
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request(
                    "POST",
                    "/api/settings",
                    body=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-GP-Token": "secret-token",
                        "Origin": "http://evil.local",
                    },
                )
                response = connection.getresponse()
                error = response.read().decode("utf-8")
                connection.close()

            self.assertEqual(response.status, 403)
            self.assertIn("origin", error)

    def test_web_auth_token_protects_sensitive_get_api_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            port = _free_port()
            with mock.patch.dict(os.environ, {"GP_WEB_AUTH": "on", "GP_WEB_TOKEN": "secret-token"}):
                thread = threading.Thread(target=serve, args=(config, "127.0.0.1", port), daemon=True)
                thread.start()
                time.sleep(0.1)

                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request("GET", "/api/backups/restore-preview?snapshot=missing")
                response = connection.getresponse()
                response.read()
                connection.close()
                self.assertEqual(response.status, 401)

                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request(
                    "GET",
                    "/api/backups/restore-preview?snapshot=missing",
                    headers={"X-GP-Token": "secret-token"},
                )
                response = connection.getresponse()
                body = response.read().decode("utf-8")
                connection.close()

            self.assertEqual(response.status, 400)
            self.assertIn("error", body)

    def test_events_endpoint_streams_sse_status(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            port = _free_port()
            thread = threading.Thread(target=serve, args=(config, "127.0.0.1", port), daemon=True)
            thread.start()
            time.sleep(0.1)

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", "/api/events")
            response = connection.getresponse()
            first_line = response.readline().decode("utf-8").strip()
            second_line = response.readline().decode("utf-8").strip()
            connection.close()

            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), "text/event-stream; charset=utf-8")
            self.assertEqual(first_line, "event: status")
            self.assertTrue(second_line.startswith("data:"))

    def test_settings_endpoint_saves_runtime_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            port = _free_port()
            thread = threading.Thread(target=serve, args=(config, "127.0.0.1", port), daemon=True)
            thread.start()
            time.sleep(0.1)

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            body = json.dumps(
                {
                    "settings": {
                        "enable_ipv6": True,
                        "debug_stdout": True,
                        "curl_parallelism_max": 25,
                        "curl_max_time": 1,
                        "curl_max_time_quic": 3,
                        "curl_max_time_doh": 4,
                    }
                }
            )
            connection.request("POST", "/api/settings", body=body, headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            saved = response.read().decode("utf-8")
            connection.close()

            self.assertEqual(response.status, 200)
            self.assertIn('"enable_ipv6":true', saved)
            self.assertIn('"debug_stdout":true', saved)
            self.assertNotIn('"settings_preset_default"', saved)
            self.assertIn('"curl_parallelism_max":25', saved)
            self.assertIn('"curl_max_time":1', saved)
            self.assertIn('"curl_max_time_quic":3', saved)
            self.assertIn('"curl_max_time_doh":4', saved)

    def test_standard_discovery_job_uses_launch_timeout_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            payload = {
                "domains": ["youtube.com"],
                "curl_max_time": 7,
                "curl_max_time_quic": 8,
                "curl_max_time_doh": 9,
            }

            with mock.patch.object(web_app, "run_standard_discovery", return_value={"status": "success"}) as runner:
                result = web_app._job_zapret_standard_discovery(config, payload, object())

            self.assertEqual({"status": "success"}, result)
            self.assertEqual(7, runner.call_args.kwargs["curl_max_time"])
            self.assertEqual(8, runner.call_args.kwargs["curl_max_time_quic"])
            self.assertEqual(9, runner.call_args.kwargs["curl_max_time_doh"])

    def test_multi_domain_discovery_job_uses_launch_timeout_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            payload = {
                "domains": ["youtube.com", "discord.com"],
                "curl_parallelism": 2,
                "curl_max_time": 11,
                "curl_max_time_quic": 12,
                "curl_max_time_doh": 13,
            }

            with mock.patch.object(web_app, "run_multi_domain_discovery", return_value={"status": "success"}) as runner:
                result = web_app._job_zapret_multi_domain_discovery(config, payload, object())

            self.assertEqual({"status": "success"}, result)
            self.assertEqual(11, runner.call_args.kwargs["curl_max_time"])
            self.assertEqual(12, runner.call_args.kwargs["curl_max_time_quic"])
            self.assertEqual(13, runner.call_args.kwargs["curl_max_time_doh"])
            self.assertEqual(2, runner.call_args.kwargs["curl_parallelism"])

    def test_run_preferences_endpoint_saves_last_finder_form(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            port = _free_port()
            thread = threading.Thread(target=serve, args=(config, "127.0.0.1", port), daemon=True)
            thread.start()
            time.sleep(0.1)

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            body = json.dumps(
                {
                    "run_preferences": {
                        "domains": ["youtube.com", "discord.com"],
                        "domain_preset": "custom",
                        "discovery_profile": "custom",
                        "settings_preset": "accelerated",
                        "run_mode": "multi",
                        "curl_parallelism": 19,
                        "enable_http": True,
                        "enable_tls12": True,
                        "enable_tls13": False,
                        "include_quic": True,
                        "enable_ipv6": False,
                        "scan_level": "force",
                        "repeats": 2,
                        "repeat_parallel": True,
                        "skip_dnscheck": False,
                        "skip_ipblock": True,
                        "limit_time_enabled": True,
                        "timeout_hours": 3.5,
                    }
                }
            )
            connection.request("POST", "/api/run-preferences", body=body, headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            saved = response.read().decode("utf-8")
            connection.close()

            self.assertEqual(response.status, 200)
            self.assertIn('"run_mode":"multi"', saved)
            self.assertIn('"curl_parallelism":19', saved)
            self.assertIn('"scan_level":"force"', saved)
            self.assertIn('"timeout_hours":3.5', saved)
            self.assertNotIn('"settings_preset"', saved)

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", "/api/status")
            response = connection.getresponse()
            status = response.read().decode("utf-8")
            connection.close()

            self.assertEqual(response.status, 200)
            self.assertIn('"run_preferences"', status)
            self.assertIn('"youtube.com"', status)
            self.assertIn('"discord.com"', status)
            self.assertNotIn('"settings_preset"', status)

    def test_preset_domain_endpoints_page_and_toggle_domains(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            port = _free_port()
            thread = threading.Thread(target=serve, args=(config, "127.0.0.1", port), daemon=True)
            thread.start()
            time.sleep(0.1)

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            body = json.dumps(
                {"custom": {"finder": {"mine": ["youtube.com", "discord.com", "discordcdn.com"]}, "common": {}}}
            )
            connection.request("POST", "/api/presets", body=body, headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            response.read()
            connection.close()
            self.assertEqual(response.status, 200)

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", "/api/presets/domains?scope=finder&name=mine&limit=2")
            response = connection.getresponse()
            page = response.read().decode("utf-8")
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertIn('"total":3', page)
            self.assertIn('"has_more":true', page)

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            body = json.dumps({"scope": "finder", "name": "mine", "domain": "discord.com", "enabled": False})
            connection.request(
                "POST",
                "/api/presets/domain-enabled",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            toggled = response.read().decode("utf-8")
            connection.close()

            self.assertEqual(response.status, 200)
            self.assertIn('"enabled":false', toggled)
            self.assertNotIn('"discord.com","discordcdn.com"', toggled)

    def test_system_preset_api_allows_empty_save_and_user_only_delete(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            port = _free_port()
            thread = threading.Thread(target=serve, args=(config, "127.0.0.1", port), daemon=True)
            thread.start()
            time.sleep(0.1)

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            body = json.dumps({"scope": "finder", "name": "required", "kind": "system", "domains": []})
            connection.request("POST", "/api/presets/save", body=body, headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            saved = response.read().decode("utf-8")
            connection.close()

            self.assertEqual(response.status, 200)
            self.assertIn('"system"', saved)
            self.assertIn('"required":[]', saved)
            self.assertIn('"enabled_count":0', saved)

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            body = json.dumps({"scope": "finder", "name": "mine", "domains": ["youtube.com"]})
            connection.request("POST", "/api/presets/save", body=body, headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            response.read()
            connection.close()
            self.assertEqual(response.status, 200)

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            body = json.dumps({"scope": "finder", "names": ["mine", "required"]})
            connection.request(
                "POST",
                "/api/presets/delete-users-lists",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            deleted = response.read().decode("utf-8")
            connection.close()

            self.assertEqual(response.status, 200)
            self.assertNotIn('"mine"', deleted)
            self.assertIn('"required":[]', deleted)
            self.assertIn('"kind":"system"', deleted)

    def test_discovery_profiles_endpoint_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            port = _free_port()
            thread = threading.Thread(target=serve, args=(config, "127.0.0.1", port), daemon=True)
            thread.start()
            time.sleep(0.1)

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            body = json.dumps(
                {
                    "profiles": {
                        "night-test": {
                            "title": "Night test",
                            "enable_http": True,
                            "enable_tls12": True,
                            "enable_tls13": True,
                            "include_quic": True,
                            "scan_level": "force",
                            "repeats": 99,
                            "curl_parallelism": 99,
                            "limit_time_enabled": True,
                            "timeout_hours": 99,
                        },
                        "standard": {
                            "title": "Changed built-in",
                            "enable_tls12": False,
                        },
                    }
                }
            )
            connection.request("POST", "/api/discovery-profiles", body=body, headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            saved = response.read().decode("utf-8")
            connection.close()

            self.assertIn(response.status, {404, 405})
            self.assertNotIn('"night-test"', saved)
            self.assertNotIn("Changed built-in", saved)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    unittest.main()
