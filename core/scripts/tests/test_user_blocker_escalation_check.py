"""Tests for user-blocker-escalation-check.py ().

The script emails the USER one digest of the non-terminal goals carrying `user`
in participants, on a FIXED CADENCE. It is the delivery-channel sibling of the
three board-posting escalators, which are all agent-to-agent and so structurally
cannot discharge a block whose condition is a human action.

What these tests pin, and why each one earns its place:

  - THE TRIGGER IS A SCHEDULE, NOT AN AGE CROSSING (D2, g-115-4963). Both
    mutation-verified 2026-08-08: re-adding an age predicate is caught by
    `test_within_cadence_sends_nothing_even_with_a_very_old_goal`, and it is the
    change most likely to be re-introduced, because raising a threshold LOOKS
    like implementing a cadence.
  - AN EMPTY LIST SENDS THE ALL-CLEAR (D3). The directive names this as the path
    that "silently regresses to a skip". Mutation-verified: re-adding `and batch`
    to the delivery condition is caught by
    `test_empty_list_sends_an_all_clear_not_a_noop`. It regresses invisibly — a
    skipped send and a genuinely quiet window produce the same empty inbox.
  - ONE DIGEST, NOT N EMAILS. The first live dry-run returned 14 eligible goals;
    a per-goal send would have delivered 14 emails in one sweep. This is the
    design property most likely to be "simplified" back into a loop later, and
    the user asked for the batch directly (D4: "I want more than one goal per
    email").
  - DELIBERATE ROUTING IS REPORTED, NEVER EMAILED. Nagging the user about a
    choice they made on purpose is the wrong correction.
  - NO SCHEDULE MARKER ON FAILED DELIVERY. Marking the clock for an email that
    never sent would suppress the retry for a full cadence.
  - THE POPULATION PREDICATE IS IMPORTED, NOT RE-DERIVED. A second copy of the
    predicate is how guard-1802's narrow-predicate hole appeared originally.
  - FAIL-OPEN EVERYWHERE EXCEPT THE SCHEDULE GATE, which fails CLOSED — see
    `test_unreadable_schedule_fails_closed` for why the inversion is deliberate.

HARNESS NOTE: `_run` always passes `--board-escalation-log`. The board is the
schedule source now, so omitting it would send the script to the LIVE
coordination board and make every verdict depend on whether a real digest
happened to have been sent on this box in the last 72 hours.
"""
import importlib.util
import json
import re
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


def _digest_post(hours_ago, tags=("user-digest-sent",)):
    """A prior-digest board record `hours_ago` in the past — the schedule marker."""
    import datetime as dt
    return {"tags": list(tags),
            "timestamp": (dt.datetime.now() - dt.timedelta(hours=hours_ago)
                          ).strftime("%Y-%m-%dT%H:%M:%S")}


def _run(tmp_path, goals, *extra, board_posts=None):
    """Run the script over a seeded queue with email+board stubbed.

    `--board-escalation-log` is passed ALWAYS, defaulting to an empty list.

    That default is load-bearing, not tidiness. The board is now the SCHEDULE
    source, so omitting the flag would send the script to `board-read.sh` and the
    LIVE coordination board — and every test's verdict would then depend on
    whether a real digest happened to have been sent on this box in the last 72
    hours. Under the predecessor's cooldown semantics a live read merely
    fail-opened to "no cooldown" and was harmless; under a schedule gate it
    decides `due`, so the same omission would make the suite nondeterministic and
    green-by-luck. An empty log means "no prior digest" => DUE, which is the
    right default for a test that is not about the schedule.
    """
    wq = tmp_path / "world-aspirations.jsonl"
    aq = tmp_path / "agent-aspirations.jsonl"
    _write_queue(wq, goals)
    aq.write_text("", encoding="utf-8")

    blog = tmp_path / "board.json"
    blog.write_text(json.dumps(board_posts if board_posts is not None else []),
                    encoding="utf-8")
    args = [sys.executable, str(SCRIPT),
            "--agent", "testagent",
            "--world-aspirations", str(wq),
            "--agent-aspirations", str(aq),
            "--board-escalation-log", str(blog),
            "--no-email", "--no-board"]
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

    def fake_send(agent, batch, cadence_hours, no_email):
        calls.append(len(batch))
        return True, "sent"

    posted = []
    mod._send_digest_email = fake_send
    mod._post_digest_board_record = lambda batch, cadence, no_board: (
        posted.append([g.get("id") for _c, g, _a, _f in batch]), (True, "ok"))[1]
    mod._read_last_digest_age = lambda *a, **k: (True, None)
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
    assert posted == [["g-1", "g-2", "g-3"]], \
        ("ONE board record must cover the whole digest — it is the schedule "
         "marker, and the schedule is a property of the digest, not of a goal")


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


