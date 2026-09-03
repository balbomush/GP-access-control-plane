"""Dual discovery engines: blockcheck2.sh (default) and blockcheckS (`bs`)."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

ENGINE_BLOCKCHECK2 = "blockcheck2"
ENGINE_BLOCKCHECKS = "blockchecks"
ENGINES = frozenset({ENGINE_BLOCKCHECK2, ENGINE_BLOCKCHECKS})

SCAN_LEVEL_TO_BS = {
    "quick": "single",
    "standard": "fast",
    "force": "full",
}

JOB_NAMES = {
    (ENGINE_BLOCKCHECK2, "standard"): "zapret-standard-discovery",
    (ENGINE_BLOCKCHECK2, "multi"): "zapret-multi-domain-discovery",
    (ENGINE_BLOCKCHECKS, "standard"): "blockchecks-standard-discovery",
    (ENGINE_BLOCKCHECKS, "multi"): "blockchecks-multi-domain-discovery",
}

DEFAULT_BS_JOB_CAP = 400
PROGRESS_LINE = r"\[(\d+)/(\d+)\] pass=(\d+)"


def normalize_engine(value: Any) -> str:
    raw = str(value or ENGINE_BLOCKCHECK2).strip().lower().replace("-", "_")
    aliases = {
        "blockcheck2": ENGINE_BLOCKCHECK2,
        "blockcheck": ENGINE_BLOCKCHECK2,
        "zapret2": ENGINE_BLOCKCHECK2,
        "zapret": ENGINE_BLOCKCHECK2,
        "blockchecks": ENGINE_BLOCKCHECKS,
        "blockcheck_s": ENGINE_BLOCKCHECKS,
        "bs": ENGINE_BLOCKCHECKS,
    }
    engine = aliases.get(raw, ENGINE_BLOCKCHECK2)
    return engine if engine in ENGINES else ENGINE_BLOCKCHECK2


def job_mode(mode: str) -> str:
    raw = str(mode or "standard").strip().lower().replace("-", "_")
    return "multi" if raw in {"multi_domain", "common_strategy", "multi"} else "standard"


def discovery_job_name(engine: str, mode: str) -> str:
    return JOB_NAMES[(normalize_engine(engine), job_mode(mode))]


def is_blockchecks_job(name: str) -> bool:
    return str(name or "").startswith("blockchecks-")


def scan_level_to_bs(scan_level: str) -> str:
    key = str(scan_level or "standard").strip().lower()
    return SCAN_LEVEL_TO_BS.get(key, "fast")


def resolve_bs_binary() -> str:
    env_path = str(os.environ.get("BLOCKCHECKS_BS") or "").strip()
    candidates = [
        env_path,
        shutil.which("bs") or "",
        str(Path.home() / "workspace" / "blockcheckS" / ".venv" / "bin" / "bs"),
        "/usr/local/libexec/gp-control-plane/bs",
    ]
    for item in candidates:
        if item and Path(item).is_file() and os.access(item, os.X_OK):
            return item
    raise RuntimeError("blockcheckS `bs` binary not found (set BLOCKCHECKS_BS or install bs on PATH)")


def resolve_bc_nfconf() -> str:
    env_path = str(os.environ.get("BLOCKCHECKS_NFCONF") or "").strip()
    candidates = [
        env_path,
        shutil.which("bc-nfconf") or "",
        str(Path.home() / "workspace" / "blockcheckS" / ".venv" / "bin" / "bc-nfconf"),
    ]
    for item in candidates:
        if item and Path(item).is_file() and os.access(item, os.X_OK):
            return item
    raise RuntimeError("bc-nfconf not found (set BLOCKCHECKS_NFCONF)")


def blockchecks_state_dir() -> Path:
    override = str(os.environ.get("BLOCKCHECKS_STATE_HOME") or "").strip()
    if override:
        # BS treats BLOCKCHECKS_*_HOME as the XDG root and appends the
        # application dir (/blockcheckS); mirror that to avoid the
        # "blockcheckS/blockcheckS" double suffix.
        return Path(override).expanduser() / "blockcheckS"
    xdg = str(os.environ.get("XDG_STATE_HOME") or "").strip()
    root = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return root / "blockcheckS"


def bs_run_env() -> dict[str, str]:
    """Child env for `bs` subprocesses: inherit + zapret handoff.

    XDG is intentionally NOT overridden here: GP runs `bs` as the same user
    and relies on the shared run.lock in the default XDG state dir. Only the
    zapret tree location is propagated (when GP knows it via ZAPRET_DIR /
    BLOCKCHECKS_ZAPRET2) so blobs/lua/nfqws2 resolve consistently.
    """
    env = dict(os.environ)
    zapret = (env.get("ZAPRET_DIR") or env.get("BLOCKCHECKS_ZAPRET2") or "").strip()
    if zapret:
        env.setdefault("BLOCKCHECKS_ZAPRET2", zapret)
        env.setdefault("ZAPRET2_ROOT", zapret)
        if not env.get("BLOCKCHECKS_NFQWS2"):
            nfq = Path(zapret) / "nfq2" / "nfqws2"
            if nfq.is_file():
                env["BLOCKCHECKS_NFQWS2"] = str(nfq)
    return env


def campaign_lock_info() -> dict[str, Any] | None:
    lock = blockchecks_state_dir() / "run.lock"
    if not lock.is_file():
        return None
    try:
        payload = json.loads(lock.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"path": str(lock), "pid": 0, "command": "unknown"}
    if not isinstance(payload, dict):
        return {"path": str(lock), "pid": 0, "command": "unknown"}
    pid = int(payload.get("pid") or 0)
    if pid > 0:
        try:
            os.kill(pid, 0)
        except OSError:
            return None
    return {
        "path": str(lock),
        "pid": pid,
        "command": str(payload.get("command") or ""),
        "argv": payload.get("argv") or [],
    }


def campaign_lock_busy_message() -> str | None:
    info = campaign_lock_info()
    if not info:
        return None
    command = info.get("command") or "campaign"
    return (
        f"blockcheckS run.lock is held by {command} (pid {info.get('pid')}); "
        "stop that run before starting GP discovery"
    )


def build_bs_scan_argv(
    *,
    domains: list[str],
    scan_level: str,
    repeats: int,
    repeat_parallel: bool,
    curl_max_time: int,
    timeout_seconds: int,
    curl_parallelism: int,
    skip_dnscheck: bool,
    db_path: str | Path | None = None,
    strategy_preset: str | None = None,
) -> list[str]:
    argv = [
        resolve_bs_binary(),
        "scan",
        "--scan-level",
        scan_level_to_bs(scan_level),
        "--repeats",
        str(max(1, min(10, int(repeats)))),
        "--timeout",
        str(max(1, int(curl_max_time))),
        "--parallel",
        str(max(1, min(4, int(curl_parallelism)))),
    ]
    if repeat_parallel:
        argv.append("--parallel-repeats")
    if skip_dnscheck:
        argv.append("--skip-dns-audit")
    if strategy_preset:
        argv.extend(["-M", strategy_preset])
    argv.extend(["--tcp-sources", "custom,configs"])
    argv.extend(["--curl-parallel", str(max(1, int(curl_parallelism)))])
    if db_path:
        argv.extend(["--db", str(db_path)])
    if timeout_seconds > 0:
        argv.extend(["--max-timem", str(max(1, int(timeout_seconds) // 60 or 1))])
    else:
        argv.extend(["--max", str(DEFAULT_BS_JOB_CAP)])
    for domain in domains:
        argv.extend(["-d", domain])
    return argv


def check_blockchecks_install() -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    nfqws2 = shutil.which("nfqws2") or "/opt/zapret2/nfq2/nfqws2"
    nfq_ok = bool(nfqws2) and Path(str(nfqws2)).is_file()
    checks.append(
        {
            "name": "nfqws2",
            "status": "ok" if nfq_ok else "error",
            "message": str(nfqws2) if nfq_ok else "nfqws2 not found",
        }
    )
    try:
        bs_path = resolve_bs_binary()
        checks.append({"name": "bs", "status": "ok", "message": bs_path})
    except RuntimeError as exc:
        checks.append({"name": "bs", "status": "error", "message": str(exc)})
    busy = campaign_lock_busy_message()
    if busy:
        checks.append({"name": "run_lock", "status": "error", "message": busy})
    ready = all(item["status"] == "ok" for item in checks)
    return {"ready": ready, "checks": checks}
