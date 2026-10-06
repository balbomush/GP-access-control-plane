'Separated candidate_queries responsibility. Existing algorithms and payload shapes are preserved.'

from __future__ import annotations


from pathlib import Path
from typing import Any, Iterator
from .strategy_safety import analyze_strategy
from .discovery_constants import CANDIDATE_RELATION_BATCH_SIZE, CORE_CANDIDATE_JSON_MAX_RESULTS, DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT
from .discovery_values import _bounded_int
from .discovery_domains import _clean_domain_list



def read_candidates(state_dir: Path, *, _connect) -> list[dict[str, Any]]:
    with _connect(state_dir) as conn:
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
    max_results: int = CORE_CANDIDATE_JSON_MAX_RESULTS, _connect) -> dict[str, Any]:
    filters = _candidate_core_filters(
        domains=domains or [],
        strategy_ids=strategy_ids or [],
        protocols=protocols or [],
        source_modes=source_modes or [],
        families=families or [],
        query=query,
    )
    with _connect(state_dir) as conn:
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
    query: str = "", _connect) -> Iterator[dict[str, Any]]:
    filters = _candidate_core_filters(
        domains=domains or [],
        strategy_ids=strategy_ids or [],
        protocols=protocols or [],
        source_modes=source_modes or [],
        families=families or [],
        query=query,
    )
    with _connect(state_dir) as conn:
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
    fragmentation_classes: list[str] | None = None, _connect) -> dict[str, Any]:
    limit = _bounded_int(limit, default=DEFAULT_PAGE_LIMIT, minimum=1, maximum=MAX_PAGE_LIMIT)
    offset = max(0, _bounded_int(offset, default=0, minimum=0, maximum=10_000_000))
    query = query.strip().lower()
    view = view if view in {"domain", "common"} else "domain"
    selected_domains = _clean_domain_list(domains or [])
    selected_domain = domain.strip()
    with _connect(state_dir) as conn:
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
    version = candidate_storage_version(state_dir, _connect=_connect)
    return {
        "candidates": candidates,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(rows) < total,
        "tested_domains": sorted(tested_domains),
        "version": version,
    }


def candidate_storage_version(state_dir: Path, *, _connect) -> dict[str, int]:
    with _connect(state_dir) as conn:
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
    fragmentation_classes: list[str] | None = None, _connect) -> dict[str, Any]:
    limit = _bounded_int(limit, default=DEFAULT_PAGE_LIMIT, minimum=1, maximum=MAX_PAGE_LIMIT)
    offset = max(0, _bounded_int(offset, default=0, minimum=0, maximum=10_000_000))
    query = query.strip().lower()
    clean_fragmentation_classes = _clean_fragmentation_classes(fragmentation_classes or [])
    with _connect(state_dir) as conn:
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
        "version": candidate_storage_version(state_dir, _connect=_connect),
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


def _placeholders(values: list[Any]) -> str:
    return ", ".join("?" for _item in values)


def _unique_nonempty_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if item and item not in result:
            result.append(item)
    return result


