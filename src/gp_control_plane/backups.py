from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import uuid
import zipfile
from pathlib import Path
from typing import Any

from . import __version__
from .resource_budget import BACKUP_STREAM_CHUNK_BYTES
from .settings import RUN_SETTINGS_KEY, SERVICE_SETTINGS_KEY
from .state import has_active_runtime, now_iso, read_state, update_state
from .strategy_safety import analyze_strategy
from .storage import connect, db_path, storage_runtime_status, storage_status


SNAPSHOT_KEEP = 5
BACKUP_SCHEMA_VERSION = "7"
SUPPORTED_BACKUP_SCHEMA_VERSIONS = {"5", "6", BACKUP_SCHEMA_VERSION}
HISTORY_BACKUP_SCHEMA_VERSION = "7"
CLEAN_INSTALL_VAULT_RELATIVE_PATH = Path(".local/share/gp-control-plane/clean-install-vault")
_VAULT_FILE_MODE = 0o600
_VAULT_DIRECTORY_MODE = 0o700
_VAULT_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_VAULT_ENTRY_NAME = "entry.json"
_VAULT_ARCHIVE_NAME = "archive.zip"
_VAULT_JOURNAL_NAME = "cleanup.journal.json"
POST_RUN_SNAPSHOT_ERROR_MESSAGE_MAX_LENGTH = 512
SNAPSHOT_DOWNLOAD_FILES = {
    "manifest.json",
    "checksums.sha256",
    "domains/domains.ndjson",
    "strategies/strategies.ndjson",
    "strategies/strategy-domain-links.ndjson",
    "presets/domain-presets.ndjson",
    "presets/preset-domains.ndjson",
    "settings/app-settings.ndjson",
    "history/runs.ndjson",
}


def backups_dir(state_dir: Path) -> Path:
    return state_dir.parent / "backups"


def snapshots_dir(state_dir: Path) -> Path:
    return backups_dir(state_dir) / "snapshots"


def archives_dir(state_dir: Path) -> Path:
    return backups_dir(state_dir) / "archives"


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


def restore_snapshot_if_idle(state_dir: Path, snapshot_id: str) -> dict[str, Any]:
    if has_active_runtime(state_dir):
        return {"restored": False, "queued": True, "reason": "job is running"}
    return restore_snapshot(state_dir, snapshot_id)


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


def clean_install_vault_dir(target_home: Path | None = None) -> Path:
    """Return the single canonical, install-user-owned clean-install vault."""
    home = Path(target_home) if target_home is not None else Path.home()
    return home / CLEAN_INSTALL_VAULT_RELATIVE_PATH


def create_clean_install_vault(state_dir: Path, *, target_home: Path | None = None) -> dict[str, Any]:
    """Create the one pending local vault and reveal its one-time token once.

    The token is never put in the archive, entry, journal, or returned status
    APIs after this call.  The caller must retain it for the explicit clean
    install operation.
    """
    if has_active_runtime(state_dir):
        raise RuntimeError("cannot create clean-install vault while a job is running")
    vault = _prepare_empty_clean_install_vault(target_home)
    snapshot = create_snapshot(state_dir)
    snapshot_id = str(snapshot["snapshot"]["id"])
    archive_source = snapshot_archive_path(state_dir, snapshot_id)
    vault_id = secrets.token_hex(16)
    confirmation_token = secrets.token_urlsafe(32)
    archive = vault / _VAULT_ARCHIVE_NAME
    entry = vault / _VAULT_ENTRY_NAME
    try:
        _copy_private_file(archive_source, archive)
        archive_sha256 = _sha256_file(archive)
        payload: dict[str, Any] = {
            "vault_id": vault_id,
            "created_at": now_iso(),
            "snapshot_id": snapshot_id,
            "schema_version": BACKUP_SCHEMA_VERSION,
            "archive_sha256": archive_sha256,
            "archive_size_bytes": archive.stat().st_size,
            "confirmation_token_sha256": _sha256_text(confirmation_token),
            "verification": "pending",
        }
        _write_private_json_atomic(entry, payload)
    except BaseException:
        # A partially written vault remains deliberately: silently replacing it
        # could overwrite the only user-data copy after a crash.
        raise
    return {
        "created": True,
        "vault_id": vault_id,
        "confirmation_token": confirmation_token,
        "archive_sha256": archive_sha256,
        "archive_size_bytes": archive.stat().st_size,
        "schema_version": BACKUP_SCHEMA_VERSION,
        "snapshot": snapshot["snapshot"],
        "semantic_manifest": _semantic_manifest_from_snapshot(state_dir, snapshot_id),
        "vault_path": str(vault),
    }


