"""Filing-time validator for structured verification checks ().

THE MEASUREMENT THAT MOTIVATED THIS. Running `predicate.evaluate()` against every
structured check on every OPEN world goal, 2026-08-07 (alpha, cc-07): **19 of 29
checks (66%) are SCHEMA-INVALID** -- 18 of them `unknown predicate type`. One more
is vacuous. Only 9 of 29 gate anything. The filing rate was 59% when g-115-5170 was
filed a day earlier, so it is getting worse, not better.

THE CHEAP TEST THIS RESTS ON, and it is the whole design:

    AT FILING TIME THE WORK HAS NOT BEEN DONE, SO ANY CHECK THAT PASSES IS
    VACUOUS BY DEFINITION.

That single observation makes a validator possible with no schema of its own. Run
the real evaluator and classify what it says:

    schema reason  -> REFUSE. The check names a type the evaluator does not have,
                      or omits a field that type requires. It can never gate
                      anything, in any world state.
    passed=True    -> WARN. Well-formed but satisfied before any work exists, so it
                      cannot distinguish done from not-done.
    work reason    -> ALLOW. This is the LOAD-BEARING NEGATIVE: a correct check on
                      undone work fails, and failing here is exactly right.

WHY REFUSE IS APPROPRIATE HERE and is not in the sibling closure gates: the author
is present at filing time and can fix it in seconds, and existing goals are
untouched because this only sees new filings. A closure gate blocks work already
done; this blocks a sentence that has just been typed.

WHY THIS IS WIRING AND NOT A FIFTH ENCODING. guard-921 already states the rule and
shows the correct form. Its utilization reads retrieval_count 18, times_helpful 0 --
the knowledge exists, is retrieved, and does not reach the moment of use (rb-840).

CLI/DAEMON PARITY IS THE POINT OF THIS BEING A MODULE. The daemon's `_validate_goal`
is a deliberate subset that "skips verification-schema" (aspirations_write.py:411),
and goal filing is daemon-routed -- so a validator living only in the CLI's
`validate_verification` would be INERT on every real filing while its own tests
passed. That is the guard-547 shape, and `gates.prose_verification` is the existing
answer to it: one module, called from both sides. This mirrors it exactly.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import _gate_log
except Exception:  # pragma: no cover - telemetry must never break a filing
    _gate_log = None


# Reasons that mean "this check is MALFORMED" -- it can never gate anything in any
# world state -- as opposed to "correctly formed, work not done yet".
#
# DERIVED BY ENUMERATING EVERY `reason=` LITERAL IN predicate.py AND JUDGING EACH,
# not from prose. The first draft of this list was copied from 's
# description and silently mis-classified SEVEN malformed shapes as fine --
# `missing anchor`, `missing command`, `missing or invalid delay_seconds`,
# `missing/invalid pr`, `missing/invalid repo`, `must specify since_goal...`, and
# `invalid anchor`. A false NEGATIVE is the worst direction for this gate: it
# reports a broken check as healthy, which is the exact defect being fixed.
#
# `test_check_schema_reason_coverage` pins this against predicate.py's literals, so
# a NEW reason string added there fails loudly instead of defaulting to "allow".
SCHEMA_REASONS = (
    "unknown predicate type",
    "not a dict",
    "evaluator error",
    "missing",          # missing required field / anchor / command / delay_seconds
                        # / missing-or-invalid pr / repo. No work-state reason in
                        # predicate.py contains this word -- verified by enumeration.
    "must specify",     # ...at least one of min,max / ...since_goal or after_ref
    "not in allowlist",
    "unresolvable",
    "unsupported",
    "invalid anchor",
)

# Reasons that are NOT schema problems even though they read like failures. Listed
# explicitly so the coverage test can assert every literal is judged rather than
# merely unmatched -- an unjudged reason and a deliberately-allowed one look
# identical from the outside, which is how a gap hides.
#
# ENTRIES ARE THE DISTINGUISHING SUBSTRING, NOT THE SHORTEST ONE THAT MATCHES. The
# first draft used bare "goal" and "json_length"; both occur INSIDE schema reasons
# ("missing required field: goal_id and after_ref", "unsupported extract mode ...
# json_length|exit_code"), so three malformed shapes were classified correctly only
# because classify() happens to test SCHEMA_REASONS first. Correct-by-order is not
# correct-by-judgment: reorder those two branches and those three shapes silently
# become "ok". `test_the_two_lists_do_not_overlap_ambiguously` is what caught it and
# is what keeps the two lists disjoint as predicate.py grows.
WORK_STATE_REASONS = (
    "ok",
    "found",                                # file_exists_after: 0 fresh of N
    "PR ",                                  # "PR {key} still {state} (not merged)"
    "has no completion timestamp",          # the goal exists; it is not done yet
    "not found in live or archive",
    "has no lastAchievedAt",
    "since_goal_last_achieved goal",        # ...{id} not found -- a MISSING ANCHOR
                                            # GOAL, not a missing field
    "timeout after",
    "command failed",
    "could not extract integer",            # command ran, output not parseable yet
    "json_length expects",
    "json_length parse error",
    "gh probe failed",                      # environmental, not the check's shape
    "git invocation failed",
    "git log rc=",
    "unparseable completion timestamp",     # bad data in ANOTHER record
    "unparseable lastAchievedAt",
)

# THE THIRD DISPOSITION. classify() consults the two lists above only AFTER testing
# `result.passed`, so a reason emitted alongside passed=True is intercepted by the
# vacuous branch and NEVER reaches either list. Such a reason is therefore neither
# schema-invalid nor work-state -- it is judged, just not by a list.
#
# Listing those reasons explicitly is what keeps the coverage test honest. Without
# this third bucket the test's model is binary while reality has three outcomes, so
# a passed=True reason reads as UNJUDGED and the only two remedies the failure
# message offers are both wrong: SCHEMA_REASONS would REFUSE a deliberately-filed
# check, and WORK_STATE_REASONS would assert "correctly formed, work not done" about
# a check that just reported passed=True. The second is the tempting one because it
# is inert TODAY -- the vacuous branch fires first -- and inert-today is exactly the
# correct-by-order reasoning the WORK_STATE_REASONS comment above rejects.
#
#  introduced the first member two commits after this gate's tests last ran
# green on another box, so the coverage test caught a real sibling-ordering drift on
# its first cross-box run. That is the mechanism working, not a flake.
SELF_PASSING_REASONS = (
    # not_machine_checkable: the author DECLARED the check unverifiable and the
    # evaluator returns passed=True with machine_checkable:False in observed_value.
    # Correct verdict is vacuous-with-warning (filing allowed, LLM verify still owes
    # the work) -- which is what classify() already produces without consulting this.
    "declared not machine-checkable",
)


def is_schema_failure(reason: Optional[str]) -> bool:
    """Is this passed=False reason a SCHEMA problem rather than a work-state one?

    The one place the SCHEMA_REASONS membership test lives. It was previously
    written inline in classify() only, which was fine while this gate was the
    sole consumer -- but the same question is asked at VERIFY time by
    verify-check-eval.py (g-115-4849), where a schema-broken check was
    byte-indistinguishable from a genuinely failing one and routed a succeeded
    goal back to `pending`. Two copies of the membership test would drift the
    first time predicate.py grows a reason, and nothing would fail when they
    did -- so both callers ask here instead.

    CALLERS MUST TEST `passed` THEMSELVES FIRST. This function judges a reason
    string in isolation and knows nothing about `passed`; a passed=True result
    carrying a SELF_PASSING_REASONS reason is intercepted by classify()'s
    vacuous branch BEFORE it ever gets here, and that ordering is load-bearing
    (see the SELF_PASSING_REASONS comment above)."""
    return any(s in (reason or "") for s in SCHEMA_REASONS)


def _predicate_module():
    """Import predicate lazily so a gate import cannot drag in its cost or its
    failure modes at module load (the daemon imports this at startup)."""
    import predicate  # noqa: PLC0415
    return predicate


def is_structured(check: Any) -> bool:
    """Is this check one the evaluator will actually try to evaluate?

    MIRRORS THE CONSUMER'S OWN FILTER -- `_extract_checks` in
    core/scripts/verify-check-eval.py, which is the single script that feeds
    verification.checks to predicate.evaluate_all:

        structured    = [c for c in checks if isinstance(c, dict) and c.get("type")]
        string_checks = [c for c in checks if isinstance(c, str)]

    THE GATE'S POPULATION MUST NOT BE WIDER THAN ITS CONSUMER'S. The first
    implementation of this module had no such filter, so classify() judged EVERY
    element of checks[] and returned "not a dict" for plain strings. Measured
    against the live world queue before shipping: of 758 checks on open goals,
    **729 (96%) are natural-language STRINGS** -- a sanctioned first-class format
    that verify-check-eval deliberately routes to LLM Q1/Q2/Q3 verification
    instead of the predicate evaluator. A gate blocking those would have refused
    the shape of 364 of the open goals and effectively stopped goal filing.

    A typeless dict is excluded for the same reason: the consumer requires a
    truthy `type` and silently skips anything without one, so a gate refusing it
    would enforce a rule no consumer holds.

    That leaves exactly 29 structured checks, which is the population the
    originating measurement was always about: 19 of those 29 (66%) are invalid.
    """
    return isinstance(check, dict) and bool(check.get("type"))


def valid_types() -> List[str]:
    """The predicate types the evaluator actually dispatches on.

    Read from PREDICATE_TYPES rather than restated here. A hand-copied list is the
    defect this gate exists to catch, one layer up: g-115-5170's own originating
    incident was a 5-goal set whose every type name was drawn from a stale list of
    'valid' names and was wrong (rb-6979).
    """
    return sorted(_predicate_module().PREDICATE_TYPES.keys())


def required_fields(ptype: str) -> List[str]:
    """Required fields for a type, discovered by ASKING THE EVALUATOR.

    Feeds `{"type": ptype}` with nothing else and reads the `missing required
    field: ...` reason back out. Deriving it this way means the message cannot
    drift from the evaluator the way a hardcoded table would -- and drift between
    a validator's idea of a schema and the real one is precisely how 18 goals came
    to carry type names that looked right.

    Returns [] when the evaluator does not report missing fields for this type.
    """
    try:
        r = _predicate_module().evaluate({"type": ptype})
    except Exception:
        return []
    reason = (r.reason or "")
    marker = "missing required field"
    if marker not in reason:
        return []
    tail = reason.split(marker, 1)[1].lstrip(": ").strip()
    if not tail:
        return []
    return [f.strip() for f in tail.replace(" and ", ", ").split(",") if f.strip()]


def classify(check: Any) -> Dict[str, Any]:
    """Classify ONE check into invalid | vacuous | ok.

    Never raises: a validator that can crash the filing path is worse than the
    malformed check it was written to catch.
    """
    if not is_structured(check):
        # NOT the gate's business -- the evaluator never sees this one either.
        # A natural-language string check is a legitimate format that routes to
        # LLM verification; a typeless dict is skipped by the consumer. Judging
        # either would make this gate stricter than the code it protects.
        return {"verdict": "skipped", "type": None,
                "reason": "not a structured check (string or typeless) — "
                          "verify-check-eval routes this to LLM Q1/Q2/Q3",
                "required": []}
    ptype = check.get("type")
    try:
        result = _predicate_module().evaluate(check)
    except Exception as exc:  # pragma: no cover - evaluate() claims never to raise
        return {"verdict": "invalid", "type": ptype,
                "reason": f"evaluator raised {type(exc).__name__}: {exc}",
                "required": []}

    reason = (result.reason or "")
    if is_schema_failure(reason):
        return {"verdict": "invalid", "type": ptype, "reason": reason,
                "required": required_fields(ptype) if ptype else []}
    if result.passed:
        return {"verdict": "vacuous", "type": ptype,
                "reason": "passes at filing time, before any work exists",
                "required": []}
    return {"verdict": "ok", "type": ptype, "reason": reason, "required": []}


def _format_invalid(idx: int, c: Dict[str, Any], types: List[str]) -> str:
    head = (f"  check[{idx}] type={c['type']!r}: {c['reason']}")
    if c["required"]:
        return head + f"\n      required fields for {c['type']!r}: {', '.join(c['required'])}"
    if c["type"] not in types:
        return head + f"\n      valid types: {', '.join(types)}"
    return head


def evaluate(goal: Dict[str, Any], *, meta_dir=None,
             agent_name: Optional[str] = None) -> Dict[str, Any]:
    """Evaluate a goal's verification.checks at filing time.

    Returns {"would_block", "invalid", "vacuous", "ok", "skipped", "message",
    "warning"}. `would_block` is True only when at least one STRUCTURED check is
    schema-invalid; a vacuous check produces `warning` and never blocks -- it is a
    real signal but a weaker one, and blocking on it would refuse checks that are
    merely optimistic rather than broken.

    `skipped` holds the checks this gate has no opinion about, because the
    evaluator has none either (see is_structured). On the live queue that is 96%
    of them, so the bucket is reported rather than dropped: a caller comparing
    len(invalid) against len(checks) would otherwise read a wildly wrong rate.
    """
    checks = ((goal or {}).get("verification") or {}).get("checks") or []
    if not isinstance(checks, list) or not checks:
        return {"would_block": False, "invalid": [], "vacuous": [], "ok": [],
                "skipped": [], "message": None, "warning": None}

    invalid, vacuous, ok, skipped = [], [], [], []
    for i, raw in enumerate(checks):
        c = classify(raw)
        c["index"] = i
        {"invalid": invalid, "vacuous": vacuous, "ok": ok,
         "skipped": skipped}[c["verdict"]].append(c)

    message = warning = None
    if invalid:
        types = valid_types()
        n_structured = len(invalid) + len(vacuous) + len(ok)
        message = (
            f"Goal {goal.get('id', '<new>')}: {len(invalid)} of {n_structured} "
            f"structured verification check(s) are schema-invalid and can never "
            f"gate anything:\n"
            + "\n".join(_format_invalid(c["index"], c, types) for c in invalid)
            + "\n  Fix the check or drop it — a check that cannot be evaluated is "
              "not weaker verification, it is none (guard-921, g-115-5170)."
        )
    if vacuous:
        warning = (
            f"Goal {goal.get('id', '<new>')}: {len(vacuous)} check(s) PASS at "
            f"filing time, before any work exists — "
            + ", ".join(f"check[{c['index']}] type={c['type']!r}" for c in vacuous)
            + ". A check satisfied before the work starts cannot distinguish done "
              "from not-done."
        )

    if _gate_log is not None:
        try:
            _gate_log.log(
                "verification-check-schema",
                "block" if invalid else ("noop" if not vacuous else "pass"),
                caller="gates.check_schema.evaluate",
                payload=str(goal.get("id", "<new>"))[:120],
                extra={"invalid": len(invalid), "vacuous": len(vacuous),
                       "ok": len(ok), "skipped": len(skipped),
                       "total": len(checks)},
                meta_dir=meta_dir, agent_name=agent_name,
            )
        except Exception:
            pass

    return {"would_block": bool(invalid), "invalid": invalid, "vacuous": vacuous,
            "ok": ok, "skipped": skipped, "message": message, "warning": warning}
