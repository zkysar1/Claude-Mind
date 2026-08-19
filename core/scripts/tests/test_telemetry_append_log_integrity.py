""": gate-firings telemetry integrity — the two write-path loss lanes.

Part 1 (storage_backend._bootstrap_env_defaults): a BARE subprocess reaching
get_backend() without the box's sourced env must self-resolve config from
PROJECT_ROOT/.env.local + _paths governed roots, so STORAGE_BACKEND=own-cloud
yields OwnCloudBackend instead of silently degrading to LocalBackend (lane A:
local-only appends S3 never sees) or raising into a never-raises caller
(lane B: _gate_log swallows -> record dropped).

Part 2 (owncloud_sync union-merge push): a merge-REGISTERED append-only store
(coordination_merge._HANDLERS, e.g. gate-firings.jsonl) must NEVER take a
blind whole-object PUT from the sync path — the If-Match fence passes on the
just-observed CURRENT etag, so a stale-TAIL local replaces the newer S3 head
(observed 2026-07-16T03:09:14 on meta/gate-firings.jsonl). The push, the
both-diverged, and the no-baseline own-cloud reconcile lanes all union-merge
instead; unregistered stores keep their pre-existing behavior.

Pure unit tests: FakeMergeBackend models S3 as a dict (same idiom as
test_owncloud_sync.py); the REAL coordination_merge registry + handler are
exercised so the union semantics pinned here are the production ones.
"""
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]  # core/scripts
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import owncloud_sync as _mod  # noqa: E402
import storage_backend  # noqa: E402
from storage_backend import FileStat  # noqa: E402


def _md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def _rec(ts: str, gate: str, extra=None) -> dict:
    r = {"schema_version": 1, "ts": ts, "gate_id": gate, "decision": "noop",
         "agent": "alpha", "session_id": "sid", "caller": "test"}
    if extra:
        r.update(extra)
    return r


def _dump(records) -> bytes:
    return b"".join(
        (json.dumps(r, ensure_ascii=True) + "\n").encode() for r in records)


@pytest.fixture(autouse=True)
def _redirect_merge_events_log(tmp_path, monkeypatch):
    """: the FakeMergeBackend success tests reach _try_merge_put,
    which now appends a durable merge-event line via _persist_merge_event.
    Redirect that write into tmp so no test touches the real
    mind_api/state/owncloud-merge-events.jsonl."""
    monkeypatch.setattr(
        _mod, "_merge_events_path",
        lambda: tmp_path / "owncloud-merge-events.jsonl")


# ---------------------------------------------------------------------------
# Part 1: _bootstrap_env_defaults
# ---------------------------------------------------------------------------

BOOT_KEYS = ("STORAGE_BACKEND", "STORAGE_S3_BUCKET", "MACHINE_ID")


def _delenv_with_restore(monkeypatch, key):
    """delenv that ALWAYS registers a teardown restore. A bare
    monkeypatch.delenv(k, raising=False) on an ABSENT var registers nothing —
    so a value the function-under-test then sets via os.environ.setdefault
    LEAKS past the test into the rest of the suite (cross-test pollution).
    setenv-first registers the original state (absent -> remove on teardown)."""
    monkeypatch.setenv(key, "_g2297_placeholder_")
    monkeypatch.delenv(key)


@pytest.fixture()
def _clean_boot_env(monkeypatch):
    for k in BOOT_KEYS:
        _delenv_with_restore(monkeypatch, k)
    yield monkeypatch


def _write_env_local(root: Path) -> Path:
    (root / ".env.local").write_text(
        "# comment line\n"
        "STORAGE_BACKEND=own-cloud\n"
        "STORAGE_S3_BUCKET='quoted-bucket'\n"
        "MACHINE_ID=cc-test\n"
        "not_a_key line\n",
        encoding="utf-8")
    return root


def test_bootstrap_skipped_under_pytest_by_default(_clean_boot_env, tmp_path):
    # PYTEST_CURRENT_TEST is set by pytest itself; without the explicit
    # allow flag the bootstrap must be a no-op (tests monkeypatch env and
    # must not inherit production config).
    _clean_boot_env.delenv("ENV_BOOTSTRAP_ALLOW_PYTEST", raising=False)
    storage_backend._bootstrap_env_defaults(root=_write_env_local(tmp_path))
    assert "STORAGE_BACKEND" not in os.environ


