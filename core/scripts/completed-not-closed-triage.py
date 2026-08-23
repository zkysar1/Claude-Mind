#!/usr/bin/env python3
"""completed-not-closed-triage.py — REPORT-ONLY reducer lane ().

Surfaces the goals that are FINISHED but still held at in-progress by a worker
Body whose liveness carrier is dead. A worker holds a finished goal on purpose
so the reducer can verify and close it; when that worker dies, the goal is
stuck — nobody closes it and nobody may re-execute it. Measured 2026-08-14:
260 such goals, 63 of them product, claim ages 2.8-6.7 days.

WHY THIS SCRIPT REPORTS AND NEVER ACTS
--------------------------------------
The obvious build here is a classifier: read each goal's `outcome_note`, decide
done/not-done, close the done ones. That was MEASURED on this exact corpus
before this file was written, and it is not safe:

  * Negative direction (flag as not-done): 33 flagged, 19 of them FALSE — a 58%
    false-positive rate. The tokens match TOPIC words, not verdict words. A note
    reading "DONE. ... 47 passed / 11 xfailed / 0 failed" flags on "failed"; a
    goal ABOUT stranded claims flags on "stranded".
  * Positive direction (the unrecoverable one): of 423 notes whose head carries
    a positive verdict word, 22 ALSO say in the same head that they are NOT
    finished. `g-115-6138` reads "DIAGNOSIS COMPLETE, FIX NOT DONE"; `g-326-191`
    reads "PARTIAL, DELIBERATELY LEFT pending". A positive-first classifier
    closes all 22, burying open work under a false verdict.

So the note's own first line is REPORTED VERBATIM and no verdict is computed.
That is guard-2852c's instruction ("LENGTH IS NOT VERDICT — read the note's own
first line") applied to the tooling instead of only to the reader.

The precedent is `scar-tissue-check.py`, per `.claude/rules/learning-philosophy.md`:
"The slate is a proposal, never an action: the cadence has no `--apply` path and
imports no mutation helper." Same posture here, for the same reason — a wrong
predicate applied across 260 goals is unrecoverable at that scale.

WHY IT SUBPROCESSES THE SWEEP INSTEAD OF RECOMPUTING
----------------------------------------------------
`stranded-claim-sweep.py` already computes this population: it reads the claim
rows, resolves each body carrier from the authoritative store, applies the
foreign-SID grace, and stamps `verdict="completed-not-closed"` with the
evidence attached. Recomputing any of that here would be a second implementation
that drifts silently when the sweep evolves (the no-transcription contract,
guard-2676 / g-306-212). This script is a PROJECTION of the sweep's own output:
filter, rank, bound, print.

The sweep is DRY-RUN BY DEFAULT (`--apply` is opt-in) and prints its summary as
clean JSON on stdout with all narration on stderr, so consuming it costs nothing
and mutates nothing. `_assert_read_only()` below re-checks that invariant on the
argv this script builds, so a future edit cannot quietly arm it.

NOTE ON READING THE OUTPUT (guard-3628)
---------------------------------------
These goals are KEPT by the sweep DELIBERATELY, not missed by it. "The sweep
failed to catch it" and "the sweep decided not to act on it" are different
findings; this is the second. Releasing the claim instead of closing the goal is
the known wrong move: it converts "held for the reducer" into "available to
anyone", whereupon the scorer ranks the goal FIRST because its metadata is fresh,
and the finished work is re-executed (g-115-5177).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

_SWEEP = "stranded-claim-sweep.py"

#: Aspiration prefixes whose goals are product work rather than framework work.
#: Used only to LABEL and to power --product-only; never to filter by default.
_PRODUCT_ASP_PREFIXES = ("asp-335", "asp-326", "asp-350", "asp-250", "asp-318")

#: Default rows printed. A bounded report that says what it dropped beats an
#: unbounded one nobody reads (guard-1760: a tool must report what it declined
#: to show, or partial coverage reads as total).
_DEFAULT_LIMIT = 20


def _walk_records(obj: Any) -> Iterator[Dict[str, Any]]:
    """Yield every dict carrying both `goal_id` and `verdict`, at any depth.

    Depth-agnostic on purpose: the sweep groups records under several summary
    keys (`kept`, `stranded`, `possible_displacement`, ...) and has added keys
    over time. Keying on the record SHAPE rather than on a list of container
    names means a new bucket joins this report automatically instead of being
    silently skipped -- the cross-agent-glob failure mode from CLAUDE.md, one
    level down.
    """
    if isinstance(obj, dict):
        if "goal_id" in obj and "verdict" in obj:
            yield obj
        for value in obj.values():
            yield from _walk_records(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk_records(value)


def _assert_read_only(argv: List[str]) -> None:
    """Refuse to run the sweep with any mutating flag.

    This script's entire safety claim is that it cannot change state. The one
    way it could is by handing `--apply` to the subprocess, so that is asserted
    at the call rather than left to review.
    """
    banned = {"--apply"}
    found = banned.intersection(argv)
    if found:
        raise SystemExit(
            f"REFUSING: completed-not-closed-triage is report-only, but the "
            f"sweep argv carries {sorted(found)}. This script must never "
            f"mutate claim state."
        )


def _load_sweep(from_sweep: Optional[str], timeout: int) -> Dict[str, Any]:
    """Return the sweep summary dict, from a saved file or a fresh dry run."""
    if from_sweep:
        raw = open(from_sweep, encoding="utf-8").read()
        # A saved log may have been captured with 2>&1, so stderr narration can
        # precede the JSON. Reading from the first brace tolerates that; a fresh
        # subprocess run below never needs it (stdout is pure JSON).
        start = raw.find("{")
        if start < 0:
            raise SystemExit(
                f"REFUSING: no JSON object found in {from_sweep}. That is an "
                f"unreadable input, NOT an empty population."
            )
        return json.loads(raw[start:])

    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), _SWEEP)
    argv = [sys.executable, script]
    _assert_read_only(argv)
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    if not proc.stdout.strip():
        raise SystemExit(
            f"REFUSING: {_SWEEP} produced no stdout (rc={proc.returncode}). "
            f"An unreadable sweep is NOT a clean queue.\n"
            f"stderr tail: {proc.stderr[-600:]}"
        )
    return json.loads(proc.stdout)


def _candidates(summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Records that are finished-but-unclosed AND whose holder is gone.

    The predicate is EXACTLY the goal's, deliberately not widened. A goal whose
    carrier is merely absent (`body_carrier` unset -- 23 of 283 measured) is NOT
    included: absent is not the same as dead, and this report exists to name a
    population a reducer will act on.
    """
    out = []
    for rec in _walk_records(summary):
        if rec.get("verdict") != "completed-not-closed":
            continue
        carrier = rec.get("body_carrier") or {}
        if carrier.get("verdict") != "stale":
            continue
        if not rec.get("foreign_sid_grace_expired"):
            continue
        out.append(rec)
    # Oldest claim first: the longest-stuck goal is the one whose re-execution
    # risk has had the most time to accumulate.
    out.sort(key=lambda r: r.get("age_minutes") or 0, reverse=True)
    return out


