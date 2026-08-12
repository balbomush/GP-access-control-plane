#!/usr/bin/env bash
# Manual functional release gate for a Raspberry Pi 5.  It is intentionally
# safe to invoke through SSH: it never reimages, installs, resets data, or
# prints an access token.
set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly DEFAULT_INSTALL_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd -P)"
readonly DEFAULT_ROOT_HELPER="/usr/local/libexec/gp-control-plane/gp-root-helper"
readonly DEFAULT_RUN_REGISTRY_DIR="/run/gp-control-plane/runs"
readonly GATE_RUNTIME_PARENT="/run/gp-control-plane/gates"
readonly GATE_REPORT_PARENT="/var/lib/gp-control-plane/release-gates"
readonly UPDATE_LOG_PARENT="/var/lib/gp-control-plane/release-updates"
readonly INSTALL_PROFILE="/etc/default/gp-control-plane-install-profile"
readonly CANONICAL_UPSTREAM_URL="https://github.com/balbomush/GP-access-control-plane.git"

REF=""
MODE="installed"
TOPOLOGY=""
ACK_CLEAN_INSTALL=0
INSTALL_DIR="$DEFAULT_INSTALL_DIR"
STATE_DIR=""
BASE_URL="http://127.0.0.1:8080"
CORE_URL="http://127.0.0.1:8081"
ROOT_HELPER="$DEFAULT_ROOT_HELPER"
RUN_REGISTRY_DIR="$DEFAULT_RUN_REGISTRY_DIR"
CORE_SERVICE="gp-control-plane-core.service"
WEB_SERVICE="gp-control-plane-web.service"
PASSWORD_ENV="GP_GATE_PASSWORD"
USERNAME="admin"
TEST_DOMAIN="example.com"
POLL_TIMEOUT_SECONDS=90

APP_USER="" APP_GROUP="" APP_GID="" API_URL="" WEB_ENABLED=0
EXPECTED_SHA="" INSTALLED_SHA="" CANDIDATE_REF="" DIRTY_MARKER=""
PASSWORD="" TOKEN="" CURL_AUTH_HEADER_FILE="" REPORT_DIR="" REPORT="" RUN_STAMP=""
LEFTOVER_SUMMARY="" REPORT_READY=0

