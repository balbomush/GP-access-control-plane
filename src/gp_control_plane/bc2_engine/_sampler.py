"""bc2_engine._sampler — runtime metrics sampler used by the live recorder."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from gp_control_plane.bc2_engine._writers import (
    _loadavg,
    _meminfo,
    _process_counts,
    _read_cpu_totals,
    _rotate_metrics_file,
    _runtime_file_sizes,
)
from gp_control_plane.engine_common._constants import METRICS_INTERVAL_SECONDS
from gp_control_plane.state import append_jsonl, now_iso


class _RuntimeMetricsSampler:
    def __init__(self, state_dir: Path, run: dict[str, Any]):
        self._state_dir = state_dir
        self._run = run
        metrics_log = str(run.get("metrics_log") or "")
        self._path = Path(metrics_log) if metrics_log else None
        self._last_written_at = 0.0
        self._last_cpu: tuple[int, int, int] | None = None

    def maybe_write(self, progress: dict[str, Any]) -> None:
        if not self._path:
            return
        now = time.monotonic()
        if now - self._last_written_at < METRICS_INTERVAL_SECONDS:
            return
        self._last_written_at = now
        payload = {
            "timestamp": now_iso(),
            "run_id": self._run.get("id"),
            "phase": progress.get("phase"),
            "phase_label": progress.get("phase_label"),
            "current_script": progress.get("current_script"),
            "attempted": progress.get("attempted"),
            "attempt_total": progress.get("attempt_total"),
            "remaining_attempts": progress.get("remaining_attempts"),
            "successful": progress.get("successful"),
            "eta_seconds": progress.get("eta_seconds"),
            "eta_status": progress.get("eta_status"),
            "progress_status": progress.get("progress_status"),
            "processes": _process_counts(),
            "system": self._system_metrics(),
            "files": _runtime_file_sizes(self._state_dir, self._run),
        }
        try:
            _rotate_metrics_file(self._path)
            append_jsonl(self._path, payload)
        except OSError:
            return

    def _system_metrics(self) -> dict[str, Any]:
        return {
            "loadavg": _loadavg(),
            "cpu_percent": self._cpu_percent(),
            "memory": _meminfo(),
        }

    def _cpu_percent(self) -> dict[str, float] | None:
        current = _read_cpu_totals()
        if current is None:
            return None
        previous = self._last_cpu
        self._last_cpu = current
        if previous is None:
            return None
        total, idle, iowait = current
        prev_total, prev_idle, prev_iowait = previous
        delta_total = total - prev_total
        if delta_total <= 0:
            return None
        busy = max(0, delta_total - (idle - prev_idle))
        iowait_delta = max(0, iowait - prev_iowait)
        return {
            "busy": round((busy / delta_total) * 100.0, 1),
            "iowait": round((iowait_delta / delta_total) * 100.0, 1),
        }
