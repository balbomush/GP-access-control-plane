"""bs_engine._dns_pins — moved from strategy_finder.py / blockchecks_backend.py."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def bs_providers_root() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    override = os.environ.get("BLOCKCHECKS_DATA_BLOCK") or ""
    base = Path(override).expanduser() if override else Path(data_home) / "blockcheckS"
    return base.resolve() / "data_block" / "providers"

def list_bs_dns_pins(*, domain: str = "", max_lines: int = 600) -> dict[str, Any]:
    """Read-only DNS-pin hosts files written by blockcheckS (anti-hijack)."""
    root = bs_providers_root()
    providers: list[dict[str, Any]] = []
    target_domain = str(domain or "").strip().lower()
    if not root.is_dir():
        return {"providers": providers, "root": str(root), "filter_domain": target_domain}
    for provider_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        hosts = provider_dir / "hosts"
        if not hosts.is_file():
            continue
        try:
            lines = hosts.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        if target_domain:
            lines = [line for line in lines if target_domain in line.lower()]
        providers.append(
            {
                "provider": provider_dir.name,
                "path": str(hosts),
                "lines": lines[:max_lines],
                "mtime": int(hosts.stat().st_mtime),
            }
        )
    return {"providers": providers, "root": str(root), "filter_domain": target_domain}
