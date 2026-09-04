"""gp_control_plane.storage._schema — moved from storage.py (split)."""
from __future__ import annotations

import sqlite3
from gp_control_plane.storage._compact import _compact_run_payloads
from gp_control_plane.storage._constants import SCHEMA_MIGRATIONS, SCHEMA_VERSION
from gp_control_plane.storage._migrations import _backfill_strategy_analysis, _drop_legacy_storage, _drop_strategy_attempts, _migrate_minimal_working_model_schema


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

        CREATE TABLE IF NOT EXISTS strategy_pairs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tcp_args TEXT NOT NULL DEFAULT '',
            udp_args TEXT NOT NULL DEFAULT '',
            domain TEXT NOT NULL DEFAULT '',
            overall TEXT NOT NULL DEFAULT '',
            tcp_ms REAL NOT NULL DEFAULT 0,
            udp_ms REAL NOT NULL DEFAULT 0,
            gateway_ms REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT '',
            UNIQUE(tcp_args, udp_args, domain)
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_pairs_domain ON strategy_pairs(domain);

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

        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT ''
        );

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
