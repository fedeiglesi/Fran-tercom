"""Interpretar una orden en español argentino y mostrar lo que recibiría el VLA.

    uv run python scripts/order.py "agarrá la palanca de freno y ponela en la bandeja 3"
    uv run python scripts/order.py --backend llm "llevame el rulemán al cajón dos"
"""
from __future__ import annotations

import argparse
import json
import sys

from picking.language.orders import parse_order


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("--backend", default="auto", choices=["auto", "rules", "llm"])
    args = ap.parse_args()
    o = parse_order(args.text, backend=args.backend)
    print(json.dumps(o.to_dict(), ensure_ascii=False, indent=2))
    return 0 if o.executable else 1


if __name__ == "__main__":
    sys.exit(main())
