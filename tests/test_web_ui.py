from __future__ import annotations

import http.client
import ast
import contextlib
import errno
import importlib
import json
import os
import socket
import socketserver
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.config import AppConfig, InstallConfig, OutputConfig
import gp_control_plane.jobs as jobs
from gp_control_plane.state import read_state, write_state
from gp_control_plane.storage import SCHEMA_VERSION, append_run, read_app_setting
from gp_control_plane.bc2_engine import _process as strategy_finder_process
from gp_control_plane.bc2_engine import _runner as strategy_finder_runner
from gp_control_plane.bc2_engine import _writers as strategy_finder_writers
from gp_control_plane.engine_common import upsert_candidates
from gp_control_plane.web import app as web_app
from gp_control_plane.web import docs as web_docs
from gp_control_plane.web import routes as web_routes
from gp_control_plane.web.app import index_html, serve, serve_core, serve_web_proxy


def _json_object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate OpenAPI key: {key}")
        result[key] = value
    return result


LEGACY_UI_API_MARKERS = (
    "API_ENDPOINTS.legacy",
    "LEGACY_API_ALLOWLIST",
    "legacyEndpoint",
    "legacyUrl",
    "/api/web/bootstrap",
)

LEGACY_API_ROUTE_LITERALS = (
    "/api/status",
    "/api/settings",
    "/api/run-preferences",
    "/api/events",
    "/api/diagnostics",
    "/api/backups/create",
    "/api/backups/list",
    "/api/backups/restore",
    "/api/backups/restore-preview",
    "/api/backups/delete",
    "/api/backups/download",
    "/api/backups/upload",
    "/api/releases",
    "/api/releases/update",
    "/api/releases/update-plan",
    "/api/presets",
    "/api/presets/save",
    "/api/presets/delete-users-lists",
    "/api/presets/domains",
    "/api/presets/domain-enabled",
    "/api/domain-sources",
    "/api/domain-sources/v2fly/preview",
    "/api/domain-sources/v2fly/import",
    "/api/strategy-finder/latest-log",
)


