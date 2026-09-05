"""Reducer selection policy — the owner directive of 2026-09-03 ().

With three or more live worker Bodies the reducer stops competing for ordinary
goals and takes reducer-only work instead, leaving the rest claimable.

WHAT THESE TESTS ARE ACTUALLY FOR. `decide()` is four branches and would be
trivially correct in isolation; guard-2783 says that is precisely the trap —
"the decision function is correct in isolation, the defect lives at the CALL
SITE, in the population it is applied to, and every branch test passes with or
without the role guard." So the load-bearing cases here are the ones that pin
the POPULATION and the ORDERING:

  * a WORKER's selection is byte-identical, even at a live count that would fire
    (`test_worker_selection_is_byte_identical_even_above_threshold`)
  * so is an OBSERVER's, which is the role that has neither signal and is the
    easy one to forget (`test_observer_session_is_not_the_reducer`)
  * the role guard runs BEFORE the decision, not after
    (`test_role_guard_precedes_the_decision`)
  * below the threshold nothing moves (`test_below_threshold_is_a_no_op`)

The count tests matter for a second reason: the live store had a 27-day-stale
in_flight_bodies row when this shipped, so "count the rows" and "count the live
workers" were already different numbers on the day it was written.
"""
from __future__ import annotations

import datetime
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

import reducer_selection_policy as R  # noqa: E402


def _load(alias, filename):
    path = CORE_SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Distinct alias so this file's monkeypatching cannot leak into the other
# goal-selector test modules through a shared module object.
gs = _load("goal_selector_rsp", "goal-selector.py")

NOW = datetime.datetime(2026, 9, 3, 12, 0, 0)
SID = "sid-reducer"


