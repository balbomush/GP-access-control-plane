"""bc2_engine._progress — moved from strategy_finder.py / blockchecks_backend.py."""
from __future__ import annotations

from collections import deque
from typing import Any
from gp_control_plane.bc2_engine._plan import _elapsed_average_ms_per_attempt, _elapsed_seconds, _empty_attempt_plan, _eta_from_remaining_attempts, _eta_parallelism_for_run, _eta_recalculation_step, _standard_attempt_plan, _standard_script_index, _standard_script_total
from gp_control_plane.engine_common._constants import ETA_SAMPLE_MAX_POINTS, ETA_SAMPLE_MIN_ATTEMPTS, ETA_SAMPLE_WINSORIZE_MIN_INTERVALS, ETA_SAMPLE_WINSORIZE_RATIO, PHASE_CHECK_DOMAIN, PHASE_CHECK_VPN, PHASE_CHECK_ZAPRET, PHASE_COMPLETE, PHASE_DISCOVERY, PHASE_LABELS, PHASE_SAVING, PHASE_SUMMARY, _ATTEMPT_RE, _SCRIPT_RE
from gp_control_plane.engine_common._logtail import parse_blockcheck_stdout
from gp_control_plane.engine_common._options import _bounded_int, _truthy
from gp_control_plane.engine_common._stdout_parse import _live_attempt_line

def progress_from_stdout(stdout: str, run: dict[str, Any]) -> dict[str, Any]:
    lines = stdout.splitlines()
    attempted = sum(1 for line in lines if _ATTEMPT_RE.match(line.strip()))
    attempts_by_script = _attempts_by_script(lines)
    parsed = parse_blockcheck_stdout(stdout)
    successful = len(
        {
            (str(item.get("protocol") or ""), str(item.get("args") or ""))
            for item in [*parsed["candidates"], *parsed["common_candidates"]]
        }
    )
    scripts = [_script_name_from_line(line) for line in lines if _script_name_from_line(line)]
    current_script = scripts[-1] if scripts else ""
    phase = PHASE_SUMMARY if any(line.strip() in {"* SUMMARY", "* COMMON"} for line in lines) else (PHASE_DISCOVERY if current_script else PHASE_CHECK_VPN)
    return _progress_from_counts(
        run=run,
        attempted=attempted,
        attempts_by_script=attempts_by_script,
        successful=successful,
        current_script=current_script,
        phase=phase,
    )

