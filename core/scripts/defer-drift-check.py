#!/usr/bin/env python3
"""Defer-Drift Check — flag goals whose deferred_until has gone STALE (PAST)
while a structured-unmet defer marker persists.

THE GAP THIS FILLS (the precise complement of precondition-defer-recheck.py):
precondition-defer-recheck.py deliberately SKIPS any goal that has
`deferred_until` set ("structured time gate is the authoritative scheduler
signal; defer_reason is parallel narrative when deferred_until is set" —
precondition-defer-recheck.py L230-233). So a goal with BOTH a structured
defer marker AND a `deferred_until` is owned by the time gate. But nothing
re-probes the time gate itself for drift. When `deferred_until` falls into the
PAST while the precondition it represents is still unmet, goal-selector's
`deferred_readiness` criterion reads the expired gate as "defer just expired,
re-evaluate now" and BOOSTS the not-ready goal to selector-top instead of
filtering it (free-text defer_reason does not self-enforce).

Canonical incident (2026-06-12, asp-304 Layer-5 cohort): g-304-11 carried
`defer_reason "precondition_unmet: ... 30-day window completes ~2026-07-11"`
but `deferred_until=2026-05-26` — a date set 16 days IN THE PAST relative to
its own `defer_reason_set_at`. The selector surfaced it at score 8.84 despite
~18h of the required 30 days of telemetry. g-304-15 had the same shape
(`deferred_until=2026-06-11` from a pre-repair start date). Four goals were
hand-re-gated; this guard makes the drift VISIBLE so it never lingers
undetected again. See the reasoning-bank entry "deferred_until drift from
defer_reason prose makes goal-selector deferred_readiness boost data-immature
goals to top" + the asp-304 Maintain goal for the lineage.

DETECTIVE + SELF-CONTAINED FILING (g-115-11720). The guard CANNOT
auto-CORRECT the gate: the correct future date lives in the defer_reason
PROSE, which it cannot parse reliably ENOUGH to pick the exact re-gate date
(parsing "~2026-07-11" out of free text is brittle, and clearing the defer
would wrongly surface a genuinely not-ready goal). So it SURFACES drift for
re-gate-by-judgment — exactly the fix a human/agent applies in ~30s once the
drift is known. The FILING moved into this script (g-115-11720): --apply
files ONE deduplicated re-gate Investigate (origin_signal
"investigate:defer-drift-audit") listing the drifted goals — the action the
precheck digest's Phase 0.5b.10 used to leave to LLM prose. An LLM WARN is
the exact drift class g-115-2595 scripted away in the sibling lane, so the
filing is bash-enforced, not left to LLM memory (rb-428 self-contained-apply
family, like reason-less-blocked-check.py). (It DOES extract prose dates for
one coarser purpose — telling an on-schedule expiry apart from genuine
drift, see on_schedule_expiry below — but that is a same-day proximity
check, not the exact-date parse it declines to trust for correction.)

DEDUP IS PER MEMBER, on the SAME read (guard-487 fail-closed by
construction). An open audit (origin_signal == "investigate:defer-drift-audit",
status pending/in-progress) covers a drifted id IFF it NAMES that id in its
title or description (audit_open_coverage.uncovered_ids — the shared
naming-surface rule both precheck filing lanes use). The old dedup skipped a
fresh filing while ANY open class-keyed audit existed — even one that named
none of the goals currently drifted. That fold was the 55-day latch behind
g-115-5132 (opened 2026-08-06, title still naming a goal absent from every
metrics run since; 9 drift reports from 3 agents folded into it, 0 re-gates
run, members flagged up to 518 h). Now: an id any open audit names is NOT
re-filed (that audit owns its members); an id no open audit names is
UNCOVERED and gets a fresh filing — at most ONE new audit per run, naming
exactly the uncovered ids. An unreadable (empty-title-and-description)
audit is treated as covering: a cross-box duplicate never self-heals, so
skip-on-uncertainty stays correct (guard-487).

ON-SCHEDULE EXPIRY (g-115-1541). A defer whose deferred_until was deliberately
set TO its own prose maturity date (e.g. defer_reason "...completes ~2026-06-18"
with deferred_until 2026-06-18T13:00) and simply elapsed is NOT drift — it is a
satisfied calendar-defer the normal deferred_readiness path should surface for
selection. Such goals are classified "on_schedule_expiry" and kept OUT of
drifted[] (into a separate on_schedule_expiry[] list), so the precheck files no
spurious drift Investigate. Genuine drift is the OPPOSITE shape: the prose
maturity date is well AFTER a stale deferred_until (the canonical g-304-11: prose
~2026-07-11 vs deferred_until 2026-05-26, ~46 days apart).

Eligibility (a "drifted" goal — ALL must hold):
  - status in (pending, in-progress)            # non-terminal
  - deferred_until is set AND parses to a PAST datetime
  - defer_reason starts with a structured-defer prefix
    (precondition_unmet: / blocked_on_dependency: / Circuit breaker:)
    — the three STRUCTURED_DEFER_PREFIXES (defer_classifier.py)

Each drifted goal is annotated with its structured-precondition status (when
structured preconditions exist, evaluate_all distinguishes "ready" — gate is
merely stale, a clear candidate — from "still_unmet" — genuine drift to
re-gate; prose-only defers are reported "prose" since the condition is
unverifiable here).

Reporting tool — NEVER mutates existing goal state. --apply ADDS at most one
audit Investigate goal (the filing is a new record, never an edit of the
drifted goals). Exit always 0 except guard-383 fatal on a source read error
(a silent empty-aggregate would hide drift behind a "0 drifted" lie — same
fatal-source-read contract as precondition-defer-recheck.py /
parent-supersession-sweep.py).

JSON output:
  {
    "scanned": N,
    "drift_count": N,                  # genuine drift only (on-schedule excluded)
    "drifted": [
      {"goal_id", "source", "aspiration_id", "deferred_until",
       "hours_past": float, "defer_prefix", "precondition_status",
       "title", "classification": "drift"}
    ],
    "on_schedule_expiry_count": N,     # : defers that expired on their
    "on_schedule_expiry": [            # own prose maturity date — NOT drift, so
      {..., "classification": "on_schedule_expiry", "prose_date": "YYYY-MM-DD"}
    ],                                 # the precheck files no drift Investigate
    "uncovered_ids": [...],            # : drifted ids NO open audit
                                       # names in its title/description — the
                                       # filing predicate (non-empty -> file)
    "open_audit_goal_ids": [...],      #   the open class-keyed audits (pending /
    "open_audit_exists": bool,         #   in-progress / candidate), for the human report
    "investigate_filed": str | None,   #   goal_id filed this run (--apply only)
    "actions_taken": "dry-run" | "apply",
    "now": iso,
  }

Sibling pattern (rb-428 bash-consolidation family): defer-recheck.py,
precondition-defer-recheck.py, unblock-parent-status-sweep.py. Guards honored:
guard-420 (datetime arithmetic — fromisoformat + Z-strip + exception-tolerant),
guard-645 (field-path reads — every field .get() with a default),
guard-614 (structured JSON output), guard-365 (bash wrapper consolidation).
Reference: g-115-1406.
"""

