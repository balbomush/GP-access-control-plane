from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "gp-root-helper.sh"
RUNNER = ROOT / "scripts" / "clean-install-root-runner.sh"


class CleanInstallRootContractTests(unittest.TestCase):
    """Linux/root protocol contract tests.

    They deliberately do not pretend to prove a Raspberry Pi transaction on
    Windows.  Linux root coverage uses a fake ``systemctl`` harness and an
    isolated temporary filesystem, never real host units or paths.
    """

    def test_helper_accepts_only_exact_clean_install_grammar_before_mutation(self) -> None:
        source = HELPER.read_text(encoding="utf-8")
        self.assertIn("clean-install requires exactly --vault-id", source)
        self.assertIn('[ "$#" -eq 7 ] && [ "$1" = --vault-id ]', source)
        self.assertIn('[ "$3" = --candidate-ref ] && [ "$5" = --expected-sha ] && [ "$7" = --apply ]', source)
        self.assertIn('validate_clean_install_candidate_ref', source)
        self.assertIn('[ "${1:-}" = refs/heads/dev ]', source)
        clean_protocol = source.split("clean_install_dispatch() {", 1)[1].split("\nrequire_root", 1)[0]
        self.assertNotIn("GP_INSTALL_FORCE_CLEAN", clean_protocol)

    def test_root_runner_is_staged_and_vault_is_validation_only(self) -> None:
        helper = HELPER.read_text(encoding="utf-8")
        runner = RUNNER.read_text(encoding="utf-8")
        self.assertIn("scripts/clean-install-root-runner.sh", helper)
        self.assertIn("root-owned staged clean-install runner hash does not match", helper)
        self.assertIn("trusted-clean-install-v1", helper)
        self.assertIn("--trusted-clean-install", runner)
        self.assertIn("validate_vault()", runner)
        self.assertNotIn("rm -rf --one-file-system \"$VAULT_DIR\"", runner)
        self.assertNotIn("rm -f -- \"$VAULT_DIR", runner)

    def test_runner_has_single_terminal_and_signal_rollback_guards(self) -> None:
        runner = RUNNER.read_text(encoding="utf-8")
        self.assertIn("TERMINAL_WRITTEN=0", runner)
        self.assertIn("ROLLBACK_ATTEMPTED=0", runner)
        self.assertIn("on_signal()", runner)
        self.assertIn("status=failed rollback=completed signal=", runner)
        self.assertIn("status=failed rollback=failed signal=", runner)
        self.assertIn("trap cleanup EXIT", runner)

    def test_runner_declares_profile_aware_topology_and_vault_boundary_guards(self) -> None:
        runner = RUNNER.read_text(encoding="utf-8")
        self.assertIn("snapshot_service_topology gp-control-plane-core.service core", runner)
        self.assertIn("snapshot_service_topology gp-control-plane-web.service web", runner)
        self.assertIn("[ \"$load_state\" = not-found ] && return 0", runner)
        self.assertIn("refusing to stop masked", runner)
        self.assertIn("quiesce_rollback_services", runner)
        self.assertIn("refusing to stop currently masked", runner)
        self.assertIn("restore_service_topology gp-control-plane-core.service core", runner)
        self.assertIn("restore_service_topology gp-control-plane-web.service web", runner)
        self.assertIn("validate_transaction_boundaries || return 1", runner)
        self.assertIn("managed install directory overlaps the device-local vault boundary", runner)
        self.assertIn("managed state directory overlaps the device-local vault boundary", runner)

    def test_runner_locks_user_writable_parents_before_mutation_and_unlocks_before_activation(self) -> None:
        runner = RUNNER.read_text(encoding="utf-8")
        self.assertIn("acquire_parent_locks || return 1", runner)
        self.assertIn('record_and_lock_parent "$target_home" "$uid"', runner)
        self.assertIn('record_and_lock_parent "$(dirname -- "$INSTALL_DIR")" "$uid"', runner)
        self.assertIn("revalidate_parent_locks || return 1", runner)
        self.assertIn("chmod 0711 \"$path\"", runner)
        self.assertIn("'0:0:711'", runner)
        self.assertIn("validate_vault || return 1", runner)
        self.assertIn("release_parent_locks || return 1\n    resume_deferred_signal\n    systemctl daemon-reload", runner)
        self.assertLess(runner.index("acquire_parent_locks || return 1"), runner.index("snapshot_managed_directories || return 1"))
        self.assertLess(runner.index("activate_target_services_after_unlock || return 1"), runner.index("commit_success || return 1"))

    def test_prepublication_service_failure_restores_marked_active_units(self) -> None:
        runner = RUNNER.read_text(encoding="utf-8")
        self.assertIn("WEB_QUIESCED=0", runner)
        self.assertIn("CORE_QUIESCED=0", runner)
        self.assertIn("case \"$name\" in web) WEB_QUIESCED=1 ;; core) CORE_QUIESCED=1 ;; esac", runner)
        early_rollback = runner[runner.index('if [ "$PUBLISHED" != 1 ]; then') : runner.index('quiesce_rollback_services || return 1')]
        self.assertLess(early_rollback.index("release_parent_locks || return 1"), early_rollback.index("restore_quiesced_services || return 1"))
        self.assertIn("restore_service_topology gp-control-plane-web.service web", runner)

    def test_pre_stop_restore_intent_covers_stop_error_and_signal_window(self) -> None:
        runner = RUNNER.read_text(encoding="utf-8")
        transaction_globals = runner[: runner.index("die() {")]
        self.assertIn("CORE_RESTORE_REQUIRED=0", transaction_globals)
        self.assertIn("WEB_RESTORE_REQUIRED=0", transaction_globals)
        quiesce = runner[
            runner.index("quiesce_service() {") : runner.index("quiesce_rollback_services() {")
        ]
        self.assertLess(quiesce.index("WEB_RESTORE_REQUIRED=1"), quiesce.index('systemctl stop "$unit"'))
        self.assertLess(quiesce.index("CORE_RESTORE_REQUIRED=1"), quiesce.index('systemctl stop "$unit"'))
        restore = runner[
            runner.index("restore_quiesced_services() {") : runner.index("target_profile_web_enabled() {")
        ]
        self.assertIn("CORE_RESTORE_REQUIRED", restore)
        self.assertIn("WEB_RESTORE_REQUIRED", restore)

    def test_parent_lock_release_defers_signals_until_complete_restoration(self) -> None:
        runner = RUNNER.read_text(encoding="utf-8")
        transaction_globals = runner[: runner.index("die() {")]
        self.assertIn("PARENT_LOCK_RELEASE_IN_PROGRESS=0", transaction_globals)
        self.assertIn("DEFERRED_SIGNAL=''", transaction_globals)
        release = runner[
            runner.index("release_parent_locks() {") : runner.index("reacquire_parent_locks_for_rollback() {")
        ]
        self.assertLess(release.index("PARENT_LOCK_RELEASE_IN_PROGRESS=1"), release.index('chown "$saved_uid:$saved_gid" "$path"'))
        self.assertLess(release.index('chmod "$saved_mode" "$path"'), release.rindex("PARENT_LOCK_RELEASE_IN_PROGRESS=0"))
        activation = runner[
            runner.index("activate_target_services_after_unlock() {") : runner.index("on_signal() {")
        ]
        self.assertLess(activation.index("release_parent_locks || return 1"), activation.index("resume_deferred_signal"))
        self.assertLess(activation.index("resume_deferred_signal"), activation.index("systemctl daemon-reload"))

    def test_success_commit_blocks_rollback_signals_only_after_terminal_persistence(self) -> None:
        runner = RUNNER.read_text(encoding="utf-8")
        commit = runner[
            runner.index("commit_success() {") : runner.index("cleanup() {")
        ]
        self.assertLess(commit.index("trap '' HUP INT TERM"), commit.index("COMMITTED=1"))
        self.assertLess(commit.index("COMMITTED=1"), commit.index("write_terminal 'status=success rollback=not-required'"))
        self.assertIn("COMMITTED=0", commit)
        self.assertIn("TERMINAL_WRITTEN=0", commit)
        self.assertIn("install_signal_traps", commit)
        self.assertIn("commit_success || return 1", runner)
        on_signal = runner[
            runner.index("on_signal() {") : runner.index("install_signal_traps() {")
        ]
        self.assertIn('[ "$COMMITTED" = 0 ] || return 0', on_signal)

    @unittest.skipUnless(
        os.name == "posix" and sys.platform.startswith("linux") and os.geteuid() == 0,
        "requires a root-owned Linux/POSIX fake-systemctl harness; Windows static checks are not runtime proof",
    )
    def test_root_runner_topology_and_vault_boundary_regressions(self) -> None:
        """Exercise state transitions with a fake systemctl, without real units."""
        source = RUNNER.read_text(encoding="utf-8")
        library, marker, _ = source.partition('[ "$(id -u)" = 0 ] ||')
        self.assertTrue(marker, "runner main guard was not found")
        with tempfile.TemporaryDirectory() as raw:
            harness_root = Path(raw)
            library_path = harness_root / "runner-library.sh"
            library_path.write_text(library, encoding="utf-8")
            harness = textwrap.dedent(
                r'''
                set -eu
                TEST_ROOT="$1"
                RUNNER_LIBRARY="$2"
                calls="$TEST_ROOT/calls"
                : > "$calls"

                unit_field() {
                    unit=$1
                    field=$2
                    case "$unit:$field" in
                        gp-control-plane-core.service:LoadState) printf '%s\n' "$CORE_LOAD" ;;
                        gp-control-plane-core.service:ActiveState) printf '%s\n' "$CORE_ACTIVE" ;;
                        gp-control-plane-core.service:FragmentPath) printf '%s\n' "$CORE_FRAGMENT" ;;
                        gp-control-plane-core.service:enabled) printf '%s\n' "$CORE_ENABLED" ;;
                        gp-control-plane-web.service:LoadState) printf '%s\n' "$WEB_LOAD" ;;
                        gp-control-plane-web.service:ActiveState) printf '%s\n' "$WEB_ACTIVE" ;;
                        gp-control-plane-web.service:FragmentPath) printf '%s\n' "$WEB_FRAGMENT" ;;
                        gp-control-plane-web.service:enabled) printf '%s\n' "$WEB_ENABLED" ;;
                        *) return 1 ;;
                    esac
                }

                set_active() {
                    case "$1" in
                        gp-control-plane-core.service) CORE_ACTIVE=$2 ;;
                        gp-control-plane-web.service) WEB_ACTIVE=$2 ;;
                        *) return 1 ;;
                    esac
                }

                set_enabled() {
                    case "$1" in
                        gp-control-plane-core.service) CORE_ENABLED=$2 ;;
                        gp-control-plane-web.service) WEB_ENABLED=$2 ;;
                        *) return 1 ;;
                    esac
                }

                systemctl() {
                    command=$1
                    shift
                    case "$command" in
                        show)
                            property=${1#--property=}
                            [ "$2" = --value ] || return 91
                            unit=$3
                            [ "${FAIL_PROPERTY_QUERY:-off}" = on ] && return 92
                            unit_field "$unit" "$property"
                            ;;
                        is-enabled) unit_field "$1" enabled ;;
                        stop)
                            unit=$1
                            printf 'stop %s\n' "$unit" >> "$calls"
                            [ "${FAIL_STOP_UNIT:-}" = "$unit" ] && return 96
                            case "$(unit_field "$unit" enabled)" in masked|masked-runtime) return 93 ;; esac
                            set_active "$unit" inactive
                            [ "${SIGNAL_AFTER_STOP_UNIT:-}" = "$unit" ] && on_signal TERM
                            ;;
                        start)
                            unit=$1
                            printf 'start %s\n' "$unit" >> "$calls"
                            case "$(unit_field "$unit" enabled)" in masked|masked-runtime) return 94 ;; esac
                            set_active "$unit" active
                            ;;
                        enable|disable|mask|unmask)
                            runtime=
                            case "${1:-}" in --runtime) runtime=-runtime; shift ;; esac
                            unit=$1
                            printf '%s%s %s\n' "$command" "$runtime" "$unit" >> "$calls"
                            case "$command$runtime" in
                                unmask) case "$(unit_field "$unit" enabled)" in masked) set_enabled "$unit" disabled ;; masked-runtime) set_enabled "$unit" disabled-runtime ;; esac ;;
                                disable) set_enabled "$unit" disabled ;;
                                disable-runtime) set_enabled "$unit" disabled-runtime ;;
                                enable) set_enabled "$unit" enabled ;;
                                enable-runtime) set_enabled "$unit" enabled-runtime ;;
                                mask) set_enabled "$unit" masked ;;
                                mask-runtime) set_enabled "$unit" masked-runtime ;;
                            esac
                            ;;
                        daemon-reload) printf 'daemon-reload\n' >> "$calls" ;;
                        *) return 95 ;;
                    esac
                }

                RELEASE_CHOWN_COUNT=0
                RELEASE_CHMOD_COUNT=0

                chown() {
                    command chown "$@" || return $?
                    [ "${PARENT_LOCK_RELEASE_IN_PROGRESS:-0}" = 1 ] || return 0
                    RELEASE_CHOWN_COUNT=$((RELEASE_CHOWN_COUNT + 1))
                    [ "${RELEASE_SIGNAL_POINT:-}" = "after-chown-$RELEASE_CHOWN_COUNT" ] && on_signal TERM
                }

                chmod() {
                    command chmod "$@" || return $?
                    [ "${PARENT_LOCK_RELEASE_IN_PROGRESS:-0}" = 1 ] || return 0
                    RELEASE_CHMOD_COUNT=$((RELEASE_CHMOD_COUNT + 1))
                    case "${RELEASE_SIGNAL_POINT:-}" in
                        "after-chmod-$RELEASE_CHMOD_COUNT") on_signal TERM ;;
                        between-parents) [ "$RELEASE_CHMOD_COUNT" = 1 ] && on_signal TERM ;;
                    esac
                }

                setup_case() {
                    name=$1
                    web_load=$2
                    web_active=$3
                    web_enabled=$4
                    web_fragment=$5
                    ROLLBACK_ROOT="$TEST_ROOT/$name/rollback"
                    mkdir -p "$ROLLBACK_ROOT"
                    : > "$calls"
                    CORE_LOAD=loaded
                    CORE_ACTIVE=active
                    CORE_ENABLED=enabled
                    CORE_FRAGMENT=/etc/systemd/system/gp-control-plane-core.service
                    WEB_LOAD=$web_load
                    WEB_ACTIVE=$web_active
                    WEB_ENABLED=$web_enabled
                    WEB_FRAGMENT=$web_fragment
                    snapshot_service_topology gp-control-plane-core.service core
                    snapshot_service_topology gp-control-plane-web.service web
                    quiesce_services
                    restore_service_topology gp-control-plane-core.service core
                    restore_service_topology gp-control-plane-web.service web
                }

                setup_case headless not-found inactive disabled ''
                ! grep -Fq 'gp-control-plane-web.service' "$calls"

                # A candidate can create Web even when the prior headless
                # topology had no Web unit; rollback must stop that active
                # current unit without treating the old absence as a stop.
                : > "$calls"
                WEB_LOAD=loaded
                WEB_ACTIVE=active
                WEB_ENABLED=enabled
                WEB_FRAGMENT=/etc/systemd/system/gp-control-plane-web.service
                quiesce_rollback_services
                grep -Fq 'stop gp-control-plane-web.service' "$calls"

                : > "$calls"
                WEB_LOAD=loaded
                WEB_ACTIVE=inactive
                WEB_ENABLED=masked
                WEB_FRAGMENT=/dev/null
                quiesce_rollback_services
                ! grep -Fq 'gp-control-plane-web.service' "$calls"

                setup_case disabled loaded inactive disabled /etc/systemd/system/gp-control-plane-web.service
                ! grep -Fq 'stop gp-control-plane-web.service' "$calls"
                ! grep -Fq 'start gp-control-plane-web.service' "$calls"
                grep -Fq 'disable gp-control-plane-web.service' "$calls"
                [ "$WEB_ENABLED" = disabled ] && [ "$WEB_ACTIVE" = inactive ]

                setup_case disabled-runtime loaded inactive disabled-runtime /run/systemd/system/gp-control-plane-web.service
                ! grep -Fq 'stop gp-control-plane-web.service' "$calls"
                ! grep -Fq 'start gp-control-plane-web.service' "$calls"
                grep -Fq 'disable-runtime gp-control-plane-web.service' "$calls"
                [ "$WEB_ENABLED" = disabled-runtime ] && [ "$WEB_ACTIVE" = inactive ]

                setup_case masked loaded inactive masked /dev/null
                ! grep -Fq 'stop gp-control-plane-web.service' "$calls"
                ! grep -Fq 'start gp-control-plane-web.service' "$calls"
                grep -Fq 'mask gp-control-plane-web.service' "$calls"
                [ "$WEB_ENABLED" = masked ] && [ "$WEB_ACTIVE" = inactive ]

                setup_case masked-runtime loaded inactive masked-runtime /dev/null
                ! grep -Fq 'stop gp-control-plane-web.service' "$calls"
                ! grep -Fq 'start gp-control-plane-web.service' "$calls"
                grep -Fq 'mask-runtime gp-control-plane-web.service' "$calls"
                [ "$WEB_ENABLED" = masked-runtime ] && [ "$WEB_ACTIVE" = inactive ]

                setup_case active-disabled loaded active disabled /etc/systemd/system/gp-control-plane-web.service
                grep -Fq 'stop gp-control-plane-web.service' "$calls"
                grep -Fq 'start gp-control-plane-web.service' "$calls"
                [ "$WEB_ENABLED" = disabled ] && [ "$WEB_ACTIVE" = active ]

                # Core stop failure after Web quiesce: rollback before the
                # first mv must restore every unit marked before its stop.
                # Starting an already-active Core is deliberately idempotent:
                # stop(1) could have partially succeeded before reporting an
                # error, so the transaction must never leave it down.
                ROLLBACK_ROOT="$TEST_ROOT/prepublish/rollback"
                TXN="$TEST_ROOT/prepublish"
                mkdir -p "$ROLLBACK_ROOT"
                : > "$calls"
                CORE_LOAD=loaded; CORE_ACTIVE=active; CORE_ENABLED=enabled; CORE_FRAGMENT=/etc/systemd/system/gp-control-plane-core.service
                WEB_LOAD=loaded; WEB_ACTIVE=active; WEB_ENABLED=enabled; WEB_FRAGMENT=/etc/systemd/system/gp-control-plane-web.service
                PUBLISHED=0; PARENT_LOCKS_HELD=0; WEB_QUIESCED=0; CORE_QUIESCED=0; WEB_RESTORE_REQUIRED=0; CORE_RESTORE_REQUIRED=0
                snapshot_service_topology gp-control-plane-core.service core
                snapshot_service_topology gp-control-plane-web.service web
                FAIL_STOP_UNIT=gp-control-plane-core.service
                ! quiesce_services
                unset FAIL_STOP_UNIT
                [ "$WEB_QUIESCED" = 1 ] && [ "$CORE_QUIESCED" = 0 ]
                rollback
                grep -Fq 'start gp-control-plane-web.service' "$calls"
                grep -Fq 'start gp-control-plane-core.service' "$calls"

                # TERM in the same pre-publication window uses the same
                # rollback path and must not falsely report success.
                ROLLBACK_ROOT="$TEST_ROOT/signal/rollback"
                TXN="$TEST_ROOT/signal"
                mkdir -p "$ROLLBACK_ROOT" "$TXN"
                : > "$calls"
                CORE_LOAD=loaded; CORE_ACTIVE=inactive; CORE_ENABLED=enabled; CORE_FRAGMENT=/etc/systemd/system/gp-control-plane-core.service
                WEB_LOAD=loaded; WEB_ACTIVE=active; WEB_ENABLED=enabled; WEB_FRAGMENT=/etc/systemd/system/gp-control-plane-web.service
                PUBLISHED=0; PARENT_LOCKS_HELD=0; WEB_QUIESCED=0; CORE_QUIESCED=0; WEB_RESTORE_REQUIRED=0; CORE_RESTORE_REQUIRED=0; TERMINAL_WRITTEN=0
                snapshot_service_topology gp-control-plane-core.service core
                snapshot_service_topology gp-control-plane-web.service web
                quiesce_services
                ( on_signal TERM ) || [ "$?" = 126 ]
                grep -Fq 'start gp-control-plane-web.service' "$calls"
                grep -Fq 'status=failed rollback=completed signal=TERM' "$TXN/result"

                # TERM immediately after successful stop, before the caller
                # can set WEB_QUIESCED or query the post-stop state, must
                # still restore the originally active Web service.
                ROLLBACK_ROOT="$TEST_ROOT/signal-after-stop/rollback"
                TXN="$TEST_ROOT/signal-after-stop"
                mkdir -p "$ROLLBACK_ROOT" "$TXN"
                : > "$calls"
                CORE_LOAD=loaded; CORE_ACTIVE=inactive; CORE_ENABLED=enabled; CORE_FRAGMENT=/etc/systemd/system/gp-control-plane-core.service
                WEB_LOAD=loaded; WEB_ACTIVE=active; WEB_ENABLED=enabled; WEB_FRAGMENT=/etc/systemd/system/gp-control-plane-web.service
                PUBLISHED=0; PARENT_LOCKS_HELD=0; WEB_QUIESCED=0; CORE_QUIESCED=0; WEB_RESTORE_REQUIRED=0; CORE_RESTORE_REQUIRED=0; TERMINAL_WRITTEN=0
                snapshot_service_topology gp-control-plane-core.service core
                snapshot_service_topology gp-control-plane-web.service web
                SIGNAL_AFTER_STOP_UNIT=gp-control-plane-web.service
                ( quiesce_services ) || [ "$?" = 126 ]
                unset SIGNAL_AFTER_STOP_UNIT
                grep -Fq 'stop gp-control-plane-web.service' "$calls"
                grep -Fq 'start gp-control-plane-web.service' "$calls"
                [ "$WEB_QUIESCED" = 0 ]
                grep -Fq 'status=failed rollback=completed signal=TERM' "$TXN/result"

                # Source a pristine runner library for each inactive/headless
                # pre-publication terminal path.  In particular, do not
                # assign either *_RESTORE_REQUIRED flag here: under set -u
                # the runner's transaction globals must provide both values.
                fresh_prepublication_case() (
                    case_name=$1
                    web_load=$2
                    terminal=$3
                    . "$RUNNER_LIBRARY"
                    ROLLBACK_ROOT="$TEST_ROOT/$case_name/rollback"
                    TXN="$TEST_ROOT/$case_name"
                    mkdir -p "$ROLLBACK_ROOT" "$TXN"
                    : > "$calls"
                    CORE_LOAD=loaded; CORE_ACTIVE=inactive; CORE_ENABLED=disabled; CORE_FRAGMENT=/etc/systemd/system/gp-control-plane-core.service
                    WEB_LOAD=$web_load; WEB_ACTIVE=inactive; WEB_ENABLED=disabled; WEB_FRAGMENT=/etc/systemd/system/gp-control-plane-web.service
                    PUBLISHED=0; PARENT_LOCKS_HELD=0
                    snapshot_service_topology gp-control-plane-core.service core
                    snapshot_service_topology gp-control-plane-web.service web
                    [ "$CORE_RESTORE_REQUIRED" = 0 ] && [ "$WEB_RESTORE_REQUIRED" = 0 ]
                    case "$terminal" in
                        error) rollback ;;
                        signal) ( on_signal TERM ) || [ "$?" = 126 ] ;;
                        *) exit 98 ;;
                    esac
                    [ "$CORE_ACTIVE" = inactive ]
                    [ "$WEB_ACTIVE" = inactive ]
                    ! grep -Fq 'start gp-control-plane-core.service' "$calls"
                    ! grep -Fq 'start gp-control-plane-web.service' "$calls"
                    if [ "$terminal" = signal ]; then
                        grep -Fq 'status=failed rollback=completed signal=TERM' "$TXN/result"
                    fi
                )

                fresh_prepublication_case inactive-error loaded error
                fresh_prepublication_case headless-signal not-found signal

                # Signal injection after every parent-release mutation and
                # between the parents.  The trap must defer, finish restoring
                # both original owner/mode tuples, restore active topology,
                # and write a signal-qualified completed terminal only then.
                release_signal_case() (
                    point=$1
                    . "$RUNNER_LIBRARY"
                    case_root="$TEST_ROOT/release-$point"
                    release_home="$case_root/home"
                    release_parent="$release_home/gp"
                    mkdir -p "$release_parent"
                    release_uid="$(id -u nobody)"; release_gid="$(id -g nobody)"
                    chown "$release_uid:$release_gid" "$release_home" "$release_parent"
                    chmod 0700 "$release_home"
                    chmod 0750 "$release_parent"
                    ROLLBACK_ROOT="$case_root/rollback"
                    TXN="$case_root/txn"
                    mkdir -p "$ROLLBACK_ROOT" "$TXN"
                    PARENT_LOCKS_FILE="$TXN/parent-locks.records"
                    : > "$PARENT_LOCKS_FILE"; chmod 0600 "$PARENT_LOCKS_FILE"
                    PARENT_LOCKS_HELD=1
                    record_and_lock_parent "$release_home" "$release_uid"
                    record_and_lock_parent "$release_parent" "$release_uid"
                    CORE_LOAD=loaded; CORE_ACTIVE=active; CORE_ENABLED=enabled; CORE_FRAGMENT=/etc/systemd/system/gp-control-plane-core.service
                    WEB_LOAD=loaded; WEB_ACTIVE=active; WEB_ENABLED=enabled; WEB_FRAGMENT=/etc/systemd/system/gp-control-plane-web.service
                    snapshot_service_topology gp-control-plane-core.service core
                    snapshot_service_topology gp-control-plane-web.service web
                    CORE_RESTORE_REQUIRED=1; WEB_RESTORE_REQUIRED=1
                    PUBLISHED=0; TERMINAL_WRITTEN=0
                    : > "$calls"
                    RELEASE_CHOWN_COUNT=0; RELEASE_CHMOD_COUNT=0; RELEASE_SIGNAL_POINT=$point
                    ( rollback ) || [ "$?" = 126 ]
                    unset RELEASE_SIGNAL_POINT
                    [ "$(stat -c '%u:%g:%a' "$release_home")" = "$release_uid:$release_gid:700" ]
                    [ "$(stat -c '%u:%g:%a' "$release_parent")" = "$release_uid:$release_gid:750" ]
                    grep -Fq 'start gp-control-plane-core.service' "$calls"
                    grep -Fq 'start gp-control-plane-web.service' "$calls"
                    grep -Fq 'status=failed rollback=completed signal=TERM' "$TXN/result"
                )

                for release_point in after-chown-1 after-chmod-1 between-parents after-chown-2 after-chmod-2; do
                    release_signal_case "$release_point"
                done

                # The same deferred path in normal activation must run before
                # daemon-reload/start.  Model a published candidate in the
                # temporary tree; root-surface restore is isolated here, while
                # parent re-acquisition, managed-directory rollback, vault
                # boundaries, service topology, and terminal are real runner
                # control flow.
                activation_release_signal_case() (
                    . "$RUNNER_LIBRARY"
                    case_root="$TEST_ROOT/activation-release-signal"
                    activation_home="$case_root/home"
                    activation_parent="$activation_home/gp"
                    activation_install="$activation_parent/GP-access-control-plane"
                    activation_state="$activation_install/state"
                    activation_vault="$activation_home/.local/share/gp-control-plane/clean-install-vault"
                    activation_uid="$(id -u nobody)"; activation_gid="$(id -g nobody)"
                    mkdir -p "$activation_state" "$activation_vault" "$case_root/rollback/install-dir"
                    printf 'candidate' > "$activation_install/candidate"
                    printf 'original' > "$case_root/rollback/install-dir/original"
                    printf 'vault' > "$activation_vault/anchor"
                    : > "$activation_vault/archive.zip"; : > "$activation_vault/entry.json"
                    chown -R "$activation_uid:$activation_gid" "$activation_home" "$case_root/rollback/install-dir"
                    chmod 0700 "$activation_home" "$activation_parent" "$activation_install" "$activation_state" "$activation_home/.local" "$activation_home/.local/share" "$activation_home/.local/share/gp-control-plane" "$activation_vault"
                    chmod 0600 "$activation_vault/archive.zip" "$activation_vault/entry.json"
                    ROLLBACK_ROOT="$case_root/rollback"
                    TXN="$case_root/txn"
                    mkdir -p "$TXN"
                    PARENT_LOCKS_FILE="$TXN/parent-locks.records"
                    : > "$PARENT_LOCKS_FILE"; chmod 0600 "$PARENT_LOCKS_FILE"
                    PARENT_LOCKS_HELD=1
                    record_and_lock_parent "$activation_home" "$activation_uid"
                    record_and_lock_parent "$activation_parent" "$activation_uid"
                    INSTALL_USER=nobody
                    INSTALL_DIR="$activation_install"
                    STATE_DIR="$activation_state"
                    VAULT_DIR="$activation_vault"
                    STATE_SEPARATE=0
                    PUBLISHED=1; TERMINAL_WRITTEN=0
                    CORE_LOAD=loaded; CORE_ACTIVE=active; CORE_ENABLED=enabled; CORE_FRAGMENT=/etc/systemd/system/gp-control-plane-core.service
                    WEB_LOAD=loaded; WEB_ACTIVE=active; WEB_ENABLED=enabled; WEB_FRAGMENT=/etc/systemd/system/gp-control-plane-web.service
                    snapshot_service_topology gp-control-plane-core.service core
                    snapshot_service_topology gp-control-plane-web.service web
                    getent() {
                        if [ "$1" = passwd ] && [ "$2" = nobody ]; then
                            printf 'nobody:x:%s:%s::%s:/usr/sbin/nologin\n' "$activation_uid" "$activation_gid" "$activation_home"
                        else
                            command getent "$@"
                        fi
                    }
                    restore_root_surface() { :; }
                    : > "$calls"
                    RELEASE_CHOWN_COUNT=0; RELEASE_CHMOD_COUNT=0; RELEASE_SIGNAL_POINT=after-chmod-2
                    ( activate_target_services_after_unlock ) || [ "$?" = 126 ]
                    unset RELEASE_SIGNAL_POINT
                    [ "$(cat "$activation_install/original")" = original ]
                    [ ! -e "$activation_install/candidate" ]
                    [ "$(cat "$activation_vault/anchor")" = vault ]
                    [ "$(stat -c '%u:%g:%a' "$activation_home")" = "$activation_uid:$activation_gid:700" ]
                    [ "$(stat -c '%u:%g:%a' "$activation_parent")" = "$activation_uid:$activation_gid:700" ]
                    grep -Fq 'start gp-control-plane-core.service' "$calls"
                    grep -Fq 'start gp-control-plane-web.service' "$calls"
                    ! grep -Fq 'daemon-reload' "$calls"
                    grep -Fq 'status=failed rollback=completed signal=TERM' "$TXN/result"
                )

                activation_release_signal_case

                # The signal is sent by the printf wrapper immediately after
                # the real write_terminal payload reaches $TXN/result.  The
                # commit trap barrier must ignore it: success remains the
                # only terminal, no rollback runs, and the shell returns 0.
                success_terminal_signal_case() (
                    signal_name=$1
                    . "$RUNNER_LIBRARY"
                    case_root="$TEST_ROOT/success-terminal-$signal_name"
                    TXN="$case_root/txn"
                    candidate="$case_root/candidate"
                    mkdir -p "$TXN" "$candidate"
                    printf 'candidate-bytes' > "$candidate/anchor"
                    TERMINAL_INJECTION_SIGNAL=$signal_name
                    TERMINAL_INJECTION_ARMED=1
                    printf() {
                        command printf "$@" || return $?
                        if [ "$TERMINAL_INJECTION_ARMED" = 1 ] && [ "${2:-}" = 'status=success rollback=not-required' ]; then
                            TERMINAL_INJECTION_ARMED=0
                            kill -"$TERMINAL_INJECTION_SIGNAL" "$$"
                        fi
                        return 0
                    }
                    install_signal_traps
                    commit_success > "$case_root/output"
                    [ "$COMMITTED" = 1 ]
                    [ "$(cat "$TXN/result")" = 'status=success rollback=not-required' ]
                    [ "$(cat "$candidate/anchor")" = candidate-bytes ]
                    ! grep -Fq 'rollback=completed signal=' "$case_root/output"
                    ! grep -Fq 'status=failed' "$case_root/output"
                    grep -Fxq 'status=success rollback=not-required' "$case_root/output"
                    on_signal TERM
                    [ "$(cat "$candidate/anchor")" = candidate-bytes ]
                )

                for success_signal in TERM HUP INT; do
                    success_terminal_signal_case "$success_signal"
                done

                FAIL_PROPERTY_QUERY=on
                ROLLBACK_ROOT="$TEST_ROOT/query-failure"
                mkdir -p "$ROLLBACK_ROOT"
                ! snapshot_service_topology gp-control-plane-web.service web
                [ ! -e "$ROLLBACK_ROOT/web.service-state" ]
                unset FAIL_PROPERTY_QUERY

                vault_base="$TEST_ROOT/home/.local/share/gp-control-plane"
                vault="$vault_base/clean-install-vault"
                install_dir="$TEST_ROOT/home/gp/GP-access-control-plane"
                state_dir="$TEST_ROOT/home/gp/.GP-access-control-plane.data/state"
                mkdir -p "$vault" "$install_dir" "$state_dir"
                chmod 700 "$TEST_ROOT/home" "$TEST_ROOT/home/.local" "$TEST_ROOT/home/.local/share" "$vault_base" "$vault" "$TEST_ROOT/home/gp" "$install_dir" "$TEST_ROOT/home/gp/.GP-access-control-plane.data" "$state_dir"
                INSTALL_USER="$(id -un)"
                VAULT_DIR="$vault"
                INSTALL_DIR="$install_dir"
                STATE_DIR="$state_dir"
                validate_transaction_boundaries

                overlap="$vault/unsafe-state"
                mkdir -p "$overlap"
                chmod 700 "$overlap"
                STATE_DIR="$overlap"
                ! validate_transaction_boundaries
                [ -d "$vault" ]

                STATE_DIR="$state_dir"
                mv "$install_dir" "$install_dir.real"
                ln -s "$install_dir.real" "$install_dir"
                ! validate_transaction_boundaries
                [ -d "$vault" ]

                # A real non-root install user keeps search/write access to
                # its child, but cannot rename through a root:root 0711 home.
                # This is the race boundary used by destructive root paths.
                command -v runuser >/dev/null 2>&1 || exit 0
                race_root="$TEST_ROOT/race"
                race_home="$race_root/home"
                race_parent="$race_home/gp"
                race_install="$race_parent/GP-access-control-plane"
                race_vault="$race_home/.local/share/gp-control-plane/clean-install-vault"
                race_uid="$(id -u nobody)"; race_gid="$(id -g nobody)"
                mkdir -p "$race_install" "$race_vault"
                printf 'vault-bytes' > "$race_vault/anchor"
                chown -R "$race_uid:$race_gid" "$race_home"
                chmod 700 "$race_home" "$race_parent" "$race_install" "$race_home/.local" "$race_home/.local/share" "$race_home/.local/share/gp-control-plane" "$race_vault"
                PARENT_LOCKS_FILE="$race_root/locks"; : > "$PARENT_LOCKS_FILE"; chmod 600 "$PARENT_LOCKS_FILE"; PARENT_LOCKS_HELD=1
                record_and_lock_parent "$race_home" "$race_uid"
                record_and_lock_parent "$race_parent" "$race_uid"
                [ "$(stat -c '%u:%g:%a' "$race_home")" = '0:0:711' ]
                runuser -u nobody -- /bin/sh -c 'touch "$1/probe"' sh "$race_install"
                if runuser -u nobody -- /bin/sh -c 'mv "$1" "$2"' sh "$race_install" "$race_vault"; then exit 97; fi
                [ "$(cat "$race_vault/anchor")" = vault-bytes ]
                [ "$(stat -c '%a' "$race_vault")" = 700 ]
                release_parent_locks
                [ "$(stat -c '%u:%g' "$race_home")" = "$race_uid:$race_gid" ]
                '''
            )
            completed = subprocess.run(
                ["/bin/sh", "-s", str(harness_root), str(library_path)],
                input=library + "\n" + harness,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "requires an unprivileged POSIX caller")
    def test_malformed_dispatch_is_rejected_without_creating_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            transaction_root = Path(raw) / "transactions"
            result = subprocess.run(
                ["/bin/sh", str(HELPER), "clean-install", "--bad"],
                text=True,
                capture_output=True,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "GP_ROOT_HELPER_CONFIG": "/nonexistent"},
                check=False,
            )
            self.assertEqual(result.returncode, 126)
            self.assertIn("must be executed as root", result.stderr)
            self.assertFalse(transaction_root.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits are not meaningful in a Windows checkout")
    def test_scripts_are_shell_readable_and_not_world_writable_in_checkout(self) -> None:
        for script in (HELPER, RUNNER):
            self.assertTrue(script.is_file())
            self.assertFalse(stat.S_IMODE(script.stat().st_mode) & stat.S_IWOTH)


if __name__ == "__main__":
    unittest.main()
