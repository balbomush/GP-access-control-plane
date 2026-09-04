"""bc2_engine._runner — moved from strategy_finder.py / blockchecks_backend.py."""
from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path
from gp_control_plane.state import now_iso
from gp_control_plane.storage import append_run
from typing import Any
from gp_control_plane.zapret2 import BLOCKCHECK_ENV_KEYS
from gp_control_plane.bc2_engine._multidomain import _resolve_blockcheck_script
from gp_control_plane.bc2_engine._plan import _standard_attempt_plan
from gp_control_plane.bc2_engine._process import _root_command_unless_stopped, _run_process_with_live_stdout, _stopped_process_result
from gp_control_plane.bc2_engine._recorder import _LiveStdoutRecorder
from gp_control_plane.bc2_engine._writers import _set_debug_stdout_env, _stdout_log_mode
from gp_control_plane.engine_common._constants import PHASE_CHECK_VPN, PHASE_COMPLETE
from gp_control_plane.engine_common._options import DiscoveryOptions, _domain_validation_run_fields, _minimum_int, validate_domain_inputs
from gp_control_plane.engine_common._retention import _cleanup_old_strategy_logs, _finder_dir
from gp_control_plane.engine_common._runmeta import _discovery_run_id, _ipvs_value
from gp_control_plane.engine_common._upsert import candidate_total, upsert_candidates

