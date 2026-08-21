#!/bin/sh
# One-time legacy transition helper. It is intentionally not a GP runtime service.
set -eu
umask 077

# This payload is only entered from legacy-bootstrap-launcher.sh after that
# launcher has copied and verified it in a root-owned staging directory.
readonly TRUSTED_PATH='/usr/sbin:/usr/bin:/sbin:/bin'
PATH="$TRUSTED_PATH"
export PATH

readonly CANONICAL_UPSTREAM='https://github.com/balbomush/GP-access-control-plane.git'
readonly ROOT_HELPER='/usr/local/libexec/gp-control-plane/gp-root-helper'
readonly ROOT_HELPER_CONFIG='/etc/default/gp-control-plane-root-helper'
readonly SUDOERS_PATH='/etc/sudoers.d/gp-control-plane-root-helper'
readonly RUN_REGISTRY_DIR='/run/gp-control-plane/runs'
readonly INSTALL_PROFILE='/etc/default/gp-control-plane-install-profile'
readonly CORE_ENV_FILE='/etc/default/gp-control-plane-core'
readonly WEB_ENV_FILE='/etc/default/gp-control-plane-web'
readonly CORE_SERVICE='gp-control-plane-core.service'
readonly WEB_SERVICE='gp-control-plane-web.service'
readonly CORE_UNIT='/etc/systemd/system/gp-control-plane-core.service'
readonly WEB_UNIT='/etc/systemd/system/gp-control-plane-web.service'
readonly JOURNAL_ROOT='/var/lib/gp-control-plane/legacy-bootstrap'
readonly STAGE_ROOT='/var/lib/gp-control-plane/legacy-bootstrap/payloads'
readonly RM='/usr/bin/rm'
readonly RMDIR='/usr/bin/rmdir'

BACKUP_READY=0
COMMITTED=0
ROLLBACK_RUNNING=0
TERMINAL_STATUS_WRITTEN=0
ERROR_PHASE_WRITTEN=0
PAYLOAD_LIFECYCLE_READY=0

usage() {
  printf '%s\n' 'Usage: legacy-bootstrap.sh --bootstrap-sha SHA256 --candidate-ref refs/heads/dev --candidate-sha SHA40' >&2
}

fail() {
  journal_nonterminal_error
  printf '%s\n' "legacy-bootstrap: $1" >&2
  exit "${2:-1}"
}

is_sha256() {
  case "${1:-}" in ''|*[!0-9a-f]*) return 1 ;; esac
  [ "${#1}" -eq 64 ]
}

is_commit_sha() {
  case "${1:-}" in ''|*[!0-9a-f]*) return 1 ;; esac
  [ "${#1}" -eq 40 ]
}

require_trusted_stage() {
  staged_directory="${LEGACY_BOOTSTRAP_STAGED_DIR:-}"
  staged_suffix="${staged_directory#"$STAGE_ROOT/payload-$BOOTSTRAP_SHA-"}"
  case "$staged_suffix" in ''|*[!0-9]*) fail 'trusted launcher staging directory is invalid' 2 ;; esac
  [ "$staged_directory" = "$STAGE_ROOT/payload-$BOOTSTRAP_SHA-$staged_suffix" ] \
    && [ "$0" = "$staged_directory/legacy-bootstrap.sh" ] \
    || fail 'trusted launcher staging path is invalid' 2
  [ "${LEGACY_BOOTSTRAP_STAGED_PATH:-}" = "$0" ] \
    || fail 'legacy bootstrap payload must be invoked by the trusted launcher' 2
  [ "${LEGACY_BOOTSTRAP_STAGED_SHA:-}" = "$BOOTSTRAP_SHA" ] \
    || fail 'trusted launcher SHA transport is invalid' 2
  [ -d "$staged_directory" ] && [ ! -L "$staged_directory" ] \
    && [ "$(/usr/bin/stat -c '%u:%g:%a' -- "$staged_directory" 2>/dev/null || true)" = '0:0:700' ] \
    || fail 'trusted launcher staging directory is not a root-owned mode 0700 directory' 2
  [ -f "$0" ] && [ ! -L "$0" ] \
    && [ "$(/usr/bin/stat -c '%u:%g:%a' -- "$0" 2>/dev/null || true)" = '0:0:700' ] \
    || fail 'trusted launcher staged payload is not a root-owned mode 0700 regular file' 2
  actual_staged_sha="$(/usr/bin/sha256sum -- "$0" | /usr/bin/awk '{print $1}')"
  [ "$actual_staged_sha" = "$BOOTSTRAP_SHA" ] \
    || fail 'trusted launcher staged payload SHA256 does not match --bootstrap-sha' 2
}

cleanup_staged_payload() {
  staged_directory="${LEGACY_BOOTSTRAP_STAGED_DIR:-}"
  staged_suffix="${staged_directory#"$STAGE_ROOT/payload-$BOOTSTRAP_SHA-"}"
  case "$staged_suffix" in ''|*[!0-9]*) return 1 ;; esac
  [ "$staged_directory" = "$STAGE_ROOT/payload-$BOOTSTRAP_SHA-$staged_suffix" ] \
    && [ "$0" = "$staged_directory/legacy-bootstrap.sh" ] \
    && [ "${LEGACY_BOOTSTRAP_STAGED_PATH:-}" = "$0" ] \
    && [ -d "$staged_directory" ] && [ ! -L "$staged_directory" ] \
    && [ "$(/usr/bin/stat -c '%u:%g:%a' -- "$staged_directory" 2>/dev/null || true)" = '0:0:700' ] \
    || return 1
  "$RM" -f -- "$0" && "$RMDIR" -- "$staged_directory"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command is unavailable: $1"
}

