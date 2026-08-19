#!/usr/bin/env python3
# pyright: strict
r"""Unblock-Parent-Status Sweep (g-250-76, rb-908).

Layer D of the capability-routing-enforcement pattern fires
`capability-gate.py --suggest-unblock` synchronously at defer-time, filing
an `Unblock: <verb> for <parent-id>` goal via cmd_update_goal. When the
parent goal subsequently lands in a terminal non-execution state — skipped
(WRONG LAYER finding), completed (resolved without the Unblock), superseded
(sibling decomposition closed the intent), or archived — the Layer D Unblock
survives as actionable work even though its premise has dissolved.

Canonical incident (rb-908):
    g-250-73 'Unblock: behavior for g-250-69' filed 2026-05-13T09:45:14 from
    capability-gate keyword-match on "behavior". g-250-69 was SKIPPED 72
    seconds later (09:46:25) with WRONG LAYER finding ("bumping
    AspirationalModule weights would violate TestAspirationalModuleInvariants:461;
    fix routes through IntentEngineVerticle.scoreCandidate fallback instead").
    g-250-73 should have been auto-skipped at parent-skip time. No mechanism
    existed to detect the parent transition. Resolution required manual
    inspection during /reflect.

This sweep closes the gap as a companion to the rb-428 family:
    - blocker-recheck.py  — Layer C for participants:[user] blockers
    - defer-recheck.py    — explicit dep-chain narrative defers
    - precondition-defer-recheck.py — structured precondition_unmet
    - monitor-stale-check.py — Monitor proc-NNN goals past current run_dir
    - pending-questions-sweep.py — source_goal-completed lifecycle
    - parent-supersession-sweep.py — sibling decomposition supersession
    - unblock-parent-status-sweep.py — THIS sweep (Layer D parent terminal)

Heuristic (conservative for v1 — title-anchored):
    For each candidate Unblock goal U:
        - title starts with "Unblock:" (Layer D canonical title format)
        - status in (pending, in-progress)
        - parent_id parseable from one of three signals (priority order):
            1. origin_signal == "unblock:<g-id>"  (Layer D capability-gate
               emits this exact form via _build_suggestion in capability-gate.py)
            2. title regex r"\bfor\s+(g-\d+-\d+)\b"  (Layer D title shape:
               "Unblock: <verb> for <parent-id>")
            3. discovered_by field == "<g-id>"  (legacy/manual unblock) —
               ADDITIONALLY guarded by _provenance_fp_guard (rb-3887,
               g-115-2534): sweeps ONLY when the Unblock's created_at
               PREDATES the parent's completion. discovered_by is also the
               sq-013 PROVENANCE field (the completed goal whose audit
               discovered the work); an Unblock created after its parent
               completed cannot be waiting on it, and sweeping it auto-skips
               brand-new work (proven FP: g-115-2530/2531).
        - age (created_at OR defer_reason_set_at) >= --max-age-hours
          (default 0 — fire immediately; tunable for testing)
    Parent status lookup (guard-1890 — resolve against the ARCHIVE, not just
    the active queues):
        - Read parent across world + agent ACTIVE queues.
        - If absent there, read world + agent ARCHIVED aspirations. Being
          archived IS the supersession signal, so a parent found there is
          treated as terminal regardless of the status it froze with (its
          aspiration is closed; nobody is advancing it).
        - If absent from BOTH: a dangling reference. NOT swept — we cannot
          confirm the parent ever reached a terminal state, and skipping a
          live Unblock on an unresolvable id is the expensive direction.
        - If the archive read DEGRADED, absence is ambiguous (archived vs.
          unreadable), so we fall back to the pre-guard-1890 behaviour and
          skip. Reported as `archive_degraded` so a dangling list is never
          read as authoritative.
    Parent in terminal state set: TERMINAL_STATES (below), or found in the
    archive. If either holds, U is a sweep candidate -- subject to TWO
    final guards on the shared path to the mark:
        - _successor_marker_guard (g-115-6252 + g-115-6223, unified): a
          candidate DECLARING itself a successor / residual scope is never
          swept. For that class the terminal parent is the goal's
          PRECONDITION, not its discharge -- the sweep's core premise
          inverts. A second, INDEPENDENT guard rather than a tuning of
          _provenance_fp_guard, which reads this exact shape backwards
          (it clears the sweep more confidently the longer the parent took
          to close). Aliased as _successor_scope_guard.
        - _provenance_fp_guard (rb-3887/g-115-2674/g-115-2681): the
          timestamp guard described at priority 3 above.

Action modes:
    --report (default): print JSON, no mutation
    --apply: mark each candidate as status=skipped with outcome_note
        "parent resolved without action needed (parent_id=<X>, parent.status=<Y>)"
        Idempotent: if outcome_note already starts with "parent resolved
        without action needed", skip the rewrite.

Single-writer / fail-quiet (rb-428 family):
    - One Python pass over active queues per call
    - Metric writes use locked_append_jsonl — fail-open on metric error
    - Update failures log but do not abort the sweep
    - Always exits 0 (reporting tool)

Exit: 0 always. JSON result:
    {
      "scanned": N,        # total Unblock:-titled goals examined
      "eligible": N,       # passed age + parent_id parse filters
      "candidates": [...], # parent in terminal state (recommend skip)
      "applied": N,        # Unblocks marked skipped (apply mode only)
      "details": [...],    # per-goal trace incl. parent status
    }

CLI:
    unblock-parent-status-sweep.py [--max-age-hours N] [--apply]
                                   [--output json|human]
                                   [--metrics-log PATH | --metrics-log ""]
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

from _paths import WORLD_DIR  # noqa: E402
from _fileops import locked_append_jsonl  # noqa: E402

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from _dt import parse_naive_iso  # noqa: E402  (shared tzinfo-stripping naive-ISO parse, )
import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)


# Title must start with literal "Unblock:" — Layer D canonical
UNBLOCK_TITLE_PATTERN = re.compile(r"^\s*Unblock\s*:", re.IGNORECASE)
# `(?:-[a-z])?` on the ANCHORED patterns below matches the SSOT (aspirations.py
# GOAL_ID_RE) — . Every one of them feeds a PARENT-STATUS LOOKUP, so
# a shape narrower than the SSOT does not fail loudly: the anchored patterns
# silently decline a suffixed id and hand it to the unanchored fallbacks, which
# then truncate it to a parent that may not exist and answer a status question
# about the wrong record. Keep them in lockstep with the SSOT — the sibling
# sweep (routing-audit-target-status-sweep.py) carries the same fix.
# GOAL_ID_EMBEDDED_PATTERN is deliberately EXEMPT: its slug-context truncation
# is a documented choice (see its comment), and rules 4-5 refuse on ambiguity
# rather than guessing, so the suffix question routes to the anchored rules.
# origin_signal: exact form emitted by capability-gate._build_suggestion
ORIGIN_SIGNAL_PATTERN = re.compile(r"^unblock:(g-\d+-\d+(?:-[a-z])?)\s*$")
# Title pattern: "Unblock: <verb> for <parent-id>"
TITLE_FOR_PATTERN = re.compile(r"\bfor\s+(g-\d+-\d+(?:-[a-z])?)\b")
# Bare goal-id check
GOAL_ID_PATTERN = re.compile(r"^g-\d+-\d+(?:-[a-z])?$")
# Embedded goal-id scan, for the  fallback rules 4-5. Unanchored on
# purpose. Note it stops at the first non-digit, so a slugified possessive
# ("...--s-user-leg-...") yields  and not -s.
GOAL_ID_EMBEDDED_PATTERN = re.compile(r"g-\d+-\d+")
# Agent-qualified starvation key: "unblock:recurring-starved-<owner>-<goal-id>"
# (recurring-starvation-check._origin_signal, agent-source form).
AGENT_QUALIFIED_STARVED_PATTERN = re.compile(
    r"^unblock:recurring-starved-([a-z][a-z0-9-]*?)-(g-\d+-\d+(?:-[a-z])?)\s*$")

# Parent states that imply "no Unblock action is needed"
#  (zeta allowlist audit D1): synced to the SSOT
# aspirations.TERMINAL_GOAL_STATUSES. This is the mirror-source the two
# insight-trigger scripts reference; it carried the identical drift (missing
# expired+decomposed, bogus archived). Parity enforced by
# tests/test_terminal_goal_states_parity.py.
TERMINAL_STATES = {"completed", "skipped", "expired", "decomposed", "superseded"}

#  — recurring parents are gated on cadence freshness, not status.
# MIRRORS recurring-starvation-check.DEFAULT_MULTIPLIER, which is the SSOT: it
# is the multiplier that FILED these starvation Unblocks, so resolving them at
# the same value makes this sweep the exact negation of that detector. Kept as
# a local literal rather than imported because that module is a hyphenated
# script requiring importlib gymnastics, and a stale copy here fails visibly
# (Unblocks resolve at the wrong cadence) rather than silently. If you change
# it there, change it here.
STARVATION_MULTIPLIER = 3.0

#  — close-sequence tolerance for the _provenance_fp_guard.
#
# The  guard tested `created < parent_completed` and called ANY
# earlier creation a "genuine wait". That boundary is wrong at the margin: an
# Unblock filed DURING its parent's close sequence (Phase 4 surfaces a finding
# -> the agent files the follow-up -> verify/state-update/learning-gate then
# stamp the parent terminal) is created SECONDS-to-MINUTES *before* the parent
# completes, and is a FOLLOW-UP, not a wait. Measured FPs, all re-swept under
# the bare test:  (28s lead),  (93s),  (97s) — each
# description literally opens "MEASURED during <parent>", i.e. the parent's
# completion is their PRECONDITION, not what moots them.
#
# Direction of the asymmetry (rb-4149): a wrongly-swept goal leaves BOTH the
# candidate list and the blocked list, so it is invisible; a wrongly-KEPT goal
# just stays pending and visible. Guard generously.
#
# 900s is ~an order of magnitude above the observed leads while staying far
# below the genuine-wait population (a real Layer-D defer-time Unblock is filed
# while the parent is still PENDING — typically hours to days ahead), so the
# sweep's reach is preserved. Tunable for a domain with slower closes.
CLOSE_SEQUENCE_WINDOW_S = int(os.environ.get("UNBLOCK_CLOSE_WINDOW_S", "900"))


def _resolve_metrics_log(cli_path):
    """Resolve metrics log path. Mirrors parent-supersession-sweep convention."""
    if cli_path == "":
        return None
    if cli_path is not None:
        return Path(cli_path)
    return Path(WORLD_DIR) / "unblock-parent-status-sweep-metrics.jsonl"


def _append_metric(path, record):
    """Append one metric record. Fail-open by design."""
    if path is None:
        return
    try:
        locked_append_jsonl(str(path), record)
    except Exception as e:
        print(f"[unblock-parent-status-sweep] WARN: metrics append failed: {e}",
              file=sys.stderr)


def _run(argv, input_text=None):
    result = subprocess.run(argv, input=input_text, capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    return result.returncode, result.stdout, result.stderr


def _py(args, input_text=None):
    return _run([sys.executable] + args, input_text=input_text)


def _tolerant_decode(source, raw):
    """-tolerant decode for daemon aspirations_read body.

    Mirrors consolidation-health.py::_tolerant_decode (lines 112-172,
    landed via g-115-796) and parent-supersession-sweep::_tolerant_decode
    (A4), adapted for unblock-parent-status-sweep's expected aggregate
    shapes: dict with "aspirations" key OR bare list.

    Contract per g-115-797-A5 / guard-383 / rb-774:
      - Empty / whitespace-only body: return None (caller maps to []).
      - Valid JSON (list OR dict with "aspirations" key): return as-is.
      - Valid prefix + trailing garbage (g-115-766 shape): raw_decode
        returns the prefix; recovery is NOT a source error.
      - JSONDecodeError: ONE stderr diagnostic + sys.exit(1).
      - Non-dict-and-non-list aggregate (e.g. string, number): stderr +
        sys.exit(1).
    guard-383 mandates source errors are FATAL for the N>=2 aggregator
    pattern (_read_aspirations is called once for "world" and once for
    "agent" then merged at line 263-264); returning [] on corruption
    would poison the merged aggregate with a complete-looking lie.
    """
    stripped = (raw or "").lstrip()
    if not stripped:
        return None  # genuinely empty queue — valid state, not source error
    try:
        obj, _consumed = json.JSONDecoder().raw_decode(stripped)
    except json.JSONDecodeError as exc:
        body_prefix = stripped[:120].replace("\n", "\\n")
        print(
            f"unblock-parent-status-sweep: {source} JSONDecodeError ({exc}); "
            f"body prefix: {body_prefix!r}",
            file=sys.stderr,
        )
        sys.exit(1)  # guard-383: source error is fatal
    if not isinstance(obj, (dict, list)):
        body_prefix = stripped[:120].replace("\n", "\\n")
        print(
            f"unblock-parent-status-sweep: {source} non-dict-and-non-list "
            f"aggregate (type={type(obj).__name__}); body prefix: {body_prefix!r}",
            file=sys.stderr,
        )
        sys.exit(1)  # guard-383: corrupt aggregate shape
    return obj


def _read_aspirations(source):
    """Return list of (aspiration_dict, source_str) tuples.

    Uses _rt.aspirations_read (daemon client) — the aspirations.py read CLI
    was deleted in the 2026-05-14 cutover. Parse path is g-115-766-tolerant
    via _tolerant_decode — see that helper for the contract.
    """
    try:
        raw = _rt.aspirations_read(source=source, active=True)
    except _rt.RtError as e:
        # guard-383: source error is FATAL for the N>=2-source aggregator
        # pattern (lines 263-264 merge "world" + "agent"). A silent [] would
        # poison the merged aggregate with a complete-looking lie.
        print(f"unblock-parent-status-sweep: {source} read failed: {e.body or e}",
              file=sys.stderr)
        sys.exit(1)
    data = _tolerant_decode(source, raw)
    if data is None:
        return []
    aspirations = data.get("aspirations") if isinstance(data, dict) else data
    return [(asp, source) for asp in (aspirations or [])]


def _read_archived_aspirations(source):
    """Goals inside ARCHIVED aspirations for one source (guard-1890).

    Returns a list of (aspiration, source) tuples, or None when the read
    FAILED (degraded).

    DEGRADES, never fatal — and the asymmetry with _read_aspirations above is
    deliberate, mirroring the sibling sweeps (dependency-cycle-check.py,
    blocked-signal-resolution-check.py). Losing the ARCHIVE falls back to
    exactly the pre-guard-1890 behaviour (an archived parent reads as
    unresolvable, so the Unblock is left alone) — a false-NEGATIVE direction:
    the sweep does less, visibly. Losing an ACTIVE source instead would make a
    still-pending parent look absent and could sweep a live Unblock, so that
    path stays guard-383 fatal.

    An EMPTY archive is a valid state (fresh world), NOT a failure — returning
    None there would make every clean world look degraded.
    """
    try:
        raw = _rt.aspirations_read(source=source, archive=True)
    except _rt.RtError as e:
        print(f"unblock-parent-status-sweep: {source} archive read failed "
              f"(degrading): {e.body or e}", file=sys.stderr)
        return None
    data = _tolerant_decode(f"{source} archive", raw)
    if data is None:
        return None
    aspirations = data.get("aspirations") if isinstance(data, dict) else data
    return [(asp, source) for asp in (aspirations or [])]


def _build_archived_id_set(archived_aspirations):
    """Return {goal_id} for every goal living in an ARCHIVED aspiration.

    A SET, not a status map, on purpose: the parent's own frozen status is not
    the signal here — its ASPIRATION being archived is. A goal that froze as
    `pending` inside a retired aspiration is just as dead as one that froze as
    `completed`, so keying on membership avoids re-deriving the terminal
    question from a status that stopped being maintained.
    """
    ids = set()
    for asp, _src in archived_aspirations:
        for g in (asp.get("goals") or []):
            gid = g.get("id")
            if gid:
                ids.add(gid)
    return ids


def _age_hours(ts):
    if not ts:
        return None
    try:
        t = parse_naive_iso(ts)
        return (dt.datetime.now() - t).total_seconds() / 3600
    except Exception:
        return None


def _is_unblock_goal(g):
    return bool(UNBLOCK_TITLE_PATTERN.match(g.get("title", "") or ""))


def _foreign_agent_owner(origin_signal):
    """Owner name if this starvation key names ANOTHER agent, else None.

    THE g-001-NN ID SPACE IS NOT UNIQUE ACROSS THE FLEET. Per-agent asp-001
    queues reuse it: every agent has its own `g-001-02`, so that id names five
    different goals fleet-wide. recurring-starvation-check._origin_signal
    qualifies agent-source keys precisely for this reason
    ("unblock:recurring-starved-<owner>-<goal-id>"); world-source keys are left
    bare because world ids ARE globally unique.

    This guard exists because the new fallback rules would otherwise strip that
    qualifier away and hand back a bare `g-001-02`, and BOTH downstream
    outcomes are wrong: `_build_status_index` covers world + THIS agent's
    queues, so a foreign id either resolves against OUR identically-numbered
    goal (wrong status, silently) or is absent — and absence defaults to
    "archived", which is IN TERMINAL_STATES, so the sweep would mark the
    Unblock resolved on the strength of a goal it never read. The failure is
    worse than the gap it closes: not-parsing costs one un-swept Unblock,
    mis-parsing closes a live one against a stranger's status.

    Not reachable via rules 1-3 (none of them parse this shape), so this only
    ever gates the g-115-5647 fallbacks.
    """
    m = AGENT_QUALIFIED_STARVED_PATTERN.match(origin_signal or "")
    if not m:
        return None
    owner = m.group(1)
    me = (os.environ.get("MIND_AGENT") or "").strip()
    # No binding => cannot prove the key is ours, so treat it as foreign. The
    # fail-safe direction: skip rather than resolve against an unknown queue.
    return None if (me and owner == me) else owner


def _sole_goal_id(text):
    """The one goal-id in `text`, or None if there are zero or several.

    SKIP RATHER THAN GUESS (g-115-5647). Zero and ambiguous both return None,
    deliberately: an Unblock reading "... blocked by g-A, needed for g-B" has
    two referents and no way to rank them here, and picking either would sweep
    a goal against the wrong parent's status. The `for` anchor above is what
    disambiguates that case; when it is absent and the text carries more than
    one id, the honest answer is "unparseable", which the caller already
    reports.

    Measured on the live corpus at fix time: 0 of 20 non-terminal Unblocks had
    a multi-id title, so this guard costs nothing today. It is here for the
    corpus this rule will meet later — guard-2201, do not widen a matcher over
    a live corpus without bounding what it admits.
    """
    ids = set(GOAL_ID_EMBEDDED_PATTERN.findall(text or ""))
    return ids.pop() if len(ids) == 1 else None


def _parse_parent_id(g):
    """Extract parent goal-id from Layer D shape. Returns goal-id or None.

    Priority order (matches Layer D capability-gate emission):
      1. origin_signal "unblock:<g-id>" — exact form
      2. Title "Unblock: <verb> for <g-id>" — Layer D canonical title
      3. discovered_by "<g-id>" — legacy/manual unblock. NOTE: the main
         loop additionally applies _provenance_fp_guard to priority-3
         links (rb-3887) — this parser stays permissive by design.
      4. origin_signal carrying exactly ONE embedded id (g-115-5647)
      5. Title carrying exactly ONE id, no `for` anchor (g-115-5647)

    RULES 4-5 ARE APPENDED BELOW THE ORIGINAL THREE, NOT MERGED INTO THEM, so
    every previously-parseable goal resolves through exactly the same rule as
    before and this change can only ADD recoveries.

    WHY THEY WERE NEEDED. Rule 1 requires the id to FOLLOW "unblock:" AND be
    end-anchored; rule 2 requires the literal word "for". The framework's own
    filers emit neither shape. origin_signal is auto-derived from the title
    slug, and recurring-starvation-check._origin_signal emits
    "unblock:recurring-starved-<goal-id>" (or "...-<owner>-<goal-id>" for
    agent-source), where the id is at the END but not immediately after the
    prefix — rejected by rule 1 on the prefix requirement, and by rule 2 for
    want of a "for". Two components each working as designed, structurally
    unable to talk to each other (guard-1802 narrow-predicate class).

    RE-DERIVED baseline on this box, which DIFFERS from the goal record and is
    recorded rather than inherited (guard-1835): 20 non-terminal Unblocks, of
    which 3 parsed — all three via rule 3 (discovered_by), none via 1 or 2. The
    goal's headline of "0 of 21" does not reproduce; the actionable gap does:
    10 recoverable single-id goals, 0 ambiguous.
    """
    os_ = (g.get("origin_signal") or "").strip()
    m = ORIGIN_SIGNAL_PATTERN.match(os_)
    if m:
        return m.group(1)
    title = g.get("title") or ""
    mm = TITLE_FOR_PATTERN.search(title)
    if mm:
        return mm.group(1)
    db = (g.get("discovered_by") or "").strip()
    if GOAL_ID_PATTERN.match(db):
        return db
    if _foreign_agent_owner(os_):
        # Cross-agent id-space collision — resolving this locally would be
        # WORSE than not resolving it. See _foreign_agent_owner.
        return None
    return _sole_goal_id(os_) or _sole_goal_id(title)


def _build_status_index(all_aspirations):
    """Return {goal_id: status} across world + agent active queues."""
    idx = {}
    for asp, _src in all_aspirations:
        for g in (asp.get("goals") or []):
            gid = g.get("id")
            if gid:
                idx[gid] = g.get("status")
    return idx


def _build_recurrence_index(all_aspirations):
    """{goal_id: (interval_hours, lastAchievedAt)} for RECURRING goals only.

    Non-recurring goals are omitted entirely, so membership in this index IS
    the "is the parent recurring" test at the call site.
    """
    idx = {}
    for asp, _src in all_aspirations:
        for g in (asp.get("goals") or []):
            gid = g.get("id")
            if gid and g.get("recurring"):
                idx[gid] = (g.get("interval_hours"),
                            g.get("lastAchievedAt"))
    return idx


def _recurring_parent_resolved(interval_hours, last_achieved):
    """(resolved: bool|None, reason: str) for a RECURRING parent.

    STATUS IS THE WRONG PREDICATE FOR A RECURRING PARENT (g-115-5647). A
    recurring goal cycles pending -> completed -> pending, sitting at
    "completed" only between a fire and aspirations-recover-recurring flipping
    it back. So `status in TERMINAL_STATES` samples a TRANSIENT state: it is
    true for a few minutes per cycle and false the rest of the time, and which
    one the sweep sees is down to when it happened to run.

    For a starvation Unblock ("recurring goal g-X has stopped firing") the
    status test can still come out right, but for the wrong reason, and it
    would come out WRONG for any other Unblock about a recurring parent. The
    question that actually matters is whether the cadence has RESUMED.

    THRESHOLD IS BORROWED, NOT INVENTED. recurring-starvation-check.py filed
    these Unblocks at `age_h > DEFAULT_MULTIPLIER * basis`, so the honest
    resolution predicate is the negation of the filing predicate at the same
    multiplier: resolved once the parent is no longer starved by the rule that
    declared it starved. A stricter threshold would strand Unblocks the
    detector would no longer file; a looser one would close Unblocks it still
    would.

    Returns None (undecidable) rather than a guess when the fields needed are
    missing or unparseable — the caller reports and skips, which is the
    fail-safe direction for a write that closes a goal.
    """
    try:
        interval = float(interval_hours)
    except (TypeError, ValueError):
        return None, "recurring parent has no usable interval_hours"
    if interval <= 0:
        return None, f"recurring parent interval_hours={interval_hours!r} is not positive"
    age_h = _age_hours(last_achieved)
    if age_h is None:
        return None, "recurring parent has no parseable lastAchievedAt"
    limit = STARVATION_MULTIPLIER * interval
    if age_h <= limit:
        return True, (f"recurring cadence has resumed "
                      f"(last fired {age_h:.1f}h ago, within "
                      f"{STARVATION_MULTIPLIER}x{interval:.1f}h={limit:.1f}h)")
    return False, (f"recurring parent still starved "
                   f"(last fired {age_h:.1f}h ago = {age_h / interval:.2f}x "
                   f"its {interval:.1f}h cadence)")


def _parse_ts(ts):
    """ISO timestamp/date string → NAIVE-LOCAL datetime, or None on missing/
    unparseable. Offset-aware stamps (e.g. +00:00 from a UTC-stamping box —
    the fleet is TZ-split, rb-3741) are converted to local then stripped
    naive (guard-982 pattern); without this, one aware stamp meeting a naive
    one at the `created < done` comparison raises TypeError and crashes the
    whole sweep."""
    if not ts:
        return None
    try:
        parsed = parse_naive_iso(ts)
    except Exception:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _build_completed_ts_index(all_aspirations):
    """Return {goal_id: completed_at-or-completed_date-or-None} across queues.

    Companion index to _build_status_index (kept separate so that function's
    contract and tests stay untouched). completed_at (datetime) preferred;
    completed_date (bare date → parses as midnight) is the fallback, which
    biases the rb-3887 guard toward NOT sweeping same-day cases — the safe
    direction.
    """
    idx = {}
    for asp, _src in all_aspirations:
        for g in (asp.get("goals") or []):
            gid = g.get("id")
            if gid:
                idx[gid] = g.get("completed_at") or g.get("completed_date")
    return idx


def _provenance_only_parent(g, parent_id):
    """True when parent_id came SOLELY from discovered_by (priority 3) —
    neither the origin_signal exact form nor the title 'for <g-id>' form
    matched. Mirrors _parse_parent_id's priority order."""
    if ORIGIN_SIGNAL_PATTERN.match((g.get("origin_signal") or "").strip()):
        return False
    if TITLE_FOR_PATTERN.search(g.get("title") or ""):
        return False
    return (g.get("discovered_by") or "").strip() == parent_id


