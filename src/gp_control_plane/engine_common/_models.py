"""engine_common._models — moved from strategy_finder.py / blockchecks_backend.py."""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from gp_control_plane.engine_common._candidate_sql import _placeholders, _unique_nonempty_strings
from gp_control_plane.engine_common._constants import CANDIDATE_RELATION_BATCH_SIZE
from gp_control_plane.strategy_safety import analyze_strategy


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

def _storage_version(state_dir: Path) -> dict[str, int]:
    return _file_version(state_dir / "strategy-finder" / "state.sqlite3")

def _tail_lines(path: Path, max_lines: int) -> list[str]:
    max_lines = max(0, max_lines)
    if max_lines <= 0 or not path.exists():
        return []
    block_size = 8192
    blocks: list[bytes] = []
    line_count = 0
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        while position > 0 and line_count <= max_lines:
            read_size = min(block_size, position)
            position -= read_size
            handle.seek(position)
            block = handle.read(read_size)
            blocks.append(block)
            line_count += block.count(b"\n")
    data = b"".join(reversed(blocks))
    return data.decode("utf-8", errors="replace").splitlines()[-max_lines:]

def _log_delta(path: Path | None, expected_path: str | None, from_size: int | None, max_bytes: int = 200_000) -> str | None:
    if path is None or from_size is None or not expected_path:
        return None
    if str(path) != str(expected_path):
        return None
    if from_size < 0 or not path.exists():
        return None
    current_size = path.stat().st_size
    if current_size < from_size:
        return None
    if current_size - from_size > max_bytes:
        return None
    with path.open("rb") as handle:
        handle.seek(from_size)
        return handle.read(current_size - from_size).decode("utf-8", errors="replace")

def _file_version(path: Path) -> dict[str, int]:
    if not path.exists():
        return {"size": 0, "mtime_ns": 0}
    stat = path.stat()
    return {"size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}

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