journal_phase() {
  # This journal intentionally records fixed lifecycle tokens only, never config
  # values, command output, or other data that might contain a secret.
  printf 'phase=%s\n' "$1" >> "$JOURNAL_FILE"
}

journal_value() {
  printf '%s=%s\n' "$1" "$2" >> "$JOURNAL_FILE"
}

journal_nonterminal_error() {
  [ "${JOURNAL_FILE:-}" ] || return 0
  [ "${ERROR_PHASE_WRITTEN:-0}" -eq 0 ] || return 0
  journal_phase error
  ERROR_PHASE_WRITTEN=1
}

journal_terminal_failure() {
  terminal_status="$1"
  terminal_error="$2"
  [ "$TERMINAL_STATUS_WRITTEN" -eq 0 ] || return 0
  journal_value status "$terminal_status"
  journal_value error "$terminal_error"
  TERMINAL_STATUS_WRITTEN=1
}

safe_parent_chain() {
  safe_parent="$1"
  while :; do
    [ -d "$safe_parent" ] && [ ! -L "$safe_parent" ] || return 1
    [ "$(readlink -f "$safe_parent" 2>/dev/null || true)" = "$safe_parent" ] || return 1
    [ "$(stat -c '%u' "$safe_parent" 2>/dev/null || true)" = 0 ] || return 1
    safe_mode="$(stat -c '%a' "$safe_parent" 2>/dev/null || true)"
    case "$safe_mode" in *[2367][0-7]|*[0-7][2367]) return 1 ;; esac
    [ "$safe_parent" = / ] && return 0
    safe_parent="$(dirname "$safe_parent")"
  done
}

safe_file_or_absent() {
  safe_target="$1"
  safe_parent_chain "$(dirname "$safe_target")" || return 1
  if [ -e "$safe_target" ] || [ -L "$safe_target" ]; then
    [ -f "$safe_target" ] && [ ! -L "$safe_target" ] || return 1
    [ "$(readlink -f "$safe_target" 2>/dev/null || true)" = "$safe_target" ] || return 1
    [ "$(stat -c '%u' "$safe_target" 2>/dev/null || true)" = 0 ] || return 1
    safe_mode="$(stat -c '%a' "$safe_target" 2>/dev/null || true)"
    case "$safe_mode" in *[2367][0-7]|*[0-7][2367]) return 1 ;; esac
  fi
}

unit_is_allowed_mask_link() {
  unit_target="$1"
  case "$unit_target" in
    "$CORE_UNIT"|"$WEB_UNIT") ;;
    *) return 1 ;;
  esac
  safe_parent_chain "$(dirname "$unit_target")" || return 1
  [ -L "$unit_target" ] || return 1
  [ "$(readlink -- "$unit_target" 2>/dev/null || true)" = /dev/null ] || return 1
  [ "$(readlink -f -- "$unit_target" 2>/dev/null || true)" = /dev/null ] || return 1
  [ -c /dev/null ]
}

safe_unit_or_allowed_mask_link() {
  unit_target="$1"
  if [ -L "$unit_target" ]; then
    unit_is_allowed_mask_link "$unit_target"
  else
    safe_file_or_absent "$unit_target"
  fi
}

safe_directory_or_absent() {
  safe_target="$1"
  safe_parent_chain "$(dirname "$safe_target")" || return 1
  if [ -e "$safe_target" ] || [ -L "$safe_target" ]; then
    [ -d "$safe_target" ] && [ ! -L "$safe_target" ] || return 1
    [ "$(readlink -f "$safe_target" 2>/dev/null || true)" = "$safe_target" ] || return 1
    [ "$(stat -c '%u' "$safe_target" 2>/dev/null || true)" = 0 ] || return 1
    safe_mode="$(stat -c '%a' "$safe_target" 2>/dev/null || true)"
    case "$safe_mode" in *[2367][0-7]|*[0-7][2367]) return 1 ;; esac
  fi
}

install_profile_is_absent() {
  [ ! -e "$INSTALL_PROFILE" ] && [ ! -L "$INSTALL_PROFILE" ]
}

core_env_is_absent() {
  [ ! -e "$CORE_ENV_FILE" ] && [ ! -L "$CORE_ENV_FILE" ]
}

require_existing_install_profile() {
  [ -f "$INSTALL_PROFILE" ] && [ ! -L "$INSTALL_PROFILE" ] \
    && [ "$(stat -c '%u:%g:%a' "$INSTALL_PROFILE" 2>/dev/null || true)" = '0:0:600' ] \
    || fail 'existing install profile must be a root-owned mode 0600 regular file'
}

validate_run_registry_path() {
  if [ -e "$RUN_REGISTRY_DIR" ] || [ -L "$RUN_REGISTRY_DIR" ]; then
    safe_directory_or_absent "$RUN_REGISTRY_DIR" || fail 'unsafe root helper run registry path'
    return 0
  fi
  run_registry_parent="$(dirname "$RUN_REGISTRY_DIR")"
  if [ -e "$run_registry_parent" ] || [ -L "$run_registry_parent" ]; then
    safe_parent_chain "$run_registry_parent" || fail 'unsafe root helper run registry parent'
  else
    safe_parent_chain "$(dirname "$run_registry_parent")" || fail 'unsafe root helper run registry grandparent'
  fi
}

