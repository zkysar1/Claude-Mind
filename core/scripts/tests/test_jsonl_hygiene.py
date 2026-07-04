"""Tests for core/scripts/jsonl_hygiene.py ( KEYSTONE).

Covers all three modes (cap / rotate / compact) x both policies (lines / age),
dry-run semantics, no-op-below-bound, absent file, malformed-line handling,
unknown mode, and the registry sweep (a DRY-RUN of the shipped store-hygiene.yaml
parses + resolves + proposes only safe non-mutating actions; g-333-08 converted
the earlier apply=True/all-disabled form)."""
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # core/scripts
import jsonl_hygiene as jh  # noqa: E402


def _write(p, records):
    p.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _read(p):
    return [json.loads(ln) for ln in
            Path(p).read_text(encoding="utf-8").splitlines() if ln.strip()]


# ── cap mode (atomic, no archive) ───────────────────────────────────────────
def test_cap_lines_over_threshold(tmp_path):
    p = tmp_path / "log.jsonl"
    _write(p, [{"i": i} for i in range(10)])
    rep = jh.hygiene_one(p, mode="cap", by="lines", max_lines=4, apply=True)
    assert rep["action"] == "capped"
    assert rep["dropped"] == 6
    assert rep["kept"] == 4
    assert [r["i"] for r in _read(p)] == [6, 7, 8, 9]      # newest 4 kept
    assert not (tmp_path / "log-archive.jsonl").exists()   # cap never archives


def test_cap_lines_under_threshold_noop(tmp_path):
    p = tmp_path / "log.jsonl"
    _write(p, [{"i": i} for i in range(3)])
    rep = jh.hygiene_one(p, mode="cap", by="lines", max_lines=10, apply=True)
    assert rep["action"] == "within-bound"
    assert rep["dropped"] == 0
    assert [r["i"] for r in _read(p)] == [0, 1, 2]


# ── rotate mode (archive-first, then drop) ──────────────────────────────────
def test_rotate_lines_archives_oldest(tmp_path):
    p = tmp_path / "journal.jsonl"
    _write(p, [{"i": i} for i in range(10)])
    rep = jh.hygiene_one(p, mode="rotate", by="lines", max_lines=4, apply=True)
    assert rep["action"] == "rotated"
    assert rep["dropped"] == 6
    assert [r["i"] for r in _read(p)] == [6, 7, 8, 9]
    assert [r["i"] for r in _read(tmp_path / "journal-archive.jsonl")] == \
        [0, 1, 2, 3, 4, 5]


def test_rotate_appends_to_existing_archive(tmp_path):
    p = tmp_path / "j.jsonl"
    arch = tmp_path / "j-archive.jsonl"
    _write(arch, [{"old": True}])
    _write(p, [{"i": i} for i in range(6)])
    jh.hygiene_one(p, mode="rotate", by="lines", max_lines=2, apply=True)
    archived = _read(arch)
    assert archived[0] == {"old": True}                    # pre-existing kept
    assert [r.get("i") for r in archived[1:]] == [0, 1, 2, 3]   # appended oldest
    assert [r["i"] for r in _read(p)] == [4, 5]


def test_rotate_custom_archive_path(tmp_path):
    p = tmp_path / "log.jsonl"
    custom = tmp_path / "sink.jsonl"
    _write(p, [{"i": i} for i in range(5)])
    jh.hygiene_one(p, mode="rotate", by="lines", max_lines=2,
                   archive_path=str(custom), apply=True)
    assert custom.exists()
    assert not (tmp_path / "log-archive.jsonl").exists()
    assert [r["i"] for r in _read(custom)] == [0, 1, 2]


# ── dry-run (no writes) ─────────────────────────────────────────────────────
def test_dry_run_no_writes(tmp_path):
    p = tmp_path / "log.jsonl"
    _write(p, [{"i": i} for i in range(10)])
    rep = jh.hygiene_one(p, mode="rotate", by="lines", max_lines=4, apply=False)
    assert rep["action"] == "would-rotate"
    assert rep["dropped"] == 6 and rep["applied"] is False
    assert len(_read(p)) == 10                             # unchanged
    assert not (tmp_path / "log-archive.jsonl").exists()


