from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .state import (
    JOB_RUNNER_LOCK_FILE_NAME,
    append_jsonl,
    is_stale_process_payload,
    now_iso,
    read_job_lock_payload,
    update_state,
)
from .storage import compact_run_payload


FINAL_JOB_STATUSES = {"success", "failed", "timeout", "stopped"}


@dataclass(frozen=True)
class Job:
    id: str
    name: str
    status: str
    created_at: str


class JobRunner:
    def __init__(self, state_dir: Path, on_idle: Callable[[], Any] | None = None):
        self.state_dir = state_dir
        self._on_idle = on_idle
        self._lock = threading.Lock()
        self._active: str | None = None
        self._active_name: str | None = None
        self._active_cancel: threading.Event | None = None
        self._active_cancel_hook: Callable[[], Any] | None = None
        self._active_state_lock: _StateDirJobLock | None = None

    def start(
        self,
        name: str,
        func: Callable[[threading.Event], Any],
        cancel_hook: Callable[[], Any] | None = None,
    ) -> Job:
        with self._lock:
            if self._active:
                raise RuntimeError(f"job already running: {self._active}")
            job_id = uuid.uuid4().hex[:12]
            cancel_event = threading.Event()
            state_lock = _StateDirJobLock.acquire(self.state_dir, job_id, name)
            self._active = job_id
            self._active_name = name
            self._active_cancel = cancel_event
            self._active_cancel_hook = cancel_hook
            self._active_state_lock = state_lock
        created_at = now_iso()
        job = Job(id=job_id, name=name, status="queued", created_at=created_at)
        try:
            self._record(job_id, name, "queued", created_at)
            self._set_current_job(job_id, name, "queued")
            thread = threading.Thread(target=self._run, args=(job_id, name, func, cancel_event), daemon=True)
            thread.start()
        except Exception:
            self._clear_active_job(job_id, release_state_lock=True)
            raise
        return job

    def cancel_active(self) -> dict[str, str]:
        with self._lock:
            if not self._active or not self._active_cancel or not self._active_name:
                raise RuntimeError("no active job")
            job_id = self._active
            name = self._active_name
            self._active_cancel.set()
            cancel_hook = self._active_cancel_hook
        if cancel_hook:
            threading.Thread(target=self._run_cancel_hook, args=(cancel_hook,), daemon=True).start()
        self._record(job_id, name, "stopping", now_iso())
        self._set_current_job_if_active(job_id, name, "stopping")
        return {"id": job_id, "name": name, "status": "stopping"}

    @staticmethod
    def _run_cancel_hook(cancel_hook: Callable[[], Any]) -> None:
        try:
            cancel_hook()
        except Exception:
            return

    def _run(self, job_id: str, name: str, func: Callable[[threading.Event], Any], cancel_event: threading.Event) -> None:
        self._record(job_id, name, "running", now_iso())
        self._set_current_job_if_active(job_id, name, "running")
        last_error: str | None = None
        last_job_status = "failed"
        try:
            result = func(cancel_event)
            status = _status_from_result(result)
            self._record(job_id, name, status, now_iso(), result=result)
            last_error = None
            last_job_status = status
        except Exception as exc:  # noqa: BLE001
            self._record(job_id, name, "failed", now_iso(), error=str(exc))
            last_error = str(exc)
            last_job_status = "failed"
        finally:
            state_lock = None
            try:
                with self._lock:
                    if self._active == job_id:
                        state_lock = self._active_state_lock
                    try:
                        def mark_finished(state: dict[str, Any]) -> dict[str, Any]:
                            state["last_error"] = last_error
                            state["last_job_status"] = last_job_status
                            state["current_job"] = None
                            state["current_job_name"] = None
                            state["current_job_status"] = None
                            return state

                        update_state(self.state_dir, mark_finished)
                    finally:
                        if self._active == job_id:
                            self._active = None
                            self._active_name = None
                            self._active_cancel = None
                            self._active_cancel_hook = None
                            self._active_state_lock = None
            finally:
                if state_lock:
                    state_lock.release()
            if self._on_idle:
                try:
                    self._on_idle()
                except Exception:
                    return

    def _clear_active_job(self, job_id: str, *, release_state_lock: bool) -> None:
        state_lock = None
        with self._lock:
            if self._active != job_id:
                return
            if release_state_lock:
                state_lock = self._active_state_lock
            self._active = None
            self._active_name = None
            self._active_cancel = None
            self._active_cancel_hook = None
            self._active_state_lock = None
        if state_lock:
            state_lock.release()

    def _record(self, job_id: str, name: str, status: str, timestamp: str, **extra: Any) -> None:
        payload = {
            "id": job_id,
            "name": name,
            "status": status,
            "timestamp": timestamp,
        }
        for key, value in extra.items():
            if key == "result" and isinstance(value, dict):
                payload[key] = compact_run_payload(value)
            else:
                payload[key] = value
        append_jsonl(self.state_dir / "jobs.jsonl", payload)

    def _set_current_job(self, job_id: str, name: str, status: str) -> None:
        with self._lock:
            self._set_current_job_locked(job_id, name, status)

    def _set_current_job_if_active(self, job_id: str, name: str, status: str) -> None:
        with self._lock:
            if self._active != job_id:
                return
            self._set_current_job_locked(job_id, name, status)

    def _set_current_job_locked(self, job_id: str, name: str, status: str) -> None:
        def mark_running(state: dict[str, Any]) -> dict[str, Any]:
            state["current_job"] = job_id
            state["current_job_name"] = name
            state["current_job_status"] = status
            return state

        update_state(self.state_dir, mark_running)


class _StateDirJobLock:
    _LOCK_FILE_NAME = JOB_RUNNER_LOCK_FILE_NAME

    def __init__(self, path: Path):
        self._path = path
        self._released = False

    @classmethod
    def acquire(cls, state_dir: Path, job_id: str, job_name: str) -> "_StateDirJobLock":
        state_dir.mkdir(parents=True, exist_ok=True)
        path = state_dir / cls._LOCK_FILE_NAME
        payload = {
            "pid": os.getpid(),
            "job_id": job_id,
            "job_name": job_name,
            "created_at": now_iso(),
        }
        for _attempt in range(2):
            try:
                fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                existing = read_job_lock_payload(state_dir)
                if is_stale_process_payload(existing):
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        raise RuntimeError(cls._busy_message(existing)) from exc
                    continue
                raise RuntimeError(cls._busy_message(existing))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=True)
            return cls(path)
        raise RuntimeError(cls._busy_message(read_job_lock_payload(state_dir)))

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            self._path.unlink()
        except FileNotFoundError:
            return

    @staticmethod
    def _busy_message(payload: dict[str, Any]) -> str:
        job_id = str(payload.get("job_id") or "").strip()
        pid = payload.get("pid")
        details = f": {job_id}" if job_id else ""
        if isinstance(pid, int) and pid > 0:
            details = f"{details} pid={pid}"
        return f"job already running in state_dir{details}"


def _status_from_result(result: Any) -> str:
    if isinstance(result, dict):
        status = str(result.get("status") or "").strip().lower()
        if status in FINAL_JOB_STATUSES:
            return status
    return "success"
