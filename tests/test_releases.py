from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.release_update import queue_release_update, release_update_plan, release_update_status
from gp_control_plane.releases import parse_git_tags, parse_github_releases, release_channel_info
from gp_control_plane.state import read_state, update_state, write_state


class ReleaseTests(unittest.TestCase):
    def test_release_channel_info_selects_stable_release(self) -> None:
        payload = """
[
  {"tag_name": "v0.4.0-beta.1", "name": "beta", "prerelease": true, "draft": false, "html_url": "https://example.test/beta", "published_at": "2026-01-02T00:00:00Z"},
  {"tag_name": "v0.3.1", "name": "stable", "prerelease": false, "draft": false, "html_url": "https://example.test/stable", "published_at": "2026-01-01T00:00:00Z", "body": "changes", "assets": [{"name": "pkg.zip", "browser_download_url": "https://example.test/pkg.zip", "size": 10}]}
]
"""

        info = release_channel_info(current_version="0.3.0", channel="stable", fetcher=lambda: payload)

        self.assertTrue(info["checked"])
        self.assertEqual(info["available_version"], "v0.3.1")
        self.assertTrue(info["update_available"])
        self.assertEqual(info["body"], "changes")
        self.assertEqual(info["assets"][0]["name"], "pkg.zip")

    def test_release_channel_info_selects_prerelease(self) -> None:
        payload = """
[
  {"tag_name": "v0.4.0-beta.1", "name": "beta", "prerelease": true, "draft": false, "html_url": "https://example.test/beta", "published_at": "2026-01-02T00:00:00Z"},
  {"tag_name": "v0.3.1", "name": "stable", "prerelease": false, "draft": false, "html_url": "https://example.test/stable", "published_at": "2026-01-01T00:00:00Z"}
]
"""

        info = release_channel_info(current_version="0.3.1", channel="prerelease", fetcher=lambda: payload)

        self.assertTrue(info["checked"])
        self.assertEqual(info["available_version"], "v0.4.0-beta.1")
        self.assertEqual(info["url"], "https://example.test/beta")

    def test_release_channel_info_falls_back_to_git_tags(self) -> None:
        tags = """
aaaa refs/tags/v0.2.0
bbbb refs/tags/v0.3.1
cccc refs/tags/v0.4.0-alpha.1
"""

        info = release_channel_info(
            current_version="0.3.0",
            channel="stable",
            fetcher=lambda: (_ for _ in ()).throw(RuntimeError("rate limited")),
            tag_fetcher=lambda: tags,
        )

        self.assertTrue(info["checked"])
        self.assertEqual(info["source"], "git-tags")
        self.assertEqual(info["available_version"], "v0.3.1")
        self.assertTrue(info["update_available"])
        self.assertIn("rate limited", info["error"])

    def test_release_channel_info_falls_back_to_prerelease_tag(self) -> None:
        tags = """
aaaa refs/tags/v0.2.0
bbbb refs/tags/v0.3.1
cccc refs/tags/v0.4.0-alpha.1
dddd refs/tags/v0.4.0-alpha.2
"""

        info = release_channel_info(
            current_version="0.3.1",
            channel="prerelease",
            fetcher=lambda: (_ for _ in ()).throw(RuntimeError("rate limited")),
            tag_fetcher=lambda: tags,
        )

        self.assertTrue(info["checked"])
        self.assertEqual(info["source"], "git-tags")
        self.assertEqual(info["available_version"], "v0.4.0-alpha.2")
        self.assertTrue(info["update_available"])

    def test_release_channel_info_reports_structured_errors(self) -> None:
        def broken_tags() -> str:
            raise subprocess.TimeoutExpired("git ls-remote", 1)

        info = release_channel_info(
            current_version="0.3.0",
            channel="stable",
            fetcher=lambda: "{broken-json",
            tag_fetcher=broken_tags,
        )

        self.assertFalse(info["checked"])
        self.assertEqual(info["error_kind"], "format")
        self.assertEqual(info["error_stage"], "github_api")
        self.assertEqual([item["stage"] for item in info["errors"]], ["github_api", "git_tags"])
        self.assertEqual([item["kind"] for item in info["errors"]], ["format", "git"])

    def test_release_channel_info_updates_between_alpha_tags(self) -> None:
        tags = """
aaaa refs/tags/v0.3.2-alpha.1
bbbb refs/tags/v0.3.2-alpha.2
"""

        info = release_channel_info(
            current_version="0.3.2-alpha.1",
            channel="prerelease",
            fetcher=lambda: (_ for _ in ()).throw(RuntimeError("rate limited")),
            tag_fetcher=lambda: tags,
        )

        self.assertEqual(info["available_version"], "v0.3.2-alpha.2")
        self.assertTrue(info["update_available"])

    def test_parse_github_releases_skips_drafts(self) -> None:
        payload = """
[
  {"tag_name": "v0.3.1", "draft": false},
  {"tag_name": "v0.3.2", "draft": true}
]
"""

        self.assertEqual([item["tag_name"] for item in parse_github_releases(payload)], ["v0.3.1"])

    def test_parse_git_tags_reads_tag_names(self) -> None:
        payload = """
111 refs/heads/main
222 refs/tags/v0.3.0
333 refs/tags/v0.3.1
"""

        self.assertEqual(parse_git_tags(payload), ["v0.3.0", "v0.3.1"])

    def test_release_update_plan_blocks_active_job(self) -> None:
        payload = """
[
  {"tag_name": "v0.3.1", "name": "stable", "prerelease": false, "draft": false, "html_url": "https://example.test/stable", "published_at": "2026-01-01T00:00:00Z"}
]
"""
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            write_state(state_dir, {"current_run_id": "job-1"})

            def unexpected_fetch() -> str:
                raise AssertionError("release lookup must not run while job is active")

            plan = release_update_plan(state_dir, channel="stable", current_version="0.3.0", fetcher=unexpected_fetch)

            self.assertFalse(plan["can_update"])
            self.assertEqual(plan["blocked_reason"], "job is running")

    def test_release_update_plan_blocks_runtime_lock_before_release_lookup(self) -> None:
        payload = """
[
  {"tag_name": "v0.3.1", "name": "stable", "prerelease": false, "draft": false, "html_url": "https://example.test/stable", "published_at": "2026-01-01T00:00:00Z"}
]
"""
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            lock_path = state_dir / "job-runner.lock"
            lock_path.write_text(json.dumps({"pid": os.getpid(), "run_id": "lock-only"}), encoding="utf-8")

            def unexpected_fetch() -> str:
                raise AssertionError("release lookup must not run while runtime lock is active")

            plan = release_update_plan(state_dir, channel="stable", current_version="0.3.0", fetcher=unexpected_fetch)

            self.assertFalse(plan["can_update"])
            self.assertEqual(plan["blocked_reason"], "job is running")
            self.assertEqual(plan["active_run_id"], "lock-only")

            lock_path.write_text(json.dumps({"pid": 99999999, "run_id": "stale"}), encoding="utf-8")
            plan = release_update_plan(state_dir, channel="stable", current_version="0.3.0", fetcher=lambda: payload)

            self.assertTrue(plan["can_update"])
            self.assertIn("root-owned install profile", plan["steps"][2])
            self.assertFalse(lock_path.exists())

    def test_release_update_plan_blocks_active_update(self) -> None:
        payload = """
[
  {"tag_name": "v0.3.1", "name": "stable", "prerelease": false, "draft": false, "html_url": "https://example.test/stable", "published_at": "2026-01-01T00:00:00Z"}
]
"""
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            write_state(
                state_dir,
                {
                    "release_update": {
                        "status": "queued",
                        "queued_at": "2999-01-01T00:00:00Z",
                        "target_ref": "v0.3.1",
                    }
                },
            )

            plan = release_update_plan(state_dir, channel="stable", current_version="0.3.0", fetcher=lambda: payload)

            self.assertFalse(plan["can_update"])
            self.assertEqual(plan["blocked_reason"], "release update is already queued")

    def test_release_update_plan_expires_stale_update_before_planning(self) -> None:
        payload = """
[
  {"tag_name": "v0.3.1", "name": "stable", "prerelease": false, "draft": false, "html_url": "https://example.test/stable", "published_at": "2026-01-01T00:00:00Z"}
]
"""
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            write_state(
                state_dir,
                {
                    "release_update": {
                        "status": "queued",
                        "queued_at": "2026-01-01T00:00:00Z",
                        "target_ref": "v0.3.1",
                    }
                },
            )

            plan = release_update_plan(state_dir, channel="stable", current_version="0.3.0", fetcher=lambda: payload)
            state = read_state(state_dir)

            self.assertTrue(plan["can_update"])
            self.assertIn("root-owned install profile", plan["steps"][2])
            self.assertEqual(state["release_update"]["status"], "failed")
            self.assertEqual(state["release_update"]["error"], "release update queue timeout")

    _TAG = "v0.3.1"
    _CANDIDATE_REF = "refs/tags/v0.3.1"
    _EXPECTED_SHA = "a" * 40
    _OTHER_SHA = "b" * 40

    def _release_payload(self) -> str:
        return """
[
  {"tag_name": "v0.3.1", "name": "stable", "prerelease": false, "draft": false, "html_url": "https://example.test/stable", "published_at": "2026-01-01T00:00:00Z"}
]
"""

    def _candidate_refs(self) -> str:
        return f"{'c' * 40} {self._CANDIDATE_REF}\n{self._EXPECTED_SHA} {self._CANDIDATE_REF}^{{}}\n"

    def _queued_helper_output(self) -> str:
        return "\n".join(
            [
                "queued=true",
                "status=queued",
                "unit=gp-control-plane-update.service",
                "log=/tmp/update.log",
                f"candidate_ref={self._CANDIDATE_REF}",
                f"expected_sha={self._EXPECTED_SHA}",
                "phase=queued",
            ]
        )

    def _queue_output_values(self) -> dict[str, str]:
        return {
            "queued": "true",
            "status": "queued",
            "phase": "queued",
            "unit": "gp-control-plane-update.service",
            "log": "/tmp/update.log",
            "candidate_ref": self._CANDIDATE_REF,
            "expected_sha": self._EXPECTED_SHA,
        }

    def _success_log_values(self) -> dict[str, str]:
        return {
            "phase": "installed",
            "status": "success",
            "verified_ref": self._CANDIDATE_REF,
            "verified_sha": self._EXPECTED_SHA,
            "checked_out_sha": self._EXPECTED_SHA,
            "installed_ref": self._CANDIDATE_REF,
            "installed_sha": self._EXPECTED_SHA,
            "installed_version": self._TAG.removeprefix("v"),
        }

    def _strict_update_payload(self, log_path: Path, *, status: str = "queued") -> dict[str, object]:
        return {
            "status": status,
            "target_ref": self._TAG,
            "candidate_ref": self._CANDIDATE_REF,
            "expected_sha": self._EXPECTED_SHA,
            "log_path": str(log_path),
            "release": {"available_version": self._TAG},
        }

    def test_release_update_plan_pins_annotated_tag_to_peeled_commit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            plan = release_update_plan(
                Path(raw) / "state",
                channel="stable",
                current_version="0.3.0",
                fetcher=self._release_payload,
                candidate_fetcher=self._candidate_refs,
            )

        self.assertTrue(plan["can_update"])
        self.assertEqual(plan["candidate"], {"candidate_ref": self._CANDIDATE_REF, "expected_sha": self._EXPECTED_SHA})

    def test_queue_release_update_uses_strict_helper_argv_and_persists_candidate(self) -> None:
        calls: list[list[str]] = []

        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            install_dir = Path(raw) / "repo"
            install_dir.mkdir()

            def fake_helper(args: list[str]) -> subprocess.CompletedProcess[str]:
                calls.append(args)
                queued = read_state(state_dir)["release_update"]
                self.assertEqual(queued["status"], "queueing")
                self.assertEqual(queued["candidate_ref"], self._CANDIDATE_REF)
                self.assertEqual(queued["expected_sha"], self._EXPECTED_SHA)
                return subprocess.CompletedProcess(args, 0, self._queued_helper_output(), "")

            result = queue_release_update(
                state_dir,
                channel="stable",
                current_version="0.3.0",
                fetcher=self._release_payload,
                candidate_fetcher=self._candidate_refs,
                install_dir=install_dir,
                helper_runner=fake_helper,
            )

        self.assertEqual(
            calls,
            [[
                "queue-update",
                "--candidate-ref",
                self._CANDIDATE_REF,
                "--expected-sha",
                self._EXPECTED_SHA,
            ]],
        )
        self.assertTrue(result["queued"])
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["phase"], "queued")
        self.assertEqual(result["candidate_ref"], self._CANDIDATE_REF)
        self.assertEqual(result["expected_sha"], self._EXPECTED_SHA)
        self.assertEqual(result["target_ref"], self._TAG)

    def test_queue_release_update_rejects_each_missing_required_helper_field(self) -> None:
        for missing_field in self._queue_output_values():
            with self.subTest(missing_field=missing_field), tempfile.TemporaryDirectory() as raw:
                values = self._queue_output_values()
                del values[missing_field]
                state_dir = Path(raw) / "state"
                with self.assertRaisesRegex(RuntimeError, "strict root-helper output"):
                    queue_release_update(
                        state_dir,
                        channel="stable",
                        current_version="0.3.0",
                        fetcher=self._release_payload,
                        candidate_fetcher=self._candidate_refs,
                        helper_runner=lambda args: subprocess.CompletedProcess(args, 0, "\n".join(f"{key}={value}" for key, value in values.items()), ""),
                    )

                failed = read_state(state_dir)["release_update"]
                self.assertEqual(failed["status"], "failed")
                self.assertEqual(failed["phase"], "queue_failed")

    def test_queue_release_update_rejects_each_substituted_pinned_helper_field(self) -> None:
        replacements = {
            "queued": "false",
            "status": "running",
            "phase": "queueing",
            "unit": "",
            "log": "",
            "candidate_ref": "refs/tags/v9.9.9",
            "expected_sha": self._OTHER_SHA,
        }
        for field, replacement in replacements.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as raw:
                values = self._queue_output_values()
                values[field] = replacement
                state_dir = Path(raw) / "state"
                with self.assertRaisesRegex(RuntimeError, "strict root-helper output"):
                    queue_release_update(
                        state_dir,
                        channel="stable",
                        current_version="0.3.0",
                        fetcher=self._release_payload,
                        candidate_fetcher=self._candidate_refs,
                        helper_runner=lambda args: subprocess.CompletedProcess(args, 0, "\n".join(f"{key}={value}" for key, value in values.items()), ""),
                    )

    def test_queue_release_update_accepts_no_install_dir_but_never_passes_paths(self) -> None:
        calls: list[list[str]] = []
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            result = queue_release_update(
                state_dir,
                channel="stable",
                current_version="0.3.0",
                fetcher=self._release_payload,
                candidate_fetcher=self._candidate_refs,
                helper_runner=lambda args: calls.append(args) or subprocess.CompletedProcess(args, 0, self._queued_helper_output(), ""),
            )

        self.assertEqual(result["status"], "queued")
        self.assertEqual(calls[0], ["queue-update", "--candidate-ref", self._CANDIDATE_REF, "--expected-sha", self._EXPECTED_SHA])

    def test_queue_release_update_deduplicates_active_update(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            write_state(
                state_dir,
                {"release_update": {"queued": True, "status": "queued", "queued_at": "2999-01-01T00:00:00Z", "target_ref": self._TAG}},
            )
            result = queue_release_update(
                state_dir,
                channel="stable",
                current_version="0.3.0",
                fetcher=lambda: "[]",
                helper_runner=lambda args: (_ for _ in ()).throw(AssertionError("helper must not run")),
            )

        self.assertTrue(result["deduplicated"])
        self.assertEqual(result["target_ref"], self._TAG)

    def test_queue_release_update_keeps_release_channel_fallback(self) -> None:
        tags = "\n".join([f"{'d' * 40} refs/tags/v0.3.0", f"{'e' * 40} {self._CANDIDATE_REF}"])
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            install_dir = Path(raw) / "repo"
            install_dir.mkdir()
            result = queue_release_update(
                state_dir,
                channel="stable",
                current_version="0.3.0",
                fetcher=lambda: (_ for _ in ()).throw(RuntimeError("rate limited")),
                tag_fetcher=lambda: tags,
                candidate_fetcher=self._candidate_refs,
                install_dir=install_dir,
                helper_runner=lambda args: subprocess.CompletedProcess(args, 0, self._queued_helper_output(), ""),
            )

        self.assertEqual(result["release"]["source"], "git-tags")
        self.assertEqual(result["candidate_ref"], self._CANDIDATE_REF)

    def test_queue_release_update_preserves_concurrent_state_fields(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            install_dir = Path(raw) / "repo"
            install_dir.mkdir()

            def fake_helper(args: list[str]) -> subprocess.CompletedProcess[str]:
                update_state(state_dir, lambda state: state | {"run_preferences": {"domains": ["youtube.com"]}})
                return subprocess.CompletedProcess(args, 0, self._queued_helper_output(), "")

            result = queue_release_update(
                state_dir,
                channel="stable",
                current_version="0.3.0",
                fetcher=self._release_payload,
                candidate_fetcher=self._candidate_refs,
                install_dir=install_dir,
                helper_runner=fake_helper,
            )
            state = read_state(state_dir)

        self.assertEqual(result["status"], "queued")
        self.assertEqual(state["release_update"]["status"], "queued")
        self.assertEqual(state["run_preferences"], {"domains": ["youtube.com"]})

    def test_release_update_status_requires_full_matching_install_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            log_path = Path(raw) / "update.log"
            log_path.write_text(
                "\n".join(f"{key}={value}" for key, value in self._success_log_values().items()),
                encoding="utf-8",
            )
            write_state(state_dir, {"release_update": self._strict_update_payload(log_path)})

            status = release_update_status(state_dir, current_version="0.0.0")
            persisted = read_state(state_dir)["release_update"]

        self.assertEqual(status["status"], "success")
        self.assertTrue(status["verified"])
        self.assertEqual(status["installed_ref"], self._CANDIDATE_REF)
        self.assertEqual(status["installed_version"], "0.3.1")
        self.assertEqual(persisted["installed_sha"], self._EXPECTED_SHA)
        self.assertEqual(persisted["phase"], "installed")
        self.assertEqual(status["cleanup_status"], "completed")
        self.assertEqual(persisted["cleanup_status"], "completed")

    def test_release_update_status_preserves_valid_cleanup_evidence(self) -> None:
        for cleanup_status, cleanup_path in (
            ("completed", ""),
            ("deferred", "/srv/gp/.GP-access-control-plane.strict-previous"),
            ("failed", ""),
        ):
            with self.subTest(cleanup_status=cleanup_status), tempfile.TemporaryDirectory() as raw:
                state_dir = Path(raw) / "state"
                values = self._success_log_values()
                success_status = values.pop("status")
                values["cleanup_status"] = cleanup_status
                if cleanup_path:
                    values["cleanup_path"] = cleanup_path
                values["status"] = success_status
                log_path = Path(raw) / "update.log"
                log_path.write_text("\n".join(f"{key}={value}" for key, value in values.items()), encoding="utf-8")
                write_state(state_dir, {"release_update": self._strict_update_payload(log_path)})

                status = release_update_status(state_dir)
                persisted = read_state(state_dir)["release_update"]

                self.assertEqual(status["status"], "success")
                self.assertTrue(status["verified"])
                self.assertEqual(status["cleanup_status"], cleanup_status)
                self.assertEqual(persisted["cleanup_status"], cleanup_status)
                self.assertEqual(status.get("cleanup_path", ""), cleanup_path)
                self.assertEqual(persisted.get("cleanup_path", ""), cleanup_path)

    def test_release_update_status_rejects_invalid_cleanup_evidence(self) -> None:
        cases = (
            ({"cleanup_status": "unknown"}, "cleanup_status"),
            ({"cleanup_status": ""}, "cleanup_status"),
            ({"cleanup_path": "/srv/gp/.strict-previous"}, "without cleanup_status"),
            ({"cleanup_status": "completed", "cleanup_path": "/srv/gp/.strict-previous"}, "completed cleanup"),
            ({"cleanup_status": "failed", "cleanup_path": "/srv/gp/.strict-previous"}, "failed cleanup"),
            ({"cleanup_status": "deferred"}, "omitted cleanup_path"),
        )
        for evidence, expected_error in cases:
            with self.subTest(evidence=evidence), tempfile.TemporaryDirectory() as raw:
                state_dir = Path(raw) / "state"
                values = self._success_log_values() | evidence
                log_path = Path(raw) / "update.log"
                log_path.write_text("\n".join(f"{key}={value}" for key, value in values.items()), encoding="utf-8")
                write_state(state_dir, {"release_update": self._strict_update_payload(log_path)})

                status = release_update_status(state_dir)

            self.assertEqual(status["status"], "failed")
            self.assertFalse(status["verified"])
            self.assertIn(expected_error, status["error"])

    def test_release_update_status_requires_terminal_success_for_new_cleanup_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            log_path = Path(raw) / "update.log"
            log_path.write_text(
                "\n".join(
                    (
                        "phase=installed",
                        f"verified_ref={self._CANDIDATE_REF}",
                        f"verified_sha={self._EXPECTED_SHA}",
                        f"checked_out_sha={self._EXPECTED_SHA}",
                        f"installed_ref={self._CANDIDATE_REF}",
                        f"installed_sha={self._EXPECTED_SHA}",
                        f"installed_version={self._TAG.removeprefix('v')}",
                        "cleanup_status=completed",
                        "status=success",
                        "unstructured trailing output",
                    )
                ),
                encoding="utf-8",
            )
            write_state(state_dir, {"release_update": self._strict_update_payload(log_path)})

            status = release_update_status(state_dir)

        self.assertEqual(status["status"], "failed")
        self.assertFalse(status["verified"])
        self.assertIn("after terminal success", status["error"])

    def test_release_update_status_rejects_each_missing_success_log_field(self) -> None:
        for missing_field in self._success_log_values():
            with self.subTest(missing_field=missing_field), tempfile.TemporaryDirectory() as raw:
                values = self._success_log_values()
                del values[missing_field]
                state_dir = Path(raw) / "state"
                log_path = Path(raw) / "update.log"
                log_path.write_text("\n".join(f"{key}={value}" for key, value in values.items()), encoding="utf-8")
                write_state(state_dir, {"release_update": self._strict_update_payload(log_path)})

                status = release_update_status(state_dir)

                self.assertEqual(status["status"], "failed")
                self.assertFalse(status["verified"])
                self.assertIn("strict root-helper output is missing", status["error"])

    def test_release_update_status_rejects_each_substituted_success_log_field(self) -> None:
        replacements = {
            "phase": "verified",
            "status": "failed",
            "verified_ref": "refs/tags/v9.9.9",
            "verified_sha": self._OTHER_SHA,
            "checked_out_sha": self._OTHER_SHA,
            "installed_ref": "refs/tags/v9.9.9",
            "installed_sha": self._OTHER_SHA,
            "installed_version": "9.9.9",
        }
        for field, replacement in replacements.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as raw:
                values = self._success_log_values()
                values[field] = replacement
                state_dir = Path(raw) / "state"
                log_path = Path(raw) / "update.log"
                log_path.write_text("\n".join(f"{key}={value}" for key, value in values.items()), encoding="utf-8")
                write_state(state_dir, {"release_update": self._strict_update_payload(log_path)})

                status = release_update_status(state_dir)

                self.assertEqual(status["status"], "failed")
                self.assertFalse(status["verified"])

    def test_release_update_status_persists_failure_phase_and_candidate_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            log_path = Path(raw) / "update.log"
            log_path.write_text(
                "\n".join(
                    [
                        "phase=checkout_failed",
                        f"candidate_ref={self._CANDIDATE_REF}",
                        f"expected_sha={self._OTHER_SHA}",
                        "error=git checkout failed",
                        "status=failed",
                    ]
                ),
                encoding="utf-8",
            )
            write_state(state_dir, {"release_update": self._strict_update_payload(log_path)})

            status = release_update_status(state_dir)
            persisted = read_state(state_dir)["release_update"]

        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["error"], "git checkout failed")
        self.assertEqual(status["phase"], "checkout_failed")
        self.assertEqual(persisted["candidate_ref"], self._CANDIDATE_REF)
        self.assertEqual(persisted["expected_sha"], self._EXPECTED_SHA)
        self.assertIn("phase=checkout_failed", status["log_tail"])

    def test_release_update_status_recovers_strict_completion_from_latest_log(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            log_dir = state_dir / "release-updates"
            log_dir.mkdir(parents=True)
            log_path = log_dir / "gp-control-plane-update-test.log"
            log_path.write_text(
                "\n".join(f"{key}={value}" for key, value in self._success_log_values().items()),
                encoding="utf-8",
            )
            write_state(state_dir, {"release_update": self._strict_update_payload(Path(""), status="queueing") | {"log_path": ""}})

            status = release_update_status(state_dir)

        self.assertEqual(status["status"], "success")
        self.assertTrue(status["verified"])
        self.assertEqual(status["log_path"], str(log_path))

    def test_release_update_status_marks_stale_update_failed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            write_state(
                state_dir,
                {"release_update": {"status": "queued", "queued_at": "2026-01-01T00:00:00Z", "target_ref": self._TAG}},
            )

            status = release_update_status(state_dir, current_version="0.3.0")
            state = read_state(state_dir)

        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["error"], "release update queue timeout")
        self.assertEqual(state["release_update"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
