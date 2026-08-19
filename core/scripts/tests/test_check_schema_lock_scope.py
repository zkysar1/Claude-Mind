"""test_check_schema_lock_scope.py — .

The verification-EDIT path in `update_goal` ran `_check_schema_eval` TWICE
while holding the world `aspirations.jsonl` lock. That chain
(`check_schema.evaluate` -> `classify` -> `predicate.evaluate`) reaches FIVE
`subprocess.run` sites in `core/scripts/predicate.py`:

    L277  resolve_after_ref        git show                (via any after_ref)
    L384  _eval_command_succeeds   arbitrary shell, <=120s
    L521  _eval_metric_threshold   arbitrary shell
    L668  _eval_vcs_commits_since  git
    L795  _eval_pr_merged          `gh pr view`  <- NETWORK call to GitHub

Measured on the two shell handlers alone: 20.0s + 20.0s = 40.0s of lock-held
subprocess at timeout_seconds=20, and up to 240s at the MAX_COMMAND_TIMEOUT
ceiling — blocking every agent's goal write fleet-wide for that long. The cost
is doubled BY CONSTRUCTION: a no-regression policy compares before against
after, so it pays the command cost twice.

The fix moves both evaluations ahead of the lock. What makes that safe is an
ASYMMETRY, and these tests pin it:

  * `new` needs NO goal load. `check_schema.evaluate` reads only
    goal["verification"]["checks"], so the synthetic `{"verification": value}`
    the pre-lock code builds is equivalent to the old in-lock candidate
    overlay. Zero TOCTOU. -> test_synthetic_goal_equivalent_to_candidate_overlay
  * `cur` IS read unlocked, so it can be stale — but only in one direction:
    a stale `cur` lists FEWER pre-existing invalid checks, which can only make
    `introduced` LARGER. The gate errs toward refusing, never toward a false
    PASS. -> test_stale_cur_can_only_refuse_never_falsely_pass

Per guard-3080 the no-regression cases below are written against the defining
PROPERTY (does the edit make the invalid set worse?) rather than against the
specific checks that happened to be broken when the goal was filed.
"""
from __future__ import annotations

import sys
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
REPO = CORE_SCRIPTS.parent.parent
DAEMON_WRITE = REPO / "mind_api" / "src" / "endpoints" / "aspirations_write.py"

sys.path.insert(0, str(CORE_SCRIPTS))

from gates.check_schema import evaluate as check_schema_eval  # noqa: E402


# `sig` / `introduced` mirror the endpoint's comparison verbatim. Kept here
# rather than imported because the endpoint defines them inline as a lambda;
# if that ever becomes a shared helper, import it instead of copying.
def _sig(result):
    return {(c["type"], c["reason"]) for c in result["invalid"]}


def _introduced(cur_goal_verification, new_verification):
    cur = check_schema_eval({"verification": cur_goal_verification})
    new = check_schema_eval({"verification": new_verification})
    return _sig(new) - _sig(cur)


# A check whose INVALIDITY is decided before any subprocess runs: the
# command_succeeds evaluator returns "missing command" ahead of
# subprocess.run. Using a schema-invalid check keeps these tests hermetic —
# nothing here may shell out, which is the whole point of the goal.
BROKEN = {"type": "command_succeeds"}                      # missing command
BROKEN_2 = {"type": "no_such_predicate_type_at_all"}       # unknown type

# FIXED must be schema-VALID *and* non-shelling. A valid command_succeeds
# would really run its command inside these tests, which would make them slow,
# environment-dependent, and — worst — reintroduce the very subprocess this
# goal is about. `not_machine_checkable` is valid, executes nothing, and lands
# in `vacuous`, which `_sig` ignores by design (vacuous warns, never blocks).
# The first draft used {"command": "pytest -q"} and the suite caught it: bare
# `pytest` is not in ALLOWED_COMMAND_PREFIXES, so it scored invalid. That
# failure is itself evidence for this goal's central claim — "not in allowlist"
# is returned BEFORE any subprocess runs.
FIXED = {"type": "not_machine_checkable", "reason": "human judgement"}


def _verif(*checks):
    return {"outcomes": ["something happened"], "checks": list(checks)}


# ── the equivalence that removes the TOCTOU on `new` ──────────────────────

def test_synthetic_goal_equivalent_to_candidate_overlay():
    """`{"verification": v}` == a full goal record carrying the same v.

    This is the load-bearing claim of the pre-lock move: if these ever
    diverge, the pre-lock `new` is measuring something different from what
    the in-lock candidate overlay measured, and the gate's verdict changes
    silently.
    """
    v = _verif(BROKEN, FIXED)
    fat_goal = {
        "id": "g-000-1", "title": "unrelated", "status": "pending",
        "priority": "HIGH", "description": "x" * 200,
        "participants": ["agent"], "verification": v,
    }
    synthetic = check_schema_eval({"verification": v})
    overlay = check_schema_eval(fat_goal)

    assert _sig(synthetic) == _sig(overlay)
    assert synthetic["would_block"] == overlay["would_block"]
    assert len(synthetic["invalid"]) == len(overlay["invalid"])


def test_synthetic_equivalence_holds_when_nothing_is_invalid():
    """Anti-vacuity for the test above: it must not pass merely because both
    sides are empty in every case it is exercised with."""
    v = _verif(FIXED)
    fat = {"id": "g-000-2", "status": "pending", "verification": v}
    assert _sig(check_schema_eval({"verification": v})) == _sig(check_schema_eval(fat))
    # ...and the previous test's fixture really does produce a non-empty set,
    # so the equality there is not trivially true.
    assert _sig(check_schema_eval({"verification": _verif(BROKEN, FIXED)}))