def test_dry_run_cap(tmp_path):
    p = tmp_path / "log.jsonl"
    _write(p, [{"i": i} for i in range(10)])
    rep = jh.hygiene_one(p, mode="cap", by="lines", max_lines=4, apply=False)
    assert rep["action"] == "would-cap"
    assert len(_read(p)) == 10


# ── age policy ──────────────────────────────────────────────────────────────
_OLD = "2020-01-01T00:00:00"
_NEW = "2099-01-01T00:00:00"


def test_cap_age(tmp_path):
    p = tmp_path / "ts.jsonl"
    _write(p, [{"t": _OLD}, {"t": _OLD}, {"t": _NEW}, {"t": _NEW}])
    rep = jh.hygiene_one(p, mode="cap", by="age", retention_days=30,
                         ts_field="t", apply=True)
    assert rep["dropped"] == 2
    assert [r["t"] for r in _read(p)] == [_NEW, _NEW]


def test_rotate_age_archives_old(tmp_path):
    p = tmp_path / "ts.jsonl"
    _write(p, [{"t": _OLD}, {"t": _OLD}, {"t": _NEW}])
    rep = jh.hygiene_one(p, mode="rotate", by="age", retention_days=30,
                         ts_field="t", apply=True)
    assert rep["dropped"] == 2
    assert [r["t"] for r in _read(p)] == [_NEW]
    assert [r["t"] for r in _read(tmp_path / "ts-archive.jsonl")] == [_OLD, _OLD]


def test_age_keeps_undateable_leading_record(tmp_path):
    # by=age stops at the first record that is not parseably-old, so an
    # undateable leading record conservatively halts the drop (never dropped).
    p = tmp_path / "ts.jsonl"
    _write(p, [{"t": "not-a-date"}, {"t": _OLD}])
    rep = jh.hygiene_one(p, mode="cap", by="age", retention_days=30,
                         ts_field="t", apply=True)
    assert rep["dropped"] == 0
    assert len(_read(p)) == 2


# ── edge cases ──────────────────────────────────────────────────────────────
def test_absent_file_noop(tmp_path):
    rep = jh.hygiene_one(tmp_path / "missing.jsonl", mode="cap", by="lines",
                         max_lines=5, apply=True)
    assert rep["action"] == "absent-or-empty"
    assert rep["dropped"] == 0


def test_rotate_skips_malformed_line(tmp_path):
    # A malformed line is skipped by the canonical reader (consistent on both
    # the snapshot and the in-lock read), so rotation operates on the parseable
    # subset and does not abort. The malformed line is dropped on rewrite
    # (framework behavior; original bytes survive in .history).
    p = tmp_path / "log.jsonl"
    p.write_text('{"i": 0}\nNOT_JSON\n{"i": 2}\n{"i": 3}\n', encoding="utf-8")
    rep = jh.hygiene_one(p, mode="rotate", by="lines", max_lines=2, apply=True)
    assert rep["dropped"] == 1
    assert [r["i"] for r in _read(p)] == [2, 3]
    assert [r["i"] for r in _read(tmp_path / "log-archive.jsonl")] == [0]
    assert "NOT_JSON" not in p.read_text(encoding="utf-8")


def test_unknown_mode_returns_error(tmp_path):
    p = tmp_path / "log.jsonl"
    _write(p, [{"i": 0}])
    rep = jh.hygiene_one(p, mode="bogus", by="lines", max_lines=1, apply=True)
    assert "error" in rep and "unknown mode" in rep["error"]
    assert len(_read(p)) == 1                              # untouched


