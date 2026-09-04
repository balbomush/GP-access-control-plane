"""Repo-wide guard: no source .py file may exceed the size budget.

A project rule keeps every Python file under src/gp_control_plane/ (and any
other .py shipped under src/) at <=500 physical lines. Large modules are split
into same-named packages whose __init__ re-exports the public surface
(e.g. strategy_finder -> engine_common/bc2_engine/bs_engine; storage.py ->
storage/; web/api_server.py -> web/api_server/).
"""
from __future__ import annotations

import unittest
from pathlib import Path

import pytest

_SRC_DIR = Path(__file__).resolve().parents[1] / "src"
_LIMIT = 500


pytestmark = pytest.mark.quality
class SrcLineLimitTests(unittest.TestCase):
    def _iter_py(self):
        return sorted(p for p in _SRC_DIR.rglob("*.py") if p.is_file())

    def test_every_src_python_file_is_within_line_limit(self) -> None:
        over = []
        for path in self._iter_py():
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) > _LIMIT:
                over.append(f"{path.relative_to(_SRC_DIR.parent)} ({len(lines)})")
        self.assertEqual([], over, "source files over the line limit")

    def test_source_python_files_exist(self) -> None:
        self.assertTrue(self._iter_py(), "no python files found under src")


if __name__ == "__main__":
    unittest.main()

