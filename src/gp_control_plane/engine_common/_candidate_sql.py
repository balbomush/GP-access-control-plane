"""engine_common._candidate_sql — moved from strategy_finder.py / blockchecks_backend.py."""
from __future__ import annotations

from typing import Any


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
