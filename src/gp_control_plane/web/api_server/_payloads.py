"""api_server web JSON payload entrypoints — moved from api_server.py (package split)."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from gp_control_plane.bs_engine import list_bs_dns_pins
from gp_control_plane.config import AppConfig
from gp_control_plane.state import now_iso
from gp_control_plane.storage import (
    delete_user_presets,
    save_custom_preset,
    save_system_preset,
)
from gp_control_plane.web.api_server._events import _events_response_payload
from gp_control_plane.web.api_server._helpers import _payload_string_list, _query_one
from gp_control_plane.web.api_server._pages import (
    _candidate_domain_index_payload,
    _candidate_page_payload,
    _preset_domains_payload,
    _runs_page_payload,
    _web_presets_payload,
)
from gp_control_plane.web.api_server._preferences import (
    read_run_preferences,
    save_run_preferences,
)


def web_json_get_payload(config: AppConfig, path: str, query: dict[str, list[str]]) -> dict[str, Any]:
    routes = {
        "/api/web/run-preferences": lambda: {"run_preferences": read_run_preferences(config)},
        "/api/web/runs/history-page": lambda: _runs_page_payload(config, query),
        "/api/web/candidate-domain-index-page": lambda: _candidate_domain_index_payload(config, query),
        "/api/web/strategy-candidates-page": lambda: _candidate_page_payload(config, query),
        "/api/web/presets": lambda: _web_presets_payload(config, query),
        "/api/web/presets/domains": lambda: _preset_domains_payload(config, query),
        "/api/web/bs-dns-pins": lambda: list_bs_dns_pins(domain=_query_one(query, "domain")),
        "/api/web/events": lambda: _events_response_payload(config, query, stream="web"),
    }
    if path not in routes:
        raise KeyError(path)
    return routes[path]()


def web_json_post_response(config: AppConfig, path: str, payload: dict[str, Any]) -> tuple[dict[str, Any], HTTPStatus]:
    if path == "/api/web/run-preferences":
        return {"run_preferences": save_run_preferences(config, payload.get("run_preferences") or payload)}, HTTPStatus.OK
    if path == "/api/web/presets/save":
        scope = str(payload.get("scope") or "")
        name = str(payload.get("name") or "")
        kind = str(payload.get("kind") or "user")
        domains = _payload_string_list(payload, "domains")
        if kind == "system":
            save_system_preset(
                config.output.state_dir,
                scope=scope,
                name=name,
                domains=domains,
                updated_at=now_iso(),
            )
        else:
            save_custom_preset(
                config.output.state_dir,
                scope=scope,
                name=name,
                domains=domains,
                updated_at=now_iso(),
            )
        return _web_presets_payload(config, {"include_domains": ["1"]}), HTTPStatus.OK
    if path == "/api/web/presets/delete-user-lists":
        names = _payload_string_list(payload, "names")
        if not names and payload.get("name"):
            names = [str(payload.get("name") or "")]
        metadata = delete_user_presets(
            config.output.state_dir,
            scope=str(payload.get("scope") or ""),
            names=names,
        )
        return _web_presets_payload(config, {"include_domains": ["1"]}) | {"metadata": metadata}, HTTPStatus.OK
    raise KeyError(path)
