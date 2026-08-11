""": cross-box staged-WM consume must route through the storage backend.

THE DEFECT (three local-only layers, each alone fatal on an own-cloud reducer):
  ENUMERATE  _consume_staged listed session/pending-body-merges/ with a local
             glob. A remote worker's staged files arrive via body-manifest.py
             push-staged -> the authoritative store; nothing on the reducer box
             ever reads those exact names, so read-through caching leaves the
             local dir EMPTY and the drain no-ops silently (the g-115-4154
             enumerate-by-what-this-box-holds class). Live specimen: 3 staged
             files for one unitKey survived a full reducer stop/start cycle
             un-consumed (2026-08-04) — the worker's divergence, including its
             spark_capture learning payload, never reached the reducer.
  READ       even when enumerated, local bytes can be stale vs the store.
  DELETE     a local unlink leaves the authoritative object in place to
             re-materialize and RE-MERGE at the next consolidation (a 3-way
             delta applied twice double-counts counters).

Hermetic two-root construction: the "authoritative store" is a FakeStore stub
injected via the merge._get_backend seam, so the store/local divergence is
built directly with no S3 (and STORAGE_BACKEND=local stays pinned per
guard-955). The fake ASSERTS path absoluteness — the `.resolve()` in the
production code is load-bearing (_fleet_diary.py lesson: a relative path makes
the real backend degrade to the local mirror silently).

Run:
  STORAGE_BACKEND=local python -m pytest core/scripts/tests/test_body_merge_staged_backend.py -q
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import yaml

TESTS_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = TESTS_DIR.parent  # core/scripts/


def _load(modname: str, filename: str):
    spec = importlib.util.spec_from_file_location(modname, CORE_SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


merge = _load("body_merge_backend_tests", "body-merge.py")

SID = "301a3333-3333-4333-8333-333333333333"


def _y(d: dict) -> bytes:
    return yaml.dump(d, default_flow_style=False, sort_keys=False).encode("utf-8")


class FakeStore:
    """Authoritative-store stub keyed by staged-file basename.

    list_dir returns EVERY staged name (wm + hash + baseline), exactly as S3
    would — pinning the caller's endswith("-wm.yaml") filter so a baseline
    sidecar is never mistaken for a staged WM.
    """

    def __init__(self, files: dict[str, bytes] | None = None,
                 fail_reads: bool = False):
        self.files = dict(files or {})
        self.deleted: list[str] = []
        self.fail_reads = fail_reads
        self.wm_at_delete: dict[str, dict] = {}
        self._reducer_wm_path: Path | None = None

    def _name(self, path) -> str:
        p = Path(path)
        assert p.is_absolute(), (
            f"backend called with non-absolute path {p} — the production "
            ".resolve() is load-bearing (relative paths silently degrade to "
            "the local mirror in the real backend)")
        return p.name

    def list_dir(self, path):
        p = Path(path)
        assert p.is_absolute(), f"list_dir called with relative path {p}"
        return sorted(self.files)

    def read_authoritative_bytes(self, path) -> bytes:
        if self.fail_reads:
            raise RuntimeError("simulated transport error")
        name = self._name(path)
        if name not in self.files:
            raise FileNotFoundError(name)
        return self.files[name]

    def delete_object(self, path) -> bool:
        name = self._name(path)
        self.deleted.append(name)
        if self._reducer_wm_path is not None and self._reducer_wm_path.is_file():
            self.wm_at_delete[name] = yaml.safe_load(
                self._reducer_wm_path.read_text(encoding="utf-8")) or {}
        return self.files.pop(name, None) is not None


def _mk_agent(tmp_path: Path, reducer_wm: dict, name: str = "alpha") -> Path:
    state = tmp_path / "agents" / name / "session"
    state.mkdir(parents=True, exist_ok=True)
    (state / "working-memory.yaml").write_bytes(_y(reducer_wm))
    return tmp_path


def _reducer_wm_path(pr: Path, name: str = "alpha") -> Path:
    return pr / "agents" / name / "session" / "working-memory.yaml"


def _read_reducer(pr: Path, name: str = "alpha") -> dict:
    return yaml.safe_load(_reducer_wm_path(pr, name).read_text(encoding="utf-8")) or {}


def _staged_dir(pr: Path, name: str = "alpha") -> Path:
    return pr / "agents" / name / "session" / "pending-body-merges"


# ── THE DEFECT PIN ──────────────────────────────────────────────────────────

def test_store_only_staging_is_consumed_with_no_local_dir(tmp_path, monkeypatch):
    """The live-specimen shape: all 3 transport files exist ONLY in the store;
    the local staging dir does not exist at all. The old local-glob code
    returned at `staged_dir.is_dir()` and drained nothing — this test fails
    against it and pins the fix end-to-end (enumerate + read + delete)."""
    pr = _mk_agent(tmp_path, {"slots": {"active_context": {"a": 1}}})
    baseline = {"slots": {}}
    body = {"slots": {"spark_capture": [{"goal_id": "g-1", "observation": "learned X"}]}}
    fake = FakeStore({
        f"{SID}-wm.yaml": _y(body),
        f"{SID}-wm.hash": hashlib.sha256(_y(baseline)).hexdigest().encode(),
        f"{SID}-wm-baseline.yaml": _y(baseline),
    })
    monkeypatch.setattr(merge, "_get_backend", lambda: fake)

    summary = merge.generalize_down("alpha", project_root=pr)

    assert SID in summary["staged_merged"], summary
    red = _read_reducer(pr)
    assert red["slots"]["spark_capture"] == [
        {"goal_id": "g-1", "observation": "learned X"}], (
        "the worker's spark_capture learning payload must reach the reducer WM")
    assert red["slots"]["active_context"] == {"a": 1}
    # Consumed exactly once IN THE STORE — a local-only unlink would leave the
    # authoritative objects to re-merge forever.
    assert sorted(fake.deleted) == sorted([
        f"{SID}-wm.yaml", f"{SID}-wm.hash", f"{SID}-wm-baseline.yaml"])
    assert fake.files == {}


def test_store_counter_merges_3way_through_backend(tmp_path, monkeypatch):
    """The baseline sidecar read must also route through the store: counter
    merges as r + (b - B), not the 2-way r + b that double-counts B."""
    pr = _mk_agent(tmp_path, {"slots": {}, "goals_completed": 10})
    baseline = {"slots": {}, "goals_completed": 7}
    body = {"slots": {}, "goals_completed": 9}
    fake = FakeStore({
        f"{SID}-wm.yaml": _y(body),
        f"{SID}-wm-baseline.yaml": _y(baseline),
    })
    monkeypatch.setattr(merge, "_get_backend", lambda: fake)

    summary = merge.generalize_down("alpha", project_root=pr)

    assert SID in summary["staged_merged"]
    assert _read_reducer(pr)["goals_completed"] == 12, (
        "expected 10 + (9 - 7); a 2-way SUM (19) means the staged baseline "
        "was not read through the backend")


def test_store_noop_when_wm_matches_staged_hash(tmp_path, monkeypatch):
    """Guard 2 through the backend: a never-diverged orphan is consumed
    without merging."""
    pr = _mk_agent(tmp_path, {"slots": {"x": 1}})
    body = {"slots": {"y": 2}}
    body_bytes = _y(body)
    fake = FakeStore({
        f"{SID}-wm.yaml": body_bytes,
        f"{SID}-wm.hash": hashlib.sha256(body_bytes).hexdigest().encode(),
    })
    monkeypatch.setattr(merge, "_get_backend", lambda: fake)

    summary = merge.generalize_down("alpha", project_root=pr)

    assert SID in summary["noop"]
    assert summary["staged_merged"] == []
    assert _read_reducer(pr) == {"slots": {"x": 1}}
    assert f"{SID}-wm.yaml" in fake.deleted


# ── UNION + FALLBACK (local-only stagings must keep draining) ───────────────

def test_local_only_staging_drains_when_store_has_nothing(tmp_path, monkeypatch):
    """cleanup-stale-bindings on THIS box stages locally without a push. The
    store lists nothing and read raises FileNotFoundError — the union
    enumeration plus the local-read fallback must still drain it."""
    pr = _mk_agent(tmp_path, {"slots": {"x": 1}})
    d = _staged_dir(pr)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{SID}-wm.yaml").write_bytes(_y({"slots": {"local_only": True}}))
    fake = FakeStore({})  # store is empty; reads raise FileNotFoundError
    monkeypatch.setattr(merge, "_get_backend", lambda: fake)

    summary = merge.generalize_down("alpha", project_root=pr)

    assert SID in summary["staged_merged"]
    assert _read_reducer(pr)["slots"]["local_only"] is True
    assert not (d / f"{SID}-wm.yaml").exists()


def test_no_backend_at_all_behaves_like_before(tmp_path, monkeypatch):
    """_get_backend() -> None (import failure / no config) must leave the
    original local-only behavior fully intact."""
    pr = _mk_agent(tmp_path, {"slots": {"x": 1}})
    d = _staged_dir(pr)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{SID}-wm.yaml").write_bytes(_y({"slots": {"from_local": 1}}))
    monkeypatch.setattr(merge, "_get_backend", lambda: None)

    summary = merge.generalize_down("alpha", project_root=pr)

    assert SID in summary["staged_merged"]
    assert _read_reducer(pr)["slots"]["from_local"] == 1
    assert not (d / f"{SID}-wm.yaml").exists()


# ── STALENESS: authoritative bytes win over a diverged local mirror ─────────

def test_authoritative_bytes_win_over_stale_local_copy(tmp_path, monkeypatch):
    pr = _mk_agent(tmp_path, {"slots": {}})
    d = _staged_dir(pr)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{SID}-wm.yaml").write_bytes(_y({"slots": {"version": "stale-local"}}))
    fake = FakeStore({f"{SID}-wm.yaml": _y({"slots": {"version": "fresh-store"}})})
    monkeypatch.setattr(merge, "_get_backend", lambda: fake)

    summary = merge.generalize_down("alpha", project_root=pr)

    assert SID in summary["staged_merged"]
    assert _read_reducer(pr)["slots"]["version"] == "fresh-store", (
        "local mirror bytes were merged — the staleness half of g-115-4154")


# ── TRANSIENT TRANSPORT ERRORS: defer, never consume unseen bytes ───────────

def test_transport_error_defers_and_a_retry_succeeds(tmp_path, monkeypatch):
    pr = _mk_agent(tmp_path, {"slots": {"x": 1}})
    body = {"slots": {"from_store": 1}}
    fake = FakeStore({f"{SID}-wm.yaml": _y(body)}, fail_reads=True)
    monkeypatch.setattr(merge, "_get_backend", lambda: fake)

    summary = merge.generalize_down("alpha", project_root=pr)
    assert SID in summary["staged_deferred"], summary
    assert summary["staged_merged"] == []
    assert fake.deleted == [], "a unit whose bytes were never seen must not be consumed"
    assert _read_reducer(pr) == {"slots": {"x": 1}}

    fake.fail_reads = False  # the hiccup clears; next consolidation retries
    summary2 = merge.generalize_down("alpha", project_root=pr)
    assert SID in summary2["staged_merged"]
    assert _read_reducer(pr)["slots"]["from_store"] == 1
    assert f"{SID}-wm.yaml" in fake.deleted


# ── CRASH ORDERING: merged units are deleted only after the WM write ────────

def test_merged_unit_deleted_only_after_reducer_wm_write(tmp_path, monkeypatch):
    """If the process dies between merge and delete, the staged copy must
    still exist so the next run recovers the divergence; therefore the
    authoritative delete must fire only once the merged WM is on disk. The
    fake snapshots the reducer WM at each delete_object call."""
    pr = _mk_agent(tmp_path, {"slots": {}})
    fake = FakeStore({f"{SID}-wm.yaml": _y({"slots": {"payload": "learning"}})})
    fake._reducer_wm_path = _reducer_wm_path(pr)
    monkeypatch.setattr(merge, "_get_backend", lambda: fake)

    summary = merge.generalize_down("alpha", project_root=pr)

    assert SID in summary["staged_merged"]
    snap = fake.wm_at_delete.get(f"{SID}-wm.yaml")
    assert snap is not None and snap.get("slots", {}).get("payload") == "learning", (
        "delete_object fired before the merged reducer WM reached disk — a "
        "crash in that window would destroy the worker's divergence")


# ── FILTER PARITY: a baseline sidecar listed by the store is not a WM ───────

def test_store_listing_of_sidecars_alone_drains_nothing(tmp_path, monkeypatch):
    pr = _mk_agent(tmp_path, {"slots": {"x": 1}})
    fake = FakeStore({
        f"{SID}-wm.hash": b"deadbeef",
        f"{SID}-wm-baseline.yaml": _y({"slots": {}}),
    })
    monkeypatch.setattr(merge, "_get_backend", lambda: fake)

    summary = merge.generalize_down("alpha", project_root=pr)

    assert summary["scanned"] == 0
    assert summary["staged_merged"] == [] and summary["skipped"] == []
    assert fake.deleted == []
    assert _read_reducer(pr) == {"slots": {"x": 1}}


if __name__ == "__main__":
    sys.exit("run via pytest")
