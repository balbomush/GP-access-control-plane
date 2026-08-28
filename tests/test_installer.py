from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
import os
from pathlib import Path


class CleanInstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.bootstrap = (root / "scripts" / "bootstrap-linux.sh").read_text(encoding="utf-8")
        cls.installer = (root / "scripts" / "install-linux.sh").read_text(encoding="utf-8")

    def test_user_flow_accepts_only_exact_annotated_tag_and_one_sudo(self) -> None:
        self.assertIn('TAG="${GP_BRANCH:-}"', self.bootstrap)
        self.assertIn("exact release tag vX.Y.Z", self.bootstrap)
        self.assertIn('cat-file -t "refs/tags/$TAG"', self.bootstrap)
        self.assertIn('python3 "$source_dir/scripts/clean-install-vault.py"', self.bootstrap)
        self.assertEqual(self.bootstrap.count("sudo --"), 1)
        self.assertIn('git -C "$source_dir" status --porcelain', self.bootstrap)
        self.assertIn('if [ -d "$legacy_state" ]', self.bootstrap)
        self.assertIn('--verify --state-dir "$legacy_state"', self.bootstrap)
        self.assertLess(self.bootstrap.index('--verify --state-dir "$legacy_state"'), self.bootstrap.index('python3 "$source_dir/scripts/clean-install-vault.py" --state-dir'))
        self.assertIn('initial_install=on', self.bootstrap)
        self.assertIn('--initial-install "$initial_install"', self.bootstrap)
        for forbidden in ("latest-stable", "refs/heads", "GP_EXPECTED_SHA", "candidate", "rollback", "clean-remove"):
            self.assertNotIn(forbidden, self.bootstrap)

    def test_root_process_verifies_vault_before_fixed_removal_and_installs_both_topologies(self) -> None:
        verify = self.installer.index('runuser -u "$INSTALL_USER" -- python3 "$vault_tool" --verify')
        removal = self.installer.index('rm -rf --one-file-system -- /usr/local/libexec/gp-control-plane')
        self.assertLess(verify, removal)
        self.assertIn('gp-control-plane-core.service', self.installer)
        self.assertIn('if [ "$INSTALL_WEB" = on ]', self.installer)
        self.assertIn('git -C "$SOURCE_DIR" status --porcelain', self.installer)
        self.assertIn('managed GP root is not canonical', self.installer)
        self.assertIn('gp-control-plane-root-helper', self.installer)
        self.assertIn('case "$INITIAL_INSTALL" in on|off)', self.installer)
        self.assertIn('if [ "$INITIAL_INSTALL" = off ]; then', self.installer)
        self.assertIn('visudo -cf /etc/sudoers.d/gp-control-plane-root-helper', self.installer)
        self.assertIn('scripts/install-zapret2.sh', self.installer)
        self.assertIn('zapret2 runtime is not ready', self.installer)
        self.assertIn('/usr/local/libexec/gp-control-plane/nfqws2', self.installer)
        self.assertIn('/usr/local/libexec/gp-control-plane/blockcheck2.sh', self.installer)
        self.assertIn('Environment=PATH=/usr/local/libexec/gp-control-plane:', self.installer)
        self.assertNotIn('/usr/local/bin/nfqws2', self.installer)
        self.assertNotIn('/usr/local/bin/blockcheck2.sh', self.installer)
        self.assertNotIn('rm -rf --one-file-system -- /usr/local/bin', self.installer)
        self.assertNotIn('rm -f -- /usr/local/bin', self.installer)
        self.assertNotIn("\nsudo ", self.installer)
        for forbidden in ("adapter", "provision", "preflight", "manifest", "candidate", "rollback", "snapshot"):
            self.assertNotIn(forbidden, self.installer)

    def test_retired_transition_entrypoints_are_absent(self) -> None:
        root = Path(__file__).resolve().parents[1] / "scripts"
        for name in ("clean-install-vault.sh", "gp-clean-remove-root.sh", "gp-clean-remove-preflight.sh", "gp-clean-remove-provision-root.sh", "legacy-bootstrap.sh", "legacy-bootstrap-launcher.sh"):
            self.assertFalse((root / name).exists(), name)

    def test_shell_syntax(self) -> None:
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash is required")
        root = Path(__file__).resolve().parents[1]
        for script in ("bootstrap-linux.sh", "install-linux.sh"):
            result = subprocess.run([bash, "-n", str(root / "scripts" / script)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_gp_owned_zapret_wrapper_is_discoverable_in_service_path_model(self) -> None:
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash is required")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); libexec = root / "usr" / "local" / "libexec" / "gp-control-plane"; zapret = root / "opt" / "zapret2"
            libexec.mkdir(parents=True); zapret.mkdir(parents=True)
            target = zapret / "nfqws2"; target.write_text("#!/bin/sh\nprintf 'ready:%s\\n' \"$1\"\n", encoding="utf-8"); target.chmod(0o755)
            wrapper = libexec / "nfqws2"; wrapper.write_text(f"#!/bin/sh\nexec '{target.as_posix()}' \"$@\"\n", encoding="utf-8"); wrapper.chmod(0o755)
            result = subprocess.run([bash, "-c", "command -v nfqws2; nfqws2 probe"], env={"PATH": str(libexec)}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(str(wrapper), result.stdout)
            self.assertIn("ready:probe", result.stdout)

    def test_bootstrap_reuses_pending_vault_across_preclean_failure_then_retry(self) -> None:
        git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
        bash = shutil.which("bash") or (str(git_bash) if git_bash.is_file() else None)
        if not bash:
            self.skipTest("bash is required")
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as raw:
            sandbox = Path(raw); fake_bin = sandbox / "bin"; home = sandbox / "home"; log = sandbox / "calls.log"
            fake_bin.mkdir(); (home / "gp" / "GP-access-control-plane" / "build" / "state").mkdir(parents=True)
            def bash_path(path: Path) -> str:
                raw_path = path.resolve().as_posix()
                return f"/{raw_path[0].lower()}{raw_path[2:]}" if len(raw_path) > 2 and raw_path[1] == ":" else raw_path
            def fake(name: str, body: str) -> None:
                path = fake_bin / name; path.write_text("#!/usr/bin/env bash\nset -eu\n" + body, encoding="utf-8"); path.chmod(0o755)
            fake("id", 'case "$1" in -u) echo 1000;; -un) echo gpuser;; *) exit 64;; esac\n')
            fake("git", 'case "$1" in clone) dest="${!#}"; mkdir -p "$dest/scripts";; -C) shift 2; case "$1" in cat-file) echo tag;; rev-parse) echo deadbeef;; checkout) :;; status) :;; *) exit 64;; esac;; *) exit 64;; esac\n')
            fake("python3", 'case " $* " in *" --verify "*) echo VERIFY >> "$TEST_LOG"; exit 0;; *) echo CREATE >> "$TEST_LOG"; exit 99;; esac\n')
            fake("sudo", 'echo SUDO >> "$TEST_LOG"; exit "${SUDO_RESULT:-73}"\n')
            env = {**os.environ, "HOME": bash_path(home), "GP_BRANCH": "v0.4.0", "TEST_LOG": bash_path(log)}
            invoke = [bash, "--noprofile", "--norc", "-c", 'PATH="$1:/usr/bin:/bin"; export PATH; exec "$2"', "bash", bash_path(fake_bin), str(root / "scripts" / "bootstrap-linux.sh")]
            first = subprocess.run(invoke, env={**env, "SUDO_RESULT": "73"}, capture_output=True, text=True)
            self.assertEqual(first.returncode, 73, first.stderr)
            second = subprocess.run(invoke, env={**env, "SUDO_RESULT": "0"}, capture_output=True, text=True)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(log.read_text(encoding="utf-8").splitlines(), ["VERIFY", "SUDO", "VERIFY", "SUDO"])


if __name__ == "__main__":
    unittest.main()