usage() {
  cat <<'EOF'
Usage:
  sudo --preserve-env=GP_GATE_PASSWORD bash scripts/release-gates/pi5-gate.sh \
    --ref v0.4.0 --topology web|headless [options]

Manual topology-aware Raspberry Pi 5 functional release gate.  It is designed
for execution over SSH and writes root-owned JSONL evidence under
/var/lib/gp-control-plane/release-gates. It never automates reimage,
installation, uninstallation, data reset, or rollback.

Required:
  --ref TAG                    Existing immutable release tag.
  --topology web|headless      Expected installed topology; the gate rejects a
                               deployed topology that does not match.

Modes:
  --mode installed             Validate an already installed release (default).
  --mode dirty-update          Create one owned dirty marker, queue the strict
                               root-helper update, and require its removal.
  --mode clean-install         Validate only the result of an operator-performed
                               clean installation. Requires --ack-clean-install.
  --ack-clean-install          Explicit acknowledgement for --mode clean-install;
                               this does not initiate installation or reimaging.

Options:
  --install-dir PATH           Installed git checkout (default: repository root).
  --state-dir PATH             Existing state directory (highest-priority override).
                               Otherwise GP_STATE_DIR is read from the trusted
                               installation profile. Pre-v0.4 tags may use the
                               legacy checkout state only when that profile is absent.
  --base-url URL               Web URL (default: http://127.0.0.1:8080).
  --core-url URL               Core URL (default: http://127.0.0.1:8081).
  --root-helper PATH           Installed root helper path.
  --run-registry-dir PATH      Root-helper run registry.
  --core-service UNIT          Core systemd unit.
  --web-service UNIT           Web systemd unit.
  --username NAME              API account (default: admin).
  --password-env NAME          Password environment variable (default:
                               GP_GATE_PASSWORD). Passwords and bearer tokens
                               are never written to reports or curl argv.
  --test-domain DOMAIN         Domain used only for immediate-cancel runs.
  --poll-timeout SECONDS       Per-condition deadline, 10..900 (default: 90).
  -h, --help                   Show this help.

For clean-install: reimage/install the Pi explicitly as an operator action,
then run this gate with --mode clean-install --ack-clean-install.  The gate
checks the installed consequence only; it has no destructive install command.
EOF
}

die() { printf 'pi5-gate: %s\n' "$*" >&2; exit 2; }
require_value() { [ "$#" -eq 2 ] && [ -n "$2" ] || die "missing value for $1"; }
require_command() { command -v "$1" >/dev/null 2>&1 || die "required command is unavailable: $1"; }

validate_url() {
  case "$1" in http://*|https://*) ;; *) die "URL must start with http:// or https://: $1" ;; esac
  [[ "$1" != *$'\n'* && "$1" != *$'\r'* && "$1" != *' '* ]] || die "URL contains whitespace"
}
validate_env_name() { [[ "$1" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "--password-env must be an environment variable name"; }
validate_domain() { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]$ && "$1" == *.* ]] || die "invalid test domain: $1"; }
canonical_existing_dir() { [ -d "$1" ] || die "directory does not exist: $1"; readlink -f -- "$1"; }

require_trusted_root_dir() {
  local path="$1" uid="$2" gid="$3" mode="$4"
  [ -d "$path" ] && [ ! -L "$path" ] || die "trusted gate directory is missing or unsafe: $path"
  [ "$(stat -c '%u' "$path" 2>/dev/null || true)" = "$uid" ] || die "trusted gate directory has unexpected owner: $path"
  [ "$(stat -c '%g' "$path" 2>/dev/null || true)" = "$gid" ] || die "trusted gate directory has unexpected group: $path"
  [ "$(stat -c '%a' "$path" 2>/dev/null || true)" = "$mode" ] || die "trusted gate directory has unexpected mode: $path"
}

read_trusted_profile_state_dir() {
  [ -e "$INSTALL_PROFILE" ] || [ -L "$INSTALL_PROFILE" ] || return 3
  [ -f "$INSTALL_PROFILE" ] && [ ! -L "$INSTALL_PROFILE" ] || { printf 'installation profile must be a regular non-symlink file: %s\n' "$INSTALL_PROFILE" >&2; return 1; }
  [ "$(stat -c '%u:%g:%a' "$INSTALL_PROFILE" 2>/dev/null || true)" = '0:0:600' ] || { printf 'installation profile must be root:root mode 0600: %s\n' "$INSTALL_PROFILE" >&2; return 1; }
  awk '
    function allowed(key) {
      return key == "GP_INSTALL_USER" || key == "GP_INSTALL_DIR" || key == "GP_STATE_DIR" || key == "GP_SERVICE_NAME" || key == "GP_CORE_SERVICE_NAME" || key == "GP_INSTALL_WEB" || key == "GP_WEB_HOST" || key == "GP_WEB_PORT" || key == "GP_WEB_ENV_FILE" || key == "GP_CORE_HOST" || key == "GP_CORE_PORT" || key == "GP_CORE_URL" || key == "GP_CORE_ENV_FILE" || key == "GP_ZAPRET_DIR" || key == "GP_ROOT_HELPER_PATH" || key == "GP_ROOT_HELPER_CONFIG" || key == "GP_ROOT_HELPER_RUN_DIR" || key == "GP_SUDOERS_PATH" || key == "GP_SERVICE_MEMORY_HIGH" || key == "GP_SERVICE_MEMORY_MAX"
    }
    function quoted(value,    length_value, position, character) {
      length_value = length(value); if (length_value < 2 || substr(value, 1, 1) != "\047") return 0
      for (position = 2; position <= length_value; position++) { character = substr(value, position, 1); if (character == "\047") { if (position == length_value) return 1; if (substr(value, position + 1, 3) != "\\\047\047") return 0; position += 3 } }
      return 0
    }
    /^[[:space:]]*$/ || /^#/ { next }
    { equal = index($0, "="); if (equal < 2) { invalid = 1; next }; key = substr($0, 1, equal - 1); value = substr($0, equal + 1); if (!allowed(key) || !quoted(value) || ++seen[key] != 1) { invalid = 1; next }; if (key == "GP_STATE_DIR") state_value = value }
    END { if (invalid || !seen["GP_STATE_DIR"]) exit 2; print state_value }
  ' "$INSTALL_PROFILE" | awk '
    function decode(value,    length_value, position, character, result) { length_value = length(value); for (position = 2; position < length_value; position++) { character = substr(value, position, 1); if (character == "\047") { result = result "\047"; position += 3 } else result = result character } print result }
    { decode($0) }
  '
}

legacy_state_fallback_allowed() {
  case "$REF" in
    v0.[0-3].*) return 0 ;;
    *) return 1 ;;
  esac
}

