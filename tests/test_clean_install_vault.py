from __future__ import annotations

import json
import multiprocessing
import os
import queue
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.backups import (  # noqa: E402
    _consume_verified_vault,
    _mark_vault_verified,
    _read_vault_entry,
    clean_install_handoff_path,
    clean_install_vault_dir,
    clean_install_vault_info,
    create_clean_install_vault,
    create_clean_install_vault_with_handoff_validation,
    restore_clean_install_vault,
    create_snapshot,
    restore_snapshot,
    _write_checksums,
)
from gp_control_plane.storage import append_run, connect, read_app_setting, save_app_setting  # noqa: E402
from gp_control_plane.strategy_finder import parse_blockcheck_stdout, read_candidate_page, upsert_candidates  # noqa: E402


def _create_vault_in_process(
    state_dir_raw: str,
    home_raw: str,
    entered: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
    results: multiprocessing.queues.Queue,
) -> None:
    """Hold the creator inside its process lock so a sibling process races it."""
    from gp_control_plane import backups as backups_module

    original_create = backups_module._create_clean_install_vault_locked

    def hold_after_lock(state_dir: Path, *, target_home: Path | None) -> dict[str, object]:
        entered.set()
        if not release.wait(timeout=10):
            raise TimeoutError("parent did not release concurrent vault creator")
        return original_create(state_dir, target_home=target_home)

    try:
        with mock.patch.object(backups_module, "_create_clean_install_vault_locked", side_effect=hold_after_lock):
            created = backups_module.create_clean_install_vault(Path(state_dir_raw), target_home=Path(home_raw))
        results.put(("success", str(created["vault_id"])))
    except BaseException as error:  # noqa: BLE001 - child result is asserted by the parent test
        results.put(("error", f"{type(error).__name__}: {error}"))


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

    def _assert_consistent_pending_sources(self, home: Path, vault_id: str) -> None:
        vault = clean_install_vault_dir(home)
        archive = vault / "archive.zip"
        entry = vault / "entry.json"
        handoff = clean_install_handoff_path(home)
        info = clean_install_vault_info(target_home=home)
        self.assertTrue(info["pending"])
        self.assertEqual(info["vault_id"], vault_id)
        self.assertTrue(archive.is_file())
        self.assertTrue(entry.is_file())
        self.assertTrue(handoff.is_file())
        entry_payload = json.loads(entry.read_text(encoding="utf-8"))
        handoff_payload = json.loads(handoff.read_text(encoding="utf-8"))
        self.assertEqual(entry_payload["vault_id"], vault_id)
        self.assertEqual(handoff_payload["vault_id"], vault_id)
        self.assertEqual(entry_payload["archive_size_bytes"], archive.stat().st_size)
        self.assertEqual(entry_payload["archive_sha256"], info["archive_sha256"])
        self.assertTrue(str(handoff_payload.get("handoff_secret") or ""))

    def test_concurrent_thread_creators_allow_exactly_one_pending_vault(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "install-user"
            home.mkdir()
            state_dir = root / "state"
            self._seed_f01(state_dir)
            entered = threading.Event()
            release = threading.Event()
            outcomes: queue.Queue[tuple[str, str]] = queue.Queue()

            from gp_control_plane import backups as backups_module

            original_create = backups_module._create_clean_install_vault_locked

            def hold_after_lock(state: Path, *, target_home: Path | None) -> dict[str, object]:
                entered.set()
                if not release.wait(timeout=10):
                    raise TimeoutError("test did not release concurrent thread creator")
                return original_create(state, target_home=target_home)

            def attempt() -> None:
                try:
                    created = create_clean_install_vault(state_dir, target_home=home)
                    outcomes.put(("success", str(created["vault_id"])))
                except BaseException as error:  # noqa: BLE001 - asserted below
                    outcomes.put(("error", f"{type(error).__name__}: {error}"))

            with mock.patch.object(backups_module, "_create_clean_install_vault_locked", side_effect=hold_after_lock):
                creator = threading.Thread(target=attempt, name="vault-creator")
                loser = threading.Thread(target=attempt, name="vault-creator-contender")
                creator.start()
                self.assertTrue(entered.wait(timeout=10))
                loser.start()
                loser.join(timeout=10)
                self.assertFalse(loser.is_alive())
                release.set()
                creator.join(timeout=10)
                self.assertFalse(creator.is_alive())

            results = [outcomes.get_nowait(), outcomes.get_nowait()]
            successes = [value for status, value in results if status == "success"]
            failures = [value for status, value in results if status == "error"]
            self.assertEqual(len(successes), 1)
            self.assertEqual(len(failures), 1)
            self.assertIn("already in progress", failures[0])
            self._assert_consistent_pending_sources(home, successes[0])

    def test_concurrent_process_creators_allow_exactly_one_pending_vault(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "install-user"
            home.mkdir()
            state_dir = root / "state"
            self._seed_f01(state_dir)
            context = multiprocessing.get_context("spawn")
            entered = context.Event()
            release = context.Event()
            outcomes = context.Queue()
            creator = context.Process(
                target=_create_vault_in_process,
                args=(str(state_dir), str(home), entered, release, outcomes),
                name="vault-process-creator",
            )
            creator.start()
            try:
                self.assertTrue(entered.wait(timeout=10))
                with self.assertRaisesRegex(RuntimeError, "already in progress"):
                    create_clean_install_vault(state_dir, target_home=home)
            finally:
                release.set()
            creator.join(timeout=10)
            if creator.is_alive():
                creator.terminate()
                creator.join(timeout=5)
                self.fail("concurrent vault creator did not stop")
            self.assertEqual(creator.exitcode, 0)
            try:
                status, value = outcomes.get(timeout=5)
            except queue.Empty:
                self.fail("concurrent vault creator did not report an outcome")
            self.assertEqual(status, "success", value)
            self._assert_consistent_pending_sources(home, value)

    def test_handoff_binding_failure_after_create_never_returns_ready_vault(self) -> None:
        for fault in ("missing", "mutated"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                home = root / "install-user"
                home.mkdir()
                state_dir = root / "state"
                self._seed_f01(state_dir)
                from gp_control_plane import backups as backups_module

                original_create = backups_module._create_clean_install_vault_locked

                def invalidate_handoff(state: Path, *, target_home: Path | None) -> dict[str, object]:
                    created = original_create(state, target_home=target_home)
                    handoff = clean_install_handoff_path(target_home)
                    if fault == "missing":
                        handoff.unlink()
                    else:
                        handoff.write_text(
                            json.dumps({"vault_id": created["vault_id"], "handoff_secret": "mutated-by-test"}),
                            encoding="utf-8",
                        )
                        if os.name == "posix":
                            os.chmod(handoff, 0o600)
                    return created

                with mock.patch.object(backups_module, "_create_clean_install_vault_locked", side_effect=invalidate_handoff):
                    with self.assertRaisesRegex((RuntimeError, ValueError), "handoff"):
                        create_clean_install_vault_with_handoff_validation(state_dir, target_home=home)

                vault = clean_install_vault_dir(home)
                self.assertTrue((vault / "archive.zip").is_file())
                self.assertTrue((vault / "entry.json").is_file())
                handoff = clean_install_handoff_path(home)
                if fault == "missing":
                    self.assertFalse(handoff.exists())
                else:
                    self.assertTrue(handoff.is_file())
                    self.assertEqual(json.loads(handoff.read_text(encoding="utf-8"))["handoff_secret"], "mutated-by-test")

    def test_creation_lock_unlock_errors_do_not_strand_future_create_or_mask_primary_error(self) -> None:
        for platform_name in ("posix", "nt"):
            with self.subTest(platform_name=platform_name), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                home = root / "install-user"
                home.mkdir()
                state_dir = root / "state"
                self._seed_f01(state_dir)
                from gp_control_plane import backups as backups_module

                unlock_constant = 4 if platform_name == "posix" else 2
                release_failures = [2]

                def unlock_fault(*args: object) -> None:
                    operation = int(args[1])
                    if operation == unlock_constant and release_failures[0]:
                        release_failures[0] -= 1
                        raise OSError(f"{platform_name} unlock failure")

                fake_lock_module = types.SimpleNamespace(
                    LOCK_EX=1,
                    LOCK_NB=2,
                    LOCK_UN=unlock_constant,
                    LK_NBLCK=1,
                    LK_UNLCK=unlock_constant,
                    flock=unlock_fault,
                    locking=unlock_fault,
                )
                module_name = "fcntl" if platform_name == "posix" else "msvcrt"
                lock_parent = home / backups_module.CLEAN_INSTALL_CREATION_LOCK_RELATIVE_PATH.parent
                lock_parent.mkdir(parents=True)
                original_fstat = backups_module.os.fstat

                def private_lock_stat(descriptor: int) -> types.SimpleNamespace:
                    details = original_fstat(descriptor)
                    return types.SimpleNamespace(st_mode=(details.st_mode & ~0o777) | 0o600, st_uid=home.stat().st_uid)

                with (
                    mock.patch.object(backups_module.os, "name", platform_name),
                    mock.patch.object(backups_module.os, "geteuid", return_value=home.stat().st_uid, create=True),
                    mock.patch.dict(sys.modules, {module_name: fake_lock_module}),
                    mock.patch.object(backups_module.os, "fstat", side_effect=private_lock_stat),
                    mock.patch.object(backups_module, "_clean_install_home", return_value=home),
                    mock.patch.object(backups_module, "_prepare_creation_lock_parent", return_value=None),
                ):
                    with self.assertRaisesRegex(OSError, f"{platform_name} unlock failure"):
                        with backups_module._clean_install_vault_creation_lock(home):
                            pass
                    with self.assertRaisesRegex(ValueError, "primary guarded failure"):
                        with backups_module._clean_install_vault_creation_lock(home):
                            raise ValueError("primary guarded failure")
                    with backups_module._clean_install_vault_creation_lock(home):
                        pass

                created = create_clean_install_vault(state_dir, target_home=home)
                self._assert_consistent_pending_sources(home, str(created["vault_id"]))

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
            self.assertNotIn("confirmation_token", created)
            self.assertNotIn("handoff_secret", created)
            self.assertEqual(created["semantic_manifest"]["history_count"], 1)
            pending = clean_install_vault_info(target_home=home)
            self.assertTrue(pending["pending"])
            self.assertNotIn("confirmation_token", pending)

            restored = restore_clean_install_vault(
                target_state,
                target_home=home,
                vault_id=created["vault_id"],
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
            self.assertFalse(clean_install_handoff_path(home).exists())
            self.assertFalse(vault.exists())

    def test_missing_or_mismatched_handoff_or_tampered_archive_leaves_sources_intact(self) -> None:
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
            handoff = clean_install_handoff_path(home)

            handoff.unlink()
            with self.assertRaisesRegex(RuntimeError, "handoff is unavailable"):
                restore_clean_install_vault(
                    root / "target-state",
                    target_home=home,
                    vault_id=created["vault_id"],
                )
            self.assertTrue(clean_install_vault_info(target_home=home)["pending"])
            self.assertTrue(archive.exists())
            self.assertTrue(entry.exists())

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
            handoff = clean_install_handoff_path(home)
            handoff_payload = json.loads(handoff.read_text(encoding="utf-8"))
            handoff.write_text(json.dumps({"vault_id": created["vault_id"], "handoff_secret": "wrong-secret"}), encoding="utf-8")
            if os.name == "posix":
                os.chmod(handoff, 0o600)
            with self.assertRaisesRegex(RuntimeError, "handoff does not match"):
                restore_clean_install_vault(root / "target-state", target_home=home, vault_id=created["vault_id"])
            self.assertTrue(archive.exists())
            self.assertTrue(entry.exists())
            self.assertTrue(handoff.exists())

            # Integrity failure is independent of handoff validation and cannot consume either source.
            handoff.write_text(json.dumps(handoff_payload), encoding="utf-8")
            if os.name == "posix":
                os.chmod(handoff, 0o600)
            with archive.open("ab") as handle:
                handle.write(b"tampered")
            if os.name == "posix":
                os.chmod(archive, 0o600)
            with self.assertRaisesRegex(ValueError, "size|checksum"):
                restore_clean_install_vault(
                    root / "target-state",
                    target_home=home,
                    vault_id=created["vault_id"],
                )
            self.assertTrue(clean_install_vault_info(target_home=home)["pending"])
            self.assertTrue(handoff.exists())

    @unittest.skipUnless(os.name == "posix", "handoff ownership and mode are POSIX contracts")
    def test_unsafe_handoff_mode_or_owner_never_starts_restore_or_deletes_sources(self) -> None:
        for case in ("mode", "owner"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw:
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
                handoff = clean_install_handoff_path(home)
                if case == "mode":
                    os.chmod(handoff, 0o644)
                    guard = self.assertRaisesRegex(PermissionError, "permissions are unsafe")
                    with guard:
                        restore_clean_install_vault(target_state, target_home=home, vault_id=created["vault_id"])
                else:
                    original_fstat = os.fstat

                    def foreign_owner(fd: int) -> types.SimpleNamespace:
                        details = original_fstat(fd)
                        return types.SimpleNamespace(st_mode=details.st_mode, st_uid=details.st_uid + 1)

                    with mock.patch("gp_control_plane.backups.os.fstat", side_effect=foreign_owner):
                        with self.assertRaisesRegex(PermissionError, "permissions are unsafe"):
                            restore_clean_install_vault(target_state, target_home=home, vault_id=created["vault_id"])
                self.assertFalse(target_state.exists())
                self.assertTrue(archive.exists())
                self.assertTrue(entry.exists())
                self.assertTrue(handoff.exists())

    def test_symlink_handoff_never_starts_restore_or_deletes_sources(self) -> None:
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
            handoff = clean_install_handoff_path(home)
            replacement = root / "handoff-replacement.json"
            handoff.replace(replacement)
            try:
                handoff.symlink_to(replacement)
            except OSError:
                self.skipTest("symlink creation is unavailable on this platform")
            with self.assertRaisesRegex(ValueError, "not a regular file"):
                restore_clean_install_vault(target_state, target_home=home, vault_id=created["vault_id"])
            self.assertFalse(target_state.exists())
            self.assertTrue(archive.exists())
            self.assertTrue(entry.exists())
            self.assertTrue(handoff.is_symlink())

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

    def test_entry_is_published_only_after_archive_and_full_handoff_chain_sync(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "install-user"
            home.mkdir()
            source_state = root / "source-state"
            self._seed_f01(source_state)
            vault = clean_install_vault_dir(home)
            observations: list[tuple[Path, bool, bool, bool]] = []
            handoff_parent = clean_install_handoff_path(home).parent

            def observe_sync(path: Path) -> None:
                observations.append(
                    (path, (vault / "archive.zip").exists(), clean_install_handoff_path(home).exists(), (vault / "entry.json").exists())
                )

            with mock.patch("gp_control_plane.backups._fsync_directory", side_effect=observe_sync):
                created = create_clean_install_vault(source_state, target_home=home)

            self.assertTrue(created["created"])
            entry_publish_index = next(index for index, (_path, _archive, _handoff, entry) in enumerate(observations) if entry)
            self.assertEqual(observations[0], (vault, True, False, False))
            self.assertTrue(all(not entry for _path, _archive, _handoff, entry in observations[:entry_publish_index]))
            self.assertTrue(all(archive for _path, archive, _handoff, _entry in observations[:entry_publish_index]))
            self.assertTrue(all(handoff for _path, _archive, handoff, _entry in observations[entry_publish_index - 1:entry_publish_index]))

            # The creation lock may create the ancestors first, but every
            # canonical directory from home through the private handoff parent
            # must be synced before entry.json can make the vault publishable.
            canonical_chain = (
                home,
                home / ".local",
                home / ".local" / "share",
                home / ".local" / "share" / "gp-control-plane",
                handoff_parent,
            )
            synced_before_publish = {path for path, _archive, _handoff, _entry in observations[:entry_publish_index]}
            self.assertTrue(set(canonical_chain).issubset(synced_before_publish))
            self.assertEqual(observations[-1], (vault, True, True, True))

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

    def test_schema7_archive_manifest_is_semantic_and_handoff_secret_is_hash_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "install-user"
            home.mkdir()
            source_state = root / "source-state"
            self._seed_f01(source_state)
            created = create_clean_install_vault(source_state, target_home=home)
            entry = json.loads((clean_install_vault_dir(home) / "entry.json").read_text(encoding="utf-8"))
            handoff = json.loads(clean_install_handoff_path(home).read_text(encoding="utf-8"))
            self.assertNotIn(handoff["handoff_secret"], json.dumps(entry))
            self.assertIn("handoff_secret_sha256", entry)
            self.assertNotIn("handoff_secret", created)
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
                )

            self.assertFalse(restored["completed"])
            self.assertFalse(restored["cleanup"]["source_deleted"])
            self.assertFalse(archive.exists())
            self.assertTrue(entry.exists())
            self.assertTrue(clean_install_handoff_path(home).exists())
            journal = json.loads((vault / "cleanup.journal.json").read_text(encoding="utf-8"))
            self.assertEqual(journal["cleanup"], "in_progress")
            self.assertEqual(journal["phase"], "entry_unlinking")
            self.assertIn("handoff_secret_sha256", journal)

            resumed = restore_clean_install_vault(
                target_state,
                target_home=home,
                vault_id=created["vault_id"],
            )
            self.assertTrue(resumed["resumed_cleanup"])
            self.assertTrue(resumed["completed"])
            self.assertFalse(archive.exists())
            self.assertFalse(entry.exists())
            self.assertFalse(clean_install_handoff_path(home).exists())
            self.assertFalse(vault.exists())

    def test_crash_after_successful_source_unlink_resumes_each_durable_intent_phase(self) -> None:
        for source_name, expected_phase in (("archive.zip", "archive_unlinking"), ("entry.json", "entry_unlinking")):
            with self.subTest(source_name=source_name), tempfile.TemporaryDirectory() as raw:
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
                handoff = clean_install_handoff_path(home)
                victim = vault / source_name
                original_unlink = Path.unlink

                def crash_after_successful_unlink(path: Path, *args: object, **kwargs: object) -> None:
                    original_unlink(path, *args, **kwargs)
                    if path == victim:
                        raise SystemExit(f"simulated crash after {source_name} unlink")

                with mock.patch.object(Path, "unlink", crash_after_successful_unlink):
                    with self.assertRaisesRegex(SystemExit, "simulated crash after"):
                        restore_clean_install_vault(target_state, target_home=home, vault_id=created["vault_id"])

                journal = json.loads((vault / "cleanup.journal.json").read_text(encoding="utf-8"))
                self.assertEqual((journal["cleanup"], journal["phase"]), ("in_progress", expected_phase))
                self.assertFalse(victim.exists())
                self.assertTrue(handoff.exists())
                if source_name == "archive.zip":
                    self.assertTrue(entry.exists())
                else:
                    self.assertFalse(archive.exists())

                resumed = restore_clean_install_vault(target_state, target_home=home, vault_id=created["vault_id"])
                self.assertTrue(resumed["resumed_cleanup"])
                self.assertTrue(resumed["completed"])
                self.assertTrue(resumed["cleanup"]["source_deleted"])
                self.assertFalse(archive.exists())
                self.assertFalse(entry.exists())
                self.assertFalse(handoff.exists())
                self.assertFalse(vault.exists())

    def test_crash_after_handoff_parent_removal_resumes_cleanup_and_allows_next_create(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "install-user"
            home.mkdir()
            source_state = root / "source-state"
            target_state = root / "target-state"
            self._seed_f01(source_state)
            created = create_clean_install_vault(source_state, target_home=home)
            vault = clean_install_vault_dir(home)
            handoff_parent = clean_install_handoff_path(home).parent
            original_rmdir = Path.rmdir

            def crash_after_handoff_parent_removal(path: Path) -> None:
                original_rmdir(path)
                if path == handoff_parent:
                    raise SystemExit("simulated crash after handoff parent removal")

            with mock.patch.object(Path, "rmdir", crash_after_handoff_parent_removal):
                with self.assertRaisesRegex(SystemExit, "handoff parent removal"):
                    restore_clean_install_vault(target_state, target_home=home, vault_id=created["vault_id"])

            self.assertFalse(handoff_parent.exists())
            self.assertTrue((vault / "cleanup.journal.json").is_file())
            self.assertFalse((vault / "archive.zip").exists())
            self.assertFalse((vault / "entry.json").exists())

            resumed = restore_clean_install_vault(target_state, target_home=home, vault_id=created["vault_id"])
            self.assertTrue(resumed["resumed_cleanup"])
            self.assertTrue(resumed["completed"])
            self.assertFalse(vault.exists())

            next_created = create_clean_install_vault(source_state, target_home=home)
            self.assertNotEqual(next_created["vault_id"], created["vault_id"])
            self._assert_consistent_pending_sources(home, str(next_created["vault_id"]))

    def test_terminal_cleanup_failures_are_guarded_resumable_and_allow_next_export(self) -> None:
        """Every fallible terminal boundary leaves a marker or durable guard.

        Once ``archive.zip`` and ``entry.json`` have gone, the vault directory
        itself cannot retain the only recovery marker.  The guard must become
        durable before the final marker unlink and must prevent an export from
        overtaking incomplete cleanup, even if marker recreation would fail.
        """
        for fault in (
            "marker_parent_fsync",
            "guard_write",
            "guard_parent_fsync",
            "vault_rmdir",
            "removed_vault_parent_fsync",
            "marker_unlink",
            "marker_unlink_parent_fsync",
            "crash_after_marker_unlink",
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                home = root / "install-user"
                home.mkdir()
                source_state = root / "source-state"
                target_state = root / "target-state"
                self._seed_f01(source_state)
                created = create_clean_install_vault(source_state, target_home=home)
                vault = clean_install_vault_dir(home)
                terminal_marker = vault.with_name(".clean-install-vault-finalization.json")
                terminal_guard = vault.with_name(".clean-install-vault-finalization.guard.json")

                from gp_control_plane import backups as backups_module

                original_fsync_directory = backups_module._fsync_directory
                original_rmdir = Path.rmdir
                original_unlink = Path.unlink
                original_write_private_json = backups_module._write_private_json_atomic

                def fail_selected_fsync(path: Path) -> None:
                    # The first vault-parent sync makes the moved terminal
                    # marker durable.  The second one follows a successful
                    # vault.rmdir(); distinguish them by the vault's state.
                    if path == vault.parent and (
                        (fault == "marker_parent_fsync" and terminal_marker.is_file() and vault.exists())
                        or (fault == "guard_parent_fsync" and terminal_guard.is_file())
                        or (fault == "removed_vault_parent_fsync" and terminal_guard.is_file() and terminal_marker.is_file() and not vault.exists())
                        or (
                            fault == "marker_unlink_parent_fsync"
                            and not vault.exists()
                            and not terminal_marker.exists()
                            and terminal_guard.is_file()
                        )
                    ):
                        raise OSError(f"simulated {fault}")
                    original_fsync_directory(path)

                def fail_selected_rmdir(path: Path) -> None:
                    if fault == "vault_rmdir" and path == vault:
                        raise OSError("simulated vault rmdir")
                    original_rmdir(path)

                def fail_selected_unlink(path: Path, *args: object, **kwargs: object) -> None:
                    if fault == "marker_unlink" and path == terminal_marker:
                        raise OSError("simulated marker unlink")
                    original_unlink(path, *args, **kwargs)
                    if fault == "crash_after_marker_unlink" and path == terminal_marker:
                        raise SystemExit("simulated crash after marker unlink")

                def fail_selected_guard_write(path: Path, payload: dict[str, object]) -> None:
                    if fault == "guard_write" and path == terminal_guard:
                        raise OSError("simulated terminal guard write failure")
                    original_write_private_json(path, payload)

                with mock.patch.object(backups_module, "_fsync_directory", side_effect=fail_selected_fsync):
                    with mock.patch.object(Path, "rmdir", autospec=True, side_effect=fail_selected_rmdir):
                        with mock.patch.object(Path, "unlink", autospec=True, side_effect=fail_selected_unlink):
                            with mock.patch.object(
                                backups_module,
                                "_write_private_json_atomic",
                                side_effect=fail_selected_guard_write,
                            ):
                                if fault == "crash_after_marker_unlink":
                                    with self.assertRaisesRegex(SystemExit, "marker unlink"):
                                        restore_clean_install_vault(
                                            target_state,
                                            target_home=home,
                                            vault_id=created["vault_id"],
                                        )
                                    interrupted = None
                                else:
                                    interrupted = restore_clean_install_vault(
                                        target_state,
                                        target_home=home,
                                        vault_id=created["vault_id"],
                                    )
                                if fault != "crash_after_marker_unlink":
                                    # A retry while the original failure is
                                    # still present must not export over it.
                                    with self.assertRaisesRegex(RuntimeError, "cleanup is incomplete"):
                                        create_clean_install_vault(source_state, target_home=home)

                if interrupted is not None:
                    self.assertFalse(interrupted["completed"])
                    self.assertFalse(interrupted["cleanup"]["source_deleted"])
                self.assertFalse((vault / "archive.zip").exists())
                self.assertFalse((vault / "entry.json").exists())
                if fault in {"marker_parent_fsync", "guard_write", "vault_rmdir"}:
                    self.assertTrue(terminal_marker.is_file())
                    self.assertTrue(vault.is_dir())
                    if fault == "guard_write":
                        self.assertFalse(terminal_guard.exists())
                    else:
                        self.assertTrue(terminal_guard.is_file())
                else:
                    self.assertTrue(terminal_guard.is_file())
                    guard = json.loads(terminal_guard.read_text(encoding="utf-8"))
                    self.assertEqual(guard["phase"], "marker_unlinking")
                    self.assertFalse(vault.exists())
                    if fault in {"marker_unlink", "guard_parent_fsync", "removed_vault_parent_fsync"}:
                        self.assertTrue(terminal_marker.is_file())
                    else:
                        self.assertFalse(terminal_marker.exists())

                pending = clean_install_vault_info(target_home=home)
                self.assertTrue(pending["exists"])
                self.assertFalse(pending["pending"])
                if terminal_guard.exists():
                    self.assertEqual(pending["cleanup"], "incomplete")
                else:
                    self.assertEqual(pending["cleanup"], "completed")

                resumed = restore_clean_install_vault(
                    target_state,
                    target_home=home,
                    vault_id=created["vault_id"],
                )
                self.assertTrue(resumed["resumed_cleanup"])
                self.assertTrue(resumed["completed"])
                self.assertTrue(resumed["cleanup"]["source_deleted"])
                self.assertFalse(terminal_marker.exists())
                self.assertTrue(terminal_guard.is_file())
                self.assertEqual(json.loads(terminal_guard.read_text(encoding="utf-8"))["phase"], "marker_deleted")
                self.assertFalse(vault.exists())
                self.assertFalse(clean_install_vault_info(target_home=home)["exists"])

                next_created = create_clean_install_vault(source_state, target_home=home)
                self.assertNotEqual(next_created["vault_id"], created["vault_id"])
                self._assert_consistent_pending_sources(home, str(next_created["vault_id"]))
                # A historical ``marker_deleted`` guard must not shadow the
                # newly published pending vault.  Its own complete restore
                # must still verify, consume sources and return the guard to
                # its terminal state.
                self.assertEqual(json.loads(terminal_guard.read_text(encoding="utf-8"))["phase"], "marker_deleted")
                second_target_state = root / "second-target-state"
                second_restored = restore_clean_install_vault(
                    second_target_state,
                    target_home=home,
                    vault_id=next_created["vault_id"],
                )
                self.assertTrue(second_restored["restored"])
                self.assertTrue(second_restored["completed"])
                self.assertTrue(second_restored["cleanup"]["source_deleted"])
                self.assertFalse(clean_install_vault_dir(home).exists())
                self.assertFalse(clean_install_handoff_path(home).exists())
                self.assertTrue(terminal_guard.is_file())
                self.assertEqual(json.loads(terminal_guard.read_text(encoding="utf-8"))["phase"], "marker_deleted")

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
                    )
            vault = clean_install_vault_dir(home)
            self.assertTrue((vault / "archive.zip").is_file())
            self.assertTrue((vault / "entry.json").is_file())
            self.assertTrue(clean_install_handoff_path(home).is_file())

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
                )
            self.assertFalse(target_state.exists())

    def test_consume_requires_durable_verification_and_bound_local_handoff(self) -> None:
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
            handoff = json.loads(clean_install_handoff_path(home).read_text(encoding="utf-8"))["handoff_secret"]
            verification = {"verified": True, "checks": {"semantic": True, "integrity": True}}

            with self.assertRaisesRegex(RuntimeError, "durably verified"):
                _consume_verified_vault(vault, created["vault_id"], handoff, verification, target_home=home)
            self.assertTrue(archive.exists())
            self.assertTrue(entry.exists())

            _mark_vault_verified(vault, _read_vault_entry(vault), verification)
            with self.assertRaisesRegex(RuntimeError, "handoff does not match"):
                _consume_verified_vault(vault, created["vault_id"], "wrong", verification, target_home=home)
            self.assertTrue(archive.exists())
            self.assertTrue(entry.exists())

            consumed = _consume_verified_vault(
                vault,
                created["vault_id"],
                handoff,
                verification,
                target_home=home,
            )
            self.assertTrue(consumed["completed"])
            self.assertTrue(consumed["source_deleted"])
            self.assertFalse(archive.exists())
            self.assertFalse(entry.exists())


if __name__ == "__main__":
    unittest.main()
