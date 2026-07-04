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
        self.assertIn("GP_SERVICE_MEMORY_HIGH", installer)
        self.assertIn("GP_SERVICE_MEMORY_MAX", installer)
        self.assertIn("MemoryAccounting=true", installer)
        self.assertIn("MemoryHigh=$SERVICE_MEMORY_HIGH", installer)
        self.assertIn("MemoryMax=$SERVICE_MEMORY_MAX", installer)
        self.assertIn("run-env", helper)
        self.assertIn("queue-update", helper)
        self.assertIn("systemd-run", helper)
        self.assertIn("GP_BRANCH", helper)
        self.assertIn("installed_version=", helper)
        self.assertIn("status=success", helper)
        self.assertIn("status=failed", helper)
        self.assertIn("nft-delete-blockcheck-table", helper)
        self.assertIn("unsupported run target", helper)


if __name__ == "__main__":
    unittest.main()
