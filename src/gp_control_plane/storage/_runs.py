"""gp_control_plane.storage._runs — moved from storage.py (split)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gp_control_plane.storage._compact import compact_run_payload
from gp_control_plane.storage._connection import connect
from gp_control_plane.storage._helpers import _page_int


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


def read_run_payloads(state_dir: Path, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    limit = _page_int(limit, default=50, minimum=1, maximum=1000)
    offset = _page_int(offset, default=0, minimum=0, maximum=10_000_000)
    with connect(state_dir) as conn:
        rows = conn.execute(
            "SELECT payload_json FROM runs ORDER BY seq DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return _decode_run_payload_rows(rows)


def read_latest_run_payloads(state_dir: Path, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    limit = _page_int(limit, default=50, minimum=1, maximum=1000)
    offset = _page_int(offset, default=0, minimum=0, maximum=10_000_000)
    with connect(state_dir) as conn:
        rows = conn.execute(
            """
            SELECT r.payload_json
            FROM runs r
            JOIN (
                SELECT MAX(seq) AS seq
                FROM runs
                GROUP BY CASE WHEN id = '' THEN 'seq:' || seq ELSE id END
            ) latest ON latest.seq = r.seq
            ORDER BY r.seq DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    return _decode_run_payload_rows(rows)


def count_latest_run_payloads(state_dir: Path) -> int:
    with connect(state_dir) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM (
                SELECT 1
                FROM runs
                GROUP BY CASE WHEN id = '' THEN 'seq:' || seq ELSE id END
            ) latest
            """
        ).fetchone()
    return int(row["count"] or 0) if row else 0


def _decode_run_payload_rows(rows: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in reversed(rows):
        try:
            data = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            result.append(compact_run_payload(data))
    return result
