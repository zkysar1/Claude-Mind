#!/usr/bin/env python3
"""sq013-dedup-probe.py — STATUS-COMPLETE ownership probe for the sq-013
work-discovery replay (g-115-8007).

Problem: the Worker Spark Replay block in .claude/skills/aspirations-spark/
SKILL.md tells the reducer to "Dedup FIRST and not only on the worker's
phrasing" and cites guard-1204 / guard-2228 / guard-3738 — all three about the
PHRASING axis. Nothing said anything about the STATUS axis, so the probe run
before filing scanned OPEN goals only. Measured 2026-08-27, first-hand:

    01:37:19  g-326-711 completed  (denominator instrumentation)
    02:01:19  g-326-712 completed  (server-side log join)
    02:12:21  g-326-714 FILED by the reducer spark replay as their duplicate
    02:20:54  g-326-714 skipped by an alpha worker Body as MOOT ON ARRIVAL

The dedup query was correct and its answer was TRUE: zero LIVE owners. Both
owners had COMPLETED, so an open-only scan could not see them. guard-5176 and
guard-4938 already state the general rule ("a dedup probe run before filing
must scan recently-completed goals"); what was missing was the wiring in this
specific path.

THE COST IS WORSE THAN REDUNDANT WORK, which is why the fix is not merely
tidiness. g-326-714's scope prescribed "one log line on the null branch of
pickNearbyPlayer". The goal it duplicated, g-326-711, exists precisely to
FALSIFY that remedy — the composer's player==null branch is UNREACHABLE. So the
duplicate carried the exact remedy the completed goal had just proven wrong, and
an executor who trusted it would have shipped a permanent zero that reads as a
100% rate. A dedup miss handed a live trap to the next Body.

WHY THIS IS NOT THE goal-duplication GATE. That gate has a `recent_completions`
check, and it is NOT the backstop here for two independent reasons:
  1. guard-4938 measured its completed-side coverage as PARTIAL and says
     explicitly it "must not be treated as the backstop".
  2. gates/goal_duplication.py:733 skips every entry whose `completed_by`
     equals the filing agent. On a one-mind-many-bodies fleet the filer and the
     completer are routinely the SAME agent-name (7 worker SIDs vs 1 reducer,
     all "alpha"), and team-state `recent_completions` carries no SID to tell
     two Bodies apart — so in the measured incident BOTH owners were invisible
     to it by construction. That filter is deliberate and is not touched here;
     this probe is a separate, earlier check that does not care who completed
     the work.

PURE stdin->stdout — this script does NO store I/O of its own, matching the
spark-fire-dedup.py convention. The caller supplies the corpus through the
canonical wrapper so there is exactly one reader of the queue:

    bash core/scripts/aspirations-query.sh \
         --goal-status pending,in-progress,completed,skipped --full \
      | py -3 core/scripts/sq013-dedup-probe.py \
            --subject "<the relay observation>" \
            [--session-start <ISO>] [--window-hours 72]

Exit codes are the decision, so a caller can branch in bash without parsing:
    0  FILE    — no owner found; proceed with the sq-013 filing
    3  DECLINE — an owner exists; stdout names it (id, status, when)
    2  usage / unreadable corpus (never a silent FILE — an unusable corpus is
       not evidence of absence; guard-2298 / verify-before-assuming rule 4)

3 rather than 1 for DECLINE is deliberate, mirroring deploy-hold-check.sh:
collapsing "an owner exists" and "the probe broke" onto one non-zero code makes
each readable as the other, and the caller cannot tell which.
"""

import argparse
import json
import math
import re
import sys
from datetime import datetime, timedelta

# Terminal statuses a duplicate can hide in. `superseded` and `expired` are
# included because guard-4938 names them alongside completed/skipped; a goal in
# any of these states is evidence the work was already considered.
TERMINAL_STATUSES = ("completed", "skipped", "superseded", "expired")
OPEN_STATUSES = ("pending", "in-progress", "blocked")

# Default lookback when no session start is supplied. Deliberately WIDER than
# the 24h the originating goal calls "the wrong shape" — the failure it fixes is
# a window too NARROW, and the cost asymmetry is one-sided: an over-wide window
# costs a cited decline a human can overrule, an under-wide one ships a trap.
DEFAULT_WINDOW_HOURS = 72

