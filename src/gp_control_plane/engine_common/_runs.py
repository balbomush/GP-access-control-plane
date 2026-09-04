"""engine_common._runs — moved from strategy_finder.py / blockchecks_backend.py."""
from __future__ import annotations

from pathlib import Path
from gp_control_plane.state import now_iso
from gp_control_plane.storage import append_run, count_latest_run_payloads, read_latest_run_payloads
from typing import Any
from gp_control_plane.engine_common._options import _bounded_int
from gp_control_plane.engine_common._retention import _finder_dir

def read_runs(state_dir: Path, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    return [_compact_run(run) for run in read_latest_run_payloads(state_dir, limit=limit, offset=offset)]

def read_runs_page(state_dir: Path, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    limit = _bounded_int(limit, default=50, minimum=1, maximum=1000)
    offset = max(0, _bounded_int(offset, default=0, minimum=0, maximum=10_000_000))
    runs = [_compact_run(run) for run in read_latest_run_payloads(state_dir, limit=limit, offset=offset)]
    total = count_latest_run_payloads(state_dir)
    return {
        "runs": runs,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(runs) < total,
    }

def _compact_run(run: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in run.items()
        if key
        not in {
            "summary",
            "common",
            "live_summary",
            "results",
            "common_results",
            "direct_available",
            "not_working",
        }
    }

def close_stale_running_runs(state_dir: Path) -> int:
    from gp_control_plane.engine_common._logtail import _read_progress_log
    root = _finder_dir(state_dir)
    runs = read_runs(state_dir, limit=200)
    latest_by_id: dict[str, dict[str, Any]] = {}
    for run in runs:
        run_id = str(run.get("id") or "")
        if run_id:
            latest_by_id[run_id] = run
    closed = 0
    for run in latest_by_id.values():
        if str(run.get("status") or "") not in {"queued", "running", "stopping"}:
            continue
        progress = run.get("progress")
        if not isinstance(progress, dict):
            progress = _read_progress_log(run)
        update = {
            "id": run.get("id"),
            "kind": run.get("kind"),
            "candidate_id": run.get("candidate_id", ""),
            "status": "stopped",
            "timestamp": run.get("timestamp") or now_iso(),
            "started_at": run.get("started_at") or run.get("timestamp") or "",
            "completed_at": now_iso(),
            "domains": run.get("domains") or [],
            "returncode": run.get("returncode"),
            "stdout_log": run.get("stdout_log", ""),
            "stderr_log": run.get("stderr_log", ""),
            "progress_log": run.get("progress_log", ""),
            "metrics_log": run.get("metrics_log", ""),
            "summary_fallback_log": run.get("summary_fallback_log", ""),
            "candidate_count": int(run.get("candidate_count") or 0),
            "common_candidate_count": int(run.get("common_candidate_count") or 0),
            "total_candidates": int(run.get("total_candidates") or 0),
            "phase": run.get("phase") or (progress.get("phase") if isinstance(progress, dict) else ""),
            "stopped": True,
            "interrupted": True,
            "interrupted_reason": "web service stopped while run was marked active",
            "test": run.get("test", "standard"),
            "attempt_plan": run.get("attempt_plan") or {},
        }
        if isinstance(progress, dict):
            update["progress"] = progress
        for key in (
            "enable_http",
            "enable_tls",
            "enable_tls13",
            "enable_quic",
            "scan_level",
            "repeats",
            "repeat_parallel",
            "skip_dnscheck",
            "skip_ipblock",
            "curl_parallelism",
            "discovery_options",
        ):
            if key in run:
                update[key] = run[key]
        append_run(state_dir, update)
        closed += 1
    return closed
