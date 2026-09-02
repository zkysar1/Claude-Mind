#!/usr/bin/env python3
"""Parent-Goal Supersession Sweep.

Detects parent "Apply: <topic>" goals that have been deferred while one or
more sibling goals in the same aspiration completed work that functionally
satisfies the parent's intent.

Pattern (rb-842, g-268-10 incident):
    Parent g-268-10 "Apply: Implement BT-seed prefetch" sat
    pending+deferred from 2026-05-06 to 2026-05-11 despite siblings
    g-268-15 ("Design: Option C", completed 2026-05-06) and g-268-16
    ("Apply: SeedGetterCache 119 LOC", completed 2026-05-06) fully
    satisfying the parent's verification criteria. The defer_reason
    narrative cited the unblock path but no script detected the sibling
    completion. This sweep closes that gap as a companion to
    blocker-recheck.sh / defer-recheck.sh / monitor-stale-check.sh.

Heuristic (conservative for v1 — favors precision):
    For each candidate parent goal P:
        - title starts with "Apply:"
        - status in (pending, in-progress)
        - defer_reason is non-null
        - age (defer_reason_set_at OR created_at) >= --max-age-hours
    Parent aspiration must be small (--max-aspiration-goals, default 50):
        - Large aspirations (e.g., asp-115 "Recurring Infrastructure
          Monitoring" with 611 goals) accumulate unrelated completions
          that produce false-positive matches. The canonical incident
          (g-268-10 / asp-268) was a 16-goal sprint where sibling
          decomposition genuinely covered parent intent. The size guard
          replicates "sprint-scope cohesion" without requiring topic
          string matching (which would have FAILED to detect the
          canonical incident — "BT-seed prefetch" shares no words with
          "SeedGetterCache").
    Scan sibling goals in same aspiration:
        - status == completed
        - title starts with "Apply:" or "Design:"
        - completed_at (or completed_date) > P.defer_reason_set_at
          (parent must have been deferred BEFORE sibling completion;
           otherwise the parent is the consumer of the sibling, not
           superseded by it)
    If >= --min-siblings (default 2) such siblings: P is a supersession
    candidate.

Second lane — structural split-parents (g-115-2603, evidence g-115-2601):
    Parents titled OTHER than "Apply:" (e.g. "Feature 3 (Tools): ...")
    qualify when completed siblings carry a DECOMPOSITION BACKREF naming
    them (origin_signal "decomposition:{parent_id}*" or discovered_by ==
    parent_id). Guards: ALL backref siblings must be completed (one
    non-terminal or skipped child = residual scope, no fire); a non-empty
    parent.blocked_by must consist entirely of completed same-aspiration
    siblings; and the newest backref completion must be >= --max-age-hours
    old (grace window). No defer_reason is required on this lane — the
    canonical g-350-04 shape had none. Result rows carry "lane":
    "apply" | "structural".

Action modes:
    --report (default): print JSON {candidates: [...], details: [...]}
    --apply: mark each candidate parent as status=completed with
        outcome_note "superseded by sibling decomposition: <sibling-ids>".

        NO note-based idempotency guard exists here, and MUST NOT be added
        (g-115-5097). This block claimed one until 2026-08-10; grep for
        outcome_note in this file and the only hits are this docstring and the
        write itself. The claim was simply false.

        Do not "restore" it. The two sibling sweeps
        (unblock-parent-status-sweep, routing-audit-target-status-sweep) DID
        implement that guard and it was a defect in both: _mark_superseded
        performs THREE non-atomic daemon writes (note, status, completed_date),
        so keying dedup on the note makes the FIRST write the key — a partial
        failure then leaves the parent note-bearing and still pending, and the
        guard skips it on every later run, permanently sealing the goal against
        its own repair. Adding the guard here would INSTALL that bug rather
        than fix an omission.

        Idempotency is instead supplied by the eligibility filter below: a
        successfully superseded parent is status=completed, which the
        pending/in-progress filter already excludes. A partial failure leaves it
        pending, so the next run simply retries and self-heals — which is the
        behaviour the two siblings had to be repaired back into.

Eligibility filters (mirror defer-recheck.py):
    - status MUST be pending OR in-progress (not completed/skipped/blocked)
    - deferred_until set: SKIPPED (structured time gate is authoritative)
    - aspiration must be active (archived aspirations are out of scope)

Companion scripts (rb-428 family):
    - blocker-recheck.py — Layer C for participants:[user] blockers
    - defer-recheck.py — explicit dep-chain narrative defers
    - precondition-defer-recheck.py — structured precondition_unmet
    - monitor-stale-check.py — Monitor proc-NNN goals past current run_dir
    - pending-questions-sweep.py — source_goal-completed lifecycle

Exit: always 0 (reporting tool, fail-open). Returns JSON:
    {
      "scanned": N,           # total parent-pattern candidates examined
      "eligible": N,          # passed age + defer + status filters
      "candidates": [...],    # supersession candidates (recommend close)
      "applied": N,           # parents marked superseded (apply mode only)
      "details": [...],       # per-goal trace incl. matched siblings
    }

CLI:
    parent-supersession-sweep.py [--max-age-hours N] [--apply] [--output json|human]
"""

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from _team_state import _is_owncloud_backend  # noqa: E402
from _sweep_write_guard import (  # noqa: E402
    reread_goal_authoritative as _shared_reread_goal_authoritative,
    stale_candidate_reason as _shared_stale_candidate_reason,
)
from _dt import parse_naive_iso  # noqa: E402  (shared tzinfo-stripping naive-ISO parse, )
import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)

