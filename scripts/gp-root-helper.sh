#!/bin/sh
set -eu

INITIAL_COMMAND="${1:-}"
CONFIG_FILE="${GP_ROOT_HELPER_CONFIG:-/etc/default/gp-control-plane-root-helper}"
# Strict updates deliberately do not consume caller-controlled environment or
# configuration.  The fixed installation profile is loaded later as data.
if [ "$INITIAL_COMMAND" != "queue-update" ]; then
  [ -r "$CONFIG_FILE" ] && . "$CONFIG_FILE"
fi
ZAPRET_DIR="${ZAPRET_DIR:-/opt/zapret2}"
RUN_REGISTRY_DIR="${GP_ROOT_HELPER_RUN_DIR:-/run/gp-control-plane/runs}"
DISCOVERY_GATE_DIR='/run/gp-control-plane/gates'
DISCOVERY_GATE_FILE="$DISCOVERY_GATE_DIR/discovery-update.lock"

fail() {
  printf 'gp-root-helper: %s\n' "$1" >&2
  exit 126
}

require_root() {
  [ "$(id -u)" -eq 0 ] || fail "must be executed as root"
}

real_path() {
  readlink -f "$1" 2>/dev/null || printf '%s\n' "$1"
}

validate_signal() {
  case "$1" in
    TERM|KILL|INT|HUP) printf '%s\n' "$1" ;;
    *) fail "unsupported signal: $1" ;;
  esac
}

validate_pid() {
  case "$1" in
    ''|0|0*|*[!0-9]*) fail "invalid pid: $1" ;;
    *) printf '%s\n' "$1" ;;
  esac
}

validate_env_assignment() {
  case "$1" in
    *=*) ;;
    *) fail "invalid env assignment" ;;
  esac
  key="${1%%=*}"
  case "$key" in
    BATCH|DOMAINS|IPVS|TEST|SKIP_DNSCHECK|SKIP_IPBLOCK|ENABLE_HTTP|ENABLE_HTTPS_TLS12|ENABLE_HTTPS_TLS13|ENABLE_HTTP3|SCANLEVEL|REPEATS|PARALLEL|CURL_MAX_TIME|CURL_MAX_TIME_QUIC|CURL_MAX_TIME_DOH|GP_MD_CURL_PARALLELISM|ZAPRET_BASE|ZAPRET_RW) ;;
    *) fail "unsupported env key: $key" ;;
  esac
}

validate_run_target() {
  [ "$#" -ge 1 ] || fail "run target is required"
  target="$(real_path "$1")"
  zapret_blockcheck="$(real_path "$ZAPRET_DIR/blockcheck2.sh")"
  case "$target" in
    "$zapret_blockcheck") ;;
    *) fail "unsupported run target: $1" ;;
  esac
  [ -x "$target" ] || fail "run target is not executable: $target"
  printf '%s\n' "$target"
}

validate_update_ref() {
  ref="${1:-}"
  [ -n "$ref" ] || fail "release ref is required"
  case "$ref" in
    *..*|/*|*\\*|*[!A-Za-z0-9._/-]*) fail "unsupported release ref: $ref" ;;
    *) printf '%s\n' "$ref" ;;
  esac
}

validate_update_candidate_ref() {
  candidate_ref="${1:-}"
  case "$candidate_ref" in
    refs/tags/*) ;;
    *) fail "candidate ref must be a typed tag under refs/tags/: $candidate_ref" ;;
  esac
  case "$candidate_ref" in
    *..*|*@\{*|*//|*/.|*/|*\\*|*[!A-Za-z0-9._/-]*) fail "unsupported candidate ref: $candidate_ref" ;;
  esac
  git check-ref-format "$candidate_ref" || fail "invalid candidate ref: $candidate_ref"
  printf '%s\n' "$candidate_ref"
}

validate_expected_sha() {
  expected_sha="${1:-}"
  case "$expected_sha" in
    *[!0-9a-f]*) fail "expected SHA must be 40 lowercase hexadecimal characters" ;;
  esac
  [ "${#expected_sha}" -eq 40 ] || fail "expected SHA must be 40 lowercase hexadecimal characters"
  printf '%s\n' "$expected_sha"
}

shell_quote() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

run_target() {
  target="$(validate_run_target "$@")"
  shift
  exec "$target" "$@"
}

ensure_discovery_gate() {
  [ ! -L "$DISCOVERY_GATE_DIR" ] || fail "discovery gate directory must not be a symlink: $DISCOVERY_GATE_DIR"
  install -d -m 0700 -o root -g root "$DISCOVERY_GATE_DIR"
  [ -d "$DISCOVERY_GATE_DIR" ] && [ ! -L "$DISCOVERY_GATE_DIR" ] || fail "discovery gate directory is unsafe: $DISCOVERY_GATE_DIR"
  [ "$(stat -c '%u:%g:%a' "$DISCOVERY_GATE_DIR" 2>/dev/null || true)" = '0:0:700' ] || fail "discovery gate directory must be root:root mode 0700: $DISCOVERY_GATE_DIR"
  if [ ! -e "$DISCOVERY_GATE_FILE" ] && [ ! -L "$DISCOVERY_GATE_FILE" ]; then
    umask 077
    : > "$DISCOVERY_GATE_FILE" || fail "cannot create discovery gate: $DISCOVERY_GATE_FILE"
    chown root:root "$DISCOVERY_GATE_FILE" || fail "cannot own discovery gate: $DISCOVERY_GATE_FILE"
    chmod 0600 "$DISCOVERY_GATE_FILE" || fail "cannot protect discovery gate: $DISCOVERY_GATE_FILE"
  fi
  [ -f "$DISCOVERY_GATE_FILE" ] && [ ! -L "$DISCOVERY_GATE_FILE" ] || fail "discovery gate must be a regular non-symlink file: $DISCOVERY_GATE_FILE"
  [ "$(stat -c '%u:%g:%a' "$DISCOVERY_GATE_FILE" 2>/dev/null || true)" = '0:0:600' ] || fail "discovery gate must be root:root mode 0600: $DISCOVERY_GATE_FILE"
}

with_discovery_gate() {
  ensure_discovery_gate
  command -v flock >/dev/null 2>&1 || fail "flock is required for discovery gate"
  # The FD is intentionally inherited by the privileged target so the shared
  # lock covers every blockcheck child, including exec-based entrypoints.
  exec 9<>"$DISCOVERY_GATE_FILE"
  flock -n -s 9 || {
    printf 'gp-root-helper: discovery blocked by strict release update gate\n' >&2
    return 75
  }
  "$@"
}

write_owned_run_record() {
  run_id="$(validate_run_id "$1")"
  pid="$(validate_pid "$2")"
  pgid="$(validate_pid "$3")"
  marker="$4"
  [ -n "$marker" ] || fail "process start marker is required"
  ensure_run_registry
  record="$(registry_record_path "$run_id")"
  umask 077
  tmp_record="$(mktemp "$RUN_REGISTRY_DIR/.${run_id}.XXXXXX")" || return 1
  if ! printf 'helper-v1 %s %s %s\n' "$pid" "$pgid" "$marker" > "$tmp_record" ||
    ! chown root:root "$tmp_record" ||
    ! chmod 0600 "$tmp_record" ||
    ! mv -f "$tmp_record" "$record"; then
    rm -f "$tmp_record"
    return 1
  fi
}

read_owned_run_ready() {
  ready_file="$1"
  [ -e "$ready_file" ] || return 1
  [ -f "$ready_file" ] && [ ! -L "$ready_file" ] || return 2
  ready_contents="$(cat "$ready_file")" || return 2
  case "$ready_contents" in
    'helper-ready-v1 '*) ;;
    *) return 2 ;;
  esac
  ready_pid="${ready_contents#helper-ready-v1 }"
  [ "$ready_contents" = "helper-ready-v1 $ready_pid" ] || return 2
  case "$ready_pid" in
    ''|0|0*|*[!0-9]*) return 2 ;;
  esac
  printf '%s\n' "$ready_pid"
}

write_owned_run_go() {
  go_file="$1"
  go_pid="$(validate_pid "$2")"
  umask 077
  tmp_go="$(mktemp "${go_file}.XXXXXX")" || return 1
  if ! printf 'helper-go-v1 %s\n' "$go_pid" > "$tmp_go" ||
    ! chown root:root "$tmp_go" ||
    ! chmod 0600 "$tmp_go" ||
    ! mv -f "$tmp_go" "$go_file"; then
    rm -f "$tmp_go"
    return 1
  fi
}

wait_for_owned_run_ready() {
  ready_file="$1"
  expected_pid="$(validate_pid "$2")"
  ready_waited=0
  while [ "$ready_waited" -lt 10 ]; do
    if ready_pid="$(read_owned_run_ready "$ready_file")"; then
      [ "$ready_pid" = "$expected_pid" ] || return 2
      printf '%s\n' "$ready_pid"
      return 0
    else
      ready_result="$?"
    fi
    [ "$ready_result" -eq 1 ] || return 2
    if ! kill -0 "$expected_pid" 2>/dev/null; then
      set +e
      wait "$expected_pid" 2>/dev/null
      set -e
      return 1
    fi
    sleep 1
    ready_waited=$((ready_waited + 1))
  done
  return 3
}