# ── registry sweep ──────────────────────────────────────────────────────────
def test_sweep_shipped_registry_dry_run_is_safe(tmp_path):
    # The shipped store-hygiene.yaml has ENABLED entries (G5/G9/G11+). A DRY-RUN
    # sweep of the REAL registry must never error and must only propose safe,
    # non-mutating actions -- it must NOT touch live data. Converted from the
    # earlier apply=True / all-disabled form (): that form rotated/capped
    # real stores on every test run once G5/G9/G11 flipped entries on, and would
    # have compacted real knowledge stores once G10 is enabled (the activation
    # blocker noted in store-hygiene.yaml). A dry-run validates the shipped
    # registry parses + resolves + proposes only safe actions, with zero writes.
    result = jh.sweep(apply=False)
    assert result["swept"] >= 1
    assert result["apply"] is False
    SAFE = {"disabled", "within-bound", "would-cap", "would-rotate",
            "would-compact", "absent-or-empty", "unresolved"}
    actions = {r.get("action") for r in result["reports"]}
    assert actions <= SAFE, f"unexpected dry-run sweep actions: {actions}"
    assert not any(r.get("applied") for r in result["reports"])  # no mutation


def test_sweep_enabled_entry_processed(tmp_path, monkeypatch):
    store = tmp_path / "s.jsonl"
    _write(store, [{"i": i} for i in range(8)])
    reg = {
        "version": 1,
        "defaults": {"enabled": False, "mode": "cap", "by": "lines"},
        "stores": [
            {"path": str(store), "enabled": True, "mode": "cap", "by": "lines",
             "max_lines": 3, "owner_goal": "g-test"},
            {"path": str(tmp_path / "other.jsonl"), "enabled": False,
             "mode": "cap", "by": "lines", "max_lines": 3},
        ],
    }
    monkeypatch.setattr(jh, "_load_registry", lambda: reg)
    result = jh.sweep(apply=True)
    assert result["swept"] == 2
    by_action = [r.get("action") for r in result["reports"]]
    assert "capped" in by_action and "disabled" in by_action
    assert [r["i"] for r in _read(store)] == [5, 6, 7]


# ── compact mode (status-based physical compaction,  / G10) ──────────
def _mk(rid, status, **kw):
    r = {"id": rid, "status": status}
    r.update(kw)
    return r


def test_compact_no_grace_archives_all_nonactive(tmp_path):
    # No grace => every retired/superseded record is archived; active records stay.
    p = tmp_path / "rb.jsonl"
    _write(p, [_mk("a1", "active"), _mk("r1", "retired"), _mk("a2", "active"),
               _mk("s1", "superseded"), _mk("r2", "retired")])
    rep = jh.hygiene_one(p, mode="compact", by="lines", apply=True)
    assert rep["action"] == "compacted"
    assert rep["dropped"] == 3
    assert sorted(r["id"] for r in _read(p)) == ["a1", "a2"]
    assert sorted(r["id"] for r in _read(tmp_path / "rb-archive.jsonl")) == \
        ["r1", "r2", "s1"]


def test_compact_preserves_active_regardless_of_age(tmp_path):
    # An OLD active record is never selected; only non-active are archivable.
    p = tmp_path / "rb.jsonl"
    _write(p, [_mk("a1", "active", created="2000-01-01"),
               _mk("r1", "retired", created="2000-01-01")])
    rep = jh.hygiene_one(p, mode="compact", by="lines", grace_days=1, apply=True)
    assert rep["dropped"] == 1
    assert [r["id"] for r in _read(p)] == ["a1"]


def test_compact_age_grace_keeps_recent_retired(tmp_path):
    # grace_days preserves recently-retired records (restore window); created
    # is the default age field.
    p = tmp_path / "rb.jsonl"
    _write(p, [_mk("rold", "retired", created="2020-01-01"),
               _mk("rnew", "retired", created=date.today().isoformat())])
    rep = jh.hygiene_one(p, mode="compact", by="lines", grace_days=30, apply=True)
    assert rep["dropped"] == 1
    assert [r["id"] for r in _read(p)] == ["rnew"]            # recent kept
    assert [r["id"] for r in _read(tmp_path / "rb-archive.jsonl")] == ["rold"]


def test_compact_ts_field_retirement_date(tmp_path):
    # Guardrails: age-grace keyed on retirement_date, not created. A record
    # CREATED long ago but RETIRED recently is kept (restore window is about
    # retirement recency, not record birth).
    p = tmp_path / "g.jsonl"
    recent = date.today().isoformat()
    _write(p, [_mk("g1", "retired", created="2020-01-01", retirement_date=recent),
               _mk("g2", "retired", created="2020-01-01",
                   retirement_date="2020-02-01")])
    rep = jh.hygiene_one(p, mode="compact", by="lines", grace_days=30,
                         ts_field="retirement_date", apply=True)
    assert rep["dropped"] == 1
    assert [r["id"] for r in _read(p)] == ["g1"]
    assert [r["id"] for r in _read(tmp_path / "g-archive.jsonl")] == ["g2"]


