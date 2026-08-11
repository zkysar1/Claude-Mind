""": the experience write path re-fences and retries (class-(b) cure).

THE BUG. All five write sites in mind_api/src/endpoints/experience_write.py that
touch experience.jsonl / experience-archive.jsonl did a read-modify-write under a
bare `file_locks.locked()` — no conflict retry. Both stores are class (b)
FENCE-ONLY (`coordination_merge.merge_handler_for` returns None), so nothing
reconciles below the write and a stale If-Match token is a PERMANENT per-object
per-box wedge, not transient contention (rb-2639,
core/config/conventions/governed-store-write-classes.md).

Measured (g-115-3783): agents/alpha/experience.jsonl wedged on EIGHT consecutive
write attempts with escalating backoff, was filed as a HIGH infrastructure
blocker, sat unwritable for hours, and was cured by one external refresh() in
0.7s. Every one of those attempts entered `add()` at the bare lock. A compliant
locked_rmw re-fences on attempt 1.

WHAT THESE TESTS PIN — the three separable invariants of the required pattern,
each mutation-proofed independently:

  1. the cycle calls refresh() — re-taking the If-Match fence per attempt is
     what breaks the deadlock; retrying on a stale token conflicts identically
     forever.
  2. the cycle is wrapped in locked_rmw — so a conflict retries at all.
  3. the READ is INSIDE the cycle — otherwise the retry re-applies a mutation
     computed from a pre-conflict snapshot and the lost-update window stays
     open. Asserted at READ time: a hoisted read still WRITES under the lock, so
     a write-time assertion passes the very revert it exists to catch.

None of this is observable under STORAGE_BACKEND=local, where `conflict_error`
is the empty tuple and locked_rmw degrades to a transparent single pass — which
is exactly why the defect survived a green suite (rb-5250). A stub backend
supplies the conflict type without S3.

PLUS the SCOPE decision, which is the part most likely to be "fixed" wrongly
later: `experience-meta.json` (meta_update) and `working-memory.yaml` (wm_write)
are class (b) by the basename classifier and are DELIBERATELY left on a bare
lock, because neither is written through the fenced backend path at all. See
test_scope_* at the bottom — they pin the discriminator, not just the outcome.

Stub-backend pattern mirrors test_meta_yaml_conflict_retry.py (the reference
suite named by the convention).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1]        # core/scripts
_ROOT = _SCRIPTS.parents[1]                           # PROJECT_ROOT
for _p in (str(_SCRIPTS), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _conflict_fixture as CF  # noqa: E402  (shared conflict seam, )
from mind_api.src.endpoints import experience_write as EW  # noqa: E402


class _Conflict(Exception):
    """Stand-in for OwnCloudBackend's optimistic-concurrency exception."""


class _StubBackend:
    """Identity ensure_local/refresh with call counters, plus a conflict type.

    `refresh_calls` is the load-bearing counter: it separates "re-read the local
    mirror" (ensure_local — the wedged shape) from "force-pull the remote and
    re-take the If-Match fence" (refresh — the fix). `_held` tracks lock state so
    the read-time scope assertion can observe it; _fileops.acquire_lock routes
    through the backend, so this is the real lock signal, not a proxy.
    """

    def __init__(self):
        self.conflict_error = _Conflict
        self.refresh_calls = 0
        self.ensure_local_calls = 0
        self.append_calls = 0
        self.atomic_write_calls = 0
        self._held = set()
        self.fail_appends = 0          # how many append_jsonl_record calls to fail

    def ensure_local(self, p):
        self.ensure_local_calls += 1
        return p

    def refresh(self, p):
        self.refresh_calls += 1
        return p

    def acquire_lock(self, lock_path, timeout=10, stale_seconds=30):
        self._held.add(str(lock_path))

    def release_lock(self, lock_path):
        self._held.discard(str(lock_path))

    def atomic_write(self, target, write_to_handle, *, max_retries=10):
        self.atomic_write_calls += 1

        class _Result:
            fallback_used = False
            retry_count = 0
            error_msg = ""

        with open(target, "w", encoding="utf-8") as h:
            write_to_handle(h)
        return _Result()

    def append_jsonl_record(self, path, record):
        """Mirrors OwnCloudBackend: no native append, so read-modify-write with
        a fenced PUT. Fails `fail_appends` times first to inject the conflict."""
        self.append_calls += 1
        if self.append_calls <= self.fail_appends:
            raise _Conflict("412 stale If-Match")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=True) + "\n")


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    """Retry sleeps would make these tests slow for no signal.

    Autouse, so it must stay separate from the `backend` fixture below (which
    also zeroes backoff): tests that never request `backend` still need it.
    """
    monkeypatch.setitem(CF.retry_globals(), "_conflict_backoff", lambda *_: 0)


