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


_HTML_SHA256 = "14744d583d5ec82157f1fcf111e384151d281b41463730a1f2ac94fda87522a5"
_CSS_SHA256 = "06f9628dcb7bf57f8a31cb61ed2d09fdfee550c706a1bb154fdba948463a2825"
_SCRIPT_SHA256 = "5ce28aae08e717f072cf457f946612105be5ca97fbf87ccc8975a4e113c7ecc0"


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
        script = ui_assets._read_resource("scripts/legacy-runtime.js")
        self.assertEqual(_CSS_SHA256, hashlib.sha256(css.encode("utf-8")).hexdigest())
        self.assertEqual(_SCRIPT_SHA256, hashlib.sha256(script.encode("utf-8")).hexdigest())
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
