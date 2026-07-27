"""Credential-source enumeration predicate — shared by BOTH blocker doors.

g-115-3158. This module is the SINGLE implementation of "does this
credentials-required blocker prove the grant is genuinely unavailable?".

WHY IT IS ITS OWN MODULE
------------------------
There are two independent doors through which a credentials-required blocker
enters the system, and until now only one of them checked anything:

  Door A — CREATE_BLOCKER -> `blocker-create-gate.py` -> check #5. Enforced.
  Door B — `aspirations-update-goal.sh` (defer_reason / status=blocked) with
           an `X-Mind-Blocker-Ref` / `--blocker-ref` payload. Ran only
           `gates.blocker_ref.validate()`, which checks the 5-key envelope
           and nothing about credentials. Un-enforced.

Door B is not a hypothetical: g-335-210 sat 90h asserting a human credential
mint was required, carrying a blocker_ref with NO enumeration at all — the
Door-B fingerprint. A duplicated predicate would drift; a shared one cannot.

DOOR B HAS TWO LANES, AND BOTH ARE LIVE
---------------------------------------
`aspirations-update-goal.sh` is daemon-only (`rt_call POST
/v1/aspirations/update-goal`, `rt_no_daemon_error` on rc=3, no CLI fallback),
so the DAEMON handler `mind_api/src/endpoints/aspirations_write.py` is the hot
path and `aspirations.py::cmd_update_goal` is the import/CLI lane. Both call
`blocker_ref.validate()` at two sites each (defer + status=blocked), so this
predicate is wired at FOUR call sites total. Gating only the CLI lane would
have produced a gate that never fires on the real traffic — the
"checker whose input does not mean what the checker assumes" defect class
(`world/knowledge/tree/system/system-constraints-loop/checker-input-assumption-defects.md`).

READ THE RAW PAYLOAD, NOT THE NORMALIZED ONE
--------------------------------------------
`blocker_ref.validate()` returns a REBUILT dict with exactly five keys
(type, external_id, state_hash, created_at, expires_at) — it SILENTLY DROPS
`credential_source_enumeration`. Any caller that hands this predicate
`validate()`'s output instead of the raw payload would be validating a field
that structurally cannot be present, and would refuse every credentials-
required blocker unconditionally. Call sites MUST pass the raw header/arg.

Daemon safety: pure. No I/O, no env reads, no clock. Mirrors the contract
`blocker_ref.validate()` states for itself.
"""
from __future__ import annotations

import json
from typing import Any

CHECK_NAME = "credential_enumeration"

# The one blocker type this predicate governs. Every other type is passed
# through untouched, so non-credentials writes stay byte-identical.
GOVERNED_TYPE = "credentials-required"


def _ok(reason: str) -> dict:
    return {"name": CHECK_NAME, "passed": True, "reason": reason}


def _fail(reason: str) -> dict:
    return {"name": CHECK_NAME, "passed": False, "reason": reason}


