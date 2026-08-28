"""Stale-source refusal for narrative writes fed from a file ().

WHY THIS EXISTS
---------------
On 2026-08-24 a goal's `outcome_note` was written from a `--summary-file` whose
contents belonged to an EARLIER unit: 36,260 chars landed over 6,580. Both
existing narrative-write gates passed it, and neither was wrong to:

  * `aspirations-update-goal.sh`'s empty-VALUE refusal (545c4a875, 2026-05-13)
    sees valid non-empty prose.
  * `gates.field_shrink` (g-115-7070, b887f7dbb, 2026-08-21) refuses a
    CATASTROPHIC SHRINK. The observed write was GROWTH — 5.5x larger — so it
    sailed past the one gate whose whole job is narrative-loss detection.

That is the shape worth naming: the corruption produces a valid, LONGER,
well-formed note. Every length- and content-based predicate reads it as a
better write than the one it destroyed. The only machine-checkable tell is
PROVENANCE — the source file is older than the work it claims to describe.

CONTRIBUTING CONDITION (measured on the incident box): `agents/alpha/temp` held
1,267 files, including the generic reusable names `note.txt` and `outcome.txt`;
and guard-1701 means a gate-blocked call's heredoc NEVER RUNS while a later
`$(cat <that path>)` still returns the PREVIOUS occupant's content. The relaying
worker was gate-blocked twice in one session and watched a heredoc die with its
block. So the stale file is not a typo — it is what the previous write left
behind at a path the next write reuses.

WHAT IT DOES NOT GATE ON, deliberately
--------------------------------------
NOT "the note cites a different goal id". Legitimate notes cite sibling goals
constantly — the cross-record citation protocol in aspirations-precheck Phase
0.5g.7 REQUIRES it. That predicate would fire on the fleet's best-written notes,
which is the guard-2860 failure (never relax an ownership test into a pattern).

THE REFERENCE TIME, and why `claimed_at` and not session start
--------------------------------------------------------------
A session spans many goals. A summary file written earlier in the SAME session,
for a DIFFERENT goal, is exactly the incident — and a session-start reference
admits it. `claimed_at` is per-unit and is the moment the work being described
began, so the healthy ordering is total: claim -> work -> write file -> close.
Session start is the FALLBACK for the rare goal with no claim (agent-queue work
is not claimed), where it still catches a file older than the whole session.

Verified live before choosing this chokepoint: `--summary-file` is resolved at
ARGUMENT-PARSE time in both consumers (`iteration-close.sh` ~line 703,
`closure-evidence-write.sh` ~line 190), which is strictly before any terminal
transition — and `aspirations.py:2698` pops `claimed_at` only on a terminal
status write. So the reference is live at both call sites.

NO DAEMON HALF, and do not add one (guard-547/2323 does not apply here).
`gates.field_shrink` is deliberately byte-parallel across the CLI and the daemon
writer because both receive the VALUE. This gate judges a FILE PATH, which
exists only in the shell layer — the daemon never sees one. A mirrored daemon
half would have nothing to inspect.

THRESHOLD, AND HOW IT WAS CHOSEN (guard-1562 — never widen blindly)
-------------------------------------------------------------------
Measured against the live corpus 2026-08-28 (echo, cc-03), asking "who NEWLY
gets refused": all 14 files then resident in `agents/*/temp` predated the
open claim (2.3h to 498.9h older) and ZERO postdated it. Every one of them
would be refused, and every one is correctly refused — none is that unit's
narrative, and `pr-body-g-369-05.md` (21.6h) is precisely the other-goal
artifact whose reuse is the incident. A file written during the unit it
describes cannot be older than the claim that opened the unit, so the healthy
path is refused zero times BY CONSTRUCTION rather than by a tuned margin.

`GRACE_SECONDS` exists only for clock skew between the writer of the goal
record and the filesystem, not as a tolerance dial. It is small on purpose: a
legitimately-authored narrative postdates its claim by minutes at least, so
widening it buys nothing and admits the near-miss reuse case.

RETIREMENT CRITERION, recorded at birth (guard-769)
---------------------------------------------------
Retire when EITHER holds:
  (a) `--summary-file` consumers stop accepting caller-chosen paths — e.g. the
      narrative is written to a per-claim path the framework derives, so a
      previous unit's file cannot be named at all; or
  (b) Telemetry shows override/(block+override) > 0.5 over 20+ firings, meaning
      the reference time is refusing legitimate work more often than it catches
      reuse.
A block-free firing log is this gate WORKING — like its field_shrink sibling it
guards a rare, expensive, self-concealing event.

DESIGN NOTES
------------
No try/except anywhere in this module, deliberately (guard-3803): a fail-open
handler also covers the construction of the refusal message, silently turning a
refusal into an approval. `evaluate` is pure comparison behind explicit
isinstance checks — no I/O, no dependency to fail. All filesystem and goal-record
reads live in the CLI half below, where a failure is REPORTED rather than
swallowed. Callers must NOT wrap `evaluate` in a bare `except: pass`.

Public API:
    evaluate(source_path, source_mtime, reference_time, reference_kind) -> dict

Return shape (every branch sets `decision_path` — guard-502):
    {
      "blocked": bool,
      "message": str | None,      # pre-formatted refusal text; None when allowed
      "source_path": str,
      "age_seconds": float | None,  # reference_time - source_mtime; >0 means stale
      "reference_kind": str,        # claimed_at | session_start | none
      "decision_path": str,         # unique per branch, for gate telemetry
    }
"""
from __future__ import annotations

