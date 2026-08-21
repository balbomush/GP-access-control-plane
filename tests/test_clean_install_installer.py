from __future__ import annotations

import re
import unittest
from pathlib import Path


class TrustedCleanInstallInstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.source = (root / "scripts" / "install-linux.sh").read_text(encoding="utf-8")

    @staticmethod
    def shell_function(source: str, name: str) -> str:
        match = re.search(
            rf"^{re.escape(name)}\(\) \{{\n(?P<body>.*?)(?=^\}}$)",
            source,
            re.MULTILINE | re.DOTALL,
        )
        if not match:
            raise AssertionError(f"Bash function {name} was not found")
        return match.group("body")

    def test_trusted_clean_install_has_exact_one_argument_grammar(self) -> None:
        self.assertIn(
            'if [ "$#" -eq 1 ] && [ "${1:-}" = "--trusted-clean-install" ]; then',
            self.source,
        )
        self.assertIn(
            '--trusted-clean-install)\n'
            '      [ "$TRUSTED_CLEAN_INSTALL" = on ] && [ "$#" -eq 1 ]',
            self.source,
        )
        self.assertNotIn("--trusted-clean-install=", self.source)

    def test_trusted_clean_install_rejects_legacy_force_clean_and_caller_controls(self) -> None:
        controls = self.source[
            self.source.index('TRUSTED_CLEAN_INSTALL_CALLER_CONTROLS=""') : self.source.index("release_update_enabled()")
        ]
        for name in (
            "GP_INSTALL_FORCE_CLEAN",
            "GP_TRUSTED_SOURCE_DIR",
            "GP_UPDATE_CANDIDATE_REF",
            "GP_UPDATE_EXPECTED_SHA",
            "GP_INSTALL_CONFIG",
            "GP_INSTALL_DIR",
            "GP_STATE_DIR",
            "GP_BRANCH",
        ):
            self.assertIn(name, controls)
        reject = self.shell_function(self.source, "reject_trusted_clean_install_caller_controls")
        self.assertIn("TRUSTED_CLEAN_INSTALL_CALLER_CONTROLS", reject)
        self.assertIn("rejects caller environment control", reject)
        self.assertIn("validate_trusted_clean_install_invocation", self.source)

    def test_trusted_clean_install_requires_root_owned_canonical_staged_runner(self) -> None:
        invocation = self.shell_function(self.source, "validate_trusted_clean_install_invocation")
        self.assertIn('[ "$CURRENT_UID" -eq 0 ]', invocation)
        self.assertIn('TRUSTED_CLEAN_INSTALL_MARKER="/run/gp-control-plane/trusted-clean-install.authorized"', self.source)
        self.assertIn("root:root mode 0600", invocation)
        self.assertIn('"trusted-clean-install-v1 $SCRIPT_SOURCE_DIR"', invocation)
        self.assertIn('readlink -f -- "$SCRIPT_SOURCE_DIR"', invocation)
        self.assertIn('trusted_source_directory "$SCRIPT_SOURCE_DIR"', invocation)
        self.assertIn('trusted_source_file "$SCRIPT_PATH"', invocation)

    def test_trusted_clean_preflight_bypasses_ordinary_pinned_update_validation(self) -> None:
        pinned = self.shell_function(self.source, "pinned_update_enabled")
        self.assertIn('[ "$PINNED_UPDATE" = on ]', pinned)
        self.assertNotIn("TRUSTED_CLEAN_INSTALL", pinned)

        pinned_inputs = self.shell_function(self.source, "validate_pinned_update_inputs")
        self.assertIn('[ "$TRUSTED_CLEAN_INSTALL" = on ] && return 0', pinned_inputs)

        preflight = self.shell_function(self.source, "validate_trusted_clean_install_preflight")
        self.assertIn("validate_trusted_clean_install_invocation", preflight)
        self.assertIn("validate_trusted_clean_install_target", preflight)
        self.assertNotIn("validate_pinned_update_inputs", preflight)
        self.assertNotIn("verify_pinned_update_checkout", preflight)

        validation = self.source[self.source.index("validate_pinned_update_inputs\n") : self.source.index('if [ "$STRICT_PREFLIGHT" = on ];')]
        self.assertIn(
            'if [ "$TRUSTED_CLEAN_INSTALL" = on ]; then\n'
            '  validate_trusted_clean_install_preflight\n'
            'else\n'
            '  validate_strict_privileged_destinations\n'
            '  verify_pinned_update_checkout\n'
            'fi',
            validation,
        )

    def test_marker_failure_is_checked_before_any_install_mutation(self) -> None:
        invocation = self.shell_function(self.source, "validate_trusted_clean_install_invocation")
        self.assertIn("TRUSTED_CLEAN_INSTALL_MARKER", invocation)
        self.assertIn("authorization marker is not a regular file", invocation)
        self.assertIn("authorization marker must be root:root mode 0600", invocation)
        self.assertIn("authorization marker does not match the canonical staged source", invocation)

        preflight_call = self.source.index("  validate_trusted_clean_install_preflight")
        first_install_log = self.source.index('log "Installing for user: $TARGET_USER"')
        first_mutation = self.source.index('if step_log packages "Updating package index and installing required packages"; then')
        self.assertLess(preflight_call, first_install_log)
        self.assertLess(preflight_call, first_mutation)

    def test_trusted_clean_install_preserves_vault_and_never_uses_generic_worktree_cleanup(self) -> None:
        self.assertIn(
            'CLEAN_INSTALL_VAULT_DIR="$TARGET_HOME/.local/share/gp-control-plane/clean-install-vault"',
            self.source,
        )
        exclusion = self.shell_function(self.source, "assert_clean_install_vault_excluded")
        self.assertIn('"$CLEAN_INSTALL_VAULT_DIR"|"$CLEAN_INSTALL_VAULT_DIR"/*', exclusion)
        target = self.shell_function(self.source, "validate_trusted_clean_install_target")
        self.assertIn('assert_clean_install_vault_excluded "$INSTALL_DIR"', target)
        self.assertIn('assert_clean_install_vault_excluded "$STATE_DIR"', target)
        candidate = self.shell_function(self.source, "install_trusted_clean_candidate")
        self.assertIn('requires the fixed profile install target to be absent', candidate)
        self.assertIn('as_root cp -a -- "$SCRIPT_SOURCE_DIR/." "$INSTALL_DIR"', candidate)
        self.assertIn('as_root chown -R --no-dereference "$TARGET_USER:$TARGET_GROUP" "$INSTALL_DIR"', candidate)
        self.assertNotIn("repo_git reset --hard", candidate)
        self.assertNotIn("repo_git clean -fd", candidate)

    def test_trusted_clean_root_helper_never_uses_user_owned_installed_copy(self) -> None:
        trusted_file = self.shell_function(self.source, "trusted_project_file")
        self.assertIn('project_source_root="$SCRIPT_SOURCE_DIR"', trusted_file)
        self.assertIn('readlink -f -- "$project_source_path"', trusted_file)
        self.assertIn('trusted_source_file "$project_source_path"', trusted_file)

        root_helper_block = self.source[
            self.source.index('elif step_log root-helper "Installing GP root helper"; then') : self.source.index(
                '  ensure_root_directory /run/gp-control-plane/gates',
                self.source.index('elif step_log root-helper "Installing GP root helper"; then'),
            )
        ]
        self.assertIn('if pinned_update_enabled || trusted_clean_install_enabled; then', root_helper_block)
        self.assertIn('ROOT_HELPER_SOURCE="$(trusted_project_file scripts/gp-root-helper.sh)"', root_helper_block)
        trusted_branch = root_helper_block.split('if pinned_update_enabled || trusted_clean_install_enabled; then', 1)[1].split('else', 1)[0]
        self.assertNotIn('$INSTALL_DIR/scripts/gp-root-helper.sh', trusted_branch)

    def test_trusted_clean_installer_defers_all_service_activation_to_root_runner(self) -> None:
        service_block = self.source[
            self.source.index('elif step_log service "Creating and starting systemd service"; then') : self.source.index(
                'if step_log check "Checking installation"; then'
            )
        ]
        self.assertIn('if trusted_clean_install_enabled; then', service_block)
        self.assertIn("service activation is deferred to the root transaction runner", service_block)
        trusted_core = service_block.split('if trusted_clean_install_enabled; then', 1)[1].split('else', 1)[0]
        self.assertNotIn('systemctl enable "$CORE_SERVICE_NAME"', trusted_core)
        self.assertNotIn('systemctl restart "$CORE_SERVICE_NAME"', trusted_core)
        trusted_web = service_block.split('if ! trusted_clean_install_enabled; then', 1)[1].split('fi', 1)[0]
        self.assertIn('systemctl enable "$SERVICE_NAME"', trusted_web)
        self.assertIn('systemctl restart "$SERVICE_NAME"', trusted_web)

    def test_trusted_clean_keeps_root_owned_0711_parent_lock_while_creating_user_child(self) -> None:
        candidate = self.shell_function(self.source, "install_trusted_clean_candidate")
        self.assertIn("transaction runner owns the existing parent as root:root 0711", candidate)
        self.assertIn('as_root test -d "$(dirname -- "$INSTALL_DIR")"', candidate)
        trusted_branch = candidate.split("if trusted_clean_install_enabled; then", 1)[1].split("else", 1)[0]
        self.assertNotIn('install -d -m 0755 -o "$TARGET_USER"', trusted_branch)
        self.assertIn('as_root install -d -m 0755 -o "$TARGET_USER" -g "$TARGET_GROUP" "$INSTALL_DIR"', candidate)

    def test_ordinary_installer_modes_remain_available(self) -> None:
        self.assertIn("--step)", self.source)
        self.assertIn("--steps)", self.source)
        self.assertIn("--strict-preflight)", self.source)
        self.assertIn("force_clean_enabled()", self.source)
        self.assertIn("repo_git reset --hard", self.source)


if __name__ == "__main__":
    unittest.main()
