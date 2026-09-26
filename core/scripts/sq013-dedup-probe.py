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
check (gates/goal_duplication.py:534, `_check_recent_completions`; re-verified
2026-09-06), and it is NOT the backstop here for two independent reasons:
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
    4  MUST-READ (batch mode only) — N records cite a terminal-but-NOT-done
       owner; each needs reading before ANY disposition. See below.
    2  usage / unreadable corpus (never a silent FILE — an unusable corpus is
       not evidence of absence; guard-2298 / verify-before-assuming rule 4)

3 rather than 1 for DECLINE is deliberate, mirroring deploy-hold-check.sh:
collapsing "an owner exists" and "the probe broke" onto one non-zero code makes
each readable as the other, and the caller cannot tell which. 4 is distinct from
3 for the same reason: "an owner exists" and "you must read before deciding" are
different instructions to the caller.

────────────────────────────────────────────────────────────────────────────
BATCH MODE (g-306-458) — the drain shape, satisfying gap-162 by EXTENSION
────────────────────────────────────────────────────────────────────────────

    bash core/scripts/aspirations-query.sh \
         --goal-status pending,in-progress,completed,skipped --full \
      | py -3 core/scripts/sq013-dedup-probe.py \
            --subjects-file <path-to-json-array-of-capture-records> \
            --positive-control \
            [--session-start <ISO>] [--window-hours 72]

gap-162 ("durable spark_capture drain") recurs structurally: workers cannot
file goals, so every worker window produces a capture only a reducer can
drain. It was resolved `satisfied-by-extension` rather than forged as a skill
— the mechanizable core is a parse+dedup+table producer, which is a flag on
this script, not a new SKILL.md. (Skill bodies are loaded into every agent's
system prompt at startup, so a forge is permanent per-turn weight on the whole
fleet; measured 2026-09-06, the corpus stood at 146 directories against a
configured ceiling of 100 that cannot be raised.) The CLASSIFICATION step —
file / cite an owner / explicitly no owner — deliberately stays with the LLM.

Two design requirements, both MEASURED in the drain that registered the gap:

 1. DO NOT PIPE THE CORPUS PER RECORD. The naive shape re-pipes the full
    ~25MB queue into a fresh process for every record — 356MB of stdin for 14
    records. The corpus is already a PARAMETER of `decide()`, so batch mode
    reads it ONCE and calls the pure function N times. Importing `decide` is
    therefore the canonical invocation, not a bypass: `main()` only reads
    stdin, parses, and calls it.

 2. A DECLINE IS NOT A DISPOSITION. rc=3 must be re-read against the cited
    owner's STATUS and CLAIM. In that same drain, 12 of 14 declines survived
    reading and TWO did not — both cited one SKIPPED goal whose own
    outcome_note asserts the OPPOSITE of the observations it was suppressing.
    Both were real, unowned work. So a DECLINE citing a terminal-but-not-done
    owner (skipped / superseded / expired) is reported as MUST-READ, carrying
    that owner's STATUS and TITLE, and the batch exits 4. guard-5147: a false
    DECLINE is the silent, PERMANENT failure direction — nothing re-opens it.
    A COMPLETED owner stays a plain DECLINE; it is a legitimate one, and that
    distinction is what keeps MUST-READ meaningful.

`--positive-control` probes an alien-token subject against the SAME loaded
corpus and reports whether it still returns FILE. Without it, a probe that
declines EVERYTHING is indistinguishable from a working probe over a
well-owned corpus. The tokens must be genuinely alien, not merely
nonsense-sounding: ordinary English like "resurfacing" or "audit" is corpus
vocabulary and will match (guard-5889).

Batch output is never clipped (guard-5893) and always states the POPULATION
it actually scanned (guard-3696) — a clean sweep and a blind one are otherwise
identical on the page. One malformed record is counted UNREADABLE and the walk
continues (guard-1512); the slot has no schema, so several observation keys are
tried and the key distribution is reported (guard-4044).
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