def test_bootstrap_skipped_at_collection_time(_clean_boot_env, tmp_path):
    # COLLECTION-time suppression: PYTEST_CURRENT_TEST is absent while pytest
    # imports test modules, so the guard must also key on `pytest in
    # sys.modules`. Without this, a module-import-time get_backend() call
    # side-loads production bucket/creds into the suite env, and a later test
    # monkeypatching only STORAGE_BACKEND=own-cloud builds a REAL production
    # backend whose cached instance poisons unrelated tests (2026-07-16: one
    # leaked backend broke 3 tests in the full-suite run; baseline was clean).
    _clean_boot_env.delenv("ENV_BOOTSTRAP_ALLOW_PYTEST", raising=False)
    _clean_boot_env.delenv("PYTEST_CURRENT_TEST", raising=False)
    storage_backend._bootstrap_env_defaults(root=_write_env_local(tmp_path))
    assert "STORAGE_BACKEND" not in os.environ


def test_bootstrap_fills_missing_keys_and_strips_quotes(_clean_boot_env, tmp_path):
    _clean_boot_env.setenv("ENV_BOOTSTRAP_ALLOW_PYTEST", "1")
    storage_backend._bootstrap_env_defaults(root=_write_env_local(tmp_path))
    assert os.environ["STORAGE_BACKEND"] == "own-cloud"
    assert os.environ["STORAGE_S3_BUCKET"] == "quoted-bucket"  # quotes stripped
    assert os.environ["MACHINE_ID"] == "cc-test"


def test_bootstrap_explicit_env_always_wins(_clean_boot_env, tmp_path):
    # guard-955: an explicit STORAGE_BACKEND=local pin (test runners, deliberate
    # overrides) must never be overridden by .env.local.
    _clean_boot_env.setenv("ENV_BOOTSTRAP_ALLOW_PYTEST", "1")
    _clean_boot_env.setenv("STORAGE_BACKEND", "local")
    storage_backend._bootstrap_env_defaults(root=_write_env_local(tmp_path))
    assert os.environ["STORAGE_BACKEND"] == "local"


def test_bootstrap_missing_env_local_is_noop(_clean_boot_env, tmp_path):
    _clean_boot_env.setenv("ENV_BOOTSTRAP_ALLOW_PYTEST", "1")
    storage_backend._bootstrap_env_defaults(root=tmp_path)  # no .env.local
    assert "STORAGE_BACKEND" not in os.environ


def test_bootstrap_defaults_governed_roots(_clean_boot_env, tmp_path):
    # Lane B: own-cloud ambient but no world/meta root vars ->
    # _resolve_root_map() raises -> _gate_log swallows -> record dropped.
    # The bootstrap must default MIND_WORLD/MIND_META from _paths.
    mp = _clean_boot_env
    mp.setenv("ENV_BOOTSTRAP_ALLOW_PYTEST", "1")
    for k in ("MIND_WORLD", "WORLD_PATH", "MIND_META", "META_PATH"):
        _delenv_with_restore(mp, k)
    storage_backend._bootstrap_env_defaults(root=tmp_path)
    assert os.environ.get("MIND_WORLD")
    assert os.environ.get("MIND_META")


def test_get_backend_invokes_bootstrap_once(monkeypatch):
    calls = []
    monkeypatch.setattr(storage_backend, "_bootstrap_env_defaults",
                        lambda root=None: calls.append(1))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    storage_backend.reset_backend_for_tests()
    try:
        storage_backend.get_backend()
        storage_backend.get_backend()  # cached — bootstrap must not re-run
        assert len(calls) == 1
    finally:
        storage_backend.reset_backend_for_tests()


# ---------------------------------------------------------------------------
# Part 2: _sync_one union-merge wiring
# ---------------------------------------------------------------------------

