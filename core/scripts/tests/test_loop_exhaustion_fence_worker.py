"""The loop-exhaustion fence runs for worker Bodies and PARKS them ().

WHAT THIS PINS
--------------
A worker Body that keeps ending turns while holding no claim is fenced by the
same ladder as the reducer (pause at 4, decisive rung at 10, a 900s wall-clock
floor), with three differences that are the goal's design constraints:

(a) The decisive rung is a PARK (body-manifest.py park), never the agent-wide
    stop-requested / stop-target-mode, which would stop the REDUCER on another
    machine. Pinned three ways: decide_worker() cannot return "stop" for any
    input; the wrapper's WORKER BLOCK contains no signal write; and the
    end-to-end park leaves both agent-wide files absent.
(b) The predicate is "no claim held by this SID". A held claim HOLDS whatever
    the count, and an unreadable claim store HOLDS.
(c) The park ALERTS: a notifying stop-reason path (worker-body-stall-parked,
    never the silent worker-body-parked) plus a coordination-board post.

Measured incident behind it: SID 1f257cc9 (cc-09) logged 205 worker-net BLOCKs
over ~2 days holding no claim. Replayed through evaluate_worker, the pause rung
lands at BLOCK #5 and the park at #10; four live Bodies with 8-20 lifetime
BLOCKs each read streak 0 (their anchors are recent goal activity).

HARNESS REUSE from test_stop_hook_in_flight_integration (same tmp PROJECT_ROOT,
daemon fixture, production shape: no running-session-id, MIND_* scrubbed).
The fixture goal is claimed by ``claimed_by_sid``; passing another sid is what
makes this Body hold NO claim. ``mutate`` runs after the root is built, so it
is where a test seeds prior worker-net BLOCK lines into the hook's log.
"""
from __future__ import annotations

import datetime
import json
import pathlib
import sys

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import loop_exhaustion_fence as lef  # noqa: E402
from _daemon_fixture import DaemonFixture  # noqa: E402
from test_stop_hook_in_flight_integration import (  # noqa: E402
    AGENT,
    BODY_SID,
    _blocked,
    _drive,
    _hook_log,
    _run_hook,
)

OTHER_SID = "some-other-body-sid"
T0 = datetime.datetime(2026, 9, 25, 9, 0, 0)

# The per-Body call site under test. The mutation proof removes exactly this
# token, so it and the positive tests cannot drift apart.
FENCE_CALL = 'bash "$CORE_ROOT/scripts/loop-exhaustion-fence.sh"'


def _line(when, sid=BODY_SID, gate="worker-net", verdict="BLOCK"):
    """A log line in the shape stop-hook.sh's worker-net branch echoes."""
    return f"{when.strftime('%Y-%m-%dT%H:%M:%S')} {verdict} gate={gate} sid={sid} agent={AGENT}"


# ------------------------------------------------------------ decide_worker

def test_decide_worker_ladder():
    d = lef.decide_worker
    assert d(False, 3, 99999)["verdict"] == "hold"
    assert d(False, 4, 899)["verdict"] == "hold"          # the wall-clock floor
    assert d(False, 4, 900)["verdict"] == "pause"
    assert d(False, 9, 900)["verdict"] == "pause"
    park = d(False, 10, 900)
    assert park["verdict"] == "park" and park["rc"] == 3 and park["role"] == "worker"
    assert "cause NOT established" in park["reason"]
    assert d(False, 4, 900)["rc"] == 1


def test_a_held_claim_is_never_fenced_whatever_the_count():
    out = lef.decide_worker(True, 10_000, 10**9)
    assert out["verdict"] == "hold" and out["rc"] == 0
    assert "live claim" in out["reason"]


def test_unreadable_inputs_hold():
    d = lef.decide_worker
    assert "unreadable" in d(None, 50, 99999)["reason"]      # claim store
    assert d(None, 50, 99999)["verdict"] == "hold"
    assert d(False, None, 99999)["verdict"] == "hold"        # log
    assert d(False, 50, None)["verdict"] == "hold"
    assert d(False, "x", 99999)["verdict"] == "hold"         # unparseable
    assert d(False, 50, 99999, pause_threshold=5, stop_threshold=5)["verdict"] == "hold"


