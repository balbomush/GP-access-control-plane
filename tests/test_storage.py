from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.storage import (
    SCHEMA_MIGRATIONS,
    connect,
    delete_custom_preset,
    read_custom_preset_index,
    read_custom_presets,
    read_preset_domains_page,
    save_custom_presets,
    set_preset_domain_enabled,
    storage_status,
)


class StorageTests(unittest.TestCase):
    def test_heavy_query_indexes_exist(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)

            with connect(state_dir) as conn:
                rows = conn.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'index' AND name NOT LIKE 'sqlite_autoindex%'
                    """
                ).fetchall()

            index_names = {str(row["name"]) for row in rows}
            self.assertTrue(
                {
                    "idx_runs_seq",
                    "idx_runs_id_seq",
                    "idx_domains_name",
                    "idx_strategies_last_seen",
                    "idx_strategy_domain_results_domain_protocol",
                    "idx_strategy_domain_results_domain_strategy",
                    "idx_strategy_domain_results_strategy_domain",
                    "idx_preset_domains_domain",
                    "idx_preset_domains_preset_enabled_position",
                    "idx_preset_domains_preset_position",
                }.issubset(index_names)
            )

    def test_schema_migration_history_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)

            with connect(state_dir) as conn:
                rows = conn.execute("SELECT version, name FROM schema_migrations ORDER BY version").fetchall()

            self.assertEqual([(int(row["version"]), str(row["name"])) for row in rows], list(SCHEMA_MIGRATIONS))
            status = storage_status(state_dir)
            self.assertEqual([item["version"] for item in status["migrations"]], [item[0] for item in SCHEMA_MIGRATIONS])

    def test_new_schema_does_not_create_legacy_candidate_tables(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)

            with connect(state_dir) as conn:
                rows = conn.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table'
                    """
                ).fetchall()

            table_names = {str(row["name"]) for row in rows}
            self.assertFalse(
                {
                    "candidates",
                    "candidate_domains",
                    "candidate_common_domains",
                    "candidate_seen_events",
                    "presets",
                }
                & table_names
            )

    def test_concurrent_connects_do_not_race_stats_views(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)

            def read_stats_views(_: int) -> tuple[int, int]:
                with connect(state_dir) as conn:
                    domain_count = conn.execute("SELECT COUNT(*) FROM domain_stats").fetchone()[0]
                    strategy_count = conn.execute("SELECT COUNT(*) FROM strategy_stats").fetchone()[0]
                    return int(domain_count), int(strategy_count)

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(read_stats_views, range(24)))

            self.assertEqual(results, [(0, 0)] * 24)

    def test_preset_domain_page_supports_search_and_pagination(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            save_custom_presets(
                state_dir,
                {"finder": {"big": ["youtube.com", "youtu.be", "discord.com"]}, "common": {}},
                "2026-07-01T00:00:00Z",
            )

            page = read_preset_domains_page(state_dir, scope="finder", name="big", limit=2)
            second = read_preset_domains_page(state_dir, scope="finder", name="big", limit=2, offset=2)
            search = read_preset_domains_page(state_dir, scope="finder", name="big", query="youtu", limit=10)

            self.assertEqual(page["total"], 3)
            self.assertTrue(page["has_more"])
            self.assertEqual([item["domain"] for item in page["domains"]], ["youtube.com", "youtu.be"])
            self.assertFalse(second["has_more"])
            self.assertEqual([item["domain"] for item in second["domains"]], ["discord.com"])
            self.assertEqual([item["domain"] for item in search["domains"]], ["youtube.com", "youtu.be"])

    def test_disabled_preset_domain_is_omitted_from_active_presets(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            save_custom_presets(
                state_dir,
                {"finder": {"mine": ["youtube.com", "discord.com"]}, "common": {}},
                "2026-07-01T00:00:00Z",
            )

            result = set_preset_domain_enabled(
                state_dir,
                scope="finder",
                name="mine",
                domain="discord.com",
                enabled=False,
                updated_at="2026-07-01T01:00:00Z",
            )
            active = read_custom_presets(state_dir)
            page = read_preset_domains_page(state_dir, scope="finder", name="mine", include_disabled=True)
            enabled_only = read_preset_domains_page(state_dir, scope="finder", name="mine", include_disabled=False)

            self.assertFalse(result["enabled"])
            self.assertEqual(active["finder"]["mine"], ["youtube.com"])
            self.assertEqual(
                [(item["domain"], item["enabled"]) for item in page["domains"]],
                [("youtube.com", True), ("discord.com", False)],
            )
            self.assertEqual([item["domain"] for item in enabled_only["domains"]], ["youtube.com"])

    def test_custom_preset_index_reports_enabled_and_total_counts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            save_custom_presets(
                state_dir,
                {"finder": {"mine": ["youtube.com", "discord.com"]}, "common": {}},
                "2026-07-01T00:00:00Z",
            )
            set_preset_domain_enabled(
                state_dir,
                scope="finder",
                name="mine",
                domain="discord.com",
                enabled=False,
                updated_at="2026-07-01T01:00:00Z",
            )

            index = read_custom_preset_index(state_dir)

            self.assertEqual(index["finder"]["mine"]["enabled_count"], 1)
            self.assertEqual(index["finder"]["mine"]["total_count"], 2)

    def test_delete_custom_preset_removes_one_scope_entry(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            save_custom_presets(
                state_dir,
                {"finder": {"mine": ["youtube.com"]}, "common": {"mine": ["discord.com"]}},
                "2026-07-01T00:00:00Z",
            )

            index = delete_custom_preset(state_dir, scope="finder", name="mine")

            self.assertNotIn("mine", index["finder"])
            self.assertIn("mine", index["common"])
            self.assertEqual(read_custom_presets(state_dir)["common"]["mine"], ["discord.com"])


if __name__ == "__main__":
    unittest.main()