def _is_product(rec: Dict[str, Any]) -> bool:
    return str(rec.get("asp_id") or "").startswith(_PRODUCT_ASP_PREFIXES)


# ───────────────────────── fleet-wide denominator () ─────────────────────────
#
# WHY THIS IS NOT A SECOND IMPLEMENTATION OF THE SWEEP (guard-2676).
# `stranded-claim-sweep.py` is BOUND-AGENT-SCOPED by construction: its query is
# `claimed_by == MIND_AGENT AND status == in-progress`, and it exits fatally
# without an agent binding. So every number it produces — including the `total`
# this report prints — is ONE agent's claims. That is correct for a sweep whose
# job is releasing the calling agent's own stranded claims, and it is structurally
# unable to answer "how big is the backlog".
#
# Measured 2026-08-15, four boxes agreeing: alpha holds 336 of the 338
# claimed-and-noted goals, so the sweep reads ~0 on every OTHER agent while the
# backlog is untouched. The pre-existing remedy in _render's zero path is
# `--from-sweep <their-sweep.json>` — an out-of-band, cross-box artifact exchange
# that requires the agent holding the backlog to produce a file and hand it over.
# Nothing does that, which is why the lane reports and nothing converts (the
# "sweep with no consumer" shape, reclaim-routed-work.md rule 6).
#
# This function computes the DENOMINATOR the sweep cannot: it reads the world
# store through the canonical daemon-routed reader and counts non-terminal goals
# carrying an outcome_note, regardless of which agent holds the claim. It does
# NOT re-derive carrier liveness, foreign-SID grace, or the keep verdict — those
# stay the sweep's, unduplicated.
#
# IT IS A SUPERSET, NOT A VERDICT. An outcome_note means work happened under the
# goal; it does not mean the goal is finished (22 of 423 positive-verdict note
# heads say in the same breath that they are not — see the module docstring).
# The count is reported as "carry completion evidence", never as "N finished
# goals", for exactly that reason.

_FLEET_READER = "aspirations-read.sh"
_TERMINAL_STATUSES = ("completed", "skipped", "expired", "superseded", "decomposed")


def _note_min_chars() -> Optional[int]:
    """Read the sweep's note-evidence threshold from its source, never a copy.

    Regex-reads the constant out of `stranded-claim-sweep.py` rather than
    re-typing the number here, so a future retune of the sweep cannot leave this
    report quoting a stale threshold. Returns None when the constant cannot be
    found — the caller then omits the thresholded split rather than inventing a
    number (a hard-coded fallback would be the drift this read exists to avoid).
    """
    import re
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), _SWEEP)
    try:
        with open(path, encoding="utf-8") as fh:
            m = re.search(r"^_NOTE_EVIDENCE_MIN_CHARS\s*=\s*(\d+)", fh.read(), re.M)
        return int(m.group(1)) if m else None
    except OSError:
        return None


