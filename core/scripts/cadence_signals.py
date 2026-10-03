#!/usr/bin/env python3
"""Cadence signal registry for signal-gated recurring goals ( / design ).

A recurring goal may carry an optional ``cadence_signal`` field naming a cheap,
internally-observable "is there work?" probe. ``goal-selector.py`` consults this
registry in its recurring time-gate block (``collect_candidates``): when the
named signal is ABSENT the goal is filtered out of candidacy ("fire IFF signal
present"); when present the goal is scored normally. An optional
``cadence_fallback_days`` field makes the goal HYBRID -- fire on signal OR after
the fallback floor -- per the 4-way decision rule in the design.

Fail-open by construction (the safe direction): an unknown signal name OR any
probe exception returns ``True`` ("fire"), so a misconfigured signal degrades to
legacy time-gated behavior and NEVER silently silences a goal. Backwards-compat:
goals WITHOUT ``cadence_signal`` never reach this module (goal-selector only
calls ``evaluate_cadence_signal`` when the field is set).

Probes MUST be cheap -- one WM-slot read or one pipeline.jsonl scan per
process. ``goal-selector.py`` runs once per selection cycle (a fresh process),
so the parsed pipeline is memoized for the process (``_PIPELINE``) and each
verdict in ``_CACHE`` -- keyed per GOAL, not per signal, because the pipeline
probes read the goal's own pass memory (below).

PASS MEMORY (g-115-10951). A pipeline probe that only asks "does a qualifying
record exist?" stays true while a pass HOLDS a record it cannot act on (not yet
resolvable, or abstained on under guard-5623). Every selection then re-admits
the goal past its interval, and the reducer-only floor re-hoists it once per
iteration. Measured: 20 of one reducer's 46 closes in 13h were g-001-08, and
g-001-02 burst six times in one day. So both pipeline probes count only a
record the goal's last completed pass could NOT have seen (it became eligible
after ``lastAchievedAt``), or any record once the goal's declared interval has
elapsed since that pass. A held record therefore gets one look per interval,
never one per selection. When there is no usable pass stamp the probe behaves
as it did before memory existed.

Add new probes to ``SIGNAL_REGISTRY`` as more goals are wired -- the design's
seeds 1-2 cover 8 signal-gate + 33 hybrid goals; this module ships the three
cleanest internal probes (encoding queue, unreflected hypotheses, resolvable
hypotheses) and the dispatch they share.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

# Per-process memos. Cleared only by process exit (the goal-selector.py
# invocation boundary) or test setup via clear_cache().
#   _CACHE    -- verdict per (signal, goal id, and the goal fields the verdict
#                reads); one signal can be present for one goal and absent for
#                another in the same cycle.
#   _PIPELINE -- the parsed pipeline, so N goals sharing a signal cost one scan.
_CACHE: dict[tuple, bool] = {}
_PIPELINE: list | None = None


def clear_cache() -> None:
    """Reset the per-process memos (test hook; harmless in production)."""
    global _PIPELINE
    _CACHE.clear()
    _PIPELINE = None


def _world_dir() -> Path | None:
    try:
        from _paths import WORLD_DIR
        return Path(WORLD_DIR)
    except Exception:
        return None


def _read_wm_slot(slot: str):
    """Cheap WM slot read; returns the slot value or None on any failure."""
    try:
        from wm import read_wm
        return (read_wm() or {}).get(slot)
    except Exception:
        return None


def _read_pipeline() -> list:
    """Parse hypothesis records from world pipeline.jsonl; silent on any error."""
    wd = _world_dir()
    if wd is None:
        return []
    p = wd / "pipeline.jsonl"
    if not p.exists():
        return []
    records = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return []
    return records


def _iter_pipeline():
    """Iterate the pipeline records, read once per process."""
    global _PIPELINE
    if _PIPELINE is None:
        _PIPELINE = _read_pipeline()
    return iter(_PIPELINE)


def _interval_hours(goal) -> float:
    """The goal's declared interval. MIRRORS goal-selector.get_interval_hours
    (interval_hours -> remind_days * 24 -> 24); it cannot be imported, because
    goal-selector imports this module. Parity is pinned in
    tests/test_cadence_signal_gate.py."""
    # A cleared interval_hours (None) reads as absent ().
    if goal.get("interval_hours") is not None:
        return float(goal["interval_hours"])
    if "remind_days" in goal:
        return float(goal["remind_days"]) * 24
    return 24.0


def _parse_stamp(value) -> datetime | None:
    """ISO date or datetime -> naive datetime (a date-only value is its day's
    00:00, like goal-selector.hours_since). None when absent or unparseable."""
    if not value:
        return None
    s = str(value)
    try:
        if "T" in s:
            return datetime.fromisoformat(s)
        return datetime.combine(date.fromisoformat(s[:10]), datetime.min.time())
    except (ValueError, TypeError):
        return None


def _pass_memory(goal) -> tuple[datetime | None, bool]:
    """(since, floor_elapsed) from the goal's record of its last completed pass.

    ``since`` is ``lastAchievedAt``, or None when that stamp is no proof a pass
    ran: absent, unparseable, in the future, or written by the precondition
    sweep (``lastAchievedAt == last_shelved_at`` means shelved, not achieved --
    recurring-precondition-sweep.py). None means no memory, so every record
    counts as unseen. ``floor_elapsed`` is True once the declared interval has
    passed since that pass, so a record the pass saw and held is due again.
    """
    la = goal.get("lastAchievedAt")
    if not la or la == goal.get("last_shelved_at"):
        return None, True
    since = _parse_stamp(la)
    if since is None or since.tzinfo is not None:
        return None, True
    hours = (datetime.now() - since).total_seconds() / 3600.0
    if hours < 0:
        return None, True
    return since, hours >= _interval_hours(goal)


def _after_pass(since: datetime | None, stamp) -> bool:
    """True when ``stamp`` is provably later than the pass at ``since``.

    No memory (``since`` None) -> True. A date-only stamp reads as its day's
    00:00, so it is later only when its DATE is: the pass's own day counts as
    seen, since it cannot be ordered against the pass time, and the interval
    floor bounds the wait. An absent or unparseable stamp is not provably
    later -> False, also floor-bounded.
    """
    if since is None:
        return True
    t = _parse_stamp(stamp)
    if t is None or t.tzinfo is not None:
        return False
    return t > since


# ----- probes (each takes the goal dict, returns True == "there is work, fire") -----

def _encoding_queue_nonempty(goal) -> bool:
    """ encoding-flush: fire IFF the encoding queue has items to flush.

    Absent/empty queue == nothing to flush == genuinely no work (the canonical
    clean signal-gate named in the design).
    """
    q = _read_wm_slot("encoding_queue")
    if isinstance(q, (list, dict)):
        return len(q) > 0
    return False


def _unreflected_hypotheses_present(goal) -> bool:
    """: fire IFF >=1 resolved, REFLECTABLE hypothesis is not yet
    reflected on, and it was resolved after the goal's last completed pass or
    that pass is older than the goal's interval (see PASS MEMORY).

    Only a reflectable outcome counts (_reflectable.is_reflectable): an
    UNRESOLVABLE or EXPIRED record is left unreflected by design, so without
    this filter the signal stayed true until such records aged to archived.
    """
    from _reflectable import is_reflectable
    since, floor_elapsed = _pass_memory(goal)
    for h in _iter_pipeline():
        if h.get("stage") != "resolved" or h.get("reflected") or h.get("reflected_on"):
            continue
        if not is_reflectable(h):
            continue
        if floor_elapsed or _after_pass(since, h.get("resolved_at") or h.get("outcome_date")):
            return True
    return False


def _resolvable_hypotheses_present(goal) -> bool:
    """: fire IFF >=1 non-terminal hypothesis is past its resolves_by
    gate, and it became due or was formed after the goal's last completed pass,
    or that pass is older than the goal's interval (see PASS MEMORY)."""
    today = date.today()
    since, floor_elapsed = _pass_memory(goal)
    for h in _iter_pipeline():
        if h.get("stage") not in ("active", "discovered"):
            continue
        rb = h.get("resolves_by") or h.get("resolves_no_earlier_than")
        if not rb:
            continue
        try:
            due = date.fromisoformat(str(rb)[:10])
        except Exception:
            continue
        if due > today:
            continue
        if (floor_elapsed or _after_pass(since, due.isoformat())
                or _after_pass(since, h.get("formed_at") or h.get("formed_date"))):
            return True
    return False