def _read_candidate_domain_index_sql(
    conn: Any,
    *,
    limit: int,
    offset: int,
    query: str,
    fragmentation_classes: list[str],
) -> tuple[list[dict[str, Any]], int, int]:
    query_clause, query_params = _strategy_query_clause(query)
    fragmentation_clause, fragmentation_params = _fragmentation_query_clause(fragmentation_classes)
    base = f"""
        FROM domains d
        JOIN strategy_domain_results r ON r.domain_id = d.id
        JOIN strategies s ON s.id = r.strategy_id
        WHERE 1 = 1 {query_clause} {fragmentation_clause}
        GROUP BY d.id, d.name
    """
    count_row = conn.execute(
        f"""
        SELECT COUNT(*) AS count, COALESCE(SUM(strategy_count), 0) AS strategy_total
        FROM (
            SELECT d.id, COUNT(DISTINCT r.strategy_id) AS strategy_count
            {base}
        ) domain_index
        """,
        [*query_params, *fragmentation_params],
    ).fetchone()
    total = int(count_row["count"] or 0) if count_row else 0
    strategy_total = int(count_row["strategy_total"] or 0) if count_row else 0
    domain_rows = conn.execute(
        f"""
        SELECT d.name AS domain, COUNT(DISTINCT r.strategy_id) AS strategy_count
        {base}
        ORDER BY d.name ASC
        LIMIT ? OFFSET ?
        """,
        [*query_params, *fragmentation_params, limit, offset],
    ).fetchall()
    page_domains = [str(row["domain"]) for row in domain_rows]
    if not page_domains:
        return [], total, strategy_total
    page_placeholders = ", ".join("?" for _item in page_domains)
    protocol_rows = conn.execute(
        f"""
        SELECT d.name AS domain, r.protocol AS protocol, COUNT(DISTINCT r.strategy_id) AS count
        FROM domains d
        JOIN strategy_domain_results r ON r.domain_id = d.id
        JOIN strategies s ON s.id = r.strategy_id
        WHERE d.name IN ({page_placeholders}) {query_clause} {fragmentation_clause}
        GROUP BY d.id, d.name, r.protocol
        ORDER BY d.name ASC, r.protocol ASC
        """,
        [*page_domains, *query_params, *fragmentation_params],
    ).fetchall()
    protocols: dict[str, list[dict[str, Any]]] = {}
    for row in protocol_rows:
        protocols.setdefault(str(row["domain"]), []).append(
            {"protocol": str(row["protocol"] or "unknown"), "count": int(row["count"] or 0)}
        )
    rows = [
        {
            "domain": str(row["domain"]),
            "strategy_count": int(row["strategy_count"] or 0),
            "protocols": protocols.get(str(row["domain"]), []),
        }
        for row in domain_rows
    ]
    return rows, total, strategy_total


def _strategy_query_clause(query: str) -> tuple[str, list[Any]]:
    if not query:
        return "", []
    pattern = f"%{query.lower()}%"
    return (
        "AND (LOWER(s.id) LIKE ? OR LOWER(s.protocol) LIKE ? OR LOWER(s.args) LIKE ? OR LOWER(d.name) LIKE ?)",
        [pattern, pattern, pattern, pattern],
    )


def _clean_fragmentation_classes(values: list[str]) -> list[str]:
    allowed = {"position_free", "position_safe", "position_risky", "unknown"}
    result: list[str] = []
    for raw in values:
        for item in str(raw or "").split(","):
            clean = item.strip()
            if clean in allowed and clean not in result:
                result.append(clean)
    return result


def _fragmentation_query_clause(classes: list[str]) -> tuple[str, list[Any]]:
    if not classes:
        return "", []
    placeholders = ", ".join("?" for _item in classes)
    return f"AND s.fragmentation_class IN ({placeholders})", list(classes)


