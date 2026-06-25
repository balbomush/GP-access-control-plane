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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

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
DEFAULT_PAGE_LIMIT = 200
MAX_PAGE_LIMIT = 200


@dataclass(frozen=True)
class DiscoveryOptions:
    enable_http: bool = False
    enable_tls12: bool = True
    enable_tls13: bool = False
    enable_quic: bool = True
    scan_level: str = "standard"
    repeats: int = 1
    repeat_parallel: bool = False
    skip_dnscheck: bool = True
    skip_ipblock: bool = True

    def normalized(self) -> "DiscoveryOptions":
        scan_level = self.scan_level if self.scan_level in {"quick", "standard", "force"} else "standard"
        repeats = _bounded_int(self.repeats, default=1, minimum=1, maximum=10)
        if not any([self.enable_http, self.enable_tls12, self.enable_tls13, self.enable_quic]):
            raise ValueError("at least one protocol check must be enabled")
        return DiscoveryOptions(
            enable_http=bool(self.enable_http),
            enable_tls12=bool(self.enable_tls12),
            enable_tls13=bool(self.enable_tls13),
            enable_quic=bool(self.enable_quic),
            scan_level=scan_level,
            repeats=repeats,
            repeat_parallel=bool(self.repeat_parallel),
            skip_dnscheck=bool(self.skip_dnscheck),
            skip_ipblock=bool(self.skip_ipblock),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "enable_http": self.enable_http,
            "enable_tls12": self.enable_tls12,
            "enable_tls13": self.enable_tls13,
            "enable_quic": self.enable_quic,
            "scan_level": self.scan_level,
            "repeats": self.repeats,
            "repeat_parallel": self.repeat_parallel,
            "skip_dnscheck": self.skip_dnscheck,
            "skip_ipblock": self.skip_ipblock,
        }

    def to_blockcheck_env(self) -> dict[str, str]:
        options = self.normalized()
        return {
            "SKIP_DNSCHECK": "1" if options.skip_dnscheck else "0",
            "SKIP_IPBLOCK": "1" if options.skip_ipblock else "0",
            "ENABLE_HTTP": "1" if options.enable_http else "0",
            "ENABLE_HTTPS_TLS12": "1" if options.enable_tls12 else "0",
            "ENABLE_HTTPS_TLS13": "1" if options.enable_tls13 else "0",
            "ENABLE_HTTP3": "1" if options.enable_quic else "0",
            "SCANLEVEL": options.scan_level,
            "REPEATS": str(options.repeats),
            "PARALLEL": "1" if options.repeat_parallel else "0",
        }

    def to_run_fields(self) -> dict[str, Any]:
        options = self.normalized()
        mapping = options.to_mapping()
        return {
            **mapping,
            "enable_tls": options.enable_tls12,
            "discovery_options": mapping,
        }


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
    enable_http: bool = False,
    enable_tls12: bool = True,
    enable_tls13: bool = False,
    scan_level: str = "standard",
    repeats: int = 1,
    repeat_parallel: bool = False,
    skip_dnscheck: bool = True,
    skip_ipblock: bool = True,
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    options = DiscoveryOptions(
        enable_http=enable_http,
        enable_tls12=enable_tls12,
        enable_tls13=enable_tls13,
        enable_quic=include_quic,
        scan_level=scan_level,
        repeats=repeats,
        repeat_parallel=repeat_parallel,
        skip_dnscheck=skip_dnscheck,
        skip_ipblock=skip_ipblock,
    ).normalized()
    return _run_blockcheck_live(
        state_dir=state_dir,
        kind="standard-discovery",
        domains=domains,
        timeout_seconds=timeout_seconds,
        test="standard",
        options=options,
        stop_event=stop_event,
    )


