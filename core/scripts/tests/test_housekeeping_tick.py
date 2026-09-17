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

import pytest

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


def _default_root_env(monkeypatch, tmp_path, uid=4242):
    """Exercise Lane B's DEFAULT root: no scratch_root, no HK_SCRATCH_ROOT."""
    monkeypatch.delenv("HK_SCRATCH_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_TMPDIR", raising=False)
    # : the base now resolves through _node_tmpdir(), which reads the
    # ENV in Node's order and never calls tempfile.gettempdir(). Point TMPDIR at
    # tmp_path and clear the two lower-precedence names so the base is
    # unambiguous. The gettempdir patch is KEPT deliberately: it is now inert for
    # this path, so if anything ever reverts to gettempdir() these tests still
    # pass against tmp_path rather than silently sweeping the REAL /tmp.
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.delenv("TMP", raising=False)
    monkeypatch.delenv("TEMP", raising=False)
    monkeypatch.setattr(HK.tempfile, "gettempdir", lambda: str(tmp_path))
    if uid is None:
        monkeypatch.delattr(HK.os, "getuid", raising=False)
    else:
        monkeypatch.setattr(HK.os, "getuid", lambda: uid, raising=False)


def test_lane_b_default_root_is_the_per_uid_harness_root(tmp_path, monkeypatch):
    """The harness writes <tmp>/claude-<uid> ()."""
    _default_root_env(monkeypatch, tmp_path)
    p_empty = tmp_path / "claude-4242" / "C--Some-Old-Project"
    (p_empty / "tasks").mkdir(parents=True)
    _age(p_empty, 40)

    out = HK.run_lane_b(True, CFG, cited_blob="")
    assert out["root"] == str(tmp_path / "claude-4242")
    assert "skipped" not in out
    assert out["empty_projects_removed_count"] == 1
    # Positive control: the pre-fix default root on this same layout skips.
    old = HK.run_lane_b(True, CFG, scratch_root=tmp_path / "claude", cited_blob="")
    assert old["skipped"] == "no-scratch-root"


@pytest.mark.parametrize("present, uid, want_root, want_unswept", [
    (["claude"], 4242, "claude", None),                          # older layout only
    (["claude-4242", "claude"], 4242, "claude-4242", ["claude"]),  # both: report the other
    (["claude-0"], None, "claude-0", None),                      # no getuid -> 0, as the CLI
])
def test_lane_b_default_root_resolution(tmp_path, monkeypatch, present, uid,
                                        want_root, want_unswept):
    _default_root_env(monkeypatch, tmp_path, uid=uid)
    for name in present:
        (tmp_path / name).mkdir()
    out = HK.run_lane_b(True, CFG, cited_blob="")
    assert out["root"] == str(tmp_path / want_root)
    assert "skipped" not in out
    assert out.get("unswept_roots") == (
        [str(tmp_path / n) for n in want_unswept] if want_unswept else None)


def test_lane_b_default_root_follows_claude_code_tmpdir(tmp_path, monkeypatch):
    """The CLI's tmp base is CLAUDE_CODE_TMPDIR when set, before the OS temp dir."""
    _default_root_env(monkeypatch, tmp_path)
    (tmp_path / "claude-4242").mkdir()                  # decoy under the OS temp dir
    relocated = tmp_path / "private"
    (relocated / "claude-4242").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CODE_TMPDIR", str(relocated))
    out = HK.run_lane_b(True, CFG, cited_blob="")
    assert out["root"] == str(relocated / "claude-4242")


def test_lane_b_hk_scratch_root_overrides_the_default(tmp_path, monkeypatch):
    _default_root_env(monkeypatch, tmp_path)
    (tmp_path / "claude-4242").mkdir()
    monkeypatch.setenv("HK_SCRATCH_ROOT", str(tmp_path / "explicit"))
    out = HK.run_lane_b(True, CFG, cited_blob="")
    assert out["root"] == str(tmp_path / "explicit")
    assert "root_candidates" not in out


def test_lane_b_never_adopts_another_uids_root(tmp_path, monkeypatch):
    """Lane B deletes under its root, so a claude-* glob would be a hazard."""
    _default_root_env(monkeypatch, tmp_path)
    (tmp_path / "claude-1000").mkdir()
    out = HK.run_lane_b(True, CFG, cited_blob="")
    assert out["skipped"] == "no-scratch-root"
    assert out["root"] == str(tmp_path / "claude-4242")
    assert out["root_candidates"] == [str(tmp_path / "claude-4242"),
                                      str(tmp_path / "claude")]