def test_compact_ts_field_falls_back_to_created(tmp_path):
    # When the named ts_field is absent on a record, age falls back to created.
    p = tmp_path / "g.jsonl"
    _write(p, [_mk("g1", "retired", created="2020-01-01")])  # no retirement_date
    rep = jh.hygiene_one(p, mode="compact", by="lines", grace_days=30,
                         ts_field="retirement_date", apply=True)
    assert rep["dropped"] == 1                               # used created fallback
    assert _read(p) == []


def test_compact_undateable_retired_kept_under_grace(tmp_path):
    # A retired record with no parseable date is conservatively KEPT when a
    # grace is set (cannot prove it is old enough). Mirrors by=age policy.
    p = tmp_path / "rb.jsonl"
    _write(p, [_mk("rno", "retired"), _mk("rold", "retired", created="2020-01-01")])
    rep = jh.hygiene_one(p, mode="compact", by="lines", grace_days=30, apply=True)
    assert rep["dropped"] == 1
    assert [r["id"] for r in _read(p)] == ["rno"]            # undateable kept


def test_compact_dry_run_no_writes(tmp_path):
    p = tmp_path / "rb.jsonl"
    _write(p, [_mk("a1", "active"), _mk("r1", "retired")])
    rep = jh.hygiene_one(p, mode="compact", by="lines", apply=False)
    assert rep["action"] == "would-compact"
    assert rep["dropped"] == 1 and rep["applied"] is False
    assert len(_read(p)) == 2                                # unchanged
    assert not (tmp_path / "rb-archive.jsonl").exists()


def test_compact_noop_when_all_active(tmp_path):
    p = tmp_path / "rb.jsonl"
    _write(p, [_mk("a1", "active"), _mk("a2", "active")])
    rep = jh.hygiene_one(p, mode="compact", by="lines", apply=True)
    assert rep["action"] == "within-bound"
    assert rep["dropped"] == 0
    assert len(_read(p)) == 2


def test_compact_archive_first_appends_to_existing(tmp_path):
    p = tmp_path / "rb.jsonl"
    arch = tmp_path / "rb-archive.jsonl"
    _write(arch, [{"old": True}])
    _write(p, [_mk("a1", "active"), _mk("r1", "retired")])
    jh.hygiene_one(p, mode="compact", by="lines", apply=True)
    archived = _read(arch)
    assert archived[0] == {"old": True}                      # pre-existing kept
    assert archived[1]["id"] == "r1"


def test_compact_custom_retired_values(tmp_path):
    # retired_values is configurable; a status not in the set is NOT archived.
    p = tmp_path / "rb.jsonl"
    _write(p, [_mk("a", "active"), _mk("d", "deprecated"), _mk("r", "retired")])
    rep = jh.hygiene_one(p, mode="compact", by="lines",
                         retired_values=("deprecated",), apply=True)
    assert rep["dropped"] == 1
    assert sorted(r["id"] for r in _read(p)) == ["a", "r"]   # retired kept here


def test_compact_reverify_keeps_unretired_during_window(tmp_path, monkeypatch):
    # If a target is un-retired (restored) between snapshot and the live drop,
    # it is KEPT in the live file; the archive holds a recoverable dup (never a
    # live loss). The drop modifier re-checks status in the fresh in-lock read.
    import _fileops
    real = _fileops.locked_modify_jsonl
    p = tmp_path / "rb.jsonl"
    _write(p, [_mk("a1", "active"), _mk("r1", "retired"), _mk("r2", "retired")])
    state = {"n": 0}

    def wrapper(path, fn):
        state["n"] += 1
        out = real(path, fn)
        if state["n"] == 1:        # after the archive write, simulate a restore
            recs = _read(p)
            for r in recs:
                if r["id"] == "r1":
                    r["status"] = "active"
            _write(p, recs)
        return out

    monkeypatch.setattr(_fileops, "locked_modify_jsonl", wrapper)
    rep = jh.hygiene_one(p, mode="compact", by="lines", apply=True)
    assert rep["applied"] is True
    live_ids = sorted(r["id"] for r in _read(p))
    assert "r1" in live_ids and "a1" in live_ids   # un-retired r1 kept
    assert "r2" not in live_ids                     # still-retired r2 dropped
    arch_ids = sorted(r["id"] for r in _read(tmp_path / "rb-archive.jsonl"))
    assert arch_ids == ["r1", "r2"]                 # r1 = recoverable dup


