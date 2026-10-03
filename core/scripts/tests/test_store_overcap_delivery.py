"""test_store_overcap_delivery.py -- the consumer for StoreOvercapProbe ().

THE GAP. StoreOvercapProbe fired as designed on 2026-09-29 (a shared board
channel at 1.26x its cap, then a shared evolution log) and nobody was told: the
event went to core/logs/watchdog-<agent>.jsonl and one stderr line, both
box-local, git-ignored and never synced, and the ratchet then keeps a surfaced
store quiet, so that one line was the whole alarm. Four shared stores sat
1.25-1.32x over cap for 20+ h. test_store_overcap_ratchet.py proves the VERDICT
(the detector fires, goes quiet, re-fires on regression, clears). This file
proves the DELIVERY: that a verdict reaches a goal an agent's selector can see
(ZDS guard-1104: test the delivery path, not only the verdict).

THE HARNESS IS A FULL CHAIN. The REAL detect_overcap (a pinned sweep over a tmp
state log -- the ratchet test's own idiom) feeds the REAL probe, and only the
process boundary and the goal queue are faked. That composition is the point:
the consumer's once-per-episode behaviour is not re-derived here, it is
INHERITED from the ratchet, and only a chain that includes the real ratchet can
show the inheritance holds. The fake queue is coherent -- a goal the probe files
lands in it, so later polls see it -- which is what lets one test follow
file -> dedup -> close as a single lifecycle.

guard-1094: nothing here writes production queue or board state. Every
subprocess the probe starts is answered by the fake; one that is not an add-goal
or update-goal call raises, so a stray call is a loud failure, never a write.

Tests:
  1.  OUTCOME 1 (test half) -- a shared store driven over cap reaches a goal with
      the right body, target and override.
  2.  OUTCOME 2 -- a store that stays over cap files nothing more, through a
      regression too (the open goal dedups it).
  3.  A failed filing is RETRIED, not lost: the ratchet has already marked the
      store, so the detector will never name it again.
  4.  ...and the retry survives a process restart (state goes through JSON).
  5.  A retry for a store that cleared meanwhile is dropped, not filed.
  6.  Clear closes the goal (note first, `skipped`) and a recurrence files anew.
  7.  Machine-local stores stay quiet; an unclassifiable one counts as shared.
  8.  A mixed surfacing names only the shared store.
  9.  An open goal filed by someone else covers the episode (no duplicate).
  10. The close is not gated on the probe's own state (guard-3437).
  11. A started goal is never closed from under its owner.
  12. A blind measurement is not a clear; machine-local debt does not hold a goal
      open; a shared store still over cap does.
  13. A filing exception never escapes the tick.
  14. The key is sanctioned and carries no agent or box (guard-2107).
  15. The body passes the real goal validator.
  16. The event serialises through the real emitter.
  17. State round-trips and tolerates garbage; title elision; refusal note.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from collections import namedtuple
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

import jsonl_hygiene as jh  # noqa: E402
import pointer_freshness as pf  # noqa: E402
from gates import origin_signal as osig  # noqa: E402


def _load_watchdog():
    # agent-watchdog.py is hyphenated -- load via importlib for its symbols.
    spec = importlib.util.spec_from_file_location(
        "agent_watchdog_store_overcap_delivery", CORE_SCRIPTS / "agent-watchdog.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


WD = _load_watchdog()
KEY = WD.StoreOvercapProbe.GOAL_ORIGIN_SIGNAL

SHARED = "/fixture/world/shared-store.jsonl"
SHARED_2 = "/fixture/world/second-shared-store.jsonl"
LOCAL = "/fixture/meta/machine-local-debt.jsonl"

_Proc = namedtuple("_Proc", "returncode stdout stderr")


class _Ctx:
    """Minimal stand-in for WatchdogContext."""

    def __init__(self, root: Path) -> None:
        self.agent_name = "testagent"
        self.project_root_path = root
        self.agent_dir = root


def _report(path, ratio, action="would-cap"):
    """One sweep report row at a chosen over-cap multiple (the ratchet test's
    helper): total/kept IS the ratio _overcap_ratio computes, by='lines' is
    required because an age-bounded store has no bound to be a multiple of."""
    kept = 100
    return {"path": path, "by": "lines", "kept": kept,
            "total": int(round(kept * ratio)), "mode": "cap",
            "action": action, "owner_goal": "g-fixture"}


class _World:
    """The goal queue and the process boundary behind the probe.

    `run` replaces subprocess.run; `open_goal_exists` / `open_goal_records`
    replace pointer_freshness's, with its semantics (EXACT origin_signal
    equality, open statuses only). A goal the probe files lands in `goals`.
    """

    OPEN = {"pending", "in-progress", "blocked"}

    def __init__(self) -> None:
        self.goals: list[dict] = []
        self.calls: list[dict] = []
        self.add_rc = 0
        self.add_raises: Exception | None = None
        self.update_rc = 0
        self._n = 0

    def seed(self, status="pending", claimed_by=None, origin_signal=KEY):
        """A goal somebody else filed (another box, or this probe before a state reset)."""
        self._n += 1
        g = {"id": "g-seed-%03d" % self._n, "status": status,
             "claimed_by": claimed_by, "origin_signal": origin_signal,
             "_source": "world"}
        self.goals.append(g)
        return g

    def open_goal_records(self, origin_signal, world_dir, agent_dir):
        return [dict(g) for g in self.goals
                if g["origin_signal"] == origin_signal and g["status"] in self.OPEN]

    def open_goal_exists(self, origin_signal, world_dir, agent_dir):
        return bool(self.open_goal_records(origin_signal, world_dir, agent_dir))

    def run(self, argv, **kw):
        names = [Path(str(a)).name for a in argv]
        if "aspirations-add-goal.sh" in names:
            script = "aspirations-add-goal.sh"
        elif "aspirations-update-goal.sh" in names:
            script = "aspirations-update-goal.sh"
        else:
            raise AssertionError("unexpected subprocess in a delivery test: %r" % (argv,))
        self.calls.append({"script": script, "argv": list(argv),
                           "input": kw.get("input"), "cwd": kw.get("cwd")})
        if script == "aspirations-add-goal.sh":
            if self.add_raises is not None:
                raise self.add_raises
            if self.add_rc != 0:
                return _Proc(self.add_rc, "", "Could not acquire lock")
            body = json.loads(kw["input"])
            self._n += 1
            gid = "g-filed-%03d" % self._n
            self.goals.append({"id": gid, "status": "pending", "claimed_by": None,
                               "origin_signal": body["origin_signal"],
                               "_source": "world"})
            return _Proc(0, json.dumps({"id": gid}), "")
        i = names.index(script)
        gid, field, value = argv[i + 1], argv[i + 2], argv[i + 3]
        if self.update_rc != 0:
            return _Proc(self.update_rc, "", "update refused")
        next(g for g in self.goals if g["id"] == gid)[field] = value
        return _Proc(0, "", "")

    def adds(self):
        return [c for c in self.calls if c["script"] == "aspirations-add-goal.sh"]

    def updates(self):
        return [c for c in self.calls if c["script"] == "aspirations-update-goal.sh"]


class _Chain:
    """Real detect_overcap -> real probe, over a fake queue."""

    def __init__(self, monkeypatch, tmp_path, machine_local=None):
        self.tmp = tmp_path
        self.log = tmp_path / "store-hygiene-overcap-log.jsonl"
        self.reports: list[dict] = []
        self.machine_local = dict(machine_local or {})   # path -> True/False/None; default shared
        self.world = _World()
        monkeypatch.setattr(jh, "_overcap_log_path", lambda: self.log)
        monkeypatch.setattr(jh, "sweep",
                            lambda apply=False: {"swept": len(self.reports),
                                                 "reports": list(self.reports)})
        monkeypatch.setattr(jh, "_machine_local",
                            lambda path: (self.machine_local.get(path, False), None))
        monkeypatch.setattr(pf, "open_goal_records", self.world.open_goal_records)
        monkeypatch.setattr(pf, "open_goal_exists", self.world.open_goal_exists)
        monkeypatch.setattr(WD, "subprocess", types.SimpleNamespace(run=self.world.run))
        # This chain stubs the boundary itself, so it opts out of the probe's
        # refusal to write under pytest; test_the_probe_refuses_to_write_under_pytest
        # puts the default back and proves what it does.
        monkeypatch.setattr(WD.StoreOvercapProbe, "REFUSE_WRITES_UNDER_PYTEST", False)
        self.ctx = _Ctx(tmp_path)
        self.probe = WD.StoreOvercapProbe(self.ctx)

    def poll(self, ratios, probe=None, **row):
        """One hourly poll with the sweep pinned to `ratios` ({path: x-cap}).
        The 60-minute pacing is defeated, not under test here."""
        self.reports = [_report(p, r, **row) for p, r in ratios.items()]
        p = probe or self.probe
        p.last_polled = None
        return p.check()

    def surface(self, ratios):
        """Two consecutive polls: the second is the one that can surface."""
        first = self.poll(ratios)
        assert first == [], "a first recorded run must never fire"
        return self.poll(ratios)


def _body(call):
    return json.loads(call["input"])


# -- 1. OUTCOME 1 (test half) --------------------------------------------------

def test_a_shared_store_driven_over_cap_reaches_a_goal(monkeypatch, tmp_path):
    c = _Chain(monkeypatch, tmp_path)
    events = c.surface({SHARED: 1.32})

    assert [e.event for e in events] == ["store_overcap"]
    assert events[0].severity == "critical"
    adds = c.world.adds()
    assert len(adds) == 1, "exactly one goal for one surfacing"

    # Target and override: the same escalation aspiration the sibling probes use,
    # never a literal, and the dup-gate override an unattended filer needs.
    argv = adds[0]["argv"]
    assert argv[1] == "core/scripts/aspirations-add-goal.sh"
    assert argv[2] == WD.ESCALATION_ASP
    assert argv[3:5] == ["--source", WD.ESCALATION_SOURCE]
    assert argv[5] == "--override-duplication" and argv[6].strip()
    assert adds[0]["cwd"] == str(c.tmp)

    body = _body(adds[0])
    assert body["origin_signal"] == KEY
    assert body["priority"] == "HIGH"
    assert body["participants"] == ["agent"]
    assert body["category"] == "framework-infrastructure"
    assert "intended_agent" not in body, "the remedy is fleet-wide, any Body may take it"
    assert body["title"].startswith("Investigate: ")
    assert SHARED in body["title"] and "1.32x" in body["title"]
    # The title states what was OBSERVED; the inferred cause is labelled as such
    # in the description (verify-before-assuming, causal attribution).
    assert "because" not in body["title"].lower()
    desc = body["description"]
    assert SHARED in desc and "g-115-1651" in desc
    assert "Plausible mechanism (inferred, not verified)" in desc
    assert "detect-overcap --no-record" in desc and "SNAPSHOT" in desc

    gid = c.world.goals[0]["id"]
    assert events[0].payload["goal"] == {"filed": True, "goal_id": gid, "error": None}
    assert events[0].payload["shared_surfaced"] == [SHARED]
    assert gid in events[0].summary


# -- 2. OUTCOME 2 ---------------------------------------------------------------

def test_a_store_that_stays_over_cap_files_nothing_more(monkeypatch, tmp_path):
    """The ratchet's once-per-episode behaviour survives the consumer."""
    c = _Chain(monkeypatch, tmp_path)
    c.surface({SHARED: 1.32})
    assert len(c.world.adds()) == 1

    for _ in range(5):
        assert c.poll({SHARED: 1.32}) == []
    # Drift under the regress factor is quiet too (1.32 * 1.5 = 1.98).
    assert c.poll({SHARED: 1.9}) == []
    assert len(c.world.adds()) == 1

    # A real regression surfaces again -- but the open goal covers the episode.
    regressed = c.poll({SHARED: 1.32 * jh.OVERCAP_REGRESS_FACTOR + 0.05})
    assert [e.event for e in regressed] == ["store_overcap"]
    assert regressed[0].payload["goal"]["dedup"] is True
    assert len(c.world.adds()) == 1


