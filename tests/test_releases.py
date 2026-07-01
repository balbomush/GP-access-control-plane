from __future__ import annotations

import unittest

from gp_control_plane.releases import parse_github_releases, release_channel_info


class ReleaseTests(unittest.TestCase):
    def test_release_channel_info_selects_stable_release(self) -> None:
        payload = """
[
  {"tag_name": "v0.4.0-beta.1", "name": "beta", "prerelease": true, "draft": false, "html_url": "https://example.test/beta", "published_at": "2026-01-02T00:00:00Z"},
  {"tag_name": "v0.3.1", "name": "stable", "prerelease": false, "draft": false, "html_url": "https://example.test/stable", "published_at": "2026-01-01T00:00:00Z"}
]
"""

        info = release_channel_info(current_version="0.3.0", channel="stable", fetcher=lambda: payload)

        self.assertTrue(info["checked"])
        self.assertEqual(info["available_version"], "v0.3.1")
        self.assertTrue(info["update_available"])

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


if __name__ == "__main__":
    unittest.main()
