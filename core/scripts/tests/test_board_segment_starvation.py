"""test_board_segment_starvation.py —  OUTCOME 2.

VERIFIED BY STARVATION, NOT BY GREP.

`test_board_paths.py` proves the SEAM works. This file proves the
record-READERS are actually wired to it (the first five from g-358-121;
notification_outreach and session-digest joined under g-358-183, found by a
census the original predicate could not see because both join the channel name
in an f-string or a loop variable), which a grep cannot: every one of them passes
`include_archive=False`, so every one of them is BYTE-IDENTICAL IN BEHAVIOUR
TODAY to its pre-routing self. There is no live segment on any board yet, so a
mis-routed consumer and a correctly-routed one are observationally identical
until the segmented writer lands — which is exactly when it is too late to find
out. Nothing currently running can tell them apart. That is what makes a
starvation fixture the only available oracle.

THE SHAPE. Each case builds a tmp board dir holding a live `<channel>.jsonl`
(1 record) PLUS a `<channel>-<today>.jsonl` segment (2 records), runs the real
consumer against it, and asserts the consumer sees all three.

EVERY CASE CARRIES ITS OWN CONTROL, and the control is the point: the same
consumer, same fixture, with `channel_paths` monkeypatched back to the
pre-routing hardcoded `<channel>.jsonl` join, must see ONLY the live record.
A test that passes against both the routed and the un-routed consumer is
testing nothing (guard-2435 — declaring a test a control does not make it one;
this one is checked by construction, since the two expectations are asserted to
differ before either is used).

TWO SEAM PROPERTIES CONSTRAIN THE ASSERTIONS, both measured under g-358-110:

  * ASSERT AN EXACT SET, NEVER NON-EMPTINESS. `read_paths` accounts at FILE
    granularity, so a consumer that read the live file and silently dropped the
    segment still returns a non-empty list. Non-emptiness is satisfied by the
    starved case, i.e. by the defect.
  * DO NOT USE `coverage_note` AS THE ORACLE. On a sparse fixture it reports
    DISCONTINUOUS for free, so a test resting on it passes without the routing
    working at all.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


# ---------------------------------------------------------------- helpers ---

def _load(stem: str, filename: str):
    """Import a hyphen-named script by path (not a legal module name)."""
    spec = importlib.util.spec_from_file_location(stem, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LIVE_ID, SEG1_ID, SEG2_ID = "msg-live-0001", "msg-seg-0002", "msg-seg-0003"
LIVE_GID, SEG1_GID, SEG2_GID = "g-999-01", "g-999-02", "g-999-03"
GID_FOR = {LIVE_GID: LIVE_ID, SEG1_GID: SEG1_ID, SEG2_GID: SEG2_ID}

ALL_THREE = {LIVE_ID, SEG1_ID, SEG2_ID}
STARVED = {LIVE_ID}
AGENT = "alpha"


def _rec(mid: str, gid: str, channel: str) -> dict:
    """A record shaped like a real board post (verified against a live
    coordination record, 2026-09-17). `type: claim` is in wm-contamination's
    `_INVOLVEMENT_TYPES`; the goal id rides in BOTH tags and text because that
    consumer matches either. `user-outreach` is the tag notification_outreach
    filters on; the other consumers ignore it."""
    return {
        "id": mid,
        "author": AGENT,
        "session_id": "sid-starvation",
        "timestamp": dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "channel": channel,
        "type": "claim",
        "text": f"Claiming {gid} (starvation fixture).",
        "reply_to": None,
        "tags": [gid, AGENT, "insight_trigger", "user-outreach"],
    }


def _build_board(tmp_path: Path, channel: str) -> Path:
    """A world dir whose board holds a live file AND one date segment."""
    board = tmp_path / "world" / "board"
    board.mkdir(parents=True, exist_ok=True)
    today = dt.date.today().isoformat()

    def _w(path: Path, recs):
        path.write_text("".join(json.dumps(r) + "\n" for r in recs),
                        encoding="utf-8")

    _w(board / f"{channel}.jsonl", [_rec(LIVE_ID, LIVE_GID, channel)])
    _w(board / f"{channel}-{today}.jsonl",
       [_rec(SEG1_ID, SEG1_GID, channel), _rec(SEG2_ID, SEG2_GID, channel)])
    return tmp_path / "world"


def _single_file_join(board_dir, channel, include_archive=True):
    """THE PRE-ROUTING BEHAVIOUR, restored verbatim: one hardcoded
    `<channel>.jsonl` join that cannot see a segment. This is the control."""
    p = Path(board_dir) / f"{channel}.jsonl"
    return [p] if p.exists() else []


# ------------------------------------------------------------------ cases ---
# (name, channel, module-loader, runner) — runner returns the set of record ids
# the consumer actually SAW.

def _run_goal_selector(mod, world):
    rows = mod._coord_rows(world / "board" / "coordination.jsonl")
    return {r.get("id") for r in rows}


def _run_wm_contamination(mod, world):
    involved = mod._board_involved_goals(
        world, AGENT, set(GID_FOR), since_days=7)
    return {GID_FOR[g] for g in involved if g in GID_FOR}


def _run_goal_duplication(mod, world):
    return {r.get("id") for r in mod._board_channel_records(world, "coordination")}


def _run_trigger_sweep(mod, world):
    mod.BOARD_DIR = world / "board"
    return {r.get("id") for r in mod._channel_records("coordination")}


def _run_trigger_gate(mod, world):
    mod._world_dir = lambda: world
    recs, err = mod._load_findings(48)
    assert err is None, f"_load_findings errored: {err}"
    return {r.get("id") for r in recs}


def _run_notification_outreach(mod, world):
    since = dt.datetime.now() - dt.timedelta(days=1)
    return {r.get("id") for r in mod._board_outreach_rows(world, since)}


def _run_session_digest(mod, world):
    # A digest item keeps no message id, so map each one back through the goal
    # id its tags carry.
    now = dt.datetime.now() + dt.timedelta(minutes=1)
    board = mod.section_board(world, now, 24, 50)
    return {GID_FOR[t] for item in board["coordination"]
            for t in (item.get("tags") or []) if t in GID_FOR}


CASES = [
    ("goal-selector", "coordination",
     lambda: _load("gs_starve", "goal-selector.py"), _run_goal_selector),
    ("wm-contamination-check", "coordination",
     lambda: _load("wm_starve", "wm-contamination-check.py"), _run_wm_contamination),
    ("gates/goal_duplication", "coordination",
     lambda: _load("gd_starve", "gates/goal_duplication.py"), _run_goal_duplication),
    ("insight-trigger-sweep", "coordination",
     lambda: _load("its_starve", "insight-trigger-sweep.py"), _run_trigger_sweep),
    ("insight-trigger-gate", "findings",
     lambda: _load("itg_starve", "insight-trigger-gate.py"), _run_trigger_gate),
    ("notification_outreach", "coordination",
     lambda: _load("no_starve", "notification_outreach.py"), _run_notification_outreach),
    ("session-digest", "coordination",
     lambda: _load("sd_starve", "session-digest.py"), _run_session_digest),
]
IDS = [c[0] for c in CASES]


def test_the_fixture_discriminates():
    """If routed and starved expectations ever coincide, every case below is
    vacuous. Asserted once, up front, rather than assumed."""
    assert ALL_THREE != STARVED
    assert STARVED < ALL_THREE
    assert len(ALL_THREE) == 3 and len(STARVED) == 1


@pytest.mark.parametrize("name,channel,loader,runner", CASES, ids=IDS)
def test_consumer_sees_the_segment(name, channel, loader, runner, tmp_path):
    """ROUTED: the consumer must see the segment's records, exactly."""
    world = _build_board(tmp_path, channel)
    seen = runner(loader(), world)
    assert seen == ALL_THREE, (
        f"{name} saw {sorted(seen)} but the board holds {sorted(ALL_THREE)} — "
        f"the {channel} segment was not enumerated through channel_paths")


@pytest.mark.parametrize("name,channel,loader,runner", CASES, ids=IDS)
def test_control_without_routing_the_consumer_starves(
        name, channel, loader, runner, tmp_path, monkeypatch):
    """CONTROL: with `channel_paths` returning the pre-routing single-file
    join, the SAME consumer must miss the segment. This is what proves the
    assertion above is measuring the routing and not something incidental."""
    world = _build_board(tmp_path, channel)
    mod = loader()
    monkeypatch.setattr(mod, "channel_paths", _single_file_join)
    seen = runner(mod, world)
    assert seen == STARVED, (
        f"{name} saw {sorted(seen)} with routing disabled; expected only "
        f"{sorted(STARVED)}. The fixture is not discriminating, so the routed "
        f"assertion proves nothing.")
