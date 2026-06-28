from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.storage import connect


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


if __name__ == "__main__":
    unittest.main()