validate_transition_surface() {
  safe_file_or_absent "$ROOT_HELPER" || fail 'unsafe root helper path'
  safe_file_or_absent "$ROOT_HELPER_CONFIG" || fail 'unsafe root helper config path'
  validate_run_registry_path
  safe_file_or_absent "$INSTALL_PROFILE" || fail 'unsafe install profile path'
  safe_file_or_absent "$CORE_ENV_FILE" || fail 'unsafe core service metadata path'
  safe_unit_or_allowed_mask_link "$CORE_UNIT" || fail 'unsafe core unit path'
  safe_unit_or_allowed_mask_link "$WEB_UNIT" || fail 'unsafe web unit path'
}

ensure_journal_root() {
  safe_parent_chain "$(dirname "$JOURNAL_ROOT")" || fail 'unsafe legacy bootstrap journal parent'
  if [ -e "$JOURNAL_ROOT" ] || [ -L "$JOURNAL_ROOT" ]; then
    [ -d "$JOURNAL_ROOT" ] && [ ! -L "$JOURNAL_ROOT" ] || fail 'unsafe legacy bootstrap journal path'
  else
    install -d -m 0700 -o root -g root "$JOURNAL_ROOT"
  fi
  [ "$(stat -c '%u:%g:%a' "$JOURNAL_ROOT" 2>/dev/null || true)" = '0:0:700' ] \
    || fail 'legacy bootstrap journal must be root-owned mode 0700'
}

snapshot_file() {
  snapshot_target="$1"
  snapshot_name="$2"
  if [ -e "$snapshot_target" ]; then
    cp -p "$snapshot_target" "$BACKUP_DIR/$snapshot_name"
    snapshot_mode="$(stat -c '%a' "$snapshot_target")"
    printf '%s|%s|file|%s\n' "$snapshot_name" "$snapshot_target" "$snapshot_mode" >> "$BACKUP_MANIFEST"
  else
    printf '%s|%s|absent-file|-\n' "$snapshot_name" "$snapshot_target" >> "$BACKUP_MANIFEST"
  fi
}

snapshot_directory() {
  snapshot_target="$1"
  snapshot_name="$2"
  if [ -e "$snapshot_target" ]; then
    cp -a "$snapshot_target" "$BACKUP_DIR/$snapshot_name"
    snapshot_mode="$(stat -c '%a' "$snapshot_target")"
    printf '%s|%s|directory|%s\n' "$snapshot_name" "$snapshot_target" "$snapshot_mode" >> "$BACKUP_MANIFEST"
  else
    printf '%s|%s|absent-directory|-\n' "$snapshot_name" "$snapshot_target" >> "$BACKUP_MANIFEST"
  fi
}

snapshot_transition_surface() {
  snapshot_file "$ROOT_HELPER" root-helper
  snapshot_file "$ROOT_HELPER_CONFIG" root-helper-config
  snapshot_directory "$RUN_REGISTRY_DIR" run-registry
  snapshot_file "$INSTALL_PROFILE" install-profile
  snapshot_file "$CORE_ENV_FILE" core-env
}

snapshot_service_state() {
  snapshot_unit="$1"
  snapshot_name="$2"
  case "$snapshot_unit" in
    "$CORE_SERVICE") snapshot_unit_path="$CORE_UNIT" ;;
    "$WEB_SERVICE") snapshot_unit_path="$WEB_UNIT" ;;
    *) return 1 ;;
  esac
  if [ ! -e "$snapshot_unit_path" ] && [ ! -L "$snapshot_unit_path" ]; then
    printf '%s|missing|missing\n' "$snapshot_name" >> "$SERVICE_MANIFEST"
    return 0
  fi
  if [ -L "$snapshot_unit_path" ]; then
    unit_is_allowed_mask_link "$snapshot_unit_path" || return 1
  else
    safe_file_or_absent "$snapshot_unit_path" || return 1
  fi
  if systemctl is-active --quiet "$snapshot_unit"; then snapshot_active=active; else snapshot_active=inactive; fi
  snapshot_enabled="$(systemctl is-enabled "$snapshot_unit" 2>/dev/null || true)"
  case "$snapshot_enabled" in
    enabled|enabled-runtime|disabled|disabled-runtime|masked|masked-runtime) ;;
    *) return 1 ;;
  esac
  case "$snapshot_enabled:$snapshot_active" in
    masked:active|masked-runtime:active) return 1 ;;
  esac
  printf '%s|%s|%s\n' "$snapshot_name" "$snapshot_active" "$snapshot_enabled" >> "$SERVICE_MANIFEST"
}

snapshot_services() {
  snapshot_service_state "$CORE_SERVICE" core && snapshot_service_state "$WEB_SERVICE" web
}

restore_transition_surface() {
  restore_ok=0
  while IFS='|' read -r restore_name restore_target restore_state restore_mode; do
    case "$restore_state" in
      file)
        safe_file_or_absent "$restore_target" || { restore_ok=1; continue; }
        [ -f "$BACKUP_DIR/$restore_name" ] && [ ! -L "$BACKUP_DIR/$restore_name" ] || { restore_ok=1; continue; }
        install -m "$restore_mode" -o root -g root "$BACKUP_DIR/$restore_name" "$restore_target" || restore_ok=1
        ;;
      absent-file)
        safe_file_or_absent "$restore_target" || { restore_ok=1; continue; }
        rm -f "$restore_target" || restore_ok=1
        ;;
      directory)
        safe_directory_or_absent "$restore_target" || { restore_ok=1; continue; }
        [ -d "$BACKUP_DIR/$restore_name" ] && [ ! -L "$BACKUP_DIR/$restore_name" ] || { restore_ok=1; continue; }
        rm -rf "$restore_target" || { restore_ok=1; continue; }
        cp -a "$BACKUP_DIR/$restore_name" "$restore_target" || restore_ok=1
        ;;
      absent-directory)
        safe_directory_or_absent "$restore_target" || { restore_ok=1; continue; }
        rm -rf "$restore_target" || restore_ok=1
        ;;
      *) restore_ok=1 ;;
    esac
  done < "$BACKUP_MANIFEST"
  return "$restore_ok"
}

