#!/usr/bin/env python3
"""Defer-Citation Parity Check — flag a defer that NAMES a dependency goal in a
form `defer-recheck._extract_dep_ids` cannot capture.

THE GAP THIS FILLS. `defer-recheck.py` clears a defer when every goal id it
extracts from `defer_reason` has completed. When the extractor captures
NOTHING it records `{"action": "skipped", "reason": "no recognized dependency
pattern"}` and moves on — and that one bucket holds two populations that mean
opposite things:

  (a) the defer genuinely names no dependency (a human act, a physical event,
      a date). Nothing to key on; skipping is CORRECT and this check is silent.
  (b) the defer DOES name a `g-NNN-NN`, in prose the two patterns do not
      match. Nothing will ever clear it, so the goal is frozen until the
      fail-open TTL expires — at which point it returns to the pool at full
      rank with the block unchanged, a Body re-derives it, and the cycle
      repeats every TTL forever.

Only (b) is a defect, and nothing distinguishes it today. defer-recheck's own
module comment already records two live instances ("they are frozen because
their prose matches NO dependency pattern at all") without surfacing the
population.

MEASURED MOTIVATION (echo, cc-03, 2026-09-21): three goals found BY HAND in one
window, two of them HIGH in boosted closing lanes. g-369-222 had been
re-derived SIX times in 48 hours by four agents. g-373-94's defer named two
EVENTS ("an ack channel exists", "a capped vessel run is launched") and
fail-opened every 120h. g-115-8280's defer says its blocker is "tracked on
g-115-9938" — a real dependency, in prose matching neither pattern. The
operative rule is guard-7217: match the gate instrument to the SHAPE of the
clearing condition; an event-shaped condition needs a parseable citation, not
a clock.

WHY IT REUSES THE EXTRACTOR RATHER THAN RE-DERIVING IT. The whole question is
"what does the real consumer capture", so a second regex here would drift from
the one that matters and answer about itself instead (rb-11292). Both the
extractor and the goal reader are imported from `defer-recheck.py` by path.
That coupling is deliberate: if the patterns widen, this check must widen with
them in the same commit, automatically.

REPORT-ONLY BY DESIGN, and no `--apply` may be added. Rewriting someone's
defer_reason to insert a citation is a narrative edit to a field whose text is
load-bearing evidence (guard-5228: update-goal REPLACES), and picking WHICH of
several named ids is the real dependency is exactly the judgment a human
reader should make. The output names the goal, the ids it mentions, and what
was captured; a reader re-writes the defer.

PARTIAL CAPTURE IS THE WORSE HALF. When SOME ids are captured and others are
not, defer-recheck reports a recognized pattern and acts on an incomplete
dependency set — so the defer can CLEAR while an uncaptured dependency is
still open. That reads as a working gate, which is why it is reported
separately from the no-capture case rather than folded in with it.

MEASURED PRECISION, stated because a candidate list read as a defect list is
worse than no list. First live run after the verb filter: scanned 3,035
non-terminal goals, 125 carried a defer, 59 cited a goal id, 12 asserted a
dependency the extractor missed (10 no-capture, 2 partial). A two-row hand
sample of those 12: g-373-78 ("Owned by g-373-16") is a clean true positive;
g-115-8891 names g-115-8930 ("now owned by") as a true positive AND g-115-8898,
which its own text calls DEAD — so ROW precision was 2/2 and ID precision 3/4.
The residual false-positive is a RETRACTED dependency still named in the prose,
which no regex can see. Treat every row as a reading assignment, not a verdict.

Exit codes: 0 always (a detector must never fail the battery). Findings are in
the JSON, never in rc.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# Same id grammar as aspirations.py GOAL_ID_RE admits (the trailing `-[a-z]`
# suffix included) — see defer-recheck.py's DEP_STRUCTURED comment for why the
# suffix is load-bearing.
ANY_GOAL_ID = re.compile(r"\bg-\d+-\d+(?:-[a-z])?\b")

# WHY A VERB IS REQUIRED, measured before this file shipped. The first draft
# flagged any mentioned-but-uncaptured id and returned 63 of 71 — and a
# three-row hand sample falsified most of it: a defer_reason routinely cites
# goal ids as EVIDENCE, not as a dependency.  cites  and
#  as the two sides of a measured comparison;  cites
#  as the record of a rule-axis check. Nothing is waiting on any of
# them. "An id appears and the extractor missed it" is therefore a WEAK
# predicate, and shipping its raw count as a defect list would have handed the
# fleet 60-odd false positives wearing a measured number.
#
# What IS discriminating is a DEPENDENCY VERB next to the id. These are the
# relation words `DEP_PROXIMITY` does not cover — it knows blocked on /
# awaiting / prerequisite / until, and <id> blocked|executes|completes|
# deploys|lands|resolves|fires. Everything below says "this goal's fate hangs
# on that one" in wording the extractor cannot see.
DEP_VERB_BEFORE = re.compile(
    r"(?:tracked (?:on|by)|owned by|gated (?:on|by)|behind|waiting (?:for|on)|"
    r"waits on|pending|blocked by|depends upon|dependent on|needs|requires|"
    r"contingent on|once|after)\s+(?:the\s+)?(?:goal\s+)?"
    r"(g-\d+-\d+(?:-[a-z])?)\b",
    re.IGNORECASE)
DEP_VERB_AFTER = re.compile(
    r"(g-\d+-\d+(?:-[a-z])?)\s+(?:is\s+)?"
    r"(?:owns|must|ships|settles|answers|closes|clears|unblocks|is done|"
    r"is complete|arrives|returns)\b",
    re.IGNORECASE)

# `human_blocked:` NEVER auto-clears by contract (gates/defer_classifier.py),
# so an uncaptured citation there freezes nothing that was not already frozen
# on purpose. Excluded, or the check reports the framework working as designed.
NEVER_AUTO_CLEARS = ("human_blocked:",)

TERMINAL = {"completed", "skipped", "expired", "superseded", "decomposed"}


def _load_defer_recheck():
    """Import defer-recheck.py by path — its filename is not a module name."""
    path = SCRIPT_DIR / "defer-recheck.py"
    spec = importlib.util.spec_from_file_location("defer_recheck_ssot", str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true",
                    help="emit JSON (the default and only machine shape)")
    ap.add_argument("--output", choices=("json", "human"), default="json")
    args = ap.parse_args()

    try:
        ssot = _load_defer_recheck()
    except Exception as e:  # noqa: BLE001 - a detector never fails the battery
        print(json.dumps({"error": f"ssot import failed: {e}",
                          "flagged": 0, "blind": True}))
        return 0

    goals = []
    for source in ("world", "agent"):
        try:
            goals.extend(ssot._read_goals(source))
        except SystemExit:
            # _read_goals exits on a daemon read failure (guard-383). Here a
            # half-view would UNDERCOUNT a detector rather than poison a write,
            # so degrade loudly instead of dying: say which source was blind.
            print(json.dumps({"error": f"{source} read failed",
                              "flagged": 0, "blind": True}))
            return 0

    scanned = 0
    defers_total = 0
    defers_naming_an_id = 0
    asserting = 0
    no_capture = []
    partial_capture = []

    for g in goals:
        if (g.get("status") or "").lower() in TERMINAL:
            continue
        scanned += 1
        reason = g.get("defer_reason") or ""
        if not reason.strip():
            continue
        defers_total += 1

        if reason.lstrip().lower().startswith(NEVER_AUTO_CLEARS):
            continue                    # frozen on purpose; see the note above

        mentioned = []
        for m in ANY_GOAL_ID.finditer(reason):
            gid = m.group(0)
            if gid != g.get("id") and gid not in mentioned:
                mentioned.append(gid)   # never count the goal's own id
        if not mentioned:
            continue                    # population (a): nothing to key on
        defers_naming_an_id += 1

        # Only an id carried by a dependency VERB is a candidate. Citing an id
        # as evidence is the common case and is not a defect (see the measured
        # note on DEP_VERB_BEFORE).
        asserted = []
        for pat in (DEP_VERB_BEFORE, DEP_VERB_AFTER):
            for m in pat.finditer(reason):
                gid = m.group(1)
                if gid != g.get("id") and gid not in asserted:
                    asserted.append(gid)
        if not asserted:
            continue                    # cited, but nothing claims to wait on it
        asserting += 1
        mentioned = asserted

        captured = list(dict.fromkeys(ssot._extract_dep_ids(reason)))
        missed = [gid for gid in mentioned if gid not in captured]
        if not missed:
            continue                    # fully parseable — the healthy case

        row = {
            "goal_id": g.get("id"),
            "source": g.get("_source"),
            "aspiration_id": g.get("_aspiration_id"),
            "status": g.get("status"),
            "priority": g.get("priority"),
            "prefix": reason.split(":", 1)[0][:40] if ":" in reason[:40] else "",
            "mentioned": mentioned,
            "captured": captured,
            "missed": missed,
        }
        (partial_capture if captured else no_capture).append(row)

    flagged = len(no_capture) + len(partial_capture)
    report = {
        # The unfiltered population sits beside the filtered count on purpose:
        # a 0 next to defers_naming_an_id=0 means "nothing cites a goal", and a
        # 0 next to defers_naming_an_id=30 means "all 30 parse". Those are
        # different facts and a bare zero hides which one you have (guard-2298).
        "goals_scanned": scanned,
        "defers_total": defers_total,
        "defers_naming_an_id": defers_naming_an_id,
        "defers_asserting_a_dependency": asserting,
        "flagged": flagged,
        "no_capture_count": len(no_capture),
        "partial_capture_count": len(partial_capture),
        "no_capture": no_capture,
        "partial_capture": partial_capture,
        "remedy": ("rewrite the defer so the dependency reads `depends on: "
                   "<goal-id>` (or `blocked on <goal-id>` / `<goal-id> lands`) "
                   "— guard-7217. Report-only: no --apply exists by design."),
    }

    if args.output == "human":
        print(f"[defer-citation-parity] scanned={scanned} defers={defers_total} "
              f"citing={defers_naming_an_id} FLAGGED={flagged} "
              f"(no-capture {len(no_capture)}, partial {len(partial_capture)})")
        for row in no_capture + partial_capture:
            print(f"  {row['goal_id']} ({row['source']}, {row['priority']}) "
                  f"mentions {row['mentioned']} captured {row['captured']}")
    else:
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