def _provenance_fp_guard(g, parent_id, completed_ts_idx):
    """rb-3887 FP guard for created-after-parent-completion parent links.

    discovered_by carries TWO incompatible meanings: (1) legacy/manual
    unblock — the goal WAITS on that parent; (2) provenance — the completed
    goal whose audit DISCOVERED this work (the sq-013 work-discovery shape).
    An Unblock created AT/AFTER its parent's completion cannot have been
    waiting on it — that is shape (2), and sweeping it auto-skips brand-new
    live work (proven: g-115-2530/2531 auto-skipped within one iteration).

    Returns a skip-reason string when the link is provenance (or when the
    timestamps cannot PROVE a genuine wait — missing/unparseable stamps are
    guarded too: the FP direction kills live work, the miss direction is
    benign and other sweeps still cover it). Returns None when sweeping may
    proceed.

    g-115-2674 (2026-07-19) — TWO-TIER guard. The PROVEN test (both stamps
    present AND created >= done) now applies at ALL link priorities; the
    CONSERVATIVE test (stamps missing/unparseable) stays priority-3-only,
    exactly as before.

    Why the proven test had to widen: priority-1/2 links were fully exempt on
    the premise that "Layer D emits those at defer time by construction".
    That premise is FALSE for a hand-filed follow-up Unblock —
    `origin_signal: unblock:<parent>` is also the DOCUMENTED convention for
    filing one by hand, so the field cannot distinguish a Layer-D
    auto-conversion from an agent-authored follow-up. The hole fired: 7 live
    goals auto-skipped fleet-wide, incl. two HIGH goals killed within minutes
    of filing (g-312-09, g-318-63) and a HIGH heartbeat-writer fix
    (g-115-2182) dead 5 days.

    Why the conservative test did NOT widen: a parent whose terminal status
    is `skipped` carries no completion stamp at all, so widening the
    missing-stamp branch would guard nearly every skipped-parent pair and
    silently reduce the sweep to a near-no-op. That is a real cost with no
    matching benefit — the observed incident is fully caught by the PROVEN
    branch (g-312-08 completed_date 2026-07-19 -> midnight; child created
    08:37 -> created >= done -> guarded). Keeping the conservative branch
    scoped preserves the sweep's existing reach.

    Net: genuine Layer-D goals are unaffected (filed at defer time while the
    parent is still pending, so created < done and this returns None).
    """
    created = _parse_ts(g.get("created_at"))
    done = _parse_ts(completed_ts_idx.get(parent_id))
    if created is not None and done is not None:
        lead = (done - created).total_seconds()
        if lead > CLOSE_SEQUENCE_WINDOW_S:
            return None  # genuine wait: Unblock long predates parent completion
        if lead > 0:
            why = (f"goal created {created.isoformat()} only {lead:.0f}s before "
                   f"parent completion {done.isoformat()} — inside the "
                   f"{CLOSE_SEQUENCE_WINDOW_S}s close-sequence window")
        else:
            why = (f"goal created {created.isoformat()} at/after parent "
                   f"completion {done.isoformat()}")
    else:
        # Stamps cannot PROVE anything. Conservative guard stays scoped to
        # priority-3 (discovered_by-only) links — see docstring.
        if not _provenance_only_parent(g, parent_id):
            return None
        why = "created_at/parent-completion timestamp missing or unparseable"
    return (f"parent link is provenance, not a wait ({why}) — "
            f"rb-3887 created-after-parent-completion FP guard "
            f"(all link priorities since g-115-2674)")