@pytest.fixture()
def agent_dir(tmp_path):
    d = tmp_path / "agents" / "foxtrot"
    d.mkdir(parents=True)
    (d / "experience.jsonl").write_text(
        json.dumps({"id": "exp-seed", "type": "research", "category": "framework",
                    "summary": "seed record for the suite fixture",
                    "content_path": "traces/seed.md", "archived": False,
                    "retrieval_stats": {"retrieval_count": 0, "times_useful": 0,
                                        "times_noise": 0, "utility_ratio": 0.0,
                                        "last_retrieved": None},
                    "created": "2026-01-01T00:00:00"}) + "\n",
        encoding="utf-8")
    (d / "experience-archive.jsonl").write_text("", encoding="utf-8")
    traces = tmp_path / "traces"
    traces.mkdir()
    (traces / "seed.md").write_text("x" * 300, encoding="utf-8")
    (traces / "new.md").write_text("y" * 300, encoding="utf-8")
    return d


@pytest.fixture()
def backend(monkeypatch):
    # The namespace enumeration this used to hand-roll now lives once in
    # _conflict_fixture.patch_conflict_backend (). EW is passed as an
    # `extra` because experience_write binds get_backend in its OWN global at
    # import (`from storage_backend import get_backend`), so patching
    # storage_backend alone is invisible to _read_jsonl / _append_record.
    be = CF.patch_conflict_backend(monkeypatch, _StubBackend(), EW)
    return be


@pytest.fixture(autouse=True)
def _quiet_side_effects(monkeypatch):
    """history/changelog/cruft-guard write outside the tmp tree; not under test."""
    monkeypatch.setattr(EW.history, "snapshot", lambda *a, **k: None)
    monkeypatch.setattr(EW.changelog, "append", lambda *a, **k: None)
    monkeypatch.setattr(EW, "assert_not_cruft", lambda *a, **k: None)


def _ctx(agent_dir: Path, body=None, query=None):
    return SimpleNamespace(
        headers={"x-mind-agent": "foxtrot"},
        body=json.dumps(body).encode("utf-8") if body is not None else b"",
        query=query or {},
        paths=SimpleNamespace(agent=agent_dir, world=agent_dir.parent,
                              project_root=agent_dir.parent.parent),
    )


def _live(agent_dir: Path):
    return [json.loads(x) for x in
            (agent_dir / "experience.jsonl").read_text().splitlines() if x.strip()]


def _archive(agent_dir: Path):
    return [json.loads(x) for x in
            (agent_dir / "experience-archive.jsonl").read_text().splitlines() if x.strip()]


def _new_rec(rec_id="exp-new-one"):
    return {"id": rec_id, "type": "research", "category": "framework",
            "summary": "a new record with a sufficiently long summary",
            "content_path": "traces/new.md"}


# ---------------------------------------------------------------------------
# add() — the site the  incident actually wedged on
# ---------------------------------------------------------------------------

def test_add_refreshes_the_fence(agent_dir, backend):
    """Invariant 1: the cycle force-pulls instead of trusting the mirror."""
    resp = EW.add(_ctx(agent_dir, _new_rec()))

    assert resp.status == 200, "add failed: {}".format(resp.body)
    assert backend.refresh_calls >= 1, (
        "write cycle never called refresh() — it is fencing against the local "
        "mirror, which is the rb-2639 stale-IfMatch wedge")
    assert [r["id"] for r in _live(agent_dir)] == ["exp-seed", "exp-new-one"]


def test_add_retries_a_conflict_and_lands_exactly_once(agent_dir, backend):
    """Invariants 2+3: a conflict is absorbed, re-fenced, and applied ONCE.

    This is the g-115-3783 incident in miniature. Before the fix the conflict
    raised straight out as a 500 with nothing written, and repeating the call
    could not help because nothing re-fenced.
    """
    backend.fail_appends = 1

    resp = EW.add(_ctx(agent_dir, _new_rec()))

    assert resp.status == 200, "conflict was not absorbed: {}".format(resp.body)
    assert backend.append_calls == 2, "conflict was not retried"
    assert backend.refresh_calls >= 2, (
        "the retry did not RE-fence — retrying against the same stale token "
        "conflicts identically forever (the deadlock, not a transient)")
    assert [r["id"] for r in _live(agent_dir)] == ["exp-seed", "exp-new-one"], \
        "the retry must apply the append exactly once, not zero or twice"


