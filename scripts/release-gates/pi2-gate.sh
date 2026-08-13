#!/usr/bin/env bash
# Manual release gate for a real Raspberry Pi 2. Run after a clean install of a
# release tag; it intentionally does not automate installation or reimaging.
set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly DEFAULT_INSTALL_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd -P)"
readonly DEFAULT_ROOT_HELPER="/usr/local/libexec/gp-control-plane/gp-root-helper"
readonly DEFAULT_RUN_REGISTRY_DIR="/run/gp-control-plane/runs"
readonly GATE_RUNTIME_PARENT="/run/gp-control-plane/gates"
readonly GATE_REPORT_PARENT="/var/lib/gp-control-plane/release-gates"
readonly INSTALL_PROFILE="/etc/default/gp-control-plane-install-profile"
readonly CANONICAL_UPSTREAM_URL="https://github.com/balbomush/GP-access-control-plane.git"
readonly CORE_RSS_LIMIT_KIB=$((180 * 1024))
readonly WEB_RSS_LIMIT_KIB=$((120 * 1024))
readonly COMBINED_RSS_LIMIT_KIB=$((300 * 1024))

REF=""
MODE="installed"
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

REPORT_DIR=""
REPORT=""
RUN_STAMP=""
START_EPOCH=0
EXPECTED_SHA=""
CANDIDATE_REF=""
INSTALLED_SHA=""
DIRTY_MARKER=""
TOKEN=""
PASSWORD=""
CURL_AUTH_HEADER_FILE=""
GATE_SECRET_DIR=""
APP_USER=""
APP_GROUP=""
APP_UID=""
APP_GID=""
API_URL=""
WEB_ENABLED=0
DIAGNOSTICS_COLLECTED=0
REPORT_READY=0
REPORT_DURATION_SECONDS=""
LEFTOVER_SUMMARY=""
CYCLE_CLEANUP_SUMMARY=""

usage() {
  cat <<'EOF'
Usage:
  sudo GP_GATE_PASSWORD='...' bash scripts/release-gates/pi2-gate.sh --ref vX.Y.Z [options]

Manual Raspberry Pi 2 release gate. Run it only after the target ref has been
installed on a real Pi 2. A clean install/reimage is a manual prerequisite;
this gate never performs a clean installation, reimage, or data reset.

Required:
  --ref TAG                 Existing release tag, not a branch. Its commit SHA is
                            resolved before the gate and must match after the gate.

Modes:
  --mode installed          Validate the already installed release (default).
  --mode dirty-update       Require a clean worktree, create one known temporary
                            dirty marker, queue the supported root-helper update,
                            and require that the marker is removed.

Locations and services:
  --install-dir PATH        Installed git checkout (default: gate's repository root).
  --state-dir PATH          Existing state directory (highest-priority override).
                            Otherwise GP_STATE_DIR is read from the trusted
                            installation profile. Pre-v0.4 tags may use the
                            legacy checkout state only when that profile is absent.
  --base-url URL            Web HTTP URL (default: http://127.0.0.1:8080).
  --core-url URL            Core HTTP URL (default: http://127.0.0.1:8081).
  --root-helper PATH        Installed root helper (default: /usr/local/libexec/gp-control-plane/gp-root-helper).
  --run-registry-dir PATH   Root-helper run registry (default: /run/gp-control-plane/runs).
  --core-service UNIT       Core systemd unit (default: gp-control-plane-core.service).
  --web-service UNIT        Web systemd unit (default: gp-control-plane-web.service).

Authentication and test controls:
  --username NAME           API account (default: admin).
  --password-env NAME       Environment variable holding the API password
                            (default: GP_GATE_PASSWORD). Its value and the issued
                            bearer token are never logged or passed in curl argv.
  --test-domain DOMAIN      Domain used only for immediate-cancel runs (default: example.com).
  --poll-timeout SECONDS    Per-condition poll deadline, 10..900 (default: 90).
  -h, --help                Show this help and exit.

The gate starts and stops ten real discovery runs (five standard and five
multi_domain). It writes JSONL reports and per-step logs below
/var/lib/gp-control-plane/release-gates.
It performs no automatic retry of failed requests or commands; polling only waits
for an already accepted operation to reach its required state.
EOF
}

die() {
  printf 'pi2-gate: %s\n' "$*" >&2
  exit 2
}

require_value() {
  [ "$#" -eq 2 ] && [ -n "$2" ] || die "missing value for $1"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command is unavailable: $1"
}

validate_url() {
  case "$1" in
    http://*|https://*) ;;
    *) die "URL must start with http:// or https://: $1" ;;
  esac
  [[ "$1" != *$'\n'* && "$1" != *$'\r'* && "$1" != *' '* ]] || die "URL contains whitespace"
}

validate_name() {
  [[ "$2" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "$1 must be an environment variable name"
}

validate_domain() {
  [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]$ ]] && [[ "$1" == *.* ]] || die "invalid test domain: $1"
}

canonical_existing_dir() {
  [ -d "$1" ] || die "directory does not exist: $1"
  readlink -f -- "$1"
}

require_trusted_root_dir() {
  local path="$1" uid="$2" gid="$3" mode="$4"
  [ -d "$path" ] && [ ! -L "$path" ] || die "trusted gate directory is missing or unsafe: $path"
  [ "$(stat -c '%u' "$path" 2>/dev/null || true)" = "$uid" ] || die "trusted gate directory has unexpected owner: $path"
  [ "$(stat -c '%g' "$path" 2>/dev/null || true)" = "$gid" ] || die "trusted gate directory has unexpected group: $path"
  [ "$(stat -c '%a' "$path" 2>/dev/null || true)" = "$mode" ] || die "trusted gate directory has unexpected mode: $path"
}

read_trusted_profile_state_dir() {
  [ -e "$INSTALL_PROFILE" ] || [ -L "$INSTALL_PROFILE" ] || return 3
  [ -f "$INSTALL_PROFILE" ] && [ ! -L "$INSTALL_PROFILE" ] || {
    printf 'installation profile must be a regular non-symlink file: %s\n' "$INSTALL_PROFILE" >&2
    return 1
  }
  [ "$(stat -c '%u:%g:%a' "$INSTALL_PROFILE" 2>/dev/null || true)" = '0:0:600' ] || {
    printf 'installation profile must be root:root mode 0600: %s\n' "$INSTALL_PROFILE" >&2
    return 1
  }
  awk '
    function allowed(key) {
      return key == "GP_INSTALL_USER" || key == "GP_INSTALL_DIR" || key == "GP_STATE_DIR" ||
        key == "GP_SERVICE_NAME" || key == "GP_CORE_SERVICE_NAME" || key == "GP_INSTALL_WEB" ||
        key == "GP_WEB_HOST" || key == "GP_WEB_PORT" || key == "GP_WEB_ENV_FILE" ||
        key == "GP_CORE_HOST" || key == "GP_CORE_PORT" || key == "GP_CORE_URL" ||
        key == "GP_CORE_ENV_FILE" || key == "GP_ZAPRET_DIR" || key == "GP_ROOT_HELPER_PATH" ||
        key == "GP_ROOT_HELPER_CONFIG" || key == "GP_ROOT_HELPER_RUN_DIR" || key == "GP_SUDOERS_PATH" ||
        key == "GP_SERVICE_MEMORY_HIGH" || key == "GP_SERVICE_MEMORY_MAX"
    }
    function quoted(value,    length_value, position, character) {
      length_value = length(value)
      if (length_value < 2 || substr(value, 1, 1) != "\047") return 0
      for (position = 2; position <= length_value; position++) {
        character = substr(value, position, 1)
        if (character == "\047") {
          if (position == length_value) return 1
          if (substr(value, position + 1, 3) != "\\\047\047") return 0
          position += 3
        }
      }
      return 0
    }
    /^[[:space:]]*$/ || /^#/ { next }
    {
      equal = index($0, "=")
      if (equal < 2) { invalid = 1; next }
      key = substr($0, 1, equal - 1)
      value = substr($0, equal + 1)
      if (!allowed(key) || !quoted(value) || ++seen[key] != 1) { invalid = 1; next }
      if (key == "GP_STATE_DIR") state_value = value
    }
    END { if (invalid || !seen["GP_STATE_DIR"]) exit 2; print state_value }
  ' "$INSTALL_PROFILE" | awk '
    function decode(value,    length_value, position, character, result) {
      length_value = length(value)
      for (position = 2; position < length_value; position++) {
        character = substr(value, position, 1)
        if (character == "\047") { result = result "\047"; position += 3 } else result = result character
      }
      print result
    }
    { decode($0) }
  '
}

legacy_state_fallback_allowed() {
  case "$REF" in v0.[0-3].*) return 0 ;; *) return 1 ;; esac
}