# ── batch mode () ────────────────────────────────────────────────
# TERMINAL, BUT NOT DONE. A `completed` owner is evidence the work HAPPENED, so
# declining to it is correct. These three are evidence only that someone once
# CONSIDERED it — a skipped goal's own outcome_note can assert the OPPOSITE of
# the observation it is suppressing, and then the decline is a silent, permanent
# loss (guard-5147: a false DECLINE is the failure direction that never
# surfaces). Measured in the  encounter-2 drain: 12 of 14 declines
# survived reading and TWO did not — both cited  (status=skipped),
# and both were real unowned work, filed afterwards as /.
# So batch mode never collapses these to a bare DECLINE; it flags them MUST-READ.
MUST_READ_STATUSES = ("skipped", "superseded", "expired")

# The capture slot has NO schema — workers write whatever key they like
# (guard-4044). A literal `record["observation"]` read silently drops every
# entry that used a different name, and the drop is invisible because the
# surviving entries process normally. So try alternates IN ORDER and REPORT the
# key distribution rather than assuming one shape.
OBSERVATION_KEYS = ("observation", "content", "text", "note", "summary",
                    "finding", "body", "message", "proposed_work")

# Tokens for the positive control. A token-overlap probe that DECLINES
# everything is indistinguishable from a working probe over a well-owned
# corpus, so batch mode can run an alien subject and assert FILE. These must be
# guaranteed-absent, NOT merely nonsense-SOUNDING: ordinary English words like
# "resurfacing" or "audit" are corpus vocabulary and will match (guard-5889).
POSITIVE_CONTROL_SUBJECT = (
    "zzqqxvv7 wgrblmk4 pflunzt9 hjxdvqw2 kbrmtzy6 nvxqplj3 "
    "dfgwzkr8 tqmvbxn5 lkzjrwc1 ybnpxvg0"
)

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

# When the SUBJECT itself carries no rare token (subj_rare empty), the `not
# rare` gate below is vacuous and silently drops true duplicates whose whole
# vocabulary is common (). The waiver that rescues that case fires
# ONLY for a token-identical-TITLE restatement with at least this many
# overlapping tokens -- the tightest form, because a false DECLINE is the
# permanent, invisible failure direction (guard-5147).
ALLCOMMON_TITLE_DUP_MIN_OVERLAP = 4

# BM25 length normalisation (). `weight` is an unnormalised SUM over
# the overlap, so a long record wins by SIZE alone: more tokens means more
# chances to overlap any subject, and more chances to contain SOME rare token by
# coincidence. Measured on the live 2,883-goal corpus (2026-08-30): the median
# blob is 127 unique tokens, p99 is 651 and the largest is 1,544 -- 12x median.
# A merge-wedge relay was DECLINED citing  (1,107 tokens, a goal about
# an unrelated sweep) on the single coincidental token `granularity`, while the
# owner  (147 tokens, sharing the rare `ayoai-journal-md` df=2,
# `hand-resolved` df=2 and `same-heading` df=10) ranked BELOW it. Dividing by the
# standard BM25 factor puts the genuine owner first.
#
# NOTE THE REPORTED MECHANISM WAS WRONG AND THE CORRECTION IS THE POINT: the
# report blamed `outcome_note` blobs. outcome_note has NEVER been in the blob
# (only title+description, in every commit this file has ever had), so excluding
# it would have shipped a no-op that read as a fix. The sponge is the
# DESCRIPTION -- on  it is 71,504 chars against a 2,057-char
# outcome_note, 35x the field that was blamed.
#
# b=0.75 is the BM25 default. The factor is exactly 1.0 at |D| == avgdl, so an
# average-length record scores as it always did and WEIGHT_THRESHOLD keeps the
# meaning it was calibrated with. sqrt/log normalisation (the report's
# suggestion) rescales the whole distribution instead; measured, no true owner
# fell under the floor with either, so that is a robustness argument for this
# form rather than a defect found in the others.
LENGTH_NORM_B = 0.75


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


def _length_norm(doc_len, avgdl):
    """BM25 document-length divisor: 1.0 at average length, >1 for sponges,
    <1 for short records. Fails open to 1.0 on an empty corpus."""
    if not avgdl:
        return 1.0
    return (1.0 - LENGTH_NORM_B) + LENGTH_NORM_B * (doc_len / float(avgdl))


def _tokens(text):
    """Discriminating lowercase tokens, stopwords and short words removed."""
    if not text:
        return set()
    return {t.lower() for t in _TOKEN_RE.findall(str(text))
            if t.lower() not in _STOPWORDS}


