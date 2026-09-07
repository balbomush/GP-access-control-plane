"""gp_control_plane.domain_sources._local — moved from storage.py (split)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import URLError

from gp_control_plane.domain_sources._constants import V2FLY_LOCAL_SOURCE
from gp_control_plane.domain_sources._network import _extract_v2fly_data_files, fetch_v2fly_archive
from gp_control_plane.domain_sources._parse import _clean_category, _utc_now, parse_v2fly_revision


def prepare_v2fly_local_storage(
    state_dir: Path,
    *,
    archive_fetcher: Callable[[], bytes] | None = None,
    revision_fetcher: Callable[[], str] | None = None,
) -> dict[str, Any]:
    archive = archive_fetcher() if archive_fetcher else fetch_v2fly_archive()
    files = _extract_v2fly_data_files(archive)
    if not files:
        raise ValueError("v2fly archive does not contain data files")
    group_dir = v2fly_group_cache_dir(state_dir)
    group_parent = group_dir.parent
    group_parent.mkdir(parents=True, exist_ok=True)
    staged_group_dir = Path(tempfile.mkdtemp(prefix=".v2fly-groups-stage-", dir=group_parent))
    categories = sorted(files)
    previous_group_dir: Path | None = None
    previous_manifest: bytes | None = None
    previous_cache: bytes | None = None
    published = False
    try:
        previous_manifest = _read_v2fly_metadata_bytes(v2fly_group_manifest_path(state_dir))
        previous_cache = _read_v2fly_metadata_bytes(v2fly_catalog_cache_path(state_dir))
        for category, content in files.items():
            (staged_group_dir / category).write_text(content, encoding="utf-8")
        revision = ""
        revision_warning = ""
        if revision_fetcher:
            try:
                revision = parse_v2fly_revision(revision_fetcher())
            except Exception as exc:  # revision is optional metadata, not catalog integrity
                revision_warning = _v2fly_error_message(exc)
        manifest = {
            "source": "v2fly/domain-list-community",
            "storage": V2FLY_LOCAL_SOURCE,
            "revision": revision,
            "revision_warning": revision_warning,
            "updated_at": _utc_now(),
            "count": len(categories),
            "categories": categories,
        }
        if group_dir.exists():
            previous_group_dir = group_parent / f".v2fly-groups-previous-{uuid.uuid4().hex}"
            os.replace(group_dir, previous_group_dir)
        try:
            os.replace(staged_group_dir, group_dir)
        except BaseException:
            if previous_group_dir and previous_group_dir.exists():
                os.replace(previous_group_dir, group_dir)
            raise
        published = True
        try:
            write_v2fly_group_manifest(state_dir, manifest)
            write_v2fly_catalog_cache(
                state_dir,
                {
                    "source": V2FLY_LOCAL_SOURCE,
                    "revision": revision,
                    "revision_warning": revision_warning,
                    "checked_at": manifest["updated_at"],
                    "categories": categories,
                },
            )
        except BaseException:
            _restore_v2fly_generation(
                group_dir,
                previous_group_dir,
                v2fly_group_manifest_path(state_dir),
                previous_manifest,
                v2fly_catalog_cache_path(state_dir),
                previous_cache,
            )
            published = False
            raise
        return {
            "source": V2FLY_LOCAL_SOURCE,
            "revision": revision,
            "revision_warning": revision_warning,
            "updated_at": manifest["updated_at"],
            "count": len(categories),
            "categories": categories,
            "group_dir": str(group_dir),
        }
    finally:
        if staged_group_dir.exists():
            shutil.rmtree(staged_group_dir)
        if published and previous_group_dir and previous_group_dir.exists():
            shutil.rmtree(previous_group_dir)


def _read_v2fly_metadata_bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _restore_v2fly_metadata_bytes(path: Path, contents: bytes | None) -> None:
    if contents is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.restore-{uuid.uuid4().hex}")
    try:
        temporary.write_bytes(contents)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _restore_v2fly_generation(
    group_dir: Path,
    previous_group_dir: Path | None,
    manifest_path: Path,
    previous_manifest: bytes | None,
    cache_path: Path,
    previous_cache: bytes | None,
) -> None:
    failed_group_dir: Path | None = None
    if group_dir.exists():
        failed_group_dir = group_dir.parent / f".v2fly-groups-failed-{uuid.uuid4().hex}"
        os.replace(group_dir, failed_group_dir)
    try:
        if previous_group_dir and previous_group_dir.exists():
            os.replace(previous_group_dir, group_dir)
        _restore_v2fly_metadata_bytes(manifest_path, previous_manifest)
        _restore_v2fly_metadata_bytes(cache_path, previous_cache)
    finally:
        if failed_group_dir and failed_group_dir.exists():
            shutil.rmtree(failed_group_dir)


def list_v2fly_categories_local(
    state_dir: Path,
    query: str = "",
    *,
    limit: int = 5000,
) -> dict[str, Any]:
    manifest, error = read_v2fly_group_manifest(state_dir)
    categories = list((manifest or {}).get("categories") or [])
    needle = str(query or "").strip().lower()
    filtered = [category for category in categories if needle in category] if needle else categories
    clean_limit = max(1, min(int(limit or 5000), 5000))
    errors = [error] if error else []
    status = "local" if categories else "missing"
    return {
        "source": V2FLY_LOCAL_SOURCE if categories else "missing",
        "data_status": status,
        "problem_status": "missing" if not categories else "",
        "status": status,
        "status_label": (
            "локальный каталог v2fly готов"
            if categories
            else "локальное хранилище v2fly еще не подготовлено"
        ),
        "query": needle,
        "total": len(filtered),
        "all_count": len(categories),
        "categories": filtered[:clean_limit],
        "has_more": len(filtered) > clean_limit,
        "limit": clean_limit,
        "cached": bool(categories),
        "revision": str((manifest or {}).get("revision") or ""),
        "remote_revision": "",
        "checked_at": str((manifest or {}).get("updated_at") or ""),
        "update_available": False,
        "can_refresh": False,
        "revision_error": _format_v2fly_errors(errors),
        "cache_error": _format_v2fly_errors(errors),
        "error_kind": errors[0]["kind"] if errors else "",
        "error_message": _format_v2fly_errors(errors),
        "errors": errors,
    }


def read_v2fly_group_manifest(state_dir: Path) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    manifest_path = v2fly_group_manifest_path(state_dir)
    if manifest_path.exists():
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except OSError as exc:
            return None, _v2fly_error("local_storage", exc)
        except json.JSONDecodeError as exc:
            return None, _v2fly_error("local_storage", exc)
        if not isinstance(payload, dict):
            return None, _v2fly_error("local_storage", ValueError("invalid v2fly local manifest"))
        categories = _categories_from_manifest(payload)
        if categories:
            return {
                "source": str(payload.get("source") or "v2fly/domain-list-community"),
                "storage": V2FLY_LOCAL_SOURCE,
                "revision": str(payload.get("revision") or ""),
                "updated_at": str(payload.get("updated_at") or ""),
                "count": len(categories),
                "categories": categories,
            }, None
    categories = _categories_from_group_dir(state_dir)
    if categories:
        return {
            "source": "v2fly/domain-list-community",
            "storage": V2FLY_LOCAL_SOURCE,
            "revision": "",
            "updated_at": "",
            "count": len(categories),
            "categories": categories,
        }, None
    return None, _v2fly_error("local_storage", FileNotFoundError("v2fly local storage is not prepared"))


def write_v2fly_group_manifest(state_dir: Path, payload: dict[str, Any]) -> None:
    path = v2fly_group_manifest_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def fetch_v2fly_category_local(state_dir: Path, category: str) -> str:
    clean = _clean_category(category)
    path = v2fly_group_cache_dir(state_dir) / clean
    if not path.exists():
        raise ValueError(f"группа v2fly не найдена в локальном каталоге: {clean}")
    return path.read_text(encoding="utf-8")


def read_v2fly_catalog_cache(state_dir: Path) -> dict[str, Any] | None:
    cache, _ = _read_v2fly_catalog_cache(state_dir)
    return cache


def _read_v2fly_catalog_cache(state_dir: Path) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    path = v2fly_catalog_cache_path(state_dir)
    if not path.exists():
        return None, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return None, _v2fly_error("cache_read", exc)
    except json.JSONDecodeError as exc:
        return None, _v2fly_error("cache_read", exc)
    if not isinstance(payload, dict):
        return None, _v2fly_error("cache_read", ValueError("invalid v2fly catalog cache"))
    categories: list[str] = []
    seen: set[str] = set()
    for raw in payload.get("categories") or []:
        try:
            clean = _clean_category(str(raw))
        except ValueError:
            continue
        if clean not in seen:
            seen.add(clean)
            categories.append(clean)
    if not categories:
        return None, _v2fly_error("cache_read", ValueError("empty v2fly catalog cache"))
    return {
        "source": str(payload.get("source") or "cache"),
        "revision": str(payload.get("revision") or ""),
        "remote_revision": str(payload.get("remote_revision") or ""),
        "checked_at": str(payload.get("checked_at") or ""),
        "update_available": bool(payload.get("update_available")),
        "categories": sorted(categories),
    }, None


def write_v2fly_catalog_cache(state_dir: Path, payload: dict[str, Any]) -> None:
    path = v2fly_catalog_cache_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def _v2fly_error(stage: str, exc: BaseException) -> dict[str, str]:
    return {
        "stage": stage,
        "kind": _v2fly_error_kind(stage, exc),
        "message": _v2fly_error_message(exc),
    }


def _v2fly_error_kind(stage: str, exc: BaseException) -> str:
    if stage.startswith("cache") or stage.startswith("local"):
        return "cache"
    if isinstance(exc, (json.JSONDecodeError, ValueError, tarfile.TarError)):
        return "format"
    if isinstance(exc, (OSError, TimeoutError, URLError, subprocess.SubprocessError)):
        return "network"
    return "unexpected"


def _v2fly_error_message(exc: BaseException) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return " ".join(text.split())


def _v2fly_problem_status(errors: list[dict[str, str]]) -> str:
    if not errors:
        return ""
    kinds = {str(error.get("kind") or "") for error in errors}
    if "network" in kinds:
        return "network"
    if "cache" in kinds:
        return "cache"
    if "format" in kinds:
        return "config"
    return "unexpected"


def _v2fly_status_label(data_status: str, problem_status: str) -> str:
    if problem_status == "network":
        return "сетевой источник недоступен, используется локальный каталог"
    if problem_status == "cache":
        return "проблема локального кэша каталога"
    if problem_status == "config":
        return "источник вернул неожиданный формат данных"
    if problem_status:
        return "ошибка загрузки каталога"
    if data_status == "remote":
        return "каталог загружен из v2fly/domain-list-community"
    return "каталог взят из локального кэша"


def _format_v2fly_errors(errors: list[dict[str, str]]) -> str:
    if not errors:
        return ""
    return "; ".join(f"{error['stage']}: {error['message']}" for error in errors)


def v2fly_catalog_cache_path(state_dir: Path) -> Path:
    return state_dir / "domain-sources" / "v2fly-catalog.json"


def v2fly_group_cache_dir(state_dir: Path) -> Path:
    return state_dir / "domain-sources" / "v2fly-groups"


def v2fly_group_manifest_path(state_dir: Path) -> Path:
    return state_dir / "domain-sources" / "v2fly-groups.json"


def _categories_from_manifest(payload: dict[str, Any]) -> list[str]:
    categories: list[str] = []
    seen: set[str] = set()
    for raw in payload.get("categories") or []:
        try:
            clean = _clean_category(str(raw))
        except ValueError:
            continue
        if clean not in seen:
            seen.add(clean)
            categories.append(clean)
    return sorted(categories)


def _categories_from_group_dir(state_dir: Path) -> list[str]:
    group_dir = v2fly_group_cache_dir(state_dir)
    if not group_dir.exists() or not group_dir.is_dir():
        return []
    categories: list[str] = []
    for path in group_dir.iterdir():
        if not path.is_file():
            continue
        try:
            categories.append(_clean_category(path.name))
        except ValueError:
            continue
    return sorted(set(categories))
