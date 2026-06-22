from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
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
) -> dict[str, Any]:
    return _run_blockcheck_live(
        state_dir=state_dir,
        kind="standard-discovery",
        domains=domains,
        timeout_seconds=timeout_seconds,
        test="standard",
        enable_tls=True,
        enable_quic=include_quic,
    )


def run_custom_verification(
    candidate: dict[str, Any],
    domains: list[str],
    state_dir: Path,
    timeout_seconds: int,
    include_quic: bool = True,
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
        return {
            "run_id": run.get("id"),
            "kind": run.get("kind"),
            "status": run.get("status"),
            "stdout_tail": "\n".join(lines[-max_lines:]),
            "stderr_tail": "\n".join(stderr_lines[-max_lines:]),
            "stdout_log": str(stdout_log),
            "stderr_log": str(stderr_log) if stderr_log else "",
        }
    return {"run_id": None, "kind": None, "status": None, "stdout_tail": "", "stderr_tail": ""}


def find_candidate(state_dir: Path, candidate_id: str) -> dict[str, Any]:
    for candidate in read_candidates(state_dir):
        if candidate.get("id") == candidate_id:
            return candidate
    raise ValueError(f"candidate not found: {candidate_id}")


def parse_blockcheck_stdout(stdout: str) -> dict[str, Any]:
    summary = _summary_lines(stdout)
    candidates = _candidate_lines(summary)
    results = [_parse_result_line(line) for line in summary if _parse_result_line(line)]
    return {
        "summary": summary,
        "candidates": candidates,
        "results": results,
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
    with stdout_log.open("w", encoding="utf-8") as stdout_handle, stderr_log.open("w", encoding="utf-8") as stderr_handle:
        process = subprocess.Popen(
            [blockcheck],
            text=True,
            stdout=stdout_handle,
            stderr=stderr_handle,
            env=full_env,
            start_new_session=hasattr(os, "setsid"),
        )
        try:
            returncode = process.wait(timeout=timeout_seconds)
            if returncode != 0:
                status = "failed"
        except subprocess.TimeoutExpired:
            timed_out = True
            status = "timeout"
            _stop_process_group(process)
            _cleanup_blockcheck_processes()
            _cleanup_nft_blockcheck_tables()
            returncode = process.returncode

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
        "timed_out": timed_out,
        "timeout_seconds": timeout_seconds,
    }
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


def _summary_lines(stdout: str) -> list[str]:
    lines = [line.strip() for line in stdout.splitlines()]
    for index, line in enumerate(lines):
        if line == "* SUMMARY":
            return [item for item in lines[index + 1 :] if item]
    return [line for line in lines if " : " in line]


def _candidate_lines(summary: list[str]) -> list[dict[str, Any]]:
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
            }
        )
    return candidates


def _parse_result_line(line: str) -> dict[str, Any] | None:
    left, sep, result = line.partition(" : ")
    if not sep:
        return None
    parts = left.split()
    if len(parts) < 3 or not parts[1].startswith("ipv"):
        return None
    return {
        "test": parts[0],
        "ip_version": parts[1].removeprefix("ipv"),
        "domain": parts[2],
        "result": result.strip(),
    }


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
