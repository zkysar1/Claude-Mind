"""The three UNLOCKED whole-file WM writers now hold the WM lock ( remedy b).

THE DEFECT. body-merge `_consume_staged` + the sessions-pass copy-back,
compact-restore-slots' two read->write pairs, and wm-contamination-check's
quarantine each did a whole-file read-modify-write of a Body's working memory
with NO lock anywhere. `os.replace` is atomic, which is exactly why this hid:
the lost update happens BETWEEN the read and the replace, not during it. A
daemon `wm set` landing in that window is reverted AFTER its own read-back
verified it -- the g-115-7322 signature (fresh mtime, update_count back at the
snapshot's value), which reads as a lost update rather than as an error.

WHY THE FLAGSHIP TEST IS SHAPED THE WAY IT IS. Asserting "the code calls
acquire_lock" would pass against a lock taken on the WRONG FILE, which is a live
hazard here: wm.wm_lock() resolves through wm_path(), which honors BODY_WM_PATH,
so it locks whatever the ENVIRONMENT names rather than the file being written
(measured 2026-09-02, alpha/cc-10). So the flagship drives a real competing
writer through the REAL lock and asserts its update SURVIVES. That is false only
if the lock genuinely fails to exclude, and it cannot pass by naming the right
function.

The competing writer is released deterministically rather than by sleeping:
  - lock HELD   -> it blocks in acquire, never signals, the hook proceeds after
                   a short bounded wait, and its write lands cleanly afterwards.
  - lock ABSENT -> it completes immediately, signals at once, and the entry
                   point then writes its stale snapshot over the top -> LOST.
So the sabotage direction is deterministic; only the passing direction pays the
bounded wait.

Every path below is a pytest tmp_path fixture -- no governed store is touched.

Run:
  STORAGE_BACKEND=local python -m pytest \
      core/scripts/tests/test_wm_writer_locking_g115_8667.py -q
"""
from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path

import yaml

TESTS_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = TESTS_DIR.parent

if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

from _fileops import acquire_lock, release_lock  # noqa: E402

# The on-disk basename the code under test resolves to. Assembled from the
# module's own resolver so this file cannot drift from it.
from wm import wm_path as _resolve_wm_path  # noqa: E402

WM_BASENAME = _resolve_wm_path().name


