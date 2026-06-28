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
from .storage import connect, db_path, import_candidates_json_if_needed
from .strategy_finder import candidate_id_for


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


def restore_snapshot_if_idle(state_dir: Path, snapshot_id: str) -> dict[str, Any]:
    state = read_state(state_dir)
    if state.get("current_job"):
        return {"restored": False, "queued": True, "reason": "job is running"}
    return restore_snapshot(state_dir, snapshot_id)


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
        if not (extracted / "manifest.yaml").is_file():
            raise ValueError("backup manifest.yaml not found")
        if not _verify_snapshot_path(extracted):
            raise ValueError("backup checksum verification failed")
        final = root / snapshot_id
        if final.exists():
            shutil.rmtree(final)
        extracted.replace(final)
        _write_latest_marker(state_dir, snapshot_id)
        _prune_snapshots(state_dir)
        return {"imported": True, "snapshot": snapshot_info(state_dir, snapshot_id)}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def restore_snapshot(state_dir: Path, snapshot_id: str) -> dict[str, Any]:
    path = _snapshot_path(state_dir, snapshot_id)
    if not path.is_dir():
        raise FileNotFoundError(snapshot_id)
    if not verify_snapshot(state_dir, snapshot_id):
        raise ValueError("backup checksum verification failed")
    strategies = _read_ndjson(path / "strategies" / "strategies.ndjson")
    links = _read_ndjson(path / "strategies" / "strategy-domain-links.ndjson")
    restored_at = now_iso()
    with connect(state_dir) as conn:
        conn.execute("DELETE FROM strategy_attempts")
        conn.execute("DELETE FROM strategy_domain_results")
        conn.execute("DELETE FROM strategies")
        conn.execute("DELETE FROM candidate_seen_events")
        conn.execute("DELETE FROM candidate_common_domains")
        conn.execute("DELETE FROM candidate_domains")
        conn.execute("DELETE FROM candidates")
        for item in _read_ndjson(path / "domains" / "domains.ndjson"):
            domain = str(item.get("domain") or item.get("name") or "").strip()
            if not domain:
                continue
            _restore_domain_id(
                conn,
                domain,
                str(item.get("created_at") or restored_at),
                str(item.get("updated_at") or restored_at),
            )
        for item in strategies:
            candidate_id = str(item.get("id") or "").strip()
            if not candidate_id:
                continue
            conn.execute(
                """
                INSERT INTO candidates(id, protocol, args, status, first_seen_at, last_seen_at, seen_count, common_seen_count)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    str(item.get("protocol") or ""),
                    str(item.get("args") or ""),
                    str(item.get("status") or "candidate"),
                    str(item.get("first_seen_at") or ""),
                    str(item.get("last_seen_at") or ""),
                    0,
                    0,
                ),
            )
            conn.execute(
                """
                INSERT INTO strategies(id, protocol, args, args_hash, status, first_seen_at, last_seen_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    str(item.get("protocol") or ""),
                    str(item.get("args") or ""),
                    _sha256_text(str(item.get("args") or "")),
                    str(item.get("status") or "candidate"),
                    str(item.get("first_seen_at") or ""),
                    str(item.get("last_seen_at") or ""),
                ),
            )
        known_ids = {
            str(row["id"])
            for row in conn.execute("SELECT id FROM candidates").fetchall()
        }
        for item in links:
            candidate_id = str(item.get("strategy_id") or item.get("candidate_id") or "").strip()
            domain = str(item.get("domain") or "").strip()
            if not candidate_id or not domain or candidate_id not in known_ids:
                continue
            table = "candidate_common_domains" if str(item.get("scope") or "") == "common" else "candidate_domains"
            domain_id = _restore_domain_id(conn, domain, str(item.get("first_seen_at") or ""), str(item.get("last_seen_at") or ""))
            source_mode = "multi_domain" if str(item.get("scope") or "") == "common" else "single_domain"
            conn.execute(
                f"""
                INSERT OR REPLACE INTO {table}(candidate_id, domain, protocol, first_seen_at, last_seen_at, seen_count)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    domain,
                    str(item.get("protocol") or ""),
                    str(item.get("first_seen_at") or ""),
                    str(item.get("last_seen_at") or ""),
                    1,
                ),
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO strategy_domain_results(
                    strategy_id, domain_id, protocol, source_mode, first_seen_at, last_seen_at,
                    success_count, fail_count, last_success_run_id, last_fail_run_id
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, 0, '', '')
                """,
                (
                    candidate_id,
                    domain_id,
                    str(item.get("protocol") or ""),
                    source_mode,
                    str(item.get("first_seen_at") or ""),
                    str(item.get("last_seen_at") or ""),
                    1,
                ),
            )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            ("restored_snapshot", snapshot_id),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            ("restored_at", restored_at),
        )
        _mark_legacy_candidates_imported(conn, state_dir)
    info = snapshot_info(state_dir, snapshot_id)
    return {
        "restored": True,
        "snapshot": info,
        "strategy_count": len(strategies),
        "restored_at": restored_at,
    }


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
    return _verify_snapshot_path(path)