def test_add_reraises_when_retries_exhaust(agent_dir, backend):
    """A persistent conflict must surface, never be swallowed as success."""
    backend.fail_appends = 99

    with pytest.raises(_Conflict):
        EW.add(_ctx(agent_dir, _new_rec()))

    assert [r["id"] for r in _live(agent_dir)] == ["exp-seed"], \
        "a failed write must leave the store untouched"


def test_add_read_happens_inside_the_lock(agent_dir, backend, monkeypatch):
    """Invariant 3, at the assertion point that actually discriminates.

    Hoisting the read out of the cycle while leaving refresh() and locked_rmw
    intact leaves every other test in this file GREEN — the revert is only
    visible at READ time, because a hoisted read still WRITES under the lock.

    SCOPED TO THE READS THAT PRECEDE THE WRITE, and that scoping is load-bearing
    rather than a convenience. `add()` calls `_update_meta` AFTER the cycle
    returns, which re-reads live+archive to recompute the sidecar — deliberately
    outside the lock, since it is a post-commit recompute and not part of the
    RMW. A blanket `all(reads)` therefore fails on correct code (measured:
    [True, True, False, False] — two in-cycle reads locked, two sidecar reads
    not). The invariant is specifically that the read feeding the modify-write
    is under the lock, so the ordered event log is the honest instrument.
    """
    events = []          # ordered ("read"|"write", lock_held) pairs
    real_refresh = backend.refresh
    real_append = backend.append_jsonl_record

    def spy_refresh(p):
        events.append(("read", bool(backend._held)))
        return real_refresh(p)

    def spy_append(p, r):
        events.append(("write", bool(backend._held)))
        return real_append(p, r)

    monkeypatch.setattr(backend, "refresh", spy_refresh)
    monkeypatch.setattr(backend, "append_jsonl_record", spy_append)

    EW.add(_ctx(agent_dir, _new_rec()))

    kinds = [k for k, _ in events]
    assert "write" in kinds, "nothing was written"
    first_write = kinds.index("write")
    rmw_reads = [held for k, held in events[:first_write] if k == "read"]

    assert len(rmw_reads) >= 2, (
        "expected the live AND archive reads inside the cycle, saw {} — the "
        "cycle is not force-refreshing both stores it fences on".format(rmw_reads))
    assert all(rmw_reads), (
        "a force_fresh READ happened with NO lock held ({}) — the read has been "
        "hoisted out of the locked_rmw cycle, re-opening the unlocked-RMW "
        "lost-update window a peer write slips through".format(rmw_reads))
    assert events[first_write][1], "the WRITE happened with no lock held"


def test_add_duplicate_id_still_maps_to_409(agent_dir, backend):
    """The dup-check moved inside the cycle — it must still reach the caller as
    a 409, not escape as an unhandled 500 or get retried into success."""
    resp = EW.add(_ctx(agent_dir, _new_rec("exp-seed")))

    assert resp.status == 409, "duplicate must be 409, got {}".format(resp.status)
    assert len(_live(agent_dir)) == 1, "a rejected add must not write"


# ---------------------------------------------------------------------------
# update_field() — the site where a hoisted read is actively LOSSY: the retry
# would rewrite the whole file from a pre-conflict snapshot.
# ---------------------------------------------------------------------------

