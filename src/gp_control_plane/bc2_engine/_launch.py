"""bc2_engine._launch — moved from strategy_finder.py / blockchecks_backend.py."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from gp_control_plane.bc2_engine._runner import (
    _run_blockcheck_live,
    _run_multidomain_blockcheck_live,
)
from gp_control_plane.engine_common._options import DiscoveryOptions, validate_domain_inputs


def run_standard_discovery(
    domains: list[str],
    state_dir: Path,
    timeout_seconds: int,
    include_quic: bool = True,
    enable_http: bool = False,
    enable_tls12: bool = True,
    enable_tls13: bool = False,
    enable_ipv6: bool = False,
    scan_level: str = "standard",
    repeats: int = 1,
    repeat_parallel: bool = False,
    skip_dnscheck: bool = True,
    skip_ipblock: bool = True,
    curl_max_time: int = 2,
    curl_max_time_quic: int = 2,
    curl_max_time_doh: int = 2,
    debug_stdout: bool | None = None,
    stop_event: threading.Event | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    options = DiscoveryOptions(
        enable_http=enable_http,
        enable_tls12=enable_tls12,
        enable_tls13=enable_tls13,
        enable_quic=include_quic,
        enable_ipv6=enable_ipv6,
        scan_level=scan_level,
        repeats=repeats,
        repeat_parallel=repeat_parallel,
        skip_dnscheck=skip_dnscheck,
        skip_ipblock=skip_ipblock,
        curl_max_time=curl_max_time,
        curl_max_time_quic=curl_max_time_quic,
        curl_max_time_doh=curl_max_time_doh,
    ).normalized()
    domain_validation = validate_domain_inputs(domains, default_to_critical=True)
    if not domain_validation["domains"]:
        raise ValueError("no valid domains to check")
    return _run_blockcheck_live(
        state_dir=state_dir,
        kind="standard-discovery",
        domains=list(domain_validation["domains"]),
        timeout_seconds=timeout_seconds,
        test="standard",
        options=options,
        domain_validation=domain_validation,
        debug_stdout=debug_stdout,
        stop_event=stop_event,
        run_id=run_id,
    )

def run_multi_domain_discovery(
    domains: list[str],
    state_dir: Path,
    timeout_seconds: int,
    include_quic: bool = True,
    enable_http: bool = False,
    enable_tls12: bool = True,
    enable_tls13: bool = False,
    enable_ipv6: bool = False,
    scan_level: str = "standard",
    repeats: int = 1,
    repeat_parallel: bool = False,
    skip_dnscheck: bool = True,
    skip_ipblock: bool = True,
    curl_max_time: int = 2,
    curl_max_time_quic: int = 2,
    curl_max_time_doh: int = 2,
    curl_parallelism: int = 4,
    debug_stdout: bool | None = None,
    stop_event: threading.Event | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    options = DiscoveryOptions(
        enable_http=enable_http,
        enable_tls12=enable_tls12,
        enable_tls13=enable_tls13,
        enable_quic=include_quic,
        enable_ipv6=enable_ipv6,
        scan_level=scan_level,
        repeats=repeats,
        repeat_parallel=repeat_parallel,
        skip_dnscheck=skip_dnscheck,
        skip_ipblock=skip_ipblock,
        curl_max_time=curl_max_time,
        curl_max_time_quic=curl_max_time_quic,
        curl_max_time_doh=curl_max_time_doh,
    ).normalized()
    domain_validation = validate_domain_inputs(domains, default_to_critical=True)
    if not domain_validation["domains"]:
        raise ValueError("no valid domains to check")
    return _run_multidomain_blockcheck_live(
        state_dir=state_dir,
        domains=list(domain_validation["domains"]),
        timeout_seconds=timeout_seconds,
        options=options,
        curl_parallelism=curl_parallelism,
        domain_validation=domain_validation,
        debug_stdout=debug_stdout,
        stop_event=stop_event,
        run_id=run_id,
    )