def _progress_from_counts(
    *,
    run: dict[str, Any],
    attempted: int,
    attempts_by_script: dict[str, int],
    successful: int,
    current_script: str,
    phase: str = PHASE_CHECK_VPN,
    runtime_ms_per_attempt: int | None = None,
    runtime_sample_count: int | None = None,
    summary_verified: int = 0,
    summary_fallbacks: int = 0,
    elapsed_seconds_override: int | None = None,
    eta_recalculation_attempts_override: int | None = None,
    eta_elapsed_seconds_override: int | None = None,
) -> dict[str, Any]:
    attempt_plan = _attempt_plan_for_run(run, current_script)
    script_order = [str(item) for item in attempt_plan.get("script_order") or []]
    script_attempt_totals = attempt_plan.get("scripts") if isinstance(attempt_plan.get("scripts"), dict) else {}
    attempt_total = int(attempt_plan.get("total") or 0)
    strategy_progress = _strategy_progress_from_attempts(attempt_plan, attempts_by_script, current_script)
    current_script_attempted = attempts_by_script.get(current_script, 0)
    current_script_attempt_total = int(script_attempt_totals.get(current_script) or 0)
    script_total = len(script_order) if script_order else (_standard_script_total() if current_script.startswith("standard/") else 0)
    script_index = _standard_script_index(current_script, script_order) if current_script else 0
    if script_total and script_index > script_total:
        script_index = script_total
    status = str(run.get("status") or "")
    finished = status in {"success", "failed", "timeout", "stopped"}
    completed = status == "success"
    if finished and phase == PHASE_SAVING:
        phase = PHASE_COMPLETE
    if completed and script_total:
        script_index = script_total
    progress_status = "unknown"
    effective_attempt_total = attempt_total
    remaining_attempts = max(0, attempt_total - attempted) if attempt_total else None
    if finished:
        remaining_attempts = None
    if attempt_total:
        progress_status = "exact" if str(attempt_plan.get("source") or "") in {"shell", "test"} else "estimated"
    current_script_underestimated = bool(
        attempt_total
        and current_script_attempt_total
        and current_script_attempted > current_script_attempt_total
    )
    if attempt_total and not finished and (attempted >= attempt_total or current_script_underestimated):
        progress_status = "underestimated"
        if current_script_underestimated and attempted < attempt_total:
            remaining_attempts = None
            effective_attempt_total = attempt_total
        else:
            script_remaining = current_script_attempt_total - current_script_attempted if current_script_attempt_total else 0
            if script_remaining > 0:
                remaining_attempts = script_remaining
                effective_attempt_total = attempted + script_remaining
            else:
                remaining_attempts = None
                effective_attempt_total = attempted
    if effective_attempt_total:
        if completed:
            percent = 100.0
        elif remaining_attempts is None and progress_status == "underestimated":
            if effective_attempt_total and attempted < effective_attempt_total:
                percent = min(99.0, (attempted / effective_attempt_total) * 100.0)
            else:
                percent = 99.0
        else:
            percent = min(99.9, (attempted / effective_attempt_total) * 100.0)
    else:
        percent = (script_index / script_total * 100.0) if script_total else None
    elapsed = (
        elapsed_seconds_override
        if elapsed_seconds_override is not None
        else _elapsed_seconds(run.get("started_at") or run.get("timestamp"))
    )
    eta_parallelism = 1
    eta_configured_parallelism = _eta_parallelism_for_run(run)
    eta_recalculation_step = _eta_recalculation_step(attempted)
    eta_recalculation_attempts = (
        eta_recalculation_attempts_override
        if eta_recalculation_attempts_override is not None
        else attempted
    )
    eta_elapsed = eta_elapsed_seconds_override if eta_elapsed_seconds_override is not None else elapsed
    eta_ms_per_attempt = _elapsed_average_ms_per_attempt(eta_elapsed, eta_recalculation_attempts)
    estimate_ms_per_attempt = eta_ms_per_attempt or 0
    eta_status = "elapsed_average" if eta_ms_per_attempt else "calculating"
    eta_method = "elapsed_average" if eta_ms_per_attempt else "waiting_for_attempts"
    if finished and not completed:
        eta = None
        eta_status = status or "finished"
        eta_method = "finished"
    elif remaining_attempts is None and not completed:
        eta = None
        if progress_status == "underestimated":
            eta_status = "underestimated"
    elif completed:
        eta = 0
        eta_status = "complete"
        eta_method = "complete"
    elif eta_status == "calculating":
        eta = None
    else:
        eta = _eta_from_remaining_attempts(remaining_attempts, completed, eta_parallelism, eta_ms_per_attempt)
    return {
        "attempted": attempted,
        "attempt_total": attempt_total,
        "effective_attempt_total": effective_attempt_total,
        "remaining_attempts": remaining_attempts,
        "successful": successful,
        "strategy_checked": strategy_progress["checked"],
        "strategy_total": strategy_progress["total"],
        "current_script_strategy_checked": strategy_progress["current_script_checked"],
        "current_script_strategy_total": strategy_progress["current_script_total"],
        "current_script": current_script,
        "current_script_attempted": current_script_attempted,
        "current_script_attempt_total": current_script_attempt_total,
        "script_index": script_index,
        "script_total": script_total,
        "percent": percent,
        "elapsed_seconds": elapsed,
        "eta_seconds": eta,
        "eta_estimate_ms_per_attempt": estimate_ms_per_attempt,
        "eta_ms_per_attempt": eta_ms_per_attempt,
        "eta_status": eta_status,
        "eta_parallelism": eta_parallelism,
        "eta_configured_parallelism": eta_configured_parallelism,
        "eta_method": eta_method,
        "eta_sample_count": runtime_sample_count or 0,
        "eta_sample_window": ETA_SAMPLE_MAX_POINTS - 1,
        "eta_recalculation_step": eta_recalculation_step,
        "eta_recalculation_attempts": eta_recalculation_attempts,
        "eta_elapsed_seconds": eta_elapsed,
        "repeats": _bounded_int(run.get("repeats"), default=1, minimum=1, maximum=10),
        "repeat_parallel": _truthy(run.get("repeat_parallel"), default=False),
        "attempt_plan_source": attempt_plan.get("source") or "",
        "progress_status": progress_status,
        "phase": phase,
        "phase_label": _phase_label(phase),
        "summary_verified": summary_verified,
        "summary_fallbacks": summary_fallbacks,
    }

