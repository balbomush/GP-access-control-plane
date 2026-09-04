"""engine_common._constants — moved from strategy_finder.py / blockchecks_backend.py."""
from __future__ import annotations

import re
from typing import Any

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

CORE_CANDIDATE_JSON_MAX_RESULTS = 1000

CANDIDATE_RELATION_BATCH_SIZE = 500

NFQUEUE_MAXLEN_MISSING_RE = re.compile(r"can't set queue maxlen:\s+No such file or directory", re.IGNORECASE)
