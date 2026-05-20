"""Blocker-reference schema + validator — daemon-safe extraction (PR 7e/2).

Single source of truth for the blocker_ref type enum, per-type TTL, and
the validate() function that parses and normalizes incoming blocker_ref
payloads.

Public API:
    BLOCKER_REF_TYPES         (tuple of valid type strings)
    BLOCKER_REF_TTL_HOURS     (dict[type, hours])
    validate(raw, *, now=None) -> Tuple[bool, dict_or_error_str]
    log_unstructured_override(world_dir, *, goal_id, defer_reason_text,
                              justification, agent_name) -> None

Schema:
    {
      "type":         <one of BLOCKER_REF_TYPES>,         REQUIRED
      "external_id":  <non-empty string>,                 REQUIRED
      "state_hash":   <string or null>,                   OPTIONAL
      "created_at":   <ISO 8601 string>,                  OPTIONAL (auto)
      "expires_at":   <ISO 8601 string>,                  OPTIONAL (auto from TTL)
    }

SCHEMA CONTRACT: every narrative defer_reason MUST be paired with a
structured blocker_ref so the quiescence gate can distinguish genuine
external gating from narrative laundering. Without this, the gate is
trivially gameable via free-text defer_reason writes. See
`.claude/rules/probe-before-defer.md` and
`core/config/conventions/goal-schemas.md`.

When adding a new blocker type:
  1. Add to BLOCKER_REF_TYPES here
  2. Add a TTL entry to BLOCKER_REF_TTL_HOURS
  3. Update core/config/conventions/goal-schemas.md
  4. Update create-blocker.py --blocker-type choices
  5. Update the quiescence-gate eligibility check

Daemon safety:
  - validate() is pure: no I/O, no env reads. `now` is injectable for tests.
  - log_unstructured_override() does ONE locked append, fail-silent.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Tuple


# Canonical type enum. Order is convention-display order, not match priority.
BLOCKER_REF_TYPES = (
    "infrastructure",
    "resource",
    "user_action",
    "credentials-required",
    "security-trust",
    "physical-hardware",
    "partner-response",          # cites board msg ID awaiting reply
    "external-service",          # cites scheduled probe ID
)

# Per-type TTL in hours. Expiry converts the blocker to an Unblock goal via
# aspirations-precheck Phase 0.5b re-probe, which disqualifies quiescence
# and forces the loop to actively resolve. 120h default matches the existing
# defer_reason_timeout_hours in goal-selector.py.
BLOCKER_REF_TTL_HOURS = {
    "partner-response":       72,    # matches handoff_aging.escalate_hours
    "external-service":       24,    # probe should be re-scheduled within a day
    "user_action":           120,    # matches defer_reason_timeout_hours
    "infrastructure":        120,
    "resource":              120,
    "credentials-required":  120,
    "security-trust":        120,
    "physical-hardware":     120,
}


def validate(raw: Any, *, now: Optional[datetime] = None
             ) -> Tuple[bool, Any]:
    """Parse and normalize a blocker_ref payload.

    Args:
        raw: Either a JSON-encoded string (CLI --blocker-ref shape) or an
            already-decoded dict (daemon X-Mind-Blocker-Ref header shape).
            Empty/None inputs return (False, error-string).
        now: datetime override for expires_at derivation. Defaults to
            datetime.now() at call time. Injectable for deterministic tests.

    Returns:
        (True, normalized_dict) on success — keys: type, external_id,
            state_hash (optional), created_at, expires_at.
        (False, error_message_string) on any validation failure.
    """
    if raw is None or raw == "":
        return False, "blocker_ref is required (pass --blocker-ref '<json>')"

    if isinstance(raw, str):
        try:
            ref = json.loads(raw)
        except json.JSONDecodeError as e:
            return False, f"blocker_ref not valid JSON: {e}"
    else:
        ref = raw

    if not isinstance(ref, dict):
        return False, (
            f"blocker_ref must be a JSON object, got {type(ref).__name__}"
        )

    btype = ref.get("type")
    if btype not in BLOCKER_REF_TYPES:
        return False, (
            f"blocker_ref.type must be one of {list(BLOCKER_REF_TYPES)}, "
            f"got {btype!r}"
        )

    ext_id = ref.get("external_id")
    if not isinstance(ext_id, str) or not ext_id.strip():
        return False, "blocker_ref.external_id must be a non-empty string"

    state_hash = ref.get("state_hash")
    if state_hash is not None and not isinstance(state_hash, str):
        return False, "blocker_ref.state_hash must be a string or null"

    now_dt = now if now is not None else datetime.now()
    created_at = ref.get("created_at") or now_dt.isoformat(timespec="seconds")
    expires_at = ref.get("expires_at")
    if not expires_at:
        ttl = BLOCKER_REF_TTL_HOURS[btype]
        expires_at = (now_dt + timedelta(hours=ttl)).isoformat(timespec="seconds")

    return True, {
        "type": btype,
        "external_id": ext_id.strip(),
        "state_hash": state_hash,
        "created_at": created_at,
        "expires_at": expires_at,
    }


def log_unstructured_override(world_dir: Optional[Path], *,
                              goal_id: str,
                              defer_reason_text: str,
                              justification: str,
                              agent_name: str,
                              source: str = "daemon:update_goal:unstructured-defer"
                              ) -> Optional[str]:
    """Append an override record to world/blocker-gate-overrides.jsonl.

    Best-effort: any write error is swallowed (returns None). The override
    was already granted by the caller — failed logging must not block the
    write. Mirrors the legacy aspirations.py:_log_unstructured_defer_override
    contract; same target file, same record shape.

    Args:
        world_dir: WORLD_DIR. None when no agent binding — skipped silently.
        goal_id: The goal whose defer_reason is being written.
        defer_reason_text: The defer narrative (truncated to 200 chars).
        justification: The override justification (free text).
        agent_name: Filing agent (or "unknown").
        source: Audit-trail "where this override came from" label. Daemon
            and CLI pass different values so the consumer can pivot.

    Returns:
        The log path on success, None on skip or write failure.
    """
    if world_dir is None:
        return None
    log_path = world_dir / "blocker-gate-overrides.jsonl"
    record = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "agent": agent_name or "unknown",
        "source": source,
        "goal_id": goal_id,
        "defer_reason": str(defer_reason_text)[:200],
        "justification": justification,
        "which_checks_bypassed": ["blocker_ref_required"],
    }
    try:
        from _fileops import locked_append_jsonl
        locked_append_jsonl(log_path, record)
        return str(log_path)
    except Exception:
        return None
