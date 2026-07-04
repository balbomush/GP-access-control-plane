from __future__ import annotations

import json
import sqlite3
import hashlib
import threading
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 8
SCHEMA_MIGRATIONS = (
    (1, "base_candidate_storage"),
    (2, "normalized_domain_strategy_model"),
    (3, "minimal_backup_model"),
    (4, "runtime_observability"),
    (5, "remove_legacy_candidate_storage"),
    (6, "preset_domain_state"),
    (7, "compact_runtime_payloads"),
    (8, "trim_strategy_attempt_diagnostics"),
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
_RUN_PAYLOAD_MAX_SCALAR_LIST = 500
_RUN_PAYLOAD_MAX_STRING = 8192
_LEGACY_RUNTIME_FILES = ("available.ndjson", "runs.jsonl", "candidates.json")


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
            _run_deferred_vacuum(conn)
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
    "strategy_attempts",
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
            service_group TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_domains_name ON domains(name);
        CREATE INDEX IF NOT EXISTS idx_domains_service_group ON domains(service_group);

        CREATE TABLE IF NOT EXISTS strategies (
            id TEXT PRIMARY KEY,
            protocol TEXT NOT NULL DEFAULT '',
            args TEXT NOT NULL DEFAULT '',
            args_hash TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'candidate',
            first_seen_at TEXT NOT NULL DEFAULT '',
            last_seen_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_strategies_last_seen ON strategies(last_seen_at DESC, id ASC);
        CREATE INDEX IF NOT EXISTS idx_strategies_protocol ON strategies(protocol);
        CREATE INDEX IF NOT EXISTS idx_strategies_args_hash ON strategies(args_hash);

        CREATE TABLE IF NOT EXISTS strategy_domain_results (
            strategy_id TEXT NOT NULL,
            domain_id INTEGER NOT NULL,
            protocol TEXT NOT NULL DEFAULT '',
            source_mode TEXT NOT NULL DEFAULT 'single_domain',
            first_seen_at TEXT NOT NULL DEFAULT '',
            last_seen_at TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(strategy_id, domain_id, source_mode),
            FOREIGN KEY(strategy_id) REFERENCES strategies(id) ON DELETE CASCADE,
            FOREIGN KEY(domain_id) REFERENCES domains(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_domain_results_domain_protocol ON strategy_domain_results(domain_id, protocol);
        CREATE INDEX IF NOT EXISTS idx_strategy_domain_results_domain_strategy ON strategy_domain_results(domain_id, strategy_id);
        CREATE INDEX IF NOT EXISTS idx_strategy_domain_results_strategy_domain ON strategy_domain_results(strategy_id, domain_id);
        CREATE INDEX IF NOT EXISTS idx_strategy_domain_results_source ON strategy_domain_results(source_mode);

        CREATE TABLE IF NOT EXISTS strategy_attempts (
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
            checked_at TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(strategy_id) REFERENCES strategies(id) ON DELETE CASCADE,
            FOREIGN KEY(domain_id) REFERENCES domains(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_attempts_strategy ON strategy_attempts(strategy_id, id);
        CREATE INDEX IF NOT EXISTS idx_strategy_attempts_domain ON strategy_attempts(domain_id, id);
        CREATE INDEX IF NOT EXISTS idx_strategy_attempts_run ON strategy_attempts(run_id, id);

        CREATE TABLE IF NOT EXISTS domain_presets (
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
               COUNT(DISTINCT CASE WHEN r.protocol = 'quic' THEN r.strategy_id END) AS quic_strategy_count,
               COUNT(r.strategy_id) AS success_count,
               0 AS fail_count,
               MAX(COALESCE(r.last_seen_at, '')) AS last_success_at
        FROM domains d
        LEFT JOIN strategy_domain_results r ON r.domain_id = d.id
        GROUP BY d.id, d.name;

        CREATE VIEW IF NOT EXISTS strategy_stats AS
        SELECT s.id AS strategy_id,
               s.protocol,
               COUNT(DISTINCT r.domain_id) AS domain_count,
               COUNT(DISTINCT CASE WHEN r.source_mode = 'single_domain' THEN r.domain_id END) AS single_domain_count,
               COUNT(DISTINCT CASE WHEN r.source_mode = 'multi_domain' THEN r.domain_id END) AS multi_domain_count,
               COUNT(r.domain_id) AS success_count,
               0 AS fail_count,
               MAX(COALESCE(r.last_seen_at, '')) AS last_success_at
        FROM strategies s
        LEFT JOIN strategy_domain_results r ON r.strategy_id = s.id
        GROUP BY s.id, s.protocol;
        """
    )
    current_schema_version = get_meta(conn, "schema_version")
    _ensure_column(conn, "domain_presets", "source_json", "TEXT NOT NULL DEFAULT '{}'")
    _ensure_column(conn, "preset_domains", "enabled", "INTEGER NOT NULL DEFAULT 1")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_preset_domains_preset_enabled_position ON preset_domains(preset_id, enabled, position)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_preset_domains_preset_position ON preset_domains(preset_id, position)")
    strategy_results_changed = _migrate_strategy_domain_results_schema(conn)
    if strategy_results_changed or current_schema_version != str(SCHEMA_VERSION):
        _recreate_stats_views(conn)
    _drop_legacy_storage(conn)
    _compact_run_payloads(conn)
    _trim_strategy_attempts(conn)
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


def _migrate_strategy_domain_results_schema(conn: sqlite3.Connection) -> bool:
    columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(strategy_domain_results)").fetchall()}
    legacy_columns = {"success_count", "fail_count", "last_success_run_id", "last_fail_run_id"}
    if not (columns & legacy_columns):
        return False
    conn.executescript(
        """
        DROP VIEW IF EXISTS domain_stats;
        DROP VIEW IF EXISTS strategy_stats;
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
            first_seen_at TEXT NOT NULL DEFAULT '',
            last_seen_at TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(strategy_id, domain_id, source_mode),
            FOREIGN KEY(strategy_id) REFERENCES strategies(id) ON DELETE CASCADE,
            FOREIGN KEY(domain_id) REFERENCES domains(id) ON DELETE CASCADE
        );
        INSERT OR IGNORE INTO strategy_domain_results(
            strategy_id, domain_id, protocol, source_mode, first_seen_at, last_seen_at
        )
        SELECT strategy_id, domain_id, protocol, source_mode, first_seen_at, last_seen_at
        FROM strategy_domain_results_old;
        DROP TABLE strategy_domain_results_old;
        CREATE INDEX IF NOT EXISTS idx_strategy_domain_results_domain_protocol ON strategy_domain_results(domain_id, protocol);
        CREATE INDEX IF NOT EXISTS idx_strategy_domain_results_domain_strategy ON strategy_domain_results(domain_id, strategy_id);
        CREATE INDEX IF NOT EXISTS idx_strategy_domain_results_strategy_domain ON strategy_domain_results(strategy_id, domain_id);
        CREATE INDEX IF NOT EXISTS idx_strategy_domain_results_source ON strategy_domain_results(source_mode);
        """
    )
    return True


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
               COUNT(DISTINCT CASE WHEN r.protocol = 'quic' THEN r.strategy_id END) AS quic_strategy_count,
               COUNT(r.strategy_id) AS success_count,
               0 AS fail_count,
               MAX(COALESCE(r.last_seen_at, '')) AS last_success_at
        FROM domains d
        LEFT JOIN strategy_domain_results r ON r.domain_id = d.id
        GROUP BY d.id, d.name;

        CREATE VIEW IF NOT EXISTS strategy_stats AS
        SELECT s.id AS strategy_id,
               s.protocol,
               COUNT(DISTINCT r.domain_id) AS domain_count,
               COUNT(DISTINCT CASE WHEN r.source_mode = 'single_domain' THEN r.domain_id END) AS single_domain_count,
               COUNT(DISTINCT CASE WHEN r.source_mode = 'multi_domain' THEN r.domain_id END) AS multi_domain_count,
               COUNT(r.domain_id) AS success_count,
               0 AS fail_count,
               MAX(COALESCE(r.last_seen_at, '')) AS last_success_at
        FROM strategies s
        LEFT JOIN strategy_domain_results r ON r.strategy_id = s.id
        GROUP BY s.id, s.protocol;
        """
    )


def _drop_legacy_storage(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS candidate_seen_events;
        DROP TABLE IF EXISTS candidate_common_domains;
        DROP TABLE IF EXISTS candidate_domains;
        DROP TABLE IF EXISTS candidates;
        DROP TABLE IF EXISTS presets;
        """
    )
    set_meta(conn, "legacy_storage_removed", "1")


def _trim_strategy_attempts(conn: sqlite3.Connection) -> None:
    if get_meta(conn, "strategy_attempts_trimmed_v8") == "1":
        return
    count = _table_count(conn, "strategy_attempts")
    if count:
        conn.execute("DELETE FROM strategy_attempts")
        set_meta(conn, "needs_vacuum", "1")
    set_meta(conn, "strategy_attempts_trimmed_v8", "1")
    set_meta(conn, "strategy_attempts_trimmed_count", str(count))


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
    seqs = [int(row["seq"]) for row in conn.execute("SELECT seq FROM runs ORDER BY seq").fetchall()]
    changed = 0
    original_bytes = 0
    compact_bytes = 0
    for seq in seqs:
        row = conn.execute("SELECT payload_json FROM runs WHERE seq = ?", (seq,)).fetchone()
        if not row:
            continue
        raw = str(row["payload_json"] or "")
        original_bytes += len(raw.encode("utf-8"))
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            compact_bytes += len(raw.encode("utf-8"))
            continue
        if not isinstance(data, dict):
            compact_bytes += len(raw.encode("utf-8"))
            continue
        compact = compact_run_payload(data)
        payload = json.dumps(compact, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        compact_bytes += len(payload.encode("utf-8"))
        if payload != raw:
            conn.execute("UPDATE runs SET payload_json = ? WHERE seq = ?", (payload, seq))
            changed += 1
    set_meta(conn, "run_payloads_compacted_v7", "1")
    set_meta(conn, "run_payloads_compacted_count", str(changed))
    set_meta(conn, "run_payloads_original_bytes", str(original_bytes))
    set_meta(conn, "run_payloads_compact_bytes", str(compact_bytes))
    if changed:
        set_meta(conn, "needs_vacuum", "1")


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


def _run_deferred_vacuum(conn: sqlite3.Connection) -> None:
    if get_meta(conn, "needs_vacuum") != "1":
        return
    conn.commit()
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
    except sqlite3.Error:
        return
    set_meta(conn, "needs_vacuum", "0")
    conn.commit()


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
    conn.execute(
        """
        INSERT INTO strategies(id, protocol, args, args_hash, status, first_seen_at, last_seen_at)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            protocol = excluded.protocol,
            args = excluded.args,
            args_hash = excluded.args_hash,
            status = excluded.status,
            last_seen_at = excluded.last_seen_at
        """,
        (strategy_id, protocol, args, _args_hash(args), status or "candidate", seen_at, seen_at),
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
        INSERT INTO domains(name, service_group, created_at, updated_at)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            service_group = CASE
                WHEN domains.service_group = '' THEN excluded.service_group
                ELSE domains.service_group
            END,
            updated_at = CASE
                WHEN excluded.updated_at != '' THEN excluded.updated_at
                ELSE domains.updated_at
            END
        """,
        (domain, service_group, created_at, updated_at or created_at),
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
        INSERT INTO domain_presets(scope, name, kind, label, source_json, created_at, updated_at)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(scope, name, kind) DO UPDATE SET
            label = excluded.label,
            source_json = excluded.source_json,
            updated_at = excluded.updated_at
        """,
        (clean_scope, clean_name, clean_kind, clean_name, source_json or "{}", updated_at, updated_at),
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
        domain_id = _upsert_domain_conn(conn, domain, updated_at=updated_at)
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
            SELECT p.scope, p.name, p.label, p.updated_at,
                   COUNT(pd.domain_id) AS total_count,
                   COUNT(CASE WHEN COALESCE(pd.enabled, 1) = 1 THEN 1 END) AS enabled_count
            FROM domain_presets p
            LEFT JOIN preset_domains pd ON pd.preset_id = p.id
            WHERE p.kind = 'user'
            GROUP BY p.id, p.scope, p.name, p.label, p.updated_at
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
            "updated_at": str(row["updated_at"] or ""),
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
        conn.execute(
            "UPDATE domain_presets SET updated_at = ? WHERE id = ?",
            (updated_at, int(row["preset_id"])),
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
                strategy_id, domain_id, protocol, source_mode, first_seen_at, last_seen_at
            )
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(strategy_id, domain_id, source_mode) DO UPDATE SET
                protocol = excluded.protocol,
                last_seen_at = excluded.last_seen_at
            """,
            (strategy_id, domain_id, protocol, source_mode, seen_at, seen_at),
        )


def _unique_nonempty(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if item and item not in result:
            result.append(item)
    return result
