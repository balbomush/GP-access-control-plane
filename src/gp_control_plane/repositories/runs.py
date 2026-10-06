'Unchanged runs operations. The injected factory is the existing storage connection/transaction owner.'

from __future__ import annotations


import json
from pathlib import Path
from typing import Any
from .run_payload import compact_run_payload


def append_run(state_dir: Path, run: dict[str, Any], *, _connect) -> None:
    payload = compact_run_payload(run)
    with _connect(state_dir) as conn:
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


def _page_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def read_run_payloads(state_dir: Path, limit: int = 50, offset: int = 0, *, _connect) -> list[dict[str, Any]]:
    limit = _page_int(limit, default=50, minimum=1, maximum=1000)
    offset = _page_int(offset, default=0, minimum=0, maximum=10_000_000)
    with _connect(state_dir) as conn:
        rows = conn.execute(
            "SELECT payload_json FROM runs ORDER BY seq DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return _decode_run_payload_rows(rows)


def read_latest_run_payloads(state_dir: Path, limit: int = 50, offset: int = 0, *, _connect) -> list[dict[str, Any]]:
    limit = _page_int(limit, default=50, minimum=1, maximum=1000)
    offset = _page_int(offset, default=0, minimum=0, maximum=10_000_000)
    with _connect(state_dir) as conn:
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


def count_latest_run_payloads(state_dir: Path, *, _connect) -> int:
    with _connect(state_dir) as conn:
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
