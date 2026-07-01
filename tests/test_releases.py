from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from gp_control_plane.release_update import queue_release_update, release_update_plan
from gp_control_plane.releases import parse_github_releases, release_channel_info
from gp_control_plane.state import write_state


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

    def test_parse_github_releases_skips_drafts(self) -> None:
        payload = """
[
  {"tag_name": "v0.3.1", "draft": false},
  {"tag_name": "v0.3.2", "draft": true}
]
"""

        self.assertEqual([item["tag_name"] for item in parse_github_releases(payload)], ["v0.3.1"])

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


if __name__ == "__main__":
    unittest.main()
