"""Finite Core-listener contracts for the existing external Web views.

This module deliberately contains names only.  It is safe for the Web process
to import when selecting a Core operation: it neither reads product state nor
imports storage or authentication implementation.
"""

from __future__ import annotations


INTERNAL_AUTH_VERIFY_BEARER_PATH = "/api/internal/auth/verify-bearer"
INTERNAL_WEB_EVENTS_SNAPSHOT_PATH = "/api/internal/web-events/snapshot"
INTERNAL_WEB_EVENTS_STREAM_PATH = "/api/internal/web-events/stream"

WEB_VIEW_INTERNAL_PATHS = {
    "/api/web/status": "/api/internal/web-views/status",
    "/api/web/run-preferences": "/api/internal/web-views/run-preferences",
    "/api/web/runs/history-page": "/api/internal/web-views/runs-history-page",
    "/api/web/candidate-domain-index-page": "/api/internal/web-views/candidate-domain-index-page",
    "/api/web/strategy-candidates-page": "/api/internal/web-views/strategy-candidates-page",
    "/api/web/presets": "/api/internal/web-views/presets",
    "/api/web/presets/domains": "/api/internal/web-views/preset-domains",
    "/api/web/presets/save": "/api/internal/web-views/preset-save",
    "/api/web/presets/delete-user-lists": "/api/internal/web-views/preset-delete-user-lists",
}

INTERNAL_WEB_VIEW_PATHS = frozenset(WEB_VIEW_INTERNAL_PATHS.values())
INTERNAL_WEB_VIEW_EXTERNAL_PATHS = {path: external for external, path in WEB_VIEW_INTERNAL_PATHS.items()}
INTERNAL_WEB_VIEW_GET_PATHS = frozenset(
    WEB_VIEW_INTERNAL_PATHS[path]
    for path in (
        "/api/web/status",
        "/api/web/run-preferences",
        "/api/web/runs/history-page",
        "/api/web/candidate-domain-index-page",
        "/api/web/strategy-candidates-page",
        "/api/web/presets",
        "/api/web/presets/domains",
    )
)
INTERNAL_WEB_VIEW_POST_PATHS = frozenset(
    WEB_VIEW_INTERNAL_PATHS[path]
    for path in (
        "/api/web/run-preferences",
        "/api/web/presets/save",
        "/api/web/presets/delete-user-lists",
    )
)


def internal_web_view_path(external_path: str) -> str:
    """Return the one subject-specific Core operation for an external view."""
    if external_path == "/api/web/events":
        return INTERNAL_WEB_EVENTS_SNAPSHOT_PATH
    if external_path == "/api/web/events/stream":
        return INTERNAL_WEB_EVENTS_STREAM_PATH
    try:
        return WEB_VIEW_INTERNAL_PATHS[external_path]
    except KeyError as error:
        raise KeyError(external_path) from error


def external_web_view_path(internal_path: str) -> str:
    """Resolve a Core operation back to the unchanged external Web view path."""
    try:
        return INTERNAL_WEB_VIEW_EXTERNAL_PATHS[internal_path]
    except KeyError as error:
        raise KeyError(internal_path) from error
