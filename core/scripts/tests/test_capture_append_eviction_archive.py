""" outcome [2] — the APPEND path archives a capture victim BEFORE it pops it.

THE DEFECT. A worker Body's capture lanes sit saturated (measured on alpha/cc-08,
2026-09-14, through wm-read.sh: spark_capture 50/50, exp_capture 20/20,
hyp_capture 10/10), so EVERY capture append evicts one older entry. The append
path popped that entry with no copy anywhere. g-115-9662 gave the PRUNE path an
archive-before-delete and named this one as its residual ("the APPEND path still
evicts capture entries with no archive ... needs its own design pass").

THE PREMISE THE OUTCOME CARRIES, CORRECTED. Outcome [2] says "while the carrier is
known-undelivered". An UNFLAGGED capture is never mirrored to the carrier at all
(the carrier is flagged-only), so for it the carrier's state is irrelevant: the
Body WM is its only copy whether the carrier is healthy or wedged (guard-6181). A
guard keyed on carrier state would only DEFER that loss to the next healthy push.
So every append-path victim on a Body WM is archived, flag and carrier state
regardless — which covers "while known-undelivered" as a subset.

WHY THE SINK IS LOCAL AND BESIDE THE BODY WM, not prune's agent-dir archive:
  * agents/<agent>/ is refused NoClaimError from every box without the runner
    claim, i.e. every worker box (measured on cc-08: owned agents = []);
  * that archive appends through the storage backend, and store I/O inside the
    stale-breakable 10 s WM lock is a stale-break generator (guard-1965; the
    body-merge g-115-8667 note);
  * sessions/ is machine-local — never synced, never rewritten by a PUT, never
    fenced — so a plain local append is safe there, and it shares the blast
    radius of the Body WM the entry came from, so nothing gets less durable.

WHY THE STAGING TESTS BELONG HERE. The reaper rm -rf's the session dir, so an
archive beside the WM that the reaper did not carry would only move the loss from
the eviction to the reap. The archive is therefore staged beside the WM at a
remote Body's genuine close and at every reap, and pushed with it.

TWIN-COPY TRAP (guard-742/547): wm-append.sh is daemon-only, so the daemon copy in
mind_api/src/endpoints/wm_write.py is the LIVE path and is tested end to end here;
the wm.py twin is tested directly and pinned structurally.
"""
from __future__ import annotations

import datetime
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

