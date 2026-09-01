from __future__ import annotations

import base64
import ctypes
import http.client
import json
import os
import re
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
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.config import AppConfig, OutputConfig
from gp_control_plane.state import read_state, update_state
from gp_control_plane.web.api_server import serve
from gp_control_plane.web import api_server
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

    def test_auth_ui_uses_russian_text_without_prefilled_credentials(self) -> None:
        self.assertIn('id="login-form"', self.html)
        for text in (
            'Войдите, чтобы продолжить работу с панелью.',
            'Логин',
            'Пароль',
            'Войти',
            'Выйти',
            'Смена пароля',
            'Текущий пароль',
            'Новый пароль',
            'Используйте не менее 8 символов или admin для возврата стандартного доступа.',
            'Изменить пароль',
        ):
            self.assertIn(text, self.html)
        self.assertIn('id="login-username" name="username" autocomplete="username" required', self.html)
        self.assertIn(
            'id="login-password" name="password" type="password" autocomplete="current-password" required', self.html
        )
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

    def test_password_change_uses_agreed_contract_and_logs_out_without_storing_replacement_token(self) -> None:
        password_change = self.script_block('async function changePassword(){', 'function apiEndpoint(namespace, name){')
        logout = self.script_block('function logout(){', 'async function authFetch(url, options){')

        self.assertIn('id="change-password-form"', self.html)
        self.assertIn('name="current_password"', self.html)
        self.assertIn('name="new_password"', self.html)
        self.assertIn("await postJson('/api/auth/change-password'", password_change)
        self.assertIn('current_password: currentPassword', password_change)
        self.assertIn('new_password: newPassword', password_change)
        self.assertIn('logout();', password_change)
        self.assertNotIn('storeAuthToken(', password_change)
        self.assertNotIn('renewRealtimeEvents(', password_change)
        self.assertIn('localStorage.removeItem(AUTH_TOKEN_KEY);', logout)
        self.assertIn('stopRealtimeEvents();', logout)
        self.assertIn('stopRealtimeFallback();', logout)
        self.assertIn('showLogin();', logout)

    def test_password_change_panel_is_independent_accessible_and_has_its_own_lifecycle_messages(self) -> None:
        password_change = self.script_block('async function changePassword(){', 'function apiEndpoint(namespace, name){')

        self.assertRegex(
            self.html,
            r'<form class="preset-panel settings-access-panel" id="change-password-form" '
            r'aria-labelledby="settings-access-heading">[\s\S]*?'
            r'<h2 id="settings-access-heading">Доступ к панели</h2>',
        )
        self.assertRegex(
            self.html,
            r'<input id="settings-new-password"(?![^>]*minlength=)[^>]*'
            r'aria-describedby="settings-new-password-hint"[^>]*>',
        )
        self.assertIn(
            'id="settings-new-password-hint">Используйте не менее 8 символов или admin для возврата стандартного доступа.</div>',
            self.html,
        )
        self.assertIn(
            'id="change-password-status" role="status" aria-live="polite" aria-atomic="true"', self.html
        )
        self.assertIn("form.setAttribute('aria-busy', 'true');", password_change)
        self.assertIn("form.removeAttribute('aria-busy');", password_change)
        self.assertIn('submitButton.disabled = true;', password_change)
        self.assertIn('submitButton.disabled = false;', password_change)
        self.assertIn("status.textContent = 'Пароль изменяется…';", password_change)
        self.assertIn(
            "status.textContent = 'Не удалось изменить пароль. Проверьте текущий пароль и повторите попытку.';",
            password_change,
        )
        self.assertIn("el('settings-current-password').value = '';", password_change)
        self.assertIn("el('settings-new-password').value = '';", password_change)
        self.assertNotIn('setMessage(', password_change)

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

    def test_password_change_uses_logout_to_stop_realtime_activity(self) -> None:
        password_change = self.script_block('async function changePassword(){', 'function apiEndpoint(namespace, name){')
        stop = self.script_block('function stopRealtimeEvents(){', 'function renewRealtimeEvents(){')
        fallback = self.script_block('function stopRealtimeFallback(){', 'function handleUnauthorized(){')
        logout = self.script_block('function logout(){', 'async function authFetch(url, options){')

        self.assertIn('logout();', password_change)
        self.assertNotIn('storeAuthToken(', password_change)
        self.assertNotIn('renewRealtimeEvents(', password_change)
        self.assertIn('if (realtimeReconnectTimer) clearTimeout(realtimeReconnectTimer);', stop)
        self.assertIn('realtimeReconnectTimer = null;', stop)
        self.assertIn('if (realtimeFallbackTimer) clearInterval(realtimeFallbackTimer);', fallback)
        self.assertIn('realtimeFallbackTimer = null;', fallback)
        self.assertIn('stopRealtimeEvents();', logout)
        self.assertIn('stopRealtimeFallback();', logout)

class _EdgeBrowserTestSupport:
    @classmethod
    def setUpClass(cls) -> None:
        cls.edge_executable = _edge_executable()
        if cls.edge_executable is None:
            raise unittest.SkipTest("Microsoft Edge headless is not installed")


