#!/usr/bin/env python3
"""Precondition Defer Recheck — re-evaluate structured-precondition defers.

For each goal whose defer_reason starts with 'precondition_unmet:', re-run
predicate.evaluate_all against the goal's structured preconditions. When ALL
structured predicates pass, clear defer_reason + defer_reason_set_at.

Replaces the LLM-side loop in aspirations-precheck Phase 0.5b.3 (which called
predicate-eval.sh per-goal as a subprocess and would false-clear when zero
structured predicates existed — the CLI exit-code-0-on-empty "vacuous-truth"
bug). Following the bash-consolidation pattern (rb-428) used by sibling
rechecks: blocker-recheck.sh (Layer C), defer-recheck.sh (Layer C for free-
form defers), monitor-stale-check.sh (proc-ID staleness), pending-questions-
sweep.sh (sentinel lifecycle).

Pre-filter (mirrors goal-selector.py L967-981 — "SYMMETRY: must be the
logical complement of the struct_pc check"):

    struct_pcs = [p for p in (goal.verification.preconditions or [])
                  if isinstance(p, dict) and "type" in p]

When the pre-filtered list is EMPTY, the defer is free-form (string-only
preconditions or no preconditions at all — LLM judgment territory).
Action: SKIP. Do NOT clear. Clearing here was the vacuous-truth bug we are
explicitly avoiding.

When the pre-filtered list is non-empty, evaluate via predicate.evaluate_all
in-process (not a subprocess). In-process avoids the CLI-exit-code-0-on-empty
collision.

UNCOVERED-GATE GUARD (g-115-10231). Passing every structured predicate is
necessary but NOT sufficient to clear. This sweep SELECTS on the defer TEXT
and TESTS `verification.preconditions`, and those are two different things:
when the structured preconditions were satisfied long ago and a LATER defer
cites a gate never added to that list, "all pass" answers a question nobody
asked and the clear destroys a live protection. So before clearing, every
goal-id token in the defer text must be mentioned somewhere in the structured
predicates; any that is not makes the goal a SKIP with
`action: "skipped"`, an explicit reason, and `uncovered_gate_refs`. A detail
row carrying BOTH `all_passed: true` and a non-empty `uncovered_gate_refs`
is precisely a defer the pre-fix sweep would have wrongly cleared, so the
sweep now measures its own former error rate on every run. Fails CLOSED: an
unrecognised reference means do not clear.

Dry-run by default; --apply clears via the typed daemon client
(_rt.aspirations_update_goal), which is the live write path.

The "sys.executable direct, never shell out" rationale this docstring used to
cite from defer-recheck.py _clear_defer WENT FALSE at the 2026-05-14 daemon
cutover and is deleted rather than reworded (g-115-9621). It read as a
deliberate reasoned invariant, which is exactly why nobody re-checked it for
four months: an in-file comment asserting its own context is not evidence
(rb-8970). Shelling out is still forbidden here (guard-1322 / guard-555) --
but the conclusion "so call aspirations.py directly" does not follow from it,
and that is the inference this deletion removes.

Exit: always 0 (reporting tool). JSON output:
  {
    "scanned": N,
    "eligible": N,            # defer_reason set + precondition_unmet: prefix + age >= threshold
    "evaluated": N,           # had structured preconditions to evaluate
    "skipped_free_form": N,   # had no structured preconditions (free-form defer)
    "skipped_uncovered_gate": N,  # all predicates passed, but the defer text
                                  # named a gate none of them mentions
                                  # () — the pre-fix wrong-clear count
    "cleared": N,             # actually cleared (apply only)
    "would_clear": [goal_ids],# dry-run list
    "details": [...],         # per-goal reasoning
    "apply": bool,
    "metrics_log": path|null,
  }

Reference: g-302-02 (this script). Sibling pattern: defer-recheck.py.
"""