# UNIFIED 2026-08-14 (cc-08 merge resolution).  and  are
# the SAME guard, discovered independently on two Bodies from the SAME live
# instance () within hours. Git conflicted on the two function bodies
# -- the LUCKY case, because it announced itself -- while AUTO-MERGING their two
# call sites without complaint, so the merged sweep called both. That silent
# half is the damage; the conflict is not.
#
# The union takes each side's stronger half:
#   from : the [Cc][Aa][Ss][Ee] spelling (local's lowercase-only
#     `case` missed `Case A` / `CASE A`), the uppercase-A discriminator, and
#     the measured rationale below.
#   from : description/title/origin_signal coverage (the marker half
#     read description alone) and the residual/remainder/delivers-only tokens.
#
# NO GLOBAL re.IGNORECASE -- it would make the `A` case-insensitive and pull in
# ordinary English ("in which case a", "worst case a"). First letters are
# spelled as explicit classes instead.
#
# TRADE-OFF INTRODUCED BY THIS MERGE, stated rather than left to be found:
# `[Rr]emainder` subsumed `[Uu]nfinished remainder`, so the latter's
# independent-pin property (deleting the token turns a test red) was weakened --
# it passed via the broader branch.
#
# THAT BARE `[Rr]emainder` BRANCH WAS RETIRED 2026-08-15 (, alpha,
# hostname cc-07). It was 3 for 3 FALSE POSITIVES on every live instance
# hand-checked, and matched ZERO genuine successors -- both real ones match on
# `[Cc]ase A`. "Remainder" is ordinary English and the matches were ordinary
# English, in prose about something else entirely:
#     "...will read fresh for the entire REMAINDER OF THE DAY..."
#     "...25 -> 1, the REMAINDER being cross-world-inject-goal.sh:109..."
#     "...whose REMAINDER is carried by successor ..."  <- about
#               a DIFFERENT goal, named only to explain what was NOT filed alongside
#
# RESIDUAL, MEASURED AND KEPT ON PURPOSE: retiring the token cleared the first
# two and NOT the third --  still flags, on `[Ss]uccessor`, because the
# same sentence names another goal's successor. So the failure mode is the TOKEN
# CLASS (ordinary prose about other goals), not one token, and `[Ss]uccessor` has
# it too. That branch stays: on the live NON-TERMINAL population it flags 1 of 30
# and that one is genuine, and the KNOWN LIMIT below needs it for reducer-filed
# successors that carry no case letter.  is `skipped`, so the sweep
# never scans it and the FP is inert THERE -- but see the second-consumer warning
# in the docstring: it was NOT inert for a restoration pass reading the same
# predicate.
#
# WHY THIS IS NOT MERELY COSMETIC, and why the asymmetry argument below does not
# excuse it: a false positive here makes an ORDINARY Unblock permanently
# undischargeable, which is exactly the  direction (the same predicate
# UNDER-discharging). 's own description predicted it in as many words
# -- "a fix that only narrows it makes  worse" -- and one token did.
# Every other token survives, so both hand-verified successors stay protected;
# `[Uu]nfinished remainder` becomes load-bearing again, which is what the pin
# above was always documenting.
SUCCESSOR_MARKER_PATTERN = re.compile(
    r"\b[Cc][Aa][Ss][Ee]\s+A\b"
    r"|\b[Ss]uccessor\b"
    r"|\b[Uu]nfinished remainder\b"
    # Narrowed replacement for the retired bare token, NOT a restoration of it.
    # 's suite pins "the remainder of the parent's outcome", and that
    # intent is right -- the successor sense of "remainder" is always OF
    # something (the parent, or the parent's goal id). The ordinary-English
    # sense that produced all three false positives never is: "remainder of the
    # DAY", "the remainder BEING <path>". Requiring the object is what separates
    # them, and it is the whole difference.
    r"|\b[Rr]emainder of (?:the parent|g-\d)"
    r"|[Ss]anctioned scope"
    r"|\b[Rr]esidual\b"
    r"|[Dd]elivers only"
)