resolve_state_dir() {
  local profile_state_dir status
  if [ -n "$STATE_DIR" ]; then
    STATE_DIR="$(canonical_existing_dir "$STATE_DIR")"
    return
  fi
  if profile_state_dir="$(read_trusted_profile_state_dir)"; then
    case "$profile_state_dir" in
      /*) ;;
      *) die "GP_STATE_DIR in installation profile must be an absolute path" ;;
    esac
    case "$profile_state_dir" in *'//'*|*'/./'*|*/'.'|*'/../'*|*/'..'|*[![:print:]]*) die "GP_STATE_DIR in installation profile is unsafe" ;; esac
    STATE_DIR="$(canonical_existing_dir "$profile_state_dir")"
    return
  fi
  status=$?
  if [ "$status" -eq 3 ] && legacy_state_fallback_allowed; then
    STATE_DIR="$(canonical_existing_dir "$INSTALL_DIR/build/state")"
    return
  fi
  if [ "$status" -eq 3 ]; then
    die "installation profile is required to derive --state-dir for $REF: $INSTALL_PROFILE"
  fi
  die "cannot safely read GP_STATE_DIR from installation profile: $INSTALL_PROFILE"
}

new_gate_report_file() {
  local suffix="$1" path
  path="$(mktemp "$REPORT_DIR/pi2-gate-$RUN_STAMP-$suffix.XXXXXX")" || return 1
  chown root:"$APP_GROUP" "$path" && chmod 0640 "$path" || { rm -f -- "$path"; return 1; }
  printf '%s\n' "$path"
}

