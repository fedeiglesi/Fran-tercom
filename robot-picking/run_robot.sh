#!/usr/bin/env bash
# Real robot: first-contact tests, then policy execution. Read docs/REAL_ROBOT.md and docs/SAFETY.md first.
# Usage: ./run_robot.sh readonly        -> Test 1 (no motion)
#        ./run_robot.sh test N          -> Tests 2..7
#        ./run_robot.sh policy outputs/<ckpt> [episodes]
set -euo pipefail
cd "$(dirname "$0")"
case "${1:-}" in
  readonly) uv run python scripts/test_mycobot_readonly.py ;;
  test)     uv run python scripts/real_tests.py --test "$2" ${3:+--checkpoint $3} ;;
  policy)   uv run python scripts/run_policy.py --policy act --checkpoint "$2" --episodes "${3:-10}" ;;
  camera)   uv run python scripts/test_oakd.py ;;
  calibrate) uv run python scripts/calibrate_camera.py ;;
  *) echo "usage: $0 readonly | test N | policy <ckpt> [episodes] | camera | calibrate"; exit 1 ;;
esac
