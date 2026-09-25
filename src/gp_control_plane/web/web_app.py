"""Bottle application factory for the Web-to-Core boundary."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from http.client import IncompleteRead
from http import HTTPStatus
from typing import Any, Protocol

from bottle import Bottle, HTTPResponse, request, response

from ..resource_budget import PROXY_STREAM_CHUNK_BYTES
from .core_client import CoreClient, CoreResponse, CoreUnavailableError, hop_by_hop_header_names
from .cheroot_connection import WebStreamLifecycle
from .docs import (
    OPENAPI_JSON_CONTENT_TYPE,
    SWAGGER_HTML_CONTENT_TYPE,
    SWAGGER_PATHS,
    openapi_json_bytes,
    swagger_ui_html,
)
from .errors import error_payload, normalize_error_payload
from .http_body import ProxyRequestBodyMalformed, ProxyRequestBodyTooLarge, read_request_body
from .legacy_http import unsupported_method
from .stream_admission import Permit, StreamAdmission, stream_kind
from .routes import route_for
from .ui import index_html


PROXY_CORE_NAMESPACES = frozenset({"auth", "core", "service"})


class AuthAdapter(Protocol):
    """Narrow Core-owned Bearer check used only for unknown protected paths."""

    def __call__(self, headers: dict[str, str]) -> CoreResponse: ...


class _WebBottle(Bottle):
    """Bottle app that commits genuine SSE headers before Core's first frame."""

    def wsgi(self, environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        is_event_stream = environ.get("REQUEST_METHOD") == "GET" and environ.get("PATH_INFO") == "/api/web/events/stream"
        if not is_event_stream:
            return super().wsgi(environ, start_response)
        out = self._handle(environ)
        try:
            # Keep Bottle's file-like wrapper: it does not peek Core's first
            # frame. Retain ownership until the WSGI caller gets the iterable.
            out = self._cast(out)
            exc_info = environ.pop("bottle.exc_info", None)
            write = start_response(response._wsgi_status_line(), response.headerlist, exc_info)
            write(b"")
            return out
        except BaseException as error:
            try:
                close = getattr(out, "close", None)
                if close is not None:
                    close()
            except BaseException as close_error:
                raise error from close_error
            raise


@dataclass(frozen=True)
class WebAssets:
    """Injected static resource readers; construction has no listener effects."""

    index: Callable[[], str] = index_html
    swagger: Callable[[], str] = swagger_ui_html
    openapi: Callable[[], bytes] = openapi_json_bytes


def core_auth_adapter(core_client: CoreClient) -> AuthAdapter:
    """Make the only Web-side token check a current Core API operation."""

    def verify(headers: dict[str, str]) -> CoreResponse:
        return core_client.open("GET", "/api/internal/auth/verify-bearer", headers=headers)

    return verify


def create_web_app(
    core_client: CoreClient,
    auth_adapter: AuthAdapter | None = None,
    assets: WebAssets | None = None,
    stream_lifecycle: WebStreamLifecycle | None = None,
) -> Bottle:
    """Create the WSGI application only; it never binds, runs, or recovers."""

    verify_bearer = auth_adapter or core_auth_adapter(core_client)
    web_assets = assets or WebAssets()
    app = _WebBottle()
    admission = StreamAdmission()

    @app.route("/", method=("GET", "HEAD"))
    def root() -> HTTPResponse:
        return _static_response(web_assets.index().encode("utf-8"), "text/html; charset=utf-8", no_store=True)

    @app.route("/swagger", method=("GET", "HEAD"))
    @app.route("/swagger/", method=("GET", "HEAD"))
    def swagger() -> HTTPResponse:
        return _static_response(web_assets.swagger().encode("utf-8"), SWAGGER_HTML_CONTENT_TYPE, no_store=True)

    @app.route("/openapi.json", method=("GET", "HEAD"))
    def openapi() -> HTTPResponse:
        try:
            data = web_assets.openapi()
        except OSError:
            return _json_response({"error": "openapi contract is not available"}, HTTPStatus.NOT_FOUND)
        return _static_response(data, OPENAPI_JSON_CONTENT_TYPE, no_store=True)

    @app.route("/<path:path>", method="ANY")
    @app.route("/", method="ANY")
    def all_routes(path: str = "") -> HTTPResponse | Iterable[bytes]:
        return _dispatch(
            core_client,
            verify_bearer,
            "/" + path,
            request.method.upper(),
            request.query_string,
            request.environ,
            _forward_headers(request.headers),
            stream_lifecycle,
            admission,
        )

    return app


def _dispatch(
    core_client: CoreClient,
    verify_bearer: AuthAdapter,
    path: str,
    method: str,
    query: str,
    environ: Mapping[str, object],
    headers: dict[str, str],
    stream_lifecycle: WebStreamLifecycle | None,
    admission: StreamAdmission,
) -> HTTPResponse | Iterable[bytes]:
    if method not in {"GET", "HEAD", "POST"}:
        return unsupported_method(method)

    if path in SWAGGER_PATHS or path == "/openapi.json" or path == "/":
        # These paths have exact routes above; this protects direct WSGI calls.
        return _json_response({"error": "not found"}, HTTPStatus.NOT_FOUND)

    if path.startswith("/api/web/"):
        route = route_for(method, path)
        if not route or route.namespace != "web":
            return _verify_then_not_found(verify_bearer, headers)
        return _open_and_relay(
            lambda body: core_client.open_web_view(method, path, query=query, body=body, headers=headers),
            path,
            method,
            environ,
            stream_lifecycle,
            admission, verify_bearer, headers,
        )

    route = route_for(method, path)
    if route and (route.namespace in PROXY_CORE_NAMESPACES or (route.namespace == "openapi" and path != "/openapi.json")):
        return _open_and_relay(
            lambda body: core_client.open(method, path, query=query, body=body, headers=headers),
            path,
            method,
            environ,
            stream_lifecycle,
            admission, verify_bearer, headers,
        )

    if path.startswith("/api/"):
        return _verify_then_not_found(verify_bearer, headers)
    return _json_response({"error": "not found"}, HTTPStatus.NOT_FOUND)


def _open_and_relay(
    opener: Callable[[bytes | None], CoreResponse],
    path: str,
    method: str,
    environ: Mapping[str, object],
    stream_lifecycle: WebStreamLifecycle | None,
    admission: StreamAdmission,
    verify_bearer: AuthAdapter,
    headers: dict[str, str],
) -> HTTPResponse | Iterable[bytes]:
    permit = None
    try:
        kind = stream_kind(method, path)
        if kind is not None:
            # Core remains the sole auth/state owner, including 401/503.
            verified = verify_bearer(headers)
            if verified.status != HTTPStatus.OK:
                return _relay_core_response(verified, method)
            verified.close()
            permit = admission.acquire(kind)
            if permit is None:
                return _json_response(error_payload("stream_capacity_exhausted", "Stream capacity is temporarily exhausted."), HTTPStatus.SERVICE_UNAVAILABLE)
        upstream = opener(read_request_body(environ, path))
        result = _relay_core_response(upstream, method, stream_lifecycle, _stream_peer(environ), permit)
        permit = None  # the file-like relay now owns release, including abort
        return result
    except ProxyRequestBodyTooLarge as error:
        return _json_response({"error": str(error)}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
    except ProxyRequestBodyMalformed as error:
        return _json_response({"error": str(error)}, HTTPStatus.BAD_REQUEST)
    except CoreUnavailableError as error:
        return _core_unavailable(error)
    finally:
        if permit is not None:
            permit.release()


def _verify_then_not_found(verify_bearer: AuthAdapter, headers: dict[str, str]) -> HTTPResponse | Iterable[bytes]:
    try:
        upstream = verify_bearer(headers)
    except CoreUnavailableError as error:
        return _core_unavailable(error)
    if upstream.status != HTTPStatus.OK:
        return _relay_core_response(upstream, request.method.upper())
    upstream.close()
    return _json_response({"error": "not found"}, HTTPStatus.NOT_FOUND)


def _relay_core_response(
    upstream: CoreResponse,
    method: str,
    stream_lifecycle: WebStreamLifecycle | None = None,
    peer: tuple[str, int] | None = None,
    permit: Permit | None = None,
) -> Iterable[bytes]:
    """Copy an authorized Core response without introducing hop-by-hop state."""

    response.status = upstream.status
    upstream_headers = tuple(upstream.headers())
    excluded = hop_by_hop_header_names(upstream_headers)
    for key, value in upstream_headers:
        if key.lower() not in excluded:
            response.add_header(key, value)

    if method == "HEAD":
        try:
            upstream.response.read()
        finally:
            upstream.close()
        return ()

    return _CoreResponseStream(upstream, stream_lifecycle, peer, permit)


class _CoreResponseStream:
    """File-like relay that lets Bottle commit SSE headers before reading Core."""

    def __init__(
        self,
        upstream: CoreResponse,
        stream_lifecycle: WebStreamLifecycle | None,
        peer: tuple[str, int] | None,
        permit: Permit | None = None,
    ) -> None:
        self._upstream = upstream
        self._permit = permit
        self._closed = False
        self._stream_lifecycle = stream_lifecycle
        self._peer = peer
        self._registered_abort: Callable[[], None] | None = None
        self._stream_id: int | None = None
        if stream_lifecycle is not None:
            self._registered_abort = self.abort
            self._stream_id = stream_lifecycle.register(peer, self._registered_abort)

    def read(self, size: int = PROXY_STREAM_CHUNK_BYTES) -> bytes:
        if self._closed:
            return b""
        upstream = self._upstream
        if upstream is None:
            return b""
        try:
            upstream_response = upstream.response
            if upstream_response is None:
                return b""
            reader = getattr(upstream_response, "read1", upstream_response.read)
            chunk = reader(size if size > 0 else PROXY_STREAM_CHUNK_BYTES)
            if not chunk:
                if upstream.abort_pending:
                    upstream.finish_abort()
                self.close()
            return chunk
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            if upstream.abort_pending:
                upstream.finish_abort()
            self.close()
            return b""
        except IncompleteRead:
            # A real downstream RST can wake the Web worker after the
            # listener-owned lifecycle has already shut down this exact Core
            # transport.  Python's chunked HTTPResponse reports that wake as
            # IncompleteRead(0), rather than an OSError.  It is terminal only
            # for the explicit pending abort: an independent truncated Core
            # response remains observable instead of becoming a false EOF.
            if upstream.abort_pending:
                upstream.finish_abort()
                self.close()
                return b""
            if self._closed:
                # ``CoreResponse.abort`` can complete an already-exhausted
                # listener transport before its blocked reader reports the
                # wake-up.  The response then has no pending resources left,
                # but this *same* lifecycle-owned stream was atomically
                # closed by ``abort``.  Do not let its late zero-byte chunk
                # parse escape as a Cheroot traceback.
                return b""
            self.close()
            raise
        except OSError:
            # A known listener-owned abort may wake HTTPResponse.read as a
            # plain OSError on Linux.  Do not turn arbitrary upstream I/O
            # failures into an EOF; only the already-confirmed peer abort has
            # a deferred reader-side close to complete here.
            if upstream.abort_pending:
                upstream.finish_abort()
                self.close()
                return b""
            self.close()
            raise
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self._finish_close(abort=False)

    def abort(self) -> None:
        """Release Core's live response because this exact Web peer closed."""

        self._finish_close(abort=True)

    def _finish_close(self, *, abort: bool) -> None:
        if self._closed:
            return
        self._closed = True
        # Terminal EOF and client abort both release the finite Core client.
        upstream = self._upstream
        self._upstream = None
        lifecycle = self._stream_lifecycle
        peer = self._peer
        registered_abort = self._registered_abort
        stream_id = self._stream_id
        # ``self.abort`` is a bound method.  Keeping it on the stream after
        # the listener removes its callback would form a self-cycle, delaying
        # Windows lock/semaphore release until an unrelated GC run.
        self._stream_lifecycle = None
        self._peer = None
        self._registered_abort = None
        self._stream_id = None
        try:
            if upstream is not None:
                if abort:
                    upstream.abort()
                else:
                    upstream.close()
        finally:
            permit = self._permit
            self._permit = None
            if permit is not None:
                permit.release()
            if lifecycle is not None and registered_abort is not None and stream_id is not None:
                lifecycle.release(stream_id, peer, registered_abort)


def _stream_peer(environ: Mapping[str, object]) -> tuple[str, int] | None:
    address = environ.get("REMOTE_ADDR")
    port = environ.get("REMOTE_PORT")
    try:
        return (str(address), int(str(port)))
    except (TypeError, ValueError):
        return None


def _forward_headers(incoming: Mapping[str, str]) -> dict[str, str]:
    excluded = hop_by_hop_header_names(incoming)
    headers = {key: value for key, value in incoming.items() if key.lower() not in excluded and key.lower() != "host"}
    headers["X-Forwarded-Host"] = incoming.get("Host") or ""
    headers["X-Forwarded-Proto"] = "http"
    return headers


def _static_response(data: bytes, content_type: str, *, no_store: bool) -> HTTPResponse:
    headers = {"Content-Type": content_type, "Content-Length": str(len(data))}
    if no_store:
        headers["Cache-Control"] = "no-store"
    return HTTPResponse(status=HTTPStatus.OK, body=b"" if request.method.upper() == "HEAD" else data, headers=headers)


def _json_response(payload: dict[str, Any], status: HTTPStatus) -> HTTPResponse:
    normalized = normalize_error_payload(payload, status)
    data = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return HTTPResponse(
        status=status,
        body=b"" if request.method.upper() == "HEAD" else data,
        headers={"Content-Type": "application/json; charset=utf-8", "Content-Length": str(len(data))},
    )


def _core_unavailable(error: CoreUnavailableError) -> HTTPResponse:
    return _json_response({"error": "core api is unavailable", "detail": str(error)}, HTTPStatus.BAD_GATEWAY)
