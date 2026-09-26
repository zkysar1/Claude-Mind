"""Tests for deadman-arm-audit.py refused-arm detection (, outcome 2).

Before this fix the audit counted a deadman arm's ScheduleWakeup tool_use as an
arm WITHOUT reading its tool_result, so a harness-REFUSED arm (the CC 2.1.280
"`noop` is required when `stop` is not true." required-field refusal) — where the
call was EMITTED but NO net was set — reported as a healthy arm. That is the true
silent-death form the batched/followed/orphan trio structurally miss: all three
assume the 600s net armed.

The fix: _collect_events reads each arm's tool_result and marks `refused` iff
is_error is truthy; _build_report counts refused arms separately (excluded from
arms_total) and _verdict returns REFUSED (dominating every other verdict).

guard-2421 positive control: each behaviour is pinned with BOTH a known-refused
fixture (detection FIRES) and a known-healthy fixture (no false flag).
"""
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone

_SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

_AUDIT_PY = os.path.join(_SCRIPTS, "deadman-arm-audit.py")
_spec = importlib.util.spec_from_file_location("deadman_arm_audit", _AUDIT_PY)
daa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(daa)

SENTINEL = "<<autonomous-loop-dynamic>>"
CUTOFF = datetime(2026, 1, 1, tzinfo=timezone.utc)  # everything after Jan 2026
# The EXACT refusal content measured from a live transcript (b34aa80f, 2026-09-24
# A/B). The backticks around the field names are part of the harness message.
REFUSAL_MSG = "`noop` is required when `stop` is not true."
SUCCESS_MSG = "Next wakeup scheduled for 01:22:00 (in 600s). Nothing more to do this turn."


# ── fixture builders ───────────────────────────────────────────────────────────

def _arm_use(ts, uid, prompt=SENTINEL, ds=600, with_skill=False):
    content = [{"type": "tool_use", "name": "ScheduleWakeup", "id": uid,
                "input": {"prompt": prompt, "delaySeconds": ds}}]
    if with_skill:
        content.append({"type": "tool_use", "name": "Skill",
                        "input": {"skill": "aspirations", "args": "loop"}})
    return json.dumps({"timestamp": ts,
                       "message": {"id": "msg-" + uid, "role": "assistant",
                                   "content": content}})


def _result(ts, uid, content, is_error=None):
    item = {"type": "tool_result", "tool_use_id": uid, "content": content}
    if is_error is not None:
        item["is_error"] = is_error
    return json.dumps({"timestamp": ts,
                       "message": {"role": "user", "content": [item]}})


def _write(dir_path, name, lines):
    p = dir_path / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _mk_project(tmp_path, agent, sid, disabled=False):
    """A minimal project root that maps <sid> -> <agent> via latest-session-id and
    flags the agent deadman-active (no deadman-disabled sentinel)."""
    root = tmp_path / "proj"
    sess = root / "agents" / agent / "session"
    sess.mkdir(parents=True)
    (sess / "latest-session-id").write_text(sid, encoding="utf-8")
    if disabled:
        (sess / "deadman-disabled").write_text("", encoding="utf-8")
    return root


def _recent(minutes_ago):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


# ── _collect_events: the core refused-arm marking ──────────────────────────────

def test_refused_arm_is_marked_refused(tmp_path):
    # POSITIVE CONTROL: a sentinel arm whose tool_result carries is_error=True is
    # marked refused (the net was NOT set) — this is the whole point of outcome 2.
    p = _write(tmp_path, "t.jsonl", [
        _arm_use("2026-09-24T01:01:00Z", "u1"),
        _result("2026-09-24T01:01:01Z", "u1", REFUSAL_MSG, is_error=True),
    ])
    arms, _skmids, _skev = daa._collect_events(p, CUTOFF)
    assert len(arms) == 1
    assert arms[0]["refused"] is True


def test_successful_arm_is_not_refused(tmp_path):
    # NEGATIVE CONTROL: a sentinel arm with a success result (is_error absent) is
    # NOT marked refused — a healthy arm must never be false-flagged.
    p = _write(tmp_path, "t.jsonl", [
        _arm_use("2026-09-24T01:12:00Z", "u2"),
        _result("2026-09-24T01:12:01Z", "u2", SUCCESS_MSG),
    ])
    arms, _a, _b = daa._collect_events(p, CUTOFF)
    assert len(arms) == 1
    assert arms[0]["refused"] is False