import argparse
import datetime as dt
import json
import os
import re
import socket
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from _dt import parse_naive_iso  # noqa: E402  (shared tzinfo-stripping naive-ISO parse, )
import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)

# Canonical path + fileop primitives (same SSOT as the writing siblings
# precondition-defer-recheck.py / defer-recheck.py — rb-468 family).
from _paths import AGENT_DIR, WORLD_DIR  # noqa: E402
from _fileops import locked_append_jsonl  # noqa: E402

# : never hardcode the escalation aspiration —  is the UPSTREAM
# deployment's queue and does not exist elsewhere, so a literal files nothing.
# (Same resolver block as the sibling self-contained filer,
# reason-less-blocked-check.py.)
try:
    from _escalation_target import resolve as _resolve_asp, source_flag as _asp_source
    ESCALATION_ASP, _ESCALATION_ASP_VIA = _resolve_asp(CORE_ROOT, WORLD_DIR, AGENT_DIR)
    ESCALATION_SOURCE = _asp_source(ESCALATION_ASP, WORLD_DIR, AGENT_DIR)
except Exception:
    ESCALATION_ASP, _ESCALATION_ASP_VIA, ESCALATION_SOURCE = (
        "asp-115", "fallback:import-failed", "world")

# : the open-audit dedup moved from the precheck digest's LLM
# prose into this script (the rb-428 self-contained-apply family — an LLM
# WARN is the exact drift class  scripted away in the sibling
# lane), and it became PER MEMBER: an open audit covers a drifted id only
# if it NAMES it. The fold (any open class-keyed audit suppresses every
# fresh filing) was the 55-day latch behind .
from audit_open_coverage import open_audits, uncovered_ids  # noqa: E402

