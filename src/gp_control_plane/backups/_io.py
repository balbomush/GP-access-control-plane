"""gp_control_plane.backups._io — moved from storage.py (split)."""
from __future__ import annotations

from gp_control_plane.resource_budget import BACKUP_STREAM_CHUNK_BYTES
from pathlib import Path
from typing import Any
import hashlib
import json
import os
import secrets
from gp_control_plane.backups._constants import _VAULT_FILE_MODE


def backups_dir(state_dir: Path) -> Path:
    return state_dir.parent / "backups"


def snapshots_dir(state_dir: Path) -> Path:
    return backups_dir(state_dir) / "snapshots"


def archives_dir(state_dir: Path) -> Path:
    return backups_dir(state_dir) / "archives"


def _set_vault_mode(path: Path, mode: int) -> None:
    if os.name == "posix":
        os.chmod(path, mode)


def _write_private_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{secrets.token_hex(8)}")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _set_vault_mode(temporary, _VAULT_FILE_MODE)
        _fsync_file(temporary)
        temporary.replace(path)
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


def _fsync_file(path: Path) -> None:
    """Persist final private-file metadata, including the required 0600 mode."""
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