def test_arm_with_missing_result_is_not_refused(tmp_path):
    # Fail-safe on absent evidence (verify-before-assuming): no tool_result for
    # the arm's id -> refused stays False. A truncated transcript must not
    # manufacture a refusal out of a missing result.
    p = _write(tmp_path, "t.jsonl", [_arm_use("2026-09-24T02:00:00Z", "u3")])
    arms, _a, _b = daa._collect_events(p, CUTOFF)
    assert len(arms) == 1
    assert arms[0]["refused"] is False


def test_is_error_false_explicit_is_not_refused(tmp_path):
    # is_error explicitly False (not merely absent) is a success too.
    p = _write(tmp_path, "t.jsonl", [
        _arm_use("2026-09-24T02:10:00Z", "u4"),
        _result("2026-09-24T02:10:01Z", "u4", SUCCESS_MSG, is_error=False),
    ])
    arms, _a, _b = daa._collect_events(p, CUTOFF)
    assert arms[0]["refused"] is False


def test_mixed_arms_marked_independently(tmp_path):
    # One refused + one healthy arm in the same transcript -> marked independently.
    p = _write(tmp_path, "t.jsonl", [
        _arm_use("2026-09-24T03:00:00Z", "m1"),
        _result("2026-09-24T03:00:01Z", "m1", REFUSAL_MSG, is_error=True),
        _arm_use("2026-09-24T03:05:00Z", "m2", with_skill=True),
        _result("2026-09-24T03:05:01Z", "m2", SUCCESS_MSG),
    ])
    arms, _a, _b = daa._collect_events(p, CUTOFF)
    by_uid = {a["tool_use_id"]: a["refused"] for a in arms}
    assert by_uid == {"m1": True, "m2": False}


# ── _verdict: REFUSED dominance ────────────────────────────────────────────────

def test_verdict_refused_when_any_refused():
    assert daa._verdict(flagged=True, total=0, orphan=0, reentries=0, refused=1) == "REFUSED"


def test_verdict_refused_dominates_healthy_arms():
    # A co-occurring healthy arm does not undo the window in which no net existed.
    assert daa._verdict(flagged=True, total=5, orphan=0, reentries=5, refused=1) == "REFUSED"


def test_verdict_refused_dominates_orphan():
    assert daa._verdict(flagged=True, total=2, orphan=2, reentries=0, refused=1) == "REFUSED"


def test_verdict_armed_ok_when_none_refused():
    assert daa._verdict(flagged=True, total=3, orphan=0, reentries=3, refused=0) == "ARMED-OK"


def test_verdict_off_ignores_refused():
    # A non-flagged agent stays off even with a stray refused count.
    assert daa._verdict(flagged=False, total=0, orphan=0, reentries=0, refused=2) == "off"


# ── _build_report: end-to-end verdict + noncompliant set ───────────────────────

def test_build_report_flags_refused_agent(tmp_path):
    agent, sid = "testagent", "sid-refused-001"
    root = _mk_project(tmp_path, agent, sid)
    tdir = tmp_path / "transcripts"
    tdir.mkdir()
    _write(tdir, f"{sid}.jsonl", [
        _arm_use(_recent(5), "r1"),
        _result(_recent(4), "r1", REFUSAL_MSG, is_error=True),
    ])
    rep = daa._build_report(tdir, root, since_hours=24)
    b = rep["per_agent"][agent]
    assert b["verdict"] == "REFUSED"
    assert b["refused"] == 1
    assert b["arms_total"] == 0          # a refused arm is NOT a real arm
    assert agent in rep["totals"]["noncompliant_agents"]