def test_a_goal_closed_while_the_store_is_still_over_cap_is_not_refiled(monkeypatch, tmp_path):
    """OUTCOME 2, the case dedup alone cannot cover: an agent closes the goal and
    the store stays over cap. A level-triggered filer would raise a fresh goal on
    the next poll and every poll after; the ratchet's edge keeps it quiet."""
    c = _Chain(monkeypatch, tmp_path)
    c.surface({SHARED: 1.32})
    c.world.goals[0]["status"] = "completed"
    assert not c.world.open_goal_exists(KEY, None, None)
    for _ in range(4):
        assert c.poll({SHARED: 1.32}) == []
    assert len(c.world.adds()) == 1


# -- 3-5. delivery survives a failed filing ------------------------------------

def test_a_failed_filing_is_retried_not_lost(monkeypatch, tmp_path):
    c = _Chain(monkeypatch, tmp_path)
    c.world.add_rc = 1
    events = c.surface({SHARED: 1.32})
    assert events[0].payload["goal"]["filed"] is False
    assert "Could not acquire lock" in events[0].payload["goal"]["error"]
    assert "retrying next poll" in events[0].summary
    assert c.probe.unfiled == [SHARED]

    # The ratchet has already marked the store, so the detector is silent about
    # it now. Only the retry can deliver it.
    again = c.poll({SHARED: 1.32})
    assert [e.event for e in again] == ["store_overcap"]
    assert again[0].payload["surfaced"] == []
    assert again[0].payload["retried"] == [SHARED]
    assert "[retry]" in again[0].summary
    assert len(c.world.adds()) == 2
    assert not c.world.open_goal_exists(KEY, None, None)

    c.world.add_rc = 0
    healed = c.poll({SHARED: 1.32})
    assert healed[0].payload["goal"]["filed"] is True
    assert c.probe.unfiled == []
    assert c.world.open_goal_exists(KEY, None, None)
    assert c.poll({SHARED: 1.32}) == []
    assert len(c.world.adds()) == 3


