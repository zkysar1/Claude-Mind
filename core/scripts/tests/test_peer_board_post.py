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
    # Drop any PEER_WORLD_* the operator set: inherited, it points the unreachable
    # tests at a REAL peer world (, rb-2312). Tests pin their own.
    env = {k: v for k, v in os.environ.items() if not k.startswith("PEER_WORLD_")}
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


def test_an_inherited_peer_world_override_is_never_used(peer_world, monkeypatch):
    """run() must not hand the script an operator's PEER_WORLD_* ().

    On a box whose environment set PEER_WORLD_ZDS_MIND to a real clone, the two
    unreachable tests above resolved the peer anyway, posted "msg" into that REAL
    peer world on every suite run, and then failed. This pins the scrub on every
    box, not only on the boxes that happen to set the variable.
    """
    monkeypatch.setenv("PEER_WORLD_ZDS_MIND", str(peer_world))
    r = run(["--peer", "zds-mind", "--channel", "coordination"])
    assert r.returncode == EXIT_UNREACHABLE, r.stderr
    assert list((peer_world / "board").iterdir()) == [], "the inherited peer world was written"


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


def test_segmented_writer_flag_never_reaches_a_peer_board(peer_world):
    """: BOARD_SEGMENTED_CHANNELS moves THIS world's writers onto date
    segments. The peer's readers run the peer's checkout, which may not understand
    segments, so this lane must keep writing the live file whatever the flag says."""
    r = run(["--peer", "zds-mind", "--channel", "coordination"],
            env_extra={"PEER_WORLD_ZDS_MIND": str(peer_world),
                       "BOARD_SEGMENTED_CHANNELS": "coordination"})
    assert r.returncode == EXIT_OK, r.stderr
    assert (peer_world / "board" / "coordination.jsonl").exists()
    minted = sorted(p.name for p in (peer_world / "board").glob("coordination-*.jsonl"))
    assert minted == [], f"peer lane wrote a segment: {minted}"


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


# ── delivery is UNCONFIRMED for a local peer (, guard-7610) ─────
# A local peer is a plain file on THIS box; exit 0 cannot say whether anyone reads or
# ships that copy. The JSON must say so on stdout, the channel callers actually read,
# and a peer whose store the tool really wrote to must NOT carry the warning (negative
# control: without it the positive assertions pass against a tool that warns about
# everything). It must also stay OFF stderr: the Bash tool merges stderr into the
# output a caller json-parses (rb-874).

def test_a_real_post_to_a_local_peer_says_delivery_is_unconfirmed(peer_world):
    r = run(["--peer", "zds-mind", "--channel", "coordination"],
            env_extra={"PEER_WORLD_ZDS_MIND": str(peer_world)})
    assert r.returncode == EXIT_OK, r.stderr
    out = json.loads(r.stdout)  # stdout stays ONE json document
    # EXACT key set, not a subset check (guard-3948): the two emit sites are separate
    # literals, and a subset assertion passes on both the shape with the field and the
    # shape of a site that forgot it.
    assert set(out) == {"posted", "peer", "peer_backend", "delivery", "delivery_note", "path"}
    assert out["peer_backend"] == "local"
    assert out["delivery"] == "unconfirmed"
    assert "THIS box's copy" in out["delivery_note"] and "pushed" in out["delivery_note"]
    assert "unconfirmed" not in r.stderr, "the warning belongs in the JSON, not on stderr"


def test_a_dry_run_to_a_local_peer_also_says_delivery_is_unconfirmed(peer_world):
    r = run(["--peer", "zds-mind", "--channel", "coordination", "--dry-run"],
            env_extra={"PEER_WORLD_ZDS_MIND": str(peer_world)})
    assert r.returncode == EXIT_OK, r.stderr
    out = json.loads(r.stdout)
    assert set(out) == {"would_write", "peer_backend", "delivery", "delivery_note", "record"}
    assert out["delivery"] == "unconfirmed" and out["delivery_note"]


def test_a_store_backed_peer_carries_no_unconfirmed_warning():
    """Negative control for the two tests above, on the pure function: the peer
    backends that really write to the peer's store must not be told 'unconfirmed'."""
    mod = _load_pbp()
    assert mod.delivery_fields("own-cloud") == {"delivery": "peer-store"}
    assert mod.delivery_fields("local")["delivery"] == "unconfirmed"


# ── G5 cross-world provenance () ───────────────────────────────────
# guard-3221: a coupling test must call the REAL producer and assert the marker
# appears, WITH a negative control asserting it does NOT appear when the
# upstream value is absent. Without the control the positive assertion passes
# against any non-empty record, which is exactly how an inert feature ships
# green. The peer is not reachable from every box, so binding to build_record
# (the function that produces the artifact the peer receives) is the closest
# real producer that runs everywhere.