def test_decide_worker_can_never_return_the_reducer_stop():
    """Constraint (a) at the decision layer: no input reaches VERDICT_STOP."""
    seen = set()
    for held in (None, False, True):
        for streak in (None, 0, 3, 4, 9, 10, 11, 500):
            for stalled in (None, 0.0, 899.0, 900.0, 1e7):
                seen.add(lef.decide_worker(held, streak, stalled)["verdict"])
    assert seen == {"hold", "pause", "park"}, seen


# ------------------------------------------------------------ the store readers

def test_claim_held():
    ch = lef.claim_held
    assert ch([], BODY_SID) is False
    row = {"claimed_by_sid": BODY_SID}
    assert ch([dict(row, status="completed")], BODY_SID) is False
    assert ch([dict(row, status="in-progress")], BODY_SID) is True
    assert ch([dict(row, status="some-new-status")], BODY_SID) is True   # unknown HOLDS
    assert ch([dict(row, status="in-progress", claimed_by_sid=OTHER_SID)], BODY_SID) is None
    assert ch(None, BODY_SID) is None
    assert ch(["not-a-row"], BODY_SID) is None
    assert ch([], "") is None


def test_last_activity_takes_the_latest_field_and_refuses_a_foreign_row():
    rows = [
        {"executed_by_sid": BODY_SID, "claimed_at": "2026-09-25T08:00:00",
         "last_modified": "2026-09-25T08:30:00", "started": "2026-09-26"},
        {"executed_by_sid": BODY_SID, "completed_at": "2026-09-25T08:10:00+00:00",
         "last_modified": "not-a-time"},
    ]
    assert lef.last_activity(rows, BODY_SID) == (datetime.datetime(2026, 9, 25, 8, 30), True)
    assert lef.last_activity([], BODY_SID) == (None, True)
    assert lef.last_activity([{"executed_by_sid": OTHER_SID}], BODY_SID) == (None, False)
    assert lef.last_activity(None, BODY_SID) == (None, False)
    # tz-aware values are compared as naive UTC wall time
    aware = [{"executed_by_sid": BODY_SID, "last_modified": "2026-09-25T10:00:00+02:00"}]
    assert lef.last_activity(aware, BODY_SID)[0] == datetime.datetime(2026, 9, 25, 8, 0)


def test_compute_worker_streak_counts_only_this_sids_worker_net_blocks(tmp_path):
    log = tmp_path / "stop-hook.log"
    lines = [
        _line(T0 - datetime.timedelta(minutes=5)),             # before the anchor
        _line(T0 + datetime.timedelta(minutes=1)),             # counted
        _line(T0 + datetime.timedelta(minutes=2)),             # counted
        _line(T0 + datetime.timedelta(minutes=3), sid=BODY_SID + "-x"),  # prefix-sharing sid
        _line(T0 + datetime.timedelta(minutes=4), sid=OTHER_SID),
        _line(T0 + datetime.timedelta(minutes=5), gate="2.6"),              # reducer BLOCK
        _line(T0 + datetime.timedelta(minutes=6), gate="worker-net-body-parked",
              verdict="ALLOW"),
        "garbage BLOCK gate=worker-net sid=%s agent=%s" % (BODY_SID, AGENT),
    ]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    now = T0 + datetime.timedelta(hours=1)
    assert lef.compute_worker_streak(log, BODY_SID, T0, now=now) == (2, 3600.0)
    # No anchor: every counted BLOCK, stall measured from the first one.
    assert lef.compute_worker_streak(log, BODY_SID, None, now=now) == (3, 3900.0)
    assert lef.compute_worker_streak(log, "nobody", None, now=now) == (0, 0.0)
    assert lef.compute_worker_streak(tmp_path / "absent.log", BODY_SID, None) == (None, None)
    assert lef.compute_worker_streak(log, "", None) == (None, None)


# ------------------------------------------------------------ evaluate_worker

def _fake_query(claimed, executed):
    calls = []

    def query(field, value):
        calls.append(field)
        return claimed if field == "claimed_by_sid" else executed
    return query, calls


