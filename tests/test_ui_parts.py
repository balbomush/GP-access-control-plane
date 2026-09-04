"""Structure guards for the split GP web UI package.

The former single-file `web/ui.py` was split into `web/ui/parts/**` (real
.css/.js/.html files, each <=500 physical lines) plus thin Python glue. These
tests keep every file under the size cap and verify the assembled document is
deterministic and follows `PART_ORDER`.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

import pytest

from gp_control_plane.web.ui import index_html
from gp_control_plane.web.ui.parts import PART_ORDER

_UI_DIR = Path(__file__).resolve().parents[1] / "src" / "gp_control_plane" / "web" / "ui"
_PART_LIMIT = 500


pytestmark = pytest.mark.quality
def _physical_lines(text: str) -> int:
    if text == "":
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


class UiPartsStructureTests(unittest.TestCase):
    def test_every_part_file_is_within_line_limit(self) -> None:
        for rel in PART_ORDER:
            content = (_UI_DIR / "parts" / rel).read_text(encoding="utf-8")
            self.assertLessEqual(
                _physical_lines(content),
                _PART_LIMIT,
                f"part {rel} exceeds {_PART_LIMIT} physical lines",
            )

    def test_ui_package_python_files_are_within_line_limit(self) -> None:
        py_files = sorted(p for p in _UI_DIR.rglob("*.py"))
        self.assertTrue(py_files, "no python files found under web/ui")
        for path in py_files:
            self.assertLessEqual(
                _physical_lines(path.read_text(encoding="utf-8")),
                _PART_LIMIT,
                f"{path.relative_to(_UI_DIR)} exceeds {_PART_LIMIT} physical lines",
            )

    def test_no_oversized_leftovers_under_ui_package(self) -> None:
        oversized: list[str] = []
        for path in _UI_DIR.rglob("*"):
            if path.is_dir():
                continue
            suffix = path.suffix
            if suffix not in {".py", ".css", ".js", ".html"}:
                continue
            lines = _physical_lines(path.read_text(encoding="utf-8"))
            if lines > _PART_LIMIT:
                oversized.append(f"{path.relative_to(_UI_DIR)} ({lines})")
        self.assertEqual([], oversized)

    def test_assembled_document_is_deterministic(self) -> None:
        first = index_html()
        second = index_html()
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("<!doctype html>"))
        self.assertIn("<script>", first)
        self.assertIn("</style>", first)

    def test_assembly_follows_part_order(self) -> None:
        root = Path(importlib.util.find_spec("gp_control_plane.web.ui.parts").origin).parent
        joined = "".join((root / rel).read_text(encoding="utf-8") for rel in PART_ORDER)
        self.assertEqual(joined, index_html())

    def test_js_parts_start_and_end_on_top_level_statements(self) -> None:
        import re

        start_pat = re.compile(
            r"^(?:async\s+)?function\s+\w+\s*\(|"
            r"^(?:const|let|var)\s+\w+\s*=|"
            r"^document\.addEventListener\(|^if\s*\(|^else\b"
        )
        js_dir = _UI_DIR / "parts" / "js"
        js_files = sorted(js_dir.glob("*.js"))
        self.assertTrue(js_files, "no js part files found")
        for path in js_files:
            lines = path.read_text(encoding="utf-8").split("\n")
            nonblank = [i for i, l in enumerate(lines) if l.strip()]
            if not nonblank:
                continue
            first = lines[nonblank[0]].lstrip()
            last = lines[nonblank[-1]].rstrip()
            self.assertRegex(first, start_pat, f"{path.name}: not a top-level statement start")
            self.assertTrue(
                last.endswith(";") or last.endswith("}"),
                f"{path.name}: does not end on a statement boundary",
            )

    def test_document_has_single_script_and_style_blocks(self) -> None:
        html = index_html()
        self.assertEqual(html.count("<script>"), 1)
        self.assertEqual(html.count("</script>"), 1)
        self.assertEqual(html.count("<style>"), 1)
        self.assertEqual(html.count("</style>"), 1)


if __name__ == "__main__":
    unittest.main()

