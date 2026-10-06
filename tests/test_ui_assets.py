from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.web.ui import index_html
from gp_control_plane.web import ui_assets


_HTML_SHA256 = "f9087ccd62fce2b93af4db8e0bb2bfba026eb659b84cc8cb47a8de1c569662e5"
_CSS_SHA256 = "06f9628dcb7bf57f8a31cb61ed2d09fdfee550c706a1bb154fdba948463a2825"
_SCRIPT_SHA256S = {
    "api-client.js": "9974e9abcdd283ff36f63c8b96c36ba11221bebc789963461ff282d5aa250203",
    "run-state.js": "9bdfb3c56a9bbfd2e7e74482e488a48923ee2aa019bf6cadc1962b851521d3d6",
    "realtime-controller.js": "2baf5be952f73cd4a9e1a44542cfb6d39ee94911da33dfc2534244b75ac30d54",
    "session-controller.js": "1caf688ea2d6601e0858b6b418884e26b1bd5c748631d882dd648d715ca4c1b8",
    "ui-lifetime.js": "82b4b08257bb1dcb5ce702a80c3b0918a66dda958310986be8036a578a1c506e",
    "releases-controller.js": "270523c1303ff523e1a63bacc12b552611cb834fcaeab6f58f5d4623cb043dec",
    "backups-controller.js": "78dfd9eeeda9a21129e3e440ca3fa99c7b123b2af1926e51e0b58c23b17948cf",
    "settings-controller.js": "77eb6a262327de37e608c826fd8125f323019f4f76231004485e198972ca87a9",
    "history-controller.js": "7aeb17c3a6c279d88f801c5768e3b923cdac5a7480eca48f2e4b3452868f24cb",
    "terminal-controller.js": "e01b9ff6757f512b0b437738c1357231e5008289dc67b3c0168c36fa4b041872",
    "finder-controller.js": "5908cd7dc454e041f5ca78f6a069f76473fd0969c7cbc7fddc5aae423dd72fe3",
    "presets-controller.js": "5bd1bde860f2a7b987db75d76f93f63a220545284a9e79609aa21fb4f0049233",
    "candidate-selection.js": "cf7048a5fe74da043c5ebde8f535660801b50d88a6ce80e4244efd37c23684b5",
    "candidates-controller.js": "a4adeffc8c5a06e7b1b4b488d9f3178c20d772faf5009c5590f3330abf086740",
    "status-view-controller.js": "c0d570f36d63fe57fa283bc67b028467ab0856d80d257ff61564bfbedd702362",
    "app-shell.js": "7f629daefe29d51e7d0fa44548be5c469a29f61e4de81fa3975a0f887d37d5de",
    "legacy-runtime.js": "a91e247b9accb351f42549994cb86fca6fc87a5f3add4e0ff1bbc9a26d8eca5c",
}


class UiAssetsTests(unittest.TestCase):
    def tearDown(self) -> None:
        ui_assets.render_index_html.cache_clear()

    def test_renderer_preserves_fixed_baseline_bytes_outside_source_cwd(self) -> None:
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary_directory:
            os.chdir(temporary_directory)
            try:
                ui_assets.render_index_html.cache_clear()
                html = index_html()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(_HTML_SHA256, hashlib.sha256(html.encode("utf-8")).hexdigest())
        self.assertEqual(html, ui_assets.render_index_html())
        css = ui_assets._read_resource("styles/app.css")
        self.assertEqual(tuple(_SCRIPT_SHA256S), ui_assets.SCRIPT_RESOURCES)
        scripts = [ui_assets._read_resource(f"scripts/{name}") for name in _SCRIPT_SHA256S]
        script = "\n".join(scripts)
        self.assertEqual(_CSS_SHA256, hashlib.sha256(css.encode("utf-8")).hexdigest())
        for name, expected_hash in _SCRIPT_SHA256S.items():
            self.assertEqual(expected_hash, hashlib.sha256(ui_assets._read_resource(f"scripts/{name}").encode("utf-8")).hexdigest())
        self.assertIn("<style>" + css + "</style>", html)
        self.assertIn("<script>" + script + "</script>", html)

    def test_renderer_uses_one_lazy_page_cache(self) -> None:
        ui_assets.render_index_html.cache_clear()
        with patch.object(ui_assets, "_read_resource", wraps=ui_assets._read_resource) as read_resource:
            first = ui_assets.render_index_html()
            first_calls = read_resource.call_count
            second = ui_assets.render_index_html()

        self.assertIs(first, second)
        self.assertGreater(first_calls, 1)
        self.assertEqual(first_calls, read_resource.call_count)

    def test_missing_required_resource_is_explicit_delivery_error(self) -> None:
        ui_assets.render_index_html.cache_clear()
        with patch.object(ui_assets, "files", side_effect=FileNotFoundError("missing package data")):
            with self.assertRaisesRegex(ui_assets.UiAssetError, r"required UI resource is missing: templates/tabs/finder.html"):
                ui_assets.render_index_html()
