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
            [--census-file <(bash core/scripts/aspirations-read.sh \
                              --source world --active-compact)] \
            [--session-start <ISO>] [--window-hours 72]

Exit codes are the decision, so a caller can branch in bash without parsing:
    0  FILE    — no owner the corpus could SCORE; proceed with the filing
    3  DECLINE — an owner exists; stdout names it (id, status, when)
    4  MUST-READ — read before ANY disposition: in batch mode a record cites a
       terminal-but-NOT-done owner (see below); in either mode, with
       --census-file, the subject cites a goal EVICTED from the corpus; in
       either mode, every owner found restates the subject only below
       SUBJECT_COVERAGE_MIN — the same TOPIC, not proven the same defect
       (g-115-11127, see SUBJECT_COVERAGE_MIN).
    2  usage / unreadable corpus or census (never a silent FILE — an unusable
       input is not evidence of absence; guard-2298 / verify-before-assuming
       rule 4)

THE CORPUS HAS A HORIZON (g-306-522). The live queue EVICTS terminal
non-recurring goals after aspirations_eviction.age_days (3); they survive only
as bare ids in their aspiration's census (_goal_census.py). An evicted owner
cannot be scored, so FILE means "no LIVE owner", never "no owner ever" —
measured on the g-306-284 occ227 replay (2026-09-27): all 16 aged work relays
that read FILE had an owner or were moot, and 8 of them needed owner ids only
the census holds (seven ids: six completed, one skipped). So FILE output states
the horizon, and --census-file (the caller's aspiration records; repeatable)
turns a FILE into MUST-READ when the relay CITES an evicted goal id, in its
subject text or (batch) as its own source goal_id. An uncited
evicted owner stays invisible: an aged relay is disposed by re-running its
reproduction against HEAD, not by this probe (guard-7398). This script reads
one CONFIG value (aspirations_eviction) for the horizon; it still does no store
I/O. Coverage is exactly what the caller passes: on 2026-09-28 (cc-09)
`aspirations-read.sh --source world --active-compact` held 11,879 evicted ids,
`--source world --archive` 957 more and `--source agent --active-compact` 115.

PROVENANCE BEFORE SIMILARITY (g-115-11192). A re-delivered relay read FILE
against the replay bundle that already carried it (6 of the 8 FILEs at the
2026-09-27 17:40 replay). Not by dilution: the bundle's weight cleared its
floor, and the coverage floor, which reads only an owner's first
OWNER_HEAD_CHARS, never reached a relay carried deeper in. Two EXACT keys
therefore run before any token is scored: the record's (goal_id, _item_ts)
against a line that carries it, and the relay's "Suggested title:" line
against goal titles (rb-9493). A hit is a DECLINE. The measurement, the
grammar, the two exclusions and what stays uncovered are in the
provenance-keys block above decide().

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
            --positive-control [--census-file <aspiration records>] \
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
import os
import re
import sys
from datetime import datetime, timedelta

from _goal_census import all_evicted_ids, evicted_status_in

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

# ── The multi-rare band is a READING ASSIGNMENT, not a DECLINE () ──
# Two shared rare tokens are no stronger identity evidence than one when both
# NAME THE SAME COMPONENT: a file and its field, a slot and its flag. Any goal
# about that component carries both, whatever defect it tracks. Measured on the
#  replays (2026-09-27/28, live 3,989-goal corpus): both false DECLINEs
# of the occ228 batch -- a mirror-health class-(a) relay cited  (a
# stale-streak repair goal) on owncloud-conflict-streaks + diverged_skipped at
# coverage 0.36; an encoding_capture non-dict relay cited  (a
# staged-WM drain goal) on encoding_capture + load_bearing at 0.34 -- and 4 of
# the 5 occ238 wrong-owner DECLINEs (0.33-0.39, including 's truly
# unowned work) sat in [MULTI_RARE, MIN). The batch's 5 correct DECLINEs sat at
# 0.41-0.89. In the 2026-08-24 capture cohort 10 of 61 DECLINEs fell in the
# band; reading each owner's title against its relay's headline found 7 wrong
# owners and 3 plausible ones.
#
# So a candidate admitted ONLY by the multi-rare floor is kept and CITED -- it
# may still be the owner -- but the decision is MUST-READ, never a terminal
# DECLINE: a false DECLINE is deleted with its relay at drain (guard-5147),
# while MUST-READ is audible (rc 4). A candidate at or above
# SUBJECT_COVERAGE_MIN outranks any band candidate, however heavy, so a real
# owner is never hidden behind a same-topic one. The title-token test the goal
# proposed was measured and REJECTED: two of the five correct DECLINEs share no
# title token with their owner (a relay writes `closure-evidence-write`, the
# owner's title says `closure-evidence`), as do three of the six wrong owners,
# while another wrong owner's title shares three.
# Residual, stated so nobody reads this as a class fix: a same-topic owner at
# coverage >= 0.40 still DECLINEs (occ238's  relay cited 
# at 0.45), which is why every cited owner's matched span is printed.

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


def _owner_window(goal, start):
    """(status, when) when `goal` can own a relay at all, else None. ONE rule
    for the token path and the provenance keys (g-115-11192). An OPEN goal owns
    its work however old it is; a TERMINAL one counts only inside the window.
    An undated terminal goal is AMBIGUOUS, not old. Counting it in is the safe
    direction: the cost is a cited decline, and the cost of counting it out is
    the trap this probe exists to stop."""
    status = (goal.get("status") or "").strip().lower()
    if status in OPEN_STATUSES:
        return status, _goal_time(goal)
    if status in TERMINAL_STATUSES:
        when = _goal_time(goal)
        if when is None or when >= start:
            return status, when
    return None


# ── eviction horizon () ─────────────────────────────────────────────
# Goal ids a relay cites. Word-bounded, so a board id like "msg-2026..." never
# reads as one.
_GOAL_ID_RE = re.compile(r"\bg-\d+-\d+\b")


def evicted_citations(subject, goals, census, source_id=None):
    """Pure. Goal ids the relay cites that the corpus cannot score because they
    were EVICTED: [{"goal_id", "status", "via"}], via the shared census helper
    (guard-5278). `source_id` is the capture record's own goal_id, checked first
    (via="source"): the relay's source goal can itself be the owner, as when
    g-363-177 was filed for work that had shipped under its source g-363-72.
    Ids in the subject text follow (via="subject"). An id the corpus holds was
    scored, so it is skipped. None when no census was supplied: NOT CHECKED
    must never read as "checked, none" (guard-1753)."""
    if census is None:
        return None
    live = {g.get("id") for g in (goals or []) if isinstance(g, dict)}
    cands = [(source_id, "source")] if isinstance(source_id, str) else []
    cands += [(gid, "subject")
              for gid in _GOAL_ID_RE.findall(str(subject or ""))]
    out, seen = [], set()
    for gid, via in cands:
        if not gid or gid in seen or gid in live:
            continue
        seen.add(gid)
        status = evicted_status_in(census, gid)
        if status:
            out.append({"goal_id": gid, "status": status, "via": via})
    return out


def _fmt_cites(cites):
    return ", ".join("%s (%s%s)" % (c["goal_id"], c["status"],
                                    "; the relay's source goal"
                                    if c.get("via") == "source" else "")
                     for c in cites)


def _evicted_reason(cites):
    return ("no owner the corpus could score, but the relay cites terminal "
            "goal(s) EVICTED from it: %s" % _fmt_cites(cites))


def load_eviction_config(path=None):
    """(block, error): `aspirations_eviction` from core/config/aspirations.yaml,
    the file aspirations-evict-tick.sh reads. Never raises."""
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "config", "aspirations.yaml")
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as fh:
            block = (yaml.safe_load(fh) or {}).get("aspirations_eviction")
    except Exception as exc:                       # reported, never defaulted
        return None, "%s: %s" % (type(exc).__name__, exc)
    if not isinstance(block, dict):
        return None, "no aspirations_eviction block in %s" % path
    return block, None