def _load_pbp():
    import importlib.util
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("pbp_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pbp_rec(**kw):
    base = dict(author="zeta@world-a", channel="general", msg_type="note",
                text="payload-body", tags=[], reply_to="", seq=1,
                now="2026-08-27T00:00:00")
    base.update(kw)
    return _load_pbp().build_record(**base)


def test_g5_provenance_reaches_the_record_the_peer_receives():
    rec = _pbp_rec(origin_env="world-a")
    assert rec["origin_env"] == "world-a"
    assert rec["influence_chain"] == ["world-a"]
    assert rec["source_trace_ids"] == []
    assert rec["contributor_ids"] == []
    # The stamp must not clobber the payload it decorates.
    assert rec["text"] == "payload-body"
    assert rec["channel"] == "general"


def test_g5_negative_control_no_stamp_without_an_origin_env():
    rec = _pbp_rec(origin_env="")
    for k in ("origin_env", "influence_chain", "source_trace_ids",
              "contributor_ids"):
        assert k not in rec, (
            "%s present with no origin_env — the marker is unconditional, so "
            "the positive test above proves nothing" % k)


# ── outbound data-class gate () ───────────────────────────────────
# guard-4061 / guard-4525: the owner's personal address and credentials do not
# cross into a peer's board. The checker runs as a SUBPROCESS and BEFORE the peer
# backend is pinned, so its own gate-firings / override-ledger rows stay THIS
# world's. Credential-shaped strings are assembled at run time: no key-shaped
# literal sits in the repo.

_OWNER = "operator@example.com"
_SHAPED = "o***@e***.com"
_AWS = "".join(["AKI", "AQWERTYUIOPASDFGH"])


def _dc_env(tmp_path, peer_world=None):
    """Caller env for a data-class test: tmp world/meta so the checker's own rows can
    never reach a real store, and USER_EMAIL set to the owner."""
    (tmp_path / "w").mkdir(exist_ok=True)
    (tmp_path / "m").mkdir(exist_ok=True)
    env = {"USER_EMAIL": _OWNER, "MIND_WORLD": str(tmp_path / "w"), "MIND_META": str(tmp_path / "m")}
    if peer_world is not None:
        env["PEER_WORLD_ZDS_MIND"] = str(peer_world)
    return env


@pytest.mark.parametrize("secret", [_OWNER, _AWS], ids=["owner-address", "credential"])
def test_an_owner_address_or_a_credential_is_refused_and_nothing_reaches_the_peer(peer_world, tmp_path, secret):
    r = run(["--peer", "zds-mind", "--channel", "coordination"], stdin=f"note: reached {secret} today",
            env_extra=_dc_env(tmp_path, peer_world))
    assert r.returncode == EXIT_REFUSED, r.stderr
    assert "REFUSED by the outbound data-class gate" in r.stderr and secret not in r.stderr + r.stdout
    assert not (peer_world / "board" / "coordination.jsonl").exists()


def test_the_data_class_gate_runs_before_the_peer_is_resolved(tmp_path):
    """No PEER_WORLD_* at all: an unreachable peer is EXIT_UNREACHABLE for clean text, so
    EXIT_REFUSED here proves the check precedes the registry read and the backend pin."""
    r = run(["--peer", "zds-mind", "--channel", "coordination"], stdin=f"note {_OWNER}",
            env_extra=_dc_env(tmp_path))
    assert r.returncode == EXIT_REFUSED, r.stderr


def test_the_data_class_gate_is_called_before_the_backend_is_pinned():
    """Structural pin of the guard-955 ordering: the checker's firing and ledger rows are THIS
    world's, so it must run (as a subprocess, in the caller's env) before the peer pin."""
    body = SCRIPT.read_text(encoding="utf-8")
    body = body[body.index("def main("):]
    assert body.index("_data_class_gate(") < body.index("_force_peer_backend(")


def test_the_shaped_remedy_and_clean_text_pass_and_land_on_the_peer_board(peer_world, tmp_path):
    r = run(["--peer", "zds-mind", "--channel", "coordination"], stdin=f"note: reached {_SHAPED} today",
            env_extra=_dc_env(tmp_path, peer_world))
    assert r.returncode == EXIT_OK, r.stderr
    [line] = [l for l in (peer_world / "board" / "coordination.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert _SHAPED in json.loads(line)["text"]


def test_override_passes_the_gate_and_a_dry_run_still_refuses_without_it(peer_world, tmp_path):
    args = ["--peer", "zds-mind", "--channel", "coordination", "--dry-run"]
    env = _dc_env(tmp_path, peer_world)
    r = run(args, stdin=f"note: example id {_AWS}", env_extra=env)
    assert r.returncode == EXIT_REFUSED, r.stderr
    r = run(args + ["--override-data-class", "documentation example"], stdin=f"note: example id {_AWS}", env_extra=env)
    assert r.returncode == EXIT_OK, r.stderr
    assert "would_write" in r.stdout
    assert not list((tmp_path / "m").glob("gate-firings*")), "a dry run must leave no firing row"


def test_a_checker_that_cannot_run_fails_open_and_loud(monkeypatch, capsys):
    mod = _load_pbp()

    class _Done:
        def __init__(self, rc):
            self.returncode = rc

    for rc in (1, 2, 127):
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, _rc=rc, **k: _Done(_rc))
        mod._data_class_gate("text", "")  # returns: the post proceeds
        assert "posting UNCHECKED" in capsys.readouterr().err, rc

    def boom(*a, **k):
        raise OSError("no interpreter")
    monkeypatch.setattr(mod.subprocess, "run", boom)
    mod._data_class_gate("text", "")
    assert "posting UNCHECKED" in capsys.readouterr().err
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Done(mod.DATA_CLASS_REFUSAL_RC))
    with pytest.raises(SystemExit) as exc:
        mod._data_class_gate("text", "")
    assert exc.value.code == mod.EXIT_REFUSED