detect_topology() {
  local core_state web_state
  core_state="$(systemctl show --property=LoadState --value "$CORE_SERVICE" 2>/dev/null || true)"
  [ "$core_state" = loaded ] || die "core service is not installed: $CORE_SERVICE"
  APP_USER="$(systemctl show --property=User --value "$CORE_SERVICE" 2>/dev/null || true)"
  [[ "$APP_USER" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]] || die "core service has no safe application user"
  APP_UID="$(id -u "$APP_USER" 2>/dev/null || true)"
  APP_GID="$(id -g "$APP_USER" 2>/dev/null || true)"
  APP_GROUP="$(id -gn "$APP_USER" 2>/dev/null || true)"
  [[ "$APP_UID" =~ ^[1-9][0-9]*$ && "$APP_GID" =~ ^[1-9][0-9]*$ ]] || die "core service must run as a non-root application user"
  [[ "$APP_GROUP" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]] || die "core service application group is unsafe"
  web_state="$(systemctl show --property=LoadState --value "$WEB_SERVICE" 2>/dev/null || true)"
  case "$web_state" in
    loaded) WEB_ENABLED=1; API_URL="$BASE_URL" ;;
    not-found|"") WEB_ENABLED=0; API_URL="$CORE_URL" ;;
    *) die "web service has unexpected LoadState=$web_state: $WEB_SERVICE" ;;
  esac
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --ref) require_value "$1" "${2:-}"; REF="$2"; shift 2 ;;
    --mode) require_value "$1" "${2:-}"; MODE="$2"; shift 2 ;;
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
case "$MODE" in installed|dirty-update) ;; *) die "--mode must be installed or dirty-update" ;; esac
case "$REF" in *..*|/*|*\\*|*[!A-Za-z0-9._/-]*) die "invalid release tag: $REF" ;; esac
[[ "$POLL_TIMEOUT_SECONDS" =~ ^[0-9]+$ ]] && [ "$POLL_TIMEOUT_SECONDS" -ge 10 ] && [ "$POLL_TIMEOUT_SECONDS" -le 900 ] || die "--poll-timeout must be 10..900"
validate_url "$BASE_URL"
validate_url "$CORE_URL"
validate_name "--password-env" "$PASSWORD_ENV"
validate_domain "$TEST_DOMAIN"

INSTALL_DIR="$(canonical_existing_dir "$INSTALL_DIR")"
[ -d "$INSTALL_DIR/.git" ] || die "--install-dir is not a git checkout: $INSTALL_DIR"
resolve_state_dir
ROOT_HELPER="$(readlink -f -- "$ROOT_HELPER" 2>/dev/null || true)"
[ -n "$ROOT_HELPER" ] && [ -x "$ROOT_HELPER" ] || die "root helper is not executable; pass --root-helper"
RUN_REGISTRY_DIR="${RUN_REGISTRY_DIR%/}"
case "$RUN_REGISTRY_DIR" in /*) ;; *) die "--run-registry-dir must be absolute" ;; esac
case "$CORE_SERVICE" in *.service) ;; *) die "--core-service must be a .service unit" ;; esac
case "$WEB_SERVICE" in *.service) ;; *) die "--web-service must be a .service unit" ;; esac

for required in bash curl git python3 systemctl ps awk find cmp nft mktemp runuser; do require_command "$required"; done
[ "$(id -u)" -eq 0 ] || die "run this hardware gate as root (sudo) so registry and process cleanup can be verified"
[ -r "/proc/device-tree/model" ] || die "not a Raspberry Pi: /proc/device-tree/model is unavailable"
BOARD_MODEL="$(tr -d '\000' < /proc/device-tree/model)"
case "$BOARD_MODEL" in *"Raspberry Pi 2"*) ;; *) die "this gate is Pi 2 only; detected: $BOARD_MODEL" ;; esac

if [ ! -v "$PASSWORD_ENV" ]; then
  die "password environment variable is unset: $PASSWORD_ENV"
fi
PASSWORD="${!PASSWORD_ENV}"
[ -n "$PASSWORD" ] || die "password environment variable is empty: $PASSWORD_ENV"

PYTHON="$INSTALL_DIR/.venv/bin/python"
[ -x "$PYTHON" ] || die "installed virtualenv Python is unavailable: $PYTHON"
detect_topology
RUN_STAMP="$(date -u +%Y%m%dT%H%M%SZ)-$$"
require_trusted_root_dir "$GATE_RUNTIME_PARENT" 0 0 700
require_trusted_root_dir "$GATE_REPORT_PARENT" 0 "$APP_GID" 750
GATE_SECRET_DIR="$GATE_RUNTIME_PARENT"
REPORT_DIR="$GATE_REPORT_PARENT"
REPORT="$(new_gate_report_file jsonl)" || die "cannot create root-owned gate report"
START_EPOCH="$(date +%s)"
REPORT_READY=1

report_event() {
  local kind="$1" name="$2" status="$3" detail="${4:-}" log_path="${5:-}"
  GATE_EVENT_KIND="$kind" GATE_EVENT_NAME="$name" GATE_EVENT_STATUS="$status" \
    GATE_EVENT_DETAIL="$detail" GATE_EVENT_LOG="$log_path" GATE_REF="$REF" \
    GATE_EXPECTED_SHA="$EXPECTED_SHA" GATE_INSTALLED_SHA="$INSTALLED_SHA" \
    GATE_BOARD_MODEL="$BOARD_MODEL" GATE_BASE_URL="$BASE_URL" GATE_CORE_URL="$CORE_URL" \
    GATE_REPORT_DURATION_SECONDS="$REPORT_DURATION_SECONDS" \
    "$PYTHON" - "$REPORT" <<'PY'
import json, os, platform, sys, time
path = sys.argv[1]
payload = {
    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "kind": os.environ["GATE_EVENT_KIND"],
    "name": os.environ["GATE_EVENT_NAME"],
    "status": os.environ["GATE_EVENT_STATUS"],
    "detail": os.environ.get("GATE_EVENT_DETAIL", ""),
    "log": os.environ.get("GATE_EVENT_LOG", ""),
    "ref": os.environ.get("GATE_REF", ""),
    "expected_sha": os.environ.get("GATE_EXPECTED_SHA", ""),
    "installed_sha": os.environ.get("GATE_INSTALLED_SHA", ""),
    "board_model": os.environ.get("GATE_BOARD_MODEL", ""),
    "os": platform.platform(),
    "base_url": os.environ.get("GATE_BASE_URL", ""),
    "core_url": os.environ.get("GATE_CORE_URL", ""),
}
duration = os.environ.get("GATE_REPORT_DURATION_SECONDS", "")
if duration:
    payload["duration_seconds"] = int(duration)
with open(path, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
PY
}

collect_diagnostics() {
  [ "$REPORT_READY" -eq 1 ] && [ "$DIAGNOSTICS_COLLECTED" -eq 0 ] || return 0
  DIAGNOSTICS_COLLECTED=1
  local diagnostic_log
  diagnostic_log="$(new_gate_report_file diagnostics)" || return 0
  {
    printf '%s\n' '== systemd =='
    systemctl --no-pager --full status "$CORE_SERVICE" || true
    journalctl --no-pager -u "$CORE_SERVICE" -n 120 || true
    if [ "$WEB_ENABLED" -eq 1 ]; then
      systemctl --no-pager --full status "$WEB_SERVICE" || true
      journalctl --no-pager -u "$WEB_SERVICE" -n 120 || true
    fi
    printf '%s\n' '== API status (tokens redacted) =='
    api_get "$API_URL" "/api/core/status" || true
    api_get "$API_URL" "/api/core/strategy-discovery/current-run-progress" || true
    api_get "$API_URL" "/api/core/runs/history?limit=20" || true
    printf '%s\n' '== root-helper registry and processes =='
    find "$RUN_REGISTRY_DIR" -mindepth 1 -maxdepth 2 -printf '%M %u %g %p\n' 2>&1 || true
    ps -eo pid=,ppid=,pgid=,sid=,args= | awk '/[b]lockcheck2\.sh|[n]fqws2|[c]url/ {print}' || true
    printf '%s\n' '== nft tables =='
    "$ROOT_HELPER" nft-list-tables || true
  } > "$diagnostic_log" 2>&1
  report_event diagnostics failure-collected complete "failure diagnostics collected" "$diagnostic_log"
}

finish() {
  local code=$?
  trap - EXIT
  set +e
  if [ -n "$DIRTY_MARKER" ] && [ -e "$DIRTY_MARKER" ]; then
    runuser -u "$APP_USER" -- rm -f -- "$DIRTY_MARKER" || true
  fi
  DIRTY_MARKER=""
  if [ "$REPORT_READY" -eq 1 ]; then
    INSTALLED_SHA="$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null || true)"
    if [ "$code" -ne 0 ]; then collect_diagnostics; fi
    local end_epoch duration
    end_epoch="$(date +%s)"
    duration=$((end_epoch - START_EPOCH))
    REPORT_DURATION_SECONDS="$duration"
    report_event summary pi2-gate "$([ "$code" -eq 0 ] && printf success || printf failed)" "exit=$code duration_seconds=$duration" "" || true
    printf 'pi2-gate: %s; report: %s\n' "$([ "$code" -eq 0 ] && printf PASS || printf FAIL)" "$REPORT" >&2
  fi
  if [ -n "$CURL_AUTH_HEADER_FILE" ] && [ -e "$CURL_AUTH_HEADER_FILE" ]; then
    rm -f -- "$CURL_AUTH_HEADER_FILE" || true
  fi
  CURL_AUTH_HEADER_FILE=""
  unset PASSWORD TOKEN
  exit "$code"
}
trap finish EXIT

run_step() {
  local name="$1"; shift
  local safe_name="${name//[^A-Za-z0-9._-]/_}"
  local step_log
  local code
  step_log="$(new_gate_report_file "$safe_name")" || return 1
  set +e
  "$@" > "$step_log" 2>&1
  code=$?
  set -e
  if [ "$code" -eq 0 ]; then
    report_event test "$name" success "completed" "$step_log"
    return 0
  fi
  report_event test "$name" failed "exit=$code" "$step_log"
  return "$code"
}

require_step() {
  local name="$1"; shift
  run_step "$name" "$@" || die "required gate step failed: $name (see $REPORT)"
}

api_get() {
  local base="$1" path="$2"
  [ -n "$CURL_AUTH_HEADER_FILE" ] && [ -f "$CURL_AUTH_HEADER_FILE" ] && [ ! -L "$CURL_AUTH_HEADER_FILE" ] || {
    printf 'authenticated request attempted before bearer header file was prepared\n' >&2
    return 1
  }
  curl --fail --silent --show-error --connect-timeout 10 --max-time 30 \
    --header "@$CURL_AUTH_HEADER_FILE" "$base$path"
}

api_post() {
  local base="$1" path="$2" payload="$3"
  [ -n "$CURL_AUTH_HEADER_FILE" ] && [ -f "$CURL_AUTH_HEADER_FILE" ] && [ ! -L "$CURL_AUTH_HEADER_FILE" ] || {
    printf 'authenticated request attempted before bearer header file was prepared\n' >&2
    return 1
  }
  printf '%s' "$payload" | curl --fail --silent --show-error --connect-timeout 10 --max-time 30 \
    --header "@$CURL_AUTH_HEADER_FILE" -H 'Content-Type: application/json' \
    --data-binary @- "$base$path"
}

prepare_bearer_header_file() {
  local header_file
  [ -n "$TOKEN" ] || {
    printf 'cannot prepare bearer header file without a token\n' >&2
    return 1
  }
  if [ -n "$CURL_AUTH_HEADER_FILE" ] && [ -e "$CURL_AUTH_HEADER_FILE" ]; then
    rm -f -- "$CURL_AUTH_HEADER_FILE" || return 1
  fi
  CURL_AUTH_HEADER_FILE=""
  header_file="$(mktemp "$GATE_SECRET_DIR/pi2-gate-bearer-$RUN_STAMP.XXXXXX")" || return 1
  if ! chmod 0600 "$header_file" || ! printf 'Authorization: Bearer %s\n' "$TOKEN" > "$header_file"; then
    rm -f -- "$header_file" || true
    return 1
  fi
  CURL_AUTH_HEADER_FILE="$header_file"
}

json_assert() {
  local expression="$1" input="$2"
  "$PYTHON" - "$expression" "$input" <<'PY'
import json, sys
expression, raw = sys.argv[1:]
data = json.loads(raw)
if expression == "core-ready":
    assert data.get("state") == "idle", data
    assert data.get("storage", {}).get("ready") is True, data
    assert int(data.get("storage", {}).get("schema_version") or 0) > 0, data
elif expression == "preflight-ready":
    assert data.get("ready") is True, data
elif expression == "accepted":
    assert data.get("accepted") is True and data.get("run_id"), data
elif expression == "stopping":
    assert data.get("status") == "stopping" and data.get("run_id"), data
elif expression == "empty-current":
    assert not data.get("run_id"), data
else:
    raise AssertionError("unknown assertion: " + expression)
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
  [ "$INSTALLED_SHA" = "$EXPECTED_SHA" ] || {
    printf 'installed SHA %s does not match tag %s (%s)\n' "$INSTALLED_SHA" "$REF" "$EXPECTED_SHA" >&2
    return 1
  }
  [ -z "$(git -C "$INSTALL_DIR" status --porcelain)" ] || {
    printf 'installed checkout has local changes\n' >&2
    return 1
  }
}

run_root_linux_test() {
  local test_name="$1"
  (
    cd "$INSTALL_DIR"
    PYTHONDONTWRITEBYTECODE=1 "$PYTHON" -B -m unittest "$test_name"
  )
}

queue_dirty_update() {
  local response queue_evidence unit log_file deadline state
  if [ -n "$(git -C "$INSTALL_DIR" status --porcelain)" ]; then
    printf 'worktree must be clean before dirty-update mode\n' >&2
    return 1
  fi
  DIRTY_MARKER="$(runuser -u "$APP_USER" -- mktemp "$INSTALL_DIR/.pi2-gate-dirty-marker-$RUN_STAMP.XXXXXX")" || {
    printf 'cannot create dirty marker as the application user\n' >&2
    return 1
  }
  printf 'created by pi2-gate %s for ref %s\n' "$RUN_STAMP" "$REF" | runuser -u "$APP_USER" -- tee "$DIRTY_MARKER" >/dev/null
  response="$("$ROOT_HELPER" queue-update --candidate-ref "$CANDIDATE_REF" --expected-sha "$EXPECTED_SHA")"
  printf '%s\n' "$response"
  queue_evidence="$(validate_queue_evidence "$response")" || return 1
  IFS=$'\t' read -r unit log_file <<< "$queue_evidence"
  deadline=$(( $(date +%s) + 900 ))
  while :; do
    if grep -qx 'status=success' "$log_file"; then
      validate_update_success_evidence "$log_file" || { cat "$log_file"; return 1; }
      break
    fi
    if grep -q '^status=' "$log_file"; then cat "$log_file"; printf 'queue-update emitted invalid terminal status\n' >&2; return 1; fi
    state="$(systemctl show --property=ActiveState --value "$unit" 2>/dev/null || true)"
    if [ "$state" = failed ]; then cat "$log_file"; return 1; fi
    [ "$(date +%s)" -lt "$deadline" ] || { cat "$log_file"; printf 'queue-update timed out\n' >&2; return 1; }
    sleep 1
  done
  [ ! -e "$DIRTY_MARKER" ] || { printf 'queued update did not remove gate dirty marker\n' >&2; return 1; }
  DIRTY_MARKER=""
}

validate_queue_evidence() {
  GATE_QUEUE_EVIDENCE="$1" GATE_CANDIDATE_REF="$CANDIDATE_REF" GATE_EXPECTED_SHA="$EXPECTED_SHA" \
    "$PYTHON" - <<'PY'
import os, re

expected = {
    "queued": "true",
    "status": "queued",
    "phase": "queued",
    "candidate_ref": os.environ["GATE_CANDIDATE_REF"],
    "expected_sha": os.environ["GATE_EXPECTED_SHA"],
}
values = {}
for line in os.environ["GATE_QUEUE_EVIDENCE"].splitlines():
    if line.count("=") != 1:
        raise SystemExit("queue-update evidence contains a malformed line")
    key, value = line.split("=", 1)
    if key in values:
        raise SystemExit("queue-update evidence contains a duplicate key: " + key)
    values[key] = value
if set(values) != set(expected) | {"unit", "log"}:
    raise SystemExit("queue-update evidence has an unexpected key set")
for key, value in expected.items():
    if values.get(key) != value:
        raise SystemExit("queue-update evidence has an invalid " + key)
unit = values["unit"]
if not re.fullmatch(r"gp-control-plane-update-[0-9]{8}T[0-9]{6}Z-[0-9]+", unit):
    raise SystemExit("queue-update evidence has an unsafe unit")
log = values["log"]
if log != "/var/lib/gp-control-plane/release-updates/" + unit + ".log":
    raise SystemExit("queue-update evidence has an unsafe log path")
print(unit + "\t" + log)
PY
}

validate_update_success_evidence() {
  local log_file="$1"
  require_trusted_root_dir /var/lib/gp-control-plane/release-updates 0 0 700
  [ -f "$log_file" ] && [ ! -L "$log_file" ] || return 1
  [ "$(stat -c '%u:%g:%a' "$log_file" 2>/dev/null || true)" = '0:0:600' ] || return 1
  GATE_UPDATE_LOG="$log_file" GATE_CANDIDATE_REF="$CANDIDATE_REF" GATE_EXPECTED_SHA="$EXPECTED_SHA" \
    "$PYTHON" - <<'PY'
import os

required = {
    "candidate_ref": [os.environ["GATE_CANDIDATE_REF"]],
    "expected_sha": [os.environ["GATE_EXPECTED_SHA"]],
    "verified_ref": [os.environ["GATE_CANDIDATE_REF"]],
    "verified_sha": [os.environ["GATE_EXPECTED_SHA"]],
    "staged_sha": [os.environ["GATE_EXPECTED_SHA"]],
    "installed_ref": [os.environ["GATE_CANDIDATE_REF"]],
    "installed_sha": [os.environ["GATE_EXPECTED_SHA"]],
    "cleanup_status": ["completed"],
    "status": ["success"],
    "phase": ["requested", "verified", "staged", "published", "root", "committed", "installed"],
}
seen = {key: [] for key in required}
success_seen = False
with open(os.environ["GATE_UPDATE_LOG"], encoding="utf-8", errors="replace") as handle:
    for raw in handle:
        line = raw.rstrip("\n")
        if success_seen:
            if line.strip():
                raise SystemExit("strict update log contains evidence after success")
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key == "error":
            raise SystemExit("strict update log contains error evidence")
        if key in seen:
            seen[key].append(value)
            if key == "status" and value == "success":
                success_seen = True
for key, expected in required.items():
    if seen[key] != expected:
        raise SystemExit("strict update log has invalid " + key + " evidence")
PY
}

login_api() {
  local payload response
  payload="$(GATE_LOGIN_USERNAME="$USERNAME" GATE_LOGIN_PASSWORD="$PASSWORD" "$PYTHON" - <<'PY'
import json, os
print(json.dumps({"username": os.environ["GATE_LOGIN_USERNAME"], "password": os.environ["GATE_LOGIN_PASSWORD"]}))
PY
)"
  response="$(printf '%s' "$payload" | curl --fail --silent --show-error --connect-timeout 10 --max-time 30 \
    -H 'Content-Type: application/json' --data-binary @- "$API_URL/api/auth/login")"
  TOKEN="$(printf '%s' "$response" | "$PYTHON" -c '
import json, sys
data = json.load(sys.stdin)
token = data.get("access_token")
if not isinstance(token, str) or not token:
    raise SystemExit("login response has no access_token")
print(token)
')"
  prepare_bearer_header_file
  unset TOKEN
}

check_http_and_auth() {
  curl --fail --silent --show-error --connect-timeout 10 --max-time 30 "$CORE_URL/api/health" >/dev/null
  if [ "$WEB_ENABLED" -eq 1 ]; then
    curl --fail --silent --show-error --connect-timeout 10 --max-time 30 "$BASE_URL/api/health" >/dev/null
  fi
  login_api
  json_assert core-ready "$(api_get "$CORE_URL" "/api/core/status")"
  if [ "$WEB_ENABLED" -eq 1 ]; then
    json_assert core-ready "$(api_get "$BASE_URL" "/api/core/status")"
    api_get "$BASE_URL" "/api/service/status" >/dev/null
  fi
  json_assert preflight-ready "$(api_get "$API_URL" "/api/core/strategy-discovery/preflight")"
}

check_required_services() {
  systemctl is-active --quiet "$CORE_SERVICE"
  if [ "$WEB_ENABLED" -eq 1 ]; then
    systemctl is-active --quiet "$WEB_SERVICE"
  fi
}

check_storage_integrity() {
  local db="$STATE_DIR/strategy-finder/state.sqlite3"
  [ -f "$db" ] || { printf 'storage database is missing: %s\n' "$db" >&2; return 1; }
  "$PYTHON" - "$db" <<'PY'
import sqlite3, sys
path = sys.argv[1]
conn = sqlite3.connect("file:" + path + "?mode=ro", uri=True)
try:
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    schema = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
finally:
    conn.close()
assert integrity == "ok", integrity
assert schema and str(schema[0]).isdigit() and int(schema[0]) > 0, schema
PY
}

current_run_id() {
  "$PYTHON" - "$1" <<'PY'
import json, sys
print(str(json.loads(sys.argv[1]).get("run_id") or ""))
PY
}

history_run_state() {
  "$PYTHON" - "$1" "$2" <<'PY'
import json, sys
run_id, data = sys.argv[1], json.loads(sys.argv[2])
if not isinstance(data.get("runs"), list):
    raise AssertionError("runs history has no runs list")
for run in data["runs"]:
    if run.get("run_id") == run_id and run.get("status") == "stopped":
        print("stopped")
        raise SystemExit(0)
print("pending")
PY
}

inspect_leftovers() {
  local current="" current_run="" current_state="unknown" lock_state registry_state
  local process_state nft_state registry_entry process_lines nft_tables clean=1
  if current="$(api_get "$API_URL" "/api/core/strategy-discovery/current-run-progress")" &&
    current_run="$(current_run_id "$current")"; then
    if [ -n "$current_run" ]; then
      current_state="present"
      clean=0
    else
      current_state="clear"
    fi
  else
    clean=0
  fi
  if [ -e "$STATE_DIR/.job-runner.lock" ]; then
    lock_state="present"
    clean=0
  else
    lock_state="clear"
  fi
  if [ -d "$RUN_REGISTRY_DIR" ]; then
    registry_entry="$(find "$RUN_REGISTRY_DIR" -mindepth 1 -maxdepth 1 -print -quit)"
    if [ -n "$registry_entry" ]; then
      registry_state="present"
      clean=0
    else
      registry_state="clear"
    fi
  else
    registry_state="unknown"
    clean=0
  fi
  process_lines="$(ps -eo args= | awk '/[b]lockcheck2\.sh|[n]fqws2|[c]url/ {print}')"
  if [ -n "$process_lines" ]; then
    process_state="present"
    clean=0
  else
    process_state="clear"
  fi
  if nft_tables="$("$ROOT_HELPER" nft-list-tables)"; then
    if awk '$2 ~ /^blockcheck[0-9]*/ {found=1} END {exit !found}' <<<"$nft_tables"; then
      nft_state="present"
      clean=0
    else
      nft_state="clear"
    fi
  else
    nft_state="unknown"
    clean=0
  fi
  LEFTOVER_SUMMARY="current_run=$current_state job_lock=$lock_state run_registry=$registry_state processes=$process_state nft=$nft_state"
  printf 'leftover_assertion %s\n' "$LEFTOVER_SUMMARY"
  [ "$clean" -eq 1 ]
}