restore_one_service_state() {
  restore_unit="$1"
  restore_active="$2"
  restore_enabled="$3"
  case "$restore_enabled:$restore_active" in
    masked:active|masked-runtime:active) return 1 ;;
  esac
  case "$restore_enabled" in
    enabled) systemctl enable "$restore_unit" >/dev/null 2>&1 || return 1 ;;
    enabled-runtime) systemctl enable --runtime "$restore_unit" >/dev/null 2>&1 || return 1 ;;
    disabled) systemctl disable "$restore_unit" >/dev/null 2>&1 || return 1 ;;
    disabled-runtime) systemctl disable --runtime "$restore_unit" >/dev/null 2>&1 || return 1 ;;
    masked) systemctl mask "$restore_unit" >/dev/null 2>&1 || return 1 ;;
    masked-runtime) systemctl mask --runtime "$restore_unit" >/dev/null 2>&1 || return 1 ;;
    missing) return 0 ;;
    *) return 1 ;;
  esac
  case "$restore_enabled" in
    masked|masked-runtime)
      [ "$restore_active" = inactive ] || return 1
      ;;
    *)
      case "$restore_active" in
        active) systemctl start "$restore_unit" >/dev/null 2>&1 || return 1 ;;
        inactive) systemctl stop "$restore_unit" >/dev/null 2>&1 || return 1 ;;
        *) return 1 ;;
      esac
      ;;
  esac
  restore_actual_enabled="$(systemctl is-enabled "$restore_unit" 2>/dev/null || true)"
  [ "$restore_actual_enabled" = "$restore_enabled" ] || return 1
  if systemctl is-active --quiet "$restore_unit"; then restore_actual_active=active; else restore_actual_active=inactive; fi
  [ "$restore_actual_active" = "$restore_active" ] || return 1
  if [ "$restore_enabled" = masked ] && [ -L "/etc/systemd/system/$restore_unit" ]; then
    unit_is_allowed_mask_link "/etc/systemd/system/$restore_unit" || return 1
  fi
}

restore_service_state() {
  restore_services_ok=0
  systemctl daemon-reload >/dev/null 2>&1 || restore_services_ok=1
  while IFS='|' read -r restore_name restore_active restore_enabled; do
    case "$restore_name" in
      core) restore_one_service_state "$CORE_SERVICE" "$restore_active" "$restore_enabled" || restore_services_ok=1 ;;
      web) restore_one_service_state "$WEB_SERVICE" "$restore_active" "$restore_enabled" || restore_services_ok=1 ;;
      *) restore_services_ok=1 ;;
    esac
  done < "$SERVICE_MANIFEST"
  return "$restore_services_ok"
}

rollback_after_failure() {
  [ "$ROLLBACK_RUNNING" -eq 0 ] || return 1
  ROLLBACK_RUNNING=1
  journal_phase rollback-started
  if restore_transition_surface && restore_service_state; then
    journal_phase rolled-back
    return 0
  fi
  journal_phase rollback-failed
  return 1
}

on_exit() {
  exit_status="$1"
  trap - 0
  if [ "$exit_status" -ne 0 ] && [ "$BACKUP_READY" -eq 1 ] && [ "$COMMITTED" -eq 0 ]; then
    set +e
    if rollback_after_failure; then
      journal_terminal_failure failed rollback-succeeded
    else
      journal_terminal_failure error rollback-failed
    fi
  elif [ "$exit_status" -ne 0 ]; then
    journal_terminal_failure failed rollback-not-required
  fi
  return "$exit_status"
}

root_wrapper_on_exit() {
  payload_status="$1"
  trap - 0
  if [ "$payload_status" -ne 0 ] && [ "$PAYLOAD_LIFECYCLE_READY" -eq 1 ]; then
    if on_exit "$payload_status"; then
      payload_status=0
    else
      payload_status="$?"
    fi
  fi
  if ! cleanup_staged_payload; then
    printf '%s\n' 'legacy-bootstrap: cannot remove root staged payload' >&2
    if [ "$payload_status" -eq 0 ] && [ "$PAYLOAD_LIFECYCLE_READY" -eq 1 ]; then
      journal_nonterminal_error
      set +e
      on_exit 1
    fi
    exit 1
  fi
  if [ "$payload_status" -eq 0 ] && [ "$PAYLOAD_LIFECYCLE_READY" -eq 1 ]; then
    journal_phase committed
    journal_value status success
    TERMINAL_STATUS_WRITTEN=1
    COMMITTED=1
    printf '%s\n' "legacy-bootstrap: committed transaction $TRANSACTION_ID"
  fi
  exit "$payload_status"
}

clean_git() {
  env -i PATH='/usr/sbin:/usr/bin:/sbin:/bin' HOME=/nonexistent GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0 git "$@"
}

