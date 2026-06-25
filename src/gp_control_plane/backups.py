from __future__ import annotations

import hashlib
import json
import shutil
import uuid
import zipfile
from pathlib import Path
from typing import Any

from . import __version__
from .state import now_iso, read_state
from .storage import connect, db_path, import_candidates_json_if_needed, read_custom_presets
from .strategy_finder import candidate_id_for, domain_sets


SNAPSHOT_KEEP = 5


def backups_dir(state_dir: Path) -> Path:
    return state_dir.parent / "backups"


def snapshots_dir(state_dir: Path) -> Path:
    return backups_dir(state_dir) / "snapshots"


def archives_dir(state_dir: Path) -> Path:
    return backups_dir(state_dir) / "archives"


def create_snapshot_if_idle(state_dir: Path) -> dict[str, Any]:
    state = read_state(state_dir)
    if state.get("current_job"):
        return {"created": False, "queued": True, "reason": "job is running"}
    return create_snapshot(state_dir)


def create_snapshot(state_dir: Path) -> dict[str, Any]:
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
        _prune_snapshots(state_dir)
    except Exception:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return {"created": True, "snapshot": snapshot_info(state_dir, snapshot_id)}


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
    manifest_path = path / "manifest.yaml"
    manifest = _read_simple_manifest(manifest_path)
    return {
        "id": snapshot_id,
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
    checksums = path / "checksums.sha256"
    if not checksums.is_file():
        return False
    for line in checksums.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        expected, _, rel = line.partition("  ")
        target = path / rel
        if not target.is_file() or _sha256_file(target) != expected:
            return False
    return True


def snapshot_file_path(state_dir: Path, snapshot_id: str, file_name: str) -> Path:
    path = _snapshot_path(state_dir, snapshot_id)
    if not path.is_dir():
        raise FileNotFoundError(snapshot_id)
    if file_name == "archive":
        return snapshot_archive_path(state_dir, snapshot_id)
    candidate = (path / file_name).resolve()
    if not str(candidate).startswith(str(path.resolve())) or not candidate.is_file():
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
    (root / "strategies").mkdir()
    (root / "presets").mkdir()
    (root / "settings").mkdir()
    strategy_count = _export_strategies(state_dir, root)
    preset_count = _export_presets(root, state_dir)
    _write_text(root / "settings" / "discovery-profiles.yaml", "profiles: []\n")
    manifest = {
        "schema_version": "1",
        "created_at": now_iso(),
        "snapshot_id": snapshot_id,
        "app_version": __version__,
        "storage": "sqlite",
        "db_path": str(db_path(state_dir)),
        "strategy_count": str(strategy_count),
        "preset_count": str(preset_count),
        "completed": "true",
    }
    _write_text(root / "manifest.yaml", _yaml_mapping(manifest))
    _write_checksums(root)


def _export_strategies(state_dir: Path, root: Path) -> int:
    import_candidates_json_if_needed(state_dir, candidate_id_for)
    strategy_count = 0
    with connect(state_dir) as conn:
        with (root / "strategies" / "strategies.ndjson").open("w", encoding="utf-8") as handle:
            for row in conn.execute(
                """
                SELECT id, protocol, args, status, first_seen_at, last_seen_at, seen_count, common_seen_count
                FROM candidates
                ORDER BY last_seen_at DESC, id ASC
                """
            ):
                strategy_count += 1
                handle.write(json.dumps(dict(row), ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")
        with (root / "strategies" / "strategy-domain-links.ndjson").open("w", encoding="utf-8") as handle:
            for scope, table in (("domain", "candidate_domains"), ("common", "candidate_common_domains")):
                for row in conn.execute(f"SELECT candidate_id, domain, protocol, first_seen_at, last_seen_at, seen_count FROM {table} ORDER BY domain, candidate_id"):
                    payload = dict(row)
                    payload["scope"] = scope
                    handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")
        with (root / "strategies" / "strategy-stats.ndjson").open("w", encoding="utf-8") as handle:
            for row in conn.execute(
                """
                SELECT c.id, c.protocol, c.last_seen_at,
                       COUNT(DISTINCT d.domain) AS domain_count,
                       COUNT(DISTINCT cd.domain) AS common_domain_count,
                       c.seen_count, c.common_seen_count
                FROM candidates c
                LEFT JOIN candidate_domains d ON d.candidate_id = c.id
                LEFT JOIN candidate_common_domains cd ON cd.candidate_id = c.id
                GROUP BY c.id
                ORDER BY c.last_seen_at DESC, c.id ASC
                """
            ):
                handle.write(json.dumps(dict(row), ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")
    return strategy_count


def _export_presets(root: Path, state_dir: Path | None = None) -> int:
    sets = domain_sets()
    custom = read_custom_presets(state_dir) if state_dir else {"finder": {}, "common": {}}
    builtin = {
        "presets": [
            {"name": name, "domains": domains}
            for name, domains in sorted(sets.items(), key=lambda item: item[0])
        ]
    }
    sources = {
        "sources": [
            {"name": name, "source": "builtin", "updated_at": now_iso()}
            for name in sorted(sets)
        ]
    }
    _write_text(root / "presets" / "builtin-presets.yaml", _yaml_value(builtin))
    _write_text(root / "presets" / "user-presets.yaml", _yaml_value({"presets": custom}))
    _write_text(root / "presets" / "preset-sources.yaml", _yaml_value(sources))
    return len(sets) + sum(len(items) for items in custom.values())


def _write_checksums(root: Path) -> None:
    rows = []
    for item in sorted(root.rglob("*")):
        if not item.is_file() or item.name == "checksums.sha256":
            continue
        rows.append(f"{_sha256_file(item)}  {item.relative_to(root).as_posix()}")
    _write_text(root / "checksums.sha256", "\n".join(rows) + "\n")


def _prune_snapshots(state_dir: Path) -> None:
    paths = _snapshot_paths(state_dir)
    paths.sort(key=lambda item: item.name, reverse=True)
    for old in paths[SNAPSHOT_KEEP:]:
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
        if path.is_dir() and not path.name.startswith(".tmp-") and (path / "manifest.yaml").is_file():
            result.append(path)
    return result


def _snapshot_path(state_dir: Path, snapshot_id: str) -> Path:
    safe = snapshot_id.replace("/", "").replace("\\", "")
    return snapshots_dir(state_dir) / safe


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


def _read_simple_manifest(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        result[key.strip()] = value.strip().strip('"')
    return result


def _dir_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _yaml_mapping(mapping: dict[str, str]) -> str:
    return "".join(f"{key}: {json.dumps(value, ensure_ascii=False)}\n" for key, value in mapping.items())


def _yaml_value(value: Any, indent: int = 0) -> str:
    prefix = " " * indent
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                lines.append(f"{prefix}{key}:")
                lines.append(_yaml_value(item, indent + 2).rstrip("\n"))
            else:
                lines.append(f"{prefix}{key}: {json.dumps(item, ensure_ascii=False)}")
        return "\n".join(lines) + "\n"
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, dict):
                lines.append(f"{prefix}-")
                lines.append(_yaml_value(item, indent + 2).rstrip("\n"))
            else:
                lines.append(f"{prefix}- {json.dumps(item, ensure_ascii=False)}")
        return "\n".join(lines) + "\n"
    return f"{prefix}{json.dumps(value, ensure_ascii=False)}\n"