class FakeMergeBackend:
    """FakeBackend (test_owncloud_sync.py idiom) + the merge_put primitive,
    mirroring OwnCloudBackend.merge_put's contract: registry lookup via the
    REAL coordination_merge dispatch, merged bytes land on both fake-S3 and
    the local file, None when unregistered."""

    def __init__(self):
        self._roots = []
        self.s3 = {}            # str(path) -> bytes
        self.mirror_puts = []   # paths that took the blind whole-object PUT
        self.merge_puts = []    # paths that took the union-merge PUT
        self.refreshes = []     # paths pulled S3 -> local

    def stat(self, path):
        b = self.s3.get(str(path))
        if b is None:
            return None
        return FileStat(version='"' + _md5(b) + '"', size=len(b), mtime_ns=0)

    def mirror_put(self, path, content, *, expected_version=None):
        self.s3[str(path)] = content
        self.mirror_puts.append(str(path))

    def refresh(self, path):
        Path(path).write_bytes(self.s3[str(path)])
        self.refreshes.append(str(path))

    def merge_put(self, path, content):
        from owncloud_backend import _coordination_merge_handler
        handler = _coordination_merge_handler(path)
        if handler is None:
            return None
        merged = handler(content, self.s3.get(str(path), b""))
        self.s3[str(path)] = merged
        Path(path).write_bytes(merged)
        self.merge_puts.append(str(path))
        return object()  # WriteResult stand-in (truthy, non-None)


S3_RECS = [_rec("2026-07-16T02:44:00", "daemon-dup-gate"),
           _rec("2026-07-16T03:00:00", "claim-gate")]
SHARED = _rec("2026-07-15T23:00:00", "shared-baseline")
LOCAL_ONLY = _rec("2026-07-15T23:08:59", "local-tail")


def _incident_files(tmp_path, name="gate-firings.jsonl"):
    """The incident shape: S3 head holds newer records the local copy lacks;
    local holds a stale tail with records S3 lacks (LocalBackend-degraded
    appends). Returns (be, full, local_bytes)."""
    be = FakeMergeBackend()
    full = tmp_path / name
    local = _dump([SHARED, LOCAL_ONLY])
    full.write_bytes(local)
    be.s3[str(full)] = _dump([SHARED] + S3_RECS)
    return be, full, local


def _all_lines(data: bytes):
    return {ln for ln in data.decode().splitlines() if ln}


def test_push_lane_unions_registered_store(tmp_path):
    # The sync_file lane exactly: no baseline, multi_machine=False -> the old
    # code blind-pushed the stale tail over the newer S3 head.
    be, full, _ = _incident_files(tmp_path)
    stats = {"scanned": 1, "in_sync": 0, "pushed": 0, "would_push": 0,
             "conflicts": 0, "errors": 0, "push_paths": []}
    out = _mod._sync_one(be, full, dry_run=False, stats=stats,
                         multi_machine=False)
    assert be.merge_puts == [str(full)]
    assert be.mirror_puts == []
    assert stats.get("pushed_merged") == 1
    merged = be.s3[str(full)]
    # ZERO record loss: both sides' records present, S3 == local, baseline = merged md5
    expect = {json.dumps(r, ensure_ascii=True)
              for r in [SHARED, LOCAL_ONLY] + S3_RECS}
    assert _all_lines(merged) == expect
    assert full.read_bytes() == merged
    assert out == _md5(merged)


def test_push_lane_unregistered_store_keeps_mirror_put(tmp_path):
    be, full, local = _incident_files(tmp_path, name="not-registered.jsonl")
    stats = {"scanned": 1, "in_sync": 0, "pushed": 0, "would_push": 0,
             "conflicts": 0, "errors": 0, "push_paths": []}
    out = _mod._sync_one(be, full, dry_run=False, stats=stats,
                         multi_machine=False)
    assert be.merge_puts == []
    assert be.mirror_puts == [str(full)]
    assert stats["pushed"] == 1 and "pushed_merged" not in stats
    assert out == _md5(local)


