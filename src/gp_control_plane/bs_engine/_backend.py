"""bs_engine._backend — moved from strategy_finder.py / blockchecks_backend.py."""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from gp_control_plane.bs_engine._harvest import _harvest_pairs, _harvest_passes, _harvest_udp
from gp_control_plane.discovery_engine import (
    DEFAULT_BS_JOB_CAP,
    DOMAIN_ARGV_THRESHOLD,
    PROGRESS_LINE,
    blockchecks_state_dir,
    bs_run_env,
    build_bs_scan_argv,
    campaign_lock_busy_message,
    resolve_bs_binary,
)
from gp_control_plane.engine_common._constants import PHASE_COMPLETE, PHASE_DISCOVERY
from gp_control_plane.engine_common._options import (
    _domain_validation_run_fields,
    validate_domain_inputs,
)
from gp_control_plane.engine_common._retention import _cleanup_old_strategy_logs, _finder_dir
from gp_control_plane.engine_common._runmeta import _discovery_run_id
from gp_control_plane.state import now_iso
from gp_control_plane.storage import append_run

_PROGRESS_RE = re.compile(PROGRESS_LINE)

AQ_JOBS_RE = re.compile(r"AQ pending jobs:\s+(\d+)")

GEN_TCP_RE = re.compile(r"Generated:\s+(\d+)\s+TCP")


log = logging.getLogger(__name__)
def stop_blockchecks() -> None:
    try:
        bs = resolve_bs_binary()
    except RuntimeError:
        return
    subprocess.run([bs, "stop", "--wait", "120"], check=False, timeout=60)

