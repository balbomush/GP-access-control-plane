"""gp_control_plane.storage._candidates — moved from storage.py (split)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from gp_control_plane.storage._connection import connect
from gp_control_plane.storage._writes import _upsert_candidate_event_conn


def upsert_candidate_event(
    state_dir: Path,
    *,
    candidate_id: str,
    protocol: str,
    args: str,
    status: str,
    run_id: str,
    domain: str,
    domains: list[str],
    test: str,
    ip_version: str,
    seen_at: str,
    common: bool,
) -> None:
    with connect(state_dir) as conn:
        _upsert_candidate_event_conn(
            conn,
            candidate_id=candidate_id,
            protocol=protocol,
            args=args,
            status=status,
            run_id=run_id,
            domain=domain,
            domains=domains,
            test=test,
            ip_version=ip_version,
            seen_at=seen_at,
            common=common,
        )


def upsert_candidate_event_conn(
    conn: sqlite3.Connection,
    *,
    candidate_id: str,
    protocol: str,
    args: str,
    status: str,
    run_id: str,
    domain: str,
    domains: list[str],
    test: str,
    ip_version: str,
    seen_at: str,
    common: bool,
) -> None:
    _upsert_candidate_event_conn(
        conn,
        candidate_id=candidate_id,
        protocol=protocol,
        args=args,
        status=status,
        run_id=run_id,
        domain=domain,
        domains=domains,
        test=test,
        ip_version=ip_version,
        seen_at=seen_at,
        common=common,
    )
