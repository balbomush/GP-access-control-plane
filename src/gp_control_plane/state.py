from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


REMOVED_STATE_KEYS = {
    "last_sync_at",
    "last_validate_at",
    "last_render_at",
    "selected_strategy",
}


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_state(state_dir: Path) -> dict[str, Any]:
    path = state_dir / "state.json"
    defaults = {
        "current_job": None,
        "last_error": None,
    }
    if not path.exists():
        return defaults
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return defaults
    state = {**defaults, **raw}
    for key in REMOVED_STATE_KEYS:
        state.pop(key, None)
    return state


def write_state(state_dir: Path, state: dict[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "state.json"
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    last_error: PermissionError | None = None
    try:
        for attempt in range(20):
            try:
                tmp.replace(path)
                return
            except PermissionError as exc:
                last_error = exc
                time.sleep(min(0.05 * (attempt + 1), 0.5))
        if last_error:
            raise last_error
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def append_jsonl(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n")


def read_jsonl(path: Path, limit: int = 100) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    result = []
    for line in lines[-limit:]:
        if line.strip():
            result.append(json.loads(line))
    return result
