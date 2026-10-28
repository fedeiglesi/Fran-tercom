#!/usr/bin/env bash
# Trains the ACT baseline on a recorded dataset.
# Usage: ./train_policy.sh [dataset_dir_name] [steps]
set -euo pipefail
cd "$(dirname "$0")"
DS="${1:-level1_cube_scripted_v001}"
uv run python scripts/train.py --config policies/act_level1.yaml --dataset "$DS" ${2:+--steps $2}