resolve_state_dir() {
  local profile_state_dir status
  if [ -n "$STATE_DIR" ]; then STATE_DIR="$(canonical_existing_dir "$STATE_DIR")"; return; fi
  if profile_state_dir="$(read_trusted_profile_state_dir)"; then
    case "$profile_state_dir" in /*) ;; *) die "GP_STATE_DIR in installation profile must be an absolute path" ;; esac
    case "$profile_state_dir" in *'//'*|*'/./'*|*/'.'|*'/../'*|*/'..'|*[![:print:]]*) die "GP_STATE_DIR in installation profile is unsafe" ;; esac
    STATE_DIR="$(canonical_existing_dir "$profile_state_dir")"; return
  fi
  status=$?
  if [ "$status" -eq 3 ] && legacy_state_fallback_allowed; then STATE_DIR="$(canonical_existing_dir "$INSTALL_DIR/build/state")"; return; fi
  [ "$status" -ne 3 ] || die "installation profile is required to derive --state-dir for $REF: $INSTALL_PROFILE"
  die "cannot safely read GP_STATE_DIR from installation profile: $INSTALL_PROFILE"
}

reload_state_dir_from_install_profile() {
  # A strict update can migrate the default state directory outside INSTALL_DIR.
  STATE_DIR=""
  resolve_state_dir
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --ref) require_value "$1" "${2:-}"; REF="$2"; shift 2 ;;
    --mode) require_value "$1" "${2:-}"; MODE="$2"; shift 2 ;;
    --topology) require_value "$1" "${2:-}"; TOPOLOGY="$2"; shift 2 ;;
    --ack-clean-install) ACK_CLEAN_INSTALL=1; shift ;;
    --install-dir) require_value "$1" "${2:-}"; INSTALL_DIR="$2"; shift 2 ;;
    --state-dir) require_value "$1" "${2:-}"; STATE_DIR="$2"; shift 2 ;;
    --base-url) require_value "$1" "${2:-}"; BASE_URL="${2%/}"; shift 2 ;;
    --core-url) require_value "$1" "${2:-}"; CORE_URL="${2%/}"; shift 2 ;;
    --root-helper) require_value "$1" "${2:-}"; ROOT_HELPER="$2"; shift 2 ;;
    --run-registry-dir) require_value "$1" "${2:-}"; RUN_REGISTRY_DIR="$2"; shift 2 ;;
    --core-service) require_value "$1" "${2:-}"; CORE_SERVICE="$2"; shift 2 ;;
    --web-service) require_value "$1" "${2:-}"; WEB_SERVICE="$2"; shift 2 ;;
    --username) require_value "$1" "${2:-}"; USERNAME="$2"; shift 2 ;;
    --password-env) require_value "$1" "${2:-}"; PASSWORD_ENV="$2"; shift 2 ;;
    --test-domain) require_value "$1" "${2:-}"; TEST_DOMAIN="$2"; shift 2 ;;
    --poll-timeout) require_value "$1" "${2:-}"; POLL_TIMEOUT_SECONDS="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (use --help)" ;;
  esac
done

