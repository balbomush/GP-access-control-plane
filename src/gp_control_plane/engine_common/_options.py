"""engine_common._options — moved from strategy_finder.py / blockchecks_backend.py."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gp_control_plane.engine_common._constants import (
    _CURL_FAILURE_INFO,
    _DOMAIN_LIST_PREFIXES,
    _HOSTNAME_RE,
    _SERVICE_DOMAIN_SUFFIXES,
    AMAZON_AWS_DOMAINS,
    CLOUDFLARE_DOMAINS,
    COVERAGE_DOMAINS,
    CRITICAL_DOMAINS,
    DIAGNOSTIC_DOMAINS,
    DISCORD_DOMAINS,
    GOOGLE_YOUTUBE_DOMAINS,
)


@dataclass(frozen=True)
class DiscoveryOptions:
    enable_http: bool = False
    enable_tls12: bool = True
    enable_tls13: bool = False
    enable_quic: bool = True
    enable_ipv6: bool = False
    scan_level: str = "standard"
    repeats: int = 1
    repeat_parallel: bool = False
    skip_dnscheck: bool = True
    skip_ipblock: bool = True
    curl_max_time: int = 2
    curl_max_time_quic: int = 2
    curl_max_time_doh: int = 2

    def normalized(self) -> DiscoveryOptions:
        scan_level = self.scan_level if self.scan_level in {"quick", "standard", "force"} else "standard"
        repeats = _bounded_int(self.repeats, default=1, minimum=1, maximum=10)
        curl_max_time = _minimum_int(self.curl_max_time, default=2, minimum=1)
        curl_max_time_quic = _minimum_int(self.curl_max_time_quic, default=2, minimum=1)
        curl_max_time_doh = _minimum_int(self.curl_max_time_doh, default=2, minimum=1)
        if not any([self.enable_http, self.enable_tls12, self.enable_tls13, self.enable_quic]):
            raise ValueError("at least one protocol check must be enabled")
        return DiscoveryOptions(
            enable_http=bool(self.enable_http),
            enable_tls12=bool(self.enable_tls12),
            enable_tls13=bool(self.enable_tls13),
            enable_quic=bool(self.enable_quic),
            enable_ipv6=bool(self.enable_ipv6),
            scan_level=scan_level,
            repeats=repeats,
            repeat_parallel=bool(self.repeat_parallel),
            skip_dnscheck=bool(self.skip_dnscheck),
            skip_ipblock=bool(self.skip_ipblock),
            curl_max_time=curl_max_time,
            curl_max_time_quic=curl_max_time_quic,
            curl_max_time_doh=curl_max_time_doh,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "enable_http": self.enable_http,
            "enable_tls12": self.enable_tls12,
            "enable_tls13": self.enable_tls13,
            "enable_quic": self.enable_quic,
            "enable_ipv6": self.enable_ipv6,
            "scan_level": self.scan_level,
            "repeats": self.repeats,
            "repeat_parallel": self.repeat_parallel,
            "skip_dnscheck": self.skip_dnscheck,
            "skip_ipblock": self.skip_ipblock,
            "curl_max_time": self.curl_max_time,
            "curl_max_time_quic": self.curl_max_time_quic,
            "curl_max_time_doh": self.curl_max_time_doh,
        }

    def to_blockcheck_env(self) -> dict[str, str]:
        options = self.normalized()
        return {
            "SKIP_DNSCHECK": "1" if options.skip_dnscheck else "0",
            "SKIP_IPBLOCK": "1" if options.skip_ipblock else "0",
            "ENABLE_HTTP": "1" if options.enable_http else "0",
            "ENABLE_HTTPS_TLS12": "1" if options.enable_tls12 else "0",
            "ENABLE_HTTPS_TLS13": "1" if options.enable_tls13 else "0",
            "ENABLE_HTTP3": "1" if options.enable_quic else "0",
            "SCANLEVEL": options.scan_level,
            "REPEATS": str(options.repeats),
            "PARALLEL": "1" if options.repeat_parallel else "0",
            "CURL_MAX_TIME": str(options.curl_max_time),
            "CURL_MAX_TIME_QUIC": str(options.curl_max_time_quic),
            "CURL_MAX_TIME_DOH": str(options.curl_max_time_doh),
        }

    def to_run_fields(self) -> dict[str, Any]:
        options = self.normalized()
        mapping = options.to_mapping()
        return {
            **mapping,
            "enable_tls": options.enable_tls12,
            "discovery_options": mapping,
        }

def domain_sets() -> dict[str, list[str]]:
    return {
        "critical": list(CRITICAL_DOMAINS),
        "diagnostic": list(DIAGNOSTIC_DOMAINS),
        "coverage": list(COVERAGE_DOMAINS),
        "google-youtube": list(GOOGLE_YOUTUBE_DOMAINS),
        "discord": list(DISCORD_DOMAINS),
        "cloudflare": list(CLOUDFLARE_DOMAINS),
        "amazon-aws": list(AMAZON_AWS_DOMAINS),
    }

def classify_domain_input(value: Any) -> dict[str, Any]:
    raw = str(value or "").strip()
    if not raw:
        return _domain_classification(raw, "", False, "empty", "пустая строка", "строка домена пустая")
    lowered = raw.lower()
    if lowered.startswith(_DOMAIN_LIST_PREFIXES):
        prefix = lowered.split(":", 1)[0]
        return _domain_classification(
            raw,
            "",
            False,
            "domain_list_rule",
            "некорректная строка домена",
            f"строка выглядит как правило domain-list ({prefix}:), а не как готовый домен",
        )
    if raw.startswith("*.") or "*" in raw:
        return _domain_classification(
            raw,
            "",
            False,
            "wildcard",
            "некорректная строка домена",
            "wildcard-строки нельзя передавать в curl как один домен",
        )
    if "://" in raw or any(char in raw for char in "/?#[]@"):
        return _domain_classification(
            raw,
            "",
            False,
            "url",
            "некорректная строка домена",
            "ожидается домен без схемы, пути и query-параметров",
        )
    if ":" in raw:
        return _domain_classification(
            raw,
            "",
            False,
            "port_or_ipv6",
            "некорректная строка домена",
            "ожидается домен без порта и без IPv6-литерала",
        )
    domain = raw.rstrip(".").lower()
    try:
        ascii_domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        return _domain_classification(
            raw,
            "",
            False,
            "idna",
            "некорректная строка домена",
            "домен не удалось привести к IDNA-формату",
        )
    if not _HOSTNAME_RE.match(ascii_domain):
        return _domain_classification(
            raw,
            "",
            False,
            "hostname",
            "некорректная строка домена",
            "строка не похожа на обычный DNS hostname",
        )
    domain_type = "service" if _is_service_domain(ascii_domain) else "https"
    label = "service-домен" if domain_type == "service" else "обычный HTTPS-домен"
    message = (
        "у service-доменов прямой curl может давать TLS/SNI code=60 из-за hostname/сертификата"
        if domain_type == "service"
        else "строка подходит для проверки curl/blockcheck2"
    )
    return _domain_classification(raw, ascii_domain, True, domain_type, label, message)

def validate_domain_inputs(domains: list[Any], *, default_to_critical: bool = False) -> dict[str, Any]:
    raw_values = [str(domain).strip() for domain in domains if str(domain or "").strip()]
    if not raw_values and default_to_critical:
        raw_values = list(CRITICAL_DOMAINS)
    valid: list[str] = []
    seen: set[str] = set()
    classification: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for raw in raw_values:
        item = classify_domain_input(raw)
        if item["valid"]:
            domain = str(item["domain"])
            if domain not in seen:
                valid.append(domain)
                seen.add(domain)
                classification.append(item)
            continue
        skipped.append(item)
    summary: dict[str, int] = {}
    for item in [*classification, *skipped]:
        status = str(item.get("status") or "unknown")
        summary[status] = summary.get(status, 0) + 1
    return {
        "input_count": len(raw_values),
        "valid_count": len(valid),
        "skipped_count": len(skipped),
        "domains": valid,
        "domain_classification": classification,
        "domain_skipped": skipped,
        "summary": summary,
    }

def curl_failure_info(code: Any, *, test: str = "", domain: str = "") -> dict[str, Any]:
    code_text = str(code or "").strip()
    base = dict(
        _CURL_FAILURE_INFO.get(
            code_text,
            {
                "status": "curl_error",
                "label": "curl ошибка",
                "message": "curl вернул ошибку, для которой пока нет отдельной трактовки.",
            },
        )
    )
    if code_text == "7" and "http3" not in str(test).lower():
        base["label"] = "connect ошибка"
        base["message"] = "соединение не установилось."
    if code_text == "60" and _is_service_domain(str(domain or "")):
        base["service_domain"] = True
        base["message"] = (
            "service-домен вернул TLS/SNI mismatch; это надо показывать отдельно от провала стратегии."
        )
    base["code"] = code_text
    return base

def _domain_classification(raw: str, domain: str, valid: bool, status: str, label: str, message: str) -> dict[str, Any]:
    return {
        "raw": raw,
        "domain": domain,
        "valid": valid,
        "status": status,
        "label": label,
        "message": message,
    }

def _is_service_domain(domain: str) -> bool:
    value = str(domain or "").lower().rstrip(".")
    return any(value == suffix or value.endswith(f".{suffix}") for suffix in _SERVICE_DOMAIN_SUFFIXES)

def _domain_validation_run_fields(validation: dict[str, Any]) -> dict[str, Any]:
    return {
        "domain_input_count": int(validation.get("input_count") or 0),
        "domain_valid_count": int(validation.get("valid_count") or 0),
        "domain_skipped_count": int(validation.get("skipped_count") or 0),
        "domain_skipped": list(validation.get("domain_skipped") or [])[:50],
        "domain_classification": list(validation.get("domain_classification") or [])[:100],
        "domain_validation_summary": dict(validation.get("summary") or {}),
    }

def _truthy(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
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
