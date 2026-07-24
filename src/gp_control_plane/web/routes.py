from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .docs import SWAGGER_PATHS


@dataclass(frozen=True)
class RouteSpec:
    path: str
    methods: frozenset[str]
    namespace: str
    dispatch: str
    openapi: bool = False
    allowed_in_core: bool = True


def route_for(method: str, path: str) -> RouteSpec | None:
    return _ROUTE_INDEX.get((method.upper(), path))


def route_paths(*, method: str | None = None, namespace: str | None = None, dispatch: str | None = None) -> set[str]:
    method_filter = method.upper() if method else None
    return {
        spec.path
        for spec in ROUTES
        if (method_filter is None or method_filter in spec.methods)
        and (namespace is None or spec.namespace == namespace)
        and (dispatch is None or spec.dispatch == dispatch)
    }


def openapi_operations() -> set[tuple[str, str]]:
    return {(spec.path, method) for spec in ROUTES if spec.openapi for method in spec.methods if method != "HEAD"}


def _route(
    path: str,
    methods: Iterable[str],
    namespace: str,
    dispatch: str,
    *,
    openapi: bool = False,
    allowed_in_core: bool = True,
) -> RouteSpec:
    return RouteSpec(
        path=path,
        methods=frozenset(method.upper() for method in methods),
        namespace=namespace,
        dispatch=dispatch,
        openapi=openapi,
        allowed_in_core=allowed_in_core,
    )


