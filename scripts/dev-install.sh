#!/usr/bin/env bash
# GP development-environment installer (non-elevated, run from the repo root).
#
# Creates ./.venv and installs the package editable plus the test/audit tools
# from requirements-dev.txt — mirroring the venv bootstrap used by the release
# installer (scripts/install-linux.sh): python -m venv → pip upgrade →
# `pip install -e .`. Only this script also adds the dev extras.
#
#   bash scripts/dev-install.sh
#   .venv/bin/python -m pytest ...        # unit suite (see testing.md)
#   bash dev/gate_all.sh                  # full audit gate
#
# Optional network-free note: if pip cannot reach the index, the created .venv
# still runs the package and unit tests (pytest/ruff/vulture/pyright are then
# missing and the corresponding gates report "unavailable").
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

[ "$(id -u)" -ne 0 ] || { echo 'run dev-install.sh as the repo user, not root' >&2; exit 1; }

PYTHON="${PYTHON:-python3}"
VENV="$ROOT/.venv"

if [ ! -x "$VENV/bin/python" ]; then
  "$PYTHON" -m venv "$VENV"
fi

"$VENV/bin/python" -m pip install --upgrade pip setuptools wheel
"$VENV/bin/python" -m pip install -e "$ROOT"
if [ -f "$ROOT/requirements-dev.txt" ]; then
  "$VENV/bin/python" -m pip install -r "$ROOT/requirements-dev.txt"
fi

echo "dev install ready: $VENV"
echo "  unit tests : $VENV/bin/python -m pytest tests/ -q -p no:cacheprovider"
echo "  audit gate : bash dev/gate_all.sh"
