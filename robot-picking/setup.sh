#!/usr/bin/env bash
# One-shot setup for the Mac Studio (or Linux). Installs uv, Python 3.12, all dependencies, runs the tests.
set -euo pipefail
cd "$(dirname "$0")"
if ! command -v uv >/dev/null 2>&1; then
  echo ">> installing uv (Python package manager)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
echo ">> creating environment (this downloads PyTorch, MuJoCo, LeRobot: several minutes the first time)"
uv sync --extra dev ${PICKING_REAL:+--extra real}
echo ">> running tests"
uv run python -m pytest -q tests
echo
echo "SETUP OK. Next: ./run_simulation.sh"
