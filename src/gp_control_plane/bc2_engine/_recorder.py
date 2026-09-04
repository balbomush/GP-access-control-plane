"""bc2_engine._recorder — moved from strategy_finder.py / blockchecks_backend.py."""
from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque
from pathlib import Path
from gp_control_plane.state import append_jsonl, now_iso
from gp_control_plane.storage import connect, upsert_candidate_event_conn
from typing import Any
from gp_control_plane.bc2_engine._plan import _elapsed_seconds, _eta_recalculation_attempts, _eta_recalculation_step
from gp_control_plane.bc2_engine._progress import _average_attempt_ms, _phase_from_line, _progress_from_counts, _script_name_from_line
from gp_control_plane.bc2_engine._writers import _loadavg, _meminfo, _process_counts, _read_cpu_totals, _rotate_metrics_file, _runtime_file_sizes
from gp_control_plane.engine_common._constants import ETA_SAMPLE_MAX_POINTS, LIVE_CANDIDATE_FLUSH_SIZE, LIVE_CANDIDATE_QUEUE_MAX_BATCHES, LIVE_CANDIDATE_SAMPLE_LIMIT, METRICS_INTERVAL_SECONDS, PHASE_CHECK_VPN, PHASE_DISCOVERY, PHASE_SUMMARY, _ATTEMPT_RE, _CANDIDATE_WRITER_STOP
from gp_control_plane.engine_common._options import curl_failure_info
from gp_control_plane.engine_common._stdout_parse import _candidate_from_live_success_line, _candidate_from_result_line, _curl_code_from_line, _domain_diagnostics_from_counts, _dominant_failure_from_counts, _is_strategy_failure, _live_attempt_line, _parse_result_line, _protocol_from_test
from gp_control_plane.engine_common._upsert import candidate_id_for

