"""Stale-dependency detection in dependency-timeout-check ().

THE DEFECT. `_root_of` returns the FIRST UNRESOLVED blocker. When every blocker
has gone terminal its loop falls through and returns `bb[0]` -- a COMPLETED goal
-- and run() escalates that as if it were live. Meanwhile goal-selector path (d)
treats a non-empty `blocked_by` as a dependency wait, so the goal never reaches
the scorer. Net effect: a goal whose dependencies are all satisfied stays blocked
forever. Measured in the originating goal: g-350-10 sat blocked 7 days on
g-350-11 (completed); g-250-258 named a completed unblock_goal with an expires_at
two days past. Both had to be unblocked BY HAND.

What these tests pin, in the order the reasoning runs:
  1 -- the ORIGINAL BUG, reproduced through `_root_of` itself, so the test fails
       loudly if someone "simplifies" the fall-through away without reading why.
  2 -- the fix fires on the all-terminal shape and reports every edge.
  3 -- FAIL-SAFE: an id absent from the index is NOT terminal. The index is built
       fail-open per source, so "not found" means "unresolvable", and treating it
       as finished would let one degraded read clear a live dependency.
  4 -- a single live blocker suppresses the whole verdict (no partial clears).
  5 -- the deliberate divergence between `_root_of`'s legacy terminal tuple and
       TERMINAL_FOR_UNBLOCK is PINNED, so a future fusion of the two fails here
       rather than silently changing which root gets escalated.
  6 -- the prose-only census counts what the sweep's predicate EXCLUDES
       (guard-2298), and does NOT count goals that already carry a signal.

Run: py -3 -m pytest core/scripts/tests/test_dependency_timeout_stale.py -v
"""
import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))


