from __future__ import annotations

import os
import re
import signal
import shutil
import subprocess
import threading
import time
from pathlib import Path


DEFAULT_ROOT_HELPER = "/usr/local/libexec/gp-control-plane/gp-root-helper"
BLOCKCHECK_ENV_KEYS = (
    "BATCH",
    "DOMAINS",
    "IPVS",
    "TEST",
    "SKIP_DNSCHECK",
    "SKIP_IPBLOCK",
    "ENABLE_HTTP",
    "ENABLE_HTTPS_TLS12",
    "ENABLE_HTTPS_TLS13",
    "ENABLE_HTTP3",
    "SCANLEVEL",
    "REPEATS",
    "PARALLEL",
    "GP_MD_CURL_PARALLELISM",
    "ZAPRET_BASE",
    "ZAPRET_RW",
)
INSTALL_CHECK_CACHE_SECONDS = 30.0
_INSTALL_CHECK_CACHE: dict[str, object] = {"expires_at": 0.0, "payload": None}
_INSTALL_CHECK_LOCK = threading.Lock()


def check_install() -> dict[str, object]:
    nfqws2 = shutil.which("nfqws2")
    blockcheck = shutil.which("blockcheck2.sh") or shutil.which("blockcheck.sh")
    nft = shutil.which("nft")
    curl = shutil.which("curl")
    helper = root_helper_status()
    payload: dict[str, object] = {
        "nfqws2_found": bool(nfqws2),
        "nfqws2_path": nfqws2 or "",
        "blockcheck_found": bool(blockcheck),
        "blockcheck_path": blockcheck or "",
        "nft_found": bool(nft),
        "nft_path": nft or "",
        "curl_found": bool(curl),
        "curl_path": curl or "",
        "root_helper_found": bool(helper["found"]),
        "root_helper_ready": bool(helper["ready"]),
        "root_helper_path": str(helper["path"]),
        "root_helper_error": str(helper["error"]),
    }
    payload["ready"] = bool(payload["nfqws2_found"] and payload["blockcheck_found"] and payload["root_helper_ready"])
    payload["diagnostics"] = _install_diagnostics(payload)
    return payload


def check_install_cached(ttl_seconds: float = INSTALL_CHECK_CACHE_SECONDS) -> dict[str, object]:
    now = time.monotonic()
    with _INSTALL_CHECK_LOCK:
        payload = _INSTALL_CHECK_CACHE.get("payload")
        expires_at = float(_INSTALL_CHECK_CACHE.get("expires_at") or 0.0)
        if isinstance(payload, dict) and now < expires_at:
            return dict(payload)
    fresh = check_install()
    with _INSTALL_CHECK_LOCK:
        _INSTALL_CHECK_CACHE["payload"] = dict(fresh)
        _INSTALL_CHECK_CACHE["expires_at"] = now + max(1.0, float(ttl_seconds))
    return fresh


def _install_diagnostics(payload: dict[str, object]) -> list[dict[str, object]]:
    diagnostics = [
        {
            "id": "nfqws2",
            "label": "nfqws2",
            "ok": bool(payload.get("nfqws2_found")),
            "message": (
                f"найден: {payload.get('nfqws2_path')}"
                if payload.get("nfqws2_found")
                else "не найден в PATH; установите zapret2 или проверьте ссылку на nfqws2"
            ),
        },
        {
            "id": "blockcheck",
            "label": "blockcheck2",
            "ok": bool(payload.get("blockcheck_found")),
            "message": (
                f"найден: {payload.get('blockcheck_path')}"
                if payload.get("blockcheck_found")
                else "не найден blockcheck2.sh/blockcheck.sh; установите zapret2"
            ),
        },
        {
            "id": "root-helper",
            "label": "root-helper",
            "ok": bool(payload.get("root_helper_ready")),
            "message": (
                "готов"
                if payload.get("root_helper_ready")
                else str(payload.get("root_helper_error") or "не готов; запустите установщик Raspberry Pi")
            ),
        },
        {
            "id": "curl",
            "label": "curl",
            "ok": bool(payload.get("curl_found")),
            "message": (
                f"найден: {payload.get('curl_path')}"
                if payload.get("curl_found")
                else "не найден; blockcheck2 не сможет проверять доступность доменов"
            ),
        },
        {
            "id": "nft",
            "label": "nft",
            "ok": bool(payload.get("nft_found")),
            "message": (
                f"найден: {payload.get('nft_path')}"
                if payload.get("nft_found")
                else "не найден в PATH; очистка временных nft-таблиц может быть недоступна"
            ),
        },
    ]
    return diagnostics


def clear_install_check_cache() -> None:
    with _INSTALL_CHECK_LOCK:
        _INSTALL_CHECK_CACHE["payload"] = None
        _INSTALL_CHECK_CACHE["expires_at"] = 0.0


def _stop_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    _signal_process_group("TERM", process)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _signal_process_group("KILL", process)
        process.wait(timeout=5)


def root_command(command: list[str], env: dict[str, str] | None = None, pass_env_keys: tuple[str, ...] = ()) -> list[str]:
    if _is_root():
        return command
    require_root_helper_ready()
    helper = _root_helper_path()
    sudo = shutil.which("sudo")
    if not sudo:
        raise RuntimeError("root-helper is not available: sudo command not found")
    if pass_env_keys:
        source_env = env or {}
        assignments = [f"{key}={source_env[key]}" for key in pass_env_keys if key in source_env]
        return [sudo, "-n", helper, "run-env", *assignments, "--", *command]
    return [sudo, "-n", helper, "run", *command]


