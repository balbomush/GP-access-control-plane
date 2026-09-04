"""bc2_engine._process — moved from strategy_finder.py / blockchecks_backend.py."""
from __future__ import annotations

import os
import subprocess
import threading
import time
from contextlib import ExitStack, contextmanager
from gp_control_plane.jobs import ManagedRuntimeQuarantinedError
from pathlib import Path
from gp_control_plane.state import active_runtime_payload
from typing import Any, Iterator
from gp_control_plane.zapret2 import _cleanup_nft_blockcheck_tables, _stop_process_group, acknowledge_registered_process_run_terminal, root_command, signal_registered_process_run
from gp_control_plane.bc2_engine._recorder import _LiveStdoutRecorder
from gp_control_plane.bc2_engine._writers import _CompactStdoutWriter, _RotatingTextWriter, _stdout_log_mode
from gp_control_plane.engine_common._constants import DEBUG_STDOUT_LOG_MAX_BYTES, PHASE_SAVING, STDOUT_LOG_MAX_BYTES

def stop_active_blockcheck_runtime(state_dir: Path | None = None) -> None:
    run_id = str(active_runtime_payload(state_dir).get("run_id") or "").strip() if state_dir else ""
    if run_id:
        try:
            signal_registered_process_run(run_id, "TERM")
        except RuntimeError:
            pass
    _cleanup_nft_blockcheck_tables()

def _stop_requested(stop_event: threading.Event | None) -> bool:
    return stop_event is not None and stop_event.is_set()

@contextmanager
def _claim_child_launch(stop_event: threading.Event | None) -> Iterator[bool]:
    """Use JobRunner's atomic cancellation claim when available."""
    claim = getattr(stop_event, "claim_child_launch", None)
    if callable(claim):
        with claim() as claimed:
            yield bool(claimed)
        return
    yield not _stop_requested(stop_event)

def _stopped_process_result(recorder: _LiveStdoutRecorder) -> dict[str, Any]:
    recorder.mark_phase(PHASE_SAVING)
    return {
        "status": "stopped",
        "returncode": None,
        "timed_out": False,
        "stopped": True,
    }

def _root_command_unless_stopped(
    command: list[str],
    *,
    env: dict[str, str],
    stop_event: threading.Event | None,
    **kwargs: Any,
) -> list[str] | None:
    if _stop_requested(stop_event):
        return None
    try:
        rooted_command = root_command(command, env=env, **kwargs)
    except Exception:
        if _stop_requested(stop_event):
            return None
        raise
    return None if _stop_requested(stop_event) else rooted_command

def _run_process_with_live_stdout(
    command: list[str],
    env: dict[str, str],
    stdout_log: Path,
    stderr_log: Path,
    debug_stdout_log: Path | None,
    timeout_seconds: int,
    stop_event: threading.Event | None,
    recorder: _LiveStdoutRecorder,
    run_id: str = "",
) -> dict[str, Any]:
    if _stop_requested(stop_event):
        return _stopped_process_result(recorder)

    status = "success"
    returncode: int | None = None
    timed_out = False
    stopped = False
    reader_errors: list[BaseException] = []

    stdout_mode = _stdout_log_mode(env)
    with ExitStack() as log_stack:
        try:
            debug_handle = (
                log_stack.enter_context(_RotatingTextWriter(debug_stdout_log, DEBUG_STDOUT_LOG_MAX_BYTES))
                if debug_stdout_log and stdout_mode == "debug"
                else None
            )
            stdout_handle = log_stack.enter_context(_RotatingTextWriter(stdout_log, STDOUT_LOG_MAX_BYTES))
            stderr_handle = log_stack.enter_context(stderr_log.open("w", encoding="utf-8"))
        except Exception:
            if _stop_requested(stop_event):
                return _stopped_process_result(recorder)
            raise

        compact_writer = _CompactStdoutWriter(stdout_handle)
        try:
            with _claim_child_launch(stop_event) as claimed:
                if not claimed:
                    return _stopped_process_result(recorder)
                process = subprocess.Popen(
                    command,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    stdout=subprocess.PIPE,
                    stderr=stderr_handle,
                    env=env,
                    start_new_session=hasattr(os, "setsid"),
                )
            def read_stdout() -> None:
                try:
                    if process.stdout is None:
                        return
                    for line in process.stdout:
                        if stdout_mode == "compact":
                            compact_writer.write(line)
                        else:
                            stdout_handle.write(line)
                            stdout_handle.flush()
                        if debug_handle:
                            debug_handle.write(line)
                        recorder.record_line(line)
                except BaseException as exc:  # noqa: BLE001
                    reader_errors.append(exc)
                finally:
                    if process.stdout is not None:
                        process.stdout.close()
                    compact_writer.close()
                    if debug_handle:
                        debug_handle.flush()

            reader = threading.Thread(target=read_stdout, daemon=True)
            reader.start()
            deadline = None if timeout_seconds <= 0 else time.monotonic() + timeout_seconds
            while True:
                if stop_event is not None and stop_event.is_set():
                    try:
                        _stop_process_group(process, run_id)
                        _cleanup_nft_blockcheck_tables()
                        recorder.mark_phase(PHASE_SAVING)
                        returncode = _wait_process_after_stop(process, run_id)
                    except (RuntimeError, subprocess.TimeoutExpired) as exc:
                        raise ManagedRuntimeQuarantinedError(f"managed process cleanup is unverified: {exc}") from exc
                    stopped = True
                    status = "stopped"
                    break
                wait_timeout = 1.0
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        status = "timeout"
                        try:
                            _stop_process_group(process, run_id)
                            _cleanup_nft_blockcheck_tables()
                            recorder.mark_phase(PHASE_SAVING)
                            returncode = _wait_process_after_stop(process, run_id)
                        except (RuntimeError, subprocess.TimeoutExpired) as exc:
                            raise ManagedRuntimeQuarantinedError(f"managed process cleanup is unverified: {exc}") from exc
                        break
                    wait_timeout = min(1.0, remaining)
                try:
                    returncode = process.wait(timeout=wait_timeout)
                    if stop_event is not None and stop_event.is_set():
                        stopped = True
                        status = "stopped"
                        if run_id:
                            try:
                                acknowledge_registered_process_run_terminal(run_id)
                            except RuntimeError as exc:
                                raise ManagedRuntimeQuarantinedError(
                                    f"managed process cleanup is unverified: {exc}"
                                ) from exc
                        _cleanup_nft_blockcheck_tables()
                        recorder.mark_phase(PHASE_SAVING)
                        break
                    if returncode != 0:
                        status = "failed"
                    break
                except subprocess.TimeoutExpired:
                    continue
            reader.join(timeout=5)
        finally:
            compact_writer.close()

    if reader_errors:
        raise RuntimeError(f"failed to read blockcheck stdout: {reader_errors[0]}")
    return {
        "status": status,
        "returncode": returncode,
        "timed_out": timed_out,
        "stopped": stopped,
    }

def _wait_process_after_stop(
    process: subprocess.Popen[str], run_id: str | None, timeout_seconds: float = 5.0
) -> int | None:
    if process.returncode is not None:
        return process.returncode
    try:
        return process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _stop_process_group(process, run_id)
        try:
            return process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            raise RuntimeError("managed process did not terminate after stop")