def terminal_horizon(now, block, error=None):
    """Pure. The oldest terminal owner a LIVE-queue corpus can still hold.

    Evicted goals are census-only, so a FILE is "no owner since the horizon",
    never "no owner ever". An unreadable config is UNKNOWN, never a default: a
    guessed horizon would state coverage nobody measured (guard-1753)."""
    try:
        age = float((block or {})["age_days"])
    except (KeyError, TypeError, ValueError):
        return {"state": "unknown", "horizon": None, "age_days": None,
                "detail": "UNKNOWN (%s)" % (error or "no readable age_days")}
    if not (block.get("enabled") and block.get("apply")):
        return {"state": "inactive", "horizon": None, "age_days": age,
                "detail": ("none: eviction is off (enabled=%s, apply=%s); "
                           "goals evicted while it ran stay census-only"
                           % (block.get("enabled"), block.get("apply")))}
    h = (now - timedelta(days=age)).replace(microsecond=0)
    return {"state": "active", "horizon": h.isoformat(), "age_days": age,
            "detail": ("%s (aspirations_eviction.age_days=%g). A terminal "
                       "owner closed before it may be EVICTED, and this probe "
                       "cannot score one; for it FILE means only 'no LIVE "
                       "owner'" % (h.isoformat(), age))}


# ── provenance keys () ────────────────────────────────────────────
# Token scoring cannot see a replay BUNDLE as the owner of a relay it carries
# verbatim. At the 2026-09-27 17:40 replay (alpha reducer, cc-04), 6 of 8 FILE
# verdicts were relays that pending  carries as its [4] [5] [6] [9]
# [10] [16]. The cause is NOT dilution, as this block first said. Measured at
# this goal's verify (cc-09, 2026-09-28 corpus of 4088 goals): the bundle holds
# every headline token of all six, and its length-normalised weight clears
# WEIGHT_THRESHOLD (3.76 to 5.60 against 1.5, at length_norm 8.03). What
# rejects it is the coverage floor, which reads only an owner's first
# OWNER_HEAD_CHARS: the six items start at characters 6,788 to 17,424, so head
# coverage is 0.00 to 0.09 against a floor of 0.30 or 0.40, while the whole
# description covers 1.00. Two of the six, [9] and [16], have no rare headline
# token and fall at the rare gate first. That cap is the sponge guard above and
# must stay; do not widen it to reach a carried relay. The conclusion survives
# the correction: two EXACT keys run before any token is scored, the
# goal-store twin of guard-7379's provenance-before-similarity rule for
# lessons:
#
#   relay-header     the record's own (goal_id, _item_ts) against a line that
#                    CARRIES it. An LLM writes the bundles, not a script, so
#                    the grammar is the union of the forms measured on the
#                    eight live bundles (2026-09-28, cc-09):
#                      --- [4] RELAY from  (sq=sq-013, ..., 2026-09-02T08:54, box=None) ---
#                      [W1] WORK RELAY from  (2026-09-04T05:53:53):
#                      --- [W1a] from  (sq=sq-013, ..., 2026-09-05T08:00:44) ---
#                      F1. <title> [from , captured 2026-08-28T19:28:29].
#                    It found 97 carried items, 71 work and 26 lesson: every
#                    item the eight bundles enumerate, addenda included, and
#                    no line of any other goal.
#                    The line must OPEN with an item label: a header quoted
#                    mid-line is prose ABOUT a relay ('s own
#                    description quotes one), not a carried copy of it.
#   suggested-title  the relay's "Suggested title:" line against goal TITLES
#                    (rb-9493). An owner filed on its own carries no header:
#                    ..8091 each hold a relay's suggested title
#                    verbatim, and all three of those relays read FILE.
#
# Two exclusions keep a bundle from owning what it only CITES. Each errs toward
# FILE, the visible failure (a duplicate), never toward a false DECLINE, the
# permanent one (guard-5147):
#   - NOT-carried lists ("ALREADY DISPOSED IN THIS PASS, NOT carried below:",
#     "DECLINED BY THE PROBE (owner exists), NOT carried:", "(do NOT
#     re-relay)"): an item there resolves to its stated disposition, never to
#     the bundle. A marker covers its own line through the next BLANK line; on
#     every live bundle a carried list resumes only after one. No live bundle
#     yet lists a disposed item in header form, so today this guards a hazard
#     rather than removing a measured match.
#   - LESSON carries ("LESSON from", "LESSON CANDIDATE from", an L-numbered
#     label, sq=None): the bundle owns that capture for ENCODING, not for work.
# Still token-scored, so still able to read FILE on a true owner: a bundle that
# carries a relay without its capture time ( lists "[2] :
# ..."), and an owner whose title differs from the relay's suggestion.
_ITEM_LABEL_RE = re.compile(
    r"[ \t]*(?:-{3,}[ \t]*)?(\[[^\]\n]{1,120}\]|[A-Za-z]{1,2}\d{1,3}[a-z]?\.)")
