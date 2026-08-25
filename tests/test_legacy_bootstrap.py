from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


class LegacyCleanHandoffContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.payload = (cls.root / "scripts" / "legacy-bootstrap.sh").read_text(encoding="utf-8")
        cls.launcher = (cls.root / "scripts" / "legacy-bootstrap-launcher.sh").read_text(encoding="utf-8")

    def test_both_supported_baselines_need_the_user_level_handoff(self) -> None:
        for tag in ("v0.3.4", "v0.3.5-alpha.4"):
            with self.subTest(tag=tag):
                result = subprocess.run(
                    ["git", "-c", f"safe.directory={self.root.as_posix()}", "show", f"{tag}:scripts/clean-install-vault.sh"],
                    cwd=self.root,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_handoff_is_unprivileged_and_pins_candidate_identity(self) -> None:
        self.assertIn("--state-dir ABSOLUTE_PATH", self.payload)
        self.assertIn("--candidate-ref", self.payload)
        self.assertIn("--candidate-sha", self.payload)
        self.assertIn("refs/heads/dev|refs/tags/v", self.payload)
        self.assertIn("candidate SHA does not match the fetched ref", self.payload)
        self.assertIn('cat-file -t FETCH_HEAD', self.payload)
        self.assertIn('candidate release ref must resolve to an annotated immutable tag', self.payload)
        self.assertIn("create_clean_install_vault", self.payload)
        self.assertIn("clean_install_vault_info", self.payload)
        self.assertIn('"handoff": "ready"', self.payload)
        self.assertNotIn("/usr/bin/sudo", self.payload)
        self.assertNotIn("systemctl", self.payload)
        self.assertNotIn("rm -rf /", self.payload)
        self.assertIn("never as root", self.launcher)
        self.assertNotIn("/usr/bin/sudo", self.launcher)

    def test_handoff_does_not_hardcode_a_release_version(self) -> None:
        self.assertNotIn("v0.4.0", self.payload)
        self.assertNotIn("v0.4.0", self.launcher)
        self.assertIn("refs/tags/v[0-9]*.[0-9]*.[0-9]*", self.payload)

    def test_shell_syntax_is_valid(self) -> None:
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash is required for shell syntax validation")
        for path in (self.root / "scripts" / "legacy-bootstrap.sh", self.root / "scripts" / "legacy-bootstrap-launcher.sh"):
            with self.subTest(path=path.name):
                result = subprocess.run([bash, "-n", str(path)], check=False, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
