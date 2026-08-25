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
readonly INSTALL_PROFILE="/etc/default/gp-control-plane-install-profile"
readonly CANONICAL_UPSTREAM_URL="https://github.com/balbomush/GP-access-control-plane.git"

REF="" CANDIDATE="" REQUESTED_EXPECTED_SHA=""
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
EXPECTED_SHA="" CANDIDATE_SHA="" INSTALLED_SHA="" CANDIDATE_REF="" CANONICAL_TAG_OBJECT_SHA=""
PASSWORD="" TOKEN="" CURL_AUTH_HEADER_FILE="" REPORT_DIR="" REPORT="" RUN_STAMP=""
LEFTOVER_SUMMARY="" REPORT_READY=0

usage() {
  cat <<'EOF'
Usage:
  sudo --preserve-env=GP_GATE_PASSWORD bash scripts/release-gates/pi5-gate.sh \
    --ref v0.4.0 --topology web|headless [options]
  sudo --preserve-env=GP_GATE_PASSWORD bash scripts/release-gates/pi5-gate.sh \
    --candidate origin/dev --expected-sha COMMIT_SHA --topology web|headless [options]

Manual topology-aware Raspberry Pi 5 functional release gate.  It is designed
for execution over SSH and writes root-owned JSONL evidence under
/var/lib/gp-control-plane/release-gates. It never automates reimage,
installation, uninstallation, data reset, or rollback.

Required:
  --ref TAG                    Existing immutable release tag. Mutually exclusive
                               with the pre-tag candidate interface.
  --candidate origin/dev       Canonical pre-tag candidate. Only origin/dev is
                               accepted and is passed as refs/heads/dev.
  --expected-sha SHA           Expected 40-character lowercase commit SHA for
                               --candidate origin/dev.
  --topology web|headless      Expected installed topology; the gate rejects a
                               deployed topology that does not match.

Modes:
  --mode installed             Validate an already installed release (default).
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

while [ "$#" -gt 0 ]; do
  case "$1" in
    --ref) require_value "$1" "${2:-}"; REF="$2"; shift 2 ;;
    --candidate) require_value "$1" "${2:-}"; CANDIDATE="$2"; shift 2 ;;
    --expected-sha) require_value "$1" "${2:-}"; REQUESTED_EXPECTED_SHA="$2"; shift 2 ;;
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

