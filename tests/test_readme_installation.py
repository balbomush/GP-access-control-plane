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
        stable_blocks = [
            block
            for block in re.findall(r"```bash\n(.*?)```", readme, flags=re.DOTALL)
            if re.search(rf"GP_BRANCH={re.escape(expected_tag)}(?:\s|$)", block)
        ]
        alpha_tag = f"{expected_tag}-alpha.1"
        alpha_url = (
            "https://github.com/balbomush/GP-access-control-plane/releases/download/"
            f"{alpha_tag}/bootstrap-linux.sh"
        )
        alpha_blocks = [
            block
            for block in re.findall(r"```bash\n(.*?)```", readme, flags=re.DOTALL)
            if f"GP_BRANCH={alpha_tag}" in block
        ]

        self.assertNotIn("/raw/", readme)
        self.assertNotIn('GP_BRANCH="${GP_BRANCH:-latest-stable}"', readme)
        self.assertGreaterEqual(len(stable_blocks), 4)
        for block in stable_blocks:
            with self.subTest(block=block):
                self.assertIn(f"GP_BOOTSTRAP_URL='{expected_url}'", block)
                self.assertIn(f"GP_BRANCH={expected_tag}", block)
        self.assertEqual(len(alpha_blocks), 1)
        self.assertIn(f"GP_BOOTSTRAP_URL='{alpha_url}'", alpha_blocks[0])
        self.assertIn(f"GP_BRANCH={alpha_tag}", alpha_blocks[0])
        self.assertIn("Переход alpha → stable и rollback не поддерживаются", readme)
        self.assertNotIn('GP_INSTALL_CONFIG', readme)
        self.assertNotIn('GP_STATE_DIR', readme)
        self.assertNotIn('v2fly/domain-list-community', readme)
        self.assertIn('только стандартный путь `$HOME/gp/GP-access-control-plane/build/state`', readme)
        self.assertIn('не входит в scope этой миграции', readme)
        self.assertNotIn('latest-stable', readme)
        self.assertIn('TAG="${GP_BRANCH:-}"', bootstrap)
        self.assertIn('exact release tag vX.Y.Z or vX.Y.Z-alpha.N', bootstrap)
        self.assertIn('автоматически восстанавливает vault', readme)
        self.assertIn('аварийным путём', readme)

    def test_release_documentation_keeps_installation_outside_web_and_api(self) -> None:
        root = Path(__file__).resolve().parents[1]
        document = (root / "docs" / "headless-runtime-core-api.md").read_text(encoding="utf-8")

        self.assertEqual(1, document.count("GET /api/service/releases/available"))
        self.assertIn("exact annotated\nrelease tag в `GP_BRANCH`", document)
        self.assertIn("`apt`/zapret2/wrapper preparation, кладёт fresh files и создаёт venv", document)
        self.assertIn("до `domain-sources prepare-v2fly` и до любого `systemctl enable --now`", document)
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