record_leftover_assertion() {
  local cycle_name="$1" phase="$2" assertion_log assertion_status
  assertion_log="$REPORT_DIR/pi2-gate-$RUN_STAMP-${cycle_name//[^A-Za-z0-9._-]/_}-$phase-leftovers.log"
  if inspect_leftovers > "$assertion_log" 2>&1; then
    assertion_status="success"
  else
    assertion_status="failed"
  fi
  report_event leftovers "$cycle_name-$phase" "$assertion_status" "$LEFTOVER_SUMMARY" "$assertion_log"
  return 0
}

attempt_safe_cycle_cleanup() {
  local run_id="$1" current="" current_run="" stop_response="" stopped_run=""
  CYCLE_CLEANUP_SUMMARY=""
  if [ -z "$run_id" ]; then
    CYCLE_CLEANUP_SUMMARY="cleanup=skipped-unidentified-run"
    printf '%s\n' "$CYCLE_CLEANUP_SUMMARY"
    return 0
  fi
  if ! current="$(api_get "$API_URL" "/api/core/strategy-discovery/current-run-progress")"; then
    CYCLE_CLEANUP_SUMMARY="cleanup=skipped-current-run-unavailable run_id=$run_id"
    printf '%s\n' "$CYCLE_CLEANUP_SUMMARY"
    return 0
  fi
  if ! current_run="$(current_run_id "$current")"; then
    CYCLE_CLEANUP_SUMMARY="cleanup=skipped-current-run-invalid run_id=$run_id"
    printf '%s\n' "$CYCLE_CLEANUP_SUMMARY"
    return 0
  fi
  if [ "$current_run" != "$run_id" ]; then
    CYCLE_CLEANUP_SUMMARY="cleanup=skipped-current-run-mismatch run_id=$run_id current_run=${current_run:-none}"
    printf '%s\n' "$CYCLE_CLEANUP_SUMMARY"
    return 0
  fi
  if ! stop_response="$(api_post "$API_URL" "/api/core/strategy-discovery/stop-current-run" '{}')"; then
    CYCLE_CLEANUP_SUMMARY="cleanup=stop-request-failed run_id=$run_id"
    printf '%s\n' "$CYCLE_CLEANUP_SUMMARY"
    return 0
  fi
  if ! json_assert stopping "$stop_response" || ! stopped_run="$(current_run_id "$stop_response")" || [ "$stopped_run" != "$run_id" ]; then
    CYCLE_CLEANUP_SUMMARY="cleanup=stop-response-invalid run_id=$run_id"
    printf '%s\n' "$CYCLE_CLEANUP_SUMMARY"
    return 0
  fi
  CYCLE_CLEANUP_SUMMARY="cleanup=stop-requested-for-own-run run_id=$run_id"
  printf '%s\n' "$CYCLE_CLEANUP_SUMMARY"
}

