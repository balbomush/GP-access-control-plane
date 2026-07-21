from __future__ import annotations

import unittest
from pathlib import Path


class InstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.installer = (root / "scripts" / "install-raspberry-pi.sh").read_text(encoding="utf-8")
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
        self.assertIn('BRANCH="${GP_BRANCH:-v0.3.4}"', self.installer)

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
        self.assertIn('BRANCH="${GP_BRANCH:-v0.3.4}"', self.installer)
        self.assertNotIn('BRANCH="${GP_BRANCH:-main}"', self.installer)
        self.assertIn('repo_git fetch origin "$BRANCH" || true', self.installer)
        self.assertIn('repo_git fetch origin "+refs/tags/$BRANCH:refs/tags/$BRANCH" || true', self.installer)
        self.assertIn('repo_git checkout -B "$BRANCH" "origin/$BRANCH"', self.installer)
        self.assertIn('repo_git checkout --detach "$BRANCH"', self.installer)
        self.assertIn('fail "Cannot find branch or tag: $BRANCH"', self.installer)

    def test_installer_service_uses_install_dir_state_and_memory_limits(self) -> None:
        self.assertIn("GP_SERVICE_MEMORY_HIGH", self.installer)
        self.assertIn("GP_SERVICE_MEMORY_MAX", self.installer)
        self.assertIn("MemoryAccounting=true", self.installer)
        self.assertIn("MemoryHigh=$SERVICE_MEMORY_HIGH", self.installer)
        self.assertIn("MemoryMax=$SERVICE_MEMORY_MAX", self.installer)
        self.assertIn("WorkingDirectory=$INSTALL_DIR", self.installer)
        self.assertIn("Environment=PATH=$SERVICE_PATH", self.installer)
        self.assertIn("WEB_ENV_FILE", self.installer)
        self.assertIn('RUNTIME_ENV_FILE="$WEB_ENV_FILE"', self.installer)
        self.assertIn('RUNTIME_ENV_FILE="$CORE_ENV_FILE"', self.installer)
        self.assertIn("GP_STATE_DIR", self.installer)
        self.assertIn("GP_STATE_DIR='%s'", self.installer)
        self.assertIn('INSTALL_WEB="${GP_INSTALL_WEB:-on}"', self.installer)
        self.assertIn('CORE_SERVICE_NAME="${GP_CORE_SERVICE_NAME:-gp-control-plane-core.service}"', self.installer)
        self.assertIn('CORE_HOST="${GP_CORE_HOST:-127.0.0.1}"', self.installer)
        self.assertIn('CORE_PORT="${GP_CORE_PORT:-8081}"', self.installer)
        self.assertIn('CORE_ENV_FILE="${GP_CORE_ENV_FILE:-/etc/default/gp-control-plane-core}"', self.installer)
        self.assertIn("install_web_enabled()", self.installer)
        self.assertIn('RUNTIME_COMMAND="web"', self.installer)
        self.assertIn('RUNTIME_COMMAND="core"', self.installer)
        self.assertIn('RUNTIME_DESCRIPTION="GP Strategy Finder Core API"', self.installer)
        self.assertIn("EnvironmentFile=-$RUNTIME_ENV_FILE", self.installer)
        self.assertIn("ExecStart=$INSTALL_DIR/.venv/bin/gp-control-plane $RUNTIME_COMMAND --host $RUNTIME_HOST --port $RUNTIME_PORT", self.installer)
        self.assertIn('as_root install -m 0644 -o root -g root "$TMP_SERVICE" "/etc/systemd/system/$RUNTIME_SERVICE_NAME"', self.installer)
        self.assertIn('TMP_SERVICE="$(mktemp)"', self.installer)
        self.assertNotIn("--config", self.installer)
        self.assertNotIn("orchestrator.example.yaml", self.installer)

    def test_installer_prepares_v2fly_with_service_config_but_keeps_install_non_blocking(self) -> None:
        self.assertIn("Preparing local v2fly domain catalog", self.installer)
        self.assertIn("prepare_v2fly_local_catalog", self.installer)
        self.assertIn(
            'cd "$1" && GP_STATE_DIR="$1/build/state" "$1/.venv/bin/gp-control-plane" domain-sources prepare-v2fly',
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

    def test_installer_supports_one_command_and_individual_steps(self) -> None:
        self.assertIn('REQUESTED_STEPS="${GP_INSTALL_STEPS:-all}"', self.installer)
        self.assertIn("--step STEP", self.installer)
        self.assertIn("packages,zapret,app,v2fly,root-helper,service,check", self.installer)
        for step in ("packages", "zapret", "app", "v2fly", "root-helper", "service", "check"):
            self.assertIn(f"step_log {step}", self.installer)

    def test_installer_generates_web_auth_env_for_systemd(self) -> None:
        self.assertIn('WEB_AUTH="${GP_WEB_AUTH:-on}"', self.installer)
        self.assertIn("GP_WEB_TOKEN", self.installer)
        self.assertIn("generate_web_token", self.installer)
        self.assertIn("install_web_env_file", self.installer)
        self.assertIn("GP_WEB_AUTH=%s", self.installer)
        self.assertIn("GP_WEB_TOKEN='%s'", self.installer)

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