TESTS_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = TESTS_DIR.parent
REPO = CORE_SCRIPTS.parent.parent
for _p in (str(CORE_SCRIPTS), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import wm  # noqa: E402
from _bash_helpers import BASH  # noqa: E402

DAEMON_SRC = REPO / "mind_api" / "src" / "endpoints" / "wm_write.py"
CLEANUP_SH = CORE_SCRIPTS / "cleanup-stale-bindings.sh"
SESSION_MANIFEST = REPO / "core" / "config" / "session-manifest.yaml"
ARCHIVE = "capture-evictions-archive.jsonl"
SIDECAR_SUFFIX = "-capture-evictions-archive.jsonl"
REASON = "append_array_limit"
SID = "5a5a5a5a-9852-4c02-8a00-000000000002"


def _load_hyphen(modname: str, filename: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(modname, CORE_SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rows(path: Path) -> list:
    if not path.is_file():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _entry(n: int, load_bearing: bool = False) -> dict:
    return {"goal_id": f"g-115-9852-{n:03d}", "observation": f"worker capture {n}",
            "load_bearing": load_bearing,
            "_item_ts": f"2026-09-14T01:{n // 60:02d}:{n % 60:02d}"}


# ─────────────────────────── the local archive helper ───────────────────────────

def test_local_archive_writes_one_parseable_row_carrying_the_entry(tmp_path):
    target = tmp_path / ARCHIVE
    assert wm.archive_evicted_capture_local(target, "spark_capture", _entry(1), REASON) is True
    assert wm.archive_evicted_capture_local(target, "spark_capture", _entry(2), REASON) is True
    rows = _rows(target)
    assert [r["entry"]["goal_id"] for r in rows] == ["g-115-9852-001", "g-115-9852-002"]
    for r in rows:
        assert r["slot"] == "spark_capture" and r["eviction_reason"] == REASON
        assert r["archived_at"] and "?" not in r["summary"]
    assert "worker capture 1" in rows[0]["summary"], (
        "the row must name what was dropped (g-115-9662), not just record that something was")


def test_local_archive_refuses_with_false_when_the_sink_is_unwritable(tmp_path):
    """False is the caller's instruction to KEEP the entry — a helper that raised,
    or returned True on failure, would re-arm the silent destroy."""
    target = tmp_path / ARCHIVE
    target.mkdir()  # a DIRECTORY where the file belongs: the append must fail
    assert wm.archive_evicted_capture_local(target, "spark_capture", _entry(1), REASON) is False


# ─────────────────────────── enforce_slot_limit's may_evict contract ───────────────────────────

def test_may_evict_false_keeps_the_victim_and_terminates():
    """`break`, never `continue`: the loop re-tests the same over-limit condition,
    so a refusal that did not stop the loop would spin forever — the call count
    is what proves it stopped."""
    arr = [_entry(i) for i in range(12)]
    calls = []

    def refuse(victim):
        calls.append(victim["goal_id"])
        return False

    assert wm.enforce_slot_limit(arr, 10, may_evict=refuse) == 0
    assert len(arr) == 12, "a refused eviction must not remove anything"
    assert calls == ["g-115-9852-000"], (
        f"may_evict must be asked once about the oldest victim, then stop: {calls}")


def test_may_evict_is_asked_about_exactly_the_entries_the_policy_evicts():
    """Archive-before-pop is only worth anything if the ARCHIVED entry is the
    POPPED one. Compare against the unchanged policy on an identical lane with a
    mixed flag population, so the floor-aware victim choice is exercised."""
    lane = ([_entry(i, load_bearing=True) for i in range(9)]
            + [_entry(100 + i) for i in range(5)])
    plain = [dict(x) for x in lane]
    seen = []
    archived = [dict(x) for x in lane]

    def record(victim):
        seen.append(victim["goal_id"])
        return True

    n_plain = wm.enforce_slot_limit(plain, 10)
    n_arch = wm.enforce_slot_limit(archived, 10, may_evict=record)
    survivors_plain = sorted(x["goal_id"] for x in plain)
    assert n_arch == n_plain == 4
    assert sorted(x["goal_id"] for x in archived) == survivors_plain
    evicted = sorted(set(x["goal_id"] for x in lane) - set(survivors_plain))
    assert sorted(seen) == evicted, (
        f"may_evict saw {sorted(seen)} but the policy evicted {evicted}")


def test_no_may_evict_leaves_the_merge_caller_unchanged():
    """body-merge.py calls enforce_slot_limit with no callback; its behaviour must
    not move. Positive control for the two tests above."""
    arr = [_entry(i) for i in range(12)]
    assert wm.enforce_slot_limit(arr, 10) == 2
    assert [x["goal_id"] for x in arr][:1] == ["g-115-9852-002"]


# ─────────────────────────── CLI twin, end to end (cmd_append) ───────────────────────────

LANE_CLI = "hyp_capture"  # a real CAPTURE_SLOT with the smallest real cap


def _real_cap(slot: str) -> int:
    cap = wm.get_pruning_config(wm.read_config()).get("array_limits", {}).get(slot)
    assert isinstance(cap, int) and cap >= 2, f"{slot} has no usable cap: {cap!r}"
    return cap


class _BodyWM:
    """Redirect the CLI WM to a Body-SHAPED path via BODY_WM_PATH (guard-862:
    patching wm.WM_PATH is a no-op for I/O and would target the live WM)."""

    def __init__(self, body_shaped: bool = True):
        self.body_shaped = body_shaped

    def __enter__(self):
        self._orig = os.environ.get("BODY_WM_PATH")
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name) / "agents" / "alpha"
        d = root / "sessions" / SID if self.body_shaped else root / "session"
        d.mkdir(parents=True)
        self.path = d / "working-memory.yaml"
        os.environ["BODY_WM_PATH"] = str(self.path)
        wm.cmd_init(SimpleNamespace())
        return self

    def seed(self, slot: str, items: list) -> None:
        data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        data.setdefault("slots", {})[slot] = items
        self.path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    def lane(self, slot: str) -> list:
        data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        return (data.get("slots") or {}).get(slot) or []

    def __exit__(self, *exc):
        if self._orig is None:
            os.environ.pop("BODY_WM_PATH", None)
        else:
            os.environ["BODY_WM_PATH"] = self._orig
        self._tmp.cleanup()
        return False


def _cli_append(slot: str, item: dict, capsys) -> dict:
    saved = sys.stdin
    sys.stdin = io.StringIO(json.dumps(item))
    try:
        capsys.readouterr()
        wm.cmd_append(SimpleNamespace(slot=slot))
    finally:
        sys.stdin = saved
    err = capsys.readouterr().err
    for line in err.splitlines():
        if line.startswith("{") and '"evicted"' in line:
            return json.loads(line)
    return {}


def test_cli_body_append_archives_the_victim_before_removing_it(capsys):
    cap = _real_cap(LANE_CLI)
    with _BodyWM() as b:
        b.seed(LANE_CLI, [_entry(i) for i in range(cap)])
        out = _cli_append(LANE_CLI, _entry(900), capsys)
        rows = _rows(b.path.parent / ARCHIVE)
        assert out.get("evicted") == 1 and out.get("evicted_archived") == 1, out
        assert out.get("eviction_deferred") == 0, out
        assert len(b.lane(LANE_CLI)) == cap
        assert [r["entry"]["goal_id"] for r in rows] == ["g-115-9852-000"], rows
        assert rows[0]["entry"]["observation"] == "worker capture 0"


def test_cli_agent_wide_append_does_not_archive(capsys):
    """Scope is the worker Body WM. The agent-wide WM keeps today's behaviour,
    reported honestly: evicted_archived is null (no archive applies), never 0."""
    cap = _real_cap(LANE_CLI)
    with _BodyWM(body_shaped=False) as b:
        b.seed(LANE_CLI, [_entry(i) for i in range(cap)])
        out = _cli_append(LANE_CLI, _entry(900), capsys)
        assert out.get("evicted") == 1, out
        assert out.get("evicted_archived") is None, out
        assert not (b.path.parent / ARCHIVE).exists()


def test_cli_failed_archive_keeps_the_entry_over_cap(capsys):
    cap = _real_cap(LANE_CLI)
    with _BodyWM() as b:
        b.seed(LANE_CLI, [_entry(i) for i in range(cap)])
        (b.path.parent / ARCHIVE).mkdir()
        out = _cli_append(LANE_CLI, _entry(900), capsys)
        assert out.get("evicted") == 0 and out.get("eviction_deferred") == 1, out
        assert len(b.lane(LANE_CLI)) == cap + 1, "an unarchivable victim must be KEPT"


def test_cli_ceiling_bounds_growth_when_the_archive_keeps_failing(capsys):
    cap = _real_cap(LANE_CLI)
    ceiling = cap * wm.EVICTION_ARCHIVE_CEILING
    with _BodyWM() as b:
        b.seed(LANE_CLI, [_entry(i) for i in range(ceiling)])
        (b.path.parent / ARCHIVE).mkdir()
        out = _cli_append(LANE_CLI, _entry(900), capsys)
        assert len(b.lane(LANE_CLI)) == ceiling, "keep-on-failure must stop at the ceiling"
        assert out.get("evicted") == 1 and out.get("evicted_archived") == 0, out
        assert out.get("eviction_deferred") == ceiling - cap, out


# ─────────────────────────── DAEMON (the LIVE path), end to end ───────────────────────────

LANE = "spark_capture"
SEEDED_CAP = 5
PRUNING = {"working_memory_pruning": {
    "stale_threshold_minutes": 30, "evict_threshold_minutes": 120,
    "array_limits": {LANE: SEEDED_CAP}, "item_stale_minutes": {},
    "protected_slots": ["known_blockers", "knowledge_debt"]}}


def _post_append(port: int, slot: str, item: dict, sid: str | None) -> dict:
    url = f"http://127.0.0.1:{port}/v1/wm/append?" + urllib.parse.urlencode({"slot": slot})
    req = urllib.request.Request(url, data=json.dumps(item).encode("utf-8"), method="POST")
    req.add_header("X-Mind-Agent", "alpha")
    if sid:
        req.add_header("X-Mind-Sid", sid)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _daemon_append(seed: list, item: dict, *, body: bool = True, sabotage: bool = False):
    """Fresh in-process daemon over a fresh tmp world (rb-659): seed a lane, append
    once, return (response, lane after, archived rows, every archive file under agents/)."""
    from _daemon_fixture import DaemonFixture

    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd) / "world"
        world.mkdir()
        with DaemonFixture(world, agent="alpha") as df:
            cfg = df.project_root / "core" / "config"
            cfg.mkdir(parents=True, exist_ok=True)
            (cfg / "memory-pipeline.yaml").write_text(yaml.safe_dump(PRUNING), encoding="utf-8")
            agent_dir = df.project_root / "agents" / "alpha"
            wm_file = (agent_dir / "sessions" / SID if body else agent_dir / "session") / "working-memory.yaml"
            wm_file.parent.mkdir(parents=True, exist_ok=True)
            now = datetime.datetime.now().isoformat()
            wm_file.write_text(yaml.safe_dump({
                "session_start": now, "slots": {LANE: seed},
                "slot_meta": {LANE: {"updated_at": now, "accessed_at": now, "update_count": 1}},
            }), encoding="utf-8")
            if sabotage:
                (wm_file.parent / ARCHIVE).mkdir()
            resp = _post_append(df.port, LANE, item, SID if body else None)
            lane = (yaml.safe_load(wm_file.read_text(encoding="utf-8"))["slots"] or {}).get(LANE) or []
            rows = _rows(wm_file.parent / ARCHIVE)
            files = sorted(p.relative_to(df.project_root).as_posix()
                           for p in (df.project_root / "agents").rglob(ARCHIVE) if p.is_file())
            return resp, lane, rows, files


def test_daemon_body_append_archives_the_unflagged_victim():
    resp, lane, rows, files = _daemon_append([_entry(i) for i in range(SEEDED_CAP)], _entry(900))
    assert resp["evicted"] == 1 and resp["evicted_archived"] == 1, resp
    assert resp["eviction_deferred"] == 0, resp
    assert len(lane) == SEEDED_CAP
    assert [r["entry"]["goal_id"] for r in rows] == ["g-115-9852-000"], rows
    assert rows[0]["entry"]["observation"] == "worker capture 0", (
        "the row must carry the ENTRY, not a note that one existed")
    assert rows[0]["eviction_reason"] == REASON and rows[0]["slot"] == LANE
    assert files == [f"agents/alpha/sessions/{SID}/{ARCHIVE}"], (
        f"the archive must sit beside the Body WM and nowhere else: {files}")


def test_daemon_body_append_archives_a_flagged_victim_too():
    """Flag-neutral on purpose: 'flagged == carrier-backed' can be false without any
    push failure (the g-115-9852 progress note, item 5), so the archive does not
    trust it."""
    seed = [_entry(i, load_bearing=True) for i in range(SEEDED_CAP)]
    resp, lane, rows, _ = _daemon_append(seed, _entry(900, load_bearing=True))
    assert resp["evicted"] == 1 and resp["evicted_archived"] == 1, resp
    assert [r["entry"]["goal_id"] for r in rows] == ["g-115-9852-000"]


def test_daemon_failed_archive_keeps_the_entry_and_says_so():
    resp, lane, rows, _ = _daemon_append([_entry(i) for i in range(SEEDED_CAP)], _entry(900),
                                         sabotage=True)
    assert rows == []
    assert resp["evicted"] == 0 and resp["eviction_deferred"] == 1, resp
    assert resp["evicted_archived"] == 0, (
        "an archive that APPLIED and failed reports 0, which is a different fact "
        f"from null (no archive applies): {resp}")
    assert len(lane) == SEEDED_CAP + 1, "a victim that cannot be archived must be KEPT"


def test_daemon_ceiling_bounds_growth_and_reports_the_unarchived_drop():
    ceiling = SEEDED_CAP * 2
    resp, lane, rows, _ = _daemon_append([_entry(i) for i in range(ceiling)], _entry(900),
                                         sabotage=True)
    assert len(lane) == ceiling, f"keep-on-failure must stop at the ceiling, got {len(lane)}"
    assert resp["evicted"] == 1 and resp["evicted_archived"] == 0, resp
    assert resp["eviction_deferred"] == ceiling - SEEDED_CAP, resp


def test_daemon_agent_wide_append_reports_no_archive_and_writes_none():
    resp, lane, rows, files = _daemon_append([_entry(i) for i in range(SEEDED_CAP)], _entry(900),
                                             body=False)
    assert resp["evicted"] == 1 and resp["evicted_archived"] is None, resp
    assert resp["eviction_deferred"] == 0
    assert files == [], f"no archive may be written for a non-Body WM: {files}"


def test_daemon_under_cap_append_attempts_no_archive():
    resp, lane, rows, files = _daemon_append([_entry(i) for i in range(SEEDED_CAP - 1)], _entry(900))
    assert resp["evicted"] == 0 and resp["evicted_archived"] is None, resp
    assert resp["eviction_deferred"] == 0 and files == []


# ─────────────────────────── the archive travels with the WM ───────────────────────────

@pytest.fixture
def hermetic_world(tmp_path, monkeypatch):
    """Point the world-rooted staging dir at tmp ( fixture reasoning:
    without it a producer test stages into the LIVE world/, guard-955)."""
    import _paths
    w = tmp_path / "world"
    w.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_paths, "WORLD_DIR", w, raising=False)
    return w


