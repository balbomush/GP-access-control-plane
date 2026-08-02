#!/usr/bin/env bash
set -Eeuo pipefail

if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "$(dirname -- "${BASH_SOURCE[0]}")/bootstrap-linux.sh" ]; then
  script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
  exec bash "$script_dir/bootstrap-linux.sh" "$@"
fi

raw_base_url="${GP_RAW_BASE_URL:-https://github.com/balbomush/GP-access-control-plane/raw}"
curl -LfsS "$raw_base_url/main/scripts/bootstrap-linux.sh" | bash -s -- "$@"
