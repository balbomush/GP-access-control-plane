from __future__ import annotations

import http.client
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.config import AppConfig, OutputConfig
from gp_control_plane.web.api_server import serve
from gp_control_plane.web.proxy import serve_web_proxy


class BearerAuthHttpTests(unittest.TestCase):
    def test_monolith_public_allowlist_and_protected_transport_routes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            port = _start_server(serve, config)

            for path in ("/", "/swagger", "/swagger/", "/openapi.json", "/api/health"):
                status, _headers, _body = _request(port, path)
                self.assertEqual(status, 200, path)
            status, _headers, _body = _request(port, "/api/health", method="HEAD")
            self.assertEqual(status, 200)

            login_status, _headers, login_body = _request(
                port,
                "/api/auth/login",
                method="POST",
                body=_json_bytes({"username": "admin", "password": "admin"}),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(login_status, 200)
            token = json.loads(login_body)["access_token"]
            bearer = {"Authorization": f"Bearer {token}"}

            anonymous_requests = (
                ("GET", "/api/core/strategy-discovery/current-run-progress", None, {}),
                ("POST", "/api/core/strategy-discovery/stop-current-run", _json_bytes({"dry_run": True}), {"Content-Type": "application/json"}),
                ("POST", "/api/core/backups/upload", b"not-a-zip", {"Content-Type": "application/zip"}),
                ("GET", "/api/core/backups/download-archive?snapshot_id=missing", None, {}),
                ("HEAD", "/api/core/strategy-candidates/export", None, {}),
                ("GET", "/api/web/events/stream", None, {"Accept": "text/event-stream"}),
            )
            for method, path, body, headers in anonymous_requests:
                status, response_headers, response_body = _request(port, path, method=method, body=body, headers=headers)
                self.assertEqual(status, 401, f"{method} {path}")
                self.assertEqual(response_headers.get("content-type"), "application/json; charset=utf-8")
                if method != "HEAD":
                    self.assertIn("error", json.loads(response_body))

            status, _headers, body = _request(port, "/api/core/strategy-discovery/current-run-progress", headers=bearer)
            self.assertEqual(status, 200)
            self.assertIn("status", json.loads(body))
            status, _headers, body = _request(port, "/api/web/run-preferences", headers=bearer)
            self.assertEqual(status, 200)
            self.assertIn("run_preferences", json.loads(body))

            status, _headers, body = _request(
                port,
                "/api/auth/change-password",
                method="POST",
                body=_json_bytes({"current_password": "admin", "new_password": "short"}),
                headers={**bearer, "Content-Type": "application/json"},
            )
            self.assertEqual(status, 400)
            self.assertIn("error", json.loads(body))

            status, _headers, body = _request(port, "/openapi.json")
            self.assertEqual(status, 200)
            contract = json.loads(body)
            self.assertEqual(contract["components"]["securitySchemes"]["bearerAuth"]["scheme"], "bearer")
            self.assertEqual(contract["security"], [{"bearerAuth": []}])
            for path, operations in contract["paths"].items():
                for method, operation in operations.items():
                    if method.lower() not in {"get", "post", "put", "patch", "delete", "head", "options", "trace"}:
                        continue
                    expected = [] if (path, method.lower()) in {
                        ("/api/health", "get"),
                        ("/api/auth/login", "post"),
                    } else [{"bearerAuth": []}]
                    self.assertEqual(operation["security"], expected, f"{method.upper()} {path}")

    def test_split_proxy_forwards_auth_and_rotates_shared_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            core_port = _start_server(serve, config, ui_enabled=False)
            proxy_port = _start_server(serve_web_proxy, config, core_url=f"http://127.0.0.1:{core_port}")

            for path in ("/", "/swagger", "/openapi.json", "/api/health"):
                status, _headers, _body = _request(proxy_port, path)
                self.assertEqual(status, 200, path)
            status, _headers, _body = _request(proxy_port, "/api/web/events/stream")
            self.assertEqual(status, 401)
            status, _headers, _body = _request(proxy_port, "/api/core/strategy-discovery/current-run-progress")
            self.assertEqual(status, 401)

            status, _headers, body = _request(
                proxy_port,
                "/api/auth/login",
                method="POST",
                body=_json_bytes({"username": "admin", "password": "admin"}),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 200)
            old_token = json.loads(body)["access_token"]
            old_bearer = {"Authorization": f"Bearer {old_token}"}
            status, _headers, _body = _request(proxy_port, "/api/core/strategy-discovery/current-run-progress", headers=old_bearer)
            self.assertEqual(status, 200)
            status, _headers, body = _request(
                proxy_port,
                "/api/auth/change-password",
                method="POST",
                body=_json_bytes({"current_password": "admin", "new_password": "newpass8"}),
                headers={**old_bearer, "Content-Type": "application/json"},
            )
            self.assertEqual(status, 200)
            new_token = json.loads(body)["access_token"]
            status, _headers, _body = _request(proxy_port, "/api/core/strategy-discovery/current-run-progress", headers=old_bearer)
            self.assertEqual(status, 401)
            status, _headers, _body = _request(
                proxy_port,
                "/api/core/strategy-discovery/current-run-progress",
                headers={"Authorization": f"Bearer {new_token}"},
            )
            self.assertEqual(status, 200)

    def test_password_rotation_revokes_open_sse_streams(self) -> None:
        for topology in ("core", "proxy"):
            with self.subTest(topology=topology), tempfile.TemporaryDirectory() as raw:
                config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
                servers: list[_ManagedServer] = []
                connection = response = new_connection = new_response = None
                try:
                    core = _start_managed_server(serve, config, ui_enabled=topology == "core")
                    servers.append(core)
                    if topology == "core":
                        port = core.port
                    else:
                        proxy = _start_managed_server(
                            serve_web_proxy, config, core_url=f"http://127.0.0.1:{core.port}"
                        )
                        servers.append(proxy)
                        port = proxy.port

                    status, _headers, body = _request(
                        port, "/api/auth/login", method="POST",
                        body=_json_bytes({"username": "admin", "password": "admin"}),
                        headers={"Content-Type": "application/json"},
                    )
                    self.assertEqual(status, 200)
                    old_bearer = {"Authorization": f"Bearer {json.loads(body)['access_token']}"}

                    connection, response = _open_sse(port, old_bearer)
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.getheader("Content-Type"), "text/event-stream; charset=utf-8")
                    status, _headers, body = _request(
                        port, "/api/auth/change-password", method="POST",
                        body=_json_bytes({"current_password": "admin", "new_password": "newpass8"}),
                        headers={**old_bearer, "Content-Type": "application/json"},
                    )
                    self.assertEqual(status, 200)
                    new_token = json.loads(body)["access_token"]
                    status, _headers, _body = _request(port, "/api/web/events/stream", headers=old_bearer)
                    self.assertEqual(status, 401)
                    self.assertIsInstance(response.read(), bytes)
                    self.assertTrue(response.isclosed())

                    new_connection, new_response = _open_sse(port, {"Authorization": f"Bearer {new_token}"})
                    self.assertEqual(new_response.status, 200)
                finally:
                    _close_sse(new_connection, new_response)
                    _close_sse(connection, response)
                    for server in reversed(servers):
                        server.close()


