from __future__ import annotations

import json
from typing import Any

from .backups import (
    clean_install_vault_info,
    create_clean_install_vault,
    list_snapshots,
    restore_clean_install_vault,
    validate_clean_install_vault_id,
)
from .config import AppConfig
from .domain_sources import fetch_v2fly_category_local, list_v2fly_categories_local, parse_v2fly_domains
from .state import now_iso, read_state
from .storage import (
    delete_user_presets,
    read_custom_preset_index,
    read_custom_presets,
    read_system_preset_index,
    read_system_presets,
    save_custom_preset,
    save_system_preset,
    storage_runtime_status,
)
from .strategy_finder import iter_strategy_candidates_filtered, latest_log_tail, read_runs, read_runs_page, read_strategy_candidates_filtered
from .v2fly_payloads import v2fly_storage_status_payload
from .discovery_engine import discovery_job_name, normalize_engine
from .zapret2 import check_install_cached


STRATEGY_DISCOVERY_START_RUN_KEYS = {
    "mode",
    "domains",
    "protocols",
    "curl_parallelism",
    "timeout_seconds",
    "settings",
}

STRATEGY_DISCOVERY_START_RUN_SETTINGS_KEYS = {
    "curl_max_time",
    "curl_max_time_quic",
    "curl_max_time_doh",
    "debug_stdout",
    "enable_http",
    "enable_ipv6",
    "enable_tls12",
    "enable_tls13",
    "include_quic",
    "repeats",
    "repeat_parallel",
    "scan_level",
    "skip_dnscheck",
    "skip_ipblock",
    "discovery_engine",
    "strategy_preset",
    "repeats_mode",
    "bs_adaptive",
    "bs_pair_mode",
}


def status_payload(config: AppConfig) -> dict[str, Any]:
    state = read_state(config.output.state_dir)
    storage = storage_runtime_status(config.output.state_dir)
    current_run_id = str(state.get("current_run_id") or "")
    current_status = str(state.get("current_run_status") or "")
    status = "running" if current_run_id else "idle"
    if current_status == "stopping":
        status = "stopping"
    if state.get("last_error"):
        status = "error"
    payload: dict[str, Any] = {
        "state": status,
        "storage": {
            "ready": bool(storage.get("ready")),
            "schema_version": int(storage.get("schema_version") or 0),
            "state_dir": str(config.output.state_dir),
        },
        "updated_at": now_iso(),
    }
    if current_run_id:
        payload["current_run"] = {"run_id": current_run_id, "status": current_status or "running"}
    if isinstance(state.get("last_snapshot"), dict):
        payload["last_snapshot"] = state["last_snapshot"]
    return payload


def current_progress_payload(config: AppConfig) -> dict[str, Any]:
    state = read_state(config.output.state_dir)
    current_run_id = str(state.get("current_run_id") or "")
    log = latest_log_tail(config.output.state_dir, max_lines=20, run_id=current_run_id)
    progress = log.get("progress") if isinstance(log.get("progress"), dict) else {}
    status = str(state.get("current_run_status") or "")
    if not current_run_id:
        status = "idle"
    result: dict[str, Any] = {
        "run_id": current_run_id,
        "status": status or "idle",
        "stage": str(progress.get("phase") or progress.get("stage") or ""),
        "current_file": str(progress.get("current_file") or progress.get("script") or ""),
    }
    for target, *sources in (
        ("domains_total", "domains_total", "total_domains"),
        ("domains_processed", "domains_processed", "processed_domains"),
        ("attempts_total", "attempts_total", "total_attempts"),
        ("attempts_processed", "attempts_processed", "processed_attempts"),
        ("strategies_total", "strategies_total", "total_strategies"),
        ("strategies_processed", "strategies_processed", "processed_strategies"),
        ("elapsed_seconds", "elapsed_seconds"),
        ("eta_seconds", "eta_seconds"),
        ("avg_attempt_seconds", "avg_attempt_seconds"),
    ):
        for source in sources:
            value = progress.get(source)
            if value is not None:
                result[target] = value
                break
    return result


def preflight_payload(config: AppConfig) -> dict[str, Any]:
    zapret = check_install_cached()
    checks = []
    diagnostics = zapret.get("diagnostics") if isinstance(zapret.get("diagnostics"), list) else []
    for item in diagnostics:
        if not isinstance(item, dict):
            continue
        check = {
            "name": str(item.get("id") or item.get("label") or "check"),
            "status": "ok" if item.get("ok") else "error",
            "message": str(item.get("message") or ""),
        }
        if isinstance(item.get("details"), dict):
            check["details"] = item["details"]
        checks.append(check)
    if not checks:
        ready = bool(zapret.get("ready") or zapret.get("ok"))
        checks.append({"name": "zapret2", "status": "ok" if ready else "error", "message": str(zapret.get("message") or "")})
    return {"ready": all(item["status"] == "ok" for item in checks), "checks": checks}