def test_update_field_retries_and_preserves_a_peer_write(agent_dir, backend, monkeypatch):
    """The strongest invariant-3 pin in this file.

    A peer appends a record between the failed write and the retry. Because the
    read is INSIDE the cycle, the retry re-reads and its full-file rewrite keeps
    the peer's record. With the read hoisted, the retry would write the stale
    snapshot and the peer's record would vanish — silently, with a 200.
    """
    real_write = EW._atomic_write_with_fallback
    calls = {"n": 0}
    peer = {"id": "exp-peer", "type": "research", "category": "framework",
            "summary": "a peer record written from another box entirely",
            "content_path": "traces/new.md", "archived": False,
            "created": "2026-02-02T00:00:00"}

    def flaky_write(path, write_fn, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            # The peer's write lands while ours is rejected — exactly what a
            # 412 means: the remote moved and NOTHING of ours was written.
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(peer, ensure_ascii=True) + "\n")
            raise _Conflict("412 stale If-Match")
        return real_write(path, write_fn, **kw)

    monkeypatch.setattr(EW, "_atomic_write_with_fallback", flaky_write)

    resp = EW.update_field(_ctx(agent_dir, query={
        "id": "exp-seed", "field": "category", "value": "framework-architecture"}))

    assert resp.status == 200, "conflict not absorbed: {}".format(resp.body)
    assert calls["n"] == 2, "conflict was not retried"
    assert backend.refresh_calls >= 2, "the retry did not re-fence"

    ids = [r["id"] for r in _live(agent_dir)]
    assert "exp-peer" in ids, (
        "THE PEER'S WRITE WAS LOST ({}) — the retry rewrote the file from a "
        "pre-conflict snapshot, which means the read is outside the cycle. This "
        "is the lost-update window invariant 3 exists to close.".format(ids))
    landed = [r for r in _live(agent_dir) if r["id"] == "exp-seed"][0]
    assert landed["category"] == "framework-architecture", "the update did not land"


def test_update_field_not_found_still_maps_to_404(agent_dir, backend):
    """The two in-cycle error exits must survive the tuple-return refactor."""
    resp = EW.update_field(_ctx(agent_dir, query={
        "id": "exp-does-not-exist", "field": "category", "value": "x"}))
    assert resp.status == 404, "got {}".format(resp.status)


# ---------------------------------------------------------------------------
# archive_sweep() — two stores, two cycles. Phase 2 REMOVES records, which is
# also why a commutative merge handler is the wrong cure here (it would
# resurrect what phase 2 archived).
# ---------------------------------------------------------------------------

def _stale_rec(rec_id):
    return {"id": rec_id, "type": "research", "category": "framework",
            "summary": "a stale record old enough to sweep into the archive",
            "content_path": "traces/seed.md", "archived": True,
            "archived_date": "2026-01-02",
            "retrieval_stats": {"retrieval_count": 0, "times_useful": 0,
                                "times_noise": 0, "utility_ratio": 0.0,
                                "last_retrieved": None},
            "created": "2026-01-01T00:00:00"}


def test_archive_sweep_retries_and_applies_each_move_once(agent_dir, backend, monkeypatch):
    """Both cycles absorb a conflict; the in-cycle dedup keeps it idempotent."""
    (agent_dir / "experience.jsonl").write_text(
        "\n".join(json.dumps(_stale_rec(i)) for i in ("exp-a", "exp-b")) + "\n",
        encoding="utf-8")

    real_write = EW._atomic_write_with_fallback
    calls = {"n": 0}

    def flaky_write(path, write_fn, **kw):
        calls["n"] += 1
        if calls["n"] in (1, 2):      # fail the first attempt of EACH cycle
            raise _Conflict("412 stale If-Match")
        return real_write(path, write_fn, **kw)

    monkeypatch.setattr(EW, "_atomic_write_with_fallback", flaky_write)

    resp = EW.archive_sweep(_ctx(agent_dir))

    assert resp.status == 200, "sweep failed: {}".format(resp.body)
    arch_ids = [r["id"] for r in _archive(agent_dir)]
    assert arch_ids == ["exp-a", "exp-b"], (
        "the retry must append each record exactly once, not twice — the "
        "in-cycle `existing` set is what makes it idempotent: {}".format(arch_ids))
    assert _live(agent_dir) == [], "phase 2 did not remove the archived records"


# ---------------------------------------------------------------------------
# archive_goal() — the site with retry semantics NOTHING else here exercises.
# Its cycle REBINDS `canonical` (nonlocal) and does an os.replace on the
# filesystem, so a retry re-enters a cycle whose prior attempt already moved a
# file. add()/update_field()/archive_sweep() are all pure in-cycle recomputes by
# comparison, so a defect in this branch would be invisible to every other test.
# ---------------------------------------------------------------------------

def _archive_goal_body(tmp_root: Path, goal="g-115-1"):
    trace = tmp_root / "traces" / "new.md"
    return {"goal": goal, "skill_slug": "aspirations-execute",
            "category": "framework",
            "summary": "an archive-goal summary of sufficient length",
            "trace_file": str(trace.relative_to(tmp_root))}


