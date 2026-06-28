from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .state import read_state
from .storage import db_path


def diagnostics_payload(state_dir: Path) -> dict[str, Any]:
    finder_dir = state_dir / "strategy-finder"
    logs_dir = finder_dir / "logs"
    backups_dir = state_dir.parent / "backups"
    sqlite = db_path(state_dir)
    return {
        "state": read_state(state_dir),
        "process": {
            "pid": os.getpid(),
            "rss_kb": _current_rss_kb(),
        },
        "system": {
            "loadavg": _loadavg(),
            "memory": _meminfo(),
        },
        "process_counts": _process_counts(),
        "files": {
            "sqlite": _file_size(sqlite),
            "sqlite_wal": _file_size(sqlite.with_name(sqlite.name + "-wal")),
            "sqlite_shm": _file_size(sqlite.with_name(sqlite.name + "-shm")),
            "logs_total": _dir_size(logs_dir),
            "backups_total": _dir_size(backups_dir),
        },
        "latest_logs": _latest_files(logs_dir, limit=10),
    }


def _current_rss_kb() -> int | None:
    status = Path("/proc/self/status")
    if status.is_file():
        try:
            for line in status.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    return int(parts[1]) if len(parts) >= 2 else None
        except (OSError, ValueError):
            return None
    return None


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


def _file_size(path: Path) -> int:
    try:
        return int(path.stat().st_size) if path.is_file() else 0
    except OSError:
        return 0


def _dir_size(path: Path) -> int:
    if not path.is_dir():
        return 0
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            total += _file_size(child)
    return total


def _latest_files(path: Path, limit: int) -> list[dict[str, Any]]:
    if not path.is_dir():
        return []
    files = []
    for child in path.iterdir():
        if not child.is_file():
            continue
        try:
            stat = child.stat()
        except OSError:
            continue
        files.append({"name": child.name, "size": stat.st_size, "mtime": stat.st_mtime})
    files.sort(key=lambda item: float(item["mtime"]), reverse=True)
    return files[:limit]