def run_settings_payload(settings: dict[str, Any]) -> dict[str, Any]:
    return {
        "curl_parallelism_default": settings.get("curl_parallelism_default"),
        "curl_parallelism_max": settings.get("curl_parallelism_max"),
        "curl_max_time": settings.get("curl_max_time"),
        "curl_max_time_quic": settings.get("curl_max_time_quic"),
        "curl_max_time_doh": settings.get("curl_max_time_doh"),
        "enable_ipv6": settings.get("enable_ipv6"),
        "debug_stdout": settings.get("debug_stdout"),
        "discovery_engine": settings.get("discovery_engine"),
    }


def domain_lists_payload(config: AppConfig) -> dict[str, Any]:
    state_dir = config.output.state_dir
    system = read_system_presets(state_dir).get("finder", {})
    system_meta = read_system_preset_index(state_dir).get("finder", {})
    custom = read_custom_presets(state_dir).get("finder", {})
    custom_meta = read_custom_preset_index(state_dir).get("finder", {})
    lists = [
        _domain_list_payload("required", "required", "Обязательные", system.get("required") or [], system_meta.get("required") or {}),
        _domain_list_payload("desired", "desired", "Желательные", system.get("desired") or [], system_meta.get("desired") or {}),
    ]
    for name, domains in custom.items():
        lists.append(_domain_list_payload(f"user:{name}", "user", name, domains, custom_meta.get(name) or {}))
    return {"lists": lists}


def save_domain_list_payload(config: AppConfig, payload: dict[str, Any]) -> dict[str, Any]:
    kind = str(payload.get("kind") or "").strip()
    name = str(payload.get("name") or kind).strip()
    domains = payload_string_list(payload, "domains")
    updated_at = now_iso()
    if kind in {"required", "desired"}:
        save_system_preset(config.output.state_dir, scope="finder", name=kind, domains=domains, updated_at=updated_at)
        meta = read_system_preset_index(config.output.state_dir).get("finder", {}).get(kind) or {}
        return _domain_list_payload(kind, kind, name or kind, domains, meta)
    if kind == "user":
        save_custom_preset(config.output.state_dir, scope="finder", name=name, domains=domains, updated_at=updated_at)
        meta = read_custom_preset_index(config.output.state_dir).get("finder", {}).get(name) or {}
        return _domain_list_payload(f"user:{name}", "user", name, domains, meta)
    raise ValueError("kind must be required, desired or user")


def delete_user_domain_list_payload(config: AppConfig, payload: dict[str, Any]) -> dict[str, int]:
    list_ids = payload_string_list(payload, "list_ids")
    if not list_ids:
        raise ValueError("list_ids must contain at least one user list id")
    names: list[str] = []
    for list_id in list_ids:
        if not list_id.startswith("user:"):
            raise ValueError("list_ids must contain only user list ids")
        name = list_id.split(":", 1)[1].strip()
        if not name:
            raise ValueError("list_ids must contain only user list ids")
        names.append(name)
    unique_names = sorted(set(names))
    existing_names = set((read_custom_preset_index(config.output.state_dir).get("finder") or {}).keys())
    delete_user_presets(config.output.state_dir, scope="finder", names=unique_names)
    return {"deleted": len([name for name in unique_names if name in existing_names])}


def v2fly_categories_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    raw = list_v2fly_categories_local(
        config.output.state_dir,
        query=query_str(query, "query", ""),
        limit=query_int(query, "limit", 2000),
    )
    return {
        "categories": [{"name": str(category)} for category in raw.get("categories") or []],
        "storage": v2fly_storage_status_payload(config, raw),
    }


def v2fly_category_domains_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    category = query_one(query, "category")
    if not category:
        raise ValueError("category is required")
    text = fetch_v2fly_category_local(config.output.state_dir, category)
    return {
        "category": category,
        "domains": parse_v2fly_domains(text),
        "storage": v2fly_storage_status_payload(config),
    }


def strategy_candidates_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    filters = strategy_candidate_filters_from_query(query, require_filter=True)
    return read_strategy_candidates_filtered(config.output.state_dir, **filters)


def iter_strategy_candidates_export_lines(config: AppConfig, query: dict[str, list[str]]) -> Any:
    filters = strategy_candidate_filters_from_query(query, require_filter=False)
    return _iter_strategy_candidates_export_lines(config, filters)


