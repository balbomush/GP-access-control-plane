"""Narrow HTTP client used by the Web listener to reach the Core listener."""

from __future__ import annotations

import errno
import http.client
import socket
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ..application.web_view_contracts import internal_web_view_path


PROXY_SKIP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


def hop_by_hop_header_names(headers: Mapping[str, str] | Iterable[tuple[str, str]]) -> frozenset[str]:
    """Return RFC hop-by-hop names, including tokens nominated by Connection."""
    header_items = getattr(headers, "items", None)
    pairs = header_items() if callable(header_items) else headers
    excluded = set(PROXY_SKIP_HEADERS)
    for key, value in pairs:
        if key.lower() != "connection":
            continue
        excluded.update(token.strip().lower() for token in value.split(",") if token.strip())
    return frozenset(excluded)


class CoreUnavailableError(OSError):
    """The configured Core listener cannot accept the Web request."""


@dataclass
class CoreResponse:
    connection: http.client.HTTPConnection | None
    response: http.client.HTTPResponse | None
    response_socket: socket.socket | None = field(default=None, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _abort_resources: tuple[http.client.HTTPConnection, http.client.HTTPResponse, socket.socket | None] | None = field(
        default=None, init=False, repr=False
    )

    @property
    def status(self) -> int:
        return int(self._require_response().status)

    @property
    def reason(self) -> str:
        return str(self._require_response().reason or "")

    def headers(self) -> Iterable[tuple[str, str]]:
        return self._require_response().getheaders()

    def close(self) -> None:
        resources = self._claim_close_resources()
        if resources is None:
            return
        self._finish_close(*resources)

    def abort(self) -> None:
        """Interrupt a live Core read after the owning Web peer has gone.

        ``HTTPConnection.close`` alone does not reliably wake another thread
        blocked in ``HTTPResponse.read`` on Windows.  This path belongs only
        to the listener's confirmed client-socket close; normal EOF uses
        :meth:`close` unchanged.  It never treats EBADF or arbitrary OSErrors
        as a successful peer abort.
        """

        resources = self._claim_close_resources()
        if resources is None:
            return
        connection, response, transport_socket = resources
        self._abort_resources = resources
        if transport_socket is not None:
            try:
                transport_socket.shutdown(socket.SHUT_RDWR)
            except OSError as error:
                error_number = getattr(error, "errno", None)
                if error_number in {errno.ENOTSOCK, 10038}:
                    # This fixed, listener-owned stream entry can race the
                    # normal finite response close.  WSAENOTSOCK/ENOTSOCK is
                    # narrower than EBADF: the socket wrapper is already no
                    # longer a socket, so there is no TCP read left for the
                    # worker to wake.  Finish the claimed response now; do
                    # not leave an orphaned abort-pending entry at shutdown.
                    self.finish_abort()
                    return
                if error_number not in {errno.ENOTCONN, 10057}:
                    try:
                        self.finish_abort()
                    except BaseException as close_error:
                        raise error from close_error
                    raise
            # HTTPResponse.makefile can retain the descriptor even after
            # socket.close(); Windows timed reads then remain blocked despite
            # shutdown. Transfer this exact owned descriptor out of its socket
            # wrapper before closing it via the public socket API. The reader
            # sees EOF/OSError and finishes its buffered response itself; its
            # later close cannot close a reused descriptor through this wrapper.
            detach = getattr(transport_socket, "detach", None)
            if detach is None:
                # Narrow socket-shaped transport doubles used by adapter tests.
                transport_socket.close()
            else:
                descriptor = detach()
                if descriptor >= 0:
                    socket.close(descriptor)

    @property
    def abort_pending(self) -> bool:
        """Whether a confirmed downstream abort still needs reader-side close."""

        return self._abort_resources is not None

    def finish_abort(self) -> None:
        """Close an aborted Core response from the thread that left ``read``.

        Closing ``HTTPResponse`` from the listener stop thread can wait on the
        same buffered reader another Cheroot worker is blocked in.  The owning
        socket shutdown wakes that read; its worker then performs the normal
        response/connection close without a cross-thread reader lock cycle.
        """

        resources = self._abort_resources
        if resources is None:
            return
        self._abort_resources = None
        self._finish_close(*resources)

    def _claim_close_resources(
        self,
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse, socket.socket | None] | None:
        # Both caller paths run under CPython's GIL.  A boolean claim avoids
        # allocating a Windows semaphore for every finite Core response while
        # preserving idempotent abort/EOF cleanup.
        if self._closed:
            return None
        self._closed = True
        connection, response, response_socket = self.connection, self.response, self.response_socket
        # The WSGI response object can outlive its closed stream until Bottle
        # releases the iterable.  Clear its Core-client references now so
        # Windows buffered-reader synchronization handles are released at
        # the deterministic close point, rather than at a later GC cycle.
        self.connection = None
        self.response = None
        self.response_socket = None
        if connection is None or response is None:
            raise RuntimeError("Core response is missing close resources")
        # HTTPConnection.getresponse() closes its public ``sock`` reference
        # for a close-delimited Core response, while HTTPResponse keeps
        # reading the same already-established socket.  Capture that public
        # connection socket before getresponse so a confirmed Web abort can
        # always wake that read during listener shutdown.
        return connection, response, response_socket or connection.sock

    def _require_response(self) -> http.client.HTTPResponse:
        response = self.response
        if response is None:
            raise RuntimeError("Core response is closed")
        return response

    @staticmethod
    def _finish_close(
        connection: http.client.HTTPConnection,
        response: http.client.HTTPResponse,
        _transport_socket: socket.socket | None,
    ) -> None:
        try:
            response.close()
        finally:
            connection.close()


class CoreClient:
    """Forward finite requests with caller Authorization to the Core listener."""

    def __init__(self, core_url: str, *, timeout_seconds: float = 30.0):
        core = urlparse(core_url)
        if core.scheme not in {"http", "https"} or not core.hostname:
            raise ValueError("core_url must be an http(s) URL with host")
        self._scheme = core.scheme
        self._host = core.hostname
        self._port = core.port or (443 if core.scheme == "https" else 80)
        self._netloc = core.netloc
        self._base = core.path.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def web_view_path(self, external_path: str) -> str:
        return internal_web_view_path(external_path)

    def open(
        self,
        method: str,
        path: str,
        *,
        query: str = "",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> CoreResponse:
        target = f"{self._base}{path}"
        if query:
            target = f"{target}?{query}"
        excluded = hop_by_hop_header_names(headers or {})
        forwarded = {
            key: value
            for key, value in (headers or {}).items()
            if key.lower() not in excluded and key.lower() != "host"
        }
        forwarded["Host"] = forwarded.get("Host") or self._netloc
        # Web already bounded and validated this body. Let Cheroot consume it
        # before an early auth rejection: closing with unread request bytes
        # can reset TCP and lose the otherwise complete 401. This remains a
        # one-shot connection owned and closed by CoreResponse, never a pool.
        forwarded["Connection"] = "keep-alive" if body else "close"
        connection_class = http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
        connection = connection_class(self._host, self._port, timeout=self._timeout_seconds)
        try:
            connection.request(method, target, body=body, headers=forwarded)
            # Keep only the public socket property.  getresponse() can clear
            # connection.sock for a close-delimited stream, although the
            # HTTPResponse reader remains blocked on this exact transport.
            response_socket = connection.sock
            return CoreResponse(
                connection=connection,
                response=connection.getresponse(),
                response_socket=response_socket,
            )
        except OSError as error:
            connection.close()
            raise CoreUnavailableError(str(error)) from error

    def open_web_view(
        self,
        method: str,
        external_path: str,
        *,
        query: str = "",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> CoreResponse:
        return self.open(method, self.web_view_path(external_path), query=query, body=body, headers=headers)
