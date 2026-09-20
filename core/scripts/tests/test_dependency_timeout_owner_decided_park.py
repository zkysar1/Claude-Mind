#!/usr/bin/env python3
""" — the dependency-timeout lane is the THIRD owner-notification
path, and it must consult the SAME owner-decided-park predicate the two owner
digests already import.

THE INCIDENT. Measured 2026-09-18 on the live queue: this lane escalated
g-350-213 (blocked on g-350-108 for 538h) with `route=notify_user`. g-350-108 is
a DECLARED owner-decided park — a `human_blocked:` defer carrying decision ref
msg-20260902-231119-alpha-599, whose ruling reads "Do not re-ask, re-file or
re-email". g-353-102 had already fixed this defect class with ONE shared
predicate, but its wiring was scoped to "both owner digests"
(`user-blocker-escalation-check.py`, `completion_digest.py`) and this lane is
neither, so it inherited none of it. The fleet was one transport call away from
re-asking a settled question. guard-4132: an already-fixed INSTANCE is not
evidence the CLASS is handled.

WHAT IS PINNED, one case per verification outcome the goal names:
  1. the lane consults `gates.owner_decided_park` — the SAME module object the
     digests import, asserted by identity, not by a behavioural look-alike;
  2. a declared park is reported with a SKIP action, an owner_decided reason and
     its decision ref, in BOTH dry-run and apply — and reaches no transport;
  4. non-vacuity is proved by MUTATION (`test_mutation_*`), not by assertion.
Outcome 3 (the enumeration of owner-facing paths) is a measurement recorded on
the goal, not a test.

THE DURABLE-RECORD REQUIREMENT (guard-6866, which this agent wrote from the
g-353-102 measurement). The predicate must be handed the goal RECORD out of
`_read_goal_index()`, never the candidate dict the loop assembles — an exemption
that reads a leg-built dict is defeated by any leg that hardcodes the field it
tests, which is precisely how 4 of 10 parks were emailed every cadence in the
sibling sweep. `test_predicate_receives_the_durable_root_record` asserts the
argument's identity, so folding the read onto `c` fails here loudly.

FAIL-OPEN DIRECTION. Every failure of the predicate — absent marker, malformed
ref, wrong defer prefix, an import that raised — must leave the escalation
FIRING. Suppression is the unrecoverable direction: a silently-swallowed
escalation is invisible, while an extra one merely annoys. Four cases below
drive that direction explicitly.

STUBBING SEAM, and what it EXCLUDES (guard-1462). `_read_blocked`,
`_read_goal_index`, `_post_board` and the escalation log are stubbed, so nothing
upstream is proved here: that the blocked view really reports g-350-213, that
the live g-350-108 record really carries the marker, and that the board scan
really yields the cooldown set belong to their own surfaces. What this file
proves is that the ROUTE DECISION consults the shared predicate and that a park
reaches no transport.

Run: py -3 -m pytest core/scripts/tests/test_dependency_timeout_owner_decided_park.py -v
"""
import datetime as dt
import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))


