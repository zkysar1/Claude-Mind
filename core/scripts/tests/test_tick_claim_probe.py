"""tick_claim_probe.py -- the one question the cron sync tick asks (, ,
g-375-137).

`none` is the only answer that lets iteration-push.sh --ff-only run the loop's
integrate, and `noloop` the only one that lets it fast-forward around agents/* files,
so every test that expects `held` or `unknown` is paired with the smallest change to
the SAME input that yields `none` or `noloop` (guard-4166): a test that could never
have produced the permissive answer proves nothing about the refusal.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

import tick_claim_probe as tcp  # noqa: E402

BODY = "c2503cad7f0b4ce6aaddff50b4dce84a"     # this box's worker Body session
OTHER_BOX = "47b99d45aa1146d6a2c3a0f6b0e07c11"  # a Body session on another box
STATUS = {"g-1-1": "pending", "g-1-2": "completed", "g-1-3": "blocked", "g-1-4": "in-progress"}


def _row(bodies=None, in_flight=None):
    return {"in_flight_bodies": bodies or {}, "in_flight": in_flight}


def _decide(sessions, row):
    return tcp.decide(sessions, row, STATUS.get)


def _age(path, hours):
    """Make path's mtime `hours` ago, on the clock probe() reads."""
    then = time.time() - hours * 3600
    os.utime(path, (then, then))


# --------------------------------------------------------------------------- #
# decide(): the rule
# --------------------------------------------------------------------------- #
def test_no_row_for_this_box_is_none():
    verdict, evidence = _decide({BODY: True}, _row())
    assert verdict == "none", evidence
    assert "no in-flight row for this box's 1 Body session(s)" in evidence


def test_a_row_naming_an_open_goal_is_held_and_its_closed_twin_is_none():
    verdict, evidence = _decide({BODY: True}, _row({BODY: {"goal_id": "g-1-1"}}))
    assert (verdict, evidence) == ("held", "row c2503cad names g-1-1 (pending)")
    # CONTROL: the same row once its goal is closed.
    verdict, evidence = _decide({BODY: True}, _row({BODY: {"goal_id": "g-1-2"}}))
    assert (verdict, evidence) == ("none", "row c2503cad names g-1-2 (completed)")


@pytest.mark.parametrize("gid", ["g-1-3", "g-1-4"], ids=["blocked", "in-progress"])
def test_blocked_and_in_progress_goals_still_hold_the_claim(gid):
    assert _decide({BODY: True}, _row({BODY: {"goal_id": gid}}))[0] == "held"


def test_another_boxs_row_is_not_this_boxs_claim():
    row = _row({OTHER_BOX: {"goal_id": "g-1-1"}})
    assert _decide({BODY: True}, row)[0] == "none"
    # CONTROL: the same row keyed by this box's session holds the claim.
    assert _decide({BODY: True, OTHER_BOX: True}, row)[0] == "held"


def test_the_sid_less_reducer_row_is_ignored_on_an_all_body_box():
    """`in_flight` carries no sid, so it cannot be this box's claim when every local
    session is a worker Body (a reducer session would make the answer `unknown`)."""
    row = _row(in_flight={"goal_id": "g-1-1", "claimed_at": "2026-09-30T10:08:41"})
    assert _decide({BODY: True}, row)[0] == "none"


def test_a_non_body_session_makes_the_box_unknown():
    verdict, evidence = _decide({BODY: True, "8913fdff-ddee-4554-b289-c3714f63c0de": False}, _row())
    assert verdict == "unknown" and "a non-Body session ran here (8913fdff)" in evidence
    # CONTROL: the same box with only its Body session.
    assert _decide({BODY: True}, _row())[0] == "none"


@pytest.mark.parametrize("row, why", [
    (None, "missing or unreadable"),
    ({"in_flight_bodies": ["not", "a", "mapping"]}, "not a mapping"),
    (_row({BODY: {"phase": "4"}}), "names no goal id"),
    (_row({BODY: "g-1-1"}), "names no goal id"),
    (_row({BODY: {"goal_id": "g-9-9"}}), "does not resolve"),
], ids=["unreadable-row", "bodies-not-mapping", "no-goal-id", "entry-not-mapping", "unresolved-goal"])
def test_every_doubt_is_unknown(row, why):
    verdict, evidence = _decide({BODY: True}, row)
    assert verdict == "unknown" and why in evidence, evidence


