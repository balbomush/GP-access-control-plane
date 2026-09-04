from __future__ import annotations

import sys as _sys

from . import api_server as _api_server

# Compatibility module: existing imports of gp_control_plane.web.app must keep
# patching and reading the real API runtime module during the headless split.
_sys.modules[__name__] = _api_server