# ── : hot-store rotation lock-contention retry (Phase-2 live drop) ────
def test_rotate_retries_live_lock_on_timeout(tmp_path, monkeypatch):
    # The hottest store (changelog.jsonl) can transiently TimeoutError on the
    # Phase-2 live-lock acquire under all-agent append contention. Retry-with-
    # backoff gives it fresh windows so a transient miss succeeds in the SAME
    # sweep instead of failing and waiting 24h for the next one ().
    import _fileops
    real = _fileops.locked_modify_jsonl
    p = tmp_path / "j.jsonl"
    _write(p, [{"i": i} for i in range(10)])
    state = {"live": 0}

    def flaky(path, fn):
        if Path(path).name == "j.jsonl":            # LIVE file = Phase 2
            state["live"] += 1
            if state["live"] <= 2:                  # first 2 acquires "time out"
                raise TimeoutError(f"Could not acquire lock: {path}")
        return real(path, fn)

    monkeypatch.setattr(_fileops, "locked_modify_jsonl", flaky)
    monkeypatch.setattr(jh.time, "sleep", lambda *_a, **_k: None)  # no real backoff
    rep = jh.hygiene_one(p, mode="rotate", by="lines", max_lines=4, apply=True)
    assert rep["action"] == "rotated" and rep["applied"] is True
    assert state["live"] == 3                        # 2 timeouts + 1 success
    assert rep["live_lock_attempts"] == 3
    assert [r["i"] for r in _read(p)] == [6, 7, 8, 9]
    # archive appended exactly ONCE (Phase 1 not re-run on retry -> no orphan dup)
    assert [r["i"] for r in _read(tmp_path / "j-archive.jsonl")] == [0, 1, 2, 3, 4, 5]


def test_rotate_live_lock_exhausted_raises_and_archives_once(tmp_path, monkeypatch):
    # All retries time out -> same failure surface as before the fix (raise; the
    # sweep reports action=error, the next sweep retries). Live untouched (no
    # loss), archive holds the recoverable front-slice appended exactly ONCE.
    import _fileops
    import pytest
    real = _fileops.locked_modify_jsonl
    p = tmp_path / "j.jsonl"
    _write(p, [{"i": i} for i in range(10)])
    state = {"live": 0}

    def always_timeout_live(path, fn):
        if Path(path).name == "j.jsonl":
            state["live"] += 1
            raise TimeoutError(f"Could not acquire lock: {path}")
        return real(path, fn)

    monkeypatch.setattr(_fileops, "locked_modify_jsonl", always_timeout_live)
    monkeypatch.setattr(jh.time, "sleep", lambda *_a, **_k: None)
    with pytest.raises(TimeoutError):
        jh.hygiene_one(p, mode="rotate", by="lines", max_lines=4, apply=True)
    assert state["live"] == jh._ROTATE_LIVE_LOCK_RETRIES       # every window used
    assert [r["i"] for r in _read(p)] == list(range(10))       # live untouched
    assert [r["i"] for r in _read(tmp_path / "j-archive.jsonl")] == [0, 1, 2, 3, 4, 5]


