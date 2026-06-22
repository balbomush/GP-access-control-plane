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
    def test_index_html_has_human_friendly_dashboard_sections(self) -> None:
        html = index_html()

        self.assertIn("Панель доступа", html)
        self.assertIn("Управление", html)
        self.assertIn("Проверка домена", html)
        self.assertIn("Подбор стратегий", html)
        self.assertIn("Найденные стратегии", html)
        self.assertIn("Запуски подбора", html)
        self.assertIn("Текущий лог подбора", html)
        self.assertIn("Стратегии zapret", html)
        self.assertIn("Журнал заданий", html)
        self.assertIn("Проверки доступности", html)
        self.assertIn("Технические данные", html)

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
