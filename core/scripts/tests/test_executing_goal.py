"""test_executing_goal.py — .

Pins core/scripts/_executing_goal.py, the per-session resolver behind the ambient
origin_goal_id stamp of tree add-child (CLI tree.py and daemon tree_write.py).

The defect: the stamp read team-state agent_status.<agent>.in_flight, which is keyed
by AGENT NAME and written only by the reducer. A worker Body's claim writes
in_flight_bodies.<sid> instead, so the old read named the REDUCER's goal on every
node a worker wrote. These are the resolver's rules, one test each. The end-to-end
CLI/daemon behaviour is pinned in test_origin_goal_id_instrumentation.py and
mind_api/tests/test_runtime_tree_write.py.
"""
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _executing_goal import read_reducer_sid, resolve_executing_goal_id  # noqa: E402

AGENT = "alpha"
REDUCER = "sid-reducer"


def _world(tmp_path, in_flight=None, bodies=None):
    """world/team-state.yaml core residual with one agent row (read_agent_row's
    fallback path, so no shard fixture is needed)."""
    world = tmp_path / "world"
    world.mkdir(parents=True, exist_ok=True)
    row = {}
    if in_flight is not None:
        row["in_flight"] = {"goal_id": in_flight}
    if bodies is not None:
        row["in_flight_bodies"] = bodies
    (world / "team-state.yaml").write_text(
        yaml.safe_dump({"agent_status": {AGENT: row}}), encoding="utf-8")
    return world, world / "team-state.yaml"


def _resolve(tmp_path, sid, reducer=REDUCER, in_flight="g-reducer", bodies=None):
    world, core = _world(tmp_path, in_flight, bodies)
    return resolve_executing_goal_id(world, AGENT, sid, reducer, core_path=core)


# ── the headline defect ─────────────────────────────────────────────────────

def test_worker_resolves_to_its_own_goal_not_the_agent_keyed_one(tmp_path):
    bodies = {"sid-w1": {"goal_id": "g-w1"}}
    assert _resolve(tmp_path, "sid-w1", bodies=bodies) == "g-w1"


def test_sibling_bodies_each_resolve_their_own_goal(tmp_path):
    bodies = {"sid-w1": {"goal_id": "g-w1"}, "sid-w2": {"goal_id": "g-w2"}}
    assert _resolve(tmp_path, "sid-w1", bodies=bodies) == "g-w1"
    assert _resolve(tmp_path, "sid-w2", bodies=bodies) == "g-w2"


def test_a_worker_never_inherits_the_reducers_goal(tmp_path):
    """The old read returned the agent-keyed goal for ANY caller. A Body with no claim
    row of its own must get nothing, not that goal."""
    bodies = {"sid-w1": {"goal_id": "g-w1"}}
    assert _resolve(tmp_path, "sid-w2", bodies=bodies) is None


# ── the reducer ─────────────────────────────────────────────────────────────

def test_reducer_resolves_to_the_agent_keyed_goal(tmp_path):
    assert _resolve(tmp_path, REDUCER, in_flight="g-reducer") == "g-reducer"


def test_reducer_with_no_in_flight_goal_resolves_to_none(tmp_path):
    assert _resolve(tmp_path, REDUCER, in_flight=None) is None


def test_a_worker_row_outranks_the_reducer_match(tmp_path):
    """A Body that has its own claim row is a non-reducer by construction; its row is
    the answer even if its sid were ever compared equal to the reducer's."""
    bodies = {REDUCER: {"goal_id": "g-own"}}
    assert _resolve(tmp_path, REDUCER, bodies=bodies) == "g-own"


def test_an_empty_reducer_sid_matches_nobody(tmp_path):
    """A worker box has no running-session-id, so reducer_sid is ''. That must never
    count as a match for any caller (a truthiness guard, not an equality accident)."""
    assert _resolve(tmp_path, "sid-w1", reducer="", in_flight="g-reducer") is None
    assert _resolve(tmp_path, "sid-w1", reducer=None, in_flight="g-reducer") is None


