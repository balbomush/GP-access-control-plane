from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.backups import (
    clean_install_vault_info,
    create_clean_install_vault,
    create_post_run_snapshot,
    create_snapshot,
    create_snapshot_if_idle,
    delete_snapshot,
    delete_snapshot_if_idle,
    import_snapshot_archive,
    list_snapshots,
    restore_snapshot,
    restore_snapshot_if_idle,
    restore_snapshot_preview,
    snapshot_archive_path,
    snapshot_file_path,
    _write_checksums,
)
from gp_control_plane.state import read_state, write_state
from gp_control_plane.storage import read_app_setting, read_custom_presets, save_app_setting, save_custom_presets
from gp_control_plane.engine_common import parse_blockcheck_stdout, read_candidate_page, upsert_candidates


class BackupTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "vault ownership and modes are POSIX-only")
    def test_clean_install_vault_requires_private_target_user_ownership_and_modes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / "home"
            state_dir = Path(raw) / "state"
            create_clean_install_vault(state_dir, target_home=home)
            vault = home / ".local" / "share" / "gp-control-plane" / "clean-install-vault"
            archive = vault / "archive.zip"

            vault.chmod(0o755)
            with self.assertRaisesRegex(PermissionError, "vault ownership or permissions are unsafe"):
                clean_install_vault_info(target_home=home)
            vault.chmod(0o700)

            archive.chmod(0o640)
            with self.assertRaisesRegex(PermissionError, "archive ownership or permissions are unsafe"):
                clean_install_vault_info(target_home=home)
            archive.chmod(0o600)

            with mock.patch("gp_control_plane.backups._vault.os.getuid", return_value=os.getuid() + 1):
                with self.assertRaisesRegex(PermissionError, "vault ownership or permissions are unsafe"):
                    clean_install_vault_info(target_home=home)

    def test_snapshot_uses_one_read_transaction_during_concurrent_write(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            before = parse_blockcheck_stdout(
                """
* SUMMARY
curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload=tls_client_hello --lua-desync=fake
"""
            )
            after = parse_blockcheck_stdout(
                """
* SUMMARY
curl_test_https_tls12 ipv4 discord.com : nfqws2 --payload=tls_client_hello --lua-desync=multisplit
"""
            )
            upsert_candidates(state_dir, before, {"id": "before"})
            save_custom_presets(state_dir, {"finder": {"before": ["youtube.com"]}, "common": {}}, "2026-08-12T00:00:00Z")

            export_started = threading.Event()
            write_finished = threading.Event()
            writer_errors: list[BaseException] = []

            def writer() -> None:
                if not export_started.wait(timeout=2):
                    writer_errors.append(AssertionError("snapshot export did not begin"))
                    write_finished.set()
                    return
                try:
                    upsert_candidates(state_dir, after, {"id": "after"})
                    save_custom_presets(
                        state_dir,
                        {"finder": {"before": ["youtube.com"], "after": ["discord.com"]}, "common": {}},
                        "2026-08-12T00:00:01Z",
                    )
                except BaseException as exc:  # noqa: BLE001
                    writer_errors.append(exc)
                finally:
                    write_finished.set()

            from gp_control_plane.backups import _export as _backups_export

            original_export_domains = _backups_export._export_domains

            def export_domains_then_write(conn: object, root: Path) -> int:
                count = original_export_domains(conn, root)
                export_started.set()
                self.assertTrue(write_finished.wait(timeout=2), "concurrent writer was blocked by snapshot export")
                return count

            write_thread = threading.Thread(target=writer)
            write_thread.start()
            with mock.patch.object(_backups_export, "_export_domains", side_effect=export_domains_then_write):
                snapshot_id = create_snapshot(state_dir)["snapshot"]["id"]
            write_thread.join(timeout=2)
            self.assertFalse(write_thread.is_alive())
            if writer_errors:
                raise writer_errors[0]

            snapshot_path = state_dir.parent / "backups" / "snapshots" / snapshot_id
            domains = _read_snapshot_ndjson(snapshot_path / "domains" / "domains.ndjson")
            strategies = _read_snapshot_ndjson(snapshot_path / "strategies" / "strategies.ndjson")
            strategy_links = _read_snapshot_ndjson(snapshot_path / "strategies" / "strategy-domain-links.ndjson")
            presets = _read_snapshot_ndjson(snapshot_path / "presets" / "domain-presets.ndjson")
            preset_links = _read_snapshot_ndjson(snapshot_path / "presets" / "preset-domains.ndjson")

            domain_names = {str(item["domain"]) for item in domains}
            strategy_ids = {str(item["id"]) for item in strategies}
            preset_keys = {(str(item["scope"]), str(item["name"]), str(item["kind"])) for item in presets}
            self.assertEqual(domain_names, {"youtube.com"})
            self.assertTrue(all(str(link["domain"]) in domain_names for link in strategy_links))
            self.assertTrue(all(str(link["strategy_id"]) in strategy_ids for link in strategy_links))
            self.assertTrue(all(str(link["domain"]) in domain_names for link in preset_links))
            self.assertTrue(
                all((str(link["scope"]), str(link["name"]), str(link["kind"])) in preset_keys for link in preset_links)
            )

    def test_post_run_snapshot_returns_contractual_success_or_bounded_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"

            success = create_post_run_snapshot(state_dir)

            self.assertEqual(success["kind"], "snapshot")
            self.assertEqual(success["status"], "success")
            self.assertTrue(success["completed_at"].endswith("Z"))
            self.assertEqual(success["snapshot_id"], success["snapshot"]["id"])
            self.assertIsInstance(success["snapshot"], dict)
            with (
                mock.patch("gp_control_plane.backups._snapshots.create_snapshot", side_effect=RuntimeError("x" * 600)),
                mock.patch("gp_control_plane.backups._snapshots.now_iso", return_value="2026-08-12T00:00:00Z"),
            ):
                failure = create_post_run_snapshot(state_dir)
            self.assertEqual(
                failure,
                {
                    "kind": "snapshot",
                    "status": "failed",
                    "completed_at": "2026-08-12T00:00:00Z",
                    "error_code": "snapshot_export_failed",
                    "error_message": "x" * 512,
                },
            )

    def test_snapshot_exports_strategies_and_keeps_last_five(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            parsed = parse_blockcheck_stdout(
                """
* SUMMARY
curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload=tls_client_hello --lua-desync=fake
"""
            )
            upsert_candidates(state_dir, parsed, {"id": "run-1"})

            for index in range(6):
                result = create_snapshot(state_dir)
                self.assertTrue(result["created"], index)

            snapshots = list_snapshots(state_dir)["snapshots"]

            self.assertEqual(len(snapshots), 5)
            self.assertTrue(all(item["checksum_ok"] for item in snapshots))
            latest_id = snapshots[0]["id"]
            archive = snapshot_archive_path(state_dir, latest_id)
            self.assertTrue(archive.is_file())
            strategy_file = state_dir.parent / "backups" / "snapshots" / latest_id / "strategies" / "strategies.ndjson"
            rows = [json.loads(line) for line in strategy_file.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[0]["protocol"], "tls")

    def test_snapshot_if_idle_skips_while_job_running(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            write_state(state_dir, {"current_run_id": "job-1", "last_error": None})

            result = create_snapshot_if_idle(state_dir)

            self.assertFalse(result["created"])
            self.assertTrue(result["queued"])
            self.assertEqual(list_snapshots(state_dir)["snapshots"], [])

    def test_backup_actions_skip_when_runtime_guard_is_busy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            parsed = parse_blockcheck_stdout(
                """
* SUMMARY
curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload=tls_client_hello --lua-desync=fake
"""
            )
            upsert_candidates(state_dir, parsed, {"id": "run-1"})
            snapshot_id = create_snapshot(state_dir)["snapshot"]["id"]
            lock_path = state_dir / "job-runner.lock"
            lock_path.write_text(json.dumps({"pid": os.getpid(), "run_id": "lock-only"}), encoding="utf-8")

            create_result = create_snapshot_if_idle(state_dir)
            delete_result = delete_snapshot_if_idle(state_dir, snapshot_id)
            restore_result = restore_snapshot_if_idle(state_dir, snapshot_id)

            self.assertFalse(create_result["created"])
            self.assertTrue(create_result["queued"])
            self.assertFalse(delete_result["deleted"])
            self.assertTrue(delete_result["queued"])
            self.assertFalse(restore_result["restored"])
            self.assertTrue(restore_result["queued"])
            self.assertEqual(list_snapshots(state_dir)["snapshots"][0]["id"], snapshot_id)

            lock_path.write_text(json.dumps({"pid": 99999999, "run_id": "stale"}), encoding="utf-8")
            create_result = create_snapshot_if_idle(state_dir)

            self.assertTrue(create_result["created"])
            self.assertFalse(lock_path.exists())

    def test_delete_snapshot_removes_files_and_archive(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            parsed = parse_blockcheck_stdout(
                """
* SUMMARY
curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload=tls_client_hello --lua-desync=fake
"""
            )
            upsert_candidates(state_dir, parsed, {"id": "run-1"})
            snapshot_id = create_snapshot(state_dir)["snapshot"]["id"]
            archive = snapshot_archive_path(state_dir, snapshot_id)
            snapshot_path = state_dir.parent / "backups" / "snapshots" / snapshot_id

            result = delete_snapshot(state_dir, snapshot_id)

            self.assertTrue(result["deleted"])
            self.assertFalse(snapshot_path.exists())
            self.assertFalse(archive.exists())
            self.assertEqual(list_snapshots(state_dir)["snapshots"], [])

    def test_delete_snapshot_if_idle_skips_while_job_running(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            parsed = parse_blockcheck_stdout(
                """
* SUMMARY
curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload=tls_client_hello --lua-desync=fake
"""
            )
            upsert_candidates(state_dir, parsed, {"id": "run-1"})
            snapshot_id = create_snapshot(state_dir)["snapshot"]["id"]
            write_state(state_dir, {"current_run_id": "job-1", "last_error": None})

            result = delete_snapshot_if_idle(state_dir, snapshot_id)

            self.assertFalse(result["deleted"])
            self.assertTrue(result["queued"])
            self.assertEqual(list_snapshots(state_dir)["snapshots"][0]["id"], snapshot_id)

    def test_custom_presets_are_exported_to_backup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            saved = save_custom_presets(
                state_dir,
                {"finder": {"mine": ["youtube.com", "discord.com"]}, "common": {"shared": ["youtube.com"]}},
                "2026-06-25T00:00:00Z",
            )

            self.assertEqual(saved["finder"]["mine"], ["youtube.com", "discord.com"])
            self.assertEqual(read_custom_presets(state_dir)["finder"]["mine"], ["youtube.com", "discord.com"])

            result = create_snapshot(state_dir)
            snapshot_id = result["snapshot"]["id"]
            snapshot_path = state_dir.parent / "backups" / "snapshots" / snapshot_id

            self.assertTrue((snapshot_path / "domains" / "domains.ndjson").exists())
            preset_file = snapshot_path / "presets" / "domain-presets.ndjson"
            preset_link_file = snapshot_path / "presets" / "preset-domains.ndjson"
            self.assertTrue(preset_file.exists())
            self.assertTrue(preset_link_file.exists())
            presets = [json.loads(line) for line in preset_file.read_text(encoding="utf-8").splitlines()]
            preset_links = [json.loads(line) for line in preset_link_file.read_text(encoding="utf-8").splitlines()]
            self.assertEqual({item["name"] for item in presets}, {"mine", "shared", "required", "desired"})
            self.assertEqual({item["kind"] for item in presets if item["name"] in {"required", "desired"}}, {"system"})
            self.assertEqual({item["domain"] for item in preset_links}, {"youtube.com", "discord.com"})

    def test_restore_snapshot_replaces_strategies_and_presets(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            first = parse_blockcheck_stdout(
                """
* SUMMARY
curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload=tls_client_hello --lua-desync=fake
"""
            )
            upsert_candidates(state_dir, first, {"id": "run-1"})
            save_custom_presets(
                state_dir,
                {"finder": {"old": ["youtube.com"]}, "common": {}},
                "2026-06-25T00:00:00Z",
            )
            snapshot_id = create_snapshot(state_dir)["snapshot"]["id"]
            second = parse_blockcheck_stdout(
                """
* SUMMARY
curl_test_https_tls12 ipv4 discord.com : nfqws2 --payload=tls_client_hello --lua-desync=multisplit
"""
            )
            upsert_candidates(state_dir, second, {"id": "run-2"})
            save_custom_presets(
                state_dir,
                {"finder": {"new": ["discord.com"]}, "common": {}},
                "2026-06-25T01:00:00Z",
            )

            result = restore_snapshot(state_dir, snapshot_id)
            page = read_candidate_page(state_dir, domain="youtube.com", limit=10)
            discord_page = read_candidate_page(state_dir, domain="discord.com", limit=10)
            presets = read_custom_presets(state_dir)

            self.assertTrue(result["restored"])
            self.assertIsNotNone(result["pre_restore_snapshot"])
            self.assertEqual(page["total"], 1)
            self.assertEqual(discord_page["total"], 0)
            self.assertEqual(presets["finder"], {"old": ["youtube.com"]})
            self.assertEqual(presets["common"], {})
            pre_restore = result["pre_restore_snapshot"]["id"]
            pre_restore_strategy_file = state_dir.parent / "backups" / "snapshots" / pre_restore / "domains" / "domains.ndjson"
            pre_restore_domains = pre_restore_strategy_file.read_text(encoding="utf-8")
            self.assertIn("discord.com", pre_restore_domains)

    def test_snapshot_restore_transfers_app_settings(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            save_app_setting(
                state_dir,
                "run_settings",
                {"curl_parallelism_max": 25, "enable_ipv6": True},
                "2026-07-22T00:00:00Z",
            )
            save_app_setting(
                state_dir,
                "service_settings",
                {"update_channel": "prerelease"},
                "2026-07-22T00:00:01Z",
            )
            snapshot_id = create_snapshot(state_dir)["snapshot"]["id"]
            save_app_setting(
                state_dir,
                "run_settings",
                {"curl_parallelism_max": 7, "enable_ipv6": False},
                "2026-07-22T01:00:00Z",
            )
            save_app_setting(
                state_dir,
                "service_settings",
                {"update_channel": "stable"},
                "2026-07-22T01:00:01Z",
            )
            write_state(state_dir, {"settings": {"curl_parallelism_max": 7, "enable_ipv6": False, "update_channel": "stable"}})

            result = restore_snapshot(state_dir, snapshot_id)

            restored = read_app_setting(state_dir, "run_settings")
            service_restored = read_app_setting(state_dir, "service_settings")
            legacy = read_state(state_dir).get("settings")
            self.assertTrue(result["restored"])
            self.assertEqual(result["settings_count"], 2)
            self.assertEqual(restored["curl_parallelism_max"], 25)
            self.assertTrue(restored["enable_ipv6"])
            self.assertEqual(service_restored["update_channel"], "prerelease")
            self.assertEqual(legacy["curl_parallelism_max"], 25)
            self.assertTrue(legacy["enable_ipv6"])
            self.assertEqual(legacy["update_channel"], "prerelease")

    def test_restore_preview_reports_replaced_and_preserved_entities(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            first = parse_blockcheck_stdout(
                """
* SUMMARY
curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload=tls_client_hello --lua-desync=fake
"""
            )
            upsert_candidates(state_dir, first, {"id": "run-1"})
            snapshot_id = create_snapshot(state_dir)["snapshot"]["id"]
            second = parse_blockcheck_stdout(
                """
* SUMMARY
curl_test_https_tls12 ipv4 discord.com : nfqws2 --payload=tls_client_hello --lua-desync=multisplit
"""
            )
            upsert_candidates(state_dir, second, {"id": "run-2"})
            save_custom_presets(
                state_dir,
                {"finder": {"mine": ["discord.com"]}, "common": {}},
                "2026-06-25T01:00:00Z",
            )
            write_state(state_dir, {"current_run_id": None, "settings": {"enable_ipv6": True}})

            preview = restore_snapshot_preview(state_dir, snapshot_id)
            entities = {item["key"]: item for item in preview["entities"]}

            self.assertTrue(preview["checksum_ok"])
            self.assertTrue(preview["compatible"])
            self.assertEqual(entities["domains"]["current_count"], 2)
            self.assertEqual(entities["domains"]["backup_count"], 1)
            self.assertTrue(entities["domains"]["will_replace"])
            self.assertEqual(entities["strategies"]["current_count"], 2)
            self.assertEqual(entities["strategies"]["backup_count"], 1)
            self.assertTrue(entities["strategy_domain_links"]["will_replace"])
            self.assertTrue(entities["user_presets"]["will_replace"])
            self.assertIn("preset_domain_links", entities)
            self.assertTrue(entities["preset_domain_links"]["will_replace"])
            self.assertTrue(entities["settings"]["will_replace"])

    def test_snapshot_excludes_derived_strategy_stats(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            parsed = parse_blockcheck_stdout(
                """
* SUMMARY
curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload=tls_client_hello --lua-desync=fake
"""
            )
            upsert_candidates(state_dir, parsed, {"id": "run-1"})
            snapshot_id = create_snapshot(state_dir)["snapshot"]["id"]
            snapshot_path = state_dir.parent / "backups" / "snapshots" / snapshot_id
            strategy_file = snapshot_path / "strategies" / "strategies.ndjson"
            link_file = snapshot_path / "strategies" / "strategy-domain-links.ndjson"
            strategy = json.loads(strategy_file.read_text(encoding="utf-8").splitlines()[0])
            link = json.loads(link_file.read_text(encoding="utf-8").splitlines()[0])

            self.assertNotIn("seen_count", strategy)
            self.assertNotIn("common_seen_count", strategy)
            self.assertNotIn("seen_count", link)
            self.assertFalse((snapshot_path / "strategies" / "strategy-stats.ndjson").exists())

    def test_snapshot_file_path_allows_only_snapshot_export_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            parsed = parse_blockcheck_stdout(
                """
* SUMMARY
curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload=tls_client_hello --lua-desync=fake
"""
            )
            upsert_candidates(state_dir, parsed, {"id": "run-1"})
            snapshot_id = create_snapshot(state_dir)["snapshot"]["id"]

            self.assertTrue(snapshot_file_path(state_dir, snapshot_id, "archive").is_file())
            self.assertTrue(snapshot_file_path(state_dir, snapshot_id, "domains/domains.ndjson").is_file())
            self.assertTrue(snapshot_file_path(state_dir, snapshot_id, "settings/app-settings.ndjson").is_file())
            with self.assertRaises(FileNotFoundError):
                snapshot_file_path(state_dir, snapshot_id, "../manifest.json")
            with self.assertRaises(FileNotFoundError):
                snapshot_file_path(state_dir, snapshot_id, "domains/../manifest.json")
            with self.assertRaises(FileNotFoundError):
                snapshot_file_path(state_dir, snapshot_id, "strategies/strategy-stats.ndjson")

    def test_import_snapshot_archive_restores_uploaded_zip(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            parsed = parse_blockcheck_stdout(
                """
* SUMMARY
curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload=tls_client_hello --lua-desync=fake
"""
            )
            upsert_candidates(state_dir, parsed, {"id": "run-1"})
            snapshot_id = create_snapshot(state_dir)["snapshot"]["id"]
            archive = snapshot_archive_path(state_dir, snapshot_id)
            target_state = Path(raw) / "target-state"

            result = import_snapshot_archive(target_state, archive.read_bytes())

            self.assertTrue(result["imported"])
            self.assertEqual(result["snapshot"]["id"], snapshot_id)

    def test_restore_rejects_unsupported_backup_schema(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            parsed = parse_blockcheck_stdout(
                """
* SUMMARY
curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload=tls_client_hello --lua-desync=fake
"""
            )
            upsert_candidates(state_dir, parsed, {"id": "run-1"})
            snapshot_id = create_snapshot(state_dir)["snapshot"]["id"]
            snapshot_path = state_dir.parent / "backups" / "snapshots" / snapshot_id
            manifest_path = snapshot_path / "manifest.json"
            manifest_path.write_text(
                manifest_path.read_text(encoding="utf-8").replace('"schema_version": "7"', '"schema_version": "999"'),
                encoding="utf-8",
            )
            _write_checksums(snapshot_path)

            with self.assertRaisesRegex(ValueError, "unsupported backup schema_version"):
                restore_snapshot(state_dir, snapshot_id)

    def test_restore_schema_5_snapshot_keeps_current_app_settings(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            save_custom_presets(
                state_dir,
                {"finder": {"backup-list": ["youtube.com"]}, "common": {}},
                "2026-06-25T00:00:00Z",
            )
            save_app_setting(
                state_dir,
                "run_settings",
                {"curl_parallelism_max": 25},
                "2026-07-22T00:00:00Z",
            )
            snapshot_id = create_snapshot(state_dir)["snapshot"]["id"]
            snapshot_path = state_dir.parent / "backups" / "snapshots" / snapshot_id
            manifest_path = snapshot_path / "manifest.json"
            manifest_path.write_text(
                manifest_path.read_text(encoding="utf-8").replace('"schema_version": "7"', '"schema_version": "5"'),
                encoding="utf-8",
            )
            _write_checksums(snapshot_path)
            save_custom_presets(
                state_dir,
                {"finder": {"current-list": ["discord.com"]}, "common": {}},
                "2026-06-25T01:00:00Z",
            )
            save_app_setting(
                state_dir,
                "run_settings",
                {"curl_parallelism_max": 7},
                "2026-07-22T01:00:00Z",
            )

            result = restore_snapshot(state_dir, snapshot_id)

            self.assertTrue(result["restored"])
            self.assertEqual(read_custom_presets(state_dir)["finder"], {"backup-list": ["youtube.com"]})
            self.assertEqual(read_app_setting(state_dir, "run_settings")["curl_parallelism_max"], 7)

    def test_restore_validates_snapshot_before_creating_pre_restore_backup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            parsed = parse_blockcheck_stdout(
                """
* SUMMARY
curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload=tls_client_hello --lua-desync=fake
"""
            )
            upsert_candidates(state_dir, parsed, {"id": "run-1"})
            snapshot_id = create_snapshot(state_dir)["snapshot"]["id"]
            snapshot_path = state_dir.parent / "backups" / "snapshots" / snapshot_id
            (snapshot_path / "strategies" / "strategies.ndjson").write_text("not-json\n", encoding="utf-8")
            _write_checksums(snapshot_path)
            count_before = len(list_snapshots(state_dir)["snapshots"])

            with self.assertRaisesRegex(ValueError, "invalid ndjson"):
                restore_snapshot(state_dir, snapshot_id)

            self.assertEqual(len(list_snapshots(state_dir)["snapshots"]), count_before)

    def test_restore_preserves_current_presets_when_backup_preset_files_are_broken(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            parsed = parse_blockcheck_stdout(
                """
* SUMMARY
curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload=tls_client_hello --lua-desync=fake
"""
            )
            upsert_candidates(state_dir, parsed, {"id": "run-1"})
            save_custom_presets(
                state_dir,
                {"finder": {"backup-list": ["youtube.com"]}, "common": {}},
                "2026-06-25T00:00:00Z",
            )
            snapshot_id = create_snapshot(state_dir)["snapshot"]["id"]
            snapshot_path = state_dir.parent / "backups" / "snapshots" / snapshot_id
            manifest_path = snapshot_path / "manifest.json"
            manifest_path.write_text(
                manifest_path.read_text(encoding="utf-8").replace('"schema_version": "7"', '"schema_version": "6"'),
                encoding="utf-8",
            )
            (snapshot_path / "presets" / "domain-presets.ndjson").write_text("not-json\n", encoding="utf-8")
            _write_checksums(snapshot_path)
            save_custom_presets(
                state_dir,
                {"finder": {"current-list": ["discord.com"]}, "common": {}},
                "2026-06-25T01:00:00Z",
            )

            preview = restore_snapshot_preview(state_dir, snapshot_id)
            result = restore_snapshot(state_dir, snapshot_id)
            presets = read_custom_presets(state_dir)

            self.assertFalse({item["key"]: item for item in preview["entities"]}["user_presets"]["will_replace"])
            self.assertTrue(result["restored"])
            self.assertEqual(presets["finder"], {"current-list": ["discord.com"]})

    def test_import_rejects_unsupported_backup_schema(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            parsed = parse_blockcheck_stdout(
                """
* SUMMARY
curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload=tls_client_hello --lua-desync=fake
"""
            )
            upsert_candidates(state_dir, parsed, {"id": "run-1"})
            snapshot_id = create_snapshot(state_dir)["snapshot"]["id"]
            snapshot_path = state_dir.parent / "backups" / "snapshots" / snapshot_id
            manifest_path = snapshot_path / "manifest.json"
            manifest_path.write_text(
                manifest_path.read_text(encoding="utf-8").replace('"schema_version": "7"', '"schema_version": "999"'),
                encoding="utf-8",
            )
            _write_checksums(snapshot_path)
            archive = snapshot_archive_path(state_dir, snapshot_id)

            with self.assertRaisesRegex(ValueError, "unsupported backup schema_version"):
                import_snapshot_archive(Path(raw) / "target-state", archive.read_bytes())

    def test_import_rejects_legacy_yaml_manifest_archive(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            archive = Path(raw) / "legacy.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("legacy-snapshot/manifest.yaml", 'schema_version: "4"\n')

            with self.assertRaisesRegex(ValueError, "unsupported legacy backup format"):
                import_snapshot_archive(Path(raw) / "target-state", archive.read_bytes())

    def test_restore_snapshot_if_idle_skips_while_job_running(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            parsed = parse_blockcheck_stdout(
                """
* SUMMARY
curl_test_https_tls12 ipv4 youtube.com : nfqws2 --payload=tls_client_hello --lua-desync=fake
"""
            )
            upsert_candidates(state_dir, parsed, {"id": "run-1"})
            snapshot_id = create_snapshot(state_dir)["snapshot"]["id"]
            write_state(state_dir, {"current_run_id": "job-1", "last_error": None})

            result = restore_snapshot_if_idle(state_dir, snapshot_id)

            self.assertFalse(result["restored"])
            self.assertTrue(result["queued"])


def _read_snapshot_ndjson(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


if __name__ == "__main__":
    unittest.main()