# ── Subject headline cap () ──────────────────────────────────────
# A relay observation is a 1-2 KB blob whose FIRST sentence(s) state the finding;
# the rest is cited evidence -- dozens of goal ids, guard ids and filenames, each
# a token the live IDF marks RARE. Scoring the whole blob lets any unrelated
# owner that coincidentally shares ONE body identifier clear the weight+rare gate
# and produce a DECLINE -- the silent, permanent false-DECLINE direction this
# probe's own guard-5147 names. Measured (): 6-of-11 and 3-of-9
# wrong-subject declines on live batches; false pairs scored idf-weight 26-34 on
# coincidental body tokens while genuine pairs scored 138-416 on the shared
# SUBJECT. The headline carries what the relay is ABOUT; a genuine duplicate
# shares THAT, an accidental collision shares a body identifier. Capping the
# SCORED subject to its headline removes the evidence tail from the overlap set.
#
# Fails SAFE by construction: dropping subject text can only REMOVE overlap, so
# the worst case is a false FILE (a dedup-able duplicate), never the false
# DECLINE guard-5147 forbids.
SUBJECT_HEADLINE_MAX_CHARS = 400   # hard ceiling for a run-on first sentence
SUBJECT_HEADLINE_MIN_CHARS = 120   # never cut a sentence shorter than this: an
#   "envelope" observation ("relaying for reducer to file. <proposed_work>") has
#   a tiny first sentence, so cutting there would drop the proposed_work the
#    fix appends; the floor keeps envelope + proposed_work head.


def _headline(text):
    """The relay's 'aboutness' for dedup scoring: its opening, bounded.

    No-op for any subject <= SUBJECT_HEADLINE_MAX_CHARS (every short subject,
    including the test corpus and the positive control). For a longer subject,
    cut at the first sentence terminator (.;!? FOLLOWED BY whitespace, or a
    newline) when that lands at or beyond the MIN floor, else at the char cap.
    A dot inside a token (filename `x.j2`, version `6.06x`) is never followed by
    whitespace, so it is never a boundary."""
    s = str(text or "").strip()
    if len(s) <= SUBJECT_HEADLINE_MAX_CHARS:
        return s
    m = re.search(r"[.;!?](?=\s)|\n", s[:SUBJECT_HEADLINE_MAX_CHARS])
    if m and m.start() + 1 >= SUBJECT_HEADLINE_MIN_CHARS:
        return s[:m.start() + 1]
    return s[:SUBJECT_HEADLINE_MAX_CHARS]


# ── Subject coverage floor (, second half) ───────────────────────
# The headline cap removed the evidence TAIL, but a coincidental owner can still
# share one rare token inside the headline itself: 5 of the goal's 9 named false
# pairs still cleared the weight+rare gates after the cap (measured 2026-09-25,
# cc-08, live 3,664-goal corpus). Weight cannot separate them, because it grows
# with how much text overlaps: false owners scored 14.6-32.6 while a genuine
# owner ( for relay 42c6c1e1d4) scored 15.6.
#
# What does separate them is how much of the relay's aboutness the owner
# RESTATES: the idf-weighted share of the headline's tokens that the owner also
# carries. The 8 false pairs whose owner record still exists covered 0.08-0.23
# of their headline ( is in no queue on this box); the 4 named
# genuine relays covered 0.52-0.90, and across the 33 archived relays of the
# goal's source goals no owner confirmed genuine by reading its title covered
# less than 0.33. A shared rare token is evidence of identity only when the
# owner also restates the subject around it. Over all 2,078 archived sq-013
# relays the gate turned 1,096 of 1,817 DECLINEs into FILE and added none; a
# 16-row random read of those flips found 13 coincidental owners, 3 borderline.
#
# Two floors, because one shared rare token is the weaker identity signal: 5 of
# the 5 false pairs that survived the cap shared exactly one. The floors sit
# between the measured classes (n=33 relays, not a validated threshold). Fails
# SAFE like the cap: the floor only removes candidates, so its worst case is a
# false FILE (a dedup-able duplicate), never the false DECLINE guard-5147 names.
# Live-IDF branch only; an inert (fixture-sized) corpus has no idf to weight by.
SUBJECT_COVERAGE_MIN = 0.40              # candidate shares ONE rare token
SUBJECT_COVERAGE_MIN_MULTI_RARE = 0.30   # candidate shares two or more

