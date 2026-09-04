"""core_api payload/query helpers — moved from core_api.py (split)."""

from __future__ import annotations

from typing import Any


def payload_snapshot_id(payload: dict[str, Any]) -> str:
    snapshot_id = str(payload.get("snapshot_id") or payload.get("snapshot") or "").strip()
    if not snapshot_id:
        raise ValueError("snapshot_id is required")
    return snapshot_id


def _domain_list_payload(list_id: str, kind: str, name: str, domains: list[str], meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "list_id": list_id,
        "kind": kind,
        "name": name,
        "domains": list(domains or []),
        "updated_at": str(meta.get("updated_at") or ""),
    }


def query_str(query: dict[str, list[str]], key: str, default: str) -> str:
    values = query.get(key) or []
    return values[0] if values else default


def query_int(query: dict[str, list[str]], key: str, default: int) -> int:
    raw = query_str(query, key, str(default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def query_domains(query: dict[str, list[str]], key: str) -> list[str]:
    values = query.get(key) or []
    domains: list[str] = []
    for value in values:
        domains.extend(item.strip() for item in value.split(",") if item.strip())
    return domains


def query_one(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key) or []
    return str(values[0]).strip() if values else ""


def strategy_candidate_filters_from_query(query: dict[str, list[str]], *, require_filter: bool) -> dict[str, Any]:
    filters = {
        "domains": [*query_domains(query, "domain"), *query_domains(query, "domains")],
        "strategy_ids": [*query_list(query, "strategy_id"), *query_list(query, "strategy_ids")],
        "protocols": [*query_list(query, "protocol"), *query_list(query, "protocols")],
        "source_modes": [*query_list(query, "source_mode"), *query_list(query, "source_modes")],
        "families": [*query_list(query, "family"), *query_list(query, "families")],
        "query": query_str(query, "query", "").strip(),
    }
    has_filter = any(value for value in filters.values())
    if require_filter and not has_filter:
        raise ValueError("strategy candidate filter is required; use /api/core/strategy-candidates/export for full stream")
    return filters


def query_list(query: dict[str, list[str]], key: str) -> list[str]:
    values = query.get(key) or []
    result: list[str] = []
    for value in values:
        result.extend(item.strip() for item in str(value).replace(",", " ").split() if item.strip())
    return result


def payload_string_list(payload: dict[str, Any], key: str) -> list[str]:
    raw = payload.get(key) or []
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def payload_domains(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("domains") or []
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    if not isinstance(raw, list):
        raw = []
    return [str(domain).strip() for domain in raw if str(domain).strip()]
