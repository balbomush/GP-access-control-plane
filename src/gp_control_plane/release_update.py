from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .releases import REPO_URL, release_channel_info
from .state import active_runtime_payload, append_jsonl, now_iso, read_state, update_state
from .zapret2 import run_root_helper_command


HelperRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]
TagFetcher = Callable[[], str]
UPDATE_LOG_TAIL_LINES = 80
UPDATE_LOG_TAIL_BYTES = 32_000
UPDATE_QUEUE_STALE_SECONDS = 30 * 60
ACTIVE_UPDATE_STATUSES = {"queueing", "queued", "running"}
_COMMIT_SHA_LENGTHS = {40}


@dataclass(frozen=True)
class ReleaseCandidate:
    """An immutable release tag and the commit it resolved to at queue time."""

    candidate_ref: str
    expected_sha: str

    def as_payload(self) -> dict[str, str]:
        return {
            "candidate_ref": self.candidate_ref,
            "expected_sha": self.expected_sha,
        }


def release_update_plan(
    state_dir: Path,
    *,
    channel: str,
    current_version: str = __version__,
    fetcher: Callable[[], str] | None = None,
    tag_fetcher: Callable[[], str] | None = None,
    candidate_fetcher: TagFetcher | None = None,
) -> dict[str, Any]:
    state = read_state(state_dir)
    _expire_stale_release_update(state_dir, state)
    runtime = active_runtime_payload(state_dir)
    active_run_id = str(runtime.get("run_id") or "")
    active_update = _active_release_update_payload(state, current_version=current_version)
    reason = ""
    if active_run_id:
        reason = "job is running"
    elif active_update:
        reason = "release update is already queued"
    release: dict[str, Any] = {}
    candidate: ReleaseCandidate | None = None
    if not reason:
        release = release_channel_info(current_version=current_version, channel=channel, fetcher=fetcher, tag_fetcher=tag_fetcher)
    if not reason:
        if not release.get("checked"):
            reason = str(release.get("error") or "release check failed")
        elif not release.get("update_available"):
            reason = "no update available"
    if not reason:
        try:
            candidate = _resolve_release_candidate(release, candidate_fetcher=candidate_fetcher)
        except RuntimeError as exc:
            reason = str(exc)
    return {
        "release": release,
        "candidate": candidate.as_payload() if candidate else {},
        "can_update": not reason,
        "blocked_reason": reason,
        "active_run_id": active_run_id,
        "steps": [
            "check selected release channel",
            "pin the release tag to its immutable commit SHA",
            "queue installer through root-helper using the root-owned install profile",
            "verify installed ref/version after installer finishes",
            "mark verification failures with the pinned candidate evidence",
            "service will come back on the selected release",
        ],
    }