record_safe_cycle_cleanup() {
  local cycle_name="$1" run_id="$2" cleanup_log
  cleanup_log="$REPORT_DIR/pi2-gate-$RUN_STAMP-${cycle_name//[^A-Za-z0-9._-]/_}-safe-cleanup.log"
  attempt_safe_cycle_cleanup "$run_id" > "$cleanup_log" 2>&1 || true
  report_event cleanup "$cycle_name" complete "$CYCLE_CLEANUP_SUMMARY" "$cleanup_log"
}

cycle_failure() {
  local mode="$1" index="$2" run_id="$3" stage="$4" original_code="$5"
  local cycle_name="cancel-$mode-$index"
  report_event recovery "$cycle_name" failed "original_stage=$stage exit=$original_code run_id=${run_id:-unknown}; no retry" ""
  record_leftover_assertion "$cycle_name" post-failure-before-cleanup
  record_safe_cycle_cleanup "$cycle_name" "$run_id"
  record_leftover_assertion "$cycle_name" post-cleanup
  printf 'cycle mode=%s index=%s failed at %s with exit=%s; recovery evidence recorded without retry\n' \
    "$mode" "$index" "$stage" "$original_code" >&2
  return "$original_code"
}

start_and_cancel_cycle() {
  local mode="$1" index="$2" payload response run_id="" deadline current current_run history history_state stop_response stop_run code
  if payload="$(GATE_MODE="$mode" GATE_DOMAIN="$TEST_DOMAIN" "$PYTHON" - <<'PY'
import json, os
print(json.dumps({
    "mode": os.environ["GATE_MODE"],
    "domains": [os.environ["GATE_DOMAIN"]],
    "protocols": ["tcp"],
    "curl_parallelism": 1,
    "settings": {
        "enable_http": False, "enable_tls12": True, "enable_tls13": False,
        "include_quic": False, "enable_ipv6": False, "scan_level": "quick",
        "repeats": 1, "repeat_parallel": False, "skip_dnscheck": True,
        "skip_ipblock": True, "curl_max_time": 10,
    },
}))
PY
)"; then
    :
  else
    code=$?
    cycle_failure "$mode" "$index" "$run_id" payload "$code" || true
    return "$code"
  fi
  if response="$(api_post "$API_URL" "/api/core/strategy-discovery/start-run" "$payload")"; then
    :
  else
    code=$?
    cycle_failure "$mode" "$index" "$run_id" start-request "$code" || true
    return "$code"
  fi
  if json_assert accepted "$response"; then
    :
  else
    code=$?
    cycle_failure "$mode" "$index" "$run_id" start-response "$code" || true
    return "$code"
  fi
  if run_id="$("$PYTHON" - "$response" <<'PY'