def _iter_db_candidates(conn: Any) -> Iterator[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, protocol, args, status,
               fragmentation_class, fragmentation_safe, fragmentation_reason,
               family, family_key, family_rank, family_reason
        FROM strategies
        ORDER BY id ASC
        """
    ).fetchall()
    yield from _iter_candidates_from_db_rows(conn, rows, include_events=False)


def _candidates_from_db_rows(conn: Any, rows: list[Any], *, include_events: bool) -> list[dict[str, Any]]:
    rows_list = list(rows)
    if not rows_list:
        return []
    strategy_ids = [str(row["id"] or "") for row in rows_list]
    seen_domain_map, common_domain_map = _candidate_domain_maps(conn, strategy_ids)
    return [
        _candidate_from_db(
            conn,
            row,
            include_events=include_events,
            seen_domain_map=seen_domain_map,
            common_domain_map=common_domain_map,
        )
        for row in rows_list
    ]


def _iter_candidates_from_db_rows(conn: Any, rows: Iterator[Any], *, include_events: bool) -> Iterator[dict[str, Any]]:
    batch: list[Any] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= CANDIDATE_RELATION_BATCH_SIZE:
            yield from _candidates_from_db_rows(conn, batch, include_events=include_events)
            batch = []
    if batch:
        yield from _candidates_from_db_rows(conn, batch, include_events=include_events)


def _candidate_domain_maps(conn: Any, strategy_ids: list[str]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    unique_ids = _unique_nonempty_strings(strategy_ids)
    seen_domain_map: dict[str, list[str]] = {strategy_id: [] for strategy_id in unique_ids}
    common_domain_map: dict[str, list[str]] = {strategy_id: [] for strategy_id in unique_ids}
    for start in range(0, len(unique_ids), CANDIDATE_RELATION_BATCH_SIZE):
        chunk = unique_ids[start : start + CANDIDATE_RELATION_BATCH_SIZE]
        if not chunk:
            continue
        rows = conn.execute(
            f"""
            SELECT DISTINCT r.strategy_id AS strategy_id, r.source_mode AS source_mode, d.name AS domain
            FROM strategy_domain_results r
            JOIN domains d ON d.id = r.domain_id
            WHERE r.strategy_id IN ({_placeholders(chunk)})
              AND r.source_mode IN ('single_domain', 'multi_domain')
            ORDER BY r.strategy_id ASC, r.source_mode ASC, d.name ASC
            """,
            chunk,
        ).fetchall()
        for row in rows:
            strategy_id = str(row["strategy_id"] or "")
            domain = str(row["domain"] or "").strip()
            if not strategy_id or not domain:
                continue
            target = common_domain_map if str(row["source_mode"] or "") == "multi_domain" else seen_domain_map
            if domain not in target.setdefault(strategy_id, []):
                target[strategy_id].append(domain)
    return seen_domain_map, common_domain_map


def _candidate_from_db(
    conn: Any,
    row: Any,
    *,
    include_events: bool,
    seen_domain_map: dict[str, list[str]] | None = None,
    common_domain_map: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    row_keys = set(row.keys()) if hasattr(row, "keys") else set()
    analysis = analyze_strategy(str(row["protocol"] or ""), str(row["args"] or ""))
    candidate = {
        "id": row["id"],
        "protocol": row["protocol"],
        "args": row["args"],
        "status": row["status"],
        "first_seen_at": row["first_seen_at"] if "first_seen_at" in row_keys else "",
        "last_seen_at": row["last_seen_at"] if "last_seen_at" in row_keys else "",
        "fragmentation_class": (
            str(row["fragmentation_class"] or "") if "fragmentation_class" in row_keys else ""
        )
        or analysis.fragmentation_class,
        "fragmentation_safe": (
            bool(row["fragmentation_safe"]) if "fragmentation_safe" in row_keys else analysis.fragmentation_safe
        ),
        "fragmentation_reason": (
            str(row["fragmentation_reason"] or "") if "fragmentation_reason" in row_keys else ""
        )
        or analysis.fragmentation_reason,
        "family": (str(row["family"] or "") if "family" in row_keys else "") or analysis.family,
        "family_key": (str(row["family_key"] or "") if "family_key" in row_keys else "") or analysis.family_key,
        "family_rank": int(row["family_rank"] or 0) if "family_rank" in row_keys else analysis.family_rank,
        "family_reason": (str(row["family_reason"] or "") if "family_reason" in row_keys else "") or analysis.family_reason,
    }
    strategy_id = str(row["id"] or "")
    if seen_domain_map is not None and common_domain_map is not None:
        seen_domains = seen_domain_map.get(strategy_id, [])
        common_domains = common_domain_map.get(strategy_id, [])
        if include_events:
            candidate["seen"] = [
                {
                    "run_id": "",
                    "domain": domain,
                    "test": "",
                    "ip_version": "",
                    "seen_at": "",
                }
                for domain in seen_domains
            ]
        else:
            candidate["seen"] = [{"domain": domain} for domain in seen_domains]
        if common_domains:
            candidate["common_seen"] = [{"domains": common_domains}]
        return candidate

    if include_events:
        seen_rows = conn.execute(
            """
            SELECT d.name AS domain
            FROM strategy_domain_results r
            JOIN domains d ON d.id = r.domain_id
            WHERE r.strategy_id = ? AND r.source_mode = 'single_domain'
            ORDER BY d.name ASC
            """,
            (row["id"],),
        ).fetchall()
        common_rows = conn.execute(
            """
            SELECT DISTINCT d.name AS domain
            FROM strategy_domain_results r
            JOIN domains d ON d.id = r.domain_id
            WHERE r.strategy_id = ? AND r.source_mode = 'multi_domain'
            ORDER BY d.name ASC
            """,
            (row["id"],),
        ).fetchall()
        candidate["seen"] = [
            {
                "run_id": "",
                "domain": item["domain"],
                "test": "",
                "ip_version": "",
                "seen_at": "",
            }
            for item in seen_rows
        ]
        common_domains = [str(item["domain"]) for item in common_rows]
        if common_domains:
            candidate["common_seen"] = [{"domains": common_domains}]
        return candidate

    domain_rows = conn.execute(
        """
        SELECT DISTINCT d.name AS domain
        FROM strategy_domain_results r
        JOIN domains d ON d.id = r.domain_id
        WHERE r.strategy_id = ? AND r.source_mode = 'single_domain'
        ORDER BY d.name ASC
        """,
        (row["id"],),
    ).fetchall()
    common_domain_rows = conn.execute(
        """
        SELECT DISTINCT d.name AS domain
        FROM strategy_domain_results r
        JOIN domains d ON d.id = r.domain_id
        WHERE r.strategy_id = ? AND r.source_mode = 'multi_domain'
        ORDER BY d.name ASC
        """,
        (row["id"],),
    ).fetchall()
    candidate["seen"] = [{"domain": item["domain"]} for item in domain_rows]
    common_domains = [str(item["domain"]) for item in common_domain_rows]
    if common_domains:
        candidate["common_seen"] = [{"domains": common_domains}]
    return candidate


def _tested_domains_from_db(conn: Any) -> set[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT d.name AS domain
        FROM domains d
        JOIN strategy_domain_results r ON r.domain_id = d.id
        ORDER BY d.name ASC
        """
    ).fetchall()
    return {str(row["domain"]).strip() for row in rows if str(row["domain"]).strip()}