ROUTES = (
    _route("/", {"GET", "HEAD"}, "web", "html", allowed_in_core=False),
    _route("/openapi.json", {"GET", "HEAD"}, "openapi", "openapi-json"),
    *(
        _route(path, {"GET", "HEAD"}, "openapi", "swagger-ui")
        for path in sorted(SWAGGER_PATHS)
    ),
    _route("/api/core/status", {"GET"}, "core", "json-get", openapi=True),
    _route("/api/core/strategy-discovery/start-run", {"POST"}, "core", "json-post", openapi=True),
    _route("/api/core/strategy-discovery/stop-current-run", {"POST"}, "core", "json-post", openapi=True),
    _route("/api/core/strategy-discovery/current-run-progress", {"GET"}, "core", "json-get", openapi=True),
    _route("/api/core/strategy-discovery/current-run-latest-log", {"GET"}, "core", "json-get", openapi=True),
    _route("/api/core/strategy-discovery/preflight", {"GET"}, "core", "json-get", openapi=True),
    _route("/api/core/presets/domain-lists", {"GET"}, "core", "json-get", openapi=True),
    _route("/api/core/presets/save-domain-list", {"POST"}, "core", "json-post", openapi=True),
    _route("/api/core/presets/delete-user-domain-list", {"POST"}, "core", "json-post", openapi=True),
    _route("/api/core/presets/v2fly/categories", {"GET"}, "core", "json-get", openapi=True),
    _route("/api/core/presets/v2fly/category-domains", {"GET"}, "core", "json-get", openapi=True),
    _route("/api/core/backups/create", {"POST"}, "core", "json-post", openapi=True),
    _route("/api/core/backups/list", {"GET"}, "core", "json-get", openapi=True),
    _route("/api/core/backups/restore", {"POST"}, "core", "json-post", openapi=True),
    _route("/api/core/backups/delete", {"POST"}, "core", "json-post", openapi=True),
    _route("/api/core/backups/download-file", {"GET"}, "core", "download", openapi=True),
    _route("/api/core/backups/upload", {"POST"}, "core", "upload", openapi=True),
    _route("/api/core/run-settings", {"GET"}, "core", "json-get", openapi=True),
    _route("/api/core/run-settings/save", {"POST"}, "core", "json-post", openapi=True),
    _route("/api/core/runs/history", {"GET"}, "core", "json-get", openapi=True),
    _route("/api/core/runs/latest-log", {"GET"}, "core", "json-get", openapi=True),
    _route("/api/core/strategy-candidates", {"GET"}, "core", "json-get", openapi=True),
    _route("/api/core/strategy-candidates/export", {"GET", "HEAD"}, "core", "ndjson-stream", openapi=True),
    _route("/api/core/events", {"GET"}, "core", "json-get", openapi=True),
    _route("/api/service/status", {"GET"}, "service", "json-get", openapi=True),
    _route("/api/service/releases/available", {"GET"}, "service", "json-get", openapi=True),
    _route("/api/service/releases/install-channel", {"GET"}, "service", "json-get", openapi=True),
    _route("/api/service/releases/set-install-channel", {"POST"}, "service", "json-post", openapi=True),
    _route("/api/service/releases/install", {"POST"}, "service", "json-post", openapi=True),
    _route("/api/service/v2fly/local-storage-status", {"GET"}, "service", "json-get", openapi=True),
    _route("/api/service/v2fly/check-updates", {"POST"}, "service", "json-post", openapi=True),
    _route("/api/service/v2fly/update-local-storage", {"POST"}, "service", "json-post", openapi=True),
    _route("/api/web/run-preferences", {"GET", "POST", "HEAD"}, "web", "json-get-post", openapi=True),
    _route("/api/web/runs/history-page", {"GET"}, "web", "json-get", openapi=True),
    _route("/api/web/candidate-domain-index-page", {"GET"}, "web", "json-get", openapi=True),
    _route("/api/web/strategy-candidates-page", {"GET"}, "web", "json-get", openapi=True),
    _route("/api/web/events", {"GET"}, "web", "json-get", openapi=True),
    _route("/api/status", {"GET", "HEAD"}, "legacy", "manual-json"),
    _route("/api/events", {"GET", "HEAD"}, "legacy", "sse"),
    _route("/api/settings", {"GET", "POST", "HEAD"}, "legacy", "manual-json"),
    _route("/api/run-preferences", {"GET", "POST", "HEAD"}, "legacy", "manual-json"),
    _route("/api/releases", {"GET", "HEAD"}, "legacy", "manual-json"),
    _route("/api/releases/update-plan", {"GET", "HEAD"}, "legacy", "manual-json"),
    _route("/api/releases/update", {"POST", "HEAD"}, "legacy", "manual-json"),
    _route("/api/diagnostics", {"GET", "HEAD"}, "legacy", "manual-json"),
    _route("/api/strategy-finder/domains", {"GET", "HEAD"}, "legacy", "manual-json"),
    _route("/api/strategy-finder/candidate-domains", {"GET", "HEAD"}, "legacy", "manual-json"),
    _route("/api/strategy-finder/candidates", {"GET", "HEAD"}, "legacy", "manual-json"),
    _route("/api/strategy-finder/runs", {"GET", "HEAD"}, "legacy", "manual-json"),
    _route("/api/strategy-finder/latest-log", {"GET", "HEAD"}, "legacy", "manual-json"),
    _route("/api/backups", {"GET", "HEAD"}, "legacy", "manual-json"),
    _route("/api/backups/create", {"POST"}, "legacy", "manual-json"),
    _route("/api/backups/restore", {"POST"}, "legacy", "manual-json"),
    _route("/api/backups/delete", {"POST"}, "legacy", "manual-json"),
    _route("/api/backups/upload", {"POST"}, "legacy", "upload"),
    _route("/api/backups/restore-preview", {"GET", "HEAD"}, "legacy", "manual-json"),
    _route("/api/backups/download", {"GET"}, "legacy", "download"),
    _route("/api/presets", {"GET", "POST", "HEAD"}, "legacy", "manual-json"),
    _route("/api/presets/domains", {"GET", "HEAD"}, "legacy", "manual-json"),
    _route("/api/presets/save", {"POST", "HEAD"}, "legacy", "manual-json"),
    _route("/api/presets/delete", {"POST", "HEAD"}, "legacy", "manual-json"),
    _route("/api/presets/delete-users-lists", {"POST", "HEAD"}, "legacy", "manual-json"),
    _route("/api/presets/domain-enabled", {"POST"}, "legacy", "manual-json"),
    _route("/api/domain-sources", {"GET", "HEAD"}, "legacy", "manual-json"),
    _route("/api/domain-sources/v2fly/categories", {"GET", "HEAD"}, "legacy", "manual-json"),
    _route("/api/domain-sources/v2fly/preview", {"POST"}, "legacy", "manual-json"),
    _route("/api/domain-sources/v2fly/import", {"POST"}, "legacy", "manual-json"),
    _route("/api/jobs/stop-current", {"POST"}, "legacy", "manual-json"),
    _route("/api/jobs/zapret-standard-discovery", {"POST"}, "legacy", "job"),
    _route("/api/jobs/zapret-multi-domain-discovery", {"POST"}, "legacy", "job"),
)

_ROUTE_INDEX = {(method, spec.path): spec for spec in ROUTES for method in spec.methods}
JSON_GET_ROUTE_PATHS = frozenset(
    spec.path for spec in ROUTES if "GET" in spec.methods and spec.dispatch in {"json-get", "json-get-post"}
)
JSON_POST_ROUTE_PATHS = frozenset(
    spec.path for spec in ROUTES if "POST" in spec.methods and spec.dispatch in {"json-post", "json-get-post"}
)
JSON_HEAD_ROUTE_PATHS = frozenset(
    spec.path for spec in ROUTES if "HEAD" in spec.methods and spec.dispatch in {"manual-json", "json-get-post"}
)
UPLOAD_ROUTE_PATHS = frozenset(spec.path for spec in ROUTES if spec.dispatch == "upload")
