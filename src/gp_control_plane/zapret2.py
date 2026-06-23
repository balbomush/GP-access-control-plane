from __future__ import annotations

import os
import re
import signal
import shutil
import subprocess
import time


def check_install() -> dict[str, str | bool]:
    nfqws2 = shutil.which("nfqws2")
    blockcheck = shutil.which("blockcheck2.sh") or shutil.which("blockcheck.sh")
    return {
        "nfqws2_found": bool(nfqws2),
        "nfqws2_path": nfqws2 or "",
        "blockcheck_found": bool(blockcheck),
        "blockcheck_path": blockcheck or "",
    }


def _stop_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if hasattr(os, "killpg"):
        os.killpg(process.pid, signal.SIGTERM)
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if hasattr(os, "killpg"):
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=5)


def _cleanup_blockcheck_processes() -> None:
    patterns = (
        "/opt/zapret2/nfq2/nfqws2",
        "curl --connect-to",
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
    sudo = shutil.which("sudo")
    if sudo:
        subprocess.run([sudo, "-n", "kill", f"-{signal_name}", *pids], text=True, capture_output=True, check=False)


def _cleanup_nft_blockcheck_tables() -> None:
    nft = shutil.which("nft")
    if not nft:
        return
    command = [nft]
    listed = subprocess.run(command + ["list", "tables"], text=True, capture_output=True, check=False)
    if listed.returncode != 0 and shutil.which("sudo"):
        command = ["sudo", "-n", nft]
        listed = subprocess.run(command + ["list", "tables"], text=True, capture_output=True, check=False)
    if listed.returncode != 0:
        return
    for family, table in _blockcheck_nft_tables(listed.stdout):
        subprocess.run(command + ["delete", "table", family, table], text=True, capture_output=True, check=False)


def _blockcheck_nft_tables(output: str) -> list[tuple[str, str]]:
    tables: list[tuple[str, str]] = []
    for line in output.splitlines():
        match = re.match(r"\s*table\s+(\S+)\s+(blockcheck\d+)\s*$", line)
        if match:
            tables.append((match.group(1), match.group(2)))
    return tables