def run_blockchecks_discovery(
    domains: list[str],
    state_dir: Path,
    timeout_seconds: int,
    include_quic: bool = True,
    enable_http: bool = False,
    enable_tls12: bool = True,
    enable_tls13: bool = False,
    enable_ipv6: bool = False,
    scan_level: str = "standard",
    repeats: int = 1,
    repeat_parallel: bool = False,
    skip_dnscheck: bool = True,
    skip_ipblock: bool = True,
    curl_max_time: int = 2,
    curl_max_time_quic: int = 2,
    curl_max_time_doh: int = 2,
    curl_parallelism: int = 4,
    debug_stdout: bool | None = None,
    stop_event: threading.Event | None = None,
    run_id: str | None = None,
    kind: str = "standard-discovery",
    strategy_preset: str = "",
    repeats_mode: str = "fast",
    adaptive: bool = True,
    pair_mode: bool = False,
) -> dict[str, Any]:
    del enable_http, enable_ipv6, curl_max_time_quic, curl_max_time_doh, include_quic
    protocol = "tls13" if bool(enable_tls13) and not bool(enable_tls12) else "tls12"
    busy = campaign_lock_busy_message()
    if busy:
        raise RuntimeError(busy)
    domain_validation = validate_domain_inputs(domains, default_to_critical=True)
    clean_domains = list(domain_validation["domains"])
    if not clean_domains:
        raise ValueError("no valid domains to check")
    run_id = _discovery_run_id(run_id)
    bs_state = blockchecks_state_dir()
    bs_runs = bs_state / "bs-runs"
    bs_runs.mkdir(parents=True, exist_ok=True)
    run_db = bs_runs / f"{run_id}.db"
    domains_file_arg: Path | None = None
    if len(clean_domains) > DOMAIN_ARGV_THRESHOLD:
        domains_file_arg = Path(state_dir) / f"bs-domains-{run_id}.txt"
        domains_file_arg.write_text("\n".join(clean_domains) + "\n", encoding="utf-8")
    argv = build_bs_scan_argv(
        domains=clean_domains,
        scan_level=scan_level,
        repeats=repeats,
        repeat_parallel=repeat_parallel,
        curl_max_time=curl_max_time,
        timeout_seconds=timeout_seconds,
        curl_parallelism=curl_parallelism,
        skip_dnscheck=skip_dnscheck,
        db_path=run_db,
        strategy_preset=strategy_preset or None,
        repeats_mode=repeats_mode,
        adaptive=adaptive,
        debug=bool(debug_stdout),
        protocol=protocol,
        skip_ipblock=skip_ipblock,
        domains_file=domains_file_arg,
        pair_mode=pair_mode,
    )
    logs = _finder_dir(state_dir) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    _cleanup_old_strategy_logs(logs)
    stdout_log = logs / f"{run_id}.{kind}.stdout.log"
    stderr_log = logs / f"{run_id}.{kind}.stderr.log"
    progress_log = logs / f"{run_id}.{kind}.progress.json"
    started_at = now_iso()
    started = {
        "id": run_id,
        "kind": kind,
        "status": "running",
        "timestamp": started_at,
        "started_at": started_at,
        "domains": clean_domains,
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "progress_log": str(progress_log),
        "phase": PHASE_DISCOVERY,
        "discovery_engine": "blockchecks",
        "scan_level": scan_level,
        "repeats": repeats,
        "timeout_seconds": timeout_seconds,
        "bs_argv": argv[1:],
        "bs_db": str(run_db),
        "bs_job_cap": DEFAULT_BS_JOB_CAP if timeout_seconds <= 0 else None,
        **_domain_validation_run_fields(domain_validation),
        "discovery_options": {
            "scan_level": scan_level,
            "repeats": repeats,
            "repeat_parallel": repeat_parallel,
            "repeats_mode": repeats_mode,
            "skip_dnscheck": skip_dnscheck,
            "skip_ipblock": skip_ipblock,
            "curl_max_time": curl_max_time,
            "strategy_preset": strategy_preset,
            "adaptive": adaptive,
            "protocol": protocol,
            "pair_mode": pair_mode,
        },
    }
    append_run(state_dir, started)
    process_started = time.monotonic()
    progress_state = {
        "attempt_total": 0,
        "strategies_total": 0,
        "last_db_poll": 0.0,
        "script": f"bs {'pair' if pair_mode else 'scan'}{(' -M ' + strategy_preset) if strategy_preset else ''}",
    }
    _write_progress(
        progress_log,
        phase=PHASE_DISCOVERY,
        processed=0,
        total=0,
        passed=0,
        started=process_started,
        current_script=progress_state["script"],
    )
    harvested: set[tuple[str, str, str]] = set()

    def _refresh_progress(force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - progress_state["last_db_poll"]) < 1.5:
            return
        progress_state["last_db_poll"] = now
        counts = _bs_progress_db_counts(run_db)
        total = progress_state["attempt_total"] or counts["attempts"]
        _write_progress(
            progress_log,
            phase=PHASE_DISCOVERY,
            processed=counts["attempts"],
            total=total,
            passed=counts["working"],
            started=process_started,
            strategies_total=progress_state["strategies_total"] or counts["strategies"],
            strategies_checked=counts["strategies"],
            current_script=progress_state["script"],
        )

    process = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=bs_run_env(),
        start_new_session=True,
    )
    stopped = False
    timed_out = False
    deadline = time.monotonic() + timeout_seconds if timeout_seconds > 0 else None
    stdout_handle = stdout_log.open("w", encoding="utf-8")
    stderr_handle = stderr_log.open("w", encoding="utf-8")
    try:
        stdout_handle.write(" ".join(argv) + "\n")
        stdout_handle.flush()
        assert process.stdout is not None
        for line in process.stdout:
            stdout_handle.write(line)
            stdout_handle.flush()
            aq = AQ_JOBS_RE.search(line)
            if aq:
                progress_state["attempt_total"] = int(aq.group(1))
            gen = GEN_TCP_RE.search(line)
            if gen:
                progress_state["strategies_total"] = int(gen.group(1))
            _harvest_passes(state_dir, run_id, kind, harvested, run_db)
            _refresh_progress()
            if stop_event is not None and stop_event.is_set():
                stopped = True
                stop_blockchecks()
                process.terminate()
                break
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                stop_blockchecks()
                process.terminate()
                break
    finally:
        stdout_handle.close()
        stderr_handle.close()
        if process.poll() is None:
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    os.killpg(process.pid, 9)
                except (OSError, ProcessLookupError):
                    log.debug("failed terminating blockchecks process during stop cleanup")
                    pass
                process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
        if domains_file_arg is not None:
            domains_file_arg.unlink(missing_ok=True)
    _harvest_passes(state_dir, run_id, kind, harvested, run_db)
    if pair_mode and clean_domains:
        _harvest_udp(state_dir, run_id, kind, harvested, run_db, clean_domains[0])
        _harvest_pairs(state_dir, run_id, run_db, clean_domains[0])
    final_counts = _bs_progress_db_counts(run_db)
    status = "success"
    if stopped:
        status = "stopped"
    elif timed_out:
        status = "timeout"
    elif process.returncode not in {0, None}:
        status = "error"
    completed_at = now_iso()
    run = {
        **started,
        "status": status,
        "completed_at": completed_at,
        "returncode": process.returncode,
        "timed_out": timed_out,
        "stopped": stopped,
        "candidate_count": len(harvested),
        "phase": PHASE_COMPLETE,
    }
    append_run(state_dir, run)
    _write_progress(
        progress_log,
        phase=PHASE_COMPLETE,
        processed=final_counts["attempts"],
        total=final_counts["attempts"] or progress_state["attempt_total"] or len(harvested),
        passed=len(harvested),
        started=process_started,
        strategies_total=progress_state["strategies_total"] or final_counts["strategies"],
        strategies_checked=final_counts["strategies"],
        current_script=progress_state["script"],
        percent=100,
    )
    return run