SIGNAL_REGISTRY = {
    "encoding_queue_nonempty": _encoding_queue_nonempty,
    "unreflected_hypotheses_present": _unreflected_hypotheses_present,
    "resolvable_hypotheses_present": _resolvable_hypotheses_present,
}


def evaluate_cadence_signal(signal_name: str, goal: dict | None = None) -> bool:
    """Return True if the named cadence signal is PRESENT (the goal should fire).

    Fail-open (the safe direction): an empty/unknown signal name OR any probe
    error returns True ("fire"), so a misconfigured signal can never silently
    silence a recurring goal -- it degrades to legacy time-gated behavior.
    Memoized per process, per goal (the key carries every goal field a probe
    reads, so two goals sharing a signal never share a verdict).
    """
    if not signal_name:
        return True  # no signal named -> behave as ungated (fire)
    g = goal if isinstance(goal, dict) else {}
    key = (signal_name, str(g.get("id")), str(g.get("lastAchievedAt")),
           str(g.get("last_shelved_at")), str(g.get("interval_hours")),
           str(g.get("remind_days")))
    if key in _CACHE:
        return _CACHE[key]
    probe = SIGNAL_REGISTRY.get(signal_name)
    if probe is None:
        # Unknown signal -> fail-open (fire). Do NOT cache: a probe may be
        # registered late (e.g. in a test) and we must not pin the miss.
        return True
    try:
        result = bool(probe(goal or {}))
    except Exception:
        result = True  # probe error -> fail-open (fire)
    _CACHE[key] = result
    return result
