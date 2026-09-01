from __future__ import annotations

import json
import re
import sys
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gp_control_plane import __version__


class VersionMetadataTests(unittest.TestCase):
    def test_runtime_package_and_openapi_versions_are_synchronized(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        contract = json.loads((ROOT / "openapi.json").read_text(encoding="utf-8"))

        self.assertEqual(__version__, project["project"]["version"])
        self.assertEqual(__version__, contract["info"]["version"])

    def test_current_openapi_examples_match_the_current_release(self) -> None:
        contract = json.loads((ROOT / "openapi.json").read_text(encoding="utf-8"))
        examples = contract["components"]["examples"]

        self.assertIn(f'\"version\":\"{__version__}\"', examples["WebEventsStream"]["value"])
        self.assertEqual(__version__, examples["ServiceStatus"]["value"]["version"]["version"])
        self.assertEqual(f"v{__version__}", examples["ServiceStatus"]["value"]["version"]["installed_ref"])

        available = examples["AvailableReleases"]["value"]
        self.assertEqual(__version__, available["current"]["version"])
        self.assertEqual(f"v{__version__}", available["current"]["installed_ref"])
        status_commit = examples["ServiceStatus"]["value"]["version"]["commit"]
        available_commit = available["current"]["commit"]
        self.assertEqual(status_commit, available_commit)
        self.assertIsNotNone(re.fullmatch(r"[0-9a-f]{40}", status_commit))


if __name__ == "__main__":
    unittest.main()