# Same 5-char floor and stopword posture as gates/goal_duplication.py's keyword
# extraction, so the two agree on what counts as a discriminating token.
#
# RAW OVERLAP COUNT IS NOT A SIGNAL AT FLEET SCALE, and the live dogfood is what
# proved it. Against the real 3,140-goal corpus this probe first cited
#  ("pipeline-archive.sh has NO scheduled caller") as the owner of a
# pickNearbyPlayer relay, on five shared tokens that were all generic English:
# returns (df=799, 25%), without (795, 25%), selection (171), denominator (150),
# unmeasured (294). The TRUE owner  shared four — but one of them was
# `picknearbyplayer` at df=4 (0.13%). So the discriminator is RARITY, not count,
# exactly as gates/goal_duplication.py concluded (, STRUCT_IDF_DF_CEIL).
# A match must therefore carry at least one rare token; topic words alone are
# a coincidence, not an owner. Fixtures could never have caught this — a
# two-goal fixture corpus has no document frequencies to speak of.
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_\-]{4,}")
_STOPWORDS = {
    "about", "after", "again", "against", "already", "another", "because",
    "before", "being", "between", "could", "during", "every", "found", "goal",
    "goals", "should", "since", "still", "their", "there", "these", "thing",
    "those", "through", "under", "until", "where", "which", "while", "would",
    "agent", "worker", "reducer", "relay", "filed", "filing", "record",
}


# House convention (gates/goal_duplication.py:717): a match needs both a weight
# floor and 2+ distinct tokens.
WEIGHT_THRESHOLD = 1.5
MIN_UNIQUE_HITS = 2

# Below this corpus size there are no meaningful document frequencies, so IDF is
# INERT (every token weighs 1.0 and counts as rare) rather than wrong — the same
# fail-open posture _compute_idf takes on an empty corpus. A fixture corpus lives
# here by construction, which is why fixtures cannot exercise the rarity gate.
MIN_IDF_CORPUS = 20

# A token is RARE when df <= n / this. At the live n=3,140 that is a ceiling of
# 15 docs (0.48%), which admits picknearbyplayer (df=4) and excludes every token
# in the measured false positive (rarest: selection, df=171). Derived from the
# LIVE corpus size rather than fixed, for the reason STRUCT_IDF_DF_CEIL gives: a
# fixed ceiling cannot span corpora of different sizes.
RARE_DF_DIVISOR = 200


def _compute_idf(docs, terms):
    """(idf_map, n) over the goal corpus. Rare tokens weigh high, common ones
    near zero. Inert below MIN_IDF_CORPUS (fail-open, never fail-blind)."""
    n = len(docs)
    if n < MIN_IDF_CORPUS:
        return {t: 1.0 for t in terms}, n
    out = {}
    for t in terms:
        df = sum(1 for d in docs if t in d)
        out[t] = (df, max(0.0, math.log(n / (1 + df))))
    return out, n


def _tokens(text):
    """Discriminating lowercase tokens, stopwords and short words removed."""
    if not text:
        return set()
    return {t.lower() for t in _TOKEN_RE.findall(str(text))
            if t.lower() not in _STOPWORDS}


def _parse_ts(value):
    """Naive ISO timestamp -> datetime, or None. Naive by fleet convention
    (CLAUDE.md: UTC wall time on every box, no zone suffix)."""
    if not value:
        return None
    s = str(value).strip().replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:len(fmt) + 2].rstrip(), fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _goal_time(goal):
    """When this goal last moved. Falls back across the fields different
    writers stamp, newest-intent first."""
    for field in ("completed_date", "lastAchievedAt", "last_modified",
                  "claimed_at", "started", "created"):
        ts = _parse_ts(goal.get(field))
        if ts is not None:
            return ts
    return None


def window_start(now, session_start=None, window_hours=DEFAULT_WINDOW_HOURS):
    """The EARLIER of session start and the fixed lookback.

    The originating goal rejects a fixed 24h window as "the wrong shape" and
    asks for session-scoped. Both are floors, not ceilings: a long session must
    not lose its own early completions, and a short one must not become blinder
    than the plain lookback. Taking the MIN satisfies both, which a single
    anchor cannot.
    """
    fixed = now - timedelta(hours=window_hours)
    if session_start is None:
        return fixed
    return min(fixed, session_start)


