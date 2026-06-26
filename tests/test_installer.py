from __future__ import annotations

import unittest
from pathlib import Path


class InstallerTests(unittest.TestCase):
    def test_installer_configures_root_helper(self) -> None:
        root = Path(__file__).resolve().parents[1]
        installer = (root / "scripts" / "install-raspberry-pi.sh").read_text(encoding="utf-8")
        helper = (root / "scripts" / "gp-root-helper.sh").read_text(encoding="utf-8")

        self.assertIn("ROOT_HELPER_PATH", installer)
        self.assertIn("gp-root-helper.sh", installer)
        self.assertIn("NOPASSWD", installer)
        self.assertIn("visudo -cf", installer)
        self.assertIn("Environment=GP_ROOT_HELPER", installer)
        self.assertIn("run-env", helper)
        self.assertIn("nft-delete-blockcheck-table", helper)
        self.assertIn("unsupported run target", helper)


if __name__ == "__main__":
    unittest.main()
