"""engine_common._retention — moved from strategy_finder.py / blockchecks_backend.py."""
from __future__ import annotations

from pathlib import Path
from gp_control_plane.engine_common._constants import CRITICAL_DOMAINS, LOG_RETENTION_MAX_FILES, LOG_RETENTION_MAX_TOTAL_BYTES, LOG_RETENTION_SUFFIXES
from gp_control_plane.engine_common._options import validate_domain_inputs

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