# ── do_run orchestration ────────────────────────────────────────────────────

def _canned_lanes(monkeypatch, lane_a):
    # Bound on purpose: an unbound do_run never calls run_lane_a (), so
    # the canned lane_a would silently not apply wherever conftest binds no agent.
    monkeypatch.setenv("MIND_AGENT", "testagent")
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


# ── unbound ticks () ───────────────────────────────────────────────

def _lane_a_run(tmp_path, monkeypatch, agent, purge_cmd=None):
    """One ARMED do_run whose lane A, if it is ever called, fails the way the
    real purge does unbound. Returns (record, lane A calls, filings)."""
    _canned_lanes(monkeypatch, {"verdict": "ok"})
    if agent is None:
        monkeypatch.delenv("MIND_AGENT", raising=False)
    else:
        monkeypatch.setenv("MIND_AGENT", agent)
    lane_a_calls = []
    monkeypatch.setattr(HK, "run_lane_a", lambda shadow, purge_cmd=None: (
        lane_a_calls.append(purge_cmd), {"verdict": "purge-error"})[1])
    monkeypatch.setattr(HK, "run_lane_d", lambda cfg, archive_cmd=None: {"verdict": "ok"})
    monkeypatch.setattr(HK, "run_lane_e", lambda cfg: {"verdict": "ok"})
    filed = []
    rec = HK.do_run(dict(CFG, shadow=False), "test", purge_cmd=purge_cmd,
                    log_path=tmp_path / "log.jsonl",
                    investigate_fn=lambda r, d: (filed.append(r), {"filed": True})[1])
    return rec, lane_a_calls, filed


def test_do_run_unbound_skips_lane_a_and_files_nothing(tmp_path, monkeypatch, capsys):
    rec, lane_a_calls, filed = _lane_a_run(tmp_path, monkeypatch, agent=None)
    assert lane_a_calls == [], "no bound agent means no temp/ to purge"
    assert rec["verdict"] == "not-applicable-unbound"
    assert rec["lane_a"]["verdict"] == "not-applicable-unbound"
    assert rec["agent"] == "unbound"
    assert filed == [] and "investigate" not in rec
    assert "lane A verdict" not in capsys.readouterr().err


def test_do_run_bound_still_runs_lane_a(tmp_path, monkeypatch):
    # POSITIVE CONTROL for the test above: the same armed fixture, agent bound.
    rec, lane_a_calls, filed = _lane_a_run(tmp_path, monkeypatch, agent="testagent")
    assert lane_a_calls == [None]
    assert rec["verdict"] == "purge-error"
    assert filed == ["purge-error"]


def test_do_run_unbound_with_injected_purge_cmd_still_runs_lane_a(tmp_path, monkeypatch):
    rec, lane_a_calls, filed = _lane_a_run(tmp_path, monkeypatch, agent=None,
                                           purge_cmd=["stub-purge"])
    assert lane_a_calls == [["stub-purge"]]
    assert rec["verdict"] == "purge-error"
    assert filed == ["purge-error"]


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


# ── skip streaks () ─────────────────────────────────────────────────

def _skip_run(tmp_path, monkeypatch, prior, reason="no-scratch-root", ticks=4,
              shadow=True):
    """Seed the ledger with `prior` records whose lane B skipped for `reason`,
    then run one tick whose lane B skips for "no-scratch-root"."""
    log = tmp_path / "log.jsonl"
    with open(log, "w", encoding="utf-8") as fh:
        for _ in range(prior):
            fh.write(json.dumps({"ts": "t", "lane_b": {"skipped": reason}}) + "\n")
    _canned_lanes(monkeypatch, {"verdict": "ok"})
    monkeypatch.setattr(HK, "run_lane_b",
                        lambda shadow, cfg, scratch_root=None: {"skipped": "no-scratch-root"})
    monkeypatch.setattr(HK, "run_lane_d", lambda cfg, archive_cmd=None: {"verdict": "ok"})
    monkeypatch.setattr(HK, "run_lane_e", lambda cfg: {"verdict": "ok"})
    calls = []
    cfg = dict(CFG, skip_streak_ticks=ticks, shadow=shadow)
    rec = HK.do_run(cfg, "test", log_path=log,
                    investigate_fn=lambda r, d: (calls.append(r), {"filed": True})[1])
    return rec, calls, log


