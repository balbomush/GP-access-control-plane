"""gp_control_plane.storage._presets — moved from storage.py (split)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gp_control_plane.storage._connection import connect
from gp_control_plane.storage._constants import SYSTEM_DOMAIN_PRESET_NAMES, SYSTEM_DOMAIN_PRESETS
from gp_control_plane.storage._helpers import _unique_nonempty
from gp_control_plane.storage._writes import (
    _ensure_system_domain_presets_conn,
    _save_domain_preset_conn,
)


def read_custom_presets(state_dir: Path) -> dict[str, dict[str, list[str]]]:
    with connect(state_dir) as conn:
        rows = conn.execute(
            """
            SELECT p.scope, p.name, d.name AS domain
            FROM domain_presets p
            LEFT JOIN preset_domains pd ON pd.preset_id = p.id
            LEFT JOIN domains d ON d.id = pd.domain_id
            WHERE p.kind = 'user' AND COALESCE(pd.enabled, 1) = 1
            ORDER BY p.scope, p.name, pd.position, d.name
            """
        ).fetchall()
    result: dict[str, dict[str, list[str]]] = {"finder": {}, "common": {}}
    for row in rows:
        scope = str(row["scope"] or "")
        name = str(row["name"] or "")
        if not scope or not name:
            continue
        result.setdefault(scope, {}).setdefault(name, [])
        domain = str(row["domain"] or "").strip()
        if domain and domain not in result[scope][name]:
            result[scope][name].append(domain)
    return result


def read_system_presets(state_dir: Path) -> dict[str, dict[str, list[str]]]:
    with connect(state_dir) as conn:
        _ensure_system_domain_presets_conn(conn)
        rows = conn.execute(
            """
            SELECT p.scope, p.name, d.name AS domain
            FROM domain_presets p
            LEFT JOIN preset_domains pd ON pd.preset_id = p.id
            LEFT JOIN domains d ON d.id = pd.domain_id
            WHERE p.kind = 'system' AND COALESCE(pd.enabled, 1) = 1
            ORDER BY p.scope, p.name, pd.position, d.name
            """
        ).fetchall()
    result: dict[str, dict[str, list[str]]] = {"finder": {}, "common": {}}
    for scope, scoped in SYSTEM_DOMAIN_PRESETS.items():
        result.setdefault(scope, {})
        for name in scoped:
            result[scope].setdefault(name, [])
    for row in rows:
        scope = str(row["scope"] or "")
        name = str(row["name"] or "")
        if not scope or not name:
            continue
        result.setdefault(scope, {}).setdefault(name, [])
        domain = str(row["domain"] or "").strip()
        if domain and domain not in result[scope][name]:
            result[scope][name].append(domain)
    return result


def read_custom_preset_index(state_dir: Path) -> dict[str, dict[str, dict[str, Any]]]:
    return _read_domain_preset_index(state_dir, kind="user")


def read_system_preset_index(state_dir: Path) -> dict[str, dict[str, dict[str, Any]]]:
    return _read_domain_preset_index(state_dir, kind="system")


def _read_domain_preset_index(state_dir: Path, *, kind: str) -> dict[str, dict[str, dict[str, Any]]]:
    clean_kind = str(kind or "user").strip() or "user"
    with connect(state_dir) as conn:
        if clean_kind == "system":
            _ensure_system_domain_presets_conn(conn)
        rows = conn.execute(
            """
            SELECT p.scope, p.name, p.kind, p.label,
                   COUNT(pd.domain_id) AS total_count,
                   COUNT(CASE WHEN pd.domain_id IS NOT NULL AND COALESCE(pd.enabled, 1) = 1 THEN 1 END) AS enabled_count
            FROM domain_presets p
            LEFT JOIN preset_domains pd ON pd.preset_id = p.id
            WHERE p.kind = ?
            GROUP BY p.id, p.scope, p.name, p.kind, p.label
            ORDER BY p.scope, p.name
            """,
            (clean_kind,),
        ).fetchall()
    result: dict[str, dict[str, dict[str, Any]]] = {"finder": {}, "common": {}}
    for row in rows:
        scope = str(row["scope"] or "")
        name = str(row["name"] or "")
        if not scope or not name:
            continue
        result.setdefault(scope, {})[name] = {
            "name": name,
            "kind": str(row["kind"] or clean_kind),
            "label": str(row["label"] or name),
            "enabled_count": int(row["enabled_count"] or 0),
            "total_count": int(row["total_count"] or 0),
            "updated_at": "",
        }
    return result


def save_custom_presets(state_dir: Path, presets: dict[str, Any], updated_at: str) -> dict[str, dict[str, list[str]]]:
    clean: dict[str, dict[str, list[str]]] = {"finder": {}, "common": {}}
    for scope in ("finder", "common"):
        raw_scope = presets.get(scope) if isinstance(presets, dict) else {}
        if not isinstance(raw_scope, dict):
            continue
        for raw_name, raw_domains in raw_scope.items():
            name = str(raw_name or "").strip()
            if not name or not isinstance(raw_domains, list):
                continue
            if (scope, name) in SYSTEM_DOMAIN_PRESET_NAMES:
                continue
            clean[scope][name] = _unique_nonempty([str(item or "") for item in raw_domains])
    with connect(state_dir) as conn:
        user_presets = conn.execute("SELECT id FROM domain_presets WHERE kind = 'user'").fetchall()
        for row in user_presets:
            conn.execute("DELETE FROM preset_domains WHERE preset_id = ?", (int(row["id"]),))
        conn.execute("DELETE FROM domain_presets WHERE kind = 'user'")
        for scope, scoped in clean.items():
            for name, domains in scoped.items():
                _save_domain_preset_conn(
                    conn,
                    scope=scope,
                    name=name,
                    kind="user",
                    domains=domains,
                    updated_at=updated_at,
                )
    return clean


def save_custom_preset(
    state_dir: Path,
    *,
    scope: str,
    name: str,
    domains: list[str],
    updated_at: str,
    source: dict[str, Any] | None = None,
) -> dict[str, dict[str, list[str]]]:
    clean_scope = str(scope or "").strip()
    clean_name = str(name or "").strip()
    if clean_scope not in {"finder", "common"}:
        raise ValueError("scope must be finder or common")
    if not clean_name:
        raise ValueError("preset name is required")
    if (clean_scope, clean_name) in SYSTEM_DOMAIN_PRESET_NAMES:
        raise ValueError("preset name is reserved for a system list")
    clean_domains = _unique_nonempty([str(item or "") for item in domains])
    if not clean_domains:
        raise ValueError("preset must contain at least one domain")
    source_json = json.dumps(source or {}, ensure_ascii=False, separators=(",", ":"))
    with connect(state_dir) as conn:
        _save_domain_preset_conn(
            conn,
            scope=clean_scope,
            name=clean_name,
            kind="user",
            domains=clean_domains,
            updated_at=updated_at,
            source_json=source_json,
        )
    return read_custom_presets(state_dir)


def save_system_preset(
    state_dir: Path,
    *,
    scope: str,
    name: str,
    domains: list[str],
    updated_at: str,
) -> dict[str, dict[str, list[str]]]:
    clean_scope = str(scope or "").strip()
    clean_name = str(name or "").strip()
    if (clean_scope, clean_name) not in SYSTEM_DOMAIN_PRESET_NAMES:
        raise ValueError("unknown system preset")
    clean_domains = _unique_nonempty([str(item or "") for item in domains])
    preset = SYSTEM_DOMAIN_PRESETS[clean_scope][clean_name]
    source_json = json.dumps({"type": "system"}, ensure_ascii=False, separators=(",", ":"))
    with connect(state_dir) as conn:
        _save_domain_preset_conn(
            conn,
            scope=clean_scope,
            name=clean_name,
            kind="system",
            domains=clean_domains,
            updated_at=updated_at,
            source_json=source_json,
        )
        label = str(preset.get("label") or clean_name)
        conn.execute(
            """
            UPDATE domain_presets
            SET label = ?
            WHERE scope = ? AND name = ? AND kind = 'system' AND label != ?
            """,
            (label, clean_scope, clean_name, label),
        )
    return read_system_presets(state_dir)


def delete_custom_preset(state_dir: Path, *, scope: str, name: str) -> dict[str, dict[str, dict[str, Any]]]:
    clean_scope = str(scope or "").strip()
    clean_name = str(name or "").strip()
    if clean_scope not in {"finder", "common"}:
        raise ValueError("scope must be finder or common")
    if not clean_name:
        raise ValueError("preset name is required")
    with connect(state_dir) as conn:
        conn.execute(
            "DELETE FROM domain_presets WHERE scope = ? AND name = ? AND kind = 'user'",
            (clean_scope, clean_name),
        )
    return read_custom_preset_index(state_dir)


def delete_user_presets(state_dir: Path, *, scope: str, names: list[str]) -> dict[str, dict[str, dict[str, Any]]]:
    clean_scope = str(scope or "").strip()
    if clean_scope not in {"finder", "common"}:
        raise ValueError("scope must be finder or common")
    clean_names = [str(name or "").strip() for name in names]
    clean_names = [name for name in clean_names if name and (clean_scope, name) not in SYSTEM_DOMAIN_PRESET_NAMES]
    if not clean_names:
        raise ValueError("user preset name is required")
    placeholders = ",".join("?" for _ in clean_names)
    with connect(state_dir) as conn:
        conn.execute(
            f"DELETE FROM domain_presets WHERE scope = ? AND kind = 'user' AND name IN ({placeholders})",
            (clean_scope, *clean_names),
        )
    return read_custom_preset_index(state_dir)


def read_preset_domains_page(
    state_dir: Path,
    *,
    scope: str,
    name: str,
    kind: str = "user",
    query: str = "",
    limit: int = 200,
    offset: int = 0,
    include_disabled: bool = True,
) -> dict[str, Any]:
    clean_scope = str(scope or "").strip()
    clean_name = str(name or "").strip()
    clean_kind = str(kind or "user").strip() or "user"
    clean_query = str(query or "").strip().lower()
    clean_limit = max(1, min(int(limit or 200), 1000))
    clean_offset = max(0, int(offset or 0))
    if not clean_scope or not clean_name:
        return _empty_preset_domains_page(clean_scope, clean_name, clean_kind, clean_query, clean_limit, clean_offset)
    filters = ["p.scope = ?", "p.name = ?", "p.kind = ?"]
    params: list[Any] = [clean_scope, clean_name, clean_kind]
    if clean_query:
        filters.append("LOWER(d.name) LIKE ?")
        params.append(f"%{clean_query}%")
    if not include_disabled:
        filters.append("COALESCE(pd.enabled, 1) = 1")
    where = " AND ".join(filters)
    with connect(state_dir) as conn:
        if clean_kind == "system":
            _ensure_system_domain_presets_conn(conn)
        total_row = conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM domain_presets p
            JOIN preset_domains pd ON pd.preset_id = p.id
            JOIN domains d ON d.id = pd.domain_id
            WHERE {where}
            """,
            params,
        ).fetchone()
        total = int(total_row["count"]) if total_row else 0
        rows = conn.execute(
            f"""
            SELECT d.name AS domain, pd.position, COALESCE(pd.enabled, 1) AS enabled
            FROM domain_presets p
            JOIN preset_domains pd ON pd.preset_id = p.id
            JOIN domains d ON d.id = pd.domain_id
            WHERE {where}
            ORDER BY pd.position, d.name
            LIMIT ? OFFSET ?
            """,
            [*params, clean_limit, clean_offset],
        ).fetchall()
    domains = [
        {
            "domain": str(row["domain"] or ""),
            "position": int(row["position"] or 0),
            "enabled": bool(row["enabled"]),
        }
        for row in rows
    ]
    return {
        "scope": clean_scope,
        "name": clean_name,
        "kind": clean_kind,
        "query": clean_query,
        "limit": clean_limit,
        "offset": clean_offset,
        "total": total,
        "has_more": clean_offset + len(domains) < total,
        "domains": domains,
    }


