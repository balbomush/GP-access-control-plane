from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.state import update_state, read_state, write_state


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
                        "current_run_id": "job",
                        "last_error": "error",
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(read_state(state_dir), {"current_run_id": "job", "last_error": "error"})

    def test_read_state_preserves_current_runtime_sections(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            (state_dir / "state.json").write_text(
                json.dumps(
                    {
                        "current_run_id": None,
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

            self.assertEqual(read_state(state_dir), {"current_run_id": None, "last_error": None})

    def test_update_state_merges_parallel_field_updates(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            barrier = threading.Barrier(3)

            def update_settings() -> None:
                barrier.wait(timeout=2)

                def mutate(state: dict[str, object]) -> dict[str, object]:
                    time.sleep(0.02)
                    state["settings"] = {"curl_parallelism_max": 10}
                    return state

                update_state(state_dir, mutate)

            def update_preferences() -> None:
                barrier.wait(timeout=2)
                update_state(state_dir, lambda state: state | {"run_preferences": {"domains": ["youtube.com"]}})

            threads = [threading.Thread(target=update_settings), threading.Thread(target=update_preferences)]
            for thread in threads:
                thread.start()
            barrier.wait(timeout=2)
            for thread in threads:
                thread.join(timeout=2)

            state = read_state(state_dir)

            self.assertEqual(state["settings"], {"curl_parallelism_max": 10})
            self.assertEqual(state["run_preferences"], {"domains": ["youtube.com"]})
            update_state(state_dir, lambda state: state | {"lock_released": True})
            self.assertTrue(read_state(state_dir)["lock_released"])

    def test_update_state_rolls_back_when_mutator_raises(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            write_state(state_dir, {"current_run_id": "job-1", "settings": {"enable_ipv6": False}})

            def broken_update(state: dict[str, object]) -> dict[str, object]:
                state["settings"] = {"enable_ipv6": True}
                raise RuntimeError("boom")

            with self.assertRaisesRegex(RuntimeError, "boom"):
                update_state(state_dir, broken_update)

            self.assertEqual(read_state(state_dir)["settings"], {"enable_ipv6": False})
            update_state(state_dir, lambda state: state | {"after_error": True})
            self.assertTrue(read_state(state_dir)["after_error"])
            self.assertEqual(list(state_dir.glob("state.json.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
