'Separated discovery_domains responsibility. Existing algorithms and payload shapes are preserved.'

from __future__ import annotations


from typing import Any
from .discovery_constants import AMAZON_AWS_DOMAINS, CLOUDFLARE_DOMAINS, COVERAGE_DOMAINS, CRITICAL_DOMAINS, DIAGNOSTIC_DOMAINS, DISCORD_DOMAINS, GOOGLE_YOUTUBE_DOMAINS, _CURL_FAILURE_INFO, _DOMAIN_LIST_PREFIXES, _HOSTNAME_RE, _SERVICE_DOMAIN_SUFFIXES



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


def _clean_domains(domains: list[str]) -> list[str]:
    return _clean_domain_list(domains) or list(CRITICAL_DOMAINS)


def _clean_domain_list(domains: list[str]) -> list[str]:
    return list(validate_domain_inputs(list(domains), default_to_critical=False)["domains"])
