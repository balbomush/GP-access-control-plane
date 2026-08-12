from __future__ import annotations

import multiprocessing
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane import auth
from gp_control_plane.auth import (
    AUTH_SETTINGS_KEY,
    PASSWORD_MIN_LENGTH,
    TOKEN_TTL_SECONDS,
    AuthenticationError,
    PasswordValidationError,
    change_password,
    login,
    require_bearer_token,
)
from gp_control_plane.storage import AUTH_BUSY_TIMEOUT_MS, StorageUnavailableError, db_path, read_app_setting


_PROCESS_TIMEOUT_SECONDS = 15


def _hold_validated_token_in_auth_transaction(
    state_dir_raw: str,
    token: str,
    entered: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
    results: multiprocessing.queues.Queue[tuple[str, str]],
) -> None:
    """Validate a bearer token and keep its real SQLite auth transaction open."""
    state_dir = Path(state_dir_raw)
    try:
        with auth.auth_transaction(state_dir) as conn:
            auth._validate_bearer_token(auth._settings(conn), f"Bearer {token}")
            entered.set()
            if not release.wait(timeout=_PROCESS_TIMEOUT_SECONDS):
                raise TimeoutError("parent did not release the auth transaction")
        results.put(("validated", ""))
    except BaseException as error:  # noqa: BLE001 - report child failures to the parent test
        results.put(("error", repr(error)))


def _change_password_in_process(
    state_dir_raw: str,
    attempted: multiprocessing.synchronize.Event,
    finished: multiprocessing.synchronize.Event,
    results: multiprocessing.queues.Queue[tuple[str, object]],
) -> None:
    try:
        attempted.set()
        token = change_password(
            Path(state_dir_raw),
            {"current_password": "admin", "new_password": "newpass8"},
        )
        results.put(("token", token))
    except BaseException as error:  # noqa: BLE001 - report child failures to the parent test
        results.put(("error", repr(error)))
    finally:
        finished.set()


def _require_token_in_process(
    state_dir_raw: str, token: str, results: multiprocessing.queues.Queue[tuple[str, str]]
) -> None:
    try:
        require_bearer_token(Path(state_dir_raw), f"Bearer {token}")
    except AuthenticationError:
        results.put((token, "rejected"))
    except BaseException as error:  # noqa: BLE001 - report child failures to the parent test
        results.put((token, f"error: {error!r}"))
    else:
        results.put((token, "accepted"))


def _require_bearer_token_from_fresh_process(
    state_dir_raw: str, results: multiprocessing.queues.Queue[tuple[str, object]]
) -> None:
    """Call auth against an uninitialized state directory in a new interpreter."""
    started = time.monotonic()
    try:
        require_bearer_token(Path(state_dir_raw), "Bearer malformed-token")
    except StorageUnavailableError:
        results.put(("storage_unavailable", time.monotonic() - started))
    except BaseException as error:  # noqa: BLE001 - report child failures to the parent test
        results.put(("error", (repr(error), time.monotonic() - started)))
    else:
        results.put(("accepted", time.monotonic() - started))


def _hold_sqlite_immediate_transaction(
    state_dir_raw: str,
    entered: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
    results: multiprocessing.queues.Queue[tuple[str, str]],
) -> None:
    """Hold the same database write lock from a separate interpreter process."""
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(db_path(Path(state_dir_raw)), timeout=0)
        conn.execute("BEGIN IMMEDIATE")
        entered.set()
        if not release.wait(timeout=_PROCESS_TIMEOUT_SECONDS):
            raise TimeoutError("parent did not release the SQLite write lock")
        conn.rollback()
        results.put(("released", ""))
    except BaseException as error:  # noqa: BLE001 - report child failures to the parent test
        results.put(("error", repr(error)))
    finally:
        if conn is not None:
            conn.close()


def _join_process(test: unittest.TestCase, process: multiprocessing.Process) -> None:
    process.join(timeout=_PROCESS_TIMEOUT_SECONDS)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        test.fail(f"child process {process.name} did not stop")
    test.assertEqual(process.exitcode, 0, f"child process {process.name} exited unexpectedly")


