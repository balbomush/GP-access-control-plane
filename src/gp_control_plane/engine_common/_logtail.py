"""engine_common._logtail — moved from strategy_finder.py / blockchecks_backend.py."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from gp_control_plane.engine_common._constants import NFQUEUE_MAXLEN_MISSING_RE
from gp_control_plane.engine_common._models import _file_version, _log_delta, _tail_lines
from gp_control_plane.engine_common._options import _bounded_int, _minimum_int
from gp_control_plane.engine_common._runs import read_runs
from gp_control_plane.engine_common._stdout_parse import _candidate_lines, _curl_summary, _dedupe_candidate_lines, _diagnostic_counts_from_stdout, _domain_diagnostics_from_counts, _dominant_failure_from_counts, _live_available_lines, _parse_result_line, _summary_sections

def latest_log_tail(
    state_dir: Path,
    max_lines: int = 200,
    *,
    run_id: str | None = None,
    stdout_from_size: int | None = None,
    stdout_log_match: str | None = None,
    stderr_from_size: int | None = None,
    stderr_log_match: str | None = None,
) -> dict[str, Any]:
    from gp_control_plane.bc2_engine._progress import progress_from_stdout
    requested_run_id = str(run_id or "") if run_id is not None else None
    for run in reversed(read_runs(state_dir, limit=200)):
        if requested_run_id is not None and str(run.get("id") or "") != requested_run_id:
            continue
        stdout_log = Path(str(run.get("stdout_log") or ""))
        if not stdout_log.is_file():
            continue
        stderr_log_raw = str(run.get("stderr_log") or "")
        stderr_log = Path(stderr_log_raw) if stderr_log_raw else None
        stdout_delta = _log_delta(stdout_log, stdout_log_match, stdout_from_size)
        stderr_delta = _log_delta(stderr_log, stderr_log_match, stderr_from_size) if stderr_log and stderr_log.is_file() else None
        if stdout_delta is None:
            stdout_tail = "\n".join(_tail_lines(stdout_log, max_lines))
            stdout_append = ""
        else:
            stdout_tail = ""
            stdout_append = stdout_delta
        if stderr_delta is None:
            stderr_tail = "\n".join(_tail_lines(stderr_log, max_lines)) if stderr_log and stderr_log.is_file() else ""
            stderr_append = ""
        else:
            stderr_tail = ""
            stderr_append = stderr_delta
        stderr_diagnostics = classify_stderr_diagnostics("\n".join(part for part in (stderr_tail, stderr_append) if part))
        progress = run.get("progress")
        if not isinstance(progress, dict):
            progress = _read_progress_log(run)
        if not isinstance(progress, dict):
            if not stdout_tail:
                stdout_tail = "\n".join(_tail_lines(stdout_log, max_lines))
            progress = progress_from_stdout(stdout_tail, run)
            progress["partial"] = True
        return {
            "run_id": run.get("id"),
            "kind": run.get("kind"),
            "status": run.get("status"),
            "stdout_tail": stdout_tail,
            "stdout_append": stdout_append,
            "stderr_tail": stderr_tail,
            "stderr_append": stderr_append,
            "stderr_diagnostics": stderr_diagnostics,
            "stdout_log": str(stdout_log),
            "stderr_log": str(stderr_log) if stderr_log else "",
            "stdout_size": _file_version(stdout_log)["size"],
            "stderr_size": _file_version(stderr_log)["size"] if stderr_log and stderr_log.is_file() else 0,
            "progress": progress,
            "metrics": _read_latest_metrics(run),
            "run_settings": _run_settings_for_progress(run),
        }
    return {
        "run_id": requested_run_id,
        "kind": None,
        "status": None,
        "stdout_tail": "",
        "stdout_append": "",
        "stderr_tail": "",
        "stderr_append": "",
        "stderr_diagnostics": [],
        "stdout_size": 0,
        "stderr_size": 0,
        "progress": {} if requested_run_id is not None else progress_from_stdout("", {}),
        "metrics": {},
        "run_settings": {},
    }

def classify_stderr_diagnostics(stderr_text: str) -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in stderr_text.splitlines():
        text = line.strip()
        if not text:
            continue
        if NFQUEUE_MAXLEN_MISSING_RE.search(text):
            status = "nfqueue_maxlen_sysctl_missing"
            if status in seen:
                continue
            seen.add(status)
            diagnostics.append(
                {
                    "severity": "warning",
                    "status": status,
                    "label": "NFQUEUE maxlen недоступен",
                    "message": (
                        "На этой системе нет sysctl для queue maxlen. "
                        "Это совместимость ядра/NFQUEUE: подбор может продолжаться, "
                        "строка не считается фатальной ошибкой GP."
                    ),
                    "source": "stderr",
                    "line": text,
                }
            )
    return diagnostics

def _run_settings_for_progress(run: dict[str, Any]) -> dict[str, Any]:
    options = run.get("discovery_options") if isinstance(run.get("discovery_options"), dict) else {}

    def option_value(key: str, fallback_keys: tuple[str, ...] = (), default: Any = None) -> Any:
        if key in options:
            return options[key]
        for fallback_key in fallback_keys:
            if fallback_key in run:
                return run[fallback_key]
        if key in run:
            return run[key]
        return default

    return {
        "domain_count": len(run.get("domains") or []),
        "kind": run.get("kind") or "",
        "enable_http": bool(option_value("enable_http", default=False)),
        "enable_tls12": bool(option_value("enable_tls12", ("enable_tls",), True)),
        "enable_tls13": bool(option_value("enable_tls13", default=False)),
        "enable_quic": bool(option_value("enable_quic", ("include_quic",), True)),
        "enable_ipv6": bool(option_value("enable_ipv6", default=False)),
        "scan_level": str(option_value("scan_level", default="standard") or "standard"),
        "discovery_engine": str(option_value("discovery_engine", default="blockcheck2") or "blockcheck2"),
        "repeats": _bounded_int(option_value("repeats", default=1), default=1, minimum=1, maximum=10),
        "repeat_parallel": bool(option_value("repeat_parallel", default=False)),
        "skip_dnscheck": bool(option_value("skip_dnscheck", default=True)),
        "skip_ipblock": bool(option_value("skip_ipblock", default=True)),
        "curl_parallelism": _minimum_int(run.get("curl_parallelism"), default=4, minimum=1)
        if str(run.get("kind") or "") == "multi-domain-discovery"
        else None,
        "timeout_seconds": _minimum_int(run.get("timeout_seconds"), default=0, minimum=0),
        "curl_max_time": _minimum_int(option_value("curl_max_time", default=2), default=2, minimum=1),
        "curl_max_time_quic": _minimum_int(option_value("curl_max_time_quic", default=2), default=2, minimum=1),
        "curl_max_time_doh": _minimum_int(option_value("curl_max_time_doh", default=2), default=2, minimum=1),
    }

def _read_progress_log(run: dict[str, Any]) -> dict[str, Any] | None:
    progress_log = str(run.get("progress_log") or "")
    if not progress_log:
        return None
    path = Path(progress_log)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None

def _read_latest_metrics(run: dict[str, Any]) -> dict[str, Any]:
    metrics_log = str(run.get("metrics_log") or "")
    if not metrics_log:
        return {}
    path = Path(metrics_log)
    if not path.is_file():
        return {}
    for line in reversed(_tail_lines(path, 20)):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return {}

def parse_blockcheck_stdout(stdout: str) -> dict[str, Any]:
    sections = _summary_sections(stdout)
    summary = sections["summary"]
    common = sections["common"]
    live_summary = _live_available_lines(stdout)
    candidates = _dedupe_candidate_lines([*_candidate_lines(summary, scope="domain"), *_candidate_lines(live_summary, scope="domain")])
    common_candidates = _candidate_lines(common, scope="common")
    results = [_parse_result_line(line) for line in summary if _parse_result_line(line)]
    common_results = [_parse_result_line(line) for line in common if _parse_result_line(line)]
    diagnostic_counts, diagnostic_codes, curl_diagnostics = _diagnostic_counts_from_stdout(stdout, results)
    return {
        "summary": summary,
        "common": common,
        "live_summary": live_summary,
        "candidates": candidates,
        "common_candidates": common_candidates,
        "results": results,
        "common_results": common_results,
        "direct_available": [item for item in results if item.get("result") == "working without bypass"],
        "not_working": [item for item in results if "not working" in str(item.get("result") or "")],
        "domain_diagnostics": _domain_diagnostics_from_counts(diagnostic_counts, diagnostic_codes),
        "curl_diagnostics": curl_diagnostics,
        "curl_diagnostics_summary": _curl_summary(curl_diagnostics),
        "dominant_failure": _dominant_failure_from_counts(diagnostic_counts),
    }
