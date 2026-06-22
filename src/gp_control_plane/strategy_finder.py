from __future__ import annotations

import hashlib
import json
import os
import re
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
    candidates = _candidate_lines(summary, scope="domain")
    common_candidates = _candidate_lines(common, scope="common")
    results = [_parse_result_line(line) for line in summary if _parse_result_line(line)]
    common_results = [_parse_result_line(line) for line in common if _parse_result_line(line)]
    return {
        "summary": summary,
        "common": common,
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
        deadline = time.monotonic() + timeout_seconds
        while True:
            if stop_event is not None and stop_event.is_set():
                stopped = True
                status = "stopped"
                _stop_process_group(process)
                _cleanup_blockcheck_processes()
                _cleanup_nft_blockcheck_tables()
                returncode = process.returncode
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                status = "timeout"
                _stop_process_group(process)
                _cleanup_blockcheck_processes()
                _cleanup_nft_blockcheck_tables()
                returncode = process.returncode
                break
            try:
                returncode = process.wait(timeout=min(1.0, remaining))
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
    }
    run["progress"] = progress_from_stdout(stdout, run)
    if kind == "standard-discovery":
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
    attempted = sum(1 for line in lines if re.match(r"^-\s+curl_test_", line.strip()))
    parsed = parse_blockcheck_stdout(stdout)
    successful = len(
        {
            (str(item.get("protocol") or ""), str(item.get("args") or ""))
            for item in [*parsed["candidates"], *parsed["common_candidates"]]
        }
    )
    scripts = [line.strip().removeprefix("* script :").strip() for line in lines if line.strip().startswith("* script :")]
    current_script = scripts[-1] if scripts else ""
    script_total = _standard_script_total()
    script_index = _standard_script_index(current_script) if current_script else 0
    if script_total and script_index > script_total:
        script_index = script_total
    finished = str(run.get("status") or "") in {"success", "failed", "timeout", "stopped"}
    if finished and script_total:
        script_index = script_total
    percent = (script_index / script_total * 100.0) if script_total else None
    elapsed = _elapsed_seconds(run.get("timestamp"))
    eta = None
    if not finished and elapsed is not None and script_total and script_index > 0:
        eta = max(0, int((elapsed / script_index) * (script_total - script_index)))
    elif finished:
        eta = 0
    return {
        "attempted": attempted,
        "successful": successful,
        "current_script": current_script,
        "script_index": script_index,
        "script_total": script_total,
        "percent": percent,
        "elapsed_seconds": elapsed,
        "eta_seconds": eta,
    }


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


def _standard_script_total() -> int:
    root = Path("/opt/zapret2/blockcheck2.d/standard")
    if not root.exists():
        return 0
    return len([path for path in root.glob("*.sh") if path.is_file()])


def _standard_script_index(current_script: str) -> int:
    root = Path("/opt/zapret2/blockcheck2.d/standard")
    if not root.exists() or not current_script.startswith("standard/"):
        return 0
    scripts = sorted(path.name for path in root.glob("*.sh") if path.is_file())
    current = current_script.split("/", 1)[1]
    try:
        return scripts.index(current) + 1
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