def test_skip_streak_at_threshold_files_once_even_in_shadow(tmp_path, monkeypatch):
    rec, calls, log = _skip_run(tmp_path, monkeypatch, prior=3)
    assert rec["skip_streaks"] == [{"lane": "lane_b", "field": "skipped",
                                    "reason": "no-scratch-root", "ticks": 4}]
    assert calls == ["lane_b.skipped=no-scratch-root"]
    assert rec["investigate_skip_streak"] == {"filed": True}
    last = json.loads(log.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert last["skip_streaks"] == rec["skip_streaks"]


def test_skip_streak_one_tick_short_stays_quiet(tmp_path, monkeypatch):
    # POSITIVE CONTROL for the test above: the same fixture, one record short.
    rec, calls, _ = _skip_run(tmp_path, monkeypatch, prior=2)
    assert "skip_streaks" not in rec and calls == []


def test_skip_streak_broken_by_a_different_reason(tmp_path, monkeypatch):
    rec, calls, _ = _skip_run(tmp_path, monkeypatch, prior=3,
                              reason="project-dir-not-found")
    assert "skip_streaks" not in rec and calls == []


def test_skip_streak_zero_disables(tmp_path, monkeypatch):
    rec, calls, _ = _skip_run(tmp_path, monkeypatch, prior=30, ticks=0)
    assert "skip_streaks" not in rec and calls == []


def test_tail_records_reads_only_the_last_n_across_blocks(tmp_path):
    log = tmp_path / "log.jsonl"
    pad = "x" * 70000              # each line outgrows one 64 KiB read from the end
    with open(log, "w", encoding="utf-8") as fh:
        for i in range(10):
            fh.write(json.dumps({"i": i, "pad": pad}) + "\n")
        fh.write("not json\n")
        fh.write(json.dumps({"i": 10}) + "\n")
    # The window is the last 3 LINES: the malformed one is dropped and NOT
    # back-filled with record 8, and the partial line the backward read starts
    # mid-way through is never returned.
    assert [r["i"] for r in HK._tail_records(log, 3)] == [9, 10]
    assert [r["i"] for r in HK._tail_records(log, 4)] == [8, 9, 10]
    assert HK._tail_records(tmp_path / "absent.jsonl", 3) == []


# ── : the tmp base must resolve the way Node's os.tmpdir() does ──
# Lane B sweeps a root the HARNESS (Node) created, so Python's tempfile
# .gettempdir() is the wrong resolver on two independent axes. Both tests below
# assert against run_lane_b's OWN resolved root_candidates -- the production
# path -- not against the helper in isolation.


def _lane_b_base(monkeypatch, env: dict, uid=4242):
    """Run run_lane_b with a fully-controlled env and return its tmp base."""
    monkeypatch.delenv("HK_SCRATCH_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_TMPDIR", raising=False)
    monkeypatch.setattr(HK.os, "getuid", lambda: uid, raising=False)
    for name in ("TMPDIR", "TMP", "TEMP"):
        if env.get(name) is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, env[name])
    out = HK.run_lane_b(True, CFG, cited_blob="")
    # root_candidates is [<base>/claude-<uid>, <base>/claude]; the base is the parent.
    return str(Path(out["root_candidates"][0]).parent)


def test_lane_b_base_prefers_tmp_over_temp_like_node(tmp_path, monkeypatch):
    """TMPDIR unset, TEMP and TMP both set -> Node takes TMP, Python takes TEMP.

    This is the ORDER axis. Python's list is TMPDIR, TEMP, TMP; Node's is
    TMPDIR, TMP, TEMP -- the middle two are swapped, so this env is exactly
    where the two resolvers disagree.
    """
    want = tmp_path / "nodeTMP"
    other = tmp_path / "pyTEMP"
    want.mkdir()
    other.mkdir()
    base = _lane_b_base(monkeypatch, {"TMPDIR": None, "TMP": str(want),
                                      "TEMP": str(other)})
    assert base == str(want), (
        "run_lane_b took TEMP -- that is tempfile.gettempdir()'s order, not "
        "Node's, so Lane B would sweep a root the harness never created")


