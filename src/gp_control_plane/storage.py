from __future__ import annotations

import json
import sqlite3
import hashlib
import threading
from pathlib import Path
from typing import Any

from .strategy_safety import analyze_strategy

SCHEMA_VERSION = 10
SCHEMA_MIGRATIONS = (
    (1, "base_candidate_storage"),
    (2, "normalized_domain_strategy_model"),
    (3, "minimal_backup_model"),
    (4, "runtime_observability"),
    (5, "remove_legacy_candidate_storage"),
    (6, "preset_domain_state"),
    (7, "compact_runtime_payloads"),
    (8, "trim_strategy_attempt_diagnostics"),
    (9, "minimal_sqlite_working_model"),
    (10, "strategy_analysis_metadata"),
)
_MIGRATION_LOCK = threading.Lock()
_MIGRATED_DB_PATHS: set[Path] = set()
_OMITTED = object()
_RUN_PAYLOAD_DROP_KEYS = {
    "summary",
    "common",
    "live_summary",
    "results",
    "common_results",
    "direct_available",
    "not_working",
    "candidates",
    "common_candidates",
    "attempts",
    "attempt_results",
    "candidate_events",
    "candidate_samples",
    "common_candidate_samples",
}
_RUN_PAYLOAD_STRUCTURED_LIST_KEYS = {"domains"}
_RUN_PAYLOAD_COMPACT_OBJECT_LIST_KEYS = {
    "domain_skipped",
    "domain_classification",
    "domain_diagnostics",
    "curl_diagnostics",
}
_RUN_PAYLOAD_MAX_SCALAR_LIST = 500
_RUN_PAYLOAD_MAX_OBJECT_LIST = 100
_RUN_PAYLOAD_MAX_STRING = 8192
_RUN_PAYLOAD_COMPACT_BATCH_SIZE = 100
_LEGACY_RUNTIME_FILES = ("available.ndjson", "runs.jsonl", "candidates.json")
_LEGACY_STORAGE_TABLES = (
    "candidate_seen_events",
    "candidate_common_domains",
    "candidate_domains",
    "candidates",
    "presets",
)


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


def db_path(state_dir: Path) -> Path:
    root = state_dir / "strategy-finder"
    root.mkdir(parents=True, exist_ok=True)
    return root / "state.sqlite3"


