"""test_completed_not_committed_reachability.py — regression for  class 2.

Tier 1 decides "committed but not pushed" from a LOCAL-vs-REMOTE BOOLEAN:
`git cat-file -e` succeeds and `git branch -r --contains` is empty, therefore
push it. But cat-file validates an OBJECT, while the question is REACHABILITY
FROM A REF — and three different situations satisfy that boolean identically:

    genuinely unpushed            STRANDED_LOCAL_ONLY   push it        (correct)
    dangling after rebase/amend   ABSENT                cannot be pushed
    carried on refs/workers/**    STRANDED_WORKER_REF   consume the ref

Measured 2026-08-12: the fleet run's single tier-1 flag (sha 679b9e7) was
ABSENT — reachable from no ref at all — and was filed as a HIGH Investigate
instructing an agent to push a commit that cannot be pushed. Both of tier 1's
stated guards passed on it: keyword-anchoring held, and the None-status drop
held precisely BECAUSE the object still exists locally. guard-3320 records that
ABSENT's remedy is explicitly not "push".

THE DIRECTION THAT MATTERS. This filter removes entries from the Investigate-
FILING lane, and that lane is the one detecting real deliverable loss
(rb-3135 / g-115-2570). So the flag must SURVIVE every uncertainty: an
unprobed sha, an INCONCLUSIVE verdict, a mixed set, or an unloadable prober all
keep committed_not_pushed. Most cases below assert exactly that.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "cnc_reach", CORE_SCRIPTS / "completed-not-committed-sweep.py")
cnc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cnc)

SHA_A = "679b9e7aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SHA_B = "beefcafe1111111111111111111111111111abcd"


def _flagged(shas=(SHA_A,), reason="committed_not_pushed"):
    return {"goal_id": "g-335-985", "source": "world", "reason": reason,
            "shas_absent_local_only": list(shas), "title": "bump consumers"}


# --- the fix itself --------------------------------------------------------

def test_absent_sha_is_not_reported_as_unpushed():
    e = cnc.apply_reachability(_flagged(), {SHA_A: "ABSENT"})
    assert e["reason"] == "absent_unreachable", (
        "an ABSENT commit is reachable from NO ref -- telling an agent to push "
        f"it is a remedy that cannot work (guard-3320). Got: {e['reason']}")


def test_worker_ref_sha_is_not_reported_as_unpushed():
    """The third instance of the same root cause, named in the goal: a commit
    carried on refs/workers/** is landed-pending-consume, not unpushed."""
    e = cnc.apply_reachability(_flagged(), {SHA_A: "STRANDED_WORKER_REF"})
    assert e["reason"] == "stranded_worker_ref"


def test_corrected_entry_still_carries_its_evidence():
    """Relabelled, not suppressed. A genuinely lost deliverable must stay
    readable -- it just stops carrying the wrong remedy."""
    e = cnc.apply_reachability(_flagged(), {SHA_A: "ABSENT"})
    assert e["shas_absent_local_only"] == [SHA_A]
    assert e["reachability_verdicts"] == ["ABSENT"]


# --- the flag must survive every uncertainty -------------------------------

def test_genuinely_unpushed_sha_keeps_the_flag():
    """The whole reason this sweep exists. If STRANDED_LOCAL_ONLY were ever
    relabelled, the detector would stop detecting."""
    e = cnc.apply_reachability(_flagged(), {SHA_A: "STRANDED_LOCAL_ONLY"})
    assert e["reason"] == "committed_not_pushed"


def test_inconclusive_verdict_keeps_the_flag():
    """INCONCLUSIVE means a probe could not run -- explicitly NOT an answer.
    An unavailable probe is not a negative result (verify-before-assuming 4)."""
    e = cnc.apply_reachability(_flagged(), {SHA_A: "INCONCLUSIVE"})
    assert e["reason"] == "committed_not_pushed"


def test_unprobed_sha_keeps_the_flag():
    """Empty status map = the prober could not be loaded at all. Silence must
    never be read as exoneration."""
    e = cnc.apply_reachability(_flagged(), {})
    assert e["reason"] == "committed_not_pushed"


def test_mixed_verdicts_keep_the_flag():
    """One dangling sha must not launder a sibling that genuinely needs a push.
    Mirrors apply_superseded's all-not-any discipline."""
    e = cnc.apply_reachability(
        _flagged((SHA_A, SHA_B)),
        {SHA_A: "ABSENT", SHA_B: "STRANDED_LOCAL_ONLY"})
    assert e["reason"] == "committed_not_pushed"


def test_empty_sha_list_keeps_the_flag():
    """`set()` has len 0, not 1, so the vacuous case cannot relabel. Guarding
    this explicitly because the sibling predicate had exactly this bug shape."""
    e = cnc.apply_reachability(_flagged(shas=()), {})
    assert e["reason"] == "committed_not_pushed"


def test_landed_verdict_keeps_the_flag():
    """LANDED contradicts tier 1 outright and is not in the misrouted map. It is
    left alone deliberately: a disagreement between the two probers is a finding
    for a human, not something to silently resolve in favour of either."""
    e = cnc.apply_reachability(_flagged(), {SHA_A: "LANDED"})
    assert e["reason"] == "committed_not_pushed"


# --- interaction with the sibling benign classifier ------------------------

def test_defers_to_benign_superseded():
    """apply_superseded runs first and proves the CONTENT is in HEAD, which is a
    stronger statement than any verdict about the orphaned sha. The guard also
    makes call order irrelevant, so a future reorder cannot flip the label."""
    e = cnc.apply_reachability(_flagged(reason="benign_superseded"),
                               {SHA_A: "ABSENT"})
    assert e["reason"] == "benign_superseded"
    assert "reachability_verdicts" not in e


# --- the canonical prober is real, and its verdict names are not invented ---

def test_misrouted_verdict_names_exist_in_the_canonical_prober():
    """Pins the two modules together by VALUE. These labels are matched as bare
    strings, so a rename in commit-reachability.py would silently turn this
    whole filter into a no-op -- the failure mode with no error message."""
    reach = cnc._load_reachability()
    assert reach is not None, "commit-reachability.py must be loadable by path"
    for verdict in cnc._MISROUTED_VERDICTS:
        assert getattr(reach, verdict, None) == verdict, (
            f"{verdict} is no longer a verdict constant in commit-reachability.py "
            "-- apply_reachability would silently stop matching it")
    # and the one that must NOT be relabelled is equally load-bearing
    assert reach.STRANDED_LOCAL_ONLY not in cnc._MISROUTED_VERDICTS