def test_rotate_front_shift_runtimeerror_not_retried(tmp_path, monkeypatch):
    # The _drop_front front-shift guard raises RuntimeError (another rotation
    # already dropped the slice). That is a CORRECTNESS signal, not lock
    # contention -- it must propagate immediately, never be retried.
    import _fileops
    import pytest
    real = _fileops.locked_modify_jsonl
    p = tmp_path / "j.jsonl"
    _write(p, [{"i": i} for i in range(10)])
    state = {"live": 0}

    def runtime_on_live(path, fn):
        if Path(path).name == "j.jsonl":
            state["live"] += 1
            raise RuntimeError("jsonl-hygiene rotate ABORT: live front shifted")
        return real(path, fn)

    monkeypatch.setattr(_fileops, "locked_modify_jsonl", runtime_on_live)
    monkeypatch.setattr(jh.time, "sleep", lambda *_a, **_k: None)
    with pytest.raises(RuntimeError):
        jh.hygiene_one(p, mode="rotate", by="lines", max_lines=4, apply=True)
    assert state["live"] == 1                                   # NOT retried


def test_compact_dry_run_in_sweep_registry(tmp_path, monkeypatch):
    store = tmp_path / "rb.jsonl"
    _write(store, [_mk("a", "active"),
                   _mk("rold", "retired", created="2020-01-01"),
                   _mk("rnew", "retired", created=date.today().isoformat())])
    reg = {
        "version": 1,
        "defaults": {"enabled": False},
        "stores": [{"path": str(store), "enabled": True, "mode": "compact",
                    "grace_days": 30, "owner_goal": "g-333-10"}],
    }
    monkeypatch.setattr(jh, "_load_registry", lambda: reg)
    result = jh.sweep(apply=True)
    assert any(r.get("action") == "compacted" for r in result["reports"])
    assert sorted(r["id"] for r in _read(store)) == ["a", "rnew"]
    assert [r["id"] for r in _read(tmp_path / "rb-archive.jsonl")] == ["rold"]


# ── glob archive-sink exclusion () ───────────────────────────────────
def test_glob_excludes_archive_sinks(tmp_path):
    # A glob must NEVER match an archive sink. Rotation MOVES records INTO
    # <stem>-archive<suffix>; if the same glob re-matched that sink, the next
    # sweep would rotate it into <stem>-archive-archive -- an unbounded
    # archive-of-archive chain (: coordination-archive-archive.jsonl).
    for name in ("coordination.jsonl", "general.jsonl",
                 "coordination-archive.jsonl",
                 "coordination-archive-archive.jsonl"):
        (tmp_path / name).write_text("{}\n", encoding="utf-8")
    got = sorted(p.name for p in jh._glob_or_single(tmp_path / "*.jsonl"))
    assert got == ["coordination.jsonl", "general.jsonl"]   # archives excluded


def test_explicit_single_path_to_archive_passes_through(tmp_path):
    # An EXPLICIT single-path target (no '*') still resolves an archive file,
    # so a deliberate age-cap of one archive remains possible.
    arch = tmp_path / "coordination-archive.jsonl"
    arch.write_text("{}\n", encoding="utf-8")
    assert jh._glob_or_single(arch) == [arch]


def test_sweep_glob_does_not_rotate_archive_into_archive_archive(tmp_path, monkeypatch):
    # End-to-end  regression: a rotate glob over a board-like directory
    # rotates ONLY the live file, never its -archive sink (which would spawn a
    # -archive-archive). Reproduces the exact world/board/*.jsonl defect.
    live = tmp_path / "coordination.jsonl"
    arch = tmp_path / "coordination-archive.jsonl"
    _write(live, [{"i": i} for i in range(8)])
    _write(arch, [{"a": i} for i in range(8)])   # archive already over bound
    reg = {
        "version": 1,
        "defaults": {"enabled": False},
        "stores": [{"path": str(tmp_path / "*.jsonl"), "enabled": True,
                    "mode": "rotate", "by": "lines", "max_lines": 4,
                    "owner_goal": "g-333-08"}],
    }
    monkeypatch.setattr(jh, "_load_registry", lambda: reg)
    result = jh.sweep(apply=True)
    report_names = {Path(r["path"]).name for r in result["reports"]}
    assert "coordination.jsonl" in report_names            # live file processed
    assert "coordination-archive.jsonl" not in report_names  # archive excluded
    assert not (tmp_path / "coordination-archive-archive.jsonl").exists()
    assert len(_read(arch)) == 12                           # 8 existing + 4 rotated in
    assert [r["i"] for r in _read(live)] == [4, 5, 6, 7]    # newest 4 kept