# Clock-skew allowance ONLY. See THRESHOLD above: this is not a tolerance dial.
GRACE_SECONDS = 120.0


def evaluate(source_path, source_mtime, reference_time, reference_kind) -> dict:
    """Decide whether a narrative source file is too old to describe this work.

    All arguments are plain data so this stays pure and directly testable:
      source_path     str  — the path as the caller wrote it (for the message)
      source_mtime    float | None — epoch seconds; None when unstattable
      reference_time  float | None — epoch seconds; None when unresolvable
      reference_kind  str  — 'claimed_at' | 'session_start' | 'none'
    """
    base = {
        "blocked": False,
        "message": None,
        "source_path": str(source_path),
        "age_seconds": None,
        "reference_kind": str(reference_kind),
    }

    if not isinstance(source_mtime, (int, float)):
        # The caller could not stat the file. Not this gate's refusal to make:
        # the consumers already refuse an unreadable/missing --summary-file with
        # their own clearer message, and duplicating it here would race them.
        return {**base, "decision_path": "no-source-mtime"}

    if not isinstance(reference_time, (int, float)):
        # No claim and no session start. "Cannot judge" is not "suspicious" —
        # blocking here would refuse every write on a box whose session state is
        # unreadable, which is a plumbing fault, not a provenance one.
        return {**base, "decision_path": "no-reference-time"}

    age = float(reference_time) - float(source_mtime)
    base["age_seconds"] = age

    if age <= GRACE_SECONDS:
        return {**base, "decision_path": "source-newer-than-reference"}

    hours = age / 3600.0
    ref_label = {
        "claimed_at": "this goal was claimed",
        "session_start": "this session started",
    }.get(str(reference_kind), "the reference time")
    return {
        **base,
        "blocked": True,
        "decision_path": f"stale-source:{reference_kind}",
        "message": (
            f"narrative source '{source_path}' was last modified {hours:.1f}h "
            f"BEFORE {ref_label}, so its contents cannot describe this unit of "
            f"work. This is the stale-source clobber (g-115-7425): the write "
            f"would succeed with valid, possibly LONGER prose belonging to "
            f"another goal, and both existing narrative gates pass it. "
            f"Re-write the narrative for THIS goal to a fresh path, or pass "
            f"--override-stale-source \"<why this file is genuinely current>\"."
        ),
    }