def check(payload: Any) -> dict:
    """Return {"name", "passed", "reason"} for a blocker or blocker_ref payload.

    Accepts a dict (Door A blocker record, daemon header dict) or a JSON string
    (CLI `--blocker-ref '<json>'`). Anything that is not a credentials-required
    blocker passes untouched.

    Requires `credential_source_enumeration`: a list of
    {source, identity, probed, denied} — one per credential source the runtime
    could resolve (e.g. an env pair, the default chain, a stored profile, an
    instance role). `probed` records that the source's identity + action were
    actually tested; `denied` records that the resolved identity CANNOT perform
    the action; `identity` is the resolved caller identity (null when the source
    is absent). Refuses when:
      (b1) fewer than 2 distinct sources are enumerated — one source cannot
           establish that no OTHER source holds the grant;
      (b2) any listed source is un-probed (`probed` != true) — an untested
           source is an untested self-service path;
      (c)  any source is NOT denied (`denied` != true) — that identity CAN
           perform the action, so the work is self-serviceable, not human-only
           (the pq-s3 failure mode; guard-1160);
      (a)  two sources resolve to the SAME non-null identity
           (pseudo-independence) — two labels for one identity is one source,
           and the agent may already hold the grant under the other label.

    Domain-agnostic: the source labels are supplied by the caller, not
    enumerated here.

    Shape errors (unparseable string, non-object) pass through. That is NOT a
    fail-open hole: `blocker_ref.validate()` owns envelope shape and runs
    BEFORE this predicate at every Door-B site, and Door A hands in a decoded
    record. Reporting a shape error here too would double-report the same
    defect under a check name that does not own it.
    """
    ref = payload
    if isinstance(ref, str):
        try:
            ref = json.loads(ref)
        except (json.JSONDecodeError, ValueError):
            return _ok("payload is not decodable here; shape is validate()'s "
                       "contract — check skipped")
    if not isinstance(ref, dict):
        return _ok("payload is not an object; shape is validate()'s contract "
                   "— check skipped")

    if (ref.get("type") or "") != GOVERNED_TYPE:
        return _ok(f"not a {GOVERNED_TYPE} blocker; check skipped")

    enum = ref.get("credential_source_enumeration")
    if not isinstance(enum, list) or not enum:
        return _fail(
            "credentials-required blocker without credential_source_enumeration: "
            "list each credential source as {source, identity, probed, denied} — "
            "its resolved identity (sts/whoami), whether it was actually probed, "
            "and whether that identity is denied the action. The pq-s3-deleteobject "
            "grant sat human-gated 86h while the root credential in the default CLI "
            "chain could already perform it — guard-1160 / g-248-111."
        )

    probed_sources = set()
    identities: dict = {}
    unprobed = []
    can_perform = []
    malformed = []
    for e in enum:
        if (not isinstance(e, dict) or not e.get("source")
                or "probed" not in e or "denied" not in e):
            malformed.append(e)
            continue
        src = str(e["source"])
        probed_sources.add(src)
        if not e.get("probed"):
            unprobed.append(src)
        if not e.get("denied"):
            can_perform.append(src)
        ident = e.get("identity")
        if ident:
            identities.setdefault(str(ident), []).append(src)

    if malformed:
        return _fail(
            f"credential_source_enumeration has {len(malformed)} malformed "
            "entry(ies); each must be an object with 'source', 'probed', and "
            "'denied' ('identity' may be null when the source is absent)."
        )

    if len(probed_sources) < 2:
        return _fail(
            f"only {len(probed_sources)} credential source enumerated; need >=2 "
            "distinct sources — one source cannot establish that no OTHER source "
            "holds the grant (g-248-111)."
        )

    if unprobed:
        return _fail(
            f"un-probed credential source(s): {unprobed}. Every enumerated source "
            "must set probed:true (its sts/whoami + action attempt was actually "
            "run) — an un-probed source is an untested self-service path."
        )

    if can_perform:
        return _fail(
            f"self-serviceable credential source(s): {can_perform} are NOT denied "
            "— that identity CAN perform the action, so this is agent-provisionable, "
            "not human-only. Route participants:[agent], do not file a "
            "credentials-required blocker (the pq-s3 failure mode; guard-1160)."
        )

    collisions = {ident: srcs for ident, srcs in identities.items() if len(srcs) >= 2}
    if collisions:
        ident, srcs = next(iter(collisions.items()))
        return _fail(
            f"pseudo-independent credential sources: {srcs} both resolve to "
            f"identity '{ident}'. Two labels for one identity is ONE source, not "
            "two — the agent may already hold the grant under the other label. "
            "Confirm the grant is genuinely absent before blocking (g-248-111)."
        )

    return _ok(
        f"{len(probed_sources)} credential sources enumerated, all probed, all "
        "denied, no pseudo-independence"
    )


def refusal_message(goal_id: str, reason: str, *, flag_hint: str) -> str:
    """Educational Door-B refusal text. `flag_hint` names the escape hatch in
    the caller's own vocabulary (a CLI flag or an HTTP header)."""
    return (
        f"BLOCKED: credentials-required blocker_ref on {goal_id} failed the "
        f"credential-enumeration check.\n"
        f"  {reason}\n"
        f"A credentials-required blocker asserts no identity available to this "
        f"agent can perform the action. That assertion must be PROVEN per source, "
        f"not stated — g-335-210 sat 90h on an unproven one. Add "
        f"credential_source_enumeration to the blocker_ref payload:\n"
        f"  \"credential_source_enumeration\": [\n"
        f"    {{\"source\": \"<label>\", \"identity\": \"<resolved caller>\", "
        f"\"probed\": true, \"denied\": true}}, ...\n"
        f"  ]\n"
        f"Same predicate as blocker-create-gate check #5 — one implementation, "
        f"both doors (g-115-3158). Genuine false positive: {flag_hint}"
    )