[ -n "$REF" ] || [ -n "$CANDIDATE" ] || die "--ref or --candidate is required"
[ -z "$REF" ] || [ -z "$CANDIDATE" ] || die "--ref and --candidate are mutually exclusive"
[ -n "$TOPOLOGY" ] || die "--topology is required"
case "$MODE" in installed|clean-install) ;; *) die "--mode must be installed or clean-install" ;; esac
case "$TOPOLOGY" in web|headless) ;; *) die "--topology must be web or headless" ;; esac
[ "$MODE" != clean-install ] || [ "$ACK_CLEAN_INSTALL" -eq 1 ] || die "--mode clean-install requires --ack-clean-install"
[ "$MODE" = clean-install ] || [ "$ACK_CLEAN_INSTALL" -eq 0 ] || die "--ack-clean-install is valid only with --mode clean-install"
if [ -n "$REF" ]; then
  [ -z "$REQUESTED_EXPECTED_SHA" ] || die "--expected-sha is valid only with --candidate origin/dev"
  case "$REF" in *..*|/*|*\\*|*[!A-Za-z0-9._/-]*) die "invalid release tag: $REF" ;; esac
else
  [ "$CANDIDATE" = origin/dev ] || die "--candidate must be the canonical origin/dev"
  [[ "$REQUESTED_EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]] || die "--expected-sha must be 40 lowercase hexadecimal characters"
  CANDIDATE_REF="refs/heads/dev"
  EXPECTED_SHA="$REQUESTED_EXPECTED_SHA"
  CANDIDATE_SHA="$EXPECTED_SHA"
fi
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
    GATE_REF="$REF" GATE_TOPOLOGY="$TOPOLOGY" GATE_CANDIDATE_REF="$CANDIDATE_REF" GATE_CANDIDATE_SHA="$CANDIDATE_SHA" GATE_EXPECTED_SHA="$EXPECTED_SHA" GATE_INSTALLED_SHA="$INSTALLED_SHA" GATE_CANONICAL_TAG_OBJECT_SHA="$CANONICAL_TAG_OBJECT_SHA" \
    "$PYTHON" - "$REPORT" <<'PY'
import json, os, sys, time
payload = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "kind": os.environ["GATE_KIND"], "name": os.environ["GATE_NAME"],
           "status": os.environ["GATE_STATUS"], "detail": os.environ["GATE_DETAIL"],
           "log": os.environ["GATE_LOG"], "ref": os.environ["GATE_REF"],
           "topology": os.environ["GATE_TOPOLOGY"], "candidate_ref": os.environ["GATE_CANDIDATE_REF"],
           "candidate_sha": os.environ["GATE_CANDIDATE_SHA"], "expected_sha": os.environ["GATE_EXPECTED_SHA"],
           "installed_sha": os.environ["GATE_INSTALLED_SHA"], "canonical_tag_object_sha": os.environ["GATE_CANONICAL_TAG_OBJECT_SHA"]}
with open(sys.argv[1], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(payload, sort_keys=True) + "\n")
PY
}

finish() {
  local code=$?
  trap - EXIT; set +e
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
  [ -n "$peeled_sha" ] || { printf 'release tag is not an annotated tag on canonical upstream: %s\n' "$REF" >&2; return 1; }
  CANONICAL_TAG_OBJECT_SHA="$direct_sha"
  EXPECTED_SHA="$peeled_sha"
  [[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]]
  CANDIDATE_SHA="$EXPECTED_SHA"
}
resolve_pre_tag_candidate() {
  local remote_sha="" remote_ref remote_output
  [ "$CANDIDATE" = origin/dev ] || return 1
  [ "$CANDIDATE_REF" = refs/heads/dev ] || return 1
  [[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]]
  remote_output="$(GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=/bin/false \
    git -C / -c credential.helper= -c core.askPass=/bin/false -c http.extraHeader= \
      ls-remote --exit-code "$CANONICAL_UPSTREAM_URL" "$CANDIDATE_REF")" || {
    printf 'cannot resolve pre-tag dev candidate from canonical upstream\n' >&2
    return 1
  }
  while IFS=$'\t' read -r candidate_sha candidate_ref; do
    [ -n "$candidate_sha" ] || continue
    [[ "$candidate_sha" =~ ^[0-9a-f]{40}$ ]] || { printf 'canonical upstream returned an invalid pre-tag SHA\n' >&2; return 1; }
    [ "$candidate_ref" = "$CANDIDATE_REF" ] || { printf 'canonical upstream returned an unexpected pre-tag ref\n' >&2; return 1; }
    [ -z "$remote_sha" ] || { printf 'canonical upstream returned duplicate pre-tag refs\n' >&2; return 1; }
    remote_sha="$candidate_sha"
  done <<< "$remote_output"
  [ "$remote_sha" = "$EXPECTED_SHA" ] || { printf 'canonical upstream dev does not match expected pre-tag SHA\n' >&2; return 1; }
  CANDIDATE_SHA="$EXPECTED_SHA"
}
check_installed_ref() {
  local local_tag_object_sha local_tag_commit_sha
  INSTALLED_SHA="$(git -C "$INSTALL_DIR" rev-parse --verify HEAD)"
  [ "$INSTALLED_SHA" = "$EXPECTED_SHA" ] || { printf 'installed SHA does not match expected candidate\n' >&2; return 1; }
  if [ -n "$REF" ]; then
    ! git -C "$INSTALL_DIR" symbolic-ref -q HEAD >/dev/null || { printf 'installed tag checkout is not detached\n' >&2; return 1; }
    git -C "$INSTALL_DIR" show-ref --verify --quiet "$CANDIDATE_REF" || { printf 'installed release tag is absent locally: %s\n' "$REF" >&2; return 1; }
    [ "$(git -C "$INSTALL_DIR" cat-file -t "$CANDIDATE_REF")" = tag ] || { printf 'installed release tag is not annotated locally: %s\n' "$REF" >&2; return 1; }
    local_tag_object_sha="$(git -C "$INSTALL_DIR" rev-parse --verify "${CANDIDATE_REF}^{tag}")" || { printf 'installed release tag object is invalid locally: %s\n' "$REF" >&2; return 1; }
    [ "$local_tag_object_sha" = "$CANONICAL_TAG_OBJECT_SHA" ] || { printf 'installed release tag object does not match canonical upstream: %s\n' "$REF" >&2; return 1; }
    local_tag_commit_sha="$(git -C "$INSTALL_DIR" rev-parse --verify "${CANDIDATE_REF}^{commit}")" || { printf 'installed release tag does not peel to a commit: %s\n' "$REF" >&2; return 1; }
    [ "$local_tag_commit_sha" = "$EXPECTED_SHA" ] || { printf 'installed release tag commit does not match expected SHA: %s\n' "$REF" >&2; return 1; }
  fi
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

current_run_id() {
  "$PYTHON" - "$1" <<'PY'
import json, sys
print(str(json.loads(sys.argv[1]).get("run_id") or ""))
PY
}
current_run_status() {
  "$PYTHON" - "$1" <<'PY'
import json, sys
print(str(json.loads(sys.argv[1]).get("status") or "unknown"))
PY
}
history_run_status() {
  "$PYTHON" - "$1" "$2" <<'PY'
import json, sys
run_id, data = sys.argv[1], json.loads(sys.argv[2])
assert isinstance(data.get("runs"), list)
for run in data["runs"]:
    if run.get("run_id") == run_id:
        print(str(run.get("status") or "unknown"))
        break
else:
    print("not-found")
PY
}
run_is_stopped() {
  "$PYTHON" - "$1" "$2" <<'PY'
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
  local mode="$1" payload response run_id="" stopped deadline current='{"run_id":"","status":"not-polled"}' history='{"runs":[]}' current_status=not-polled history_status=not-polled code
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
    current_status="$(current_run_status "$current")" || { code=$?; cycle_error "$run_id" current-status "$code"; return "$code"; }
    history_status="$(history_run_status "$run_id" "$history")" || { code=$?; cycle_error "$run_id" history-status "$code"; return "$code"; }
    if [ -z "$(current_run_id "$current")" ] && run_is_stopped "$run_id" "$history"; then inspect_leftovers; return; fi
    case "$history_status" in
      success|failed|timeout)
        printf 'stop reached unexpected terminal status: target_run_id=%s current_run_id=%s current_run_status=%s target_history_status=%s\n' \
          "$run_id" "$(current_run_id "$current")" "$current_status" "$history_status" >&2
        cycle_error "$run_id" "stop-terminal-$history_status" 1
        return 1
        ;;
    esac
    sleep 1
  done
  printf 'stop timeout: target_run_id=%s current_run_id=%s current_run_status=%s target_history_status=%s\n' \
    "$run_id" "$(current_run_id "$current")" "${current_status:-unknown}" "${history_status:-not-polled}" >&2
  cycle_error "$run_id" stop-timeout 1
}

report_event metadata pi5-gate started "mode=$MODE topology=$TOPOLOGY" ""
if [ -n "$REF" ]; then
  require_step immutable-ref resolve_immutable_tag
  report_event candidate immutable-tag resolved "candidate_ref=$CANDIDATE_REF candidate_sha=$CANDIDATE_SHA expected_sha=$EXPECTED_SHA canonical_tag_object_sha=$CANONICAL_TAG_OBJECT_SHA" ""
else
  require_step pre-tag-candidate resolve_pre_tag_candidate
  report_event candidate pre-tag resolved "candidate_ref=$CANDIDATE_REF candidate_sha=$CANDIDATE_SHA expected_sha=$EXPECTED_SHA" ""
fi
require_step installed-ref check_installed_ref
if [ "$MODE" = clean-install ]; then report_event operator clean-install-acknowledged verified "operator performed reimage/install outside this gate" ""; fi
require_step services check_services
require_step api-login-protected check_api_and_auth
require_step storage-integrity check_storage_integrity
require_step start-stop-standard start_and_stop_cycle standard
require_step start-stop-multi-domain start_and_stop_cycle multi_domain
require_step no-leftovers inspect_leftovers
