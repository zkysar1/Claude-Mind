""" — a direct blocker_ref field write cannot land an un-normalized dict.

Before this fix, `gates/blocker_ref.validate()` was reachable ONLY via the
`--blocker-ref` FLAG (paired with a defer_reason or status=blocked write). A
DIRECT field write — `aspirations-update-goal.sh <id> blocker_ref '<json>'` —
fell through to the generic `goal[field] = value` with no validation, no alias
normalization and no TTL. Measured 2026-07-27: of 11 live dict refs, ONE matched
validate()'s output shape and 6 carried no `expires_at` at all, so the TTL that
exists to force a Phase 0.5b re-probe never armed.

These tests pin the three properties the fix guarantees:
  1. validate() PRESERVES the promoted optional keys instead of stripping them
     (a blind strip would have destroyed `unblock_goal`, which
     blocked-signal-resolution-check._resolve_blocker_ref actively reads).
  2. Aliases fold onto ONE canonical spelling.
  3. Unknown keys are REFUSED, not silently dropped — so the vocabulary cannot
     grow a second spelling of a concept that already has one.

Pure-function tests against gates.blocker_ref: no daemon, no world writes, no
subprocess. Safe under any STORAGE_BACKEND.
"""
import re
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gates.blocker_ref import (  # noqa: E402
    BLOCKER_REF_CORE_KEYS,
    BLOCKER_REF_KEY_ALIASES,
    BLOCKER_REF_OPTIONAL_KEYS,
    BLOCKER_REF_TTL_HOURS,
    validate,
)

NOW = datetime(2026, 7, 27, 18, 0, 0)


def _ok(payload):
    ok, res = validate(payload, now=NOW)
    assert ok, f"expected accept, got refusal: {res}"
    return res


def _refused(payload):
    ok, res = validate(payload, now=NOW)
    assert not ok, f"expected refusal, got accept: {res}"
    return res


# --- 1. promoted keys survive -------------------------------------------

def test_unblock_goal_survives_validation():
    """The key a live reader consumes must NOT be stripped.

    blocked-signal-resolution-check._resolve_blocker_ref does
    `as_dict.get("unblock_goal") or as_dict.get("unblocking_goal")`. If
    validate() dropped it, normalizing a ref would destroy the only signal
    that reader can resolve, turning a resolvable block into an opaque one.
    """
    out = _ok({"type": "resource", "external_id": "x",
               "unblock_goal": "g-115-1"})
    assert out["unblock_goal"] == "g-115-1"


def test_why_survives_validation():
    out = _ok({"type": "resource", "external_id": "x", "why": "waiting on X"})
    assert out["why"] == "waiting on X"


def test_absent_optional_key_does_not_become_null():
    """An absent optional key stays ABSENT rather than becoming a null.

    Otherwise every ref without a rationale grows a `"why": null` and the
    stored shape accretes noise the readers then have to special-case.
    """
    out = _ok({"type": "resource", "external_id": "x"})
    for key in BLOCKER_REF_OPTIONAL_KEYS:
        assert key not in out


# --- 2. aliases fold to one spelling ------------------------------------

@pytest.mark.parametrize("alias,canonical,value", [
    ("unblocking_goal", "unblock_goal", "g-1-1"),
    ("unblocking_goal_id", "unblock_goal", "g-1-2"),
    ("reason", "why", "because"),
])
def test_alias_folds_onto_canonical(alias, canonical, value):
    out = _ok({"type": "resource", "external_id": "x", alias: value})
    assert out[canonical] == value
    assert alias not in out, f"{alias} must not survive alongside {canonical}"


def test_ref_alias_folds_onto_external_id():
    out = _ok({"type": "resource", "ref": "some-observable-id"})
    assert out["external_id"] == "some-observable-id"
    assert "ref" not in out


def test_canonical_wins_over_alias():
    """Explicit beats implied when both spellings arrive.

    This precedence is PRINCIPLED (the canonical spelling was supplied
    deliberately), which is why it resolves silently — contrast the
    alias-vs-alias case below, where no such principle exists.
    """
    out = _ok({"type": "resource", "external_id": "keep", "ref": "drop"})
    assert out["external_id"] == "keep"


