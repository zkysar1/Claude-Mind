#!/usr/bin/env python3
"""delivery-recheck — release dependents whose blocker's deliverable has landed.

THE HALF THAT MAKES THE HOLD SAFE (g-306-442). The delivery gate in
`aspirations.py _clear_stale_blockers` and `dependent-unblock.py` keeps a
dependent blocked while its predecessor's commit is reachable only from
`refs/workers/**`. Both fire on a TRANSITION — a predecessor going terminal —
and a held dependent's predecessor is already terminal, so neither will ever
look at it again. Without this sweep the hold is permanent, and a correct safety
mechanism composes with a correct release path into a dead end.

This is the read-time half: it re-derives the verdict from scratch on every run
and releases what has since landed. It reads NO stored verdict — there is none
to read, by design (see `_delivery_gate`), because a deliverable stranded at
close time becomes reachable exactly when someone consumes the carrier ref, and
a cached verdict would be wrong from that moment until forever.

DRY-RUN IS THE DEFAULT and the mode is always printed. `--apply` writes through
`aspirations-update-goal.sh`, never the store directly.

WHAT IT DOES NOT DO: it never CREATES a hold, only removes one. A goal blocked
for any other reason (blocker_ref, an unfinished predecessor, a defer) is not
this sweep's business and is left exactly as found.
"""

import argparse
import importlib.util
import json
import os
import sys

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from _paths import WORLD_DIR, AGENT_DIR  # noqa: E402
from aspirations import read_jsonl  # noqa: E402  (same source as dependent-unblock)

TERMINAL = {"completed", "skipped", "expired", "archived", "superseded"}


def _load_sibling(name, modname):
    """Load a hyphenated sibling script as a module (its name is not importable)."""
    path = os.path.join(SCRIPTS, name)
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _all_goals():
    """[(source, asp_id, goal)] across both queues, plus an id->goal lookup."""
    rows, lookup = [], {}
    for source, base in (("world", WORLD_DIR), ("agent", AGENT_DIR)):
        if base is None:
            continue
        path = base / "aspirations.jsonl"
        if not path.exists():
            continue
        for asp in read_jsonl(path):
            asp_id = asp.get("id", "")
            for goal in asp.get("goals", []) or []:
                rows.append((source, asp_id, goal))
                gid = goal.get("id")
                if gid:
                    lookup[gid] = goal
    return rows, lookup


def sweep(apply=False, update=None):
    """Re-probe every delivery-held dependent; release what has since landed.

    `update` is the write callable `(source, goal_id, field, value, dry_run) ->
    (ok, err)`. It is a PARAMETER rather than an import so the caller that owns
    the release path — `dependent-unblock.py`, which runs on every goal close —
    can pass its own `_update` without this module importing it back. That
    inversion is what lets the sweep have a real call site: a standalone script
    nothing invokes is indistinguishable from one that always returns clean
    (guard-1943), and both gate sites fire only on a predecessor's terminal
    TRANSITION, which a held dependent's predecessor has already made.
    """
    import _delivery_gate as dg
    if apply and update is None:
        update = _load_sibling("dependent-unblock.py", "_dependent_unblock")._update

    rows, lookup = _all_goals()
    released, still_held, checked = [], [], 0

    for source, asp_id, goal in rows:
        bb = goal.get("blocked_by") or []
        if isinstance(bb, str):
            bb = [bb]
        if not bb:
            continue

        # Only entries whose blocker is ALREADY TERMINAL are ours: a live
        # predecessor is an ordinary dependency wait, not a delivery hold.
        terminal_entries = [b for b in bb
                            if (lookup.get(b) or {}).get("status") in TERMINAL]
        if not terminal_entries:
            continue

        keep, freed = [], []
        for b in terminal_entries:
            checked += 1
            state, detail = dg.blocker_delivery_state(lookup[b])
            if state == dg.PENDING:
                keep.append(b)
                still_held.append({"goal_id": goal.get("id"), "source": source,
                                   "blocker": b, "reason": detail})
            else:
                freed.append({"blocker": b, "state": state, "detail": detail})

        if not freed:
            continue

        new_bb = [b for b in bb if b not in {f["blocker"] for f in freed}]
        entry = {"goal_id": goal.get("id"), "source": source, "asp_id": asp_id,
                 "freed": freed, "still_blocked_by": new_bb}

        if apply:
            ok, err = update(source, goal.get("id"), "blocked_by",
                             json.dumps(new_bb), False)
            entry["written"] = bool(ok)
            if not ok:
                entry["error"] = err
            # Mirror dependent-unblock Step 1b EXACTLY — same three conditions.
            # Restoring status on a goal that still carries a structured
            # blocker_ref, or that is not `blocked`, is not this sweep's to undo.
            elif (not new_bb
                    and goal.get("status") == "blocked"
                    and not goal.get("blocker_ref")):
                ok2, _ = update(source, goal.get("id"), "status",
                                "pending", False)
                entry["status_restored"] = bool(ok2)
        released.append(entry)

    return {
        "mode": "apply" if apply else "dry-run",
        "terminal_entries_checked": checked,
        "candidate_count": len(released),
        "released": released,
        "still_held": still_held,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true",
                    help="perform the releases (default: dry-run report only)")
    args = ap.parse_args(argv)
    print(json.dumps(sweep(apply=args.apply), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
