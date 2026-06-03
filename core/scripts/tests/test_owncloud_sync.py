"""B15: owncloud-sync mirror sweep — exclusion policy + sync-decision + manifest.

Pure unit test with a FakeBackend (no S3, no moto): exercises _is_machine_local,
_etag_matches, _sync_one (in-sync skip / push-on-differ / push-on-absent /
conflict), and _sweep (dir pruning, dry-run plan, real push, manifest skip on
re-run). The fenced-PUT mechanics of the real backend are covered by moto in
test_owncloud_backend.py (test_mirror_put_*).

H4 (machine-2 gate) coverage: MIND_OWNED_AGENTS agent-dir scoping (H4a) and the
content-baseline stale-cache / conflict classifier (H4b) that stops a second
machine from pushing a stale cache over a peer's newer S3 bytes — including the
--full clobber path the If-Match fence alone does not cover."""
import hashlib
import os
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]  # core/scripts
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import owncloud_sync as _mod  # noqa: E402 — importable module (the daemon imports it too)
from owncloud_backend import ConflictError  # noqa: E402
from storage_backend import FileStat  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_multimachine_env(monkeypatch):
    """H4 reads MIND_OWNED_AGENTS / MIND_MULTI_MACHINE from the environment.
    Default every test to the single-machine (unset) state so a runner shell
    that has them set cannot perturb results; tests exercising multi-machine
    behavior set them explicitly inside the test body."""
    monkeypatch.delenv("MIND_OWNED_AGENTS", raising=False)
    monkeypatch.delenv("MIND_MULTI_MACHINE", raising=False)


def _md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


class FakeBackend:
    """Models S3 as a dict keyed by str(path), with a quoted ETag = md5(content),
    and a fence on mirror_put (expected_version must match current ETag)."""
    def __init__(self, roots):
        self._roots = roots
        self.s3 = {}          # str(path) -> bytes
        self.puts = []        # str(path) pushed

    def stat(self, path):
        b = self.s3.get(str(path))
        if b is None:
            return None
        return FileStat(version='"' + _md5(b) + '"', size=len(b), mtime_ns=0)

    def mirror_put(self, path, content, *, expected_version=None):
        cur = self.s3.get(str(path))
        cur_etag = ('"' + _md5(cur) + '"') if cur is not None else None
        if expected_version is not None and expected_version != cur_etag:
            raise ConflictError(f"stale fence for {path}")
        self.s3[str(path)] = content
        self.puts.append(str(path))

    def refresh(self, path):
        # Models OwnCloudBackend.refresh: GET S3 -> materialize the local file.
        # No-op when absent remotely (matches _refresh returning the local path).
        b = self.s3.get(str(path))
        if b is None:
            return
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b)

    def list_dir(self, path):
        """Immediate child names under `path` among S3 keys — models the real
        backend's ListObjects+Delimiter='/' (owncloud_backend.list_dir). Used by
        pull_temp's prefix walk over the dynamically-named temp/ store."""
        prefix = str(path).replace("\\", "/").rstrip("/") + "/"
        names = set()
        for k in self.s3:
            kk = str(k).replace("\\", "/")
            if kk.startswith(prefix):
                names.add(kk[len(prefix):].split("/")[0])
        return sorted(names)


# --- _is_machine_local policy ----------------------------------------------
@pytest.mark.parametrize("name,prefix,excluded", [
    ("node.md", "world", False),
    ("aspirations.jsonl", "world", False),
    ("self.md", "agents", False),
    ("evolution-log.jsonl", "meta", False),   # meta *-log = domain audit -> SYNC
    ("meta-log.jsonl", "meta", False),
    ("changelog.jsonl", "world", True),
    ("tree-update-log.jsonl", "world", True),  # world *-log = per-machine decision
    ("tree-update-log.jsonl", "meta", False),  # but only under world/
    ("x.lock", "world", True),
    ("foo.pyc", "agents", True),
    ("local-paths.conf", "agents", True),
    (".env.local", "agents", True),
    ("file-contention-telemetry.jsonl", "meta", True),
    (".fallback-stats.jsonl", "world", True),
])
def test_is_machine_local(name, prefix, excluded):
    assert _mod._is_machine_local(name, prefix) is excluded


# --- session-file sync policy (sync-by-default + manifest denylist) ---------
# Files under <agent>/session/ are classified by core/config/session-manifest.yaml
# sync_tier. These tests exercise the real repo manifest (the SSOT) + the
# fail-safe heuristic for unregistered files + the unreadable-manifest fallback.
_SROOT = Path("X:/agents") if os.name == "nt" else Path("/x/agents")


def _sml(name, *mid):
    """machine_local? for X:/agents/alpha/session/<mid...>/name."""
    fp = _SROOT.joinpath("alpha", "session", *mid, name)
    return _mod._is_machine_local(name, "agents", full_path=fp, root_path=_SROOT)


@pytest.mark.parametrize("name", [
    "agent-state", "agent-mode", "running-session-id", "runner-token",
    "runner-heartbeat", "loop-active", "stop-requested",
    "background-jobs.yaml", "pending-agents.yaml",   # registered machine_local (have ext)
    "retrieval-session.json", "context-budget.json", "aspirations-compact.json",
    "recovery-notice",
])
def test_session_machine_local_excluded(name):
    # Liveness/identity/this-machine files must NEVER sync (phantom-runner risk).
    assert _sml(name) is True


@pytest.mark.parametrize("name", [
    "handoff.yaml", "working-memory.yaml", "pending-questions.yaml",
    "execution-diary.jsonl", "email-last-seen.txt", "reasoning-snapshot.yaml",
    "goal-reads.jsonl", "consolidation-lean-streak",
])
def test_session_continuity_syncs(name):
    # Accumulated cross-machine knowledge must reach S3 (pulled on machine-B).
    assert _sml(name) is False


@pytest.mark.parametrize("name", [
    "quiescence-audit.jsonl", "precheck-drops.jsonl", "streak-breaks.jsonl",
    "yaml-lint-errors.jsonl",
])
def test_session_ephemeral_syncs(name):
    # Ephemeral telemetry syncs opportunistically (not pulled, but harmless).
    assert _sml(name) is False


@pytest.mark.parametrize("name", [
    "brand-new-signal",        # extensionless -> signal-shaped -> stay local
    "future-marker",
    "weird.flag",              # unknown extension -> stay local (safe)
    "thing.lock",              # also caught by _EXCLUDE_GLOBS, but heuristic too
])
def test_session_unregistered_signal_shaped_stays_local(name):
    # Fail-safe: an unregistered file that is NOT a known data extension stays
    # local, so a not-yet-classified liveness file can never sync (phantom runner).
    assert _sml(name) is True


@pytest.mark.parametrize("name", [
    "brand-new-knowledge.yaml", "notes.txt", "audit.jsonl", "config.json",
])
def test_session_unregistered_data_extension_syncs(name):
    # Drift defense: an unregistered file WITH a known data extension syncs by
    # default, so knowledge accumulating in a new file is not silently lost.
    assert _sml(name) is False


