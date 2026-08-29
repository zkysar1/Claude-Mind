#!/usr/bin/env python3
"""frontier-check.py — print the claimable-frontier census (read-only).

The number the fleet was missing: how many pending goals a Body could claim
RIGHT NOW, versus how many wait on a live goal, and which goals they wait on.
Same implementation as agent-watchdog's DependencyFunnelProbe
(`core/scripts/_frontier.py`), so what this prints is what the probe fired on.

Usage:
  bash core/scripts/frontier-check.sh            # human summary
  bash core/scripts/frontier-check.sh --json     # full census as JSON

Exit codes: 0 frontier > 0 or nothing pending; 2 frontier is 0 with gated
goals (a funnel); 1 census error. Read `parse_skipped` before trusting a
clean number — a nonzero skip count means the census did not see every goal
(guard-3714).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Claimable-frontier census (read-only)")
    parser.add_argument("--json", action="store_true", help="emit the full census as JSON")
    parser.add_argument("--lookback-hours", type=float, default=None,
                        help="body-census window (default: aspirations.yaml dependency_funnel.lookback_hours)")
    args = parser.parse_args(argv)

    try:
        from _paths import WORLD_DIR, agents_root
        import _frontier
        lookback = args.lookback_hours
        if lookback is None:
            import yaml
            cfg_path = Path(__file__).resolve().parent.parent / "config" / "aspirations.yaml"
            with cfg_path.open(encoding="utf-8") as f:
                lookback = float(((yaml.safe_load(f) or {}).get("dependency_funnel") or {})
                                 .get("lookback_hours", 6))
        census = _frontier.frontier_census(WORLD_DIR, agents_root(), lookback_hours=lookback)
    except Exception as e:  # noqa: BLE001 — report, never traceback
        print(f"frontier-check: census failed — {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    funnel = census["claimable_count"] == 0 and census["gated_count"] > 0
    if args.json:
        print(json.dumps(dict(census, funnel=funnel), indent=2, ensure_ascii=False))
        return 2 if funnel else 0

    b = census["bodies"]
    print(f"claimable frontier: {census['claimable_count']}   gated: {census['gated_count']}   "
          f"in-progress: {census['in_progress']}   deferred: {census['deferred']}   "
          f"blocked: {census['blocked']}   user-only: {census['user_only']}")
    print(f"bodies: active={b['active']} active-stale={b.get('active_stale', 0)} "
          f"closed-recent={b['closed_recent']} "
          f"(of {b['scanned']} manifests, last {lookback:g}h)   "
          f"active aspirations: {census['active_aspirations']}   "
          f"parse_skipped: {census['parse_skipped']}")
    if census["claimable"]:
        print("claimable: " + ", ".join(census["claimable"]))
    for r in census["roots"][:8]:
        claimed = f", claimed by {r['claimed_by']}" if r.get("claimed_by") else ""
        print(f"root {r['id']} gates {r['gates']}: [{r['status']}{claimed}] {r['title']}")
    if census["unknown_blockers"]:
        print("unknown blockers (ids absent from every queue): "
              + ", ".join(f"{g}->{bid}" for g, bid in census["unknown_blockers"][:10]))
    if funnel:
        print("FUNNEL: frontier is 0 — relax the gated consumers to the root's interface "
              "(see the DependencyFunnelProbe goal) or split the root.")
    return 2 if funnel else 0


if __name__ == "__main__":
    sys.exit(main())
