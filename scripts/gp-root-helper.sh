#!/bin/sh
set -eu

CONFIG_FILE="${GP_ROOT_HELPER_CONFIG:-/etc/default/gp-control-plane-root-helper}"
[ -r "$CONFIG_FILE" ] && . "$CONFIG_FILE"
ZAPRET_DIR="${ZAPRET_DIR:-/opt/zapret2}"

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
    ''|*[!0-9]*) fail "invalid pid: $1" ;;
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

validate_install_dir() {
  [ "$#" -ge 1 ] || fail "install directory is required"
  install_dir="$(real_path "$1")"
  [ -d "$install_dir/.git" ] || fail "install directory is not a git repository: $install_dir"
  [ -x "$install_dir/scripts/install-raspberry-pi.sh" ] || fail "installer is not executable: $install_dir/scripts/install-raspberry-pi.sh"
  printf '%s\n' "$install_dir"
}

shell_quote() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

run_target() {
  target="$(validate_run_target "$@")"
  shift
  exec "$target" "$@"
}

write_multidomain_runner() {
  source="$1"
  runner="$2"
  if ! awk '
    $0 == "fsleep_setup" { found=1; exit }
    { print }
    END { if (!found) exit 42 }
  ' "$source" > "$runner"; then
    fail "unsupported blockcheck2.sh layout: main marker not found"
  fi
  cat >> "$runner" <<'RUNNER'

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
  chmod 0700 "$runner"
}

run_multidomain_target() {
  target="$(validate_run_target "$@")"
  shift
  tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/gp-root-helper.XXXXXX")"
  runner="$tmp_dir/gp-multidomain-blockcheck.sh"
  cleanup_runner() {
    rm -rf "$tmp_dir"
  }
  trap cleanup_runner EXIT HUP INT TERM
  write_multidomain_runner "$target" "$runner"
  set +e
  "$runner" "$@"
  code="$?"
  set -e
  cleanup_runner
  trap - EXIT HUP INT TERM
  exit "$code"
}

queue_update() {
  install_dir="$(validate_install_dir "$1")"
  ref="$(validate_update_ref "$2")"
  service_name="${GP_SERVICE_NAME:-gp-control-plane-web.service}"
  case "$service_name" in
    gp-control-plane-web.service|gp-control-plane-web@*.service) ;;
    *) fail "unsupported service name: $service_name" ;;
  esac

  log_dir="$install_dir/build/state/release-updates"
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  unit="gp-control-plane-update-$stamp"
  script="$log_dir/$unit.sh"
  log_file="$log_dir/$unit.log"
  install_user="$(stat -c '%U' "$install_dir" 2>/dev/null || printf '%s\n' '')"
  [ -n "$install_user" ] && [ "$install_user" != "UNKNOWN" ] || fail "cannot detect install directory owner: $install_dir"
  repo_url="$(git -c safe.directory="$install_dir" -C "$install_dir" remote get-url origin 2>/dev/null || printf '%s\n' 'https://github.com/balbomush/GP-access-control-plane.git')"

  mkdir -p "$log_dir"
  cat > "$script" <<SCRIPT
#!/bin/sh
set -eu
umask 022
exec > $(shell_quote "$log_file") 2>&1
echo "gp-control-plane update queued at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "install_dir=$(shell_quote "$install_dir")"
echo "ref=$(shell_quote "$ref")"
export GP_INSTALL_DIR=$(shell_quote "$install_dir")
export GP_INSTALL_USER=$(shell_quote "$install_user")
export GP_BRANCH=$(shell_quote "$ref")
export GP_REPO_URL=$(shell_quote "$repo_url")
export GP_SERVICE_NAME=$(shell_quote "$service_name")
if bash $(shell_quote "$install_dir/scripts/install-raspberry-pi.sh"); then
  installed_ref="\$(git -c safe.directory=$(shell_quote "$install_dir") -C $(shell_quote "$install_dir") describe --tags --exact-match 2>/dev/null || git -c safe.directory=$(shell_quote "$install_dir") -C $(shell_quote "$install_dir") rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  installed_commit="\$(git -c safe.directory=$(shell_quote "$install_dir") -C $(shell_quote "$install_dir") rev-parse --short HEAD 2>/dev/null || true)"
  installed_version="\$($(shell_quote "$install_dir/.venv/bin/gp-control-plane") --version 2>/dev/null | awk '{print \$NF}' || true)"
  expected_version="\$(printf '%s' $(shell_quote "$ref") | sed 's/^v//')"
  echo "installed_ref=\$installed_ref"
  echo "installed_commit=\$installed_commit"
  echo "installed_version=\$installed_version"
  if [ "\$installed_ref" = $(shell_quote "$ref") ] || [ "\$installed_version" = "\$expected_version" ]; then
    echo "status=success"
  else
    echo "status=failed"
    echo "error=installed ref/version does not match requested release"
    exit 127
  fi
else
  code="\$?"
  echo "status=failed"
  echo "error=installer failed with code \$code"
  exit "\$code"
fi
SCRIPT
  chmod 0700 "$script"

  if command -v systemd-run >/dev/null 2>&1; then
    systemd-run --unit="$unit" --collect --property=Type=oneshot /bin/sh "$script" >/dev/null
  else
    nohup /bin/sh "$script" >/dev/null 2>&1 &
  fi
  printf 'queued=true\nunit=%s\nlog=%s\n' "$unit" "$log_file"
}

require_root

command="${1:-}"
[ -n "$command" ] || fail "command is required"
shift

case "$command" in
  check)
    exit 0
    ;;
  run)
    run_target "$@"
    ;;
  run-multidomain)
    run_multidomain_target "$@"
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
    run_target "$@"
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
    run_multidomain_target "$@"
    ;;
  queue-update)
    [ "$#" -eq 2 ] || fail "queue-update requires install directory and release ref"
    queue_update "$@"
    ;;
  kill)
    signal="$(validate_signal "${1:-}")"
    shift
    [ "$#" -gt 0 ] || exit 0
    for pid in "$@"; do
      validate_pid "$pid" >/dev/null
      kill "-$signal" "$pid" 2>/dev/null || true
    done
    ;;
  killpg)
    signal="$(validate_signal "${1:-}")"
    pgid="$(validate_pid "${2:-}")"
    kill "-$signal" "-$pgid" 2>/dev/null || true
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
