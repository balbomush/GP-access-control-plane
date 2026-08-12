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
        self.assertIn("STRICT_UPSTREAM='https://github.com/balbomush/GP-access-control-plane.git'", self.helper)
        self.assertIn("STRICT_INSTALL_PROFILE='/etc/default/gp-control-plane-install-profile'", self.helper)
        self.assertIn('strict_git -C "\\$stage_repo" fetch --no-tags "\\$STRICT_UPSTREAM" "\\$STRICT_REF"', self.helper)
        self.assertIn('GP_TRUSTED_SOURCE_DIR="\\$stage_repo" bash "\\$installer" --strict-preflight', self.helper)
        self.assertIn("installed_version=", self.helper)
        self.assertIn("status=success", self.helper)
        self.assertIn("status=failed", self.helper)
        self.assertIn("nft-delete-blockcheck-table", self.helper)
        self.assertIn("unsupported run target", self.helper)
        self.assertNotIn("/tmp/*/gp-multidomain-blockcheck.sh", self.helper)
        self.assertNotIn("/var/tmp/*/gp-multidomain-blockcheck.sh", self.helper)
        self.assertIn("write_multidomain_runner", self.helper)
        self.assertIn('BRANCH="${GP_BRANCH:-latest-stable}"', self.installer)

    def test_release_update_forces_clean_checkout_but_manual_install_keeps_dirty_guard(self) -> None:
        self.assertIn('INSTALL_FORCE_CLEAN="${GP_INSTALL_FORCE_CLEAN:-off}"', self.installer)
        self.assertIn("force_clean_enabled()", self.installer)
        self.assertIn("Repository has local changes; release update will discard worktree changes before checkout", self.installer)
        self.assertIn("repo_git reset --hard", self.installer)
        self.assertIn("repo_git clean -fd", self.installer)
        self.assertIn('fail "Repository has local changes: $INSTALL_DIR. Commit or remove them, then run installer again."', self.installer)
        self.assertIn('GP_INSTALL_FORCE_CLEAN=on GP_UPDATE_CANDIDATE_REF="\\$STRICT_REF"', self.helper)

    def test_strict_release_runner_fetches_and_verifies_a_canonical_root_owned_stage_before_publication(self) -> None:
        stage = self.helper.split("queue_strict_update() {", 1)[1].split("validate_run_id()", 1)[0]
        self.assertIn('strict_git ls-remote "\\$STRICT_UPSTREAM" "\\$STRICT_REF"', stage)
        self.assertIn('strict_git -C "\\$stage_repo" fetch --no-tags "\\$STRICT_UPSTREAM" "\\$STRICT_REF"', stage)
        self.assertIn('strict_git -C "\\$stage_repo" checkout --detach "\\$STRICT_SHA"', stage)
        self.assertIn('[ "\\$stage_head" = "\\$STRICT_SHA" ]', stage)
        self.assertIn("strict stage ownership check failed", stage)
        self.assertLess(stage.index('GP_TRUSTED_SOURCE_DIR="\\$stage_repo" bash "\\$installer" --strict-preflight'), stage.index("USER_PUBLISH"))
        self.assertLess(stage.index("USER_PUBLISH"), stage.index("phase=published"))

    def test_discovery_update_gate_is_root_owned_and_taken_after_preflight_before_publication(self) -> None:
        self.assertIn("ensure_root_regular_file()", self.installer)
        self.assertIn(
            "ensure_root_regular_file /run/gp-control-plane/gates/discovery-update.lock 0600 root root",
            self.installer,
        )
        stage = self.helper.split("queue_strict_update() {", 1)[1].split("validate_run_id()", 1)[0]
        gate = stage.split("strict_acquire_update_gate() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('flock -n -x 9', gate)
        self.assertIn("phase=blocked-discovery-gate", gate)
        self.assertIn("status=failed", gate)
        self.assertIn("exit 75", gate)
        self.assertIn('[ -f "\\$STRICT_DISCOVERY_GATE_FILE" ] && [ ! -L "\\$STRICT_DISCOVERY_GATE_FILE" ]', gate)
        self.assertLess(stage.index('bash "\\$installer" --strict-preflight'), stage.index("strict_acquire_update_gate", stage.index('bash "\\$installer" --strict-preflight')))
        self.assertLess(stage.index("strict_acquire_update_gate\n"), stage.index("USER_PUBLISH"))
        self.assertLess(stage.index("USER_PUBLISH"), stage.index("rollback_published_code()"))

    def test_installer_strict_update_accepts_only_tag_pins_and_preflights_before_actions(self) -> None:
        self.assertIn('UPDATE_CANDIDATE_REF="${GP_UPDATE_CANDIDATE_REF:-}"', self.installer)
        self.assertIn('UPDATE_EXPECTED_SHA="${GP_UPDATE_EXPECTED_SHA:-}"', self.installer)
        self.assertIn('validate_pinned_update_inputs()', self.installer)
        self.assertIn('verify_pinned_update_checkout()', self.installer)
        self.assertIn('Strict trusted update requires GP_INSTALL_FORCE_CLEAN=on.', self.installer)
        self.assertIn('Strict trusted update ref must be a full refs/tags/* ref', self.installer)
        self.assertIn('Strict trusted update SHA must be exactly 40 lowercase hexadecimal characters.', self.installer)
        self.assertIn("--strict-preflight", self.installer)

        validate_pos = self.installer.index('validate_pinned_update_inputs')
        verify_pos = self.installer.index('verify_pinned_update_checkout', validate_pos + 1)
        app_pos = self.installer.index('if step_log app "Installing GP Access Control Plane"; then')
        self.assertLess(validate_pos, verify_pos)
        self.assertLess(verify_pos, app_pos)
        self.assertLess(self.installer.index('if [ "$STRICT_PREFLIGHT" = on ]; then'), app_pos)

        app_block = self.installer[app_pos : self.installer.index('\nfi\n\nstrict_migrate_internal_state', app_pos)]
        self.assertIn('if ! pinned_update_enabled; then', app_block)
        for source_action in (
            'resolve_install_ref',
            'repo_git fetch origin "$BRANCH" || true',
            'repo_git pull --ff-only origin "$BRANCH"',
            'repo_git checkout -B "$BRANCH" "origin/$BRANCH"',
            'repo_git checkout --detach "$BRANCH"',
        ):
            self.assertIn(source_action, app_block)
        self.assertNotIn('GP_UPDATE_CANDIDATE_REF', app_block)

    def test_root_helper_queue_update_has_one_exact_unambiguous_argv_contract(self) -> None:
        dispatch = self.helper.split('  queue-update)\n', 1)[1].split('  *)', 1)[0]
        self.assertIn('[ "$#" -eq 4 ] && [ "$1" = --candidate-ref ] && [ "$3" = --expected-sha ]', dispatch)
        self.assertIn('queue_strict_update "$2" "$4"', dispatch)
        self.assertNotIn('resolve-update-candidate', self.helper)
        self.assertNotIn('queue_update_legacy', self.helper)
        validator = self.helper.split('validate_update_candidate_ref() {', 1)[1].split('\n}\n', 1)[0]
        self.assertIn('refs/tags/*)', validator)
        self.assertNotIn('refs/heads/*', validator)
        self.assertIn('expected SHA must be 40 lowercase hexadecimal characters', self.helper)

    def test_strict_success_log_and_post_publication_failure_use_code_rollback_contract(self) -> None:
        stage = self.helper.split("queue_strict_update() {", 1)[1].split("validate_run_id()", 1)[0]
        success = stage.split("if run_staged_installer; then", 1)[1]
        for key in (
            "phase=installed",
            "status=success",
            "verified_ref=",
            "verified_sha=",
            "checked_out_sha=",
            "installed_ref=",
            "installed_sha=",
            "installed_version=",
        ):
            self.assertIn(key, success)
        self.assertIn('installed_version="\\${installed_version#v}"', success)
        rollback = stage.split("rollback_published_code() {", 1)[1]
        self.assertIn("USER_ROLLBACK", rollback)
        self.assertIn('mv "\\$install_dir" "\\$failed"', rollback)
        self.assertIn('mv "\\$previous" "\\$install_dir"', rollback)
        self.assertIn('systemctl restart "\\$STRICT_CORE_SERVICE"', rollback)
        self.assertIn('rollback_scope=code', rollback)
        self.assertIn("restore_deployment_configuration", rollback)

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

    def test_fresh_installs_default_to_sibling_state_but_manual_legacy_installs_stay_put(self) -> None:
        self.assertIn('STATE_DIR="$(default_state_dir)"', self.installer)
        self.assertIn(
            'elif ! strict_update_requested && [ -d "$INSTALL_DIR/build/state" ] && [ ! -L "$INSTALL_DIR/build/state" ]; then',
            self.installer,
        )
        self.assertIn('STATE_DIR="$INSTALL_DIR/build/state"', self.installer)

    def test_strict_update_state_migration_contract_preserves_backups_and_switches_services(self) -> None:
        migration = self.installer.split("strict_migrate_internal_state() {", 1)[1].split("\n}\n\nrepo_git()", 1)[0]
        self.assertIn('pinned_update_enabled || fail', migration)
        self.assertIn('GP_STRICT_STATE_MIGRATION_SOURCE', migration)
        self.assertIn('GP_STRICT_STATE_MIGRATION_ROOT', migration)
        self.assertIn('expected_root="$(dirname -- "$INSTALL_DIR")/.$(basename -- "$INSTALL_DIR").data"', migration)
        self.assertIn('[ ! -e "$migration_root" ] && [ ! -L "$migration_root" ]', migration)
        self.assertIn('migration_backups="$migration_source_parent/backups"', migration)
        self.assertIn('run_as_target cp -a -- "$migration_source" "$migration_stage/state"', migration)
        self.assertIn('run_as_target cp -a -- "$migration_backups" "$migration_stage/backups"', migration)
        self.assertIn('run_as_target mv -- "$migration_stage" "$migration_root"', migration)
        self.assertIn('STATE_DIR="$migration_root/state"', migration)
        self.assertIn('state_layout=migrated', migration)
        self.assertLess(migration.index('systemctl stop "$CORE_SERVICE_NAME"'), migration.index('cp -a -- "$migration_source"'))
        self.assertLess(migration.index('cp -a -- "$migration_backups"'), migration.index('mv -- "$migration_stage"'))

    def test_strict_update_helper_migrates_only_internal_state_and_rolls_back_configuration(self) -> None:
        helper = self.helper
        self.assertIn("prepare_strict_state_layout()", helper)
        layout = helper.split("prepare_strict_state_layout() {", 1)[1].split("\n}\n\nqueue_strict_update", 1)[0]
        self.assertIn('strict_canonical_directory "$strict_install_dir" GP_INSTALL_DIR', layout)
        self.assertIn('strict_canonical_directory "$strict_state_dir" GP_STATE_DIR', layout)
        self.assertIn('"$strict_install_dir_resolved"/*)', layout)
        self.assertIn('strict_state_layout=external', layout)
        self.assertIn('[ ! -e "$strict_data_root" ] && [ ! -L "$strict_data_root" ]', layout)
        publish = helper.split("USER_PUBLISH'", 1)[1].split("echo 'phase=published'", 1)[0]
        self.assertNotIn('cp -a "$state_dir"', publish)
        self.assertIn("snapshot_deployment_configuration", helper)
        self.assertIn("restore_deployment_configuration", helper)
        self.assertIn("STRICT_CORE_ENV_FILE", helper)
        self.assertIn("STRICT_WEB_ENV_FILE", helper)
        self.assertIn("GP_STRICT_STATE_MIGRATION=on", helper)
        staged_runner = helper.split("run_staged_installer() {", 1)[1].split("\n}\n\necho 'phase=root'", 1)[0]
        self.assertIn('if [ "\\$STRICT_STATE_LAYOUT" = internal ]; then', staged_runner)
        self.assertIn('GP_STRICT_STATE_MIGRATION=on', staged_runner)
        self.assertIn('else\n    env -i', staged_runner)
        self.assertIn("systemctl is-active --quiet", helper)
        rollback = helper.split("rollback_published_code() {", 1)[1].split("\n}\n\nrollback_after_publication_failure", 1)[0]
        self.assertLess(rollback.index("restore_deployment_configuration"), rollback.index("runuser -u"))
        self.assertIn("rollback_scope=code", rollback)
        self.assertNotIn('rm -rf -- "\\$STRICT_DATA_ROOT"', helper)

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

    def test_strict_update_uses_only_a_regular_root_root_0600_resolved_install_profile(self) -> None:
        self.assertIn('INSTALL_PROFILE="/etc/default/gp-control-plane-install-profile"', self.installer)
        self.assertIn('strict_update_requested()', self.installer)
        self.assertIn('validate_install_profile_file()', self.installer)
        self.assertIn('[ -e "$INSTALL_PROFILE" ] || [ -L "$INSTALL_PROFILE" ]', self.installer)
        self.assertIn('[ -f "$INSTALL_PROFILE" ] && [ ! -L "$INSTALL_PROFILE" ]', self.installer)
        self.assertIn("stat -c '%u:%g:%a' \"$INSTALL_PROFILE\"", self.installer)
        self.assertIn('= "0:0:600"', self.installer)
        self.assertIn("[ \"$(stat -c '%u:%g:%a' \"$STRICT_INSTALL_PROFILE\"", self.helper)
        self.assertIn("= '0:0:600'", self.helper)
        self.assertIn('load_trusted_env_values "$INSTALL_PROFILE"', self.installer)
        self.assertIn('read_trusted_env_value()', self.installer)
        self.assertIn('trusted_env_reader=(sudo awk)', self.installer)
        self.assertNotIn('. "$INSTALL_PROFILE"', self.installer)
        self.assertNotIn('INSTALL_PROFILE="${GP_INSTALL_PROFILE', self.installer)

        profile_load_pos = self.installer.index('if strict_update_requested; then')
        defaults_pos = self.installer.index('REPO_URL="${GP_REPO_URL')
        self.assertLess(profile_load_pos, defaults_pos)

    def test_strict_update_allowlists_and_revalidates_all_privileged_profile_destinations(self) -> None:
        # This remains source-level because Windows CI cannot create root-owned
        # /etc and /run ancestors.  It locks the two independent Linux runtime
        # gates and their ordering before queue/publication and root writes.
        helper_gate_start = self.helper.index('validate_strict_privileged_destinations() {')
        helper_gate_end = self.helper.index('\nload_strict_install_profile()', helper_gate_start)
        helper_gate = self.helper[helper_gate_start:helper_gate_end]
        installer_gate_start = self.installer.index('validate_strict_privileged_destinations() {')
        installer_gate_end = self.installer.index('\nrun_zapret_install_bin()', installer_gate_start)
        installer_gate = self.installer[installer_gate_start:installer_gate_end]

        expected = (
            ('GP_CORE_ENV_FILE', 'strict_core_env_file', 'CORE_ENV_FILE', '/etc/default/gp-control-plane-core'),
            ('GP_WEB_ENV_FILE', 'strict_web_env_file', 'WEB_ENV_FILE', '/etc/default/gp-control-plane-web'),
            ('GP_ROOT_HELPER_PATH', 'strict_root_helper_path', 'ROOT_HELPER_PATH', '/usr/local/libexec/gp-control-plane/gp-root-helper'),
            ('GP_ROOT_HELPER_CONFIG', 'strict_root_helper_config', 'ROOT_HELPER_CONFIG', '/etc/default/gp-control-plane-root-helper'),
            ('GP_ROOT_HELPER_RUN_DIR', 'strict_root_helper_run_dir', 'ROOT_HELPER_RUN_DIR', '/run/gp-control-plane/runs'),
            ('GP_SUDOERS_PATH', 'strict_sudoers_path', 'SUDOERS_PATH', '/etc/sudoers.d/gp-control-plane-root-helper'),
            ('GP_ZAPRET_DIR', 'strict_zapret_dir', 'ZAPRET_DIR', '/opt/zapret2'),
        )
        for key, helper_value, installer_value, default in expected:
            with self.subTest(profile_key=key):
                self.assertIn(f'strict_require_profile_path {key} "${helper_value}" {default}', helper_gate)
                self.assertIn(f'strict_require_fixed_privileged_path {key} "${installer_value}" {default}', installer_gate)

        self.assertIn('strict_safe_root_target "$strict_core_env_file" file GP_CORE_ENV_FILE', helper_gate)
        self.assertIn('strict_safe_root_target "$CORE_ENV_FILE" file GP_CORE_ENV_FILE', installer_gate)
        for source, parent_check, target_check in (
            (self.helper, '[ -d "$strict_parent" ] && [ ! -L "$strict_parent" ]', '[ -f "$strict_target" ] && [ ! -L "$strict_target" ]'),
            (self.installer, 'as_root test -d "$strict_parent" && ! as_root test -L "$strict_parent"', 'as_root test -f "$strict_target" && ! as_root test -L "$strict_target"'),
        ):
            self.assertIn('strict_safe_root_parent_chain()', source)
            self.assertIn('strict_safe_root_target()', source)
            self.assertIn(parent_check, source)
            self.assertIn('[ "$strict_parent_uid" = 0 ]', source)
            self.assertIn('?????w*|????????w*', source)
            self.assertIn(target_check, source)
            self.assertIn('[ "$strict_target_uid" = 0 ]', source)

        profile_load = self.helper.index('load_strict_install_profile\n', helper_gate_end)
        stage_dirs = self.helper.index('ensure_strict_root_dir "$STRICT_RUN_DIR"', profile_load)
        publication = self.helper.index('runuser -u "\\$STRICT_USER" -- /bin/sh -s -- "\\$STRICT_BUNDLE"', stage_dirs)
        self.assertLess(profile_load, stage_dirs)
        self.assertLess(helper_gate_start, publication)
        self.assertIn('strict_validate_config_targets || return 1', self.helper)
        self.assertIn('strict_safe_config_target "\\$restore_target" || return 1', self.helper)

        strict_validation = self.installer.index('validate_strict_privileged_destinations\nverify_pinned_update_checkout')
        preflight = self.installer.index('if [ "$STRICT_PREFLIGHT" = on ]; then')
        self.assertLess(strict_validation, preflight)
        for write_phase in (
            'if step_log zapret "Installing zapret2"; then\n  validate_strict_privileged_destinations',
            'elif step_log root-helper "Installing GP root helper"; then\n  validate_strict_privileged_destinations',
            'elif step_log service "Creating and starting systemd service"; then\n  validate_strict_privileged_destinations',
            'then\n  validate_strict_privileged_destinations\n  log "Writing root-owned resolved install profile"',
        ):
            self.assertIn(write_phase, self.installer)

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
                    "    %g) printf '%s\\n' \"${GP_TEST_PROFILE_GID:?}\" ;;\n"
                    "    %a) printf '%s\\n' \"${GP_TEST_PROFILE_MODE:?}\" ;;\n"
                    "    *) command stat \"$@\" ;;\n"
                    "  esac\n"
                    "}\n",
                    1,
                )
                source = source.replace('if [ "$(id -u)" -eq 0 ]; then', 'if true; then', 1)
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

            def run_probe(*, uid: str = "0", gid: str = "0", mode: str = "600") -> subprocess.CompletedProcess[str]:
                environment = os.environ | {
                    "GP_INSTALL_FORCE_CLEAN": "on",
                    "GP_INSTALL_WEB": "on",
                    "GP_CORE_HOST": "0.0.0.0",
                    "GP_CORE_PORT": "9999",
                    "GP_CORE_URL": "http://0.0.0.0:9999",
                    "GP_TEST_PROFILE_UID": uid,
                    "GP_TEST_PROFILE_GID": gid,
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
            profile.chmod(0o600)
            write_probe(profile)

            accepted = run_probe()
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertEqual(
                accepted.stdout.strip(),
                "profile_web=disabled core=127.0.0.9:18081 url=http://127.0.0.9:18081",
            )

            wrong_owner = run_probe(uid="1000")
            self.assertNotEqual(wrong_owner.returncode, 0)
            self.assertIn("root:root mode 0600", wrong_owner.stderr)

            wrong_group = run_probe(gid="1000")
            self.assertNotEqual(wrong_group.returncode, 0)
            self.assertIn("root:root mode 0600", wrong_group.stderr)

            writable_profile = run_probe(mode="660")
            self.assertNotEqual(writable_profile.returncode, 0)
            self.assertIn("root:root mode 0600", writable_profile.stderr)

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
        trusted_env_start = self.installer.index('load_trusted_service_env() {')
        trusted_env_end = self.installer.index('\ncapture_legacy_install_profile()', trusted_env_start)
        trusted_env_loader = self.installer[trusted_env_start:trusted_env_end]
        self.assertIn('load_trusted_env_values "$service_env_file"', trusted_env_loader)
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
            ).replace("/etc/systemd/system", shlex.quote(bash_path(systemd_dir)))
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
                        "GP_INSTALL_FORCE_CLEAN": "on",
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
