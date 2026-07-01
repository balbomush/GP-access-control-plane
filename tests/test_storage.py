from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.storage import SCHEMA_MIGRATIONS, connect, storage_status


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


if __name__ == "__main__":
    unittest.main()
