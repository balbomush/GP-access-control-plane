from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.backups import (  # noqa: E402
    _consume_verified_vault,
    _mark_vault_verified,
    _read_vault_entry,
    clean_install_vault_dir,
    clean_install_vault_info,
    create_clean_install_vault,
    restore_clean_install_vault,
    create_snapshot,
    restore_snapshot,
    _write_checksums,
)
from gp_control_plane.storage import append_run, connect, read_app_setting, save_app_setting  # noqa: E402
from gp_control_plane.strategy_finder import parse_blockcheck_stdout, read_candidate_page, upsert_candidates  # noqa: E402


class CleanInstallVaultTests(unittest.TestCase):
    def _seed_f01(self, state_dir: Path) -> None:
        parsed = parse_blockcheck_stdout(
            """
* SUMMARY
curl_test_https_tls12 ipv4 f01-a.example.test : nfqws2 --payload=tls_client_hello --lua-desync=fake
"""
        )
        upsert_candidates(state_dir, parsed, {"id": "f01-run-0001"})
        with connect(state_dir) as conn:
            first_domain = conn.execute(
                "SELECT id FROM domains WHERE name = ?", ("f01-a.example.test",)
            ).fetchone()
            strategy = conn.execute("SELECT id, protocol FROM strategies").fetchone()
            self.assertIsNotNone(first_domain)
            self.assertIsNotNone(strategy)
            conn.execute("UPDATE domains SET service_group = ? WHERE id = ?", ("primary", first_domain["id"]))
            second_domain = conn.execute(
                "INSERT INTO domains(name, service_group) VALUES(?, ?) RETURNING id",
                ("f01-b.example.test", "secondary"),
            ).fetchone()
            self.assertIsNotNone(second_domain)
            conn.execute(
                "INSERT INTO strategy_domain_results(strategy_id, domain_id, protocol, source_mode) VALUES(?, ?, ?, ?)",
                (strategy["id"], second_domain["id"], strategy["protocol"], "single_domain"),
            )
            preset = conn.execute(
                """
                INSERT INTO domain_presets(scope, name, kind, label, source_json)
                VALUES(?, ?, 'user', ?, ?)
                RETURNING id
                """,
                ("tests", "two-domain", "Two domains", '{"origin":"test"}'),
            ).fetchone()
            self.assertIsNotNone(preset)
            conn.execute(
                "INSERT INTO preset_domains(preset_id, domain_id, position, enabled) VALUES(?, ?, ?, ?)",
                (preset["id"], first_domain["id"], 0, 1),
            )
            conn.execute(
                "INSERT INTO preset_domains(preset_id, domain_id, position, enabled) VALUES(?, ?, ?, ?)",
                (preset["id"], second_domain["id"], 1, 0),
            )
        append_run(
            state_dir,
            {
                "id": "f01-run-0001",
                "kind": "standard-discovery",
                "status": "success",
                "timestamp": "2026-08-21T00:00:00Z",
                "returncode": 0,
                "domains": ["f01-a.example.test", "f01-b.example.test"],
                "candidate_count": 1,
                "result_count": 1,
            },
        )
        save_app_setting(state_dir, "run_settings", {"curl_parallelism_default": 3}, "2026-08-21T00:00:00Z")
        save_app_setting(state_dir, "service_settings", {"update_channel": "prerelease"}, "2026-08-21T00:00:00Z")

    def test_schema7_vault_restores_history_and_consumes_source_after_verification(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "install-user"
            home.mkdir()
            source_state = root / "source-state"
            target_state = root / "target-state"
            self._seed_f01(source_state)

            created = create_clean_install_vault(source_state, target_home=home)
            self.assertEqual(created["schema_version"], "7")
            self.assertIn("confirmation_token", created)
            self.assertEqual(created["semantic_manifest"]["history_count"], 1)
            pending = clean_install_vault_info(target_home=home)
            self.assertTrue(pending["pending"])
            self.assertNotIn("confirmation_token", pending)

            restored = restore_clean_install_vault(
                target_state,
                target_home=home,
                vault_id=created["vault_id"],
                confirmation_token=created["confirmation_token"],
            )

            self.assertTrue(restored["restored"])
            self.assertTrue(restored["verification"]["verified"])
            self.assertTrue(restored["cleanup"]["source_deleted"])
            self.assertTrue(restored["completed"])
            self.assertEqual(read_candidate_page(target_state, domain="f01-a.example.test", limit=10)["total"], 1)
            self.assertEqual(read_app_setting(target_state, "run_settings")["curl_parallelism_default"], 3)
            self.assertEqual(read_app_setting(target_state, "service_settings")["update_channel"], "prerelease")
            with connect(target_state) as conn:
                domains = {
                    (str(row["name"]), str(row["service_group"]))
                    for row in conn.execute("SELECT name, service_group FROM domains WHERE name LIKE 'f01-%'")
                }
                preset_links = [
                    (str(row["name"]), str(row["domain"]), int(row["position"]), int(row["enabled"]))
                    for row in conn.execute(
                        """
                        SELECT p.name, d.name AS domain, pd.position, pd.enabled
                        FROM domain_presets p JOIN preset_domains pd ON pd.preset_id = p.id
                        JOIN domains d ON d.id = pd.domain_id
                        WHERE p.scope = 'tests' AND p.kind = 'user'
                        ORDER BY pd.position
                        """
                    )
                ]
            self.assertEqual(domains, {("f01-a.example.test", "primary"), ("f01-b.example.test", "secondary")})
            self.assertEqual(
                preset_links,
                [("two-domain", "f01-a.example.test", 0, 1), ("two-domain", "f01-b.example.test", 1, 0)],
            )
            self.assertFalse(clean_install_vault_info(target_home=home)["pending"])
            vault = clean_install_vault_dir(home)
            self.assertFalse((vault / "archive.zip").exists())
            self.assertFalse((vault / "entry.json").exists())
            self.assertTrue((vault / "cleanup.journal.json").exists())

    def test_wrong_token_or_tampered_archive_leaves_pending_vault(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "install-user"
            home.mkdir()
            source_state = root / "source-state"
            self._seed_f01(source_state)
            created = create_clean_install_vault(source_state, target_home=home)

            with self.assertRaisesRegex(ValueError, "confirmation token"):
                restore_clean_install_vault(
                    root / "target-state",
                    target_home=home,
                    vault_id=created["vault_id"],
                    confirmation_token="wrong",
                )
            self.assertTrue(clean_install_vault_info(target_home=home)["pending"])

            archive = clean_install_vault_dir(home) / "archive.zip"
            with archive.open("ab") as handle:
                handle.write(b"tampered")
            if os.name == "posix":
                os.chmod(archive, 0o600)
            with self.assertRaisesRegex(ValueError, "size|checksum"):
                restore_clean_install_vault(
                    root / "target-state",
                    target_home=home,
                    vault_id=created["vault_id"],
                    confirmation_token=created["confirmation_token"],
                )
            self.assertTrue(clean_install_vault_info(target_home=home)["pending"])

    def test_invalid_export_is_rejected_before_a_complete_vault_can_reach_clean_remove(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "install-user"
            home.mkdir()
            source_state = root / "source-state"
            self._seed_f01(source_state)
            invalid_archive = root / "invalid-export.zip"
            invalid_archive.write_bytes(b"not a zip archive")

            with mock.patch("gp_control_plane.backups.snapshot_archive_path", return_value=invalid_archive):
                with self.assertRaisesRegex(ValueError, "not a valid zip"):
                    create_clean_install_vault(source_state, target_home=home)

            vault = clean_install_vault_dir(home)
            self.assertTrue((vault / "archive.zip").is_file())
            self.assertFalse((vault / "entry.json").exists())
            with self.assertRaisesRegex(ValueError, "incomplete topology"):
                clean_install_vault_info(target_home=home)

    def test_archive_directory_sync_failure_prevents_entry_publication(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "install-user"
            home.mkdir()
            source_state = root / "source-state"
            self._seed_f01(source_state)

            with mock.patch("gp_control_plane.backups._fsync_directory", side_effect=OSError("simulated fsync failure")):
                with self.assertRaisesRegex(OSError, "simulated fsync failure"):
                    create_clean_install_vault(source_state, target_home=home)

            vault = clean_install_vault_dir(home)
            self.assertTrue((vault / "archive.zip").is_file())
            self.assertFalse((vault / "entry.json").exists())
            with self.assertRaisesRegex(ValueError, "incomplete topology"):
                clean_install_vault_info(target_home=home)

    def test_entry_is_published_only_after_archive_directory_sync(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "install-user"
            home.mkdir()
            source_state = root / "source-state"
            self._seed_f01(source_state)
            vault = clean_install_vault_dir(home)
            observations: list[tuple[bool, bool]] = []

            def observe_sync(path: Path) -> None:
                self.assertEqual(path, vault)
                observations.append(((vault / "archive.zip").exists(), (vault / "entry.json").exists()))

            with mock.patch("gp_control_plane.backups._fsync_directory", side_effect=observe_sync):
                created = create_clean_install_vault(source_state, target_home=home)

            self.assertTrue(created["created"])
            self.assertEqual(observations, [(True, False), (True, True)])

    def test_retry_replaces_only_incomplete_private_export(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "install-user"
            home.mkdir()
            source_state = root / "source-state"
            self._seed_f01(source_state)
            vault = clean_install_vault_dir(home)
            archive = vault / "archive.zip"
            entry = vault / "entry.json"

            with mock.patch("gp_control_plane.backups._fsync_directory", side_effect=OSError("simulated fsync failure")):
                with self.assertRaisesRegex(OSError, "simulated fsync failure"):
                    create_clean_install_vault(source_state, target_home=home)
            self.assertTrue(archive.exists())
            self.assertFalse(entry.exists())

            original_unlink = Path.unlink
            removed: list[Path] = []

            def remember_unlink(path: Path, *args: object, **kwargs: object) -> None:
                if path.parent == vault:
                    removed.append(path)
                original_unlink(path, *args, **kwargs)

            with mock.patch.object(Path, "unlink", remember_unlink):
                retried = create_clean_install_vault(source_state, target_home=home)

            self.assertTrue(retried["created"])
            self.assertEqual(removed, [archive])
            self.assertTrue(archive.is_file())
            self.assertTrue(entry.is_file())
            self.assertTrue(clean_install_vault_info(target_home=home)["pending"])

    def test_retry_never_removes_a_complete_pending_vault(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "install-user"
            home.mkdir()
            source_state = root / "source-state"
            self._seed_f01(source_state)
            created = create_clean_install_vault(source_state, target_home=home)
            vault = clean_install_vault_dir(home)
            original_unlink = Path.unlink

            def reject_vault_unlink(path: Path, *args: object, **kwargs: object) -> None:
                if path.parent == vault:
                    self.fail(f"retry attempted to remove pending vault member: {path.name}")
                original_unlink(path, *args, **kwargs)

            with mock.patch.object(Path, "unlink", reject_vault_unlink):
                with self.assertRaisesRegex(RuntimeError, "pending"):
                    create_clean_install_vault(source_state, target_home=home)

            self.assertTrue((vault / "archive.zip").is_file())
            self.assertTrue((vault / "entry.json").is_file())
            self.assertEqual(clean_install_vault_info(target_home=home)["vault_id"], created["vault_id"])

    def test_refuses_second_pending_entry_and_symlink_vault_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "install-user"
            home.mkdir()
            source_state = root / "source-state"
            self._seed_f01(source_state)
            create_clean_install_vault(source_state, target_home=home)
            with self.assertRaisesRegex(RuntimeError, "pending"):
                create_clean_install_vault(source_state, target_home=home)

            archive = clean_install_vault_dir(home) / "archive.zip"
            replacement = root / "replacement.zip"
            archive.replace(replacement)
            try:
                archive.symlink_to(replacement)
            except OSError:
                self.skipTest("symlink creation is unavailable on this platform")
            with self.assertRaisesRegex(ValueError, "file is invalid"):
                clean_install_vault_info(target_home=home)

    def test_schema7_archive_manifest_is_semantic_and_token_is_hash_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "install-user"
            home.mkdir()
            source_state = root / "source-state"
            self._seed_f01(source_state)
            created = create_clean_install_vault(source_state, target_home=home)
            entry = json.loads((clean_install_vault_dir(home) / "entry.json").read_text(encoding="utf-8"))
            self.assertNotIn(created["confirmation_token"], json.dumps(entry))
            self.assertIn("confirmation_token_sha256", entry)
            self.assertEqual(entry["schema_version"], "7")

    def test_schema6_is_accepted_but_reports_limited_history_restore(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            self._seed_f01(state_dir)
            snapshot_id = create_snapshot(state_dir)["snapshot"]["id"]
            snapshot = state_dir.parent / "backups" / "snapshots" / snapshot_id
            manifest = snapshot / "manifest.json"
            manifest.write_text(manifest.read_text(encoding="utf-8").replace('"schema_version": "7"', '"schema_version": "6"'), encoding="utf-8")
            _write_checksums(snapshot)
            save_app_setting(state_dir, "run_settings", {"curl_parallelism_default": 9}, "2026-08-21T01:00:00Z")
            append_run(state_dir, {"id": "current", "kind": "other", "status": "success", "timestamp": "2026-08-21T01:00:00Z"})

            restored = restore_snapshot(state_dir, snapshot_id)

            self.assertTrue(restored["limited_restore"])
            self.assertFalse(restored["full_f01_restore"])
            self.assertEqual(restored["missing_f01_data"], ["completed_history"])
            self.assertEqual(restored["settings_count"], 2)
            self.assertEqual(restored["history_count"], 0)
            self.assertEqual(read_app_setting(state_dir, "run_settings")["curl_parallelism_default"], 3)

    def test_schema7_missing_history_is_rejected_before_pre_restore_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            self._seed_f01(state_dir)
            snapshot_id = create_snapshot(state_dir)["snapshot"]["id"]
            snapshot = state_dir.parent / "backups" / "snapshots" / snapshot_id
            (snapshot / "history" / "runs.ndjson").unlink()
            _write_checksums(snapshot)

            with self.assertRaisesRegex(ValueError, "schema 7 backup is incomplete"):
                restore_snapshot(state_dir, snapshot_id)

    def test_cleanup_second_unlink_failure_is_durable_and_resume_completes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "install-user"
            home.mkdir()
            source_state = root / "source-state"
            target_state = root / "target-state"
            self._seed_f01(source_state)
            created = create_clean_install_vault(source_state, target_home=home)
            vault = clean_install_vault_dir(home)
            archive = vault / "archive.zip"
            entry = vault / "entry.json"
            original_unlink = Path.unlink

            def fail_second_unlink(path: Path, *args: object, **kwargs: object) -> None:
                if path == entry:
                    raise OSError("simulated cleanup interruption")
                original_unlink(path, *args, **kwargs)

            with mock.patch.object(Path, "unlink", fail_second_unlink):
                restored = restore_clean_install_vault(
                    target_state,
                    target_home=home,
                    vault_id=created["vault_id"],
                    confirmation_token=created["confirmation_token"],
                )

            self.assertFalse(restored["completed"])
            self.assertFalse(restored["cleanup"]["source_deleted"])
            self.assertFalse(archive.exists())
            self.assertTrue(entry.exists())
            journal = json.loads((vault / "cleanup.journal.json").read_text(encoding="utf-8"))
            self.assertEqual(journal["cleanup"], "in_progress")
            self.assertEqual(journal["phase"], "archive_deleted")
            self.assertIn("confirmation_token_sha256", journal)

            resumed = restore_clean_install_vault(
                target_state,
                target_home=home,
                vault_id=created["vault_id"],
                confirmation_token=created["confirmation_token"],
            )
            self.assertTrue(resumed["resumed_cleanup"])
            self.assertTrue(resumed["completed"])
            self.assertFalse(archive.exists())
            self.assertFalse(entry.exists())
            completed = json.loads((vault / "cleanup.journal.json").read_text(encoding="utf-8"))
            self.assertEqual((completed["cleanup"], completed["phase"]), ("completed", "completed"))

    def test_semantic_domain_group_mismatch_retains_verified_source_vault(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "install-user"
            home.mkdir()
            source_state = root / "source-state"
            target_state = root / "target-state"
            self._seed_f01(source_state)
            created = create_clean_install_vault(source_state, target_home=home)

            from gp_control_plane import backups as backups_module

            original_verify = backups_module._verify_restore_semantics

            def mutate_group_before_verify(state_dir: Path, restore_plan: dict[str, object]) -> dict[str, object]:
                with connect(state_dir) as conn:
                    conn.execute("UPDATE domains SET service_group = 'wrong' WHERE name = ?", ("f01-b.example.test",))
                return original_verify(state_dir, restore_plan)

            with mock.patch.object(backups_module, "_verify_restore_semantics", side_effect=mutate_group_before_verify):
                with self.assertRaisesRegex(RuntimeError, "semantic verification failed"):
                    restore_clean_install_vault(
                        target_state,
                        target_home=home,
                        vault_id=created["vault_id"],
                        confirmation_token=created["confirmation_token"],
                    )
            vault = clean_install_vault_dir(home)
            self.assertTrue((vault / "archive.zip").is_file())
            self.assertTrue((vault / "entry.json").is_file())

    def test_unexpected_vault_member_is_rejected_before_restore_db_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "install-user"
            home.mkdir()
            source_state = root / "source-state"
            target_state = root / "target-state"
            self._seed_f01(source_state)
            created = create_clean_install_vault(source_state, target_home=home)
            vault = clean_install_vault_dir(home)
            unexpected = vault / "unexpected.txt"
            unexpected.write_text("not part of the vault", encoding="utf-8")
            if os.name == "posix":
                os.chmod(unexpected, 0o600)

            with self.assertRaisesRegex(ValueError, "unexpected member"):
                clean_install_vault_info(target_home=home)
            with self.assertRaisesRegex(ValueError, "unexpected member"):
                restore_clean_install_vault(
                    target_state,
                    target_home=home,
                    vault_id=created["vault_id"],
                    confirmation_token=created["confirmation_token"],
                )
            self.assertFalse(target_state.exists())

    def test_consume_requires_durable_verification_and_bound_confirmation_token(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "install-user"
            home.mkdir()
            source_state = root / "source-state"
            self._seed_f01(source_state)
            created = create_clean_install_vault(source_state, target_home=home)
            vault = clean_install_vault_dir(home)
            archive = vault / "archive.zip"
            entry = vault / "entry.json"
            verification = {"verified": True, "checks": {"semantic": True, "integrity": True}}

            with self.assertRaisesRegex(RuntimeError, "durably verified"):
                _consume_verified_vault(vault, created["vault_id"], created["confirmation_token"], verification)
            self.assertTrue(archive.exists())
            self.assertTrue(entry.exists())

            _mark_vault_verified(vault, _read_vault_entry(vault), verification)
            with self.assertRaisesRegex(RuntimeError, "confirmation token"):
                _consume_verified_vault(vault, created["vault_id"], "wrong", verification)
            self.assertTrue(archive.exists())
            self.assertTrue(entry.exists())

            consumed = _consume_verified_vault(
                vault,
                created["vault_id"],
                created["confirmation_token"],
                verification,
            )
            self.assertTrue(consumed["completed"])
            self.assertTrue(consumed["source_deleted"])
            self.assertFalse(archive.exists())
            self.assertFalse(entry.exists())


if __name__ == "__main__":
    unittest.main()
