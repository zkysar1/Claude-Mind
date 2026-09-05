"""recovery_yank.py + recovery-yank-reverse.sh — the consumer recovery-gate.sh
never had (g-357-51 part 3, the 2026-09-01 fleet-wide false-recovery incident).

Three layers, each pinned here:

  1. `classify()` — the pure worker-side verdict over already-read facts.
  2. The `check` / `preconditions` / `record-reversal` CLIs over a tmp agent dir.
  3. `recovery-yank-reverse.sh` end to end inside a physical-copy sandbox root
     (the script resolves everything through `_paths.sh`, so the sandbox must
     carry its own core/scripts — same discipline as
     test_stop_hook_in_flight_integration.py).

Plus the structural pins that make "post-yank resurrection-or-notify" a
falsifiable claim: the stop hook must hand a demoted SID to the reversal BEFORE
its not-RUNNING gate, and the worker-loop park sequence must classify the park
and name the notification. Silent terminal is the measured defect; a mutation
that removes either invocation turns these red.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent
CORE = SCRIPTS.parent
REAL_ROOT = CORE.parent
for p in (str(SCRIPTS), str(TESTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

import recovery_yank as ry  # noqa: E402
from _bash_helpers import BASH  # noqa: E402

PY = sys.executable
YANK_TS = "2026-09-01T07:01:33"
NOW = "2026-09-01T08:00:00"
SID = "live-sid-1111-2222"
OTHER_SID = "other-sid-9999"
AGENT = "alpha"


def _yank(ts=YANK_TS, sid=SID, path="A", action="recover"):
    e = {"ts": ts, "agent": AGENT, "path": path,
         "cause": "crashed runner: state=RUNNING, heartbeat stale, ...",
         "sid_recorded": sid, "acting_sid": "hook-sid", "source": "startup"}
    if action is not None:
        e["action"] = action
    return e


def _agent_dir(tmp_path: Path, entries=(), state="IDLE", mode="autonomous",
               binding=True, binding_started="2026-09-01T03:00:00",
               running_sid=None, signals=()) -> Path:
    adir = tmp_path / "agents" / AGENT
    sess = adir / "session"
    sess.mkdir(parents=True, exist_ok=True)
    if entries:
        (sess / "recovery-log.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    (sess / "agent-state").write_text(state, encoding="utf-8")
    (sess / "agent-mode").write_text(mode, encoding="utf-8")
    if running_sid:
        (sess / "running-session-id").write_text(running_sid + "\n", encoding="utf-8")
    for s in signals:
        (sess / s).write_text("", encoding="utf-8")
    if binding:
        b = adir / "sessions" / SID
        b.mkdir(parents=True, exist_ok=True)
        (b / "binding.yaml").write_text(
            f"session_id: {SID}\nagent: {AGENT}\nmode: autonomous\n"
            f"started_at: '{binding_started}'\nstarted_by: claude-code\n",
            encoding="utf-8")
    return adir


def _touch_at(p: Path, iso: str) -> None:
    t = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S")
    # mtimes are read back as UTC wall time; write them the same way.
    epoch = (t - datetime(1970, 1, 1)).total_seconds()
    if not p.exists():
        p.write_text("", encoding="utf-8")
    os.utime(p, (epoch, epoch))


def _run_py(*args, env=None):
    e = dict(os.environ)
    e.setdefault("STORAGE_BACKEND", "local")
    e["RT_NO_AUTOSPAWN"] = "1"
    if env:
        e.update(env)
    return subprocess.run([PY, str(SCRIPTS / "recovery_yank.py"), *args],
                          capture_output=True, text=True, env=e)


# ───────────────────────── 1. classify (pure) ─────────────────────────

def _cls(**kw):
    base = dict(local_yank=None, marker_yank=None, reversed_entry=None,
                user_stops=[], agent_state="IDLE", running_sid=None,
                escalated_ts=None)
    base.update(kw)
    return ry.classify(**base)


def test_classify_none_without_any_yank():
    v = _cls()
    assert v["verdict"] == "none" and v["yank"] is None
    assert ry.VERDICT_RC[v["verdict"]] == 2


def test_classify_recovery_yank_when_nothing_post_dates_it():
    v = _cls(local_yank=_yank())
    assert v["verdict"] == "recovery-yank"
    assert v["escalated_before"] is False
    assert ry.VERDICT_RC["recovery-yank"] == 0


def test_classify_user_stop_when_a_stop_artifact_follows_the_yank():
    v = _cls(local_yank=_yank(),
             user_stops=[{"signal": "stop-requested", "ts": "2026-09-01T07:30:00"}])
    assert v["verdict"] == "user-stop"
    assert "stop-requested" in v["reason"]


def test_classify_ignores_stop_artifacts_that_predate_the_yank():
    v = _cls(local_yank=_yank(),
             user_stops=[{"signal": "last-stop-reason", "ts": "2026-08-30T00:00:00"}])
    assert v["verdict"] == "recovery-yank"


def test_classify_none_once_reversed():
    v = _cls(local_yank=_yank(), reversed_entry={"action": "yank_reversed", "ts": "2026-09-01T07:05:00"})
    assert v["verdict"] == "none" and "reversed" in v["reason"]


def test_classify_none_when_a_start_post_dates_the_yank():
    v = _cls(local_yank=_yank(), agent_state="RUNNING", running_sid="new-sid")
    assert v["verdict"] == "none" and "/start" in v["reason"]


def test_classify_escalated_before_matches_only_the_same_yank():
    assert _cls(local_yank=_yank(), escalated_ts=YANK_TS)["escalated_before"] is True
    assert _cls(local_yank=_yank(), escalated_ts="2026-08-01T00:00:00")["escalated_before"] is False


def test_classify_cross_box_marker_alone_is_a_yank():
    m = ry.marker_to_yank(json.dumps({"ts": YANK_TS, "path": "A", "cause": "x",
                                      "sid_recorded": SID, "acting_sid": "h"}))
    assert m and m["source_channel"] == "team-state"
    assert _cls(marker_yank=m)["verdict"] == "recovery-yank"


def test_classify_prefers_the_newer_of_local_and_marker():
    newer = ry.marker_to_yank({"ts": "2026-09-01T07:30:00", "path": "D", "cause": "later"})
    v = _cls(local_yank=_yank(), marker_yank=newer)
    assert v["yank"]["ts"] == "2026-09-01T07:30:00"


def test_marker_to_yank_rejects_garbage():
    assert ry.marker_to_yank("not json") is None
    assert ry.marker_to_yank({"no": "ts"}) is None
    assert ry.marker_to_yank(None) is None


# ───────────────────────── 2. log reads + CLI check ─────────────────────────

def test_latest_yank_accepts_the_pre_g357_shape_and_skips_corrupt_lines(tmp_path):
    adir = _agent_dir(tmp_path)
    log = adir / "session" / "recovery-log.jsonl"
    log.write_text(json.dumps(_yank(action=None)) + "\n{not json\n", encoding="utf-8")
    entries = ry.read_log_entries(adir)
    assert len(entries) == 1
    assert ry.latest_yank(entries)["sid_recorded"] == SID


def test_check_cli_recovery_yank_rc0_and_marks_escalated_once(tmp_path):
    adir = _agent_dir(tmp_path, entries=[_yank()])
    r = _run_py("check", "--agent-dir", str(adir), "--no-team-state", "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    d = json.loads(r.stdout.strip().splitlines()[-1])
    assert d["verdict"] == "recovery-yank" and d["escalated_before"] is False

    r2 = _run_py("check", "--agent-dir", str(adir), "--no-team-state", "--json", "--mark-escalated")
    assert r2.returncode == 0
    sentinel = adir / "session" / ry.ESCALATED_SENTINEL
    assert sentinel.read_text(encoding="utf-8").strip() == YANK_TS

    r3 = _run_py("check", "--agent-dir", str(adir), "--no-team-state", "--json")
    d3 = json.loads(r3.stdout.strip().splitlines()[-1])
    assert r3.returncode == 0 and d3["escalated_before"] is True


def test_check_cli_user_stop_rc1_when_handoff_is_newer_than_the_yank(tmp_path):
    adir = _agent_dir(tmp_path, entries=[_yank()])
    (adir / "session" / "handoff.yaml").write_text("session: 1\n", encoding="utf-8")  # mtime = now > yank
    r = _run_py("check", "--agent-dir", str(adir), "--no-team-state", "--json")
    assert r.returncode == 1, r.stdout + r.stderr
    d = json.loads(r.stdout.strip().splitlines()[-1])
    assert d["verdict"] == "user-stop"
    assert any(s["signal"] == "handoff.yaml" for s in d["user_stops"])


def test_check_cli_handoff_older_than_the_yank_does_not_count(tmp_path):
    adir = _agent_dir(tmp_path, entries=[_yank()])
    _touch_at(adir / "session" / "handoff.yaml", "2026-08-31T00:00:00")
    r = _run_py("check", "--agent-dir", str(adir), "--no-team-state", "--json")
    assert r.returncode == 0, r.stdout + r.stderr


def test_check_cli_user_stop_reason_file_after_the_yank(tmp_path):
    adir = _agent_dir(tmp_path, entries=[_yank()])
    (adir / "session" / "last-stop-reason").write_text(
        "path=user-stop\nstopped_at=2026-09-01T07:40:00\nagent=alpha\nuser_initiated=True\n",
        encoding="utf-8")
    r = _run_py("check", "--agent-dir", str(adir), "--no-team-state", "--json")
    assert r.returncode == 1
    assert json.loads(r.stdout.strip().splitlines()[-1])["verdict"] == "user-stop"


def test_check_cli_the_yanks_own_reason_file_is_not_a_user_stop(tmp_path):
    adir = _agent_dir(tmp_path, entries=[_yank()])
    (adir / "session" / "last-stop-reason").write_text(
        "path=recovery-gate-zombie\nstopped_at=2026-09-01T07:01:34\nagent=alpha\n",
        encoding="utf-8")
    r = _run_py("check", "--agent-dir", str(adir), "--no-team-state", "--json")
    assert r.returncode == 0, r.stdout + r.stderr


def test_check_cli_none_rc2_without_a_log(tmp_path):
    adir = _agent_dir(tmp_path)
    r = _run_py("check", "--agent-dir", str(adir), "--no-team-state", "--json")
    assert r.returncode == 2


def test_check_cli_injected_team_state_marker_is_a_cross_box_yank(tmp_path):
    adir = _agent_dir(tmp_path)
    marker = json.dumps({"ts": YANK_TS, "path": "A", "cause": "c", "sid_recorded": SID, "acting_sid": "h"})
    r = _run_py("check", "--agent-dir", str(adir), "--team-state-marker", marker, "--json")
    assert r.returncode == 0
    d = json.loads(r.stdout.strip().splitlines()[-1])
    assert d["yank"]["source_channel"] == "team-state"


# ───────────────────────── 3. preconditions ─────────────────────────

def _pre(adir, sid=SID, now=NOW, window=360):
    return ry.evaluate_preconditions(adir, AGENT, sid, ry.parse_ts(now), window)


def test_preconditions_happy_path(tmp_path):
    adir = _agent_dir(tmp_path, entries=[_yank()])
    res = _pre(adir)
    assert res["ok"] is True, res["reasons"]
    assert res["yank"]["sid_recorded"] == SID


@pytest.mark.parametrize("knob, expect", [
    ("other-sid", "not this sid"),
    ("window", "reversal window"),
    ("running", "not IDLE"),
    ("assistant-mode", "not autonomous"),
    ("new-binding", "post-dates the yank"),
    ("no-binding", "binding.yaml"),
    ("stop-requested", "stop-requested is present"),
    ("stop-target-mode", "stop-target-mode is present"),
    ("user-stop-reason", "user-stop evidence"),
    ("handoff", "handoff.yaml"),
    ("other-runner", "another runner"),
    ("reversed", "already reversed"),
    ("no-yank", "no `recover` entry"),
])
def test_preconditions_each_miss_is_a_no_op(tmp_path, knob, expect):
    entries = [_yank()]
    kw = {}
    sid = SID
    now = NOW
    if knob == "other-sid":
        entries = [_yank(sid=OTHER_SID)]
    elif knob == "window":
        now = "2026-09-01T14:00:00"  # 6h59m after the yank
    elif knob == "running":
        kw["state"] = "RUNNING"
    elif knob == "assistant-mode":
        kw["mode"] = "assistant"
    elif knob == "new-binding":
        kw["binding_started"] = "2026-09-01T07:30:00"
    elif knob == "no-binding":
        kw["binding"] = False
    elif knob in ("stop-requested", "stop-target-mode"):
        kw["signals"] = (knob,)
    elif knob == "other-runner":
        kw["running_sid"] = OTHER_SID
    elif knob == "reversed":
        entries = [_yank(), {"ts": "2026-09-01T07:10:00", "action": "yank_reversed", "agent": AGENT}]
    elif knob == "no-yank":
        entries = [{"ts": YANK_TS, "action": "suppressed", "agent": AGENT, "path": "A"}]
    adir = _agent_dir(tmp_path, entries=entries, **kw)
    if knob == "user-stop-reason":
        (adir / "session" / "last-stop-reason").write_text(
            "path=user-stop\nstopped_at=2026-09-01T07:45:00\n", encoding="utf-8")
    if knob == "handoff":
        (adir / "session" / "handoff.yaml").write_text("x: 1\n", encoding="utf-8")
        _touch_at(adir / "session" / "handoff.yaml", "2026-09-01T07:50:00")
    res = _pre(adir, sid=sid, now=now)
    assert res["ok"] is False
    assert any(expect in r for r in res["reasons"]), res["reasons"]


def test_preconditions_cli_rc_and_env_window(tmp_path):
    adir = _agent_dir(tmp_path, entries=[_yank()])
    ok = _run_py("preconditions", "--agent-dir", str(adir), "--sid", SID, "--now", NOW)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    late = _run_py("preconditions", "--agent-dir", str(adir), "--sid", SID, "--now", "2026-09-01T09:30:00",
                   env={"RECOVERY_YANK_REVERSE_WINDOW_MINUTES": "60"})
    assert late.returncode == 1
    assert "reversal window" in late.stdout


def test_record_reversal_appends_audit_row_and_rewrites_notice(tmp_path):
    adir = _agent_dir(tmp_path, entries=[_yank()])
    (adir / "session" / "recovery-notice").write_text("Crashed-runner auto-recovery ...", encoding="utf-8")
    r = _run_py("record-reversal", "--agent-dir", str(adir), "--sid", SID, "--now", NOW)
    assert r.returncode == 0, r.stdout + r.stderr
    rows = ry.read_log_entries(adir)
    assert rows[-1]["action"] == "yank_reversed"
    assert rows[-1]["sid_recorded"] == SID and rows[-1]["acting_sid"] == SID
    assert rows[-1]["evidence"]["yank"]["ts"] == YANK_TS
    assert "REVERSED" in (adir / "session" / "recovery-notice").read_text(encoding="utf-8")
    marker = json.loads(r.stdout)["team_state_marker"]
    assert marker["reversed_at"] == NOW and marker["reversed_by"] == SID
    # and the classifier now reads it as resolved
    chk = _run_py("check", "--agent-dir", str(adir), "--no-team-state", "--json")
    assert chk.returncode == 2


# ───────────────────────── 4. recovery-yank-reverse.sh in a sandbox ─────────────────────────

def _copy_core(dest_root: Path) -> None:
    (dest_root / "core" / "logs").mkdir(parents=True, exist_ok=True)
    ignore = shutil.ignore_patterns("__pycache__", "tests")
    for name in ("scripts", "config"):
        shutil.copytree(CORE / name, dest_root / "core" / name, ignore=ignore, symlinks=False)


def _sandbox(tmp_path: Path, **kw) -> tuple[Path, Path]:
    root = tmp_path / "root"
    root.mkdir()
    _copy_core(root)
    world = tmp_path / "world"
    meta = tmp_path / "meta"
    world.mkdir()
    meta.mkdir()
    adir = _agent_dir(root, **kw)
    (adir / "local-paths.conf").write_text(
        f"WORLD_PATH={world.as_posix()}\nMETA_PATH={meta.as_posix()}\n", encoding="utf-8")
    return root, adir


def _run_reverse(root: Path, *args):
    env = dict(os.environ)
    env.update({"MIND_AGENT": AGENT, "STORAGE_BACKEND": "local", "RT_NO_AUTOSPAWN": "1",
                "RUNTIME_DIR": str(root / "rt")})
    env.pop("MIND_SID", None)
    return subprocess.run([BASH, str(root / "core" / "scripts" / "recovery-yank-reverse.sh"),
                           "--agent", AGENT, "--sid", SID, *args],
                          capture_output=True, text=True, env=env, timeout=240)


def _recent_yank():
    ts = (datetime.utcnow() - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%S")
    return _yank(ts=ts)


def test_reverse_dry_run_reports_without_writing(tmp_path):
    root, adir = _sandbox(tmp_path, entries=[_recent_yank()],
                          binding_started=(datetime.utcnow() - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S"))
    r = _run_reverse(root, "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "DRY RUN" in r.stderr
    assert (adir / "session" / "agent-state").read_text(encoding="utf-8").strip() == "IDLE"
    assert not (adir / "session" / "running-session-id").exists()


def test_reverse_restores_running_for_the_demoted_live_sid(tmp_path):
    started = (datetime.utcnow() - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")
    root, adir = _sandbox(tmp_path, entries=[_recent_yank()], binding_started=started)
    r = _run_reverse(root)
    assert r.returncode == 0, r.stdout + r.stderr
    sess = adir / "session"
    assert sess.joinpath("agent-state").read_text(encoding="utf-8").strip() == "RUNNING"
    assert sess.joinpath("running-session-id").read_text(encoding="utf-8").strip() == SID
    assert sess.joinpath("latest-session-id").read_text(encoding="utf-8").strip() == SID
    assert len(sess.joinpath("runner-token").read_text(encoding="utf-8").strip()) >= 32
    rows = ry.read_log_entries(adir)
    assert rows[-1]["action"] == "yank_reversed" and rows[-1]["sid_recorded"] == SID
    assert "REVERSED" in sess.joinpath("recovery-notice").read_text(encoding="utf-8")
    assert not list(sess.glob("*.pre-reverse"))
    # idempotent: a second call is a no-op (state is RUNNING, yank already reversed)
    again = _run_reverse(root)
    assert again.returncode == 1
    assert "already reversed" in again.stderr or "not IDLE" in again.stderr


def test_reverse_is_a_no_op_after_a_user_stop(tmp_path):
    started = (datetime.utcnow() - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")
    root, adir = _sandbox(tmp_path, entries=[_recent_yank()], binding_started=started,
                          signals=("stop-requested",))
    r = _run_reverse(root)
    assert r.returncode == 1, r.stdout + r.stderr
    sess = adir / "session"
    assert sess.joinpath("agent-state").read_text(encoding="utf-8").strip() == "IDLE"
    assert not sess.joinpath("running-session-id").exists()
    assert not sess.joinpath("runner-token").exists()
    assert all(e.get("action") != "yank_reversed" for e in ry.read_log_entries(adir))


def test_reverse_refuses_a_session_that_started_after_the_yank(tmp_path):
    root, adir = _sandbox(tmp_path, entries=[_recent_yank()],
                          binding_started=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"))
    r = _run_reverse(root)
    assert r.returncode == 1
    assert "post-dates the yank" in r.stderr


# ───────────────────────── 5. structural: resurrection-or-notify ─────────────────────────

def test_stop_hook_hands_a_demoted_sid_to_the_reversal_before_its_running_gate():
    src = (SCRIPTS / "stop-hook.sh").read_text(encoding="utf-8")
    call = src.index('recovery-yank-reverse.sh" --agent "$HOOK_AGENT" --sid "$HOOK_SID"')
    gate = src.index('if [ "$STATE" != "RUNNING" ]; then')
    assert call < gate, "the reversal must run BEFORE the not-RUNNING allow, or a yanked live loop still dies silently"
    assert 'STATE=$(bash "$CORE_ROOT/scripts/session-state-get.sh"' in src[call:gate], \
        "the hook must RE-READ agent-state after a reversal so the RUNNING gates see the restored state"


def test_worker_loop_park_sequence_classifies_the_park_and_notifies():
    src = (REAL_ROOT / ".claude" / "skills" / "worker-loop" / "SKILL.md").read_text(encoding="utf-8")
    assert 'recovery_yank.py check --agent "$MIND_AGENT"' in src
    assert "Notify the user about the recovery yank" in src
    assert "--mark-escalated" in src
    assert "park-due" in src, "Phase -0 must ask park-due before the full re-poll (part 4)"
    # ordering: classify BEFORE the terminal ScheduleWakeup of the park sequence
    seq = src.index("THE PARK SEQUENCE")
    assert src.index("recovery_yank.py check", seq) < src.index("Tool (not Bash): ScheduleWakeup", seq)


def test_deadman_parked_branch_asks_park_due():
    src = (SCRIPTS / "deadman-directive.sh").read_text(encoding="utf-8")
    assert "park-due" in src


def test_session_manifest_preserves_the_escalation_sentinel():
    src = (CORE / "config" / "session-manifest.yaml").read_text(encoding="utf-8")
    assert "recovery-yank-escalated" in src


def test_state_mismatch_landing_is_wired_before_both_iteration_complete_imperatives():
    for name in ("iteration-close.sh", "recurring-close.sh"):
        src = (SCRIPTS / name).read_text(encoding="utf-8")
        call = src.index("state-mismatch-landing.sh")
        imperative = src.index("ITERATION COMPLETE ═══")
        assert call < imperative, name


def _run_landing(root: Path, sid=SID, body_role=None):
    env = dict(os.environ)
    env.update({"MIND_AGENT": AGENT, "STORAGE_BACKEND": "local", "RT_NO_AUTOSPAWN": "1",
                "RUNTIME_DIR": str(root / "rt")})
    env.pop("MIND_SID", None)
    # BODY_ROLE decides whether the landing may fire at all (), and the
    # PreToolUse bash hook injects it into every Bash call — so on a WORKER box
    # dict(os.environ) silently carries BODY_ROLE=worker and every landing test
    # below would go RED there while staying green on the reducer's box. That is
    # the guard-1515 environment axis: a suite result is a claim about ONE box's
    # env, not about the code. Pin it explicitly instead of inheriting; the
    # default is the REDUCER shape (absent), which is what these tests assert.
    env.pop("BODY_ROLE", None)
    if body_role is not None:
        env["BODY_ROLE"] = body_role
    return subprocess.run([BASH, str(root / "core" / "scripts" / "state-mismatch-landing.sh"),
                           "--agent", AGENT, "--sid", sid],
                          capture_output=True, text=True, env=env, timeout=240)


def test_landing_is_silent_while_running(tmp_path):
    root, _ = _sandbox(tmp_path, state="RUNNING")
    r = _run_landing(root)
    assert r.returncode == 1 and "LANDING" not in r.stdout


def test_landing_prints_the_consolidate_directive_when_the_yank_cannot_be_reversed(tmp_path):
    # yank of ANOTHER sid: this session cannot reverse it, so it must land gracefully
    root, _ = _sandbox(tmp_path, entries=[_recent_yank()])
    r = _run_landing(root, sid=OTHER_SID)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "STATE MISMATCH" in r.stdout and "aspirations-consolidate" in r.stdout
    assert "do NOT call Skill(aspirations)" in r.stdout
    assert "ITERATION COMPLETE" not in r.stdout


def test_landing_is_silent_on_a_worker_body_whose_idle_state_is_by_design(tmp_path):
    """A worker Body is agent-state=IDLE BY DESIGN, so IDLE is not a mismatch.

    guard-5821: agent-state is a REDUCER-OWNED signal exactly one Body writes, at
    /start and /stop; a worker never writes it. Before g-115-8903 the landing
    fired at EVERY worker close and told the worker to run /aspirations-consolidate
    — a reducer-only phase — over its own unmerged state.

    The reducer run is the POSITIVE CONTROL: same sandbox, same sid, same script;
    only BODY_ROLE differs. Without it a broken guard that suppressed everything
    would still pass the worker assertion.
    """
    root, _ = _sandbox(tmp_path, entries=[_recent_yank()])

    reducer = _run_landing(root, sid=OTHER_SID)
    assert reducer.returncode == 0, reducer.stdout + reducer.stderr
    assert "STATE MISMATCH" in reducer.stdout

    worker = _run_landing(root, sid=OTHER_SID, body_role="worker")
    assert worker.returncode == 1, worker.stdout + worker.stderr
    assert worker.stdout == ""
    assert "LANDING" not in worker.stderr

    # The guard lower-cases before comparing, so the hook's casing cannot matter.
    upper = _run_landing(root, sid=OTHER_SID, body_role="WORKER")
    assert upper.returncode == 1, upper.stdout + upper.stderr


def test_landing_reverses_a_true_yank_and_continues(tmp_path):
    started = (datetime.utcnow() - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")
    root, adir = _sandbox(tmp_path, entries=[_recent_yank()], binding_started=started)
    r = _run_landing(root)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "REVERSED" in r.stderr
    assert (adir / "session" / "agent-state").read_text(encoding="utf-8").strip() == "RUNNING"