def test_archive_goal_retries_and_lands_exactly_once(agent_dir, backend):
    """Baseline retry: no id race, so `canonical` is never rebound."""
    backend.fail_appends = 1
    root = agent_dir.parent.parent

    resp = EW.archive_goal(_ctx(agent_dir, _archive_goal_body(root)))

    assert resp.status == 200, "conflict not absorbed: {}".format(resp.body)
    assert backend.append_calls == 2, "conflict was not retried"
    assert backend.refresh_calls >= 2, "the retry did not re-fence"
    ids = [r["id"] for r in _live(agent_dir)]
    assert ids == ["exp-seed", "exp-g-115-1-aspirations-execute"], \
        "the retry must append exactly once: {}".format(ids)
    landed = _live(agent_dir)[-1]
    assert (root / landed["content_path"]).exists(), \
        "content_path points at a file that is not on disk"


def test_archive_goal_race_branch_is_correct_under_retry(agent_dir, backend, monkeypatch):
    """A PEER takes our id in the same 412 that rejects our write.

    This is the exact meaning of a conflict — the remote moved and nothing of
    ours landed — so it is the realistic way the in-cycle race branch gets
    entered on attempt 2. It rebinds `canonical` and renames the already-copied
    .md; the record must then land under the NEW id with its content_path
    pointing at the renamed file, and the old path must be gone.

    Without `nonlocal canonical` this raises UnboundLocalError; with a stale
    `canonical` it would leave an orphaned .md and a content_path that 404s.
    """
    root = agent_dir.parent.parent
    real_append = backend.append_jsonl_record
    calls = {"n": 0}
    stolen = "exp-g-115-1-aspirations-execute"

    def racing_append(path, record):
        calls["n"] += 1
        if calls["n"] == 1:
            # Peer's record lands; ours is rejected.
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"id": stolen, "type": "research",
                                    "category": "framework",
                                    "summary": "peer took this id first",
                                    "content_path": "traces/seed.md",
                                    "created": "2026-03-03T00:00:00"}) + "\n")
            raise _Conflict("412 stale If-Match")
        return real_append(path, record)

    monkeypatch.setattr(backend, "append_jsonl_record", racing_append)

    resp = EW.archive_goal(_ctx(agent_dir, _archive_goal_body(root)))

    assert resp.status == 200, "race branch failed under retry: {}".format(resp.body)
    ids = [r["id"] for r in _live(agent_dir)]
    assert stolen in ids, "the peer's record should still be there"
    ours = [i for i in ids if i.startswith("exp-g-115-1-aspirations-execute") and i != stolen]
    assert len(ours) == 1, (
        "expected exactly one re-uniquified record of ours, got {}".format(ids))

    landed = [r for r in _live(agent_dir) if r["id"] == ours[0]][0]
    assert (root / landed["content_path"]).exists(), (
        "content_path points at a file that is not on disk — `canonical` was "
        "not rebound through the retry (the nonlocal), so the .md was left at "
        "the pre-rename path")
    assert not (agent_dir / "experience" / f"{stolen}.md").exists(), (
        "the pre-rename .md was left behind as an orphan alongside the renamed one")


# ---------------------------------------------------------------------------
# SCOPE. These pin the DISCRIMINATOR behind leaving two class-(b) stores on a
# bare lock, so a future audit reading only the basename classifier does not
# "complete" this goal by converting them. The classifier says class (b) for
# both; the write path says there is no fence to go stale.
# ---------------------------------------------------------------------------

def test_scope_meta_update_issues_no_backend_write(agent_dir, backend):
    """experience-meta.json: raw json.load + raw tmp/os.replace, no fenced PUT.

    If this ever starts routing through the backend, the store acquires an
    If-Match fence and DOES need locked_rmw — this test failing is the signal to
    convert it, not to relax the test.
    """
    before = (backend.append_calls, backend.atomic_write_calls)

    resp = EW.meta_update(_ctx(agent_dir, query={"field": "note", "value": "x"}))

    assert resp.status == 200, "meta_update failed: {}".format(resp.body)
    assert (backend.append_calls, backend.atomic_write_calls) == before, (
        "meta_update now writes THROUGH the backend, so experience-meta.json "
        "takes an If-Match fence that can go stale — it must be converted to "
        "locked_rmw like its five siblings (g-115-3719)")
    assert json.loads((agent_dir / "experience-meta.json").read_text())["note"] == "x"