class EdgeBearerAuthBrowserTests(_EdgeBrowserTestSupport, unittest.TestCase):

    def test_login_auth_fetch_blob_download_and_password_change_logs_out(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with _TestServer(config) as server, _EdgeCdp(self.edge_executable) as page:
                snapshot_id = _create_backup(server.port)
                page.navigate(f"http://127.0.0.1:{server.port}/")
                page.wait_for(
                    "document.readyState === 'complete' && typeof submitLogin === 'function' && document.getElementById('login-form')",
                    "initialized login form",
                    diagnostics="""({
                      readyState: document.readyState,
                      submitLogin: typeof submitLogin,
                      loginForm: Boolean(document.getElementById('login-form')),
                      loginScreenHidden: document.getElementById('login-screen')?.hidden,
                      appShellHidden: document.getElementById('app-shell')?.hidden
                    })""",
                )
                login_values = page.evaluate(
                    """
                    ({
                      username: document.getElementById('login-username').value,
                      password: document.getElementById('login-password').value,
                    })
                    """
                )
                self.assertEqual(login_values, {"username": "", "password": ""})
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
                access_panel = page.evaluate(
                    """
                    (() => {
                      const form = document.getElementById('change-password-form');
                      const status = document.getElementById('change-password-status');
                      const newPassword = document.getElementById('settings-new-password');
                      return {
                        isSeparatePanel: form.classList.contains('settings-access-panel') && !form.closest('.settings-discovery-panel'),
                        heading: document.getElementById(form.getAttribute('aria-labelledby'))?.textContent.trim(),
                        statusRole: status.getAttribute('role'),
                        statusLive: status.getAttribute('aria-live'),
                        statusAtomic: status.getAttribute('aria-atomic'),
                        minLength: newPassword.minLength,
                        describedBy: newPassword.getAttribute('aria-describedby'),
                        hint: document.getElementById(newPassword.getAttribute('aria-describedby'))?.textContent.trim(),
                      };
                    })()
                    """
                )
                self.assertEqual(
                    access_panel,
                    {
                        "isSeparatePanel": True,
                        "heading": "Доступ к панели",
                        "statusRole": "status",
                        "statusLive": "polite",
                        "statusAtomic": "true",
                        "minLength": -1,
                        "describedBy": "settings-new-password-hint",
                        "hint": "Используйте не менее 8 символов или admin для возврата стандартного доступа.",
                    },
                )
                page.evaluate(
                    """
                    (() => {
                      const state = window.__bearerAuthE2E = {
                        downloads: [],
                        sse: [],
                        blob: null,
                        anchor: null,
                        passwordChange: { requests: 0, held: false, release: null },
                        fallbackTimers: [],
                        clearedFallbackTimers: []
                      };
                      const originalFetch = window.fetch.bind(window);
                      window.fetch = async (input, init) => {
                        const url = typeof input === 'string' ? input : input.url;
                        const headers = Array.from(new Headers((init && init.headers) || (input instanceof Request ? input.headers : undefined)).entries());
                        if (url.includes('/api/core/backups/download-archive')) state.downloads.push({ url, headers });
                        if (url.includes('/api/web/events/stream')) {
                          const entry = { url, headers, aborted: Boolean(init?.signal?.aborted) };
                          init?.signal?.addEventListener('abort', () => { entry.aborted = true; });
                          state.sse.push(entry);
                        }
                        if (url.includes('/api/auth/change-password')) {
                          state.passwordChange.requests += 1;
                          if (state.passwordChange.requests === 1) {
                            return new Response(JSON.stringify({ error: { message: 'wrong password' } }), {
                              status: 400,
                              headers: { 'Content-Type': 'application/json' }
                            });
                          }
                          if (state.passwordChange.requests === 2) {
                            state.passwordChange.held = true;
                            await new Promise((resolve) => { state.passwordChange.release = resolve; });
                            return originalFetch(input, init);
                          }
                          return originalFetch(input, init);
                        }
                        return originalFetch(input, init);
                      };
                      const originalSetInterval = window.setInterval.bind(window);
                      const originalClearInterval = window.clearInterval.bind(window);
                      window.setInterval = (callback, delay) => {
                        const timer = originalSetInterval(callback, delay);
                        state.fallbackTimers.push(timer);
                        return timer;
                      };
                      window.clearInterval = (timer) => {
                        state.clearedFallbackTimers.push(timer);
                        return originalClearInterval(timer);
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
                    stopRealtimeFallback();
                    startRealtimeFallback();
                    window.__bearerAuthE2E.fallbackTimer = window.__bearerAuthE2E.fallbackTimers.at(-1);
                    window.__bearerAuthE2E.clearedFallbackTimers = [];
                    """
                )
                old_token = page.evaluate("localStorage.getItem('gp-control-plane-auth-token')")
                page.evaluate(
                    """
                    document.getElementById('settings-current-password').value = 'wrongpass';
                    document.getElementById('settings-new-password').value = 'another8';
                    document.getElementById('change-password-form').requestSubmit();
                    """
                )
                page.wait_for(
                    f"""
                    window.__bearerAuthE2E.passwordChange.requests === 1
                      && localStorage.getItem('gp-control-plane-auth-token') === {json.dumps(old_token)}
                      && document.getElementById('login-screen').hidden
                      && !document.getElementById('app-shell').hidden
                      && !document.getElementById('change-password-form').hasAttribute('aria-busy')
                      && !document.querySelector('#change-password-form [type="submit"]').disabled
                      && document.getElementById('settings-current-password').value === ''
                      && document.getElementById('settings-new-password').value === ''
                      && document.getElementById('change-password-status').textContent === 'Не удалось изменить пароль. Проверьте текущий пароль и повторите попытку.'
                    """,
                    "password change failure leaves the authenticated session intact",
                )
                page.evaluate(
                    """
                    document.getElementById('settings-current-password').value = 'admin';
                    document.getElementById('settings-new-password').value = 'newpass8';
                    document.getElementById('change-password-form').requestSubmit();
                    """
                )
                page.wait_for(
                    """
                    window.__bearerAuthE2E.passwordChange.held
                      && document.getElementById('change-password-form').getAttribute('aria-busy') === 'true'
                      && document.querySelector('#change-password-form [type="submit"]').disabled
                      && document.getElementById('change-password-status').textContent === 'Пароль изменяется…'
                    """,
                    "successful password change pending lifecycle",
                )
                page.evaluate("window.__bearerAuthE2E.passwordChange.release()")
                page.wait_for(
                    """
                    window.__bearerAuthE2E.passwordChange.requests === 2
                      && localStorage.getItem('gp-control-plane-auth-token') === null
                      && !document.getElementById('login-screen').hidden
                      && document.getElementById('app-shell').hidden
                      && !document.getElementById('change-password-form').hasAttribute('aria-busy')
                      && !document.querySelector('#change-password-form [type="submit"]').disabled
                      && document.getElementById('settings-current-password').value === ''
                      && document.getElementById('settings-new-password').value === ''
                      && window.__bearerAuthE2E.sse.length === 1
                      && window.__bearerAuthE2E.sse[0].aborted
                      && window.__bearerAuthE2E.clearedFallbackTimers.includes(window.__bearerAuthE2E.fallbackTimer)
                    """,
                    "successful password change clears the session and stops realtime activity",
                )
                result = page.evaluate("JSON.parse(JSON.stringify(window.__bearerAuthE2E))")

            self.assertEqual(len(result["downloads"]), 1)
            download = result["downloads"][0]
            self.assertNotIn("token=", download["url"])
            self.assertNotIn("gp_token", download["url"])
            self.assertTrue(dict(download["headers"])["authorization"].startswith("Bearer "))
            self.assertGreater(result["blob"]["size"], 0)
            self.assertTrue(result["anchor"]["href"].startswith("blob:"))
            self.assertEqual(len(result["sse"]), 1)
            self.assertTrue(result["sse"][0]["aborted"])
            self.assertIn(result["fallbackTimer"], result["clearedFallbackTimers"])

    def test_web_layout_matrix_keeps_metrics_summary_disclosure_and_password_fields_usable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with _TestServer(config) as server, _EdgeCdp(self.edge_executable) as page:
                page.navigate(f"http://127.0.0.1:{server.port}/")
                page.wait_for(
                    "document.readyState === 'complete' && document.getElementById('login-form')",
                    "initialized login form",
                )
                page.evaluate(
                    """
                    document.getElementById('login-username').value = 'admin';
                    document.getElementById('login-password').value = 'admin';
                    document.getElementById('login-form').requestSubmit();
                    """
                )
                page.wait_for(
                    "document.getElementById('login-screen').hidden && !document.getElementById('app-shell').hidden",
                    "authenticated application shell",
                )
                page.wait_for(
                    "document.querySelectorAll('.status-grid .metric').length === 3 && document.querySelectorAll('.run-launch-summary-grid .run-launch-summary-item').length > 0",
                    "layout fixture rendered",
                    diagnostics="""({
                      metrics: document.querySelectorAll('.status-grid .metric').length,
                      summaryItems: document.querySelectorAll('.run-launch-summary-grid .run-launch-summary-item').length
                    })""",
                )

                for width in (960, 1024, 1440):
                    page.set_viewport(width)
                    page.wait_for(
                        f"window.innerWidth === {width}",
                        f"{width}px viewport applied",
                    )
                    layout = page.evaluate(
                        """
                        (() => {
                          const rect = (element) => {
                            const box = element.getBoundingClientRect();
                            return { left: box.left, top: box.top, width: box.width, right: box.right };
                          };
                          const metrics = Array.from(document.querySelectorAll('.status-grid .metric')).map(rect);
                          const summary = document.querySelector('.run-launch-summary-grid');
                          const summaryItems = Array.from(summary.querySelectorAll('.run-launch-summary-item')).map(rect);
                          const summaryStyle = getComputedStyle(summary);
                          return {
                            metrics,
                            summary: rect(summary),
                            summaryItems,
                            summaryGap: parseFloat(summaryStyle.columnGap),
                            summaryColumns: summaryStyle.gridTemplateColumns,
                            summaryJustifyContent: summaryStyle.justifyContent,
                          };
                        })()
                        """
                    )
                    metrics = layout["metrics"]
                    self.assertEqual(3, len(metrics))
                    self.assertLess(max(metric["top"] for metric in metrics) - min(metric["top"] for metric in metrics), 2)
                    self.assertLess(max(metric["width"] for metric in metrics) - min(metric["width"] for metric in metrics), 2)

                    summary_items = layout["summaryItems"]
                    self.assertGreater(len(summary_items), 0)
                    self.assertGreaterEqual(layout["summaryGap"], 0)
                    self.assertLessEqual(layout["summaryGap"], 16)
                    self.assertNotIn("fr", layout["summaryColumns"])
                    self.assertEqual("start", layout["summaryJustifyContent"])
                    self.assertLessEqual(max(item["width"] for item in summary_items), 222)
                    first_row = [item for item in summary_items if abs(item["top"] - summary_items[0]["top"]) < 2]
                    row_span = first_row[-1]["right"] - first_row[0]["left"]
                    if layout["summary"]["width"] - row_span > 2:
                        self.assertLess(abs(first_row[0]["left"] - layout["summary"]["left"]), 2)

                page.evaluate("document.getElementById('tab-settings').click()")
                page.wait_for(
                    "document.getElementById('tab-settings').getAttribute('aria-selected') === 'true' && !document.getElementById('change-password-form').closest('[hidden]')",
                    "visible settings access panel",
                )
                for width in (600, 768, 960, 1024, 1440):
                    page.set_viewport(width)
                    page.wait_for(f"window.innerWidth === {width}", f"{width}px viewport applied")
                    password_fields = page.evaluate(
                        """
                        (() => Array.from(document.querySelectorAll('#change-password-form input[type="password"]')).map((element) => {
                          const box = element.getBoundingClientRect();
                          return { left: box.left, top: box.top, width: box.width };
                        }))()
                        """
                    )
                    self.assertEqual(2, len(password_fields))
                    self.assertLess(abs(password_fields[0]["top"] - password_fields[1]["top"]), 2, f"{width}px {password_fields}")
                    self.assertLess(abs(password_fields[0]["width"] - password_fields[1]["width"]), 2)

                for width in (320, 375):
                    page.set_viewport(width)
                    page.wait_for(f"window.innerWidth === {width}", f"{width}px viewport applied")
                    password_fields = page.evaluate(
                        """
                        (() => Array.from(document.querySelectorAll('#change-password-form input[type="password"]')).map((element) => {
                          const box = element.getBoundingClientRect();
                          const parent = element.closest('.settings-access-grid').getBoundingClientRect();
                          return { left: box.left, top: box.top, width: box.width, parentLeft: parent.left, parentWidth: parent.width };
                        }))()
                        """
                    )
                    self.assertEqual(2, len(password_fields))
                    self.assertGreater(password_fields[1]["top"] - password_fields[0]["top"], 2, f"{width}px {password_fields}")
                    for field in password_fields:
                        self.assertLess(abs(field["left"] - field["parentLeft"]), 2)
                        self.assertLess(abs(field["width"] - field["parentWidth"]), 2)

                page.evaluate(
                    """
                    const fixture = document.createElement('details');
                    fixture.className = 'domain-group l-stack';
                    fixture.innerHTML = '<summary class="domain-header">Тестовая группа</summary><div>Содержимое</div>';
                    document.body.append(fixture);
                    """
                )
                marker = page.evaluate(
                    """
                    (() => {
                      const details = document.querySelector('details.domain-group:last-of-type');
                      const summary = details.querySelector('summary');
                      return {
                        closed: getComputedStyle(summary, '::after').transform,
                        focusable: summary.tabIndex,
                      };
                    })()
                    """
                )
                self.assertNotEqual("none", marker["closed"])
                self.assertEqual(0, marker["focusable"])
                page.evaluate("document.querySelector('details.domain-group:last-of-type > summary').click()")
                page.wait_for(
                    "document.querySelector('details.domain-group:last-of-type').open",
                    "domain disclosure opens by click",
                )
                page.wait_for(
                    f"getComputedStyle(document.querySelector('details.domain-group:last-of-type > summary'), '::after').transform !== {json.dumps(marker['closed'])}",
                    "domain disclosure marker changes while open",
                )
                page.evaluate("document.querySelector('details.domain-group:last-of-type > summary').focus()")
                page.wait_for(
                    "document.activeElement === document.querySelector('details.domain-group:last-of-type > summary')",
                    "focused domain disclosure summary",
                )
                page.press_key("Enter")
                page.wait_for(
                    "!document.querySelector('details.domain-group:last-of-type').open",
                    "domain disclosure closes by keyboard",
                )

    def test_login_outer_inset_uses_responsive_tokens_without_overflow(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with _TestServer(config) as server, _EdgeCdp(self.edge_executable) as page:
                page.navigate(f"http://127.0.0.1:{server.port}/")
                page.wait_for(
                    "document.readyState === 'complete' && document.getElementById('login-screen')",
                    "initialized login screen",
                )
                for width, expected_inset in ((320, 16), (600, 24), (960, 24)):
                    page.set_viewport(width)
                    page.wait_for(f"window.innerWidth === {width}", f"{width}px viewport applied")
                    layout = page.evaluate(
                        """
                        (() => {
                          const screen = document.getElementById('login-screen');
                          const style = getComputedStyle(screen);
                          return {
                            paddingLeft: parseFloat(style.paddingLeft),
                            paddingRight: parseFloat(style.paddingRight),
                            scrollWidth: document.documentElement.scrollWidth,
                            clientWidth: document.documentElement.clientWidth,
                          };
                        })()
                        """
                    )
                    self.assertLess(abs(layout["paddingLeft"] - expected_inset), 1, f"{width}px {layout}")
                    self.assertLess(abs(layout["paddingRight"] - expected_inset), 1, f"{width}px {layout}")
                    self.assertLessEqual(layout["scrollWidth"], layout["clientWidth"], f"{width}px {layout}")

    def test_mobile_candidate_header_and_protocol_controls_follow_responsive_grid(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with _TestServer(config) as server, _EdgeCdp(self.edge_executable) as page:
                page.navigate(f"http://127.0.0.1:{server.port}/")
                page.wait_for(
                    "document.readyState === 'complete' && document.getElementById('login-form')",
                    "initialized login form",
                )
                page.evaluate(
                    """
                    document.getElementById('login-username').value = 'admin';
                    document.getElementById('login-password').value = 'admin';
                    document.getElementById('login-form').requestSubmit();
                    """
                )
                page.wait_for(
                    "document.getElementById('login-screen').hidden && !document.getElementById('app-shell').hidden",
                    "authenticated application shell",
                )
                page.evaluate("document.getElementById('tab-candidates').click(); document.getElementById('candidate-view-common').click()")
                page.wait_for(
                    "document.getElementById('tab-candidates').getAttribute('aria-selected') === 'true' && document.getElementById('candidate-view-common').getAttribute('aria-selected') === 'true'",
                    "visible common candidates",
                )
                for width in (320, 375):
                    page.set_viewport(width)
                    page.wait_for(f"window.innerWidth === {width}", f"{width}px viewport applied")
                    header = page.evaluate(
                        """
                        (() => {
                          const rect = (node) => {
                            const box = node.getBoundingClientRect();
                            return { left: box.left, top: box.top, right: box.right, bottom: box.bottom, width: box.width };
                          };
                          const head = document.querySelector('.candidate-result-head');
                          const description = head.firstElementChild;
                          const toolbar = head.querySelector('.candidate-result-toolbar');
                          const action = toolbar.querySelector('[data-action="build-candidate-result"]');
                          action.scrollIntoView({ block: 'center', inline: 'nearest' });
                          const actionRect = action.getBoundingClientRect();
                          const target = document.elementFromPoint(actionRect.left + actionRect.width / 2, actionRect.top + actionRect.height / 2);
                          return {
                            head: rect(head), description: rect(description), toolbar: rect(toolbar), action: rect(action),
                            targetable: Boolean(target && (target === action || action.contains(target))),
                            scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth,
                          };
                        })()
                        """
                    )
                    self.assertGreater(header["description"]["bottom"], header["description"]["top"], f"{width}px {header}")
                    self.assertGreaterEqual(header["toolbar"]["top"], header["description"]["bottom"] - 1, f"{width}px {header}")
                    for row in ("description", "toolbar"):
                        self.assertLess(abs(header[row]["left"] - header["head"]["left"]), 2, f"{width}px {header}")
                        self.assertLess(abs(header[row]["width"] - header["head"]["width"]), 2, f"{width}px {header}")
                    self.assertTrue(header["targetable"], f"{width}px {header}")
                    self.assertLessEqual(header["scrollWidth"], header["clientWidth"], f"{width}px {header}")

                page.evaluate("document.getElementById('tab-finder').click()")
                page.wait_for(
                    "document.getElementById('tab-finder').getAttribute('aria-selected') === 'true'",
                    "visible finder controls",
                )
                for width in (320, 600, 960):
                    page.set_viewport(width)
                    page.wait_for(f"window.innerWidth === {width}", f"{width}px viewport applied")
                    protocol = page.evaluate(
                        """
                        (() => {
                          const rect = (node) => {
                            const box = node.getBoundingClientRect();
                            return { left: box.left, top: box.top, right: box.right, width: box.width };
                          };
                          const grid = document.querySelector('.protocol-grid');
                          const http = document.getElementById('enable-http').closest('label');
                          const tls = document.getElementById('enable-tls12').closest('label');
                          return {
                            grid: rect(grid), http: rect(http), tls: rect(tls),
                          };
                        })()
                        """
                    )
                    self.assertLess(abs(protocol["http"]["width"] - protocol["tls"]["width"]), 2, f"{width}px {protocol}")
                    if width == 320:
                        self.assertGreater(protocol["tls"]["top"] - protocol["http"]["top"], 2, f"{width}px {protocol}")
                        self.assertLess(abs(protocol["http"]["left"] - protocol["grid"]["left"]), 2, f"{width}px {protocol}")
                        self.assertLess(abs(protocol["http"]["width"] - protocol["grid"]["width"]), 2, f"{width}px {protocol}")
                    else:
                        self.assertLess(abs(protocol["http"]["top"] - protocol["tls"]["top"]), 2, f"{width}px {protocol}")
                        self.assertLess(abs(protocol["http"]["width"] * 2 - protocol["grid"]["width"]), 20, f"{width}px {protocol}")


class TestServerLifecycleTests(unittest.TestCase):
    def test_active_root_managed_startup_keeps_state_when_generic_recovery_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            run_id = "interrupted-root-run"
            update_state(
                config.output.state_dir,
                lambda state: {
                    **state,
                    "current_run_id": run_id,
                    "current_run_name": "zapret-multi-domain-discovery",
                    "current_run_status": "running",
                },
            )

            with patch.object(api_server, "recover_registered_process_runs", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "managed runtime recovery could not be verified"):
                    api_server._recover_runtime_before_serve(config)

            blocked = read_state(config.output.state_dir)
            self.assertEqual(blocked["current_run_id"], run_id)
            self.assertEqual(blocked["current_run_status"], "running")

            with patch.object(api_server, "recover_registered_process_runs", return_value=True):
                api_server._recover_runtime_before_serve(config)

            released = read_state(config.output.state_dir)
            self.assertIsNone(released["current_run_id"])
            self.assertIsNone(released["current_run_status"])

    def test_quarantined_startup_requires_matching_root_recovery_before_state_is_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            run_id = "quarantined-run"
            update_state(
                config.output.state_dir,
                lambda state: {
                    **state,
                    "current_run_id": run_id,
                    "current_run_name": "zapret-multi-domain-discovery",
                    "current_run_status": "quarantined",
                },
            )
            with patch.object(api_server, "recover_quarantined_process_run", side_effect=RuntimeError("root artifacts missing")):
                with self.assertRaisesRegex(RuntimeError, "root artifacts missing"):
                    api_server._recover_runtime_before_serve(config)
            blocked = read_state(config.output.state_dir)
            self.assertEqual(blocked["current_run_id"], run_id)
            self.assertEqual(blocked["current_run_status"], "quarantined")

            with patch.object(api_server, "recover_quarantined_process_run") as recovered:
                api_server._recover_runtime_before_serve(config)

            recovered.assert_called_once_with(run_id)
            released = read_state(config.output.state_dir)
            self.assertIsNone(released["current_run_id"])
            self.assertIsNone(released["current_run_status"])

    def test_startup_failure_closes_listener_that_binds_during_cleanup(self) -> None:
        bind_started = threading.Event()
        allow_bind = threading.Event()
        original_server = api_server.ThreadingHTTPServer

        class DelayedServer(original_server):
            def __init__(self, *args: Any, **kwargs: Any):
                bind_started.set()
                if not allow_bind.wait(timeout=5):
                    raise AssertionError("test did not allow the server to bind")
                super().__init__(*args, **kwargs)

        with tempfile.TemporaryDirectory() as raw:
            server = _TestServer(
                AppConfig(output=OutputConfig(state_dir=Path(raw) / "state")),
                startup_timeout=0.01,
                server_type=DelayedServer,
                startup_timeout_gate=bind_started,
            )
            startup_error: list[BaseException] = []

            def start_server() -> None:
                try:
                    server.__enter__()
                except BaseException as error:
                    startup_error.append(error)

            startup_thread = threading.Thread(target=start_server)
            startup_thread.start()
            try:
                self.assertTrue(bind_started.wait(timeout=5), "DelayedServer construction did not begin")
                self.assertTrue(server._startup_cancelled.wait(timeout=5), "startup cancellation did not begin")
            finally:
                server._startup_cancelled.set()
                allow_bind.set()
                startup_thread.join(timeout=5)

            self.assertFalse(startup_thread.is_alive())
            self.assertEqual(1, len(startup_error))
            self.assertEqual("test server did not bind its HTTP listener", str(startup_error[0]))
            self.assertIsNotNone(server._server)
            self.assertIsNotNone(server._thread)
            self.assertFalse(server._thread.is_alive())
            self.assertIs(api_server.ThreadingHTTPServer, original_server)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", server.port))


class ResponsiveLayoutBrowserTests(_EdgeBrowserTestSupport, unittest.TestCase):
    """WEBL browser matrix: read-only rendering and navigation at each target width."""

    VIEWPORTS = (320, 375, 768, 1024, 1440)

    def assert_document_fits_viewport(self, page: "_EdgeCdp", width: int, state: str) -> None:
        metrics = page.evaluate(
            "({ scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth })"
        )
        self.assertLessEqual(metrics["scrollWidth"], metrics["clientWidth"], f"{width}px {state}: {metrics}")

    def assert_click_targetable(self, page: "_EdgeCdp", selector: str, width: int) -> None:
        result = page.evaluate(
            f"""
            (() => {{
              const node = document.querySelector({json.dumps(selector)});
              if (!node) return {{ found: false }};
              node.scrollIntoView({{ block: 'center', inline: 'nearest' }});
              const rect = node.getBoundingClientRect();
              const style = getComputedStyle(node);
              const target = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
              return {{
                found: true,
                visible: rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none',
                inViewport: rect.top >= 0 && rect.bottom <= window.innerHeight && rect.left >= 0 && rect.right <= window.innerWidth,
                targetable: Boolean(target && (target === node || node.contains(target)))
              }};
            }})()
            """
        )
        self.assertEqual(result, {"found": True, "visible": True, "inViewport": True, "targetable": True}, f"{width}px {selector}")

    def assert_horizontal_scroll_is_local(self, page: "_EdgeCdp", width: int, state: str) -> None:
        unexpected = page.evaluate(
            """
            Array.from(document.querySelectorAll('*')).filter((node) => {
              const style = getComputedStyle(node);
              return node.scrollWidth > node.clientWidth + 1 && ['auto', 'scroll'].includes(style.overflowX)
                && !node.closest('.table-wrap, .code-editor, .strategy-code, .line-numbered-textarea, .raw-log-panel, .release-log');
            }).map((node) => ({ tag: node.tagName, id: node.id, className: String(node.className).slice(0, 160) }))
            """
        )
        self.assertEqual(unexpected, [], f"{width}px {state}: unexpected horizontal scrollers {unexpected}")

    def assert_finder_stack_spans_layout(self, page: "_EdgeCdp", width: int) -> None:
        geometry = page.evaluate(
            """
            (() => {
              const layout = document.querySelector('#tab-panel-finder .finder-layout');
              const stack = layout?.querySelector(':scope > .stack');
              if (!layout || !stack) return null;
              const layoutRect = layout.getBoundingClientRect();
              const stackRect = stack.getBoundingClientRect();
              return {
                layoutWidth: layoutRect.width,
                stackWidth: stackRect.width,
                leftInset: stackRect.left - layoutRect.left,
                rightInset: layoutRect.right - stackRect.right,
              };
            })()
            """
        )
        self.assertIsNotNone(geometry, f"{width}px finder layout and direct stack are required")
        assert geometry is not None
        self.assertGreaterEqual(geometry["stackWidth"], geometry["layoutWidth"] * 0.95, f"{width}px {geometry}")
        self.assertLessEqual(abs(geometry["leftInset"]), 2, f"{width}px {geometry}")
        self.assertLessEqual(abs(geometry["rightInset"]), 2, f"{width}px {geometry}")

    def assert_raw_log_is_visible_and_locally_scrollable(self, page: "_EdgeCdp", width: int) -> None:
        geometry = page.evaluate(
            """
            (() => {
              const panel = document.querySelector('.raw-log-panel');
              const pre = document.getElementById('finder-log');
              if (!panel || !pre) return null;
              panel.open = true;
              pre.scrollIntoView({ block: 'center', inline: 'nearest' });
              const rect = pre.getBoundingClientRect();
              const style = getComputedStyle(pre);
              return {
                panelOpen: panel.open,
                visible: rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden',
                inViewport: rect.left >= 0 && rect.right <= window.innerWidth && rect.top >= 0 && rect.bottom <= window.innerHeight,
                localContainer: pre.closest('.raw-log-panel') === panel,
                overflowX: style.overflowX,
                overflowY: style.overflowY,
                hasLocalVerticalOverflow: pre.scrollHeight > pre.clientHeight,
              };
            })()
            """
        )
        self.assertEqual(
            geometry,
            {
                "panelOpen": True,
                "visible": True,
                "inViewport": True,
                "localContainer": True,
                "overflowX": "auto",
                "overflowY": "auto",
                "hasLocalVerticalOverflow": True,
            },
            f"{width}px raw log geometry: {geometry}",
        )

    @staticmethod
    def render_safe_dynamic_fixtures(page: "_EdgeCdp") -> None:
        page.evaluate(
            """
            (() => {
              const long = (part, count = 18) => Array(count).fill(part).join('');
              state.finderRuns = [{
                kind: 'standard-discovery', run_id: long('run-id-with-a-long-unbroken-value-'),
                timestamp: '2026-08-31T12:00:00Z', status: 'failed',
                domains: [long('very-long-history-domain-name-') + '.example.test'],
                progress: { phase: 'strategy_summary', completed: 1, total: 2, message: long('history-diagnostic-') },
                settings: { enable_http: true, enable_tls12: true, domain_count: 1, scan_level: 'standard' },
                diagnostics: { message: long('history-result-error-') }
              }];
              state.finderRunTotal = 1;
              renderRuns();

              state.status = {
                version: '0.4.1',
                current_run: { run_id: 'fixture-run', status: 'running' },
                state: { last_error: long('terminal-service-error-') }
              };
              state.finderLog = {
                status: 'failed',
                stdout_tail: long('raw-log-output-without-breaks-', 200),
                stderr_tail: long('raw-log-error-without-breaks-', 200),
                stderr_diagnostics: [{ severity: 'error', label: 'Fixture diagnostics', message: long('terminal-diagnostic-') }],
                progress: { phase: 'strategy_summary', completed: 1, total: 2, message: long('terminal-progress-') },
                run_settings: { enable_http: true, enable_tls12: true, domain_count: 1, scan_level: 'standard' }
              };
              renderLog();

              state.backups = [{
                id: 'x'.repeat(180), created_at: '2026-08-31T12:00:00Z',
                checksum_ok: true, size_bytes: 1234, strategy_count: 1
              }];
              state.backupsLoaded = true;
              state.backupsLoading = false;
              renderBackups();

              state.v2flyPreview = { error: true, message: long('v2fly-preview-error-') };
              el('preset-editor-domains').value = long('profile-domain-without-breaks-') + '.example.test';
              updateEditorLineNumbers('preset-editor-domains');
              renderPresetEditorPreview({ name: long('profile-name-'), total: 1, added: 1, removed: 0, unchanged: 0 });
              renderSettings();
              document.querySelector('.raw-log-panel').open = true;
            })()
            """
        )

    @staticmethod
    def login(page: "_EdgeCdp") -> None:
        page.wait_for(
            "document.readyState === 'complete' && document.getElementById('login-form') && !document.getElementById('login-screen').hidden",
            "unauthenticated login form",
        )
        page.evaluate(
            """
            document.getElementById('login-username').value = 'admin';
            document.getElementById('login-password').value = 'admin';
            document.getElementById('login-form').requestSubmit();
            """
        )
        page.wait_for(
            "localStorage.getItem('gp-control-plane-auth-token') && !document.getElementById('app-shell').hidden",
            "authenticated shell",
        )

    def test_settings_omits_vault_controls_and_never_fetches_vaults_while_backups_remain_usable(self) -> None:
        """WEBL-017/T09: Settings owns everyday backups, never clean-install vault operations."""
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with _TestServer(config) as server, _EdgeCdp(self.edge_executable) as page:
                _create_backup(server.port)
                page.navigate(f"http://127.0.0.1:{server.port}/")
                self.login(page)
                page.wait_for(
                    "!document.querySelector('[data-action=\"run-selected-discovery\"]').disabled",
                    "initial system status",
                    timeout=15,
                )
                page.evaluate(
                    """
                    (() => {
                      const originalFetch = window.fetch.bind(window);
                      window.__settingsFetches = [];
                      window.fetch = (...args) => {
                        window.__settingsFetches.push(String(args[0] instanceof Request ? args[0].url : args[0]));
                        return originalFetch(...args);
                      };
                      document.getElementById('tab-settings').click();
                    })()
                    """
                )
                page.wait_for(
                    "document.getElementById('tab-settings').getAttribute('aria-selected') === 'true' && document.getElementById('tab-panel-settings').classList.contains('active')",
                    "Settings tab",
                )
                page.evaluate("document.querySelector('#tab-panel-settings [data-action=\"refresh-backups\"]').click()")
                page.wait_for(
                    "document.querySelector('#backups-table [data-backup-download]')",
                    "backup rendered after Settings refresh",
                )
                result = page.evaluate(
                    """
                    (() => ({
                      vaultElements: document.querySelectorAll('[id*="clean-install-vault"], [data-action*="clean-install-vault"], [data-clean-install-vault-restore]').length,
                      vaultButtons: Array.from(document.querySelectorAll('button')).filter((button) => /vault/i.test(button.textContent || '')).length,
                      ordinaryUiText: document.body.innerText,
                      requested: window.__settingsFetches || [],
                      backupDownload: Boolean(document.querySelector('#backups-table [data-backup-download]')),
                      backupRestore: Boolean(document.querySelector('#backups-table [data-backup-restore]')),
                      backupDelete: Boolean(document.querySelector('#backups-table [data-backup-delete]')),
                    }))()
                    """
                )
                self.assertEqual(result["vaultElements"], 0, result)
                self.assertEqual(result["vaultButtons"], 0, result)
                self.assertNotRegex(result["ordinaryUiText"], r"(?i)vault", result)
                self.assertFalse(any("clean-install-vaults" in url for url in result["requested"]), result)
                self.assertTrue(all(result[key] for key in ("backupDownload", "backupRestore", "backupDelete")), result)

    def test_domain_header_and_debug_help_keep_their_user_visible_geometry(self) -> None:
        """WEBL-016/019: title, metadata, chevron and debug explanation stay coherent at target widths."""
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with _TestServer(config) as server, _EdgeCdp(self.edge_executable) as page:
                page.navigate(f"http://127.0.0.1:{server.port}/")
                self.login(page)
                page.evaluate(
                    """
                    state.activeTab = 'candidates';
                    state.candidateView = 'domain';
                    state.candidateLoading = false;
                    state.candidateDomainsLoaded = true;
                    state.candidateDomains = [{ domain: 'very-long-domain-name-for-header-layout.example.test', strategy_count: 3, protocols: [{ protocol: 'tcp', count: 3 }] }];
                    syncActiveTabUi();
                    renderCandidates();
                    """
                )
                page.wait_for("document.querySelector('#candidates-table .domain-header')", "domain header fixture")
                for width in (320, 375, 1024):
                    page.set_viewport(width)
                    geometry = page.evaluate(
                        """
                        (() => {
                          const header = document.querySelector('#candidates-table .domain-header');
                          const title = header?.querySelector('.domain-title');
                          const meta = header?.querySelector('.domain-meta');
                          if (!header || !title || !meta) return null;
                          header.scrollIntoView({ block: 'center', inline: 'nearest' });
                          const rect = (node) => { const value = node.getBoundingClientRect(); return { left: value.left, right: value.right, top: value.top, bottom: value.bottom }; };
                          const chevron = getComputedStyle(header, '::after');
                          return { header: rect(header), title: rect(title), meta: rect(meta), chevronDisplay: chevron.display, chevronWidth: Number.parseFloat(chevron.width), chevronHeight: Number.parseFloat(chevron.height) };
                        })()
                        """
                    )
                    self.assertIsNotNone(geometry, f"{width}px domain header missing")
                    assert geometry is not None
                    self.assertNotEqual(geometry["chevronDisplay"], "none", f"{width}px {geometry}")
                    self.assertGreater(geometry["chevronWidth"], 0, f"{width}px {geometry}")
                    self.assertGreater(geometry["chevronHeight"], 0, f"{width}px {geometry}")
                    self.assertLessEqual(geometry["title"]["left"], geometry["meta"]["left"], f"{width}px {geometry}")
                    self.assertLessEqual(geometry["meta"]["right"], geometry["header"]["right"], f"{width}px {geometry}")
                    self.assertTrue(
                        geometry["title"]["right"] <= geometry["meta"]["left"] + 1
                        or geometry["title"]["bottom"] <= geometry["meta"]["top"] + 1,
                        f"{width}px metadata overlaps title: {geometry}",
                    )
                    self.assert_document_fits_viewport(page, width, "domain header")

                page.evaluate("document.getElementById('tab-settings').click()")
                page.wait_for("document.getElementById('tab-panel-settings').classList.contains('active')", "Settings tab")
                for width in (320, 375, 1024):
                    page.set_viewport(width)
                    debug_geometry = page.evaluate(
                        """
                        (() => {
                          const input = document.getElementById('settings-debug-stdout');
                          const label = input?.closest('label');
                          const note = Array.from(document.querySelectorAll('.setting-note')).find((node) => node.textContent.includes('Включает расширенную запись stdout проверки стратегий'));
                          let block = label;
                          while (block && !block.contains(note)) block = block.parentElement;
                          if (!input || !label || !note || !block) return null;
                          block.scrollIntoView({ block: 'center', inline: 'nearest' });
                          const rect = (node) => { const value = node.getBoundingClientRect(); return { left: value.left, right: value.right, top: value.top, bottom: value.bottom }; };
                          return { labelContainsInput: label.contains(input), checkboxCount: block.querySelectorAll('input[type="checkbox"]').length, block: rect(block), note: rect(note) };
                        })()
                        """
                    )
                    self.assertIsNotNone(debug_geometry, f"{width}px debug control block missing")
                    assert debug_geometry is not None
                    self.assertTrue(debug_geometry["labelContainsInput"], f"{width}px {debug_geometry}")
                    self.assertEqual(debug_geometry["checkboxCount"], 1, f"{width}px debug note escaped its control block: {debug_geometry}")
                    self.assertGreaterEqual(debug_geometry["note"]["left"], debug_geometry["block"]["left"] - 1, f"{width}px {debug_geometry}")
                    self.assertLessEqual(debug_geometry["note"]["right"], debug_geometry["block"]["right"] + 1, f"{width}px {debug_geometry}")
                    self.assertGreaterEqual(debug_geometry["note"]["top"], debug_geometry["block"]["top"] - 1, f"{width}px {debug_geometry}")
                    self.assertLessEqual(debug_geometry["note"]["bottom"], debug_geometry["block"]["bottom"] + 1, f"{width}px {debug_geometry}")
                    self.assert_document_fits_viewport(page, width, "debug control block")

    def test_web_color_roles_status_markup_focus_and_backup_fixture(self) -> None:
        """WEBC browser matrix: roles stay distinct and status text is never color-only."""
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with _TestServer(config) as server, _EdgeCdp(self.edge_executable) as page:
                page.navigate(f"http://127.0.0.1:{server.port}/")
                self.login(page)
                page.evaluate(
                    """
                    setMessage('<небезопасный>', 'bad');
                    showToast('<очередь>', 'queue');
                    setLoginError('Ошибка входа');
                    state.v2flyPreview = { error: true, message: 'Ошибка v2fly' };
                    renderV2flyPreview();
                    renderPresetNewPreview('Ошибка списка', 'bad');
                    document.getElementById('backups-table').innerHTML = backupCard({
                      id: 'fixture-backup', created_at: 'now', checksum_ok: true, size_bytes: 1, strategy_count: 2
                    });
                    """
                )
                for width in self.VIEWPORTS:
                    page.set_viewport(width)
                    page.evaluate("document.getElementById('backups-table').innerHTML = backupCard({ id: 'fixture-backup', created_at: 'now', checksum_ok: true, size_bytes: 1, strategy_count: 2 });")
                    result = page.evaluate(
                        """
                        (() => {
                          const root = getComputedStyle(document.documentElement);
                          const primary = document.querySelector('[data-action="run-selected-discovery"]');
                          const stop = document.querySelector('[data-action="stop-current"]');
                          const restore = document.querySelector('[data-backup-restore]');
                          const remove = document.querySelector('[data-backup-delete]');
                          const beforeFocus = { scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth };
                          primary.focus({ preventScroll: true });
                          const role = (node) => { const style = getComputedStyle(node); return { background: style.backgroundColor, color: style.color, border: style.borderColor }; };
                          const tokenColor = (token) => {
                            const probe = document.createElement('span');
                            probe.style.color = `var(${token})`;
                            document.body.appendChild(probe);
                            const color = getComputedStyle(probe).color;
                            probe.remove();
                            return color;
                          };
                          const message = document.getElementById('message');
                          const toast = document.getElementById('toast');
                          const focus = { outline: getComputedStyle(primary).outline, border: getComputedStyle(primary).borderColor };
                          primary.blur();
                          return {
                            primary: role(primary), stop: role(stop), restore: role(restore), remove: role(remove),
                            beforeFocus,
                            blue: tokenColor('--blue'), blueStrong: tokenColor('--blue-strong'), red: tokenColor('--red'),
                            surfaceCode: tokenColor('--surface-code'), surfaceSoft: tokenColor('--surface-soft'),
                            focus,
                            message: { marker: message.querySelector('.status-marker')?.getAttribute('aria-hidden'), label: message.querySelector('.status-label')?.textContent, reason: message.querySelector('.status-reason')?.textContent, border: getComputedStyle(message).borderColor },
                            toast: { marker: toast.querySelector('.status-marker')?.getAttribute('aria-hidden'), label: toast.querySelector('.status-label')?.textContent, reason: toast.querySelector('.status-reason')?.textContent, border: getComputedStyle(toast).borderColor },
                            backup: { dangerZone: Boolean(document.querySelector('.backup-danger-zone')), restoreText: document.querySelector('[data-backup-restore]')?.parentElement.textContent, deleteText: document.querySelector('.backup-danger-zone')?.textContent, checksumText: document.querySelector('.backup-card')?.textContent || '' },
                            errors: ['login-error', 'v2fly-preview-result', 'preset-new-preview'].map((id) => { const node = document.getElementById(id); const style = getComputedStyle(node); return { id, marker: Boolean(node.querySelector('.status-marker.bad')), text: style.color, background: style.backgroundColor, border: style.borderColor }; }),
                          };
                        })()
                        """
                    )
                    self.assertEqual(result["primary"]["background"], result["blue"], f"{width}px {result}")
                    self.assertEqual(result["primary"]["color"], result["surfaceCode"], f"{width}px {result}")
                    self.assertEqual(result["focus"]["border"], result["blueStrong"], f"{width}px {result}")
                    self.assertEqual(result["restore"]["background"], result["surfaceSoft"], f"{width}px {result}")
                    self.assertEqual(result["remove"]["background"], result["red"], f"{width}px {result}")
                    self.assertNotEqual(result["stop"]["background"], result["red"], f"{width}px {result}")
                    self.assertIn("rgb", result["focus"]["outline"], f"{width}px {result}")
                    self.assertEqual(result["message"], {"marker": "true", "label": "Ошибка", "reason": "<небезопасный>", "border": result["red"]})
                    self.assertEqual(result["toast"], {"marker": "true", "label": "В очереди", "reason": "<очередь>", "border": result["blue"]})
                    self.assertTrue(result["backup"]["dangerZone"], result)
                    self.assertIn("заменит текущие данные", result["backup"]["restoreText"])
                    self.assertIn("без возможности восстановления", result["backup"]["deleteText"])
                    self.assertNotIn("checksum", result["backup"]["checksumText"].lower())
                    for error in result["errors"]:
                        self.assertTrue(error["marker"], result)
                        self.assertEqual(error["text"], result["surfaceCode"] if False else "rgb(215, 224, 234)", result)
                    metrics = page.evaluate("({ scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth })")
                    self.assertLessEqual(metrics["scrollWidth"], metrics["clientWidth"], f"{width}px WEBC roles and backup fixture: {metrics}; before focus={result['beforeFocus']}")

    def test_initial_missing_system_status_stays_neutral_and_blocks_actions(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with _TestServer(config) as server, _EdgeCdp(self.edge_executable) as page:
                page.navigate(f"http://127.0.0.1:{server.port}/")
                self.login(page)
                page.wait_for(
                    "!document.querySelector('[data-action=\"run-selected-discovery\"]').disabled",
                    "initial system status",
                    timeout=15,
                )
                page.evaluate("stopRealtimeEvents(); stopRealtimeFallback(); clearInitialSystemStatusRetry();")
                result = page.evaluate(
                    """
                    (() => {
                      state.status = { state: 'idle' };
                      state.statusLoading = true;
                      renderMetrics();
                      renderRunLaunchSummary();
                      const reading = {
                        system: document.getElementById('metric-zapret').innerText,
                        note: document.getElementById('metric-zapret-note').innerText,
                        job: document.getElementById('metric-job').innerText,
                        disabled: document.querySelector('[data-action="run-selected-discovery"]').disabled,
                        readiness: document.getElementById('run-launch-readiness').innerText,
                      };
                      state.statusLoading = false;
                      renderMetrics();
                      return {
                        reading,
                        unavailable: {
                          system: document.getElementById('metric-zapret').innerText,
                          note: document.getElementById('metric-zapret-note').innerText,
                          disabled: document.querySelector('[data-action="run-selected-discovery"]').disabled,
                        },
                      };
                    })()
                    """
                )
                self.assertIn("Проверяем", result["reading"]["system"])
                self.assertEqual(result["reading"]["note"], "получаем состояние")
                self.assertEqual(result["reading"]["job"], "Ожидание")
                self.assertTrue(result["reading"]["disabled"])
                self.assertIn("Проверяем", result["reading"]["readiness"])
                self.assertNotIn("Проблема", result["reading"]["system"])
                self.assertIn("Нет статуса", result["unavailable"]["system"])
                self.assertEqual(result["unavailable"]["note"], "обновите страницу")
                self.assertTrue(result["unavailable"]["disabled"])

    def test_web_color_keyboard_contrast_and_disclosure_matrix(self) -> None:
        """WEBC R04: keyboard traversal and computed normal/hover/active contrast evidence."""
        def contrast(style: dict[str, object], key: str, minimum: float, context: str) -> None:
            self.assertGreaterEqual(float(style[key]), minimum, f"{context}: {style}")

        def tab_round_trip(page: "_EdgeCdp", selector: str, width: int) -> None:
            if selector == '[data-action="stop-current"]':
                page.evaluate("document.querySelector('[data-action=\"stop-current\"]').disabled = false;")
            page.evaluate(f"document.querySelector({json.dumps(selector)}).focus({{ preventScroll: true, focusVisible: true }});")
            self.assertTrue(page.evaluate(f"document.activeElement === document.querySelector({json.dumps(selector)})"), f"{width}px could not focus {selector}")
            page.press_key("Tab", modifiers=8)  # CDP Shift modifier.
            self.assertFalse(page.evaluate(f"document.activeElement === document.querySelector({json.dumps(selector)})"), f"{width}px Shift+Tab did not traverse from {selector}")
            page.evaluate(f"document.querySelector({json.dumps(selector)}).focus({{ preventScroll: true, focusVisible: true }});")
            page.press_key("Tab")
            self.assertFalse(page.evaluate(f"document.activeElement === document.querySelector({json.dumps(selector)})"), f"{width}px Tab did not traverse from {selector}")

        def computed_states(page: "_EdgeCdp", selector: str) -> dict[str, dict[str, object]]:
            geometry = page.evaluate(
                f"""
                (() => {{
                  const node = document.querySelector({json.dumps(selector)});
                  if (!node) return null;
                  node.scrollIntoView({{ block: 'center', inline: 'nearest' }});
                  const rect = node.getBoundingClientRect();
                  return {{ x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 }};
                }})()
                """
            )
            self.assertIsNotNone(geometry, selector)
            assert geometry is not None
            page.move_pointer_to(0, 0)
            normal = page.evaluate(f"(() => {{ const s = getComputedStyle(document.querySelector({json.dumps(selector)})); return contrastStyle(s); }})()")
            page.evaluate(f"document.querySelector({json.dumps(selector)}).focus({{ preventScroll: true, focusVisible: true }});")
            focus = page.evaluate(f"(() => {{ const node = document.querySelector({json.dumps(selector)}); const s = getComputedStyle(node); return {{ ...contrastStyle(s), focusVisible: node.matches(':focus-visible'), outlineStyle: s.outlineStyle, outlineWidth: Number.parseFloat(s.outlineWidth), outlineColor: s.outlineColor }}; }})()")
            page.move_pointer_to(geometry["x"], geometry["y"])
            hover = page.evaluate(f"(() => {{ const s = getComputedStyle(document.querySelector({json.dumps(selector)})); return contrastStyle(s); }})()")
            page.pointer_down(geometry["x"], geometry["y"])
            active = page.evaluate(f"(() => {{ const s = getComputedStyle(document.querySelector({json.dumps(selector)})); return contrastStyle(s); }})()")
            page.pointer_up(0, 0)
            return {"normal": normal, "focus": focus, "hover": hover, "active": active}

        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with _TestServer(config) as server, _EdgeCdp(self.edge_executable) as page:
                page.navigate(f"http://127.0.0.1:{server.port}/")
                self.login(page)
                page.evaluate("new Promise((resolve) => setTimeout(resolve, 300)).then(() => { stopRealtimeEvents(); stopRealtimeFallback(); })")
                page.evaluate(
                    """
                    window.contrastStyle = (style) => {
                      const rgb = (value) => (value.match(/\\d+(?:\\.\\d+)?/g) || []).slice(0, 3).map(Number);
                      const luminance = (value) => rgb(value).map((channel) => {
                        const normalized = channel / 255;
                        return normalized <= .04045 ? normalized / 12.92 : ((normalized + .055) / 1.055) ** 2.4;
                      }).reduce((total, channel, index) => total + channel * [0.2126, 0.7152, 0.0722][index], 0);
                      const ratio = (first, second) => {
                        const [lighter, darker] = [luminance(first), luminance(second)].sort((a, b) => b - a);
                        return (lighter + .05) / (darker + .05);
                      };
                      return { text: ratio(style.color, style.backgroundColor), border: ratio(style.borderColor, style.backgroundColor), focus: Math.max(ratio(style.outlineColor, style.backgroundColor), ratio(style.borderColor, style.backgroundColor)) };
                    };
                    document.querySelector('[data-action="stop-current"]').disabled = false;
                    document.querySelector('details.preset-panel').open = true;
                    const fixture = document.createElement('div');
                    fixture.id = 'webc-contrast-fixture';
                    fixture.style.cssText = 'position:fixed;left:12px;bottom:12px;z-index:10000;display:flex;gap:8px';
                    fixture.innerHTML = '<button class="secondary" data-backup-restore="contrast" type="button">Восстановить</button><button class="secondary danger" data-backup-delete="contrast" type="button">Удалить</button>';
                    document.body.appendChild(fixture);
                    """
                )
                for width in self.VIEWPORTS:
                    page.set_viewport(width)
                    page.evaluate("document.getElementById('tab-finder').click(); document.querySelector('details.preset-panel').open = true; document.querySelector('[data-action=\"stop-current\"]').disabled = false;")
                    for selector in ('#tab-finder', 'details.preset-panel > summary', '[data-action="run-selected-discovery"]', '[data-action="stop-current"]'):
                        tab_round_trip(page, selector, width)
                    disclosure = page.evaluate(
                        """
                        (() => {
                          const summary = document.querySelector('details.preset-panel > summary');
                          const panel = summary.closest('details');
                          const before = getComputedStyle(summary, '::before');
                          const after = getComputedStyle(summary, '::after');
                          return { open: panel.open, before: [Number.parseFloat(before.width), Number.parseFloat(before.height)], after: after.content, summaryBackground: getComputedStyle(summary).backgroundColor, panelBackground: getComputedStyle(panel).backgroundColor };
                        })()
                        """
                    )
                    self.assertTrue(disclosure["open"], f"{width}px {disclosure}")
                    self.assertGreaterEqual(disclosure["before"][0], 8, f"{width}px {disclosure}")
                    self.assertGreaterEqual(disclosure["before"][1], 8, f"{width}px {disclosure}")
                    self.assertEqual(disclosure["after"], "none", f"{width}px {disclosure}")
                    self.assertNotEqual(disclosure["summaryBackground"], "rgba(0, 0, 0, 0)", f"{width}px {disclosure}")
                    for selector in ('#tab-finder', 'details.preset-panel > summary', '[data-action="run-selected-discovery"]', '[data-action="stop-current"]'):
                        for state, style in computed_states(page, selector).items():
                            contrast(style, "text", 4.5, f"{width}px {selector} {state} text")
                            if state == "focus":
                                self.assertTrue(style["focusVisible"], f"{width}px {selector} {style}")
                                self.assertNotEqual(style["outlineStyle"], "none", f"{width}px {selector} {style}")
                                self.assertGreaterEqual(style["outlineWidth"], 2, f"{width}px {selector} {style}")
                                contrast(style, "focus", 3.0, f"{width}px {selector} focus indicator")
                            if selector == '[data-action="stop-current"]':
                                contrast(style, "border", 3.0, f"{width}px {selector} {state} border")

                    for selector in ('#webc-contrast-fixture [data-backup-restore]', '#webc-contrast-fixture [data-backup-delete]'):
                        tab_round_trip(page, selector, width)
                        for state, style in computed_states(page, selector).items():
                            contrast(style, "text", 4.5, f"{width}px {selector} {state} text")
                            if state == "focus":
                                self.assertTrue(style["focusVisible"], f"{width}px {selector} {style}")
                                self.assertNotEqual(style["outlineStyle"], "none", f"{width}px {selector} {style}")
                                self.assertGreaterEqual(style["outlineWidth"], 2, f"{width}px {selector} {style}")
                                contrast(style, "focus", 3.0, f"{width}px {selector} focus indicator")
                            if selector == '[data-backup-restore]':
                                contrast(style, "border", 3.0, f"{width}px {selector} {state} border")
                    self.assert_document_fits_viewport(page, width, "WEBC keyboard, contrast and disclosure")
                page.evaluate("stopRealtimeEvents(); stopRealtimeFallback(); window.stop();")

    def test_advanced_disclosure_uses_one_chevron_and_single_actions_are_balanced(self) -> None:
        """WEBL-020/021: one chevron; one action fills its original action row."""
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with _TestServer(config) as server, _EdgeCdp(self.edge_executable) as page:
                page.navigate(f"http://127.0.0.1:{server.port}/")
                self.login(page)

                for width in (320, 600, 960, 1440):
                    page.set_viewport(width)
                    disclosure = page.evaluate(
                        """
                        (() => {
                          const summary = document.querySelector('#tab-panel-finder details.preset-panel > summary');
                          if (!summary) return null;
                          summary.scrollIntoView({ block: 'center', inline: 'nearest' });
                          const before = getComputedStyle(summary, '::before');
                          const after = getComputedStyle(summary, '::after');
                          return {
                            helperCount: summary.querySelectorAll('.helper-text').length,
                            beforeWidth: Number.parseFloat(before.width),
                            beforeHeight: Number.parseFloat(before.height),
                            afterContent: after.content,
                          };
                        })()
                        """
                    )
                    self.assertIsNotNone(disclosure, f"{width}px advanced summary missing")
                    assert disclosure is not None
                    self.assertEqual(disclosure["helperCount"], 0, f"{width}px duplicate advanced meta: {disclosure}")
                    self.assertGreaterEqual(disclosure["beforeWidth"], 8, f"{width}px chevron missing: {disclosure}")
                    self.assertGreaterEqual(disclosure["beforeHeight"], 8, f"{width}px chevron missing: {disclosure}")
                    self.assertEqual(disclosure["afterContent"], "none", f"{width}px text disclosure leaked: {disclosure}")
                    self.assert_document_fits_viewport(page, width, "advanced disclosure")

                page.evaluate("document.getElementById('tab-settings').click()")
                page.wait_for("document.getElementById('tab-panel-settings').classList.contains('active')", "Settings tab")
                for width in (320, 600, 960, 1440):
                    page.set_viewport(width)
                    action = page.evaluate(
                        """
                        (() => {
                          const button = document.querySelector('#tab-panel-settings [data-action="save-settings"]');
                          const row = button?.closest('.button-row');
                          if (!button || !row) return null;
                          button.scrollIntoView({ block: 'center', inline: 'nearest' });
                          const rect = (node) => { const box = node.getBoundingClientRect(); return { left: box.left, width: box.width, right: box.right }; };
                          return { button: rect(button), row: rect(row), children: row.children.length };
                        })()
                        """
                    )
                    self.assertIsNotNone(action, f"{width}px save-settings action missing")
                    assert action is not None
                    self.assertEqual(action["children"], 1, f"{width}px fixture is not a single action: {action}")
                    if width == 320:
                        self.assertAlmostEqual(action["button"]["width"], action["row"]["width"], delta=2, msg=f"{width}px {action}")
                    else:
                        self.assertAlmostEqual(action["button"]["width"], action["row"]["width"], delta=2, msg=f"{width}px {action}")
                        self.assertAlmostEqual(
                            action["button"]["left"],
                            action["row"]["left"],
                            delta=2,
                            msg=f"{width}px {action}",
                        )
                    self.assert_document_fits_viewport(page, width, "single settings action")

    def test_login_shell_tabs_candidates_and_dynamic_layouts_fit_all_target_viewports(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with _TestServer(config) as server, _EdgeCdp(self.edge_executable) as page:
                page.set_viewport(self.VIEWPORTS[0])
                page.navigate(f"http://127.0.0.1:{server.port}/")
                page.wait_for(
                    "document.readyState === 'complete' && document.getElementById('login-form') && !document.getElementById('login-screen').hidden",
                    "unauthenticated login form",
                )
                for width in self.VIEWPORTS:
                    page.set_viewport(width)
                    page.evaluate(
                        "document.getElementById('login-error').textContent = 'Ошибка входа: ' + 'очень-длинное-сообщение-'.repeat(24);"
                    )
                    self.assert_click_targetable(page, "#login-form button[type='submit']", width)
                    self.assert_document_fits_viewport(page, width, "login with long error")
                    self.assert_horizontal_scroll_is_local(page, width, "login with long error")

                page.evaluate(
                    """
                    document.getElementById('login-username').value = 'admin';
                    document.getElementById('login-password').value = 'admin';
                    document.getElementById('login-form').requestSubmit();
                    """
                )
                page.wait_for(
                    "localStorage.getItem('gp-control-plane-auth-token') && !document.getElementById('app-shell').hidden",
                    "authenticated shell",
                )
                page.evaluate(
                    """
                    const finderTab = document.getElementById('tab-finder');
                    finderTab.focus();
                    finderTab.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
                    """
                )
                page.wait_for(
                    "document.activeElement === document.getElementById('tab-history') && document.getElementById('tab-history').getAttribute('aria-selected') === 'true' && document.getElementById('tab-history').tabIndex === 0",
                    "primary tab ArrowRight roving focus",
                )
                page.evaluate(
                    "document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true }))"
                )
                page.wait_for(
                    "document.activeElement === document.getElementById('tab-settings') && document.getElementById('tab-settings').getAttribute('aria-selected') === 'true'",
                    "primary tab End roving focus",
                )

                primary_tabs = ("finder", "history", "candidates", "terminal", "lists", "settings")
                visible_actions = {
                    "finder": "#tab-panel-finder button[data-action='run-selected-discovery']",
                    "terminal": "#tab-panel-terminal button[data-action='stop-current']",
                    "lists": "#tab-panel-lists button[data-action='preset-editor-save']",
                    "settings": "#tab-panel-settings button[data-action='save-settings']",
                }
                for width in self.VIEWPORTS:
                    page.set_viewport(width)
                    for tab in primary_tabs:
                        page.evaluate(f"document.getElementById('tab-{tab}').click()")
                        page.wait_for(
                            f"document.getElementById('tab-{tab}').getAttribute('aria-selected') === 'true' && document.getElementById('tab-panel-{tab}').classList.contains('active')",
                            f"active {tab} tab",
                        )
                        self.assert_click_targetable(page, f"#tab-{tab}", width)
                        if tab in visible_actions:
                            self.assert_click_targetable(page, visible_actions[tab], width)
                        self.assert_document_fits_viewport(page, width, f"{tab} tab")
                        self.assert_horizontal_scroll_is_local(page, width, f"{tab} tab")

                    page.evaluate(
                        """
                        state.activeTab = 'candidates';
                        state.candidateView = 'domain';
                        state.candidateLoading = false;
                        state.candidateDomainsLoaded = true;
                        state.candidateDomains = [];
                        syncActiveTabUi();
                        renderCandidates();
                        """
                    )
                    page.wait_for(
                        "document.querySelector('#candidates-table .empty')?.textContent.includes('По фильтру ничего не найдено')",
                        "candidate empty state",
                    )
                    self.assert_click_targetable(page, "#candidate-view-domain", width)
                    self.assert_document_fits_viewport(page, width, "candidate empty state")
                    self.assert_horizontal_scroll_is_local(page, width, "candidate empty state")

                    page.evaluate(
                        """
                        state.candidateDomains = [{
                          domain: ('very-long-domain-name-without-breaks-'.repeat(12) + 'example.test'),
                          strategy_count: 1,
                          protocols: [{ protocol: 'tcp', count: 1 }]
                        }];
                        renderCandidates();
                        setMessage('Ошибка: ' + 'длинный-диагностический-текст-'.repeat(20), 'bad');
                        """
                    )
                    page.wait_for("document.querySelector('#candidates-table .domain-group')", "long candidate row")
                    self.assert_document_fits_viewport(page, width, "long candidate and error state")
                    self.assert_horizontal_scroll_is_local(page, width, "long candidate and error state")

                    page.evaluate("document.getElementById('candidate-view-common').click()")
                    page.wait_for(
                        "document.getElementById('candidate-view-common').getAttribute('aria-selected') === 'true'",
                        "common candidate subtab",
                    )
                    self.assert_click_targetable(page, "#tab-panel-candidates button[data-action='build-candidate-result']", width)
                    for mode in ("coverage", "minimal", "balance"):
                        page.evaluate(f"document.getElementById('candidate-result-mode-{mode}').click()")
                        page.wait_for(
                            f"document.getElementById('candidate-result-mode-{mode}').getAttribute('aria-selected') === 'true'",
                            f"candidate result {mode} mode",
                        )
                        self.assert_click_targetable(page, f"#candidate-result-mode-{mode}", width)
                        self.assert_document_fits_viewport(page, width, f"candidate {mode} mode")
                        self.assert_horizontal_scroll_is_local(page, width, f"candidate {mode} mode")

                self.render_safe_dynamic_fixtures(page)
                page.wait_for(
                    "document.querySelector('#finder-runs-table .run-card') && document.querySelector('#events-panel .event-card') && document.querySelector('#backups-table [data-backup-download]')",
                    "safe dynamic layout fixtures",
                )
                for width in self.VIEWPORTS:
                    page.set_viewport(width)
                    for tab, fixture_selector in (
                        ("history", "#finder-runs-table .run-card"),
                        ("terminal", "#events-panel .event-card"),
                        ("lists", "#preset-editor-preview"),
                        ("settings", "#backups-table [data-backup-download]"),
                    ):
                        page.evaluate(f"document.getElementById('tab-{tab}').click()")
                        page.wait_for(f"!document.querySelector({json.dumps(fixture_selector)}).closest('[hidden]')", f"visible {tab} fixture")
                        self.assert_click_targetable(page, fixture_selector, width)
                        self.assert_document_fits_viewport(page, width, f"{tab} dynamic fixture")
                        self.assert_horizontal_scroll_is_local(page, width, f"{tab} dynamic fixture")
                        if tab == "terminal":
                            self.assert_raw_log_is_visible_and_locally_scrollable(page, width)

                    self.assert_click_targetable(page, "#backups-table [data-backup-download]", width)
                    self.assert_click_targetable(page, "#backups-table [data-backup-restore]", width)
                    self.assert_click_targetable(page, "#backups-table [data-backup-delete]", width)
                    self.assert_document_fits_viewport(page, width, "long backup identifiers")
                    self.assert_horizontal_scroll_is_local(page, width, "long backup identifiers")

                for width in (960, 1024, 1440):
                    page.set_viewport(width)
                    page.evaluate("document.getElementById('tab-finder').click()")
                    page.wait_for("document.getElementById('tab-finder').getAttribute('aria-selected') === 'true'", "finder tab")
                    self.assert_finder_stack_spans_layout(page, width)
                    self.assert_document_fits_viewport(page, width, "finder full-span layout")


class BrowserTestDiscoveryTests(unittest.TestCase):
    def test_browser_cases_are_discovered_once_per_concrete_suite(self) -> None:
        """WEBL-F09: responsive cases must share setup, not inherit another suite's tests."""
        loader = unittest.TestLoader()
        auth_names = set(loader.getTestCaseNames(EdgeBearerAuthBrowserTests))
        responsive_names = set(loader.getTestCaseNames(ResponsiveLayoutBrowserTests))

        self.assertFalse(auth_names & responsive_names)
        self.assertEqual(
            responsive_names,
            {
                "test_advanced_disclosure_uses_one_chevron_and_single_actions_are_balanced",
                "test_domain_header_and_debug_help_keep_their_user_visible_geometry",
                "test_login_shell_tabs_candidates_and_dynamic_layouts_fit_all_target_viewports",
                "test_settings_omits_vault_controls_and_never_fetches_vaults_while_backups_remain_usable",
                "test_initial_missing_system_status_stays_neutral_and_blocks_actions",
                "test_web_color_roles_status_markup_focus_and_backup_fixture",
                "test_web_color_keyboard_contrast_and_disclosure_matrix",
            },
        )
        self.assertEqual(
            loader.loadTestsFromTestCase(ResponsiveLayoutBrowserTests).countTestCases(), len(responsive_names)
        )


class EdgeCdpLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._platform_patch = patch.object(sys, "platform", "linux")
        self._platform_patch.start()

    def tearDown(self) -> None:
        self._platform_patch.stop()

    def test_startup_failure_cleans_its_process_profile_and_reports_redacted_diagnostics(self) -> None:
        class FakeProfile:
            name = "fake-edge-profile"

            def __init__(self) -> None:
                self.cleaned = False

            def cleanup(self) -> None:
                self.cleaned = True

        class FakePopen:
            def __init__(self, *_args: Any, **kwargs: Any) -> None:
                self.returncode: int | None = None
                self.terminate_calls = 0
                self.wait_calls: list[float] = []
                kwargs["stderr"].write(b"headless startup failed; token=top-secret\\n")

            def poll(self) -> int | None:
                return self.returncode

            def terminate(self) -> None:
                self.terminate_calls += 1

            def wait(self, timeout: float) -> int:
                self.wait_calls.append(timeout)
                self.returncode = 17
                return self.returncode

        profile = FakeProfile()
        processes: list[FakePopen] = []

        def popen(*args: Any, **kwargs: Any) -> FakePopen:
            process = FakePopen(*args, **kwargs)
            processes.append(process)
            return process

        with (
            patch.object(tempfile, "TemporaryDirectory", return_value=profile),
            patch.object(subprocess, "Popen", side_effect=popen) as mock_popen,
            patch(__name__ + "._free_port", return_value=9222),
            patch(__name__ + "._wait_for_http", side_effect=AssertionError("connection refused")),
        ):
            with self.assertRaisesRegex(
                AssertionError,
                r"Edge CDP startup failed: connection refused; process exit code 17; stderr: .*token=\[REDACTED\]",
            ) as raised:
                _EdgeCdp(Path("fake-msedge.exe")).__enter__()

        self.assertNotIn("top-secret", str(raised.exception))
        self.assertEqual(1, mock_popen.call_count)
        self.assertEqual(
            [
                "fake-msedge.exe",
                "--headless=new",
                "--remote-debugging-port=9222",
                "--user-data-dir=fake-edge-profile",
                "--password-store=basic",
                "--disable-background-networking",
                "--disable-sync",
                "--no-service-autorun",
                "--no-first-run",
                "--no-default-browser-check",
                "about:blank",
            ],
            mock_popen.call_args.args[0],
        )
        self.assertEqual(1, len(processes))
        self.assertEqual(1, processes[0].terminate_calls)
        self.assertEqual([5], processes[0].wait_calls)
        self.assertTrue(profile.cleaned)

    def test_windows_job_closes_before_profile_cleanup_when_parent_already_exited(self) -> None:
        events: list[str] = []

        class FakePopen:
            def __init__(self) -> None:
                self.returncode = 0

            def poll(self) -> int | None:
                return self.returncode

        class Profile:
            def __init__(self) -> None:
                self.cleanup_calls = 0

            def cleanup(self) -> None:
                events.append("profile cleanup")
                self.cleanup_calls += 1

        class FakeWindowsJob:
            def __init__(self) -> None:
                events.append("job created")

            def close(self) -> None:
                events.append("job close")

        process = FakePopen()
        profile = Profile()
        edge = _EdgeCdp(Path("fake-msedge.exe"))
        edge._process = process  # type: ignore[assignment]
        edge._job = FakeWindowsJob()  # type: ignore[assignment]
        edge._profile = profile

        with patch.object(sys, "platform", "win32"):
            edge.__exit__(None, None, None)

        self.assertEqual(1, profile.cleanup_calls)
        self.assertEqual(["job created", "job close", "profile cleanup"], events)
        self.assertIsNone(edge._process)
        self.assertIsNone(edge._job)
        self.assertIsNone(edge._profile)

    def test_windows_startup_assigns_edge_to_kill_on_close_job(self) -> None:
        events: list[str] = []

        class FakeProfile:
            name = "fake-edge-profile"

            def cleanup(self) -> None:
                events.append("profile cleanup")

        class FakePopen:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                self.returncode = 0

            def poll(self) -> int:
                return self.returncode

        class FakeWindowsJob:
            def __init__(self) -> None:
                events.append("job created")

            def assign(self, process: FakePopen) -> None:
                self.process = process
                events.append("job assigned")

            def close(self) -> None:
                events.append("job close")

        profile = FakeProfile()
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(tempfile, "TemporaryDirectory", return_value=profile),
            patch.object(subprocess, "Popen", return_value=FakePopen()),
            patch(__name__ + "._WindowsJob", FakeWindowsJob),
            patch(__name__ + "._free_port", return_value=9222),
            patch(__name__ + "._wait_for_http", side_effect=AssertionError("connection refused")),
        ):
            with self.assertRaisesRegex(AssertionError, r"Edge CDP startup failed: connection refused; process exit code 0"):
                _EdgeCdp(Path("fake-msedge.exe")).__enter__()

        self.assertEqual(["job created", "job assigned", "job close", "profile cleanup"], events)

    def test_cleanup_succeeds_when_process_reaps_after_terminate(self) -> None:
        class FakePopen:
            def __init__(self) -> None:
                self.returncode: int | None = None
                self.terminate_calls = 0
                self.kill_calls = 0
                self.wait_calls: list[float] = []

            def poll(self) -> int | None:
                return self.returncode

            def terminate(self) -> None:
                self.terminate_calls += 1

            def kill(self) -> None:
                self.kill_calls += 1

            def wait(self, timeout: float) -> int:
                self.wait_calls.append(timeout)
                self.returncode = 0
                return self.returncode

        process = FakePopen()
        edge = _EdgeCdp(Path("fake-msedge.exe"))
        edge._process = process  # type: ignore[assignment]

        edge.__exit__(None, None, None)

        self.assertEqual(1, process.terminate_calls)
        self.assertEqual(0, process.kill_calls)
        self.assertEqual([5], process.wait_calls)
        self.assertIsNone(edge._process)

    def test_cleanup_failure_after_second_timeout_fails_or_notes_primary_error(self) -> None:
        class FakePopen:
            def __init__(self) -> None:
                self.terminate_calls = 0
                self.kill_calls = 0
                self.wait_calls: list[float] = []

            @staticmethod
            def poll() -> None:
                return None

            def terminate(self) -> None:
                self.terminate_calls += 1

            def kill(self) -> None:
                self.kill_calls += 1

            def wait(self, timeout: float) -> int:
                self.wait_calls.append(timeout)
                raise subprocess.TimeoutExpired("fake-msedge.exe", timeout)

        def edge_with_unreaped_process() -> tuple[_EdgeCdp, FakePopen]:
            process = FakePopen()
            edge = _EdgeCdp(Path("fake-msedge.exe"))
            edge._process = process  # type: ignore[assignment]
            return edge, process

        edge, process = edge_with_unreaped_process()
        with self.assertRaisesRegex(AssertionError, r"Edge CDP cleanup failed: process did not exit after kill"):
            edge.__exit__(None, None, None)

        self.assertEqual(1, process.terminate_calls)
        self.assertEqual(1, process.kill_calls)
        self.assertEqual([5, 5], process.wait_calls)
        self.assertIsNone(edge._process)

        edge, process = edge_with_unreaped_process()
        primary_error = AssertionError("primary test failure")

        edge.__exit__(AssertionError, primary_error, None)

        self.assertEqual("primary test failure", str(primary_error))
        self.assertEqual(
            ["Edge CDP cleanup failed: process did not exit after kill; process exit code None"],
            primary_error.__notes__,
        )
        self.assertEqual(1, process.terminate_calls)
        self.assertEqual(1, process.kill_calls)
        self.assertEqual([5, 5], process.wait_calls)
        self.assertIsNone(edge._process)

    def test_cleanup_failure_after_terminate_os_error_fails_or_notes_primary_error(self) -> None:
        class FakePopen:
            def __init__(self) -> None:
                self.terminate_calls = 0
                self.kill_calls = 0

            @staticmethod
            def poll() -> None:
                return None

            def terminate(self) -> None:
                self.terminate_calls += 1
                raise OSError("terminate unavailable")

            def kill(self) -> None:
                self.kill_calls += 1

        def edge_with_unterminated_process() -> tuple[_EdgeCdp, FakePopen]:
            process = FakePopen()
            edge = _EdgeCdp(Path("fake-msedge.exe"))
            edge._process = process  # type: ignore[assignment]
            return edge, process

        edge, process = edge_with_unterminated_process()
        with self.assertRaisesRegex(AssertionError, r"Edge CDP cleanup failed: process cleanup failed: terminate unavailable"):
            edge.__exit__(None, None, None)

        self.assertEqual(1, process.terminate_calls)
        self.assertEqual(0, process.kill_calls)
        self.assertIsNone(edge._process)

        edge, process = edge_with_unterminated_process()
        primary_error = AssertionError("primary test failure")

        edge.__exit__(AssertionError, primary_error, None)

        self.assertEqual("primary test failure", str(primary_error))
        self.assertEqual(
            ["Edge CDP cleanup failed: process cleanup failed: terminate unavailable"],
            primary_error.__notes__,
        )
        self.assertEqual(1, process.terminate_calls)
        self.assertEqual(0, process.kill_calls)
        self.assertIsNone(edge._process)

    def test_cleanup_failure_after_kill_os_error_fails_or_notes_primary_error(self) -> None:
        class FakePopen:
            def __init__(self) -> None:
                self.terminate_calls = 0
                self.kill_calls = 0
                self.wait_calls: list[float] = []

            @staticmethod
            def poll() -> None:
                return None

            def terminate(self) -> None:
                self.terminate_calls += 1

            def kill(self) -> None:
                self.kill_calls += 1
                raise OSError("kill unavailable")

            def wait(self, timeout: float) -> int:
                self.wait_calls.append(timeout)
                raise subprocess.TimeoutExpired("fake-msedge.exe", timeout)

        def edge_with_unreaped_process() -> tuple[_EdgeCdp, FakePopen]:
            process = FakePopen()
            edge = _EdgeCdp(Path("fake-msedge.exe"))
            edge._process = process  # type: ignore[assignment]
            return edge, process

        edge, process = edge_with_unreaped_process()
        with self.assertRaisesRegex(AssertionError, r"Edge CDP cleanup failed: process cleanup failed: kill unavailable"):
            edge.__exit__(None, None, None)

        self.assertEqual(1, process.terminate_calls)
        self.assertEqual(1, process.kill_calls)
        self.assertEqual([5], process.wait_calls)
        self.assertIsNone(edge._process)

        edge, process = edge_with_unreaped_process()
        primary_error = AssertionError("primary test failure")

        edge.__exit__(AssertionError, primary_error, None)

        self.assertEqual("primary test failure", str(primary_error))
        self.assertEqual(
            ["Edge CDP cleanup failed: process cleanup failed: kill unavailable"],
            primary_error.__notes__,
        )
        self.assertEqual(1, process.terminate_calls)
        self.assertEqual(1, process.kill_calls)
        self.assertEqual([5], process.wait_calls)
        self.assertIsNone(edge._process)

    def test_profile_cleanup_fails_when_lock_persists_until_deadline(self) -> None:
        class LockedProfile:
            def __init__(self) -> None:
                self.cleanup_calls = 0

            def cleanup(self) -> None:
                self.cleanup_calls += 1
                raise PermissionError("profile token=top-secret is locked")

        def edge_with_locked_profile() -> tuple[_EdgeCdp, LockedProfile]:
            profile = LockedProfile()
            edge = _EdgeCdp(Path("fake-msedge.exe"))
            edge._profile = profile
            return edge, profile

        class FakeClock:
            value = 0.0

            def monotonic(self) -> float:
                return self.value

            def sleep(self, delay: float) -> None:
                self.value += delay

        edge, profile = edge_with_locked_profile()
        clock = FakeClock()
        with patch(__name__ + ".time.monotonic", side_effect=clock.monotonic), patch(
            __name__ + ".time.sleep", side_effect=clock.sleep
        ) as sleep:
            with self.assertRaisesRegex(
                AssertionError,
                r"Edge CDP cleanup failed: profile cleanup failed: profile token=\[REDACTED\] is locked",
            ) as raised:
                edge.__exit__(None, None, None)

        self.assertNotIn("top-secret", str(raised.exception))
        self.assertEqual(21, profile.cleanup_calls)
        self.assertEqual(20, sleep.call_count)
        self.assertAlmostEqual(2.0, clock.value)
        self.assertIsNone(edge._profile)

        edge, profile = edge_with_locked_profile()
        primary_error = AssertionError("primary test failure")
        clock = FakeClock()

        with patch(__name__ + ".time.monotonic", side_effect=clock.monotonic), patch(
            __name__ + ".time.sleep", side_effect=clock.sleep
        ) as sleep:
            edge.__exit__(AssertionError, primary_error, None)

        self.assertEqual("primary test failure", str(primary_error))
        self.assertEqual(
            ["Edge CDP cleanup failed: profile cleanup failed: profile token=[REDACTED] is locked"],
            primary_error.__notes__,
        )
        self.assertEqual(21, profile.cleanup_calls)
        self.assertEqual(20, sleep.call_count)
        self.assertAlmostEqual(2.0, clock.value)
        self.assertIsNone(edge._profile)

    def test_profile_cleanup_succeeds_when_lock_releases_before_deadline(self) -> None:
        class DelayedProfile:
            def __init__(self) -> None:
                self.cleanup_calls = 0

            def cleanup(self) -> None:
                self.cleanup_calls += 1
                if self.cleanup_calls < 3:
                    raise PermissionError("profile token=top-secret is locked")

        class FakeClock:
            value = 0.0

            def monotonic(self) -> float:
                return self.value

            def sleep(self, delay: float) -> None:
                self.value += delay

        edge = _EdgeCdp(Path("fake-msedge.exe"))
        profile = DelayedProfile()
        clock = FakeClock()
        edge._profile = profile

        with patch(__name__ + ".time.monotonic", side_effect=clock.monotonic), patch(
            __name__ + ".time.sleep", side_effect=clock.sleep
        ) as sleep:
            diagnostics, cleanup_failed = edge._cleanup()

        self.assertFalse(cleanup_failed)
        self.assertEqual("no process was created", diagnostics)
        self.assertEqual(3, profile.cleanup_calls)
        self.assertEqual(2, sleep.call_count)
        self.assertAlmostEqual(0.2, clock.value)
        self.assertIsNone(edge._profile)

    def test_os_error_during_profile_cleanup_fails_or_notes_primary_error(self) -> None:
        class BrokenProfile:
            def __init__(self) -> None:
                self.cleanup_calls = 0

            def cleanup(self) -> None:
                self.cleanup_calls += 1
                raise OSError("profile cleanup unavailable")

        def edge_with_broken_profile() -> tuple[_EdgeCdp, BrokenProfile]:
            profile = BrokenProfile()
            edge = _EdgeCdp(Path("fake-msedge.exe"))
            edge._profile = profile
            return edge, profile

        edge, profile = edge_with_broken_profile()
        with self.assertRaisesRegex(
            AssertionError,
            r"Edge CDP cleanup failed: profile cleanup failed: profile cleanup unavailable",
        ):
            edge.__exit__(None, None, None)

        self.assertEqual(1, profile.cleanup_calls)
        self.assertIsNone(edge._profile)

        edge, profile = edge_with_broken_profile()
        primary_error = AssertionError("primary test failure")

        edge.__exit__(AssertionError, primary_error, None)

        self.assertEqual("primary test failure", str(primary_error))
        self.assertEqual(
            ["Edge CDP cleanup failed: profile cleanup failed: profile cleanup unavailable"],
            primary_error.__notes__,
        )
        self.assertEqual(1, profile.cleanup_calls)
        self.assertIsNone(edge._profile)


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


class _TestServer:
    def __init__(
        self,
        config: AppConfig,
        *,
        startup_timeout: float = 5,
        server_type: type[Any] | None = None,
        startup_timeout_gate: threading.Event | None = None,
    ):
        self._config = config
        self._startup_timeout = startup_timeout
        self._server_type = server_type
        self._startup_timeout_gate = startup_timeout_gate
        self.port = _free_port()
        self._server: Any | None = None
        self._thread: threading.Thread | None = None
        self._startup_lock = threading.Lock()
        self._startup_cancelled = threading.Event()
        self._serving = threading.Event()

    def __enter__(self) -> "_TestServer":
        ready = threading.Event()
        original_server = self._server_type or api_server.ThreadingHTTPServer
        self._startup_cancelled.clear()
        self._serving.clear()

        owner = self

        class CapturingServer(original_server):
            def __init__(self, *args: Any, **kwargs: Any):
                super().__init__(*args, **kwargs)
                with owner._startup_lock:
                    owner._server = self
                    ready.set()
                    if owner._startup_cancelled.is_set():
                        self.server_close()
                        raise _ServerStartupCancelled()

            def serve_forever(self, *args: Any, **kwargs: Any) -> None:
                with owner._startup_lock:
                    if owner._startup_cancelled.is_set():
                        return
                    owner._serving.set()
                super().serve_forever(*args, **kwargs)

        def run_server() -> None:
            try:
                serve(self._config, "127.0.0.1", self.port)
            except _ServerStartupCancelled:
                return

        with patch.object(api_server, "ThreadingHTTPServer", CapturingServer):
            try:
                self._thread = threading.Thread(target=run_server, daemon=True)
                self._thread.start()
                if self._startup_timeout_gate is not None and not self._startup_timeout_gate.wait(timeout=5):
                    raise AssertionError("test server did not reach the startup timeout gate")
                if not ready.wait(timeout=self._startup_timeout):
                    raise AssertionError("test server did not bind its HTTP listener")
                _wait_for_http(f"http://127.0.0.1:{self.port}/api/health")
            except BaseException:
                self._stop()
                raise
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self._stop()

    def _stop(self) -> None:
        with self._startup_lock:
            self._startup_cancelled.set()
            server = self._server
            serving = self._serving.is_set()

        if server is not None and serving:
            server.shutdown()
        if server is not None:
            server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                raise AssertionError("test server did not stop cleanly")


class _ServerStartupCancelled(Exception):
    pass


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
    _PROCESS_EXIT_TIMEOUT = 5
    _PROFILE_CLEANUP_TIMEOUT = 2
    _PROFILE_CLEANUP_POLL_INTERVAL = 0.1

    def __init__(self, executable: Path) -> None:
        self._executable = executable
        self._process: subprocess.Popen[bytes] | None = None
        self._client: _CdpClient | None = None
        self._session_id: str | None = None
        self._profile: Any | None = None
        self._stdout: Any | None = None
        self._stderr: Any | None = None
        self._job: _WindowsJob | None = None

    def __enter__(self) -> "_EdgeCdp":
        self._debug_port = _free_port()
        self._profile = tempfile.TemporaryDirectory()
        self._stdout = tempfile.TemporaryFile()
        self._stderr = tempfile.TemporaryFile()
        try:
            if sys.platform == "win32":
                self._job = _WindowsJob()
            self._process = subprocess.Popen(
                [
                    str(self._executable),
                    "--headless=new",
                    f"--remote-debugging-port={self._debug_port}",
                    f"--user-data-dir={self._profile.name}",
                    "--password-store=basic",
                    "--disable-background-networking",
                    "--disable-sync",
                    "--no-service-autorun",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "about:blank",
                ],
                stdout=self._stdout,
                stderr=self._stderr,
            )
            if self._job is not None:
                self._job.assign(self._process)
            version = _wait_for_http(f"http://127.0.0.1:{self._debug_port}/json/version")
            self._client = _CdpClient(str(version["webSocketDebuggerUrl"]))
            target_id = str(self._client.command("Target.createTarget", {"url": "about:blank"})["targetId"])
            self._session_id = str(
                self._client.command("Target.attachToTarget", {"targetId": target_id, "flatten": True})["sessionId"]
            )
            self._client.command("Runtime.enable", session_id=self._session_id)
        except BaseException as error:
            diagnostics, _ = self._cleanup()
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise AssertionError(f"Edge CDP startup failed: {error}; {diagnostics}") from error
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        diagnostics, cleanup_failed = self._cleanup()
        if not cleanup_failed:
            return
        message = f"Edge CDP cleanup failed: {diagnostics}"
        if isinstance(_value, BaseException):
            _value.add_note(message)
            return
        raise AssertionError(message)

    def _cleanup(self) -> tuple[str, bool]:
        diagnostics: list[str] = []
        cleanup_failed = False
        if self._client is not None:
            try:
                self._client.close()
            except OSError as error:
                diagnostics.append(f"CDP close failed: {error}")
            self._client = None
        if self._job is not None:
            try:
                self._job.close()
            except OSError as error:
                diagnostics.append(f"Windows Edge job cleanup failed: {self._redact_browser_output(str(error))}")
                cleanup_failed = True
            self._job = None
        if self._process is not None:
            process = self._process
            try:
                exit_code = process.poll()
                if exit_code is None:
                    if sys.platform == "win32":
                        try:
                            exit_code = process.wait(timeout=self._PROCESS_EXIT_TIMEOUT)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            try:
                                exit_code = process.wait(timeout=self._PROCESS_EXIT_TIMEOUT)
                            except subprocess.TimeoutExpired:
                                diagnostics.append("process did not exit after Windows job close")
                                cleanup_failed = True
                                exit_code = process.poll()
                    else:
                        process.terminate()
                        try:
                            exit_code = process.wait(timeout=self._PROCESS_EXIT_TIMEOUT)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            try:
                                exit_code = process.wait(timeout=self._PROCESS_EXIT_TIMEOUT)
                            except subprocess.TimeoutExpired:
                                diagnostics.append("process did not exit after kill")
                                cleanup_failed = True
                                exit_code = process.poll()
                diagnostics.append(f"process exit code {exit_code}")
            except OSError as error:
                diagnostics.append(f"process cleanup failed: {self._redact_browser_output(str(error))}")
                cleanup_failed = True
            self._process = None
        self._close_browser_log(self._stdout, "stdout", diagnostics)
        self._stdout = None
        self._close_browser_log(self._stderr, "stderr", diagnostics)
        self._stderr = None
        if self._profile is not None:
            cleanup_failed = self._cleanup_profile(self._profile, diagnostics) or cleanup_failed
            self._profile = None
        return "; ".join(diagnostics) or "no process was created", cleanup_failed

    def _cleanup_profile(self, profile: Any, diagnostics: list[str]) -> bool:
        deadline = time.monotonic() + self._PROFILE_CLEANUP_TIMEOUT
        while True:
            try:
                profile.cleanup()
                return False
            except PermissionError as error:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    diagnostics.append(f"profile cleanup failed: {self._redact_browser_output(str(error))}")
                    return True
                time.sleep(min(self._PROFILE_CLEANUP_POLL_INTERVAL, remaining))
            except OSError as error:
                diagnostics.append(f"profile cleanup failed: {self._redact_browser_output(str(error))}")
                return True

    @staticmethod
    def _close_browser_log(stream: Any | None, name: str, diagnostics: list[str]) -> None:
        if stream is None:
            return
        try:
            stream.seek(0)
            output = stream.read()
            if output:
                diagnostics.append(f"{name}: {_EdgeCdp._redact_browser_output(output)}")
        except OSError as error:
            diagnostics.append(f"{name} capture failed: {error}")
        finally:
            try:
                stream.close()
            except OSError as error:
                diagnostics.append(f"{name} close failed: {error}")

    @staticmethod
    def _redact_browser_output(output: bytes | str) -> str:
        text = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else output
        text = re.sub(
            r"(?i)\b(authorization\s*[:=]\s*(?:bearer\s+)?|(?:cookie|password|token|secret)\s*[:=]\s*)[^\s,;]+",
            r"\1[REDACTED]",
            text.strip(),
        )
        return text[:2000] + ("... [truncated]" if len(text) > 2000 else "")

    def navigate(self, url: str) -> None:
        self._command("Page.navigate", {"url": url})

    def set_viewport(self, width: int, height: int = 900) -> None:
        """Use CDP device metrics so the test matrix needs no browser dependency."""
        self._command(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
        )
        self._command("Emulation.setVisibleSize", {"width": width, "height": height})

    def press_key(self, key: str, modifiers: int = 0) -> None:
        key_code = {"Enter": 13, "Tab": 9}.get(key, 0)
        key_text = "\r" if key == "Enter" else ""
        self._command(
            "Input.dispatchKeyEvent",
            {
                "type": "keyDown",
                "key": key,
                "code": key,
                "windowsVirtualKeyCode": key_code,
                "nativeVirtualKeyCode": key_code,
                "text": key_text,
                "unmodifiedText": key_text,
                "modifiers": modifiers,
            },
        )
        self._command(
            "Input.dispatchKeyEvent",
            {"type": "keyUp", "key": key, "code": key, "windowsVirtualKeyCode": key_code, "modifiers": modifiers},
        )

    def move_pointer_to(self, x: float, y: float) -> None:
        self._command("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})

    def pointer_down(self, x: float, y: float) -> None:
        self._command("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})

    def pointer_up(self, x: float, y: float) -> None:
        self._command("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})

    def evaluate(self, expression: str) -> Any:
        response = self._command(
            "Runtime.evaluate", {"expression": expression, "awaitPromise": True, "returnByValue": True}
        )
        if "exceptionDetails" in response:
            raise AssertionError(f"browser JavaScript failed: {response['exceptionDetails']}")
        return response["result"].get("value")

    def wait_for(self, expression: str, description: str, timeout: float = 10, diagnostics: str | None = None) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.evaluate(f"Boolean({expression})"):
                return
            time.sleep(0.02)
        detail = self.evaluate(diagnostics) if diagnostics else None
        suffix = f"; diagnostics: {detail!r}" if diagnostics else ""
        raise AssertionError(f"browser condition did not become true: {description}{suffix}")

    def _command(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        assert self._client is not None
        assert self._session_id is not None
        return self._client.command(method, params, session_id=self._session_id)


class _WindowsJobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("per_process_user_time_limit", ctypes.c_longlong),
        ("per_job_user_time_limit", ctypes.c_longlong),
        ("limit_flags", ctypes.c_uint32),
        ("minimum_working_set_size", ctypes.c_size_t),
        ("maximum_working_set_size", ctypes.c_size_t),
        ("active_process_limit", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t),
        ("priority_class", ctypes.c_uint32),
        ("scheduling_class", ctypes.c_uint32),
    ]


class _WindowsJobIoCounters(ctypes.Structure):
    _fields_ = [
        ("read_operation_count", ctypes.c_uint64),
        ("write_operation_count", ctypes.c_uint64),
        ("other_operation_count", ctypes.c_uint64),
        ("read_transfer_count", ctypes.c_uint64),
        ("write_transfer_count", ctypes.c_uint64),
        ("other_transfer_count", ctypes.c_uint64),
    ]


class _WindowsJobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("basic_limit_information", _WindowsJobBasicLimitInformation),
        ("io_info", _WindowsJobIoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory_used", ctypes.c_size_t),
        ("peak_job_memory_used", ctypes.c_size_t),
    ]


class _WindowsJob:
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    def __init__(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.AssignProcessToJobObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int
        self._kernel32 = kernel32
        self._handle = kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            self._raise_last_error("CreateJobObjectW")
        limits = _WindowsJobExtendedLimitInformation()
        limits.basic_limit_information.limit_flags = self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            self._handle,
            self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            try:
                self.close()
            finally:
                self._raise_last_error("SetInformationJobObject")

    def assign(self, process: subprocess.Popen[bytes]) -> None:
        if self._handle is None:
            raise OSError("Windows Edge job is already closed")
        if not self._kernel32.AssignProcessToJobObject(self._handle, ctypes.c_void_p(process._handle)):  # type: ignore[attr-defined]
            self._raise_last_error("AssignProcessToJobObject")

    def close(self) -> None:
        if self._handle is None:
            return
        handle, self._handle = self._handle, None
        if not self._kernel32.CloseHandle(handle):
            self._raise_last_error("CloseHandle")

    @staticmethod
    def _raise_last_error(operation: str) -> None:
        raise OSError(ctypes.get_last_error(), f"{operation} failed")


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