def test_evaluate_worker_parks_a_claimless_stall(tmp_path):
    log = tmp_path / "stop-hook.log"
    log.write_text("\n".join(_line(T0 + datetime.timedelta(minutes=i)) for i in range(10))
                   + "\n", encoding="utf-8")
    query, calls = _fake_query([], [])
    out = lef.evaluate_worker(BODY_SID, str(log), query=query,
                              now=T0 + datetime.timedelta(hours=1))
    assert out["verdict"] == "park" and out["streak"] == 10, out
    assert calls == ["claimed_by_sid", "executed_by_sid"]


def test_evaluate_worker_holding_a_claim_skips_the_second_query(tmp_path):
    query, calls = _fake_query([{"claimed_by_sid": BODY_SID, "status": "in-progress"}], [])
    out = lef.evaluate_worker(BODY_SID, str(tmp_path / "unread.log"), query=query)
    assert out["verdict"] == "hold" and out["claim_held"] is True
    assert calls == ["claimed_by_sid"]


def test_evaluate_worker_holds_when_either_store_is_unreadable(tmp_path):
    query, calls = _fake_query(None, [])
    assert lef.evaluate_worker(BODY_SID, "x", query=query)["claim_held"] is None
    assert calls == ["claimed_by_sid"]
    query, _ = _fake_query([], None)
    assert lef.evaluate_worker(BODY_SID, "x", query=query)["verdict"] == "hold"


def test_recent_goal_activity_resets_the_streak(tmp_path):
    log = tmp_path / "stop-hook.log"
    log.write_text("\n".join(_line(T0 + datetime.timedelta(minutes=i)) for i in range(20))
                   + "\n", encoding="utf-8")
    activity = [{"executed_by_sid": BODY_SID, "last_modified": "2026-09-25T09:30:00"}]
    query, _ = _fake_query([], activity)
    out = lef.evaluate_worker(BODY_SID, str(log), query=query,
                              now=T0 + datetime.timedelta(hours=1))
    assert out["verdict"] == "hold" and out["streak"] == 0, out


# ------------------------------------------------------------ structure

def test_the_per_body_call_is_worker_mode_logged_and_before_the_payload():
    src = (SCRIPTS / "stop-hook.sh").read_text(encoding="utf-8")
    per_body = src[:src.index("--- Gate 0-pre:")]
    assert per_body.count(FENCE_CALL) == 1
    call = per_body.index(FENCE_CALL)
    stmt = per_body[per_body.rindex("_WN_FENCE=", 0, call):per_body.index("\n", call)]
    assert "FENCE_ROLE=worker" in stmt and 'HOOK_SID="$HOOK_SID"' in stmt, stmt
    assert '2>>"$LOG"' in stmt and "/dev/null" not in stmt, stmt
    assert per_body.index("BLOCK gate=worker-net sid=") < call
    assert call < per_body.index('"${_WN_FENCE:+ $_WN_FENCE}"') < per_body.index(
        '"${_WN_CTX:+ $_WN_CTX}"')


def test_the_wrappers_worker_block_writes_no_stop_signal_and_parks_first():
    src = (SCRIPTS / "loop-exhaustion-fence.sh").read_text(encoding="utf-8")
    start = src.index('if [ "${FENCE_ROLE:-}" = "worker" ]; then')
    block = src[start:src.index("\nfi\n", start)]
    for forbidden in ("session-signal-set", "stop-target-mode", "stop-requested"):
        assert forbidden not in block, forbidden
    park = block.index('body-manifest.py" park')
    assert park < block.index("board-post.sh") < block.index("stop-reason-record.py")
    assert "--path worker-body-stall-parked" in block
    # The worker block exits before the reducer ladder is ever reached.
    assert start < src.index("BUDGET_ZONE=")


# ------------------------------------------------------------ end to end

def _executed_by(sid):
    """PRODUCTION SHAPE: a claimed goal carries executed_by_sid, and closed goals
    keep it. The daemon refuses a filter on a key no record carries
    (`unknown_goal_field`), so a fixture without it measures only the fail-safe
    hold -- which is what the first draft of these tests did."""
    def edit(goals):
        goals[0]["executed_by_sid"] = sid
    return edit


