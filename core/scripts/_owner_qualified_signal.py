"""Owner-qualified origin_signal keys for per-agent goal ids ().

WHY THIS EXISTS
---------------
Agent-queue goal ids are NOT globally unique: every agent has its own asp-001
with its own `g-001-NN` series, so `g-001-08` names a DIFFERENT goal in each
agent's queue (measured: three live records with different lastAchievedAt and
different interval_hours). A dedup / cooldown key that embeds a bare goal id is
therefore ambiguous, and the first agent to file under it can permanently
suppress every other agent's genuinely-distinct escalation.

guard-2107 states the rule this module implements: when a dedup / origin_signal
/ cooldown key EMBEDS an identifier, the identifier's UNIQUENESS SCOPE must
match the scope of the check that READS it.

WHAT THE SCOPE ACTUALLY IS (measured 2026-08-01, g-115-4110)
------------------------------------------------------------
Every dedup reader in this family spans exactly one shared surface: the WORLD
queue (`aspirations-query.sh` and each sweep read "world + the bound agent's
queue"; a partner's private agent queue is never visible). So the defect
condition is precise:

    a key derived from a PER-AGENT goal id that lands in the WORLD queue.

That is why world-source ids are left alone below — they are already globally
unique, and rewriting them would orphan every key already filed under the old
form, re-filing each exactly once.

SEMANTICS (copied deliberately from recurring-starvation-check.py:446-453,
which shipped this pattern in g-115-4241 — that local copy is intentionally
left in place; this module is for the sweeps fixed in g-115-4110)
-------------------------------------------------------------------
1. `source != "agent"`  -> return the legacy unqualified key unchanged.
2. blank / unresolvable owner -> return the LEGACY key, never
   `<prefix><blank>-<id>`. A blank-qualified key would READ as qualified while
   colliding fleet-wide in a NEW way, which is strictly worse than the bug.
3. Otherwise -> `<prefix><owner>-<goal_id>`.

READ SIDE: callers doing dedup MUST accept BOTH forms via `signal_candidates`,
or in-flight signals filed under the old key stop matching their own history
and get re-filed exactly once each.
"""

from __future__ import annotations

import os


def qualified_signal(prefix: str, goal_id: str, source: str,
                     owner: str | None = None) -> str:
    """The origin_signal to WRITE for `goal_id` in queue `source`.

    `owner` defaults to $MIND_AGENT. Pass it explicitly when the caller may be
    acting on a goal it does not own — stamping the RUNNING agent's name onto
    another agent's goal would mint a confidently-wrong qualification, which is
    worse than leaving it legacy.
    """
    legacy = f"{prefix}{goal_id}"
    if source != "agent":
        return legacy
    who = (owner if owner is not None else os.environ.get("MIND_AGENT", "")).strip()
    if not who:
        return legacy
    return f"{prefix}{who}-{goal_id}"


def legacy_signal(prefix: str, goal_id: str) -> str:
    """The pre- unqualified key. Kept for read-side fallback."""
    return f"{prefix}{goal_id}"


def signal_candidates(prefix: str, goal_id: str, source: str,
                      owner: str | None = None) -> list[str]:
    """Every key form a dedup READ must treat as a match, qualified first.

    Returns ONE element when the qualified and legacy forms coincide (world
    source, or an unresolvable owner), so callers can compare lengths without
    special-casing.
    """
    q = qualified_signal(prefix, goal_id, source, owner)
    legacy = legacy_signal(prefix, goal_id)
    return [q] if q == legacy else [q, legacy]