read_install_profile_value() {
  profile_key="$1"
  profile_line="$(awk -F= -v key="$profile_key" '$1 == key { print; count++ } END { exit count == 1 ? 0 : 1 }' "$INSTALL_PROFILE")" || return 1
  profile_value="${profile_line#*=}"
  case "$profile_value" in
    \') profile_value= ;;
    \'*\') profile_value="${profile_value#\'}"; profile_value="${profile_value%\'}" ;;
    *) return 1 ;;
  esac
  case "$profile_value" in *\'*) return 1 ;; esac
  printf '%s\n' "$profile_value"
}

require_root_owned_fixed_unit() {
  fixed_unit="$1"
  fixed_label="$2"
  [ -f "$fixed_unit" ] && [ ! -L "$fixed_unit" ] \
    && [ "$(stat -c '%u' "$fixed_unit" 2>/dev/null || true)" = 0 ] \
    || fail "legacy bootstrap requires a root-owned fixed $fixed_label unit"
}

read_fixed_unit_value() {
  unit_path="$1"
  unit_key="$2"
  unit_value="$(awk -v key="$unit_key" '
    /^\[Service\][[:space:]]*$/ { in_service = 1; next }
    /^\[/ { in_service = 0 }
    in_service && index($0, key "=") == 1 {
      found++
      value = substr($0, length(key) + 2)
    }
    END { if (found == 1 && value != "") print value; else exit 1 }
  ' "$unit_path")" || return 1
  printf '%s\n' "$unit_value"
}

read_core_env_value() {
  core_env_key="$1"
  core_env_line="$(awk -F= -v key="$core_env_key" '$1 == key { print; count++ } END { exit count == 1 ? 0 : 1 }' "$CORE_ENV_FILE")" || return 1
  core_env_value="${core_env_line#*=}"
  case "$core_env_value" in
    \') core_env_value= ;;
    \'*\') core_env_value="${core_env_value#\'}"; core_env_value="${core_env_value%\'}" ;;
    *) return 1 ;;
  esac
  case "$core_env_value" in *\'*) return 1 ;; esac
  printf '%s\n' "$core_env_value"
}

validate_legacy_install_user() {
  legacy_user="$1"
  case "$legacy_user" in
    root|''|*[!A-Za-z0-9_-]*) fail 'legacy service unit User is unsafe' ;;
  esac
  getent passwd "$legacy_user" >/dev/null 2>&1 || fail 'legacy service unit User does not exist'
}

validate_legacy_install_directory() {
  legacy_dir="$1"
  case "$legacy_dir" in
    /*) ;;
    *) fail 'legacy service unit WorkingDirectory is not absolute' ;;
  esac
  case "$legacy_dir" in
    *'//'*|*'/./'*|*/'.'|*'/../'*|*/'..'|*[!A-Za-z0-9._/-]*) fail 'legacy service unit WorkingDirectory is unsafe' ;;
  esac
  validate_canonical_install_directory "$legacy_dir" 'legacy service unit WorkingDirectory'
  legacy_state_dir="$legacy_dir/build/state"
  [ -d "$legacy_state_dir" ] && [ ! -L "$legacy_state_dir" ] \
    && [ "$(readlink -f "$legacy_state_dir" 2>/dev/null || true)" = "$legacy_state_dir" ] \
    || fail 'legacy service unit state directory is not a canonical directory'
}

validate_legacy_core_endpoint() {
  legacy_core_host="$1"
  legacy_core_port="$2"
  case "$legacy_core_host" in
    127.0.0.1|0.0.0.0) ;;
    *) fail 'legacy Core unit host is unsafe or unsupported' ;;
  esac
  case "$legacy_core_port" in
    ''|0|*[!0-9]*|0[0-9]*) fail 'legacy Core unit port is unsafe' ;;
  esac
  [ "$legacy_core_port" -le 65535 ] || fail 'legacy Core unit port is unsafe'
}

validate_legacy_web_endpoint() {
  legacy_web_host="$1"
  legacy_web_port="$2"
  case "$legacy_web_host" in
    127.0.0.1|0.0.0.0) ;;
    *) fail 'legacy Web unit host is unsafe or unsupported' ;;
  esac
  case "$legacy_web_port" in
    ''|0|*[!0-9]*|0[0-9]*) fail 'legacy Web unit port is unsafe' ;;
  esac
  [ "$legacy_web_port" -le 65535 ] || fail 'legacy Web unit port is unsafe'
}

derive_legacy_core_endpoint() {
  core_exec_start="$(read_fixed_unit_value "$CORE_UNIT" ExecStart)" || fail 'legacy Core unit ExecStart is missing or ambiguous'
  core_prefix="$LEGACY_INSTALL_DIR/.venv/bin/gp-control-plane core --host "
  case "$core_exec_start" in
    "$core_prefix"*) core_endpoint_rest="${core_exec_start#"$core_prefix"}" ;;
    *) fail 'legacy Core unit ExecStart is unsupported' ;;
  esac
  case "$core_endpoint_rest" in *' '*) LEGACY_CORE_HOST="${core_endpoint_rest%% *}" ;; *) fail 'legacy Core unit ExecStart is unsupported' ;; esac
  core_endpoint_rest="${core_endpoint_rest#"$LEGACY_CORE_HOST "}"
  case "$core_endpoint_rest" in --port\ *) LEGACY_CORE_PORT="${core_endpoint_rest#--port }" ;; *) fail 'legacy Core unit ExecStart is unsupported' ;; esac
  case "$LEGACY_CORE_PORT" in *' '*) fail 'legacy Core unit ExecStart is unsupported' ;; esac
  validate_legacy_core_endpoint "$LEGACY_CORE_HOST" "$LEGACY_CORE_PORT"
  LEGACY_CORE_URL="http://$LEGACY_CORE_HOST:$LEGACY_CORE_PORT"
}

