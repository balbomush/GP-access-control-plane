"""bs_engine._triage — domain preflight/triage checks using blockcheckS."""

from __future__ import annotations

import json
import subprocess
from typing import Any

from gp_control_plane.discovery_engine import bs_run_env, campaign_lock_info, resolve_bs_binary


def bs_triage_domain(domain: str, timeout_seconds: int = 30) -> dict[str, Any]:
    """Run `bs preflight --json -d <domain>` for domain preflight analysis."""
    clean_domain = str(domain or "").strip()
    if not clean_domain:
        return {
            "domain": "",
            "status": "error",
            "message": "domain parameter is required",
            "checks": [],
        }
    try:
        bs = resolve_bs_binary()
    except RuntimeError as exc:
        return {
            "domain": clean_domain,
            "status": "error",
            "message": str(exc),
            "checks": [],
        }
    cmd = [bs, "preflight", "--json", "-d", clean_domain]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=bs_run_env(),
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "domain": clean_domain,
            "status": "error",
            "message": f"failed to run bs preflight: {exc}",
            "checks": [],
        }
    out = (proc.stdout or "").strip()
    if out and out.startswith("{"):
        try:
            data = json.loads(out)
            if isinstance(data, dict):
                return {"domain": clean_domain, "status": "ok", **data}
        except json.JSONDecodeError:
            pass
    return {
        "domain": clean_domain,
        "status": "ok" if proc.returncode == 0 else "error",
        "output": out or (proc.stderr or "").strip(),
        "returncode": proc.returncode,
    }


def bs_quarantine_status() -> dict[str, Any]:
    """Return campaign lock and quarantine status for blockcheckS."""
    lock = campaign_lock_info()
    if not lock:
        return {"quarantined": False, "status": "idle", "lock": None}
    return {
        "quarantined": True,
        "status": "busy",
        "lock": lock,
        "message": f"blockcheckS campaign lock held by {lock.get('command')} (pid {lock.get('pid')})",
    }