# Coverage is read from the OWNER's opening too, for the mirror-image reason the
# subject is capped: a long record covers any subject's tokens by size alone.
# Measured on the first cut of this gate (2,078 archived sq-013 relays, live
# corpus): when it dropped a short coincidental owner, the next survivor was a
# sponge -- the re-cited owners had median 366 tokens against 94 for the owners
# they replaced, 62% of them over 2x the corpus average, and the most re-cited
# was a recurring goal with a 71k-char description. Title plus this many
# description chars is roughly an average-length record; the genuine owners in
# the regression fixture restate their relay inside it.
OWNER_HEAD_CHARS = 2500


def _owner_head_tokens(goal):
    """Tokens of the owner's title plus the opening of its description."""
    return _tokens("%s %s" % (goal.get("title") or "",
                              str(goal.get("description") or "")[:OWNER_HEAD_CHARS]))


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
    # Score the relay's HEADLINE, not its evidence body (): a long
    # relay's cited-identifier tail is what lets an unrelated owner win the rare
    # gate by coincidence. No-op for short subjects (see _headline).
    subj = _tokens(_headline(subject))
    start = window_start(now, session_start, window_hours)
    matches = []

    records = [g for g in (goals or []) if isinstance(g, dict)]
    docs = [_tokens(" ".join(str(g.get(f) or "")
                             for f in ("title", "description")))
            for g in records]
    titles = [_tokens(g.get("title") or "") for g in records]
    idf, n = _compute_idf(docs, subj)
    inert = n < MIN_IDF_CORPUS
    rare_ceil = max(2, n // RARE_DF_DIVISOR)
    avgdl = (sum(len(d) for d in docs) / float(len(docs))) if docs else 0.0
    # Subject's OWN rare tokens (live IDF only; inert corpora have none by
    # construction). When empty, the `not rare` veto below has no premise to
    # enforce -- see the all-common waiver at the gate.
    subj_rare = {t for t in subj
                 if not inert and t in idf and idf[t][0] <= rare_ceil}
    subj_idf_total = 0.0 if inert else sum(idf[t][1] for t in subj)

    for g, blob_tokens, cand_title in zip(records, docs, titles):
        status = (g.get("status") or "").strip().lower()
        overlap = subj & blob_tokens
        if len(overlap) < min_overlap:
            continue
        if inert:
            weight, rare = float(len(overlap)), sorted(overlap)
        else:
            # Length-normalised: an unnormalised sum lets a sponge outrank the
            # genuine owner. The INERT branch above is deliberately left raw --
            # there IDF is off, so there is no idf-sum for a length factor to
            # correct, and fixture corpora all live there.
            weight = (sum(idf[t][1] for t in overlap)
                      / _length_norm(len(blob_tokens), avgdl))
            rare = sorted(t for t in overlap if idf[t][0] <= rare_ceil)
        # Weight gate first. Weight alone does not discriminate: the measured
        # false positive scored 11.04 on generic English, above any sane floor.
        if weight < WEIGHT_THRESHOLD:
            continue
        # Rare gate. What separated the false positive from the true owner
        # (16.04) was that the owner shared a RARE identifier and it shared
        # none. But that premise assumes the SUBJECT has a rare identifier to
        # share. When it does not (subj_rare empty), requiring one silently
        # drops true duplicates whose vocabulary is entirely common
        # (:  self-matched at weight 3.55, subject-coverage
        # 1.0, and was vetoed then re-filed). Waive the veto ONLY for an
        # all-common subject that a candidate restates VERBATIM at the title
        # level -- subject fully covered AND the candidate's whole title
        # covered, >= ALLCOMMON_TITLE_DUP_MIN_OVERLAP tokens. This is the one
        # path that can newly produce a DECLINE, so it is the tightest:
        # subj_rare non-empty keeps the generic decoy () suppressed,
        # and the title-identity guard keeps a description-only collision (an
        # alarm vs its own fix) from declining. (guard-5147: a false DECLINE is
        # the silent, permanent failure direction.)
        if not rare:
            if subj_rare:
                continue
            if (len(overlap) < ALLCOMMON_TITLE_DUP_MIN_OVERLAP
                    or len(overlap) != len(subj)
                    or not cand_title
                    or not subj.issuperset(cand_title)):
                continue
        # Coverage gate: the owner must restate the subject, not merely share
        # one identifier with it (see SUBJECT_COVERAGE_MIN).
        if inert:
            coverage = len(overlap) / float(len(subj))
        else:
            head_overlap = overlap & _owner_head_tokens(g)
            coverage = (sum(idf[t][1] for t in head_overlap) / subj_idf_total
                        if subj_idf_total else 1.0)
            floor = (SUBJECT_COVERAGE_MIN_MULTI_RARE if len(rare) >= 2
                     else SUBJECT_COVERAGE_MIN)
            if coverage < floor:
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
            "coverage": round(coverage, 2),
            "rare_tokens": rare[:5],
            "title": (g.get("title") or "")[:120],
        })

    # Strongest signal first, then most recent, so the cited id is the most
    # defensible one rather than whichever the corpus happened to list first.
    # Ranked by LENGTH-NORMALISED weight, not count — see the module header.
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


