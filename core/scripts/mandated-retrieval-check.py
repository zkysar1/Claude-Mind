#!/usr/bin/env python3
"""Advisory: hold a goal to a retrieval step its OWN description mandates.

g-115-3282. Filed from g-335-279, which prepended a mandatory
`Step 0: bash core/scripts/retrieve.sh --category vinheim-runtime --goal g-335-09`
to g-335-09's description after run 21 executed with the no-retrieval stub and
re-derived rb-4081 for the ~5th time. That fix was Layer A only -- a
natural-language instruction with NO enforcement, which is the exact shape
guard-399 warns about ("write the corresponding bash gate or hook").

WHY THE OBVIOUS PREDICATE IS NOT THE ONE IMPLEMENTED (measured, do not re-derive)
--------------------------------------------------------------------------------
The originating goal proposed: fire when the description contains the literal
`retrieve.sh --category`. Measured 2026-08-10 (bravo, cc-05, uname -r
6.8.0-136-generic) over the full live corpus -- 6,155 distinct goal records
across pending/in-progress/blocked/completed:

    descriptions containing the literal ............ 21
    of those, an actual MANDATE to the executor .....  2   (g-335-09, g-115-23)
    of those, NARRATION about retrieval machinery ... 19

19/21 = 90.5% false positives. That is guard-1430's "narration class dominates"
condition verbatim, and its instruction is explicit: measure both counts before
shipping, and block only on the live set -- because a gate that fires on
everything "trains the reader to ignore the output," which is worse than a gate
that never fires. The literal-substring predicate is therefore REJECTED, with
evidence, rather than shipped and tuned later.

Note the corpus makes this self-demonstrating: g-115-3282 -- the goal that
proposed the predicate -- contains the literal twice, both times as discussion.
It would have been its own first false positive.

THE PREDICATE THAT IS IMPLEMENTED
---------------------------------
A mandate is SELF-ADDRESSED: it tells *this* goal's executor to run a retrieval
for *this* goal, so it names this goal's own id. Narration quoting an
invocation names some OTHER goal's id (measured: g-335-764, g-115-4633,
g-335-684, g-001-08 ...). So:

    description contains `retrieve.sh --category`  AND  names `--goal <own-id>`

Measured on the same 6,155-goal corpus: 1 fire, 0 false positives, and the one
fire is g-335-09 -- the precise case this goal exists to catch.

Recall is 1 of the 2 mandates: g-115-23's line is a TEMPLATE
(`--category "<subject in domain terms>"`, no --goal at all), not a concrete
self-addressed command, so it is out of scope by construction rather than by
accident. Under-matching is the correct direction here: a missed advisory costs
one un-enforced retrieval, an over-match costs the gate's credibility.

Deliberately NOT tuned into a broader regex on the n=2 mandate population --
that is fitting a rule to two examples (rb-7352: replace an n=2 induction with
a structural proof before scaling). The self-reference IS the structural proof.

ADVISORY ONLY. Always exits 0. Never blocks a close (guard-1562: do not ship a
new check fail-closed). Wired from iteration-close.sh do_learning_gate, on the
no-retrieval branch only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

LITERAL = "retrieve.sh --category"


def mandates_self_retrieval(description, goal_id):
    """True when the description carries a retrieval command addressed to THIS goal.

    Both `--goal <id>` and `--goal=<id>` spellings count. A description that
    merely discusses retrieval, or quotes another goal's invocation, is False.
    """
    if not description or not goal_id:
        return False
    if LITERAL not in description:
        return False
    return ("--goal " + goal_id) in description or ("--goal=" + goal_id) in description


def retrieval_performed_for(session, goal_id):
    """True when retrieval-session.json records a REAL retrieval for THIS goal.

    Two independent ways to be False, and both matter:

    1. `retrieval_performed is False` -- the explicit no-retrieval stub written
       by iteration-close.sh. MUST be `is not False`, never bool()/falsy: the
       real retrieve.py path OMITS the key entirely rather than setting it True,
       so a truthiness test reads every genuine retrieval as a miss. That is the
       g-115-3113 regression class; the reference implementation is
       pre-apply-consult-gate.py (`d.get("retrieval_performed") is not False`).

    2. The manifest belongs to a DIFFERENT goal. retrieval-session.json is
       SINGLE-SLOT per agent and is overwritten by each goal's retrieval, so a
       prior goal's real manifest sitting in the slot would otherwise read as
       this goal's success. iteration-close.sh:2539 already treats a goal_id
       mismatch as stale for its own purposes; this mirrors that rather than
       assuming the file is ours.
    """
    if not isinstance(session, dict):
        return False
    if session.get("goal_id") != goal_id:
        return False
    return session.get("retrieval_performed") is not False


def evaluate(description, session, goal_id):
    """Return (fired, reason). fired=True means the advisory should print."""
    if not mandates_self_retrieval(description, goal_id):
        return (False, "no self-addressed retrieval mandate in description")
    if retrieval_performed_for(session, goal_id):
        return (False, "mandate present and retrieval performed for this goal")
    return (True, "description mandates a retrieval for this goal, "
                  "but no retrieval was recorded for it")


def _load_session(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def description_from_query(raw, goal_id):
    """Pull one goal's description out of `aspirations-query.sh --full` output.

    Tolerant by design: any shape that is not the expected list-of-records
    yields "", which evaluates to "no mandate" and the advisory stays silent.
    A broken or empty query must never manufacture a fire -- the caller is a
    close path, and a false advisory there is worse than a missed one.
    """
    try:
        rows = json.loads(raw)
    except Exception:
        return ""
    if not isinstance(rows, list):
        return ""
    for row in rows:
        if isinstance(row, dict) and row.get("id") == goal_id:
            return row.get("description") or ""
    return ""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--goal-id", required=True)
    ap.add_argument("--session-file", required=True,
                    help="path to retrieval-session.json")
    ap.add_argument("--output", choices=("text", "json"), default="text")
    ap.add_argument("--query-json", action="store_true",
                    help="stdin is `aspirations-query.sh --full` JSON rather "
                         "than raw description text; extract --goal-id's row")
    args = ap.parse_args(argv)

    # Description arrives on stdin so this module spawns no subprocess -- the
    # caller (iteration-close.sh) already has the aspirations wrapper in hand,
    # and shelling out from here would put a bare "bash" in argv[0] (guard-580).
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    description = description_from_query(raw, args.goal_id) if args.query_json else raw

    session = _load_session(args.session_file)
    fired, reason = evaluate(description, session, args.goal_id)

    if args.output == "json":
        print(json.dumps({"goal_id": args.goal_id, "fired": fired,
                          "reason": reason}, indent=2))
    elif fired:
        sys.stderr.write(
            "[mandated-retrieval] ADVISORY: %s declares its own retrieval step "
            "(`%s ... --goal %s`) but closed with no retrieval recorded for it.\n"
            "  The step was written into the goal because a prior run re-derived "
            "knowledge it already had.\n"
            "  Run it before closing, or remove the step from the description if "
            "it no longer applies.\n"
            % (args.goal_id, LITERAL, args.goal_id))
    return 0


if __name__ == "__main__":
    sys.exit(main())