read_owned_run_status() {
  status_file="$1"
  [ -e "$status_file" ] || return 1
  [ -f "$status_file" ] && [ ! -L "$status_file" ] || return 2
  status_contents="$(cat "$status_file")" || return 2
  case "$status_contents" in
    'helper-status-v1 '*) ;;
    *) return 2 ;;
  esac
  status_code="${status_contents#helper-status-v1 }"
  [ "$status_contents" = "helper-status-v1 $status_code" ] || return 2
  case "$status_code" in
    [0-9]|[1-9][0-9]|1[0-9][0-9]|2[0-4][0-9]|25[0-5]) printf '%s\n' "$status_code" ;;
    *) return 2 ;;
  esac
}

wait_for_owned_run_status() {
  status_file="$1"
  known_pid="$2"
  known_pgid="$3"
  known_marker="$4"
  while :; do
    if status_code="$(read_owned_run_status "$status_file")"; then
      printf '%s\n' "$status_code"
      return 0
    else
      status_result="$?"
    fi
    [ "$status_result" -eq 1 ] || return 2
    managed_process_matches "$known_pid" "$known_pgid" "$known_marker" || return 1
    sleep 1
  done
}

run_owned_process() {
  run_id="$(validate_run_id "$1")"
  shift
  owned_cleanup_dir=""
  if [ "${1:-}" = --cleanup-dir ]; then
    [ "$#" -ge 3 ] || fail "run-owned cleanup directory requires a target"
    owned_cleanup_dir="$2"
    shift 2
    case "$owned_cleanup_dir" in
      "${TMPDIR:-/tmp}"/gp-root-helper.*) ;;
      *) fail "unsupported owned cleanup directory" ;;
    esac
  fi
  target="$1"
  shift
  lock_dir="$RUN_REGISTRY_DIR/.${run_id}.lock"
  record="$(registry_record_path "$run_id")"
  ready_file="$lock_dir/supervisor-ready"
  go_file="$lock_dir/supervisor-go"
  status_file="$lock_dir/target-status"
  lock_created=0
  supervisor_started=0
  supervisor_attested=0
  abort_in_progress=0
  cleanup_context_done=0
  pid=""
  pgid=""
  marker=""
  remove_unattested_run_lock() {
    [ "$lock_created" = 1 ] || return 0
    [ "$supervisor_started" = 0 ] || return 1
    rm -f "$ready_file" "$go_file"
    rmdir "$lock_dir" 2>/dev/null || return 1
    lock_created=0
  }
  remove_owned_run_artifacts() {
    [ "$supervisor_attested" = 1 ] || return 1
    known_process_group_exists "$pid" "$pgid" && return 1
    set +e
    wait "$pid" 2>/dev/null
    set -e
    known_process_group_exists "$pid" "$pgid" && return 1
    managed_process_is_gone "$pid" || return 1
    rm -f "$record" "$ready_file" "$go_file" "$status_file"
    rmdir "$lock_dir" 2>/dev/null || return 1
    supervisor_attested=0
  }
  stop_unattested_supervisor() {
    [ "$supervisor_started" = 1 ] || return 0
    kill -TERM "$pid" 2>/dev/null || true
    set +e
    wait "$pid" 2>/dev/null
    set -e
    managed_process_is_gone "$pid" || return 1
    supervisor_started=0
  }
  cleanup_owned_run() {
    if [ "$supervisor_attested" = 1 ]; then
      if terminate_known_process_group "$pid" "$pgid" "$marker" TERM; then
        remove_owned_run_artifacts
      else
        cleanup_status="$?"
        if ! known_process_group_exists "$pid" "$pgid" && managed_process_is_gone "$pid"; then
          remove_owned_run_artifacts || return "$cleanup_status"
          return 0
        fi
        return "$cleanup_status"
      fi
    elif [ "$supervisor_started" = 1 ]; then
      stop_unattested_supervisor || return 1
      remove_unattested_run_lock
    else
      remove_unattested_run_lock
    fi
  }
  cleanup_owned_lifecycle() {
    if cleanup_owned_run; then
      cleanup_status=0
    else
      cleanup_status="$?"
    fi
    if [ -n "$owned_cleanup_dir" ] && [ "$cleanup_context_done" = 0 ]; then
      cleanup_context_done=1
      if [ -e "$owned_cleanup_dir" ] || [ -L "$owned_cleanup_dir" ]; then
        [ -d "$owned_cleanup_dir" ] && [ ! -L "$owned_cleanup_dir" ] || return 126
        rm -rf -- "$owned_cleanup_dir" || return 126
      fi
    fi
    return "$cleanup_status"
  }
  abort_owned_run() {
    abort_status="$1"
    [ "$abort_in_progress" = 0 ] || exit "$abort_status"
    abort_in_progress=1
    trap '' HUP INT TERM
    cleanup_owned_lifecycle || true
    trap - EXIT HUP INT TERM
    exit "$abort_status"
  }
  trap cleanup_owned_lifecycle EXIT
  trap 'abort_owned_run 129' HUP
  trap 'abort_owned_run 130' INT
  trap 'abort_owned_run 143' TERM
  ensure_run_registry
  umask 077
  mkdir "$lock_dir" 2>/dev/null || fail "run is already active"
  lock_created=1
  if registered_process_matches "$run_id" >/dev/null 2>&1; then
    fail "run is already active"
  fi
  [ ! -e "$record" ] && [ ! -L "$record" ] || fail "run record exists; recover it before starting a new run"
  command -v setsid >/dev/null 2>&1 || fail "setsid is required for managed runs"
  setsid /bin/sh -c '
    lock_dir="$1"
    ready_file="$2"
    go_file="$3"
    status_file="$4"
    shift 4
    trap - HUP INT TERM
    umask 077
    tmp_ready="$(mktemp "${ready_file}.XXXXXX")" || exit 125
    if ! printf "helper-ready-v1 %s\\n" "$$" > "$tmp_ready" ||
      ! chown root:root "$tmp_ready" ||
      ! chmod 0600 "$tmp_ready" ||
      ! mv -f "$tmp_ready" "$ready_file"; then
      rm -f "$tmp_ready"
      exit 125
    fi
    while :; do
      [ -d "$lock_dir" ] || exit 125
      if [ -e "$go_file" ]; then
        [ -f "$go_file" ] && [ ! -L "$go_file" ] || exit 125
        go_contents="$(cat "$go_file")" || exit 125
        [ "$go_contents" = "helper-go-v1 $$" ] || exit 125
        break
      fi
      sleep 1
    done
    ( trap - HUP INT TERM; exec "$@" ) &
    target_pid="$!"
    set +e
    wait "$target_pid"
    target_code="$?"
    set -e
    umask 077
    tmp_status="$(mktemp "${status_file}.XXXXXX")" || exit 125
    if ! printf "helper-status-v1 %s\\n" "$target_code" > "$tmp_status" ||
      ! chown root:root "$tmp_status" ||
      ! chmod 0600 "$tmp_status" ||
      ! mv -f "$tmp_status" "$status_file"; then
      rm -f "$tmp_status"
      exit 125
    fi
    trap "" HUP INT TERM
    while :; do
      sleep 2147483647 &
      wait "$!"
    done
  ' gp-owned-supervisor "$lock_dir" "$ready_file" "$go_file" "$status_file" "$target" "$@" &
  pid="$!"
  supervisor_started=1
  if ready_pid="$(wait_for_owned_run_ready "$ready_file" "$pid")"; then
    :
  else
    ready_result="$?"
    case "$ready_result" in
      2) fail "managed supervisor ready file is invalid" ;;
      3) fail "managed supervisor did not become ready" ;;
      *) fail "managed supervisor exited before ready" ;;
    esac
  fi
  marker="$(process_start_time "$pid" 2>/dev/null || true)"
  pgid="$(process_group_id "$pid" 2>/dev/null || true)"
  session="$(process_session_id "$pid" 2>/dev/null || true)"
  if [ -z "$marker" ] || [ -z "$pgid" ] || [ "$pgid" != "$pid" ] || [ "$session" != "$pid" ] ||
    ! managed_process_matches "$pid" "$pgid" "$marker"; then
    fail "managed process exited before registration"
  fi
  supervisor_attested=1
  if ! write_owned_run_record "$run_id" "$pid" "$pgid" "$marker"; then
    abort_owned_run 126
  fi
  if ! write_owned_run_go "$go_file" "$pid"; then
    abort_owned_run 126
  fi
  if code="$(wait_for_owned_run_status "$status_file" "$pid" "$pgid" "$marker")"; then
    :
  else
    status_result="$?"
    cleanup_owned_lifecycle || true
    trap - EXIT HUP INT TERM
    if [ "$status_result" -eq 2 ]; then
      fail "managed target status is invalid"
    fi
    fail "managed supervisor exited before target status"
  fi
  if cleanup_owned_lifecycle; then
    :
  else
    cleanup_status="$?"
    trap - EXIT HUP INT TERM
    fail "managed process group could not be safely cleaned up (status $cleanup_status)"
  fi
  trap - EXIT HUP INT TERM
  return "$code"
}

run_owned_target() {
  [ "$#" -ge 2 ] || fail "run-owned requires run id and target"
  run_id="$(validate_run_id "$1")"
  shift
  target="$(validate_run_target "$@")"
  shift
  run_owned_process "$run_id" "$target" "$@"
}