def run_multi_domain_discovery(
    domains: list[str],
    state_dir: Path,
    timeout_seconds: int,
    include_quic: bool = True,
    enable_http: bool = False,
    enable_tls12: bool = True,
    enable_tls13: bool = False,
    scan_level: str = "standard",
    repeats: int = 1,
    repeat_parallel: bool = False,
    skip_dnscheck: bool = True,
    skip_ipblock: bool = True,
    curl_parallelism: int = 4,
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    options = DiscoveryOptions(
        enable_http=enable_http,
        enable_tls12=enable_tls12,
        enable_tls13=enable_tls13,
        enable_quic=include_quic,
        scan_level=scan_level,
        repeats=repeats,
        repeat_parallel=repeat_parallel,
        skip_dnscheck=skip_dnscheck,
        skip_ipblock=skip_ipblock,
    ).normalized()
    return _run_multidomain_blockcheck_live(
        state_dir=state_dir,
        domains=domains,
        timeout_seconds=timeout_seconds,
        options=options,
        curl_parallelism=curl_parallelism,
        stop_event=stop_event,
    )


def read_candidates(state_dir: Path) -> list[dict[str, Any]]:
    path = _finder_dir(state_dir) / "candidates.json"
    if not path.exists():
        return []
    return list(_iter_candidate_file(path))


def read_candidate_page(
    state_dir: Path,
    *,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
    query: str = "",
    view: str = "domain",
    domains: list[str] | None = None,
    domain: str = "",
) -> dict[str, Any]:
    path = _finder_dir(state_dir) / "candidates.json"
    limit = _bounded_int(limit, default=DEFAULT_PAGE_LIMIT, minimum=1, maximum=MAX_PAGE_LIMIT)
    offset = max(0, _bounded_int(offset, default=0, minimum=0, maximum=10_000_000))
    query = query.strip().lower()
    view = view if view in {"domain", "common"} else "domain"
    selected_domains = _clean_domain_list(domains or [])
    selected_domain = domain.strip()
    tested_domains: set[str] = set()
    total = 0
    rows: list[dict[str, Any]] = []
    for candidate in _iter_candidate_file(path):
        candidate_domains = _candidate_domains(candidate)
        all_domains = sorted({*candidate_domains, *_candidate_common_domains(candidate)})
        tested_domains.update(all_domains)
        if view == "domain":
            if not candidate_domains:
                continue
            if selected_domain and selected_domain not in candidate_domains:
                continue
        if view == "common":
            if len(selected_domains) < 2:
                continue
            domain_set = set(all_domains)
            if not all(domain in domain_set for domain in selected_domains):
                continue
        if query and not _candidate_matches_query(candidate, query, all_domains):
            continue
        total += 1
        if total <= offset:
            continue
        if len(rows) < limit:
            rows.append(_compact_candidate(candidate))
    version = _file_version(path)
    return {
        "candidates": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(rows) < total,
        "tested_domains": sorted(tested_domains),
        "version": version,
    }


def read_candidate_domain_index(state_dir: Path, *, query: str = "") -> dict[str, Any]:
    path = _finder_dir(state_dir) / "candidates.json"
    query = query.strip().lower()
    domains: dict[str, dict[str, Any]] = {}
    tested_domains: set[str] = set()
    for candidate in _iter_candidate_file(path):
        candidate_domains = _candidate_domains(candidate)
        all_domains = sorted({*candidate_domains, *_candidate_common_domains(candidate)})
        tested_domains.update(all_domains)
        if query and not _candidate_matches_query(candidate, query, all_domains):
            continue
        candidate_id = str(candidate.get("id") or candidate_id_for(str(candidate.get("protocol") or ""), str(candidate.get("args") or "")))
        protocol = str(candidate.get("protocol") or "unknown")
        for domain in candidate_domains:
            item = domains.setdefault(domain, {"domain": domain, "strategy_ids": set(), "protocols": {}})
            item["strategy_ids"].add(candidate_id)
            protocol_ids = item["protocols"].setdefault(protocol, set())
            protocol_ids.add(candidate_id)
    rows = []
    for item in domains.values():
        rows.append(
            {
                "domain": item["domain"],
                "strategy_count": len(item["strategy_ids"]),
                "protocols": [
                    {"protocol": protocol, "count": len(ids)}
                    for protocol, ids in sorted(item["protocols"].items(), key=lambda pair: pair[0])
                ],
            }
        )
    rows.sort(key=lambda item: str(item["domain"]))
    return {
        "domains": rows,
        "total": len(rows),
        "strategy_total": sum(int(item["strategy_count"]) for item in rows),
        "tested_domains": sorted(tested_domains),
        "version": _file_version(path),
    }


def read_runs(state_dir: Path, limit: int = 50) -> list[dict[str, Any]]:
    path = _finder_dir(state_dir) / "runs.jsonl"
    if not path.exists():
        return []
    lines = _tail_lines(path, limit)
    result: list[dict[str, Any]] = []
    for line in lines:
        if line.strip():
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                result.append(parsed)
    return result


def close_stale_running_runs(state_dir: Path) -> int:
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
            "domains": run.get("domains") or [],
            "returncode": run.get("returncode"),
            "stdout_log": run.get("stdout_log", ""),
            "stderr_log": run.get("stderr_log", ""),
            "progress_log": run.get("progress_log", ""),
            "candidate_count": int(run.get("candidate_count") or 0),
            "common_candidate_count": int(run.get("common_candidate_count") or 0),
            "total_candidates": int(run.get("total_candidates") or 0),
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
        append_jsonl(root / "runs.jsonl", update)
        closed += 1
    return closed


def latest_log_tail(state_dir: Path, max_lines: int = 200) -> dict[str, Any]:
    for run in reversed(read_runs(state_dir, limit=200)):
        stdout_log = Path(str(run.get("stdout_log") or ""))
        if not stdout_log.is_file():
            continue
        lines = _tail_lines(stdout_log, max_lines)
        stderr_log_raw = str(run.get("stderr_log") or "")
        stderr_log = Path(stderr_log_raw) if stderr_log_raw else None
        stderr_lines = _tail_lines(stderr_log, max_lines) if stderr_log and stderr_log.is_file() else []
        stdout_tail = "\n".join(lines)
        progress = run.get("progress")
        if not isinstance(progress, dict):
            progress = _read_progress_log(run)
        if not isinstance(progress, dict):
            progress = progress_from_stdout(stdout_tail, run)
            progress["partial"] = True
        return {
            "run_id": run.get("id"),
            "kind": run.get("kind"),
            "status": run.get("status"),
            "stdout_tail": stdout_tail,
            "stderr_tail": "\n".join(stderr_lines),
            "stdout_log": str(stdout_log),
            "stderr_log": str(stderr_log) if stderr_log else "",
            "progress": progress,
        }
    return {
        "run_id": None,
        "kind": None,
        "status": None,
        "stdout_tail": "",
        "stderr_tail": "",
        "progress": progress_from_stdout("", {}),
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


class _LiveStdoutRecorder:
    def __init__(self, state_dir: Path, run: dict[str, Any]):
        self._lock = threading.Lock()
        self._run = run
        self._available_path = _finder_dir(state_dir) / "available.ndjson"
        progress_log = str(run.get("progress_log") or "")
        self._progress_log = Path(progress_log) if progress_log else None
        self._last_progress_attempted = -1
        self._last_progress_written_at = 0.0
        self._section = ""
        self._current_script = ""
        self._pending_attempt: str | None = None
        self._attempted = 0
        self._attempts_by_script: dict[str, int] = {}
        self._summary: list[str] = []
        self._common: list[str] = []
        self._results: list[dict[str, Any]] = []
        self._common_results: list[dict[str, Any]] = []
        self._candidates: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        self._common_candidates: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}

    def record_line(self, raw_line: str) -> None:
        line = raw_line.strip()
        if not line:
            return
        with self._lock:
            self._record_line_locked(line)
            self._write_progress_locked()

    def parsed(self) -> dict[str, Any]:
        with self._lock:
            candidates = list(self._candidates.values())
            common_candidates = list(self._common_candidates.values())
            results = list(self._results)
            common_results = list(self._common_results)
            return {
                "summary": list(self._summary),
                "common": list(self._common),
                "live_summary": [str(item.get("raw") or "") for item in candidates if item.get("raw")],
                "candidates": candidates,
                "common_candidates": common_candidates,
                "results": results,
                "common_results": common_results,
                "direct_available": [item for item in results if item.get("result") == "working without bypass"],
                "not_working": [item for item in results if "not working" in str(item.get("result") or "")],
            }

    def progress(self, run: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            progress = self._progress_locked(run)
            self._write_progress_locked(force=True, run=run)
            return progress

    def _record_line_locked(self, line: str) -> None:
        if line == "* SUMMARY":
            self._section = "summary"
            return
        if line == "* COMMON":
            self._section = "common"
            return
        script = _script_name_from_line(line)
        if script:
            self._current_script = script
            self._attempts_by_script.setdefault(script, 0)
            return
        if _ATTEMPT_RE.match(line):
            self._attempted += 1
            self._attempts_by_script[self._current_script] = self._attempts_by_script.get(self._current_script, 0) + 1
        attempt = _live_attempt_line(line)
        if attempt:
            self._pending_attempt = attempt
            return
        if line == "!!!!! AVAILABLE !!!!!" and self._pending_attempt:
            candidate = _candidate_from_result_line(self._pending_attempt, scope="domain")
            if candidate:
                self._record_candidate_locked(candidate, common=False)
            self._pending_attempt = None
            return
        if line.startswith("UNAVAILABLE") or line.startswith("FAILED"):
            self._pending_attempt = None
            return
        live_success = _candidate_from_live_success_line(line)
        if live_success:
            self._record_candidate_locked(live_success, common=False)
            return
        if self._section in {"summary", "common"}:
            target = self._common if self._section == "common" else self._summary
            target.append(line)
            parsed = _parse_result_line(line)
            if parsed:
                result_target = self._common_results if self._section == "common" else self._results
                result_target.append(parsed)
            candidate = _candidate_from_result_line(line, scope=self._section)
            if candidate:
                self._record_candidate_locked(candidate, common=self._section == "common")

    def _record_candidate_locked(self, candidate: dict[str, Any], common: bool) -> None:
        key = (
            str(candidate.get("scope") or ""),
            str(candidate.get("test") or ""),
            str(candidate.get("ip_version") or ""),
            str(candidate.get("domain") or ""),
            str(candidate.get("args") or ""),
        )
        target = self._common_candidates if common else self._candidates
        if key in target:
            return
        target[key] = candidate
        append_jsonl(
            self._available_path,
            {
                **candidate,
                "run_id": self._run["id"],
                "seen_at": now_iso(),
            },
        )

    def _progress_locked(self, run: dict[str, Any]) -> dict[str, Any]:
        successful = len(
            {
                (str(item.get("protocol") or ""), str(item.get("args") or ""))
                for item in [*self._candidates.values(), *self._common_candidates.values()]
            }
        )
        return _progress_from_counts(
            run=run,
            attempted=self._attempted,
            attempts_by_script=dict(self._attempts_by_script),
            successful=successful,
            current_script=self._current_script,
        )

    def _write_progress_locked(self, force: bool = False, run: dict[str, Any] | None = None) -> None:
        if not self._progress_log:
            return
        now = time.monotonic()
        attempt_delta = self._attempted - self._last_progress_attempted
        if not force and attempt_delta < 20 and now - self._last_progress_written_at < 2.0:
            return
        progress = self._progress_locked(run or self._run)
        tmp = self._progress_log.with_suffix(".json.tmp")
        try:
            self._progress_log.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(progress, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
            tmp.replace(self._progress_log)
            self._last_progress_attempted = self._attempted
            self._last_progress_written_at = now
        except OSError:
            return


def _run_process_with_live_stdout(
    command: list[str],
    env: dict[str, str],
    stdout_log: Path,
    stderr_log: Path,
    timeout_seconds: int,
    stop_event: threading.Event | None,
    recorder: _LiveStdoutRecorder,
) -> dict[str, Any]:
    status = "success"
    returncode: int | None = None
    timed_out = False
    stopped = False
    reader_errors: list[BaseException] = []

    with stdout_log.open("w", encoding="utf-8") as stdout_handle, stderr_log.open("w", encoding="utf-8") as stderr_handle:
        process = subprocess.Popen(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=stderr_handle,
            env=env,
            start_new_session=hasattr(os, "setsid"),
        )

        def read_stdout() -> None:
            try:
                if process.stdout is None:
                    return
                for line in process.stdout:
                    stdout_handle.write(line)
                    stdout_handle.flush()
                    recorder.record_line(line)
            except BaseException as exc:  # noqa: BLE001
                reader_errors.append(exc)

        reader = threading.Thread(target=read_stdout, daemon=True)
        reader.start()
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
        reader.join(timeout=5)

    if reader_errors:
        raise RuntimeError(f"failed to read blockcheck stdout: {reader_errors[0]}")
    return {
        "status": status,
        "returncode": returncode,
        "timed_out": timed_out,
        "stopped": stopped,
    }


def _run_blockcheck_live(
    state_dir: Path,
    kind: str,
    domains: list[str],
    timeout_seconds: int,
    test: str,
    options: DiscoveryOptions,
    candidate_id: str = "",
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    blockcheck = shutil.which("blockcheck2.sh") or shutil.which("blockcheck.sh")
    if not blockcheck:
        raise RuntimeError("blockcheck2.sh/blockcheck.sh not found in PATH")
    options = options.normalized()
    clean_domains = _clean_domains(domains)
    full_env = os.environ.copy()
    full_env.update(
        {
            "BATCH": "1",
            "DOMAINS": " ".join(clean_domains),
            "IPVS": "4",
            "TEST": test,
            **options.to_blockcheck_env(),
        }
    )

    root = _finder_dir(state_dir)
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    run_id = f"{now_iso().replace(':', '').replace('-', '')}-{uuid.uuid4().hex[:8]}"
    stdout_log = logs / f"{run_id}.{kind}.stdout.log"
    stderr_log = logs / f"{run_id}.{kind}.stderr.log"
    progress_log = logs / f"{run_id}.{kind}.progress.json"
    attempt_plan = _standard_attempt_plan(
        domains=clean_domains,
        test=test,
        enable_http=options.enable_http,
        enable_tls=options.enable_tls12,
        enable_tls13=options.enable_tls13,
        enable_quic=options.enable_quic,
    )
    option_fields = options.to_run_fields()
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
        "progress_log": str(progress_log),
        "candidate_count": 0,
        "test": test,
        **option_fields,
        "attempt_plan": attempt_plan,
    }
    append_jsonl(root / "runs.jsonl", started)

    recorder = _LiveStdoutRecorder(state_dir, started)
    process_result = _run_process_with_live_stdout(
        command=[blockcheck],
        env=full_env,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        timeout_seconds=timeout_seconds,
        stop_event=stop_event,
        recorder=recorder,
    )
    parsed = recorder.parsed()
    run = {
        "id": run_id,
        "kind": kind,
        "candidate_id": candidate_id,
        "status": process_result["status"],
        "timestamp": now_iso(),
        "domains": clean_domains,
        "returncode": process_result["returncode"],
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "progress_log": str(progress_log),
        "summary": parsed["summary"],
        "results": parsed["results"],
        "candidate_count": len(parsed["candidates"]),
        "common_candidate_count": len(parsed["common_candidates"]),
        "timed_out": process_result["timed_out"],
        "stopped": process_result["stopped"],
        "timeout_seconds": timeout_seconds,
        "test": test,
        **option_fields,
        "attempt_plan": attempt_plan,
    }
    run["progress"] = recorder.progress(run)
    if kind in {"standard-discovery", "multi-domain-discovery"}:
        candidates = upsert_candidates(state_dir, parsed, run)
        run["total_candidates"] = len(candidates)
    append_jsonl(root / "runs.jsonl", run)
    return run


def _run_multidomain_blockcheck_live(
    state_dir: Path,
    domains: list[str],
    timeout_seconds: int,
    options: DiscoveryOptions,
    curl_parallelism: int,
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    blockcheck = shutil.which("blockcheck2.sh") or shutil.which("blockcheck.sh")
    if not blockcheck:
        raise RuntimeError("blockcheck2.sh/blockcheck.sh not found in PATH")
    options = options.normalized()
    clean_domains = _clean_domains(domains)
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
                **options.to_blockcheck_env(),
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
            options=options,
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
    options: DiscoveryOptions,
    curl_parallelism: int | None = None,
    candidate_id: str = "",
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    options = options.normalized()
    root = _finder_dir(state_dir)
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    run_id = f"{now_iso().replace(':', '').replace('-', '')}-{uuid.uuid4().hex[:8]}"
    stdout_log = logs / f"{run_id}.{kind}.stdout.log"
    stderr_log = logs / f"{run_id}.{kind}.stderr.log"
    progress_log = logs / f"{run_id}.{kind}.progress.json"
    attempt_plan = _standard_attempt_plan(
        domains=domains,
        test=test,
        enable_http=options.enable_http,
        enable_tls=options.enable_tls12,
        enable_tls13=options.enable_tls13,
        enable_quic=options.enable_quic,
    )
    option_fields = options.to_run_fields()
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
        "progress_log": str(progress_log),
        "candidate_count": 0,
        "test": test,
        **option_fields,
        "curl_parallelism": curl_parallelism,
        "attempt_plan": attempt_plan,
    }
    append_jsonl(root / "runs.jsonl", started)

    recorder = _LiveStdoutRecorder(state_dir, started)
    process_result = _run_process_with_live_stdout(
        command=command,
        env=env,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        timeout_seconds=timeout_seconds,
        stop_event=stop_event,
        recorder=recorder,
    )
    parsed = recorder.parsed()
    run = {
        "id": run_id,
        "kind": kind,
        "candidate_id": candidate_id,
        "status": process_result["status"],
        "timestamp": now_iso(),
        "domains": domains,
        "returncode": process_result["returncode"],
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "progress_log": str(progress_log),
        "summary": parsed["summary"],
        "results": parsed["results"],
        "candidate_count": len(parsed["candidates"]),
        "common_candidate_count": len(parsed["common_candidates"]),
        "timed_out": process_result["timed_out"],
        "stopped": process_result["stopped"],
        "timeout_seconds": timeout_seconds,
        "test": test,
        **option_fields,
        "curl_parallelism": curl_parallelism,
        "attempt_plan": attempt_plan,
    }
    run["progress"] = recorder.progress(run)
    if kind in {"standard-discovery", "multi-domain-discovery"}:
        candidates = upsert_candidates(state_dir, parsed, run)
        run["total_candidates"] = len(candidates)
    append_jsonl(root / "runs.jsonl", run)
    return run


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
    return _progress_from_counts(
        run=run,
        attempted=attempted,
        attempts_by_script=attempts_by_script,
        successful=successful,
        current_script=current_script,
    )


def _progress_from_counts(
    *,
    run: dict[str, Any],
    attempted: int,
    attempts_by_script: dict[str, int],
    successful: int,
    current_script: str,
) -> dict[str, Any]:
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
    eta_ms_per_attempt = _eta_ms_per_attempt_for_run(run)
    eta = _eta_from_remaining_attempts(attempted, attempt_total, completed, eta_parallelism, eta_ms_per_attempt)
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
        "eta_estimate_ms_per_attempt": eta_ms_per_attempt,
        "eta_parallelism": eta_parallelism,
        "repeats": _bounded_int(run.get("repeats"), default=1, minimum=1, maximum=10),
        "repeat_parallel": _truthy(run.get("repeat_parallel"), default=False),
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


def _eta_ms_per_attempt_for_run(run: dict[str, Any]) -> int:
    repeats = _bounded_int(run.get("repeats"), default=1, minimum=1, maximum=10)
    if _truthy(run.get("repeat_parallel"), default=False):
        repeats = 1
    return ATTEMPT_TIMEOUT_ESTIMATE_MS * repeats


def _eta_from_remaining_attempts(
    attempted: int,
    attempt_total: int,
    completed: bool,
    parallelism: int = 1,
    ms_per_attempt: int = ATTEMPT_TIMEOUT_ESTIMATE_MS,
) -> int | None:
    if completed:
        return 0
    if not attempt_total:
        return None
    remaining = max(0, attempt_total - attempted)
    if remaining <= 0:
        return 0
    effective_remaining = (remaining + max(1, parallelism) - 1) // max(1, parallelism)
    return max(0, int((effective_remaining * max(1, ms_per_attempt)) / 1000))


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
        parsed = _candidate_from_result_line(line, scope)
        if parsed:
            candidates.append(parsed)
    return candidates


def _candidate_from_result_line(line: str, scope: str) -> dict[str, Any] | None:
    parsed = _parse_result_line(line)
    if not parsed:
        return None
    raw_result = str(parsed.get("result") or "")
    if not raw_result.startswith("nfqws2 ") or raw_result == "nfqws2 not working":
        return None
    args = raw_result.removeprefix("nfqws2 ").strip()
    return {
        "domain": parsed["domain"],
        "test": parsed["test"],
        "ip_version": parsed["ip_version"],
        "protocol": _protocol_from_test(str(parsed["test"])),
        "args": args,
        "raw": line,
        "scope": scope,
    }


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
    for line in stdout.splitlines():
        candidate = _candidate_from_live_success_line(line.strip())
        if candidate:
            result.append(
                f"{candidate['test']} ipv{candidate['ip_version']} {candidate['domain']} : "
                f"nfqws2 {candidate['args']}"
            )
    return result


def _candidate_from_live_success_line(line: str) -> dict[str, Any] | None:
    pattern = re.compile(
        r"^!!!!!\s+(?P<test>\S+): working strategy found for ipv(?P<ip_version>\d+)\s+"
        r"(?P<domain>\S+)\s+:\s+nfqws2\s+(?P<args>.*?)\s+!!!!!$"
    )
    match = pattern.match(line.strip())
    if not match:
        return None
    result_line = (
        f"{match.group('test')} ipv{match.group('ip_version')} {match.group('domain')} : "
        f"nfqws2 {match.group('args').strip()}"
    )
    return _candidate_from_result_line(result_line, scope="domain")


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
	[ "$ENABLE_HTTP" = 1 ] && gp_md_run_protocol pktws_check_http curl_test_http tcp "$HTTP_PORT"
	[ "$ENABLE_HTTPS_TLS12" = 1 ] && gp_md_run_protocol pktws_check_https_tls12 curl_test_https_tls12 tcp "$HTTPS_PORT"
	[ "$ENABLE_HTTPS_TLS13" = 1 ] && gp_md_run_protocol pktws_check_https_tls13 curl_test_https_tls13 tcp "$HTTPS_PORT"
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
    return _clean_domain_list(domains) or list(CRITICAL_DOMAINS)


def _clean_domain_list(domains: list[str]) -> list[str]:
    result: list[str] = []
    for domain in domains:
        value = str(domain).strip()
        if value and value not in result:
            result.append(value)
    return result


def _finder_dir(state_dir: Path) -> Path:
    path = state_dir / "strategy-finder"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_candidates(state_dir: Path, candidates: list[dict[str, Any]]) -> None:
    path = _finder_dir(state_dir) / "candidates.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(candidates, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _iter_candidate_file(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    decoder = json.JSONDecoder()
    buffer = ""
    eof = False

    def fill(handle: Any) -> None:
        nonlocal buffer, eof
        chunk = handle.read(65536)
        if chunk:
            buffer += chunk
        else:
            eof = True

    with path.open("r", encoding="utf-8") as handle:
        fill(handle)
        while True:
            buffer = buffer.lstrip()
            if buffer:
                break
            if eof:
                return
            fill(handle)
        if not buffer.startswith("["):
            return
        buffer = buffer[1:]
        while True:
            while True:
                buffer = buffer.lstrip()
                if buffer:
                    break
                if eof:
                    return
                fill(handle)
            if buffer.startswith("]"):
                return
            if buffer.startswith(","):
                buffer = buffer[1:]
                continue
            while True:
                try:
                    value, end = decoder.raw_decode(buffer)
                    break
                except json.JSONDecodeError:
                    if eof:
                        return
                    fill(handle)
            if isinstance(value, dict):
                yield value
            buffer = buffer[end:]


def _tail_lines(path: Path, max_lines: int) -> list[str]:
    max_lines = max(0, max_lines)
    if max_lines <= 0 or not path.exists():
        return []
    block_size = 8192
    blocks: list[bytes] = []
    line_count = 0
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        while position > 0 and line_count <= max_lines:
            read_size = min(block_size, position)
            position -= read_size
            handle.seek(position)
            block = handle.read(read_size)
            blocks.append(block)
            line_count += block.count(b"\n")
    data = b"".join(reversed(blocks))
    return data.decode("utf-8", errors="replace").splitlines()[-max_lines:]


def _file_version(path: Path) -> dict[str, int]:
    if not path.exists():
        return {"size": 0, "mtime_ns": 0}
    stat = path.stat()
    return {"size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}


def _candidate_domains(candidate: dict[str, Any]) -> list[str]:
    seen = candidate.get("seen")
    if not isinstance(seen, list):
        return []
    return sorted({str(item.get("domain") or "").strip() for item in seen if isinstance(item, dict) and str(item.get("domain") or "").strip()})


def _candidate_common_domains(candidate: dict[str, Any]) -> list[str]:
    common_seen = candidate.get("common_seen")
    if not isinstance(common_seen, list):
        return []
    domains: set[str] = set()
    for item in common_seen:
        if not isinstance(item, dict) or not isinstance(item.get("domains"), list):
            continue
        domains.update(str(domain or "").strip() for domain in item["domains"] if str(domain or "").strip())
    return sorted(domains)


def _candidate_all_domains(candidate: dict[str, Any]) -> list[str]:
    return sorted({*_candidate_domains(candidate), *_candidate_common_domains(candidate)})


def _candidate_matches_query(candidate: dict[str, Any], query: str, domains: list[str]) -> bool:
    haystack = " ".join(
        [
            str(candidate.get("id") or ""),
            str(candidate.get("protocol") or ""),
            str(candidate.get("args") or ""),
            *domains,
        ]
    ).lower()
    return query in haystack


def _compact_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    domains = _candidate_domains(candidate)
    common_domains = _candidate_common_domains(candidate)
    result = {
        "id": candidate.get("id"),
        "protocol": candidate.get("protocol"),
        "args": candidate.get("args"),
        "status": candidate.get("status"),
        "first_seen_at": candidate.get("first_seen_at"),
        "last_seen_at": candidate.get("last_seen_at"),
        "seen": [{"domain": domain} for domain in domains],
    }
    if common_domains:
        result["common_seen"] = [{"domains": common_domains}]
    return result
