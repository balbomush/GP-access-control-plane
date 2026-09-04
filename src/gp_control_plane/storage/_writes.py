"""gp_control_plane.storage._writes — moved from storage.py (split)."""
from __future__ import annotations

import json
import sqlite3

from gp_control_plane.storage._constants import SYSTEM_DOMAIN_PRESETS
from gp_control_plane.storage._helpers import _args_hash, _unique_nonempty
from gp_control_plane.strategy_safety import analyze_strategy


def _upsert_strategy_conn(
    conn: sqlite3.Connection,
    *,
    strategy_id: str,
    protocol: str,
    args: str,
    status: str,
    seen_at: str,
) -> None:
    strategy_id = str(strategy_id or "").strip()
    if not strategy_id:
        return
    analysis = analyze_strategy(protocol, args)
    conn.execute(
        """
        INSERT INTO strategies(
            id, protocol, args, args_hash, status,
            fragmentation_class, fragmentation_safe, fragmentation_reason,
            family, family_key, family_rank, family_reason
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            protocol = excluded.protocol,
            args = excluded.args,
            args_hash = excluded.args_hash,
            status = excluded.status,
            fragmentation_class = excluded.fragmentation_class,
            fragmentation_safe = excluded.fragmentation_safe,
            fragmentation_reason = excluded.fragmentation_reason,
            family = excluded.family,
            family_key = excluded.family_key,
            family_rank = excluded.family_rank,
            family_reason = excluded.family_reason
        WHERE strategies.protocol != excluded.protocol
           OR strategies.args != excluded.args
           OR strategies.args_hash != excluded.args_hash
           OR strategies.status != excluded.status
           OR strategies.fragmentation_class != excluded.fragmentation_class
           OR strategies.fragmentation_safe != excluded.fragmentation_safe
           OR strategies.fragmentation_reason != excluded.fragmentation_reason
           OR strategies.family != excluded.family
           OR strategies.family_key != excluded.family_key
           OR strategies.family_rank != excluded.family_rank
           OR strategies.family_reason != excluded.family_reason
        """,
        (
            strategy_id,
            protocol,
            args,
            _args_hash(args),
            status or "candidate",
            analysis.fragmentation_class,
            1 if analysis.fragmentation_safe else 0,
            analysis.fragmentation_reason,
            analysis.family,
            analysis.family_key,
            analysis.family_rank,
            analysis.family_reason,
        ),
    )


def _upsert_domain_conn(
    conn: sqlite3.Connection,
    name: str,
    *,
    service_group: str = "",
    created_at: str = "",
    updated_at: str = "",
) -> int | None:
    domain = str(name or "").strip()
    if not domain:
        return None
    conn.execute(
        """
        INSERT INTO domains(name, service_group)
        VALUES(?, ?)
        ON CONFLICT(name) DO UPDATE SET
            service_group = excluded.service_group
        WHERE domains.service_group = '' AND excluded.service_group != ''
        """,
        (domain, service_group),
    )
    row = conn.execute("SELECT id FROM domains WHERE name = ?", (domain,)).fetchone()
    return int(row["id"]) if row else None


def _save_domain_preset_conn(
    conn: sqlite3.Connection,
    *,
    scope: str,
    name: str,
    kind: str,
    domains: list[str],
    updated_at: str,
    source_json: str = "{}",
) -> None:
    clean_name = str(name or "").strip()
    clean_scope = str(scope or "").strip()
    clean_kind = str(kind or "user").strip() or "user"
    if not clean_scope or not clean_name:
        return
    conn.execute(
        """
        INSERT INTO domain_presets(scope, name, kind, label, source_json)
        VALUES(?, ?, ?, ?, ?)
        ON CONFLICT(scope, name, kind) DO UPDATE SET
            label = excluded.label,
            source_json = excluded.source_json
        WHERE domain_presets.label != excluded.label
           OR domain_presets.source_json != excluded.source_json
        """,
        (clean_scope, clean_name, clean_kind, clean_name, source_json or "{}"),
    )
    preset = conn.execute(
        "SELECT id FROM domain_presets WHERE scope = ? AND name = ? AND kind = ?",
        (clean_scope, clean_name, clean_kind),
    ).fetchone()
    if not preset:
        return
    preset_id = int(preset["id"])
    conn.execute("DELETE FROM preset_domains WHERE preset_id = ?", (preset_id,))
    for position, domain in enumerate(_unique_nonempty(domains)):
        domain_id = _upsert_domain_conn(conn, domain)
        if domain_id is None:
            continue
        conn.execute(
            """
            INSERT OR REPLACE INTO preset_domains(preset_id, domain_id, position, enabled)
            VALUES(?, ?, ?, 1)
            """,
            (preset_id, domain_id, position),
        )


def _ensure_system_domain_presets_conn(conn: sqlite3.Connection) -> None:
    for scope, scoped in SYSTEM_DOMAIN_PRESETS.items():
        for name, preset in scoped.items():
            label = str(preset.get("label") or name)
            source_json = json.dumps({"type": "system"}, ensure_ascii=False, separators=(",", ":"))
            row = conn.execute(
                "SELECT id FROM domain_presets WHERE scope = ? AND name = ? AND kind = 'system'",
                (scope, name),
            ).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE domain_presets
                    SET label = ?, source_json = ?
                    WHERE id = ? AND (label != ? OR source_json != ?)
                    """,
                    (label, source_json, int(row["id"]), label, source_json),
                )
                continue
            _save_domain_preset_conn(
                conn,
                scope=scope,
                name=name,
                kind="system",
                domains=[str(item or "") for item in preset.get("domains") or []],
                updated_at="",
                source_json=source_json,
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
