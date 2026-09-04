"""gp_control_plane.backups._paths — moved from storage.py (split)."""
from __future__ import annotations

import shutil
import zipfile
from pathlib import Path
from typing import Any

from gp_control_plane.backups._constants import SNAPSHOT_DOWNLOAD_FILES, SNAPSHOT_KEEP
from gp_control_plane.backups._io import archives_dir, backups_dir, snapshots_dir


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
