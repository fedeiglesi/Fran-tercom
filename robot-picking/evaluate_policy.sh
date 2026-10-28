#!/usr/bin/env bash
# Evaluates a trained checkpoint in simulation on holdout seeds (>= 100000).
# Usage: ./evaluate_policy.sh outputs/<checkpoint_dir> [episodes]
set -euo pipefail
cd "$(dirname "$0")"
uv run python scripts/evaluate_sim.py --policy act --checkpoint "$1" --episodes "${2:-100}" --seed-start 100000 --tag act
