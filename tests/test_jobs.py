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

from gp_control_plane.config import AppConfig, OutputConfig
from gp_control_plane.core_api import release_update_accepted_payload, runs_history_payload
from gp_control_plane.jobs import JobRunner, _CancellationToken
from gp_control_plane.state import read_state, update_state
from gp_control_plane.storage import append_run, connect, read_latest_run_payloads
from gp_control_plane.strategy_finder import latest_log_tail


class JobRunnerTests(unittest.TestCase):
    def test_cancellation_token_rejects_child_launch_claim_after_cancellation(self) -> None:
        token = _CancellationToken()
        token.set()
        launches: list[str] = []

        def launcher() -> None:
            launches.append("called")

        with token.claim_child_launch() as claimed:
            if claimed:
                launcher()

        self.assertFalse(claimed)
        self.assertEqual(launches, [])

    def test_cancellation_token_defers_cancellation_while_child_launch_is_claimed(self) -> None:
        token = _CancellationToken()
        cancellation_started = threading.Event()
        cancellation_finished = threading.Event()

        def cancel() -> None:
            cancellation_started.set()
            token.set()
            cancellation_finished.set()

        with token.claim_child_launch() as claimed:
            self.assertTrue(claimed)
            canceller = threading.Thread(target=cancel)
            canceller.start()
            self.assertTrue(cancellation_started.wait(timeout=1))
            self.assertFalse(cancellation_finished.wait(timeout=0.1))

        self.assertTrue(cancellation_finished.wait(timeout=1))
        canceller.join(timeout=1)
        self.assertFalse(canceller.is_alive())
        self.assertTrue(token.is_set())

    def test_current_job_is_cleared_when_job_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            runner = JobRunner(state_dir)

            def failing_job(_stop: threading.Event, _run_id: str) -> None:
                raise RuntimeError("root helper unavailable")

            run = runner.start("zapret-standard-discovery", failing_job)
            state = _wait_for_idle_state(state_dir)
            persisted = read_latest_run_payloads(state_dir, limit=1)[0]

            self.assertIsNone(state["current_run_id"])
            self.assertEqual(state["last_run_status"], "failed")
            self.assertIn("root helper unavailable", state["last_error"])
            self.assertEqual(persisted["id"], run.run_id)
            self.assertEqual(persisted["kind"], "zapret-standard-discovery")
            self.assertEqual(persisted["status"], "failed")
            self.assertIn("root helper unavailable", persisted["error"])
            self.assertIn("completed_at", persisted)

    def test_history_api_returns_one_failed_run_after_early_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            runner = JobRunner(state_dir)

            def failing_discovery(_stop: threading.Event, _run_id: str) -> None:
                raise RuntimeError("discovery setup failed")

            run = runner.start("zapret-standard-discovery", failing_discovery)
            _wait_for_idle_state(state_dir)
            history = runs_history_payload(AppConfig(output=OutputConfig(state_dir=state_dir)), {})

            self.assertEqual(
                [(item["run_id"], item["status"]) for item in history["runs"] if item["run_id"] == run.run_id],
                [(run.run_id, "failed")],
            )

    def test_current_job_is_cleared_before_idle_hook_errors_are_swallowed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)

            def failing_idle() -> None:
                raise RuntimeError("idle hook failed")

            runner = JobRunner(state_dir, on_idle=failing_idle)
            runner.start("ok", lambda _stop, _run_id: {"status": "success"})
            state = _wait_for_idle_state(state_dir)

            self.assertIsNone(state["current_run_id"])
            self.assertEqual(state["last_run_status"], "success")
            self.assertIsNone(state["last_error"])

    def test_dict_result_timeout_is_recorded_as_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            runner = JobRunner(state_dir)

            runner.start("timeout-job", lambda _stop, _run_id: {"status": "timeout", "id": "run-timeout"})
            state = _wait_for_idle_state(state_dir)
            records = _job_records(state_dir)

            self.assertIsNone(state["current_run_id"])
            self.assertEqual(state["last_run_status"], "timeout")
            self.assertIsNone(state["last_error"])
            self.assertIn("timeout", [item["status"] for item in records])

    def test_dict_result_failed_is_recorded_as_failed_without_exception(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            runner = JobRunner(state_dir)

            runner.start("failed-result-job", lambda _stop, _run_id: {"status": "failed", "id": "run-failed"})
            state = _wait_for_idle_state(state_dir)
            records = _job_records(state_dir)

            self.assertIsNone(state["current_run_id"])
            self.assertEqual(state["last_run_status"], "failed")
            self.assertIsNone(state["last_error"])
            self.assertIn("failed", [item["status"] for item in records])

    def test_current_job_is_cleared_when_cancelled_job_fails_during_save(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            runner = JobRunner(state_dir)

            def stop_then_fail(stop: threading.Event, _run_id: str) -> None:
                self.assertTrue(stop.wait(timeout=2))
                raise RuntimeError("postprocess failed")

            runner.start("stoppable", stop_then_fail)
            runner.cancel_active()
            state = _wait_for_idle_state(state_dir)

            self.assertIsNone(state["current_run_id"])
            self.assertEqual(state["last_run_status"], "failed")
            self.assertIn("postprocess failed", state["last_error"])

    def test_cancel_active_runs_cancel_hook_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            runner = JobRunner(state_dir)
            hook_called = threading.Event()

            def stoppable_job(stop: threading.Event, _run_id: str) -> dict[str, str]:
                self.assertTrue(stop.wait(timeout=2))
                time.sleep(0.1)
                return {"status": "stopped"}

            runner.start("stoppable", stoppable_job, cancel_hook=hook_called.set)
            result = runner.cancel_active()

            self.assertEqual(result["status"], "stopping")
            self.assertTrue(hook_called.wait(timeout=1))
            state = _wait_for_idle_state(state_dir)
            self.assertEqual(state["last_run_status"], "stopped")

    def test_cancel_active_runs_hook_only_once_for_duplicate_cancels(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            runner = JobRunner(state_dir)
            hook_called = threading.Event()
            job_stopping = threading.Event()
            release = threading.Event()
            hook_calls: list[str] = []

            def stoppable_job(stop: threading.Event, _run_id: str) -> dict[str, str]:
                self.assertTrue(stop.wait(timeout=2))
                job_stopping.set()
                self.assertTrue(release.wait(timeout=2))
                return {"status": "stopped"}

            def hook() -> None:
                hook_calls.append("called")
                hook_called.set()

            runner.start("stoppable", stoppable_job, cancel_hook=hook)
            first = runner.cancel_active()
            self.assertTrue(job_stopping.wait(timeout=1))
            second = runner.cancel_active()
            self.assertTrue(hook_called.wait(timeout=1))
            self.assertEqual(first, second)
            self.assertEqual(hook_calls, ["called"])

            release.set()
            state = _wait_for_idle_state(state_dir)
            self.assertEqual(state["last_run_status"], "stopped")

    def test_cancel_hook_error_does_not_block_stop(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            runner = JobRunner(state_dir)

            def stoppable_job(stop: threading.Event, _run_id: str) -> dict[str, str]:
                self.assertTrue(stop.wait(timeout=2))
                return {"status": "stopped"}

            def broken_hook() -> None:
                raise RuntimeError("cleanup failed")

            runner.start("stoppable", stoppable_job, cancel_hook=broken_hook)
            result = runner.cancel_active()

            self.assertEqual(result["status"], "stopping")
            state = _wait_for_idle_state(state_dir)
            self.assertEqual(state["last_run_status"], "stopped")

    def test_job_result_is_compacted_in_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            runner = JobRunner(state_dir)

            def heavy_job(_stop: threading.Event, _run_id: str) -> dict[str, object]:
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

            self.assertEqual(state["last_run_status"], "success")
            self.assertEqual(success["result"]["candidate_count"], 2)
            self.assertNotIn("candidates", success["result"])
            self.assertNotIn("summary", success["result"])

    def test_failed_job_preserves_existing_log_metadata_beyond_history_page(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            runner = JobRunner(state_dir)
            log_dir = state_dir / "strategy-finder" / "logs"
            log_dir.mkdir(parents=True)

            def failing_job(_stop: threading.Event, run_id: str) -> None:
                stdout_log = log_dir / f"{run_id}.stdout.log"
                stderr_log = log_dir / f"{run_id}.stderr.log"
                stdout_log.write_text("before failure\n", encoding="utf-8")
                stderr_log.write_text("root helper failed\n", encoding="utf-8")
                append_run(
                    state_dir,
                    {
                        "id": run_id,
                        "kind": "zapret-standard-discovery",
                        "status": "running",
                        "stdout_log": str(stdout_log),
                        "stderr_log": str(stderr_log),
                        "debug_stdout": False,
                    },
                )
                with connect(state_dir) as conn:
                    conn.executemany(
                        "INSERT INTO runs(id, kind, status, timestamp, payload_json) VALUES(?, ?, ?, ?, ?)",
                        [
                            (
                                f"newer-run-{index}",
                                "zapret-standard-discovery",
                                "success",
                                "",
                                json.dumps(
                                    {
                                        "id": f"newer-run-{index}",
                                        "kind": "zapret-standard-discovery",
                                        "status": "success",
                                    }
                                ),
                            )
                            for index in range(1_001)
                        ],
                    )
                raise RuntimeError("registered process is stale or invalid")

            run = runner.start("zapret-standard-discovery", failing_job)
            _wait_for_idle_state(state_dir)
            persisted = read_latest_run_payloads(state_dir, limit=1)[0]
            tail = latest_log_tail(state_dir, run_id=run.run_id)

            self.assertEqual(persisted["status"], "failed")
            self.assertTrue(persisted["stdout_log"].endswith(".stdout.log"))
            self.assertTrue(persisted["stderr_log"].endswith(".stderr.log"))
            self.assertIn("debug_stdout", persisted)
            self.assertFalse(persisted["debug_stdout"])
            self.assertEqual(tail["stdout_tail"], "before failure")
            self.assertEqual(tail["stderr_tail"], "root helper failed")

    def test_one_run_id_is_persisted_before_start_returns_and_used_by_events(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            runner = JobRunner(state_dir)
            started = threading.Event()
            release = threading.Event()
            observed_run_ids: list[str] = []

            def blocking_run(_stop: threading.Event, run_id: str) -> dict[str, str]:
                observed_run_ids.append(run_id)
                started.set()
                self.assertTrue(release.wait(timeout=2))
                return {"id": run_id, "status": "success"}

            run = runner.start("zapret-standard-discovery", blocking_run)
            self.assertTrue(started.wait(timeout=1))
            persisted = read_latest_run_payloads(state_dir, limit=1)[0]
            events = _job_records(state_dir)

            self.assertEqual(run.run_id, observed_run_ids[0])
            self.assertEqual(persisted["id"], run.run_id)
            self.assertEqual(persisted["status"], "queued")
            self.assertTrue(events)
            self.assertTrue(all(item["run_id"] == run.run_id for item in events))
            self.assertTrue(all("job_id" not in item for item in events))

            release.set()
            _wait_for_idle_state(state_dir)

    def test_release_update_acknowledgement_uses_only_update_id(self) -> None:
        payload = release_update_accepted_payload({"update_id": "update-123", "status": "queued"})

        self.assertEqual(payload, {"accepted": True, "update_id": "update-123", "status": "queued"})
        self.assertNotIn("run_id", payload)
        self.assertNotIn("job_id", payload)
        with self.assertRaisesRegex(ValueError, "update_id"):
            release_update_accepted_payload({"status": "queued"})

    def test_state_dir_lock_blocks_parallel_runners(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            first = JobRunner(state_dir)
            second = JobRunner(state_dir)
            started = threading.Event()
            release = threading.Event()

            def long_job(_stop: threading.Event, _run_id: str) -> dict[str, str]:
                started.set()
                self.assertTrue(release.wait(timeout=2))
                return {"status": "success"}

            first.start("first", long_job)
            self.assertTrue(started.wait(timeout=1))
            self.assertTrue((state_dir / "job-runner.lock").is_file())

            with self.assertRaisesRegex(RuntimeError, "run already running in state_dir"):
                second.start("second", lambda _stop, _run_id: {"status": "success"})

            release.set()
            state = _wait_for_idle_state(state_dir)
            self.assertEqual(state["last_run_status"], "success")

    def test_job_completion_preserves_state_written_while_job_runs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            runner = JobRunner(state_dir)
            started = threading.Event()
            release = threading.Event()

            def long_job(_stop: threading.Event, _run_id: str) -> dict[str, str]:
                started.set()
                self.assertTrue(release.wait(timeout=2))
                return {"status": "success"}

            runner.start("state-preserve", long_job)
            self.assertTrue(started.wait(timeout=1))
            update_state(state_dir, lambda state: state | {"settings": {"curl_parallelism_max": 12}})
            release.set()
            state = _wait_for_idle_state(state_dir)

            self.assertIsNone(state["current_run_id"])
            self.assertEqual(state["last_run_status"], "success")
            self.assertEqual(state["settings"], {"curl_parallelism_max": 12})

    def test_state_dir_lock_reclaims_stale_foreign_pid_lock(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            lock_path = state_dir / "job-runner.lock"
            state_dir.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(
                json.dumps({"pid": 99999999, "run_id": "dead-run", "run_name": "dead"}),
                encoding="utf-8",
            )

            runner = JobRunner(state_dir)
            runner.start("after-stale", lambda _stop, _run_id: {"status": "success"})
            state = _wait_for_idle_state(state_dir)

            self.assertEqual(state["last_run_status"], "success")
            self.assertFalse(lock_path.exists())

    def test_state_dir_lock_does_not_reclaim_corrupt_or_current_pid_lock(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw)
            lock_path = state_dir / "job-runner.lock"
            state_dir.mkdir(parents=True, exist_ok=True)
            runner = JobRunner(state_dir)

            lock_path.write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "run already running in state_dir"):
                runner.start("blocked", lambda _stop, _run_id: {"status": "success"})

            lock_path.unlink()
            lock_path.write_text(json.dumps({"pid": os.getpid(), "run_id": "same-pid"}), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "run already running in state_dir"):
                runner.start("blocked", lambda _stop, _run_id: {"status": "success"})
            self.assertTrue((state_dir / "job-runner.lock").exists())

            lock_path.unlink()
            second = JobRunner(state_dir)
            second.start("second", lambda _stop, _run_id: {"status": "success"})
            state = _wait_for_idle_state(state_dir)
            self.assertEqual(state["last_run_status"], "success")


def _wait_for_idle_state(state_dir: Path) -> dict[str, object]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = read_state(state_dir)
        lock_path = state_dir / "job-runner.lock"
        if state.get("current_run_id") is None and state.get("last_run_status") and not lock_path.exists():
            return state
        time.sleep(0.01)
    raise AssertionError("job did not become idle")


def _job_records(state_dir: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in (state_dir / "jobs.jsonl").read_text(encoding="utf-8").splitlines()]


if __name__ == "__main__":
    unittest.main()