from _paths import WORLD_DIR  # noqa: E402
from _fileops import locked_append_jsonl  # noqa: E402


APPLY_PATTERN = re.compile(r"^\s*Apply\s*:\s*(.+)$", re.IGNORECASE)
DESIGN_PATTERN = re.compile(r"^\s*Design\s*:\s*(.+)$", re.IGNORECASE)


def _resolve_metrics_log(cli_path):
    """Resolve metrics log path. Mirrors defer-recheck convention."""
    if cli_path == "":
        return None
    if cli_path is not None:
        return Path(cli_path)
    return Path(WORLD_DIR) / "parent-supersession-sweep-metrics.jsonl"


def _append_metric(path, record):
    """Append one metric record. Fail-open by design — metric-write
    failure must not abort the sweep."""
    if path is None:
        return
    try:
        locked_append_jsonl(str(path), record)
    except Exception as e:
        print(f"[parent-supersession-sweep] WARN: metrics append failed: {e}",
              file=sys.stderr)


def _run(argv, input_text=None):
    result = subprocess.run(argv, input=input_text, capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    return result.returncode, result.stdout, result.stderr


def _py(args, input_text=None):
    return _run([sys.executable] + args, input_text=input_text)


def _tolerant_decode(source, raw):
    """-tolerant decode for daemon aspirations_read body.

    Thin wrapper around `_rt.tolerant_decode_aggregate` (extracted via g-115-949).
    The shared primitive enforces the full contract: empty -> None, raw_decode
    recovery, guard-383 fatal on JSONDecodeError or non-dict-and-non-list.
    This function exists only to prepend the script-name prefix to the stderr
    diagnostic so existing log consumers don't need updates.

    See _rt.tolerant_decode_aggregate for the full guard-383 contract.
    """
    return _rt.tolerant_decode_aggregate(f"parent-supersession-sweep: {source}", raw)


def _read_aspirations(source):
    """Return list of (aspiration_dict, source_str) tuples. Preserves
    aspiration grouping so sibling lookup stays cheap (in-memory, not
    by-id index).

    Uses the daemon via _rt (aspirations.py read CLI was deleted in the
    2026-05-14 cutover; _rt is the canonical Python -> daemon client).
    Parse path is g-115-766-tolerant via _tolerant_decode — see that
    helper for the contract.
    """
    try:
        out = _rt.aspirations_read(source=source, active=True)
    except _rt.RtError as e:
        # guard-383: source error is FATAL for the N>=2-source aggregator
        # pattern (line 279 merges "world" + "agent"). A silent [] would
        # poison the merged aggregate with a complete-looking lie.
        print(f"parent-supersession-sweep: {source} read failed: {e.body or e}",
              file=sys.stderr)
        sys.exit(1)
    data = _tolerant_decode(source, out)
    if data is None:
        return []
    aspirations = data.get("aspirations") if isinstance(data, dict) else data
    return [(asp, source) for asp in (aspirations or [])]


def _age_hours(ts):
    if not ts:
        return None
    try:
        t = parse_naive_iso(ts)
        return (dt.datetime.now() - t).total_seconds() / 3600
    except Exception:
        return None


def _parse_ts(ts):
    """Parse ISO timestamp returning datetime or None."""
    if not ts:
        return None
    try:
        return parse_naive_iso(ts)
    except Exception:
        return None


def _is_apply_goal(g):
    return bool(APPLY_PATTERN.match(g.get("title", "") or ""))


def _is_design_or_apply(g):
    title = g.get("title", "") or ""
    return bool(APPLY_PATTERN.match(title) or DESIGN_PATTERN.match(title))


def _find_superseding_siblings(parent, siblings):
    """Return list of sibling goal-ids that plausibly superseded parent.

    A sibling qualifies when:
      - id != parent.id
      - title starts with "Apply:" or "Design:"
      - status == "completed"
      - completed timestamp is AFTER parent's defer_set_at
        (or parent's created_at as fallback)
    """
    parent_defer_set = (parent.get("defer_reason_set_at")
                        or parent.get("created_at"))
    parent_ref_ts = _parse_ts(parent_defer_set)
    if parent_ref_ts is None:
        # Without a parent reference timestamp we cannot enforce the
        # "sibling completed AFTER parent deferred" temporal guard.
        # Skip the goal — conservative.
        return []
    out = []
    for s in siblings:
        if s.get("id") == parent.get("id"):
            continue
        if s.get("status") != "completed":
            continue
        if not _is_design_or_apply(s):
            continue
        s_ts = _parse_ts(s.get("completed_at") or s.get("completed_date"))
        if s_ts is None:
            continue
        if s_ts <= parent_ref_ts:
            continue
        out.append({
            "id": s.get("id"),
            "title": s.get("title", ""),
            "completed_at": s.get("completed_at") or s.get("completed_date"),
        })
    return out


def _find_structural_split_siblings(parent, siblings):
    """Return completed decomposition-siblings that structurally superseded parent.

    Second candidate lane (g-115-2603, evidence g-115-2601): g-350-04
    "Feature 3 (Tools): ..." was split into g-350-17+g-350-18 (origin_signal
    "decomposition:g-350-04-*", both completed same night) but stranded 2 days
    because the Apply:-title lane above excluded it. Title prefixes are
    incidental; the DECOMPOSITION BACKREF is the structural signal:

      backref sibling := origin_signal startswith "decomposition:{parent_id}"
                         OR discovered_by == parent_id

    Guards (precision-first, mirrors the temporal conservatism of
    _find_superseding_siblings):
      - No backref siblings at all -> [] (lane does not apply).
      - ANY backref sibling not status=completed -> [] (split still in
        flight, or a child was skipped — residual scope may remain; the
        umbrella must not be auto-closed).
      - parent.blocked_by non-empty: every listed id must be a completed
        sibling in this aspiration; an unknown or non-completed dep -> []
        (the parent is a consumer still waiting, not a superseded umbrella).
    """
    pid = parent.get("id") or ""
    if not pid:
        return []
    backrefs = []
    for s in siblings:
        if s.get("id") == pid:
            continue
        osig = str(s.get("origin_signal") or "")
        # Boundary-anchored match (fresh-eyes finding, 2026-07-18): a bare
        # startswith("decomposition:{pid}") prefix-collides when one goal id
        # is a prefix of another ( vs ) — the longer id's
        # children would falsely count as the shorter parent's backrefs.
        if (osig == f"decomposition:{pid}"
                or osig.startswith(f"decomposition:{pid}-")
                or s.get("discovered_by") == pid):
            backrefs.append(s)
    if not backrefs:
        return []
    for s in backrefs:
        if s.get("status") != "completed":
            return []
    blocked_by = parent.get("blocked_by") or []
    if blocked_by:
        by_id = {s.get("id"): s for s in siblings}
        for dep_id in blocked_by:
            dep = by_id.get(dep_id)
            if dep is None or dep.get("status") != "completed":
                return []
    return [{
        "id": s.get("id"),
        "title": s.get("title", ""),
        "completed_at": s.get("completed_at") or s.get("completed_date"),
    } for s in backrefs]


def _newest_completion_age_hours(siblings_match):
    """Age in hours since the NEWEST completion among matched siblings, or
    None if no timestamp parses. Used as the structural lane's grace window:
    supersession fires only after the finished split has sat for
    --max-age-hours (a fresher completion may still be mid-handoff)."""
    newest = None
    for s in siblings_match:
        t = _parse_ts(s.get("completed_at"))
        if t is not None and (newest is None or t > newest):
            newest = t
    if newest is None:
        return None
    return (dt.datetime.now() - newest).total_seconds() / 3600


def _reread_goal_authoritative(source, goal_id):
    """``(goal, provenance)`` from the STORE OF RECORD — seam over the shared
    reader. Collaborators are passed explicitly and resolved as module globals
    at call time so they stay monkeypatchable here (guard-2385)."""
    return _shared_reread_goal_authoritative(
        source, goal_id,
        read_aspirations=_read_aspirations,
        is_owncloud=_is_owncloud_backend,
        label="parent-supersession-sweep",
    )


def _stale_candidate_reason(source, goal_id):
    """``None`` when the write may proceed, else the refusal reason."""
    goal, prov = _reread_goal_authoritative(source, goal_id)
    return _shared_stale_candidate_reason(goal, prov)


def _write_failure_reason(field, rc, out, err):
    """One-line diagnosis for a failed child write.

    stderr FIRST: aspirations.py prints its refusals there, and a gate that
    refuses often still writes a JSON body to stdout, so preferring stdout
    would surface the payload and bury the cause. Bounded to 400 chars —
    this rides on a per-goal record, and an unbounded child dump would push
    the emitted JSON past what a reader (or a log line) keeps.
    """
    detail = (err or "").strip() or (out or "").strip() or "no output"
    detail = " ".join(detail.split())
    if len(detail) > 400:
        detail = detail[:400] + "…"
    return f"write of {field!r} failed (rc={rc}): {detail}"


def _mark_superseded(source, goal_id, sibling_ids, metrics_path=None,
                     aspiration_id=None):
    """Mark parent as completed with outcome_note.

    Returns (ok, reason): reason is None on success, else a SHORT diagnosis
    string naming which child write failed and what it said. The caller puts
    it on the emitted record so `action: "mark_failed"` is not the only
    surviving trace (g-115-7957). Before this, all three write sites bound
    `rc1, _, _ = _py(...)` and returned a bare False, so the child's stderr
    was destroyed AT THE CALL, not merely unrouted -- a failed write reported
    action=mark_failed with no reason while every other action value carried
    one. Cost measured on this very script: its own :496 comment records
    g-249-06 stuck at mark_failed for 235h of repeated write attempts,
    "refused only by a downstream guard the sweep never consults".

    INVARIANT: uses sys.executable directly. Same rationale as
    defer-recheck._clear_defer — bash on Windows can resolve to WSL
    bash.exe with surprising PATH semantics; aspirations-update-goal.sh
    just shells aspirations.py with the same args, so calling .py
    directly loses nothing functional.

    g-115-6332: re-asserts the candidate predicate against the STORE OF RECORD
    immediately before writing, and refuses when it no longer holds.

    WORTH SPELLING OUT because this sweep writes `completed` rather than
    `skipped`, which reads like the harmless direction and is not. The
    destructive half is the FIRST write: outcome_note is overwritten with a
    template, so a goal another box closed seconds earlier loses its real
    closure evidence and keeps a plausible-looking terminal status — damage
    that leaves no anomaly for a status-based audit to notice."""
    stale = _stale_candidate_reason(source, goal_id)
    if stale is not None:
        print(f"[parent-supersession-sweep] REFUSED {goal_id}: {stale}",
              file=sys.stderr)
        # COUNT the refusal — a silent no-op is indistinguishable from never
        # having raced, which makes the guard's own effectiveness unmeasurable.
        _append_metric(metrics_path, {
            "type": "parent_supersession_refused_stale_candidate",
            "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
            "goal_id": goal_id,
            "source": source,
            "aspiration_id": aspiration_id,
            "sibling_ids": list(sibling_ids or []),
            "reason": stale,
            "agent": os.environ.get("MIND_AGENT", "") or None,
        })
        return False, stale

    sibling_str = ", ".join(sibling_ids)
    note = f"superseded by sibling decomposition: {sibling_str}"

    # : BOTH failure arms below emit a goal-keyed metrics row, and the
    # second one is the whole reason this goal existed.
    #
    # The writes are ORDERED note-then-status and the caller's success metric is
    # gated on `if ok:` (main(), the `_append_metric` after `applied += 1`). So a
    # run where the note lands and the status write is refused returns False,
    # emits `action: "mark_failed"` into the in-memory `details` only, and writes
    # NO metrics row naming the goal — while a false "superseded by sibling
    # decomposition" note is now sitting on a live goal. Phase 0.5b.8.5, the
    # sweep-mutation visibility surface (), reads this log, so the one
    # case where a mutation ACTUALLY LANDED was the one case it could not see.
    #
    # Measured on this world 2026-08-31 (alpha, cc-08) before the fix: the log
    # held 2189 rows, ALL of type run_summary — `grep -c 'g-[0-9]'` returned 0,
    # so no goal was named anywhere in it, ever. 117 of those runs were
    # mode=apply with candidates>0, i.e. 117 attempted mutations with zero
    # goal-keyed trace. Zero refused_stale_candidate rows exist, which rules out
    # the pre-write guard and leaves this path. That is how  came to
    # carry a supersession note the metrics log had never heard of.
    #
    # Logging from INSIDE rather than from the caller is deliberate: only here is
    # it known WHICH write failed, and therefore whether the note is on disk.
    # Deriving that in the caller would mean sniffing the reason string.
    _failed_common = {
        "type": "parent_supersession_mark_failed",
        "goal_id": goal_id,
        "source": source,
        "aspiration_id": aspiration_id,
        "sibling_ids": list(sibling_ids or []),
        "agent": os.environ.get("MIND_AGENT", "") or None,
    }

    # First write outcome_note (informational only — does NOT close goal).
    rc1, out1, err1 = _py([str(SCRIPT_DIR / "aspirations.py"),
                           "--source", source, "update-goal",
                           goal_id, "outcome_note", note])
    if rc1 != 0:
        reason = _write_failure_reason("outcome_note", rc1, out1, err1)
        _append_metric(metrics_path, {
            **_failed_common,
            "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
            "failed_field": "outcome_note",
            # Nothing landed — the goal is untouched and needs no repair.
            "outcome_note_written": False,
            "reason": reason,
        })
        return False, reason
    # Then close the goal.
    rc2, out2, err2 = _py([str(SCRIPT_DIR / "aspirations.py"),
                           "--source", source, "update-goal",
                           goal_id, "status", "completed"])
    if rc2 != 0:
        reason = _write_failure_reason("status", rc2, out2, err2)
        _append_metric(metrics_path, {
            **_failed_common,
            "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
            "failed_field": "status",
            # THE REPAIRABLE CASE. The note IS on disk and the goal is still
            # open, so it now reads as superseded while remaining live. The
            # note text rides along so a repair can match it exactly rather
            # than reconstructing the template and hoping the format matches.
            "outcome_note_written": True,
            "outcome_note": note,
            "reason": reason,
        })
        return False, reason
    # Stamp completed_date if absent.
    today = dt.date.today().isoformat()
    rc3, _, _ = _py([str(SCRIPT_DIR / "aspirations.py"),
                     "--source", source, "update-goal",
                     goal_id, "completed_date", today])
    # completed_date rewrite is best-effort — non-fatal if it fails
    # (most stores stamp it server-side via update-goal hooks).
    return True, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-age-hours", type=float, default=24.0,
                    help="Minimum parent age before consideration (default 24h)")
    ap.add_argument("--min-siblings", type=int, default=2,
                    help="Minimum sibling completion count for candidacy (default 2)")
    ap.add_argument("--max-aspiration-goals", type=int, default=50,
                    help=("Skip aspirations larger than this many goals "
                          "(default 50). Sprint-scope cohesion filter — large "
                          "aspirations like asp-115 accumulate unrelated "
                          "completions that produce false-positive matches."))
    ap.add_argument("--apply", action="store_true",
                    help="Mark candidates as superseded (default: report only)")
    ap.add_argument("--output", choices=["json", "human"], default="json")
    ap.add_argument("--metrics-log", default=None,
                    help=("Path to JSONL metrics log. Default: "
                          "<WORLD_PATH>/parent-supersession-sweep-metrics.jsonl. "
                          "Pass empty string to disable."))
    args = ap.parse_args()

    metrics_path = _resolve_metrics_log(args.metrics_log)

    # Gather all aspirations from both sources.
    all_aspirations = _read_aspirations("world") + _read_aspirations("agent")

    scanned = 0
    eligible = 0
    candidates = []
    applied = 0
    details = []

    for asp, source in all_aspirations:
        if asp.get("status") and asp["status"] != "active":
            continue
        goals = asp.get("goals", []) or []
        # Aspiration-size guard (sprint-scope cohesion filter). Large
        # aspirations accumulate unrelated completions that produce
        # false-positive matches. See docstring "Heuristic" section.
        if len(goals) > args.max_aspiration_goals:
            continue
        # Index siblings once per aspiration (cheap).
        for g in goals:
            # Two candidate lanes ():
            #   apply      — original title-prefix lane (defer_reason required)
            #   structural — decomposition-backref lane for split-parents
            #                titled otherwise (no defer_reason required; the
            #                 shape had none post-reconcile)
            lane = "apply" if _is_apply_goal(g) else None
            struct_sibs = []
            if lane is None:
                # +candidate — §11b/ (world/conventions/goal-intake-management.md):
                # row 31, found only after the sweep's predicate was widened to see
                # the `.get("status")` accessor. Without this a candidate parent is
                # never superseded by its own decomposition siblings.
                if g.get("status") in ("pending", "in-progress", "candidate"):
                    struct_sibs = _find_structural_split_siblings(g, goals)
                if not struct_sibs:
                    continue
                lane = "structural"
            scanned += 1
            # +candidate — §11b/ (world/conventions/goal-intake-management.md).
            if g.get("status") not in ("pending", "in-progress", "candidate"):
                continue
            # A recurring goal is a STANDING CADENCE and can never be
            # superseded by sibling decomposition (). Siblings that
            # HARDEN what a recurring goal invokes improve the cadence, they do
            # not retire it — consolidate-before-expand.md rule 5, "improvement
            # is not redundancy". Placed here deliberately: after `scanned` (so
            # the scan count stays honest) and before BOTH lanes' `eligible`
            # bump (so a recurring goal never reports as an eligible candidate).
            #
            # Measured before the fix:  ("Recurring: run
            # infra-streak-notify.sh...", recurring=True, interval 16.2h,
            # achievedCount=174, currentStreak=4, status=pending) had been
            # reported eligible with action=mark_failed for 235h — ~10 days of
            # write attempts refused only by a downstream guard the sweep never
            # consults. Both outcomes were bad: keep failing and the sweep
            # permanently pollutes its own candidate signal; ever succeed and it
            # terminally closes a 174-completion cadence, which
            # aspirations-recover-recurring would then flip back — a close/
            # recover churn loop between two components each believing itself
            # correct. Excluding at the predicate (not relying on the refusal)
            # is the fix: a guard you only discover by reading a failed write is
            # not a guard this sweep is honoring.
            if g.get("recurring"):
                details.append({
                    "goal_id": g.get("id"),
                    "aspiration_id": asp.get("id"),
                    "lane": lane,
                    "action": "skipped",
                    "reason": ("recurring goal — a standing cadence cannot be "
                               "superseded by sibling decomposition "
                               "(achievedCount="
                               f"{g.get('achievedCount')}, interval_hours="
                               f"{g.get('interval_hours')})"),
                })
                continue
            if g.get("deferred_until"):
                # Structured time gate is authoritative — same skip rule
                # as defer-recheck.py.
                continue
            if lane == "apply":
                if not g.get("defer_reason"):
                    continue
                age_h = _age_hours(g.get("defer_reason_set_at")
                                   or g.get("started")
                                   or g.get("created_at"))
                if age_h is None or age_h < args.max_age_hours:
                    continue
                eligible += 1
                siblings_match = _find_superseding_siblings(g, goals)
            else:
                # Structural lane grace window: fire only after the newest
                # split completion has aged past --max-age-hours (a fresher
                # completion may still be mid-handoff to the parent).
                age_h = _newest_completion_age_hours(struct_sibs)
                if age_h is None or age_h < args.max_age_hours:
                    continue
                eligible += 1
                siblings_match = struct_sibs
            if len(siblings_match) < args.min_siblings:
                details.append({
                    "goal_id": g.get("id"),
                    "aspiration_id": asp.get("id"),
                    "age_hours": round(age_h, 1),
                    "matched_siblings": len(siblings_match),
                    "action": "skipped",
                    "reason": (f"insufficient sibling matches "
                               f"({len(siblings_match)} < {args.min_siblings})"),
                })
                continue
            sibling_ids = [s["id"] for s in siblings_match]
            entry = {
                "goal_id": g.get("id"),
                "aspiration_id": asp.get("id"),
                "source": source,
                "lane": lane,
                "age_hours": round(age_h, 1),
                "title": g.get("title", ""),
                "defer_reason": (g.get("defer_reason") or "")[:120],
                "siblings": siblings_match,
                "action": "would_mark",
            }
            candidates.append({
                "goal_id": g.get("id"),
                "aspiration_id": asp.get("id"),
                "lane": lane,
                "siblings": sibling_ids,
            })
            if args.apply:
                ok, fail_reason = _mark_superseded(
                    source, g.get("id"), sibling_ids,
                    metrics_path=metrics_path,
                    aspiration_id=asp.get("id"))
                entry["action"] = "marked" if ok else "mark_failed"
                if not ok and fail_reason:
                    # Every other action value this script emits carries a
                    # reason; mark_failed did not ().
                    entry["reason"] = fail_reason
                if ok:
                    applied += 1
                    _append_metric(metrics_path, {
                        "type": "parent_superseded",
                        "lane": lane,
                        "timestamp": dt.datetime.now().isoformat(
                            timespec="seconds"),
                        "goal_id": g.get("id"),
                        "source": source,
                        "aspiration_id": asp.get("id"),
                        "age_hours_at_mark": round(age_h, 2),
                        "sibling_ids": sibling_ids,
                        "sibling_count": len(sibling_ids),
                        "agent": os.environ.get("MIND_AGENT", "") or None,
                    })
            details.append(entry)

    # Always log run summary (whether or not anything fired).
    _append_metric(metrics_path, {
        "type": "run_summary",
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "scanned": scanned,
        "eligible": eligible,
        "candidates": len(candidates),
        "applied": applied,
        "mode": "apply" if args.apply else "report",
        "agent": os.environ.get("MIND_AGENT", "") or None,
    })

    result = {
        "scanned": scanned,
        "eligible": eligible,
        "candidates": candidates,
        "applied": applied,
        "details": details,
    }

    if args.output == "human":
        print(f"parent-supersession-sweep: scanned={scanned} eligible={eligible} "
              f"candidates={len(candidates)} applied={applied} "
              f"mode={'apply' if args.apply else 'report'}")
        for c in candidates:
            print(f"  {c['goal_id']} → superseded by {', '.join(c['siblings'])}")
    else:
        print(json.dumps(result, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
