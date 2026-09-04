"""gp_control_plane.storage._status — moved from storage.py (split)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from gp_control_plane.storage._connection import connect
from gp_control_plane.storage._constants import SCHEMA_VERSION
from gp_control_plane.storage._helpers import _file_size, _table_count
from gp_control_plane.storage._paths import db_path


def storage_runtime_status(state_dir: Path) -> dict[str, Any]:
    path = db_path(state_dir)
    with connect(state_dir) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    schema_version = str(row["value"] or "") if row else ""
    expected_schema_version = str(SCHEMA_VERSION)
    return {
        "db_path": str(path),
        "schema_version": schema_version,
        "expected_schema_version": expected_schema_version,
        "integrity_check": "not_checked",
        "ready": schema_version == expected_schema_version,
        "db_size_bytes": _file_size(path),
    }


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
    "app_settings",
)


_STORAGE_STATUS_VIEWS = ("domain_stats", "strategy_stats")