def _load():
    spec = importlib.util.spec_from_file_location(
        "dependency_timeout_stale_module",
        SCRIPT_DIR / "dependency-timeout-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load()


def _g(gid, status="pending", blocked_by=None, **kw):
    d = {"id": gid, "status": status, "title": gid, "_source": "world"}
    if blocked_by is not None:
        d["blocked_by"] = blocked_by
    d.update(kw)
    return d


# 1 -- the original bug, reproduced at its source ---------------------------

def test_root_of_falls_through_to_a_completed_blocker():
    """The bug this goal exists to fix, pinned at `_root_of`.

    With every blocker terminal the loop body never returns, so the trailing
    `return bb[0]` hands back a COMPLETED goal that run() then escalates. This
    is asserted rather than fixed: `_root_of` is deliberately left alone, and
    the new detector runs ahead of it.
    """
    idx = {"g-350-10": _g("g-350-10", "blocked", ["g-350-11"]),
           "g-350-11": _g("g-350-11", "completed")}
    rid, root = M._root_of(idx["g-350-10"], idx)
    assert rid == "g-350-11"
    assert root is not None and root["status"] == "completed", (
        "_root_of no longer falls through to a terminal blocker. If that was "
        "fixed deliberately, this test documents the OLD contract -- update it "
        "and re-check that _stale_dependency still runs first in run().")


# 2 -- the fix fires --------------------------------------------------------

def test_stale_dependency_fires_when_every_edge_is_terminal():
    idx = {"g-350-10": _g("g-350-10", "blocked", ["g-350-11"]),
           "g-350-11": _g("g-350-11", "completed")}
    out = M._stale_dependency(idx["g-350-10"], idx)
    assert out is not None
    assert out["edge_count"] == 1
    assert out["edges"][0] == {"root_id": "g-350-11", "status": "completed",
                               "via": "blocked_by", "archived": False}
    assert "g-350-11=completed(blocked_by)" in out["summary"]


def test_stale_dependency_accepts_a_bare_string_edge():
    """`blocked_by` is a bare string on some records, not always a list."""
    idx = {"a": _g("a", "blocked", "b"), "b": _g("b", "superseded")}
    out = M._stale_dependency(idx["a"], idx)
    assert out is not None and out["edge_count"] == 1


def test_every_terminal_status_counts_as_satisfied():
    for st in M.TERMINAL_FOR_UNBLOCK:
        idx = {"a": _g("a", "blocked", ["b"]), "b": _g("b", st)}
        assert M._stale_dependency(idx["a"], idx) is not None, st


# 3 -- fail-safe on the unknown --------------------------------------------

def test_an_unresolvable_edge_is_never_treated_as_terminal():
    """"Not in the index" means unresolvable, NOT finished.

    _read_goal_index is fail-open per source: a failed read yields FEWER
    entries with no exception. If a missing id counted as terminal, one
    degraded read would clear live dependencies wholesale.
    """
    idx = {"a": _g("a", "blocked", ["ghost"])}
    assert M._stale_dependency(idx["a"], idx) is None


def test_one_live_blocker_suppresses_the_whole_verdict():
    idx = {"a": _g("a", "blocked", ["b", "c"]),
           "b": _g("b", "completed"), "c": _g("c", "pending")}
    assert M._stale_dependency(idx["a"], idx) is None


def test_no_edges_is_not_this_class():
    idx = {"a": _g("a", "blocked", [])}
    assert M._stale_dependency(idx["a"], idx) is None
    assert M._stale_dependency(_g("a", "blocked"), idx) is None


# 4 -- the deliberate divergence -------------------------------------------

def test_terminal_sets_diverge_deliberately():
    """`_root_of` uses a NARROWER terminal set than TERMINAL_FOR_UNBLOCK.

    _root_of tests ("completed", "skipped", "expired") inline; it is left alone
    because widening it changes WHICH root gets escalated on live chains -- a
    different change than g-115-3201 asked for. TERMINAL_FOR_UNBLOCK is the
    complete set per CLAUDE.md status values. This asserts the gap so a future
    fusion of the two fails HERE, loudly, instead of silently altering
    escalation. To unify them, do it as its own change with its own measurement.
    """
    extra = set(M.TERMINAL_FOR_UNBLOCK) - {"completed", "skipped", "expired"}
    assert extra == {"superseded", "decomposed"}, (
        "the terminal sets moved; re-read the WHY A SEPARATE TERMINAL SET "
        "comment before reconciling them")
    idx = {"a": _g("a", "blocked", ["b"]), "b": _g("b", "superseded")}
    # the new detector sees it as satisfied ...
    assert M._stale_dependency(idx["a"], idx) is not None
    # ... while _root_of still reports it as the live root. That is the gap.
    _, root = M._root_of(idx["a"], idx)
    assert root is not None and root["status"] == "superseded"


# 4b -- blocker_ref edges, the goal's SECOND named instance -----------------
#
# 's title is "blocked_by/blocker_ref targets have ALL gone terminal"
# and its second measured incident is a blocker_ref one:  "named a
# completed unblock_goal with an expires_at two days past". A blocked_by-only
# detector covers one of the two incidents the goal was filed from while still
# reporting under the name "stale_dependency" -- the miss would read as a clean
# result. These pin the other half.

def test_blocker_ref_unblock_goal_counts_as_an_edge():
    """The  shape: no blocked_by at all, only a blocker_ref."""
    idx = {"g-250-258": _g("g-250-258", "blocked",
                           blocker_ref={"type": "resource",
                                        "unblock_goal": "g-250-259",
                                        "expires_at": "2026-07-24T00:00:00"}),
           "g-250-259": _g("g-250-259", "completed")}
    out = M._stale_dependency(idx["g-250-258"], idx)
    assert out is not None, "a blocker_ref-only block is still this class"
    assert out["edges"][0]["via"] == "blocker_ref"
    assert out["edge_count"] == 1


def test_a_live_blocker_ref_target_suppresses_the_verdict():
    idx = {"a": _g("a", "blocked", blocker_ref={"unblock_goal": "b"}),
           "b": _g("b", "in-progress")}
    assert M._stale_dependency(idx["a"], idx) is None


def test_both_field_kinds_must_be_terminal_together():
    """blocked_by terminal but blocker_ref live -> still blocked."""
    idx = {"a": _g("a", "blocked", ["b"], blocker_ref={"unblock_goal": "c"}),
           "b": _g("b", "completed"), "c": _g("c", "pending")}
    assert M._stale_dependency(idx["a"], idx) is None
    idx["c"]["status"] = "completed"
    out = M._stale_dependency(idx["a"], idx)
    assert out is not None and out["edge_count"] == 2


def test_the_same_id_in_both_fields_is_counted_once():
    """The common shape -- double-counting would read as two blockers."""
    idx = {"a": _g("a", "blocked", ["b"], blocker_ref={"unblock_goal": "b"}),
           "b": _g("b", "completed")}
    out = M._stale_dependency(idx["a"], idx)
    assert out["edge_count"] == 1, out
    assert out["edges"][0]["via"] == "blocked_by", "first origin wins"


def test_external_id_is_never_followed_as_a_goal():
    """`external_id` names infrastructure, not a goal -- guessing at it would
    fabricate an edge the schema does not assert."""
    idx = {"a": _g("a", "blocked",
                   blocker_ref={"type": "infrastructure",
                                "external_id": "vinheim-dev:POST /run/stop -> 502"})}
    assert M._stale_dependency(idx["a"], idx) is None


def test_a_non_dict_blocker_ref_does_not_crash():
    idx = {"a": _g("a", "blocked", ["b"], blocker_ref="some string"),
           "b": _g("b", "completed")}
    out = M._stale_dependency(idx["a"], idx)
    assert out is not None and out["edge_count"] == 1


# 5 -- the census of what the predicate EXCLUDES ---------------------------

def test_prose_only_census_counts_the_excluded_population():
    """guard-2298: report the excluded population beside the filtered count."""
    idx = {
        "g-368-27": _g("g-368-27", "pending",
                       description="DEPENDS ON g-368-26 -- do not start until "
                                   "that spec exists"),
        "has-edge": _g("has-edge", "pending", ["x"],
                       description="depends on x"),
        "has-defer": _g("has-defer", "pending",
                        description="depends on x", defer_reason="precondition_unmet: x"),
        "unrelated": _g("unrelated", "pending", description="ship the thing"),
        "done": _g("done", "completed", description="depends on x"),
    }
    out = M._prose_only_dependency_census(idx)
    ids = {h["goal_id"] for h in out["sample"]}
    assert ids == {"g-368-27"}, ids
    assert out["count"] == 1
    assert "guard-2298" in out["note"]


def test_prose_census_sample_is_bounded_but_count_is_not():
    idx = {("g-9-%02d" % i): _g("g-9-%02d" % i, "pending",
                                description="depends on something")
           for i in range(25)}
    out = M._prose_only_dependency_census(idx, sample=10)
    assert out["count"] == 25
    assert len(out["sample"]) == 10, "the SAMPLE is bounded; the COUNT must not be"


# 6 -- the WIRING, not just the function -----------------------------------
#
# guard-1943: a green suite certifies the FUNCTION, never the WIRING. Every test
# above calls _stale_dependency directly, so all ten would still pass if the
# call site in run() were deleted. These drive run() itself.

class _Args:
    apply = False
    threshold_hours = 999.0     # so the age gate would reject everything ...
    agent = "test-agent"
    board_escalation_log = None
    no_board = True


def _drive(monkeypatch, index, dep_ids):
    monkeypatch.setattr(M, "_read_blocked", lambda: {
        "blocked_goals": [{"goal_id": g, "block_reason": "dependency"}
                          for g in dep_ids]})
    monkeypatch.setattr(M, "_read_goal_index", lambda: index)
    monkeypatch.setattr(M, "_read_recent_escalations", lambda *a, **k: set())
    monkeypatch.setattr(M, "_load_threshold_hours", lambda a: 999.0)
    monkeypatch.setattr(M, "_resolve_self_agent", lambda a: "test-agent")
    return M.run(_Args())


def test_run_reports_the_stale_goal_despite_the_age_gates(monkeypatch):
    """The stale pass MUST run before the age gates, or it ships inert.

    `blocked_since` is absent here and the threshold is 999h -- the two gates
    that would otherwise route this goal to skipped_no_blocked_since /
    skipped_below_threshold and never look at it again. An already-SATISFIED
    dependency is neither young nor old, so it must not wait on a clock.
    """
    idx = {"g-350-10": _g("g-350-10", "blocked", ["g-350-11"]),
           "g-350-11": _g("g-350-11", "completed")}
    out = _drive(monkeypatch, idx, ["g-350-10"])
    assert len(out["stale_dependency"]) == 1, out
    rec = out["stale_dependency"][0]
    assert rec["goal_id"] == "g-350-10"
    assert rec["summary"] == "g-350-11=completed(blocked_by)"
    assert rec["cleared"] is None, "dry-run must not mutate"
    assert out["skipped_no_blocked_since"] == [], (
        "the stale goal fell through to the age gate -- the pass is in the "
        "wrong place and the detector is inert")


def test_run_leaves_a_genuinely_blocked_goal_to_the_normal_path(monkeypatch):
    """A live blocker must NOT be swallowed by the stale pass."""
    idx = {"a": _g("a", "blocked", ["b"]), "b": _g("b", "pending")}
    out = _drive(monkeypatch, idx, ["a"])
    assert out["stale_dependency"] == []
    assert out["skipped_no_blocked_since"] == ["a"], out


def test_run_always_reports_the_excluded_census(monkeypatch):
    """The census rides on EVERY run, including one that scanned nothing --
    that is the case where a bare zero is most likely to be misread."""
    idx = {"p": _g("p", "pending", description="depends on something")}
    out = _drive(monkeypatch, idx, [])
    assert out["scanned"] == 0
    assert out["stale_dependency"] == []
    assert out["prose_only_dependencies"]["count"] == 1, (
        "a run that scanned nothing still owes the reader the excluded count")
