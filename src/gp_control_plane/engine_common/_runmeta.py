"""engine_common._runmeta — moved from strategy_finder.py / blockchecks_backend.py."""
from __future__ import annotations

import uuid
from gp_control_plane.state import now_iso
from gp_control_plane.engine_common._options import DiscoveryOptions

def allocate_discovery_run_id() -> str:
    return f"{now_iso().replace(':', '').replace('-', '')}-{uuid.uuid4().hex[:8]}"

def _discovery_run_id(run_id: str | None) -> str:
    value = str(run_id or "").strip()
    return value or allocate_discovery_run_id()

def _ipvs_value(options: DiscoveryOptions) -> str:
    return "4 6" if options.enable_ipv6 else "4"
