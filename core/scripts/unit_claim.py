#!/usr/bin/env python3
"""Unit-level claim for deliberately multi-unit goals ().

WHAT THIS EXISTS FOR. ``aspirations-claim.sh`` claims a GOAL. A deliberately
multi-unit goal (g-326-422: "wire the 11 templates, one at a time, one PR each")
is NON-TERMINAL by its own instruction, so each Body claims it, does ONE unit,
and RELEASES. The claim is free again within minutes and nothing anywhere
records which UNIT is in flight. ``partner_in_flight`` and the duplication gate
both see an unclaimed goal, correctly. Measured 2026-08-19: two alpha Bodies
built ``reflection_focal_points.j2`` concurrently; PR #32 landed as f554a79 while
PR #33 was still building it, and #33 was closed as a duplicate. One full unit of
work — schema, 11 tests, manifest edits, a 6-case live A/B, an E2E and two full
suite runs — was wasted.

WHY THE BOARD AND NOT A FIELD ON THE GOAL RECORD. The suggested shapes were (a) a
``unit_in_flight`` lease field on the goal record or (b) a board pre-claim, with
a stated preference for whichever composes with the EXISTING claim record. The
goal record loses on a mechanism, not on taste:

  ``coordination_merge._merge_goal`` reconciles two copies of one goal by an
  LWW base pick (``out = dict(win)`` on newer ``last_modified``). The claim pair
  survives concurrent writes only because it is named EXPLICITLY in the
  conflict branch -- ``_diff_agent or _diff_body`` -> first-claim-wins on
  ``claimed_at``, with ``claimed_by_sid`` moved as part of the same unit
  (g-306-132-c). A NEW field gets none of that: it rides the LWW base pick, so
  the second Body's write silently wins whenever its snapshot is newer. That is
  the precise failure the field would have been added to prevent, and it would
  present as a working mechanism on one box.

The board is append-only and cross-box replicated (guard-997: it survives the
partition a blocked merge IS), which is the write semantics a claim actually
needs. ``claim`` and ``release`` are already VALID_MESSAGE_TYPES, so this adds no
parallel claim surface -- it uses the one the fleet already coordinates on.

WHY NOT A BETTER HANDOFF NOTE. The goal is explicit that a prose remedy is wrong
by construction: unit 1's ``progress_note`` named the next unit, and Phase 2.9
makes every claimer read it, so a note naming a specific next unit is a MAGNET --
it deterministically steers every reader to the SAME unit, and the better the
note, the more reliably two Bodies collide. Writing no note collides too, via the
"narrowest first" rule the goal itself states. This module therefore reads and
writes a STRUCTURED marker and never consults prose.

IDENTITY IS THE SESSION, NOT THE AGENT. A worker Body and its reducer are both
``alpha``; only the session id separates them (the same reason
``_merge_goal._diff_body`` exists, and the same reason the claim guard in
``aspirations.py cmd_update_goal`` calls the SID condition PRIMARY). Board
records already carry ``session_id`` from ``MIND_SID``, so no schema change is
needed here either.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent

CHANNEL = "coordination"
TAG = "unit-claim"

# The structured marker. Deliberately strict and machine-only: a claim that can
# be expressed in prose is a claim a reader has to interpret, which is the
# failure mode this module exists to remove.
_MARKER_RE = re.compile(
    r"^UNIT-(?P<verb>CLAIM|RELEASE)\s+goal=(?P<goal>[A-Za-z0-9._-]+)\s+"
    r"unit=(?P<unit>\S+)\s*$",
    re.MULTILINE,
)

# Units are usually file paths (``reflection_focal_points.j2``,
# ``experiment/correlation_discovery.py``), so the charset admits ``/`` and
# ``.``; whitespace is excluded because the marker is whitespace-delimited.
_UNIT_RE = re.compile(r"^[A-Za-z0-9._/-]+$")

DEFAULT_LEASE_HOURS = 4.0


# --------------------------------------------------------------------------
# Pure logic (no I/O -- this is the part the two-Body test drives directly)
# --------------------------------------------------------------------------

def parse_marker(text):
    """Extract ``(verb, goal_id, unit)`` from a board message, or None.

    Returns the FIRST marker only. A message carrying two markers is malformed
    by construction (one post = one claim), and silently honouring the second
    would let a crafted post release someone else's unit as a side effect.
    """
    if not isinstance(text, str):
        return None
    m = _MARKER_RE.search(text)
    if not m:
        return None
    return (m.group("verb"), m.group("goal"), m.group("unit"))


def _parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def live_claims(records, *, now, lease_hours):
    """Map ``(goal_id, unit) -> claim record`` for every UNEXPIRED, UNRELEASED claim.

    Records may arrive in any order (the board is append-only but two boxes
    interleave on sync), so this makes no ordering assumption. Every claim is
    kept, each is cancelled only by a release from its OWN session at-or-after
    it, and the newest SURVIVOR holds the unit. A release by a DIFFERENT session
    cannot clear a claim -- otherwise a peer could free a live unit and re-create
    the collision.

    KEEPING EVERY CLAIM, NOT JUST THE NEWEST, IS LOAD-BEARING. With
    newest-claim-only bookkeeping the sequence [A claims, B claims anyway
    (--force or a sync race), B releases] resolves to "free" -- B's release
    cancels the record that shadowed A's, and A's still-live claim has already
    been discarded. The unit then reads free while A is mid-build, which is the
    duplicate this module exists to prevent. Fail direction matters more than
    likelihood here: the cost of remembering an extra claim is a dict entry.
    """
    claims = {}    # key -> [(ts, rec), ...]
    releases = {}  # (key, sid) -> newest release ts
    for rec in records:
        if not isinstance(rec, dict):
            continue
        parsed = parse_marker(rec.get("text"))
        if not parsed:
            continue
        verb, goal, unit = parsed
        ts = _parse_ts(rec.get("timestamp"))
        key = (goal, unit)
        if verb == "CLAIM":
            # An unparseable timestamp is KEPT, not dropped -- see the fail
            # direction note below.
            claims.setdefault(key, []).append((ts, rec))
        elif ts is not None:
            # A release with an unparseable timestamp cannot be shown to
            # postdate any claim, so it supersedes nothing (parity with
            # goal-pickup-coordination-check.supersede_released_claims).
            rkey = (key, rec.get("session_id") or "")
            if rkey not in releases or ts > releases[rkey]:
                releases[rkey] = ts

    cutoff = now - timedelta(hours=lease_hours)
    out = {}
    for key, entries in claims.items():
        survivors = []
        for ts, rec in entries:
            # FAIL DIRECTION, adopted from the fleet's existing posture in
            # goal-pickup-coordination-check.supersede_released_claims: "an
            # unparseable timestamp on either side KEEPS the claim (a false
            # yield is cheaper than a missed race)". An unaged claim cannot be
            # expired by the lease, so it holds until someone passes --force --
            # which is an escape hatch that probe does NOT have, making this
            # posture strictly safer here than there. Dropping the record
            # instead would report the unit FREE, i.e. fail in the
            # duplicate-producing direction, which is the one thing this module
            # must never do.
            if ts is not None and ts < cutoff:
                continue  # lease expired -- a dead Body must not wedge a unit
            rel_ts = releases.get((key, rec.get("session_id") or ""))
            if rel_ts is not None and ts is not None and rel_ts >= ts:
                continue  # released by its own author
            survivors.append((ts, rec))
        if survivors:
            # None sorts oldest, so a well-formed claim wins the holder slot
            # over a malformed peer while the malformed one still blocks.
            out[key] = max(survivors, key=lambda pair: pair[0] or datetime.min)[1]
    return out


def decide(records, *, goal_id, unit, my_sid, now, lease_hours):
    """Should this Body start work on ``unit`` of ``goal_id``?

    Verdicts:
      ``free``          -- nothing live; acquire it.
      ``already-mine``  -- this Body already holds it (idempotent re-entry after
                           an autocompact resume; NOT an error).
      ``held``          -- another Body holds an unexpired claim. REFUSE.
      ``unprovable``    -- a live claim exists but this request carries no
                           session id, so it cannot be shown to be ours.

    ``unprovable`` REFUSES, matching ``aspirations.py cmd_update_goal``'s
    ``_sid_unprovable`` branch: if the check goes quiet whenever the caller omits
    the sid, unsetting MIND_SID defeats it entirely. The asymmetry is the reason
    -- a false refusal costs a stalled unit that ``--force`` clears in seconds,
    while a false allow costs the duplicated unit this module exists to prevent.
    """
    live = live_claims(records, now=now, lease_hours=lease_hours)
    holder = live.get((goal_id, unit))
    if holder is None:
        return {"verdict": "free", "holder": None}
    holder_sid = holder.get("session_id") or ""
    if not my_sid:
        return {"verdict": "unprovable", "holder": holder}
    if holder_sid and holder_sid == my_sid:
        return {"verdict": "already-mine", "holder": holder}
    return {"verdict": "held", "holder": holder}


# --------------------------------------------------------------------------
# I/O
# --------------------------------------------------------------------------

def load_lease_hours(project_root=PROJECT_ROOT):
    """Read ``multi_agent.claim_timeout_hours`` -- the SAME lease the goal-level
    claim uses (``aspirations_write._load_claim_timeout_hours``). Reused rather
    than re-declared so a unit claim can never outlive the goal claim that
    contains it. Fail-open to the historical 4.0 on any read error.
    """
    try:
        import yaml
        cfg = yaml.safe_load(
            (project_root / "core" / "config" / "aspirations.yaml").read_text(encoding="utf-8")
        ) or {}
        ma = cfg.get("multi_agent") or {}
        if "claim_timeout_hours" not in ma:
            return DEFAULT_LEASE_HOURS
        v = ma["claim_timeout_hours"]
        return DEFAULT_LEASE_HOURS if v is None else float(v)
    except Exception:
        return DEFAULT_LEASE_HOURS


def _read_board(since_hours):
    """Fetch recent unit-claim traffic via the canonical wrapper.

    Both ``claim`` and ``release`` types are needed, and ``board-read.sh``
    filters to ONE type per call, so this makes two calls and concatenates.
    """
    from _runtime_bash import bash_cmd

    script = str(PROJECT_ROOT / "core" / "scripts" / "board-read.sh")
    records = []
    for msg_type in ("claim", "release"):
        proc = subprocess.run(
            bash_cmd(script, "--channel", CHANNEL, "--type", msg_type,
                     "--tag", TAG, "--since", f"{int(since_hours) + 1}h", "--json"),
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )
        if proc.returncode != 0:
            # Loud, never silent: a swallowed read error would present as an
            # empty board, i.e. "the unit is free" -- the exact wrong answer
            # (verify-before-assuming rule 4: a silenced command is zero
            # signals, not one).
            print(f"[unit-claim] board-read {msg_type} failed rc={proc.returncode}: "
                  f"{proc.stderr.strip()}", file=sys.stderr)
            raise SystemExit(2)
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _post(verb, goal_id, unit, note):
    from _runtime_bash import bash_cmd

    script = str(PROJECT_ROOT / "core" / "scripts" / "board-post.sh")
    text = f"UNIT-{verb} goal={goal_id} unit={unit}"
    if note:
        text += f"\n{note}"
    proc = subprocess.run(
        bash_cmd(script, "--channel", CHANNEL,
                 "--type", "claim" if verb == "CLAIM" else "release",
                 "--tags", f"{TAG},{goal_id}"),
        input=text, capture_output=True, text=True, cwd=str(PROJECT_ROOT),
    )
    if proc.returncode != 0:
        print(f"[unit-claim] board-post failed rc={proc.returncode}: "
              f"{proc.stderr.strip()}", file=sys.stderr)
        raise SystemExit(2)
    return proc.stdout.strip()


def _describe(rec, now):
    ts = _parse_ts(rec.get("timestamp"))
    age = "unknown age" if ts is None else f"{(now - ts).total_seconds() / 60:.0f} min ago"
    return (f"session {rec.get('session_id') or '(none)'} "
            f"(author {rec.get('author') or '?'}, {age}, {rec.get('id')})")


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="unit-claim",
        description="Unit-level claim for deliberately multi-unit goals (g-306-322).",
    )
    ap.add_argument("command", choices=("acquire", "release", "status"))
    ap.add_argument("goal_id")
    ap.add_argument("unit", nargs="?")
    ap.add_argument("--force", metavar="JUSTIFICATION",
                    help="Proceed despite a live claim; the justification is posted.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.command in ("acquire", "release"):
        if not args.unit:
            ap.error(f"{args.command} requires a unit token")
        if not _UNIT_RE.match(args.unit):
            ap.error(f"unit token {args.unit!r} must match {_UNIT_RE.pattern} "
                     "(no whitespace -- the marker is whitespace-delimited)")

    now = datetime.now()
    lease = load_lease_hours()
    my_sid = os.environ.get("MIND_SID", "").strip()
    records = _read_board(lease)

    if args.command == "status":
        live = live_claims(records, now=now, lease_hours=lease)
        rows = [{"goal_id": g, "unit": u, "session_id": r.get("session_id"),
                 "author": r.get("author"), "timestamp": r.get("timestamp"),
                 "message_id": r.get("id")}
                for (g, u), r in sorted(live.items()) if g == args.goal_id]
        if args.json:
            print(json.dumps({"goal_id": args.goal_id, "lease_hours": lease,
                              "live_units": rows}, indent=2))
        elif not rows:
            print(f"no live unit claims on {args.goal_id} (lease {lease}h)")
        else:
            for r in rows:
                print(f"{r['unit']}\t{r['session_id']}\t{r['timestamp']}\t{r['message_id']}")
        return 0

    if args.command == "release":
        msg_id = _post("RELEASE", args.goal_id, args.unit, args.force)
        print(f"released {args.goal_id} unit={args.unit} ({msg_id})")
        return 0

    result = decide(records, goal_id=args.goal_id, unit=args.unit,
                    my_sid=my_sid, now=now, lease_hours=lease)
    verdict, holder = result["verdict"], result["holder"]

    if verdict in ("held", "unprovable") and not args.force:
        why = ("held by another Body" if verdict == "held"
               else "held by some Body, and this request carries NO session id "
                    "so it cannot be shown to be ours")
        print(
            f"REFUSED: unit '{args.unit}' of {args.goal_id} is {why} — "
            f"{_describe(holder, now)}. DO NOT START THIS UNIT. Pick a different "
            f"unit, or wait: the lease expires {lease}h after the claim. "
            f"If that Body is provably dead, or you are resuming your own work "
            f"after losing your session id, re-run with "
            f"--force \"<justification>\".",
            file=sys.stderr)
        if args.json:
            print(json.dumps({"verdict": verdict, "acquired": False,
                              "goal_id": args.goal_id, "unit": args.unit,
                              "holder_session_id": holder.get("session_id"),
                              "holder_message_id": holder.get("id"),
                              "lease_hours": lease}, indent=2))
        return 1

    if verdict == "already-mine" and not args.force:
        # Idempotent: an autocompact resume re-enters the loop mid-iteration and
        # re-runs this. Re-posting would double the marker for no benefit.
        print(f"already held by this Body: {args.goal_id} unit={args.unit} "
              f"({holder.get('id')})")
        if args.json:
            print(json.dumps({"verdict": verdict, "acquired": True,
                              "goal_id": args.goal_id, "unit": args.unit,
                              "holder_message_id": holder.get("id"),
                              "lease_hours": lease}, indent=2))
        return 0

    note = f"forced: {args.force}" if args.force else None
    msg_id = _post("CLAIM", args.goal_id, args.unit, note)
    print(f"acquired {args.goal_id} unit={args.unit} ({msg_id})")
    if args.json:
        print(json.dumps({"verdict": verdict, "acquired": True,
                          "goal_id": args.goal_id, "unit": args.unit,
                          "message_id": msg_id, "forced": bool(args.force),
                          "lease_hours": lease}, indent=2))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    raise SystemExit(main())