# Checked in this order; first hit wins. description leads because the filing
# conventions (worker-loop case letter, "successor preserving ...") land there.
# origin_signal is included but outcome_note deliberately is NOT -- this sweep
# WRITES outcome_note itself, so matching it would key the guard on partly
# self-authored text.
SUCCESSOR_MARKER_FIELDS = ("description", "title", "origin_signal")


def _successor_marker_guard(g):
    """ + . A SUCCESSOR's parent being terminal is why it EXISTS.

    The whole sweep rests on one premise: the Unblock waits ON the parent, so a
    terminal parent makes it moot. For a SUCCESSOR goal that premise inverts --
    a successor is filed precisely BECAUSE the parent completed while leaving
    residual scope, so `parent.status=completed` is its PRECONDITION, not its
    discharge. Sweeping it kills live work and writes an outcome_note that
    contradicts the goal's own description.

    MEASURED INSTANCE (2026-08-14, alpha): g-350-215 carries the verbatim
    description "case A - successor preserving g-350-202 sanctioned scope" and
    the verbatim sweep note "parent resolved without action needed
    (parent_id=g-350-202, parent.status=completed)". Real product work. It was
    killed 10 minutes after its parent closed, and re-killed 162s after a
    manual re-open.

    WHY `_provenance_fp_guard` CANNOT CATCH THIS, which is the part worth
    reading before touching either guard. That guard asks whether the Unblock
    was created AT/AFTER the parent's completion; a successor filed at close
    time is created BEFORE it, because filing the successor is part of closing
    the parent and closing takes hours. Measured on the instance: successor
    created 03:35:05, parent completed 07:40:03 -- lead 14,698s against
    CLOSE_SEQUENCE_WINDOW_S=900, so that guard returns None with the reason
    "genuine wait: Unblock long predates parent completion". The failure is not
    that the temporal guard misses this case; it is that the guard reads it
    BACKWARDS, and grows MORE confident the more thorough the parent's
    execution was. Widening CLOSE_SEQUENCE_WINDOW_S cannot fix that without
    guarding nearly every pair -- the two signals are genuinely independent, so
    this is a second guard rather than a tuning of the first.

    WHY NOT `parent_id`, the obvious structural answer and the filing goal's own
    suggested remedy: measured 0 of 2072 non-terminal goals fleet-wide carry a
    populated `parent_id`. The field is never written, so a predicate built on
    it would never fire. (guard-1719 -- a goal's diagnosis and its prescribed
    remedy carry different evidentiary weight; the diagnosis was exact and the
    remedy named a field that does not exist.)

    THE ASYMMETRY INVERTS guard-1923's usual advice, deliberately. Elsewhere a
    broad token is dangerous because a hit causes ACTION; here a hit causes
    INACTION (the sweep declines), so breadth is the SAFE direction and a false
    NEGATIVE is what kills work. Breadth is still bounded, because a marker
    matching everything would silently reduce the sweep to a no-op.

    ** THAT ASYMMETRY IS A PROPERTY OF THIS CONSUMER, NOT OF THE PREDICATE, AND
    THE NEXT CONSUMER INVERTS IT BACK. ** Measured 2026-08-15 (g-115-6252): a
    RESTORATION pass -- "find goals this sweep falsely discharged and re-open
    them" -- reads this same predicate, and there a hit causes ACTION. Run over
    the 7 goals carrying the sweep's discharge note it flagged 2, and one
    (g-115-6232) was a false positive that would have re-opened a correctly
    discharged goal. Hand-verification caught it; the predicate did not, and the
    safety argument above reads as blanket reassurance right up to the moment it
    stops applying. Any second consumer of SUCCESSOR_MARKER_PATTERN must
    hand-verify hits before acting on them.

    RE-MEASURED 2026-08-15 across all three fields, closing the un-re-measured
    note this paragraph used to carry: the title/origin_signal extension adds
    ZERO flags on this population (description-alone and all-three both returned
    4 of 30 before the `[Rr]emainder` retirement). After it: 2 of 30 flagged,
    28 stay sweepable, and both flagged goals are the genuine hand-verified
    successors (g-350-215, g-115-6161) -- so the retirement removed only false
    positives and cost no coverage. Population is 30, not the 32 cited before.

    THE UPPERCASE `A` IS LOAD-BEARING -- do not "tidy" this to IGNORECASE. The
    ruling's case letter is always uppercase; the English phrase is always
    lowercase ("in which case a...", "worst case a...", "special-case a single
    filename"). Case-insensitive matching pulled 7 such English false positives
    into a 45-hit whole-population set. "case" itself varies (case/Case/CASE),
    which is why only the `A` is pinned.

    KNOWN LIMIT, stated rather than left to be discovered: the case-letter
    marker exists only because the worker-loop g-306-250 ruling obligation 1
    REQUIRES a worker to stamp it. A successor filed by the REDUCER carries no
    case letter -- the secondary tokens are the natural-language forms that
    reach those. This is a strict improvement over sweeping every successor,
    not a complete discriminator; the complete one needs a successor relation
    the schema does not currently record.

    Returns a skip-reason string when the goal declares itself a successor,
    else None.
    """
    for field_name in SUCCESSOR_MARKER_FIELDS:
        m = SUCCESSOR_MARKER_PATTERN.search(g.get(field_name) or "")
        if m:
            return (f"goal declares itself a SUCCESSOR -- {field_name} asserts "
                    f"{m.group(0)!r}: the terminal parent is this goal's "
                    f"PRECONDITION, not its discharge (g-115-6252 + g-115-6223 "
                    f"successor-marker guard; do not sweep)")
    return None