# ── the three no-regression cases named in the goal's verification ────────

def test_fixing_a_broken_check_passes():
    assert _introduced(_verif(BROKEN), _verif(FIXED)) == set()


def test_leaving_checks_alone_passes():
    assert _introduced(_verif(BROKEN), _verif(BROKEN)) == set()


def test_adding_a_new_broken_kind_is_refused():
    introduced = _introduced(_verif(BROKEN), _verif(BROKEN, BROKEN_2))
    assert introduced, "a NEW schema-invalid kind must be reported"
    assert any(t == "no_such_predicate_type_at_all" for t, _ in introduced)


def test_reordering_checks_is_not_a_regression():
    """Signatures are (type, reason) SETS, so order must not matter."""
    assert _introduced(_verif(BROKEN, FIXED), _verif(FIXED, BROKEN)) == set()


# ── the direction of the benign race on `cur` ─────────────────────────────

def test_stale_cur_can_only_refuse_never_falsely_pass():
    """A stale `cur` sees FEWER pre-existing invalid checks.

    `introduced = sig(new) - sig(cur)`, so dropping entries from `cur` can
    only GROW `introduced`. That is the direction that refuses. There is no
    input for which a smaller `cur` yields a smaller `introduced`.
    """
    fresh_cur = _verif(BROKEN, BROKEN_2)   # what the locked read would see
    stale_cur = _verif(BROKEN)             # an older record, missing one

    new = _verif(BROKEN, BROKEN_2)
    with_fresh = _introduced(fresh_cur, new)
    with_stale = _introduced(stale_cur, new)

    assert with_fresh <= with_stale, "staleness must never shrink `introduced`"
    assert with_fresh == set(), "no regression against the fresh record"
    assert with_stale, "the stale read errs toward refusing — the safe direction"


# ── regression guard on the ordering itself ───────────────────────────────

def test_eval_calls_precede_the_update_goal_lock():
    """INSPECTION-GRADE, and labelled as such.

    The goal asks for a timing probe rather than inspection, and the tests
    above are behavioural. This one is a cheap tripwire for the specific
    regression of moving the evaluation back inside the lock — it cannot
    prove absence of shell-out under lock, only that the call sites sit
    ahead of the `update_goal` critical section.
    """
    src = DAEMON_WRITE.read_text(encoding="utf-8").splitlines()
    lo, hi = _function_bounds(src, "update_goal")

    eval_lines = [i for i in range(lo, hi)
                  if "_check_schema_eval(" in src[i]
                  and not src[i].lstrip().startswith("#")]
    assert len(eval_lines) == 2, (
        f"expected the cur/new pair inside update_goal, found {eval_lines}")

    locks = [i for i in range(lo, hi)
             if "with file_locks.locked(live_path):" in src[i]]
    assert locks, "update_goal should still take the aspirations lock"

    inside = [i for i in eval_lines if _enclosing_lock(src, lo, i) is not None]
    assert not inside, (
        f"check-schema evaluation is INSIDE update_goal's lock (lines {inside}) "
        "— it shells out at five sites in predicate.py (one a network call to "
        "GitHub) and holding aspirations.jsonl across that blocks every "
        "agent's goal write fleet-wide. See g-115-5357."
    )


def _enclosing_lock(src, lo, target):
    """Line index of the `with file_locks.locked(...)` enclosing `target`, else None.

    ASKS THE DEFINING PROPERTY — "is this call nested inside an open lock?" —
    rather than a positional one. Two successive positional formulations were
    both wrong, and each looked right:

      1. "evals come after the marker, before the next lock" — on the pre-fix
         source the enclosing lock precedes the marker, so the search matched a
         DIFFERENT endpoint's lock ~880 lines on and passed on broken code.
      2. "max(evals) < min(locks)" — `update_goal` holds TWO locks, and the
         first (the Layer-D auto-Unblock filing, inside an early-return branch
         the main path never reaches) sits ahead of the evals, so this failed
         on CORRECT code.

    Nesting is what the goal is actually about, so test nesting. Indentation is
    a sound proxy here because the file is uniformly 4-space indented Python.
    """
    enclosing = None
    for i in range(lo, target):
        line = src[i]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if enclosing is not None and indent <= enclosing[1]:
            enclosing = None          # dedented back out of the with-block
        if "with file_locks.locked(" in line:
            enclosing = (i, indent)
    if enclosing is None:
        return None
    t_indent = len(src[target]) - len(src[target].lstrip())
    return enclosing[0] if t_indent > enclosing[1] else None


def _function_bounds(src, name):
    """[start, end) line indices of a top-level `def name(` block.

    SCOPING TO THE FUNCTION IS THE WHOLE POINT, and the first draft of this
    test got it wrong in a way that made the guard VACUOUS — it anchored on
    the `if field == "verification":` marker and then looked for the NEXT
    lock in the file. On the pre-fix source the enclosing lock sits BEFORE
    that marker, so the search skipped past it and matched a different
    endpoint's lock ~880 lines later, making `max(evals) < min(locks)`
    trivially true. The guard passed on the very code it was written to
    reject. Caught by re-running the assertion against
    `git show HEAD:...aspirations_write.py`; that mutation check is the only
    reason it was found, and it is worth repeating on any future edit here.
    """
    start = next(i for i, l in enumerate(src) if l.startswith(f"def {name}("))
    end = next((i for i in range(start + 1, len(src))
                if src[i].startswith("def ") or src[i].startswith("class ")),
               len(src))
    return start, end
