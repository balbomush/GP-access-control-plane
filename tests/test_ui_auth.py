from __future__ import annotations

import http.client
import json
import os
import errno
import socket
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
from tests.browser.runner import PlaywrightPage


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

class PlaywrightBearerAuthBrowserTests(unittest.TestCase):

    def test_login_auth_fetch_blob_download_and_password_change_logs_out(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with _TestServer(config) as server, PlaywrightPage() as page:
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
                page.fill("#login-username", "admin")
                page.fill("#login-password", "admin")
                page.click('#login-form button[type="submit"]')
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
                page.click("#tab-settings")
                page.wait_for("document.getElementById('tab-panel-settings').classList.contains('active')", "visible settings tab")
                page.click('[data-action="refresh-backups"]')
                snapshot = json.dumps(snapshot_id)
                page.wait_for(
                    f"Array.from(document.querySelectorAll('[data-backup-download]')).some((button) => button.dataset.backupDownload === {snapshot})",
                    "backup download action",
                )
                page.click(f'[data-backup-download="{snapshot_id}"]')
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
                page.fill("#settings-current-password", "wrongpass")
                page.fill("#settings-new-password", "another8")
                page.click('#change-password-form [type="submit"]')
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
                page.fill("#settings-current-password", "admin")
                page.fill("#settings-new-password", "newpass8")
                page.click('#change-password-form [type="submit"]')
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
                      && document.getElementById('app-shell') === null
                      && document.getElementById('change-password-form') === null
                      && window.__bearerAuthE2E.sse.length === 1
                      && window.__bearerAuthE2E.sse[0].aborted
                      && window.__bearerAuthE2E.clearedFallbackTimers.includes(window.__bearerAuthE2E.fallbackTimer)
                    """,
                    "successful password change clears the session and stops realtime activity",
                    diagnostics="JSON.parse(JSON.stringify({ auth: window.__bearerAuthE2E, loginHidden: document.getElementById('login-screen')?.hidden, appShell: document.getElementById('app-shell')?.hidden, form: Boolean(document.getElementById('change-password-form')), token: localStorage.getItem('gp-control-plane-auth-token') }))",
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
            with _TestServer(config) as server, PlaywrightPage() as page:
                page.navigate(f"http://127.0.0.1:{server.port}/")
                page.wait_for(
                    "document.readyState === 'complete' && document.getElementById('login-form')",
                    "initialized login form",
                )
                page.fill("#login-username", "admin")
                page.fill("#login-password", "admin")
                page.click('#login-form button[type="submit"]')
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

                page.click("#tab-settings")
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
                page.click("details.domain-group:last-of-type > summary")
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
            with _TestServer(config) as server, PlaywrightPage() as page:
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
            with _TestServer(config) as server, PlaywrightPage() as page:
                page.navigate(f"http://127.0.0.1:{server.port}/")
                page.wait_for(
                    "document.readyState === 'complete' && document.getElementById('login-form')",
                    "initialized login form",
                )
                page.fill("#login-username", "admin")
                page.fill("#login-password", "admin")
                page.click('#login-form button[type="submit"]')
                page.wait_for(
                    "document.getElementById('login-screen').hidden && !document.getElementById('app-shell').hidden",
                    "authenticated application shell",
                )
                page.click("#tab-candidates")
                page.click("#candidate-view-common")
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

                page.click("#tab-finder")
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
    def test_primary_body_failure_is_preserved_when_server_cleanup_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            server = _TestServer(AppConfig(output=OutputConfig(state_dir=Path(raw) / "state")))
            primary = AssertionError("primary browser assertion")
            with patch.object(server, "_stop", side_effect=AssertionError("forced server cleanup failure")):
                self.assertFalse(server.__exit__(AssertionError, primary, None))
            self.assertIn("primary browser assertion", str(primary))
            self.assertIn("forced server cleanup failure", "\n".join(getattr(primary, "__notes__", ())))

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


class ResponsiveLayoutBrowserTests(unittest.TestCase):
    """WEBL browser matrix: read-only rendering and navigation at each target width."""

    VIEWPORTS = (320, 375, 768, 960, 1024, 1440)

    def assert_document_fits_viewport(self, page: PlaywrightPage, width: int, state: str) -> None:
        metrics = page.evaluate(
            "({ scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth })"
        )
        self.assertLessEqual(metrics["scrollWidth"], metrics["clientWidth"], f"{width}px {state}: {metrics}")

    def assert_click_targetable(self, page: PlaywrightPage, selector: str, width: int) -> None:
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

    def assert_horizontal_scroll_is_local(self, page: PlaywrightPage, width: int, state: str) -> None:
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

    def assert_finder_stack_spans_layout(self, page: PlaywrightPage, width: int) -> None:
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

    def assert_raw_log_is_visible_and_locally_scrollable(self, page: PlaywrightPage, width: int) -> None:
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
    def render_safe_dynamic_fixtures(page: PlaywrightPage) -> None:
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
    def login(page: PlaywrightPage) -> None:
        page.wait_for(
            "document.readyState === 'complete' && document.getElementById('login-form') && !document.getElementById('login-screen').hidden",
            "unauthenticated login form",
        )
        page.fill("#login-username", "admin")
        page.fill("#login-password", "admin")
        page.click('#login-form button[type="submit"]')
        page.wait_for(
            "localStorage.getItem('gp-control-plane-auth-token') && !document.getElementById('app-shell').hidden",
            "authenticated shell",
        )

    def test_settings_omits_vault_controls_and_never_fetches_vaults_while_backups_remain_usable(self) -> None:
        """WEBL-017/T09: Settings owns everyday backups, never clean-install vault operations."""
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with _TestServer(config) as server, PlaywrightPage() as page:
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
                    })()
                    """
                )
                page.click("#tab-settings")
                page.wait_for(
                    "document.getElementById('tab-settings').getAttribute('aria-selected') === 'true' && document.getElementById('tab-panel-settings').classList.contains('active')",
                    "Settings tab",
                )
                page.click('#tab-panel-settings [data-action="refresh-backups"]')
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
            with _TestServer(config) as server, PlaywrightPage() as page:
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

                page.click("#tab-settings")
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
            with _TestServer(config) as server, PlaywrightPage() as page:
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
                          primary.focus({ preventScroll: true, focusVisible: true });
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
            with _TestServer(config) as server, PlaywrightPage() as page:
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

        def tab_round_trip(page: PlaywrightPage, selector: str, width: int) -> None:
            if selector == '[data-action="stop-current"]':
                page.evaluate("document.querySelector('[data-action=\"stop-current\"]').disabled = false;")
            page.evaluate(f"document.querySelector({json.dumps(selector)}).focus({{ preventScroll: true, focusVisible: true }});")
            self.assertTrue(page.evaluate(f"document.activeElement === document.querySelector({json.dumps(selector)})"), f"{width}px could not focus {selector}")
            page.press_key("Tab", modifiers=8)  # CDP Shift modifier.
            self.assertFalse(page.evaluate(f"document.activeElement === document.querySelector({json.dumps(selector)})"), f"{width}px Shift+Tab did not traverse from {selector}")
            page.evaluate(f"document.querySelector({json.dumps(selector)}).focus({{ preventScroll: true, focusVisible: true }});")
            page.press_key("Tab")
            self.assertFalse(page.evaluate(f"document.activeElement === document.querySelector({json.dumps(selector)})"), f"{width}px Tab did not traverse from {selector}")

        def computed_states(page: PlaywrightPage, selector: str) -> dict[str, dict[str, object]]:
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
            with _TestServer(config) as server, PlaywrightPage() as page:
                page.navigate(f"http://127.0.0.1:{server.port}/")
                self.login(page)
                page.evaluate("stopRealtimeEvents(); stopRealtimeFallback();")
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
                    page.click("#tab-finder")
                    page.evaluate("document.querySelector('details.preset-panel').open = true; document.querySelector('[data-action=\"stop-current\"]').disabled = false;")
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
            with _TestServer(config) as server, PlaywrightPage() as page:
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

                page.click("#tab-settings")
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
                        self.assertLessEqual(action["button"]["width"], min(520, action["row"]["width"]) + 2, msg=f"{width}px {action}")
                        self.assertAlmostEqual(
                            action["button"]["left"],
                            action["row"]["left"],
                            delta=2,
                            msg=f"{width}px {action}",
                        )
                    self.assert_document_fits_viewport(page, width, "single settings action")

    def test_history_and_explicit_action_patterns_keep_geometry_and_targets(self) -> None:
        """WEBL-UX-001/002: named responsive patterns must not depend on child order or count."""
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with _TestServer(config) as server, PlaywrightPage() as page:
                page.navigate(f"http://127.0.0.1:{server.port}/")
                self.login(page)
                page.wait_for(
                    "!document.querySelector('[data-action=\"run-selected-discovery\"]').disabled",
                    "initial system status",
                    timeout=15,
                )
                self.render_safe_dynamic_fixtures(page)

                for width in self.VIEWPORTS:
                    page.set_viewport(width)
                    self.render_safe_dynamic_fixtures(page)
                    page.click("#tab-history")
                    page.wait_for("document.getElementById('tab-panel-history').classList.contains('active')", "History tab")
                    page.evaluate(
                        """
                        state.finderRuns = [{
                          kind: 'standard-discovery', run_id: 'layout-history-fixture', timestamp: '2026-08-31T12:00:00Z',
                          status: 'failed', domains: ['layout.example.test'],
                          progress: { phase: 'strategy_summary', completed: 1, total: 2 },
                          settings: { enable_http: true, enable_tls12: true, domain_count: 1, scan_level: 'standard' },
                          diagnostics: { message: 'layout fixture' }
                        }];
                        state.finderRunTotal = 1;
                        renderRuns();
                        """
                    )
                    history = page.evaluate(
                        """
                        (() => {
                          const card = document.querySelector('.run-card');
                          const status = card?.querySelector('.run-field-status .badge');
                          const phase = card?.querySelector('.run-field-phase .run-field-value');
                          if (!card || !status || !phase) return { missing: { card: Boolean(card), status: Boolean(status), phase: Boolean(phase) } };
                          const rect = (node) => { const r = node.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom }; };
                          return { status: rect(status), phase: rect(phase) };
                        })()
                        """
                    )
                    self.assertIsNotNone(history, f"{width}px history fields missing")
                    assert history is not None
                    self.assertNotIn("missing", history, f"{width}px history fields missing: {history}")
                    vertical_overlap = history["status"]["top"] < history["phase"]["bottom"] and history["phase"]["top"] < history["status"]["bottom"]
                    horizontal_overlap = history["status"]["left"] < history["phase"]["right"] and history["phase"]["left"] < history["status"]["right"]
                    self.assertFalse(vertical_overlap and horizontal_overlap, f"{width}px status overlaps phase: {history}")
                    self.assert_document_fits_viewport(page, width, "history status and phase")

                    page.click("#tab-finder")
                    page.wait_for("document.getElementById('tab-panel-finder').classList.contains('active')", "Finder tab")
                    page.evaluate("state.acknowledgedRun = null; state.status = { version: '0.4.1', current_run: null }; renderMetrics();")
                    finder = page.evaluate(
                        """
                        (() => {
                          const start = document.querySelector('.run-action-start');
                          const stop = document.querySelector('.run-action-stop');
                          const note = document.querySelector('.run-actions .action-consequence');
                          if (!start || !stop || !note) return null;
                          const rect = (node) => { const r = node.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width }; };
                          return { start: rect(start), stop: rect(stop), note: rect(note), stopDisabled: stop.disabled };
                        })()
                        """
                    )
                    self.assertIsNotNone(finder, f"{width}px finder actions missing")
                    assert finder is not None
                    self.assertTrue(finder["stopDisabled"], f"{width}px inactive stop must retain its slot")
                    self.assertAlmostEqual(finder["start"]["top"], finder["stop"]["top"], delta=2, msg=f"{width}px {finder}")
                    self.assertGreaterEqual(finder["note"]["top"], max(finder["start"]["bottom"], finder["stop"]["bottom"]) - 1, f"{width}px {finder}")
                    self.assert_click_targetable(page, ".run-action-start", width)
                    self.assert_click_targetable(page, ".run-action-stop", width)
                    self.assert_document_fits_viewport(page, width, "finder stable action pair")

                    page.click("#tab-lists")
                    page.wait_for("document.getElementById('tab-panel-lists').classList.contains('active')", "Lists tab")
                    geometry = page.evaluate(
                        """
                        (() => {
                          document.querySelector('.preset-create-panel').open = true;
                          const selectors = {
                            search: '#v2fly-category-search', reload: '[data-action="v2fly-load-categories"]', update: '[data-action="v2fly-update-local-storage"]',
                            preview: '[data-action="v2fly-preview"]', import: '[data-action="v2fly-import"]',
                            save: '[data-action="preset-editor-save"]', download: '[data-action="preset-editor-export"]', remove: '[data-action="preset-editor-delete"]',
                            primary: '[data-action="preset-new-save"]'
                          };
                          const rect = (node) => { const r = node?.getBoundingClientRect(); return r ? { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width } : null; };
                          return Object.fromEntries(Object.entries(selectors).map(([key, selector]) => [key, rect(document.querySelector(selector))]));
                        })()
                        """
                    )
                    self.assertTrue(all(geometry.values()), f"{width}px list controls missing: {geometry}")
                    if width < 960:
                        self.assertGreaterEqual(geometry["reload"]["top"], geometry["search"]["bottom"] - 1, f"{width}px v2fly tablet field must own its row: {geometry}")
                        if width >= 600:
                            self.assertAlmostEqual(geometry["reload"]["top"], geometry["update"]["top"], delta=2, msg=f"{width}px {geometry}")
                    else:
                        self.assertAlmostEqual(geometry["search"]["top"], geometry["reload"]["top"], delta=2, msg=f"{width}px {geometry}")
                        self.assertAlmostEqual(geometry["reload"]["top"], geometry["update"]["top"], delta=2, msg=f"{width}px {geometry}")
                        self.assertAlmostEqual(geometry["preview"]["top"], geometry["import"]["top"], delta=2, msg=f"{width}px {geometry}")
                    self.assertGreaterEqual(geometry["remove"]["top"], max(geometry["save"]["bottom"], geometry["download"]["bottom"]) - 1, f"{width}px destructive action needs its own zone: {geometry}")
                    if width < 960:
                        self.assertGreater(geometry["primary"]["width"], 0, f"{width}px primary action must remain actionable: {geometry}")
                    else:
                        self.assertLessEqual(geometry["primary"]["width"], 522, f"{width}px desktop primary action must stay readable: {geometry}")
                    for selector in ('[data-action="preset-editor-save"]', '[data-action="preset-editor-export"]', '[data-action="v2fly-preview"]', '[data-action="v2fly-import"]'):
                        self.assert_click_targetable(page, selector, width)
                    self.assert_document_fits_viewport(page, width, "lists explicit action patterns")

    def test_login_shell_tabs_candidates_and_dynamic_layouts_fit_all_target_viewports(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            with _TestServer(config) as server, PlaywrightPage() as page:
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

                page.fill("#login-username", "admin")
                page.fill("#login-password", "admin")
                page.click('#login-form button[type="submit"]')
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
                        page.click(f"#tab-{tab}")
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

                    page.click("#candidate-view-common")
                    page.wait_for(
                        "document.getElementById('candidate-view-common').getAttribute('aria-selected') === 'true'",
                        "common candidate subtab",
                    )
                    self.assert_click_targetable(page, "#tab-panel-candidates button[data-action='build-candidate-result']", width)
                    for mode in ("coverage", "minimal", "balance"):
                        page.click(f"#candidate-result-mode-{mode}")
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
                        page.click(f"#tab-{tab}")
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
                    page.click("#tab-finder")
                    page.wait_for("document.getElementById('tab-finder').getAttribute('aria-selected') === 'true'", "finder tab")
                    self.assert_finder_stack_spans_layout(page, width)
                    self.assert_document_fits_viewport(page, width, "finder full-span layout")


class BrowserTestDiscoveryTests(unittest.TestCase):
    def test_browser_cases_are_discovered_once_per_concrete_suite(self) -> None:
        """WEBL-F09: responsive cases must share setup, not inherit another suite's tests."""
        loader = unittest.TestLoader()
        auth_names = set(loader.getTestCaseNames(PlaywrightBearerAuthBrowserTests))
        responsive_names = set(loader.getTestCaseNames(ResponsiveLayoutBrowserTests))

        self.assertFalse(auth_names & responsive_names)
        self.assertEqual(
            auth_names,
            {
                "test_login_auth_fetch_blob_download_and_password_change_logs_out",
                "test_web_layout_matrix_keeps_metrics_summary_disclosure_and_password_fields_usable",
                "test_login_outer_inset_uses_responsive_tokens_without_overflow",
                "test_mobile_candidate_header_and_protocol_controls_follow_responsive_grid",
            },
        )
        self.assertEqual(
            responsive_names,
            {
                "test_advanced_disclosure_uses_one_chevron_and_single_actions_are_balanced",
                "test_domain_header_and_debug_help_keep_their_user_visible_geometry",
                "test_login_shell_tabs_candidates_and_dynamic_layouts_fit_all_target_viewports",
                "test_settings_omits_vault_controls_and_never_fetches_vaults_while_backups_remain_usable",
                "test_initial_missing_system_status_stays_neutral_and_blocks_actions",
                "test_history_and_explicit_action_patterns_keep_geometry_and_targets",
                "test_web_color_roles_status_markup_focus_and_backup_fixture",
                "test_web_color_keyboard_contrast_and_disclosure_matrix",
            },
        )
        self.assertEqual(
            loader.loadTestsFromTestCase(ResponsiveLayoutBrowserTests).countTestCases(), len(responsive_names)
        )


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
        self._request_cancelled = threading.Event()
        self._serving = threading.Event()
        self._sleep_patch: Any | None = None

    def _interruptible_server_sleep(self, seconds: float) -> None:
        if self._request_cancelled.wait(timeout=seconds):
            raise _ServerRequestCancelled()

    def __enter__(self) -> "_TestServer":
        ready = threading.Event()
        original_server = self._server_type or api_server.ThreadingHTTPServer
        self._startup_cancelled.clear()
        self._request_cancelled.clear()
        self._serving.clear()

        owner = self

        class CapturingServer(original_server):
            # Browser bootstrap opens long-lived SSE/HTTP requests. Track their
            # sockets so teardown closes the clients before joining handlers and
            # TemporaryDirectory removes the state database on Windows.
            daemon_threads = False
            block_on_close = False

            def __init__(self, *args: Any, **kwargs: Any):
                self._active_requests: set[socket.socket] = set()
                self._active_requests_lock = threading.Lock()
                self._request_workers: set[threading.Thread] = set()
                self._request_workers_lock = threading.Lock()
                self._worker_requests: dict[threading.Thread, socket.socket] = {}
                self._request_details: dict[socket.socket, str] = {}
                super().__init__(*args, **kwargs)
                with owner._startup_lock:
                    owner._server = self
                    ready.set()
                    if owner._startup_cancelled.is_set():
                        self.server_close()
                        raise _ServerStartupCancelled()

            def process_request_thread(self, request: socket.socket, client_address: Any) -> None:
                worker = threading.current_thread()
                with self._active_requests_lock:
                    self._active_requests.add(request)
                with self._request_workers_lock:
                    self._request_workers.add(worker)
                    self._worker_requests[worker] = request
                try:
                    super().process_request_thread(request, client_address)
                finally:
                    with self._active_requests_lock:
                        self._active_requests.discard(request)
                        self._request_details.pop(request, None)
                    with self._request_workers_lock:
                        self._request_workers.discard(worker)
                        self._worker_requests.pop(worker, None)

            def finish_request(self, request: socket.socket, client_address: Any) -> None:
                handler_class = self.RequestHandlerClass
                server = self

                class TrackingHandler(handler_class):
                    def do_GET(self) -> None:  # noqa: N802
                        with server._active_requests_lock:
                            server._request_details[self.request] = f"{client_address[0]}:{client_address[1]} {self.path}"
                        try:
                            super().do_GET()
                        finally:
                            if self.path == "/api/web/events/stream":
                                self.close_connection = True

                TrackingHandler(request, client_address, self)

            def close_active_requests(self) -> list[str]:
                errors: list[str] = []
                with self._active_requests_lock:
                    requests = tuple(self._active_requests)
                for request in requests:
                    try:
                        request.shutdown(socket.SHUT_RDWR)
                    except OSError as error:
                        if not _expected_connection_close(error):
                            errors.append(f"request shutdown: {error}")
                    try:
                        request.close()
                    except OSError as error:
                        if not _expected_connection_close(error):
                            errors.append(f"request close: {error}")
                return errors

            def wake_request_workers(self) -> None:
                owner._request_cancelled.set()

            def join_request_workers(self, timeout: float = 5) -> None:
                with self._request_workers_lock:
                    workers = tuple(self._request_workers)
                for worker in workers:
                    worker.join(timeout=timeout)
                with self._request_workers_lock:
                    active = [
                        f"{worker.name} ({self._request_details.get(self._worker_requests.get(worker), 'path unavailable')})"
                        for worker in workers
                        if worker.is_alive()
                    ]
                if active:
                    raise AssertionError(f"test server request workers did not stop cleanly: {active}")

            def handle_error(self, request: socket.socket, client_address: Any) -> None:
                # Closing a keep-alive socket deliberately unblocks its handler
                # during teardown; it is not a product-server failure.
                active_error = sys.exc_info()[1]
                if owner._startup_cancelled.is_set() and isinstance(active_error, OSError) and _expected_connection_close(active_error):
                    return
                super().handle_error(request, client_address)

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
                self._sleep_patch = patch.object(api_server.time, "sleep", self._interruptible_server_sleep)
                self._sleep_patch.start()
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

    def __exit__(self, _type: object, value: BaseException | None, _traceback: object) -> bool:
        try:
            self._stop()
        except BaseException as error:
            if value is not None:
                _attach_cleanup_note(value, str(error))
                return False
            raise
        return False

    def _stop(self) -> None:
        errors: list[str] = []
        with self._startup_lock:
            self._startup_cancelled.set()
            server = self._server
            serving = self._serving.is_set()

        if server is not None and serving:
            try:
                server.shutdown()
            except BaseException as error:
                errors.append(f"server shutdown: {error}")
        if server is not None:
            try:
                errors.extend(server.close_active_requests())
            except BaseException as error:
                errors.append(f"request close: {error}")
            try:
                server.wake_request_workers()
            except BaseException as error:
                errors.append(f"request worker wakeup: {error}")
            try:
                server.server_close()
            except BaseException as error:
                errors.append(f"server close: {error}")
        try:
            if server is not None:
                try:
                    server.join_request_workers()
                except BaseException as error:
                    errors.append(f"request worker join: {error}")
            if self._thread is not None:
                try:
                    self._thread.join(timeout=5)
                except BaseException as error:
                    errors.append(f"server thread join: {error}")
                if self._thread.is_alive():
                    errors.append("test server did not stop cleanly")
        finally:
            if self._sleep_patch is not None:
                try:
                    self._sleep_patch.stop()
                except BaseException as error:
                    errors.append(f"sleep patch stop: {error}")
                finally:
                    self._sleep_patch = None
        if errors:
            raise AssertionError("BGT-001 test server cleanup failed: " + "; ".join(errors))


class _ServerStartupCancelled(Exception):
    pass


class _ServerRequestCancelled(Exception):
    pass


def _expected_connection_close(error: OSError) -> bool:
    return error.errno in {errno.EPIPE, errno.ECONNABORTED, errno.ECONNRESET, errno.ENOTCONN} or getattr(error, "winerror", None) in {10053, 10054, 10058}


def _attach_cleanup_note(error: BaseException, cleanup: str) -> None:
    if hasattr(error, "add_note"):
        error.add_note("BGT-001 cleanup: " + cleanup)


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


if __name__ == '__main__':
    unittest.main()
