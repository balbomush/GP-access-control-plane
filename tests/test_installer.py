from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class InstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.installer = (root / "scripts" / "install-linux.sh").read_text(encoding="utf-8")
        cls.bootstrap = (root / "scripts" / "bootstrap-linux.sh").read_text(encoding="utf-8")
        cls.legacy_installer = (root / "scripts" / "install-raspberry-pi.sh").read_text(encoding="utf-8")
        cls.legacy_bootstrap = (root / "scripts" / "bootstrap-raspberry-pi.sh").read_text(encoding="utf-8")
        cls.zapret_installer = (root / "scripts" / "install-zapret2.sh").read_text(encoding="utf-8")
        cls.helper = (root / "scripts" / "gp-root-helper.sh").read_text(encoding="utf-8")

    @staticmethod
    def shell_function(source: str, name: str) -> str:
        match = re.search(rf"^{re.escape(name)}\(\) \{{\n(?P<body>.*?)(?=^\}}$)", source, re.MULTILINE | re.DOTALL)
        if not match:
            raise AssertionError(f"Bash function {name} was not found")
        return match.group("body")


    def test_complex_update_is_absent_and_clean_install_is_a_separate_acknowledged_path(self) -> None:
        root = Path(__file__).resolve().parents[1]
        clean_launcher = (root / "scripts" / "clean-install-vault.sh").read_text(encoding="utf-8")

        for obsolete in ("queue-update", "queue_strict_update", "strict_fetch_pinned_candidate", "rollback_published_code", "release-updates"):
            with self.subTest(obsolete=obsolete):
                self.assertNotIn(obsolete, self.helper)
        for obsolete in ("GP_INSTALL_FORCE_CLEAN", "GP_UPDATE_CANDIDATE_REF", "GP_UPDATE_EXPECTED_SHA", "--strict-preflight", "strict_update_requested", "pinned_update_enabled"):
            with self.subTest(obsolete=obsolete):
                self.assertNotIn(obsolete, self.installer)
        for obsolete in ("--trusted-clean-install", "clean-install-root-runner", "trusted-clean-install"):
            with self.subTest(obsolete=obsolete):
                self.assertNotIn(obsolete, self.installer)
                self.assertNotIn(obsolete, self.helper)
        self.assertIn("clean-remove", clean_launcher)
        self.assertIn("--confirm-clean-remove", clean_launcher)
        self.assertNotIn("queue-update", clean_launcher)
        self.assertNotIn("rollback", clean_launcher)

    def test_clean_install_launcher_has_one_bounded_root_clean_remove_protocol(self) -> None:
        root = Path(__file__).resolve().parents[1]
        launcher = (root / "scripts" / "clean-install-vault.sh").read_text(encoding="utf-8")

        self.assertIn("ROOT_HELPER=/usr/local/libexec/gp-control-plane/gp-root-helper", launcher)
        self.assertIn('[ "$#" -eq 1 ] && [ "$1" = --confirm-clean-remove ]', launcher)
        self.assertIn('exec /usr/bin/sudo -n "$ROOT_HELPER" clean-remove --confirm-clean-remove', launcher)
        self.assertNotIn('/bin/sh -c', launcher)
        self.assertNotIn('sudo systemctl', launcher)
        self.assertNotIn('sudo rm', launcher)
        self.assertNotIn('GP_INSTALL_FORCE_CLEAN', launcher)
        self.assertNotIn('archive.zip', launcher)
        self.assertNotIn('entry.json', launcher)
        self.assertNotIn('candidate-ref', launcher)
        self.assertNotIn('expected-sha', launcher)




    def test_managed_paths_normalize_octal_modes_before_exact_postcondition_checks(self) -> None:
        for function_name in ("ensure_root_directory", "ensure_root_regular_file"):
            with self.subTest(function_name=function_name):
                function = self.shell_function(self.installer, function_name)
                self.assertIn("expected_mode=\"$(printf '%o' \"$((8#$managed_mode))\")\"", function)
                self.assertIn(":$expected_mode\"", function)
                self.assertNotIn(":$managed_mode\"", function)







    def test_installer_defaults_to_stable_release_and_accepts_only_tag_or_pinned_pre_tag_candidate(self) -> None:
        self.assertIn('BRANCH="${GP_BRANCH:-latest-stable}"', self.installer)
        self.assertIn("resolve_install_ref()", self.installer)
        self.assertIn('latest|stable|latest-stable)', self.installer)
        self.assertIn('git ls-remote --tags --refs "$REPO_URL" "v*"', self.installer)
        self.assertIn("grep -E '^v[0-9]+([.][0-9]+)*$'", self.installer)
        self.assertIn("Latest stable GP release: $BRANCH", self.installer)
        self.assertNotIn('BRANCH="${GP_BRANCH:-main}"', self.installer)
        self.assertNotIn('BRANCH="${GP_BRANCH:-v0.3.4}"', self.installer)
        self.assertIn('validate_release_selector()', self.installer)
        self.assertIn('GP_BRANCH must be an immutable release tag vX.Y.Z', self.installer)
        self.assertIn('GP_BRANCH=dev requires GP_EXPECTED_SHA', self.installer)
        self.assertIn('Installed dev candidate SHA does not match GP_EXPECTED_SHA', self.installer)
        self.assertIn('annotated immutable release tag', self.installer)
        self.assertIn('Clean installation requires an absent target: $INSTALL_DIR', self.installer)
        self.assertIn('resolve_selected_release_identity()', self.installer)
        self.assertIn('preflight_fresh_install', self.installer)
        self.assertIn('git ls-remote --tags "$REPO_URL" "$remote_tag" "${remote_tag}^{}"', self.installer)
        self.assertIn('"+refs/tags/$BRANCH:refs/tags/$BRANCH"', self.installer)
        self.assertIn('repo_git checkout --detach --force "$SELECTED_COMMIT_SHA"', self.installer)
        self.assertIn('Checked-out annotated release tag object does not match canonical release identity', self.installer)
        self.assertIn('Installed release SHA does not match the annotated release tag commit', self.installer)
        self.assertIn('Installed release checkout must be detached at the annotated tag commit', self.installer)
        self.assertNotIn('git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"', self.installer)
        self.assertNotIn('repo_git pull --ff-only', self.installer)

    def test_installer_preflight_precedes_all_root_mutating_steps(self) -> None:
        preflight_call = self.installer.index('  preflight_fresh_install\n')
        packages = self.installer.index('if step_log packages "Updating package index and installing required packages"; then')
        zapret = self.installer.index('if step_log zapret "Installing zapret2"; then')
        self.assertLess(preflight_call, packages)
        self.assertLess(preflight_call, zapret)

    def test_usage_exposes_only_tag_or_frozen_dev_candidate(self) -> None:
        usage = self.shell_function(self.installer, "usage")
        self.assertIn('GP_BRANCH=vX.Y.Z        installs one exact annotated release tag.', usage)
        self.assertIn('GP_BRANCH=dev           pre-tag hardware validation only; GP_EXPECTED_SHA is required.', usage)
        self.assertNotIn('branch or tag, for example main', usage)

    def test_tag_identity_requires_a_peeled_annotated_tag_and_ignores_same_named_branch(self) -> None:
        bash = shutil.which("bash")
        if bash is None:
            git_bash = Path(os.environ.get("ProgramFiles", r"C:\\Program Files")) / "Git" / "bin" / "bash.exe"
            if git_bash.is_file():
                bash = str(git_bash)
        if bash is None:
            self.skipTest("bash is required for release identity regression")

        remote_ref_sha = self.shell_function(self.installer, "remote_ref_sha")
        resolve_identity = self.shell_function(self.installer, "resolve_selected_release_identity")
        source = (
            "remote_ref_sha() {\n"
            + remote_ref_sha
            + "\n}\nresolve_selected_release_identity() {\n"
            + resolve_identity
            + "\n}\n"
            + "fail() { exit 73; }\n"
            + "git() { case \"$*\" in *refs/heads/*) exit 91 ;; esac; printf '%s\\n' \"$FAKE_REMOTE\"; }\n"
            + "BRANCH=v1.2.3\nREPO_URL=https://canonical.invalid/gp.git\nEXPECTED_SHA=\n"
            + "resolve_selected_release_identity\nprintf '%s %s\\n' \"$SELECTED_TAG_OBJECT_SHA\" \"$SELECTED_COMMIT_SHA\"\n"
        )
        tag_object = "a" * 40
        peeled_commit = "b" * 40
        annotated = subprocess.run(
            [bash, "-c", source],
            check=False,
            capture_output=True,
            text=True,
            env=os.environ
            | {
                "FAKE_REMOTE": f"{tag_object}\trefs/tags/v1.2.3\n{peeled_commit}\trefs/tags/v1.2.3^{{}}",
            },
        )
        self.assertEqual(annotated.returncode, 0, annotated.stderr)
        self.assertEqual(annotated.stdout.strip(), f"{tag_object} {peeled_commit}")

        lightweight = subprocess.run(
            [bash, "-c", source],
            check=False,
            capture_output=True,
            text=True,
            env=os.environ | {"FAKE_REMOTE": f"{tag_object}\trefs/tags/v1.2.3"},
        )
        self.assertEqual(lightweight.returncode, 73, lightweight.stderr)

    def test_legacy_raspberry_fallbacks_use_the_stable_bootstrap_asset(self) -> None:
        stable_bootstrap_asset = (
            "https://github.com/balbomush/GP-access-control-plane/"
            "releases/latest/download/bootstrap-linux.sh"
        )
        for script in (self.legacy_installer, self.legacy_bootstrap):
            self.assertIn("bootstrap-linux.sh", script)
            self.assertIn("GP_LEGACY_BOOTSTRAP_URL", script)
            self.assertIn(stable_bootstrap_asset, script)
            self.assertIn('GP_BRANCH:-latest-stable', script)
            self.assertNotIn('/main/scripts', script)
            self.assertNotIn('GP_BRANCH:-main', script)

    def test_installer_service_uses_persistent_state_and_memory_limits(self) -> None:
        self.assertIn("GP_SERVICE_MEMORY_HIGH", self.installer)
        self.assertIn("GP_SERVICE_MEMORY_MAX", self.installer)
        self.assertIn("GP_INSTALL_CONFIG", self.installer)
        self.assertIn("set -a", self.installer)
        self.assertIn("MemoryAccounting=true", self.installer)
        self.assertIn("MemoryHigh=$SERVICE_MEMORY_HIGH", self.installer)
        self.assertIn("MemoryMax=$SERVICE_MEMORY_MAX", self.installer)
        self.assertIn("KillMode=control-group", self.installer)
        self.assertIn("WorkingDirectory=$INSTALL_DIR", self.installer)
        self.assertIn("Environment=PATH=$SERVICE_PATH", self.installer)
        self.assertIn("WEB_ENV_FILE", self.installer)
        self.assertIn("default_state_dir()", self.installer)
        self.assertIn("'%s/.%s.data/state", self.installer)
        self.assertIn("GP_INSTALL_DIR", self.installer)
        self.assertIn("GP_INSTALL_DIR='%s'", self.installer)
        self.assertIn("GP_STATE_DIR", self.installer)
        self.assertIn("GP_STATE_DIR='%s'", self.installer)
        self.assertIn("GP_INSTALL_WEB='%s'", self.installer)
        self.assertIn('INSTALL_WEB="${GP_INSTALL_WEB:-on}"', self.installer)
        self.assertIn('CORE_SERVICE_NAME="${GP_CORE_SERVICE_NAME:-gp-control-plane-core.service}"', self.installer)
        self.assertIn('CORE_HOST="${GP_CORE_HOST:-127.0.0.1}"', self.installer)
        self.assertIn('CORE_PORT="${GP_CORE_PORT:-8081}"', self.installer)
        self.assertIn('CORE_URL="${GP_CORE_URL:-http://$CORE_HOST:$CORE_PORT}"', self.installer)
        self.assertIn('CORE_ENV_FILE="${GP_CORE_ENV_FILE:-/etc/default/gp-control-plane-core}"', self.installer)
        self.assertIn("install_web_enabled()", self.installer)
        self.assertIn("install_systemd_service()", self.installer)
        self.assertIn("EnvironmentFile=-$env_file", self.installer)
        self.assertIn("ExecStart=$exec_start", self.installer)
        self.assertIn('install_service_env_file "$CORE_ENV_FILE"', self.installer)
        self.assertIn('install_systemd_service "$CORE_SERVICE_NAME" "GP Strategy Finder Core API" "core" "$CORE_HOST" "$CORE_PORT" "$CORE_ENV_FILE"', self.installer)
        self.assertIn('install_service_env_file "$WEB_ENV_FILE"', self.installer)
        self.assertIn('install_systemd_service "$SERVICE_NAME" "GP Strategy Finder Web UI" "web" "$WEB_HOST" "$WEB_PORT" "$WEB_ENV_FILE" "--core-url $CORE_URL" "$CORE_SERVICE_NAME" "$CORE_SERVICE_NAME"', self.installer)
        self.assertIn('as_root systemctl enable "$CORE_SERVICE_NAME"', self.installer)
        self.assertIn('as_root systemctl enable "$SERVICE_NAME"', self.installer)
        self.assertIn('as_root systemctl disable --now "$SERVICE_NAME"', self.installer)
        self.assertIn('TMP_SERVICE="$(mktemp)"', self.installer)
        self.assertNotIn("--config", self.installer)
        self.assertNotIn("orchestrator.example.yaml", self.installer)




    def test_installer_prepares_v2fly_with_service_config_but_keeps_install_non_blocking(self) -> None:
        self.assertIn("Preparing local v2fly domain catalog", self.installer)
        self.assertIn("prepare_v2fly_local_catalog", self.installer)
        self.assertIn(
            'cd "$1" && GP_INSTALL_DIR="$1" GP_STATE_DIR="$2" "$1/.venv/bin/gp-control-plane" domain-sources prepare-v2fly',
            self.installer,
        )
        self.assertIn("if ! prepare_v2fly_local_catalog", self.installer)
        self.assertIn("v2fly local catalog was not prepared", self.installer)

    def test_installer_keeps_luajit_build_dependency_architecture_tolerant(self) -> None:
        self.assertIn("libluajit2-5.1-dev", self.installer)
        self.assertIn("libluajit-5.1-dev", self.installer)
        self.assertIn("apt_package_available", self.installer)
        self.assertIn("install_luajit_dev_package", self.installer)
        self.assertIn("LuaJIT development package was not found", self.installer)
        self.assertNotIn("apt-get install -y libluajit2-5.1-dev \\", self.installer)

    def test_installer_does_not_run_full_apt_upgrade(self) -> None:
        for script in (self.installer, self.bootstrap):
            self.assertNotIn("GP_APT_UPGRADE", script)
            self.assertNotIn("APT_UPGRADE", script)
            self.assertNotIn("apt_upgrade_enabled()", script)
            self.assertNotIn("apt-get -y upgrade", script)
            self.assertNotIn("apt-get upgrade", script)
        self.assertIn("as_root apt-get update", self.installer)
        self.assertIn("apt-get install -y", self.installer)

    def test_bootstrap_installs_minimal_dependencies_and_runs_stable_installer(self) -> None:
        self.assertIn('INSTALL_REF="${GP_BRANCH:-latest-stable}"', self.bootstrap)
        self.assertIn("GP_INSTALL_CONFIG", self.bootstrap)
        self.assertIn("set -a", self.bootstrap)
        self.assertIn("as_root apt-get update", self.bootstrap)
        self.assertIn("apt-get install -y ca-certificates git", self.bootstrap)
        self.assertIn('git ls-remote --tags --refs "$REPO_URL" "v*"', self.bootstrap)
        self.assertIn("grep -E '^v[0-9]+([.][0-9]+)*$'", self.bootstrap)
        self.assertIn("validate_release_tag()", self.bootstrap)
        self.assertIn("GP_BRANCH must be an immutable release tag vX.Y.Z", self.bootstrap)
        self.assertIn('export GP_BRANCH="$INSTALL_REF"', self.bootstrap)
        self.assertIn('git clone --no-checkout --depth=1 --branch "$INSTALL_REF" "$REPO_URL" "$checkout_dir"', self.bootstrap)
        self.assertIn('cat-file -t "refs/tags/$INSTALL_REF"', self.bootstrap)
        self.assertIn('GP_BRANCH must resolve to an annotated immutable release tag', self.bootstrap)
        self.assertIn('checkout --detach "$INSTALL_REF"', self.bootstrap)
        self.assertIn('verified release checkout does not match the annotated tag commit', self.bootstrap)
        self.assertIn('bash "$installer" "$@"', self.bootstrap)
        self.assertNotIn("curl -LfsS", self.bootstrap)

    def test_zapret2_installer_is_short_standalone_script(self) -> None:
        self.assertIn('ZAPRET_REPO_URL="${ZAPRET_REPO_URL:-https://github.com/bol-van/zapret2.git}"', self.zapret_installer)
        self.assertIn("apt-get install -y git bsdextrautils", self.zapret_installer)
        self.assertIn('git clone --branch "$ZAPRET_BRANCH" "$ZAPRET_REPO_URL" "$ZAPRET_DIR"', self.zapret_installer)
        self.assertIn("./install_bin.sh", self.zapret_installer)

    def test_installer_supports_one_command_and_individual_steps(self) -> None:
        self.assertIn('REQUESTED_STEPS="${GP_INSTALL_STEPS:-all}"', self.installer)
        self.assertIn("--step STEP", self.installer)
        self.assertIn("packages,zapret,app,v2fly,root-helper,service,check", self.installer)
        for step in ("packages", "zapret", "app", "v2fly", "root-helper", "service", "check"):
            self.assertIn(f"step_log {step}", self.installer)

    def test_installer_does_not_generate_deferred_web_auth_env(self) -> None:
        self.assertNotIn("GP_WEB_AUTH", self.installer)
        self.assertNotIn("GP_WEB_TOKEN", self.installer)
        self.assertNotIn("generate_web_token", self.installer)
        self.assertNotIn("resolve_service_token", self.installer)
        self.assertIn("install_web_env_file", self.installer)
        self.assertIn("GP_INSTALL_DIR", self.installer)
        self.assertIn("GP_STATE_DIR", self.installer)




    def test_installer_writes_profile_only_after_success_and_legacy_capture_skips_reconfiguration(self) -> None:
        self.assertIn('capture_legacy_install_profile()', self.installer)
        self.assertIn('load_trusted_service_env "$CORE_ENV_FILE"', self.installer)
        trusted_env_start = self.installer.index('load_trusted_service_env() {')
        trusted_env_end = self.installer.index('\ncapture_legacy_install_profile()', trusted_env_start)
        trusted_env_loader = self.installer[trusted_env_start:trusted_env_end]
        self.assertIn('load_root_env_values "$service_env_file"', trusted_env_loader)
        self.assertIn('GP_INSTALL_DIR GP_STATE_DIR GP_INSTALL_WEB', trusted_env_loader)
        self.assertNotIn('. "$service_env_file"', trusted_env_loader)
        self.assertIn('unit_option "$CORE_SERVICE_NAME" --host', self.installer)
        self.assertIn('log "[root-helper] skipped while capturing the existing install profile"', self.installer)
        self.assertIn('log "[service] skipped while capturing the existing install profile"', self.installer)
        self.assertIn('as_root install -m 0600 -o root -g root "$tmp_profile" "$INSTALL_PROFILE"', self.installer)
        self.assertIn('write_install_profile_value GP_INSTALL_WEB "$profile_install_web"', self.installer)
        self.assertIn('write_install_profile_value GP_CORE_URL "$CORE_URL"', self.installer)
        self.assertIn('write_install_profile_value GP_WEB_HOST "$WEB_HOST"', self.installer)
        self.assertIn('write_install_profile_value GP_WEB_PORT "$WEB_PORT"', self.installer)

        check_pos = self.installer.index('if step_log check "Checking installation"; then')
        write_pos = self.installer.index('write_install_profile', check_pos)
        self.assertLess(check_pos, write_pos)

    def test_legacy_capture_reads_root_only_service_env_without_sourcing_it(self) -> None:
        bash = shutil.which("bash")
        if bash is None:
            git_bash = Path(os.environ.get("ProgramFiles", r"C:\\Program Files")) / "Git" / "bin" / "bash.exe"
            if git_bash.is_file():
                bash = str(git_bash)
        if bash is None:
            self.skipTest("bash is required for legacy service environment regression probe")

        with tempfile.TemporaryDirectory() as raw:
            work_dir = Path(raw)
            bash_work_dir = subprocess.run(
                [bash, "-lc", "pwd"],
                cwd=work_dir,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            def bash_path(path: Path) -> str:
                return f"{bash_work_dir}/{path.relative_to(work_dir).as_posix()}"

            systemd_dir = work_dir / "systemd"
            systemd_dir.mkdir()
            core_env = work_dir / "gp-control-plane-core"
            core_unit = systemd_dir / "gp-control-plane-core.service"
            install_profile = work_dir / "install-profile"
            probe = work_dir / "legacy-capture-probe.sh"
            fake_bin = work_dir / "fake-bin"
            fake_sudo = fake_bin / "sudo"
            fake_id = fake_bin / "id"
            fake_awk = fake_bin / "awk"
            sudo_log = work_dir / "sudo.log"
            direct_awk_log = work_dir / "direct-awk.log"
            core_env_bash = bash_path(core_env)
            real_awk = subprocess.run(
                [bash, "-lc", "command -v awk"],
                cwd=work_dir,
                check=False,
                capture_output=True,
                text=True,
            )
            if real_awk.returncode != 0:
                self.skipTest("awk is required for legacy service environment regression probe")
            real_awk_path = real_awk.stdout.strip()
            core_unit.write_text(
                "[Service]\n"
                f"EnvironmentFile=-{core_env_bash}\n"
                "ExecStart=/opt/gp/.venv/bin/gp-control-plane serve-core --host 127.0.0.9 --port 18081\n",
                encoding="utf-8",
                newline="\n",
            )

            source = self.installer.replace(
                'INSTALL_PROFILE="/etc/default/gp-control-plane-install-profile"',
                f"INSTALL_PROFILE={shlex.quote(bash_path(install_profile))}",
                1,
            ).replace("/etc/systemd/system", shlex.quote(bash_path(systemd_dir))).replace(
                "LEGACY_PROFILE_CAPTURE=off",
                "LEGACY_PROFILE_CAPTURE=on",
                1,
            )
            current_uid_start = source.index('CURRENT_UID="$(id -u)"')
            as_root_start = source.index('as_root() {', current_uid_start)
            source = (
                source[:current_uid_start]
                + "CURRENT_UID=1000\n"
                "CURRENT_USER=tester\n"
                "TARGET_USER=tester\n"
                "TARGET_HOME=/tmp\n"
                "TARGET_GROUP=tester\n"
                "INSTALL_DIR=/initial/install\n"
                "STATE_DIR=/initial/state\n"
                "TARGET_BIN_DIR=/tmp/.local/bin\n"
                "SERVICE_PATH=/tmp/.local/bin\n\n"
                + source[as_root_start:]
            )
            preflight_start = source.index('if [ "$CURRENT_UID" -ne 0 ]; then')
            trusted_root_start = source.index('trusted_root_file() {', preflight_start)
            source = (
                source[:preflight_start]
                + "need_command() { :; }\n"
                "log() { :; }\n\n"
                + source[trusted_root_start:]
            )
            capture_marker = 'if [ "$LEGACY_PROFILE_CAPTURE" = on ]; then\n  capture_legacy_install_profile\nfi\n'
            self.assertIn(capture_marker, source)
            source = source.replace(
                capture_marker,
                capture_marker
                + 'printf "install_dir=%s state_dir=%s web=%s core=%s:%s\\n" "$INSTALL_DIR" "$STATE_DIR" "$INSTALL_WEB" "$CORE_HOST" "$CORE_PORT"\n'
                + "exit 0\n",
                1,
            )
            probe.write_text(source, encoding="utf-8", newline="\n")
            probe.chmod(0o755)
            fake_bin.mkdir()
            fake_id.write_text(
                "#!/usr/bin/env bash\n"
                "case \"${1:-}\" in\n"
                "  -u) printf '1000\\n' ;;\n"
                "  *) exit 98 ;;\n"
                "esac\n",
                encoding="utf-8",
                newline="\n",
            )
            fake_sudo.write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                "printf '%s\\n' \"$*\" >> \"$GP_TEST_SUDO_LOG\"\n"
                "if [ \"${GP_TEST_SUDO_FAIL_AWK:-off}\" = on ] && [ \"${1:-}\" = awk ]; then\n"
                "  exit 75\n"
                "fi\n"
                "case \"${1:-}\" in\n"
                "  stat)\n"
                "    case \"${3:-}\" in\n"
                "      %u) printf '0\\n' ;;\n"
                "      %a) printf '640\\n' ;;\n"
                "      *) exit 98 ;;\n"
                "    esac\n"
                "    ;;\n"
                "  awk)\n"
                "    shift\n"
                "    exec \"$GP_TEST_REAL_AWK\" \"$@\"\n"
                "    ;;\n"
                "  *) exec \"$@\" ;;\n"
                "esac\n",
                encoding="utf-8",
                newline="\n",
            )
            fake_awk.write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                "case \"$*\" in\n"
                "  *\"$GP_TEST_CORE_ENV\"*)\n"
                "    printf 'direct awk was used for the service environment\\n' >> \"$GP_TEST_DIRECT_AWK_LOG\"\n"
                "    exit 97\n"
                "    ;;\n"
                "esac\n"
                "exec \"$GP_TEST_REAL_AWK\" \"$@\"\n",
                encoding="utf-8",
                newline="\n",
            )
            for helper in (fake_id, fake_sudo, fake_awk):
                helper.chmod(0o755)

            def run_probe(*, fail_sudo_awk: bool = False) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [bash, bash_path(probe)],
                    check=False,
                    capture_output=True,
                    env=os.environ
                    | {
                        "GP_TEST_CORE_ENV": core_env_bash,
                        "GP_TEST_DIRECT_AWK_LOG": bash_path(direct_awk_log),
                        "GP_TEST_REAL_AWK": real_awk_path,
                        "GP_TEST_SUDO_FAIL_AWK": "on" if fail_sudo_awk else "off",
                        "GP_TEST_SUDO_LOG": bash_path(sudo_log),
                        "PATH": f"{bash_path(fake_bin)}:{os.environ['PATH']}",
                    },
                    text=True,
                )

            core_env.write_text(
                "GP_INSTALL_DIR='/srv/gp'\\''s'\n"
                "GP_STATE_DIR='/srv/gp/state'\n"
                "GP_INSTALL_WEB='off'\n",
                encoding="utf-8",
                newline="\n",
            )
            core_env.chmod(0o640)
            accepted = run_probe()
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertEqual(
                accepted.stdout.strip(),
                "install_dir=/srv/gp's state_dir=/srv/gp/state web=off core=127.0.0.9:18081",
            )
            sudo_awk_calls = [
                line for line in sudo_log.read_text(encoding="utf-8").splitlines() if line.startswith("awk ")
            ]
            self.assertEqual(len(sudo_awk_calls), 3)
            self.assertTrue(all(call.startswith("awk -v key=") for call in sudo_awk_calls))
            self.assertIn("key=GP_INSTALL_DIR", sudo_awk_calls[0])
            self.assertIn("key=GP_STATE_DIR", sudo_awk_calls[1])
            self.assertIn("key=GP_INSTALL_WEB", sudo_awk_calls[2])
            self.assertFalse(direct_awk_log.exists(), "service environment must not fall back to direct awk")

            sudo_failure = run_probe(fail_sudo_awk=True)
            self.assertNotEqual(sudo_failure.returncode, 0)
            self.assertIn("Cannot safely parse service environment", sudo_failure.stderr)
            self.assertFalse(direct_awk_log.exists(), "sudo failure must not fall back to direct awk")

            marker = work_dir / "sourced-marker"
            core_env.write_text(
                "GP_INSTALL_DIR='/srv/gp'; touch " + shlex.quote(bash_path(marker)) + "\n"
                "GP_STATE_DIR='/srv/gp/state'\n",
                encoding="utf-8",
                newline="\n",
            )
            core_env.chmod(0o640)
            rejected = run_probe()
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("Cannot safely parse service environment", rejected.stderr)
            self.assertFalse(marker.exists(), "legacy service environment must not be sourced")

    def test_root_helper_multidomain_runner_normalizes_empty_ip_list_before_nft(self) -> None:
        self.assertIn("gp_md_normalize_ip_list", self.helper)
        self.assertIn('ips="$(gp_md_normalize_ip_list "$ips")', self.helper)
        self.assertIn("GP-MULTIDOMAIN no resolved ip addresses for $proto/$port", self.helper)
        self.assertIn('tcp) pktws_ipt_prepare_tcp "$port" "$ips" ;;', self.helper)
        self.assertIn('udp) pktws_ipt_prepare_udp "$port" "$ips" ;;', self.helper)

        resolve_pos = self.helper.index('ips="$(gp_md_resolve_all_ips)"')
        normalize_pos = self.helper.index('ips="$(gp_md_normalize_ip_list "$ips")', resolve_pos)
        empty_guard_pos = self.helper.index('[ -n "$ips" ] || {', normalize_pos)
        udp_prepare_pos = self.helper.index("pktws_ipt_prepare_udp", empty_guard_pos)
        self.assertLess(resolve_pos, normalize_pos)
        self.assertLess(normalize_pos, empty_guard_pos)
        self.assertLess(empty_guard_pos, udp_prepare_pos)


if __name__ == "__main__":
    unittest.main()
