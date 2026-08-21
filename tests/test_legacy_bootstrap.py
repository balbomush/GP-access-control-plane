from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class LegacyBootstrapContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.path = root / "scripts" / "legacy-bootstrap.sh"
        cls.launcher_path = root / "scripts" / "legacy-bootstrap-launcher.sh"
        cls.source = cls.path.read_text(encoding="utf-8")
        cls.launcher_source = cls.launcher_path.read_text(encoding="utf-8")
        cls.root = root

    @classmethod
    def git_show(cls, revision: str, path: str, *, check: bool = True) -> str:
        completed = subprocess.run(
            ["git", "-C", str(cls.root), "show", f"{revision}:{path}"],
            check=False,
            capture_output=True,
            text=True,
        )
        if check:
            if completed.returncode != 0:
                raise AssertionError(completed.stderr)
        return completed.stdout

    @classmethod
    def legacy_service_template(cls, revision: str, path: str) -> str:
        """Return the service heredoc emitted by the actual historical tag."""
        installer = cls.git_show(revision, path)
        match = re.search(
            r'cat > "\$TMP_SERVICE" <<SERVICE\n(?P<template>.*?)\nSERVICE',
            installer,
            re.DOTALL,
        )
        if match is None:
            raise AssertionError(f"{revision} has no systemd service template")
        return match.group("template")

    @classmethod
    def render_legacy_service(
        cls,
        revision: str,
        command: str,
        host: str,
        port: str,
        install_dir: str,
    ) -> str:
        """Materialize a unit from the historical installer's own heredoc."""
        if revision == "v0.3.4":
            template = cls.legacy_service_template(revision, "scripts/install-raspberry-pi.sh")
            replacements = {
                "TARGET_USER": "legacyuser",
                "INSTALL_DIR": install_dir,
                "TARGET_HOME": "/home/legacyuser",
                "SERVICE_PATH": f"{install_dir}/.venv/bin:/usr/bin:/bin",
                "ROOT_HELPER_PATH": "/usr/local/libexec/gp-control-plane/gp-root-helper",
                "ZAPRET_DIR": "/opt/zapret2",
                "WEB_ENV_FILE": "/etc/default/gp-control-plane-web",
                "WEB_HOST": host,
                "WEB_PORT": port,
                "SERVICE_MEMORY_HIGH": "512M",
                "SERVICE_MEMORY_MAX": "1G",
            }
        elif revision == "v0.3.5-alpha.4":
            template = cls.legacy_service_template(revision, "scripts/install-linux.sh")
            exec_start = f"{install_dir}/.venv/bin/gp-control-plane {command} --host {host} --port {port}"
            if command == "web":
                exec_start += " --core-url http://127.0.0.1:8081"
            replacements = {
                "description": "GP Strategy Finder Core API" if command == "core" else "GP Strategy Finder Web UI",
                "after_line": "network-online.target",
                "wants_line": "network-online.target",
                "TARGET_USER": "legacyuser",
                "INSTALL_DIR": install_dir,
                "TARGET_HOME": "/home/legacyuser",
                "SERVICE_PATH": f"{install_dir}/.venv/bin:/usr/bin:/bin",
                "privileged_env": "Environment=GP_ROOT_HELPER=/usr/local/libexec/gp-control-plane/gp-root-helper\nEnvironment=GP_ZAPRET_DIR=/opt/zapret2" if command == "core" else "",
                "env_file": "/etc/default/gp-control-plane-core" if command == "core" else "/etc/default/gp-control-plane-web",
                "exec_start": exec_start,
                "SERVICE_MEMORY_HIGH": "512M",
                "SERVICE_MEMORY_MAX": "1G",
            }
        else:
            raise AssertionError(f"unexpected legacy revision: {revision}")
        for name, value in replacements.items():
            template = template.replace(f"${name}", value)
        return template

    @staticmethod
    def posix_shell() -> str | None:
        shell = shutil.which("sh")
        if shell is not None:
            return shell
        git_shell = Path(r"C:\Program Files\Git\bin\sh.exe")
        return str(git_shell) if git_shell.is_file() else None

    @staticmethod
    def bash_shell() -> str | None:
        shell = shutil.which("bash")
        if shell is not None:
            return shell
        git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
        return str(git_bash) if git_bash.is_file() else None

    def test_is_a_standalone_posix_script_with_exact_typed_arguments(self) -> None:
        self.assertTrue(self.source.startswith("#!/bin/sh\n"))
        self.assertIn('[ "$#" -eq 6 ]', self.source)
        self.assertIn('[ "$1" = --bootstrap-sha ] && [ "$3" = --candidate-ref ] && [ "$5" = --candidate-sha ]', self.source)
        self.assertIn('[ "$CANDIDATE_REF" = refs/heads/dev ]', self.source)
        self.assertIn('is_sha256 "$BOOTSTRAP_SHA"', self.source)
        self.assertIn('is_commit_sha "$CANDIDATE_SHA"', self.source)
        self.assertIn("require_trusted_stage", self.source)
        self.assertIn('"${LEGACY_BOOTSTRAP_STAGED_PATH:-}" = "$0"', self.source)
        self.assertIn('"${LEGACY_BOOTSTRAP_STAGED_SHA:-}" = "$BOOTSTRAP_SHA"', self.source)
        self.assertIn('/usr/bin/sha256sum -- "$0"', self.source)
        self.assertLess(self.source.index("require_trusted_stage"), self.source.index('[ "$(/usr/bin/id -u)" -eq 0 ]'))

    def test_launcher_stages_and_rehashes_the_payload_before_root_shell_runs(self) -> None:
        launcher = self.launcher_source
        self.assertTrue(launcher.startswith("#!/bin/sh\n"))
        self.assertIn("readonly TRUSTED_PATH='/usr/sbin:/usr/bin:/sbin:/bin'", launcher)
        for utility in ("SUDO", "ENV", "INSTALL", "SHA256SUM", "AWK", "STAT", "READLINK", "TEST", "SH"):
            with self.subTest(utility=utility):
                self.assertRegex(launcher, rf"readonly {utility}='/")
        self.assertIn('"$SUDO" "$ENV" -i "PATH=$TRUSTED_PATH" "$@"', launcher)
        self.assertIn('trusted_root "$INSTALL" -T -m 0700 -o root -g root -- "$PAYLOAD" "$STAGED_PAYLOAD"', launcher)
        self.assertIn('STAGED_DIRECTORY="$STAGE_ROOT/payload-$BOOTSTRAP_SHA-$stage_suffix"', launcher)
        self.assertIn('STAGED_PAYLOAD="$STAGED_DIRECTORY/legacy-bootstrap.sh"', launcher)
        self.assertIn('"LEGACY_BOOTSTRAP_STAGED_DIR=$STAGED_DIRECTORY"', launcher)
        self.assertIn('actual_staged_sha="$(trusted_root "$SHA256SUM" -- "$STAGED_PAYLOAD" | "$AWK" \'{print $1}\')"', launcher)
        root_exec = launcher.rindex('"$SUDO" "$ENV" -i "PATH=$TRUSTED_PATH"')
        staged_hash = launcher.index('actual_staged_sha=')
        self.assertLess(staged_hash, root_exec)
        self.assertNotIn('exec "$SUDO" "$ENV" -i', launcher)
        self.assertNotIn('"$SH" "$PAYLOAD"', launcher)

    def test_launcher_hash_failure_cleanup_removes_only_the_exact_staged_payload_directory(self) -> None:
        shell = self.posix_shell()
        if shell is None:
            self.skipTest("a POSIX sh interpreter is required")

        definitions = self.launcher_source.split('[ "$#" -eq 6 ] || { usage; exit 2; }', 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            stage_root = tmp_path / "stage-root"
            bootstrap_sha = "a" * 64
            stage_directory = stage_root / f"payload-{bootstrap_sha}-123"
            stage_directory.mkdir(parents=True)
            staged_payload = stage_directory / "legacy-bootstrap.sh"
            staged_payload.write_text("staged payload", encoding="utf-8")
            harness = tmp_path / "launcher-cleanup-harness.sh"
            harness.write_text(
                definitions.replace("readonly STAGE_ROOT='/var/lib/gp-control-plane/legacy-bootstrap/payloads'", "STAGE_ROOT='stage-root'")
                + '''\nBOOTSTRAP_SHA="$1"
STAGED_DIRECTORY="$STAGE_ROOT/$2"
STAGED_PAYLOAD="$STAGE_ROOT/$3"
trusted_root() {
  if [ "$1" = "$TEST" ]; then
    return 0
  elif [ "$1" = "$STAT" ]; then
    printf '%s\\n' '0:0:700:directory'
  else
    "$@"
  fi
}
cleanup_staged_payload "$4"
''',
                encoding="utf-8",
            )
            completed = subprocess.run(
                [shell, harness.name, bootstrap_sha, f"payload-{bootstrap_sha}-123", f"payload-{bootstrap_sha}-123/legacy-bootstrap.sh", "17"],
                cwd=tmp_path,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 17, completed.stderr)
            self.assertFalse(stage_directory.exists())
            self.assertTrue(stage_root.exists())

    def test_root_wrapper_commits_success_only_after_removing_staged_payload(self) -> None:
        shell = self.posix_shell()
        if shell is None:
            self.skipTest("a POSIX sh interpreter is required")

        definitions = self.source.split('[ "$#" -eq 6 ] || { usage; exit 2; }', 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            stage_root = tmp_path / "stage-root"
            bootstrap_sha = "b" * 64
            stage_directory = stage_root / f"payload-{bootstrap_sha}-456"
            stage_directory.mkdir(parents=True)
            stat = tmp_path / "stat"
            stat.write_text("#!/bin/sh\nprintf '%s\\n' '0:0:700'\n", encoding="utf-8", newline="\n")
            stat.chmod(0o700)
            harness = stage_directory / "legacy-bootstrap.sh"
            harness.write_text(
                definitions.replace("readonly STAGE_ROOT='/var/lib/gp-control-plane/legacy-bootstrap/payloads'", "STAGE_ROOT='stage-root'")
                .replace("/usr/bin/stat", "./stat")
                + f'''\nBOOTSTRAP_SHA='{bootstrap_sha}'
LEGACY_BOOTSTRAP_STAGED_PATH="$0"
LEGACY_BOOTSTRAP_STAGED_DIR="$(dirname -- "$0")"
JOURNAL_FILE="$1"
TRANSACTION_ID='test-transaction'
PAYLOAD_LIFECYCLE_READY=1
root_wrapper_on_exit 0
''',
                encoding="utf-8",
            )
            harness.chmod(0o700)
            journal = tmp_path / "journal"
            completed = subprocess.run(
                [shell, f"stage-root/payload-{bootstrap_sha}-456/legacy-bootstrap.sh", str(journal)],
                cwd=tmp_path,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(stage_directory.exists())
            self.assertEqual(journal.read_text(encoding="utf-8").splitlines(), ["phase=committed", "status=success"])

    def test_root_wrapper_fails_closed_when_staged_payload_cleanup_fails(self) -> None:
        shell = self.posix_shell()
        if shell is None:
            self.skipTest("a POSIX sh interpreter is required")

        definitions = self.source.split('[ "$#" -eq 6 ] || { usage; exit 2; }', 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            stage_root = tmp_path / "stage-root"
            bootstrap_sha = "c" * 64
            stage_directory = stage_root / f"payload-{bootstrap_sha}-789"
            stage_directory.mkdir(parents=True)
            (stage_directory / "blocker").write_text("not owned by cleanup", encoding="utf-8")
            stat = tmp_path / "stat"
            stat.write_text("#!/bin/sh\nprintf '%s\\n' '0:0:700'\n", encoding="utf-8", newline="\n")
            stat.chmod(0o700)
            harness = stage_directory / "legacy-bootstrap.sh"
            harness.write_text(
                definitions.replace("readonly STAGE_ROOT='/var/lib/gp-control-plane/legacy-bootstrap/payloads'", "STAGE_ROOT='stage-root'")
                .replace("/usr/bin/stat", "./stat")
                + f'''\nBOOTSTRAP_SHA='{bootstrap_sha}'
LEGACY_BOOTSTRAP_STAGED_PATH="$0"
LEGACY_BOOTSTRAP_STAGED_DIR="$(dirname -- "$0")"
JOURNAL_FILE="$1"
PAYLOAD_LIFECYCLE_READY=1
root_wrapper_on_exit 0
''',
                encoding="utf-8",
            )
            harness.chmod(0o700)
            journal = tmp_path / "journal"
            completed = subprocess.run(
                [shell, f"stage-root/payload-{bootstrap_sha}-789/legacy-bootstrap.sh", str(journal)],
                cwd=tmp_path,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 1, completed.stderr)
            self.assertIn("cannot remove root staged payload", completed.stderr)
            self.assertTrue(stage_directory.exists())
            self.assertFalse(harness.exists())
            entries = journal.read_text(encoding="utf-8").splitlines()
            self.assertEqual(entries, ["phase=error", "status=failed", "error=rollback-not-required"])
            self.assertEqual(sum(entry.startswith("status=") for entry in entries), 1)
            self.assertNotIn("status=success", entries)
            self.assertTrue((stage_directory / "blocker").exists())

    def test_modified_payload_and_hostile_path_fail_before_sudo_or_payload_execution(self) -> None:
        shell = self.posix_shell()
        if shell is None:
            self.skipTest("a POSIX sh interpreter is required")

        availability = subprocess.run([shell, "-c", "test -x /usr/bin/sha256sum && test -x /usr/bin/awk"], check=False)
        if availability.returncode != 0:
            self.skipTest("trusted hash utilities are unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            launcher = tmp_path / "legacy-bootstrap-launcher.sh"
            payload = tmp_path / "legacy-bootstrap.sh"
            launcher.write_text(self.launcher_source, encoding="utf-8", newline="\n")
            payload.write_text("#!/bin/sh\nprintf 'payload-ran\\n' > payload-ran\n", encoding="utf-8", newline="\n")
            expected_sha = hashlib.sha256(payload.read_bytes()).hexdigest()
            payload.write_text("#!/bin/sh\nprintf 'payload-ran\\n' > payload-ran\nprintf 'changed\\n'\n", encoding="utf-8", newline="\n")

            hostile_bin = tmp_path / "hostile-bin"
            hostile_bin.mkdir()
            for name in ("sha256sum", "awk", "sudo"):
                tool = hostile_bin / name
                tool.write_text("#!/bin/sh\nprintf 'hostile-%s\\n' \"$0\" >&2\nexit 99\n", encoding="utf-8", newline="\n")
                tool.chmod(0o700)

            completed = subprocess.run(
                [
                    shell,
                    str(launcher),
                    "--bootstrap-sha",
                    expected_sha,
                    "--candidate-ref",
                    "refs/heads/dev",
                    "--candidate-sha",
                    "a" * 40,
                ],
                cwd=tmp_path,
                env={"PATH": str(hostile_bin)},
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("bootstrap payload SHA256 does not match", completed.stderr)
            self.assertNotIn("hostile-", completed.stderr)
            self.assertFalse((tmp_path / "payload-ran").exists())

    def test_invalid_argument_shapes_fail_before_root_or_network_work(self) -> None:
        shell = self.posix_shell()
        if shell is None:
            self.skipTest("a POSIX sh interpreter is required")

        for arguments in ([], ["--bootstrap-sha", "a" * 64], ["--bootstrap-sha", "A" * 64, "--candidate-ref", "refs/heads/dev", "--candidate-sha", "a" * 40]):
            with self.subTest(arguments=arguments):
                completed = subprocess.run(
                    [shell, str(self.path), *arguments],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(completed.returncode, 2, completed.stderr)
                self.assertNotIn("canonical", completed.stderr.lower())

    def test_privileged_transition_paths_are_fixed_and_allowlisted(self) -> None:
        expected_paths = {
            "ROOT_HELPER": "/usr/local/libexec/gp-control-plane/gp-root-helper",
            "ROOT_HELPER_CONFIG": "/etc/default/gp-control-plane-root-helper",
            "SUDOERS_PATH": "/etc/sudoers.d/gp-control-plane-root-helper",
            "RUN_REGISTRY_DIR": "/run/gp-control-plane/runs",
            "INSTALL_PROFILE": "/etc/default/gp-control-plane-install-profile",
            "CORE_ENV_FILE": "/etc/default/gp-control-plane-core",
            "WEB_ENV_FILE": "/etc/default/gp-control-plane-web",
            "CORE_UNIT": "/etc/systemd/system/gp-control-plane-core.service",
            "WEB_UNIT": "/etc/systemd/system/gp-control-plane-web.service",
        }
        for name, path in expected_paths.items():
            with self.subTest(name=name):
                self.assertIn(f"readonly {name}='{path}'", self.source)
        self.assertIn("validate_transition_surface()", self.source)
        self.assertIn("ensure_journal_root()", self.source)
        self.assertIn("legacy bootstrap journal must be root-owned mode 0700", self.source)
        for command in ("checkout", "reset", "clean", "pull", "merge", "tag"):
            self.assertNotIn(f"clean_git {command}", self.source)
            self.assertNotIn(f"git {command}", self.source)

    def test_candidate_is_verified_and_fetched_from_the_fixed_canonical_source(self) -> None:
        self.assertIn("readonly CANONICAL_UPSTREAM='https://github.com/balbomush/GP-access-control-plane.git'", self.source)
        self.assertIn('clean_git ls-remote "$CANONICAL_UPSTREAM" "$CANDIDATE_REF"', self.source)
        self.assertIn('clean_git -C "$SOURCE_REPO" fetch --no-tags "$CANONICAL_UPSTREAM" "$CANDIDATE_REF"', self.source)
        self.assertIn("rev-parse --verify 'FETCH_HEAD^{commit}'", self.source)
        self.assertIn('[ "$remote_sha" = "$CANDIDATE_SHA" ]', self.source)
        self.assertIn('[ "$fetched_sha" = "$CANDIDATE_SHA" ]', self.source)
        self.assertIn('show "$CANDIDATE_SHA:scripts/gp-root-helper.sh"', self.source)

    def test_journal_has_a_terminal_success_or_rollback_lifecycle_without_secret_payloads(self) -> None:
        phases = [
            "journal_phase started",
            "journal_phase source-verified",
            "journal_phase backup-created",
            "journal_phase mutation-started",
        ]
        positions = [self.source.index(phase) for phase in phases]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("journal_phase rollback-started", self.source)
        self.assertIn("journal_phase rolled-back", self.source)
        self.assertIn("journal_phase rollback-failed", self.source)
        self.assertIn("trap 'root_wrapper_on_exit $?' 0", self.source)
        self.assertIn("root_wrapper_on_exit()", self.source)
        self.assertIn('journal_value bootstrap_sha "$BOOTSTRAP_SHA"', self.source)
        self.assertIn('journal_value baseline_sha "$baseline_sha"', self.source)
        self.assertIn('journal_value candidate_ref "$CANDIDATE_REF"', self.source)
        self.assertIn('journal_value candidate_sha "$CANDIDATE_SHA"', self.source)
        self.assertIn('journal_value backup_path "$BACKUP_DIR"', self.source)
        self.assertIn("journal_phase error", self.source)
        self.assertIn("journal_terminal_failure failed rollback-succeeded", self.source)
        self.assertIn("journal_terminal_failure error rollback-failed", self.source)
        wrapper = re.search(r"^root_wrapper_on_exit\(\) \{\n(?P<body>.*?)^\}$", self.source, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(wrapper)
        wrapper_body = wrapper.group("body")  # type: ignore[union-attr]
        self.assertLess(wrapper_body.index("if ! cleanup_staged_payload"), wrapper_body.index("journal_phase committed"))
        self.assertLess(wrapper_body.index("journal_phase committed"), wrapper_body.index("journal_value status success"))
        self.assertIn("TERMINAL_STATUS_WRITTEN=1", wrapper_body)
        journal_function = re.search(r"^journal_phase\(\) \{\n(?P<body>.*?)^\}$", self.source, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(journal_function)
        self.assertIn("fixed lifecycle tokens only", journal_function.group("body"))  # type: ignore[union-attr]
        self.assertNotIn("cat \"$ROOT_HELPER_CONFIG\"", self.source)

    def test_failure_after_backup_restores_only_transition_surface_then_services(self) -> None:
        rollback = re.search(r"^rollback_after_failure\(\) \{\n(?P<body>.*?)^\}$", self.source, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(rollback)
        body = rollback.group("body")  # type: ignore[union-attr]
        self.assertIn("journal_phase rollback-started", body)
        self.assertIn("restore_transition_surface && restore_service_state", body)
        self.assertLess(body.index("restore_transition_surface"), body.index("restore_service_state"))
        self.assertIn('if [ "$exit_status" -ne 0 ] && [ "$BACKUP_READY" -eq 1 ] && [ "$COMMITTED" -eq 0 ]; then', self.source)
        self.assertIn('bash "$ROOT_HELPER" check >/dev/null 2>&1 || fail', self.source)

    def test_failure_after_backup_restores_service_topology_without_stopping_masked_units(self) -> None:
        shell = self.posix_shell()
        if shell is None:
            self.skipTest("a POSIX sh interpreter is required")

        definitions = self.source.split('[ "$#" -eq 6 ] || { usage; exit 2; }', 1)[0]
        scenarios = (
            (
                "runtime-enable-and-disable",
                "core|active|enabled-runtime\nweb|inactive|disabled-runtime\n",
                [
                    "daemon-reload",
                    "enable --runtime gp-control-plane-core.service",
                    "start gp-control-plane-core.service",
                    "is-enabled gp-control-plane-core.service",
                    "is-active --quiet gp-control-plane-core.service",
                    "disable --runtime gp-control-plane-web.service",
                    "stop gp-control-plane-web.service",
                    "is-enabled gp-control-plane-web.service",
                    "is-active --quiet gp-control-plane-web.service",
                ],
                {
                    "core": ("active", "enabled-runtime", None),
                    "web": ("inactive", "disabled-runtime", None),
                },
            ),
            (
                "persistent-mask",
                "core|inactive|masked\nweb|active|enabled-runtime\n",
                [
                    "daemon-reload",
                    "mask gp-control-plane-core.service",
                    "is-enabled gp-control-plane-core.service",
                    "is-active --quiet gp-control-plane-core.service",
                    "enable --runtime gp-control-plane-web.service",
                    "start gp-control-plane-web.service",
                    "is-enabled gp-control-plane-web.service",
                    "is-active --quiet gp-control-plane-web.service",
                ],
                {
                    "core": ("inactive", "masked", "/dev/null"),
                    "web": ("active", "enabled-runtime", None),
                },
            ),
            (
                "runtime-mask",
                "core|inactive|masked-runtime\nweb|active|enabled-runtime\n",
                [
                    "daemon-reload",
                    "mask --runtime gp-control-plane-core.service",
                    "is-enabled gp-control-plane-core.service",
                    "is-active --quiet gp-control-plane-core.service",
                    "enable --runtime gp-control-plane-web.service",
                    "start gp-control-plane-web.service",
                    "is-enabled gp-control-plane-web.service",
                    "is-active --quiet gp-control-plane-web.service",
                ],
                {
                    "core": ("inactive", "masked-runtime", None),
                    "web": ("active", "enabled-runtime", None),
                },
            ),
        )
        for name, manifest, expected_calls, expected_state in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                harness = tmp_path / "rollback-harness.sh"
                unit_dir = tmp_path / "units"
                state_dir = tmp_path / "state"
                unit_dir.mkdir()
                state_dir.mkdir()
                harness.write_text(
                    '''UNIT_DIR="$5"
'''
                    + definitions.replace("readonly CORE_UNIT='/etc/systemd/system/gp-control-plane-core.service'", 'CORE_UNIT="$UNIT_DIR/gp-control-plane-core.service"').replace("readonly WEB_UNIT='/etc/systemd/system/gp-control-plane-web.service'", 'WEB_UNIT="$UNIT_DIR/gp-control-plane-web.service"')
                    + '''\nSYSTEMCTL_LOG="$1"
SERVICE_MANIFEST="$2"
JOURNAL_FILE="$3"
STATE_DIR="$4"
UNIT_DIR="$5"
BACKUP_READY=1
COMMITTED=0
ROLLBACK_RUNNING=0
TERMINAL_STATUS_WRITTEN=0
ERROR_PHASE_WRITTEN=0
restore_transition_surface() { return 0; }
safe_parent_chain() { :; }
service_key() {
  case "$1" in
    gp-control-plane-core.service) printf '%s\\n' core ;;
    gp-control-plane-web.service) printf '%s\\n' web ;;
    *) return 1 ;;
  esac
}
set_service_value() {
  printf '%s\\n' "$3" > "$STATE_DIR/$1.$2"
}
service_value() {
  cat "$STATE_DIR/$1.$2"
}
systemctl() {
  printf '%s\\n' "$*" >> "$SYSTEMCTL_LOG"
  case "$1" in
    daemon-reload) return 0 ;;
    enable|disable|mask)
      action="$1"
      if [ "${2:-}" = --runtime ]; then runtime=1; service="$3"; else runtime=0; service="$2"; fi
      key="$(service_key "$service")" || return 1
      case "$action:$runtime" in
        enable:0) set_service_value "$key" enabled enabled ;;
        enable:1) set_service_value "$key" enabled enabled-runtime ;;
        disable:0) set_service_value "$key" enabled disabled ;;
        disable:1) set_service_value "$key" enabled disabled-runtime ;;
        mask:0)
          set_service_value "$key" enabled masked
          set_service_value "$key" link /dev/null
          ;;
        mask:1)
          set_service_value "$key" enabled masked-runtime
          set_service_value "$key" link absent
          ;;
      esac
      ;;
    start)
      key="$(service_key "$2")" || return 1
      case "$(service_value "$key" enabled)" in masked|masked-runtime) return 1 ;; esac
      set_service_value "$key" active active
      ;;
    stop)
      key="$(service_key "$2")" || return 1
      case "$(service_value "$key" enabled)" in masked|masked-runtime) return 1 ;; esac
      set_service_value "$key" active inactive
      ;;
    is-enabled)
      key="$(service_key "$2")" || return 1
      service_value "$key" enabled
      ;;
    is-active)
      key="$(service_key "$3")" || return 1
      [ "$(service_value "$key" active)" = active ]
      ;;
  esac
}
on_exit 1
''',
                    encoding="utf-8",
                )
                systemctl_log = tmp_path / "systemctl.log"
                service_manifest = tmp_path / "services.manifest"
                journal = tmp_path / "journal"
                service_manifest.write_text(manifest, encoding="utf-8", newline="\n")
                for service, (active, enabled, link) in (("core", ("inactive", "disabled", None)), ("web", ("inactive", "disabled", None))):
                    (state_dir / f"{service}.active").write_text(active + "\n", encoding="utf-8")
                    (state_dir / f"{service}.enabled").write_text(enabled + "\n", encoding="utf-8")
                    (state_dir / f"{service}.link").write_text((link or "absent") + "\n", encoding="utf-8")
                completed = subprocess.run(
                    [shell, str(harness), str(systemctl_log), str(service_manifest), str(journal), str(state_dir), str(unit_dir)],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(completed.returncode, 1, completed.stderr)
                self.assertEqual(
                    systemctl_log.read_text(encoding="utf-8").splitlines(),
                    expected_calls,
                    journal.read_text(encoding="utf-8"),
                )
                calls = systemctl_log.read_text(encoding="utf-8").splitlines()
                self.assertNotIn("enable gp-control-plane-core.service", calls)
                self.assertNotIn("enable gp-control-plane-web.service", calls)
                self.assertNotIn("stop gp-control-plane-core.service", calls)
                self.assertEqual(journal.read_text(encoding="utf-8").splitlines()[-2:], ["status=failed", "error=rollback-succeeded"])
                self.assertEqual(
                    journal.read_text(encoding="utf-8").splitlines().count("status=failed")
                    + journal.read_text(encoding="utf-8").splitlines().count("status=error"),
                    1,
                )
                for service, (active, enabled, link) in expected_state.items():
                    with self.subTest(service=service):
                        self.assertEqual((state_dir / f"{service}.active").read_text(encoding="utf-8").strip(), active)
                        self.assertEqual((state_dir / f"{service}.enabled").read_text(encoding="utf-8").strip(), enabled)
                        self.assertEqual(
                            (state_dir / f"{service}.link").read_text(encoding="utf-8").strip(),
                            link or "absent",
                        )

    def test_masked_active_service_is_rejected_during_snapshot_before_mutation(self) -> None:
        shell = self.posix_shell()
        if shell is None:
            self.skipTest("a POSIX sh interpreter is required")

        definitions = self.source.split('[ "$#" -eq 6 ] || { usage; exit 2; }', 1)[0]
        definitions = definitions.replace("readonly CORE_UNIT='/etc/systemd/system/gp-control-plane-core.service'", 'CORE_UNIT="$TMP/gp-control-plane-core.service"')
        definitions = definitions.replace("readonly WEB_UNIT='/etc/systemd/system/gp-control-plane-web.service'", 'WEB_UNIT="$TMP/gp-control-plane-web.service"')
        for enabled, persistent_link in (("masked", True), ("masked-runtime", False)):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                harness = tmp_path / "masked-active-harness.sh"
                harness.write_text(
                    '''TMP="$1"
'''
                    + definitions
                    + f'''\nSERVICE_MANIFEST="$2"
safe_parent_chain() {{ :; }}
systemctl() {{
  case "$1" in
    is-active) return 0 ;;
    is-enabled) printf '%s\\n' '{enabled}' ;;
  esac
}}
{('ln -s /dev/null "$CORE_UNIT"' if persistent_link else 'touch "$CORE_UNIT"')}
snapshot_service_state "$CORE_SERVICE" core
''',
                    encoding="utf-8",
                )
                manifest = tmp_path / "services.manifest"
                completed = subprocess.run([shell, str(harness), str(tmp_path), str(manifest)], check=False, capture_output=True, text=True)
                self.assertNotEqual(completed.returncode, 0, completed.stderr)
                self.assertFalse(manifest.exists())

    def test_unsupported_service_enablement_is_rejected_during_snapshot(self) -> None:
        shell = self.posix_shell()
        if shell is None:
            self.skipTest("a POSIX sh interpreter is required")

        definitions = self.source.split('[ "$#" -eq 6 ] || { usage; exit 2; }', 1)[0]
        definitions = definitions.replace("readonly CORE_UNIT='/etc/systemd/system/gp-control-plane-core.service'", 'CORE_UNIT="$TMP/gp-control-plane-core.service"')
        definitions = definitions.replace("readonly WEB_UNIT='/etc/systemd/system/gp-control-plane-web.service'", 'WEB_UNIT="$TMP/gp-control-plane-web.service"')
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            harness = tmp_path / "snapshot-harness.sh"
            harness.write_text(
                '''TMP="$1"
'''
                + definitions
                + '''\nSERVICE_MANIFEST="$2"
safe_parent_chain() { :; }
touch "$CORE_UNIT" "$WEB_UNIT"
systemctl() {
  case "$1" in
    is-active) return 3 ;;
    is-enabled) printf '%s\\n' static ;;
  esac
}
snapshot_services
''',
                encoding="utf-8",
            )
            manifest = tmp_path / "services.manifest"
            completed = subprocess.run([shell, str(harness), str(tmp_path), str(manifest)], check=False, capture_output=True, text=True)
            self.assertNotEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(manifest.exists())

    def test_journal_commits_terminal_failure_only_after_rollback_result(self) -> None:
        shell = self.posix_shell()
        if shell is None:
            self.skipTest("a POSIX sh interpreter is required")

        definitions = self.source.split('[ "$#" -eq 6 ] || { usage; exit 2; }', 1)[0]
        for rollback_succeeds, expected_terminal in ((True, ["status=failed", "error=rollback-succeeded"]), (False, ["status=error", "error=rollback-failed"])):
            with self.subTest(rollback_succeeds=rollback_succeeds), tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                harness = tmp_path / "journal-harness.sh"
                restore_result = "0" if rollback_succeeds else "1"
                harness.write_text(
                    definitions
                    + f'''\nJOURNAL_FILE="$1"
BACKUP_READY=1
COMMITTED=0
ROLLBACK_RUNNING=0
TERMINAL_STATUS_WRITTEN=0
ERROR_PHASE_WRITTEN=0
restore_transition_surface() {{ return 0; }}
restore_service_state() {{ return {restore_result}; }}
journal_nonterminal_error
on_exit 1
''',
                    encoding="utf-8",
                )
                journal = tmp_path / "journal"
                completed = subprocess.run([shell, str(harness), str(journal)], check=False, capture_output=True, text=True)
                self.assertEqual(completed.returncode, 1, completed.stderr)
                entries = journal.read_text(encoding="utf-8").splitlines()
                self.assertEqual(entries.count("status=failed") + entries.count("status=error"), 1)
                self.assertNotIn("status=success", entries)
                self.assertEqual(entries[-2:], expected_terminal)
                self.assertLess(entries.index("phase=error"), entries.index("phase=rollback-started"))
                self.assertLess(entries.index("phase=rollback-started"), entries.index(expected_terminal[0]))
                rollback_phase = "phase=rolled-back" if rollback_succeeds else "phase=rollback-failed"
                self.assertLess(entries.index(rollback_phase), entries.index(expected_terminal[0]))

    def test_web_endpoint_is_derived_from_the_fixed_web_unit(self) -> None:
        self.assertIn("readonly WEB_UNIT='/etc/systemd/system/gp-control-plane-web.service'", self.source)
        parser = re.search(r"^read_fixed_unit_value\(\) \{\n(?P<body>.*?)^\}$", self.source, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(parser)
        parser_body = parser.group("body")  # type: ignore[union-attr]
        self.assertIn("/^\\[Service\\][[:space:]]*$/", parser_body)
        self.assertIn('index($0, key "=") == 1', parser_body)
        self.assertIn("found == 1", parser_body)

        profile = re.search(r"^write_legacy_install_profile\(\) \{\n(?P<body>.*?)^\}$", self.source, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(profile)
        profile_body = profile.group("body")  # type: ignore[union-attr]
        expected_values = (
            "GP_INSTALL_USER='$LEGACY_INSTALL_USER'",
            "GP_INSTALL_DIR='$LEGACY_INSTALL_DIR'",
            "GP_STATE_DIR='$LEGACY_INSTALL_DIR/build/state'",
            "GP_SERVICE_NAME='gp-control-plane-web.service'",
            "GP_CORE_SERVICE_NAME='gp-control-plane-core.service'",
            "GP_INSTALL_WEB='$LEGACY_INSTALL_WEB'",
            "$LEGACY_WEB_PROFILE",
            "GP_WEB_ENV_FILE='/etc/default/gp-control-plane-web'",
            "GP_CORE_HOST='$LEGACY_CORE_HOST'",
            "GP_CORE_PORT='$LEGACY_CORE_PORT'",
            "GP_CORE_URL='$LEGACY_CORE_URL'",
            "GP_CORE_ENV_FILE='/etc/default/gp-control-plane-core'",
            "GP_ZAPRET_DIR='/opt/zapret2'",
            "GP_ROOT_HELPER_PATH='/usr/local/libexec/gp-control-plane/gp-root-helper'",
            "GP_ROOT_HELPER_CONFIG='/etc/default/gp-control-plane-root-helper'",
            "GP_ROOT_HELPER_RUN_DIR='/run/gp-control-plane/runs'",
            "GP_SUDOERS_PATH='/etc/sudoers.d/gp-control-plane-root-helper'",
        )
        for value in expected_values:
            with self.subTest(value=value):
                self.assertIn(value, profile_body)

        self.assertNotIn("GP_WEB_HOST='0.0.0.0'", profile_body)
        self.assertNotIn("GP_WEB_PORT='8080'", profile_body)

        endpoint = re.search(r"^derive_legacy_web_endpoint\(\) \{\n(?P<body>.*?)^\}$", self.source, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(endpoint)
        endpoint_body = endpoint.group("body")  # type: ignore[union-attr]
        self.assertIn('require_root_owned_fixed_unit "$WEB_UNIT" Web', endpoint_body)
        self.assertIn('read_fixed_unit_value "$WEB_UNIT" ExecStart', endpoint_body)
        self.assertIn('gp-control-plane web --host ', endpoint_body)
        self.assertIn('validate_legacy_web_endpoint "$LEGACY_WEB_HOST" "$LEGACY_WEB_PORT"', endpoint_body)

    def test_actual_legacy_tag_service_templates_preserve_web_and_headless_profiles(self) -> None:
        shell = self.posix_shell()
        if shell is None:
            self.skipTest("a POSIX sh interpreter is required")

        definitions = self.source.split('[ "$#" -eq 6 ] || { usage; exit 2; }', 1)[0]
        definitions = definitions.replace("readonly CORE_UNIT='/etc/systemd/system/gp-control-plane-core.service'", 'CORE_UNIT="$TMP/core.service"')
        definitions = definitions.replace("readonly WEB_UNIT='/etc/systemd/system/gp-control-plane-web.service'", 'WEB_UNIT="$TMP/web.service"')
        definitions = definitions.replace("readonly CORE_ENV_FILE='/etc/default/gp-control-plane-core'", 'CORE_ENV_FILE="$TMP/core.env"')
        definitions = definitions.replace("readonly INSTALL_PROFILE='/etc/default/gp-control-plane-install-profile'", 'INSTALL_PROFILE="$TMP/install-profile"')

        for revision, install_web, web_host, web_port in (
            ("v0.3.4", "on", "0.0.0.0", "8080"),
            ("v0.3.4", "on", "127.0.0.1", "9090"),
            ("v0.3.5-alpha.4", "on", "127.0.0.1", "9090"),
            ("v0.3.5-alpha.4", "off", None, None),
        ):
            with self.subTest(revision=revision, install_web=install_web, web_host=web_host, web_port=web_port), tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                harness = tmp_path / "profile-harness.sh"
                create_core = revision == "v0.3.5-alpha.4"
                install_dir = "$TMP/app"
                core_env = "" if not create_core else f'''\ncat > "$CORE_ENV_FILE" <<ENV
GP_INSTALL_DIR='$TMP/app'
GP_STATE_DIR='$TMP/app/build/state'
GP_INSTALL_WEB='{install_web}'
ENV
cat > "$CORE_UNIT" <<UNIT
{self.render_legacy_service(revision, "core", "127.0.0.1", "8081", install_dir)}
UNIT
'''
                web_unit = "" if install_web == "off" else f'''\ncat > "$WEB_UNIT" <<UNIT
{self.render_legacy_service(revision, "web", web_host, web_port, install_dir)}
UNIT
'''
                harness.write_text(
                    f'''TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
{definitions}
require_root_owned_fixed_unit() {{ :; }}
validate_legacy_install_user() {{ :; }}
stat() {{
  if [ "$1" = -c ] && [ "$2" = '%u:%g' ] && [ "$3" = "$CORE_ENV_FILE" ]; then
    printf '%s\\n' '0:0'
  else
    command stat "$@"
  fi
}}
mkdir -p "$TMP/app/build/state"
STAGED_INSTALL_PROFILE="$TMP/staged-profile"
{core_env}{web_unit}
prepare_install_profile
cat "$STAGED_INSTALL_PROFILE"
''',
                    encoding="utf-8",
                )
                completed = subprocess.run([shell, str(harness)], check=False, capture_output=True, text=True)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                profile = completed.stdout
                self.assertIn(f"GP_INSTALL_WEB='{install_web}'", profile)
                if web_host is None:
                    self.assertNotIn("GP_WEB_HOST=", profile)
                    self.assertNotIn("GP_WEB_PORT=", profile)
                else:
                    self.assertIn(f"GP_WEB_HOST='{web_host}'", profile)
                    self.assertIn(f"GP_WEB_PORT='{web_port}'", profile)

    def test_profile_rejects_unsafe_or_ambiguous_unit_values_and_preserves_existing_profile(self) -> None:
        preparation = re.search(r"^prepare_install_profile\(\) \{\n(?P<body>.*?)^\}$", self.source, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(preparation)
        body = preparation.group("body")  # type: ignore[union-attr]
        self.assertIn("install_profile_is_absent", body)
        self.assertIn('read_fixed_unit_value "$WEB_UNIT" User', body)
        self.assertIn('read_fixed_unit_value "$WEB_UNIT" WorkingDirectory', body)
        self.assertIn('read_fixed_unit_value "$CORE_UNIT" User', body)
        self.assertIn('read_fixed_unit_value "$CORE_UNIT" WorkingDirectory', body)
        self.assertIn('read_core_env_value GP_INSTALL_WEB', body)
        self.assertIn("derive_legacy_core_endpoint", body)
        self.assertIn("derive_legacy_web_endpoint", body)
        self.assertIn('validate_legacy_install_user "$LEGACY_INSTALL_USER"', body)
        self.assertIn('validate_legacy_install_directory "$LEGACY_INSTALL_DIR"', body)
        self.assertIn("PROFILE_ACTION=create", body)
        self.assertIn("require_existing_install_profile", body)
        self.assertIn("PROFILE_ACTION=preserve", body)
        self.assertIn("legacy service unit User is unsafe", self.source)
        self.assertIn("legacy service unit WorkingDirectory is unsafe", self.source)
        self.assertIn("legacy service unit state directory is not a canonical directory", self.source)

        backup = self.source.index("journal_phase backup-created")
        profile_install = self.source.index('install -m 0600 -o root -g root "$STAGED_INSTALL_PROFILE" "$INSTALL_PROFILE"')
        self.assertLess(backup, profile_install)

    def test_actual_legacy_tags_prove_web_only_and_core_topologies(self) -> None:
        v034 = self.git_show("v0.3.4", "scripts/install-raspberry-pi.sh")
        v035 = self.git_show("v0.3.5-alpha.4", "scripts/install-linux.sh")
        missing_core = subprocess.run(
            ["git", "-C", str(self.root), "show", "v0.3.4:scripts/install-linux.sh"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(missing_core.returncode, 0)
        self.assertIn('WEB_HOST="${GP_WEB_HOST:-0.0.0.0}"', v034)
        self.assertIn('WEB_PORT="${GP_WEB_PORT:-8080}"', v034)
        self.assertIn("EnvironmentFile=-$WEB_ENV_FILE", v034)
        self.assertIn("ExecStart=$INSTALL_DIR/.venv/bin/gp-control-plane web --host $WEB_HOST --port $WEB_PORT", v034)
        self.assertNotIn("gp-control-plane-core.service", v034)

        for actual_fact in (
            'CORE_SERVICE_NAME="${GP_CORE_SERVICE_NAME:-gp-control-plane-core.service}"',
            'INSTALL_WEB="${GP_INSTALL_WEB:-on}"',
            'WEB_HOST="${GP_WEB_HOST:-0.0.0.0}"',
            'WEB_PORT="${GP_WEB_PORT:-8080}"',
            'CORE_HOST="${GP_CORE_HOST:-127.0.0.1}"',
            'CORE_PORT="${GP_CORE_PORT:-8081}"',
            'CORE_ENV_FILE="${GP_CORE_ENV_FILE:-/etc/default/gp-control-plane-core}"',
            'printf "GP_INSTALL_DIR=\'%s\'\\n"',
            'printf "GP_STATE_DIR=\'%s\'\\n"',
            'printf "GP_INSTALL_WEB=\'%s\'\\n"',
            'install_service_env_file "$CORE_ENV_FILE"',
            'install_systemd_service "$CORE_SERVICE_NAME" "GP Strategy Finder Core API" "core" "$CORE_HOST" "$CORE_PORT" "$CORE_ENV_FILE"',
            'User=$TARGET_USER',
            'WorkingDirectory=$INSTALL_DIR',
            'EnvironmentFile=-$env_file',
            'ExecStart=$exec_start',
        ):
            with self.subTest(actual_fact=actual_fact):
                self.assertIn(actual_fact, v035)

        self.assertNotIn("legacy_fixtures", self.source)
        core_env = re.search(r"^prepare_core_env\(\) \{\n(?P<body>.*?)^\}$", self.source, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(core_env)
        core_env_body = core_env.group("body")  # type: ignore[union-attr]
        self.assertIn("core_env_is_absent", core_env_body)
        self.assertIn("CORE_ENV_ACTION=create", core_env_body)
        self.assertIn('install -m 0600 -o root -g root /dev/null "$STAGED_CORE_ENV_FILE"', core_env_body)
        self.assertIn("CORE_ENV_ACTION=preserve", core_env_body)
        self.assertIn("existing core service metadata must be a root-owned regular file", core_env_body)
        self.assertNotIn("WEB_ENV_FILE", core_env_body)

        profile = re.search(r"^prepare_install_profile\(\) \{\n(?P<body>.*?)^\}$", self.source, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(profile)
        profile_body = profile.group("body")  # type: ignore[union-attr]
        self.assertIn("if [ -e \"$CORE_UNIT\" ] || [ -L \"$CORE_UNIT\" ]; then", profile_body)
        self.assertIn("case \"$LEGACY_INSTALL_WEB\" in on|off)", profile_body)
        self.assertIn("LEGACY_INSTALL_WEB=on", profile_body)
        self.assertNotIn("WEB_ENV_FILE", profile_body)

    def test_missing_core_env_is_created_after_backup_and_rollback_restores_its_absence(self) -> None:
        self.assertIn('snapshot_file "$CORE_ENV_FILE" core-env', self.source)
        backup = self.source.index("journal_phase backup-created")
        core_env_install = self.source.index('install -m 0600 -o root -g root "$STAGED_CORE_ENV_FILE" "$CORE_ENV_FILE"')
        root_helper_config_install = self.source.index('install -m 0644 -o root -g root "$STAGED_ROOT_HELPER_CONFIG" "$ROOT_HELPER_CONFIG"')
        self.assertLess(backup, core_env_install)
        self.assertLess(core_env_install, root_helper_config_install)
        self.assertIn("core service metadata was not created as root:root mode 0600", self.source)

        rollback = re.search(r"^restore_transition_surface\(\) \{\n(?P<body>.*?)^\}$", self.source, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(rollback)
        rollback_body = rollback.group("body")  # type: ignore[union-attr]
        self.assertIn("absent-file", rollback_body)
        self.assertIn('rm -f "$restore_target" || restore_ok=1', rollback_body)

    def test_transition_does_not_snapshot_or_mutate_legacy_web_env_or_sudoers(self) -> None:
        surface = re.search(r"^snapshot_transition_surface\(\) \{\n(?P<body>.*?)^\}$", self.source, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(surface)
        surface_body = surface.group("body")  # type: ignore[union-attr]
        self.assertNotIn("sudoers", surface_body)
        self.assertNotIn("web-env", surface_body)

        validation = re.search(r"^validate_transition_surface\(\) \{\n(?P<body>.*?)^\}$", self.source, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(validation)
        validation_body = validation.group("body")  # type: ignore[union-attr]
        self.assertNotIn("SUDOERS_PATH", validation_body)
        self.assertNotIn("WEB_ENV_FILE", validation_body)

    def test_root_helper_config_and_run_registry_are_normalized_after_backup_and_restorable(self) -> None:
        self.assertIn('snapshot_file "$ROOT_HELPER_CONFIG" root-helper-config', self.source)
        self.assertIn('snapshot_directory "$RUN_REGISTRY_DIR" run-registry', self.source)
        self.assertIn("directory)", self.source)
        self.assertIn("absent-directory)", self.source)
        config = re.search(r"^write_normalized_root_helper_config\(\) \{\n(?P<body>.*?)^\}$", self.source, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(config)
        config_body = config.group("body")  # type: ignore[union-attr]
        self.assertIn("ZAPRET_DIR='/opt/zapret2'", config_body)
        self.assertIn("GP_ROOT_HELPER_RUN_DIR='/run/gp-control-plane/runs'", config_body)
        self.assertIn("install -d -m 0750 -o root -g root \"$RUN_REGISTRY_DIR\"", self.source)
        self.assertIn("root helper run registry must be root-owned mode 0750", self.source)

        backup = self.source.index("journal_phase backup-created")
        config_install = self.source.index('install -m 0644 -o root -g root "$STAGED_ROOT_HELPER_CONFIG" "$ROOT_HELPER_CONFIG"')
        registry_create = self.source.index("ensure_run_registry\ninstall -m 0755", backup)
        self.assertLess(backup, config_install)
        self.assertLess(config_install, registry_create)

    def test_bootstrap_uses_a_root_owned_nonblocking_transaction_lock(self) -> None:
        self.assertIn("require_command flock", self.source)
        self.assertIn('exec 9>"$JOURNAL_ROOT/bootstrap.lock"', self.source)
        self.assertIn("flock -n -x 9 || fail 'another legacy bootstrap transaction is active'", self.source)

    def test_script_is_parseable_by_bash_when_available(self) -> None:
        bash = self.bash_shell()
        if bash is None:
            self.skipTest("bash is required for syntax validation")
        completed = subprocess.run([bash, "-n", str(self.path)], check=False, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