write_multidomain_runner() {
  source="$1"
  runner="$2"
  if ! awk '
    $0 == "fsleep_setup" { found=1; exit }
    { print }
    END { if (!found) exit 42 }
  ' "$source" > "$runner"; then
    printf 'gp-root-helper: unsupported blockcheck2.sh layout: main marker not found\n' >&2
    return 126
  fi
  cat >> "$runner" <<'RUNNER' || return 126

gp_md_primary_domain()
{
	local d
	for d in $DOMAINS; do
		echo "$d"
		return
	done
}

gp_md_resolve_all_ips()
{
	local d ips all_ips
	for d in $DOMAINS; do
		mdig_resolve_all $IPV ips "$d"
		all_ips="${all_ips:+$all_ips }$ips"
	done
	echo "$all_ips" | tr ' ' '\n' | sort -u | tr '\n' ' '
}

gp_md_normalize_ip_list()
{
	local ip result
	for ip in $1; do
		result="${result:+$result }$ip"
	done
	echo "$result"
}

gp_md_parallel_limit()
{
	local n="${GP_MD_CURL_PARALLELISM:-4}"
	case "$n" in
		""|*[!0-9]*) n=4 ;;
	esac
	n=$((n + 0))
	[ "$n" -lt 1 ] && n=1
	echo "$n"
}

gp_md_out_file()
{
	echo "${PARALLEL_OUT}_md_$1.out"
}

gp_md_code_file()
{
	echo "${PARALLEL_OUT}_md_$1.code"
}

gp_md_run_domain_curl()
{
	# $1 - index
	# $2 - test function
	# $3 - domain
	local idx=$1 testf=$2 gp_domain="$3" code out codefile
	out="$(gp_md_out_file "$idx")"
	codefile="$(gp_md_code_file "$idx")"
	curl_test "$testf" "$gp_domain" >"$out" 2>&1
	code=$?
	echo "$code" >"$codefile"
	return 0
}

gp_md_collect_record()
{
	# $1 - pid:index:domain
	# $2 - test function
	# $3 - strategy text
	local record="$1" testf=$2 strategy_text="$3" pid rest idx gp_domain code out codefile
	pid="${record%%:*}"
	rest="${record#*:}"
	idx="${rest%%:*}"
	gp_domain="${rest#*:}"

	wait "$pid" 2>/dev/null
	out="$(gp_md_out_file "$idx")"
	codefile="$(gp_md_code_file "$idx")"
	code="$(cat "$codefile" 2>/dev/null)"
	[ -n "$code" ] || code=1

	echo "- $testf ipv$IPV $gp_domain : $PKTWSD ${WF:+$WF }$strategy_text"
	[ -f "$out" ] && cat "$out"
	rm -f "$out" "$codefile"
	if [ "$code" = 0 ]; then
		echo "!!!!! $testf: working strategy found for ipv$IPV $gp_domain : nfqws2 ${WF:+$WF }$strategy_text !!!!!"
		report_append "$gp_domain" "$testf ipv${IPV}" "$PKTWSD ${WF:+$WF }$strategy_text"
		return 0
	fi
	echo "GP-MULTIDOMAIN unavailable code=$code"
	return 1
}

pktws_curl_test_update()
{
	# $1 - curl test function
	# $2 - sample domain from the standard zapret2 script
	# $3+ - nfqws2 args
	local testf=$1 dom="$2" strategy ok=0 total=0 gp_domain idx=0 limit active=0 pending record
	shift
	shift
	strategy="$*"
	limit="$(gp_md_parallel_limit)"
	rm -f "${PARALLEL_OUT}_md_"*

	echo
	echo "- gp_multidomain_strategy ipv$IPV parallel=$limit : $PKTWSD ${WF:+$WF }$strategy"
	pktws_start "$@"
	for gp_domain in $DOMAINS; do
		idx=$(($idx + 1))
		total=$(($total + 1))
		gp_md_run_domain_curl "$idx" "$testf" "$gp_domain" &
		record="$!:$idx:$gp_domain"
		pending="${pending:+$pending }$record"
		active=$(($active + 1))
		if [ "$active" -ge "$limit" ]; then
			record="${pending%% *}"
			if [ "$record" = "$pending" ]; then
				pending=
			else
				pending="${pending#* }"
			fi
			gp_md_collect_record "$record" "$testf" "$strategy" && ok=$(($ok + 1))
			active=$(($active - 1))
		fi
	done
	while [ -n "$pending" ]; do
		record="${pending%% *}"
		if [ "$record" = "$pending" ]; then
			pending=
		else
			pending="${pending#* }"
		fi
		gp_md_collect_record "$record" "$testf" "$strategy" && ok=$(($ok + 1))
	done
	ws_kill
	rm -f "${PARALLEL_OUT}_md_"*
	echo "GP-MULTIDOMAIN result: $ok/$total domains available"
	[ "$ok" = "$total" ]
}

gp_md_run_protocol()
{
	# $1 - standard script function
	# $2 - curl test function
	# $3 - tcp/udp
	# $4 - port
	local func=$1 testf=$2 proto=$3 port=$4 ips primary
	primary="$(gp_md_primary_domain)"
	[ -n "$primary" ] || return 1
	ips="$(gp_md_resolve_all_ips)"
	ips="$(gp_md_normalize_ip_list "$ips")"
	[ -n "$ips" ] || {
		echo "GP-MULTIDOMAIN no resolved ip addresses for $proto/$port"
		return 1
	}

	echo
	echo "GP-MULTIDOMAIN preparing $PKTWSD redirection for $proto/$port"
	case "$proto" in
		tcp) pktws_ipt_prepare_tcp "$port" "$ips" ;;
		udp) pktws_ipt_prepare_udp "$port" "$ips" ;;
		*) return 1 ;;
	esac
	test_runner "$func" "$testf" "$primary"
	echo "GP-MULTIDOMAIN clearing $PKTWSD redirection for $proto/$port"
	case "$proto" in
		tcp) pktws_ipt_unprepare_tcp "$port" ;;
		udp) pktws_ipt_unprepare_udp "$port" ;;
	esac
}

fsleep_setup
fix_sbin_path
check_system
check_already
[ "$UNAME" != CYGWIN  -a "$SKIP_PKTWS" != 1 ] && require_root
check_prerequisites
trap sigint_cleanup INT
check_dns
check_virt
ask_params
trap - INT

PID=
NREPORT=
unset WF
trap sigint INT
trap sigsilent PIPE
trap sigsilent HUP
for IPV in $IPVS; do
	configure_ip_version
	[ "$ENABLE_HTTP" = 1 ] && gp_md_run_protocol pktws_check_http curl_test_http tcp "$HTTP_PORT"
	[ "$ENABLE_HTTPS_TLS12" = 1 ] && gp_md_run_protocol pktws_check_https_tls12 curl_test_https_tls12 tcp "$HTTPS_PORT"
	[ "$ENABLE_HTTPS_TLS13" = 1 ] && gp_md_run_protocol pktws_check_https_tls13 curl_test_https_tls13 tcp "$HTTPS_PORT"
	[ "$ENABLE_HTTP3" = 1 ] && gp_md_run_protocol pktws_check_http3 curl_test_http3 udp "$QUIC_PORT"
done
trap - HUP
trap - PIPE
trap - INT

cleanup
RUNNER
  chmod 0700 "$runner" || return 126
}

run_multidomain_target() (
  target="$(validate_run_target "$@")"
  shift
  tmp_dir=""
  runner="$tmp_dir/gp-multidomain-blockcheck.sh"
  cleanup_runner() {
    [ -n "${tmp_dir:-}" ] || return 0
    rm -rf -- "$tmp_dir"
    tmp_dir=""
  }
  abort_multidomain_run() {
    abort_status="$1"
    trap '' HUP INT TERM
    cleanup_runner || true
    trap - EXIT HUP INT TERM
    exit "$abort_status"
  }
  trap cleanup_runner EXIT
  trap 'abort_multidomain_run 129' HUP
  trap 'abort_multidomain_run 130' INT
  trap 'abort_multidomain_run 143' TERM
  tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/gp-root-helper.XXXXXX")"
  runner="$tmp_dir/gp-multidomain-blockcheck.sh"
  write_multidomain_runner "$target" "$runner"
  set +e
  "$runner" "$@"
  code="$?"
  set -e
  exit "$code"
)

run_owned_multidomain_target() (
  [ "$#" -ge 2 ] || fail "run-multidomain-owned requires run id and target"
  run_id="$(validate_run_id "$1")"
  shift
  target="$(validate_run_target "$@")"
  shift
  tmp_dir=""
  cleanup_runner() {
    [ -n "${tmp_dir:-}" ] || return 0
    rm -rf -- "$tmp_dir"
    tmp_dir=""
  }
  abort_multidomain_owned_run() {
    abort_status="$1"
    trap '' HUP INT TERM
    cleanup_runner || true
    trap - EXIT HUP INT TERM
    exit "$abort_status"
  }
  trap cleanup_runner EXIT
  trap 'abort_multidomain_owned_run 129' HUP
  trap 'abort_multidomain_owned_run 130' INT
  trap 'abort_multidomain_owned_run 143' TERM
  tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/gp-root-helper.XXXXXX")"
  runner="$tmp_dir/gp-multidomain-blockcheck.sh"
  write_multidomain_runner "$target" "$runner"
  set +e
  # run_owned_process owns lifecycle traps for its supervisor.  Keep that
  # trap scope in a child shell so it cannot replace this runner's cleanup
  # callback; this outer subshell remains the sole owner of tmp_dir.
  ( run_owned_process "$run_id" "$runner" "$@" )
  code="$?"
  set -e
  exit "$code"
)

