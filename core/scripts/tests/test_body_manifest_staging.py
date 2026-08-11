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
    staged = state / "pending-body-merges"
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
    staged = state / "pending-body-merges"
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
    staged = state / "pending-body-merges"
    assert (staged / f"{SID}-wm-baseline.yaml").is_file()
    assert not any(p.name.endswith("-wm-baseline.yaml")
                   for p in staged.glob("*-wm.yaml"))


def test_local_body_does_not_stage(tmp_path, captured_puts):
    """Same-box Body: the reducer reads sessions/ directly, so staging would
    duplicate work and risk a double-merge."""
    sess, state = _build(tmp_path, remote_body=False)
    assert bm.close_body_on_genuine(SID, AGENT, tmp_path) == "marked"
    assert not (state / "pending-body-merges").exists()
    assert captured_puts == []


def test_missing_baseline_degrades_to_wm_plus_hash(tmp_path, captured_puts):
    """A missing baseline costs 3-way precision, never safety — the reducer's
    existing 2-way union+SUM fallback still applies."""
    sess, state = _build(tmp_path, baseline=False)
    assert bm.close_body_on_genuine(SID, AGENT, tmp_path) == "marked"
    staged = state / "pending-body-merges"
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
    assert (state / "pending-body-merges" / f"{SID}-wm.yaml").is_file()


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
    staged = tmp_path / "agents" / AGENT / "session" / "pending-body-merges"
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
