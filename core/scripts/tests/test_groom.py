"""groom.py — the B4 grooming engine (; goal-intake-management.md §5-6).

What the goal names, each pinned below: the promote cap is unexceedable by
construction, ledger rows carry the §5 schema and the read API samples them
reproducibly, move-on-touch moves exactly what §5 allows and moves BEFORE it
writes, and a fresh touch keeps a candidate out of every other groomer's bite.
The daemon half of the re-home (byte-identical adopted copy, pointer shape,
pointer-aware lookups over HTTP) is pinned in
mind_api/tests/test_rehome_goal_endpoint.py.
"""
import copy
import datetime
import json
import os
import subprocess
from pathlib import Path

import pytest

import aspirations
import coordination_merge
import groom
from _runtime_bash import bash_cmd
from _sweep_write_guard import PROV_AUTHORITATIVE, _find_goal as sweep_find_goal
from gates import candidate_transition as ct

SCRIPTS = Path(groom.__file__).resolve().parent

AGENT = "agent-a"
ROSTER = "agent-a,agent-b,agent-c"
NOW = datetime.datetime(2026, 10, 1, 12, 0, 0)
STAMP = "2026-10-01T12:00:00"


def _goal(gid, **kw):
    g = {"id": gid, "title": f"title {gid}", "status": "candidate",
         "recurring": False, "created": "2026-09-01"}
    g.update(kw)
    return g


class FakeDaemon:
    """The daemon reduced to what groom.py touches. update-goal enforces the §2
    table and appends B1c's ledger row on a committed candidate transition,
    exactly as the real write path does; lookups prefer the live copy over a
    rehome pointer, as aspirations_write._find_goal now does."""

    def __init__(self, asps, world):
        self.asps, self.world, self.calls = asps, world, []
        self.bodies = {}   # goal id -> the status body groom actually sent

    def find(self, gid):
        pointer = None
        for a in self.asps:
            for g in a["goals"]:
                if g["id"] == gid:
                    if g["status"] != "superseded":
                        return a, g
                    pointer = pointer or (a, g)
        return pointer

    def read(self, source):
        return copy.deepcopy(self.asps)

    def reread(self, source, gid):
        hit = self.find(gid)
        return (copy.deepcopy(hit[1]) if hit else None), PROV_AUTHORITATIVE

    def update_goal(self, gid, field, value, source="world"):
        self.calls.append(("update", gid, field))
        _asp, g = self.find(gid)
        if field != "status":
            g[field] = value
            return {"ok": True, "goal": copy.deepcopy(g)}
        new, note = value["value"], value.get("outcome_note")
        self.bodies[gid] = dict(value)
        prev = g["status"]
        if prev == "candidate" and not ct.evaluate(prev, new)["allowed"]:
            raise groom._rt.RtError("refused", status=400,
                                    body=json.dumps({"error": "candidate_transition_forbidden"}))
        g["status"] = new
        if note is not None:
            g["outcome_note"] = note
        if prev == "candidate":
            # : the real write path validates the writer's half of the
            # row with ct.caller_channel and merges it, so the verdict groom names
            # reaches the ledger. Mirror that, or this fake could never see it.
            cc_verdict, cc_evidence, cc_err = ct.caller_channel(
                new, value.get("ledger_evidence"), value.get("ledger_verdict"))
            assert cc_err is None, cc_err
            ct.append_ledger(self.world, goal_id=gid, new_status=new, agent=AGENT,
                             verdict=cc_verdict,
                             evidence={"outcome_note": note, **cc_evidence})
        return {"ok": True, "goal": copy.deepcopy(g)}

    def rehome(self, gid, to_asp, reason=None, source="world", dry_run=False):
        self.calls.append(("rehome", gid, to_asp))
        src, g = self.find(gid)
        target = next(a for a in self.asps if a["id"] == to_asp)
        target["goals"].append(dict(g, rehomed_from=src["id"]))
        g["status"], g["rehomed_to"] = "superseded", to_asp
        return {"ok": True, "goal_id": gid, "from_asp": src["id"], "to_asp": to_asp,
                "adopted_new": True}

    def survivor_pointer(self, survivor, source, goal, evidence, ts):
        self.calls.append(("pointer", survivor, goal["id"]))
        return True, ""


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("GATE_LOG_ALLOW_PYTEST", "1")  # let ct.append_ledger write (to tmp)
    monkeypatch.setattr(groom, "WORLD_DIR", str(tmp_path))
    monkeypatch.setattr(groom, "AGENT_NAME", AGENT)
    monkeypatch.setattr(groom, "_now", lambda: NOW)
    monkeypatch.setattr(groom, "load_config", lambda: (dict(groom.SPEC_DEFAULTS), "test"))
    monkeypatch.setattr(groom, "load_role_multipliers", lambda: {
        "agent-a": {"framework": 1.5, "research": 1.5, "product": 0.8, "unclassified": 0.3},
        "agent-b": {"product": 1.5, "framework": 1.0},
        "agent-c": {"hygiene": 1.5}})

    def install(asps):
        d = FakeDaemon(asps, tmp_path)
        monkeypatch.setattr(groom, "read_aspirations", d.read)
        monkeypatch.setattr(groom, "reread", d.reread)
        monkeypatch.setattr(groom, "append_survivor_pointer", d.survivor_pointer)
        monkeypatch.setattr(groom._rt, "aspirations_update_goal", d.update_goal)
        monkeypatch.setattr(groom._rt, "aspirations_rehome_goal", d.rehome)
        return d
    return install


