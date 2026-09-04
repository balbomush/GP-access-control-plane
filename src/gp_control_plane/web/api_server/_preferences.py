"""api_server run/discovery preference state — moved from api_server.py (package split)."""

from __future__ import annotations

from typing import Any

from gp_control_plane.config import AppConfig
from gp_control_plane.state import read_state, update_state
from gp_control_plane.web.api_server._helpers import (
    _bounded_int,
    _clean_domain_list,
    _minimum_int,
    _payload_bool,
)

DEFAULT_RUN_PREFERENCES = {
    "domains": [],
    "domain_preset": "system:required",
    "discovery_profile": "standard",
    "run_mode": "standard",
    "curl_parallelism": 4,
    "enable_http": False,
    "enable_tls12": True,
    "enable_tls13": False,
    "include_quic": True,
    "enable_ipv6": False,
    "scan_level": "standard",
    "repeats": 1,
    "repeat_parallel": False,
    "skip_dnscheck": True,
    "skip_ipblock": True,
    "limit_time_enabled": False,
    "timeout_hours": 6,
}


DEFAULT_DISCOVERY_PROFILES = {
    "quick": {
        "name": "quick",
        "title": "Быстрый",
        "enable_http": False,
        "enable_tls12": True,
        "enable_tls13": False,
        "include_quic": True,
        "enable_ipv6": False,
        "scan_level": "quick",
        "repeats": 1,
        "repeat_parallel": False,
        "skip_dnscheck": True,
        "skip_ipblock": True,
        "curl_parallelism": 4,
        "limit_time_enabled": False,
        "timeout_hours": 6,
    },
    "standard": {
        "name": "standard",
        "title": "Стандартный",
        "enable_http": False,
        "enable_tls12": True,
        "enable_tls13": False,
        "include_quic": True,
        "enable_ipv6": False,
        "scan_level": "standard",
        "repeats": 1,
        "repeat_parallel": False,
        "skip_dnscheck": True,
        "skip_ipblock": True,
        "curl_parallelism": 4,
        "limit_time_enabled": False,
        "timeout_hours": 6,
    },
    "force": {
        "name": "force",
        "title": "Глубокий",
        "enable_http": True,
        "enable_tls12": True,
        "enable_tls13": True,
        "include_quic": True,
        "enable_ipv6": False,
        "scan_level": "force",
        "repeats": 1,
        "repeat_parallel": False,
        "skip_dnscheck": False,
        "skip_ipblock": False,
        "curl_parallelism": 4,
        "limit_time_enabled": False,
        "timeout_hours": 6,
    },
}


def read_run_preferences(config: AppConfig) -> dict[str, Any]:
    state = read_state(config.output.state_dir)
    stored = state.get("run_preferences") if isinstance(state.get("run_preferences"), dict) else {}
    return _normalize_run_preferences({**DEFAULT_RUN_PREFERENCES, **stored})


def save_run_preferences(config: AppConfig, payload: dict[str, Any]) -> dict[str, Any]:
    preferences = _normalize_run_preferences(
        {**read_run_preferences(config), **(payload if isinstance(payload, dict) else {})}
    )
    update_state(config.output.state_dir, lambda state: state | {"run_preferences": preferences})
    return preferences


def _normalize_run_preferences(raw: dict[str, Any]) -> dict[str, Any]:
    run_mode = str(raw.get("run_mode") or "standard")
    if run_mode not in {"standard", "multi"}:
        run_mode = "standard"
    scan_level = str(raw.get("scan_level") or "standard")
    if scan_level not in {"quick", "standard", "force"}:
        scan_level = "standard"
    discovery_profile = str(raw.get("discovery_profile") or scan_level)
    if discovery_profile not in {"quick", "standard", "force", "custom"}:
        discovery_profile = scan_level if scan_level in {"quick", "standard", "force"} else "custom"
    timeout_hours_raw = raw.get("timeout_hours")
    try:
        timeout_hours = float(timeout_hours_raw)
    except (TypeError, ValueError):
        timeout_hours = 6.0
    timeout_hours = max(0.1, min(24.0, timeout_hours))
    return {
        "domains": _clean_domain_list(raw.get("domains") or []),
        "domain_preset": str(raw.get("domain_preset") or "system:required")[:160],
        "discovery_profile": discovery_profile,
        "run_mode": run_mode,
        "curl_parallelism": _minimum_int(raw.get("curl_parallelism"), default=4, minimum=1),
        "enable_http": bool(raw.get("enable_http")),
        "enable_tls12": bool(raw.get("enable_tls12", True)),
        "enable_tls13": bool(raw.get("enable_tls13")),
        "include_quic": bool(raw.get("include_quic", True)),
        "enable_ipv6": bool(raw.get("enable_ipv6")),
        "scan_level": scan_level,
        "repeats": _bounded_int(raw.get("repeats"), default=1, minimum=1, maximum=10),
        "repeat_parallel": bool(raw.get("repeat_parallel")),
        "skip_dnscheck": bool(raw.get("skip_dnscheck", True)),
        "skip_ipblock": bool(raw.get("skip_ipblock", True)),
        "limit_time_enabled": bool(raw.get("limit_time_enabled")),
        "timeout_hours": timeout_hours,
    }


def read_discovery_profiles(config: AppConfig) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for name, profile in DEFAULT_DISCOVERY_PROFILES.items():
        merged[name] = _normalize_discovery_profile(name, profile)
    return dict(sorted(merged.items()))


def save_discovery_profiles(config: AppConfig, payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    update_state(config.output.state_dir, lambda state: state | {"discovery_profiles": {}})
    return read_discovery_profiles(config)


def _normalize_discovery_profile(name: str, raw: dict[str, Any]) -> dict[str, Any]:
    scan_level = str(raw.get("scan_level") or "standard")
    if scan_level not in {"quick", "standard", "force"}:
        scan_level = "standard"
    return {
        "name": name,
        "title": str(raw.get("title") or name),
        "enable_http": _payload_bool(raw, "enable_http", False),
        "enable_tls12": _payload_bool(raw, "enable_tls12", True),
        "enable_tls13": _payload_bool(raw, "enable_tls13", False),
        "include_quic": _payload_bool(raw, "include_quic", True),
        "enable_ipv6": _payload_bool(raw, "enable_ipv6", False),
        "scan_level": scan_level,
        "repeats": _bounded_int(raw.get("repeats"), default=1, minimum=1, maximum=10),
        "repeat_parallel": _payload_bool(raw, "repeat_parallel", False),
        "skip_dnscheck": _payload_bool(raw, "skip_dnscheck", True),
        "skip_ipblock": _payload_bool(raw, "skip_ipblock", True),
        "curl_parallelism": _minimum_int(raw.get("curl_parallelism"), default=4, minimum=1),
        "limit_time_enabled": _payload_bool(raw, "limit_time_enabled", False),
        "timeout_hours": _bounded_int(raw.get("timeout_hours"), default=6, minimum=1, maximum=24),
    }


def _profile_name(value: Any) -> str:
    name = str(value or "").strip().lower()
    allowed = []
    for char in name:
        if char.isalnum() or char in {"-", "_"}:
            allowed.append(char)
    return "".join(allowed)[:64]