def test_build_report_healthy_agent_armed_ok(tmp_path):
    # NEGATIVE CONTROL end-to-end: a batched healthy arm -> ARMED-OK, not flagged.
    agent, sid = "testagent", "sid-healthy-001"
    root = _mk_project(tmp_path, agent, sid)
    tdir = tmp_path / "transcripts"
    tdir.mkdir()
    _write(tdir, f"{sid}.jsonl", [
        _arm_use(_recent(5), "h1", with_skill=True),
        _result(_recent(4), "h1", SUCCESS_MSG),
    ])
    rep = daa._build_report(tdir, root, since_hours=24)
    b = rep["per_agent"][agent]
    assert b["verdict"] == "ARMED-OK"
    assert b["refused"] == 0
    assert b["arms_total"] == 1
    assert b["batched"] == 1
    assert agent not in rep["totals"]["noncompliant_agents"]


def test_build_report_refused_dominates_with_healthy_arm(tmp_path):
    # A window carrying BOTH a healthy batched arm and a refused arm: refused is
    # counted (=1), the healthy arm still classifies (arms_total=1, batched=1),
    # and the verdict is REFUSED (dominance) with the agent flagged noncompliant.
    agent, sid = "testagent", "sid-mixed-001"
    root = _mk_project(tmp_path, agent, sid)
    tdir = tmp_path / "transcripts"
    tdir.mkdir()
    _write(tdir, f"{sid}.jsonl", [
        _arm_use(_recent(6), "x1", with_skill=True),
        _result(_recent(6), "x1", SUCCESS_MSG),
        _arm_use(_recent(4), "x2"),
        _result(_recent(4), "x2", REFUSAL_MSG, is_error=True),
    ])
    rep = daa._build_report(tdir, root, since_hours=24)
    b = rep["per_agent"][agent]
    assert b["verdict"] == "REFUSED"
    assert b["refused"] == 1
    assert b["arms_total"] == 1
    assert b["batched"] == 1
    assert agent in rep["totals"]["noncompliant_agents"]


# ── rearm: the sanctioned re-arm-first call after a resume ─────────────────────
# A compaction resume and a wakeup firing both OWE a lone re-arm as their first
# tool call (schedule-wakeup-correctness.md § Re-arm FIRST). No Skill follows it
# until that iteration closes, so before `rearm` existed every such arm read as
# an orphan (zeta 2026-09-25: 2 of 3 arms). Each case below has its control.

def _boundary(ts, subtype="compact_boundary"):
    return json.dumps({"timestamp": ts, "type": "system", "subtype": subtype})


def _other_use(ts, uid, name="Bash"):
    return json.dumps({"timestamp": ts,
                       "message": {"id": "msg-" + uid, "role": "assistant",
                                   "content": [{"type": "tool_use", "name": name,
                                                "id": uid, "input": {}}]}})


def _summary(ts):
    # The measured shape: the compaction summary is stamped BEFORE its own
    # boundary but written AFTER it, and carries no tool_use.
    return json.dumps({"timestamp": ts, "type": "user", "isCompactSummary": True,
                       "message": {"role": "user", "content": [{"type": "text", "text": "s"}]}})


def test_first_arm_after_a_compaction_is_marked(tmp_path):
    # POSITIVE CONTROL, in the live transcript's own order and stamps.
    p = _write(tmp_path, "t.jsonl", [
        _boundary("2026-09-25T15:36:18Z"),
        _summary("2026-09-25T15:36:15Z"),
        _arm_use("2026-09-25T15:36:26Z", "b1"),
        _result("2026-09-25T15:36:26Z", "b1", SUCCESS_MSG),
    ])
    arms, _a, _b = daa._collect_events(p, CUTOFF)
    assert [a["after_boundary"] for a in arms] == [True]


def test_an_arm_after_some_other_call_is_not_marked(tmp_path):
    # NEGATIVE CONTROL: the boundary is consumed by the FIRST tool call, whatever
    # it is. An arm that comes later is not the sanctioned re-arm.
    p = _write(tmp_path, "t.jsonl", [
        _boundary("2026-09-25T15:36:18Z"),
        _other_use("2026-09-25T15:36:20Z", "o1"),
        _arm_use("2026-09-25T15:36:26Z", "b2"),
    ])
    arms, _a, _b = daa._collect_events(p, CUTOFF)
    assert [a["after_boundary"] for a in arms] == [False]