STRICT_UPSTREAM='https://github.com/balbomush/GP-access-control-plane.git'
STRICT_INSTALL_PROFILE='/etc/default/gp-control-plane-install-profile'
STRICT_RUN_DIR='/var/lib/gp-control-plane/release-updates'
STRICT_STAGE_DIR='/var/lib/gp-control-plane/strict-updates'
STRICT_BUNDLE_DIR='/var/lib/gp-control-plane/update-bundles'

ensure_strict_root_dir() {
  strict_dir="$1"
  strict_mode="$2"
  [ ! -L "$strict_dir" ] || fail "strict update directory must not be a symlink: $strict_dir"
  install -d -m "$strict_mode" -o root -g root "$strict_dir"
  [ -d "$strict_dir" ] && [ ! -L "$strict_dir" ] || fail "strict update directory is unsafe: $strict_dir"
  [ "$(stat -c '%u:%a' "$strict_dir" 2>/dev/null || true)" = "0:$strict_mode" ] || fail "strict update directory must be root-owned mode $strict_mode: $strict_dir"
}

validate_strict_profile_file() {
  [ -e "$STRICT_INSTALL_PROFILE" ] || [ -L "$STRICT_INSTALL_PROFILE" ] || fail "strict update requires installation profile: $STRICT_INSTALL_PROFILE"
  [ -f "$STRICT_INSTALL_PROFILE" ] && [ ! -L "$STRICT_INSTALL_PROFILE" ] || fail "installation profile must be a regular non-symlink file: $STRICT_INSTALL_PROFILE"
  [ "$(stat -c '%u:%g:%a' "$STRICT_INSTALL_PROFILE" 2>/dev/null || true)" = '0:0:600' ] || fail "installation profile must be root:root mode 0600: $STRICT_INSTALL_PROFILE"
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
      if (!allowed(key) || !quoted(substr($0, equal + 1)) || ++seen[key] != 1) invalid = 1
    }
    END { exit invalid || !seen["GP_INSTALL_USER"] || !seen["GP_INSTALL_DIR"] || !seen["GP_STATE_DIR"] }
  ' "$STRICT_INSTALL_PROFILE" || fail "cannot safely parse installation profile: $STRICT_INSTALL_PROFILE"
}

read_strict_profile_value() {
  strict_key="$1"
  strict_value="$(awk -v key="$strict_key" '
    function decode(value,    length_value, position, character, result) {
      length_value = length(value)
      if (length_value < 2 || substr(value, 1, 1) != "\047") exit 2
      for (position = 2; position <= length_value; position++) {
        character = substr(value, position, 1)
        if (character == "\047") {
          if (position == length_value) { print result; exit 0 }
          if (substr(value, position + 1, 3) != "\\\047\047") exit 2
          result = result "\047"
          position += 3
        } else result = result character
      }
      exit 2
    }
    index($0, key "=") == 1 { found++; value = substr($0, length(key) + 2) }
    END { if (found != 1) exit 3; decode(value) }
  ' "$STRICT_INSTALL_PROFILE")" || fail "cannot read $strict_key from installation profile"
  printf '%s\n' "$strict_value"
}

validate_strict_profile_path() {
  strict_path="$1"
  strict_label="$2"
  case "$strict_path" in
    /*) ;;
    *) fail "$strict_label in installation profile must be an absolute path" ;;
  esac
  case "$strict_path" in
    *'//'*|*'/./'*|*/'.'|*'/../'*|*/'..'|*[![:print:]]*) fail "$strict_label in installation profile is unsafe" ;;
  esac
  printf '%s\n' "$strict_path"
}

# Strict updates may only use the deployed root-owned destinations.  Manual
# installer runs remain configurable; this gate exists because the queued
# runner later executes root writes and restores based on this profile.
strict_require_profile_path() {
  strict_profile_key="$1"
  strict_profile_value="$2"
  strict_profile_expected="$3"
  [ "$strict_profile_value" = "$strict_profile_expected" ] \
    || fail "$strict_profile_key in installation profile is unsupported for strict updates"
}

strict_safe_root_parent_chain() {
  strict_parent="$1"
  strict_label="$2"
  while :; do
    [ -d "$strict_parent" ] && [ ! -L "$strict_parent" ] \
      || fail "$strict_label has a missing or symlinked parent: $strict_parent"
    strict_parent_resolved="$(readlink -f -- "$strict_parent" 2>/dev/null || true)"
    [ "$strict_parent_resolved" = "$strict_parent" ] \
      || fail "$strict_label has a non-canonical parent: $strict_parent"
    strict_parent_uid="$(stat -c '%u' "$strict_parent" 2>/dev/null || true)"
    strict_parent_mode="$(stat -c '%A' "$strict_parent" 2>/dev/null || true)"
    case "$strict_parent_mode" in ?????w*|????????w*) strict_parent_writable=1 ;; *) strict_parent_writable=0 ;; esac
    [ "$strict_parent_uid" = 0 ] && [ "$strict_parent_writable" = 0 ] \
      || fail "$strict_label parent must be root-owned and not group/world-writable: $strict_parent"
    [ "$strict_parent" = / ] && return 0
    strict_parent="$(dirname -- "$strict_parent")"
  done
}

strict_safe_root_target() {
  strict_target="$1"
  strict_target_kind="$2"
  strict_target_label="$3"
  strict_safe_root_parent_chain "$(dirname -- "$strict_target")" "$strict_target_label"
  if [ -e "$strict_target" ] || [ -L "$strict_target" ]; then
    case "$strict_target_kind" in
      file) [ -f "$strict_target" ] && [ ! -L "$strict_target" ] ;;
      directory) [ -d "$strict_target" ] && [ ! -L "$strict_target" ] ;;
      *) return 2 ;;
    esac || fail "$strict_target_label must be a regular non-symlink $strict_target_kind: $strict_target"
    strict_target_resolved="$(readlink -f -- "$strict_target" 2>/dev/null || true)"
    [ "$strict_target_resolved" = "$strict_target" ] \
      || fail "$strict_target_label must be canonical: $strict_target"
    strict_target_uid="$(stat -c '%u' "$strict_target" 2>/dev/null || true)"
    strict_target_mode="$(stat -c '%A' "$strict_target" 2>/dev/null || true)"
    case "$strict_target_mode" in ?????w*|????????w*) strict_target_writable=1 ;; *) strict_target_writable=0 ;; esac
    [ "$strict_target_uid" = 0 ] && [ "$strict_target_writable" = 0 ] \
      || fail "$strict_target_label must be root-owned and not group/world-writable: $strict_target"
  fi
}

validate_strict_privileged_destinations() {
  strict_require_profile_path GP_CORE_ENV_FILE "$strict_core_env_file" /etc/default/gp-control-plane-core
  strict_require_profile_path GP_WEB_ENV_FILE "$strict_web_env_file" /etc/default/gp-control-plane-web
  strict_require_profile_path GP_ROOT_HELPER_PATH "$strict_root_helper_path" /usr/local/libexec/gp-control-plane/gp-root-helper
  strict_require_profile_path GP_ROOT_HELPER_CONFIG "$strict_root_helper_config" /etc/default/gp-control-plane-root-helper
  strict_require_profile_path GP_ROOT_HELPER_RUN_DIR "$strict_root_helper_run_dir" /run/gp-control-plane/runs
  strict_require_profile_path GP_SUDOERS_PATH "$strict_sudoers_path" /etc/sudoers.d/gp-control-plane-root-helper
  strict_require_profile_path GP_ZAPRET_DIR "$strict_zapret_dir" /opt/zapret2
  [ "$strict_install_user" != root ] || fail "GP_INSTALL_USER=root is unsupported for strict updates"
  strict_safe_root_target "$STRICT_INSTALL_PROFILE" file installation-profile
  strict_safe_root_target "$strict_core_env_file" file GP_CORE_ENV_FILE
  strict_safe_root_target "$strict_web_env_file" file GP_WEB_ENV_FILE
  strict_safe_root_target "$strict_root_helper_path" file GP_ROOT_HELPER_PATH
  strict_safe_root_target "$strict_root_helper_config" file GP_ROOT_HELPER_CONFIG
  strict_safe_root_target "$strict_root_helper_run_dir" directory GP_ROOT_HELPER_RUN_DIR
  strict_safe_root_target "$strict_sudoers_path" file GP_SUDOERS_PATH
  strict_safe_root_target "$strict_zapret_dir" directory GP_ZAPRET_DIR
}

