"""gp_control_plane.backups._restore — moved from storage.py (split)."""
from __future__ import annotations

from gp_control_plane.settings import RUN_SETTINGS_KEY, SERVICE_SETTINGS_KEY
from gp_control_plane.state import has_active_runtime, now_iso, read_state, update_state
from gp_control_plane.storage import connect, db_path, storage_runtime_status, storage_status
from gp_control_plane.strategy_safety import analyze_strategy
from pathlib import Path
from typing import Any
import json
from gp_control_plane.backups._constants import HISTORY_BACKUP_SCHEMA_VERSION
from gp_control_plane.backups._info import snapshot_info, verify_snapshot
from gp_control_plane.backups._io import _sha256_text
from gp_control_plane.backups._manifest import _bool_value, _ensure_snapshot_compatible, _int_value, _read_manifest, _read_ndjson, _read_required_ndjson, _snapshot_replaces_app_settings, _snapshot_replaces_history, _snapshot_replaces_presets, _unique_nonempty
from gp_control_plane.backups._paths import _snapshot_path
from gp_control_plane.backups._snapshots import create_snapshot


def restore_snapshot_if_idle(state_dir: Path, snapshot_id: str) -> dict[str, Any]:
    if has_active_runtime(state_dir):
        return {"restored": False, "queued": True, "reason": "job is running"}
    return restore_snapshot(state_dir, snapshot_id)


def restore_snapshot(state_dir: Path, snapshot_id: str) -> dict[str, Any]:
    path = _snapshot_path(state_dir, snapshot_id)
    if not path.is_dir():
        raise FileNotFoundError(snapshot_id)
    if not verify_snapshot(state_dir, snapshot_id):
        raise ValueError("backup checksum verification failed")
    restore_plan = _load_restore_plan(path)
    return _restore_snapshot_plan(state_dir, snapshot_id, restore_plan, snapshot_info(state_dir, snapshot_id))


def _restore_snapshot_plan(
    state_dir: Path,
    snapshot_id: str,
    restore_plan: dict[str, Any],
    source_snapshot: dict[str, Any],
) -> dict[str, Any]:
    pre_restore = create_snapshot(state_dir, protect_ids={snapshot_id})
    strategies = restore_plan["strategies"]
    links = restore_plan["links"]
    domains = restore_plan["domains"]
    restore_presets = bool(restore_plan["restore_presets"])
    presets = restore_plan["presets"]
    preset_links = restore_plan["preset_links"]
    restore_settings = bool(restore_plan["restore_settings"])
    app_settings = restore_plan["app_settings"]
    restore_history = bool(restore_plan["restore_history"])
    history = restore_plan["history"]
    restored_at = now_iso()
    with connect(state_dir) as conn:
        conn.execute("DELETE FROM strategy_domain_results")
        conn.execute("DELETE FROM strategies")
        if restore_presets:
            conn.execute("DELETE FROM preset_domains")
            conn.execute("DELETE FROM domain_presets")
        for item in domains:
            domain = str(item.get("domain") or item.get("name") or "").strip()
            if not domain:
                continue
            _restore_domain_id(conn, domain, str(item.get("service_group") or ""))
        for item in strategies:
            candidate_id = str(item.get("id") or "").strip()
            if not candidate_id:
                continue
            protocol = str(item.get("protocol") or "")
            args = str(item.get("args") or "")
            analysis = analyze_strategy(protocol, args)
            conn.execute(
                """
                INSERT INTO strategies(
                    id, protocol, args, args_hash, status,
                    fragmentation_class, fragmentation_safe, fragmentation_reason,
                    family, family_key, family_rank, family_reason
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    protocol,
                    args,
                    _sha256_text(args),
                    str(item.get("status") or "candidate"),
                    str(item.get("fragmentation_class") or analysis.fragmentation_class),
                    1 if _bool_value(item.get("fragmentation_safe", analysis.fragmentation_safe)) else 0,
                    str(item.get("fragmentation_reason") or analysis.fragmentation_reason),
                    str(item.get("family") or analysis.family),
                    str(item.get("family_key") or analysis.family_key),
                    int(item.get("family_rank") or analysis.family_rank),
                    str(item.get("family_reason") or analysis.family_reason),
                ),
            )
        known_ids = {
            str(row["id"])
            for row in conn.execute("SELECT id FROM strategies").fetchall()
        }
        for item in links:
            candidate_id = str(item.get("strategy_id") or item.get("candidate_id") or "").strip()
            domain = str(item.get("domain") or "").strip()
            if not candidate_id or not domain or candidate_id not in known_ids:
                continue
            domain_id = _restore_domain_id(conn, domain)
            source_mode = "multi_domain" if str(item.get("scope") or "") == "common" else "single_domain"
            conn.execute(
                """
                INSERT OR REPLACE INTO strategy_domain_results(
                    strategy_id, domain_id, protocol, source_mode
                )
                VALUES(?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    domain_id,
                    str(item.get("protocol") or ""),
                    source_mode,
                ),
            )
        if restore_presets:
            _restore_domain_presets(conn, presets, preset_links)
        if restore_settings:
            _restore_app_settings(conn, app_settings)
        if restore_history:
            _restore_completed_history(conn, history)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            ("restored_snapshot", snapshot_id),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            ("restored_at", restored_at),
        )
    if restore_settings:
        _sync_legacy_state_settings_after_restore(state_dir, app_settings)
    return {
        "restored": True,
        "snapshot": source_snapshot,
        "pre_restore_snapshot": pre_restore.get("snapshot"),
        "strategy_count": len(strategies),
        "settings_count": len(app_settings) if restore_settings else 0,
        "history_count": len(history) if restore_history else 0,
        "full_f01_restore": bool(restore_plan["full_f01_restore"]),
        "limited_restore": not bool(restore_plan["full_f01_restore"]),
        "missing_f01_data": list(restore_plan["missing_f01_data"]),
        "restored_at": restored_at,
    }


