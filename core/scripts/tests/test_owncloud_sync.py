"""B15: owncloud-sync mirror sweep — exclusion policy + sync-decision + manifest.

Pure unit test with a FakeBackend (no S3, no moto): exercises _is_machine_local,
_etag_matches, _sync_one (in-sync skip / push-on-differ / push-on-absent /
conflict), and _sweep (dir pruning, dry-run plan, real push, manifest skip on
re-run). The fenced-PUT mechanics of the real backend are covered by moto in
test_owncloud_backend.py (test_mirror_put_*).

H4 (machine-2 gate) coverage: live-claim agent-dir scoping (H4a — _owned_agents
derives the owned set from the DDB runner claims; the sweep tests here monkeypatch
it directly) and the content-baseline stale-cache / conflict classifier (H4b) that
stops a second machine from pushing a stale cache over a peer's newer S3 bytes —
including the --full clobber path the If-Match fence alone does not cover."""
import hashlib
import os
import sys
import time
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
    """Sync ownership derives from STORAGE_BACKEND + the live DDB claims;
    _multi_machine() also honors MACHINE_MULTI. Default every test to the
    single-machine (local, unset) state so a runner shell that exports these
    cannot perturb results; tests exercising multi-machine behavior set them
    explicitly (or monkeypatch _owned_agents) inside the test body."""
    monkeypatch.delenv("MACHINE_MULTI", raising=False)
    monkeypatch.delenv("OWNERSHIP_STALE_SECONDS", raising=False)
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)


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

    def read_bytes(self, key):
        """Read raw bytes for a key from fake S3. Returns None if absent."""
        return self.s3.get(str(key))

    def write_bytes(self, key, data):
        """Write raw bytes to fake S3 for a key (ownership-claim path)."""
        self.s3[str(key)] = data


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


def test_reports_abolished_temp_included():
    # reports/ was abolished (user-directed, 2026-06-02): removed from the repo AND
    # from _EXCLUDE_DIRS. It is no longer a frozen archive — git history is its
    # archive, and the Phase-4 allowlist gate (path-resolution-hook.py) still DENIES
    # new reports/ writes so it cannot be recreated (see test_agent_dir_allowlist).
    # temp/ remains LIVE working docs -> sync-by-default (NOT excluded; pull_temp
    # resumes it cross-machine).
    assert "reports" not in _mod._EXCLUDE_DIRS
    assert "temp" not in _mod._EXCLUDE_DIRS


def test_sweep_keeps_temp(tmp_path, monkeypatch):
    # End-to-end: a temp/ working doc syncs to S3 (live staging). reports/ was
    # abolished 2026-06-02 and no longer exists to prune.
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
    agents = tmp_path / "agents"
    tmp = agents / "alpha" / "temp" / "design-2026-06-02.md"
    tmp.parent.mkdir(parents=True)
    tmp.write_bytes(b"live working doc")
    be = FakeBackend([(agents, "agents")])
    _mod.sweep(be, only_root="agents", dry_run=False, use_manifest=False, full=True)
    puts = set(be.puts)
    assert str(tmp) in puts, "temp/ (live working docs) MUST sync to S3"


def test_non_session_agent_file_unaffected():
    # An agent file NOT under session/ (e.g. self.md, aspirations.jsonl) syncs
    # normally — the session policy must not leak into the rest of the agent dir.
    fp = _SROOT.joinpath("alpha", "self.md")
    assert _mod._is_machine_local("self.md", "agents", full_path=fp, root_path=_SROOT) is False
    fp2 = _SROOT.joinpath("alpha", "aspirations.jsonl")
    assert _mod._is_machine_local("aspirations.jsonl", "agents", full_path=fp2, root_path=_SROOT) is False


# --- health-ledger sync policy (health-ledger.md, 2026-06-03) ---------------
# The per-agent health ledger lives at agents/<agent>/health/<YYYY-MM-DD>.jsonl.
# It is DURABLE cross-machine signal state (the regression-detection record must
# be queryable from any machine), so it MUST sync to S3 — the OPPOSITE of
# .history (local CoW snapshots whose S3 copy is the bucket's own versioning, so
# .history is walk-pruned). The day-file basenames are dynamic dates, not in any
# exclude set; the *-log.jsonl rule is world-scoped; and the session-file policy
# fires only for rel_parts[1] == "session", whereas here it is "health".
def test_health_ledger_day_file_syncs():
    fp = _SROOT.joinpath("zeta", "health", "2026-06-03.jsonl")
    assert _mod._is_machine_local(
        "2026-06-03.jsonl", "agents", full_path=fp, root_path=_SROOT) is False
    # basename-only form (no path context) must also classify as syncable —
    # the date-stamped name matches no _EXCLUDE_NAMES / _EXCLUDE_GLOBS entry.
    assert _mod._is_machine_local("2026-06-03.jsonl", "agents") is False