load_strict_install_profile() {
  validate_strict_profile_file
  strict_install_user="$(read_strict_profile_value GP_INSTALL_USER)"
  case "$strict_install_user" in ''|*[!A-Za-z0-9_-]*) fail "GP_INSTALL_USER in installation profile is unsafe" ;; esac
  strict_target_entry="$(getent passwd "$strict_install_user" || true)"
  [ -n "$strict_target_entry" ] || fail "installation profile user does not exist: $strict_install_user"
  strict_target_home="$(printf '%s\n' "$strict_target_entry" | cut -d: -f6)"
  [ -n "$strict_target_home" ] || fail "installation profile user has no home: $strict_install_user"
  strict_install_dir="$(validate_strict_profile_path "$(read_strict_profile_value GP_INSTALL_DIR)" GP_INSTALL_DIR)"
  strict_state_dir="$(validate_strict_profile_path "$(read_strict_profile_value GP_STATE_DIR)" GP_STATE_DIR)"
  strict_core_env_file="$(validate_strict_profile_path "$(read_strict_profile_value GP_CORE_ENV_FILE)" GP_CORE_ENV_FILE)"
  strict_web_env_file="$(validate_strict_profile_path "$(read_strict_profile_value GP_WEB_ENV_FILE)" GP_WEB_ENV_FILE)"
  strict_root_helper_path="$(validate_strict_profile_path "$(read_strict_profile_value GP_ROOT_HELPER_PATH)" GP_ROOT_HELPER_PATH)"
  strict_root_helper_config="$(validate_strict_profile_path "$(read_strict_profile_value GP_ROOT_HELPER_CONFIG)" GP_ROOT_HELPER_CONFIG)"
  strict_root_helper_run_dir="$(validate_strict_profile_path "$(read_strict_profile_value GP_ROOT_HELPER_RUN_DIR)" GP_ROOT_HELPER_RUN_DIR)"
  strict_sudoers_path="$(validate_strict_profile_path "$(read_strict_profile_value GP_SUDOERS_PATH)" GP_SUDOERS_PATH)"
  strict_zapret_dir="$(validate_strict_profile_path "$(read_strict_profile_value GP_ZAPRET_DIR)" GP_ZAPRET_DIR)"
  strict_core_service="$(read_strict_profile_value GP_CORE_SERVICE_NAME)"
  strict_web_service="$(read_strict_profile_value GP_SERVICE_NAME)"
  strict_install_web="$(read_strict_profile_value GP_INSTALL_WEB)"
  case "$strict_core_service" in gp-control-plane-core.service) ;; *) fail "GP_CORE_SERVICE_NAME in installation profile is unsupported" ;; esac
  case "$strict_web_service" in gp-control-plane-web.service) ;; *) fail "GP_SERVICE_NAME in installation profile is unsupported" ;; esac
  case "$strict_install_web" in on|off) ;; *) fail "GP_INSTALL_WEB in installation profile is unsafe" ;; esac
  validate_strict_privileged_destinations
}

strict_canonical_directory() {
  strict_directory="$1"
  strict_label="$2"
  [ -d "$strict_directory" ] && [ ! -L "$strict_directory" ] || fail "$strict_label must be an existing non-symlink directory: $strict_directory"
  strict_directory_resolved="$(readlink -f -- "$strict_directory" 2>/dev/null || true)"
  [ "$strict_directory_resolved" = "$strict_directory" ] || fail "$strict_label must be canonical and contain no symlink components: $strict_directory"
  printf '%s\n' "$strict_directory_resolved"
}

