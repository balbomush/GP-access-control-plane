"""gp_control_plane.storage._migrations — moved from storage.py (split)."""
from __future__ import annotations

from gp_control_plane.strategy_safety import analyze_strategy
import sqlite3
from gp_control_plane.storage._constants import _LEGACY_STORAGE_TABLES
from gp_control_plane.storage._helpers import _args_hash, _table_columns, _table_count, _table_exists, get_meta, set_meta


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