# `from <goal id>`, then the capture time within 80 chars, with no OTHER goal
# id in between (so a time that belongs to a different id is never borrowed).
_CARRIED_FROM_RE = re.compile(
    r"\bfrom[ \t]+(g-\d+-\d+)((?:(?!g-\d+-\d+)[^\n]){0,80}?)"
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?)")
_NOT_CARRIED_RE = re.compile(
    r"\bNOT[ \t]+carried\b|\bNOT[ \t]+re-relay\b"
    r"|^[ \t]*(?:\(\w{1,3}\)[ \t]*|[-*][ \t]+)?(?:ALREADY[ \t]+)?"
    r"(?:DISPOSED|DECLINED[ \t]+BY[ \t]+THE[ \t]+PROBE)\b", re.I)
_LESSON_LABEL_RE = re.compile(r"\[?L\d")
_SUGGESTED_TITLE_RE = re.compile(r"suggested[ \t]+title[ \t]*:[ \t]*", re.I)
_TITLE_QUOTES = {"'": "'", '"': '"', "‘": "’", "“": "”",
                 "`": "`"}
# A suggested title shorter than this collides with a goal title by chance.
SUGGESTED_TITLE_MIN_WORDS = 4


def carried_captures(description):
    """Pure. [(label, source_goal_id, capture_ts)] for every capture a goal
    description CARRIES as work: a line opening with an item label that names
    `from <goal id>` and its capture time, outside a NOT-carried list, and not
    a LESSON carry (see the provenance-keys block). Never raises."""
    out, not_carried = [], False
    for line in str(description or "").split("\n"):
        if not line.strip():
            not_carried = False
            continue
        if _NOT_CARRIED_RE.search(line):
            not_carried = True
        item = _ITEM_LABEL_RE.match(line)
        if not_carried or not item:
            continue
        label = item.group(1)
        for m in _CARRIED_FROM_RE.finditer(line, item.end()):
            if (_LESSON_LABEL_RE.match(label)
                    or "lesson" in line[item.end():m.start()].lower()
                    or "sq=none" in m.group(2).lower()):
                continue
            out.append((label, m.group(1), m.group(3)))
    return out