def queue_release_update(
    state_dir: Path,
    *,
    channel: str,
    install_dir: Path | None = None,
    current_version: str = __version__,
    fetcher: Callable[[], str] | None = None,
    tag_fetcher: Callable[[], str] | None = None,
    candidate_fetcher: TagFetcher | None = None,
    helper_runner: HelperRunner | None = None,
) -> dict[str, Any]:
    # Kept for source compatibility with existing callers; the strict helper
    # contract deliberately carries no filesystem paths.
    del install_dir
    state = read_state(state_dir)
    _expire_stale_release_update(state_dir, state)
    runtime = active_runtime_payload(state_dir)
    if runtime.get("active"):
        raise RuntimeError("job is running")
    active_update = _active_release_update_payload(state, current_version=current_version)
    if active_update:
        active_update = {**active_update, "deduplicated": True}
        return active_update
    plan = release_update_plan(
        state_dir,
        channel=channel,
        current_version=current_version,
        fetcher=fetcher,
        tag_fetcher=tag_fetcher,
        candidate_fetcher=candidate_fetcher,
    )
    if not plan["can_update"]:
        raise RuntimeError(str(plan["blocked_reason"] or "update is not allowed"))
    release = plan["release"]
    candidate = _candidate_from_payload(plan.get("candidate"))

    queued_at = now_iso()
    update_id = uuid.uuid4().hex
    payload = {
        "queued": True,
        "update_id": update_id,
        "status": "queueing",
        "queued_at": queued_at,
        "release": release,
        "helper_stdout": "",
        "helper": {},
        "unit": "",
        "log_path": "",
        "target_ref": str(release.get("available_version") or "").strip(),
        "candidate_ref": candidate.candidate_ref,
        "expected_sha": candidate.expected_sha,
        "verified_ref": "",
        "verified_sha": "",
        "checked_out_sha": "",
        "installed_sha": "",
        "phase": "queueing",
        "steps": plan["steps"],
    }
    update_state(state_dir, lambda state: state | {"release_update": payload})
    append_jsonl(state_dir / "release-updates.jsonl", payload)

    runner = helper_runner or run_root_helper_command
    result = runner(
        [
            "queue-update",
            "--candidate-ref",
            candidate.candidate_ref,
            "--expected-sha",
            candidate.expected_sha,
        ]
    )
    if result.returncode != 0:
        raise _fail_release_update_queue(state_dir, payload, result.stderr or result.stdout or "root-helper update queue failed")
    helper = _parse_key_value_lines(result.stdout)
    try:
        _validate_queue_helper_output(helper, candidate)
    except RuntimeError as exc:
        raise _fail_release_update_queue(state_dir, payload, str(exc)) from exc

    payload["status"] = "queued"
    payload["helper_stdout"] = result.stdout.strip()
    payload["helper"] = helper
    payload["unit"] = helper.get("unit", "")
    payload["log_path"] = helper.get("log", "")
    payload["phase"] = helper["phase"]
    update_state(state_dir, lambda state: state | {"release_update": payload})
    append_jsonl(state_dir / "release-updates.jsonl", payload)
    return payload


def release_update_status(
    state_dir: Path,
    *,
    current_version: str = __version__,
) -> dict[str, Any]:
    del current_version
    state = read_state(state_dir)
    raw = state.get("release_update")
    if not isinstance(raw, dict):
        return {}
    payload = dict(raw)
    if _is_stale_release_update(payload):
        payload["status"] = "failed"
        payload["error"] = payload.get("error") or "release update queue timeout"
        update_state(state_dir, lambda state: state | {"release_update": payload})
        append_jsonl(state_dir / "release-updates.jsonl", payload)
        return payload
    helper = payload.get("helper") if isinstance(payload.get("helper"), dict) else {}
    log_path = str(payload.get("log_path") or helper.get("log") or "")
    log_tail = ""
    log_values: dict[str, str] = {}
    if log_path:
        path = Path(log_path)
        if path.is_file():
            log_tail = _tail_text(path)
            log_values = _parse_key_value_lines(log_tail)
            payload["log_tail"] = log_tail
    status = str(log_values.get("status") or payload.get("status") or "queued").lower()
    installed_version = str(log_values.get("installed_version") or "")
    installed_ref = str(log_values.get("installed_ref") or "")
    if not log_path:
        latest_log = _latest_update_log(state_dir)
        if latest_log:
            log_path = str(latest_log)
            log_tail = _tail_text(latest_log)
            log_values = _parse_key_value_lines(log_tail)
            payload["log_tail"] = log_tail
            installed_version = str(log_values.get("installed_version") or installed_version)
            installed_ref = str(log_values.get("installed_ref") or installed_ref)
            status = str(log_values.get("status") or status).lower()
    if status in {"queued", "queueing"} and log_tail:
        status = "running"
    candidate_error = _strict_candidate_error(payload, log_values)
    _apply_strict_helper_evidence(payload, log_values)
    if candidate_error:
        prior_error = str(payload.get("error") or log_values.get("error") or "")
        helper_reported_success = status == "success"
        status = "failed"
        payload["verification_error"] = candidate_error
        if helper_reported_success or not prior_error:
            payload["error"] = candidate_error
    elif status == "success" or log_values.get("phase") == "installed":
        strict_error = _strict_success_error(payload, log_values, log_tail)
        if strict_error:
            status = "failed"
            payload["error"] = strict_error
        else:
            _apply_cleanup_success_evidence(payload, log_values)
    payload["status"] = status
    payload["log_path"] = log_path
    payload["installed_version"] = installed_version
    payload["installed_ref"] = installed_ref
    payload["verified"] = bool(status == "success")
    payload["error"] = "" if status == "success" else str(payload.get("error") or log_values.get("error") or "")
    _persist_release_update_status(state_dir, raw, payload)
    return payload