def flatten_proposed_work(val):
    """Pure. Render a `proposed_work` value as scorable text, or "" if it has none.

    MEASURED SHAPES, not assumed ones (guard-645). Over the live 2,082-record
    capture corpus on 2026-09-21, `proposed_work` occurs in exactly THREE
    records: one `list[dict]` carrying title/detail/priority per item, and two
    plain `str`. Both are handled; anything else renders empty rather than
    raising, because one malformed record must never abort the walk
    (guard-1512).

    `priority` is deliberately NOT rendered. It is routing metadata, not
    content, and folding it in would inject the corpus-common token "HIGH" into
    every subject — which a token-overlap scorer reads as signal.
    """
    if isinstance(val, str):
        return val.strip()
    if not isinstance(val, list):
        return ""
    parts = []
    for item in val:
        if isinstance(item, str):
            if item.strip():
                parts.append(item.strip())
        elif isinstance(item, dict):
            for field in ("title", "detail"):
                sub = item.get(field)
                if isinstance(sub, str) and sub.strip():
                    parts.append(sub.strip())
    return " ".join(parts)


def extract_subject(record):
    """Pure. Return (text, key_used) for one capture record, or (None, None).

    Tries OBSERVATION_KEYS in order rather than reading `observation` literally
    (guard-4044). A record that is a bare string is its own subject — the slot
    has no schema, so that shape occurs.

    `proposed_work` is then APPENDED rather than competing in that order, and
    the two halves of that sentence are each load-bearing (g-115-10347):

    APPENDED, because first-match-wins made the key unreachable in practice.
    `observation` is FIRST in OBSERVATION_KEYS and present on 2,074 of 2,082
    live records, so any record carrying both scored on `observation` alone.
    When that observation is a contentless envelope ("relaying for reducer to
    file"), the probe scored the envelope, matched nothing, and returned FILE
    with an empty candidate list — which reads exactly like honest novelty
    (guard-7119). The measured instance would have duplicated two HIGH
    money-path goals.

    NOT PROMOTED above `observation`, because that would change the scored
    subject for records where both carry real content, and a change to what a
    scoring analyzer OBSERVES changes the metric's semantics (rb-4988). Appending
    is additive: for the 2,079 records with no `proposed_work` the subject is
    byte-identical to what it was before.

    The key is reported as a COMPOSITE ("observation+proposed_work") so
    `subject_keys_used` still enumerates every key actually consumed — the field
    that made this diagnosable in the first place, and an outcome of the goal.
    """
    if isinstance(record, str):
        return (record.strip() or None), ("<bare-string>" if record.strip() else None)
    if not isinstance(record, dict):
        return None, None
    base, base_key = None, None
    for key in OBSERVATION_KEYS:
        if key == "proposed_work":
            continue  # appended below, never the first-match winner
        val = record.get(key)
        if isinstance(val, str) and val.strip():
            base, base_key = val.strip(), key
            break
    extra = flatten_proposed_work(record.get("proposed_work"))
    if extra and base:
        return (base + " " + extra), (base_key + "+proposed_work")
    if extra:
        return extra, "proposed_work"
    return base, base_key


