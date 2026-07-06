from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.state import read_state


class StateTests(unittest.TestCase):
    def test_read_state_filters_removed_future_fields(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            (state_dir / "state.json").write_text(
                json.dumps(
                    {
                        "last_sync_at": "old",
                        "last_validate_at": "old",
                        "last_render_at": "old",
                        "selected_strategy": "old",
                        "current_job": "job",
                        "last_error": "error",
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(read_state(state_dir), {"current_job": "job", "last_error": "error"})

    def test_read_state_preserves_current_runtime_sections(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            (state_dir / "state.json").write_text(
                json.dumps(
                    {
                        "current_job": None,
                        "last_error": None,
                        "settings": {"enable_ipv6": True},
                        "discovery_profiles": {"night-test": {"title": "Night test"}},
                    }
                ),
                encoding="utf-8",
            )

            state = read_state(state_dir)

            self.assertEqual(state["settings"], {"enable_ipv6": True})
            self.assertEqual(state["discovery_profiles"], {"night-test": {"title": "Night test"}})

    def test_read_state_returns_defaults_for_corrupt_json(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            (state_dir / "state.json").write_text("{broken", encoding="utf-8")

            self.assertEqual(read_state(state_dir), {"current_job": None, "last_error": None})


if __name__ == "__main__":
    unittest.main()