import json, sys
print(json.loads(sys.argv[1])["run_id"])
PY
)"; then
    :
  else
    code=$?
    cycle_failure "$mode" "$index" "$run_id" start-run-id "$code" || true
    return "$code"
  fi
  if stop_response="$(api_post "$API_URL" "/api/core/strategy-discovery/stop-current-run" '{}')"; then
    :
  else
    code=$?
    cycle_failure "$mode" "$index" "$run_id" immediate-stop-request "$code" || true
    return "$code"
  fi
  if json_assert stopping "$stop_response"; then
    :
  else
    code=$?
    cycle_failure "$mode" "$index" "$run_id" immediate-stop-response "$code" || true
    return "$code"
  fi
  if stop_run="$(current_run_id "$stop_response")" && [ "$stop_run" = "$run_id" ]; then
    :
  else
    printf 'stop accepted another run\n' >&2
    cycle_failure "$mode" "$index" "$run_id" immediate-stop-run-mismatch 1 || true
    return 1
  fi
  deadline=$(( $(date +%s) + POLL_TIMEOUT_SECONDS ))
  while :; do
    if current="$(api_get "$API_URL" "/api/core/strategy-discovery/current-run-progress")"; then
      :
    else
      code=$?
      cycle_failure "$mode" "$index" "$run_id" observe-stop-current "$code" || true
      return "$code"
    fi
    if current_run="$(current_run_id "$current")"; then
      :
    else
      code=$?
      cycle_failure "$mode" "$index" "$run_id" parse-stop-current "$code" || true
      return "$code"
    fi
    if history="$(api_get "$API_URL" "/api/core/runs/history?limit=1000")"; then
      :
    else
      code=$?
      cycle_failure "$mode" "$index" "$run_id" observe-stop-history "$code" || true
      return "$code"
    fi
    if history_state="$(history_run_state "$run_id" "$history")"; then
      :
    else
      code=$?
      cycle_failure "$mode" "$index" "$run_id" parse-stop-history "$code" || true
      return "$code"
    fi
    if [ -z "$current_run" ] && [ "$history_state" = stopped ]; then
      printf 'cycle mode=%s index=%s run_id=%s stopped\n' "$mode" "$index" "$run_id"
      return 0
    fi
    if [ "$(date +%s)" -lt "$deadline" ]; then
      :
    else
      printf 'run %s did not clear and persist as stopped\n' "$run_id" >&2
      cycle_failure "$mode" "$index" "$run_id" stop-timeout 1 || true
      return 1
    fi
    if sleep 1; then
      :
    else
      code=$?
      cycle_failure "$mode" "$index" "$run_id" stop-sleep "$code" || true
      return "$code"
    fi
  done
}

