from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.application.discovery import DiscoveryService, DiscoverySpec
from gp_control_plane.backups import create_snapshot_if_idle
from gp_control_plane.config import AppConfig, OutputConfig
from gp_control_plane.engines.blockcheck2 import Blockcheck2Adapter
from gp_control_plane.jobs import JobRunner
from gp_control_plane.settings import save_run_settings
from gp_control_plane.state import read_state


def _wait_for_idle(test: unittest.TestCase, state_dir: Path) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = read_state(state_dir)
        if state.get("current_run_id") is None and state.get("last_run_status"):
            return state
        time.sleep(0.01)
    test.fail("controlled JobRunner did not become idle")


class DiscoveryServiceTests(unittest.TestCase):
    def _service(
        self,
        config: AppConfig,
        runner: JobRunner,
        standard: Any,
        multi: Any,
        cancel_hook: Any = None,
    ) -> DiscoveryService:
        return DiscoveryService(
            config,
            runner,
            execute_standard=standard,
            execute_multi_domain=multi,
            cancel_hook=cancel_hook,
        )

    def test_standard_and_multi_start_use_real_runner_admission_and_one_public_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            config = AppConfig(output=OutputConfig(state_dir=state_dir))
            runner = JobRunner(state_dir)
            received: list[tuple[str, DiscoverySpec, object, str]] = []
            completed = threading.Event()

            def execute(kind: str):
                def callback(spec: DiscoverySpec, stop_event: object, run_id: str) -> dict[str, str]:
                    received.append((kind, spec, stop_event, run_id))
                    completed.set()
                    return {"id": run_id, "status": "success"}

                return callback

            service = self._service(config, runner, execute("standard"), execute("multi"))
            standard = service.start_payload({"mode": "standard", "domains": ["youtube.com"], "protocols": ["tcp"]})
            self.assertTrue(completed.wait(timeout=2))
            self.assertEqual("zapret-standard-discovery", standard.name)
            self.assertEqual(standard.run_id, received[0][3])
            self.assertEqual("standard", received[0][0])
            self.assertFalse(received[0][1].payload["include_quic"])
            _wait_for_idle(self, state_dir)

            completed.clear()
            multi = service.start_payload(
                {"mode": "multi_domain", "domains": ["youtube.com", "discord.com"], "protocols": ["tcp", "quic"]}
            )
            self.assertTrue(completed.wait(timeout=2))
            self.assertEqual("zapret-multi-domain-discovery", multi.name)
            self.assertEqual(multi.run_id, received[1][3])
            self.assertEqual("multi", received[1][0])
            self.assertTrue(received[1][1].payload["include_quic"])
            _wait_for_idle(self, state_dir)

    def test_duplicate_start_dry_run_and_stale_reads_keep_the_accepted_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            config = AppConfig(output=OutputConfig(state_dir=state_dir))
            runner = JobRunner(state_dir)
            started = threading.Event()
            cancelled = threading.Event()
            cleanup_calls: list[str] = []

            def active(spec: DiscoverySpec, stop_event: threading.Event, run_id: str) -> dict[str, str]:
                self.assertEqual("zapret-standard-discovery", spec.name)
                started.set()
                self.assertTrue(stop_event.wait(timeout=2))
                cancelled.set()
                return {"id": run_id, "status": "stopped"}

            service = self._service(
                config,
                runner,
                active,
                lambda *_args: self.fail("multi-domain executor must not run"),
                cancel_hook=lambda: cleanup_calls.append("cleanup"),
            )
            accepted = service.start_payload({"mode": "standard", "domains": ["youtube.com"], "protocols": ["tcp"]})
            self.assertTrue(started.wait(timeout=2))
            with self.assertRaisesRegex(RuntimeError, "run already running"):
                service.start_payload({"mode": "multi_domain", "domains": ["discord.com"]})

            for _ in range(2):
                self.assertEqual(accepted.run_id, service.status()["current_run"]["run_id"])
                self.assertTrue(service.history()["runs"])
            dry_run = service.cancel(dry_run=True)
            self.assertEqual({"accepted": True, "status": "dry_run", "run_id": accepted.run_id}, dry_run)
            self.assertFalse(cancelled.is_set())

            stopped = service.cancel(dry_run=False)
            self.assertEqual({"run_id": accepted.run_id, "name": accepted.name, "status": "stopping"}, stopped)
            self.assertTrue(cancelled.wait(timeout=2))
            self.assertEqual(["cleanup"], cleanup_calls)
            finished = _wait_for_idle(self, state_dir)
            self.assertEqual("stopped", finished["last_run_status"])
            self.assertIsNone(finished["current_run_id"])

    def test_stop_before_synthetic_popen_uses_the_same_real_runner_token(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            config = AppConfig(output=OutputConfig(state_dir=state_dir))
            runner = JobRunner(state_dir)
            before_popen = threading.Event()
            allow_popen = threading.Event()
            child_launches: list[str] = []

            def delayed_launch(_spec: DiscoverySpec, stop_event: threading.Event, run_id: str) -> dict[str, str]:
                before_popen.set()
                self.assertTrue(allow_popen.wait(timeout=2))
                if stop_event.is_set():
                    return {"id": run_id, "status": "stopped"}
                child_launches.append(run_id)
                return {"id": run_id, "status": "success"}

            service = self._service(config, runner, delayed_launch, delayed_launch)
            accepted = service.start_payload({"mode": "standard", "domains": ["youtube.com"]})
            self.assertTrue(before_popen.wait(timeout=2))
            self.assertEqual(accepted.run_id, service.cancel(dry_run=False)["run_id"])
            allow_popen.set()
            finished = _wait_for_idle(self, state_dir)
            self.assertEqual("stopped", finished["last_run_status"])
            self.assertEqual([], child_launches)

    def test_stop_during_work_and_finalization_never_targets_stale_history(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            config = AppConfig(output=OutputConfig(state_dir=state_dir))
            snapshot_started = threading.Event()
            release_snapshot = threading.Event()
            snapshot_calls: list[str] = []

            def snapshot() -> dict[str, str]:
                snapshot_calls.append("snapshot")
                snapshot_started.set()
                self.assertTrue(release_snapshot.wait(timeout=2))
                return {
                    "kind": "snapshot",
                    "status": "success",
                    "completed_at": "test-completed",
                    "snapshot_id": "post-run-snapshot",
                }

            runner = JobRunner(state_dir, on_idle=snapshot)
            working = threading.Event()

            def work(_spec: DiscoverySpec, stop_event: threading.Event, run_id: str) -> dict[str, str]:
                working.set()
                self.assertTrue(stop_event.wait(timeout=2))
                return {"id": run_id, "status": "stopped"}

            service = self._service(config, runner, work, work)
            accepted = service.start_payload({"mode": "standard", "domains": ["youtube.com"]})
            self.assertTrue(working.wait(timeout=2))
            self.assertEqual(accepted.run_id, service.cancel(dry_run=False)["run_id"])
            self.assertTrue(snapshot_started.wait(timeout=2))

            saving_status = service.status()
            self.assertEqual({"run_id": accepted.run_id, "status": "saving"}, saving_status["current_run"])
            self.assertEqual(accepted.run_id, service.cancel(dry_run=True)["run_id"])
            with self.assertRaisesRegex(RuntimeError, "no active run"):
                service.cancel(dry_run=False)
            self.assertTrue(create_snapshot_if_idle(state_dir)["queued"])
            self.assertEqual(["snapshot"], snapshot_calls)

            release_snapshot.set()
            finished = _wait_for_idle(self, state_dir)
            self.assertEqual("stopped", finished["last_run_status"])
            self.assertEqual(["snapshot"], snapshot_calls)
            self.assertEqual("post-run-snapshot", finished["last_snapshot"]["snapshot_id"])

    def test_normalization_errors_do_not_admit_a_runner_job(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            config = AppConfig(output=OutputConfig(state_dir=state_dir))
            runner = JobRunner(state_dir)
            service = self._service(
                config,
                runner,
                lambda *_args: self.fail("invalid start must not execute"),
                lambda *_args: self.fail("invalid start must not execute"),
            )

            invalid_payloads = (
                {},
                {"mode": "unsupported", "domains": ["youtube.com"]},
                {"mode": "standard", "domains": ["youtube.com"], "protocols": ["icmp"]},
                {"mode": "standard", "domains": ["youtube.com"], "settings": {"unknown": True}},
            )
            for payload in invalid_payloads:
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    service.start_payload(payload)
                self.assertFalse((state_dir / "job-runner.lock").exists())
                self.assertIsNone(read_state(state_dir).get("current_run_id"))


class Blockcheck2AdapterTests(unittest.TestCase):
    def test_adapter_delegates_existing_defaults_parameters_stop_event_and_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            config = AppConfig(output=OutputConfig(state_dir=state_dir))
            save_run_settings(
                config,
                {
                    "curl_parallelism_max": 7,
                    "curl_parallelism_default": 3,
                    "curl_max_time": 4,
                    "curl_max_time_quic": 5,
                    "curl_max_time_doh": 6,
                    "enable_ipv6": True,
                    "debug_stdout": True,
                },
            )
            standard_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
            multi_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
            stop = threading.Event()

            def standard(*args: Any, **kwargs: Any) -> dict[str, str]:
                standard_calls.append((args, kwargs))
                return {"id": kwargs["run_id"], "status": "success"}

            def multi(*args: Any, **kwargs: Any) -> dict[str, str]:
                multi_calls.append((args, kwargs))
                return {"id": kwargs["run_id"], "status": "success"}

            adapter = Blockcheck2Adapter(config, standard_discovery=standard, multi_domain_discovery=multi)
            standard_result = adapter.execute_standard(
                DiscoverySpec(name="zapret-standard-discovery", payload={"domains": ["youtube.com"]}), stop, "standard-run"
            )
            multi_result = adapter.execute_multi_domain(
                DiscoverySpec(
                    name="zapret-multi-domain-discovery",
                    payload={"domains": ["youtube.com", "discord.com"], "curl_parallelism": 99},
                ),
                stop,
                "multi-run",
            )

            self.assertEqual({"id": "standard-run", "status": "success"}, standard_result)
            self.assertEqual({"id": "multi-run", "status": "success"}, multi_result)
            self.assertEqual(["youtube.com"], standard_calls[0][0][0])
            self.assertEqual(state_dir, standard_calls[0][0][1])
            self.assertIs(stop, standard_calls[0][1]["stop_event"])
            self.assertEqual("standard-run", standard_calls[0][1]["run_id"])
            self.assertTrue(standard_calls[0][1]["enable_ipv6"])
            self.assertTrue(standard_calls[0][1]["debug_stdout"])
            self.assertEqual(4, standard_calls[0][1]["curl_max_time"])
            self.assertEqual(["youtube.com", "discord.com"], multi_calls[0][0][0])
            self.assertIs(stop, multi_calls[0][1]["stop_event"])
            self.assertEqual("multi-run", multi_calls[0][1]["run_id"])
            self.assertEqual(7, multi_calls[0][1]["curl_parallelism"])