def _run_blockcheck_live(
    state_dir: Path,
    kind: str,
    domains: list[str],
    timeout_seconds: int,
    test: str,
    options: DiscoveryOptions,
    candidate_id: str = "",
    domain_validation: dict[str, Any] | None = None,
    debug_stdout: bool | None = None,
    stop_event: threading.Event | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    blockcheck = shutil.which("blockcheck2.sh") or shutil.which("blockcheck.sh")
    if not blockcheck:
        raise RuntimeError("blockcheck2.sh/blockcheck.sh not found in PATH")
    options = options.normalized()
    domain_validation = domain_validation or validate_domain_inputs(domains, default_to_critical=True)
    clean_domains = list(domain_validation["domains"])
    if not clean_domains:
        raise ValueError("no valid domains to check")
    validation_fields = _domain_validation_run_fields(domain_validation)
    blockcheck_path = _resolve_blockcheck_script(Path(blockcheck))
    full_env = os.environ.copy()
    full_env.update(
        {
            "BATCH": "1",
            "DOMAINS": " ".join(clean_domains),
            "IPVS": _ipvs_value(options),
            "TEST": test,
            **options.to_blockcheck_env(),
        }
    )
    _set_debug_stdout_env(full_env, debug_stdout)
    root = _finder_dir(state_dir)
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    _cleanup_old_strategy_logs(logs)
    run_id = _discovery_run_id(run_id)
    stdout_log = logs / f"{run_id}.{kind}.stdout.log"
    stderr_log = logs / f"{run_id}.{kind}.stderr.log"
    progress_log = logs / f"{run_id}.{kind}.progress.json"
    metrics_log = logs / f"{run_id}.{kind}.metrics.ndjson"
    summary_fallback_log = logs / f"{run_id}.{kind}.summary-fallback.ndjson"
    debug_stdout_log = logs / f"{run_id}.{kind}.debug.stdout.log"
    attempt_plan = _standard_attempt_plan(
        domains=clean_domains,
        test=test,
        enable_http=options.enable_http,
        enable_tls=options.enable_tls12,
        enable_tls13=options.enable_tls13,
        enable_quic=options.enable_quic,
        enable_ipv6=options.enable_ipv6,
    )
    option_fields = options.to_run_fields()
    started_at = now_iso()
    started = {
        "id": run_id,
        "kind": kind,
        "candidate_id": candidate_id,
        "status": "running",
        "timestamp": started_at,
        "started_at": started_at,
        "domains": clean_domains,
        "returncode": None,
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "progress_log": str(progress_log),
        "metrics_log": str(metrics_log),
        "summary_fallback_log": str(summary_fallback_log),
        "debug_stdout_log": str(debug_stdout_log),
        "stdout_log_mode": _stdout_log_mode(full_env),
        "debug_stdout": _stdout_log_mode(full_env) == "debug",
        "candidate_count": 0,
        "phase": PHASE_CHECK_VPN,
        "test": test,
        **option_fields,
        **validation_fields,
        "attempt_plan": attempt_plan,
    }
    append_run(state_dir, started)

    recorder = _LiveStdoutRecorder(state_dir, started)
    command = _root_command_unless_stopped(
        [str(blockcheck_path)],
        env=full_env,
        pass_env_keys=BLOCKCHECK_ENV_KEYS,
        run_id=run_id,
        stop_event=stop_event,
    )
    if command is None:
        process_result = _stopped_process_result(recorder)
    else:
        process_result = _run_process_with_live_stdout(
            command=command,
            env=full_env,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            debug_stdout_log=debug_stdout_log,
            timeout_seconds=timeout_seconds,
            stop_event=stop_event,
            recorder=recorder,
            run_id=run_id,
        )
    parsed = recorder.parsed()
    completed_at = now_iso()
    run = {
        "id": run_id,
        "kind": kind,
        "candidate_id": candidate_id,
        "status": process_result["status"],
        "timestamp": started_at,
        "started_at": started_at,
        "completed_at": completed_at,
        "domains": clean_domains,
        "returncode": process_result["returncode"],
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "progress_log": str(progress_log),
        "metrics_log": str(metrics_log),
        "summary_fallback_log": str(summary_fallback_log),
        "debug_stdout_log": str(debug_stdout_log),
        "stdout_log_mode": _stdout_log_mode(full_env),
        "debug_stdout": _stdout_log_mode(full_env) == "debug",
        "candidate_count": int(parsed.get("candidate_count") or len(parsed["candidates"])),
        "common_candidate_count": int(parsed.get("common_candidate_count") or len(parsed["common_candidates"])),
        "summary_line_count": int(parsed.get("summary_line_count") or 0),
        "common_line_count": int(parsed.get("common_line_count") or 0),
        "result_count": int(parsed.get("result_count") or 0),
        "common_result_count": int(parsed.get("common_result_count") or 0),
        "direct_available_count": int(parsed.get("direct_available_count") or 0),
        "not_working_count": int(parsed.get("not_working_count") or 0),
        "phase": parsed.get("phase") or PHASE_COMPLETE,
        "domain_diagnostics": parsed.get("domain_diagnostics") or [],
        "curl_diagnostics": parsed.get("curl_diagnostics") or [],
        "curl_diagnostics_summary": parsed.get("curl_diagnostics_summary") or {},
        "dominant_failure": parsed.get("dominant_failure") or {},
        "summary_verified": parsed.get("summary_verified", 0),
        "summary_fallbacks": parsed.get("summary_fallbacks", 0),
        "summary_common_seen": parsed.get("summary_common_seen", 0),
        "timed_out": process_result["timed_out"],
        "stopped": process_result["stopped"],
        "timeout_seconds": timeout_seconds,
        "test": test,
        **option_fields,
        **validation_fields,
        "attempt_plan": attempt_plan,
    }
    run["progress"] = recorder.progress(run)
    if kind in {"standard-discovery", "multi-domain-discovery"}:
        run["total_candidates"] = candidate_total(state_dir) if parsed.get("live_recorded") else upsert_candidates(state_dir, parsed, run)
    recorder.close()
    append_run(state_dir, run)
    return run

def _run_multidomain_blockcheck_live(
    state_dir: Path,
    domains: list[str],
    timeout_seconds: int,
    options: DiscoveryOptions,
    curl_parallelism: int,
    domain_validation: dict[str, Any] | None = None,
    debug_stdout: bool | None = None,
    stop_event: threading.Event | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    blockcheck = shutil.which("blockcheck2.sh") or shutil.which("blockcheck.sh")
    if not blockcheck:
        raise RuntimeError("blockcheck2.sh/blockcheck.sh not found in PATH")
    options = options.normalized()
    domain_validation = domain_validation or validate_domain_inputs(domains, default_to_critical=True)
    clean_domains = list(domain_validation["domains"])
    if not clean_domains:
        raise ValueError("no valid domains to check")
    blockcheck_path = _resolve_blockcheck_script(Path(blockcheck))
    zapret_base = blockcheck_path.parent
    normalized_parallelism = _minimum_int(curl_parallelism, default=4, minimum=1)

    full_env = os.environ.copy()
    full_env.update(
        {
            "BATCH": "1",
            "DOMAINS": " ".join(clean_domains),
            "IPVS": _ipvs_value(options),
            "TEST": "standard",
            **options.to_blockcheck_env(),
            "GP_MD_CURL_PARALLELISM": str(normalized_parallelism),
            "ZAPRET_BASE": str(zapret_base),
            "ZAPRET_RW": str(zapret_base),
        }
    )
    _set_debug_stdout_env(full_env, debug_stdout)
    run_id = _discovery_run_id(run_id)
    command = _root_command_unless_stopped(
        [str(blockcheck_path)],
        env=full_env,
        pass_env_keys=BLOCKCHECK_ENV_KEYS,
        helper_command="run-multidomain",
        run_id=run_id,
        stop_event=stop_event,
    )
    return _run_blockcheck_command_live(
        command=command or [],
        env=full_env,
        state_dir=state_dir,
        kind="multi-domain-discovery",
        domains=clean_domains,
        timeout_seconds=timeout_seconds,
        test="standard",
        options=options,
        curl_parallelism=normalized_parallelism,
        domain_validation=domain_validation,
        debug_stdout=debug_stdout,
        stop_event=stop_event,
        run_id=run_id,
    )

def _run_blockcheck_command_live(
    command: list[str],
    env: dict[str, str],
    state_dir: Path,
    kind: str,
    domains: list[str],
    timeout_seconds: int,
    test: str,
    options: DiscoveryOptions,
    curl_parallelism: int | None = None,
    candidate_id: str = "",
    domain_validation: dict[str, Any] | None = None,
    debug_stdout: bool | None = None,
    stop_event: threading.Event | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    options = options.normalized()
    domain_validation = domain_validation or validate_domain_inputs(domains, default_to_critical=True)
    domains = list(domain_validation["domains"])
    if not domains:
        raise ValueError("no valid domains to check")
    validation_fields = _domain_validation_run_fields(domain_validation)
    _set_debug_stdout_env(env, debug_stdout)
    root = _finder_dir(state_dir)
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    run_id = _discovery_run_id(run_id)
    stdout_log = logs / f"{run_id}.{kind}.stdout.log"
    stderr_log = logs / f"{run_id}.{kind}.stderr.log"
    progress_log = logs / f"{run_id}.{kind}.progress.json"
    metrics_log = logs / f"{run_id}.{kind}.metrics.ndjson"
    summary_fallback_log = logs / f"{run_id}.{kind}.summary-fallback.ndjson"
    debug_stdout_log = logs / f"{run_id}.{kind}.debug.stdout.log"
    attempt_plan = _standard_attempt_plan(
        domains=domains,
        test=test,
        enable_http=options.enable_http,
        enable_tls=options.enable_tls12,
        enable_tls13=options.enable_tls13,
        enable_quic=options.enable_quic,
        enable_ipv6=options.enable_ipv6,
    )
    option_fields = options.to_run_fields()
    started_at = now_iso()
    started = {
        "id": run_id,
        "kind": kind,
        "candidate_id": candidate_id,
        "status": "running",
        "timestamp": started_at,
        "started_at": started_at,
        "domains": domains,
        "returncode": None,
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "progress_log": str(progress_log),
        "metrics_log": str(metrics_log),
        "summary_fallback_log": str(summary_fallback_log),
        "debug_stdout_log": str(debug_stdout_log),
        "stdout_log_mode": _stdout_log_mode(env),
        "debug_stdout": _stdout_log_mode(env) == "debug",
        "candidate_count": 0,
        "phase": PHASE_CHECK_VPN,
        "test": test,
        **option_fields,
        "curl_parallelism": curl_parallelism,
        **validation_fields,
        "attempt_plan": attempt_plan,
    }
    append_run(state_dir, started)

    recorder = _LiveStdoutRecorder(state_dir, started)
    process_result = _run_process_with_live_stdout(
        command=command,
        env=env,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        debug_stdout_log=debug_stdout_log,
        timeout_seconds=timeout_seconds,
        stop_event=stop_event,
        recorder=recorder,
        run_id=run_id,
    )
    parsed = recorder.parsed()
    completed_at = now_iso()
    run = {
        "id": run_id,
        "kind": kind,
        "candidate_id": candidate_id,
        "status": process_result["status"],
        "timestamp": started_at,
        "started_at": started_at,
        "completed_at": completed_at,
        "domains": domains,
        "returncode": process_result["returncode"],
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "progress_log": str(progress_log),
        "metrics_log": str(metrics_log),
        "summary_fallback_log": str(summary_fallback_log),
        "debug_stdout_log": str(debug_stdout_log),
        "stdout_log_mode": _stdout_log_mode(env),
        "debug_stdout": _stdout_log_mode(env) == "debug",
        "candidate_count": int(parsed.get("candidate_count") or len(parsed["candidates"])),
        "common_candidate_count": int(parsed.get("common_candidate_count") or len(parsed["common_candidates"])),
        "summary_line_count": int(parsed.get("summary_line_count") or 0),
        "common_line_count": int(parsed.get("common_line_count") or 0),
        "result_count": int(parsed.get("result_count") or 0),
        "common_result_count": int(parsed.get("common_result_count") or 0),
        "direct_available_count": int(parsed.get("direct_available_count") or 0),
        "not_working_count": int(parsed.get("not_working_count") or 0),
        "phase": parsed.get("phase") or PHASE_COMPLETE,
        "domain_diagnostics": parsed.get("domain_diagnostics") or [],
        "curl_diagnostics": parsed.get("curl_diagnostics") or [],
        "curl_diagnostics_summary": parsed.get("curl_diagnostics_summary") or {},
        "dominant_failure": parsed.get("dominant_failure") or {},
        "summary_verified": parsed.get("summary_verified", 0),
        "summary_fallbacks": parsed.get("summary_fallbacks", 0),
        "summary_common_seen": parsed.get("summary_common_seen", 0),
        "timed_out": process_result["timed_out"],
        "stopped": process_result["stopped"],
        "timeout_seconds": timeout_seconds,
        "test": test,
        **option_fields,
        "curl_parallelism": curl_parallelism,
        **validation_fields,
        "attempt_plan": attempt_plan,
    }
    run["progress"] = recorder.progress(run)
    if kind in {"standard-discovery", "multi-domain-discovery"}:
        run["total_candidates"] = candidate_total(state_dir) if parsed.get("live_recorded") else upsert_candidates(state_dir, parsed, run)
    recorder.close()
    append_run(state_dir, run)
    return run
