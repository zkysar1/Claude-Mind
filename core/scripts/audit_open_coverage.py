#!/usr/bin/env python3
"""audit_open_coverage.py — the open-audit coverage rule shared by the two
Investigate-filing precheck lanes (g-115-11720):

  - defer-drift-check.py        (aspirations-precheck Phase 0.5b.10)
  - reason-less-blocked-check.py (aspirations-precheck Phase 0.5b.11)

THE DEFECT THIS CARRIES THE FIX FOR
-----------------------------------
Both lanes file ONE deduplicated audit Investigate keyed by a stable
origin_signal ("investigate:defer-drift-audit" /
"investigate:reason-less-blocked-audit"). That key names the CLASS of
violation, not its members — the members are recorded in the record's
title/description at filing time. The old dedup (0.5b.10: an LLM prose
"existing is empty" check; 0.5b.11: `_find_open_audit` returning ANY open
audit with the key) therefore skipped a fresh filing while ANY open
class-keyed audit existed — even one that named none of the goals currently
flagged. Measured consequence (g-115-11720): g-115-5132, opened 2026-08-06,
held the defer-drift key for 55 days while 9 drift reports from 3 agents
folded into it, 0 re-gates ran against it, its title still named g-350-36
(a goal that appears in none of the 2,294 metrics runs since 2026-08-25),
and drifted goals stayed flagged for up to 518 h. Same latch measured on
the reason-less lane (g-115-3068, opened ~67 days, unclaimed, no appends).

THE RULE (one function, both lanes)
-----------------------------------
An open audit (origin_signal == the lane's key, status pending/in-progress/
candidate) COVERS a flagged goal IFF the goal's id appears in the audit's NAMING
SURFACE — its title or description (the two surfaces every filer in this
family writes the ids into; both lanes name each affected id there by
construction). Coverage is PER MEMBER, so:

  - an open audit that names some of the current set covers exactly those;
    the rest are UNCOVERED and get a fresh filing;
  - an open audit that names none of the current set is stale — it no
    longer suppresses a fresh filing (guard-516: record identities, or the
    next run cannot tell new from already-cleared);
  - every current id covered -> no filing (the idempotent skip the old
    dedup existed for is preserved, not replaced).

The lanes file at most ONE fresh audit per run, naming exactly the
uncovered ids — the same single-audit spirit, minus the fold.

FAIL-CLOSED ON AN UNREADABLE SURFACE (guard-487 posture, unchanged): an
open audit whose title AND description are both empty has a naming surface
that cannot be checked — treat it as covering (suppress the filing). A
cross-box duplicate never self-heals, so skipping on uncertainty stays
correct; the realistic id-erasure (goal-note rotation of an audit
description) preserves the pinned head, where the filing block with the id
list lives, so the surface stays readable in the cases that happen.

ID MATCHING IS WORD-BOUNDARIED (not bare substring). Goal ids share
per-aspiration prefixes ("g-115-..."), so a bare `in` would let an audit
naming g-115-11720 "cover" g-115-117, and a prose id mention in one audit
would silently cover a shorter sibling id. This matcher enforces the
listing-only reading: an id is named iff it appears as a standalone token
(regex \b boundaries; ids are [a-z0-9-] so word boundaries sit exactly at
token edges). An incidental PROSE citation of an id in an audit's text
(lineage, a fix reference) therefore covers at most that EXACT id — never
a prefix sibling — and a cover is conservative by design (it suppresses a
filing; a cross-box duplicate never self-heals). The lanes still keep the
member LISTING (title + per-member lines) as the authoritative surface and
keep incidental citations to a minimum.

This module is pure (no I/O, no daemon): it takes the goal dicts the lane
already read and returns the uncovered ones, so the full rule is
unit-testable with synthetic goals — the same testable-pure-helper shape as
defer-drift-check._classify_drift.
"""

from __future__ import annotations

import re

# A goal is "open" (able to hold a lane's dedup) when it is live in the queue.
# That INCLUDES `candidate`: Investigate/Idea/Maintain filings land in the
# candidate tier until groomed, so a lane's own earlier filing sits there, and a
# set that stops at pending/in-progress cannot see it — the lane then refiles a
# duplicate audit on every run (measured 2026-10-06: 13 candidate audits for 3
# drift events, 0 pending). reason-less-blocked-check.NON_TERMINAL_OPEN is this
# constant, imported, so the two lanes cannot drift apart.
OPEN_STATUSES = ("pending", "in-progress", "candidate")


def open_audits(all_goals, origin_signal):
    """The open audits for ONE lane's key, from the SAME active read the
    lane used to find its flagged goals (fail-closed by construction,
    guard-487 — no separate query that could error into a blind
    double-file). Resolved/skipped/completed audits do NOT block a fresh
    filing (the violation may have recurred with new goals)."""
    return [
        g for g in (all_goals or [])
        if isinstance(g, dict)
        and g.get("origin_signal") == origin_signal
        and g.get("status") in OPEN_STATUSES
    ]


def naming_surface(audit_goal):
    """The text in which an audit records its members: title + description,
    lowercased and concatenated. Both filers in this family write every
    affected goal id into this surface at filing time."""
    g = audit_goal or {}
    return " ".join(
        (g.get("title") or "", g.get("description") or "")
    ).lower()


def covered_by(audit_goal, goal_id):
    """True iff this ONE open audit covers goal_id.

    An empty (unreadable) naming surface is treated as covering (the
    fail-closed posture above): uncertainty suppresses the filing rather
    than risking a duplicate that never self-heals."""
    if not goal_id:
        return False
    surface = naming_surface(audit_goal)
    if not surface.strip():
        return True  # unreadable -> conservative, status-quo behavior
    return re.search(r"\b" + re.escape(str(goal_id)) + r"\b", surface) is not None


def uncovered_ids(all_goals, origin_signal, flagged_ids):
    """The flagged goal ids NO open audit for this lane's key names.

    This is the filing predicate: a lane files a fresh audit (naming
    exactly these) iff the result is non-empty. An id that any open audit
    names — in its title or description — is covered and is NOT re-filed
    (the old audit stays the owner of its own members)."""
    flagged = [i for i in (flagged_ids or []) if i]
    audits = open_audits(all_goals, origin_signal)
    out = []
    for gid in flagged:
        if not any(covered_by(a, gid) for a in audits):
            out.append(gid)
    return out
