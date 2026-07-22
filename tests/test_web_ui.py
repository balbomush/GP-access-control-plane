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
from gp_control_plane.web.app import index_html, serve, serve_core, serve_web_proxy


class WebUiTests(unittest.TestCase):
    def test_candidates_and_runs_use_50_item_load_more_pagination(self) -> None:
        html = index_html()

        self.assertIn("const LIST_PAGE_LIMIT = 50;", html)
        self.assertIn("const CANDIDATE_PAGE_LIMIT = LIST_PAGE_LIMIT;", html)
        self.assertIn("const DOMAIN_PAGE_LIMIT = LIST_PAGE_LIMIT;", html)
        self.assertIn("const RUN_PAGE_LIMIT = LIST_PAGE_LIMIT;", html)
        self.assertIn("params.set('limit', String(DOMAIN_PAGE_LIMIT));", html)
        self.assertIn("params.set('limit', String(RUN_PAGE_LIMIT));", html)
        self.assertIn("listLoadMore('load-more-candidates'", html)
        self.assertIn("listLoadMore('load-more-candidate-domains'", html)
        self.assertIn("listLoadMore('load-more-runs'", html)
        self.assertIn('data-action="${esc(action)}"', html)
        self.assertIn("Загрузить еще", html)
        self.assertIn("refreshDomainIndex(false)", html)
        self.assertIn("refreshRuns(false)", html)
        self.assertNotIn(".slice(0, 12)", html)

    def test_refresh_does_not_prefetch_candidates_before_candidates_tab_is_opened(self) -> None:
        html = index_html()

        self.assertIn("if (state.activeTab === 'candidates') ensureCandidateViewLoaded();", html)
        self.assertNotIn("if (!state.candidateDomainsLoaded) refreshDomainIndex();\n    else if", html)

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
        self.assertIn("requestHeaders", html)
        self.assertIn("requestUrl", html)
        self.assertNotIn("authHeaders", html)
        self.assertNotIn("authUrl", html)
        self.assertNotIn("web-auth-badge", html)
        self.assertNotIn("WEB_AUTH", html)
        self.assertNotIn("X-GP-Token", html)
        self.assertNotIn("gp_token", html)
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
        self.assertNotIn("FRAGMENTATION_CLASSES", html)
        self.assertNotIn("candidate-filter-row", html)
        self.assertNotIn("candidate-hide-risky", html)
        self.assertNotIn("data-fragmentation-class=\"position_risky\"", html)
        self.assertNotIn("fragmentationBadge", html)
        self.assertIn("strategyFamilyGroups", html)
        self.assertIn("strategyDisplayFamilyKey", html)
        self.assertIn("return `${protocol}:${family}`;", html)
        self.assertIn("strategy-family-list", html)
        self.assertIn("strategy-family-reason", html)
        self.assertNotIn("appendCandidateFilters", html)
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
        self.assertIn("time-limit-panel", html)
        self.assertIn("time-limit-field", html)
        self.assertIn("timeoutSecondsOrNull", html)
        self.assertIn("syncTimeLimitUi()", html)
        self.assertIn("data-tooltip=\"Запускает штатную проверку стратегий", html)
        self.assertIn("data-tooltip=\"Одна стратегия запускается один раз", html)
        self.assertIn("grid-template-columns: minmax(0, 460px) minmax(0, 1fr)", html)
        self.assertIn("grid-template-columns: minmax(0, 1fr)", html)
        self.assertNotIn(".finder-layout {\n  grid-template-columns: minmax(0, 520px);\n  max-width: 560px;\n}", html)
        self.assertIn("grid-template-columns: repeat(auto-fit, minmax(180px, 1fr))", html)
        self.assertIn("grid-template-columns: repeat(auto-fit, minmax(150px, 1fr))", html)
        self.assertIn(".time-limit-row { grid-template-columns: 1fr; }", html)
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
        self.assertNotIn("renderWebAuthStatus", html)
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

    def test_common_tested_preset_waits_for_loaded_tested_domains(self) -> None:
        html = index_html()

        preset_start = html.index("function presetGroups(target)")
        preset_end = html.index("function presetDomains(target, value)", preset_start)
        preset_html = html[preset_start:preset_end]
        self.assertIn("const tested = testedDomains();", preset_html)
        self.assertIn("if (tested.length)", preset_html)

        select_start = html.index("function renderPresetSelect(target)")
        select_end = html.index("function renderPresetSelects()", select_start)
        select_html = html[select_start:select_end]
        self.assertIn("else if (target === 'common') select.value = CUSTOM_SELECT_VALUE;", select_html)
        self.assertNotIn("target === 'common' && [...select.options].some((option) => option.value === 'builtin:tested')", select_html)
        self.assertIn("function updateTestedDomains(domains)", html)
        self.assertIn("updateTestedDomains(data.tested_domains)", html)
        self.assertIn("renderPresetSelect('common')", html)

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

    def test_expandable_preset_panel_has_clear_affordance(self) -> None:
        html = index_html()

        self.assertIn("details.preset-panel > summary::before", html)
        self.assertIn('content: "Раскрыть";', html)
        self.assertIn("details.preset-panel[open] > summary::after", html)
        self.assertIn('content: "Свернуть";', html)
        self.assertIn("details.preset-panel > summary:focus-visible", html)

    def test_time_limit_panel_keeps_layout_stable_when_disabled(self) -> None:
        html = index_html()

        advanced_start = html.index('<details class="preset-panel">')
        advanced_end = html.index("</details>", advanced_start)
        advanced_html = html[advanced_start:advanced_end]
        time_panel_start = advanced_html.index('id="time-limit-panel"')
        preset_grid_start = advanced_html.index('<div class="preset-grid">')
        preset_grid_html = advanced_html[preset_grid_start:advanced_html.index('id="repeat-parallel"', preset_grid_start)]

        self.assertLess(time_panel_start, preset_grid_start)
        self.assertIn('<div class="time-limit-panel disabled" id="time-limit-panel">', advanced_html)
        self.assertIn('<div class="field time-limit-field" id="time-limit-field" aria-disabled="true">', advanced_html)
        self.assertIn('id="finder-timeout-hours" type="number" min="0.1" max="24" step="0.5" value="6" disabled', advanced_html)
        self.assertNotIn('id="limit-time-enabled"', preset_grid_html)
        self.assertNotIn(".time-limit-field[hidden]", html)
        self.assertNotIn("el('time-limit-field').hidden", html)
        self.assertIn("input.disabled = !enabled;", html)
        self.assertIn("panel.classList.toggle('disabled', !enabled);", html)

    def test_live_run_panel_has_current_operational_slice(self) -> None:
        html = index_html()

        self.assertIn('id="live-run-panel"', html)
        self.assertIn("function renderLiveRun()", html)
        self.assertIn("function liveRunCells(progress)", html)
        for label in ("Текущий подбор", "Статус", "Этап", "Попытки", "Стратегии", "Найдено", "Текущий файл", "Прошло", "Осталось"):
            self.assertIn(label, html)

    def test_live_run_panel_keeps_stop_log_and_results_actions(self) -> None:
        html = index_html()

        self.assertIn('data-action="stop-current"', html)
        self.assertIn('data-action="open-log"', html)
        self.assertIn('data-action="open-candidates"', html)
        self.assertIn("renderLiveRun();", html)
        self.assertIn("latestImportantLogMessage", html)

    def test_live_run_panel_warns_about_interrupted_run_after_restart(self) -> None:
        html = index_html()

        self.assertIn("interruptedRunWarning", html)
        self.assertIn("Предыдущий подбор был прерван перезагрузкой", html)
        self.assertIn("Активный подбор не восстанавливается после перезагрузки", html)
        self.assertIn("!['running', 'queued', 'stopping'].includes(status)", html)

    def test_terminal_tab_keeps_raw_log_as_secondary_debug_block(self) -> None:
        html = index_html()

        self.assertIn('class="raw-log-panel"', html)
        self.assertIn("Raw log / debug", html)
        self.assertLess(html.index('id="live-run-panel"'), html.index('class="raw-log-panel"'))
        self.assertLess(html.index('id="events-panel"'), html.index('class="raw-log-panel"'))

    def test_events_panel_uses_existing_status_log_and_diagnostics(self) -> None:
        html = index_html()

        self.assertIn('id="events-panel"', html)
        self.assertIn("function eventRows()", html)
        self.assertIn("stateBoard.last_error", html)
        self.assertIn("log.stderr_diagnostics", html)
        self.assertIn("state.releaseUpdate", html)
        self.assertIn("releaseStatus === 'failed'", html)
        self.assertNotIn("release.status === 'failed' || release.error", html)
        self.assertNotIn("eventStore", html)

    def test_events_panel_has_repeat_log_and_copy_actions(self) -> None:
        html = index_html()

        self.assertIn('data-action="repeat-last-run"', html)
        self.assertIn('data-action="open-log"', html)
        self.assertIn('data-action="copy-diagnostics"', html)
        self.assertIn("function copyDiagnostics()", html)
        self.assertIn("diagnosticsText()", html)

    def test_candidate_result_panel_has_agreed_modes_and_fields(self) -> None:
        html = index_html()

        self.assertIn("candidate-result-panel", html)
        common_start = html.index('id="common-controls"')
        result_start = html.index('class="candidate-result-panel"', common_start)
        preset_start = html.index('id="common-preset-select"', common_start)
        self.assertGreater(result_start, common_start)
        self.assertGreater(result_start, preset_start)
        self.assertIn("Пресет доменов для пересечения", html)
        self.assertIn("Итоговый набор общих стратегий", html)
        self.assertIn('data-action="build-candidate-result"', html)
        for label in ("Максимум покрытия", "Минимум стратегий", "Баланс"):
            self.assertIn(label, html)
        for field in ("required_coverage", "desired_coverage", "uncovered_required", "uncovered_desired", "strategy_set", "reason", "mode"):
            self.assertIn(field, html)

    def test_candidate_result_is_computed_from_loaded_candidates_only(self) -> None:
        html = index_html()

        self.assertIn("function commonCandidateResultRows()", html)
        self.assertIn("state.candidates", html)
        self.assertIn("const rows = commonCandidateResultRows();", html)
        self.assertNotIn("function loadedCandidateRows()", html)
        self.assertIn("Расчет по загруженным общим стратегиям", html)
        self.assertNotIn("/api/candidate-result", html)

    def test_candidate_result_is_one_button_common_action(self) -> None:
        html = index_html()

        self.assertIn("candidateResultRequested: false", html)
        self.assertIn("function buildCandidateResultNow()", html)
        self.assertIn("state.candidateResultRequested = true;", html)
        self.assertIn("if (!state.candidateResultRequested)", html)
        self.assertIn("panel.hidden = state.candidateView !== 'common';", html)
        self.assertIn("Нажмите «Собрать итоговый набор»", html)
        self.assertIn("state.candidateResultRequested = false;", html)

    def test_candidate_result_does_not_fallback_required_to_launch_domains(self) -> None:
        html = index_html()

        start = html.index("function candidateResultTargets()")
        end = html.index("function commonCandidateResultRows()", start)
        target_html = html[start:end]
        self.assertIn("const required = uniqueDomains(presetDomains('finder', 'system:required'));", target_html)
        self.assertIn("const desired = uniqueDomains(presetDomains('finder', 'system:desired'))", target_html)
        self.assertNotIn("selectedFinderDomains()", target_html)
        self.assertNotIn("required.length ? required", target_html)
        self.assertIn("Нет обязательных или желательных доменов для расчета итогового набора.", html)

    def test_candidate_result_actions_are_practical_without_new_validation(self) -> None:
        html = index_html()

        self.assertIn('data-action="copy-candidate-result"', html)
        self.assertIn('data-action="export-candidate-result"', html)
        self.assertIn('data-action="use-candidate-result-domains"', html)
        self.assertIn('data-action="open-candidate-result"', html)
        self.assertNotIn("data-candidate-verify", html)
        self.assertNotIn("/api/jobs/zapret-custom-verification", html)

    def test_candidate_balance_covers_required_before_desired(self) -> None:
        html = index_html()

        self.assertIn("requiredGain * 100000 + desiredGain * 1000", html)
        self.assertIn("uncoveredRequired", html)
        self.assertIn("uncoveredDesired", html)
        self.assertIn("Нет загруженных стратегий, которые покрывают выбранные домены.", html)

    def test_history_repeat_fills_launch_form_without_autostart(self) -> None:
        html = index_html()

        start = html.index("function repeatRun(runKey)")
        end = html.index("function runProgressText(row)", start)
        repeat_html = html[start:end]
        self.assertIn("fillRunFormFromPayload(row, payload);", repeat_html)
        self.assertNotIn("startJob(", repeat_html)
        self.assertIn("Параметры прошлого подбора перенесены в форму запуска", html)

    def test_history_repeat_restores_result_affecting_parameters(self) -> None:
        html = index_html()

        start = html.index("function fillRunFormFromPayload")
        end = html.index("function repeatRun(runKey)", start)
        repeat_html = html[start:end]
        for token in ("finder-domains", "run-mode", "curl-parallelism", "enable-http", "enable-tls12", "include-quic", "scan-level", "repeats", "skip-dnscheck", "run-curl-max-time", "limit-time-enabled"):
            self.assertIn(token, repeat_html)

    def test_mutating_actions_are_blocked_during_active_run(self) -> None:
        html = index_html()

        self.assertIn("const MUTATING_ACTIONS = new Set", html)
        for action in ("save-settings", "check-releases", "update-from-release", "create-backup", "upload-backup", "preset-editor-save", "preset-editor-delete", "preset-new-save"):
            self.assertIn(action, html)
        self.assertIn("requireNoActiveRun()", html)
        self.assertIn("protectedMutation", html)

    def test_mutating_buttons_are_disabled_but_monitoring_actions_remain(self) -> None:
        html = index_html()

        self.assertIn("mutatingSelectors", html)
        self.assertIn("button.disabled = busy;", html)
        self.assertIn("button.disabled = !busy;", html)
        self.assertNotIn("'open-log'", html[html.index("const MUTATING_ACTIONS = new Set"):html.index("]);", html.index("const MUTATING_ACTIONS = new Set"))])

    def test_settings_auto_release_check_waits_for_active_run(self) -> None:
        html = index_html()

        self.assertIn("if (!mutatingBlocked() && !state.releaseChecked && !state.releaseChecking) checkReleases({ silent: true });", html)

    def test_settings_are_split_into_operational_groups(self) -> None:
        html = index_html()

        for marker in ("settings-discovery-panel", "settings-release-panel", "settings-backups-panel", "settings-danger-panel"):
            self.assertIn(marker, html)
        for title in ("Параметры подбора", "Релизы и обновления", "Бекапы и восстановление", "Опасные действия"):
            self.assertIn(title, html)

    def test_tabs_have_basic_accessibility_contract(self) -> None:
        html = index_html()

        self.assertIn('role="tablist"', html)
        self.assertIn('role="tab"', html)
        self.assertIn('aria-selected="true"', html)
        self.assertIn('aria-controls="tab-panel-finder"', html)
        self.assertIn('role="tabpanel"', html)
        self.assertIn("syncActiveTabUi", html)

    def test_progress_and_focus_have_accessibility_contract(self) -> None:
        html = index_html()

        self.assertIn('role="progressbar"', html)
        self.assertIn('aria-valuemin="0"', html)
        self.assertIn('aria-valuemax="100"', html)
        self.assertIn("aria-valuenow", html)
        self.assertIn("button:focus-visible", html)
        self.assertIn("summary:focus-visible", html)

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

    def test_core_mode_serves_api_without_web_ui(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            port = _free_port()
            thread = threading.Thread(target=serve_core, args=(config, "127.0.0.1", port), daemon=True)
            thread.start()
            time.sleep(0.1)

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", "/")
            root_response = connection.getresponse()
            root_body = root_response.read().decode("utf-8")
            connection.close()

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", "/api/status")
            api_response = connection.getresponse()
            api_body = api_response.read().decode("utf-8")
            connection.close()

            self.assertEqual(root_response.status, 404)
            self.assertIn("web ui is disabled", root_body)
            self.assertNotIn("<!doctype html>", root_body.lower())
            self.assertEqual(api_response.status, 200)
            self.assertIn('"version"', api_body)

    def test_openapi_and_swagger_are_served_by_monolith_and_core_mode(self) -> None:
        for target in (serve, serve_core):
            with tempfile.TemporaryDirectory() as raw:
                tmp = Path(raw)
                config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
                port = _free_port()
                thread = threading.Thread(target=target, args=(config, "127.0.0.1", port), daemon=True)
                thread.start()
                time.sleep(0.1)

                status, headers, body = _http_request(port, "/openapi.json")
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("content-type"), "application/json; charset=utf-8")
                openapi_contract = json.loads(body.decode("utf-8"))
                self.assertEqual(openapi_contract["openapi"], "3.1.0")
                self.assertNotIn("jsonSchemaDialect", openapi_contract)
                self.assertNotIn("securitySchemes", openapi_contract["components"])
                self.assertEqual([{"url": "/"}], [{"url": server["url"]} for server in openapi_contract["servers"]])
                self.assertNotIn("localhost", body.decode("utf-8"))
                self.assertNotIn("127.0.0.1:8081", body.decode("utf-8"))
                examples = openapi_contract["components"]["examples"]
                self.assertIn("StartRunRequestMultiDomain", examples)
                self.assertEqual(30, examples["StartRunRequestMultiDomain"]["value"]["curl_parallelism"])
                self.assertIn("CoreStatusRunning", examples)
                self.assertIn("PagedStrategyCandidatesResponse", examples)
                self.assertIn(
                    "multiDomain30Parallel",
                    openapi_contract["paths"]["/api/core/strategy-discovery/start-run"]["post"]["requestBody"][
                        "content"
                    ]["application/json"]["examples"],
                )
                self.assertIn(
                    "structuredError",
                    openapi_contract["components"]["responses"]["Error"]["content"]["application/json"]["examples"],
                )
                self.assertIn("/api/core/presets/delete-user-domain-list", openapi_contract["paths"])
                self.assertNotIn("/api/core/presets/delete-user-lists", openapi_contract["paths"])
                delete_schema = openapi_contract["components"]["schemas"]["DeleteDomainListRequest"]
                self.assertEqual(["list_ids"], delete_schema["required"])
                self.assertEqual(1, delete_schema["properties"]["list_ids"]["minItems"])

                status, headers, body = _http_request(port, "/swagger")
                swagger_html = body.decode("utf-8")
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("content-type"), "text/html; charset=utf-8")
                self.assertIn("SwaggerUIBundle", swagger_html)
                self.assertIn("url: '/openapi.json'", swagger_html)

                status, headers, body = _http_request(port, "/swagger", method="HEAD")
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("content-type"), "text/html; charset=utf-8")
                self.assertEqual(body, b"")

    def test_openapi_paths_are_callable_through_web_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            web_app.save_settings(config, {"curl_parallelism_max": 50, "curl_parallelism_default": 10})
            snapshot = web_app.create_snapshot_if_idle(config.output.state_dir)["snapshot"]["id"]
            port = _free_port()
            release = {
                "channel": "stable",
                "available_version": "v0.0.0-test",
                "url": "https://example.invalid/release",
                "published_at": "",
            }
            with (
                mock.patch.object(web_app, "release_channel_info", return_value=release),
                mock.patch.object(web_app, "queue_release_update", return_value={"status": "queued", "target_ref": "v0.0.0-test"}),
                mock.patch.object(web_app, "fetch_v2fly_revision", return_value="remote-test-revision"),
                mock.patch.object(web_app, "prepare_v2fly_local_storage", return_value={"count": 0}),
            ):
                thread = threading.Thread(target=serve, args=(config, "127.0.0.1", port), daemon=True)
                thread.start()
                time.sleep(0.1)

                cases = [
                    ("GET", "/api/core/status", None, {}),
                    ("POST", "/api/core/strategy-discovery/start-run", {"mode": "bad", "domains": ["youtube.com"]}, {}),
                    ("POST", "/api/core/strategy-discovery/stop-current-run", {"dry_run": True}, {}),
                    ("GET", "/api/core/strategy-discovery/current-run-progress", None, {}),
                    ("GET", "/api/core/strategy-discovery/current-run-latest-log", None, {}),
                    ("GET", "/api/core/strategy-discovery/preflight", None, {}),
                    ("GET", "/api/core/presets/domain-lists", None, {}),
                    ("POST", "/api/core/presets/save-domain-list", {"kind": "user", "name": "work", "domains": ["youtube.com"]}, {}),
                    ("POST", "/api/core/presets/save-domain-list", {"kind": "user", "name": "games", "domains": ["discord.com"]}, {}),
                    ("POST", "/api/core/presets/delete-user-domain-list", {"list_ids": ["user:work", "user:games"]}, {}),
                    ("GET", "/api/core/presets/v2fly/categories", None, {}),
                    ("GET", "/api/core/presets/v2fly/category-domains?category=missing", None, {}),
                    ("POST", "/api/core/backups/create", {}, {}),
                    ("GET", "/api/core/backups/list", None, {}),
                    ("POST", "/api/core/backups/restore", {"snapshot_id": "missing"}, {}),
                    ("POST", "/api/core/backups/delete", {"snapshot_id": "missing"}, {}),
                    ("GET", f"/api/core/backups/download-file?snapshot_id={snapshot}", None, {}),
                    ("POST", "/api/core/backups/upload", b"not-a-zip", {"Content-Type": "application/zip"}),
                    ("GET", "/api/core/run-settings", None, {}),
                    ("POST", "/api/core/run-settings/save", {"curl_parallelism_default": 10, "curl_parallelism_max": 50}, {}),
                    ("GET", "/api/core/runs/history", None, {}),
                    ("GET", "/api/core/runs/latest-log", None, {}),
                    ("GET", "/api/core/strategy-candidates", None, {}),
                    ("GET", "/api/core/events", None, {}),
                    ("GET", "/api/service/status", None, {}),
                    ("GET", "/api/service/diagnostics", None, {}),
                    ("GET", "/api/service/releases/available", None, {}),
                    ("GET", "/api/service/releases/install-channel", None, {}),
                    ("POST", "/api/service/releases/set-install-channel", {"channel": "prerelease"}, {}),
                    ("POST", "/api/service/releases/install", {"channel": "stable", "dry_run": True}, {}),
                    ("GET", "/api/service/v2fly/local-storage-status", None, {}),
                    ("POST", "/api/service/v2fly/check-updates", {}, {}),
                    ("POST", "/api/service/v2fly/update-local-storage", {"dry_run": True}, {}),
                    ("GET", "/api/web/runs/history-page", None, {}),
                    ("GET", "/api/web/strategy-candidates-page", None, {}),
                    ("GET", "/api/web/events", None, {}),
                ]
                openapi = json.loads(web_app.openapi_json_bytes().decode("utf-8"))
                expected = {(method.upper(), path) for path, ops in openapi["paths"].items() for method in ops}
                requested = {(method, path.split("?", 1)[0]) for method, path, _body, _headers in cases}
                self.assertEqual(expected, requested)

                for method, path, body, headers in cases:
                    raw_body = body if isinstance(body, bytes) else (json.dumps(body).encode("utf-8") if body is not None else None)
                    request_headers = dict(headers)
                    if raw_body is not None and "Content-Type" not in request_headers:
                        request_headers["Content-Type"] = "application/json"
                    status, _response_headers, response_body = _http_request(
                        port,
                        path,
                        method=method,
                        body=raw_body,
                        headers=request_headers,
                    )
                    self.assertNotEqual(status, 404, (method, path, response_body.decode("utf-8", errors="replace")))

    def test_core_delete_user_domain_lists_requires_explicit_non_empty_ids(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            port = _free_port()
            thread = threading.Thread(target=serve, args=(config, "127.0.0.1", port), daemon=True)
            thread.start()
            time.sleep(0.1)

            status, _headers, _body = _http_request(
                port,
                "/api/core/presets/delete-user-domain-list",
                method="POST",
                body=json.dumps({"list_ids": []}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 400)

            status, _headers, _body = _http_request(
                port,
                "/api/core/presets/delete-user-lists",
                method="POST",
                body=json.dumps({"dry_run": True}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 404)

            for name in ("work", "games"):
                status, _headers, body = _http_request(
                    port,
                    "/api/core/presets/save-domain-list",
                    method="POST",
                    body=json.dumps({"kind": "user", "name": name, "domains": [f"{name}.example"]}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(status, 200, body.decode("utf-8", errors="replace"))

            status, _headers, body = _http_request(
                port,
                "/api/core/presets/delete-user-domain-list",
                method="POST",
                body=json.dumps({"list_ids": ["user:work", "user:games"]}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 200, body.decode("utf-8", errors="replace"))
            self.assertEqual({"deleted": 2}, json.loads(body.decode("utf-8")))

            status, _headers, body = _http_request(port, "/api/core/presets/domain-lists")
            self.assertEqual(status, 200)
            list_ids = [item["list_id"] for item in json.loads(body.decode("utf-8"))["lists"]]
            self.assertNotIn("user:work", list_ids)
            self.assertNotIn("user:games", list_ids)

    def test_web_proxy_serves_ui_and_forwards_api_to_core(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            core_port = _free_port()
            web_port = _free_port()
            core_thread = threading.Thread(target=serve_core, args=(config, "127.0.0.1", core_port), daemon=True)
            web_thread = threading.Thread(
                target=serve_web_proxy,
                args=(config, "127.0.0.1", web_port),
                kwargs={"core_url": f"http://127.0.0.1:{core_port}"},
                daemon=True,
            )
            core_thread.start()
            web_thread.start()
            time.sleep(0.1)

            connection = http.client.HTTPConnection("127.0.0.1", web_port, timeout=5)
            connection.request("GET", "/")
            root_response = connection.getresponse()
            root_body = root_response.read().decode("utf-8")
            connection.close()

            connection = http.client.HTTPConnection("127.0.0.1", web_port, timeout=5)
            connection.request("GET", "/api/status")
            api_response = connection.getresponse()
            api_body = api_response.read().decode("utf-8")
            connection.close()

            self.assertEqual(root_response.status, 200)
            self.assertIn("<!doctype html>", root_body.lower())
            self.assertEqual(api_response.status, 200)
            self.assertIn('"version"', api_body)
            self.assertNotIn('"web_auth"', api_body)

    def test_web_proxy_reports_bad_gateway_when_core_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            unused_core_port = _free_port()
            web_port = _free_port()
            thread = threading.Thread(
                target=serve_web_proxy,
                args=(config, "127.0.0.1", web_port),
                kwargs={"core_url": f"http://127.0.0.1:{unused_core_port}"},
                daemon=True,
            )
            thread.start()
            time.sleep(0.1)

            openapi_status, openapi_headers, openapi_body = _http_request(web_port, "/openapi.json")
            swagger_status, swagger_headers, swagger_body = _http_request(web_port, "/swagger")
            status, _, body = _http_request(web_port, "/api/status")

            self.assertEqual(openapi_status, 200)
            self.assertEqual(openapi_headers.get("content-type"), "application/json; charset=utf-8")
            self.assertEqual(json.loads(openapi_body.decode("utf-8"))["openapi"], "3.1.0")
            self.assertEqual(swagger_status, 200)
            self.assertEqual(swagger_headers.get("content-type"), "text/html; charset=utf-8")
            self.assertIn("SwaggerUIBundle", swagger_body.decode("utf-8"))
            self.assertEqual(status, 502)
            self.assertIn("core api is unavailable", body.decode("utf-8"))

    def test_deferred_web_auth_env_does_not_require_token(self) -> None:
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
                saved = response.read().decode("utf-8")
                connection.close()

                self.assertEqual(response.status, 200)
                self.assertIn('"curl_parallelism_max":17', saved)

                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request("GET", "/api/backups/restore-preview?snapshot=missing")
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

    def test_core_strategy_discovery_start_run_routes_swagger_payload(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            save_settings = web_app.save_settings
            save_settings(config, {"curl_parallelism_max": 50, "curl_parallelism_default": 10})
            port = _free_port()
            finished = threading.Event()

            def fake_run(*args: object, **kwargs: object) -> dict[str, str]:
                finished.set()
                return {"status": "success"}

            with (
                mock.patch.object(web_app, "run_multi_domain_discovery", side_effect=fake_run) as runner,
                mock.patch.object(web_app, "create_snapshot_if_idle", return_value={}),
            ):
                thread = threading.Thread(target=serve, args=(config, "127.0.0.1", port), daemon=True)
                thread.start()
                time.sleep(0.1)

                body = json.dumps(
                    {
                        "mode": "multi_domain",
                        "domains": ["youtube.com", "discord.com", "airhorn.solutions"],
                        "protocols": ["tcp", "quic"],
                        "curl_parallelism": 30,
                        "timeout_seconds": 172800,
                        "settings": {
                            "curl_max_time": 7,
                            "curl_max_time_quic": 7,
                            "enable_ipv6": False,
                        },
                        "mode_settings": {
                            "common_strategy_min_domains": 2,
                        },
                    }
                )
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request(
                    "POST",
                    "/api/core/strategy-discovery/start-run",
                    body=body,
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                )
                response = connection.getresponse()
                raw_response = response.read().decode("utf-8")
                connection.close()

                self.assertEqual(response.status, 202)
                accepted = json.loads(raw_response)
                self.assertTrue(accepted["accepted"])
                self.assertTrue(accepted["run_id"])
                self.assertEqual("queued", accepted["status"])
                self.assertTrue(finished.wait(2))
                self.assertEqual(30, runner.call_args.kwargs["curl_parallelism"])
                self.assertEqual(7, runner.call_args.kwargs["curl_max_time"])
                self.assertEqual(7, runner.call_args.kwargs["curl_max_time_quic"])
                self.assertFalse(runner.call_args.kwargs["enable_ipv6"])
                self.assertTrue(runner.call_args.kwargs["include_quic"])
                self.assertTrue(runner.call_args.kwargs["enable_tls12"])
                deadline = time.time() + 2
                while time.time() < deadline:
                    if read_state(config.output.state_dir).get("current_job") is None:
                        break
                    time.sleep(0.02)

    def test_core_strategy_discovery_start_run_rejects_unknown_protocols(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported protocols"):
            web_app._core_strategy_discovery_job_payload(
                {
                    "mode": "multi_domain",
                    "domains": ["youtube.com"],
                    "protocols": ["tcp", "bad"],
                }
            )

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


def _http_request(
    port: int,
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    headers = {key.lower(): value for key, value in response.getheaders()}
    body = response.read()
    connection.close()
    return response.status, headers, body


if __name__ == "__main__":
    unittest.main()
