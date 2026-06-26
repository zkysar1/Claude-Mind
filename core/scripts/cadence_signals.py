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

Probes MUST be cheap -- one WM-slot read or one pipeline.jsonl scan, memoized
per process via ``_CACHE``. ``goal-selector.py`` runs once per selection cycle
(a fresh process), so process-lifetime memoization means one evaluation per
signal per cycle even when several goals share a signal.

Add new probes to ``SIGNAL_REGISTRY`` as more goals are wired -- the design's
seeds 1-2 cover 8 signal-gate + 33 hybrid goals; this module ships the three
cleanest internal probes (encoding queue, unreflected hypotheses, resolvable
hypotheses) and the dispatch they share.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

# Per-process memo: signal-name -> bool. Cleared only by process exit (the
# goal-selector.py invocation boundary) or test setup via clear_cache().
_CACHE: dict[str, bool] = {}


def clear_cache() -> None:
    """Reset the per-process signal memo (test hook; harmless in production)."""
    _CACHE.clear()


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


def _iter_pipeline():
    """Yield hypothesis records from world pipeline.jsonl; silent on any error."""
    wd = _world_dir()
    if wd is None:
        return
    p = wd / "pipeline.jsonl"
    if not p.exists():
        return
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue
    except Exception:
        return


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
    """: fire IFF >=1 resolved hypothesis has not yet been reflected on."""
    for h in _iter_pipeline():
        if h.get("stage") == "resolved" and not (h.get("reflected") or h.get("reflected_on")):
            return True
    return False


def _resolvable_hypotheses_present(goal) -> bool:
    """: fire IFF >=1 non-terminal hypothesis is past its resolves_by gate."""
    today = date.today()
    for h in _iter_pipeline():
        if h.get("stage") not in ("active", "discovered"):
            continue
        rb = h.get("resolves_by") or h.get("resolves_no_earlier_than")
        if not rb:
            continue
        try:
            if date.fromisoformat(str(rb)[:10]) <= today:
                return True
        except Exception:
            continue
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
    Memoized per process (one evaluation per signal per selection cycle).
    """
    if not signal_name:
        return True  # no signal named -> behave as ungated (fire)
    if signal_name in _CACHE:
        return _CACHE[signal_name]
    probe = SIGNAL_REGISTRY.get(signal_name)
    if probe is None:
        # Unknown signal -> fail-open (fire). Do NOT cache: a probe may be
        # registered late (e.g. in a test) and we must not pin the miss.
        return True
    try:
        result = bool(probe(goal or {}))
    except Exception:
        result = True  # probe error -> fail-open (fire)
    _CACHE[signal_name] = result
    return result