def test_the_retry_survives_a_process_restart(monkeypatch, tmp_path):
    c = _Chain(monkeypatch, tmp_path)
    c.world.add_rc = 1
    c.surface({SHARED: 1.32})
    # Through JSON, as run_tick's state file carries it between invocations.
    saved = json.loads(json.dumps(c.probe.to_dict()))
    fresh = WD.StoreOvercapProbe(c.ctx)
    fresh.from_dict(saved)
    assert fresh.unfiled == [SHARED]

    c.world.add_rc = 0
    events = c.poll({SHARED: 1.32}, probe=fresh)
    assert events[0].payload["goal"]["filed"] is True
    assert fresh.unfiled == []


def test_a_retry_for_a_store_that_has_since_cleared_is_dropped(monkeypatch, tmp_path):
    c = _Chain(monkeypatch, tmp_path)
    c.world.add_rc = 1
    c.surface({SHARED: 1.32})
    c.world.add_rc = 0
    events = c.poll({SHARED: 0.4})
    assert c.probe.unfiled == []
    assert len(c.world.adds()) == 1, "the one failed attempt; a cleared store is not retried"
    assert [e.event for e in events] == ["store_overcap_cleared"]


# -- 6. clear closes, recurrence re-files --------------------------------------

def test_clear_closes_the_goal_and_a_recurrence_files_a_new_one(monkeypatch, tmp_path):
    c = _Chain(monkeypatch, tmp_path)
    c.surface({SHARED: 1.32})
    gid = c.world.goals[0]["id"]
    assert c.poll({SHARED: 1.32}) == []           # carried quietly, goal stays open

    events = c.poll({SHARED: 0.5})                # the sweep brought it under cap
    assert [e.event for e in events] == ["store_overcap_cleared"]
    assert events[0].severity == "info"
    assert events[0].payload["closed"]["closed"] == [gid]
    goal = c.world.goals[0]
    assert goal["status"] == "skipped", "skipped, never completed: no investigation happened"
    assert "No investigation was performed" in goal["outcome_note"]
    # The note goes in BEFORE the status, so a partial failure leaves an open
    # goal carrying a true sentence (guard-3437 constraint 3).
    order = [(u["argv"][2], u["argv"][3]) for u in c.world.updates()]
    assert order == [(gid, "outcome_note"), (gid, "status")]
    assert c.world.updates()[1]["argv"][4] == "skipped"
    assert c.world.updates()[0]["argv"][-2:] == ["--source", "world"]

    # The last leg of the lifecycle: the alarm can fire again after it cleared.
    assert c.poll({SHARED: 1.32}) == []
    again = c.poll({SHARED: 1.32})
    assert [e.event for e in again] == ["store_overcap"]
    assert len(c.world.adds()) == 2
    assert c.world.goals[-1]["id"] != gid and c.world.goals[-1]["status"] == "pending"