def verdict(*argv):
    return groom.main(["verdict", *argv])


# --- (a) the promote cap ------------------------------------------------------

def test_promote_cap_is_unexceedable_by_construction(daemon):
    d = daemon([{"id": "asp-200", "status": "active",
                 "goals": [_goal(f"g-200-0{i}") for i in range(1, 6)]}])
    rcs = [verdict("--goal", f"g-200-0{i}", "--verdict", "promote", "--evidence", "real work")
           for i in range(1, 6)]
    assert rcs == [0, 0, 0, groom.RC_CAP, groom.RC_CAP]
    assert [g["id"] for g in d.asps[0]["goals"] if g["status"] == "pending"] == [
        "g-200-01", "g-200-02", "g-200-03"]
    rows, malformed = groom.read_ledger()
    assert [r["verdict"] for r in rows] == ["promote"] * 3 and malformed == 0
    # a refused promote writes NOTHING
    assert not [c for c in d.calls if c[1] in ("g-200-04", "g-200-05")]


def test_no_argument_can_mint_a_fresh_budget(daemon):
    daemon([{"id": "asp-200", "status": "active", "goals": [_goal("g-200-01")]}])
    with pytest.raises(SystemExit) as exc:
        verdict("--goal", "g-200-01", "--verdict", "promote", "--evidence", "x",
                "--firing-id", "a-fresh-one")
    assert exc.value.code == 2  # there is no firing id to supply


def test_the_window_counts_only_this_agents_grooming_promotes():
    since = NOW - datetime.timedelta(hours=20)
    rows = [
        {"verdict": "promote", "agent": AGENT, "ts": "2026-10-01T11:00:00"},     # counts
        {"verdict": "promote", "agent": AGENT, "ts": "2026-09-30T10:00:00"},     # before window
        {"verdict": "promote", "agent": "agent-b", "ts": "2026-10-01T11:00:00"},  # other agent
        {"verdict": "promote", "agent": AGENT, "ts": "2026-10-01T11:00:00",
         "evidence": {"promoted_by": "starvation-failsafe"}},                     # §4, not grooming
        {"verdict": "promote", "agent": AGENT, "ts": "2026-10-01T11:00:00",
         "evidence": {"promoted_by": "kill-switch-drain"}},                       # §9, not grooming
        {"verdict": "keep", "agent": AGENT, "ts": "2026-10-01T11:00:00"},
        {"verdict": "promote", "agent": f"{AGENT}@env-1", "ts": "not-a-time"},   # fails CLOSED
    ]
    assert groom.promotes_in_window(rows, AGENT, since) == 2


# --- (b) ledger schema + read API ---------------------------------------------

