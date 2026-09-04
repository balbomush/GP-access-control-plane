"""engine_common._stdout_parse — moved from strategy_finder.py / blockchecks_backend.py."""
from __future__ import annotations

import re
from typing import Any
from gp_control_plane.engine_common._constants import LIVE_CANDIDATE_SAMPLE_LIMIT, _CURL_FAILURE_INFO
from gp_control_plane.engine_common._options import curl_failure_info

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

def _diagnostic_counts_from_stdout(
    stdout: str,
    summary_results: list[dict[str, Any] | None],
) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]], list[dict[str, Any]]]:
    status_counts: dict[str, dict[str, int]] = {}
    code_counts: dict[str, dict[str, int]] = {}
    diagnostics: list[dict[str, Any]] = []
    pending: str | None = None
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        attempt = _live_attempt_line(line)
        if attempt:
            pending = attempt
            continue
        if (line.startswith("UNAVAILABLE") or line.startswith("FAILED")) and pending:
            parsed = _parse_result_line(pending)
            if parsed:
                domain = str(parsed.get("domain") or "")
                test = str(parsed.get("test") or "")
                code = _curl_code_from_line(line)
                info = curl_failure_info(code, test=test, domain=domain)
                _increment_nested(status_counts, domain, str(info.get("status") or "curl_error"))
                if code:
                    _increment_nested(code_counts, domain, code)
                if len(diagnostics) < LIVE_CANDIDATE_SAMPLE_LIMIT:
                    diagnostics.append(
                        {
                            "domain": domain,
                            "test": test,
                            "protocol": _protocol_from_test(test),
                            "code": code,
                            "status": info.get("status") or "curl_error",
                            "label": info.get("label") or "curl ошибка",
                            "message": info.get("message") or "",
                            "strategy_failure": _is_strategy_failure(info),
                        }
                    )
            pending = None
            continue
        if line == "!!!!! AVAILABLE !!!!!":
            pending = None
    for item in summary_results:
        if not item:
            continue
        domain = str(item.get("domain") or "")
        result = str(item.get("result") or "")
        if result == "working without bypass":
            _increment_nested(status_counts, domain, "direct_available")
        elif "not working" in result:
            _increment_nested(status_counts, domain, "needs_discovery")
    return status_counts, code_counts, diagnostics

def _increment_nested(target: dict[str, dict[str, int]], first: str, second: str) -> None:
    if not first or not second:
        return
    counts = target.setdefault(first, {})
    counts[second] = counts.get(second, 0) + 1

def _curl_summary(diagnostics: list[dict[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in diagnostics:
        code = str(item.get("code") or "")
        if not code:
            continue
        result[code] = result.get(code, 0) + 1
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

def _curl_code_from_line(line: str) -> str:
    match = re.search(r"(?:code|код)\s*=\s*(\d+)", line, re.IGNORECASE)
    return match.group(1) if match else ""

def _is_strategy_failure(info: dict[str, Any]) -> bool:
    status = str(info.get("status") or "")
    return status not in {"invalid_domain", "dns_error", "tls_sni_problem"}

def _domain_status_info(status: str) -> dict[str, str]:
    mapping = {
        "direct_available": {
            "label": "прямой доступ",
            "message": "домен открывается без zapret; подбор стратегии для него не нужен.",
        },
        "needs_discovery": {
            "label": "нужен подбор",
            "message": "домен не открылся напрямую и может требовать подбора стратегии.",
        },
    }
    if status in mapping:
        return mapping[status]
    for item in _CURL_FAILURE_INFO.values():
        if item["status"] == status:
            return {"label": str(item["label"]), "message": str(item["message"])}
    return {"label": status or "неизвестно", "message": ""}

def _domain_diagnostics_from_counts(
    domain_status_counts: dict[str, dict[str, int]],
    domain_code_counts: dict[str, dict[str, int]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for domain, counts in sorted(domain_status_counts.items()):
        status = _dominant_status(counts)
        info = _domain_status_info(status)
        codes = domain_code_counts.get(domain, {})
        result.append(
            {
                "domain": domain,
                "status": status,
                "label": info["label"],
                "message": info["message"],
                "count": int(counts.get(status, 0)),
                "total": int(sum(counts.values())),
                "codes": dict(sorted(codes.items(), key=lambda item: (-item[1], item[0]))),
            }
        )
    return result

def _dominant_failure_from_counts(domain_status_counts: dict[str, dict[str, int]]) -> dict[str, Any]:
    totals: dict[str, int] = {}
    for counts in domain_status_counts.values():
        for status, count in counts.items():
            if status == "direct_available":
                continue
            totals[status] = totals.get(status, 0) + int(count)
    if not totals:
        return {}
    status = _dominant_status(totals)
    info = _domain_status_info(status)
    return {"status": status, "label": info["label"], "message": info["message"], "count": totals[status]}

def _dominant_status(counts: dict[str, int]) -> str:
    priority = {
        "invalid_domain": 90,
        "dns_error": 80,
        "tls_sni_problem": 70,
        "ssl_connect_error": 60,
        "quic_connect_error": 55,
        "timeout": 50,
        "needs_discovery": 40,
        "curl_error": 30,
        "direct_available": 10,
    }
    if not counts:
        return ""
    return sorted(counts.items(), key=lambda item: (-int(item[1]), -priority.get(item[0], 0), item[0]))[0][0]

def _protocol_from_test(test: str) -> str:
    if "http3" in test:
        return "quic"
    if "http_" in test and "https" not in test:
        return "http"
    return "tls"
