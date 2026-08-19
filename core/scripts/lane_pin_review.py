#!/usr/bin/env python3
"""Startup surface for lapsed lane-pin review dates ().

A lane pin is a durable USER-DIRECTED restriction on one agent's whole work
surface. Its `review_by` column says when a human should look at it again --
NOT when it stops binding. Past that date this prints a confirm-or-retire
prompt, and it keeps printing at every startup until a human answers by editing
the registry row.

WHAT THIS DELIBERATELY DOES NOT DO. It never retires, voids, weakens, or edits a
pin, and `gates/lane_pin.py::evaluate` never consults it -- the claim gate is
byte-identical before and after a review date lapses. A hard expiry would hand an
agent back a work surface the user deliberately took away, silently, at the exact
moment nobody was paying attention. Only the user retires a pin, by deleting the
row.

Exit code is ALWAYS 0. A startup advisory that can break startup would be removed
from startup, and then it would advise nobody.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "gates"))


def _load():
    from _paths import WORLD_DIR, AGENT_NAME  # noqa: E402
    import lane_pin  # noqa: E402
    return WORLD_DIR, AGENT_NAME, lane_pin


def collect(agent, registry_text, lane_pin, today=None):
    """Lapsed-review rows for `agent`, or every agent when `agent` is falsy."""
    out = []
    for pin in lane_pin.parse_pins(registry_text):
        if agent and pin.get("agent") != str(agent).strip().lower():
            continue
        status = lane_pin.review_status(pin, today)
        if status:
            out.append(status)
    return out


def render(rows):
    lines = ["", "=" * 62, "LANE PIN REVIEW DUE -- the pin is STILL ENFORCED", "=" * 62]
    for r in rows:
        lines.append("  %s" % r["message"])
        lines.append("     agent: %s   granted: %s   review_by: %s (%d days ago)"
                     % (r["agent"], r["granted"] or "(not recorded)",
                        r["review_by"], r["days_overdue"]))
    lines += [
        "",
        "  This is NOT an expiry. The pin binds exactly as before and the claim",
        "  gate is unchanged -- a lapsed date means nobody has LOOKED lately, not",
        "  that the constraint should end.",
        "",
        "  ONLY THE USER resolves this, in world/conventions/capability-routing.md",
        "  -> '## Standing Lane Pins':",
        "     CONFIRM -- move the row's review_by date forward, or",
        "     RETIRE  -- delete the row (the gate auto-lifts on the next claim).",
        "  Until then this prompt repeats at every startup.",
        "=" * 62, "",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--agent", default=None,
                    help="agent to check (default: the bound agent; 'all' for every pin)")
    ap.add_argument("--today", default=None, help="ISO date override (testing)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of the banner")
    args = ap.parse_args()

    try:
        world_dir, bound_agent, lane_pin = _load()
        agent = args.agent if args.agent is not None else bound_agent
        if str(agent).strip().lower() == "all":
            agent = None
        registry = Path(world_dir).joinpath(lane_pin.REGISTRY_RELPATH).read_text(encoding="utf-8")
        rows = collect(agent, registry, lane_pin, args.today)
    except Exception as e:
        # Fail-open and SAY SO. A silent swallow here is indistinguishable from
        # "no review due", which is the failure mode this whole goal is about.
        print("[lane-pin-review] could not read the pin registry (%s) -- "
              "no review state known" % e, file=sys.stderr)
        return 0

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if rows:
        print(render(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
