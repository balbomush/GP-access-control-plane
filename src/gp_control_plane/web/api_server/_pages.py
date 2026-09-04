"""api_server UI page + preset/candidate payload builders — moved from api_server.py."""

from __future__ import annotations

from typing import Any

from gp_control_plane import __version__, core_api
from gp_control_plane.config import AppConfig
from gp_control_plane.domain_sources import (
    builtin_preset_sources,
    fetch_v2fly_category_local,
    import_v2fly_preset,
    list_v2fly_categories_local,
    preview_v2fly_preset,
)
from gp_control_plane.engine_common import (
    domain_sets,
    read_candidate_domain_index,
    read_candidate_page,
)
from gp_control_plane.releases import release_channel_info
from gp_control_plane.settings import read_service_settings
from gp_control_plane.storage import (
    read_custom_preset_index,
    read_custom_presets,
    read_preset_domains_page,
    read_system_preset_index,
    read_system_presets,
)
from gp_control_plane.web.api_server._helpers import (
    _payload_string_list,
    _query_bool,
    _query_domains,
    _query_int,
    _query_str,
)


def index_html() -> str:
    from gp_control_plane.web.ui import index_html as _index_html

    return _index_html()


def _candidate_page_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    return read_candidate_page(
        config.output.state_dir,
        limit=_query_int(query, "limit", 50),
        offset=_query_int(query, "offset", 0),
        query=_query_str(query, "query", ""),
        view=_query_str(query, "view", "domain"),
        domains=_query_domains(query, "domains"),
        domain=_query_str(query, "domain", ""),
        fragmentation_classes=_query_domains(query, "fragmentation_class"),
    )


def _candidate_domain_index_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    return read_candidate_domain_index(
        config.output.state_dir,
        limit=_query_int(query, "limit", 50),
        offset=_query_int(query, "offset", 0),
        query=_query_str(query, "query", ""),
        fragmentation_classes=_query_domains(query, "fragmentation_class"),
    )


def _runs_page_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    return core_api.runs_history_page_payload(config, query)


def _presets_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "metadata": read_custom_preset_index(config.output.state_dir),
        "system_metadata": read_system_preset_index(config.output.state_dir),
        "system": read_system_presets(config.output.state_dir),
    }
    if _query_bool(query, "include_domains", False):
        payload["custom"] = read_custom_presets(config.output.state_dir)
    else:
        payload["custom"] = {"finder": {}, "common": {}}
    return payload


def _web_presets_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    return _presets_payload(config, query) | {
        "domain_sets": domain_sets(),
        "builtin": builtin_preset_sources(),
    }


def _preset_domains_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    return read_preset_domains_page(
        config.output.state_dir,
        scope=_query_str(query, "scope", ""),
        name=_query_str(query, "name", ""),
        kind=_query_str(query, "kind", "user"),
        query=_query_str(query, "query", ""),
        limit=_query_int(query, "limit", 200),
        offset=_query_int(query, "offset", 0),
        include_disabled=_query_bool(query, "include_disabled", True),
    )


def _release_info_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    settings = read_service_settings(config)
    channel = _query_str(query, "channel", str(settings.get("update_channel") or "stable"))
    stable = release_channel_info(current_version=__version__, channel="stable")
    prerelease = release_channel_info(current_version=__version__, channel="prerelease")
    selected = prerelease if channel == "prerelease" else stable
    return {"release": selected, "releases": {"stable": stable, "prerelease": prerelease}}


def _v2fly_categories_payload(config: AppConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    return list_v2fly_categories_local(
        config.output.state_dir,
        query=_query_str(query, "query", ""),
        limit=_query_int(query, "limit", 2000),
    )


def _v2fly_preview_payload(config: AppConfig, payload: dict[str, Any]) -> dict[str, Any]:
    state_dir = config.output.state_dir
    return preview_v2fly_preset(
        state_dir,
        scope=str(payload.get("scope") or "finder"),
        name=str(payload.get("name") or ""),
        categories=_payload_string_list(payload, "categories"),
        domains=_payload_string_list(payload, "domains"),
        fetcher=lambda category: fetch_v2fly_category_local(state_dir, category),
    )


def _v2fly_import_payload(config: AppConfig, payload: dict[str, Any]) -> dict[str, Any]:
    state_dir = config.output.state_dir
    return import_v2fly_preset(
        state_dir,
        scope=str(payload.get("scope") or "finder"),
        name=str(payload.get("name") or ""),
        categories=_payload_string_list(payload, "categories"),
        domains=_payload_string_list(payload, "domains"),
        fetcher=lambda category: fetch_v2fly_category_local(state_dir, category),
    )