# The stable filing key. MUST keep the "investigate:" prefix: the
# origin-signal-gate ALLOWED_PREFIXES has no bare "defer-drift-audit" form,
# so an unprefixed key gets Layer-D auto-derive REWRITTEN to
# investigate:<title-slug> at filing time and the exact-match dedup goes
# vacuous (, second instance 2026-07-17  — the value,
# not the flag).
AUDIT_ORIGIN_SIGNAL = "investigate:defer-drift-audit"

# The three STRUCTURED_DEFER_PREFIXES (single source of truth:
# core/scripts/gates/defer_classifier.py). Imported when available so a future
# prefix addition there flows here automatically; literal fallback keeps this
# guard self-contained if the import path shifts.
try:
    from gates.defer_classifier import STRUCTURED_DEFER_PREFIXES
except Exception:
    STRUCTURED_DEFER_PREFIXES = (
        "precondition_unmet:",
        "blocked_on_dependency",
        "Circuit breaker:",
    )

# evaluate_all is optional enrichment — used only to annotate drifted goals
# that carry structured preconditions. A missing import must never break
# detection (the dominant g-304 case is prose-only, no structured pcs).
try:
    from predicate import evaluate_all
except Exception:
    evaluate_all = None


def _tolerant_decode(source, raw):
    """-tolerant decode for the daemon aspirations_read body.
    Thin wrapper around _rt.tolerant_decode_aggregate (guard-383 contract:
    empty -> None, raw_decode recovery, fatal on JSONDecodeError / non
    dict-or-list). Prefixes the script name onto the stderr diagnostic."""
    return _rt.tolerant_decode_aggregate(f"defer-drift-check: {source}", raw)


def _read_goals(source):
    """Read all active goals from world or agent queue via the daemon.

    guard-383 fatal symmetry (rb-987): a per-source read error in an N>=2
    source aggregator MUST be fatal — a silent `return []` writes a
    complete-looking lie into the merged aggregate (drift hidden behind
    "0 drifted"). The single fail-open boundary is the shell wrapper's
    `|| echo WARN`, never inside this aggregator. Mirrors
    precondition-defer-recheck.py / parent-supersession-sweep.py.
    """
    try:
        out = _rt.aspirations_read(source=source, active=True)
    except _rt.RtError as e:
        print(f"[defer-drift-check] {source} read failed: {e.body or e}",
              file=sys.stderr)
        sys.exit(1)  # guard-383: source error fatal
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


def _parse_iso(ts):
    """Tolerant ISO parse (guard-420). Returns datetime or None — never raises.
    Strips a trailing Z (the records are local-time naive per the repo
    convention; a 'Z' would only ever be spurious)."""
    if not ts:
        return None
    try:
        return parse_naive_iso(ts)
    except Exception:
        return None


