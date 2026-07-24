from __future__ import annotations

from ..config import AppConfig


def serve_core(config: AppConfig, host: str = "127.0.0.1", port: int = 8081) -> None:
    from .api_server import serve

    serve(config, host=host, port=port, ui_enabled=False)