import argparse
import datetime as dt
import json
import os
import re
# import subprocess  # removed with _run/_py ()
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from _dt import parse_naive_iso  # noqa: E402  (shared tzinfo-stripping naive-ISO parse, )
import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)

# Canonical path + fileop primitives (same SSOT as defer-recheck.py).
from _paths import WORLD_DIR  # noqa: E402
from _fileops import locked_append_jsonl  # noqa: E402
from predicate import evaluate_all  # noqa: E402


# --- Uncovered-gate guard () ------------------------------------
#
# DELIBERATELY BROADER THAN defer-recheck._extract_dep_ids, AND NOT SHARED WITH
# IT. That extractor answers "which goal-ids is this defer DEPENDENT on?" and is
# tuned to EXCLUDE incidental mentions (a parenthetical "( sibling
# pattern)" is correctly dropped from a dependency list). Here the question is
# the opposite polarity — "is there anything in this defer text that the
# structured predicates might not cover?" — and it gates a DESTRUCTIVE write
# (clearing a defer destroys a protection; nothing restores it). guard-2486: do
# not reuse one predicate across a reversible read and a destructive
# invalidation. Inheriting the extractor's exclusions here would mean the ids it
# deliberately drops are exactly the ids that slip through to the clear path,
# which is the defect this guard exists to stop. So: match ANY goal-id token,
# accept over-matching, and pay for it only in extra SKIPS — never in extra
# clears.
#
# The suffix group is `[a-z]+`, not `[a-z]`: a single letter TRUNCATES an
# id like `-fixture` to `-f`, which both mangles the id shown
# to the operator in `uncovered_gate_refs` and makes the token disagree with
# the same id read off a predicate.
_DEFER_GID_RE = re.compile(r"g-\d+-\d+(?:-[a-z]+)?", re.IGNORECASE)


def _gid_tokens(text):
    """The set of goal-id tokens in `text`, lowercased. One tokenizer, used on
    BOTH sides of every comparison in this module (rb-11386)."""
    return {m.group(0).lower() for m in _DEFER_GID_RE.finditer(text or "")}


