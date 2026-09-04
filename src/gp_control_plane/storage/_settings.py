"""gp_control_plane.storage._settings — moved from storage.py (split)."""
from __future__ import annotations

from pathlib import Path
from typing import Any
import json
from gp_control_plane.storage._connection import connect


def read_app_setting(state_dir: Path, key: str) -> Any | None:
    clean_key = str(key or "").strip()
    if not clean_key:
        return None
    with connect(state_dir) as conn:
        row = conn.execute("SELECT value_json FROM app_settings WHERE key = ?", (clean_key,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(str(row["value_json"] or "null"))
    except json.JSONDecodeError:
        return None


def read_or_create_app_setting(state_dir: Path, key: str, value: Any, updated_at: str) -> Any:
    clean_key = str(key or "").strip()
    if not clean_key:
        raise ValueError("setting key is required")
    value_json = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    with connect(state_dir) as conn:
        conn.execute(
            """
            INSERT INTO app_settings(key, value_json, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(key) DO NOTHING
            """,
            (clean_key, value_json, str(updated_at or "")),
        )
        row = conn.execute("SELECT value_json FROM app_settings WHERE key = ?", (clean_key,)).fetchone()
    try:
        return json.loads(str(row["value_json"] or "null")) if row else None
    except json.JSONDecodeError:
        return None


def save_app_setting(state_dir: Path, key: str, value: Any, updated_at: str) -> Any:
    clean_key = str(key or "").strip()
    if not clean_key:
        raise ValueError("setting key is required")
    value_json = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    with connect(state_dir) as conn:
        conn.execute(
            """
            INSERT INTO app_settings(key, value_json, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (clean_key, value_json, str(updated_at or "")),
        )
    return value