def test_lane_b_base_keeps_an_unwritable_tmpdir_like_node(tmp_path, monkeypatch):
    """TMPDIR set but unwritable -> Node keeps it, gettempdir() falls through.

    This is the WRITABILITY axis, independent of order: the env here is
    unambiguous (only TMPDIR is set) and the resolvers STILL disagree, because
    gettempdir() probes for writability and silently drops to /tmp.
    """
    unwritable = tmp_path / "unwritable-tmpdir"   # deliberately never created
    base = _lane_b_base(monkeypatch, {"TMPDIR": str(unwritable), "TMP": None,
                                      "TEMP": None})
    assert base == str(unwritable), (
        "run_lane_b dropped an unwritable TMPDIR -- that is gettempdir()'s "
        "writability fallback, which Node does not perform")


def test_node_tmpdir_strips_a_trailing_separator_but_not_a_root(monkeypatch):
    """Node strips trailing separators UNLESS the path is a root."""
    monkeypatch.setenv("TMPDIR", "/tmp/withslash/")
    monkeypatch.delenv("TMP", raising=False)
    monkeypatch.delenv("TEMP", raising=False)
    assert HK._node_tmpdir() == "/tmp/withslash"
    monkeypatch.setenv("TMPDIR", "/")
    assert HK._node_tmpdir() == "/", "a root must not be stripped to empty"

    # THE OTHER ROOT SHAPE, which this test's NAME has always promised and its
    # BODY did not cover. An emptiness test cannot reach it: "C:\\" strips to
    # "C:", which is TRUTHY, so the falsy branch that rescues "/" never fires.
    # FAILS against the pre-fix rstrip-only form (measured: it returned "C:"),
    # which is what makes this assertion evidence rather than decoration.
    monkeypatch.setenv("TMPDIR", "C:\\")
    assert HK._node_tmpdir() == "C:\\", (
        "a Windows drive root must not be reduced to the DRIVE-RELATIVE 'C:' "
        "-- PureWindowsPath('C:') / 'claude-0' is 'C:claude-0', is_absolute() "
        "False, so Lane B would sweep the per-drive cwd, not the drive root")

    # NEGATIVE CONTROL for the guard's REACH: a bare "C:" carried no trailing
    # separator, so Node strips nothing and neither may we. Without this, a fix
    # that unconditionally re-appended "\\" to any drive-shaped result would
    # pass the assertion above while corrupting a path nobody asked it to touch.
    monkeypatch.setenv("TMPDIR", "C:")
    assert HK._node_tmpdir() == "C:", "nothing was stripped, so nothing is owed back"


def test_node_tmpdir_falls_back_when_every_name_is_unset(monkeypatch):
    """All three unset -> /tmp on POSIX. The one case where the two resolvers
    AGREE, which is why the bug was invisible on the authoring box (cc-05 has
    all four vars unset)."""
    for name in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.delenv(name, raising=False)
    assert HK._node_tmpdir() == "/tmp"