def _verify_snapshot_path(path: Path) -> bool:
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
    if not str(target).startswith(str(root.resolve())):
        raise ValueError("invalid zip path")
    return target


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
    (root / "domains").mkdir()
    (root / "strategies").mkdir()
    domain_count = _export_domains(state_dir, root)
    strategy_count, link_count = _export_strategies(state_dir, root)
    manifest = {
        "schema_version": "2",
        "created_at": now_iso(),
        "snapshot_id": snapshot_id,
        "app_version": __version__,
        "storage": "sqlite",
        "db_path": str(db_path(state_dir)),
        "domain_count": str(domain_count),
        "strategy_count": str(strategy_count),
        "link_count": str(link_count),
        "preset_count": "0",
        "completed": "true",
    }
    _write_text(root / "manifest.yaml", _yaml_mapping(manifest))
    _write_checksums(root)


def _export_domains(state_dir: Path, root: Path) -> int:
    import_candidates_json_if_needed(state_dir, candidate_id_for)
    count = 0
    with connect(state_dir) as conn:
        with (root / "domains" / "domains.ndjson").open("w", encoding="utf-8") as handle:
            for row in conn.execute(
                """
                SELECT d.name AS domain, d.service_group, d.created_at, d.updated_at
                FROM domains d
                WHERE EXISTS (
                    SELECT 1 FROM strategy_domain_results r WHERE r.domain_id = d.id
                )
                ORDER BY d.name ASC
                """
            ):
                count += 1
                handle.write(json.dumps(dict(row), ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")
    return count


def _export_strategies(state_dir: Path, root: Path) -> tuple[int, int]:
    import_candidates_json_if_needed(state_dir, candidate_id_for)
    strategy_count = 0
    link_count = 0
    with connect(state_dir) as conn:
        with (root / "strategies" / "strategies.ndjson").open("w", encoding="utf-8") as handle:
            for row in conn.execute(
                """
                SELECT s.id, s.protocol, s.args, s.status, s.first_seen_at, s.last_seen_at
                FROM strategies s
                ORDER BY s.last_seen_at DESC, s.id ASC
                """
            ):
                strategy_count += 1
                handle.write(json.dumps(dict(row), ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")
        with (root / "strategies" / "strategy-domain-links.ndjson").open("w", encoding="utf-8") as handle:
            for row in conn.execute(
                """
                SELECT r.strategy_id AS strategy_id, d.name AS domain, r.protocol, r.first_seen_at,
                       r.last_seen_at, r.source_mode
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


def _read_user_presets(path: Path) -> dict[str, dict[str, list[str]]]:
    result: dict[str, dict[str, list[str]]] = {"finder": {}, "common": {}}
    if not path.is_file():
        return result
    current_scope = ""
    current_name = ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        if indent == 2 and line.endswith(":"):
            scope = line[:-1].strip()
            current_scope = scope if scope in result else ""
            current_name = ""
            continue
        if indent == 4 and line.endswith(":") and current_scope:
            current_name = line[:-1].strip()
            if current_name:
                result[current_scope][current_name] = []
            continue
        if indent >= 6 and line.startswith("- ") and current_scope and current_name:
            raw_value = line[2:].strip()
            try:
                value = json.loads(raw_value)
            except json.JSONDecodeError:
                value = raw_value.strip('"')
            domain = str(value or "").strip()
            if domain and domain not in result[current_scope][current_name]:
                result[current_scope][current_name].append(domain)
    return result


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _restore_domain_id(conn: Any, domain: str, created_at: str, updated_at: str) -> int:
    conn.execute(
        """
        INSERT INTO domains(name, service_group, created_at, updated_at)
        VALUES(?, '', ?, ?)
        ON CONFLICT(name) DO UPDATE SET updated_at = excluded.updated_at
        """,
        (domain, created_at, updated_at or created_at),
    )
    row = conn.execute("SELECT id FROM domains WHERE name = ?", (domain,)).fetchone()
    return int(row["id"])


def _restore_domain_preset(conn: Any, scope: str, name: str, domains: list[str], updated_at: str) -> None:
    conn.execute(
        """
        INSERT INTO domain_presets(scope, name, kind, label, created_at, updated_at)
        VALUES(?, ?, 'user', ?, ?, ?)
        ON CONFLICT(scope, name, kind) DO UPDATE SET label = excluded.label, updated_at = excluded.updated_at
        """,
        (scope, name, name, updated_at, updated_at),
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
        domain_id = _restore_domain_id(conn, domain, updated_at, updated_at)
        conn.execute(
            "INSERT OR REPLACE INTO preset_domains(preset_id, domain_id, position) VALUES(?, ?, ?)",
            (preset_id, domain_id, position),
        )


def _unique_nonempty(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if item and item not in result:
            result.append(item)
    return result


def _mark_legacy_candidates_imported(conn: Any, state_dir: Path) -> None:
    path = state_dir / "strategy-finder" / "candidates.json"
    if not path.exists():
        return
    stat = path.stat()
    marker = f"{stat.st_size}:{stat.st_mtime_ns}"
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
        ("imported_candidates_json", marker),
    )


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
