"""gp_control_plane.storage._constants — moved from storage.py (split)."""
from __future__ import annotations

from pathlib import Path
from typing import Any
import threading


SCHEMA_VERSION = 11


SCHEMA_MIGRATIONS = (
    (1, "base_candidate_storage"),
    (2, "normalized_domain_strategy_model"),
    (3, "minimal_backup_model"),
    (4, "runtime_observability"),
    (5, "remove_legacy_candidate_storage"),
    (6, "preset_domain_state"),
    (7, "compact_runtime_payloads"),
    (8, "trim_strategy_attempt_diagnostics"),
    (9, "minimal_sqlite_working_model"),
    (10, "strategy_analysis_metadata"),
    (11, "app_settings"),
)


_MIGRATION_LOCK = threading.Lock()


_MIGRATED_DB_PATHS: set[Path] = set()


AUTH_BUSY_TIMEOUT_MS = 2_000


_OMITTED = object()


_PRIVATE_DIRECTORY_MODE = 0o700


_PRIVATE_FILE_MODE = 0o600


_SQLITE_SIDECAR_SUFFIXES = ("", "-wal", "-shm")


_RUN_PAYLOAD_DROP_KEYS = {
    "summary",
    "common",
    "live_summary",
    "results",
    "common_results",
    "direct_available",
    "not_working",
    "candidates",
    "common_candidates",
    "attempts",
    "attempt_results",
    "candidate_events",
    "candidate_samples",
    "common_candidate_samples",
}


_RUN_PAYLOAD_STRUCTURED_LIST_KEYS = {"domains"}


_RUN_PAYLOAD_COMPACT_OBJECT_LIST_KEYS = {
    "domain_skipped",
    "domain_classification",
    "domain_diagnostics",
    "curl_diagnostics",
}


_RUN_PAYLOAD_MAX_SCALAR_LIST = 500


_RUN_PAYLOAD_MAX_OBJECT_LIST = 100


_RUN_PAYLOAD_MAX_STRING = 8192


_RUN_PAYLOAD_COMPACT_BATCH_SIZE = 100


_LEGACY_RUNTIME_FILES = ("available.ndjson", "runs.jsonl", "candidates.json")


_LEGACY_STORAGE_TABLES = (
    "candidate_seen_events",
    "candidate_common_domains",
    "candidate_domains",
    "candidates",
    "presets",
)


SYSTEM_DOMAIN_PRESETS: dict[str, dict[str, dict[str, Any]]] = {
    "finder": {
        "required": {
            "label": "Обязательные домены",
            "domains": [],
        },
        "desired": {
            "label": "Желательные домены",
            "domains": [],
        },
    },
    "common": {},
}


SYSTEM_DOMAIN_PRESET_NAMES = {
    (scope, name)
    for scope, scoped in SYSTEM_DOMAIN_PRESETS.items()
    for name in scoped
}