prepare_strict_state_layout() {
  strict_install_dir_resolved="$(strict_canonical_directory "$strict_install_dir" GP_INSTALL_DIR)"
  strict_state_dir_resolved="$(strict_canonical_directory "$strict_state_dir" GP_STATE_DIR)"
  strict_state_layout=external
  strict_state_relative=""
  strict_data_root=""
  case "$strict_state_dir_resolved" in
    "$strict_install_dir_resolved"/*)
      strict_state_layout=internal
      strict_state_relative="${strict_state_dir_resolved#"$strict_install_dir_resolved"/}"
      strict_install_parent="$(dirname -- "$strict_install_dir_resolved")"
      strict_install_base="$(basename -- "$strict_install_dir_resolved")"
      strict_data_root="$strict_install_parent/.${strict_install_base}.data"
      [ ! -e "$strict_data_root" ] && [ ! -L "$strict_data_root" ] || fail "strict state migration target already exists: $strict_data_root"
      ;;
  esac
}

queue_strict_update() {
  candidate_ref="$(validate_update_candidate_ref "$1")"
  expected_sha="$(validate_expected_sha "$2")"
  load_strict_install_profile
  prepare_strict_state_layout
  ensure_strict_root_dir "$STRICT_RUN_DIR" 700
  ensure_strict_root_dir "$STRICT_STAGE_DIR" 700
  ensure_strict_root_dir "$STRICT_BUNDLE_DIR" 711

  stamp="$(date -u +%Y%m%dT%H%M%SZ)-$$"
  unit="gp-control-plane-update-$stamp"
  script="$STRICT_RUN_DIR/$unit.sh"
  log_file="$STRICT_RUN_DIR/$unit.log"
  stage_root="$STRICT_STAGE_DIR/$unit"
  bundle="$STRICT_BUNDLE_DIR/$unit.tar"

  [ ! -e "$script" ] && [ ! -L "$script" ] && [ ! -e "$log_file" ] && [ ! -L "$log_file" ] && [ ! -e "$stage_root" ] && [ ! -L "$stage_root" ] && [ ! -e "$bundle" ] && [ ! -L "$bundle" ] || fail "strict update queue entry already exists"
  umask 077
  : > "$log_file"
  chown root:root "$log_file"
  chmod 0600 "$log_file"
  cat > "$script" <<SCRIPT
#!/bin/sh
set -eu
umask 077
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
STRICT_UPSTREAM=$(shell_quote "$STRICT_UPSTREAM")
STRICT_INSTALL_PROFILE=$(shell_quote "$STRICT_INSTALL_PROFILE")
STRICT_STAGE_ROOT=$(shell_quote "$stage_root")
STRICT_BUNDLE=$(shell_quote "$bundle")
STRICT_USER=$(shell_quote "$strict_install_user")
STRICT_INSTALL_DIR=$(shell_quote "$strict_install_dir")
STRICT_STATE_DIR=$(shell_quote "$strict_state_dir")
STRICT_STATE_LAYOUT=$(shell_quote "$strict_state_layout")
STRICT_STATE_RELATIVE=$(shell_quote "$strict_state_relative")
STRICT_DATA_ROOT=$(shell_quote "$strict_data_root")
STRICT_CORE_SERVICE=$(shell_quote "$strict_core_service")
STRICT_WEB_SERVICE=$(shell_quote "$strict_web_service")
STRICT_INSTALL_WEB=$(shell_quote "$strict_install_web")
STRICT_CORE_ENV_FILE=$(shell_quote "$strict_core_env_file")
STRICT_WEB_ENV_FILE=$(shell_quote "$strict_web_env_file")
STRICT_REF=$(shell_quote "$candidate_ref")
STRICT_SHA=$(shell_quote "$expected_sha")
STRICT_DISCOVERY_GATE_FILE=$(shell_quote "$DISCOVERY_GATE_FILE")
exec > $(shell_quote "$log_file") 2>&1

strict_fail() { echo 'status=failed'; echo "error=\$1"; exit 126; }
strict_git() { env -i PATH="\$PATH" HOME=/nonexistent GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0 git "\$@"; }
strict_sha() { strict_sha_value="\${1:-}"; case "\$strict_sha_value" in ''|*[!0-9a-f]*) return 1 ;; esac; [ "\${#strict_sha_value}" -eq 40 ]; }
strict_safe_config_parent_chain() {
  strict_parent="\$1"
  while :; do
    [ -d "\$strict_parent" ] && [ ! -L "\$strict_parent" ] || return 1
    [ "\$(readlink -f -- "\$strict_parent" 2>/dev/null || true)" = "\$strict_parent" ] || return 1
    strict_parent_uid="\$(stat -c '%u' "\$strict_parent" 2>/dev/null || true)"
    strict_parent_mode="\$(stat -c '%A' "\$strict_parent" 2>/dev/null || true)"
    case "\$strict_parent_mode" in ?????w*|????????w*) strict_parent_writable=1 ;; *) strict_parent_writable=0 ;; esac
    [ "\$strict_parent_uid" = 0 ] && [ "\$strict_parent_writable" = 0 ] || return 1
    [ "\$strict_parent" = / ] && return 0
    strict_parent="\$(dirname -- "\$strict_parent")"
  done
}
strict_safe_config_target() {
  strict_target="\$1"
  strict_safe_config_parent_chain "\$(dirname -- "\$strict_target")" || return 1
  if [ -e "\$strict_target" ] || [ -L "\$strict_target" ]; then
    [ -f "\$strict_target" ] && [ ! -L "\$strict_target" ] || return 1
    [ "\$(readlink -f -- "\$strict_target" 2>/dev/null || true)" = "\$strict_target" ] || return 1
    strict_target_uid="\$(stat -c '%u' "\$strict_target" 2>/dev/null || true)"
    strict_target_mode="\$(stat -c '%A' "\$strict_target" 2>/dev/null || true)"
    case "\$strict_target_mode" in ?????w*|????????w*) strict_target_writable=1 ;; *) strict_target_writable=0 ;; esac
    [ "\$strict_target_uid" = 0 ] && [ "\$strict_target_writable" = 0 ] || return 1
  fi
}
strict_validate_config_targets() {
  strict_safe_config_target "\$STRICT_INSTALL_PROFILE" &&
    strict_safe_config_target "\$STRICT_CORE_ENV_FILE" &&
    strict_safe_config_target "\$STRICT_WEB_ENV_FILE"
}
strict_acquire_update_gate() {
  strict_gate_dir="\$(dirname "\$STRICT_DISCOVERY_GATE_FILE")"
  [ ! -L "\$strict_gate_dir" ] || strict_fail 'discovery gate directory must not be a symlink'
  install -d -m 0700 -o root -g root "\$strict_gate_dir" || strict_fail 'cannot create discovery gate directory'
  [ -d "\$strict_gate_dir" ] && [ ! -L "\$strict_gate_dir" ] || strict_fail 'discovery gate directory is unsafe'
  [ "\$(stat -c '%u:%g:%a' "\$strict_gate_dir" 2>/dev/null || true)" = '0:0:700' ] || strict_fail 'discovery gate directory ownership check failed'
  if [ ! -e "\$STRICT_DISCOVERY_GATE_FILE" ] && [ ! -L "\$STRICT_DISCOVERY_GATE_FILE" ]; then
    : > "\$STRICT_DISCOVERY_GATE_FILE" || strict_fail 'cannot create discovery gate'
    chown root:root "\$STRICT_DISCOVERY_GATE_FILE" || strict_fail 'cannot own discovery gate'
    chmod 0600 "\$STRICT_DISCOVERY_GATE_FILE" || strict_fail 'cannot protect discovery gate'
  fi
  [ -f "\$STRICT_DISCOVERY_GATE_FILE" ] && [ ! -L "\$STRICT_DISCOVERY_GATE_FILE" ] || strict_fail 'discovery gate must be a regular non-symlink file'
  [ "\$(stat -c '%u:%g:%a' "\$STRICT_DISCOVERY_GATE_FILE" 2>/dev/null || true)" = '0:0:600' ] || strict_fail 'discovery gate ownership check failed'
  command -v flock >/dev/null 2>&1 || strict_fail 'flock is required for strict update gate'
  exec 9<>"\$STRICT_DISCOVERY_GATE_FILE"
  flock -n -x 9 || {
    echo 'phase=blocked-discovery-gate'
    echo 'status=failed'
    echo 'error=active discovery holds the release gate'
    exit 75
  }
}

echo "phase=requested"
echo "candidate_ref=\$STRICT_REF"
echo "expected_sha=\$STRICT_SHA"
[ ! -e "\$STRICT_STAGE_ROOT" ] && [ ! -L "\$STRICT_STAGE_ROOT" ] || strict_fail 'strict stage already exists'
install -d -m 0700 -o root -g root "\$STRICT_STAGE_ROOT" || strict_fail 'cannot create strict stage'
[ "\$(stat -c '%u:%a' "\$STRICT_STAGE_ROOT" 2>/dev/null || true)" = '0:700' ] || strict_fail 'strict stage ownership check failed'

remote_refs="\$(strict_git ls-remote "\$STRICT_UPSTREAM" "\$STRICT_REF" "\${STRICT_REF}^{}")" || strict_fail 'cannot read canonical tag'
direct_sha="\$(printf '%s\\n' "\$remote_refs" | awk -v ref="\$STRICT_REF" '\$2 == ref { count++; value = \$1 } END { if (count == 1) print value; else exit 1 }')" || strict_fail 'canonical direct tag is ambiguous'
peeled_sha="\$(printf '%s\\n' "\$remote_refs" | awk -v ref="\${STRICT_REF}^{}" '\$2 == ref { count++; value = \$1 } END { if (count == 1) print value; else exit 0 }')" || strict_fail 'canonical peeled tag is ambiguous'
strict_sha "\$direct_sha" >/dev/null || strict_fail 'canonical direct tag SHA is invalid'
if [ -n "\$peeled_sha" ]; then strict_sha "\$peeled_sha" >/dev/null || strict_fail 'canonical peeled tag SHA is invalid'; fi
verified_sha="\$direct_sha"
[ -n "\$peeled_sha" ] && verified_sha="\$peeled_sha"
[ "\$verified_sha" = "\$STRICT_SHA" ] || strict_fail 'canonical tag does not match expected SHA'

verify_repo="\$STRICT_STAGE_ROOT/verify"
strict_git init "\$verify_repo" >/dev/null || strict_fail 'cannot initialize verification repository'
chown root:root "\$verify_repo" && chmod 0700 "\$verify_repo" || strict_fail 'cannot protect verification repository'
strict_git -C "\$verify_repo" fetch --no-tags "\$STRICT_UPSTREAM" "\$STRICT_REF" >/dev/null || strict_fail 'verification fetch failed'
fetch_sha="\$(strict_git -C "\$verify_repo" rev-parse --verify FETCH_HEAD^{commit})" || strict_fail 'verification fetch did not resolve a commit'
[ "\$fetch_sha" = "\$STRICT_SHA" ] || strict_fail 'verification fetch SHA does not match expected SHA'
echo 'phase=verified'
echo "verified_ref=\$STRICT_REF"
echo "verified_sha=\$fetch_sha"

stage_repo="\$STRICT_STAGE_ROOT/repo"
strict_git init "\$stage_repo" >/dev/null || strict_fail 'cannot initialize fresh strict stage repository'
chown root:root "\$stage_repo" && chmod 0700 "\$stage_repo" || strict_fail 'cannot protect strict stage repository'
strict_git -C "\$stage_repo" fetch --no-tags "\$STRICT_UPSTREAM" "\$STRICT_REF" >/dev/null || strict_fail 'stage fetch failed'
stage_fetch_sha="\$(strict_git -C "\$stage_repo" rev-parse --verify FETCH_HEAD^{commit})" || strict_fail 'stage fetch did not resolve a commit'
[ "\$stage_fetch_sha" = "\$STRICT_SHA" ] || strict_fail 'stage fetch SHA does not match expected SHA'
strict_git -C "\$stage_repo" checkout --detach "\$STRICT_SHA" >/dev/null || strict_fail 'cannot check out strict stage'
stage_head="\$(strict_git -C "\$stage_repo" rev-parse --verify HEAD^{commit})" || strict_fail 'cannot resolve staged commit'
[ "\$stage_head" = "\$STRICT_SHA" ] || strict_fail 'staged commit does not match expected SHA'
[ -f "\$stage_repo/scripts/install-linux.sh" ] || [ -f "\$stage_repo/scripts/install-raspberry-pi.sh" ] || strict_fail 'staged installer is missing'
installer="\$stage_repo/scripts/install-linux.sh"
[ -f "\$installer" ] || installer="\$stage_repo/scripts/install-raspberry-pi.sh"
[ -f "\$installer" ] || strict_fail 'staged installer is missing'
grep -F -- 'GP_TRUSTED_SOURCE_DIR' "\$installer" >/dev/null || strict_fail 'staged installer does not implement the trusted-source strict-update contract'
tar -C "\$stage_repo" --exclude='./build/state' -cf "\$STRICT_BUNDLE" . || strict_fail 'cannot create staged publication bundle'
chown root:root "\$STRICT_BUNDLE" && chmod 0444 "\$STRICT_BUNDLE" || strict_fail 'cannot protect staged publication bundle'
echo 'phase=staged'
echo "staged_sha=\$stage_head"

# The staged installer independently validates the same fixed root:root 0600
# profile and the root-owned checkout before this script publishes any code.
if ! env -i PATH="\$PATH" HOME=/root GP_INSTALL_FORCE_CLEAN=on GP_UPDATE_CANDIDATE_REF="\$STRICT_REF" GP_UPDATE_EXPECTED_SHA="\$STRICT_SHA" GP_TRUSTED_SOURCE_DIR="\$stage_repo" bash "\$installer" --strict-preflight; then
  strict_fail 'staged installer preflight failed before publication'
fi

# Take the exclusive gate only after all network/tag/stage/preflight work.
# FD 9 stays open in this runner until publication, installer, rollback, and
# service restart have either completed or the runner exits.
strict_acquire_update_gate

runuser -u "\$STRICT_USER" -- /bin/sh -s -- "\$STRICT_BUNDLE" "\$STRICT_INSTALL_DIR" <<'USER_PUBLISH' || strict_fail 'target-user publication failed; existing worktree retained or recovery directory was left'
set -eu
bundle="\$1"
install_dir="\$2"
case "\$install_dir" in /*) ;; *) exit 126 ;; esac
parent="\$(dirname "\$install_dir")"
base="\$(basename "\$install_dir")"
publish_dir="\$parent/.\${base}.strict-publish"
[ -d "\$install_dir" ] && [ ! -L "\$install_dir" ] || exit 126
[ ! -e "\$publish_dir" ] && [ ! -L "\$publish_dir" ] || exit 126
mkdir -m 0700 "\$publish_dir"
tar -C "\$publish_dir" -xpf "\$bundle"
[ -d "\$publish_dir/scripts" ] || exit 126
next="\$parent/.\${base}.strict-next"
previous="\$parent/.\${base}.strict-previous"
[ ! -e "\$next" ] && [ ! -L "\$next" ] && [ ! -e "\$previous" ] && [ ! -L "\$previous" ] || exit 126
mv "\$publish_dir" "\$next"
mv "\$install_dir" "\$previous" || exit 126
if ! mv "\$next" "\$install_dir"; then
  [ ! -e "\$install_dir" ] && mv "\$previous" "\$install_dir" || true
  exit 126
fi
USER_PUBLISH
echo 'phase=published'
echo "state_layout=\$STRICT_STATE_LAYOUT"
if [ "\$STRICT_STATE_LAYOUT" = internal ]; then echo 'state_migration=pending'; else echo 'state_migration=not-required'; fi

STRICT_CONFIG_SNAPSHOT="\$STRICT_STAGE_ROOT/deployment-config"
snapshot_deployment_configuration() {
  strict_validate_config_targets || return 1
  [ ! -e "\$STRICT_CONFIG_SNAPSHOT" ] && [ ! -L "\$STRICT_CONFIG_SNAPSHOT" ] || return 1
  install -d -m 0700 -o root -g root "\$STRICT_CONFIG_SNAPSHOT" || return 1
  snapshot_one() {
    snapshot_source="\$1"
    snapshot_name="\$2"
    [ -f "\$snapshot_source" ] && [ ! -L "\$snapshot_source" ] || return 1
    [ "\$(stat -c '%u:%g' "\$snapshot_source" 2>/dev/null || true)" = '0:0' ] || return 1
    cp -p -- "\$snapshot_source" "\$STRICT_CONFIG_SNAPSHOT/\$snapshot_name"
  }
  snapshot_one "\$STRICT_INSTALL_PROFILE" install-profile || return 1
  snapshot_one "\$STRICT_CORE_ENV_FILE" core-env || return 1
  if [ "\$STRICT_INSTALL_WEB" = on ]; then snapshot_one "\$STRICT_WEB_ENV_FILE" web-env || return 1; fi
  : > "\$STRICT_CONFIG_SNAPSHOT/complete" && chown root:root "\$STRICT_CONFIG_SNAPSHOT/complete" && chmod 0600 "\$STRICT_CONFIG_SNAPSHOT/complete"
}

restore_deployment_configuration() {
  restore_one() {
    restore_source="\$1"
    restore_target="\$2"
    strict_safe_config_target "\$restore_target" || return 1
    [ -f "\$restore_source" ] && [ ! -L "\$restore_source" ] || return 1
    [ ! -e "\$restore_target" ] && [ ! -L "\$restore_target" ] || { [ -f "\$restore_target" ] && [ ! -L "\$restore_target" ]; } || return 1
    install -m "\$(stat -c '%a' "\$restore_source")" -o root -g root "\$restore_source" "\$restore_target" || return 1
  }
  restore_one "\$STRICT_CONFIG_SNAPSHOT/install-profile" "\$STRICT_INSTALL_PROFILE" || return 1
  restore_one "\$STRICT_CONFIG_SNAPSHOT/core-env" "\$STRICT_CORE_ENV_FILE" || return 1
  if [ "\$STRICT_INSTALL_WEB" = on ]; then restore_one "\$STRICT_CONFIG_SNAPSHOT/web-env" "\$STRICT_WEB_ENV_FILE" || return 1; fi
}

rollback_published_code() {
  echo 'phase=rollback'
  if [ -f "\$STRICT_CONFIG_SNAPSHOT/complete" ] && [ ! -L "\$STRICT_CONFIG_SNAPSHOT/complete" ]; then
    restore_deployment_configuration || return 1
  fi
  if runuser -u "\$STRICT_USER" -- /bin/sh -s -- "\$STRICT_INSTALL_DIR" <<'USER_ROLLBACK'
set -eu
install_dir="\$1"
parent="\$(dirname "\$install_dir")"
base="\$(basename "\$install_dir")"
previous="\$parent/.\${base}.strict-previous"
failed="\$parent/.\${base}.strict-failed"
[ -d "\$install_dir" ] && [ ! -L "\$install_dir" ] || exit 126
[ -d "\$previous" ] && [ ! -L "\$previous" ] || exit 126
[ ! -e "\$failed" ] && [ ! -L "\$failed" ] || exit 126
mv "\$install_dir" "\$failed"
if ! mv "\$previous" "\$install_dir"; then
  [ ! -e "\$install_dir" ] && mv "\$failed" "\$install_dir" || true
  exit 126
fi
USER_ROLLBACK
  then
    if systemctl daemon-reload && systemctl restart "\$STRICT_CORE_SERVICE"; then
      if [ "\$STRICT_INSTALL_WEB" = on ]; then
        systemctl restart "\$STRICT_WEB_SERVICE" || return 1
      else
        systemctl stop "\$STRICT_WEB_SERVICE" >/dev/null 2>&1 || true
      fi
      echo 'rollback_scope=code'
      return 0
    fi
  fi
  return 1
}

rollback_after_publication_failure() {
  rollback_reason="\$1"
  if rollback_published_code; then
    strict_fail "\$rollback_reason; previous code was restored and previous services were restarted"
  fi
  strict_fail "\$rollback_reason; code rollback could not be completed"
}

snapshot_deployment_configuration || rollback_after_publication_failure 'cannot safely snapshot deployment configuration before update'

installed_checkout_sha="\$(runuser -u "\$STRICT_USER" -- env -i PATH="\$PATH" HOME=/nonexistent GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0 git -c safe.directory="\$STRICT_INSTALL_DIR" -C "\$STRICT_INSTALL_DIR" rev-parse --verify HEAD^{commit})" || rollback_after_publication_failure 'published worktree SHA could not be verified'
[ "\$installed_checkout_sha" = "\$STRICT_SHA" ] || rollback_after_publication_failure 'published worktree SHA does not match expected SHA'

run_staged_installer() {
  if [ "\$STRICT_STATE_LAYOUT" = internal ]; then
    strict_previous="\$(dirname "\$STRICT_INSTALL_DIR")/.\$(basename "\$STRICT_INSTALL_DIR").strict-previous"
    strict_migration_source="\$strict_previous/\$STRICT_STATE_RELATIVE"
    env -i PATH="\$PATH" HOME=/root GP_INSTALL_FORCE_CLEAN=on GP_UPDATE_CANDIDATE_REF="\$STRICT_REF" GP_UPDATE_EXPECTED_SHA="\$STRICT_SHA" GP_TRUSTED_SOURCE_DIR="\$stage_repo" GP_STRICT_STATE_MIGRATION=on GP_STRICT_STATE_MIGRATION_SOURCE="\$strict_migration_source" GP_STRICT_STATE_MIGRATION_ROOT="\$STRICT_DATA_ROOT" bash "\$installer"
  else
    env -i PATH="\$PATH" HOME=/root GP_INSTALL_FORCE_CLEAN=on GP_UPDATE_CANDIDATE_REF="\$STRICT_REF" GP_UPDATE_EXPECTED_SHA="\$STRICT_SHA" GP_TRUSTED_SOURCE_DIR="\$stage_repo" bash "\$installer"
  fi
}

echo 'phase=root'
if run_staged_installer; then
  systemctl is-active --quiet "\$STRICT_CORE_SERVICE" || rollback_after_publication_failure 'updated core service did not become active'
  if [ "\$STRICT_INSTALL_WEB" = on ]; then systemctl is-active --quiet "\$STRICT_WEB_SERVICE" || rollback_after_publication_failure 'updated web service did not become active'; fi
  runuser -u "\$STRICT_USER" -- /bin/sh -s -- "\$STRICT_INSTALL_DIR" <<'USER_FINALIZE' || rollback_after_publication_failure 'installed code succeeded but previous worktree cleanup failed'
set -eu
install_dir="\$1"
parent="\$(dirname "\$install_dir")"
base="\$(basename "\$install_dir")"
previous="\$parent/.\${base}.strict-previous"
[ -d "\$previous" ] && [ ! -L "\$previous" ] || exit 126
rm -rf -- "\$previous"
USER_FINALIZE
  echo 'phase=installed'
  echo 'status=success'
  echo "verified_ref=\$STRICT_REF"
  echo "verified_sha=\$fetch_sha"
  echo "checked_out_sha=\$stage_head"
  echo "installed_ref=\$STRICT_REF"
  echo "installed_sha=\$installed_checkout_sha"
  echo "state_layout=\$STRICT_STATE_LAYOUT"
  if [ "\$STRICT_STATE_LAYOUT" = internal ]; then echo 'state_migration=completed'; else echo 'state_migration=not-required'; fi
  installed_version="\${STRICT_REF#refs/tags/}"
  installed_version="\${installed_version#v}"
  echo "installed_version=\$installed_version"
else
  rollback_after_publication_failure 'staged installer failed after publication'
fi
SCRIPT
  chown root:root "$script"
  chmod 0700 "$script"

  if command -v systemd-run >/dev/null 2>&1; then
    systemd-run --unit="$unit" --collect --property=Type=oneshot /bin/sh "$script" >/dev/null
  else
    nohup /bin/sh "$script" >/dev/null 2>&1 &
  fi
  printf 'queued=true\nstatus=queued\nphase=queued\nunit=%s\nlog=%s\ncandidate_ref=%s\nexpected_sha=%s\n' "$unit" "$log_file" "$candidate_ref" "$expected_sha"
}

validate_run_id() {
  case "${1:-}" in
    ""|*[!A-Za-z0-9._-]*|.*|*..*) fail "invalid run id" ;;
    *) printf '%s\n' "$1" ;;
  esac
}

registry_record_path() {
  run_id="$(validate_run_id "${1:-}")"
  printf '%s/%s\n' "$RUN_REGISTRY_DIR" "$run_id"
}

ensure_run_registry() {
  install -d -m 0750 -o root -g root "$RUN_REGISTRY_DIR"
}

process_start_time() {
  pid="$(validate_pid "${1:-}")"
  [ -r "/proc/$pid/stat" ] || return 1
  awk '{ sub(/^.*\\) /, ""); print $20 }' "/proc/$pid/stat"
}

process_group_id() {
  pid="$(validate_pid "${1:-}")"
  ps -o pgid= -p "$pid" 2>/dev/null | tr -d '[:space:]'
}

process_session_id() {
  pid="$(validate_pid "${1:-}")"
  ps -o sid= -p "$pid" 2>/dev/null | tr -d '[:space:]'
}

known_process_group_exists() {
  known_pid="$(validate_pid "${1:-}")"
  known_pgid="$(validate_pid "${2:-}")"
  [ "$known_pid" = "$known_pgid" ] || return 1
  ps -e -o pgid= -o sid= 2>/dev/null | awk -v pgid="$known_pgid" -v sid="$known_pid" '
    $1 == pgid && $2 == sid { found = 1; exit }
    END { exit !found }
  '
}

managed_process_matches() {
  known_pid="$(validate_pid "${1:-}")"
  known_pgid="$(validate_pid "${2:-}")"
  known_marker="${3:-}"
  [ -n "$known_marker" ] || return 1
  [ "$known_pid" = "$known_pgid" ] || return 1
  [ "$(process_start_time "$known_pid" 2>/dev/null || true)" = "$known_marker" ] || return 1
  [ "$(process_group_id "$known_pid" 2>/dev/null || true)" = "$known_pgid" ] || return 1
  [ "$(process_session_id "$known_pid" 2>/dev/null || true)" = "$known_pid" ] || return 1
}

managed_process_group_snapshot() {
  known_pid="$(validate_pid "${1:-}")"
  known_pgid="$(validate_pid "${2:-}")"
  [ "$known_pid" = "$known_pgid" ] || return 1
  ps -e -o pid= -o pgid= -o sid= 2>/dev/null | awk -v pgid="$known_pgid" -v sid="$known_pid" '
    $2 == pgid && $3 == sid { print $1 }
  ' | while IFS= read -r known_member_pid; do
    known_member_marker="$(process_start_time "$known_member_pid" 2>/dev/null || true)"
    [ -n "$known_member_marker" ] && printf '%s %s\n' "$known_member_pid" "$known_member_marker"
  done
}

snapshot_member_matches() {
  known_pid="$(validate_pid "${1:-}")"
  known_pgid="$(validate_pid "${2:-}")"
  known_member_pid="$(validate_pid "${3:-}")"
  known_member_marker="${4:-}"
  [ -n "$known_member_marker" ] || return 1
  [ "$known_pid" = "$known_pgid" ] || return 1
  [ "$(process_start_time "$known_member_pid" 2>/dev/null || true)" = "$known_member_marker" ] || return 1
  [ "$(process_group_id "$known_member_pid" 2>/dev/null || true)" = "$known_pgid" ] || return 1
  [ "$(process_session_id "$known_member_pid" 2>/dev/null || true)" = "$known_pid" ] || return 1
}

snapshot_has_live_member() {
  known_pid="$(validate_pid "${1:-}")"
  known_pgid="$(validate_pid "${2:-}")"
  known_snapshot="${3:-}"
  [ "$known_pid" = "$known_pgid" ] || return 1
  while IFS=' ' read -r known_member_pid known_member_marker known_extra; do
    [ -n "$known_member_pid" ] || continue
    [ -z "${known_extra:-}" ] || continue
    if snapshot_member_matches "$known_pid" "$known_pgid" "$known_member_pid" "$known_member_marker"; then
      return 0
    fi
  done <<EOF
$known_snapshot
EOF
  return 1
}

managed_process_is_gone() {
  known_pid="$(validate_pid "${1:-}")"
  [ -z "$(process_start_time "$known_pid" 2>/dev/null || true)" ]
}

terminate_known_process_group() {
  known_pid="$(validate_pid "${1:-}")"
  known_pgid="$(validate_pid "${2:-}")"
  known_marker="${3:-}"
  known_signal="$(validate_signal "${4:-}")"
  [ "$known_pid" = "$known_pgid" ] || fail "managed process group is not isolated"
  [ -n "$known_marker" ] || return 2
  known_waited=0
  managed_process_matches "$known_pid" "$known_pgid" "$known_marker" || return 2
  if [ "$known_signal" = KILL ]; then
    managed_process_matches "$known_pid" "$known_pgid" "$known_marker" || return 2
    kill -KILL -- "-$known_pgid" 2>/dev/null || true
  else
    known_snapshot="$(managed_process_group_snapshot "$known_pid" "$known_pgid")"
    managed_process_matches "$known_pid" "$known_pgid" "$known_marker" || return 2
    kill "-$known_signal" -- "-$known_pgid" 2>/dev/null || true
    while known_process_group_exists "$known_pid" "$known_pgid"; do
      [ "$known_waited" -ge 2 ] && break
      sleep 1
      known_waited=$((known_waited + 1))
    done
    if ! known_process_group_exists "$known_pid" "$known_pgid"; then
      return 0
    fi
    if managed_process_matches "$known_pid" "$known_pgid" "$known_marker"; then
      :
    elif managed_process_is_gone "$known_pid" && snapshot_has_live_member "$known_pid" "$known_pgid" "$known_snapshot"; then
      :
    else
      return 2
    fi
    kill -KILL -- "-$known_pgid" 2>/dev/null || true
  fi
  known_waited=0
  while known_process_group_exists "$known_pid" "$known_pgid"; do
    [ "$known_waited" -ge 2 ] && fail "managed process group did not exit after KILL"
    sleep 1
    known_waited=$((known_waited + 1))
  done
  return 0
}

registered_process_matches() {
  run_id="$(validate_run_id "${1:-}")"
  record="$(registry_record_path "$run_id")"
  [ -f "$record" ] && [ ! -L "$record" ] || return 1
  IFS=' ' read -r version pid pgid marker extra < "$record" || return 1
  [ "$version" = "helper-v1" ] || return 1
  [ -z "${extra:-}" ] || return 1
  validate_pid "$pid" >/dev/null
  validate_pid "$pgid" >/dev/null
  [ -n "$marker" ] || return 1
  managed_process_matches "$pid" "$pgid" "$marker" || return 1
  printf '%s %s %s\n' "$pid" "$pgid" "$marker"
}

signal_registered_process_run() {
  [ "$#" -eq 2 ] || fail "signal-run requires run id and signal"
  run_id="$(validate_run_id "$1")"
  signal="$(validate_signal "$2")"
  record="$(registry_record_path "$run_id")"
  target="$(registered_process_matches "$run_id" || true)"
  if [ -z "$target" ]; then
    rm -f "$record"
    fail "registered process is stale or invalid"
  fi
  pid="${target%% *}"
  target="${target#* }"
  pgid="${target%% *}"
  marker="${target#* }"
  if terminate_known_process_group "$pid" "$pgid" "$marker" "$signal"; then
    rm -f "$record"
  else
    termination_status="$?"
    [ "$termination_status" -eq 2 ] || return "$termination_status"
    rm -f "$record"
    fail "registered process is stale or invalid"
  fi
}

recover_registered_process_runs() {
  ensure_run_registry
  for record in "$RUN_REGISTRY_DIR"/*; do
    [ -e "$record" ] || continue
    run_id="$(basename "$record")"
    if target="$(registered_process_matches "$run_id" 2>/dev/null || true)" && [ -n "$target" ]; then
      pid="${target%% *}"
      target="${target#* }"
      pgid="${target%% *}"
      marker="${target#* }"
      if terminate_known_process_group "$pid" "$pgid" "$marker" TERM; then
        :
      else
        termination_status="$?"
        [ "$termination_status" -eq 2 ] || return "$termination_status"
      fi
    fi
    rm -f "$record"
  done
}

require_root

command="${1:-}"
[ -n "$command" ] || fail "command is required"
shift

case "$command" in
  check)
    ensure_run_registry
    exit 0
    ;;
  signal-run)
    signal_registered_process_run "$@"
    ;;
  recover-runs)
    [ "$#" -eq 0 ] || fail "recover-runs accepts no arguments"
    recover_registered_process_runs
    ;;
  run)
    with_discovery_gate run_target "$@"
    ;;
  run-owned)
    with_discovery_gate run_owned_target "$@"
    ;;
  run-multidomain)
    with_discovery_gate run_multidomain_target "$@"
    ;;
  run-multidomain-owned)
    with_discovery_gate run_owned_multidomain_target "$@"
    ;;
  run-env)
    while [ "$#" -gt 0 ]; do
      if [ "$1" = "--" ]; then
        shift
        break
      fi
      validate_env_assignment "$1"
      export "$1"
      shift
    done
    with_discovery_gate run_target "$@"
    ;;
  run-owned-env)
    run_id="$(validate_run_id "${1:-}")"
    shift
    while [ "$#" -gt 0 ]; do
      if [ "$1" = "--" ]; then
        shift
        break
      fi
      validate_env_assignment "$1"
      export "$1"
      shift
    done
    with_discovery_gate run_owned_target "$run_id" "$@"
    ;;
  run-multidomain-env)
    while [ "$#" -gt 0 ]; do
      if [ "$1" = "--" ]; then
        shift
        break
      fi
      validate_env_assignment "$1"
      export "$1"
      shift
    done
    with_discovery_gate run_multidomain_target "$@"
    ;;
  run-multidomain-owned-env)
    run_id="$(validate_run_id "${1:-}")"
    shift
    while [ "$#" -gt 0 ]; do
      if [ "$1" = "--" ]; then
        shift
        break
      fi
      validate_env_assignment "$1"
      export "$1"
      shift
    done
    with_discovery_gate run_owned_multidomain_target "$run_id" "$@"
    ;;
  queue-update)
    [ "$#" -eq 4 ] && [ "$1" = --candidate-ref ] && [ "$3" = --expected-sha ] || fail "queue-update requires exactly --candidate-ref refs/tags/NAME --expected-sha 40-lowercase-hex"
    queue_strict_update "$2" "$4"
    ;;
  nft-list-tables)
    exec nft list tables
    ;;
  nft-delete-blockcheck-table)
    family="${1:-}"
    table="${2:-}"
    case "$family" in
      ip|ip6|inet|arp|bridge|netdev) ;;
      *) fail "unsupported nft family: $family" ;;
    esac
    case "$table" in
      blockcheck[0-9]*) ;;
      *) fail "unsupported nft table: $table" ;;
    esac
    exec nft delete table "$family" "$table"
    ;;
  *)
    fail "unsupported command: $command"
    ;;
esac
