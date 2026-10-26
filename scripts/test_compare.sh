#!/usr/bin/env bash
# From anywhere: bash scripts/test_compare.sh 01 --run
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -n "${PYTHON:-}" ]]; then
    PY="$PYTHON"
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
    PY="$ROOT/.venv/bin/python"
else
    PY="python3"
fi
cd "$ROOT"
exec "$PY" -m benchmarks.suite "$@"
