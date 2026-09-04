"""gp_control_plane.backups._snapshots — moved from storage.py (split)."""
from __future__ import annotations

import shutil
import uuid
import zipfile
from pathlib import Path
from typing import Any

from gp_control_plane.backups._constants import POST_RUN_SNAPSHOT_ERROR_MESSAGE_MAX_LENGTH
from gp_control_plane.backups._export import _write_snapshot_files
from gp_control_plane.backups._info import _verify_snapshot_path, snapshot_info, verify_snapshot
from gp_control_plane.backups._io import archives_dir, backups_dir, snapshots_dir
from gp_control_plane.backups._manifest import (
    _ensure_snapshot_compatible,
    _int_value,
    _is_supported_snapshot_manifest,
    _linked_domain_count,
    _read_manifest,
    _safe_extract_target,
    _safe_zip_top,
    _snapshot_replaces_app_settings,
    _snapshot_replaces_presets,
    _table_count,
)
from gp_control_plane.backups._paths import (
    _prune_snapshots,
    _snapshot_path,
    _snapshot_paths,
    _write_latest_marker,
)
from gp_control_plane.state import has_active_runtime, now_iso
from gp_control_plane.storage import connect


def create_snapshot_if_idle(state_dir: Path) -> dict[str, Any]:
    if has_active_runtime(state_dir):
        return {"created": False, "queued": True, "reason": "job is running"}
    return create_snapshot(state_dir)


def create_post_run_snapshot(state_dir: Path) -> dict[str, Any]:
    """Create the post-run snapshot while JobRunner still owns the runtime lock.

    This is intentionally separate from ``create_snapshot_if_idle``: the runner
    remains active during finalization so that no second job or lock-aware
    backup mutation can race the export. The export itself uses one deferred
    SQLite read transaction, so ordinary HTTP mutations remain available in
    WAL mode.
    """
    try:
        created = create_snapshot(state_dir)
    except Exception as exc:  # noqa: BLE001
        return _post_run_snapshot_failure(exc)
    snapshot = created.get("snapshot") if isinstance(created, dict) else None
    snapshot_id = str(snapshot.get("id") or "").strip() if isinstance(snapshot, dict) else ""
    if not snapshot_id:
        return _post_run_snapshot_failure("snapshot export returned no snapshot metadata")
    return {
        "kind": "snapshot",
        "status": "success",
        "completed_at": now_iso(),
        "snapshot_id": snapshot_id,
        "snapshot": snapshot,
    }


def _post_run_snapshot_failure(error: BaseException | str) -> dict[str, str]:
    message = str(error).strip() or (type(error).__name__ if isinstance(error, BaseException) else "snapshot export failed")
    return {
        "kind": "snapshot",
        "status": "failed",
        "completed_at": now_iso(),
        "error_code": "snapshot_export_failed",
        "error_message": " ".join(message.split())[:POST_RUN_SNAPSHOT_ERROR_MESSAGE_MAX_LENGTH],
    }


def create_snapshot(state_dir: Path, protect_ids: set[str] | None = None) -> dict[str, Any]:
    root = snapshots_dir(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    snapshot_id = f"{now_iso().replace(':', '-')}-{uuid.uuid4().hex[:8]}"
    final_dir = root / snapshot_id
    tmp_dir = root / f".tmp-{snapshot_id}"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)
    try:
        _write_snapshot_files(state_dir, tmp_dir, snapshot_id)
        if final_dir.exists():
            shutil.rmtree(final_dir)
        tmp_dir.replace(final_dir)
        _write_latest_marker(state_dir, snapshot_id)
        _prune_snapshots(state_dir, protect_ids=protect_ids)
    except Exception:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return {"created": True, "snapshot": snapshot_info(state_dir, snapshot_id)}


def delete_snapshot_if_idle(state_dir: Path, snapshot_id: str) -> dict[str, Any]:
    if has_active_runtime(state_dir):
        return {"deleted": False, "queued": True, "reason": "job is running"}
    return delete_snapshot(state_dir, snapshot_id)


def delete_snapshot(state_dir: Path, snapshot_id: str) -> dict[str, Any]:
    path = _snapshot_path(state_dir, snapshot_id)
    if not path.is_dir():
        raise FileNotFoundError(snapshot_id)
    shutil.rmtree(path)
    archive = archives_dir(state_dir) / f"{path.name}.zip"
    if archive.exists():
        archive.unlink()
    latest = backups_dir(state_dir) / "latest.txt"
    if latest.exists() and latest.read_text(encoding="utf-8").strip() == path.name:
        remaining = sorted(_snapshot_paths(state_dir), key=lambda item: item.stat().st_mtime, reverse=True)
        if remaining:
            _write_latest_marker(state_dir, remaining[0].name)
        else:
            latest.unlink()
    return {"deleted": True, "snapshot": path.name}


