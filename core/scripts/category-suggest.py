#!/usr/bin/env python3
"""Category-suggest CLI — thin shim over gates.category_suggest.evaluate().

Logic lives in `core/scripts/gates/category_suggest.py` (PR 7c/4
extraction); this file is a CLI adapter for the legacy invocation contract.

Usage:
    category-suggest.sh --text "Fix authentication retry logic" [--top 3]

Output: JSON array of matches sorted by score descending:
    [{"key": "api-auth", "score": 4.2, "summary": "..."}]
"""

import argparse
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from _paths import WORLD_DIR  # noqa: E402
from gates.category_suggest import evaluate  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Suggest tree node categories for free text."
    )
    parser.add_argument("--text", required=True,
                        help="Free text to match against tree nodes")
    parser.add_argument("--top", type=int, default=3,
                        help="Number of top matches to return (default: 3)")
    args = parser.parse_args()

    matches = evaluate(args.text, top_n=args.top, world_dir=WORLD_DIR)
    print(json.dumps(matches, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