def test_the_gettempdir_form_fails_both_cases(tmp_path, monkeypatch):
    """MUTATION CONTROL (verification outcome 1, guard-6701).

    Loads a THROWAWAY copy of the module with the pre-fix `gettempdir()` form
    substituted in, and asserts it gets the WRONG base in BOTH cases the two
    tests above pin -- rather than sabotaging the real file in place. If either
    half of this ever passes, the two resolvers have stopped disagreeing on that
    axis and the corresponding test above proves nothing.

    It does NOT stub `gettempdir`: it clears `tempfile.tempdir` (the memo
    `gettempdir()` fills on first call) so the mutant runs Python's REAL
    resolution against the same env. Stubbing the return value would only prove
    the mutant returns what the test handed it.
    """
    import importlib.util as _ilu
    src = (SCRIPT_DIR / "housekeeping-tick.py").read_text(encoding="utf-8")
    mutated = src.replace(
        'base = os.environ.get("CLAUDE_CODE_TMPDIR") or _node_tmpdir()',
        'base = os.environ.get("CLAUDE_CODE_TMPDIR") or tempfile.gettempdir()')
    assert mutated != src, "the mutation anchor did not match -- control is inert"

    target = tmp_path / "hk_mutated.py"
    target.write_text(mutated, encoding="utf-8")
    spec = _ilu.spec_from_file_location("hk_mutated", target)
    mut = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mut)

    def _mutant_base(env):
        monkeypatch.delenv("HK_SCRATCH_ROOT", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_TMPDIR", raising=False)
        monkeypatch.setattr(mut.os, "getuid", lambda: 4242, raising=False)
        for name in ("TMPDIR", "TMP", "TEMP"):
            if env.get(name) is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, env[name])
        monkeypatch.setattr(mut.tempfile, "tempdir", None)   # drop the memo
        out = mut.run_lane_b(True, CFG, cited_blob="")
        return str(Path(out["root_candidates"][0]).parent)

    # ORDER axis: Python takes TEMP where Node takes TMP.
    want = tmp_path / "nodeTMP"
    other = tmp_path / "pyTEMP"
    want.mkdir()
    other.mkdir()
    assert _mutant_base({"TMPDIR": None, "TMP": str(want), "TEMP": str(other)}) \
        == str(other), (
        "the pre-fix form did NOT take TEMP, so it does not discriminate and "
        "the order test above is not pinning anything")

    # WRITABILITY axis: gettempdir() drops an unwritable TMPDIR, Node keeps it.
    unwritable = tmp_path / "unwritable-tmpdir"   # deliberately never created
    assert _mutant_base({"TMPDIR": str(unwritable), "TMP": None, "TEMP": None}) \
        != str(unwritable), (
        "the pre-fix form KEPT an unwritable TMPDIR, so it does not "
        "discriminate and the writability test above is not pinning anything")
# ── : lane E stale files in SHADOW, on its own origin signal ──────────

def test_lane_e_stale_files_in_shadow_on_its_own_filer(tmp_path, monkeypatch):
    """Fails if the `if not shadow` gate around the lane E filing returns: every
    box runs shadow, so that gate made a measured stale seed file nowhere."""
    _canned_lanes(monkeypatch, {"verdict": "ok"})
    monkeypatch.setattr(HK, "run_lane_d", lambda cfg, archive_cmd=None: {"verdict": "ok"})
    monkeypatch.setattr(HK, "run_lane_e",
                        lambda cfg: {"verdict": "stale", "publish_due": "yes"})
    filed = []
    monkeypatch.setattr(HK, "file_mind_seed_stale_investigate",
                        lambda r, d: (filed.append(r), {"filed": True})[1])
    rec = HK.do_run(dict(CFG), "test", log_path=tmp_path / "log.jsonl")
    assert rec["mode"] == "shadow"
    assert filed == ["mind-seed-publish-stale"]
    assert rec["investigate_lane_e"] == {"filed": True}


def test_lane_e_ok_files_nothing_in_shadow(tmp_path, monkeypatch):
    """Positive control for the test above: only a measured stale files."""
    _canned_lanes(monkeypatch, {"verdict": "ok"})
    monkeypatch.setattr(HK, "run_lane_d", lambda cfg, archive_cmd=None: {"verdict": "ok"})
    monkeypatch.setattr(HK, "run_lane_e",
                        lambda cfg: {"verdict": "ok", "publish_due": "no"})
    filed = []
    monkeypatch.setattr(HK, "file_mind_seed_stale_investigate",
                        lambda r, d: (filed.append(r), {"filed": True})[1])
    rec = HK.do_run(dict(CFG), "test", log_path=tmp_path / "log.jsonl")
    assert filed == [] and "investigate_lane_e" not in rec


QUEUE_FILE = "aspirations" ".jsonl"      # the name _recent_investigate_exists reads


def _queue_with(tmp_path, goal):
    d = tmp_path / "q"
    d.mkdir()
    (d / QUEUE_FILE).write_text(
        json.dumps({"id": "asp-1", "goals": [goal]}) + "\n", encoding="utf-8")
    return d


