"""Tests for user-blocker-escalation-check.py ().

The script emails the USER about non-terminal goals carrying `user` in
participants that have aged past a threshold. It is the delivery-channel sibling
of the three board-posting escalators, which are all agent-to-agent and so
structurally cannot discharge a block whose condition is a human action.

What these tests pin, and why each one earns its place:

  - ONE DIGEST, NOT N EMAILS. The first live dry-run returned 14 eligible goals;
    a per-goal send would have delivered 14 emails in one sweep. This is the
    design property most likely to be "simplified" back into a loop later.
  - DELIBERATE ROUTING IS REPORTED, NEVER EMAILED. Nagging the user about a
    choice they made on purpose is the wrong correction.
  - NO COOLDOWN ON FAILED DELIVERY. Recording a cooldown for an email that never
    sent would suppress the retry and reproduce the exact silence being fixed.
  - THE POPULATION PREDICATE IS IMPORTED, NOT RE-DERIVED. A second copy of the
    predicate is how guard-1802's narrow-predicate hole appeared originally.
  - FAIL-OPEN. Delivery is additive; a broken layer must mean fewer emails, not
    an aborted precheck.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SCRIPT_DIR / "user-blocker-escalation-check.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("ube_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    spec.loader.exec_module(mod)
    return mod


def _goal(gid, age_hours, participants=("agent", "user"), status="blocked",
          origin_signal=None, title=None, priority="HIGH"):
    """Goal whose blocked_since is `age_hours` in the past."""
    import datetime as dt
    ts = (dt.datetime.now() - dt.timedelta(hours=age_hours)).strftime(
        "%Y-%m-%dT%H:%M:%S")
    g = {
        "id": gid,
        "title": title or ("goal " + gid),
        "description": "d" * 50,
        "participants": list(participants),
        "status": status,
        "blocked_since": ts,
        "priority": priority,
    }
    if origin_signal:
        g["origin_signal"] = origin_signal
    return g


def _write_queue(path: Path, goals):
    rec = {"id": "asp-999", "status": "active", "title": "t", "goals": goals}
    path.write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _run(tmp_path, goals, *extra, board_posts=None):
    """Run the script over a seeded queue with email+board stubbed."""
    wq = tmp_path / "world-aspirations.jsonl"
    aq = tmp_path / "agent-aspirations.jsonl"
    _write_queue(wq, goals)
    aq.write_text("", encoding="utf-8")

    args = [sys.executable, str(SCRIPT),
            "--agent", "testagent",
            "--world-aspirations", str(wq),
            "--agent-aspirations", str(aq),
            "--no-email", "--no-board"]
    if board_posts is not None:
        blog = tmp_path / "board.json"
        blog.write_text(json.dumps(board_posts), encoding="utf-8")
        args += ["--board-escalation-log", str(blog)]
    args += list(extra)

    proc = subprocess.run(args, capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# ═══ the core design property: one digest, not N emails ═══


def test_many_eligible_goals_produce_exactly_one_email(tmp_path):
    """N eligible goals -> ONE _send_digest_email call, N cooldown records.

    This is the property the live 14-goal dry-run made necessary. Asserted by
    counting calls to the digest sender directly, because the JSON output alone
    cannot distinguish one digest from fourteen sends.
    """
    mod = _load_module()
    calls = []

    def fake_send(agent, batch, escalate_hours, no_email):
        calls.append(len(batch))
        return True, "sent"

    posted = []
    mod._send_digest_email = fake_send
    mod._post_board = lambda goal, age, no_board: (posted.append(goal["id"]), (True, "ok"))[1]
    mod._read_recent_escalations = lambda *a, **k: set()
    mod._load_population_predicate = lambda: (lambda label, path: [
        {"goal": _goal("g-1", 100), "aspiration_id": "asp-999", "shape": "agent-user",
         "deliberate": False},
        {"goal": _goal("g-2", 90), "aspiration_id": "asp-999", "shape": "agent-user",
         "deliberate": False},
        {"goal": _goal("g-3", 80), "aspiration_id": "asp-999", "shape": "agent-user",
         "deliberate": False},
    ] if label == "world" else [])

    sys.argv = ["x", "--apply", "--agent", "t",
                "--world-aspirations", str(tmp_path / "w.jsonl"),
                "--no-board"]
    (tmp_path / "w.jsonl").write_text("", encoding="utf-8")
    mod.main()

    assert len(calls) == 1, "expected exactly ONE digest send, got %d" % len(calls)
    assert calls[0] == 3, "digest should carry all 3 goals, carried %d" % calls[0]
    assert sorted(posted) == ["g-1", "g-2", "g-3"], \
        "cooldown must be recorded PER GOAL even though delivery was one email"


def test_digest_body_lists_every_goal_oldest_first(tmp_path):
    mod = _load_module()
    batch = [
        ({"aspiration_id": "asp-1"}, _goal("g-young", 50), 50.0, "blocked_since"),
        ({"aspiration_id": "asp-1"}, _goal("g-oldest", 200), 200.0, "blocked_since"),
        ({"aspiration_id": "asp-1"}, _goal("g-mid", 100), 100.0, "blocked_since"),
    ]
    body = mod._compose_digest_body(batch, 48.0)
    for gid in ("g-young", "g-oldest", "g-mid"):
        assert gid in body, "%s missing from digest" % gid
    assert body.index("g-oldest") < body.index("g-mid") < body.index("g-young"), \
        "digest must be ordered oldest-first"
    assert "3 goal(s)" in body


# ═══ skip lanes ═══


def test_below_threshold_is_not_escalated(tmp_path):
    out = _run(tmp_path, [_goal("g-young", 10)], "--apply", "--escalate-hours", "48")
    assert out["eligible"] == 0
    assert out["skipped"]["below_threshold"] == 1
    assert out["applied"] == 0


def test_deliberate_user_routing_is_reported_not_escalated(tmp_path):
    """A user_directive goal is counted and labelled, never emailed.

    Tagged rather than dropped: a silent skip is indistinguishable from a clean
    sweep, which is the failure this lane exists to correct.
    """
    out = _run(tmp_path,
               [_goal("g-park", 500, origin_signal="user_directive")],
               "--apply")
    assert out["skipped"]["deliberate"] == 1
    assert out["applied"] == 0
    rec = [r for r in out["results"] if r["goal_id"] == "g-park"]
    assert rec and rec[0]["reason"] == "deliberate_user_routing", \
        "the skip must state its reason, not vanish"


def test_goal_without_user_participant_is_not_in_population(tmp_path):
    out = _run(tmp_path, [_goal("g-agentonly", 500, participants=("agent",))],
               "--apply")
    assert out["scanned"] == 0
    assert out["applied"] == 0


def test_terminal_goal_is_not_in_population(tmp_path):
    out = _run(tmp_path, [_goal("g-done", 500, status="completed")], "--apply")
    assert out["scanned"] == 0


def test_cooldown_from_board_suppresses_reescalation(tmp_path):
    """A prior board record inside the window excludes that goal."""
    import datetime as dt
    recent = (dt.datetime.now() - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    out = _run(tmp_path, [_goal("g-cool", 500)], "--apply",
               board_posts=[{"tags": ["user-blocker-escalated", "g-cool"],
                             "timestamp": recent}])
    assert out["skipped"]["cooldown"] == 1
    assert out["applied"] == 0


def test_expired_cooldown_does_not_suppress(tmp_path):
    """A board record OLDER than the window must not suppress — else one
    escalation would silence a goal permanently."""
    import datetime as dt
    old = (dt.datetime.now() - dt.timedelta(hours=200)).strftime("%Y-%m-%dT%H:%M:%S")
    out = _run(tmp_path, [_goal("g-stale", 500)], "--apply",
               "--escalate-hours", "48",
               board_posts=[{"tags": ["user-blocker-escalated", "g-stale"],
                             "timestamp": old}])
    assert out["skipped"]["cooldown"] == 0
    assert out["applied"] == 1


# ═══ failure posture ═══


def test_failed_delivery_records_no_cooldown(tmp_path):
    """The retry must survive a failed send.

    Recording a cooldown for an email that never sent would suppress the retry
    and reproduce exactly the silence this script exists to fix.
    """
    mod = _load_module()
    posted = []
    mod._send_digest_email = lambda *a, **k: (False, "smtp_down")
    mod._post_board = lambda goal, age, no_board: (posted.append(goal["id"]), (True, "ok"))[1]
    mod._read_recent_escalations = lambda *a, **k: set()
    mod._load_population_predicate = lambda: (lambda label, path: [
        {"goal": _goal("g-fail", 100), "aspiration_id": "a", "shape": "agent-user",
         "deliberate": False}] if label == "world" else [])

    (tmp_path / "w.jsonl").write_text("", encoding="utf-8")
    sys.argv = ["x", "--apply", "--agent", "t",
                "--world-aspirations", str(tmp_path / "w.jsonl")]
    mod.main()
    assert posted == [], "no board cooldown may be recorded when delivery failed"


def test_dry_run_sends_nothing(tmp_path):
    out = _run(tmp_path, [_goal("g-a", 500), _goal("g-b", 500)])
    assert out["dry_run"] is True
    assert out["eligible"] == 2
    assert out["applied"] == 0
    assert all(r["action"] in ("would_escalate", "skip") for r in out["results"])


def test_unavailable_predicate_fails_open_to_zero(tmp_path):
    """A broken predicate import means fewer emails, never a crash."""
    mod = _load_module()
    mod._load_population_predicate = lambda: None
    mod._read_recent_escalations = lambda *a, **k: set()
    sys.argv = ["x", "--agent", "t"]
    rc = mod.main()
    assert rc == 0


def test_population_predicate_is_imported_not_reimplemented():
    """The predicate must come from audit-user-to-agent.py.

    A second copy is precisely how guard-1802's narrow-predicate hole appeared;
    this pins that there is exactly one definition.
    """
    mod = _load_module()
    fn = mod._load_population_predicate()
    assert fn is not None, "predicate must load from audit-user-to-agent.py"
    assert fn.__name__ == "_find_user_participant_goals"
    src = SCRIPT.read_text(encoding="utf-8")
    assert "audit-user-to-agent.py" in src
    assert "def _find_user_participant_goals" not in src, \
        "predicate must be imported, never redefined here"


def test_always_exits_zero_and_emits_json(tmp_path):
    out = _run(tmp_path, [_goal("g-x", 500)])
    for key in ("scanned", "eligible", "applied", "skipped", "escalate_hours",
                "dry_run", "predicate_loaded"):
        assert key in out, "missing %s in JSON output" % key


def test_blocker_category_is_used_for_delivery():
    """Category MUST be `blocker`.

    notify-user Step 1.5's approval-request gate refuses sends that ask the user
    to do agent-capable work. This population asks the user to act BY
    CONSTRUCTION, so any other category would be refused and would silently
    recreate the original silence. `blocker` is exempt from that gate.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    assert '"--category", "blocker"' in src
    assert '"--error"' in src, "blocker => SendErrorAlert shape needs --error"
