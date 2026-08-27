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


# ---------------------------------------------------------------------------
# Canonical key vocabulary ().
#
# Before this, validate() silently STRIPPED every key outside its 5-key output.
# That was data loss, not normalization: `unblock_goal` is actively read by
# blocked-signal-resolution-check.py (_resolve_blocker_ref, the `ug =` line),
# so a blind strip would have destroyed signal a live reader consumes. The
# measured corpus (11 live refs, 2026-07-27) carried THREE spellings of
# "the goal that unblocks this" and TWO of "free-text rationale".
#
# Resolution: promote the keys a reader actually consumes, normalize the
# aliases INTO them, and REJECT everything else at the write path. Absorbing
# variants one at a time is how a vocabulary reaches five spellings — the
# reader's own comment says so, and declines to add a third spelling for
# exactly that reason. This is the write-side half of that decision.
BLOCKER_REF_CORE_KEYS = (
    "type", "external_id", "state_hash", "created_at", "expires_at",
)

# Promoted optional keys — preserved through validate(), documented in
# goal-schemas.md. Each earns its place by having a live reader.
BLOCKER_REF_OPTIONAL_KEYS = (
    "unblock_goal",   # read by blocked-signal-resolution-check._resolve_blocker_ref
    "why",            # free-text rationale; surfaced in blocker_ref_why output
    "owner",          # deploy-hold reservations: the agent accountable for
                      # renewing or clearing this hold. Read by
                      # world/scripts/deploy-hold-check.sh, which surfaces it on
                      # the HELD verdict so a blocked pusher knows who to ask.
                      # REQUIRED on deploy-hold:* refs (see DEPLOY_HOLD_* below),
                      # optional everywhere else so no existing writer is
                      # disturbed. Note the module rule above — a promoted key
                      # earns its place by having a live reader, not by being
                      # useful in principle.
)


# ---------------------------------------------------------------------------
# Deploy-hold RESERVATIONS ().
#
# A deploy hold used to be an open-ended CLAIM: declared once, owned by nobody
# on a cadence, honored only by whoever happened to probe. This turns it into a
# LEASE — bounded, owned, and renewable — by refusing an unbounded declaration
# at the write path instead of hoping a later audit catches it.
#
# THE 48h WINDOW IS A FORCING FUNCTION, NOT AN ESTIMATE. A hold that genuinely
# needs longer is not forbidden; it is required to come back and say so, which
# is the whole difference between a lease and a claim. The audited override
# (allow_long_hold, below) is the sanctioned door for that case.
#
# GRANDFATHERING IS LOAD-BEARING — DO NOT REMOVE IT (guard-2400). validate() is
# pure: it sees a payload, never the stored record, so it cannot tell a FRESH
# declaration from a re-write of one that already exists. Without a cutoff,
# shipping this gate would make every pre-existing deploy-hold ref permanently
# unwritable BY EVERY WRITER — including the writes that would clear the hold —
# and the breakage would surface only when someone tried to write, as a
# rejection naming a field they did not send.
# MEASURED before shipping, 2026-08-26 (the count guard-2400 requires): the live
# world queue carried exactly TWO deploy-hold refs, and BOTH would have been
# refused —  (span 120h, no owner) and  (span 296h, no owner,
# and status=blocked, so the wedge would have blocked its own unblocking).
# Both predate the cutoff and are therefore exempt; every ref declared from the
# cutoff onward is governed.
# The cutoff is honest rather than airtight: a writer that back-dates created_at
# dodges the check. That is accepted deliberately — this is an internal fleet
# contract, and a forgeable field is a far smaller cost than a wedged store.
DEPLOY_HOLD_PREFIX = "deploy-hold:"
DEPLOY_HOLD_MAX_HOURS = 48
DEPLOY_HOLD_CONTRACT_EFFECTIVE_FROM = "2026-08-26T00:00:00"

