"""Candidate transition-table gate (, B1c — the validation half of B1).

Enforces goal-intake-management.md §2's transition table in the status-update
path, in BOTH write paths (guard-742 twin; the daemon is the live one, the
CLI mirror exists so the two cannot drift — guard-2323):

  - core/scripts/aspirations.py :: cmd_update_goal
  - mind_api/src/endpoints/aspirations_write.py :: update_goal

The table (spec §2, "Transition table (enforced in the status-update path)"):

  candidate -> pending      ALLOWED  — grooming promote / starvation
                                      auto-promote / owner/manual; ledgered
  candidate -> superseded   ALLOWED  — grooming MERGE (outcome_note names the
                                      surviving consolidated goal); ledgered
  candidate -> skipped      ALLOWED  — grooming rb-route (outcome_note carries
                                      rb-id) or close-moot; ledgered
  candidate -> in-progress  FORBIDDEN — must pass through pending
  candidate -> completed    FORBIDDEN — must pass through pending

Scope is DELIBERATELY the candidate row only. Non-candidate goals are not
this module's concern: their status transitions are owned by the other
guards in the two write paths (notably the blanket "no direct
status=superseded" refusal, which this module carves out for candidate
prev-status and leaves intact for every other prev-status — the
g-353-62 intent-satisfaction evidence gate still owns those).

Ledger (spec §5): every ALLOWED candidate transition appends one row to
world/candidate-grooming-ledger.jsonl shaped
  {ts, agent, goal_id, verdict, evidence}
matching the §5 spec line "LEDGER: append JSONL {ts, agent, goal_id,
verdict, evidence}". The grooming recurring (B2) reads per-firing numbers
from this file, and sprint-planning oversight samples >=5 verdicts/cycle
(I4). The file location is the SSOT: LEDGER_NAME below.

FAIL-OPEN for the ledger, FAIL-CLOSED for the table:
  - a table refusal must never be skipped because a helper read failed —
    both call sites therefore re-evaluate the table INSIDE their write
    lock, against the locked goal read, and the pre-lock checks there
    (daemon) are only fail-fast copies;
  - a ledger APPEND failure must never block an allowed transition —
    the gate's job is the refusal; the audit row is best-effort and its
    failure is logged to stderr, never raised.

The pre-g-353-63 premise that "candidate is invisible to goal-selector" is
STALE (g-353-82 deliberately admitted candidate at goal-selector.py) —
which is exactly why this table must live in the status-update path:
admitted candidates are selectable, and an ungated write could take one
straight to in-progress, silently turning intake back into an ordinary
queue.
"""

import datetime as _dt
import os as _os
from pathlib import Path
from typing import Any, Dict, Optional

# --- The table (spec §2) ----------------------------------------------------

CANDIDATE = "candidate"

#: new-status -> default ledger verdict for an ALLOWED transition.
#: `skipped` is ambiguous between the two §5 verdicts (rb-route and
#: close-moot); the default is "close-moot" and a writer that performed an
#: rb-route passes verdict="rb-route" explicitly to append_ledger() (the
#: spec says outcome_note carries the rb-id — the evidence dict here
#: captures outcome_note, so the distinction is recoverable either way).
ALLOWED_TRANSITIONS = {
    "pending": "promote",
    "superseded": "merge",
    "skipped": "close-moot",
}

#: FORBIDDEN new-statuses from candidate (spec §2 last row).
FORBIDDEN_TRANSITIONS = ("in-progress", "completed")

#: The §2 rule, verbatim-ish, for the refusal message. The goal requires the
#: error to NAME the rule.
RULE = (
    "goal-intake-management.md §2 candidate transition table: candidate "
    "-> in-progress/completed is FORBIDDEN — a candidate must pass through "
    "pending first (grooming promote / starvation auto-promote / owner)"
)


