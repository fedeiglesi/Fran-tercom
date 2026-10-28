"""Serve a policy over WebSocket (run on the GPU machine, or locally for testing).

    uv run python scripts/policy_server.py --policy act --checkpoint outputs/act_level1_v001 --port 8765
"""
from __future__ import annotations

import argparse
import sys

from picking.networking.server import PolicyServer
from picking.utils.logging import setup_logging


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", default="act", choices=["act", "zero"])
    ap.add_argument("--checkpoint", default=None)
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    setup_logging()
    if args.policy == "act":
        from picking.policies.act_policy import ACTPolicyWrapper

        policy = ACTPolicyWrapper(args.checkpoint, device=args.device)
    else:
        from picking.core.types import Action
        from picking.policies.base import Policy

        class Zero(Policy):
            name, version, needs_images = "zero", "0", False

            def act(self, obs):
                return Action.zero()

        policy = Zero()
    PolicyServer(policy, args.host, args.port).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