# Keys ACCEPTED on input but deliberately NOT carried into the output.
#
# A separate gate reads these off the RAW payload BEFORE validate() runs:
# gates/credential_enum.py:113 does ref.get("credential_source_enumeration").
# That gate is why the key must be accepted — refusing it would reject every
# credentials-required blocker that carries its enumeration evidence. But it
# must equally NOT be emitted: credential_enum's whole design (and
# test_credential_enum_both_doors.test_blocker_ref_validate_drops_the_
# enumeration_field) depends on validate() returning a REBUILT 5-key dict, so
# that a guard reading validate's OUTPUT sees the field missing and cannot
# silently pass on it. Accepted-but-dropped is the only shape satisfying both.
#
# Found by widening a regression glob ('s own close ran
# blocker|defer|quiesc|aspiration and missed `credential` — the refusal
# shipped in 0c20f24e and was caught one iteration later). Before adding a key
# here, confirm it has a live RAW-payload reader; before adding one to
# BLOCKER_REF_OPTIONAL_KEYS instead, confirm its reader wants it in the OUTPUT.
BLOCKER_REF_PASSTHROUGH_KEYS = (
    "credential_source_enumeration",
)

# Input aliases → canonical key. Accepted on the way IN so existing writers
# keep working, but always stored under the canonical name so readers need
# exactly one spelling.
BLOCKER_REF_KEY_ALIASES = {
    "unblocking_goal":    "unblock_goal",
    "unblocking_goal_id": "unblock_goal",
    "reason":             "why",
    "ref":                "external_id",
}

# Keys seen in the wild that are deliberately NOT absorbed. Rejected with a
# pointed message rather than silently dropped, so the writer fixes the payload
# instead of the vocabulary growing a third spelling of a concept that already
# has one. `blocker_id` is included: the blocker's own identity belongs in
# external_id, which is what every reader already keys on.
BLOCKER_REF_REJECTED_KEYS = {
    "blocker_type":       "use `type`",
    "blocking_goal":      "use `unblock_goal`",
    "blocker_id":         "use `external_id`",
    "denied_action":      "put it in `why`",
    "human_only_reason":  "put it in `why`",
    "principal":          "put it in `why`",
    "probe":              "put it in `why`",
    "probed_at":          "put it in `why`",
    "probed_by":          "put it in `why`",
}


