from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.release_update import queue_release_update, release_update_plan, release_update_status
from gp_control_plane.releases import parse_git_tags, parse_github_releases, release_channel_info
from gp_control_plane.state import read_state, write_state


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
            write_state(state_dir, {"current_job": "job-1"})

            plan = release_update_plan(state_dir, channel="stable", current_version="0.3.0", fetcher=lambda: payload)

            self.assertFalse(plan["can_update"])
            self.assertEqual(plan["blocked_reason"], "job is running")

    def test_queue_release_update_creates_backup_and_calls_helper(self) -> None:
        payload = """
[
  {"tag_name": "v0.3.1", "name": "stable", "prerelease": false, "draft": false, "html_url": "https://example.test/stable", "published_at": "2026-01-01T00:00:00Z"}
]
"""
        calls: list[list[str]] = []

        def fake_helper(args: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "queued=true\nunit=test\nlog=/tmp/update.log\n", "")

        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            install_dir = Path(raw) / "repo"
            install_dir.mkdir()

            result = queue_release_update(
                state_dir,
                channel="stable",
                current_version="0.3.0",
                fetcher=lambda: payload,
                install_dir=install_dir,
                helper_runner=fake_helper,
            )

            self.assertTrue(result["queued"])
            self.assertEqual(calls[0], ["queue-update", str(install_dir.resolve()), "v0.3.1"])
            self.assertIn("snapshot", result)
            self.assertIn("queued=true", result["helper_stdout"])
            self.assertEqual(result["status"], "queued")
            self.assertEqual(result["target_ref"], "v0.3.1")
            self.assertIn("pre-update", result["rollback_instruction"])
            self.assertTrue(any("restore" in step for step in result["steps"]))

    def test_queue_release_update_can_use_git_tag_fallback(self) -> None:
        tags = """
aaaa refs/tags/v0.3.0
bbbb refs/tags/v0.3.1
"""
        calls: list[list[str]] = []

        def fake_helper(args: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "queued=true\nunit=test\nlog=/tmp/update.log\n", "")

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
                install_dir=install_dir,
                helper_runner=fake_helper,
            )

            self.assertEqual(result["release"]["source"], "git-tags")
            self.assertEqual(calls[0], ["queue-update", str(install_dir.resolve()), "v0.3.1"])

    def test_queue_release_update_prewrites_state_before_root_helper(self) -> None:
        payload = """
[
  {"tag_name": "v0.3.1", "name": "stable", "prerelease": false, "draft": false, "html_url": "https://example.test/stable", "published_at": "2026-01-01T00:00:00Z"}
]
"""

        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            install_dir = Path(raw) / "repo"
            install_dir.mkdir()

            def fake_helper(args: list[str]) -> subprocess.CompletedProcess[str]:
                state = read_state(state_dir)
                self.assertEqual(state["release_update"]["status"], "queueing")
                self.assertEqual(state["release_update"]["target_ref"], "v0.3.1")
                return subprocess.CompletedProcess(args, 0, "queued=true\nunit=test\nlog=/tmp/update.log\n", "")

            result = queue_release_update(
                state_dir,
                channel="stable",
                current_version="0.3.0",
                fetcher=lambda: payload,
                install_dir=install_dir,
                helper_runner=fake_helper,
            )

            self.assertEqual(result["status"], "queued")

    def test_release_update_status_reads_helper_log(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            log_path = Path(raw) / "update.log"
            log_path.write_text(
                "\n".join(
                    [
                        "gp-control-plane update queued",
                        "installed_ref=v0.3.1",
                        "installed_version=0.3.1",
                        "status=success",
                    ]
                ),
                encoding="utf-8",
            )
            write_state(
                state_dir,
                {
                    "release_update": {
                        "status": "queued",
                        "target_ref": "v0.3.1",
                        "log_path": str(log_path),
                        "release": {"available_version": "v0.3.1"},
                    }
                },
            )

            status = release_update_status(state_dir, current_version="0.3.1")

            self.assertEqual(status["status"], "success")
            self.assertTrue(status["verified"])
            self.assertEqual(status["installed_ref"], "v0.3.1")
            self.assertEqual(status["installed_version"], "0.3.1")
            self.assertIn("status=success", status["log_tail"])

    def test_release_update_status_recovers_after_service_restart_before_final_state_write(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            log_dir = state_dir / "release-updates"
            log_dir.mkdir(parents=True)
            log_path = log_dir / "gp-control-plane-update-test.log"
            log_path.write_text(
                "\n".join(
                    [
                        "installed_ref=v0.3.1",
                        "installed_version=0.3.1",
                        "status=success",
                    ]
                ),
                encoding="utf-8",
            )
            write_state(
                state_dir,
                {
                    "release_update": {
                        "status": "queueing",
                        "target_ref": "v0.3.1",
                        "release": {"available_version": "v0.3.1"},
                    }
                },
            )

            status = release_update_status(state_dir, current_version="0.3.1")

            self.assertEqual(status["status"], "success")
            self.assertTrue(status["verified"])
            self.assertEqual(status["log_path"], str(log_path))
            self.assertEqual(status["installed_ref"], "v0.3.1")


if __name__ == "__main__":
    unittest.main()
