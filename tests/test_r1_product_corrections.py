"""Focused application/real HTTP coverage for authorized PROD-01/02/03."""

from __future__ import annotations

import contextlib
import errno
import http.client
import io
import json
import select
import socket
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from wsgiref.util import setup_testing_defaults

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.auth import AuthenticationError
from gp_control_plane.config import AppConfig, OutputConfig
from gp_control_plane.web import core_app, docs
from gp_control_plane.web.cheroot_connection import WebStreamLifecycle
from gp_control_plane.web.core_client import CoreClient, CoreResponse
from gp_control_plane.web.http_runtime import WebHttpRuntime
from gp_control_plane.web.legacy_http import unsupported_method
from gp_control_plane.web.stream_admission import ClosingIterator, StreamAdmission, stream_kind
from gp_control_plane.web.web_app import _CoreResponseStream, create_web_app
from test_a6_core_transport import _free_port, _request, _wait_for_listener


@contextlib.contextmanager
def listeners():
    """Real Core/Web transports; synthetic app data, no recovery or helper."""
    with tempfile.TemporaryDirectory() as raw:
        state = {"storage_error": False, "revoked": False}

        def auth(_state, token):
            if state["storage_error"]:
                raise PermissionError("fixture unavailable")
            if token != "Bearer valid" or state["revoked"]:
                raise AuthenticationError("fixture invalid token")

        config = AppConfig(output=OutputConfig(state_dir=Path(raw)))
        core_lifecycle, web_lifecycle = WebStreamLifecycle(), WebStreamLifecycle()
        services = core_app.CoreServices(config, mock.Mock(), ui_enabled=True, stream_lifecycle=core_lifecycle)
        core_port, web_port = _free_port(), _free_port()
        core = WebHttpRuntime(core_app.create_core_app(services, auth), "127.0.0.1", core_port, stream_lifecycle=core_lifecycle)
        web = WebHttpRuntime(create_web_app(CoreClient(f"http://127.0.0.1:{core_port}"), stream_lifecycle=web_lifecycle), "127.0.0.1", web_port, stream_lifecycle=web_lifecycle)
        threads = [threading.Thread(target=runtime.serve_forever, daemon=True) for runtime in (core, web)]
        with mock.patch.object(core_app, "_core_get", return_value=core_app.HTTPResponse(body=b'{}', headers={"Content-Type": "application/json", "Content-Length": "2"})), mock.patch("gp_control_plane.web.api_server._event_payloads", return_value={"fixture": {"ok": True}}):
            for thread, port in zip(threads, (core_port, web_port)):
                thread.start()
                _wait_for_listener(port)
            try:
                yield core_port, web_port, services, state, core, web
            finally:
                web.stop()
                core.stop()
                for thread in threads:
                    thread.join(5)
                    if thread.is_alive():
                        raise AssertionError("fixture listener did not stop")