def test_ledger_rows_carry_the_section5_schema_and_sample_reproducibly(daemon, capsys):
    d = daemon([{"id": "asp-200", "status": "active", "goals": [
        _goal("g-200-01"), _goal("g-200-02", outcome_note="earlier finding"), _goal("g-200-03")]}])
    assert verdict("--goal", "g-200-01", "--verdict", "keep") == 0
    assert verdict("--goal", "g-200-02", "--verdict", "close-moot", "--evidence", "done elsewhere") == 0
    assert verdict("--goal", "g-200-03", "--verdict", "promote", "--evidence", "real work") == 0
    rows, _ = groom.read_ledger()
    assert all({"ts", "agent", "goal_id", "verdict", "evidence"} <= set(r) for r in rows)
    keep = next(r for r in rows if r["verdict"] == "keep")
    assert keep["goal_id"] == "g-200-01" and keep["evidence"]["groom_touched_at"] == STAMP
    g1, g2, _g3 = d.asps[0]["goals"]
    assert g1["status"] == "candidate" and g1["groom_touched_at"] == STAMP  # keep never transitions
    # the verdict note APPENDS (guard-5228)
    assert g2["outcome_note"] == f"earlier finding\n\nGROOM close-moot ({STAMP}, {AGENT}): done elsewhere"
    capsys.readouterr()
    assert groom.main(["ledger", "--sample", "2"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["matched"] == 3 and len(first["rows"]) == 2
    assert first["verdict_counts"] == {"keep": 1, "close-moot": 1, "promote": 1}
    assert groom.main(["ledger", "--sample", "2"]) == 0
    assert json.loads(capsys.readouterr().out)["rows"] == first["rows"]
    assert groom.main(["ledger", "--verdict", "keep", "--agent", AGENT]) == 0
    assert [r["goal_id"] for r in json.loads(capsys.readouterr().out)["rows"]] == ["g-200-01"]


# --- (c) move-on-touch --------------------------------------------------------

def _inbox_world(triage=True):
    return [
        {"id": "asp-115", "status": "active",
         "goals": [_goal("g-115-01"), _goal("g-115-02", recurring=True)]},
        {"id": "asp-300", "status": "active",
         "goals": [_goal("g-300-01", status="pending"), _goal("g-300-02")]},
        {"id": "asp-301", "status": "active", "triage_inbox": triage, "goals": []},
        {"id": "asp-302", "status": "completed", "goals": []},
    ]


def test_move_on_touch_moves_first_then_writes_onto_the_moved_copy(daemon):
    d = daemon(_inbox_world())
    assert verdict("--goal", "g-115-01", "--verdict", "merge", "--survivor", "g-300-01",
                   "--evidence", "same scope") == 0
    assert [c[0] for c in d.calls] == ["rehome", "update", "pointer"]
    moved = next(g for g in d.asps[2]["goals"] if g["id"] == "g-115-01")
    assert moved["rehomed_from"] == "asp-115" and moved["status"] == "superseded"
    assert "merged into survivor g-300-01" in moved["outcome_note"]


def test_move_on_touch_moves_only_what_section5_allows(daemon):
    d = daemon(_inbox_world())
    assert verdict("--goal", "g-115-01", "--verdict", "keep") == 0             # keep never moves
    assert verdict("--goal", "g-115-02", "--verdict", "close-moot", "--evidence", "moot") == 0
    assert not [c for c in d.calls if c[0] == "rehome"]                          # recurring never moves
    before = list(d.calls)
    assert verdict("--goal", "g-300-02", "--verdict", "close-moot", "--evidence", "x",
                   "--rehome-to", "asp-301") == groom.RC_INVALID                 # not in the legacy inbox
    for bad in ("asp-302", "asp-115", "asp-999"):                                 # dead, self, absent
        assert verdict("--goal", "g-115-01", "--verdict", "close-moot", "--evidence", "x",
                       "--rehome-to", bad) == groom.RC_INVALID
    assert d.calls == before                                                      # refused before any write


def test_a_laneless_move_without_a_triage_inbox_is_deferred_not_guessed(daemon, capsys):
    d = daemon(_inbox_world(triage=False))
    assert verdict("--goal", "g-115-01", "--verdict", "close-moot", "--evidence", "moot") == 0
    assert json.loads(capsys.readouterr().out)["rehome"] == {"deferred": "no-triage-inbox"}
    assert not [c for c in d.calls if c[0] == "rehome"]
    assert d.asps[0]["goals"][0]["status"] == "skipped"


def test_a_failed_move_is_partial_and_the_verdict_still_lands(daemon, monkeypatch):
    d = daemon(_inbox_world())

    def broken(*a, **kw):
        raise groom._rt.RtError("route missing", status=404, body='{"error":"not_found"}')
    monkeypatch.setattr(groom._rt, "aspirations_rehome_goal", broken)
    assert verdict("--goal", "g-115-01", "--verdict", "close-moot", "--evidence", "moot") == groom.RC_PARTIAL
    assert d.asps[0]["goals"][0]["status"] == "skipped"


# --- (d) touch collision + affinity --------------------------------------------

def test_a_fresh_touch_keeps_a_candidate_out_of_the_bite(daemon, capsys):
    daemon([{"id": "asp-200", "status": "active", "goals": [
        _goal("g-200-01", work_class="framework", groom_touched_at="2026-10-01T11:00:00"),  # 1h
        _goal("g-200-02", work_class="framework", groom_touched_at="2026-09-30T10:00:00"),  # 26h
        _goal("g-200-03", work_class="framework")]}])
    assert groom.main(["bite", "--roster", ROSTER]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [r["goal_id"] for r in out["bite"]] == ["g-200-02", "g-200-03"]
    assert out["skipped_fresh_touch"] == 1


def test_bite_orders_by_affinity_then_age_and_partitions_the_rest(daemon, capsys, monkeypatch):
    unclassified = [f"g-201-{n:02d}" for n in range(1, 31)]
    daemon([{"id": "asp-200", "status": "active", "goals": [
        _goal("g-200-01", intended_agent="agent-b"),
        _goal("g-200-02", intended_agent="agent-a@env-1", created="2026-09-20"),
        _goal("g-200-03", work_class="product"),
        _goal("g-200-04", work_class="research", created="2026-08-01"),
    ] + [_goal(gid) for gid in unclassified]}])
    hashed = {}
    for agent in ROSTER.split(","):
        monkeypatch.setattr(groom, "AGENT_NAME", agent)
        assert groom.main(["bite", "--roster", ROSTER]) == 0
        out = json.loads(capsys.readouterr().out)
        ids = [r["goal_id"] for r in out["bite"]]
        if agent == "agent-a":
            assert ids[:2] == ["g-200-02", "g-200-04"]  # intent, then role class; tier beats age
            assert "g-200-01" not in ids and "g-200-03" not in ids
        hashed[agent] = {r["goal_id"] for r in out["bite"] if r["tier"] == "hash_partition"}
    # a partition: disjoint across groomers, and together it covers every unowned goal
    assert sum(len(s) for s in hashed.values()) == len(set().union(*hashed.values()))
    assert set().union(*hashed.values()) == set(unclassified)


def test_either_is_the_routers_no_opinion_value_not_another_agents_pin(daemon, capsys, monkeypatch):
    """B6 canary (alpha, 2026-10-02): the capability router stamps
    intended_agent="either" on ~87% of Investigate/Idea/Maintain filings. It names
    no agent, so it falls through to the class and partition tiers; read as a pin
    it left every such candidate in no agent's bite."""
    unclassified = [f"g-202-{n:02d}" for n in range(1, 31)]
    daemon([{"id": "asp-200", "status": "active", "goals": [
        _goal("g-200-01", intended_agent="either", work_class="research"),
        _goal("g-200-02", intended_agent="either", work_class="product"),
    ] + [_goal(gid, intended_agent="either") for gid in unclassified]}])
    hashed = {}
    for agent in ROSTER.split(","):
        monkeypatch.setattr(groom, "AGENT_NAME", agent)
        assert groom.main(["bite", "--roster", ROSTER]) == 0
        out = json.loads(capsys.readouterr().out)
        tiers = {r["goal_id"]: r["tier"] for r in out["bite"]}
        if agent == "agent-a":
            assert tiers["g-200-01"] == "role_top_class"  # research is agent-a's top class
            assert "g-200-02" not in tiers                # product is another agent's class
        hashed[agent] = {gid for gid, tier in tiers.items() if tier == "hash_partition"}
    # owned exactly once: disjoint across groomers, and together every unowned goal
    assert sum(len(s) for s in hashed.values()) == len(set().union(*hashed.values()))
    assert set().union(*hashed.values()) == set(unclassified)


# --- refusals ------------------------------------------------------------------

def test_a_stale_bite_never_writes(daemon):
    d = daemon([{"id": "asp-200", "status": "active", "goals": [
        _goal("g-200-01", status="pending"),
        _goal("g-200-02", claimed_by="agent-b", claimed_by_sid="sid-1")]}])
    assert verdict("--goal", "g-200-01", "--verdict", "close-moot", "--evidence", "x") == groom.RC_STALE
    assert verdict("--goal", "g-200-02", "--verdict", "promote", "--evidence", "x") == groom.RC_STALE
    assert verdict("--goal", "g-404-01", "--verdict", "keep") == groom.RC_STALE
    assert d.calls == []


def test_invalid_arguments_are_refused_before_any_write(daemon, monkeypatch):
    d = daemon([{"id": "asp-200", "status": "active", "goals": [
        _goal("g-200-01"), _goal("g-200-02", status="completed"), _goal("g-200-03", status="pending")]}])

    def rb_read(method, path, query=None, body=None, headers=None):
        if query == "id=rb-1":
            return json.dumps({"id": "rb-1"})
        raise groom._rt.RtError("not found", status=404, body='{"error":"not_found"}')
    monkeypatch.setattr(groom._rt, "rt_call", rb_read)
    for argv in (["--verdict", "merge", "--survivor", "g-200-02", "--evidence", "x"],   # terminal survivor
                 ["--verdict", "merge", "--survivor", "g-999-01", "--evidence", "x"],   # absent
                 ["--verdict", "merge", "--survivor", "g-200-01", "--evidence", "x"],   # itself
                 ["--verdict", "rb-route", "--rb-id", "rb-2", "--evidence", "x"],       # constructed id
                 ["--verdict", "rb-route", "--rb-id", "2", "--evidence", "x"],
                 ["--verdict", "promote"],                                              # no evidence
                 ["--verdict", "close-moot", "--survivor", "g-200-03", "--evidence", "x"]):
        assert verdict("--goal", "g-200-01", *argv) == groom.RC_INVALID, argv
    assert d.calls == []
    assert verdict("--goal", "g-200-01", "--verdict", "rb-route", "--rb-id", "rb-1",
                   "--evidence", "encoded") == 0
    assert d.asps[0]["goals"][0]["status"] == "skipped"
    assert "routed to rb-1" in d.asps[0]["goals"][0]["outcome_note"]


def test_rb_route_names_its_ledger_verdict_and_nothing_else_does(daemon, monkeypatch):
    """: skipped's table default is close-moot, so an rb-route row would
    read close-moot unless the writer names its verdict. groom passes it through
    the status write's ledger channel, and ONLY when it differs from the default:
    a grooming promote, merge or close-moot body carries no channel key, so its
    row is unchanged and carries no promoted_by."""
    d = daemon([{"id": "asp-200", "status": "active", "goals": [
        _goal("g-200-01"), _goal("g-200-02"), _goal("g-200-03"), _goal("g-200-04")]}])
    monkeypatch.setattr(groom._rt, "rt_call",
                        lambda method, path, query=None, body=None, headers=None:
                        json.dumps({"id": "rb-1"}))
    assert verdict("--goal", "g-200-01", "--verdict", "rb-route", "--rb-id", "rb-1",
                   "--evidence", "x") == 0
    assert verdict("--goal", "g-200-02", "--verdict", "close-moot", "--evidence", "x") == 0
    assert verdict("--goal", "g-200-03", "--verdict", "promote", "--evidence", "x") == 0
    assert verdict("--goal", "g-200-04", "--verdict", "merge", "--survivor", "g-200-03",
                   "--evidence", "x") == 0
    assert d.bodies["g-200-01"]["ledger_verdict"] == "rb-route"
    for gid in ("g-200-02", "g-200-03", "g-200-04"):
        assert not {"ledger_verdict", "ledger_evidence"} & set(d.bodies[gid]), (
            gid, d.bodies[gid])
    rows, _ = groom.read_ledger()
    by_goal = {r["goal_id"]: r for r in rows}
    assert {g: r["verdict"] for g, r in by_goal.items()} == {
        "g-200-01": "rb-route", "g-200-02": "close-moot",
        "g-200-03": "promote", "g-200-04": "merge"}
    assert all("promoted_by" not in r["evidence"] for r in rows)


def test_verdict_exit_codes_never_collide_with_python_or_argparse():
    assert {groom.RC_STALE, groom.RC_CAP, groom.RC_INVALID, groom.RC_PARTIAL}.isdisjoint({0, 1, 2})


def test_the_wrappers_exec_the_engine_and_pass_its_exit_code_through(tmp_path):
    env = dict(os.environ, MIND_WORLD=str(tmp_path), STORAGE_BACKEND="local")
    (tmp_path / ct.LEDGER_NAME).write_text(json.dumps(
        {"ts": STAMP, "agent": AGENT, "goal_id": "g-200-01", "verdict": "keep", "evidence": {}}) + "\n")

    def run(wrapper, *argv):
        return subprocess.run(bash_cmd(str(SCRIPTS / wrapper), *argv), capture_output=True,
                              text=True, env=env, timeout=120)
    led = run("groom-ledger.sh", "--verdict", "keep")
    assert led.returncode == 0, led.stderr
    assert json.loads(led.stdout)["matched"] == 1
    for wrapper in ("groom-bite.sh", "groom-verdict.sh", "drain-candidates-to-pending.sh"):
        assert "usage: groom.py" in run(wrapper, "--help").stdout
    assert run("groom-verdict.sh", "--goal", "g-200-01").returncode == 2  # --verdict missing


# --- (d) the §9 kill-switch drain () -----------------------------------

def _drain_world():
    return [{"id": "asp-200", "status": "active", "goals": [
                _goal("g-200-01"), _goal("g-200-02", outcome_note="earlier finding"),
                _goal("g-200-03", status="pending")]},
            {"id": "asp-201", "status": "active", "goals": [_goal("g-201-01")]}]


def drain(*argv):
    return groom.main(["drain", "--source", "world", *argv])


def test_a_drain_dry_run_names_every_candidate_and_writes_nothing(daemon, capsys):
    d = daemon(_drain_world())
    assert drain() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True and out["candidates"] == 3
    assert sorted(out["would_promote"]) == ["g-200-01", "g-200-02", "g-201-01"]
    assert "promoted" not in out            # a dry run must never read as a finished drain
    assert out["by_aspiration"] == {"world:asp-200": 2, "world:asp-201": 1}
    assert d.calls == [] and groom.read_ledger() == ([], 0)
    assert [g["status"] for a in d.asps for g in a["goals"]] == [
        "candidate", "candidate", "pending", "candidate"]


def test_a_drain_promotes_every_candidate_through_the_status_write_and_stamps_the_row(daemon, capsys):
    d = daemon(_drain_world())
    assert drain("--apply") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is False and out["remaining"] == [] and out["refused"] == []
    assert sorted(out["promoted"]) == ["g-200-01", "g-200-02", "g-201-01"]
    assert {g["status"] for a in d.asps for g in a["goals"]} == {"pending"}
    assert ("update", "g-200-03", "status") not in d.calls   # a pending goal is never touched
    rows, malformed = groom.read_ledger()
    assert malformed == 0 and len(rows) == 3
    assert {r["verdict"] for r in rows} == {"promote"}
    assert {r["evidence"]["promoted_by"] for r in rows} == {groom.DRAIN_PROMOTER}
    assert d.bodies["g-200-01"]["ledger_evidence"] == {"promoted_by": "kill-switch-drain"}
    assert d.asps[0]["goals"][1]["outcome_note"].startswith(
        "earlier finding\n\nKILL-SWITCH DRAIN (" + STAMP)


def test_a_drain_is_not_a_grooming_promote_and_spends_no_cap(daemon, capsys):
    cap = groom.SPEC_DEFAULTS["promote_cap_per_firing"]
    d = daemon([{"id": "asp-200", "status": "active",
                 "goals": [_goal(f"g-200-{i:02d}") for i in range(1, cap + 3)]}])
    assert drain("--apply") == 0            # more candidates than the cap, in one pass
    capsys.readouterr()
    assert [g["status"] for g in d.asps[0]["goals"]] == ["pending"] * (cap + 2)
    budget = groom.promote_budget(dict(groom.SPEC_DEFAULTS), AGENT, NOW)
    assert budget["used"] == 0 and budget["remaining"] == cap   # the groomers' budget is whole


def test_a_drain_that_leaves_a_candidate_behind_is_partial_and_says_which(daemon, monkeypatch, capsys):
    d = daemon(_drain_world())
    real = d.update_goal

    def flaky(gid, field, value, source="world"):
        if gid == "g-200-02":               # a write the store of record refuses
            raise groom._rt.RtError("boom", status=500, body='{"error":"store_down"}')
        if gid == "g-201-01":               # another groomer promoted it first
            d.find(gid)[1]["status"] = "pending"
            raise groom._rt.RtError("refused", status=400,
                                    body=json.dumps({"error": "invalid_status_transition"}))
        return real(gid, field, value, source)
    monkeypatch.setattr(groom._rt, "aspirations_update_goal", flaky)
    assert drain("--apply") == groom.RC_PARTIAL
    out = json.loads(capsys.readouterr().out)
    assert out["promoted"] == ["g-200-01"]
    assert out["remaining"] == ["g-200-02"]   # the verdict is the fresh read, not the write answers
    assert {r["goal_id"]: r["rc"] for r in out["refused"]} == {
        "g-200-02": groom.RC_ERROR, "g-201-01": groom.RC_STALE}


def test_a_drain_walks_every_queue_and_reads_them_all_before_its_first_write(daemon, monkeypatch, capsys):
    d = daemon([{"id": "asp-200", "status": "active", "goals": [_goal("g-200-01")]},
                {"id": "asp-901", "status": "active", "goals": [_goal("g-901-01")]}])
    queues = {"world": "asp-200", "agent": "asp-901"}
    seen, real = [], d.update_goal

    def read(source):
        return [copy.deepcopy(a) for a in d.asps if a["id"] == queues[source]]

    def recording(gid, field, value, source="world"):
        seen.append((source, gid))
        return real(gid, field, value, source)
    monkeypatch.setattr(groom, "read_aspirations", read)
    monkeypatch.setattr(groom._rt, "aspirations_update_goal", recording)
    assert groom.main(["drain", "--apply"]) == 0           # the default source is `all`
    assert seen == [("world", "g-200-01"), ("agent", "g-901-01")]
    capsys.readouterr()

    seen.clear()
    d.asps[0]["goals"].append(_goal("g-200-02"))

    def agent_queue_down(source):
        if source == "agent":
            raise groom.GroomError(groom.RC_ERROR, "agent queue unreadable")
        return read(source)
    monkeypatch.setattr(groom, "read_aspirations", agent_queue_down)
    assert groom.main(["drain", "--apply"]) == groom.RC_ERROR
    assert seen == [] and d.find("g-200-02")[1]["status"] == "candidate"


@pytest.mark.parametrize("flag_on", [True, False])
def test_a_drain_says_when_the_flag_is_still_on(daemon, monkeypatch, capsys, flag_on):
    daemon(_drain_world())
    monkeypatch.setattr(groom.intake_route, "load_config", lambda root: {"enabled": flag_on})
    assert drain("--apply") == 0
    cap = capsys.readouterr()
    assert json.loads(cap.out)["flag_enabled"] is flag_on
    assert ("candidate_tier.enabled is still true" in cap.err) is flag_on


# --- the pointer a move leaves behind -------------------------------------------

POINTER = _goal("g-115-09", status="superseded", rehomed_to="asp-301", last_modified=STAMP)
LIVE = _goal("g-115-09", status="pending", rehomed_from="asp-115")


def test_every_write_path_lookup_prefers_the_live_copy_over_the_pointer():
    items = [{"id": "asp-115", "goals": [POINTER]}, {"id": "asp-301", "goals": [LIVE]}]
    assert sweep_find_goal(items, "g-115-09") is LIVE
    assert aspirations.find_goal_in_aspirations(items, "g-115-09")[2]["id"] == "asp-301"
    lone = [{"id": "asp-115", "goals": [POINTER]}]          # a lone pointer is still found
    assert sweep_find_goal(lone, "g-115-09") is POINTER
    assert aspirations.find_goal_in_aspirations(lone, "g-115-09")[2]["id"] == "asp-115"


def test_the_pointer_wins_the_cross_box_merge_only_because_it_is_newer():
    stale_peer = _goal("g-115-09", last_modified="2026-09-30T00:00:00")  # a box that never saw the move
    assert coordination_merge._merge_goal(POINTER, stale_peer)["status"] == "superseded"
    assert coordination_merge._merge_goal(stale_peer, POINTER)["status"] == "superseded"
    # positive control (guard-6913): `superseded` is not merge-terminal, so a
    # pointer that does not out-date the peer loses and the move comes back
    unstamped = dict(POINTER, last_modified="2026-09-29T00:00:00")
    assert coordination_merge._merge_goal(unstamped, stale_peer)["status"] == "candidate"