def _repo_skeleton(tmp_path: Path) -> Path:
    dst = tmp_path / "core" / "scripts"
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy2(CLEANUP_SH, dst / "cleanup-stale-bindings.sh")
    return dst / "cleanup-stale-bindings.sh"


def _call_preserve(script: Path, body_dir: Path):
    code = (f'source "{script.as_posix()}"; '
            f'_preserve_unmerged_body_wm "alpha" "{body_dir.as_posix()}" "{SID}"')
    return subprocess.run([BASH, "-c", code], capture_output=True, text=True, timeout=60)


def _body_dir(tmp_path: Path, state: str, archive_bytes: bytes | None = b'{"slot": "spark_capture"}\n'):
    bd = tmp_path / "agents" / "alpha" / "sessions" / SID
    bd.mkdir(parents=True, exist_ok=True)
    (bd / "working-memory.yaml").write_text("slots:\n  forked: true\n", encoding="utf-8")
    (bd / "body-manifest.yaml").write_text(
        f"unitKey: {SID}\nmindKey: alpha\nbody_state: {state}\n", encoding="utf-8")
    if archive_bytes is not None:
        (bd / ARCHIVE).write_bytes(archive_bytes)
    return bd


def _legacy(tmp_path: Path) -> Path:
    return tmp_path / "agents" / "alpha" / "session" / "pending-body-merges"


