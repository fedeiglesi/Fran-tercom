#!/usr/bin/env bash
# Records scripted demonstrations in simulation into a new versioned dataset (never overwrites).
# Usage: ./record_demonstrations.sh [episodes] [name]
set -euo pipefail
cd "$(dirname "$0")"
uv run python scripts/record_demos.py --episodes "${1:-200}" --seed-start 0 --name "${2:-level1_cube_scripted}"