def _active_release_update_payload(state: dict[str, Any], *, current_version: str) -> dict[str, Any] | None:
    raw = state.get("release_update")
    if not isinstance(raw, dict):
        return None
    status = str(raw.get("status") or "").lower()
    if status not in ACTIVE_UPDATE_STATUSES:
        return None
    if _is_stale_release_update(raw):
        return None
    target = str(raw.get("target_ref") or "")
    if target and _version_matches(target, current_version):
        return None
    return dict(raw)


def _expire_stale_release_update(state_dir: Path, state: dict[str, Any]) -> None:
    raw = state.get("release_update")
    if not isinstance(raw, dict) or not _is_stale_release_update(raw):
        return
    payload = dict(raw)
    payload["status"] = "failed"
    payload["error"] = payload.get("error") or "release update queue timeout"
    state["release_update"] = payload
    update_state(state_dir, lambda current: current | {"release_update": payload})
    append_jsonl(state_dir / "release-updates.jsonl", payload)


def _is_stale_release_update(payload: dict[str, Any]) -> bool:
    status = str(payload.get("status") or "").lower()
    if status not in ACTIVE_UPDATE_STATUSES:
        return False
    timestamp = str(payload.get("started_at") or payload.get("queued_at") or "").strip()
    if not timestamp:
        return False
    started_at = _parse_iso(timestamp)
    if not started_at:
        return False
    return (datetime.now(UTC) - started_at).total_seconds() > UPDATE_QUEUE_STALE_SECONDS


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _resolve_release_candidate(
    release: dict[str, Any],
    *,
    candidate_fetcher: TagFetcher | None = None,
) -> ReleaseCandidate:
    tag = str(release.get("available_version") or "").strip()
    candidate_ref = _release_tag_ref(tag)
    try:
        raw_tags = candidate_fetcher() if candidate_fetcher else _fetch_release_tag_refs()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"release candidate resolution failed: {exc}") from exc
    expected_sha = _tag_commit_sha(raw_tags, candidate_ref)
    if not expected_sha:
        raise RuntimeError(f"release candidate resolution failed: commit SHA is missing for {candidate_ref}")
    return ReleaseCandidate(candidate_ref=candidate_ref, expected_sha=expected_sha)


