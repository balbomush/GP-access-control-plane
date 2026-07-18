from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.storage import (
    SCHEMA_MIGRATIONS,
    append_run,
    connect,
    delete_custom_preset,
    delete_user_presets,
    db_path,
    get_meta,
    read_custom_preset_index,
    read_custom_presets,
    read_preset_domains_page,
    read_run_payloads,
    read_system_preset_index,
    read_system_presets,
    save_custom_presets,
    save_system_preset,
    set_preset_domain_enabled,
    storage_status,
)


class StorageTests(unittest.TestCase):
    def test_heavy_query_indexes_exist(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)

            with connect(state_dir) as conn:
                rows = conn.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'index' AND name NOT LIKE 'sqlite_autoindex%'
                    """
                ).fetchall()

            index_names = {str(row["name"]) for row in rows}
            self.assertTrue(
                {
                    "idx_runs_seq",
                    "idx_runs_id_seq",
                    "idx_domains_name",
                    "idx_strategy_domain_results_domain_protocol",
                    "idx_strategy_domain_results_domain_strategy",
                    "idx_strategy_domain_results_strategy_domain",
                    "idx_preset_domains_domain",
                    "idx_preset_domains_preset_enabled_position",
                    "idx_preset_domains_preset_position",
                }.issubset(index_names)
            )

    def test_schema_migration_history_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)

            with connect(state_dir) as conn:
                rows = conn.execute("SELECT version, name FROM schema_migrations ORDER BY version").fetchall()

            self.assertEqual([(int(row["version"]), str(row["name"])) for row in rows], list(SCHEMA_MIGRATIONS))
            status = storage_status(state_dir)
            self.assertEqual([item["version"] for item in status["migrations"]], [item[0] for item in SCHEMA_MIGRATIONS])

    def test_append_run_stores_compact_payload(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            heavy_run = {
                "id": "run-heavy",
                "kind": "multi-domain-discovery",
                "status": "success",
                "timestamp": "2026-07-01T00:00:00Z",
                "started_at": "2026-07-01T00:00:00Z",
                "completed_at": "2026-07-01T00:01:00Z",
                "domains": ["youtube.com"],
                "candidate_count": 1,
                "progress": {"elapsed_seconds": 60},
                "candidates": [{"protocol": "tls", "args": "--heavy"}],
                "summary": {"items": ["x"] * 1000},
            }

            append_run(state_dir, heavy_run)

            with connect(state_dir) as conn:
                raw = str(conn.execute("SELECT payload_json FROM runs").fetchone()["payload_json"])
            stored = json.loads(raw)
            read_back = read_run_payloads(state_dir, limit=10)[0]

            self.assertEqual(stored["id"], "run-heavy")
            self.assertEqual(stored["candidate_count"], 1)
            self.assertEqual(stored["progress"]["elapsed_seconds"], 60)
            self.assertNotIn("candidates", stored)
            self.assertNotIn("summary", stored)
            self.assertNotIn("candidates", read_back)

    def test_append_run_keeps_compact_domain_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            append_run(
                state_dir,
                {
                    "id": "run-diagnostics",
                    "kind": "standard-discovery",
                    "status": "stopped",
                    "timestamp": "2026-07-01T00:00:00Z",
                    "domains": ["youtube.com"],
                    "domain_skipped": [
                        {
                            "raw": "*.example.com",
                            "status": "wildcard",
                            "label": "некорректная строка домена",
                        }
                    ],
                    "domain_diagnostics": [
                        {
                            "domain": "googlevideo.com",
                            "status": "tls_sni_problem",
                            "label": "TLS/SNI проблема",
                            "codes": {"60": 1},
                        }
                    ],
                },
            )

            stored = read_run_payloads(state_dir, limit=10)[0]

            self.assertEqual(stored["domain_skipped"][0]["raw"], "*.example.com")
            self.assertEqual(stored["domain_diagnostics"][0]["status"], "tls_sni_problem")

    def test_migration_compacts_runtime_payloads_and_legacy_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            path = db_path(state_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path)
            try:
                conn.executescript(
                    """
                    CREATE TABLE meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    INSERT INTO meta(key, value) VALUES('schema_version', '6');
                    CREATE TABLE runs (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL,
                        kind TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT '',
                        timestamp TEXT NOT NULL DEFAULT '',
                        payload_json TEXT NOT NULL
                    );
                    """
                )
                heavy_run = {
                    "id": "old-run",
                    "kind": "multi-domain-discovery",
                    "status": "success",
                    "timestamp": "2026-07-01T00:00:00Z",
                    "domains": ["youtube.com"],
                    "candidate_count": 2,
                    "candidates": [{"args": "--a"}, {"args": "--b"}],
                    "common_candidates": [{"args": "--a"}],
                    "summary": {"items": list(range(100))},
                }
                conn.execute(
                    "INSERT INTO runs(id, kind, status, timestamp, payload_json) VALUES(?, ?, ?, ?, ?)",
                    (
                        "old-run",
                        "multi-domain-discovery",
                        "success",
                        "2026-07-01T00:00:00Z",
                        json.dumps(heavy_run, separators=(",", ":")),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
            for name in ("available.ndjson", "runs.jsonl", "candidates.json"):
                (state_dir / "strategy-finder" / name).write_text("legacy\n", encoding="utf-8")
            (state_dir / "strategy-finder" / "jobs.jsonl").write_text(
                json.dumps({"id": "job", "result": heavy_run}, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            (state_dir / "jobs.jsonl").write_text(
                json.dumps({"id": "root-job", "result": heavy_run}, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )

            with connect(state_dir) as conn:
                raw = str(conn.execute("SELECT payload_json FROM runs WHERE id = 'old-run'").fetchone()["payload_json"])
                self.assertEqual(get_meta(conn, "schema_version"), "10")
                self.assertEqual(get_meta(conn, "run_payloads_compacted_v7"), "1")

            stored = json.loads(raw)
            job = json.loads((state_dir / "strategy-finder" / "jobs.jsonl").read_text(encoding="utf-8"))
            root_job = json.loads((state_dir / "jobs.jsonl").read_text(encoding="utf-8"))

            self.assertEqual(stored["candidate_count"], 2)
            self.assertNotIn("candidates", stored)
            self.assertNotIn("common_candidates", stored)
            self.assertNotIn("summary", stored)
            self.assertNotIn("candidates", job["result"])
            self.assertNotIn("candidates", root_job["result"])
            self.assertFalse((state_dir / "strategy-finder" / "available.ndjson").exists())
            self.assertFalse((state_dir / "strategy-finder" / "runs.jsonl").exists())
            self.assertFalse((state_dir / "strategy-finder" / "candidates.json").exists())

    def test_migration_removes_strategy_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            path = db_path(state_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path)
            try:
                conn.executescript(
                    """
                    CREATE TABLE meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    INSERT INTO meta(key, value) VALUES('schema_version', '7');
                    CREATE TABLE strategy_attempts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL DEFAULT '',
                        strategy_id TEXT NOT NULL,
                        domain_id INTEGER NOT NULL,
                        protocol TEXT NOT NULL DEFAULT '',
                        source_mode TEXT NOT NULL DEFAULT 'single_domain',
                        test_name TEXT NOT NULL DEFAULT '',
                        ip_version TEXT NOT NULL DEFAULT '',
                        result TEXT NOT NULL DEFAULT '',
                        error_code TEXT NOT NULL DEFAULT '',
                        duration_ms INTEGER NOT NULL DEFAULT 0,
                        checked_at TEXT NOT NULL DEFAULT ''
                    );
                    INSERT INTO strategy_attempts(strategy_id, domain_id, result) VALUES('s1', 1, 'success');
                    """
                )
                conn.commit()
            finally:
                conn.close()

            with connect(state_dir) as conn:
                schema = get_meta(conn, "schema_version")
                removed_count = get_meta(conn, "strategy_attempts_removed_count")
                has_attempts_table = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM sqlite_master
                    WHERE type = 'table' AND name = 'strategy_attempts'
                    """
                ).fetchone()[0]

            self.assertEqual(schema, "10")
            self.assertEqual(int(has_attempts_table), 0)
            self.assertEqual(removed_count, "1")

    def test_migration_resumes_run_payload_compaction_from_progress_marker(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            path = db_path(state_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            first_compact = {
                "id": "old-run-1",
                "kind": "multi-domain-discovery",
                "status": "success",
                "timestamp": "2026-07-01T00:00:00Z",
                "candidate_count": 1,
            }
            second_heavy = {
                "id": "old-run-2",
                "kind": "multi-domain-discovery",
                "status": "success",
                "timestamp": "2026-07-01T00:01:00Z",
                "candidate_count": 2,
                "candidates": [{"args": "--a"}, {"args": "--b"}],
                "summary": {"items": list(range(100))},
            }
            first_payload = json.dumps(first_compact, separators=(",", ":"), sort_keys=True)
            conn = sqlite3.connect(path)
            try:
                conn.executescript(
                    """
                    CREATE TABLE meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    INSERT INTO meta(key, value) VALUES('schema_version', '6');
                    CREATE TABLE runs (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL,
                        kind TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT '',
                        timestamp TEXT NOT NULL DEFAULT '',
                        payload_json TEXT NOT NULL
                    );
                    """
                )
                conn.execute(
                    "INSERT INTO runs(id, kind, status, timestamp, payload_json) VALUES(?, ?, ?, ?, ?)",
                    ("old-run-1", "multi-domain-discovery", "success", "2026-07-01T00:00:00Z", first_payload),
                )
                conn.execute(
                    "INSERT INTO runs(id, kind, status, timestamp, payload_json) VALUES(?, ?, ?, ?, ?)",
                    (
                        "old-run-2",
                        "multi-domain-discovery",
                        "success",
                        "2026-07-01T00:01:00Z",
                        json.dumps(second_heavy, separators=(",", ":")),
                    ),
                )
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('run_payloads_compaction_started_v7', '1')"
                )
                conn.execute("INSERT INTO meta(key, value) VALUES('run_payloads_compaction_last_seq_v7', '1')")
                conn.execute("INSERT INTO meta(key, value) VALUES('run_payloads_compacted_count', '1')")
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('run_payloads_original_bytes', ?)",
                    (str(len(first_payload.encode("utf-8"))),),
                )
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('run_payloads_compact_bytes', ?)",
                    (str(len(first_payload.encode("utf-8"))),),
                )
                conn.commit()
            finally:
                conn.close()

            with connect(state_dir) as conn:
                rows = conn.execute("SELECT id, payload_json FROM runs ORDER BY seq").fetchall()
                meta_done = get_meta(conn, "run_payloads_compacted_v7")
                meta_last_seq = get_meta(conn, "run_payloads_compaction_last_seq_v7")
                meta_count = get_meta(conn, "run_payloads_compacted_count")

            first = json.loads(str(rows[0]["payload_json"]))
            second = json.loads(str(rows[1]["payload_json"]))
            self.assertEqual(first, first_compact)
            self.assertNotIn("candidates", second)
            self.assertNotIn("summary", second)
            self.assertEqual(meta_done, "1")
            self.assertEqual(meta_last_seq, "2")
            self.assertEqual(meta_count, "2")

    def test_legacy_storage_drop_recovers_from_started_marker(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            path = db_path(state_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path)
            try:
                conn.executescript(
                    """
                    CREATE TABLE meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    INSERT INTO meta(key, value) VALUES('schema_version', '4');
                    INSERT INTO meta(key, value) VALUES('legacy_storage_removed_started_v9', '1');
                    CREATE TABLE candidates(id TEXT PRIMARY KEY);
                    CREATE TABLE candidate_domains(candidate_id TEXT, domain TEXT);
                    CREATE TABLE candidate_common_domains(candidate_id TEXT, domain TEXT);
                    CREATE TABLE candidate_seen_events(id INTEGER PRIMARY KEY);
                    CREATE TABLE presets(scope TEXT, name TEXT);
                    INSERT INTO candidates(id) VALUES('c1');
                    """
                )
                conn.commit()
            finally:
                conn.close()

            with connect(state_dir) as conn:
                table_names = {
                    str(row["name"])
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
                }
                done = get_meta(conn, "legacy_storage_removed_v9")
                removed = get_meta(conn, "legacy_storage_removed_tables")

            self.assertFalse(
                {
                    "candidates",
                    "candidate_domains",
                    "candidate_common_domains",
                    "candidate_seen_events",
                    "presets",
                }
                & table_names
            )
            self.assertEqual(done, "1")
            self.assertEqual(removed, "5")

    def test_new_schema_does_not_create_legacy_candidate_tables(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)

            with connect(state_dir) as conn:
                rows = conn.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table'
                    """
                ).fetchall()

            table_names = {str(row["name"]) for row in rows}
            self.assertFalse(
                {
                    "candidates",
                    "candidate_domains",
                    "candidate_common_domains",
                    "candidate_seen_events",
                    "presets",
                }
                & table_names
            )

    def test_new_schema_uses_minimal_strategy_model(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)

            with connect(state_dir) as conn:
                strategy_columns = {
                    str(row["name"]) for row in conn.execute("PRAGMA table_info(strategies)").fetchall()
                }
                domain_columns = {
                    str(row["name"]) for row in conn.execute("PRAGMA table_info(domains)").fetchall()
                }
                link_columns = {
                    str(row["name"])
                    for row in conn.execute("PRAGMA table_info(strategy_domain_results)").fetchall()
                }
                preset_columns = {
                    str(row["name"]) for row in conn.execute("PRAGMA table_info(domain_presets)").fetchall()
                }
                table_names = {
                    str(row["name"])
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
                }

            self.assertEqual(
                strategy_columns,
                {
                    "id",
                    "protocol",
                    "args",
                    "args_hash",
                    "status",
                    "fragmentation_class",
                    "fragmentation_safe",
                    "fragmentation_reason",
                    "family",
                    "family_key",
                    "family_rank",
                    "family_reason",
                },
            )
            self.assertEqual(domain_columns, {"id", "name", "service_group"})
            self.assertEqual(link_columns, {"strategy_id", "domain_id", "protocol", "source_mode"})
            self.assertEqual(preset_columns, {"id", "scope", "name", "kind", "label", "source_json"})
            self.assertNotIn("strategy_attempts", table_names)

    def test_concurrent_connects_do_not_race_stats_views(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)

            def read_stats_views(_: int) -> tuple[int, int]:
                with connect(state_dir) as conn:
                    domain_count = conn.execute("SELECT COUNT(*) FROM domain_stats").fetchone()[0]
                    strategy_count = conn.execute("SELECT COUNT(*) FROM strategy_stats").fetchone()[0]
                    return int(domain_count), int(strategy_count)

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(read_stats_views, range(24)))

            self.assertEqual(results, [(0, 0)] * 24)

    def test_migrates_existing_preset_domains_without_enabled_column(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            path = db_path(state_dir)
            conn = sqlite3.connect(path)
            try:
                conn.executescript(
                    """
                    CREATE TABLE meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    INSERT INTO meta(key, value) VALUES('schema_version', '5');
                    CREATE TABLE domain_presets (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        scope TEXT NOT NULL DEFAULT '',
                        name TEXT NOT NULL DEFAULT '',
                        kind TEXT NOT NULL DEFAULT 'user',
                        label TEXT NOT NULL DEFAULT '',
                        source_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL DEFAULT '',
                        updated_at TEXT NOT NULL DEFAULT '',
                        UNIQUE(scope, name, kind)
                    );
                    CREATE TABLE domains (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        service_group TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL DEFAULT '',
                        updated_at TEXT NOT NULL DEFAULT ''
                    );
                    CREATE TABLE preset_domains (
                        preset_id INTEGER NOT NULL,
                        domain_id INTEGER NOT NULL,
                        position INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY(preset_id, domain_id)
                    );
                    """
                )
                conn.commit()
            finally:
                conn.close()

            with connect(state_dir) as conn:
                columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(preset_domains)").fetchall()}
                indexes = {
                    str(row["name"])
                    for row in conn.execute(
                        """
                        SELECT name
                        FROM sqlite_master
                        WHERE type = 'index' AND name NOT LIKE 'sqlite_autoindex%'
                        """
                    ).fetchall()
                }

            self.assertIn("enabled", columns)
            self.assertIn("idx_preset_domains_preset_enabled_position", indexes)

    def test_repairs_foreign_keys_left_by_table_rename_migration(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            path = db_path(state_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path)
            try:
                conn.executescript(
                    """
                    CREATE TABLE meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    INSERT INTO meta(key, value) VALUES('schema_version', '9');
                    CREATE TABLE domains (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        service_group TEXT NOT NULL DEFAULT ''
                    );
                    CREATE TABLE strategies (
                        id TEXT PRIMARY KEY,
                        protocol TEXT NOT NULL DEFAULT '',
                        args TEXT NOT NULL DEFAULT '',
                        args_hash TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'candidate'
                    );
                    CREATE TABLE domain_presets (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        scope TEXT NOT NULL DEFAULT '',
                        name TEXT NOT NULL DEFAULT '',
                        kind TEXT NOT NULL DEFAULT 'user',
                        label TEXT NOT NULL DEFAULT '',
                        source_json TEXT NOT NULL DEFAULT '{}',
                        UNIQUE(scope, name, kind)
                    );
                    CREATE TABLE strategy_domain_results (
                        strategy_id TEXT NOT NULL,
                        domain_id INTEGER NOT NULL,
                        protocol TEXT NOT NULL DEFAULT '',
                        source_mode TEXT NOT NULL DEFAULT 'single_domain',
                        PRIMARY KEY(strategy_id, domain_id, source_mode),
                        FOREIGN KEY(strategy_id) REFERENCES strategies_old(id) ON DELETE CASCADE,
                        FOREIGN KEY(domain_id) REFERENCES domains_old(id) ON DELETE CASCADE
                    );
                    CREATE TABLE preset_domains (
                        preset_id INTEGER NOT NULL,
                        domain_id INTEGER NOT NULL,
                        position INTEGER NOT NULL DEFAULT 0,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        PRIMARY KEY(preset_id, domain_id),
                        FOREIGN KEY(preset_id) REFERENCES domain_presets_old(id) ON DELETE CASCADE,
                        FOREIGN KEY(domain_id) REFERENCES domains_old(id) ON DELETE CASCADE
                    );
                    INSERT INTO domains(id, name) VALUES(1, 'youtube.com');
                    INSERT INTO strategies(id, protocol, args, args_hash) VALUES('s1', 'tls', '--a', 'hash-a');
                    INSERT INTO domain_presets(id, scope, name, kind, label) VALUES(1, 'finder', 'mine', 'user', 'mine');
                    INSERT INTO strategy_domain_results(strategy_id, domain_id, protocol, source_mode)
                    VALUES('s1', 1, 'tls', 'single_domain');
                    INSERT INTO preset_domains(preset_id, domain_id, position, enabled)
                    VALUES(1, 1, 0, 1);
                    """
                )
                conn.commit()
            finally:
                conn.close()

            with connect(state_dir) as conn:
                strategy_refs = {
                    str(row["table"]) for row in conn.execute("PRAGMA foreign_key_list(strategy_domain_results)")
                }
                preset_refs = {str(row["table"]) for row in conn.execute("PRAGMA foreign_key_list(preset_domains)")}
                repair_marker = get_meta(conn, "renamed_foreign_key_targets_repaired_v9")

            save_custom_presets(
                state_dir,
                {"finder": {"v2fly-smoke": ["codex-smoke.example"]}, "common": {}},
                "2026-07-01T00:00:00Z",
            )
            saved = read_custom_presets(state_dir)

            self.assertEqual(strategy_refs, {"strategies", "domains"})
            self.assertEqual(preset_refs, {"domain_presets", "domains"})
            self.assertEqual(repair_marker, "1")
            self.assertEqual(saved["finder"]["v2fly-smoke"], ["codex-smoke.example"])

    def test_preset_domain_page_supports_search_and_pagination(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            save_custom_presets(
                state_dir,
                {"finder": {"big": ["youtube.com", "youtu.be", "discord.com"]}, "common": {}},
                "2026-07-01T00:00:00Z",
            )

            page = read_preset_domains_page(state_dir, scope="finder", name="big", limit=2)
            second = read_preset_domains_page(state_dir, scope="finder", name="big", limit=2, offset=2)
            search = read_preset_domains_page(state_dir, scope="finder", name="big", query="youtu", limit=10)

            self.assertEqual(page["total"], 3)
            self.assertTrue(page["has_more"])
            self.assertEqual([item["domain"] for item in page["domains"]], ["youtube.com", "youtu.be"])
            self.assertFalse(second["has_more"])
            self.assertEqual([item["domain"] for item in second["domains"]], ["discord.com"])
            self.assertEqual([item["domain"] for item in search["domains"]], ["youtube.com", "youtu.be"])

    def test_disabled_preset_domain_is_omitted_from_active_presets(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            save_custom_presets(
                state_dir,
                {"finder": {"mine": ["youtube.com", "discord.com"]}, "common": {}},
                "2026-07-01T00:00:00Z",
            )

            result = set_preset_domain_enabled(
                state_dir,
                scope="finder",
                name="mine",
                domain="discord.com",
                enabled=False,
                updated_at="2026-07-01T01:00:00Z",
            )
            active = read_custom_presets(state_dir)
            page = read_preset_domains_page(state_dir, scope="finder", name="mine", include_disabled=True)
            enabled_only = read_preset_domains_page(state_dir, scope="finder", name="mine", include_disabled=False)

            self.assertFalse(result["enabled"])
            self.assertEqual(active["finder"]["mine"], ["youtube.com"])
            self.assertEqual(
                [(item["domain"], item["enabled"]) for item in page["domains"]],
                [("youtube.com", True), ("discord.com", False)],
            )
            self.assertEqual([item["domain"] for item in enabled_only["domains"]], ["youtube.com"])

    def test_custom_preset_index_reports_enabled_and_total_counts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            save_custom_presets(
                state_dir,
                {"finder": {"mine": ["youtube.com", "discord.com"]}, "common": {}},
                "2026-07-01T00:00:00Z",
            )
            set_preset_domain_enabled(
                state_dir,
                scope="finder",
                name="mine",
                domain="discord.com",
                enabled=False,
                updated_at="2026-07-01T01:00:00Z",
            )

            index = read_custom_preset_index(state_dir)

            self.assertEqual(index["finder"]["mine"]["enabled_count"], 1)
            self.assertEqual(index["finder"]["mine"]["total_count"], 2)

    def test_system_domain_presets_are_created_with_required_and_desired_lists(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)

            presets = read_system_presets(state_dir)
            index = read_system_preset_index(state_dir)

            self.assertIn("required", presets["finder"])
            self.assertIn("desired", presets["finder"])
            self.assertEqual(presets["finder"]["required"], [])
            self.assertEqual(presets["finder"]["desired"], [])
            self.assertEqual(index["finder"]["required"]["kind"], "system")
            self.assertEqual(index["finder"]["required"]["enabled_count"], 0)
            self.assertEqual(index["finder"]["desired"]["enabled_count"], 0)

    def test_system_domain_preset_can_be_saved_as_empty_list(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)

            save_system_preset(
                state_dir,
                scope="finder",
                name="required",
                domains=[],
                updated_at="2026-07-18T00:00:00Z",
            )
            page = read_preset_domains_page(state_dir, scope="finder", name="required", kind="system")
            index = read_system_preset_index(state_dir)

            self.assertEqual(read_system_presets(state_dir)["finder"]["required"], [])
            self.assertEqual(page["total"], 0)
            self.assertEqual(index["finder"]["required"]["total_count"], 0)

    def test_delete_user_presets_does_not_remove_system_list(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            save_custom_presets(
                state_dir,
                {"finder": {"mine": ["youtube.com"], "required": ["example.com"]}, "common": {}},
                "2026-07-18T00:00:00Z",
            )

            index = delete_user_presets(state_dir, scope="finder", names=["mine", "required"])

            self.assertNotIn("mine", index["finder"])
            self.assertIn("required", read_system_preset_index(state_dir)["finder"])
            self.assertEqual(read_system_presets(state_dir)["finder"]["required"], [])

    def test_delete_custom_preset_removes_one_scope_entry(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            save_custom_presets(
                state_dir,
                {"finder": {"mine": ["youtube.com"]}, "common": {"mine": ["discord.com"]}},
                "2026-07-01T00:00:00Z",
            )

            index = delete_custom_preset(state_dir, scope="finder", name="mine")

            self.assertNotIn("mine", index["finder"])
            self.assertIn("mine", index["common"])
            self.assertEqual(read_custom_presets(state_dir)["common"]["mine"], ["discord.com"])


if __name__ == "__main__":
    unittest.main()
