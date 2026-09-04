"""gp_control_plane.domain_sources._constants — moved from storage.py (split)."""
from __future__ import annotations

from urllib.error import URLError
import re
import subprocess
import tarfile


V2FLY_BASE_URL = "https://raw.githubusercontent.com/v2fly/domain-list-community/master/data"


V2FLY_CONTENTS_URL = "https://api.github.com/repos/v2fly/domain-list-community/contents/data?ref=master"


V2FLY_REVISION_URL = "https://api.github.com/repos/v2fly/domain-list-community/commits/master"


V2FLY_GIT_URL = "https://github.com/v2fly/domain-list-community.git"


V2FLY_ARCHIVE_URL = "https://codeload.github.com/v2fly/domain-list-community/tar.gz/refs/heads/master"


V2FLY_LOCAL_SOURCE = "local-storage"


_COVERAGE_NOTE = "publicly known verifiable domain set; not a guarantee of full service coverage"


_CATEGORY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")


_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")


_FALLBACK_V2FLY_CATEGORIES = [
    "amazon",
    "cloudflare",
    "discord",
    "facebook",
    "google",
    "instagram",
    "meta",
    "telegram",
    "youtube",
]


_EXPECTED_V2FLY_SOURCE_ERRORS = (
    OSError,
    TimeoutError,
    URLError,
    subprocess.SubprocessError,
    tarfile.TarError,
    ValueError,
)


_EXPECTED_V2FLY_REVISION_FALLBACK_ERRORS = (
    OSError,
    TimeoutError,
    subprocess.SubprocessError,
)


_EXPECTED_V2FLY_ARCHIVE_FALLBACK_ERRORS = (
    OSError,
    TimeoutError,
    URLError,
    tarfile.TarError,
)
