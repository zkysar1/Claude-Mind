"""One carrier verdict for every door ().

Three doors judge a Body's body-heartbeat carrier: gates/body_hold.py (the goal
selector and the claim endpoint's `stale` branch), the claim endpoint's absent-row
path (`_body_carrier_is_fresh`), and stranded-claim-sweep.py (`_body_carrier_verdict`).
They applied different conjuncts: the absent-row path a 60-minute window and no sid
check, the sweep no body_state check. Sampled every 5 min for 6 h on 2026-09-27/28,
one live Body's carrier went 62.5 min between refreshes during one long goal, and at
that sample the absent-row path read it as gone while the other two kept it.

Each case below is one where two doors used to disagree, plus the plain ones on both
sides. Every door must give every case the same verdict.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from gates import body_hold  # noqa: E402
from mind_api.src.endpoints import aspirations_write  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "stranded_claim_sweep_doors_ut", SCRIPTS / "stranded-claim-sweep.py")
sweep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sweep)

AGENT = "alpha"
SID = "7659f585-0000-4000-8000-000000000001"
GOAL = "g-900-01"


def _ts(minutes_ago: float) -> str:
    return (datetime.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S")


def _carrier(minutes_ago: float, *, sid: str = SID, state: str = "active",
             ts: str | None = None) -> dict:
    return {"sid": sid, "agent": AGENT, "host": "box-1",
            "ts": ts if ts is not None else _ts(minutes_ago), "body_state": state}


# (case, carrier, live). The first column names the door that used to differ.
CASES = [
    ("fresh, its own, active", _carrier(5), True),
    ("62.5 min old: the absent-row door read it as gone", _carrier(62.5), True),
    ("fresh and parked, which is resumable", _carrier(5, state="parked"), True),
    ("fresh but closed-graceful: the sweep kept it", _carrier(5, state="closed-graceful"), False),
    ("fresh but merged: the sweep kept it", _carrier(5, state="merged"), False),
    ("fresh but another session's: the absent-row door kept it",
     _carrier(5, sid="0ther-sid"), False),
    ("past the 100-minute window", _carrier(105), False),
    ("unparseable ts", _carrier(0, ts="yesterday"), False),
]


@pytest.fixture
def doors(tmp_path, monkeypatch):
    """Put one carrier in front of all three doors and return their verdicts."""
    agent_dir = tmp_path / "agents" / AGENT
    (agent_dir / "session").mkdir(parents=True)
    monkeypatch.setattr(sweep, "agent_dir", lambda name: agent_dir)
    ctx = types.SimpleNamespace(paths=types.SimpleNamespace(agent_name=AGENT))

    def judge(carrier: dict) -> dict:
        (agent_dir / "session" / f"body-heartbeat-{SID}.json").write_text(
            json.dumps(carrier), encoding="utf-8")
        monkeypatch.setattr(aspirations_write, "_read_body_carrier",
                            lambda ctx, agent, sid: dict(carrier))
        now = datetime.now()
        row = {"goal_id": GOAL, "claimed_at": _ts(30)}
        verdict, _ = sweep._body_carrier_verdict(
            AGENT, SID, sweep.DEFAULT_CARRIER_FRESH_MINUTES)
        return {
            "body_hold carrier": body_hold.evaluate_carrier(carrier, sid=SID, now=now)["live"],
            "body_hold hold": body_hold.evaluate(
                row, carrier, goal_id=GOAL, sid=SID, now=now)["holds"],
            "claim absent-row": aspirations_write._body_carrier_is_fresh(ctx, AGENT, SID),
            "stranded-claim sweep": verdict == "fresh-correct",
        }

    return judge


@pytest.mark.parametrize("case,carrier,live", CASES, ids=[c[0] for c in CASES])
def test_every_door_gives_one_carrier_one_verdict(doors, case, carrier, live):
    verdicts = doors(carrier)
    assert verdicts == {door: live for door in verdicts}, case


def test_the_sweep_names_a_closed_body_and_the_reaper_keeps_its_row(doors, tmp_path):
    import body_row_reaper as reaper

    doors(_carrier(5, state="closed-graceful"))
    verdict, evidence = sweep._body_carrier_verdict(
        AGENT, SID, sweep.DEFAULT_CARRIER_FRESH_MINUTES)
    assert (verdict, evidence["carrier_body_state"]) == ("closed", "closed-graceful")
    decided = reaper.decide_row(
        sid=SID, row={"goal_id": GOAL, "claimed_at": _ts(30)}, carrier_verdict=verdict,
        carrier_evidence=evidence, holds_live_claim=False, self_sid=None)
    assert decided["verdict"] == reaper.K_CLOSED_BODY
    assert not reaper.is_reaping(decided["verdict"])