def test_session_scratch_contents_machine_local():
    # scratch/ is the machine-local ad-hoc workspace — contents never sync,
    # even a data-extension file inside it.
    assert _sml("probe.json", "scratch") is True
    assert _sml("query-out.txt", "scratch", "sub") is True


def test_session_failsafe_unreadable_manifest_all_local(monkeypatch):
    # If the manifest cannot be loaded, EVERY session file is treated as
    # machine-local (safe pre-redesign behavior) — even a continuity file.
    monkeypatch.setattr(_mod, "_load_session_tiers", lambda: None)
    assert _sml("handoff.yaml") is True
    assert _sml("brand-new-knowledge.yaml") is True


def test_session_singular_walked_plural_pruned():
    # session (singular) is sync-by-default (NOT walk-pruned); sessions (plural,
    # per-SID scratch) stays walk-pruned. This is the load-bearing distinction.
    assert "session" not in _mod._EXCLUDE_DIRS
    assert "sessions" in _mod._EXCLUDE_DIRS


def test_reports_excluded_temp_included():
    # file-model normalization: reports/ is a FROZEN git archive -> walk-pruned
    # (git is its transport, not S3). temp/ is LIVE working docs -> sync-by-default
    # (NOT excluded; pull_temp resumes it cross-machine). The asymmetry is the point.
    assert "reports" in _mod._EXCLUDE_DIRS
    assert "temp" not in _mod._EXCLUDE_DIRS


def test_sweep_prunes_reports_keeps_temp(tmp_path, monkeypatch):
    # End-to-end: a reports/ file is walk-pruned (never pushed); a temp/ working
    # doc syncs. Proves the frozen-archive vs live-staging distinction in the walk.
    monkeypatch.setenv("MIND_RUNTIME_DIR", str(tmp_path / "rt"))
    agents = tmp_path / "agents"
    rpt = agents / "alpha" / "reports" / "old-closure-2026-05-01.md"
    tmp = agents / "alpha" / "temp" / "design-2026-06-02.md"
    rpt.parent.mkdir(parents=True)
    tmp.parent.mkdir(parents=True)
    rpt.write_bytes(b"frozen archive")
    tmp.write_bytes(b"live working doc")
    be = FakeBackend([(agents, "agents")])
    _mod.sweep(be, only_root="agents", dry_run=False, use_manifest=False, full=True)
    puts = set(be.puts)
    assert str(rpt) not in puts, "reports/ (frozen archive) must NOT sync to S3"
    assert str(tmp) in puts, "temp/ (live working docs) MUST sync to S3"


def test_non_session_agent_file_unaffected():
    # An agent file NOT under session/ (e.g. self.md, aspirations.jsonl) syncs
    # normally — the session policy must not leak into the rest of the agent dir.
    fp = _SROOT.joinpath("alpha", "self.md")
    assert _mod._is_machine_local("self.md", "agents", full_path=fp, root_path=_SROOT) is False
    fp2 = _SROOT.joinpath("alpha", "aspirations.jsonl")
    assert _mod._is_machine_local("aspirations.jsonl", "agents", full_path=fp2, root_path=_SROOT) is False


# --- _etag_matches ---------------------------------------------------------
def test_etag_matches_quoted():
    m = _md5(b"hello")
    assert _mod._etag_matches('"' + m + '"', m) is True
    assert _mod._etag_matches('"' + m + '"', _md5(b"other")) is False


def test_etag_multipart_never_matches():
    # multipart ETag ("...-N") can't be compared -> force a push.
    assert _mod._etag_matches('"abc123-4"', "abc123") is False


# --- _sync_one -------------------------------------------------------------
def test_sync_one_skips_in_sync(tmp_path):
    be = FakeBackend([(tmp_path, "world")])
    f = tmp_path / "a.md"
    f.write_bytes(b"same")
    be.s3[str(f)] = b"same"               # already in sync
    stats = _new_stats()
    _mod._sync_one(be, f, dry_run=False, stats=stats)
    assert stats["in_sync"] == 1 and stats["pushed"] == 0 and be.puts == []


def test_sync_one_pushes_when_absent(tmp_path):
    be = FakeBackend([(tmp_path, "world")])
    f = tmp_path / "a.md"
    f.write_bytes(b"local body")          # not on S3 (the gap)
    stats = _new_stats()
    _mod._sync_one(be, f, dry_run=False, stats=stats)
    assert stats["pushed"] == 1 and be.s3[str(f)] == b"local body"


def test_sync_one_pushes_when_differs(tmp_path):
    be = FakeBackend([(tmp_path, "world")])
    f = tmp_path / "a.md"
    f.write_bytes(b"local NEW")
    be.s3[str(f)] = b"remote OLD"
    stats = _new_stats()
    _mod._sync_one(be, f, dry_run=False, stats=stats)
    assert stats["pushed"] == 1 and be.s3[str(f)] == b"local NEW"


def test_sync_one_dry_run_no_write(tmp_path):
    be = FakeBackend([(tmp_path, "world")])
    f = tmp_path / "a.md"
    f.write_bytes(b"local body")
    stats = _new_stats()
    _mod._sync_one(be, f, dry_run=True, stats=stats)
    assert stats["would_push"] == 1 and be.puts == []


def test_sync_one_conflict_counted_not_raised(tmp_path):
    class Conflicter(FakeBackend):
        def mirror_put(self, path, content, *, expected_version=None):
            raise ConflictError("concurrent")
    be = Conflicter([(tmp_path, "world")])
    f = tmp_path / "a.md"
    f.write_bytes(b"x")
    be.s3[str(f)] = b"y"
    stats = _new_stats()
    _mod._sync_one(be, f, dry_run=False, stats=stats)   # must not raise
    assert stats["conflicts"] == 1 and stats["pushed"] == 0


def _new_stats():
    return {"scanned": 0, "in_sync": 0, "pushed": 0, "would_push": 0,
            "conflicts": 0, "errors": 0, "skipped_unchanged": 0,
            "push_paths": []}


# --- _sweep (walk + prune + manifest) --------------------------------------
def _build_tree(tmp_path):
    world = tmp_path / "world"
    meta = tmp_path / "meta"
    agents = tmp_path / "agents"
    files = {
        world / "knowledge" / "node.md": b"node body",          # sync
        world / "board" / "general.jsonl": b'{"m":1}\n',         # sync
        world / "changelog.jsonl": b"audit\n",                   # excluded (name)
        world / "tree-log.jsonl": b"dec\n",                      # excluded (world *-log)
        world / ".history" / "snap.gz": b"\x1f\x8b",             # excluded (dir)
        world / "a.lock": b"",                                   # excluded (glob)
        meta / "evolution-log.jsonl": b"strat\n",                # sync (meta *-log)
        agents / "alpha" / "self.md": b"identity",               # sync
        agents / "alpha" / "local-paths.conf": b"WORLD=...",     # excluded (name)
        agents / "alpha" / "session" / "agent-state": b"RUNNING",  # excluded (session machine_local)
        agents / "alpha" / "sessions" / "SID1" / "x.json": b"{}",  # excluded (sessions/ walk-pruned)
    }
    for p, b in files.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b)
    roots = [(world, "world"), (meta, "meta"), (agents, "agents")]
    return roots


