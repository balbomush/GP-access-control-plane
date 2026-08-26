#!/bin/sh
# Fixed, root-owned but unprivileged final preflight for GP clean-remove.
# It derives every path from the declared install user, reads vault contents
# only after runuser dropped privileges, and never prints the handoff secret.
set -eu
umask 077

PATH='/usr/bin:/bin'
export PATH

die() {
    printf 'gp-clean-remove-preflight: %s\n' "$1" >&2
    exit 126
}

usage() {
    printf '%s\n' 'usage: gp-clean-remove-preflight.sh --install-user USER' >&2
    exit 64
}

[ "$(id -u)" -ne 0 ] || die 'must run as the declared non-root install user'
[ "$#" -eq 2 ] || usage
[ "$1" = --install-user ] || usage
INSTALL_USER=$2
case "$INSTALL_USER" in ''|root|*[!A-Za-z0-9_-]*) die 'install user is invalid' ;; esac
for command_name in cut getent id python3; do
    command -v "$command_name" >/dev/null 2>&1 || die "required command is unavailable: $command_name"
done
CURRENT_USER=$(id -un 2>/dev/null || true)
[ "$CURRENT_USER" = "$INSTALL_USER" ] || die 'effective user does not match the declared install user'
TARGET_HOME=$(getent passwd "$INSTALL_USER" | cut -d: -f6) || die 'cannot resolve install-user home'
[ -n "$TARGET_HOME" ] || die 'cannot resolve install-user home'

python3 - "$INSTALL_USER" "$TARGET_HOME" <<'PY'
import hashlib
import json
import os
import pwd
import stat
import sys
from pathlib import Path
from typing import Optional


def fail(message: str) -> None:
    print(f"gp-clean-remove-preflight: {message}", file=sys.stderr)
    raise SystemExit(126)


def require_canonical_directory(path: Path, *, mode: Optional[int] = None, label: str) -> None:
    try:
        details = path.lstat()
    except OSError:
        fail(f"{label} is unavailable")
    if not stat.S_ISDIR(details.st_mode) or path.is_symlink():
        fail(f"{label} is not a canonical directory")
    if path.resolve(strict=True) != path:
        fail(f"{label} is non-canonical")
    if details.st_uid != os.geteuid():
        fail(f"{label} owner does not match the install user")
    if mode is not None and stat.S_IMODE(details.st_mode) != mode:
        fail(f"{label} has unsafe mode")


def require_private_regular(path: Path, label: str) -> None:
    try:
        details = path.lstat()
    except OSError:
        fail(f"{label} is unavailable")
    if not stat.S_ISREG(details.st_mode) or path.is_symlink():
        fail(f"{label} is not a regular non-symlink file")
    if details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) != 0o600:
        fail(f"{label} owner or mode is unsafe")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


install_user = sys.argv[1]
home = Path(sys.argv[2])
try:
    effective_name = pwd.getpwuid(os.geteuid()).pw_name
except KeyError:
    fail("effective user cannot be resolved")
if os.geteuid() == 0 or effective_name != install_user:
    fail("effective user does not match the declared install user")

require_canonical_directory(home, label="install-user home")
for parent in (
    home / ".local",
    home / ".local" / "share",
    home / ".local" / "share" / "gp-control-plane",
):
    require_canonical_directory(parent, label="clean-install parent")

vault = home / ".local" / "share" / "gp-control-plane" / "clean-install-vault"
handoff_parent = home / ".local" / "share" / "gp-control-plane" / "clean-install-handoff"
archive = vault / "archive.zip"
entry = vault / "entry.json"
handoff = handoff_parent / "handoff.json"
require_canonical_directory(vault, mode=0o700, label="clean-install vault")
require_canonical_directory(handoff_parent, mode=0o700, label="clean-install handoff directory")
for path, label in (
    (archive, "vault archive"),
    (entry, "vault entry"),
    (handoff, "device-local handoff"),
):
    require_private_regular(path, label)

try:
    entry_payload = json.loads(entry.read_text(encoding="utf-8"))
    handoff_payload = json.loads(handoff.read_text(encoding="utf-8"))
except (OSError, UnicodeDecodeError, json.JSONDecodeError):
    fail("vault metadata is invalid")
if not isinstance(entry_payload, dict) or not isinstance(handoff_payload, dict):
    fail("vault metadata is invalid")

vault_id = str(entry_payload.get("vault_id") or "")
archive_sha256 = str(entry_payload.get("archive_sha256") or "")
handoff_secret_sha256 = str(entry_payload.get("handoff_secret_sha256") or "")
handoff_vault_id = str(handoff_payload.get("vault_id") or "")
handoff_secret = str(handoff_payload.get("handoff_secret") or "")
if len(vault_id) != 32 or any(character not in "0123456789abcdef" for character in vault_id):
    fail("vault id is invalid")
if len(archive_sha256) != 64 or any(character not in "0123456789abcdef" for character in archive_sha256):
    fail("vault archive checksum is invalid")
if len(handoff_secret_sha256) != 64 or any(character not in "0123456789abcdef" for character in handoff_secret_sha256):
    fail("handoff secret checksum is invalid")
if handoff_vault_id != vault_id:
    fail("device-local handoff id does not match the vault entry")
if not handoff_secret or hashlib.sha256(handoff_secret.encode("utf-8")).hexdigest() != handoff_secret_sha256:
    fail("device-local handoff secret does not match the vault entry")
try:
    if sha256_file(archive) != archive_sha256:
        fail("vault archive checksum does not match the vault entry")
except OSError:
    fail("vault archive cannot be read")
PY

printf '%s\n' 'status=success phase=clean-remove-preflight'
