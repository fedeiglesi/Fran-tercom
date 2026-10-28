#!/usr/bin/env bash
# Runs the scripted baseline in simulation on holdout seeds and prints the metrics.
# Usage: ./run_simulation.sh [episodes]   (default 100)
set -euo pipefail
cd "$(dirname "$0")"
uv run python scripts/evaluate_sim.py --policy heuristic --episodes "${1:-100}" --seed-start 100000 --tag baseline0
