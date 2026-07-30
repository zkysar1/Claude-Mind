"""Tests for peer_board_post.py ().

The load-bearing test is `test_peer_backend_is_forced_not_inherited`. Everything
else here is ordinary contract coverage; that one pins the single safety
property the module exists for, and it is written so that DELETING the pin makes
it fail. A backend pin that silently stopped working would look exactly like one
that works -- the write still succeeds, it just lands in the wrong store -- which
is precisely how the 2026-07-09 truncation (guard-955 / rb-2983) went unnoticed.

`test_unreachable_peer_never_writes_locally` is the second one that matters: the
dangerous failure is not "refused to post", it is "posted to the wrong world."
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS / "peer_board_post.py"
PROJECT_ROOT = SCRIPTS.parent.parent

EXIT_OK, EXIT_USAGE, EXIT_UNREACHABLE, EXIT_REFUSED = 0, 2, 3, 4


def run(args, stdin="msg", env_extra=None):
    env = os.environ.copy()
    # Caller is deliberately own-cloud in every test: that is the hazard shape.
    env.update({"STORAGE_BACKEND": "own-cloud", "ENVIRONMENT_ID": "ayoai-mind",
                "MIND_AGENT": "foxtrot"})
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(SCRIPT), *args], input=stdin,
                          capture_output=True, text=True, env=env, timeout=90)


@pytest.fixture
def peer_world(tmp_path):
    w = tmp_path / "peerworld"
    (w / "board").mkdir(parents=True)
    return w


def test_unknown_peer_exits_usage_and_lists_known(tmp_path):
    r = run(["--peer", "nonesuch", "--channel", "coordination"])
    assert r.returncode == EXIT_USAGE
    # The message must name the real alternatives, not just complain.
    assert "zds-mind" in r.stderr and "ayoai-mind" in r.stderr


def test_self_post_is_refused(tmp_path):
    r = run(["--peer", "ayoai-mind", "--channel", "coordination"])
    assert r.returncode == EXIT_REFUSED
    assert "board-post.sh" in r.stderr, "refusal must name the correct local tool"


def test_unreachable_peer_exits_3_with_actionable_env_var(tmp_path):
    r = run(["--peer", "zds-mind", "--channel", "coordination"])
    assert r.returncode == EXIT_UNREACHABLE
    assert "PEER_WORLD_ZDS_MIND" in r.stderr, "must name the exact env var to set"


def test_unreachable_peer_never_writes_locally(tmp_path):
    """The dangerous failure is posting to the WRONG world, not refusing.

    A fallback-to-local would satisfy 'the post went somewhere' while silently
    addressing the wrong deployment.
    """
    local_board = PROJECT_ROOT / ".mind-data" / "world" / "board" / "coordination.jsonl"
    before = local_board.stat().st_size if local_board.is_file() else None
    r = run(["--peer", "zds-mind", "--channel", "coordination"])
    assert r.returncode == EXIT_UNREACHABLE
    after = local_board.stat().st_size if local_board.is_file() else None
    assert before == after, "an unreachable peer write must NOT touch the local board"


def test_peer_backend_is_forced_not_inherited(peer_world):
    """THE safety property: caller own-cloud, peer local -> resolved MUST be local.

    Mutation-VERIFIED, not merely asserted: changing the pin in
    _force_peer_backend from an overwrite to `os.environ.setdefault(...)` -- the
    real-world shape of this defect, where the caller's value silently wins --
    makes this test fail.

    That verification is why `peer_backend` is read back out of the environment
    rather than returned from the registry. An earlier version of this test
    asserted on the returned registry value and PASSED under that same mutation,
    while claiming in its own docstring to be mutation-proof: it pinned "the
    registry was read correctly", which is true whether or not the pin took
    effect. A test for a safety property has to observe the property, not the
    intent behind it.
    """
    r = run(["--peer", "zds-mind", "--channel", "coordination", "--dry-run"],
            env_extra={"PEER_WORLD_ZDS_MIND": str(peer_world)})
    assert r.returncode == EXIT_OK, r.stderr
    out = json.loads(r.stdout)
    assert out["peer_backend"] == "local", (
        "peer backend must come from the PEER registry entry, not the caller env")
    assert out["peer_backend"] != "own-cloud", "caller's backend must never win"


def test_author_uses_at_not_hyphen(peer_world):
    """`@` is required: every env-id contains a hyphen, so the hyphen form is
    ambiguous (alpha-ayoai-mind cannot be split back into agent+env)."""
    r = run(["--peer", "zds-mind", "--channel", "coordination", "--dry-run"],
            env_extra={"PEER_WORLD_ZDS_MIND": str(peer_world)})
    author = json.loads(r.stdout)["record"]["author"]
    assert author == "foxtrot@ayoai-mind"
    assert "@" in author and not author.endswith("-ayoai-mind")


def test_cross_deployment_tag_is_always_added(peer_world):
    """The installed base is 0.7% tagged; new posts must not extend that."""
    r = run(["--peer", "zds-mind", "--channel", "findings", "--tags", "mytag", "--dry-run"],
            env_extra={"PEER_WORLD_ZDS_MIND": str(peer_world)})
    tags = json.loads(r.stdout)["record"]["tags"]
    assert "cross-deployment" in tags and "mytag" in tags


def test_dry_run_writes_nothing(peer_world):
    run(["--peer", "zds-mind", "--channel", "coordination", "--dry-run"],
        env_extra={"PEER_WORLD_ZDS_MIND": str(peer_world)})
    assert not (peer_world / "board" / "coordination.jsonl").exists()


def test_real_writes_reparse_with_unique_ids(peer_world):
    """The goal's own verification check: every line reparses, no duplicate ids."""
    for i in range(3):
        r = run(["--peer", "zds-mind", "--channel", "coordination"], stdin=f"m{i}",
                env_extra={"PEER_WORLD_ZDS_MIND": str(peer_world)})
        assert r.returncode == EXIT_OK, r.stderr
    lines = [l for l in (peer_world / "board" / "coordination.jsonl")
             .read_text(encoding="utf-8").splitlines() if l.strip()]
    ids = [json.loads(l)["id"] for l in lines]   # raises if any line fails to reparse
    assert len(ids) == 3
    assert len(set(ids)) == 3, f"duplicate ids: {ids}"