def test_health_dir_not_walk_pruned_history_is():
    # health/ must be descended into (its contents sync); .history stays pruned.
    assert "health" not in _mod._EXCLUDE_DIRS
    assert ".history" in _mod._EXCLUDE_DIRS


def test_sweep_pushes_health_ledger(tmp_path, monkeypatch):
    # End-to-end through the real walk: a health day-file is pushed to S3.
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
    agents = tmp_path / "agents"
    ledger = agents / "alpha" / "health" / "2026-06-03.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_bytes(b'{"agent":"alpha","composite":0.9466}\n')
    be = FakeBackend([(agents, "agents")])
    _mod.sweep(be, only_root="agents", dry_run=False, use_manifest=False, full=True)
    assert str(ledger) in set(be.puts), "health ledger MUST sync to S3 (durable signal state)"


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
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
    roots = _build_tree(tmp_path)
    be = FakeBackend(roots)
    stats = _mod.sweep(be, only_root=None, dry_run=False,
                       use_manifest=True, full=True)
    assert set(be.puts) == _syncable(tmp_path)
    assert stats["pushed"] == 4


def test_sweep_dry_run_plans_no_write(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
    roots = _build_tree(tmp_path)
    be = FakeBackend(roots)
    stats = _mod.sweep(be, only_root=None, dry_run=True,
                        use_manifest=True, full=True)
    assert stats["would_push"] == 4 and be.puts == []
    assert set(stats["push_paths"]) == _syncable(tmp_path)


def test_sweep_manifest_skips_unchanged_on_rerun(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
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
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
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
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
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
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
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

# --- ownership + multi-machine signals -------------------------------------
def test_owned_agents_unset_is_none():
    assert _mod._owned_agents() is None                  # local backend => own all


def test_multi_machine_from_owncloud_backend(monkeypatch):
    # own-cloud is the multi-machine signal now: _owned_agents returns a set (not
    # None) under own-cloud, so _multi_machine() is True. Monkeypatch the resolver
    # so the test needs no live DDB backend.
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    monkeypatch.setattr(_mod, "_owned_agents", lambda be=None: set())
    assert _mod._multi_machine() is True


def test_multi_machine_explicit_flag(monkeypatch):
    monkeypatch.setenv("MACHINE_MULTI", "1")
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
    monkeypatch.setenv("RUNTIME_DIR", str(rt))
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
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
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
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
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
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("MACHINE_MULTI", "1")
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
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("MACHINE_MULTI", "1")
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


def test_sweep_owncloud_singlemachine_defers_nobaseline_no_clobber(tmp_path, monkeypatch):
    """H4c / 3: under own-cloud on a SINGLE machine (this machine owns
    all agents -> _owned_agents monkeypatched to None), the periodic sweep must
    NOT push a no-baseline local file that DIVERGES from a PRESENT S3 object.
    The transplant scenario: /boot re-runs init-mind, which writes default meta
    files locally with no manifest baseline, while S3 already holds the learned
    state. Pre-fix the single-machine branch pushed local -> CLOBBERED S3. The fix
    folds own-cloud into the sweep's 'cannot prove authority' flag, so it defers
    (nobaseline_skipped) exactly as multi-machine does -- while S3-ABSENT files
    still push, so a genuine first bootstrap stays intact."""
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    # Own-all (None) so the agents-dir prune does not fire — this test targets the
    # WORLD/META no-baseline clobber-prevention, driven by the sweep's own-cloud
    # `mm` flag, not by agent ownership. With _owned_agents -> None, _multi_machine
    # is False, so the protection below comes purely from the own-cloud branch.
    monkeypatch.setattr(_mod, "_owned_agents", lambda be=None: None)
    assert _mod._multi_machine() is False          # precondition: would push pre-fix
    roots = _build_tree(tmp_path)
    be = FakeBackend(roots)
    # S3 already holds AUTHORITATIVE learned content for node.md (diverges from the
    # local default b"node body"); NO manifest baseline (use_manifest=False).
    node = tmp_path / "world" / "knowledge" / "node.md"
    be.s3[str(node)] = b"S3 LEARNED AUTHORITATIVE META (do not clobber)"
    # self.md is ABSENT on S3 -> the first-bootstrap push path must still fire.
    self_md = tmp_path / "agents" / "alpha" / "self.md"
    stats = _mod.sweep(be, only_root=None, dry_run=False,
                       use_manifest=False, full=True)
    # Clobber prevention: a no-baseline file diverging from PRESENT S3 is deferred.
    assert str(node) not in be.puts
    assert be.s3[str(node)] == b"S3 LEARNED AUTHORITATIVE META (do not clobber)"
    assert stats.get("nobaseline_skipped", 0) >= 1
    # First bootstrap intact: an S3-absent governed file still pushes under own-cloud.
    assert str(self_md) in be.puts
    assert be.s3[str(self_md)] == b"identity"


# --- sweep agent-dir ownership scoping (H4a) -------------------------------
def test_sweep_prunes_unowned_agent_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr(_mod, "_owned_agents", lambda be=None: {"alpha"})
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
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
    roots = _build_tree(tmp_path)                 # local backend (fixture) => own all
    bravo = tmp_path / "agents" / "bravo" / "self.md"
    bravo.parent.mkdir(parents=True, exist_ok=True)
    bravo.write_bytes(b"bravo identity")
    be = FakeBackend(roots)
    stats = _mod.sweep(be, only_root=None, dry_run=False,
                       use_manifest=True, full=True)
    assert str(tmp_path / "agents" / "alpha" / "self.md") in be.puts
    assert str(bravo) in be.puts                  # own all agents (local backend)
    assert stats["pruned_agents"] == 0


# --- sweep per-agent flush scope (§6 /stop flush — 9) --------------
def test_sweep_only_agent_scopes_to_one_owned_dir(tmp_path, monkeypatch):
    """only_agent=<name> flushes exactly agents/<name>/ and prunes every sibling
    agent dir — the per-agent /stop flush scope (design §6). With own-all
    (local backend), the only_agent narrowing alone drops siblings."""
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
    roots = _build_tree(tmp_path)                 # local backend => own all
    bravo = tmp_path / "agents" / "bravo" / "self.md"
    bravo.parent.mkdir(parents=True, exist_ok=True)
    bravo.write_bytes(b"bravo identity")
    be = FakeBackend(roots)
    stats = _mod.sweep(be, only_root="agents", dry_run=False,
                       use_manifest=True, full=True, only_agent="alpha")
    assert str(tmp_path / "agents" / "alpha" / "self.md") in be.puts
    assert str(bravo) not in be.puts              # sibling pruned by only_agent
    assert stats["pruned_agents"] == 1            # bravo pruned, alpha kept


def test_sweep_only_agent_never_pushes_unowned_target(tmp_path, monkeypatch):
    """SAFETY (§6): naming an UNOWNED agent in only_agent yields an EMPTY flush.
    The ownership filter drops the dir BEFORE the only_agent narrowing, so a
    mis-targeted per-agent flush can never push a peer's stale cache — even when
    --agent explicitly names that peer."""
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr(_mod, "_owned_agents", lambda be=None: {"alpha"})  # own only alpha
    roots = _build_tree(tmp_path)
    bravo = tmp_path / "agents" / "bravo" / "self.md"    # a peer we do NOT own
    bravo.parent.mkdir(parents=True, exist_ok=True)
    bravo.write_bytes(b"bravo identity")
    be = FakeBackend(roots)
    _mod.sweep(be, only_root="agents", dry_run=False,
               use_manifest=True, full=True, only_agent="bravo")
    assert be.puts == []                          # unowned target => nothing pushed
    assert str(bravo) not in be.puts              # peer cache never clobbered


def test_sweep_only_agent_none_preserves_full_owned_walk(tmp_path, monkeypatch):
    """only_agent=None (the default — full-owned /stop flush) is byte-identical
    to the pre-g-1339 behavior: every owned agent dir is walked."""
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
    roots = _build_tree(tmp_path)                 # own all (unset)
    bravo = tmp_path / "agents" / "bravo" / "self.md"
    bravo.parent.mkdir(parents=True, exist_ok=True)
    bravo.write_bytes(b"bravo identity")
    be = FakeBackend(roots)
    _mod.sweep(be, only_root="agents", dry_run=False,
               use_manifest=True, full=True, only_agent=None)
    assert str(tmp_path / "agents" / "alpha" / "self.md") in be.puts
    assert str(bravo) in be.puts                  # default => all owned agents flushed


# --- sync_file ownership (H4a, single-file PostToolUse path) ----------------
def test_sync_file_skips_unowned_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(_mod, "_owned_agents", lambda be=None: {"alpha"})
    agents = tmp_path / "agents"
    bravo = agents / "bravo" / "self.md"
    bravo.parent.mkdir(parents=True, exist_ok=True)
    bravo.write_bytes(b"bravo body")
    be = FakeBackend([(agents, "agents")])
    rc = _mod.sync_file(be, bravo, dry_run=False)
    assert rc == 0 and be.puts == []              # peer file never pushed


def test_sync_file_pushes_owned_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(_mod, "_owned_agents", lambda be=None: {"alpha"})
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
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
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
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
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
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
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


# --- fresh-box firmware materialization () -------------------------
def _fw_backend(tmp_path, files):
    """FakeBackend with a `world` root; `files` maps rel-path-under-world -> bytes,
    seeded into fake S3 ONLY (local absent — the fresh-box case materialize_firmware
    exists to fix)."""
    world = tmp_path / "world"
    be = FakeBackend([(world, "world")])
    for rel, content in files.items():
        be.s3[str(world / rel)] = content
    return be, world


def test_materialize_firmware_local_backend_noop(tmp_path, monkeypatch):
    # STORAGE_BACKEND unset by the autouse fixture -> local -> pure no-op, no marker.
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
    be, world = _fw_backend(tmp_path, {"scripts/email-send.sh": b"x"})
    stats = _mod.materialize_firmware(be, tmp_path)
    assert stats["skipped"] == "local backend (no-op)"
    assert stats["pulled"] == 0
    assert not (world / "scripts" / "email-send.sh").exists()
    assert not (tmp_path / "mind_api" / "state" / ".firmware-materialized").exists()


def test_materialize_firmware_pulls_world_scripts(tmp_path, monkeypatch):
    # own-cloud + no marker -> the two verified day-1 breakage scripts materialize
    # locally + the one-time marker is written.
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    be, world = _fw_backend(tmp_path, {
        "scripts/email-send.sh": b"#!/usr/bin/env bash\necho send\n",
        "scripts/output-style-mode-guard.sh": b"#!/usr/bin/env bash\nexit 0\n",
    })
    stats = _mod.materialize_firmware(be, tmp_path)
    assert stats["skipped"] is None
    assert stats["pulled"] == 2
    assert (world / "scripts" / "email-send.sh").read_bytes().startswith(b"#!/usr/bin/env bash")
    assert (world / "scripts" / "output-style-mode-guard.sh").exists()
    assert "world/scripts" in stats["materialized_roots"]
    assert (tmp_path / "mind_api" / "state" / ".firmware-materialized").exists()


def test_materialize_firmware_marker_skips_rerun(tmp_path, monkeypatch):
    # Second run with the marker present is a one-stat no-op — a script added to S3
    # AFTER the first materialization is NOT pulled (one-time-per-box semantics).
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    be, world = _fw_backend(tmp_path, {"scripts/email-send.sh": b"x"})
    _mod.materialize_firmware(be, tmp_path)  # first run writes the marker
    be.s3[str(world / "scripts" / "new.sh")] = b"new"
    stats = _mod.materialize_firmware(be, tmp_path)
    assert stats["skipped"] == "already materialized (marker present)"
    assert stats["pulled"] == 0
    assert not (world / "scripts" / "new.sh").exists()


def test_materialize_firmware_force_ignores_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    be, world = _fw_backend(tmp_path, {"scripts/email-send.sh": b"x"})
    _mod.materialize_firmware(be, tmp_path)  # marker written
    be.s3[str(world / "scripts" / "new.sh")] = b"new"
    stats = _mod.materialize_firmware(be, tmp_path, force=True)
    assert stats["skipped"] is None
    assert (world / "scripts" / "new.sh").exists()


def test_materialize_firmware_no_clobber_unpushed_local(tmp_path, monkeypatch):
    # A local script with unpushed edits (local != manifest baseline) must NOT be
    # clobbered by the S3 copy — the _pull_one no-clobber gate is load-bearing here.
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    be, world = _fw_backend(tmp_path, {"scripts/edit.sh": b"S3 VERSION"})
    local = world / "scripts" / "edit.sh"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"LOCAL UNPUSHED EDIT")
    monkeypatch.setattr(_mod, "_load_manifest",
                        lambda: {"world/scripts/edit.sh": {"mtime": 0, "md5": _md5(b"ORIGINAL")}})
    stats = _mod.materialize_firmware(be, tmp_path)
    assert stats["local_ahead_skipped"] == 1
    assert stats["pulled"] == 0
    assert local.read_bytes() == b"LOCAL UNPUSHED EDIT"  # preserved, not clobbered


def test_materialize_firmware_marker_not_written_on_error(tmp_path, monkeypatch):
    # A per-file S3 error is counted (fail-open) but the one-time marker is NOT
    # written, so the next daemon start retries rather than masking the gap forever.
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")

    class Erroring(FakeBackend):
        def stat(self, path):
            if str(path).endswith("boom.sh"):
                raise RuntimeError("S3 HEAD failed")
            return super().stat(path)

    world = tmp_path / "world"
    be = Erroring([(world, "world")])
    be.s3[str(world / "scripts" / "boom.sh")] = b"x"
    stats = _mod.materialize_firmware(be, tmp_path)
    assert stats["errors"] >= 1
    assert not (tmp_path / "mind_api" / "state" / ".firmware-materialized").exists()


def test_materialize_firmware_excludes_pyc_and_pycache(tmp_path, monkeypatch):
    # *.pyc (glob) + __pycache__/ (dir) must never be materialized — honors the
    # same exclusion policy as the push sweep.
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    be, world = _fw_backend(tmp_path, {
        "scripts/keep.sh": b"keep",
        "scripts/skip.pyc": b"compiled",
        "scripts/__pycache__/mod.pyc": b"cache",
    })
    stats = _mod.materialize_firmware(be, tmp_path)
    assert (world / "scripts" / "keep.sh").exists()
    assert not (world / "scripts" / "skip.pyc").exists()
    assert not (world / "scripts" / "__pycache__" / "mod.pyc").exists()
    assert stats["pulled"] == 1


def test_materialize_firmware_recurses_extensionless_subdir(tmp_path, monkeypatch):
    # Robustness of the list_dir-based dir/file split (NOT extension-routing): a
    # `.python-shim/` subdir with an extensionless leaf `python3` must recurse +
    # pull — the exact shape an extension heuristic would mis-handle.
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    be, world = _fw_backend(tmp_path, {
        "scripts/top.sh": b"top",
        "scripts/.python-shim/python3": b"#!shim",
    })
    stats = _mod.materialize_firmware(be, tmp_path)
    assert (world / "scripts" / "top.sh").exists()
    assert (world / "scripts" / ".python-shim" / "python3").read_bytes() == b"#!shim"
    assert stats["pulled"] == 2


# --- fresh-box bootstrap pull (durable closer) -----------------------------
def _bootstrap_backend(tmp_path, world_files=None, meta_files=None):
    """FakeBackend with world+meta roots; *_files map rel-path-under-root -> bytes,
    seeded into fake S3 ONLY (local absent — the fresh-box case pull_bootstrap
    exists to fix)."""
    world = tmp_path / "world"
    meta = tmp_path / "meta"
    be = FakeBackend([(world, "world"), (meta, "meta")])
    for rel, content in (world_files or {}).items():
        be.s3[str(world / rel)] = content
    for rel, content in (meta_files or {}).items():
        be.s3[str(meta / rel)] = content
    return be, world, meta


def test_pull_bootstrap_local_backend_noop(tmp_path, monkeypatch):
    # STORAGE_BACKEND unset by the autouse fixture -> local -> pure no-op.
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
    be, world, meta = _bootstrap_backend(tmp_path, {".initialized": b""})
    stats = _mod.pull_bootstrap(be)
    assert stats["skipped"] == "local backend (no-op)"
    assert stats["pulled"] == 0
    assert not (world / ".initialized").exists()


def test_pull_bootstrap_pulls_world_and_meta(tmp_path, monkeypatch):
    # own-cloud -> the full shared world/+meta/ state (incl .initialized + the
    # tree) materializes locally, so init's idempotency gate sees the TRUE state
    # instead of re-seeding empty stubs ( / BLOCKER 9).
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    be, world, meta = _bootstrap_backend(
        tmp_path,
        world_files={
            ".initialized": b"",
            "knowledge/tree/_tree.yaml": b"nodes: {}\n",
            "program.md": b"# The Program\n",
        },
        meta_files={
            ".initialized": b"",
            "goal-selection-strategy.yaml": b"selection_heuristics: []\n",
        },
    )
    stats = _mod.pull_bootstrap(be)
    assert stats["skipped"] is None
    assert stats["pulled"] == 5
    assert (world / ".initialized").exists()                # the gate-fixing file
    assert (world / "knowledge" / "tree" / "_tree.yaml").read_bytes().startswith(b"nodes:")
    assert (world / "program.md").exists()
    assert (meta / ".initialized").exists()
    assert (meta / "goal-selection-strategy.yaml").exists()
    assert stats["pulled_roots"] == ["world", "meta"]


def test_pull_bootstrap_only_root_limits_scope(tmp_path, monkeypatch):
    # only_root="world" pulls ONLY world (the per-script --root wiring); meta is
    # untouched.
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    be, world, meta = _bootstrap_backend(
        tmp_path,
        world_files={".initialized": b"", "program.md": b"x"},
        meta_files={".initialized": b"", "meta-log.jsonl": b"{}\n"},
    )
    stats = _mod.pull_bootstrap(be, only_root="world")
    assert stats["pulled_roots"] == ["world"]
    assert (world / ".initialized").exists()
    assert not (meta / ".initialized").exists()             # meta NOT pulled


def test_pull_bootstrap_honors_exclusions(tmp_path, monkeypatch):
    # A WHOLE-ROOT pull must still prune _EXCLUDE_DIRS (.history/) and
    # _EXCLUDE_NAMES (world changelog.jsonl) — the differentiator from firmware's
    # narrow world/scripts scope. Only the governed leaf pulls.
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    be, world, meta = _bootstrap_backend(
        tmp_path,
        world_files={
            "aspirations.jsonl": b"{}\n",            # governed -> pull
            "changelog.jsonl": b"{}\n",              # _EXCLUDE_NAMES -> skip
            ".history/knowledge/tree/old.md": b"x",  # _EXCLUDE_DIRS -> pruned
        },
    )
    stats = _mod.pull_bootstrap(be, only_root="world")
    assert (world / "aspirations.jsonl").exists()
    assert not (world / "changelog.jsonl").exists()
    assert not (world / ".history" / "knowledge" / "tree" / "old.md").exists()
    assert stats["pulled"] == 1


def test_pull_bootstrap_no_clobber_unpushed_local(tmp_path, monkeypatch):
    # The _pull_one no-clobber baseline gate protects a genuine unpushed local
    # edit (local != manifest baseline) from being overwritten by the S3 copy.
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    be, world, meta = _bootstrap_backend(tmp_path, world_files={"program.md": b"S3 VERSION"})
    local = world / "program.md"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(b"LOCAL UNPUSHED EDIT")
    monkeypatch.setattr(_mod, "_load_manifest",
                        lambda: {"world/program.md": {"mtime": 0, "md5": _md5(b"ORIGINAL")}})
    stats = _mod.pull_bootstrap(be, only_root="world")
    assert stats["local_ahead_skipped"] == 1
    assert stats["pulled"] == 0
    assert local.read_bytes() == b"LOCAL UNPUSHED EDIT"     # preserved, not clobbered


def test_pull_bootstrap_no_marker_reruns(tmp_path, monkeypatch):
    # Unlike materialize_firmware, pull_bootstrap is NOT marker-gated: a second
    # run re-scans S3 (idempotent via manifest+baseline) and pulls a newly-added
    # file. The caller's local .initialized gate is the freshness signal, not a
    # per-box marker.
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    be, world, meta = _bootstrap_backend(tmp_path, world_files={"program.md": b"x"})
    _mod.pull_bootstrap(be, only_root="world")
    be.s3[str(world / "sources.yaml")] = b"sources: []\n"
    stats = _mod.pull_bootstrap(be, only_root="world")
    assert (world / "sources.yaml").exists()                # re-scan pulled it
    assert stats["pulled"] == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