def decide(subject, goals, now, session_start=None,
           window_hours=DEFAULT_WINDOW_HOURS, min_overlap=2):
    """Pure decision. Returns a dict; never raises on odd goal records.

    An OPEN owner is disqualifying whenever it overlaps, with no time bound —
    an open goal owns its work however old it is. A TERMINAL owner counts only
    inside the window, because "someone considered this two months ago and
    closed it" is not the same claim as "this was just done".
    """
    subj = _tokens(subject)
    start = window_start(now, session_start, window_hours)
    matches = []

    records = [g for g in (goals or []) if isinstance(g, dict)]
    docs = [_tokens(" ".join(str(g.get(f) or "")
                             for f in ("title", "description")))
            for g in records]
    idf, n = _compute_idf(docs, subj)
    inert = n < MIN_IDF_CORPUS
    rare_ceil = max(2, n // RARE_DF_DIVISOR)

    for g, blob_tokens in zip(records, docs):
        status = (g.get("status") or "").strip().lower()
        overlap = subj & blob_tokens
        if len(overlap) < min_overlap:
            continue
        if inert:
            weight, rare = float(len(overlap)), sorted(overlap)
        else:
            weight = sum(idf[t][1] for t in overlap)
            rare = sorted(t for t in overlap if idf[t][0] <= rare_ceil)
        # BOTH gates. Weight alone does not discriminate: the measured false
        # positive scored 11.04 on generic English, above any sane floor. What
        # separated it from the true owner (16.04) was that the owner shared a
        # RARE identifier and it shared none.
        if weight < WEIGHT_THRESHOLD or not rare:
            continue

        if status in OPEN_STATUSES:
            when, in_window = _goal_time(g), True
        elif status in TERMINAL_STATUSES:
            when = _goal_time(g)
            # An undated terminal goal is AMBIGUOUS, not old. Counting it in is
            # the safe direction: the cost is a cited decline, and the cost of
            # counting it out is the trap this probe exists to stop.
            in_window = (when is None) or (when >= start)
        else:
            continue

        if not in_window:
            continue
        matches.append({
            "goal_id": g.get("id"),
            "status": status,
            "when": when.isoformat() if when else None,
            "overlap": sorted(overlap)[:8],
            "overlap_count": len(overlap),
            "weight": round(weight, 2),
            "rare_tokens": rare[:5],
            "title": (g.get("title") or "")[:120],
        })

    # Strongest signal first, then most recent, so the cited id is the most
    # defensible one rather than whichever the corpus happened to list first.
    # Ranked by WEIGHT, not count — see the module header.
    matches.sort(key=lambda m: (m["weight"], m["when"] or ""), reverse=True)

    if matches:
        top = matches[0]
        return {
            "decision": "DECLINE",
            "reason": ("owner exists: %s (%s%s) shares %d tokens "
                       "(idf weight %.2f, rare: %s) with the relay subject"
                       % (top["goal_id"], top["status"],
                          ", " + top["when"] if top["when"] else "",
                          top["overlap_count"], top["weight"],
                          ", ".join(top["rare_tokens"]) or "none")),
            "cited_goal_id": top["goal_id"],
            "cited_status": top["status"],
            "matches": matches[:10],
            "window_start": start.isoformat(),
            "scanned": len(goals or []),
        }
    return {
        "decision": "FILE",
        "reason": "no open or recently-terminal goal overlaps the relay subject",
        "cited_goal_id": None,
        "cited_status": None,
        "matches": [],
        "window_start": start.isoformat(),
        "scanned": len(goals or []),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--subject", required=True,
                    help="the relay observation being deduped")
    ap.add_argument("--session-start", default=None,
                    help="ISO start of the current session (widens the window)")
    ap.add_argument("--window-hours", type=float, default=DEFAULT_WINDOW_HOURS)
    ap.add_argument("--now", default=None, help="ISO override, for tests")
    ap.add_argument("--min-overlap", type=int, default=2)
    args = ap.parse_args(argv)

    raw = sys.stdin.read()
    if not raw.strip():
        print("sq013-dedup-probe: EMPTY corpus on stdin — refusing to report "
              "FILE. An unreadable corpus is not evidence of absence.",
              file=sys.stderr)
        return 2
    try:
        goals = json.loads(raw)
    except json.JSONDecodeError as e:
        print("sq013-dedup-probe: corpus is not JSON (%s) — refusing to "
              "report FILE." % e, file=sys.stderr)
        return 2
    if not isinstance(goals, list):
        goals = goals.get("goals") or goals.get("results") or []
    if not goals:
        print("sq013-dedup-probe: corpus parsed to ZERO goals — refusing to "
              "report FILE. Run a positive control before believing this "
              "(guard-2298).", file=sys.stderr)
        return 2

    now = _parse_ts(args.now) or datetime.now()
    result = decide(args.subject, goals, now,
                    _parse_ts(args.session_start), args.window_hours,
                    args.min_overlap)
    print(json.dumps(result, indent=2))
    return 3 if result["decision"] == "DECLINE" else 0


if __name__ == "__main__":
    sys.exit(main())
