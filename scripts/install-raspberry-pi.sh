#!/usr/bin/env bash
set -Eeuo pipefail

if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "$(dirname -- "${BASH_SOURCE[0]}")/install-linux.sh" ]; then
  script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
  exec bash "$script_dir/install-linux.sh" "$@"
fi

raw_base_url="${GP_RAW_BASE_URL:-https://github.com/balbomush/GP-access-control-plane/raw}"
install_ref="${GP_BRANCH:-main}"
curl -LfsS "$raw_base_url/$install_ref/scripts/install-linux.sh" | bash -s -- "$@"
