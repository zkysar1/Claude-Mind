"""Tests for peer_surface.py -- the /prime cross-deployment surface ().

Each test pins one of the three measured traps documented in the module
docstring. All three produce a WRONG-BUT-PLAUSIBLE peer count rather than an
error, which is why they need explicit pins.
"""
import io
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import peer_board_post  # noqa: E402
import peer_surface  # noqa: E402


@pytest.fixture(autouse=True)
def _no_ambient_peer_pointers(monkeypatch, tmp_path):
    """The surface now reads PEER_WORLD_* and the registry's peer_world_path ().
    Scrub both so a box with a real pointer cannot change what these tests see: an
    inherited override is what turned refusal tests into real writes (guard-7618)."""
    for key in [k for k in os.environ if k.startswith("PEER_WORLD_")]:
        monkeypatch.delenv(key)
    empty = tmp_path / "empty-registry"
    empty.mkdir()
    monkeypatch.setattr(peer_board_post, "ENV_REGISTRY", empty)


REGISTRY = {
    "ayoai-mind": "own-cloud",
    "claude-mind": "local",
    "zds-mind": "local",
    "local": "local",
}
SELF = "ayoai-mind"
ROSTER = ["alpha", "bravo", "echo", "foxtrot", "zeta"]


def row(author, channel="coordination"):
    return {"author": author, "channel": channel, "text": "x"}


def classify(rows):
    return peer_surface.classify(rows, SELF, REGISTRY, ROSTER)


# ── Trap 1: board-read.sh --json emits JSONL, not a JSON array ───────────

def test_parse_jsonl_reads_concatenated_objects():
    """json.load() raises 'Extra data' on line 2 of this input."""
    text = "\n".join(json.dumps({"author": "omni", "n": i}) for i in range(3))
    assert len(peer_surface.parse_jsonl(io.StringIO(text))) == 3


def test_parse_jsonl_rejects_array_assumption():
    """A real JSON array is NOT the wire format; one line -> one row, not 3."""
    assert len(peer_surface.parse_jsonl(io.StringIO(json.dumps([1, 2, 3])))) == 1


def test_parse_jsonl_skips_blank_and_malformed_lines():
    text = '{"author": "omni"}\n\nnot json\n{"author": "omni@zds-mind"}\n'
    assert len(peer_surface.parse_jsonl(io.StringIO(text))) == 2


# ── Trap 3: "author not in roster" over-counts ───────────────────────────

def test_bare_local_artifact_authors_are_not_counted_as_peers():
    """Measured 2026-07-30: `investigate` / `meta-tiebreaker` are LOCAL posts
    whose author field captured a goal-title fragment. They are non-roster, so
    a naive not-in-roster predicate counts them as peer traffic. They must be
    excluded from the count AND surfaced, not silently dropped."""
    res = classify([row("investigate"), row("meta-tiebreaker")])
    assert res["inbound_total"] == 0
    assert res["unattributed"] == {"investigate": 1, "meta-tiebreaker": 1}


def test_bare_author_attributes_only_with_at_form_evidence():
    """Bare `omni` counts ONLY because `omni@zds-mind` is independently seen."""
    res = classify([row("omni"), row("omni"), row("omni@zds-mind")])
    assert res["inbound_total"] == 3
    assert res["confirmed"] == 1
    assert res["attributed"] == 2
    assert res["by_env"] == {"zds-mind": 3}
    assert res["unattributed"] == {}


def test_bare_author_without_evidence_stays_unattributed():
    """Same author, no @-form anywhere -> not counted. This is the ONLY thing
    separating `omni` from `investigate`; drop the evidence and omni must fall
    back to unattributed too."""
    res = classify([row("omni"), row("omni")])
    assert res["inbound_total"] == 0
    assert res["unattributed"] == {"omni": 2}


def test_local_roster_authors_never_counted_or_reported():
    res = classify([row("bravo"), row("alpha"), row("zeta")])
    assert res["inbound_total"] == 0
    assert res["unattributed"] == {}


def test_self_env_marker_is_not_inbound():
    """A local agent stamping its own env-id is outbound-shaped, not inbound."""
    res = classify([row("bravo@ayoai-mind")])
    assert res["inbound_total"] == 0