class ProductCorrectionTests(unittest.TestCase):
    def test_abort_detaches_owned_descriptor_once_and_preserves_close_errors(self):
        for fail_close in (False, True):
            with self.subTest(fail_close=fail_close):
                events = []
                class OwnedSocket:
                    def shutdown(self, how):
                        events.append(("shutdown", how))
                    def detach(self):
                        events.append(("detach", 424242))
                        return 424242
                    def close(self):
                        raise AssertionError("detached socket wrapper must not close descriptor")
                response = mock.Mock()
                response.close.side_effect = lambda: events.append(("response-close", None))
                connection = mock.Mock()
                connection.sock = None
                connection.close.side_effect = lambda: events.append(("connection-close", None))
                upstream = CoreResponse(connection, response, OwnedSocket())
                unexpected = OSError(errno.EIO, "descriptor close failure")
                def close(descriptor):
                    events.append(("descriptor-close", descriptor))
                    if fail_close:
                        raise unexpected
                with mock.patch("gp_control_plane.web.core_client.socket.close", side_effect=close):
                    if fail_close:
                        with self.assertRaises(OSError) as caught:
                            upstream.abort()
                        self.assertIs(caught.exception, unexpected)
                    else:
                        upstream.abort()
                    upstream.abort()
                    upstream.close()
                    self.assertEqual(events, [("shutdown", socket.SHUT_RDWR), ("detach", 424242), ("descriptor-close", 424242)])
                    self.assertTrue(upstream.abort_pending)
                    upstream.finish_abort()
                    upstream.finish_abort()
                self.assertEqual(events[-2:], [("response-close", None), ("connection-close", None)])
                self.assertFalse(upstream.abort_pending)

    def test_segmented_body_auth_rejection_is_complete_and_connection_is_single_use(self):
        observed = []
        base_connection = http.client.HTTPConnection
        class SegmentedConnection(base_connection):
            def putheader(self, header, *values):
                if header.lower() == "connection":
                    observed.append(("connection", values[0]))
                return super().putheader(header, *values)

            def send(self, data):
                if data == b"{}":
                    # Send headers first. A body-bearing rejection must wait
                    # for its two already-bounded request bytes before replying.
                    observed.append(("reply_before_body", bool(select.select([self.sock], [], [], 0.1)[0])))
                return super().send(data)

        with listeners() as (port, *_), mock.patch("gp_control_plane.web.core_client.http.client.HTTPConnection", SegmentedConnection), mock.patch.object(core_app, "_post") as action:
            client = CoreClient(f"http://127.0.0.1:{port}")
            upstream = client.open("POST", "/api/core/strategy-discovery/stop-current-run", body=b"{}", headers={"Content-Type": "application/json"})
            owned_socket = upstream.response_socket
            connection = upstream.connection
            try:
                self.assertEqual(upstream.status, 401)
                headers = dict((key.lower(), value) for key, value in upstream.headers())
                body = upstream.response.read()
                self.assertEqual(len(body), int(headers["content-length"]))
                self.assertEqual(json.loads(body)["error"]["code"], "authentication_required")
                self.assertEqual(headers["www-authenticate"], "Bearer")
            finally:
                upstream.close()
                upstream.close()
            self.assertEqual(observed, [("connection", "keep-alive"), ("reply_before_body", False)])
            self.assertIsNone(connection.sock)
            self.assertEqual(owned_socket.fileno(), -1)
            self.assertIsNone(upstream.connection)
            action.assert_not_called()
            observed.clear()
            upstream = client.open("GET", "/api/web/events/stream")
            try:
                self.assertEqual(upstream.status, 401)
                upstream.response.read()
            finally:
                upstream.close()
            self.assertEqual(observed, [("connection", "close")])

    def test_two_real_sse_readers_shutdown_without_token_rotation(self):
        with listeners() as (_, port, services, state, core, web):
            connections, replies = [], []
            try:
                for _ in range(2):
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                    connections.append(connection)
                    connection.request("GET", "/api/web/events/stream", headers={"Authorization": "Bearer valid"})
                    reply = connection.getresponse()
                    replies.append(reply)
                    self.assertIn(b"event:", reply.readline())
                for reply in replies:
                    reply.close()
                for connection in connections:
                    connection.close()
                stopped = threading.Thread(target=web.stop, daemon=True)
                started = time.monotonic()
                stopped.start()
                stopped.join(3)
                clean = not stopped.is_alive()
                if not clean:
                    # Test teardown only, never used to make the assertion pass.
                    state["revoked"] = True
                    stopped.join(5)
                self.assertTrue(clean, "Web shutdown did not interrupt both Core readers")
                self.assertLess(time.monotonic() - started, 3)
                self.assertEqual(web._stream_lifecycle.active_count(), 0)
            finally:
                state["revoked"] = True
                for reply in replies:
                    reply.close()
                for connection in connections:
                    connection.close()

    def test_early_sse_start_response_and_write_failure_close_before_handoff(self):
        def environ():
            value = {}
            setup_testing_defaults(value)
            value.update(PATH_INFO="/api/web/events/stream", HTTP_AUTHORIZATION="Bearer valid", REMOTE_ADDR="127.0.0.1", REMOTE_PORT="43210")
            return value

        for failure_at in ("start", "write"):
            with self.subTest(failure_at=failure_at), tempfile.TemporaryDirectory() as raw:
                lifecycle = WebStreamLifecycle()
                services = core_app.CoreServices(AppConfig(output=OutputConfig(state_dir=Path(raw))), mock.Mock(), ui_enabled=True, stream_lifecycle=lifecycle)
                core = core_app.create_core_app(services, lambda *_: None)
                web_lifecycle = WebStreamLifecycle()
                upstreams = []
                def open_stream(*args, **kwargs):
                    upstream = mock.Mock(spec=CoreResponse)
                    upstream.status = 200
                    upstream.headers.return_value = [("Content-Type", "text/event-stream")]
                    upstream.response = io.BytesIO(b"event: fixture\ndata: {}\n\n")
                    upstream.abort_pending = False
                    upstreams.append(upstream)
                    return upstream
                client = mock.Mock(spec=CoreClient)
                client.open_web_view.side_effect = open_stream
                verified = mock.Mock(spec=CoreResponse)
                verified.status = 200
                web = create_web_app(client, lambda _: verified, stream_lifecycle=web_lifecycle)
                failure = BrokenPipeError("header handoff failed")
                def fail_start(*_):
                    if failure_at == "start":
                        raise failure
                    def write(_):
                        raise failure
                    return write
                for app, owner in ((core, lifecycle), (web, web_lifecycle)):
                    for _ in range(2):
                        with self.assertRaises(BrokenPipeError) as caught:
                            app(environ(), fail_start)
                        self.assertIs(caught.exception, failure)
                        owner.close_peer(("127.0.0.1", 43210))
                        self.assertEqual(owner.active_count(), 0)
                    statuses = []
                    def start(status, headers, exc_info=None):
                        statuses.append(status)
                        return lambda _: None
                    stream = app(environ(), start)
                    self.assertTrue(statuses[0].startswith("200"), statuses)
                    stream.close()
                    self.assertEqual(owner.active_count(), 0)
                permits = [services.stream_admission.acquire("sse") for _ in range(2)]
                self.assertTrue(all(permits))
                for permit in permits:
                    permit.release()
                self.assertTrue(all(item.close.call_count == 1 for item in upstreams))

    def test_headless_public_sse_stays_404_after_auth_when_internal_slots_full(self):
        with tempfile.TemporaryDirectory() as raw:
            services = core_app.CoreServices(AppConfig(output=OutputConfig(state_dir=Path(raw))), mock.Mock())
            permits = [services.stream_admission.acquire("sse") for _ in range(2)]
            def auth(_state, token):
                if token != "Bearer valid":
                    raise AuthenticationError("invalid")
            app = core_app.create_core_app(services, auth)
            for path, token, expected in (("/api/web/events/stream", "Bearer valid", 404), ("/api/web/events/stream", "", 401), ("/api/internal/web-events/stream", "Bearer valid", 503)):
                environ = {}
                setup_testing_defaults(environ)
                environ.update(PATH_INFO=path, HTTP_AUTHORIZATION=token)
                statuses = []
                def start(status, headers, exc_info=None):
                    statuses.append(status)
                    return lambda _: None
                result = app(environ, start)
                try:
                    list(result)
                    self.assertEqual(int(statuses[0].split()[0]), expected)
                finally:
                    if hasattr(result, "close"):
                        result.close()
            for permit in permits:
                permit.release()

    def test_legacy_exact_body_headers_and_method_escaping(self):
        response = unsupported_method("PUT")
        self.assertEqual(len(response.body), 356)
        self.assertEqual(response.headers["Content-Type"], "text/html;charset=utf-8")
        self.assertEqual(response.headers["Content-Length"], "356")
        self.assertNotIn("Allow", response.headers)
        for method in ("PATCH", "DELETE", "X<&>"):
            result = unsupported_method(method)
            self.assertEqual(result.status_code, 501)
            self.assertEqual(int(result.headers["Content-Length"]), len(result.body))
        self.assertIn(b"X&lt;&amp;&gt;", unsupported_method("X<&>").body)

    def test_source_contract_and_core_filter_ignore_working_directory(self):
        contract = (Path(__file__).resolve().parents[1] / "openapi.json").read_bytes()
        self.assertEqual(docs.openapi_json_bytes(), contract)
        core = json.loads(docs.openapi_json_bytes(core_only=True))
        self.assertFalse(any(path.startswith("/api/web/") for path in core["paths"]))
        with mock.patch.object(docs, "__file__", "/tmp/site-packages/gp_control_plane/web/docs.py"), mock.patch.object(docs, "files") as resource:
            resource.return_value.joinpath.return_value.is_file.return_value = False
            with self.assertRaises(FileNotFoundError):
                docs.openapi_json_bytes()

    def test_admission_classes_unstarted_empty_exception_and_double_close(self):
        for method in ("HEAD", "POST"):
            self.assertIsNone(stream_kind(method, "/api/core/backups/download-archive"))
        self.assertEqual(stream_kind("GET", "/api/core/strategy-candidates/export"), "long")
        admission = StreamAdmission()
        first, second = admission.acquire("sse"), admission.acquire("sse")
        self.assertIsNone(admission.acquire("sse"))
        unstarted = ClosingIterator(iter([b"unused"]), first.release)
        unstarted.close()
        unstarted.close()
        empty = ClosingIterator(iter(()), second.release)
        self.assertEqual(list(empty), [])
        acquired = [admission.acquire("sse"), admission.acquire("sse")]
        self.assertTrue(all(acquired))
        self.assertIsNone(admission.acquire("sse"))
        for permit in acquired:
            permit.release()
        def fail():
            raise RuntimeError("visible stream failure")
            yield b""
        failed = ClosingIterator(fail(), admission.acquire("long").release)
        with self.assertRaisesRegex(RuntimeError, "visible stream failure"):
            next(failed)
        self.assertIsNotNone(admission.acquire("long"))
        self.assertIsNotNone(admission.acquire("long"))

    def test_relay_error_and_unstarted_abort_release_exactly_once(self):
        admission = StreamAdmission()
        for abort in (False, True):
            upstream = mock.Mock(spec=CoreResponse)
            upstream.response = io.BytesIO(b"")
            upstream.abort_pending = False
            stream = _CoreResponseStream(upstream, None, None, admission.acquire("sse"))
            (stream.abort if abort else stream.close)()
            stream.close()
        permits = [admission.acquire("sse"), admission.acquire("sse")]
        self.assertTrue(all(permits))
        for permit in permits:
            permit.release()
        upstream = mock.Mock(spec=CoreResponse)
        upstream.response = mock.Mock()
        upstream.response.read1.side_effect = OSError("unexpected failure")
        upstream.abort_pending = False
        stream = _CoreResponseStream(upstream, None, None, admission.acquire("sse"))
        with self.assertRaisesRegex(OSError, "unexpected failure"):
            stream.read()
        self.assertIsNotNone(admission.acquire("sse"))
        self.assertIsNotNone(admission.acquire("sse"))

    def test_core_unstarted_sse_close_releases_lifecycle_and_both_slots(self):
        with tempfile.TemporaryDirectory() as raw:
            services = core_app.CoreServices(AppConfig(output=OutputConfig(state_dir=Path(raw))), mock.Mock(), ui_enabled=True, stream_lifecycle=WebStreamLifecycle())
            core_app.request.bind({"REQUEST_METHOD": "GET"})
            core_app.response.bind()
            streams = [core_app._dispatch(services, lambda *_: None, "/api/web/events/stream", "GET", "", {}, {}) for _ in range(2)]
            self.assertEqual(services.stream_lifecycle.active_count(), 2)
            for stream in streams:
                stream.close()
                stream.close()
            self.assertEqual(services.stream_lifecycle.active_count(), 0)
            acquired = [services.stream_admission.acquire("sse") for _ in range(2)]
            self.assertTrue(all(acquired))
            self.assertIsNone(services.stream_admission.acquire("sse"))
            for permit in acquired:
                permit.release()

    def test_real_long_download_export_share_limit_and_release_on_completion(self):
        release = threading.Event()
        def download(*_):
            def chunks():
                yield b"a" * 4096
                if not release.wait(4):
                    raise RuntimeError("fixture stream not released")
                yield b"z"
            return core_app._stream_response(chunks(), "application/zip", headers={"Content-Length": "4097"})
        with mock.patch.object(core_app, "_download_backup", side_effect=download), listeners() as (core_port, web_port, services, state, core, web):
            connections, replies = [], []
            try:
                for _ in range(2):
                    connection = http.client.HTTPConnection("127.0.0.1", web_port, timeout=2)
                    connections.append(connection)
                    connection.request("GET", "/api/core/backups/download-archive?snapshot_id=fixture", headers={"Authorization": "Bearer valid"})
                    reply = connection.getresponse()
                    replies.append(reply)
                    self.assertEqual(reply.status, 200)
                    self.assertEqual(reply.read(4096), b"a" * 4096)
                for port in (core_port, web_port):
                    started = time.monotonic()
                    for path in ("/api/core/backups/download-archive", "/api/core/strategy-candidates/export"):
                        status, _, body = _request(port, "GET", path, headers={"Authorization": "Bearer valid"})
                        self.assertEqual(status, 503)
                        self.assertEqual(json.loads(body)["error"]["code"], "stream_capacity_exhausted")
                        self.assertEqual(_request(port, "GET", path)[0], 401)
                    self.assertEqual(_request(port, "GET", "/api/core/status", headers={"Authorization": "Bearer valid"})[0], 200)
                    self.assertLess(time.monotonic() - started, 2)
                release.set()
                for reply in replies:
                    self.assertEqual(reply.read(), b"z")
                # A fresh complete long response proves both listeners return permits.
                result = _request(web_port, "GET", "/api/core/backups/download-archive?snapshot_id=fixture", headers={"Authorization": "Bearer valid"})
                self.assertEqual(result[0], 200)
                self.assertEqual(result[2], b"a" * 4096 + b"z")
            finally:
                release.set()
                for reply in replies:
                    reply.close()
                for connection in connections:
                    connection.close()

    def test_real_sse_saturation_auth_precedence_short_requests_and_rotation(self):
        with listeners() as (core_port, web_port, services, state, core, web):
            for port in (core_port, web_port):
                for method, path in (("PUT", "/api/health"), ("PATCH", "/"), ("DELETE", "/unknown")):
                    status, headers, body = _request(port, method, path)
                    self.assertEqual(status, 501)
                    self.assertEqual(body, unsupported_method(method).body)
                    self.assertEqual(headers["content-type"], "text/html;charset=utf-8")
                    self.assertIn("Cheroot", headers["server"])
                    self.assertNotIn("allow", headers)
            for runtime in (core, web):
                self.assertEqual(runtime._server.requests.min, 8)
                self.assertEqual(runtime._server.requests.max, 8)
                self.assertEqual(runtime._server.requests._queue.maxsize, 8)
            connections = []
            replies = []
            try:
                for _ in range(2):
                    connection = http.client.HTTPConnection("127.0.0.1", web_port, timeout=2)
                    connections.append(connection)
                    connection.request("GET", "/api/web/events/stream", headers={"Authorization": "Bearer valid"})
                    reply = connection.getresponse()
                    replies.append(reply)
                    self.assertEqual(reply.status, 200)
                    self.assertIn(b"event:", reply.readline())
                for port in (core_port, web_port):
                    started = time.monotonic()
                    status, headers, body = _request(port, "GET", "/api/web/events/stream", headers={"Authorization": "Bearer valid"})
                    self.assertEqual(status, 503, body)
                    self.assertEqual(json.loads(body)["error"]["code"], "stream_capacity_exhausted")
                    self.assertNotIn("allow", headers)
                    self.assertNotIn("text/event-stream", headers["content-type"])
                    self.assertLess(time.monotonic() - started, 2)
                    self.assertEqual(_request(port, "GET", "/api/web/events/stream")[0], 401)
                    self.assertEqual(_request(port, "GET", "/api/core/status", headers={"Authorization": "Bearer valid"})[0], 200)
                state["storage_error"] = True
                status, _, body = _request(web_port, "GET", "/api/web/events/stream", headers={"Authorization": "Bearer valid"})
                self.assertEqual(status, 503)
                self.assertEqual(json.loads(body)["error"]["code"], "storage_unavailable")
                state["storage_error"] = False
                state["revoked"] = True
                for reply in replies:
                    reply.read()
                state["revoked"] = False
                for _ in range(2):
                    permit = services.stream_admission.acquire("sse")
                    self.assertIsNotNone(permit)
                    permit.release()
                connection = http.client.HTTPConnection("127.0.0.1", web_port, timeout=2)
                connections.append(connection)
                connection.request("GET", "/api/web/events/stream", headers={"Authorization": "Bearer valid"})
                reply = connection.getresponse()
                replies.append(reply)
                self.assertEqual(reply.status, 200)
                self.assertIn(b"event:", reply.readline())
            finally:
                state["revoked"] = True
                for reply in replies:
                    reply.close()
                for connection in connections:
                    connection.close()


if __name__ == "__main__":
    unittest.main()
