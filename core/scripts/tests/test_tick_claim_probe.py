"""tick_claim_probe.py -- the one question the cron sync tick asks ().

`none` is the only answer that lets iteration-push.sh --ff-only run the loop's
integrate, so every test that expects `held` or `unknown` is paired with the
smallest change to the SAME input that yields `none` (guard-4166): a test that
could never have produced `none` proves nothing about the refusal.
"""
from __future__ import annotations

import subprocess
import sys
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


def test_local_sessions_marks_worker_bodies_and_skips_non_sid_dirs(tmp_path):
    root = tmp_path / "sessions"
    (root / BODY).mkdir(parents=True)
    (root / BODY / tcp.BODY_MARKER).write_text("{}\n", encoding="utf-8")
    (root / "8913fdff-ddee-4554-b289-c3714f63c0de").mkdir()
    (root / "index").mkdir()
    (root / "notes.txt").write_text("x", encoding="utf-8")
    assert tcp.local_sessions(root) == {BODY: True, "8913fdff-ddee-4554-b289-c3714f63c0de": False}
    assert tcp.local_sessions(tmp_path / "absent") == {}


# --------------------------------------------------------------------------- #
# end to end: the fail-safe answers the tick relies on
# --------------------------------------------------------------------------- #
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