def _write_progress(
    path: Path,
    *,
    phase: str,
    processed: int,
    total: int,
    passed: int,
    started: float | None,
    strategies_total: int = 0,
    strategies_checked: int | None = None,
    current_script: str = "",
    elapsed_seconds: float | None = None,
    eta_seconds: float | None = None,
    percent: float | None = None,
) -> None:
    progress_status = "complete" if phase == PHASE_COMPLETE else "running"
    phase_label = "завершено" if phase == PHASE_COMPLETE else "подбор стратегий"
    if elapsed_seconds is None:
        elapsed_seconds = 0.0 if started is None else max(0, round(time.monotonic() - started, 1))
    if percent is None and total > 0:
        percent = round(min(100.0, (processed / float(total)) * 100.0), 1)
    payload = {
        "phase": phase,
        "stage": phase,
        "progress_status": progress_status,
        "phase_label": phase_label,
        "attempted": processed,
        "attempt_total": total,
        "effective_attempt_total": total,
        "strategy_checked": strategies_checked if strategies_checked is not None else processed,
        "strategy_total": strategies_total,
        "successful": passed,
        "current_script": current_script,
        "elapsed_seconds": elapsed_seconds,
        "eta_seconds": eta_seconds,
        "eta_status": "complete" if progress_status == "complete" else ("" if eta_seconds is None else "estimated"),
        "percent": percent or 0,
        # legacy blockchecks keys (kept for current-run-progress consumers)
        "attempts_processed": processed,
        "attempts_total": total,
        "processed_attempts": processed,
        "total_attempts": total,
        "candidate_count": passed,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

def _bs_progress_db_counts(db: Path) -> dict[str, int]:
    """Live attempt/working/strategy counts from the per-run bs database."""
    counts = {"attempts": 0, "working": 0, "strategies": 0}
    if not db.is_file():
        return counts
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return counts
    try:
        counts["attempts"] = int(conn.execute("SELECT COUNT(*) FROM tcp_results").fetchone()[0])
        counts["working"] = int(
            conn.execute(
                "SELECT COUNT(*) FROM tcp_results"
                " WHERE status IN ('PASS','THROTTLED')"
                " AND (bridge_applied IS NULL OR bridge_applied = 1)"
            ).fetchone()[0]
        )
        counts["strategies"] = int(
            conn.execute("SELECT COUNT(DISTINCT strategy_id) FROM tcp_results").fetchone()[0]
        )
    except sqlite3.Error:
        log.debug("could not open blockchecks run db read-only for metrics")
        pass
    finally:
        conn.close()
    return counts

