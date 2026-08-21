#!/usr/bin/env python3
"""Measure the SELF-BLOCKED share of the deferred-goal queue — read-only.

WHAT A SELF-BLOCKED DEFER IS: a `precondition_unmet:` whose named precondition
is the goal's OWN unfinished deliverable. `precondition_unmet:` re-probes BY
DESIGN, so the framework re-asks "is it true yet?" on a cadence — and for this
class the answer is NO forever, because nothing outside the goal is making it
true. The goal waits on itself, at a 120h fail-open TTL, indefinitely.

WHY reclaim-routed-work.md's TWO AXES BOTH MISS IT. The PREMISE axis re-probes
and finds the condition still true (it is). The RULE axis asks whether a grant
or convention retired the excuse (none did). Both correctly return "still
blocked" and the goal freezes anyway. This is a THIRD axis — is the precondition
EXOGENOUS to the goal, or IS it the goal? (Origin: g-363-19 -> g-363-59.)

═══ WHY THE PREDICATE IS A NEGATIVE FILTER, MEASURED BOTH WAYS ═══

The obvious predicate — diff the defer's named precondition against the goal's
own remaining-work text — was measured and REJECTED before this script existed:

  * TITLE-vs-DEFER token overlap admits almost everything. 87 of 153
    `precondition_unmet:` rows (56.9%) share >=2 distinctive tokens, and reading
    the top 14 verbatim, ZERO were self-blocked — they were locus blocks,
    dependency waits and time windows. A well-written defer NATURALLY restates
    its own goal's subject; that is what makes it readable. Same perverse
    gradient as guard-3882 and as locus-sweep.py's provenance false-positives:
    the better the defer is documented, the more it looks like the target.
  * The "remaining-work list" is PROSE inside progress_note, and the SSOT
    population does not even carry that field. A prose-vs-prose diff driving a
    write REFUSAL would refuse legitimate defers on a fuzzy match.

So the predicate inverts: ask WHO CLEARS THIS. A defer is exogenous when it
names a party other than "whoever picks up this goal" — a box, another goal, a
date, an accumulation count, live traffic, a human, a role. A defer that names
NO external clearer is the candidate. Over-excluding is deliberate: it drives
the count toward a FLOOR, and a floor is what this question needs.

READ-ONLY BY CONSTRUCTION: opens nothing for writing, imports no mutation
helper, has no --apply. Same posture as locus-sweep.py and the scar-tissue
cadence — a sweep that proposes, never acts.

IT REPORTS CANDIDATES; IT DOES NOT RENDER A VERDICT (guard-4432). A band is a
statement about what the text NAMES, never a conclusion that a goal is blocked
on itself, and this script deliberately attaches no action instruction to any
row. Adjudication is a reader's job.

═══ THE BAND COUNTS ARE EXCLUSION COUNTS, NOT A TAXONOMY ═══

Read this before quoting a band size as a finding. `classify` walks CLEARERS in
order and returns the FIRST family that matches, so a row naming two clearers is
attributed to whichever family is listed earlier — the cascade is tuned to
EXCLUDE reliably, not to attribute correctly. Every non-candidate band is
therefore a lower bound on itself and an upper bound on nothing.

MEASURED (g-363-60, 2026-08-20, cc-07). Role-classifying all 33 `other-goal`
rows by opening them: only 5 were genuine goal-waits (4 could adopt `blocked_by`,
1 already had), 1 named a goal absent from every store, 5 cited their OWN id,
and 11 were locus / date / accumulation / role blocks that merely CITED a goal
id in passing. So `other-goal: 33` overstates "waiting on another goal" by ~6x.
This is guard-3154 exactly — an id occurring in a field has a ROLE, and the
citation share does not average out.

The worked example is one word wide: four live-PLAY-session blocks read "no
Linux fleet box can host one", and the locus pattern was `\blinux box\b`, which
"Linux fleet box" does not match — so four locus rows fell through to
`other-goal`. The pattern now tolerates one intervening qualifier. Fixing it
moved rows BETWEEN exclusion families and left the candidate floor unchanged,
which is the whole point: the floor is what this instrument is for, and the band
attribution around it is soft.

Do NOT respond to this by chasing regex precision. A first-match cascade over
free prose will always mis-attribute some rows, the failure is invisible in the
output (a wrongly-excluded row never appears), and the candidate count — the one
number this script exists to produce — is invariant under every such fix. The
correct response is to open the rows before quoting a band, which is what the
paragraph above did.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def _load_population():
    """Reuse audit-deferred-defers.load_deferred() — do NOT re-derive it.

    That function owns the population definition (world + every agent
    aspirations.jsonl, non-null defer_reason, TERMINAL_STATUSES excluded) and
    routes through _paths. A second definition here would drift from it
    silently, and the two counts would disagree with nothing to say which was
    right. Hyphenated filename, hence importlib. Same import locus-sweep.py
    uses, for the same reason.
    """
    path = SCRIPT_DIR / "audit-deferred-defers.py"
    spec = importlib.util.spec_from_file_location("_audit_deferred_defers", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.load_deferred()


# ── the external-clearer families ───────────────────────────────────────────
#
# EVERY TOKEN IS \b-ANCHORED (guard-3845). An unanchored fragment matches inside
# unrelated words and the dominant false-positive is invisible in the output,
# because a wrongly-EXCLUDED row simply never appears.
#
# ROLE is listed separately rather than folded into the exogenous families: a
# "reducer-only" block is exogenous to a WORKER and not to the fleet, so it
# clears when a differently-roled Body picks the goal up. Collapsing it into
# either neighbouring band would erase that distinction (guard-2418's shape —
# two paths that take the same action today must still stay separable).
CLEARERS = (
    ("locus", r"\bcc-\d+\b|\bLAPTOP-[A-Z0-9-]+|\bDESKTOP-[A-Z0-9-]+"
              r"|\b\w+_required\b|\bhost-bound\b|\bbox-bounded\b|\bthis box\b"
              r"|\bowning box\b|\bthe \w+ host\b"
              # one optional intervening qualifier: "linux box" AND "Linux
              # fleet box". The bare two-word form missed four real locus rows
              # (/199/201/204, "no Linux fleet box can host one"),
              # which then fell through to `other-goal` — see the band-counts
              # note in the module docstring.
              r"|\b(?:windows|linux)\s+(?:\w+\s+)?box\b"),
    ("other-goal", r"\bg-\d{3}-\d{2,4}\b|\bblocked_on_dependency\b|\bdepends on\b"
                   r"|\bPR\b|\b#\d+\b|\bmerged\b|\breview\b"),
    ("date-or-window", r"\b20\d\d-\d\d-\d\d\b|\bdate gate\b|\bdays?\b|\bweeks?\b"
                       r"|\bhours?\b|\bresolves_by\b|\bearliest_wake\w*\b|\bwindow\b"
                       r"|\bquiesced?\b|\bcadence\b"),
    ("accumulation", r"\b\d+\s*(?:of|/)\s*(?:the\s+)?(?:required\s+)?\d+\b"
                     r"|\baccumulated?\b|\bsamples?\b|\bqualifying\b|\bdenominator\b"
                     r"|\bnot occurred\b|\bhas not\b|\bhave not\b|\bthreshold\b"),
    ("live-signal", r"\blive\b|\btraffic\b|\bdeploy\w*\b|\bcold ?start\w*\b"
                    r"|\bsession\b|\bproduction\b|\bcustomer\b|\bE2E\b"),
    ("human", r"\bapprov\w*\b|\bowner\b|\buser\b|\bcounsel\b|\bcredentials?\b"
              r"|\bpermissions?\b|\backnowledg\w*\b|\bomni\b"),
    ("role", r"\breducer[- ]only\b|\bworker cannot\b|\bneeds reducer\b"
             r"|\breducer to run\b|\bPhase 3\.6[0-9]?\b"),
)
COMPILED = tuple((name, re.compile(pat, re.I)) for name, pat in CLEARERS)

_PREFIX = "precondition_unmet"


def classify(defer_reason: str) -> str:
    """Which band does this defer_reason fall in?

    Returns one of:
      out_of_scope            not a `precondition_unmet:` defer at all
      exogenous:<family>      names an external clearer (see CLEARERS)
      role                    clears when a differently-roled Body picks it up
      self_blocked_candidate  names NO external clearer

    `self_blocked_candidate` is a statement about what the TEXT NAMES. It is not
    a finding that the goal is blocked on itself — that requires reading the
    goal (guard-4432).
    """
    text = str(defer_reason or "")
    if not text.strip().lower().startswith(_PREFIX):
        return "out_of_scope"
    for name, rx in COMPILED:
        if rx.search(text):
            return "role" if name == "role" else f"exogenous:{name}"
    return "self_blocked_candidate"


# ── controls (guard-3845: POSITIVE **and** NEGATIVE, in the same call) ───────
#
# guard-4512: a control that certifies the INPUT cannot certify the QUESTION. A
# population-size assertion would prove only that the loader ran. These fixtures
# exercise `classify` itself, and every string is a VERBATIM excerpt from the
# live corpus rather than an invented one, so the control tests shapes
# production actually contains (guard-920).
#
# The NEGATIVE half is the load-bearing one here: this predicate's failure mode
# is UNDER-exclusion (an exogenous row surfacing as a candidate), and a
# positive-only control cannot see it.
CONTROL = (
    # POSITIVE — must reach self_blocked_candidate
    ("precondition_unmet: candidate fix (b) implementation needed "
     "(candidate (c) refuted)",
     "self_blocked_candidate", "the precondition is the goal's own fix"),
    ("precondition_unmet: two verification outcomes remain unmet -- (1) count "
     "tree nodes carrying ready_to_decompose; (2) decide whether "
     "decompose_threshold in tree.yaml retains a consumer",
     "self_blocked_candidate", "names the goal's OWN verification outcomes"),
    ("precondition_unmet: code change implementing option (b) execute=False "
     "threading and associated tests not yet written",
     "self_blocked_candidate", "unwritten code is not an external party"),
    # NEGATIVE — must NOT reach self_blocked_candidate
    ("precondition_unmet:studio_session_required — RE-PROBED 2026-08-15T20:00 "
     "(foxtrot, LAPTOP-3IOFCNEO, the Studio host).",
     "exogenous:locus", "a declared locus clears on another box"),
    ("precondition_unmet: g-350-215 (HIGH, intended_agent foxtrot, status "
     "pending) — provision the DEV-place host infra",
     "exogenous:other-goal", "another goal is the clearer"),
    ("precondition_unmet: 0 of the required 8 qualifying closes have "
     "accumulated, measured 2026-08-10 on cc-08",
     "exogenous:locus", "locus matches first; still correctly exogenous"),
    ("precondition_unmet: hypothesis resolution is reducer-only (worker Phase "
     "3.65); evidence captured in outcome_note",
     "role", "clears when a reducer picks it up, not when the world changes"),
    ("human_blocked: owner must authorize the production deploy",
     "out_of_scope", "a different prefix — never auto-clears by design"),
)


def run_control():
    """Return (ok, failures). Never mutates anything."""
    bad = []
    for text, want, why in CONTROL:
        got = classify(text)
        if got != want:
            bad.append({"expected": want, "got": got, "why": why,
                        "text": text[:110]})
    return (not bad), bad


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Read-only: measure the self-blocked share of the deferred "
                    "queue. Reports candidates for adjudication; renders no "
                    "verdict and prescribes no action.")
    ap.add_argument("--json", action="store_true",
                    help="emit the full result as JSON instead of a report")
    ap.add_argument("--limit", type=int, default=0,
                    help="print at most N candidate rows (0 = all)")
    args = ap.parse_args(argv)

    ok, bad = run_control()
    if not ok:
        print("CONTROL REGRESSED — the classifier is broken, so an empty band "
              "would mean nothing. Refusing to report.", file=sys.stderr)
        for b in bad:
            print(f"  expected={b['expected']} got={b['got']} :: {b['why']}\n"
                  f"    {b['text']}", file=sys.stderr)
        return 2

    pop = _load_population()
    if not pop:
        print("REFUSING: the SSOT loader returned an EMPTY population. That is "
              "an unreadable queue, NOT a queue with no self-blocked defers.",
              file=sys.stderr)
        return 2

    bands: dict[str, list] = {}
    for g in pop:
        bands.setdefault(classify(g.get("defer_reason")), []).append(g)

    cands = bands.get("self_blocked_candidate", [])
    in_scope = [g for g in pop
                if str(g.get("defer_reason") or "").strip().lower()
                .startswith(_PREFIX)]

    if args.json:
        print(json.dumps({
            "population": len(pop),
            "in_scope_precondition_unmet": len(in_scope),
            "band_counts": {k: len(v) for k, v in sorted(bands.items())},
            "self_blocked_candidates": [
                {"goal_id": g.get("goal_id"), "src": g.get("src"),
                 "asp_id": g.get("asp_id"), "status": g.get("status"),
                 "defer_set_at": g.get("defer_set_at"),
                 "title": g.get("title"), "defer_reason": g.get("defer_reason")}
                for g in cands],
        }, indent=2))
        return 0

    print(f"deferred population (SSOT loader) : {len(pop)}")
    print(f"in scope (precondition_unmet:)    : {len(in_scope)}")
    print()
    for band, rows in sorted(bands.items(), key=lambda x: -len(x[1])):
        print(f"  {len(rows):4d}  {band}")
    print("  ^ EXCLUSION counts, NOT a taxonomy: first-match-wins over an "
          "ordered cascade, so a row\n    naming two clearers lands in "
          "whichever family is listed first. Measured, the `other-goal`\n"
          "    band overstated real goal-waits ~6x. Open the rows before "
          "quoting a band size.")
    pct = (100.0 * len(cands) / len(in_scope)) if in_scope else 0.0
    print(f"\nSELF-BLOCKED CANDIDATES: {len(cands)} of {len(in_scope)} "
          f"in-scope ({pct:.1f}%)")
    print("Each row NAMES no external clearer. Whether the precondition is the "
          "goal's own deliverable is a reader's call — open the goal.\n")

    rows = cands[: args.limit] if args.limit else cands
    for g in rows:
        print(f"--- {g.get('goal_id')}  [{g.get('src')}]  "
              f"status={g.get('status')}  deferred={g.get('defer_set_at')}")
        print(f"    TITLE : {str(g.get('title'))[:150]}")
        print(f"    DEFER : {str(g.get('defer_reason'))[:280]}")
    if args.limit and len(cands) > args.limit:
        print(f"\n({len(cands) - args.limit} further candidate(s) not printed "
              f"— --limit {args.limit} was in effect. NOT a clean tail.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