# -- 7-9. which stores, and who else may have filed ----------------------------

def test_machine_local_stores_stay_quiet(monkeypatch, tmp_path):
    c = _Chain(monkeypatch, tmp_path, machine_local={LOCAL: True})
    events = c.surface({LOCAL: 8.0})
    # The existing log event is unchanged; it just has no consumer to deliver to.
    assert [e.event for e in events] == ["store_overcap"]
    assert events[0].payload["goal"] is None
    assert "machine-local only" in events[0].summary
    assert c.world.adds() == []
    assert c.probe.unfiled == []


def test_an_unclassifiable_store_is_filed_as_shared(monkeypatch, tmp_path):
    c = _Chain(monkeypatch, tmp_path, machine_local={SHARED: None})
    events = c.surface({SHARED: 1.32})
    assert events[0].payload["shared_surfaced"] == [SHARED]
    assert len(c.world.adds()) == 1


def test_a_mixed_surfacing_names_only_the_shared_store(monkeypatch, tmp_path):
    c = _Chain(monkeypatch, tmp_path, machine_local={LOCAL: True})
    events = c.surface({SHARED: 1.32, LOCAL: 8.0})
    assert set(events[0].payload["surfaced"]) == {SHARED, LOCAL}
    assert events[0].payload["shared_surfaced"] == [SHARED]
    body = _body(c.world.adds()[0])
    assert SHARED in body["title"]
    assert LOCAL not in body["title"] and LOCAL not in body["description"]


