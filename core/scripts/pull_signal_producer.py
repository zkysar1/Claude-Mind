"""pull_signal_producer.py — decide-and-write for the `pull_signal` dependency-pull flag.

g-115-6590 item (1), producer half. The CONSUMER (goal-selector.py
``apply_pull_boost``) shipped 2026-08-17 and had NO producer for six days, so the
feature was inert by construction: the boost requires a ``pull_signal`` dict on a
goal and nothing ever wrote one. Worse, the field was not even writable — the
goal-field allowlist in ``_goal_fields.py`` was derived 2026-08-18 from a census
of keys OBSERVED on live goals, and a read-only field whose writer has not
shipped is invisible to such a census, so every write was refused by the
``goal-field-allowlist`` gate. Both halves are fixed in the change that ships
this module.

WHY THE DECISION IS A PURE FUNCTION. ``decide()`` takes a goal record and returns
a verdict; it performs no I/O, so every branch is testable without a daemon, a
world, or a git tree. Same shape and the same reason as
``reducer_self_fence.py::decide`` — a producer that fires on the wrong branch is
exactly the class of defect that is invisible in production, because its failure
mode is *silence*.

THE AGE TEST IS THE CONSUMER'S OWN. ``_signal_age_hours`` deliberately mirrors
``apply_pull_boost``'s parse, its ``max_age_hours`` bound and its 1h skew
tolerance rather than re-deriving them. A producer that judges liveness on
different terms than the consumer will either skip writes the consumer would
have ignored, or re-write signals it already honours — producer and consumer
silently ceasing to name the same object (guard-4065).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

# Mirrors apply_pull_boost's own tolerance. The signal is written on the
# PRODUCER's box and read on the CONSUMER's, so a producer even seconds ahead
# stamps a set_at in the reader's future; treating that as corrupt is the exact
# cross-box failure (guard-3221) this mechanism lives inside.
SKEW_TOLERANCE_H = 1.0

VERDICT_SET = "SET"
VERDICT_CLEARED = "CLEARED"
VERDICT_SKIP_LIVE = "SKIP-live"
VERDICT_SKIP_NO_GOAL = "SKIP-no-goal"
VERDICT_SKIP_UNREADABLE = "SKIP-unreadable"
VERDICT_SKIP_WRITE_FAILED = "SKIP-write-failed"


def signal_age_hours(sig, now):
    """Age in hours of a pull_signal dict, or None when it is not readable.

    Returns a SIGNED age: a signal stamped ahead of the reader yields a negative
    value, which the caller tests against the skew tolerance. Never raises —
    a malformed signal is 'no readable signal', not an error.
    """
    if not isinstance(sig, dict):
        return None
    raw = sig.get("set_at")
    if not raw or not isinstance(raw, str):
        return None
    try:
        return (now - datetime.fromisoformat(raw)).total_seconds() / 3600.0
    except (ValueError, TypeError):
        return None


def live_age_hours(sig, now, max_age_hours):
    """SINGLE SOURCE OF TRUTH for "is this ``pull_signal`` live, and how old?".

    Returns the age in hours (clamped at 0.0) when the signal is LIVE, or
    ``None`` when it is absent, malformed, aged out, or implausibly far in the
    future.

    THREE consumers across TWO files, and they must never disagree about which
    signals are live:

      * ``is_live`` below — the producer's idempotence guard (SKIP-live).
      * goal-selector ``apply_pull_boost`` — converts a live signal into RANK.
      * goal-selector's recurring hour gate — converts it into ELIGIBILITY.

    WHY THE ARITHMETIC LIVES HERE AND THE SELECTOR IMPORTS IT (g-115-6590,
    2026-08-30). Until now the selector carried its own copy of this parse and
    these bounds. Both copies agreed — verified across the whole age range —
    but agreeing today is not the same as being one rule, and the g-353-62
    review named the residual precisely: "one predicate per FILE, not one per
    mechanism". The direction is forced rather than chosen: ``goal-selector.py``
    is HYPHENATED and so cannot be imported by name, so the shared arithmetic
    can only sit on this side. Same argument ``peer_surface`` makes for
    ``split_author``.

    It returns the AGE rather than a bool because ``apply_pull_boost`` records
    ``pull_signal_age_hours``; a bool consumer is a one-line wrapper over an age
    (``is_live``, immediately below), while the reverse cannot be written — so
    the age is the shareable form.

    ``enabled`` is deliberately NOT read here, and that is the one thing the two
    callers do NOT share. The selector gates on it so a single flag disables both
    of its axes; the producer's idempotence guard must stay correct whatever the
    consumer's flag says, or a disabled mechanism would re-stamp a live signal on
    a shared world goal several times an hour. One shared arithmetic, each caller
    keeping its own policy over it.
    """
    age = signal_age_hours(sig, now)
    if age is None:
        return None
    if age > max_age_hours or age < -SKEW_TOLERANCE_H:
        return None
    return max(0.0, age)


def is_live(sig, now, max_age_hours):
    """True when apply_pull_boost would currently honour this signal.

    A bool view of ``live_age_hours`` — no second copy of the bounds.
    """
    return live_age_hours(sig, now, max_age_hours) is not None


def decide(goal, *, now, max_age_hours, clear=False, reason="", by=""):
    """Pure decision. Returns ``(verdict, value)``.

    ``value`` is the string to hand ``aspirations-update-goal.sh`` when the
    verdict is SET or CLEARED, else None.

    ``goal`` is None when the id resolved to no record — kept as an explicit
    input rather than an exception so the no-goal branch is testable like any
    other.
    """
    if goal is None:
        return VERDICT_SKIP_NO_GOAL, None

    if clear:
        # NULL, never key removal. Measured against coordination_merge._merge_goal:
        # a clear by REMOVING the key is RESURRECTED by the cross-box merge even
        # when the clearer is strictly newer, while a null clear survives whenever
        # the clearer is the newer write. goal-schemas.md "THE CLEAR IS FRAGILE".
        return VERDICT_CLEARED, "null"

    # Idempotence (rb-662, claim-once). The worker lane fires on every carrier
    # push and the reducer lane on every iteration close, so an unguarded
    # producer would rewrite a shared world-store field several times an hour to
    # no effect: a live signal already carries the boost, and re-stamping it
    # changes no ranking while adding contention. Once the signal has aged out —
    # or the consumer cleared it — a still-outstanding dependency re-stamps,
    # which is correct: outstanding content should keep pulling.
    if is_live(goal.get("pull_signal"), now, max_age_hours):
        return VERDICT_SKIP_LIVE, None

    payload = {
        "set_at": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "by": by,
        # One line, bounded. The reason is audit prose that rides in a shared
        # store; an unbounded field here is how a store grows a narrative column.
        "reason": " ".join((reason or "").split())[:200],
    }
    return VERDICT_SET, json.dumps(payload)


# --------------------------------------------------------------------------
# I/O half — thin, so the branch logic above stays pure and testable.
# --------------------------------------------------------------------------

def _read_goal(gid):
    """Return the goal record, or None if absent. Raises LookupError if the
    store could not be read at all — an unreadable store must never render as
    an absent goal (guard-2298: an error that looks like a healthy zero)."""
    from _runtime_bash import bash_cmd  # guard-580: never a bare "bash" argv[0]
    q = subprocess.run(
        bash_cmd(SCRIPT_DIR / "aspirations-query.sh", "--goal-field", "id", gid, "--full"),
        capture_output=True, text=True, encoding="utf-8",
    )
    try:
        recs = json.loads(q.stdout or "[]")
    except json.JSONDecodeError:
        raise LookupError(
            f"query returned unparseable output (rc={q.returncode}): "
            f"{' '.join(((q.stdout or '') + (q.stderr or '')).split())[:160]}"
        )
    return recs[0] if recs else None


def _write(gid, value):
    """Write pull_signal. Returns (ok, detail)."""
    from _runtime_bash import bash_cmd
    w = subprocess.run(
        bash_cmd(SCRIPT_DIR / "aspirations-update-goal.sh", gid, "pull_signal", value),
        capture_output=True, text=True, encoding="utf-8",
    )
    combined = (w.stdout or "") + (w.stderr or "")
    # The wrapper exits 0 on a refusal and reports it in the JSON payload, so
    # the rc is not the signal — the payload is (guard-1150: never read an rc as
    # a verdict).
    if w.returncode != 0 or '"error"' in combined:
        return False, " ".join(combined.split())[:220]
    return True, ""


def main(argv):
    gid = os.environ["GOAL"]
    clear = os.environ.get("DO_CLEAR") == "1"
    max_age = float(os.environ.get("MAX_AGE", "24.0"))

    try:
        goal = _read_goal(gid)
    except LookupError as exc:
        print(f"{VERDICT_SKIP_UNREADABLE} ({gid}: {exc})")
        return 0

    verdict, value = decide(
        goal, now=datetime.now(), max_age_hours=max_age, clear=clear,
        reason=os.environ.get("REASON", ""), by=os.environ.get("BY", ""),
    )

    if verdict == VERDICT_SKIP_NO_GOAL:
        print(f"{VERDICT_SKIP_NO_GOAL} ({gid}: no record)")
        return 0
    if verdict == VERDICT_SKIP_LIVE:
        age = signal_age_hours(goal.get("pull_signal"), datetime.now())
        print(f"{VERDICT_SKIP_LIVE} ({gid}: signal age {age:.2f}h within {max_age}h; already boosted)")
        return 0

    ok, detail = _write(gid, value)
    if not ok:
        print(f"{VERDICT_SKIP_WRITE_FAILED} ({gid}: {detail})")
        return 0
    if verdict == VERDICT_CLEARED:
        print(f"{VERDICT_CLEARED} ({gid})")
    else:
        print(f"{VERDICT_SET} ({gid}: {json.loads(value)['reason']})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
