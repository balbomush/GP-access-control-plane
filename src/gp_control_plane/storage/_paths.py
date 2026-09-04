"""gp_control_plane.storage._paths — moved from storage.py (split)."""
from __future__ import annotations

from pathlib import Path
import os
from gp_control_plane.storage._constants import _PRIVATE_DIRECTORY_MODE, _PRIVATE_FILE_MODE, _SQLITE_SIDECAR_SUFFIXES


def _set_private_mode(path: Path, mode: int) -> None:
    """Restrict a state path on POSIX without changing Windows ACL handling."""
    if os.name != "posix":
        return
    try:
        os.chmod(path, mode)
    except OSError:
        # Existing state directories may be managed by another account or filesystem.
        # Continue operating there rather than breaking an existing installation.
        pass


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=_PRIVATE_DIRECTORY_MODE)
    _set_private_mode(path, _PRIVATE_DIRECTORY_MODE)


def _secure_sqlite_files(path: Path) -> None:
    _set_private_mode(path.parent, _PRIVATE_DIRECTORY_MODE)
    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        _set_private_mode(path.with_name(f"{path.name}{suffix}"), _PRIVATE_FILE_MODE)


def _prepare_sqlite_path(path: Path) -> None:
    if os.name == "posix":
        # SQLite otherwise creates the database using the process umask. Pre-creating
        # it ensures its first inode is owner-only even when that umask is permissive.
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, _PRIVATE_FILE_MODE)
        os.close(descriptor)
    _secure_sqlite_files(path)


def db_path(state_dir: Path) -> Path:
    _ensure_private_directory(state_dir)
    root = state_dir / "strategy-finder"
    _ensure_private_directory(root)
    path = root / "state.sqlite3"
    _prepare_sqlite_path(path)
    return path