@pytest.mark.parametrize("state", ["closed-pending-merge", "active", "merged"])
def test_reaper_stages_the_archive_before_the_session_dir_is_deleted(tmp_path, state):
    """cleanup-stale-bindings.sh rm -rf's the session dir right after this function.
    `merged` is the case that matters most: the WM is (correctly) NOT re-staged
    there, and without its own branch the archive would die with the dir."""
    script = _repo_skeleton(tmp_path)
    payload = b'{"slot": "spark_capture", "entry": {"goal_id": "g-1"}}\n'
    bd = _body_dir(tmp_path, state, payload)
    r = _call_preserve(script, bd)
    assert r.returncode == 0, r.stderr
    staged = _legacy(tmp_path) / f"{SID}{SIDECAR_SUFFIX}"
    assert staged.is_file(), f"archive not staged for body_state={state}; stderr={r.stderr}"
    assert staged.read_bytes() == payload, "the staged archive must be byte-identical"
    wm_staged = _legacy(tmp_path) / f"{SID}-wm.yaml"
    assert wm_staged.exists() is (state != "merged"), (
        "the merged double-merge guard must still hold for the WM itself")


def test_reaper_stages_no_sidecar_when_nothing_was_evicted(tmp_path):
    script = _repo_skeleton(tmp_path)
    bd = _body_dir(tmp_path, "closed-pending-merge", archive_bytes=None)
    r = _call_preserve(script, bd)
    assert r.returncode == 0, r.stderr
    assert (_legacy(tmp_path) / f"{SID}-wm.yaml").is_file(), "positive control: the WM staged"
    assert not (_legacy(tmp_path) / f"{SID}{SIDECAR_SUFFIX}").exists()