def _load_restore_plan(path: Path) -> dict[str, Any]:
    _ensure_snapshot_compatible(path)
    manifest = _read_manifest(path / "manifest.json")
    domains = _read_required_ndjson(path / "domains" / "domains.ndjson")
    strategies = _read_required_ndjson(path / "strategies" / "strategies.ndjson")
    links = _read_required_ndjson(path / "strategies" / "strategy-domain-links.ndjson")
    for item in domains:
        if not str(item.get("domain") or item.get("name") or "").strip():
            raise ValueError("backup contains domain row without domain")
    for item in strategies:
        if not str(item.get("id") or "").strip():
            raise ValueError("backup contains strategy row without id")
    for item in links:
        if not str(item.get("strategy_id") or item.get("candidate_id") or "").strip():
            raise ValueError("backup contains strategy-domain link without strategy id")
        if not str(item.get("domain") or "").strip():
            raise ValueError("backup contains strategy-domain link without domain")
    restore_presets = _snapshot_replaces_presets(path, manifest)
    presets = _read_ndjson(path / "presets" / "domain-presets.ndjson") if restore_presets else []
    preset_links = _read_ndjson(path / "presets" / "preset-domains.ndjson") if restore_presets else []
    restore_settings = _snapshot_replaces_app_settings(path, manifest)
    app_settings = _read_ndjson(path / "settings" / "app-settings.ndjson") if restore_settings else []
    restore_history = _snapshot_replaces_history(path, manifest)
    history = _read_ndjson(path / "history" / "runs.ndjson") if restore_history else []
    schema = str(manifest.get("schema_version") or "")
    if schema == HISTORY_BACKUP_SCHEMA_VERSION and not (restore_presets and restore_settings and restore_history):
        raise ValueError("schema 7 backup is incomplete")
    for item in presets:
        if not str(item.get("scope") or "").strip() or not str(item.get("name") or "").strip():
            raise ValueError("backup contains preset row without scope/name")
    for item in preset_links:
        if not str(item.get("scope") or "").strip() or not str(item.get("name") or "").strip():
            raise ValueError("backup contains preset-domain link without scope/name")
        if not str(item.get("domain") or "").strip():
            raise ValueError("backup contains preset-domain link without domain")
    for item in app_settings:
        if not str(item.get("key") or "").strip():
            raise ValueError("backup contains app setting row without key")
    for item in history:
        if not str(item.get("status") or "").strip():
            raise ValueError("backup contains history row without status")
        if str(item.get("status") or "").strip().lower() not in {"success", "failed", "stopped", "cancelled", "completed"}:
            raise ValueError("backup contains non-terminal history row")
        if not isinstance(item.get("payload"), dict):
            raise ValueError("backup contains history row without payload")
    missing_f01_data: list[str] = []
    if not restore_settings:
        missing_f01_data.append("settings")
    if not restore_history:
        missing_f01_data.append("completed_history")
    return {
        "manifest": manifest,
        "domains": domains,
        "strategies": strategies,
        "links": links,
        "restore_presets": restore_presets,
        "presets": presets,
        "preset_links": preset_links,
        "restore_settings": restore_settings,
        "app_settings": app_settings,
        "restore_history": restore_history,
        "history": history,
        "missing_f01_data": missing_f01_data,
        "full_f01_restore": schema == HISTORY_BACKUP_SCHEMA_VERSION and not missing_f01_data,
    }


