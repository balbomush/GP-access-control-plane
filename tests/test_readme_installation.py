from __future__ import annotations

import re
import unittest
from pathlib import Path


class ReadmeInstallationTests(unittest.TestCase):
    def test_installation_commands_require_an_exact_release_tag(self) -> None:
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        bootstrap = (root / "scripts" / "bootstrap-linux.sh").read_text(encoding="utf-8")

        self.assertNotIn("/raw/", readme)
        self.assertNotIn('GP_BRANCH="${GP_BRANCH:-latest-stable}"', readme)
        self.assertIn('GP_BRANCH=v0.4.0', readme)
        self.assertNotIn('GP_INSTALL_CONFIG', readme)
        self.assertNotIn('GP_STATE_DIR', readme)
        self.assertNotIn('v2fly/domain-list-community', readme)
        self.assertIn('только стандартный путь `$HOME/gp/GP-access-control-plane/build/state`', readme)
        self.assertIn('не входит в scope этой миграции', readme)
        self.assertNotIn('latest-stable', readme)
        self.assertIn('TAG="${GP_BRANCH:-}"', bootstrap)
        self.assertIn('exact release tag vX.Y.Z', bootstrap)


if __name__ == "__main__":
    unittest.main()
