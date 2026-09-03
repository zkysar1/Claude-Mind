#!/usr/bin/env python3
"""Detect recurring goals that have silently stopped firing.

THE GAP THIS CLOSES (g-115-3921, measured 2026-07-30 before building):
every pre-existing recurring-cadence detector is CLOSE-TRIGGERED. The
streak-break canary is emitted by `cmd_complete_by` (aspirations.py:863 /
aspirations_write.py:3094) and consumed by streak-break-reflector.py — so a
recurring goal that eventually closes LATE produces a signal, while a goal
that simply never closes produces nothing at all. `cadence-stale-canary.py` is
the sibling for the SIX skill-invocation cadences (_cadence_registry) and has
no analogue for a goal record. So the one failure mode nobody was watching is
the open-loop one: not "closed late", but "stopped closing".

Originating incident: g-115-817 (alert-inbox sweep, interval_hours=6) sat 28.9h
unfired, unclaimed, unblocked, undeferred, with nothing erroring — found by
hand. First run of THIS sweep found 24 starved goals, including 8 of echo's own
asp-001 lane that all stopped within a 4-hour window on 2026-07-25 and had not
fired in 5 days. Nothing had noticed, because nothing was watching this
property.

TWO EVIDENCE GATES, both load-bearing (guard-138: a clock-only staleness
heuristic MUST be paired with an evidence gate before it acts):

  1. BASIS — declared `interval_hours` is frequently aspirational, so the
     overdue multiple is computed against `max(interval_hours, recent p50)`,
     the same rule `_streak_break_canary_fields` applies to the close-triggered
     canary (the rb-1391 chronic-late class). Measured: g-115-22 declares 6h and
     its demonstrated p50 is ~30h, so 20.8h is UNDER its own norm, not 3.47x
     past it. Firing on the declared interval alone would cry wolf on every
     goal whose interval was set optimistically.
  2. SHELVED — a recurring goal whose structured gates currently FAIL is
     legitimately parked, not starved. Gates are gathered and evaluated exactly
     as recurring-precondition-sweep.py does it (verification.preconditions +
     fire_when, `predicate.evaluate_all`), so the two sweeps can never disagree
     about what "shelved" means. Evaluated LIVE rather than inferred from that
     sweep having run: it is `deferrable`-tier and drops in a tight context
     zone, and a shelved goal whose lastAchievedAt was never advanced would
     otherwise read as starved.
  3. CADENCE-PARKED — a recurring goal carrying `cadence_signal` fires IFF that
     signal is PRESENT (pure gate), or once `cadence_fallback_days` elapses
     (hybrid). While the signal is absent and the floor has not elapsed,
     `goal-selector.collect_candidates` skips it DELIBERATELY. Reason 2 cannot
     see this: `_structured_gates` reads verification.preconditions/fire_when,
     and a signal-gated goal expresses its parking in `cadence_signal` (often
     alongside a STRING precondition, which is also invisible to
     `predicate.evaluate_all`). The selector's gate is mirrored by calling its
     own `evaluate_cadence_signal`, so the two can never disagree.

     Load-bearing: for this goal class the selector bypasses the hour gate
     ENTIRELY, so `interval_hours` is a field it never reads — the ratio this
     sweep computes describes nothing the selector did. MEASURED (g-001-81,
     2026-08-12): zeta's g-001-07 read 30.7x its declared 1.33h while sitting
     inside its own 3d fallback floor, and echo's copy was in the same state.
     The Unblock filed from it asserted "nothing is parking it deliberately" —
     the cadence gate was, on every cycle. Without this branch every
     signal-gated goal whose signal is chronically absent files a HIGH Unblock
     forever, and the design this reads (cadence_signals.py) targets 8
     signal-gate + 33 hybrid goals against the 4 wired today.

Deliberately NOT keyed on selector rank: measured across three consecutive runs
on identical inputs, g-115-817's rank swung 4 -> 56 -> 8, because
exploration_noise makes rank a per-run sample rather than a property of the
goal. `lastAchievedAt` vs interval is noise-free.

Basis samples are read fleet-wide (every agent dir), which diverges from the
per-agent twins (cargo-cult-detector._recent_actual_cadence reads only the
bound agent's diary). Reason: a WORLD recurring goal is closed by whichever
agent picks it, so a single agent's history systematically undercounts samples
and biases the basis DOWN toward the raw interval — i.e. toward firing MORE.
Fleet-wide is the conservative direction for something that files goals.

Report-only by default. `--apply` files at most `--max-file` Unblock goals per
run (default 1), highest overdue-ratio first, deduplicated on the EXACT
origin_signal built by `_origin_signal()` (never a title substring —
g-115-2196 is the vacuous-dedup class): `unblock:recurring-starved-<goal-id>`
for WORLD-source goals, whose ids are globally unique, and
`unblock:recurring-starved-<agent>-<goal-id>` for AGENT-source goals, whose
ids are per-queue and repeat across the fleet (g-115-4241). The cap exists because the starved set
is not independent: the first run's 24 hits clustered on one common cause, and
filing 24 goals would fragment one finding into 24 individually-undiagnosable
ones while swamping the queue. The summary line always reports the FULL count,
so capping what is filed never hides what was found.

Fail-open throughout: any error prints a warning and exits 0. This is
observability, not a gate — it must never block the loop.

Usage:
    py -3 core/scripts/recurring-starvation-check.py [--multiplier N]
        [--apply] [--max-file N] [--output human|json]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _paths  # noqa: E402
import _rt  # noqa: E402  canonical Python -> daemon client
from predicate import evaluate_all  # noqa: E402
from cadence_signals import evaluate_cadence_signal  # noqa: E402  ()

DEFAULT_MULTIPLIER = 3.0
BASIS_WINDOW = 5
BASIS_MIN_SAMPLES = 3
# Statuses that mean "not silently starved": either already terminal, or
# already visibly flagged. A blocked/deferred goal is a KNOWN stall with an
# owner — reporting it here would double-count what the blocker sweeps own.
SKIP_STATUSES = {"completed", "skipped", "expired", "blocked", "in-progress"}


def _hours_since(iso_ts) -> float | None:
    if not iso_ts:
        return None
    try:
        raw = str(iso_ts).replace("Z", "")
        return (datetime.now() - datetime.fromisoformat(raw)).total_seconds() / 3600.0
    except Exception:
        return None


def _recent_actual_p50(goal_id: str, breaks: dict) -> float | None:
    """Median of the last BASIS_WINDOW actual_elapsed_hours for goal_id.

    Returns None below BASIS_MIN_SAMPLES — too few samples to call a
    demonstrated cadence, so the basis stays the declared interval. Same
    min-samples posture as _streak_break_canary_fields.
    """
    vals = breaks.get(goal_id) or []
    if len(vals) < BASIS_MIN_SAMPLES:
        return None
    return statistics.median(vals[-BASIS_WINDOW:])


def _read_source_lines(path: Path, backend, stats: dict | None = None) -> list | None:
    """Lines of one streak-breaks.jsonl, read through the STORAGE BACKEND.

    Returns None when the source could not be read at all (vs [] for a source
    that is genuinely empty) so the caller can count could-not-measure apart
    from measured-zero.

    Why not ``path.exists()`` + ``open()`` (g-115-4038, defect 1): under
    own-cloud the local tree is a READ-THROUGH CACHE, so a peer's file that
    nobody has read on this box never materialises locally and ``exists()``
    returns False for a file that is alive in the authoritative store
    (guard-980). Measured on cc-03: 1 of 5 sources present locally before the
    first backend-routed run, 5 of 5 after it — the read pulled 4 peer files
    (alpha 5,075 / bravo 28,789 / foxtrot 15,871 / zeta 38,631 bytes) that
    ``path.exists()`` had been skipping.

    CORRECTION: the originating finding (board msg-20260730-100821) quoted
    "28.2% of samples, 123,034 bytes" — that was measured on
    ``execution-diary.jsonl``, a DIFFERENT file from the ``streak-breaks.jsonl``
    this loader actually reads. The defect was real and the direction was right,
    but the number cited the wrong file. Re-measured above on the correct one.

    NOTE FOR ANY FUTURE RE-MEASUREMENT: the first backend-routed run WARMS the
    local cache, so a local-vs-backend counterfactual run afterwards shows no
    difference and reads as "the fix does nothing". Capture the local file count
    BEFORE the first run, or the evidence erases itself.

    And the miss is NOT harmless-by-direction. Fewer samples means the median
    stays below BASIS_MIN_SAMPLES, the basis falls back to the DECLARED cadence,
    and the sweep fires MORE — defeating the basis gate precisely on world goals
    closed by partner agents, which is the cry-wolf class that gate exists to
    prevent.
    """
    if backend is not None:
        try:
            return backend.read_text(path).splitlines()
        except FileNotFoundError:
            return []
        except Exception as e:  # noqa: BLE001 — degrade to the local read below
            # COUNT the degrade. Falling back to the local read silently would
            # reintroduce defect 3 one layer down: the local read is exactly the
            # read this function exists to replace, so an invisible fallback
            # restores the original under-sampling while the headline still
            # claims backend-routed coverage (guard-1893 — a swallow inside a
            # resilience loop must stay VISIBLE). Caught reviewing this fix
            # 15 minutes after landing it.
            if stats is not None:
                stats["sources_backend_fallback"] = stats.get(
                    "sources_backend_fallback", 0) + 1
                stats.setdefault("backend_fallback_reason", type(e).__name__)
    try:
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8") as f:
            return f.read().splitlines()
    except OSError:
        return None


def _load_break_actuals(agents_root: Path | None = None,
                        stats: dict | None = None) -> dict:
    """{goal_id: [actual_elapsed_hours, ...]} across every agent's sources.

    Fail-open per-file and per-line: an unreadable source or a corrupt line
    contributes nothing rather than aborting the sweep. Missing samples only
    lower the basis toward the declared interval (fire more), never suppress.

    When ``stats`` is passed, records per-source accounting into
    ``sources_seen`` / ``sources_unreadable`` / ``sources_enumerate_failed``.
    Before g-115-4038 every skip here was silent and uncounted, so nothing
    reported that 4 of 5 sample sources had been passed over (guard-1893 — a
    swallow inside a resilience loop must stay VISIBLE).
    """
    out: dict = {}
    root = agents_root or _paths.agents_root()
    backend = None
    try:
        from _fileops import get_backend
        backend = get_backend()
    except Exception:  # noqa: BLE001 — bare subprocess / no governed root
        backend = None
    try:
        agent_dirs = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        if stats is not None:
            stats["sources_enumerate_failed"] = stats.get(
                "sources_enumerate_failed", 0) + 1
        return out
    for adir in agent_dirs:
        path = adir / "session" / "streak-breaks.jsonl"
        lines = _read_source_lines(path, backend, stats)
        if lines is None:
            if stats is not None:
                stats["sources_unreadable"] = stats.get("sources_unreadable", 0) + 1
            continue
        if stats is not None:
            # Three outcomes, deliberately counted apart. Counting a MISSING
            # source as "seen" would reproduce the very could-not-measure-vs-
            # measured-zero conflation this goal exists to fix — caught in
            # review of this fix: _read_source_lines returns [] for BOTH a
            # genuinely empty source and an absent one, so `sources_seen`
            # silently included absences until this split.
            if lines:
                stats["sources_seen"] = stats.get("sources_seen", 0) + 1
            else:
                stats["sources_empty_or_absent"] = stats.get(
                    "sources_empty_or_absent", 0) + 1
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            gid = rec.get("goal_id")
            val = rec.get("actual_elapsed_hours")
            if gid and isinstance(val, (int, float)) and val > 0:
                out.setdefault(gid, []).append(float(val))
    return out


def _structured_gates(goal: dict) -> list:
    """verification.preconditions + fire_when, gathered as the sibling does.

    Kept byte-identical in shape to recurring-precondition-sweep.py's block so
    the two sweeps cannot drift on what counts as a gate.
    """
    gates = [p for p in ((goal.get("verification") or {}).get("preconditions") or [])
             if isinstance(p, dict) and "type" in p]
    fire_when = goal.get("fire_when")
    if isinstance(fire_when, dict) and "type" in fire_when:
        gates.append(fire_when)
    return gates


def _is_shelved(goal: dict) -> tuple[bool, str | None]:
    """(shelved, failing_gate_type) — a currently-failing gate means parked.

    Fail-open: an evaluator error returns not-shelved, so a broken predicate
    surfaces the goal for a human read rather than silently hiding it.
    """
    gates = _structured_gates(goal)
    if not gates:
        return False, None
    try:
        results = evaluate_all(gates, mode="fail_fast", include_skippable=False)
    except Exception as e:  # noqa: BLE001
        print(f"[recurring-starvation] WARN gate eval failed for "
              f"{goal.get('id')}: {e}", file=sys.stderr)
        return False, None
    failed = [r for r in results if not r.passed]
    if failed:
        return True, getattr(failed[0], "type", None)
    return False, None


def _cadence_parked(goal: dict) -> tuple[bool, str | None]:
    """(parked, reason) — mirrors goal-selector's signal-gated cadence gate.

    See suppression reason 3 in the module docstring for WHY. This calls the
    selector's own `evaluate_cadence_signal` rather than re-deriving the
    predicate, so a future change to the gate cannot make the detector and the
    selector disagree about which goals are parked (guard-2094: sweep the
    condition, not the token).

    Fail-open in the SAME direction as `_is_shelved`: `evaluate_cadence_signal`
    returns True ("fire") on an unknown signal name or any probe error, which
    yields not-parked here — a misconfigured signal surfaces the goal for a
    human read rather than silently hiding it. Note the two fail-open
    directions agree only because a "fire" verdict means "not parked"; do not
    invert this without re-reading that module's contract.
    """
    signal = goal.get("cadence_signal")
    if not signal:
        return False, None
    try:
        if evaluate_cadence_signal(signal, goal):
            return False, None  # signal PRESENT -> selector fires it; not parked
    except Exception as e:  # noqa: BLE001 — mirror _is_shelved's visible swallow
        print(f"[recurring-starvation] WARN cadence signal eval failed for "
              f"{goal.get('id')}: {e}", file=sys.stderr)
        return False, None

    fallback_days = goal.get("cadence_fallback_days")
    if fallback_days is None:
        return True, f"signal-gate:{signal}"  # pure gate, signal absent -> skipped
    try:
        floor_h = float(fallback_days) * 24.0
    except (TypeError, ValueError):
        print(f"[recurring-starvation] WARN unreadable cadence_fallback_days on "
              f"{goal.get('id')}: {fallback_days!r} — treating as NOT parked",
              file=sys.stderr)
        return False, None
    age_h = _hours_since(goal.get("lastAchievedAt"))
    if age_h is not None and age_h < floor_h:
        return True, f"hybrid-floor:{signal}"
    # Hybrid floor elapsed (or never achieved): the selector WILL fire it, so a
    # goal still sitting here is genuinely overdue. Report it.
    return False, None


def _read_active(source: str) -> list:
    """Aspirations via the daemon (authoritative), fail-open to []."""
    try:
        raw = _rt.aspirations_read(source=source, active=True)
    except Exception as e:  # noqa: BLE001
        print(f"[recurring-starvation] WARN read failed for source={source}: {e}",
              file=sys.stderr)
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        return raw.get("aspirations") or ([raw] if raw.get("goals") else [])
    return []


def _sources():
    yield "world"
    if os.environ.get("MIND_AGENT", "").strip():
        yield "agent"


def scan(multiplier: float, breaks: dict | None = None) -> tuple[list, dict]:
    """Return (starved_rows, stats). Pure enough to unit-test via `breaks`."""
    starved: list = []
    stats = {"examined": 0, "shelved": 0, "cadence_parked": 0,
             "no_interval": 0, "basis_suppressed": 0,
             "unreadable_anchor": 0, "sources_seen": 0, "sources_unreadable": 0,
             "sources_empty_or_absent": 0, "sources_backend_fallback": 0,
             "sources_enumerate_failed": 0}
    # stats is threaded INTO the loader so per-source skips are counted rather
    # than swallowed ( defect 3). An injected `breaks` (unit tests)
    # bypasses the loader, so source counts stay 0 — which is why the caveat
    # below keys on `> 0` and never on `== 0`.
    break_actuals = (breaks if breaks is not None
                     else _load_break_actuals(stats=stats))

    for source in _sources():
        for asp in _read_active(source):
            for goal in (asp.get("goals") or []):
                if not goal.get("recurring"):
                    continue
                if goal.get("status") in SKIP_STATUSES:
                    continue
                if goal.get("defer_reason"):
                    continue  # visibly deferred, not silently starved
                if goal.get("claimed_by"):
                    # Someone is executing it RIGHT NOW. A claim does not
                    # advance lastAchievedAt until the goal closes, so a
                    # claimed goal still looks stale — and filing against a
                    # partner mid-execution is the  race class.
                    # `claimed_by` is authoritative here, not status: the claim
                    # can land before the status flip. Caught in pre-completion
                    # review — the filed description asserts "not claimed", and
                    # shipping that assertion without the check would have made
                    # the artifact state something unverified.
                    continue
                interval = goal.get("interval_hours")
                if not isinstance(interval, (int, float)) or interval <= 0:
                    stats["no_interval"] += 1
                    continue
                stats["examined"] += 1

                anchor_field = "lastAchievedAt"
                anchor = goal.get("lastAchievedAt")
                if not anchor:
                    # Never fired. Anchor on creation so the threshold stays
                    # meaningful (mirrors the sibling's treat-as-overdue
                    # posture without firing the instant a goal is created).
                    anchor_field = "created_at"
                    anchor = goal.get("created_at")
                age_h = _hours_since(anchor)
                if age_h is None:
                    # COULD-NOT-READ, not found-nothing (guard-1753/guard-1091),
                    # and a swallow inside a resilience loop must stay visible
                    # (guard-1893). Without this the goal drops out of the sweep
                    # SILENTLY and permanently — a blind spot inside the detector
                    # built to end blind spots. Live trigger: a tz-AWARE stamp
                    # (`...+00:00`) makes the subtraction raise, because the
                    # framework mandates naive UTC and this compares against a
                    # naive now(). Measured 2026-07-30: 0 of 127 live recurring
                    # timestamps carry an offset, so this is LATENT — kept
                    # because latent-and-silent is exactly how the 5-day
                    # starvation went unnoticed.
                    stats["unreadable_anchor"] += 1
                    print(f"[recurring-starvation] WARN unreadable {anchor_field} "
                          f"on {goal.get('id')}: {anchor!r} — goal EXCLUDED from "
                          f"this sweep (not a clean result)", file=sys.stderr)
                    continue

                p50 = _recent_actual_p50(goal.get("id", ""), break_actuals)
                basis_h = float(interval)
                basis_reason = "interval"
                # basis_reason="interval" covers TWO cases a reader must not
                # conflate: no usable samples (p50 is None -> the basis is the
                # DECLARED cadence and is UNMEASURED), and samples exist but the
                # declared cadence already covers them. Only the first is the
                # cry-wolf class the basis gate exists to prevent, and reporting
                # both identically is why a reader cannot tell an assumed cadence
                # from a demonstrated one ( defect 3).
                #
                # Carried as a SEPARATE boolean rather than a new basis_reason
                # value on purpose: six live assertions pin basis_reason ==
                # "interval" exactly (test_streak_break_canary_basis L78/85/110/119,
                # test_recurring_starvation_check L89/156), and this is a REPORTING
                # fix — widening the enum would break them for no gain in signal.
                basis_measured = p50 is not None
                if p50 is not None and p50 > basis_h:
                    basis_h = float(p50)
                    basis_reason = "recent_actual_p50"

                if age_h <= multiplier * basis_h:
                    if age_h > multiplier * float(interval):
                        # Would have fired on the declared interval alone;
                        # the demonstrated cadence explains it. Count it so
                        # the suppression is visible, not invisible.
                        stats["basis_suppressed"] += 1
                    continue

                parked, park_reason = _cadence_parked(goal)
                if parked:
                    # Deliberately parked by the selector's cadence gate, not
                    # starved. Counted so the suppression is visible, never
                    # invisible (same posture as basis_suppressed above).
                    stats["cadence_parked"] += 1
                    continue

                shelved, gate_type = _is_shelved(goal)
                if shelved:
                    stats["shelved"] += 1
                    continue

                starved.append({
                    "goal_id": goal.get("id"),
                    "aspiration_id": asp.get("id"),
                    "source": source,
                    "title": (goal.get("title") or "")[:70],
                    "age_hours": round(age_h, 1),
                    "anchor_field": anchor_field,
                    # The anchor VALUE, not just its field name: it is the
                    # EPISODE identity the dedup key is built from
                    # (). The row carried only `anchor_field` before,
                    # so a reader — and the dedup — could see WHICH clock was
                    # used but never WHEN, and the key could not encode the
                    # episode it was deduping.
                    "anchor": anchor,
                    "interval_hours": interval,
                    "basis_hours": round(basis_h, 2),
                    "basis_reason": basis_reason,
                    "ratio": round(age_h / basis_h, 2),
                    "declared_ratio": round(age_h / float(interval), 2),
                    "basis_measured": basis_measured,
                    # The scale the scorer's exemption ACTUALLY uses (
                    # defect 2). goal-selector.py:3409 computes
                    #     overdue_ratio = max((elapsed - interval) / interval, 0.0)
                    # which is exactly declared_ratio - 1. Neither ratio above sits
                    # on that scale, so a "5.9x" headline reads as comfortably past
                    # a documented exemption while the scorer sees 4.94 and demotes
                    # anyway. Emitting the scorer's own number stops a diagnosing
                    # reader starting from the wrong one.
                    #
                    # DO NOT rewrite this as "compared against 5.0". The bar is NOT
                    # one constant, and asserting one is the same confident-wrong
                    # shape this goal was filed about. goal-selector.py:4015
                    # overdue_exemption_level takes THREE knobs and returns a GRADED
                    # fraction in [0,1]: the pure-ratio arm divides by
                    # substantive_demotion_overdue_exempt_ratio (5.0), but a
                    # monitor-class goal (0 < interval <= short_interval_hours, 6.0)
                    # takes the larger of that and ratio/short_interval_exempt_ratio
                    # (1.0) — so a short-interval goal is fully exempt at 1x excess,
                    # not 5x. apply_substantive_demotion consumes `>= 1.0` as a
                    # binary; recurring_saturation consumes the graded value. Hence
                    # the filed body names no number for the bar and tells the reader
                    # to compare against the selector's exemption itself.
                    "selector_excess_ratio": round(
                        max(age_h - float(interval), 0.0) / float(interval), 2),
                    "intended_agent": goal.get("intended_agent"),
                })

    starved.sort(key=lambda r: -r["ratio"])
    return starved, stats


def _anchor_token(anchor) -> str | None:
    """Compact, colon-free EPISODE token from a starvation anchor timestamp.

    Digits only, so the key stays greppable and carries no `:` that a reader
    could mistake for the `unblock:` prefix boundary. Returns None for a blank
    or unusable anchor so `_origin_signal()` falls back to the un-anchored form
    rather than minting `...-None` — same posture as the blank-agent fallback
    below (a key that READS qualified while colliding is worse than the bug).
    """
    if not anchor:
        return None
    digits = "".join(ch for ch in str(anchor) if ch.isdigit())
    return digits or None


def _origin_signal(goal_id: str, source: str, agent: str | None = None,
                   anchor: str | None = None) -> str:
    """The dedup key for one starvation Unblock. SINGLE SOURCE for both users.

    WHY THE ANCHOR IS IN THE KEY (g-115-6398). Without it the key encodes no
    EPISODE, so once any Unblock for an anchor goes terminal the anchor can
    never be reported starved again — `_existing_origin_signals()` matches ANY
    status, so a completed or SKIPPED holder silences it forever. The docstring
    on that function defends any-status by arguing a completed Unblock means
    "the goal fired, so its age reset and it cannot be starved again on the
    same anchor" — true, but the key it was defending could not express it: a
    genuinely NEW starvation minted the IDENTICAL key and was deduped against
    the old episode. Keying on the anchor implements that stated intent
    directly. Within one episode `lastAchievedAt` is constant, so re-filing is
    still deduped; once the anchor fires it moves, and the next episode mints a
    fresh key automatically — the lease releases itself with no close-on-clear
    branch to forget (guard-3419).

    Measured live 2026-08-16 (alpha, cc-07): of 5 starved recurring goals, 2
    were silenced by terminal-only holders — g-326-85 by a COMPLETED g-326-293
    and g-326-84 by a SKIPPED g-326-135. The skipped case is the sharper one:
    a skipped Unblock is precisely where the anchor did NOT fire, so the
    any-status rationale does not even hold on its own terms there.

    There are exactly two consumers — the pre-file dedup check in `main()` and
    the payload built in `_file_unblock()` — and they MUST agree. They were
    independently-written f-strings before g-115-4241; qualifying one without
    the other would make the local dedup miss its own prior filing and re-file
    every run, which is the failure the fix is meant to prevent. Grep every
    publisher before changing a key at one call site (rb-3879).

    WHY AGENT-SOURCE KEYS ARE QUALIFIED. Per-agent asp-001 queues REUSE the
    `g-001-NN` id space: every agent has its own `g-001-02`, so `g-001-02`
    names five different goals across the fleet. An unqualified key therefore
    collides. The collision is invisible locally and fatal remotely, because
    the two dedup scopes differ: `_existing_origin_signals()` reads only THIS
    agent's queues, while `goal-duplication` scans EVERY agent's. So the local
    check passes, the filing is attempted, and the gate refuses it — meaning
    only the FIRST agent in the fleet to detect a starved `g-001-NN` can ever
    file, and every later agent's starvation is permanently un-filable while
    the sweep prints REFUSED on every run. Measured twice independently: echo
    on cc-03 (g-001-02 starved 154.1h, g-001-05 145.3h, both refused against
    foxtrot's identically-keyed g-001-61), and bravo on cc-05 (g-001-01
    refused against alpha's g-001-349 and zeta's g-001-70).

    WHY WORLD-SOURCE KEYS ARE LEFT ALONE. World goal ids are already globally
    unique, so they never collide. Re-keying them would orphan every Unblock
    already filed under the old form — the dedup would stop matching its own
    history and re-file each one exactly once. Unchanged is the correct and
    smaller change.

    A blank/unresolvable agent falls back to the legacy unqualified key rather
    than minting `...-<blank>-<id>`: that would read as qualified while
    colliding fleet-wide in a NEW way, which is strictly worse than the bug.
    """
    legacy = f"unblock:recurring-starved-{goal_id}"
    if source != "agent":
        base = legacy
    else:
        owner = (agent if agent is not None
                 else os.environ.get("MIND_AGENT", "")).strip()
        base = legacy if not owner else (
            f"unblock:recurring-starved-{owner}-{goal_id}")
    token = _anchor_token(anchor)
    return base if token is None else f"{base}-{token}"


def _existing_origin_signals() -> set:
    """Every origin_signal already present across both queues, any status.

    Any-status is deliberate AND now safe, but it was not safe on its own. The
    argument is: if the Unblock was completed, the goal fired, so its age reset
    and it cannot be starved again on the same anchor; re-filing would only be
    possible after a genuinely new starvation, which carries a fresh age. That
    holds ONLY because `_origin_signal()` now puts the anchor IN the key, so a
    fresh starvation really does mint a fresh key (g-115-6398). Before that the
    key carried no age and this any-status match silenced the anchor forever.

    Keeping any-status here is the guard-895 half — within ONE episode a
    completed or skipped Unblock must still suppress a duplicate. The
    across-episode release is the key's job, not this function's.
    """
    seen = set()
    for source in _sources():
        for asp in _read_active(source):
            for goal in (asp.get("goals") or []):
                sig = goal.get("origin_signal")
                if sig:
                    seen.add(sig)
    return seen


# Statuses at which a starvation Unblock will never be acted on again.
# DELIBERATELY NOT `SKIP_STATUSES`, which answers a different question (which
# recurring ANCHORS to scan) and includes `blocked`/`in-progress` — both of
# which are live work whose Unblock SHOULD still suppress a duplicate. Also
# deliberately local: the tree carries four disagreeing `TERMINAL_STATUSES`
# constants (_goal_census, _dependency_graph, events), so importing one would
# bind this dedup to a vocabulary chosen for another purpose.
TERMINAL_HOLDER_STATUSES = {"completed", "skipped", "expired", "archived"}


def _open_origin_signals() -> set:
    """Signals with at least ONE non-terminal holder.

    Used only for the LEGACY un-anchored key. That key cannot distinguish
    episodes, so matching it any-status is exactly the permanent-silence bug:
    a terminal pre-fix Unblock would suppress every future episode forever.
    Matching it open-only keeps the transition safety it exists for (do not
    re-file an Unblock this agent already has OPEN) while letting a terminal
    one release (guard-3419: a lease with no release path is a one-shot).

    A goal carrying no status at all counts as OPEN — the fail-safe direction
    here is to suppress, since a missing status is a read problem and filing a
    duplicate on the strength of one is the worse error.
    """
    seen = set()
    for source in _sources():
        for asp in _read_active(source):
            for goal in (asp.get("goals") or []):
                sig = goal.get("origin_signal")
                if sig and (goal.get("status") or "") not in TERMINAL_HOLDER_STATUSES:
                    seen.add(sig)
    return seen


def _summarize_refusal(raw) -> str:
    """Render a gate refusal as a readable one-liner naming the FAILING check
    and its matches, instead of a head-truncated blob.

    WHY THIS EXISTS (g-115-4205, measured 2026-07-31). The prior form was
    ``(body or str(e)).strip()[:300]``. A goal-duplication refusal serializes
    its checks in a fixed order and the PASSING ones come first, so 300 chars
    lands inside ``recent_completions`` — a check that PASSED. The operator
    therefore saw ``"passed": true, "reason": "no blocking overlap ..."`` cut
    mid-word, and nothing at all about the check that actually blocked. That is
    worse than no detail: it displays a green check as though it were the
    refusal reason.

    The cost was not hypothetical. It produced a HIGH goal asserting the gate
    matched "a COMPLETED goal and a phantom id" — both claims false. The real
    matches were ``g-001-60`` in FOXTROT's queue and ``g-001-69`` in ZETA's,
    each carrying the exact same ``origin_signal``: three agents had
    independently detected the same starved goal and the gate was correctly
    refusing a third duplicate. The gate's own output labels every match with
    ``source`` (``agent:foxtrot``), so the disambiguating field was present and
    truncated away. Goal ids of the form ``g-001-NN`` are PER-AGENT-QUEUE, not
    global — ``g-001-60`` names three different goals across three agents — so
    a reader who cannot see ``source`` will resolve the id against their OWN
    queue and reach a confidently wrong conclusion.

    Fail-open: anything that does not parse as the expected gate shape falls
    back to the raw text (truncated far more generously than before), so a
    non-gate exception is never swallowed.
    """
    text = raw if isinstance(raw, str) else str(raw)
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001
        return text.strip()[:1000]
    if not isinstance(data, dict):
        return text.strip()[:1000]
    checks = ((data.get("gate_output") or {}).get("checks") or [])
    failing = [c for c in checks if isinstance(c, dict) and not c.get("passed")]
    if not failing:
        return text.strip()[:1000]
    parts = []
    for c in failing:
        bits = []
        for m in (c.get("matches") or [])[:6]:
            if not isinstance(m, dict):
                continue
            # `source` is the load-bearing field — it names WHICH agent's queue
            # the matched id lives in. Never drop it.
            bits.append(f"{m.get('goal_id')}[{m.get('source')}"
                        f"{'/' + m['match_strategy'] if m.get('match_strategy') else ''}]")
        parts.append(f"{c.get('name')}: {c.get('reason')}"
                     + (f" -- matches: {', '.join(bits)}" if bits else ""))
    return f"{data.get('error', 'refused')} | " + " | ".join(parts)


def _file_unblock(row: dict, errors: list | None = None) -> str | None:
    """File one Unblock. Returns filed goal id, or None on failure.

    `errors`, when given, collects `{goal_id, reason}` for each refusal so the
    caller can surface the reason on stdout. Optional and defaulted so every
    existing call site and test keeps working unchanged.

    The description deliberately does NOT name this script or quote schema
    field names. The duplication gate's `target_state` check resolves target
    files from identifiers in the description, and a description that cited
    this detector plus the fields it reads scored hit_ratio=1.0 against this
    detector's OWN source — verdict `already_present`, refused. The refusal was
    correct on its own terms and the fix is not an override: a starvation
    Unblock should describe the goal that stopped firing, not the instrument
    that noticed. (Measured 2026-07-30 while wiring g-115-3921.)
    """
    goal_id = row["goal_id"]
    # Both notes default to the PRE- rendering when the field is absent,
    # because rows are also hand-built by callers and tests
    # (test_recurring_starvation_dedup_key.py builds one literally). A missing
    # field must degrade to the old text, never to a wrong claim.
    basis_note = "" if row.get("basis_measured", True) else (
        "; UNMEASURED — no demonstrated cadence is on record, so this is the "
        "cadence the goal DECLARES, not one it has been observed to keep"
    )
    # ROUTING IS THE FIRST HYPOTHESIS (, measured 2026-09-02). It used
    # to be the LAST line of the description, phrased as "confirm that agent is
    # live" — and that check answers YES in the case that actually happens, so
    # the reader ruled routing out and went looking elsewhere. A LIVE but busy
    # owner starves a recurring goal exactly like a dormant one, and it is the
    # MORE likely of the two here: a dormant owner opens the selector's
    # idle-reallocation escape, so a goal still starving is evidence its owner
    # is awake. Same degrade-to-old-text discipline as basis_note above — rows
    # are hand-built by callers and tests, so an absent field must produce the
    # ruled-out wording, never a claim about a value nobody supplied.
    _ia = str(row.get("intended_agent") or "").strip()
    if _ia and _ia != "either":
        routing_note = (
            f"FIRST HYPOTHESIS — SINGLE-AGENT ROUTING. This goal carries "
            f"intended_agent={_ia}, so no other Body can select it: every peer "
            f"classifies it block_reason=routed_to_agent. Check {_ia}'s RANKING "
            f"of it, NOT {_ia}'s liveness. A live-but-busy owner and a dormant "
            f"one are indistinguishable from the goal's side, and the busy case "
            f"is the more likely one here — a dormant owner would have opened "
            f"the idle-reallocation escape and some other Body would have run "
            f"it. Measured: g-353-04 stopped 144h and g-115-8602 stopped 9h at "
            f"ranks 1097/1180 on a live owner's queue, recurring_urgency already "
            f"AT the urgency_max clamp so no further waiting could raise them. "
            f"Remedy: widen to intended_agent=either and READ THE FIELD BACK "
            f"(guard-2980 — omission does not mean unrouted; the capability-route "
            f"gate re-stamps an owner).\n\n"
        )
    elif _ia == "either":
        routing_note = (
            "Routing is NOT the cause: intended_agent=either, so every Body can "
            "select this goal. (Routing is checked first because it is the most "
            "common cause — see g-115-8700 — so this line records a measured "
            "negative rather than a silence.)\n\n"
        )
    else:
        routing_note = (
            "Routing was NOT measured for this row: no intended_agent value was "
            "supplied. Read the field off the goal before ruling routing out — "
            "an absent value in THIS report is a gap in the report, not evidence "
            "the goal is unrouted.\n\n"
        )
    _sel = row.get("selector_excess_ratio")
    selector_note = "" if _sel is None else (
        f"On the scorer's own scale this is {_sel}x overdue. Compare THAT number "
        f"against the selector's overdue exemption, not the ratios above: those "
        f"are elapsed/cadence and sit exactly 1.0 higher than the excess ratio "
        f"the scorer computes, so a headline ratio can clear a documented "
        f"exemption the scorer never sees at that value.\n\n"
    )
    payload = {
        "title": f"Unblock: recurring goal {goal_id} has stopped firing "
                 f"({row['age_hours']}h = {row['ratio']}x its expected cadence)",
        "description": (
            f"{goal_id} ('{row['title']}') last ran {row['age_hours']}h ago, "
            f"measured from its {row['anchor_field'].replace('_', ' ')} stamp.\n\n"
            f"That is {row['ratio']}x a {row['basis_hours']}h expected cadence "
            f"(basis: {row['basis_reason'].replace('_', ' ')}{basis_note}), and "
            f"{row['declared_ratio']}x the {row['interval_hours']}h cadence the "
            f"goal declares for itself.\n\n"
            f"{routing_note}"
            f"{selector_note}"
            f"No blocker, no defer, no claim, and no failing structured gate was "
            f"found. That is what was checked, and it does NOT establish that "
            f"nothing is parking this goal: the scorer can deliberately cap a "
            f"recurring goal's score beneath the best substantive candidate, and "
            f"this detector never consults the scorer. Rule that out before "
            f"concluding the goal has simply not been picked.\n\n"
            f"Ask why it is not being selected. Whether its declared cadence is "
            f"realistic is part of the question: a cadence the goal has never "
            f"actually met is itself a data-quality defect worth correcting, and "
            f"correcting it is a legitimate resolution of this Unblock. Routing "
            f"is addressed above and is the first thing to rule out — do not "
            f"re-check it by asking whether the owner is alive."
        ),
        "priority": "HIGH",
        "participants": ["agent"],
        "category": "infrastructure",
        "work_class": "hygiene",
        "verification": {
            "outcomes": [
                f"{goal_id} has either run once, had its cadence corrected to a "
                f"realistic value, or been retired with a recorded reason"
            ],
            "checks": [],
            "preconditions": [],
        },
        # rb-3879: this payload and main()'s pre-file dedup are the two
        # consumers of the key and MUST agree. Both pass the anchor.
        "origin_signal": _origin_signal(goal_id, row["source"],
                                        anchor=row.get("anchor")),
    }
    try:
        rec = _rt.aspirations_add_goal(row["aspiration_id"], payload,
                                      source=row["source"])
    except Exception as e:  # noqa: BLE001
        body = getattr(e, "body", None)
        detail = _summarize_refusal(body or str(e))
        print(f"[recurring-starvation] WARN filing failed for {goal_id}: {detail}",
              file=sys.stderr)
        # Also hand the reason back to the caller so it can reach STDOUT.
        # stderr alone is not enough: the summary line used to say "see the
        # WARN line(s) above", which points at a channel many callers drop
        # (`2>/dev/null`, stdout-only capture, a log tail). Same split-channel
        # class as guard-1680.
        if errors is not None:
            errors.append({"goal_id": goal_id, "reason": detail})
        return None
    # EVERY return-None path below must record a reason. `failed` is now
    # len(errors), so a path that returns None silently is a failure the run
    # reports as zero — and with nothing filed and nothing deduped the summary
    # block matches no branch and prints NOTHING. That is less visible than the
    # `failed += 1` counter this replaced, i.e. a regression in exactly the
    # property this function was changed to provide. Caught by fresh-eyes on
    # the same iteration that introduced it ().
    def _fail(reason: str) -> None:
        print(f"[recurring-starvation] WARN filing failed for {goal_id}: {reason}",
              file=sys.stderr)
        if errors is not None:
            errors.append({"goal_id": goal_id, "reason": reason})
        return None

    if isinstance(rec, str):
        try:
            rec = json.loads(rec)
        except json.JSONDecodeError:
            return _fail(f"unparseable response (str, {len(rec)} chars): "
                         f"{rec.strip()[:200]}")
    if isinstance(rec, dict):
        new_id = rec.get("id") or (rec.get("goal") or {}).get("id")
        if new_id:
            return new_id
        return _fail(f"response carried no goal id; keys={sorted(rec)[:12]}")
    return _fail(f"unexpected response type {type(rec).__name__}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Detect silently-starved recurring goals.")
    ap.add_argument("--multiplier", type=float, default=DEFAULT_MULTIPLIER,
                    help=f"Starved when age > N x basis (default {DEFAULT_MULTIPLIER}).")
    ap.add_argument("--apply", action="store_true",
                    help="File Unblock goals for the worst offenders.")
    ap.add_argument("--max-file", type=int, default=1,
                    help="Max Unblocks to file per run (default 1).")
    ap.add_argument("--output", choices=("human", "json"), default="human")
    args = ap.parse_args()

    try:
        starved, stats = scan(args.multiplier)
    except Exception as e:  # noqa: BLE001
        print(f"[recurring-starvation] WARN sweep failed: {e}", file=sys.stderr)
        return 0

    filed = []
    deduped = 0
    failures: list[dict] = []
    if args.apply and starved:
        existing = _existing_origin_signals()
        open_sigs = _open_origin_signals()
        for row in starved:
            if len(filed) >= max(0, args.max_file):
                break
            sig = _origin_signal(row["goal_id"], row["source"],
                                 anchor=row.get("anchor"))
            # Also honour the LEGACY unqualified key during the transition, so
            # the first post-fix run does not re-file every Unblock this agent
            # already has.
            #
            # WHY THAT IS SAFE, stated precisely — the obvious reason is WRONG.
            # `existing` is NOT "this agent's queues only": _existing_origin_signals
            # walks _sources(), which ALWAYS yields "world" (the queue every agent
            # shares) plus this agent's private queue. What actually keeps a
            # PARTNER's legacy key out of this set is a ROUTING property one call
            # below — _file_unblock passes `source=row["source"]`, so an
            # agent-source Unblock is filed into the filing agent's OWN private
            # queue and never into world. Agent-scoped legacy keys therefore
            # cannot reach another agent's `existing`. Verified 2026-07-31: of 17
            # starvation-keyed goals in the shared world queue, 0 carry a
            # per-agent g-001-NN id.
            #
            # SO: if agent-source Unblocks are ever routed to world (or this
            # dedup starts reading partner queues), this fallback silently
            # becomes a cross-agent suppressor and reintroduces the exact bug
            #  fixed — a partner's pre-fix legacy key would block us
            # again. Revisit this branch with that change, not after it.
            # THERE ARE THREE KEY GENERATIONS, not two, and missing one
            # re-files against a live open Unblock. (1) bare unqualified,
            # (2) agent-qualified (), (3) anchored ().
            # `unanchored` is generation 2 for an agent-source row and
            # generation 1 for a world-source row — derived from
            # _origin_signal() rather than re-typed, so it cannot drift from
            # the real format (rb-3879). `bare` additionally covers an
            # agent-source row whose holder predates .
            #
            # Measured 2026-08-16 before this line existed: checking only
            # `bare` left  and  — both deduped by OPEN
            # holders ( pending,  in-progress) — reading as
            # WOULD FILE, because their holders carry the agent-qualified
            # form that `bare` does not match.
            unanchored = _origin_signal(row["goal_id"], row["source"])
            legacy = f"unblock:recurring-starved-{row['goal_id']}"
            # TWO SCOPES, TWO STATUS RULES — the asymmetry is the whole fix.
            #
            # `sig` carries the anchor, so it names ONE episode. Matching it at
            # ANY status is correct and is the guard-895 half: a completed or
            # skipped Unblock for THIS episode must still suppress a duplicate.
            #
            # `legacy` carries no anchor, so it names EVERY episode this goal
            # will ever have. Matching it at any status is what made the first
            # filing the last one, forever (guard-3419). It is matched OPEN-only
            # — enough to keep the transition safety it was added for, while a
            # terminal pre-fix holder releases instead of silencing.
            if sig in existing or unanchored in open_sigs or legacy in open_sigs:
                deduped += 1
                continue
            before = len(failures)
            new_id = _file_unblock(row, errors=failures)
            if new_id:
                filed.append({"goal_id": row["goal_id"], "filed_as": new_id})
            elif len(failures) == before:
                # Structural net, not a second implementation of the reason.
                # `failed` is len(failures), so any future return-None path
                # that forgets to record would be counted as ZERO and vanish
                # from both the JSON and the human summary. Making the caller
                # enforce "None implies a recorded failure" keeps the count
                # honest without every future author having to remember.
                failures.append({"goal_id": row["goal_id"],
                                 "reason": "filing returned no goal id and "
                                           "recorded no reason (unrecorded "
                                           "failure path in _file_unblock)"})
    failed = len(failures)

    if args.output == "json":
        print(json.dumps({"starved": starved, "stats": stats, "filed": filed,
                          "deduped": deduped, "file_failures": failed,
                          "file_failure_details": failures,
                          "multiplier": args.multiplier}, indent=2))
        return 0

    # A non-zero unreadable count qualifies EVERY headline below — including a
    # zero-starved one, which would otherwise read as an all-clear the sweep
    # cannot vouch for.
    # Every could-not-measure condition qualifies EVERY headline below,
    # including a zero-starved one — which would otherwise read as an all-clear
    # the sweep cannot vouch for (guard-1091: a failed measurement is not a
    # zero).  extended this from the single unreadable-timestamp case
    # to the three source-level ones, which had NO signal at all.
    warnings = []
    if stats["unreadable_anchor"]:
        warnings.append(f"{stats['unreadable_anchor']} goal(s) EXCLUDED on an "
                        f"unreadable timestamp")
    if stats.get("sources_unreadable"):
        warnings.append(f"{stats['sources_unreadable']} basis sample source(s) "
                        f"UNREADABLE")
    if stats.get("sources_backend_fallback"):
        warnings.append(f"{stats['sources_backend_fallback']} source(s) fell back "
                        f"to a LOCAL read after a backend error "
                        f"({stats.get('backend_fallback_reason', '?')}) — those are "
                        f"the under-sampled reads this sweep exists to avoid")
    if stats.get("sources_enumerate_failed"):
        warnings.append("the agent-dir enumeration FAILED, so basis samples are "
                        "from no source at all")
    caveat = (f" [WARNING: {'; '.join(warnings)} — this result is INCOMPLETE]"
              if warnings else "")

    if not starved:
        # examined==0 is NOT an all-clear: it means the sweep evaluated nothing,
        # which a total read failure produces while returning cleanly (defect 2).
        # Reported as UNKNOWN so a vacuous zero can never read as "no drift".
        if stats["examined"] == 0:
            print(f"[recurring-starvation] UNKNOWN — 0 goals examined, so this "
                  f"is NOT an all-clear: nothing was measured "
                  f"(sources_seen={stats.get('sources_seen', 0)} "
                  f"sources_unreadable={stats.get('sources_unreadable', 0)})"
                  f"{caveat}")
            return 0
        print(f"[recurring-starvation] no starved recurring goals "
              f"({stats['examined']} examined, {stats['shelved']} shelved, "
              f"{stats['basis_suppressed']} basis-suppressed, "
              f"basis_sources={stats.get('sources_seen', 0)}){caveat}")
        return 0

    print(f"[recurring-starvation] {len(starved)} starved of {stats['examined']} "
          f"examined (shelved={stats['shelved']} "
          f"cadence_parked={stats.get('cadence_parked', 0)} "
          f"basis_suppressed={stats['basis_suppressed']} N={args.multiplier} "
          f"basis_sources={stats.get('sources_seen', 0)}){caveat}")
    for row in starved[:10]:
        print(f"    {row['goal_id']:<14} {row['age_hours']:>7}h = {row['ratio']:>6}x "
              f"basis {row['basis_hours']}h ({row['basis_reason']}) "
              f"[declared {row['interval_hours']}h -> {row['declared_ratio']}x] "
              f"{row['title'][:44]}")
    if len(starved) > 10:
        print(f"    ... and {len(starved) - 10} more")
    for f in filed:
        print(f"    FILED {f['filed_as']} for {f['goal_id']}")
    # Refusals print whenever there ARE refusals — NOT only when the run filed
    # nothing. The old `if args.apply and not filed:` gate hid every refusal
    # that shared a run with a success, and with the default --max-file 1 that
    # is reachable on the very first two rows: row 1 refused, row 2 filed, loop
    # breaks. Measured 2026-07-30 before the fix — stdout read
    # "2 starved ... FILED  for g-999-BB" with NO mention that the
    # worse-starved row had been refused, which reads as a clean partial run
    # rather than a swallowed detection. A detector whose refusals are
    # invisible is indistinguishable from one that found nothing (guard-1802).
    if failed:
        # The trailing clause only makes sense when there ARE FILED lines to
        # mis-read; on an all-refused run it would point at nothing.
        qualifier = (" — do not read the FILED lines above as the full result"
                     if filed else "")
        print(f"    {failed} attempt(s) REFUSED (detected and NOT filed"
              f"{qualifier}):")
        for e in failures:
            # NO display cap here. `reason` already came through
            # _summarize_refusal, which bounds every return path at 1000 chars,
            # so a second cut is redundant — and it re-created the exact defect
            # that function exists to fix. The summarizer puts the FAILING check
            # first and its `matches` (with the load-bearing per-agent `source`)
            # LAST, so a 200-char cut landed on the ` -- ` and dropped every
            # match, in the one block an operator actually reads. The WARN line
            # at the filing site was fixed and this summary block was not, which
            # is why the run looked repaired: the two render paths disagreed.
            # Keep the bound in ONE place (the summarizer), not two.
            print(f"      REFUSED {e['goal_id']}: {e['reason']}")
    if args.apply and not filed:
        # Name the ACTUAL reason. An earlier version printed the dedup reason
        # unconditionally, which asserted a false cause on a run where every
        # attempt had in fact been refused by a gate (verify-before-assuming:
        # a diagnostic must not claim a cause it did not establish).
        if failed:
            print(f"    (nothing filed — all {failed} attempt(s) REFUSED, "
                  f"reasons above)")
        elif deduped:
            # Say what was ACTUALLY checked. This read "already have an OPEN
            # origin_signal" while the match was any-status, so a run silenced
            # by a COMPLETED or SKIPPED holder printed the one property the
            # code did not test and read as correctly-deduped ().
            print(f"    (nothing filed — {deduped} already have a matching "
                  f"origin_signal for this starvation episode, or an OPEN "
                  f"pre-anchor one)")
        else:
            print("    (nothing filed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