def test_reaper_copies_the_archive_before_the_trigger_and_before_the_merged_return():
    """Source order governs, not mtimes (the existing trigger-last pin's reasoning)."""
    src = CLEANUP_SH.read_text(encoding="utf-8")
    body = src[src.index("_preserve_unmerged_body_wm() {"):]
    body = body[: body.index("\n}\n")]
    code = "\n".join(l for l in body.splitlines() if not l.strip().startswith("#"))
    arch_at = code.index(SIDECAR_SUFFIX)
    assert arch_at < code.index('"$_STAGE_DIR/${_SID}-wm.yaml"'), "archive must precede the trigger"
    assert arch_at < code.index('if [ "$_STATE" = "merged" ]; then'), (
        "archive must be staged before the merged early return")


@pytest.fixture
def captured_puts(monkeypatch):
    pushed = []

    class _FakeBackend:
        def write_bytes(self, path, content):
            pushed.append((Path(path).name, content))

    mod = type(sys)("storage_backend")
    mod.get_backend = lambda: _FakeBackend()
    monkeypatch.setitem(sys.modules, "storage_backend", mod)
    return pushed


def _remote_body(tmp_path: Path, archive_bytes: bytes | None):
    sess = tmp_path / "agents" / "alpha" / "sessions" / SID
    sess.mkdir(parents=True)
    (tmp_path / "agents" / "alpha" / "session").mkdir(parents=True)
    (sess / "body-manifest.yaml").write_text(
        "\n".join([f"unitKey: '{SID}'", "mindKey: 'alpha'", "role: 'worker'",
                   "body_state: 'active'", "forked_wm_hash: 'abc123'",
                   "remote_body: true"]) + "\n", encoding="utf-8")
    (sess / "working-memory.yaml").write_bytes(b"slots: {}\n")
    (sess / "body-closing").write_text("", encoding="utf-8")
    if archive_bytes is not None:
        (sess / ARCHIVE).write_bytes(archive_bytes)
    return sess


