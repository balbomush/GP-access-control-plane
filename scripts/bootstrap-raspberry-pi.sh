#!/usr/bin/env bash
set -Eeuo pipefail

if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "$(dirname -- "${BASH_SOURCE[0]}")/bootstrap-linux.sh" ]; then
  script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
  exec bash "$script_dir/bootstrap-linux.sh" "$@"
fi

bootstrap_url="${GP_LEGACY_BOOTSTRAP_URL:-https://github.com/balbomush/GP-access-control-plane/releases/latest/download/bootstrap-linux.sh}"
export GP_BRANCH="${GP_BRANCH:-latest-stable}"
curl -LfsS "$bootstrap_url" | bash -s -- "$@"
