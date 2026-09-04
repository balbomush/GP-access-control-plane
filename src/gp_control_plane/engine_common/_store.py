"""engine_common._store — moved from strategy_finder.py / blockchecks_backend.py."""
from __future__ import annotations

from pathlib import Path
from gp_control_plane.storage import connect
from typing import Any, Iterator
from gp_control_plane.engine_common._candidate_sql import _clean_fragmentation_classes, _fragmentation_query_clause, _placeholders, _read_candidate_domain_index_sql, _strategy_query_clause, _unique_nonempty_strings
from gp_control_plane.engine_common._constants import CORE_CANDIDATE_JSON_MAX_RESULTS, DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT
from gp_control_plane.engine_common._models import _candidates_from_db_rows, _compact_candidate, _iter_candidates_from_db_rows, _tested_domains_from_db
from gp_control_plane.engine_common._options import _bounded_int
from gp_control_plane.engine_common._retention import _clean_domain_list

def read_candidates(state_dir: Path) -> list[dict[str, Any]]:
    with connect(state_dir) as conn:
        rows = conn.execute(
            """
            SELECT id, protocol, args, status,
                   fragmentation_class, fragmentation_safe, fragmentation_reason,
                   family, family_key, family_rank, family_reason
            FROM strategies
            ORDER BY id ASC
            """
        ).fetchall()
        return _candidates_from_db_rows(conn, rows, include_events=True)

def read_strategy_candidates_filtered(
    state_dir: Path,
    *,
    domains: list[str] | None = None,
    strategy_ids: list[str] | None = None,
    protocols: list[str] | None = None,
    source_modes: list[str] | None = None,
    families: list[str] | None = None,
    query: str = "",
    max_results: int = CORE_CANDIDATE_JSON_MAX_RESULTS,
) -> dict[str, Any]:
    filters = _candidate_core_filters(
        domains=domains or [],
        strategy_ids=strategy_ids or [],
        protocols=protocols or [],
        source_modes=source_modes or [],
        families=families or [],
        query=query,
    )
    with connect(state_dir) as conn:
        total = _filtered_candidate_total(conn, filters)
        if total > max_results:
            raise ValueError(
                f"strategy candidate result is too large ({total}); narrow filters or use /api/core/strategy-candidates/export"
            )
        rows = list(_iter_filtered_candidate_rows(conn, filters))
        candidates = _candidates_from_db_rows(conn, rows, include_events=True)
    return {"candidates": candidates, "total": total, "filters": _candidate_filter_payload(filters)}

def iter_strategy_candidates_filtered(
    state_dir: Path,
    *,
    domains: list[str] | None = None,
    strategy_ids: list[str] | None = None,
    protocols: list[str] | None = None,
    source_modes: list[str] | None = None,
    families: list[str] | None = None,
    query: str = "",
) -> Iterator[dict[str, Any]]:
    filters = _candidate_core_filters(
        domains=domains or [],
        strategy_ids=strategy_ids or [],
        protocols=protocols or [],
        source_modes=source_modes or [],
        families=families or [],
        query=query,
    )
    with connect(state_dir) as conn:
        yield from _iter_candidates_from_db_rows(conn, _iter_filtered_candidate_rows(conn, filters), include_events=True)

def read_candidate_page(
    state_dir: Path,
    *,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
    query: str = "",
    view: str = "domain",
    domains: list[str] | None = None,
    domain: str = "",
    fragmentation_classes: list[str] | None = None,
) -> dict[str, Any]:
    limit = _bounded_int(limit, default=DEFAULT_PAGE_LIMIT, minimum=1, maximum=MAX_PAGE_LIMIT)
    offset = max(0, _bounded_int(offset, default=0, minimum=0, maximum=10_000_000))
    query = query.strip().lower()
    view = view if view in {"domain", "common"} else "domain"
    selected_domains = _clean_domain_list(domains or [])
    selected_domain = domain.strip()
    with connect(state_dir) as conn:
        tested_domains = _tested_domains_from_db(conn)
        rows, total = _read_candidate_page_sql(
            conn,
            limit=limit,
            offset=offset,
            query=query,
            view=view,
            domains=selected_domains,
            domain=selected_domain,
            fragmentation_classes=_clean_fragmentation_classes(fragmentation_classes or []),
        )
        candidates = [_compact_candidate(candidate) for candidate in _candidates_from_db_rows(conn, rows, include_events=False)]
    version = candidate_storage_version(state_dir)
    return {
        "candidates": candidates,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(rows) < total,
        "tested_domains": sorted(tested_domains),
        "version": version,
    }