class WebUiTests(unittest.TestCase):
    def assertNoLegacyUiApi(self, html: str) -> None:
        for marker in (*LEGACY_UI_API_MARKERS, *LEGACY_API_ROUTE_LITERALS):
            self.assertNotIn(marker, html)

    def assertApiError(self, payload: dict[str, Any], code: str) -> None:
        self.assertEqual(set(payload), {"error"})
        error = payload["error"]
        self.assertIsInstance(error, dict)
        self.assertEqual(error.get("code"), code)
        self.assertIsInstance(error.get("message"), str)
        self.assertEqual(error.get("details"), {})

    def test_core_status_reports_sqlite_storage_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))

            payload = web_app.core_api.status_payload(config)

            self.assertTrue(payload["storage"]["ready"])
            self.assertEqual(payload["storage"]["schema_version"], SCHEMA_VERSION)
            self.assertNotIn("last_snapshot", payload)

            expected_snapshot = {
                "kind": "snapshot",
                "status": "success",
                "completed_at": "2026-08-12T00:00:00Z",
                "snapshot_id": "snapshot-1",
                "snapshot": {"id": "snapshot-1"},
            }
            write_state(config.output.state_dir, {"last_snapshot": expected_snapshot})

            payload = web_app.core_api.status_payload(config)

            self.assertEqual(payload["last_snapshot"], expected_snapshot)


    def test_core_status_uses_lightweight_storage_runtime_status(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with mock.patch.object(
                web_app.core_api,
                "storage_runtime_status",
                return_value={
                    "ready": True,
                    "schema_version": str(SCHEMA_VERSION),
                    "expected_schema_version": str(SCHEMA_VERSION),
                    "integrity_check": "not_checked",
                },
            ) as storage_status:
                payload = web_app.core_api.status_payload(config)

            storage_status.assert_called_once_with(config.output.state_dir)
            self.assertTrue(payload["storage"]["ready"])
            self.assertEqual(payload["storage"]["schema_version"], SCHEMA_VERSION)

    def test_clean_install_vault_api_never_exposes_known_local_secret_and_restores_by_id_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            created = {
                "vault_id": "a" * 32,
                "handoff_secret": "SAFE-HANDOFF-001-KNOWN-SECRET",
                "archive_sha256": "b" * 64,
                "archive_size_bytes": 12,
                "schema_version": "7",
                "semantic_manifest": {"history_count": 1},
            }
            info = {
                "exists": True,
                "pending": True,
                "vault_id": "a" * 32,
                "created_at": "2026-08-21T12:00:00Z",
                "schema_version": "7",
                "archive_sha256": "b" * 64,
                "archive_size_bytes": 12,
                "verification": "pending",
            }
            restored = {
                "completed": True,
                "vault_id": "a" * 32,
                "verification": {"verified": True, "storage": {"ready": True, "integrity_check": "ok"}},
                "storage_status": {"ready": True, "integrity_check": "ok"},
                "cleanup": {"completed": True, "source_deleted": True},
            }
            with (
                mock.patch.object(web_app.core_api, "create_clean_install_vault", return_value=created),
                mock.patch.object(web_app.core_api, "clean_install_vault_info", return_value=info),
                mock.patch.object(web_app.core_api, "restore_clean_install_vault", return_value=restored) as restore,
            ):
                server = _start_captured_server(serve, config)
                with server:
                    status, _headers, body = _http_request(
                        server.port,
                        "/api/core/clean-install-vaults/create",
                        method="POST",
                        body=b"{}",
                        headers={"Content-Type": "application/json"},
                    )
                    self.assertEqual(status, 201, body)
                    create_payload = json.loads(body)
                    self.assertEqual(
                        set(create_payload),
                        {"vault_id", "archive_sha256", "archive_size_bytes", "schema_version", "semantic_manifest"},
                    )
                    self.assertNotIn("SAFE-HANDOFF-001-KNOWN-SECRET", body.decode("utf-8"))

                    status, _headers, body = _http_request(server.port, "/api/core/clean-install-vaults/list")
                    self.assertEqual(status, 200, body)
                    self.assertNotIn("SAFE-HANDOFF-001-KNOWN-SECRET", body.decode("utf-8"))

                    status, _headers, body = _http_request(
                        server.port, "/api/core/clean-install-vaults/status?vault_id=" + ("a" * 32)
                    )
                    self.assertEqual(status, 200, body)
                    self.assertNotIn("SAFE-HANDOFF-001-KNOWN-SECRET", body.decode("utf-8"))

                    status, _headers, body = _http_request(
                        server.port,
                        "/api/core/clean-install-vaults/restore",
                        method="POST",
                        body=json.dumps({"vault_id": "a" * 32, "confirm_restore": True}).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                    )
                    self.assertEqual(status, 200, body)
                    self.assertNotIn("SAFE-HANDOFF-001-KNOWN-SECRET", body.decode("utf-8"))
                    self.assertTrue(json.loads(body)["completed"])
                    restore.assert_called_once_with(config.output.state_dir, vault_id="a" * 32)

    def test_core_status_keeps_saving_lifecycle_until_post_run_snapshot_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            worker_started = threading.Event()
            release_worker = threading.Event()
            snapshot_started = threading.Event()
            release_snapshot = threading.Event()
            snapshot_finished = threading.Event()

            def active_run(
                _config: AppConfig, _payload: dict[str, Any], _stop_event: threading.Event, _run_id: str
            ) -> dict[str, str]:
                worker_started.set()
                self.assertTrue(release_worker.wait(timeout=2))
                return {"status": "success"}

            def snapshot_after_run(_state_dir: Path) -> dict[str, object]:
                snapshot_started.set()
                self.assertTrue(release_snapshot.wait(timeout=2))
                try:
                    return {
                        "kind": "snapshot",
                        "status": "success",
                        "completed_at": "2026-08-12T00:00:00Z",
                        "snapshot_id": "post-run-snapshot",
                        "snapshot": {"id": "post-run-snapshot"},
                    }
                finally:
                    snapshot_finished.set()

            with (
                mock.patch.object(web_app, "_job_zapret_standard_discovery", side_effect=active_run),
                mock.patch.object(web_app, "create_post_run_snapshot", side_effect=snapshot_after_run),
            ):
                server = _start_captured_server(serve, config)
                with server, _JobRunnerThreadTracker() as runner_threads:
                    runner_threads.release_barrier(release_worker)
                    runner_threads.release_barrier(release_snapshot, "release post-run snapshot barrier")
                    status, _headers, body = _http_request(
                        server.port,
                        "/api/core/strategy-discovery/start-run",
                        method="POST",
                        body=json.dumps({"mode": "standard", "domains": ["youtube.com"], "protocols": ["tcp"]}).encode(
                            "utf-8"
                        ),
                        headers={"Content-Type": "application/json"},
                    )
                    self.assertEqual(status, 202, body.decode("utf-8", errors="replace"))
                    self.assertTrue(worker_started.wait(timeout=2))
                    release_worker.set()
                    self.assertTrue(snapshot_started.wait(timeout=2))

                    status, _headers, body = _http_request(server.port, "/api/core/status")

                    self.assertEqual(status, 200, body.decode("utf-8", errors="replace"))
                    self.assertEqual(json.loads(body.decode("utf-8"))["current_run"]["status"], "saving")
                    self.assertEqual(runner_threads.tracked_count, 1)

                self.assertTrue(snapshot_finished.is_set())
                self.assertEqual(
                    read_state(config.output.state_dir).get("last_snapshot"),
                    {
                        "kind": "snapshot",
                        "status": "success",
                        "completed_at": "2026-08-12T00:00:00Z",
                        "snapshot_id": "post-run-snapshot",
                        "snapshot": {"id": "post-run-snapshot"},
                    },
                )

    def test_active_run_http_contract_keeps_one_public_run_id_for_ui(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            started = threading.Event()
            release = threading.Event()
            snapshot_completed = threading.Event()
            log_dir = tmp / "logs"
            log_dir.mkdir()

            def active_run(_config: AppConfig, _payload: dict[str, Any], stop_event: threading.Event, run_id: str) -> dict[str, str]:
                stdout_log = log_dir / f"{run_id}.stdout.log"
                stdout_log.write_text("active run\n", encoding="utf-8")
                append_run(
                    config.output.state_dir,
                    {
                        "id": run_id,
                        "kind": "zapret-standard-discovery",
                        "status": "running",
                        "timestamp": "2026-08-10T00:00:00Z",
                        "stdout_log": str(stdout_log),
                        "progress": {"phase": "strategy_discovery"},
                    },
                )
                started.set()
                release.wait(timeout=2)
                return {"id": run_id, "status": "stopped" if stop_event.is_set() else "success"}

            def create_snapshot_after_run(_state_dir: Path) -> dict[str, Any]:
                try:
                    return {
                        "kind": "snapshot",
                        "status": "success",
                        "completed_at": "2026-08-12T00:00:00Z",
                        "snapshot_id": "post-run-snapshot",
                        "snapshot": {"id": "post-run-snapshot"},
                    }
                finally:
                    snapshot_completed.set()

            with (
                mock.patch.object(web_app, "_job_zapret_standard_discovery", side_effect=active_run),
                mock.patch.object(web_app, "create_post_run_snapshot", side_effect=create_snapshot_after_run),
            ):
                server = _start_captured_server(serve, config)
                with server, _JobRunnerThreadTracker() as runner_threads:
                    runner_threads.release_barrier(release)
                    status, _headers, body = _http_request(
                        server.port,
                        "/api/core/strategy-discovery/start-run",
                        method="POST",
                        body=json.dumps({"mode": "standard", "domains": ["youtube.com"], "protocols": ["tcp"]}).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                    )
                    self.assertEqual(status, 202, body.decode("utf-8", errors="replace"))
                    accepted = json.loads(body.decode("utf-8"))
                    run_id = accepted["run_id"]
                    self.assertTrue(started.wait(timeout=2))

                    status_payload = json.loads(_http_request(server.port, "/api/core/status")[2].decode("utf-8"))
                    progress_payload = json.loads(
                        _http_request(server.port, "/api/core/strategy-discovery/current-run-progress")[2].decode("utf-8")
                    )
                    current_log_payload = json.loads(
                        _http_request(server.port, "/api/core/strategy-discovery/current-run-latest-log")[2].decode("utf-8")
                    )
                    history_payload = json.loads(_http_request(server.port, "/api/core/runs/history")[2].decode("utf-8"))
                    log_payload = json.loads(
                        _http_request(server.port, f"/api/core/runs/latest-log?run_id={run_id}")[2].decode("utf-8")
                    )
                    stop_status, _stop_headers, stop_body = _http_request(
                        server.port,
                        "/api/core/strategy-discovery/stop-current-run",
                        method="POST",
                        body=json.dumps({"dry_run": True}).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                    )

                    self.assertEqual(status_payload["current_run"], {"run_id": run_id, "status": "running"})
                    self.assertEqual(progress_payload["run_id"], run_id)
                    self.assertEqual(current_log_payload["run_id"], run_id)
                    self.assertEqual(history_payload["runs"][0]["run_id"], run_id)
                    self.assertEqual(log_payload["run_id"], run_id)
                    self.assertEqual(stop_status, 202)
                    self.assertEqual(json.loads(stop_body.decode("utf-8"))["run_id"], run_id)
                    self.assertEqual(runner_threads.tracked_count, 1)

                self.assertIsNone(read_state(config.output.state_dir).get("current_run_id"))
                self.assertTrue(snapshot_completed.is_set())

    def test_current_run_endpoints_do_not_fall_back_to_previous_run_log(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            old_stdout = tmp / "old.stdout.log"
            old_stdout.write_text("old run output\n", encoding="utf-8")
            append_run(
                config.output.state_dir,
                {
                    "id": "old-run",
                    "kind": "zapret-standard-discovery",
                    "status": "success",
                    "timestamp": "2026-08-09T00:00:00Z",
                    "stdout_log": str(old_stdout),
                    "progress": {"phase": "old-phase", "attempts_processed": 42},
                },
            )
            started = threading.Event()
            release = threading.Event()
            worker_finished = threading.Event()
            snapshot_finished = threading.Event()

            def queued_run(
                _config: AppConfig, _payload: dict[str, Any], _stop_event: threading.Event, _run_id: str
            ) -> dict[str, str]:
                started.set()
                try:
                    self.assertTrue(release.wait(timeout=2))
                    return {"status": "success"}
                finally:
                    worker_finished.set()

            def create_snapshot_after_run(_state_dir: Path) -> dict[str, Any]:
                try:
                    return {
                        "kind": "snapshot",
                        "status": "success",
                        "completed_at": "2026-08-12T00:00:00Z",
                        "snapshot_id": "post-run-snapshot",
                        "snapshot": {"id": "post-run-snapshot"},
                    }
                finally:
                    snapshot_finished.set()

            with (
                mock.patch.object(web_app, "_job_zapret_standard_discovery", side_effect=queued_run),
                mock.patch.object(web_app, "create_post_run_snapshot", side_effect=create_snapshot_after_run),
            ):
                server = start_server(serve, config)
                port = server.port
                with _JobRunnerThreadTracker() as runner_threads:
                    runner_threads.release_barrier(release)
                    status, _headers, body = _http_request(
                        port,
                        "/api/core/strategy-discovery/start-run",
                        method="POST",
                        body=json.dumps({"mode": "standard", "domains": ["youtube.com"], "protocols": ["tcp"]}).encode(
                            "utf-8"
                        ),
                        headers={"Content-Type": "application/json"},
                    )
                    self.assertEqual(status, 202, body.decode("utf-8", errors="replace"))
                    current_run_id = json.loads(body.decode("utf-8"))["run_id"]

                    current_progress = json.loads(
                        _http_request(port, "/api/core/strategy-discovery/current-run-progress")[2].decode("utf-8")
                    )
                    current_log = json.loads(
                        _http_request(port, "/api/core/strategy-discovery/current-run-latest-log")[2].decode("utf-8")
                    )
                    history = json.loads(_http_request(port, "/api/core/runs/history")[2].decode("utf-8"))
                    requested_log = json.loads(
                        _http_request(port, f"/api/core/runs/latest-log?run_id={current_run_id}")[2].decode("utf-8")
                    )
                    unknown_log = json.loads(
                        _http_request(port, "/api/core/runs/latest-log?run_id=unknown-run")[2].decode("utf-8")
                    )

                    self.assertEqual(current_progress["run_id"], current_run_id)
                    self.assertEqual(current_progress["stage"], "")
                    self.assertEqual(current_progress["current_file"], "")
                    self.assertNotIn("attempts_processed", current_progress)
                    self.assertEqual(current_log["run_id"], current_run_id)
                    self.assertEqual(current_log["stdout_tail"], "")
                    self.assertEqual(current_log["stderr_tail"], "")
                    self.assertNotEqual(current_log["progress"].get("phase"), "old-phase")
                    self.assertIn(current_run_id, [item["run_id"] for item in history["runs"]])
                    self.assertEqual(requested_log["run_id"], current_run_id)
                    self.assertEqual(requested_log["stdout_tail"], "")
                    self.assertEqual(unknown_log["run_id"], "unknown-run")
                    self.assertEqual(unknown_log["stdout_tail"], "")
                    self.assertTrue(started.wait(timeout=2))
                    self.assertEqual(runner_threads.tracked_count, 1)

                self.assertTrue(worker_finished.is_set())
                self.assertTrue(snapshot_finished.is_set())

    def test_ui_runtime_uses_public_current_run_without_legacy_job_fields(self) -> None:
        html = index_html()

        self.assertIn("function currentRun()", html)
        self.assertIn("const run = (state.status || {}).current_run;", html)
        self.assertIn("return Boolean(currentRun());", html)
        self.assertIn("const jobStatus = currentRun()?.status", html)
        self.assertIn("const runId = response?.run_id || '';", html)
        self.assertNotIn("target_ref: data.update_id || channel,", html)
        self.assertNotIn("current_job", html)
        self.assertNotIn("job_id", html)
        self.assertNotIn("response?.job", html)

    def test_candidates_and_runs_use_50_item_load_more_pagination(self) -> None:
        html = index_html()

        self.assertIn("const LIST_PAGE_LIMIT = 50;", html)
        self.assertIn("const CANDIDATE_PAGE_LIMIT = LIST_PAGE_LIMIT;", html)
        self.assertIn("const DOMAIN_PAGE_LIMIT = LIST_PAGE_LIMIT;", html)
        self.assertIn("const RUN_PAGE_LIMIT = LIST_PAGE_LIMIT;", html)
        self.assertIn("params.set('limit', String(DOMAIN_PAGE_LIMIT));", html)
        self.assertIn("params.set('limit', String(RUN_PAGE_LIMIT));", html)
        self.assertIn("listLoadMore('load-more-candidates'", html)
        self.assertIn("listLoadMore('load-more-candidate-domains'", html)
        self.assertIn("listLoadMore('load-more-runs'", html)
        self.assertIn("loadMoreDomainStrategies", html)
        self.assertIn("loadMoreCommonStrategies", html)
        self.assertNotIn("loadAllDomainStrategies", html)
        self.assertNotIn("loadAllCommonStrategies", html)
        self.assertIn('data-action="${esc(action)}"', html)
        self.assertIn("Загрузить еще", html)
        self.assertIn("refreshDomainIndex(false)", html)
        self.assertIn("refreshRuns(false)", html)
        self.assertIn("const API_ENDPOINTS = Object.freeze", html)
        self.assertNoLegacyUiApi(html)
        self.assertIn("candidateDomainIndexPage: '/api/web/candidate-domain-index-page'", html)
        self.assertIn("strategyCandidatesPage: '/api/web/strategy-candidates-page'", html)
        self.assertIn("runHistoryPage: '/api/web/runs/history-page'", html)
        self.assertIn("latestLog: '/api/core/runs/latest-log'", html)
        self.assertIn("apiUrl('web', 'candidateDomainIndexPage', params)", html)
        self.assertIn("apiUrl('web', 'strategyCandidatesPage', candidateParams", html)
        self.assertIn("apiUrl('web', 'runHistoryPage', runParams", html)
        self.assertIn("apiEndpoint('core', 'latestLog')", html)
        self.assertNotIn("getJson('/api/strategy-finder/latest-log')", html)
        self.assertNotIn(".slice(0, 12)", html)

    def test_refresh_does_not_prefetch_candidates_before_candidates_tab_is_opened(self) -> None:
        html = index_html()

        self.assertIn("if (state.activeTab === 'candidates') ensureCandidateViewLoaded();", html)
        self.assertNotIn("if (!state.candidateDomainsLoaded) refreshDomainIndex();\n    else if", html)

    def test_index_html_is_focused_on_strategy_finder_only(self) -> None:
        html = index_html()

        self.assertIn("<title>GP Control Plane</title>", html)
        self.assertIn("Подбор стратегий zapret2", html)
        self.assertIn("GP Control Plane · локальная web panel · Linux host", html)
        self.assertNotIn("Raspberry Pi · blockcheck2 · live-лог", html)
        self.assertIn('<div class="metric-label">Система</div>', html)
        self.assertIn('<div class="metric-label">Подбор</div>', html)
        self.assertNotIn('<div class="metric-label">zapret2</div>', html)
        self.assertNotIn('<div class="metric-label">Задание</div>', html)
        self.assertIn("nextActionStatus", html)
        self.assertIn("Можно запускать", html)
        self.assertIn("Требуется настройка", html)
        self.assertIn("Есть ошибка", html)
        self.assertIn("Запуск поиска", html)
        self.assertIn("Найденные стратегии", html)
        self.assertIn("История запусков", html)
        self.assertIn("data-tab=\"history\"", html)
        self.assertIn("data-tab-page=\"history\"", html)
        self.assertIn("data-tab=\"candidates\"", html)
        self.assertIn("data-tab=\"terminal\"", html)
        self.assertNotIn("data-tab=\"backups\"", html)
        self.assertNotIn("data-tab-page=\"backups\"", html)
        self.assertIn("data-tab=\"settings\"", html)
        self.assertIn("data-tab-page=\"settings\"", html)
        self.assertIn("Бекапы", html)
        self.assertIn("settings-backups-panel", html)
        self.assertIn("Создать бекап сейчас", html)
        self.assertIn("backups-table", html)
        self.assertIn("refreshBackups", html)
        self.assertIn("/api/core/backups/list", html)
        self.assertIn("/api/core/backups/create", html)
        self.assertIn("/api/core/backups/restore", html)
        self.assertIn("/api/core/backups/delete", html)
        self.assertIn("/api/core/backups/upload", html)
        self.assertIn("/api/core/backups/download-archive", html)
        self.assertNotIn("/api/core/backups/download-file", html)
        self.assertIn("backupDownloadUrl", html)
        self.assertIn("apiUrl('core', 'backupsDownloadArchive', params)", html)
        self.assertIn("backupsDownloadArchive", html)
        self.assertNotIn("backupsDownloadFile", html)
        self.assertIn("backupListFromPayload", html)
        self.assertIn("backup-upload-panel", html)
        self.assertIn("backup-downloads", html)
        self.assertIn("backup-archive-link", html)
        self.assertNotIn("backup-file-links", html)
        self.assertIn("backup-upload-file", html)
        self.assertIn("Vault для чистой установки", html)
        self.assertIn(
            "Создает одну защищенную локальную копию для отдельного сценария чистой установки. "
            "Сервис автоматически сохраняет защищенный device-local handoff вне очищаемых GP-данных; "
            "для восстановления выберите vault и явно подтвердите восстановление с удалением источника.",
            html,
        )
        self.assertIn("clean-install-vaults/create", html)
        self.assertIn("clean-install-vaults/list", html)
        self.assertIn("clean-install-vaults/status", html)
        self.assertIn("clean-install-vaults/restore", html)
        self.assertIn("function createCleanInstallVault()", html)
        self.assertIn("function restoreCleanInstallVault(vaultId)", html)
        self.assertIn("window.confirm(`Восстановить данные из vault ${id}", html)
        self.assertIn(
            "После явного подтверждения восстановление проверит данные и SQLite. "
            "При успехе исходный vault будет удален автоматически.",
            html,
        )
        self.assertIn(
            "Vault создан. После чистой установки выберите его и явно подтвердите восстановление с удалением источника.",
            html,
        )
        self.assertNotIn("window.prompt(", html)
        self.assertNotIn("Токен подтверждения", html)
        self.assertNotIn("одноразовый токен", html)
        self.assertNotIn("confirmation_token", html)
        self.assertNotIn("handoff_secret", html)
        self.assertNotIn("cleanInstallVaultToken", html)
        self.assertNotIn("postJson('/api/backups/create'", html)
        self.assertNotIn("postJson('/api/backups/restore'", html)
        self.assertNotIn("postJson('/api/backups/delete'", html)
        self.assertNotIn("fetch('/api/backups/upload'", html)
        self.assertIn("app-version-badge", html)
        self.assertIn("authFetch", html)
        self.assertIn("Authorization: `Bearer ${token}`", html)
        self.assertIn("requestUrl", html)
        self.assertIn("AUTH_TOKEN_KEY", html)
        self.assertNotIn("authUrl", html)
        self.assertNotIn("web-auth-badge", html)
        self.assertNotIn("WEB_AUTH", html)
        self.assertNotIn("X-GP-Token", html)
        self.assertNotIn("gp_token", html)
        self.assertNotIn("settings-version", html)
        self.assertIn("settings-enable-ipv6", html)
        self.assertIn("settings-debug-stdout", html)
        self.assertIn("settings-discovery-panel", html)
        self.assertIn("Подробный debug-лог stdout", html)
        self.assertIn("может увеличить запись на диск", html)
        self.assertIn("settings-release-panel", html)
        self.assertIn("settings-release-current", html)
        self.assertIn("settings-release-stable", html)
        self.assertIn("settings-release-prerelease", html)
        self.assertIn("release-version-link", html)
        self.assertIn("data-action=\"check-releases\"", html)
        self.assertIn("/api/service/releases/available", html)
        self.assertNotIn("/api/service/releases/install-channel", html)
        self.assertNotIn("/api/service/releases/set-install-channel", html)
        self.assertNotIn("/api/service/releases/install-plan", html)
        self.assertNotIn("/api/service/releases/install", html)
        self.assertNotIn("getJson(`/api/releases?", html)
        self.assertNotIn("postJson('/api/releases/update'", html)
        self.assertNotIn("releaseUpdate", html)
        self.assertIn("checkReleases({ silent: true })", html)
        self.assertIn("чистая установка по точному тегу", html)
        self.assertNotIn("Релизы еще не проверялись. Обновление из UI", html)
        self.assertNotIn("Установить выбранное обновление", html)
        self.assertIn("debug_stdout", html)
        self.assertIn("settings-discovery-engine", html)
        self.assertIn("finder-discovery-engine", html)
        self.assertIn("export-nfconf", html)
        self.assertIn("/api/core/strategy-discovery/export-nfconf", html)
        self.assertIn("/api/core/run-settings", html)
        self.assertIn("/api/core/run-settings/save", html)
        self.assertIn("fetchSettingsPayload", html)
        self.assertIn("saveSettingsPayload", html)
        self.assertNotIn("postJson('/api/settings'", html)
        self.assertNotIn("getJson('/api/settings'", html)
        self.assertNotIn("/api/discovery-profiles", html)
        self.assertIn("discovery-profile-select", html)
        self.assertIn("DISCOVERY_PROFILES", html)
        self.assertNotIn("settings-preset-select", html)
        self.assertNotIn("settings-preset-note", html)
        self.assertIn("/api/web/run-preferences", html)
        self.assertIn("runPreferences", html)
        self.assertNotIn("postJson('/api/run-preferences'", html)
        self.assertIn("useRunPreferencesOnce", html)
        self.assertIn("saveRunPreferencesNow", html)
        self.assertNotIn("scheduleRunPreferencesSave", html)
        self.assertNotIn("settings-default-settings-preset", html)
        self.assertNotIn("SETTINGS_PRESETS", html)
        self.assertNotIn("setSettingsPreset", html)
        self.assertIn("run-selected-discovery", html)
        self.assertIn("Все домены на одной стратегии", html)
        self.assertIn("useDiscoveryProfile", html)
        self.assertNotIn("discovery-profile-name", html)
        self.assertNotIn("saveDiscoveryProfile", html)
        self.assertNotIn("deleteDiscoveryProfile", html)
        self.assertIn("/api/core/presets/v2fly/categories", html)
        self.assertIn("/api/core/presets/v2fly/category-domains", html)
        self.assertIn("apiUrl('core', 'v2flyCategories', params)", html)
        self.assertIn("apiUrl('core', 'v2flyCategoryDomains', params)", html)
        self.assertIn("apiEndpoint('web', 'presetSave')", html)
        self.assertNotIn("getJson(`/api/domain-sources/v2fly/categories?", html)
        self.assertIn("v2fly-category-search", html)
        self.assertIn("v2fly-category-status", html)
        self.assertIn("v2fly-category-matches", html)
        self.assertIn("v2fly-domains", html)
        self.assertIn("suggestV2flyPresetName", html)
        self.assertIn("data-action=\"v2fly-load-categories\"", html)
        self.assertIn("data-action=\"v2fly-select-category\"", html)
        self.assertIn("v2fly-preset-name", html)
        self.assertIn("v2fly-preview-result", html)
        self.assertIn("не гарантия полного покрытия сервиса", html)
        self.assertIn("renderV2flyCategoryCatalog", html)
        self.assertIn("loadV2flyCategories", html)
        self.assertIn("previewV2flyPreset", html)
        self.assertIn("importV2flyPreset", html)
        self.assertIn("setV2flyLocalError", html)
        self.assertIn("function clearV2flyDomains", html)
        self.assertIn("clearV2flyDomains();", html)
        self.assertIn("Локальный каталог v2fly", html)
        self.assertNotIn("params.set('check'", html)
        self.assertNotIn("params.set('refresh'", html)
        self.assertNotIn("Ошибка проверки v2fly: ${error.message}`, 'bad'", html)
        self.assertNotIn("Ошибка сохранения v2fly: ${error.message}`, 'bad'", html)
        self.assertIn("state.presetManager.name = payload.name;", html)
        self.assertIn("await loadPresetEditorFromSelection({ silent: true });", html)
        self.assertNotIn("v2fly-scope", html)
        self.assertNotIn("v2fly-categories", html)
        self.assertNotIn("v2fly-category-list", html)
        self.assertNotIn("data-v2fly-category", html)
        self.assertNotIn("domain/full", html)
        self.assertIn("uniqueDomainCount", html)
        self.assertNotIn("backup-file-links", html)
        self.assertIn("Восстановить из бекапа", html)
        self.assertIn("Удалить бекап", html)
        self.assertIn("data-backup-restore", html)
        self.assertIn("data-backup-delete", html)
        self.assertNotIn("backup-restore-select", html)
        self.assertNotIn("backup-restore-preview", html)
        self.assertNotIn("data-action=\"restore-selected-backup\"", html)
        self.assertIn("restoreBackup", html)
        self.assertIn("deleteBackup", html)
        self.assertNotIn("refreshBackupRestorePreview", html)
        self.assertNotIn("renderBackupRestorePreview", html)
        self.assertIn("/api/web/presets", html)
        self.assertIn("/api/web/presets/save", html)
        self.assertIn("/api/web/presets/delete-user-lists", html)
        self.assertIn("/api/web/presets/domains", html)
        self.assertIn("apiEndpoint('web', 'presets')", html)
        self.assertIn("apiEndpoint('web', 'presetSave')", html)
        self.assertIn("apiEndpoint('web', 'presetDeleteUserLists')", html)
        self.assertIn("apiUrl('web', 'presetDomains', params)", html)
        self.assertIn("domain-preset-manager-panel", html)
        self.assertNotIn("profiles-manager-panel", html)
        self.assertNotIn("settings-presets-manager-panel", html)
        self.assertNotIn("preset-manager-scope", html)
        self.assertIn("preset-manager-name", html)
        self.assertNotIn("preset-manager-query", html)
        self.assertNotIn("preset-domain-list", html)
        self.assertNotIn("preset-domain-row", html)
        self.assertNotIn("preset-editor-name", html)
        self.assertIn("preset-editor-domains", html)
        self.assertIn("preset-editor-preview", html)
        self.assertIn("preset-new-name", html)
        self.assertIn("preset-new-domains", html)
        self.assertIn("data-action=\"preset-new-save\"", html)
        self.assertIn("data-action=\"preset-editor-delete\"", html)
        self.assertIn("savePresetNew", html)
        self.assertIn("deletePresetEditor", html)
        self.assertIn("Создать новый список", html)
        self.assertIn("system:required", html)
        self.assertIn("systemPresets", html)
        self.assertIn("systemPresetMeta", html)
        self.assertIn("fetchAllPresetDomains", html)
        self.assertIn("function hasCustomPreset(target, name)", html)
        self.assertIn("function managerPresetEntries()", html)
        self.assertIn("function managerPresetEntry(name)", html)
        self.assertIn("const builtin = builtInPresets(target).find((item) => item.key === name);", html)
        self.assertIn("if (builtin) return uniqueDomains(builtin.domains);", html)
        self.assertNotIn("refreshPresetManager", html)
        self.assertNotIn("togglePresetDomain", html)
        self.assertIn("loadPresetEditorFromSelection", html)
        self.assertNotIn("previewPresetEditor", html)
        self.assertIn("savePresetEditor", html)
        self.assertIn("exportPresetEditor", html)
        self.assertIn("customPresetMeta", html)
        self.assertNotIn("Показать домены", html)
        self.assertNotIn("Показать изменения", html)
        self.assertIn("Скачать TXT", html)
        self.assertIn("candidateGroups(rows)", html)
        self.assertIn("data-candidate-view=\"domain\"", html)
        self.assertIn("data-candidate-view=\"common\"", html)
        self.assertIn("domain-group", html)
        self.assertIn("protocol-group", html)
        self.assertIn("domain-strategy-box", html)
        self.assertIn("strategy-editor", html)
        self.assertIn("strategy-code", html)
        self.assertIn("line-numbers", html)
        self.assertIn("line-numbered-textarea", html)
        self.assertIn("text-editor", html)
        self.assertIn("STRATEGY_LIST_LIMIT", html)
        self.assertIn("expandedStrategyLists", html)
        self.assertIn("strategyEditorScrolls", html)
        self.assertIn("strategyListState", html)
        self.assertIn("normalizeStrategyArg", html)
        self.assertNotIn("FRAGMENTATION_CLASSES", html)
        self.assertNotIn("candidate-filter-row", html)
        self.assertNotIn("candidate-hide-risky", html)
        self.assertNotIn("data-fragmentation-class=\"position_risky\"", html)
        self.assertNotIn("fragmentationBadge", html)
        self.assertIn("strategyFamilyGroups", html)
        self.assertIn("strategyDisplayFamilyKey", html)
        self.assertIn("return `${protocol}:${family}`;", html)
        self.assertIn("strategy-family-list", html)
        self.assertIn("strategy-family-reason", html)
        self.assertNotIn("appendCandidateFilters", html)
        self.assertIn("loadMoreDomainStrategies", html)
        self.assertIn("loadMoreCommonStrategies", html)
        self.assertNotIn("loadAllDomainStrategies", html)
        self.assertNotIn("loadAllCommonStrategies", html)
        self.assertIn("Загрузить еще общие стратегии", html)
        self.assertIn("domainFromStrategyListKey", html)
        self.assertIn("isCommonStrategyListKey", html)
        self.assertIn("Загрузить еще стратегии домена", html)
        self.assertIn("data-strategy-list-toggle", html)
        self.assertIn("data-strategy-remote-more", html)
        self.assertIn("button.dataset.strategyRemoteMore === 'true'", html)
        self.assertIn("data-strategy-code-key", html)
        self.assertIn("rememberStrategyEditorScrolls", html)
        self.assertIn("restoreStrategyEditorScrolls", html)
        self.assertIn("strategyEditorScrollKey", html)
        self.assertIn("updateEditorLineNumbers", html)
        self.assertIn("data-line-numbers-for=\"finder-domains\"", html)
        self.assertIn("data-line-numbers-for=\"common-domains\"", html)
        self.assertIn("color-scheme: dark", html)
        self.assertIn("#161c27", html)
        self.assertIn("#1b2434", html)
        self.assertIn("#0097dc", html)
        self.assertIn("dynamicCommonRows", html)
        self.assertIn("selectedFinderDomains", html)
        self.assertIn("selectedCommonDomains", html)
        self.assertIn("candidateKnownVersion", html)
        self.assertIn("candidateCacheValid", html)
        self.assertIn("syncCandidateVersion", html)
        self.assertIn("Object.keys(value).sort().map", html)
        self.assertNotIn("value.mtime_ns || 0", html)
        self.assertIn("invalidateCandidateCaches", html)
        self.assertIn("common-controls", html)
        self.assertIn(".common-filter-panel .preset-grid", html)
        self.assertIn("common-domains", html)
        self.assertIn("tested-domain-options", html)
        self.assertIn("common-domain-add", html)
        self.assertIn("common-domain-suggestions", html)
        self.assertIn("data-common-domain-suggestion", html)
        self.assertIn("commonDomainSuggestions", html)
        self.assertIn("renderCommonDomainSuggestions", html)
        self.assertIn("finder-preset-select", html)
        self.assertIn("common-preset-select", html)
        self.assertIn("CUSTOM_SELECT_VALUE", html)
        self.assertIn("markDomainPresetCustom", html)
        self.assertIn("markDiscoveryProfileCustom", html)
        self.assertIn("discovery-profile-note", html)
        self.assertIn("multi-curl-field", html)
        self.assertIn("zapretCompactStatus", html)
        self.assertIn("compact-status", html)
        self.assertIn("optgroup label=\"Персональные\"", html)
        self.assertIn("label: 'Обязательные'", html)
        self.assertIn("label: 'Сервисы'", html)
        self.assertIn("label: 'Готовые наборы'", html)
        self.assertNotIn("label: 'Диагностика'", html)
        self.assertIn("label: 'Протестированные'", html)
        self.assertNotIn("data-preset-use=\"finder\"", html)
        self.assertNotIn("data-preset-use=\"common\"", html)
        self.assertNotIn("data-action=\"use-discovery-profile\"", html)
        self.assertNotIn("data-preset-save=\"common\"", html)
        self.assertNotIn("data-preset-delete=\"common\"", html)
        self.assertIn("CUSTOM_PRESETS_KEY", html)
        self.assertIn("localStorage", html)
        self.assertIn("testedDomains()", html)
        self.assertIn("domainsTouched", html)
        self.assertIn("domainsInitialized", html)
        self.assertIn("id=\"toast\"", html)
        self.assertIn("showToast", html)
        self.assertIn("progress-fill", html)
        self.assertIn("progress-attempted", html)
        self.assertIn("progress-strategies", html)
        self.assertIn("progress-successful", html)
        self.assertIn("progress-phase", html)
        self.assertIn("progress-elapsed", html)
        self.assertIn("progress-metrics", html)
        self.assertIn("phaseLabel", html)
        self.assertIn("renderRunSettingsSummary", html)
        self.assertIn("run_settings", html)
        self.assertIn("attempt_total", html)
        self.assertIn("strategy_total", html)
        self.assertIn("eta_estimate_ms_per_attempt", html)
        self.assertIn("расчитанное среднее время попытки", html)
        self.assertNotIn("curl: ${processes.curl", html)
        self.assertIn("progressLiveElapsedSeconds(progress)", html)
        self.assertIn("progressLiveEtaSeconds(progress)", html)
        self.assertIn("runCandidateCount(row)", html)
        self.assertIn("runProgressText(row)", html)
        self.assertIn("data-action=\"run-selected-discovery\"", html)
        self.assertNotIn("data-action=\"multi-domain-discovery\"", html)
        self.assertNotIn("data-action=\"standard-discovery\"", html)
        self.assertIn("/api/core/strategy-discovery/start-run", html)
        self.assertIn("/api/core/strategy-discovery/stop-current-run", html)
        self.assertIn("coreStrategyDiscoveryPayload", html)
        self.assertIn("startStrategyDiscoveryRun", html)
        self.assertNotIn("/api/jobs/zapret-multi-domain-discovery", html)
        self.assertNotIn("/api/jobs/zapret-standard-discovery", html)
        self.assertNotIn("/api/jobs/stop-current", html)
        self.assertIn("curl-parallelism", html)
        self.assertNotIn('id="curl-parallelism" type="number" min="1" max="10"', html)
        self.assertNotIn("id=\"settings-curl-max\" type=\"number\" min=\"1\" max=\"10\"", html)
        self.assertIn("Можно ставить любое число от 1", html)
        self.assertIn("value=\"4\"", html)
        self.assertIn("curlParallelism()", html)
        self.assertIn("curl_parallelism", html)
        self.assertIn("enable-http", html)
        self.assertIn("enable-tls12", html)
        self.assertIn("enable-tls13", html)
        self.assertIn("include-quic", html)
        self.assertIn("scan-level", html)
        self.assertIn("repeats", html)
        self.assertIn("repeat-parallel", html)
        self.assertIn("skip-dnscheck", html)
        self.assertIn("skip-ipblock", html)
        self.assertIn("Расширенные параметры", html)
        self.assertIn("run-curl-max-time", html)
        self.assertIn("run-curl-max-time-quic", html)
        self.assertIn("run-curl-max-time-doh", html)
        self.assertIn("runTimeoutSettings()", html)
        self.assertIn("RUN_TIMEOUT_CONTROL_IDS", html)
        self.assertIn("saveLaunchTimeoutDefaultsNow", html)
        self.assertNotIn("settings-curl-max-time", html)
        self.assertNotIn("settings-curl-max-time-quic", html)
        self.assertNotIn("settings-curl-max-time-doh", html)
        self.assertIn("discoveryOptions()", html)
        self.assertIn("hasEnabledProtocol", html)
        self.assertIn("enable_http", html)
        self.assertIn("enable_tls12", html)
        self.assertIn("enable_tls13", html)
        self.assertIn("scan_level", html)
        self.assertIn("repeat_parallel", html)
        self.assertIn("skip_dnscheck", html)
        self.assertIn("skip_ipblock", html)
        self.assertNotIn("Пресет настроек", html)
        self.assertIn("limit-time-enabled", html)
        self.assertIn("time-limit-panel", html)
        self.assertIn("time-limit-field", html)
        self.assertIn("timeoutSecondsOrNull", html)
        self.assertIn("syncTimeLimitUi()", html)
        self.assertIn("data-tooltip=\"Запускает штатную проверку стратегий", html)
        self.assertIn("data-tooltip=\"Одна стратегия запускается один раз", html)
        self.assertIn("grid-template-columns: minmax(0, 460px) minmax(0, 1fr)", html)
        self.assertIn("grid-template-columns: minmax(0, 1fr)", html)
        self.assertNotIn(".finder-layout {\n  grid-template-columns: minmax(0, 520px);\n  max-width: 560px;\n}", html)
        self.assertIn("grid-template-columns: repeat(auto-fit, minmax(180px, 1fr))", html)
        self.assertIn("grid-template-columns: repeat(auto-fit, minmax(150px, 1fr))", html)
        self.assertIn(".time-limit-row { grid-template-columns: 1fr; }", html)
        self.assertIn("class=\"button-row run-actions\"", html)
        self.assertIn("min-width: 760px", html)
        self.assertIn("table-layout: auto", html)
        self.assertIn(".run-history", html)
        self.assertIn(".run-card-main", html)
        self.assertIn(".run-card-actions", html)
        self.assertIn(".run-card-status-success", html)
        self.assertIn(".run-card-status-timeout", html)
        self.assertIn(".run-card-kind-multi", html)
        self.assertIn("runSettingsText", html)
        self.assertIn("runPayload", html)
        self.assertIn("repeatRun", html)
        self.assertIn("data-run-repeat", html)
        self.assertIn("Повторить с этими настройками", html)
        self.assertIn(".run-domain-list", html)
        self.assertIn(".run-domain-chip", html)
        self.assertIn(".run-domains-preview", html)
        self.assertIn(".run-domains-count", html)
        self.assertIn(".run-domains-arrow", html)
        self.assertIn(".run-domains[open]", html)
        self.assertIn("openRunDomains", html)
        self.assertIn("data-run-domains", html)
        self.assertIn("renderRunCard(row)", html)
        self.assertIn("runCardClass(row)", html)
        self.assertIn("runDomainKey(row)", html)
        self.assertIn("служба с повышенными правами", html)
        self.assertIn("metric-job-card", html)
        self.assertIn("/api/web/events/stream", html)
        self.assertIn("authFetch(apiEndpoint('web', 'eventsStream')", html)
        self.assertIn("response.body.getReader()", html)
        self.assertNotIn("new EventSource(", html)
        self.assertIn("startRealtimeEvents", html)
        self.assertIn("startRealtimeFallback", html)
        self.assertIn("stdout_size", html)
        self.assertIn("mergeLogPayload", html)
        self.assertIn("30000", html)
        self.assertIn("refresh({ light: true, silent: true })", html)
        self.assertIn("function refreshRequestMap(light)", html)
        self.assertIn("Promise.allSettled", html)
        self.assertIn("const status = settledValue(results, 'status');", html)
        self.assertIn("if (status) mergeStatusPayload(status);", html)
        self.assertIn("const finderRuns = settledValue(results, 'finderRuns');", html)
        self.assertIn("if (finderRuns) mergeRunPage(finderRuns, true);", html)
        self.assertIn("refreshFailureMessages(results)", html)
        self.assertIn("'Частичная ошибка обновления'", html)
        self.assertIn("renderAll({ skipCandidates: true })", html)
        self.assertNotIn("setInterval(refresh, 5000)", html)
        self.assertNotIn(
            "const [status, finderRuns, finderLog, domainSets, presets, settings, domainSources] = await Promise.all",
            html,
        )
        self.assertIn("statusCheck", html)
        self.assertIn("zapretDiagnostics", html)
        self.assertIn("status-check-message", html)
        self.assertIn("testedDomainCount", html)
        self.assertIn("lastCandidateDomainTotal", html)
        self.assertNotIn("metric-last-run", html)
        self.assertNotIn("Последний запуск", html)
        self.assertNotIn("data-action=\"refresh\"", html)
        self.assertNotIn("Обновить данные", html)
        self.assertIn("runStatusLabel(status)", html)
        self.assertIn("metricJobNoteText(ready, busy, jobStatus, status)", html)
        self.assertNotIn("jobDetails.push", html)
        self.assertNotIn("этап: ${phase}", html)
        self.assertIn("etaModeLabel(progress)", html)
        self.assertIn("loading-skeleton", html)
        self.assertIn("candidateLoading", html)
        self.assertIn("candidateUpdatedAt", html)
        self.assertIn("backupsLoading", html)
        self.assertIn("backupsUpdatedAt", html)
        self.assertNotIn("renderWebAuthStatus", html)
        self.assertIn("friendlyTime", html)
        self.assertIn("backups-updated-at", html)
        self.assertIn("останавливается", html)
        self.assertIn("остановлено", html)
        self.assertIn("сохраняются результаты", html)
        self.assertIn("ошибка сохранения", html)
        self.assertIn("runDomains(row, domainKey)", html)
        self.assertIn("runDomainChips(domains)", html)
        self.assertIn("runDiagnosticsSummary(row)", html)
        self.assertIn("runDiagnostics(row)", html)
        self.assertIn(".run-diagnostics", html)
        self.assertIn(".run-diagnostic-table", html)
        self.assertIn("diagnosticTableRow", html)
        self.assertIn("diagnosticShortLabel", html)
        self.assertIn("diagnosticExplanation", html)
        self.assertIn("curlCodeLabel", html)
        self.assertIn("curlCodeDetails", html)
        self.assertIn(".run-diagnostic-tech", html)
        self.assertIn("технически", html)
        self.assertIn("Это не отменяет найденные стратегии", html)
        self.assertNotIn(".run-diagnostic-chip", html)
        self.assertNotIn("Коды curl показывают", html)
        self.assertIn("details.run-domains[data-run-domains]", html)
        self.assertIn("white-space: nowrap", html)
        self.assertIn("isDiscoveryRun(row)", html)
        self.assertIn("runMode(row)", html)
        self.assertIn("data-action=\"stop-current\"", html)
        self.assertIn("Терминал", html)
        self.assertIn("scrollLogToBottom", html)
        self.assertIn("state.activeTab === 'terminal'", html)
        self.assertIn("runSummary(row)", html)
        self.assertIn("latestById", html)
        self.assertNotIn("Задания подбора", html)
        self.assertNotIn("Запуски с находками", html)
        self.assertNotIn("candidate-runs-table", html)
        self.assertNotIn("jobs-table", html)
        self.assertNotIn("table('finder-runs-table'", html)
        self.assertNotIn("jobSummary(row)", html)
        self.assertNotIn("effectiveJobStatus(row)", html)
        self.assertNotIn("{label: 'Лог'", html)
        self.assertNotIn("{label: 'Детали'", html)
        self.assertNotIn("JSON.stringify(row.result)", html)
        self.assertNotIn("data-candidate-verify", html)
        self.assertNotIn("candidateCopyGroups", html)
        self.assertNotIn("registerCopyText", html)
        self.assertNotIn("strategy-textarea", html)
        self.assertNotIn("strategyText", html)
        self.assertNotIn("strategyItem", html)
        self.assertNotIn("strategy-item", html)
        self.assertNotIn("data-copy-scope", html)
        self.assertNotIn("data-copy-candidate-id", html)
        self.assertNotIn("copyTextForButton", html)
        self.assertNotIn("copy-fallback", html)
        self.assertNotIn("showCopyFallback", html)
        self.assertNotIn("Копировать группу", html)
        self.assertNotIn("Копировать стратегию", html)
        self.assertNotIn("Копировать домен", html)
        self.assertNotIn("candidate-message", html)
        self.assertNotIn("setCandidateMessage", html)
        self.assertNotIn("<code>nfqws2", html)
        self.assertNotIn("{label: 'ID'", html)
        self.assertNotIn("{label: 'Найдено'", html)
        self.assertNotIn("Синхронизировать", html)
        self.assertNotIn("dry-run", html)
        self.assertNotIn("Браузер заблокировал буфер", html)
        self.assertNotIn("Проверка домена", html)
        self.assertNotIn("Проверки доступности", html)
        self.assertNotIn("Технические данные", html)
        self.assertNotIn("Фильтр по домену", html)
        self.assertNotIn("Показать еще стратегии этого домена", html)
        self.assertNotIn("data-domain-load-more", html)
        self.assertNotIn("/api/rules", html)
        self.assertNotIn("/api/strategies", html)
        self.assertNotIn("/api/healthchecks", html)
        self.assertNotIn("/api/jobs/validate", html)
        self.assertNotIn("/api/jobs/sync-pull-only", html)
        self.assertNotIn("/api/jobs/render-dry-run", html)
        self.assertNotIn("/api/jobs/healthcheck-direct", html)
        self.assertNotIn("/api/jobs/zapret-strategy-check", html)
        self.assertNotIn("/api/jobs/zapret-custom-verification", html)

    def test_common_tested_preset_waits_for_loaded_tested_domains(self) -> None:
        html = index_html()

        preset_start = html.index("function presetGroups(target)")
        preset_end = html.index("function presetDomains(target, value)", preset_start)
        preset_html = html[preset_start:preset_end]
        self.assertIn("const tested = testedDomains();", preset_html)
        self.assertIn("if (tested.length)", preset_html)

        select_start = html.index("function renderPresetSelect(target)")
        select_end = html.index("function renderPresetSelects()", select_start)
        select_html = html[select_start:select_end]
        self.assertIn("else if (target === 'common') select.value = CUSTOM_SELECT_VALUE;", select_html)
        self.assertNotIn("target === 'common' && [...select.options].some((option) => option.value === 'builtin:tested')", select_html)
        self.assertIn("function updateTestedDomains(domains)", html)
        self.assertIn("updateTestedDomains(data.tested_domains)", html)
        self.assertIn("renderPresetSelect('common')", html)

    def test_launch_summary_panel_is_next_to_start_actions(self) -> None:
        html = index_html()

        summary_start = html.index('class="run-launch-summary"')
        actions_start = html.index('class="button-row run-actions"')
        self.assertLess(summary_start, actions_start)
        self.assertIn('aria-label="Сводка параметров запуска"', html)
        self.assertIn("run-launch-readiness", html)
        self.assertIn("run-launch-summary-grid", html)
        self.assertIn("Параметры запуска", html)

    def test_launch_summary_shows_result_affecting_parameters(self) -> None:
        html = index_html()

        for label in (
            "Домены запуска",
            "Обязательные",
            "Желательные",
            "Источник",
            "Режим",
            "Проверочные запросы",
            "Протоколы",
            "IP-режим",
            "Глубина",
            "DNS/IP-check",
            "Повторы",
            "Лимит времени",
            "Таймауты",
        ):
            self.assertIn(label, html)
        self.assertIn("selectedFinderPresetSummary", html)
        self.assertIn("selectedRunModeLabel", html)
        self.assertIn("protocolSummary(options)", html)
        self.assertIn("curlParallelism()", html)
        self.assertIn("timeoutSecondsOrNull()", html)

    def test_launch_summary_has_explicit_readiness_states(self) -> None:
        html = index_html()

        self.assertIn("runLaunchReadiness", html)
        self.assertIn("Готово к старту", html)
        self.assertIn("Требуется настройка", html)
        self.assertIn("Нужны домены", html)
        self.assertIn("Нужен протокол", html)
        self.assertIn("Идет подбор", html)
        self.assertIn("hasEnabledProtocol(options)", html)

    def test_curl_parallelism_field_is_scoped_to_multi_domain_mode(self) -> None:
        html = index_html()

        self.assertIn("[hidden] { display: none !important; }", html)
        self.assertIn('<div class="field multi-curl-field" id="multi-curl-field" hidden>', html)
        field_start = html.index('id="multi-curl-field" hidden')
        field_end = html.index("</div>", html.index("Работает только в режиме", field_start))
        field_html = html[field_start:field_end]

        self.assertIn('id="curl-parallelism"', field_html)
        self.assertIn("Все домены на одной стратегии", field_html)
        self.assertIn("curlField.hidden = mode !== 'multi';", html)

    def test_discovery_profile_is_advanced_blockcheck_scan_level_control(self) -> None:
        html = index_html()

        self.assertNotIn("Профиль подбора", html)
        self.assertIn("Глубина проверки стратегий", html)
        self.assertNotIn("Уровень поиска blockcheck2", html)
        options_start = html.index('<div class="preset-panel finder-options-panel">')
        options_end = html.index('<details class="preset-panel">', options_start)
        options_html = html[options_start:options_end]
        advanced_start = options_end
        advanced_end = html.index("</details>", advanced_start)
        advanced_html = html[advanced_start:advanced_end]

        self.assertIn('id="enable-http"', options_html)
        self.assertIn('id="enable-tls12"', options_html)
        self.assertIn('id="include-quic"', options_html)
        self.assertNotIn('id="discovery-profile-select"', options_html)
        self.assertNotIn('id="scan-level"', options_html)
        self.assertNotIn('id="repeats"', options_html)
        self.assertIn('id="discovery-profile-select"', advanced_html)
        self.assertIn('id="scan-level"', advanced_html)
        self.assertIn('id="repeats"', advanced_html)
        self.assertIn('id="limit-time-enabled"', advanced_html)
        self.assertIn('id="run-curl-max-time"', advanced_html)
        self.assertIn('id="run-curl-max-time-quic"', advanced_html)
        self.assertIn('id="run-curl-max-time-doh"', advanced_html)
        render_start = html.index("function renderDiscoveryProfiles()")
        render_end = html.index("function hasEnabledProtocol", render_start)
        render_html = html[render_start:render_end]
        self.assertNotIn('<option value="${CUSTOM_SELECT_VALUE}">Custom</option>', render_html)
        self.assertNotIn("event.target.value === CUSTOM_SELECT_VALUE", html[html.index("if (event.target && event.target.id === 'discovery-profile-select')"):html.index("if (event.target && event.target.name === 'run-mode')")])

    def test_expandable_preset_panel_has_clear_affordance(self) -> None:
        html = index_html()

        self.assertIn("details.preset-panel > summary::before", html)
        self.assertIn('content: "Раскрыть";', html)
        self.assertIn("details.preset-panel[open] > summary::after", html)
        self.assertIn('content: "Свернуть";', html)
        self.assertIn("details.preset-panel > summary:focus-visible", html)

    def test_time_limit_panel_keeps_layout_stable_when_disabled(self) -> None:
        html = index_html()

        advanced_start = html.index('<details class="preset-panel">')
        advanced_end = html.index("</details>", advanced_start)
        advanced_html = html[advanced_start:advanced_end]
        time_panel_start = advanced_html.index('id="time-limit-panel"')
        preset_grid_start = advanced_html.index('<div class="preset-grid">')
        preset_grid_html = advanced_html[preset_grid_start:advanced_html.index('id="repeat-parallel"', preset_grid_start)]

        self.assertLess(time_panel_start, preset_grid_start)
        self.assertIn('<div class="time-limit-panel disabled" id="time-limit-panel">', advanced_html)
        self.assertIn('<div class="field time-limit-field" id="time-limit-field" aria-disabled="true">', advanced_html)
        self.assertIn('id="finder-timeout-hours" type="number" min="0.1" max="24" step="0.5" value="6" disabled', advanced_html)
        self.assertNotIn('id="limit-time-enabled"', preset_grid_html)
        self.assertNotIn(".time-limit-field[hidden]", html)
        self.assertNotIn("el('time-limit-field').hidden", html)
        self.assertIn("input.disabled = !enabled;", html)
        self.assertIn("panel.classList.toggle('disabled', !enabled);", html)

    def test_live_run_panel_has_current_operational_slice(self) -> None:
        html = index_html()

        self.assertIn('id="live-run-panel"', html)
        self.assertIn("function renderLiveRun()", html)
        self.assertIn("function liveRunCells(progress)", html)
        for label in ("Текущий подбор", "Статус", "Этап", "Попытки", "Стратегии", "Найдено", "Текущий файл", "Прошло", "Осталось"):
            self.assertIn(label, html)

    def test_live_run_panel_keeps_stop_log_and_results_actions(self) -> None:
        html = index_html()

        self.assertIn('data-action="stop-current"', html)
        self.assertIn('data-action="open-log"', html)
        self.assertIn('data-action="open-candidates"', html)
        self.assertIn("renderLiveRun();", html)
        self.assertIn("latestImportantLogMessage", html)

    def test_live_run_panel_warns_about_interrupted_run_after_restart(self) -> None:
        html = index_html()

        self.assertIn("interruptedRunWarning", html)
        self.assertIn("Предыдущий подбор был прерван перезагрузкой", html)
        self.assertIn("Активный подбор не восстанавливается после перезагрузки", html)
        self.assertIn("!['running', 'queued', 'stopping'].includes(status)", html)

    def test_terminal_tab_keeps_raw_log_as_secondary_debug_block(self) -> None:
        html = index_html()

        self.assertIn('class="raw-log-panel"', html)
        self.assertIn("Raw log / debug", html)
        self.assertLess(html.index('id="live-run-panel"'), html.index('class="raw-log-panel"'))
        self.assertLess(html.index('id="events-panel"'), html.index('class="raw-log-panel"'))

    def test_events_panel_uses_existing_status_log_and_diagnostics(self) -> None:
        html = index_html()

        self.assertIn('id="events-panel"', html)
        self.assertIn("function eventRows()", html)
        self.assertIn("stateBoard.last_error", html)
        self.assertIn("log.stderr_diagnostics", html)
        self.assertNotIn("state.releaseUpdate", html)
        self.assertNotIn("releaseStatus === 'failed'", html)
        self.assertNotIn("release.status === 'failed' || release.error", html)
        self.assertNotIn("eventStore", html)

    def test_events_panel_has_repeat_log_and_copy_actions(self) -> None:
        html = index_html()

        self.assertIn('data-action="repeat-last-run"', html)
        self.assertIn('data-action="open-log"', html)
        self.assertIn('data-action="copy-diagnostics"', html)
        self.assertIn("function copyDiagnostics()", html)
        self.assertIn("diagnosticsText()", html)

    def test_candidate_result_panel_has_agreed_modes_and_fields(self) -> None:
        html = index_html()

        self.assertIn("candidate-result-panel", html)
        common_start = html.index('id="common-controls"')
        result_start = html.index('class="candidate-result-panel"', common_start)
        preset_start = html.index('id="common-preset-select"', common_start)
        self.assertGreater(result_start, common_start)
        self.assertGreater(result_start, preset_start)
        self.assertIn("Пресет доменов для пересечения", html)
        self.assertIn("Итоговый набор общих стратегий", html)
        self.assertIn('data-action="build-candidate-result"', html)
        for label in ("Максимум покрытия", "Минимум стратегий", "Баланс"):
            self.assertIn(label, html)
        for field in ("required_coverage", "desired_coverage", "uncovered_required", "uncovered_desired", "strategy_set", "reason", "mode"):
            self.assertIn(field, html)

    def test_candidate_result_is_computed_from_loaded_candidates_only(self) -> None:
        html = index_html()

        self.assertIn("function commonCandidateResultRows()", html)
        self.assertIn("state.candidates", html)
        self.assertIn("const rows = commonCandidateResultRows();", html)
        self.assertNotIn("function loadedCandidateRows()", html)
        self.assertIn("Расчет по загруженным общим стратегиям", html)
        self.assertNotIn("/api/candidate-result", html)

    def test_candidate_result_is_one_button_common_action(self) -> None:
        html = index_html()

        self.assertIn("candidateResultRequested: false", html)
        self.assertIn("function buildCandidateResultNow()", html)
        self.assertIn("state.candidateResultRequested = true;", html)
        self.assertIn("if (!state.candidateResultRequested)", html)
        self.assertIn("panel.hidden = state.candidateView !== 'common';", html)
        self.assertIn("Нажмите «Собрать итоговый набор»", html)
        self.assertIn("state.candidateResultRequested = false;", html)

    def test_candidate_result_does_not_fallback_required_to_launch_domains(self) -> None:
        html = index_html()

        start = html.index("function candidateResultTargets()")
        end = html.index("function commonCandidateResultRows()", start)
        target_html = html[start:end]
        self.assertIn("const required = uniqueDomains(presetDomains('finder', 'system:required'));", target_html)
        self.assertIn("const desired = uniqueDomains(presetDomains('finder', 'system:desired'))", target_html)
        self.assertNotIn("selectedFinderDomains()", target_html)
        self.assertNotIn("required.length ? required", target_html)
        self.assertIn("Нет обязательных или желательных доменов для расчета итогового набора.", html)

    def test_candidate_result_actions_are_practical_without_new_validation(self) -> None:
        html = index_html()

        self.assertIn('data-action="copy-candidate-result"', html)
        self.assertIn('data-action="export-candidate-result"', html)
        self.assertIn('data-action="use-candidate-result-domains"', html)
        self.assertIn('data-action="open-candidate-result"', html)
        self.assertNotIn("data-candidate-verify", html)
        self.assertNotIn("/api/jobs/zapret-custom-verification", html)

    def test_candidate_balance_covers_required_before_desired(self) -> None:
        html = index_html()

        self.assertIn("requiredGain * 100000 + desiredGain * 1000", html)
        self.assertIn("uncoveredRequired", html)
        self.assertIn("uncoveredDesired", html)
        self.assertIn("Нет загруженных стратегий, которые покрывают выбранные домены.", html)

    def test_history_repeat_fills_launch_form_without_autostart(self) -> None:
        html = index_html()

        start = html.index("function repeatRun(runKey)")
        end = html.index("function runProgressText(row)", start)
        repeat_html = html[start:end]
        self.assertIn("fillRunFormFromPayload(row, payload);", repeat_html)
        self.assertNotIn("startJob(", repeat_html)
        self.assertIn("Параметры прошлого подбора перенесены в форму запуска", html)

    def test_history_repeat_restores_result_affecting_parameters(self) -> None:
        html = index_html()

        start = html.index("function fillRunFormFromPayload")
        end = html.index("function repeatRun(runKey)", start)
        repeat_html = html[start:end]
        for token in ("finder-domains", "run-mode", "curl-parallelism", "enable-http", "enable-tls12", "include-quic", "scan-level", "repeats", "skip-dnscheck", "run-curl-max-time", "limit-time-enabled"):
            self.assertIn(token, repeat_html)

    def test_mutating_actions_are_blocked_during_active_run(self) -> None:
        html = index_html()

        self.assertIn("const MUTATING_ACTIONS = new Set", html)
        for action in ("save-settings", "create-backup", "upload-backup", "preset-editor-save", "preset-editor-delete", "preset-new-save"):
            self.assertIn(action, html)
        self.assertIn("requireNoActiveRun()", html)
        self.assertIn("protectedMutation", html)

    def test_mutating_buttons_are_disabled_but_monitoring_actions_remain(self) -> None:
        html = index_html()

        self.assertIn("mutatingSelectors", html)
        self.assertIn("button.disabled = busy;", html)
        self.assertIn("button.disabled = !busy;", html)
        self.assertNotIn("'open-log'", html[html.index("const MUTATING_ACTIONS = new Set"):html.index("]);", html.index("const MUTATING_ACTIONS = new Set"))])

    def test_settings_auto_release_check_waits_for_active_run(self) -> None:
        html = index_html()

        self.assertIn("if (!mutatingBlocked() && !state.releaseChecked && !state.releaseChecking) checkReleases({ silent: true });", html)

    def test_settings_are_split_into_operational_groups(self) -> None:
        html = index_html()

        for marker in ("settings-discovery-panel", "settings-release-panel", "settings-backups-panel", "settings-danger-panel"):
            self.assertIn(marker, html)
        for title in ("Параметры подбора", "Релизы и обновления", "Бекапы и восстановление", "Опасные действия"):
            self.assertIn(title, html)

    def test_tabs_have_basic_accessibility_contract(self) -> None:
        html = index_html()

        self.assertIn('role="tablist"', html)
        self.assertIn('role="tab"', html)
        self.assertIn('aria-selected="true"', html)
        self.assertIn('aria-controls="tab-panel-finder"', html)
        self.assertIn('role="tabpanel"', html)
        self.assertIn('tabindex="-1"', html)
        self.assertIn("syncActiveTabUi", html)
        self.assertIn("TAB_NAVIGATION_KEYS", html)
        self.assertIn("function handleTabControlKeydown", html)
        self.assertIn("function activateTabControl", html)
        self.assertIn("ArrowRight", html)
        self.assertIn("ArrowLeft", html)
        self.assertIn("Home", html)
        self.assertIn("End", html)
        self.assertIn("button.tabIndex = active ? 0 : -1;", html)
        self.assertIn("if (handleTabControlKeydown(event)) return;", html)

    def test_progress_and_focus_have_accessibility_contract(self) -> None:
        html = index_html()

        self.assertIn('role="progressbar"', html)
        self.assertIn('aria-valuemin="0"', html)
        self.assertIn('aria-valuemax="100"', html)
        self.assertIn("aria-valuenow", html)
        self.assertIn("button:focus-visible", html)
        self.assertIn("summary:focus-visible", html)

    def test_index_script_has_no_raw_newlines_inside_quoted_strings(self) -> None:
        html = index_html()
        marker_start = "<script>"
        marker_end = "</script>"
        start = html.index(marker_start) + len(marker_start)
        end = html.index(marker_end, start)
        script = html[start:end]

        errors: list[int] = []
        line = 1
        in_string: str | None = None
        in_template = False
        in_regex = False
        in_regex_class = False
        in_line_comment = False
        in_block_comment = False
        escaped = False
        last_significant: str | None = None
        index = 0
        while index < len(script):
            char = script[index]
            next_char = script[index + 1] if index + 1 < len(script) else ""
            if char == "\n":
                line += 1
                in_line_comment = False
            if in_line_comment:
                index += 1
                continue
            if in_block_comment:
                if char == "*" and next_char == "/":
                    in_block_comment = False
                    index += 2
                    continue
                index += 1
                continue
            if in_regex:
                if char in "\r\n":
                    errors.append(line)
                    in_regex = False
                    in_regex_class = False
                elif escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == "[":
                    in_regex_class = True
                elif char == "]":
                    in_regex_class = False
                elif char == "/" and not in_regex_class:
                    in_regex = False
                    last_significant = "/"
                index += 1
                continue
            if in_string:
                if char in "\r\n":
                    errors.append(line)
                    in_string = None
                elif escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == in_string:
                    in_string = None
                index += 1
                continue
            if in_template:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == "`":
                    in_template = False
                index += 1
                continue
            if char == "/" and next_char == "/":
                in_line_comment = True
                index += 2
                continue
            if char == "/" and next_char == "*":
                in_block_comment = True
                index += 2
                continue
            if char == "/" and (last_significant is None or last_significant in "([{=,:;!&|?"):
                in_regex = True
                escaped = False
                in_regex_class = False
                index += 1
                continue
            if char in ("'", '"'):
                in_string = char
            elif char == "`":
                in_template = True
            if not char.isspace():
                last_significant = char
            index += 1

        self.assertEqual([], errors[:10], f"Raw newline inside JS string literal near lines: {errors[:10]}")

    def test_index_html_does_not_contain_mojibake_markers(self) -> None:
        html = index_html()

        for marker in ("\u0420\u045b", "\u0420\u0451", "\u0421\u0403"):
            self.assertNotIn(marker, html)

        self.assertIn("Ошибка обновления истории", html)
        self.assertIn("Ошибка обновления лога", html)
        self.assertIn("Ошибка обновления пресетов", html)

    def test_head_root_returns_ok_for_curl_i(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            port = start_server(serve, config).port

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("HEAD", "/")
            response = connection.getresponse()
            response.read()
            connection.close()

            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), "text/html; charset=utf-8")
            self.assertEqual(response.getheader("Cache-Control"), "no-store")

    def test_serve_clears_stale_current_job_on_start(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            write_state(config.output.state_dir, {"current_run_id": "stale-job", "last_error": None})
            server = _start_captured_server(serve, config)
            with server:
                self.assertIsNone(read_state(config.output.state_dir)["current_run_id"])

    def test_serve_does_not_clear_current_job_when_runtime_lock_exists(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            write_state(config.output.state_dir, {"current_run_id": "active-job", "last_error": None})
            config.output.state_dir.mkdir(parents=True, exist_ok=True)
            (config.output.state_dir / "job-runner.lock").write_text(
                json.dumps({"pid": os.getpid(), "run_id": "active-job"}),
                encoding="utf-8",
            )
            server = _start_captured_server(serve, config)
            with server:
                self.assertEqual(read_state(config.output.state_dir)["current_run_id"], "active-job")

    def test_legacy_diagnostics_endpoint_is_removed_from_alpha_api(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            port = start_server(serve, config).port

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", "/api/diagnostics", headers=_authenticated_headers(port))
            response = connection.getresponse()
            body = response.read().decode("utf-8")
            connection.close()

            self.assertEqual(response.status, 404)
            self.assertIn("not found", body)

    def test_core_mode_serves_api_without_web_ui(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            port = start_server(serve_core, config).port

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", "/")
            root_response = connection.getresponse()
            root_body = root_response.read().decode("utf-8")
            connection.close()

            core_status, _core_headers, core_body = _http_request(port, "/api/core/status")
            web_status, _web_headers, web_body = _http_request(port, "/api/web/run-preferences")

            self.assertEqual(root_response.status, 404)
            self.assertApiError(json.loads(root_body), "not_found")
            self.assertNotIn("<!doctype html>", root_body.lower())
            self.assertEqual(core_status, 200)
            self.assertIn('"state"', core_body.decode("utf-8"))
            self.assertEqual(web_status, 404)
            self.assertIn("not found", web_body.decode("utf-8"))

    def test_history_routes_use_run_id_without_legacy_id(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            append_run(
                config.output.state_dir,
                {
                    "id": "run-contract",
                    "kind": "standard-discovery",
                    "status": "success",
                    "timestamp": "2026-08-10T00:00:00Z",
                },
            )
            authorization = _bearer_authorization_for_state(config.output.state_dir)
            core_port = start_server(serve_core, config).port
            web_port = start_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{core_port}").port

            contract = json.loads(web_app.openapi_json_bytes().decode("utf-8"))
            item_schema = contract["components"]["schemas"]["RunHistoryItem"]
            self.assertIn("run_id", item_schema["properties"])
            self.assertNotIn("id", item_schema["properties"])

            for path in ("/api/core/runs/history", "/api/web/runs/history-page"):
                status, _headers, body = _http_request(web_port, path, headers={"Authorization": authorization})
                self.assertEqual(status, 200, body.decode("utf-8", errors="replace"))
                item = json.loads(body.decode("utf-8"))["runs"][0]
                self.assertEqual(item["run_id"], "run-contract")
                self.assertNotIn("id", item)

    def test_openapi_and_swagger_are_served_by_monolith_and_core_mode(self) -> None:
        for target in (serve, serve_core):
            with _captured_server_temporary_directory() as (raw, start_server):
                tmp = Path(raw)
                config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
                port = start_server(target, config).port

                status, headers, body = _http_request(port, "/openapi.json")
                head_status, head_headers, head_body = _http_request(port, "/openapi.json", method="HEAD")
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("content-type"), "application/json; charset=utf-8")
                self.assertEqual(head_status, 200)
                self.assertEqual(head_headers.get("content-type"), "application/json; charset=utf-8")
                self.assertEqual(int(head_headers.get("content-length") or "0"), len(body))
                self.assertEqual(head_body, b"")
                openapi_text = body.decode("utf-8")
                openapi_contract = json.loads(
                    openapi_text,
                    object_pairs_hook=_json_object_without_duplicate_keys,
                )
                self.assertEqual(openapi_contract["openapi"], "3.1.0")
                openapi_operations = {
                    (path, method.upper())
                    for path, operations in openapi_contract["paths"].items()
                    for method in operations
                }
                self.assertEqual(openapi_operations, web_routes.openapi_operations(core_only=target is serve_core))
                if target is serve_core:
                    self.assertEqual(openapi_contract["info"]["title"], "GP Control Plane Core API")
                    self.assertIn("Callable Core/Service/OpenAPI operations", openapi_contract["info"]["description"])
                    self.assertFalse(any(path.startswith("/api/web/") for path in openapi_contract["paths"]))
                else:
                    self.assertEqual(openapi_contract["info"]["title"], "GP Control Plane API")
                    self.assertIn("/api/web", openapi_text)
                    self.assertTrue(any(path.startswith("/api/web/") for path in openapi_contract["paths"]))
                self.assertIn("/api/core/backups/download-archive", openapi_contract["paths"])
                self.assertNotIn("/api/core/backups/download-file", openapi_contract["paths"])
                self.assertEqual(
                    "downloadBackupArchive",
                    openapi_contract["paths"]["/api/core/backups/download-archive"]["get"]["operationId"],
                )
                self.assertNotIn("jsonSchemaDialect", openapi_contract)
                self.assertEqual(
                    {"type": "http", "scheme": "bearer", "bearerFormat": "opaque"},
                    openapi_contract["components"]["securitySchemes"]["bearerAuth"],
                )
                self.assertEqual([{"bearerAuth": []}], openapi_contract["security"])
                self.assertEqual([], openapi_contract["paths"]["/api/health"]["get"]["security"])
                self.assertEqual([], openapi_contract["paths"]["/api/auth/login"]["post"]["security"])
                self.assertEqual([{"url": "/"}], [{"url": server["url"]} for server in openapi_contract["servers"]])
                self.assertNotIn("localhost", openapi_text)
                self.assertNotIn("127.0.0.1:8081", openapi_text)
                examples = openapi_contract["components"]["examples"]
                self.assertIn("StartRunRequestMultiDomain", examples)
                self.assertEqual(30, examples["StartRunRequestMultiDomain"]["value"]["curl_parallelism"])
                self.assertNotIn("mode_settings", examples["StartRunRequestMultiDomain"]["value"])
                start_schema = openapi_contract["components"]["schemas"]["StartRunRequest"]
                self.assertEqual(["domains"], start_schema["required"])
                self.assertNotIn("mode_settings", start_schema["properties"])
                self.assertFalse(start_schema["properties"]["settings"]["additionalProperties"])
                self.assertIn("curl_max_time", start_schema["properties"]["settings"]["properties"])
                self.assertNotIn("mode_settings", start_schema["properties"]["settings"]["properties"])
                error_schema = openapi_contract["components"]["schemas"]["ErrorResponse"]
                envelope_schema = error_schema["properties"]["error"]
                self.assertEqual("object", envelope_schema["type"])
                self.assertEqual(["code", "message", "details"], envelope_schema["required"])
                self.assertEqual(
                    {"error": {"code": "invalid_request", "message": "The request is invalid.", "details": {}}},
                    examples["ErrorResponse"]["value"],
                )
                self.assertIn(
                    "409",
                    openapi_contract["paths"]["/api/core/strategy-discovery/stop-current-run"]["post"]["responses"],
                )
                self.assertNotIn(
                    "404",
                    openapi_contract["paths"]["/api/core/strategy-discovery/stop-current-run"]["post"]["responses"],
                )
                category_responses = openapi_contract["paths"]["/api/core/presets/v2fly/category-domains"]["get"]["responses"]
                self.assertIn("400", category_responses)
                self.assertNotIn("404", category_responses)
                self.assertNotIn("503", category_responses)
                categories_responses = openapi_contract["paths"]["/api/core/presets/v2fly/categories"]["get"]["responses"]
                self.assertNotIn("503", categories_responses)
                self.assertNotIn("InstallReleaseRequest", openapi_contract["components"]["schemas"])
                self.assertNotIn("ReleaseInstallPlan", openapi_contract["components"]["schemas"])
                self.assertNotIn("ReleaseUpdateInfo", openapi_contract["components"]["schemas"])
                self.assertNotIn("ReleaseInstallAccepted", openapi_contract["components"]["schemas"])
                self.assertNotIn("InstallChannel", openapi_contract["components"]["schemas"])
                self.assertNotIn("InstallReleaseRequest", examples)
                self.assertNotIn("ReleaseInstallPlanResponse", examples)
                self.assertNotIn("ReleaseInstallAccepted", examples)
                self.assertNotIn("InstallChannel", examples)
                self.assertIn("CoreStatusRunning", examples)
                self.assertIn("PagedStrategyCandidatesResponse", examples)
                self.assertIn("PagedCandidateDomainIndexResponse", examples)
                self.assertIn("WebRunPreferencesResponse", examples)
                self.assertIn("WebRunPreferencesSaveRequest", examples)
                self.assertIn("WebRunPreferencesResponse", openapi_contract["components"]["schemas"])
                self.assertIn("WebRunPreferencesSaveRequest", openapi_contract["components"]["schemas"])
                self.assertIn("PagedCandidateDomainIndexResponse", openapi_contract["components"]["schemas"])
                self.assertIn("WebPresetsResponse", openapi_contract["components"]["schemas"])
                self.assertIn("WebPresetDomainsResponse", openapi_contract["components"]["schemas"])
                self.assertNotIn("/api/service/releases/install", openapi_contract["paths"])
                if target is serve_core:
                    self.assertNotIn("/api/web/candidate-domain-index-page", openapi_contract["paths"])
                    self.assertNotIn("/api/web/presets", openapi_contract["paths"])
                    self.assertNotIn("/api/web/presets/domains", openapi_contract["paths"])
                    self.assertNotIn("/api/web/presets/save", openapi_contract["paths"])
                    self.assertNotIn("/api/web/presets/delete-user-lists", openapi_contract["paths"])
                    self.assertNotIn("/api/web/events/stream", openapi_contract["paths"])
                else:
                    self.assertIn("/api/web/candidate-domain-index-page", openapi_contract["paths"])
                    self.assertIn("/api/web/presets", openapi_contract["paths"])
                    self.assertIn("/api/web/presets/domains", openapi_contract["paths"])
                    self.assertIn("/api/web/presets/save", openapi_contract["paths"])
                    self.assertIn("/api/web/presets/delete-user-lists", openapi_contract["paths"])
                    self.assertIn("/api/web/events/stream", openapi_contract["paths"])
                web_preset_save_schema = openapi_contract["components"]["schemas"]["WebPresetSaveRequest"]
                self.assertNotIn("minItems", web_preset_save_schema["properties"]["domains"])
                runtime_busy_response = openapi_contract["components"]["responses"]["RuntimeBusy"]
                self.assertEqual(
                    {
                        "error": {
                            "code": "runtime_busy",
                            "message": "The operation is unavailable while discovery is running.",
                            "details": {},
                        }
                    },
                    runtime_busy_response["content"]["application/json"]["examples"]["runtimeBusy"]["value"],
                )
                for backup_mutation_path in (
                    "/api/core/backups/create",
                    "/api/core/backups/restore",
                    "/api/core/backups/delete",
                    "/api/core/backups/upload",
                ):
                    responses = openapi_contract["paths"][backup_mutation_path]["post"]["responses"]
                    self.assertEqual({"$ref": "#/components/responses/RuntimeBusy"}, responses["409"])
                self.assertEqual(
                    "legacy-root-apis-removed-in-alpha",
                    openapi_contract["x-gp-decisions"]["legacy_api_compatibility"],
                )
                self.assertNotIn("/api/service/diagnostics", openapi_contract["paths"])
                self.assertNotIn("ServiceDiagnostics", openapi_contract["components"]["schemas"])
                self.assertNotIn("ServiceDiagnostics", examples)
                log_tail_schema = openapi_contract["components"]["schemas"]["RunLogTail"]
                self.assertIn("stdout_tail", log_tail_schema["required"])
                self.assertIn("stderr_tail", log_tail_schema["required"])
                self.assertIn("progress", log_tail_schema["required"])
                self.assertNotIn("lines", log_tail_schema["properties"])
                self.assertNotIn("lines", examples["RunLogTail"]["value"])
                self.assertIn(
                    "multiDomain30Parallel",
                    openapi_contract["paths"]["/api/core/strategy-discovery/start-run"]["post"]["requestBody"][
                        "content"
                    ]["application/json"]["examples"],
                )
                self.assertIn(
                    "plainError",
                    openapi_contract["components"]["responses"]["Error"]["content"]["application/json"]["examples"],
                )
                self.assertIn("/api/core/presets/delete-user-domain-list", openapi_contract["paths"])
                self.assertNotIn("/api/core/presets/delete-user-lists", openapi_contract["paths"])
                delete_schema = openapi_contract["components"]["schemas"]["DeleteDomainListRequest"]
                self.assertEqual(["list_ids"], delete_schema["required"])
                self.assertEqual(1, delete_schema["properties"]["list_ids"]["minItems"])

                status, headers, body = _http_request(port, "/swagger")
                swagger_html = body.decode("utf-8")
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("content-type"), "text/html; charset=utf-8")
                self.assertIn("SwaggerUIBundle", swagger_html)
                self.assertIn("url: '/openapi.json'", swagger_html)

                status, headers, body = _http_request(port, "/swagger", method="HEAD")
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("content-type"), "text/html; charset=utf-8")
                self.assertEqual(body, b"")

    def test_openapi_contract_matches_routes_auth_errors_and_identifiers(self) -> None:
        for core_only in (False, True):
            contract = json.loads(web_docs.openapi_json_bytes(core_only=core_only))
            documented = {
                (path, method.upper())
                for path, operations in contract["paths"].items()
                for method in operations
            }
            expected = {
                (spec.path, method)
                for spec in web_routes.ROUTES
                if spec.openapi and (not core_only or spec.allowed_in_core)
                for method in spec.methods
                if method != "HEAD"
            }
            self.assertEqual(expected, documented)

            for path, method in documented:
                route = web_routes.route_for(method, path)
                self.assertIsNotNone(route)
                operation = contract["paths"][path][method.lower()]
                expected_security = [] if not route.auth_required else [{"bearerAuth": []}]
                self.assertEqual(expected_security, operation.get("security", contract["security"]))
                for status, response in operation["responses"].items():
                    if status == "default" or not status.startswith("2"):
                        self.assertIn("$ref", response)
                        self.assertIn(
                            response["$ref"],
                            {f"#/components/responses/{name}" for name in contract["components"]["responses"]},
                        )

            error_schema = contract["components"]["schemas"]["ErrorResponse"]["properties"]["error"]
            self.assertEqual(["code", "message", "details"], error_schema["required"])
            self.assertIn("saving", contract["components"]["schemas"]["RunStatus"]["enum"])
            self.assertEqual(
                "#/components/schemas/RunAccepted",
                contract["paths"]["/api/core/strategy-discovery/stop-current-run"]["post"]["responses"]["202"]["content"][
                    "application/json"
                ]["schema"]["$ref"],
            )
            self.assertIn("run_id", contract["components"]["schemas"]["RunAccepted"]["properties"])
            self.assertNotIn("/api/service/releases/install", contract["paths"])
            self.assertNotIn("/api/service/releases/install-plan", contract["paths"])
            self.assertNotIn("job_id", json.dumps(contract))

        swagger_html = web_docs.swagger_ui_html()
        self.assertIn("persistAuthorization: true", swagger_html)
        self.assertIn("tryItOutEnabled: true", swagger_html)

    def test_route_registry_defines_runtime_boundaries(self) -> None:
        self.assertEqual("core", web_routes.route_for("GET", "/api/core/status").namespace)
        self.assertEqual("core", web_routes.route_for("GET", "/api/core/backups/download-archive").namespace)
        self.assertIsNone(web_routes.route_for("POST", "/api/service/releases/install"))
        self.assertIsNone(web_routes.route_for("GET", "/api/service/releases/install-plan"))
        self.assertEqual("web", web_routes.route_for("GET", "/api/web/run-preferences").namespace)
        self.assertEqual("web", web_routes.route_for("GET", "/api/web/candidate-domain-index-page").namespace)
        self.assertEqual("web", web_routes.route_for("GET", "/api/web/presets").namespace)
        self.assertEqual("web", web_routes.route_for("POST", "/api/web/presets/save").namespace)
        self.assertEqual("web", web_routes.route_for("GET", "/api/web/events/stream").namespace)
        self.assertIsNone(web_routes.route_for("GET", "/api/status"))
        self.assertIsNone(web_routes.route_for("GET", "/api/core/backups/download-file"))
        self.assertIsNone(web_routes.route_for("POST", "/api/settings"))
        self.assertFalse(any(spec.namespace == "legacy" for spec in web_routes.ROUTES))
        self.assertFalse(web_routes.route_for("GET", "/").allowed_in_core)
        self.assertFalse(web_routes.route_for("GET", "/api/web/run-preferences").allowed_in_core)
        self.assertFalse(web_routes.route_for("GET", "/api/web/events/stream").allowed_in_core)
        web_only_paths = {spec.path for spec in web_routes.ROUTES if spec.namespace == "web"}
        self.assertEqual(
            web_only_paths,
            {spec.path for spec in web_routes.ROUTES if not spec.allowed_in_core},
        )
        self.assertTrue(
            all(spec.allowed_in_core for spec in web_routes.ROUTES if spec.namespace in {"core", "service", "openapi"})
        )
        self.assertIn("/api/core/status", web_routes.JSON_GET_ROUTE_PATHS)
        self.assertNotIn("/api/core/backups/download-file", web_routes.route_paths(method="GET"))
        self.assertIn("/api/service/status", web_routes.JSON_GET_ROUTE_PATHS)
        self.assertNotIn("/api/service/releases/install-plan", web_routes.JSON_GET_ROUTE_PATHS)
        self.assertIn("/api/web/run-preferences", web_routes.JSON_GET_ROUTE_PATHS)
        self.assertIn("/api/web/candidate-domain-index-page", web_routes.JSON_GET_ROUTE_PATHS)
        self.assertIn("/api/web/presets", web_routes.JSON_GET_ROUTE_PATHS)
        self.assertIn("/api/web/presets/domains", web_routes.JSON_GET_ROUTE_PATHS)
        self.assertNotIn("/api/status", web_routes.JSON_GET_ROUTE_PATHS)
        self.assertNotIn("/api/status", web_routes.JSON_HEAD_ROUTE_PATHS)
        self.assertIn("/api/core/strategy-discovery/start-run", web_routes.JSON_POST_ROUTE_PATHS)
        self.assertIn("/api/web/presets/save", web_routes.JSON_POST_ROUTE_PATHS)
        self.assertIn("/api/web/presets/delete-user-lists", web_routes.JSON_POST_ROUTE_PATHS)
        self.assertEqual({"/api/core/backups/upload"}, web_routes.UPLOAD_ROUTE_PATHS)

    def test_app_api_literals_are_registered_routes(self) -> None:
        source_path = Path(web_app.__file__)
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        api_literals = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith("/api/")
            and not node.value.endswith("/")
        }
        registered = {spec.path for spec in web_routes.ROUTES if spec.path.startswith("/api/")}
        self.assertFalse(api_literals - registered)

    def test_legacy_api_routes_return_not_found_in_alpha_runtimes(self) -> None:
        representative_legacy_requests = [
            ("GET", "/api/status", None, {}),
            ("POST", "/api/settings", {"settings": {"curl_parallelism_max": 12}}, {}),
            ("POST", "/api/settings", b"{", {"Content-Type": "application/json"}),
            ("GET", "/api/events", None, {}),
            ("GET", "/api/diagnostics", None, {}),
            ("POST", "/api/presets/save", {"scope": "finder", "name": "work", "domains": ["youtube.com"]}, {}),
            ("POST", "/api/presets/save", b"01234567890", {"Content-Type": "application/json"}),
            ("POST", "/api/backups/create", {}, {}),
            ("GET", "/api/releases/update-plan", None, {}),
        ]

        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            with mock.patch.object(web_app, "MAX_JSON_REQUEST_BYTES", 10):
                monolith_port = start_server(serve, config).port
                core_port = start_server(serve_core, config).port
                proxy_port = start_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{core_port}").port

                for port in (monolith_port, core_port, proxy_port):
                    for method, path, payload, headers in representative_legacy_requests:
                        if isinstance(payload, bytes):
                            raw_body = payload
                        elif payload is not None:
                            raw_body = json.dumps(payload).encode("utf-8")
                        else:
                            raw_body = None
                        request_headers = dict(headers)
                        request_headers.setdefault("Authorization", _bearer_authorization_for_state(config.output.state_dir))
                        if raw_body is not None and "Content-Type" not in request_headers:
                            request_headers["Content-Type"] = "application/json"
                        status, response_headers, body = _http_request(
                            port,
                            path,
                            method=method,
                            body=raw_body,
                            headers=request_headers,
                        )
                        message = (port, method, path, body.decode("utf-8", errors="replace"))
                        self.assertEqual(status, 404, message)
                        self.assertEqual(response_headers.get("content-type"), "application/json; charset=utf-8", message)

    def test_core_server_does_not_import_web_app_runtime(self) -> None:
        from gp_control_plane.web import core_server

        source = Path(core_server.__file__).read_text(encoding="utf-8")
        self.assertNotIn("from .app import", source)
        self.assertNotIn("gp_control_plane.web.app", source)

    def test_core_server_uses_api_server_entrypoint(self) -> None:
        from gp_control_plane.web import api_server, core_server

        config = AppConfig(output=OutputConfig(state_dir=Path("unused-state")))
        with mock.patch.object(api_server, "serve") as serve_mock:
            core_server.serve_core(config, host="127.0.0.1", port=18081)

        serve_mock.assert_called_once_with(config, host="127.0.0.1", port=18081, ui_enabled=False)

    def test_web_app_is_api_server_compatibility_alias(self) -> None:
        app_module = importlib.import_module("gp_control_plane.web.app")
        api_module = importlib.import_module("gp_control_plane.web.api_server")

        self.assertIs(app_module, api_module)

    def test_openapi_paths_are_callable_through_web_runtime(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            web_app.save_settings(config, {"curl_parallelism_max": 50, "curl_parallelism_default": 10})
            snapshot = web_app.create_snapshot_if_idle(config.output.state_dir)["snapshot"]["id"]
            release = {
                "channel": "stable",
                "available_version": "v0.0.0-test",
                "url": "https://example.invalid/release",
                "published_at": "",
            }
            with (
                mock.patch.object(web_app.service_api, "release_channel_info", return_value=release),
                mock.patch.object(web_app.service_api, "fetch_v2fly_revision", return_value="remote-test-revision"),
                mock.patch.object(web_app.service_api, "prepare_v2fly_local_storage", return_value={"count": 0}),
                mock.patch.object(
                    web_app.core_api,
                    "create_clean_install_vault",
                    return_value={
                        "vault_id": "a" * 32,
                        "handoff_secret": "SAFE-HANDOFF-001-KNOWN-SECRET",
                        "archive_sha256": "b" * 64,
                        "archive_size_bytes": 12,
                        "schema_version": "7",
                        "semantic_manifest": {},
                    },
                ),
                mock.patch.object(
                    web_app.core_api,
                    "clean_install_vault_info",
                    return_value={
                        "exists": True,
                        "pending": True,
                        "vault_id": "a" * 32,
                        "created_at": "2026-08-21T12:00:00Z",
                        "schema_version": "7",
                        "archive_sha256": "b" * 64,
                        "archive_size_bytes": 12,
                        "verification": "pending",
                    },
                ),
                mock.patch.object(
                    web_app.core_api,
                    "restore_clean_install_vault",
                    return_value={
                        "completed": True,
                        "vault_id": "a" * 32,
                        "verification": {"verified": True},
                        "cleanup": {"source_deleted": True},
                    },
                ),
                mock.patch.object(
                    web_app,
                    "export_nfconf",
                    return_value={
                        "engine": "blockchecks",
                        "out_dir": "/tmp/nfconf-out",
                        "paths": ["/tmp/nfconf-out/nfqws2.conf"],
                        "db": "/tmp/state.db",
                    },
                ),
            ):
                port = start_server(serve, config).port

                cases = [
                    ("GET", "/api/health", None, {}),
                    ("POST", "/api/auth/login", {"username": "admin", "password": "admin"}, {}),
                    ("POST", "/api/auth/change-password", {"current_password": "admin", "new_password": "short"}, {}),
                    ("GET", "/api/core/status", None, {}),
                    ("POST", "/api/core/strategy-discovery/start-run", {"mode": "bad", "domains": ["youtube.com"]}, {}),
                    ("POST", "/api/core/strategy-discovery/export-nfconf", {"limit": 1}, {}),
                    ("POST", "/api/core/strategy-discovery/stop-current-run", {"dry_run": True}, {}),
                    ("GET", "/api/core/strategy-discovery/current-run-progress", None, {}),
                    ("GET", "/api/core/strategy-discovery/current-run-latest-log", None, {}),
                    ("GET", "/api/core/strategy-discovery/preflight", None, {}),
                    ("GET", "/api/core/presets/domain-lists", None, {}),
                    ("POST", "/api/core/presets/save-domain-list", {"kind": "user", "name": "work", "domains": ["youtube.com"]}, {}),
                    ("POST", "/api/core/presets/save-domain-list", {"kind": "user", "name": "games", "domains": ["discord.com"]}, {}),
                    ("POST", "/api/core/presets/delete-user-domain-list", {"list_ids": ["user:work", "user:games"]}, {}),
                    ("GET", "/api/core/presets/v2fly/categories", None, {}),
                    ("GET", "/api/core/presets/v2fly/category-domains?category=missing", None, {}),
                    ("POST", "/api/core/backups/create", {}, {}),
                    ("GET", "/api/core/backups/list", None, {}),
                    ("POST", "/api/core/backups/restore", {"snapshot_id": "missing"}, {}),
                    ("POST", "/api/core/backups/delete", {"snapshot_id": "missing"}, {}),
                    ("GET", f"/api/core/backups/download-archive?snapshot_id={snapshot}", None, {}),
                    ("POST", "/api/core/backups/upload", b"not-a-zip", {"Content-Type": "application/zip"}),
                    ("POST", "/api/core/clean-install-vaults/create", {}, {}),
                    ("GET", "/api/core/clean-install-vaults/list", None, {}),
                    ("GET", "/api/core/clean-install-vaults/status?vault_id=" + ("a" * 32), None, {}),
                    (
                        "POST",
                        "/api/core/clean-install-vaults/restore",
                        {"vault_id": "a" * 32},
                        {},
                    ),
                    ("GET", "/api/core/run-settings", None, {}),
                    ("POST", "/api/core/run-settings/save", {"curl_parallelism_default": 10, "curl_parallelism_max": 50}, {}),
                    ("GET", "/api/core/runs/history", None, {}),
                    ("GET", "/api/core/runs/latest-log", None, {}),
                    ("GET", "/api/core/strategy-candidates?domain=youtube.com", None, {}),
                    ("GET", "/api/core/strategy-candidates/export", None, {}),
                    ("GET", "/api/core/events", None, {}),
                    ("GET", "/api/service/status", None, {}),
                    ("GET", "/api/service/releases/available", None, {}),
                    ("GET", "/api/service/v2fly/local-storage-status", None, {}),
                    ("POST", "/api/service/v2fly/check-updates", {}, {}),
                    ("POST", "/api/service/v2fly/update-local-storage", {"dry_run": True}, {}),
                    ("GET", "/api/web/run-preferences", None, {}),
                    ("POST", "/api/web/run-preferences", {"run_preferences": {"domains": ["youtube.com"], "run_mode": "multi"}}, {}),
                    ("GET", "/api/web/runs/history-page", None, {}),
                    ("GET", "/api/web/candidate-domain-index-page", None, {}),
                    ("GET", "/api/web/strategy-candidates-page", None, {}),
                    ("GET", "/api/web/presets", None, {}),
                    ("GET", "/api/web/presets/domains?scope=finder&name=required&kind=system&limit=2", None, {}),
                    ("POST", "/api/web/presets/save", {"scope": "finder", "name": "work", "domains": ["youtube.com"]}, {}),
                    ("POST", "/api/web/presets/delete-user-lists", {"scope": "finder", "names": ["work"]}, {}),
                    ("GET", "/api/web/events", None, {}),
                    ("GET", "/api/web/events/stream", None, {"Accept": "text/event-stream"}),
                ]
                openapi = json.loads(web_app.openapi_json_bytes().decode("utf-8"))
                expected = {(method.upper(), path) for path, ops in openapi["paths"].items() for method in ops}
                requested = {(method, path.split("?", 1)[0]) for method, path, _body, _headers in cases}
                self.assertEqual(expected, requested)

                expected_json_fields = {
                    ("GET", "/api/core/status"): {"state", "storage", "updated_at"},
                    ("POST", "/api/core/strategy-discovery/stop-current-run"): {"accepted", "status", "run_id"},
                    ("GET", "/api/core/strategy-discovery/current-run-progress"): {"run_id", "status", "stage", "current_file"},
                    ("GET", "/api/core/strategy-discovery/current-run-latest-log"): {"stdout_tail", "stderr_tail", "progress"},
                    ("GET", "/api/core/strategy-discovery/preflight"): {"ready", "checks"},
                    ("GET", "/api/core/presets/domain-lists"): {"lists"},
                    ("POST", "/api/core/presets/save-domain-list"): {"list_id", "kind", "name", "domains", "updated_at"},
                    ("POST", "/api/core/presets/delete-user-domain-list"): {"deleted"},
                    ("GET", "/api/core/presets/v2fly/categories"): {"categories", "storage"},
                    ("POST", "/api/core/backups/create"): {"snapshot_id", "created_at", "filename", "size_bytes"},
                    ("GET", "/api/core/backups/list"): {"backups"},
                    ("POST", "/api/core/clean-install-vaults/create"): {
                        "vault_id", "archive_sha256", "archive_size_bytes", "schema_version", "semantic_manifest"
                    },
                    ("GET", "/api/core/clean-install-vaults/list"): {"vaults"},
                    ("GET", "/api/core/clean-install-vaults/status"): {
                        "vault_id", "created_at", "schema_version", "archive_sha256", "archive_size_bytes", "verification", "pending"
                    },
                    ("POST", "/api/core/clean-install-vaults/restore"): {"completed", "vault_id", "verification", "cleanup"},
                    ("GET", "/api/core/run-settings"): {
                        "curl_parallelism_default",
                        "curl_parallelism_max",
                        "curl_max_time",
                        "curl_max_time_quic",
                        "curl_max_time_doh",
                        "enable_ipv6",
                        "debug_stdout",
                        "discovery_engine",
                    },
                    ("POST", "/api/core/run-settings/save"): {
                        "curl_parallelism_default",
                        "curl_parallelism_max",
                        "curl_max_time",
                        "curl_max_time_quic",
                        "curl_max_time_doh",
                        "enable_ipv6",
                        "debug_stdout",
                        "discovery_engine",
                    },
                    ("GET", "/api/core/runs/history"): {"runs"},
                    ("GET", "/api/core/runs/latest-log"): {"stdout_tail", "stderr_tail", "progress"},
                    ("GET", "/api/core/strategy-candidates"): {"candidates", "total", "filters"},
                    ("GET", "/api/core/events"): {"events", "next_after_id"},
                    ("GET", "/api/service/status"): {"state", "mode", "services", "version", "data_state", "updated_at"},
                    ("GET", "/api/service/releases/available"): {"current", "releases", "stable_release_url", "prerelease_url"},
                    ("GET", "/api/service/v2fly/local-storage-status"): {
                        "state",
                        "source_repo",
                        "source_ref",
                        "source_commit",
                        "prepared_at",
                        "group_count",
                        "last_update_check",
                    },
                    ("POST", "/api/service/v2fly/check-updates"): {"status", "operation_id", "storage"},
                    ("POST", "/api/service/v2fly/update-local-storage"): {"status", "operation_id", "storage"},
                    ("GET", "/api/web/run-preferences"): {"run_preferences"},
                    ("POST", "/api/web/run-preferences"): {"run_preferences"},
                    ("GET", "/api/web/runs/history-page"): {"runs", "total", "limit", "offset", "has_more"},
                    ("GET", "/api/web/candidate-domain-index-page"): {"domains", "total", "strategy_total", "limit", "offset", "has_more"},
                    ("GET", "/api/web/strategy-candidates-page"): {"candidates", "total", "limit", "offset", "has_more"},
                    ("GET", "/api/web/presets"): {"metadata", "system_metadata", "custom", "system", "domain_sets", "builtin"},
                    ("GET", "/api/web/presets/domains"): {"scope", "name", "kind", "domains", "total", "limit", "offset", "has_more"},
                    ("POST", "/api/web/presets/save"): {"metadata", "system_metadata", "custom", "system", "domain_sets", "builtin"},
                    ("POST", "/api/web/presets/delete-user-lists"): {
                        "metadata",
                        "system_metadata",
                        "custom",
                        "system",
                        "domain_sets",
                        "builtin",
                    },
                    ("GET", "/api/web/events"): {"events", "next_after_id"},
                }

                for method, path, body, headers in cases:
                    raw_body = body if isinstance(body, bytes) else (json.dumps(body).encode("utf-8") if body is not None else None)
                    request_headers = dict(headers)
                    if raw_body is not None and "Content-Type" not in request_headers:
                        request_headers["Content-Type"] = "application/json"
                    base_path = path.split("?", 1)[0]
                    if base_path == "/api/web/events/stream":
                        status, response_headers, first_line, second_line = _http_sse_first_event(port, path)
                        message = (method, path, status, first_line, second_line)
                        self.assertEqual(status, 200, message)
                        self.assertEqual(response_headers.get("content-type"), "text/event-stream; charset=utf-8", message)
                        self.assertEqual(first_line, "event: status", message)
                        self.assertTrue(second_line.startswith("data:"), message)
                        continue
                    status, _response_headers, response_body = _http_request(
                        port,
                        path,
                        method=method,
                        body=raw_body,
                        headers=request_headers,
                    )
                    message = (method, path, response_body.decode("utf-8", errors="replace"))
                    self.assertNotEqual(status, 404, message)
                    if base_path == "/api/core/backups/download-archive":
                        self.assertEqual(status, 200, message)
                        continue
                    if base_path == "/api/core/strategy-candidates/export":
                        self.assertEqual(status, 200, message)
                        self.assertEqual(_response_headers.get("content-type"), "application/x-ndjson; charset=utf-8", message)
                        continue
                    self.assertEqual(_response_headers.get("content-type"), "application/json; charset=utf-8", message)
                    response_payload = json.loads(response_body.decode("utf-8"))
                    if status >= 400:
                        self.assertIn("error", response_payload, message)
                        continue
                    for field in expected_json_fields.get((method, base_path), set()):
                        self.assertIn(field, response_payload, message)

                status, headers, body = _http_request(port, "/api/web/events/stream", method="HEAD")
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("content-type"), "text/event-stream; charset=utf-8")
                self.assertEqual(body, b"")

                old_status, old_headers, old_body = _http_request(
                    port,
                    f"/api/core/backups/download-file?snapshot_id={snapshot}",
                )
                self.assertEqual(old_status, 404)
                self.assertEqual(old_headers.get("content-type"), "application/json; charset=utf-8")
                self.assertIn("not found", old_body.decode("utf-8"))

    def test_openapi_paths_are_callable_through_split_core_and_web_proxy_runtime(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            web_app.save_settings(config, {"curl_parallelism_max": 50, "curl_parallelism_default": 10})
            snapshot = web_app.create_snapshot_if_idle(config.output.state_dir)["snapshot"]["id"]
            release = {
                "channel": "stable",
                "available_version": "v0.0.0-test",
                "url": "https://example.invalid/release",
                "published_at": "",
            }

            def create_post_run_snapshot(_state_dir: Path) -> dict[str, object]:
                return {
                    "kind": "snapshot",
                    "status": "success",
                    "completed_at": "2026-08-13T00:00:00Z",
                    "snapshot_id": "openapi-contract-post-run-snapshot",
                    "snapshot": {"id": "openapi-contract-post-run-snapshot"},
                }

            with (
                mock.patch.object(web_app, "run_multi_domain_discovery", return_value={"status": "success"}),
                mock.patch.object(web_app, "run_standard_discovery", return_value={"status": "success"}),
                mock.patch.object(
                    web_app,
                    "export_nfconf",
                    return_value={
                        "engine": "blockchecks",
                        "out_dir": "/tmp/nfconf-out",
                        "paths": ["/tmp/nfconf-out/nfqws2.conf"],
                        "db": "/tmp/state.db",
                    },
                ),
                mock.patch.object(web_app.service_api, "release_channel_info", return_value=release),
                mock.patch.object(web_app.service_api, "fetch_v2fly_revision", return_value="remote-test-revision"),
                mock.patch.object(web_app.service_api, "prepare_v2fly_local_storage", return_value={"count": 0}),
                mock.patch.object(web_app, "create_post_run_snapshot", side_effect=create_post_run_snapshot) as post_run_snapshot,
                _JobRunnerThreadTracker() as runner_threads,
            ):
                core_port = start_server(serve_core, config).port
                web_port = start_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{core_port}").port

                openapi_contract = json.loads(
                    web_app.openapi_json_bytes().decode("utf-8"),
                    object_pairs_hook=_json_object_without_duplicate_keys,
                )
                core_openapi_status, core_openapi_headers, core_openapi_body = _http_request(core_port, "/openapi.json")
                proxy_openapi_status, proxy_openapi_headers, proxy_openapi_body = _http_request(web_port, "/openapi.json")
                self.assertEqual(core_openapi_status, 200)
                self.assertEqual(core_openapi_headers.get("content-type"), "application/json; charset=utf-8")
                self.assertEqual(proxy_openapi_status, 200)
                self.assertEqual(proxy_openapi_headers.get("content-type"), "application/json; charset=utf-8")
                full_openapi_text = web_app.openapi_json_bytes().decode("utf-8")
                core_openapi_text = core_openapi_body.decode("utf-8")
                proxy_openapi_text = proxy_openapi_body.decode("utf-8")
                self.assertIn("/api/web", full_openapi_text)
                self.assertIn("/api/web", proxy_openapi_text)
                core_openapi_contract = json.loads(
                    core_openapi_text,
                    object_pairs_hook=_json_object_without_duplicate_keys,
                )
                proxy_openapi_contract = json.loads(
                    proxy_openapi_text,
                    object_pairs_hook=_json_object_without_duplicate_keys,
                )
                core_openapi_operations = {
                    (path, method.upper())
                    for path, operations in core_openapi_contract["paths"].items()
                    for method in operations
                }
                proxy_openapi_operations = {
                    (path, method.upper())
                    for path, operations in proxy_openapi_contract["paths"].items()
                    for method in operations
                }
                self.assertEqual(core_openapi_operations, web_routes.openapi_operations(core_only=True))
                self.assertFalse(any(path.startswith("/api/web/") for path in core_openapi_contract["paths"]))
                self.assertEqual(proxy_openapi_operations, web_routes.openapi_operations())
                self.assertTrue(any(path.startswith("/api/web/") for path in proxy_openapi_contract["paths"]))
                expected = {(method.upper(), path) for path, ops in openapi_contract["paths"].items() for method in ops}
                web_expected = {
                    (method, path)
                    for method, path in expected
                    if web_routes.route_for(method, path) and web_routes.route_for(method, path).namespace == "web"
                }
                core_checked: set[tuple[str, str]] = set()
                proxy_checked: set[tuple[str, str]] = set()
                successful_start_requests = 0
                for runtime, port in (("core", core_port), ("proxy", web_port)):
                    for path, operations in openapi_contract["paths"].items():
                        for raw_method, operation in operations.items():
                            method = raw_method.upper()
                            request_path, raw_body, request_headers = _openapi_test_request(
                                openapi_contract,
                                path,
                                method,
                                operation,
                                snapshot,
                            )
                            route = web_routes.route_for(method, path)
                            if runtime == "core" and route and route.namespace == "web":
                                status, _headers, response_body = _http_request(
                                    port,
                                    request_path,
                                    method=method,
                                    body=raw_body,
                                    headers=request_headers,
                                )
                                message = (runtime, method, request_path, status, response_body.decode("utf-8", errors="replace")[:500])
                                self.assertEqual(status, 404, message)
                                continue
                            if path == "/api/web/events/stream" and method == "GET":
                                status, headers, first_line, second_line = _http_sse_first_event(port, request_path)
                                message = (runtime, method, request_path, status, first_line, second_line)
                                proxy_checked.add((method, path))
                                self.assertEqual(status, 200, message)
                                self.assertEqual(headers.get("content-type"), "text/event-stream; charset=utf-8", message)
                                self.assertEqual(first_line, "event: status", message)
                                self.assertTrue(second_line.startswith("data:"), message)
                                continue
                            status, headers, response_body = _http_request(
                                port,
                                request_path,
                                method=method,
                                body=raw_body,
                                headers=request_headers,
                            )
                            message = (runtime, method, request_path, status, response_body.decode("utf-8", errors="replace")[:500])
                            if runtime == "core":
                                core_checked.add((method, path))
                            else:
                                proxy_checked.add((method, path))
                            _assert_openapi_response_contract(
                                self,
                                openapi_contract,
                                operation,
                                status,
                                headers,
                                response_body,
                                context=message,
                            )
                            if path == "/api/core/backups/download-archive" and status == 200:
                                self.assertGreater(len(response_body), 0, message)
                            if path == "/api/core/strategy-discovery/start-run" and method == "POST":
                                self.assertEqual(status, 202, message)
                                successful_start_requests += 1
                                runner_threads.join_tracked()

                core_head_status, core_head_headers, core_head_body = _http_request(
                    core_port,
                    "/api/web/events/stream",
                    method="HEAD",
                )
                proxy_head_status, proxy_head_headers, proxy_head_body = _http_request(
                    web_port,
                    "/api/web/events/stream",
                    method="HEAD",
                )
                self.assertEqual(core_head_status, 404)
                self.assertEqual(core_head_headers.get("content-type"), "application/json; charset=utf-8")
                self.assertEqual(core_head_body, b"")
                self.assertEqual(proxy_head_status, 200)
                self.assertEqual(proxy_head_headers.get("content-type"), "text/event-stream; charset=utf-8")
                self.assertEqual(proxy_head_body, b"")
                self.assertEqual(expected - web_expected, core_checked)
                self.assertEqual(expected, proxy_checked)
                self.assertEqual(successful_start_requests, 2)
                self.assertEqual(runner_threads.tracked_count, successful_start_requests)
                self.assertEqual(post_run_snapshot.call_count, successful_start_requests)

                for runtime, port in (("core", core_port), ("proxy", web_port)):
                    old_status, old_headers, old_body = _http_request(
                        port,
                        f"/api/core/backups/download-file?snapshot_id={snapshot}",
                    )
                    self.assertEqual(old_status, 404, runtime)
                    self.assertEqual(old_headers.get("content-type"), "application/json; charset=utf-8", runtime)
                    self.assertIn("not found", old_body.decode("utf-8"), runtime)

    def test_core_delete_user_domain_lists_requires_explicit_non_empty_ids(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            port = start_server(serve, config).port

            status, _headers, _body = _http_request(
                port,
                "/api/core/presets/delete-user-domain-list",
                method="POST",
                body=json.dumps({"list_ids": []}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 400)

            status, _headers, _body = _http_request(
                port,
                "/api/core/presets/delete-user-lists",
                method="POST",
                body=json.dumps({"dry_run": True}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 404)

            for name in ("work", "games"):
                status, _headers, body = _http_request(
                    port,
                    "/api/core/presets/save-domain-list",
                    method="POST",
                    body=json.dumps({"kind": "user", "name": name, "domains": [f"{name}.example"]}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(status, 200, body.decode("utf-8", errors="replace"))

            status, _headers, body = _http_request(
                port,
                "/api/core/presets/delete-user-domain-list",
                method="POST",
                body=json.dumps({"list_ids": ["user:work", "user:games"]}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 200, body.decode("utf-8", errors="replace"))
            self.assertEqual({"deleted": 2}, json.loads(body.decode("utf-8")))

            status, _headers, body = _http_request(port, "/api/core/presets/domain-lists")
            self.assertEqual(status, 200)
            list_ids = [item["list_id"] for item in json.loads(body.decode("utf-8"))["lists"]]
            self.assertNotIn("user:work", list_ids)
            self.assertNotIn("user:games", list_ids)

    def test_core_error_contract_returns_plain_error_payloads(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            port = start_server(serve, config).port

            status, headers, body = _http_request(
                port,
                "/api/core/strategy-discovery/stop-current-run",
                method="POST",
                body=json.dumps({}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            payload = json.loads(body.decode("utf-8"))
            self.assertEqual(status, 409)
            self.assertEqual(headers.get("content-type"), "application/json; charset=utf-8")
            self.assertApiError(payload, "conflict")

            status, headers, body = _http_request(port, "/api/core/presets/v2fly/category-domains?category=missing")
            payload = json.loads(body.decode("utf-8"))
            self.assertEqual(status, 400)
            self.assertEqual(headers.get("content-type"), "application/json; charset=utf-8")
            self.assertApiError(payload, "invalid_request")

    def test_core_and_service_v2fly_storage_status_use_same_formatter(self) -> None:
        from gp_control_plane.domain_sources import write_v2fly_catalog_cache

        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            write_v2fly_catalog_cache(
                config.output.state_dir,
                {
                    "checked_at": "2026-07-23T10:00:00Z",
                    "remote_revision": "remote-revision",
                    "update_available": True,
                    "categories": ["youtube"],
                },
            )
            raw_status = {
                "data_status": "local",
                "revision": "local-revision",
                "checked_at": "2026-07-22T10:00:00Z",
                "all_count": 12,
            }

            core_payload = web_app.core_api.v2fly_storage_status_payload(config, raw_status)
            service_payload = web_app.service_api.v2fly_storage_status_payload(config, raw_status)

            self.assertIs(web_app.core_api.v2fly_storage_status_payload, web_app.service_api.v2fly_storage_status_payload)
            self.assertEqual(core_payload, service_payload)
            self.assertEqual(core_payload["state"], "ready")
            self.assertEqual(core_payload["source_commit"], "local-revision")
            self.assertEqual(core_payload["last_update_check"]["remote_revision"], "remote-revision")

    def test_service_v2fly_update_conflicts_when_runtime_lock_exists(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            config.output.state_dir.mkdir(parents=True, exist_ok=True)
            (config.output.state_dir / "job-runner.lock").write_text(
                json.dumps({"pid": os.getpid(), "run_id": "lock-only"}),
                encoding="utf-8",
            )
            with mock.patch.object(
                web_app.service_api,
                "prepare_v2fly_local_storage",
                side_effect=AssertionError("storage update must not run while runtime lock exists"),
            ):
                port = start_server(serve_core, config).port

                status, headers, body = _http_request(
                    port,
                    "/api/service/v2fly/update-local-storage",
                    method="POST",
                    body=json.dumps({}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                dry_status, _dry_headers, dry_body = _http_request(
                    port,
                    "/api/service/v2fly/update-local-storage",
                    method="POST",
                    body=json.dumps({"dry_run": True}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )

            payload = json.loads(body.decode("utf-8"))
            dry_payload = json.loads(dry_body.decode("utf-8"))
            self.assertEqual(status, 409)
            self.assertEqual(headers.get("content-type"), "application/json; charset=utf-8")
            self.assertApiError(payload, "conflict")
            self.assertEqual(dry_status, 200)
            self.assertEqual(dry_payload["status"], "dry_run")
            self.assertEqual(dry_payload["operation_id"], "v2fly-update-local-storage")
            self.assertNotIn("accepted", dry_payload)

    def test_core_start_run_conflicts_when_state_idle_but_runner_lock_exists(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            config.output.state_dir.mkdir(parents=True, exist_ok=True)
            (config.output.state_dir / "job-runner.lock").write_text(
                json.dumps({"pid": os.getpid(), "run_id": "lock-only"}),
                encoding="utf-8",
            )
            port = start_server(serve_core, config).port

            status, headers, body = _http_request(
                port,
                "/api/core/strategy-discovery/start-run",
                method="POST",
                body=json.dumps({"domains": ["youtube.com"]}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            payload = json.loads(body.decode("utf-8"))

            self.assertEqual(status, 409)
            self.assertEqual(headers.get("content-type"), "application/json; charset=utf-8")
            self.assertApiError(payload, "conflict")
            self.assertIsNone(read_state(config.output.state_dir)["current_run_id"])

    def test_ingress_budget_rejects_oversize_json_without_mutating_state(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            authorization = _bearer_authorization_for_state(config.output.state_dir)
            with mock.patch.object(web_app, "MAX_JSON_REQUEST_BYTES", 10):
                port = start_server(serve, config).port

                status, headers, body = _http_request(
                    port,
                    "/api/core/run-settings/save",
                    method="POST",
                    body=b'{"settings":{"curl_parallelism_max":99}}',
                    headers={"Content-Type": "application/json", "Authorization": authorization},
                )

            self.assertEqual(status, 413)
            self.assertEqual(headers.get("content-type"), "application/json; charset=utf-8")
            self.assertApiError(json.loads(body.decode("utf-8")), "request_too_large")
            self.assertNotIn("settings", read_state(config.output.state_dir))

    def test_backup_upload_rejects_oversize_before_import(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            with (
                mock.patch.object(web_app, "MAX_BACKUP_UPLOAD_BYTES", 10),
                mock.patch.object(web_app, "import_snapshot_archive") as import_mock,
            ):
                port = start_server(serve, config).port

                status, _headers, body = _http_request(
                    port,
                    "/api/core/backups/upload",
                    method="POST",
                    body=b"01234567890",
                    headers={"Content-Type": "application/zip"},
                )

            self.assertEqual(status, 413)
            self.assertApiError(json.loads(body.decode("utf-8")), "request_too_large")
            import_mock.assert_not_called()

    def test_backup_mutations_return_runtime_busy_while_list_and_download_remain_available(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            snapshot = web_app.create_snapshot_if_idle(config.output.state_dir)["snapshot"]["id"]
            config.output.state_dir.mkdir(parents=True, exist_ok=True)
            (config.output.state_dir / "job-runner.lock").write_text(
                json.dumps({"pid": os.getpid(), "run_id": "active-job"}),
                encoding="utf-8",
            )
            port = start_server(serve_core, config).port

            allowed_requests = [
                ("GET", "/api/core/backups/list", None, {}),
                ("GET", f"/api/core/backups/download-archive?snapshot_id={snapshot}", None, {}),
            ]
            for method, path, body, headers in allowed_requests:
                raw_body = json.dumps(body).encode("utf-8") if isinstance(body, dict) else body
                status, _response_headers, response_body = _http_request(
                    port,
                    path,
                    method=method,
                    body=raw_body,
                    headers=headers,
                )
                self.assertEqual(status, 200, (method, path, response_body.decode("utf-8", errors="replace")[:500]))

            blocked_requests = [
                ("POST", "/api/core/backups/create", {}, {"Content-Type": "application/json"}),
                ("POST", "/api/core/backups/restore", {"snapshot_id": snapshot}, {"Content-Type": "application/json"}),
                ("POST", "/api/core/backups/delete", {"snapshot_id": snapshot}, {"Content-Type": "application/json"}),
                ("POST", "/api/core/backups/upload", b"not-a-zip", {"Content-Type": "application/zip"}),
            ]
            for method, path, body, headers in blocked_requests:
                raw_body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
                status, response_headers, response_body = _http_request(
                    port,
                    path,
                    method=method,
                    body=raw_body,
                    headers=headers,
                )
                message = (method, path, response_body.decode("utf-8", errors="replace"))
                self.assertEqual(status, 409, message)
                self.assertEqual(response_headers.get("content-type"), "application/json; charset=utf-8", message)
                self.assertApiError(json.loads(response_body.decode("utf-8")), "runtime_busy")

    def test_web_proxy_serves_ui_and_forwards_api_to_core(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            core_port = start_server(serve_core, config).port
            web_port = start_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{core_port}").port

            connection = http.client.HTTPConnection("127.0.0.1", web_port, timeout=5)
            connection.request("GET", "/")
            root_response = connection.getresponse()
            root_body = root_response.read().decode("utf-8")
            connection.close()

            core_status, _core_headers, core_body = _http_request(web_port, "/api/core/status")
            service_status, _service_headers, service_body = _http_request(web_port, "/api/service/status")
            web_status, _web_headers, web_body = _http_request(web_port, "/api/web/run-preferences")
            legacy_status, _legacy_headers, legacy_body = _http_request(web_port, "/api/status")

            self.assertEqual(root_response.status, 200)
            self.assertIn("<!doctype html>", root_body.lower())
            self.assertEqual(core_status, 200)
            self.assertIn('"state"', core_body.decode("utf-8"))
            self.assertEqual(service_status, 200)
            self.assertIn('"version"', service_body.decode("utf-8"))
            self.assertEqual(web_status, 200)
            self.assertIn('"run_preferences"', web_body.decode("utf-8"))
            self.assertEqual(legacy_status, 404)
            self.assertIn("not found", legacy_body.decode("utf-8"))

    def test_web_proxy_forwards_post_body_and_query_to_core(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            core_port = start_server(serve_core, config).port
            web_port = start_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{core_port}").port

            status, headers, body = _http_request(
                web_port,
                "/api/core/run-settings/save",
                method="POST",
                body=json.dumps({"curl_parallelism_default": 17, "curl_parallelism_max": 50}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 200, body.decode("utf-8", errors="replace"))
            self.assertEqual(headers.get("content-type"), "application/json; charset=utf-8")
            self.assertEqual(17, json.loads(body.decode("utf-8"))["curl_parallelism_default"])

            status, _headers, body = _http_request(web_port, "/api/web/runs/history-page?limit=1&offset=2")
            self.assertEqual(status, 200, body.decode("utf-8", errors="replace"))
            page = json.loads(body.decode("utf-8"))
            self.assertEqual(1, page["limit"])
            self.assertEqual(2, page["offset"])

    def test_web_proxy_rejects_oversize_api_body_without_forwarding_to_core(self) -> None:
        from gp_control_plane.web import proxy as proxy_module

        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            authorization = _bearer_authorization_for_state(config.output.state_dir)
            with mock.patch.object(proxy_module, "JSON_REQUEST_MAX_BYTES", 10):
                core_port = start_server(serve_core, config).port
                web_port = start_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{core_port}").port

                status, headers, body = _http_request(
                    web_port,
                    "/api/core/run-settings/save",
                    method="POST",
                    body=b'{"curl_parallelism_default":17}',
                    headers={"Content-Type": "application/json", "Authorization": authorization},
                )

            self.assertEqual(status, 413)
            self.assertEqual(headers.get("content-type"), "application/json; charset=utf-8")
            self.assertApiError(json.loads(body.decode("utf-8")), "request_too_large")
            self.assertNotIn("settings", read_state(config.output.state_dir))

    def test_web_proxy_serves_head_requests_without_body(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            core_port = start_server(serve_core, config).port
            web_port = start_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{core_port}").port

            root_status, root_headers, root_body = _http_request(web_port, "/", method="HEAD")
            openapi_status, openapi_headers, openapi_body = _http_request(web_port, "/openapi.json", method="HEAD")

            self.assertEqual(root_status, 200)
            self.assertEqual(root_headers.get("content-type"), "text/html; charset=utf-8")
            self.assertGreater(int(root_headers.get("content-length") or "0"), 0)
            self.assertEqual(root_body, b"")
            self.assertEqual(openapi_status, 200)
            self.assertEqual(openapi_headers.get("content-type"), "application/json; charset=utf-8")
            self.assertGreater(int(openapi_headers.get("content-length") or "0"), 0)
            self.assertEqual(openapi_body, b"")

    def test_web_proxy_rejects_invalid_core_url_before_listening(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))

            with self.assertRaisesRegex(ValueError, "core_url must be an http\\(s\\) URL with host"):
                serve_web_proxy(config, "127.0.0.1", 0, core_url="127.0.0.1:8081")

    def test_web_proxy_reports_bad_gateway_when_core_is_unavailable(self) -> None:
        with (
            _captured_server_temporary_directory() as (raw, start_server),
            _reserved_unavailable_loopback_port() as unused_core_port,
        ):
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            web_port = start_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{unused_core_port}").port
            protected_headers = {"Authorization": _bearer_authorization_for_state(config.output.state_dir)}

            openapi_status, openapi_headers, openapi_body = _http_request(web_port, "/openapi.json")
            swagger_status, swagger_headers, swagger_body = _http_request(web_port, "/swagger")
            core_status, _, core_body = _http_request(web_port, "/api/core/status", headers=protected_headers)
            web_status, web_headers, web_body = _http_request(web_port, "/api/web/run-preferences", headers=protected_headers)
            legacy_status, legacy_headers, legacy_body = _http_request(web_port, "/api/status", headers=protected_headers)
            unknown_status, unknown_headers, unknown_body = _http_request(web_port, "/api/unknown", headers=protected_headers)

            self.assertEqual(openapi_status, 200)
            self.assertEqual(openapi_headers.get("content-type"), "application/json; charset=utf-8")
            self.assertIn("/api/web/run-preferences", json.loads(openapi_body.decode("utf-8"))["paths"])
            self.assertEqual(swagger_status, 200)
            self.assertEqual(swagger_headers.get("content-type"), "text/html; charset=utf-8")
            self.assertIn("SwaggerUIBundle", swagger_body.decode("utf-8"))
            self.assertEqual(core_status, 502)
            self.assertApiError(json.loads(core_body.decode("utf-8")), "core_unavailable")
            self.assertEqual(web_status, 200)
            self.assertEqual(web_headers.get("content-type"), "application/json; charset=utf-8")
            self.assertIn('"run_preferences"', web_body.decode("utf-8"))
            self.assertEqual(legacy_status, 404)
            self.assertEqual(legacy_headers.get("content-type"), "application/json; charset=utf-8")
            self.assertIn("not found", legacy_body.decode("utf-8"))
            self.assertEqual(unknown_status, 404)
            self.assertEqual(unknown_headers.get("content-type"), "application/json; charset=utf-8")
            self.assertIn("not found", unknown_body.decode("utf-8"))

    def test_web_proxy_returns_local_404_for_unknown_core_service_routes_with_dead_core(self) -> None:
        from gp_control_plane.web import proxy as proxy_module

        with (
            _captured_server_temporary_directory() as (raw, start_server),
            _reserved_unavailable_loopback_port() as unused_core_port,
        ):
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            authorization = _bearer_authorization_for_state(config.output.state_dir)
            with mock.patch.object(proxy_module, "JSON_REQUEST_MAX_BYTES", 8):
                web_port = start_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{unused_core_port}").port

                cases = (
                    ("GET", "/api/core/not-a-route", None, {}),
                    ("GET", "/api/service/not-a-route", None, {}),
                    ("POST", "/api/core/not-a-route", b"{bad", {"Content-Type": "application/json"}),
                    ("POST", "/api/service/not-a-route", b"{bad", {"Content-Type": "application/json"}),
                    ("POST", "/api/core/not-a-route", b'{"oversized":true}', {"Content-Type": "application/json"}),
                    ("POST", "/api/service/not-a-route", b'{"oversized":true}', {"Content-Type": "application/json"}),
                )
                for method, path, request_body, request_headers in cases:
                    request_headers["Authorization"] = authorization
                    status, response_headers, response_body = _http_request(
                        web_port,
                        path,
                        method=method,
                        body=request_body,
                        headers=request_headers,
                    )
                    message = (method, path, response_body.decode("utf-8", errors="replace"))
                    self.assertEqual(status, 404, message)
                    self.assertEqual(response_headers.get("content-type"), "application/json; charset=utf-8")
                    self.assertApiError(json.loads(response_body.decode("utf-8")), "not_found")
                    self.assertNotIn("core api is unavailable", response_body.decode("utf-8"), message)
                    self.assertNotIn("request body is too large", response_body.decode("utf-8"), message)

    def test_protected_api_requires_bearer_token_before_app_logic(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            port = start_server(serve, config).port

            anonymous_status, anonymous_headers, anonymous_body = _http_request(
                port,
                "/api/core/backups/list",
                authenticated=False,
            )
            self.assertEqual(anonymous_status, 401)
            self.assertEqual(anonymous_headers.get("content-type"), "application/json; charset=utf-8")
            self.assertApiError(json.loads(anonymous_body.decode("utf-8")), "authentication_required")

            status, _headers, body = _http_request(port, "/api/core/backups/list")
            self.assertEqual(status, 200)
            self.assertIn('"backups"', body.decode("utf-8"))
    def test_web_events_stream_endpoint_streams_sse_status(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            server = _start_captured_server(serve, config)
            connection = response = None
            with _cleanup_scope() as cleanup:
                cleanup.add("SSE server close", server.close)
                connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
                connection.request("GET", "/api/web/events/stream", headers=_authenticated_headers(server.port))
                response = connection.getresponse()
                cleanup.add(
                    "SSE status write after disconnect",
                    lambda: web_app.save_run_settings(config, {"curl_parallelism_default": 17}),
                )
                cleanup.add("SSE active request connection close", server.close_active_request_connections)
                cleanup.add("SSE stream close", lambda: _close_sse_stream(connection, response))
                first_line = response.readline().decode("utf-8").strip()
                second_line = response.readline().decode("utf-8").strip()

                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Content-Type"), "text/event-stream; charset=utf-8")
                self.assertEqual(first_line, "event: status")
                self.assertTrue(second_line.startswith("data:"))

    def test_captured_server_close_keeps_handler_timeout_bounded(self) -> None:
        server = mock.Mock()
        server.request_handlers_idle.wait.return_value = False
        server.active_request_handler_count = 1
        thread = mock.Mock()
        thread.is_alive.return_value = False
        captured = _CapturedTestServer(12345, server, thread)

        with mock.patch.object(socketserver.TCPServer, "server_close") as base_server_close:
            with self.assertRaisesRegex(AssertionError, "request handlers did not stop on port 12345: 1 still active"):
                captured.close()

        server.request_handlers_idle.wait.assert_called_once()
        wait_args, wait_kwargs = server.request_handlers_idle.wait.call_args
        timeout = wait_args[0] if wait_args else wait_kwargs.get("timeout")
        self.assertNotIsInstance(timeout, bool)
        self.assertIsInstance(timeout, (int, float))
        self.assertGreater(timeout, 0)
        self.assertLess(timeout, float("inf"))
        server.server_close.assert_not_called()
        base_server_close.assert_called_once_with(server)

    def test_captured_server_context_flattens_handler_wait_and_raw_close_leaves(self) -> None:
        server = mock.Mock()
        server.request_handlers_idle.wait.return_value = False
        server.active_request_handler_count = 1
        thread = mock.Mock()
        thread.is_alive.return_value = False
        captured = _CapturedTestServer(12345, server, thread)

        with mock.patch.object(
            socketserver.TCPServer,
            "server_close",
            side_effect=ValueError("raw listener close leaf failure"),
        ):
            with self.assertRaisesRegex(AssertionError, "original test failure") as raised:
                with captured:
                    self.fail("original test failure")

        notes = getattr(raised.exception, "__notes__", ())
        self.assertEqual(len(notes), 2)
        self.assertTrue(
            any(
                "captured HTTP request handler wait listener 1" in note
                and "request handlers did not stop on port 12345: 1 still active" in note
                for note in notes
            )
        )
        self.assertTrue(
            any(
                "captured HTTP raw listener close listener 1" in note
                and "ValueError('raw listener close leaf failure')" in note
                for note in notes
            )
        )

    def test_captured_server_close_retains_each_active_socket_failure_leaf(self) -> None:
        shutdown_failure = mock.Mock()
        shutdown_failure.shutdown.side_effect = OSError(errno.EIO, "socket shutdown leaf failure")
        close_failure = mock.Mock()
        close_failure.close.side_effect = OSError(errno.EIO, "socket close leaf failure")

        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            captured = _start_captured_server(serve, config)
            with captured._server._request_handlers_lock:
                captured._server._active_request_sockets.update((shutdown_failure, close_failure))

            with self.assertRaises(_CleanupFailureRecords) as raised:
                captured.close()

        self.assertEqual(len(raised.exception.records), 2)
        descriptions_by_error = {str(error): description for description, error in raised.exception.records}
        self.assertIn("[Errno 5] socket shutdown leaf failure", descriptions_by_error)
        self.assertIn("[Errno 5] socket close leaf failure", descriptions_by_error)
        self.assertIn(
            "shutdown",
            descriptions_by_error["[Errno 5] socket shutdown leaf failure"],
        )
        self.assertIn(
            "close",
            descriptions_by_error["[Errno 5] socket close leaf failure"],
        )

    def test_captured_server_context_flattens_active_socket_failure_leaves(self) -> None:
        shutdown_failure = mock.Mock()
        shutdown_failure.shutdown.side_effect = OSError(errno.EIO, "socket shutdown context failure")
        close_failure = mock.Mock()
        close_failure.close.side_effect = OSError(errno.EIO, "socket close context failure")

        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            captured = _start_captured_server(serve, config)
            with self.assertRaisesRegex(AssertionError, "original test failure") as raised:
                with captured:
                    with captured._server._request_handlers_lock:
                        captured._server._active_request_sockets.update((shutdown_failure, close_failure))
                    self.fail("original test failure")

        notes = getattr(raised.exception, "__notes__", ())
        self.assertEqual(len(notes), 2)
        self.assertTrue(
            any(
                "captured HTTP active request socket" in note
                and "shutdown" in note
                and "socket shutdown context failure" in note
                for note in notes
            )
        )
        self.assertTrue(
            any(
                "captured HTTP active request socket" in note
                and "close" in note
                and "socket close context failure" in note
                for note in notes
            )
        )

    def test_captured_server_context_manager_closes_server(self) -> None:
        server = mock.Mock()
        thread = mock.Mock()
        thread.is_alive.return_value = False
        captured = _CapturedTestServer(12345, server, thread)

        with captured as entered:
            self.assertIs(captured, entered)

        server.shutdown.assert_called_once()
        thread.join.assert_called_once_with(timeout=5)
        server.close_active_request_connections.assert_called_once()
        server.server_close.assert_called_once()

    def test_captured_server_startup_failure_closes_constructed_listener(self) -> None:
        constructed_servers: list[Any] = []

        class NoopRequestHandler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                return

        def startup_failure(_config: AppConfig, host: str, port: int) -> None:
            constructed_servers.append(web_app.ThreadingHTTPServer((host, port), NoopRequestHandler))
            raise RuntimeError("intentional startup failure")

        startup_failure.__module__ = web_app.__name__
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with self.assertRaisesRegex(AssertionError, "server failed during startup") as raised:
                _start_captured_server(startup_failure, config)

        self.assertIsInstance(raised.exception.__cause__, RuntimeError)
        self.assertEqual(str(raised.exception.__cause__), "intentional startup failure")
        self.assertEqual(len(constructed_servers), 1)
        self.assertEqual(constructed_servers[0].socket.fileno(), -1)

    def test_captured_server_abort_closes_listener_after_late_registration(self) -> None:
        constructed_servers: list[Any] = []
        construction_threads: list[threading.Thread] = []
        constructor_passed_abandon_check = threading.Event()

        class NoopRequestHandler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                return

        def wait_until_startup_is_abandoned(server: Any, startup_abandoned: threading.Event) -> None:
            constructed_servers.append(server)
            construction_threads.append(threading.current_thread())
            constructor_passed_abandon_check.set()
            self.assertTrue(startup_abandoned.wait(timeout=2))

        def startup_server(_config: AppConfig, host: str, port: int) -> None:
            web_app.ThreadingHTTPServer((host, port), NoopRequestHandler)

        startup_server.__module__ = web_app.__name__
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with self.assertRaisesRegex(AssertionError, "server (did not start serving|exited during startup)"):
                _start_captured_server(
                    startup_server,
                    config,
                    _startup_timeout=0.1,
                    _after_constructor_abandon_check=wait_until_startup_is_abandoned,
                )

        self.assertTrue(constructor_passed_abandon_check.is_set())
        self.assertEqual(len(constructed_servers), 1)
        self.assertEqual(constructed_servers[0].socket.fileno(), -1)
        self.assertEqual(len(construction_threads), 1)
        self.assertFalse(construction_threads[0].is_alive())

    def test_captured_server_startup_failure_closes_two_bound_listeners_in_reverse_order(self) -> None:
        constructed_servers: list[Any] = []
        close_order: list[Any] = []
        raw_server_close = socketserver.TCPServer.server_close

        class NoopRequestHandler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                return

        def record_raw_close(server: Any) -> None:
            close_order.append(server)
            raw_server_close(server)

        def startup_failure(_config: AppConfig, host: str, port: int) -> None:
            constructed_servers.append(web_app.ThreadingHTTPServer((host, port), NoopRequestHandler))
            constructed_servers.append(web_app.ThreadingHTTPServer((host, port), NoopRequestHandler))
            raise RuntimeError("intentional two-listener startup failure")

        startup_failure.__module__ = web_app.__name__
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with mock.patch.object(
                socketserver.TCPServer,
                "server_close",
                side_effect=record_raw_close,
            ):
                with self.assertRaisesRegex(AssertionError, "server failed during startup") as raised:
                    _start_captured_server(startup_failure, config)

        self.assertIsInstance(raised.exception.__cause__, RuntimeError)
        self.assertEqual(len(constructed_servers), 2)
        self.assertEqual(close_order, list(reversed(constructed_servers)))
        self.assertTrue(all(server.socket.fileno() == -1 for server in constructed_servers))

    def test_captured_server_abort_closes_late_listener_after_registration_is_abandoned(self) -> None:
        constructed_servers: list[Any] = []
        first_listener_registered = threading.Event()
        original_server = web_app.ThreadingHTTPServer

        class RecordingServer(original_server):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                constructed_servers.append(self)

        class NoopRequestHandler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                return

        def wait_for_abort(_server: Any, startup_abandoned: threading.Event) -> None:
            if len(constructed_servers) == 1:
                first_listener_registered.set()
                self.assertTrue(startup_abandoned.wait(timeout=2))

        def startup_server(_config: AppConfig, host: str, port: int) -> None:
            web_app.ThreadingHTTPServer((host, port), NoopRequestHandler)
            web_app.ThreadingHTTPServer((host, port), NoopRequestHandler)

        startup_server.__module__ = web_app.__name__
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with self.assertRaisesRegex(AssertionError, "server did not start serving"):
                _start_captured_server(
                    startup_server,
                    config,
                    _startup_timeout=0.1,
                    _after_constructor_abandon_check=wait_for_abort,
                    _server_type=RecordingServer,
                )

        self.assertTrue(first_listener_registered.is_set())
        self.assertEqual(len(constructed_servers), 2)
        self.assertTrue(all(server.socket.fileno() == -1 for server in constructed_servers))

    def test_captured_listener_registry_claims_late_listener_before_concurrent_pre_join_drain(self) -> None:
        registry = _CapturedListenerRegistry(threading.Event())
        listener = mock.Mock(server_address=("127.0.0.1", 12345), serve_forever_started=None)
        late_registration_complete = threading.Event()
        release_direct_close = threading.Event()
        direct_close_errors: list[BaseException] = []

        registry.abandon()

        def close_late_listener() -> None:
            try:
                self.assertFalse(registry.register(listener))
                late_registration_complete.set()
                self.assertTrue(release_direct_close.wait(timeout=2))
                registry.close_after_abandonment(listener)
            except BaseException as error:
                direct_close_errors.append(error)

        direct_close_thread = threading.Thread(target=close_late_listener)
        with mock.patch.object(socketserver.TCPServer, "server_close") as raw_server_close:
            direct_close_thread.start()
            try:
                self.assertTrue(late_registration_complete.wait(timeout=2))
                registry.close_all("pre-join")
            finally:
                release_direct_close.set()
                direct_close_thread.join(timeout=2)

        self.assertFalse(direct_close_thread.is_alive())
        self.assertEqual(direct_close_errors, [])
        raw_server_close.assert_called_once_with(listener)
        self.assertEqual(registry.snapshot(), ())

    def test_captured_server_abort_retries_late_listener_after_early_close_failure(self) -> None:
        constructed_servers: list[Any] = []
        construction_threads: list[threading.Thread] = []
        close_attempts: list[Any] = []
        first_listener_registered = threading.Event()
        original_server = web_app.ThreadingHTTPServer
        raw_server_close = socketserver.TCPServer.server_close

        class RecordingServer(original_server):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                constructed_servers.append(self)
                construction_threads.append(threading.current_thread())

        class NoopRequestHandler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                return

        def wait_for_abort(_server: Any, startup_abandoned: threading.Event) -> None:
            if len(constructed_servers) == 1:
                first_listener_registered.set()
                self.assertTrue(startup_abandoned.wait(timeout=2))

        def fail_first_late_listener_close(server: Any) -> None:
            close_attempts.append(server)
            if (
                len(constructed_servers) > 1
                and server is constructed_servers[1]
                and close_attempts.count(server) == 1
            ):
                raise OSError(errno.EIO, "late listener early close failure")
            raw_server_close(server)

        def startup_server(_config: AppConfig, host: str, port: int) -> None:
            web_app.ThreadingHTTPServer((host, port), NoopRequestHandler)
            web_app.ThreadingHTTPServer((host, port), NoopRequestHandler)

        startup_server.__module__ = web_app.__name__
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with mock.patch.object(
                socketserver.TCPServer,
                "server_close",
                side_effect=fail_first_late_listener_close,
            ):
                with self.assertRaisesRegex(AssertionError, "server did not start serving") as raised:
                    _start_captured_server(
                        startup_server,
                        config,
                        _startup_timeout=0.1,
                        _after_constructor_abandon_check=wait_for_abort,
                        _server_type=RecordingServer,
                    )

        self.assertTrue(first_listener_registered.is_set())
        self.assertEqual(len(constructed_servers), 2)
        self.assertEqual(close_attempts.count(constructed_servers[1]), 2)
        self.assertTrue(all(server.socket.fileno() == -1 for server in constructed_servers))
        self.assertTrue(all(not thread.is_alive() for thread in construction_threads))
        self.assertIsInstance(raised.exception.__cause__, _CleanupFailureRecords)
        self.assertEqual(
            [
                (description, type(error), str(error))
                for description, error in raised.exception.__cause__.records
            ],
            [
                (
                    "captured server abandoned listener raw close",
                    OSError,
                    "[Errno 5] late listener early close failure",
                )
            ],
        )

    def test_captured_server_abort_retries_registered_listener_after_pre_join_failure(self) -> None:
        constructed_servers: list[Any] = []
        construction_threads: list[threading.Thread] = []
        close_attempts: list[Any] = []
        listener_registered = threading.Event()
        original_server = web_app.ThreadingHTTPServer
        raw_server_close = socketserver.TCPServer.server_close

        class RecordingServer(original_server):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                constructed_servers.append(self)
                construction_threads.append(threading.current_thread())

        class NoopRequestHandler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                return

        def wait_for_abort(_server: Any, startup_abandoned: threading.Event) -> None:
            listener_registered.set()
            self.assertTrue(startup_abandoned.wait(timeout=2))

        def fail_first_close(server: Any) -> None:
            close_attempts.append(server)
            if close_attempts.count(server) == 1:
                raise OSError(errno.EIO, "pre-join listener close failure")
            raw_server_close(server)

        def startup_server(_config: AppConfig, host: str, port: int) -> None:
            web_app.ThreadingHTTPServer((host, port), NoopRequestHandler)

        startup_server.__module__ = web_app.__name__
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with mock.patch.object(
                socketserver.TCPServer,
                "server_close",
                side_effect=fail_first_close,
            ):
                with self.assertRaisesRegex(AssertionError, "server did not start serving") as raised:
                    _start_captured_server(
                        startup_server,
                        config,
                        _startup_timeout=0.1,
                        _after_constructor_abandon_check=wait_for_abort,
                        _server_type=RecordingServer,
                    )

        self.assertTrue(listener_registered.is_set())
        self.assertEqual(len(constructed_servers), 1)
        self.assertEqual(close_attempts, [constructed_servers[0], constructed_servers[0]])
        self.assertEqual(constructed_servers[0].socket.fileno(), -1)
        self.assertTrue(all(not thread.is_alive() for thread in construction_threads))
        self.assertTrue(
            any(
                "pre-join listener" in note
                and "OSError(5, 'pre-join listener close failure')" in note
                for note in getattr(raised.exception, "__notes__", ())
            )
        )

    def test_captured_server_successfully_owns_all_listeners_and_uses_serving_port(self) -> None:
        constructed_servers: list[Any] = []
        close_order: list[Any] = []
        original_server = web_app.ThreadingHTTPServer
        raw_server_close = socketserver.TCPServer.server_close

        class RecordingServer(original_server):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                constructed_servers.append(self)

        class NoopRequestHandler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                return

        def record_raw_close(server: Any) -> None:
            close_order.append(server)
            raw_server_close(server)

        def serve_second_listener(_config: AppConfig, host: str, port: int) -> None:
            web_app.ThreadingHTTPServer((host, port), NoopRequestHandler)
            serving_listener = web_app.ThreadingHTTPServer((host, port), NoopRequestHandler)
            serving_listener.serve_forever()

        serve_second_listener.__module__ = web_app.__name__
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with mock.patch.object(
                socketserver.TCPServer,
                "server_close",
                autospec=True,
                side_effect=record_raw_close,
            ):
                captured = _start_captured_server(
                    serve_second_listener,
                    config,
                    _server_type=RecordingServer,
                )
                try:
                    self.assertEqual(len(constructed_servers), 2)
                    self.assertEqual(captured.port, constructed_servers[1].server_address[1])
                    self.assertNotEqual(captured.port, constructed_servers[0].server_address[1])
                    self.assertTrue(constructed_servers[1].serve_forever_started.is_set())
                finally:
                    captured.close()

        self.assertEqual(close_order, list(reversed(constructed_servers)))
        self.assertTrue(all(server.socket.fileno() == -1 for server in constructed_servers))

    def test_captured_server_context_preserves_primary_failure_when_cleanup_fails(self) -> None:
        server = mock.Mock()
        server.request_handlers_idle.wait.return_value = True
        server.shutdown.side_effect = RuntimeError("cleanup failure")
        thread = mock.Mock()
        thread.is_alive.return_value = False
        captured = _CapturedTestServer(12345, server, thread)

        with self.assertRaisesRegex(AssertionError, "original test failure") as raised:
            with captured:
                self.fail("original test failure")

        self.assertTrue(any("Cleanup also failed" in note for note in getattr(raised.exception, "__notes__", ())))

    def test_captured_server_context_flattens_nested_cleanup_failures(self) -> None:
        server = mock.Mock()
        server.request_handlers_idle.wait.return_value = True
        server.shutdown.side_effect = RuntimeError("shutdown leaf failure")
        server.server_close.side_effect = ValueError("listener close leaf failure")
        thread = mock.Mock()
        thread.is_alive.return_value = False
        captured = _CapturedTestServer(12345, server, thread)

        with self.assertRaisesRegex(AssertionError, "original test failure") as raised:
            with captured:
                self.fail("original test failure")

        notes = getattr(raised.exception, "__notes__", ())
        self.assertEqual(len(notes), 2)
        self.assertTrue(
            any(
                "captured HTTP server shutdown listener 1" in note
                and "RuntimeError('shutdown leaf failure')" in note
                for note in notes
            )
        )
        self.assertTrue(
            any(
                "captured HTTP listener close listener 1" in note
                and "ValueError('listener close leaf failure')" in note
                for note in notes
            )
        )

    def test_cleanup_scope_preserves_primary_failure_and_all_cleanup_failure_details(self) -> None:
        def fail_first_cleanup() -> None:
            raise RuntimeError("first cleanup failure")

        def fail_second_cleanup() -> None:
            raise ValueError("second cleanup failure")

        with self.assertRaisesRegex(AssertionError, "original test failure") as raised:
            with _cleanup_scope() as cleanup:
                cleanup.add("first cleanup action", fail_first_cleanup)
                cleanup.add("second cleanup action", fail_second_cleanup)
                self.fail("original test failure")

        self.assertEqual(str(raised.exception), "original test failure")
        notes = getattr(raised.exception, "__notes__", ())
        self.assertTrue(any("second cleanup action" in note and "ValueError('second cleanup failure')" in note for note in notes))
        self.assertTrue(any("first cleanup action" in note and "RuntimeError('first cleanup failure')" in note for note in notes))

    def test_cleanup_scope_raises_all_nested_leaf_records_without_primary_error(self) -> None:
        def fail_nested_first_cleanup() -> None:
            raise RuntimeError("nested first failure")

        def fail_nested_second_cleanup() -> None:
            raise ValueError("nested second failure")

        def nested_cleanup() -> None:
            with _cleanup_scope() as cleanup:
                cleanup.add("nested first cleanup action", fail_nested_first_cleanup)
                cleanup.add("nested second cleanup action", fail_nested_second_cleanup)

        with self.assertRaises(_CleanupFailureRecords) as raised:
            with _cleanup_scope() as cleanup:
                cleanup.add("outer nested cleanup", nested_cleanup)

        self.assertEqual(
            [(description, type(error), str(error)) for description, error in raised.exception.records],
            [
                ("nested second cleanup action", ValueError, "nested second failure"),
                ("nested first cleanup action", RuntimeError, "nested first failure"),
            ],
        )

    def test_job_runner_tracker_preserves_primary_failure_when_join_fails(self) -> None:
        stuck_thread = mock.Mock()
        stuck_thread.is_alive.return_value = True

        with self.assertRaisesRegex(AssertionError, "original test failure") as raised:
            with _JobRunnerThreadTracker() as tracker:
                tracker._register(stuck_thread)
                self.fail("original test failure")

        stuck_thread.join.assert_called_once_with(timeout=2)
        self.assertTrue(any("Cleanup also failed" in note for note in getattr(raised.exception, "__notes__", ())))

    def test_job_runner_tracker_raises_join_failure_without_primary_error(self) -> None:
        stuck_thread = mock.Mock()
        stuck_thread.is_alive.return_value = True

        with self.assertRaisesRegex(AssertionError, "JobRunner worker thread did not stop"):
            with _JobRunnerThreadTracker() as tracker:
                tracker._register(stuck_thread)

    def test_job_runner_tracker_joins_worker_registered_during_cleanup(self) -> None:
        worker_finished = threading.Event()
        late_worker: list[threading.Thread] = []

        def start_late_worker(tracker: _JobRunnerThreadTracker) -> None:
            thread = threading.Thread(target=worker_finished.set, daemon=True)
            late_worker.append(thread)
            tracker._register(thread)
            thread.start()

        with _JobRunnerThreadTracker() as tracker:
            tracker.add_release_action("start late JobRunner worker", lambda: start_late_worker(tracker))

        self.assertTrue(worker_finished.is_set())
        self.assertEqual(len(late_worker), 1)
        self.assertFalse(late_worker[0].is_alive())

    def test_core_and_web_events_have_separate_payloads_and_cursors(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with web_app._EVENT_CURSOR_LOCK:
                web_app._EVENT_CURSOR_STATE.clear()

            core_events = web_app._events_response_payload(config, {}, stream="core")
            web_events = web_app._events_response_payload(config, {}, stream="web")

            core_types = {event["type"] for event in core_events["events"]}
            web_types = {event["type"] for event in web_events["events"]}
            self.assertIn("core.status", core_types)
            self.assertIn("strategy-discovery.progress", core_types)
            self.assertIn("strategy-candidates", core_types)
            self.assertNotIn("status", core_types)
            self.assertIn("status", web_types)
            self.assertIn("runs", web_types)
            self.assertIn("candidates", web_types)
            self.assertNotIn("core.status", web_types)
            self.assertTrue(all(str(event["event_id"]).startswith("core:") for event in core_events["events"]))
            self.assertTrue(all(str(event["event_id"]).startswith("web:") for event in web_events["events"]))

            unchanged = web_app._events_response_payload(
                config,
                {"after_id": [str(core_events["next_after_id"])]},
                stream="core",
            )
            self.assertEqual([], unchanged["events"])
            self.assertEqual(core_events["next_after_id"], unchanged["next_after_id"])

            web_app.save_run_settings(config, {"curl_parallelism_default": 17})
            changed = web_app._events_response_payload(
                config,
                {"after_id": [str(core_events["next_after_id"])]},
                stream="core",
            )

            self.assertEqual(["run-settings"], [event["type"] for event in changed["events"]])
            self.assertTrue(str(changed["events"][0]["event_id"]).startswith("core:"))

            upsert_candidates(
                config.output.state_dir,
                {
                    "candidates": [
                        {
                            "protocol": "tcp",
                            "args": "--dpi-desync=fake",
                            "domain": "youtube.com",
                            "test": "https",
                            "ip_version": "4",
                        }
                    ],
                    "common_candidates": [],
                },
                {"id": "run_1"},
            )
            candidate_changed = web_app._events_response_payload(
                config,
                {"after_id": [str(changed["next_after_id"])]},
                stream="core",
            )

            self.assertEqual(["strategy-candidates"], [event["type"] for event in candidate_changed["events"]])

    def test_split_settings_endpoints_save_runtime_defaults(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            port = start_server(serve, config).port

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            body = json.dumps(
                {
                    "settings": {
                        "enable_ipv6": True,
                        "debug_stdout": True,
                        "curl_parallelism_max": 25,
                        "curl_max_time": 1,
                        "curl_max_time_quic": 3,
                        "curl_max_time_doh": 4,
                    }
                }
            )
            connection.request("POST", "/api/core/run-settings/save", body=body, headers=_authenticated_headers(port, {"Content-Type": "application/json"}))
            response = connection.getresponse()
            saved = response.read().decode("utf-8")
            connection.close()

            self.assertEqual(response.status, 200)
            self.assertIn('"enable_ipv6":true', saved)
            self.assertIn('"debug_stdout":true', saved)
            self.assertNotIn('"settings_preset_default"', saved)
            self.assertIn('"curl_parallelism_max":25', saved)
            self.assertIn('"curl_max_time":1', saved)
            self.assertIn('"curl_max_time_quic":3', saved)
            self.assertIn('"curl_max_time_doh":4', saved)
            self.assertNotIn('"update_channel"', saved)

            stored = read_app_setting(config.output.state_dir, "run_settings")
            legacy = read_state(config.output.state_dir).get("settings")
            self.assertIsInstance(stored, dict)
            self.assertEqual(stored["curl_parallelism_max"], 25)
            self.assertNotIn("update_channel", stored)
            self.assertEqual(legacy["curl_parallelism_max"], 25)

    def test_read_settings_migrates_legacy_state_settings_to_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            config = AppConfig(output=OutputConfig(state_dir=state_dir))
            write_state(
                state_dir,
                {"settings": {"curl_parallelism_max": 33, "enable_ipv6": True, "update_channel": "prerelease"}},
            )

            settings = web_app.read_settings(config)

            self.assertEqual(settings["curl_parallelism_max"], 33)
            self.assertTrue(settings["enable_ipv6"])
            self.assertEqual(settings["update_channel"], "prerelease")
            stored = read_app_setting(state_dir, "run_settings")
            service_stored = read_app_setting(state_dir, "service_settings")
            self.assertIsInstance(stored, dict)
            self.assertIsInstance(service_stored, dict)
            self.assertEqual(stored["curl_parallelism_max"], 33)
            self.assertNotIn("update_channel", stored)
            self.assertEqual(service_stored["update_channel"], "prerelease")
            self.assertIn("settings", read_state(state_dir))

    def test_core_run_settings_save_ignores_service_settings(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))

            saved = web_app.save_run_settings(
                config,
                {"curl_parallelism_max": 44, "curl_parallelism_default": 12, "update_channel": "prerelease"},
            )

            stored = read_app_setting(config.output.state_dir, "run_settings")
            service_stored = read_app_setting(config.output.state_dir, "service_settings")
            legacy = read_state(config.output.state_dir).get("settings")
            self.assertEqual(saved["curl_parallelism_max"], 44)
            self.assertEqual(saved["curl_parallelism_default"], 12)
            self.assertNotIn("update_channel", saved)
            self.assertNotIn("update_channel", stored)
            self.assertIsNone(service_stored)
            self.assertEqual(legacy["curl_parallelism_max"], 44)
            self.assertEqual(legacy["update_channel"], "stable")

    def test_resource_budget_constants_are_wired_to_runtime_limits(self) -> None:
        from gp_control_plane import backups as backup_module
        from gp_control_plane import resource_budget
        from gp_control_plane.web import proxy as proxy_module

        self.assertEqual(web_app.MAX_BACKUP_UPLOAD_BYTES, resource_budget.BACKUP_UPLOAD_MAX_BYTES)
        self.assertEqual(web_app.MAX_JSON_REQUEST_BYTES, resource_budget.JSON_REQUEST_MAX_BYTES)
        self.assertEqual(proxy_module.JSON_REQUEST_MAX_BYTES, resource_budget.JSON_REQUEST_MAX_BYTES)
        self.assertEqual(proxy_module.BACKUP_UPLOAD_MAX_BYTES, resource_budget.BACKUP_UPLOAD_MAX_BYTES)
        self.assertEqual(resource_budget.JSON_REQUEST_MAX_BYTES, 1 * 1024 * 1024)
        self.assertEqual(resource_budget.BACKUP_UPLOAD_MAX_BYTES, 64 * 1024 * 1024)
        self.assertEqual(backup_module.BACKUP_STREAM_CHUNK_BYTES, resource_budget.BACKUP_STREAM_CHUNK_BYTES)
        self.assertEqual(proxy_module.PROXY_STREAM_CHUNK_BYTES, resource_budget.PROXY_STREAM_CHUNK_BYTES)
        self.assertEqual(
            web_app.DEFAULT_SETTINGS["curl_parallelism_max"],
            resource_budget.RASPBERRY_PI2_CURL_PARALLELISM_SAFE_MAX,
        )
        self.assertFalse(resource_budget.DIAGNOSTICS_INCLUDE_HOST_METRICS)

    def test_standard_discovery_job_uses_launch_timeout_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            payload = {
                "domains": ["youtube.com"],
                "curl_max_time": 7,
                "curl_max_time_quic": 8,
                "curl_max_time_doh": 9,
            }

            with mock.patch.object(web_app, "run_standard_discovery", return_value={"status": "success"}) as runner:
                result = web_app._job_zapret_standard_discovery(config, payload, object())

            self.assertEqual({"status": "success"}, result)
            self.assertEqual(7, runner.call_args.kwargs["curl_max_time"])
            self.assertEqual(8, runner.call_args.kwargs["curl_max_time_quic"])
            self.assertEqual(9, runner.call_args.kwargs["curl_max_time_doh"])

    def test_multi_domain_discovery_job_uses_launch_timeout_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            payload = {
                "domains": ["youtube.com", "discord.com"],
                "curl_parallelism": 2,
                "curl_max_time": 11,
                "curl_max_time_quic": 12,
                "curl_max_time_doh": 13,
            }

            with mock.patch.object(web_app, "run_multi_domain_discovery", return_value={"status": "success"}) as runner:
                result = web_app._job_zapret_multi_domain_discovery(config, payload, object())

            self.assertEqual({"status": "success"}, result)
            self.assertEqual(11, runner.call_args.kwargs["curl_max_time"])
            self.assertEqual(12, runner.call_args.kwargs["curl_max_time_quic"])
            self.assertEqual(13, runner.call_args.kwargs["curl_max_time_doh"])
            self.assertEqual(2, runner.call_args.kwargs["curl_parallelism"])

    def test_core_strategy_discovery_start_run_routes_swagger_payload(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
            save_settings = web_app.save_settings
            save_settings(config, {"curl_parallelism_max": 50, "curl_parallelism_default": 10})
            finished = threading.Event()
            snapshot_finished = threading.Event()

            def fake_run(*args: object, **kwargs: object) -> dict[str, str]:
                try:
                    return {"status": "success"}
                finally:
                    finished.set()

            def create_snapshot_after_run(*_args: object, **_kwargs: object) -> dict[str, object]:
                try:
                    return {
                        "kind": "snapshot",
                        "status": "success",
                        "completed_at": "2026-08-12T00:00:00Z",
                        "snapshot_id": "post-run-snapshot",
                    }
                finally:
                    snapshot_finished.set()

            with (
                mock.patch.object(web_app, "run_multi_domain_discovery", side_effect=fake_run) as runner,
                mock.patch.object(web_app, "create_post_run_snapshot", side_effect=create_snapshot_after_run),
            ):
                with _JobRunnerThreadTracker() as runner_threads:
                    port = start_server(serve, config).port
                    body = json.dumps(
                        {
                            "mode": "multi_domain",
                            "domains": ["youtube.com", "discord.com", "airhorn.solutions"],
                            "protocols": ["tcp", "quic"],
                            "curl_parallelism": 30,
                            "timeout_seconds": 172800,
                            "settings": {
                                "curl_max_time": 7,
                                "curl_max_time_quic": 7,
                                "enable_ipv6": False,
                                "skip_ipblock": False,
                            },
                        }
                    )
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    connection.request(
                        "POST",
                        "/api/core/strategy-discovery/start-run",
                        body=body,
                        headers=_authenticated_headers(port, {"Content-Type": "application/json", "Accept": "application/json"}),
                    )
                    response = connection.getresponse()
                    raw_response = response.read().decode("utf-8")
                    connection.close()

                    self.assertEqual(response.status, 202)
                    accepted = json.loads(raw_response)
                    self.assertTrue(accepted["accepted"])
                    self.assertTrue(accepted["run_id"])
                    self.assertEqual("queued", accepted["status"])
                    self.assertTrue(finished.wait(timeout=2))
                    runner_threads.join_tracked()
                    self.assertEqual(30, runner.call_args.kwargs["curl_parallelism"])
                    self.assertEqual(7, runner.call_args.kwargs["curl_max_time"])
                    self.assertEqual(7, runner.call_args.kwargs["curl_max_time_quic"])
                    self.assertFalse(runner.call_args.kwargs["enable_ipv6"])
                    self.assertFalse(runner.call_args.kwargs["skip_ipblock"])
                    self.assertTrue(runner.call_args.kwargs["include_quic"])
                    self.assertTrue(runner.call_args.kwargs["enable_tls12"])
                    self.assertEqual(runner_threads.tracked_count, 1)
                    self.assertTrue(snapshot_finished.is_set())

    def test_strategy_discovery_start_installs_one_time_runtime_cleanup_hook(self) -> None:
        def run_cancel_hook_regression(mode: str) -> None:
            with tempfile.TemporaryDirectory() as raw:
                tmp = Path(raw)
                config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
                captured_start_kwargs: list[dict[str, object]] = []
                original_runner = web_app.JobRunner
                worker_started = threading.Event()
                worker_cancelled = threading.Event()
                release_worker = threading.Event()
                worker_finished = threading.Event()
                snapshot_completed = threading.Event()
                cleanup_started = threading.Event()

                class CapturingJobRunner(original_runner):
                    def start(self, *args: object, **kwargs: object) -> object:
                        captured_start_kwargs.append(dict(kwargs))
                        return super().start(*args, **kwargs)

                def worker_run(*args: object, **kwargs: object) -> dict[str, str]:
                    stop_event = kwargs["stop_event"]
                    run_id = kwargs["run_id"]
                    self.assertIsInstance(stop_event, threading.Event)
                    self.assertIsInstance(run_id, str)
                    worker_started.set()
                    try:
                        self.assertTrue(stop_event.wait(timeout=2))
                        strategy_finder_process.signal_registered_process_run(run_id, "TERM")
                        worker_cancelled.set()
                        self.assertTrue(release_worker.wait(timeout=2))
                        return {"status": "stopped"}
                    finally:
                        worker_finished.set()

                def create_snapshot_when_idle(*_args: object, **_kwargs: object) -> dict[str, object]:
                    try:
                        return {
                            "kind": "snapshot",
                            "status": "success",
                            "completed_at": "2026-08-12T00:00:00Z",
                            "snapshot_id": "post-run-snapshot",
                        }
                    finally:
                        snapshot_completed.set()

                with (
                    mock.patch.object(web_app, "JobRunner", CapturingJobRunner),
                    mock.patch.object(web_app, "run_standard_discovery", side_effect=worker_run),
                    mock.patch.object(web_app, "run_multi_domain_discovery", side_effect=worker_run),
                    mock.patch.object(web_app, "create_post_run_snapshot", side_effect=create_snapshot_when_idle),
                    mock.patch.object(strategy_finder_process, "signal_registered_process_run") as root_signal,
                    mock.patch.object(
                        web_app,
                        "cleanup_nft_blockcheck_tables",
                        side_effect=cleanup_started.set,
                    ) as cleanup,
                ):
                    server = _start_captured_server(serve, config)
                    with server, _JobRunnerThreadTracker() as runner_threads:
                        runner_threads.release_barrier(release_worker)
                        runner_threads.add_release_action(
                            "cancel active JobRunner before releasing worker",
                            lambda: _stop_current_run_if_started(server.port, worker_started, worker_finished),
                        )
                        status, _headers, body = _http_request(
                            server.port,
                            "/api/core/strategy-discovery/start-run",
                            method="POST",
                            body=json.dumps({"mode": mode, "domains": ["youtube.com"], "protocols": ["tcp"]}).encode(
                                "utf-8"
                            ),
                            headers={"Content-Type": "application/json"},
                        )
                        self.assertEqual(status, 202, body.decode("utf-8", errors="replace"))
                        accepted = json.loads(body.decode("utf-8"))
                        self.assertTrue(accepted["run_id"])
                        self.assertTrue(worker_started.wait(timeout=2))
                        self.assertEqual(len(captured_start_kwargs), 1)
                        self.assertTrue(callable(captured_start_kwargs[0].get("cancel_hook")))

                        for _ in range(2):
                            stop_status, _stop_headers, stop_body = _http_request(
                                server.port,
                                "/api/core/strategy-discovery/stop-current-run",
                                method="POST",
                                body=b"{}",
                                headers={"Content-Type": "application/json"},
                            )
                            self.assertEqual(stop_status, 202, stop_body.decode("utf-8", errors="replace"))
                        self.assertTrue(worker_cancelled.wait(timeout=2))
                        self.assertTrue(cleanup_started.wait(timeout=2))
                        cleanup.assert_called_once_with()
                        root_signal.assert_called_once_with(accepted["run_id"], "TERM")

                        release_worker.set()
                        self.assertTrue(worker_finished.wait(timeout=2))
                        runner_threads.join_tracked()
                        state = read_state(config.output.state_dir)
                        status_code, _status_headers, status_body = _http_request(server.port, "/api/core/status")
                        self.assertEqual(status_code, 200, status_body.decode("utf-8", errors="replace"))
                        status_payload = json.loads(status_body.decode("utf-8"))
                        self.assertIsNone(state["current_run_id"])
                        self.assertEqual("stopped", state["last_run_status"])
                        self.assertEqual("idle", status_payload["state"])
                        self.assertEqual(runner_threads.tracked_count, 1)

                        self.assertTrue(snapshot_completed.is_set())

        for mode in ("standard", "multi_domain"):
            with self.subTest(mode=mode):
                run_cancel_hook_regression(mode)

    def test_strategy_discovery_immediate_stop_finishes_without_privileged_child(self) -> None:
        def run_immediate_stop(mode: str) -> None:
            with tempfile.TemporaryDirectory() as raw:
                tmp = Path(raw)
                config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
                original_runner = web_app.JobRunner
                worker_at_barrier = threading.Event()
                release_worker = threading.Event()
                worker_finished = threading.Event()
                cleanup_completed = threading.Event()

                class BarrierJobRunner(original_runner):
                    def _run(self, *args: object, **kwargs: object) -> None:
                        worker_at_barrier.set()
                        try:
                            if not release_worker.wait(timeout=2):
                                raise AssertionError("test did not release discovery worker")
                            super()._run(*args, **kwargs)
                        finally:
                            worker_finished.set()

                with (
                    mock.patch.object(web_app, "JobRunner", BarrierJobRunner),
                    mock.patch.object(
                        web_app,
                        "create_post_run_snapshot",
                        return_value={
                            "kind": "snapshot",
                            "status": "success",
                            "completed_at": "2026-08-12T00:00:00Z",
                            "snapshot_id": "post-run-snapshot",
                        },
                    ),
                    mock.patch.object(strategy_finder_runner.shutil, "which", return_value="/test/blockcheck2.sh"),
                    mock.patch.object(
                        strategy_finder_process,
                        "root_command",
                        side_effect=AssertionError("root_command must not run after immediate stop"),
                    ) as root_command,
                    mock.patch.object(
                        strategy_finder_process.subprocess, "Popen", side_effect=AssertionError("privileged child must not start")
                    ) as popen,
                    mock.patch.object(
                        strategy_finder_process,
                        "signal_registered_process_run",
                        side_effect=AssertionError("root signal must not run after immediate stop"),
                    ) as root_signal,
                    mock.patch.object(
                        web_app,
                        "cleanup_nft_blockcheck_tables",
                        side_effect=cleanup_completed.set,
                    ) as cleanup,
                ):
                    server = _start_captured_server(serve, config)
                    with server, _JobRunnerThreadTracker() as runner_threads:
                        runner_threads.release_barrier(release_worker)
                        start_status, _headers, start_body = _http_request(
                            server.port,
                            "/api/core/strategy-discovery/start-run",
                            method="POST",
                            body=json.dumps({"mode": mode, "domains": ["youtube.com"], "protocols": ["tcp"]}).encode(
                                "utf-8"
                            ),
                            headers={"Content-Type": "application/json"},
                        )
                        self.assertEqual(start_status, 202, start_body.decode("utf-8", errors="replace"))
                        accepted = json.loads(start_body.decode("utf-8"))
                        self.assertTrue(worker_at_barrier.wait(timeout=2))

                        stop_status, _headers, stop_body = _http_request(
                            server.port,
                            "/api/core/strategy-discovery/stop-current-run",
                            method="POST",
                            body=b"{}",
                            headers={"Content-Type": "application/json"},
                        )
                        self.assertEqual(stop_status, 202, stop_body.decode("utf-8", errors="replace"))
                        self.assertEqual(accepted["run_id"], json.loads(stop_body.decode("utf-8"))["run_id"])
                        self.assertTrue(cleanup_completed.wait(timeout=2))
                        cleanup.assert_called_once_with()

                        release_worker.set()
                        self.assertTrue(worker_finished.wait(timeout=2))
                        runner_threads.join_tracked()

                        state = read_state(config.output.state_dir)
                        status_status, _headers, status_body = _http_request(server.port, "/api/core/status")
                        history_status, _headers, history_body = _http_request(server.port, "/api/core/runs/history")
                        self.assertEqual(status_status, 200, status_body.decode("utf-8", errors="replace"))
                        self.assertEqual(history_status, 200, history_body.decode("utf-8", errors="replace"))
                        status_payload = json.loads(status_body.decode("utf-8"))
                        history = json.loads(history_body.decode("utf-8"))

                        self.assertIsNone(state["current_run_id"])
                        self.assertIsNone(state["last_error"])
                        self.assertEqual("stopped", state["last_run_status"])
                        self.assertEqual("idle", status_payload["state"])
                        self.assertEqual(accepted["run_id"], history["runs"][0]["run_id"])
                        self.assertEqual("stopped", history["runs"][0]["status"])
                        root_command.assert_not_called()
                        popen.assert_not_called()
                        root_signal.assert_not_called()
                        self.assertEqual(runner_threads.tracked_count, 1)

        for mode in ("standard", "multi_domain"):
            with self.subTest(mode=mode):
                run_immediate_stop(mode)

    def test_strategy_discovery_stop_during_root_command_finishes_without_privileged_child(self) -> None:
        def run_stop_during_root_command(mode: str) -> None:
            with tempfile.TemporaryDirectory() as raw:
                tmp = Path(raw)
                config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
                original_runner = web_app.JobRunner
                root_command_entered = threading.Event()
                release_root_command = threading.Event()
                worker_finished = threading.Event()
                cleanup_completed = threading.Event()

                class ObservingJobRunner(original_runner):
                    def _run(self, *args: object, **kwargs: object) -> None:
                        try:
                            super()._run(*args, **kwargs)
                        finally:
                            worker_finished.set()

                def root_command_that_waits(command: list[str], **_kwargs: object) -> list[str]:
                    root_command_entered.set()
                    if not release_root_command.wait(timeout=2):
                        raise AssertionError("test did not release root_command")
                    return command

                with (
                    mock.patch.object(web_app, "JobRunner", ObservingJobRunner),
                    mock.patch.object(
                        web_app,
                        "create_post_run_snapshot",
                        return_value={
                            "kind": "snapshot",
                            "status": "success",
                            "completed_at": "2026-08-12T00:00:00Z",
                            "snapshot_id": "post-run-snapshot",
                        },
                    ),
                    mock.patch.object(strategy_finder_runner.shutil, "which", return_value="/test/blockcheck2.sh"),
                    mock.patch.object(
                        strategy_finder_process,
                        "root_command",
                        side_effect=root_command_that_waits,
                    ) as root_command,
                    mock.patch.object(
                        strategy_finder_process.subprocess,
                        "Popen",
                        side_effect=AssertionError("privileged child must not start after stop during root_command"),
                    ) as popen,
                    mock.patch.object(
                        strategy_finder_process,
                        "signal_registered_process_run",
                        side_effect=AssertionError("root signal must not run after stop during root_command"),
                    ) as root_signal,
                    mock.patch.object(
                        web_app,
                        "cleanup_nft_blockcheck_tables",
                        side_effect=cleanup_completed.set,
                    ) as cleanup,
                ):
                    server = _start_captured_server(serve, config)
                    with server, _JobRunnerThreadTracker() as runner_threads:
                        runner_threads.release_barrier(release_root_command)
                        start_status, _headers, start_body = _http_request(
                            server.port,
                            "/api/core/strategy-discovery/start-run",
                            method="POST",
                            body=json.dumps({"mode": mode, "domains": ["youtube.com"], "protocols": ["tcp"]}).encode(
                                "utf-8"
                            ),
                            headers={"Content-Type": "application/json"},
                        )
                        self.assertEqual(start_status, 202, start_body.decode("utf-8", errors="replace"))
                        accepted = json.loads(start_body.decode("utf-8"))
                        self.assertTrue(root_command_entered.wait(timeout=2))

                        stop_status, _headers, stop_body = _http_request(
                            server.port,
                            "/api/core/strategy-discovery/stop-current-run",
                            method="POST",
                            body=b"{}",
                            headers={"Content-Type": "application/json"},
                        )
                        self.assertEqual(stop_status, 202, stop_body.decode("utf-8", errors="replace"))
                        self.assertEqual(accepted["run_id"], json.loads(stop_body.decode("utf-8"))["run_id"])
                        self.assertTrue(cleanup_completed.wait(timeout=2))
                        cleanup.assert_called_once_with()

                        release_root_command.set()
                        self.assertTrue(worker_finished.wait(timeout=2))
                        runner_threads.join_tracked()

                        state = read_state(config.output.state_dir)
                        history_status, _headers, history_body = _http_request(server.port, "/api/core/runs/history")
                        self.assertEqual(history_status, 200, history_body.decode("utf-8", errors="replace"))
                        history = json.loads(history_body.decode("utf-8"))
                        self.assertIsNone(state["last_error"])
                        self.assertEqual("stopped", state["last_run_status"])
                        self.assertEqual(accepted["run_id"], history["runs"][0]["run_id"])
                        self.assertEqual("stopped", history["runs"][0]["status"])
                        root_command.assert_called_once()
                        popen.assert_not_called()
                        root_signal.assert_not_called()
                        self.assertEqual(runner_threads.tracked_count, 1)

        for mode in ("standard", "multi_domain"):
            with self.subTest(mode=mode):
                run_stop_during_root_command(mode)

    def test_strategy_discovery_stop_after_precheck_before_popen_finishes_stopped(self) -> None:
        def run_stop_after_precheck_before_popen(mode: str) -> None:
            with tempfile.TemporaryDirectory() as raw:
                tmp = Path(raw)
                config = AppConfig(output=OutputConfig(state_dir=tmp / "state"))
                original_runner = web_app.JobRunner
                original_stdout_log_enter = strategy_finder_writers._RotatingTextWriter.__enter__
                stdout_log_opened = threading.Event()
                release_stdout_log = threading.Event()
                worker_finished = threading.Event()
                cleanup_completed = threading.Event()

                class ObservingJobRunner(original_runner):
                    def _run(self, *args: object, **kwargs: object) -> None:
                        try:
                            super()._run(*args, **kwargs)
                        finally:
                            worker_finished.set()

                def open_stdout_log_at_barrier(writer: strategy_finder_writers._RotatingTextWriter) -> strategy_finder_writers._RotatingTextWriter:
                    result = original_stdout_log_enter(writer)
                    if writer._path.name.endswith(".stdout.log"):
                        stdout_log_opened.set()
                        if not release_stdout_log.wait(timeout=2):
                            raise AssertionError("test did not release stdout-log barrier")
                    return result

                with (
                    mock.patch.object(web_app, "JobRunner", ObservingJobRunner),
                    mock.patch.object(
                        web_app,
                        "create_post_run_snapshot",
                        return_value={
                            "kind": "snapshot",
                            "status": "success",
                            "completed_at": "2026-08-12T00:00:00Z",
                            "snapshot_id": "post-run-snapshot",
                        },
                    ),
                    mock.patch.object(strategy_finder_runner.shutil, "which", return_value="/test/blockcheck2.sh"),
                    mock.patch.object(strategy_finder_process, "root_command", side_effect=lambda command, **_kwargs: command) as root_command,
                    mock.patch.object(
                        strategy_finder_writers._RotatingTextWriter,
                        "__enter__",
                        new=open_stdout_log_at_barrier,
                    ),
                    mock.patch.object(
                        strategy_finder_process.subprocess,
                        "Popen",
                        side_effect=AssertionError("privileged child must not start after stop before Popen"),
                    ) as popen,
                    mock.patch.object(
                        strategy_finder_process,
                        "signal_registered_process_run",
                        side_effect=AssertionError("root signal must not run before a child is launched"),
                    ) as root_signal,
                    mock.patch.object(
                        web_app,
                        "cleanup_nft_blockcheck_tables",
                        side_effect=cleanup_completed.set,
                    ) as cleanup,
                ):
                    server = _start_captured_server(serve, config)
                    with server, _JobRunnerThreadTracker() as runner_threads:
                        runner_threads.release_barrier(release_stdout_log)
                        start_status, _headers, start_body = _http_request(
                            server.port,
                            "/api/core/strategy-discovery/start-run",
                            method="POST",
                            body=json.dumps({"mode": mode, "domains": ["youtube.com"], "protocols": ["tcp"]}).encode(
                                "utf-8"
                            ),
                            headers={"Content-Type": "application/json"},
                        )
                        self.assertEqual(start_status, 202, start_body.decode("utf-8", errors="replace"))
                        accepted = json.loads(start_body.decode("utf-8"))
                        self.assertTrue(stdout_log_opened.wait(timeout=2))

                        stop_status, _headers, stop_body = _http_request(
                            server.port,
                            "/api/core/strategy-discovery/stop-current-run",
                            method="POST",
                            body=b"{}",
                            headers={"Content-Type": "application/json"},
                        )
                        self.assertEqual(stop_status, 202, stop_body.decode("utf-8", errors="replace"))
                        self.assertEqual(accepted["run_id"], json.loads(stop_body.decode("utf-8"))["run_id"])
                        self.assertTrue(cleanup_completed.wait(timeout=2))
                        cleanup.assert_called_once_with()

                        release_stdout_log.set()
                        self.assertTrue(worker_finished.wait(timeout=2))
                        runner_threads.join_tracked()

                        state = read_state(config.output.state_dir)
                        status_status, _headers, status_body = _http_request(server.port, "/api/core/status")
                        history_status, _headers, history_body = _http_request(server.port, "/api/core/runs/history")
                        self.assertEqual(status_status, 200, status_body.decode("utf-8", errors="replace"))
                        self.assertEqual(history_status, 200, history_body.decode("utf-8", errors="replace"))
                        status_payload = json.loads(status_body.decode("utf-8"))
                        history = json.loads(history_body.decode("utf-8"))

                        self.assertIsNone(state["current_run_id"])
                        self.assertIsNone(state["last_error"])
                        self.assertEqual("stopped", state["last_run_status"])
                        self.assertEqual("idle", status_payload["state"])
                        self.assertEqual(accepted["run_id"], history["runs"][0]["run_id"])
                        self.assertEqual("stopped", history["runs"][0]["status"])
                        root_command.assert_called_once()
                        popen.assert_not_called()
                        root_signal.assert_not_called()
                        self.assertEqual(runner_threads.tracked_count, 1)

        for mode in ("standard", "multi_domain"):
            with self.subTest(mode=mode):
                run_stop_after_precheck_before_popen(mode)

    def test_core_strategy_discovery_start_run_rejects_unknown_protocols(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported protocols"):
            web_app._core_strategy_discovery_job_payload(
                {
                    "mode": "multi_domain",
                    "domains": ["youtube.com"],
                    "protocols": ["tcp", "bad"],
                }
            )

    def test_core_strategy_discovery_start_run_quic_only_disables_tcp_protocols(self) -> None:
        job_name, job_payload = web_app._core_strategy_discovery_job_payload(
            {
                "mode": "multi_domain",
                "domains": ["youtube.com"],
                "protocols": ["quic"],
                "settings": {
                    "curl_max_time_quic": 7,
                    "enable_ipv6": False,
                },
            }
        )

        self.assertEqual("zapret-multi-domain-discovery", job_name)
        self.assertEqual(["youtube.com"], job_payload["domains"])
        self.assertTrue(job_payload["include_quic"])
        self.assertFalse(job_payload["enable_http"])
        self.assertFalse(job_payload["enable_tls12"])
        self.assertFalse(job_payload["enable_tls13"])
        self.assertEqual(7, job_payload["curl_max_time_quic"])
        self.assertFalse(job_payload["enable_ipv6"])

    def test_core_strategy_discovery_start_run_rejects_empty_domain_list(self) -> None:
        with self.assertRaisesRegex(ValueError, "domains are required"):
            web_app._core_strategy_discovery_job_payload(
                {
                    "mode": "multi_domain",
                    "domains": ["", "   "],
                    "protocols": ["tcp"],
                }
            )

    def test_core_strategy_discovery_start_run_rejects_unknown_top_level_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported start-run fields: mode_settings"):
            web_app._core_strategy_discovery_job_payload(
                {
                    "mode": "multi_domain",
                    "domains": ["youtube.com"],
                    "mode_settings": {"common_strategy_min_domains": 2},
                }
            )

    def test_core_strategy_discovery_start_run_rejects_unknown_settings_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported start-run settings: mode_settings"):
            web_app._core_strategy_discovery_job_payload(
                {
                    "mode": "multi_domain",
                    "domains": ["youtube.com"],
                    "settings": {"mode_settings": {"common_strategy_min_domains": 2}},
                }
            )

    def test_core_strategy_candidates_require_filter_and_export_streams_full_facts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            config = AppConfig(output=OutputConfig(state_dir=state_dir))
            upsert_candidates(
                state_dir,
                {
                    "candidates": [
                        {"protocol": "tcp", "args": "--dpi-desync=fake", "domain": "youtube.com"},
                        {"protocol": "quic", "args": "--dpi-desync=fake", "domain": "discord.com"},
                    ]
                },
                {"id": "run-1"},
            )

            with self.assertRaisesRegex(ValueError, "filter is required"):
                web_app.core_api.strategy_candidates_payload(config, {})

            payload = web_app.core_api.strategy_candidates_payload(config, {"domain": ["youtube.com"]})
            exported = [
                json.loads(line.decode("utf-8"))
                for line in web_app.core_api.iter_strategy_candidates_export_lines(config, {})
            ]

            self.assertEqual(len(payload["candidates"]), 1)
            self.assertEqual(payload["total"], 1)
            self.assertEqual(payload["filters"], {"domains": ["youtube.com"]})
            self.assertEqual(payload["candidates"][0]["seen"][0]["domain"], "youtube.com")
            self.assertEqual(len(exported), 2)
            self.assertEqual({item["protocol"] for item in exported}, {"tcp", "quic"})
            self.assertNotIn("has_more", payload)
            self.assertIn("id", payload["candidates"][0])
            self.assertIn("protocol", payload["candidates"][0])
            self.assertIn("seen", payload["candidates"][0])

    def test_service_status_reports_installation_shape_without_web_unit_hardcode(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with mock.patch.dict(os.environ, {"GP_INSTALL_WEB": "off"}, clear=False):
                payload = web_app.service_api.service_status_payload(
                    config,
                    current_version="0.test",
                    runtime_role="core",
                )

            self.assertEqual(payload["state"], "active")
            self.assertEqual(payload["mode"], "core")
            self.assertNotIn("unit", payload)
            self.assertEqual(payload["services"]["core"]["name"], "gp-control-plane-core.service")
            self.assertEqual(payload["services"]["core"]["state"], "active")
            self.assertEqual(payload["services"]["web"]["state"], "disabled")
            self.assertFalse(payload["services"]["web"]["required"])
            self.assertIsInstance(payload["data_state"], dict)
            self.assertIn("v2fly", payload["data_state"])

    def test_service_status_reports_missing_v2fly_as_data_state_not_runtime_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with mock.patch.object(
                web_app.service_api,
                "v2fly_storage_status_payload",
                return_value={"state": "missing", "source_commit": "", "group_count": 0},
            ):
                payload = web_app.service_api.service_status_payload(
                    config,
                    current_version="0.test",
                    runtime_role="core",
                    web_enabled=True,
                )

            self.assertEqual(payload["state"], "active")
            self.assertEqual(payload["data_state"]["state"], "missing")
            self.assertEqual(payload["data_state"]["v2fly"]["state"], "missing")
            self.assertEqual(payload["data_state"]["v2fly"]["group_count"], 0)

    def test_run_preferences_endpoint_saves_last_finder_form(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            port = start_server(serve, config).port

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            body = json.dumps(
                {
                    "run_preferences": {
                        "domains": ["youtube.com", "discord.com"],
                        "domain_preset": "custom",
                        "discovery_profile": "custom",
                        "settings_preset": "accelerated",
                        "run_mode": "multi",
                        "curl_parallelism": 19,
                        "enable_http": True,
                        "enable_tls12": True,
                        "enable_tls13": False,
                        "include_quic": True,
                        "enable_ipv6": False,
                        "scan_level": "force",
                        "repeats": 2,
                        "repeat_parallel": True,
                        "skip_dnscheck": False,
                        "skip_ipblock": True,
                        "limit_time_enabled": True,
                        "timeout_hours": 3.5,
                    }
                }
            )
            connection.request("POST", "/api/web/run-preferences", body=body, headers=_authenticated_headers(port, {"Content-Type": "application/json"}))
            response = connection.getresponse()
            saved = response.read().decode("utf-8")
            connection.close()

            self.assertEqual(response.status, 200)
            self.assertIn('"run_mode":"multi"', saved)
            self.assertIn('"curl_parallelism":19', saved)
            self.assertIn('"scan_level":"force"', saved)
            self.assertIn('"timeout_hours":3.5', saved)
            self.assertNotIn('"settings_preset"', saved)

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", "/api/web/run-preferences", headers=_authenticated_headers(port))
            response = connection.getresponse()
            status = response.read().decode("utf-8")
            connection.close()

            self.assertEqual(response.status, 200)
            self.assertIn('"run_preferences"', status)
            self.assertIn('"youtube.com"', status)
            self.assertIn('"discord.com"', status)
            self.assertNotIn('"settings_preset"', status)

    def test_web_preset_domain_endpoints_save_and_page_domains(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            port = start_server(serve, config).port

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            body = json.dumps({"scope": "finder", "name": "mine", "domains": ["youtube.com", "discord.com", "discordcdn.com"]})
            connection.request("POST", "/api/web/presets/save", body=body, headers=_authenticated_headers(port, {"Content-Type": "application/json"}))
            response = connection.getresponse()
            saved = response.read().decode("utf-8")
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertIn('"mine"', saved)

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", "/api/web/presets/domains?scope=finder&name=mine&limit=2", headers=_authenticated_headers(port))
            response = connection.getresponse()
            page = response.read().decode("utf-8")
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertIn('"total":3', page)
            self.assertIn('"has_more":true', page)

    def test_system_preset_api_allows_empty_save_and_user_only_delete(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            port = start_server(serve, config).port

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            body = json.dumps({"scope": "finder", "name": "required", "kind": "system", "domains": []})
            connection.request("POST", "/api/web/presets/save", body=body, headers=_authenticated_headers(port, {"Content-Type": "application/json"}))
            response = connection.getresponse()
            saved = response.read().decode("utf-8")
            connection.close()

            self.assertEqual(response.status, 200)
            self.assertIn('"system"', saved)
            self.assertIn('"required":[]', saved)
            self.assertIn('"enabled_count":0', saved)

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            body = json.dumps({"scope": "finder", "name": "mine", "domains": ["youtube.com"]})
            connection.request("POST", "/api/web/presets/save", body=body, headers=_authenticated_headers(port, {"Content-Type": "application/json"}))
            response = connection.getresponse()
            response.read()
            connection.close()
            self.assertEqual(response.status, 200)

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            body = json.dumps({"scope": "finder", "names": ["mine", "required"]})
            connection.request(
                "POST",
                "/api/web/presets/delete-user-lists",
                body=body,
                headers=_authenticated_headers(port, {"Content-Type": "application/json"}),
            )
            response = connection.getresponse()
            deleted = response.read().decode("utf-8")
            connection.close()

            self.assertEqual(response.status, 200)
            self.assertNotIn('"mine"', deleted)
            self.assertIn('"required":[]', deleted)
            self.assertIn('"kind":"system"', deleted)

    def test_discovery_profiles_endpoint_is_removed(self) -> None:
        with _captured_server_temporary_directory() as (raw, start_server):
            tmp = Path(raw)
            config = AppConfig(
                output=OutputConfig(
                    state_dir=tmp / "state",
                ),
            )
            port = start_server(serve, config).port

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            body = json.dumps(
                {
                    "profiles": {
                        "night-test": {
                            "title": "Night test",
                            "enable_http": True,
                            "enable_tls12": True,
                            "enable_tls13": True,
                            "include_quic": True,
                            "scan_level": "force",
                            "repeats": 99,
                            "curl_parallelism": 99,
                            "limit_time_enabled": True,
                            "timeout_hours": 99,
                        },
                        "standard": {
                            "title": "Changed built-in",
                            "enable_tls12": False,
                        },
                    }
                }
            )
            connection.request("POST", "/api/discovery-profiles", body=body, headers=_authenticated_headers(port, {"Content-Type": "application/json"}))
            response = connection.getresponse()
            saved = response.read().decode("utf-8")
            connection.close()

            self.assertIn(response.status, {404, 405})
            self.assertNotIn('"night-test"', saved)
            self.assertNotIn("Changed built-in", saved)


class _CleanupFailureRecords(AssertionError):
    """Structured leaf cleanup failures that survive nested cleanup scopes."""

    def __init__(self, records: list[tuple[str, BaseException]]) -> None:
        self.records = tuple(records)
        super().__init__(
            "\n".join(
                f"Cleanup failed during {description}: {error!r}"
                for description, error in self.records
            )
        )


def _cleanup_failure_records(error: BaseException, description: str) -> tuple[tuple[str, BaseException], ...]:
    if isinstance(error, _CleanupFailureRecords):
        return error.records
    return ((description, error),)


class _CleanupActions:
    """Run every cleanup action without hiding the test's primary failure."""

    def __init__(self) -> None:
        self._actions: list[tuple[str, Any]] = []

    def add(self, description: str, action: Any) -> None:
        self._actions.append((description, action))

    def run(self, primary_error: BaseException | None = None) -> None:
        cleanup_failures: list[tuple[str, BaseException]] = []
        for description, action in reversed(self._actions):
            try:
                action()
            except BaseException as error:
                cleanup_failures.extend(_cleanup_failure_records(error, description))
        if not cleanup_failures:
            return
        if primary_error is not None:
            for description, error in cleanup_failures:
                primary_error.add_note(f"Cleanup also failed during {description}: {error!r}")
            return
        raise _CleanupFailureRecords(cleanup_failures)


@contextlib.contextmanager
def _cleanup_scope() -> Any:
    cleanup = _CleanupActions()
    primary_error: BaseException | None = None
    try:
        yield cleanup
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup.run(primary_error)


class _CapturedTestServer:
    def __init__(
        self,
        port: int,
        server: Any,
        thread: threading.Thread,
        listener_registry: _CapturedListenerRegistry | None = None,
    ) -> None:
        self.port = port
        self._server = server
        self._thread = thread
        self._listener_registry = listener_registry
        self._close_lock = threading.Lock()
        self._closed = False

    def __enter__(self) -> _CapturedTestServer:
        return self

    def __exit__(self, _exc_type: Any, exc_value: BaseException | None, _traceback: Any) -> None:
        cleanup = _CleanupActions()
        cleanup.add("captured HTTP server close", self.close)
        cleanup.run(exc_value)

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True

        listeners = self._owned_listeners()

        def join_server_thread() -> None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                raise AssertionError(f"server thread did not stop on port {self.port}")

        def close_listener_after_handlers(listener: Any, index: int) -> None:
            if not self._listener_started_serving(listener):
                try:
                    socketserver.TCPServer.server_close(listener)
                except BaseException as error:
                    raise _CleanupFailureRecords(
                        _cleanup_failure_records(
                            error,
                            f"captured HTTP raw listener close listener {index}",
                        )
                    ) from error
                self._release_listener_after_close(listener)
                return

            cleanup_failures: list[tuple[str, BaseException]] = []
            try:
                self._wait_for_request_handlers(listener)
            except BaseException as error:
                cleanup_failures.extend(
                    _cleanup_failure_records(
                        error,
                        f"captured HTTP request handler wait listener {index}",
                    )
                )
                # ThreadingMixIn.server_close() joins every registered request
                # handler without a timeout. The bounded wait is the diagnostic
                # boundary, so only close the listening socket on this path.
                try:
                    socketserver.TCPServer.server_close(listener)
                except BaseException as error:
                    cleanup_failures.extend(
                        _cleanup_failure_records(
                            error,
                            f"captured HTTP raw listener close listener {index}",
                        )
                    )
                else:
                    self._release_listener_after_close(listener)
                raise _CleanupFailureRecords(cleanup_failures)
            try:
                listener.server_close()
            except BaseException:
                raise
            self._release_listener_after_close(listener)

        try:
            with _cleanup_scope() as cleanup:
                # Add in creation order so every listener phase runs in LIFO order.
                for index, listener in enumerate(listeners, start=1):
                    cleanup.add(
                        f"captured HTTP listener close listener {index}",
                        lambda listener=listener, index=index: close_listener_after_handlers(listener, index),
                    )
                for index, listener in enumerate(listeners, start=1):
                    if self._listener_started_serving(listener):
                        cleanup.add(
                            f"captured HTTP active connection close listener {index}",
                            listener.close_active_request_connections,
                        )
                cleanup.add("captured HTTP server thread join", join_server_thread)
                for index, listener in enumerate(listeners, start=1):
                    if self._listener_started_serving(listener):
                        cleanup.add(f"captured HTTP server shutdown listener {index}", listener.shutdown)
        finally:
            self._finalize_owned_listeners()

    def close_active_request_connections(self) -> None:
        self._server.close_active_request_connections()

    def _owned_listeners(self) -> tuple[Any, ...]:
        if self._listener_registry is None:
            return (self._server,)
        self._listener_registry.abandon()
        return self._listener_registry.snapshot()

    def _release_listener_after_close(self, listener: Any) -> None:
        if self._listener_registry is not None:
            self._listener_registry.release(listener)

    def _finalize_owned_listeners(self) -> None:
        if self._listener_registry is not None:
            self._listener_registry.finalize()

    @staticmethod
    def _listener_started_serving(listener: Any) -> bool:
        serving_started = getattr(listener, "serve_forever_started", None)
        return serving_started is None or bool(serving_started.is_set())

    def _wait_for_request_handlers(self, listener: Any, timeout: float = 5) -> None:
        if listener.request_handlers_idle.wait(timeout):
            return
        port = self.port if self._listener_registry is None else listener.server_address[1]
        raise AssertionError(
            f"request handlers did not stop on port {port}: "
            f"{listener.active_request_handler_count} still active"
        )


class _CapturedListenerRegistry:
    def __init__(self, startup_abandoned: threading.Event) -> None:
        self._startup_abandoned = startup_abandoned
        self._lock = threading.Lock()
        self._listeners: list[Any] = []

    def register(self, listener: Any) -> bool:
        with self._lock:
            self._listeners.append(listener)
            return not self._startup_abandoned.is_set()

    def abandon(self) -> None:
        with self._lock:
            self._startup_abandoned.set()

    def snapshot(self) -> tuple[Any, ...]:
        with self._lock:
            return tuple(self._listeners)

    def release(self, listener: Any) -> None:
        with self._lock:
            self._listeners.remove(listener)

    def finalize(self) -> None:
        with self._lock:
            self._listeners.clear()

    def close_after_abandonment(self, listener: Any) -> None:
        claim_index = self._claim(listener)
        if claim_index is None:
            return
        try:
            socketserver.TCPServer.server_close(listener)
        except BaseException as error:
            self._requeue(listener, claim_index)
            raise _CleanupFailureRecords(
                _cleanup_failure_records(error, "captured server abandoned listener raw close")
            ) from error

    def close_all(self, phase: str, *, final: bool = False) -> None:
        listeners = self.snapshot()

        try:
            with _cleanup_scope() as cleanup:
                # Register in creation order so _CleanupActions closes in LIFO order.
                for listener in listeners:
                    cleanup.add(
                        f"{phase} listener {listener.server_address!r}",
                        lambda listener=listener: self._close_claimed(listener),
                    )
        finally:
            if final:
                self.finalize()

    def _claim(self, listener: Any) -> int | None:
        with self._lock:
            for index, pending_listener in enumerate(self._listeners):
                if pending_listener is listener:
                    self._listeners.pop(index)
                    return index
        return None

    def _requeue(self, listener: Any, index: int) -> None:
        with self._lock:
            self._listeners.insert(min(index, len(self._listeners)), listener)

    def _close_claimed(self, listener: Any) -> None:
        claim_index = self._claim(listener)
        if claim_index is None:
            return
        try:
            self._close(listener)
        except BaseException:
            self._requeue(listener, claim_index)
            raise

    @staticmethod
    def _close(listener: Any) -> None:
        serving_started = getattr(listener, "serve_forever_started", None)
        if serving_started is None or not serving_started.is_set():
            socketserver.TCPServer.server_close(listener)
            return

        with _cleanup_scope() as cleanup:
            cleanup.add("serving listener close", listener.server_close)
            cleanup.add("serving listener active connection close", listener.close_active_request_connections)
            cleanup.add("serving listener shutdown", listener.shutdown)


class _JobRunnerThreadTracker:
    """Scoped capture of JobRunner worker threads without changing production code."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._release_actions: list[tuple[str, Any]] = []
        self._joined_thread_ids: set[int] = set()
        self._patch: Any | None = None

    def __enter__(self) -> _JobRunnerThreadTracker:
        original_threading = jobs.threading
        tracker = self

        class ThreadingProxy:
            def Thread(self, *args: Any, **kwargs: Any) -> threading.Thread:
                thread = original_threading.Thread(*args, **kwargs)
                if tracker._is_job_runner_worker(kwargs.get("target")):
                    tracker._register(thread)
                return thread

            def __getattr__(self, name: str) -> Any:
                return getattr(original_threading, name)

        self._patch = mock.patch.object(jobs, "threading", ThreadingProxy())
        self._patch.start()
        return self

    def __exit__(self, _exc_type: Any, exc_value: BaseException | None, _traceback: Any) -> None:
        assert self._patch is not None
        cleanup = _CleanupActions()
        cleanup.add("restore JobRunner threading module", self._patch.stop)
        cleanup.add("JobRunner worker thread join", self.join_tracked)
        for description, action in self._release_actions:
            cleanup.add(description, action)
        cleanup.run(exc_value)

    @property
    def threads(self) -> tuple[threading.Thread, ...]:
        with self._lock:
            return tuple(self._threads)

    @property
    def tracked_count(self) -> int:
        with self._lock:
            return len(self._threads)

    def release_barrier(self, barrier: threading.Event, description: str = "release JobRunner test barrier") -> None:
        self._release_actions.append((description, barrier.set))

    def add_release_action(self, description: str, action: Any) -> None:
        self._release_actions.append((description, action))

    def join_tracked(self) -> None:
        while True:
            with self._lock:
                pending = [thread for thread in self._threads if id(thread) not in self._joined_thread_ids]
            if not pending:
                return
            for thread in pending:
                if thread is threading.current_thread():
                    raise AssertionError("JobRunner tracker attempted to join its current thread")
                try:
                    thread.join(timeout=2)
                except BaseException as error:
                    raise AssertionError(f"JobRunner worker thread could not be joined: {thread!r}") from error
                if thread.is_alive():
                    raise AssertionError(f"JobRunner worker thread did not stop: {thread!r}")
                with self._lock:
                    self._joined_thread_ids.add(id(thread))

    @staticmethod
    def _is_job_runner_worker(target: Any) -> bool:
        return (
            getattr(target, "__name__", "") == "_run"
            and isinstance(getattr(target, "__self__", None), jobs.JobRunner)
        )

    def _register(self, thread: threading.Thread) -> None:
        with self._lock:
            self._threads.append(thread)


@contextlib.contextmanager
def _captured_server_temporary_directory() -> Any:
    with tempfile.TemporaryDirectory() as raw:
        with _cleanup_scope() as cleanup:
            def start_server(function: Any, config: AppConfig, **kwargs: Any) -> _CapturedTestServer:
                server = _start_captured_server(function, config, **kwargs)
                cleanup.add("captured HTTP server close", server.close)
                return server

            yield raw, start_server


def _is_expected_socket_teardown_error(error: OSError) -> bool:
    return error.errno in {
        errno.EBADF,
        errno.ECONNABORTED,
        errno.ECONNRESET,
        errno.ENOTCONN,
    } or getattr(error, "winerror", None) in {
        10038,  # WSAENOTSOCK: already closed.
        10053,  # WSAECONNABORTED: connection aborted.
        10054,  # WSAECONNRESET: connection reset.
        10057,  # WSAENOTCONN: no longer connected.
    }


def _start_captured_server(
    function: Any,
    config: AppConfig,
    *,
    _startup_timeout: float = 5,
    _after_constructor_abandon_check: Any | None = None,
    _server_type: type[Any] | None = None,
    **kwargs: Any,
) -> _CapturedTestServer:
    module = sys.modules[function.__module__]
    server_type = _server_type or getattr(module, "ThreadingHTTPServer")
    server_modules = (
        module,
        importlib.import_module("gp_control_plane.web.api_server"),
        importlib.import_module("gp_control_plane.web.proxy"),
    )
    startup_abandoned = threading.Event()
    listeners = _CapturedListenerRegistry(startup_abandoned)
    startup_condition = threading.Condition()
    listener_constructed = False
    serving_listener: Any | None = None
    startup_finished = False
    startup_errors: list[BaseException] = []

    class CapturingThreadingHTTPServer(server_type):
        def __init__(self, *args: Any, **server_kwargs: Any) -> None:
            server_address, request_handler_class, *server_args = args
            server_address = (server_address[0], 0)

            class CapturingRequestHandler(request_handler_class):
                def handle_one_request(self) -> None:
                    super().handle_one_request()
                    if getattr(self, "path", "").split("?", 1)[0] == "/api/web/events/stream":
                        self.close_connection = True

            super().__init__(server_address, CapturingRequestHandler, *server_args, **server_kwargs)
            if not listeners.register(self):
                # The registry owns this listener before observing abandonment.
                # A failed early raw close therefore remains available to the
                # post-join drain instead of disappearing from cleanup.
                listeners.close_after_abandonment(self)
                raise RuntimeError("captured server startup was abandoned during listener registration")
            with startup_condition:
                nonlocal listener_constructed
                listener_constructed = True
                startup_condition.notify_all()
            if _after_constructor_abandon_check is not None:
                _after_constructor_abandon_check(self, startup_abandoned)
            self._request_handlers_lock = threading.Lock()
            self._active_request_handler_count = 0
            self._active_request_sockets: set[socket.socket] = set()
            self.request_handlers_idle = threading.Event()
            self.request_handlers_idle.set()
            self.serve_forever_started = threading.Event()

        def serve_forever(self, *args: Any, **server_kwargs: Any) -> None:
            if startup_abandoned.is_set():
                return
            self.serve_forever_started.set()
            with startup_condition:
                nonlocal serving_listener
                serving_listener = self
                startup_condition.notify_all()
            super().serve_forever(*args, **server_kwargs)

        @property
        def active_request_handler_count(self) -> int:
            with self._request_handlers_lock:
                return self._active_request_handler_count

        def process_request(self, request: Any, client_address: Any) -> None:
            with self._request_handlers_lock:
                self._active_request_handler_count += 1
                self._active_request_sockets.add(request)
                self.request_handlers_idle.clear()
            try:
                super().process_request(request, client_address)
            except BaseException:
                # ThreadingMixIn starts the worker in process_request().  If that
                # fails, no worker finally block will release this reservation.
                with self._request_handlers_lock:
                    self._active_request_handler_count -= 1
                    self._active_request_sockets.discard(request)
                    if self._active_request_handler_count == 0:
                        self.request_handlers_idle.set()
                raise

        def process_request_thread(self, request: Any, client_address: Any) -> None:
            try:
                super().process_request_thread(request, client_address)
            finally:
                with self._request_handlers_lock:
                    self._active_request_handler_count -= 1
                    self._active_request_sockets.discard(request)
                    if self._active_request_handler_count == 0:
                        self.request_handlers_idle.set()

        def close_active_request_connections(self) -> None:
            with self._request_handlers_lock:
                active_request_sockets = tuple(self._active_request_sockets)
            cleanup_failures: list[tuple[str, BaseException]] = []

            for index, request in enumerate(active_request_sockets, start=1):
                try:
                    request.shutdown(socket.SHUT_RDWR)
                except OSError as error:
                    if not _is_expected_socket_teardown_error(error):
                        cleanup_failures.extend(
                            _cleanup_failure_records(
                                error,
                                f"captured HTTP active request socket {index} shutdown",
                            )
                        )
                try:
                    request.close()
                except OSError as error:
                    if not _is_expected_socket_teardown_error(error):
                        cleanup_failures.extend(
                            _cleanup_failure_records(
                                error,
                                f"captured HTTP active request socket {index} close",
                            )
                        )
            if cleanup_failures:
                raise _CleanupFailureRecords(cleanup_failures)

    def run() -> None:
        nonlocal startup_finished
        try:
            with contextlib.ExitStack() as patches:
                patched_modules: set[int] = set()
                for server_module in server_modules:
                    if id(server_module) in patched_modules:
                        continue
                    patched_modules.add(id(server_module))
                    patches.enter_context(
                        mock.patch.object(server_module, "ThreadingHTTPServer", CapturingThreadingHTTPServer)
                    )
                function(config, "127.0.0.1", 0, **kwargs)
        except BaseException as error:
            with startup_condition:
                startup_errors.append(error)
                startup_condition.notify_all()
        finally:
            with startup_condition:
                startup_finished = True
                startup_condition.notify_all()

    def abort_startup() -> BaseException | None:
        listeners.abandon()

        def join_startup_thread() -> None:
            thread.join(timeout=5)
            if thread.is_alive():
                raise AssertionError("server startup thread did not stop")

        cleanup = _CleanupActions()
        # LIFO execution produces the required pre-join then post-join sweep.
        cleanup.add(
            "captured server post-join listener close",
            lambda: listeners.close_all("post-join", final=True),
        )
        cleanup.add("captured server startup thread join", join_startup_thread)
        cleanup.add("captured server pre-join listener close", lambda: listeners.close_all("pre-join"))
        try:
            cleanup.run()
        except BaseException as error:
            return error
        return None

    def raise_startup_failure(message: str, cause: BaseException | None = None) -> None:
        cleanup_error = abort_startup()
        if cause is None:
            with startup_condition:
                if startup_errors:
                    cause = startup_errors[0]
        failure = AssertionError(message)
        if cleanup_error is not None:
            for description, error in _cleanup_failure_records(cleanup_error, "captured server startup cleanup"):
                failure.add_note(f"Captured server startup cleanup also failed during {description}: {error!r}")
        if cause is not None:
            raise failure from cause
        raise failure

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    startup_deadline = time.monotonic() + _startup_timeout
    startup_failure: tuple[str, BaseException | None] | None = None
    with startup_condition:
        while not listener_constructed and not startup_errors and not startup_finished:
            remaining = startup_deadline - time.monotonic()
            if remaining <= 0:
                break
            startup_condition.wait(timeout=remaining)
        if startup_errors:
            startup_failure = ("server failed during startup", startup_errors[0])
        elif not listener_constructed:
            startup_failure = ("server did not construct", None)
        while startup_failure is None and serving_listener is None and not startup_errors and not startup_finished:
            remaining = startup_deadline - time.monotonic()
            if remaining <= 0:
                break
            startup_condition.wait(timeout=remaining)
        if startup_failure is None and startup_errors:
            startup_failure = ("server failed during startup", startup_errors[0])
        elif startup_failure is None and serving_listener is None:
            message = "server exited during startup" if startup_finished else "server did not start serving"
            startup_failure = (message, None)

    if startup_failure is not None:
        raise_startup_failure(*startup_failure)

    if serving_listener is None:
        raise_startup_failure("server listener registry was empty")
    port = int(serving_listener.server_address[1])
    return _CapturedTestServer(port, serving_listener, thread, listeners)


def _close_sse_stream(
    connection: http.client.HTTPConnection | None, response: http.client.HTTPResponse | None
) -> None:
    try:
        if connection is not None and connection.sock is not None:
            linger_format = "HH" if os.name == "nt" else "ii"
            connection.sock.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_LINGER,
                struct.pack(linger_format, 1, 0),
            )
        if response is not None:
            response.close()
    finally:
        if connection is not None:
            connection.close()


def _stop_current_run_if_started(port: int, worker_started: threading.Event, worker_finished: threading.Event) -> None:
    if not worker_started.is_set() or worker_finished.is_set():
        return
    status, _headers, body = _http_request(
        port,
        "/api/core/strategy-discovery/stop-current-run",
        method="POST",
        body=b"{}",
        headers={"Content-Type": "application/json"},
    )
    if status != 202:
        raise AssertionError(f"test cleanup could not stop active JobRunner: HTTP {status}: {body!r}")



@contextlib.contextmanager
def _reserved_unavailable_loopback_port() -> Any:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        # Holding a bound-but-not-listening socket guarantees connection refusal
        # while preventing another test or process from claiming this port.
        yield int(sock.getsockname()[1])


def _openapi_test_request(
    openapi_contract: dict[str, Any],
    path: str,
    method: str,
    operation: dict[str, Any],
    snapshot_id: str,
) -> tuple[str, bytes | None, dict[str, str]]:
    request_path = path
    if path == "/api/core/backups/download-archive":
        request_path = f"{path}?snapshot_id={snapshot_id}"
    elif path == "/api/core/clean-install-vaults/create":
        # Do not create a device-local vault from the generic OpenAPI route
        # walker: the route contract is exercised through its documented
        # fail-closed invalid-request response here and targeted vault tests.
        return request_path, b'{"unexpected":true}', {"Content-Type": "application/json"}
    elif path == "/api/core/presets/v2fly/category-domains":
        request_path = f"{path}?category=missing"
    elif path == "/api/core/strategy-candidates":
        request_path = f"{path}?domain=youtube.com"
    elif path == "/api/web/presets/domains":
        request_path = f"{path}?scope=finder&name=required&kind=system&limit=2"

    if method != "POST":
        return request_path, None, {}
    content = ((operation.get("requestBody") or {}).get("content") or {})
    if "application/zip" in content:
        return request_path, b"not-a-zip", {"Content-Type": "application/zip"}
    if "multipart/form-data" in content:
        return request_path, b"not-a-zip", {"Content-Type": "application/zip"}
    json_body = content.get("application/json")
    if not isinstance(json_body, dict):
        return request_path, None, {}
    payload = _openapi_first_example_value(openapi_contract, json_body.get("examples") or {})
    if payload is None:
        payload = {}
    return request_path, json.dumps(payload).encode("utf-8"), {"Content-Type": "application/json"}


def _openapi_first_example_value(openapi_contract: dict[str, Any], examples: dict[str, Any]) -> Any:
    if not examples:
        return None
    example = next(iter(examples.values()))
    if isinstance(example, dict) and "$ref" in example:
        example = _openapi_resolve_ref(openapi_contract, str(example["$ref"]))
    if isinstance(example, dict) and "value" in example:
        return example["value"]
    return None


def _openapi_resolve_ref(openapi_contract: dict[str, Any], ref: str) -> Any:
    if not ref.startswith("#/"):
        raise ValueError(f"unsupported OpenAPI ref: {ref}")
    node: Any = openapi_contract
    for part in ref[2:].split("/"):
        node = node[part]
    return node



def _assert_openapi_response_contract(
    test_case: unittest.TestCase,
    openapi_contract: dict[str, Any],
    operation: dict[str, Any],
    status: int,
    headers: dict[str, str],
    body: bytes,
    *,
    context: object,
) -> None:
    responses = operation.get("responses")
    test_case.assertIsInstance(responses, dict, context)
    response = responses.get(str(status)) or responses.get("default")
    test_case.assertIsNotNone(
        response,
        (*context, f"HTTP {status} has no documented response") if isinstance(context, tuple) else context,
    )
    response = _openapi_resolve_document_node(openapi_contract, response)
    content = response.get("content") or {}
    if not content:
        test_case.assertEqual(body, b"", context)
        return

    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    test_case.assertIn(content_type, content, context)
    if content_type != "application/json":
        return

    media = content[content_type]
    schema = media.get("schema")
    test_case.assertIsInstance(schema, dict, context)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        test_case.fail(f"{context}: documented JSON response is not valid JSON: {error}")
    _assert_openapi_schema(test_case, openapi_contract, schema, payload, path="$")


def _openapi_resolve_document_node(openapi_contract: dict[str, Any], node: Any) -> Any:
    while isinstance(node, dict) and "$ref" in node:
        resolved = _openapi_resolve_ref(openapi_contract, str(node["$ref"]))
        if len(node) == 1:
            node = resolved
        else:
            node = {**resolved, **{key: value for key, value in node.items() if key != "$ref"}}
    return node


def _assert_openapi_schema(
    test_case: unittest.TestCase,
    openapi_contract: dict[str, Any],
    schema: Any,
    value: Any,
    *,
    path: str,
) -> None:
    schema = _openapi_resolve_document_node(openapi_contract, schema)
    if schema is True:
        return
    test_case.assertIsInstance(schema, dict, f"{path}: invalid OpenAPI schema")
    if schema is False:
        test_case.fail(f"{path}: value is forbidden by the documented schema")

    for keyword in ("allOf", "anyOf", "oneOf"):
        if keyword not in schema:
            continue
        choices = schema[keyword]
        test_case.assertIsInstance(choices, list, f"{path}: {keyword} must be an array")
        if keyword == "allOf":
            for index, choice in enumerate(choices):
                _assert_openapi_schema(test_case, openapi_contract, choice, value, path=f"{path}.allOf[{index}]")
            continue
        matches = 0
        failures: list[str] = []
        for index, choice in enumerate(choices):
            try:
                _assert_openapi_schema(test_case, openapi_contract, choice, value, path=f"{path}.{keyword}[{index}]")
            except AssertionError as error:
                failures.append(str(error))
            else:
                matches += 1
        if keyword == "anyOf":
            test_case.assertGreater(matches, 0, f"{path}: no anyOf schema matched: {failures}")
        else:
            test_case.assertEqual(matches, 1, f"{path}: expected exactly one oneOf match, got {matches}: {failures}")

    if "const" in schema:
        test_case.assertEqual(value, schema["const"], f"{path}: const mismatch")
    if "enum" in schema:
        test_case.assertIn(value, schema["enum"], f"{path}: value is not in enum")

    schema_type = schema.get("type")
    if schema_type is not None:
        expected_types = schema_type if isinstance(schema_type, list) else [schema_type]
        test_case.assertTrue(
            any(_openapi_value_matches_type(value, expected_type) for expected_type in expected_types),
            f"{path}: expected {expected_types}, got {type(value).__name__}",
        )

    if isinstance(value, str):
        if "minLength" in schema:
            test_case.assertGreaterEqual(len(value), schema["minLength"], f"{path}: shorter than minLength")
        if "maxLength" in schema:
            test_case.assertLessEqual(len(value), schema["maxLength"], f"{path}: longer than maxLength")
        if "pattern" in schema:
            test_case.assertIsNotNone(re.search(schema["pattern"], value), f"{path}: pattern mismatch")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema:
            test_case.assertGreaterEqual(value, schema["minimum"], f"{path}: below minimum")
        if "maximum" in schema:
            test_case.assertLessEqual(value, schema["maximum"], f"{path}: above maximum")
        if "exclusiveMinimum" in schema:
            test_case.assertGreater(value, schema["exclusiveMinimum"], f"{path}: below exclusiveMinimum")
        if "exclusiveMaximum" in schema:
            test_case.assertLess(value, schema["exclusiveMaximum"], f"{path}: above exclusiveMaximum")

    if isinstance(value, list):
        if "minItems" in schema:
            test_case.assertGreaterEqual(len(value), schema["minItems"], f"{path}: fewer than minItems")
        if "maxItems" in schema:
            test_case.assertLessEqual(len(value), schema["maxItems"], f"{path}: more than maxItems")
        if schema.get("uniqueItems"):
            test_case.assertEqual(len(value), len({json.dumps(item, sort_keys=True) for item in value}), f"{path}: items are not unique")
        if "items" in schema:
            for index, item in enumerate(value):
                _assert_openapi_schema(test_case, openapi_contract, schema["items"], item, path=f"{path}[{index}]")

    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        required = schema.get("required") or []
        for name in required:
            test_case.assertIn(name, value, f"{path}: missing required property {name!r}")
        for name, item in value.items():
            if name in properties:
                _assert_openapi_schema(test_case, openapi_contract, properties[name], item, path=f"{path}.{name}")
                continue
            additional = schema.get("additionalProperties", True)
            if additional is False:
                test_case.fail(f"{path}: unexpected property {name!r}")
            if isinstance(additional, dict):
                _assert_openapi_schema(test_case, openapi_contract, additional, item, path=f"{path}.{name}")


def _openapi_value_matches_type(value: Any, expected_type: str) -> bool:
    return {
        "null": value is None,
        "boolean": isinstance(value, bool),
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
    }.get(expected_type, False)


def _http_request(
    port: int,
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 5,
    authenticated: bool = True,
) -> tuple[int, dict[str, str], bytes]:
    request_headers = dict(headers or {})
    if authenticated and _requires_bearer_token(path):
        request_headers = _authenticated_headers(port, request_headers)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        response_body = response.read()
        return response.status, response_headers, response_body
    finally:
        connection.close()

def _http_sse_first_event(
    port: int,
    path: str,
    *,
    timeout: float = 5,
) -> tuple[int, dict[str, str], str, str]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(
            "GET",
            path,
            headers={"Accept": "text/event-stream", "Authorization": _bearer_authorization(port, timeout=timeout)},
        )
        response = connection.getresponse()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        first_line = response.readline().decode("utf-8").strip()
        second_line = response.readline().decode("utf-8").strip()
        return response.status, response_headers, first_line, second_line
    finally:
        connection.close()


def _requires_bearer_token(path: str) -> bool:
    return path.split("?", 1)[0] not in {"/", "/swagger", "/swagger/", "/openapi.json", "/api/health", "/api/auth/login"}


_BEARER_AUTHORIZATION_BY_PORT: dict[int, str] = {}


def _authenticated_headers(port: int, headers: dict[str, str] | None = None) -> dict[str, str]:
    request_headers = dict(headers or {})
    if "Authorization" not in request_headers:
        request_headers["Authorization"] = _bearer_authorization(port)
    return request_headers


def _bearer_authorization_for_state(state_dir: Path) -> str:
    from gp_control_plane.auth import login

    token = login(state_dir, {"username": "admin", "password": "admin"})["access_token"]
    return f"Bearer {token}"


def _bearer_authorization(port: int, *, timeout: float = 5) -> str:
    if token := _BEARER_AUTHORIZATION_BY_PORT.get(port):
        return token
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(
            "POST",
            "/api/auth/login",
            body=b'{"username":"admin","password":"admin"}',
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        body = response.read()
    finally:
        connection.close()
    if response.status != 200:
        raise AssertionError(f"test login failed with HTTP {response.status}: {body.decode('utf-8', errors='replace')}")
    authorization = f"Bearer {json.loads(body)['access_token']}"
    _BEARER_AUTHORIZATION_BY_PORT[port] = authorization
    return authorization


if __name__ == "__main__":
    unittest.main()
