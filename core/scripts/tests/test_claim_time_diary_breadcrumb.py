"""Pin the claim-time execution-diary breadcrumb ().

THE DEFECT. stranded-claim-sweep's liveness predicate is "an execution-diary
entry exists after claimed_at". Every diary-append point in aspirations-execute
Phase 4 is CONDITIONAL, and the unconditional phase_start/phase_end markers are
written by iteration-close -- at CLOSE, after the window has already elapsed. So
the uncovered window is claim -> first close phase, which for a deep goal is the
entire execution. Measured 2026-08-18 (zeta, cc-02): the sweep released
g-326-214 as "stranded" 26 minutes into an execution that had already opened and
merged a PR, against a 5-minute stale threshold.

The remedy is a write, not a threshold change, and not a second KEEP branch in
the sweep -- guard-4000 warns that a fail-safe KEEP which returns before reading
the record's age cannot collect its oldest cases. Writing the breadcrumb makes
the EXISTING predicate true by construction and adds no branch.

WHAT THIS FILE PROTECTS, and why each half is here:

1. ORDERING (the invariant with no other protection). The breadcrumb must sit
   ABOVE `[ -z "${claimed_by:-}" ] && return 0` in _post_claim_effects. Below it,
   the breadcrumb silently stops firing for any claim whose response carries no
   claimed_by -- a whole queue loses the fix, with nothing failing. Nothing else
   in the tree checks this, and it is a one-line move away at all times.

2. BEHAVIOUR through the PRODUCTION predicate. The emitted JSON is fed to
   stranded-claim-sweep._scan_diary_text -- the real function, not a regex
   rewritten here (guard-4323: validate a predicate through the production
   predicate, never an equivalent you write in the probe). Both negative
   directions are asserted too, so the file cannot pass against a predicate that
   simply returns True.

3. ENUM COUPLING. The entry_type must be in execution-diary.VALID_ENTRY_TYPES.
   cmd_append rejects an unknown type, and the breadcrumb is fail-open by design
   (`|| echo WARN`), so a rename of that enum would make the breadcrumb silently
   stop landing while the claim still succeeded -- exactly the silent-failure
   shape this whole goal is about.
"""
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CLAIM_SH = SCRIPTS / "aspirations-claim.sh"
SWEEP_PY = SCRIPTS / "stranded-claim-sweep.py"
DIARY_PY = SCRIPTS / "execution-diary.py"

# The literal the breadcrumb emits. Kept as one constant so a content reword has
# to come through here and re-run the behavioural assertions below.
BREADCRUMB_MARKER = "claim-time liveness breadcrumb"
EARLY_RETURN = '[ -z "${claimed_by:-}" ] && return 0'


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def claim_src():
    return CLAIM_SH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def sweep():
    return _load(SWEEP_PY, "stranded_claim_sweep")


@pytest.fixture(scope="module")
def diary():
    return _load(DIARY_PY, "execution_diary")


def test_breadcrumb_exists_in_the_claim_wrapper(claim_src):
    """Positive control for every other assertion in this file.

    Without this, a deleted breadcrumb would make the ordering test below pass
    vacuously (nothing to be out of order).
    """
    assert BREADCRUMB_MARKER in claim_src
    assert "execution-diary.sh" in claim_src


def test_breadcrumb_precedes_the_claimed_by_early_return(claim_src):
    """THE LOAD-BEARING ORDERING INVARIANT.

    _post_claim_effects returns early when the claim response carried no
    claimed_by. A breadcrumb below that return fires for world-queue claims and
    silently never fires for the other path -- no error, no warning, just a
    queue that quietly keeps the original defect.
    """
    assert EARLY_RETURN in claim_src, (
        "the early-return guard this ordering is defined against has moved or "
        "been reworded -- re-derive the invariant before editing this test"
    )
    assert claim_src.index(BREADCRUMB_MARKER) < claim_src.index(EARLY_RETURN)


def test_breadcrumb_is_fail_open(claim_src):
    """A breadcrumb failure must never fail a claim that already committed."""
    tail = claim_src[claim_src.index(BREADCRUMB_MARKER):]
    block = tail[: tail.index(EARLY_RETURN)]
    assert "|| echo" in block, "breadcrumb must degrade to a WARN, never fail the claim"


def test_emitted_entry_satisfies_the_production_sweep_predicate(sweep):
    """guard-4323: exercise the REAL predicate, not a regex rewritten here."""
    entry = {
        "entry_type": "observation",
        "goal_id": "g-115-6677",
        "content": f"{BREADCRUMB_MARKER} for g-115-6677 (source=world) - g-115-6677",
        "timestamp": "2026-08-18T22:47:10",
    }
    raw = json.dumps(entry)
    assert sweep._scan_diary_text(raw, "g-115-6677", "2026-08-18T22:37:10") is True


def test_predicate_still_discriminates(sweep):
    """Both negative directions -- otherwise the test above passes against a
    predicate that returns True unconditionally."""
    entry = {
        "entry_type": "observation",
        "goal_id": "g-115-6677",
        "content": f"{BREADCRUMB_MARKER} for g-115-6677 (source=world) - g-115-6677",
        "timestamp": "2026-08-18T22:47:10",
    }
    raw = json.dumps(entry)
    # an entry BEFORE the window start does not prove liveness in the window
    assert sweep._scan_diary_text(raw, "g-115-6677", "2026-08-18T23:59:00") is False
    # and it is never credited to a different goal
    assert sweep._scan_diary_text(raw, "g-999-99", "2026-08-18T22:37:10") is False


def test_entry_type_is_accepted_by_the_diary_writer(diary, claim_src):
    """The breadcrumb is fail-open, so an unknown entry_type would make it stop
    landing SILENTLY while the claim kept succeeding. Pin the coupling."""
    m = re.search(r'"entry_type"\s*:\s*"([a-z_]+)"[^\n]*' + re.escape(BREADCRUMB_MARKER),
                  claim_src)
    if m is None:
        # the printf puts entry_type first on the same line as the marker; if the
        # emitter is reformatted, fail loudly rather than silently skipping
        pytest.fail("could not locate the breadcrumb's entry_type in the emitter")
    assert m.group(1) in diary.VALID_ENTRY_TYPES
