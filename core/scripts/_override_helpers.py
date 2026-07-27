"""Bulk gate-override translator + audit ledger.

Single source of truth for the `--override-all <justification>` consolidation
pattern (Phase 4 of the gate audit/retirement plan). Used by orchestrator
scripts (aspirations.py, create-blocker.py) that compose multiple gates and
need to let a caller bypass all of them with one justification instead of
remembering N per-gate flag names.

Design:
  - Per-gate flags (--override-duplication, --override-signal,
    --override-blocker-gate, --override-agent-match, --override) stay
    untouched for fine-grained control. Backward compatible.
  - The orchestrator adds --override-all "<justification>" alongside them.
  - apply_override_all() fans the bulk justification into every per-gate
    slot that wasn't explicitly set. Per-gate flags ALWAYS win — supplying
    both --override-all "X" and --override-duplication "Y" applies X
    everywhere except duplication, which gets Y.
  - audit_bulk_override() writes ONE record to
    world/override-bypass-ledger.jsonl per --override-all use, capturing the
    blast radius (which gates were silenced) so retrospective review can
    spot cases where a vague justification suppressed too many checks.

The ledger is distinct from gate-firings.jsonl. Per-gate firings already
log decision=override individually; the ledger correlates them by
override_token so the dashboard can answer "which agent runs hit the
bulk-override path vs fine-grained per-gate overrides."

Contract: never raises. Audit failures print a stderr WARN and continue.
"""

import datetime as _dt
import hashlib as _hashlib
import json as _json
import os as _os

from _paths import WORLD_DIR
from _fileops import locked_append_jsonl


# Canonical mapping from argparse `dest` attribute name → gate id in
# core/config/gates.yaml. Used by audit_bulk_override() to translate the
# slot list into the gate ids the bulk override actually silenced, so
# downstream consumers (gate-stats.py bulk-override correlation,
# gate-retirement-eval.py) can pivot by gate without re-doing the mapping.
#
# Keep in sync with gates.yaml: when a new override slot is added to the
# bulk pattern (callers of apply_override_all), add an entry here. Missing
# entries fall into `gate_ids_unmapped` in the record so the gap is
# visible in the audit instead of silently rendering as "unknown" at the
# consumer side ().
SLOT_TO_GATE_ID = {
    "override_signal": "origin-signal-gate",
    "override_duplication": "goal-duplication-gate",
    "override_uncommitted": "uncommitted-work-gate",
    "override_blocker_gate": "blocker-create-gate",
    "override_agent_match": "capability-gate",
    "override_offload": "operator-offload-gate",
    "override_no_investigate": "scaffolded-exploration-gate",
}


def apply_override_all(args, slot_attrs):
    """Fan args.override_all into per-gate slots that are still None.

    Mutates `args` in place. Returns (token, slots_filled) where token is
    a short hash of the justification (for cross-referencing the ledger
    with per-gate firing records) and slots_filled is the list of attr
    names that received the unified value.

    Returns (None, []) when no --override-all was supplied.
    """
    bulk = getattr(args, "override_all", None)
    if not bulk:
        return None, []
    token = _hashlib.sha1(bulk.encode("utf-8", errors="replace")).hexdigest()[:12]
    filled = []
    for attr in slot_attrs:
        # CRITICAL: `is None` (not falsy) — empty string per-gate flag means
        # "explicitly cleared by caller", not "unset". Bulk MUST NOT overwrite
        # it. Per-gate flag always wins over --override-all.
        if getattr(args, attr, None) is None:
            setattr(args, attr, bulk)
            filled.append(attr)
    return token, filled


def audit_bulk_override(token, justification, slots_filled, context, *,
                        world_dir=None):
    """Append one record to world/override-bypass-ledger.jsonl.

    `context` is a dict of free-form labels (caller, goal_id, source, etc.)
    surfaced verbatim in the record so the audit reader can pivot by them.
    Best-effort: any failure prints a stderr WARN and continues.

    Each slot in slots_filled is translated to its canonical gate id via
    SLOT_TO_GATE_ID and stored in `gate_ids` so downstream consumers
    (gate-stats.py, gate-retirement-eval.py) can pivot by gate without
    re-doing the mapping. Slots without a mapping appear in
    `gate_ids_unmapped` — visibility for audit gaps rather than silent
    fallback to "unknown" at the consumer side (g-281-01 fix).

    `world_dir` (keyword-only) overrides the module-level WORLD_DIR for
    daemon callers whose per-request world path differs from the import-
    time _paths value. CLI callers pass None and rely on the module-level
    constant.
    """
    if not token or not slots_filled:
        return
    gate_ids = []
    gate_ids_unmapped = []
    for slot in slots_filled:
        gid = SLOT_TO_GATE_ID.get(slot)
        if gid:
            gate_ids.append(gid)
        else:
            gate_ids_unmapped.append(slot)
    record = {
        "ts": _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "override_token": token,
        "justification": (justification or "")[:1000],
        "slots_filled": list(slots_filled),
        "gate_ids": gate_ids,
        "agent": _os.environ.get("MIND_AGENT", "") or None,
        "session_id": _os.environ.get("MIND_SID", "") or None,
        "context": context or {},
    }
    if gate_ids_unmapped:
        record["gate_ids_unmapped"] = gate_ids_unmapped
    ledger_root = world_dir if world_dir is not None else WORLD_DIR
    try:
        locked_append_jsonl(ledger_root / "override-bypass-ledger.jsonl", record)
    except Exception as e:
        # Audit failure must not break the caller. WARN so silent loss is visible.
        import sys as _sys
        print(f"[_override_helpers] WARN: ledger write failed: {e}",
              file=_sys.stderr)


def audit_cross_lane_claim(goal_id, agent_claiming, intended_agent,
                           justification, category=None, title=None, context=None):
    """Append one cross-lane-claim record to override-bypass-ledger.jsonl.

    Distinct from audit_bulk_override (which records argparse-slot fan-outs):
    this records a SINGLE-gate override fired at goal-claim time when the
    claimer's identity disagrees with goal.intended_agent (capability-route-
    gate.py's classification). Feeds asp-281's analyzer for taxonomy tuning
    over time — a high rate of cross-lane claims for a given (category,
    intended_agent → claimer) tuple is evidence the routing table is wrong.

    Schema mirrors audit_bulk_override but emits `gate` (single string)
    instead of `slots_filled` / `gate_ids` (lists). Consumer code that
    needs both forms should pivot on the presence of `slots_filled` vs.
    `gate` to discriminate the two record shapes. (g-282-07)

    Best-effort: failures print stderr WARN and continue.
    """
    if not justification or not goal_id:
        return
    token = _hashlib.sha1(justification.encode("utf-8", errors="replace")).hexdigest()[:12]
    merged_context = {
        "goal_id": goal_id,
        "agent_claiming": agent_claiming,
        "intended_agent": intended_agent,
    }
    if category:
        merged_context["category"] = category
    if title:
        merged_context["title"] = title[:200]
    if context:
        merged_context.update(context)
    record = {
        "ts": _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "override_token": token,
        "justification": (justification or "")[:1000],
        "gate": "capability-route-gate",
        "agent": _os.environ.get("MIND_AGENT", "") or None,
        "session_id": _os.environ.get("MIND_SID", "") or None,
        "context": merged_context,
    }
    try:
        locked_append_jsonl(WORLD_DIR / "override-bypass-ledger.jsonl", record)
    except Exception as e:
        import sys as _sys
        print(f"[_override_helpers] WARN: cross-lane ledger write failed: {e}",
              file=_sys.stderr)