# ── ambiguity -> omit ───────────────────────────────────────────────────────

def test_no_session_identity_resolves_to_none(tmp_path):
    for sid in (None, "", "   "):
        assert _resolve(tmp_path, sid, bodies={"sid-w1": {"goal_id": "g-w1"}}) is None


def test_unrecognised_session_resolves_to_none(tmp_path):
    """Neither the reducer nor a Body with a row (an observer, a reaped Body)."""
    assert _resolve(tmp_path, "sid-observer", bodies={"sid-w1": {"goal_id": "g-w1"}}) is None


def test_released_row_with_null_goal_resolves_to_none(tmp_path):
    """aspirations-release clears in_flight_bodies.<sid>.goal_id; the Body holds nothing
    now, and the agent-keyed goal is not its own."""
    assert _resolve(tmp_path, "sid-w1", bodies={"sid-w1": {"goal_id": None}}) is None


def test_cleared_row_resolves_to_none(tmp_path):
    """worker_close_in_flight_clear leaves {sid: null}; a non-mapping row is no claim."""
    assert _resolve(tmp_path, "sid-w1", bodies={"sid-w1": None}) is None


def test_non_string_or_empty_goal_ids_are_not_goals(tmp_path):
    for bad in ("", 0, 7, ["g-1"], {"x": 1}):
        assert _resolve(tmp_path, "sid-w1", bodies={"sid-w1": {"goal_id": bad}}) is None


# ── fail-open ───────────────────────────────────────────────────────────────

def test_missing_world_resolves_to_none(tmp_path):
    missing = tmp_path / "nowhere"
    assert resolve_executing_goal_id(missing, AGENT, "sid-w1", REDUCER,
                                     core_path=missing / "team-state.yaml") is None


def test_malformed_bodies_map_resolves_to_none(tmp_path):
    assert _resolve(tmp_path, "sid-w1", bodies=["not", "a", "mapping"]) is None


def test_no_agent_resolves_to_none(tmp_path):
    world, core = _world(tmp_path, "g-reducer", {"sid-w1": {"goal_id": "g-w1"}})
    assert resolve_executing_goal_id(world, "", "sid-w1", REDUCER, core_path=core) is None
    assert resolve_executing_goal_id(world, None, "sid-w1", REDUCER, core_path=core) is None


# ── read_reducer_sid ────────────────────────────────────────────────────────

def test_read_reducer_sid_reads_the_file_and_strips_all_whitespace(tmp_path):
    """The write side reads the same file with `tr -d '[:space:]'`
    (team-state-in-flight.sh), so the reader strips ALL whitespace, not just the ends."""
    (tmp_path / "running-session-id").write_text("  abc\n def \t\n", encoding="utf-8")
    assert read_reducer_sid(tmp_path) == "abcdef"


def test_read_reducer_sid_is_empty_when_absent_or_unreadable(tmp_path):
    assert read_reducer_sid(tmp_path) == ""              # no file: every worker box
    assert read_reducer_sid(tmp_path / "missing") == ""  # no dir
    assert read_reducer_sid(None) == ""                  # no agent dir bound


def test_reducer_match_uses_the_file_as_written(tmp_path):
    """End to end through both functions: the file's content, newline and all, is the
    reducer's sid exactly as the claim path compares it."""
    state = tmp_path / "session"
    state.mkdir()
    (state / "running-session-id").write_text(REDUCER + "\n", encoding="utf-8")
    world, core = _world(tmp_path, "g-reducer")
    assert resolve_executing_goal_id(world, AGENT, REDUCER, read_reducer_sid(state),
                                     core_path=core) == "g-reducer"
    assert resolve_executing_goal_id(world, AGENT, "sid-other", read_reducer_sid(state),
                                     core_path=core) is None
