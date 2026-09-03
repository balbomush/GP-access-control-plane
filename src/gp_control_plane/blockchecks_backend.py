"""Run interactive `bs scan` and harvest PASS∧APPLIED into GP SQLite."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .discovery_engine import (
    DEFAULT_BS_JOB_CAP,
    DOMAIN_ARGV_THRESHOLD,
    PROGRESS_LINE,
    blockchecks_state_dir,
    bs_run_env,
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
AQ_JOBS_RE = re.compile(r"AQ pending jobs:\s+(\d+)")
GEN_TCP_RE = re.compile(r"Generated:\s+(\d+)\s+TCP")


def _default_export_out_dir() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data_home) / "blockcheckS" / "export"


def latest_bs_run_db() -> Path | None:
    """Most recent per-GP-run BS database under the blockcheckS state dir."""
    runs_dir = blockchecks_state_dir() / "bs-runs"
    if runs_dir.is_dir():
        dbs = sorted(runs_dir.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        if dbs:
            return dbs[0]
    default = blockchecks_state_dir() / "state.db"
    return default if default.is_file() else None


def bs_providers_root() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    override = os.environ.get("BLOCKCHECKS_DATA_BLOCK") or ""
    base = Path(override).expanduser() if override else Path(data_home) / "blockcheckS"
    return base.resolve() / "data_block" / "providers"


def list_bs_dns_pins(*, max_lines: int = 600) -> dict[str, Any]:
    """Read-only DNS-pin hosts files written by blockcheckS (anti-hijack)."""
    root = bs_providers_root()
    providers: list[dict[str, Any]] = []
    if not root.is_dir():
        return {"providers": providers, "root": str(root)}
    for provider_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        hosts = provider_dir / "hosts"
        if not hosts.is_file():
            continue
        try:
            lines = hosts.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        providers.append(
            {
                "provider": provider_dir.name,
                "path": str(hosts),
                "lines": lines[:max_lines],
                "mtime": int(hosts.stat().st_mtime),
            }
        )
    return {"providers": providers, "root": str(root)}


def stop_blockchecks() -> None:
    try:
        bs = resolve_bs_binary()
    except RuntimeError:
        return
    subprocess.run([bs, "stop", "--wait", "120"], check=False, timeout=60)


def export_nfconf(
    *,
    out_dir: Path | None = None,
    limit: int = 5,
    db: Path | None = None,
    allow_stock_fallback: bool = True,
) -> dict[str, Any]:
    """Re-export nfqws2 confs from a blockcheckS run DB.

    bc-nfconf targets explicit domains only (its built-in set otherwise).
    We scope it to the distinct domains recorded in the run DB and let it
    fall back to per-domain best export (``--no-common-only``).
    """
    nfconf = resolve_bc_nfconf()
    target = Path(out_dir) if out_dir else _default_export_out_dir()
    target.mkdir(parents=True, exist_ok=True)
    target_db = Path(db) if db else latest_bs_run_db()
    if target_db is None or not target_db.is_file():
        raise RuntimeError(f"blockcheckS run database not found: {blockchecks_state_dir()}")
    domains = _distinct_run_domains(target_db)
    if not domains:
        raise RuntimeError(f"no tcp_results domains in run database: {target_db}")
    temp_dir = Path(tempfile.mkdtemp(prefix="gp-bs-nfconf-"))
    try:
        domains_file = temp_dir / "domains.txt"
        domains_file.write_text("\n".join(domains) + "\n", encoding="utf-8")
        cmd = [
            nfconf,
            "--db",
            str(target_db),
            "--out-dir",
            str(target),
            "--limit",
            str(max(1, int(limit))),
            "--domains-file",
            str(domains_file),
            "--no-common-only",
        ]
        if allow_stock_fallback:
            cmd.append("--allow-stock-fallback")
        completed = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                (completed.stderr or "").strip() or (completed.stdout or "").strip() or "bc-nfconf failed"
            )
    finally:
        import shutil

        shutil.rmtree(temp_dir, ignore_errors=True)
    confs = sorted(str(path) for path in target.glob("*.conf"))
    return {"engine": "blockchecks", "out_dir": str(target), "paths": confs, "db": str(target_db)}


def _distinct_run_domains(db: Path) -> list[str]:
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "SELECT DISTINCT domain FROM tcp_results WHERE domain IS NOT NULL AND domain != ''"
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    return [str(row[0]) for row in rows]


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
            "discovery_engine": "blockchecks",
        },
    }
    append_run(state_dir, started)
    process_started = time.monotonic()
    progress_state = {
        "attempt_total": 0,
        "strategies_total": 0,
        "last_db_poll": 0.0,
        "script": f"bs scan{(' -M ' + strategy_preset) if strategy_preset else ''}",
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
                    pass
                process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
        if domains_file_arg is not None:
            domains_file_arg.unlink(missing_ok=True)
    _harvest_passes(state_dir, run_id, kind, harvested, run_db)
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
        pass
    finally:
        conn.close()
    return counts


def _looks_like_conf_path(value: str) -> bool:
    v = str(value or "").strip()
    return bool(v) and ("/" in v or "\\" in v) and v.lower().endswith(".conf")


def _desync_cores_from_conf(path: str) -> list[str]:
    """Return ``--lua-desync=`` core strings from an nfqws2 .conf file."""
    cores: list[str] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if line.startswith("--lua-desync="):
                    core = line[len("--lua-desync=") :].strip()
                    if core:
                        cores.append(core)
    except OSError:
        return []
    return cores


def _expand_config_candidate_args(value: str) -> list[str]:
    """Turn a stored strategy value into harvest candidate arg strings.

    ``config_path`` may be an nfqws2 .conf file (default BS configs source):
    each ``--lua-desync=`` core becomes its own inline candidate so the web
    panel shows real strategy lines instead of file paths.
    """
    v = str(value or "").strip()
    if v and _looks_like_conf_path(v) and os.path.isfile(v):
        cores = _desync_cores_from_conf(v)
        if cores:
            return cores
    return [v] if v else []


def _harvest_passes(
    state_dir: Path,
    run_id: str,
    kind: str,
    harvested: set[tuple[str, str, str]],
    db: Path,
) -> None:
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
            SELECT t.domain, s.name, s.config_path, s.proto
            FROM tcp_results t
            JOIN strategies s ON s.id = t.strategy_id
            WHERE t.status IN ('PASS','THROTTLED')
              AND (t.bridge_applied IS NULL OR t.bridge_applied = 1)
              AND t.id = (
                SELECT t2.id FROM tcp_results t2
                WHERE t2.strategy_id = s.id AND t2.domain = t.domain
                ORDER BY t2.id DESC LIMIT 1
              )
            """
        ).fetchall()
    except sqlite3.Error:
        conn.close()
        return
    conn.close()
    seen_at = now_iso()
    for domain, name, config_path, proto in rows:
        host = str(domain or "").strip()
        if not host:
            continue
        base_args = str(config_path or "").strip() or str(name or "").strip()
        proto_raw = str(proto or "").lower()
        protocol = "quic" if proto_raw in ("quic", "udp") else "tls"
        for args in _expand_config_candidate_args(base_args):
            if not args:
                continue
            if protocol == "tls" and "quic" in args.lower():
                protocol = "quic"
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
