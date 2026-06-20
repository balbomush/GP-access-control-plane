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
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self._lock = threading.Lock()
        self._active: str | None = None

    def start(self, name: str, func: Callable[[], Any]) -> Job:
        with self._lock:
            if self._active:
                raise RuntimeError(f"job already running: {self._active}")
            job_id = uuid.uuid4().hex[:12]
            self._active = job_id
        created_at = now_iso()
        job = Job(id=job_id, name=name, status="queued", created_at=created_at)
        self._record(job_id, name, "queued", created_at)
        thread = threading.Thread(target=self._run, args=(job_id, name, func), daemon=True)
        thread.start()
        return job

    def _run(self, job_id: str, name: str, func: Callable[[], Any]) -> None:
        self._record(job_id, name, "running", now_iso())
        state = read_state(self.state_dir)
        state["current_job"] = job_id
        write_state(self.state_dir, state)
        try:
            result = func()
            self._record(job_id, name, "success", now_iso(), result=result)
            state = read_state(self.state_dir)
            state["last_error"] = None
        except Exception as exc:  # noqa: BLE001
            self._record(job_id, name, "failed", now_iso(), error=str(exc))
            state = read_state(self.state_dir)
            state["last_error"] = str(exc)
        finally:
            state["current_job"] = None
            write_state(self.state_dir, state)
            with self._lock:
                if self._active == job_id:
                    self._active = None

    def _record(self, job_id: str, name: str, status: str, timestamp: str, **extra: Any) -> None:
        payload = {
            "id": job_id,
            "name": name,
            "status": status,
            "timestamp": timestamp,
        }
        payload.update(extra)
        append_jsonl(self.state_dir / "jobs.jsonl", payload)