def test_colliding_aliases_with_different_values_are_refused():
    """fresh-eyes-code F-001: two aliases of ONE concept, two different values.

    unblocking_goal and unblocking_goal_id both map to unblock_goal. Neither
    outranks the other, so a silent winner would be chosen by dict-insertion
    order and the loser discarded — the exact silent-drop this module refuses
    for unknown keys. Downstream, _resolve_blocker_ref would then point the
    blocked-signal sweep at whichever pointer happened to survive.
    """
    msg = _refused({"type": "resource", "external_id": "x",
                    "unblocking_goal": "g-AAA",
                    "unblocking_goal_id": "g-BBB"})
    assert "g-AAA" in msg and "g-BBB" in msg, (
        "the refusal must name BOTH conflicting values, not just the loser"
    )
    assert "unblock_goal" in msg


def test_colliding_aliases_with_same_value_still_normalize():
    """The narrowing guard: an idempotent double-write is NOT a conflict.

    Two spellings carrying the SAME value are unambiguous, so refusing them
    would be a spurious failure on a payload with exactly one meaning.
    """
    out = _ok({"type": "resource", "external_id": "x",
               "unblocking_goal": "g-SAME",
               "unblocking_goal_id": "g-SAME"})
    assert out["unblock_goal"] == "g-SAME"


def test_every_alias_targets_a_real_key():
    """Guards against an alias pointing at a key validate() would then reject."""
    allowed = set(BLOCKER_REF_CORE_KEYS) | set(BLOCKER_REF_OPTIONAL_KEYS)
    for alias, canonical in BLOCKER_REF_KEY_ALIASES.items():
        assert canonical in allowed, (
            f"alias {alias!r} maps to {canonical!r}, which is not an allowed key"
        )


# --- 3. unknown keys are REFUSED, not dropped ---------------------------

@pytest.mark.parametrize("bad_key", [
    "blocking_goal", "blocker_type", "blocker_id",
    "denied_action", "principal", "probe",
])
def test_unknown_key_is_refused(bad_key):
    msg = _refused({"type": "resource", "external_id": "x", bad_key: "v"})
    assert bad_key in msg, "the refusal must NAME the offending key"


def test_refusal_message_suggests_the_canonical_key():
    msg = _refused({"type": "resource", "external_id": "x",
                    "blocking_goal": "g-1-1"})
    assert "unblock_goal" in msg


def test_wholly_unrecognized_shape_is_refused():
    """The  shape: no `type` at all, eight unrecognized keys."""
    msg = _refused({
        "blocker_type": "x", "blocking_goal": "g", "denied_action": "d",
        "human_only_reason": "h", "principal": "p", "probe": "pr",
        "probed_at": "t", "probed_by": "b",
    })
    assert "blocker_type" in msg


# --- 4. the TTL that motivated the goal actually arms -------------------

def test_expires_at_is_auto_populated_when_absent():
    """The untimed-ref defect: 6 of 11 live refs had no expires_at, so the
    TTL that forces a Phase 0.5b re-probe never armed."""
    out = _ok({"type": "external-service", "external_id": "x"})
    assert out["expires_at"], "expires_at must auto-populate"
    assert out["expires_at"].startswith("2026-07-28"), (
        f"external-service TTL is {BLOCKER_REF_TTL_HOURS['external-service']}h "
        f"from {NOW.isoformat()}, got {out['expires_at']}"
    )


def test_supplied_expires_at_is_preserved():
    out = _ok({"type": "resource", "external_id": "x",
               "expires_at": "2026-12-25T00:00:00"})
    assert out["expires_at"] == "2026-12-25T00:00:00"


def test_core_keys_always_present():
    out = _ok({"type": "resource", "external_id": "x"})
    for key in BLOCKER_REF_CORE_KEYS:
        assert key in out


# --- 5. pre-existing contract still holds -------------------------------

def test_bad_type_still_refused():
    msg = _refused({"type": "not-a-real-type", "external_id": "x"})
    assert "type" in msg


