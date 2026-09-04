"""gp_control_plane.backups._vault — moved from storage.py (split)."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import zipfile
from pathlib import Path
from typing import Any

from gp_control_plane.backups._constants import (
    _VAULT_ARCHIVE_NAME,
    _VAULT_DIRECTORY_MODE,
    _VAULT_ENTRY_NAME,
    _VAULT_FILE_MODE,
    _VAULT_ID_RE,
    BACKUP_SCHEMA_VERSION,
    CLEAN_INSTALL_HANDOFF_RELATIVE_PATH,
    CLEAN_INSTALL_VAULT_RELATIVE_PATH,
)
from gp_control_plane.backups._info import (
    _snapshot_info_from_path,
    _verify_restore_semantics,
    _verify_snapshot_path,
)
from gp_control_plane.backups._io import _set_vault_mode, _sha256_file, _write_private_json_atomic
from gp_control_plane.backups._manifest import (
    _safe_extract_target,
    _semantic_manifest_from_snapshot,
)
from gp_control_plane.backups._paths import snapshot_archive_path
from gp_control_plane.backups._restore import _load_restore_plan, _restore_snapshot_plan
from gp_control_plane.backups._snapshots import create_snapshot
from gp_control_plane.resource_budget import BACKUP_STREAM_CHUNK_BYTES
from gp_control_plane.state import has_active_runtime, now_iso
from gp_control_plane.storage import storage_status


def clean_install_vault_dir(target_home: Path | None = None) -> Path:
    """Return the single canonical, install-user-owned clean-install vault."""
    home = Path(target_home) if target_home is not None else Path.home()
    return home / CLEAN_INSTALL_VAULT_RELATIVE_PATH


def clean_install_handoff_path(target_home: Path | None = None) -> Path:
    """Return the fixed device-local handoff path for one vault."""
    home = Path(target_home) if target_home is not None else Path.home()
    return home / CLEAN_INSTALL_HANDOFF_RELATIVE_PATH


def _validate_vault_id(value: str) -> str:
    clean = str(value or "").strip()
    if not _VAULT_ID_RE.fullmatch(clean):
        raise ValueError("invalid clean-install vault id")
    return clean


def validate_clean_install_vault_id(value: object) -> str:
    """Validate the raw public API identifier without normalization."""
    if not isinstance(value, str) or not _VAULT_ID_RE.fullmatch(value):
        raise ValueError("invalid clean-install vault id")
    return value


def _extract_clean_install_archive(archive: Path, staging: Path) -> tuple[Path, str]:
    try:
        with zipfile.ZipFile(archive, "r") as zf:
            members = [item for item in zf.infolist() if not item.is_dir()]
            if not members:
                raise ValueError("clean-install vault archive is empty")
            seen: set[str] = set()
            top_dirs: set[str] = set()
            for member in members:
                name = member.filename.replace("\\", "/")
                if name in seen or name.startswith("/") or "\x00" in name:
                    raise ValueError("clean-install vault archive has unsafe topology")
                seen.add(name)
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("clean-install vault archive contains symlink")
                parts = [part for part in name.split("/") if part]
                if len(parts) < 2 or any(part in {".", ".."} for part in parts):
                    raise ValueError("clean-install vault archive has unsafe topology")
                top_dirs.add(parts[0])
            if len(top_dirs) != 1:
                raise ValueError("clean-install vault archive must contain one snapshot")
            snapshot_id = next(iter(top_dirs))
            if not snapshot_id or snapshot_id.startswith(".") or "/" in snapshot_id or "\\" in snapshot_id:
                raise ValueError("clean-install vault archive has invalid snapshot id")
            for member in members:
                target = _safe_extract_target(staging, member.filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member, "r") as src, target.open("xb") as dst:
                    shutil.copyfileobj(src, dst, length=BACKUP_STREAM_CHUNK_BYTES)
    except zipfile.BadZipFile as exc:
        raise ValueError("clean-install vault archive is not a valid zip") from exc
    path = staging / snapshot_id
    if not path.is_dir() or path.is_symlink():
        raise ValueError("clean-install vault archive snapshot is invalid")
    return path, snapshot_id


def _validate_clean_install_vault_export(archive: Path, state_dir: Path, vault_id: str) -> None:
    """Fail closed before publishing a complete vault to the root phase.

    The root helper intentionally treats the vault as user data and only
    validates its narrow ownership/topology boundary.  The application must
    therefore prove that the copied ZIP itself can be parsed, checksum-checked
    and converted to a supported restore plan *before* it writes ``entry.json``.
    Without that entry the root helper rejects the vault as incomplete, so an
    export failure cannot advance to clean-remove.
    """
    staging = state_dir.parent / f".clean-install-vault-export-check-{vault_id}"
    if staging.exists() or staging.is_symlink():
        raise RuntimeError("clean-install vault export validation staging already exists")
    staging.mkdir(mode=_VAULT_DIRECTORY_MODE)
    try:
        snapshot_path, _snapshot_id = _extract_clean_install_archive(archive, staging)
        if not _verify_snapshot_path(snapshot_path):
            raise ValueError("clean-install vault export checksum verification failed")
        _load_restore_plan(snapshot_path)
    finally:
        if staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging, ignore_errors=True)


# The active clean-install route is deliberately a small device-local handoff.
# It uses the normal semantic snapshot format and never exposes credentials or
# asks root to inspect user data.
def _simple_vault_paths(target_home: Path | None) -> tuple[Path, Path, Path, Path]:
    home = Path(target_home) if target_home is not None else Path.home()
    vault = clean_install_vault_dir(home)
    return vault, vault / _VAULT_ARCHIVE_NAME, vault / _VAULT_ENTRY_NAME, clean_install_handoff_path(home)


def _local_device_binding() -> str:
    """Stable local-only identifier; the raw machine id never enters the vault."""
    try:
        value = Path("/etc/machine-id").read_text(encoding="utf-8").strip()
    except OSError:
        value = os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or os.uname().nodename
    if not value:
        raise RuntimeError("local device identity is unavailable")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _simple_private_file(path: Path, label: str) -> None:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"{label} is missing or unsafe")
    if os.name == "posix":
        metadata = path.stat()
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o777 != _VAULT_FILE_MODE:
            raise PermissionError(f"{label} ownership or permissions are unsafe")


def _simple_private_directory(path: Path, label: str) -> None:
    if not path.is_dir() or path.is_symlink():
        raise ValueError(f"{label} is missing or unsafe")
    if os.name == "posix":
        metadata = path.stat()
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o777 != _VAULT_DIRECTORY_MODE:
            raise PermissionError(f"{label} ownership or permissions are unsafe")


def _simple_read_vault(target_home: Path | None) -> tuple[Path, Path, Path, Path, dict[str, Any]]:
    vault, archive, entry, handoff = _simple_vault_paths(target_home)
    _simple_private_directory(vault, "clean-install vault")
    if {item.name for item in vault.iterdir()} != {_VAULT_ARCHIVE_NAME, _VAULT_ENTRY_NAME, handoff.name}:
        raise ValueError("clean-install vault has unexpected source members")
    _simple_private_file(archive, "clean-install vault archive")
    _simple_private_file(entry, "clean-install vault entry")
    _simple_private_file(handoff, "clean-install handoff")
    try:
        payload = json.loads(entry.read_text(encoding="utf-8"))
        handoff_payload = json.loads(handoff.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("clean-install vault metadata is invalid") from exc
    if not isinstance(payload, dict) or not isinstance(handoff_payload, dict):
        raise ValueError("clean-install vault metadata is invalid")
    vault_id = validate_clean_install_vault_id(payload.get("vault_id"))
    if handoff_payload.get("vault_id") != vault_id:
        raise ValueError("clean-install handoff does not match the vault")
    if payload.get("device_binding") != _local_device_binding() or handoff_payload.get("device_binding") != _local_device_binding():
        raise ValueError("clean-install vault belongs to another device")
    if payload.get("archive_sha256") != _sha256_file(archive):
        raise ValueError("clean-install vault archive checksum does not match")
    if payload.get("archive_size_bytes") != archive.stat().st_size:
        raise ValueError("clean-install vault archive size does not match")
    return vault, archive, entry, handoff, payload


def create_clean_install_vault(state_dir: Path, *, target_home: Path | None = None) -> dict[str, Any]:
    """Export a single semantic vault before the installer removes legacy state."""
    if has_active_runtime(state_dir):
        raise RuntimeError("cannot create clean-install vault while a job is running")
    vault, archive, entry, handoff = _simple_vault_paths(target_home)
    if vault.exists() or vault.is_symlink() or handoff.exists() or handoff.is_symlink():
        raise RuntimeError("a clean-install vault or handoff already exists")
    stage = vault.with_name(f".{vault.name}.stage-{secrets.token_hex(8)}")
    try:
        stage.mkdir(parents=True, mode=_VAULT_DIRECTORY_MODE)
        _set_vault_mode(stage, _VAULT_DIRECTORY_MODE)
        snapshot = create_snapshot(state_dir)
        snapshot_id = str(snapshot["snapshot"]["id"])
        staged_archive = stage / _VAULT_ARCHIVE_NAME
        shutil.copyfile(snapshot_archive_path(state_dir, snapshot_id), staged_archive)
        _set_vault_mode(staged_archive, _VAULT_FILE_MODE)
        _validate_clean_install_vault_export(staged_archive, state_dir, secrets.token_hex(16))
        vault_id = secrets.token_hex(16); device_binding = _local_device_binding()
        payload: dict[str, Any] = {"vault_id": vault_id, "created_at": now_iso(), "schema_version": BACKUP_SCHEMA_VERSION,
                                   "archive_sha256": _sha256_file(staged_archive), "archive_size_bytes": staged_archive.stat().st_size,
                                   "semantic_manifest": _semantic_manifest_from_snapshot(state_dir, snapshot_id), "device_binding": device_binding,
                                   "verification": "pending"}
        _write_private_json_atomic(stage / _VAULT_ENTRY_NAME, payload)
        _write_private_json_atomic(stage / handoff.name, {"vault_id": vault_id, "device_binding": device_binding})
        stage.replace(vault)
        return {"created": True, **payload, "vault_path": str(vault)}
    finally:
        if stage.exists(): shutil.rmtree(stage, ignore_errors=True)


def clean_install_vault_info(*, target_home: Path | None = None) -> dict[str, Any]:
    vault, _, _, handoff = _simple_vault_paths(target_home)
    if not vault.exists() and not handoff.exists():
        return {"exists": False, "pending": False, "vault_path": str(vault)}
    vault, _, _, _, payload = _simple_read_vault(target_home)
    return {
        "exists": True, "pending": True, "vault_path": str(vault),
        "vault_id": payload["vault_id"], "created_at": payload["created_at"],
        "schema_version": payload["schema_version"], "archive_sha256": payload["archive_sha256"],
        "archive_size_bytes": payload["archive_size_bytes"], "verification": "pending",
    }


def restore_clean_install_vault(state_dir: Path, *, vault_id: str, target_home: Path | None = None) -> dict[str, Any]:
    """Restore after an explicit confirmation; any failure leaves vault and handoff."""
    if has_active_runtime(state_dir):
        raise RuntimeError("cannot restore clean-install vault while a job is running")
    vault, archive, entry, handoff, payload = _simple_read_vault(target_home)
    clean_id = validate_clean_install_vault_id(vault_id)
    if payload["vault_id"] != clean_id:
        raise ValueError("clean-install vault id does not match")
    staging = state_dir.parent / f".clean-install-restore-{clean_id}"
    if staging.exists() or staging.is_symlink():
        raise RuntimeError("clean-install restore staging already exists")
    staging.mkdir(parents=True, mode=_VAULT_DIRECTORY_MODE)
    try:
        snapshot_path, snapshot_id = _extract_clean_install_archive(archive, staging)
        if not _verify_snapshot_path(snapshot_path):
            raise ValueError("clean-install vault backup checksum verification failed")
        restore_plan = _load_restore_plan(snapshot_path)
        result = _restore_snapshot_plan(state_dir, snapshot_id, restore_plan, _snapshot_info_from_path(snapshot_path, snapshot_id))
        verification = _verify_restore_semantics(state_dir, restore_plan)
        readiness = storage_status(state_dir)
        if not verification.get("verified") or readiness.get("integrity_check") != "ok" or readiness.get("schema_version") != readiness.get("expected_schema_version"):
            raise RuntimeError("clean-install restore did not pass semantic or SQLite verification")
        # Preserve the complete source on an ordinary cleanup error.  The
        # short-lived copies are local implementation detail, not a second
        # handoff or recovery protocol.
        recovery = staging / "source-recovery"
        recovery.mkdir()
        backups = ((archive, recovery / "archive.zip"), (entry, recovery / "entry.json"), (handoff, recovery / "handoff.json"))
        for source, backup in backups:
            shutil.copy2(source, backup)
            _set_vault_mode(backup, _VAULT_FILE_MODE)
        try:
            entry.unlink(); handoff.unlink(); archive.unlink()
        except OSError:
            for source, backup in backups:
                if not source.exists() and backup.exists():
                    shutil.copy2(backup, source)
                    _set_vault_mode(source, _VAULT_FILE_MODE)
            raise
        try:
            vault.rmdir()
        except OSError:
            for source, backup in backups:
                if not source.exists() and backup.exists():
                    shutil.copy2(backup, source)
                    _set_vault_mode(source, _VAULT_FILE_MODE)
            raise
        cleanup = {"completed": True, "source_deleted": True}
        result.update({"vault_id": clean_id, "verification": verification,
                       "storage_status": {"ready": True, "integrity_check": "ok"},
                       "cleanup": cleanup, "completed": True})
        return result
    finally:
        if staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging, ignore_errors=True)
