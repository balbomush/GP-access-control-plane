"""bs_engine._export — moved from strategy_finder.py / blockchecks_backend.py."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from gp_control_plane.discovery_engine import blockchecks_state_dir, resolve_bc_nfconf


def _default_export_out_dir() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data_home) / "blockcheckS" / "export"

def latest_bs_run_db() -> Path | None:
    """Most recent per-GP-run BS database under the blockcheckS state dir."""
    runs_dir = blockchecks_state_dir() / "bs-runs"
    if runs_dir.is_dir():
        dbs = sorted(runs_dir.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        if dbs:
            return dbs[0]
    default = blockchecks_state_dir() / "state.db"
    return default if default.is_file() else None

def export_nfconf(
    *,
    out_dir: Path | None = None,
    limit: int = 5,
    db: Path | None = None,
    allow_stock_fallback: bool = True,
) -> dict[str, Any]:
    """Re-export nfqws2 confs from a blockcheckS run DB.

    bc-nfconf targets explicit domains only (its built-in set otherwise).
    We scope it to the distinct domains recorded in the run DB and let it
    fall back to per-domain best export (``--no-common-only``).
    """
    nfconf = resolve_bc_nfconf()
    target = Path(out_dir) if out_dir else _default_export_out_dir()
    target.mkdir(parents=True, exist_ok=True)
    target_db = Path(db) if db else latest_bs_run_db()
    if target_db is None or not target_db.is_file():
        raise RuntimeError(f"blockcheckS run database not found: {blockchecks_state_dir()}")
    domains = _distinct_run_domains(target_db)
    if not domains:
        raise RuntimeError(f"no tcp_results domains in run database: {target_db}")
    temp_dir = Path(tempfile.mkdtemp(prefix="gp-bs-nfconf-"))
    try:
        domains_file = temp_dir / "domains.txt"
        domains_file.write_text("\n".join(domains) + "\n", encoding="utf-8")
        cmd = [
            nfconf,
            "--db",
            str(target_db),
            "--out-dir",
            str(target),
            "--limit",
            str(max(1, int(limit))),
            "--domains-file",
            str(domains_file),
            "--no-common-only",
        ]
        if allow_stock_fallback:
            cmd.append("--allow-stock-fallback")
        completed = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                (completed.stderr or "").strip() or (completed.stdout or "").strip() or "bc-nfconf failed"
            )
    finally:
        import shutil

        shutil.rmtree(temp_dir, ignore_errors=True)
    confs = sorted(str(path) for path in target.glob("*.conf"))
    return {"engine": "blockchecks", "out_dir": str(target), "paths": confs, "db": str(target_db)}

def _distinct_run_domains(db: Path) -> list[str]:
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "SELECT DISTINCT domain FROM tcp_results WHERE domain IS NOT NULL AND domain != ''"
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    return [str(row[0]) for row in rows]

def _looks_like_conf_path(value: str) -> bool:
    v = str(value or "").strip()
    return bool(v) and ("/" in v or "\\" in v) and v.lower().endswith(".conf")

def _desync_cores_from_conf(path: str) -> list[str]:
    """Return ``--lua-desync=`` core strings from an nfqws2 .conf file."""
    cores: list[str] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if line.startswith("--lua-desync="):
                    core = line[len("--lua-desync=") :].strip()
                    if core:
                        cores.append(core)
    except OSError:
        return []
    return cores

def _expand_config_candidate_args(value: str) -> list[str]:
    """Turn a stored strategy value into harvest candidate arg strings.

    ``config_path`` may be an nfqws2 .conf file (default BS configs source):
    each ``--lua-desync=`` core becomes its own inline candidate so the web
    panel shows real strategy lines instead of file paths.
    """
    v = str(value or "").strip()
    if v and _looks_like_conf_path(v) and os.path.isfile(v):
        cores = _desync_cores_from_conf(v)
        if cores:
            return cores
    return [v] if v else []
