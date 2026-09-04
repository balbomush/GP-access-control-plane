"""api_server job workers + clean-install vault response builders — moved from api_server.py."""

from __future__ import annotations

from typing import Any

from gp_control_plane.bc2_engine import (
    run_multi_domain_discovery,
    run_standard_discovery,
)
from gp_control_plane.bs_engine import run_blockchecks_discovery
from gp_control_plane.config import AppConfig
from gp_control_plane.discovery_engine import is_blockchecks_job
from gp_control_plane.settings import read_run_settings
from gp_control_plane.web.api_server._helpers import (
    _bounded_int,
    _minimum_int,
    _payload_bool,
    _payload_domains,
    _payload_int,
    _payload_timeout_seconds,
)


def _clean_install_vault_public_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep the HTTP contract limited to non-secret vault metadata."""
    return {
        "vault_id": str(payload.get("vault_id") or ""),
        "created_at": str(payload.get("created_at") or ""),
        "schema_version": str(payload.get("schema_version") or ""),
        "archive_sha256": str(payload.get("archive_sha256") or ""),
        "archive_size_bytes": int(payload.get("archive_size_bytes") or 0),
        "verification": str(payload.get("verification") or ""),
        "pending": bool(payload.get("pending")),
    }


def _clean_install_vault_create_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Return only public creation metadata from the protected vault handoff."""
    return {
        "vault_id": str(payload.get("vault_id") or ""),
        "archive_sha256": str(payload.get("archive_sha256") or ""),
        "archive_size_bytes": int(payload.get("archive_size_bytes") or 0),
        "schema_version": str(payload.get("schema_version") or ""),
        "semantic_manifest": payload.get("semantic_manifest") or {},
    }


def _clean_install_vault_restore_response(payload: dict[str, Any], vault_id: str) -> dict[str, Any]:
    """Expose only completion flags from the local restore."""
    verification = payload.get("verification") if isinstance(payload.get("verification"), dict) else {}
    cleanup = payload.get("cleanup") if isinstance(payload.get("cleanup"), dict) else {}
    readiness = payload.get("storage_status") if isinstance(payload.get("storage_status"), dict) else {}
    return {
        "completed": bool(payload.get("completed")),
        "vault_id": str(payload.get("vault_id") or vault_id),
        "verification": {"verified": bool(verification.get("verified"))},
        "storage_status": {"ready": bool(readiness.get("ready"))},
        "cleanup": {"source_deleted": bool(cleanup.get("source_deleted"))},
    }


def _job_discovery(
    config: AppConfig, name: str, payload: dict[str, Any], stop_event: Any, run_id: str = ""
) -> dict[str, Any]:
    if is_blockchecks_job(name):
        kind = "multi-domain-discovery" if "multi-domain" in name else "standard-discovery"
        return _job_blockchecks_discovery(config, payload, stop_event, run_id, kind=kind)
    if name == "zapret-multi-domain-discovery":
        return _job_zapret_multi_domain_discovery(config, payload, stop_event, run_id)
    return _job_zapret_standard_discovery(config, payload, stop_event, run_id)


def _job_blockchecks_discovery(
    config: AppConfig, payload: dict[str, Any], stop_event: Any, run_id: str, *, kind: str
) -> dict[str, Any]:
    domains = _payload_domains(payload)
    settings = read_run_settings(config)
    max_parallelism = _minimum_int(settings.get("curl_parallelism_max"), default=10, minimum=1)
    return run_blockchecks_discovery(
        domains,
        config.output.state_dir,
        timeout_seconds=_payload_timeout_seconds(payload, default=0),
        include_quic=_payload_bool(payload, "include_quic", True),
        enable_http=_payload_bool(payload, "enable_http", False),
        enable_tls12=_payload_bool(payload, "enable_tls12", True),
        enable_tls13=_payload_bool(payload, "enable_tls13", False),
        enable_ipv6=_payload_bool(payload, "enable_ipv6", bool(settings.get("enable_ipv6"))),
        scan_level=str(payload.get("scan_level") or "standard"),
        repeats=_payload_int(payload, "repeats", 1),
        repeat_parallel=_payload_bool(payload, "repeat_parallel", False),
        skip_dnscheck=_payload_bool(payload, "skip_dnscheck", True),
        skip_ipblock=_payload_bool(payload, "skip_ipblock", True),
        curl_max_time=_minimum_int(payload.get("curl_max_time", settings.get("curl_max_time")), default=2, minimum=1),
        curl_parallelism=_bounded_int(
            payload.get("curl_parallelism"),
            default=int(settings.get("curl_parallelism_default") or 4),
            minimum=1,
            maximum=max_parallelism,
        ),
        debug_stdout=_payload_bool(payload, "debug_stdout", bool(settings.get("debug_stdout"))),
        stop_event=stop_event,
        run_id=run_id,
        kind=kind,
        strategy_preset=str(payload.get("strategy_preset") or settings.get("strategy_preset") or ""),
        repeats_mode=str(payload.get("repeats_mode") or settings.get("repeats_mode") or "fast"),
        adaptive=_payload_bool(payload, "bs_adaptive", bool(settings.get("bs_adaptive", True))),
        pair_mode=_payload_bool(payload, "bs_pair_mode", False),
    )