def test_genuine_close_stages_and_pushes_the_archive_before_the_trigger(
        tmp_path, hermetic_world, captured_puts):
    bm = _load_hyphen("body_manifest_under_test_9852", "body-manifest.py")
    payload = b'{"slot": "exp_capture"}\n'
    _remote_body(tmp_path, payload)
    assert bm.close_body_on_genuine(SID, "alpha", tmp_path) == "marked"
    staged = hermetic_world / "body-staged-wm" / "alpha"
    assert (staged / f"{SID}{SIDECAR_SUFFIX}").read_bytes() == payload
    names = [n for n, _ in captured_puts]
    assert f"{SID}{SIDECAR_SUFFIX}" in names, f"archive never pushed: {names}"
    assert names.index(f"{SID}{SIDECAR_SUFFIX}") < names.index(f"{SID}-wm.yaml"), (
        f"the -wm.yaml trigger must be pushed LAST: {names}")


def test_genuine_close_without_an_archive_is_unchanged(tmp_path, hermetic_world, captured_puts):
    bm = _load_hyphen("body_manifest_under_test_9852b", "body-manifest.py")
    _remote_body(tmp_path, None)
    assert bm.close_body_on_genuine(SID, "alpha", tmp_path) == "marked"
    names = [n for n, _ in captured_puts]
    assert f"{SID}-wm.yaml" in names and f"{SID}{SIDECAR_SUFFIX}" not in names, names


