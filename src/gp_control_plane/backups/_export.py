"""gp_control_plane.backups._export — moved from storage.py (split)."""
from __future__ import annotations

from gp_control_plane import __version__
from gp_control_plane.state import has_active_runtime, now_iso, read_state, update_state
from gp_control_plane.storage import connect, db_path, storage_runtime_status, storage_status
from pathlib import Path
from typing import Any
import json
from gp_control_plane.backups._constants import BACKUP_SCHEMA_VERSION
from gp_control_plane.backups._io import _sha256_file, _write_json, _write_text


def _write_snapshot_files(state_dir: Path, root: Path, snapshot_id: str) -> None:
    (root / "domains").mkdir()
    (root / "strategies").mkdir()
    (root / "presets").mkdir()
    (root / "settings").mkdir()
    (root / "history").mkdir()
    # All NDJSON files must describe one SQLite snapshot.  A deferred read
    # transaction starts on the first SELECT, does not acquire a write lock,
    # and therefore lets normal HTTP mutations continue in WAL mode.
    with connect(state_dir) as conn:
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN DEFERRED")
        try:
            domain_count = _export_domains(conn, root)
            strategy_count, link_count = _export_strategies(conn, root)
            preset_count, preset_link_count = _export_domain_presets(conn, root)
            settings_count = _export_app_settings(conn, root)
            history_count = _export_completed_history(conn, root)
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
    manifest = {
        "schema_version": BACKUP_SCHEMA_VERSION,
        "created_at": now_iso(),
        "snapshot_id": snapshot_id,
        "app_version": __version__,
        "storage": "sqlite",
        "db_path": str(db_path(state_dir)),
        "domain_count": str(domain_count),
        "strategy_count": str(strategy_count),
        "link_count": str(link_count),
        "preset_count": str(preset_count),
        "preset_link_count": str(preset_link_count),
        "settings_count": str(settings_count),
        "history_count": str(history_count),
        "semantic_scope": "f01-complete",
        "completed": "true",
    }
    _write_json(root / "manifest.json", manifest)
    _write_checksums(root)


def _export_domains(conn: Any, root: Path) -> int:
    count = 0
    with (root / "domains" / "domains.ndjson").open("w", encoding="utf-8") as handle:
        for row in conn.execute(
            """
            SELECT d.name AS domain, d.service_group
            FROM domains d
            WHERE EXISTS (SELECT 1 FROM strategy_domain_results r WHERE r.domain_id = d.id)
               OR EXISTS (SELECT 1 FROM preset_domains pd WHERE pd.domain_id = d.id)
            ORDER BY d.name ASC
            """
        ):
            count += 1
            handle.write(json.dumps(dict(row), ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")
    return count


def _export_strategies(conn: Any, root: Path) -> tuple[int, int]:
    strategy_count = 0
    link_count = 0
    with (root / "strategies" / "strategies.ndjson").open("w", encoding="utf-8") as handle:
        for row in conn.execute(
            """
            SELECT s.id, s.protocol, s.args, s.status,
                   s.fragmentation_class, s.fragmentation_safe, s.fragmentation_reason,
                   s.family, s.family_key, s.family_rank, s.family_reason
            FROM strategies s
            ORDER BY s.id ASC
            """
        ):
            strategy_count += 1
            handle.write(json.dumps(dict(row), ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")
    with (root / "strategies" / "strategy-domain-links.ndjson").open("w", encoding="utf-8") as handle:
        for row in conn.execute(
            """
            SELECT r.strategy_id AS strategy_id, d.name AS domain, r.protocol, r.source_mode
            FROM strategy_domain_results r
            JOIN domains d ON d.id = r.domain_id
            ORDER BY d.name, r.strategy_id
            """
        ):
            link_count += 1
            payload = dict(row)
            payload["candidate_id"] = payload["strategy_id"]
            payload["scope"] = "common" if payload.pop("source_mode", "") == "multi_domain" else "domain"
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")
    return strategy_count, link_count


def _export_domain_presets(conn: Any, root: Path) -> tuple[int, int]:
    preset_count = 0
    link_count = 0
    with (root / "presets" / "domain-presets.ndjson").open("w", encoding="utf-8") as handle:
        for row in conn.execute(
                """
                SELECT scope, name, kind, label, source_json
                FROM domain_presets
                ORDER BY scope, kind, name
                """
        ):
            preset_count += 1
            source_json = str(row["source_json"] or "{}")
            try:
                source = json.loads(source_json)
            except json.JSONDecodeError:
                source = {}
            payload = {
                "scope": row["scope"],
                "name": row["name"],
                "kind": row["kind"],
                "label": row["label"],
                "source": source if isinstance(source, dict) else {},
            }
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")
    with (root / "presets" / "preset-domains.ndjson").open("w", encoding="utf-8") as handle:
        for row in conn.execute(
                """
                SELECT p.scope, p.name, p.kind, d.name AS domain, pd.position, pd.enabled
                FROM domain_presets p
                JOIN preset_domains pd ON pd.preset_id = p.id
                JOIN domains d ON d.id = pd.domain_id
                ORDER BY p.scope, p.kind, p.name, pd.position, d.name
                """
        ):
            link_count += 1
            handle.write(json.dumps(dict(row), ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")
    return preset_count, link_count


def _export_app_settings(conn: Any, root: Path) -> int:
    count = 0
    with (root / "settings" / "app-settings.ndjson").open("w", encoding="utf-8") as handle:
        for row in conn.execute("SELECT key, value_json, updated_at FROM app_settings ORDER BY key ASC"):
            value_json = str(row["value_json"] or "null")
            try:
                value = json.loads(value_json)
            except json.JSONDecodeError:
                value = None
            payload = {
                "key": str(row["key"] or ""),
                "value": value,
                "updated_at": str(row["updated_at"] or ""),
            }
            count += 1
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")
    return count


def _export_completed_history(conn: Any, root: Path) -> int:
    """Export only terminal history records; active runtime is never portable."""
    count = 0
    terminal = ("success", "failed", "stopped", "cancelled", "completed")
    with (root / "history" / "runs.ndjson").open("w", encoding="utf-8") as handle:
        for row in conn.execute(
            """
            SELECT id, kind, status, timestamp, payload_json
            FROM runs
            WHERE lower(status) IN (?, ?, ?, ?, ?)
            ORDER BY seq ASC
            """,
            terminal,
        ):
            try:
                payload = json.loads(str(row["payload_json"] or "{}"))
            except json.JSONDecodeError:
                # A corrupt history record cannot be represented semantically;
                # it is deliberately excluded instead of copying raw SQLite.
                continue
            if not isinstance(payload, dict):
                continue
            item = {
                "id": str(row["id"] or ""),
                "kind": str(row["kind"] or ""),
                "status": str(row["status"] or ""),
                "timestamp": str(row["timestamp"] or ""),
                "payload": payload,
            }
            count += 1
            handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")
    return count


def _write_checksums(root: Path) -> None:
    rows = []
    for item in sorted(root.rglob("*")):
        if not item.is_file() or item.name == "checksums.sha256":
            continue
        rows.append(f"{_sha256_file(item)}  {item.relative_to(root).as_posix()}")
    _write_text(root / "checksums.sha256", "\n".join(rows) + "\n")
