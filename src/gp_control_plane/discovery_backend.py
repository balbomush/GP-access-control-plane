"""Discovery backends used by GP strategy-finder jobs.

blockcheck2 remains the default stdout/marker path (``run_standard_discovery`` /
``run_multi_domain_discovery``). blockcheckS is a separate backend that runs
``bs scan`` and harvests PASS∧APPLIED from SQLite — it does not emulate
blockcheck2 ``!!!!! AVAILABLE !!!!!`` markers.
"""

from __future__ import annotations

from .blockchecks_backend import export_nfconf, run_blockchecks_discovery, stop_blockchecks
from .strategy_finder import run_multi_domain_discovery, run_standard_discovery

__all__ = [
    "export_nfconf",
    "run_blockchecks_discovery",
    "run_multi_domain_discovery",
    "run_standard_discovery",
    "stop_blockchecks",
]
