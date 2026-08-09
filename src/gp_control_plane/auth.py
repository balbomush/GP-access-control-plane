from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Any

from .state import now_iso
from .storage import connect, read_app_setting, read_or_create_app_setting, save_app_setting


AUTH_SETTINGS_KEY = "auth"
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "admin"
PASSWORD_MIN_LENGTH = 8
TOKEN_TTL_SECONDS = 24 * 60 * 60
PBKDF2_ITERATIONS = 240_000


class AuthenticationError(ValueError):
    """Authentication failure that API adapters must expose as HTTP 401."""

    status_code = 401


class PasswordValidationError(ValueError):
    """Invalid new-password input that API adapters must expose as HTTP 400."""

    status_code = 400


def login(state_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    username = str(payload.get("username") or "").strip()
    password = _password_value(payload.get("password"))
    settings = _settings(state_dir)
    if username != DEFAULT_USERNAME or not _password_matches(settings, password):
        raise AuthenticationError("invalid credentials")
    return _token_payload(settings)


def change_password(state_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    current_password = _password_value(payload.get("current_password"))
    new_password = _password_value(payload.get("new_password"))
    if len(new_password) < PASSWORD_MIN_LENGTH:
        raise PasswordValidationError(f"new password must be at least {PASSWORD_MIN_LENGTH} characters")
    settings = _settings(state_dir)
    if not _password_matches(settings, current_password):
        raise AuthenticationError("invalid current password")
    salt = secrets.token_urlsafe(24)
    updated = {
        **settings,
        "password_salt": salt,
        "password_hash": _password_hash(new_password, salt),
        "token_secret": secrets.token_urlsafe(32),
        "token_version": int(settings["token_version"]) + 1,
    }
    save_app_setting(state_dir, AUTH_SETTINGS_KEY, updated, now_iso())
    return _token_payload(updated)


def require_bearer_token(state_dir: Path, authorization: str | None) -> None:
    if not isinstance(authorization, str):
        raise AuthenticationError("missing bearer token")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthenticationError("missing bearer token")
    settings = _settings(state_dir)
    signed_payload, separator, signature = token.strip().partition(".")
    if not separator or not signature:
        raise AuthenticationError("invalid bearer token")
    expected = _sign(settings, signed_payload)
    if not hmac.compare_digest(signature, expected):
        raise AuthenticationError("invalid bearer token")
    try:
        payload = json.loads(_decode(signed_payload).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise AuthenticationError("invalid bearer token") from exc
    if not isinstance(payload, dict):
        raise AuthenticationError("invalid bearer token")
    if payload.get("sub") != DEFAULT_USERNAME:
        raise AuthenticationError("invalid bearer token")
    try:
        token_version = int(payload["token_version"])
        expires_at = int(payload["exp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthenticationError("invalid bearer token") from exc
    if token_version != int(settings["token_version"]):
        raise AuthenticationError("expired bearer token")
    if expires_at <= int(time.time()):
        raise AuthenticationError("expired bearer token")


def health_payload() -> dict[str, str]:
    return {"status": "ok"}


def _settings(state_dir: Path) -> dict[str, Any]:
    raw = read_app_setting(state_dir, AUTH_SETTINGS_KEY)
    if raw is None:
        if _auth_setting_exists(state_dir):
            raise AuthenticationError("auth settings are invalid")
        raw = read_or_create_app_setting(state_dir, AUTH_SETTINGS_KEY, _initial_settings(), now_iso())
    if not isinstance(raw, dict):
        raise AuthenticationError("auth settings are invalid")
    required = {"password_salt", "password_hash", "token_secret", "token_version"}
    if not required.issubset(raw):
        raise AuthenticationError("auth settings are invalid")
    return raw


def _auth_setting_exists(state_dir: Path) -> bool:
    with connect(state_dir) as conn:
        row = conn.execute("SELECT 1 FROM app_settings WHERE key = ?", (AUTH_SETTINGS_KEY,)).fetchone()
    return row is not None


def _initial_settings() -> dict[str, Any]:
    salt = secrets.token_urlsafe(24)
    return {
        "username": DEFAULT_USERNAME,
        "password_salt": salt,
        "password_hash": _password_hash(DEFAULT_PASSWORD, salt),
        "token_secret": secrets.token_urlsafe(32),
        "token_version": 1,
    }


def _password_value(value: Any) -> str:
    return str(value) if isinstance(value, str) else ""


def _password_hash(password: str, salt: str) -> str:
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PBKDF2_ITERATIONS,
    )
    return derived.hex()


def _password_matches(settings: dict[str, Any], password: str) -> bool:
    expected = str(settings.get("password_hash") or "")
    actual = _password_hash(password, str(settings.get("password_salt") or ""))
    return hmac.compare_digest(actual, expected)


def _token_payload(settings: dict[str, Any]) -> dict[str, Any]:
    expires_at = int(time.time()) + TOKEN_TTL_SECONDS
    payload = {
        "sub": DEFAULT_USERNAME,
        "exp": expires_at,
        "token_version": int(settings["token_version"]),
        "nonce": secrets.token_urlsafe(12),
    }
    encoded = _encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return {
        "access_token": f"{encoded}.{_sign(settings, encoded)}",
        "token_type": "Bearer",
        "expires_in": TOKEN_TTL_SECONDS,
    }


def _sign(settings: dict[str, Any], payload: str) -> str:
    secret = str(settings.get("token_secret") or "").encode("utf-8")
    return _encode(hmac.new(secret, payload.encode("ascii"), hashlib.sha256).digest())


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))