def _job_zapret_standard_discovery(
    config: AppConfig, payload: dict[str, Any], stop_event: Any, run_id: str = ""
) -> dict[str, Any]:
    domains = _payload_domains(payload)
    settings = read_run_settings(config)
    return run_standard_discovery(
        domains,
        config.output.state_dir,
        timeout_seconds=_payload_timeout_seconds(payload, default=0),
        include_quic=_payload_bool(payload, "include_quic", True),
        enable_http=_payload_bool(payload, "enable_http", False),
        enable_tls12=_payload_bool(payload, "enable_tls12", True),
        enable_tls13=_payload_bool(payload, "enable_tls13", False),
        enable_ipv6=_payload_bool(payload, "enable_ipv6", bool(settings.get("enable_ipv6"))),
        scan_level=str(payload.get("scan_level") or "standard"),
        repeats=_payload_int(payload, "repeats", 1),
        repeat_parallel=_payload_bool(payload, "repeat_parallel", False),
        skip_dnscheck=_payload_bool(payload, "skip_dnscheck", True),
        skip_ipblock=_payload_bool(payload, "skip_ipblock", True),
        curl_max_time=_minimum_int(payload.get("curl_max_time", settings.get("curl_max_time")), default=2, minimum=1),
        curl_max_time_quic=_minimum_int(
            payload.get("curl_max_time_quic", settings.get("curl_max_time_quic")), default=2, minimum=1
        ),
        curl_max_time_doh=_minimum_int(
            payload.get("curl_max_time_doh", settings.get("curl_max_time_doh")), default=2, minimum=1
        ),
        debug_stdout=_payload_bool(payload, "debug_stdout", bool(settings.get("debug_stdout"))),
        stop_event=stop_event,
        run_id=run_id,
    )


def _job_zapret_multi_domain_discovery(
    config: AppConfig, payload: dict[str, Any], stop_event: Any, run_id: str = ""
) -> dict[str, Any]:
    domains = _payload_domains(payload)
    settings = read_run_settings(config)
    max_parallelism = _minimum_int(settings.get("curl_parallelism_max"), default=10, minimum=1)
    return run_multi_domain_discovery(
        domains,
        config.output.state_dir,
        timeout_seconds=_payload_timeout_seconds(payload, default=0),
        include_quic=_payload_bool(payload, "include_quic", True),
        enable_http=_payload_bool(payload, "enable_http", False),
        enable_tls12=_payload_bool(payload, "enable_tls12", True),
        enable_tls13=_payload_bool(payload, "enable_tls13", False),
        enable_ipv6=_payload_bool(payload, "enable_ipv6", bool(settings.get("enable_ipv6"))),
        scan_level=str(payload.get("scan_level") or "standard"),
        repeats=_payload_int(payload, "repeats", 1),
        repeat_parallel=_payload_bool(payload, "repeat_parallel", False),
        skip_dnscheck=_payload_bool(payload, "skip_dnscheck", True),
        skip_ipblock=_payload_bool(payload, "skip_ipblock", True),
        curl_max_time=_minimum_int(payload.get("curl_max_time", settings.get("curl_max_time")), default=2, minimum=1),
        curl_max_time_quic=_minimum_int(
            payload.get("curl_max_time_quic", settings.get("curl_max_time_quic")), default=2, minimum=1
        ),
        curl_max_time_doh=_minimum_int(
            payload.get("curl_max_time_doh", settings.get("curl_max_time_doh")), default=2, minimum=1
        ),
        curl_parallelism=_bounded_int(payload.get("curl_parallelism"), default=int(settings.get("curl_parallelism_default") or 4), minimum=1, maximum=max_parallelism),
        debug_stdout=_payload_bool(payload, "debug_stdout", bool(settings.get("debug_stdout"))),
        stop_event=stop_event,
        run_id=run_id,
    )