def _ts(hours_ago):
    return (NOW - datetime.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S")


def _team_state(*rows):
    """rows: (agent, sid, hours_ago | None) — None means no claimed_at."""
    agents = {}
    for agent, sid, hours in rows:
        body = {} if hours is None else {"claimed_at": _ts(hours)}
        agents.setdefault(agent, {}).setdefault("in_flight_bodies", {})[sid] = body
    return {"agent_status": agents}


def _rows(*specs):
    """specs: (goal_id, skill, executable_by_role)."""
    return [{"goal_id": g, "skill": s, "executable_by_role": r, "score": 10.0 - i}
            for i, (g, s, r) in enumerate(specs)]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """No diary write, no team-state file read, no residency surprise.

    The diary stub is not tidiness: `_append_policy_diary` shells out to
    execution-diary.sh, which writes the LIVE agent's append-only store. A test
    that let it run would put fabricated policy rows into the running fleet's
    audit trail — the same class of mistake as pointing a test at a real store.
    """
    monkeypatch.setattr(gs, "_append_policy_diary", lambda entry, agent_dir: None)
    gs._TEAM_STATE_CACHE = None
    yield
    gs._TEAM_STATE_CACHE = None


# ─── role_of: the predicate, and the roles that are NEITHER ──────────────────

def test_worker_is_decided_by_BODY_ROLE_alone_and_first():
    """A Body carrying BODY_ROLE=worker is never the reducer whatever else holds.

    The second assertion is the real one: even when the sid MATCHES
    running-session-id, worker still wins. A worker box's agent dir can carry a
    synced running-session-id pointing at the reducer's SID, so the sid test
    alone would misclassify it — and mislabelling a worker as the reducer is the
    direction that changes a worker's selection.
    """
    assert R.role_of("worker", "a", "b") == R.ROLE_WORKER
    assert R.role_of("worker", "same", "same") == R.ROLE_WORKER
    assert R.role_of("WORKER", "a", "b") == R.ROLE_WORKER   # case-insensitive
    assert R.role_of(" worker ", "a", "b") == R.ROLE_WORKER  # whitespace


def test_reducer_requires_the_positive_identity_not_merely_absent_BODY_ROLE():
    assert R.role_of(None, SID, SID) == R.ROLE_REDUCER
    assert R.role_of("", SID, SID) == R.ROLE_REDUCER
    assert R.role_of(None, SID, "other-sid") == R.ROLE_UNKNOWN


def test_observer_session_is_not_the_reducer():
    """The role that is easy to forget, and the reason BODY_ROLE-unset alone is
    not reducer-hood. A reader/assistant observer forks no Body, so it has no
    BODY_ROLE — and its SID is not running-session-id. It must land in UNKNOWN,
    which does nothing, rather than inheriting the reducer branch."""
    assert R.role_of(None, "observer-sid", "reducer-sid") == R.ROLE_UNKNOWN
    assert R.role_of(None, None, None) == R.ROLE_UNKNOWN
    assert R.role_of(None, "", "") == R.ROLE_UNKNOWN
    # and an UNKNOWN never prefers, however many workers are live
    assert R.decide(role=R.ROLE_UNKNOWN, live_workers=99).prefer_reducer_only is False


# ─── live_worker_count: the population ───────────────────────────────────────

def test_counts_live_bodies_across_every_agent():
    """Fleet-wide, not same-agent. Every Body selects from the same
    world/aspirations.jsonl, so every live Body is competition."""
    ts = _team_state(("alpha", "s1", 1), ("bravo", "s2", 2), ("zeta", "s3", 0.5))
    assert R.live_worker_count(ts, NOW, 6.0)["live"] == 3


def test_a_stale_row_is_not_a_live_worker():
    """The measured case. On 2026-09-03 bravo carried an in_flight_bodies row
    claimed 2026-08-07 — 27 days — for a long-terminal goal. Counting rows
    instead of live claims reports phantom workers and stands the reducer down
    against a fleet that is not there."""
    ts = _team_state(("alpha", "s1", 1), ("bravo", "s2", 27 * 24))
    detail = R.live_worker_count(ts, NOW, 6.0)
    assert detail["live"] == 1 and detail["stale"] == 1


def test_an_uncountable_row_makes_the_count_SMALLER():
    """The fail-safe direction, stated as a test because it is a choice.

    A row with a missing or unparseable claimed_at is NOT counted. That biases
    toward the reducer KEEPING today's behaviour; the opposite default would
    stand it down on unreadable data.
    """
    ts = _team_state(("alpha", "s1", 1), ("alpha", "s2", None))
    detail = R.live_worker_count(ts, NOW, 6.0)
    assert detail["live"] == 1 and detail["undated"] == 1
    ts["agent_status"]["alpha"]["in_flight_bodies"]["s3"] = {"claimed_at": "not-a-date"}
    assert R.live_worker_count(ts, NOW, 6.0)["live"] == 1


def test_the_reducer_does_not_count_itself():
    ts = _team_state(("alpha", SID, 1), ("alpha", "s2", 1))
    detail = R.live_worker_count(ts, NOW, 6.0, exclude_sid=SID)
    assert detail["live"] == 1 and detail["excluded_self"] == 1


def test_a_malformed_team_state_counts_zero_and_does_not_raise():
    for bad in ({}, {"agent_status": None}, {"agent_status": []},
                {"agent_status": {"alpha": None}},
                {"agent_status": {"alpha": {"in_flight_bodies": "nope"}}}):
        assert R.live_worker_count(bad, NOW, 6.0)["live"] == 0


# ─── decide: the four branches ───────────────────────────────────────────────

def test_below_threshold_is_a_no_op():
    """Outcome 4's named property."""
    d = R.decide(role=R.ROLE_REDUCER, live_workers=2, config={"worker_threshold": 3})
    assert d.branch == R.BRANCH_BELOW_THRESHOLD and d.prefer_reducer_only is False


def test_at_the_threshold_it_fires():
    """>= not >. Three is the directive's number and three must be enough."""
    d = R.decide(role=R.ROLE_REDUCER, live_workers=3, config={"worker_threshold": 3})
    assert d.branch == R.BRANCH_PREFER_REDUCER_ONLY and d.prefer_reducer_only is True


def test_disabled_is_a_no_op_at_any_count():
    d = R.decide(role=R.ROLE_REDUCER, live_workers=99, config={"enabled": False})
    assert d.branch == R.BRANCH_DISABLED and d.prefer_reducer_only is False


def test_a_malformed_threshold_falls_back_rather_than_raising():
    d = R.decide(role=R.ROLE_REDUCER, live_workers=3,
                 config={"worker_threshold": "three"})
    assert d.prefer_reducer_only is True  # fell back to the default 3


def test_role_guard_precedes_the_decision():
    """guard-2783: 'a guard that runs after the decision is no guard at all'.

    Pinned by construction rather than by inspection: a worker at a live count
    far above any threshold, with the policy enabled, still reports the ROLE
    branch — proving the role test short-circuits before the count is consulted.
    """
    d = R.decide(role=R.ROLE_WORKER, live_workers=999,
                 config={"enabled": True, "worker_threshold": 1})
    assert d.branch == R.BRANCH_NOT_REDUCER
    assert d.prefer_reducer_only is False


# ─── is_reducer_only_row: one routing implementation ─────────────────────────

def test_the_goal_level_declaration_is_honoured():
    assert R.is_reducer_only_row({"executable_by_role": "reducer"}) is True
    assert R.is_reducer_only_row({"executable_by_role": "REDUCER"}) is True
    assert R.is_reducer_only_row({"executable_by_role": "worker"}) is False
    assert R.is_reducer_only_row({"executable_by_role": None}) is False


def test_the_optional_second_source_is_honoured_but_unused_by_the_selector():
    """`is_reducer_only_row` accepts a caller-supplied bridge verdict, and the
    goal-selector deliberately passes NONE.

    `test_selection_stays_role_blind` forbids goal-selector.py from naming the
    worker-side eligibility module at all -- by raw source grep, comments
    included -- because that would put the worker's routing code inside the
    component both roles run. So the selector reads the FIELD only. The parameter
    stays because a non-selector caller (the worker loop already holds that
    verdict) can supply it without a second implementation of this predicate.

    CONSEQUENCE: the floor is INERT until g-115-7372's commit lands and goals are
    stamped. That is honest and it is the right trade -- shipping a mechanism
    ahead of its data is the close-review gate's pattern (g-357-40), while making
    a tested fence green by deleting it is guard-4618.
    """
    assert R.is_reducer_only_row({"skill": "/reflect"}, True) is True
    assert R.is_reducer_only_row({"skill": "/reflect"}, False) is False
    assert R.is_reducer_only_row({"executable_by_role": "reducer"}, False) is True


def test_a_non_dict_row_is_not_reducer_only():
    for bad in (None, "g-1-1", 7, []):
        assert R.is_reducer_only_row(bad) is False


# ─── the CALL SITE — where guard-2783 says the defect actually lives ─────────

def _floor(monkeypatch, *, body_role, sid, running_sid, team_state, rows,
           prior_hoist=False, config=None):
    monkeypatch.setattr(gs, "_reducer_policy_inputs",
                        lambda agent_dir: (body_role, sid, running_sid))
    monkeypatch.setattr(gs, "_load_team_state_cached", lambda: team_state)
    monkeypatch.setattr(gs, "REDUCER_SELECTION_CONFIG",
                        config or dict(R.DEFAULTS))
    # now=NOW is LOAD-BEARING, not tidiness. _team_state stamps `claimed_at`
    # relative to the frozen NOW above, and live_worker_count drops any row older
    # than claim_fresh_hours (6h). While the floor read the real wall clock, these
    # fixtures were fresh only while real-now sat within 6h of NOW -- so this file
    # passed 25/25 the morning it was written and went 4-red the same evening,
    # then stayed red, with no code change in between. Freeze both ends or the
    # freshness tests measure the calendar.
    return gs.apply_reducer_only_floor(rows, None, prior_hoist_fired=prior_hoist,
                                       now=NOW)


def test_worker_selection_is_byte_identical_even_above_threshold(monkeypatch):
    """THE load-bearing case, and the one a branch test cannot reach.

    Five live workers, policy enabled, a reducer-only row sitting second — every
    condition for the hoist is met except the role. The order must not move, no
    row may gain the marker key, and no score may change.
    LIFECYCLE_DISPOSITIONS["select"]: "A worker selects exactly like the reducer
    -- same scorer, same candidate set."
    """
    rows = _rows(("g-1-1", None, None), ("g-2-2", None, "reducer"))
    before = [dict(r) for r in rows]
    picked, status = _floor(
        monkeypatch, body_role="worker", sid="w", running_sid="w",
        team_state=_team_state(*[("alpha", f"s{i}", 1) for i in range(5)]),
        rows=rows)
    assert picked is None
    assert status["branch"] == R.BRANCH_NOT_REDUCER
    assert rows == before, "a worker's ranked output must be byte-identical"


def test_reducer_above_threshold_hoists_reducer_only_work(monkeypatch):
    rows = _rows(("g-1-1", None, None), ("g-2-2", None, "reducer"))
    scores_before = [r["score"] for r in rows]
    picked, status = _floor(
        monkeypatch, body_role=None, sid=SID, running_sid=SID,
        team_state=_team_state(*[("a", f"s{i}", 1) for i in range(4)]),
        rows=rows)
    assert picked is not None and picked["goal_id"] == "g-2-2"
    assert rows[0]["goal_id"] == "g-2-2"
    assert status["branch"] == R.BRANCH_PREFER_REDUCER_ONLY
    assert picked["reducer_only_pick"] is True
    assert sorted(r["score"] for r in rows) == sorted(scores_before), (
        "the floor REORDERS and must never rescore")


def test_reducer_below_threshold_leaves_the_order_alone(monkeypatch):
    rows = _rows(("g-1-1", None, None), ("g-2-2", None, "reducer"))
    before = [dict(r) for r in rows]
    picked, status = _floor(
        monkeypatch, body_role=None, sid=SID, running_sid=SID,
        team_state=_team_state(("a", "s1", 1), ("a", "s2", 1)), rows=rows)
    assert picked is None
    assert status["branch"] == R.BRANCH_BELOW_THRESHOLD
    assert rows == before


def test_the_floor_yields_to_a_prior_hoist(monkeypatch):
    """The drain lane and the strategic-focus floor both write index 0, so
    whichever runs last wins. This one carries no cadence bound and can take the
    very next invocation; a starving recurring goal and a standing user directive
    cannot. So it yields."""
    rows = _rows(("g-1-1", None, None), ("g-2-2", None, "reducer"))
    before = [dict(r) for r in rows]
    picked, status = _floor(
        monkeypatch, body_role=None, sid=SID, running_sid=SID,
        team_state=_team_state(*[("a", f"s{i}", 1) for i in range(4)]),
        rows=rows, prior_hoist=True)
    assert picked is None and status["yielded"] is True
    assert rows == before


def test_no_reducer_only_row_in_the_pool_is_inert_not_an_error(monkeypatch):
    rows = _rows(("g-1-1", None, None), ("g-2-2", None, "worker"))
    before = [dict(r) for r in rows]
    picked, status = _floor(
        monkeypatch, body_role=None, sid=SID, running_sid=SID,
        team_state=_team_state(*[("a", f"s{i}", 1) for i in range(4)]),
        rows=rows)
    assert picked is None
    assert status["reducer_only_rows"] == 0
    assert rows == before


def test_an_already_top_reducer_only_row_is_not_reshuffled(monkeypatch):
    rows = _rows(("g-2-2", None, "reducer"), ("g-1-1", None, None))
    picked, _ = _floor(
        monkeypatch, body_role=None, sid=SID, running_sid=SID,
        team_state=_team_state(*[("a", f"s{i}", 1) for i in range(4)]),
        rows=rows)
    assert picked["goal_id"] == "g-2-2" and rows[0]["goal_id"] == "g-2-2"
    assert [r["goal_id"] for r in rows] == ["g-2-2", "g-1-1"]


def test_the_diary_records_the_count_and_branch_every_reducer_iteration(monkeypatch):
    """Outcome 1, including the branches that change nothing — a record that
    only appears when the policy fires cannot distinguish 'did not fire' from
    'did not run'."""
    seen = []
    monkeypatch.setattr(gs, "_append_policy_diary",
                        lambda entry, agent_dir: seen.append(entry))
    for n, branch in ((4, R.BRANCH_PREFER_REDUCER_ONLY), (1, R.BRANCH_BELOW_THRESHOLD)):
        _floor(monkeypatch, body_role=None, sid=SID, running_sid=SID,
               team_state=_team_state(*[("a", f"s{i}", 1) for i in range(n)]),
               rows=_rows(("g-1-1", None, "reducer")))
    assert len(seen) == 2
    assert [e["reducer_selection_policy"]["branch"] for e in seen] == \
        [R.BRANCH_PREFER_REDUCER_ONLY, R.BRANCH_BELOW_THRESHOLD]
    assert [e["reducer_selection_policy"]["live_workers"] for e in seen] == [4, 1]
    assert all(e["entry_type"] == "decision" for e in seen), (
        "execution-diary.py fails CLOSED on an unknown entry_type")


def test_a_worker_writes_no_diary_row(monkeypatch):
    """A worker must not pollute the reducer's audit trail with rows about a
    policy that never applies to it."""
    seen = []
    monkeypatch.setattr(gs, "_append_policy_diary",
                        lambda entry, agent_dir: seen.append(entry))
    _floor(monkeypatch, body_role="worker", sid="w", running_sid="w",
           team_state=_team_state(*[("a", f"s{i}", 1) for i in range(9)]),
           rows=_rows(("g-1-1", None, "reducer")))
    assert seen == []


def test_config_is_read_from_aspirations_yaml_not_hardcoded():
    """Outcome 4: the threshold is a config key. Read through the real loader so
    a rename in the YAML fails here rather than silently reverting to a default.
    """
    cfg = gs.load_reducer_selection_policy_config()
    assert set(cfg) == set(R.DEFAULTS)
    assert cfg["worker_threshold"] == 3
    assert cfg["enabled"] is True


# --- emitter (guard-1362): the ROW the floor actually filters ----------------
#
# Every test above builds its rows by hand via _rows(), so all of them passed
# while the shipped floor was inert on EVERY box. score_goal's emission never
# carried `executable_by_role`, so is_reducer_only_row read None for every real
# candidate and `nominees` was empty on every pass -- the documented "inert
# case" -- long after both preconditions the floor's own comment names were
# satisfied (e62c24033 on origin/main; 60 goals stamped). Measured 2026-09-03
# (echo, cc-03, Linux 6.8.0-138-generic): branch=prefer-reducer-only logged 3x
# at 6 live workers while 6 stamped goals sat at ranks 28/512/628/633/665/1102,
# none hoisted. Hand-built rows cannot catch that; only the emitter can.
# Same class as guard-920 (a regression test must replicate the production
# shape, not the contract-ideal one) and guard-2518 (a projection is a schema in
# its own right; filtering on a field it omits yields a silent zero).

def test_score_goal_emits_executable_by_role():
    """guard-1362: the EMITTER carries the field, not only the consumer."""
    gs._ACTIVE_DIRECTIVES = []
    goal = {"id": "g-x-1", "title": "t", "priority": "HIGH",
            "executable_by_role": "reducer"}
    cand = {"goal": goal, "aspiration": {"id": "asp-x"}, "source": "world"}
    result = gs.score_goal(cand, {}, [], [])
    assert result["executable_by_role"] == "reducer"
    # The end-to-end contract: the predicate must accept the row the emitter
    # produced. Asserting only the key would still pass if the VALUE shape drifted.
    assert R.is_reducer_only_row(result) is True


def test_score_goal_emits_executable_by_role_as_none_when_unstamped():
    """An unstamped goal emits the KEY with value None.

    Presence-with-None, not absence: the key being in the emission is what makes
    a future projection regression visible to a key-set assertion, and it keeps
    the unstamped path byte-equivalent for the predicate (False either way).
    """
    gs._ACTIVE_DIRECTIVES = []
    goal = {"id": "g-x-2", "title": "t", "priority": "HIGH"}
    cand = {"goal": goal, "aspiration": {"id": "asp-x"}, "source": "world"}
    result = gs.score_goal(cand, {}, [], [])
    assert "executable_by_role" in result
    assert result["executable_by_role"] is None
    assert R.is_reducer_only_row(result) is False


# ---- the frozen-clock seam ( follow-up; the time-bomb regression) ----

def test_the_floor_honours_an_injected_clock(monkeypatch):
    """`now=` must actually REACH live_worker_count's freshness comparison.

    THE DEFECT THIS PINS, measured on cc-08 2026-09-03: apply_reducer_only_floor
    hardcoded datetime.now() while this file anchors its fixture rows to the
    frozen NOW above. live_worker_count drops rows older than claim_fresh_hours
    (6h), so the fixtures were fresh only while real-now sat inside 6h of NOW --
    the file passed 25/25 the morning it was authored and went 4-red the same
    evening, then stayed red forever, with NO code change in between. A red that
    switches on by calendar reads as a genuine regression to whoever meets it
    next, and it cost a reducer a pristine-worktree baseline run to rule out.

    Dropping the parameter is already caught (the `now=NOW` call in _floor would
    raise TypeError). This pins the OTHER half -- accepting the parameter and
    then ignoring it, which is silent. Both rows below are 1h before NOW and
    therefore STALE against any real wall clock after NOW+7h; the count can only
    reach the threshold if the injected clock is the one being compared against.
    """
    picked, status = _floor(
        monkeypatch, body_role=None, sid=SID, running_sid=SID,
        team_state=_team_state(("a", "s0", 1), ("a", "s1", 1), ("a", "s2", 1)),
        rows=_rows(("g-1-1", None, "reducer"), ("g-1-2", None, None)))
    assert status["live_workers"] == 3, (
        "injected clock was ignored -- freshness compared against the wall clock")
    assert status["branch"] == R.BRANCH_PREFER_REDUCER_ONLY
    assert picked is not None and picked["goal_id"] == "g-1-1"
