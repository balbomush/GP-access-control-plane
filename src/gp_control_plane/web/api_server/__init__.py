"""gp_control_plane.web.api_server — HTTP API/web server (package split, Milestone 3).

Module->package facade: the whole original module namespace is re-exported so
existing ``from gp_control_plane.web.api_server import X`` (and the
``gp_control_plane.web.app`` compatibility alias) keeps working unchanged.

The request handler and serve() live across submodules:
  _http/_get/_post/_events  — composed BaseHTTPRequestHandler mixins
  _payloads/_pages/_events  — payload/page/event builders
  _jobs/_preferences        — job workers + vault + run preferences
  _helpers/_errors          — leaf helpers + exceptions
  _server                   — runtime recovery + ApiHandler composition
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from gp_control_plane import __version__, core_api, service_api
from gp_control_plane.auth import (
    AuthenticationError,
    PasswordValidationError,
    change_password,
    health_payload,
    login,
    require_bearer_token,
)
from gp_control_plane.backups import (
    create_post_run_snapshot,
    create_snapshot_if_idle,
    delete_snapshot_if_idle,
    import_snapshot_archive,
    restore_snapshot_if_idle,
    snapshot_file_path,
)
from gp_control_plane.bc2_engine import (
    run_multi_domain_discovery,
    run_standard_discovery,
)
from gp_control_plane.bs_engine import (
    export_nfconf,
    list_bs_dns_pins,
    run_blockchecks_discovery,
    stop_blockchecks,
)
from gp_control_plane.config import AppConfig
from gp_control_plane.discovery_engine import (
    campaign_lock_busy_message,
    check_blockchecks_install,
    is_blockchecks_job,
    normalize_engine,
)
from gp_control_plane.domain_sources import (
    builtin_preset_sources,
    fetch_v2fly_category_local,
    fetch_v2fly_revision,
    import_v2fly_preset,
    list_v2fly_categories_local,
    parse_v2fly_domains,
    parse_v2fly_revision,
    prepare_v2fly_local_storage,
    preview_v2fly_preset,
    read_v2fly_catalog_cache,
    read_v2fly_group_manifest,
    write_v2fly_catalog_cache,
)
from gp_control_plane.engine_common import (
    candidate_storage_version,
    close_stale_running_runs,
    domain_sets,
    latest_log_tail,
    read_candidate_domain_index,
    read_candidate_page,
    read_runs,
)
from gp_control_plane.jobs import JobRunner
from gp_control_plane.releases import release_channel_info
from gp_control_plane.resource_budget import (
    BACKUP_STREAM_CHUNK_BYTES,
    BACKUP_UPLOAD_MAX_BYTES,
    JSON_REQUEST_MAX_BYTES,
)
from gp_control_plane.settings import (
    DEFAULT_SETTINGS,
    read_run_settings,
    read_service_settings,
    read_settings,
    save_run_settings,
    save_settings,
)
from gp_control_plane.state import (
    active_job_lock_payload,
    has_active_runtime,
    now_iso,
    read_state,
    update_state,
)
from gp_control_plane.storage import (
    delete_custom_preset,
    delete_user_presets,
    read_custom_preset_index,
    read_custom_presets,
    read_preset_domains_page,
    read_system_preset_index,
    read_system_presets,
    save_custom_preset,
    save_custom_presets,
    save_system_preset,
    set_preset_domain_enabled,
)
from gp_control_plane.storage import (
    is_storage_unavailable_error as _is_storage_unavailable_error,
)
from gp_control_plane.web.api_server._errors import (
    RequestBodyTooLarge,
    RuntimeBusyError,
)
from gp_control_plane.web.api_server._events import (
    _EVENT_CURSOR_LOCK,
    _EVENT_CURSOR_STATE,
    EventsMixin,
    _core_event_payloads,
    _current_run_latest_log_payload,
    _event_cursor,
    _event_fingerprint,
    _event_payloads,
    _event_sequence,
    _events_response_payload,
    _latest_log_payload,
    _log_event_payload,
    _optional_path,
    _path_version,
    _runs_event_payload,
    _web_event_payloads,
    status_payload,
    web_event_changes,
)
from gp_control_plane.web.api_server._get import GetMixin
from gp_control_plane.web.api_server._helpers import (
    _bounded_int,
    _clean_domain_list,
    _minimum_int,
    _multipart_file_bytes,
    _payload_bool,
    _payload_domains,
    _payload_int,
    _payload_string_list,
    _payload_timeout_seconds,
    _query_bool,
    _query_domains,
    _query_int,
    _query_one,
    _query_str,
)
from gp_control_plane.web.api_server._http import (
    MAX_BACKUP_UPLOAD_BYTES,
    MAX_JSON_REQUEST_BYTES,
    NDJSON_CONTENT_TYPE,
    HttpMixin,
)
from gp_control_plane.web.api_server._jobs import (
    _clean_install_vault_create_response,
    _clean_install_vault_public_metadata,
    _clean_install_vault_restore_response,
    _job_blockchecks_discovery,
    _job_discovery,
    _job_zapret_multi_domain_discovery,
    _job_zapret_standard_discovery,
)
from gp_control_plane.web.api_server._pages import (
    _candidate_domain_index_payload,
    _candidate_page_payload,
    _preset_domains_payload,
    _presets_payload,
    _release_info_payload,
    _runs_page_payload,
    _v2fly_categories_payload,
    _v2fly_import_payload,
    _v2fly_preview_payload,
    _web_presets_payload,
    index_html,
)
from gp_control_plane.web.api_server._payloads import (
    web_json_get_payload,
    web_json_post_response,
)
from gp_control_plane.web.api_server._post import PostMixin
from gp_control_plane.web.api_server._preferences import (
    DEFAULT_DISCOVERY_PROFILES,
    DEFAULT_RUN_PREFERENCES,
    _normalize_discovery_profile,
    _normalize_run_preferences,
    _profile_name,
    read_discovery_profiles,
    read_run_preferences,
    save_discovery_profiles,
    save_run_preferences,
)
from gp_control_plane.web.api_server._server import (
    _ROOT_MANAGED_DISCOVERY_NAMES,
    ApiHandler,
    _clear_stale_current_run,
    _recover_runtime_before_serve,
    _requires_verified_root_recovery,
)
from gp_control_plane.web.docs import (
    OPENAPI_JSON_CONTENT_TYPE,
    SWAGGER_HTML_CONTENT_TYPE,
    SWAGGER_PATHS,
    openapi_json_bytes,
    swagger_ui_html,
)
from gp_control_plane.web.errors import error_payload, normalize_error_payload
from gp_control_plane.web.routes import (
    JSON_GET_ROUTE_PATHS,
    JSON_HEAD_ROUTE_PATHS,
    JSON_POST_ROUTE_PATHS,
    route_for,
)
from gp_control_plane.zapret2 import (
    check_install_cached,
    cleanup_nft_blockcheck_tables,
    recover_quarantined_process_run,
    recover_registered_process_runs,
)

_core_strategy_discovery_job_payload = core_api.strategy_discovery_job_payload


def serve(config: AppConfig, host: str, port: int, *, ui_enabled: bool = True) -> None:
    _recover_runtime_before_serve(config)
    close_stale_running_runs(config.output.state_dir)
    runner = JobRunner(config.output.state_dir, on_idle=lambda: create_post_run_snapshot(config.output.state_dir))
    runtime_role = "monolith" if ui_enabled else "core"
    web_install_enabled = True if ui_enabled else None

    class Handler(ApiHandler):
        pass

    Handler.config = config
    Handler.runner = runner
    Handler.ui_enabled = ui_enabled
    Handler.runtime_role = runtime_role
    Handler.web_install_enabled = web_install_enabled

    server = ThreadingHTTPServer((host, port), Handler)
    mode = "web UI" if ui_enabled else "core API"
    print(f"GP control plane {mode} listening on http://{host}:{port}")
    server.serve_forever()


def serve_core(config: AppConfig, host: str = "127.0.0.1", port: int = 8081) -> None:
    from ..core_server import serve_core as _serve_core

    _serve_core(config, host=host, port=port)


def serve_web_proxy(config: AppConfig, host: str, port: int, *, core_url: str) -> None:
    from ..proxy import serve_web_proxy as _serve_web_proxy

    _serve_web_proxy(config, host=host, port=port, core_url=core_url)


__all__ = [
    'BACKUP_STREAM_CHUNK_BYTES',
    'BACKUP_UPLOAD_MAX_BYTES',
    'DEFAULT_DISCOVERY_PROFILES',
    'DEFAULT_RUN_PREFERENCES',
    'DEFAULT_SETTINGS',
    'JSON_GET_ROUTE_PATHS',
    'JSON_HEAD_ROUTE_PATHS',
    'JSON_POST_ROUTE_PATHS',
    'JSON_REQUEST_MAX_BYTES',
    'MAX_BACKUP_UPLOAD_BYTES',
    'MAX_JSON_REQUEST_BYTES',
    'NDJSON_CONTENT_TYPE',
    'OPENAPI_JSON_CONTENT_TYPE',
    'SWAGGER_HTML_CONTENT_TYPE',
    'SWAGGER_PATHS',
    '_EVENT_CURSOR_LOCK',
    '_EVENT_CURSOR_STATE',
    '_ROOT_MANAGED_DISCOVERY_NAMES',
    'Any',
    'ApiHandler',
    'AppConfig',
    'AuthenticationError',
    'BaseHTTPRequestHandler',
    'EventsMixin',
    'GetMixin',
    'HTTPStatus',
    'HttpMixin',
    'JobRunner',
    'PasswordValidationError',
    'Path',
    'PostMixin',
    'RequestBodyTooLarge',
    'RuntimeBusyError',
    'ThreadingHTTPServer',
    '__version__',
    '_bounded_int',
    '_candidate_domain_index_payload',
    '_candidate_page_payload',
    '_clean_domain_list',
    '_clean_install_vault_create_response',
    '_clean_install_vault_public_metadata',
    '_clean_install_vault_restore_response',
    '_clear_stale_current_run',
    '_core_event_payloads',
    '_core_strategy_discovery_job_payload',
    '_current_run_latest_log_payload',
    '_event_cursor',
    '_event_fingerprint',
    '_event_payloads',
    '_event_sequence',
    '_events_response_payload',
    '_is_storage_unavailable_error',
    '_job_blockchecks_discovery',
    '_job_discovery',
    '_job_zapret_multi_domain_discovery',
    '_job_zapret_standard_discovery',
    '_latest_log_payload',
    '_log_event_payload',
    '_minimum_int',
    '_multipart_file_bytes',
    '_normalize_discovery_profile',
    '_normalize_run_preferences',
    '_optional_path',
    '_path_version',
    '_payload_bool',
    '_payload_domains',
    '_payload_int',
    '_payload_string_list',
    '_payload_timeout_seconds',
    '_preset_domains_payload',
    '_presets_payload',
    '_profile_name',
    '_query_bool',
    '_query_domains',
    '_query_int',
    '_query_one',
    '_query_str',
    '_recover_runtime_before_serve',
    '_release_info_payload',
    '_requires_verified_root_recovery',
    '_runs_event_payload',
    '_runs_page_payload',
    '_v2fly_categories_payload',
    '_v2fly_import_payload',
    '_v2fly_preview_payload',
    '_web_event_payloads',
    '_web_presets_payload',
    'active_job_lock_payload',
    'builtin_preset_sources',
    'campaign_lock_busy_message',
    'candidate_storage_version',
    'change_password',
    'check_blockchecks_install',
    'check_install_cached',
    'cleanup_nft_blockcheck_tables',
    'close_stale_running_runs',
    'core_api',
    'create_post_run_snapshot',
    'create_snapshot_if_idle',
    'delete_custom_preset',
    'delete_snapshot_if_idle',
    'delete_user_presets',
    'domain_sets',
    'error_payload',
    'export_nfconf',
    'fetch_v2fly_category_local',
    'fetch_v2fly_revision',
    'has_active_runtime',
    'hashlib',
    'health_payload',
    'import_snapshot_archive',
    'import_v2fly_preset',
    'index_html',
    'is_blockchecks_job',
    'json',
    'latest_log_tail',
    'list_bs_dns_pins',
    'list_v2fly_categories_local',
    'login',
    'mimetypes',
    'normalize_engine',
    'normalize_error_payload',
    'now_iso',
    'openapi_json_bytes',
    'parse_qs',
    'parse_v2fly_domains',
    'parse_v2fly_revision',
    'prepare_v2fly_local_storage',
    'preview_v2fly_preset',
    'read_candidate_domain_index',
    'read_candidate_page',
    'read_custom_preset_index',
    'read_custom_presets',
    'read_discovery_profiles',
    'read_preset_domains_page',
    'read_run_preferences',
    'read_run_settings',
    'read_runs',
    'read_service_settings',
    'read_settings',
    'read_state',
    'read_system_preset_index',
    'read_system_presets',
    'read_v2fly_catalog_cache',
    'read_v2fly_group_manifest',
    'recover_quarantined_process_run',
    'recover_registered_process_runs',
    'release_channel_info',
    'require_bearer_token',
    'restore_snapshot_if_idle',
    'route_for',
    'run_blockchecks_discovery',
    'run_multi_domain_discovery',
    'run_standard_discovery',
    'save_custom_preset',
    'save_custom_presets',
    'save_discovery_profiles',
    'save_run_preferences',
    'save_run_settings',
    'save_settings',
    'save_system_preset',
    'serve',
    'serve_core',
    'serve_web_proxy',
    'service_api',
    'set_preset_domain_enabled',
    'snapshot_file_path',
    'status_payload',
    'stop_blockchecks',
    'swagger_ui_html',
    'threading',
    'time',
    'update_state',
    'urlparse',
    'web_event_changes',
    'web_json_get_payload',
    'web_json_post_response',
    'write_v2fly_catalog_cache',
]
