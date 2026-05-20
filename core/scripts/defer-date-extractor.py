#!/usr/bin/env python3
"""Extract structured date from a defer_reason string — thin CLI shim.

Logic lives in `core/scripts/gates/defer_date.py` (PR 7e/3 extraction);
this file is a CLI adapter for the legacy invocation contract.

Usage:
  python3 core/scripts/defer-date-extractor.py "Not before 2026-07-14"
  -> {"matched": true, "deferred_until": "2026-07-14T00:00:00", ...}

Exit codes:
  0 - extraction completed (matched or unmatched, both written to stdout)
  1 - invalid arguments / parse failure on a malformed input
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from gates.defer_date import extract  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("text", help="defer_reason text to parse")
    p.add_argument("--now", help="ISO timestamp override for testing")
    args = p.parse_args()

    now = None
    if args.now:
        try:
            now = datetime.fromisoformat(args.now)
        except ValueError:
            print(f"Invalid --now: {args.now}", file=sys.stderr)
            return 1

    result = extract(args.text, now=now)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
