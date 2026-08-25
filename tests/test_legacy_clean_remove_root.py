from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gp-clean-remove-root.sh"


class LegacyCleanRemoveRootContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_only_accepts_explicit_fixed_user_confirmation_grammar(self) -> None:
        self.assertIn('[ "$#" -eq 3 ] || usage', self.source)
        self.assertIn('[ "$1" = --install-user ] && [ "$3" = --confirm-clean-remove ] || usage', self.source)
        self.assertIn("case \"$INSTALL_USER\" in ''|root|*[!A-Za-z0-9_-]*)", self.source)
        self.assertNotIn("--install-dir", self.source)
        self.assertNotIn("--state-dir", self.source)
        self.assertNotIn("--vault-dir", self.source)

    def test_requires_separate_root_owned_fixed_path_provision_before_removal(self) -> None:
        self.assertIn("readonly CLEAN_REMOVE_ROOT='/usr/local/libexec/gp-control-plane/gp-clean-remove-root'", self.source)
        provision = self.source[
            self.source.index("require_fixed_root_provision() {") : self.source.index("safe_user_directory() {")
        ]
        self.assertIn('[ "$self_path" = "$CLEAN_REMOVE_ROOT" ]', provision)
        self.assertIn("'0:0:700'", provision)
        dispatch = self.source[self.source.index('[ "$(id -u)" -eq 0 ] ||') :]
        self.assertIn("require_fixed_root_provision || exit 126", dispatch)
        self.assertLess(dispatch.index("require_fixed_root_provision || exit 126"), dispatch.index("trap 'on_signal HUP' HUP"))

    def test_supports_exactly_the_two_known_baseline_state_layouts(self) -> None:
        fixed = self.source[
            self.source.index("validate_fixed_paths() {") : self.source.index("record_and_lock_parent() {")
        ]
        self.assertIn('INSTALL_DIR="$GP_ROOT/GP-access-control-plane"', fixed)
        self.assertIn('LEGACY_STATE_DIR="$INSTALL_DIR/build/state"', fixed)
        self.assertIn('CURRENT_STATE_ROOT="$GP_ROOT/.GP-access-control-plane.data"', fixed)
        self.assertIn('CURRENT_STATE_DIR="$CURRENT_STATE_ROOT/state"', fixed)
        self.assertIn('neither supported legacy nor current GP state layout exists', fixed)
        self.assertNotIn("GP_STATE_DIR", fixed)

    def test_root_boundary_never_reads_or_deletes_vault_content(self) -> None:
        boundary = self.source[
            self.source.index("validate_fixed_paths() {") : self.source.index("record_and_lock_parent() {")
        ]
        removal = self.source[
            self.source.index("remove_old_gp_surface() {") : self.source.index("cleanup() {")
        ]
        for forbidden in ("archive.zip", "entry.json", "sha256", "python3", "git ", "curl ", "checkout", "fetch"):
            self.assertNotIn(forbidden, boundary)
            self.assertNotIn(forbidden, removal)
        self.assertIn('safe_user_private_directory "$VAULT_DIR"', boundary)
        self.assertNotIn("VAULT_DIR", removal)

    def test_removal_is_one_way_and_limited_to_gp_surface(self) -> None:
        removal = self.source[
            self.source.index("remove_old_gp_surface() {") : self.source.index("cleanup() {")
        ]
        for path in (
            "/etc/systemd/system/gp-control-plane-core.service",
            "/etc/systemd/system/gp-control-plane-web.service",
            "/etc/default/gp-control-plane-install-profile",
            "/etc/sudoers.d/gp-control-plane-root-helper",
            "/usr/local/libexec/gp-control-plane/gp-root-helper",
            'rm -rf --one-file-system -- "$INSTALL_DIR"',
            'rm -rf --one-file-system -- "$CURRENT_STATE_ROOT"',
        ):
            self.assertIn(path, removal)
        for forbidden in ("rollback", "install-linux.sh", "gp-root-helper clean-install", "activate", "restore"):
            self.assertNotIn(forbidden, removal.lower())

    def test_both_approved_baselines_use_one_of_the_fixed_layouts(self) -> None:
        expected = {
            "v0.3.4": "$INSTALL_DIR/build/state",
            "v0.3.5-alpha.4": "$INSTALL_DIR/build/state",
        }
        for tag, layout in expected.items():
            with self.subTest(tag=tag):
                installer = subprocess.run(
                    ["git", "-c", f"safe.directory={ROOT.as_posix()}", "show", f"{tag}:scripts/install-linux.sh"],
                    cwd=ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if installer.returncode != 0:
                    installer = subprocess.run(
                        ["git", "-c", f"safe.directory={ROOT.as_posix()}", "show", f"{tag}:scripts/install-raspberry-pi.sh"],
                        cwd=ROOT,
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                self.assertIn(layout, installer.stdout)

    @unittest.skipUnless(os.name == "posix" and os.geteuid() == 0, "requires isolated POSIX root harness")
    def test_posix_harness_covers_both_layouts_and_keeps_vault(self) -> None:
        library = self.source.split('[ "$(id -u)" -eq 0 ] ||', 1)[0]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            harness = textwrap.dedent(
                r'''
                set -eu
                TEST_ROOT=$1
                mkdir -p "$TEST_ROOT/bin"
                cat > "$TEST_ROOT/bin/systemctl" <<'SH'
                #!/bin/sh
                printf '%s\n' "$*" >> "$TEST_CALLS"
                case "$1" in
                  show) printf '%s\n' loaded ;;
                  is-active) exit 1 ;;
                  stop|unmask|disable|daemon-reload) exit 0 ;;
                  *) exit 1 ;;
                esac
                SH
                cat > "$TEST_ROOT/bin/rm" <<'SH'
                #!/bin/sh
                printf 'rm %s\n' "$*" >> "$TEST_CALLS"
                exit 0
                SH
                cat > "$TEST_ROOT/bin/rmdir" <<'SH'
                #!/bin/sh
                printf 'rmdir %s\n' "$*" >> "$TEST_CALLS"
                exit 0
                SH
                chmod 700 "$TEST_ROOT/bin/systemctl" "$TEST_ROOT/bin/rm" "$TEST_ROOT/bin/rmdir"
                TEST_CALLS="$TEST_ROOT/calls"
                export TEST_CALLS PATH="$TEST_ROOT/bin:$PATH"
                '''
            )
            harness += library
            harness += textwrap.dedent(
                r'''
                TEST_UID="$(command id -u nobody)"
                TEST_GID="$(command id -g nobody)"
                id() {
                  if [ "$1" = -u ] && [ "${2:-}" = gpuser ]; then printf '%s\n' "$TEST_UID"; else command id "$@"; fi
                }
                getent() { [ "$1" = passwd ] && [ "$2" = gpuser ] && printf 'gpuser:x:%s:%s::%s:/bin/sh\n' "$TEST_UID" "$TEST_GID" "$TEST_HOME"; }
                setup_variant() {
                  variant=$1
                  TEST_HOME="$TEST_ROOT/home-$variant"
                  mkdir -p "$TEST_HOME/gp/GP-access-control-plane" "$TEST_HOME/.local/share/gp-control-plane/clean-install-vault"
                  if [ "$variant" = legacy ]; then
                    mkdir -p "$TEST_HOME/gp/GP-access-control-plane/build/state"
                  else
                    mkdir -p "$TEST_HOME/gp/.GP-access-control-plane.data/state"
                  fi
                  chown -R "$TEST_UID:$TEST_GID" "$TEST_HOME"
                  find "$TEST_HOME" -type d -exec chmod 700 {} \;
                  INSTALL_USER=gpuser
                  validate_fixed_paths
                  : > "$TEST_CALLS"
                  acquire_parent_locks
                  revalidate_locked_fixed_paths
                  chown root:root "$INSTALL_DIR"
                  ! revalidate_locked_fixed_paths
                  chown "$TEST_UID:$TEST_GID" "$INSTALL_DIR"
                  revalidate_locked_fixed_paths
                  remove_old_gp_surface
                  grep -Fq "rm -rf --one-file-system -- $TEST_HOME/gp/GP-access-control-plane" "$TEST_CALLS"
                  [ -d "$VAULT_DIR" ] && [ "$(stat -c '%u:%a' "$VAULT_DIR")" = "$TEST_UID:700" ]
                  release_parent_locks
                  [ "$(stat -c '%u:%g:%a' "$TEST_HOME")" = "$TEST_UID:$TEST_GID:700" ]
                  [ "$(stat -c '%u:%g:%a' "$TEST_HOME/gp")" = "$TEST_UID:$TEST_GID:700" ]
                }
                setup_variant legacy
                setup_variant current
                TEST_HOME="$TEST_ROOT/no-state"
                mkdir -p "$TEST_HOME/gp/GP-access-control-plane" "$TEST_HOME/.local/share/gp-control-plane/clean-install-vault"
                chown -R "$TEST_UID:$TEST_GID" "$TEST_HOME"
                find "$TEST_HOME" -type d -exec chmod 700 {} \;
                INSTALL_USER=gpuser
                ! validate_fixed_paths
                '''
            )
            completed = subprocess.run(
                ["/bin/sh", "-s", str(root)],
                input=harness,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)

    def test_shell_syntax_is_valid(self) -> None:
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash is required for shell syntax validation")
        result = subprocess.run([bash, "-n", str(SCRIPT)], check=False, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        if os.name != "nt":
            self.assertFalse(stat.S_IMODE(SCRIPT.stat().st_mode) & stat.S_IWOTH)


if __name__ == "__main__":
    unittest.main()
