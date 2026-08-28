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
PROVISIONER = ROOT / "scripts" / "gp-clean-remove-provision-root.sh"
PREFLIGHT = ROOT / "scripts" / "gp-clean-remove-preflight.sh"


class LegacyCleanRemoveRootContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")
        cls.provisioner = PROVISIONER.read_text(encoding="utf-8")
        cls.preflight = PREFLIGHT.read_text(encoding="utf-8")

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

    def test_release_gates_directory_has_the_only_install_group_exception(self) -> None:
        generic = self.source[
            self.source.index("validate_root_directory() {") : self.source.index("validate_release_gates_directory() {")
        ]
        special = self.source[
            self.source.index("validate_release_gates_directory() {") : self.source.index("validate_unit_path() {")
        ]
        fixed = self.source[
            self.source.index("validate_fixed_paths() {") : self.source.index("record_and_lock_parent() {")
        ]
        removal_surface = self.source[
            self.source.index("validate_removal_surface() {") : self.source.index("validate_fixed_paths() {")
        ]
        self.assertIn("= '0:0'", generic)
        self.assertNotIn("INSTALL_GID", generic)
        self.assertIn('release_gates_dir=/var/lib/gp-control-plane/release-gates', special)
        self.assertIn('"0:$INSTALL_GID:750"', special)
        self.assertIn('INSTALL_GID=$(id -g "$INSTALL_USER")', fixed)
        self.assertIn("install-user group must be a nonzero numeric GID", fixed)
        self.assertIn("validate_release_gates_directory", removal_surface)
        self.assertNotIn("validate_root_directory /var/lib/gp-control-plane/release-gates", removal_surface)

    def test_root_boundary_checks_vault_topology_without_reading_or_deleting_content(self) -> None:
        boundary = self.source[
            self.source.index("validate_fixed_paths() {") : self.source.index("record_and_lock_parent() {")
        ]
        removal = self.source[
            self.source.index("remove_old_gp_surface() {") : self.source.index("cleanup() {")
        ]
        for label in (
            "clean-install vault archive",
            "clean-install vault entry",
            "clean-install handoff directory",
            "clean-install handoff file",
        ):
            self.assertIn(label, boundary)
        self.assertIn('safe_user_private_file "$VAULT_ARCHIVE"', boundary)
        self.assertIn('safe_user_private_file "$VAULT_ENTRY"', boundary)
        self.assertIn('safe_user_private_directory "$HANDOFF_DIR"', boundary)
        self.assertIn('safe_user_private_file "$HANDOFF_FILE"', boundary)
        for forbidden in (
            'cat "$VAULT_ARCHIVE"',
            'cat "$VAULT_ENTRY"',
            'cat "$HANDOFF_FILE"',
            'sha256sum "$VAULT_ARCHIVE"',
            'sha256sum "$VAULT_ENTRY"',
            'sha256sum "$HANDOFF_FILE"',
            "python3",
            "git ",
            "curl ",
            "checkout",
            "fetch",
        ):
            self.assertNotIn(forbidden, boundary)
            self.assertNotIn(forbidden, removal)
        self.assertIn('safe_user_private_directory "$VAULT_DIR"', boundary)
        self.assertNotIn("VAULT_DIR", removal)
        self.assertNotIn("HANDOFF_FILE", removal)

    def test_root_preflight_rejects_missing_symlinked_or_unsafe_private_sources_before_remove(self) -> None:
        preflight = self.source[
            self.source.index("safe_user_private_file() {") : self.source.index("record_and_lock_parent() {")
        ]
        flow = self.source[
            self.source.index("run_preclean_flow() {") : self.source.index("release_parent_locks() {")
        ]
        removal = self.source[
            self.source.index("remove_old_gp_surface() {") : self.source.index("cleanup() {")
        ]
        self.assertIn('[ -f "$path" ] && [ ! -L "$path" ]', preflight)
        self.assertIn('"$uid:600"', preflight)
        self.assertIn('safe_user_private_directory "$VAULT_DIR"', preflight)
        self.assertIn('safe_user_private_file "$VAULT_ARCHIVE"', preflight)
        self.assertIn('safe_user_private_file "$VAULT_ENTRY"', preflight)
        self.assertIn('safe_user_private_directory "$HANDOFF_DIR"', preflight)
        self.assertIn('safe_user_private_file "$HANDOFF_FILE"', preflight)
        self.assertLess(flow.index("validate_fixed_paths"), flow.index("run_final_unprivileged_preflight"))
        self.assertLess(flow.index("run_final_unprivileged_preflight"), flow.index("acquire_parent_locks"))
        self.assertLess(flow.index("acquire_parent_locks"), flow.index("remove_old_gp_surface"))
        self.assertIn("DESTRUCTIVE_PHASE=1", removal)

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
            "/usr/local/libexec/gp-control-plane/gp-clean-remove-adapter",
            "/usr/local/libexec/gp-control-plane/gp-clean-remove-adapter.manifest",
            'rm -rf --one-file-system -- "$INSTALL_DIR"',
            'rm -rf --one-file-system -- "$CURRENT_STATE_ROOT"',
        ):
            self.assertIn(path, removal)
        for forbidden in ("rollback", "install-linux.sh", "gp-root-helper clean-install", "activate", "restore"):
            self.assertNotIn(forbidden, removal.lower())

    def test_cleaner_accepts_only_complete_safe_trust_anchor_and_removes_it_before_rmdir(self) -> None:
        directory = self.source[
            self.source.index("validate_root_helper_directory() {") : self.source.index("validate_removal_surface() {")
        ]
        surface = self.source[
            self.source.index("validate_removal_surface() {") : self.source.index("validate_fixed_paths() {")
        ]
        flow = self.source[
            self.source.index("run_preclean_flow() {") : self.source.index("release_parent_locks() {")
        ]
        removal = self.source[
            self.source.index("remove_old_gp_surface() {") : self.source.index("cleanup() {")
        ]

        self.assertIn("readonly CLEAN_REMOVE_ADAPTER='/usr/local/libexec/gp-control-plane/gp-clean-remove-adapter'", self.source)
        self.assertIn(
            "readonly CLEAN_REMOVE_ADAPTER_MANIFEST='/usr/local/libexec/gp-control-plane/gp-clean-remove-adapter.manifest'",
            self.source,
        )
        self.assertIn('"$CLEAN_REMOVE_ADAPTER"|"$CLEAN_REMOVE_ADAPTER_MANIFEST"', directory)
        self.assertIn('die "legacy root-helper directory contains a foreign path: $member"', directory)
        self.assertIn("validate_trust_anchor_members || return 1", surface)
        self.assertLess(flow.index("validate_removal_surface"), flow.index("remove_old_gp_surface"))

        trust = directory[directory.index("validate_trust_anchor_members() {") :]
        self.assertIn('[ "$adapter_present" = "$manifest_present" ]', trust)
        self.assertIn("legacy clean-remove trust anchor is incomplete", trust)
        self.assertIn('[ -f "$CLEAN_REMOVE_ADAPTER" ] && [ ! -L "$CLEAN_REMOVE_ADAPTER" ]', trust)
        self.assertIn("= '0:0:700'", trust)
        self.assertIn("legacy clean-remove adapter is unsafe", trust)
        self.assertIn('[ -f "$CLEAN_REMOVE_ADAPTER_MANIFEST" ] && [ ! -L "$CLEAN_REMOVE_ADAPTER_MANIFEST" ]', trust)
        self.assertIn("= '0:0:600'", trust)
        self.assertIn("legacy clean-remove adapter manifest is unsafe", trust)
        self.assertLess(trust.index("legacy clean-remove trust anchor is incomplete"), trust.index("legacy clean-remove adapter is unsafe"))
        self.assertLess(trust.index("legacy clean-remove adapter is unsafe"), trust.index("legacy clean-remove adapter manifest is unsafe"))

        adapter_remove = 'rm -f -- /usr/local/libexec/gp-control-plane/gp-clean-remove-adapter'
        manifest_remove = 'rm -f -- /usr/local/libexec/gp-control-plane/gp-clean-remove-adapter.manifest'
        rmdir = 'rmdir -- /usr/local/libexec/gp-control-plane'
        self.assertIn(adapter_remove, removal)
        self.assertIn(manifest_remove, removal)
        self.assertLess(removal.index(adapter_remove), removal.index(manifest_remove))
        self.assertLess(removal.index(manifest_remove), removal.index(rmdir))
        self.assertNotIn("rm -rf --one-file-system -- /usr/local/libexec/gp-control-plane", removal)

    def test_trust_anchor_adapter_has_no_user_cache_or_caller_selected_provision_interface(self) -> None:
        source = self.provisioner
        fixed = source[source.index("require_fixed_trust_anchor() {") : source.index('[ "$(id -u)" -eq 0 ] ||')]
        dispatch = source[source.index('[ "$(id -u)" -eq 0 ] ||') :]

        self.assertIn("readonly ROOT_ADAPTER='/usr/local/libexec/gp-control-plane/gp-clean-remove-adapter'", source)
        self.assertIn("readonly ROOT_ADAPTER_MANIFEST='/usr/local/libexec/gp-control-plane/gp-clean-remove-adapter.manifest'", source)
        self.assertIn("readonly ROOT_CLEANER='/usr/local/libexec/gp-control-plane/gp-clean-remove-root'", source)
        self.assertIn("readonly ROOT_PREFLIGHT='/usr/local/libexec/gp-control-plane/gp-clean-remove-preflight'", source)
        self.assertIn('[ "$#" -eq 2 ] && [ "$1" = clean-remove ] && [ "$2" = --confirm-clean-remove ]', dispatch)
        self.assertIn("usage: gp-clean-remove-adapter clean-remove --confirm-clean-remove", dispatch)
        self.assertIn("require_fixed_trust_anchor", dispatch)
        self.assertIn('exec "$ROOT_CLEANER" --install-user "$manifest_install_user" --confirm-clean-remove', dispatch)
        self.assertLess(dispatch.index("require_fixed_trust_anchor"), dispatch.index('exec "$ROOT_CLEANER"'))

        for required in (
            '[ "$self_path" = "$ROOT_ADAPTER" ]',
            "'0:0:700'",
            "'0:0:600'",
            "adapter must be a root:root mode 0700 regular file",
            "adapter manifest must be a root:root mode 0600 regular file",
            "cleaner must be a root:root mode 0700 regular file",
            "preflight must be a root:root mode 0755 regular file",
            "install_user",
            "candidate_sha",
            "adapter_sha256",
            "cleaner_sha256",
            "preflight_sha256",
            "NR == 5",
            "adapter manifest format is invalid",
            "adapter hash does not match its manifest",
            "cleaner hash does not match adapter manifest",
            "preflight hash does not match adapter manifest",
        ):
            self.assertIn(required, fixed)

        cleaner_exec = source.index('exec "$ROOT_CLEANER"')
        for failure in (
            "must be installed and invoked from $ROOT_ADAPTER",
            "adapter must be a root:root mode 0700 regular file",
            "adapter manifest must be a root:root mode 0600 regular file",
            "cleaner must be a root:root mode 0700 regular file",
            "preflight must be a root:root mode 0755 regular file",
            "adapter manifest format is invalid",
            "adapter hash does not match its manifest",
            "cleaner hash does not match adapter manifest",
            "preflight hash does not match adapter manifest",
        ):
            self.assertLess(source.index(failure), cleaner_exec)

        for forbidden in (
            "candidate_repository",
            "CANDIDATE_SHA",
            "CLEANER_SHA256",
            "PREFLIGHT_SHA256",
            "--candidate-sha",
            "--cleaner-sha256",
            "--preflight-sha256",
            "git -C",
            "git fetch",
            "git clone",
            "curl ",
            "mktemp",
            "mv -f",
            "systemctl",
            "queue-update",
            "rollback",
            "snapshot",
            "sudo ",
        ):
            self.assertNotIn(forbidden, source)
        self.assertNotIn("remove_old_gp_surface", source)

    def test_fixed_cleaner_requires_strict_manifest_and_runs_unprivileged_final_preflight_before_remove(self) -> None:
        provision = self.source[
            self.source.index("read_strict_manifest() {") : self.source.index("safe_user_directory() {")
        ]
        topology = self.source[
            self.source.index("validate_exact_private_members() {") : self.source.index("validate_root_file() {")
        ]
        flow = self.source[
            self.source.index("run_preclean_flow() {") : self.source.index("release_parent_locks() {")
        ]
        removal = self.source[
            self.source.index("remove_old_gp_surface() {") : self.source.index("cleanup() {")
        ]

        for item in (
            "candidate_sha",
            "cleaner_sha256",
            "preflight_sha256",
            "cleaner_path",
            "preflight_path",
            "NR == 5",
            "provisioned clean-remove manifest format is invalid",
            "provisioned clean-remove script hash does not match its manifest",
            "provisioned clean-remove preflight hash does not match its manifest",
            "root:root mode 0600",
            "root:root mode 0755",
        ):
            self.assertIn(item, provision)
        self.assertIn('validate_exact_private_members "$VAULT_DIR" "$uid"', topology)
        self.assertIn("archive.zip entry.json", topology)
        self.assertIn('validate_exact_private_members "$HANDOFF_DIR" "$uid"', topology)
        self.assertIn("handoff.json", topology)
        self.assertIn('runuser -u "$INSTALL_USER" -- "$CLEAN_REMOVE_PREFLIGHT" --install-user "$INSTALL_USER"', self.source)
        self.assertLess(flow.index("run_final_unprivileged_preflight"), flow.index("acquire_parent_locks"))
        self.assertLess(flow.index("acquire_parent_locks"), flow.index("remove_old_gp_surface"))
        self.assertIn("DESTRUCTIVE_PHASE=1", removal)

    def test_unprivileged_final_preflight_binds_id_archive_and_handoff_without_printing_secret(self) -> None:
        source = self.preflight
        self.assertIn('[ "$(id -u)" -ne 0 ] || die', source)
        self.assertIn('CURRENT_USER=$(id -un 2>/dev/null || true)', source)
        self.assertIn('effective user does not match the declared install user', source)
        self.assertIn('require_canonical_directory(vault, mode=0o700', source)
        self.assertIn('require_canonical_directory(handoff_parent, mode=0o700', source)
        self.assertIn('require_private_regular(path, label)', source)
        self.assertIn('handoff_vault_id != vault_id', source)
        self.assertIn('sha256_file(archive) != archive_sha256', source)
        self.assertIn('hashlib.sha256(handoff_secret.encode("utf-8")).hexdigest() != handoff_secret_sha256', source)
        self.assertNotIn('print(handoff_secret', source)
        self.assertNotIn('print(handoff_payload', source)

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
        library = self.source.split('[ "$(id -u)" -eq 0 ] ||', 1)[0].replace(
            "/var/lib/gp-control-plane/release-gates", '"$TEST_RELEASE_GATES"'
        ).replace(
            "PATH='/usr/sbin:/usr/bin:/sbin:/bin'",
            'PATH="$TEST_ROOT/bin:/usr/sbin:/usr/bin:/sbin:/bin"',
        )
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
                cat > "$TEST_ROOT/bin/runuser" <<'SH'
                #!/bin/sh
                printf 'runuser %s\n' "$*" >> "$TEST_CALLS"
                [ "${TEST_RUNUSER_FAIL:-0}" = 1 ] && exit 1
                exit 0
                SH
                chmod 700 "$TEST_ROOT/bin/systemctl" "$TEST_ROOT/bin/rm" "$TEST_ROOT/bin/rmdir" "$TEST_ROOT/bin/runuser"
                TEST_CALLS="$TEST_ROOT/calls"
                export TEST_CALLS TEST_RUNUSER_FAIL=0 PATH="$TEST_ROOT/bin:$PATH"
                '''
            )
            harness += library
            harness += textwrap.dedent(
                r'''
                TEST_UID="$(command id -u nobody)"
                TEST_GID="$(command id -g nobody)"
                TEST_RELEASE_GATES="$TEST_ROOT/release-gates"
                id() {
                  if [ "$1" = -u ] && [ "${2:-}" = gpuser ]; then printf '%s\n' "$TEST_UID"; else command id "$@"; fi
                }
                getent() { [ "$1" = passwd ] && [ "$2" = gpuser ] && printf 'gpuser:x:%s:%s::%s:/bin/sh\n' "$TEST_UID" "$TEST_GID" "$TEST_HOME"; }
                setup_variant() {
                  variant=$1
                  TEST_HOME="$TEST_ROOT/home-$variant"
                  mkdir -p "$TEST_HOME/gp/GP-access-control-plane" "$TEST_HOME/.local/share/gp-control-plane/clean-install-vault" "$TEST_HOME/.local/share/gp-control-plane/clean-install-handoff"
                  if [ "$variant" = legacy ]; then
                    mkdir -p "$TEST_HOME/gp/GP-access-control-plane/build/state"
                  else
                    mkdir -p "$TEST_HOME/gp/.GP-access-control-plane.data/state"
                  fi
                  chown -R "$TEST_UID:$TEST_GID" "$TEST_HOME"
                  find "$TEST_HOME" -type d -exec chmod 700 {} \;
                  printf 'archive' > "$TEST_HOME/.local/share/gp-control-plane/clean-install-vault/archive.zip"
                  printf 'entry' > "$TEST_HOME/.local/share/gp-control-plane/clean-install-vault/entry.json"
                  printf 'handoff' > "$TEST_HOME/.local/share/gp-control-plane/clean-install-handoff/handoff.json"
                  chown "$TEST_UID:$TEST_GID" "$TEST_HOME/.local/share/gp-control-plane/clean-install-vault/archive.zip" "$TEST_HOME/.local/share/gp-control-plane/clean-install-vault/entry.json" "$TEST_HOME/.local/share/gp-control-plane/clean-install-handoff/handoff.json"
                  chmod 600 "$TEST_HOME/.local/share/gp-control-plane/clean-install-vault/archive.zip" "$TEST_HOME/.local/share/gp-control-plane/clean-install-vault/entry.json" "$TEST_HOME/.local/share/gp-control-plane/clean-install-handoff/handoff.json"
                  INSTALL_USER=gpuser
                  validate_fixed_paths

                  # The final unprivileged validator must run while user
                  # ownership is still intact, before locks or removals.
                  validate_removal_surface() { :; }
                  : > "$TEST_CALLS"
                  run_preclean_flow
                  runuser_line=$(grep -n '^runuser ' "$TEST_CALLS" | cut -d: -f1)
                  stop_line=$(grep -n '^show ' "$TEST_CALLS" | cut -d: -f1)
                  rm_line=$(grep -n '^rm -rf --one-file-system -- ' "$TEST_CALLS" | head -n 1 | cut -d: -f1)
                  [ -n "$runuser_line" ] && [ -n "$stop_line" ] && [ -n "$rm_line" ]
                  [ "$runuser_line" -lt "$stop_line" ]
                  [ "$stop_line" -lt "$rm_line" ]
                  [ -d "$VAULT_DIR" ] && [ "$(stat -c '%u:%a' "$VAULT_DIR")" = "$TEST_UID:700" ]
                  release_parent_locks
                  [ "$(stat -c '%u:%g:%a' "$TEST_HOME")" = "$TEST_UID:$TEST_GID:700" ]

                  # The post-lock revalidation remains a separate TOCTOU
                  # boundary and rejects a changed parent before removal.
                  : > "$TEST_CALLS"
                  acquire_parent_locks
                  revalidate_locked_fixed_paths
                  chown root:root "$INSTALL_DIR"
                  ! revalidate_locked_fixed_paths
                  chown "$TEST_UID:$TEST_GID" "$INSTALL_DIR"
                  revalidate_locked_fixed_paths
                  release_parent_locks
                  [ "$(stat -c '%u:%g:%a' "$TEST_HOME")" = "$TEST_UID:$TEST_GID:700" ]
                  [ "$(stat -c '%u:%g:%a' "$TEST_HOME/gp")" = "$TEST_UID:$TEST_GID:700" ]
                }
                setup_variant legacy
                setup_variant current

                # A failure in the final user-owned content validation occurs
                # after root path checks, but before parent locks or any
                # destructive helper.  It must leave every private source and
                # the managed path ownership untouched.
                export TEST_RUNUSER_FAIL=1
                DESTRUCTIVE_PHASE=0
                before_home=$(stat -c '%u:%g:%a' "$TEST_HOME")
                before_gp=$(stat -c '%u:%g:%a' "$TEST_HOME/gp")
                before_install=$(stat -c '%u:%g:%a' "$TEST_HOME/gp/GP-access-control-plane")
                : > "$TEST_CALLS"
                ! run_preclean_flow
                [ "$PARENT_LOCKS_HELD" = 0 ]
                [ "$DESTRUCTIVE_PHASE" = 0 ]
                [ "$(stat -c '%u:%g:%a' "$TEST_HOME")" = "$before_home" ]
                [ "$(stat -c '%u:%g:%a' "$TEST_HOME/gp")" = "$before_gp" ]
                [ "$(stat -c '%u:%g:%a' "$TEST_HOME/gp/GP-access-control-plane")" = "$before_install" ]
                [ -d "$VAULT_DIR" ] && [ "$(stat -c '%u:%a' "$VAULT_DIR")" = "$TEST_UID:700" ]
                grep -Fq 'runuser -u gpuser -- ' "$TEST_CALLS"
                ! grep -Eq '^(show|is-active|stop|unmask|disable|daemon-reload|rm |rmdir )' "$TEST_CALLS"
                export TEST_RUNUSER_FAIL=0

                # The root boundary does not read private payloads, but it
                # must reject every absent/unsafe private source before a
                # single destructive helper command can be reached.
                TEST_ARCHIVE="$TEST_HOME/.local/share/gp-control-plane/clean-install-vault/archive.zip"
                TEST_ENTRY="$TEST_HOME/.local/share/gp-control-plane/clean-install-vault/entry.json"
                TEST_HANDOFF="$TEST_HOME/.local/share/gp-control-plane/clean-install-handoff/handoff.json"
                reject_preclean() {
                  : > "$TEST_CALLS"
                  ! validate_fixed_paths
                  [ ! -s "$TEST_CALLS" ]
                }
                /bin/rm -f -- "$TEST_ARCHIVE"
                reject_preclean
                printf 'archive' > "$TEST_ARCHIVE"
                chown "$TEST_UID:$TEST_GID" "$TEST_ARCHIVE"
                chmod 600 "$TEST_ARCHIVE"
                /bin/rm -f -- "$TEST_HANDOFF"
                reject_preclean
                printf 'handoff' > "$TEST_HANDOFF"
                chown "$TEST_UID:$TEST_GID" "$TEST_HANDOFF"
                chmod 600 "$TEST_HANDOFF"
                /bin/rm -f -- "$TEST_ARCHIVE"
                ln -s /tmp "$TEST_ARCHIVE"
                reject_preclean
                /bin/rm -f -- "$TEST_ARCHIVE"
                printf 'archive' > "$TEST_ARCHIVE"
                chown "$TEST_UID:$TEST_GID" "$TEST_ARCHIVE"
                chmod 600 "$TEST_ARCHIVE"
                chmod 644 "$TEST_ENTRY"
                reject_preclean
                chmod 600 "$TEST_ENTRY"

                # This is the sole fixed directory that may be root:<install
                # group>:0750. The generic root validator must still reject it.
                INSTALL_GID="$TEST_GID"
                validate_release_gates_directory
                mkdir "$TEST_RELEASE_GATES"
                chown root:"$TEST_GID" "$TEST_RELEASE_GATES"
                chmod 0750 "$TEST_RELEASE_GATES"
                validate_release_gates_directory
                ! validate_root_directory "$TEST_RELEASE_GATES"
                chmod 0755 "$TEST_RELEASE_GATES"
                ! validate_release_gates_directory
                chmod 0770 "$TEST_RELEASE_GATES"
                ! validate_release_gates_directory
                chown "$TEST_UID:$TEST_GID" "$TEST_RELEASE_GATES"
                chmod 0750 "$TEST_RELEASE_GATES"
                ! validate_release_gates_directory
                /bin/rm -rf -- "$TEST_RELEASE_GATES"
                : > "$TEST_RELEASE_GATES"
                chown root:"$TEST_GID" "$TEST_RELEASE_GATES"
                chmod 0750 "$TEST_RELEASE_GATES"
                ! validate_release_gates_directory
                /bin/rm -f -- "$TEST_RELEASE_GATES"
                ln -s /tmp "$TEST_RELEASE_GATES"
                ! validate_release_gates_directory
                /bin/rm -f -- "$TEST_RELEASE_GATES"

                TEST_HOME="$TEST_ROOT/no-state"
                mkdir -p "$TEST_HOME/gp/GP-access-control-plane" "$TEST_HOME/.local/share/gp-control-plane/clean-install-vault"
                chown -R "$TEST_UID:$TEST_GID" "$TEST_HOME"
                find "$TEST_HOME" -type d -exec chmod 700 {} \;
                printf 'archive' > "$TEST_HOME/.local/share/gp-control-plane/clean-install-vault/archive.zip"
                printf 'entry' > "$TEST_HOME/.local/share/gp-control-plane/clean-install-vault/entry.json"
                mkdir -p "$TEST_HOME/.local/share/gp-control-plane/clean-install-handoff"
                printf 'handoff' > "$TEST_HOME/.local/share/gp-control-plane/clean-install-handoff/handoff.json"
                chown "$TEST_UID:$TEST_GID" "$TEST_HOME/.local/share/gp-control-plane/clean-install-vault/archive.zip" "$TEST_HOME/.local/share/gp-control-plane/clean-install-vault/entry.json" "$TEST_HOME/.local/share/gp-control-plane/clean-install-handoff/handoff.json"
                chmod 600 "$TEST_HOME/.local/share/gp-control-plane/clean-install-vault/archive.zip" "$TEST_HOME/.local/share/gp-control-plane/clean-install-vault/entry.json" "$TEST_HOME/.local/share/gp-control-plane/clean-install-handoff/handoff.json"
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
