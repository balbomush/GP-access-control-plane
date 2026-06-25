from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


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


def connect(state_dir: Path) -> sqlite3.Connection:
    path = db_path(state_dir)
    conn = sqlite3.connect(path, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate_schema(conn)
    return conn


def _migrate_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
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

        CREATE TABLE IF NOT EXISTS candidates (
            id TEXT PRIMARY KEY,
            protocol TEXT NOT NULL DEFAULT '',
            args TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'candidate',
            first_seen_at TEXT NOT NULL DEFAULT '',
            last_seen_at TEXT NOT NULL DEFAULT '',
            seen_count INTEGER NOT NULL DEFAULT 0,
            common_seen_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_candidates_last_seen ON candidates(last_seen_at DESC);
        CREATE INDEX IF NOT EXISTS idx_candidates_protocol ON candidates(protocol);

        CREATE TABLE IF NOT EXISTS candidate_domains (
            candidate_id TEXT NOT NULL,
            domain TEXT NOT NULL,
            protocol TEXT NOT NULL DEFAULT '',
            first_seen_at TEXT NOT NULL DEFAULT '',
            last_seen_at TEXT NOT NULL DEFAULT '',
            seen_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(candidate_id, domain),
            FOREIGN KEY(candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_candidate_domains_domain ON candidate_domains(domain);

        CREATE TABLE IF NOT EXISTS candidate_common_domains (
            candidate_id TEXT NOT NULL,
            domain TEXT NOT NULL,
            protocol TEXT NOT NULL DEFAULT '',
            first_seen_at TEXT NOT NULL DEFAULT '',
            last_seen_at TEXT NOT NULL DEFAULT '',
            seen_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(candidate_id, domain),
            FOREIGN KEY(candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_candidate_common_domains_domain ON candidate_common_domains(domain);

        CREATE TABLE IF NOT EXISTS candidate_seen_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id TEXT NOT NULL,
            run_id TEXT NOT NULL DEFAULT '',
            domain TEXT NOT NULL DEFAULT '',
            domains_json TEXT NOT NULL DEFAULT '[]',
            test TEXT NOT NULL DEFAULT '',
            ip_version TEXT NOT NULL DEFAULT '',
            is_common INTEGER NOT NULL DEFAULT 0,
            seen_at TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_candidate_seen_candidate_seq ON candidate_seen_events(candidate_id, seq);
        CREATE INDEX IF NOT EXISTS idx_candidate_seen_run ON candidate_seen_events(run_id);

        CREATE TABLE IF NOT EXISTS presets (
            scope TEXT NOT NULL,
            name TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            domains_json TEXT NOT NULL DEFAULT '[]',
            source_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL DEFAULT '',
            builtin INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(scope, name)
        );
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else ""


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))


def append_run(state_dir: Path, run: dict[str, Any]) -> None:
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
                json.dumps(run, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            ),
        )


def read_run_payloads(state_dir: Path, limit: int = 50) -> list[dict[str, Any]]:
    _import_runs_jsonl_if_needed(state_dir)
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
            result.append(data)
    return result


def _import_runs_jsonl_if_needed(state_dir: Path) -> None:
    path = state_dir / "strategy-finder" / "runs.jsonl"
    if not path.exists():
        return
    stat = path.stat()
    marker = f"{stat.st_size}:{stat.st_mtime_ns}"
    with connect(state_dir) as conn:
        if get_meta(conn, "imported_runs_jsonl") == marker:
            return
        existing = int(conn.execute("SELECT COUNT(*) AS count FROM runs").fetchone()["count"])
        if existing:
            set_meta(conn, "imported_runs_jsonl", marker)
            conn.commit()
            return
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                conn.execute(
                    """
                    INSERT INTO runs(id, kind, status, timestamp, payload_json)
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    (
                        str(payload.get("id") or ""),
                        str(payload.get("kind") or ""),
                        str(payload.get("status") or ""),
                        str(payload.get("timestamp") or ""),
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                    ),
                )
        set_meta(conn, "imported_runs_jsonl", marker)
        conn.commit()


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


def read_custom_presets(state_dir: Path) -> dict[str, dict[str, list[str]]]:
    with connect(state_dir) as conn:
        rows = conn.execute(
            "SELECT scope, name, domains_json FROM presets WHERE builtin = 0 ORDER BY scope, name"
        ).fetchall()
    result: dict[str, dict[str, list[str]]] = {"finder": {}, "common": {}}
    for row in rows:
        scope = str(row["scope"] or "")
        name = str(row["name"] or "")
        if not scope or not name:
            continue
        try:
            domains = json.loads(str(row["domains_json"] or "[]"))
        except json.JSONDecodeError:
            domains = []
        if not isinstance(domains, list):
            domains = []
        result.setdefault(scope, {})[name] = _unique_nonempty([str(item or "") for item in domains])
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
        conn.execute("DELETE FROM presets WHERE builtin = 0")
        for scope, scoped in clean.items():
            for name, domains in scoped.items():
                conn.execute(
                    """
                    INSERT INTO presets(scope, name, label, domains_json, source_json, updated_at, builtin)
                    VALUES(?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        scope,
                        name,
                        name,
                        json.dumps(domains, ensure_ascii=False, separators=(",", ":")),
                        "{}",
                        updated_at,
                    ),
                )
    return clean


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
    conn.execute(
        """
        INSERT INTO candidates(id, protocol, args, status, first_seen_at, last_seen_at, seen_count, common_seen_count)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            protocol = excluded.protocol,
            args = excluded.args,
            status = excluded.status,
            last_seen_at = excluded.last_seen_at,
            seen_count = candidates.seen_count + ?,
            common_seen_count = candidates.common_seen_count + ?
        """,
        (
            candidate_id,
            protocol,
            args,
            status,
            seen_at,
            seen_at,
            0 if common else 1,
            1 if common else 0,
            0 if common else 1,
            1 if common else 0,
        ),
    )
    domains_to_record = domains if common else ([domain] if domain else [])
    table = "candidate_common_domains" if common else "candidate_domains"
    for item in _unique_nonempty(domains_to_record):
        conn.execute(
            f"""
            INSERT INTO {table}(candidate_id, domain, protocol, first_seen_at, last_seen_at, seen_count)
            VALUES(?, ?, ?, ?, ?, 1)
            ON CONFLICT(candidate_id, domain) DO UPDATE SET
                protocol = excluded.protocol,
                last_seen_at = excluded.last_seen_at,
                seen_count = {table}.seen_count + 1
            """,
            (candidate_id, item, protocol, seen_at, seen_at),
        )
    conn.execute(
        """
        INSERT INTO candidate_seen_events(
            candidate_id, run_id, domain, domains_json, test, ip_version, is_common, seen_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            candidate_id,
            run_id,
            domain,
            json.dumps(_unique_nonempty(domains), ensure_ascii=False, separators=(",", ":")),
            test,
            ip_version,
            1 if common else 0,
            seen_at,
        ),
    )


def import_candidates_json_if_needed(state_dir: Path, candidate_id_for: Any) -> None:
    path = state_dir / "strategy-finder" / "candidates.json"
    if not path.exists():
        return
    stat = path.stat()
    marker = f"{stat.st_size}:{stat.st_mtime_ns}"
    with connect(state_dir) as conn:
        if get_meta(conn, "imported_candidates_json") == marker:
            return
        existing = int(conn.execute("SELECT COUNT(*) AS count FROM candidates").fetchone()["count"])
        if existing:
            set_meta(conn, "imported_candidates_json", marker)
            conn.commit()
            return
        for candidate in iter_candidate_json(path):
            protocol = str(candidate.get("protocol") or "")
            args = str(candidate.get("args") or "")
            candidate_id = str(candidate.get("id") or candidate_id_for(protocol, args))
            status = str(candidate.get("status") or "candidate")
            first_seen = str(candidate.get("first_seen_at") or "")
            last_seen = str(candidate.get("last_seen_at") or first_seen)
            conn.execute(
                """
                INSERT OR IGNORE INTO candidates(
                    id, protocol, args, status, first_seen_at, last_seen_at, seen_count, common_seen_count
                )
                VALUES(?, ?, ?, ?, ?, ?, 0, 0)
                """,
                (candidate_id, protocol, args, status, first_seen, last_seen),
            )
            for seen in _list_of_dicts(candidate.get("seen")):
                _upsert_candidate_event_conn(
                    conn,
                    candidate_id=candidate_id,
                    protocol=protocol,
                    args=args,
                    status=status,
                    run_id=str(seen.get("run_id") or ""),
                    domain=str(seen.get("domain") or ""),
                    domains=[],
                    test=str(seen.get("test") or ""),
                    ip_version=str(seen.get("ip_version") or ""),
                    seen_at=str(seen.get("seen_at") or last_seen),
                    common=False,
                )
            for seen in _list_of_dicts(candidate.get("common_seen")):
                domains = [str(item or "") for item in seen.get("domains", [])] if isinstance(seen.get("domains"), list) else []
                _upsert_candidate_event_conn(
                    conn,
                    candidate_id=candidate_id,
                    protocol=protocol,
                    args=args,
                    status=status,
                    run_id=str(seen.get("run_id") or ""),
                    domain="",
                    domains=domains,
                    test=str(seen.get("test") or ""),
                    ip_version=str(seen.get("ip_version") or ""),
                    seen_at=str(seen.get("seen_at") or last_seen),
                    common=True,
                )
        set_meta(conn, "imported_candidates_json", marker)
        conn.commit()


def iter_candidate_json(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    decoder = json.JSONDecoder()
    buffer = ""
    eof = False

    def fill(handle: Any) -> None:
        nonlocal buffer, eof
        chunk = handle.read(65536)
        if chunk:
            buffer += chunk
        else:
            eof = True

    with path.open("r", encoding="utf-8") as handle:
        fill(handle)
        while True:
            buffer = buffer.lstrip()
            if buffer:
                break
            if eof:
                return
            fill(handle)
        if not buffer.startswith("["):
            return
        buffer = buffer[1:]
        while True:
            while True:
                buffer = buffer.lstrip()
                if buffer:
                    break
                if eof:
                    return
                fill(handle)
            if buffer.startswith("]"):
                return
            if buffer.startswith(","):
                buffer = buffer[1:]
                continue
            while True:
                try:
                    value, end = decoder.raw_decode(buffer)
                    break
                except json.JSONDecodeError:
                    if eof:
                        return
                    fill(handle)
            if isinstance(value, dict):
                yield value
            buffer = buffer[end:]


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _unique_nonempty(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if item and item not in result:
            result.append(item)
    return result