def clean_install_vault_info(*, target_home: Path | None = None) -> dict[str, Any]:
    """Read non-secret vault state.  Invalid/pending state is fail-closed."""
    vault = clean_install_vault_dir(target_home)
    if not vault.exists() and not vault.is_symlink():
        return {"exists": False, "pending": False, "vault_path": str(vault)}
    journal = _validate_vault_topology(vault)
    entry = vault / _VAULT_ENTRY_NAME
    archive = vault / _VAULT_ARCHIVE_NAME
    if journal and journal.get("cleanup") == "completed":
        return {
            "exists": True,
            "pending": False,
            "vault_path": str(vault),
            "cleanup": journal.get("cleanup") if journal else "unknown",
        }
    if journal:
        return {
            "exists": True,
            "pending": True,
            "vault_id": journal["vault_id"],
            "verification": journal["verification"],
            "cleanup": journal["cleanup"],
            "vault_path": str(vault),
        }
    payload = _read_vault_entry(vault)
    return {
        "exists": True,
        "pending": True,
        "vault_id": payload["vault_id"],
        "created_at": payload["created_at"],
        "schema_version": payload["schema_version"],
        "archive_sha256": payload["archive_sha256"],
        "archive_size_bytes": payload["archive_size_bytes"],
        "verification": payload.get("verification", "pending"),
        "vault_path": str(vault),
    }


def restore_clean_install_vault(
    state_dir: Path,
    *,
    vault_id: str,
    confirmation_token: str,
    target_home: Path | None = None,
) -> dict[str, Any]:
    """Restore a pending vault, verify semantic data, then consume it safely."""
    if has_active_runtime(state_dir):
        raise RuntimeError("cannot restore clean-install vault while a job is running")
    vault = clean_install_vault_dir(target_home)
    clean_id = _validate_vault_id(vault_id)
    journal = _validate_vault_topology(vault)
    if journal:
        if journal.get("vault_id") != clean_id or journal.get("verification") != "verified":
            raise RuntimeError("clean-install vault has no verified recovery journal")
        verification = _verification_from_vault_journal(journal)
        _verify_vault_confirmation_hash(journal, confirmation_token)
        cleanup = _consume_verified_vault(vault, clean_id, confirmation_token, verification)
        return {
            "restored": True,
            "vault_id": clean_id,
            "verification": verification,
            "cleanup": cleanup,
            "completed": bool(cleanup["completed"]),
            "resumed_cleanup": True,
        }
    payload = _read_vault_entry(vault)
    if clean_id != payload["vault_id"]:
        raise ValueError("clean-install vault id does not match")
    supplied_hash = _sha256_text(str(confirmation_token or ""))
    if not hmac.compare_digest(supplied_hash, str(payload["confirmation_token_sha256"])):
        raise ValueError("clean-install confirmation token does not match")
    archive = vault / _VAULT_ARCHIVE_NAME
    if archive.stat().st_size != int(payload["archive_size_bytes"]):
        raise ValueError("clean-install vault archive size does not match")
    if not hmac.compare_digest(_sha256_file(archive), str(payload["archive_sha256"])):
        raise ValueError("clean-install vault archive checksum does not match")

    staging = state_dir.parent / f".clean-install-vault-restore-{clean_id}"
    if staging.exists() or staging.is_symlink():
        raise RuntimeError("clean-install vault restore staging already exists")
    staging.mkdir(mode=_VAULT_DIRECTORY_MODE, parents=True)
    try:
        snapshot_path, snapshot_id = _extract_clean_install_archive(archive, staging)
        if not _verify_snapshot_path(snapshot_path):
            raise ValueError("backup checksum verification failed")
        restore_plan = _load_restore_plan(snapshot_path)
        source_snapshot = _snapshot_info_from_path(snapshot_path, snapshot_id)
        result = _restore_snapshot_plan(state_dir, snapshot_id, restore_plan, source_snapshot)
        verification = _verify_restore_semantics(state_dir, restore_plan)
        if not verification["verified"]:
            raise RuntimeError("clean-install vault semantic verification failed")
        _mark_vault_verified(vault, payload, verification)
        cleanup = _consume_verified_vault(vault, clean_id, confirmation_token, verification)
        result.update(
            {
                "vault_id": clean_id,
                "verification": verification,
                "cleanup": cleanup,
                "completed": bool(cleanup["completed"]),
            }
        )
        return result
    finally:
        if staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging, ignore_errors=True)