def test_push_lane_torn_local_self_heals(tmp_path, monkeypatch, capsys):
    # : the cc-04 franken-local shape END-TO-END — the local copy
    # carries a TORN half-line tail (truncated append). Pre-fix the handler
    # raised inside merge_put -> _try_merge_put counted an error and skipped ->
    # the wedge persisted until a manual pre-filter (the  heal).
    # Now: pre-merge snapshot fires, the union proceeds, the torn line is
    # dropped LOUDLY, and every parseable record from both sides survives.
    be, full, local = _incident_files(tmp_path)
    full.write_bytes(local + b'{"ts": "2026-07-15T23:59:59", "gate_id": "to')
    snapshots = []
    # **kw, not a bare (p): _snapshot_before_pull takes a keyword-only `summary`
    # and the merge chokepoint now passes it (). A 1-arg stub pinned a
    # signature the real function never had — the local-wins call site has passed
    # `summary=` since ; these tests simply never reached it.
    monkeypatch.setattr(_mod, "_snapshot_before_pull",
                        lambda p, **kw: snapshots.append(str(p)))
    stats = {"scanned": 1, "in_sync": 0, "pushed": 0, "would_push": 0,
             "conflicts": 0, "errors": 0, "push_paths": []}
    out = _mod._sync_one(be, full, dry_run=False, stats=stats,
                         multi_machine=False)
    assert snapshots == [str(full)]          # preserve-before-drop fired first
    assert be.merge_puts == [str(full)]      # union lane, not error/skip
    assert stats["errors"] == 0
    assert stats.get("pushed_merged") == 1
    merged = be.s3[str(full)]
    expect = {json.dumps(r, ensure_ascii=True)
              for r in [SHARED, LOCAL_ONLY] + S3_RECS}
    assert _all_lines(merged) == expect      # zero parseable-record loss
    assert b'"to' not in merged              # torn fragment not re-emitted
    assert "dropped 1 torn line(s)" in capsys.readouterr().err
    assert out == _md5(merged)


def test_push_lane_s3_absent_stays_plain_put(tmp_path):
    # Nothing on S3 to merge with — a registered store still takes the plain
    # push for a brand-new object.
    be = FakeMergeBackend()
    full = tmp_path / "gate-firings.jsonl"
    local = _dump([SHARED])
    full.write_bytes(local)
    stats = {"scanned": 1, "in_sync": 0, "pushed": 0, "would_push": 0,
             "conflicts": 0, "errors": 0, "push_paths": []}
    out = _mod._sync_one(be, full, dry_run=False, stats=stats,
                         multi_machine=False)
    assert be.merge_puts == []
    assert be.mirror_puts == [str(full)]
    assert out == _md5(local)


def test_diverged_lane_merges_registered_store(tmp_path):
    # baseline set, BOTH sides moved -> old behavior: diverged_skipped forever.
    be, full, local = _incident_files(tmp_path)
    baseline = _md5(_dump([SHARED]))  # both sides appended since this
    stats = {"scanned": 1, "in_sync": 0, "pushed": 0, "would_push": 0,
             "conflicts": 0, "errors": 0, "push_paths": []}
    out = _mod._sync_one(be, full, dry_run=False, stats=stats,
                         baseline_md5=baseline, multi_machine=True)
    assert stats.get("diverged_merged") == 1
    assert "conflict_paths" not in stats and stats.get("diverged_skipped", 0) == 0
    assert _all_lines(be.s3[str(full)]) == {
        json.dumps(r, ensure_ascii=True)
        for r in [SHARED, LOCAL_ONLY] + S3_RECS}
    assert out == _md5(be.s3[str(full)])


def test_diverged_lane_unregistered_still_skips(tmp_path):
    be, full, _ = _incident_files(tmp_path, name="not-registered.jsonl")
    baseline = _md5(_dump([SHARED]))
    stats = {"scanned": 1, "in_sync": 0, "pushed": 0, "would_push": 0,
             "conflicts": 0, "errors": 0, "push_paths": []}
    out = _mod._sync_one(be, full, dry_run=False, stats=stats,
                         baseline_md5=baseline, multi_machine=True)
    assert out is None
    assert stats["diverged_skipped"] == 1
    assert stats["conflict_paths"] == [str(full)]
    assert be.merge_puts == [] and be.mirror_puts == []


def test_nobaseline_owncloud_lane_merges_registered_store(tmp_path, monkeypatch):
    # No baseline + multi-machine + own-cloud authority: the old deterministic
    # reconcile PULLED S3 wholesale, dropping the locally-authored tail (the
    # cc-02 franken-copy heal path). Registered stores must union instead.
    monkeypatch.setattr(_mod, "_snapshot_before_pull", lambda p, **kw: None)
    be, full, _ = _incident_files(tmp_path)
    stats = {"scanned": 1, "in_sync": 0, "pushed": 0, "would_push": 0,
             "conflicts": 0, "errors": 0, "push_paths": []}
    out = _mod._sync_one(be, full, dry_run=False, stats=stats,
                         multi_machine=True, own_cloud_authority=True)
    assert stats.get("nobaseline_merged") == 1
    assert be.refreshes == []  # union replaced the wholesale pull
    assert json.dumps(LOCAL_ONLY, ensure_ascii=True) in _all_lines(be.s3[str(full)])
    assert out == _md5(be.s3[str(full)])