# YYYY-MM-DD date token — the form defer_reason prose uses for maturity dates
# (e.g. "30-day window completes ~2026-07-11"). Matches a calendar date anywhere
# in the free text.
_PROSE_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def _extract_prose_dates(reason):
    """Extract calendar dates (YYYY-MM-DD) from defer_reason prose.

    Returns a list of date objects; tolerant (guard-420) — tokens that are not
    real calendar dates (e.g. 2026-13-45) are skipped, never raised. Used ONLY
    to distinguish an on-schedule defer expiry (prose maturity date ~=
    deferred_until) from genuine drift (prose date well after deferred_until).
    """
    out = []
    for m in _PROSE_DATE_RE.finditer(reason or ""):
        try:
            out.append(dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except (ValueError, TypeError):
            continue
    return out


def _is_on_schedule_expiry(reason, deferred_until_dt, window_days=1):
    """True when defer_reason prose carries a maturity date within window_days
    of deferred_until — i.e. the defer was set TO its own maturity date and
    simply expired on schedule. This is NOT drift; it is a satisfied
    precondition-defer that the normal deferred_readiness path should surface
    for selection (or that a human clears), NOT a drift Investigate.

    Discriminator (calibrated from the g-115-1541 incident, guard-594 — pick the
    threshold from real data, not intuition):
      - on-schedule FP:  prose '~2026-06-18' vs deferred_until 2026-06-18T13:00
                         -> delta 0 days  (<= window)
      - genuine drift:   prose '~2026-07-11' vs deferred_until 2026-05-26
                         -> delta ~46 days (>> window)
    A 1-day window cleanly separates the two (0 vs 46 days).

    Returns (True, closest_prose_date) when on-schedule, else (False, None).
    """
    if deferred_until_dt is None:
        return (False, None)
    prose_dates = _extract_prose_dates(reason)
    if not prose_dates:
        return (False, None)
    du_date = deferred_until_dt.date()
    closest = min(prose_dates, key=lambda d: abs((d - du_date).days))
    if abs((closest - du_date).days) <= window_days:
        return (True, closest)
    return (False, None)


def _defer_prefix(reason):
    """Return the matched structured-defer prefix, or None."""
    for pfx in STRUCTURED_DEFER_PREFIXES:
        if reason.startswith(pfx):
            return pfx
    return None


def _precondition_status(goal):
    """Annotate a drifted goal's structured-precondition state.

    Returns one of:
      "prose"        — no structured (dict+type) preconditions: the condition
                       lives in free text, unverifiable here. Re-gate by
                       judgment (the canonical g-304-11 shape).
      "ready"        — has structured preconditions AND all pass: the gate is
                       merely stale, the goal is actually ready (clear candidate).
      "still_unmet"  — has structured preconditions, >=1 fails: genuine drift.
      "uncheckable"  — has structured preconditions but evaluate_all unavailable.
    """
    pcs_raw = (goal.get("verification") or {}).get("preconditions") or []
    struct_pcs = [p for p in pcs_raw if isinstance(p, dict) and "type" in p]
    if not struct_pcs:
        return "prose"
    if evaluate_all is None:
        return "uncheckable"
    try:
        results = evaluate_all(struct_pcs, mode="all", include_skippable=True)
    except Exception:
        return "uncheckable"
    return "ready" if all(r.passed for r in results) else "still_unmet"


def _classify_drift(goal, now, min_hours_past=0.0, on_schedule_window_days=1):
    """Pure eligibility test for ONE goal. Returns an entry dict when the goal
    is time-gate-expired (non-terminal + structured-defer prefix +
    deferred_until set, parseable, and >= min_hours_past in the past), else
    None. The entry's "classification" field is:
      "drift"              — genuine drift (deferred_until is stale relative to
                             the defer_reason maturity date, or no prose date is
                             present). main() routes these to drifted[].
      "on_schedule_expiry" — the prose maturity date is within
                             on_schedule_window_days of deferred_until: the
                             defer expired on its OWN schedule (g-115-1541 FP).
                             main() routes these OUT of drifted[] into the
                             on_schedule_expiry[] list, so the precheck files no
                             drift Investigate for them.

    Pure (no I/O, no daemon) so the full eligibility ladder is unit-testable
    with synthetic goals — the daemon read in main() is the only impure part.
    """
    if goal.get("status") not in ("pending", "in-progress"):
        return None
    reason = goal.get("defer_reason") or ""
    prefix = _defer_prefix(reason)
    if prefix is None:
        return None
    du = _parse_iso(goal.get("deferred_until"))
    if du is None:
        # No (or unparseable) deferred_until — NOT time-gated, so this is
        # precondition-defer-recheck's domain, not ours.
        return None
    if du >= now:
        return None  # gate still in the future — working as intended
    hours_past = (now - du).total_seconds() / 3600
    if hours_past < min_hours_past:
        return None
    # On-schedule-expiry discrimination (): a defer whose prose
    # maturity date ~= deferred_until expired on its own schedule — that is a
    # satisfied calendar-defer, NOT drift. Classify it distinctly so main()
    # keeps it OUT of drifted[] (and thus out of the precheck's drift
    # Investigate); the normal deferred_readiness path surfaces it for
    # selection or clearing.
    on_schedule, prose_date = _is_on_schedule_expiry(
        reason, du, on_schedule_window_days)
    entry = {
        "goal_id": goal.get("id"),
        "source": goal.get("_source"),
        "aspiration_id": goal.get("_aspiration_id"),
        "deferred_until": goal.get("deferred_until"),
        "hours_past": round(hours_past, 1),
        "defer_prefix": prefix,
        "precondition_status": _precondition_status(goal),
        "title": (goal.get("title") or "")[:80],
        "classification": "on_schedule_expiry" if on_schedule else "drift",
    }
    if on_schedule and prose_date is not None:
        entry["prose_date"] = prose_date.isoformat()
    return entry


# --- Metrics logging (mirrors precondition-defer-recheck.py — rb-468 family) -
#
# WHY THIS EXISTS (, 2026-08-25). This script was the ONLY member of
# the defer-recheck detective family with no metrics ledger: its three writing
# siblings all append to world/*-metrics.jsonl, while this one emitted to stdout
# and discarded every observation. Hypothesis
# 2026-08-18_drifted-defers-enriched-for-permanent-premises needs n>=8 DISTINCT
# drifted goals accumulated over time to resolve; drift is rare (measured
# scanned=2926 drift_count=0 on 2026-08-25), so the population can only ever be
# built by accumulation. With no writer, n could never leave 0 and the
# hypothesis was unresolvable BY CONSTRUCTION, not by rarity — the failure mode
# it was filed to study.
#
# The record carries drifted[] only (usually empty) plus scalar counts, so a
# ledger written on every precheck stays small: the siblings that log full
# populations are already 1.3 MB.


def _resolve_metrics_log(cli_path):
    """Resolve metrics log path. cli_path=='' -> disabled; None -> default."""
    if cli_path == "":
        return None
    if cli_path is not None:
        return Path(cli_path)
    return Path(WORLD_DIR) / "defer-drift-metrics.jsonl"


def _append_metric(path, record):
    """Append one record to metrics JSONL. Fail-open: a write failure stays
    visible on stderr but never aborts the sweep (this is a detective; losing
    a datapoint must not cost the caller its drift verdict)."""
    if path is None:
        return
    try:
        locked_append_jsonl(str(path), record)
    except Exception as e:
        print(f"[defer-drift-check] WARN: metrics append failed: {e}",
              file=sys.stderr)


def _build_investigate(entries):
    """Build the re-gate audit Investigate naming EXACTLY the uncovered
    drifted goals (the ids no open audit covers — the per-member dedup,
    g-115-11720). Every affected goal id lands in the title AND the
    description (the naming-surface audit_open_coverage reads back next
    run), so the per-id coverage rule can see this audit's own members."""
    lines = []
    for e in entries:
        lines.append(
            f"  - {e['goal_id']} [{e['aspiration_id']}] "
            f"({e['source']}): deferred_until={e['deferred_until']} "
            f"{e['hours_past']}h past | {e['defer_prefix']} | "
            f"pc={e['precondition_status']} | {e['title']}")
    listing = "\n".join(lines)
    ids = " ".join(e["goal_id"] for e in entries)
    # NOTE: no goal ids as PROSE in this text — the title+description IS the
    # naming surface the per-member dedup reads back (audit_open_coverage),
    # so an incidental id mention would accidentally "cover" that id on the
    # next run. This description therefore cites the lane, never a goal.
    description = (
        f"{len(entries)} goal(s) have a PAST deferred_until while a "
        f"structured-defer marker persists (deferred_readiness selector "
        f"pollution; detection: defer-drift-check.py, aspirations-precheck "
        f"Phase 0.5b.10). This audit names exactly the drifted goal(s) no "
        f"OLDER open re-gate audit covers (per-member dedup, g-115-11720): "
        f"do NOT fold a later drift onto this audit unless it names that "
        f"goal — an audit that names none of the current set is stale and "
        f"must not suppress a fresh filing.\n\n"
        f"Drifted goals (most-overdue first):\n{listing}\n\n"
        f"RE-GATE each by judgment (the ~30s fix the detective cannot make: "
        f"the exact future date lives in the defer_reason prose): "
        f"pc=prose -> re-gate deferred_until to the correct future date from "
        f"the defer_reason; pc=ready -> the gate is merely stale, clear the "
        f"defer; pc=still_unmet -> re-gate. Route lane-owned goals to their "
        f"owning agent's vertical rather than appropriating them."
    )
    return {
        "title": (f"Investigate: re-gate {len(entries)} drifted defer(s) "
                  f"{ids}"),
        "description": description,
        "priority": "MEDIUM",
        "participants": ["agent"],
        "category": "framework-architecture",
        "tags": ["defer-drift", "deferred-readiness", "learning-health"],
        "origin_signal": AUDIT_ORIGIN_SIGNAL,
    }


def _file_investigate(aspiration_id, entries):
    """File the re-gate audit Investigate via the daemon. Returns goal_id or
    an <add-goal-failed:...> marker (never raises — the caller surfaces it).

    On a goal_duplication_blocked refusal, retries ONCE with a justified,
    audited X-Mind-Override-Duplication (the same structural-rationale
    retry as the sibling self-contained filer,
    reason-less-blocked-check._file_investigate, g-115-3067): _file_investigate
    is reached ONLY when the per-member dedup found UNCOVERED ids, so any
    dup-gate match is against COMPLETED prior recurring audits — each fresh
    straggler legitimately needs its own audit once the prior one closed.
    """
    record = _build_investigate(entries)
    try:
        result = _rt.aspirations_add_goal(
            aspiration_id, record, source=ESCALATION_SOURCE)
    except _rt.RtError as e:
        body = (e.body or str(e)).strip()
        if "goal_duplication_blocked" not in body:
            return f"<add-goal-failed:{body or 'no detail'}>"
        try:
            result = _rt.aspirations_add_goal(
                aspiration_id, record, source=ESCALATION_SOURCE,
                overrides={"Duplication": (
                    "defer-drift sweep: per-member dedup confirmed the "
                    "filed ids are UNCOVERED by every open audit; dup-gate "
                    "match is COMPLETED prior recurring audits (structural "
                    "FP). g-115-11720")})
        except _rt.RtError as e2:
            return f"<add-goal-failed:{(e2.body or str(e2)).strip() or 'no detail'}>"
    if isinstance(result, dict):
        return result.get("goal_id") or result.get("id") or "<unknown-id>"
    return "<unknown-id>"


def main():
    ap = argparse.ArgumentParser(
        description=("Flag goals whose deferred_until is PAST while a "
                     "structured-defer marker persists (deferred_readiness "
                     "pollution). Detective by default; --apply files ONE "
                     "per-member-deduplicated re-gate Investigate. Sibling: "
                     "precondition-defer-recheck.py (detection), "
                     "reason-less-blocked-check.py (apply family)."),
    )
    ap.add_argument("--apply", action="store_true",
                    help=("File a re-gate Investigate naming the drifted "
                          "goals NO open audit already names (per-member "
                          "dedup, g-115-11720). Default: dry-run (report "
                          "only — the uncovered ids are in the JSON either "
                          "way)."))
    ap.add_argument("--investigate-aspiration", default=ESCALATION_ASP,
                    help=("Aspiration ID the audit Investigate is filed "
                          "under. Default %(default)s (framework hygiene), "
                          "resolved per deployment. Must exist in the "
                          "resolved queue."))
    ap.add_argument("--output", choices=["json", "human"], default="json")
    ap.add_argument("--min-hours-past", type=float, default=0.0,
                    help=("Only flag goals whose deferred_until is at least "
                          "this many hours in the past (default 0 — any past "
                          "gate). Raise to suppress just-elapsed gates that a "
                          "later sweep would legitimately re-evaluate."))
    ap.add_argument("--on-schedule-window-days", type=float, default=1.0,
                    help=("Max |prose maturity date - deferred_until| in days "
                          "for an expired defer to count as an on-schedule "
                          "expiry (g-115-1541 FP) rather than drift (default "
                          "1). Calibrated from the incident: on-schedule delta "
                          "~0d vs genuine drift ~46d."))
    ap.add_argument("--metrics-log", default=None,
                    help=("Path to the drift metrics JSONL. Omit for the "
                          "default (world/defer-drift-metrics.jsonl); pass an "
                          "empty string to disable logging."))
    args = ap.parse_args()
    metrics_path = _resolve_metrics_log(args.metrics_log)

    now = dt.datetime.now()
    all_goals = _read_goals("world") + _read_goals("agent")

    scanned = 0
    drifted = []
    on_schedule = []

    for g in all_goals:
        scanned += 1
        entry = _classify_drift(g, now, args.min_hours_past,
                                args.on_schedule_window_days)
        if entry is None:
            continue
        if entry.get("classification") == "on_schedule_expiry":
            on_schedule.append(entry)
        else:
            drifted.append(entry)

    # Sort most-overdue first — the staler the gate, the longer the pollution
    # has been distorting selection.
    drifted.sort(key=lambda d: d["hours_past"], reverse=True)
    on_schedule.sort(key=lambda d: d["hours_past"], reverse=True)

    # : the dedup is PER MEMBER. An open class-keyed audit covers a
    # drifted id only if it NAMES it (audit_open_coverage) — the old
    # any-open-audit suppression was the 55-day latch (). The SAME
    # active read that found the drift is the audit surface (guard-487).
    drifted_ids = [d["goal_id"] for d in drifted if d.get("goal_id")]
    uncovered = sorted(
        set(uncovered_ids(all_goals, AUDIT_ORIGIN_SIGNAL, drifted_ids)))
    open_audit_goal_ids = sorted(
        a.get("id") for a in open_audits(all_goals, AUDIT_ORIGIN_SIGNAL)
        if a.get("id"))

    result = {
        "scanned": scanned,
        "drift_count": len(drifted),
        "drifted": drifted,
        # On-schedule expiries are NOT drift (): a defer that expired
        # on its own prose maturity date. Surfaced distinctly so the precheck
        # files no drift Investigate for them; deferred_readiness surfaces them
        # for selection / clearing on the normal path.
        "on_schedule_expiry_count": len(on_schedule),
        "on_schedule_expiry": on_schedule,
        #  apply path (computed for dry-run too — it IS the report):
        "uncovered_ids": uncovered,
        "open_audit_goal_ids": open_audit_goal_ids,
        "open_audit_exists": bool(open_audit_goal_ids),
        "investigate_filed": None,
        "actions_taken": "apply" if args.apply else "dry-run",
        "now": now.isoformat(timespec="seconds"),
    }

    if args.apply and uncovered:
        entries = [d for d in drifted if d.get("goal_id") in set(uncovered)]
        filed = _file_investigate(args.investigate_aspiration, entries)
        if str(filed).startswith("<add-goal-failed"):
            # Surface the failure loudly; the sweep re-detects next run
            # (guard-487 fail-open: a missed filing re-detects, so it is safe
            # to leave unfiled — never file a possible duplicate on
            # uncertainty).
            print(f"[defer-drift-check] Investigate filing failed: {filed}",
                  file=sys.stderr)
            result["investigate_filed"] = None
            result["investigate_error"] = filed
        else:
            result["investigate_filed"] = filed

    # Accumulate the observation BEFORE emitting, so a broken stdout consumer
    # cannot cost the ledger a datapoint. Naive-UTC stamp per the fleet clock
    # convention (TZ=UTC enforced in .claude/settings.json) — guard-2613.
    _append_metric(metrics_path, {
        "ts": now.isoformat(timespec="seconds"),
        "agent": os.environ.get("MIND_AGENT") or None,
        "host": socket.gethostname(),
        "scanned": scanned,
        "drift_count": len(drifted),
        "on_schedule_expiry_count": len(on_schedule),
        "min_hours_past": args.min_hours_past,
        "on_schedule_window_days": args.on_schedule_window_days,
        "drifted": [
            {
                "goal_id": d["goal_id"],
                "source": d["source"],
                "defer_prefix": d["defer_prefix"],
                "hours_past": d["hours_past"],
                "precondition_status": d["precondition_status"],
            }
            for d in drifted
        ],
    })

    if args.output == "human":
        print(f"scanned={scanned} drift_count={len(drifted)} "
              f"on_schedule_expiry={len(on_schedule)}")
        if drifted:
            print(f"  uncovered={len(uncovered)} "
                  f"(no open audit names these) | open_audits="
                  f"{','.join(open_audit_goal_ids) or 'none'} | "
                  f"investigate_filed={result['investigate_filed'] or '-'} "
                  f"({result['actions_taken']})")
        for d in drifted:
            print(f"  [drift] {d['goal_id']} ({d['source']}): deferred_until="
                  f"{d['deferred_until']} {d['hours_past']}h past | "
                  f"{d['defer_prefix']} | pc={d['precondition_status']} | "
                  f"{d['title']}")
        for d in on_schedule:
            print(f"  [on-schedule] {d['goal_id']} ({d['source']}): "
                  f"deferred_until={d['deferred_until']} "
                  f"prose={d.get('prose_date', '?')} | {d['title']}")
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