def candidate_storage_version(state_dir: Path) -> dict[str, int]:
    with connect(state_dir) as conn:
        row = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM strategies) AS strategy_count,
                (SELECT COUNT(*) FROM strategy_domain_results) AS result_count,
                (SELECT COUNT(DISTINCT domain_id) FROM strategy_domain_results) AS domain_count,
                (
                    SELECT COALESCE(SUM(
                        LENGTH(id) + LENGTH(protocol) + LENGTH(args_hash) + LENGTH(status) +
                        LENGTH(fragmentation_class) + fragmentation_safe + LENGTH(fragmentation_reason) +
                        LENGTH(family) + LENGTH(family_key) + family_rank + LENGTH(family_reason)
                    ), 0)
                    FROM strategies
                ) AS strategy_signature,
                (
                    SELECT COALESCE(SUM(
                        LENGTH(strategy_id) + domain_id + LENGTH(protocol) + LENGTH(source_mode)
                    ), 0)
                    FROM strategy_domain_results
                ) AS result_signature
            """
        ).fetchone()
    return {
        "strategy_count": int(row["strategy_count"] or 0) if row else 0,
        "result_count": int(row["result_count"] or 0) if row else 0,
        "domain_count": int(row["domain_count"] or 0) if row else 0,
        "strategy_signature": int(row["strategy_signature"] or 0) if row else 0,
        "result_signature": int(row["result_signature"] or 0) if row else 0,
    }

def read_candidate_domain_index(
    state_dir: Path,
    *,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
    query: str = "",
    fragmentation_classes: list[str] | None = None,
) -> dict[str, Any]:
    limit = _bounded_int(limit, default=DEFAULT_PAGE_LIMIT, minimum=1, maximum=MAX_PAGE_LIMIT)
    offset = max(0, _bounded_int(offset, default=0, minimum=0, maximum=10_000_000))
    query = query.strip().lower()
    clean_fragmentation_classes = _clean_fragmentation_classes(fragmentation_classes or [])
    with connect(state_dir) as conn:
        tested_domains = _tested_domains_from_db(conn)
        rows, total, strategy_total = _read_candidate_domain_index_sql(
            conn,
            limit=limit,
            offset=offset,
            query=query,
            fragmentation_classes=clean_fragmentation_classes,
        )
    return {
        "domains": rows,
        "total": total,
        "strategy_total": strategy_total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(rows) < total,
        "tested_domains": sorted(tested_domains),
        "version": candidate_storage_version(state_dir),
    }

def _read_candidate_page_sql(
    conn: Any,
    *,
    limit: int,
    offset: int,
    query: str,
    view: str,
    domains: list[str],
    domain: str,
    fragmentation_classes: list[str],
) -> tuple[list[Any], int]:
    query_clause, query_params = _strategy_query_clause(query)
    fragmentation_clause, fragmentation_params = _fragmentation_query_clause(fragmentation_classes)
    if view == "common":
        if len(domains) < 2:
            return [], 0
        placeholders = ", ".join("?" for _item in domains)
        base = f"""
            FROM strategies s
            JOIN strategy_domain_results r ON r.strategy_id = s.id
            JOIN domains d ON d.id = r.domain_id
            WHERE d.name IN ({placeholders}) {query_clause} {fragmentation_clause}
            GROUP BY s.id
            HAVING COUNT(DISTINCT d.name) = ?
        """
        params: list[Any] = [*domains, *query_params, *fragmentation_params, len(domains)]
    elif domain:
        base = f"""
            FROM strategies s
            JOIN strategy_domain_results r ON r.strategy_id = s.id
            JOIN domains d ON d.id = r.domain_id
            WHERE d.name = ? {query_clause} {fragmentation_clause}
            GROUP BY s.id
        """
        params = [domain, *query_params, *fragmentation_params]
    else:
        base = f"""
            FROM strategies s
            JOIN strategy_domain_results r ON r.strategy_id = s.id
            JOIN domains d ON d.id = r.domain_id
            WHERE 1 = 1 {query_clause} {fragmentation_clause}
            GROUP BY s.id
        """
        params = [*query_params, *fragmentation_params]
    total = int(
        conn.execute(
            f"SELECT COUNT(*) AS count FROM (SELECT s.id {base}) AS candidate_page",
            params,
        ).fetchone()["count"]
    )
    rows = conn.execute(
        f"""
        SELECT s.id, s.protocol, s.args, s.status
               , s.fragmentation_class, s.fragmentation_safe, s.fragmentation_reason
               , s.family, s.family_key, s.family_rank, s.family_reason
        {base}
        ORDER BY s.id ASC
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()
    return rows, total