def connect(state_dir: Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    path = db_path(state_dir)
    conn = sqlite3.connect(path, timeout=30, factory=ClosingConnection, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    migration_key = path.resolve()
    with _MIGRATION_LOCK:
        if migration_key not in _MIGRATED_DB_PATHS:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            _migrate_schema(conn)
            _cleanup_runtime_state(conn, path.parent)
            _run_deferred_vacuum(conn, state_dir)
            _MIGRATED_DB_PATHS.add(migration_key)
            return conn
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
    return conn


def storage_status(state_dir: Path) -> dict[str, Any]:
    path = db_path(state_dir)
    with connect(state_dir) as conn:
        meta = {
            str(row["key"] or ""): str(row["value"] or "")
            for row in conn.execute("SELECT key, value FROM meta ORDER BY key").fetchall()
        }
        counts = {table: _table_count(conn, table) for table in _STORAGE_STATUS_TABLES}
        view_counts = {view: _table_count(conn, view) for view in _STORAGE_STATUS_VIEWS}
        migrations = [
            {"version": int(row["version"]), "name": str(row["name"]), "applied_at": str(row["applied_at"])}
            for row in conn.execute("SELECT version, name, applied_at FROM schema_migrations ORDER BY version").fetchall()
        ]
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    return {
        "db_path": str(path),
        "schema_version": meta.get("schema_version", ""),
        "expected_schema_version": str(SCHEMA_VERSION),
        "integrity_check": integrity,
        "db_size_bytes": _file_size(path),
        "wal_size_bytes": _file_size(path.with_name(f"{path.name}-wal")),
        "shm_size_bytes": _file_size(path.with_name(f"{path.name}-shm")),
        "tables": counts,
        "views": view_counts,
        "meta": meta,
        "migrations": migrations,
    }


_STORAGE_STATUS_TABLES = (
    "runs",
    "domains",
    "strategies",
    "strategy_domain_results",
    "domain_presets",
    "preset_domains",
)

_STORAGE_STATUS_VIEWS = ("domain_stats", "strategy_stats")


def _table_count(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS count FROM {name}").fetchone()
    return int(row["count"]) if row else 0


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _migrate_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS runs (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            timestamp TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_runs_id_seq ON runs(id, seq);
        CREATE INDEX IF NOT EXISTS idx_runs_seq ON runs(seq);

        CREATE TABLE IF NOT EXISTS domains (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            service_group TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_domains_name ON domains(name);
        CREATE INDEX IF NOT EXISTS idx_domains_service_group ON domains(service_group);

        CREATE TABLE IF NOT EXISTS strategies (
            id TEXT PRIMARY KEY,
            protocol TEXT NOT NULL DEFAULT '',
            args TEXT NOT NULL DEFAULT '',
            args_hash TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'candidate',
            fragmentation_class TEXT NOT NULL DEFAULT 'unknown',
            fragmentation_safe INTEGER NOT NULL DEFAULT 0,
            fragmentation_reason TEXT NOT NULL DEFAULT '',
            family TEXT NOT NULL DEFAULT 'other',
            family_key TEXT NOT NULL DEFAULT '',
            family_rank INTEGER NOT NULL DEFAULT 900,
            family_reason TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_strategies_protocol ON strategies(protocol);
        CREATE INDEX IF NOT EXISTS idx_strategies_args_hash ON strategies(args_hash);

        CREATE TABLE IF NOT EXISTS strategy_domain_results (
            strategy_id TEXT NOT NULL,
            domain_id INTEGER NOT NULL,
            protocol TEXT NOT NULL DEFAULT '',
            source_mode TEXT NOT NULL DEFAULT 'single_domain',
            PRIMARY KEY(strategy_id, domain_id, source_mode),
            FOREIGN KEY(strategy_id) REFERENCES strategies(id) ON DELETE CASCADE,
            FOREIGN KEY(domain_id) REFERENCES domains(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_domain_results_domain_protocol ON strategy_domain_results(domain_id, protocol);
        CREATE INDEX IF NOT EXISTS idx_strategy_domain_results_domain_strategy ON strategy_domain_results(domain_id, strategy_id);
        CREATE INDEX IF NOT EXISTS idx_strategy_domain_results_strategy_domain ON strategy_domain_results(strategy_id, domain_id);
        CREATE INDEX IF NOT EXISTS idx_strategy_domain_results_source ON strategy_domain_results(source_mode);

        CREATE TABLE IF NOT EXISTS domain_presets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'user',
            label TEXT NOT NULL DEFAULT '',
            source_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(scope, name, kind)
        );
        CREATE INDEX IF NOT EXISTS idx_domain_presets_scope_name ON domain_presets(scope, name);

        CREATE TABLE IF NOT EXISTS preset_domains (
            preset_id INTEGER NOT NULL,
            domain_id INTEGER NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(preset_id, domain_id),
            FOREIGN KEY(preset_id) REFERENCES domain_presets(id) ON DELETE CASCADE,
            FOREIGN KEY(domain_id) REFERENCES domains(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_preset_domains_domain ON preset_domains(domain_id);

        CREATE VIEW IF NOT EXISTS domain_stats AS
        SELECT d.id AS domain_id,
               d.name AS domain,
               COUNT(DISTINCT r.strategy_id) AS strategy_count,
               COUNT(DISTINCT CASE WHEN r.protocol = 'tls' THEN r.strategy_id END) AS tls_strategy_count,
               COUNT(DISTINCT CASE WHEN r.protocol = 'quic' THEN r.strategy_id END) AS quic_strategy_count
        FROM domains d
        LEFT JOIN strategy_domain_results r ON r.domain_id = d.id
        GROUP BY d.id, d.name;

        CREATE VIEW IF NOT EXISTS strategy_stats AS
        SELECT s.id AS strategy_id,
               s.protocol,
               COUNT(DISTINCT r.domain_id) AS domain_count,
               COUNT(DISTINCT CASE WHEN r.source_mode = 'single_domain' THEN r.domain_id END) AS single_domain_count,
               COUNT(DISTINCT CASE WHEN r.source_mode = 'multi_domain' THEN r.domain_id END) AS multi_domain_count
        FROM strategies s
        LEFT JOIN strategy_domain_results r ON r.strategy_id = s.id
        GROUP BY s.id, s.protocol;
        """
    )
    _ensure_column(conn, "strategies", "fragmentation_class", "TEXT NOT NULL DEFAULT 'unknown'")
    _ensure_column(conn, "strategies", "fragmentation_safe", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "strategies", "fragmentation_reason", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "strategies", "family", "TEXT NOT NULL DEFAULT 'other'")
    _ensure_column(conn, "strategies", "family_key", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "strategies", "family_rank", "INTEGER NOT NULL DEFAULT 900")
    _ensure_column(conn, "strategies", "family_reason", "TEXT NOT NULL DEFAULT ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_strategies_family ON strategies(family, family_rank)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_strategies_fragmentation ON strategies(fragmentation_class)")
    _ensure_column(conn, "domain_presets", "source_json", "TEXT NOT NULL DEFAULT '{}'")
    _ensure_column(conn, "preset_domains", "enabled", "INTEGER NOT NULL DEFAULT 1")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_preset_domains_preset_enabled_position ON preset_domains(preset_id, enabled, position)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_preset_domains_preset_position ON preset_domains(preset_id, position)")
    _migrate_minimal_working_model_schema(conn)
    _recreate_stats_views(conn)
    _backfill_strategy_analysis(conn)
    _drop_legacy_storage(conn)
    _compact_run_payloads(conn)
    _drop_strategy_attempts(conn)
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    _record_schema_migrations(conn)
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else ""


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))


def _record_schema_migrations(conn: sqlite3.Connection) -> None:
    for version, name in SCHEMA_MIGRATIONS:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, name) VALUES(?, ?)",
            (version, name),
        )


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migrate_minimal_working_model_schema(conn: sqlite3.Connection) -> bool:
    changed = False
    conn.executescript("DROP VIEW IF EXISTS domain_stats; DROP VIEW IF EXISTS strategy_stats;")
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("PRAGMA legacy_alter_table=ON")
    try:
        changed = _migrate_domains_schema(conn) or changed
        changed = _migrate_strategies_schema(conn) or changed
        changed = _migrate_strategy_domain_results_schema(conn) or changed
        changed = _migrate_domain_presets_schema(conn) or changed
        changed = _repair_renamed_foreign_key_targets(conn) or changed
    finally:
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute("PRAGMA foreign_keys=ON")
    if changed:
        problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise sqlite3.IntegrityError("foreign key check failed after SQLite model migration")
    return changed


def _migrate_domains_schema(conn: sqlite3.Connection) -> bool:
    columns = _table_columns(conn, "domains")
    if {"created_at", "updated_at"}.isdisjoint(columns):
        return False
    conn.executescript(
        """
        DROP INDEX IF EXISTS idx_domains_name;
        DROP INDEX IF EXISTS idx_domains_service_group;
        ALTER TABLE domains RENAME TO domains_old;
        CREATE TABLE domains (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            service_group TEXT NOT NULL DEFAULT ''
        );
        INSERT OR IGNORE INTO domains(id, name, service_group)
        SELECT id, name, COALESCE(service_group, '')
        FROM domains_old
        WHERE COALESCE(name, '') != '';
        DROP TABLE domains_old;
        CREATE INDEX IF NOT EXISTS idx_domains_name ON domains(name);
        CREATE INDEX IF NOT EXISTS idx_domains_service_group ON domains(service_group);
        """
    )
    set_meta(conn, "minimal_domains_schema_v9", "1")
    return True


def _migrate_strategies_schema(conn: sqlite3.Connection) -> bool:
    columns = _table_columns(conn, "strategies")
    if {"first_seen_at", "last_seen_at"}.isdisjoint(columns):
        return False
    conn.executescript(
        """
        DROP INDEX IF EXISTS idx_strategies_last_seen;
        DROP INDEX IF EXISTS idx_strategies_protocol;
        DROP INDEX IF EXISTS idx_strategies_args_hash;
        ALTER TABLE strategies RENAME TO strategies_old;
        CREATE TABLE strategies (
            id TEXT PRIMARY KEY,
            protocol TEXT NOT NULL DEFAULT '',
            args TEXT NOT NULL DEFAULT '',
            args_hash TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'candidate'
        );
        INSERT OR IGNORE INTO strategies(id, protocol, args, args_hash, status)
        SELECT id, COALESCE(protocol, ''), COALESCE(args, ''), COALESCE(args_hash, ''), COALESCE(status, 'candidate')
        FROM strategies_old
        WHERE COALESCE(id, '') != '';
        DROP TABLE strategies_old;
        CREATE INDEX IF NOT EXISTS idx_strategies_protocol ON strategies(protocol);
        CREATE INDEX IF NOT EXISTS idx_strategies_args_hash ON strategies(args_hash);
        """
    )
    conn.execute(
        """
        UPDATE strategies
        SET args_hash = ?
        WHERE COALESCE(args_hash, '') = '' AND COALESCE(args, '') = ''
        """,
        (_args_hash(""),),
    )
    set_meta(conn, "minimal_strategies_schema_v9", "1")
    return True


def _migrate_strategy_domain_results_schema(conn: sqlite3.Connection) -> bool:
    columns = _table_columns(conn, "strategy_domain_results")
    legacy_columns = {
        "success_count",
        "fail_count",
        "last_success_run_id",
        "last_fail_run_id",
        "first_seen_at",
        "last_seen_at",
    }
    if legacy_columns.isdisjoint(columns):
        return False
    protocol_expr = "COALESCE(protocol, '')" if "protocol" in columns else "''"
    source_mode_expr = (
        "COALESCE(NULLIF(source_mode, ''), 'single_domain')" if "source_mode" in columns else "'single_domain'"
    )
    conn.executescript(
        """
        DROP INDEX IF EXISTS idx_strategy_domain_results_domain_protocol;
        DROP INDEX IF EXISTS idx_strategy_domain_results_domain_strategy;
        DROP INDEX IF EXISTS idx_strategy_domain_results_strategy_domain;
        DROP INDEX IF EXISTS idx_strategy_domain_results_source;
        ALTER TABLE strategy_domain_results RENAME TO strategy_domain_results_old;
        CREATE TABLE strategy_domain_results (
            strategy_id TEXT NOT NULL,
            domain_id INTEGER NOT NULL,
            protocol TEXT NOT NULL DEFAULT '',
            source_mode TEXT NOT NULL DEFAULT 'single_domain',
            PRIMARY KEY(strategy_id, domain_id, source_mode),
            FOREIGN KEY(strategy_id) REFERENCES strategies(id) ON DELETE CASCADE,
            FOREIGN KEY(domain_id) REFERENCES domains(id) ON DELETE CASCADE
        );
        """
    )
    conn.execute(
        f"""
        INSERT OR IGNORE INTO strategy_domain_results(strategy_id, domain_id, protocol, source_mode)
        SELECT strategy_id, domain_id, {protocol_expr}, {source_mode_expr}
        FROM strategy_domain_results_old
        WHERE COALESCE(strategy_id, '') != '' AND domain_id IS NOT NULL
        """
    )
    conn.executescript(
        """
        DROP TABLE strategy_domain_results_old;
        CREATE INDEX IF NOT EXISTS idx_strategy_domain_results_domain_protocol ON strategy_domain_results(domain_id, protocol);
        CREATE INDEX IF NOT EXISTS idx_strategy_domain_results_domain_strategy ON strategy_domain_results(domain_id, strategy_id);
        CREATE INDEX IF NOT EXISTS idx_strategy_domain_results_strategy_domain ON strategy_domain_results(strategy_id, domain_id);
        CREATE INDEX IF NOT EXISTS idx_strategy_domain_results_source ON strategy_domain_results(source_mode);
        """
    )
    set_meta(conn, "minimal_strategy_domain_results_schema_v9", "1")
    return True


def _migrate_domain_presets_schema(conn: sqlite3.Connection) -> bool:
    columns = _table_columns(conn, "domain_presets")
    if {"created_at", "updated_at"}.isdisjoint(columns):
        return False
    conn.executescript(
        """
        DROP INDEX IF EXISTS idx_domain_presets_scope_name;
        ALTER TABLE domain_presets RENAME TO domain_presets_old;
        CREATE TABLE domain_presets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'user',
            label TEXT NOT NULL DEFAULT '',
            source_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(scope, name, kind)
        );
        INSERT OR IGNORE INTO domain_presets(id, scope, name, kind, label, source_json)
        SELECT id, COALESCE(scope, ''), COALESCE(name, ''), COALESCE(kind, 'user'), COALESCE(label, ''), COALESCE(source_json, '{}')
        FROM domain_presets_old
        WHERE COALESCE(scope, '') != '' AND COALESCE(name, '') != '';
        DROP TABLE domain_presets_old;
        CREATE INDEX IF NOT EXISTS idx_domain_presets_scope_name ON domain_presets(scope, name);
        """
    )
    set_meta(conn, "minimal_domain_presets_schema_v9", "1")
    return True


def _repair_renamed_foreign_key_targets(conn: sqlite3.Connection) -> bool:
    changed = False
    strategy_refs = _foreign_key_parent_tables(conn, "strategy_domain_results")
    if {"domains_old", "strategies_old"} & strategy_refs:
        _rebuild_strategy_domain_results(conn)
        changed = True
    preset_refs = _foreign_key_parent_tables(conn, "preset_domains")
    if {"domains_old", "domain_presets_old"} & preset_refs:
        _rebuild_preset_domains(conn)
        changed = True
    if changed:
        set_meta(conn, "renamed_foreign_key_targets_repaired_v9", "1")
    return changed


def _foreign_key_parent_tables(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["table"]) for row in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()}


def _rebuild_strategy_domain_results(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "strategy_domain_results")
    protocol_expr = "COALESCE(old.protocol, '')" if "protocol" in columns else "''"
    source_mode_expr = (
        "COALESCE(NULLIF(old.source_mode, ''), 'single_domain')" if "source_mode" in columns else "'single_domain'"
    )
    conn.executescript(
        """
        DROP INDEX IF EXISTS idx_strategy_domain_results_domain_protocol;
        DROP INDEX IF EXISTS idx_strategy_domain_results_domain_strategy;
        DROP INDEX IF EXISTS idx_strategy_domain_results_strategy_domain;
        DROP INDEX IF EXISTS idx_strategy_domain_results_source;
        ALTER TABLE strategy_domain_results RENAME TO strategy_domain_results_fk_old;
        CREATE TABLE strategy_domain_results (
            strategy_id TEXT NOT NULL,
            domain_id INTEGER NOT NULL,
            protocol TEXT NOT NULL DEFAULT '',
            source_mode TEXT NOT NULL DEFAULT 'single_domain',
            PRIMARY KEY(strategy_id, domain_id, source_mode),
            FOREIGN KEY(strategy_id) REFERENCES strategies(id) ON DELETE CASCADE,
            FOREIGN KEY(domain_id) REFERENCES domains(id) ON DELETE CASCADE
        );
        """
    )
    conn.execute(
        f"""
        INSERT OR IGNORE INTO strategy_domain_results(strategy_id, domain_id, protocol, source_mode)
        SELECT old.strategy_id, old.domain_id, {protocol_expr}, {source_mode_expr}
        FROM strategy_domain_results_fk_old old
        JOIN strategies s ON s.id = old.strategy_id
        JOIN domains d ON d.id = old.domain_id
        WHERE COALESCE(old.strategy_id, '') != '' AND old.domain_id IS NOT NULL
        """
    )
    conn.executescript(
        """
        DROP TABLE strategy_domain_results_fk_old;
        CREATE INDEX IF NOT EXISTS idx_strategy_domain_results_domain_protocol ON strategy_domain_results(domain_id, protocol);
        CREATE INDEX IF NOT EXISTS idx_strategy_domain_results_domain_strategy ON strategy_domain_results(domain_id, strategy_id);
        CREATE INDEX IF NOT EXISTS idx_strategy_domain_results_strategy_domain ON strategy_domain_results(strategy_id, domain_id);
        CREATE INDEX IF NOT EXISTS idx_strategy_domain_results_source ON strategy_domain_results(source_mode);
        """
    )


def _rebuild_preset_domains(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "preset_domains")
    enabled_expr = "COALESCE(old.enabled, 1)" if "enabled" in columns else "1"
    position_expr = "COALESCE(old.position, 0)" if "position" in columns else "0"
    conn.executescript(
        """
        DROP INDEX IF EXISTS idx_preset_domains_domain;
        DROP INDEX IF EXISTS idx_preset_domains_preset_enabled_position;
        DROP INDEX IF EXISTS idx_preset_domains_preset_position;
        ALTER TABLE preset_domains RENAME TO preset_domains_fk_old;
        CREATE TABLE preset_domains (
            preset_id INTEGER NOT NULL,
            domain_id INTEGER NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(preset_id, domain_id),
            FOREIGN KEY(preset_id) REFERENCES domain_presets(id) ON DELETE CASCADE,
            FOREIGN KEY(domain_id) REFERENCES domains(id) ON DELETE CASCADE
        );
        """
    )
    conn.execute(
        f"""
        INSERT OR IGNORE INTO preset_domains(preset_id, domain_id, position, enabled)
        SELECT old.preset_id, old.domain_id, {position_expr}, {enabled_expr}
        FROM preset_domains_fk_old old
        JOIN domain_presets p ON p.id = old.preset_id
        JOIN domains d ON d.id = old.domain_id
        WHERE old.preset_id IS NOT NULL AND old.domain_id IS NOT NULL
        """
    )
    conn.executescript(
        """
        DROP TABLE preset_domains_fk_old;
        CREATE INDEX IF NOT EXISTS idx_preset_domains_domain ON preset_domains(domain_id);
        CREATE INDEX IF NOT EXISTS idx_preset_domains_preset_enabled_position ON preset_domains(preset_id, enabled, position);
        CREATE INDEX IF NOT EXISTS idx_preset_domains_preset_position ON preset_domains(preset_id, position);
        """
    )


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _recreate_stats_views(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP VIEW IF EXISTS domain_stats;
        DROP VIEW IF EXISTS strategy_stats;
        CREATE VIEW IF NOT EXISTS domain_stats AS
        SELECT d.id AS domain_id,
               d.name AS domain,
               COUNT(DISTINCT r.strategy_id) AS strategy_count,
               COUNT(DISTINCT CASE WHEN r.protocol = 'tls' THEN r.strategy_id END) AS tls_strategy_count,
               COUNT(DISTINCT CASE WHEN r.protocol = 'quic' THEN r.strategy_id END) AS quic_strategy_count
        FROM domains d
        LEFT JOIN strategy_domain_results r ON r.domain_id = d.id
        GROUP BY d.id, d.name;

        CREATE VIEW IF NOT EXISTS strategy_stats AS
        SELECT s.id AS strategy_id,
               s.protocol,
               COUNT(DISTINCT r.domain_id) AS domain_count,
               COUNT(DISTINCT CASE WHEN r.source_mode = 'single_domain' THEN r.domain_id END) AS single_domain_count,
               COUNT(DISTINCT CASE WHEN r.source_mode = 'multi_domain' THEN r.domain_id END) AS multi_domain_count
        FROM strategies s
        LEFT JOIN strategy_domain_results r ON r.strategy_id = s.id
        GROUP BY s.id, s.protocol;
        """
    )


def _backfill_strategy_analysis(conn: sqlite3.Connection) -> None:
    if get_meta(conn, "strategy_analysis_backfilled_v10") == "1":
        return
    rows = conn.execute(
        """
        SELECT id, protocol, args
        FROM strategies
        WHERE COALESCE(family_key, '') = ''
           OR COALESCE(fragmentation_reason, '') = ''
           OR COALESCE(family_reason, '') = ''
        """
    ).fetchall()
    for row in rows:
        analysis = analyze_strategy(str(row["protocol"] or ""), str(row["args"] or ""))
        conn.execute(
            """
            UPDATE strategies
            SET fragmentation_class = ?,
                fragmentation_safe = ?,
                fragmentation_reason = ?,
                family = ?,
                family_key = ?,
                family_rank = ?,
                family_reason = ?
            WHERE id = ?
            """,
            (
                analysis.fragmentation_class,
                1 if analysis.fragmentation_safe else 0,
                analysis.fragmentation_reason,
                analysis.family,
                analysis.family_key,
                analysis.family_rank,
                analysis.family_reason,
                str(row["id"] or ""),
            ),
        )
    set_meta(conn, "strategy_analysis_backfilled_v10", "1")


def _drop_legacy_storage(conn: sqlite3.Connection) -> None:
    if get_meta(conn, "legacy_storage_removed_v9") == "1":
        return
    removed = sum(1 for table in _LEGACY_STORAGE_TABLES if _table_exists(conn, table))
    set_meta(conn, "legacy_storage_removed_started_v9", "1")
    for table in _LEGACY_STORAGE_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    if removed:
        set_meta(conn, "needs_vacuum", "1")
    set_meta(conn, "legacy_storage_removed", "1")
    set_meta(conn, "legacy_storage_removed_v9", "1")
    set_meta(conn, "legacy_storage_removed_tables", str(removed))


def _drop_strategy_attempts(conn: sqlite3.Connection) -> None:
    if get_meta(conn, "strategy_attempts_removed_v9") == "1":
        return
    count = _table_count(conn, "strategy_attempts") if _table_exists(conn, "strategy_attempts") else 0
    conn.executescript(
        """
        DROP INDEX IF EXISTS idx_strategy_attempts_strategy;
        DROP INDEX IF EXISTS idx_strategy_attempts_domain;
        DROP INDEX IF EXISTS idx_strategy_attempts_run;
        DROP TABLE IF EXISTS strategy_attempts;
        """
    )
    if count:
        set_meta(conn, "needs_vacuum", "1")
    set_meta(conn, "strategy_attempts_removed_v9", "1")
    set_meta(conn, "strategy_attempts_removed_count", str(count))


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def compact_run_payload(run: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key, value in run.items():
        cleaned = _compact_payload_value(str(key), value, depth=0)
        if cleaned is not _OMITTED:
            compact[str(key)] = cleaned
    return compact


def _compact_payload_value(key: str, value: Any, *, depth: int) -> Any:
    if key in _RUN_PAYLOAD_DROP_KEYS:
        return _OMITTED
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        if len(value) <= _RUN_PAYLOAD_MAX_STRING:
            return value
        return value[:_RUN_PAYLOAD_MAX_STRING] + "...[truncated]"
    if isinstance(value, list):
        if key in _RUN_PAYLOAD_STRUCTURED_LIST_KEYS:
            return [str(item) for item in value if str(item or "").strip()]
        if key in _RUN_PAYLOAD_COMPACT_OBJECT_LIST_KEYS:
            return [
                _compact_payload_value("", item, depth=depth + 1)
                for item in value[:_RUN_PAYLOAD_MAX_OBJECT_LIST]
            ]
        if all(item is None or isinstance(item, bool | int | float | str) for item in value):
            return [
                _compact_payload_value("", item, depth=depth + 1)
                for item in value[:_RUN_PAYLOAD_MAX_SCALAR_LIST]
            ]
        return {"omitted_count": len(value), "omitted_reason": "large structured list"}
    if isinstance(value, dict):
        if depth >= 5:
            return {"omitted_reason": "nested object too deep"}
        compact: dict[str, Any] = {}
        for child_key, child_value in value.items():
            cleaned = _compact_payload_value(str(child_key), child_value, depth=depth + 1)
            if cleaned is not _OMITTED:
                compact[str(child_key)] = cleaned
        return compact
    return str(value)


def _compact_run_payloads(conn: sqlite3.Connection) -> None:
    if get_meta(conn, "run_payloads_compacted_v7") == "1":
        return
    last_seq = _meta_int(conn, "run_payloads_compaction_last_seq_v7")
    changed = _meta_int(conn, "run_payloads_compacted_count")
    original_bytes = _meta_int(conn, "run_payloads_original_bytes")
    compact_bytes = _meta_int(conn, "run_payloads_compact_bytes")
    set_meta(conn, "run_payloads_compaction_started_v7", "1")
    while True:
        rows = conn.execute(
            """
            SELECT seq, payload_json
            FROM runs
            WHERE seq > ?
            ORDER BY seq
            LIMIT ?
            """,
            (last_seq, _RUN_PAYLOAD_COMPACT_BATCH_SIZE),
        ).fetchall()
        if not rows:
            break
        batch_changed = 0
        for row in rows:
            seq = int(row["seq"])
            raw = str(row["payload_json"] or "")
            raw_bytes = len(raw.encode("utf-8"))
            original_bytes += raw_bytes
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                compact_bytes += raw_bytes
                last_seq = seq
                continue
            if not isinstance(data, dict):
                compact_bytes += raw_bytes
                last_seq = seq
                continue
            compact = compact_run_payload(data)
            payload = json.dumps(compact, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            compact_bytes += len(payload.encode("utf-8"))
            if payload != raw:
                conn.execute("UPDATE runs SET payload_json = ? WHERE seq = ?", (payload, seq))
                changed += 1
                batch_changed += 1
            last_seq = seq
        set_meta(conn, "run_payloads_compaction_last_seq_v7", str(last_seq))
        set_meta(conn, "run_payloads_compacted_count", str(changed))
        set_meta(conn, "run_payloads_original_bytes", str(original_bytes))
        set_meta(conn, "run_payloads_compact_bytes", str(compact_bytes))
        if batch_changed:
            set_meta(conn, "needs_vacuum", "1")
        conn.commit()
    set_meta(conn, "run_payloads_compacted_v7", "1")
    set_meta(conn, "run_payloads_compacted_count", str(changed))
    set_meta(conn, "run_payloads_original_bytes", str(original_bytes))
    set_meta(conn, "run_payloads_compact_bytes", str(compact_bytes))
    set_meta(conn, "run_payloads_compaction_completed_v7", "1")
    if changed:
        set_meta(conn, "needs_vacuum", "1")
    conn.commit()


def _meta_int(conn: sqlite3.Connection, key: str, default: int = 0) -> int:
    try:
        return int(get_meta(conn, key) or default)
    except ValueError:
        return default


def _cleanup_runtime_state(conn: sqlite3.Connection, root: Path) -> None:
    if get_meta(conn, "runtime_state_cleaned_v7") != "1":
        has_runtime_data = _table_count(conn, "runs") > 0 or _table_count(conn, "strategies") > 0
        if has_runtime_data:
            for name in _LEGACY_RUNTIME_FILES:
                try:
                    (root / name).unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    continue
        set_meta(conn, "runtime_state_cleaned_v7", "1")
    if get_meta(conn, "jobs_jsonl_compacted_v7") == "1":
        return
    for path in dict.fromkeys((root / "jobs.jsonl", root.parent / "jobs.jsonl")):
        _compact_jobs_jsonl(path)
    set_meta(conn, "jobs_jsonl_compacted_v7", "1")


def _compact_jobs_jsonl(path: Path) -> bool:
    if not path.is_file():
        return False
    changed = False
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with path.open("r", encoding="utf-8", errors="replace") as source, tmp.open("w", encoding="utf-8") as target:
            for line in source:
                text = line.strip()
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    target.write(line if line.endswith("\n") else line + "\n")
                    continue
                if isinstance(payload, dict):
                    compact = _compact_job_record(payload)
                    changed = changed or compact != payload
                    target.write(json.dumps(compact, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")
                else:
                    target.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        if changed:
            tmp.replace(path)
        else:
            tmp.unlink(missing_ok=True)
        return changed
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _compact_job_record(payload: dict[str, Any]) -> dict[str, Any]:
    compact = dict(payload)
    result = compact.get("result")
    if isinstance(result, dict):
        compact["result"] = compact_run_payload(result)
    return compact


def _run_deferred_vacuum(conn: sqlite3.Connection, state_dir: Path) -> None:
    if get_meta(conn, "needs_vacuum") != "1":
        return
    if _state_has_active_job(state_dir):
        return
    conn.commit()
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
    except sqlite3.Error:
        return
    set_meta(conn, "needs_vacuum", "0")
    conn.commit()


def _state_has_active_job(state_dir: Path) -> bool:
    path = state_dir / "state.json"
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    return isinstance(payload, dict) and bool(payload.get("current_job"))


def _args_hash(args: str) -> str:
    return hashlib.sha256(str(args or "").encode("utf-8")).hexdigest()


def _upsert_strategy_conn(
    conn: sqlite3.Connection,
    *,
    strategy_id: str,
    protocol: str,
    args: str,
    status: str,
    seen_at: str,
) -> None:
    strategy_id = str(strategy_id or "").strip()
    if not strategy_id:
        return
    analysis = analyze_strategy(protocol, args)
    conn.execute(
        """
        INSERT INTO strategies(
            id, protocol, args, args_hash, status,
            fragmentation_class, fragmentation_safe, fragmentation_reason,
            family, family_key, family_rank, family_reason
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            protocol = excluded.protocol,
            args = excluded.args,
            args_hash = excluded.args_hash,
            status = excluded.status,
            fragmentation_class = excluded.fragmentation_class,
            fragmentation_safe = excluded.fragmentation_safe,
            fragmentation_reason = excluded.fragmentation_reason,
            family = excluded.family,
            family_key = excluded.family_key,
            family_rank = excluded.family_rank,
            family_reason = excluded.family_reason
        WHERE strategies.protocol != excluded.protocol
           OR strategies.args != excluded.args
           OR strategies.args_hash != excluded.args_hash
           OR strategies.status != excluded.status
           OR strategies.fragmentation_class != excluded.fragmentation_class
           OR strategies.fragmentation_safe != excluded.fragmentation_safe
           OR strategies.fragmentation_reason != excluded.fragmentation_reason
           OR strategies.family != excluded.family
           OR strategies.family_key != excluded.family_key
           OR strategies.family_rank != excluded.family_rank
           OR strategies.family_reason != excluded.family_reason
        """,
        (
            strategy_id,
            protocol,
            args,
            _args_hash(args),
            status or "candidate",
            analysis.fragmentation_class,
            1 if analysis.fragmentation_safe else 0,
            analysis.fragmentation_reason,
            analysis.family,
            analysis.family_key,
            analysis.family_rank,
            analysis.family_reason,
        ),
    )


def _upsert_domain_conn(
    conn: sqlite3.Connection,
    name: str,
    *,
    service_group: str = "",
    created_at: str = "",
    updated_at: str = "",
) -> int | None:
    domain = str(name or "").strip()
    if not domain:
        return None
    conn.execute(
        """
        INSERT INTO domains(name, service_group)
        VALUES(?, ?)
        ON CONFLICT(name) DO UPDATE SET
            service_group = excluded.service_group
        WHERE domains.service_group = '' AND excluded.service_group != ''
        """,
        (domain, service_group),
    )
    row = conn.execute("SELECT id FROM domains WHERE name = ?", (domain,)).fetchone()
    return int(row["id"]) if row else None


def _save_domain_preset_conn(
    conn: sqlite3.Connection,
    *,
    scope: str,
    name: str,
    kind: str,
    domains: list[str],
    updated_at: str,
    source_json: str = "{}",
) -> None:
    clean_name = str(name or "").strip()
    clean_scope = str(scope or "").strip()
    clean_kind = str(kind or "user").strip() or "user"
    if not clean_scope or not clean_name:
        return
    conn.execute(
        """
        INSERT INTO domain_presets(scope, name, kind, label, source_json)
        VALUES(?, ?, ?, ?, ?)
        ON CONFLICT(scope, name, kind) DO UPDATE SET
            label = excluded.label,
            source_json = excluded.source_json
        WHERE domain_presets.label != excluded.label
           OR domain_presets.source_json != excluded.source_json
        """,
        (clean_scope, clean_name, clean_kind, clean_name, source_json or "{}"),
    )
    preset = conn.execute(
        "SELECT id FROM domain_presets WHERE scope = ? AND name = ? AND kind = ?",
        (clean_scope, clean_name, clean_kind),
    ).fetchone()
    if not preset:
        return
    preset_id = int(preset["id"])
    conn.execute("DELETE FROM preset_domains WHERE preset_id = ?", (preset_id,))
    for position, domain in enumerate(_unique_nonempty(domains)):
        domain_id = _upsert_domain_conn(conn, domain)
        if domain_id is None:
            continue
        conn.execute(
            """
            INSERT OR REPLACE INTO preset_domains(preset_id, domain_id, position, enabled)
            VALUES(?, ?, ?, 1)
            """,
            (preset_id, domain_id, position),
        )


def append_run(state_dir: Path, run: dict[str, Any]) -> None:
    payload = compact_run_payload(run)
    with connect(state_dir) as conn:
        conn.execute(
            """
            INSERT INTO runs(id, kind, status, timestamp, payload_json)
            VALUES(?, ?, ?, ?, ?)
            """,
            (
                str(run.get("id") or ""),
                str(run.get("kind") or ""),
                str(run.get("status") or ""),
                str(run.get("timestamp") or ""),
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            ),
        )


def read_run_payloads(state_dir: Path, limit: int = 50) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 50), 1000))
    with connect(state_dir) as conn:
        rows = conn.execute(
            "SELECT payload_json FROM runs ORDER BY seq DESC LIMIT ?",
            (limit,),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in reversed(rows):
        try:
            data = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            result.append(compact_run_payload(data))
    return result


def upsert_candidate_event(
    state_dir: Path,
    *,
    candidate_id: str,
    protocol: str,
    args: str,
    status: str,
    run_id: str,
    domain: str,
    domains: list[str],
    test: str,
    ip_version: str,
    seen_at: str,
    common: bool,
) -> None:
    with connect(state_dir) as conn:
        _upsert_candidate_event_conn(
            conn,
            candidate_id=candidate_id,
            protocol=protocol,
            args=args,
            status=status,
            run_id=run_id,
            domain=domain,
            domains=domains,
            test=test,
            ip_version=ip_version,
            seen_at=seen_at,
            common=common,
        )


def upsert_candidate_event_conn(
    conn: sqlite3.Connection,
    *,
    candidate_id: str,
    protocol: str,
    args: str,
    status: str,
    run_id: str,
    domain: str,
    domains: list[str],
    test: str,
    ip_version: str,
    seen_at: str,
    common: bool,
) -> None:
    _upsert_candidate_event_conn(
        conn,
        candidate_id=candidate_id,
        protocol=protocol,
        args=args,
        status=status,
        run_id=run_id,
        domain=domain,
        domains=domains,
        test=test,
        ip_version=ip_version,
        seen_at=seen_at,
        common=common,
    )


def read_custom_presets(state_dir: Path) -> dict[str, dict[str, list[str]]]:
    with connect(state_dir) as conn:
        rows = conn.execute(
            """
            SELECT p.scope, p.name, d.name AS domain
            FROM domain_presets p
            LEFT JOIN preset_domains pd ON pd.preset_id = p.id
            LEFT JOIN domains d ON d.id = pd.domain_id
            WHERE p.kind = 'user' AND COALESCE(pd.enabled, 1) = 1
            ORDER BY p.scope, p.name, pd.position, d.name
            """
        ).fetchall()
    result: dict[str, dict[str, list[str]]] = {"finder": {}, "common": {}}
    for row in rows:
        scope = str(row["scope"] or "")
        name = str(row["name"] or "")
        if not scope or not name:
            continue
        result.setdefault(scope, {}).setdefault(name, [])
        domain = str(row["domain"] or "").strip()
        if domain and domain not in result[scope][name]:
            result[scope][name].append(domain)
    return result


def read_custom_preset_index(state_dir: Path) -> dict[str, dict[str, dict[str, Any]]]:
    with connect(state_dir) as conn:
        rows = conn.execute(
            """
            SELECT p.scope, p.name, p.label,
                   COUNT(pd.domain_id) AS total_count,
                   COUNT(CASE WHEN COALESCE(pd.enabled, 1) = 1 THEN 1 END) AS enabled_count
            FROM domain_presets p
            LEFT JOIN preset_domains pd ON pd.preset_id = p.id
            WHERE p.kind = 'user'
            GROUP BY p.id, p.scope, p.name, p.label
            ORDER BY p.scope, p.name
            """
        ).fetchall()
    result: dict[str, dict[str, dict[str, Any]]] = {"finder": {}, "common": {}}
    for row in rows:
        scope = str(row["scope"] or "")
        name = str(row["name"] or "")
        if not scope or not name:
            continue
        result.setdefault(scope, {})[name] = {
            "name": name,
            "label": str(row["label"] or name),
            "enabled_count": int(row["enabled_count"] or 0),
            "total_count": int(row["total_count"] or 0),
            "updated_at": "",
        }
    return result


def save_custom_presets(state_dir: Path, presets: dict[str, Any], updated_at: str) -> dict[str, dict[str, list[str]]]:
    clean: dict[str, dict[str, list[str]]] = {"finder": {}, "common": {}}
    for scope in ("finder", "common"):
        raw_scope = presets.get(scope) if isinstance(presets, dict) else {}
        if not isinstance(raw_scope, dict):
            continue
        for raw_name, raw_domains in raw_scope.items():
            name = str(raw_name or "").strip()
            if not name or not isinstance(raw_domains, list):
                continue
            clean[scope][name] = _unique_nonempty([str(item or "") for item in raw_domains])
    with connect(state_dir) as conn:
        user_presets = conn.execute("SELECT id FROM domain_presets WHERE kind = 'user'").fetchall()
        for row in user_presets:
            conn.execute("DELETE FROM preset_domains WHERE preset_id = ?", (int(row["id"]),))
        conn.execute("DELETE FROM domain_presets WHERE kind = 'user'")
        for scope, scoped in clean.items():
            for name, domains in scoped.items():
                _save_domain_preset_conn(
                    conn,
                    scope=scope,
                    name=name,
                    kind="user",
                    domains=domains,
                    updated_at=updated_at,
                )
    return clean


def save_custom_preset(
    state_dir: Path,
    *,
    scope: str,
    name: str,
    domains: list[str],
    updated_at: str,
    source: dict[str, Any] | None = None,
) -> dict[str, dict[str, list[str]]]:
    clean_scope = str(scope or "").strip()
    clean_name = str(name or "").strip()
    if clean_scope not in {"finder", "common"}:
        raise ValueError("scope must be finder or common")
    if not clean_name:
        raise ValueError("preset name is required")
    clean_domains = _unique_nonempty([str(item or "") for item in domains])
    if not clean_domains:
        raise ValueError("preset must contain at least one domain")
    source_json = json.dumps(source or {}, ensure_ascii=False, separators=(",", ":"))
    with connect(state_dir) as conn:
        _save_domain_preset_conn(
            conn,
            scope=clean_scope,
            name=clean_name,
            kind="user",
            domains=clean_domains,
            updated_at=updated_at,
            source_json=source_json,
        )
    return read_custom_presets(state_dir)


def delete_custom_preset(state_dir: Path, *, scope: str, name: str) -> dict[str, dict[str, dict[str, Any]]]:
    clean_scope = str(scope or "").strip()
    clean_name = str(name or "").strip()
    if clean_scope not in {"finder", "common"}:
        raise ValueError("scope must be finder or common")
    if not clean_name:
        raise ValueError("preset name is required")
    with connect(state_dir) as conn:
        conn.execute(
            "DELETE FROM domain_presets WHERE scope = ? AND name = ? AND kind = 'user'",
            (clean_scope, clean_name),
        )
    return read_custom_preset_index(state_dir)


def read_preset_domains_page(
    state_dir: Path,
    *,
    scope: str,
    name: str,
    kind: str = "user",
    query: str = "",
    limit: int = 200,
    offset: int = 0,
    include_disabled: bool = True,
) -> dict[str, Any]:
    clean_scope = str(scope or "").strip()
    clean_name = str(name or "").strip()
    clean_kind = str(kind or "user").strip() or "user"
    clean_query = str(query or "").strip().lower()
    clean_limit = max(1, min(int(limit or 200), 1000))
    clean_offset = max(0, int(offset or 0))
    if not clean_scope or not clean_name:
        return _empty_preset_domains_page(clean_scope, clean_name, clean_kind, clean_query, clean_limit, clean_offset)
    filters = ["p.scope = ?", "p.name = ?", "p.kind = ?"]
    params: list[Any] = [clean_scope, clean_name, clean_kind]
    if clean_query:
        filters.append("LOWER(d.name) LIKE ?")
        params.append(f"%{clean_query}%")
    if not include_disabled:
        filters.append("COALESCE(pd.enabled, 1) = 1")
    where = " AND ".join(filters)
    with connect(state_dir) as conn:
        total_row = conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM domain_presets p
            JOIN preset_domains pd ON pd.preset_id = p.id
            JOIN domains d ON d.id = pd.domain_id
            WHERE {where}
            """,
            params,
        ).fetchone()
        total = int(total_row["count"]) if total_row else 0
        rows = conn.execute(
            f"""
            SELECT d.name AS domain, pd.position, COALESCE(pd.enabled, 1) AS enabled
            FROM domain_presets p
            JOIN preset_domains pd ON pd.preset_id = p.id
            JOIN domains d ON d.id = pd.domain_id
            WHERE {where}
            ORDER BY pd.position, d.name
            LIMIT ? OFFSET ?
            """,
            [*params, clean_limit, clean_offset],
        ).fetchall()
    domains = [
        {
            "domain": str(row["domain"] or ""),
            "position": int(row["position"] or 0),
            "enabled": bool(row["enabled"]),
        }
        for row in rows
    ]
    return {
        "scope": clean_scope,
        "name": clean_name,
        "kind": clean_kind,
        "query": clean_query,
        "limit": clean_limit,
        "offset": clean_offset,
        "total": total,
        "has_more": clean_offset + len(domains) < total,
        "domains": domains,
    }


def set_preset_domain_enabled(
    state_dir: Path,
    *,
    scope: str,
    name: str,
    domain: str,
    enabled: bool,
    updated_at: str,
    kind: str = "user",
) -> dict[str, Any]:
    clean_scope = str(scope or "").strip()
    clean_name = str(name or "").strip()
    clean_domain = str(domain or "").strip()
    clean_kind = str(kind or "user").strip() or "user"
    if clean_kind != "user":
        raise ValueError("only user presets can be edited")
    if clean_scope not in {"finder", "common"}:
        raise ValueError("scope must be finder or common")
    if not clean_name:
        raise ValueError("preset name is required")
    if not clean_domain:
        raise ValueError("domain is required")
    with connect(state_dir) as conn:
        row = conn.execute(
            """
            SELECT pd.preset_id, pd.domain_id
            FROM domain_presets p
            JOIN preset_domains pd ON pd.preset_id = p.id
            JOIN domains d ON d.id = pd.domain_id
            WHERE p.scope = ? AND p.name = ? AND p.kind = 'user' AND d.name = ?
            """,
            (clean_scope, clean_name, clean_domain),
        ).fetchone()
        if not row:
            raise ValueError("preset domain was not found")
        conn.execute(
            "UPDATE preset_domains SET enabled = ? WHERE preset_id = ? AND domain_id = ?",
            (1 if enabled else 0, int(row["preset_id"]), int(row["domain_id"])),
        )
    return {
        "scope": clean_scope,
        "name": clean_name,
        "kind": clean_kind,
        "domain": clean_domain,
        "enabled": bool(enabled),
    }


def _empty_preset_domains_page(
    scope: str,
    name: str,
    kind: str,
    query: str,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    return {
        "scope": scope,
        "name": name,
        "kind": kind,
        "query": query,
        "limit": limit,
        "offset": offset,
        "total": 0,
        "has_more": False,
        "domains": [],
    }


def _upsert_candidate_event_conn(
    conn: sqlite3.Connection,
    *,
    candidate_id: str,
    protocol: str,
    args: str,
    status: str,
    run_id: str,
    domain: str,
    domains: list[str],
    test: str,
    ip_version: str,
    seen_at: str,
    common: bool,
) -> None:
    _upsert_strategy_domain_result_conn(
        conn,
        strategy_id=candidate_id,
        protocol=protocol,
        args=args,
        status=status,
        run_id=run_id,
        domain=domain,
        domains=domains,
        test=test,
        ip_version=ip_version,
        seen_at=seen_at,
        common=common,
    )


def _upsert_strategy_domain_result_conn(
    conn: sqlite3.Connection,
    *,
    strategy_id: str,
    protocol: str,
    args: str,
    status: str,
    run_id: str,
    domain: str,
    domains: list[str],
    test: str,
    ip_version: str,
    seen_at: str,
    common: bool,
) -> None:
    _upsert_strategy_conn(
        conn,
        strategy_id=strategy_id,
        protocol=protocol,
        args=args,
        status=status,
        seen_at=seen_at,
    )
    source_mode = "multi_domain" if common else "single_domain"
    target_domains = domains if common else ([domain] if domain else [])
    for item in _unique_nonempty([str(value or "") for value in target_domains]):
        domain_id = _upsert_domain_conn(conn, item, created_at=seen_at, updated_at=seen_at)
        if domain_id is None:
            continue
        conn.execute(
            """
            INSERT INTO strategy_domain_results(
                strategy_id, domain_id, protocol, source_mode
            )
            VALUES(?, ?, ?, ?)
            ON CONFLICT(strategy_id, domain_id, source_mode) DO UPDATE SET
                protocol = excluded.protocol
            WHERE strategy_domain_results.protocol != excluded.protocol
            """,
            (strategy_id, domain_id, protocol, source_mode),
        )


def _unique_nonempty(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if item and item not in result:
            result.append(item)
    return result