class AuthTests(unittest.TestCase):
    def test_default_admin_login_persists_pbkdf2_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            with patch("gp_control_plane.auth.time.time", return_value=1_700_000_000):
                token = login(state_dir, {"username": "admin", "password": "admin"})

            settings = read_app_setting(state_dir, AUTH_SETTINGS_KEY)

            self.assertEqual(token["token_type"], "Bearer")
            self.assertEqual(token["expires_in"], TOKEN_TTL_SECONDS)
            self.assertIn(".", token["access_token"])
            self.assertEqual(settings["username"], "admin")
            self.assertNotEqual(settings["password_hash"], "admin")
            self.assertEqual(len(settings["password_hash"]), 64)
            self.assertIn("password_salt", settings)
            self.assertIn("token_secret", settings)
            self.assertEqual(settings["token_version"], 1)

    def test_wrong_credentials_are_authentication_errors(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)

            with self.assertRaises(AuthenticationError) as password_error:
                login(state_dir, {"username": "admin", "password": "wrong"})
            with self.assertRaises(AuthenticationError) as username_error:
                login(state_dir, {"username": "not-admin", "password": "admin"})

            self.assertEqual(password_error.exception.status_code, 401)
            self.assertEqual(username_error.exception.status_code, 401)

    def test_bearer_token_is_valid_until_ttl_and_then_expires(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            issued_at = 1_700_000_000
            with patch("gp_control_plane.auth.time.time", return_value=issued_at):
                token = login(state_dir, {"username": "admin", "password": "admin"})

            with patch("gp_control_plane.auth.time.time", return_value=issued_at + TOKEN_TTL_SECONDS - 1):
                require_bearer_token(state_dir, f"Bearer {token['access_token']}")
            with patch("gp_control_plane.auth.time.time", return_value=issued_at + TOKEN_TTL_SECONDS):
                with self.assertRaises(AuthenticationError) as expired:
                    require_bearer_token(state_dir, f"Bearer {token['access_token']}")

            self.assertEqual(expired.exception.status_code, 401)

    def test_existing_auth_settings_skip_initial_pbkdf2_for_valid_and_malformed_bearer(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            issued_at = 1_700_000_000
            with patch("gp_control_plane.auth.time.time", return_value=issued_at):
                token = login(state_dir, {"username": "admin", "password": "admin"})

            initial_calls = 0
            pbkdf2_calls = 0
            original_initial_settings = auth._initial_settings
            original_password_hash = auth._password_hash

            def counted_initial_settings() -> dict[str, object]:
                nonlocal initial_calls
                initial_calls += 1
                return original_initial_settings()

            def counted_password_hash(password: str, salt: str) -> str:
                nonlocal pbkdf2_calls
                pbkdf2_calls += 1
                return original_password_hash(password, salt)

            with patch("gp_control_plane.auth._initial_settings", side_effect=counted_initial_settings), patch(
                "gp_control_plane.auth._password_hash", side_effect=counted_password_hash
            ), patch("gp_control_plane.auth.time.time", return_value=issued_at + 1):
                require_bearer_token(state_dir, f"Bearer {token['access_token']}")
                with self.assertRaises(AuthenticationError):
                    require_bearer_token(state_dir, "Bearer malformed-token")

            self.assertEqual(initial_calls, 0)
            self.assertEqual(pbkdf2_calls, 0)

    def test_short_new_password_is_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)

            with self.assertRaises(PasswordValidationError) as error:
                change_password(
                    state_dir,
                    {"current_password": "admin", "new_password": "x" * (PASSWORD_MIN_LENGTH - 1)},
                )

            self.assertEqual(error.exception.status_code, 400)

    def test_password_change_persists_and_invalidates_old_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            issued_at = 1_700_000_000
            with patch("gp_control_plane.auth.time.time", return_value=issued_at):
                old_token = login(state_dir, {"username": "admin", "password": "admin"})
                fresh_token = change_password(
                    state_dir,
                    {"current_password": "admin", "new_password": "newpass8"},
                )

            with patch("gp_control_plane.auth.time.time", return_value=issued_at + 1):
                with self.assertRaises(AuthenticationError):
                    require_bearer_token(state_dir, f"Bearer {old_token['access_token']}")
                require_bearer_token(state_dir, f"Bearer {fresh_token['access_token']}")
            with self.assertRaises(AuthenticationError):
                login(state_dir, {"username": "admin", "password": "admin"})
            replacement_login = login(state_dir, {"username": "admin", "password": "newpass8"})

            settings = read_app_setting(state_dir, AUTH_SETTINGS_KEY)
            self.assertEqual(settings["token_version"], 2)
            self.assertNotEqual(old_token["access_token"], fresh_token["access_token"])
            self.assertIn(".", replacement_login["access_token"])

    def test_fresh_process_bearer_validation_uses_auth_busy_budget_before_migration(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "uninitialized-state"
            context = multiprocessing.get_context("spawn")
            entered = context.Event()
            release = context.Event()
            holder_results = context.Queue()
            auth_results = context.Queue()
            holder = context.Process(
                target=_hold_sqlite_immediate_transaction,
                args=(str(state_dir), entered, release, holder_results),
                name="sqlite-write-lock-holder",
            )
            validation = context.Process(
                target=_require_bearer_token_from_fresh_process,
                args=(str(state_dir), auth_results),
                name="fresh-bearer-validation",
            )
            holder.start()
            try:
                self.assertTrue(entered.wait(timeout=_PROCESS_TIMEOUT_SECONDS))
                validation.start()
                result, elapsed = auth_results.get(timeout=(AUTH_BUSY_TIMEOUT_MS / 1000) + 2)
                self.assertEqual(result, "storage_unavailable", elapsed)
                self.assertIsInstance(elapsed, float)
                self.assertLessEqual(elapsed, (AUTH_BUSY_TIMEOUT_MS / 1000) + 1)
            finally:
                release.set()
                if validation.pid is not None:
                    _join_process(self, validation)
                _join_process(self, holder)

            self.assertEqual(holder_results.get(timeout=_PROCESS_TIMEOUT_SECONDS), ("released", ""))
            token = login(state_dir, {"username": "admin", "password": "admin"})["access_token"]
            require_bearer_token(state_dir, f"Bearer {token}")
            with self.assertRaises(AuthenticationError):
                require_bearer_token(state_dir, "Bearer invalid-token")

    def test_password_rotation_is_linearizable_across_spawn_processes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            old_token = login(state_dir, {"username": "admin", "password": "admin"})["access_token"]
            context = multiprocessing.get_context("spawn")
            validation_entered = context.Event()
            release_validation = context.Event()
            rotation_attempted = context.Event()
            rotation_finished = context.Event()
            validation_results = context.Queue()
            rotation_results = context.Queue()
            validation = context.Process(
                target=_hold_validated_token_in_auth_transaction,
                args=(str(state_dir), old_token, validation_entered, release_validation, validation_results),
                name="old-token-validation",
            )
            rotation = context.Process(
                target=_change_password_in_process,
                args=(str(state_dir), rotation_attempted, rotation_finished, rotation_results),
                name="password-rotation",
            )
            validation.start()
            try:
                self.assertTrue(validation_entered.wait(timeout=_PROCESS_TIMEOUT_SECONDS))
                rotation.start()
                self.assertTrue(rotation_attempted.wait(timeout=_PROCESS_TIMEOUT_SECONDS))
                self.assertFalse(rotation_finished.is_set(), "password rotation committed before token validation released")
                release_validation.set()
                _join_process(self, validation)
                _join_process(self, rotation)
            finally:
                release_validation.set()
                if validation.is_alive():
                    _join_process(self, validation)
                if rotation.pid is not None and rotation.is_alive():
                    _join_process(self, rotation)

            self.assertEqual(validation_results.get(timeout=_PROCESS_TIMEOUT_SECONDS), ("validated", ""))
            result_kind, fresh_token = rotation_results.get(timeout=_PROCESS_TIMEOUT_SECONDS)
            self.assertEqual(result_kind, "token")
            self.assertIsInstance(fresh_token, dict)
            fresh_access_token = str(fresh_token["access_token"])

            post_rotation_results = context.Queue()
            old_token_check = context.Process(
                target=_require_token_in_process,
                args=(str(state_dir), old_token, post_rotation_results),
                name="old-token-after-rotation",
            )
            fresh_token_check = context.Process(
                target=_require_token_in_process,
                args=(str(state_dir), fresh_access_token, post_rotation_results),
                name="fresh-token-after-rotation",
            )
            old_token_check.start()
            fresh_token_check.start()
            _join_process(self, old_token_check)
            _join_process(self, fresh_token_check)
            self.assertEqual(
                {post_rotation_results.get(timeout=_PROCESS_TIMEOUT_SECONDS) for _ in range(2)},
                {(old_token, "rejected"), (fresh_access_token, "accepted")},
            )


if __name__ == "__main__":
    unittest.main()
