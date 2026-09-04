"""gp_control_plane.backups._info — moved from storage.py (split)."""
from __future__ import annotations

from gp_control_plane.storage import connect, db_path, storage_runtime_status, storage_status
from pathlib import Path
from typing import Any
import json
import re
from gp_control_plane.backups._constants import SNAPSHOT_KEEP
from gp_control_plane.backups._io import _dir_size, _sha256_file
from gp_control_plane.backups._manifest import _int_value, _is_supported_snapshot_manifest, _read_manifest, _safe_extract_target
from gp_control_plane.backups._paths import _snapshot_files, _snapshot_path, _snapshot_paths


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