def test_empty_external_id_still_refused():
    _refused({"type": "resource", "external_id": "   "})


def test_json_string_input_still_accepted():
    """The CLI passes a JSON-encoded string; the daemon passes a dict."""
    ok, out = validate('{"type":"resource","external_id":"x"}', now=NOW)
    assert ok and out["external_id"] == "x"


def test_validate_is_pure_no_input_mutation():
    """validate() must not mutate its caller's dict — the alias fold pops keys,
    so a shallow-copy bug here would corrupt the caller's payload."""
    payload = {"type": "resource", "external_id": "x", "reason": "r"}
    before = dict(payload)
    validate(payload, now=NOW)
    assert payload == before, "validate() mutated its input"


# --- 6. BOTH DOORS are wired (the inert-gate regression) ----------------
#
# bravo, findings msg-20260725-233143: "WIRING A GATE INTO
# aspirations.py::cmd_update_goal ALONE MAKES IT INERT."
# `aspirations-update-goal.sh` is daemon-only (rt_call POST, no
# _fallback_exec), so cmd_update_goal is the CLI/import lane and the DAEMON
# handler is where real traffic lands. A normalization wired only into the CLI
# would pass every pure-function test above and still never fire in production.
# These three tests are the ones that would catch that.

CORE_SCRIPTS = Path(__file__).resolve().parents[1]      # core/scripts
REPO = CORE_SCRIPTS.parents[1]                          # repo root (NOT core/)
DAEMON_WRITE = REPO / "mind_api" / "src" / "endpoints" / "aspirations_write.py"
CLI_WRITE = CORE_SCRIPTS / "aspirations.py"


def _assignment_offset(text):
    """Offset of the real `goal[field] = value` STATEMENT.

    Deliberately not `text.index("goal[field] = value")`: both files discuss
    that assignment in prose comments ABOVE it ("Moving this read below
    `goal[field] = value` would ..."), so a plain substring search returns a
    comment offset and the ordering assertion below compares against the wrong
    anchor. Caught by this test failing on correct code before the anchor was
    tightened — a positional assertion is only as good as what it anchors to.
    """
    m = re.search(r"^[ \t]*goal\[field\] = value[ \t]*$", text, re.MULTILINE)
    assert m, "could not locate the goal[field] = value statement"
    return m.start()


def _assert_guard_precedes_mutation(path, lane):
    text = path.read_text(encoding="utf-8")
    assert 'if field == "blocker_ref"' in text, (
        f"{lane} does not normalize a direct blocker_ref field write — "
        f"that lane is unguarded (g-115-3532)"
    )
    guard_at = text.index('if field == "blocker_ref"')
    assert guard_at < _assignment_offset(text), (
        f"{lane}: the blocker_ref normalization must run BEFORE the mutation, "
        f"otherwise the un-normalized dict has already landed"
    )


def test_daemon_lane_normalizes_direct_blocker_ref_write():
    """The hot path — `aspirations-update-goal.sh` is daemon-only, so this is
    where real traffic lands. A fix wired only into the CLI would pass every
    pure-function test above and still never fire in production."""
    _assert_guard_precedes_mutation(DAEMON_WRITE, "daemon handler")


def test_cli_lane_normalizes_direct_blocker_ref_write():
    """The CLI/import lane must agree with the daemon, or the two writers
    disagree on the stored shape (guard-330)."""
    _assert_guard_precedes_mutation(CLI_WRITE, "cmd_update_goal")


def test_update_goal_wrapper_is_still_daemon_only():
    """Pins the premise of the two tests above. If a CLI fallback is ever
    restored, the 'daemon is the hot path' reasoning must be re-derived."""
    sh = (CORE_SCRIPTS / "aspirations-update-goal.sh").read_text(encoding="utf-8")
    assert "rt_call" in sh and "/v1/aspirations/update-goal" in sh
    assert "_fallback_exec" not in sh, (
        "a Python CLI fallback reappeared "
        "(see .claude/rules/no-python-cli-fallback.md)"
    )
