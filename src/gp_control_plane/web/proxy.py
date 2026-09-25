"""Compatibility facade for the standalone Web listener."""

from __future__ import annotations

from ..config import AppConfig
from ..resource_budget import BACKUP_UPLOAD_MAX_BYTES, JSON_REQUEST_MAX_BYTES, PROXY_STREAM_CHUNK_BYTES
from .core_client import CoreClient
from .cheroot_connection import WebStreamLifecycle
from .http_body import ProxyRequestBodyTooLarge
from .http_runtime import WebHttpRuntime
from .web_app import create_web_app


def create_web_runtime(config: AppConfig, host: str, port: int, *, core_url: str) -> WebHttpRuntime:
    """Assemble one Web listener without binding or starting it."""

    del config
    core_client = CoreClient(core_url)
    stream_lifecycle = WebStreamLifecycle()
    return WebHttpRuntime(
        create_web_app(core_client, stream_lifecycle=stream_lifecycle),
        host,
        port,
        stream_lifecycle=stream_lifecycle,
    )


def serve_web_proxy(config: AppConfig, host: str, port: int, *, core_url: str) -> None:
    """Preserved external entrypoint; listener ownership lives in WebHttpRuntime."""

    runtime = create_web_runtime(config, host, port, core_url=core_url)
    print(f"GP control plane web UI proxy listening on http://{host}:{port}; core={core_url}")
    runtime.serve_forever()


__all__ = [
    "BACKUP_UPLOAD_MAX_BYTES",
    "JSON_REQUEST_MAX_BYTES",
    "PROXY_STREAM_CHUNK_BYTES",
    "ProxyRequestBodyTooLarge",
    "create_web_runtime",
    "serve_web_proxy",
]