def test_an_open_goal_already_covering_the_episode_is_not_duplicated(monkeypatch, tmp_path):
    c = _Chain(monkeypatch, tmp_path)
    c.world.seed()                                # another box filed first
    events = c.surface({SHARED: 1.32})
    assert c.world.adds() == []
    assert events[0].payload["goal"]["dedup"] is True
    assert "an open goal already covers it" in events[0].summary
    assert c.probe.unfiled == [], "a covered episode is not retried"


# -- 10-12. the close ----------------------------------------------------------

def test_close_is_not_gated_on_the_probes_own_state(monkeypatch, tmp_path):
    """guard-3437: a goal filed by another box, or by this probe before a state
    reset, must still be closed by a probe that has never seen the episode."""
    c = _Chain(monkeypatch, tmp_path)
    seeded = c.world.seed()
    events = c.poll({SHARED: 0.4})                # no marks, nothing unfiled, nothing over
    assert c.probe.unfiled == []
    assert seeded["status"] == "skipped"
    assert [e.event for e in events] == ["store_overcap_cleared"]


@pytest.mark.parametrize("status,claimed_by", [
    ("in-progress", None), ("pending", "some-agent"), ("blocked", None)])
def test_a_started_goal_is_never_closed_from_under_its_owner(
        monkeypatch, tmp_path, status, claimed_by):
    c = _Chain(monkeypatch, tmp_path)
    seeded = c.world.seed(status=status, claimed_by=claimed_by)
    assert c.poll({SHARED: 0.4}) == []
    assert c.world.updates() == []
    assert seeded["status"] == status and seeded["claimed_by"] == claimed_by