def _load():
    spec = importlib.util.spec_from_file_location(
        "dependency_timeout_owner_park_module",
        SCRIPT_DIR / "dependency-timeout-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load()

BLOCKED_SINCE = (dt.datetime.now() - dt.timedelta(hours=538)).isoformat()

# The live shape, copied from the incident rather than invented: a human_blocked
# defer carrying a board-message decision ref.
PARK_DEFER = ("human_blocked: prod API key — owner said 'i will do prod key "
              "later' [owner-decided: msg-20260902-231119-alpha-599]")
PARK_REF = "msg-20260902-231119-alpha-599"


class _Args:
    apply = True
    threshold_hours = 1.0
    agent = "echo"
    board_escalation_log = None
    no_board = False


class _DryArgs(_Args):
    apply = False


def _drive(defer_reason, args_cls=_Args, participants=None, root_status="blocked",
           predicate=None, seen=None):
    """Run the sweep over ONE aged dependency whose root carries `defer_reason`.

    Returns (result, board_posts).
    """
    posts = []
    saved = (M._read_blocked, M._read_goal_index, M._read_recent_escalations,
             M._load_threshold_hours, M._resolve_self_agent, M._post_board,
             M._clear_defer, M.owner_decided_ref)
    M._read_blocked = lambda: {"blocked_goals": [
        {"goal_id": "g-waiter", "block_reason": "dependency"}]}
    M._read_goal_index = lambda: {
        "g-waiter": {"id": "g-waiter", "title": "the waiter", "_source": "world",
                     "blocked_since": BLOCKED_SINCE, "blocked_by": ["g-root"]},
        "g-root": {"id": "g-root", "title": "the parked root", "_source": "world",
                   "status": root_status, "description": "root desc",
                   "participants": participants or ["agent"],
                   "defer_reason": defer_reason},
    }
    M._read_recent_escalations = lambda *a, **k: set()
    M._load_threshold_hours = lambda a: 1.0
    M._resolve_self_agent = lambda a: "echo"
    M._post_board = lambda gid, rid, age, detail, nb: (
        posts.append(detail) or (True, "posted"))
    M._clear_defer = lambda rid, src: (True, "cleared")
    if predicate is not None:
        M.owner_decided_ref = predicate
    elif seen is not None:
        real = M.owner_decided_ref
        M.owner_decided_ref = lambda g: (seen.append(g) or real(g))
    try:
        return M.run(args_cls()), posts
    finally:
        (M._read_blocked, M._read_goal_index, M._read_recent_escalations,
         M._load_threshold_hours, M._resolve_self_agent, M._post_board,
         M._clear_defer, M.owner_decided_ref) = saved


# ── Outcome 1: the SAME predicate, not a second copy ────────────────────────

def test_lane_imports_the_shared_predicate_module_itself():
    """Identity, not behaviour. A local re-implementation that happened to agree
    on today's inputs would pass every behavioural case in this file — and would
    BE the defect (two correct-looking copies of one exemption set, guard-2275),
    because the copies diverge later on matching semantics, not on inputs."""
    import gates.owner_decided_park as canonical
    assert M.owner_decided_ref is canonical.owner_decided_ref
    assert M._OWNER_DECIDED_LOADED is True


def test_predicate_receives_the_durable_root_record():
    """guard-6866: the branch must read the goal RECORD from the index, never
    the candidate dict the loop builds. Folding the read onto `c` fails here."""
    seen = []
    _drive(PARK_DEFER, seen=seen)
    assert seen, "the route decision never consulted the predicate at all"
    arg = seen[0]
    assert arg.get("id") == "g-root" and arg.get("defer_reason") == PARK_DEFER, \
        "predicate was handed %r, not the durable root record" % (arg,)
    assert "route" not in arg and "age_hours" not in arg, \
        "predicate was handed the candidate dict — the guard-6866 defect"


# ── Outcome 2: skip action, owner_decided reason, decision ref ──────────────

def test_declared_park_is_skipped_not_notified():
    res, posts = _drive(PARK_DEFER)
    assert res["needs_user_notification"] == [], \
        "a declared owner-decided park was routed to the owner"
    assert len(res["owner_decided_skipped"]) == 1
    rec = res["owner_decided_skipped"][0]
    assert rec["owner_decided_ref"] == PARK_REF, "decision ref missing from output"
    assert rec["root_id"] == "g-root"
    assert posts == [], "a suppressed park posted to the board anyway"


def test_dry_run_reports_the_skip_action_and_the_ref():
    """Outcome 2 literally: the DRY RUN is what a reader inspects before an
    apply, so the skip action and its reason must be visible there."""
    res, _ = _drive(PARK_DEFER, args_cls=_DryArgs)
    assert len(res["candidates"]) == 1
    c = res["candidates"][0]
    assert c["route"] == "skip_owner_decided"
    assert c["owner_decided_ref"] == PARK_REF


def test_suppression_burns_no_cooldown_slot():
    """No board post means no cooldown key, so the next sweep re-derives the
    same suppression instead of going silent for an unrelated reason. The
    sibling re-probe branch needs its post (a falsified verdict is news); this
    one must not have it — a `human_blocked:` root never auto-clears, so a post
    here would recur every window forever."""
    res, posts = _drive(PARK_DEFER)
    assert res["escalated"] == [], "a suppressed park was counted as escalated"
    assert posts == []


def test_predicate_loaded_flag_is_reported():
    res, _ = _drive(PARK_DEFER)
    assert res["owner_decided_predicate_loaded"] is True


# ── Fail-open: every predicate failure leaves the escalation FIRING ─────────

def test_plain_human_blocked_without_a_marker_still_notifies():
    """The control that gives every case above its meaning: suppression is
    caused by the declared MARKER, not by the `human_blocked:` class."""
    res, _ = _drive("human_blocked: waiting on a person to decide")
    assert len(res["needs_user_notification"]) == 1
    assert res["owner_decided_skipped"] == []


def test_prose_saying_do_not_re_ask_still_notifies():
    """guard-4015: an exemption harvested from free text inherits the SCRAPER's
    precision, and fails as a silent MISS that disables the protection."""
    res, _ = _drive("human_blocked: the owner said do not re-ask about this")
    assert len(res["needs_user_notification"]) == 1


def test_malformed_ref_still_notifies():
    """'yes' names no record anyone can open, so it is not a decision ref."""
    res, _ = _drive("human_blocked: something [owner-decided: yes]")
    assert len(res["needs_user_notification"]) == 1


def test_marker_on_a_non_human_blocked_defer_does_not_suppress():
    """Scope is deliberately narrow — the marker cannot suppress a
    precondition_unmet defer, which re-probes and is not owner-facing."""
    res, _ = _drive("precondition_unmet: waiting on a window "
                    "[owner-decided: msg-20260902-231119-alpha-599]")
    assert res["owner_decided_skipped"] == [], \
        "the marker suppressed a defer class it is not scoped to"


def test_predicate_raising_escalates_rather_than_swallowing():
    """The unrecoverable direction. A broken predicate must never read as
    'park' — that silently removes an owner-facing escalation."""
    def _boom(goal):
        raise RuntimeError("predicate exploded")
    try:
        res, _ = _drive(PARK_DEFER, predicate=_boom)
    except RuntimeError:
        raise AssertionError(
            "a raising predicate propagated and killed the whole lane — "
            "fail-CLOSED on delivery, worse than the over-notification")
    assert len(res["needs_user_notification"]) == 1, \
        "a raising predicate suppressed the escalation"
    assert any("predicate raised" in f.get("detail", "") for f in res["failed"]), \
        "the degraded run did not record its own failure, so it reports as clean"


# ── Outcome 4: the MUTATION PROOF ───────────────────────────────────────────

def test_mutation_disabling_the_predicate_restores_the_notification():
    """NON-VACUITY BY MUTATION, not by assertion (outcome 4).

    Drive the IDENTICAL park entry twice, changing exactly one thing: the
    predicate's verdict. Live predicate -> suppressed. Mutated to always-None
    (the pre-fix behaviour) -> notified. That the two differ is what proves the
    suppression is CAUSED by the predicate consult and not by some incidental
    property of the fixture — the failure mode a green test cannot otherwise
    distinguish from a path that would answer the same way regardless.
    """
    live, _ = _drive(PARK_DEFER)
    mutated, _ = _drive(PARK_DEFER, predicate=lambda goal: None)

    assert live["needs_user_notification"] == []
    assert len(live["owner_decided_skipped"]) == 1

    assert len(mutated["needs_user_notification"]) == 1, \
        "mutation did not restore the notification — the test is vacuous: the " \
        "park was being suppressed by something other than the predicate"
    assert mutated["owner_decided_skipped"] == []


def test_the_dispositions_do_not_collapse():
    """Anti-vacuity across inputs (guard-1220), the companion to the mutation
    proof above: four root shapes, and the park must be the ONLY one suppressed."""
    seen = set()
    for dr in (PARK_DEFER,
               "human_blocked: waiting on a person to decide",
               "human_blocked: something [owner-decided: yes]",
               "user_action: click approve"):
        res, posts = _drive(dr)
        seen.add((len(res["needs_user_notification"]),
                  len(res["owner_decided_skipped"]),
                  len(posts)))
    assert len(seen) == 2, \
        "expected exactly two dispositions (suppressed vs notified), got %d" % len(seen)
    assert (0, 1, 0) in seen, "the park was never suppressed"
    assert (1, 0, 1) in seen, "the non-park roots were never notified"
