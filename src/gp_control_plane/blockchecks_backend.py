"""Run interactive `bs scan` and harvest PASS∧APPLIED into GP SQLite."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .discovery_engine import (
    DEFAULT_BS_JOB_CAP,
    PROGRESS_LINE,
    blockchecks_state_dir,
    build_bs_scan_argv,
    campaign_lock_busy_message,
    resolve_bc_nfconf,
    resolve_bs_binary,
)
from .state import now_iso
from .storage import append_run, upsert_candidate_event
from .strategy_finder import (
    PHASE_COMPLETE,
    PHASE_DISCOVERY,
    _cleanup_old_strategy_logs,
    _discovery_run_id,
    _domain_validation_run_fields,
    _finder_dir,
    candidate_id_for,
    validate_domain_inputs,
)

_PROGRESS_RE = re.compile(PROGRESS_LINE)


def stop_blockchecks() -> None:
    try:
        bs = resolve_bs_binary()
    except RuntimeError:
        return
    subprocess.run([bs, "stop", "--wait"], check=False, timeout=60)


def export_nfconf(*, out_dir: Path | None = None, limit: int = 5) -> dict[str, Any]:
    nfconf = resolve_bc_nfconf()
    target = Path(out_dir) if out_dir else Path.home() / ".local" / "share" / "blockcheckS" / "export"
    target.mkdir(parents=True, exist_ok=True)
    db = blockchecks_state_dir() / "state.db"
    if not db.is_file():
        raise RuntimeError(f"blockcheckS state.db not found: {db}")
    completed = subprocess.run(
        [nfconf, "--db", str(db), "--out-dir", str(target), "--limit", str(max(1, int(limit)))],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "bc-nfconf failed")
    confs = sorted(str(path) for path in target.glob("*.conf"))
    return {"engine": "blockchecks", "out_dir": str(target), "paths": confs, "db": str(db)}


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
) -> dict[str, Any]:
    del enable_http, enable_tls12, enable_tls13, enable_ipv6, skip_ipblock
    del curl_max_time_quic, curl_max_time_doh, debug_stdout, include_quic
    busy = campaign_lock_busy_message()
    if busy:
        raise RuntimeError(busy)
    domain_validation = validate_domain_inputs(domains, default_to_critical=True)
    clean_domains = list(domain_validation["domains"])
    if not clean_domains:
        raise ValueError("no valid domains to check")
    argv = build_bs_scan_argv(
        domains=clean_domains,
        scan_level=scan_level,
        repeats=repeats,
        repeat_parallel=repeat_parallel,
        curl_max_time=curl_max_time,
        timeout_seconds=timeout_seconds,
        curl_parallelism=curl_parallelism,
        skip_dnscheck=skip_dnscheck,
    )
    logs = _finder_dir(state_dir) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    _cleanup_old_strategy_logs(logs)
    run_id = _discovery_run_id(run_id)
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
        "bs_job_cap": DEFAULT_BS_JOB_CAP if timeout_seconds <= 0 else None,
        **_domain_validation_run_fields(domain_validation),
        "discovery_options": {
            "scan_level": scan_level,
            "repeats": repeats,
            "repeat_parallel": repeat_parallel,
            "skip_dnscheck": skip_dnscheck,
            "curl_max_time": curl_max_time,
            "discovery_engine": "blockchecks",
        },
    }
    append_run(state_dir, started)
    _write_progress(progress_log, phase=PHASE_DISCOVERY, processed=0, total=0, passed=0, started=time.monotonic())
    harvested: set[tuple[str, str, str]] = set()
    process = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=os.environ.copy(),
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
            match = _PROGRESS_RE.search(line)
            if match:
                processed, total, passed = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
                _write_progress(
                    progress_log,
                    phase=PHASE_DISCOVERY,
                    processed=processed,
                    total=total,
                    passed=passed,
                    started=time.monotonic() if processed <= 1 else None,
                )
            _harvest_passes(state_dir, run_id, kind, harvested)
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
                process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
    _harvest_passes(state_dir, run_id, kind, harvested)
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
        processed=run.get("candidate_count") or 0,
        total=run.get("candidate_count") or 0,
        passed=len(harvested),
        started=None,
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
) -> None:
    payload = {
        "phase": phase,
        "stage": phase,
        "attempts_processed": processed,
        "attempts_total": total,
        "processed_attempts": processed,
        "total_attempts": total,
        "candidate_count": passed,
        "progress_status": "complete" if phase == PHASE_COMPLETE else "running",
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _harvest_passes(
    state_dir: Path,
    run_id: str,
    kind: str,
    harvested: set[tuple[str, str, str]],
) -> None:
    db = blockchecks_state_dir() / "state.db"
    if not db.is_file():
        return
    source_mode = "multi_domain" if "multi" in kind else "single_domain"
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return
    try:
        rows = conn.execute(
            """
            SELECT t.domain, s.name, s.proto
            FROM tcp_results t
            JOIN strategies s ON s.id = t.strategy_id
            WHERE t.status = 'PASS' AND coalesce(t.bridge_applied, 0) = 1
            """
        ).fetchall()
    except sqlite3.Error:
        conn.close()
        return
    conn.close()
    seen_at = now_iso()
    for domain, name, proto in rows:
        args = str(name or "").strip()
        host = str(domain or "").strip()
        if not args or not host:
            continue
        protocol = "quic" if str(proto or "").lower() == "udp" or "quic" in args.lower() else "tls"
        key = (protocol, args, host)
        if key in harvested:
            continue
        harvested.add(key)
        upsert_candidate_event(
            state_dir,
            candidate_id=candidate_id_for(protocol, args),
            protocol=protocol,
            args=args,
            status="working",
            run_id=run_id,
            domain=host,
            domains=[host],
            test="blockchecks-scan",
            ip_version="4",
            seen_at=seen_at,
            common=source_mode == "multi_domain",
        )
