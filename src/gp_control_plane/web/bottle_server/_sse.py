"""bottle_server._sse — SSE streaming generator for Bottle WebUI server."""

from __future__ import annotations

import json
import time
import traceback
from collections.abc import Iterator
from typing import Any

from gp_control_plane.auth import AuthenticationError, require_bearer_token
from gp_control_plane.config import AppConfig
from gp_control_plane.storage import is_storage_unavailable_error
from gp_control_plane.web.api_server._events import web_event_changes


def stream_web_events(config: AppConfig, authorization: str | None) -> Iterator[bytes]:
    """Yield SSE event byte chunks for /api/web/events/stream."""
    previous: dict[str, str] = {}
    heartbeat_at = 0.0
    while True:
        try:
            require_bearer_token(config.output.state_dir, authorization)
            for event_name, payload in web_event_changes(config, previous):
                require_bearer_token(config.output.state_dir, authorization)
                yield _format_sse(event_name, payload)
            now = time.monotonic()
            if now - heartbeat_at >= 15:
                require_bearer_token(config.output.state_dir, authorization)
                yield b": keepalive\n\n"
                heartbeat_at = now
            time.sleep(1)
        except AuthenticationError:
            yield _format_sse(
                "event-error",
                {"error": "authentication_required", "message": "Bearer token is required"},
            )
            return
        except Exception as exc:  # noqa: BLE001
            print("GENERATOR EXCEPTION:", exc)
            traceback.print_exc()
            if is_storage_unavailable_error(exc):
                yield _format_sse(
                    "event-error",
                    {"error": "storage_unavailable", "message": "Storage is temporarily unavailable."},
                )
                return
            yield _format_sse("event-error", {"error": "event-loop", "message": str(exc)})
            time.sleep(1)


def _format_sse(event_name: str, payload: dict[str, Any]) -> bytes:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event_name}\ndata: {data}\n\n".encode("utf-8")
