"""bs_engine._harvest — moved from strategy_finder.py / blockchecks_backend.py."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from gp_control_plane.bs_engine._export import _expand_config_candidate_args
from gp_control_plane.engine_common._upsert import candidate_id_for
from gp_control_plane.state import now_iso
from gp_control_plane.storage import upsert_candidate_event, upsert_strategy_pair


def _harvest_udp(
    state_dir: Path,
    run_id: str,
    kind: str,
    harvested: set[tuple[str, str, str]],
    db: Path,
    domain: str,
) -> None:
    """Harvest working BS UDP strategies (bs pair) as protocol='udp' candidates
    attributed to the primary domain (udp_results carries no domain column)."""
    host = str(domain or "").strip()
    if not host or not db.is_file():
        return
    source_mode = "multi_domain" if "multi" in kind else "single_domain"
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return
    try:
        rows = conn.execute(
            """
            SELECT s.name, s.config_path
            FROM strategies s
            JOIN udp_results u ON u.strategy_id = s.id
            WHERE s.proto = 'udp'
              AND u.status IN ('PASS','THROTTLED')
              AND u.id = (
                SELECT u2.id FROM udp_results u2
                WHERE u2.strategy_id = u.strategy_id
                ORDER BY u2.id DESC LIMIT 1
              )
            """
        ).fetchall()
    except sqlite3.Error:
        conn.close()
        return
    conn.close()
    seen_at = now_iso()
    for name, config_path in rows:
        base_args = str(config_path or "").strip() or str(name or "").strip()
        for args in _expand_config_candidate_args(base_args):
            if not args:
                continue
            protocol = "udp"
            key = (protocol, args, host)
            if key in harvested:
                continue
            harvested.add(key)
            upsert_candidate_event(
                state_dir,
                candidate_id=candidate_id_for(protocol, args),
                protocol=protocol,
                args=args,
                status="working",
                run_id=run_id,
                domain=host,
                domains=[host],
                test="blockchecks-pair",
                ip_version="4",
                seen_at=seen_at,
                common=source_mode == "multi_domain",
            )

def _harvest_pairs(
    state_dir: Path,
    run_id: str,
    db: Path,
    domain: str,
) -> None:
    """Harvest working bs pair_results (TCP×UDP) into GP strategy_pairs.

    pair_results stores strategy *labels* (no ids/run_id); labels are mapped to
    stored config strings via the run DB's strategies table. For file-based
    configs the first lua-desync core is used as the display string (MVP).
    """
    host = str(domain or "").strip()
    if not host or not db.is_file():
        return
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return
    try:
        strat_rows = conn.execute(
            "SELECT name, proto, config_path FROM strategies"
        ).fetchall()
        pair_rows = conn.execute(
            """
            SELECT tcp_strategy, udp_strategy, domain, overall,
                   tcp_ms, gateway_ms, udp_ms
            FROM pair_results
            WHERE overall IN ('PASS','THROTTLED')
            """
        ).fetchall()
    except sqlite3.Error:
        conn.close()
        return
    conn.close()

    def args_for(name: str, proto: str) -> str:
        for row in strat_rows:
            if row[0] == name and (row[1] or "") == proto:
                candidates = _expand_config_candidate_args(str(row[2] or "") or str(row[0] or ""))
                return candidates[0] if candidates else str(row[0] or "")
        return str(name or "")

    seen_at = now_iso()
    for tcp_name, udp_name, pair_domain, overall, tcp_ms, gateway_ms, udp_ms in pair_rows:
        pd = str(pair_domain or "").strip()
        if not (pd == host or pd.startswith(host + "@")):
            continue
        tcp_args = args_for(str(tcp_name or ""), "tcp")
        udp_args = args_for(str(udp_name or ""), "udp")
        if not tcp_args or not udp_args:
            continue
        upsert_strategy_pair(
            state_dir,
            tcp_args=tcp_args,
            udp_args=udp_args,
            domain=pd,
            overall=str(overall or ""),
            tcp_ms=float(tcp_ms or 0),
            udp_ms=float(udp_ms or 0),
            gateway_ms=float(gateway_ms or 0),
            updated_at=seen_at,
        )

def _harvest_passes(
    state_dir: Path,
    run_id: str,
    kind: str,
    harvested: set[tuple[str, str, str]],
    db: Path,
) -> None:
    if not db.is_file():
        return
    source_mode = "multi_domain" if "multi" in kind else "single_domain"
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return
    try:
        rows = conn.execute(
            """
            SELECT t.domain, s.name, s.config_path, s.proto
            FROM tcp_results t
            JOIN strategies s ON s.id = t.strategy_id
            WHERE t.status IN ('PASS','THROTTLED')
              AND (t.bridge_applied IS NULL OR t.bridge_applied = 1)
              AND t.id = (
                SELECT t2.id FROM tcp_results t2
                WHERE t2.strategy_id = s.id AND t2.domain = t.domain
                ORDER BY t2.id DESC LIMIT 1
              )
            """
        ).fetchall()
    except sqlite3.Error:
        conn.close()
        return
    conn.close()
    seen_at = now_iso()
    for domain, name, config_path, proto in rows:
        host = str(domain or "").strip()
        if not host:
            continue
        base_args = str(config_path or "").strip() or str(name or "").strip()
        proto_raw = str(proto or "").lower()
        protocol = "quic" if proto_raw in ("quic", "udp") else "tls"
        for args in _expand_config_candidate_args(base_args):
            if not args:
                continue
            if protocol == "tls" and "quic" in args.lower():
                protocol = "quic"
            key = (protocol, args, host)
            if key in harvested:
                continue
            harvested.add(key)
            upsert_candidate_event(
                state_dir,
                candidate_id=candidate_id_for(protocol, args),
                protocol=protocol,
                args=args,
                status="working",
                run_id=run_id,
                domain=host,
                domains=[host],
                test="blockchecks-scan",
                ip_version="4",
                seen_at=seen_at,
                common=source_mode == "multi_domain",
            )