check_no_leftovers() {
  inspect_leftovers
}

cgroup_rss_kib() {
  local unit="$1" group proc_file pid rss=0 seen=0
  group="$(systemctl show --property=ControlGroup --value "$unit")"
  [ -n "$group" ] && [ "$group" != / ] || return 1
  proc_file="/sys/fs/cgroup$group/cgroup.procs"
  [ -r "$proc_file" ] || return 1
  while IFS= read -r pid; do
    [ -n "$pid" ] || continue
    seen=$((seen + 1))
    local value
    value="$(ps -o rss= -p "$pid" 2>/dev/null | tr -d '[:space:]')"
    [[ "$value" =~ ^[0-9]+$ ]] && rss=$((rss + value))
  done < "$proc_file"
  [ "$seen" -gt 0 ] || return 1
  printf '%s\n' "$rss"
}

check_resource_budget() {
  local core_rss web_rss combined
  core_rss="$(cgroup_rss_kib "$CORE_SERVICE")"
  [[ "$core_rss" =~ ^[0-9]+$ ]] || return 1
  if [ "$WEB_ENABLED" -eq 0 ]; then
    printf 'topology=headless core_rss_kib=%s\n' "$core_rss"
    [ "$core_rss" -le "$CORE_RSS_LIMIT_KIB" ]
    return
  fi
  web_rss="$(cgroup_rss_kib "$WEB_SERVICE")"
  [[ "$web_rss" =~ ^[0-9]+$ ]] || return 1
  combined=$((core_rss + web_rss))
  printf 'core_rss_kib=%s web_rss_kib=%s combined_rss_kib=%s\n' "$core_rss" "$web_rss" "$combined"
  [ "$core_rss" -le "$CORE_RSS_LIMIT_KIB" ]
  [ "$web_rss" -le "$WEB_RSS_LIMIT_KIB" ]
  [ "$combined" -le "$COMBINED_RSS_LIMIT_KIB" ]
}

