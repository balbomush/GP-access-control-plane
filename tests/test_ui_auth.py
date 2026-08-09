from __future__ import annotations

import base64
import http.client
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.config import AppConfig, OutputConfig
from gp_control_plane.web.api_server import serve
from gp_control_plane.web.ui import index_html


class UiBearerAuthSourceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = index_html()

    @staticmethod
    def script_block(start_marker: str, end_marker: str) -> str:
        script = UiBearerAuthSourceContractTests.html.split("<script>", 1)[1].split("</script>", 1)[0]
        start = script.index(start_marker)
        end = script.index(end_marker, start)
        return script[start:end]

    def test_login_uses_agreed_endpoint_and_default_admin_credentials(self) -> None:
        self.assertIn('id="login-form"', self.html)
        self.assertIn('value="admin"', self.html)
        self.assertIn("fetch('/api/auth/login'", self.html)
        self.assertIn("method: 'POST'", self.html)

    def test_token_is_persisted_and_sent_in_central_request_headers(self) -> None:
        self.assertIn("const AUTH_TOKEN_KEY = 'gp-control-plane-auth-token';", self.html)
        self.assertIn('localStorage.getItem(AUTH_TOKEN_KEY)', self.html)
        self.assertIn('localStorage.setItem(AUTH_TOKEN_KEY, token);', self.html)
        self.assertIn('Authorization: `Bearer ${token}`', self.html)
        self.assertIn('async function authFetch(url, options)', self.html)
        self.assertIn('const response = await authFetch(url);', self.html)
        self.assertIn("await authFetch(apiEndpoint('core', 'backupsUpload')", self.html)

    def test_unauthorized_response_clears_token_and_returns_to_login(self) -> None:
        self.assertIn('if (response.status === 401) handleUnauthorized();', self.html)
        self.assertIn('localStorage.removeItem(AUTH_TOKEN_KEY);', self.html)
        self.assertIn("showLogin('Your session has expired. Sign in again.');", self.html)
        self.assertIn("data-action=\"logout\"", self.html)

    def test_password_change_uses_agreed_contract_and_replaces_token(self) -> None:
        self.assertIn('id="change-password-form"', self.html)
        self.assertIn('name="current_password"', self.html)
        self.assertIn('name="new_password"', self.html)
        self.assertIn("postJson('/api/auth/change-password'", self.html)
        self.assertIn('current_password: currentPassword', self.html)
        self.assertIn('new_password: newPassword', self.html)
        self.assertIn('storeAuthToken(data);', self.html)

    def test_archive_download_is_top_level_and_uses_authenticated_blob_without_token_query_parameter(self) -> None:
        backup_url = self.script_block('function backupDownloadUrl(snapshot){', 'async function downloadBackup(url, snapshotId){')
        download = self.script_block('async function downloadBackup(url, snapshotId){', 'function formatBytes(value){')

        self.assertRegex(backup_url, r"function backupDownloadUrl\(snapshot\)\{[\s\S]*return requestUrl\(apiUrl\('core', 'backupsDownloadArchive', params\)\);\s*\}\s*$")
        self.assertIn('const response = await authFetch(url);', download)
        self.assertIn('const blob = await response.blob();', download)
        self.assertIn('URL.createObjectURL(blob)', download)
        self.assertIn('URL.revokeObjectURL(objectUrl)', download)
        self.assertIn('data-backup-download="${esc(id)}"', self.html)
        self.assertNotIn("params.set('token'", backup_url)
        self.assertNotIn('gp_token', backup_url)

    def test_realtime_stream_uses_fetch_reader_with_cancellation_and_reconnect(self) -> None:
        self.assertIn("authFetch(apiEndpoint('web', 'eventsStream')", self.html)
        self.assertIn('const controller = new AbortController();', self.html)
        self.assertIn('const reader = response.body.getReader();', self.html)
        self.assertIn('function parseSseEvent(frame)', self.html)
        self.assertIn('function scheduleRealtimeReconnect()', self.html)
        self.assertNotIn('new EventSource(', self.html)

    def test_password_rotation_renews_exactly_one_realtime_stream_with_fresh_token(self) -> None:
        password_change = self.script_block('async function changePassword(){', 'function apiEndpoint(namespace, name){')
        stop = self.script_block('function stopRealtimeEvents(){', 'function renewRealtimeEvents(){')
        renew = self.script_block('function renewRealtimeEvents(){', 'function stopRealtimeFallback(){')
        realtime_connect = self.script_block('async function connectRealtimeEvents(controller){', 'function startRealtimeEvents(options){')
        realtime_start = self.script_block('function startRealtimeEvents(options){', 'function startRealtimeFallback(){')

        self.assertLess(password_change.index('storeAuthToken(data);'), password_change.index('renewRealtimeEvents();'))
        self.assertIn('if (realtimeReconnectTimer) clearTimeout(realtimeReconnectTimer);', stop)
        self.assertIn('realtimeReconnectTimer = null;', stop)
        self.assertRegex(renew, r"stopRealtimeEvents\(\);[\s\S]*realtimeReconnectDelay = 1000;[\s\S]*startRealtimeEvents\(\{ alreadyStopped: true \}\);")
        self.assertEqual(1, renew.count('startRealtimeEvents({ alreadyStopped: true });'))
        self.assertEqual(1, realtime_start.count('connectRealtimeEvents(controller);'))
        self.assertIn("authFetch(apiEndpoint('web', 'eventsStream')", realtime_connect)
        self.assertIn('const alreadyStopped = Boolean(options && options.alreadyStopped);', realtime_start)
        self.assertIn('if (!alreadyStopped) stopRealtimeEvents();', realtime_start)

class EdgeBearerAuthBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.edge_executable = _edge_executable()
        if cls.edge_executable is None:
            raise unittest.SkipTest("Microsoft Edge headless is not installed")

    def test_login_auth_fetch_blob_download_and_password_rotation_restart_sse(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            port = _start_server(config)
            snapshot_id = _create_backup(port)

            with _EdgeCdp(self.edge_executable) as page:
                page.navigate(f"http://127.0.0.1:{port}/")
                page.wait_for("document.getElementById('login-form')", "login form")
                page.evaluate(
                    """
                    document.getElementById('login-username').value = 'admin';
                    document.getElementById('login-password').value = 'admin';
                    document.getElementById('login-form').requestSubmit();
                    """
                )
                page.wait_for(
                    "localStorage.getItem('gp-control-plane-auth-token') && document.getElementById('login-screen').hidden && !document.getElementById('app-shell').hidden",
                    "authenticated application shell",
                )
                page.evaluate(
                    """
                    (() => {
                      const state = window.__bearerAuthE2E = { downloads: [], sse: [], blob: null, anchor: null };
                      const originalFetch = window.fetch.bind(window);
                      window.fetch = async (input, init) => {
                        const url = typeof input === 'string' ? input : input.url;
                        const headers = Array.from(new Headers((init && init.headers) || (input instanceof Request ? input.headers : undefined)).entries());
                        if (url.includes('/api/core/backups/download-archive')) state.downloads.push({ url, headers });
                        if (url.includes('/api/web/events/stream')) state.sse.push({ url, headers });
                        return originalFetch(input, init);
                      };
                      const originalObjectUrl = URL.createObjectURL.bind(URL);
                      URL.createObjectURL = (blob) => {
                        const objectUrl = originalObjectUrl(blob);
                        state.blob = { size: blob.size, objectUrl };
                        return objectUrl;
                      };
                      HTMLAnchorElement.prototype.click = function() {
                        state.anchor = { href: this.href, download: this.download };
                      };
                    })();
                    """
                )
                page.evaluate("refreshBackups();")
                snapshot = json.dumps(snapshot_id)
                page.wait_for(
                    f"Array.from(document.querySelectorAll('[data-backup-download]')).some((button) => button.dataset.backupDownload === {snapshot})",
                    "backup download action",
                )
                page.evaluate(
                    f"Array.from(document.querySelectorAll('[data-backup-download]')).find((button) => button.dataset.backupDownload === {snapshot}).click();"
                )
                page.wait_for(
                    "window.__bearerAuthE2E.downloads.length === 1 && window.__bearerAuthE2E.blob && window.__bearerAuthE2E.anchor",
                    "authenticated Blob download",
                )

                page.evaluate("stopRealtimeEvents(); startRealtimeEvents();")
                page.wait_for("window.__bearerAuthE2E.sse.length === 1", "initial authenticated SSE stream")
                page.evaluate(
                    """
                    document.getElementById('settings-current-password').value = 'admin';
                    document.getElementById('settings-new-password').value = 'newpass8';
                    document.getElementById('change-password-form').requestSubmit();
                    """
                )
                page.wait_for(
                    "window.__bearerAuthE2E.sse.length >= 2 && window.__bearerAuthE2E.sse[0].headers.find(([key]) => key === 'authorization')[1] !== window.__bearerAuthE2E.sse[1].headers.find(([key]) => key === 'authorization')[1]",
                    "SSE restart with the rotated token",
                )
                result = page.evaluate("JSON.parse(JSON.stringify(window.__bearerAuthE2E))")

            self.assertEqual(len(result["downloads"]), 1)
            download = result["downloads"][0]
            self.assertNotIn("token=", download["url"])
            self.assertNotIn("gp_token", download["url"])
            self.assertTrue(dict(download["headers"])["authorization"].startswith("Bearer "))
            self.assertGreater(result["blob"]["size"], 0)
            self.assertTrue(result["anchor"]["href"].startswith("blob:"))
            self.assertEqual(len(result["sse"]), 2)
            self.assertTrue(dict(result["sse"][1]["headers"])["authorization"].startswith("Bearer "))


def _edge_executable() -> Path | None:
    program_files_x86 = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
    program_files = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
    local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
    candidates = (
        program_files_x86 / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        program_files / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        local_app_data / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    )
    return next((path for path in candidates if path.is_file()), None)


def _start_server(config: AppConfig) -> int:
    port = _free_port()
    thread = threading.Thread(target=serve, args=(config, "127.0.0.1", port), daemon=True)
    thread.start()
    _wait_for_http(f"http://127.0.0.1:{port}/api/health")
    return port


def _create_backup(port: int) -> str:
    status, body = _http_request(
        port,
        "POST",
        "/api/auth/login",
        {"Content-Type": "application/json"},
        json.dumps({"username": "admin", "password": "admin"}).encode("utf-8"),
    )
    if status != 200:
        raise AssertionError(f"test setup login failed with HTTP {status}: {body!r}")
    token = json.loads(body)["access_token"]
    status, body = _http_request(
        port,
        "POST",
        "/api/core/backups/create",
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        b"{}",
    )
    if status != 201:
        raise AssertionError(f"test setup backup creation failed with HTTP {status}: {body!r}")
    return str(json.loads(body)["snapshot_id"])


def _http_request(port: int, method: str, path: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for_http(url: str, timeout: float = 5) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                return json.loads(response.read())
        except (OSError, ValueError) as error:
            last_error = error
            time.sleep(0.02)
    raise AssertionError(f"server did not become ready: {last_error}")


class _EdgeCdp:
    def __init__(self, executable: Path) -> None:
        self._executable = executable
        self._process: subprocess.Popen[bytes] | None = None
        self._client: _CdpClient | None = None
        self._session_id: str | None = None

    def __enter__(self) -> "_EdgeCdp":
        self._debug_port = _free_port()
        self._profile = tempfile.TemporaryDirectory()
        self._process = subprocess.Popen(
            [
                str(self._executable),
                "--headless=new",
                f"--remote-debugging-port={self._debug_port}",
                f"--user-data-dir={self._profile.name}",
                "--no-first-run",
                "--no-default-browser-check",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        version = _wait_for_http(f"http://127.0.0.1:{self._debug_port}/json/version")
        self._client = _CdpClient(str(version["webSocketDebuggerUrl"]))
        target_id = str(self._client.command("Target.createTarget", {"url": "about:blank"})["targetId"])
        self._session_id = str(
            self._client.command("Target.attachToTarget", {"targetId": target_id, "flatten": True})["sessionId"]
        )
        self._client.command("Runtime.enable", session_id=self._session_id)
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        if self._client is not None:
            self._client.close()
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
        self._profile.cleanup()

    def navigate(self, url: str) -> None:
        self._command("Page.navigate", {"url": url})

    def evaluate(self, expression: str) -> Any:
        response = self._command(
            "Runtime.evaluate", {"expression": expression, "awaitPromise": True, "returnByValue": True}
        )
        if "exceptionDetails" in response:
            raise AssertionError(f"browser JavaScript failed: {response['exceptionDetails']}")
        return response["result"].get("value")

    def wait_for(self, expression: str, description: str, timeout: float = 10) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.evaluate(f"Boolean({expression})"):
                return
            time.sleep(0.02)
        raise AssertionError(f"browser condition did not become true: {description}")

    def _command(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        assert self._client is not None
        assert self._session_id is not None
        return self._client.command(method, params, session_id=self._session_id)


class _CdpClient:
    def __init__(self, websocket_url: str) -> None:
        address = websocket_url.removeprefix("ws://")
        host_port, path = address.split("/", 1)
        host, raw_port = host_port.rsplit(":", 1)
        self._socket = socket.create_connection((host, int(raw_port)), timeout=5)
        self._socket.settimeout(5)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        self._socket.sendall(
            (
                f"GET /{path} HTTP/1.1\r\nHost: {host_port}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode("ascii")
        )
        response = self._read_http_headers()
        if not response.startswith(b"HTTP/1.1 101"):
            raise AssertionError(f"CDP WebSocket handshake failed: {response!r}")
        self._next_id = 0

    def close(self) -> None:
        self._socket.close()

    def command(self, method: str, params: dict[str, Any] | None = None, *, session_id: str | None = None) -> dict[str, Any]:
        self._next_id += 1
        message: dict[str, Any] = {"id": self._next_id, "method": method}
        if params:
            message["params"] = params
        if session_id:
            message["sessionId"] = session_id
        self._send(message)
        while True:
            response = self._receive()
            if response.get("id") != self._next_id:
                continue
            if "error" in response:
                raise AssertionError(f"CDP command {method} failed: {response['error']}")
            return dict(response.get("result", {}))

    def _read_http_headers(self) -> bytes:
        response = bytearray()
        while b"\r\n\r\n" not in response:
            response.extend(self._socket.recv(1024))
        return bytes(response)

    def _send(self, message: dict[str, Any]) -> None:
        payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
        mask = os.urandom(4)
        if len(payload) < 126:
            header = bytes((0x81, 0x80 | len(payload)))
        elif len(payload) < 65536:
            header = bytes((0x81, 0x80 | 126)) + struct.pack("!H", len(payload))
        else:
            header = bytes((0x81, 0x80 | 127)) + struct.pack("!Q", len(payload))
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self._socket.sendall(header + mask + masked)

    def _receive(self) -> dict[str, Any]:
        while True:
            first, second = self._read_exact(2)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            masked = bool(second & 0x80)
            mask = self._read_exact(4) if masked else b""
            payload = self._read_exact(length)
            if masked:
                payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            if opcode == 0x8:
                raise AssertionError("CDP WebSocket closed unexpectedly")
            if opcode == 0x9:
                self._socket.sendall(bytes((0x8A, len(payload))) + payload)
                continue
            if opcode == 0x1:
                return json.loads(payload)

    def _read_exact(self, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            chunk = self._socket.recv(size - len(chunks))
            if not chunk:
                raise AssertionError("CDP WebSocket closed unexpectedly")
            chunks.extend(chunk)
        return bytes(chunks)


if __name__ == '__main__':
    unittest.main()
