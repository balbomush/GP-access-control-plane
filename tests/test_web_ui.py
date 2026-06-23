from __future__ import annotations

import http.client
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.config import AppConfig, HealthcheckConfig, LocalConfig, OutputConfig, RepoConfig
from gp_control_plane.web.app import index_html, serve


class WebUiTests(unittest.TestCase):
    def test_index_html_is_focused_on_strategy_finder_only(self) -> None:
        html = index_html()

        self.assertIn("Подбор стратегий zapret2", html)
        self.assertIn("Запуск поиска", html)
        self.assertIn("Найденные стратегии", html)
        self.assertIn("История запусков", html)
        self.assertIn("data-tab=\"candidates\"", html)
        self.assertIn("data-tab=\"terminal\"", html)
        self.assertIn("candidate-filter", html)
        self.assertIn("candidateGroups(rows)", html)
        self.assertIn("data-candidate-view=\"domain\"", html)
        self.assertIn("data-candidate-view=\"common\"", html)
        self.assertIn("domain-group", html)
        self.assertIn("protocol-group", html)
        self.assertIn("strategy-list", html)
        self.assertIn("domain-strategy-box", html)
        self.assertIn("strategy-textarea", html)
        self.assertIn("strategyText", html)
        self.assertIn("data-copy-scope", html)
        self.assertIn("data-copy-candidate-id", html)
        self.assertIn("Копировать группу", html)
        self.assertIn("Копировать стратегию", html)
        self.assertIn("copyTextForButton", html)
        self.assertIn("dynamicCommonRows", html)
        self.assertIn("selectedFinderDomains", html)
        self.assertIn("selectedCommonDomains", html)
        self.assertIn("common-controls", html)
        self.assertIn("common-domains", html)
        self.assertIn("tested-domain-options", html)
        self.assertIn("common-domain-add", html)
        self.assertIn("finder-preset-select", html)
        self.assertIn("common-preset-select", html)
        self.assertIn("data-preset-use=\"finder\"", html)
        self.assertIn("data-preset-save=\"common\"", html)
        self.assertIn("CUSTOM_PRESETS_KEY", html)
        self.assertIn("localStorage", html)
        self.assertIn("testedDomains()", html)
        self.assertIn("domainsTouched", html)
        self.assertIn("domainsInitialized", html)
        self.assertIn("copy-fallback", html)
        self.assertIn("id=\"toast\"", html)
        self.assertIn("showToast", html)
        self.assertIn("showCopyFallback", html)
        self.assertIn("progress-fill", html)
        self.assertIn("progress-attempted", html)
        self.assertIn("progress-successful", html)
        self.assertIn("attempt_total", html)
        self.assertIn("eta_estimate_ms_per_attempt", html)
        self.assertIn("runCandidateCount(row)", html)
        self.assertIn("runProgressText(row)", html)
        self.assertIn("data-action=\"multi-domain-discovery\"", html)
        self.assertIn("/api/jobs/zapret-multi-domain-discovery", html)
        self.assertIn("curl-parallelism", html)
        self.assertIn("max=\"10\"", html)
        self.assertIn("value=\"4\"", html)
        self.assertIn("curlParallelism()", html)
        self.assertIn("curl_parallelism", html)
        self.assertIn("limit-time-enabled", html)
        self.assertIn("time-limit-field", html)
        self.assertIn("timeoutSecondsOrNull", html)
        self.assertIn("title=\"Запускает штатный blockcheck2", html)
        self.assertIn("title=\"Экспериментальный режим", html)
        self.assertIn("isDiscoveryRun(row)", html)
        self.assertIn("runMode(row)", html)
        self.assertIn("data-action=\"stop-current\"", html)
        self.assertIn("Терминал", html)
        self.assertIn("scrollLogToBottom", html)
        self.assertIn("runSummary(row)", html)
        self.assertIn("latestById", html)
        self.assertNotIn("Задания подбора", html)
        self.assertNotIn("Запуски с находками", html)
        self.assertNotIn("candidate-runs-table", html)
        self.assertNotIn("jobs-table", html)
        self.assertNotIn("jobSummary(row)", html)
        self.assertNotIn("effectiveJobStatus(row)", html)
        self.assertNotIn("{label: 'Лог'", html)
        self.assertNotIn("{label: 'Детали'", html)
        self.assertNotIn("JSON.stringify(row.result)", html)
        self.assertNotIn("data-candidate-verify", html)
        self.assertNotIn("candidateCopyGroups", html)
        self.assertNotIn("registerCopyText", html)
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

    def test_head_root_returns_ok_for_curl_i(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            rules_repo = tmp / "rules"
            stable = rules_repo / "stable"
            stable.mkdir(parents=True)
            for name in ("direct.yaml", "vpn.yaml", "zapret.yaml"):
                (stable / name).write_text("version: 1\nrules: []\n", encoding="utf-8")
            strategies_repo = tmp / "strategies"
            strategies_repo.mkdir()
            config = AppConfig(
                repos=RepoConfig(rules=rules_repo, strategies=strategies_repo),
                local=LocalConfig(
                    overrides=tmp / "local-overrides.yaml",
                    devices=tmp / "devices.yaml",
                    selected_strategy=tmp / "selected-strategy.yaml",
                ),
                output=OutputConfig(
                    rendered_dir=tmp / "rendered",
                    evidence_dir=tmp / "evidence",
                    state_dir=tmp / "state",
                ),
                healthcheck=HealthcheckConfig(),
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


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    unittest.main()
