#!/usr/bin/env bash
# Start the Orange canvas from the project's .venv (create it with scripts/dev-setup.sh).
# Extra arguments go to Orange, for example:
#   scripts/run-orange.sh --no-splash --no-welcome -l 2

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  echo "error: $ROOT/.venv not found; run scripts/dev-setup.sh first" >&2
  exit 1
fi

exec "$PYTHON" -m Orange.canvas "$@"
