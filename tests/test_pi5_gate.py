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

WINDOWS_GIT_BASH_CANDIDATES = (
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files\Git\usr\bin\bash.exe",
)


def bash_executable() -> str | None:
    candidates = WINDOWS_GIT_BASH_CANDIDATES + (
        "/bin/bash",
        "/usr/bin/bash",
        shutil.which("bash"),
    )
    return next(
        (candidate for candidate in candidates if candidate and Path(candidate).is_file()),
        None,
    )


def git_bash_tool(bash: str | None, name: str) -> str | None:
    if not bash:
        return None
    bash_path = Path(bash).resolve()
    bash_bin_dir = bash_path.parent
    git_root = bash_bin_dir.parent
    if git_root.name.lower() == "usr":
        git_root = git_root.parent
    candidates = [git_root / "usr" / "bin" / f"{name}.exe"]
    if os.name != "nt":
        discovered = shutil.which(name)
        if discovered:
            candidates.append(Path(discovered))
    return next((str(candidate) for candidate in candidates if candidate.is_file()), None)


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
        cls.awk = git_bash_tool(cls.bash, "awk")
        cls.rm = git_bash_tool(cls.bash, "rm")

    def test_git_bash_tool_resolves_both_supported_bash_layouts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            git_root = Path(raw) / "Git"
            tool_dir = git_root / "usr" / "bin"
            tool_dir.mkdir(parents=True)

            for tool_name in ("awk", "rm"):
                (tool_dir / f"{tool_name}.exe").touch()

            for bash_parts in (("bin",), ("usr", "bin")):
                with self.subTest(bash_parts=bash_parts):
                    bash_path = git_root.joinpath(*bash_parts, "bash.exe")
                    bash_path.parent.mkdir(parents=True, exist_ok=True)
                    bash_path.touch()

                    for tool_name in ("awk", "rm"):
                        self.assertEqual(
                            Path(git_bash_tool(str(bash_path), tool_name)).resolve(),
                            (tool_dir / f"{tool_name}.exe").resolve(),
                        )

    def run_gate(self, *args: str) -> subprocess.CompletedProcess[str]:
        if not self.bash:
            self.skipTest("real Bash is unavailable")
        harness = '''\
PATH=/nonexistent
dirname() {
  [ "$1" = -- ] && shift
  case "${1%/}" in
    */*) printf '%s\n' "${1%/*}" ;;
    *) printf '.\n' ;;
  esac
}
cat() {
  local line
  while IFS= read -r line || [ -n "$line" ]; do printf '%s\n' "$line"; done
}
source "$1" "${@:2}"
'''
        return subprocess.run(
            [self.bash, "-c", harness, "gate-cli", posix_shell_path(SCRIPT), *args],
            cwd=REPOSITORY,
            text=True,
            capture_output=True,
            check=False,
        )

    def run_profile_reader(self, profile: Path) -> subprocess.CompletedProcess[str]:
        if not self.bash:
            self.skipTest("real Bash is unavailable")
        if not self.awk:
            self.fail("real awk is required for the Pi5 profile harness; expected Git Bash usr/bin/awk.exe")
        reader = shell_function(self.source, "read_trusted_profile_state_dir")
        harness = f'''set -o pipefail
PATH=/nonexistent
awk() {{ "$GATE_REAL_AWK" "$@"; }}
read_trusted_profile_state_dir() {{
{reader}
}}
INSTALL_PROFILE="$1"
stat() {{ printf '0:0:600\\n'; }}
read_trusted_profile_state_dir
        '''
        environment = os.environ | {"GATE_REAL_AWK": posix_shell_path(Path(self.awk))}
        return subprocess.run(
            [self.bash, "-c", harness, "profile-reader", posix_shell_path(profile)],
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )

    def run_installed_identity_check(
        self, *, tag_mode: bool, head: str, attached: bool, status: str, tag_exists: bool = True,
        tag_type: str = "tag", local_tag_object: str = "b" * 40, local_tag_commit: str = "a" * 40,
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        if not self.bash:
            self.skipTest("real Bash is unavailable")
        resolve = shell_function(self.source, "resolve_immutable_tag")
        check = shell_function(self.source, "check_installed_ref")
        with tempfile.TemporaryDirectory() as raw:
            temporary = Path(raw)
            git_log = temporary / "git.log"
            environment = os.environ | {
                "GIT_LOG": posix_shell_path(git_log),
                "FAKE_HEAD": head,
                "FAKE_ATTACHED": "1" if attached else "0",
                "FAKE_STATUS": status,
                "FAKE_TAG_EXISTS": "1" if tag_exists else "0",
                "FAKE_TAG_TYPE": tag_type,
                "FAKE_LOCAL_TAG_OBJECT": local_tag_object,
                "FAKE_LOCAL_TAG_COMMIT": local_tag_commit,
            }
            harness = f'''set -o pipefail
PATH=/nonexistent
INSTALL_DIR="/installed"
resolve_immutable_tag() {{
{resolve}
}}
check_installed_ref() {{
{check}
}}
git() {{
  printf '%s\\n' "$*" >> "$GIT_LOG"
  while [ "$1" = -C ] || [ "$1" = -c ]; do shift 2; done
  case "$1" in
    check-ref-format) return 0 ;;
    ls-remote) printf '%s\\trefs/tags/v1.2.3\\n%s\\trefs/tags/v1.2.3^{{}}\\n' "{'b' * 40}" "{'a' * 40}" ;;
    rev-parse)
      case "$3" in
        HEAD) printf '%s\\n' "$FAKE_HEAD" ;;
        *'^{{tag}}') [ "$FAKE_TAG_TYPE" = tag ] && printf '%s\\n' "$FAKE_LOCAL_TAG_OBJECT" || return 1 ;;
        *'^{{commit}}') printf '%s\\n' "$FAKE_LOCAL_TAG_COMMIT" ;;
        *) return 98 ;;
      esac
      ;;
    show-ref) [ "$FAKE_TAG_EXISTS" = 1 ] ;;
    cat-file) printf '%s\\n' "$FAKE_TAG_TYPE" ;;
    symbolic-ref) [ "$FAKE_ATTACHED" = 1 ] && {{ printf '%s\\n' refs/heads/dev; return 0; }}; return 1 ;;
    status) printf '%s' "$FAKE_STATUS" ;;
    *) printf 'unexpected fake git command: %s\\n' "$*" >&2; return 99 ;;
  esac
}}
if [ "$1" = tag ]; then
  REF='v1.2.3'; CANDIDATE_REF=''; EXPECTED_SHA=''; CANONICAL_TAG_OBJECT_SHA=''
  resolve_immutable_tag
else
  REF=''; CANDIDATE_REF='refs/heads/dev'; EXPECTED_SHA="{'a' * 40}"; CANONICAL_TAG_OBJECT_SHA=''
fi
check_installed_ref
check_status=$?
printf 'canonical=%s expected=%s\\n' "$CANONICAL_TAG_OBJECT_SHA" "$EXPECTED_SHA"
exit "$check_status"
'''
            result = subprocess.run(
                [self.bash, "-c", harness, "installed-identity", "tag" if tag_mode else "dev"],
                text=True,
                capture_output=True,
                env=environment,
                check=False,
            )
            return result, git_log.read_text(encoding="utf-8").splitlines()


    def run_stop_cycle_harness(
        self,
        *,
        history_status: str,
        timeout: bool,
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        if not self.bash:
            self.skipTest("real Bash is unavailable")
        functions = {
            name: shell_function(self.source, name)
            for name in ("current_run_id", "current_run_status", "history_run_status", "run_is_stopped", "start_and_stop_cycle")
        }
        with tempfile.TemporaryDirectory() as raw:
            clock = Path(raw) / "clock"
            progress = Path(raw) / "progress.log"
            clock.write_bytes(b"100\n")
            history = "stopping" if timeout else history_status
            harness = f'''\
PATH=/nonexistent
PYTHON="$1"
POLL_TIMEOUT_SECONDS=10
API_URL="http://gate.test"
TEST_DOMAIN="example.com"
CLOCK="$2"
PROGRESS_LOG="$3"
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
sleep() {{ printf 'sleep-called\\n' >> "$PROGRESS_LOG"; return 0; }}
date() {{
  IFS= read -r value < "$CLOCK" || return 70
  case "$value" in
    100) next=101; now=100 ;;
    101) next=110; now=109 ;;
    *) next=111; now=110 ;;
  esac
  printf '%s\\n' "$next" > "$CLOCK"
  printf 'date:%s\\n' "$now" >> "$PROGRESS_LOG"
  printf '%s\\n' "$now"
}}
start_and_stop_cycle() {{
{functions["start_and_stop_cycle"]}
}}
start_and_stop_cycle standard
'''
            command = [
                self.bash,
                "-c",
                harness,
                "stop-cycle-harness",
                posix_shell_path(Path(sys.executable)),
                posix_shell_path(clock),
                posix_shell_path(progress),
            ]
            result = subprocess.run(command, text=True, capture_output=True, check=False)
            progress_lines = progress.read_text(encoding="utf-8").splitlines() if progress.exists() else []
            return result, progress_lines

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

    def test_gate_excludes_obsolete_update_mode(self) -> None:
        self.assertNotIn("queue-update", self.source)
        self.assertNotIn("dirty-update", self.source)
        self.assertNotIn("release-updates", self.source)

    def test_tag_identity_requires_detached_canonical_annotated_local_tag_while_dev_accepts_attached_head(self) -> None:
        sha = "a" * 40
        success, calls = self.run_installed_identity_check(tag_mode=True, head=sha, attached=False, status="")
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertIn(f"canonical={'b' * 40} expected={sha}", success.stdout)
        self.assertTrue(any("symbolic-ref -q HEAD" in call for call in calls))
        self.assertTrue(any("show-ref --verify --quiet refs/tags/v1.2.3" in call for call in calls))
        self.assertTrue(any("cat-file -t refs/tags/v1.2.3" in call for call in calls))

        attached, _ = self.run_installed_identity_check(tag_mode=True, head=sha, attached=True, status="")
        self.assertNotEqual(attached.returncode, 0)
        self.assertIn("tag checkout is not detached", attached.stderr)

        missing, _ = self.run_installed_identity_check(tag_mode=True, head=sha, attached=False, status="", tag_exists=False)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("absent locally", missing.stderr)

        lightweight, _ = self.run_installed_identity_check(tag_mode=True, head=sha, attached=False, status="", tag_type="commit")
        self.assertNotEqual(lightweight.returncode, 0)
        self.assertIn("not annotated locally", lightweight.stderr)

        forged, _ = self.run_installed_identity_check(tag_mode=True, head=sha, attached=False, status="", local_tag_object="c" * 40)
        self.assertNotEqual(forged.returncode, 0)
        self.assertIn("does not match canonical upstream", forged.stderr)

        dirty, _ = self.run_installed_identity_check(tag_mode=True, head=sha, attached=False, status=" M tracked")
        self.assertNotEqual(dirty.returncode, 0)
        self.assertIn("local changes", dirty.stderr)

        mismatch, _ = self.run_installed_identity_check(tag_mode=True, head="d" * 40, attached=False, status="")
        self.assertNotEqual(mismatch.returncode, 0)
        self.assertIn("does not match expected candidate", mismatch.stderr)

        dev_attached, dev_calls = self.run_installed_identity_check(tag_mode=False, head=sha, attached=True, status="")
        self.assertEqual(dev_attached.returncode, 0, dev_attached.stderr)
        self.assertNotIn("show-ref", "\n".join(dev_calls))

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
        self.assertIn('[ -n "$peeled_sha" ] || { printf \'release tag is not an annotated tag on canonical upstream: %s\\n\'', resolve)
        self.assertIn('EXPECTED_SHA="$peeled_sha"', resolve)
        self.assertIn('cannot resolve release tag from canonical upstream', resolve)
        self.assertNotIn('show-ref', resolve)
        self.assertNotIn('rev-parse', resolve)
        self.assertNotIn('remote get-url', resolve)
        self.assertNotIn('git config', resolve)

    def test_pre_tag_candidate_must_match_canonical_dev_sha(self) -> None:
        resolve = shell_function(self.source, "resolve_pre_tag_candidate")

        self.assertIn('ls-remote --exit-code "$CANONICAL_UPSTREAM_URL" "$CANDIDATE_REF"', resolve)
        self.assertIn('canonical upstream dev does not match expected pre-tag SHA', resolve)
        self.assertIn('canonical upstream returned duplicate pre-tag refs', resolve)
        self.assertIn('CANDIDATE_SHA="$EXPECTED_SHA"', resolve)

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

        terminal, terminal_progress = self.run_stop_cycle_harness(history_status="failed", timeout=False)
        self.assertEqual(terminal.returncode, 1, terminal.stderr)
        self.assertIn("stop reached unexpected terminal status", terminal.stderr)
        self.assertIn("target_run_id=run-1", terminal.stderr)
        self.assertIn("current_run_id=run-1", terminal.stderr)
        self.assertIn("current_run_status=stopping", terminal.stderr)
        self.assertIn("target_history_status=failed", terminal.stderr)
        self.assertNotIn("sleep-called", terminal.stderr)
        self.assertEqual(terminal_progress, ["date:100", "date:109"])

        timeout, timeout_progress = self.run_stop_cycle_harness(history_status="stopping", timeout=True)
        self.assertEqual(timeout.returncode, 1, timeout.stderr)
        self.assertIn("stop timeout", timeout.stderr)
        self.assertIn("target_run_id=run-1", timeout.stderr)
        self.assertIn("current_run_id=run-1", timeout.stderr)
        self.assertIn("current_run_status=stopping", timeout.stderr)
        self.assertIn("target_history_status=stopping", timeout.stderr)
        self.assertEqual(timeout_progress, ["date:100", "date:109", "sleep-called", "date:110"])


if __name__ == "__main__":
    unittest.main()