def test_empty_stdin_is_rejected(peer_world):
    r = run(["--peer", "zds-mind", "--channel", "coordination"], stdin="   ",
            env_extra={"PEER_WORLD_ZDS_MIND": str(peer_world)})
    assert r.returncode == EXIT_USAGE


@pytest.mark.parametrize("bad", ["../../escaped", "../sneak", "a/b", "/abs", "UPPER", ""])
def test_traversal_and_malformed_channels_are_refused(peer_world, tmp_path, bad):
    """A channel becomes a PATH SEGMENT, so '..' escapes the peer's board dir.

    Confirmed by real write before the fix: `--channel ../../escaped` wrote to
    <world>/board/../../escaped.jsonl and left board/ empty. The helper exists to
    make a cross-deployment write SAFE; one steerable by its own channel argument
    does not deliver that.
    """
    r = run(["--peer", "zds-mind", "--channel", bad],
            env_extra={"PEER_WORLD_ZDS_MIND": str(peer_world)})
    assert r.returncode == EXIT_USAGE, f"channel {bad!r} was not refused"
    # Nothing may be created anywhere outside the (empty) board dir.
    assert list((peer_world / "board").iterdir()) == []


def test_seq_allocation_is_structurally_inside_the_lock():
    """STRUCTURAL pin: seq must come from the allocator, not a pre-lock count.

    This is a source assertion rather than a behavioral one, and that is a
    deliberate, measured choice. The obvious behavioral test -- spawn N concurrent
    writers and assert unique ids -- was written first and MUTATION-TESTED: with
    the pre-lock count race reintroduced, it still PASSED. Subprocess startup
    jitter (~100ms) dwarfs the race window (microseconds), so the writers
    serialize naturally and the window never opens. A test that cannot fail on
    the defect it names is worse than no test, because it certifies the fix.

    So the property is pinned where it is actually checkable: the code must use
    the in-lock allocator and must not re-derive seq from a separate file read.
    Same rationale as test_cygpath_wrapper_pattern.py, which asserts on source for
    a property whose failure only manifests on another platform.

    board.py already found and fixed this exact race (its cmd_post comment cites
    msg-20260428-045553-alpha-NNN). This helper reintroduced it by not reading the
    sibling first -- guard-1853.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    assert "locked_append_jsonl_with_allocator" in src, (
        "must use the in-lock allocator (board.py's fix for this same race)")
    # The bare append primitive must NOT be the write path: it cannot see the
    # in-lock snapshot, so any seq passed to it was computed before the lock.
    assert "locked_append_jsonl(target" not in src, (
        "regression: bare locked_append_jsonl reintroduces the pre-lock seq race")
    # NOTE: deliberately NOT asserting the absence of a line-count anywhere in
    # the file. main() legitimately counts lines in the --dry-run branch to
    # PREVIEW the seq, and a first draft of this test flagged exactly that,
    # failing on correct code. The two assertions above are what mutation
    # testing actually showed to catch the race; a third that fires on the
    # baseline is not extra safety, it is a broken test.


def test_concurrent_writes_lose_no_records(peer_world):
    """Smoke test only -- NOT a race detector (see the structural test above).

    Mutation-tested: this passes with the race reintroduced. It is retained
    because it does prove concurrent invocations neither crash nor drop records,
    which is worth knowing; it is NOT evidence the seq allocation is correct.
    """
    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(
            lambda i: run(["--peer", "zds-mind", "--channel", "coordination"],
                          stdin=f"concurrent-{i}",
                          env_extra={"PEER_WORLD_ZDS_MIND": str(peer_world)}),
            range(6)))
    assert all(r.returncode == EXIT_OK for r in results), \
        [r.stderr for r in results if r.returncode != EXIT_OK]
    lines = [l for l in (peer_world / "board" / "coordination.jsonl")
             .read_text(encoding="utf-8").splitlines() if l.strip()]
    ids = [json.loads(l)["id"] for l in lines]
    assert len(ids) == 6, f"lost a concurrent write: {len(ids)} of 6"
    assert len(set(ids)) == 6, f"duplicate id under concurrency: {sorted(ids)}"


def test_id_shape_matches_board_py(peer_world):
    """Same channel, same id shape: board.py zero-pads seq to 3 digits."""
    r = run(["--peer", "zds-mind", "--channel", "coordination", "--dry-run"],
            env_extra={"PEER_WORLD_ZDS_MIND": str(peer_world)})
    assert json.loads(r.stdout)["record"]["id"].endswith("-001")


def test_resolved_but_missing_dir_is_unreachable(tmp_path):
    """A configured-but-absent path must refuse, not create the peer world."""
    ghost = tmp_path / "does-not-exist"
    r = run(["--peer", "zds-mind", "--channel", "coordination"],
            env_extra={"PEER_WORLD_ZDS_MIND": str(ghost)})
    assert r.returncode == EXIT_UNREACHABLE
    assert not ghost.exists(), "must not create the peer world it failed to find"