# ── refresh machine-local guard () ───────────────────────────────────
# _snapshot must NOT refresh a per-machine store: backend.refresh() force-pulls
# the remote over the local file, and for a never-pushed per-machine store that
# clobbers the only good copy with stale/empty remote data (guard-881 /
#  presence clobber). SYNCED stores must still refresh.
class _SpyBackend:
    """Stand-in for the own-cloud backend: carries _roots so the REAL
    owncloud_sync.refresh_would_clobber classifies, and records every refresh()
    call so the test asserts skip-vs-invoke. LocalBackend has no _roots and a
    no-op refresh, so it cannot exercise the clobber path -- this spy does."""
    def __init__(self, roots):
        self._roots = roots
        self.refreshed = []

    def refresh(self, path):
        self.refreshed.append(Path(path))


def _spy_world(tmp_path, monkeypatch):
    """Build a tmp 'world' root + install a spy backend whose _roots map it to
    the 'world' prefix. Returns (world_root, spy)."""
    import storage_backend
    world = (tmp_path / "world")
    world.mkdir(exist_ok=True)
    spy = _SpyBackend([(str(world.resolve()), "world")])
    monkeypatch.setattr(storage_backend, "get_backend", lambda: spy)
    return world, spy


def test_snapshot_skips_refresh_for_presence_dir_store(tmp_path, monkeypatch):
    # The load-bearing case: world/presence/<agent>.jsonl is machine-local by
    # DIRECTORY exclusion (presence/ in _EXCLUDE_DIRS, walk-pruned), NOT by
    # basename -- _is_machine_local alone returns False for it. The refresh guard
    # must still skip it.
    world, spy = _spy_world(tmp_path, monkeypatch)
    (world / "presence").mkdir()
    f = world / "presence" / "alpha.jsonl"
    _write(f, [{"i": 1}])
    out = jh._snapshot(f)
    assert out == [{"i": 1}]          # local file still read
    assert spy.refreshed == []        # refresh SKIPPED (would clobber)


def test_snapshot_skips_refresh_for_changelog_basename(tmp_path, monkeypatch):
    # Basename machine-local path (changelog.jsonl in _EXCLUDE_NAMES).
    world, spy = _spy_world(tmp_path, monkeypatch)
    f = world / "changelog.jsonl"
    _write(f, [{"i": 2}])
    jh._snapshot(f)
    assert spy.refreshed == []        # refresh SKIPPED


def test_snapshot_invokes_refresh_for_synced_store(tmp_path, monkeypatch):
    # A SYNCED store (reasoning-bank.jsonl) must still refresh -- S3 is
    # authoritative, so the pull is correct, not a clobber.
    world, spy = _spy_world(tmp_path, monkeypatch)
    f = world / "reasoning-bank.jsonl"
    _write(f, [{"i": 3}])
    out = jh._snapshot(f)
    assert out == [{"i": 3}]
    assert len(spy.refreshed) == 1    # refresh INVOKED for synced store
    assert spy.refreshed[0].name == "reasoning-bank.jsonl"


def test_refresh_would_clobber_classifies(tmp_path):
    # Direct unit test of the owncloud_sync predicate _snapshot guards on.
    import owncloud_sync
    world = tmp_path / "world"
    (world / "presence").mkdir(parents=True)
    (world / ".history").mkdir()
    be = _SpyBackend([(str(world.resolve()), "world")])
    rwc = owncloud_sync.refresh_would_clobber
    assert rwc(be, world / "presence" / "alpha.jsonl") is True   # dir-excluded
    assert rwc(be, world / ".history" / "x.jsonl") is True       # dir-excluded
    assert rwc(be, world / "changelog.jsonl") is True            # EXCLUDE_NAMES
    assert rwc(be, world / "skill-discovery-log.jsonl") is True  # world *-log
    assert rwc(be, world / "reasoning-bank.jsonl") is False      # synced
    # A backend with no _roots (LocalBackend shape) -> refresh is a no-op, so
    # 'clobber' is impossible -> never skip.
    class _NoRoots:
        def refresh(self, p): pass
    assert rwc(_NoRoots(), world / "presence" / "alpha.jsonl") is False
