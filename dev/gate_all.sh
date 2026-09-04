#!/usr/bin/env bash
# gate_all.sh — run every test/audit gate in one shot (see testing.md).
#
#   bash dev/gate_all.sh                → unit + quality + ruff + vulture + pyright(best-effort) + biome + coverage
#   bash dev/gate_all.sh --integration  → also runs the sudo integration suite
#   PY=/path/to/python bash dev/gate_all.sh   → override interpreter
#
# Exits non-zero on the first failing hard gate (unit, quality, ruff, vulture,
# biome). pyright and coverage are soft: pyright reports diagnostics without
# aborting (network/best-effort), coverage prints the report and honours
# fail_under only if it is set in pyproject.toml.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
if [ -n "${PY:-}" ]; then PYTHON="$PY";
elif [ -x "$ROOT/.venv/bin/python" ]; then PYTHON="$ROOT/.venv/bin/python";
else PYTHON="$(command -v python3)"; fi
RUFF="$("$PYTHON" -c 'import shutil,sys; print(shutil.which("ruff") or sys.executable+" -m ruff")' 2>/dev/null || true)"


echo "=== gate: pytest unit (default: not integration and not quality) ==="
"$PYTHON" -m pytest tests/ -q -p no:cacheprovider

echo "=== gate: pytest quality ==="
"$PYTHON" -m pytest -m quality -q -p no:cacheprovider

echo "=== gate: ruff ==="
"$PYTHON" -m ruff check src tests

echo "=== gate: vulture (dead code) ==="
"$PYTHON" -m vulture --config pyproject.toml

echo "=== gate: pyright (best-effort, does not abort) ==="
PYRIGHT="$(dirname "$PYTHON")/pyright"
if [ -x "$PYRIGHT" ]; then
  "$PYRIGHT" 2>/dev/null || echo "pyright: diagnostics present (see above; best-effort gate)"
else
  echo "pyright not installed — skip (pip install pyright)"
fi

echo "=== gate: biome (js/css assets) ==="
if [ -x node_modules/.bin/biome ]; then
  npm run lint --silent
else
  echo "biome not installed — run: npm install"
fi

echo "=== gate: coverage (fail_under from pyproject) ==="
"$PYTHON" -m coverage erase >/dev/null 2>&1 || true
"$PYTHON" -m coverage run -m pytest tests/ -q -p no:cacheprovider
"$PYTHON" -m coverage report -m

if [ "${1:-}" = "--integration" ]; then
  echo "=== gate: integration (sudo) ==="
  sudo -n "$PYTHON" -m pytest -m integration -q --timeout=600
fi

echo "ALL GATES PASSED"
