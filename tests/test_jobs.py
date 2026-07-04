from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.jobs import JobRunner
from gp_control_plane.state import read_state


class JobRunnerTests(unittest.TestCase):
    def test_current_job_is_cleared_when_job_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            runner = JobRunner(state_dir)

            def failing_job(_stop: threading.Event) -> None:
                raise RuntimeError("save failed")

            runner.start("failing", failing_job)
            state = _wait_for_idle_state(state_dir)

            self.assertIsNone(state["current_job"])
            self.assertEqual(state["last_job_status"], "failed")
            self.assertIn("save failed", state["last_error"])

    def test_current_job_is_cleared_before_idle_hook_errors_are_swallowed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)

            def failing_idle() -> None:
                raise RuntimeError("idle hook failed")

            runner = JobRunner(state_dir, on_idle=failing_idle)
            runner.start("ok", lambda _stop: {"status": "success"})
            state = _wait_for_idle_state(state_dir)

            self.assertIsNone(state["current_job"])
            self.assertEqual(state["last_job_status"], "success")
            self.assertIsNone(state["last_error"])

    def test_current_job_is_cleared_when_cancelled_job_fails_during_save(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            runner = JobRunner(state_dir)

            def stop_then_fail(stop: threading.Event) -> None:
                self.assertTrue(stop.wait(timeout=2))
                raise RuntimeError("postprocess failed")

            runner.start("stoppable", stop_then_fail)
            runner.cancel_active()
            state = _wait_for_idle_state(state_dir)

            self.assertIsNone(state["current_job"])
            self.assertEqual(state["last_job_status"], "failed")
            self.assertIn("postprocess failed", state["last_error"])

    def test_cancel_active_runs_cancel_hook_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            runner = JobRunner(state_dir)
            hook_called = threading.Event()

            def stoppable_job(stop: threading.Event) -> dict[str, str]:
                self.assertTrue(stop.wait(timeout=2))
                time.sleep(0.1)
                return {"status": "stopped"}

            runner.start("stoppable", stoppable_job, cancel_hook=hook_called.set)
            result = runner.cancel_active()

            self.assertEqual(result["status"], "stopping")
            self.assertTrue(hook_called.wait(timeout=1))
            state = _wait_for_idle_state(state_dir)
            self.assertEqual(state["last_job_status"], "stopped")

    def test_cancel_hook_error_does_not_block_stop(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            runner = JobRunner(state_dir)

            def stoppable_job(stop: threading.Event) -> dict[str, str]:
                self.assertTrue(stop.wait(timeout=2))
                return {"status": "stopped"}

            def broken_hook() -> None:
                raise RuntimeError("cleanup failed")

            runner.start("stoppable", stoppable_job, cancel_hook=broken_hook)
            result = runner.cancel_active()

            self.assertEqual(result["status"], "stopping")
            state = _wait_for_idle_state(state_dir)
            self.assertEqual(state["last_job_status"], "stopped")

    def test_job_result_is_compacted_in_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            runner = JobRunner(state_dir)

            def heavy_job(_stop: threading.Event) -> dict[str, object]:
                return {
                    "id": "run-heavy",
                    "status": "success",
                    "timestamp": "2026-07-01T00:00:00Z",
                    "candidate_count": 2,
                    "candidates": [{"args": "--a"}, {"args": "--b"}],
                    "summary": {"items": list(range(100))},
                }

            runner.start("heavy", heavy_job)
            state = _wait_for_idle_state(state_dir)
            lines = (state_dir / "jobs.jsonl").read_text(encoding="utf-8").splitlines()
            success = [json.loads(line) for line in lines if json.loads(line).get("status") == "success"][0]

            self.assertEqual(state["last_job_status"], "success")
            self.assertEqual(success["result"]["candidate_count"], 2)
            self.assertNotIn("candidates", success["result"])
            self.assertNotIn("summary", success["result"])


def _wait_for_idle_state(state_dir: Path) -> dict[str, object]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = read_state(state_dir)
        if state.get("current_job") is None and state.get("last_job_status"):
            return state
        time.sleep(0.01)
    raise AssertionError("job did not become idle")


if __name__ == "__main__":
    unittest.main()