def _fleet_population(timeout: int) -> Dict[str, Any]:
    """Count non-terminal goals carrying completion evidence, fleet-wide.

    Returns a dict with `readable: False` and a `reason` when the store cannot be
    read. An unreadable store is NOT an empty population (verify-before-assuming
    rule 4 / guard-3878): the caller must print the failure, never a zero.
    """
    from _runtime_bash import bash_cmd
    from _paths import CORE_ROOT

    out: Dict[str, Any] = {"readable": False, "reason": None}
    try:
        proc = subprocess.run(
            bash_cmd(Path(CORE_ROOT, "scripts", _FLEET_READER).as_posix(),
                     "--source", "world", "--active"),
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except Exception as exc:                                # noqa: BLE001
        out["reason"] = f"{_FLEET_READER} raised {type(exc).__name__}: {exc}"
        return out
    if proc.returncode != 0 or not proc.stdout.strip():
        out["reason"] = (f"{_FLEET_READER} rc={proc.returncode}, "
                         f"{len(proc.stdout)} bytes stdout; stderr tail: "
                         f"{(proc.stderr or '').strip()[-300:]}")
        return out
    try:
        start = min(i for i in (proc.stdout.find("{"), proc.stdout.find("[")) if i >= 0)
        parsed = json.JSONDecoder().raw_decode(proc.stdout[start:])[0]
    except Exception as exc:                                # noqa: BLE001
        out["reason"] = f"unparseable reader output: {type(exc).__name__}: {exc}"
        return out

    asps = parsed if isinstance(parsed, list) else (parsed.get("aspirations") or [parsed])
    if not asps:
        out["reason"] = "reader returned zero aspirations — an empty read, not an empty world"
        return out

    min_chars = _note_min_chars()
    noted = 0
    claimed_noted = 0
    over_threshold = 0
    product = 0
    by_holder: Dict[str, int] = {}
    scanned = 0
    #  run 3: the headline `noted` is a DENOMINATOR, and reporting it
    # without this split overstates the drainable backlog ~7x. Two sub-populations
    # inside it are not undrained work at all:
    #   recurring — a recurring goal legitimately carries its LAST fire's note
    #     while pending its NEXT. Closing one banks nothing; it kills the cadence.
    #     (This goal is itself recurring and appeared in its own actionable list.)
    #   deferred  — a non-recurring goal carrying a defer_reason is parked against
    #     a NAMED gate, i.e. already routed. It is not waiting to be noticed.
    # guard-2529: a sweep that filters before counting must report what the filter
    # EXCLUDED. guard-2273: print the unfiltered population beside the filtered
    # count. So these are reported ALONGSIDE `noted`, never subtracted from it.
    recurring = 0
    deferred = 0
    # guard-4434: a NULL claimed_by beside a NON-NULL claimed_by_sid IS a claim.
    # `by_holder`/`claimed_and_noted` key on the NAME only, so such a record reads
    # as unclaimed here. Counted separately rather than folded in — changing the
    # existing ownership tallies is a behaviour change this reporting fix does not
    # make; naming the gap is what lets a reader see it. Measured 2026-08-21: 2 of
    # 225, the same pair guard-4434 named on 2026-08-19 (, ).
    sid_only_claimed = 0
    for asp in asps:
        asp_id = str(asp.get("id") or "")
        for goal in (asp.get("goals") or []):
            if goal.get("status") in _TERMINAL_STATUSES:
                continue
            scanned += 1
            note = (goal.get("outcome_note") or "").strip()
            if not note:
                continue
            noted += 1
            if asp_id.startswith(_PRODUCT_ASP_PREFIXES):
                product += 1
            if min_chars is not None and len(note) >= min_chars:
                over_threshold += 1
            # Recurring wins over deferred: a recurring goal is legitimate
            # re-pending whether or not it also carries a defer.
            if goal.get("recurring"):
                recurring += 1
            elif goal.get("defer_reason"):
                deferred += 1
            holder = goal.get("claimed_by")
            if holder:
                claimed_noted += 1
                by_holder[holder] = by_holder.get(holder, 0) + 1
            elif goal.get("claimed_by_sid"):
                sid_only_claimed += 1

    out.update({
        "readable": True,
        "aspirations": len(asps),
        "non_terminal_scanned": scanned,
        "noted": noted,
        "claimed_and_noted": claimed_noted,
        "unclaimed_and_noted": noted - claimed_noted,
        "product": product,
        "min_chars": min_chars,
        "over_threshold": over_threshold if min_chars is not None else None,
        "by_holder": dict(sorted(by_holder.items(), key=lambda kv: -kv[1])),
        "recurring": recurring,
        "deferred": deferred,
        "undrained": noted - recurring - deferred,
        "sid_only_claimed": sid_only_claimed,
    })
    return out


def _render_fleet(pop: Dict[str, Any], this_agent: str) -> None:
    """Print the fleet block. Always called, on every path, before the report.

    guard-3830: a batch bound must never be reported as a scan result. The
    ACTIONABLE list below is bounded twice over — to one agent's claims and to
    dead carriers — so without this block a structural zero reads as an
    all-clear on four boxes out of five.
    """
    print("=" * 78)
    if not pop.get("readable"):
        state = "NOT COMPUTED" if pop.get("waived") else "UNREADABLE"
        print(f"FLEET POPULATION: {state} — this is NOT zero and NOT an all-clear.")
        print(f"  reason: {pop.get('reason')}")
        print("=" * 78)
        print()
        return

    mine = pop["by_holder"].get(this_agent, 0)
    print(f"FLEET POPULATION: {pop['noted']} non-terminal goal(s) carry completion "
          f"evidence ({pop['product']} product)")
    print("=" * 78)
    print(f"  claimed by an agent : {pop['claimed_and_noted']}  "
          f"(held by {this_agent}: {mine})")
    print(f"  no claim holder     : {pop['unclaimed_and_noted']}")
    if pop.get("min_chars") is not None:
        label = f"note >= {pop['min_chars']} chars"
        print(f"  {label:<20}: {pop['over_threshold']}  "
              f"(the sweep's own keep threshold, read from its source)")
    if pop.get("sid_only_claimed"):
        print(f"    ...of which claimed_by is NULL but claimed_by_sid is SET: "
              f"{pop['sid_only_claimed']} — guard-4434 says these ARE claims; the two "
              f"lines above count them as unclaimed")
    if pop["by_holder"]:
        holders = ", ".join(f"{k}={v}" for k, v in list(pop["by_holder"].items())[:6])
        print(f"  by holder           : {holders}")
    print(f"  scanned             : {pop['non_terminal_scanned']} non-terminal goals "
          f"across {pop['aspirations']} active aspirations")
    if pop.get("recurring") is not None:
        print()
        print("  WHAT IS INSIDE THAT NUMBER (guard-2529: report what the filter excludes):")
        print(f"    recurring           : {pop['recurring']}  "
              f"legitimately re-pending between fires — closing one KILLS THE CADENCE")
        print(f"    deferred (non-recur): {pop['deferred']}  "
              f"parked against a NAMED gate — already routed, not waiting to be noticed")
        print(f"    UNDRAINED           : {pop['undrained']}  "
              f"<- the drainable class; the other two are not backlog")
    print()
    print("  READ THIS AS A DENOMINATOR, NOT A VERDICT. An outcome_note means work")
    print("  happened under the goal; it does not mean the goal is finished. The")
    print("  ACTIONABLE list below is a strict subset — bounded to THIS agent's")
    print("  claims and to DEAD carriers — so a zero there says nothing about the")
    print("  number above. The gap between them is the undrained backlog.")
    print("=" * 78)
    print()


def _render(cands: List[Dict[str, Any]], total_records: int,
            limit: int, product_only: bool,
            summary: Optional[Dict[str, Any]] = None) -> int:
    """Print the report. Returns the number of ACTIONABLE goals."""
    if total_records == 0:
        # Two very different causes produce this zero, and until 2026-08-15 both
        # printed the extraction-failure banner below. The old comment asserted
        # "this sweep always emits records on a live world" — measured true for
        # an agent holding 324 claims (alpha/cc-07) and FALSE for one holding 1
        # (zeta/cc-02, scanned=1 kept_completed_not_closed=0, every summary key
        # present and correct). So the banner sent a reader to debug an output
        # shape that was fine, on the agent where it fires most often.
        #
        # The sweep's OWN candidate counter settles it, and is a better
        # authority than the record walk that produced the zero.
        kcnc = (summary or {}).get("kept_completed_not_closed")
        scanned = (summary or {}).get("scanned")
        if kcnc == 0:
            print("clean for THIS AGENT: sweep scanned %s claim(s), "
                  "kept_completed_not_closed=0 — no candidates to triage."
                  % (scanned if scanned is not None else "?"))
            print("NOT a fleet all-clear. stranded-claim-sweep only examines "
                  "the CALLING agent's claims, so this lane surfaces one "
                  "agent's backlog per box. A clean read here is consistent "
                  "with a large backlog held by another agent — measured "
                  "2026-08-15: zeta read 0 while 338 sat under alpha's sid. "
                  "To see another agent's: --from-sweep <their-sweep.json>.")
            return 0
        # kcnc is non-zero or absent while the walk found nothing: the summary
        # really is the wrong shape, which is what this banner is for.
        print("!! NO RECORDS EXTRACTED from the sweep summary — extraction "
              "failed, this is NOT a result. Check the sweep's output shape. "
              "(kept_completed_not_closed=%r, scanned=%r)" % (kcnc, scanned))
        return -1

    shown = [c for c in cands if not product_only or _is_product(c)]
    n_product = sum(1 for c in cands if _is_product(c))

    print("=" * 78)
    print(f"ACTIONABLE: {len(cands)} goals held by a DEAD carrier "
          f"({n_product} product) — MOST finished, but read each note: "
          f"dead-carrier is a carrier-side fact, finished is a work-side one "
          f"(rb-7935)")
    print("=" * 78)
    print("Each goal below has already been WORKED. A worker Body finished it,")
    print("then died holding the claim. The reducer must VERIFY and CLOSE.")
    print()
    print("NO VERDICT IS COMPUTED — the note's opening is quoted verbatim, and")
    print("it is a 220-char TRUNCATION, not a first line: the verdict often")
    print("falls past the cut. Each row carries the command to read the whole")
    print("note; run it before deciding. LENGTH IS NOT VERDICT (guard-2852c).")
    print("Automated verdict-classification was measured unsafe on this corpus:")
    print("  58% false positives flagging not-done, and 22 notes that carry a")
    print("  positive verdict word while saying they are NOT finished.")
    print()
    print("DO NOT release these claims. The sweep KEEPS them deliberately")
    print("(guard-3628); releasing makes the scorer rank them first and the")
    print("finished work is re-executed (g-115-5177).")
    print()
    print("CLOSE RECIPE (the same one 0.5g.7 and g-115-6337 use — a by-hand close")
    print("here is NOT do_verify, so outcome_class is not stamped for you):")
    print("  bash core/scripts/aspirations-complete-by.sh --source <world|agent> <goal-id> \\")
    print("      --key-finding '<one line: what the note proves>'")
    print("  bash core/scripts/aspirations-update-goal.sh --source <world|agent> <goal-id> \\")
    print("      outcome_class <deep|routine>")
    print("Measured 2026-08-16: 63 same-day reducer closes carried outcome_class=None,")
    print("so Phase-6 spark routing and the encoding lane read them as unclassified.")
    print()

    if not shown:
        print("(no rows match the current filter)")
        return len(cands)

    for rec in shown[:limit]:
        ev = rec.get("completion_evidence") or {}
        carrier = rec.get("body_carrier") or {}
        days = (rec.get("age_minutes") or 0) / 1440.0
        tag = "PRODUCT" if _is_product(rec) else "framework"
        print(f"--- {rec.get('goal_id')}  [{rec.get('asp_id')}, {tag}]  "
              f"stuck {days:.1f}d")
        print(f"    title   : {str(rec.get('title') or '')[:150]}")
        print(f"    holder  : sid {carrier.get('sid')} on "
              f"{carrier.get('carrier_host')}, carrier stale "
              f"{(carrier.get('carrier_age_minutes') or 0)/1440.0:.1f}d")
        if ev.get("predicate") == "outcome_note":
            note_len = ev.get("note_len") or 0
            head = ev.get("note_head") or ""
            print(f"    note    : {note_len} chars — OPENS WITH (first "
                  f"{len(head)} only, TRUNCATED):")
            print(f"      {head}")
            # Naming what the quote omits is the whole point. Measured on
            # : the head is a provenance banner and the note's actual
            # "=== THE VERDICT ===" section begins around char 400, invisible
            # here. A reader who treats this excerpt as the verdict is making
            # exactly the error this script refuses to automate.
            if note_len > len(head):
                print(f"      ^ {note_len - len(head)} chars NOT shown. The "
                      f"verdict may be past the cut. Read the whole note:")
                print(f"        bash core/scripts/aspirations-query.sh "
                      f"--goal-field id {rec.get('goal_id')} --full")
        else:
            # The note-less case: evidence is a pipeline back-reference, so
            # there is no first line to read. Say so rather than printing an
            # empty quote, which would read as "the note is blank".
            print(f"    note    : NONE. Evidence is "
                  f"{ev.get('predicate')} — the reducer must read the pipeline "
                  f"record; this goal has no outcome_note to judge from.")
        print()

    dropped = len(shown) - limit
    if dropped > 0:
        print(f"... {dropped} more not shown (--limit {limit}). "
              f"Raise --limit to see them; they are not excluded, only unprinted.")
    if product_only:
        print(f"NOTE: --product-only is active — {len(cands) - len(shown)} "
              f"framework goals are hidden from this view but still stuck.")
    return len(cands)


# ─────────────────────── cadence gate (mirrors scar-tissue-check) ───────────────────────
#
# WHY THIS EXISTS ( outcome 1: "invoked from a named call site, NOT
# merely present on disk"). Before this block, the only reference to this script
# anywhere in the tree was a suggestion STRING in stranded-claim-sweep.py's
# stderr — a sentence addressed to a human who happened to be reading sweep
# output. Measured 2026-08-15: zero call sites in core/, .claude/, the cadence
# registry, the precheck batteries and aspirations.yaml; the only other hits were
# this script's own tests. That is rb-7741 exactly — the producer shipped and the
# consuming cadence did not — and it is the same defect this whole lane exists to
# report on, reproduced one layer up in the reporter itself.
#
# The gate is COPIED IN SHAPE from scar-tissue-check.py, which this file's header
# already names as its posture precedent, rather than invented: same WM-slot
# record, same first-fire normalization, same zero-guard. Both guards below are
# load-bearing and were learned the hard way by the sibling rituals — do not
# simplify them away.

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

_DEFAULT_GOAL_CADENCE = 50
_DEFAULT_WM_SLOT = "last_completed_not_closed_triage"


def _load_cadence_config() -> Dict[str, Any]:
    """Load the ``completed_not_closed_triage`` block from aspirations.yaml.

    Fails SOFT to defaults (this is an observability instrument, not a gate) but
    prints the reason — a config read that silently returns defaults advertises a
    tuning knob that does nothing.
    """
    cfg: Dict[str, Any] = {"goal_cadence": _DEFAULT_GOAL_CADENCE,
                           "wm_slot": _DEFAULT_WM_SLOT,
                           "limit": _DEFAULT_LIMIT}
    try:
        import yaml
        from _paths import PROJECT_ROOT
        cfg_path = os.path.join(str(PROJECT_ROOT), "core", "config", "aspirations.yaml")
        if not os.path.exists(cfg_path):
            return cfg
        with open(cfg_path, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
        block = loaded.get("completed_not_closed_triage") or {}
        cfg["goal_cadence"] = int(block.get("goal_cadence", cfg["goal_cadence"]))
        cfg["wm_slot"] = str(block.get("wm_slot", cfg["wm_slot"]))
        cfg["limit"] = int(block.get("limit", cfg["limit"]))
    except Exception as e:
        print("[cnc-triage] cadence config read failed: " + str(e), file=sys.stderr)
    return cfg


def _count_completed_goals() -> int:
    """Total completed goals, via the shared helper every cadence ritual uses.

    Returns 0 on EVERY failure path — which is why _cadence_gate carries the
    zero-guard: this sentinel is indistinguishable from a real zero.
    """
    try:
        from _paths import CORE_ROOT, PROJECT_ROOT
        script = os.path.join(str(CORE_ROOT), "scripts", "fresh-eyes-cadence-check.py")
        r = subprocess.run([sys.executable, script, "--print-current"],
                           cwd=str(PROJECT_ROOT), capture_output=True,
                           text=True, timeout=10)
        if r.returncode != 0:
            return 0
        return int(r.stdout.strip() or 0)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return 0


def _wm_read(slot: str):
    try:
        import _rt
        raw = (_rt.wm_read(slot=slot, as_json=True) or "").strip()
        if not raw or raw == "null":
            return None
        return json.loads(raw)
    except Exception:
        return None


def _wm_set(slot: str, value: Any) -> None:
    try:
        from _paths import CORE_ROOT
        subprocess.run([sys.executable, os.path.join(str(CORE_ROOT), "scripts", "wm.py"),
                        "set", slot],
                       input=json.dumps(value), capture_output=True,
                       text=True, check=True, timeout=10)
    except Exception as e:
        print("[cnc-triage] wm-set failed: " + str(e), file=sys.stderr)


def _cadence_gate():
    """Return (fire, current, cfg, last). fire=True when the cadence crossed.

    first-fire normalization (g-001-190) — an unset slot must not fire on the
    full historical goal count; cap the diff at one cadence so the ritual reads
    'due now' rather than 'overdue by thousands'.

    zero-guard (guard-1091) — _count_completed_goals returns 0 as a silent
    failure sentinel. Re-baselining on it would persist a transient failure as
    the new basis and then spuriously fire. Noop WITHOUT re-stamping so the next
    check retries; a real completed-goal count never falls to zero.
    """
    cfg = _load_cadence_config()
    current = _count_completed_goals()
    last = _wm_read(cfg["wm_slot"])

    if not isinstance(last, dict):
        diff = min(current, cfg["goal_cadence"])
        return diff >= cfg["goal_cadence"], current, cfg, None

    last_count = int(last.get("goals_count_at_last_fire", 0) or 0)
    diff = current - last_count
    if last_count == 0:
        diff = min(diff, cfg["goal_cadence"])

    if diff < 0:
        if current == 0:
            print("[cnc-triage] negative diff with current=0 (last=%d) — FAILED "
                  "MEASUREMENT, not a real basis; noop WITHOUT re-stamp so the "
                  "next check retries" % last_count, file=sys.stderr)
            return False, current, cfg, last
        _wm_set(cfg["wm_slot"], {"timestamp": last.get("timestamp", "0000-00-00T00:00:00"),
                                 "goals_count_at_last_fire": current,
                                 "rebaselined_from": last_count})
        print("[cnc-triage] negative diff — basis moved backward (last=%d > "
              "current=%d); re-baselined, noop this iter" % (last_count, current))
        return False, current, cfg, last

    return diff >= cfg["goal_cadence"], current, cfg, last


def _stamp(cfg: Dict[str, Any], current: int) -> None:
    from datetime import datetime
    _wm_set(cfg["wm_slot"], {"timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                             "goals_count_at_last_fire": current})


def _post_board(cands: List[Dict[str, Any]], total: int,
                pop: Optional[Dict[str, Any]] = None) -> None:
    """Post the backlog to the findings board. Called only under --post-board.

    THE HEADLINE IS THE BACKLOG, NEVER THE SCAN TOTAL (g-115-6260 outcome 3).
    The sweep's own summary already emits this population every iteration and it
    still reads as HEALTH, because its headline is `scanned=363 / kept=363 /
    released=0` — a 100%-kept sweep reads cleaner than a partial one. So this
    post leads with the stuck count and the product share, and never prints a
    kept/scanned ratio.

    FIRING CONDITION (g-115-6337). Until this change the caller fired only when
    `cands` was non-empty — i.e. only on DEAD-carrier goals held by THE CALLING
    AGENT. That is the doubly-bounded subset, and it was empty on every box on
    2026-08-15 while 341 claimed-and-noted goals sat unbanked (337 under alpha,
    which was alive). So the lane's ONLY outbound signal was gated on a subset
    that is structurally zero for whoever is not holding the backlog — the
    "sweep with no consumer" shape (reclaim-routed-work.md rule 6) reproduced in
    the one line that was supposed to be the consumer. The post now fires on the
    FLEET population, which is the number a reducer on any box can act on, and
    the dead-carrier count rides along as the subset it is.
    """
    from _paths import CORE_ROOT
    prod = sum(1 for c in cands if _is_product(c))
    # The sweep's age field is `age_minutes`. Named here rather than guessed:
    # an earlier draft of this line read a `claim_age_hours` key that no record
    # carries, so `oldest` would have printed 0.0h forever — a wrong ZERO that
    # reads as "nothing is old", which is precisely the reassuring direction
    # (rb-245 key-format mismatch; guard-3878 on false zeros from a hand-written
    # read). Verified against a live candidate before this line was written.
    oldest = max((c.get("age_minutes") or 0) for c in cands) / 60.0 if cands else 0.0

    # Fleet headline first: it is the only number that is the same on every box.
    # The dead-carrier count below it is scoped to whoever ran the lane, so
    # leading with it would publish one agent's view as the fleet's (guard-3830).
    if pop and pop.get("readable"):
        holders = ", ".join(f"{k}={v}" for k, v in
                            list(pop["by_holder"].items())[:6]) or "none"
        head = (
            "COMPLETED-NOT-CLOSED, FLEET-WIDE: %d non-terminal goal(s) carry "
            "completion evidence (%d product). %d are claimed — by holder: %s. "
            "%d carry a note with no claim holder.\n\n"
            "This is a DENOMINATOR, not a verdict: an outcome_note means work "
            "happened under the goal, not that the goal is finished. It is also "
            "the number the per-agent lane cannot see — stranded-claim-sweep is "
            "bound-agent-scoped, so a reducer reads ~0 on every box except the "
            "one holding the claims.\n\n"
            "WHOEVER HOLDS THE BULK OF THESE IS THE ONLY MIND THAT CAN BANK "
            "THEM. A goal held at in-progress by another agent's Body must not "
            "be closed cross-agent; the holder's reducer verifies and closes.\n\n"
            % (pop["noted"], pop["product"], pop["claimed_and_noted"], holders,
               pop["unclaimed_and_noted"])
        )
    elif pop:
        head = ("COMPLETED-NOT-CLOSED: the fleet population was UNREADABLE this "
                "run (%s). That is not zero — treat the per-agent count below as "
                "a floor.\n\n" % pop.get("reason"))
    else:
        head = ""

    body = head + (
        "DEAD-CARRIER SUBSET (this agent only): %d goal(s) held at in-progress "
        "by a DEAD worker carrier, %d of them product. Oldest claim %.1fh.\n\n"
        "MOST carry completion evidence that nothing will bank: the reducer that "
        "would verify them is gone, and the selector correctly refuses to "
        "re-execute a goal claimed by the same mind from another Body. So that "
        "work is paid for and unbanked.\n\n"
        "Do NOT read the count as 'N finished goals'. Dead-carrier is a "
        "CARRIER-side fact; finished is a WORK-side one, and they are "
        "independent. Age does not separate them either: run live 2026-08-15 on "
        "the three oldest, two closed on their evidence and the SINGLE OLDEST "
        "(g-335-989) said 'NOT RESOLVED — the remaining gate is elapsed time' and "
        "needed a claim release plus a precondition_unmet: defer. So the case an "
        "age-ranked list surfaces first is the one that must not be closed. "
        "(rb-7935)\n\n"
        "Triage (report-only, cannot mutate claim state):\n"
        "    bash core/scripts/completed-not-closed-triage.sh\n\n"
        "Do NOT reach for the two obvious remedies — both are measured-rejected. "
        "RELEASE converts 'held for the reducer' into 'available to anyone' and "
        "the scorer then ranks finished work FIRST on fresh metadata (g-115-5177). "
        "BLIND-CLOSE by classifying outcome_note was measured on this exact "
        "corpus: 58%% false-positive rate flagging not-done, and 22 of 423 "
        "positive-verdict notes say in the same breath that they are NOT finished "
        "('DIAGNOSIS COMPLETE, FIX NOT DONE'). Closing those buries open work "
        "under a false verdict."
        % (len(cands), prod, oldest)
    )
    try:
        # bash_cmd, never a bare "bash" argv[0] — that resolves via System32 to
        # the WSL launcher on win32 and can hang forever (guard-580/581).
        # Absolute path preserved from the original CORE_ROOT join: bash_cmd does
        # not absolutize, and this script must not assume cwd is PROJECT_ROOT.
        from _runtime_bash import bash_cmd
        r = subprocess.run(bash_cmd(Path(CORE_ROOT, "scripts", "board-post.sh").as_posix(),
                                    "--channel", "findings", "--type", "finding",
                                    "--tags",
                                    "completed-not-closed,reducer-lane,unbanked-work"),
                           input=body, capture_output=True, text=True, timeout=60)
        # A non-zero rc is NOT an exception, so the except below never sees it.
        # Without this branch a board post that FAILED returned silently and the
        # caller printed nothing — in the one path whose entire job is making an
        # invisible backlog visible. capture_output also swallows the stderr that
        # explains it, so the rc must be read and the stderr re-emitted by hand.
        # Fail-open deliberately: a failed post must not break the report, but it
        # must not masquerade as a successful one either. (fresh-eyes F-1,
        # ; sibling calls at L353/L377 already did this.)
        if r.returncode != 0:
            print("[cnc-triage] board post FAILED rc=%d: %s"
                  % (r.returncode, (r.stderr or "").strip()[:400]), file=sys.stderr)
    except Exception as e:
        print("[cnc-triage] board post failed: " + str(e), file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="REPORT-ONLY triage of finished goals held by dead worker "
                    "carriers. Never mutates; has no --apply by design.",
    )
    parser.add_argument(
        "--from-sweep", metavar="PATH",
        help="Read a saved stranded-claim-sweep JSON instead of running the "
             "sweep. Use this to triage without paying for a fresh scan.",
    )
    parser.add_argument(
        "--limit", type=int, default=_DEFAULT_LIMIT,
        help=f"Maximum rows to print, oldest claim first. The count of dropped "
             f"rows is always announced. Default: {_DEFAULT_LIMIT}.",
    )
    parser.add_argument(
        "--product-only", action="store_true",
        help="Show only product-aspiration goals. The hidden count is still "
             "reported so the filter cannot read as a smaller population.",
    )
    parser.add_argument(
        "--timeout", type=int, default=900,
        help="Seconds to allow the sweep subprocess. Default: 900.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit the candidate records as JSON instead of the report.",
    )
    parser.add_argument(
        "--no-fleet", action="store_true",
        help="Skip the fleet-wide denominator (one world-store read). Opt-OUT "
             "rather than opt-in on purpose: the ACTIONABLE list is bounded to "
             "this agent's claims AND to dead carriers, so without the "
             "denominator a structural zero reads as a fleet all-clear "
             "(guard-3830).",
    )
    parser.add_argument(
        "--cadence", action="store_true",
        help="Cadence-gated run (the periodic call site). Exits 0 without "
             "running the sweep when the goal cadence has not been crossed, so "
             "it is cheap to call every iteration.",
    )
    parser.add_argument(
        "--post-board", action="store_true",
        help="Post the backlog to the findings board when candidates exist. "
             "Silent when the backlog is empty — an instrument that posts every "
             "fire trains its readers to skip it.",
    )
    args = parser.parse_args()

    cfg = None
    prev_population = None
    if args.cadence:
        fire, current, cfg, _last = _cadence_gate()
        if isinstance(_last, dict):
            prev_population = _last.get("population")
        if not fire:
            print("[cnc-triage] cadence not crossed (completed=%d, every %d) — noop"
                  % (current, cfg["goal_cadence"]))
            return 0
        # Stamp BEFORE the expensive sweep. A crash mid-sweep then costs one
        # skipped cycle rather than re-running a ~5-minute scan every iteration
        # until it happens to succeed.
        _stamp(cfg, current)
        if args.limit == _DEFAULT_LIMIT:
            args.limit = cfg["limit"]

    summary = _load_sweep(args.from_sweep, args.timeout)
    total = sum(1 for _ in _walk_records(summary))
    cands = _candidates(summary)

    # The fleet denominator is computed unless explicitly waived, and rendered
    # BEFORE _render — which returns early on its zero path, so a block printed
    # inside it would be skipped on exactly the runs that most need it.
    # `waived` separates "the caller opted out" from "the read failed". Both are
    # readable=False, and conflating them would make --no-fleet publish an
    # UNREADABLE-population finding that nothing actually failed to read.
    pop: Dict[str, Any] = {"readable": False, "waived": True,
                           "reason": "not computed (--no-fleet)"}
    if not args.no_fleet:
        pop = _fleet_population(args.timeout)

    if args.json:
        json.dump(
            {"actionable": len(cands),
             "product": sum(1 for c in cands if _is_product(c)),
             "total_sweep_records": total,
             "fleet_population": pop,
             "candidates": cands},
            sys.stdout, indent=2, ensure_ascii=False,
        )
        print()
        return 0

    # Trend before the report: a run whose own batch drained cleanly still has
    # to show a RISING population, or the lane reports health while the backlog
    # grows behind it ( outcome 5).
    if pop.get("readable"):
        if prev_population is not None:
            delta = pop["noted"] - prev_population
            arrow = "RISING" if delta > 0 else ("falling" if delta < 0 else "flat")
            print(f"TREND since last cadence fire: {prev_population} -> "
                  f"{pop['noted']} ({delta:+d}, {arrow})")
        elif args.cadence:
            print("TREND: no prior population recorded — this fire establishes "
                  "the baseline, so no direction can be read from it yet.")
        # Merge the population into the cadence stamp. A SECOND write, not a
        # move of the pre-sweep one: that stamp fires before the expensive sweep
        # on purpose (a crash then costs one cycle, not a re-run every
        # iteration), and it has no population to record yet.
        if args.cadence and cfg:
            from datetime import datetime
            _wm_set(cfg["wm_slot"], {
                "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "goals_count_at_last_fire": current,
                "population": pop["noted"],
                "population_claimed": pop["claimed_and_noted"],
                "population_product": pop["product"],
            })

    _render_fleet(pop, (os.environ.get("MIND_AGENT") or "?").strip() or "?")
    rc = 0 if _render(cands, total, args.limit, args.product_only,
                      summary) >= 0 else 1
    # Fire on the FLEET backlog, not on the doubly-bounded dead-carrier subset
    # ( — see _post_board's FIRING CONDITION note). Still silent when
    # there is genuinely nothing to report: an instrument that posts on every
    # cadence trains its readers to skip it. An UNREADABLE population posts,
    # because a failed measurement is itself worth surfacing.
    backlog = bool(cands) or (
        not pop.get("waived")
        and (not pop.get("readable") or (pop.get("noted") or 0) > 0)
    )
    if args.post_board and backlog:
        _post_board(cands, total, pop)
    return rc


if __name__ == "__main__":
    sys.exit(main())
