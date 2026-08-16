from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY / "scripts" / "release-gates" / "pi5-gate.sh"


def bash_executable() -> str | None:
    return next(
        (candidate for candidate in (shutil.which("bash"), r"C:\\Program Files\\Git\\bin\\bash.exe") if candidate and Path(candidate).is_file()),
        None,
    )


def shell_function(source: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n(?P<body>.*?)(?=^\}}$)", source, re.MULTILINE | re.DOTALL)
    if not match:
        raise AssertionError(f"Bash function {name} was not found")
    return match.group("body")


def posix_shell_path(path: Path) -> str:
    value = str(path)
    if os.name != "nt":
        return value
    drive, tail = os.path.splitdrive(value)
    if drive:
        posix_tail = tail.replace("\\", "/")
        return f"/{drive[0].lower()}{posix_tail}"
    return value.replace("\\", "/")


class Pi5GateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")
        cls.bash = bash_executable()

    def run_gate(self, *args: str) -> subprocess.CompletedProcess[str]:
        if not self.bash:
            self.skipTest("real Bash is unavailable")
        return subprocess.run([self.bash, str(SCRIPT), *args], cwd=REPOSITORY, text=True, capture_output=True, check=False)

    def run_profile_reader(self, profile: Path) -> subprocess.CompletedProcess[str]:
        if not self.bash:
            self.skipTest("real Bash is unavailable")
        reader = shell_function(self.source, "read_trusted_profile_state_dir")
        harness = f'''set -o pipefail
read_trusted_profile_state_dir() {{
{reader}
}}
INSTALL_PROFILE="$1"
stat() {{ printf '0:0:600\\n'; }}
read_trusted_profile_state_dir
        '''
        return subprocess.run([self.bash, "-c", harness, "profile-reader", str(profile)], text=True, capture_output=True, check=False)

    def run_update_success_validation(
        self, log: Path, candidate_ref: str = "refs/tags/v0.4.0", expected_sha: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        if not self.bash:
            self.skipTest("real Bash is unavailable")
        validation = shell_function(self.source, "validate_update_success_evidence")
        expected_sha = expected_sha or "a" * 40
        harness = f'''set -o pipefail
require_trusted_root_dir() {{ return 0; }}
stat() {{ printf '0:0:600\\n'; }}
UPDATE_LOG_PARENT="$1"
CANDIDATE_REF="$2"
EXPECTED_SHA="$3"
PYTHON="$4"
validate_update_success_evidence() {{
{validation}
}}
validate_update_success_evidence "$5"
'''
        return subprocess.run(
            [
                self.bash,
                "-c",
                harness,
                "update-success-validation",
                posix_shell_path(log.parent),
                candidate_ref,
                expected_sha,
                posix_shell_path(Path(sys.executable)),
                posix_shell_path(log),
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def run_installed_identity_check(
        self, *, head: str, attached: bool, status: str, require_detached: bool
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        if not self.bash:
            self.skipTest("real Bash is unavailable")
        check = shell_function(self.source, "check_installed_ref")
        with tempfile.TemporaryDirectory() as raw:
            temporary = Path(raw)
            git_log = temporary / "git.log"
            environment = os.environ | {
                "GIT_LOG": posix_shell_path(git_log),
                "FAKE_HEAD": head,
                "FAKE_ATTACHED": "1" if attached else "0",
                "FAKE_STATUS": status,
            }
            harness = f'''set -o pipefail
INSTALL_DIR="/installed"
EXPECTED_SHA="{'a' * 40}"
git() {{
  printf '%s\\n' "$*" >> "$GIT_LOG"
  if [ "$1" = -C ]; then shift 2; fi
  case "$1" in
    rev-parse) printf '%s\\n' "$FAKE_HEAD" ;;
    symbolic-ref) [ "$FAKE_ATTACHED" = 1 ] && {{ printf '%s\\n' refs/heads/dev; return 0; }}; return 1 ;;
    status) printf '%s' "$FAKE_STATUS" ;;
    *) printf 'unexpected fake git command: %s\\n' "$*" >&2; return 99 ;;
  esac
}}
check_installed_ref() {{
{check}
}}
check_installed_ref "$1"
'''
            result = subprocess.run(
                [self.bash, "-c", harness, "installed-identity", "1" if require_detached else "0"],
                text=True,
                capture_output=True,
                env=environment,
                check=False,
            )
            return result, git_log.read_text(encoding="utf-8").splitlines()

    def run_dirty_update_queue(self, expected_sha: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        if not self.bash:
            self.skipTest("real Bash is unavailable")
        queue = shell_function(self.source, "queue_dirty_update")
        with tempfile.TemporaryDirectory() as raw:
            temporary = Path(raw)
            install_dir = temporary / "install"
            install_dir.mkdir()
            order_log = temporary / "order.log"
            helper = temporary / "root-helper"
            helper.write_text(
                """#!/usr/bin/env bash
printf 'helper:%s\\n' "$*" >> "$ORDER_LOG"
printf '%s\\n' 'queued=true' 'status=queued' 'phase=queued' 'candidate_ref=refs/heads/dev' "expected_sha=$EXPECTED_SHA" 'unit=gp-control-plane-update-20260816T120000Z-1' 'log=/var/lib/gp-control-plane/release-updates/gp-control-plane-update-20260816T120000Z-1.log'
""",
                encoding="utf-8",
            )
            helper.chmod(0o755)
            environment = os.environ | {
                "ORDER_LOG": posix_shell_path(order_log),
                "EXPECTED_SHA": expected_sha,
            }
            harness = f'''set -o pipefail
INSTALL_DIR="$1"
APP_USER=gate
RUN_STAMP=test
CANDIDATE_REF=refs/heads/dev
EXPECTED_SHA="$2"
ROOT_HELPER="$3"
DIRTY_MARKER=""
VERIFIED_SHA="" STAGED_SHA="" INSTALLED_SHA="" TERMINAL_STATUS=""
git() {{
  printf 'git:%s\\n' "$*" >> "$ORDER_LOG"
  if [ "$1" = -C ]; then shift 2; fi
  [ "$1" = status ] || return 99
}}
validate_queue_evidence() {{ printf '%s\\t%s\\n' unit log; }}
validate_update_success_evidence() {{ printf '%s\\t%s\\t%s\\n' "$EXPECTED_SHA" "$EXPECTED_SHA" "$EXPECTED_SHA"; }}
grep() {{ rm -f -- "$DIRTY_MARKER"; return 0; }}
date() {{ printf '0\\n'; }}
runuser() {{ shift 3; "$@"; }}
queue_dirty_update() {{
{queue}
}}
queue_dirty_update
'''
            result = subprocess.run(
                [
                    self.bash,
                    "-c",
                    harness,
                    "dirty-update-queue",
                    posix_shell_path(install_dir),
                    expected_sha,
                    posix_shell_path(helper),
                ],
                text=True,
                capture_output=True,
                env=environment,
                check=False,
            )
            return result, order_log.read_text(encoding="utf-8").splitlines()

    def run_stop_cycle_harness(self, *, history_status: str, timeout: bool) -> subprocess.CompletedProcess[str]:
        if not self.bash:
            self.skipTest("real Bash is unavailable")
        functions = {
            name: shell_function(self.source, name)
            for name in ("current_run_id", "current_run_status", "history_run_status", "run_is_stopped", "start_and_stop_cycle")
        }
        with tempfile.TemporaryDirectory() as raw:
            clock = Path(raw) / "clock"
            clock.write_text("0\n", encoding="utf-8")
            history = "stopping" if timeout else history_status
            harness = f'''\
PYTHON="$1"
POLL_TIMEOUT_SECONDS=10
API_URL="http://gate.test"
TEST_DOMAIN="example.com"
CLOCK="$2"
current_run_id() {{
{functions["current_run_id"]}
}}
current_run_status() {{
{functions["current_run_status"]}
}}
history_run_status() {{
{functions["history_run_status"]}
}}
run_is_stopped() {{
{functions["run_is_stopped"]}
}}
api_post() {{
  case "$2" in
    */start-run) printf '%s\\n' '{{"run_id":"run-1","status":"queued"}}' ;;
    */stop-current-run) printf '%s\\n' '{{"run_id":"run-1","status":"stopping"}}' ;;
  esac
}}
api_get() {{
  case "$2" in
    */current-run-progress) printf '%s\\n' '{{"run_id":"run-1","status":"stopping"}}' ;;
    */runs/history*) printf '%s\\n' '{{"runs":[{{"run_id":"run-1","status":"{history}"}}]}}' ;;
  esac
}}
json_assert() {{ return 0; }}
inspect_leftovers() {{ return 0; }}
cycle_error() {{ printf 'cycle-error stage=%s code=%s\\n' "$2" "$3" >&2; return "$3"; }}
sleep() {{ printf 'sleep-called\\n' >&2; return 0; }}
date() {{
  value="$(cat "$CLOCK")"
  case "$value" in
    0) printf '1\\n' > "$CLOCK"; printf '0\\n' ;;
    1) printf '2\\n' > "$CLOCK"; printf '0\\n' ;;
    *) printf '3\\n' > "$CLOCK"; printf '11\\n' ;;
  esac
}}
start_and_stop_cycle() {{
{functions["start_and_stop_cycle"]}
}}
start_and_stop_cycle standard
'''
            return subprocess.run(
                [self.bash, "-c", harness, "stop-cycle-harness", posix_shell_path(Path(sys.executable)), posix_shell_path(clock)],
                text=True,
                capture_output=True,
                check=False,
            )

    def test_syntax_help_and_required_arguments_fail_before_hardware_access(self) -> None:
        if not self.bash:
            self.skipTest("real Bash is unavailable")
        syntax = subprocess.run([self.bash, "-n", str(SCRIPT)], text=True, capture_output=True, check=False)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        self.assertEqual(self.run_gate("--help").returncode, 0)
        cases = (
            (("--unknown",), "unknown argument: --unknown"),
            (("--ref",), "missing value for --ref"),
            (("--ref", "v0.4.0"), "--topology is required"),
            (("--ref", "v0.4.0", "--topology", "invalid"), "--topology must be web or headless"),
            (("--ref", "v0.4.0", "--topology", "web", "--mode", "clean-install"), "requires --ack-clean-install"),
            (("--ref", "v0.4.0", "--topology", "web", "--ack-clean-install"), "valid only with --mode clean-install"),
            (("--ref", "v0.4.0", "--topology", "web", "--poll-timeout", "9"), "--poll-timeout must be 10..900"),
        )
        for args, message in cases:
            with self.subTest(args=args):
                result = self.run_gate(*args)
                self.assertEqual(result.returncode, 2)
                self.assertIn(message, result.stderr)

    def test_state_dir_uses_explicit_override_or_a_trusted_install_profile(self) -> None:
        resolve = shell_function(self.source, "resolve_state_dir")
        profile = shell_function(self.source, "read_trusted_profile_state_dir")

        self.assertIn('if [ -n "$STATE_DIR" ]; then', resolve)
        self.assertLess(resolve.index('if [ -n "$STATE_DIR" ]; then'), resolve.index('read_trusted_profile_state_dir'))
        self.assertIn('readonly INSTALL_PROFILE="/etc/default/gp-control-plane-install-profile"', self.source)
        self.assertIn('STATE_DIR="$(canonical_existing_dir "$profile_state_dir")"', resolve)
        self.assertIn("[ -f \"$INSTALL_PROFILE\" ] && [ ! -L \"$INSTALL_PROFILE\" ]", profile)
        self.assertIn("stat -c '%u:%g:%a' \"$INSTALL_PROFILE\"", profile)
        self.assertIn("= '0:0:600'", profile)
        self.assertIn('key == "GP_STATE_DIR"', profile)
        self.assertNotIn("source \"$INSTALL_PROFILE\"", self.source)
        self.assertNotIn("eval \"$INSTALL_PROFILE\"", self.source)

    def test_pre_tag_cli_only_accepts_origin_dev_and_a_lowercase_pinned_sha(self) -> None:
        sha = "a" * 40
        cases = (
            (("--topology", "web"), "--ref or --candidate is required"),
            (("--candidate", "dev", "--expected-sha", sha, "--topology", "web"), "--candidate must be the canonical origin/dev"),
            (("--candidate", "origin/dev", "--topology", "web"), "--expected-sha must be 40 lowercase hexadecimal characters"),
            (("--candidate", "origin/dev", "--expected-sha", "A" * 40, "--topology", "web"), "--expected-sha must be 40 lowercase hexadecimal characters"),
            (("--ref", "v0.4.0", "--candidate", "origin/dev", "--expected-sha", sha, "--topology", "web"), "mutually exclusive"),
            (("--ref", "v0.4.0", "--expected-sha", sha, "--topology", "web"), "valid only with --candidate origin/dev"),
        )
        for args, message in cases:
            with self.subTest(args=args):
                result = self.run_gate(*args)
                self.assertEqual(result.returncode, 2)
                self.assertIn(message, result.stderr)

        self.assertIn('CANDIDATE_REF="refs/heads/dev"', self.source)
        self.assertIn('CANDIDATE_SHA="$EXPECTED_SHA"', shell_function(self.source, "resolve_pre_tag_candidate"))

    def test_pre_tag_dirty_update_queues_before_post_update_detached_identity_check(self) -> None:
        sha = "a" * 40
        result, order = self.run_dirty_update_queue(sha)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(order[0], r"^git:-C .+ status --porcelain$")
        self.assertEqual(
            order[1],
            f"helper:queue-update --candidate-ref refs/heads/dev --expected-sha {sha}",
        )
        self.assertNotIn("tag", "\n".join(order).lower())

        main = self.source.split('report_event metadata pi5-gate started', 1)[1]
        self.assertLess(main.index("queue_dirty_update"), main.index("check_installed_ref 1"))
        self.assertIn('[ "$MODE" = dirty-update ] || require_step installed-ref-before check_installed_ref', main)

    def test_post_update_requires_detached_expected_head_and_clean_worktree_without_tag_commands(self) -> None:
        sha = "a" * 40
        success, calls = self.run_installed_identity_check(head=sha, attached=False, status="", require_detached=True)
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertEqual([call.split()[-1] for call in calls], ["HEAD", "HEAD", "--porcelain"])
        self.assertTrue(any("symbolic-ref -q HEAD" in call for call in calls))
        self.assertNotIn("tag", "\n".join(calls).lower())

        attached, _ = self.run_installed_identity_check(head=sha, attached=True, status="", require_detached=True)
        self.assertNotEqual(attached.returncode, 0)
        self.assertIn("not detached", attached.stderr)

        dirty, _ = self.run_installed_identity_check(head=sha, attached=False, status=" M tracked", require_detached=True)
        self.assertNotEqual(dirty.returncode, 0)
        self.assertIn("local changes", dirty.stderr)

        mismatch, _ = self.run_installed_identity_check(head="b" * 40, attached=False, status="", require_detached=True)
        self.assertNotEqual(mismatch.returncode, 0)
        self.assertIn("does not match expected candidate", mismatch.stderr)

    def test_pre_tag_remote_sha_mismatch_and_terminal_evidence_are_rejected_or_recorded(self) -> None:
        sha = "a" * 40
        common = "\n".join(
            (
                "candidate_ref=refs/heads/dev",
                f"expected_sha={sha}",
                "verified_ref=refs/heads/dev",
                f"verified_sha={'b' * 40}",
                f"staged_sha={sha}",
                "phase=requested",
                "phase=verified",
                "phase=staged",
                "phase=published",
                "phase=root",
                "phase=committed",
                "phase=installed",
                "installed_ref=refs/heads/dev",
                f"installed_sha={sha}",
                "cleanup_status=completed",
                "status=success",
            )
        )
        with tempfile.TemporaryDirectory() as raw:
            log = Path(raw) / "remote-sha-mismatch.log"
            log.write_text(common + "\n", encoding="utf-8")
            result = self.run_update_success_validation(log, "refs/heads/dev", sha)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("incomplete", result.stderr)

        for field in ("candidate_sha", "expected_sha", "verified_sha", "staged_sha", "installed_sha", "terminal_status"):
            self.assertIn(f'"{field}"', self.source)

    def test_state_dir_refuses_unsafe_or_missing_profile_for_v0_4_and_newer(self) -> None:
        resolve = shell_function(self.source, "resolve_state_dir")
        profile = shell_function(self.source, "read_trusted_profile_state_dir")

        self.assertIn('return 3', profile)
        self.assertIn('regular non-symlink file', profile)
        self.assertIn('cannot safely read GP_STATE_DIR from installation profile', resolve)
        self.assertIn('installation profile is required to derive --state-dir for $REF', resolve)
        self.assertIn('v0.[0-3].*', shell_function(self.source, "legacy_state_fallback_allowed"))
        self.assertNotIn('STATE_DIR="$(canonical_existing_dir "${STATE_DIR:-$INSTALL_DIR/build/state}")"', self.source)

    def test_dirty_update_reloads_state_dir_after_a_possible_migration(self) -> None:
        reload = shell_function(self.source, "reload_state_dir_from_install_profile")
        main = self.source.split('report_event metadata pi5-gate started', 1)[1]

        self.assertIn('STATE_DIR=""', reload)
        self.assertIn('resolve_state_dir', reload)
        self.assertLess(main.index('queue_dirty_update'), main.index('reload_state_dir_from_install_profile'))
        self.assertLess(main.index('reload_state_dir_from_install_profile'), main.index('check_storage_integrity'))

    def test_profile_reader_derives_trusted_state_and_refuses_malformed_or_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            trusted = temporary / "profile"
            trusted.write_text("GP_STATE_DIR='/srv/gp/state'\n", encoding="utf-8")
            result = self.run_profile_reader(trusted)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "/srv/gp/state\n")

            malformed = temporary / "malformed"
            malformed.write_text("GP_STATE_DIR=$(not-safe)\n", encoding="utf-8")
            self.assertNotEqual(self.run_profile_reader(malformed).returncode, 0)

            missing = self.run_profile_reader(temporary / "missing")
            self.assertEqual(missing.returncode, 3)

    def test_clean_install_is_acknowledged_observer_only(self) -> None:
        self.assertIn("--ack-clean-install", self.source)
        self.assertIn('die "--mode clean-install requires --ack-clean-install"', self.source)
        self.assertIn('operator performed reimage/install outside this gate', self.source)
        self.assertNotIn("install-linux.sh", self.source)
        self.assertNotRegex(self.source, r"(?m)^\s*(?:dd|wipefs|mkfs|apt-get\s+(?:install|remove|purge))\b")

    def test_topology_is_explicit_and_matches_service_layout(self) -> None:
        topology = shell_function(self.source, "detect_topology")
        self.assertIn('[ -n "$TOPOLOGY" ] || die "--topology is required"', self.source)
        self.assertIn('case "$TOPOLOGY:$web_state"', topology)
        self.assertIn('web:loaded) WEB_ENABLED=1; API_URL="$BASE_URL"', topology)
        self.assertIn('headless:not-found|headless:"") WEB_ENABLED=0; API_URL="$CORE_URL"', topology)
        self.assertIn('--topology headless requires no $WEB_SERVICE', topology)
        self.assertIn('systemctl is-active --quiet "$WEB_SERVICE"', shell_function(self.source, "check_services"))

    def test_authentication_uses_file_backed_bearer_header_and_tests_protection(self) -> None:
        prepare = shell_function(self.source, "prepare_bearer_header_file")
        login = shell_function(self.source, "login_api")
        api_get = shell_function(self.source, "api_get")
        api_post = shell_function(self.source, "api_post")
        protected = shell_function(self.source, "check_unauthenticated_protected_api")
        self.assertRegex(prepare, r'mktemp "\$GATE_RUNTIME_PARENT/pi5-gate-bearer-\$RUN_STAMP\.XXXXXX"')
        self.assertIn('chmod 0600 "$header_file"', prepare)
        self.assertIn('unset TOKEN', prepare)
        self.assertNotIn("$TOKEN", api_get)
        self.assertNotIn("$TOKEN", api_post)
        self.assertIn('--header "@$CURL_AUTH_HEADER_FILE"', api_get)
        self.assertIn('--header "@$CURL_AUTH_HEADER_FILE"', api_post)
        curl_login = login[login.index("response=") :]
        self.assertNotIn("$PASSWORD", curl_login)
        self.assertIn('401|403', protected)
        self.assertIn('/api/core/status', protected)

    def test_malicious_local_tag_cannot_replace_canonical_upstream_sha(self) -> None:
        resolve = shell_function(self.source, "resolve_immutable_tag")

        self.assertIn(
            'Security invariant: only fixed canonical upstream direct/peeled tag refs determine EXPECTED_SHA; local refs/remotes/config never do.',
            resolve,
        )
        self.assertIn('readonly CANONICAL_UPSTREAM_URL="https://github.com/balbomush/GP-access-control-plane.git"', self.source)
        self.assertIn('git -C / -c credential.helper= -c core.askPass=/bin/false -c http.extraHeader=', resolve)
        self.assertIn('ls-remote "$CANONICAL_UPSTREAM_URL" "$CANDIDATE_REF" "${CANDIDATE_REF}^{}"', resolve)
        self.assertIn('GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=/bin/false', resolve)
        self.assertIn('EXPECTED_SHA="${peeled_sha:-$direct_sha}"', resolve)
        self.assertIn('cannot resolve release tag from canonical upstream', resolve)
        self.assertNotIn('show-ref', resolve)
        self.assertNotIn('rev-parse', resolve)
        self.assertNotIn('remote get-url', resolve)
        self.assertNotIn('git config', resolve)

    def test_functional_cycles_assert_standard_multi_domain_and_no_leftovers(self) -> None:
        cycle = shell_function(self.source, "start_and_stop_cycle")
        leftovers = shell_function(self.source, "inspect_leftovers")
        cleanup = shell_function(self.source, "safe_stop_own_run")
        self.assertIn('start_and_stop_cycle standard', self.source)
        self.assertIn('start_and_stop_cycle multi_domain', self.source)
        self.assertLess(cycle.index('/start-run'), cycle.index('/stop-current-run'))
        self.assertIn("run_is_stopped", cycle)
        self.assertLess(cleanup.index('[ "$current_run" = "$expected_run" ]'), cleanup.index('/stop-current-run'))
        self.assertIn('safe_stop_own_run "$run_id" || true', shell_function(self.source, "cycle_error"))
        for evidence in (".job-runner.lock", "RUN_REGISTRY_DIR", "blockcheck", "[n]fqws2"):
            self.assertIn(evidence, leftovers)
        executable = "\n".join(line for line in self.source.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotRegex(executable, r"(?m)^\s*(?:kill|pkill|killall|nft\s+delete)\b")

    def test_stop_cycle_reports_timeout_state_and_fails_early_for_unexpected_terminal_history(self) -> None:
        cycle = shell_function(self.source, "start_and_stop_cycle")
        self.assertIn("current_run_status", cycle)
        self.assertIn("history_run_status", cycle)
        self.assertIn("success|failed|timeout", cycle)
        self.assertIn("stop timeout: target_run_id=%s current_run_id=%s current_run_status=%s target_history_status=%s", cycle)

        terminal = self.run_stop_cycle_harness(history_status="failed", timeout=False)
        self.assertEqual(terminal.returncode, 1, terminal.stderr)
        self.assertIn("stop reached unexpected terminal status", terminal.stderr)
        self.assertIn("target_run_id=run-1", terminal.stderr)
        self.assertIn("current_run_id=run-1", terminal.stderr)
        self.assertIn("current_run_status=stopping", terminal.stderr)
        self.assertIn("target_history_status=failed", terminal.stderr)
        self.assertNotIn("sleep-called", terminal.stderr)

        timeout = self.run_stop_cycle_harness(history_status="stopping", timeout=True)
        self.assertEqual(timeout.returncode, 1, timeout.stderr)
        self.assertIn("stop timeout", timeout.stderr)
        self.assertIn("target_run_id=run-1", timeout.stderr)
        self.assertIn("current_run_id=run-1", timeout.stderr)
        self.assertIn("current_run_status=stopping", timeout.stderr)
        self.assertIn("target_history_status=stopping", timeout.stderr)

    def test_strict_update_requires_typed_queue_success_and_rollback_contract_evidence(self) -> None:
        queue = shell_function(self.source, "queue_dirty_update")
        queue_validation = shell_function(self.source, "validate_queue_evidence")
        success_validation = shell_function(self.source, "validate_update_success_evidence")
        rollback = shell_function(self.source, "check_rollback_contract")
        self.assertIn('"$ROOT_HELPER" queue-update --candidate-ref "$CANDIDATE_REF" --expected-sha "$EXPECTED_SHA"', queue)
        self.assertIn('validate_queue_evidence "$response"', queue)
        self.assertIn('validate_update_success_evidence "$log"', queue)
        self.assertIn('candidate_ref', queue_validation)
        self.assertIn('expected_sha', queue_validation)
        self.assertIn('verified_sha', success_validation)
        self.assertIn('installed_sha', success_validation)
        self.assertIn('status":["success"]', success_validation)
        self.assertIn('phase":["requested","verified","staged","published","root","committed","installed"]', success_validation)
        self.assertIn('cleanup_status":["completed"]', success_validation)
        self.assertIn('rollback_published_code() {', rollback)
        self.assertIn('rollback_scope=code', rollback)

    def test_strict_update_success_requires_completed_cleanup(self) -> None:
        sha = "a" * 40
        common = "\n".join(
            (
                "candidate_ref=refs/tags/v0.4.0",
                f"expected_sha={sha}",
                "verified_ref=refs/tags/v0.4.0",
                f"verified_sha={sha}",
                f"staged_sha={sha}",
                "phase=requested",
                "phase=verified",
                "phase=staged",
                "phase=published",
                "phase=root",
                "phase=committed",
                "phase=installed",
                "installed_ref=refs/tags/v0.4.0",
                f"installed_sha={sha}",
                "cleanup_status=completed",
            )
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            completed = temporary / "completed.log"
            completed.write_text(f"{common}\nstatus=success\n", encoding="utf-8")
            result = self.run_update_success_validation(completed)
            self.assertEqual(result.returncode, 0, result.stderr)

            premature_success = temporary / "premature-success.log"
            premature_success.write_text(
                common.replace("phase=installed", "status=success\nphase=installed") + "\n",
                encoding="utf-8",
            )
            result = self.run_update_success_validation(premature_success)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("after terminal status", result.stderr)

            trailing_output = temporary / "trailing-output.log"
            trailing_output.write_text(f"{common}\nstatus=success\nunstructured trailing output\n", encoding="utf-8")
            result = self.run_update_success_validation(trailing_output)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("after terminal status", result.stderr)

            deferred = temporary / "deferred.log"
            deferred.write_text(f"{common.replace('cleanup_status=completed', 'cleanup_status=deferred')}\nstatus=success\n", encoding="utf-8")
            result = self.run_update_success_validation(deferred)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("strict update success evidence is incomplete", result.stderr)


if __name__ == "__main__":
    unittest.main()
