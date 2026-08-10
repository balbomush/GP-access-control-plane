from __future__ import annotations

import os
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

    def test_installer_configures_root_helper(self) -> None:
        self.assertIn("ROOT_HELPER_PATH", self.installer)
        self.assertIn("gp-root-helper.sh", self.installer)
        self.assertIn("NOPASSWD", self.installer)
        self.assertIn("visudo -cf", self.installer)
        self.assertIn("Environment=GP_ROOT_HELPER", self.installer)
        self.assertIn("run-env", self.helper)
        self.assertIn("run-multidomain-env", self.helper)
        self.assertIn("queue-update", self.helper)
        self.assertIn("systemd-run", self.helper)
        self.assertIn("GP_BRANCH", self.helper)
        self.assertIn("GP_INSTALL_USER", self.helper)
        self.assertIn("GP_INSTALL_FORCE_CLEAN", self.helper)
        self.assertIn("safe.directory", self.helper)
        self.assertIn("repo_git", self.installer)
        self.assertIn("install-linux.sh", self.helper)
        self.assertIn("install-raspberry-pi.sh", self.helper)
        self.assertIn('installed_ref="\\$(git', self.helper)
        self.assertIn('echo "installed_ref=\\$installed_ref"', self.helper)
        self.assertIn("awk '{print \\$NF}'", self.helper)
        self.assertIn("installed_version=", self.helper)
        self.assertIn("status=success", self.helper)
        self.assertIn("status=failed", self.helper)
        self.assertIn("nft-delete-blockcheck-table", self.helper)
        self.assertIn("unsupported run target", self.helper)
        self.assertNotIn("/tmp/*/gp-multidomain-blockcheck.sh", self.helper)
        self.assertNotIn("/var/tmp/*/gp-multidomain-blockcheck.sh", self.helper)
        self.assertIn("write_multidomain_runner", self.helper)
        self.assertIn('BRANCH="${GP_BRANCH:-latest-stable}"', self.installer)
        self.assertIn("validate_state_dir()", self.helper)
        self.assertIn('state_dir="$(validate_state_dir "${3:-$install_dir/build/state}")"', self.helper)
        self.assertIn('log_dir="$state_dir/release-updates"', self.helper)
        self.assertIn('echo "state_dir=$(shell_quote "$state_dir")"', self.helper)
        self.assertIn("export GP_STATE_DIR=", self.helper)
        self.assertIn('queue-update requires install directory, release ref and optional state directory', self.helper)

    def test_release_update_forces_clean_checkout_but_manual_install_keeps_dirty_guard(self) -> None:
        self.assertIn('INSTALL_FORCE_CLEAN="${GP_INSTALL_FORCE_CLEAN:-off}"', self.installer)
        self.assertIn("force_clean_enabled()", self.installer)
        self.assertIn("Repository has local changes; release update will discard worktree changes before checkout", self.installer)
        self.assertIn("repo_git reset --hard", self.installer)
        self.assertIn("repo_git clean -fd", self.installer)
        self.assertIn('fail "Repository has local changes: $INSTALL_DIR. Commit or remove them, then run installer again."', self.installer)
        self.assertIn("export GP_INSTALL_FORCE_CLEAN=on", self.helper)

        force_pos = self.helper.index("export GP_INSTALL_FORCE_CLEAN=on")
        installer_pos = self.helper.index("if bash", force_pos)
        self.assertLess(force_pos, installer_pos)

    def test_release_update_bootstraps_target_ref_before_running_installer(self) -> None:
        self.assertIn('repo_git fetch origin "\\$GP_BRANCH" || true', self.helper)
        self.assertIn('repo_git checkout -B "\\$GP_BRANCH" "origin/\\$GP_BRANCH"', self.helper)
        self.assertIn('repo_git fetch origin "+refs/tags/\\$GP_BRANCH:refs/tags/\\$GP_BRANCH" || true', self.helper)
        self.assertIn('repo_git checkout --detach "\\$GP_BRANCH"', self.helper)
        self.assertIn('repo_git reset --hard "\\$GP_BRANCH"', self.helper)

        bootstrap_pos = self.helper.index('repo_git fetch origin "\\$GP_BRANCH" || true')
        installer_pos = self.helper.index("if bash", bootstrap_pos)
        self.assertLess(bootstrap_pos, installer_pos)

    def test_installer_defaults_to_stable_release_and_supports_branch_or_tag(self) -> None:
        self.assertIn('BRANCH="${GP_BRANCH:-latest-stable}"', self.installer)
        self.assertIn("resolve_install_ref()", self.installer)
        self.assertIn('latest|stable|latest-stable)', self.installer)
        self.assertIn('git ls-remote --tags --refs "$REPO_URL" "v*"', self.installer)
        self.assertIn("grep -E '^v[0-9]+([.][0-9]+)*$'", self.installer)
        self.assertIn("Latest stable GP release: $BRANCH", self.installer)
        self.assertNotIn('BRANCH="${GP_BRANCH:-main}"', self.installer)
        self.assertNotIn('BRANCH="${GP_BRANCH:-v0.3.4}"', self.installer)
        self.assertIn('repo_git fetch origin "$BRANCH" || true', self.installer)
        self.assertIn('repo_git fetch origin "+refs/tags/$BRANCH:refs/tags/$BRANCH" || true', self.installer)
        self.assertIn('repo_git checkout -B "$BRANCH" "origin/$BRANCH"', self.installer)
        self.assertIn('repo_git checkout --detach "$BRANCH"', self.installer)
        self.assertIn('fail "Cannot find branch or tag: $BRANCH"', self.installer)

    def test_legacy_raspberry_script_names_forward_to_linux_scripts(self) -> None:
        self.assertIn("install-linux.sh", self.legacy_installer)
        self.assertIn("bootstrap-linux.sh", self.legacy_bootstrap)

    def test_installer_service_uses_install_dir_state_and_memory_limits(self) -> None:
        self.assertIn("GP_SERVICE_MEMORY_HIGH", self.installer)
        self.assertIn("GP_SERVICE_MEMORY_MAX", self.installer)
        self.assertIn("GP_INSTALL_CONFIG", self.installer)
        self.assertIn("set -a", self.installer)
        self.assertIn("MemoryAccounting=true", self.installer)
        self.assertIn("MemoryHigh=$SERVICE_MEMORY_HIGH", self.installer)
        self.assertIn("MemoryMax=$SERVICE_MEMORY_MAX", self.installer)
        self.assertIn("WorkingDirectory=$INSTALL_DIR", self.installer)
        self.assertIn("Environment=PATH=$SERVICE_PATH", self.installer)
        self.assertIn("WEB_ENV_FILE", self.installer)
        self.assertIn('STATE_DIR="${GP_STATE_DIR:-$INSTALL_DIR/build/state}"', self.installer)
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
        self.assertIn('RAW_BASE_URL="${GP_RAW_BASE_URL:-https://github.com/balbomush/GP-access-control-plane/raw}"', self.bootstrap)
        self.assertIn("as_root apt-get update", self.bootstrap)
        self.assertIn("apt-get install -y ca-certificates curl git", self.bootstrap)
        self.assertIn('git ls-remote --tags --refs "$REPO_URL" "v*"', self.bootstrap)
        self.assertIn("grep -E '^v[0-9]+([.][0-9]+)*$'", self.bootstrap)
        self.assertIn('export GP_BRANCH="$INSTALL_REF"', self.bootstrap)
        self.assertIn('installer_url="$RAW_BASE_URL/$INSTALL_REF/scripts/install-linux.sh"', self.bootstrap)
        self.assertIn('legacy_installer_url="$RAW_BASE_URL/$INSTALL_REF/scripts/install-raspberry-pi.sh"', self.bootstrap)
        self.assertIn('curl -LfsS "$installer_url" -o "$tmp_installer"', self.bootstrap)
        self.assertIn('curl -LfsS "$legacy_installer_url" -o "$tmp_installer"', self.bootstrap)
        self.assertIn('bash "$tmp_installer" "$@"', self.bootstrap)

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

    def test_release_update_uses_only_root_owned_resolved_install_profile(self) -> None:
        self.assertIn('INSTALL_PROFILE="/etc/default/gp-control-plane-install-profile"', self.installer)
        self.assertIn("if release_update_enabled; then", self.installer)
        self.assertIn('LEGACY_PROFILE_CAPTURE=on', self.installer)
        self.assertIn('if [ -e "$INSTALL_PROFILE" ] || [ -L "$INSTALL_PROFILE" ]; then', self.installer)
        self.assertIn('[ -f "$INSTALL_PROFILE" ] && [ ! -L "$INSTALL_PROFILE" ]', self.installer)
        self.assertIn('profile_uid="$(stat -c \'%u\' "$INSTALL_PROFILE"', self.installer)
        self.assertIn('profile_mode="$(stat -c \'%a\' "$INSTALL_PROFILE"', self.installer)
        self.assertIn('$((8#$profile_mode & 022)) -eq 0', self.installer)
        self.assertIn('. "$INSTALL_PROFILE"', self.installer)
        self.assertNotIn('INSTALL_PROFILE="${GP_INSTALL_PROFILE', self.installer)

        profile_load_pos = self.installer.index('. "$INSTALL_PROFILE"')
        defaults_pos = self.installer.index('REPO_URL="${GP_REPO_URL')
        self.assertLess(profile_load_pos, defaults_pos)

    def test_release_update_install_profile_runtime_regression(self) -> None:
        bash = shutil.which("bash")
        if bash is None:
            git_bash = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe"
            if git_bash.is_file():
                bash = str(git_bash)
        if bash is None:
            self.skipTest("bash is required for installer profile regression probe")

        with tempfile.TemporaryDirectory() as raw:
            work_dir = Path(raw)
            bash_work_dir = subprocess.run(
                [bash, "-lc", "pwd"],
                cwd=work_dir,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            profile = work_dir / "install-profile"
            probe = work_dir / "install-profile-probe.sh"

            def bash_path(path: Path) -> str:
                return f"{bash_work_dir}/{path.name}"

            def write_probe(profile_path: Path) -> None:
                source = self.installer.replace(
                    'INSTALL_PROFILE="/etc/default/gp-control-plane-install-profile"',
                    f"INSTALL_PROFILE={shlex.quote(bash_path(profile_path))}",
                    1,
                )
                source = source.replace(
                    "LEGACY_PROFILE_CAPTURE=off\n",
                    "LEGACY_PROFILE_CAPTURE=off\n"
                    "stat() {\n"
                    "  case \"${2:-}\" in\n"
                    "    %u) printf '%s\\n' \"${GP_TEST_PROFILE_UID:?}\" ;;\n"
                    "    %a) printf '%s\\n' \"${GP_TEST_PROFILE_MODE:?}\" ;;\n"
                    "    *) command stat \"$@\" ;;\n"
                    "  esac\n"
                    "}\n",
                    1,
                )
                marker = 'CURRENT_UID="$(id -u)"'
                replacement = (
                    'if install_web_enabled; then profile_web=enabled; else profile_web=disabled; fi\n'
                    'printf "profile_web=%s core=%s:%s url=%s\\n" "$profile_web" "$CORE_HOST" "$CORE_PORT" "$CORE_URL"\n'
                    'exit 0\n\n'
                    + marker
                )
                self.assertIn(marker, source)
                probe.write_text(source.replace(marker, replacement, 1), encoding="utf-8", newline="\n")
                probe.chmod(0o755)

            def run_probe(*, uid: str = "0", mode: str = "640") -> subprocess.CompletedProcess[str]:
                environment = os.environ | {
                    "GP_INSTALL_FORCE_CLEAN": "on",
                    "GP_INSTALL_WEB": "on",
                    "GP_CORE_HOST": "0.0.0.0",
                    "GP_CORE_PORT": "9999",
                    "GP_CORE_URL": "http://0.0.0.0:9999",
                    "GP_TEST_PROFILE_UID": uid,
                    "GP_TEST_PROFILE_MODE": mode,
                }
                return subprocess.run(
                    [bash, bash_path(probe)],
                    check=False,
                    capture_output=True,
                    env=environment,
                    text=True,
                )

            profile.write_text(
                "GP_INSTALL_WEB='off'\n"
                "GP_CORE_HOST='127.0.0.9'\n"
                "GP_CORE_PORT='18081'\n"
                "GP_CORE_URL='http://127.0.0.9:18081'\n",
                encoding="utf-8",
                newline="\n",
            )
            write_probe(profile)

            accepted = run_probe()
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertEqual(
                accepted.stdout.strip(),
                "profile_web=disabled core=127.0.0.9:18081 url=http://127.0.0.9:18081",
            )

            wrong_owner = run_probe(uid="1000")
            self.assertNotEqual(wrong_owner.returncode, 0)
            self.assertIn("must be root-owned", wrong_owner.stderr)

            writable_profile = run_probe(mode="660")
            self.assertNotEqual(writable_profile.returncode, 0)
            self.assertIn("must be root-owned", writable_profile.stderr)

            profile.unlink()
            symlink = subprocess.run(
                [bash, "-c", 'cd "$1" && ln -s install-profile-target install-profile', "bash", bash_work_dir],
                check=False,
                capture_output=True,
                text=True,
            )
            if symlink.returncode:
                if os.name == "nt":
                    return  # The remaining profile cases ran; Windows lacks symlink creation privilege here.
                self.fail(symlink.stderr)
            write_probe(profile)
            symlink_profile = run_probe()
            self.assertNotEqual(symlink_profile.returncode, 0)
            self.assertIn("not a regular file", symlink_profile.stderr)

    def test_installer_writes_profile_only_after_success_and_legacy_capture_skips_reconfiguration(self) -> None:
        self.assertIn('capture_legacy_install_profile()', self.installer)
        self.assertIn('load_trusted_service_env "$CORE_ENV_FILE"', self.installer)
        self.assertIn('unit_option "$CORE_SERVICE_NAME" --host', self.installer)
        self.assertIn('log "[root-helper] skipped while capturing the existing install profile"', self.installer)
        self.assertIn('log "[service] skipped while capturing the existing install profile"', self.installer)
        self.assertIn('as_root install -m 0640 -o root -g root "$tmp_profile" "$INSTALL_PROFILE"', self.installer)
        self.assertIn('write_install_profile_value GP_INSTALL_WEB "$profile_install_web"', self.installer)
        self.assertIn('write_install_profile_value GP_CORE_URL "$CORE_URL"', self.installer)
        self.assertIn('write_install_profile_value GP_WEB_HOST "$WEB_HOST"', self.installer)
        self.assertIn('write_install_profile_value GP_WEB_PORT "$WEB_PORT"', self.installer)

        check_pos = self.installer.index('if step_log check "Checking installation"; then')
        write_pos = self.installer.index('write_install_profile', check_pos)
        self.assertLess(check_pos, write_pos)
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
