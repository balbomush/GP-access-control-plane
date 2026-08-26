from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path


class CleanInstallHandoffBaselineContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.python = Path(os.sys.executable)

    def test_candidate_vault_export_accepts_each_supported_baseline_schema_without_secret_output(self) -> None:
        for tag in ("v0.3.4", "v0.3.5-alpha.4"):
            with self.subTest(tag=tag), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                source = root / "baseline"
                source.mkdir()
                archive = subprocess.run(
                    ["git", "-c", f"safe.directory={self.root.as_posix()}", "archive", tag, "src"],
                    cwd=self.root,
                    check=True,
                    capture_output=True,
                ).stdout
                with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
                    bundle.extractall(source, filter="data")
                state_dir = root / "state"
                legacy_env = os.environ | {"PYTHONPATH": str(source / "src")}
                subprocess.run(
                    [
                        str(self.python),
                        "-c",
                        "from pathlib import Path; from gp_control_plane.storage import connect; connect(Path(__import__('sys').argv[1])).close()",
                        str(state_dir),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    env=legacy_env,
                )
                candidate_env = os.environ | {"PYTHONPATH": str(self.root / "src")}
                home = root / "home"
                home.mkdir()
                result = subprocess.run(
                    [
                        str(self.python),
                        "-c",
                        "import json; from pathlib import Path; from gp_control_plane.backups import create_clean_install_vault, clean_install_vault_info; "
                        "state=Path(__import__('sys').argv[1]); home=Path(__import__('sys').argv[2]); "
                        "created=create_clean_install_vault(state, target_home=home); info=clean_install_vault_info(target_home=home); "
                        "assert info['pending'] and info['vault_id'] == created['vault_id'] and info['archive_sha256'] == created['archive_sha256']; "
                        "print(json.dumps({'created': created, 'info': info}, sort_keys=True))",
                        str(state_dir),
                        str(home),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    env=candidate_env,
                )
                self.assertEqual(result.stderr, "")
                output = json.loads(result.stdout)
                self.assertEqual(output["created"]["schema_version"], "7")
                self.assertEqual(output["info"]["schema_version"], "7")
                self.assertNotIn("confirmation_token", result.stdout)
                self.assertNotIn("handoff_secret", result.stdout)
                self.assertNotIn("SAFE-HANDOFF-001-KNOWN-SECRET", result.stdout + result.stderr)
