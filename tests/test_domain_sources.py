from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.domain_sources import (
    builtin_preset_sources,
    import_v2fly_preset,
    list_v2fly_categories,
    list_v2fly_categories_cached,
    parse_v2fly_category_index,
    parse_v2fly_domains,
    parse_v2fly_revision,
    preview_v2fly_preset,
)
from gp_control_plane.storage import read_custom_presets


class DomainSourcesTests(unittest.TestCase):
    def test_parse_v2fly_domains_keeps_safe_domain_rules(self) -> None:
        text = """
include:google
domain:youtube.com @video
full:www.youtube.com
keyword:google
regexp:.*google.*
googlevideo.com
*.gstatic.com
domain:youtube.com
"""

        self.assertEqual(
            parse_v2fly_domains(text),
            ["youtube.com", "www.youtube.com", "googlevideo.com", "gstatic.com"],
        )

    def test_preview_v2fly_preset_reports_diff_without_saving(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)

            preview = preview_v2fly_preset(
                state_dir,
                scope="finder",
                name="v2fly-youtube",
                categories=["youtube"],
                fetcher=lambda category: "domain:youtube.com\nfull:www.youtube.com\n",
            )

            self.assertEqual(preview["count"], 2)
            self.assertIn("not a guarantee", preview["coverage_note"])
            self.assertEqual(preview["added"], ["youtube.com", "www.youtube.com"])
            self.assertEqual(read_custom_presets(state_dir)["finder"], {})

    def test_builtin_preset_sources_disclose_coverage_limit(self) -> None:
        sources = builtin_preset_sources()

        self.assertIn("not a guarantee", sources["google-youtube"]["coverage_note"])

    def test_import_v2fly_preset_saves_user_preset(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)

            result = import_v2fly_preset(
                state_dir,
                scope="finder",
                name="v2fly-discord",
                categories=["discord"],
                fetcher=lambda category: "domain:discord.com\ndomain:discordcdn.com\n",
            )

            self.assertEqual(result["count"], 2)
            self.assertEqual(result["custom"]["finder"]["v2fly-discord"], ["discord.com", "discordcdn.com"])
            self.assertEqual(read_custom_presets(state_dir)["finder"]["v2fly-discord"], ["discord.com", "discordcdn.com"])

    def test_parse_v2fly_category_index_keeps_files_only(self) -> None:
        text = """
[
  {"name": "google", "type": "file"},
  {"name": "youtube", "type": "file"},
  {"name": "nested", "type": "dir"},
  {"name": "../bad", "type": "file"}
]
"""

        self.assertEqual(parse_v2fly_category_index(text), ["google", "youtube"])

    def test_list_v2fly_categories_filters_index(self) -> None:
        text = """
[
  {"name": "discord", "type": "file"},
  {"name": "google", "type": "file"},
  {"name": "youtube", "type": "file"}
]
"""

        result = list_v2fly_categories("goo", fetcher=lambda: text)

        self.assertEqual(result["source"], "github")
        self.assertEqual(result["categories"], ["google"])

    def test_parse_v2fly_revision_reads_github_commit_sha(self) -> None:
        self.assertEqual(parse_v2fly_revision('{"sha": "abc123"}'), "abc123")
        self.assertEqual(parse_v2fly_revision("plain-revision"), "plain-revision")

    def test_cached_v2fly_catalog_uses_local_copy_until_revision_changes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            index_v1 = """
[
  {"name": "discord", "type": "file"},
  {"name": "google", "type": "file"}
]
"""

            first = list_v2fly_categories_cached(
                state_dir,
                limit=100,
                index_fetcher=lambda: index_v1,
                revision_fetcher=lambda: '{"sha": "rev1"}',
            )

            self.assertTrue(first["cached"])
            self.assertEqual(first["revision"], "rev1")
            self.assertEqual(first["categories"], ["discord", "google"])
            self.assertFalse(first["update_available"])

            second = list_v2fly_categories_cached(
                state_dir,
                query="goo",
                limit=100,
                revision_fetcher=lambda: '{"sha": "rev1"}',
            )

            self.assertEqual(second["categories"], ["google"])
            self.assertFalse(second["update_available"])

            changed = list_v2fly_categories_cached(
                state_dir,
                limit=100,
                revision_fetcher=lambda: '{"sha": "rev2"}',
            )

            self.assertTrue(changed["update_available"])
            self.assertTrue(changed["can_refresh"])
            self.assertEqual(changed["categories"], ["discord", "google"])

    def test_refresh_v2fly_catalog_downloads_when_revision_changed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            list_v2fly_categories_cached(
                state_dir,
                limit=100,
                index_fetcher=lambda: '[{"name": "google", "type": "file"}]',
                revision_fetcher=lambda: '{"sha": "rev1"}',
            )

            refreshed = list_v2fly_categories_cached(
                state_dir,
                limit=100,
                refresh=True,
                index_fetcher=lambda: '[{"name": "youtube", "type": "file"}]',
                revision_fetcher=lambda: '{"sha": "rev2"}',
            )

            self.assertEqual(refreshed["revision"], "rev2")
            self.assertEqual(refreshed["categories"], ["youtube"])
            self.assertFalse(refreshed["update_available"])


if __name__ == "__main__":
    unittest.main()