def _fetch_release_tag_refs() -> str:
    result = subprocess.run(
        ["git", "ls-remote", "--tags", REPO_URL],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout


def _release_tag_ref(tag: str) -> str:
    clean_tag = str(tag or "").strip()
    if not clean_tag or clean_tag == "-" or clean_tag.startswith("refs/"):
        raise RuntimeError("release candidate resolution failed: release tag is missing or invalid")
    if any(char.isspace() for char in clean_tag) or any(
        marker in clean_tag for marker in ("..", "//", "@{", "\\", "~", "^", ":", "?", "*", "[")
    ):
        raise RuntimeError("release candidate resolution failed: release tag is invalid")
    if clean_tag.startswith(".") or clean_tag.endswith((".", "/")):
        raise RuntimeError("release candidate resolution failed: release tag is invalid")
    return f"refs/tags/{clean_tag}"


def _tag_commit_sha(raw_tags: str, candidate_ref: str) -> str:
    direct_sha = ""
    peeled_sha = ""
    for raw_line in str(raw_tags or "").splitlines():
        parts = raw_line.strip().split()
        if len(parts) != 2:
            continue
        sha, ref = parts
        if ref == candidate_ref:
            direct_sha = sha
        elif ref == f"{candidate_ref}^{{}}":
            peeled_sha = sha
    candidate_sha = peeled_sha or direct_sha
    return candidate_sha.lower() if _is_full_commit_sha(candidate_sha) else ""


def _candidate_from_payload(value: Any) -> ReleaseCandidate:
    if not isinstance(value, dict):
        raise RuntimeError("release candidate resolution failed: pinned candidate is missing")
    candidate_ref = str(value.get("candidate_ref") or "")
    expected_sha = str(value.get("expected_sha") or "")
    try:
        normalized_ref = _release_tag_ref(candidate_ref.removeprefix("refs/tags/"))
    except RuntimeError as exc:
        raise RuntimeError("release candidate resolution failed: pinned candidate is malformed") from exc
    if candidate_ref != normalized_ref or not _is_full_commit_sha(expected_sha):
        raise RuntimeError("release candidate resolution failed: pinned candidate is malformed")
    return ReleaseCandidate(candidate_ref=candidate_ref, expected_sha=expected_sha)


def _is_full_commit_sha(value: str) -> bool:
    return len(value) in _COMMIT_SHA_LENGTHS and all(char in "0123456789abcdef" for char in value)


def _validate_queue_helper_output(helper: dict[str, str], candidate: ReleaseCandidate) -> None:
    if helper.get("queued") != "true":
        raise RuntimeError("strict root-helper output is missing queued=true")
    if helper.get("status") != "queued":
        raise RuntimeError("strict root-helper output must report status=queued")
    if helper.get("phase") != "queued":
        raise RuntimeError("strict root-helper output must report phase=queued")
    if not helper.get("unit") or not helper.get("log"):
        raise RuntimeError("strict root-helper output is missing unit or log")
    if helper.get("candidate_ref") != candidate.candidate_ref:
        raise RuntimeError("strict root-helper output has an unexpected candidate_ref")
    if helper.get("expected_sha") != candidate.expected_sha:
        raise RuntimeError("strict root-helper output has an unexpected expected_sha")


def _fail_release_update_queue(state_dir: Path, payload: dict[str, Any], error: str) -> RuntimeError:
    message = str(error or "root-helper update queue failed").strip()
    payload["status"] = "failed"
    payload["phase"] = "queue_failed"
    payload["error"] = message
    update_state(state_dir, lambda state: state | {"release_update": payload})
    append_jsonl(state_dir / "release-updates.jsonl", payload)
    return RuntimeError(message)


def _apply_strict_helper_evidence(payload: dict[str, Any], values: dict[str, str]) -> None:
    for key in (
        "installed_ref",
        "installed_version",
        "verified_ref",
        "verified_sha",
        "checked_out_sha",
        "installed_sha",
        "phase",
    ):
        if key in values:
            payload[key] = values[key]


def _apply_cleanup_success_evidence(payload: dict[str, Any], values: dict[str, str]) -> None:
    cleanup_status = str(values.get("cleanup_status") or "completed").strip().lower()
    cleanup_path = str(values.get("cleanup_path") or "").strip()
    payload["cleanup_status"] = cleanup_status
    if cleanup_path:
        payload["cleanup_path"] = cleanup_path
    else:
        payload.pop("cleanup_path", None)


def _strict_candidate_error(payload: dict[str, Any], values: dict[str, str]) -> str:
    try:
        candidate = _candidate_from_payload(payload)
    except RuntimeError as exc:
        return f"strict root-helper output is malformed: {exc}"
    reported_ref = values.get("candidate_ref")
    if reported_ref is not None and reported_ref != candidate.candidate_ref:
        return "strict root-helper output reported a different candidate_ref"
    reported_sha = values.get("expected_sha")
    if reported_sha is not None and reported_sha != candidate.expected_sha:
        return "strict root-helper output reported a different expected_sha"
    return ""


def _strict_success_error(payload: dict[str, Any], values: dict[str, str], log_text: str) -> str:
    try:
        candidate = _candidate_from_payload(payload)
    except RuntimeError as exc:
        return f"strict root-helper output is malformed: {exc}"
    required = (
        "phase",
        "status",
        "verified_ref",
        "verified_sha",
        "checked_out_sha",
        "installed_ref",
        "installed_sha",
        "installed_version",
    )
    missing = [key for key in required if not str(values.get(key) or "").strip()]
    if missing:
        return f"strict root-helper output is missing {', '.join(missing)}"
    if values["phase"] != "installed":
        return "strict root-helper output has an unexpected success phase"
    if values["status"] != "success":
        return "strict root-helper output has an unexpected success status"
    for key in ("verified_sha", "checked_out_sha", "installed_sha"):
        if not _is_full_commit_sha(values[key]):
            return f"strict root-helper output has malformed {key}"
    if values["verified_ref"] != candidate.candidate_ref:
        return "strict root-helper output verified a different candidate_ref"
    if values["verified_sha"] != candidate.expected_sha:
        return "strict root-helper output verified a different SHA"
    if values["checked_out_sha"] != candidate.expected_sha:
        return "strict root-helper output checked out a different SHA"
    if values["installed_ref"] != candidate.candidate_ref:
        return "strict root-helper output installed a different candidate_ref"
    if values["installed_sha"] != candidate.expected_sha:
        return "installed SHA does not match expected SHA"
    if values["installed_version"] != _candidate_version(candidate):
        return "strict root-helper output installed a different version"
    has_cleanup_status = "cleanup_status" in values
    cleanup_status = str(values.get("cleanup_status") or "").strip().lower()
    cleanup_path = str(values.get("cleanup_path") or "").strip()
    if has_cleanup_status and cleanup_status not in {"completed", "deferred", "failed"}:
        return "strict root-helper output has an unexpected cleanup_status"
    if not has_cleanup_status and cleanup_path:
        return "strict root-helper output reported cleanup_path without cleanup_status"
    if cleanup_path and (not cleanup_path.startswith("/") or "\x00" in cleanup_path):
        return "strict root-helper output has an unsafe cleanup_path"
    if cleanup_status == "completed" and cleanup_path:
        return "strict root-helper output reported cleanup_path for completed cleanup"
    if cleanup_status == "failed" and cleanup_path:
        return "strict root-helper output reported cleanup_path for failed cleanup"
    if cleanup_status == "deferred" and not cleanup_path:
        return "strict root-helper output omitted cleanup_path for deferred cleanup"
    if "cleanup_status" in values:
        terminal_error = _strict_success_terminal_error(log_text)
        if terminal_error:
            return terminal_error
    return ""


def _strict_success_terminal_error(log_text: str) -> str:
    records = [line.strip() for line in str(log_text or "").splitlines() if line.strip()]
    if not records or records[-1] != "status=success":
        return "strict root-helper output has evidence after terminal success"
    for record in records:
        if "=" not in record:
            continue
        key, _value = record.split("=", 1)
        if key.strip() in {"error", "rollback_scope"}:
            return "strict root-helper output contains failure evidence"
    return ""


def _candidate_version(candidate: ReleaseCandidate) -> str:
    return candidate.candidate_ref.removeprefix("refs/tags/").removeprefix("v")


def _persist_release_update_status(state_dir: Path, raw: dict[str, Any], payload: dict[str, Any]) -> None:
    persisted = dict(payload)
    persisted.pop("log_tail", None)
    if persisted == raw:
        return
    update_state(state_dir, lambda state: state | {"release_update": persisted})
    append_jsonl(state_dir / "release-updates.jsonl", persisted)


def _parse_key_value_lines(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        clean_key = key.strip()
        if clean_key:
            result[clean_key] = value.strip()
    return result


def _tail_text(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if len(data) > UPDATE_LOG_TAIL_BYTES:
        data = data[-UPDATE_LOG_TAIL_BYTES:]
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) > UPDATE_LOG_TAIL_LINES:
        lines = lines[-UPDATE_LOG_TAIL_LINES:]
    return "\n".join(lines)


def _latest_update_log(state_dir: Path) -> Path | None:
    log_dir = state_dir / "release-updates"
    if not log_dir.is_dir():
        return None
    logs = [path for path in log_dir.glob("*.log") if path.is_file()]
    if not logs:
        return None
    return max(logs, key=lambda path: path.stat().st_mtime)


def _version_matches(target: str, current: str) -> bool:
    return str(target or "").lstrip("v") == str(current or "").lstrip("v")
