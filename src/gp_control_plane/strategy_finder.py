from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .state import append_jsonl, now_iso
from .zapret2 import _cleanup_blockcheck_processes, _cleanup_nft_blockcheck_tables, _stop_process_group


CRITICAL_DOMAINS = ["youtube.com", "googlevideo.com", "discord.com", "discordcdn.com"]
DIAGNOSTIC_DOMAINS = ["web.telegram.org"]
COVERAGE_DOMAINS = [
    "youtu.be",
    "googleapis.com",
    "i.ytimg.com",
    "i9.ytimg.com",
    "yt3.ggpht.com",
    "yt3.googleusercontent.com",
    "yt4.ggpht.com",
    "yt4.googleusercontent.com",
    "gvt1.com",
    "gstatic.com",
    "youtube-ui.l.google.com",
    "ytimg.l.google.com",
    "ytstatic.l.google.com",
    "play.google.com",
    "discord-attachments-uploads-prd.storage.googleapis.com",
    "dis.gd",
    "discord.co",
    "discord.com",
    "discord.design",
    "discord.dev",
    "discord.gg",
    "discord.gift",
    "discord.gifts",
    "discord.media",
    "discord.new",
    "discord.store",
    "discord.tools",
    "discordapp.com",
    "discordapp.net",
    "discordmerch.com",
    "discordpartygames.com",
    "discord-activities.com",
    "discordactivities.com",
    "discordsays.com",
    "discordstatus.com",
    "speedtest.net",
    "cloudflare-ech.com",
]

ATTEMPT_TIMEOUT_ESTIMATE_MS = 2100
_ATTEMPT_PLAN_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}
_ATTEMPT_RE = re.compile(r"^-\s+curl_test_")
_SCRIPT_RE = re.compile(r"^\*\s+script\s+:\s+(.+)$")


def domain_sets() -> dict[str, list[str]]:
    return {
        "critical": list(CRITICAL_DOMAINS),
        "diagnostic": list(DIAGNOSTIC_DOMAINS),
        "coverage": list(COVERAGE_DOMAINS),
    }