def _prepare_empty_clean_install_vault(target_home: Path | None) -> Path:
    vault = clean_install_vault_dir(target_home)
    home = vault.parents[3]
    if (
        not home.is_absolute()
        or any(part in {".", ".."} for part in home.parts)
        or not home.exists()
        or not home.is_dir()
        or home.is_symlink()
    ):
        raise ValueError("clean-install vault target home is not a canonical directory")
    if vault.exists() or vault.is_symlink():
        journal_payload = _validate_vault_topology(vault)
        if journal_payload is None:
            raise RuntimeError("a pending clean-install vault already exists")
        if journal_payload.get("cleanup") != "completed":
            raise RuntimeError("clean-install vault cleanup is incomplete")
        (vault / _VAULT_JOURNAL_NAME).unlink()
    else:
        vault.mkdir(parents=True, mode=_VAULT_DIRECTORY_MODE)
    _set_vault_mode(vault, _VAULT_DIRECTORY_MODE)
    return vault


def _validate_vault_directory(vault: Path, *, require_existing: bool) -> None:
    if not vault.is_absolute() or any(part in {".", ".."} for part in vault.parts):
        raise ValueError("clean-install vault path is not canonical")
    if require_existing and (not vault.exists() or not vault.is_dir() or vault.is_symlink()):
        raise ValueError("clean-install vault is not a canonical directory")
    if not vault.exists():
        return
    if any(part.is_symlink() for part in (vault, *vault.parents) if part.exists()):
        raise ValueError("clean-install vault path must not contain a symlink")
    _validate_vault_mode_owner(vault, _VAULT_DIRECTORY_MODE)


def _validate_vault_topology(vault: Path) -> dict[str, Any] | None:
    """Validate every member before any vault read, DB mutation, or cleanup.

    Only the durable cleanup journal may coexist with a verified source.  Its
    explicit phases tolerate the two crash windows around archive/entry unlink;
    every other file, symlink, or phase is rejected fail-closed.
    """
    _validate_vault_directory(vault, require_existing=True)
    members: dict[str, Path] = {}
    for member in vault.iterdir():
        if member.name not in {_VAULT_ARCHIVE_NAME, _VAULT_ENTRY_NAME, _VAULT_JOURNAL_NAME}:
            raise ValueError("clean-install vault contains an unexpected member")
        if member.is_symlink() or not member.is_file():
            raise ValueError("clean-install vault file is invalid")
        _validate_vault_file(member)
        members[member.name] = member
    journal_path = members.get(_VAULT_JOURNAL_NAME)
    if journal_path is None:
        if set(members) != {_VAULT_ARCHIVE_NAME, _VAULT_ENTRY_NAME}:
            raise ValueError("clean-install vault has incomplete topology")
        return None
    journal = _read_optional_private_json(journal_path)
    if not journal or not _VAULT_ID_RE.fullmatch(str(journal.get("vault_id") or "")):
        raise ValueError("clean-install vault cleanup journal is invalid")
    if journal.get("verification") != "verified" or not isinstance(journal.get("checks"), dict):
        raise ValueError("clean-install vault cleanup journal is not verified")
    if not re.fullmatch(r"[a-f0-9]{64}", str(journal.get("confirmation_token_sha256") or "")):
        raise ValueError("clean-install vault cleanup journal has no bound confirmation token")
    cleanup = str(journal.get("cleanup") or "")
    phase = str(journal.get("phase") or "")
    if cleanup == "completed" and phase == "completed":
        if set(members) != {_VAULT_JOURNAL_NAME}:
            raise ValueError("completed clean-install vault has unexpected source members")
    elif cleanup in {"pending", "in_progress"} and phase in {"verified", "archive_pending", "archive_deleted"}:
        if _VAULT_ENTRY_NAME not in members and phase != "archive_deleted":
            raise ValueError("clean-install vault cleanup lost its recovery entry")
    else:
        raise ValueError("clean-install vault cleanup journal has invalid phase")
    return journal