def test_scope_wm_write_issues_no_backend_write(tmp_path):
    """working-memory.yaml: _write_wm is a documented raw local write.

    Audited 2026-06-02 and deliberately excluded from the #38 own-cloud RMW
    conflict-retry (per-agent single-writer, hot path, tier-model integrity — it
    reaches S3 via the sweep). It therefore cannot raise the backend's
    ConflictError, and wrapping it in locked_rmw would retry an exception the
    path cannot produce while contradicting that tier decision.

    Pinned by SOURCE inspection rather than a call-count spy because the point
    is the absence of a backend reference on the write path at all.

    EXECUTABLE CODE ONLY — the docstring is stripped before matching. Both
    functions DISCUSS get_backend in prose (that is precisely where the
    exclusion is justified), so a raw substring check on the source fails on
    correct code. Caught by this test on its first run.
    """
    import ast
    import inspect
    import textwrap

    from mind_api.src.endpoints import wm_write as WM

    def _code_of(fn) -> str:
        node = ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]
        body = node.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            node.body = body[1:]          # drop the docstring
        return ast.unparse(node)

    write_code = _code_of(WM._write_wm)
    assert "get_backend" not in write_code, (
        "_write_wm now references the backend — working-memory.yaml may have "
        "acquired an If-Match fence, which changes its write class (g-115-3719)")
    assert "replace" in write_code, \
        "_write_wm is no longer the raw local tmp+replace this test assumes"

    read_code = _code_of(WM._read_yaml)
    assert "refresh" not in read_code, (
        "_read_yaml now force-refreshes — WM has joined the fenced backend path "
        "and its write class must be re-derived (g-115-3719)")


# ---------------------------------------------------------------------------
#  — exhausting the retry cap must CLEAN UP, not just raise.
#
# test_add_reraises_when_retries_exhaust (above) proves the re-raise fires and
# stops there. Proving an error path fires is not proving it cleans up. For
# archive_goal the difference is a permanent wedge: it copy2's the trace to
# `canonical` BEFORE the write, and the backend's ConflictError is not an
# OSError, so before the fix it flew past `except OSError`, `_abort` never ran,
# and the orphan .md stayed on disk. The pre-lock guard then returns 409
# content_path_exists for that experience_id FOREVER (rb-2639 shape).
# ---------------------------------------------------------------------------

def test_archive_goal_conflict_exhaustion_leaves_no_orphan_md(agent_dir, backend):
    """Exhaust the cap, then assert the canonical .md is ABSENT.

    The two file assertions are the point; the status assertion alone would
    pass against a bare `raise` that stranded the copy.
    """
    backend.fail_appends = CF.retry_globals()["_CONFLICT_RETRY_CAP"]  # every attempt conflicts
    root = agent_dir.parent.parent
    canonical = agent_dir / "experience" / "exp-g-115-1-aspirations-execute.md"
    trace_src = root / "traces" / "new.md"
    assert trace_src.exists(), "fixture precondition: the trace source must exist"

    resp = EW.archive_goal(_ctx(agent_dir, _archive_goal_body(root)))

    # 1. The conflict is a clean response, not an escaped exception.
    assert resp.status == 409, \
        "conflict escaped as an unhandled exception: {}".format(resp.body)
    body_txt = resp.body.decode("utf-8") if isinstance(resp.body, bytes) else str(resp.body)
    assert "write_conflict" in body_txt, \
        "wrong error contract for an exhausted conflict: {}".format(body_txt)

    # 2. THE CLEANUP PROOF — _abort ran and removed the copy.
    assert not canonical.exists(), (
        "orphan .md survived conflict exhaustion at {} — every later "
        "archive_goal for this experience_id now 409s content_path_exists "
        "forever (g-115-3837)".format(canonical))

    # 3. The other half of _abort's stated contract: the caller's trace is intact,
    #    so the operation is idempotent and the caller can retry ().
    assert trace_src.exists(), \
        "failed attempt consumed the caller's trace source — retry is now impossible"

    # 4. No record landed, so the id is genuinely free for a retry.
    assert [r["id"] for r in _live(agent_dir)] == ["exp-seed"], \
        "a record landed despite every write attempt conflicting"
