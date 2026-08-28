from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.backups import clean_install_handoff_path, clean_install_vault_dir, clean_install_vault_info, create_clean_install_vault, restore_clean_install_vault
from gp_control_plane.storage import connect, read_app_setting, save_app_setting
from gp_control_plane.strategy_finder import parse_blockcheck_stdout, read_candidate_page, upsert_candidates


class CleanInstallVaultTests(unittest.TestCase):
    def seed(self, state_dir: Path) -> None:
        parsed = parse_blockcheck_stdout("* SUMMARY\ncurl_test_https_tls12 ipv4 legacy.example.test : nfqws2 --payload=tls_client_hello --lua-desync=fake\n")
        upsert_candidates(state_dir, parsed, {"id": "legacy-run"})
        save_app_setting(state_dir, "run_settings", {"curl_parallelism_default": 3}, "2026-08-28T00:00:00Z")

    def test_restore_preserves_semantic_data_then_deletes_vault_and_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); home = root / "home"; home.mkdir(); source = root / "legacy"; target = root / "fresh"
            self.seed(source)
            created = create_clean_install_vault(source, target_home=home)
            self.assertTrue(clean_install_vault_info(target_home=home)["pending"])
            restored = restore_clean_install_vault(target, target_home=home, vault_id=created["vault_id"])
            self.assertTrue(restored["completed"])
            self.assertTrue(restored["verification"]["verified"])
            self.assertTrue(restored["storage_status"]["ready"])
            self.assertTrue(restored["cleanup"]["source_deleted"])
            self.assertEqual(read_candidate_page(target, domain="legacy.example.test", limit=5)["total"], 1)
            self.assertEqual(read_app_setting(target, "run_settings")["curl_parallelism_default"], 3)
            self.assertFalse(clean_install_vault_dir(home).exists())
            self.assertFalse(clean_install_handoff_path(home).exists())

    def test_corrupt_vault_is_rejected_without_consuming_either_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); home = root / "home"; home.mkdir(); source = root / "legacy"
            self.seed(source)
            created = create_clean_install_vault(source, target_home=home)
            archive = clean_install_vault_dir(home) / "archive.zip"
            archive.write_bytes(b"corrupt")
            with self.assertRaisesRegex(ValueError, "checksum|size"):
                restore_clean_install_vault(root / "fresh", target_home=home, vault_id=created["vault_id"])
            self.assertTrue(archive.exists())
            self.assertTrue((clean_install_vault_dir(home) / "entry.json").exists())
            self.assertTrue(clean_install_handoff_path(home).exists())

    def test_handoff_is_public_metadata_not_a_secret_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); home = root / "home"; home.mkdir(); source = root / "legacy"; self.seed(source)
            created = create_clean_install_vault(source, target_home=home)
            handoff = json.loads(clean_install_handoff_path(home).read_text(encoding="utf-8"))
            self.assertEqual(handoff["vault_id"], created["vault_id"])
            self.assertEqual(handoff["device_binding"], created["device_binding"])

    def test_vault_and_handoff_publish_as_one_atomic_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); home = root / "home"; home.mkdir(); source = root / "legacy"; self.seed(source)
            create_clean_install_vault(source, target_home=home)
            vault = clean_install_vault_dir(home)
            self.assertEqual(clean_install_handoff_path(home).parent, vault)
            self.assertEqual({item.name for item in vault.iterdir()}, {"archive.zip", "entry.json", "handoff.json"})
            source_text = (Path(__file__).resolve().parents[1] / "src" / "gp_control_plane" / "backups.py").read_text(encoding="utf-8")
            self.assertIn("stage.replace(vault)", source_text)
            self.assertNotIn("handoff_stage", source_text)

    def test_cross_device_vault_is_rejected_without_touching_sources(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); home = root / "home"; home.mkdir(); source = root / "legacy"; self.seed(source)
            with mock.patch("gp_control_plane.backups._local_device_binding", return_value="a" * 64):
                created = create_clean_install_vault(source, target_home=home)
            with mock.patch("gp_control_plane.backups._local_device_binding", return_value="b" * 64):
                with self.assertRaisesRegex(ValueError, "another device"):
                    restore_clean_install_vault(root / "fresh", target_home=home, vault_id=created["vault_id"])
            self.assertTrue((clean_install_vault_dir(home) / "archive.zip").exists())
            self.assertTrue(clean_install_handoff_path(home).exists())

    def test_extra_vault_member_rejects_restore_before_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); home = root / "home"; home.mkdir(); source = root / "legacy"; self.seed(source)
            created = create_clean_install_vault(source, target_home=home)
            extra = clean_install_vault_dir(home) / "unexpected"
            extra.write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unexpected source members"):
                restore_clean_install_vault(root / "fresh", target_home=home, vault_id=created["vault_id"])
            self.assertTrue((clean_install_vault_dir(home) / "archive.zip").exists())
            self.assertTrue(clean_install_handoff_path(home).exists())

    def test_cleanup_directory_failure_restores_the_complete_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); home = root / "home"; home.mkdir(); source = root / "legacy"; self.seed(source)
            created = create_clean_install_vault(source, target_home=home)
            vault = clean_install_vault_dir(home)
            original_rmdir = Path.rmdir
            def fail_vault_rmdir(path: Path) -> None:
                if path == vault:
                    raise OSError("injected vault cleanup failure")
                original_rmdir(path)
            with mock.patch.object(Path, "rmdir", new=fail_vault_rmdir):
                with self.assertRaisesRegex(OSError, "injected vault cleanup failure"):
                    restore_clean_install_vault(root / "fresh", target_home=home, vault_id=created["vault_id"])
            self.assertTrue((vault / "archive.zip").exists())
            self.assertTrue((vault / "entry.json").exists())
            self.assertTrue(clean_install_handoff_path(home).exists())

    def test_failed_atomic_publish_leaves_no_pending_path_and_retry_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); home = root / "home"; home.mkdir(); source = root / "legacy"; self.seed(source)
            from gp_control_plane import backups
            original = backups._write_private_json_atomic
            def fail_entry(path: Path, payload: dict[str, object]) -> None:
                if path.name == "entry.json":
                    raise OSError("injected publish failure")
                original(path, payload)
            with mock.patch.object(backups, "_write_private_json_atomic", side_effect=fail_entry):
                with self.assertRaisesRegex(OSError, "injected publish failure"):
                    create_clean_install_vault(source, target_home=home)
            self.assertFalse(clean_install_vault_dir(home).exists())
            self.assertFalse(clean_install_handoff_path(home).exists())
            self.assertTrue(create_clean_install_vault(source, target_home=home)["created"])

    def test_pending_vault_remains_verifiable_after_failed_fresh_install_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); home = root / "home"; home.mkdir(); source = root / "legacy"; self.seed(source)
            created = create_clean_install_vault(source, target_home=home)
            failed_install = subprocess.run([sys.executable, "-c", "raise SystemExit(73)"], check=False)
            self.assertEqual(failed_install.returncode, 73)
            tool = Path(__file__).resolve().parents[1] / "scripts" / "clean-install-vault.py"
            retry = subprocess.run([sys.executable, str(tool), "--verify", "--state-dir", str(source), "--home", str(home)], capture_output=True, text=True, check=False)
            self.assertEqual(retry.returncode, 0, retry.stderr)
            self.assertTrue(clean_install_vault_info(target_home=home)["pending"])
            self.assertEqual(clean_install_vault_info(target_home=home)["vault_id"], created["vault_id"])

    def test_cli_create_reports_ready_and_publishes_a_pending_vault(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); home = root / "home"; home.mkdir(); source = root / "legacy"; self.seed(source)
            tool = Path(__file__).resolve().parents[1] / "scripts" / "clean-install-vault.py"
            created = subprocess.run(
                [sys.executable, str(tool), "--state-dir", str(source), "--home", str(home)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            self.assertRegex(created.stdout, r"^status=ready vault_id=[0-9a-f-]+\n$")
            info = clean_install_vault_info(target_home=home)
            self.assertTrue(info["pending"])
            self.assertRegex(str(info["vault_id"]), r"^[0-9a-f-]+$")

    def test_module_has_one_active_public_vault_contract(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "src" / "gp_control_plane" / "backups.py").read_text(encoding="utf-8")
        for name in ("create_clean_install_vault", "clean_install_vault_info", "restore_clean_install_vault"):
            self.assertEqual(source.count(f"def {name}("), 1, name)
        self.assertNotIn("def validate_clean_install_handoff(", source)
        self.assertNotIn("def create_clean_install_vault_with_handoff_validation(", source)
        for retired in ("def _legacy_", "def _retired_", "handoff_secret", "cleanup.journal.json", "finalization journal"):
            self.assertNotIn(retired, source, retired)

    def test_both_supported_legacy_tags_have_the_fixed_state_layout(self) -> None:
        import subprocess
        repo = Path(__file__).resolve().parents[1]
        for tag in ("v0.3.4", "v0.3.5-alpha.4"):
            shown = subprocess.run(["git", "show", f"{tag}:scripts/install-linux.sh"], cwd=repo, capture_output=True, text=True, check=False)
            if shown.returncode:
                # Historical tag inspection only: v0.3.4 used this filename.
                shown = subprocess.run(["git", "show", f"{tag}:scripts/install-raspberry-pi.sh"], cwd=repo, capture_output=True, text=True, check=True)
            self.assertIn("build/state", shown.stdout, tag)


if __name__ == "__main__":
    unittest.main()
