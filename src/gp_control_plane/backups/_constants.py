"""gp_control_plane.backups._constants — moved from storage.py (split)."""
from __future__ import annotations

import re
from pathlib import Path

SNAPSHOT_KEEP = 5


BACKUP_SCHEMA_VERSION = "7"


SUPPORTED_BACKUP_SCHEMA_VERSIONS = {"5", "6", BACKUP_SCHEMA_VERSION}


HISTORY_BACKUP_SCHEMA_VERSION = "7"


CLEAN_INSTALL_VAULT_RELATIVE_PATH = Path(".local/share/gp-control-plane/clean-install-vault")


CLEAN_INSTALL_HANDOFF_RELATIVE_PATH = Path(".local/share/gp-control-plane/clean-install-vault/handoff.json")


_VAULT_FILE_MODE = 0o600


_VAULT_DIRECTORY_MODE = 0o700


_VAULT_ID_RE = re.compile(r"^[a-f0-9]{32}$")


_VAULT_ENTRY_NAME = "entry.json"


_VAULT_ARCHIVE_NAME = "archive.zip"


POST_RUN_SNAPSHOT_ERROR_MESSAGE_MAX_LENGTH = 512


SNAPSHOT_DOWNLOAD_FILES = {
    "manifest.json",
    "checksums.sha256",
    "domains/domains.ndjson",
    "strategies/strategies.ndjson",
    "strategies/strategy-domain-links.ndjson",
    "presets/domain-presets.ndjson",
    "presets/preset-domains.ndjson",
    "settings/app-settings.ndjson",
    "history/runs.ndjson",
}
