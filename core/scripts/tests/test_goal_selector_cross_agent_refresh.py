"""test_goal_selector_cross_agent_refresh.py --  regression.

SIBLING of test_goal_selector_stale_cache_refresh.py (g-115-9264). That goal
routed the selector's WORLD and OWN-AGENT aspiration reads through a force_fresh
cache refresh. It did not cover the third read pass: collect_cross_agent_
candidates reads every SIBLING agent's aspirations.jsonl with a plain
``read_jsonl``, and under STORAGE_BACKEND=own-cloud the local tree is a
read-through cache (guard-980, rb-2636). So a peer's goal queue here can be
arbitrarily behind the authoritative object while the lane scores it.

MEASURED. cc-05, 2026-09-06: an already-terminal peer goal (g-001-109, closed by
zeta at 19:19:21 and read back by zeta on the coordination board) held rank 1 of
~1,570 candidates for SIX consecutive iterations, with the zeta mirror unchanged
at 108,141 B for >5h. Independently on cc-08 the same day: zeta's mirror 2,604 B
behind the authoritative object and echo's 30 B behind -- 2 of 5 peer queues
stale on a second box.

WHY IT MATTERS MORE THAN THE SIBLING DEFECT. rank 1 is authoritative under scorer
sovereignty, so the selector hands the Body dead work as its highest-value pick.
The only thing that stopped execution was a WRITE-side gate (aspirations-claim.sh
refusing no_claim on a queue this box does not hold the runner claim for). A goal
needing no peer-queue write -- an investigation, a re-measure, a report -- would
have run to completion against a closed record.

WHAT IS PINNED HERE, and why each is a real regression rather than a restatement:

  1. WIRING AT RUNTIME, NOT BY SOURCE INDEX (guard-1943: a passing unit test
     proves the function, never the wiring). A refresh that runs AFTER the read
     it exists to make current is a no-op that greps as present, so these tests
     record actual call ORDER through collect_cross_agent_candidates.

  2. BREADTH. Refreshing only the first sibling would pass any single-sibling
     ordering test while leaving every other peer queue on the stale snapshot --
     rb-9476's shape, a fix that is present, correct-looking and inert across
     most of its population.

  3. THE NO-SIBLING CASE COSTS NOTHING. This goal's third verification outcome
     forbids adding a backend fetch on an unmeasured cost, and its third check
     requires no regression where local IS authoritative. A single-agent box has
     no siblings, so the helper must not be called at all -- not called with an
     empty list, which would still pay a function call and read as coverage.

  4. THE SWEEP IS NEVER ABORTED -- but a peer it could not vouch for IS
     withheld. This point read "FAIL-OPEN ... the candidates still come back,
     scored from the cache" until g-115-9276 item 3, and that was right while
     the decline was only a MESSAGE. The same goal's ordered remainder
     commissions the reversal: outcome 1 requires that a peer-queue record
     which is terminal in the authoritative store not rank as executable on a
     non-claim-holding box, and a warning does not stop a ranking. The rows a
     stale peer mirror is missing are always the NEWEST -- exactly the closes
     (guard-6156).

     guard-1562 is untouched and is what these tests now pin, because WEDGING
     and WITHHOLDING are different acts. The sweep runs to completion, raises
     nothing, leaves the world and own-agent lanes untouched, and still scores
     every peer that refreshed cleanly; only the unverifiable peer is withheld,
     for one selection, retried on the next. The old assertion was broader than
     the rationale that justified it.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# goal-selector.py requires MIND_AGENT to load (paths derive AGENT_DIR).
# Capture-restore around the module-level mutation so collection-time env
# pollution cannot leak to other tests (rb-1096, guard-588).
_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "bravo")

gs = importlib.import_module("goal-selector")

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT

TARGET_AGENT = gs.AGENT_NAME


# --------------------------------------------------------------------------
# fixtures -- same shape as test_goal_selector_cross_agent_pull.py
# --------------------------------------------------------------------------

def _write_aspirations(agent_dir: Path, aspirations: list) -> None:
    agent_dir.mkdir(parents=True, exist_ok=True)
    with (agent_dir / "aspirations.jsonl").open("w", encoding="utf-8") as fh:
        for asp in aspirations:
            fh.write(json.dumps(asp) + "\n")


def _routed_goal(goal_id: str) -> dict:
    return {
        "id": goal_id,
        "title": f"test goal {goal_id}",
        "status": "pending",
        "priority": "MEDIUM",
        "category": "framework-architecture",
        "participants": ["agent"],
        "recurring": False,
        "intended_agent": TARGET_AGENT,
    }


def _aspiration(asp_id: str, goals: list) -> dict:
    return {"id": asp_id, "title": f"test aspiration {asp_id}",
            "status": "active", "goals": goals, "priority": "MEDIUM"}


def _tree(tmp: str, peers: list) -> tuple:
    """Build PROJECT_ROOT/agents/{<peers>, TARGET_AGENT} and return (root, target)."""
    root = Path(tmp)
    for name in peers:
        _write_aspirations(root / "agents" / name,
                           [_aspiration("asp-test", [_routed_goal(f"g-{name}-01")])])
    target_dir = root / "agents" / TARGET_AGENT
    _write_aspirations(target_dir, [])
    return root, target_dir


class _Recorder:
    """Records refresh calls and sibling reads IN ORDER."""

    def __init__(self):
        self.events = []
        self.refreshed = []

    def install(self, monkeypatch, fail=False):
        real_read = gs.read_jsonl

        def _refresh(paths=None):
            self.events.append("refresh")
            self.refreshed.extend(str(p) for p in (paths or []))
            return not fail

        def _rj(path):
            if str(path).endswith("aspirations.jsonl"):
                self.events.append(f"read:{Path(path).parent.name}")
            return real_read(path)

        monkeypatch.setattr(gs, "refresh_aspiration_caches", _refresh)
        monkeypatch.setattr(gs, "read_jsonl", _rj)
        return self


# --------------------------------------------------------------------------
# 1. wiring -- ORDER, at runtime (guard-1943)
# --------------------------------------------------------------------------

def test_sibling_queues_are_refreshed_before_they_are_read(monkeypatch):
    rec = _Recorder().install(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        root, target = _tree(tmp, ["foo"])
        results = gs.collect_cross_agent_candidates(root, target, TARGET_AGENT)

    assert "refresh" in rec.events, (
        "collect_cross_agent_candidates never refreshed the sibling queues -- "
        "the lane is back on the read-through cache (g-115-9276)")
    reads = [e for e in rec.events if e.startswith("read:")]
    assert reads, "probe did not observe any sibling read"
    assert rec.events.index("refresh") < rec.events.index(reads[0]), (
        "the refresh must PRECEDE the read it exists to make current; a refresh "
        f"after the read is a no-op that greps as present. got {rec.events}")
    # POSITIVE CONTROL (guard-2421): the sweep must still actually work, or the
    # ordering assertions above would hold vacuously over an empty sweep.
    assert [c["goal"]["id"] for c in results] == ["g-foo-01"], results


def test_every_sibling_queue_is_refreshed_not_just_the_first(monkeypatch):
    """Refreshing only sibling[0] passes a single-sibling ordering test while
    leaving every other peer queue stale (rb-9476's inert-fix shape)."""
    rec = _Recorder().install(monkeypatch)
    peers = ["foo", "bar", "baz"]
    with tempfile.TemporaryDirectory() as tmp:
        root, target = _tree(tmp, peers)
        gs.collect_cross_agent_candidates(root, target, TARGET_AGENT)

    refreshed_dirs = {Path(p).parent.name for p in rec.refreshed}
    assert refreshed_dirs == set(peers), (
        f"every sibling queue must be refreshed; got {sorted(refreshed_dirs)}")
    assert TARGET_AGENT not in refreshed_dirs, (
        "the OWN queue is refreshed by the three existing call sites; "
        "refreshing it here would double the round trip on every selection")


# --------------------------------------------------------------------------
# 2. the no-sibling case pays nothing (verification check 3)
# --------------------------------------------------------------------------

def test_a_single_agent_box_never_calls_the_helper(monkeypatch):
    """Where local IS authoritative there is nothing to refresh. Guarding on a
    non-empty list (not calling with []) keeps the added cost at exactly zero."""
    rec = _Recorder().install(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        root, target = _tree(tmp, [])  # only the target agent exists
        results = gs.collect_cross_agent_candidates(root, target, TARGET_AGENT)

    assert results == []
    assert "refresh" not in rec.events, (
        "a box with no sibling queues must not call the refresh helper at all")


def test_a_dir_without_an_aspirations_file_is_not_refreshed(monkeypatch):
    """Non-agent dirs under the agents parent must not become backend round
    trips -- the existing sweep skips them, and so must the refresh."""
    rec = _Recorder().install(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        root, target = _tree(tmp, ["foo"])
        (root / "agents" / "not-an-agent").mkdir(parents=True, exist_ok=True)
        gs.collect_cross_agent_candidates(root, target, TARGET_AGENT)

    assert {Path(p).parent.name for p in rec.refreshed} == {"foo"}, rec.refreshed


# --------------------------------------------------------------------------
# 3. fail-open direction (guard-1562)
# --------------------------------------------------------------------------

def test_a_failing_refresh_withholds_that_peer_but_never_aborts_the_sweep(monkeypatch):
    """DELIBERATELY SUPERSEDED ASSERTION ( item 3) -- see docstring 4.

    This asserted the opposite until item 3: "a failed refresh must degrade to
    the cache, not drop the candidates". Recording the reversal here rather
    than silently rewriting it, because a future reader who finds only the new
    assertion cannot tell a considered reversal from a test someone bent to fit
    their change.

    What is pinned now: the sweep RUNS (it did not abort, raise, or stop the
    other lanes) and the peer whose queue could not be refreshed contributes
    nothing. Wedging selection is still forbidden (guard-1562); declining to
    score one unverifiable peer is not wedging.
    """
    rec = _Recorder().install(monkeypatch, fail=True)
    with tempfile.TemporaryDirectory() as tmp:
        root, target = _tree(tmp, ["foo"])
        results = gs.collect_cross_agent_candidates(root, target, TARGET_AGENT)

    assert results == [], (
        "a peer queue that could not be refreshed authoritatively must not be "
        "scored -- the rows a stale mirror lacks are exactly the closes")
    assert "refresh" in rec.events, (
        "the sweep must still have RUN -- withholding a peer is not aborting")


def test_a_raising_refresh_does_not_wedge_selection(monkeypatch):
    """refresh_aspiration_caches never raises by contract, but this lane must
    not DEPEND on that -- a future edit to the helper must not be able to stop
    every Body on the box from selecting."""
    def _boom(paths=None):
        raise RuntimeError("simulated refresh explosion")

    monkeypatch.setattr(gs, "refresh_aspiration_caches", _boom)
    with tempfile.TemporaryDirectory() as tmp:
        root, target = _tree(tmp, ["foo"])
        try:
            gs.collect_cross_agent_candidates(root, target, TARGET_AGENT)
        except RuntimeError:
            raise AssertionError(
                "a raising refresh wedged the cross-agent lane; selection must "
                "degrade to the cache (guard-1562)")


# --------------------------------------------------------------------------
# 4. breadth -- the call site exists at all
# --------------------------------------------------------------------------

def test_the_sibling_call_site_is_present_and_distinct(monkeypatch):
    """Complements the sibling file's `count("refresh_aspiration_caches()") == 3`
    breadth check, which counts the BARE call sites only and is therefore blind
    to this one. Without an assertion here, deleting this call site leaves both
    files green."""
    src = (CORE_SCRIPTS / "goal-selector.py").read_text(encoding="utf-8")
    assert "refresh_aspiration_caches([_sib_q])" in src, (
        "the cross-agent lane's refresh call site is gone -- peer queues are "
        "back on the read-through cache (g-115-9276). NOTE the call is PER "
        "QUEUE since item 3: the helper returns ONE bool for a whole path "
        "list, so a batched call would let a single unrefreshable peer "
        "withhold EVERY peer's candidates.")
    assert src.count("def refresh_aspiration_caches") == 1, (
        "the helper must have exactly one definition -- a second copy is the "
        "no-transcription violation guard-2676 forbids")