def _uncovered_gate_refs(reason, struct_pcs, self_id):
    """Goal-ids named in the defer TEXT that no structured precondition mentions.

    Coverage is tested against the JSON serialisation of the structured
    predicates, so it catches the id wherever it lives on a predicate
    (`goal_id`, `after_ref`, `id`, a nested arg) without this function having
    to know the predicate schema — which would be a second place to keep in
    sync with predicate.py.

    BOTH SIDES ARE TOKENIZED, AND THE COVERED SIDE IS A SET, NOT A STRING
    (g-115-10365 — fresh-eyes finding on g-115-10231's own fix). A raw
    `id in json_blob` substring test has no trailing-digit boundary, so a defer
    naming a SHORT id reads as covered whenever the predicates happen to
    mention any LONGER id it prefixes — measured: defer names `g-115-1`,
    predicates carry only the unrelated `g-115-1364`, result `[]`, clear
    proceeds. That is guard-4516's boundary defect and guard-2362's
    container-type defect at once, and it fails OPEN in a DESTRUCTIVE write
    path — the exact polarity rb-11386 warns about, on the coverage side of
    the same comparison whose defer side rb-11386 came from. Membership in a
    token SET has the boundary built in.

    The goal's OWN id is never uncovered: a defer that names the goal it sits
    on is describing itself, not a gate.
    """
    try:
        covered_blob = json.dumps(struct_pcs, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001 — unserialisable predicate: assume nothing covered
        covered_blob = ""
    covered = _gid_tokens(covered_blob)
    # Normalise the OWN id through the SAME tokenizer used on the defer text.
    # Comparing a raw id string against an extracted token is an asymmetric
    # match and it fails open in the dangerous direction: any id the regex
    # matches only a PREFIX of (an unexpected suffix form) never equals the raw
    # string, so the goal's own id reads as a foreign gate. Caught by this
    # change's own test — one tokenizer on both sides is the invariant
    # (the filter-predicate-divergence class, rb-301).
    _own_m = _DEFER_GID_RE.search(self_id or "")
    own = (_own_m.group(0) if _own_m else (self_id or "")).lower()
    # NOTE: the fallback keeps an unparseable id comparable AS ITSELF rather
    # than collapsing to "" — an empty own-id matches no token and would
    # silently re-open the self-reference hole.
    out, seen = [], set()
    for m in _DEFER_GID_RE.finditer(reason or ""):
        gid = m.group(0)
        low = gid.lower()
        if low == own or low in seen:
            continue
        seen.add(low)
        if low not in covered:
            out.append(gid)
    return out


# --- Metrics logging (mirrors defer-recheck.py — rb-468 family) -------------

def _resolve_metrics_log(cli_path):
    """Resolve metrics log path. cli_path=='' → disabled; None → default."""
    if cli_path == "":
        return None
    if cli_path is not None:
        return Path(cli_path)
    return Path(WORLD_DIR) / "precondition-defer-recheck-metrics.jsonl"


def _append_metric(path, record):
    """Append one record to metrics JSONL. Fail-open: write failures stay
    visible on stderr but never abort the clearance run."""
    if path is None:
        return
    try:
        locked_append_jsonl(str(path), record)
    except Exception as e:
        print(f"[precondition-defer-recheck] WARN: metrics append failed: {e}",
              file=sys.stderr)


# --- () _run/_py deleted with the last caller. They existed only to
# invoke `aspirations.py update-goal` from _clear_defer, which now routes
# through _rt.aspirations_update_goal. Leaving an unused sys.executable
# subprocess helper here would be a loaded gun for the next reader who needs
# to call a migrated wrapper -- the shape is forbidden (guard-1322/guard-555),
# so it should not be sitting in the file looking sanctioned.


def _tolerant_decode(source, raw):
    """-tolerant decode for daemon aspirations_read body.

    Thin wrapper around `_rt.tolerant_decode_aggregate` (extracted via g-115-949).
    The shared primitive enforces the full contract: empty -> None, raw_decode
    recovery, guard-383 fatal on JSONDecodeError or non-dict-and-non-list.
    This function exists only to prepend the script-name prefix to the stderr
    diagnostic so existing log consumers don't need updates.

    See _rt.tolerant_decode_aggregate for the full guard-383 contract.
    """
    return _rt.tolerant_decode_aggregate(f"precondition-defer-recheck: {source}", raw)


def _read_goals(source):
    """Read all active goals from world or agent queue.

    Uses the daemon via _rt (aspirations.py read CLI was deleted in the
    2026-05-14 cutover; _rt is the canonical Python -> daemon client).
    Parse path is g-115-766-tolerant via _tolerant_decode — see that
    helper for the contract. Applied via g-115-797-A3 (bravo audit
    catalog row 14) — replaces the prior silent-collapse
    `except json.JSONDecodeError: return []` that would hide corruption
    behind a "no goals to recheck" no-op and freeze precondition_unmet
    defers indefinitely (preconditions never re-evaluated).

    RtError handling — guard-383 fatal symmetry (rb-987):
    `all_goals = _read_goals("world") + _read_goals("agent")` at line 179
    is the canonical N>=2-source aggregator pattern. Per guard-383, a
    per-source error must be FATAL (sys.exit(1)) — a silent `return []`
    would write a complete-looking lie into the merged aggregate (e.g.
    agent-only queue presented as the entire portfolio). The exemplar
    consolidation-health.py corrected this in commit 28a3b7a (after the
    initial g-115-796 spec-prescribed silent return); sibling A4
    (parent-supersession-sweep.py) followed the corrected pattern. A3
    matches A4 / consolidation-health.py — NOT A1 (defer-recheck.py),
    which still carries the pre-correction silent-return pattern and is
    itself a candidate for an audit-followup fix.
    The single fail-open boundary is the caller's shell wrapper
    `|| echo WARN` (rb-347), never inside this aggregator.
    """
    try:
        out = _rt.aspirations_read(source=source, active=True)
    except _rt.RtError as e:
        print(f"[precondition-defer-recheck] {source} read failed: {e.body or e}",
              file=sys.stderr)
        sys.exit(1)  # guard-383: source error fatal — single fail-open boundary is wrapper
    data = _tolerant_decode(source, out)
    if data is None:
        return []
    goals = []
    for asp in (data.get("aspirations") if isinstance(data, dict) else data) or []:
        for g in asp.get("goals", []) or []:
            g["_source"] = source
            g["_aspiration_id"] = asp.get("id")
            goals.append(g)
    return goals


def _age_hours(ts):
    if not ts:
        return None
    try:
        t = parse_naive_iso(ts)
        return (dt.datetime.now() - t).total_seconds() / 3600
    except Exception:
        return None


def _clear_defer(source, goal_id):
    """Clear both defer_reason and defer_reason_set_at on a goal.

    Returns (ok, detail) — detail is None on success, else the daemon's own
    error body. Callers MUST surface detail rather than reducing this to a
    boolean (rb-10397: aggregating per-record failures into a count hides the
    body that says why).

    Routed through the typed daemon client, NOT `aspirations.py update-goal`
    (g-115-9621). Three separate reasons, each sufficient:
      - no-python-cli-fallback.md: the migrated CLI path is not a fallback to
        keep, and the daemon is the live write path.
      - The CLI is BOX-DEPENDENT. Measured 2026-09-11: on cc-13 it printed
        `Error: 'NoneType' object has no attribute 'get'` and the sweep
        reported clear_failed for every eligible goal, while on cc-02
        (also STORAGE_BACKEND=own-cloud) the identical argv succeeded and the
        write landed. A path that works on one box and silently no-ops on
        another is worse than one that fails everywhere.
      - guard-1322 / guard-555 forbid the other tempting repair — shelling out
        to aspirations-update-goal.sh from Python. The PreToolUse MIND_AGENT
        injection fires only on Bash TOOL calls, so a Python-spawned subprocess
        resolves the WRONG AGENT: quieter than the bug being fixed.

    Two calls because update-goal is single-field; matches defer-recheck.py."""
    for field in ("defer_reason", "defer_reason_set_at"):
        try:
            _rt.aspirations_update_goal(goal_id, field, None, source=source)
        except _rt.RtError as e:
            return False, "%s: %s" % (field, e.body or e)
    return True, None


# --- Main --------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description=("Re-evaluate structured-precondition defers and clear "
                     "when all predicates pass. Avoids vacuous-truth bug via "
                     "explicit empty-list skip. Sibling: defer-recheck.py."),
    )
    ap.add_argument("--max-age-hours", type=float, default=2.0,
                    help="Minimum defer age before recheck (default 2h).")
    ap.add_argument("--apply", action="store_true",
                    help="Actually clear defer_reason + defer_reason_set_at "
                         "(default: dry-run, reports would_clear list).")
    ap.add_argument("--output", choices=["json", "human"], default="json")
    ap.add_argument("--metrics-log", default=None,
                    help=("Path to JSONL metrics log. Default: "
                          "<WORLD_PATH>/precondition-defer-recheck-metrics.jsonl. "
                          "Pass empty string to disable."))
    args = ap.parse_args()
    metrics_path = _resolve_metrics_log(args.metrics_log)

    all_goals = _read_goals("world") + _read_goals("agent")

    scanned = 0
    eligible = 0
    evaluated = 0
    skipped_free_form = 0
    skipped_uncovered_gate = 0
    cleared = 0
    would_clear = []
    details = []

    for g in all_goals:
        scanned += 1
        reason = g.get("defer_reason") or ""

        # Eligibility filters (same shape as defer-recheck.py).
        if not reason.startswith("precondition_unmet:"):
            continue
        if g.get("status") not in ("pending", "in-progress"):
            continue
        if g.get("deferred_until"):
            # Structured time gate is the authoritative scheduler signal;
            # defer_reason is parallel narrative when deferred_until is set.
            continue
        age_h = _age_hours(g.get("defer_reason_set_at")
                           or g.get("started")
                           or g.get("created_at"))
        if age_h is None or age_h < args.max_age_hours:
            continue
        eligible += 1

        # Pre-filter: dict-structured predicates only (mirrors goal-selector.py
        # L967-981). String preconditions are LLM-judgment-only; free-form
        # narratives never reach this script's clear path.
        pcs_raw = (g.get("verification") or {}).get("preconditions") or []
        struct_pcs = [p for p in pcs_raw
                      if isinstance(p, dict) and "type" in p]

        if not struct_pcs:
            # Vacuous-truth guard: zero structured predicates ≠ "all pass".
            # The defer is free-form / string-only — LLM judgment territory.
            # Do NOT clear. Report skip with explicit reason.
            skipped_free_form += 1
            details.append({
                "goal_id": g["id"],
                "source": g["_source"],
                "age_hours": round(age_h, 1),
                "action": "skipped",
                "reason": ("no structured preconditions to evaluate — defer "
                           "is free-form, LLM judgment required"),
                "struct_pc_count": 0,
                "raw_pc_count": len(pcs_raw),
            })
            continue

        # In-process evaluate_all (NOT a subprocess — avoids exit-code-on-empty
        # vacuous-truth bug AND avoids Windows bash subprocess unreliability).
        # mode="all" returns one result per predicate; we want the full picture
        # for diagnostic reporting on partial-pass cases.
        evaluated += 1
        pc_results = evaluate_all(struct_pcs, mode="all", include_skippable=True)
        failed = [r for r in pc_results if not r.passed]

        if failed:
            details.append({
                "goal_id": g["id"],
                "source": g["_source"],
                "age_hours": round(age_h, 1),
                "action": "skipped",
                "reason": f"{len(failed)}/{len(pc_results)} structured predicates still failing",
                "struct_pc_count": len(struct_pcs),
                "failing_predicates": [
                    {"type": r.type,
                     "id": r.predicate_id,
                     "reason": r.reason}
                    for r in failed
                ],
            })
            continue

        # All structured predicates pass — but that is not yet a licence to
        # clear. THE SELECTION KEY AND THE TESTED PREDICATE ARE TWO DIFFERENT
        # THINGS (): this sweep selects on the defer TEXT's
        # `precondition_unmet:` prefix and then tests `verification.
        # preconditions`. When a goal's structured preconditions were satisfied
        # long ago and a LATER defer cites a NEW gate that was never added to
        # that list, "all pass" is true of a question nobody asked, and the
        # clear destroys a live protection while leaving its gate untouched.
        # Measured on  (, a boosted closing lane): its one
        # structured precondition was discharged 2026-09-16, a 2026-09-17 defer
        # named  (still pending), and by 2026-09-18 the defer was gone.
        # That goal's remaining outcome needs a BILLED live vessel against a
        # channel that is still wrong on main, so the wrongly-cleared defer
        # converts a protected gate into a paid run with a guaranteed-misleading
        # result.
        #
        # THIS IS DELIBERATELY A SKIP, NOT A NARROWER CLEAR (guard-3628 — "the
        # sweep decided not to act" must be distinguishable from "the sweep
        # failed to catch it", so it is reported with its own action, reason and
        # the offending refs). The REJECTED alternative was remedy (b): require
        # the defer text to name a precondition ID carried by the structured
        # list, treating an unfindable name as free-form. Rejected because it
        # silently re-classifies every defer written before that convention
        # existed — which is all of them — turning a targeted safety skip into a
        # blanket one, and because it makes the SAFE behaviour depend on authors
        # adopting a new id-citation habit, i.e. it fails open on exactly the
        # inattention that produced the defect. This remedy fails CLOSED instead:
        # an unrecognised reference means do not clear. Do not re-litigate.
        #
        # The entry below is also the standing measurement for how often the old
        # predicate would have been wrong: a detail row carrying BOTH
        # all_passed=true AND a non-empty uncovered_gate_refs is, exactly, a
        # defer the pre-fix sweep would have cleared.
        uncovered = _uncovered_gate_refs(reason, struct_pcs, g.get("id"))
        if uncovered:
            skipped_uncovered_gate += 1
            details.append({
                "goal_id": g["id"],
                "source": g["_source"],
                "age_hours": round(age_h, 1),
                "action": "skipped",
                "reason": ("all %d structured predicate(s) pass, but the defer "
                           "text names %s which no structured precondition "
                           "mentions — the tested gate is not the named gate, "
                           "so clearing would destroy an untested protection "
                           "(g-115-10231)"
                           % (len(struct_pcs), ", ".join(uncovered))),
                "struct_pc_count": len(struct_pcs),
                "all_passed": True,
                "uncovered_gate_refs": uncovered,
                "would_have_cleared_pre_fix": True,
            })
            continue

        # All structured predicates pass AND the defer names no gate outside
        # them — clearable.
        entry = {
            "goal_id": g["id"],
            "source": g["_source"],
            "age_hours": round(age_h, 1),
            "action": "would_clear",
            "struct_pc_count": len(struct_pcs),
            "all_passed": True,
        }
        would_clear.append(g["id"])

        if args.apply:
            ok, clear_error = _clear_defer(g["_source"], g["id"])
            entry["action"] = "cleared" if ok else "clear_failed"
            if not ok:
                # Surface the daemon body, not just the count (rb-10397).
                entry["clear_error"] = clear_error
                print("precondition-defer-recheck: clear FAILED for %s: %s"
                      % (g["id"], clear_error), file=sys.stderr)
            if ok:
                cleared += 1
                # LOAD-BEARING: log ONLY after the clear succeeds — same
                # invariant as defer-recheck.py (rb-468 metrics integrity).
                _append_metric(metrics_path, {
                    "type": "precondition_defer_cleared",
                    "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
                    "goal_id": g["id"],
                    "source": g["_source"],
                    "aspiration_id": g.get("_aspiration_id"),
                    "age_hours_at_clear": round(age_h, 2),
                    "struct_pc_count": len(struct_pcs),
                    "clear_reason": "all_structured_predicates_passed",
                    "agent": os.environ.get("MIND_AGENT", "") or None,
                })
        details.append(entry)

    # Per-run summary — emitted regardless of clears, so consumers can compute
    # rate with a real denominator. Dry-runs log too (apply=false).
    _append_metric(metrics_path, {
        "type": "run_summary",
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "scanned": scanned,
        "eligible": eligible,
        "evaluated": evaluated,
        "skipped_free_form": skipped_free_form,
        "skipped_uncovered_gate": skipped_uncovered_gate,
        "would_clear_count": len(would_clear),
        "cleared": cleared,
        "apply": args.apply,
        "max_age_hours": args.max_age_hours,
        "agent": os.environ.get("MIND_AGENT", "") or None,
    })

    result = {
        "scanned": scanned,
        "eligible": eligible,
        "evaluated": evaluated,
        "skipped_free_form": skipped_free_form,
        "skipped_uncovered_gate": skipped_uncovered_gate,
        "cleared": cleared,
        "would_clear": would_clear,
        "details": details,
        "apply": args.apply,
        "metrics_log": str(metrics_path) if metrics_path else None,
    }

    if args.output == "human":
        print(f"scanned={scanned} eligible={eligible} evaluated={evaluated} "
              f"skipped_free_form={skipped_free_form} "
              f"skipped_uncovered_gate={skipped_uncovered_gate} "
              f"would_clear={len(would_clear)} cleared={cleared}")
        for d in details:
            print(f"  {d['goal_id']}: {d['action']} ({d.get('reason','')})")
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
