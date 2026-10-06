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


_HTML_SHA256 = "247e232757e6a6937f79a232c0d9626774db2ab69ece27491874ff16331133d7"
_CSS_SHA256 = "06f9628dcb7bf57f8a31cb61ed2d09fdfee550c706a1bb154fdba948463a2825"
_SCRIPT_SHA256S = {
    "api-client.js": "9974e9abcdd283ff36f63c8b96c36ba11221bebc789963461ff282d5aa250203",
    "run-state.js": "7f5ce0f81e9559aa83593fb844d8398342f070c80645a2d5a30d5295d02a4c4a",
    "realtime-controller.js": "2baf5be952f73cd4a9e1a44542cfb6d39ee94911da33dfc2534244b75ac30d54",
    "session-controller.js": "1caf688ea2d6601e0858b6b418884e26b1bd5c748631d882dd648d715ca4c1b8",
    "legacy-runtime.js": "2ca2f6912e4579897df317f5a880672705f36bf075104c7e9762491eac4749f8",
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
