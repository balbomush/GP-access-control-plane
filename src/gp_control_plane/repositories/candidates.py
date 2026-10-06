'Unchanged candidates operations. The injected factory is the existing storage connection/transaction owner.'

from __future__ import annotations


import sqlite3
from pathlib import Path
from .primitives import _upsert_strategy_conn, _upsert_domain_conn, _unique_nonempty


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
    common: bool, _connect) -> None:
    with _connect(state_dir) as conn:
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


def _upsert_candidate_event_conn(
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
    _upsert_strategy_domain_result_conn(
        conn,
        strategy_id=candidate_id,
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


def _upsert_strategy_domain_result_conn(
    conn: sqlite3.Connection,
    *,
    strategy_id: str,
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
    _upsert_strategy_conn(
        conn,
        strategy_id=strategy_id,
        protocol=protocol,
        args=args,
        status=status,
        seen_at=seen_at,
    )
    source_mode = "multi_domain" if common else "single_domain"
    target_domains = domains if common else ([domain] if domain else [])
    for item in _unique_nonempty([str(value or "") for value in target_domains]):
        domain_id = _upsert_domain_conn(conn, item, created_at=seen_at, updated_at=seen_at)
        if domain_id is None:
            continue
        conn.execute(
            """
            INSERT INTO strategy_domain_results(
                strategy_id, domain_id, protocol, source_mode
            )
            VALUES(?, ?, ?, ?)
            ON CONFLICT(strategy_id, domain_id, source_mode) DO UPDATE SET
                protocol = excluded.protocol
            WHERE strategy_domain_results.protocol != excluded.protocol
            """,
            (strategy_id, domain_id, protocol, source_mode),
        )