def require_root_helper_ready() -> None:
    status = root_helper_status()
    if bool(status["ready"]):
        return
    error = str(status["error"]) or "root-helper is not configured"
    raise RuntimeError(f"{error}. Run scripts/install-raspberry-pi.sh to install the root helper.")


def root_helper_status() -> dict[str, str | bool]:
    helper = _root_helper_path()
    found = Path(helper).is_file()
    executable = os.access(helper, os.X_OK)
    if _is_root():
        return {
            "path": helper,
            "found": found,
            "executable": executable,
            "sudo_found": bool(shutil.which("sudo")),
            "ready": True,
            "error": "",
        }
    if not found:
        return {
            "path": helper,
            "found": False,
            "executable": False,
            "sudo_found": bool(shutil.which("sudo")),
            "ready": False,
            "error": f"root-helper not found at {helper}",
        }
    if not executable:
        return {
            "path": helper,
            "found": True,
            "executable": False,
            "sudo_found": bool(shutil.which("sudo")),
            "ready": False,
            "error": f"root-helper is not executable: {helper}",
        }
    sudo = shutil.which("sudo")
    if not sudo:
        return {
            "path": helper,
            "found": True,
            "executable": True,
            "sudo_found": False,
            "ready": False,
            "error": "sudo command not found",
        }
    try:
        checked = subprocess.run(
            [sudo, "-n", helper, "check"],
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "path": helper,
            "found": True,
            "executable": True,
            "sudo_found": True,
            "ready": False,
            "error": f"root-helper check failed: {exc}",
        }
    stderr = checked.stderr.strip()
    return {
        "path": helper,
        "found": True,
        "executable": True,
        "sudo_found": True,
        "ready": checked.returncode == 0,
        "error": "" if checked.returncode == 0 else (stderr or f"root-helper check returned {checked.returncode}"),
    }


def _cleanup_blockcheck_processes() -> None:
    patterns = (
        "/opt/zapret2/nfq2/nfqws2",
        "curl --connect-to",
        "blockcheck2.sh",
        "blockcheck.sh",
        "gp_multidomain_strategy",
        "pktws_curl_test_update",
    )
    pids = _blockcheck_process_pids(patterns)
    if not pids:
        return
    _signal_pids("TERM", pids)
    time.sleep(1)
    remaining = _blockcheck_process_pids(patterns)
    if remaining:
        _signal_pids("KILL", remaining)


def _blockcheck_process_pids(patterns: tuple[str, ...]) -> list[str]:
    pgrep = shutil.which("pgrep")
    if not pgrep:
        return []
    pids: list[str] = []
    for pattern in patterns:
        found = subprocess.run([pgrep, "-f", pattern], text=True, capture_output=True, check=False)
        if found.returncode == 0:
            pids.extend(pid for pid in found.stdout.split() if pid.isdigit() and int(pid) != os.getpid())
    return sorted(set(pids), key=int)


def _signal_pids(signal_name: str, pids: list[str]) -> None:
    if not pids:
        return
    subprocess.run(["kill", f"-{signal_name}", *pids], text=True, capture_output=True, check=False)
    _run_root_helper(["kill", signal_name, *pids])


def _cleanup_nft_blockcheck_tables() -> None:
    nft = shutil.which("nft")
    if not nft:
        return
    command = [nft]
    listed = subprocess.run(command + ["list", "tables"], text=True, capture_output=True, check=False)
    if listed.returncode != 0:
        listed = _run_root_helper(["nft-list-tables"])
    if listed.returncode != 0:
        return
    for family, table in _blockcheck_nft_tables(listed.stdout):
        deleted = subprocess.run(command + ["delete", "table", family, table], text=True, capture_output=True, check=False)
        if deleted.returncode != 0:
            _run_root_helper(["nft-delete-blockcheck-table", family, table])


def _blockcheck_nft_tables(output: str) -> list[tuple[str, str]]:
    tables: list[tuple[str, str]] = []
    for line in output.splitlines():
        match = re.match(r"\s*table\s+(\S+)\s+(blockcheck\d+)\s*$", line)
        if match:
            tables.append((match.group(1), match.group(2)))
    return tables


def _signal_process_group(signal_name: str, process: subprocess.Popen[str]) -> None:
    if hasattr(os, "killpg"):
        try:
            os.killpg(process.pid, getattr(signal, f"SIG{signal_name}"))
        except ProcessLookupError:
            return
        except PermissionError:
            pass
        _run_root_helper(["killpg", signal_name, str(process.pid)])
        return
    if signal_name == "TERM":
        process.terminate()
    else:
        process.kill()


def _run_root_helper(args: list[str]) -> subprocess.CompletedProcess[str]:
    if _is_root():
        return subprocess.CompletedProcess(args, 1, "", "already running as root")
    helper = _root_helper_path()
    sudo = shutil.which("sudo")
    if not sudo or not Path(helper).is_file():
        return subprocess.CompletedProcess(args, 1, "", "root-helper unavailable")
    return subprocess.run([sudo, "-n", helper, *args], text=True, capture_output=True, check=False)


def run_root_helper_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    return _run_root_helper(args)


def _root_helper_path() -> str:
    return os.environ.get("GP_ROOT_HELPER", DEFAULT_ROOT_HELPER)


def _is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid and geteuid() == 0)
