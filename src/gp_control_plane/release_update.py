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
UPDATE_LOG_TAIL_LINES = 80
UPDATE_LOG_TAIL_BYTES = 32_000


def release_update_plan(
    state_dir: Path,
    *,
    channel: str,
    current_version: str = __version__,
    fetcher: Callable[[], str] | None = None,
    tag_fetcher: Callable[[], str] | None = None,
) -> dict[str, Any]:
    release = release_channel_info(current_version=current_version, channel=channel, fetcher=fetcher, tag_fetcher=tag_fetcher)
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
            "verify installed ref/version after installer finishes",
            "if verification fails, restore the pre-update backup from Backups",
            "service will come back on the selected release",
        ],
    }


def queue_release_update(
    state_dir: Path,
    *,
    channel: str,
    install_dir: Path | None = None,
    current_version: str = __version__,
    fetcher: Callable[[], str] | None = None,
    tag_fetcher: Callable[[], str] | None = None,
    helper_runner: HelperRunner | None = None,
) -> dict[str, Any]:
    plan = release_update_plan(
        state_dir,
        channel=channel,
        current_version=current_version,
        fetcher=fetcher,
        tag_fetcher=tag_fetcher,
    )
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
    helper = _parse_key_value_lines(result.stdout)

    queued_at = now_iso()
    payload = {
        "queued": True,
        "status": "queued",
        "queued_at": queued_at,
        "release": release,
        "snapshot": snapshot.get("snapshot"),
        "helper_stdout": result.stdout.strip(),
        "helper": helper,
        "unit": helper.get("unit", ""),
        "log_path": helper.get("log", ""),
        "target_ref": tag,
        "rollback_instruction": "If update verification fails, open Backups and restore the pre-update snapshot.",
        "steps": plan["steps"],
    }
    state = read_state(state_dir)
    state["release_update"] = payload
    write_state(state_dir, state)
    append_jsonl(state_dir / "release-updates.jsonl", payload)
    return payload


def release_update_status(
    state_dir: Path,
    *,
    current_version: str = __version__,
) -> dict[str, Any]:
    state = read_state(state_dir)
    raw = state.get("release_update")
    if not isinstance(raw, dict):
        return {}
    payload = dict(raw)
    helper = payload.get("helper") if isinstance(payload.get("helper"), dict) else {}
    log_path = str(payload.get("log_path") or helper.get("log") or "")
    log_tail = ""
    log_values: dict[str, str] = {}
    if log_path:
        path = Path(log_path)
        if path.is_file():
            log_tail = _tail_text(path)
            log_values = _parse_key_value_lines(log_tail)
            payload["log_tail"] = log_tail
    status = str(log_values.get("status") or payload.get("status") or "queued")
    release = payload.get("release") if isinstance(payload.get("release"), dict) else {}
    target = str(payload.get("target_ref") or release.get("available_version") or "")
    installed_version = str(log_values.get("installed_version") or "")
    installed_ref = str(log_values.get("installed_ref") or "")
    if status == "queued" and log_tail:
        status = "running"
    if status not in {"failed", "success"} and target and _version_matches(target, current_version):
        status = "success"
        installed_version = installed_version or current_version
    payload["status"] = status
    payload["log_path"] = log_path
    payload["installed_version"] = installed_version
    payload["installed_ref"] = installed_ref
    payload["verified"] = bool(status == "success" and (installed_ref or installed_version))
    payload["error"] = str(log_values.get("error") or payload.get("error") or "")
    return payload


def _parse_key_value_lines(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        clean_key = key.strip()
        if clean_key:
            result[clean_key] = value.strip()
    return result


def _tail_text(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if len(data) > UPDATE_LOG_TAIL_BYTES:
        data = data[-UPDATE_LOG_TAIL_BYTES:]
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) > UPDATE_LOG_TAIL_LINES:
        lines = lines[-UPDATE_LOG_TAIL_LINES:]
    return "\n".join(lines)


def _version_matches(target: str, current: str) -> bool:
    return str(target or "").lstrip("v") == str(current or "").lstrip("v")
