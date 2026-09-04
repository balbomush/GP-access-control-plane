"""bc2_engine._plan — moved from strategy_finder.py / blockchecks_backend.py."""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from gp_control_plane.engine_common._constants import ATTEMPT_TIMEOUT_ESTIMATE_MS, ETA_RECALC_LARGE_AFTER, ETA_RECALC_LARGE_STEP, ETA_RECALC_SMALL_STEP, _ATTEMPT_PLAN_CACHE
from gp_control_plane.engine_common._options import _bounded_int, _minimum_int, _truthy
from gp_control_plane.engine_common._retention import _clean_domains

def _standard_attempt_plan(
    domains: list[str],
    test: str = "standard",
    enable_http: bool = False,
    enable_tls: bool = True,
    enable_tls13: bool = False,
    enable_quic: bool = True,
    enable_ipv6: bool = False,
    root: Path | None = None,
) -> dict[str, Any]:
    if test != "standard":
        return _empty_attempt_plan(test)
    root = root or _blockcheck_test_dir(test)
    if not root.exists():
        return _empty_attempt_plan(test)
    scripts = _standard_scripts(root)
    domain_count = len(_clean_domains(domains))
    ip_version_count = 2 if enable_ipv6 else 1
    fingerprint = tuple((path.name, path.stat().st_mtime_ns, path.stat().st_size) for path in scripts)
    key = (
        str(root),
        fingerprint,
        domain_count,
        bool(enable_http),
        bool(enable_tls),
        bool(enable_tls13),
        bool(enable_quic),
        bool(enable_ipv6),
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
    strategy_script_totals: dict[str, int] = {}
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
        script_totals[name] = per_domain * domain_count * ip_version_count
        strategy_script_totals[name] = per_domain

    total = sum(script_totals.values())
    strategy_total = sum(strategy_script_totals.values())
    plan = {
        "test": test,
        "total": total,
        "scripts": script_totals,
        "strategy_total": strategy_total,
        "strategy_scripts": strategy_script_totals,
        "script_order": script_order,
        "domain_count": domain_count,
        "ip_version_count": ip_version_count,
        "source": source if total else "",
    }
    _ATTEMPT_PLAN_CACHE[key] = plan
    return plan

def _empty_attempt_plan(test: str) -> dict[str, Any]:
    return {
        "test": test,
        "total": 0,
        "scripts": {},
        "strategy_total": 0,
        "strategy_scripts": {},
        "script_order": [],
        "domain_count": 0,
        "ip_version_count": 1,
        "source": "",
    }

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
    return _minimum_int(run.get("curl_parallelism"), default=4, minimum=1)

def _eta_ms_per_attempt_for_run(run: dict[str, Any]) -> int:
    repeats = _bounded_int(run.get("repeats"), default=1, minimum=1, maximum=10)
    if _truthy(run.get("repeat_parallel"), default=False):
        repeats = 1
    return ATTEMPT_TIMEOUT_ESTIMATE_MS * repeats

def _eta_recalculation_step(attempted: int) -> int:
    return ETA_RECALC_LARGE_STEP if attempted >= ETA_RECALC_LARGE_AFTER else ETA_RECALC_SMALL_STEP

def _eta_recalculation_attempts(attempted: int) -> int:
    if attempted <= 0:
        return 0
    if attempted < ETA_RECALC_SMALL_STEP:
        return attempted
    step = _eta_recalculation_step(attempted)
    return max(step, (attempted // step) * step)

def _elapsed_average_ms_per_attempt(elapsed_seconds: int | None, attempted: int) -> int | None:
    if elapsed_seconds is None or attempted <= 0:
        return None
    return max(1, int((max(0, elapsed_seconds) * 1000) / attempted))

def _eta_from_remaining_attempts(
    remaining: int | None,
    completed: bool,
    parallelism: int = 1,
    ms_per_attempt: int = ATTEMPT_TIMEOUT_ESTIMATE_MS,
) -> int | None:
    if completed:
        return 0
    if remaining is None:
        return None
    if remaining <= 0:
        return 0
    effective_remaining = (remaining + max(1, parallelism) - 1) // max(1, parallelism)
    return max(0, int((effective_remaining * max(1, ms_per_attempt)) / 1000))

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
