from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .state import append_jsonl, now_iso
from .strategy_safety import analyze_strategy
from .storage import (
    append_run,
    connect,
    count_latest_run_payloads,
    read_latest_run_payloads,
    read_run_payloads,
    upsert_candidate_event,
    upsert_candidate_event_conn,
)
from .zapret2 import (
    BLOCKCHECK_ENV_KEYS,
    _cleanup_blockcheck_processes,
    _cleanup_nft_blockcheck_tables,
    _stop_process_group,
    root_command,
)


CRITICAL_DOMAINS = ["youtube.com", "googlevideo.com", "discord.com", "discordcdn.com"]
DIAGNOSTIC_DOMAINS = ["web.telegram.org"]
COVERAGE_DOMAINS = [
    "youtu.be",
    "googleapis.com",
    "i.ytimg.com",
    "i9.ytimg.com",
    "yt3.ggpht.com",
    "yt3.googleusercontent.com",
    "yt4.ggpht.com",
    "yt4.googleusercontent.com",
    "gvt1.com",
    "gstatic.com",
    "youtube-ui.l.google.com",
    "ytimg.l.google.com",
    "ytstatic.l.google.com",
    "play.google.com",
    "discord-attachments-uploads-prd.storage.googleapis.com",
    "dis.gd",
    "discord.co",
    "discord.com",
    "discord.design",
    "discord.dev",
    "discord.gg",
    "discord.gift",
    "discord.gifts",
    "discord.media",
    "discord.new",
    "discord.store",
    "discord.tools",
    "discordapp.com",
    "discordapp.net",
    "discordmerch.com",
    "discordpartygames.com",
    "discord-activities.com",
    "discordactivities.com",
    "discordsays.com",
    "discordstatus.com",
    "speedtest.net",
    "cloudflare-ech.com",
]
GOOGLE_YOUTUBE_DOMAINS = [
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "youtubei.googleapis.com",
    "youtube.googleapis.com",
    "googlevideo.com",
    "video.google.com",
    "i.ytimg.com",
    "i9.ytimg.com",
    "ytimg.com",
    "yt3.ggpht.com",
    "yt3.googleusercontent.com",
    "yt4.ggpht.com",
    "yt4.googleusercontent.com",
    "ggpht.com",
    "gstatic.com",
    "gvt1.com",
    "googleapis.com",
    "googleusercontent.com",
    "play.google.com",
]
DISCORD_DOMAINS = [
    "discord.com",
    "discord.gg",
    "discordapp.com",
    "discordapp.net",
    "discordcdn.com",
    "discord.media",
    "discord.co",
    "discord.design",
    "discord.dev",
    "discord.gift",
    "discord.gifts",
    "discord.new",
    "discord.store",
    "discord.tools",
    "discordmerch.com",
    "discordpartygames.com",
    "discord-activities.com",
    "discordactivities.com",
    "discordsays.com",
    "discordstatus.com",
    "dis.gd",
    "discord-attachments-uploads-prd.storage.googleapis.com",
]
CLOUDFLARE_DOMAINS = [
    "cloudflare.com",
    "www.cloudflare.com",
    "cloudflare-dns.com",
    "cloudflare-ech.com",
    "cloudflareclient.com",
    "cloudflareinsights.com",
    "cdnjs.cloudflare.com",
    "workers.dev",
    "pages.dev",
]
AMAZON_AWS_DOMAINS = [
    "amazon.com",
    "www.amazon.com",
    "amazonaws.com",
    "aws.amazon.com",
    "cloudfront.net",
    "s3.amazonaws.com",
    "ec2.amazonaws.com",
    "globalaccelerator.amazonaws.com",
    "media-amazon.com",
    "ssl-images-amazon.com",
    "images-na.ssl-images-amazon.com",
]

ATTEMPT_TIMEOUT_ESTIMATE_MS = 2100
ETA_SAMPLE_MIN_ATTEMPTS = 3
ETA_SAMPLE_MAX_POINTS = 201
ETA_SAMPLE_WINSORIZE_MIN_INTERVALS = 20
ETA_SAMPLE_WINSORIZE_RATIO = 0.1
ETA_RECALC_SMALL_STEP = 10
ETA_RECALC_LARGE_STEP = 100
ETA_RECALC_LARGE_AFTER = 1000
LIVE_CANDIDATE_FLUSH_SIZE = 50
LIVE_CANDIDATE_QUEUE_MAX_BATCHES = 128
LIVE_CANDIDATE_SAMPLE_LIMIT = 200
_CANDIDATE_WRITER_STOP = object()
METRICS_INTERVAL_SECONDS = 10.0
METRICS_MAX_BYTES = 1_000_000
STDOUT_LOG_MAX_BYTES = 2_000_000
DEBUG_STDOUT_LOG_MAX_BYTES = 10_000_000
LOG_RETENTION_MAX_FILES = 120
LOG_RETENTION_MAX_TOTAL_BYTES = 100_000_000
LOG_RETENTION_SUFFIXES = (
    ".stdout.log",
    ".stderr.log",
    ".debug.stdout.log",
    ".progress.json",
    ".metrics.ndjson",
    ".summary-fallback.ndjson",
)
PHASE_CHECK_VPN = "checking_vpn"
PHASE_CHECK_ZAPRET = "checking_zapret"
PHASE_CHECK_DOMAIN = "checking_domain"
PHASE_DISCOVERY = "strategy_discovery"
PHASE_SUMMARY = "strategy_summary"
PHASE_SAVING = "saving_results"
PHASE_COMPLETE = "complete"
PHASE_LABELS = {
    PHASE_CHECK_VPN: "проверка VPN",
    PHASE_CHECK_ZAPRET: "проверка zapret",
    PHASE_CHECK_DOMAIN: "проверка доступности домена",
    PHASE_DISCOVERY: "подбор стратегий",
    PHASE_SUMMARY: "суммаризация стратегий",
    PHASE_SAVING: "сохранение результатов",
    PHASE_COMPLETE: "завершено",
}
_ATTEMPT_PLAN_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}
_ATTEMPT_RE = re.compile(r"^-\s+curl_test_")
_SCRIPT_RE = re.compile(r"^\*\s+script\s+:\s+(.+)$")
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$",
    re.IGNORECASE,
)
_DOMAIN_LIST_PREFIXES = ("domain:", "full:", "keyword:", "regexp:", "include:", "geosite:")
_SERVICE_DOMAIN_SUFFIXES = (
    "googlevideo.com",
    "googleapis.com",
    "googleusercontent.com",
    "gstatic.com",
    "gvt1.com",
    "ggpht.com",
    "cloudflare-ech.com",
    "cloudfront.net",
    "amazonaws.com",
    "discordcdn.com",
)
_CURL_FAILURE_INFO = {
    "3": {
        "status": "invalid_domain",
        "label": "некорректная строка домена",
        "message": "curl не смог разобрать строку как домен или URL.",
    },
    "6": {
        "status": "dns_error",
        "label": "DNS ошибка",
        "message": "домен не резолвится или DNS не вернул адрес.",
    },
    "7": {
        "status": "quic_connect_error",
        "label": "QUIC/connect ошибка",
        "message": "соединение не установилось; для HTTP3/QUIC это отдельный сетевой сбой.",
    },
    "28": {
        "status": "timeout",
        "label": "таймаут",
        "message": "соединение не завершилось за лимит времени.",
    },
    "35": {
        "status": "ssl_connect_error",
        "label": "SSL/connect ошибка",
        "message": "ошибка TLS/SSL или уровня соединения.",
    },
    "60": {
        "status": "tls_sni_problem",
        "label": "TLS/SNI проблема",
        "message": "сертификат или hostname не совпали; для service-доменов это не всегда провал стратегии.",
    },
}
DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 200
NFQUEUE_MAXLEN_MISSING_RE = re.compile(r"can't set queue maxlen:\s+No such file or directory", re.IGNORECASE)


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

    def normalized(self) -> "DiscoveryOptions":
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


def _ipvs_value(options: DiscoveryOptions) -> str:
    return "4 6" if options.enable_ipv6 else "4"


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
    )


def read_candidates(state_dir: Path) -> list[dict[str, Any]]:
    with connect(state_dir) as conn:
        rows = conn.execute(
            """
            SELECT id, protocol, args, status,
                   fragmentation_class, fragmentation_safe, fragmentation_reason,
                   family, family_key, family_rank, family_reason
            FROM strategies
            ORDER BY id ASC
            """
        ).fetchall()
        return [_candidate_from_db(conn, row, include_events=True) for row in rows]


def read_candidate_page(
    state_dir: Path,
    *,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
    query: str = "",
    view: str = "domain",
    domains: list[str] | None = None,
    domain: str = "",
    fragmentation_classes: list[str] | None = None,
) -> dict[str, Any]:
    limit = _bounded_int(limit, default=DEFAULT_PAGE_LIMIT, minimum=1, maximum=MAX_PAGE_LIMIT)
    offset = max(0, _bounded_int(offset, default=0, minimum=0, maximum=10_000_000))
    query = query.strip().lower()
    view = view if view in {"domain", "common"} else "domain"
    selected_domains = _clean_domain_list(domains or [])
    selected_domain = domain.strip()
    with connect(state_dir) as conn:
        tested_domains = _tested_domains_from_db(conn)
        rows, total = _read_candidate_page_sql(
            conn,
            limit=limit,
            offset=offset,
            query=query,
            view=view,
            domains=selected_domains,
            domain=selected_domain,
            fragmentation_classes=_clean_fragmentation_classes(fragmentation_classes or []),
        )
        candidates = [_compact_candidate(_candidate_from_db(conn, row, include_events=False)) for row in rows]
    version = _storage_version(state_dir)
    return {
        "candidates": candidates,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(rows) < total,
        "tested_domains": sorted(tested_domains),
        "version": version,
    }


def candidate_storage_version(state_dir: Path) -> dict[str, int]:
    return _storage_version(state_dir)


def read_candidate_domain_index(
    state_dir: Path,
    *,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
    query: str = "",
    fragmentation_classes: list[str] | None = None,
) -> dict[str, Any]:
    limit = _bounded_int(limit, default=DEFAULT_PAGE_LIMIT, minimum=1, maximum=MAX_PAGE_LIMIT)
    offset = max(0, _bounded_int(offset, default=0, minimum=0, maximum=10_000_000))
    query = query.strip().lower()
    clean_fragmentation_classes = _clean_fragmentation_classes(fragmentation_classes or [])
    with connect(state_dir) as conn:
        tested_domains = _tested_domains_from_db(conn)
        rows, total, strategy_total = _read_candidate_domain_index_sql(
            conn,
            limit=limit,
            offset=offset,
            query=query,
            fragmentation_classes=clean_fragmentation_classes,
        )
    return {
        "domains": rows,
        "total": total,
        "strategy_total": strategy_total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(rows) < total,
        "tested_domains": sorted(tested_domains),
        "version": _storage_version(state_dir),
    }


