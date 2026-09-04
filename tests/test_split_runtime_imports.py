from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


pytestmark = pytest.mark.quality
class SplitRuntimeImportTests(unittest.TestCase):
    def test_importing_core_server_does_not_load_web_app(self) -> None:
        code = """
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "src"))
import gp_control_plane.web.core_server
blocked = [
    name for name in (
        "gp_control_plane.web.app",
        "gp_control_plane.web.proxy",
        "gp_control_plane.web.ui",
    )
    if name in sys.modules
]
if blocked:
    raise SystemExit("unexpected imports: " + ",".join(blocked))
"""
        self._run_clean_python(code)

    def test_serve_core_uses_api_server_without_importing_web_app(self) -> None:
        code = """
import importlib.abc
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "src"))

from gp_control_plane.config import AppConfig, OutputConfig
from gp_control_plane.web import api_server, core_server

class BlockWebAppImport(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "gp_control_plane.web.app":
            raise ImportError("web.app must not be imported by core runtime")
        return None

class FakeServer:
    def __init__(self, address, handler):
        self.address = address
        self.handler = handler
    def serve_forever(self):
        print("fake-server-started")

sys.meta_path.insert(0, BlockWebAppImport())
api_server.ThreadingHTTPServer = FakeServer
with tempfile.TemporaryDirectory() as raw:
    config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
    core_server.serve_core(config, host="127.0.0.1", port=18081)
if "gp_control_plane.web.app" in sys.modules:
    raise SystemExit("web.app was imported")
if "gp_control_plane.web.api_server" not in sys.modules:
    raise SystemExit("api_server was not imported")
"""
        self._run_clean_python(code)

    def test_core_server_source_has_no_legacy_app_import(self) -> None:
        tree = ast.parse((ROOT / "src/gp_control_plane/web/core_server.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                self.assertNotIn(module, {".app", "gp_control_plane.web.app", "gp_control_plane.web"})
                if module == "gp_control_plane.web":
                    self.assertNotIn("app", {alias.name for alias in node.names})
            if isinstance(node, ast.Import):
                self.assertNotIn("gp_control_plane.web.app", {alias.name for alias in node.names})

    def _run_clean_python(self, code: str) -> None:
        result = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(code)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            self.fail(f"subprocess failed\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")


if __name__ == "__main__":
    unittest.main()

