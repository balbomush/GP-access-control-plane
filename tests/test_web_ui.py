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
        self.assertIn("Задания подбора", html)
        self.assertIn("data-tab=\"candidates\"", html)
        self.assertIn("data-tab=\"terminal\"", html)
        self.assertIn("candidate-filter", html)
        self.assertIn("candidateGroups(rows)", html)
        self.assertIn("domain-group", html)
        self.assertIn("protocol-group", html)
        self.assertIn("strategy-list", html)
        self.assertIn("data-copy-candidate-group", html)
        self.assertIn("Копировать группу", html)
        self.assertIn("copy-fallback", html)
        self.assertIn("showCopyFallback", html)
        self.assertIn("Терминал", html)
        self.assertIn("scrollLogToBottom", html)
        self.assertIn("runSummary(row)", html)
        self.assertIn("jobSummary(row)", html)
        self.assertIn("effectiveJobStatus(row)", html)
        self.assertIn("latestById", html)
        self.assertNotIn("{label: 'Лог'", html)
        self.assertNotIn("{label: 'Детали'", html)
        self.assertNotIn("JSON.stringify(row.result)", html)
        self.assertNotIn("data-candidate-verify", html)
        self.assertNotIn("<code>nfqws2", html)
        self.assertNotIn("{label: 'ID'", html)
        self.assertNotIn("{label: 'Найдено'", html)
        self.assertNotIn("Синхронизировать", html)
        self.assertNotIn("dry-run", html)
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
