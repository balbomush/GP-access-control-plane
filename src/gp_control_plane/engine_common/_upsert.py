"""engine_common._upsert — moved from strategy_finder.py / blockchecks_backend.py."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from gp_control_plane.state import now_iso
from gp_control_plane.storage import connect, upsert_candidate_event


def upsert_candidates(state_dir: Path, parsed: dict[str, Any], run: dict[str, Any]) -> int:
    now = now_iso()
    for raw in parsed.get("candidates") or []:
        if not isinstance(raw, dict):
            continue
        candidate_id = candidate_id_for(str(raw.get("protocol")), str(raw.get("args")))
        upsert_candidate_event(
            state_dir,
            candidate_id=candidate_id,
            protocol=str(raw.get("protocol") or ""),
            args=str(raw.get("args") or ""),
            status="candidate",
            run_id=str(run.get("id") or ""),
            domain=str(raw.get("domain") or ""),
            domains=[],
            test=str(raw.get("test") or ""),
            ip_version=str(raw.get("ip_version") or ""),
            seen_at=now,
            common=False,
        )
    for raw in parsed.get("common_candidates") or []:
        if not isinstance(raw, dict):
            continue
        candidate_id = candidate_id_for(str(raw.get("protocol")), str(raw.get("args")))
        upsert_candidate_event(
            state_dir,
            candidate_id=candidate_id,
            protocol=str(raw.get("protocol") or ""),
            args=str(raw.get("args") or ""),
            status="candidate",
            run_id=str(run.get("id") or ""),
            domain="",
            domains=[str(item or "") for item in run.get("domains", [])] if isinstance(run.get("domains"), list) else [],
            test=str(raw.get("test") or ""),
            ip_version=str(raw.get("ip_version") or ""),
            seen_at=now,
            common=True,
        )
    return candidate_total(state_dir)

def candidate_total(state_dir: Path) -> int:
    with connect(state_dir) as conn:
        return int(conn.execute("SELECT COUNT(*) AS count FROM strategies").fetchone()["count"])

def candidate_id_for(protocol: str, args: str) -> str:
    digest = hashlib.sha256(f"{protocol}\n{args}".encode()).hexdigest()[:12]
    return f"{protocol}-{digest}"
