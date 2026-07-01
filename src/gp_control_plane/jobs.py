from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .state import append_jsonl, now_iso, read_state, write_state


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
            self._active = job_id
            self._active_name = name
            self._active_cancel = cancel_event
            self._active_cancel_hook = cancel_hook
        created_at = now_iso()
        job = Job(id=job_id, name=name, status="queued", created_at=created_at)
        self._record(job_id, name, "queued", created_at)
        self._set_current_job(job_id, name, "queued")
        thread = threading.Thread(target=self._run, args=(job_id, name, func, cancel_event), daemon=True)
        thread.start()
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
            status = "stopped" if isinstance(result, dict) and result.get("status") == "stopped" else "success"
            self._record(job_id, name, status, now_iso(), result=result)
            last_error = None
            last_job_status = status
        except Exception as exc:  # noqa: BLE001
            self._record(job_id, name, "failed", now_iso(), error=str(exc))
            last_error = str(exc)
            last_job_status = "failed"
        finally:
            with self._lock:
                state = read_state(self.state_dir)
                state["last_error"] = last_error
                state["last_job_status"] = last_job_status
                state["current_job"] = None
                state["current_job_name"] = None
                state["current_job_status"] = None
                write_state(self.state_dir, state)
                if self._active == job_id:
                    self._active = None
                    self._active_name = None
                    self._active_cancel = None
                    self._active_cancel_hook = None
            if self._on_idle:
                try:
                    self._on_idle()
                except Exception:
                    return

    def _record(self, job_id: str, name: str, status: str, timestamp: str, **extra: Any) -> None:
        payload = {
            "id": job_id,
            "name": name,
            "status": status,
            "timestamp": timestamp,
        }
        payload.update(extra)
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
        state = read_state(self.state_dir)
        state["current_job"] = job_id
        state["current_job_name"] = name
        state["current_job_status"] = status
        write_state(self.state_dir, state)