[ -n "$REF" ] || die "--ref is required"
[ -n "$TOPOLOGY" ] || die "--topology is required"
case "$MODE" in installed|dirty-update|clean-install) ;; *) die "--mode must be installed, dirty-update, or clean-install" ;; esac
case "$TOPOLOGY" in web|headless) ;; *) die "--topology must be web or headless" ;; esac
[ "$MODE" != clean-install ] || [ "$ACK_CLEAN_INSTALL" -eq 1 ] || die "--mode clean-install requires --ack-clean-install"
[ "$MODE" = clean-install ] || [ "$ACK_CLEAN_INSTALL" -eq 0 ] || die "--ack-clean-install is valid only with --mode clean-install"
case "$REF" in *..*|/*|*\\*|*[!A-Za-z0-9._/-]*) die "invalid release tag: $REF" ;; esac
[[ "$POLL_TIMEOUT_SECONDS" =~ ^[0-9]+$ && "$POLL_TIMEOUT_SECONDS" -ge 10 && "$POLL_TIMEOUT_SECONDS" -le 900 ]] || die "--poll-timeout must be 10..900"
validate_url "$BASE_URL"; validate_url "$CORE_URL"; validate_env_name "$PASSWORD_ENV"; validate_domain "$TEST_DOMAIN"

INSTALL_DIR="$(canonical_existing_dir "$INSTALL_DIR")"
[ -d "$INSTALL_DIR/.git" ] || die "--install-dir is not a git checkout: $INSTALL_DIR"
resolve_state_dir
ROOT_HELPER="$(readlink -f -- "$ROOT_HELPER" 2>/dev/null || true)"
[ -n "$ROOT_HELPER" ] && [ -x "$ROOT_HELPER" ] || die "root helper is not executable; pass --root-helper"
case "$RUN_REGISTRY_DIR" in /*) ;; *) die "--run-registry-dir must be absolute" ;; esac
for required in bash curl git python3 systemctl ps awk find nft mktemp runuser cmp; do require_command "$required"; done
[ "$(id -u)" -eq 0 ] || die "run this hardware gate as root (sudo)"
[ -r /proc/device-tree/model ] || die "not a Raspberry Pi: /proc/device-tree/model is unavailable"
BOARD_MODEL="$(tr -d '\000' < /proc/device-tree/model)"
case "$BOARD_MODEL" in *"Raspberry Pi 5"*) ;; *) die "this gate is Pi 5 only; detected: $BOARD_MODEL" ;; esac
[ -v "$PASSWORD_ENV" ] || die "password environment variable is unset: $PASSWORD_ENV"
PASSWORD="${!PASSWORD_ENV}"; [ -n "$PASSWORD" ] || die "password environment variable is empty: $PASSWORD_ENV"
PYTHON="$INSTALL_DIR/.venv/bin/python"; [ -x "$PYTHON" ] || die "installed virtualenv Python is unavailable: $PYTHON"

detect_topology() {
  local core_state web_state
  core_state="$(systemctl show --property=LoadState --value "$CORE_SERVICE" 2>/dev/null || true)"
  [ "$core_state" = loaded ] || die "core service is not installed: $CORE_SERVICE"
  APP_USER="$(systemctl show --property=User --value "$CORE_SERVICE" 2>/dev/null || true)"
  [[ "$APP_USER" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]] || die "core service has no safe application user"
  APP_GROUP="$(id -gn "$APP_USER" 2>/dev/null || true)"; APP_GID="$(id -g "$APP_USER" 2>/dev/null || true)"
  [[ "$APP_GROUP" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ && "$APP_GID" =~ ^[1-9][0-9]*$ ]] || die "core service application group is unsafe"
  web_state="$(systemctl show --property=LoadState --value "$WEB_SERVICE" 2>/dev/null || true)"
  case "$TOPOLOGY:$web_state" in
    web:loaded) WEB_ENABLED=1; API_URL="$BASE_URL" ;;
    headless:not-found|headless:"") WEB_ENABLED=0; API_URL="$CORE_URL" ;;
    web:*) die "--topology web requires loaded $WEB_SERVICE (LoadState=$web_state)" ;;
    headless:*) die "--topology headless requires no $WEB_SERVICE (LoadState=$web_state)" ;;
  esac
}

detect_topology
RUN_STAMP="$(date -u +%Y%m%dT%H%M%SZ)-$$"
require_trusted_root_dir "$GATE_RUNTIME_PARENT" 0 0 700
require_trusted_root_dir "$GATE_REPORT_PARENT" 0 "$APP_GID" 750
REPORT_DIR="$GATE_REPORT_PARENT"
REPORT="$(mktemp "$REPORT_DIR/pi5-gate-$RUN_STAMP.jsonl.XXXXXX")" || die "cannot create gate report"
chown root:"$APP_GROUP" "$REPORT" && chmod 0640 "$REPORT" || die "cannot secure gate report"
REPORT_READY=1

report_event() {
  local kind="$1" name="$2" status="$3" detail="${4:-}" log_path="${5:-}"
  GATE_KIND="$kind" GATE_NAME="$name" GATE_STATUS="$status" GATE_DETAIL="$detail" GATE_LOG="$log_path" \
    GATE_REF="$REF" GATE_TOPOLOGY="$TOPOLOGY" GATE_EXPECTED_SHA="$EXPECTED_SHA" GATE_INSTALLED_SHA="$INSTALLED_SHA" \
    "$PYTHON" - "$REPORT" <<'PY'
import json, os, sys, time
payload = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "kind": os.environ["GATE_KIND"], "name": os.environ["GATE_NAME"],
           "status": os.environ["GATE_STATUS"], "detail": os.environ["GATE_DETAIL"],
           "log": os.environ["GATE_LOG"], "ref": os.environ["GATE_REF"],
           "topology": os.environ["GATE_TOPOLOGY"], "expected_sha": os.environ["GATE_EXPECTED_SHA"],
           "installed_sha": os.environ["GATE_INSTALLED_SHA"]}
with open(sys.argv[1], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(payload, sort_keys=True) + "\n")
PY
}

finish() {
  local code=$?
  trap - EXIT; set +e
  [ -z "$DIRTY_MARKER" ] || runuser -u "$APP_USER" -- rm -f -- "$DIRTY_MARKER" || true
  [ -z "$CURL_AUTH_HEADER_FILE" ] || rm -f -- "$CURL_AUTH_HEADER_FILE" || true
  if [ "$REPORT_READY" -eq 1 ]; then
    INSTALLED_SHA="$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null || true)"
    report_event summary pi5-gate "$([ "$code" -eq 0 ] && printf success || printf failed)" "exit=$code" "" || true
    printf 'pi5-gate: %s; report: %s\n' "$([ "$code" -eq 0 ] && printf PASS || printf FAIL)" "$REPORT" >&2
  fi
  unset PASSWORD TOKEN
  exit "$code"
}
trap finish EXIT

run_step() {
  local name="$1"; shift
  local log="$REPORT_DIR/pi5-gate-$RUN_STAMP-${name//[^A-Za-z0-9._-]/_}.log" code
  set +e; "$@" > "$log" 2>&1; code=$?; set -e
  report_event test "$name" "$([ "$code" -eq 0 ] && printf success || printf failed)" "exit=$code" "$log"
  return "$code"
}
require_step() { local name="$1"; shift; run_step "$name" "$@" || die "required gate step failed: $name (see $REPORT)"; }

prepare_bearer_header_file() {
  local header_file
  [ -n "$TOKEN" ] || return 1
  header_file="$(mktemp "$GATE_RUNTIME_PARENT/pi5-gate-bearer-$RUN_STAMP.XXXXXX")" || return 1
  chmod 0600 "$header_file" && printf 'Authorization: Bearer %s\n' "$TOKEN" > "$header_file" || { rm -f -- "$header_file"; return 1; }
  CURL_AUTH_HEADER_FILE="$header_file"; unset TOKEN
}
api_get() {
  local base="$1" path="$2"
  [ -f "$CURL_AUTH_HEADER_FILE" ] && [ ! -L "$CURL_AUTH_HEADER_FILE" ] || return 1
  curl --fail --silent --show-error --connect-timeout 10 --max-time 30 --header "@$CURL_AUTH_HEADER_FILE" "$base$path"
}
api_post() {
  local base="$1" path="$2" payload="$3"
  [ -f "$CURL_AUTH_HEADER_FILE" ] && [ ! -L "$CURL_AUTH_HEADER_FILE" ] || return 1
  printf '%s' "$payload" | curl --fail --silent --show-error --connect-timeout 10 --max-time 30 \
    --header "@$CURL_AUTH_HEADER_FILE" -H 'Content-Type: application/json' --data-binary @- "$base$path"
}

json_assert() {
  GATE_ASSERT="$1" GATE_JSON="$2" "$PYTHON" - <<'PY'
import json, os
d = json.loads(os.environ["GATE_JSON"]); a = os.environ["GATE_ASSERT"]
if a == "ready": assert d.get("state") == "idle" and d.get("storage", {}).get("ready") is True, d
elif a == "preflight": assert d.get("ready") is True, d
elif a == "accepted": assert d.get("accepted") is True and d.get("run_id"), d
elif a == "stopping": assert d.get("status") == "stopping" and d.get("run_id"), d
elif a == "current-empty": assert not d.get("run_id"), d
else: raise AssertionError(a)
PY
}

resolve_immutable_tag() {
  # Security invariant: only fixed canonical upstream direct/peeled tag refs determine EXPECTED_SHA; local refs/remotes/config never do.
  local direct_sha="" peeled_sha="" remote_sha remote_ref remote_output
  CANDIDATE_REF="refs/tags/$REF"
  git -C "$INSTALL_DIR" check-ref-format "$CANDIDATE_REF"

  # Do not consult the checkout's remotes/configuration: a local tag can be forged.
  # Without --refs, ls-remote returns both the direct tag ref and an annotated
  # tag's peeled commit ref. A network or protocol failure is a failed gate.
  remote_output="$(GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=/bin/false \
    git -C / -c credential.helper= -c core.askPass=/bin/false -c http.extraHeader= \
      ls-remote "$CANONICAL_UPSTREAM_URL" "$CANDIDATE_REF" "${CANDIDATE_REF}^{}")" || {
    printf 'cannot resolve release tag from canonical upstream: %s\n' "$REF" >&2
    return 1
  }
  while IFS=$'\t' read -r remote_sha remote_ref; do
    [ -n "$remote_sha" ] || continue
    [[ "$remote_sha" =~ ^[0-9a-f]{40}$ ]] || { printf 'canonical upstream returned an invalid SHA\n' >&2; return 1; }
    case "$remote_ref" in
      "$CANDIDATE_REF") [ -z "$direct_sha" ] || { printf 'canonical upstream returned duplicate direct tag refs\n' >&2; return 1; }; direct_sha="$remote_sha" ;;
      "${CANDIDATE_REF}^{}") [ -z "$peeled_sha" ] || { printf 'canonical upstream returned duplicate peeled tag refs\n' >&2; return 1; }; peeled_sha="$remote_sha" ;;
      *) printf 'canonical upstream returned an unexpected ref: %s\n' "$remote_ref" >&2; return 1 ;;
    esac
  done <<< "$remote_output"
  [ -n "$direct_sha" ] || { printf 'release tag is absent from canonical upstream: %s\n' "$REF" >&2; return 1; }
  EXPECTED_SHA="${peeled_sha:-$direct_sha}"
  [[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]]
}
check_installed_ref() {
  INSTALLED_SHA="$(git -C "$INSTALL_DIR" rev-parse --verify HEAD)"
  [ "$INSTALLED_SHA" = "$EXPECTED_SHA" ] || { printf 'installed SHA does not match tag %s\n' "$REF" >&2; return 1; }
  [ -z "$(git -C "$INSTALL_DIR" status --porcelain)" ] || { printf 'installed checkout has local changes\n' >&2; return 1; }
}
check_services() {
  systemctl is-active --quiet "$CORE_SERVICE"
  [ "$WEB_ENABLED" -eq 0 ] || systemctl is-active --quiet "$WEB_SERVICE"
}
check_unauthenticated_protected_api() {
  local code
  code="$(curl --silent --output /dev/null --write-out '%{http_code}' --connect-timeout 10 --max-time 30 "$API_URL/api/core/status")"
  case "$code" in 401|403) ;; *) printf 'protected API accepted unauthenticated request: HTTP %s\n' "$code" >&2; return 1 ;; esac
}
login_api() {
  local payload response
  payload="$(GATE_LOGIN_USERNAME="$USERNAME" GATE_LOGIN_PASSWORD="$PASSWORD" "$PYTHON" - <<'PY'
import json, os
print(json.dumps({"username": os.environ["GATE_LOGIN_USERNAME"], "password": os.environ["GATE_LOGIN_PASSWORD"]}))
PY
)"
  response="$(printf '%s' "$payload" | curl --fail --silent --show-error --connect-timeout 10 --max-time 30 -H 'Content-Type: application/json' --data-binary @- "$API_URL/api/auth/login")"
  TOKEN="$(printf '%s' "$response" | "$PYTHON" -c 'import json,sys; token=json.load(sys.stdin).get("access_token"); assert isinstance(token,str) and token; print(token)')"
  prepare_bearer_header_file
}
check_api_and_auth() {
  curl --fail --silent --show-error --connect-timeout 10 --max-time 30 "$CORE_URL/api/health" >/dev/null
  if [ "$WEB_ENABLED" -eq 1 ]; then curl --fail --silent --show-error --connect-timeout 10 --max-time 30 "$BASE_URL/api/health" >/dev/null; fi
  check_unauthenticated_protected_api
  login_api
  json_assert ready "$(api_get "$CORE_URL" /api/core/status)"
  json_assert preflight "$(api_get "$API_URL" /api/core/strategy-discovery/preflight)"
  if [ "$WEB_ENABLED" -eq 1 ]; then
    json_assert ready "$(api_get "$BASE_URL" /api/core/status)"
    api_get "$BASE_URL" /api/service/status >/dev/null
    api_get "$BASE_URL" /api/web/run-preferences >/dev/null
  fi
}
check_storage_integrity() {
  local db="$STATE_DIR/strategy-finder/state.sqlite3"
  [ -f "$db" ] || { printf 'storage database is missing: %s\n' "$db" >&2; return 1; }
  "$PYTHON" - "$db" <<'PY'
import sqlite3, sys
conn = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
try:
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert row and int(row[0]) > 0
finally: conn.close()
PY
}

current_run_id() { "$PYTHON" - "$1" <<'PY'
import json, sys
print(str(json.loads(sys.argv[1]).get("run_id") or ""))
PY
}
run_is_stopped() { "$PYTHON" - "$1" "$2" <<'PY'
import json, sys
run_id, data = sys.argv[1], json.loads(sys.argv[2])
assert isinstance(data.get("runs"), list)
raise SystemExit(0 if any(r.get("run_id") == run_id and r.get("status") == "stopped" for r in data["runs"]) else 1)
PY
}
inspect_leftovers() {
  local current run lock registry processes tables nft_state=clear clean=1
  current="$(api_get "$API_URL" /api/core/strategy-discovery/current-run-progress)" || clean=0
  run="$(current_run_id "$current")" || clean=0
  [ -z "$run" ] || clean=0
  lock=clear; [ ! -e "$STATE_DIR/.job-runner.lock" ] || { lock=present; clean=0; }
  registry=clear; [ -d "$RUN_REGISTRY_DIR" ] && [ -z "$(find "$RUN_REGISTRY_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ] || { registry=present; clean=0; }
  processes="$(ps -eo args= | awk '/[b]lockcheck2\.sh|[n]fqws2|[c]url/ {print}')"; [ -z "$processes" ] || clean=0
  tables="$("$ROOT_HELPER" nft-list-tables)" || clean=0
  if awk '$2 ~ /^blockcheck[0-9]*/ {found=1} END {exit !found}' <<<"$tables"; then nft_state=present; clean=0; fi
  LEFTOVER_SUMMARY="current_run=${run:-clear} job_lock=$lock run_registry=$registry processes=$([ -n "$processes" ] && printf present || printf clear) nft_blockcheck=$nft_state"
  printf 'leftover_assertion %s\n' "$LEFTOVER_SUMMARY"
  [ "$clean" -eq 1 ]
}

