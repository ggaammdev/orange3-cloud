#!/usr/bin/env bash
# Create the project-local development environment (.venv) with Orange and
# this add-on (editable) in it.
#
# Safe to run again: an existing .venv with the right Python version is kept
# and packages are only installed or upgraded to the pinned versions.
#
# Usage:
#   scripts/dev-setup.sh
#
# Environment overrides:
#   PYTHON_VERSION  Python for the venv (default 3.12, the version Orange's
#                   own install instructions use; orange3 also ships 3.13 wheels)
#   ORANGE_VERSION  orange3 version to install (default 3.41.0)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
ORANGE_VERSION="${ORANGE_VERSION:-3.41.0}"

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv is not installed (https://docs.astral.sh/uv/)" >&2
  exit 1
fi

# Create the venv, or recreate it if it uses a different Python version.
current=""
if [[ -x "$VENV/bin/python" ]]; then
  current="$("$VENV/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
fi
if [[ "$current" == "$PYTHON_VERSION" ]]; then
  echo "Keeping existing .venv (Python $current)"
else
  [[ -n "$current" ]] && echo "Recreating .venv: Python $current -> $PYTHON_VERSION"
  # uv-managed Python gives the latest patch release, independent of what the
  # system happens to have installed.
  uv venv --clear --managed-python --python "$PYTHON_VERSION" "$VENV"
fi

# Orange plus the Qt binding Orange's README recommends (PyQt6 + WebEngine),
# and this add-on in editable mode with its server and dev extras.
uv pip install --python "$VENV/bin/python" \
  "orange3==$ORANGE_VERSION" \
  PyQt6 \
  PyQt6-WebEngine \
  -e "$ROOT[dev,server]"

echo
"$VENV/bin/python" - <<'EOF'
from importlib.metadata import version
import sys
print(f"{'Python':<16}", sys.version.split()[0])
for name in ("orange3", "PyQt6", "PyQt6-Qt6", "PyQt6-WebEngine", "numpy", "scipy", "scikit-learn"):
    print(f"{name:<16}", version(name))
EOF
echo
echo "Done. Start Orange with: scripts/run-orange.sh"