def test_reap_relocates_and_pushes_the_legacy_staged_archive(tmp_path, hermetic_world, captured_puts):
    """The bash reaper stages to the LEGACY dir; push-staged must relocate the
    archive world-rooted and push it, or it is stranded on the worker box."""
    bm = _load_hyphen("body_manifest_under_test_9852c", "body-manifest.py")
    legacy = _legacy(tmp_path)
    legacy.mkdir(parents=True)
    (legacy / f"{SID}{SIDECAR_SUFFIX}").write_bytes(b'{"slot": "hyp_capture"}\n')
    (legacy / f"{SID}-wm.yaml").write_bytes(b"slots: {}\n")
    copied = bm.relocate_legacy_staging(tmp_path / "agents" / "alpha" / "session", SID)
    assert f"{SID}{SIDECAR_SUFFIX}" in copied, copied
    world_staged = hermetic_world / "body-staged-wm" / "alpha"
    assert bm.push_staged_files(world_staged, SID) is True
    names = [n for n, _ in captured_puts]
    assert names.index(f"{SID}{SIDECAR_SUFFIX}") < names.index(f"{SID}-wm.yaml"), names


def test_consumer_merges_the_wm_and_leaves_the_archive_in_place(tmp_path, hermetic_world):
    """The reducer consumes the triple and must NOT consume or delete the archive:
    in world/body-staged-wm it IS the durable copy of every entry the Body evicted."""
    merge = _load_hyphen("body_merge_under_test_9852", "body-merge.py")
    state = tmp_path / "agents" / "alpha" / "session"
    state.mkdir(parents=True)
    (state / "working-memory.yaml").write_text("slots:\n  x: 1\n", encoding="utf-8")
    legacy = _legacy(tmp_path)
    legacy.mkdir(parents=True)
    (legacy / f"{SID}-wm.yaml").write_text("slots:\n  body_only: 1\n", encoding="utf-8")
    sidecar = legacy / f"{SID}{SIDECAR_SUFFIX}"
    sidecar.write_bytes(b'{"slot": "spark_capture"}\n')
    summary = merge.generalize_down("alpha", project_root=tmp_path)
    assert SID in summary["staged_merged"], summary
    assert not (legacy / f"{SID}-wm.yaml").exists(), "positive control: the WM was consumed"
    assert sidecar.is_file(), "the consumer must never delete the eviction archive"


# ─────────────────────────── registrations and twin pins ───────────────────────────

def test_session_manifest_registers_the_staged_archive_glob():
    data = yaml.safe_load(SESSION_MANIFEST.read_text(encoding="utf-8"))
    entries = [e for e in data["files"] if e.get("file") == f"*{SIDECAR_SUFFIX}"]
    assert len(entries) == 1, "the staged eviction archive must be registered exactly once"
    e = entries[0]
    assert e.get("glob") is True and e.get("sync_tier") == "continuity"
    assert e.get("recovery_action") == "preserve", (
        "clearing it on /start --recover would destroy the only copy of evicted captures")


def test_one_basename_across_every_writer():
    """The Body WM writers, the close stager and the bash reaper must name the same
    file, or the archive is written under one name and staged under another —
    silently, because every stager skips a file that is not there."""
    bm = _load_hyphen("body_manifest_under_test_9852d", "body-manifest.py")
    assert wm.CAPTURE_EVICTION_ARCHIVE == ARCHIVE
    assert bm._EVICTIONS_FILENAME == ARCHIVE
    assert bm._STAGED_EVICTIONS_SUFFIX == SIDECAR_SUFFIX
    sh = CLEANUP_SH.read_text(encoding="utf-8")
    assert f'"$_SD/{ARCHIVE}"' in sh and f"${{_SID}}{SIDECAR_SUFFIX}" in sh


