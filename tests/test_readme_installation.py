from __future__ import annotations

import re
import unittest
from pathlib import Path


class ReadmeInstallationTests(unittest.TestCase):
    # The fork tracks the upstream v0.4.1 line while shipping a `-fork` runtime
    # version, so the documentation install commands keep naming the real
    # released tag the fork is based on rather than the pyproject version.
    STABLE_TAG = "v0.4.1"
    ALPHA_TAG = "v0.4.1-alpha.1"
    RELEASE_ROOT = "https://github.com/balbomush/GP-access-control-plane/releases/download"

    def test_installation_commands_require_an_exact_release_tag(self) -> None:
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        bootstrap = (root / "scripts" / "bootstrap-linux.sh").read_text(encoding="utf-8")

        stable_url = f"{self.RELEASE_ROOT}/{self.STABLE_TAG}/bootstrap-linux.sh"
        stable_blocks = [
            block
            for block in re.findall(r"```bash\n(.*?)```", readme, flags=re.DOTALL)
            if re.search(rf"GP_BRANCH={re.escape(self.STABLE_TAG)}(?:\s|$)", block)
        ]
        alpha_url = f"{self.RELEASE_ROOT}/{self.ALPHA_TAG}/bootstrap-linux.sh"
        alpha_blocks = [
            block
            for block in re.findall(r"```bash\n(.*?)```", readme, flags=re.DOTALL)
            if f"GP_BRANCH={self.ALPHA_TAG}" in block
        ]

        self.assertNotIn("/raw/", readme)
        self.assertNotIn('GP_BRANCH="${GP_BRANCH:-latest-stable}"', readme)
        self.assertGreaterEqual(len(stable_blocks), 4)
        for block in stable_blocks:
            with self.subTest(block=block):
                self.assertIn(f"GP_BOOTSTRAP_URL='{stable_url}'", block)
                self.assertIn(f"GP_BRANCH={self.STABLE_TAG}", block)
        self.assertEqual(len(alpha_blocks), 1)
        self.assertIn(f"GP_BOOTSTRAP_URL='{alpha_url}'", alpha_blocks[0])
        self.assertIn(f"GP_BRANCH={self.ALPHA_TAG}", alpha_blocks[0])
        self.assertIn("Переход alpha → stable и rollback не поддерживаются", readme)
        self.assertNotIn('GP_INSTALL_CONFIG', readme)
        self.assertNotIn('GP_STATE_DIR', readme)
        self.assertNotIn('v2fly/domain-list-community', readme)
        self.assertIn('один из двух стандартных путей: `$HOME/gp/GP-access-control-plane/build/state` или `$HOME/gp/.GP-access-control-plane.data/state`', readme)
        self.assertIn('При наличии обоих bootstrap останавливается до `sudo`', readme)
        self.assertIn('не входит в scope этой миграции', readme)
        self.assertNotIn('latest-stable', readme)
        self.assertIn('TAG="${GP_BRANCH:-}"', bootstrap)
        self.assertIn('exact release tag vX.Y.Z or vX.Y.Z-alpha.N', bootstrap)
        self.assertIn('автоматически восстанавливает vault', readme)
        self.assertIn('аварийным путём', readme)
        self.assertIn('найденный pending vault при всё ещё живом source останавливает путь до `sudo`', readme)


if __name__ == "__main__":
    unittest.main()
