"""bc2_engine._writers — moved from strategy_finder.py / blockchecks_backend.py."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from gp_control_plane.bc2_engine._progress import _script_name_from_line
from gp_control_plane.engine_common._constants import METRICS_MAX_BYTES
from gp_control_plane.engine_common._options import _truthy
from gp_control_plane.engine_common._retention import _finder_dir
from gp_control_plane.engine_common._stdout_parse import _candidate_from_live_success_line, _live_attempt_line

def _rotate_metrics_file(path: Path) -> None:
    if not path.exists() or path.stat().st_size <= METRICS_MAX_BYTES:
        return
    rotated = path.with_suffix(path.suffix + ".1")
    try:
        if rotated.exists():
            rotated.unlink()
        path.replace(rotated)
    except OSError:
        return

def _runtime_file_sizes(state_dir: Path, run: dict[str, Any]) -> dict[str, int]:
    root = _finder_dir(state_dir)
    paths = {
        "stdout_log": Path(str(run.get("stdout_log") or "")),
        "stderr_log": Path(str(run.get("stderr_log") or "")),
        "progress_log": Path(str(run.get("progress_log") or "")),
        "sqlite": root / "state.sqlite3",
        "sqlite_wal": root / "state.sqlite3-wal",
    }
    return {name: _file_size(path) for name, path in paths.items()}

def _file_size(path: Path) -> int:
    try:
        return int(path.stat().st_size) if path.is_file() else 0
    except OSError:
        return 0

class _RotatingTextWriter:
    def __init__(self, path: Path, max_bytes: int):
        self._path = path
        self._max_bytes = max(1024, int(max_bytes))
        self._handle: Any | None = None

    def __enter__(self) -> "_RotatingTextWriter":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("w", encoding="utf-8")
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def write(self, text: str) -> None:
        if self._handle is None:
            raise ValueError("writer is closed")
        self._handle.write(text)
        self._handle.flush()
        self._rotate_if_needed()

    def flush(self) -> None:
        if self._handle is not None:
            self._handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def _rotate_if_needed(self) -> None:
        try:
            if not self._path.is_file() or self._path.stat().st_size <= self._max_bytes:
                return
        except OSError:
            return
        self.close()
        rotated = self._path.with_suffix(self._path.suffix + ".1")
        try:
            if rotated.exists():
                rotated.unlink()
            self._path.replace(rotated)
            self._handle = self._path.open("w", encoding="utf-8")
            self._handle.write(f"# log rotated, previous chunk: {rotated.name}\n")
            self._handle.flush()
        except OSError:
            self._handle = self._path.open("a", encoding="utf-8")

def _loadavg() -> list[float]:
    try:
        return [round(float(item), 2) for item in os.getloadavg()]
    except (AttributeError, OSError):
        return []

def _meminfo() -> dict[str, int]:
    path = Path("/proc/meminfo")
    if not path.is_file():
        return {}
    wanted = {"MemTotal", "MemAvailable", "MemFree", "Buffers", "Cached", "SwapTotal", "SwapFree"}
    result: dict[str, int] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            key, _, raw_value = line.partition(":")
            if key not in wanted:
                continue
            parts = raw_value.strip().split()
            if parts:
                result[key] = int(parts[0])
    except (OSError, ValueError):
        return {}
    return result

def _read_cpu_totals() -> tuple[int, int, int] | None:
    path = Path("/proc/stat")
    if not path.is_file():
        return None
    try:
        first = path.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
        parts = [int(item) for item in first.split()[1:]]
    except (OSError, IndexError, ValueError):
        return None
    if len(parts) < 5:
        return None
    idle = parts[3] + parts[4]
    iowait = parts[4]
    return (sum(parts), idle, iowait)

def _process_counts() -> dict[str, int]:
    proc = Path("/proc")
    if not proc.is_dir():
        return {"curl": 0, "nfqws2": 0, "blockcheck2": 0}
    counts = {"curl": 0, "nfqws2": 0, "blockcheck2": 0}
    for child in proc.iterdir():
        if not child.name.isdigit():
            continue
        try:
            cmdline = (child / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="ignore")
        except OSError:
            continue
        if not cmdline:
            continue
        if "curl" in cmdline:
            counts["curl"] += 1
        if "nfqws2" in cmdline:
            counts["nfqws2"] += 1
        if "blockcheck2" in cmdline:
            counts["blockcheck2"] += 1
    return counts

class _CompactStdoutWriter:
    def __init__(self, handle: Any):
        self._handle = handle
        self._pending_attempt = ""
        self._attempt_lines = 0
        self._summary_lines = 0
        self._section = ""

    def write(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return
        if stripped in {"* SUMMARY", "* COMMON"}:
            self._flush_attempt_counter()
            self._section = stripped.removeprefix("* ").lower()
            self._write(line)
            return
        script = _script_name_from_line(stripped)
        if script:
            self._flush_attempt_counter()
            self._section = ""
            self._write(line)
            return
        attempt = _live_attempt_line(stripped)
        if attempt:
            self._pending_attempt = attempt
            self._attempt_lines += 1
            if self._attempt_lines % 1000 == 0:
                self._write(f"# compact-log: skipped {self._attempt_lines} attempt lines\n")
            return
        if stripped == "!!!!! AVAILABLE !!!!!":
            if self._pending_attempt:
                self._write(self._pending_attempt + "\n")
            self._write(line)
            self._pending_attempt = ""
            return
        if _candidate_from_live_success_line(stripped):
            self._write(line)
            return
        if stripped.startswith("UNAVAILABLE") or stripped.startswith("FAILED"):
            self._pending_attempt = ""
            return
        if self._section in {"summary", "common"}:
            self._summary_lines += 1
            if self._summary_lines % 1000 == 0:
                self._write(f"# compact-log: skipped {self._summary_lines} summary/common lines\n")
            return
        if stripped.startswith("* "):
            self._write(line)

    def close(self) -> None:
        self._flush_attempt_counter()

    def _flush_attempt_counter(self) -> None:
        if self._attempt_lines:
            self._write(f"# compact-log: total attempt lines skipped {self._attempt_lines}\n")
            self._attempt_lines = 0
        self._pending_attempt = ""

    def _write(self, line: str) -> None:
        self._handle.write(line)
        self._handle.flush()

def _stdout_log_mode(env: dict[str, str]) -> str:
    if _truthy(env.get("GP_DEBUG_STDOUT"), default=False):
        return "debug"
    if _truthy(env.get("GP_COMPACT_STDOUT"), default=False):
        return "compact"
    return "raw"

def _set_debug_stdout_env(env: dict[str, str], debug_stdout: bool | None) -> None:
    if debug_stdout is True:
        env["GP_DEBUG_STDOUT"] = "1"
    elif debug_stdout is False:
        env.pop("GP_DEBUG_STDOUT", None)
