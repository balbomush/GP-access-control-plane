from __future__ import annotations

import socket
import ssl
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable

from . import simpleyaml


@dataclass
class HealthcheckResult:
    domain: str
    dns_ok: bool
    tcp_ok: bool
    https_ok: bool
    latency_ms: int | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.dns_ok and self.tcp_ok and self.https_ok

    def to_mapping(self) -> dict[str, object]:
        data = asdict(self)
        data["ok"] = self.ok
        return data


Resolver = Callable[[str], list[str]]
HttpsProbe = Callable[[str, str, float], tuple[bool, bool, int | None, str | None]]


def check_domains_direct(
    domains: list[str],
    timeout_seconds: float = 5,
    resolver: Resolver | None = None,
    https_probe: HttpsProbe | None = None,
) -> list[HealthcheckResult]:
    resolver = resolver or resolve_domain
    https_probe = https_probe or probe_https
    return [_check_one(domain, timeout_seconds, resolver, https_probe) for domain in domains]


def write_report(path: Path, results: list[HealthcheckResult]) -> None:
    payload = {
        "version": 1,
        "results": [result.to_mapping() for result in results],
    }
    simpleyaml.dump_file(path, payload)


def resolve_domain(domain: str) -> list[str]:
    infos = socket.getaddrinfo(domain, 443, type=socket.SOCK_STREAM)
    addresses = []
    for info in infos:
        address = info[4][0]
        if address not in addresses:
            addresses.append(address)
    return addresses


def _check_one(domain: str, timeout_seconds: float, resolver: Resolver, https_probe: HttpsProbe) -> HealthcheckResult:
    try:
        addresses = resolver(domain)
        if not addresses:
            return HealthcheckResult(domain, False, False, False, None, "no DNS addresses")
    except Exception as exc:  # noqa: BLE001
        return HealthcheckResult(domain, False, False, False, None, f"dns: {exc}")

    tcp_ok, https_ok, latency_ms, error = https_probe(domain, addresses[0], timeout_seconds)
    return HealthcheckResult(domain, True, tcp_ok, https_ok, latency_ms, error)


def probe_https(domain: str, address: str, timeout_seconds: float) -> tuple[bool, bool, int | None, str | None]:
    start = time.monotonic()
    try:
        with socket.create_connection((address, 443), timeout=timeout_seconds) as sock:
            context = ssl.create_default_context()
            with context.wrap_socket(sock, server_hostname=domain) as tls:
                request = f"HEAD / HTTP/1.1\r\nHost: {domain}\r\nConnection: close\r\n\r\n"
                tls.settimeout(timeout_seconds)
                tls.sendall(request.encode("ascii"))
                response = tls.recv(32)
                latency_ms = int((time.monotonic() - start) * 1000)
                return True, response.startswith(b"HTTP/"), latency_ms, None
    except Exception as exc:  # noqa: BLE001
        latency_ms = int((time.monotonic() - start) * 1000)
        return False, False, latency_ms, f"https: {exc}"