def _restore_domain_id(conn: Any, domain: str, service_group: str | None = None) -> int:
    if service_group is None:
        conn.execute("INSERT OR IGNORE INTO domains(name, service_group) VALUES(?, '')", (domain,))
    else:
        conn.execute(
            """
            INSERT INTO domains(name, service_group)
            VALUES(?, ?)
            ON CONFLICT(name) DO UPDATE SET service_group = excluded.service_group
            """,
            (domain, service_group),
        )
    row = conn.execute("SELECT id FROM domains WHERE name = ?", (domain,)).fetchone()
    return int(row["id"])


def _restore_domain_preset(conn: Any, scope: str, name: str, domains: list[str], updated_at: str) -> None:
    conn.execute(
        """
        INSERT INTO domain_presets(scope, name, kind, label)
        VALUES(?, ?, 'user', ?)
        ON CONFLICT(scope, name, kind) DO UPDATE SET label = excluded.label
        """,
        (scope, name, name),
    )
    row = conn.execute(
        "SELECT id FROM domain_presets WHERE scope = ? AND name = ? AND kind = 'user'",
        (scope, name),
    ).fetchone()
    if not row:
        return
    preset_id = int(row["id"])
    conn.execute("DELETE FROM preset_domains WHERE preset_id = ?", (preset_id,))
    for position, domain in enumerate(_unique_nonempty([str(item or "") for item in domains])):
        domain_id = _restore_domain_id(conn, domain)
        conn.execute(
            "INSERT OR REPLACE INTO preset_domains(preset_id, domain_id, position) VALUES(?, ?, ?)",
            (preset_id, domain_id, position),
        )


def _restore_domain_presets(conn: Any, presets: list[dict[str, Any]], links: list[dict[str, Any]]) -> None:
    for item in presets:
        scope = str(item.get("scope") or "").strip()
        name = str(item.get("name") or "").strip()
        kind = str(item.get("kind") or "user").strip() or "user"
        if not scope or not name:
            continue
        source = item.get("source") if isinstance(item.get("source"), dict) else {}
        conn.execute(
            """
            INSERT OR REPLACE INTO domain_presets(scope, name, kind, label, source_json)
            VALUES(?, ?, ?, ?, ?)
            """,
            (
                scope,
                name,
                kind,
                str(item.get("label") or name),
                json.dumps(source, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            ),
        )
    preset_ids: dict[tuple[str, str, str], int] = {}
    for row in conn.execute("SELECT id, scope, name, kind FROM domain_presets").fetchall():
        preset_ids[(str(row["scope"]), str(row["name"]), str(row["kind"]))] = int(row["id"])
    for item in links:
        scope = str(item.get("scope") or "").strip()
        name = str(item.get("name") or "").strip()
        kind = str(item.get("kind") or "user").strip() or "user"
        domain = str(item.get("domain") or "").strip()
        preset_id = preset_ids.get((scope, name, kind))
        if not preset_id or not domain:
            continue
        domain_id = _restore_domain_id(conn, domain)
        conn.execute(
            """
            INSERT OR REPLACE INTO preset_domains(preset_id, domain_id, position, enabled)
            VALUES(?, ?, ?, ?)
            """,
            (
                preset_id,
                domain_id,
                _int_value(item.get("position")),
                1 if _int_value(item.get("enabled")) else 0,
            ),
        )


def _restore_app_settings(conn: Any, app_settings: list[dict[str, Any]]) -> None:
    conn.execute("DELETE FROM app_settings")
    for item in app_settings:
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        conn.execute(
            """
            INSERT OR REPLACE INTO app_settings(key, value_json, updated_at)
            VALUES(?, ?, ?)
            """,
            (
                key,
                json.dumps(item.get("value"), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                str(item.get("updated_at") or ""),
            ),
        )


def _restore_completed_history(conn: Any, history: list[dict[str, Any]]) -> None:
    conn.execute("DELETE FROM runs")
    for item in history:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        conn.execute(
            """
            INSERT INTO runs(id, kind, status, timestamp, payload_json)
            VALUES(?, ?, ?, ?, ?)
            """,
            (
                str(item.get("id") or ""),
                str(item.get("kind") or ""),
                str(item.get("status") or ""),
                str(item.get("timestamp") or ""),
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            ),
        )


def _sync_legacy_state_settings_after_restore(state_dir: Path, app_settings: list[dict[str, Any]]) -> None:
    restored_settings: dict[str, Any] = {}
    for item in app_settings:
        key = str(item.get("key") or "").strip()
        value = item.get("value")
        if key in {RUN_SETTINGS_KEY, SERVICE_SETTINGS_KEY} and isinstance(value, dict):
            restored_settings.update(value)

    def sync_settings(state: dict[str, Any]) -> dict[str, Any]:
        if not restored_settings:
            state.pop("settings", None)
        else:
            state["settings"] = restored_settings
        return state

    update_state(state_dir, sync_settings)
