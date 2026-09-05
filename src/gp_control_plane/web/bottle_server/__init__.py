"""bottle_server — Bottle-based WSGI WebUI server for GP Control Plane."""

from gp_control_plane.web.bottle_server._routes import create_bottle_app
from gp_control_plane.web.bottle_server._server import serve_web_bottle

__all__ = ["create_bottle_app", "serve_web_bottle"]