def batch_decide(records, goals, now, session_start=None,
                 window_hours=DEFAULT_WINDOW_HOURS, min_overlap=2):
    """Pure. Run `decide` over N records against ONE already-loaded corpus.

    This is the whole point of batch mode: the naive shape pipes the full
    corpus into a fresh process per record, which measured 356MB of stdin for
    14 records (g-306-458). The corpus is a parameter of `decide`, so caching it
    once and calling the function N times is the canonical invocation.

    Returns a dict with `rows` (ONE per input record, never clipped —
    guard-5893) and a `population` block naming what was actually scanned
    (guard-3696). A record that cannot be parsed is SKIPPED and COUNTED, never
    fatal: one malformed record must not abort the walk and silently disable the
    sweep for everything after it (guard-1512).
    """
    rows, key_counts = [], {}
    unreadable = 0
    for idx, record in enumerate(records or []):
        subject, key = extract_subject(record)
        if not subject:
            unreadable += 1
            rows.append({
                "index": idx,
                "goal_id": (record.get("goal_id")
                            if isinstance(record, dict) else None),
                "verdict": "UNREADABLE",
                "subject_key": None,
                "reason": ("no non-empty value under any of %s — the slot has "
                           "no schema (guard-4044)" % (", ".join(OBSERVATION_KEYS))),
                "cited_goal_id": None, "cited_status": None,
                "cited_title": None, "must_read": False, "matches": [],
            })
            continue
        key_counts[key] = key_counts.get(key, 0) + 1
        try:
            res = decide(subject, goals, now, session_start,
                         window_hours, min_overlap)
        except Exception as exc:                       # never fatal (guard-1512)
            unreadable += 1
            rows.append({
                "index": idx,
                "goal_id": (record.get("goal_id")
                            if isinstance(record, dict) else None),
                "verdict": "PROBE-ERROR", "subject_key": key,
                "reason": "%s: %s" % (type(exc).__name__, exc),
                "cited_goal_id": None, "cited_status": None,
                "cited_title": None, "must_read": False, "matches": [],
            })
            continue
        top = (res.get("matches") or [{}])[0] if res.get("matches") else {}
        cited_status = res.get("cited_status")
        must_read = (res["decision"] == "DECLINE"
                     and (cited_status or "").lower() in MUST_READ_STATUSES)
        rows.append({
            "index": idx,
            "goal_id": (record.get("goal_id")
                        if isinstance(record, dict) else None),
            # A DECLINE against a terminal-but-not-done owner is NOT a
            # disposition — it is a reading assignment (outcome 3).
            "verdict": "MUST-READ" if must_read else res["decision"],
            "subject_key": key,
            "reason": res.get("reason"),
            "cited_goal_id": res.get("cited_goal_id"),
            "cited_status": cited_status,
            "cited_title": top.get("title"),
            "must_read": must_read,
            "subject": subject[:200],
            "matches": res.get("matches") or [],
        })
    return {
        "rows": rows,
        "population": {
            "records_in": len(records or []),
            "records_scored": len(records or []) - unreadable,
            "records_unreadable": unreadable,
            "corpus_goals": len(goals or []),
            "subject_keys_used": key_counts,
        },
        "must_read_count": sum(1 for r in rows if r["must_read"]),
        "file_count": sum(1 for r in rows if r["verdict"] == "FILE"),
        "decline_count": sum(1 for r in rows if r["verdict"] == "DECLINE"),
    }