def test_a_blind_measurement_is_not_a_clear(monkeypatch, tmp_path):
    """`line_bounded` 0 means the sweep resolved no store at all; an empty
    over_now over no population is not "back under cap" (guard-2273)."""
    c = _Chain(monkeypatch, tmp_path)
    seeded = c.world.seed()
    assert c.poll({}) == []
    assert c.world.updates() == [] and seeded["status"] == "pending"


def test_machine_local_debt_does_not_hold_the_goal_open(monkeypatch, tmp_path):
    c = _Chain(monkeypatch, tmp_path, machine_local={LOCAL: True})
    c.surface({SHARED: 1.32, LOCAL: 8.0})         # a goal for SHARED; LOCAL is log-only
    assert len(c.world.adds()) == 1
    events = c.poll({SHARED: 0.4, LOCAL: 8.0})    # LOCAL carried quietly, SHARED cleared
    assert [e.event for e in events] == ["store_overcap_cleared"]
    assert c.world.goals[0]["status"] == "skipped"
    assert events[0].payload["ratcheted_quiet"] == [LOCAL]


def test_a_shared_store_still_over_cap_holds_the_goal_open(monkeypatch, tmp_path):
    c = _Chain(monkeypatch, tmp_path)
    c.surface({SHARED: 1.32, SHARED_2: 1.4})
    assert len(c.world.adds()) == 1
    events = c.poll({SHARED: 1.32, SHARED_2: 0.4})   # one cleared, one still over
    assert [e.event for e in events] == ["store_overcap_cleared"]
    assert events[0].payload["closed"] is None
    assert c.world.goals[0]["status"] == "pending" and c.world.updates() == []


# -- 13. never raises ----------------------------------------------------------

def test_a_filing_exception_never_escapes_the_tick(monkeypatch, tmp_path):
    c = _Chain(monkeypatch, tmp_path)
    c.world.add_raises = OSError("bash not found")
    events = c.surface({SHARED: 1.32})
    assert events[0].payload["goal"]["filed"] is False
    assert "OSError" in events[0].payload["goal"]["error"]
    assert c.probe.unfiled == [SHARED]


# -- 14-16. the key, the body, the emitter -------------------------------------

def test_the_key_is_sanctioned_and_fleet_scoped():
    assert osig.is_valid(KEY), "gates/origin_signal.py rewrites a non-sanctioned prefix"
    # guard-2107: the condition and the queue are fleet-scoped, so no agent or box.
    assert "testagent" not in KEY and WD._box_id() not in KEY


def test_the_filed_goal_is_routed_to_pending_not_parked_as_a_candidate(monkeypatch, tmp_path):
    """Delivery is only real if the selector can SEE the goal. With the candidate
    tier on, an `investigate:` head is parked as a `candidate` that the selector
    never offers, and this probe's first filing landed that way (g-115-11838,
    retired). Asked of the REAL gate against the REAL config, for the body the
    probe actually files, with an `investigate:` control that proves the tier is
    on and the question can come back the other way."""
    from gates import intake_route
    cfg = intake_route.load_config(CORE_SCRIPTS.parent.parent)
    if not cfg["enabled"]:
        pytest.skip("the candidate tier is off in this tree: nothing to route around")
    c = _Chain(monkeypatch, tmp_path)
    c.surface({SHARED: 1.32})
    body = _body(c.world.adds()[0])
    assert intake_route.route_intake(body, config=cfg, user_context=False) == "pending"
    control = dict(body, origin_signal="investigate:" + KEY.split(":", 1)[1])
    assert intake_route.route_intake(control, config=cfg, user_context=False) == "candidate"


