from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path


class ReadmeInstallationTests(unittest.TestCase):
    def test_installation_commands_require_an_exact_release_tag(self) -> None:
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        bootstrap = (root / "scripts" / "bootstrap-linux.sh").read_text(encoding="utf-8")
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        expected_tag = f"v{project['project']['version']}"
        expected_url = (
            "https://github.com/balbomush/GP-access-control-plane/releases/download/"
            f"{expected_tag}/bootstrap-linux.sh"
        )
        bootstrap_blocks = [
            block
            for block in re.findall(r"```bash\n(.*?)```", readme, flags=re.DOTALL)
            if "bootstrap-linux.sh" in block or "GP_BRANCH" in block
        ]

        self.assertNotIn("/raw/", readme)
        self.assertNotIn('GP_BRANCH="${GP_BRANCH:-latest-stable}"', readme)
        self.assertGreaterEqual(len(bootstrap_blocks), 4)
        for block in bootstrap_blocks:
            with self.subTest(block=block):
                self.assertIn(f"GP_BOOTSTRAP_URL='{expected_url}'", block)
                self.assertIn(f"GP_BRANCH={expected_tag}", block)
        self.assertNotIn('GP_INSTALL_CONFIG', readme)
        self.assertNotIn('GP_STATE_DIR', readme)
        self.assertNotIn('v2fly/domain-list-community', readme)
        self.assertIn('только стандартный путь `$HOME/gp/GP-access-control-plane/build/state`', readme)
        self.assertIn('не входит в scope этой миграции', readme)
        self.assertNotIn('latest-stable', readme)
        self.assertIn('TAG="${GP_BRANCH:-}"', bootstrap)
        self.assertIn('exact release tag vX.Y.Z', bootstrap)

    def test_release_documentation_keeps_installation_outside_web_and_api(self) -> None:
        root = Path(__file__).resolve().parents[1]
        document = (root / "docs" / "headless-runtime-core-api.md").read_text(encoding="utf-8")

        self.assertEqual(1, document.count("GET /api/service/releases/available"))
        self.assertIn("exact annotated\nrelease tag в `GP_BRANCH`", document)
        for retired_operation in (
            "GET /api/service/releases/install-channel",
            "POST /api/service/releases/set-install-channel",
            "GET /api/service/releases/install-plan",
            "POST /api/service/releases/install",
        ):
            with self.subTest(retired_operation=retired_operation):
                self.assertNotIn(retired_operation, document)


if __name__ == "__main__":
    unittest.main()
