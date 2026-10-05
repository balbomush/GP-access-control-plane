#!/usr/bin/env bash
# Called by the one elevated GP installer. Existing /opt/zapret2 is never edited.
set -Eeuo pipefail
PATH='/usr/sbin:/usr/bin:/sbin:/bin'
fail() { printf 'ERROR: zapret2 preparation: %s; existing GP and zapret2 were preserved\n' "$*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || fail 'run via the GP clean installer sudo process'
[ "$#" -eq 2 ] && [ "$1" = --prepare-dir ] || fail 'usage: install-zapret2.sh --prepare-dir DIR'
prepare_dir="$2"
[ -d "$prepare_dir" ] && [ ! -L "$prepare_dir" ] && [ "$prepare_dir" = "$(readlink -f -- "$prepare_dir")" ] || fail 'unsafe preparation directory'
tool="$(dirname "$(readlink -f -- "$0")")/prepare-zapret2.py"
url='https://github.com/bol-van/zapret2/releases/download/v1.0.5.2/zapret2-v1.0.5.2.tar.gz'
curl --proto '=https' --proto-redir '=https' --location --fail --show-error --connect-timeout 20 --max-time 180 \
  --output "$prepare_dir/zapret2.tar.gz" "$url" || fail 'release download failed'
python3 "$tool" --archive "$prepare_dir/zapret2.tar.gz" --destination "$prepare_dir/runtime" || fail 'release integrity, extraction or compatibility check failed'
printf '%s\n' 'zapret2 release extracted and architecture checked; runtime probe is still required'
