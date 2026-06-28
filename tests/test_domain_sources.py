from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.domain_sources import import_v2fly_preset, parse_v2fly_domains, preview_v2fly_preset
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
            self.assertEqual(preview["added"], ["youtube.com", "www.youtube.com"])
            self.assertEqual(read_custom_presets(state_dir)["finder"], {})

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


if __name__ == "__main__":
    unittest.main()