def test_no_local_session_is_none():
    assert _decide({}, _row({OTHER_BOX: {"goal_id": "g-1-1"}}))[0] == "none"


# --------------------------------------------------------------------------- #
# the claim of record: a claim whose row was never written ()
# --------------------------------------------------------------------------- #
STORE_HELD = f"{BODY[:8]} holds g-1-4 (in-progress) in this box's world queue"


def _queue(path, *aspirations):
    """Write a world queue file holding one aspiration per list of goals."""
    path.write_text("".join(json.dumps({"id": f"asp-{i}", "goals": goals}) + "\n"
                            for i, goals in enumerate(aspirations, 1)), encoding="utf-8")
    return path


def test_a_store_claim_with_no_row_is_held_and_its_released_twin_is_none():
    """zc-05's shape on 2026-10-05: its claim committed with no Body row, and the tick
    integrated under it 19 times."""
    claims = [(BODY, "g-1-4", "in-progress")]
    assert tcp.decide({BODY: True}, _row(), STATUS.get, claims) == ("held", STORE_HELD)
    # CONTROL: the same box once the claim is released (release erases claimed_by_sid).
    assert tcp.decide({BODY: True}, _row(), STATUS.get, []) == (
        "none", "no in-flight row for this box's 1 Body session(s)")


def test_a_store_claim_holds_beside_a_row_naming_a_closed_goal():
    row = _row({BODY: {"goal_id": "g-1-2"}})
    assert tcp.decide({BODY: True}, row, STATUS.get, [(BODY, "g-1-4", "in-progress")]) == (
        "held", STORE_HELD)
    # CONTROL: without the store claim, the closed row alone answers none.
    assert tcp.decide({BODY: True}, row, STATUS.get, [])[0] == "none"


def test_another_boxs_store_claim_is_not_this_boxs():
    claims = [(OTHER_BOX, "g-1-4", "in-progress")]
    assert tcp.decide({BODY: True}, _row(), STATUS.get, claims)[0] == "none"
    # CONTROL: the same claim held by one of this box's sessions.
    assert tcp.decide({BODY: True, OTHER_BOX: True}, _row(), STATUS.get, claims)[0] == "held"


def test_a_queue_copy_that_cannot_be_read_is_unknown():
    assert tcp.decide({BODY: True}, _row(), STATUS.get, None) == (
        "unknown", "this box's copy of the world queue cannot be read")
    # CONTROL: the same box with a copy that reads clean.
    assert tcp.decide({BODY: True}, _row(), STATUS.get, [])[0] == "none"


def test_open_store_claims_reads_only_open_goals_these_sessions_hold(tmp_path):
    queue = _queue(tmp_path / tcp.QUEUE_FILE,
                   [{"id": "g-1-1", "status": "pending", "claimed_by_sid": BODY},
                    {"id": "g-1-2", "status": "completed", "claimed_by_sid": BODY},
                    {"id": "g-1-3", "status": "in-progress", "claimed_by_sid": OTHER_BOX}],
                   [{"id": "g-2-1", "status": "blocked", "claimed_by_sid": BODY},
                    {"id": "g-2-2", "status": "pending"}])
    assert tcp.open_store_claims(queue, {BODY}) == [
        (BODY, "g-1-1", "pending"), (BODY, "g-2-1", "blocked")]
    assert tcp.open_store_claims(queue, {OTHER_BOX}) == [(OTHER_BOX, "g-1-3", "in-progress")]
    assert tcp.open_store_claims(queue, set()) == []