def test_unregistered_env_marker_is_not_counted_but_IS_reported():
    """An env-id absent from the registry must not be counted as peer traffic
    (we cannot vouch for it) but must never be dropped silently either.

    The first draft did exactly that -- not counted, not reported, zero trace,
    indistinguishable from no traffic. Caught by fresh-eyes review of this very
    file. A deployment nobody registered is the highest-signal thing this
    surface can see, so silence is the worst possible handling."""
    res = classify([row("someone@not-a-registered-world")] * 2)
    assert res["inbound_total"] == 0
    assert res["unregistered_envs"] == {"not-a-registered-world": 2}
    assert res["unattributed"] == {}  # distinct bucket: has a marker, just unknown


def test_self_env_marker_is_not_reported_as_unregistered():
    """Guards the fix's blast radius: `bravo@ayoai-mind` is outbound-shaped and
    must stay out of BOTH the peer count and the unregistered bucket."""
    res = classify([row("bravo@ayoai-mind")])
    assert res["inbound_total"] == 0
    assert res["unregistered_envs"] == {}


def test_unregistered_line_is_rendered(monkeypatch, capsys):
    out = _render([row("ghost@who-dis")], monkeypatch, capsys)
    assert "UNREGISTERED" in out
    assert "who-dis" in out


def test_channel_and_env_breakdown():
    res = classify([
        row("omni@zds-mind", "coordination"),
        row("omni", "findings"),
        row("omni", "findings"),
    ])
    assert res["by_channel"] == {"coordination": 1, "findings": 2}
    assert res["by_env"] == {"zds-mind": 3}


# ── split_author: '@' not '-', because every env-id contains a hyphen ────

@pytest.mark.parametrize("author,expected", [
    ("omni@zds-mind", ("omni", "zds-mind")),
    ("omni", ("omni", None)),
    ("alpha-ayoai-mind", ("alpha-ayoai-mind", None)),  # hyphen form: unsplittable
    ("", ("", None)),
    (None, ("", None)),
])
def test_split_author(author, expected):
    assert peer_surface.split_author(author) == expected


# ── Trap 2 / display: absence of traffic is not absence of a channel ─────

def _render(rows, monkeypatch, capsys, **env):
    monkeypatch.setattr(sys, "stdin", io.StringIO(
        "\n".join(json.dumps(r) for r in rows)))
    base = {
        "PEER_SELF_ENV": SELF,
        "PEER_REGISTRY": json.dumps(REGISTRY),
        "PEER_ROSTER": ",".join(ROSTER),
        "PEER_WINDOW": "7d",
        "PEER_JSON": "",
    }
    base.update(env)
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    peer_surface.main()
    return capsys.readouterr().out


def test_quiet_window_does_not_read_as_no_channel(monkeypatch, capsys):
    out = _render([], monkeypatch, capsys)
    assert "live but quiet" in out
    assert "Peers: 3 registered" in out


def test_display_names_peers_backends_and_pointer(monkeypatch, capsys):
    out = _render([row("omni@zds-mind")], monkeypatch, capsys)
    assert "zds-mind:local" in out
    assert "self=ayoai-mind:own-cloud" in out
    assert "peer-board-post.sh" in out
    assert "cross-deployment-channel.md" in out


def test_single_env_inbound_line_is_not_redundant(monkeypatch, capsys):
    out = _render([row("omni@zds-mind")], monkeypatch, capsys)
    assert "from zds-mind" in out
    assert "from zds-mind 1" not in out


def test_unreadable_registry_says_so_rather_than_reporting_zero_peers(
        monkeypatch, capsys):
    out = _render([], monkeypatch, capsys, PEER_REGISTRY="{}")
    assert "registry unreadable" in out
    assert "0 peers" not in out


def test_json_mode_is_machine_readable(monkeypatch, capsys):
    out = _render([row("omni@zds-mind")], monkeypatch, capsys, PEER_JSON="1")
    data = json.loads(out)
    assert data["inbound_total"] == 1
    assert data["self_backend"] == "own-cloud"
    assert sorted(data["peers"]) == ["claude-mind", "local", "zds-mind"]
    assert data["local_copies"] == [], "no pointer on this box -> no copy to report"


