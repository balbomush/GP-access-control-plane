from __future__ import annotations

import sys
import tempfile
import io
import tarfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.domain_sources import (
    builtin_preset_sources,
    fetch_v2fly_category_local,
    import_v2fly_preset,
    fetch_v2fly_category_index_from_archive,
    list_v2fly_categories_local,
    parse_v2fly_category_index,
    parse_v2fly_domains,
    parse_v2fly_revision,
    prepare_v2fly_local_storage,
    preview_v2fly_preset,
    v2fly_group_cache_dir,
)
from gp_control_plane.storage import read_custom_presets


def _v2fly_archive(files: dict[str, str]) -> bytes:
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        for category, text in files.items():
            payload = text.encode("utf-8")
            info = tarfile.TarInfo(f"domain-list-community-master/data/{category}")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        ignored = tarfile.TarInfo("domain-list-community-master/docs/readme.md")
        ignored.size = 0
        tar.addfile(ignored, io.BytesIO())
    return archive.getvalue()


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

    def test_fetch_v2fly_category_index_from_archive_lists_data_files(self) -> None:
        archive = _v2fly_archive({"google": "domain:example.com\n", "youtube": "domain:example.com\n"})

        class Response:
            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def read(self) -> bytes:
                return archive

        with patch("gp_control_plane.domain_sources.urlopen", return_value=Response()):
            result = fetch_v2fly_category_index_from_archive()

        self.assertEqual(parse_v2fly_category_index(result), ["google", "youtube"])

    def test_prepare_v2fly_local_storage_writes_group_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)

            result = prepare_v2fly_local_storage(
                state_dir,
                archive_fetcher=lambda: _v2fly_archive(
                    {
                        "google": "domain:google.com\n",
                        "youtube": "domain:youtube.com\n",
                    }
                ),
                revision_fetcher=lambda: '{"sha": "rev1"}',
            )

            self.assertEqual(result["source"], "local-storage")
            self.assertEqual(result["count"], 2)
            self.assertEqual((v2fly_group_cache_dir(state_dir) / "google").read_text(encoding="utf-8"), "domain:google.com\n")
            categories = list_v2fly_categories_local(state_dir, query="goo", limit=100)
            self.assertEqual(categories["source"], "local-storage")
            self.assertEqual(categories["status"], "local")
            self.assertEqual(categories["revision"], "rev1")
            self.assertEqual(categories["categories"], ["google"])
            self.assertEqual(fetch_v2fly_category_local(state_dir, "youtube"), "domain:youtube.com\n")

    def test_local_v2fly_preview_uses_local_storage_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            prepare_v2fly_local_storage(
                state_dir,
                archive_fetcher=lambda: _v2fly_archive({"discord": "domain:discord.com\ndomain:discordcdn.com\n"}),
            )

            with patch("gp_control_plane.domain_sources.urlopen", side_effect=AssertionError("network forbidden")):
                preview = preview_v2fly_preset(
                    state_dir,
                    scope="finder",
                    name="v2fly-discord",
                    categories=["discord"],
                    fetcher=lambda category: fetch_v2fly_category_local(state_dir, category),
                )

            self.assertEqual(preview["domains"], ["discord.com", "discordcdn.com"])

    def test_local_v2fly_categories_report_missing_storage(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            result = list_v2fly_categories_local(Path(raw), limit=100)

            self.assertEqual(result["source"], "missing")
            self.assertEqual(result["status"], "missing")
            self.assertEqual(result["error_kind"], "cache")
            self.assertIn("local", result["error_message"])

    def test_parse_v2fly_revision_reads_github_commit_sha(self) -> None:
        self.assertEqual(parse_v2fly_revision('{"sha": "abc123"}'), "abc123")
        self.assertEqual(parse_v2fly_revision("plain-revision"), "plain-revision")


if __name__ == "__main__":
    unittest.main()