def _read_candidate_page_sql(
    conn: Any,
    *,
    limit: int,
    offset: int,
    query: str,
    view: str,
    domains: list[str],
    domain: str,
    fragmentation_classes: list[str],
) -> tuple[list[Any], int]:
    query_clause, query_params = _strategy_query_clause(query)
    fragmentation_clause, fragmentation_params = _fragmentation_query_clause(fragmentation_classes)
    if view == "common":
        if len(domains) < 2:
            return [], 0
        placeholders = ", ".join("?" for _item in domains)
        base = f"""
            FROM strategies s
            JOIN strategy_domain_results r ON r.strategy_id = s.id
            JOIN domains d ON d.id = r.domain_id
            WHERE d.name IN ({placeholders}) {query_clause} {fragmentation_clause}
            GROUP BY s.id
            HAVING COUNT(DISTINCT d.name) = ?
        """
        params: list[Any] = [*domains, *query_params, *fragmentation_params, len(domains)]
    elif domain:
        base = f"""
            FROM strategies s
            JOIN strategy_domain_results r ON r.strategy_id = s.id
            JOIN domains d ON d.id = r.domain_id
            WHERE d.name = ? {query_clause} {fragmentation_clause}
            GROUP BY s.id
        """
        params = [domain, *query_params, *fragmentation_params]
    else:
        base = f"""
            FROM strategies s
            JOIN strategy_domain_results r ON r.strategy_id = s.id
            JOIN domains d ON d.id = r.domain_id
            WHERE 1 = 1 {query_clause} {fragmentation_clause}
            GROUP BY s.id
        """
        params = [*query_params, *fragmentation_params]
    total = int(
        conn.execute(
            f"SELECT COUNT(*) AS count FROM (SELECT s.id {base}) AS candidate_page",
            params,
        ).fetchone()["count"]
    )
    rows = conn.execute(
        f"""
        SELECT s.id, s.protocol, s.args, s.status
               , s.fragmentation_class, s.fragmentation_safe, s.fragmentation_reason
               , s.family, s.family_key, s.family_rank, s.family_reason
        {base}
        ORDER BY s.id ASC
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()
    return rows, total


def _read_candidate_domain_index_sql(
    conn: Any,
    *,
    limit: int,
    offset: int,
    query: str,
    fragmentation_classes: list[str],
) -> tuple[list[dict[str, Any]], int, int]:
    query_clause, query_params = _strategy_query_clause(query)
    fragmentation_clause, fragmentation_params = _fragmentation_query_clause(fragmentation_classes)
    base = f"""
        FROM domains d
        JOIN strategy_domain_results r ON r.domain_id = d.id
        JOIN strategies s ON s.id = r.strategy_id
        WHERE 1 = 1 {query_clause} {fragmentation_clause}
        GROUP BY d.id, d.name
    """
    count_row = conn.execute(
        f"""
        SELECT COUNT(*) AS count, COALESCE(SUM(strategy_count), 0) AS strategy_total
        FROM (
            SELECT d.id, COUNT(DISTINCT r.strategy_id) AS strategy_count
            {base}
        ) domain_index
        """,
        [*query_params, *fragmentation_params],
    ).fetchone()
    total = int(count_row["count"] or 0) if count_row else 0
    strategy_total = int(count_row["strategy_total"] or 0) if count_row else 0
    domain_rows = conn.execute(
        f"""
        SELECT d.name AS domain, COUNT(DISTINCT r.strategy_id) AS strategy_count
        {base}
        ORDER BY d.name ASC
        LIMIT ? OFFSET ?
        """,
        [*query_params, *fragmentation_params, limit, offset],
    ).fetchall()
    page_domains = [str(row["domain"]) for row in domain_rows]
    if not page_domains:
        return [], total, strategy_total
    page_placeholders = ", ".join("?" for _item in page_domains)
    protocol_rows = conn.execute(
        f"""
        SELECT d.name AS domain, r.protocol AS protocol, COUNT(DISTINCT r.strategy_id) AS count
        FROM domains d
        JOIN strategy_domain_results r ON r.domain_id = d.id
        JOIN strategies s ON s.id = r.strategy_id
        WHERE d.name IN ({page_placeholders}) {query_clause} {fragmentation_clause}
        GROUP BY d.id, d.name, r.protocol
        ORDER BY d.name ASC, r.protocol ASC
        """,
        [*page_domains, *query_params, *fragmentation_params],
    ).fetchall()
    protocols: dict[str, list[dict[str, Any]]] = {}
    for row in protocol_rows:
        protocols.setdefault(str(row["domain"]), []).append(
            {"protocol": str(row["protocol"] or "unknown"), "count": int(row["count"] or 0)}
        )
    rows = [
        {
            "domain": str(row["domain"]),
            "strategy_count": int(row["strategy_count"] or 0),
            "protocols": protocols.get(str(row["domain"]), []),
        }
        for row in domain_rows
    ]
    return rows, total, strategy_total


def _strategy_query_clause(query: str) -> tuple[str, list[Any]]:
    if not query:
        return "", []
    pattern = f"%{query.lower()}%"
    return (
        "AND (LOWER(s.id) LIKE ? OR LOWER(s.protocol) LIKE ? OR LOWER(s.args) LIKE ? OR LOWER(d.name) LIKE ?)",
        [pattern, pattern, pattern, pattern],
    )


def _clean_fragmentation_classes(values: list[str]) -> list[str]:
    allowed = {"position_free", "position_safe", "position_risky", "unknown"}
    result: list[str] = []
    for raw in values:
        for item in str(raw or "").split(","):
            clean = item.strip()
            if clean in allowed and clean not in result:
                result.append(clean)
    return result


def _fragmentation_query_clause(classes: list[str]) -> tuple[str, list[Any]]:
    if not classes:
        return "", []
    placeholders = ", ".join("?" for _item in classes)
    return f"AND s.fragmentation_class IN ({placeholders})", list(classes)


def read_runs(state_dir: Path, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    return [_compact_run(run) for run in read_run_payloads(state_dir, limit=limit, offset=offset)]


def read_runs_page(state_dir: Path, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    limit = _bounded_int(limit, default=50, minimum=1, maximum=1000)
    offset = max(0, _bounded_int(offset, default=0, minimum=0, maximum=10_000_000))
    runs = [_compact_run(run) for run in read_latest_run_payloads(state_dir, limit=limit, offset=offset)]
    total = count_latest_run_payloads(state_dir)
    return {
        "runs": runs,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(runs) < total,
    }


def _compact_run(run: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in run.items()
        if key
        not in {
            "summary",
            "common",
            "live_summary",
            "results",
            "common_results",
            "direct_available",
            "not_working",
        }
    }


def close_stale_running_runs(state_dir: Path) -> int:
    root = _finder_dir(state_dir)
    runs = read_runs(state_dir, limit=200)
    latest_by_id: dict[str, dict[str, Any]] = {}
    for run in runs:
        run_id = str(run.get("id") or "")
        if run_id:
            latest_by_id[run_id] = run
    closed = 0
    for run in latest_by_id.values():
        if str(run.get("status") or "") not in {"queued", "running", "stopping"}:
            continue
        progress = run.get("progress")
        if not isinstance(progress, dict):
            progress = _read_progress_log(run)
        update = {
            "id": run.get("id"),
            "kind": run.get("kind"),
            "candidate_id": run.get("candidate_id", ""),
            "status": "stopped",
            "timestamp": run.get("timestamp") or now_iso(),
            "started_at": run.get("started_at") or run.get("timestamp") or "",
            "completed_at": now_iso(),
            "domains": run.get("domains") or [],
            "returncode": run.get("returncode"),
            "stdout_log": run.get("stdout_log", ""),
            "stderr_log": run.get("stderr_log", ""),
            "progress_log": run.get("progress_log", ""),
            "metrics_log": run.get("metrics_log", ""),
            "summary_fallback_log": run.get("summary_fallback_log", ""),
            "candidate_count": int(run.get("candidate_count") or 0),
            "common_candidate_count": int(run.get("common_candidate_count") or 0),
            "total_candidates": int(run.get("total_candidates") or 0),
            "phase": run.get("phase") or (progress.get("phase") if isinstance(progress, dict) else ""),
            "stopped": True,
            "interrupted": True,
            "interrupted_reason": "web service stopped while run was marked active",
            "test": run.get("test", "standard"),
            "attempt_plan": run.get("attempt_plan") or {},
        }
        if isinstance(progress, dict):
            update["progress"] = progress
        for key in (
            "enable_http",
            "enable_tls",
            "enable_tls13",
            "enable_quic",
            "scan_level",
            "repeats",
            "repeat_parallel",
            "skip_dnscheck",
            "skip_ipblock",
            "curl_parallelism",
            "discovery_options",
        ):
            if key in run:
                update[key] = run[key]
        append_run(state_dir, update)
        closed += 1
    return closed


def latest_log_tail(
    state_dir: Path,
    max_lines: int = 200,
    *,
    stdout_from_size: int | None = None,
    stdout_log_match: str | None = None,
    stderr_from_size: int | None = None,
    stderr_log_match: str | None = None,
) -> dict[str, Any]:
    for run in reversed(read_runs(state_dir, limit=200)):
        stdout_log = Path(str(run.get("stdout_log") or ""))
        if not stdout_log.is_file():
            continue
        stderr_log_raw = str(run.get("stderr_log") or "")
        stderr_log = Path(stderr_log_raw) if stderr_log_raw else None
        stdout_delta = _log_delta(stdout_log, stdout_log_match, stdout_from_size)
        stderr_delta = _log_delta(stderr_log, stderr_log_match, stderr_from_size) if stderr_log and stderr_log.is_file() else None
        if stdout_delta is None:
            stdout_tail = "\n".join(_tail_lines(stdout_log, max_lines))
            stdout_append = ""
        else:
            stdout_tail = ""
            stdout_append = stdout_delta
        if stderr_delta is None:
            stderr_tail = "\n".join(_tail_lines(stderr_log, max_lines)) if stderr_log and stderr_log.is_file() else ""
            stderr_append = ""
        else:
            stderr_tail = ""
            stderr_append = stderr_delta
        stderr_diagnostics = classify_stderr_diagnostics("\n".join(part for part in (stderr_tail, stderr_append) if part))
        progress = run.get("progress")
        if not isinstance(progress, dict):
            progress = _read_progress_log(run)
        if not isinstance(progress, dict):
            if not stdout_tail:
                stdout_tail = "\n".join(_tail_lines(stdout_log, max_lines))
            progress = progress_from_stdout(stdout_tail, run)
            progress["partial"] = True
        return {
            "run_id": run.get("id"),
            "kind": run.get("kind"),
            "status": run.get("status"),
            "stdout_tail": stdout_tail,
            "stdout_append": stdout_append,
            "stderr_tail": stderr_tail,
            "stderr_append": stderr_append,
            "stderr_diagnostics": stderr_diagnostics,
            "stdout_log": str(stdout_log),
            "stderr_log": str(stderr_log) if stderr_log else "",
            "stdout_size": _file_version(stdout_log)["size"],
            "stderr_size": _file_version(stderr_log)["size"] if stderr_log and stderr_log.is_file() else 0,
            "progress": progress,
            "metrics": _read_latest_metrics(run),
            "run_settings": _run_settings_for_progress(run),
        }
    return {
        "run_id": None,
        "kind": None,
        "status": None,
        "stdout_tail": "",
        "stdout_append": "",
        "stderr_tail": "",
        "stderr_append": "",
        "stderr_diagnostics": [],
        "stdout_size": 0,
        "stderr_size": 0,
        "progress": progress_from_stdout("", {}),
        "metrics": {},
        "run_settings": {},
    }


def classify_stderr_diagnostics(stderr_text: str) -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in stderr_text.splitlines():
        text = line.strip()
        if not text:
            continue
        if NFQUEUE_MAXLEN_MISSING_RE.search(text):
            status = "nfqueue_maxlen_sysctl_missing"
            if status in seen:
                continue
            seen.add(status)
            diagnostics.append(
                {
                    "severity": "warning",
                    "status": status,
                    "label": "NFQUEUE maxlen недоступен",
                    "message": (
                        "На этой системе нет sysctl для queue maxlen. "
                        "Это совместимость ядра/NFQUEUE: подбор может продолжаться, "
                        "строка не считается фатальной ошибкой GP."
                    ),
                    "source": "stderr",
                    "line": text,
                }
            )
    return diagnostics


def _run_settings_for_progress(run: dict[str, Any]) -> dict[str, Any]:
    options = run.get("discovery_options") if isinstance(run.get("discovery_options"), dict) else {}

    def option_value(key: str, fallback_keys: tuple[str, ...] = (), default: Any = None) -> Any:
        if key in options:
            return options[key]
        for fallback_key in fallback_keys:
            if fallback_key in run:
                return run[fallback_key]
        if key in run:
            return run[key]
        return default

    return {
        "domain_count": len(run.get("domains") or []),
        "kind": run.get("kind") or "",
        "enable_http": bool(option_value("enable_http", default=False)),
        "enable_tls12": bool(option_value("enable_tls12", ("enable_tls",), True)),
        "enable_tls13": bool(option_value("enable_tls13", default=False)),
        "enable_quic": bool(option_value("enable_quic", ("include_quic",), True)),
        "enable_ipv6": bool(option_value("enable_ipv6", default=False)),
        "scan_level": str(option_value("scan_level", default="standard") or "standard"),
        "repeats": _bounded_int(option_value("repeats", default=1), default=1, minimum=1, maximum=10),
        "repeat_parallel": bool(option_value("repeat_parallel", default=False)),
        "skip_dnscheck": bool(option_value("skip_dnscheck", default=True)),
        "skip_ipblock": bool(option_value("skip_ipblock", default=True)),
        "curl_parallelism": _minimum_int(run.get("curl_parallelism"), default=4, minimum=1)
        if str(run.get("kind") or "") == "multi-domain-discovery"
        else None,
        "timeout_seconds": _minimum_int(run.get("timeout_seconds"), default=0, minimum=0),
        "curl_max_time": _minimum_int(option_value("curl_max_time", default=2), default=2, minimum=1),
        "curl_max_time_quic": _minimum_int(option_value("curl_max_time_quic", default=2), default=2, minimum=1),
        "curl_max_time_doh": _minimum_int(option_value("curl_max_time_doh", default=2), default=2, minimum=1),
    }


def _read_progress_log(run: dict[str, Any]) -> dict[str, Any] | None:
    progress_log = str(run.get("progress_log") or "")
    if not progress_log:
        return None
    path = Path(progress_log)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_latest_metrics(run: dict[str, Any]) -> dict[str, Any]:
    metrics_log = str(run.get("metrics_log") or "")
    if not metrics_log:
        return {}
    path = Path(metrics_log)
    if not path.is_file():
        return {}
    for line in reversed(_tail_lines(path, 20)):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return {}


def parse_blockcheck_stdout(stdout: str) -> dict[str, Any]:
    sections = _summary_sections(stdout)
    summary = sections["summary"]
    common = sections["common"]
    live_summary = _live_available_lines(stdout)
    candidates = _dedupe_candidate_lines([*_candidate_lines(summary, scope="domain"), *_candidate_lines(live_summary, scope="domain")])
    common_candidates = _candidate_lines(common, scope="common")
    results = [_parse_result_line(line) for line in summary if _parse_result_line(line)]
    common_results = [_parse_result_line(line) for line in common if _parse_result_line(line)]
    diagnostic_counts, diagnostic_codes, curl_diagnostics = _diagnostic_counts_from_stdout(stdout, results)
    return {
        "summary": summary,
        "common": common,
        "live_summary": live_summary,
        "candidates": candidates,
        "common_candidates": common_candidates,
        "results": results,
        "common_results": common_results,
        "direct_available": [item for item in results if item.get("result") == "working without bypass"],
        "not_working": [item for item in results if "not working" in str(item.get("result") or "")],
        "domain_diagnostics": _domain_diagnostics_from_counts(diagnostic_counts, diagnostic_codes),
        "curl_diagnostics": curl_diagnostics,
        "curl_diagnostics_summary": _curl_summary(curl_diagnostics),
        "dominant_failure": _dominant_failure_from_counts(diagnostic_counts),
    }


def upsert_candidates(state_dir: Path, parsed: dict[str, Any], run: dict[str, Any]) -> int:
    now = now_iso()
    for raw in parsed.get("candidates") or []:
        if not isinstance(raw, dict):
            continue
        candidate_id = candidate_id_for(str(raw.get("protocol")), str(raw.get("args")))
        upsert_candidate_event(
            state_dir,
            candidate_id=candidate_id,
            protocol=str(raw.get("protocol") or ""),
            args=str(raw.get("args") or ""),
            status="candidate",
            run_id=str(run.get("id") or ""),
            domain=str(raw.get("domain") or ""),
            domains=[],
            test=str(raw.get("test") or ""),
            ip_version=str(raw.get("ip_version") or ""),
            seen_at=now,
            common=False,
        )
    for raw in parsed.get("common_candidates") or []:
        if not isinstance(raw, dict):
            continue
        candidate_id = candidate_id_for(str(raw.get("protocol")), str(raw.get("args")))
        upsert_candidate_event(
            state_dir,
            candidate_id=candidate_id,
            protocol=str(raw.get("protocol") or ""),
            args=str(raw.get("args") or ""),
            status="candidate",
            run_id=str(run.get("id") or ""),
            domain="",
            domains=[str(item or "") for item in run.get("domains", [])] if isinstance(run.get("domains"), list) else [],
            test=str(raw.get("test") or ""),
            ip_version=str(raw.get("ip_version") or ""),
            seen_at=now,
            common=True,
        )
    return candidate_total(state_dir)


def candidate_total(state_dir: Path) -> int:
    with connect(state_dir) as conn:
        return int(conn.execute("SELECT COUNT(*) AS count FROM strategies").fetchone()["count"])


def candidate_id_for(protocol: str, args: str) -> str:
    digest = hashlib.sha256(f"{protocol}\n{args}".encode("utf-8")).hexdigest()[:12]
    return f"{protocol}-{digest}"


class _LiveStdoutRecorder:
    def __init__(self, state_dir: Path, run: dict[str, Any]):
        self._lock = threading.Lock()
        self._state_dir = state_dir
        self._run = run
        progress_log = str(run.get("progress_log") or "")
        self._progress_log = Path(progress_log) if progress_log else None
        fallback_log = str(run.get("summary_fallback_log") or "")
        self._summary_fallback_log = Path(fallback_log) if fallback_log else None
        self._metrics = _RuntimeMetricsSampler(state_dir, run)
        self._last_progress_attempted = -1
        self._last_progress_written_at = 0.0
        self._eta_baseline_attempted = 0
        self._eta_baseline_elapsed_seconds: int | None = None
        self._section = ""
        self._current_script = ""
        self._phase = PHASE_CHECK_VPN
        self._pending_attempt: str | None = None
        self._attempted = 0
        self._attempts_by_script: dict[str, int] = {}
        self._attempt_times: deque[float] = deque(maxlen=ETA_SAMPLE_MAX_POINTS)
        self._summary_verified = 0
        self._summary_fallbacks = 0
        self._summary_common_seen = 0
        self._summary_line_count = 0
        self._common_line_count = 0
        self._result_count = 0
        self._common_result_count = 0
        self._direct_available_count = 0
        self._not_working_count = 0
        self._candidate_count = 0
        self._common_candidate_count = 0
        self._domain_status_counts: dict[str, dict[str, int]] = {}
        self._domain_code_counts: dict[str, dict[str, int]] = {}
        self._curl_code_counts: dict[str, int] = {}
        self._curl_diagnostics: list[dict[str, Any]] = []
        self._candidate_keys: set[tuple[str, str, str, str, str]] = set()
        self._common_candidate_keys: set[tuple[str, str, str, str, str]] = set()
        self._successful_strategy_keys: set[tuple[str, str]] = set()
        self._candidate_samples: list[dict[str, Any]] = []
        self._common_candidate_samples: list[dict[str, Any]] = []
        self._pending_candidate_events: list[dict[str, Any]] = []
        self._candidate_writer_queue: queue.Queue[list[dict[str, Any]] | object] = queue.Queue(
            maxsize=LIVE_CANDIDATE_QUEUE_MAX_BATCHES
        )
        self._candidate_writer: threading.Thread | None = None
        self._candidate_writer_closed = False
        self._candidate_writer_error: BaseException | None = None

    def record_line(self, raw_line: str) -> None:
        line = raw_line.strip()
        if not line:
            return
        with self._lock:
            self._record_line_locked(line)
            self._write_progress_locked()

    def parsed(self) -> dict[str, Any]:
        self.close()
        with self._lock:
            candidates = list(self._candidate_samples)
            common_candidates = list(self._common_candidate_samples)
            return {
                "summary": [],
                "common": [],
                "live_summary": [str(item.get("raw") or "") for item in candidates if item.get("raw")],
                "candidates": candidates,
                "common_candidates": common_candidates,
                "results": [],
                "common_results": [],
                "direct_available": [],
                "not_working": [],
                "summary_line_count": self._summary_line_count,
                "common_line_count": self._common_line_count,
                "result_count": self._result_count,
                "common_result_count": self._common_result_count,
                "direct_available_count": self._direct_available_count,
                "not_working_count": self._not_working_count,
                "candidate_count": self._candidate_count,
                "common_candidate_count": self._common_candidate_count,
                "domain_diagnostics": _domain_diagnostics_from_counts(
                    self._domain_status_counts,
                    self._domain_code_counts,
                ),
                "curl_diagnostics": list(self._curl_diagnostics),
                "curl_diagnostics_summary": dict(self._curl_code_counts),
                "dominant_failure": _dominant_failure_from_counts(self._domain_status_counts),
                "phase": self._phase,
                "summary_verified": self._summary_verified,
                "summary_fallbacks": self._summary_fallbacks,
                "summary_common_seen": self._summary_common_seen,
                "live_recorded": True,
            }

    def progress(self, run: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            progress = self._progress_locked(run)
            self._write_progress_locked(force=True, run=run)
            return progress

    def mark_phase(self, phase: str, run: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._phase = phase
            self._write_progress_locked(force=True, run=run or self._run)

    def close(self) -> None:
        writer: threading.Thread | None = None
        with self._lock:
            if not self._candidate_writer_closed:
                self._enqueue_pending_candidate_events_locked()
                self._candidate_writer_closed = True
                if self._candidate_writer is not None:
                    self._candidate_writer_queue.put(_CANDIDATE_WRITER_STOP)
                    writer = self._candidate_writer
            elif self._candidate_writer is not None and self._candidate_writer.is_alive():
                writer = self._candidate_writer
        if writer is not None:
            writer.join()
        if self._candidate_writer_error is not None:
            raise RuntimeError("live candidate writer failed") from self._candidate_writer_error

    def _record_line_locked(self, line: str) -> None:
        if line == "* SUMMARY":
            self._section = "summary"
            self._phase = PHASE_SUMMARY
            return
        if line == "* COMMON":
            self._section = "common"
            self._phase = PHASE_SUMMARY
            return
        self._phase = _phase_from_line(line, self._phase)
        script = _script_name_from_line(line)
        if script:
            self._current_script = script
            self._phase = PHASE_DISCOVERY
            self._attempts_by_script.setdefault(script, 0)
            return
        if _ATTEMPT_RE.match(line):
            self._attempted += 1
            self._phase = PHASE_DISCOVERY
            self._attempt_times.append(time.monotonic())
            self._attempts_by_script[self._current_script] = self._attempts_by_script.get(self._current_script, 0) + 1
            self._maybe_update_eta_baseline_locked()
        attempt = _live_attempt_line(line)
        if attempt:
            self._pending_attempt = attempt
            return
        if line == "!!!!! AVAILABLE !!!!!" and self._pending_attempt:
            candidate = _candidate_from_result_line(self._pending_attempt, scope="domain")
            if candidate:
                self._record_candidate_locked(candidate, common=False)
            self._pending_attempt = None
            return
        if line.startswith("UNAVAILABLE") or line.startswith("FAILED"):
            self._record_unavailable_locked(line)
            self._pending_attempt = None
            return
        live_success = _candidate_from_live_success_line(line)
        if live_success:
            self._record_candidate_locked(live_success, common=False)
            return
        if self._section in {"summary", "common"}:
            if self._section == "common":
                self._common_line_count += 1
            else:
                self._summary_line_count += 1
            parsed = _parse_result_line(line)
            if parsed:
                if self._section == "common":
                    self._common_result_count += 1
                else:
                    self._result_count += 1
                    if parsed.get("result") == "working without bypass":
                        self._direct_available_count += 1
                        self._record_domain_status_locked(
                            str(parsed.get("domain") or ""),
                            "direct_available",
                        )
                    if "not working" in str(parsed.get("result") or ""):
                        self._not_working_count += 1
                        self._record_domain_status_locked(
                            str(parsed.get("domain") or ""),
                            "needs_discovery",
                        )
            candidate = _candidate_from_result_line(line, scope=self._section)
            if candidate:
                self._record_summary_candidate_locked(candidate, common=self._section == "common")

    def _maybe_update_eta_baseline_locked(self) -> None:
        baseline_attempted = _eta_recalculation_attempts(self._attempted)
        if baseline_attempted <= 0 or baseline_attempted == self._eta_baseline_attempted:
            return
        self._eta_baseline_attempted = baseline_attempted
        self._eta_baseline_elapsed_seconds = _elapsed_seconds(self._run.get("started_at") or self._run.get("timestamp"))

    def _record_unavailable_locked(self, line: str) -> None:
        if not self._pending_attempt:
            return
        parsed = _parse_result_line(self._pending_attempt)
        if not parsed:
            return
        domain = str(parsed.get("domain") or "")
        if not domain:
            return
        code = _curl_code_from_line(line)
        test = str(parsed.get("test") or "")
        info = curl_failure_info(code, test=test, domain=domain)
        status = str(info.get("status") or "curl_error")
        self._record_domain_status_locked(domain, status)
        if code:
            self._curl_code_counts[code] = self._curl_code_counts.get(code, 0) + 1
            domain_codes = self._domain_code_counts.setdefault(domain, {})
            domain_codes[code] = domain_codes.get(code, 0) + 1
        if len(self._curl_diagnostics) < LIVE_CANDIDATE_SAMPLE_LIMIT:
            self._curl_diagnostics.append(
                {
                    "domain": domain,
                    "test": test,
                    "protocol": _protocol_from_test(test),
                    "code": code,
                    "status": status,
                    "label": info.get("label") or status,
                    "message": info.get("message") or "",
                    "strategy_failure": _is_strategy_failure(info),
                }
            )

    def _record_domain_status_locked(self, domain: str, status: str) -> None:
        if not domain:
            return
        counts = self._domain_status_counts.setdefault(domain, {})
        counts[status] = counts.get(status, 0) + 1

    def _record_summary_candidate_locked(self, candidate: dict[str, Any], common: bool) -> None:
        if common:
            self._summary_common_seen += 1
            return
        live_key = (
            "domain",
            str(candidate.get("test") or ""),
            str(candidate.get("ip_version") or ""),
            str(candidate.get("domain") or ""),
            str(candidate.get("args") or ""),
        )
        if live_key in self._candidate_keys:
            self._summary_verified += 1
            return
        fallback = {**candidate, "scope": "domain", "source": "summary_fallback"}
        self._summary_fallbacks += 1
        self._record_candidate_locked(fallback, common=False)
        if self._summary_fallback_log:
            append_jsonl(
                self._summary_fallback_log,
                {
                    "run_id": self._run["id"],
                    "seen_at": now_iso(),
                    "reason": "summary candidate was not recorded by live parser",
                    "candidate": fallback,
                },
            )

    def _record_candidate_locked(self, candidate: dict[str, Any], common: bool) -> None:
        key = (
            str(candidate.get("scope") or ""),
            str(candidate.get("test") or ""),
            str(candidate.get("ip_version") or ""),
            str(candidate.get("domain") or ""),
            str(candidate.get("args") or ""),
        )
        target = self._common_candidate_keys if common else self._candidate_keys
        if key in target:
            return
        target.add(key)
        if common:
            self._common_candidate_count += 1
            if len(self._common_candidate_samples) < LIVE_CANDIDATE_SAMPLE_LIMIT:
                self._common_candidate_samples.append(candidate)
        else:
            self._candidate_count += 1
            if len(self._candidate_samples) < LIVE_CANDIDATE_SAMPLE_LIMIT:
                self._candidate_samples.append(candidate)
        self._successful_strategy_keys.add((str(candidate.get("protocol") or ""), str(candidate.get("args") or "")))
        candidate_id = candidate_id_for(str(candidate.get("protocol") or ""), str(candidate.get("args") or ""))
        self._pending_candidate_events.append(
            {
                "candidate_id": candidate_id,
                "protocol": str(candidate.get("protocol") or ""),
                "args": str(candidate.get("args") or ""),
                "status": "candidate",
                "run_id": str(self._run.get("id") or ""),
                "domain": str(candidate.get("domain") or ""),
                "domains": (
                    [str(item or "") for item in self._run.get("domains", [])]
                    if common and isinstance(self._run.get("domains"), list)
                    else []
                ),
                "test": str(candidate.get("test") or ""),
                "ip_version": str(candidate.get("ip_version") or ""),
                "seen_at": now_iso(),
                "common": common,
            }
        )
        if len(self._pending_candidate_events) >= LIVE_CANDIDATE_FLUSH_SIZE:
            self._enqueue_pending_candidate_events_locked()

    def _progress_locked(self, run: dict[str, Any]) -> dict[str, Any]:
        successful = len(self._successful_strategy_keys)
        sample = _average_attempt_ms(self._attempt_times)
        return _progress_from_counts(
            run=run,
            attempted=self._attempted,
            attempts_by_script=dict(self._attempts_by_script),
            successful=successful,
            current_script=self._current_script,
            phase=self._phase,
            runtime_ms_per_attempt=sample,
            runtime_sample_count=len(self._attempt_times),
            summary_verified=self._summary_verified,
            summary_fallbacks=self._summary_fallbacks,
            eta_recalculation_attempts_override=self._eta_baseline_attempted,
            eta_elapsed_seconds_override=self._eta_baseline_elapsed_seconds,
        )

    def _write_progress_locked(self, force: bool = False, run: dict[str, Any] | None = None) -> None:
        if not self._progress_log:
            return
        now = time.monotonic()
        attempt_delta = self._attempted - self._last_progress_attempted
        if not force and attempt_delta < _eta_recalculation_step(self._attempted) and now - self._last_progress_written_at < 2.0:
            return
        progress = self._progress_locked(run or self._run)
        tmp = self._progress_log.with_suffix(".json.tmp")
        try:
            self._progress_log.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(progress, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
            tmp.replace(self._progress_log)
            self._last_progress_attempted = self._attempted
            self._last_progress_written_at = now
            self._metrics.maybe_write(progress)
        except OSError:
            return

    def _enqueue_pending_candidate_events_locked(self) -> None:
        if not self._pending_candidate_events:
            return
        if self._candidate_writer_closed:
            raise RuntimeError("live candidate writer is already closed")
        if self._candidate_writer_error is not None:
            raise RuntimeError("live candidate writer failed") from self._candidate_writer_error
        self._ensure_candidate_writer_locked()
        events = self._pending_candidate_events
        self._pending_candidate_events = []
        self._candidate_writer_queue.put(events)

    def _ensure_candidate_writer_locked(self) -> None:
        if self._candidate_writer is not None and self._candidate_writer.is_alive():
            return
        if self._candidate_writer_error is not None:
            raise RuntimeError("live candidate writer failed") from self._candidate_writer_error
        self._candidate_writer = threading.Thread(
            target=self._candidate_writer_loop,
            name=f"gp-live-candidate-writer-{self._run.get('id') or 'run'}",
            daemon=True,
        )
        self._candidate_writer.start()

    def _candidate_writer_loop(self) -> None:
        try:
            with connect(self._state_dir) as conn:
                while True:
                    item = self._candidate_writer_queue.get()
                    if item is _CANDIDATE_WRITER_STOP:
                        return
                    events = item
                    if not isinstance(events, list):
                        continue
                    for event in events:
                        upsert_candidate_event_conn(conn, **event)
                    conn.commit()
        except BaseException as exc:  # pragma: no cover - covered through close()
            self._candidate_writer_error = exc


class _RuntimeMetricsSampler:
    def __init__(self, state_dir: Path, run: dict[str, Any]):
        self._state_dir = state_dir
        self._run = run
        metrics_log = str(run.get("metrics_log") or "")
        self._path = Path(metrics_log) if metrics_log else None
        self._last_written_at = 0.0
        self._last_cpu: tuple[int, int, int] | None = None

    def maybe_write(self, progress: dict[str, Any]) -> None:
        if not self._path:
            return
        now = time.monotonic()
        if now - self._last_written_at < METRICS_INTERVAL_SECONDS:
            return
        self._last_written_at = now
        payload = {
            "timestamp": now_iso(),
            "run_id": self._run.get("id"),
            "phase": progress.get("phase"),
            "phase_label": progress.get("phase_label"),
            "current_script": progress.get("current_script"),
            "attempted": progress.get("attempted"),
            "attempt_total": progress.get("attempt_total"),
            "remaining_attempts": progress.get("remaining_attempts"),
            "successful": progress.get("successful"),
            "eta_seconds": progress.get("eta_seconds"),
            "eta_status": progress.get("eta_status"),
            "progress_status": progress.get("progress_status"),
            "processes": _process_counts(),
            "system": self._system_metrics(),
            "files": _runtime_file_sizes(self._state_dir, self._run),
        }
        try:
            _rotate_metrics_file(self._path)
            append_jsonl(self._path, payload)
        except OSError:
            return

    def _system_metrics(self) -> dict[str, Any]:
        return {
            "loadavg": _loadavg(),
            "cpu_percent": self._cpu_percent(),
            "memory": _meminfo(),
        }

    def _cpu_percent(self) -> dict[str, float] | None:
        current = _read_cpu_totals()
        if current is None:
            return None
        previous = self._last_cpu
        self._last_cpu = current
        if previous is None:
            return None
        total, idle, iowait = current
        prev_total, prev_idle, prev_iowait = previous
        delta_total = total - prev_total
        if delta_total <= 0:
            return None
        busy = max(0, delta_total - (idle - prev_idle))
        iowait_delta = max(0, iowait - prev_iowait)
        return {
            "busy": round((busy / delta_total) * 100.0, 1),
            "iowait": round((iowait_delta / delta_total) * 100.0, 1),
        }


def _rotate_metrics_file(path: Path) -> None:
    if not path.exists() or path.stat().st_size <= METRICS_MAX_BYTES:
        return
    rotated = path.with_suffix(path.suffix + ".1")
    try:
        if rotated.exists():
            rotated.unlink()
        path.replace(rotated)
    except OSError:
        return


def _runtime_file_sizes(state_dir: Path, run: dict[str, Any]) -> dict[str, int]:
    root = _finder_dir(state_dir)
    paths = {
        "stdout_log": Path(str(run.get("stdout_log") or "")),
        "stderr_log": Path(str(run.get("stderr_log") or "")),
        "progress_log": Path(str(run.get("progress_log") or "")),
        "sqlite": root / "state.sqlite3",
        "sqlite_wal": root / "state.sqlite3-wal",
    }
    return {name: _file_size(path) for name, path in paths.items()}


def _file_size(path: Path) -> int:
    try:
        return int(path.stat().st_size) if path.is_file() else 0
    except OSError:
        return 0


class _RotatingTextWriter:
    def __init__(self, path: Path, max_bytes: int):
        self._path = path
        self._max_bytes = max(1024, int(max_bytes))
        self._handle: Any | None = None

    def __enter__(self) -> "_RotatingTextWriter":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("w", encoding="utf-8")
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def write(self, text: str) -> None:
        if self._handle is None:
            raise ValueError("writer is closed")
        self._handle.write(text)
        self._handle.flush()
        self._rotate_if_needed()

    def flush(self) -> None:
        if self._handle is not None:
            self._handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def _rotate_if_needed(self) -> None:
        try:
            if not self._path.is_file() or self._path.stat().st_size <= self._max_bytes:
                return
        except OSError:
            return
        self.close()
        rotated = self._path.with_suffix(self._path.suffix + ".1")
        try:
            if rotated.exists():
                rotated.unlink()
            self._path.replace(rotated)
            self._handle = self._path.open("w", encoding="utf-8")
            self._handle.write(f"# log rotated, previous chunk: {rotated.name}\n")
            self._handle.flush()
        except OSError:
            self._handle = self._path.open("a", encoding="utf-8")


def _loadavg() -> list[float]:
    try:
        return [round(float(item), 2) for item in os.getloadavg()]
    except (AttributeError, OSError):
        return []


def _meminfo() -> dict[str, int]:
    path = Path("/proc/meminfo")
    if not path.is_file():
        return {}
    wanted = {"MemTotal", "MemAvailable", "MemFree", "Buffers", "Cached", "SwapTotal", "SwapFree"}
    result: dict[str, int] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            key, _, raw_value = line.partition(":")
            if key not in wanted:
                continue
            parts = raw_value.strip().split()
            if parts:
                result[key] = int(parts[0])
    except (OSError, ValueError):
        return {}
    return result


def _read_cpu_totals() -> tuple[int, int, int] | None:
    path = Path("/proc/stat")
    if not path.is_file():
        return None
    try:
        first = path.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
        parts = [int(item) for item in first.split()[1:]]
    except (OSError, IndexError, ValueError):
        return None
    if len(parts) < 5:
        return None
    idle = parts[3] + parts[4]
    iowait = parts[4]
    return (sum(parts), idle, iowait)


def _process_counts() -> dict[str, int]:
    proc = Path("/proc")
    if not proc.is_dir():
        return {"curl": 0, "nfqws2": 0, "blockcheck2": 0}
    counts = {"curl": 0, "nfqws2": 0, "blockcheck2": 0}
    for child in proc.iterdir():
        if not child.name.isdigit():
            continue
        try:
            cmdline = (child / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="ignore")
        except OSError:
            continue
        if not cmdline:
            continue
        if "curl" in cmdline:
            counts["curl"] += 1
        if "nfqws2" in cmdline:
            counts["nfqws2"] += 1
        if "blockcheck2" in cmdline:
            counts["blockcheck2"] += 1
    return counts


class _CompactStdoutWriter:
    def __init__(self, handle: Any):
        self._handle = handle
        self._pending_attempt = ""
        self._attempt_lines = 0
        self._summary_lines = 0
        self._section = ""

    def write(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return
        if stripped in {"* SUMMARY", "* COMMON"}:
            self._flush_attempt_counter()
            self._section = stripped.removeprefix("* ").lower()
            self._write(line)
            return
        script = _script_name_from_line(stripped)
        if script:
            self._flush_attempt_counter()
            self._section = ""
            self._write(line)
            return
        attempt = _live_attempt_line(stripped)
        if attempt:
            self._pending_attempt = attempt
            self._attempt_lines += 1
            if self._attempt_lines % 1000 == 0:
                self._write(f"# compact-log: skipped {self._attempt_lines} attempt lines\n")
            return
        if stripped == "!!!!! AVAILABLE !!!!!":
            if self._pending_attempt:
                self._write(self._pending_attempt + "\n")
            self._write(line)
            self._pending_attempt = ""
            return
        if _candidate_from_live_success_line(stripped):
            self._write(line)
            return
        if stripped.startswith("UNAVAILABLE") or stripped.startswith("FAILED"):
            self._pending_attempt = ""
            return
        if self._section in {"summary", "common"}:
            self._summary_lines += 1
            if self._summary_lines % 1000 == 0:
                self._write(f"# compact-log: skipped {self._summary_lines} summary/common lines\n")
            return
        if stripped.startswith("* "):
            self._write(line)

    def close(self) -> None:
        self._flush_attempt_counter()

    def _flush_attempt_counter(self) -> None:
        if self._attempt_lines:
            self._write(f"# compact-log: total attempt lines skipped {self._attempt_lines}\n")
            self._attempt_lines = 0
        self._pending_attempt = ""

    def _write(self, line: str) -> None:
        self._handle.write(line)
        self._handle.flush()


def _stdout_log_mode(env: dict[str, str]) -> str:
    if _truthy(env.get("GP_DEBUG_STDOUT"), default=False):
        return "debug"
    if _truthy(env.get("GP_COMPACT_STDOUT"), default=False):
        return "compact"
    return "raw"


def _set_debug_stdout_env(env: dict[str, str], debug_stdout: bool | None) -> None:
    if debug_stdout is True:
        env["GP_DEBUG_STDOUT"] = "1"
    elif debug_stdout is False:
        env.pop("GP_DEBUG_STDOUT", None)


def stop_active_blockcheck_runtime() -> None:
    _cleanup_blockcheck_processes()
    _cleanup_nft_blockcheck_tables()


def _run_process_with_live_stdout(
    command: list[str],
    env: dict[str, str],
    stdout_log: Path,
    stderr_log: Path,
    debug_stdout_log: Path | None,
    timeout_seconds: int,
    stop_event: threading.Event | None,
    recorder: _LiveStdoutRecorder,
) -> dict[str, Any]:
    status = "success"
    returncode: int | None = None
    timed_out = False
    stopped = False
    reader_errors: list[BaseException] = []

    stdout_mode = _stdout_log_mode(env)
    debug_writer = _RotatingTextWriter(debug_stdout_log, DEBUG_STDOUT_LOG_MAX_BYTES) if debug_stdout_log and stdout_mode == "debug" else None
    debug_handle = debug_writer.__enter__() if debug_writer else None
    try:
        with _RotatingTextWriter(stdout_log, STDOUT_LOG_MAX_BYTES) as stdout_handle, stderr_log.open("w", encoding="utf-8") as stderr_handle:
            compact_writer = _CompactStdoutWriter(stdout_handle)
            process = subprocess.Popen(
                command,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=stderr_handle,
                env=env,
                start_new_session=hasattr(os, "setsid"),
            )

            def read_stdout() -> None:
                try:
                    if process.stdout is None:
                        return
                    for line in process.stdout:
                        if stdout_mode == "compact":
                            compact_writer.write(line)
                        else:
                            stdout_handle.write(line)
                            stdout_handle.flush()
                        if debug_handle:
                            debug_handle.write(line)
                        recorder.record_line(line)
                except BaseException as exc:  # noqa: BLE001
                    reader_errors.append(exc)
                finally:
                    if process.stdout is not None:
                        process.stdout.close()
                    compact_writer.close()
                    if debug_handle:
                        debug_handle.flush()

            reader = threading.Thread(target=read_stdout, daemon=True)
            reader.start()
            deadline = None if timeout_seconds <= 0 else time.monotonic() + timeout_seconds
            while True:
                if stop_event is not None and stop_event.is_set():
                    stopped = True
                    status = "stopped"
                    _stop_process_group(process)
                    _cleanup_blockcheck_processes()
                    _cleanup_nft_blockcheck_tables()
                    recorder.mark_phase(PHASE_SAVING)
                    returncode = _wait_process_after_stop(process)
                    break
                wait_timeout = 1.0
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        status = "timeout"
                        _stop_process_group(process)
                        _cleanup_blockcheck_processes()
                        _cleanup_nft_blockcheck_tables()
                        recorder.mark_phase(PHASE_SAVING)
                        returncode = _wait_process_after_stop(process)
                        break
                    wait_timeout = min(1.0, remaining)
                try:
                    returncode = process.wait(timeout=wait_timeout)
                    if stop_event is not None and stop_event.is_set():
                        stopped = True
                        status = "stopped"
                        _cleanup_blockcheck_processes()
                        _cleanup_nft_blockcheck_tables()
                        recorder.mark_phase(PHASE_SAVING)
                        break
                    if returncode != 0:
                        status = "failed"
                    break
                except subprocess.TimeoutExpired:
                    continue
            reader.join(timeout=5)
    finally:
        if debug_writer:
            debug_writer.__exit__(None, None, None)

    if reader_errors:
        raise RuntimeError(f"failed to read blockcheck stdout: {reader_errors[0]}")
    return {
        "status": status,
        "returncode": returncode,
        "timed_out": timed_out,
        "stopped": stopped,
    }


def _wait_process_after_stop(process: subprocess.Popen[str], timeout_seconds: float = 5.0) -> int | None:
    if process.returncode is not None:
        return process.returncode
    try:
        return process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _stop_process_group(process)
        try:
            return process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            return process.returncode


def _run_blockcheck_live(
    state_dir: Path,
    kind: str,
    domains: list[str],
    timeout_seconds: int,
    test: str,
    options: DiscoveryOptions,
    candidate_id: str = "",
    domain_validation: dict[str, Any] | None = None,
    debug_stdout: bool | None = None,
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    blockcheck = shutil.which("blockcheck2.sh") or shutil.which("blockcheck.sh")
    if not blockcheck:
        raise RuntimeError("blockcheck2.sh/blockcheck.sh not found in PATH")
    options = options.normalized()
    domain_validation = domain_validation or validate_domain_inputs(domains, default_to_critical=True)
    clean_domains = list(domain_validation["domains"])
    if not clean_domains:
        raise ValueError("no valid domains to check")
    validation_fields = _domain_validation_run_fields(domain_validation)
    blockcheck_path = _resolve_blockcheck_script(Path(blockcheck))
    full_env = os.environ.copy()
    full_env.update(
        {
            "BATCH": "1",
            "DOMAINS": " ".join(clean_domains),
            "IPVS": _ipvs_value(options),
            "TEST": test,
            **options.to_blockcheck_env(),
        }
    )
    _set_debug_stdout_env(full_env, debug_stdout)
    command = root_command([str(blockcheck_path)], env=full_env, pass_env_keys=BLOCKCHECK_ENV_KEYS)

    root = _finder_dir(state_dir)
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    _cleanup_old_strategy_logs(logs)
    run_id = f"{now_iso().replace(':', '').replace('-', '')}-{uuid.uuid4().hex[:8]}"
    stdout_log = logs / f"{run_id}.{kind}.stdout.log"
    stderr_log = logs / f"{run_id}.{kind}.stderr.log"
    progress_log = logs / f"{run_id}.{kind}.progress.json"
    metrics_log = logs / f"{run_id}.{kind}.metrics.ndjson"
    summary_fallback_log = logs / f"{run_id}.{kind}.summary-fallback.ndjson"
    debug_stdout_log = logs / f"{run_id}.{kind}.debug.stdout.log"
    attempt_plan = _standard_attempt_plan(
        domains=clean_domains,
        test=test,
        enable_http=options.enable_http,
        enable_tls=options.enable_tls12,
        enable_tls13=options.enable_tls13,
        enable_quic=options.enable_quic,
        enable_ipv6=options.enable_ipv6,
    )
    option_fields = options.to_run_fields()
    started_at = now_iso()
    started = {
        "id": run_id,
        "kind": kind,
        "candidate_id": candidate_id,
        "status": "running",
        "timestamp": started_at,
        "started_at": started_at,
        "domains": clean_domains,
        "returncode": None,
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "progress_log": str(progress_log),
        "metrics_log": str(metrics_log),
        "summary_fallback_log": str(summary_fallback_log),
        "debug_stdout_log": str(debug_stdout_log),
        "stdout_log_mode": _stdout_log_mode(full_env),
        "debug_stdout": _stdout_log_mode(full_env) == "debug",
        "candidate_count": 0,
        "phase": PHASE_CHECK_VPN,
        "test": test,
        **option_fields,
        **validation_fields,
        "attempt_plan": attempt_plan,
    }
    append_run(state_dir, started)

    recorder = _LiveStdoutRecorder(state_dir, started)
    process_result = _run_process_with_live_stdout(
        command=command,
        env=full_env,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        debug_stdout_log=debug_stdout_log,
        timeout_seconds=timeout_seconds,
        stop_event=stop_event,
        recorder=recorder,
    )
    parsed = recorder.parsed()
    completed_at = now_iso()
    run = {
        "id": run_id,
        "kind": kind,
        "candidate_id": candidate_id,
        "status": process_result["status"],
        "timestamp": started_at,
        "started_at": started_at,
        "completed_at": completed_at,
        "domains": clean_domains,
        "returncode": process_result["returncode"],
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "progress_log": str(progress_log),
        "metrics_log": str(metrics_log),
        "summary_fallback_log": str(summary_fallback_log),
        "debug_stdout_log": str(debug_stdout_log),
        "stdout_log_mode": _stdout_log_mode(full_env),
        "debug_stdout": _stdout_log_mode(full_env) == "debug",
        "candidate_count": int(parsed.get("candidate_count") or len(parsed["candidates"])),
        "common_candidate_count": int(parsed.get("common_candidate_count") or len(parsed["common_candidates"])),
        "summary_line_count": int(parsed.get("summary_line_count") or 0),
        "common_line_count": int(parsed.get("common_line_count") or 0),
        "result_count": int(parsed.get("result_count") or 0),
        "common_result_count": int(parsed.get("common_result_count") or 0),
        "direct_available_count": int(parsed.get("direct_available_count") or 0),
        "not_working_count": int(parsed.get("not_working_count") or 0),
        "phase": parsed.get("phase") or PHASE_COMPLETE,
        "domain_diagnostics": parsed.get("domain_diagnostics") or [],
        "curl_diagnostics": parsed.get("curl_diagnostics") or [],
        "curl_diagnostics_summary": parsed.get("curl_diagnostics_summary") or {},
        "dominant_failure": parsed.get("dominant_failure") or {},
        "summary_verified": parsed.get("summary_verified", 0),
        "summary_fallbacks": parsed.get("summary_fallbacks", 0),
        "summary_common_seen": parsed.get("summary_common_seen", 0),
        "timed_out": process_result["timed_out"],
        "stopped": process_result["stopped"],
        "timeout_seconds": timeout_seconds,
        "test": test,
        **option_fields,
        **validation_fields,
        "attempt_plan": attempt_plan,
    }
    run["progress"] = recorder.progress(run)
    if kind in {"standard-discovery", "multi-domain-discovery"}:
        run["total_candidates"] = candidate_total(state_dir) if parsed.get("live_recorded") else upsert_candidates(state_dir, parsed, run)
    recorder.close()
    append_run(state_dir, run)
    return run


def _run_multidomain_blockcheck_live(
    state_dir: Path,
    domains: list[str],
    timeout_seconds: int,
    options: DiscoveryOptions,
    curl_parallelism: int,
    domain_validation: dict[str, Any] | None = None,
    debug_stdout: bool | None = None,
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    blockcheck = shutil.which("blockcheck2.sh") or shutil.which("blockcheck.sh")
    if not blockcheck:
        raise RuntimeError("blockcheck2.sh/blockcheck.sh not found in PATH")
    options = options.normalized()
    domain_validation = domain_validation or validate_domain_inputs(domains, default_to_critical=True)
    clean_domains = list(domain_validation["domains"])
    if not clean_domains:
        raise ValueError("no valid domains to check")
    blockcheck_path = _resolve_blockcheck_script(Path(blockcheck))
    zapret_base = blockcheck_path.parent
    normalized_parallelism = _minimum_int(curl_parallelism, default=4, minimum=1)

    full_env = os.environ.copy()
    full_env.update(
        {
            "BATCH": "1",
            "DOMAINS": " ".join(clean_domains),
            "IPVS": _ipvs_value(options),
            "TEST": "standard",
            **options.to_blockcheck_env(),
            "GP_MD_CURL_PARALLELISM": str(normalized_parallelism),
            "ZAPRET_BASE": str(zapret_base),
            "ZAPRET_RW": str(zapret_base),
        }
    )
    _set_debug_stdout_env(full_env, debug_stdout)
    return _run_blockcheck_command_live(
        command=root_command(
            [str(blockcheck_path)],
            env=full_env,
            pass_env_keys=BLOCKCHECK_ENV_KEYS,
            helper_command="run-multidomain",
        ),
        env=full_env,
        state_dir=state_dir,
        kind="multi-domain-discovery",
        domains=clean_domains,
        timeout_seconds=timeout_seconds,
        test="standard",
        options=options,
        curl_parallelism=normalized_parallelism,
        domain_validation=domain_validation,
        debug_stdout=debug_stdout,
        stop_event=stop_event,
    )


def _run_blockcheck_command_live(
    command: list[str],
    env: dict[str, str],
    state_dir: Path,
    kind: str,
    domains: list[str],
    timeout_seconds: int,
    test: str,
    options: DiscoveryOptions,
    curl_parallelism: int | None = None,
    candidate_id: str = "",
    domain_validation: dict[str, Any] | None = None,
    debug_stdout: bool | None = None,
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    options = options.normalized()
    domain_validation = domain_validation or validate_domain_inputs(domains, default_to_critical=True)
    domains = list(domain_validation["domains"])
    if not domains:
        raise ValueError("no valid domains to check")
    validation_fields = _domain_validation_run_fields(domain_validation)
    _set_debug_stdout_env(env, debug_stdout)
    root = _finder_dir(state_dir)
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    run_id = f"{now_iso().replace(':', '').replace('-', '')}-{uuid.uuid4().hex[:8]}"
    stdout_log = logs / f"{run_id}.{kind}.stdout.log"
    stderr_log = logs / f"{run_id}.{kind}.stderr.log"
    progress_log = logs / f"{run_id}.{kind}.progress.json"
    metrics_log = logs / f"{run_id}.{kind}.metrics.ndjson"
    summary_fallback_log = logs / f"{run_id}.{kind}.summary-fallback.ndjson"
    debug_stdout_log = logs / f"{run_id}.{kind}.debug.stdout.log"
    attempt_plan = _standard_attempt_plan(
        domains=domains,
        test=test,
        enable_http=options.enable_http,
        enable_tls=options.enable_tls12,
        enable_tls13=options.enable_tls13,
        enable_quic=options.enable_quic,
        enable_ipv6=options.enable_ipv6,
    )
    option_fields = options.to_run_fields()
    started_at = now_iso()
    started = {
        "id": run_id,
        "kind": kind,
        "candidate_id": candidate_id,
        "status": "running",
        "timestamp": started_at,
        "started_at": started_at,
        "domains": domains,
        "returncode": None,
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "progress_log": str(progress_log),
        "metrics_log": str(metrics_log),
        "summary_fallback_log": str(summary_fallback_log),
        "debug_stdout_log": str(debug_stdout_log),
        "stdout_log_mode": _stdout_log_mode(env),
        "debug_stdout": _stdout_log_mode(env) == "debug",
        "candidate_count": 0,
        "phase": PHASE_CHECK_VPN,
        "test": test,
        **option_fields,
        "curl_parallelism": curl_parallelism,
        **validation_fields,
        "attempt_plan": attempt_plan,
    }
    append_run(state_dir, started)

    recorder = _LiveStdoutRecorder(state_dir, started)
    process_result = _run_process_with_live_stdout(
        command=command,
        env=env,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        debug_stdout_log=debug_stdout_log,
        timeout_seconds=timeout_seconds,
        stop_event=stop_event,
        recorder=recorder,
    )
    parsed = recorder.parsed()
    completed_at = now_iso()
    run = {
        "id": run_id,
        "kind": kind,
        "candidate_id": candidate_id,
        "status": process_result["status"],
        "timestamp": started_at,
        "started_at": started_at,
        "completed_at": completed_at,
        "domains": domains,
        "returncode": process_result["returncode"],
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "progress_log": str(progress_log),
        "metrics_log": str(metrics_log),
        "summary_fallback_log": str(summary_fallback_log),
        "debug_stdout_log": str(debug_stdout_log),
        "stdout_log_mode": _stdout_log_mode(env),
        "debug_stdout": _stdout_log_mode(env) == "debug",
        "candidate_count": int(parsed.get("candidate_count") or len(parsed["candidates"])),
        "common_candidate_count": int(parsed.get("common_candidate_count") or len(parsed["common_candidates"])),
        "summary_line_count": int(parsed.get("summary_line_count") or 0),
        "common_line_count": int(parsed.get("common_line_count") or 0),
        "result_count": int(parsed.get("result_count") or 0),
        "common_result_count": int(parsed.get("common_result_count") or 0),
        "direct_available_count": int(parsed.get("direct_available_count") or 0),
        "not_working_count": int(parsed.get("not_working_count") or 0),
        "phase": parsed.get("phase") or PHASE_COMPLETE,
        "domain_diagnostics": parsed.get("domain_diagnostics") or [],
        "curl_diagnostics": parsed.get("curl_diagnostics") or [],
        "curl_diagnostics_summary": parsed.get("curl_diagnostics_summary") or {},
        "dominant_failure": parsed.get("dominant_failure") or {},
        "summary_verified": parsed.get("summary_verified", 0),
        "summary_fallbacks": parsed.get("summary_fallbacks", 0),
        "summary_common_seen": parsed.get("summary_common_seen", 0),
        "timed_out": process_result["timed_out"],
        "stopped": process_result["stopped"],
        "timeout_seconds": timeout_seconds,
        "test": test,
        **option_fields,
        "curl_parallelism": curl_parallelism,
        **validation_fields,
        "attempt_plan": attempt_plan,
    }
    run["progress"] = recorder.progress(run)
    if kind in {"standard-discovery", "multi-domain-discovery"}:
        run["total_candidates"] = candidate_total(state_dir) if parsed.get("live_recorded") else upsert_candidates(state_dir, parsed, run)
    recorder.close()
    append_run(state_dir, run)
    return run


def progress_from_stdout(stdout: str, run: dict[str, Any]) -> dict[str, Any]:
    lines = stdout.splitlines()
    attempted = sum(1 for line in lines if _ATTEMPT_RE.match(line.strip()))
    attempts_by_script = _attempts_by_script(lines)
    parsed = parse_blockcheck_stdout(stdout)
    successful = len(
        {
            (str(item.get("protocol") or ""), str(item.get("args") or ""))
            for item in [*parsed["candidates"], *parsed["common_candidates"]]
        }
    )
    scripts = [_script_name_from_line(line) for line in lines if _script_name_from_line(line)]
    current_script = scripts[-1] if scripts else ""
    phase = PHASE_SUMMARY if any(line.strip() in {"* SUMMARY", "* COMMON"} for line in lines) else (PHASE_DISCOVERY if current_script else PHASE_CHECK_VPN)
    return _progress_from_counts(
        run=run,
        attempted=attempted,
        attempts_by_script=attempts_by_script,
        successful=successful,
        current_script=current_script,
        phase=phase,
    )


def _progress_from_counts(
    *,
    run: dict[str, Any],
    attempted: int,
    attempts_by_script: dict[str, int],
    successful: int,
    current_script: str,
    phase: str = PHASE_CHECK_VPN,
    runtime_ms_per_attempt: int | None = None,
    runtime_sample_count: int | None = None,
    summary_verified: int = 0,
    summary_fallbacks: int = 0,
    elapsed_seconds_override: int | None = None,
    eta_recalculation_attempts_override: int | None = None,
    eta_elapsed_seconds_override: int | None = None,
) -> dict[str, Any]:
    attempt_plan = _attempt_plan_for_run(run, current_script)
    script_order = [str(item) for item in attempt_plan.get("script_order") or []]
    script_attempt_totals = attempt_plan.get("scripts") if isinstance(attempt_plan.get("scripts"), dict) else {}
    attempt_total = int(attempt_plan.get("total") or 0)
    strategy_progress = _strategy_progress_from_attempts(attempt_plan, attempts_by_script, current_script)
    current_script_attempted = attempts_by_script.get(current_script, 0)
    current_script_attempt_total = int(script_attempt_totals.get(current_script) or 0)
    script_total = len(script_order) if script_order else (_standard_script_total() if current_script.startswith("standard/") else 0)
    script_index = _standard_script_index(current_script, script_order) if current_script else 0
    if script_total and script_index > script_total:
        script_index = script_total
    status = str(run.get("status") or "")
    finished = status in {"success", "failed", "timeout", "stopped"}
    completed = status == "success"
    if finished and phase == PHASE_SAVING:
        phase = PHASE_COMPLETE
    if completed and script_total:
        script_index = script_total
    progress_status = "unknown"
    effective_attempt_total = attempt_total
    remaining_attempts = max(0, attempt_total - attempted) if attempt_total else None
    if finished:
        remaining_attempts = None
    if attempt_total:
        progress_status = "exact" if str(attempt_plan.get("source") or "") in {"shell", "test"} else "estimated"
    current_script_underestimated = bool(
        attempt_total
        and current_script_attempt_total
        and current_script_attempted > current_script_attempt_total
    )
    if attempt_total and not finished and (attempted >= attempt_total or current_script_underestimated):
        progress_status = "underestimated"
        if current_script_underestimated and attempted < attempt_total:
            remaining_attempts = None
            effective_attempt_total = attempt_total
        else:
            script_remaining = current_script_attempt_total - current_script_attempted if current_script_attempt_total else 0
            if script_remaining > 0:
                remaining_attempts = script_remaining
                effective_attempt_total = attempted + script_remaining
            else:
                remaining_attempts = None
                effective_attempt_total = attempted
    if effective_attempt_total:
        if completed:
            percent = 100.0
        elif remaining_attempts is None and progress_status == "underestimated":
            if effective_attempt_total and attempted < effective_attempt_total:
                percent = min(99.0, (attempted / effective_attempt_total) * 100.0)
            else:
                percent = 99.0
        else:
            percent = min(99.9, (attempted / effective_attempt_total) * 100.0)
    else:
        percent = (script_index / script_total * 100.0) if script_total else None
    elapsed = (
        elapsed_seconds_override
        if elapsed_seconds_override is not None
        else _elapsed_seconds(run.get("started_at") or run.get("timestamp"))
    )
    eta_parallelism = 1
    eta_configured_parallelism = _eta_parallelism_for_run(run)
    eta_recalculation_step = _eta_recalculation_step(attempted)
    eta_recalculation_attempts = (
        eta_recalculation_attempts_override
        if eta_recalculation_attempts_override is not None
        else attempted
    )
    eta_elapsed = eta_elapsed_seconds_override if eta_elapsed_seconds_override is not None else elapsed
    eta_ms_per_attempt = _elapsed_average_ms_per_attempt(eta_elapsed, eta_recalculation_attempts)
    estimate_ms_per_attempt = eta_ms_per_attempt or 0
    eta_status = "elapsed_average" if eta_ms_per_attempt else "calculating"
    eta_method = "elapsed_average" if eta_ms_per_attempt else "waiting_for_attempts"
    if finished and not completed:
        eta = None
        eta_status = status or "finished"
        eta_method = "finished"
    elif remaining_attempts is None and not completed:
        eta = None
        if progress_status == "underestimated":
            eta_status = "underestimated"
    elif completed:
        eta = 0
        eta_status = "complete"
        eta_method = "complete"
    elif eta_status == "calculating":
        eta = None
    else:
        eta = _eta_from_remaining_attempts(remaining_attempts, completed, eta_parallelism, eta_ms_per_attempt)
    return {
        "attempted": attempted,
        "attempt_total": attempt_total,
        "effective_attempt_total": effective_attempt_total,
        "remaining_attempts": remaining_attempts,
        "successful": successful,
        "strategy_checked": strategy_progress["checked"],
        "strategy_total": strategy_progress["total"],
        "current_script_strategy_checked": strategy_progress["current_script_checked"],
        "current_script_strategy_total": strategy_progress["current_script_total"],
        "current_script": current_script,
        "current_script_attempted": current_script_attempted,
        "current_script_attempt_total": current_script_attempt_total,
        "script_index": script_index,
        "script_total": script_total,
        "percent": percent,
        "elapsed_seconds": elapsed,
        "eta_seconds": eta,
        "eta_estimate_ms_per_attempt": estimate_ms_per_attempt,
        "eta_ms_per_attempt": eta_ms_per_attempt,
        "eta_status": eta_status,
        "eta_parallelism": eta_parallelism,
        "eta_configured_parallelism": eta_configured_parallelism,
        "eta_method": eta_method,
        "eta_sample_count": runtime_sample_count or 0,
        "eta_sample_window": ETA_SAMPLE_MAX_POINTS - 1,
        "eta_recalculation_step": eta_recalculation_step,
        "eta_recalculation_attempts": eta_recalculation_attempts,
        "eta_elapsed_seconds": eta_elapsed,
        "repeats": _bounded_int(run.get("repeats"), default=1, minimum=1, maximum=10),
        "repeat_parallel": _truthy(run.get("repeat_parallel"), default=False),
        "attempt_plan_source": attempt_plan.get("source") or "",
        "progress_status": progress_status,
        "phase": phase,
        "phase_label": _phase_label(phase),
        "summary_verified": summary_verified,
        "summary_fallbacks": summary_fallbacks,
    }


def _strategy_progress_from_attempts(
    attempt_plan: dict[str, Any],
    attempts_by_script: dict[str, int],
    current_script: str,
) -> dict[str, int]:
    script_order = [str(item) for item in attempt_plan.get("script_order") or []]
    script_attempt_totals = attempt_plan.get("scripts") if isinstance(attempt_plan.get("scripts"), dict) else {}
    raw_strategy_scripts = attempt_plan.get("strategy_scripts") if isinstance(attempt_plan.get("strategy_scripts"), dict) else {}
    domain_count = max(1, int(attempt_plan.get("domain_count") or 0))
    ip_version_count = max(1, int(attempt_plan.get("ip_version_count") or 1))
    default_attempts_per_strategy = max(1, domain_count * ip_version_count)
    strategy_scripts: dict[str, int] = {}
    for script in script_order:
        raw_total = int(raw_strategy_scripts.get(script) or 0)
        if raw_total <= 0:
            raw_total = int(script_attempt_totals.get(script) or 0) // default_attempts_per_strategy
        strategy_scripts[script] = max(0, raw_total)
    strategy_total = int(attempt_plan.get("strategy_total") or sum(strategy_scripts.values()))
    checked = 0
    current_checked = 0
    current_total = strategy_scripts.get(current_script, 0)
    for script in script_order:
        script_strategy_total = strategy_scripts.get(script, 0)
        if script_strategy_total <= 0:
            continue
        script_attempt_total = int(script_attempt_totals.get(script) or 0)
        attempts_per_strategy = max(1, script_attempt_total // script_strategy_total) if script_attempt_total else default_attempts_per_strategy
        script_checked = min(script_strategy_total, int(attempts_by_script.get(script, 0)) // attempts_per_strategy)
        if script == current_script:
            current_checked = script_checked
        checked += script_checked
    return {
        "checked": min(strategy_total, checked),
        "total": strategy_total,
        "current_script_checked": current_checked,
        "current_script_total": current_total,
    }


def _attempts_by_script(lines: list[str]) -> dict[str, int]:
    current_script = ""
    result: dict[str, int] = {}
    for line in lines:
        script = _script_name_from_line(line)
        if script:
            current_script = script
            result.setdefault(current_script, 0)
            continue
        if _ATTEMPT_RE.match(line.strip()):
            result[current_script] = result.get(current_script, 0) + 1
    return result


def _average_attempt_ms(samples: deque[float]) -> int | None:
    if len(samples) < ETA_SAMPLE_MIN_ATTEMPTS:
        return None
    values = list(samples)
    intervals = [right - left for left, right in zip(values, values[1:]) if right >= left]
    if not intervals:
        return None
    if len(intervals) >= ETA_SAMPLE_WINSORIZE_MIN_INTERVALS:
        intervals = _winsorized(intervals, ETA_SAMPLE_WINSORIZE_RATIO)
    return max(1, int((sum(intervals) / len(intervals)) * 1000))


def _winsorized(values: list[float], ratio: float) -> list[float]:
    if not values:
        return []
    ordered = sorted(values)
    edge = int(len(ordered) * ratio)
    if edge <= 0 or edge * 2 >= len(ordered):
        return values
    low = ordered[edge]
    high = ordered[-edge - 1]
    return [min(max(value, low), high) for value in values]


def _phase_label(phase: str) -> str:
    return PHASE_LABELS.get(phase, phase or "-")


def _phase_from_line(line: str, current: str) -> str:
    text = line.strip().lower()
    if not text:
        return current
    if text in {"* summary", "* common"}:
        return PHASE_SUMMARY
    if _ATTEMPT_RE.match(line.strip()) or _live_attempt_line(line):
        return PHASE_DISCOVERY
    if text.startswith("* script"):
        return PHASE_DISCOVERY
    if text.startswith("* checking"):
        if "vpn" in text:
            return PHASE_CHECK_VPN
        if "dpi" in text or "bypass" in text or "zapret" in text or "nfqws" in text:
            return PHASE_CHECK_ZAPRET
        if "dns" in text or "domain" in text or "ip" in text or "port" in text or "http" in text:
            return PHASE_CHECK_DOMAIN
        if current in {PHASE_CHECK_VPN, PHASE_CHECK_ZAPRET, PHASE_CHECK_DOMAIN}:
            return current
        return PHASE_CHECK_DOMAIN
    return current


def _script_name_from_line(line: str) -> str:
    match = _SCRIPT_RE.match(line.strip())
    return match.group(1).strip() if match else ""


def _attempt_plan_for_run(run: dict[str, Any], current_script: str) -> dict[str, Any]:
    raw_plan = run.get("attempt_plan")
    if isinstance(raw_plan, dict) and int(raw_plan.get("total") or 0) > 0:
        return raw_plan
    if not current_script.startswith("standard/") and str(run.get("test") or "standard") != "standard":
        return _empty_attempt_plan(str(run.get("test") or ""))
    return _standard_attempt_plan(
        domains=[str(item) for item in run.get("domains") or []],
        test=str(run.get("test") or "standard"),
        enable_http=_truthy(run.get("enable_http"), default=False),
        enable_tls=_truthy(run.get("enable_tls"), default=True),
        enable_tls13=_truthy(run.get("enable_tls13"), default=False),
        enable_quic=_truthy(run.get("enable_quic"), default=True),
        enable_ipv6=_truthy(run.get("enable_ipv6"), default=False),
    )


def _standard_attempt_plan(
    domains: list[str],
    test: str = "standard",
    enable_http: bool = False,
    enable_tls: bool = True,
    enable_tls13: bool = False,
    enable_quic: bool = True,
    enable_ipv6: bool = False,
    root: Path | None = None,
) -> dict[str, Any]:
    if test != "standard":
        return _empty_attempt_plan(test)
    root = root or _blockcheck_test_dir(test)
    if not root.exists():
        return _empty_attempt_plan(test)
    scripts = _standard_scripts(root)
    domain_count = len(_clean_domains(domains))
    ip_version_count = 2 if enable_ipv6 else 1
    fingerprint = tuple((path.name, path.stat().st_mtime_ns, path.stat().st_size) for path in scripts)
    key = (
        str(root),
        fingerprint,
        domain_count,
        bool(enable_http),
        bool(enable_tls),
        bool(enable_tls13),
        bool(enable_quic),
        bool(enable_ipv6),
    )
    cached = _ATTEMPT_PLAN_CACHE.get(key)
    if cached:
        return cached

    enabled_functions: list[str] = []
    if enable_http:
        enabled_functions.append("pktws_check_http")
    if enable_tls:
        enabled_functions.append("pktws_check_https_tls12")
    if enable_tls13:
        enabled_functions.append("pktws_check_https_tls13")
    if enable_quic:
        enabled_functions.append("pktws_check_http3")

    script_totals: dict[str, int] = {}
    strategy_script_totals: dict[str, int] = {}
    script_order: list[str] = []
    source = "shell"
    for script in scripts:
        name = f"{test}/{script.name}"
        script_order.append(name)
        per_domain = 0
        for function_name in enabled_functions:
            counted = _count_script_function_attempts(root, script, function_name)
            if counted is None:
                source = "static"
                per_domain = _count_script_attempts_static(script)
                break
            per_domain += counted
        script_totals[name] = per_domain * domain_count * ip_version_count
        strategy_script_totals[name] = per_domain

    total = sum(script_totals.values())
    strategy_total = sum(strategy_script_totals.values())
    plan = {
        "test": test,
        "total": total,
        "scripts": script_totals,
        "strategy_total": strategy_total,
        "strategy_scripts": strategy_script_totals,
        "script_order": script_order,
        "domain_count": domain_count,
        "ip_version_count": ip_version_count,
        "source": source if total else "",
    }
    _ATTEMPT_PLAN_CACHE[key] = plan
    return plan


def _empty_attempt_plan(test: str) -> dict[str, Any]:
    return {
        "test": test,
        "total": 0,
        "scripts": {},
        "strategy_total": 0,
        "strategy_scripts": {},
        "script_order": [],
        "domain_count": 0,
        "ip_version_count": 1,
        "source": "",
    }


def _blockcheck_test_dir(test: str) -> Path:
    base = Path(os.environ.get("GP_BLOCKCHECK2D", "/opt/zapret2/blockcheck2.d"))
    return base / test


def _standard_scripts(root: Path | None = None) -> list[Path]:
    root = root or _blockcheck_test_dir("standard")
    if not root.exists():
        return []
    return sorted(path for path in root.glob("*.sh") if path.is_file() and path.name != "def.inc")


def _count_script_function_attempts(root: Path, script: Path, function_name: str) -> int | None:
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", function_name):
        return None
    shell = shutil.which("sh")
    if not shell:
        return None
    probe = "\n".join(
        [
            f"TESTDIR={shlex.quote(str(root))}",
            "SCANLEVEL=force",
            "IPV=4",
            "IPVV=",
            "UNAME=Linux",
            "pktws_curl_test_update() { echo __GP_ATTEMPT__; return 1; }",
            f". {shlex.quote(str(script))}",
            f"if command -v {function_name} >/dev/null 2>&1; then {function_name} curl_test_probe example.com; fi",
        ]
    )
    try:
        result = subprocess.run(
            [shell, "-c", probe],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    count = sum(1 for line in result.stdout.splitlines() if line.strip() == "__GP_ATTEMPT__")
    if result.returncode != 0 and count == 0:
        return None
    return count


def _count_script_attempts_static(script: Path) -> int:
    try:
        text = script.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 0
    loop_stack: list[int] = []
    total = 0
    for raw_line in text.splitlines():
        line = _strip_shell_comment(raw_line).strip()
        if not line:
            continue
        loop_match = re.match(r"^for\s+\w+\s+in\s+(.+?);?\s+do\s*$", line)
        if loop_match:
            loop_stack.append(max(1, _shell_word_count(loop_match.group(1))))
            continue
        if "pktws_curl_test_update" in line:
            multiplier = 1
            for value in loop_stack:
                multiplier *= value
            total += multiplier
        if line == "done" or line.endswith("; done"):
            if loop_stack:
                loop_stack.pop()
    return total


def _strip_shell_comment(line: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    result: list[str] = []
    for char in line:
        if escaped:
            result.append(char)
            escaped = False
            continue
        if char == "\\":
            result.append(char)
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            break
        result.append(char)
    return "".join(result)


def _shell_word_count(value: str) -> int:
    try:
        return len(shlex.split(value))
    except ValueError:
        return len([part for part in value.split() if part])


def _eta_parallelism_for_run(run: dict[str, Any]) -> int:
    if str(run.get("kind") or "") != "multi-domain-discovery":
        return 1
    return _minimum_int(run.get("curl_parallelism"), default=4, minimum=1)


def _eta_ms_per_attempt_for_run(run: dict[str, Any]) -> int:
    repeats = _bounded_int(run.get("repeats"), default=1, minimum=1, maximum=10)
    if _truthy(run.get("repeat_parallel"), default=False):
        repeats = 1
    return ATTEMPT_TIMEOUT_ESTIMATE_MS * repeats


def _eta_recalculation_step(attempted: int) -> int:
    return ETA_RECALC_LARGE_STEP if attempted >= ETA_RECALC_LARGE_AFTER else ETA_RECALC_SMALL_STEP


def _eta_recalculation_attempts(attempted: int) -> int:
    if attempted <= 0:
        return 0
    if attempted < ETA_RECALC_SMALL_STEP:
        return attempted
    step = _eta_recalculation_step(attempted)
    return max(step, (attempted // step) * step)


def _elapsed_average_ms_per_attempt(elapsed_seconds: int | None, attempted: int) -> int | None:
    if elapsed_seconds is None or attempted <= 0:
        return None
    return max(1, int((max(0, elapsed_seconds) * 1000) / attempted))


def _eta_from_remaining_attempts(
    remaining: int | None,
    completed: bool,
    parallelism: int = 1,
    ms_per_attempt: int = ATTEMPT_TIMEOUT_ESTIMATE_MS,
) -> int | None:
    if completed:
        return 0
    if remaining is None:
        return None
    if remaining <= 0:
        return 0
    effective_remaining = (remaining + max(1, parallelism) - 1) // max(1, parallelism)
    return max(0, int((effective_remaining * max(1, ms_per_attempt)) / 1000))


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


def _summary_sections(stdout: str) -> dict[str, list[str]]:
    lines = [line.strip() for line in stdout.splitlines()]
    summary: list[str] = []
    common: list[str] = []
    section = ""
    for index, line in enumerate(lines):
        if line == "* SUMMARY":
            section = "summary"
            continue
        if line == "* COMMON":
            section = "common"
            continue
        if not line:
            continue
        if section == "summary":
            summary.append(line)
        elif section == "common":
            common.append(line)
    if summary or common:
        return {"summary": summary, "common": common}
    return {"summary": _live_success_lines(stdout), "common": []}


def _summary_lines(stdout: str) -> list[str]:
    return _summary_sections(stdout)["summary"]


def _dedupe_candidate_lines(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for candidate in candidates:
        key = (
            str(candidate.get("scope") or ""),
            str(candidate.get("test") or ""),
            str(candidate.get("ip_version") or ""),
            str(candidate.get("domain") or ""),
            str(candidate.get("args") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def _candidate_lines(summary: list[str], scope: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for line in summary:
        parsed = _candidate_from_result_line(line, scope)
        if parsed:
            candidates.append(parsed)
    return candidates


def _candidate_from_result_line(line: str, scope: str) -> dict[str, Any] | None:
    parsed = _parse_result_line(line)
    if not parsed:
        return None
    raw_result = str(parsed.get("result") or "")
    if not raw_result.startswith("nfqws2 ") or raw_result == "nfqws2 not working":
        return None
    args = raw_result.removeprefix("nfqws2 ").strip()
    return {
        "domain": parsed["domain"],
        "test": parsed["test"],
        "ip_version": parsed["ip_version"],
        "protocol": _protocol_from_test(str(parsed["test"])),
        "args": args,
        "raw": line,
        "scope": scope,
    }


def _parse_result_line(line: str) -> dict[str, Any] | None:
    left, sep, result = line.partition(" : ")
    if not sep:
        return None
    parts = left.split()
    if len(parts) == 2 and parts[1].startswith("ipv"):
        domain = ""
    elif len(parts) >= 3 and parts[1].startswith("ipv"):
        domain = parts[2]
    else:
        return None
    return {
        "test": parts[0],
        "ip_version": parts[1].removeprefix("ipv"),
        "domain": domain,
        "result": result.strip(),
    }


def _live_success_lines(stdout: str) -> list[str]:
    result: list[str] = []
    for line in stdout.splitlines():
        candidate = _candidate_from_live_success_line(line.strip())
        if candidate:
            result.append(
                f"{candidate['test']} ipv{candidate['ip_version']} {candidate['domain']} : "
                f"nfqws2 {candidate['args']}"
            )
    return result


def _candidate_from_live_success_line(line: str) -> dict[str, Any] | None:
    pattern = re.compile(
        r"^!!!!!\s+(?P<test>\S+): working strategy found for ipv(?P<ip_version>\d+)\s+"
        r"(?P<domain>\S+)\s+:\s+nfqws2\s+(?P<args>.*?)\s+!!!!!$"
    )
    match = pattern.match(line.strip())
    if not match:
        return None
    result_line = (
        f"{match.group('test')} ipv{match.group('ip_version')} {match.group('domain')} : "
        f"nfqws2 {match.group('args').strip()}"
    )
    return _candidate_from_result_line(result_line, scope="domain")


def _live_available_lines(stdout: str) -> list[str]:
    result: list[str] = []
    pending: str | None = None
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        attempt = _live_attempt_line(line)
        if attempt:
            pending = attempt
            continue
        if line == "!!!!! AVAILABLE !!!!!" and pending:
            result.append(pending)
            pending = None
            continue
        if line.startswith("UNAVAILABLE") or line.startswith("FAILED"):
            pending = None
    return result


def _diagnostic_counts_from_stdout(
    stdout: str,
    summary_results: list[dict[str, Any] | None],
) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]], list[dict[str, Any]]]:
    status_counts: dict[str, dict[str, int]] = {}
    code_counts: dict[str, dict[str, int]] = {}
    diagnostics: list[dict[str, Any]] = []
    pending: str | None = None
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        attempt = _live_attempt_line(line)
        if attempt:
            pending = attempt
            continue
        if (line.startswith("UNAVAILABLE") or line.startswith("FAILED")) and pending:
            parsed = _parse_result_line(pending)
            if parsed:
                domain = str(parsed.get("domain") or "")
                test = str(parsed.get("test") or "")
                code = _curl_code_from_line(line)
                info = curl_failure_info(code, test=test, domain=domain)
                _increment_nested(status_counts, domain, str(info.get("status") or "curl_error"))
                if code:
                    _increment_nested(code_counts, domain, code)
                if len(diagnostics) < LIVE_CANDIDATE_SAMPLE_LIMIT:
                    diagnostics.append(
                        {
                            "domain": domain,
                            "test": test,
                            "protocol": _protocol_from_test(test),
                            "code": code,
                            "status": info.get("status") or "curl_error",
                            "label": info.get("label") or "curl ошибка",
                            "message": info.get("message") or "",
                            "strategy_failure": _is_strategy_failure(info),
                        }
                    )
            pending = None
            continue
        if line == "!!!!! AVAILABLE !!!!!":
            pending = None
    for item in summary_results:
        if not item:
            continue
        domain = str(item.get("domain") or "")
        result = str(item.get("result") or "")
        if result == "working without bypass":
            _increment_nested(status_counts, domain, "direct_available")
        elif "not working" in result:
            _increment_nested(status_counts, domain, "needs_discovery")
    return status_counts, code_counts, diagnostics


def _increment_nested(target: dict[str, dict[str, int]], first: str, second: str) -> None:
    if not first or not second:
        return
    counts = target.setdefault(first, {})
    counts[second] = counts.get(second, 0) + 1


def _curl_summary(diagnostics: list[dict[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in diagnostics:
        code = str(item.get("code") or "")
        if not code:
            continue
        result[code] = result.get(code, 0) + 1
    return result


def _live_attempt_line(line: str) -> str | None:
    if not line.startswith("- "):
        return None
    normalized = line[2:].strip()
    parsed = _parse_result_line(normalized)
    if not parsed:
        return None
    result = str(parsed.get("result") or "")
    if result.startswith("nfqws2 ") and result != "nfqws2 not working":
        return normalized
    return None


def _curl_code_from_line(line: str) -> str:
    match = re.search(r"(?:code|код)\s*=\s*(\d+)", line, re.IGNORECASE)
    return match.group(1) if match else ""


def _is_strategy_failure(info: dict[str, Any]) -> bool:
    status = str(info.get("status") or "")
    return status not in {"invalid_domain", "dns_error", "tls_sni_problem"}


def _domain_status_info(status: str) -> dict[str, str]:
    mapping = {
        "direct_available": {
            "label": "прямой доступ",
            "message": "домен открывается без zapret; подбор стратегии для него не нужен.",
        },
        "needs_discovery": {
            "label": "нужен подбор",
            "message": "домен не открылся напрямую и может требовать подбора стратегии.",
        },
    }
    if status in mapping:
        return mapping[status]
    for item in _CURL_FAILURE_INFO.values():
        if item["status"] == status:
            return {"label": str(item["label"]), "message": str(item["message"])}
    return {"label": status or "неизвестно", "message": ""}


def _domain_diagnostics_from_counts(
    domain_status_counts: dict[str, dict[str, int]],
    domain_code_counts: dict[str, dict[str, int]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for domain, counts in sorted(domain_status_counts.items()):
        status = _dominant_status(counts)
        info = _domain_status_info(status)
        codes = domain_code_counts.get(domain, {})
        result.append(
            {
                "domain": domain,
                "status": status,
                "label": info["label"],
                "message": info["message"],
                "count": int(counts.get(status, 0)),
                "total": int(sum(counts.values())),
                "codes": dict(sorted(codes.items(), key=lambda item: (-item[1], item[0]))),
            }
        )
    return result


def _dominant_failure_from_counts(domain_status_counts: dict[str, dict[str, int]]) -> dict[str, Any]:
    totals: dict[str, int] = {}
    for counts in domain_status_counts.values():
        for status, count in counts.items():
            if status == "direct_available":
                continue
            totals[status] = totals.get(status, 0) + int(count)
    if not totals:
        return {}
    status = _dominant_status(totals)
    info = _domain_status_info(status)
    return {"status": status, "label": info["label"], "message": info["message"], "count": totals[status]}


def _dominant_status(counts: dict[str, int]) -> str:
    priority = {
        "invalid_domain": 90,
        "dns_error": 80,
        "tls_sni_problem": 70,
        "ssl_connect_error": 60,
        "quic_connect_error": 55,
        "timeout": 50,
        "needs_discovery": 40,
        "curl_error": 30,
        "direct_available": 10,
    }
    if not counts:
        return ""
    return sorted(counts.items(), key=lambda item: (-int(item[1]), -priority.get(item[0], 0), item[0]))[0][0]


def _standard_script_total() -> int:
    return len(_standard_scripts())


def _standard_script_index(current_script: str, script_order: list[str] | None = None) -> int:
    if not current_script.startswith("standard/"):
        return 0
    scripts = script_order or [f"standard/{path.name}" for path in _standard_scripts()]
    try:
        return scripts.index(current_script) + 1
    except ValueError:
        return 0


def _elapsed_seconds(value: Any) -> int | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        started = datetime.fromisoformat(text)
    except ValueError:
        return None
    if started.tzinfo is None:
        now = datetime.now()
    else:
        now = datetime.now(started.tzinfo)
    return max(0, int((now - started).total_seconds()))


def _protocol_from_test(test: str) -> str:
    if "http3" in test:
        return "quic"
    if "http_" in test and "https" not in test:
        return "http"
    return "tls"


def _write_multidomain_runner(root: Path, blockcheck: Path) -> Path:
    source = blockcheck.read_text(encoding="utf-8", errors="replace")
    marker = "\nfsleep_setup\n"
    if marker not in source:
        raise RuntimeError("unsupported blockcheck2.sh layout: main marker not found")
    prefix = source.split(marker, 1)[0]
    runner = root / "gp-multidomain-blockcheck.sh"
    runner.write_text(prefix + MULTIDOMAIN_BLOCKCHECK_MAIN, encoding="utf-8")
    runner.chmod(0o700)
    return runner


def _resolve_blockcheck_script(path: Path) -> Path:
    current = path.resolve()
    seen: set[Path] = set()
    for _ in range(5):
        if current in seen:
            break
        seen.add(current)
        try:
            text = current.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return current
        if "\nfsleep_setup\n" in text:
            return current
        target = _exec_target_from_shell_wrapper(text)
        if not target:
            return current
        candidate = Path(target)
        if not candidate.is_absolute():
            candidate = current.parent / candidate
        current = candidate.resolve()
    return current


def _exec_target_from_shell_wrapper(text: str) -> str:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("exec "):
            continue
        try:
            parts = shlex.split(line)
        except ValueError:
            continue
        for part in parts[1:]:
            if part.endswith(("blockcheck2.sh", "blockcheck.sh")):
                return part
    return ""


MULTIDOMAIN_BLOCKCHECK_MAIN = r'''

gp_md_primary_domain()
{
	local d
	for d in $DOMAINS; do
		echo "$d"
		return
	done
}

gp_md_resolve_all_ips()
{
	local d ips all_ips
	for d in $DOMAINS; do
		mdig_resolve_all $IPV ips "$d"
		all_ips="${all_ips:+$all_ips }$ips"
	done
	echo "$all_ips" | tr ' ' '\n' | sort -u | tr '\n' ' '
}

gp_md_normalize_ip_list()
{
	local ip result
	for ip in $1; do
		result="${result:+$result }$ip"
	done
	echo "$result"
}

gp_md_parallel_limit()
{
	local n="${GP_MD_CURL_PARALLELISM:-4}"
	case "$n" in
		""|*[!0-9]*) n=4 ;;
	esac
	n=$((n + 0))
	[ "$n" -lt 1 ] && n=1
	echo "$n"
}

gp_md_out_file()
{
	echo "${PARALLEL_OUT}_md_$1.out"
}

gp_md_code_file()
{
	echo "${PARALLEL_OUT}_md_$1.code"
}

gp_md_run_domain_curl()
{
	# $1 - index
	# $2 - test function
	# $3 - domain
	local idx=$1 testf=$2 gp_domain="$3" code out codefile
	out="$(gp_md_out_file "$idx")"
	codefile="$(gp_md_code_file "$idx")"
	curl_test "$testf" "$gp_domain" >"$out" 2>&1
	code=$?
	echo "$code" >"$codefile"
	return 0
}

gp_md_collect_record()
{
	# $1 - pid:index:domain
	# $2 - test function
	# $3 - strategy text
	local record="$1" testf=$2 strategy_text="$3" pid rest idx gp_domain code out codefile
	pid="${record%%:*}"
	rest="${record#*:}"
	idx="${rest%%:*}"
	gp_domain="${rest#*:}"

	wait "$pid" 2>/dev/null
	out="$(gp_md_out_file "$idx")"
	codefile="$(gp_md_code_file "$idx")"
	code="$(cat "$codefile" 2>/dev/null)"
	[ -n "$code" ] || code=1

	echo "- $testf ipv$IPV $gp_domain : $PKTWSD ${WF:+$WF }$strategy_text"
	[ -f "$out" ] && cat "$out"
	rm -f "$out" "$codefile"
	if [ "$code" = 0 ]; then
		echo "!!!!! $testf: working strategy found for ipv$IPV $gp_domain : nfqws2 ${WF:+$WF }$strategy_text !!!!!"
		report_append "$gp_domain" "$testf ipv${IPV}" "$PKTWSD ${WF:+$WF }$strategy_text"
		return 0
	fi
	echo "GP-MULTIDOMAIN unavailable code=$code"
	return 1
}

pktws_curl_test_update()
{
	# $1 - curl test function
	# $2 - sample domain from the standard zapret2 script
	# $3+ - nfqws2 args
	local testf=$1 dom="$2" strategy ok=0 total=0 gp_domain idx=0 limit active=0 pending record
	shift
	shift
	strategy="$*"
	limit="$(gp_md_parallel_limit)"
	rm -f "${PARALLEL_OUT}_md_"*

	echo
	echo "- gp_multidomain_strategy ipv$IPV parallel=$limit : $PKTWSD ${WF:+$WF }$strategy"
	pktws_start "$@"
	for gp_domain in $DOMAINS; do
		idx=$(($idx + 1))
		total=$(($total + 1))
		gp_md_run_domain_curl "$idx" "$testf" "$gp_domain" &
		record="$!:$idx:$gp_domain"
		pending="${pending:+$pending }$record"
		active=$(($active + 1))
		if [ "$active" -ge "$limit" ]; then
			record="${pending%% *}"
			if [ "$record" = "$pending" ]; then
				pending=
			else
				pending="${pending#* }"
			fi
			gp_md_collect_record "$record" "$testf" "$strategy" && ok=$(($ok + 1))
			active=$(($active - 1))
		fi
	done
	while [ -n "$pending" ]; do
		record="${pending%% *}"
		if [ "$record" = "$pending" ]; then
			pending=
		else
			pending="${pending#* }"
		fi
		gp_md_collect_record "$record" "$testf" "$strategy" && ok=$(($ok + 1))
	done
	ws_kill
	rm -f "${PARALLEL_OUT}_md_"*
	echo "GP-MULTIDOMAIN result: $ok/$total domains available"
	[ "$ok" = "$total" ]
}

gp_md_run_protocol()
{
	# $1 - standard script function
	# $2 - curl test function
	# $3 - tcp/udp
	# $4 - port
	local func=$1 testf=$2 proto=$3 port=$4 ips primary
	primary="$(gp_md_primary_domain)"
	[ -n "$primary" ] || return 1
	ips="$(gp_md_resolve_all_ips)"
	ips="$(gp_md_normalize_ip_list "$ips")"
	[ -n "$ips" ] || {
		echo "GP-MULTIDOMAIN no resolved ip addresses for $proto/$port"
		return 1
	}

	echo
	echo "GP-MULTIDOMAIN preparing $PKTWSD redirection for $proto/$port"
	case "$proto" in
		tcp) pktws_ipt_prepare_tcp "$port" "$ips" ;;
		udp) pktws_ipt_prepare_udp "$port" "$ips" ;;
		*) return 1 ;;
	esac
	test_runner "$func" "$testf" "$primary"
	echo "GP-MULTIDOMAIN clearing $PKTWSD redirection for $proto/$port"
	case "$proto" in
		tcp) pktws_ipt_unprepare_tcp "$port" ;;
		udp) pktws_ipt_unprepare_udp "$port" ;;
	esac
}

fsleep_setup
fix_sbin_path
check_system
check_already
[ "$UNAME" != CYGWIN  -a "$SKIP_PKTWS" != 1 ] && require_root
check_prerequisites
trap sigint_cleanup INT
check_dns
check_virt
ask_params
trap - INT

PID=
NREPORT=
unset WF
trap sigint INT
trap sigsilent PIPE
trap sigsilent HUP
for IPV in $IPVS; do
	configure_ip_version
	[ "$ENABLE_HTTP" = 1 ] && gp_md_run_protocol pktws_check_http curl_test_http tcp "$HTTP_PORT"
	[ "$ENABLE_HTTPS_TLS12" = 1 ] && gp_md_run_protocol pktws_check_https_tls12 curl_test_https_tls12 tcp "$HTTPS_PORT"
	[ "$ENABLE_HTTPS_TLS13" = 1 ] && gp_md_run_protocol pktws_check_https_tls13 curl_test_https_tls13 tcp "$HTTPS_PORT"
	[ "$ENABLE_HTTP3" = 1 ] && gp_md_run_protocol pktws_check_http3 curl_test_http3 udp "$QUIC_PORT"
done
trap - HUP
trap - PIPE
trap - INT

cleanup

echo
echo \* SUMMARY
report_print
[ "$DOMAINS_COUNT" -gt 1 ] && {
	echo
	echo \* COMMON
	result_intersection_print
}
'''


def _clean_domains(domains: list[str]) -> list[str]:
    return _clean_domain_list(domains) or list(CRITICAL_DOMAINS)


def _clean_domain_list(domains: list[str]) -> list[str]:
    return list(validate_domain_inputs(list(domains), default_to_critical=False)["domains"])


def _finder_dir(state_dir: Path) -> Path:
    path = state_dir / "strategy-finder"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cleanup_old_strategy_logs(logs: Path) -> dict[str, int]:
    if not logs.is_dir():
        return {"removed_files": 0, "removed_bytes": 0}
    files: list[tuple[float, int, Path]] = []
    for path in logs.iterdir():
        if not path.is_file():
            continue
        name = path.name
        if not any(name.endswith(suffix) for suffix in LOG_RETENTION_SUFFIXES):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        files.append((stat.st_mtime, int(stat.st_size), path))
    files.sort(key=lambda item: item[0], reverse=True)
    kept_count = 0
    kept_bytes = 0
    removed_files = 0
    removed_bytes = 0
    for _mtime, size, path in files:
        keep = kept_count < LOG_RETENTION_MAX_FILES and kept_bytes + size <= LOG_RETENTION_MAX_TOTAL_BYTES
        if keep:
            kept_count += 1
            kept_bytes += size
            continue
        try:
            path.unlink()
        except OSError:
            continue
        removed_files += 1
        removed_bytes += size
    return {"removed_files": removed_files, "removed_bytes": removed_bytes}


def _iter_db_candidates(conn: Any) -> Iterator[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, protocol, args, status,
               fragmentation_class, fragmentation_safe, fragmentation_reason,
               family, family_key, family_rank, family_reason
        FROM strategies
        ORDER BY id ASC
        """
    ).fetchall()
    for row in rows:
        yield _candidate_from_db(conn, row, include_events=False)


