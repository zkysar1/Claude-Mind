""": late close of abandoned Body manifests, on the box that ran them.

Two properties carry the goal and every test pins one of them:
  CLOSE  a Body whose session the harness registry proves gone is closed by the
         ORDINARY close — staged exactly as a genuine close stages it, marked,
         carrier mirrored — and nothing in its session dir is removed;
  HOLD   a session that is running, or whose state cannot be proven, is never
         transitioned.

guard-4166: a pass that closes NOTHING satisfies every HOLD assertion, and a
pass that closes EVERYTHING satisfies every CLOSE assertion. So the closing
tests keep a live session in the same run, and the holding tests flip exactly
one input against a control that closes.
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CORE))

import abandoned_sessions as ab  # noqa: E402

_spec = importlib.util.spec_from_file_location("body_manifest", CORE / "body-manifest.py")
bm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bm)

AGENT = "alpha"


@pytest.fixture(autouse=True)
def _hermetic_world_staged(tmp_path, monkeypatch):
    """Point the staged-WM root at a TMP world for every test in this file.

    Load-bearing, not tidiness (g-115-9750). `close_body_on_genuine` stages
    world-rooted now, resolving through `_paths.WORLD_DIR`, so without this
    fixture a producer test WRITES INTO THE LIVE `world/` — the guard-955
    production-key collision class, from a test that looks hermetic because
    every path it constructs itself is under tmp_path.

    MEASURED, not hypothetical: this file was missed in the first fixture
    sweep and the very next run left
    `world/body-staged-wm/alpha/dddddddd-0000-4000-8000-000000000002-wm{,.hash,-baseline}`
    (10 B / 7 B / 10 B) in the live world at 09:12:34. It never reached the
    STORE only because `STORAGE_BACKEND=local` was set, which is exactly the
    containment guard-955 mandates.

    Patching the module ATTRIBUTE works because `world_staged_dir` does its
    `from _paths import WORLD_DIR` inside the function body.
    """
    import _paths
    w = tmp_path / "world"
    w.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_paths, "WORLD_DIR", w, raising=False)
    return w


def _staged_dir(tmp_path):
    """Where close_body_on_genuine stages now (): world-rooted."""
    return tmp_path / "world" / "body-staged-wm" / AGENT
CUR = "cccccccc-0000-4000-8000-000000000001"   # the session running the pass
DEAD = "dddddddd-0000-4000-8000-000000000002"
LIVE = "eeeeeeee-0000-4000-8000-000000000003"
HOST = "box-a"
DOMAIN = "linux:m1:pid:[4026530001]"
BOOT = 1_000_000.0
NOW = BOOT + 86_400.0
QUIET = NOW - 3_600.0          # an hour without activity

CUR_PID, LIVE_PID, DEAD_PID = 10, 20, 30


class FakeProc:
    def __init__(self, alive, unknowable=False):
        self.alive = dict(alive)   # pid -> start ticks
        self.unknowable = unknowable

    def exists(self, pid):
        return None if self.unknowable else pid in self.alive

    def start_ticks(self, pid):
        return self.alive.get(pid)


@pytest.fixture
def pushes(monkeypatch):
    """Capture explicit backend pushes; route the pass to THIS body-manifest."""
    got = []

    class _Backend:
        def write_bytes(self, path, content):
            got.append(Path(path).name)

    mod = type(sys)("storage_backend")
    mod.get_backend = lambda: _Backend()
    monkeypatch.setitem(sys.modules, "storage_backend", mod)
    monkeypatch.setitem(sys.modules, "body_manifest", bm)
    return got


def _body(root, sid, *, role="worker", forked=True, remote=True, state="active",
          machine_id=HOST, mtime=QUIET, carrier_host=HOST):
    sess = root / "agents" / AGENT / "sessions" / sid
    sess.mkdir(parents=True)
    st = root / "agents" / AGENT / "session"
    st.mkdir(parents=True, exist_ok=True)
    lines = [f"unitKey: '{sid}'", f"mindKey: '{AGENT}'", "env_id: 'local'",
             f"role: '{role}'", f"body_state: '{state}'",
             "forked_wm_hash: 'abc123'" if forked else "forked_wm_hash: null",
             f"remote_body: {'true' if remote else 'false'}",
             f"machine_id: '{machine_id}'"]
    (sess / "body-manifest.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if forked:
        (sess / "working-memory.yaml").write_bytes(b"slots: {}\n")
        (sess / "forked-wm-baseline.yaml").write_bytes(b"slots: {}\n")
    if carrier_host is not None:
        (st / f"body-heartbeat-{sid}.json").write_text(json.dumps(
            {"sid": sid, "host": carrier_host, "ts": "2026-09-09T20:00:00",
             "body_state": state}) + "\n", encoding="utf-8")
    for f in sess.iterdir():
        os.utime(f, (mtime, mtime))
    return sess, st


def _entry(sid, pid, *, start="1", started_ms=(BOOT + 10) * 1000, domain=DOMAIN):
    return {"pid": pid, "sessionId": sid, "procStart": start,
            "startedAt": started_ms, "pidDomain": domain}


def _config(root, entries, transcripts=(CUR, DEAD, LIVE), t_mtime=QUIET):
    cfg = root / "claude"
    (cfg / "sessions").mkdir(parents=True)
    (cfg / "projects" / "-opt-mind").mkdir(parents=True)
    for e in entries:
        (cfg / "sessions" / f"{e['pid']}.json").write_text(json.dumps(e), encoding="utf-8")
    for sid in transcripts:
        t = cfg / "projects" / "-opt-mind" / f"{sid}.jsonl"
        t.write_text("{}\n", encoding="utf-8")
        os.utime(t, (t_mtime, t_mtime))
    return cfg


def _run(root, cfg, proc, **kw):
    return ab.late_close_pass(root, kw.pop("current_sid", CUR), now=NOW,
                              config_dir=cfg, proc=proc, boot_epoch=BOOT,
                              pid_domain=DOMAIN, host=HOST, machine_ids={HOST}, **kw)


def _state(root, sid):
    text = (root / "agents" / AGENT / "sessions" / sid / "body-manifest.yaml").read_text(
        encoding="utf-8")
    return ab._BODY_STATE_RE.search(text).group(1)


def _carrier_state(root, sid):
    return json.loads((root / "agents" / AGENT / "session" /
                       f"body-heartbeat-{sid}.json").read_text(encoding="utf-8"))["body_state"]


def _held(summary, sid):
    return [h["reason"] for h in summary["held"] if h["sid"] == sid]


def _standard(tmp_path, **dead_kw):
    """CUR and LIVE running; DEAD gone. Returns (cfg, proc)."""
    for sid in (CUR, LIVE):
        _body(tmp_path, sid)
    _body(tmp_path, DEAD, **dead_kw)
    cfg = _config(tmp_path, [_entry(CUR, CUR_PID, start="100"),
                             _entry(LIVE, LIVE_PID, start="200")])
    return cfg, FakeProc({CUR_PID: "100", LIVE_PID: "200"})


# ------------------------------------------------------------------ CLOSE


def test_dead_worker_closes_by_ordinary_path_and_live_session_is_untouched(tmp_path, pushes):
    cfg, proc = _standard(tmp_path)
    sess = tmp_path / "agents" / AGENT / "sessions" / DEAD
    before = {p.name for p in sess.iterdir()}

    s = _run(tmp_path, cfg, proc)

    assert s["authoritative"] is True
    assert s["closed"] == [{"agent": AGENT, "sid": DEAD, "result": "marked"}]
    assert _state(tmp_path, DEAD) == "closed-pending-merge"
    assert _carrier_state(tmp_path, DEAD) == "closed-pending-merge"
    staged = _staged_dir(tmp_path)
    assert sorted(p.name for p in staged.iterdir()) == sorted(
        [f"{DEAD}-wm.yaml", f"{DEAD}-wm-baseline.yaml", f"{DEAD}-wm.hash"])
    # The staged WM files AND the carrier (). The carrier push is the
    # delivery half of the mirror asserted on line 159: writing `body_state`
    # locally is not the same as a peer being able to read it, and after a close
    # this Body never ticks again, so nothing else will ever deliver it.
    assert sorted(pushes) == sorted(
        [p.name for p in staged.iterdir()] + [f"body-heartbeat-{DEAD}.json"])
    # Not a deletion: every file the Body had is still there.
    assert before <= {p.name for p in sess.iterdir()}
    assert (sess / "working-memory.yaml").read_bytes() == b"slots: {}\n"
    # The live control and the session running the pass are untouched.
    assert _state(tmp_path, LIVE) == "active" and _carrier_state(tmp_path, LIVE) == "active"
    assert _state(tmp_path, CUR) == "active"
    assert _held(s, LIVE) == ["running"]
    assert all(h["sid"] != CUR for h in s["held"])  # never even a candidate


def test_non_forking_body_closes_stale_and_stages_nothing(tmp_path, pushes):
    cfg, proc = _standard(tmp_path, role="reducer", forked=False, remote=False)
    s = _run(tmp_path, cfg, proc)
    assert s["closed"] == [{"agent": AGENT, "sid": DEAD, "result": "marked-stale"}]
    assert _state(tmp_path, DEAD) == "closed-stale"
    assert _carrier_state(tmp_path, DEAD) == "closed-stale"
    assert not _staged_dir(tmp_path).exists()
    # STAGES nothing, but still DELIVERS the carrier (): this Body
    # forked no WM so there is nothing to merge, yet its `closed-stale` state
    # must still reach peers or it is counted as a live stall forever.
    assert pushes == [f"body-heartbeat-{DEAD}.json"]
    assert _state(tmp_path, LIVE) == "active"


def test_same_box_worker_closes_without_staging_like_a_genuine_close(tmp_path, pushes):
    cfg, proc = _standard(tmp_path, remote=False)
    s = _run(tmp_path, cfg, proc)
    assert s["closed"][0]["result"] == "marked"
    assert _state(tmp_path, DEAD) == "closed-pending-merge"
    assert not _staged_dir(tmp_path).exists()
    # Same-box close stages nothing (the WM is already where the reducer reads
    # it) but still delivers the carrier — see  above.
    assert pushes == [f"body-heartbeat-{DEAD}.json"]


def test_leftover_close_sentinel_is_consumed(tmp_path, pushes):
    cfg, proc = _standard(tmp_path)
    sentinel = tmp_path / "agents" / AGENT / "sessions" / DEAD / "body-closing"
    sentinel.write_text("", encoding="utf-8")
    os.utime(sentinel, (QUIET, QUIET))
    _run(tmp_path, cfg, proc)
    assert _state(tmp_path, DEAD) == "closed-pending-merge"
    assert not sentinel.exists()


# ------------------------------------------------------- HOLD (with controls)


@pytest.mark.parametrize("current_listed", [True, False])
def test_registry_must_vouch_for_the_current_session(tmp_path, pushes, current_listed):
    for sid in (CUR, DEAD):
        _body(tmp_path, sid)
    entries = [_entry(CUR, CUR_PID, start="100")] if current_listed else []
    cfg = _config(tmp_path, entries)
    s = _run(tmp_path, cfg, FakeProc({CUR_PID: "100"}))
    if current_listed:
        assert _state(tmp_path, DEAD) == "closed-pending-merge"
    else:
        assert s["authoritative"] is False
        assert _held(s, DEAD) == ["current-session-not-live-in-registry"]
        assert _state(tmp_path, DEAD) == "active"


def test_no_current_sid_closes_nothing(tmp_path, pushes):
    cfg, proc = _standard(tmp_path)
    s = _run(tmp_path, cfg, proc, current_sid="")
    assert _held(s, DEAD) == ["no-current-sid"]
    assert _state(tmp_path, DEAD) == "active"


def test_unreadable_registry_entry_closes_nothing(tmp_path, pushes):
    cfg, proc = _standard(tmp_path)
    (cfg / "sessions" / "999.json").write_text("{half a wri", encoding="utf-8")
    s = _run(tmp_path, cfg, proc)
    assert _held(s, DEAD) == ["registry-unreadable:1"]
    assert _state(tmp_path, DEAD) == "active"


@pytest.mark.parametrize("ticks_now, expect", [("300", "active"), ("999", "closed-pending-merge")])
def test_pid_reuse_is_detected_by_process_start(tmp_path, pushes, ticks_now, expect):
    """DEAD's recorded pid is alive. Same start ticks = the same process = hold;
    different start ticks = the pid now names another process = gone."""
    for sid in (CUR, DEAD):
        _body(tmp_path, sid)
    cfg = _config(tmp_path, [_entry(CUR, CUR_PID, start="100"),
                             _entry(DEAD, DEAD_PID, start="300")])
    _run(tmp_path, cfg, FakeProc({CUR_PID: "100", DEAD_PID: ticks_now}))
    assert _state(tmp_path, DEAD) == expect


@pytest.mark.parametrize("started_ms, expect", [
    ((BOOT - 3_600) * 1000, "closed-pending-merge"),   # began before the box started
    ((BOOT + 3_600) * 1000, "active"),                 # after boot, foreign namespace: unknowable
])
def test_foreign_namespace_entry_is_dead_only_when_it_predates_boot(tmp_path, pushes,
                                                                    started_ms, expect):
    for sid in (CUR, DEAD):
        _body(tmp_path, sid)
    cfg = _config(tmp_path, [_entry(CUR, CUR_PID, start="100"),
                             _entry(DEAD, DEAD_PID, start="300", started_ms=started_ms,
                                    domain="linux:m1:pid:[4026539999]")])
    s = _run(tmp_path, cfg, FakeProc({CUR_PID: "100", DEAD_PID: "300"}))
    assert _state(tmp_path, DEAD) == expect
    if expect == "active":
        assert _held(s, DEAD) == ["liveness-unknown"]


def test_platform_that_cannot_see_processes_closes_nothing(tmp_path, pushes):
    cfg, _ = _standard(tmp_path)
    proc = FakeProc({CUR_PID: "100", LIVE_PID: "200"}, unknowable=True)
    s = _run(tmp_path, cfg, proc)
    assert s["authoritative"] is False  # even the positive control is unknowable
    assert _state(tmp_path, DEAD) == "active"


@pytest.mark.parametrize("mtime, expect", [(NOW - 60, "active"), (QUIET, "closed-pending-merge")])
def test_recent_activity_holds(tmp_path, pushes, mtime, expect):
    cfg, proc = _standard(tmp_path, mtime=mtime)
    _run(tmp_path, cfg, proc)
    assert _state(tmp_path, DEAD) == expect


def test_recent_transcript_write_holds(tmp_path, pushes):
    cfg, proc = _standard(tmp_path)
    t = cfg / "projects" / "-opt-mind" / f"{DEAD}.jsonl"
    os.utime(t, (NOW - 60, NOW - 60))
    s = _run(tmp_path, cfg, proc)
    assert _held(s, DEAD) == ["recent-activity"]
    assert _state(tmp_path, DEAD) == "active"


def test_session_without_a_transcript_in_this_config_dir_holds(tmp_path, pushes):
    for sid in (CUR, DEAD):
        _body(tmp_path, sid)
    cfg = _config(tmp_path, [_entry(CUR, CUR_PID, start="100")], transcripts=(CUR,))
    s = _run(tmp_path, cfg, FakeProc({CUR_PID: "100"}))
    assert _held(s, DEAD) == ["no-transcript"]
    assert _state(tmp_path, DEAD) == "active"


@pytest.mark.parametrize("kw, reason", [
    ({"machine_id": "box-b"}, "foreign-machine"),
    ({"carrier_host": "box-b"}, "foreign-host"),
])
def test_another_boxes_copy_is_never_closed(tmp_path, pushes, kw, reason):
    cfg, proc = _standard(tmp_path, **kw)
    s = _run(tmp_path, cfg, proc)
    assert _held(s, DEAD) == [reason]
    assert _state(tmp_path, DEAD) == "active"


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX ownership only")
def test_session_dir_owned_by_another_user_holds(tmp_path, pushes):
    cfg, proc = _standard(tmp_path)
    s = _run(tmp_path, cfg, proc, uid=os.getuid() + 1)
    assert _held(s, DEAD) == ["foreign-owner"]
    assert _state(tmp_path, DEAD) == "active"


@pytest.mark.parametrize("state", ["parked", "closed-pending-merge", "merged", "closed-stale"])
def test_only_active_manifests_are_candidates(tmp_path, pushes, state):
    cfg, proc = _standard(tmp_path, state=state)
    s = _run(tmp_path, cfg, proc)
    assert _state(tmp_path, DEAD) == state
    assert all(c["sid"] != DEAD for c in s["closed"]) and not _held(s, DEAD)


def test_session_that_appears_before_the_write_holds(tmp_path, pushes):
    """The registry is re-read immediately before each write; a resume that
    lands between the scan and the write must win."""
    for sid in (CUR, DEAD):
        _body(tmp_path, sid)
    cfg = _config(tmp_path, [])
    reads = []

    def reader():
        reads.append(1)
        entries = [_entry(CUR, CUR_PID, start="100")]
        if len(reads) > 1:
            entries.append(_entry(DEAD, DEAD_PID, start="300"))
        return entries, 0

    s = _run(tmp_path, cfg, FakeProc({CUR_PID: "100", DEAD_PID: "300"}),
             registry_reader=reader)
    assert _held(s, DEAD) == ["registry-changed:session-appeared"]
    assert _state(tmp_path, DEAD) == "active"


def test_dry_run_writes_nothing(tmp_path, pushes):
    cfg, proc = _standard(tmp_path)
    s = _run(tmp_path, cfg, proc, dry_run=True)
    assert s["closed"] == [{"agent": AGENT, "sid": DEAD, "result": "would-close"}]
    assert _state(tmp_path, DEAD) == "active"
    assert not _staged_dir(tmp_path).exists()


# ------------------------------------------------------- close_body_late unit


def test_close_body_late_never_closes_a_park(tmp_path, pushes):
    _body(tmp_path, DEAD, state="parked")
    assert bm.close_body_late(DEAD, AGENT, tmp_path) == "not-active"
    assert _state(tmp_path, DEAD) == "parked"


def test_close_body_late_without_a_manifest(tmp_path, pushes):
    (tmp_path / "agents" / AGENT / "sessions" / DEAD).mkdir(parents=True)
    assert bm.close_body_late(DEAD, AGENT, tmp_path) == "no-manifest"


def test_close_body_late_mirrors_the_carrier_that_survives_the_reap(tmp_path, pushes):
    """THE POINT OF WIRING THIS INTO THE REAP ( item c).

    cleanup-stale-bindings.sh rm -rf's the session dir, so the manifest it just
    stamped dies with it. What SURVIVES is the carrier in session/ (singular),
    and worker_stall.py reads the CARRIER, not the manifest. If the late close
    did not mirror it, every reap would mint a permanent orphan carrier reading
    `active` that the stall probe counts as a live stall forever.
    """
    _body(tmp_path, DEAD)
    assert _carrier_state(tmp_path, DEAD) == "active"

    assert bm.close_body_late(DEAD, AGENT, tmp_path) in ("marked", "marked-push-failed")

    # The carrier is the assertion that matters: it outlives the directory.
    assert _carrier_state(tmp_path, DEAD) != "active"
    assert _carrier_state(tmp_path, DEAD) == _state(tmp_path, DEAD)


def test_close_body_late_is_idempotent_so_a_second_stage_cannot_corrupt(tmp_path, pushes):
    """The one hazard in wiring this before `rm -rf`: for a REMOTE Body both
    _preserve_unmerged_body_wm and _mark_pending_merge stage the same files, and
    _preserve's trigger-last ordering is called out in its own comment as
    LOAD-BEARING against a concurrent generalize_down.

    Same content either way, so the risk is redundancy rather than corruption —
    but a second writer into that directory deserves a test rather than a shrug.
    A re-close must be a no-op, never a partial re-stage.
    """
    _body(tmp_path, DEAD, remote=True)
    first = bm.close_body_late(DEAD, AGENT, tmp_path)
    assert first in ("marked", "marked-push-failed")
    staged = sorted(q.name for q in
                    _staged_dir(tmp_path).iterdir())
    assert staged, "a remote Body must stage something for the reducer to consume"

    # Second call: the state is no longer `active`, so it declines.
    assert bm.close_body_late(DEAD, AGENT, tmp_path) == "not-active"
    assert sorted(q.name for q in
                  _staged_dir(tmp_path).iterdir()) == staged


def test_close_body_late_is_reachable_from_bash(monkeypatch, capsys):
    """The gap that made item (c) a unit rather than a one-liner.

    close_body_late existed with ONE in-process caller and NO CLI verb, and
    cleanup-stale-bindings.sh is bash — so the reap could not reach it at all.
    This pins the VERB and its DISPATCH, not the function: drop the subparser
    entry and argparse exits 2 on an invalid choice; point the branch elsewhere
    and the recorder never fires. Either way the reap silently goes back to
    leaving active carriers behind with nothing else failing.

    Deliberately hermetic — it monkeypatches the function so no project root,
    manifest or filesystem is involved. The wiring is the whole subject.
    """
    seen = []
    monkeypatch.setattr(bm, "close_body_late", lambda sid, agent: seen.append((sid, agent)) or "marked")
    bm.main(["close-body-late", "--sid", DEAD, "--agent", AGENT])
    assert seen == [(DEAD, AGENT)]
    assert capsys.readouterr().out.strip() == "marked"


# ------------------------------------------------------------------ the hook


def test_hook_spawns_nothing_when_only_the_current_session_is_active(tmp_path, monkeypatch):
    _body(tmp_path, CUR)
    monkeypatch.setattr(ab, "PROJECT_ROOT", tmp_path)

    def _no_spawn(*a, **k):
        raise AssertionError("spawned with no candidate")

    monkeypatch.setattr(ab.subprocess, "Popen", _no_spawn)
    assert ab._hook(json.dumps({"session_id": CUR})) == 0


def test_hook_spawns_the_pass_when_another_session_reads_active(tmp_path, monkeypatch):
    _body(tmp_path, CUR)
    _body(tmp_path, DEAD)
    monkeypatch.setattr(ab, "PROJECT_ROOT", tmp_path)
    calls = []
    monkeypatch.setattr(ab.subprocess, "Popen", lambda argv, **k: calls.append(argv))
    assert ab._hook(json.dumps({"session_id": CUR})) == 0
    assert len(calls) == 1 and "--run" in calls[0] and CUR in calls[0]


# ---  unit 2 (bravo/cc-05): the CLEAN zero must be distinguishable
# --- from the BLIND zero. Outcome 1 of the goal is verified by re-running this
# --- pass and reading its zero, so the two must not serialize identically.
# --- Per this module's guard-4166 discipline each test flips exactly ONE input
# --- against a control that reports the opposite verdict.

def test_clean_box_reports_an_authoritative_zero(tmp_path, pushes):
    """No candidates, agents root readable -> authoritative, with a reason."""
    _body(tmp_path, CUR)  # only the current session is active: nothing to close
    cfg = _config(tmp_path, [_entry(CUR, CUR_PID, start="100")])
    s = _run(tmp_path, cfg, FakeProc({CUR_PID: "100"}))
    assert s["scanned"] == 0
    assert s["closed"] == [] and s["held"] == []
    assert s["authoritative"] is True
    assert s["authority"] == "ok-nothing-to-close"


def test_blind_scan_reports_a_NON_authoritative_zero(tmp_path, pushes):
    """CONTROL for the test above — one input flipped: the agents root is
    unreadable, so active_manifests() swallows the OSError and returns the SAME
    empty list. The zero must NOT come back authoritative."""
    _body(tmp_path, CUR)
    cfg = _config(tmp_path, [_entry(CUR, CUR_PID, start="100")])
    missing = tmp_path / "no-such-root"  # never created
    s = ab.late_close_pass(missing, CUR, now=NOW, config_dir=cfg,
                           proc=FakeProc({CUR_PID: "100"}), boot_epoch=BOOT,
                           pid_domain=DOMAIN, host=HOST, machine_ids={HOST})
    assert s["scanned"] == 0
    assert s["closed"] == [] and s["held"] == []
    assert s["authoritative"] is False
    assert str(s["authority"]).startswith("agents-root-unreadable:")


def test_the_two_zeros_do_not_serialize_identically(tmp_path, pushes):
    """The property that actually matters to a fleet-wide verifier reading JSON:
    a clean box and a blind box must be tellable apart from the summary alone."""
    _body(tmp_path, CUR)
    cfg = _config(tmp_path, [_entry(CUR, CUR_PID, start="100")])
    proc = FakeProc({CUR_PID: "100"})
    clean = _run(tmp_path, cfg, proc)
    blind = ab.late_close_pass(tmp_path / "no-such-root", CUR, now=NOW,
                               config_dir=cfg, proc=proc, boot_epoch=BOOT,
                               pid_domain=DOMAIN, host=HOST, machine_ids={HOST})
    assert clean["scanned"] == blind["scanned"] == 0
    assert json.dumps(clean, sort_keys=True) != json.dumps(blind, sort_keys=True)


# ------------------------------------- orphan-carrier reconcile (unit 23)
#  unit 23. The two early returns above ('not-active', 'no-manifest')
# were the only close_body_late paths that wrote NOTHING, and they are exactly
# the two states an orphan carrier is in. Unit 22 root-caused the surviving
# `active` carriers to the own-cloud claim fence; that fence was fixed
# 2026-09-04 (, carve-out live at all three gates), and a cc-03 census
# then found a phantom minted AFTER it — so the residue is made on THIS path.
# guard-4166: each reconcile test flips exactly one input against a control that
# does NOT reconcile (the negative control below), so a function that repaired
# everything would fail as loudly as one that repaired nothing.


def _carrier(root, sid):
    return root / "agents" / AGENT / "session" / f"body-heartbeat-{sid}.json"


def _carrier_ts(root, sid):
    return json.loads(_carrier(root, sid).read_text(encoding="utf-8"))["ts"]


def _manifest_says(root, sid, state):
    """Set the manifest's state WITHOUT going through set_state, which would
    mirror the carrier and so destroy the divergence under test."""
    mp = root / "agents" / AGENT / "sessions" / sid / "body-manifest.yaml"
    mp.write_text(mp.read_text(encoding="utf-8").replace(
        "body_state: 'active'", f"body_state: '{state}'"), encoding="utf-8")


def test_late_close_reconciles_a_carrier_the_manifest_already_finished(tmp_path, pushes):
    """'not-active' must still repair the CARRIER (hole b).

    A reap stamps the manifest closed and then rm -rf's it; if anything re-runs
    the late close afterward it hits `!= active` and used to decline, leaving the
    surviving carrier reading `active` forever. worker_stall reads the carrier.
    """
    _body(tmp_path, DEAD, state="active")
    _manifest_says(tmp_path, DEAD, "closed-stale")
    assert _carrier_state(tmp_path, DEAD) == "active"
    before = _carrier_ts(tmp_path, DEAD)

    assert bm.close_body_late(DEAD, AGENT, tmp_path) == "not-active"

    assert _carrier_state(tmp_path, DEAD) == "closed-stale"
    # THE LOAD-BEARING ASSERTION: a repair is not a tick. classify_body returns
    # V_ALIVE on freshness BEFORE it reads body_state, so bumping `ts` here would
    # trade a false stall for a phantom LIVE Body that fleet-live-bodies and
    # reducer_promotion both act on.
    assert _carrier_ts(tmp_path, DEAD) == before


def test_late_close_reconciles_an_orphan_carrier_whose_manifest_is_gone(tmp_path, pushes):
    """'no-manifest' must still repair the CARRIER (hole a).

    This is unit 1's own "no manifest left to close" population (78777e3c,
    a7fe3fd4 on cc-10): the manifest dies inside the session dir, the carrier
    lives in session/ (singular) and survives.
    """
    import shutil
    sess, _ = _body(tmp_path, DEAD, state="active")
    before = _carrier_ts(tmp_path, DEAD)
    shutil.rmtree(sess)                      # precisely what the reap's rm -rf does

    assert bm.close_body_late(DEAD, AGENT, tmp_path) == "no-manifest"

    assert _carrier_state(tmp_path, DEAD) == "closed-stale"
    assert _carrier_ts(tmp_path, DEAD) == before


def test_late_close_never_un_finishes_a_carrier_that_already_closed(tmp_path, pushes):
    """NEGATIVE CONTROL (guard-4166) — the reconcile is not a blanket mirror.

    Only an `active` carrier is repaired. Here the manifest is `parked` while the
    carrier already reads closed: mirroring the manifest over it would resurrect
    a finished Body, the one direction that cannot be undone.
    """
    _body(tmp_path, DEAD, state="parked")
    c = _carrier(tmp_path, DEAD)
    doc = json.loads(c.read_text(encoding="utf-8"))
    doc["body_state"] = "closed-pending-merge"
    c.write_text(json.dumps(doc) + "\n", encoding="utf-8")

    assert bm.close_body_late(DEAD, AGENT, tmp_path) == "not-active"
    assert _carrier_state(tmp_path, DEAD) == "closed-pending-merge"


def test_why_the_timestamp_must_survive_the_repair():
    """Pins the THREE classifier readings the repair depends on, so a future
    edit that bumps `ts` fails here with the reason attached rather than quietly
    converting stall alerts into phantom live Bodies."""
    import worker_stall as ws
    assert ws.classify_body(600.0, False, 60, "active") == ws.V_STALLED_NO_CLOSE
    assert ws.classify_body(600.0, False, 60, "closed-stale") == ws.V_STALE_NO_CLAIM
    # Freshness is tested BEFORE body_state — this is the regression the
    # ts-preserving repair avoids.
    assert ws.classify_body(1.0, False, 60, "closed-stale") == ws.V_ALIVE


# ------------------------------- delivery verdict for the repair (unit 28)
#  unit 28. Unit 26 measured the reconcile FIRING on three cc-09
# carriers, every push refused `no_claim`, and the pass reporting rc=0 with
# empty stdout and empty stderr — i.e. success. Three layers each discarded the
# signal: _push_carrier returned None, _reconcile_orphan_carrier returned True
# unconditionally, and the call site branched on an rc the handler pins at 0.
# On a worker box a refused push is the STEADY STATE (the carrier sits in the
# claim-fenced agent tree, unit 22), so "repaired" was wrong far more often than
# it was right, and after the local write the call site's `active` pre-filter
# never revisits that carrier — the box looks clean locally and is untouched
# authoritatively, forever.
#
# guard-4166 shape: the two detectors below flip exactly ONE input (whether the
# backend accepts the write) against controls that must keep the bare verdict,
# so neither a version that suffixed everything nor one that suffixed nothing
# can pass the set.


@pytest.fixture
def refused_pushes(monkeypatch):
    """A backend that REFUSES the write — the no_claim fence, in miniature.

    Sibling of `pushes`, differing in one behaviour, because that is the single
    variable these tests turn. The real refusal is storage_backend raising
    NoClaimError from a non-claim-holding box; what matters here is only that
    `write_bytes` raises, which is the shape `_push_carrier` catches.
    """
    attempts = []

    class _Backend:
        def write_bytes(self, path, content):
            attempts.append(Path(path).name)
            raise RuntimeError("NoClaimError: no_claim (write did not land)")

    mod = type(sys)("storage_backend")
    mod.get_backend = lambda: _Backend()
    monkeypatch.setitem(sys.modules, "storage_backend", mod)
    monkeypatch.setitem(sys.modules, "body_manifest", bm)
    return attempts


def test_push_carrier_reports_whether_delivery_landed(tmp_path, refused_pushes):
    """LAYER 0. It already knew — it caught the exception — and threw it away."""
    c = tmp_path / "body-heartbeat-x.json"
    c.write_text('{"body_state": "active"}\n', encoding="utf-8")
    assert bm._push_carrier(c) is False
    assert refused_pushes == ["body-heartbeat-x.json"], "the push must be ATTEMPTED"


def test_push_carrier_reports_a_delivery_that_landed(tmp_path, pushes):
    """CONTROL for the above: the same call on an accepting backend."""
    c = tmp_path / "body-heartbeat-y.json"
    c.write_text('{"body_state": "active"}\n', encoding="utf-8")
    assert bm._push_carrier(c) is True


def test_orphan_repair_that_never_reached_peers_says_so(tmp_path, refused_pushes):
    """THE REGRESSION, 'no-manifest' path. Reported success while peers kept
    reading `active` — the exact cc-09 measurement behind this unit."""
    import shutil
    sess, _ = _body(tmp_path, DEAD, state="active")
    shutil.rmtree(sess)

    assert bm.close_body_late(DEAD, AGENT, tmp_path) == "no-manifest-push-failed"


def test_finished_manifest_repair_that_never_reached_peers_says_so(tmp_path, refused_pushes):
    """THE REGRESSION, 'not-active' path. Same defect, reached through the other
    manifest state, which is why the suffix is applied by one shared helper."""
    _body(tmp_path, DEAD, state="active")
    _manifest_says(tmp_path, DEAD, "closed-stale")

    assert bm.close_body_late(DEAD, AGENT, tmp_path) == "not-active-push-failed"


def test_a_repair_that_did_reach_peers_keeps_the_bare_verdict(tmp_path, pushes):
    """CONTROL (guard-4166) — success must NOT acquire the suffix.

    This is also why every pre-existing caller and equality test keeps working:
    only the failure case is new. A fix that suffixed unconditionally would be
    caught here rather than in production.
    """
    import shutil
    sess, _ = _body(tmp_path, DEAD, state="active")
    shutil.rmtree(sess)

    assert bm.close_body_late(DEAD, AGENT, tmp_path) == "no-manifest"


def test_no_repair_attempted_keeps_the_bare_verdict(tmp_path, refused_pushes):
    """CONTROL — 'nothing to repair' is not a delivery failure.

    The backend here WOULD refuse, but there is no carrier to push, so the
    reconcile never reaches the mirror and the verdict must stay bare. Without
    this, a fix that keyed the suffix on the backend rather than on an attempted
    repair would pass the two detectors above.
    """
    import shutil
    sess, _ = _body(tmp_path, DEAD, state="active")
    _carrier(tmp_path, DEAD).unlink()
    shutil.rmtree(sess)

    assert bm.close_body_late(DEAD, AGENT, tmp_path) == "no-manifest"
    assert refused_pushes == [], "no carrier means no push was attempted"


def test_the_local_repair_still_happens_when_delivery_is_refused(tmp_path, refused_pushes):
    """THE FAIL-OPEN IS UNCHANGED, and this pins it (guard-373).

    Unit 26 declined to collapse the three layers' fail-opens because each is
    separately justified; this unit threads a VALUE through them instead. So the
    local write must still land and nothing may raise — only the report differs.
    The manifest already says closed, so the local carrier agreeing with it is
    correct in itself; what was wrong was calling that delivery.
    """
    import shutil
    sess, _ = _body(tmp_path, DEAD, state="active")
    before = _carrier_ts(tmp_path, DEAD)
    shutil.rmtree(sess)

    bm.close_body_late(DEAD, AGENT, tmp_path)

    assert _carrier_state(tmp_path, DEAD) == "closed-stale"
    assert _carrier_ts(tmp_path, DEAD) == before, "a repair is still not a tick"


def test_the_call_site_reads_the_verdict_and_not_only_the_rc():
    """LAYER 3 IS WIRED — guard-6374: a new output field is not wired until the
    CONSUMER predicate reads it, and neither a green suite nor a mutation proof
    can tell you that.

    Both call sites in cleanup-stale-bindings.sh previously sent stdout to
    /dev/null and branched on an rc that `main` pins at 0, so their WARN arms
    were unreachable by construction (guard-5501). This asserts the verdict is
    captured and matched, which is the half a Python-only test cannot see.
    """
    src = (Path(__file__).resolve().parents[1] / "cleanup-stale-bindings.sh"
           ).read_text(encoding="utf-8")
    assert src.count("_CBL_OUT=\"$(py -3") == 2, "both call sites must capture stdout"
    assert src.count("*-push-failed)") == 2, "both call sites must branch on it"
    assert "close-body-late \\\n                    --sid \"$_BIND_SID\" --agent \"$_BA\" >/dev/null" not in src