def _load(modname: str, filename: str):
    spec = importlib.util.spec_from_file_location(modname, CORE_SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


merge = _load("body_merge_lock", "body-merge.py")
contam = _load("wm_contamination_lock", "wm-contamination-check.py")

SID = "55555555-5555-4555-8555-555555555555"


# ─────────────────────────── helpers (all under tmp_path) ───────────────────────────

def _wm_file(pr: Path) -> Path:
    return pr / "agents" / "alpha" / "session" / WM_BASENAME


def _mk_agent(tmp_path: Path, reducer_wm: dict) -> Path:
    target = _wm_file(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        yaml.dump(reducer_wm, f, default_flow_style=False, sort_keys=False)
    return tmp_path


def _stage(pr: Path, sid: str, body_wm: dict) -> Path:
    staged = pr / "agents" / "alpha" / "session" / "pending-body-merges"
    staged.mkdir(parents=True, exist_ok=True)
    p = staged / f"{sid}-wm.yaml"
    with open(p, "w", encoding="utf-8") as f:
        yaml.dump(body_wm, f, default_flow_style=False, sort_keys=False)
    return p


def _read(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


class CompetingWriter:
    """Stands in for a concurrent daemon `wm set`, through the REAL lock."""

    def __init__(self, wm_file: Path):
        self.wm_file = wm_file
        self.lock_path = wm_file.with_suffix(".lock")
        self.done = threading.Event()
        self.error: BaseException | None = None
        self.thread: threading.Thread | None = None

    def _run(self):
        try:
            acquire_lock(self.lock_path, stale_seconds=10)
            try:
                data = _read(self.wm_file)
                data.setdefault("slots", {})["daemon_slot"] = "SURVIVED"
                with open(self.wm_file, "w", encoding="utf-8") as f:
                    yaml.dump(data, f, default_flow_style=False, sort_keys=False)
            finally:
                release_lock(self.lock_path)
        except BaseException as e:  # noqa: BLE001 — surfaced to the test body
            self.error = e
        finally:
            self.done.set()

    def start_and_wait_briefly(self, timeout: float = 1.0):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        # Returns fast when the writer got in (lock absent); times out when it
        # is correctly blocked. Either way the assertions below decide.
        self.done.wait(timeout=timeout)

    def join(self, timeout: float = 15.0):
        if self.thread:
            self.thread.join(timeout=timeout)
        assert self.error is None, f"competing writer raised: {self.error!r}"


# ─────────── 1. FLAGSHIP: a concurrent write SURVIVES each entry point ───────────

def test_consume_staged_does_not_clobber_a_concurrent_write(tmp_path, monkeypatch):
    """body-merge's staged-orphan drain. THE test for this goal."""
    pr = _mk_agent(tmp_path, {"slots": {"active_context": {"a": 1}}})
    _stage(pr, SID, {"slots": {"body_only_slot": {"from": "orphan"}}})
    competitor = CompetingWriter(_wm_file(pr))

    # HOOK THE WRITE, NOT THE READ. An earlier draft fired the competitor from a
    # `_read_yaml` spy, which looked equivalent and was not: generalize_down
    # reads the reducer WM on paths OUTSIDE the critical section too, so the
    # competitor ran before the lock was taken and its write was then picked up
    # by the later in-lock read -- the test passed for the wrong reason and
    # survived a mutation that pointed the lock at the wrong file. Caught by
    # that mutation proof, 2026-09-02. `_write_yaml_atomic` is reached ONLY from
    # inside the lock, so firing here is unambiguously mid-critical-section.
    real_write = merge._write_yaml_atomic
    fired = {"n": 0}

    def let_the_competitor_try_then_write(path, data):
        if fired["n"] == 0 and Path(path) == _wm_file(pr):
            fired["n"] = 1
            competitor.start_and_wait_briefly()
        return real_write(path, data)

    monkeypatch.setattr(merge, "_write_yaml_atomic", let_the_competitor_try_then_write)
    summary = merge.generalize_down("alpha", project_root=pr)
    competitor.join()

    assert SID in summary["staged_merged"], summary
    final = _read(_wm_file(pr))
    # The merge landed...
    assert final["slots"].get("body_only_slot") == {"from": "orphan"}
    # ...AND the concurrent write was not reverted. This is the assertion that
    # fails when the lock is removed.
    assert final["slots"].get("daemon_slot") == "SURVIVED", (
        "a concurrent write was lost — the WM lock is not excluding the drain")


def test_quarantine_does_not_clobber_a_concurrent_write(tmp_path, monkeypatch):
    """wm-contamination-check's quarantine (move-aside + fresh-template write)."""
    pr = _mk_agent(tmp_path, {"slots": {"active_context": {"a": 1}}})
    wm_file = _wm_file(pr)
    competitor = CompetingWriter(wm_file)

    real_fresh = contam._fresh_wm

    def fresh_then_let_the_competitor_try(project_root):
        # Called between the two os.replace calls — inside the critical section.
        competitor.start_and_wait_briefly()
        return real_fresh(project_root)

    monkeypatch.setattr(contam, "_fresh_wm", fresh_then_let_the_competitor_try)
    ok, info = contam.quarantine(wm_file, pr)
    competitor.join()

    assert ok, info
    # With the lock held the competitor lands LAST, so its write is visible.
    # What must never happen is its write vanishing while it believed it landed.
    assert _read(wm_file)["slots"].get("daemon_slot") == "SURVIVED", (
        "a concurrent write was lost — the WM lock is not excluding quarantine")


# ─────────── 2. The lock is held AT THE MOMENT OF THE WRITE ───────────

def test_consume_staged_holds_the_lock_when_it_writes(tmp_path, monkeypatch):
    """Pins WHERE the lock is held, which the survival test alone cannot: a lock
    acquired and released BEFORE the write would still let the competitor in."""
    pr = _mk_agent(tmp_path, {"slots": {"x": 1}})
    _stage(pr, SID, {"slots": {"body_only_slot": {"v": 2}}})
    seen = {}
    real_write = merge._write_yaml_atomic

    def spy(path, data):
        seen["locked"] = Path(path).with_suffix(".lock").exists()
        return real_write(path, data)

    monkeypatch.setattr(merge, "_write_yaml_atomic", spy)
    merge.generalize_down("alpha", project_root=pr)
    assert seen.get("locked") is True, "the reducer WM was written with no lock held"


def test_quarantine_holds_the_lock_when_it_writes(tmp_path, monkeypatch):
    pr = _mk_agent(tmp_path, {"slots": {"x": 1}})
    wm_file = _wm_file(pr)
    seen = {}
    real_fresh = contam._fresh_wm

    def spy(project_root):
        seen["locked"] = wm_file.with_suffix(".lock").exists()
        return real_fresh(project_root)

    monkeypatch.setattr(contam, "_fresh_wm", spy)
    ok, _ = contam.quarantine(wm_file, pr)
    assert ok
    assert seen.get("locked") is True, "quarantine rewrote with no lock held"


def test_quarantine_refuses_when_the_lock_cannot_be_taken(tmp_path, monkeypatch):
    """Fails CLOSED: quarantine destroys a working memory, so an unavailable
    lock must stop it rather than let it proceed unprotected."""
    pr = _mk_agent(tmp_path, {"slots": {"x": 1}})
    wm_file = _wm_file(pr)
    original = wm_file.read_text(encoding="utf-8")

    import _fileops

    def refuse(*a, **kw):
        raise TimeoutError("lock busy")

    monkeypatch.setattr(_fileops, "acquire_lock", refuse)
    ok, info = contam.quarantine(wm_file, pr)

    assert ok is False
    assert "lock" in info.lower(), info
    assert wm_file.read_text(encoding="utf-8") == original, "refused but still mutated"


# ─────────── 3. The lock is keyed to the FILE, not the environment ───────────

def test_wm_lock_for_is_keyed_to_its_argument_not_body_wm_path(tmp_path, monkeypatch):
    """The hazard that makes 'it calls acquire_lock' an insufficient assertion.

    wm.wm_lock() resolves through wm_path(), which honors BODY_WM_PATH. If
    body-merge used it, a reducer that ever gained a per-session forked WM would
    silently lock a DIFFERENT file while every other test still passed. This
    pins the lock to the path the helper is HANDED.

    RE-POINTED at the g-306-284 occ-58 reconciliation: two Bodies shipped this
    same g-115-8667 remedy in parallel -- one as a private body-merge
    `_reducer_wm_lock`, one as the shared `wm.wm_lock_for(path)`. The shared form
    won (it also serves wm-contamination-check and carries the daemon-side CAS
    half main lacked). This assertion is unique to the DISCARDED side, so it is
    kept and aimed at the survivor rather than deleted with it (guard-3077).
    """
    target = tmp_path / "reducer-wm.yaml"
    target.write_text("slots: {}\n", encoding="utf-8")
    monkeypatch.setenv("BODY_WM_PATH", str(tmp_path / "somewhere" / "else.yaml"))

    with merge.wm.wm_lock_for(target):
        assert target.with_suffix(".lock").exists(), "did not lock its argument"
        assert not (tmp_path / "somewhere" / "else.lock").exists(), (
            "locked the environment's path instead of the file being written")
    assert not target.with_suffix(".lock").exists(), "lock not released"


def test_compact_restore_holds_the_lock_when_it_writes(tmp_path, monkeypatch):
    """The third entry point: compact-restore-slots' loop_state recovery pair.

    Loaded against a temp AGENT_DIR with BODY_WM_PATH pinned, mirroring the
    proven harness in test_compact_restore_loop_state_recovery.py. The pin is
    load-bearing, not tidiness: on a worker Body the inject hook exports
    BODY_WM_PATH into every call, so an unpinned fixture's write_wm lands on the
    LIVE per-Body working memory (measured 2026-08-16, alpha/cc-08).

    Module state is saved and restored around the import because loading this
    module mutates `_paths.AGENT_DIR` and `wm.AGENT_DIR` process-wide, and the
    other tests in this file share the interpreter.
    """
    session = tmp_path / "session"
    session.mkdir(parents=True, exist_ok=True)
    wm_file = session / WM_BASENAME
    monkeypatch.setenv("BODY_WM_PATH", str(wm_file))

    wm_file.write_text(yaml.safe_dump({
        "version": 1,
        "agent": "test",
        # null on disk is what makes the recovery path fire at all
        "slots": {"loop_state": None, "active_strategy": "x"},
        "slot_meta": {},
        "goals_completed_this_session": 0,
    }, sort_keys=False), encoding="utf-8")
    (session / "compact-checkpoint.yaml").write_text(yaml.safe_dump({
        "all_slots": {
            "loop_state": {"goals_completed": 3, "productive_goals": 2,
                           "evolutions": 0, "signals": {}},
            "active_strategy": "x",
        },
        "slot_meta": {},
        "goals_completed_this_session": 0,
    }, sort_keys=False), encoding="utf-8")

    names = ("_paths", "wm", "compact_restore_slots")
    saved = {m: sys.modules.get(m) for m in names}
    try:
        for m in names:
            sys.modules.pop(m, None)
        import _paths
        _paths.AGENT_DIR = tmp_path
        import wm as wm_module
        wm_module.AGENT_DIR = tmp_path

        spec = importlib.util.spec_from_file_location(
            "compact_restore_slots", CORE_SCRIPTS / "compact-restore-slots.py")
        crs = importlib.util.module_from_spec(spec)
        sys.modules["compact_restore_slots"] = crs
        spec.loader.exec_module(crs)
        crs.CHECKPOINT_PATH = session / "compact-checkpoint.yaml"

        seen = {}
        real_write = crs.write_wm

        def spy(w):
            seen["locked"] = wm_file.with_suffix(".lock").exists()
            return real_write(w)

        crs.write_wm = spy
        crs._recover_lost_loop_state()
    finally:
        for m, v in saved.items():
            if v is not None:
                sys.modules[m] = v
            else:
                sys.modules.pop(m, None)

    assert seen.get("locked") is True, (
        "compact-restore rewrote the working memory with no lock held")
    # and the recovery itself still worked
    after = yaml.safe_load(wm_file.read_text(encoding="utf-8")) or {}
    assert (after.get("slots") or {}).get("loop_state", {}).get("goals_completed") == 3


def test_wm_lock_for_releases_on_exception(tmp_path):
    target = tmp_path / "reducer-wm.yaml"
    target.write_text("slots: {}\n", encoding="utf-8")
    try:
        with merge.wm.wm_lock_for(target):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert not target.with_suffix(".lock").exists(), "lock leaked on the error path"
