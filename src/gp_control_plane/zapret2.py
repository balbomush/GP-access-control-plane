from __future__ import annotations

import os
import re
import signal
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .models import StrategySelection
from .strategies import list_local_strategies, load_strategy_dir


def check_install() -> dict[str, str | bool]:
    nfqws2 = shutil.which("nfqws2")
    blockcheck = shutil.which("blockcheck2.sh") or shutil.which("blockcheck.sh")
    return {
        "nfqws2_found": bool(nfqws2),
        "nfqws2_path": nfqws2 or "",
        "blockcheck_found": bool(blockcheck),
        "blockcheck_path": blockcheck or "",
    }


def list_strategies(strategies_repo: Path) -> list[Path]:
    return list_local_strategies(strategies_repo)


def run_check(domain: str, strategy_path: Path, timeout_seconds: int = 60) -> subprocess.CompletedProcess[str]:
    if not strategy_path.exists():
        raise FileNotFoundError(strategy_path)
    strategy = load_strategy_dir(strategy_path)
    blockcheck = shutil.which("blockcheck2.sh") or shutil.which("blockcheck.sh")
    if not blockcheck:
        raise RuntimeError("blockcheck2.sh/blockcheck.sh not found in PATH")
    env = blockcheck_env(domain, strategy)
    return _run_blockcheck(
        [blockcheck],
        env=env,
        timeout=timeout_seconds,
    )


def blockcheck_env(domain: str, strategy: StrategySelection) -> dict[str, str]:
    env = os.environ.copy()
    metadata = strategy.metadata
    blockcheck = metadata.get("blockcheck")
    if not isinstance(blockcheck, dict):
        blockcheck = {}

    env["BATCH"] = "1"
    env["DOMAINS"] = domain
    env["IPVS"] = str(blockcheck.get("ip_versions") or "4")
    env["TEST"] = str(blockcheck.get("test") or "custom")
    env["SKIP_DNSCHECK"] = "1" if bool(blockcheck.get("skip_dnscheck", True)) else "0"

    checks = blockcheck.get("checks")
    if not isinstance(checks, dict):
        checks = {}
    protocols = {str(protocol) for protocol in metadata.get("protocols") or []}
    env["ENABLE_HTTP"] = _flag(checks.get("http", "http" in protocols))
    env["ENABLE_HTTPS_TLS12"] = _flag(checks.get("https_tls12", "tls" in protocols))
    env["ENABLE_HTTPS_TLS13"] = _flag(checks.get("https_tls13", False))
    env["ENABLE_HTTP3"] = _flag(checks.get("http3", "quic" in protocols))

    _set_strategy_lists(env, strategy, blockcheck)
    return env


def _set_strategy_lists(env: dict[str, str], strategy: StrategySelection, blockcheck: dict[str, Any]) -> None:
    lists = blockcheck.get("lists")
    if not isinstance(lists, dict):
        lists = {"https_tls12": strategy.nfqws2_config.name}

    mapping = {
        "http": "LIST_HTTP",
        "https_tls12": "LIST_HTTPS_TLS12",
        "https_tls13": "LIST_HTTPS_TLS13",
        "quic": "LIST_QUIC",
    }
    for key, env_name in mapping.items():
        value = lists.get(key)
        if not value:
            continue
        path = Path(str(value))
        if not path.is_absolute():
            path = (strategy.path / path).resolve()
        env[env_name] = str(path)


def _flag(value: Any) -> str:
    return "1" if bool(value) else "0"


def _run_blockcheck(command: list[str], env: dict[str, str], timeout: int) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=hasattr(os, "setsid"),
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _stop_process_group(process)
        _cleanup_nft_blockcheck_tables()
        stdout, stderr = process.communicate()
        exc.output = stdout
        exc.stderr = stderr
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


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
