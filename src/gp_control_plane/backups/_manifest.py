"""gp_control_plane.backups._manifest — moved from storage.py (split)."""
from __future__ import annotations

from pathlib import Path
from typing import Any
import json
from gp_control_plane.backups._constants import BACKUP_SCHEMA_VERSION, HISTORY_BACKUP_SCHEMA_VERSION, SUPPORTED_BACKUP_SCHEMA_VERSIONS
from gp_control_plane.backups._paths import _snapshot_path


def _semantic_manifest_from_snapshot(state_dir: Path, snapshot_id: str) -> dict[str, Any]:
    manifest = _read_manifest(_snapshot_path(state_dir, snapshot_id) / "manifest.json")
    return {
        "schema_version": str(manifest.get("schema_version") or ""),
        "semantic_scope": str(manifest.get("semantic_scope") or "limited"),
        "domain_count": _int_value(manifest.get("domain_count")),
        "strategy_count": _int_value(manifest.get("strategy_count")),
        "link_count": _int_value(manifest.get("link_count")),
        "preset_count": _int_value(manifest.get("preset_count")),
        "preset_link_count": _int_value(manifest.get("preset_link_count")),
        "settings_count": _int_value(manifest.get("settings_count")),
        "history_count": _int_value(manifest.get("history_count")),
    }


def _ensure_snapshot_compatible(path: Path) -> None:
    manifest = _read_manifest(path / "manifest.json")
    if not _is_supported_snapshot_manifest(manifest):
        version = manifest.get("schema_version") or "missing"
        raise ValueError(f"unsupported backup schema_version: {version}")


def _is_supported_snapshot_manifest(manifest: dict[str, str]) -> bool:
    return str(manifest.get("schema_version") or "") in SUPPORTED_BACKUP_SCHEMA_VERSIONS


def _safe_zip_top(name: str) -> str:
    normalized = name.replace("\\", "/").lstrip("/")
    parts = [part for part in normalized.split("/") if part and part not in {".", ".."}]
    return parts[0] if parts else ""


def _safe_extract_target(root: Path, name: str) -> Path:
    normalized = name.replace("\\", "/").lstrip("/")
    parts = [part for part in normalized.split("/") if part and part not in {".", ".."}]
    if not parts:
        raise ValueError("invalid empty zip member")
    target = (root / Path(*parts)).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("invalid zip path") from exc
    return target


def _read_ndjson(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    result: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid ndjson in {path.name}") from exc
            if isinstance(payload, dict):
                result.append(payload)
    return result


def _read_required_ndjson(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ValueError(f"backup file not found: {path.name}")
    return _read_ndjson(path)


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _table_count(conn: Any, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"])


def _linked_domain_count(conn: Any) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT domain_id) AS count
            FROM strategy_domain_results
            """
        ).fetchone()["count"]
    )


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _snapshot_replaces_presets(path: Path, manifest: dict[str, str]) -> bool:
    if str(manifest.get("schema_version") or "") not in SUPPORTED_BACKUP_SCHEMA_VERSIONS:
        return False
    preset_file = path / "presets" / "domain-presets.ndjson"
    preset_link_file = path / "presets" / "preset-domains.ndjson"
    if not preset_file.is_file() or not preset_link_file.is_file():
        return False
    try:
        _read_ndjson(preset_file)
        _read_ndjson(preset_link_file)
    except ValueError:
        return False
    return True


def _snapshot_replaces_app_settings(path: Path, manifest: dict[str, str]) -> bool:
    if str(manifest.get("schema_version") or "") not in {"6", BACKUP_SCHEMA_VERSION}:
        return False
    settings_file = path / "settings" / "app-settings.ndjson"
    if not settings_file.is_file():
        return False
    try:
        _read_ndjson(settings_file)
    except ValueError:
        return False
    return True


def _snapshot_replaces_history(path: Path, manifest: dict[str, str]) -> bool:
    if str(manifest.get("schema_version") or "") != HISTORY_BACKUP_SCHEMA_VERSION:
        return False
    history_file = path / "history" / "runs.ndjson"
    if not history_file.is_file():
        return False
    try:
        _read_ndjson(history_file)
    except ValueError:
        return False
    return True


def _unique_nonempty(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if item and item not in result:
            result.append(item)
    return result


def _read_manifest(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("invalid backup manifest.json") from exc
    if not isinstance(payload, dict):
        raise ValueError("backup manifest.json must be an object")
    return {str(key): str(value) for key, value in payload.items()}