def _iter_strategy_candidates_export_lines(config: AppConfig, filters: dict[str, Any]) -> Any:
    for candidate in iter_strategy_candidates_filtered(config.output.state_dir, **filters):
        yield json.dumps(candidate, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"


def backups_list_payload(config: AppConfig) -> dict[str, Any]:
    return {"backups": [backup_snapshot_payload(item) for item in list_snapshots(config.output.state_dir).get("snapshots") or []]}


def clean_install_vault_create_payload(config: AppConfig, payload: dict[str, Any]) -> dict[str, Any]:
    if payload:
        raise ValueError("clean-install vault create does not accept request fields")
    created = create_clean_install_vault(config.output.state_dir)
    return {
        "vault_id": str(created.get("vault_id") or ""),
        "archive_sha256": str(created.get("archive_sha256") or ""),
        "archive_size_bytes": int(created.get("archive_size_bytes") or 0),
        "schema_version": str(created.get("schema_version") or ""),
        "semantic_manifest": created.get("semantic_manifest") or {},
    }


def clean_install_vault_list_payload(config: AppConfig) -> dict[str, Any]:
    del config
    info = clean_install_vault_info()
    if not info.get("exists") or not info.get("pending") or not str(info.get("vault_id") or ""):
        return {"vaults": []}
    return {"vaults": [clean_install_vault_public_payload(info)]}


def clean_install_vault_status_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    del config
    values = query.get("vault_id") or []
    if not values or values[0] == "":
        raise ValueError("vault_id is required")
    if len(values) != 1:
        raise ValueError("invalid clean-install vault id")
    vault_id = validate_clean_install_vault_id(values[0])
    info = clean_install_vault_info()
    if not info.get("exists") or not info.get("pending") or str(info.get("vault_id") or "") != vault_id:
        raise FileNotFoundError(vault_id)
    return clean_install_vault_public_payload(info)


def clean_install_vault_restore_payload(config: AppConfig, payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {"vault_id", "confirm_restore"}
    unknown = sorted(str(key) for key in payload if str(key) not in allowed)
    if unknown:
        raise ValueError(f"unsupported clean-install vault restore fields: {', '.join(unknown)}")
    raw_vault_id = payload.get("vault_id")
    if raw_vault_id is None or raw_vault_id == "":
        raise ValueError("vault_id is required")
    vault_id = validate_clean_install_vault_id(raw_vault_id)
    if payload.get("confirm_restore") is not True:
        raise ValueError("confirm_restore=true is required")
    restored = restore_clean_install_vault(
        config.output.state_dir,
        vault_id=vault_id,
    )
    verification = restored.get("verification") if isinstance(restored.get("verification"), dict) else {}
    cleanup = restored.get("cleanup") if isinstance(restored.get("cleanup"), dict) else {}
    readiness = restored.get("storage_status") if isinstance(restored.get("storage_status"), dict) else {}
    if not bool(verification.get("verified")) or not bool(readiness.get("ready")) or not bool(cleanup.get("source_deleted")):
        raise RuntimeError("clean-install vault restore did not complete verified source cleanup")
    return {
        "completed": bool(restored.get("completed")),
        "vault_id": str(restored.get("vault_id") or vault_id),
        "verification": verification,
        "storage_status": readiness,
        "cleanup": cleanup,
    }


def clean_install_vault_public_payload(info: dict[str, Any]) -> dict[str, Any]:
    """Return vault metadata without confirmation credentials."""
    return {
        "vault_id": str(info.get("vault_id") or ""),
        "created_at": str(info.get("created_at") or ""),
        "schema_version": str(info.get("schema_version") or ""),
        "archive_sha256": str(info.get("archive_sha256") or ""),
        "archive_size_bytes": int(info.get("archive_size_bytes") or 0),
        "verification": str(info.get("verification") or ""),
        "pending": bool(info.get("pending")),
    }


def backup_snapshot_payload(item: dict[str, Any]) -> dict[str, Any]:
    snapshot_id = str(item.get("snapshot_id") or item.get("id") or "")
    return {
        "snapshot_id": snapshot_id,
        "created_at": str(item.get("created_at") or ""),
        "schema_version": str(item.get("schema_version") or ""),
        "filename": str(item.get("filename") or f"{snapshot_id}.zip"),
        "size_bytes": int(item.get("size_bytes") or 0),
        "checksum": "ok" if item.get("checksum_ok") else "",
        "entity_counts": {
            "strategies": int(item.get("strategy_count") or 0),
            "domain_lists": int(item.get("preset_count") or 0),
        },
    }


def runs_history_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    return {
        "runs": [
            run_history_item_payload(run)
            for run in read_runs(
                config.output.state_dir,
                limit=query_int(query, "limit", 1000),
                offset=query_int(query, "offset", 0),
            )
        ]
    }
def runs_history_page_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    page = read_runs_page(
        config.output.state_dir,
        limit=query_int(query, "limit", 50),
        offset=query_int(query, "offset", 0),
    )
    return page | {"runs": [run_history_item_payload(run) for run in page["runs"]]}


def run_history_item_payload(run: dict[str, Any]) -> dict[str, Any]:
    return {"run_id": str(run.get("id") or "")} | {key: value for key, value in run.items() if key != "id"}




def run_accepted_payload(run: Any) -> dict[str, Any]:
    return {
        "accepted": True,
        "run_id": str(getattr(run, "run_id", "")),
        "status": str(getattr(run, "status", "")),
    }


def action_accepted_payload(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "accepted": True,
        "run_id": str(run.get("run_id") or ""),
        "status": str(run.get("status") or ""),
    }

def strategy_discovery_job_payload(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    unknown_keys = sorted(str(key) for key in payload if str(key) not in STRATEGY_DISCOVERY_START_RUN_KEYS)
    if unknown_keys:
        raise ValueError(f"unsupported start-run fields: {', '.join(unknown_keys)}")

    domains = payload_domains(payload)
    if not domains:
        raise ValueError("domains are required")

    mode = str(payload.get("mode") or "standard").strip().lower().replace("-", "_")
    if mode not in {"standard", "multi_domain", "common_strategy"}:
        raise ValueError("unsupported strategy discovery mode")

    settings = payload.get("settings") if isinstance(payload.get("settings"), dict) else {}
    unknown_settings = sorted(str(key) for key in settings if str(key) not in STRATEGY_DISCOVERY_START_RUN_SETTINGS_KEYS)
    if unknown_settings:
        raise ValueError(f"unsupported start-run settings: {', '.join(unknown_settings)}")

    job_payload = {str(key): value for key, value in settings.items()}
    for key, value in payload.items():
        if key not in {"mode", "settings", "protocols"}:
            job_payload[str(key)] = value
    job_payload["domains"] = domains

    protocols = {item.lower() for item in payload_string_list(payload, "protocols")}
    unknown_protocols = protocols - {"tcp", "quic"}
    if unknown_protocols:
        raise ValueError(f"unsupported protocols: {', '.join(sorted(unknown_protocols))}")
    if protocols:
        job_payload["include_quic"] = "quic" in protocols
        if "tcp" not in protocols:
            job_payload["enable_http"] = False
            job_payload["enable_tls12"] = False
            job_payload["enable_tls13"] = False
        else:
            job_payload.setdefault("enable_http", False)
            job_payload.setdefault("enable_tls12", True)
            job_payload.setdefault("enable_tls13", False)

    engine = normalize_engine(job_payload.get("discovery_engine"))
    job_payload["discovery_engine"] = engine
    return discovery_job_name(engine, mode), job_payload


def payload_snapshot_id(payload: dict[str, Any]) -> str:
    snapshot_id = str(payload.get("snapshot_id") or payload.get("snapshot") or "").strip()
    if not snapshot_id:
        raise ValueError("snapshot_id is required")
    return snapshot_id


def _domain_list_payload(list_id: str, kind: str, name: str, domains: list[str], meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "list_id": list_id,
        "kind": kind,
        "name": name,
        "domains": list(domains or []),
        "updated_at": str(meta.get("updated_at") or ""),
    }


def query_str(query: dict[str, list[str]], key: str, default: str) -> str:
    values = query.get(key) or []
    return values[0] if values else default


def query_int(query: dict[str, list[str]], key: str, default: int) -> int:
    raw = query_str(query, key, str(default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def query_domains(query: dict[str, list[str]], key: str) -> list[str]:
    values = query.get(key) or []
    domains: list[str] = []
    for value in values:
        domains.extend(item.strip() for item in value.split(",") if item.strip())
    return domains


def query_one(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key) or []
    return str(values[0]).strip() if values else ""


def strategy_candidate_filters_from_query(query: dict[str, list[str]], *, require_filter: bool) -> dict[str, Any]:
    filters = {
        "domains": [*query_domains(query, "domain"), *query_domains(query, "domains")],
        "strategy_ids": [*query_list(query, "strategy_id"), *query_list(query, "strategy_ids")],
        "protocols": [*query_list(query, "protocol"), *query_list(query, "protocols")],
        "source_modes": [*query_list(query, "source_mode"), *query_list(query, "source_modes")],
        "families": [*query_list(query, "family"), *query_list(query, "families")],
        "query": query_str(query, "query", "").strip(),
    }
    has_filter = any(value for value in filters.values())
    if require_filter and not has_filter:
        raise ValueError("strategy candidate filter is required; use /api/core/strategy-candidates/export for full stream")
    return filters


def query_list(query: dict[str, list[str]], key: str) -> list[str]:
    values = query.get(key) or []
    result: list[str] = []
    for value in values:
        result.extend(item.strip() for item in str(value).replace(",", " ").split() if item.strip())
    return result


def payload_string_list(payload: dict[str, Any], key: str) -> list[str]:
    raw = payload.get(key) or []
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def payload_domains(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("domains") or []
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    if not isinstance(raw, list):
        raw = []
    return [str(domain).strip() for domain in raw if str(domain).strip()]