def _parse_iso(value: Any) -> Optional[datetime]:
    """Best-effort ISO-8601 parse. Returns None when unparseable, never raises.

    Tolerates a trailing 'Z' and sub-second precision because both appear in
    stored refs. A None return is treated as a REFUSAL by the deploy-hold
    checks below, never as a pass — an unparseable expiry is exactly the
    open-ended claim this contract exists to refuse.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "")[:19])
    except ValueError:
        return None


def _check_deploy_hold_reservation(ref: dict, ext_id: str, created_at: Any,
                                   expires_at: Any, *,
                                   allow_long_hold: bool = False
                                   ) -> Tuple[bool, Optional[str]]:
    """Enforce the deploy-hold reservation contract. Pure; no I/O.

    Returns (True, None) when the ref is a valid reservation OR is exempt,
    else (False, refusal_message).

    Every refusal message here is built by CONCATENATION, never .format() or
    %-formatting (guard-3803): these messages quote payload content and file
    paths, and a formatter cannot tell the message from the data it quotes. A
    ValueError raised while COMPOSING a refusal would be swallowed by a
    caller's fail-open handler and silently convert this refusal into an
    approval — the decision correct, complete, and never shipped.
    """
    created_dt = _parse_iso(created_at)
    effective_dt = _parse_iso(DEPLOY_HOLD_CONTRACT_EFFECTIVE_FROM)

    # Grandfather clause — see DEPLOY_HOLD_* above and guard-2400.
    if (created_dt is not None and effective_dt is not None
            and created_dt < effective_dt):
        return True, None

    owner = ref.get("owner")
    if not isinstance(owner, str) or not owner.strip():
        return False, (
            "blocker_ref '" + ext_id + "' is a deploy-hold reservation and "
            "must name an `owner` — the agent accountable for renewing or "
            "clearing it. A hold with no owner is an open-ended claim nobody "
            "is on the hook for, which is the dead-letter class this contract "
            "exists to remove. See world/conventions/deploy-holds.md."
        )

    # An explicit expiry is required: the per-type TTL default would silently
    # manufacture one, which is precisely the open-ended declaration being
    # refused. Read the RAW input, not the resolved value, so an auto-filled
    # expiry cannot satisfy the check that exists to demand a deliberate one.
    if not ref.get("expires_at"):
        return False, (
            "blocker_ref '" + ext_id + "' is a deploy-hold reservation and "
            "must carry an explicit `expires_at`. The per-type TTL default is "
            "not a reservation — a lease says when it ends, deliberately, and "
            "letting the default fill it in reintroduces the open-ended hold. "
            "See world/conventions/deploy-holds.md."
        )

    expires_dt = _parse_iso(expires_at)
    if expires_dt is None or created_dt is None:
        bad = "expires_at" if expires_dt is None else "created_at"
        return False, (
            "blocker_ref '" + ext_id + "' is a deploy-hold reservation whose "
            "`" + bad + "` is not a parseable ISO-8601 timestamp. An expiry "
            "that cannot be read is an expiry that cannot be enforced, so it "
            "is refused rather than assumed valid."
        )

    span_hours = (expires_dt - created_dt).total_seconds() / 3600.0
    if span_hours <= 0:
        return False, (
            "blocker_ref '" + ext_id + "' is a deploy-hold reservation whose "
            "`expires_at` is not after its `created_at` (span "
            + str(round(span_hours, 1)) + "h). A hold that expires before it "
            "begins gates nothing and reads as active."
        )

    if span_hours > DEPLOY_HOLD_MAX_HOURS and not allow_long_hold:
        return False, (
            "blocker_ref '" + ext_id + "' is a deploy-hold reservation "
            "spanning " + str(round(span_hours, 1)) + "h, over the "
            + str(DEPLOY_HOLD_MAX_HOURS) + "h reservation window. A hold that "
            "genuinely needs longer is not forbidden — it is required to come "
            "back and renew, which is the difference between a lease and a "
            "claim. Either shorten the window and renew before it lapses, or "
            "re-declare with the audited long-hold override. See "
            "world/conventions/deploy-holds.md."
        )

    return True, None


def validate(raw: Any, *, now: Optional[datetime] = None,
             allow_long_hold: bool = False
             ) -> Tuple[bool, Any]:
    """Parse and normalize a blocker_ref payload.

    Args:
        raw: Either a JSON-encoded string (CLI --blocker-ref shape) or an
            already-decoded dict (daemon X-Mind-Blocker-Ref header shape).
            Empty/None inputs return (False, error-string).
        now: datetime override for expires_at derivation. Defaults to
            datetime.now() at call time. Injectable for deterministic tests.
        allow_long_hold: audited override for the genuine long deploy-hold
            case. Only affects deploy-hold:* refs, and only the window check —
            an owner and an explicit expiry are still required, because those
            are what make a long hold accountable rather than merely long.
            Callers that pass True MUST record it via
            log_unstructured_override(..., which_checks_bypassed=
            ["deploy_hold_window"]) per the gate-overrides convention.

    Returns:
        (True, normalized_dict) on success — keys: type, external_id,
            state_hash, created_at, expires_at, plus any of the promoted
            optional keys (unblock_goal, why) that were supplied.
        (False, error_message_string) on any validation failure, including
            an unknown or rejected key (g-115-3532 — unknown keys are
            REFUSED, never silently stripped).
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

    # --- key normalization () -----------------------------------
    # Fold aliases onto their canonical name BEFORE any other check, so the
    # rest of this function only ever sees canonical spellings. A canonical
    # key already present wins over its alias (explicit beats implied).
    ref = dict(ref)
    # Which canonical keys were filled BY an alias (vs. supplied explicitly).
    # The distinction is load-bearing — see the two branches below.
    alias_filled = {}
    for alias, canonical in BLOCKER_REF_KEY_ALIASES.items():
        if alias in ref:
            value = ref.pop(alias)
            existing = ref.get(canonical)
            if existing in (None, ""):
                ref[canonical] = value
                alias_filled[canonical] = alias
            elif canonical in alias_filled and existing != value:
                # TWO ALIASES collide (e.g. unblocking_goal + unblocking_goal_id,
                # which both map to unblock_goal). Neither spelling outranks the
                # other, so the winner would be decided by dict-insertion order
                # alone and the loser discarded silently — the exact silent-drop
                # this module refuses for unknown keys. Refuse, naming both.
                # (fresh-eyes-code F-001, board msg-20260727-185028.)
                return False, (
                    f"blocker_ref carries conflicting values for {canonical!r}: "
                    f"{existing!r} (as {alias_filled[canonical]!r}) vs "
                    f"{value!r} (as {alias!r}). Neither spelling outranks the "
                    "other, so picking one would be arbitrary — supply exactly "
                    "one (g-115-3532)."
                )
            # Otherwise the canonical key was supplied EXPLICITLY in the input.
            # Explicit beats implied: the canonical wins and the alias is
            # dropped. That precedence is principled, not arbitrary — unlike the
            # alias-vs-alias case above — so it stays silent.

    # Passthrough keys are ACCEPTED here but never reach `out` below — see
    # BLOCKER_REF_PASSTHROUGH_KEYS for why both halves are load-bearing.
    allowed = (set(BLOCKER_REF_CORE_KEYS) | set(BLOCKER_REF_OPTIONAL_KEYS)
               | set(BLOCKER_REF_PASSTHROUGH_KEYS))
    unknown = [k for k in ref if k not in allowed]
    if unknown:
        hints = []
        for k in sorted(unknown):
            hint = BLOCKER_REF_REJECTED_KEYS.get(k)
            hints.append(f"{k} ({hint})" if hint else k)
        return False, (
            "blocker_ref carries unrecognized key(s): " + ", ".join(hints)
            + f". Allowed: {sorted(allowed)}. Unknown keys are REFUSED rather "
              "than silently dropped so the vocabulary cannot grow a second "
              "spelling of a concept that already has one (g-115-3532)."
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

    # Deploy-hold reservations carry a stricter contract than the generic ref
    # (). Keyed on the external_id PREFIX, not on `type`, because a
    # hold is declared under whichever type fits its cause (infrastructure,
    # resource, ...) — the prefix is what every existing reader already keys on
    # (world/scripts/deploy-hold-check.sh hold_ref()), so keying the gate the
    # same way keeps one vocabulary instead of introducing a second.
    if ext_id.strip().startswith(DEPLOY_HOLD_PREFIX):
        hold_ok, hold_err = _check_deploy_hold_reservation(
            ref, ext_id.strip(), created_at, expires_at,
            allow_long_hold=allow_long_hold,
        )
        if not hold_ok:
            return False, hold_err

    out = {
        "type": btype,
        "external_id": ext_id.strip(),
        "state_hash": state_hash,
        "created_at": created_at,
        "expires_at": expires_at,
    }
    # Carry the promoted optional keys through (). Only when
    # supplied — an absent `why` stays absent rather than becoming a null,
    # so the stored shape does not grow noise for refs that have no rationale.
    for key in BLOCKER_REF_OPTIONAL_KEYS:
        value = ref.get(key)
        if value not in (None, ""):
            out[key] = value
    return True, out


def log_unstructured_override(world_dir: Optional[Path], *,
                              goal_id: str,
                              defer_reason_text: str,
                              justification: str,
                              agent_name: str,
                              source: str = "daemon:update_goal:unstructured-defer",
                              which_checks_bypassed: Optional[list] = None,
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
        "which_checks_bypassed": which_checks_bypassed or ["blocker_ref_required"],
    }
    try:
        from _fileops import locked_append_jsonl
        locked_append_jsonl(log_path, record)
        return str(log_path)
    except Exception:
        return None