def _seed(tmp_path, n, age=datetime.timedelta(hours=1), edit=None, goals=None):
    """A `mutate` for _drive: n prior worker-net BLOCKs for this Body, and an
    optional edit of the fixture world's goal records."""
    def mutate(src: str) -> str:
        first = datetime.datetime.now() - age
        log = tmp_path / "hookroot" / "core" / "logs" / "stop-hook.log"
        log.write_text("".join(_line(first + datetime.timedelta(seconds=i)) + "\n"
                               for i in range(n)), encoding="utf-8")
        if goals is not None:
            store = tmp_path / "world" / "aspirations.jsonl"
            asp = json.loads(store.read_text(encoding="utf-8"))
            goals(asp["goals"])
            store.write_text(json.dumps(asp) + "\n", encoding="utf-8")
        return edit(src) if edit else src
    return mutate


def _worker_turn_end(tmp_path, n, claimed_by_sid=OTHER_SID, edit=None,
                     goals=_executed_by(OTHER_SID)):
    proc, _shard, root = _drive(tmp_path, claimed_by_sid=claimed_by_sid,
                                closing=False, runner_file=False, scrub_env=True,
                                mutate=_seed(tmp_path, n, edit=edit, goals=goals))
    assert proc.returncode == 0, proc.stderr[-2000:]
    return proc, root


def _reason(proc) -> str:
    lines = [ln for ln in (proc.stdout or "").splitlines() if '"decision"' in ln]
    assert len(lines) == 1, proc.stdout
    return json.loads(lines[0])["reason"]


def _body_state(root) -> str:
    text = (root / "agents" / AGENT / "sessions" / BODY_SID
            / "body-manifest.yaml").read_text(encoding="utf-8")
    return next(ln.split(":", 1)[1].strip().strip("'\"") for ln in text.splitlines()
                if ln.startswith("body_state:"))


def _coordination_posts(tmp_path) -> list:
    """Every coordination post in the fixture world. The board shards channels
    by date (coordination-YYYY-MM-DD.jsonl), so read every shard."""
    posts = []
    for path in sorted((tmp_path / "world" / "board").glob("coordination*.jsonl")):
        posts += [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()
                  if ln.strip()]
    return posts


def _assert_no_agent_wide_stop(root):
    session = root / "agents" / AGENT / "session"
    assert not (session / "stop-requested").exists()
    assert not (session / "stop-target-mode").exists()


def test_tenth_claimless_block_parks_alerts_and_writes_no_stop_signal(tmp_path):
    # 9 seeded + the hook's own line = 10: also pins the hook's emitter format,
    # since a line the fence failed to match would leave the streak at 9 (pause).
    proc, root = _worker_turn_end(tmp_path, 9)
    assert _blocked(proc), _hook_log(root)
    reason = _reason(proc)
    assert "WORKER-STALL PARK" in reason and "#10" in reason, reason
    assert _body_state(root) == "parked"
    stop_reason = (root / "agents" / AGENT / "session" / "last-stop-reason"
                   ).read_text(encoding="utf-8")
    assert "path=worker-body-stall-parked\n" in stop_reason, stop_reason
    posts = _coordination_posts(tmp_path)
    assert any("worker-stall" in json.dumps(p) and BODY_SID in json.dumps(p)
               for p in posts), posts
    _assert_no_agent_wide_stop(root)

    # The park takes the Body out of the worker-net branch: the next turn-end is
    # ALLOWed by the parked valve instead of counted.
    with DaemonFixture(tmp_path / "world") as df:
        again = _run_hook(root, df.runtime_dir, scrub_env=True)
    assert not _blocked(again), again.stdout
    assert "gate=worker-net-body-parked" in _hook_log(root)


def test_fourth_claimless_block_pauses_and_writes_nothing(tmp_path):
    proc, root = _worker_turn_end(tmp_path, 3)
    assert _blocked(proc)
    reason = _reason(proc)
    assert "WORKER-STALL PAUSE" in reason and "PARK:" not in reason, reason
    assert reason.index("WORKER-STALL PAUSE") < reason.index("CTX: ")
    assert _body_state(root) == "active"
    assert not (root / "agents" / AGENT / "session" / "last-stop-reason").exists()
    assert _coordination_posts(tmp_path) == []
    _assert_no_agent_wide_stop(root)


