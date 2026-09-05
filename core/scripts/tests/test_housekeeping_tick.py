"""housekeeping-tick.py — hermetic unit tests (P1, 2026-08-21).

Every path is injected (guard-1039: no live daemon, no production store, no
real scratchpad, no real purge). The purge runner is stubbed with a
`python -c` that prints canned JSON; lane functions are exercised against
tmp_path fixtures; do_run's orchestration is tested with monkeypatched lanes
so it never walks the real agents tree.

Run: STORAGE_BACKEND=local python -m pytest core/scripts/tests/test_housekeeping_tick.py -q
"""
import datetime as dt
import importlib.util
import json
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))


def load_mod():
    spec = importlib.util.spec_from_file_location(
        "housekeeping_tick", SCRIPT_DIR / "housekeeping-tick.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


HK = load_mod()

CFG = {"interval_hours": 6, "shadow": True,
       "scratch_session_age_days": 14, "scratch_empty_project_age_days": 30}


def _stub_purge(payload: dict) -> list:
    """A purge-command stub: prints the canned JSON and exits 0."""
    return [sys.executable, "-c",
            f"import json; print(json.dumps({payload!r}))"]


def _age(p: Path, days: float) -> None:
    ts = time.time() - days * 86400
    import os
    for root, dirs, files in os.walk(p):
        for n in files + dirs:
            os.utime(Path(root) / n, (ts, ts))
    os.utime(p, (ts, ts))


# ── gating ──────────────────────────────────────────────────────────────────

def test_is_due_fresh_state_runs():
    assert HK.is_due({}, 6) is True


def test_is_due_recent_stamp_blocks():
    now = dt.datetime(2026, 8, 21, 12, 0, 0)
    state = {"last_run": "2026-08-21T11:00:00"}
    assert HK.is_due(state, 6, now=now) is False


def test_is_due_old_stamp_runs():
    now = dt.datetime(2026, 8, 21, 12, 0, 0)
    state = {"last_run": "2026-08-21T05:59:00"}
    assert HK.is_due(state, 6, now=now) is True


def test_is_due_garbage_stamp_runs():
    assert HK.is_due({"last_run": "not-a-timestamp"}, 6) is True


# ── config natural gate ─────────────────────────────────────────────────────

def test_load_config_missing_block_is_inert(tmp_path):
    p = tmp_path / "aspirations.yaml"
    p.write_text("temp_pressure:\n  warn_threshold: 10\n", encoding="utf-8")
    assert HK.load_config(p) is None


def test_load_config_merges_defaults(tmp_path):
    p = tmp_path / "aspirations.yaml"
    p.write_text("housekeeping_tick:\n  interval_hours: 12\n", encoding="utf-8")
    cfg = HK.load_config(p)
    assert cfg["interval_hours"] == 12
    assert cfg["shadow"] is True                       # default preserved
    assert cfg["scratch_session_age_days"] == 14


# ── lane A verdict classification ───────────────────────────────────────────

def test_lane_a_ok():
    out = HK.run_lane_a(True, purge_cmd=_stub_purge(
        {"would_purge": 3, "citation_lookup": "ok", "files": ["a.log"],
         "watermark_source": "absent", "dry_run": True}))
    assert out["verdict"] == "ok"
    assert out["would_purge"] == 3


def test_lane_a_degraded_on_failed_citation_lookup():
    """The silent-zero guard: failed lookup => UNMEASURED, never clean."""
    out = HK.run_lane_a(True, purge_cmd=_stub_purge(
        {"would_purge": 0, "citation_lookup": "failed", "dry_run": True}))
    assert out["verdict"] == "degraded"


def test_lane_a_purge_error_on_nonzero_rc():
    out = HK.run_lane_a(True, purge_cmd=[
        sys.executable, "-c", "import sys; sys.exit(1)"])
    assert out["verdict"] == "purge-error"
    assert out["rc"] == 1


def test_lane_a_unparseable_stdout_is_error():
    out = HK.run_lane_a(True, purge_cmd=[
        sys.executable, "-c", "print('not json')"])
    assert out["verdict"] == "purge-error"


# ── lane B scratchpad GC ────────────────────────────────────────────────────

def _fixture_root(tmp_path):
    """A synthetic scratchpad: root/<slug>/ with session dirs + neighbors."""
    root = tmp_path / "claude"
    my = root / HK.project_slug()
    my.mkdir(parents=True)
    # aged, uncited session — the removable shape
    s_old = my / "aaaa1111-dead-beef-0000-000000000001"
    (s_old / "scratchpad").mkdir(parents=True)
    (s_old / "scratchpad" / "junk.txt").write_text("x", encoding="utf-8")
    _age(s_old, 20)
    # aged but CITED session — must survive
    s_cited = my / "bbbb2222-dead-beef-0000-000000000002"
    s_cited.mkdir()
    (s_cited / "evidence.txt").write_text("x", encoding="utf-8")
    _age(s_cited, 20)
    # aged with a top-level RECEIPT — must survive (Lane 3 idiom)
    s_rcpt = my / "cccc3333-dead-beef-0000-000000000003"
    s_rcpt.mkdir()
    (s_rcpt / "RECEIPT.json").write_text("{}", encoding="utf-8")
    _age(s_rcpt, 20)
    # fresh session — must survive (live sessions always have fresh mtimes)
    s_new = my / "dddd4444-dead-beef-0000-000000000004"
    s_new.mkdir()
    (s_new / "wip.txt").write_text("x", encoding="utf-8")
    # empty old project dir — removable, zero loss
    p_empty = root / "C--Some-Old-Project"
    (p_empty / "tasks").mkdir(parents=True)
    _age(p_empty, 40)
    # non-empty other project — REPORT ONLY
    p_other = root / "C--Other-Live-Project"
    p_other.mkdir()
    (p_other / "keep.txt").write_text("x" * 100, encoding="utf-8")
    _age(p_other, 40)
    return root, s_old, s_cited, s_rcpt, s_new, p_empty, p_other


def test_lane_b_shadow_reports_but_deletes_nothing(tmp_path):
    root, s_old, s_cited, s_rcpt, s_new, p_empty, p_other = _fixture_root(tmp_path)
    blob = "cite: bbbb2222-dead-beef-0000-000000000002 in a goal description"
    out = HK.run_lane_b(True, CFG, scratch_root=root, cited_blob=blob)
    assert out["empty_projects_removed_count"] == 1
    assert [d["sid"] for d in out["sessions_removed"]] == [s_old.name]
    assert out["sessions_kept_cited"] == [s_cited.name]
    assert out["sessions_kept_receipt"] == [s_rcpt.name]
    assert out["other_projects_nonempty"] == 1
    # shadow: EVERYTHING still on disk
    for p in (s_old, s_cited, s_rcpt, s_new, p_empty, p_other):
        assert p.exists(), f"shadow mode deleted {p.name}"


def test_lane_b_armed_removes_exactly_the_removable(tmp_path):
    root, s_old, s_cited, s_rcpt, s_new, p_empty, p_other = _fixture_root(tmp_path)
    blob = "cite: bbbb2222-dead-beef-0000-000000000002"
    out = HK.run_lane_b(False, CFG, scratch_root=root, cited_blob=blob)
    assert not s_old.exists(), "aged uncited session must be removed"
    assert not p_empty.exists(), "aged empty project must be removed"
    assert s_cited.exists(), "cited session must survive"
    assert s_rcpt.exists(), "receipted session must survive"
    assert s_new.exists(), "fresh session must survive"
    assert p_other.exists(), "non-empty other project is report-only"
    assert out["shadow"] is False


def test_lane_b_unreadable_blob_fails_closed(tmp_path):
    """'Unknown' and 'nothing cited' must not render identically when the
    consumer deletes on the answer — the purge Lane-2 policy, extended here."""
    root, s_old, *_ = _fixture_root(tmp_path)
    out = HK.run_lane_b(False, CFG, scratch_root=root, cited_blob=None)
    assert out["cited_blob"] == "unreadable"
    assert out["sessions_removed"] == []
    assert s_old.exists(), "fail-closed: no session deletion without the blob"
    # the zero-loss empty-project pass still ran (needs no citations)
    assert out["empty_projects_removed_count"] == 1


def test_lane_b_missing_root_skips(tmp_path):
    out = HK.run_lane_b(True, CFG, scratch_root=tmp_path / "nope", cited_blob="")
    assert out["skipped"] == "no-scratch-root"


# ── do_run orchestration ────────────────────────────────────────────────────

def _canned_lanes(monkeypatch, lane_a):
    monkeypatch.setattr(HK, "run_lane_a", lambda shadow, purge_cmd=None: lane_a)
    monkeypatch.setattr(HK, "run_lane_b",
                        lambda shadow, cfg, scratch_root=None, cited_blob="UNSET",
                        now=None: {"stub": True})
    monkeypatch.setattr(HK, "run_lane_c", lambda agents_root_fn=None: [])


def test_do_run_ok_files_nothing(tmp_path, monkeypatch):
    _canned_lanes(monkeypatch, {"verdict": "ok", "would_purge": 2})
    calls = []
    rec = HK.do_run(dict(CFG), "test", investigate_fn=lambda r, d: calls.append(r),
                    log_path=tmp_path / "log.jsonl")
    assert rec["verdict"] == "ok"
    assert calls == []
    lines = (tmp_path / "log.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["verdict"] == "ok"


def test_do_run_degraded_shadow_records_but_does_not_file(tmp_path, monkeypatch):
    _canned_lanes(monkeypatch, {"verdict": "degraded", "citation_lookup": "failed"})
    calls = []
    rec = HK.do_run(dict(CFG), "test", investigate_fn=lambda r, d: calls.append(r),
                    log_path=tmp_path / "log.jsonl")
    assert rec["verdict"] == "degraded"
    assert rec["mode"] == "shadow"
    assert calls == [], "shadow mode observes; it must not file goals"


def test_do_run_degraded_armed_files_once(tmp_path, monkeypatch):
    _canned_lanes(monkeypatch, {"verdict": "degraded", "citation_lookup": "failed"})
    cfg = dict(CFG)
    cfg["shadow"] = False
    calls = []
    rec = HK.do_run(cfg, "test",
                    investigate_fn=lambda r, d: (calls.append(r), {"filed": True})[1],
                    log_path=tmp_path / "log.jsonl")
    assert rec["mode"] == "armed"
    assert calls == ["degraded"], "armed degraded run must file exactly once"
    assert rec["investigate"] == {"filed": True}


# ── helpers ─────────────────────────────────────────────────────────────────

def test_has_top_receipt_shapes(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    assert HK._has_top_receipt(d) is False
    (d / "receipt.json").write_text("{}", encoding="utf-8")   # lowercase producer shape
    assert HK._has_top_receipt(d) is True


def test_has_top_receipt_not_nested(tmp_path):
    d = tmp_path / "d"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "RECEIPT.md").write_text("x", encoding="utf-8")
    assert HK._has_top_receipt(d) is False, "receipt must be TOP-level (Lane 3 idiom)"


def test_project_slug_transform():
    # Windows drive path: ':' and '\' each become '-' (C:\a\b → C--a-b, the
    # harness scratchpad dir shape); POSIX absolute path keeps its leading '-'.
    assert HK.project_slug(Path(r"C:\Widgets\Acme-Repo")) == "C--Widgets-Acme-Repo"
    assert HK.project_slug(Path("/home/user/acme-repo")) == "-home-user-acme-repo"


# ── Lane D: transcript archive ──────────────────────────────────────────────

DCFG = dict(CFG, transcript_archive_interval_hours=12)

RECEIPT = {"destination": "s3://b/env/transcripts/BOX", "machine": "BOX",
           "live_files": 1060, "live_bytes": 768_000_000, "archived_count": 3,
           "archived_bytes": 4096, "unchanged_skipped": 1057, "failed_count": 0,
           "failures": [], "newly_deleted_detected": 0, "newly_deleted_sample": [],
           "index_total_entries": 1060, "by_harness": {"claude-code": 3}}


def _stub_archive(payload: dict) -> list:
    return [sys.executable, "-c",
            f"import json; print(json.dumps({payload!r}))"]


def test_lane_d_disabled_when_interval_zero(tmp_path):
    cfg = dict(CFG, transcript_archive_interval_hours=0)
    out = HK.run_lane_d(cfg, state_path=tmp_path / "s.json",
                        archive_cmd=_stub_archive(RECEIPT))
    assert out == {"verdict": "disabled"}, "0 is the operator off-switch"


def test_lane_d_absent_key_is_disabled(tmp_path):
    """A config predating this lane must not start uploading by surprise."""
    out = HK.run_lane_d(dict(CFG), state_path=tmp_path / "s.json",
                        archive_cmd=_stub_archive(RECEIPT))
    assert out["verdict"] == "disabled"


def test_lane_d_not_due_within_interval(tmp_path):
    sp = tmp_path / "s.json"
    now = dt.datetime(2026, 9, 3, 12, 0, 0)
    sp.write_text(json.dumps({"last_transcript_archive":
                              (now - dt.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")}),
                  encoding="utf-8")
    out = HK.run_lane_d(DCFG, state_path=sp,
                        archive_cmd=_stub_archive(RECEIPT), now=now)
    assert out["verdict"] == "not-due"


def test_lane_d_due_runs_and_stamps(tmp_path):
    sp = tmp_path / "s.json"
    now = dt.datetime(2026, 9, 3, 12, 0, 0)
    sp.write_text(json.dumps({"last_run": "keep-me", "last_transcript_archive":
                              (now - dt.timedelta(hours=13)).strftime("%Y-%m-%dT%H:%M:%S")}),
                  encoding="utf-8")
    out = HK.run_lane_d(DCFG, state_path=sp,
                        archive_cmd=_stub_archive(RECEIPT), now=now)
    assert out["verdict"] == "ok"
    assert out["archived_count"] == 3 and out["unchanged_skipped"] == 1057
    st = json.loads(sp.read_text(encoding="utf-8"))
    assert st["last_transcript_archive"] == "2026-09-03T12:00:00"
    assert st["last_run"] == "keep-me", "must merge, never clobber the tick stamp"


def test_lane_d_partial_still_stamps(tmp_path):
    sp = tmp_path / "s.json"
    r = dict(RECEIPT, failed_count=2, failures=[{"key": "a"}, {"key": "b"}])
    out = HK.run_lane_d(DCFG, state_path=sp, archive_cmd=_stub_archive(r))
    assert out["verdict"] == "partial" and len(out["failures"]) == 2
    assert json.loads(sp.read_text(encoding="utf-8"))["last_transcript_archive"]


def test_lane_d_timeout_does_not_stamp(tmp_path, monkeypatch):
    sp = tmp_path / "s.json"
    monkeypatch.setattr(HK, "LANE_D_TIMEOUT", 1)
    out = HK.run_lane_d(DCFG, state_path=sp,
                        archive_cmd=[sys.executable, "-c", "import time; time.sleep(20)"])
    assert out["verdict"] == "timeout"
    assert not sp.exists(), "an unreachable backend must retry next tick"


def test_lane_d_unparseable_does_not_stamp(tmp_path):
    sp = tmp_path / "s.json"
    out = HK.run_lane_d(DCFG, state_path=sp,
                        archive_cmd=[sys.executable, "-c", "print('not json')"])
    assert out["verdict"] == "unparseable"
    assert not sp.exists()


def test_lane_d_spawn_error_does_not_stamp(tmp_path):
    sp = tmp_path / "s.json"
    out = HK.run_lane_d(DCFG, state_path=sp, archive_cmd=["/no/such/binary-xyz"])
    assert out["verdict"] == "spawn-error"
    assert not sp.exists()


def test_lane_d_refuses_real_archiver_under_pytest(tmp_path, monkeypatch):
    """No archive_cmd + inside pytest ⇒ never shell out to the production path.

    PYTEST_CURRENT_TEST is pinned explicitly rather than inherited from the
    ambient runner (guard-4522): the branch under test is env-dependent, so a
    test that reads the env instead of setting it asserts nothing about the
    branch when the runner changes.
    """
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "pinned::test (call)")
    out = HK.run_lane_d(DCFG, state_path=tmp_path / "s.json")
    assert out == {"verdict": "skipped-under-pytest"}


def test_lane_d_explicit_cmd_bypasses_the_pytest_guard(tmp_path, monkeypatch):
    """The guard must gate only the DEFAULT command, never an injected one."""
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "pinned::test (call)")
    out = HK.run_lane_d(DCFG, state_path=tmp_path / "s.json",
                        archive_cmd=_stub_archive(RECEIPT))
    assert out["verdict"] == "ok"


def test_lane_d_is_not_gated_by_shadow(tmp_path):
    """shadow arms DELETERS; lane D only copies. Parity here would be a bug."""
    out = HK.run_lane_d(dict(DCFG, shadow=True), state_path=tmp_path / "s.json",
                        archive_cmd=_stub_archive(RECEIPT))
    assert out["verdict"] == "ok"


def test_do_run_records_lane_d(tmp_path, monkeypatch):
    _canned_lanes(monkeypatch, {"verdict": "ok"})
    monkeypatch.setattr(HK, "run_lane_d",
                        lambda cfg, archive_cmd=None: {"verdict": "ok", "archived_count": 7})
    rec = HK.do_run(dict(DCFG), "test", log_path=tmp_path / "log.jsonl")
    assert rec["lane_d"] == {"verdict": "ok", "archived_count": 7}
    assert json.loads((tmp_path / "log.jsonl").read_text(encoding="utf-8"))["lane_d"]["archived_count"] == 7
