from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .backups import create_snapshot
from .releases import release_channel_info
from .state import append_jsonl, now_iso, read_state, write_state
from .zapret2 import run_root_helper_command


HelperRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def release_update_plan(
    state_dir: Path,
    *,
    channel: str,
    current_version: str = __version__,
    fetcher: Callable[[], str] | None = None,
) -> dict[str, Any]:
    release = release_channel_info(current_version=current_version, channel=channel, fetcher=fetcher)
    state = read_state(state_dir)
    active_job = str(state.get("current_job") or "")
    reason = ""
    if active_job:
        reason = "job is running"
    elif not release.get("checked"):
        reason = str(release.get("error") or "release check failed")
    elif not release.get("update_available"):
        reason = "no update available"
    return {
        "release": release,
        "can_update": not reason,
        "blocked_reason": reason,
        "active_job": active_job,
        "steps": [
            "check selected release channel",
            "create backup before code change",
            "queue alpha installer through root-helper",
            "service will come back on the selected alpha release",
        ],
    }


def queue_release_update(
    state_dir: Path,
    *,
    channel: str,
    install_dir: Path | None = None,
    current_version: str = __version__,
    fetcher: Callable[[], str] | None = None,
    helper_runner: HelperRunner | None = None,
) -> dict[str, Any]:
    plan = release_update_plan(state_dir, channel=channel, current_version=current_version, fetcher=fetcher)
    if not plan["can_update"]:
        raise RuntimeError(str(plan["blocked_reason"] or "update is not allowed"))
    release = plan["release"]
    tag = str(release.get("available_version") or "").strip()
    if not tag:
        raise RuntimeError("release tag is missing")

    snapshot = create_snapshot(state_dir)
    root = (install_dir or Path.cwd()).resolve()
    runner = helper_runner or run_root_helper_command
    result = runner(["queue-update", str(root), tag])
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "root-helper update queue failed").strip())

    queued_at = now_iso()
    payload = {
        "queued": True,
        "queued_at": queued_at,
        "release": release,
        "snapshot": snapshot.get("snapshot"),
        "helper_stdout": result.stdout.strip(),
        "steps": plan["steps"],
    }
    state = read_state(state_dir)
    state["release_update"] = payload
    write_state(state_dir, state)
    append_jsonl(state_dir / "release-updates.jsonl", payload)
    return payload