def render_batch(result):
    """Human-readable table. Every row is printed — a dedup probe clipped to
    the first N is not a dedup probe (guard-5893)."""
    out, pop = [], result["population"]
    out.append("POPULATION: %d record(s) in, %d scored, %d unreadable, "
               "against %d corpus goal(s)"
               % (pop["records_in"], pop["records_scored"],
                  pop["records_unreadable"], pop["corpus_goals"]))
    out.append("SUBJECT KEYS USED: %s"
               % (", ".join("%s=%d" % kv for kv in
                            sorted(pop["subject_keys_used"].items())) or "none"))
    out.append("")
    for r in result["rows"]:
        out.append("[%d] %-9s %s" % (r["index"], r["verdict"],
                                     r.get("goal_id") or ""))
        if r["cited_goal_id"]:
            out.append("      owner: %s  STATUS=%s" % (r["cited_goal_id"],
                                                       r["cited_status"]))
            out.append("      title: %s" % (r["cited_title"] or "(none)"))
        if r["must_read"]:
            out.append("      ^^ TERMINAL-BUT-NOT-DONE owner. Read its "
                       "outcome_note before accepting this as a decline: a "
                       "skipped/expired goal can assert the OPPOSITE of the "
                       "observation it suppresses (guard-5147).")
        elif r["verdict"] in ("UNREADABLE", "PROBE-ERROR"):
            out.append("      %s" % r["reason"])
    out.append("")
    out.append("TOTALS: file=%d decline=%d must-read=%d"
               % (result["file_count"], result["decline_count"],
                  result["must_read_count"]))
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--subject", default=None,
                    help="the relay observation being deduped")
    ap.add_argument("--subjects-file", default=None,
                    help="BATCH MODE (g-306-458): path to a JSON array of "
                         "capture records. The corpus is still read ONCE from "
                         "stdin and reused for every record.")
    ap.add_argument("--positive-control", action="store_true",
                    help="also probe an alien-token subject and FAIL if it "
                         "does not return FILE — a probe that declines "
                         "everything is indistinguishable from a working one "
                         "(guard-5889).")
    ap.add_argument("--session-start", default=None,
                    help="ISO start of the current session (widens the window)")
    ap.add_argument("--window-hours", type=float, default=DEFAULT_WINDOW_HOURS)
    ap.add_argument("--now", default=None, help="ISO override, for tests")
    ap.add_argument("--min-overlap", type=int, default=2)
    args = ap.parse_args(argv)

    # Exactly one subject source. Enforced here rather than by
    # required=True on --subject, which would make batch mode unreachable.
    if bool(args.subject) == bool(args.subjects_file):
        print("sq013-dedup-probe: pass exactly ONE of --subject <text> or "
              "--subjects-file <path> (batch).", file=sys.stderr)
        return 2

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
    session_start = _parse_ts(args.session_start)

    # Positive control (guard-5889). Runs against the SAME loaded corpus, so it
    # proves this run's probe can still say FILE — not merely that it could in
    # principle. Alien tokens, not nonsense-sounding English.
    control_failed = False
    if args.positive_control:
        ctl = decide(POSITIVE_CONTROL_SUBJECT, goals, now, session_start,
                     args.window_hours, args.min_overlap)
        ok = ctl["decision"] == "FILE"
        print("POSITIVE CONTROL: alien-token subject -> %s%s"
              % (ctl["decision"],
                 "" if ok else "  <-- FAILED: this probe declines everything, "
                               "so no DECLINE below is trustworthy"),
              file=sys.stderr)
        control_failed = not ok

    if args.subjects_file:
        try:
            with open(args.subjects_file, "r", encoding="utf-8") as fh:
                records = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            print("sq013-dedup-probe: --subjects-file unreadable (%s) — "
                  "refusing to report anything. An unreadable input is not "
                  "evidence of absence." % exc, file=sys.stderr)
            return 2
        if isinstance(records, dict):
            records = (records.get("records") or records.get("entries")
                       or records.get("spark_capture") or [])
        if not isinstance(records, list) or not records:
            print("sq013-dedup-probe: --subjects-file parsed to ZERO records "
                  "— refusing to report a clean sweep (guard-2298).",
                  file=sys.stderr)
            return 2
        result = batch_decide(records, goals, now, session_start,
                              args.window_hours, args.min_overlap)
        print(render_batch(result))
        print(json.dumps(result, indent=2), file=sys.stderr)
        if control_failed:
            return 2
        # 4, not 3: a MUST-READ batch is not "an owner exists", it is "N records
        # need reading before any disposition". Collapsing them makes each
        # readable as the other, the same reason DECLINE is 3 and not 1.
        return 4 if result["must_read_count"] else 0

    result = decide(args.subject, goals, now, session_start,
                    args.window_hours, args.min_overlap)
    print(json.dumps(result, indent=2))
    if control_failed:
        return 2
    return 3 if result["decision"] == "DECLINE" else 0


if __name__ == "__main__":
    sys.exit(main())
