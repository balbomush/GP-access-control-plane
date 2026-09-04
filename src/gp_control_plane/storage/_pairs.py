"""gp_control_plane.storage._pairs — moved from storage.py (split)."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from gp_control_plane.storage._connection import connect


def upsert_strategy_pair(
    state_dir: Path,
    *,
    tcp_args: str,
    udp_args: str,
    domain: str,
    overall: str,
    tcp_ms: float = 0.0,
    udp_ms: float = 0.0,
    gateway_ms: float = 0.0,
    updated_at: str = "",
) -> None:
    """Upsert a TCP×UDP pair result (blockcheckS pair engine)."""
    with connect(state_dir) as conn:
        conn.execute(
            """
            INSERT INTO strategy_pairs
                (tcp_args, udp_args, domain, overall, tcp_ms, udp_ms, gateway_ms, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tcp_args, udp_args, domain) DO UPDATE SET
                overall = excluded.overall,
                tcp_ms = excluded.tcp_ms,
                udp_ms = excluded.udp_ms,
                gateway_ms = excluded.gateway_ms,
                updated_at = excluded.updated_at
            """,
            (tcp_args, udp_args, domain, overall, float(tcp_ms or 0), float(udp_ms or 0),
             float(gateway_ms or 0), updated_at),
        )


def read_strategy_pairs(state_dir: Path, domain: str | None = None) -> list[dict[str, Any]]:
    """Read TCP×UDP pair rows, newest first (optionally for one domain)."""
    with connect(state_dir) as conn:
        if domain:
            rows = conn.execute(
                "SELECT tcp_args, udp_args, domain, overall, tcp_ms, udp_ms, gateway_ms, updated_at"
                " FROM strategy_pairs WHERE domain = ? ORDER BY id DESC",
                (domain,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT tcp_args, udp_args, domain, overall, tcp_ms, udp_ms, gateway_ms, updated_at"
                " FROM strategy_pairs ORDER BY id DESC"
            ).fetchall()
    return [
        {
            "tcp_args": r["tcp_args"],
            "udp_args": r["udp_args"],
            "domain": r["domain"],
            "overall": r["overall"],
            "tcp_ms": float(r["tcp_ms"] or 0),
            "udp_ms": float(r["udp_ms"] or 0),
            "gateway_ms": float(r["gateway_ms"] or 0),
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]
