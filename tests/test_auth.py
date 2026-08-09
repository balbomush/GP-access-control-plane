from __future__ import annotations

import sys
import tempfile
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
from gp_control_plane.storage import read_app_setting


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


if __name__ == "__main__":
    unittest.main()