@pytest.mark.parametrize("content", [
    None,                                       # no copy at all
    b'{"goals": [{"claimed_by_sid": "x"\n',     # a claim line cut short
    b'["claimed_by_sid"]\n',                    # a claim line that is not an aspiration
    b'{"claimed_by_sid": "\xff"}\n',            # not UTF-8
], ids=["missing", "truncated", "not-a-mapping", "not-utf8"])
def test_a_scan_that_cannot_finish_answers_none_not_an_empty_list(tmp_path, content):
    queue = tmp_path / tcp.QUEUE_FILE
    if content is not None:
        queue.write_bytes(content)
    assert tcp.open_store_claims(queue, {BODY}) is None
    # CONTROL: the same path holding a readable copy with no claim in it.
    _queue(queue, [{"id": "g-1-1", "status": "pending"}])
    assert tcp.open_store_claims(queue, {BODY}) == []


# --------------------------------------------------------------------------- #
# resolving the agent and this box's sessions
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("confs, env, want", [
    (["alpha"], "", "alpha"),
    (["alpha", "bravo"], "bravo", "bravo"),
    (["alpha", "bravo"], "", None),
    ([], "", None),
    (["alpha"], "bravo", None),
], ids=["only-conf", "env-named", "two-confs", "no-conf", "env-without-conf"])
def test_resident_agent(confs, env, want):
    assert tcp.resident_agent(confs, env)[0] == want


def test_local_sessions_reads_each_role_and_skips_non_sid_dirs(tmp_path):
    root = tmp_path / "sessions"
    (root / BODY).mkdir(parents=True)
    (root / BODY / tcp.BODY_MARKER).write_text("{}\n", encoding="utf-8")
    (root / OTHER_BOX).mkdir()
    (root / OTHER_BOX / tcp.BODY_MARKER).write_text("role: reducer\n", encoding="utf-8")
    (root / "8913fdff-ddee-4554-b289-c3714f63c0de").mkdir()
    (root / "index").mkdir()
    (root / "notes.txt").write_text("x", encoding="utf-8")
    assert tcp.local_sessions(root) == {BODY: "worker", OTHER_BOX: "reducer",
                                        "8913fdff-ddee-4554-b289-c3714f63c0de": ""}
    assert tcp.local_sessions(tmp_path / "absent") == {}


# --------------------------------------------------------------------------- #
# no_loop(): a box no worker Body has run on ()
# --------------------------------------------------------------------------- #
SEAT = "8913fdff-ddee-4554-b289-c3714f63c0de"   # an interactive seat: no manifest
IDLE_BOX = {"alpha": "IDLE", "bravo": None, "foxtrot": "IDLE"}


def test_a_box_of_seats_with_nothing_running_is_noloop():
    """The measured cc-14 shape: several agents configured, interactive seats only."""
    assert tcp.no_loop({SEAT: ""}, IDLE_BOX) == (
        "noloop", "no loop runs here: 3 agent(s) configured, none RUNNING; "
                  "1 session(s), none a worker Body")
    # CONTROL: the same box with one agent RUNNING.
    assert tcp.no_loop({SEAT: ""}, dict(IDLE_BOX, bravo="RUNNING")) == (
        "unknown", "bravo is RUNNING here, so its loop owns the merge")


def test_a_worker_role_leaves_the_answer_to_decide():
    """A worker Body box keeps the  rule. Its loop reads IDLE, as all ten zc
    Bodies did on 2026-10-02, so the RUNNING check alone could not have caught it."""
    assert tcp.no_loop({BODY: "worker", SEAT: ""}, {"alpha": "IDLE"}) is None
    # CONTROL: the same sessions once that manifest records another role.
    assert tcp.no_loop({BODY: "observer", SEAT: ""}, {"alpha": "IDLE"})[0] == "noloop"


@pytest.mark.parametrize("role", ["reducer", "observer"])
def test_reducer_and_observer_manifests_are_not_worker_bodies(role):
    assert tcp.no_loop({BODY: role}, {"alpha": "IDLE"})[0] == "noloop"
    # A reducer's loop is caught by its RUNNING state instead.
    assert tcp.no_loop({BODY: role}, {"alpha": "RUNNING"})[0] == "unknown"