def _ten_days_ago():
    return (dt.datetime.now() - dt.timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S")


def test_lane_e_old_open_duplicate_suppresses_on_the_shared_predicate(tmp_path, monkeypatch):
    sig = HK.MIND_SEED_STALE_ORIGIN_SIGNAL
    q = _queue_with(tmp_path, {"id": "g-1", "status": "pending",
                               "created_at": _ten_days_ago(), "origin_signal": sig})
    monkeypatch.setattr(HK, "WORLD_DIR", str(q))
    monkeypatch.setattr(HK, "AGENT_DIR", None)
    # 10 days old, far outside the 48h window: only the open-status predicate
    # can see it, and lane E needs no flag of its own to get it ().
    assert HK._recent_investigate_exists(sig) is True


def test_lane_e_old_closed_goal_does_not_suppress(tmp_path, monkeypatch):
    sig = HK.MIND_SEED_STALE_ORIGIN_SIGNAL
    q = _queue_with(tmp_path, {"id": "g-1", "status": "completed",
                               "created_at": _ten_days_ago(), "origin_signal": sig})
    monkeypatch.setattr(HK, "WORLD_DIR", str(q))
    monkeypatch.setattr(HK, "AGENT_DIR", None)
    assert HK._recent_investigate_exists(sig) is False


# ── filer attribution: an unbound tick must name its own box () ─────
#
# The retired placeholder token is ASSEMBLED FROM FRAGMENTS, never written as a
# source literal. test_no_placeholder_token_in_source scans housekeeping-tick.py,
# and this test file sits in the same tree — a literal fixture here would become a
# genuine violation in the file that holds it, and the only escapes are a
# self-exclusion (a permanent blind spot over exactly the file most likely to
# contain the pattern) or a scan surface narrowed to dodge its own tests. Both are
# worse than one join (guard-1855).
RETIRED_PLACEHOLDER = "<" + "see description" + ">"


def test_filer_id_names_agent_and_host_when_unbound(monkeypatch):
    """The unbound tick is the ORDINARY case for the cadence runner."""
    monkeypatch.delenv("MIND_AGENT", raising=False)
    filer = HK._filer_id()
    host = __import__("socket").gethostname()
    assert filer == f"unbound@{host}"
    assert host in filer                      # the box is named, not implied
    assert RETIRED_PLACEHOLDER not in filer


def test_filer_id_names_the_bound_agent_and_host(monkeypatch):
    monkeypatch.setenv("MIND_AGENT", "echo")
    host = __import__("socket").gethostname()
    assert HK._filer_id() == f"echo@{host}"


def test_no_placeholder_token_in_source():
    """Reverting the fix puts the token back and fails HERE.

    Whole-file scan, no exclusions: the fix removed the token from the two
    filers AND declined to quote it in the new helper's docstring, so zero is
    honestly achievable across the entire file.
    """
    src = (SCRIPT_DIR / "housekeeping-tick.py").read_text(encoding="utf-8")
    assert RETIRED_PLACEHOLDER not in src


@pytest.mark.parametrize("filer_fn,dedup_signal", [
    ("file_investigate", "ORIGIN_SIGNAL"),
    ("file_skip_streak_investigate", "SKIP_STREAK_ORIGIN_SIGNAL"),
])
def test_filer_description_names_host_and_resolved_ledger(
        monkeypatch, tmp_path, filer_fn, dedup_signal):
    """Both filers: the description must say WHICH box and WHICH ledger.

    `_add_goal` is monkeypatched to capture, so no goal is ever filed and the
    test never shells out to aspirations-add-goal.sh.
    """
    monkeypatch.delenv("MIND_AGENT", raising=False)
    ledger = tmp_path / "housekeeping-unbound.jsonl"
    monkeypatch.setenv("HK_LOG_PATH", str(ledger))
    monkeypatch.setattr(HK, "_recent_investigate_exists", lambda *a, **k: False)
    captured = {}
    monkeypatch.setattr(HK, "_add_goal", lambda payload: captured.setdefault("p", payload))

    getattr(HK, filer_fn)("some-reason", "some-detail")

    desc = captured["p"]["description"]
    host = __import__("socket").gethostname()
    assert host in desc, "description must name the filing box"
    assert str(ledger) in desc, "description must name the RESOLVED ledger path"
    assert RETIRED_PLACEHOLDER not in desc
    # The self-referential form is gone: the filer id resolves to a real
    # agent@host pair, so the reader is never pointed back at this text.
    assert f"unbound@{host}" in desc


# ── open-goal dedup () ─────────────────────────────────────────────

def _one_goal_queue(tmp_path, monkeypatch, status, age_hours, signal=None):
    """A world queue holding one goal with the origin_signal, and no agent queue."""
    created = (dt.datetime.now() - dt.timedelta(hours=age_hours)).strftime(
        "%Y-%m-%dT%H:%M:%S")
    goal = {"id": "g-999-01", "status": status, "created_at": created,
            "origin_signal": signal or HK.SKIP_STREAK_ORIGIN_SIGNAL}
    (tmp_path / "aspirations.jsonl").write_text(
        json.dumps({"id": "asp-999", "goals": [goal]}) + "\n", encoding="utf-8")
    monkeypatch.setattr(HK, "WORLD_DIR", str(tmp_path))     # guard-2102
    monkeypatch.setattr(HK, "AGENT_DIR", None)


def _add_goal_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(HK, "_add_goal",
                        lambda payload: calls.append(payload) or {"filed": True})
    return calls


STALE = HK.DEDUP_HOURS + 24


def test_open_goal_past_the_window_suppresses_skip_streak_filing(tmp_path, monkeypatch):
    _one_goal_queue(tmp_path, monkeypatch, "pending", STALE)
    calls = _add_goal_calls(monkeypatch)
    out = HK.file_skip_streak_investigate("lane_b.skipped=no-scratch-root", "[]")
    assert out == {"filed": False, "suppressed": "recent-duplicate"}
    assert calls == []


@pytest.mark.parametrize("status", ["in-progress", "blocked"])
def test_every_open_status_suppresses_past_the_window(tmp_path, monkeypatch, status):
    _one_goal_queue(tmp_path, monkeypatch, status, STALE)
    assert HK._recent_investigate_exists(HK.SKIP_STREAK_ORIGIN_SIGNAL) is True


def test_closed_goal_past_the_window_allows_refiling(tmp_path, monkeypatch):
    # POSITIVE CONTROL: closing the goal releases the suppression (guard-3419).
    _one_goal_queue(tmp_path, monkeypatch, "completed", STALE)
    calls = _add_goal_calls(monkeypatch)
    out = HK.file_skip_streak_investigate("lane_b.skipped=no-scratch-root", "[]")
    assert out == {"filed": True}
    assert [c["origin_signal"] for c in calls] == [HK.SKIP_STREAK_ORIGIN_SIGNAL]


def test_closed_goal_inside_the_window_still_suppresses(tmp_path, monkeypatch):
    _one_goal_queue(tmp_path, monkeypatch, "completed", 1)
    assert HK._recent_investigate_exists(HK.SKIP_STREAK_ORIGIN_SIGNAL) is True


def test_open_goal_under_another_signal_does_not_suppress(tmp_path, monkeypatch):
    _one_goal_queue(tmp_path, monkeypatch, "pending", STALE, signal=HK.ORIGIN_SIGNAL)
    calls = _add_goal_calls(monkeypatch)
    assert HK.file_skip_streak_investigate("lane_b.skipped=x", "[]") == {"filed": True}
    assert len(calls) == 1


def test_lane_a_filer_shares_the_open_goal_suppression(tmp_path, monkeypatch):
    _one_goal_queue(tmp_path, monkeypatch, "pending", STALE, signal=HK.ORIGIN_SIGNAL)
    calls = _add_goal_calls(monkeypatch)
    assert HK.file_investigate("purge-error", "{}") == {
        "filed": False, "suppressed": "recent-duplicate"}
    assert calls == []


# ── _state_path: the unbound branch () ─────────────────────────────

def _unbound(monkeypatch, tmp_path):
    """Make HK look like a tick spawned with no agent binding.

    SCRIPT_DIR is redirected so the box-local stamp lands in tmp_path/logs and
    never touches the real core/logs/ (guard-1039: no production paths).
    """
    monkeypatch.delenv("HK_STATE_PATH", raising=False)
    monkeypatch.setattr(HK, "AGENT_DIR", None)
    monkeypatch.setattr(HK, "SCRIPT_DIR", tmp_path / "scripts")
    return tmp_path / "logs" / "housekeeping-tick-state-unbound.json"


def test_state_path_unbound_is_box_local_never_none(monkeypatch, tmp_path):
    """The pre-fix `return None` is what this pins.

    load_state(None) answers {} and save_state(None) is a silent no-op, so a
    None here disengages EVERY interval gate in this module on the unbound
    path -- do_tick's `interval_hours` and lane D's
    `transcript_archive_interval_hours` alike -- with no error and no log
    line. Measured on cc-05 over 24h (g-358-179): 57 executed unbound ticks,
    lane_d on all 57, 8,109,400,693 bytes of transcript re-PUT, while the
    BOUND path on the same box ran lane_d twice and skipped twice.
    """
    expected = _unbound(monkeypatch, tmp_path)

    sp = HK._state_path()

    assert sp is not None, "an unbound tick must still have a stamp to gate on"
    assert sp == expected
    assert sp.is_absolute(), "a shared resolver returns an absolute Path (guard-552)"


def test_state_path_unbound_stamp_is_per_machine_not_per_agent(monkeypatch, tmp_path):
    """The filename carries no agent component, deliberately.

    Lane D archives transcripts/<machine>/, so its effect is a property of the
    BOX. Resolving this through the agent fall-through would bind an unbound
    tick to whichever agent dir sorts first on a multi-agent box (guard-4048),
    and naming it after an agent would re-introduce the per-agent multiplier
    the bound stamp already has (guard-2585, guard-6523).
    """
    _unbound(monkeypatch, tmp_path)
    monkeypatch.setenv("MIND_AGENT", "alpha")

    assert "alpha" not in HK._state_path().name
    assert HK._state_path().name == "housekeeping-tick-state-unbound.json"


def test_state_path_precedence_env_then_agent_then_box(monkeypatch, tmp_path):
    """Positive control for the two branches the fallback must NOT swallow."""
    monkeypatch.setattr(HK, "SCRIPT_DIR", tmp_path / "scripts")

    monkeypatch.setattr(HK, "AGENT_DIR", str(tmp_path / "agents" / "alpha"))
    monkeypatch.delenv("HK_STATE_PATH", raising=False)
    assert HK._state_path() == tmp_path / "agents" / "alpha" / "session" / \
        "housekeeping-tick-state.json"

    monkeypatch.setenv("HK_STATE_PATH", str(tmp_path / "explicit.json"))
    assert HK._state_path() == tmp_path / "explicit.json", \
        "the env override outranks a bound AGENT_DIR"

    monkeypatch.setattr(HK, "AGENT_DIR", None)
    assert HK._state_path() == tmp_path / "explicit.json", \
        "and it outranks the box-local fallback too"


def test_unbound_double_lane_d_runs_at_most_once(monkeypatch, tmp_path):
    """The goal's verification outcome, end to end.

    Two back-to-back unbound lane-D runs with NO injected state_path: the
    first archives and stamps, the second reads that stamp back off the
    box-local file and returns not-due. Against the pre-fix `_state_path()`
    both return `ok`, which is exactly the 57-in-24h shape.
    """
    expected = _unbound(monkeypatch, tmp_path)
    now = dt.datetime(2026, 9, 17, 12, 0, 0)

    first = HK.run_lane_d(DCFG, archive_cmd=_stub_archive(RECEIPT), now=now)
    second = HK.run_lane_d(DCFG, archive_cmd=_stub_archive(RECEIPT),
                           now=now + dt.timedelta(hours=3))

    assert first["verdict"] == "ok"
    assert second["verdict"] == "not-due"
    assert second["interval_hours"] == 12
    assert expected.is_file(), "the stamp must actually persist to disk"
    assert json.loads(expected.read_text(encoding="utf-8"))[
        "last_transcript_archive"] == "2026-09-17T12:00:00"


def test_unbound_lane_d_is_due_again_after_the_interval(monkeypatch, tmp_path):
    """Positive control for the guard above: the throttle must EXPIRE.

    A fix that pinned the unbound path to not-due forever would pass the
    regression test and silently disable transcript archiving on every
    assistant box.
    """
    _unbound(monkeypatch, tmp_path)
    now = dt.datetime(2026, 9, 17, 12, 0, 0)

    HK.run_lane_d(DCFG, archive_cmd=_stub_archive(RECEIPT), now=now)
    later = HK.run_lane_d(DCFG, archive_cmd=_stub_archive(RECEIPT),
                          now=now + dt.timedelta(hours=13))

    assert later["verdict"] == "ok"