def test_digest_survives_disproof_gate_when_a_quoted_description_has_a_marker(tmp_path):
    """A digest QUOTING a goal whose own text carries a universal marker must ship.

    g-115-4594: one aged goal's description used a universal adverb to describe
    its own finding. finding-disproof-gate scanned the outgoing body, read the
    QUOTATION as an assertion the agent was making, and refused the payload.
    Because delivery is ONE digest per batch and no cooldown is recorded on
    failure, that single description wedged the only agent-to-human escalation
    path for EVERY eligible goal, retrying identically every sweep.

    Pinned end-to-end against the REAL gate rather than asserting on the `> `
    character: a refactor could keep the prefix and still break the exemption,
    and the property that matters is "the digest ships", not "a marker is
    present" (guard-355 — assert the behaviour, not the token).
    """
    mod = _load_module()
    goal = _goal("g-marked", 100)
    goal["description"] = ("Investigate: this report keeps firing permanently "
                           "with no reachable remedy.")
    body = mod._compose_digest_body(
        [({"aspiration_id": "asp-1"}, goal, 100.0, "blocked_since")], 48.0)

    gate = SCRIPT_DIR / "finding-disproof-gate.py"
    run = subprocess.run([sys.executable, str(gate), "--claim", body,
                          "--fenced-quotes", "--json"],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, (
        "digest was REFUSED by finding-disproof-gate on QUOTED goal text — the "
        "escalation lane is wedged again (g-115-4594): %s" % run.stderr)

    # Opt-in control: the exemption must NOT be ambient. Without the flag the
    # same body is refused, which is what keeps a markdown blockquote in
    # agent-authored prose fully scanned on every other notify path.
    ambient = subprocess.run(
        [sys.executable, str(gate), "--claim", body, "--json"],
        capture_output=True, text=True, timeout=60)
    assert ambient.returncode == 1, (
        "quote-stripping is ambient rather than opt-in — every caller that did "
        "NOT ask for it just lost disproof coverage on any '> ' line")

    # Mutation control: the SAME body with the quote fence stripped MUST be
    # refused. Without this, the test would still pass if the gate stopped
    # gating at all, which is the more dangerous regression of the two.
    unfenced = "\n".join(ln.replace("> ", "", 1) if ln.lstrip().startswith(">") else ln
                         for ln in body.splitlines())
    mutated = subprocess.run(
        [sys.executable, str(gate), "--claim", unfenced, "--json"],
        capture_output=True, text=True, timeout=60)
    assert mutated.returncode == 1, (
        "gate no longer refuses an UNFENCED universal claim — the exemption has "
        "swallowed the gate itself, not just the quotation")


# ═══ skip lanes ═══


def test_young_goal_is_included_because_the_schedule_decides_not_the_age(tmp_path):
    """D2: age is no longer a membership filter.

    The predecessor skipped anything under `escalate_hours`. Under a fixed
    cadence there is no threshold to be under — the digest reports what is
    waiting on the user RIGHT NOW, and the clock alone decides when it goes.
    A goal filed two hours ago that genuinely needs the user belongs in the next
    digest; making it wait for an age bar would delay first contact by up to two
    full windows.
    """
    out = _run(tmp_path, [_goal("g-young", 2)], "--apply", "--cadence-hours", "72")
    assert out["eligible"] == 1
    assert out["applied"] == 1
    assert "below_threshold" not in out["skipped"], \
        "the age threshold must be gone, not merely set to zero"


def test_untimestamped_goal_is_included_with_age_reported_unknown(tmp_path):
    """A goal with NO parseable timestamp is DELIVERED, not dropped.

    g-115-4084: `if age is None or age < escalate_hours` folded an UNDEFINED age
    into the young bucket, so a goal that could never age into escalation read as
    merely too new. 16 of 796 open world goals carried no created_at when this
    was measured — 3 HIGH, one the unblocking goal for a live outage.

    The predecessor's fix NAMED the skip. Removing the threshold retires the hole
    outright: with nothing to compare against, a null age disqualifies nothing, so
    these goals reach the user. What must not come back is the null being
    rendered as a number — "waiting 0h" reads as brand-new and sorts the reader's
    attention away from the oldest problem in the queue.
    """
    g = _goal("g-notime", 500)
    del g["blocked_since"]          # no blocked_since / blocked_at / created_at
    out = _run(tmp_path, [g], "--apply", "--cadence-hours", "72")

    assert out["eligible"] == 1, "an undefined age must not exclude the goal"
    assert out["applied"] == 1
    assert out["unknown_age"] == 1, "the unknown age is still counted, not silent"

    rec = next(r for r in out["results"] if r["goal_id"] == "g-notime")
    assert rec["action"] == "escalated"
    assert rec["age_hours"] is None

    mod = _load_module()
    body = mod._compose_digest_body(
        [({"aspiration_id": "a"}, g, None, None)], 72.0)
    assert "age unknown" in body, "a null age must say so"
    assert "waiting 0h" not in body, \
        "a null age rendered as zero is the g-115-4084 fusion moved into the body"


def test_deliberate_user_routing_is_reported_not_escalated(tmp_path):
    """A TRUE PARK — participants:['user'] only, plus a deliberate signal — is
    counted and labelled, never emailed.

    Tagged rather than dropped: a silent skip is indistinguishable from a clean
    sweep, which is the failure this lane exists to correct.

    THIS TEST PINNED THE BUG UNTIL 2026-08-21 (g-115-6991). Its fixture was named
    `g-park` but used the `_goal` default `participants=("agent","user")`, so it
    asserted that a JOINT goal carrying origin_signal=user_directive is skipped —
    reproducing in the test the exact provenance-vs-routing confusion the
    production predicate had. A park is a ROUTING SHAPE; the name was the only
    thing park-like about it. The fixture now passes `participants=("user",)`,
    which is what the docstring always claimed it was testing.
    """
    out = _run(tmp_path,
               [_goal("g-park", 500, participants=("user",),
                      origin_signal="user_directive")],
               "--apply")
    assert out["skipped"]["deliberate"] == 1
    assert out["applied"] == 0
    rec = [r for r in out["results"] if r["goal_id"] == "g-park"]
    assert rec and rec[0]["reason"] == "deliberate_user_routing", \
        "the skip must state its reason, not vanish"


def test_joint_goal_with_user_directive_origin_is_eligible(tmp_path):
    """A ['agent','user'] goal is ELIGIBLE even when the user asked for it.

    The regression this file exists to hold shut (g-115-6991). The predicate
    tested `deliberate` — origin_signal provenance — alone, so every goal the
    principal personally requested was suppressed from the digest. The filter was
    positively correlated with importance: a goal carries
    origin_signal=user_directive precisely BECAUSE the user asked, so the
    strongest claim on their attention became the suppression criterion, and the
    digest reported itself clean the whole time. Measured at fix time on the live
    queue: 10 of 11 suppressed goals were joint, 5 of them HIGH.

    Nothing FAILS when a digest under-reports — that is why this needs a test and
    not just a fix. There is no error, no bounce, no red: the user simply never
    hears about their own request, and the silence is indistinguishable from
    having nothing to say.
    """
    out = _run(tmp_path,
               [_goal("g-joint", 500, participants=("agent", "user"),
                      origin_signal="user_directive")],
               "--apply")
    assert out["skipped"]["deliberate"] == 0, \
        "a joint goal is not a park; provenance must not suppress it"
    rec = [r for r in out["results"] if r["goal_id"] == "g-joint"]
    assert rec and rec[0]["action"] != "skip", \
        "the user's own directive must reach the digest, not be filtered by it"


def test_user_only_without_deliberate_signal_is_still_eligible(tmp_path):
    """`participants:['user']` alone is NOT a park — the deliberate half matters.

    Guards the other direction, and the reason the fix is an AND rather than the
    bare shape test. A user-only goal that nobody marked deliberate is ordinary
    work waiting on the user, which is precisely what this digest exists to
    surface; suppressing it would be a new instance of the same family the fix
    removes. audit-user-to-agent.py already treats this shape as ACTIONABLE
    (`shape == "user-only" and not deliberate` is its promote lane), so this
    keeps the two consumers of that tag reconciled rather than merely
    coincidentally agreeing on today's queue, where zero such goals exist.
    """
    out = _run(tmp_path,
               [_goal("g-useronly", 500, participants=("user",))],
               "--apply")
    assert out["skipped"]["deliberate"] == 0, \
        "shape alone must not suppress; a park needs the deliberate signal too"
    rec = [r for r in out["results"] if r["goal_id"] == "g-useronly"]
    assert rec and rec[0]["action"] != "skip"


def test_goal_without_user_participant_is_not_in_population(tmp_path):
    out = _run(tmp_path, [_goal("g-agentonly", 500, participants=("agent",))],
               "--apply")
    assert out["scanned"] == 0
    assert out["applied"] == 0


def test_terminal_goal_is_not_in_population(tmp_path):
    out = _run(tmp_path, [_goal("g-done", 500, status="completed")], "--apply")
    assert out["scanned"] == 0


# ═══ D2: the trigger is a fixed schedule, not an age crossing ═══


def test_within_cadence_sends_nothing_even_with_a_very_old_goal(tmp_path):
    """THE anti-regression for "a 72h-crossing threshold in slower clothing".

    The directive's VERIFY names this exact failure: raising the threshold from
    48h to 72h would look like the change and would keep the unpredictable ping,
    just slower. The discriminator is a goal FAR past the cadence arriving inside
    a fresh window — under a threshold it fires, under a schedule it waits.
    """
    out = _run(tmp_path, [_goal("g-ancient", 500)], "--apply",
               "--cadence-hours", "72", board_posts=[_digest_post(10)])
    assert out["schedule"]["due"] is False
    assert out["schedule"]["reason"] == "within_cadence"
    assert out["applied"] == 0
    assert out["delivery"]["attempted"] is False, \
        "a 500h-old goal must NOT trigger a send 10h into a 72h window"
    assert out["eligible"] == 1, \
        "it is still IN the population — it waits for the schedule, it is not dropped"
    assert out["schedule"]["hours_until_next"] == 62.0


def test_cadence_elapsed_sends(tmp_path):
    out = _run(tmp_path, [_goal("g-any", 5)], "--apply",
               "--cadence-hours", "72", board_posts=[_digest_post(80)])
    assert out["schedule"]["due"] is True
    assert out["schedule"]["reason"] == "cadence_elapsed"
    assert out["delivery"]["attempted"] is True
    assert out["applied"] == 1


def test_pre_cutover_per_goal_post_does_not_count_as_a_digest(tmp_path):
    """Board history holds the predecessor's per-GOAL records under BOARD_TAG.

    Keying the schedule on that tag would read one of those as "a digest was
    already sent" and suppress the first digest under the new cadence for a full
    window — silently, on the one send whose absence nobody could distinguish
    from a quiet queue. The schedule keys on DIGEST_TAG, which no pre-cutover
    post carries.
    """
    out = _run(tmp_path, [_goal("g-x", 500)], "--apply",
               board_posts=[_digest_post(1, tags=["user-blocker-escalated", "g-x"])])
    assert out["schedule"]["due"] is True
    assert out["schedule"]["reason"] == "no_prior_digest"
    assert out["applied"] == 1


def test_a_goal_still_waiting_reappears_in_the_next_digest(tmp_path):
    """No per-goal cooldown: the list is what is waiting NOW, every time.

    Under the predecessor a goal was suppressed for a window after being
    escalated. Kept alongside a schedule that fires on the same period, that
    would drain the list toward empty while the work was still blocked — and the
    D3 all-clear would then fire over real outstanding asks, turning a comfort
    signal into a false one. The user expects the opposite: "I presume there will
    be a set of these goals."
    """
    posts = [_digest_post(80, tags=["user-digest-sent", "user-blocker-escalated",
                                    "g-persistent"])]
    out = _run(tmp_path, [_goal("g-persistent", 500)], "--apply",
               "--cadence-hours", "72", board_posts=posts)
    assert out["applied"] == 1, \
        "appearing in the previous digest must not exclude a goal from this one"
    rec = next(r for r in out["results"] if r["goal_id"] == "g-persistent")
    assert rec["action"] == "escalated"


def test_schedule_verdict_covers_every_branch():
    """The trigger is a pure function — pin all four verdicts directly.

    The unreadable-clock branch cannot be reached through the test board-log
    seam (a missing file there means "no prior digest", deliberately), so it is
    tested here or nowhere.
    """
    mod = _load_module()
    assert mod._schedule_verdict(True, None, 72.0) == (True, "no_prior_digest")
    assert mod._schedule_verdict(True, 71.9, 72.0) == (False, "within_cadence")
    assert mod._schedule_verdict(True, 72.0, 72.0) == (True, "cadence_elapsed")
    assert mod._schedule_verdict(True, 500.0, 72.0) == (True, "cadence_elapsed")


def test_unreadable_schedule_fails_closed(tmp_path):
    """An unreadable clock must NOT send — the one inverted fail direction.

    Every other layer here fails OPEN because delivery is additive. A schedule
    gate cannot inherit that: this script runs from aspirations-precheck on every
    loop iteration, so "I could not read when I last sent" failing open to "send
    now" would mail the user on every iteration, all day. Failing closed costs
    one sweep and is announced on stderr.
    """
    mod = _load_module()
    assert mod._schedule_verdict(False, None, 72.0) == (False, "schedule_unreadable")
    assert mod._schedule_verdict(False, 500.0, 72.0) == (False, "schedule_unreadable"), \
        "an unreadable read must not send even when the stale age looks overdue"


# ═══ failure posture ═══


# ═══ D3: an empty list is a SEND, not a skip ═══


def test_empty_list_sends_an_all_clear_not_a_noop(tmp_path):
    """THE pin the directive asks for by name.

    "the empty-list path is the one that silently regresses to a skip" — and it
    regresses invisibly, because a skipped send and a genuinely quiet window
    produce the same empty inbox. Every instinct in a sweep script says "nothing
    to report, return early"; `if args.apply and due:` carries no `and batch`
    for exactly that reason.

    The user asked for this explicitly: "And yes, I do like this, it would give
    me comfort."
    """
    out = _run(tmp_path, [], "--apply", "--cadence-hours", "72",
               board_posts=[_digest_post(80)])

    assert out["eligible"] == 0
    assert out["all_clear"] is True
    assert out["delivery"]["attempted"] is True, \
        "an empty list must still SEND — this is the regression the directive names"
    assert out["delivery"]["shape"] == "all_clear"
    assert out["delivery"]["ok"] is True


def test_all_clear_is_not_sent_before_the_cadence_elapses(tmp_path):
    """The all-clear is on the schedule too — it is not "send whenever empty".

    Without this, D3 implemented alone would mail a reassurance on every precheck
    iteration. Two-way proof: the companion above shows it DOES send when due.
    """
    out = _run(tmp_path, [], "--apply", "--cadence-hours", "72",
               board_posts=[_digest_post(10)])
    assert out["all_clear"] is False
    assert out["delivery"]["attempted"] is False


def test_all_clear_body_is_short_and_reads_as_reassurance(tmp_path):
    mod = _load_module()
    body = mod._compose_all_clear_body(72.0)

    assert len(body) < 700, "the directive asks for the SHORT all-clear"
    assert len(body) > 20, "notify-build-payload refuses a body under 20 chars"
    assert "72" in body, "predictability is the stated value — date the next one"

    lowered = body.lower()
    # The all-clear must not read as a request. /notify-user Step 1.5 refuses
    # sends matching these; that gate does not execute on this script's direct
    # notify-build-payload path today, so this is a wording pin against a future
    # transport change turning the comfort email into a refused one.
    for pattern in ("please approve", "please run", "waiting for you to",
                    "user must", "user needs to", "user should",
                    "awaiting your approval", "blocked on user action"):
        assert pattern not in lowered, \
            "all-clear must not read as an approval request: %r" % pattern


def test_all_clear_and_digest_use_different_subjects_and_categories():
    """A reader must be able to tell the two apart in a subject line, and the
    all-clear must not arrive as an ERROR alert — that would deliver the comfort
    email as an alarm, the opposite of D3's purpose."""
    src = SCRIPT.read_text(encoding="utf-8")
    populated = re.search(r'category, fenced = "([a-z-]+)", True', src)
    assert populated, "could not find the populated-digest category assignment"
    assert 'category, fenced = "info", False' in src, \
        "the all-clear must not inherit the blocker/SendErrorAlert shape"
    # Assert the DISTINCTION this docstring is about, not a specific literal.
    # The populated category was pinned as `blocker` here until  moved
    # it to `user-digest`; because that literal sat in a region git auto-merged
    # WITHOUT a conflict, this line kept passing on one box and failing on the
    # other. A test that pins the current value of a decision, rather than the
    # property the decision has to satisfy, breaks every time the decision moves.
    assert populated.group(1) != "info", \
        "the two branches must use different categories, or a reader cannot " \
        "tell a to-do list from an all-clear"
    assert populated.group(1) != "blocker", \
        "`blocker` selects SendErrorAlert, the one shape with no pretty " \
        "renderer — that is the D1 raw-text defect (g-115-4962)"
    assert 'subject = "Nothing waiting on you"' in src


# ═══ failure posture ═══


def test_failed_delivery_records_no_schedule_marker(tmp_path):
    """The retry must survive a failed send.

    Sharper under a schedule than under the predecessor's cooldown: marking the
    schedule for an email that never sent would start the next window from a send
    that did not happen, suppressing the retry for a FULL CADENCE rather than
    just skipping one goal.
    """
    mod = _load_module()
    posted = []
    mod._send_digest_email = lambda *a, **k: (False, "smtp_down")
    mod._post_digest_board_record = lambda batch, cadence, no_board: (
        posted.append([g.get("id") for _c, g, _a, _f in batch]), (True, "ok"))[1]
    mod._read_last_digest_age = lambda *a, **k: (True, None)
    mod._load_population_predicate = lambda: (lambda label, path: [
        {"goal": _goal("g-fail", 100), "aspiration_id": "a", "shape": "agent-user",
         "deliberate": False}] if label == "world" else [])

    (tmp_path / "w.jsonl").write_text("", encoding="utf-8")
    sys.argv = ["x", "--apply", "--agent", "t",
                "--world-aspirations", str(tmp_path / "w.jsonl")]
    mod.main()
    assert posted == [], \
        "no schedule marker may be recorded when delivery failed"


def test_all_clear_records_a_schedule_marker(tmp_path):
    """The quiet case MUST write the marker.

    Skipping it there leaves the clock unset on every quiet sweep, so the next
    sweep reads "no prior digest", finds DUE, and sends again — the all-clear
    would fire on every precheck iteration instead of once a window. The
    companion above proves the marker is withheld on FAILURE, so this pair shows
    the marker tracks delivery rather than being unconditional.
    """
    mod = _load_module()
    posted = []
    mod._send_digest_email = lambda *a, **k: (True, "sent")
    mod._post_digest_board_record = lambda batch, cadence, no_board: (
        posted.append(len(batch)), (True, "ok"))[1]
    mod._read_last_digest_age = lambda *a, **k: (True, None)
    mod._load_population_predicate = lambda: (lambda label, path: [])

    (tmp_path / "w.jsonl").write_text("", encoding="utf-8")
    sys.argv = ["x", "--apply", "--agent", "t",
                "--world-aspirations", str(tmp_path / "w.jsonl")]
    mod.main()
    assert posted == [0], "the all-clear must record the schedule marker too"


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
    for key in ("scanned", "eligible", "applied", "skipped", "cadence_hours",
                "schedule", "delivery", "dry_run", "predicate_loaded"):
        assert key in out, "missing %s in JSON output" % key


def test_digest_category_is_gate_exempt_AND_pretty_rendered():
    """The populated digest's category must satisfy BOTH properties at once.

    This test is the merge of two independent corrections to the SAME decision,
    made on different boxes, and it is worth recording why neither alone was
    sufficient.

    The category was `blocker` because `blocker` is exempt from notify-user
    Step 1.5's approval-request gate, and this digest quotes arbitrary goal
    descriptions — one containing "user must" would refuse the entire send, and
    the caller records no cooldown on failure, so the lane wedges on every retry
    (g-115-4594).

      - g-115-4963 measured that the gate NEVER RUNS on this path: Step 1.5 is
        pseudocode in the SKILL.md, and this script invokes
        notify-build-payload.py and email-send.sh directly. The exemption was
        never binding.
      - g-115-4962 measured the cost that reasoning had missed: `blocker` is the
        ONE category emitting SendErrorAlert, which has no render_structured, so
        the user's routine to-do list arrived as a red "AyoAi Error Alert" box —
        user directive D1's "they come across as raw text".

    Finding 1 alone makes `blocker` look merely UNNECESSARY. Finding 2 is what
    makes it WRONG. So the pin is the INTERSECTION, and it READS both source
    files rather than restating their contents here — the defect was two
    locally-correct decisions in two files whose intersection nobody checked,
    and a test that quotes one file cannot catch that. Edit either file and this
    fails.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    m = re.search(r'category, fenced = "([a-z-]+)", True', src)
    assert m, "could not find the populated-digest category assignment"
    category = m.group(1)

    # (a) gate-exempt — read notify-user Step 1.5's ACTUAL exempt tuple, so this
    # stays correct if the path ever DOES route through the skill.
    skill = (SCRIPT_DIR.parent.parent / ".claude" / "skills" / "notify-user"
             / "SKILL.md").read_text(encoding="utf-8")
    gate = re.search(r'IF category not in \(([^)]*)\):', skill)
    assert gate, "could not find Step 1.5's exempt tuple in notify-user/SKILL.md"
    exempt = set(re.findall(r'"([a-z-]+)"', gate.group(1)))
    assert category in exempt, (
        "category %r is NOT exempt from notify-user Step 1.5; if this path ever "
        "routes through the skill, a quoted goal description would refuse the "
        "digest and wedge the lane. Exempt: %s" % (category, sorted(exempt)))

    # (b) pretty-rendered — anything EXCEPT the SendErrorAlert shape, which is
    # the only shape with no render_structured on the  side.
    builder = (SCRIPT_DIR / "notify-build-payload.py").read_text(encoding="utf-8")
    valid = re.search(r'VALID_CATEGORIES = \(([^)]*)\)', builder, re.S)
    assert valid, "could not find VALID_CATEGORIES in notify-build-payload.py"
    assert category in set(re.findall(r'"([a-z-]+)"', valid.group(1))), (
        "category %r is not a category the payload builder accepts" % category)
    assert category != "blocker", (
        "`blocker` emits the SendErrorAlert shape, which has NO pretty renderer "
        "— the digest would arrive as an error alert in raw text (D1)")

    # (c) the transport flag must TRACK the category, never be unconditional.
    # Neither branch uses `blocker` today, so --error never fires; the guard is
    # kept rather than deleted because the mismatch it prevents fails SILENTLY —
    # an info-shaped payload posted to the error endpoint still reports success.
    assert '(["--error"] if category == "blocker" else [])' in src, (
        "--error must be conditional on the category the payload was built with")


# ── Digest READABILITY pins () ──────────────────────────────────────
# The user replied to a live digest on 2026-08-03 saying it "caused anxiety",
# that they could not tell "what you need the user to do", and that the goals
# "seem to be cut off and not have all the information". Each test below pins
# the BEHAVIOUR that answers one of those, not the token that implements it
# (guard-355) — a rewrite may change the wording freely and must keep the
# property.


def test_needs_from_you_line_is_present_for_every_goal(tmp_path):
    """Every item states what the human is being asked for — including when
    nothing was recorded.

    `user_leg_scope` is the field that answers "what do you need from me" and it
    was rendered NOWHERE before this. Measured 2026-08-03: populated on 16 of 45
    live user-carrying goals. The ABSENT case matters as much as the present
    one: silently omitting the line turns "nobody recorded why you are on this"
    into "this email has nothing to say", which is indistinguishable from the
    pre-fix behaviour the user complained about.
    """
    mod = _load_module()
    scoped = _goal("g-scoped", 100)
    scoped["user_leg_scope"] = "credential-grant"
    unscoped = _goal("g-unscoped", 90)
    unscoped.pop("user_leg_scope", None)

    body = mod._compose_digest_body(
        [({"aspiration_id": "asp-1"}, scoped, 100.0, "blocked_since"),
         ({"aspiration_id": "asp-1"}, unscoped, 90.0, "blocked_since")], 48.0)

    assert "credential-grant" in body, (
        "a recorded user_leg_scope is the literal answer to 'what do you need "
        "from me' and must reach the reader")
    # Both goals must contribute a needs-from-you statement: count, don't just
    # check presence, or the unscoped goal can silently drop out.
    assert body.count("NEEDS FROM YOU") == 2, (
        "every item needs an explicit ask line — the unrecorded case must SAY "
        "it is unrecorded rather than omitting the line: %s" % body)


def test_ask_buried_past_the_old_clip_still_reaches_the_reader(tmp_path):
    """A description whose ask sits past the old 400-char clip must survive.

    Descriptions in this fleet routinely OPEN with diagnosis and reach the
    request later, so a 400-char head-clip landed in the background and cut
    before the ask — the reader got context and no request. Asserted on the ASK
    being reachable rather than on any particular budget, so raising or lowering
    the clip is free as long as this property holds.
    """
    mod = _load_module()
    goal = _goal("g-buried", 100)
    filler = "background " * 60          # ~660 chars, well past the old 400
    goal["description"] = filler + "ASK: please approve the DEV rotation."

    body = mod._compose_digest_body(
        [({"aspiration_id": "asp-1"}, goal, 100.0, "blocked_since")], 48.0)

    assert "ASK: please approve the DEV rotation." in body, (
        "the request was clipped away and the reader received only background — "
        "this is the exact defect reported in g-115-4815")


def test_truncation_reports_how_much_it_dropped(tmp_path):
    """When the digest DOES clip, it must say so quantitatively.

    A bare ellipsis leaves the reader unable to judge whether opening the full
    goal is worth it. The digest stays bounded — reproducing 14 full
    descriptions is not a digest — so the honest form is "clipped, and here is
    how much".
    """
    mod = _load_module()
    goal = _goal("g-huge", 100)
    goal["description"] = "x" * 5000

    body = mod._compose_digest_body(
        [({"aspiration_id": "asp-1"}, goal, 100.0, "blocked_since")], 48.0)

    assert "more characters" in body, (
        "a clipped description must report the dropped length, not just trail "
        "off: %s" % body[-400:])
    assert len(body) < 5000, "digest must still be bounded, not the full text"


def test_asks_come_before_the_framework_background(tmp_path):
    """Ordering pin: the reader's action precedes our archaeology.

    The pre-fix body opened with six lines explaining which internal goal id
    created this escalation lane, before a single actionable word. The user
    named this directly. Ordering is the property; the heading text is not.
    """
    mod = _load_module()
    body = mod._compose_digest_body(
        [({"aspiration_id": "asp-1"}, _goal("g-first", 100), 100.0,
          "blocked_since")], 48.0)

    assert "WHY YOU ARE HEARING ABOUT IT NOW" in body, \
        "the background must still be present — it was moved, not deleted"
    assert body.index("g-first") < body.index("WHY YOU ARE HEARING ABOUT IT NOW"), \
        "the goals must appear BEFORE the framework background"


def test_dispatcher_rc4_means_superseded_by_the_fleet_digest_not_failed(tmp_path, monkeypatch):
    """Since 2026-08-17 the daily FLEET DIGEST (agent-completion-report) lists
    this same population under `user-digest`, and the framework dispatcher
    dedups that category fleet-wide (rc 4). The user HAS been told, so the
    schedule is satisfied -- a 'failed' verdict here would re-fire every sweep."""
    mod = _load_module()
    import subprocess as sp
    import types

    class FakeBuilt:
        returncode = 0
        stdout = '{"InfoMessage":"x","InfoType":"Fleet Digest","XPayloadProvenance":"notify-build-payload/v1"}'
        stderr = ""

    def fake_run(argv, **kw):
        # first call: payload builder; second: email-send.sh -> dispatcher rc 4
        s = " ".join(str(a) for a in argv)
        if "notify-build-payload" in s:
            return FakeBuilt()
        return sp.CompletedProcess(argv, 4, "", "[notify-dispatch] DUPLICATE: digest already sent this window")
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    sender = tmp_path / "scripts"
    sender.mkdir()
    (sender / "email-send.sh").write_text("#!/usr/bin/env bash\nexit 4\n")
    monkeypatch.setenv("WORLD_PATH", str(tmp_path))
    monkeypatch.setenv("MIND_WORLD", str(tmp_path))
    g = _goal("g-1", 100)
    cand = {"goal": g, "aspiration_id": "asp-999", "shape": "agent-user", "deliberate": False}
    ok, why = mod._send_digest_email("t", [(cand, g, 100.0, "blocked_since")], 72.0, False)
    assert (ok, why) == (True, "superseded_by_fleet_digest"), (ok, why)
