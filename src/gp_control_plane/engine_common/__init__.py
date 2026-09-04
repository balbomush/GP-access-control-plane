"""engine_common — shared strategy-data layer split out of strategy_finder.py.

Public re-exports keep consumer imports clean. Internal cross-module imports
target the concrete submodules (``_store``, ``_models``, ...) directly.
"""

from gp_control_plane.engine_common._logtail import (
    classify_stderr_diagnostics,
    latest_log_tail,
    parse_blockcheck_stdout,
)
from gp_control_plane.engine_common._options import (
    classify_domain_input,
    curl_failure_info,
    domain_sets,
    validate_domain_inputs,
)
from gp_control_plane.engine_common._runmeta import allocate_discovery_run_id
from gp_control_plane.engine_common._runs import close_stale_running_runs, read_runs, read_runs_page
from gp_control_plane.engine_common._store import (
    candidate_storage_version,
    iter_strategy_candidates_filtered,
    read_candidate_domain_index,
    read_candidate_page,
    read_candidates,
    read_strategy_candidates_filtered,
)
from gp_control_plane.engine_common._upsert import (
    candidate_id_for,
    candidate_total,
    upsert_candidates,
)

__all__ = [
    "allocate_discovery_run_id",
    "candidate_id_for",
    "candidate_storage_version",
    "candidate_total",
    "classify_domain_input",
    "classify_stderr_diagnostics",
    "close_stale_running_runs",
    "curl_failure_info",
    "domain_sets",
    "iter_strategy_candidates_filtered",
    "latest_log_tail",
    "parse_blockcheck_stdout",
    "read_candidate_domain_index",
    "read_candidate_page",
    "read_candidates",
    "read_runs",
    "read_runs_page",
    "read_strategy_candidates_filtered",
    "upsert_candidates",
    "validate_domain_inputs",
]