def _strategy_progress_from_attempts(
    attempt_plan: dict[str, Any],
    attempts_by_script: dict[str, int],
    current_script: str,
) -> dict[str, int]:
    script_order = [str(item) for item in attempt_plan.get("script_order") or []]
    script_attempt_totals = attempt_plan.get("scripts") if isinstance(attempt_plan.get("scripts"), dict) else {}
    raw_strategy_scripts = attempt_plan.get("strategy_scripts") if isinstance(attempt_plan.get("strategy_scripts"), dict) else {}
    domain_count = max(1, int(attempt_plan.get("domain_count") or 0))
    ip_version_count = max(1, int(attempt_plan.get("ip_version_count") or 1))
    default_attempts_per_strategy = max(1, domain_count * ip_version_count)
    strategy_scripts: dict[str, int] = {}
    for script in script_order:
        raw_total = int(raw_strategy_scripts.get(script) or 0)
        if raw_total <= 0:
            raw_total = int(script_attempt_totals.get(script) or 0) // default_attempts_per_strategy
        strategy_scripts[script] = max(0, raw_total)
    strategy_total = int(attempt_plan.get("strategy_total") or sum(strategy_scripts.values()))
    checked = 0
    current_checked = 0
    current_total = strategy_scripts.get(current_script, 0)
    for script in script_order:
        script_strategy_total = strategy_scripts.get(script, 0)
        if script_strategy_total <= 0:
            continue
        script_attempt_total = int(script_attempt_totals.get(script) or 0)
        attempts_per_strategy = max(1, script_attempt_total // script_strategy_total) if script_attempt_total else default_attempts_per_strategy
        script_checked = min(script_strategy_total, int(attempts_by_script.get(script, 0)) // attempts_per_strategy)
        if script == current_script:
            current_checked = script_checked
        checked += script_checked
    return {
        "checked": min(strategy_total, checked),
        "total": strategy_total,
        "current_script_checked": current_checked,
        "current_script_total": current_total,
    }

def _attempts_by_script(lines: list[str]) -> dict[str, int]:
    current_script = ""
    result: dict[str, int] = {}
    for line in lines:
        script = _script_name_from_line(line)
        if script:
            current_script = script
            result.setdefault(current_script, 0)
            continue
        if _ATTEMPT_RE.match(line.strip()):
            result[current_script] = result.get(current_script, 0) + 1
    return result

def _average_attempt_ms(samples: deque[float]) -> int | None:
    if len(samples) < ETA_SAMPLE_MIN_ATTEMPTS:
        return None
    values = list(samples)
    intervals = [right - left for left, right in zip(values, values[1:]) if right >= left]
    if not intervals:
        return None
    if len(intervals) >= ETA_SAMPLE_WINSORIZE_MIN_INTERVALS:
        intervals = _winsorized(intervals, ETA_SAMPLE_WINSORIZE_RATIO)
    return max(1, int((sum(intervals) / len(intervals)) * 1000))

def _winsorized(values: list[float], ratio: float) -> list[float]:
    if not values:
        return []
    ordered = sorted(values)
    edge = int(len(ordered) * ratio)
    if edge <= 0 or edge * 2 >= len(ordered):
        return values
    low = ordered[edge]
    high = ordered[-edge - 1]
    return [min(max(value, low), high) for value in values]

def _phase_label(phase: str) -> str:
    return PHASE_LABELS.get(phase, phase or "-")

def _phase_from_line(line: str, current: str) -> str:
    text = line.strip().lower()
    if not text:
        return current
    if text in {"* summary", "* common"}:
        return PHASE_SUMMARY
    if _ATTEMPT_RE.match(line.strip()) or _live_attempt_line(line):
        return PHASE_DISCOVERY
    if text.startswith("* script"):
        return PHASE_DISCOVERY
    if text.startswith("* checking"):
        if "vpn" in text:
            return PHASE_CHECK_VPN
        if "dpi" in text or "bypass" in text or "zapret" in text or "nfqws" in text:
            return PHASE_CHECK_ZAPRET
        if "dns" in text or "domain" in text or "ip" in text or "port" in text or "http" in text:
            return PHASE_CHECK_DOMAIN
        if current in {PHASE_CHECK_VPN, PHASE_CHECK_ZAPRET, PHASE_CHECK_DOMAIN}:
            return current
        return PHASE_CHECK_DOMAIN
    return current

def _script_name_from_line(line: str) -> str:
    match = _SCRIPT_RE.match(line.strip())
    return match.group(1).strip() if match else ""

def _attempt_plan_for_run(run: dict[str, Any], current_script: str) -> dict[str, Any]:
    raw_plan = run.get("attempt_plan")
    if isinstance(raw_plan, dict) and int(raw_plan.get("total") or 0) > 0:
        return raw_plan
    if not current_script.startswith("standard/") and str(run.get("test") or "standard") != "standard":
        return _empty_attempt_plan(str(run.get("test") or ""))
    return _standard_attempt_plan(
        domains=[str(item) for item in run.get("domains") or []],
        test=str(run.get("test") or "standard"),
        enable_http=_truthy(run.get("enable_http"), default=False),
        enable_tls=_truthy(run.get("enable_tls"), default=True),
        enable_tls13=_truthy(run.get("enable_tls13"), default=False),
        enable_quic=_truthy(run.get("enable_quic"), default=True),
        enable_ipv6=_truthy(run.get("enable_ipv6"), default=False),
    )