report_event metadata pi2-gate started "mode=$MODE state_dir=$STATE_DIR run_registry_dir=$RUN_REGISTRY_DIR" ""
require_step immutable-ref resolve_immutable_tag
report_event candidate update-candidate resolved "candidate_ref=$CANDIDATE_REF expected_sha=$EXPECTED_SHA" ""
require_step installed-ref-before check_installed_ref
if [ "$MODE" = dirty-update ]; then
  require_step dirty-update queue_dirty_update
  require_step installed-ref-after check_installed_ref
  require_step dirty-update-services check_required_services
fi
require_step root-helper-check "$ROOT_HELPER" check
require_step root-helper-installed-copy cmp -s "$INSTALL_DIR/scripts/gp-root-helper.sh" "$ROOT_HELPER"
require_step root-linux-helper-reap run_root_linux_test tests.test_zapret2.Zapret2Tests.test_root_run_owned_reaps_term_ignoring_child_and_returns_target_code
require_step root-linux-helper-signal run_root_linux_test tests.test_zapret2.Zapret2Tests.test_root_signal_after_go_reaps_term_ignoring_target_and_child
require_step root-linux-helper-registration run_root_linux_test tests.test_zapret2.Zapret2Tests.test_root_helper_creates_the_only_signalable_record_and_rejects_direct_registration
require_step systemd-services check_required_services
require_step http-health-auth check_http_and_auth
require_step storage-integrity-schema check_storage_integrity

for index in 1 2 3 4 5; do
  require_step "cancel-standard-$index" start_and_cancel_cycle standard "$index"
done
for index in 1 2 3 4 5; do
  require_step "cancel-multi-domain-$index" start_and_cancel_cycle multi_domain "$index"
done

require_step no-leftovers check_no_leftovers
require_step resource-budget-rss check_resource_budget
