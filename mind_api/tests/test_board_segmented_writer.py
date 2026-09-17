"""Pins for the board SEGMENTED WRITER rule ().

Every lane that posts to THIS world's board resolves its target file through ONE
rule, `_board_paths.write_name`: the daemon endpoint (`board_write.post`) and the
CLI (`board.py post`). The rule is DEFAULT OFF. With `BOARD_SEGMENTED_CHANNELS`
unset, every post still lands in `<channel>.jsonl`. A channel named in that list
appends to `<channel>-<YYYY-MM-DD>.jsonl` instead.

Why one rule matters is measured, not assumed: the gate-firings flip was
half-effective for a day because one writer lane carried the rule and a second
lane hard-coded the legacy name (`_gate_log.store_name`, g-358-16). So the lane
tests below drive BOTH lanes under the same env and assert they land in the SAME
file.

Two further properties are pinned because each fails silently:
  * guard-6907 - the name the writer MINTS must classify to its parent channel's
    merge handler. A None there is write-class (b) fence-only on a six-writer
    store, which makes a stale fence a permanent wedge.
  * the reply_to warning must look across the whole live half. A segment holds
    one day, so checking only the held file warns on nearly every real reply.

Write tests use the `general` channel on purpose: a `coordination` post touches
peer wake signals and a `findings` post increments citation counters, and neither
side effect belongs in a test of which FILE a post lands in. The rule is
channel-generic, so `general` exercises it fully. The lane that writes into
ANOTHER world's board must ignore the flag; that pin lives in
core/scripts/tests/test_peer_board_post.py beside that lane's own fixtures.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "core" / "scripts"
BOARD_PY = SCRIPTS / "board.py"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _board_paths  # noqa: E402
from mind_api.src.endpoints import board, board_write  # noqa: E402

ENV = _board_paths.SEGMENTED_ENV
CH = "general"


class _FakePaths:
    def __init__(self, world: Path):
        self.world = world
        self.agent_name = "alpha"


class _FakeCtx:
    def __init__(self, world: Path, query: dict, body: bytes = b""):
        self.paths = _FakePaths(world)
        self.query = query
        self.body = body
        self.headers = {"x-mind-agent": "alpha"}


def _world(tmp_path: Path, name: str = "world") -> Path:
    w = tmp_path / name
    (w / "board").mkdir(parents=True)
    return w


def _lines(path: Path):
    if not path.exists():
        return []
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _segments(world: Path, channel: str = CH):
    return sorted(p.name for p in (world / "board").glob(f"{channel}-*.jsonl")
                  if _board_paths.is_segment(channel, p.name))


def _daemon_post(world: Path, text: str, **query):
    resp = board_write.post(_FakeCtx(world, {"channel": CH, "author": "alpha", **query},
                                     text.encode("utf-8")))
    body = resp.body.decode("utf-8") if isinstance(resp.body, bytes) else str(resp.body)
    return json.loads(body)


def _cli_post(world: Path, meta: Path, text: str, *extra, segmented=None):
    env = dict(os.environ)
    env.update({"MIND_WORLD": str(world), "MIND_META": str(meta),
                "MIND_AGENT": "alpha", "STORAGE_BACKEND": "local"})
    env.pop("MIND_SID", None)
    env.pop(ENV, None)
    if segmented is not None:
        env[ENV] = segmented
    meta.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, str(BOARD_PY), "post", "--channel", CH, "--author", "alpha", *extra],
        input=text, text=True, env=env, cwd=str(REPO_ROOT), capture_output=True, timeout=60)
    assert proc.returncode == 0, f"board.py post rc={proc.returncode}\n{proc.stderr}"
    return proc


def _seed_live_post(world: Path, mid: str):
    rec = {"id": mid, "author": "bravo", "session_id": "",
           "timestamp": "2026-09-10T08:00:00", "channel": CH, "type": "status",
           "text": "an older post", "reply_to": None, "tags": []}
    with open(world / "board" / f"{CH}.jsonl", "w", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=True) + "\n")


# --- The rule ----------------------------------------------------------------

def test_rule_is_off_by_default_and_a_truthy_value_flips_nothing(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    assert _board_paths.write_name("coordination") == "coordination.jsonl"
    for truthy in ("1", "true", "yes"):
        monkeypatch.setenv(ENV, truthy)
        assert _board_paths.write_name("coordination") == "coordination.jsonl", truthy


def test_rule_flips_only_the_channels_it_names(monkeypatch):
    monkeypatch.setenv(ENV, " coordination , findings ")
    day = dt.date(2026, 9, 17)
    name = _board_paths.write_name("coordination", day)
    assert name == "coordination-2026-09-17.jsonl"
    assert _board_paths.write_name("findings", day) == "findings-2026-09-17.jsonl"
    assert _board_paths.write_name("general", day) == "general.jsonl"
    # The reader recognises exactly what the writer mints.
    assert _board_paths.is_segment("coordination", name)
    assert _board_paths.segment_parent(name) == "coordination"


def test_minted_name_classifies_to_its_parent_channels_merge_handler(monkeypatch):
    """guard-6907: classify the name the writer PRODUCES, made by the writer's own
    helper, never the live file it derives from."""
    import board as board_cli
    from coordination_merge import merge_handler_for, merge_rotated_board_jsonl

    for channel in board_cli.DEFAULT_CHANNELS:
        monkeypatch.setenv(ENV, channel)
        minted = _board_paths.write_name(channel)
        assert minted != _board_paths.live_name(channel), channel
        assert merge_handler_for(f"world/board/{minted}") is merge_rotated_board_jsonl, minted
        assert (merge_handler_for(f"world/board/{minted}")
                is merge_handler_for(f"world/board/{channel}.jsonl")), minted


# --- Both writer lanes -------------------------------------------------------

def test_daemon_post_with_the_flag_off_appends_to_the_live_file(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    world = _world(tmp_path)
    out = _daemon_post(world, "flag off")
    assert out["ok"] is True
    assert len(_lines(world / "board" / f"{CH}.jsonl")) == 1
    assert _segments(world) == []


def test_daemon_post_with_the_flag_on_appends_to_todays_segment(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, CH)
    world = _world(tmp_path)
    before = dt.datetime.now().date()
    out = _daemon_post(world, "flag on")
    after = dt.datetime.now().date()
    segs = _segments(world)
    assert segs in ([_board_paths.segment_name(CH, before)],
                    [_board_paths.segment_name(CH, after)]), segs
    assert not (world / "board" / f"{CH}.jsonl").exists(), "live file must not be written"
    rows = [json.loads(ln) for ln in _lines(world / "board" / segs[0])]
    assert [r["id"] for r in rows] == [out["id"]]


def test_cli_and_daemon_lanes_land_in_the_same_file_with_the_same_line_shape(tmp_path, monkeypatch):
    """The gate-firings failure, pinned: both lanes read the same env and must
    resolve the same target. Line structure stays byte-compatible across lanes."""
    monkeypatch.setenv(ENV, CH)
    cli_world = _world(tmp_path, "cli")
    dae_world = _world(tmp_path, "dae")
    _cli_post(cli_world, tmp_path / "cli-meta", "same rule", segmented=CH)
    _daemon_post(dae_world, "same rule")

    assert _segments(cli_world) == _segments(dae_world) != []
    assert not (cli_world / "board" / f"{CH}.jsonl").exists()
    cli_line = _lines(cli_world / "board" / _segments(cli_world)[0])[0]
    dae_line = _lines(dae_world / "board" / _segments(dae_world)[0])[0]
    c, d = json.loads(cli_line), json.loads(dae_line)
    assert list(c) == list(d)
    assert cli_line == json.dumps(c, ensure_ascii=True)
    assert dae_line == json.dumps(d, ensure_ascii=True)


# --- reply_to across the live half --------------------------------------------

def test_daemon_reply_to_a_parent_in_the_live_file_does_not_warn(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, CH)
    world = _world(tmp_path)
    _seed_live_post(world, "msg-20260910-080000-bravo-001")

    ok = _daemon_post(world, "a reply", reply_to="msg-20260910-080000-bravo-001")
    assert "warnings" not in ok, ok.get("warnings")

    dangling = _daemon_post(world, "a guess", reply_to="msg-20260910-080000-bravo-999")
    assert any("not found" in w for w in dangling.get("warnings", [])), dangling


def test_cli_reply_to_a_parent_in_the_live_file_does_not_warn(tmp_path):
    world = _world(tmp_path)
    meta = tmp_path / "meta"
    _seed_live_post(world, "msg-20260910-080000-bravo-001")

    ok = _cli_post(world, meta, "a reply", "--reply-to", "msg-20260910-080000-bravo-001",
                   segmented=CH)
    assert "not found" not in ok.stderr, ok.stderr

    dangling = _cli_post(world, meta, "a guess", "--reply-to", "msg-20260910-080000-bravo-999",
                         segmented=CH)
    assert "not found" in dangling.stderr, dangling.stderr


# --- The reader sees what the writer wrote ------------------------------------

def test_a_segmented_post_is_returned_by_the_read_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, CH)
    world = _world(tmp_path)
    _seed_live_post(world, "msg-20260910-080000-bravo-001")
    out = _daemon_post(world, "written to a segment")

    resp = board.read(_FakeCtx(world, {"channel": CH, "json": "1"}))
    body = resp.body.decode("utf-8") if isinstance(resp.body, bytes) else str(resp.body)
    ids = [json.loads(ln)["id"] for ln in body.splitlines() if ln.strip()]
    assert ids == ["msg-20260910-080000-bravo-001", out["id"]]


@pytest.mark.parametrize("value", ["", "coordination"])
def test_an_unnamed_channel_is_untouched_whatever_else_is_flipped(tmp_path, monkeypatch, value):
    monkeypatch.setenv(ENV, value)
    world = _world(tmp_path)
    _daemon_post(world, "general stays on the live file")
    assert len(_lines(world / "board" / f"{CH}.jsonl")) == 1
    assert _segments(world) == []