derive_legacy_web_endpoint() {
  require_root_owned_fixed_unit "$WEB_UNIT" Web
  web_exec_start="$(read_fixed_unit_value "$WEB_UNIT" ExecStart)" || fail 'legacy Web unit ExecStart is missing or ambiguous'
  web_prefix="$LEGACY_INSTALL_DIR/.venv/bin/gp-control-plane web --host "
  case "$web_exec_start" in
    "$web_prefix"*) web_endpoint_rest="${web_exec_start#"$web_prefix"}" ;;
    *) fail 'legacy Web unit ExecStart is unsupported' ;;
  esac
  case "$web_endpoint_rest" in *' '*) LEGACY_WEB_HOST="${web_endpoint_rest%% *}" ;; *) fail 'legacy Web unit ExecStart is unsupported' ;; esac
  web_endpoint_rest="${web_endpoint_rest#"$LEGACY_WEB_HOST "}"
  case "$web_endpoint_rest" in --port\ *) LEGACY_WEB_PORT="${web_endpoint_rest#--port }" ;; *) fail 'legacy Web unit ExecStart is unsupported' ;; esac
  case "$LEGACY_WEB_PORT" in
    *' '*)
      web_extra_args="${LEGACY_WEB_PORT#* }"
      LEGACY_WEB_PORT="${LEGACY_WEB_PORT%% *}"
      [ "$web_extra_args" = "--core-url $LEGACY_CORE_URL" ] || fail 'legacy Web unit ExecStart is unsupported'
      ;;
  esac
  validate_legacy_web_endpoint "$LEGACY_WEB_HOST" "$LEGACY_WEB_PORT"
}

validate_canonical_install_directory() {
  install_dir="$1"
  install_label="$2"
  case "$install_dir" in
    /*) ;;
    *) fail "$install_label is not absolute" ;;
  esac
  case "$install_dir" in
    *'//'*|*'/./'*|*/'.'|*'/../'*|*/'..'|*[!A-Za-z0-9._/-]*) fail "$install_label is unsafe" ;;
  esac
  [ -d "$install_dir" ] && [ ! -L "$install_dir" ] \
    && [ "$(readlink -f "$install_dir" 2>/dev/null || true)" = "$install_dir" ] \
    || fail "$install_label is not a canonical directory"
}

write_legacy_install_profile() {
  cat > "$STAGED_INSTALL_PROFILE" <<PROFILE
# Managed by GP Access Control Plane legacy bootstrap. Reconfigure only through the explicit installer.
GP_INSTALL_USER='$LEGACY_INSTALL_USER'
GP_INSTALL_DIR='$LEGACY_INSTALL_DIR'
GP_STATE_DIR='$LEGACY_INSTALL_DIR/build/state'
GP_SERVICE_NAME='gp-control-plane-web.service'
GP_CORE_SERVICE_NAME='gp-control-plane-core.service'
GP_INSTALL_WEB='$LEGACY_INSTALL_WEB'
$LEGACY_WEB_PROFILE
GP_WEB_ENV_FILE='/etc/default/gp-control-plane-web'
GP_CORE_HOST='$LEGACY_CORE_HOST'
GP_CORE_PORT='$LEGACY_CORE_PORT'
GP_CORE_URL='$LEGACY_CORE_URL'
GP_CORE_ENV_FILE='/etc/default/gp-control-plane-core'
GP_ZAPRET_DIR='/opt/zapret2'
GP_ROOT_HELPER_PATH='/usr/local/libexec/gp-control-plane/gp-root-helper'
GP_ROOT_HELPER_CONFIG='/etc/default/gp-control-plane-root-helper'
GP_ROOT_HELPER_RUN_DIR='/run/gp-control-plane/runs'
GP_SUDOERS_PATH='/etc/sudoers.d/gp-control-plane-root-helper'
GP_SERVICE_MEMORY_HIGH='512M'
GP_SERVICE_MEMORY_MAX='1G'
PROFILE
  chmod 0600 "$STAGED_INSTALL_PROFILE"
}