def test_both_twins_archive_inside_the_append_eviction_loop():
    """The daemon keeps its loop INLINE (test_capture_fast_lane pins
    key=_eviction_sort_key there), so the pin is on its source; the CLI routes
    through enforce_slot_limit's may_evict."""
    src = DAEMON_SRC.read_text(encoding="utf-8")
    body = src.split("def append_slot(", 1)[1].split("\ndef ", 1)[0]
    loop = body[body.index("while len(arr) > limit:"):body.index("_record_capture_evictions(")]
    assert "archive_evicted_capture_local(" in loop, "daemon append loop lost its archive call"
    assert loop.index("archive_evicted_capture_local(") < loop.index("arr.pop(_victim)"), (
        "the daemon must archive BEFORE it pops")
    assert "key=_eviction_sort_key" in body, "the inline sort-key pin must survive"
    assert "def archive_evicted_capture_local(" in src, (
        "the daemon must DEFINE its twin helper, not import it (guard-742)")
    cli = (CORE_SCRIPTS / "wm.py").read_text(encoding="utf-8")
    cmd = cli.split("def cmd_append(", 1)[1].split("\ndef ", 1)[0]
    assert "may_evict=" in cmd, "cmd_append no longer passes an archive callback"


def _emit_notice(resp: dict):
    """Run wm-append.sh's _emit_notice the way the wrapper does: under
    set -euo pipefail, with RESP holding the daemon's response body."""
    src = (CORE_SCRIPTS / "wm-append.sh").read_text(encoding="utf-8")
    start = src.index("_emit_notice() {")
    end = src.index("\n}\n", start) + 3
    code = ("set -euo pipefail\n" + src[start:end]
            + 'SLOT="$1"; RESP="$2"\n_emit_notice\necho NOTICE-RETURNED\n')
    return subprocess.run([BASH, "-c", code, "wm-append", "spark_capture", json.dumps(resp)],
                          capture_output=True, text=True, timeout=60)


def _resp(evicted, archived, deferred):
    return {"ok": True, "slot": "spark_capture", "evicted": evicted,
            "evicted_archived": archived, "eviction_deferred": deferred,
            "placement": "slots", "carrier_pushed": None}


A, U, F = "[wm-append] archived:", "[wm-append] UNARCHIVED:", "[wm-append] ARCHIVE WRITE FAILED:"


@pytest.mark.parametrize("resp, present, absent", [
    # Body WM, victim archived: recoverable, and the old "unrecoverable" claim is gone.
    (_resp(1, 1, 0), [A + " 1 of the 1 evicted"], [U, F, "unrecoverable loss on any box"]),
    # Ceiling reached with a broken sink: the drop and the deferral are both loud.
    (_resp(1, 0, 5), [U + " 1 of the 1 evicted", F + " 5 entries"], [A]),
    # Kept, nothing evicted: the deferral is still reported.
    (_resp(0, 0, 1), [F + " 1 entries"], ["is at its cap", A, U]),
    # Agent-wide WM: null is not zero, so no archive line either way.
    (_resp(1, None, 0), ["is at its cap"], [A, U, F]),
    # The common quiet path must not trip set -e.
    (_resp(0, None, 0), [], ["is at its cap", A, U, F]),
])
def test_wrapper_reports_archived_unarchived_and_deferred(resp, present, absent):
    """A producer nobody displays is not shipped ()."""
    r = _emit_notice(resp)
    assert r.returncode == 0 and "NOTICE-RETURNED" in r.stdout, r.stderr
    for text in present:
        assert text in r.stderr, (text, r.stderr)
    for text in absent:
        assert text not in r.stderr, (text, r.stderr)
