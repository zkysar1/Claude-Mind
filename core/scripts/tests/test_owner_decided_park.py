"""Tests for the owner-decided park exemption ().

WHAT THIS PROTECTS
------------------
The 72h escalation digest gained a second population leg in g-115-9894: goals
that wait on a human through a `human_blocked:` defer rather than through
`user` in participants. That leg is correct and necessary — 14 of 23 such goals
could not reach the digest before it. But part of the fleet relied on that
invisibility ON PURPOSE: `participants:['agent']` plus a `human_blocked:` defer
is how an agent parks work on a decision the owner has ALREADY MADE, without
re-asking him.

Measured 2026-09-15 (bravo, cc-05): 4 of the 9 members the second leg admitted
were owner-decided parks, one carrying the owner's verbatim "Do not re-ask,
re-file or re-email" (board msg-20260902-231119-alpha-599). Re-derived on the
live queue 2026-09-16 (echo, cc-03): 10 members, the same 4 parks.

The leg-1 park skip could not reach them: it reads
`shape == "user-only" and deliberate`, and the second leg appends every member
with `deliberate: False` HARDCODED. So the exemption is a new branch, keyed on
a DECLARED marker inside the defer.

WHY A DECLARED MARKER AND NOT A PROSE MATCH — the property most likely to be
"simplified" later, and the one that fails silently when it is:
guard-4015 measured that an exemption harvested by scraping free text inherits
the SCRAPER's precision, not the author's intent, and that it fails as a silent
MISS which DISABLES the protection. `test_prose_do_not_re_ask_is_still_emailed`
is the pin: a defer that merely SAYS "do not re-ask" MUST still be emailed.
Loosening `OWNER_DECIDED_RE` to a phrase match reds it.

WHY THE PREDICATE TESTS THE GOAL AND NOT THE CANDIDATE:
bravo's fresh-eyes pass (msg-20260915-092345-bravo-7135) showed the leg-1 park
skip is only reachable when leg 1 SUCCEEDS — an exception anywhere in
`_find_user_participant_goals` loses that source's whole first leg, and the
second leg then re-admits the same goals as `human-blocked-defer` /
`deliberate: False`, bypassing it. `test_park_survives_a_leg_one_scan_exception`
is that case, and it is why the branch reads `goal` rather than `cand`.

MUTATION PROOF (run 2026-09-16, echo, cc-03 — recorded so a future reader does
not have to take "the test fails on pre-fix code" on trust):
  - delete the owner-decided branch in user-blocker-escalation-check.py
      -> test_marked_park_is_reported_not_emailed RED
         test_park_survives_a_leg_one_scan_exception RED
  - widen OWNER_DECIDED_RE to a bare `owner-decided` phrase match
      -> test_prose_do_not_re_ask_is_still_emailed RED
  - drop the record-id shape from the ref group
      -> test_marker_with_a_non_record_ref_is_still_emailed RED
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SCRIPT_DIR / "user-blocker-escalation-check.py"

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from gates.owner_decided_park import (  # noqa: E402
    is_owner_decided_park,
    owner_decided_ref,
)

REAL_REF = "msg-20260902-231119-alpha-599"


def _load_module():
    spec = importlib.util.spec_from_file_location("ube_odp_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _goal(gid, age_hours, defer=None, participants=("agent",), status="pending",
          origin_signal=None):
    """A goal `age_hours` old; `defer` becomes its defer_reason verbatim."""
    import datetime as dt
    ts = (dt.datetime.now() - dt.timedelta(hours=age_hours)).strftime(
        "%Y-%m-%dT%H:%M:%S")
    g = {
        "id": gid,
        "title": "goal " + gid,
        "description": "d" * 50,
        "participants": list(participants),
        "status": status,
        "blocked_since": ts,
        "created_at": ts,
        "priority": "HIGH",
    }
    if defer is not None:
        g["defer_reason"] = defer
    if origin_signal:
        g["origin_signal"] = origin_signal
    return g


def _run(tmp_path, goals, *extra):
    """Run the real script over a seeded queue, email+board stubbed.

    `--board-escalation-log` is always passed with an empty log, so "no prior
    digest" => DUE. Omitting it would send the script to the LIVE coordination
    board and make every verdict depend on whether a real digest happened to
    have been sent on this box in the last 72 hours (the harness note in
    test_user_blocker_escalation_check.py).
    """
    wq = tmp_path / "world-aspirations.jsonl"
    aq = tmp_path / "agent-aspirations.jsonl"
    wq.write_text(json.dumps(
        {"id": "asp-999", "status": "active", "title": "t", "goals": goals}
    ) + "\n", encoding="utf-8")
    aq.write_text("", encoding="utf-8")

    blog = tmp_path / "board.json"
    blog.write_text("[]", encoding="utf-8")

    args = [sys.executable, str(SCRIPT),
            "--agent", "testagent",
            "--world-aspirations", str(wq),
            "--agent-aspirations", str(aq),
            "--board-escalation-log", str(blog),
            "--no-email", "--no-board"] + list(extra)
    proc = subprocess.run(args, capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _by_id(out, gid):
    for r in out["results"]:
        if r.get("goal_id") == gid:
            return r
    raise AssertionError("%s missing from results: %s" % (gid, out["results"]))


# ═══ the predicate itself ═══════════════════════════════════════════════════


def test_predicate_accepts_the_declared_marker():
    g = _goal("g-p", 10, defer="human_blocked: prod key postponed by the owner. "
                                "[owner-decided: %s]" % REAL_REF)
    assert owner_decided_ref(g) == REAL_REF
    assert is_owner_decided_park(g) is True


def test_predicate_is_case_insensitive_on_the_keyword_not_on_the_shape():
    # Casing drifts across LLM rewrites — defer_classifier.py gives the same
    # reasoning for its own lowercase compare.
    g = _goal("g-p", 10, defer="human_blocked: x [Owner-Decided: %s]" % REAL_REF)
    assert owner_decided_ref(g) == REAL_REF

    # The other half of this test's name, and the half a global re.IGNORECASE
    # silently dropped: the SHAPE stays case-exact. Record ids in this framework
    # are lowercase, so an uppercase ref names no openable record — and unlike
    # every other rejection here, admitting one SUPPRESSES an owner-facing line.
    # Pinned on the shape, not on one malformed token.
    for bad in ("G-363-81", "MSG-20260902-231119-ALPHA-599", "ASP-314", "PQ-007"):
        g = _goal("g-p", 10, defer="human_blocked: x [owner-decided: %s]" % bad)
        assert owner_decided_ref(g) is None, bad
        assert is_owner_decided_park(g) is False, bad


def test_predicate_rejects_a_non_human_blocked_defer():
    """Scope is deliberately narrow — the marker cannot suppress anything else."""
    g = _goal("g-p", 10, defer="precondition_unmet: waiting [owner-decided: %s]" % REAL_REF)
    assert owner_decided_ref(g) is None


def test_predicate_never_raises_on_junk():
    """It runs inside two fail-open sweeps; an exception costs a population leg."""
    for junk in (None, "a string", [], 42, {}, {"defer_reason": None},
                 {"defer_reason": 7}):
        assert owner_decided_ref(junk) is None


# ═══ the digest behaviour — the three outcomes the goal names ═══════════════


def test_marked_park_is_reported_not_emailed(tmp_path):
    """OUTCOME 1: a human_blocked-only goal with the marker is skipped + counted."""
    out = _run(tmp_path, [
        _goal("g-park", 400, defer="human_blocked: prod API key value is postponed "
                                   "BY OWNER DECISION. [owner-decided: %s]" % REAL_REF),
    ])
    row = _by_id(out, "g-park")
    assert row["action"] == "skip", row
    assert row["reason"] == "owner_decided_park", row
    assert row["owner_decision_ref"] == REAL_REF, row
    assert out["skipped"]["owner_decided"] == 1, out["skipped"]
    assert out["eligible"] == 0, "a park must not be eligible for the email batch"
    # guard-3752: the skip must stay VISIBLE — a reported, not-sent item was not
    # told to the owner, so the decision record has to be readable from the
    # sweep's own output.
    assert out["owner_decided_parks"] == [{"goal_id": "g-park", "ref": REAL_REF}]


def test_unmarked_human_blocked_goal_is_still_emailed(tmp_path):
    """OUTCOME 2a: no marker => the second leg still escalates it."""
    out = _run(tmp_path, [
        _goal("g-ask", 400, defer="human_blocked: the owner must physically "
                                  "install the RAM; no agent capability reaches it."),
    ])
    row = _by_id(out, "g-ask")
    assert row["action"] == "would_escalate", row
    assert out["skipped"]["owner_decided"] == 0
    assert out["eligible"] == 1


def test_prose_do_not_re_ask_is_still_emailed(tmp_path):
    """OUTCOME 2b — THE PIN AGAINST guard-4015.

    A defer that merely SAYS "do not re-ask" in prose is NOT exempt. Widening
    the predicate to a phrase match reds this test. The failure direction is
    what makes it worth a dedicated case: a prose-matching exempter fails as a
    silent MISS that disables the protection, so nothing else would notice.
    """
    out = _run(tmp_path, [
        _goal("g-prose", 400,
              defer="human_blocked: the owner decided this already and said "
                    "do not re-ask, re-file or re-email. Stays parked."),
    ])
    row = _by_id(out, "g-prose")
    assert row["action"] == "would_escalate", \
        "a prose 'do not re-ask' must NOT grant the exemption (guard-4015)"
    assert out["skipped"]["owner_decided"] == 0


def test_marker_with_a_non_record_ref_is_still_emailed(tmp_path):
    """The ref must name a record someone can open — shape validation.

    guard-4015's RULE clause: validate the SHAPE of each pattern before
    honoring it. `[owner-decided: yes]` names no record, so it is not a
    citation however it got there.
    """
    out = _run(tmp_path, [
        _goal("g-bogus", 400, defer="human_blocked: x [owner-decided: yes]"),
        _goal("g-bare", 400, defer="human_blocked: x owner-decided: %s" % REAL_REF),
    ])
    assert _by_id(out, "g-bogus")["action"] == "would_escalate"
    assert _by_id(out, "g-bare")["action"] == "would_escalate", \
        "the brackets are part of the declared marker, not decoration"
    assert out["skipped"]["owner_decided"] == 0


def test_parks_and_asks_are_separated_in_one_sweep(tmp_path):
    """The live shape: both families present, only the parks suppressed."""
    out = _run(tmp_path, [
        _goal("g-park-1", 1000, defer="human_blocked: a [owner-decided: %s]" % REAL_REF),
        _goal("g-park-2", 900, defer="human_blocked: b [owner-decided: g-326-730]"),
        _goal("g-ask-1", 800, defer="human_blocked: physical power-on required"),
        _goal("g-ask-2", 700, defer="human_blocked: owner go/no-go on the live lane"),
    ])
    assert out["skipped"]["owner_decided"] == 2, out["skipped"]
    assert out["eligible"] == 2, out
    assert {p["goal_id"] for p in out["owner_decided_parks"]} == {"g-park-1", "g-park-2"}


# ═══ the degraded case bravo's fresh-eyes pass found ════════════════════════


def test_park_survives_a_leg_one_scan_exception(tmp_path):
    """FRESH-EYES CONSTRAINT 1 (msg-20260915-092345-bravo-7135).

    When `_find_user_participant_goals` raises, that source loses its ENTIRE
    first leg, and the second leg re-admits every user-carrying goal from the
    same file as `shape: human-blocked-defer` / `deliberate: False` — which
    bypasses the leg-1 park skip at the `shape == "user-only" and deliberate`
    branch. A candidate-shaped predicate cannot hold here; a goal-shaped one
    can, and that is why the owner-decided branch reads `goal`.

    Pinned with a `participants: ['user']` park, which is the live victim shape
    (g-306-382 carries exactly that plus a live human_blocked defer).
    """
    mod = _load_module()

    def exploding_predicate(label, path):
        raise RuntimeError("simulated leg-1 scan failure")

    mod._load_population_predicate = lambda: exploding_predicate
    mod._read_last_digest_age = lambda *a, **k: (True, None)

    sent = []
    mod._send_digest_email = lambda agent, batch, cadence, no_email, stalled=None: (
        sent.append([g.get("id") for _c, g, _a, _f in batch]), (True, "sent"))[1]
    mod._post_digest_board_record = lambda batch, cadence, no_board: (True, "ok")

    wq = tmp_path / "w.jsonl"
    wq.write_text(json.dumps({
        "id": "asp-999", "status": "active", "title": "t",
        "goals": [
            _goal("g-park", 400, participants=("user",),
                  origin_signal="user_directive",
                  defer="human_blocked: parked [owner-decided: %s]" % REAL_REF),
            _goal("g-ask", 300, participants=("user",),
                  origin_signal="user_directive",
                  defer="human_blocked: still genuinely needs you"),
        ],
    }) + "\n", encoding="utf-8")

    sys.argv = ["x", "--apply", "--agent", "t",
                "--world-aspirations", str(wq), "--no-board"]
    mod.main()

    assert sent, "the digest should still send for the genuine ask"
    assert sent[0] == ["g-ask"], (
        "with leg 1 broken, the park must STILL be suppressed — it was admitted "
        "through the second leg with deliberate=False, so only a goal-shaped "
        "predicate can catch it. Got: %r" % (sent[0],))


# ═══ one predicate, two consumers ══════════════════════════════════════════


def test_both_consumers_import_the_same_predicate():
    """guard-4015 corollary / guard-2275: never two correct-looking copies.

    Asserted on the SOURCE rather than by behaviour because the defect this
    pins is divergence over time, and a behavioural assertion can only see the
    day it runs on. Both files must name the shared module.
    """
    for name in ("user-blocker-escalation-check.py", "completion_digest.py"):
        src = (SCRIPT_DIR / name).read_text(encoding="utf-8")
        assert "gates.owner_decided_park" in src, \
            "%s must import the shared predicate, not re-derive it" % name
        assert "owner_decided_ref" in src, name


def test_both_consumers_announce_a_dead_predicate():
    """The guarded import must DEGRADE LOUDLY in both consumers, not just one.

    A fail-open exemption that dies in silence renders every park as an ordinary
    ask — the pre-fix behaviour restored on an owner-facing surface, with no way
    for the reader to tell that from "the owner has no parked goals". That is
    catalogue Entry 12's shape (a report with no vocabulary for what it could
    not evaluate), reproduced inside the fix written to honour Entry 13.

    Source-asserted for the same reason as the sibling above: the defect is
    divergence between two copies OVER TIME, which a one-day behavioural
    assertion cannot see. Measured 2026-09-16: the escalation check had all
    three tokens and the digest had none.
    """
    for name in ("user-blocker-escalation-check.py", "completion_digest.py"):
        src = (SCRIPT_DIR / name).read_text(encoding="utf-8")
        assert "could not import gates.owner_decided_park" in src, \
            "%s must WARN on the degraded import, not swallow it" % name
        assert "_OWNER_DECIDED_LOADED = False" in src, \
            "%s must record that the predicate is dead" % name
        assert "owner_decided_predicate_loaded" in src, \
            "%s must publish the flag in its structured output" % name