safe_stop_own_run() {
  local expected_run="$1" current current_run stop_response
  [ -n "$expected_run" ] || return 0
  current="$(api_get "$API_URL" /api/core/strategy-discovery/current-run-progress)" || return 0
  current_run="$(current_run_id "$current")" || return 0
  [ "$current_run" = "$expected_run" ] || return 0
  stop_response="$(api_post "$API_URL" /api/core/strategy-discovery/stop-current-run '{}')" || return 0
  json_assert stopping "$stop_response" || return 0
  [ "$(current_run_id "$stop_response")" = "$expected_run" ] || return 0
  printf 'safe cleanup requested for own run %s\n' "$expected_run"
}

cycle_error() {
  local run_id="$1" stage="$2" code="$3"
  safe_stop_own_run "$run_id" || true
  printf 'cycle failed at %s (exit=%s); safe cleanup was attempted only for own run\n' "$stage" "$code" >&2
  return "$code"
}

start_and_stop_cycle() {
  local mode="$1" payload response run_id="" stopped deadline current history code
  payload="$(GATE_MODE="$mode" GATE_DOMAIN="$TEST_DOMAIN" "$PYTHON" - <<'PY'
import json, os
print(json.dumps({"mode": os.environ["GATE_MODE"], "domains": [os.environ["GATE_DOMAIN"]], "protocols": ["tcp"], "curl_parallelism": 1,
 "settings": {"enable_http": False, "enable_tls12": True, "enable_tls13": False, "include_quic": False, "enable_ipv6": False, "scan_level": "quick", "repeats": 1, "repeat_parallel": False, "skip_dnscheck": True, "skip_ipblock": True, "curl_max_time": 10}}))
PY
)" || { code=$?; cycle_error "$run_id" payload "$code"; return "$code"; }
  response="$(api_post "$API_URL" /api/core/strategy-discovery/start-run "$payload")" || { code=$?; cycle_error "$run_id" start-request "$code"; return "$code"; }
  json_assert accepted "$response" || { code=$?; cycle_error "$run_id" start-response "$code"; return "$code"; }
  run_id="$(current_run_id "$response")" || { code=$?; cycle_error "$run_id" start-run-id "$code"; return "$code"; }
  [ -n "$run_id" ] || { cycle_error "$run_id" empty-run-id 1; return 1; }
  stopped="$(api_post "$API_URL" /api/core/strategy-discovery/stop-current-run '{}')" || { code=$?; cycle_error "$run_id" stop-request "$code"; return "$code"; }
  json_assert stopping "$stopped" || { code=$?; cycle_error "$run_id" stop-response "$code"; return "$code"; }
  [ "$(current_run_id "$stopped")" = "$run_id" ] || { cycle_error "$run_id" stop-run-mismatch 1; return 1; }
  deadline=$(( $(date +%s) + POLL_TIMEOUT_SECONDS ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    current="$(api_get "$API_URL" /api/core/strategy-discovery/current-run-progress)" || { code=$?; cycle_error "$run_id" current-run "$code"; return "$code"; }
    history="$(api_get "$API_URL" '/api/core/runs/history?limit=1000')" || { code=$?; cycle_error "$run_id" history "$code"; return "$code"; }
    if [ -z "$(current_run_id "$current")" ] && run_is_stopped "$run_id" "$history"; then inspect_leftovers; return; fi
    sleep 1
  done
  cycle_error "$run_id" stop-timeout 1
}

validate_queue_evidence() {
  GATE_QUEUE="$1" GATE_REF="$CANDIDATE_REF" GATE_SHA="$EXPECTED_SHA" "$PYTHON" - <<'PY'
import os, re
values = {}
for line in os.environ["GATE_QUEUE"].splitlines():
    if line.count("=") != 1: raise SystemExit("malformed queue evidence")
    key, value = line.split("=", 1)
    if key in values: raise SystemExit("duplicate queue evidence key")
    values[key] = value
expected = {"queued":"true", "status":"queued", "phase":"queued", "candidate_ref":os.environ["GATE_REF"], "expected_sha":os.environ["GATE_SHA"]}
if set(values) != set(expected) | {"unit", "log"}: raise SystemExit("unexpected queue evidence key set")
if any(values[k] != v for k,v in expected.items()): raise SystemExit("invalid queue evidence")
unit = values["unit"]
if not re.fullmatch(r"gp-control-plane-update-[0-9]{8}T[0-9]{6}Z-[0-9]+", unit): raise SystemExit("unsafe unit")
if values["log"] != "/var/lib/gp-control-plane/release-updates/" + unit + ".log": raise SystemExit("unsafe log")
print(unit + "\t" + values["log"])
PY
}
validate_update_success_evidence() {
  local log="$1"
  require_trusted_root_dir "$UPDATE_LOG_PARENT" 0 0 700
  [ -f "$log" ] && [ ! -L "$log" ] && [ "$(stat -c '%u:%g:%a' "$log")" = 0:0:600 ] || return 1
  GATE_LOG="$log" GATE_REF="$CANDIDATE_REF" GATE_SHA="$EXPECTED_SHA" "$PYTHON" - <<'PY'
import os
want = {"candidate_ref":[os.environ["GATE_REF"]], "expected_sha":[os.environ["GATE_SHA"]], "verified_ref":[os.environ["GATE_REF"]], "verified_sha":[os.environ["GATE_SHA"]], "staged_sha":[os.environ["GATE_SHA"]], "installed_ref":[os.environ["GATE_REF"]], "installed_sha":[os.environ["GATE_SHA"]], "status":["success"], "phase":["requested","verified","staged","published","root","installed"]}
seen = {key: [] for key in want}
for raw in open(os.environ["GATE_LOG"], encoding="utf-8", errors="replace"):
    if "=" in raw:
        key,value = raw.rstrip("\n").split("=",1)
        if key == "error" or key == "rollback_scope": raise SystemExit("success log contains failure or rollback evidence")
        if key in seen: seen[key].append(value)
if seen != want: raise SystemExit("strict update success evidence is incomplete")
PY
}
check_rollback_contract() {
  cmp -s "$INSTALL_DIR/scripts/gp-root-helper.sh" "$ROOT_HELPER"
  "$PYTHON" - "$ROOT_HELPER" <<'PY'
import sys
source = open(sys.argv[1], encoding="utf-8").read()
for required in ("rollback_published_code() {", "phase=rollback", "rollback_scope=code", "rollback_after_publication_failure() {"):
    assert required in source, required
PY
}
queue_dirty_update() {
  local response evidence unit log deadline state
  [ -z "$(git -C "$INSTALL_DIR" status --porcelain)" ] || { printf 'worktree must be clean before dirty-update mode\n' >&2; return 1; }
  DIRTY_MARKER="$(runuser -u "$APP_USER" -- mktemp "$INSTALL_DIR/.pi5-gate-dirty-marker-$RUN_STAMP.XXXXXX")"
  printf 'owned Pi5 release-gate marker for %s\n' "$REF" | runuser -u "$APP_USER" -- tee "$DIRTY_MARKER" >/dev/null
  response="$("$ROOT_HELPER" queue-update --candidate-ref "$CANDIDATE_REF" --expected-sha "$EXPECTED_SHA")"
  evidence="$(validate_queue_evidence "$response")"; IFS=$'\t' read -r unit log <<< "$evidence"
  deadline=$(( $(date +%s) + 900 ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if grep -qx 'status=success' "$log"; then validate_update_success_evidence "$log"; break; fi
    if grep -q '^status=' "$log" || [ "$(systemctl show --property=ActiveState --value "$unit" 2>/dev/null || true)" = failed ]; then
      printf 'strict update failed; inspect rollback evidence in %s\n' "$log" >&2; return 1
    fi
    sleep 1
  done
  grep -qx 'status=success' "$log" || { printf 'strict update timed out\n' >&2; return 1; }
  [ ! -e "$DIRTY_MARKER" ] || { printf 'strict update did not remove gate dirty marker\n' >&2; return 1; }
  DIRTY_MARKER=""
}

report_event metadata pi5-gate started "mode=$MODE topology=$TOPOLOGY" ""
require_step immutable-ref resolve_immutable_tag
report_event candidate immutable-tag resolved "candidate_ref=$CANDIDATE_REF expected_sha=$EXPECTED_SHA" ""
require_step installed-ref-before check_installed_ref
if [ "$MODE" = dirty-update ]; then
  require_step strict-update-rollback-contract check_rollback_contract
  require_step strict-update-queue-success-evidence queue_dirty_update
  require_step state-dir-after-strict-update reload_state_dir_from_install_profile
  require_step installed-ref-after check_installed_ref
fi
if [ "$MODE" = clean-install ]; then report_event operator clean-install-acknowledged verified "operator performed reimage/install outside this gate" ""; fi
require_step services check_services
require_step api-login-protected check_api_and_auth
require_step storage-integrity check_storage_integrity
require_step start-stop-standard start_and_stop_cycle standard
require_step start-stop-multi-domain start_and_stop_cycle multi_domain
require_step no-leftovers inspect_leftovers