def test_a_wakeup_firing_is_a_boundary_too(tmp_path):
    p = _write(tmp_path, "t.jsonl", [
        _boundary("2026-09-16T01:48:35Z", subtype="scheduled_task_fire"),
        _arm_use("2026-09-16T01:48:40Z", "b3"),
    ])
    arms, _a, _b = daa._collect_events(p, CUTOFF)
    assert arms[0]["after_boundary"] is True


def test_other_system_subtypes_are_not_boundaries(tmp_path):
    p = _write(tmp_path, "t.jsonl", [
        _boundary("2026-09-25T15:00:00Z", subtype="turn_duration"),
        _arm_use("2026-09-25T15:00:05Z", "b4"),
    ])
    arms, _a, _b = daa._collect_events(p, CUTOFF)
    assert arms[0]["after_boundary"] is False


def _arm_ev(ts, mid, after_boundary):
    return {"ts_dt": datetime.fromisoformat(ts), "ts_str": ts, "msg_id": mid,
            "tool_use_id": mid, "after_boundary": after_boundary}


def test_an_unfollowed_rearm_is_rearm_and_an_unfollowed_arm_is_orphan():
    arms = [_arm_ev("2026-09-25T15:00:00+00:00", "m1", True),
            _arm_ev("2026-09-25T15:30:00+00:00", "m2", False)]
    got = [a["klass"] for a in daa._classify_arms(arms, set(), [])]
    assert got == ["rearm", "orphan"]


def test_a_rearm_the_loop_closes_in_window_is_followed():
    arms = [_arm_ev("2026-09-25T15:00:00+00:00", "m1", True)]
    skill = [(datetime.fromisoformat("2026-09-25T15:02:00+00:00"), "m9")]
    assert [a["klass"] for a in daa._classify_arms(arms, {"m9"}, skill)] == ["followed"]


def test_verdict_rearm_beside_a_close_arm_is_armed_ok():
    # The zeta shape: one batched close pair and two re-arms.
    assert daa._verdict(flagged=True, total=3, orphan=0, reentries=1,
                        refused=0, rearm=2) == "ARMED-OK"


def test_verdict_rearms_alone_never_read_as_armed_ok():
    # A re-arm is not evidence that CLOSES arm. Re-arms with no re-entry is a
    # loop resurrecting without progress: QUIET, never ARMED-OK.
    assert daa._verdict(flagged=True, total=2, orphan=0, reentries=0,
                        refused=0, rearm=2) == "QUIET"
    # Re-entries whose closes carried no arm are NOT-ARMING however many
    # re-arms sit beside them.
    assert daa._verdict(flagged=True, total=1, orphan=0, reentries=3,
                        refused=0, rearm=1) == "NOT-ARMING"


def _compacted_window(with_boundary):
    lines = [_arm_use(_recent(40), "c1", with_skill=True),
             _result(_recent(40), "c1", SUCCESS_MSG)]
    if with_boundary:
        lines.append(_boundary(_recent(10)))
    lines += [_arm_use(_recent(9), "c2"), _result(_recent(9), "c2", SUCCESS_MSG)]
    return lines


def test_build_report_a_compacted_agent_is_armed_ok(tmp_path):
    agent, sid = "testagent", "sid-rearm-001"
    root = _mk_project(tmp_path, agent, sid)
    tdir = tmp_path / "transcripts"
    tdir.mkdir()
    _write(tdir, f"{sid}.jsonl", _compacted_window(with_boundary=True))
    b = daa._build_report(tdir, root, since_hours=24)["per_agent"][agent]
    assert (b["verdict"], b["batched"], b["rearm"], b["orphan"]) == ("ARMED-OK", 1, 1, 0)


def test_build_report_the_same_arm_without_a_boundary_is_still_an_orphan(tmp_path):
    # CONTROL for the case above: the only difference is the boundary line.
    agent, sid = "testagent", "sid-rearm-002"
    root = _mk_project(tmp_path, agent, sid)
    tdir = tmp_path / "transcripts"
    tdir.mkdir()
    _write(tdir, f"{sid}.jsonl", _compacted_window(with_boundary=False))
    rep = daa._build_report(tdir, root, since_hours=24)
    b = rep["per_agent"][agent]
    assert (b["verdict"], b["rearm"], b["orphan"]) == ("ORPHANS", 0, 1)
    assert agent in rep["totals"]["noncompliant_agents"]