#  shipped this behaviour under its own name and its own test suite.
# The alias keeps that suite meaningful against the unified implementation
# rather than deleting one Body's work to settle a merge.
_successor_scope_guard = _successor_marker_guard


# --------------------------------------------------------------------------
# The lost-update guard (). Sits directly above _mark_skipped because
# it exists only to gate it.
# --------------------------------------------------------------------------

# IMPORTED, never redefined. `_is_owncloud_backend`'s own docstring says it
# "mirrors the dispatch in liveness_check.py so both authoritative-read paths
# agree" — a third copy here would be a second predicate for a question another
# module already owns (guard-2783), and it would drift silently the first time
# a backend name is added, in the direction that makes this guard read the
# mirror while believing it read the store.
from _team_state import (  # noqa: E402
    _is_owncloud_backend,
    PROV_AUTHORITATIVE,
    PROV_LOCAL_MIRROR,
    PROV_NONE,
)

# The refusal POLICY is shared with the sibling scan-then-write sweeps
# (routing-audit-target-status-sweep, parent-supersession-sweep). Only the
# policy moved; the authoritative READ below stays local on purpose, so the
# module-attribute stubs in test_unblock_parent_lost_update_guard.py still
# resolve at call time (guard-2385). See _sweep_write_guard's header for why
# the split falls exactly there.
from _sweep_write_guard import (  # noqa: E402
    COMPLETION_PROVENANCE_FIELDS as _COMPLETION_PROVENANCE_FIELDS,
    reread_goal_authoritative as _shared_reread_goal_authoritative,
    stale_candidate_reason as _shared_stale_candidate_reason,
)