def evaluate(prev_status: Optional[str], new_status: Optional[str]) -> Dict[str, Any]:
    """Decide one candidate-row transition. Pure; no I/O.

    Returns a dict:
      allowed: bool — False only for a FORBIDDEN candidate transition.
      ledger:  bool — True when the transition is an ALLOWED candidate
               transition that must append a ledger row.
      verdict: str | None — default ledger verdict (see
               ALLOWED_TRANSITIONS); None when no row is due.
      message: str | None — refusal text naming the rule (allowed=False).
      candidate: bool — whether prev_status is candidate (call sites use
               this to know the table applied, for carve-outs).
    """
    out: Dict[str, Any] = {
        "allowed": True,
        "ledger": False,
        "verdict": None,
        "message": None,
        "candidate": prev_status == CANDIDATE,
    }
    if prev_status != CANDIDATE:
        return out  # not the candidate row — other guards own this write
    if new_status in FORBIDDEN_TRANSITIONS:
        out["allowed"] = False
        out["message"] = (
            f"BLOCKED: candidate -> {new_status} is a forbidden transition "
            f"({RULE}). Promote it to pending first, then claim and work it."
        )
        return out
    if new_status in ALLOWED_TRANSITIONS:
        out["ledger"] = True
        out["verdict"] = ALLOWED_TRANSITIONS[new_status]
        return out
    # candidate -> candidate, candidate -> blocked, candidate -> deferred...
    # not in the §2 table: the spec's table lists the grooming verdicts and
    # the forbidden row, and nothing else is defined. Treat undefined as
    # REFUSED (fail-closed): the table is closed, and an undefined row
    # silently passing would be the same class of hole  was filed
    # to close.
    out["allowed"] = False
    out["message"] = (
        f"BLOCKED: candidate -> {new_status} is not a row in the §2 "
        f"candidate transition table (allowed: "
        f"{', '.join(ALLOWED_TRANSITIONS)}; forbidden: "
        f"{', '.join(FORBIDDEN_TRANSITIONS)}). {RULE}."
    )
    return out


# --- The ledger (spec §5) ----------------------------------------------------

LEDGER_NAME = "candidate-grooming-ledger.jsonl"


def ledger_path(world_dir: Path) -> Path:
    """SSOT for the §5 ledger location: world/candidate-grooming-ledger.jsonl."""
    return Path(world_dir) / LEDGER_NAME


def append_ledger(world_dir: Path, *, goal_id: str, new_status: str,
                  agent: str = "unknown",
                  verdict: Optional[str] = None,
                  evidence: Optional[Dict[str, Any]] = None) -> bool:
    """Append one §5-shaped row for an ALLOWED candidate transition.

    Row shape (spec §5): {ts, agent, goal_id, verdict, evidence}.

    FAIL-OPEN by contract: any error (missing dir, lock contention, bad
    world_dir) is logged to stderr and returns False; the caller's
    transition still stands. The audit row is best-effort, never a
    blocker — same posture as the uncommitted-work override ledger
    (gates/uncommitted_work.py), which is the closest sibling idiom.

    `verdict` defaults to ALLOWED_TRANSITIONS[new_status]; pass an explicit
    one for the ambiguous `skipped` row (rb-route vs close-moot).

    Returns True when a row was appended OR the append was waived by
    pytest suppression (a waiver is not a failure — the transition stands
    either way); False only on a real append error.
    """
    # Pytest suppression () — the guard-1041 writer-side pattern
    # this module's contract promises (see _pytest_suppressed below and
    # _gate_log's GATE_LOG_ALLOW_PYTEST, ). An incidental allowed
    # candidate transition inside an unrelated e2e test must not append a
    # live ledger row — and with an unpinned MIND_WORLD that row would
    # land in the PRODUCTION world. Tests that assert rows opt in with
    # GATE_LOG_ALLOW_PYTEST=1.
    if _pytest_suppressed():
        return True
    from _fileops import locked_append_jsonl  # local: _fileops is heavy
    record = {
        "ts": _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "agent": agent or "unknown",
        "goal_id": goal_id,
        "verdict": verdict or ALLOWED_TRANSITIONS.get(new_status, "close-moot"),
        "evidence": {
            "from": CANDIDATE,
            "to": new_status,
            "rule": "goal-intake-management.md §2 transition table",
            **(evidence or {}),
        },
    }
    try:
        locked_append_jsonl(str(ledger_path(world_dir)), record)
        return True
    except Exception as exc:  # noqa: BLE001 — fail-open is the contract
        import sys as _sys
        print(f"[candidate-transition] ledger append failed for "
              f"{goal_id} -> {new_status}: {exc}", file=_sys.stderr)
        return False


def _pytest_suppressed() -> bool:
    """Pytest suppression () — the guard-1041 writer-side pattern,
    same shape as _gate_log's GATE_LOG_ALLOW_PYTEST (g-248-102): importing
    this module under pytest must not make live ledger writes from
    incidental code paths. Tests that need a row pass GATE_LOG_ALLOW_PYTEST=1.
    """
    return ("PYTEST_CURRENT_TEST" in _os.environ
            and "GATE_LOG_ALLOW_PYTEST" not in _os.environ)