def _syncable(tmp_path):
    return {
        str(tmp_path / "world" / "knowledge" / "node.md"),
        str(tmp_path / "world" / "board" / "general.jsonl"),
        str(tmp_path / "meta" / "evolution-log.jsonl"),
        str(tmp_path / "agents" / "alpha" / "self.md"),
    }


def test_sweep_pushes_only_syncable(tmp_path, monkeypatch):
    monkeypatch.setenv("MIND_RUNTIME_DIR", str(tmp_path / "rt"))
    roots = _build_tree(tmp_path)
    be = FakeBackend(roots)
    stats = _mod.sweep(be, only_root=None, dry_run=False,
                       use_manifest=True, full=True)
    assert set(be.puts) == _syncable(tmp_path)
    assert stats["pushed"] == 4


def test_sweep_dry_run_plans_no_write(tmp_path, monkeypatch):
    monkeypatch.setenv("MIND_RUNTIME_DIR", str(tmp_path / "rt"))
    roots = _build_tree(tmp_path)
    be = FakeBackend(roots)
    stats = _mod.sweep(be, only_root=None, dry_run=True,
                        use_manifest=True, full=True)
    assert stats["would_push"] == 4 and be.puts == []
    assert set(stats["push_paths"]) == _syncable(tmp_path)


def test_sweep_manifest_skips_unchanged_on_rerun(tmp_path, monkeypatch):
    monkeypatch.setenv("MIND_RUNTIME_DIR", str(tmp_path / "rt"))
    roots = _build_tree(tmp_path)
    be = FakeBackend(roots)
    _mod.sweep(be, only_root=None, dry_run=False, use_manifest=True, full=True)
    be.puts.clear()
    # second sweep (manifest active, not --full): unchanged files -> no HEAD/PUT
    stats = _mod.sweep(be, only_root=None, dry_run=False,
                        use_manifest=True, full=False)
    assert be.puts == [] and stats["pushed"] == 0
    assert stats["skipped_unchanged"] == 4


def test_sweep_manifest_repushes_changed_file(tmp_path, monkeypatch):
    # The complement to the skip test: a file whose CONTENT (and mtime) changed
    # since the last sync must bypass the manifest and re-push; unchanged peers
    # stay skipped. This is the path the daemon thread relies on every tick.
    monkeypatch.setenv("MIND_RUNTIME_DIR", str(tmp_path / "rt"))
    roots = _build_tree(tmp_path)
    be = FakeBackend(roots)
    _mod.sweep(be, only_root=None, dry_run=False, use_manifest=True, full=True)
    be.puts.clear()
    node = tmp_path / "world" / "knowledge" / "node.md"
    node.write_bytes(b"node body EDITED")          # content + mtime change
    os.utime(node, ns=(2_000_000_000_000_000_000, 2_000_000_000_000_000_000))  # distinct mtime
    stats = _mod.sweep(be, only_root=None, dry_run=False,
                       use_manifest=True, full=False)
    assert be.puts == [str(node)]                   # only the changed file
    assert be.s3[str(node)] == b"node body EDITED"  # new content reached S3
    assert stats["skipped_unchanged"] == 3          # the 3 unchanged peers


def test_sweep_only_root_filter(tmp_path, monkeypatch):
    monkeypatch.setenv("MIND_RUNTIME_DIR", str(tmp_path / "rt"))
    roots = _build_tree(tmp_path)
    be = FakeBackend(roots)
    _mod.sweep(be, only_root="agents", dry_run=False,
                use_manifest=False, full=True)
    assert be.puts == [str(tmp_path / "agents" / "alpha" / "self.md")]


def test_sweep_syncs_session_continuity_excludes_local_and_scratch(tmp_path, monkeypatch):
    # End-to-end through the real walk: a session/ continuity file (handoff.yaml)
    # IS pushed; a session/ machine_local file (agent-state) is NOT; session/
    # scratch/ contents are walk-pruned. Exercises the Phase-2 sweep changes
    # (session no longer in _EXCLUDE_DIRS, manifest-driven filter, scratch prune).
    monkeypatch.setenv("MIND_RUNTIME_DIR", str(tmp_path / "rt"))
    agents = tmp_path / "agents"
    sess = agents / "alpha" / "session"
    (sess / "scratch").mkdir(parents=True)
    (sess / "handoff.yaml").write_bytes(b"next_focus: x")          # continuity -> sync
    (sess / "agent-state").write_bytes(b"RUNNING")                 # machine_local -> excluded
    (sess / "scratch" / "tmp.json").write_bytes(b"{}")             # scratch -> pruned
    be = FakeBackend([(agents, "agents")])
    _mod.sweep(be, only_root="agents", dry_run=False, use_manifest=False, full=True)
    puts = set(be.puts)
    assert str(sess / "handoff.yaml") in puts
    assert str(sess / "agent-state") not in puts
    assert str(sess / "scratch" / "tmp.json") not in puts


# === H4: multi-machine ownership + freshness ===============================

# --- env parsing (H4a/H4b switches) ----------------------------------------
def test_owned_agents_unset_is_none():
    assert _mod._owned_agents() is None                  # autouse fixture unsets


def test_owned_agents_parses_csv(monkeypatch):
    monkeypatch.setenv("MIND_OWNED_AGENTS", " alpha , bravo ,")
    assert _mod._owned_agents() == {"alpha", "bravo"}


def test_owned_agents_blank_is_none(monkeypatch):
    monkeypatch.setenv("MIND_OWNED_AGENTS", "   ")
    assert _mod._owned_agents() is None


def test_multi_machine_from_owned_agents(monkeypatch):
    monkeypatch.setenv("MIND_OWNED_AGENTS", "alpha")
    assert _mod._multi_machine() is True


def test_multi_machine_explicit_flag(monkeypatch):
    monkeypatch.setenv("MIND_MULTI_MACHINE", "1")
    assert _mod._multi_machine() is True


def test_multi_machine_default_false():
    assert _mod._multi_machine() is False                # autouse fixture unsets


def test_manifest_entry_legacy_int():
    assert _mod._manifest_entry(123456789) == (123456789, None)


def test_manifest_entry_new_dict():
    assert _mod._manifest_entry({"mtime": 7, "md5": "abc"}) == (7, "abc")


def test_manifest_entry_none():
    assert _mod._manifest_entry(None) == (None, None)


