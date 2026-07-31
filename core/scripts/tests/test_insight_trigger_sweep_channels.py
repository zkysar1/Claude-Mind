""": insight-trigger-sweep scans EVERY live board channel.

From inception until 2026-07-29 the sweep bound a single module constant
`FINDINGS = WORLD_DIR/board/findings.jsonl` and its reader opened only that
file. A `requires_action_by:` tag posted to any other channel was therefore
structurally invisible: it could never convert, at any age, and nothing
reported that it was being skipped. Measured cost:
msg-20260728-194530-omni-5115 (omni -> alpha, coordination, action_type:revisit)
sat undelivered.

What these pins hold, and why each one is here rather than implied:

  1. coordination converts        — the reported defect, stated as behavior.
  2. every channel converts       — the fix is discovery, not "findings plus
                                    one more". A second hardcoded name would
                                    pass pin 1 and fail here.
  3. sidecars/archives excluded   — `<ch>-reads.jsonl` rows are not messages;
                                    `<ch>-archive.jsonl` is 11MB of rows older
                                    than the window by construction. A naive
                                    `glob("*.jsonl")` passes 1+2 and fails here.
  4. board pointer names the READ channel — the filed goal's pointer has to
                                    resolve for a human. It was hardcoded to
                                    findings.jsonl and would have kept lying
                                    about every widened hit.
  5. channels_scanned reported    — a silently-narrow scan is the defect
                                    itself, so the scope must be visible in
                                    the output, not inferable from source.
  6. SPECIFICITY control          — an untagged coordination post must still
                                    be dropped. Without this, "everything
                                    converts now" would satisfy pins 1-2, and
                                    the widening would read as correct while
                                    having deleted the routing filter.

Run: py -3 -m pytest core/scripts/tests/test_insight_trigger_sweep_channels.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent

SWEEP_PATH = CORE_SCRIPTS / "insight-trigger-sweep.py"
_spec = importlib.util.spec_from_file_location("its_channels_under_test", SWEEP_PATH)
its = importlib.util.module_from_spec(_spec)
sys.modules["its_channels_under_test"] = its
_spec.loader.exec_module(its)


def _msg(msg_id, *, author="omni", target="alpha", action="revisit",
         severity="constrains", tags=None, hours_ago=3.0):
    """One board row. `tags=[]` yields a post with NO routing tags."""
    if tags is None:
        tags = [
            f"requires_action_by:{target}",
            f"action_type:{action}",
            f"severity:{severity}",
        ]
    ts = (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S")
    return json.dumps({
        "id": msg_id,
        "author": author,
        "type": "handoff",
        "text": f"test trigger {msg_id}",
        "tags": tags,
        "timestamp": ts,
    }) + "\n"


@pytest.fixture
def board(monkeypatch, tmp_path: Path):
    """Sandbox BOARD_DIR + an empty agents root, and stub the filing side."""
    board_dir = tmp_path / "world" / "board"
    board_dir.mkdir(parents=True)
    asp_jsonl = tmp_path / "world" / "aspirations.jsonl"
    asp_jsonl.write_text("", encoding="utf-8")
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()

    monkeypatch.setattr(its, "BOARD_DIR", board_dir)
    monkeypatch.setattr(its, "WORLD_ASPS", asp_jsonl)
    monkeypatch.setattr(its, "_agents_root", lambda: agents_dir)
    #  hermeticity: neutralize the addressing-resolution inputs so
    # these channel pins never read the REAL registry/roster (empty registry
    # + empty roster => empty collision set => routing behavior these tests
    # pin is unchanged by the addressing rule).
    monkeypatch.setattr(its, "ENV_REGISTRY_DIR", tmp_path / "no-environments")
    monkeypatch.setattr(its, "_self_env", lambda: "test-env")
    monkeypatch.setattr(its, "_local_roster", lambda: set())

    filed = []

    def fake_file_goal(trigger, *, dry_run=False):
        filed.append(trigger)
        return {"would_file": dry_run, "rc": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(its, "file_goal", fake_file_goal)
    return {"dir": board_dir, "filed": filed}


def _write(board, channel, *rows):
    (board["dir"] / f"{channel}.jsonl").write_text("".join(rows), encoding="utf-8")


# ---------------------------------------------------------------------------
# 1 + 2 — the widening itself
# ---------------------------------------------------------------------------


def test_coordination_trigger_is_picked_up(board):
    """The reported defect: a requires_action_by post on coordination.

    Modeled on the real msg-20260728-194530-omni-5115 (omni -> alpha,
    action_type:revisit) that sat undelivered because the sweep never opened
    coordination.jsonl.
    """
    _write(board, "coordination", _msg("msg-coord-1"))
    triggers = its.load_triggers()

    assert len(triggers) == 1, f"coordination post not swept: {triggers}"
    assert triggers[0]["msg_id"] == "msg-coord-1"
    assert triggers[0]["channel"] == "coordination"
    assert triggers[0]["target"] == "alpha"
    assert triggers[0]["action"] == "revisit"


@pytest.mark.parametrize(
    "channel", ["findings", "coordination", "general", "decisions", "reasoning"])
def test_every_live_channel_is_swept(board, channel):
    """Discovery, not an allowlist — including channels with zero traffic today.

    general / decisions / reasoning carried 0 fully-routable posts all-time as
    of 2026-07-29. They are pinned anyway: the point of the fix is that a
    channel need not be anticipated to be swept, and a regression to a
    two-name constant would pass the coordination pin alone.
    """
    _write(board, channel, _msg(f"msg-{channel}-1"))
    triggers = its.load_triggers()

    assert len(triggers) == 1, f"{channel} not swept: {triggers}"
    assert triggers[0]["channel"] == channel


def test_all_channels_swept_in_one_run(board):
    """Multi-channel run — not N single-channel runs that each happen to pass."""
    _write(board, "findings", _msg("msg-f-1"))
    _write(board, "coordination", _msg("msg-c-1"))
    _write(board, "decisions", _msg("msg-d-1"))

    got = {t["msg_id"]: t["channel"] for t in its.load_triggers()}
    assert got == {"msg-f-1": "findings", "msg-c-1": "coordination",
                   "msg-d-1": "decisions"}


# ---------------------------------------------------------------------------
# 3 — non-channel files stay out
# ---------------------------------------------------------------------------


def test_reads_sidecar_and_archive_are_not_channels(board):
    """`-reads.jsonl` and `-archive.jsonl` are excluded by suffix.

    The sidecar is written with message-shaped rows here on purpose: exclusion
    must come from the FILENAME, not from the rows failing the tag filter. A
    sidecar that happened to contain a routable row would otherwise be swept.
    """
    _write(board, "coordination", _msg("msg-live-1"))
    _write(board, "coordination-reads", _msg("msg-sidecar-1"))
    _write(board, "coordination-archive", _msg("msg-archive-1"))
    _write(board, "coordination-archive-archive", _msg("msg-archive2-1"))

    names = [p.name for p in its.board_channels()]
    assert names == ["coordination.jsonl"], names

    ids = {t["msg_id"] for t in its.load_triggers()}
    assert ids == {"msg-live-1"}, ids


def test_board_channels_empty_when_dir_absent(monkeypatch, tmp_path):
    """No board/ at all -> [] rather than an exception."""
    monkeypatch.setattr(its, "BOARD_DIR", tmp_path / "nope")
    assert its.board_channels() == []


# ---------------------------------------------------------------------------
# 4 — the filed goal's board pointer
# ---------------------------------------------------------------------------


def test_goal_pointer_names_the_channel_it_was_read_from(board):
    """The pointer must resolve. It was hardcoded to findings.jsonl."""
    _write(board, "coordination", _msg("msg-coord-2"))
    triggers = its.load_triggers()
    assert triggers, "fixture produced no trigger — pointer assertion would be vacuous"

    payload = its._build_goal_payload(triggers[0])
    assert "world/board/coordination.jsonl (msg-coord-2)" in payload["description"]
    assert "findings.jsonl" not in payload["description"]


# ---------------------------------------------------------------------------
# 5 — scope is visible in the output
# ---------------------------------------------------------------------------


def test_summary_reports_channels_scanned(board, monkeypatch, capsys):
    _write(board, "findings", _msg("msg-f-2"))
    _write(board, "coordination", _msg("msg-c-2"))
    _write(board, "coordination-reads", _msg("msg-sc-2"))

    monkeypatch.setattr(sys, "argv", ["insight-trigger-sweep.py", "--dry-run", "--json"])
    assert its.main() == 0
    summary = json.loads(capsys.readouterr().out)

    assert summary["channels_scanned"] == ["coordination", "findings"]
    assert summary["scanned"] == 2


# ---------------------------------------------------------------------------
# 6 — SPECIFICITY control (guard-1660)
# ---------------------------------------------------------------------------


def test_untagged_coordination_post_is_still_dropped(board):
    """Widening the channels must not widen WHAT converts.

    Without this pin, deleting the tag filter outright would satisfy every
    assertion above — the sweep would look fixed while having become a
    "file a goal for every board post" machine. coordination.jsonl alone
    holds 5696 rows.
    """
    _write(board, "coordination",
           _msg("msg-routable"),
           _msg("msg-chatter", tags=["lodestar-commons", "trust-ledger"]),
           _msg("msg-half-tagged", tags=["requires_action_by:alpha"]),
           _msg("msg-other-half", tags=["action_type:revisit"]))

    ids = {t["msg_id"] for t in its.load_triggers()}
    assert ids == {"msg-routable"}, ids


def test_window_and_grace_still_bound_the_widened_scan(board):
    """DO item 5 of : WINDOW_HOURS was exonerated, do not widen it.

    A regression that dropped the time filter while widening channels would
    replay years of archived triggers across every channel at once.
    """
    _write(board, "coordination",
           _msg("msg-in-window", hours_ago=3.0),
           _msg("msg-too-old", hours_ago=48.0),
           _msg("msg-too-fresh", hours_ago=0.2))

    ids = {t["msg_id"] for t in its.load_triggers()}
    assert ids == {"msg-in-window"}, ids
