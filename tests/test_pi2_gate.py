from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY / "scripts" / "release-gates" / "pi2-gate.sh"


def bash_executable() -> str | None:
    """Find a real Bash on Linux and the standard Git for Windows locations."""
    return next(
        (
            candidate
            for candidate in (
                shutil.which("bash"),
                r"C:\\Program Files\\Git\\bin\\bash.exe",
                r"C:\\Program Files\\Git\\usr\\bin\\bash.exe",
            )
            if candidate and Path(candidate).is_file()
        ),
        None,
    )


def shell_function(source: str, name: str) -> str:
    """Return one top-level Bash function without depending on its line count."""
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n(?P<body>.*?)(?=^\}}$)", source, re.MULTILINE | re.DOTALL)
    if not match:
        raise AssertionError(f"Bash function {name} was not found")
    return match.group("body")


class Pi2GateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")
        cls.bash = bash_executable()

    def run_gate(self, *args: str) -> subprocess.CompletedProcess[str]:
        if not self.bash:
            self.skipTest("real Bash is unavailable")
        return subprocess.run(
            [self.bash, str(SCRIPT), *args],
            cwd=REPOSITORY,
            text=True,
            capture_output=True,
            check=False,
        )

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

    def test_bash_syntax_help_and_early_argument_validation(self) -> None:
        if not self.bash:
            self.skipTest("real Bash is unavailable")

        syntax = subprocess.run([self.bash, "-n", str(SCRIPT)], text=True, capture_output=True, check=False)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)

        help_result = self.run_gate("--help")
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("Usage:", help_result.stdout)
        self.assertIn("--ref TAG", help_result.stdout)

        for args, expected_error in (
            (("--unknown",), "unknown argument: --unknown"),
            (("--ref",), "missing value for --ref"),
            (("--ref", "v1.2.3", "--mode", "unsupported"), "--mode must be installed or dirty-update"),
            (("--ref", "v1.2.3", "--poll-timeout", "9"), "--poll-timeout must be 10..900"),
        ):
            with self.subTest(args=args):
                result = self.run_gate(*args)
                self.assertEqual(result.returncode, 2)
                self.assertIn(expected_error, result.stderr)

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

    def test_state_dir_refuses_unsafe_or_missing_profile_for_v0_4_and_newer(self) -> None:
        resolve = shell_function(self.source, "resolve_state_dir")
        profile = shell_function(self.source, "read_trusted_profile_state_dir")

        self.assertIn('return 3', profile)
        self.assertIn('regular non-symlink file', profile)
        self.assertIn('cannot safely read GP_STATE_DIR from installation profile', resolve)
        self.assertIn('installation profile is required to derive --state-dir for $REF', resolve)
        self.assertIn('v0.[0-3].*', shell_function(self.source, "legacy_state_fallback_allowed"))
        self.assertNotIn('STATE_DIR="$(canonical_existing_dir "${STATE_DIR:-$INSTALL_DIR/build/state}")"', self.source)

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

    def test_bearer_header_is_file_backed_only_in_root_runtime_and_cleaned_up(self) -> None:
        prepare = shell_function(self.source, "prepare_bearer_header_file")
        api_get = shell_function(self.source, "api_get")
        api_post = shell_function(self.source, "api_post")
        finish = shell_function(self.source, "finish")

        self.assertIn('GATE_SECRET_DIR="$GATE_RUNTIME_PARENT"', self.source)
        self.assertIn('require_trusted_root_dir "$GATE_RUNTIME_PARENT" 0 0 700', self.source)
        self.assertRegex(prepare, r'mktemp "\$GATE_SECRET_DIR/pi2-gate-bearer-\$RUN_STAMP\.XXXXXX"')
        self.assertNotIn("$REPORT_DIR", prepare)
        self.assertRegex(prepare, r'chmod 0600 "\$header_file"')
        self.assertRegex(prepare, r"printf 'Authorization: Bearer %s\\n' \"\$TOKEN\" > \"\$header_file\"")
        self.assertRegex(prepare, r'rm -f -- "\$header_file" \|\| true')
        self.assertRegex(prepare, r'CURL_AUTH_HEADER_FILE="\$header_file"')

        for authenticated_request in (api_get, api_post):
            self.assertNotIn("$TOKEN", authenticated_request)
            self.assertRegex(authenticated_request, r'--header "@\$CURL_AUTH_HEADER_FILE"')
            self.assertNotRegex(authenticated_request, r"(?:--header|-H).*Authorization:\\s*Bearer")

        self.assertRegex(self.source, r"(?m)^trap finish EXIT$")
        cleanup = finish.index('rm -f -- "$CURL_AUTH_HEADER_FILE"')
        clear_reference = finish.index('CURL_AUTH_HEADER_FILE=""')
        scrub_secrets = finish.index("unset PASSWORD TOKEN")
        self.assertLess(cleanup, clear_reference)
        self.assertLess(clear_reference, scrub_secrets)

    def test_reports_are_root_app_group_and_gate_never_writes_state_as_root(self) -> None:
        report_file = shell_function(self.source, "new_gate_report_file")
        queue = shell_function(self.source, "queue_dirty_update")
        finish = shell_function(self.source, "finish")

        self.assertIn('require_trusted_root_dir "$GATE_REPORT_PARENT" 0 "$APP_GID" 750', self.source)
        self.assertIn('chown root:"$APP_GROUP" "$path" && chmod 0640 "$path"', report_file)
        self.assertIn('runuser -u "$APP_USER" -- mktemp "$INSTALL_DIR/.pi2-gate-dirty-marker-$RUN_STAMP.XXXXXX"', queue)
        self.assertIn('runuser -u "$APP_USER" -- rm -f -- "$DIRTY_MARKER"', finish)
        self.assertNotIn("--state-dir", queue)
        self.assertNotRegex(queue, r">\s*\"\$STATE_DIR")

    def test_dirty_update_uses_typed_pinned_candidate_and_login_keeps_secrets_off_argv(self) -> None:
        resolve = shell_function(self.source, "resolve_immutable_tag")
        queue = shell_function(self.source, "queue_dirty_update")
        login = shell_function(self.source, "login_api")

        self.assertIn('CANDIDATE_REF="refs/tags/$REF"', resolve)
        self.assertIn('git -C "$INSTALL_DIR" check-ref-format "$CANDIDATE_REF"', resolve)
        self.assertIn('EXPECTED_SHA="${peeled_sha:-$direct_sha}"', resolve)
        self.assertRegex(resolve, r'\[\[ "\$EXPECTED_SHA" =~ \^\[0-9a-f\]\{40\}\$ \]\]')

        typed_queue = '"$ROOT_HELPER" queue-update --candidate-ref "$CANDIDATE_REF" --expected-sha "$EXPECTED_SHA"'
        self.assertIn(typed_queue, queue)
        self.assertLess(queue.index('git -C "$INSTALL_DIR" status --porcelain'), queue.index(typed_queue))
        self.assertNotIn("--state-dir", queue)

        curl_login = login.split('response="$(', 1)[1].split('TOKEN="$(', 1)[0]
        self.assertIn("printf '%s' \"$payload\" | curl", curl_login)
        self.assertIn('--data-binary @-', curl_login)
        self.assertNotIn("$PASSWORD", curl_login)
        self.assertNotIn("$TOKEN", curl_login)
        self.assertIn('json.load(sys.stdin)', login)
        self.assertNotRegex(login, r'\$PYTHON"\s+-\s+"\$response"')
        self.assertIn("prepare_bearer_header_file\n  unset TOKEN", login)

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
        self.assertIn('cannot resolve release tag from canonical upstream', resolve)
        self.assertNotIn('show-ref', resolve)
        self.assertNotIn('rev-parse', resolve)
        self.assertNotIn('remote get-url', resolve)
        self.assertNotIn('git config', resolve)

    def test_dirty_update_requires_complete_queue_and_success_evidence(self) -> None:
        queue = shell_function(self.source, "queue_dirty_update")
        validate_queue = shell_function(self.source, "validate_queue_evidence")
        validate_success = shell_function(self.source, "validate_update_success_evidence")

        self.assertIn('queue_evidence="$(validate_queue_evidence "$response")" || return 1', queue)
        self.assertIn('validate_update_success_evidence "$log_file"', queue)
        self.assertIn('if set(values) != set(expected) | {"unit", "log"}:', self.source)
        self.assertIn('if key in values:', self.source)
        self.assertIn('candidate_ref', self.source)
        self.assertIn('verified_sha', self.source)
        self.assertIn('staged_sha', self.source)
        self.assertIn('installed_sha', self.source)
        self.assertIn('"status": ["success"]', self.source)
        self.assertIn('require_trusted_root_dir /var/lib/gp-control-plane/release-updates 0 0 700', self.source)

    def test_failure_recovery_records_evidence_stops_only_own_run_and_returns_original_failure(self) -> None:
        failure = shell_function(self.source, "cycle_failure")
        safe_cleanup = shell_function(self.source, "attempt_safe_cycle_cleanup")

        ordered_steps = (
            'report_event recovery "$cycle_name" failed',
            'record_leftover_assertion "$cycle_name" post-failure-before-cleanup',
            'record_safe_cycle_cleanup "$cycle_name" "$run_id"',
            'record_leftover_assertion "$cycle_name" post-cleanup',
            'return "$original_code"',
        )
        positions = [failure.index(step) for step in ordered_steps]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("no retry", failure)
        self.assertNotIn("start_and_cancel_cycle", failure)

        mismatch = safe_cleanup.index('if [ "$current_run" != "$run_id" ]; then')
        stop_request = safe_cleanup.index('api_post "$API_URL" "/api/core/strategy-discovery/stop-current-run"')
        self.assertLess(mismatch, stop_request)
        self.assertIn('json_assert stopping "$stop_response"', safe_cleanup)
        self.assertIn('[ "$stopped_run" != "$run_id" ]', safe_cleanup)

    def test_immediate_cancel_and_headless_topology_have_no_web_dependency(self) -> None:
        cycle = shell_function(self.source, "start_and_cancel_cycle")
        topology = shell_function(self.source, "detect_topology")
        resources = shell_function(self.source, "check_resource_budget")
        services = shell_function(self.source, "check_required_services")

        self.assertNotIn("observe-current", cycle)
        self.assertLess(
            cycle.index('api_post "$API_URL" "/api/core/strategy-discovery/stop-current-run"'),
            cycle.index('api_get "$API_URL" "/api/core/strategy-discovery/current-run-progress"'),
        )
        self.assertIn('not-found|"") WEB_ENABLED=0; API_URL="$CORE_URL"', topology)
        self.assertIn('loaded) WEB_ENABLED=1; API_URL="$BASE_URL"', topology)
        self.assertIn('if [ "$WEB_ENABLED" -eq 0 ]; then', resources)
        self.assertIn('topology=headless core_rss_kib=%s', resources)
        self.assertIn('if [ "$WEB_ENABLED" -eq 1 ]; then', services)

    def test_script_never_uses_generic_process_or_nft_deletion(self) -> None:
        executable_lines = "\n".join(
            line for line in self.source.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertNotRegex(executable_lines, r"(?m)^\s*(?:sudo\s+)?(?:kill|pkill|killall)\b")
        self.assertNotRegex(executable_lines, r"(?m)^\s*(?:sudo\s+)?nft\s+delete\b")


if __name__ == "__main__":
    unittest.main()