def _candidate_domains(candidate: dict[str, Any]) -> list[str]:
    seen = candidate.get("seen")
    if not isinstance(seen, list):
        return []
    return sorted({str(item.get("domain") or "").strip() for item in seen if isinstance(item, dict) and str(item.get("domain") or "").strip()})


def _candidate_common_domains(candidate: dict[str, Any]) -> list[str]:
    common_seen = candidate.get("common_seen")
    if not isinstance(common_seen, list):
        return []
    domains: set[str] = set()
    for item in common_seen:
        if not isinstance(item, dict) or not isinstance(item.get("domains"), list):
            continue
        domains.update(str(domain or "").strip() for domain in item["domains"] if str(domain or "").strip())
    return sorted(domains)


def _compact_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    domains = _candidate_domains(candidate)
    common_domains = _candidate_common_domains(candidate)
    result = {
        "id": candidate.get("id"),
        "protocol": candidate.get("protocol"),
        "args": candidate.get("args"),
        "status": candidate.get("status"),
        "first_seen_at": candidate.get("first_seen_at"),
        "last_seen_at": candidate.get("last_seen_at"),
        "fragmentation_class": candidate.get("fragmentation_class"),
        "fragmentation_safe": bool(candidate.get("fragmentation_safe")),
        "fragmentation_reason": candidate.get("fragmentation_reason"),
        "family": candidate.get("family"),
        "family_key": candidate.get("family_key"),
        "family_rank": candidate.get("family_rank"),
        "family_reason": candidate.get("family_reason"),
        "seen": [{"domain": domain} for domain in domains],
    }
    if common_domains:
        result["common_seen"] = [{"domains": common_domains}]
    return result
