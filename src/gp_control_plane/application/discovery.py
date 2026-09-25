"""Application boundary for the existing strategy-discovery lifecycle.

The service deliberately receives its JobRunner and concrete execution
callables from runtime startup.  Importing this module and handling an HTTP
request therefore cannot construct a runner, listener, or worker of its own.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .. import core_api
from ..config import AppConfig
from ..jobs import JobRunner, Run
from ..state import read_state


STANDARD_DISCOVERY_JOB = "zapret-standard-discovery"
MULTI_DOMAIN_DISCOVERY_JOB = "zapret-multi-domain-discovery"

DiscoveryExecute = Callable[["DiscoverySpec", Any, str], dict[str, Any]]
CancelHook = Callable[[], Any]


@dataclass(frozen=True)
class DiscoverySpec:
    """Already-normalized start request passed from the compatible Core adapter."""

    name: str
    payload: Mapping[str, Any]


class DiscoveryService:
    """Own the application-level Start/Stop dispatch, not a runner lifecycle."""

    def __init__(
        self,
        config: AppConfig,
        runner: JobRunner,
        *,
        execute_standard: DiscoveryExecute,
        execute_multi_domain: DiscoveryExecute,
        cancel_hook: CancelHook | None = None,
    ):
        self._config = config
        self._runner = runner
        self._execute_standard = execute_standard
        self._execute_multi_domain = execute_multi_domain
        self._cancel_hook = cancel_hook

    def start(self, spec: DiscoverySpec) -> Run:
        """Submit one normalized discovery spec through the injected runner."""
        if spec.name == STANDARD_DISCOVERY_JOB:
            execute = self._execute_standard
        elif spec.name == MULTI_DOMAIN_DISCOVERY_JOB:
            execute = self._execute_multi_domain
        else:
            raise ValueError("unsupported strategy discovery mode")

        return self._runner.start(
            spec.name,
            lambda stop_event, run_id: execute(spec, stop_event, run_id),
            cancel_hook=self._cancel_hook,
        )

    def start_payload(self, payload: dict[str, Any]) -> Run:
        """Preserve the existing Core payload normalisation and public errors."""
        name, normalized_payload = core_api.strategy_discovery_job_payload(payload)
        return self.start(DiscoverySpec(name=name, payload=normalized_payload))

    def cancel(self, *, dry_run: bool) -> dict[str, Any]:
        """Preserve the existing dry-run response and real runner cancellation."""
        if dry_run:
            state = read_state(self._config.output.state_dir)
            return {
                "accepted": True,
                "status": "dry_run",
                "run_id": str(state.get("current_run_id") or ""),
            }
        return self._runner.cancel_active()

    def status(self) -> dict[str, Any]:
        """Expose the unchanged Core status payload without mutating admission."""
        return core_api.status_payload(self._config)

    def history(self, query: dict[str, list[str]] | None = None) -> dict[str, Any]:
        """Use existing history payload adapters; late reads cannot alter a run."""
        if query is None:
            return core_api.runs_history_payload(self._config, {})
        return core_api.runs_history_page_payload(self._config, query)
