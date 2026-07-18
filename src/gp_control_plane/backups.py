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
from .strategy_safety import analyze_strategy
from .storage import connect, db_path


SNAPSHOT_KEEP = 5
BACKUP_SCHEMA_VERSION = "5"
SUPPORTED_BACKUP_SCHEMA_VERSIONS = {BACKUP_SCHEMA_VERSION}
SNAPSHOT_DOWNLOAD_FILES = {
    "manifest.json",
    "checksums.sha256",
    "domains/domains.ndjson",
    "strategies/strategies.ndjson",
    "strategies/strategy-domain-links.ndjson",
    "presets/domain-presets.ndjson",
    "presets/preset-domains.ndjson",
}


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
    state = read_state(state_dir)
    if state.get("current_job"):
        return {"restored": False, "queued": True, "reason": "job is running"}
    return restore_snapshot(state_dir, snapshot_id)


def delete_snapshot_if_idle(state_dir: Path, snapshot_id: str) -> dict[str, Any]:
    state = read_state(state_dir)
    if state.get("current_job"):
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
    replaces_presets = _snapshot_replaces_presets(path, manifest)
    with connect(state_dir) as conn:
        current_domain_count = _linked_domain_count(conn)
        current_strategy_count = _table_count(conn, "strategies")
        current_link_count = _table_count(conn, "strategy_domain_results")
        current_preset_count = int(
            conn.execute("SELECT COUNT(*) AS count FROM domain_presets WHERE kind = 'user'").fetchone()["count"]
        )
        current_preset_link_count = _table_count(conn, "preset_domains")
    settings = read_state(state_dir).get("settings")
    current_settings_count = 1 if isinstance(settings, dict) and settings else 0
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
                "backup_count": 0,
                "will_replace": False,
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
    pre_restore = create_snapshot(state_dir, protect_ids={snapshot_id})
    strategies = restore_plan["strategies"]
    links = restore_plan["links"]
    domains = restore_plan["domains"]
    restore_presets = bool(restore_plan["restore_presets"])
    presets = restore_plan["presets"]
    preset_links = restore_plan["preset_links"]
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
            _restore_domain_id(conn, domain)
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
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            ("restored_snapshot", snapshot_id),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            ("restored_at", restored_at),
        )
    info = snapshot_info(state_dir, snapshot_id)
    return {
        "restored": True,
        "snapshot": info,
        "pre_restore_snapshot": pre_restore.get("snapshot"),
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
    domain_count = _export_domains(state_dir, root)
    strategy_count, link_count = _export_strategies(state_dir, root)
    preset_count, preset_link_count = _export_domain_presets(state_dir, root)
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
        "completed": "true",
    }
    _write_json(root / "manifest.json", manifest)
    _write_checksums(root)


def _export_domains(state_dir: Path, root: Path) -> int:
    count = 0
    with connect(state_dir) as conn:
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


def _export_strategies(state_dir: Path, root: Path) -> tuple[int, int]:
    strategy_count = 0
    link_count = 0
    with connect(state_dir) as conn:
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


def _export_domain_presets(state_dir: Path, root: Path) -> tuple[int, int]:
    preset_count = 0
    link_count = 0
    with connect(state_dir) as conn:
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
    for item in presets:
        if not str(item.get("scope") or "").strip() or not str(item.get("name") or "").strip():
            raise ValueError("backup contains preset row without scope/name")
    for item in preset_links:
        if not str(item.get("scope") or "").strip() or not str(item.get("name") or "").strip():
            raise ValueError("backup contains preset-domain link without scope/name")
        if not str(item.get("domain") or "").strip():
            raise ValueError("backup contains preset-domain link without domain")
    return {
        "manifest": manifest,
        "domains": domains,
        "strategies": strategies,
        "links": links,
        "restore_presets": restore_presets,
        "presets": presets,
        "preset_links": preset_links,
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


def _restore_domain_id(conn: Any, domain: str) -> int:
    conn.execute(
        """
        INSERT INTO domains(name, service_group)
        VALUES(?, '')
        ON CONFLICT(name) DO NOTHING
        """,
        (domain,),
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


def _snapshot_replaces_presets(path: Path, manifest: dict[str, str]) -> bool:
    if str(manifest.get("schema_version") or "") != BACKUP_SCHEMA_VERSION:
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
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, payload: dict[str, str]) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