def _validate_vault_file(path: Path) -> None:
    if not path.exists() or not path.is_file() or path.is_symlink():
        raise ValueError(f"clean-install vault file is invalid: {path.name}")
    _validate_vault_mode_owner(path, _VAULT_FILE_MODE)


def _validate_vault_mode_owner(path: Path, expected_mode: int) -> None:
    if os.name != "posix":
        return
    stat = path.stat()
    if stat.st_uid != os.geteuid():
        raise PermissionError(f"clean-install vault owner does not match install user: {path.name}")
    if stat.st_mode & 0o777 != expected_mode:
        raise PermissionError(f"clean-install vault mode is unsafe: {path.name}")


def _set_vault_mode(path: Path, mode: int) -> None:
    if os.name == "posix":
        os.chmod(path, mode)


def _copy_private_file(source: Path, destination: Path) -> None:
    temporary = destination.with_name(f".{destination.name}.tmp-{secrets.token_hex(8)}")
    try:
        with source.open("rb") as src, temporary.open("xb") as dst:
            shutil.copyfileobj(src, dst, length=BACKUP_STREAM_CHUNK_BYTES)
        _set_vault_mode(temporary, _VAULT_FILE_MODE)
        temporary.replace(destination)
        _set_vault_mode(destination, _VAULT_FILE_MODE)
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()


def _write_private_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{secrets.token_hex(8)}")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _set_vault_mode(temporary, _VAULT_FILE_MODE)
        temporary.replace(path)
        _set_vault_mode(path, _VAULT_FILE_MODE)
        _fsync_directory(path.parent)
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()


