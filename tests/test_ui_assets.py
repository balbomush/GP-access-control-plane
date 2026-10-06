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


_HTML_SHA256 = "8f2ae7a382f13158be960243fd926a0086b7c63a6bc6c5786618174ae96804ea"
_CSS_SHA256 = "06f9628dcb7bf57f8a31cb61ed2d09fdfee550c706a1bb154fdba948463a2825"
_SCRIPT_SHA256S = {
    "api-client.js": "9974e9abcdd283ff36f63c8b96c36ba11221bebc789963461ff282d5aa250203",
    "run-state.js": "9bdfb3c56a9bbfd2e7e74482e488a48923ee2aa019bf6cadc1962b851521d3d6",
    "realtime-controller.js": "2baf5be952f73cd4a9e1a44542cfb6d39ee94911da33dfc2534244b75ac30d54",
    "session-controller.js": "1caf688ea2d6601e0858b6b418884e26b1bd5c748631d882dd648d715ca4c1b8",
    "ui-lifetime.js": "ec3a0471def1397efe88ee2a2cfa8daf53ad09d40a048b14a51866f2349963fe",
    "releases-controller.js": "459dbc8034b259d5513c6e0d37e11af8267b6055f5feb9bfb762c35a77eb12f0",
    "backups-controller.js": "25d8b60e9266fd49b3f26f608a283b830ba1b14dd8ccf7f4ed09b8a15eac33eb",
    "settings-controller.js": "d18bb4cbed13f98b98903ece9eef9e187625122a1bc85e14c4ceacf9dad50f70",
    "history-controller.js": "185d7d70306d1fa30fe2ed85e87c6197fce1c71cf5c1791012a36d00b27bd7b4",
    "terminal-controller.js": "adf3c18770437d9c58a7aad430d7a31934beb73286edf3f1792d0c468827affc",
    "finder-controller.js": "230f56afb22f9e0c01ea67072f3b49173064489b981554ef2beea640c1671b89",
    "presets-controller.js": "5b4128f4653fa746db82ea4108f4f15f0300707fb9bc552f2f2690c1f8aa39e6",
    "candidate-selection.js": "cf7048a5fe74da043c5ebde8f535660801b50d88a6ce80e4244efd37c23684b5",
    "candidates-controller.js": "c122efa881191d59a979e3cdc0e829dcd5f6f3d6fbfb01586acd24b4da11a696",
    "status-view-controller.js": "0787d06153c82b30df796a4da84a5828ecbb74aa2ecbe5ae1159cd4902834798",
    "app-shell.js": "0112e08fb2e47366a3b15d7333e8ceaa991b86473d7956c5045bc66af344ecac",
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