def _reread_goal_authoritative(source, goal_id):
    """``(goal, provenance)`` from the STORE OF RECORD — thin seam over the
    shared reader in `_sweep_write_guard`.

    Both collaborators are passed EXPLICITLY and are resolved as module globals
    at call time, which is what keeps `monkeypatch.setattr(mod,
    "_read_aspirations", ...)` and `monkeypatch.setattr(mod,
    "_is_owncloud_backend", ...)` working in this file's tests (guard-2385).
    Do not "simplify" this by letting the shared module import them itself: the
    patches would still apply and would silently stop being consulted.

    See `_sweep_write_guard.reread_goal_authoritative` for WHY this is an
    authoritative read rather than a local re-read or a lock.
    """
    return _shared_reread_goal_authoritative(
        source, goal_id,
        read_aspirations=_read_aspirations,
        is_owncloud=_is_owncloud_backend,
        label="unblock-parent-status-sweep",
    )


def _stale_candidate_reason(source, goal_id):
    """``None`` when the write may proceed, else the refusal reason.

    Thin seam over the SHARED policy in `_sweep_write_guard`. The read stays
    here and the judgement lives there — see that module's header for why the
    split falls exactly on that line, and why moving the read too would have
    silently disarmed this file's own test stubs.

    The reader is looked up through the module namespace at CALL time (not
    captured at import), which is what keeps
    `mod._reread_goal_authoritative = ...` working in the tests.
    """
    goal, prov = _reread_goal_authoritative(source, goal_id)
    return _shared_stale_candidate_reason(goal, prov)