def set_preset_domain_enabled(
    state_dir: Path,
    *,
    scope: str,
    name: str,
    domain: str,
    enabled: bool,
    updated_at: str,
    kind: str = "user",
) -> dict[str, Any]:
    clean_scope = str(scope or "").strip()
    clean_name = str(name or "").strip()
    clean_domain = str(domain or "").strip()
    clean_kind = str(kind or "user").strip() or "user"
    if clean_kind not in {"user", "system"}:
        raise ValueError("preset kind must be user or system")
    if clean_scope not in {"finder", "common"}:
        raise ValueError("scope must be finder or common")
    if not clean_name:
        raise ValueError("preset name is required")
    if not clean_domain:
        raise ValueError("domain is required")
    with connect(state_dir) as conn:
        if clean_kind == "system":
            _ensure_system_domain_presets_conn(conn)
        row = conn.execute(
            """
            SELECT pd.preset_id, pd.domain_id
            FROM domain_presets p
            JOIN preset_domains pd ON pd.preset_id = p.id
            JOIN domains d ON d.id = pd.domain_id
            WHERE p.scope = ? AND p.name = ? AND p.kind = ? AND d.name = ?
            """,
            (clean_scope, clean_name, clean_kind, clean_domain),
        ).fetchone()
        if not row:
            raise ValueError("preset domain was not found")
        conn.execute(
            "UPDATE preset_domains SET enabled = ? WHERE preset_id = ? AND domain_id = ?",
            (1 if enabled else 0, int(row["preset_id"]), int(row["domain_id"])),
        )
    return {
        "scope": clean_scope,
        "name": clean_name,
        "kind": clean_kind,
        "domain": clean_domain,
        "enabled": bool(enabled),
    }


def _empty_preset_domains_page(
    scope: str,
    name: str,
    kind: str,
    query: str,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    return {
        "scope": scope,
        "name": name,
        "kind": kind,
        "query": query,
        "limit": limit,
        "offset": offset,
        "total": 0,
        "has_more": False,
        "domains": [],
    }