def test_an_unreadable_manifest_is_unknown():
    verdict, evidence = tcp.no_loop({BODY: None, SEAT: ""}, {"alpha": "IDLE"})
    assert verdict == "unknown", evidence
    assert evidence == "session c2503cad has a body-manifest.yaml that cannot be read"
    # CONTROL: the same box with that manifest readable.
    assert tcp.no_loop({BODY: "observer", SEAT: ""}, {"alpha": "IDLE"})[0] == "noloop"


def test_a_role_this_probe_does_not_know_is_unknown():
    """A role outside VALID_ROLES may name a newer kind of session that runs a loop."""
    assert tcp.no_loop({BODY: "worker-lite", SEAT: ""}, {"alpha": "IDLE"}) == (
        "unknown", "session c2503cad records role 'worker-lite', which this probe does not know")
    # CONTROL: the same box with a role the writer can record.
    assert tcp.no_loop({BODY: "reducer", SEAT: ""}, {"alpha": "IDLE"})[0] == "noloop"


def test_known_roles_are_the_writers_valid_roles():
    import importlib.util
    spec = importlib.util.spec_from_file_location("body_manifest", CORE_SCRIPTS / "body-manifest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert set(tcp.KNOWN_ROLES) == set(mod.VALID_ROLES)


def test_a_box_with_no_session_and_no_agent_is_noloop():
    assert tcp.no_loop({}, {}) == ("noloop", "no loop runs here: 0 agent(s) configured, "
                                             "none RUNNING; 0 session(s), none a worker Body")


# --------------------------------------------------------------------------- #
# a worker Body that has ended is not a Body that runs here ()
# --------------------------------------------------------------------------- #
ENDED = {BODY: 72.4}   # the worker session was last written 72.4 h ago


def test_a_worker_session_that_has_ended_is_not_a_body_that_runs_here():
    """The measured shape as the pure rule sees it: a worker-role session, every agent IDLE."""
    assert tcp.no_loop({BODY: "worker"}, IDLE_BOX, ENDED) == (
        "noloop", "no loop runs here: 3 agent(s) configured, none RUNNING; "
                  "1 worker Body session(s) ended, the newest silent 72 h")
    # CONTROLS: the same sessions with no word that they ended.
    assert tcp.no_loop({BODY: "worker"}, IDLE_BOX) is None
    assert tcp.no_loop({BODY: "worker"}, IDLE_BOX, {}) is None


def test_one_worker_inside_the_window_leaves_the_box_to_decide():
    roles = {BODY: "worker", OTHER_BOX: "worker"}
    assert tcp.no_loop(roles, IDLE_BOX, {BODY: 72.0}) is None
    # CONTROL: once the other session has ended too, and the evidence names the newest.
    assert tcp.no_loop(roles, IDLE_BOX, {BODY: 72.0, OTHER_BOX: 30.0}) == (
        "noloop", "no loop runs here: 3 agent(s) configured, none RUNNING; "
                  "2 worker Body session(s) ended, the newest silent 30 h")


def test_an_ended_session_excuses_none_of_the_other_doubts():
    assert tcp.no_loop({BODY: "worker"}, dict(IDLE_BOX, bravo="RUNNING"), ENDED) == (
        "unknown", "bravo is RUNNING here, so its loop owns the merge")
    assert tcp.no_loop({BODY: "worker", SEAT: None}, IDLE_BOX, ENDED)[0] == "unknown"
    assert tcp.no_loop({BODY: "worker", SEAT: "worker-lite"}, IDLE_BOX, ENDED)[0] == "unknown"
    # CONTROL: the same box without those doubts.
    assert tcp.no_loop({BODY: "worker", SEAT: "observer"}, IDLE_BOX, ENDED)[0] == "noloop"


def test_session_idle_hours_reads_the_writers_files_not_the_directory(tmp_path):
    """guard-7582: a directory's mtime moves only when a direct child is created or removed."""
    now = time.time()
    assert tcp.session_idle_hours(tmp_path, now) is None      # neither file: no evidence
    manifest = tmp_path / tcp.BODY_MARKER
    manifest.write_text("role: worker\n", encoding="utf-8")
    _age(manifest, 10)
    assert tcp.session_idle_hours(tmp_path, now) == pytest.approx(10, abs=0.01)
    heartbeat = tmp_path / tcp.HEARTBEAT
    heartbeat.write_text("", encoding="utf-8")
    _age(heartbeat, 2)
    assert tcp.session_idle_hours(tmp_path, now) == pytest.approx(2, abs=0.01)   # newest wins
    # An ancient directory mtime does not make a running writer read silent ...
    _age(tmp_path, 100)
    heartbeat.touch()
    assert tcp.session_idle_hours(tmp_path, time.time()) == pytest.approx(0, abs=0.01)
    # ... and a fresh one does not make a silent writer read alive.
    _age(heartbeat, 50)
    _age(manifest, 60)
    os.utime(tmp_path, None)
    assert tcp.session_idle_hours(tmp_path, time.time()) == pytest.approx(50, abs=0.01)


def test_ended_sessions_lists_a_session_only_once_it_has_been_silent_for_live_hours(tmp_path):
    root = tmp_path / "sessions"
    for sid, hours in ((BODY, tcp.LIVE_HOURS + 0.5), (OTHER_BOX, tcp.LIVE_HOURS - 0.5)):
        (root / sid).mkdir(parents=True)
        (root / sid / tcp.HEARTBEAT).write_text("", encoding="utf-8")
        _age(root / sid / tcp.HEARTBEAT, hours)
    (root / SEAT).mkdir()          # no file at all: no evidence either way, so never ended
    (root / "index").mkdir()       # not a session dir
    ended = tcp.ended_sessions(root, time.time())
    assert set(ended) == {BODY}
    assert ended[BODY] == pytest.approx(tcp.LIVE_HOURS + 0.5, abs=0.01)
    assert tcp.ended_sessions(tmp_path / "absent", time.time()) == {}


@pytest.mark.parametrize("text, want", [
    (None, ""),
    ("role: worker\nbody_state: active\n", "worker"),
    ("role: reducer\n", "reducer"),
    ("role: observer\n", "observer"),
    ("body_state: active\n", "worker"),   # no role: the writer's default
    ("role: [unclosed\n", None),          # not YAML
    ("- a list\n", None),                 # not a mapping
], ids=["no-manifest", "worker", "reducer", "observer", "no-role", "bad-yaml", "not-mapping"])
def test_manifest_role(tmp_path, text, want):
    if text is not None:
        (tmp_path / tcp.BODY_MARKER).write_text(text, encoding="utf-8")
    assert tcp.manifest_role(tmp_path) == want


def test_agent_state_reads_as_session_state_get_does(tmp_path):
    assert tcp.agent_state(tmp_path) is None
    (tmp_path / "session").mkdir()
    (tmp_path / "session" / "agent-state").write_text(" RUNNING\r\n", encoding="utf-8")
    assert tcp.agent_state(tmp_path) == "RUNNING"


def test_an_unreadable_agent_state_raises_so_main_answers_unknown(tmp_path):
    (tmp_path / "session" / "agent-state").mkdir(parents=True)   # a dir, not a file
    with pytest.raises(OSError):
        tcp.agent_state(tmp_path)


# --------------------------------------------------------------------------- #
# end to end: the fail-safe answers the tick relies on
# --------------------------------------------------------------------------- #
def _fake_box(monkeypatch, tmp_path, agents):
    """Point probe() at a box under tmp_path with these agents configured."""
    import _paths
    root = tmp_path / "box"
    for agent in agents:
        (root / "agents" / agent).mkdir(parents=True)
        (root / "agents" / agent / "local-paths.conf").write_text("", encoding="utf-8")
    monkeypatch.delenv("MIND_AGENT", raising=False)   # a root cron carries none
    monkeypatch.setattr(_paths, "PROJECT_ROOT", str(root))
    monkeypatch.setattr(_paths, "enumerate_agent_confs",
                        lambda: [root / "agents" / a / "local-paths.conf" for a in agents])
    monkeypatch.setattr(_paths, "agent_dir", lambda name: root / "agents" / name)
    monkeypatch.setattr(_paths, "agent_sessions_root",
                        lambda name: root / "agents" / name / "sessions")
    return root


def test_a_multi_resident_box_of_seats_answers_noloop(monkeypatch, tmp_path):
    root = _fake_box(monkeypatch, tmp_path, ["alpha", "bravo"])
    (root / "agents/alpha/sessions" / SEAT).mkdir(parents=True)
    (root / "agents/alpha/session").mkdir()
    (root / "agents/alpha/session/agent-state").write_text("IDLE\n", encoding="utf-8")
    assert tcp.probe(str(root)) == (
        "noloop", "-", "no loop runs here: 2 agent(s) configured, none RUNNING; "
                       "1 session(s), none a worker Body")
    # CONTROL: one worker Body session hands the same box to the  rule, which
    # cannot answer for two agents.
    body = root / "agents/alpha/sessions" / BODY
    body.mkdir()
    (body / tcp.BODY_MARKER).write_text("role: worker\n", encoding="utf-8")
    assert tcp.probe(str(root)) == ("unknown", "-", "2 agents have a local-paths.conf here")


def test_a_running_agent_on_a_box_of_seats_is_unknown(monkeypatch, tmp_path):
    root = _fake_box(monkeypatch, tmp_path, ["alpha", "bravo"])
    (root / "agents/bravo/session").mkdir(parents=True)
    (root / "agents/bravo/session/agent-state").write_text("RUNNING\n", encoding="utf-8")
    assert tcp.probe(str(root)) == ("unknown", "-", "bravo is RUNNING here, so its loop owns the merge")
    # CONTROL: the same box once that loop has stopped.
    (root / "agents/bravo/session/agent-state").write_text("IDLE\n", encoding="utf-8")
    assert tcp.probe(str(root))[0] == "noloop"


# --------------------------------------------------------------------------- #
# end to end: the box measured on cc-08, cc-09, cc-15 and cc-17 ()
# --------------------------------------------------------------------------- #
ALL_AGENTS = ["alpha", "bravo", "echo", "foxtrot", "zeta"]


def _worker(root, agent, sid, hours):
    """A worker Body session of `agent` on the fake box, last written `hours` ago."""
    d = root / "agents" / agent / "sessions" / sid
    d.mkdir(parents=True)
    (d / tcp.BODY_MARKER).write_text("role: worker\n", encoding="utf-8")
    (d / tcp.HEARTBEAT).write_text("", encoding="utf-8")
    _age(d / tcp.BODY_MARKER, hours + 0.1)
    _age(d / tcp.HEARTBEAT, hours)
    return d


def test_a_multi_agent_box_whose_worker_bodies_have_ended_answers_noloop(monkeypatch, tmp_path):
    """Five confs, three worker-role session dirs last written 72, 100 and 1500 h ago, two
    agent-state files IDLE and the rest absent: the shape that answered `unknown` on every
    tick while the box fell 938 to 1004 commits behind."""
    root = _fake_box(monkeypatch, tmp_path, ALL_AGENTS)
    live = _worker(root, "alpha", BODY, 72.4)
    _worker(root, "alpha", OTHER_BOX, 100)
    _worker(root, "foxtrot", SEAT, 1500)
    for agent in ("alpha", "foxtrot"):
        (root / "agents" / agent / "session").mkdir()
        (root / "agents" / agent / "session" / "agent-state").write_text("IDLE\n", encoding="utf-8")
    assert tcp.probe(str(root)) == (
        "noloop", "-", "no loop runs here: 5 agent(s) configured, none RUNNING; "
                       "3 worker Body session(s) ended, the newest silent 72 h")
    # NEGATIVE CONTROL: one of those Bodies writes its heartbeat. The tick stays log-only.
    (live / tcp.HEARTBEAT).touch()
    assert tcp.probe(str(root)) == ("unknown", "-", "5 agents have a local-paths.conf here")


def test_the_window_between_live_and_ended_is_live_hours(monkeypatch, tmp_path):
    root = _fake_box(monkeypatch, tmp_path, ALL_AGENTS)
    body = _worker(root, "alpha", BODY, tcp.LIVE_HOURS - 0.5)
    assert tcp.probe(str(root)) == ("unknown", "-", "5 agents have a local-paths.conf here")
    for name in (tcp.HEARTBEAT, tcp.BODY_MARKER):
        _age(body / name, tcp.LIVE_HOURS + 0.5)
    assert tcp.probe(str(root))[0] == "noloop"


def test_a_session_with_a_manifest_but_no_heartbeat_ends_by_its_manifest(monkeypatch, tmp_path):
    """A Body that never ticked is judged by when /start wrote its manifest."""
    root = _fake_box(monkeypatch, tmp_path, ALL_AGENTS)
    body = _worker(root, "alpha", BODY, 72)
    (body / tcp.HEARTBEAT).unlink()
    assert tcp.probe(str(root))[0] == "noloop"
    # CONTROL: the same session started a minute ago has had no time to tick yet.
    _age(body / tcp.BODY_MARKER, 1 / 60)
    assert tcp.probe(str(root)) == ("unknown", "-", "5 agents have a local-paths.conf here")


def test_a_running_agent_keeps_a_box_of_ended_bodies_unknown(monkeypatch, tmp_path):
    root = _fake_box(monkeypatch, tmp_path, ALL_AGENTS)
    _worker(root, "alpha", BODY, 72)
    assert tcp.probe(str(root))[0] == "noloop"        # CONTROL: nothing RUNNING
    (root / "agents/bravo/session").mkdir(parents=True)
    (root / "agents/bravo/session/agent-state").write_text("RUNNING\n", encoding="utf-8")
    assert tcp.probe(str(root)) == (
        "unknown", "-", "bravo is RUNNING here, so its loop owns the merge")


def test_the_ended_rule_is_for_a_box_that_names_no_agent(monkeypatch, tmp_path):
    """A named agent hands the box back to the claim rule, as before."""
    import _paths
    root = _fake_box(monkeypatch, tmp_path, ALL_AGENTS)
    _worker(root, "alpha", BODY, 72)
    assert tcp.probe(str(root))[0] == "noloop"        # CONTROL: no agent named
    monkeypatch.setenv("MIND_AGENT", "alpha")
    monkeypatch.setattr(_paths, "WORLD_DIR", "")
    assert tcp.probe(str(root)) == ("unknown", "alpha", "WORLD_PATH does not resolve")


def test_a_single_agent_box_keeps_the_claim_rule_whatever_its_heartbeat_age(monkeypatch, tmp_path):
    """With one resident agent the claim rule can still name whose integrate to run, and an
    ended Body there may have left commits that integrate must merge (g-375-96)."""
    import yaml
    import _paths
    import _team_state
    root = _fake_box(monkeypatch, tmp_path, ["alpha"])
    world = tmp_path / "world"
    row_file = _team_state.row_path(world, "alpha")
    row_file.parent.mkdir(parents=True)
    row_file.write_text(yaml.safe_dump({"in_flight_bodies": {BODY: {"goal_id": "g-1-1"}}}),
                        encoding="utf-8")
    _queue(world / tcp.QUEUE_FILE, [{"id": "g-1-1", "status": "pending"}])
    monkeypatch.setattr(_paths, "WORLD_DIR", str(world))
    monkeypatch.setattr(tcp, "_goal_status_resolver", lambda w: STATUS.get)
    _worker(root, "alpha", BODY, 72)
    assert tcp.probe(str(root)) == ("held", "alpha", f"row {BODY[:8]} names g-1-1 (pending)")


@pytest.mark.parametrize("role", ["reducer", "observer"])
def test_a_reducer_or_observer_seat_beside_a_worker_body_is_unknown(monkeypatch, tmp_path, role):
    """: /start writes body-manifest.yaml for a reducer and an observer seat too.
    The reducer's claim sits in the sid-less in_flight row, so counting its session as a
    worker Body would answer none while that claim is held."""
    import yaml
    import _paths
    import _team_state
    root = _fake_box(monkeypatch, tmp_path, ["alpha"])
    world = tmp_path / "world"
    row_file = _team_state.row_path(world, "alpha")
    row_file.parent.mkdir(parents=True)
    row_file.write_text(yaml.safe_dump({"in_flight": {"goal_id": "g-1-1"}, "in_flight_bodies": {}}),
                        encoding="utf-8")
    _queue(world / tcp.QUEUE_FILE, [{"id": "g-1-1", "status": "pending"}])
    monkeypatch.setattr(_paths, "WORLD_DIR", str(world))
    monkeypatch.setattr(tcp, "_goal_status_resolver", lambda w: STATUS.get)
    for sid, r in ((BODY, "worker"), (SEAT, role)):
        (root / "agents/alpha/sessions" / sid).mkdir(parents=True)
        (root / "agents/alpha/sessions" / sid / tcp.BODY_MARKER).write_text(
            f"role: {r}\n", encoding="utf-8")
    assert tcp.probe(str(root)) == ("unknown", "alpha", f"a non-Body session ran here ({SEAT[:8]})")
    # CONTROLS: the worker-only box answers none, and held once its own row names an open goal.
    (root / "agents/alpha/sessions" / SEAT / tcp.BODY_MARKER).unlink()
    (root / "agents/alpha/sessions" / SEAT).rmdir()
    assert tcp.probe(str(root)) == (
        "none", "alpha", "no in-flight row for this box's 1 Body session(s)")
    row_file.write_text(yaml.safe_dump({"in_flight_bodies": {BODY: {"goal_id": "g-1-1"}}}),
                        encoding="utf-8")
    assert tcp.probe(str(root)) == ("held", "alpha", f"row {BODY[:8]} names g-1-1 (pending)")


def test_a_store_claim_with_no_row_holds_the_box_end_to_end(monkeypatch, tmp_path):
    """: the row file names nothing for this box, and the box's copy of the world
    queue carries its worker Body's open claim."""
    import yaml
    import _paths
    import _team_state
    root = _fake_box(monkeypatch, tmp_path, ["alpha"])
    world = tmp_path / "world"
    row_file = _team_state.row_path(world, "alpha")
    row_file.parent.mkdir(parents=True)
    row_file.write_text(yaml.safe_dump({"in_flight_bodies": {}}), encoding="utf-8")
    monkeypatch.setattr(_paths, "WORLD_DIR", str(world))
    monkeypatch.setattr(tcp, "_goal_status_resolver", lambda w: STATUS.get)
    (root / "agents/alpha/sessions" / BODY).mkdir(parents=True)
    (root / "agents/alpha/sessions" / BODY / tcp.BODY_MARKER).write_text(
        "role: worker\n", encoding="utf-8")
    queue = _queue(world / tcp.QUEUE_FILE,
                   [{"id": "g-1-4", "status": "in-progress", "claimed_by_sid": BODY}])
    assert tcp.probe(str(root)) == ("held", "alpha", STORE_HELD)
    # CONTROL: once the claim is released, the same box hands off to the integrate.
    _queue(queue, [{"id": "g-1-4", "status": "in-progress"}])
    assert tcp.probe(str(root)) == (
        "none", "alpha", "no in-flight row for this box's 1 Body session(s)")
    # A copy that is gone answers unknown, never none.
    queue.unlink()
    assert tcp.probe(str(root)) == (
        "unknown", "alpha", "this box's copy of the world queue cannot be read")


def test_a_foreign_repo_is_unknown_before_any_store_is_read(tmp_path):
    """Why the hermetic iteration-push tests stay log-only without a stub."""
    r = subprocess.run([sys.executable, str(CORE_SCRIPTS / "tick_claim_probe.py"),
                        "--repo", str(tmp_path)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith(f"unknown - --repo {tmp_path} is not this script's tree"), r.stdout


def test_a_crashing_probe_answers_unknown(monkeypatch, capsys):
    def boom(repo):
        raise RuntimeError("store unreachable")
    monkeypatch.setattr(tcp, "probe", boom)
    assert tcp.main(["--repo", "/nowhere"]) == 0
    assert capsys.readouterr().out == "unknown - probe failed: RuntimeError: store unreachable\n"
