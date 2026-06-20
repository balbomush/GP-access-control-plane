from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.web.app import index_html


class WebUiTests(unittest.TestCase):
    def test_index_html_has_human_friendly_dashboard_sections(self) -> None:
        html = index_html()

        self.assertIn("Панель доступа", html)
        self.assertIn("Управление", html)
        self.assertIn("Проверка домена", html)
        self.assertIn("Стратегии zapret", html)
        self.assertIn("Журнал заданий", html)
        self.assertIn("Проверки доступности", html)
        self.assertIn("Технические данные", html)


if __name__ == "__main__":
    unittest.main()