def _same_capture(carried_ts, item_ts):
    """The carried capture time equals the record's, at the finer precision
    BOTH print. A header may print only the minute; a minute is the floor."""
    n = min(len(carried_ts), len(item_ts), 19)
    return n >= 16 and carried_ts[:n] == item_ts[:n]


def _norm_title(text):
    """Whitespace, case, surrounding quotes and a trailing period are the only
    differences a copied title is allowed; anything else is another title."""
    t = " ".join(str(text or "").split())
    while (len(t) > 1 and t[0] in _TITLE_QUOTES
           and t[-1] == _TITLE_QUOTES[t[0]]):
        t = t[1:-1].strip()
    return t.rstrip(".").strip().casefold()


def suggested_titles(text):
    """Pure. The normalised titles a relay proposes on "Suggested title:" lines
    (rb-9493). Quoted: the quoted span. Unquoted: the rest of the line, and its
    first sentence too, because relays often run on past the title (28 live
    captures read on 2026-09-28 used both shapes). Titles under
    SUGGESTED_TITLE_MIN_WORDS words are dropped."""
    text = str(text or "")
    out = []
    for m in _SUGGESTED_TITLE_RE.finditer(text):
        rest = text[m.end():].split("\n", 1)[0].strip()
        if not rest:
            continue
        close = _TITLE_QUOTES.get(rest[0])
        if close:
            end = re.search(re.escape(close) + r"(?=$|[\s.,;:)])", rest[1:])
            cands = [rest[1:1 + end.start()] if end else rest[1:]]
        else:
            cands = [rest]
            stop = re.search(r"\.\s", rest)
            if stop:
                cands.append(rest[:stop.start()])
        for cand in cands:
            norm = _norm_title(cand)
            if len(norm.split()) >= SUGGESTED_TITLE_MIN_WORDS and norm not in out:
                out.append(norm)
    return out


