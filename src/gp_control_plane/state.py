from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


REMOVED_STATE_KEYS = {
    "last_sync_at",
    "last_validate_at",
    "last_render_at",
    "selected_strategy",
    "current_job",
    "current_job_name",
    "current_job_status",
}

JOB_RUNNER_LOCK_FILE_NAME = "job-runner.lock"
STATE_UPDATE_LOCK_FILE_NAME = "state-update.lock"
STATE_UPDATE_LOCK_TIMEOUT_SECONDS = 5.0


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_state(state_dir: Path) -> dict[str, Any]:
    path = state_dir / "state.json"
    defaults = {
        "current_run_id": None,
        "last_error": None,
    }
    if not path.exists():
        return defaults
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return defaults
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


def update_state(state_dir: Path, updater: Callable[[dict[str, Any]], dict[str, Any] | None]) -> dict[str, Any]:
    with _StateUpdateLock.acquire(state_dir):
        state = read_state(state_dir)
        updated = updater(dict(state))
        next_state = state if updated is None else updated
        write_state(state_dir, next_state)
        return next_state


def active_runtime_payload(state_dir: Path) -> dict[str, Any]:
    state = read_state(state_dir)
    lock = active_job_lock_payload(state_dir, cleanup_stale=True)
    current_run_id = str(state.get("current_run_id") or lock.get("run_id") or "").strip()
    current_name = str(state.get("current_run_name") or lock.get("run_name") or "").strip()
    current_status = str(state.get("current_run_status") or ("running" if lock else "") or "").strip()
    return {
        "active": bool(current_run_id or lock),
        "run_id": current_run_id,
        "run_name": current_name,
        "status": current_status,
        "source": "state" if state.get("current_run_id") else ("lock" if lock else ""),
        "lock": lock,
    }


def has_active_runtime(state_dir: Path) -> bool:
    return bool(active_runtime_payload(state_dir).get("active"))


def job_lock_path(state_dir: Path) -> Path:
    return state_dir / JOB_RUNNER_LOCK_FILE_NAME


def read_job_lock_payload(state_dir: Path) -> dict[str, Any]:
    path = job_lock_path(state_dir)
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except Exception:  # noqa: BLE001
        return {}
    return payload if isinstance(payload, dict) else {}


def active_job_lock_payload(state_dir: Path, *, cleanup_stale: bool = False) -> dict[str, Any]:
    path = job_lock_path(state_dir)
    if not path.exists():
        return {}
    payload = read_job_lock_payload(state_dir)
    if not payload:
        return {"run_id": "", "run_name": "", "corrupt": True}
    if is_stale_process_payload(payload):
        if cleanup_stale:
            try:
                path.unlink()
            except FileNotFoundError:
                return {}
            except OSError:
                return payload
        return {}
    return payload


def is_stale_process_payload(payload: dict[str, Any]) -> bool:
    pid = payload.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    if pid == os.getpid():
        return False
    return not _pid_is_running(pid)


class _StateUpdateLock:
    def __init__(self, path: Path, handle: Any):
        self._path = path
        self._handle = handle
        self._released = False

    @classmethod
    def acquire(cls, state_dir: Path) -> "_StateUpdateLock":
        state_dir.mkdir(parents=True, exist_ok=True)
        path = state_dir / STATE_UPDATE_LOCK_FILE_NAME
        payload = {"pid": os.getpid(), "created_at": now_iso()}
        deadline = time.monotonic() + STATE_UPDATE_LOCK_TIMEOUT_SECONDS
        handle = path.open("a+", encoding="utf-8")
        while True:
            try:
                _try_lock_file(handle)
            except OSError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise TimeoutError("state update lock is busy")
                time.sleep(0.01)
                continue
            handle.seek(0)
            handle.truncate()
            json.dump(payload, handle, ensure_ascii=True)
            handle.flush()
            return cls(path, handle)

    def __enter__(self) -> "_StateUpdateLock":
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            _unlock_file(self._handle)
        finally:
            self._handle.close()


def _try_lock_file(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_lock_payload(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except Exception:  # noqa: BLE001
        return {}
    return payload if isinstance(payload, dict) else {}


def _pid_is_running(pid: int) -> bool:
    if os.name == "nt":
        return _windows_pid_is_running(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _windows_pid_is_running(pid: int) -> bool:
    import ctypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


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