def _candidate_core_filters(
    *,
    domains: list[str],
    strategy_ids: list[str],
    protocols: list[str],
    source_modes: list[str],
    families: list[str],
    query: str,
) -> dict[str, Any]:
    clean_source_modes = [item for item in _unique_nonempty_strings(source_modes) if item in {"single_domain", "multi_domain"}]
    return {
        "domains": _clean_domain_list(domains),
        "strategy_ids": _unique_nonempty_strings(strategy_ids),
        "protocols": _unique_nonempty_strings([item.lower() for item in protocols]),
        "source_modes": clean_source_modes,
        "families": _unique_nonempty_strings([item.lower() for item in families]),
        "query": str(query or "").strip().lower(),
    }

def _candidate_filter_payload(filters: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in filters.items() if value}

def _filtered_candidate_total(conn: Any, filters: dict[str, Any]) -> int:
    where_sql, params = _filtered_candidate_where(filters)
    return int(conn.execute(f"SELECT COUNT(*) AS count FROM strategies s WHERE {where_sql}", params).fetchone()["count"])

def _iter_filtered_candidate_rows(conn: Any, filters: dict[str, Any]) -> Iterator[Any]:
    where_sql, params = _filtered_candidate_where(filters)
    cursor = conn.execute(
        f"""
        SELECT id, protocol, args, status,
               fragmentation_class, fragmentation_safe, fragmentation_reason,
               family, family_key, family_rank, family_reason
        FROM strategies s
        WHERE {where_sql}
        ORDER BY id ASC
        """,
        params,
    )
    yield from cursor

def _filtered_candidate_where(filters: dict[str, Any]) -> tuple[str, list[Any]]:
    clauses = ["1 = 1"]
    params: list[Any] = []
    strategy_ids = list(filters.get("strategy_ids") or [])
    protocols = list(filters.get("protocols") or [])
    families = list(filters.get("families") or [])
    source_modes = list(filters.get("source_modes") or [])
    domains = list(filters.get("domains") or [])
    query = str(filters.get("query") or "")
    if strategy_ids:
        clauses.append(f"s.id IN ({_placeholders(strategy_ids)})")
        params.extend(strategy_ids)
    if protocols:
        clauses.append(f"LOWER(s.protocol) IN ({_placeholders(protocols)})")
        params.extend(protocols)
    if families:
        clauses.append(f"LOWER(s.family) IN ({_placeholders(families)})")
        params.extend(families)
    if domains or source_modes:
        subclauses = ["r.strategy_id = s.id"]
        subparams: list[Any] = []
        domain_join = ""
        if domains:
            domain_join = "JOIN domains d ON d.id = r.domain_id"
            subclauses.append(f"d.name IN ({_placeholders(domains)})")
            subparams.extend(domains)
        if source_modes:
            subclauses.append(f"r.source_mode IN ({_placeholders(source_modes)})")
            subparams.extend(source_modes)
        clauses.append(
            f"""
            EXISTS (
                SELECT 1
                FROM strategy_domain_results r
                {domain_join}
                WHERE {' AND '.join(subclauses)}
            )
            """
        )
        params.extend(subparams)
    if query:
        pattern = f"%{query}%"
        clauses.append(
            """
            (
                LOWER(s.id) LIKE ?
                OR LOWER(s.protocol) LIKE ?
                OR LOWER(s.args) LIKE ?
                OR LOWER(s.family) LIKE ?
                OR LOWER(s.family_key) LIKE ?
                OR EXISTS (
                    SELECT 1
                    FROM strategy_domain_results qr
                    JOIN domains qd ON qd.id = qr.domain_id
                    WHERE qr.strategy_id = s.id AND LOWER(qd.name) LIKE ?
                )
            )
            """
        )
        params.extend([pattern, pattern, pattern, pattern, pattern, pattern])
    return " AND ".join(clauses), params
