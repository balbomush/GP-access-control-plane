from __future__ import annotations

import re
import unittest
from pathlib import Path


class ReadmeInstallationTests(unittest.TestCase):
    def test_installation_commands_use_latest_release_assets_and_a_branch_variable(self) -> None:
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        bootstrap = (root / "scripts" / "bootstrap-linux.sh").read_text(encoding="utf-8")

        self.assertNotIn("/raw/", readme)
        self.assertNotRegex(readme, r"/releases/download/v[0-9][^/\s]*/")
        self.assertIn(
            "/releases/latest/download/bootstrap-linux.sh",
            readme,
        )
        self.assertIn(
            "/releases/latest/download/install-zapret2.sh",
            readme,
        )
        self.assertIn('GP_BRANCH="${GP_BRANCH:-latest-stable}"', readme)
        self.assertIn('INSTALL_REF="${GP_BRANCH:-latest-stable}"', bootstrap)


if __name__ == "__main__":
    unittest.main()
