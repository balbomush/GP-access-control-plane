"""bc2_engine — blockcheck2 runtime split out of strategy_finder.py.

Launchers are re-exported for clean consumer imports; internal helpers live
in ``_plan``/``_progress``/``_recorder``/``_writers``/``_process``/
``_runner``/``_multidomain``.
"""

from gp_control_plane.bc2_engine._launch import run_multi_domain_discovery, run_standard_discovery

__all__ = ["run_multi_domain_discovery", "run_standard_discovery"]
