"""Compatibility module for historical ``gp_control_plane.web.app`` imports."""

from __future__ import annotations

import sys as _sys

from . import api_server as _api_server


# This is an import-compatibility alias only.  The public ``serve`` facade in
# api_server delegates exclusively to core_runtime's Bottle/Cheroot listener;
# no BaseHTTP listener or second production dispatch remains here.
_sys.modules[__name__] = _api_server
