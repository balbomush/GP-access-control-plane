'Pure unchanged run payload compaction, shared by runs and migration owner.'

from __future__ import annotations


from typing import Any

_OMITTED = object()


_RUN_PAYLOAD_DROP_KEYS = {
    "summary",
    "common",
    "live_summary",
    "results",
    "common_results",
    "direct_available",
    "not_working",
    "candidates",
    "common_candidates",
    "attempts",
    "attempt_results",
    "candidate_events",
    "candidate_samples",
    "common_candidate_samples",
}



_RUN_PAYLOAD_STRUCTURED_LIST_KEYS = {"domains"}



_RUN_PAYLOAD_COMPACT_OBJECT_LIST_KEYS = {
    "domain_skipped",
    "domain_classification",
    "domain_diagnostics",
    "curl_diagnostics",
}



_RUN_PAYLOAD_MAX_SCALAR_LIST = 500



_RUN_PAYLOAD_MAX_OBJECT_LIST = 100



_RUN_PAYLOAD_MAX_STRING = 8192



def compact_run_payload(run: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key, value in run.items():
        cleaned = _compact_payload_value(str(key), value, depth=0)
        if cleaned is not _OMITTED:
            compact[str(key)] = cleaned
    return compact



def _compact_payload_value(key: str, value: Any, *, depth: int) -> Any:
    if key in _RUN_PAYLOAD_DROP_KEYS:
        return _OMITTED
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        if len(value) <= _RUN_PAYLOAD_MAX_STRING:
            return value
        return value[:_RUN_PAYLOAD_MAX_STRING] + "...[truncated]"
    if isinstance(value, list):
        if key in _RUN_PAYLOAD_STRUCTURED_LIST_KEYS:
            return [str(item) for item in value if str(item or "").strip()]
        if key in _RUN_PAYLOAD_COMPACT_OBJECT_LIST_KEYS:
            return [
                _compact_payload_value("", item, depth=depth + 1)
                for item in value[:_RUN_PAYLOAD_MAX_OBJECT_LIST]
            ]
        if all(item is None or isinstance(item, bool | int | float | str) for item in value):
            return [
                _compact_payload_value("", item, depth=depth + 1)
                for item in value[:_RUN_PAYLOAD_MAX_SCALAR_LIST]
            ]
        return {"omitted_count": len(value), "omitted_reason": "large structured list"}
    if isinstance(value, dict):
        if depth >= 5:
            return {"omitted_reason": "nested object too deep"}
        compact: dict[str, Any] = {}
        for child_key, child_value in value.items():
            cleaned = _compact_payload_value(str(child_key), child_value, depth=depth + 1)
            if cleaned is not _OMITTED:
                compact[str(child_key)] = cleaned
        return compact
    return str(value)

