"""Small, Linux-specific helpers for identifying a process without PID reuse.

The authoritative registry is deliberately maintained by gp-root-helper under
``/run``.  This module never persists privileged process metadata in the
service user's state directory.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    pgid: int
    start_time: str


def validate_run_id(run_id: str) -> str:
    value = str(run_id).strip()
    if not RUN_ID_RE.fullmatch(value):
        raise ValueError("invalid process registry run_id")
    return value


def process_start_time(pid: int) -> str:
    """Return Linux /proc stat field 22, safely handling spaces in comm."""
    if pid <= 0:
        raise ValueError("pid must be positive")
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    try:
        fields = stat.rsplit(") ", 1)[1].split()
        marker = fields[19]  # field 22 after state (field 3) begins this list
    except (IndexError, ValueError) as exc:
        raise ValueError(f"cannot read process identity for pid {pid}") from exc
    if not marker.isdigit():
        raise ValueError(f"invalid process identity for pid {pid}")
    return marker
