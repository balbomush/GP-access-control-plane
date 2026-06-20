from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .strategies import list_local_strategies


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
    blockcheck = shutil.which("blockcheck2.sh") or shutil.which("blockcheck.sh")
    if not blockcheck:
        raise RuntimeError("blockcheck2.sh/blockcheck.sh not found in PATH")
    return subprocess.run(
        [blockcheck, domain],
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