def restore_snapshot_preview(state_dir: Path, snapshot_id: str) -> dict[str, Any]:
    path = _snapshot_path(state_dir, snapshot_id)
    if not path.is_dir():
        raise FileNotFoundError(snapshot_id)
    checksum_ok = verify_snapshot(state_dir, snapshot_id)
    if checksum_ok:
        _ensure_snapshot_compatible(path)
    manifest = _read_manifest(path / "manifest.json")
    backup_domain_count = _int_value(manifest.get("domain_count"))
    backup_strategy_count = _int_value(manifest.get("strategy_count"))
    backup_link_count = _int_value(manifest.get("link_count"))
    backup_preset_count = _int_value(manifest.get("preset_count"))
    backup_preset_link_count = _int_value(manifest.get("preset_link_count"))
    backup_settings_count = _int_value(manifest.get("settings_count"))
    replaces_presets = _snapshot_replaces_presets(path, manifest)
    replaces_settings = _snapshot_replaces_app_settings(path, manifest)
    with connect(state_dir) as conn:
        current_domain_count = _linked_domain_count(conn)
        current_strategy_count = _table_count(conn, "strategies")
        current_link_count = _table_count(conn, "strategy_domain_results")
        current_preset_count = int(
            conn.execute("SELECT COUNT(*) AS count FROM domain_presets WHERE kind = 'user'").fetchone()["count"]
        )
        current_preset_link_count = _table_count(conn, "preset_domains")
        current_settings_count = _table_count(conn, "app_settings")
    return {
        "snapshot": snapshot_info(state_dir, snapshot_id),
        "checksum_ok": checksum_ok,
        "compatible": checksum_ok and _is_supported_snapshot_manifest(manifest),
        "entities": [
            {
                "key": "domains",
                "label": "Домены со стратегиями",
                "current_count": current_domain_count,
                "backup_count": backup_domain_count,
                "will_replace": True,
            },
            {
                "key": "strategies",
                "label": "Стратегии",
                "current_count": current_strategy_count,
                "backup_count": backup_strategy_count,
                "will_replace": True,
            },
            {
                "key": "strategy_domain_links",
                "label": "Связи стратегия-домен",
                "current_count": current_link_count,
                "backup_count": backup_link_count,
                "will_replace": True,
            },
            {
                "key": "user_presets",
                "label": "Пользовательские списки",
                "current_count": current_preset_count,
                "backup_count": backup_preset_count,
                "will_replace": replaces_presets,
            },
            {
                "key": "preset_domain_links",
                "label": "Связи список-домен",
                "current_count": current_preset_link_count,
                "backup_count": backup_preset_link_count,
                "will_replace": replaces_presets,
            },
            {
                "key": "settings",
                "label": "Настройки",
                "current_count": current_settings_count,
                "backup_count": backup_settings_count,
                "will_replace": replaces_settings,
            },
        ],
    }


def import_snapshot_archive(state_dir: Path, archive_bytes: bytes) -> dict[str, Any]:
    root = snapshots_dir(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    upload_id = uuid.uuid4().hex[:8]
    tmp_dir = root / f".upload-{upload_id}"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)
    try:
        archive_path = tmp_dir / "upload.zip"
        archive_path.write_bytes(archive_bytes)
        with zipfile.ZipFile(archive_path, "r") as zf:
            members = [item for item in zf.infolist() if not item.is_dir()]
            top_dirs = {_safe_zip_top(item.filename) for item in members}
            top_dirs.discard("")
            if len(top_dirs) != 1:
                raise ValueError("backup archive must contain exactly one snapshot directory")
            snapshot_id = top_dirs.pop()
            if snapshot_id.startswith("."):
                raise ValueError("invalid snapshot directory")
            for member in members:
                target = _safe_extract_target(tmp_dir, member.filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member, "r") as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
        extracted = tmp_dir / snapshot_id
        if not (extracted / "manifest.json").is_file():
            if (extracted / "manifest.yaml").is_file():
                raise ValueError("unsupported legacy backup format: manifest.yaml")
            raise ValueError("backup manifest.json not found")
        if not _verify_snapshot_path(extracted):
            raise ValueError("backup checksum verification failed")
        _ensure_snapshot_compatible(extracted)
        final = root / snapshot_id
        if final.exists():
            shutil.rmtree(final)
        extracted.replace(final)
        _write_latest_marker(state_dir, snapshot_id)
        _prune_snapshots(state_dir, protect_ids={snapshot_id})
        return {"imported": True, "snapshot": snapshot_info(state_dir, snapshot_id)}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
