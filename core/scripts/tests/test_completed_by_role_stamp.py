"""`completed_by_role` — the worker-vs-reducer close provenance stamp ().

WHY THE FIELD EXISTS. Nothing durable and fleet-visible recorded WHICH ROLE
closed a goal, so no audit could compute a worker-vs-reducer rate of anything —
which is what g-306-204's "artifact-production-rate check" needs as its
population key. The one marker the design named, worker-loop Phase 3.9's
`--prefix "[worker-loop] close:"`, is only ever used in
closure-evidence-write.sh's own echo lines and is NEVER written into the record.
Measured 2026-08-22 over the whole world store (18.4 MB, 2,860 goals, 655
completed, 534 carrying an outcome_note): the marker appears on ZERO goals and
could not have appeared on any. Body manifests are box-local and worker refs are
consumed on merge, so neither is a durable fleet-visible discriminator either.

WHAT THIS FILE PINS, and why each half is here:

  1. REGISTRY — the field is in GOAL_KNOWN_FIELDS. Without it the shared write
     path refuses the write and the stamp is inert.
  2. WIRING — iteration-close.sh's do_verify actually performs the write, under
     BOTH guards. Registry membership alone proves nothing about whether
     anything writes it (guard-1943: pinning the writer says nothing about the
     wiring, and the inverse holds too).
  3. ORDERING — the stamp comes AFTER the status write. A stamp emitted before
     the status write would land `completed_by_role` on a goal that is not yet
     (and may never become) completed, which is the exact incoherent-provenance
     shape aspirations_write.py's own comments describe for the
     completed_at/completed_by/completed_by_sid triple.
  4. NEGATIVE CONTROL — the write is not reachable unguarded. `absent` must keep
     meaning reducer-or-unknown; a stamp that fired with BODY_ROLE empty would
     write an empty string and destroy that meaning.

THE ASYMMETRY IS DELIBERATE AND MUST NOT BE "FIXED". bash-agent-inject.py
exports BODY_ROLE ONLY on the worker fork path, so the reducer never sets it.
PRESENT+"worker" positively identifies a worker close; ABSENT means
reducer-OR-unknown and must never be read as "reducer". Any consumer treating
this as a partition of all closes is wrong.

Run:
  py -3 -m pytest core/scripts/tests/test_completed_by_role_stamp.py -q
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

CORE_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(CORE_ROOT / "scripts"))

FIELD = "completed_by_role"
# CORE_ROOT is already <repo>/core (parent.parent.parent of this file), so the
# script dir is CORE_ROOT/"scripts" — NOT CORE_ROOT/"core"/"scripts".
CLOSE_SH = CORE_ROOT / "scripts" / "iteration-close.sh"


def _do_verify_body() -> str:
    """The text of iteration-close.sh's do_verify function.

    Sliced by the next top-level `function-name() {` rather than by a fixed
    length, so the assertions below cannot silently start reading a neighbour
    when the file grows.
    """
    src = CLOSE_SH.read_text(encoding="utf-8")
    assert "do_verify()" in src, "iteration-close.sh no longer defines do_verify()"
    after = src.split("do_verify()", 1)[1]
    nxt = re.search(r"\n[A-Za-z_][A-Za-z0-9_]*\(\)\s*\{", after)
    return after[: nxt.start()] if nxt else after


def test_field_is_in_the_shared_write_allowlist():
    """Registry half. `aspirations-update-goal.sh` refuses fields outside
    GOAL_KNOWN_FIELDS, so an unregistered field makes the stamp a silent no-op —
    which is precisely the self-concealing failure _goal_fields.py was created
    to stop.
    """
    import _goal_fields

    assert FIELD in _goal_fields.GOAL_KNOWN_FIELDS, (
        f"{FIELD} missing from GOAL_KNOWN_FIELDS — the shared write path will "
        f"refuse the stamp and iteration-close's write becomes a silent no-op"
    )
    assert FIELD not in _goal_fields.GOAL_STRAY_FIELDS, (
        f"{FIELD} is listed as a STRAY field while a live writer sets it"
    )


def _stamp_invocation(body: str):
    """The line that actually INVOKES the writer, or None.

    Requiring `aspirations-update-goal.sh` on the same line is load-bearing, not
    belt-and-braces: the first cut of this test asserted only `FIELD in body`
    and mutation-proof-test.sh certified it VACUOUS — deleting the write line
    left the `|| echo "... completed_by_role stamp failed ..."` fallback behind,
    whose text contains the field name, so a bare substring match passed against
    code with no writer at all. A grep for a name is not a test that something
    happens.
    """
    return next(
        (ln for ln in body.splitlines()
         if FIELD in ln and "aspirations-update-goal.sh" in ln),
        None,
    )


def test_do_verify_stamps_the_role():
    """Wiring half: do_verify must actually issue the write."""
    body = _do_verify_body()
    assert _stamp_invocation(body) is not None, (
        "iteration-close.sh do_verify no longer INVOKES the completed_by_role "
        "write — the field may still be named in a comment or an error message, "
        "but nothing sets it, so every close records nothing about which role "
        "produced it"
    )


def test_the_stamp_is_guarded_on_both_body_role_and_completed():
    """Negative control. An unguarded stamp writes an empty role on reducer
    closes, which destroys the meaning of ABSENT (reducer-or-unknown) that every
    consumer of this field depends on.
    """
    body = _do_verify_body()
    stamp_line = _stamp_invocation(body)
    assert stamp_line is not None, f"no {FIELD} write found in do_verify"

    # The guard is the `if` above the write. Take the nearest preceding `if [[`.
    lines = body.splitlines()
    idx = next(i for i, ln in enumerate(lines) if ln is stamp_line or ln == stamp_line)
    guard = next(
        (lines[j] for j in range(idx - 1, max(idx - 6, -1), -1) if "if [[" in lines[j]),
        "",
    )
    assert "BODY_ROLE" in guard, (
        f"the {FIELD} write is not guarded on BODY_ROLE (guard line: {guard!r}) — "
        f"it would stamp an empty value on reducer closes and ABSENT would stop "
        f"meaning reducer-or-unknown"
    )
    assert "completed" in guard, (
        f"the {FIELD} write is not scoped to status=completed (guard line: "
        f"{guard!r}) — provenance on a non-completed goal is the incoherent "
        f"shape the completed_by/_sid triple exists to avoid"
    )


def test_the_stamp_runs_after_the_status_write():
    """Ordering. Stamping before the status write would attach completion
    provenance to a goal that is not yet completed.
    """
    body = _do_verify_body()
    status_write = body.find('"$GOAL_ID" status "$GOAL_STATUS"')
    role_write = body.find(FIELD)
    assert status_write != -1, (
        "could not locate do_verify's status write — this test's ordering "
        "assertion is anchored to it and must be re-anchored, not deleted"
    )
    assert role_write > status_write, (
        f"the {FIELD} stamp (offset {role_write}) precedes the status write "
        f"(offset {status_write}) — completion provenance would land on a goal "
        f"that is not yet completed"
    )


if __name__ == "__main__":
    failures = []
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as exc:
            failures.append(name)
            print(f"FAIL {name}: {exc}")
    print(f"{'TEST FAIL' if failures else 'TEST PASS'} ({len(failures)} failed)")
    sys.exit(1 if failures else 0)