# ── Local peer copies: which of OUR posts never left this box () ──
# Real git checkouts in tmp dirs, not mocks: the property is what `git diff` says
# about a working copy, so a stub would only restate our own assumption.

NOW = datetime(2026, 10, 6, 12, 0, 0)


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _post(post_id, env, hours_ago, channel="coordination"):
    stamped = (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S")
    return json.dumps({"id": post_id, "timestamp": stamped, "author": "agent@%s" % env,
                       "channel": channel, "origin_env": env, "text": "x"})


def _append(board, *posts):
    with board.open("a", encoding="utf-8", newline="\n") as fh:
        for p in posts:
            fh.write(p + "\n")


def _copy_report(world, self_env=SELF):
    return peer_surface.local_copy_report("zds-mind", world, self_env, NOW)


@pytest.fixture
def peer_copy(tmp_path):
    """A git checkout holding a peer world: one old post of ours and one of the peer's,
    both already committed (so a healthy copy contains old posts of ours on purpose)."""
    repo = tmp_path / "peerrepo"
    world = repo / ".mind-data" / "world"
    (world / "board").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")  # same name as the pushed branch, or a bare `git push` is refused
    for key, val in (("user.email", "t@example.com"), ("user.name", "t"),
                     ("commit.gpgsign", "false"), ("core.autocrlf", "false")):
        _git(repo, "config", key, val)
    board = world / "board" / "coordination.jsonl"
    board.write_text(_post("msg-ours-old", SELF, 72) + "\n" + _post("msg-theirs-old", "zds-mind", 72) + "\n",
                     encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo, world, board


def _give_upstream(tmp_path, repo):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "-u", "origin", "HEAD:refs/heads/main")


def test_a_copy_that_holds_nothing_back_is_ok_though_it_holds_old_posts_of_ours(peer_copy):
    """False-positive control: every post of ours that ever left is still in the file, so
    "an old post of ours exists" must never be the trigger."""
    _repo, world, _board = peer_copy
    rep = _copy_report(world)
    assert rep["status"] == "ok" and rep["held"] == []
    assert rep["ref"] == "HEAD" and rep["checked"] == 1


def test_only_an_old_uncommitted_post_of_OURS_is_held(peer_copy):
    """Three uncommitted lines, one qualifying: old+ours is held; a FRESH post of ours
    (it has had no time to leave) and an old post written by the PEER are not."""
    _repo, world, board = peer_copy
    _append(board, _post("msg-stranded", SELF, 48), _post("msg-fresh", SELF, 1),
            _post("msg-theirs-new", "zds-mind", 48))
    rep = _copy_report(world)
    assert rep["status"] == "held"
    assert [h["id"] for h in rep["held"]] == ["msg-stranded"]
    assert rep["held"][0]["age_hours"] == 48.0


def test_a_post_committed_here_but_never_pushed_is_held_against_the_upstream(peer_copy, tmp_path):
    """Delivery needs the push, not the commit: against HEAD this copy looks clean."""
    repo, world, board = peer_copy
    _give_upstream(tmp_path, repo)
    assert _copy_report(world)["status"] == "ok"
    _append(board, _post("msg-committed-unpushed", SELF, 48))
    _git(repo, "commit", "-q", "-am", "committed here, never pushed")
    rep = _copy_report(world)
    assert rep["ref"] == "upstream"
    assert [h["id"] for h in rep["held"]] == ["msg-committed-unpushed"]
    _git(repo, "push", "-q")  # positive control: the signal clears once it is delivered
    assert _copy_report(world)["status"] == "ok"


def test_untracked_board_files_are_not_checkable_rather_than_clean(tmp_path):
    repo = tmp_path / "r"
    world = repo / "world"
    (world / "board").mkdir(parents=True)
    _git(repo, "init", "-q")
    (world / "board" / "coordination.jsonl").write_text(_post("msg-x", SELF, 72) + "\n",
                                                        encoding="utf-8", newline="\n")
    assert _copy_report(world)["status"] == "not-tracked"


def test_a_plain_directory_is_not_checkable(tmp_path):
    world = tmp_path / "plain"
    (world / "board").mkdir(parents=True)
    assert _copy_report(world)["status"] == "not-a-git-checkout"


def test_a_world_without_a_board_directory_is_reported(tmp_path):
    (tmp_path / "w").mkdir()
    assert _copy_report(tmp_path / "w")["status"] == "no-board"


def test_an_unknown_own_environment_is_an_error_not_a_clean_bill(peer_copy):
    """With no env id we cannot recognise our own posts; reporting "ok" would be a false zero."""
    _repo, world, _board = peer_copy
    assert _copy_report(world, self_env="")["status"] == "error"


def test_lines_it_cannot_read_are_counted_not_dropped(peer_copy):
    _repo, world, board = peer_copy
    _append(board, "{not json", json.dumps({"id": "msg-bad-ts", "timestamp": "yesterday", "origin_env": SELF}))
    rep = _copy_report(world)
    assert rep["unreadable"] == 2 and rep["status"] == "ok"


def test_main_prints_nothing_about_copies_when_no_pointer_is_set(monkeypatch, capsys):
    assert "local copy" not in _render([], monkeypatch, capsys)


def test_main_flags_a_stranded_copy_and_says_what_to_do(peer_copy, monkeypatch, capsys):
    _repo, world, board = peer_copy
    _append(board, _post("msg-stranded", SELF, 48))
    out = _render([], monkeypatch, capsys, PEER_WORLD_ZDS_MIND=str(world))
    assert "/!\\ zds-mind" in out and "1 post(s) of ours" in out and "msg-stranded" in out
    assert "committed and pushed" in out and "PEER_WORLD_ZDS_MIND" in out


def test_main_gives_a_clean_copy_one_positive_line(peer_copy, monkeypatch, capsys):
    """No line at all would be indistinguishable from a check that never ran."""
    _repo, world, _board = peer_copy
    out = _render([], monkeypatch, capsys, PEER_WORLD_ZDS_MIND=str(world))
    assert "holds none of our posts back" in out and "/!\\" not in out


def test_main_json_carries_the_copy_reports(peer_copy, monkeypatch, capsys):
    _repo, world, board = peer_copy
    _append(board, _post("msg-stranded", SELF, 48))
    data = json.loads(_render([], monkeypatch, capsys, PEER_JSON="1", PEER_WORLD_ZDS_MIND=str(world)))
    [rep] = data["local_copies"]
    assert rep["peer"] == "zds-mind" and rep["status"] == "held"
    assert [h["id"] for h in rep["held"]] == ["msg-stranded"]


def test_a_pointer_declared_in_the_registry_is_followed_too(peer_copy, tmp_path, monkeypatch, capsys):
    _repo, world, board = peer_copy
    _append(board, _post("msg-stranded", SELF, 48))
    reg = tmp_path / "reg"
    reg.mkdir()
    (reg / "zds-mind.yaml").write_text(
        'environment_id: zds-mind\nbackend: local\npeer_world_path: "%s"  # a comment\n' % world.as_posix(),
        encoding="utf-8")
    monkeypatch.setattr(peer_board_post, "ENV_REGISTRY", reg)
    assert "msg-stranded" in _render([], monkeypatch, capsys)


@pytest.mark.parametrize("exc", [RuntimeError("boom"), SystemExit(2)], ids=["exception", "sys-exit"])
def test_a_failing_check_degrades_to_a_visible_line_and_keeps_the_rest(peer_copy, exc, monkeypatch, capsys):
    """/prime treats this surface as observability: a failure here must not take the
    lines above it down, and must not read as "nothing stranded" either."""
    _repo, world, _board = peer_copy

    def boom(*_a, **_k):
        raise exc
    monkeypatch.setattr(peer_surface, "local_copy_report", boom)
    out = _render([], monkeypatch, capsys, PEER_WORLD_ZDS_MIND=str(world))
    assert "local copy check failed" in out
    assert "Peers: 3 registered" in out and "peer-board-post.sh" in out