prepare_install_profile() {
  if install_profile_is_absent; then
    if [ -e "$CORE_UNIT" ] || [ -L "$CORE_UNIT" ]; then
      require_root_owned_fixed_unit "$CORE_UNIT" Core
      [ -f "$CORE_ENV_FILE" ] && [ ! -L "$CORE_ENV_FILE" ] \
        && [ "$(stat -c '%u:%g' "$CORE_ENV_FILE" 2>/dev/null || true)" = '0:0' ] \
        || fail 'legacy Core service metadata must be a root-owned regular file'
      LEGACY_INSTALL_USER="$(read_fixed_unit_value "$CORE_UNIT" User)" || fail 'legacy Core unit User is missing or ambiguous'
      LEGACY_INSTALL_DIR="$(read_fixed_unit_value "$CORE_UNIT" WorkingDirectory)" || fail 'legacy Core unit WorkingDirectory is missing or ambiguous'
      LEGACY_ENV_INSTALL_DIR="$(read_core_env_value GP_INSTALL_DIR)" || fail 'legacy Core env GP_INSTALL_DIR is missing or ambiguous'
      LEGACY_ENV_STATE_DIR="$(read_core_env_value GP_STATE_DIR)" || fail 'legacy Core env GP_STATE_DIR is missing or ambiguous'
      LEGACY_INSTALL_WEB="$(read_core_env_value GP_INSTALL_WEB)" || fail 'legacy Core env GP_INSTALL_WEB is missing or ambiguous'
      [ "$LEGACY_ENV_INSTALL_DIR" = "$LEGACY_INSTALL_DIR" ] || fail 'legacy Core env install directory does not match Core unit'
      [ "$LEGACY_ENV_STATE_DIR" = "$LEGACY_INSTALL_DIR/build/state" ] || fail 'legacy Core env state directory does not match Core unit'
      case "$LEGACY_INSTALL_WEB" in on|off) ;; *) fail 'legacy Core env topology is unsafe' ;; esac
      derive_legacy_core_endpoint
      if [ "$LEGACY_INSTALL_WEB" = on ]; then
        derive_legacy_web_endpoint
        LEGACY_WEB_PROFILE="GP_WEB_HOST='$LEGACY_WEB_HOST'
GP_WEB_PORT='$LEGACY_WEB_PORT'"
      else
        LEGACY_WEB_PROFILE=''
      fi
    else
      require_root_owned_fixed_unit "$WEB_UNIT" Web
      LEGACY_INSTALL_USER="$(read_fixed_unit_value "$WEB_UNIT" User)" || fail 'legacy Web unit User is missing or ambiguous'
      LEGACY_INSTALL_DIR="$(read_fixed_unit_value "$WEB_UNIT" WorkingDirectory)" || fail 'legacy Web unit WorkingDirectory is missing or ambiguous'
      LEGACY_INSTALL_WEB=on
      LEGACY_CORE_HOST=127.0.0.1
      LEGACY_CORE_PORT=8081
      LEGACY_CORE_URL='http://127.0.0.1:8081'
      derive_legacy_web_endpoint
      LEGACY_WEB_PROFILE="GP_WEB_HOST='$LEGACY_WEB_HOST'
GP_WEB_PORT='$LEGACY_WEB_PORT'"
    fi
    validate_legacy_install_user "$LEGACY_INSTALL_USER"
    validate_legacy_install_directory "$LEGACY_INSTALL_DIR"
    PROFILE_ACTION=create
    write_legacy_install_profile
    baseline_dir="$LEGACY_INSTALL_DIR"
    return 0
  fi

  require_existing_install_profile
  PROFILE_ACTION=preserve
  baseline_dir="$(read_install_profile_value GP_INSTALL_DIR)" || fail 'installed profile has no unambiguous GP_INSTALL_DIR'
  validate_canonical_install_directory "$baseline_dir" 'installed profile GP_INSTALL_DIR'
}

prepare_core_env() {
  if core_env_is_absent; then
    CORE_ENV_ACTION=create
    install -m 0600 -o root -g root /dev/null "$STAGED_CORE_ENV_FILE"
    return 0
  fi

  [ -f "$CORE_ENV_FILE" ] && [ ! -L "$CORE_ENV_FILE" ] \
    && [ "$(stat -c '%u:%g' "$CORE_ENV_FILE" 2>/dev/null || true)" = '0:0' ] \
    || fail 'existing core service metadata must be a root-owned regular file'
  CORE_ENV_ACTION=preserve
}

write_normalized_root_helper_config() {
  cat > "$STAGED_ROOT_HELPER_CONFIG" <<CONFIG
ZAPRET_DIR='/opt/zapret2'
GP_ROOT_HELPER_RUN_DIR='/run/gp-control-plane/runs'
CONFIG
  chmod 0644 "$STAGED_ROOT_HELPER_CONFIG"
}

ensure_run_registry() {
  run_registry_parent="$(dirname "$RUN_REGISTRY_DIR")"
  if [ ! -e "$run_registry_parent" ] && [ ! -L "$run_registry_parent" ]; then
    install -d -m 0755 -o root -g root "$run_registry_parent"
  fi
  safe_parent_chain "$run_registry_parent" || fail 'unsafe root helper run registry parent'
  safe_directory_or_absent "$RUN_REGISTRY_DIR" || fail 'unsafe root helper run registry path'
  install -d -m 0750 -o root -g root "$RUN_REGISTRY_DIR"
  [ "$(stat -c '%u:%g:%a' "$RUN_REGISTRY_DIR" 2>/dev/null || true)" = '0:0:750' ] \
    || fail 'root helper run registry must be root-owned mode 0750'
}

[ "$#" -eq 6 ] || { usage; exit 2; }
[ "$1" = --bootstrap-sha ] && [ "$3" = --candidate-ref ] && [ "$5" = --candidate-sha ] || { usage; exit 2; }
BOOTSTRAP_SHA="$2"
CANDIDATE_REF="$4"
CANDIDATE_SHA="$6"
is_sha256 "$BOOTSTRAP_SHA" || fail 'bootstrap SHA256 must be exactly 64 lowercase hexadecimal characters' 2
[ "$CANDIDATE_REF" = refs/heads/dev ] || fail 'candidate ref must be refs/heads/dev' 2
is_commit_sha "$CANDIDATE_SHA" || fail 'candidate SHA must be exactly 40 lowercase hexadecimal characters' 2

trap 'root_wrapper_on_exit $?' 0
require_trusted_stage
[ "$(/usr/bin/id -u)" -eq 0 ] || fail 'trusted launcher did not start the payload as root'
require_command git
require_command install
require_command readlink
require_command stat
require_command systemctl
require_command bash
require_command getent

