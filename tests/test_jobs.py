from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.jobs import JobRunner
from gp_control_plane.state import read_state, update_state


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

    def test_dict_result_timeout_is_recorded_as_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            runner = JobRunner(state_dir)

            runner.start("timeout-job", lambda _stop: {"status": "timeout", "id": "run-timeout"})
            state = _wait_for_idle_state(state_dir)
            records = _job_records(state_dir)

            self.assertIsNone(state["current_job"])
            self.assertEqual(state["last_job_status"], "timeout")
            self.assertIsNone(state["last_error"])
            self.assertIn("timeout", [item["status"] for item in records])

    def test_dict_result_failed_is_recorded_as_failed_without_exception(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            runner = JobRunner(state_dir)

            runner.start("failed-result-job", lambda _stop: {"status": "failed", "id": "run-failed"})
            state = _wait_for_idle_state(state_dir)
            records = _job_records(state_dir)

            self.assertIsNone(state["current_job"])
            self.assertEqual(state["last_job_status"], "failed")
            self.assertIsNone(state["last_error"])
            self.assertIn("failed", [item["status"] for item in records])

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

    def test_state_dir_lock_blocks_parallel_runners(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            first = JobRunner(state_dir)
            second = JobRunner(state_dir)
            started = threading.Event()
            release = threading.Event()

            def long_job(_stop: threading.Event) -> dict[str, str]:
                started.set()
                self.assertTrue(release.wait(timeout=2))
                return {"status": "success"}

            first.start("first", long_job)
            self.assertTrue(started.wait(timeout=1))
            self.assertTrue((state_dir / "job-runner.lock").is_file())

            with self.assertRaisesRegex(RuntimeError, "job already running in state_dir"):
                second.start("second", lambda _stop: {"status": "success"})

            release.set()
            state = _wait_for_idle_state(state_dir)
            self.assertEqual(state["last_job_status"], "success")

    def test_job_completion_preserves_state_written_while_job_runs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            runner = JobRunner(state_dir)
            started = threading.Event()
            release = threading.Event()

            def long_job(_stop: threading.Event) -> dict[str, str]:
                started.set()
                self.assertTrue(release.wait(timeout=2))
                return {"status": "success"}

            runner.start("state-preserve", long_job)
            self.assertTrue(started.wait(timeout=1))
            update_state(state_dir, lambda state: state | {"settings": {"curl_parallelism_max": 12}})
            release.set()
            state = _wait_for_idle_state(state_dir)

            self.assertIsNone(state["current_job"])
            self.assertEqual(state["last_job_status"], "success")
            self.assertEqual(state["settings"], {"curl_parallelism_max": 12})

    def test_state_dir_lock_reclaims_stale_foreign_pid_lock(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            lock_path = state_dir / "job-runner.lock"
            state_dir.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(
                json.dumps({"pid": 99999999, "job_id": "dead-job", "job_name": "dead"}),
                encoding="utf-8",
            )

            runner = JobRunner(state_dir)
            runner.start("after-stale", lambda _stop: {"status": "success"})
            state = _wait_for_idle_state(state_dir)

            self.assertEqual(state["last_job_status"], "success")
            self.assertFalse(lock_path.exists())

    def test_state_dir_lock_does_not_reclaim_corrupt_or_current_pid_lock(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            lock_path = state_dir / "job-runner.lock"
            state_dir.mkdir(parents=True, exist_ok=True)
            runner = JobRunner(state_dir)

            lock_path.write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "job already running in state_dir"):
                runner.start("blocked", lambda _stop: {"status": "success"})

            lock_path.unlink()
            lock_path.write_text(json.dumps({"pid": os.getpid(), "job_id": "same-pid"}), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "job already running in state_dir"):
                runner.start("blocked", lambda _stop: {"status": "success"})
            self.assertTrue((state_dir / "job-runner.lock").exists())

            lock_path.unlink()
            second = JobRunner(state_dir)
            second.start("second", lambda _stop: {"status": "success"})
            state = _wait_for_idle_state(state_dir)
            self.assertEqual(state["last_job_status"], "success")


def _wait_for_idle_state(state_dir: Path) -> dict[str, object]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = read_state(state_dir)
        if state.get("current_job") is None and state.get("last_job_status"):
            return state
        time.sleep(0.01)
    raise AssertionError("job did not become idle")


def _job_records(state_dir: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in (state_dir / "jobs.jsonl").read_text(encoding="utf-8").splitlines()]


if __name__ == "__main__":
    unittest.main()