def _mark_skipped(source, goal_id, parent_id, parent_status,
                  metrics_path=None, aspiration_id=None):
    """Mark Unblock goal as skipped with parent-resolved outcome_note.

    INVARIANT: uses sys.executable directly. Same rationale as
    parent-supersession-sweep._mark_superseded — bash on Windows can resolve
    to WSL bash.exe with surprising PATH semantics; aspirations-update-goal.sh
    just shells aspirations.py with the same args.

    g-115-6332: re-asserts the candidate predicate against the STORE OF RECORD
    immediately before writing, and refuses when it no longer holds. The scan
    that produced this candidate ran over the whole eligible set, so the
    scan->apply gap is unbounded in principle and was 3 seconds in the measured
    incident. See `_stale_candidate_reason` for why this is an authoritative
    read rather than a re-read or a lock.

    NOT A RARE COINCIDENCE, which is why the guard is worth its cost: for an
    Unblock goal, executing it CORRECTLY is what drives its parent terminal — so
    the success path is exactly what makes the goal an eligible candidate while
    it is still in flight. The race is structurally coupled to the good outcome.
    """
    stale = _stale_candidate_reason(source, goal_id)
    if stale is not None:
        print(f"[unblock-parent-status-sweep] REFUSED {goal_id}: {stale}",
              file=sys.stderr)
        # COUNT the refusal. A silent no-op is indistinguishable from never
        # having raced, which would make this guard's own effectiveness
        # unmeasurable — and an unmeasurable guard is the one that gets
        # "simplified" away later.
        _append_metric(metrics_path, {
            "type": "unblock_parent_refused_stale_candidate",
            "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
            "goal_id": goal_id,
            "source": source,
            "aspiration_id": aspiration_id,
            "parent_id": parent_id,
            "parent_status": parent_status,
            "reason": stale,
            "agent": os.environ.get("MIND_AGENT", "") or None,
        })
        return False

    note = (f"parent resolved without action needed "
            f"(parent_id={parent_id}, parent.status={parent_status})")
    rc1, _, err1 = _py([str(SCRIPT_DIR / "aspirations.py"),
                        "--source", source, "update-goal",
                        goal_id, "outcome_note", note])
    if rc1 != 0:
        print(f"[unblock-parent-status-sweep] update outcome_note rc={rc1}: {err1.strip()}",
              file=sys.stderr)
        return False
    rc2, _, err2 = _py([str(SCRIPT_DIR / "aspirations.py"),
                        "--source", source, "update-goal",
                        goal_id, "status", "skipped"])
    if rc2 != 0:
        print(f"[unblock-parent-status-sweep] update status rc={rc2}: {err2.strip()}",
              file=sys.stderr)
        return False
    return True