# -- 18. the refusal to write under pytest -------------------------------------

def _canned(over_now, surfaced=(), cleared=(), line_bounded=40):
    """A detect_overcap report, for tests that must not touch the real detector."""
    return {"threshold": 1.25, "swept": 52, "line_bounded": line_bounded,
            "over_now": over_now, "surfaced": list(surfaced),
            "newly_over": list(surfaced), "regressed": [], "ratcheted_quiet": [],
            "ratchet_cleared": list(cleared), "repeat_offenders": list(surfaced),
            "regress_factor": 1.5, "recorded": True}


_SHARED_ROW = {"ratio": 1.32, "total": 132, "bound": 100, "mode": "cap",
               "action": "would-cap", "owner_goal": "g-fixture", "machine_local": False}


def test_the_refusal_defaults_on():
    assert WD.StoreOvercapProbe.REFUSE_WRITES_UNDER_PYTEST is True


def test_a_default_probe_ticked_under_pytest_cannot_reach_the_queue(monkeypatch, tmp_path):
    """The offender's exact shape: a DEFAULT probe, the default refusal, and a
    detector that reports a surfaced shared store, over a boundary that fails
    loudly on any call. The existing worker-role test does this to every reducer
    probe against the REAL project root, and the first version of this consumer
    filed a real goal from it (g-115-11838, retired)."""
    def boom(*args, **kwargs):
        raise AssertionError("a test reached the goal-queue boundary")
    monkeypatch.setattr(WD, "subprocess", types.SimpleNamespace(run=boom))
    monkeypatch.setattr(pf, "open_goal_exists", lambda *a, **k: False)
    monkeypatch.setattr(jh, "detect_overcap",
                        lambda record=True: _canned({SHARED: _SHARED_ROW}, surfaced=[SHARED]))
    events = WD.StoreOvercapProbe(_Ctx(tmp_path)).check()
    assert [e.event for e in events] == ["store_overcap"]
    assert "refused: running under pytest" in events[0].payload["goal"]["error"]

    # The close is refused too: a real pending goal is on the queue and nothing is
    # over cap, which is the state that would retire it from a test.
    monkeypatch.setattr(pf, "open_goal_records", lambda *a, **k: [
        {"id": "g-real-1", "status": "pending", "claimed_by": None, "_source": "world"}])
    monkeypatch.setattr(jh, "detect_overcap",
                        lambda record=True: _canned({}, cleared=[SHARED]))
    cleared = WD.StoreOvercapProbe(_Ctx(tmp_path)).check()
    assert [e.event for e in cleared] == ["store_overcap_cleared"]
    assert "refused: running under pytest" in cleared[0].payload["closed"]["detail"]


def test_a_refused_filing_is_retried_once_a_test_opts_out(monkeypatch, tmp_path):
    c = _Chain(monkeypatch, tmp_path)
    monkeypatch.setattr(WD.StoreOvercapProbe, "REFUSE_WRITES_UNDER_PYTEST", True)
    c.surface({SHARED: 1.32})
    assert c.world.adds() == [] and c.probe.unfiled == [SHARED]
    monkeypatch.setattr(WD.StoreOvercapProbe, "REFUSE_WRITES_UNDER_PYTEST", False)
    events = c.poll({SHARED: 1.32})
    assert events[0].payload["goal"]["filed"] is True and c.probe.unfiled == []