def test_nobaseline_owncloud_lane_unregistered_still_pulls(tmp_path, monkeypatch):
    monkeypatch.setattr(_mod, "_snapshot_before_pull", lambda p, **kw: None)
    be, full, _ = _incident_files(tmp_path, name="not-registered.jsonl")
    stats = {"scanned": 1, "in_sync": 0, "pushed": 0, "would_push": 0,
             "conflicts": 0, "errors": 0, "push_paths": []}
    out = _mod._sync_one(be, full, dry_run=False, stats=stats,
                         multi_machine=True, own_cloud_authority=True)
    assert stats.get("nobaseline_reconciled") == 1
    assert be.refreshes == [str(full)]
    assert full.read_bytes() == be.s3[str(full)]  # S3 adopted wholesale
    assert out == _md5(be.s3[str(full)])


def test_merge_put_failure_counts_error_not_blind_push(tmp_path):
    # A failed union must NOT fall back to the blind PUT (that would be the
    # clobber this change removes) — count the error, skip, retry next sweep.
    be, full, _ = _incident_files(tmp_path)

    def _boom(path, content):
        raise RuntimeError("CAS exhausted")
    be.merge_put = _boom
    stats = {"scanned": 1, "in_sync": 0, "pushed": 0, "would_push": 0,
             "conflicts": 0, "errors": 0, "push_paths": []}
    out = _mod._sync_one(be, full, dry_run=False, stats=stats,
                         multi_machine=False)
    assert out is None
    assert stats["errors"] == 1
    assert be.mirror_puts == []


def test_backend_without_merge_put_preserves_legacy_behavior(tmp_path):
    # Belt-and-braces: a backend lacking the primitive (older
    # owncloud_backend.py, or any duck-typed test double) keeps the exact
    # pre-change push behavior for every store — _try_merge_put returns the
    # not-applicable sentinel on the missing attribute alone.
    class LegacyBackend(FakeMergeBackend):
        merge_put = None  # getattr(be, "merge_put", None) -> None -> _MERGE_NA

    be = LegacyBackend()
    full = tmp_path / "gate-firings.jsonl"
    local = _dump([SHARED, LOCAL_ONLY])
    full.write_bytes(local)
    be.s3[str(full)] = _dump([SHARED] + S3_RECS)
    stats = {"scanned": 1, "in_sync": 0, "pushed": 0, "would_push": 0,
             "conflicts": 0, "errors": 0, "push_paths": []}
    out = _mod._sync_one(be, full, dry_run=False, stats=stats,
                         multi_machine=False)
    assert be.merge_puts == []
    assert be.mirror_puts == [str(full)]  # legacy blind push preserved
    assert out == _md5(local)


# ---------------------------------------------------------------------------
# : the merge lane's .history label must name the lane it took.
#
# These live HERE rather than beside the other _snapshot_before_pull tests in
# test_owncloud_sync.py because they need a merge-REGISTERED store, and
# FakeMergeBackend / _incident_files above are the only fixture in the tree that
# provides one. Duplicating a merge backend to keep the topical filing tidy
# would mean testing the label against a second implementation of the thing.
#
# WHAT WENT WRONG. _try_merge_put called _snapshot_before_pull(full) with no
# summary, so every merge inherited the DEFAULT label -- "pre-pull snapshot:
# S3-authoritative overwrite of no-baseline local". On a merge not one clause of
# that is true. It cost a full misdiagnosis:  read the note out of file
# history (correctly preferring evidence over inference), and its mechanism
# section, its framing and its first question all followed from a sentence that
# described a branch the code never took.
#
# THE ASSERTION THAT MATTERS IS THE ABSENCE. A presence-only check ("the label
# mentions the lane") passes just as happily on a label that ALSO still claims a
# no-baseline overwrite, which is the pre-fix string with a prefix bolted on.
_DEFAULT_SNAPSHOT_LABEL_MARKER = "no-baseline"