validate_transition_surface
ensure_journal_root
require_command flock
exec 9>"$JOURNAL_ROOT/bootstrap.lock"
flock -n -x 9 || fail 'another legacy bootstrap transaction is active'
TRANSACTION_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
TRANSACTION_DIR="$JOURNAL_ROOT/$TRANSACTION_ID"
[ ! -e "$TRANSACTION_DIR" ] && [ ! -L "$TRANSACTION_DIR" ] || fail 'transaction path already exists'
install -d -m 0700 -o root -g root "$TRANSACTION_DIR"
JOURNAL_FILE="$TRANSACTION_DIR/journal"
BACKUP_DIR="$TRANSACTION_DIR/backup"
BACKUP_MANIFEST="$BACKUP_DIR/surface.manifest"
SERVICE_MANIFEST="$BACKUP_DIR/services.manifest"
SOURCE_REPO="$TRANSACTION_DIR/source"
install -m 0600 -o root -g root /dev/null "$JOURNAL_FILE"
PAYLOAD_LIFECYCLE_READY=1
journal_phase started
journal_value bootstrap_sha "$BOOTSTRAP_SHA"
journal_value candidate_ref "$CANDIDATE_REF"
journal_value candidate_sha "$CANDIDATE_SHA"

# Verify the exact ref at the canonical source before preserving or mutating the
# installed transition surface. No tag is created or used by this script.
remote_sha="$(clean_git ls-remote "$CANONICAL_UPSTREAM" "$CANDIDATE_REF" | awk -v ref="$CANDIDATE_REF" '$2 == ref { count++; sha = $1 } END { if (count == 1) print sha; else exit 1 }')" || fail 'canonical candidate ref is ambiguous or unavailable'
is_commit_sha "$remote_sha" || fail 'canonical candidate ref did not resolve to a commit SHA'
[ "$remote_sha" = "$CANDIDATE_SHA" ] || fail 'canonical candidate ref does not match --candidate-sha'
clean_git init "$SOURCE_REPO" >/dev/null
chmod 0700 "$SOURCE_REPO"
fetched_sha="$(clean_git -C "$SOURCE_REPO" fetch --no-tags "$CANONICAL_UPSTREAM" "$CANDIDATE_REF" >/dev/null && clean_git -C "$SOURCE_REPO" rev-parse --verify 'FETCH_HEAD^{commit}')" || fail 'canonical candidate fetch failed'
[ "$fetched_sha" = "$CANDIDATE_SHA" ] || fail 'fetched candidate SHA does not match --candidate-sha'
clean_git -C "$SOURCE_REPO" cat-file -e "$CANDIDATE_SHA:scripts/gp-root-helper.sh" || fail 'candidate root helper is missing'
clean_git -C "$SOURCE_REPO" show "$CANDIDATE_SHA:scripts/gp-root-helper.sh" > "$TRANSACTION_DIR/candidate-root-helper"
bash -n "$TRANSACTION_DIR/candidate-root-helper" || fail 'candidate root helper has invalid shell syntax'
journal_phase source-verified

STAGED_INSTALL_PROFILE="$TRANSACTION_DIR/legacy-install-profile"
STAGED_CORE_ENV_FILE="$TRANSACTION_DIR/core-env"
STAGED_ROOT_HELPER_CONFIG="$TRANSACTION_DIR/root-helper-config"
prepare_install_profile
prepare_core_env
write_normalized_root_helper_config
baseline_sha="$(clean_git -c safe.directory="$baseline_dir" -C "$baseline_dir" rev-parse --verify 'HEAD^{commit}')" || fail 'installed checkout baseline SHA is unavailable'
is_commit_sha "$baseline_sha" || fail 'installed checkout baseline SHA is invalid'
journal_value baseline_sha "$baseline_sha"

install -d -m 0700 -o root -g root "$BACKUP_DIR"
install -m 0600 -o root -g root /dev/null "$BACKUP_MANIFEST"
install -m 0600 -o root -g root /dev/null "$SERVICE_MANIFEST"
snapshot_transition_surface
snapshot_services || fail 'legacy service enablement state is unsupported for rollback'
BACKUP_READY=1
journal_phase backup-created
journal_value backup_path "$BACKUP_DIR"

journal_phase mutation-started
if [ "$PROFILE_ACTION" = create ]; then
  install -m 0600 -o root -g root "$STAGED_INSTALL_PROFILE" "$INSTALL_PROFILE"
fi
if [ "$CORE_ENV_ACTION" = create ]; then
  install -m 0600 -o root -g root "$STAGED_CORE_ENV_FILE" "$CORE_ENV_FILE"
  [ -f "$CORE_ENV_FILE" ] && [ ! -L "$CORE_ENV_FILE" ] \
    && [ "$(stat -c '%u:%g:%a' "$CORE_ENV_FILE" 2>/dev/null || true)" = '0:0:600' ] \
    || fail 'core service metadata was not created as root:root mode 0600'
fi
install -m 0644 -o root -g root "$STAGED_ROOT_HELPER_CONFIG" "$ROOT_HELPER_CONFIG"
ensure_run_registry
install -m 0755 -o root -g root "$TRANSACTION_DIR/candidate-root-helper" "$ROOT_HELPER"
bash "$ROOT_HELPER" check >/dev/null 2>&1 || fail 'candidate root helper check failed after installation'