class _ManagedServer:
    def __init__(self, port: int, server: Any, thread: threading.Thread) -> None:
        self.port = port
        self._server = server
        self._thread = thread

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            raise AssertionError(f"server thread did not stop on port {self.port}")


def _start_managed_server(function: Any, config: AppConfig, **kwargs: Any) -> _ManagedServer:
    port = _free_port()
    module = sys.modules[function.__module__]
    server_type = getattr(module, "ThreadingHTTPServer")
    server_created = threading.Event()
    server_holder: dict[str, Any] = {}
    startup_errors: list[BaseException] = []

    class CapturingThreadingHTTPServer(server_type):
        def __init__(self, *args: Any, **server_kwargs: Any) -> None:
            super().__init__(*args, **server_kwargs)
            server_holder["server"] = self
            server_created.set()

    def run() -> None:
        try:
            function(config, "127.0.0.1", port, **kwargs)
        except BaseException as error:
            startup_errors.append(error)
            server_created.set()

    thread = threading.Thread(target=run, daemon=True)
    with mock.patch.object(module, "ThreadingHTTPServer", CapturingThreadingHTTPServer):
        thread.start()
        if not server_created.wait(timeout=5):
            raise AssertionError(f"server on port {port} did not construct")

    if startup_errors:
        raise AssertionError(f"server on port {port} failed during startup") from startup_errors[0]
    server = server_holder.get("server")
    if server is None:
        raise AssertionError(f"server on port {port} was not captured")
    managed = _ManagedServer(port, server, thread)
    try:
        _wait_for_server(port)
    except BaseException:
        managed.close()
        raise
    return managed


def _close_sse(
    connection: http.client.HTTPConnection | None, response: http.client.HTTPResponse | None
) -> None:
    try:
        if response is not None:
            response.close()
    finally:
        if connection is not None:
            connection.close()



def _start_server(function: Any, config: AppConfig, **kwargs: Any) -> int:
    port = _free_port()
    thread = threading.Thread(target=function, args=(config, "127.0.0.1", port), kwargs=kwargs, daemon=True)
    thread.start()
    _wait_for_server(port)
    return port


def _request(
    port: int,
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
    finally:
        connection.close()


def _open_sse(port: int, headers: dict[str, str]) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("GET", "/api/web/events/stream", headers=headers)
    return connection, connection.getresponse()


def _wait_for_server(port: int) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            status, _headers, _body = _request(port, "/api/health")
        except OSError:
            time.sleep(0.02)
            continue
        if status == 200:
            return
        time.sleep(0.02)
    raise AssertionError(f"server on port {port} did not become ready")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode("utf-8")


if __name__ == "__main__":
    unittest.main()