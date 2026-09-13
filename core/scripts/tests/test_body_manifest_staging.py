"""-b: cross-box Body WM transport — staging + explicit push.

Covers BOTH required fixes plus the precondition found by fresh-eyes on
g-306-122:

  FIX 1  close_body_on_genuine STAGES wm + baseline + hash (not merely marks)
  FIX 2  each staged file is EXPLICITLY pushed through the storage backend
  PRE    a malformed manifest degrades cleanly AND consumes the sentinel

The sentinel assertions are the load-bearing ones (guard-1943): a test that
only checks "returned without raising" passes while the permanent turn-end
re-fire wedge remains. Every close-path case here asserts the sentinel state.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CORE))

_spec = importlib.util.spec_from_file_location("body_manifest", CORE / "body-manifest.py")
bm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bm)

SID = "44b5b26f-7921-4229-b1df-93236dae1c34"
AGENT = "alpha"


def _build(tmp_path, *, remote_body=True, manifest_body=None, baseline=True,
           forked_hash="abc123", wm_body="slots: {}\n"):
    """Materialize a worker Body mid-genuine-close under a tmp project root."""
    sess = tmp_path / "agents" / AGENT / "sessions" / SID
    sess.mkdir(parents=True)
    state = tmp_path / "agents" / AGENT / "session"
    state.mkdir(parents=True)
    if manifest_body is None:
        lines = [
            f"unitKey: '{SID}'", f"mindKey: '{AGENT}'", "env_id: 'local'",
            "role: 'worker'", "body_state: 'active'",
            f"forked_wm_hash: '{forked_hash}'" if forked_hash else "forked_wm_hash: null",
            f"remote_body: {'true' if remote_body else 'false'}",
        ]
        manifest_body = "\n".join(lines) + "\n"
    (sess / "body-manifest.yaml").write_text(manifest_body, encoding="utf-8")
    # BYTES, not text (). This is the one fixture whose exact bytes a
    # test asserts (test_pushed_bytes_match_staged_bytes compares the pushed
    # payload against a literal b"...\n"). write_text opens in TEXT mode, so on
    # Windows Python translates \n to \r\n on the way to disk; the staging
    # pipeline then carries those bytes faithfully — it is working correctly —
    # and the assertion fails against an LF literal. The red accused the
    # pipeline of corrupting bytes when the fixture had never written the bytes
    # it claimed. Deliberately NOT applied to the siblings above and below:
    # those are YAML-parsed or existence-checked (both CRLF-tolerant), and
    # forcing LF there would make them diverge from what production's own
    # text-mode writes produce on Windows, which is the shape they exist to
    # imitate.
    (sess / "working-memory.yaml").write_bytes(wm_body.encode("utf-8"))
    if baseline:
        (sess / "forked-wm-baseline.yaml").write_text("slots: {}\n", encoding="utf-8")
    (sess / "body-closing").write_text("", encoding="utf-8")
    return sess, state


@pytest.fixture(autouse=True)
def _hermetic_world(tmp_path, monkeypatch):
    """Point the staged-WM root at a TMP world for every test in this file.

    Load-bearing, not tidiness (g-115-9750, inherited verbatim from the
    g-306-420 carrier fixture). The staged triple now resolves through
    `_paths.WORLD_DIR` via `bm.world_staged_dir`, so without this fixture a
    producer test would stage into the LIVE `world/` — and on an own-cloud box
    that is the guard-955 production-key collision class, from a test that
    looks hermetic because every path it constructs itself is under tmp_path.

    Patching the module ATTRIBUTE works because `world_staged_dir` does its
    `from _paths import WORLD_DIR` inside the function body; a module-level
    import there would have frozen the real path at collection time and this
    fixture would silently do nothing.
    """
    import _paths
    w = tmp_path / "world"
    w.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_paths, "WORLD_DIR", w, raising=False)
    return w


def _staged_dir(tmp_path):
    """Where close_body_on_genuine stages now (): world-rooted."""
    return tmp_path / "world" / "body-staged-wm" / AGENT


@pytest.fixture
def captured_puts(monkeypatch):
    """Capture every explicit backend push _stage_and_push performs."""
    pushed = []

    class _FakeBackend:
        def write_bytes(self, path, content):
            pushed.append((Path(path).name, content))

    mod = type(sys)("storage_backend")
    mod.get_backend = lambda: _FakeBackend()
    monkeypatch.setitem(sys.modules, "storage_backend", mod)
    return pushed


# ---------------------------------------------------------------- FIX 1

def test_remote_body_stages_wm_baseline_and_hash(tmp_path, captured_puts):
    sess, state = _build(tmp_path)
    assert bm.close_body_on_genuine(SID, AGENT, tmp_path) == "marked"
    staged = _staged_dir(tmp_path)
    assert (staged / f"{SID}-wm.yaml").read_text(encoding="utf-8") == "slots: {}\n"
    assert (staged / f"{SID}-wm-baseline.yaml").is_file()
    # Hash sidecar carries the manifest's forked_wm_hash, newline-terminated;
    # body-merge reads it via _read_text_strip so the newline is harmless.
    assert (staged / f"{SID}-wm.hash").read_text(encoding="utf-8").strip() == "abc123"


def test_staged_names_match_what_body_merge_derives(tmp_path, captured_puts):
    """The reducer globs '*-wm.yaml' and strips that suffix to get unitKey.

    Pinning it here means a rename on either side reddens instead of silently
    stranding every cross-box Body (the reader would simply glob zero files).
    """
    sess, state = _build(tmp_path)
    bm.close_body_on_genuine(SID, AGENT, tmp_path)
    staged = _staged_dir(tmp_path)
    hits = sorted(p.name for p in staged.glob("*-wm.yaml"))
    assert hits == [f"{SID}-wm.yaml"]
    assert hits[0][: -len("-wm.yaml")] == SID


def test_baseline_is_not_mis_consumed_as_a_body_wm(tmp_path, captured_puts):
    """'-wm-baseline.yaml' must NOT match the reader's '*-wm.yaml' glob.

    If it did, the reducer would merge the fork-time ancestor as if it were
    the diverged Body WM — silently reverting that Body's work.
    """
    sess, state = _build(tmp_path)
    bm.close_body_on_genuine(SID, AGENT, tmp_path)
    staged = _staged_dir(tmp_path)
    assert (staged / f"{SID}-wm-baseline.yaml").is_file()
    assert not any(p.name.endswith("-wm-baseline.yaml")
                   for p in staged.glob("*-wm.yaml"))


def test_local_body_does_not_stage(tmp_path, captured_puts):
    """Same-box Body: the reducer reads sessions/ directly, so staging would
    duplicate work and risk a double-merge."""
    sess, state = _build(tmp_path, remote_body=False)
    assert bm.close_body_on_genuine(SID, AGENT, tmp_path) == "marked"
    assert not _staged_dir(tmp_path).exists()
    assert captured_puts == []


def test_missing_baseline_degrades_to_wm_plus_hash(tmp_path, captured_puts):
    """A missing baseline costs 3-way precision, never safety — the reducer's
    existing 2-way union+SUM fallback still applies."""
    sess, state = _build(tmp_path, baseline=False)
    assert bm.close_body_on_genuine(SID, AGENT, tmp_path) == "marked"
    staged = _staged_dir(tmp_path)
    assert (staged / f"{SID}-wm.yaml").is_file()
    assert (staged / f"{SID}-wm.hash").is_file()
    assert not (staged / f"{SID}-wm-baseline.yaml").exists()


# ---------------------------------------------------------------- FIX 2

def test_every_staged_file_is_explicitly_pushed(tmp_path, captured_puts):
    """The push is the whole fix: a worker box holds no DDB claim, so the
    periodic sweep AND owncloud-flush both push zero agent dirs."""
    sess, state = _build(tmp_path)
    bm.close_body_on_genuine(SID, AGENT, tmp_path)
    assert sorted(n for n, _ in captured_puts) == sorted([
        f"{SID}-wm.yaml", f"{SID}-wm-baseline.yaml", f"{SID}-wm.hash"])


def test_pushed_bytes_match_staged_bytes(tmp_path, captured_puts):
    sess, state = _build(tmp_path, wm_body="slots: {a: 1}\n")
    bm.close_body_on_genuine(SID, AGENT, tmp_path)
    by_name = dict(captured_puts)
    assert by_name[f"{SID}-wm.yaml"] == b"slots: {a: 1}\n"


def test_push_failure_is_visible_and_still_closes(tmp_path, monkeypatch):
    """A failed push must NOT read as success, must NOT raise out of the
    stop-hook, and must still consume the sentinel (no re-fire wedge)."""
    class _Boom:
        def write_bytes(self, path, content):
            raise RuntimeError("s3 unreachable")

    mod = type(sys)("storage_backend")
    mod.get_backend = lambda: _Boom()
    monkeypatch.setitem(sys.modules, "storage_backend", mod)

    sess, state = _build(tmp_path)
    assert bm.close_body_on_genuine(SID, AGENT, tmp_path) == "marked-push-failed"
    # The close really happened — state transitioned, sentinel consumed.
    assert bm.read_manifest(SID, AGENT, tmp_path)["body_state"] == "closed-pending-merge"
    assert not (sess / "body-closing").exists()
    # ...and the bytes are on local disk for recovery.
    assert (_staged_dir(tmp_path) / f"{SID}-wm.yaml").is_file()


# -------------------- FIX 2 shared push (cleanup-stale-bindings path)

def test_push_staged_files_pushes_bash_staged_files(tmp_path, captured_puts):
    """cleanup-stale-bindings.sh stages in bash then shells out to
    `push-staged`. Same push implementation, so a fix to one covers both."""
    staged = tmp_path / "pending-body-merges"
    staged.mkdir(parents=True)
    (staged / f"{SID}-wm.yaml").write_text("slots: {}\n", encoding="utf-8")
    (staged / f"{SID}-wm-baseline.yaml").write_text("slots: {}\n", encoding="utf-8")
    (staged / f"{SID}-wm.hash").write_text("deadbeef", encoding="utf-8")
    assert bm.push_staged_files(staged, SID) is True
    assert sorted(n for n, _ in captured_puts) == sorted([
        f"{SID}-wm.yaml", f"{SID}-wm-baseline.yaml", f"{SID}-wm.hash"])


def test_push_staged_skips_absent_files_without_failing(tmp_path, captured_puts):
    """A crash-preserve that staged no baseline must not report failure."""
    staged = tmp_path / "pending-body-merges"
    staged.mkdir(parents=True)
    (staged / f"{SID}-wm.yaml").write_text("slots: {}\n", encoding="utf-8")
    assert bm.push_staged_files(staged, SID) is True
    assert [n for n, _ in captured_puts] == [f"{SID}-wm.yaml"]


def test_push_staged_cli_exits_4_when_transport_down(tmp_path, monkeypatch):
    """Exit 4 is distinct from validation(2)/io(3) so the bash caller can tell
    'transport down' from 'nothing to do'."""
    class _Boom:
        def write_bytes(self, path, content):
            raise RuntimeError("s3 unreachable")

    mod = type(sys)("storage_backend")
    mod.get_backend = lambda: _Boom()
    monkeypatch.setitem(sys.modules, "storage_backend", mod)

    _build(tmp_path)
    # : the CLI pushes from the WORLD-rooted dir now, so seed there.
    staged = _staged_dir(tmp_path)
    staged.mkdir(parents=True)
    (staged / f"{SID}-wm.yaml").write_text("slots: {}\n", encoding="utf-8")
    monkeypatch.setattr(bm, "_project_root", lambda: tmp_path)
    assert bm.main(["push-staged", "--sid", SID, "--agent", AGENT]) == 4


# ------------------------------------------------- PRECONDITION (sentinel)

def test_malformed_manifest_consumes_sentinel(tmp_path):
    """guard-1943: assert the SENTINEL, not merely that the error was caught.

    Pre-fix the YAMLError escaped close_body_on_genuine entirely, so the
    sentinel survived and the turn-end condition re-fired for that Body at
    every subsequent turn-end, permanently.
    """
    sess, state = _build(tmp_path, manifest_body="unitKey: 'x'\nmachine_id: box: 1\n")
    assert bm.close_body_on_genuine(SID, AGENT, tmp_path) == "bad-manifest"
    assert not (sess / "body-closing").exists()


def test_manifest_parse_error_is_a_valueerror(tmp_path):
    """Subclassing ValueError is what lets main()'s existing exit-2 path catch
    a malformed manifest WITHOUT importing yaml at module level."""
    assert issubclass(bm.ManifestParseError, ValueError)
    sess, state = _build(tmp_path, manifest_body="machine_id: box: 1\n")
    with pytest.raises(bm.ManifestParseError):
        bm.read_manifest(SID, AGENT, tmp_path)


def test_cli_malformed_manifest_exits_2_not_traceback(tmp_path, monkeypatch):
    """The module docstring promises a non-zero exit + stderr diagnostic."""
    _build(tmp_path, manifest_body="machine_id: box: 1\n")
    monkeypatch.setattr(bm, "_project_root", lambda: tmp_path)
    assert bm.main(["read", "--sid", SID, "--agent", AGENT]) == 2


def test_sentinel_consumed_on_every_close_branch(tmp_path, captured_puts):
    """The docstring's central invariant, exercised across the noop branches.

    'marked' / 'marked-push-failed' / 'bad-manifest' are covered above; this
    pins the two remaining genuine-close branches so a future edit cannot
    reintroduce a surviving sentinel on any of them.
    """
    # not-active branch
    sess, state = _build(tmp_path / "notactive")
    m = sess / "body-manifest.yaml"
    m.write_text(m.read_text(encoding="utf-8").replace("'active'", "'merged'"),
                 encoding="utf-8")
    assert bm.close_body_on_genuine(SID, AGENT, tmp_path / "notactive") == "not-active"
    assert not (sess / "body-closing").exists()
    # no-manifest branch
    sess2, _ = _build(tmp_path / "nomanifest")
    (sess2 / "body-manifest.yaml").unlink()
    assert bm.close_body_on_genuine(SID, AGENT, tmp_path / "nomanifest") == "no-manifest"
    assert not (sess2 / "body-closing").exists()


# ---------------------------------------------------------------- 

def test_staged_destination_is_out_of_the_claim_fence_reach(tmp_path):
    """The whole point of the move, pinned by the FENCE'S OWN predicate.

    `owncloud_backend` decides whether a write is claim-fenced by resolving the
    path and asking `relative_to(agents_root())`: success means the write is
    under some agent's dir and is refused unless this box holds that agent's
    live runner claim; a `ValueError` means `_agent = None` and the fence is
    never consulted at all. A worker Body never holds the claim, so "outside
    `agents_root()`" is precisely the property that makes `push_staged_files`
    able to succeed from a worker box.

    Asserting a string shape ("the path contains 'world'") would pass just as
    happily on a path that still sat under the agent tree. Running the real
    predicate is the only assertion that tracks the real gate.

    The legacy destination is the POSITIVE CONTROL (guard-2298): it must still
    resolve INSIDE `agents_root()`, or this test would pass for the trivial
    reason that the predicate never matches anything.
    """
    from _paths import agents_root
    root = Path(agents_root()).resolve()

    world_staged = bm.world_staged_dir(tmp_path / "agents" / AGENT).resolve()
    with pytest.raises(ValueError):
        world_staged.relative_to(root)

    # POSITIVE CONTROL — the destination this change moved AWAY from is inside
    # the fence, so the predicate above genuinely discriminates.
    legacy = (root / AGENT / "session" / "pending-body-merges").resolve()
    assert legacy.relative_to(root).parts[0] == AGENT


def test_world_staged_dir_keys_on_agent_name_not_path_depth(tmp_path):
    """Derived from the agent NAME, so a caller passing any agent dir shape
    lands in the same per-agent bucket. Pins the contract `_consume_staged`
    relies on when it calls `bm.world_staged_dir(state_dir.parent)` — the
    reducer and the producer must compute the identical directory or the
    union scan silently reads an empty one.
    """
    a = bm.world_staged_dir(tmp_path / "agents" / AGENT)
    b = bm.world_staged_dir(Path("/somewhere/else/entirely") / AGENT)
    assert a.name == b.name == AGENT
    assert a.parent.name == b.parent.name == "body-staged-wm"
    # and it honours an explicit world_dir override (the seam the tests use)
    c = bm.world_staged_dir(tmp_path / "agents" / AGENT, world_dir=tmp_path / "w2")
    assert c == tmp_path / "w2" / "body-staged-wm" / AGENT


# ------------------------------------- : the bash/CLI staging bridge

def _legacy_dir(tmp_path):
    """Where cleanup-stale-bindings.sh stages, in pure bash (IRREDUCIBLY
    LOCAL, so it cannot resolve WORLD_DIR and still writes here)."""
    return tmp_path / "agents" / AGENT / "session" / "pending-body-merges"


def _seed_legacy(tmp_path, sid=SID, *, wm=True, baseline=True, hash_=True):
    d = _legacy_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    if baseline:
        (d / f"{sid}-wm-baseline.yaml").write_text("slots: {}\n", encoding="utf-8")
    if hash_:
        (d / f"{sid}-wm.hash").write_text("deadbeef", encoding="utf-8")
    if wm:
        (d / f"{sid}-wm.yaml").write_text("slots: {bash: staged}\n", encoding="utf-8")
    return d


def test_cli_push_staged_transports_what_the_bash_staged(tmp_path, captured_puts,
                                                         monkeypatch):
    """THE DEFECT PIN, and the one the pre-existing tests could not catch.

    `test_push_staged_files_pushes_bash_staged_files` hands `push_staged_files`
    a directory of its own making, so it pins the push IMPLEMENTATION and says
    nothing about which directory the CLI CHOOSES (guard-1943: pinning the
    writer says nothing about the wiring). When the CLI's destination moved
    world-rooted and the bash kept staging in the agent tree, that test stayed
    green while the crash-preserve path transported ZERO BYTES — and because
    `push_staged_files` skips absent files and returns True, the bash `||`
    warning never fired either. Silent success is worse than the loud
    NoClaimError it replaced.

    This test goes through `main(["push-staged", ...])` with the files seeded
    where the BASH actually puts them, so the two halves must agree.
    """
    _build(tmp_path)
    _seed_legacy(tmp_path)
    monkeypatch.setattr(bm, "_project_root", lambda: tmp_path)
    assert bm.main(["push-staged", "--sid", SID, "--agent", AGENT]) == 0
    assert sorted(n for n, _ in captured_puts) == sorted([
        f"{SID}-wm.yaml", f"{SID}-wm-baseline.yaml", f"{SID}-wm.hash"]), \
        "the CLI pushed a different set than the bash staged"
    # and the bytes that went out are the bytes the bash wrote
    sent = dict(captured_puts)
    assert sent[f"{SID}-wm.yaml"] == b"slots: {bash: staged}\n"


def test_relocate_copies_and_never_moves(tmp_path):
    """COPY, not move: the legacy triple is some Bodies' SOLE SURVIVING TRACE
    (archive-before-delete.md), and the duplicate is retired later by
    `_consume_staged`'s shadowed-duplicate branch on the same disposition as
    the copy actually merged."""
    _build(tmp_path)
    legacy = _seed_legacy(tmp_path)
    state_dir = tmp_path / "agents" / AGENT / "session"
    copied = bm.relocate_legacy_staging(state_dir, SID)
    assert sorted(copied) == sorted([
        f"{SID}-wm.yaml", f"{SID}-wm-baseline.yaml", f"{SID}-wm.hash"])
    world = _staged_dir(tmp_path)
    for n in copied:
        assert (world / n).read_bytes() == (legacy / n).read_bytes()
        assert (legacy / n).is_file(), "the legacy copy must SURVIVE the relocation"


def test_relocate_writes_the_trigger_last(tmp_path):
    """`_consume_staged` globs '*-wm.yaml', so the WM is the CONSUMER'S
    TRIGGER. A trigger visible before its sidecars is silently consumed down a
    degraded path (no baseline -> 2-way double-count; no hash -> the
    never-diverged no-op is skipped) — no error, no log, wrong number."""
    _build(tmp_path)
    _seed_legacy(tmp_path)
    state_dir = tmp_path / "agents" / AGENT / "session"
    copied = bm.relocate_legacy_staging(state_dir, SID)
    assert copied[-1] == f"{SID}-wm.yaml", \
        f"the -wm.yaml TRIGGER must be written LAST; observed order {copied}"


def test_relocate_never_overwrites_the_world_copy(tmp_path):
    """close_body_on_genuine stages the authoritative copy world-rooted and is
    the fresher writer; a stale legacy duplicate must not clobber it."""
    _build(tmp_path)
    _seed_legacy(tmp_path)
    world = _staged_dir(tmp_path)
    world.mkdir(parents=True, exist_ok=True)
    (world / f"{SID}-wm.yaml").write_text("slots: {genuine: close}\n", encoding="utf-8")
    state_dir = tmp_path / "agents" / AGENT / "session"
    copied = bm.relocate_legacy_staging(state_dir, SID)
    assert f"{SID}-wm.yaml" not in copied
    assert (world / f"{SID}-wm.yaml").read_text(encoding="utf-8") == \
        "slots: {genuine: close}\n"


def test_relocate_is_a_noop_without_a_legacy_dir(tmp_path):
    """The common case on a box that never staged under the old code."""
    _build(tmp_path)
    state_dir = tmp_path / "agents" / AGENT / "session"
    assert bm.relocate_legacy_staging(state_dir, SID) == []