@pytest.mark.parametrize("claimant,parked", [(BODY_SID, False), (OTHER_SID, True)])
def test_a_body_holding_its_claim_is_never_fenced(tmp_path, claimant, parked):
    """The record carries no activity timestamp, so the anchor is None and all 51
    BLOCKs count. The pair differs ONLY in who holds the claim, so the park in
    the second case proves the hold in the first is the claim gate's doing."""
    proc, root = _worker_turn_end(tmp_path, 50, claimed_by_sid=claimant,
                                  goals=_executed_by(BODY_SID))
    assert _blocked(proc)
    assert ("WORKER-STALL PARK" in _reason(proc)) is parked
    assert _body_state(root) == ("parked" if parked else "active")


def test_recent_goal_activity_by_this_body_resets_the_streak(tmp_path):
    """50 old BLOCKs, but this Body closed a goal 5 minutes ago: the anchor read
    from the live store leaves a streak of 1 (the hook's own line), so it holds."""
    recent = (datetime.datetime.now() - datetime.timedelta(minutes=5)
              ).strftime("%Y-%m-%dT%H:%M:%S")

    def closed_recently(goals):
        goals[0]["executed_by_sid"] = OTHER_SID
        goals.append({"id": "g-306-163-fixture", "title": "closed by this Body",
                      "status": "completed", "priority": "MEDIUM",
                      "participants": ["agent"], "executed_by_sid": BODY_SID,
                      "last_modified": recent, "completed_at": recent})

    proc, root = _worker_turn_end(tmp_path, 50, goals=closed_recently)
    assert _blocked(proc)
    assert "WORKER-STALL" not in _reason(proc)
    assert _body_state(root) == "active"


def test_a_store_that_cannot_answer_holds(tmp_path):
    """No record carries executed_by_sid, so the daemon refuses the anchor query
    (unknown_goal_field): the fence holds rather than guess, whatever the count."""
    proc, root = _worker_turn_end(tmp_path, 50, goals=None)
    assert _blocked(proc)
    assert "WORKER-STALL" not in _reason(proc)
    assert _body_state(root) == "active"


def test_mutation_removing_the_fence_call_leaves_the_stall_unparked(tmp_path):
    """Positive control for the park test: same fixture, call site removed."""
    def drop(src: str) -> str:
        assert src.count(FENCE_CALL) == 3, "call-site count moved; re-derive the mutation"
        per_body, rest = src.split("--- Gate 0-pre:", 1)
        return per_body.replace(FENCE_CALL, "true") + "--- Gate 0-pre:" + rest

    proc, root = _worker_turn_end(tmp_path, 9, edit=drop)
    assert _blocked(proc)
    assert "WORKER-STALL" not in _reason(proc)
    assert _body_state(root) == "active"


def test_a_missing_wrapper_fails_open(tmp_path):
    def remove(src: str) -> str:
        (tmp_path / "hookroot" / "core" / "scripts" / "loop-exhaustion-fence.sh").unlink()
        return src

    proc, root = _worker_turn_end(tmp_path, 9, edit=remove)
    assert _blocked(proc)
    assert "WORKER-STALL" not in _reason(proc)
    assert _body_state(root) == "active"


def test_hostile_wrapper_output_keeps_the_payload_valid_json(tmp_path):
    hostile = ("#!/usr/bin/env bash\n"
               "printf 'WORKER-STALL PAUSE: a \"q\" b \\\\ c\\td\\001e\\n'\nexit 1\n")

    def swap(src: str) -> str:
        (tmp_path / "hookroot" / "core" / "scripts" / "loop-exhaustion-fence.sh"
         ).write_text(hostile, encoding="utf-8")
        return src

    proc, _root = _worker_turn_end(tmp_path, 0, edit=swap)
    assert _blocked(proc)
    assert 'WORKER-STALL PAUSE: a "q" b \\ cde' in _reason(proc)
