from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


class LegacyCleanHandoffContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.payload = (cls.root / "scripts" / "legacy-bootstrap.sh").read_text(encoding="utf-8")
        cls.launcher = (cls.root / "scripts" / "legacy-bootstrap-launcher.sh").read_text(encoding="utf-8")

    def test_both_supported_baselines_need_the_user_level_handoff(self) -> None:
        for tag in ("v0.3.4", "v0.3.5-alpha.4"):
            with self.subTest(tag=tag):
                result = subprocess.run(
                    ["git", "-c", f"safe.directory={self.root.as_posix()}", "show", f"{tag}:scripts/clean-install-vault.sh"],
                    cwd=self.root,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_handoff_is_unprivileged_and_pins_candidate_identity(self) -> None:
        self.assertIn("--state-dir ABSOLUTE_PATH", self.payload)
        self.assertIn("--candidate-ref", self.payload)
        self.assertIn("--candidate-sha", self.payload)
        self.assertIn("refs/heads/dev) git check-ref-format", self.payload)
        self.assertIn("refs/tags/v*)", self.payload)
        self.assertIn("candidate SHA does not match the fetched ref", self.payload)
        self.assertIn('cat-file -t FETCH_HEAD', self.payload)
        self.assertIn('candidate release ref must resolve to an annotated immutable tag', self.payload)
        self.assertIn("create_clean_install_vault", self.payload)
        self.assertIn('require_exact_legacy_state_dir', self.payload)
        self.assertIn('expected="$HOME/gp/GP-access-control-plane/build/state"', self.payload)
        self.assertIn('[ "$STATE_DIR" = "$expected" ]', self.payload)
        self.assertIn('canonical legacy state directory is unavailable or unsafe', self.payload)
        self.assertIn("clean_install_vault_info", self.payload)
        self.assertLess(self.payload.index("created = create_clean_install_vault"), self.payload.index("info = clean_install_vault_info"))
        self.assertIn('if not info.get("exists") or not info.get("pending"):', self.payload)
        self.assertIn('if info.get("vault_id") != created.get("vault_id"):', self.payload)
        self.assertIn("materialize_candidate_runtime", self.payload)
        self.assertIn("validate_candidate_checkout", self.payload)
        self.assertIn("persisted candidate repository has unstaged changes", self.payload)
        self.assertIn("persisted candidate repository has staged changes", self.payload)
        self.assertIn("persisted candidate repository has local changes", self.payload)
        self.assertIn('git -C "$CANDIDATE_REPOSITORY" archive --format=tar "$CANDIDATE_SHA"', self.payload)
        self.assertIn('tar -xf "$runtime_archive" -C "$RUNTIME_STAGE"', self.payload)
        self.assertIn('"handoff": "ready"', self.payload)
        self.assertNotIn("confirmation_token", self.payload)
        public_result = self.payload[self.payload.index('print(json.dumps({'):]
        self.assertIn('"handoff_secret_sha256"', self.payload)
        self.assertNotIn('"cleaner_sha256":', public_result)
        self.assertNotIn('"preflight_sha256":', public_result)
        self.assertNotIn("handoff_secret", public_result)
        self.assertNotIn("SAFE-HANDOFF-001-KNOWN-SECRET", self.payload)
        self.assertNotIn("/usr/bin/sudo", self.payload)
        self.assertNotIn("systemctl", self.payload)
        self.assertNotIn("rm -rf /", self.payload)
        self.assertIn("never as root", self.launcher)
        self.assertNotIn("/usr/bin/sudo", self.launcher)

    def test_handoff_does_not_hardcode_a_release_version(self) -> None:
        self.assertNotIn("v0.4.0", self.payload)
        self.assertNotIn("v0.4.0", self.launcher)
        self.assertIn("tag_major=${tag_version%%.*}", self.payload)
        self.assertIn("tag_minor=${tag_rest%%.*}", self.payload)
        self.assertIn("case \"$tag_major:$tag_minor:$tag_patch\"", self.payload)

    @unittest.skipUnless(os.name == "posix", "hermetic shell execution requires a POSIX user boundary")
    def test_legacy_bootstrap_runs_hermetically_for_each_baseline_and_pins_candidate_sha(self) -> None:
        """Local-only shell evidence; the mandatory Pi clean-install gate remains separate hardware evidence."""
        shell = shutil.which("sh")
        real_git = shutil.which("git")
        self.assertIsNotNone(shell)
        self.assertIsNotNone(real_git)
        assert shell is not None and real_git is not None

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            candidate = root / "candidate"
            shutil.copytree(self.root / "src", candidate / "src")
            shutil.copytree(self.root / "scripts", candidate / "scripts")
            self._inject_candidate_handoff_fault_hook(candidate / "src" / "gp_control_plane" / "backups.py")
            self._git(real_git, candidate, "init", "-q")
            self._git(real_git, candidate, "config", "user.email", "safe-handoff@test.invalid")
            self._git(real_git, candidate, "config", "user.name", "SAFE-HANDOFF test")
            self._git(real_git, candidate, "add", "src", "scripts")
            self._git(real_git, candidate, "commit", "-qm", "candidate")
            self._git(real_git, candidate, "branch", "-M", "dev")
            candidate_sha = self._git(real_git, candidate, "rev-parse", "HEAD").stdout.strip()
            remote = root / "candidate-remote.git"
            self._git(real_git, root, "clone", "--bare", str(candidate), str(remote))

            tools = root / "safe-tools"
            tools.mkdir()
            self._write_safe_tool(tools / "git", """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_GIT_LOG"
case \"$*\" in *https://github.com/*) exit 97;; esac
exec \"$REAL_GIT\" \"$@\"
""")
            self._write_safe_tool(tools / "python3", """#!/bin/sh
exec \"$PYTHON_EXECUTABLE\" \"$@\"
""")
            self._write_safe_tool(tools / "id", """#!/bin/sh
if [ \"${1:-}\" = \"-u\" ]; then printf '%s\\n' 1000; exit 0; fi
exec /usr/bin/id \"$@\"
""")
            scripts = root / "scripts"
            scripts.mkdir()
            payload = self.payload.replace(
                "readonly CANONICAL_UPSTREAM='https://github.com/balbomush/GP-access-control-plane.git'",
                f"readonly CANONICAL_UPSTREAM='{remote}'",
            ).replace("PATH=/usr/bin:/bin", f"PATH={tools}:/usr/bin:/bin")
            (scripts / "legacy-bootstrap.sh").write_text(payload, encoding="utf-8")
            (scripts / "legacy-bootstrap-launcher.sh").write_text(self.launcher, encoding="utf-8")

            for baseline_tag in ("v0.3.4", "v0.3.5-alpha.4"):
                with self.subTest(baseline_tag=baseline_tag):
                    home = root / f"home-{baseline_tag}"
                    home.mkdir()
                    state = home / "gp" / "GP-access-control-plane" / "build" / "state"
                    self._seed_state_from_baseline(real_git, baseline_tag, state, root)
                    environment = os.environ | {
                        "HOME": str(home),
                        "REAL_GIT": real_git,
                        "PYTHON_EXECUTABLE": sys.executable,
                        "FAKE_GIT_LOG": str(root / f"git-{baseline_tag}.log"),
                    }
                    Path(environment["FAKE_GIT_LOG"]).write_text("", encoding="utf-8")
                    command = [
                        shell,
                        str(scripts / "legacy-bootstrap-launcher.sh"),
                        "--state-dir",
                        str(state),
                        "--candidate-ref",
                        "refs/heads/dev",
                        "--candidate-sha",
                        candidate_sha,
                    ]
                    success = subprocess.run(command, cwd=root, env=environment, check=False, capture_output=True, text=True)
                    self.assertEqual(success.returncode, 0, success.stderr)
                    ready = json.loads(success.stdout)
                    self.assertEqual(ready["handoff"], "ready")
                    self.assertEqual(ready["candidate_ref"], "refs/heads/dev")
                    self.assertEqual(ready["candidate_sha"], candidate_sha)
                    git_log = Path(environment["FAKE_GIT_LOG"]).read_text(encoding="utf-8")
                    self.assertIn("fetch --no-tags --depth=1 origin refs/heads/dev", git_log)
                    self.assertIn(f"archive --format=tar {candidate_sha}", git_log)
                    handoff = home / ".local" / "share" / "gp-control-plane" / "clean-install-handoff" / "handoff.json"
                    self.assertEqual(handoff.parent, home / ".local" / "share" / "gp-control-plane" / "clean-install-handoff")
                    self.assertTrue(handoff.is_file())
                    self.assertFalse(handoff.is_symlink())
                    self.assertTrue(handoff.parent.is_dir())
                    self.assertFalse(handoff.parent.is_symlink())
                    self.assertEqual(handoff.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(handoff.parent.stat().st_mode & 0o777, 0o700)
                    handoff_secret = str(json.loads(handoff.read_text(encoding="utf-8"))["handoff_secret"])
                    self.assertTrue(handoff_secret)
                    self.assertNotIn(handoff_secret, success.stdout)
                    self.assertNotIn(handoff_secret, success.stderr)
                    self.assertNotIn("confirmation_token", success.stdout + success.stderr)
                    self.assertNotIn("handoff_secret", success.stdout + success.stderr)
                    self.assertNotIn("SAFE-HANDOFF-001-KNOWN-SECRET", success.stdout + success.stderr)

                    # A persisted candidate cache is only an immutable object
                    # store.  Any local edit must stop before vault export,
                    # handoff publication, or materializing a runtime stage.
                    for dirty_kind, expected_error in (
                        ("unstaged", "persisted candidate repository has unstaged changes"),
                        ("staged", "persisted candidate repository has staged changes"),
                        ("untracked", "persisted candidate repository has local changes"),
                    ):
                        with self.subTest(baseline_tag=baseline_tag, dirty_kind=dirty_kind):
                            dirty_home = root / f"home-{baseline_tag}-dirty-{dirty_kind}"
                            dirty_home.mkdir()
                            dirty_state = dirty_home / "gp" / "GP-access-control-plane" / "build" / "state"
                            self._seed_state_from_baseline(real_git, baseline_tag, dirty_state, root)
                            dirty_cache = (
                                dirty_home
                                / ".cache"
                                / "gp-control-plane"
                                / "clean-handoff"
                                / f"candidate-{candidate_sha}"
                            )
                            dirty_cache.parent.mkdir(parents=True)
                            shutil.copytree(candidate, dirty_cache)
                            self._git(real_git, dirty_cache, "checkout", "--detach", candidate_sha)
                            dirty_source = dirty_cache / "scripts" / "gp-clean-remove-root.sh"
                            if dirty_kind == "untracked":
                                (dirty_cache / "unexpected-local-file").write_text("dirty\n", encoding="utf-8")
                            else:
                                dirty_source.write_text(
                                    dirty_source.read_text(encoding="utf-8") + "\n# dirty-cache-test\n",
                                    encoding="utf-8",
                                )
                                if dirty_kind == "staged":
                                    self._git(real_git, dirty_cache, "add", str(dirty_source.relative_to(dirty_cache)))

                            Path(environment["FAKE_GIT_LOG"]).write_text("", encoding="utf-8")
                            dirty_result = subprocess.run(
                                command,
                                cwd=root,
                                env=environment | {"HOME": str(dirty_home)},
                                check=False,
                                capture_output=True,
                                text=True,
                            )
                            self.assertEqual(dirty_result.returncode, 64)
                            self.assertIn(expected_error, dirty_result.stderr)
                            self.assertEqual(dirty_result.stdout, "")
                            self.assertNotIn('"handoff":"ready"', dirty_result.stdout)
                            self.assertFalse(
                                (
                                    dirty_home
                                    / ".local"
                                    / "share"
                                    / "gp-control-plane"
                                    / "clean-install-vault"
                                ).exists()
                            )
                            self.assertFalse(
                                (
                                    dirty_home
                                    / ".local"
                                    / "share"
                                    / "gp-control-plane"
                                    / "clean-install-handoff"
                                ).exists()
                            )
                            self.assertNotIn(
                                "archive --format=tar",
                                Path(environment["FAKE_GIT_LOG"]).read_text(encoding="utf-8"),
                            )

                    rejected_home = root / f"home-{baseline_tag}-wrong-sha"
                    rejected_home.mkdir()
                    rejected_state = rejected_home / "gp" / "GP-access-control-plane" / "build" / "state"
                    self._seed_state_from_baseline(real_git, baseline_tag, rejected_state, root)
                    wrong_sha = subprocess.run(
                        [
                            shell,
                            str(scripts / "legacy-bootstrap-launcher.sh"),
                            "--state-dir",
                            str(rejected_state),
                            "--candidate-ref",
                            "refs/heads/dev",
                            "--candidate-sha",
                            "0" * 40,
                        ],
                        cwd=root,
                        env=environment | {"HOME": str(rejected_home)},
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(wrong_sha.returncode, 64)
                    self.assertIn("candidate SHA does not match the fetched ref", wrong_sha.stderr)
                    self.assertEqual(wrong_sha.stdout, "")
                    self.assertFalse((rejected_home / ".local" / "share" / "gp-control-plane" / "clean-install-vault").exists())
                    self.assertFalse((rejected_home / ".local" / "share" / "gp-control-plane" / "clean-install-handoff").exists())
                    self.assertFalse((rejected_state.parent / "backups").exists())

                    rejected = subprocess.run(
                        [*command[:-1], "0" * 40], cwd=root, env=environment, check=False, capture_output=True, text=True
                    )
                    self.assertEqual(rejected.returncode, 64)
                    self.assertIn("candidate SHA does not match the fetched ref", rejected.stderr)
                    self.assertEqual(rejected.stdout, "")
                    self.assertNotIn(handoff_secret, rejected.stdout + rejected.stderr)
                    self.assertNotIn("confirmation_token", rejected.stderr)
                    self.assertNotIn("handoff_secret", rejected.stderr)

                    for invalid_ref in ("refs/tags/v1.2.3-alpha", "refs/tags/v1.2.x"):
                        with self.subTest(baseline_tag=baseline_tag, invalid_ref=invalid_ref):
                            Path(environment["FAKE_GIT_LOG"]).write_text("", encoding="utf-8")
                            invalid_command = [*command]
                            invalid_command[5] = invalid_ref
                            invalid = subprocess.run(
                                invalid_command, cwd=root, env=environment, check=False, capture_output=True, text=True
                            )
                            self.assertEqual(invalid.returncode, 64)
                            self.assertIn("candidate ref must be", invalid.stderr)
                            self.assertNotIn('"handoff":"ready"', invalid.stdout)
                            self.assertNotIn("fetch", Path(environment["FAKE_GIT_LOG"]).read_text(encoding="utf-8"))

                    for fault in ("missing", "mutated", "vault_id", "archive"):
                        with self.subTest(baseline_tag=baseline_tag, fault=fault):
                            fault_home = root / f"home-{baseline_tag}-{fault}"
                            fault_home.mkdir()
                            fault_state = fault_home / "gp" / "GP-access-control-plane" / "build" / "state"
                            self._seed_state_from_baseline(real_git, baseline_tag, fault_state, root)
                            fault_result = subprocess.run(
                                [
                                    shell,
                                    str(scripts / "legacy-bootstrap-launcher.sh"),
                                    "--state-dir",
                                    str(fault_state),
                                    "--candidate-ref",
                                    "refs/heads/dev",
                                    "--candidate-sha",
                                    candidate_sha,
                                ],
                                cwd=root,
                                env=environment | {"HOME": str(fault_home), "GP_TEST_HANDOFF_FAULT": fault},
                                check=False,
                                capture_output=True,
                                text=True,
                            )
                            self.assertNotEqual(fault_result.returncode, 0)
                            expected_error = "archive" if fault == "archive" else "handoff"
                            self.assertIn(expected_error, fault_result.stderr)
                            self.assertNotIn('"handoff":"ready"', fault_result.stdout)
                            self.assertNotIn("confirmation_token", fault_result.stdout + fault_result.stderr)
                            self.assertNotIn("handoff_secret", fault_result.stdout + fault_result.stderr)

    @unittest.skipUnless(os.name == "posix", "legacy source-path rejection needs a POSIX user boundary")
    def test_noncanonical_or_nonexact_state_dir_stops_before_cache_or_vault(self) -> None:
        shell = shutil.which("sh")
        self.assertIsNotNone(shell)
        assert shell is not None
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "home"
            home.mkdir()
            expected = home / "gp" / "GP-access-control-plane" / "build" / "state"
            other = root / "other-state"
            other.mkdir()
            link = root / "state-link"
            link.symlink_to(other, target_is_directory=True)
            payload = root / "legacy-bootstrap.sh"
            payload.write_text(self.payload, encoding="utf-8")
            payload.chmod(0o700)
            for supplied in (other, link, expected):
                with self.subTest(supplied=supplied):
                    result = subprocess.run(
                        [shell, str(payload), "--state-dir", str(supplied), "--candidate-ref", "refs/heads/dev", "--candidate-sha", "a" * 40],
                        cwd=root,
                        env=os.environ | {"HOME": str(home)},
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 64)
                    self.assertIn("state dir", result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertFalse((home / ".cache" / "gp-control-plane" / "clean-handoff").exists())
                    self.assertFalse((home / ".local" / "share" / "gp-control-plane" / "clean-install-vault").exists())
                    self.assertFalse((home / ".local" / "share" / "gp-control-plane" / "clean-install-handoff").exists())

    def _git(self, git: str, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([git, *args], cwd=cwd, check=True, capture_output=True, text=True)

    def _write_safe_tool(self, path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
        path.chmod(0o700)

    def _inject_candidate_handoff_fault_hook(self, path: Path) -> None:
        source = path.read_text(encoding="utf-8")
        expected = """        created = _create_clean_install_vault_locked(state_dir, target_home=target_home)
        validate_clean_install_handoff(vault_id=str(created[\"vault_id\"]), target_home=target_home)
        return created
"""
        replacement = """        created = _create_clean_install_vault_locked(state_dir, target_home=target_home)
        fault = os.environ.get(\"GP_TEST_HANDOFF_FAULT\")
        if fault == \"missing\":
            clean_install_handoff_path(target_home).unlink()
        elif fault == \"mutated\":
            path = clean_install_handoff_path(target_home)
            payload = json.loads(path.read_text(encoding=\"utf-8\"))
            payload[\"handoff_secret\"] = \"mutated-by-hermetic-test\"
            path.write_text(json.dumps(payload), encoding=\"utf-8\")
        elif fault == \"vault_id\":
            path = clean_install_handoff_path(target_home)
            payload = json.loads(path.read_text(encoding=\"utf-8\"))
            payload[\"vault_id\"] = \"0\" * 32
            path.write_text(json.dumps(payload), encoding=\"utf-8\")
        elif fault == \"archive\":
            with (clean_install_vault_dir(target_home) / \"archive.zip\").open(\"ab\") as handle:
                handle.write(b\"tampered-by-hermetic-test\")
        validate_clean_install_handoff(vault_id=str(created[\"vault_id\"]), target_home=target_home)
        return created
"""
        self.assertIn(expected, source)
        path.write_text(source.replace(expected, replacement), encoding="utf-8")

    def _seed_state_from_baseline(self, git: str, tag: str, state: Path, root: Path) -> None:
        archive = subprocess.run(
            [git, "-c", f"safe.directory={self.root.as_posix()}", "archive", tag, "src"],
            cwd=self.root,
            check=True,
            capture_output=True,
        ).stdout
        baseline = root / f"baseline-{tag}"
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
            bundle.extractall(baseline, filter="data")
        subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; from gp_control_plane.storage import connect; connect(Path(__import__('sys').argv[1])).close()",
                str(state),
            ],
            check=True,
            capture_output=True,
            text=True,
            env=os.environ | {"PYTHONPATH": str(baseline / "src")},
        )

    def test_shell_syntax_is_valid(self) -> None:
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash is required for shell syntax validation")
        for path in (self.root / "scripts" / "legacy-bootstrap.sh", self.root / "scripts" / "legacy-bootstrap-launcher.sh"):
            with self.subTest(path=path.name):
                result = subprocess.run([bash, "-n", str(path)], check=False, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