def provenance_matches(subject, goals, start, source_goal_id=None,
                       item_ts=None):
    """Pure. Owners found by an EXACT key before any token is scored
    (g-115-11192): a goal whose description CARRIES this capture as work
    (relay-header), or whose title IS the relay's suggested title
    (suggested-title). The status/window rule is the token path's own
    (_owner_window). Ranked header before title, open before terminal, done
    before not-done, then newest, so the citation is the live owner when one
    exists."""
    ts = str(item_ts or "").strip()
    gid = (source_goal_id
           if isinstance(source_goal_id, str) and len(ts) >= 16 else None)
    titles = set(suggested_titles(subject))
    out = []
    if not gid and not titles:
        return out
    for g in goals or []:
        if not isinstance(g, dict):
            continue
        by = evidence = None
        desc = str(g.get("description") or "")
        if gid and gid in desc:      # a cheap filter; the parse below decides
            for label, cgid, cts in carried_captures(desc):
                if cgid == gid and _same_capture(cts, ts):
                    by = "relay-header"
                    evidence = ("its description carries this capture as item "
                                "%s: from %s at %s" % (label, cgid, cts))
                    break
        if by is None and titles and _norm_title(g.get("title")) in titles:
            by = "suggested-title"
            evidence = "its title IS the relay's Suggested title line"
        if by is None:
            continue
        owned = _owner_window(g, start)
        if owned is None:
            continue
        status, when = owned
        out.append({
            "goal_id": g.get("id"),
            "status": status,
            "when": when.isoformat() if when else None,
            "matched_by": by,
            "evidence": evidence,
            "weak": False,
            "title": (g.get("title") or "")[:120],
        })
    out.sort(key=lambda m: (m["matched_by"] == "relay-header",
                            m["status"] in OPEN_STATUSES,
                            m["status"] not in MUST_READ_STATUSES,
                            m["when"] or ""), reverse=True)
    return out


