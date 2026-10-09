"""`completed_by_harness`: which harness closed a worker goal ().

WHY THE FIELD EXISTS. The HIGH-goal pace forecast compares how long each kind of
worker takes per goal, and the kinds differ by harness, not by role: a Claude
Code worker session and a zakcode Body are both BODY_ROLE=worker. Measured
2026-10-07 over the completed goals in the live queue since 2026-09-30, start to
close: zakcode Body workers median 21.7 h (25 closes), Claude Code worker
sessions median 1.2 h (74 closes). Nothing on a closed goal recorded which
harness did the work, so the forecast had no population key.

WHAT THIS FILE PINS, mirroring test_completed_by_role_stamp.py, whose guard this
stamp shares:

  1. REGISTRY: the field is in GOAL_KNOWN_FIELDS, or the shared write path
     refuses the stamp and it is a silent no-op.
  2. WIRING: do_verify actually invokes the writer for this field.
  3. ONE RESOLVER: the value comes from _runtime.sh rt_judge_provenance, the
     resolver judge provenance already uses, never from a second hand-rolled
     read of CLAUDECODE / ZAKCODE_*.
  4. GUARD: the write sits under BODY_ROLE and status=completed, like the role
     stamp, and under a non-empty value, so an unresolvable harness writes
     nothing and ABSENT keeps meaning unknown.
  5. ORDERING: the stamp follows the status write.

Run:
  py -3 -m pytest core/scripts/tests/test_completed_by_harness_stamp.py -q
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

CORE_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(CORE_ROOT / "scripts"))

FIELD = "completed_by_harness"
CLOSE_SH = CORE_ROOT / "scripts" / "iteration-close.sh"


def _do_verify_body() -> str:
    """The text of iteration-close.sh's do_verify, cut at the next top-level function."""
    src = CLOSE_SH.read_text(encoding="utf-8")
    assert "do_verify()" in src, "iteration-close.sh no longer defines do_verify()"
    after = src.split("do_verify()", 1)[1]
    nxt = re.search(r"\n[A-Za-z_][A-Za-z0-9_]*\(\)\s*\{", after)
    return after[: nxt.start()] if nxt else after


def _stamp_index(lines: list[str]) -> int:
    """Index of the line that INVOKES the writer for FIELD. Requiring the writer on
    the same line keeps the failure message's echo, which also names the field,
    from passing for a write that is gone."""
    idx = next((i for i, ln in enumerate(lines)
                if FIELD in ln and "aspirations-update-goal.sh" in ln), None)
    assert idx is not None, (
        f"iteration-close.sh do_verify no longer INVOKES the {FIELD} write; the "
        f"pace forecast then has no harness on any close")
    return idx


def test_field_is_in_the_shared_write_allowlist():
    import _goal_fields

    assert FIELD in _goal_fields.GOAL_KNOWN_FIELDS, (
        f"{FIELD} missing from GOAL_KNOWN_FIELDS; the shared write path refuses "
        f"the stamp and iteration-close's write becomes a silent no-op")
    assert FIELD not in _goal_fields.GOAL_STRAY_FIELDS


def test_do_verify_stamps_the_harness():
    _stamp_index(_do_verify_body().splitlines())


def test_the_value_comes_from_rt_judge_provenance():
    """One resolver. A second hand-rolled read of the harness variables would
    drift from rt_judge_provenance the first time either changes."""
    lines = _do_verify_body().splitlines()
    value_var = re.search(r'"\$(\w+)"\s*\\?\s*$',
                          lines[_stamp_index(lines)].split(FIELD, 1)[1])
    assert value_var, f"cannot find the variable the {FIELD} write passes"
    name = value_var.group(1)
    assigns = [ln for ln in lines if re.match(rf"\s*{name}=", ln)]
    assert assigns, f"no assignment of ${name} in do_verify"
    assert any("rt_judge_provenance" in ln and "RT_JUDGE_HARNESS" in ln for ln in assigns), (
        f"${name} is not resolved by rt_judge_provenance: {assigns}")


def test_the_stamp_is_guarded_on_body_role_completed_and_a_value():
    """The write sits under BODY_ROLE and status=completed, like the role stamp,
    and under a non-empty value, so an unresolvable harness writes nothing."""
    lines = _do_verify_body().splitlines()
    idx = _stamp_index(lines)
    guards = [lines[j] for j in range(max(idx - 8, 0), idx) if "if [[" in lines[j]]
    joined = " ".join(guards)
    assert "BODY_ROLE" in joined, f"the {FIELD} write is not guarded on BODY_ROLE: {guards}"
    assert '"completed"' in joined, f"the {FIELD} write is not scoped to completed: {guards}"
    assert re.search(r'-n "\$_?\w*harness\w*"', joined, re.I), (
        f"the {FIELD} write is not guarded on a non-empty value: {guards}")


def test_the_stamp_runs_after_the_status_write():
    body = _do_verify_body()
    status_write = body.find('"$GOAL_ID" status "$GOAL_STATUS"')
    harness_write = body.find(FIELD)
    assert status_write != -1, (
        "could not locate do_verify's status write; re-anchor this assertion, "
        "do not delete it")
    assert harness_write > status_write, (
        f"the {FIELD} stamp precedes the status write, so it would land on a goal "
        f"that is not yet completed")
