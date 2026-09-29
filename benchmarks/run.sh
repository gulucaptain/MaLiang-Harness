#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"
export PYTHONDONTWRITEBYTECODE=1
if [[ -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
  exec "$PROJECT_ROOT/.venv/bin/python" -m benchmarks "$@"
fi
exec python3 -m benchmarks "$@"
