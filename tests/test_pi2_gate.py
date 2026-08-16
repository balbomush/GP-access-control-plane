from __future__ import annotations

import re
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY / "scripts" / "release-gates" / "pi2-gate.sh"

WINDOWS_GIT_BASH_CANDIDATES = (
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files\Git\usr\bin\bash.exe",
)


def bash_executable() -> str | None:
    """Find a real Bash on Linux and the standard Git for Windows locations."""
    candidates = WINDOWS_GIT_BASH_CANDIDATES + (
        "/bin/bash",
        "/usr/bin/bash",
        shutil.which("bash"),
    )
    return next(
        (
            candidate
            for candidate in candidates
            if candidate and Path(candidate).is_file()
        ),
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
    """Return one top-level Bash function without depending on its line count."""
    match = re.search(
        rf"^{re.escape(name)}\(\) \{{\n(?P<body>.*?)(?=^\}}\n\n)",
        source,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        raise AssertionError(f"Bash function {name} was not found")
    return match.group("body")


def bash_path(path: str) -> str:
    """Return a path that Git Bash can execute without consulting PATH."""
    resolved = Path(path).resolve()
    drive, tail = os.path.splitdrive(str(resolved))
    if drive:
        return f"/{drive[0].lower()}{tail.replace(os.sep, '/')}"
    return str(resolved)


class Pi2GateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")
        cls.bash = bash_executable()
        cls.python = bash_path(sys.executable)
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
        harness = r'''PATH=/nonexistent
dirname() {
  [ "$1" = '--' ] && shift
  case "$1" in
    */*) printf '%s\n' "${1%/*}" ;;
    *) printf '.\n' ;;
  esac
}
cat() {
  while IFS= read -r line || [ -n "$line" ]; do
    printf '%s\n' "$line"
  done
}
export -f dirname cat
exec "$@"
'''
        return subprocess.run(
            [self.bash, "-c", harness, "pi2-gate-launcher", self.bash, str(SCRIPT), *args],
            cwd=REPOSITORY,
            text=True,
            capture_output=True,
            check=False,
        )

    def run_profile_reader(self, profile: Path) -> subprocess.CompletedProcess[str]:
        if not self.bash:
            self.skipTest("real Bash is unavailable")
        if not self.awk:
            self.fail("real awk is required for the Pi2 profile harness; expected Git Bash usr/bin/awk.exe")
        reader = shell_function(self.source, "read_trusted_profile_state_dir")
        harness = f'''set -o pipefail
PATH=/nonexistent
read_trusted_profile_state_dir() {{
{reader}
}}
awk() {{ "$GATE_REAL_AWK" "$@"; }}
INSTALL_PROFILE="$1"
stat() {{ printf '0:0:600\\n'; }}
read_trusted_profile_state_dir
'''
        return subprocess.run(
            [self.bash, "-c", harness, "profile-reader", bash_path(str(profile))],
            text=True,
            capture_output=True,
            check=False,
            env={**os.environ, "GATE_REAL_AWK": bash_path(self.awk)},
        )

    def run_update_success_validator(
        self,
        update_log: Path,
        candidate_ref: str = "refs/tags/v1.2.3",
        expected_sha: str = "a" * 40,
    ) -> subprocess.CompletedProcess[str]:
        if not self.bash:
            self.skipTest("real Bash is unavailable")
        validator = shell_function(self.source, "validate_update_success_evidence")
        harness = f'''set -o pipefail
PATH=/nonexistent
require_trusted_root_dir() {{ :; }}
validate_update_success_evidence() {{
{validator}
}}
python() {{ "$GATE_TEST_PYTHON" "$@"; }}
CANDIDATE_REF="$2"
EXPECTED_SHA="$3"
PYTHON=python
stat() {{ printf '0:0:600\\n'; }}
validate_update_success_evidence "$1"
'''
        return subprocess.run(
            [self.bash, "-c", harness, "update-success-validator", str(update_log), candidate_ref, expected_sha],
            text=True,
            capture_output=True,
            check=False,
            env={**os.environ, "GATE_TEST_PYTHON": self.python},
        )

    def run_cli_validator(self, *args: str) -> subprocess.CompletedProcess[str]:
        if not self.bash:
            self.skipTest("real Bash is unavailable")
        functions = "\n".join(
            f"{name}() {{\n{shell_function(self.source, name)}\n}}"
            for name in (
                "die",
                "require_value",
                "validate_url",
                "validate_name",
                "validate_domain",
                "parse_arguments",
                "validate_arguments",
            )
        )
        harness = f'''set -Eeuo pipefail
PATH=/nonexistent
{functions}
REF=''
CANDIDATE=''
EXPECTED_SHA=''
MODE='installed'
BASE_URL='http://127.0.0.1:8080'
CORE_URL='http://127.0.0.1:8081'
PASSWORD_ENV='GP_GATE_PASSWORD'
TEST_DOMAIN='example.com'
POLL_TIMEOUT_SECONDS=90
parse_arguments "$@"
validate_arguments
printf 'candidate=%s expected_sha=%s\n' "$CANDIDATE" "$EXPECTED_SHA"
'''
        return subprocess.run(
            [self.bash, "-c", harness, "pi2-cli-validator", *args],
            text=True,
            capture_output=True,
            check=False,
        )

    def run_pretag_update_harness(
        self,
        temporary: Path,
        *,
        head: str,
        post_status: str = "",
        update_status: str = "success",
        retain_dirty_marker: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        if not self.bash:
            self.skipTest("real Bash is unavailable")
        if not self.rm:
            self.fail("real rm is required for the Pi2 pre-tag harness; expected Git Bash usr/bin/rm.exe")
        install_dir = temporary / "install"
        install_dir.mkdir()
        events = temporary / "events.log"
        update_log = temporary / "update.log"
        expected_sha = "a" * 40
        update_log.write_bytes(f"status={update_status}\n".encode("ascii"))

        queue = f"queue_dirty_update() {{\n{shell_function(self.source, 'queue_dirty_update')}\n}}"
        resolve = f"resolve_pre_tag_candidate() {{\n{shell_function(self.source, 'resolve_pre_tag_candidate')}\n}}"
        identity = f"check_installed_ref() {{\n{shell_function(self.source, 'check_installed_ref')}\n}}"
        harness = f'''set -Eeuo pipefail
PATH=/nonexistent
{resolve}
{queue}
{identity}
validate_queue_evidence() {{ printf 'gp-control-plane-update-20260816T120000Z-1\t%s\n' "$GATE_UPDATE_LOG"; }}
validate_update_success_evidence() {{ printf 'update-evidence\n' >> "$GATE_EVENTS"; }}
git() {{
  printf 'git %s\n' "$*" >> "$GATE_EVENTS"
  case " $* " in
    *" check-ref-format refs/heads/dev "*) return 0 ;;
    *" status --porcelain "*)
      count_file="$GATE_DIR/status-count"
      count=0
      [ -f "$count_file" ] && count="$(< "$count_file")"
      count=$((count + 1))
      printf '%s' "$count" > "$count_file"
      [ "$count" -gt 1 ] && printf '%s' "$GATE_POST_STATUS"
      return 0
      ;;
    *" rev-parse --verify HEAD "*) printf '%s\n' "$GATE_HEAD"; return 0 ;;
    *) printf 'unexpected fake git call: %s\n' "$*" >&2; return 64 ;;
  esac
}}
runuser() {{
  [ "$1" = '-u' ] || return 64
  shift 3
  case "$1" in
    mktemp)
      marker="${{2%XXXXXX}}harness"
      [ "$marker" != "$2" ] || return 64
      : > "$marker"
      printf 'marker-created %s\n' "$marker" >> "$GATE_EVENTS"
      printf '%s\n' "$marker"
      ;;
    tee)
      [ "$#" -eq 2 ] || return 64
      while IFS= read -r line || [ -n "$line" ]; do
        printf '%s\n' "$line"
      done > "$2"
      ;;
    rm) return 0 ;;
    *) printf 'unexpected fake runuser command: %s\n' "$1" >&2; return 64 ;;
  esac
}}
grep() {{
  case "$1" in
    -qx) [ "$#" -eq 3 ] || return 64; expected="$2"; mode=exact ;;
    -q) [ "$#" -eq 3 ] || return 64; [ "$2" = '^status=' ] || return 64; expected="$2"; mode=status ;;
    *) printf 'unexpected fake grep invocation: %s\n' "$*" >&2; return 64 ;;
  esac
  printf 'grep %s\n' "$*" >> "$GATE_EVENTS"
  while IFS= read -r line || [ -n "$line" ]; do
    if [ "$mode" = exact ] && [ "$line" = "$expected" ]; then return 0; fi
    if [ "$mode" = status ] && [ "${{line#status=}}" != "$line" ]; then return 0; fi
  done < "$3"
  return 1
}}
cat() {{
  [ "$#" -eq 1 ] || return 64
  while IFS= read -r line || [ -n "$line" ]; do printf '%s\n' "$line"; done < "$1"
}}
date() {{ printf 'date %s\n' "$*" >> "$GATE_EVENTS"; printf '100\n'; }}
sleep() {{ printf 'sleep %s\n' "$*" >> "$GATE_EVENTS"; return 0; }}
systemctl() {{
  printf 'systemctl %s\n' "$*" >> "$GATE_EVENTS"
  [ "$*" = 'show --property=ActiveState --value gp-control-plane-update-20260816T120000Z-1' ] || return 64
  printf '%s\n' "$GATE_SYSTEMCTL_STATE"
}}
root-helper() {{
  printf 'root-helper %s\n' "$*" >> "$GATE_EVENTS"
  [ "$1" = queue-update ] || return 64
  [ -n "$DIRTY_MARKER" ] && [ -f "$DIRTY_MARKER" ] || {{ printf 'dirty marker was not materialized\n' >&2; return 65; }}
  IFS= read -r marker_line < "$DIRTY_MARKER"
  [ "$marker_line" = "created by pi2-gate $RUN_STAMP for ref $REF" ] || {{ printf 'dirty marker content is invalid\n' >&2; return 66; }}
  if [ "$GATE_RETAIN_DIRTY_MARKER" = 1 ]; then
    printf 'marker-retained %s\n' "$DIRTY_MARKER" >> "$GATE_EVENTS"
    printf 'queued=true\nstatus=queued\nphase=queued\ncandidate_ref=refs/heads/dev\nexpected_sha=%s\nunit=gp-control-plane-update-20260816T120000Z-1\nlog=/unused\n' "$GATE_EXPECTED_SHA"
    return 0
  fi
  "$GATE_TEST_RM" -f -- "$DIRTY_MARKER"
  [ ! -e "$DIRTY_MARKER" ] || {{ printf 'dirty marker survived root helper\n' >&2; return 67; }}
  printf 'marker-removed %s\n' "$DIRTY_MARKER" >> "$GATE_EVENTS"
  printf 'queued=true\nstatus=queued\nphase=queued\ncandidate_ref=refs/heads/dev\nexpected_sha=%s\nunit=gp-control-plane-update-20260816T120000Z-1\nlog=/unused\n' "$GATE_EXPECTED_SHA"
}}
INSTALL_DIR="$1"
ROOT_HELPER=root-helper
export GATE_DIR GATE_EVENTS GATE_UPDATE_LOG
APP_USER='gate-test'
RUN_STAMP='20260816T120000Z-1'
REF=''
CANDIDATE='origin/dev'
CANDIDATE_REF=''
EXPECTED_SHA="$GATE_EXPECTED_SHA"
PYTHON=python
DIRTY_MARKER=''
resolve_pre_tag_candidate
queue_dirty_update
check_installed_ref
'''
        environment = {
            **os.environ,
            "GATE_DIR": bash_path(str(install_dir)),
            "GATE_EVENTS": bash_path(str(events)),
            "GATE_UPDATE_LOG": bash_path(str(update_log)),
            "GATE_EXPECTED_SHA": expected_sha,
            "GATE_HEAD": head,
            "GATE_POST_STATUS": post_status,
            "GATE_SYSTEMCTL_STATE": "active",
            "GATE_TEST_RM": bash_path(self.rm),
            "GATE_RETAIN_DIRTY_MARKER": "1" if retain_dirty_marker else "0",
        }
        result = subprocess.run(
            [self.bash, "-c", harness, "pi2-pretag-update", bash_path(str(install_dir))],
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        result.events = events.read_text(encoding="utf-8") if events.exists() else ""  # type: ignore[attr-defined]
        return result

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

    def test_pretag_cli_accepts_only_origin_dev_with_a_lowercase_pinned_sha(self) -> None:
        expected_sha = "a" * 40
        accepted = self.run_cli_validator("--candidate", "origin/dev", "--expected-sha", expected_sha)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertIn(f"candidate=origin/dev expected_sha={expected_sha}", accepted.stdout)

        for args, error in (
            (("--candidate", "refs/heads/dev", "--expected-sha", expected_sha), "--candidate must be canonical origin/dev"),
            (("--candidate", "origin/dev", "--expected-sha", "A" * 40), "--expected-sha must be 40 lowercase hexadecimal characters"),
            (("--candidate", "origin/dev"), "use --ref TAG or --candidate origin/dev --expected-sha SHA"),
            (("--ref", "v1.2.3", "--candidate", "origin/dev", "--expected-sha", expected_sha), "--ref cannot be combined"),
        ):
            with self.subTest(args=args):
                rejected = self.run_cli_validator(*args)
                self.assertEqual(rejected.returncode, 2)
                self.assertIn(error, rejected.stderr)

    def test_pretag_dirty_update_queues_before_post_update_identity_check_and_never_uses_tags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = self.run_pretag_update_harness(Path(temporary_directory), head="a" * 40)

        self.assertEqual(result.returncode, 0, result.stderr)
        events = result.events  # type: ignore[attr-defined]
        self.assertIn("git -C", events)
        self.assertIn("check-ref-format refs/heads/dev", events)
        self.assertIn("root-helper queue-update --candidate-ref refs/heads/dev --expected-sha " + "a" * 40, events)
        self.assertIn("marker-created ", events)
        self.assertIn("marker-removed ", events)
        self.assertLess(events.index("marker-created "), events.index("root-helper queue-update"))
        self.assertLess(events.index("root-helper queue-update"), events.index("marker-removed "))
        self.assertLess(events.index("status --porcelain"), events.index("root-helper queue-update"))
        self.assertLess(events.index("root-helper queue-update"), events.index("rev-parse --verify HEAD"))
        self.assertNotIn("refs/tags/", events)
        self.assertNotIn("ls-remote", events)
        self.assertNotIn(" tag ", events)

    def test_pretag_dirty_update_terminal_failure_is_hermetic_and_does_not_poll(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = self.run_pretag_update_harness(
                Path(temporary_directory),
                head="a" * 40,
                update_status="failed",
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("queue-update emitted invalid terminal status", result.stderr)
        self.assertIn("status=failed", result.stdout)
        events = result.events  # type: ignore[attr-defined]
        self.assertIn("grep -qx status=success", events)
        self.assertIn("grep -q ^status=", events)
        self.assertNotIn("systemctl ", events)
        self.assertIn("date +%s", events)
        self.assertNotIn("sleep ", events)

    def test_pretag_dirty_update_rejects_post_update_sha_or_cleanliness_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            sha_mismatch = self.run_pretag_update_harness(Path(temporary_directory), head="b" * 40)
        self.assertNotEqual(sha_mismatch.returncode, 0)
        self.assertIn("root-helper queue-update", sha_mismatch.events)  # type: ignore[attr-defined]
        self.assertIn("does not match candidate refs/heads/dev", sha_mismatch.stderr)

        with tempfile.TemporaryDirectory() as temporary_directory:
            dirty = self.run_pretag_update_harness(
                Path(temporary_directory),
                head="a" * 40,
                post_status=" M scripts/release-gates/pi2-gate.sh",
            )
        self.assertNotEqual(dirty.returncode, 0)
        self.assertIn("installed checkout has local changes", dirty.stderr)

    def test_pretag_dirty_update_rejects_valid_evidence_when_the_root_helper_retains_the_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            retained = self.run_pretag_update_harness(
                Path(temporary_directory),
                head="a" * 40,
                retain_dirty_marker=True,
            )

        self.assertNotEqual(retained.returncode, 0)
        self.assertIn("queued update did not remove gate dirty marker", retained.stderr)
        self.assertIn("marker-retained ", retained.events)  # type: ignore[attr-defined]

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

    def test_root_linux_test_never_writes_bytecode_in_the_installed_worktree(self) -> None:
        root_test = shell_function(self.source, "run_root_linux_test")

        self.assertIn('cd "$INSTALL_DIR"', root_test)
        self.assertIn('PYTHONDONTWRITEBYTECODE=1 "$PYTHON" -B -m unittest "$test_name"', root_test)

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
        self.assertIn('"cleanup_status": ["completed"]', validate_success)
        self.assertIn('"phase": ["requested", "verified", "staged", "published", "root", "committed", "installed"]', validate_success)
        self.assertIn('"status": ["success"]', self.source)
        self.assertIn('require_trusted_root_dir /var/lib/gp-control-plane/release-updates 0 0 700', self.source)

    def test_update_success_validator_requires_completed_cleanup_and_terminal_success(self) -> None:
        completed_log = """\
phase=requested
candidate_ref=refs/tags/v1.2.3
expected_sha=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
phase=verified
verified_ref=refs/tags/v1.2.3
verified_sha=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
phase=staged
staged_sha=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
phase=published
phase=root
phase=committed
phase=installed
installed_ref=refs/tags/v1.2.3
installed_sha=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
cleanup_status=completed
status=success
"""
        deferred_log = completed_log.replace("cleanup_status=completed", "cleanup_status=deferred")
        early_success_log = completed_log.replace(
            "cleanup_status=completed\nstatus=success\n",
            "status=success\ncleanup_status=completed\n",
        )
        trailing_output_log = completed_log + "unstructured trailing output\n"

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            completed_path = temporary / "completed.log"
            deferred_path = temporary / "deferred.log"
            early_success_path = temporary / "early-success.log"
            trailing_output_path = temporary / "trailing-output.log"
            completed_path.write_text(completed_log, encoding="utf-8")
            deferred_path.write_text(deferred_log, encoding="utf-8")
            early_success_path.write_text(early_success_log, encoding="utf-8")
            trailing_output_path.write_text(trailing_output_log, encoding="utf-8")

            self.assertEqual(self.run_update_success_validator(completed_path).returncode, 0)
            self.assertNotEqual(self.run_update_success_validator(deferred_path).returncode, 0)
            self.assertNotEqual(self.run_update_success_validator(early_success_path).returncode, 0)
            self.assertNotEqual(self.run_update_success_validator(trailing_output_path).returncode, 0)

    def test_pretag_update_evidence_rejects_remote_sha_mismatch(self) -> None:
        expected_sha = "a" * 40
        candidate_ref = "refs/heads/dev"
        completed_log = f"""\
phase=requested
candidate_ref={candidate_ref}
expected_sha={expected_sha}
phase=verified
verified_ref={candidate_ref}
verified_sha={expected_sha}
phase=staged
staged_sha={expected_sha}
phase=published
phase=root
phase=committed
phase=installed
installed_ref={candidate_ref}
installed_sha={expected_sha}
cleanup_status=completed
status=success
"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "update.log"
            path.write_text(completed_log, encoding="utf-8")
            self.assertEqual(
                self.run_update_success_validator(path, candidate_ref, expected_sha).returncode,
                0,
            )
            path.write_text(completed_log.replace(f"verified_sha={expected_sha}", "verified_sha=" + "b" * 40), encoding="utf-8")
            self.assertNotEqual(
                self.run_update_success_validator(path, candidate_ref, expected_sha).returncode,
                0,
            )

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
