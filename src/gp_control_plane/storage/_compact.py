"""gp_control_plane.storage._compact — moved from storage.py (split)."""
from __future__ import annotations

from gp_control_plane.state import has_active_runtime
from pathlib import Path
from typing import Any
import json
import sqlite3
from gp_control_plane.storage._constants import _LEGACY_RUNTIME_FILES, _OMITTED, _RUN_PAYLOAD_COMPACT_BATCH_SIZE, _RUN_PAYLOAD_COMPACT_OBJECT_LIST_KEYS, _RUN_PAYLOAD_DROP_KEYS, _RUN_PAYLOAD_MAX_OBJECT_LIST, _RUN_PAYLOAD_MAX_SCALAR_LIST, _RUN_PAYLOAD_MAX_STRING, _RUN_PAYLOAD_STRUCTURED_LIST_KEYS
from gp_control_plane.storage._helpers import _meta_int, _table_count, get_meta, set_meta


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
    return has_active_runtime(state_dir)