def _is_already_swept(g):
    """Idempotency: the note AND a terminal status — never the note alone.

    g-115-5097. _mark_skipped performs TWO non-atomic daemon writes (note, then
    status). Keying idempotency on the note alone makes the FIRST write the
    dedup key, so a partial success — daemon hiccup, own-cloud write_conflict,
    process death between two subprocess calls — leaves the goal carrying this
    sweep's note with status STILL pending, and every later run skips it. The
    sweep's own partial success permanently seals the goal against its own
    repair, silently in both directions: the sweep reports it as already-swept
    (never as a failure) and the goal sits in pending looking like live work.

    Requiring a terminal status too means a note-without-terminal-status
    re-qualifies for retry and SELF-HEALS on the next run, including instances
    already stranded — no migration needed.

    WHY THIS READS AS ALWAYS-FALSE AT THE ONLY CALL SITE, and why that is the
    point rather than dead code: main() already skips any goal whose status is
    not pending/in-progress, one line above the call. So a FULLY swept goal
    (terminal) never reaches here at all. Which means the pre-fix guard's only
    REACHABLE effect was to skip exactly the stranded goals — it could never
    prevent a legitimate double-sweep, because the caller had already excluded
    the goals it was nominally protecting. The conjunct is retained rather than
    deleted so the function stays correct on its own terms if that pre-filter
    is ever loosened.

    DELIBERATELY NOT ALSO REORDERING THE WRITES (fix (a) in g-115-5097). The
    goal records (a) and (b) as composing; measured against the call site they
    do not. Writing status FIRST would leave a partial failure terminal-with-no-
    note, which the pending/in-progress pre-filter then excludes from the
    candidate set forever — so the note never lands and the goal never
    self-heals. Note-first + this conjunct keeps the partial state INSIDE the
    candidate set, which is the only reason the retry can reach it.
    """
    note = (g.get("outcome_note") or "")
    if not note.startswith("parent resolved without action needed"):
        return False
    return (g.get("status") or "") in TERMINAL_STATES


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-age-hours", type=float, default=0.0,
                    help=("Minimum Unblock age before consideration "
                          "(default 0 — fire immediately when parent state "
                          "becomes terminal)."))
    ap.add_argument("--apply", action="store_true",
                    help=("Mark candidates as skipped with parent-resolved "
                          "outcome_note (default: report only)."))
    ap.add_argument("--output", choices=["json", "human"], default="json")
    ap.add_argument("--metrics-log", default=None,
                    help=("Path to JSONL metrics log. Default: "
                          "<WORLD_PATH>/unblock-parent-status-sweep-metrics.jsonl. "
                          "Pass empty string to disable."))
    args = ap.parse_args()

    metrics_path = _resolve_metrics_log(args.metrics_log)

    all_aspirations = (_read_aspirations("world")
                       + _read_aspirations("agent"))
    status_idx = _build_status_index(all_aspirations)
    recurrence_idx = _build_recurrence_index(all_aspirations)
    completed_ts_idx = _build_completed_ts_index(all_aspirations)

    # guard-1890: resolve parent ids against the ARCHIVE too. Without this a
    # COMPLETED-then-ARCHIVED parent is indistinguishable from one that never
    # existed, and the Unblock freezes PERMANENTLY — precisely because the
    # housekeeping worked.
    archived_degraded = False
    archived_ids = set()
    for _src in ("world", "agent"):
        arch = _read_archived_aspirations(_src)
        if arch is None:
            archived_degraded = True
            continue
        archived_ids |= _build_archived_id_set(arch)

    scanned = 0
    eligible = 0
    candidates = []
    applied = 0
    details = []

    for asp, source in all_aspirations:
        if asp.get("status") and asp["status"] != "active":
            continue
        for g in (asp.get("goals") or []):
            if not _is_unblock_goal(g):
                continue
            scanned += 1
            if g.get("status") not in ("pending", "in-progress"):
                continue
            if _is_already_swept(g):
                # Idempotent: already swept once, leave alone
                continue
            parent_id = _parse_parent_id(g)
            if parent_id is None:
                details.append({
                    "goal_id": g.get("id"),
                    "aspiration_id": asp.get("id"),
                    "action": "skipped",
                    "reason": "parent_id not parseable from origin_signal/title/discovered_by",
                })
                continue
            ref_ts = (g.get("created_at") or g.get("defer_reason_set_at")
                      or g.get("started"))
            age_h = _age_hours(ref_ts)
            if age_h is None:
                # An UNDEFINED age is not a small one. Fusing it into the
                # below-threshold branch rendered the literal string
                # "age None below threshold 24h" — a sentence that is not even
                # well-formed, let alone true — and a reader scanning `reason`
                # sees a threshold word and moves on. This goal carries NO
                # parseable created_at / defer_reason_set_at / started, so it can
                # never age into eligibility: the skip is permanent, not pending.
                # Skipping stays correct (guard-420: no arithmetic on a null);
                # only the name was wrong. See guard-2024, and  which
                # fixed the identical fusion in user-blocker-escalation-check.py.
                details.append({
                    "goal_id": g.get("id"),
                    "aspiration_id": asp.get("id"),
                    "parent_id": parent_id,
                    "age_hours": None,
                    "action": "skipped",
                    "reason": ("age_uncomputable: no parseable created_at / "
                               "defer_reason_set_at / started — this goal can "
                               "never age into eligibility"),
                })
                continue
            if age_h < args.max_age_hours:
                details.append({
                    "goal_id": g.get("id"),
                    "aspiration_id": asp.get("id"),
                    "parent_id": parent_id,
                    "age_hours": round(age_h, 1),
                    "action": "skipped",
                    "reason": f"age {age_h:.1f}h below threshold {args.max_age_hours}h",
                })
                continue
            eligible += 1
            # Resolve the parent across ACTIVE first, then the ARCHIVE.
            #
            # guard-2390: the old form was `status_idx.get(parent_id,
            # "archived")` — a SENTINEL meaning "absent" that was then tested
            # against TERMINAL_STATES and interpolated into the reason string.
            # Once  removed "archived" from TERMINAL_STATES as bogus,
            # that sentinel could no longer pass its own test, so the branch
            # this sweep's docstring calls "the supersession signal" went DEAD
            # while emitting `parent.status=archived` about goals that carry no
            # such status. Measured 2026-08-13:  skipped for 319.6h
            # against parent , which is absent from all 7,212 goals in
            # the six active statuses. The sentinel never reaches a message now.
            parent_status = status_idx.get(parent_id)
            if parent_status is not None:
                parent_terminal = parent_status in TERMINAL_STATES
            elif parent_id in archived_ids:
                # Found in an ARCHIVED aspiration. Being archived IS the
                # supersession signal — the aspiration is closed, so nobody is
                # advancing this parent whatever status it froze with.
                parent_status = "archived"
                parent_terminal = True
            else:
                parent_terminal = False

            # RECURRING PARENTS TAKE A DIFFERENT PREDICATE ENTIRELY
            # (). Membership in recurrence_idx IS the recurring test.
            # A recurring goal cycles pending -> completed -> pending, so its
            # status is transient and TERMINAL_STATES samples whichever phase
            # this run happened to catch. Ask whether the cadence resumed.
            #
            # ORDER IS LOAD-BEARING (merge of  + ): the
            # active/archive resolution above runs FIRST so this branch sees a
            # real status or None, never the old absent-sentinel — but the
            # recurring test runs BEFORE the terminal test, because for a
            # recurring parent the status is the wrong question entirely.
            if parent_id in recurrence_idx:
                interval_h, last_ach = recurrence_idx[parent_id]
                resolved, why = _recurring_parent_resolved(interval_h, last_ach)
                if resolved is not True:
                    details.append({
                        "goal_id": g.get("id"),
                        "aspiration_id": asp.get("id"),
                        "parent_id": parent_id,
                        "parent_status": parent_status,
                        "parent_recurring": True,
                        "age_hours": round(age_h, 1),
                        "action": "skipped",
                        "reason": why,
                    })
                    continue
                # Cadence resumed → fall through to the provenance guard and
                # the mark, carrying the freshness reason rather than a status.
                # `or "absent"` because parent_status is now None for a dangling
                # parent, and "recurring/None" would read as a real status.
                parent_status = f"recurring/{parent_status or 'absent'} — {why}"
            elif not parent_terminal:
                if parent_status is None:
                    reason = ("parent not found in active or archived queues "
                              "(dangling reference)")
                    if archived_degraded:
                        reason += (" — NOTE: the archive read DEGRADED, so this "
                                   "absence is ambiguous (archived vs unreadable); "
                                   "not swept, per the fail-safe direction")
                else:
                    reason = (f"parent not in terminal state "
                              f"(parent.status={parent_status})")
                details.append({
                    "goal_id": g.get("id"),
                    "aspiration_id": asp.get("id"),
                    "parent_id": parent_id,
                    "parent_status": parent_status,
                    "age_hours": round(age_h, 1),
                    "action": "skipped",
                    "reason": reason,
                })
                continue
            #  +  (unified 2026-08-14). ONE call site: the
            # two goals shipped the same guard under two names, and the merge
            # that conflicted on their bodies AUTO-MERGED their call sites, so
            # the sweep briefly ran both back to back. Either firing means "do
            # not discharge", so the duplicate was harmless-but-confusing; the
            # order affects only which reason gets reported.
            #
            # It runs FIRST, before the provenance guard — it is the sharper
            # semantic signal and must not depend on that guard's timestamp
            # reach (a successor created hours before its parent completed
            # passes every timestamp test;  did). Placed on the shared
            # path so BOTH lanes into the mark — the terminal-parent lane and
            # the recurring-cadence-resumed lane — pass through it.
            succ_reason = _successor_marker_guard(g)
            if succ_reason:
                details.append({
                    "goal_id": g.get("id"),
                    "aspiration_id": asp.get("id"),
                    "parent_id": parent_id,
                    "parent_status": parent_status,
                    "age_hours": round(age_h, 1),
                    "action": "skipped",
                    "reason": succ_reason,
                })
                continue
            fp_reason = _provenance_fp_guard(g, parent_id, completed_ts_idx)
            if fp_reason:
                details.append({
                    "goal_id": g.get("id"),
                    "aspiration_id": asp.get("id"),
                    "parent_id": parent_id,
                    "parent_status": parent_status,
                    "age_hours": round(age_h, 1),
                    "action": "skipped",
                    "reason": fp_reason,
                })
                continue
            entry = {
                "goal_id": g.get("id"),
                "aspiration_id": asp.get("id"),
                "source": source,
                "parent_id": parent_id,
                "parent_status": parent_status,
                "age_hours": round(age_h, 1),
                "title": g.get("title", ""),
                "action": "would_mark",
            }
            candidates.append({
                "goal_id": g.get("id"),
                "aspiration_id": asp.get("id"),
                "parent_id": parent_id,
                "parent_status": parent_status,
            })
            if args.apply:
                ok = _mark_skipped(source, g.get("id"), parent_id, parent_status,
                                   metrics_path=metrics_path,
                                   aspiration_id=asp.get("id"))
                entry["action"] = "marked" if ok else "mark_failed"
                if ok:
                    applied += 1
                    _append_metric(metrics_path, {
                        "type": "unblock_parent_resolved",
                        "timestamp": dt.datetime.now().isoformat(
                            timespec="seconds"),
                        "goal_id": g.get("id"),
                        "source": source,
                        "aspiration_id": asp.get("id"),
                        "parent_id": parent_id,
                        "parent_status": parent_status,
                        "age_hours_at_mark": round(age_h, 2),
                        "agent": os.environ.get("MIND_AGENT", "") or None,
                    })
            details.append(entry)

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
        # guard-1890: when the archive read degraded, an "absent" parent is
        # ambiguous (archived vs unreadable). Callers MUST NOT read the
        # dangling-reference entries in `details` as authoritative on a
        # degraded run.
        "archive_degraded": archived_degraded,
        "archived_ids_resolved": len(archived_ids),
        "details": details,
    }

    if args.output == "human":
        print(f"unblock-parent-status-sweep: scanned={scanned} "
              f"eligible={eligible} candidates={len(candidates)} "
              f"applied={applied} mode={'apply' if args.apply else 'report'}")
        for c in candidates:
            print(f"  {c['goal_id']} → parent {c['parent_id']} "
                  f"(status={c['parent_status']})")
    else:
        print(json.dumps(result, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