def _candidate_from_db(conn: Any, row: Any, *, include_events: bool) -> dict[str, Any]:
    row_keys = set(row.keys()) if hasattr(row, "keys") else set()
    analysis = analyze_strategy(str(row["protocol"] or ""), str(row["args"] or ""))
    candidate = {
        "id": row["id"],
        "protocol": row["protocol"],
        "args": row["args"],
        "status": row["status"],
        "first_seen_at": row["first_seen_at"] if "first_seen_at" in row_keys else "",
        "last_seen_at": row["last_seen_at"] if "last_seen_at" in row_keys else "",
        "fragmentation_class": (
            str(row["fragmentation_class"] or "") if "fragmentation_class" in row_keys else ""
        )
        or analysis.fragmentation_class,
        "fragmentation_safe": (
            bool(row["fragmentation_safe"]) if "fragmentation_safe" in row_keys else analysis.fragmentation_safe
        ),
        "fragmentation_reason": (
            str(row["fragmentation_reason"] or "") if "fragmentation_reason" in row_keys else ""
        )
        or analysis.fragmentation_reason,
        "family": (str(row["family"] or "") if "family" in row_keys else "") or analysis.family,
        "family_key": (str(row["family_key"] or "") if "family_key" in row_keys else "") or analysis.family_key,
        "family_rank": int(row["family_rank"] or 0) if "family_rank" in row_keys else analysis.family_rank,
        "family_reason": (str(row["family_reason"] or "") if "family_reason" in row_keys else "") or analysis.family_reason,
    }
    if include_events:
        seen_rows = conn.execute(
            """
            SELECT d.name AS domain
            FROM strategy_domain_results r
            JOIN domains d ON d.id = r.domain_id
            WHERE r.strategy_id = ? AND r.source_mode = 'single_domain'
            ORDER BY d.name ASC
            """,
            (row["id"],),
        ).fetchall()
        common_rows = conn.execute(
            """
            SELECT DISTINCT d.name AS domain
            FROM strategy_domain_results r
            JOIN domains d ON d.id = r.domain_id
            WHERE r.strategy_id = ? AND r.source_mode = 'multi_domain'
            ORDER BY d.name ASC
            """,
            (row["id"],),
        ).fetchall()
        candidate["seen"] = [
            {
                "run_id": "",
                "domain": item["domain"],
                "test": "",
                "ip_version": "",
                "seen_at": "",
            }
            for item in seen_rows
        ]
        common_domains = [str(item["domain"]) for item in common_rows]
        if common_domains:
            candidate["common_seen"] = [{"domains": common_domains}]
        return candidate

    domain_rows = conn.execute(
        """
        SELECT DISTINCT d.name AS domain
        FROM strategy_domain_results r
        JOIN domains d ON d.id = r.domain_id
        WHERE r.strategy_id = ? AND r.source_mode = 'single_domain'
        ORDER BY d.name ASC
        """,
        (row["id"],),
    ).fetchall()
    common_domain_rows = conn.execute(
        """
        SELECT DISTINCT d.name AS domain
        FROM strategy_domain_results r
        JOIN domains d ON d.id = r.domain_id
        WHERE r.strategy_id = ? AND r.source_mode = 'multi_domain'
        ORDER BY d.name ASC
        """,
        (row["id"],),
    ).fetchall()
    candidate["seen"] = [{"domain": item["domain"]} for item in domain_rows]
    common_domains = [str(item["domain"]) for item in common_domain_rows]
    if common_domains:
        candidate["common_seen"] = [{"domains": common_domains}]
    return candidate