def decide(subject, goals, now, session_start=None,
           window_hours=DEFAULT_WINDOW_HOURS, min_overlap=2,
           reference_time=None, source_goal_id=None, item_ts=None):
    """Pure decision. Returns a dict; never raises on odd goal records.

    EXACT KEYS FIRST (g-115-11192). `source_goal_id` and `item_ts` are the
    capture record's own goal_id and raw `_item_ts`. With them, a goal whose
    description CARRIES this capture as work owns it; a goal whose title IS the
    relay's "Suggested title:" line owns it too, and that key needs only the
    subject. A hit is a DECLINE and nothing is token-scored. Both default to
    None, so a caller passing neither is scored as before, apart from the title
    key (see the provenance-keys block).

    An OPEN owner is disqualifying whenever it overlaps, with no time bound —
    an open goal owns its work however old it is. A TERMINAL owner counts only
    inside the window, because "someone considered this two months ago and
    closed it" is not the same claim as "this was just done".

    `reference_time` anchors that terminal window (g-306-512); it defaults to
    `now`, so a live relay is scored exactly as before. A BACKLOG relay is
    scored weeks after capture, and its owner most likely went terminal near
    the relay's OWN time, not near replay-time (the g-306-284 measurement: 25
    backlog relays, all FILE, one already fixed upstream). batch_decide passes
    each record's `_item_ts` here. Because the floor below is a min() with
    session_start, a per-record anchor can only move the window EARLIER.

    THE ANCHOR CANNOT SCORE AN OWNER THE CORPUS NO LONGER HOLDS (g-306-522).
    Terminal non-recurring goals are evicted after aspirations_eviction.age_days
    (3), about where the fixed 72h lookback already stops. So for a backlog
    relay the anchor adds only owners the evictor has not removed yet (about
    one tick interval past the 72h mark) and goals it cannot date but this
    probe can. It turns a false FILE into a DECLINE only while the owner is
    still in the corpus, and for the backlog it was written for it is
    near-inert. (That last point is inferred from the eviction cadence and the
    census lookups, not instrumented.) An evicted owner the relay CITES is
    surfaced by evicted_citations(); an uncited one is not.

    A candidate admitted only by the multi-rare coverage floor is WEAK
    (g-115-11127): it is still cited, but when every candidate is weak the
    decision is MUST-READ, not DECLINE, and any candidate at or above
    SUBJECT_COVERAGE_MIN outranks every weak one (see SUBJECT_COVERAGE_MIN).
    """
    # Score the relay's HEADLINE, not its evidence body (): a long
    # relay's cited-identifier tail is what lets an unrelated owner win the rare
    # gate by coincidence. No-op for short subjects (see _headline).
    subj = _tokens(_headline(subject))
    # Anchor the terminal window on the relay's own capture time when given
    # (); fall back to replay-time for every pre-existing caller.
    anchor = reference_time if reference_time is not None else now
    start = window_start(anchor, session_start, window_hours)
    matches = []

    records = [g for g in (goals or []) if isinstance(g, dict)]
    # Provenance before similarity (): an exact key outranks every
    # token score, so a hit decides here.
    exact = provenance_matches(subject, records, start, source_goal_id, item_ts)
    if exact:
        top = exact[0]
        return {
            "decision": "DECLINE",
            "reason": ("owner exists: %s (%s%s): %s"
                       % (top["goal_id"], top["status"],
                          ", " + top["when"] if top["when"] else "",
                          top["evidence"])),
            "cited_goal_id": top["goal_id"],
            "cited_status": top["status"],
            "matches": exact[:10],
            "window_start": start.isoformat(),
            "scanned": len(goals or []),
        }
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
        # Admitted only by the multi-rare floor: same topic, not proven the
        # same defect (). Live-IDF only, like the floors themselves.
        weak = (not inert) and coverage < SUBJECT_COVERAGE_MIN

        owned = _owner_window(g, start)
        if owned is None:
            continue
        status, when = owned
        matches.append({
            "goal_id": g.get("id"),
            "status": status,
            "when": when.isoformat() if when else None,
            "overlap": sorted(overlap)[:8],
            "overlap_count": len(overlap),
            "weight": round(weight, 2),
            "coverage": round(coverage, 2),
            "rare_tokens": rare[:5],
            # The matched span: which shared tokens the owner's TITLE carries,
            # so a reader sees a description-only match without opening it.
            "title_overlap": sorted(overlap & cand_title)[:8],
            "weak": weak,
            "title": (g.get("title") or "")[:120],
        })

    # A candidate that clears the full coverage floor first, then the strongest
    # signal, then the most recent, so the cited id is the most defensible one
    # rather than whichever the corpus happened to list first. Ranked by
    # LENGTH-NORMALISED weight, not count — see the module header.
    matches.sort(key=lambda m: (not m["weak"], m["weight"], m["when"] or ""),
                 reverse=True)

    if matches and matches[0]["weak"]:
        top = matches[0]
        return {
            "decision": "MUST-READ",
            "reason": ("same topic, not proven the same defect: %s (%s) "
                       "restates %.2f of the relay subject, below the %.2f a "
                       "DECLINE needs, and was admitted on %d shared rare "
                       "tokens (%s)"
                       % (top["goal_id"], top["status"], top["coverage"],
                          SUBJECT_COVERAGE_MIN, len(top["rare_tokens"]),
                          ", ".join(top["rare_tokens"]))),
            "cited_goal_id": top["goal_id"],
            "cited_status": top["status"],
            "matches": matches[:10],
            "window_start": start.isoformat(),
            "scanned": len(goals or []),
        }
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
                 window_hours=DEFAULT_WINDOW_HOURS, min_overlap=2,
                 census=None):
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
        # Anchor the terminal window on THIS relay's capture time, not
        # replay-time (). wm.py stamps _item_ts on every capture
        # append. It reaches an owner that closed near the relay's OWN time
        # only while that owner is still in the corpus; eviction usually has
        # removed it (see decide(), ). Absent/unparseable -> None ->
        # decide() falls back to `now` (every pre- caller and record).
        ref = (_parse_ts(record.get("_item_ts"))
               if isinstance(record, dict) else None)
        try:
            # The record's own provenance keys the exact pass ().
            res = decide(subject, goals, now, session_start,
                         window_hours, min_overlap, reference_time=ref,
                         source_goal_id=(record.get("goal_id")
                                         if isinstance(record, dict) else None),
                         item_ts=(record.get("_item_ts")
                                  if isinstance(record, dict) else None))
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
        # decide() itself says MUST-READ when every owner is weak ().
        weak_owner = res["decision"] == "MUST-READ"
        must_read = weak_owner or (
            res["decision"] == "DECLINE"
            and (cited_status or "").lower() in MUST_READ_STATUSES)
        reason = res.get("reason")
        # FILE means "no owner the corpus could SCORE", and an evicted owner
        # was never scored at all ().
        evicted = evicted_citations(
            subject, goals, census,
            record.get("goal_id") if isinstance(record, dict) else None)
        if res["decision"] == "FILE" and evicted:
            must_read, reason = True, _evicted_reason(evicted)
        rows.append({
            "index": idx,
            "goal_id": (record.get("goal_id")
                        if isinstance(record, dict) else None),
            # A DECLINE against a terminal-but-not-done owner is NOT a
            # disposition — it is a reading assignment (outcome 3).
            "verdict": "MUST-READ" if must_read else res["decision"],
            "subject_key": key,
            "reason": reason,
            "cited_goal_id": res.get("cited_goal_id"),
            "cited_status": cited_status,
            "cited_title": top.get("title"),
            "must_read": must_read,
            "weak_owner": weak_owner,
            "evicted_citations": evicted,
            "item_ts": ref.isoformat() if ref else None,
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
            # None = NOT CHECKED, distinct from a census holding 0 ids.
            "census_aspirations": None if census is None else len(census),
            "census_evicted_ids": (None if census is None else
                                   sum(len(all_evicted_ids(a)) for a in census)),
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
    if pop.get("census_aspirations") is None:
        out.append("EVICTION CENSUS: NOT CHECKED (no --census-file) -- a relay "
                   "citing an EVICTED owner still reads FILE")
    else:
        out.append("EVICTION CENSUS: %d aspiration record(s), %d evicted id(s); "
                   "cited goal ids resolved against it"
                   % (pop["census_aspirations"], pop["census_evicted_ids"]))
    hz = result.get("terminal_horizon") or {}
    if hz:
        out.append("TERMINAL-COVERAGE HORIZON: %s" % hz["detail"])
    horizon = _parse_ts(hz.get("horizon"))
    out.append("")
    for r in result["rows"]:
        out.append("[%d] %-9s %s" % (r["index"], r["verdict"],
                                     r.get("goal_id") or ""))
        if r["cited_goal_id"]:
            out.append("      owner: %s  STATUS=%s" % (r["cited_goal_id"],
                                                       r["cited_status"]))
            out.append("      title: %s" % (r["cited_title"] or "(none)"))
            top = (r.get("matches") or [{}])[0]
            if top.get("matched_by"):
                out.append("      matched on: PROVENANCE (%s): %s"
                           % (top["matched_by"], top.get("evidence")))
            else:
                out.append("      matched on: rare %s; owner TITLE shares %s; "
                           "restates %s of the relay"
                           % (", ".join(top.get("rare_tokens") or []) or "none",
                              ", ".join(top.get("title_overlap") or [])
                              or "NOTHING (description only)",
                              top.get("coverage")))
        if r.get("evicted_citations"):
            out.append("      cites EVICTED: %s"
                       % _fmt_cites(r["evicted_citations"]))
        if r.get("weak_owner"):
            out.append("      ^^ SAME TOPIC, NOT PROVEN THE SAME DEFECT: the "
                       "owner restates the relay only below the %.2f floor a "
                       "DECLINE needs. Read its title against the relay's "
                       "defect; cite it only if it tracks THAT defect, else "
                       "re-run the relay's reproduction and file "
                       "(g-115-11127, guard-5553)." % SUBJECT_COVERAGE_MIN)
        elif r["must_read"] and r["cited_goal_id"]:
            out.append("      ^^ TERMINAL-BUT-NOT-DONE owner. Read its "
                       "outcome_note before accepting this as a decline: a "
                       "skipped or expired goal can assert the OPPOSITE of the "
                       "observation it suppresses (guard-5147).")
        elif r["must_read"]:
            out.append("      ^^ NO SCORED OWNER, NOT NO OWNER: a cited goal was "
                       "EVICTED, so this probe never scored it. Read it "
                       "(goal-resolve.py <id> --recover) and re-run the relay's "
                       "reproduction against HEAD before filing (guard-7398, "
                       "guard-5278).")
        elif r["verdict"] in ("UNREADABLE", "PROBE-ERROR"):
            out.append("      %s" % r["reason"])
        elif r["verdict"] == "FILE" and horizon:
            ts = _parse_ts(r.get("item_ts"))
            if ts and ts < horizon:
                out.append("      AGED: captured %s, before the horizon -- this "
                           "FILE means only 'no LIVE owner' (guard-7398)"
                           % r["item_ts"])
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
    ap.add_argument("--census-file", action="append", default=None,
                    help="JSON aspiration records (e.g. aspirations-read.sh "
                         "--source world --active-compact); repeatable. Goal "
                         "ids the subject cites are resolved against their "
                         "eviction census (g-306-522).")
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

    # A census the caller asked for but that cannot be read is refused, never
    # skipped: a FILE would then pass as census-checked (guard-1753).
    census = None
    for path in args.census_file or []:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            print("sq013-dedup-probe: --census-file %s unreadable (%s) — "
                  "refusing to report anything." % (path, exc), file=sys.stderr)
            return 2
        if isinstance(data, dict):
            data = data.get("aspirations") or []
        recs = ([a for a in data if isinstance(a, dict)]
                if isinstance(data, list) else [])
        if not recs:
            print("sq013-dedup-probe: --census-file %s parsed to ZERO "
                  "aspiration records — refusing (guard-2298)." % path,
                  file=sys.stderr)
            return 2
        census = (census or []) + recs

    now = _parse_ts(args.now) or datetime.now()
    session_start = _parse_ts(args.session_start)
    horizon = terminal_horizon(now, *load_eviction_config())

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
                              args.window_hours, args.min_overlap, census)
        result["terminal_horizon"] = horizon
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
    result["evicted_citations"] = evicted_citations(args.subject, goals, census)
    result["terminal_horizon"] = horizon
    if result["decision"] == "FILE" and result["evicted_citations"]:
        result["decision"] = "MUST-READ"
        result["reason"] = _evicted_reason(result["evicted_citations"])
    print(json.dumps(result, indent=2))
    if control_failed:
        return 2
    if result["decision"] == "MUST-READ":
        return 4
    return 3 if result["decision"] == "DECLINE" else 0


if __name__ == "__main__":
    sys.exit(main())