def test_save_manifest_atomic_roundtrip_no_temp_residue(tmp_path, monkeypatch):
    """_save_manifest writes atomically (unique temp + os.replace) so the two
    concurrent in-daemon callers Phase 3 introduces (periodic sweep thread +
    flush endpoint) can never produce a truncated manifest. Asserts the write
    round-trips through _load_manifest, the overwrite (replace) path round-trips
    too, and the finally-block leaves NO .tmp residue behind."""
    rt = tmp_path / "rt"
    monkeypatch.setenv("MIND_RUNTIME_DIR", str(rt))
    m = {"world/a.md": {"mtime": 123, "md5": "abc"},
         "agents/alpha/session/handoff.yaml": {"mtime": 456, "md5": "def"}}
    _mod._save_manifest(m)
    assert _mod._load_manifest() == m
    p = _mod._manifest_path()
    assert p.exists()
    residue = [x.name for x in p.parent.iterdir() if x.name.endswith(".tmp")]
    assert residue == [], f"temp residue left behind: {residue}"
    # overwrite via the os.replace path: round-trips and still leaves no residue
    m2 = {"world/b.md": {"mtime": 789, "md5": "ghi"}}
    _mod._save_manifest(m2)
    assert _mod._load_manifest() == m2
    residue2 = [x.name for x in p.parent.iterdir() if x.name.endswith(".tmp")]
    assert residue2 == []


# --- continuity pull (_pull_one, the read-side inverse of _sync_one) --------
def _new_pull_stats():
    return {"scanned": 0, "pulled": 0, "in_sync": 0, "would_pull": 0,
            "s3_absent": 0, "local_ahead_skipped": 0, "multipart_deferred": 0,
            "errors": 0}


def test_pull_one_s3_absent_skips(tmp_path):
    be = FakeBackend([(tmp_path, "agents")])
    f = tmp_path / "alpha" / "session" / "handoff.yaml"
    stats = _new_pull_stats()
    out = _mod._pull_one(be, f, dry_run=False, stats=stats, baseline_md5=None)
    assert out is None and stats["s3_absent"] == 1 and stats["pulled"] == 0
    assert not f.exists()


def test_pull_one_local_absent_pulls(tmp_path):
    be = FakeBackend([(tmp_path, "agents")])
    f = tmp_path / "alpha" / "session" / "handoff.yaml"
    be.s3[str(f)] = b"from-other-machine"
    stats = _new_pull_stats()
    out = _mod._pull_one(be, f, dry_run=False, stats=stats, baseline_md5=None)
    assert stats["pulled"] == 1 and out == _md5(b"from-other-machine")
    assert f.read_bytes() == b"from-other-machine"


def test_pull_one_in_sync_skips(tmp_path):
    be = FakeBackend([(tmp_path, "agents")])
    f = tmp_path / "alpha" / "session" / "handoff.yaml"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"same")
    be.s3[str(f)] = b"same"
    stats = _new_pull_stats()
    out = _mod._pull_one(be, f, dry_run=False, stats=stats, baseline_md5=_md5(b"same"))
    assert stats["in_sync"] == 1 and stats["pulled"] == 0
    assert out == _md5(b"same")  # in-sync returns the md5 -> baseline stays recorded


def test_pull_one_local_at_baseline_s3_moved_pulls(tmp_path):
    """Machine-move case: local == baseline (untouched cache), S3 carries the
    other machine's newer flush -> pull."""
    be = FakeBackend([(tmp_path, "agents")])
    f = tmp_path / "alpha" / "session" / "handoff.yaml"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"old-m2-cache")
    be.s3[str(f)] = b"new-m1-flush"
    stats = _new_pull_stats()
    out = _mod._pull_one(be, f, dry_run=False, stats=stats,
                         baseline_md5=_md5(b"old-m2-cache"))
    assert stats["pulled"] == 1
    assert f.read_bytes() == b"new-m1-flush"


def test_pull_one_local_ahead_not_clobbered(tmp_path):
    """Same-machine crash-restart case: local diverged from baseline (unpushed
    writes) -> NEVER clobber, even though S3 differs."""
    be = FakeBackend([(tmp_path, "agents")])
    f = tmp_path / "alpha" / "session" / "handoff.yaml"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"local-unpushed-edit")
    be.s3[str(f)] = b"older-s3"
    stats = _new_pull_stats()
    out = _mod._pull_one(be, f, dry_run=False, stats=stats,
                         baseline_md5=_md5(b"prior-baseline"))
    assert out is None and stats["local_ahead_skipped"] == 1 and stats["pulled"] == 0
    assert f.read_bytes() == b"local-unpushed-edit"  # NOT clobbered


def test_pull_one_no_baseline_local_present_pulls(tmp_path):
    """No baseline + local present + differs -> S3-authoritative at bind -> pull."""
    be = FakeBackend([(tmp_path, "agents")])
    f = tmp_path / "alpha" / "session" / "handoff.yaml"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"leftover")
    be.s3[str(f)] = b"s3-truth"
    stats = _new_pull_stats()
    out = _mod._pull_one(be, f, dry_run=False, stats=stats, baseline_md5=None)
    assert stats["pulled"] == 1
    assert f.read_bytes() == b"s3-truth"


def test_pull_one_dry_run_no_write(tmp_path):
    be = FakeBackend([(tmp_path, "agents")])
    f = tmp_path / "alpha" / "session" / "handoff.yaml"
    be.s3[str(f)] = b"x"
    stats = _new_pull_stats()
    out = _mod._pull_one(be, f, dry_run=True, stats=stats, baseline_md5=None)
    assert out is None and stats["would_pull"] == 1 and stats["pulled"] == 0
    assert not f.exists()