def _tested_domains_from_db(conn: Any) -> set[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT d.name AS domain
        FROM domains d
        JOIN strategy_domain_results r ON r.domain_id = d.id
        ORDER BY d.name ASC
        """
    ).fetchall()
    return {str(row["domain"]).strip() for row in rows if str(row["domain"]).strip()}


def _storage_version(state_dir: Path) -> dict[str, int]:
    return _file_version(state_dir / "strategy-finder" / "state.sqlite3")


def _tail_lines(path: Path, max_lines: int) -> list[str]:
    max_lines = max(0, max_lines)
    if max_lines <= 0 or not path.exists():
        return []
    block_size = 8192
    blocks: list[bytes] = []
    line_count = 0
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        while position > 0 and line_count <= max_lines:
            read_size = min(block_size, position)
            position -= read_size
            handle.seek(position)
            block = handle.read(read_size)
            blocks.append(block)
            line_count += block.count(b"\n")
    data = b"".join(reversed(blocks))
    return data.decode("utf-8", errors="replace").splitlines()[-max_lines:]


def _log_delta(path: Path | None, expected_path: str | None, from_size: int | None, max_bytes: int = 200_000) -> str | None:
    if path is None or from_size is None or not expected_path:
        return None
    if str(path) != str(expected_path):
        return None
    if from_size < 0 or not path.exists():
        return None
    current_size = path.stat().st_size
    if current_size < from_size:
        return None
    if current_size - from_size > max_bytes:
        return None
    with path.open("rb") as handle:
        handle.seek(from_size)
        return handle.read(current_size - from_size).decode("utf-8", errors="replace")


def _file_version(path: Path) -> dict[str, int]:
    if not path.exists():
        return {"size": 0, "mtime_ns": 0}
    stat = path.stat()
    return {"size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}


def _candidate_domains(candidate: dict[str, Any]) -> list[str]:
    seen = candidate.get("seen")
    if not isinstance(seen, list):
        return []
    return sorted({str(item.get("domain") or "").strip() for item in seen if isinstance(item, dict) and str(item.get("domain") or "").strip()})


def _candidate_common_domains(candidate: dict[str, Any]) -> list[str]:
    common_seen = candidate.get("common_seen")
    if not isinstance(common_seen, list):
        return []
    domains: set[str] = set()
    for item in common_seen:
        if not isinstance(item, dict) or not isinstance(item.get("domains"), list):
            continue
        domains.update(str(domain or "").strip() for domain in item["domains"] if str(domain or "").strip())
    return sorted(domains)


def _compact_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    domains = _candidate_domains(candidate)
    common_domains = _candidate_common_domains(candidate)
    result = {
        "id": candidate.get("id"),
        "protocol": candidate.get("protocol"),
        "args": candidate.get("args"),
        "status": candidate.get("status"),
        "first_seen_at": candidate.get("first_seen_at"),
        "last_seen_at": candidate.get("last_seen_at"),
        "fragmentation_class": candidate.get("fragmentation_class"),
        "fragmentation_safe": bool(candidate.get("fragmentation_safe")),
        "fragmentation_reason": candidate.get("fragmentation_reason"),
        "family": candidate.get("family"),
        "family_key": candidate.get("family_key"),
        "family_rank": candidate.get("family_rank"),
        "family_reason": candidate.get("family_reason"),
        "seen": [{"domain": domain} for domain in domains],
    }
    if common_domains:
        result["common_seen"] = [{"domains": common_domains}]
    return result