def test_an_unreadable_classification_is_said_in_the_goal(monkeypatch, tmp_path):
    """An unreadable classifier is filed as shared, so the goal must not state it
    as a fact; a classified-shared store carries no such note."""
    c = _Chain(monkeypatch, tmp_path)
    c.surface({SHARED: 1.32})
    assert "UNREADABLE" not in _body(c.world.adds()[0])["description"]

    (tmp_path / "second").mkdir()
    c2 = _Chain(monkeypatch, tmp_path / "second")
    monkeypatch.setattr(jh, "_machine_local",
                        lambda path: (None, "RuntimeError: classifier down"))
    c2.surface({SHARED: 1.32})
    desc = _body(c2.world.adds()[0])["description"]
    assert ("machine-local classification UNREADABLE (RuntimeError: classifier down), "
            "treated as shared") in desc


def test_the_filed_body_passes_the_real_goal_validator(monkeypatch, tmp_path):
    import aspirations
    c = _Chain(monkeypatch, tmp_path)
    c.surface({SHARED: 1.32})
    body = _body(c.world.adds()[0])
    aspirations.validate_goal(dict(body, id="g-999-99", status="pending"))
    # The payload crosses a shell stdin, so it must survive as pure ASCII JSON
    # (the filing path's rb-137 note): json.dumps(ensure_ascii=True) guarantees it.
    assert c.world.adds()[0]["input"].isascii()


def test_the_events_serialise_through_the_real_emitter(monkeypatch, tmp_path):
    c = _Chain(monkeypatch, tmp_path)
    ctx = WD.WatchdogContext(agent_name="testagent", agent_dir=tmp_path,
                             project_root_path=tmp_path)
    log = tmp_path / "watchdog-testagent.jsonl"
    emitted = c.surface({SHARED: 1.32}) + c.poll({SHARED: 0.4})
    assert [e.event for e in emitted] == ["store_overcap", "store_overcap_cleared"]
    for e in emitted:
        WD.emit_event(ctx, log, e)
    rows = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["payload"]["goal"]["filed"] is True
    assert rows[1]["payload"]["closed"]["closed"] == [c.world.goals[0]["id"]]


# -- 17. state, elision, refusal -----------------------------------------------

def test_state_round_trips_and_tolerates_garbage(tmp_path):
    p = WD.StoreOvercapProbe(_Ctx(tmp_path))
    p.last_polled, p.unfiled = 123.0, [SHARED]
    q = WD.StoreOvercapProbe(_Ctx(tmp_path))
    q.from_dict(json.loads(json.dumps(p.to_dict())))
    assert (q.last_polled, q.unfiled) == (123.0, [SHARED])
    # A state file from before this field existed, and several malformed ones.
    for bad in ({"last_polled": 5.0}, {}, {"unfiled": None}, {"unfiled": "x"}, {"unfiled": 3}):
        r = WD.StoreOvercapProbe(_Ctx(tmp_path))
        r.from_dict(bad)
        assert r.unfiled == []


def test_the_title_elides_past_three_stores_but_the_description_names_all(monkeypatch, tmp_path):
    c = _Chain(monkeypatch, tmp_path)
    stores = {"/fixture/world/s%d.jsonl" % i: 1.3 + i / 100 for i in range(5)}
    c.surface(stores)
    body = _body(c.world.adds()[0])
    assert "+2 more" in body["title"]
    ordered = sorted(stores)
    assert all(p in body["title"] for p in ordered[:3])
    assert not any(p in body["title"] for p in ordered[3:])
    assert all(p in body["description"] for p in ordered)


def test_a_refused_store_carries_the_detectors_note_into_the_goal(monkeypatch, tmp_path):
    """A store no sweep can bring down must not be filed as 'the sweep is behind'."""
    c = _Chain(monkeypatch, tmp_path)
    c.poll({SHARED: 1.32}, action="refused-no-recovery-layer")
    c.poll({SHARED: 1.32}, action="refused-no-recovery-layer")
    note = jh.detect_overcap(record=False)["unbounded_by_refusal_note"]
    assert note, "the detector must attach its note to a refused store"
    desc = _body(c.world.adds()[0])["description"]
    assert "Refused by the sweep's recovery-layer gate" in desc and note in desc