def test_pull_continuity_end_to_end(tmp_path, monkeypatch):
    """Drives the 16 continuity names: pulls an absent file + an untouched-cache
    file whose S3 moved, protects a local-ahead file, and counts S3-absent ones."""
    monkeypatch.setenv("MIND_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr(_mod, "_SESSION_TIERS_LOADED", False)  # read real manifest fresh
    monkeypatch.setattr(_mod, "_SESSION_TIERS", None)
    agents_root = tmp_path / "agents"
    be = FakeBackend([(agents_root, "agents")])
    sess = agents_root / "alpha" / "session"
    sess.mkdir(parents=True)
    # handoff.yaml: local absent, S3 present -> pull
    be.s3[str(sess / "handoff.yaml")] = b"handoff-from-m1"
    # working-memory.yaml: local == baseline (untouched cache), S3 newer -> pull
    (sess / "working-memory.yaml").write_bytes(b"wm-old")
    be.s3[str(sess / "working-memory.yaml")] = b"wm-new-m1"
    # execution-diary.jsonl: local ahead of baseline (unpushed) -> NOT clobbered
    (sess / "execution-diary.jsonl").write_bytes(b"diary-local-ahead")
    be.s3[str(sess / "execution-diary.jsonl")] = b"diary-old-s3"
    _mod._save_manifest({
        "agents/alpha/session/working-memory.yaml": {"mtime": 1, "md5": _md5(b"wm-old")},
        "agents/alpha/session/execution-diary.jsonl": {"mtime": 1, "md5": _md5(b"diary-prior")},
    })
    stats = _mod.pull_continuity(be, "alpha")
    assert stats["scanned"] == 16
    assert set(stats["pulled_files"]) == {"handoff.yaml", "working-memory.yaml"}
    assert stats["pulled"] == 2
    assert stats["local_ahead_skipped"] == 1
    assert stats["s3_absent"] == 13  # the other 13 continuity names absent on S3
    assert (sess / "handoff.yaml").read_bytes() == b"handoff-from-m1"
    assert (sess / "working-memory.yaml").read_bytes() == b"wm-new-m1"
    assert (sess / "execution-diary.jsonl").read_bytes() == b"diary-local-ahead"


def test_pull_continuity_failsafe_untrustworthy_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("MIND_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr(_mod, "_SESSION_TIERS_LOADED", True)
    monkeypatch.setattr(_mod, "_SESSION_TIERS", None)  # untrustworthy -> pull nothing
    be = FakeBackend([(tmp_path / "agents", "agents")])
    stats = _mod.pull_continuity(be, "alpha")
    assert "error" in stats and stats["pulled"] == 0 and stats["scanned"] == 0


# --- _sync_one baseline classification (H4b) -------------------------------
def test_sync_one_skips_stale_cache(tmp_path):
    """local == baseline (untouched cache), S3 moved (peer wrote) -> STALE -> skip,
    never clobber the peer's newer bytes."""
    be = FakeBackend([(tmp_path, "world")])
    f = tmp_path / "a.md"
    f.write_bytes(b"v1")                          # local is the cached v1
    be.s3[str(f)] = b"v2-peer"                    # S3 moved to v2 by a peer
    stats = _new_stats()
    out = _mod._sync_one(be, f, dry_run=False, stats=stats,
                         baseline_md5=_md5(b"v1"), multi_machine=True)
    assert out is None                            # no baseline update on skip
    assert stats.get("stale_skipped") == 1 and stats["pushed"] == 0
    assert be.s3[str(f)] == b"v2-peer"            # peer bytes survive


def test_sync_one_pushes_legit_local_write(tmp_path):
    """local != baseline (we edited), S3 == baseline -> legit local write -> push."""
    be = FakeBackend([(tmp_path, "world")])
    f = tmp_path / "a.md"
    f.write_bytes(b"v2-local")
    be.s3[str(f)] = b"v1"                         # S3 still at baseline v1
    stats = _new_stats()
    out = _mod._sync_one(be, f, dry_run=False, stats=stats,
                         baseline_md5=_md5(b"v1"), multi_machine=True)
    assert out == _md5(b"v2-local")
    assert stats["pushed"] == 1 and be.s3[str(f)] == b"v2-local"


def test_sync_one_skips_true_conflict(tmp_path):
    """both moved since baseline -> CONFLICT -> skip + warn, clobber neither side."""
    be = FakeBackend([(tmp_path, "world")])
    f = tmp_path / "a.md"
    f.write_bytes(b"v2-local")
    be.s3[str(f)] = b"v2-peer"                    # both diverged from baseline v1
    stats = _new_stats()
    out = _mod._sync_one(be, f, dry_run=False, stats=stats,
                         baseline_md5=_md5(b"v1"), multi_machine=True)
    assert out is None
    assert stats.get("diverged_skipped") == 1 and stats["pushed"] == 0
    assert be.s3[str(f)] == b"v2-peer"


def test_sync_one_nobaseline_multimachine_skips(tmp_path):
    """diverged, no baseline, multi-machine -> cannot prove authority -> skip."""
    be = FakeBackend([(tmp_path, "world")])
    f = tmp_path / "a.md"
    f.write_bytes(b"local")
    be.s3[str(f)] = b"remote"
    stats = _new_stats()
    out = _mod._sync_one(be, f, dry_run=False, stats=stats,
                         baseline_md5=None, multi_machine=True)
    assert out is None
    assert stats.get("nobaseline_skipped") == 1 and stats["pushed"] == 0
    assert be.s3[str(f)] == b"remote"


def test_sync_one_nobaseline_singlemachine_pushes(tmp_path):
    """diverged, no baseline, SINGLE machine -> local authoritative -> push
    (preserves the pre-H4 single-machine behavior)."""
    be = FakeBackend([(tmp_path, "world")])
    f = tmp_path / "a.md"
    f.write_bytes(b"local")
    be.s3[str(f)] = b"remote"
    stats = _new_stats()
    out = _mod._sync_one(be, f, dry_run=False, stats=stats,
                         baseline_md5=None, multi_machine=False)
    assert out == _md5(b"local")
    assert stats["pushed"] == 1 and be.s3[str(f)] == b"local"


def test_sync_one_absent_pushes_even_multimachine(tmp_path):
    """S3 absent -> new local content -> push (no peer bytes to clobber), any mode."""
    be = FakeBackend([(tmp_path, "world")])
    f = tmp_path / "a.md"
    f.write_bytes(b"brand new")
    stats = _new_stats()
    out = _mod._sync_one(be, f, dry_run=False, stats=stats,
                         baseline_md5=None, multi_machine=True)
    assert out == _md5(b"brand new")
    assert stats["pushed"] == 1 and be.s3[str(f)] == b"brand new"


# --- multipart ETag handling (uncomparable -> honest defer, never clobber) --
class _MultipartBackend(FakeBackend):
    """Like FakeBackend but reports + fences on a MULTIPART etag ('<md5>-2') for
    every object — the case an md5-based comparison cannot resolve. Fencing stays
    self-consistent (stat and mirror_put use the same etag form) so a single-part
    PUT fence is not spuriously rejected."""
    def _etag(self, b):
        return '"' + _md5(b) + '-2"'

    def stat(self, path):
        b = self.s3.get(str(path))
        if b is None:
            return None
        return FileStat(version=self._etag(b), size=len(b), mtime_ns=0)

    def mirror_put(self, path, content, *, expected_version=None):
        cur = self.s3.get(str(path))
        cur_etag = self._etag(cur) if cur is not None else None
        if expected_version is not None and expected_version != cur_etag:
            raise ConflictError(f"stale fence for {path}")
        self.s3[str(path)] = content
        self.puts.append(str(path))


def test_etag_is_multipart():
    assert _mod._etag_is_multipart('"abc123-2"') is True
    assert _mod._etag_is_multipart('"abc123"') is False
    assert _mod._etag_is_multipart("") is False


def test_sync_one_multipart_multimachine_defers_no_clobber(tmp_path):
    """A genuine local write to a file whose S3 ETag is multipart must NOT be
    mislabeled CONFLICT and dropped, and must NEVER clobber S3 -> honest defer.
    (Regression for the fresh-eyes finding: multipart etag -> false CONFLICT.)"""
    be = _MultipartBackend([(tmp_path, "world")])
    f = tmp_path / "a.md"
    f.write_bytes(b"v2-local")                    # we edited locally
    be.s3[str(f)] = b"v1-peer"                    # S3 present, multipart etag
    stats = _new_stats()
    out = _mod._sync_one(be, f, dry_run=False, stats=stats,
                         baseline_md5=_md5(b"v1"), multi_machine=True)
    assert out is None
    assert stats.get("multipart_deferred") == 1
    assert stats["pushed"] == 0 and stats.get("diverged_skipped", 0) == 0
    assert be.puts == [] and be.s3[str(f)] == b"v1-peer"   # peer bytes survive


def test_sync_one_multipart_singlemachine_pushes(tmp_path):
    """Single-machine: no peer to clobber -> local authoritative -> push even
    though the S3 etag is multipart (uncomparable)."""
    be = _MultipartBackend([(tmp_path, "world")])
    f = tmp_path / "a.md"
    f.write_bytes(b"local")
    be.s3[str(f)] = b"remote"
    stats = _new_stats()
    out = _mod._sync_one(be, f, dry_run=False, stats=stats,
                         baseline_md5=_md5(b"base"), multi_machine=False)
    assert out == _md5(b"local")
    assert stats["pushed"] == 1 and be.s3[str(f)] == b"local"


def test_pull_one_multipart_etag_defers_no_clobber(tmp_path):
    """PULL-side mirror of the multipart defer: an S3 ETag that is multipart
    (uncomparable to a content md5) must NOT be classified vs baseline and
    pulled — that could clobber a local file whose S3 ETag merely became
    multipart (server-side copy/replication, bytes maybe unchanged). Defer."""
    be = _MultipartBackend([(tmp_path, "agents")])
    f = tmp_path / "alpha" / "session" / "handoff.yaml"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"local-v1")
    be.s3[str(f)] = b"s3-v2"                       # present, multipart etag
    stats = _new_pull_stats()
    out = _mod._pull_one(be, f, dry_run=False, stats=stats,
                         baseline_md5=_md5(b"local-v1"))
    assert out is None
    assert stats["multipart_deferred"] == 1 and stats["pulled"] == 0
    assert f.read_bytes() == b"local-v1"            # local NOT clobbered


# --- sweep --full clobber prevention (the money tests) ---------------------
def test_sweep_full_does_not_clobber_peer_write(tmp_path, monkeypatch):
    """THE machine-2 hazard: a file this machine only CACHED (local == last sync)
    that a PEER then moved on S3 must NOT be pushed back stale by a --full sweep.
    The baseline md5 catches it; the If-Match fence alone would not."""
    monkeypatch.setenv("MIND_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("MIND_MULTI_MACHINE", "1")
    roots = _build_tree(tmp_path)
    be = FakeBackend(roots)
    _mod.sweep(be, only_root=None, dry_run=False, use_manifest=True, full=True)
    node = tmp_path / "world" / "knowledge" / "node.md"
    assert be.s3[str(node)] == b"node body"
    be.puts.clear()
    be.s3[str(node)] = b"PEER NEWER BYTES"        # a peer moved S3; local unchanged
    stats = _mod.sweep(be, only_root=None, dry_run=False,
                       use_manifest=True, full=True)
    assert str(node) not in be.puts
    assert be.s3[str(node)] == b"PEER NEWER BYTES"   # peer bytes survive --full
    assert stats["stale_skipped"] >= 1


def test_sweep_full_pushes_legit_local_edit_multimachine(tmp_path, monkeypatch):
    """Complement: on machine-2, a file WE edited (local != baseline, S3 ==
    baseline) still pushes on --full — proves the stale-skip is not over-broad."""
    monkeypatch.setenv("MIND_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("MIND_MULTI_MACHINE", "1")
    roots = _build_tree(tmp_path)
    be = FakeBackend(roots)
    _mod.sweep(be, only_root=None, dry_run=False, use_manifest=True, full=True)
    node = tmp_path / "world" / "knowledge" / "node.md"
    be.puts.clear()
    node.write_bytes(b"node body EDITED HERE")    # WE edited; S3 still at baseline
    stats = _mod.sweep(be, only_root=None, dry_run=False,
                       use_manifest=True, full=True)
    assert str(node) in be.puts
    assert be.s3[str(node)] == b"node body EDITED HERE"


# --- sweep agent-dir ownership scoping (H4a) -------------------------------
def test_sweep_prunes_unowned_agent_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("MIND_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("MIND_OWNED_AGENTS", "alpha")
    roots = _build_tree(tmp_path)
    bravo = tmp_path / "agents" / "bravo" / "self.md"   # a peer agent we don't own
    bravo.parent.mkdir(parents=True, exist_ok=True)
    bravo.write_bytes(b"bravo identity")
    be = FakeBackend(roots)
    stats = _mod.sweep(be, only_root=None, dry_run=False,
                       use_manifest=True, full=True)
    assert str(tmp_path / "agents" / "alpha" / "self.md") in be.puts
    assert str(bravo) not in be.puts              # pruned (peer cache)
    assert stats["pruned_agents"] == 1


def test_sweep_owns_all_agents_when_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("MIND_RUNTIME_DIR", str(tmp_path / "rt"))
    roots = _build_tree(tmp_path)                 # MIND_OWNED_AGENTS unset (fixture)
    bravo = tmp_path / "agents" / "bravo" / "self.md"
    bravo.parent.mkdir(parents=True, exist_ok=True)
    bravo.write_bytes(b"bravo identity")
    be = FakeBackend(roots)
    stats = _mod.sweep(be, only_root=None, dry_run=False,
                       use_manifest=True, full=True)
    assert str(tmp_path / "agents" / "alpha" / "self.md") in be.puts
    assert str(bravo) in be.puts                  # unset => own all agents
    assert stats["pruned_agents"] == 0


# --- sync_file ownership (H4a, single-file PostToolUse path) ----------------
def test_sync_file_skips_unowned_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("MIND_OWNED_AGENTS", "alpha")
    agents = tmp_path / "agents"
    bravo = agents / "bravo" / "self.md"
    bravo.parent.mkdir(parents=True, exist_ok=True)
    bravo.write_bytes(b"bravo body")
    be = FakeBackend([(agents, "agents")])
    rc = _mod.sync_file(be, bravo, dry_run=False)
    assert rc == 0 and be.puts == []              # peer file never pushed


def test_sync_file_pushes_owned_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("MIND_OWNED_AGENTS", "alpha")
    agents = tmp_path / "agents"
    alpha = agents / "alpha" / "self.md"
    alpha.parent.mkdir(parents=True, exist_ok=True)
    alpha.write_bytes(b"alpha body")
    be = FakeBackend([(agents, "agents")])
    _mod.sync_file(be, alpha, dry_run=False)
    assert be.puts == [str(alpha.resolve())]      # owned file pushed


def test_sync_file_skips_session_machine_local(tmp_path):
    # The PostToolUse single-file path must NOT push a session machine_local
    # file (liveness/identity) — same denylist as the sweep.
    agents = tmp_path / "agents"
    f = agents / "alpha" / "session" / "agent-state"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"RUNNING")
    be = FakeBackend([(agents, "agents")])
    rc = _mod.sync_file(be, f, dry_run=False)
    assert rc == 0 and be.puts == []


def test_sync_file_pushes_session_continuity(tmp_path):
    # A session continuity file (handoff.yaml) IS pushed by the single-file path.
    agents = tmp_path / "agents"
    f = agents / "alpha" / "session" / "handoff.yaml"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"next_focus: x")
    be = FakeBackend([(agents, "agents")])
    _mod.sync_file(be, f, dry_run=False)
    assert be.puts == [str(f.resolve())]


# === Phase 7: end-to-end capstone — flush -> machine-move -> pull ===========
# The push-side (sweep) and pull-side (pull_continuity) are each unit-tested
# above in isolation. These two capstone tests chain them through ONE
# FakeBackend S3 store so a regression that breaks the HANDOFF between the two
# sides — e.g. a key-shape mismatch where _sync_one writes be.s3[K1] but
# _pull_one stats be.s3[K2] — is caught. They drive the EXACT argument tuple the
# production flush endpoint uses (only_root=None, dry_run=False,
# use_manifest=True, full=False — the SSOT shared with the periodic sweep
# thread); with a single agents root registered, only_root=None walks just it.

def test_e2e_flush_then_pull_round_trips_continuity(tmp_path, monkeypatch):
    """machine-1 /stop flush -> machine move -> machine-2 /start pull.

    Drives ALL THREE tiers in one round-trip so the pull's continuity-tier
    filter is load-bearing within this single test:
      - handoff.yaml (continuity)  survives the move, resumes byte-identical.
      - agent-state (machine_local) never reaches S3 -> no phantom RUNNING on m2.
      - quiescence-audit.jsonl (ephemeral) DOES reach S3 (the flush mirrors it)
        but is NOT pulled — proving the pull excludes a non-continuity file that
        is genuinely present in S3, not merely one that was never pushed.
    This is the user's exact failure scenario — stop alpha on m1, start alpha on
    m2 — proven closed across all three steps."""
    monkeypatch.setenv("MIND_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr(_mod, "_SESSION_TIERS_LOADED", False)  # real manifest fresh
    monkeypatch.setattr(_mod, "_SESSION_TIERS", None)
    agents_root = tmp_path / "agents"
    be = FakeBackend([(agents_root, "agents")])
    sess = agents_root / "alpha" / "session"
    sess.mkdir(parents=True)
    handoff = sess / "handoff.yaml"
    state = sess / "agent-state"
    eph = sess / "quiescence-audit.jsonl"
    body = b"session: 50\nnext_focus: session-continuity\n"
    handoff.write_bytes(body)          # continuity    -> must cross machines
    state.write_bytes(b"RUNNING")      # machine_local  -> must NOT cross machines
    eph.write_bytes(b'{"q":1}\n')      # ephemeral      -> synced, NOT pulled

    # --- machine-1: /stop graceful flush (D6.7), exact production args -------
    _mod.sweep(be, only_root=None, dry_run=False, use_manifest=True, full=False)
    assert str(handoff) in be.s3, "continuity handoff.yaml must reach S3"
    assert str(eph) in be.s3, "ephemeral file must reach S3 (mirrored, not pulled)"
    assert str(state) not in be.s3, "machine_local agent-state must NOT reach S3"

    # --- machine move: a fresh machine has no local session state -----------
    handoff.unlink()
    state.unlink()
    eph.unlink()

    # --- machine-2: /start continuity pull (Step 2.6) -----------------------
    stats = _mod.pull_continuity(be, "alpha")
    assert "error" not in stats
    assert "handoff.yaml" in stats["pulled_files"]
    assert handoff.read_bytes() == body, "handoff resumes byte-identical on m2"
    # the liveness marker did NOT cross -> no phantom runner on the new machine
    assert "agent-state" not in stats["pulled_files"]
    assert not state.exists()
    # the ephemeral file WAS in S3 yet the continuity-tier filter excluded it
    # from the pull -> it is the filter, not absence-from-S3, doing the work
    assert "quiescence-audit.jsonl" not in stats["pulled_files"]
    assert not eph.exists(), "ephemeral present in S3 must STILL not be pulled"


def test_e2e_ephemeral_synced_but_not_pulled(tmp_path, monkeypatch):
    """The third tier: ephemeral telemetry (obligation-audit.jsonl) IS mirrored
    to S3 by the flush sweep (so a dead machine's telemetry is not lost) but is
    NOT in the continuity pull-set, so a fresh machine does not re-materialize
    it — by design, ephemeral telemetry is per-machine and harmless to leave
    behind. This guards against an over-broad pull that would resurrect
    machine-1's transient telemetry onto machine-2."""
    monkeypatch.setenv("MIND_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr(_mod, "_SESSION_TIERS_LOADED", False)
    monkeypatch.setattr(_mod, "_SESSION_TIERS", None)
    agents_root = tmp_path / "agents"
    be = FakeBackend([(agents_root, "agents")])
    sess = agents_root / "alpha" / "session"
    sess.mkdir(parents=True)
    eph = sess / "obligation-audit.jsonl"   # sync_tier == ephemeral
    eph.write_bytes(b'{"obligation":"x","ts":1}\n')

    # flush: ephemeral IS mirrored (ephemeral -> not machine_local -> syncs)
    _mod.sweep(be, only_root=None, dry_run=False, use_manifest=True, full=False)
    assert str(eph) in be.s3, "ephemeral file must be mirrored by the flush sweep"

    # machine move
    eph.unlink()

    # pull: ephemeral is NOT in the continuity pull-set -> not restored
    stats = _mod.pull_continuity(be, "alpha")
    assert "error" not in stats
    assert "obligation-audit.jsonl" not in stats["pulled_files"]
    assert not eph.exists(), "ephemeral must NOT be pulled on a machine-move"


# === Phase 5 (file-model): temp/ working-doc store cross-machine pull ========
# temp/ filenames are dynamic timestamps (not manifest-enumerable like the
# session continuity set), so pull_temp lists S3 by prefix and _pull_one()'s
# each, reusing the same no-clobber baseline gate. These prove the prefix walk,
# the drained/ recursion, the no-clobber inheritance, and the shared-manifest
# wiring into pull_continuity (the single-save invariant).

def test_pull_temp_local_absent_pulls(tmp_path):
    """Fresh machine-move: S3 holds a temp working doc, local temp/ is empty ->
    pull it down so 'search through temp' works on the new machine."""
    agents_root = tmp_path / "agents"
    be = FakeBackend([(agents_root, "agents")])
    f = agents_root / "alpha" / "temp" / "design-2026-06-02.md"
    be.s3[str(f)] = b"design body from m1"
    stats = _mod.pull_temp(be, "alpha", _manifest={}, _new_manifest={})
    assert stats["scanned"] == 1 and stats["pulled"] == 1
    assert stats["pulled_files"] == ["temp/design-2026-06-02.md"]
    assert f.read_bytes() == b"design body from m1"


def test_pull_temp_in_sync_skips(tmp_path):
    """Local == S3 -> already current -> no pull, no clobber."""
    agents_root = tmp_path / "agents"
    be = FakeBackend([(agents_root, "agents")])
    f = agents_root / "alpha" / "temp" / "audit.md"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"same")
    be.s3[str(f)] = b"same"
    stats = _mod.pull_temp(be, "alpha", _manifest={}, _new_manifest={})
    assert stats["in_sync"] == 1 and stats["pulled"] == 0


def test_pull_temp_local_ahead_not_clobbered(tmp_path):
    """Unpushed local temp edit (local diverged from baseline) -> NEVER clobber,
    even though S3 differs. Inherits _pull_one's local-ahead guard via the
    manifest baseline keyed by the temp/ rel-key."""
    agents_root = tmp_path / "agents"
    be = FakeBackend([(agents_root, "agents")])
    f = agents_root / "alpha" / "temp" / "notes.md"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"local-unpushed-edit")
    be.s3[str(f)] = b"older-s3"
    rel = "agents/alpha/temp/notes.md"
    stats = _mod.pull_temp(
        be, "alpha",
        _manifest={rel: {"mtime": 1, "md5": _md5(b"prior-baseline")}},
        _new_manifest={})
    assert stats["local_ahead_skipped"] == 1 and stats["pulled"] == 0
    assert f.read_bytes() == b"local-unpushed-edit"  # NOT clobbered


def test_pull_temp_drained_subdir_pulled(tmp_path):
    """The one sanctioned subdir (drained/) is recursed: a drained audit file in
    S3 is pulled so the drain audit trail survives a machine-move."""
    agents_root = tmp_path / "agents"
    be = FakeBackend([(agents_root, "agents")])
    flat = agents_root / "alpha" / "temp" / "live.md"
    drained = agents_root / "alpha" / "temp" / "drained" / "2026-05-old.md"
    be.s3[str(flat)] = b"live doc"
    be.s3[str(drained)] = b"already-drained doc"
    stats = _mod.pull_temp(be, "alpha", _manifest={}, _new_manifest={})
    assert stats["pulled"] == 2
    assert set(stats["pulled_files"]) == {"temp/live.md", "temp/drained/2026-05-old.md"}
    assert drained.read_bytes() == b"already-drained doc"


def test_pull_temp_empty_no_error(tmp_path):
    """No temp/ objects in S3 -> empty prefix listing -> scanned 0, no error
    (the common single-machine / fresh-agent case)."""
    agents_root = tmp_path / "agents"
    be = FakeBackend([(agents_root, "agents")])
    stats = _mod.pull_temp(be, "alpha", _manifest={}, _new_manifest={})
    assert stats["scanned"] == 0 and stats["pulled"] == 0
    assert "error" not in stats and stats["errors"] == 0


def test_pull_temp_updates_shared_manifest_in_place(tmp_path):
    """pull_temp mutates the passed _new_manifest in place (rather than saving its
    own) so pull_continuity's single _save_manifest persists BOTH the session and
    temp baselines — the double-save-clobber hazard this wiring guards against."""
    agents_root = tmp_path / "agents"
    be = FakeBackend([(agents_root, "agents")])
    f = agents_root / "alpha" / "temp" / "x.md"
    be.s3[str(f)] = b"pulled body"
    new_manifest = {"agents/alpha/session/handoff.yaml": {"mtime": 9, "md5": "sess"}}
    _mod.pull_temp(be, "alpha", _manifest={}, _new_manifest=new_manifest)
    # session entry preserved AND temp entry added to the SAME dict object
    assert new_manifest["agents/alpha/session/handoff.yaml"] == {"mtime": 9, "md5": "sess"}
    assert new_manifest["agents/alpha/temp/x.md"]["md5"] == _md5(b"pulled body")


def test_pull_continuity_also_pulls_temp(tmp_path, monkeypatch):
    """Integration: the /start pull (pull_continuity) now also resumes temp/.
    A continuity session file AND a temp working doc both cross the machine-move
    in one call, both appear in pulled_files, and the temp sub-stats are exposed."""
    monkeypatch.setenv("MIND_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr(_mod, "_SESSION_TIERS_LOADED", False)  # real manifest fresh
    monkeypatch.setattr(_mod, "_SESSION_TIERS", None)
    agents_root = tmp_path / "agents"
    be = FakeBackend([(agents_root, "agents")])
    sess = agents_root / "alpha" / "session"
    sess.mkdir(parents=True)
    be.s3[str(sess / "handoff.yaml")] = b"handoff-from-m1"          # continuity
    be.s3[str(agents_root / "alpha" / "temp" / "brief.md")] = b"brief body"  # temp
    stats = _mod.pull_continuity(be, "alpha")
    assert "error" not in stats
    assert "handoff.yaml" in stats["pulled_files"]
    assert "temp/brief.md" in stats["pulled_files"]
    assert stats["temp"]["pulled"] == 1
    assert (agents_root / "alpha" / "temp" / "brief.md").read_bytes() == b"brief body"
    assert (sess / "handoff.yaml").read_bytes() == b"handoff-from-m1"


def test_pull_temp_list_dir_failure_counts_error_no_crash(tmp_path):
    """A network/credential failure listing the temp/ prefix is counted +
    surfaced, never crashes the surrounding /start pull (non-fatal — session
    resume is the critical path)."""
    agents_root = tmp_path / "agents"

    class _Raises(FakeBackend):
        def list_dir(self, path):
            raise OSError("network down")

    be = _Raises([(agents_root, "agents")])
    stats = _mod.pull_temp(be, "alpha", _manifest={}, _new_manifest={})
    assert stats["errors"] == 1 and stats["scanned"] == 0 and stats["pulled"] == 0
    assert "error" in stats


def test_pull_temp_dry_run_no_side_effects(tmp_path):
    """dry_run lists what WOULD pull but materializes nothing on disk and writes
    no manifest entry."""
    agents_root = tmp_path / "agents"
    be = FakeBackend([(agents_root, "agents")])
    f = agents_root / "alpha" / "temp" / "x.md"
    be.s3[str(f)] = b"would pull"
    new_manifest = {}
    stats = _mod.pull_temp(be, "alpha", dry_run=True, _manifest={}, _new_manifest=new_manifest)
    assert stats["would_pull"] == 1 and stats["pulled"] == 0
    assert not f.exists()            # nothing materialized
    assert new_manifest == {}        # no baseline written in dry-run


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