class _LiveStdoutRecorder:
    def __init__(self, state_dir: Path, run: dict[str, Any]):
        self._lock = threading.Lock()
        self._state_dir = state_dir
        self._run = run
        progress_log = str(run.get("progress_log") or "")
        self._progress_log = Path(progress_log) if progress_log else None
        fallback_log = str(run.get("summary_fallback_log") or "")
        self._summary_fallback_log = Path(fallback_log) if fallback_log else None
        self._metrics = _RuntimeMetricsSampler(state_dir, run)
        self._last_progress_attempted = -1
        self._last_progress_written_at = 0.0
        self._eta_baseline_attempted = 0
        self._eta_baseline_elapsed_seconds: int | None = None
        self._section = ""
        self._current_script = ""
        self._phase = PHASE_CHECK_VPN
        self._pending_attempt: str | None = None
        self._attempted = 0
        self._attempts_by_script: dict[str, int] = {}
        self._attempt_times: deque[float] = deque(maxlen=ETA_SAMPLE_MAX_POINTS)
        self._summary_verified = 0
        self._summary_fallbacks = 0
        self._summary_common_seen = 0
        self._summary_line_count = 0
        self._common_line_count = 0
        self._result_count = 0
        self._common_result_count = 0
        self._direct_available_count = 0
        self._not_working_count = 0
        self._candidate_count = 0
        self._common_candidate_count = 0
        self._domain_status_counts: dict[str, dict[str, int]] = {}
        self._domain_code_counts: dict[str, dict[str, int]] = {}
        self._curl_code_counts: dict[str, int] = {}
        self._curl_diagnostics: list[dict[str, Any]] = []
        self._candidate_keys: set[tuple[str, str, str, str, str]] = set()
        self._common_candidate_keys: set[tuple[str, str, str, str, str]] = set()
        self._successful_strategy_keys: set[tuple[str, str]] = set()
        self._candidate_samples: list[dict[str, Any]] = []
        self._common_candidate_samples: list[dict[str, Any]] = []
        self._pending_candidate_events: list[dict[str, Any]] = []
        self._candidate_writer_queue: queue.Queue[list[dict[str, Any]] | object] = queue.Queue(
            maxsize=LIVE_CANDIDATE_QUEUE_MAX_BATCHES
        )
        self._candidate_writer: threading.Thread | None = None
        self._candidate_writer_closed = False
        self._candidate_writer_error: BaseException | None = None

    def record_line(self, raw_line: str) -> None:
        line = raw_line.strip()
        if not line:
            return
        with self._lock:
            self._record_line_locked(line)
            self._write_progress_locked()

    def parsed(self) -> dict[str, Any]:
        self.close()
        with self._lock:
            candidates = list(self._candidate_samples)
            common_candidates = list(self._common_candidate_samples)
            return {
                "summary": [],
                "common": [],
                "live_summary": [str(item.get("raw") or "") for item in candidates if item.get("raw")],
                "candidates": candidates,
                "common_candidates": common_candidates,
                "results": [],
                "common_results": [],
                "direct_available": [],
                "not_working": [],
                "summary_line_count": self._summary_line_count,
                "common_line_count": self._common_line_count,
                "result_count": self._result_count,
                "common_result_count": self._common_result_count,
                "direct_available_count": self._direct_available_count,
                "not_working_count": self._not_working_count,
                "candidate_count": self._candidate_count,
                "common_candidate_count": self._common_candidate_count,
                "domain_diagnostics": _domain_diagnostics_from_counts(
                    self._domain_status_counts,
                    self._domain_code_counts,
                ),
                "curl_diagnostics": list(self._curl_diagnostics),
                "curl_diagnostics_summary": dict(self._curl_code_counts),
                "dominant_failure": _dominant_failure_from_counts(self._domain_status_counts),
                "phase": self._phase,
                "summary_verified": self._summary_verified,
                "summary_fallbacks": self._summary_fallbacks,
                "summary_common_seen": self._summary_common_seen,
                "live_recorded": True,
            }

    def progress(self, run: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            progress = self._progress_locked(run)
            self._write_progress_locked(force=True, run=run)
            return progress

    def mark_phase(self, phase: str, run: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._phase = phase
            self._write_progress_locked(force=True, run=run or self._run)

    def close(self) -> None:
        writer: threading.Thread | None = None
        with self._lock:
            if not self._candidate_writer_closed:
                self._enqueue_pending_candidate_events_locked()
                self._candidate_writer_closed = True
                if self._candidate_writer is not None:
                    self._candidate_writer_queue.put(_CANDIDATE_WRITER_STOP)
                    writer = self._candidate_writer
            elif self._candidate_writer is not None and self._candidate_writer.is_alive():
                writer = self._candidate_writer
        if writer is not None:
            writer.join()
        if self._candidate_writer_error is not None:
            raise RuntimeError("live candidate writer failed") from self._candidate_writer_error

    def _record_line_locked(self, line: str) -> None:
        if line == "* SUMMARY":
            self._section = "summary"
            self._phase = PHASE_SUMMARY
            return
        if line == "* COMMON":
            self._section = "common"
            self._phase = PHASE_SUMMARY
            return
        self._phase = _phase_from_line(line, self._phase)
        script = _script_name_from_line(line)
        if script:
            self._current_script = script
            self._phase = PHASE_DISCOVERY
            self._attempts_by_script.setdefault(script, 0)
            return
        if _ATTEMPT_RE.match(line):
            self._attempted += 1
            self._phase = PHASE_DISCOVERY
            self._attempt_times.append(time.monotonic())
            self._attempts_by_script[self._current_script] = self._attempts_by_script.get(self._current_script, 0) + 1
            self._maybe_update_eta_baseline_locked()
        attempt = _live_attempt_line(line)
        if attempt:
            self._pending_attempt = attempt
            return
        if line == "!!!!! AVAILABLE !!!!!" and self._pending_attempt:
            candidate = _candidate_from_result_line(self._pending_attempt, scope="domain")
            if candidate:
                self._record_candidate_locked(candidate, common=False)
            self._pending_attempt = None
            return
        if line.startswith("UNAVAILABLE") or line.startswith("FAILED"):
            self._record_unavailable_locked(line)
            self._pending_attempt = None
            return
        live_success = _candidate_from_live_success_line(line)
        if live_success:
            self._record_candidate_locked(live_success, common=False)
            return
        if self._section in {"summary", "common"}:
            if self._section == "common":
                self._common_line_count += 1
            else:
                self._summary_line_count += 1
            parsed = _parse_result_line(line)
            if parsed:
                if self._section == "common":
                    self._common_result_count += 1
                else:
                    self._result_count += 1
                    if parsed.get("result") == "working without bypass":
                        self._direct_available_count += 1
                        self._record_domain_status_locked(
                            str(parsed.get("domain") or ""),
                            "direct_available",
                        )
                    if "not working" in str(parsed.get("result") or ""):
                        self._not_working_count += 1
                        self._record_domain_status_locked(
                            str(parsed.get("domain") or ""),
                            "needs_discovery",
                        )
            candidate = _candidate_from_result_line(line, scope=self._section)
            if candidate:
                self._record_summary_candidate_locked(candidate, common=self._section == "common")

    def _maybe_update_eta_baseline_locked(self) -> None:
        baseline_attempted = _eta_recalculation_attempts(self._attempted)
        if baseline_attempted <= 0 or baseline_attempted == self._eta_baseline_attempted:
            return
        self._eta_baseline_attempted = baseline_attempted
        self._eta_baseline_elapsed_seconds = _elapsed_seconds(self._run.get("started_at") or self._run.get("timestamp"))

    def _record_unavailable_locked(self, line: str) -> None:
        if not self._pending_attempt:
            return
        parsed = _parse_result_line(self._pending_attempt)
        if not parsed:
            return
        domain = str(parsed.get("domain") or "")
        if not domain:
            return
        code = _curl_code_from_line(line)
        test = str(parsed.get("test") or "")
        info = curl_failure_info(code, test=test, domain=domain)
        status = str(info.get("status") or "curl_error")
        self._record_domain_status_locked(domain, status)
        if code:
            self._curl_code_counts[code] = self._curl_code_counts.get(code, 0) + 1
            domain_codes = self._domain_code_counts.setdefault(domain, {})
            domain_codes[code] = domain_codes.get(code, 0) + 1
        if len(self._curl_diagnostics) < LIVE_CANDIDATE_SAMPLE_LIMIT:
            self._curl_diagnostics.append(
                {
                    "domain": domain,
                    "test": test,
                    "protocol": _protocol_from_test(test),
                    "code": code,
                    "status": status,
                    "label": info.get("label") or status,
                    "message": info.get("message") or "",
                    "strategy_failure": _is_strategy_failure(info),
                }
            )

    def _record_domain_status_locked(self, domain: str, status: str) -> None:
        if not domain:
            return
        counts = self._domain_status_counts.setdefault(domain, {})
        counts[status] = counts.get(status, 0) + 1

    def _record_summary_candidate_locked(self, candidate: dict[str, Any], common: bool) -> None:
        if common:
            self._summary_common_seen += 1
            return
        live_key = (
            "domain",
            str(candidate.get("test") or ""),
            str(candidate.get("ip_version") or ""),
            str(candidate.get("domain") or ""),
            str(candidate.get("args") or ""),
        )
        if live_key in self._candidate_keys:
            self._summary_verified += 1
            return
        fallback = {**candidate, "scope": "domain", "source": "summary_fallback"}
        self._summary_fallbacks += 1
        self._record_candidate_locked(fallback, common=False)
        if self._summary_fallback_log:
            append_jsonl(
                self._summary_fallback_log,
                {
                    "run_id": self._run["id"],
                    "seen_at": now_iso(),
                    "reason": "summary candidate was not recorded by live parser",
                    "candidate": fallback,
                },
            )

    def _record_candidate_locked(self, candidate: dict[str, Any], common: bool) -> None:
        key = (
            str(candidate.get("scope") or ""),
            str(candidate.get("test") or ""),
            str(candidate.get("ip_version") or ""),
            str(candidate.get("domain") or ""),
            str(candidate.get("args") or ""),
        )
        target = self._common_candidate_keys if common else self._candidate_keys
        if key in target:
            return
        target.add(key)
        if common:
            self._common_candidate_count += 1
            if len(self._common_candidate_samples) < LIVE_CANDIDATE_SAMPLE_LIMIT:
                self._common_candidate_samples.append(candidate)
        else:
            self._candidate_count += 1
            if len(self._candidate_samples) < LIVE_CANDIDATE_SAMPLE_LIMIT:
                self._candidate_samples.append(candidate)
        self._successful_strategy_keys.add((str(candidate.get("protocol") or ""), str(candidate.get("args") or "")))
        candidate_id = candidate_id_for(str(candidate.get("protocol") or ""), str(candidate.get("args") or ""))
        self._pending_candidate_events.append(
            {
                "candidate_id": candidate_id,
                "protocol": str(candidate.get("protocol") or ""),
                "args": str(candidate.get("args") or ""),
                "status": "candidate",
                "run_id": str(self._run.get("id") or ""),
                "domain": str(candidate.get("domain") or ""),
                "domains": (
                    [str(item or "") for item in self._run.get("domains", [])]
                    if common and isinstance(self._run.get("domains"), list)
                    else []
                ),
                "test": str(candidate.get("test") or ""),
                "ip_version": str(candidate.get("ip_version") or ""),
                "seen_at": now_iso(),
                "common": common,
            }
        )
        if len(self._pending_candidate_events) >= LIVE_CANDIDATE_FLUSH_SIZE:
            self._enqueue_pending_candidate_events_locked()

    def _progress_locked(self, run: dict[str, Any]) -> dict[str, Any]:
        successful = len(self._successful_strategy_keys)
        sample = _average_attempt_ms(self._attempt_times)
        return _progress_from_counts(
            run=run,
            attempted=self._attempted,
            attempts_by_script=dict(self._attempts_by_script),
            successful=successful,
            current_script=self._current_script,
            phase=self._phase,
            runtime_ms_per_attempt=sample,
            runtime_sample_count=len(self._attempt_times),
            summary_verified=self._summary_verified,
            summary_fallbacks=self._summary_fallbacks,
            eta_recalculation_attempts_override=self._eta_baseline_attempted,
            eta_elapsed_seconds_override=self._eta_baseline_elapsed_seconds,
        )

    def _write_progress_locked(self, force: bool = False, run: dict[str, Any] | None = None) -> None:
        if not self._progress_log:
            return
        now = time.monotonic()
        attempt_delta = self._attempted - self._last_progress_attempted
        if not force and attempt_delta < _eta_recalculation_step(self._attempted) and now - self._last_progress_written_at < 2.0:
            return
        progress = self._progress_locked(run or self._run)
        tmp = self._progress_log.with_suffix(".json.tmp")
        try:
            self._progress_log.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(progress, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
            tmp.replace(self._progress_log)
            self._last_progress_attempted = self._attempted
            self._last_progress_written_at = now
            self._metrics.maybe_write(progress)
        except OSError:
            return

    def _enqueue_pending_candidate_events_locked(self) -> None:
        if not self._pending_candidate_events:
            return
        if self._candidate_writer_closed:
            raise RuntimeError("live candidate writer is already closed")
        if self._candidate_writer_error is not None:
            raise RuntimeError("live candidate writer failed") from self._candidate_writer_error
        self._ensure_candidate_writer_locked()
        events = self._pending_candidate_events
        self._pending_candidate_events = []
        self._candidate_writer_queue.put(events)

    def _ensure_candidate_writer_locked(self) -> None:
        if self._candidate_writer is not None and self._candidate_writer.is_alive():
            return
        if self._candidate_writer_error is not None:
            raise RuntimeError("live candidate writer failed") from self._candidate_writer_error
        self._candidate_writer = threading.Thread(
            target=self._candidate_writer_loop,
            name=f"gp-live-candidate-writer-{self._run.get('id') or 'run'}",
            daemon=True,
        )
        self._candidate_writer.start()

    def _candidate_writer_loop(self) -> None:
        try:
            with connect(self._state_dir) as conn:
                while True:
                    item = self._candidate_writer_queue.get()
                    if item is _CANDIDATE_WRITER_STOP:
                        return
                    events = item
                    if not isinstance(events, list):
                        continue
                    for event in events:
                        upsert_candidate_event_conn(conn, **event)
                    conn.commit()
        except BaseException as exc:  # pragma: no cover - covered through close()
            self._candidate_writer_error = exc

class _RuntimeMetricsSampler:
    def __init__(self, state_dir: Path, run: dict[str, Any]):
        self._state_dir = state_dir
        self._run = run
        metrics_log = str(run.get("metrics_log") or "")
        self._path = Path(metrics_log) if metrics_log else None
        self._last_written_at = 0.0
        self._last_cpu: tuple[int, int, int] | None = None

    def maybe_write(self, progress: dict[str, Any]) -> None:
        if not self._path:
            return
        now = time.monotonic()
        if now - self._last_written_at < METRICS_INTERVAL_SECONDS:
            return
        self._last_written_at = now
        payload = {
            "timestamp": now_iso(),
            "run_id": self._run.get("id"),
            "phase": progress.get("phase"),
            "phase_label": progress.get("phase_label"),
            "current_script": progress.get("current_script"),
            "attempted": progress.get("attempted"),
            "attempt_total": progress.get("attempt_total"),
            "remaining_attempts": progress.get("remaining_attempts"),
            "successful": progress.get("successful"),
            "eta_seconds": progress.get("eta_seconds"),
            "eta_status": progress.get("eta_status"),
            "progress_status": progress.get("progress_status"),
            "processes": _process_counts(),
            "system": self._system_metrics(),
            "files": _runtime_file_sizes(self._state_dir, self._run),
        }
        try:
            _rotate_metrics_file(self._path)
            append_jsonl(self._path, payload)
        except OSError:
            return

    def _system_metrics(self) -> dict[str, Any]:
        return {
            "loadavg": _loadavg(),
            "cpu_percent": self._cpu_percent(),
            "memory": _meminfo(),
        }

    def _cpu_percent(self) -> dict[str, float] | None:
        current = _read_cpu_totals()
        if current is None:
            return None
        previous = self._last_cpu
        self._last_cpu = current
        if previous is None:
            return None
        total, idle, iowait = current
        prev_total, prev_idle, prev_iowait = previous
        delta_total = total - prev_total
        if delta_total <= 0:
            return None
        busy = max(0, delta_total - (idle - prev_idle))
        iowait_delta = max(0, iowait - prev_iowait)
        return {
            "busy": round((busy / delta_total) * 100.0, 1),
            "iowait": round((iowait_delta / delta_total) * 100.0, 1),
        }
