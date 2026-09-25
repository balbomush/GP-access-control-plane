"""Thin blockcheck2 adapter retaining the established discovery algorithms."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from ..config import AppConfig
from ..settings import read_run_settings
from ..strategy_finder import run_multi_domain_discovery, run_standard_discovery

if TYPE_CHECKING:
    from ..application.discovery import DiscoverySpec


DiscoveryCallable = Callable[..., dict[str, Any]]


class Blockcheck2Adapter:
    """Map a normalized spec to the pre-existing standard or multi-domain call."""

    def __init__(
        self,
        config: AppConfig,
        *,
        standard_discovery: DiscoveryCallable = run_standard_discovery,
        multi_domain_discovery: DiscoveryCallable = run_multi_domain_discovery,
    ):
        self._config = config
        self._standard_discovery = standard_discovery
        self._multi_domain_discovery = multi_domain_discovery

    def execute_standard(self, spec: DiscoverySpec, stop_event: Any, run_id: str) -> dict[str, Any]:
        payload = spec.payload
        settings = read_run_settings(self._config)
        return self._standard_discovery(
            _payload_domains(payload),
            self._config.output.state_dir,
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

    def execute_multi_domain(self, spec: DiscoverySpec, stop_event: Any, run_id: str) -> dict[str, Any]:
        payload = spec.payload
        settings = read_run_settings(self._config)
        max_parallelism = _minimum_int(settings.get("curl_parallelism_max"), default=10, minimum=1)
        return self._multi_domain_discovery(
            _payload_domains(payload),
            self._config.output.state_dir,
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
            curl_parallelism=_bounded_int(
                payload.get("curl_parallelism"),
                default=int(settings.get("curl_parallelism_default") or 4),
                minimum=1,
                maximum=max_parallelism,
            ),
            debug_stdout=_payload_bool(payload, "debug_stdout", bool(settings.get("debug_stdout"))),
            stop_event=stop_event,
            run_id=run_id,
        )


def _payload_domains(payload: Mapping[str, Any]) -> list[str]:
    raw = payload.get("domains") or []
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    if not isinstance(raw, list):
        raw = []
    return [str(domain).strip() for domain in raw if str(domain).strip()]


def _payload_timeout_seconds(payload: Mapping[str, Any], default: int) -> int:
    if "timeout_seconds" not in payload or payload.get("timeout_seconds") is None:
        return default
    try:
        seconds = int(payload.get("timeout_seconds"))
    except (TypeError, ValueError):
        return default
    return max(0, seconds)


def _payload_int(payload: Mapping[str, Any], key: str, default: int) -> int:
    try:
        return int(payload.get(key, default))
    except (TypeError, ValueError):
        return default


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _minimum_int(value: Any, default: int, minimum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, number)


def _payload_bool(payload: Mapping[str, Any], key: str, default: bool) -> bool:
    if key not in payload:
        return default
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)