def _capture_snapshot_summaries(monkeypatch):
    """Record (path, summary) at the seam. summary is None when the caller
    passed none, which is how the genuine no-baseline lanes reach the default."""
    seen = []
    monkeypatch.setattr(
        _mod, "_snapshot_before_pull",
        lambda p, *, summary=None: seen.append((str(p), summary)))
    return seen


@pytest.mark.parametrize(
    "sync_kwargs,expected_lane",
    [
        ({"multi_machine": False}, "pushed_merged"),
        ({"multi_machine": True, "own_cloud_authority": True},
         "nobaseline_merged"),
    ],
    ids=["pushed_merged", "nobaseline_merged"],
)
def test_merge_lane_snapshot_label_names_the_lane_and_not_a_nobaseline_overwrite(
    tmp_path, monkeypatch, sync_kwargs, expected_lane
):
    seen = _capture_snapshot_summaries(monkeypatch)
    be, full, _ = _incident_files(tmp_path)
    stats = {"scanned": 1, "in_sync": 0, "pushed": 0, "would_push": 0,
             "conflicts": 0, "errors": 0, "push_paths": []}
    _mod._sync_one(be, full, dry_run=False, stats=stats, **sync_kwargs)

    assert stats.get(expected_lane) == 1, (
        f"the {expected_lane} lane did not run, so this test proves nothing "
        f"about its label; stats={stats}")
    assert seen, "no snapshot was taken on the merge path at all"
    _path, summary = seen[-1]

    assert summary is not None, (
        "the merge lane passed NO summary, so it inherits the default "
        "'S3-authoritative overwrite of no-baseline local' label -- the exact "
        "sentence that misrouted g-115-5125")
    assert _DEFAULT_SNAPSHOT_LABEL_MARKER not in summary, (
        f"a merge wrote a history note claiming a no-baseline overwrite: {summary!r}")
    assert f"lane={expected_lane}" in summary, (
        f"the note does not name the lane actually taken; got {summary!r}")


def test_the_default_label_really_does_say_nobaseline(tmp_path, monkeypatch):
    """Positive control for the absence assertion above.

    Without this, a future edit to the default text would make
    `"no-baseline" not in summary` pass for a reason that has nothing to do with
    the merge lane being labelled correctly -- an absence test whose string can
    no longer appear anywhere is vacuous, and it would stay green through a full
    revert of the fix.
    """
    captured = {}
    monkeypatch.setattr(
        _mod, "_fileops_save_history_probe", lambda *a, **k: None, raising=False)

    import types
    fake_fileops = types.ModuleType("_fileops")
    fake_fileops.resolve_base_dir = lambda full: Path(tmp_path)
    fake_fileops.save_history = (
        lambda full, base, source, summary=None: captured.update(summary=summary))
    monkeypatch.setitem(sys.modules, "_fileops", fake_fileops)

    f = tmp_path / "probe.jsonl"
    f.write_bytes(b"x\n")
    _mod._snapshot_before_pull(f)  # no summary -> the default

    assert _DEFAULT_SNAPSHOT_LABEL_MARKER in (captured.get("summary") or ""), (
        f"the default label no longer contains "
        f"{_DEFAULT_SNAPSHOT_LABEL_MARKER!r}, so the merge-lane absence "
        f"assertion above is now vacuous: {captured!r}")


def test_genuine_nobaseline_pull_still_takes_the_default_label(tmp_path, monkeypatch):
    """The fix must not over-apply: the S3-authoritative-at-bind PULL really IS
    a no-baseline overwrite, so it keeps the default label. Relabelling every
    call site would trade one wrong note for another."""
    seen = _capture_snapshot_summaries(monkeypatch)
    be = FakeMergeBackend()
    f = tmp_path / "not-registered.md"
    f.write_bytes(b"authored-local")
    be.s3[str(f)] = b"s3-object"
    stats = {"scanned": 1, "pulled": 0, "would_pull": 0, "skipped": 0,
             "errors": 0, "in_sync": 0}
    _mod._pull_one(be, f, dry_run=False, stats=stats, baseline_md5=None)

    assert seen, "the no-baseline pull took no snapshot"
    assert seen[-1][1] is None, (
        f"the genuine no-baseline pull lane now passes an explicit summary "
        f"({seen[-1][1]!r}); it should inherit the default, which is accurate there")