def _fsync_directory(path: Path) -> None:
    """Make a completed vault journal replace durable before destructive I/O."""
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_optional_private_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    _validate_vault_file(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid clean-install vault metadata: {path.name}") from exc
    return payload if isinstance(payload, dict) else None


def _validate_vault_id(value: str) -> str:
    clean = str(value or "").strip()
    if not _VAULT_ID_RE.fullmatch(clean):
        raise ValueError("invalid clean-install vault id")
    return clean


def _read_vault_entry(vault: Path) -> dict[str, Any]:
    if _validate_vault_topology(vault) is not None:
        raise ValueError("clean-install vault source is already in verified cleanup")
    entry = vault / _VAULT_ENTRY_NAME
    archive = vault / _VAULT_ARCHIVE_NAME
    _validate_vault_file(entry)
    _validate_vault_file(archive)
    payload = _read_optional_private_json(entry)
    if not payload:
        raise ValueError("clean-install vault entry is invalid")
    required = {
        "vault_id",
        "created_at",
        "snapshot_id",
        "schema_version",
        "archive_sha256",
        "archive_size_bytes",
        "confirmation_token_sha256",
    }
    if not required.issubset(payload):
        raise ValueError("clean-install vault entry is incomplete")
    payload["vault_id"] = _validate_vault_id(str(payload["vault_id"]))
    if str(payload["schema_version"]) not in SUPPORTED_BACKUP_SCHEMA_VERSIONS:
        raise ValueError("clean-install vault entry has unsupported schema")
    for key in ("archive_sha256", "confirmation_token_sha256"):
        if not re.fullmatch(r"[a-f0-9]{64}", str(payload[key])):
            raise ValueError(f"clean-install vault entry has invalid {key}")
    try:
        payload["archive_size_bytes"] = int(payload["archive_size_bytes"])
    except (TypeError, ValueError) as exc:
        raise ValueError("clean-install vault entry has invalid archive_size_bytes") from exc
    if payload["archive_size_bytes"] <= 0:
        raise ValueError("clean-install vault entry has invalid archive_size_bytes")
    return payload


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


def _snapshot_info_from_path(path: Path, snapshot_id: str) -> dict[str, Any]:
    manifest = _read_manifest(path / "manifest.json")
    return {
        "id": snapshot_id,
        "schema_version": manifest.get("schema_version") or "",
        "compatible": _is_supported_snapshot_manifest(manifest),
        "created_at": manifest.get("created_at") or snapshot_id,
        "completed": manifest.get("completed") == "true",
        "size_bytes": _dir_size(path),
        "strategy_count": int(manifest.get("strategy_count") or 0),
        "preset_count": int(manifest.get("preset_count") or 0),
        "checksum_ok": _verify_snapshot_path(path),
        "files": _snapshot_files(path),
    }


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


def _verify_restore_semantics(state_dir: Path, restore_plan: dict[str, Any]) -> dict[str, Any]:
    """Independently compare restored relational values with the parsed backup."""
    expected_domains = {
        (
            str(item.get("domain") or item.get("name") or ""),
            str(item.get("service_group") or ""),
        )
        for item in restore_plan["domains"]
    }
    expected_strategies = {str(item.get("id") or "") for item in restore_plan["strategies"]}
    expected_links = {
        (
            str(item.get("strategy_id") or item.get("candidate_id") or ""),
            str(item.get("domain") or ""),
            "multi_domain" if str(item.get("scope") or "") == "common" else "single_domain",
        )
        for item in restore_plan["links"]
    }
    expected_history = [
        json.dumps(item.get("payload"), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        for item in restore_plan["history"]
    ]
    expected_presets = {
        (
            str(item.get("scope") or ""),
            str(item.get("name") or ""),
            str(item.get("kind") or "user"),
            str(item.get("label") or item.get("name") or ""),
            json.dumps(
                item.get("source") if isinstance(item.get("source"), dict) else {},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        for item in restore_plan["presets"]
    }
    expected_preset_links = {
        (
            str(item.get("scope") or ""),
            str(item.get("name") or ""),
            str(item.get("kind") or "user"),
            str(item.get("domain") or ""),
            _int_value(item.get("position")),
            1 if _int_value(item.get("enabled")) else 0,
        )
        for item in restore_plan["preset_links"]
    }
    with connect(state_dir) as conn:
        expected_domain_names = {domain for domain, _service_group in expected_domains}
        actual_domains = {
            (str(row["name"]), str(row["service_group"] or ""))
            for row in conn.execute("SELECT name, service_group FROM domains").fetchall()
            if str(row["name"]) in expected_domain_names
        }
        actual_strategies = {str(row["id"]) for row in conn.execute("SELECT id FROM strategies")}
        actual_links = {
            (str(row["strategy_id"]), str(row["domain"]), str(row["source_mode"]))
            for row in conn.execute(
                """
                SELECT r.strategy_id, d.name AS domain, r.source_mode
                FROM strategy_domain_results r JOIN domains d ON d.id = r.domain_id
                """
            )
        }
        actual_history = [
            str(row["payload_json"])
            for row in conn.execute("SELECT payload_json FROM runs ORDER BY seq ASC").fetchall()
        ]
        actual_settings = {
            str(row["key"]): str(row["value_json"])
            for row in conn.execute("SELECT key, value_json FROM app_settings").fetchall()
        }
        actual_presets = {
            (
                str(row["scope"]),
                str(row["name"]),
                str(row["kind"]),
                str(row["label"]),
                str(row["source_json"] or "{}"),
            )
            for row in conn.execute("SELECT scope, name, kind, label, source_json FROM domain_presets").fetchall()
        }
        actual_preset_links = {
            (
                str(row["scope"]),
                str(row["name"]),
                str(row["kind"]),
                str(row["domain"]),
                int(row["position"]),
                int(row["enabled"]),
            )
            for row in conn.execute(
                """
                SELECT p.scope, p.name, p.kind, d.name AS domain, pd.position, pd.enabled
                FROM domain_presets p
                JOIN preset_domains pd ON pd.preset_id = p.id
                JOIN domains d ON d.id = pd.domain_id
                """
            ).fetchall()
        }
    expected_settings = {
        str(item.get("key") or ""): json.dumps(item.get("value"), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        for item in restore_plan["app_settings"]
    }
    checks = {
        "domains": actual_domains == expected_domains,
        "strategies": actual_strategies == expected_strategies,
        "strategy_domain_links": actual_links == expected_links,
        "presets": (not restore_plan["restore_presets"]) or actual_presets == expected_presets,
        "preset_domains": (not restore_plan["restore_presets"]) or actual_preset_links == expected_preset_links,
        "settings": (not restore_plan["restore_settings"]) or actual_settings == expected_settings,
        "completed_history": (not restore_plan["restore_history"]) or actual_history == expected_history,
    }
    runtime = storage_runtime_status(state_dir)
    status = storage_status(state_dir)
    checks["storage_ready"] = bool(runtime.get("ready"))
    checks["integrity_check"] = status.get("integrity_check") == "ok"
    return {
        "verified": all(checks.values()),
        "checks": checks,
        "full_f01_restore": bool(restore_plan["full_f01_restore"]),
        "missing_f01_data": list(restore_plan["missing_f01_data"]),
        "storage": {"ready": runtime.get("ready"), "integrity_check": status.get("integrity_check")},
    }


def _mark_vault_verified(vault: Path, payload: dict[str, Any], verification: dict[str, Any]) -> None:
    payload = dict(payload)
    payload["verification"] = "verified"
    payload["verified_at"] = now_iso()
    _write_private_json_atomic(vault / _VAULT_ENTRY_NAME, payload)
    _write_private_json_atomic(
        vault / _VAULT_JOURNAL_NAME,
        {
            "vault_id": payload["vault_id"],
            "verification": "verified",
            "verified_at": payload["verified_at"],
            "checks": verification["checks"],
            "confirmation_token_sha256": payload["confirmation_token_sha256"],
            "cleanup": "pending",
            "phase": "verified",
        },
    )


def _verification_from_vault_journal(journal: dict[str, Any]) -> dict[str, Any]:
    checks = journal.get("checks")
    if not isinstance(checks, dict) or not checks or not all(value is True for value in checks.values()):
        raise RuntimeError("clean-install vault has no successful verified checks")
    return {"verified": True, "checks": checks}


def _verify_vault_confirmation_hash(payload: dict[str, Any], confirmation_token: str) -> None:
    supplied_hash = _sha256_text(str(confirmation_token or ""))
    if not hmac.compare_digest(supplied_hash, str(payload.get("confirmation_token_sha256") or "")):
        raise RuntimeError("clean-install vault confirmation token does not match for consumption")


def _write_cleanup_journal(journal_path: Path, journal: dict[str, Any], *, cleanup: str, phase: str) -> dict[str, Any]:
    updated = dict(journal)
    updated["cleanup"] = cleanup
    updated["phase"] = phase
    updated["updated_at"] = now_iso()
    _write_private_json_atomic(journal_path, updated)
    return updated


def _consume_verified_vault(
    vault: Path,
    vault_id: str,
    confirmation_token: str,
    verification: dict[str, Any],
) -> dict[str, Any]:
    """Delete source data only after independently re-checking consume guards.

    This deliberately does not trust the caller to have performed the restore
    verification.  A future call site must provide the same one-time token and
    can only consume an entry that is durably marked verified with a pending
    verification journal.
    """
    if not isinstance(verification, dict) or verification.get("verified") is not True:
        raise RuntimeError("clean-install vault consumption requires verified restore")
    checks = verification.get("checks")
    if not isinstance(checks, dict) or not checks or not all(value is True for value in checks.values()):
        raise RuntimeError("clean-install vault consumption requires successful verification checks")
    clean_id = _validate_vault_id(vault_id)
    journal_payload = _validate_vault_topology(vault)
    if not journal_payload or journal_payload.get("vault_id") != clean_id:
        raise RuntimeError("clean-install vault is not durably verified for consumption")
    durable_verification = _verification_from_vault_journal(journal_payload)
    if durable_verification["checks"] != checks:
        raise RuntimeError("clean-install vault verification does not match durable journal")
    _verify_vault_confirmation_hash(journal_payload, confirmation_token)
    journal = vault / _VAULT_JOURNAL_NAME
    if journal_payload.get("cleanup") == "completed":
        return {"completed": True, "source_deleted": True, "status": "completed"}
    if journal_payload.get("cleanup") not in {"pending", "in_progress"}:
        raise RuntimeError("clean-install vault cleanup journal is not resumable")
    journal_payload = _write_cleanup_journal(journal, journal_payload, cleanup="in_progress", phase="archive_pending")
    archive = vault / _VAULT_ARCHIVE_NAME
    entry = vault / _VAULT_ENTRY_NAME
    try:
        if archive.exists():
            _validate_vault_file(archive)
            archive.unlink()
        journal_payload = _write_cleanup_journal(journal, journal_payload, cleanup="in_progress", phase="archive_deleted")
        if entry.exists():
            _validate_vault_file(entry)
            entry.unlink()
    except OSError as exc:
        _write_cleanup_journal(journal, journal_payload, cleanup="in_progress", phase=str(journal_payload["phase"]))
        return {"completed": False, "source_deleted": False, "status": "cleanup_incomplete"}
    _write_cleanup_journal(journal, journal_payload, cleanup="completed", phase="completed")
    return {"completed": True, "source_deleted": True, "status": "completed"}


def list_snapshots(state_dir: Path) -> dict[str, Any]:
    items = [snapshot_info(state_dir, path.name) for path in _snapshot_paths(state_dir)]
    items = [item for item in items if item]
    items.sort(key=lambda item: str(item.get("created_at") or item.get("id") or ""), reverse=True)
    return {
        "snapshots": items[:SNAPSHOT_KEEP],
        "latest": items[0]["id"] if items else "",
        "keep": SNAPSHOT_KEEP,
    }


def snapshot_info(state_dir: Path, snapshot_id: str) -> dict[str, Any]:
    path = _snapshot_path(state_dir, snapshot_id)
    manifest_path = path / "manifest.json"
    manifest = _read_manifest(manifest_path)
    return {
        "id": snapshot_id,
        "schema_version": manifest.get("schema_version") or "",
        "compatible": _is_supported_snapshot_manifest(manifest),
        "created_at": manifest.get("created_at") or snapshot_id,
        "completed": manifest.get("completed") == "true",
        "size_bytes": _dir_size(path),
        "strategy_count": int(manifest.get("strategy_count") or 0),
        "preset_count": int(manifest.get("preset_count") or 0),
        "checksum_ok": verify_snapshot(state_dir, snapshot_id),
        "files": _snapshot_files(path),
    }


def verify_snapshot(state_dir: Path, snapshot_id: str) -> bool:
    path = _snapshot_path(state_dir, snapshot_id)
    return _verify_snapshot_path(path)


def _verify_snapshot_path(path: Path) -> bool:
    checksums = path / "checksums.sha256"
    if not checksums.is_file() or checksums.is_symlink() or path.is_symlink():
        return False
    seen: set[str] = set()
    for line in checksums.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        expected, _, rel = line.partition("  ")
        if not re.fullmatch(r"[a-f0-9]{64}", expected) or not rel or rel in seen:
            return False
        seen.add(rel)
        try:
            target = _safe_extract_target(path, rel)
            target.relative_to(path.resolve())
        except ValueError:
            return False
        if not target.is_file() or target.is_symlink() or _sha256_file(target) != expected:
            return False
    return bool(seen)


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


def snapshot_file_path(state_dir: Path, snapshot_id: str, file_name: str) -> Path:
    path = _snapshot_path(state_dir, snapshot_id)
    if not path.is_dir():
        raise FileNotFoundError(snapshot_id)
    if file_name == "archive":
        return snapshot_archive_path(state_dir, snapshot_id)
    if file_name not in SNAPSHOT_DOWNLOAD_FILES:
        raise FileNotFoundError(file_name)
    candidate = (path / file_name).resolve()
    try:
        candidate.relative_to(path.resolve())
    except ValueError as exc:
        raise FileNotFoundError(file_name) from exc
    if not candidate.is_file():
        raise FileNotFoundError(file_name)
    return candidate


def snapshot_archive_path(state_dir: Path, snapshot_id: str) -> Path:
    path = _snapshot_path(state_dir, snapshot_id)
    if not path.is_dir():
        raise FileNotFoundError(snapshot_id)
    archives = archives_dir(state_dir)
    archives.mkdir(parents=True, exist_ok=True)
    archive = archives / f"{snapshot_id}.zip"
    if archive.exists() and archive.stat().st_mtime_ns >= path.stat().st_mtime_ns:
        return archive
    tmp = archive.with_suffix(".zip.tmp")
    if tmp.exists():
        tmp.unlink()
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for item in sorted(path.rglob("*")):
            if item.is_file():
                zf.write(item, item.relative_to(path.parent))
    tmp.replace(archive)
    return archive


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


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


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


def _prune_snapshots(state_dir: Path, protect_ids: set[str] | None = None) -> None:
    protected = protect_ids or set()
    paths = _snapshot_paths(state_dir)
    paths.sort(key=lambda item: item.name, reverse=True)
    kept = 0
    for old in paths:
        if old.name in protected:
            continue
        kept += 1
        if kept <= SNAPSHOT_KEEP:
            continue
        shutil.rmtree(old, ignore_errors=True)
        archive = archives_dir(state_dir) / f"{old.name}.zip"
        if archive.exists():
            archive.unlink()


def _snapshot_paths(state_dir: Path) -> list[Path]:
    root = snapshots_dir(state_dir)
    if not root.exists():
        return []
    result = []
    for path in root.iterdir():
        if path.is_dir() and not path.name.startswith(".tmp-") and (path / "manifest.json").is_file():
            result.append(path)
    return result


def _snapshot_path(state_dir: Path, snapshot_id: str) -> Path:
    safe = str(snapshot_id or "").strip()
    if not safe or safe.startswith(".") or ".." in safe or "/" in safe or "\\" in safe:
        raise FileNotFoundError(snapshot_id)
    root = snapshots_dir(state_dir).resolve()
    path = (root / safe).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise FileNotFoundError(snapshot_id) from exc
    return path


def _snapshot_files(path: Path) -> list[dict[str, Any]]:
    result = []
    for item in sorted(path.rglob("*")):
        if item.is_file():
            result.append({"path": item.relative_to(path).as_posix(), "size_bytes": item.stat().st_size})
    return result


def _write_latest_marker(state_dir: Path, snapshot_id: str) -> None:
    latest = backups_dir(state_dir) / "latest.txt"
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(snapshot_id + "\n", encoding="utf-8")


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


def _dir_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(BACKUP_STREAM_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, payload: dict[str, str]) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