def run_standard_discovery(
    domains: list[str],
    state_dir: Path,
    timeout_seconds: int,
    include_quic: bool = True,
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    return _run_blockcheck_live(
        state_dir=state_dir,
        kind="standard-discovery",
        domains=domains,
        timeout_seconds=timeout_seconds,
        test="standard",
        enable_tls=True,
        enable_quic=include_quic,
        stop_event=stop_event,
    )


def run_multi_domain_discovery(
    domains: list[str],
    state_dir: Path,
    timeout_seconds: int,
    include_quic: bool = True,
    scan_level: str = "standard",
    curl_parallelism: int = 4,
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    return _run_multidomain_blockcheck_live(
        state_dir=state_dir,
        domains=domains,
        timeout_seconds=timeout_seconds,
        include_quic=include_quic,
        scan_level=scan_level,
        curl_parallelism=curl_parallelism,
        stop_event=stop_event,
    )


def run_custom_verification(
    candidate: dict[str, Any],
    domains: list[str],
    state_dir: Path,
    timeout_seconds: int,
    include_quic: bool = True,
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    args = str(candidate.get("args") or "").strip()
    protocol = str(candidate.get("protocol") or "tls")
    if not args:
        raise ValueError("candidate args are required")

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        lists = _write_custom_lists(tmp, args, protocol, include_quic)
        run = _run_blockcheck_live(
            state_dir=state_dir,
            kind="custom-verification",
            domains=domains,
            timeout_seconds=timeout_seconds,
            test="custom",
            enable_tls=protocol in {"tls", "http"},
            enable_quic=include_quic and protocol == "quic",
            list_paths=lists,
            candidate_id=str(candidate.get("id") or ""),
            stop_event=stop_event,
        )
    _update_candidate_verification(state_dir, candidate, run)
    return run


def read_candidates(state_dir: Path) -> list[dict[str, Any]]:
    path = _finder_dir(state_dir) / "candidates.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def read_runs(state_dir: Path, limit: int = 50) -> list[dict[str, Any]]:
    path = _finder_dir(state_dir) / "runs.jsonl"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    result: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        if line.strip():
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                result.append(parsed)
    return result


def latest_log_tail(state_dir: Path, max_lines: int = 200) -> dict[str, Any]:
    for run in reversed(read_runs(state_dir, limit=200)):
        stdout_log = Path(str(run.get("stdout_log") or ""))
        if not stdout_log.exists():
            continue
        lines = stdout_log.read_text(encoding="utf-8", errors="replace").splitlines()
        stderr_log = Path(str(run.get("stderr_log") or ""))
        stderr_lines = stderr_log.read_text(encoding="utf-8", errors="replace").splitlines() if stderr_log.exists() else []
        stdout = "\n".join(lines)
        return {
            "run_id": run.get("id"),
            "kind": run.get("kind"),
            "status": run.get("status"),
            "stdout_tail": "\n".join(lines[-max_lines:]),
            "stderr_tail": "\n".join(stderr_lines[-max_lines:]),
            "stdout_log": str(stdout_log),
            "stderr_log": str(stderr_log) if stderr_log else "",
            "progress": progress_from_stdout(stdout, run),
        }
    return {
        "run_id": None,
        "kind": None,
        "status": None,
        "stdout_tail": "",
        "stderr_tail": "",
        "progress": progress_from_stdout("", {}),
    }


def find_candidate(state_dir: Path, candidate_id: str) -> dict[str, Any]:
    for candidate in read_candidates(state_dir):
        if candidate.get("id") == candidate_id:
            return candidate
    raise ValueError(f"candidate not found: {candidate_id}")


def parse_blockcheck_stdout(stdout: str) -> dict[str, Any]:
    sections = _summary_sections(stdout)
    summary = sections["summary"]
    common = sections["common"]
    live_summary = _live_available_lines(stdout)
    candidates = _dedupe_candidate_lines([*_candidate_lines(summary, scope="domain"), *_candidate_lines(live_summary, scope="domain")])
    common_candidates = _candidate_lines(common, scope="common")
    results = [_parse_result_line(line) for line in summary if _parse_result_line(line)]
    common_results = [_parse_result_line(line) for line in common if _parse_result_line(line)]
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
    }


def upsert_candidates(state_dir: Path, parsed: dict[str, Any], run: dict[str, Any]) -> list[dict[str, Any]]:
    existing = {str(item.get("id")): item for item in read_candidates(state_dir)}
    now = now_iso()
    for raw in parsed.get("candidates") or []:
        if not isinstance(raw, dict):
            continue
        candidate_id = candidate_id_for(str(raw.get("protocol")), str(raw.get("args")))
        item = existing.get(candidate_id) or {
            "id": candidate_id,
            "protocol": raw.get("protocol"),
            "args": raw.get("args"),
            "status": "candidate",
            "first_seen_at": now,
            "seen": [],
            "verifications": [],
        }
        item["last_seen_at"] = now
        seen = item.setdefault("seen", [])
        if isinstance(seen, list):
            seen.append(
                {
                    "run_id": run["id"],
                    "domain": raw.get("domain"),
                    "test": raw.get("test"),
                    "ip_version": raw.get("ip_version"),
                    "seen_at": now,
                }
            )
        existing[candidate_id] = item
    for raw in parsed.get("common_candidates") or []:
        if not isinstance(raw, dict):
            continue
        candidate_id = candidate_id_for(str(raw.get("protocol")), str(raw.get("args")))
        item = existing.get(candidate_id) or {
            "id": candidate_id,
            "protocol": raw.get("protocol"),
            "args": raw.get("args"),
            "status": "candidate",
            "first_seen_at": now,
            "seen": [],
            "verifications": [],
        }
        item["last_seen_at"] = now
        common_seen = item.setdefault("common_seen", [])
        if isinstance(common_seen, list):
            common_seen.append(
                {
                    "run_id": run["id"],
                    "domains": run.get("domains") or [],
                    "test": raw.get("test"),
                    "ip_version": raw.get("ip_version"),
                    "seen_at": now,
                }
            )
        existing[candidate_id] = item
    candidates = sorted(existing.values(), key=lambda item: str(item.get("last_seen_at") or ""), reverse=True)
    _write_candidates(state_dir, candidates)
    return candidates


def candidate_id_for(protocol: str, args: str) -> str:
    digest = hashlib.sha256(f"{protocol}\n{args}".encode("utf-8")).hexdigest()[:12]
    return f"{protocol}-{digest}"


def _run_blockcheck_live(
    state_dir: Path,
    kind: str,
    domains: list[str],
    timeout_seconds: int,
    test: str,
    enable_tls: bool,
    enable_quic: bool,
    list_paths: dict[str, Path] | None = None,
    candidate_id: str = "",
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    blockcheck = shutil.which("blockcheck2.sh") or shutil.which("blockcheck.sh")
    if not blockcheck:
        raise RuntimeError("blockcheck2.sh/blockcheck.sh not found in PATH")
    clean_domains = _clean_domains(domains)
    full_env = os.environ.copy()
    full_env.update(
        {
            "BATCH": "1",
            "DOMAINS": " ".join(clean_domains),
            "IPVS": "4",
            "TEST": test,
            "SKIP_DNSCHECK": "1",
            "ENABLE_HTTP": "0",
            "ENABLE_HTTPS_TLS12": "1" if enable_tls else "0",
            "ENABLE_HTTPS_TLS13": "0",
            "ENABLE_HTTP3": "1" if enable_quic else "0",
        }
    )
    for key, value in (list_paths or {}).items():
        full_env[key] = str(value)

    root = _finder_dir(state_dir)
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    run_id = f"{now_iso().replace(':', '').replace('-', '')}-{uuid.uuid4().hex[:8]}"
    stdout_log = logs / f"{run_id}.{kind}.stdout.log"
    stderr_log = logs / f"{run_id}.{kind}.stderr.log"
    attempt_plan = _standard_attempt_plan(
        domains=clean_domains,
        test=test,
        enable_http=False,
        enable_tls=enable_tls,
        enable_tls13=False,
        enable_quic=enable_quic,
    )
    started = {
        "id": run_id,
        "kind": kind,
        "candidate_id": candidate_id,
        "status": "running",
        "timestamp": now_iso(),
        "domains": clean_domains,
        "returncode": None,
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "candidate_count": 0,
        "test": test,
        "enable_tls": enable_tls,
        "enable_quic": enable_quic,
        "attempt_plan": attempt_plan,
    }
    append_jsonl(root / "runs.jsonl", started)

    status = "success"
    returncode: int | None = None
    timed_out = False
    stopped = False
    with stdout_log.open("w", encoding="utf-8") as stdout_handle, stderr_log.open("w", encoding="utf-8") as stderr_handle:
        process = subprocess.Popen(
            [blockcheck],
            text=True,
            stdout=stdout_handle,
            stderr=stderr_handle,
            env=full_env,
            start_new_session=hasattr(os, "setsid"),
        )
        deadline = None if timeout_seconds <= 0 else time.monotonic() + timeout_seconds
        while True:
            if stop_event is not None and stop_event.is_set():
                stopped = True
                status = "stopped"
                _stop_process_group(process)
                _cleanup_blockcheck_processes()
                _cleanup_nft_blockcheck_tables()
                returncode = process.returncode
                break
            wait_timeout = 1.0
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    status = "timeout"
                    _stop_process_group(process)
                    _cleanup_blockcheck_processes()
                    _cleanup_nft_blockcheck_tables()
                    returncode = process.returncode
                    break
                wait_timeout = min(1.0, remaining)
            try:
                returncode = process.wait(timeout=wait_timeout)
                if returncode != 0:
                    status = "failed"
                break
            except subprocess.TimeoutExpired:
                continue

    stdout = stdout_log.read_text(encoding="utf-8", errors="replace")
    parsed = parse_blockcheck_stdout(stdout)
    run = {
        "id": run_id,
        "kind": kind,
        "candidate_id": candidate_id,
        "status": status,
        "timestamp": now_iso(),
        "domains": clean_domains,
        "returncode": returncode,
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "summary": parsed["summary"],
        "results": parsed["results"],
        "candidate_count": len(parsed["candidates"]),
        "common_candidate_count": len(parsed["common_candidates"]),
        "timed_out": timed_out,
        "stopped": stopped,
        "timeout_seconds": timeout_seconds,
        "test": test,
        "enable_tls": enable_tls,
        "enable_quic": enable_quic,
        "attempt_plan": attempt_plan,
    }
    run["progress"] = progress_from_stdout(stdout, run)
    if kind in {"standard-discovery", "multi-domain-discovery"}:
        candidates = upsert_candidates(state_dir, parsed, run)
        run["total_candidates"] = len(candidates)
    append_jsonl(root / "runs.jsonl", run)
    return run


def _run_multidomain_blockcheck_live(
    state_dir: Path,
    domains: list[str],
    timeout_seconds: int,
    include_quic: bool,
    scan_level: str,
    curl_parallelism: int,
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    blockcheck = shutil.which("blockcheck2.sh") or shutil.which("blockcheck.sh")
    if not blockcheck:
        raise RuntimeError("blockcheck2.sh/blockcheck.sh not found in PATH")
    clean_domains = _clean_domains(domains)
    normalized_scan_level = scan_level if scan_level in {"quick", "standard", "force"} else "standard"
    blockcheck_path = _resolve_blockcheck_script(Path(blockcheck))
    zapret_base = blockcheck_path.parent
    normalized_parallelism = _bounded_int(curl_parallelism, default=4, minimum=1, maximum=10)

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        runner = _write_multidomain_runner(tmp, blockcheck_path)
        full_env = os.environ.copy()
        full_env.update(
            {
                "BATCH": "1",
                "DOMAINS": " ".join(clean_domains),
                "IPVS": "4",
                "TEST": "standard",
                "SKIP_DNSCHECK": "1",
                "SKIP_IPBLOCK": "1",
                "ENABLE_HTTP": "0",
                "ENABLE_HTTPS_TLS12": "1",
                "ENABLE_HTTPS_TLS13": "0",
                "ENABLE_HTTP3": "1" if include_quic else "0",
                "SCANLEVEL": normalized_scan_level,
                "GP_MD_CURL_PARALLELISM": str(normalized_parallelism),
                "ZAPRET_BASE": str(zapret_base),
                "ZAPRET_RW": str(zapret_base),
            }
        )
        return _run_blockcheck_command_live(
            command=[str(runner)],
            env=full_env,
            state_dir=state_dir,
            kind="multi-domain-discovery",
            domains=clean_domains,
            timeout_seconds=timeout_seconds,
            test="standard",
            enable_tls=True,
            enable_quic=include_quic,
            scan_level=normalized_scan_level,
            curl_parallelism=normalized_parallelism,
            stop_event=stop_event,
        )


def _run_blockcheck_command_live(
    command: list[str],
    env: dict[str, str],
    state_dir: Path,
    kind: str,
    domains: list[str],
    timeout_seconds: int,
    test: str,
    enable_tls: bool,
    enable_quic: bool,
    scan_level: str = "",
    curl_parallelism: int | None = None,
    candidate_id: str = "",
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    root = _finder_dir(state_dir)
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    run_id = f"{now_iso().replace(':', '').replace('-', '')}-{uuid.uuid4().hex[:8]}"
    stdout_log = logs / f"{run_id}.{kind}.stdout.log"
    stderr_log = logs / f"{run_id}.{kind}.stderr.log"
    attempt_plan = _standard_attempt_plan(
        domains=domains,
        test=test,
        enable_http=False,
        enable_tls=enable_tls,
        enable_tls13=False,
        enable_quic=enable_quic,
    )
    started = {
        "id": run_id,
        "kind": kind,
        "candidate_id": candidate_id,
        "status": "running",
        "timestamp": now_iso(),
        "domains": domains,
        "returncode": None,
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "candidate_count": 0,
        "test": test,
        "enable_tls": enable_tls,
        "enable_quic": enable_quic,
        "scan_level": scan_level,
        "curl_parallelism": curl_parallelism,
        "attempt_plan": attempt_plan,
    }
    append_jsonl(root / "runs.jsonl", started)

    status = "success"
    returncode: int | None = None
    timed_out = False
    stopped = False
    with stdout_log.open("w", encoding="utf-8") as stdout_handle, stderr_log.open("w", encoding="utf-8") as stderr_handle:
        process = subprocess.Popen(
            command,
            text=True,
            stdout=stdout_handle,
            stderr=stderr_handle,
            env=env,
            start_new_session=hasattr(os, "setsid"),
        )
        deadline = None if timeout_seconds <= 0 else time.monotonic() + timeout_seconds
        while True:
            if stop_event is not None and stop_event.is_set():
                stopped = True
                status = "stopped"
                _stop_process_group(process)
                _cleanup_blockcheck_processes()
                _cleanup_nft_blockcheck_tables()
                returncode = process.returncode
                break
            wait_timeout = 1.0
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    status = "timeout"
                    _stop_process_group(process)
                    _cleanup_blockcheck_processes()
                    _cleanup_nft_blockcheck_tables()
                    returncode = process.returncode
                    break
                wait_timeout = min(1.0, remaining)
            try:
                returncode = process.wait(timeout=wait_timeout)
                if returncode != 0:
                    status = "failed"
                break
            except subprocess.TimeoutExpired:
                continue

    stdout = stdout_log.read_text(encoding="utf-8", errors="replace")
    parsed = parse_blockcheck_stdout(stdout)
    run = {
        "id": run_id,
        "kind": kind,
        "candidate_id": candidate_id,
        "status": status,
        "timestamp": now_iso(),
        "domains": domains,
        "returncode": returncode,
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "summary": parsed["summary"],
        "results": parsed["results"],
        "candidate_count": len(parsed["candidates"]),
        "common_candidate_count": len(parsed["common_candidates"]),
        "timed_out": timed_out,
        "stopped": stopped,
        "timeout_seconds": timeout_seconds,
        "test": test,
        "enable_tls": enable_tls,
        "enable_quic": enable_quic,
        "scan_level": scan_level,
        "curl_parallelism": curl_parallelism,
        "attempt_plan": attempt_plan,
    }
    run["progress"] = progress_from_stdout(stdout, run)
    if kind in {"standard-discovery", "multi-domain-discovery"}:
        candidates = upsert_candidates(state_dir, parsed, run)
        run["total_candidates"] = len(candidates)
    append_jsonl(root / "runs.jsonl", run)
    return run


def _update_candidate_verification(state_dir: Path, candidate: dict[str, Any], run: dict[str, Any]) -> None:
    candidates = read_candidates(state_dir)
    candidate_id = str(candidate.get("id") or "")
    for item in candidates:
        if item.get("id") != candidate_id:
            continue
        verifications = item.setdefault("verifications", [])
        if isinstance(verifications, list):
            results = run.get("results") if isinstance(run.get("results"), list) else []
            total = len(results)
            ok = sum(1 for result in results if str(result.get("result") or "").startswith("nfqws2 "))
            verifications.append(
                {
                    "run_id": run["id"],
                    "verified_at": run["timestamp"],
                    "domains": run["domains"],
                    "total": total,
                    "success": ok,
                    "success_rate": (ok / total) if total else 0.0,
                }
            )
        break
    _write_candidates(state_dir, candidates)


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
    attempt_plan = _attempt_plan_for_run(run, current_script)
    script_order = [str(item) for item in attempt_plan.get("script_order") or []]
    script_attempt_totals = attempt_plan.get("scripts") if isinstance(attempt_plan.get("scripts"), dict) else {}
    attempt_total = int(attempt_plan.get("total") or 0)
    current_script_attempted = attempts_by_script.get(current_script, 0)
    current_script_attempt_total = int(script_attempt_totals.get(current_script) or 0)
    remaining_attempts = max(0, attempt_total - attempted) if attempt_total else None
    script_total = len(script_order) if script_order else (_standard_script_total() if current_script.startswith("standard/") else 0)
    script_index = _standard_script_index(current_script, script_order) if current_script else 0
    if script_total and script_index > script_total:
        script_index = script_total
    status = str(run.get("status") or "")
    finished = status in {"success", "failed", "timeout", "stopped"}
    completed = status == "success"
    if completed and script_total:
        script_index = script_total
    if attempt_total:
        percent = 100.0 if completed else min(100.0, (attempted / attempt_total) * 100.0)
    else:
        percent = (script_index / script_total * 100.0) if script_total else None
    elapsed = _elapsed_seconds(run.get("timestamp"))
    eta_parallelism = _eta_parallelism_for_run(run)
    eta = _eta_from_remaining_attempts(attempted, attempt_total, completed, eta_parallelism)
    return {
        "attempted": attempted,
        "attempt_total": attempt_total,
        "remaining_attempts": remaining_attempts,
        "successful": successful,
        "current_script": current_script,
        "current_script_attempted": current_script_attempted,
        "current_script_attempt_total": current_script_attempt_total,
        "script_index": script_index,
        "script_total": script_total,
        "percent": percent,
        "elapsed_seconds": elapsed,
        "eta_seconds": eta,
        "eta_estimate_ms_per_attempt": ATTEMPT_TIMEOUT_ESTIMATE_MS,
        "eta_parallelism": eta_parallelism,
        "attempt_plan_source": attempt_plan.get("source") or "",
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
    )


def _standard_attempt_plan(
    domains: list[str],
    test: str = "standard",
    enable_http: bool = False,
    enable_tls: bool = True,
    enable_tls13: bool = False,
    enable_quic: bool = True,
    root: Path | None = None,
) -> dict[str, Any]:
    if test != "standard":
        return _empty_attempt_plan(test)
    root = root or _blockcheck_test_dir(test)
    if not root.exists():
        return _empty_attempt_plan(test)
    scripts = _standard_scripts(root)
    domain_count = len(_clean_domains(domains))
    fingerprint = tuple((path.name, path.stat().st_mtime_ns, path.stat().st_size) for path in scripts)
    key = (
        str(root),
        fingerprint,
        domain_count,
        bool(enable_http),
        bool(enable_tls),
        bool(enable_tls13),
        bool(enable_quic),
    )
    cached = _ATTEMPT_PLAN_CACHE.get(key)
    if cached:
        return cached

    enabled_functions: list[str] = []
    if enable_http:
        enabled_functions.append("pktws_check_http")
    if enable_tls:
        enabled_functions.append("pktws_check_https_tls12")
    if enable_tls13:
        enabled_functions.append("pktws_check_https_tls13")
    if enable_quic:
        enabled_functions.append("pktws_check_http3")

    script_totals: dict[str, int] = {}
    script_order: list[str] = []
    source = "shell"
    for script in scripts:
        name = f"{test}/{script.name}"
        script_order.append(name)
        per_domain = 0
        for function_name in enabled_functions:
            counted = _count_script_function_attempts(root, script, function_name)
            if counted is None:
                source = "static"
                per_domain = _count_script_attempts_static(script)
                break
            per_domain += counted
        script_totals[name] = per_domain * domain_count

    total = sum(script_totals.values())
    plan = {
        "test": test,
        "total": total,
        "scripts": script_totals,
        "script_order": script_order,
        "domain_count": domain_count,
        "source": source if total else "",
    }
    _ATTEMPT_PLAN_CACHE[key] = plan
    return plan


def _empty_attempt_plan(test: str) -> dict[str, Any]:
    return {"test": test, "total": 0, "scripts": {}, "script_order": [], "domain_count": 0, "source": ""}


def _blockcheck_test_dir(test: str) -> Path:
    base = Path(os.environ.get("GP_BLOCKCHECK2D", "/opt/zapret2/blockcheck2.d"))
    return base / test


def _standard_scripts(root: Path | None = None) -> list[Path]:
    root = root or _blockcheck_test_dir("standard")
    if not root.exists():
        return []
    return sorted(path for path in root.glob("*.sh") if path.is_file() and path.name != "def.inc")


def _count_script_function_attempts(root: Path, script: Path, function_name: str) -> int | None:
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", function_name):
        return None
    shell = shutil.which("sh")
    if not shell:
        return None
    probe = "\n".join(
        [
            f"TESTDIR={shlex.quote(str(root))}",
            "SCANLEVEL=force",
            "IPV=4",
            "IPVV=",
            "UNAME=Linux",
            "pktws_curl_test_update() { echo __GP_ATTEMPT__; return 1; }",
            f". {shlex.quote(str(script))}",
            f"if command -v {function_name} >/dev/null 2>&1; then {function_name} curl_test_probe example.com; fi",
        ]
    )
    try:
        result = subprocess.run(
            [shell, "-c", probe],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    count = sum(1 for line in result.stdout.splitlines() if line.strip() == "__GP_ATTEMPT__")
    if result.returncode != 0 and count == 0:
        return None
    return count


def _count_script_attempts_static(script: Path) -> int:
    try:
        text = script.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 0
    loop_stack: list[int] = []
    total = 0
    for raw_line in text.splitlines():
        line = _strip_shell_comment(raw_line).strip()
        if not line:
            continue
        loop_match = re.match(r"^for\s+\w+\s+in\s+(.+?);?\s+do\s*$", line)
        if loop_match:
            loop_stack.append(max(1, _shell_word_count(loop_match.group(1))))
            continue
        if "pktws_curl_test_update" in line:
            multiplier = 1
            for value in loop_stack:
                multiplier *= value
            total += multiplier
        if line == "done" or line.endswith("; done"):
            if loop_stack:
                loop_stack.pop()
    return total


def _strip_shell_comment(line: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    result: list[str] = []
    for char in line:
        if escaped:
            result.append(char)
            escaped = False
            continue
        if char == "\\":
            result.append(char)
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            break
        result.append(char)
    return "".join(result)


def _shell_word_count(value: str) -> int:
    try:
        return len(shlex.split(value))
    except ValueError:
        return len([part for part in value.split() if part])


def _eta_parallelism_for_run(run: dict[str, Any]) -> int:
    if str(run.get("kind") or "") != "multi-domain-discovery":
        return 1
    return _bounded_int(run.get("curl_parallelism"), default=4, minimum=1, maximum=10)


def _eta_from_remaining_attempts(attempted: int, attempt_total: int, completed: bool, parallelism: int = 1) -> int | None:
    if completed:
        return 0
    if not attempt_total:
        return None
    remaining = max(0, attempt_total - attempted)
    if remaining <= 0:
        return 0
    effective_remaining = (remaining + max(1, parallelism) - 1) // max(1, parallelism)
    return max(0, int((effective_remaining * ATTEMPT_TIMEOUT_ESTIMATE_MS) / 1000))


def _truthy(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _summary_sections(stdout: str) -> dict[str, list[str]]:
    lines = [line.strip() for line in stdout.splitlines()]
    summary: list[str] = []
    common: list[str] = []
    section = ""
    for index, line in enumerate(lines):
        if line == "* SUMMARY":
            section = "summary"
            continue
        if line == "* COMMON":
            section = "common"
            continue
        if not line:
            continue
        if section == "summary":
            summary.append(line)
        elif section == "common":
            common.append(line)
    if summary or common:
        return {"summary": summary, "common": common}
    return {"summary": _live_success_lines(stdout), "common": []}


def _summary_lines(stdout: str) -> list[str]:
    return _summary_sections(stdout)["summary"]


def _dedupe_candidate_lines(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for candidate in candidates:
        key = (
            str(candidate.get("scope") or ""),
            str(candidate.get("test") or ""),
            str(candidate.get("ip_version") or ""),
            str(candidate.get("domain") or ""),
            str(candidate.get("args") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def _candidate_lines(summary: list[str], scope: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for line in summary:
        parsed = _parse_result_line(line)
        if not parsed:
            continue
        raw_result = str(parsed.get("result") or "")
        if not raw_result.startswith("nfqws2 ") or raw_result == "nfqws2 not working":
            continue
        args = raw_result.removeprefix("nfqws2 ").strip()
        candidates.append(
            {
                "domain": parsed["domain"],
                "test": parsed["test"],
                "ip_version": parsed["ip_version"],
                "protocol": _protocol_from_test(str(parsed["test"])),
                "args": args,
                "raw": line,
                "scope": scope,
            }
        )
    return candidates


def _parse_result_line(line: str) -> dict[str, Any] | None:
    left, sep, result = line.partition(" : ")
    if not sep:
        return None
    parts = left.split()
    if len(parts) == 2 and parts[1].startswith("ipv"):
        domain = ""
    elif len(parts) >= 3 and parts[1].startswith("ipv"):
        domain = parts[2]
    else:
        return None
    return {
        "test": parts[0],
        "ip_version": parts[1].removeprefix("ipv"),
        "domain": domain,
        "result": result.strip(),
    }


def _live_success_lines(stdout: str) -> list[str]:
    result: list[str] = []
    pattern = re.compile(
        r"^!!!!!\s+(?P<test>\S+): working strategy found for ipv(?P<ip_version>\d+)\s+"
        r"(?P<domain>\S+)\s+:\s+nfqws2\s+(?P<args>.*?)\s+!!!!!$"
    )
    for line in stdout.splitlines():
        match = pattern.match(line.strip())
        if match:
            result.append(
                f"{match.group('test')} ipv{match.group('ip_version')} {match.group('domain')} : "
                f"nfqws2 {match.group('args').strip()}"
            )
    return result


def _live_available_lines(stdout: str) -> list[str]:
    result: list[str] = []
    pending: str | None = None
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        attempt = _live_attempt_line(line)
        if attempt:
            pending = attempt
            continue
        if line == "!!!!! AVAILABLE !!!!!" and pending:
            result.append(pending)
            pending = None
            continue
        if line.startswith("UNAVAILABLE") or line.startswith("FAILED"):
            pending = None
    return result


def _live_attempt_line(line: str) -> str | None:
    if not line.startswith("- "):
        return None
    normalized = line[2:].strip()
    parsed = _parse_result_line(normalized)
    if not parsed:
        return None
    result = str(parsed.get("result") or "")
    if result.startswith("nfqws2 ") and result != "nfqws2 not working":
        return normalized
    return None


def _standard_script_total() -> int:
    return len(_standard_scripts())


def _standard_script_index(current_script: str, script_order: list[str] | None = None) -> int:
    if not current_script.startswith("standard/"):
        return 0
    scripts = script_order or [f"standard/{path.name}" for path in _standard_scripts()]
    try:
        return scripts.index(current_script) + 1
    except ValueError:
        return 0


def _elapsed_seconds(value: Any) -> int | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        started = datetime.fromisoformat(text)
    except ValueError:
        return None
    if started.tzinfo is None:
        now = datetime.now()
    else:
        now = datetime.now(started.tzinfo)
    return max(0, int((now - started).total_seconds()))


def _protocol_from_test(test: str) -> str:
    if "http3" in test:
        return "quic"
    if "http_" in test and "https" not in test:
        return "http"
    return "tls"


def _write_custom_lists(root: Path, args: str, protocol: str, include_quic: bool) -> dict[str, Path]:
    empty = root / "empty.txt"
    empty.write_text("", encoding="utf-8")
    tls = root / "list_https_tls12.txt"
    quic = root / "list_quic.txt"
    tls.write_text((args + "\n") if protocol == "tls" else "", encoding="utf-8")
    quic.write_text((args + "\n") if protocol == "quic" and include_quic else "", encoding="utf-8")
    return {
        "LIST_HTTP": empty,
        "LIST_HTTPS_TLS12": tls,
        "LIST_HTTPS_TLS13": empty,
        "LIST_QUIC": quic,
    }


def _write_multidomain_runner(root: Path, blockcheck: Path) -> Path:
    source = blockcheck.read_text(encoding="utf-8", errors="replace")
    marker = "\nfsleep_setup\n"
    if marker not in source:
        raise RuntimeError("unsupported blockcheck2.sh layout: main marker not found")
    prefix = source.split(marker, 1)[0]
    runner = root / "gp-multidomain-blockcheck.sh"
    runner.write_text(prefix + MULTIDOMAIN_BLOCKCHECK_MAIN, encoding="utf-8")
    runner.chmod(0o700)
    return runner


def _resolve_blockcheck_script(path: Path) -> Path:
    current = path.resolve()
    seen: set[Path] = set()
    for _ in range(5):
        if current in seen:
            break
        seen.add(current)
        try:
            text = current.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return current
        if "\nfsleep_setup\n" in text:
            return current
        target = _exec_target_from_shell_wrapper(text)
        if not target:
            return current
        candidate = Path(target)
        if not candidate.is_absolute():
            candidate = current.parent / candidate
        current = candidate.resolve()
    return current


def _exec_target_from_shell_wrapper(text: str) -> str:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("exec "):
            continue
        try:
            parts = shlex.split(line)
        except ValueError:
            continue
        for part in parts[1:]:
            if part.endswith(("blockcheck2.sh", "blockcheck.sh")):
                return part
    return ""


MULTIDOMAIN_BLOCKCHECK_MAIN = r'''

gp_md_primary_domain()
{
	local d
	for d in $DOMAINS; do
		echo "$d"
		return
	done
}

gp_md_resolve_all_ips()
{
	local d ips all_ips
	for d in $DOMAINS; do
		mdig_resolve_all $IPV ips "$d"
		all_ips="${all_ips:+$all_ips }$ips"
	done
	echo "$all_ips" | tr ' ' '\n' | sort -u | tr '\n' ' '
}

gp_md_parallel_limit()
{
	local n="${GP_MD_CURL_PARALLELISM:-4}"
	case "$n" in
		""|*[!0-9]*) n=4 ;;
	esac
	n=$((n + 0))
	[ "$n" -lt 1 ] && n=1
	[ "$n" -gt 16 ] && n=16
	echo "$n"
}

gp_md_out_file()
{
	echo "${PARALLEL_OUT}_md_$1.out"
}

gp_md_code_file()
{
	echo "${PARALLEL_OUT}_md_$1.code"
}

gp_md_run_domain_curl()
{
	# $1 - index
	# $2 - test function
	# $3 - domain
	local idx=$1 testf=$2 gp_domain="$3" code out codefile
	out="$(gp_md_out_file "$idx")"
	codefile="$(gp_md_code_file "$idx")"
	curl_test "$testf" "$gp_domain" >"$out" 2>&1
	code=$?
	echo "$code" >"$codefile"
	return 0
}

gp_md_collect_record()
{
	# $1 - pid:index:domain
	# $2 - test function
	# $3 - strategy text
	local record="$1" testf=$2 strategy_text="$3" pid rest idx gp_domain code out codefile
	pid="${record%%:*}"
	rest="${record#*:}"
	idx="${rest%%:*}"
	gp_domain="${rest#*:}"

	wait "$pid" 2>/dev/null
	out="$(gp_md_out_file "$idx")"
	codefile="$(gp_md_code_file "$idx")"
	code="$(cat "$codefile" 2>/dev/null)"
	[ -n "$code" ] || code=1

	echo "- $testf ipv$IPV $gp_domain : $PKTWSD ${WF:+$WF }$strategy_text"
	[ -f "$out" ] && cat "$out"
	rm -f "$out" "$codefile"
	if [ "$code" = 0 ]; then
		report_append "$gp_domain" "$testf ipv${IPV}" "$PKTWSD ${WF:+$WF }$strategy_text"
		return 0
	fi
	echo "GP-MULTIDOMAIN unavailable code=$code"
	return 1
}

pktws_curl_test_update()
{
	# $1 - curl test function
	# $2 - sample domain from the standard zapret2 script
	# $3+ - nfqws2 args
	local testf=$1 dom="$2" strategy ok=0 total=0 gp_domain idx=0 limit active=0 pending record
	shift
	shift
	strategy="$*"
	limit="$(gp_md_parallel_limit)"
	rm -f "${PARALLEL_OUT}_md_"*

	echo
	echo "- gp_multidomain_strategy ipv$IPV parallel=$limit : $PKTWSD ${WF:+$WF }$strategy"
	pktws_start "$@"
	for gp_domain in $DOMAINS; do
		idx=$(($idx + 1))
		total=$(($total + 1))
		gp_md_run_domain_curl "$idx" "$testf" "$gp_domain" &
		record="$!:$idx:$gp_domain"
		pending="${pending:+$pending }$record"
		active=$(($active + 1))
		if [ "$active" -ge "$limit" ]; then
			record="${pending%% *}"
			if [ "$record" = "$pending" ]; then
				pending=
			else
				pending="${pending#* }"
			fi
			gp_md_collect_record "$record" "$testf" "$strategy" && ok=$(($ok + 1))
			active=$(($active - 1))
		fi
	done
	while [ -n "$pending" ]; do
		record="${pending%% *}"
		if [ "$record" = "$pending" ]; then
			pending=
		else
			pending="${pending#* }"
		fi
		gp_md_collect_record "$record" "$testf" "$strategy" && ok=$(($ok + 1))
	done
	ws_kill
	rm -f "${PARALLEL_OUT}_md_"*
	echo "GP-MULTIDOMAIN result: $ok/$total domains available"
	[ "$ok" = "$total" ]
}

gp_md_run_protocol()
{
	# $1 - standard script function
	# $2 - curl test function
	# $3 - tcp/udp
	# $4 - port
	local func=$1 testf=$2 proto=$3 port=$4 ips primary
	primary="$(gp_md_primary_domain)"
	[ -n "$primary" ] || return 1
	ips="$(gp_md_resolve_all_ips)"
	[ -n "$ips" ] || {
		echo "GP-MULTIDOMAIN no resolved ip addresses for $proto/$port"
		return 1
	}

	echo
	echo "GP-MULTIDOMAIN preparing $PKTWSD redirection for $proto/$port"
	case "$proto" in
		tcp) pktws_ipt_prepare_tcp "$port" "$ips" ;;
		udp) pktws_ipt_prepare_udp "$port" "$ips" ;;
		*) return 1 ;;
	esac
	test_runner "$func" "$testf" "$primary"
	echo "GP-MULTIDOMAIN clearing $PKTWSD redirection for $proto/$port"
	case "$proto" in
		tcp) pktws_ipt_unprepare_tcp "$port" ;;
		udp) pktws_ipt_unprepare_udp "$port" ;;
	esac
}

fsleep_setup
fix_sbin_path
check_system
check_already
[ "$UNAME" != CYGWIN  -a "$SKIP_PKTWS" != 1 ] && require_root
check_prerequisites
trap sigint_cleanup INT
check_dns
check_virt
ask_params
trap - INT

PID=
NREPORT=
unset WF
trap sigint INT
trap sigsilent PIPE
trap sigsilent HUP
for IPV in $IPVS; do
	configure_ip_version
	[ "$ENABLE_HTTPS_TLS12" = 1 ] && gp_md_run_protocol pktws_check_https_tls12 curl_test_https_tls12 tcp "$HTTPS_PORT"
	[ "$ENABLE_HTTP3" = 1 ] && gp_md_run_protocol pktws_check_http3 curl_test_http3 udp "$QUIC_PORT"
done
trap - HUP
trap - PIPE
trap - INT

cleanup

echo
echo \* SUMMARY
report_print
[ "$DOMAINS_COUNT" -gt 1 ] && {
	echo
	echo \* COMMON
	result_intersection_print
}
'''


def _clean_domains(domains: list[str]) -> list[str]:
    result: list[str] = []
    for domain in domains:
        value = str(domain).strip()
        if value and value not in result:
            result.append(value)
    return result or list(CRITICAL_DOMAINS)


def _finder_dir(state_dir: Path) -> Path:
    path = state_dir / "strategy-finder"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_candidates(state_dir: Path, candidates: list[dict[str, Any]]) -> None:
    path = _finder_dir(state_dir) / "candidates.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(candidates, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
