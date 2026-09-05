"""bottle_server._server — WSGI launcher for Bottle WebUI server."""

from __future__ import annotations

from typing import Any
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer

from gp_control_plane.backups import create_post_run_snapshot
from gp_control_plane.config import AppConfig
from gp_control_plane.engine_common import close_stale_running_runs
from gp_control_plane.web import api_server
from gp_control_plane.web.api_server._server import _recover_runtime_before_serve
from gp_control_plane.web.bottle_server._routes import create_bottle_app
from gp_control_plane.web.vendor.bottle import ServerAdapter


class ThreadingWSGIAdapter(ServerAdapter):
    def run(self, handler: Any) -> None:
        base_server_cls = api_server.ThreadingHTTPServer

        class ThreadingWSGIServer(base_server_cls, WSGIServer):  # type: ignore[valid-type, misc]
            daemon_threads = True

            def __init__(self, server_address: tuple[str, int], RequestHandlerClass: Any) -> None:
                base_server_cls.__init__(self, server_address, RequestHandlerClass)

        class QuietHandler(WSGIRequestHandler):
            def log_message(self, *args: Any, **kwargs: Any) -> None:
                pass

        handler_cls = QuietHandler if self.quiet else WSGIRequestHandler
        server = ThreadingWSGIServer((self.host, self.port), handler_cls)
        server.set_app(handler)
        server.serve_forever()


def serve_web_bottle(
    config: AppConfig,
    host: str = "127.0.0.1",
    port: int = 8080,
    *,
    ui_enabled: bool = True,
) -> None:
    """Start GP Control Plane Web UI using Bottle WSGI framework."""
    _recover_runtime_before_serve(config)
    close_stale_running_runs(config.output.state_dir)
    runner = api_server.JobRunner(
        config.output.state_dir,
        on_idle=lambda: api_server.create_post_run_snapshot(config.output.state_dir),
    )
    runtime_role = "monolith" if ui_enabled else "core"
    app = create_bottle_app(config, runner, runtime_role=runtime_role, ui_enabled=ui_enabled)
    mode = "web UI (Bottle)" if ui_enabled else "core API (Bottle)"
    print(f"GP control plane {mode} listening on http://{host}:{port}")
    app.run(host=host, port=port, quiet=True, server=ThreadingWSGIAdapter)
