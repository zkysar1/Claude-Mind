#!/usr/bin/env python3
""" — dependency-timeout-check must RE-PROBE a root's stored blocker
before escalating it to a human, not relay the stale verdict.

THE INCIDENT. alpha filed a `credentials-required` blocker on g-335-210
(2026-07-22) concluding the fleet key mint needed a human Cognito mint. zeta's
escalation email 89.5h later relayed that conclusion faithfully AND inherited
its confidence, telling the user it was "a genuine human-only class ... not work
an agent routed around". A ~5-minute read-only re-probe falsified it three days
later. Nothing in the escalation path had re-run the probe that produced the
verdict — it read `defer_reason` and rendered it.

WHAT IS PINNED, one case per verification outcome the goal names:
  1. a probeable root whose probe now PASSES suppresses the notification,
     clears the defer, and posts a correction;
  2. a probeable root whose probe still FAILS notifies, and the body carries
     the re-probe's OWN timestamp so the human can see the claim is fresh;
  3. the unprobeable classes escalate UNCHANGED, and the body is byte-identical
     to the pre-change text (asserted by absence of the re-probe paragraph);
  4. a probe that raises, times out, or cannot be imported is FAIL-OPEN — the
     escalation still fires rather than being silently swallowed;
  5. suppression happens BEFORE the board post, so a suppressed escalation does
     not burn its own cooldown slot and go silent on the next sweep too.

Case 5 is the one whose absence would be invisible: every other assertion would
still pass if the re-probe ran after _post_board, and the damage (a permanently
silent escalation) would only appear a sweep later, in production.

ANTI-VACUITY (guard-1220). test_the_four_outcomes_do_not_collapse drives the
SAME entry through all four probe verdicts and asserts four DISTINCT
dispositions. A path that answered identically for all of them would pass every
individual case above while being useless.

STUBBING SEAM, and what it EXCLUDES (guard-1462). `_cred_probe_module` is
stubbed, so everything UPSTREAM of it is structurally unfalsifiable here: that
credential-defer-recheck.py is importable at all, that its `_extract_env_key`
regexes match real defer prose, and that `env-read.sh has KEY` reports what the
fleet's credential store actually holds. Those belong to that module's own
tests; what this file proves is that the ESCALATION PATH consults a probe and
routes on its verdict.

Run: py -3 -m pytest core/scripts/tests/test_dependency_timeout_reprobe.py -v
"""
import datetime as dt
import importlib.util
import sys
import types
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))


def _load():
    spec = importlib.util.spec_from_file_location(
        "dependency_timeout_reprobe_module",
        SCRIPT_DIR / "dependency-timeout-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load()

BLOCKED_SINCE = (dt.datetime.now() - dt.timedelta(hours=40)).isoformat()


def _cred_stub(key="LOD_MINT_KEY", present=False, raises=None):
    """A stand-in for credential-defer-recheck.py's two probe entry points."""
    m = types.SimpleNamespace()
    m._extract_env_key = lambda dr: key
    def _probe(k):
        if raises:
            raise raises
        return present
    m._probe_env_key = _probe
    return m


# ── _reprobe_root: one verdict per input class ──────────────────────────────

def test_no_defer_reason_is_unprobeable():
    assert M._reprobe_root({"id": "g-1"})["outcome"] == "unprobeable"


def test_user_action_class_is_unprobeable_and_spawns_nothing():
    """Outcome 4's 'no added latency' half, asserted structurally.

    If the module loader is reached at all for an unprobeable class, the class
    is paying import + subprocess cost it was promised it would not.
    """
    reached = []
    orig = M._cred_probe_module
    M._cred_probe_module = lambda: reached.append(1) or _cred_stub()
    try:
        for dr in ("user_action: click approve",
                   "security-trust: needs a human decision",
                   "physical-hardware: replug the token"):
            assert M._reprobe_root({"defer_reason": dr})["outcome"] == "unprobeable"
    finally:
        M._cred_probe_module = orig
    assert reached == [], "an unprobeable class reached the probe module"


def test_human_blocked_is_deliberately_not_probed():
    """`human_blocked:` is the one prefix that never auto-clears
    (probe-before-defer.md); credential-defer-recheck.py owns that lane."""
    assert M._reprobe_root(
        {"defer_reason": "human_blocked: needs LOD_MINT_KEY"}
    )["outcome"] == "unprobeable"


def test_credentials_defer_naming_no_key_is_unprobeable_not_cleared():
    """No key means no measurement, and an absent measurement must never
    become a verdict — the exact inversion this whole change removes."""
    orig = M._cred_probe_module
    M._cred_probe_module = lambda: _cred_stub(key=None)
    try:
        r = M._reprobe_root({"defer_reason": "credentials-required: something"})
    finally:
        M._cred_probe_module = orig
    assert r["outcome"] == "unprobeable" and r["key"] is None


def test_probe_present_clears():
    orig = M._cred_probe_module
    M._cred_probe_module = lambda: _cred_stub(present=True)
    try:
        r = M._reprobe_root({"defer_reason": "credentials-required: LOD_MINT_KEY"})
    finally:
        M._cred_probe_module = orig
    assert r["outcome"] == "cleared" and r["key"] == "LOD_MINT_KEY"


def test_probe_absent_stays_blocked():
    orig = M._cred_probe_module
    M._cred_probe_module = lambda: _cred_stub(present=False)
    try:
        r = M._reprobe_root({"defer_reason": "credentials-required: LOD_MINT_KEY"})
    finally:
        M._cred_probe_module = orig
    assert r["outcome"] == "still_blocked"


def test_probe_raising_is_error_not_cleared():
    """FAIL-OPEN direction check. A broken probe must never read as 'cleared',
    which would SUPPRESS a real escalation — the unrecoverable direction."""
    orig = M._cred_probe_module
    M._cred_probe_module = lambda: _cred_stub(raises=RuntimeError("boom"))
    try:
        r = M._reprobe_root({"defer_reason": "credentials-required: LOD_MINT_KEY"})
    finally:
        M._cred_probe_module = orig
    assert r["outcome"] == "error" and "boom" in r["detail"]


def test_unimportable_probe_module_is_error_not_cleared():
    orig = M._cred_probe_module
    M._cred_probe_module = lambda: None
    try:
        r = M._reprobe_root({"defer_reason": "credentials-required: LOD_MINT_KEY"})
    finally:
        M._cred_probe_module = orig
    assert r["outcome"] == "error"


# ── _reprobe_line: what the human actually reads ────────────────────────────

def test_unprobeable_adds_no_paragraph_at_all():
    """Outcome 4 literally: those escalation bodies did not move."""
    assert M._reprobe_line(None) == ""
    assert M._reprobe_line({"outcome": "unprobeable", "at": "X"}) == ""


def test_still_blocked_line_carries_the_probe_timestamp():
    line = M._reprobe_line({"outcome": "still_blocked", "at": "2026-08-28T06:00:00",
                            "detail": "env-read.sh has K -> absent"})
    assert "2026-08-28T06:00:00" in line and "fresh measurement" in line


def test_error_line_says_the_claim_was_not_retested():
    line = M._reprobe_line({"outcome": "error", "at": "2026-08-28T06:00:00",
                            "detail": "probe raised: boom"})
    assert "WITHOUT fresh evidence" in line and "not been re-tested" in line


# ── run(): the escalation path end to end ───────────────────────────────────

class _Args:
    apply = True
    threshold_hours = 1.0
    agent = "alpha"
    board_escalation_log = None
    no_board = False


def _drive(defer_reason, probe_module, participants=None):
    """Run the sweep over ONE aged dependency whose root carries `defer_reason`.

    Returns (result, board_posts, cleared_calls).
    """
    posts, cleared = [], []
    saved = (M._read_blocked, M._read_goal_index, M._read_recent_escalations,
             M._load_threshold_hours, M._resolve_self_agent,
             M._post_board, M._clear_defer, M._cred_probe_module)
    M._read_blocked = lambda: {"blocked_goals": [
        {"goal_id": "g-1", "block_reason": "dependency"}]}
    M._read_goal_index = lambda: {
        "g-1": {"id": "g-1", "title": "waiter", "_source": "world",
                "blocked_since": BLOCKED_SINCE, "blocked_by": ["g-root"]},
        "g-root": {"id": "g-root", "title": "the root", "_source": "world",
                   "status": "blocked", "description": "root desc",
                   "participants": participants or ["agent"],
                   "defer_reason": defer_reason},
    }
    M._read_recent_escalations = lambda *a, **k: set()
    M._load_threshold_hours = lambda a: 1.0
    M._resolve_self_agent = lambda a: "alpha"
    M._post_board = lambda gid, rid, age, detail, nb: (
        posts.append(detail) or (True, "posted"))
    M._clear_defer = lambda rid, src: (cleared.append(rid) or (True, "cleared %s" % rid))
    M._cred_probe_module = lambda: probe_module
    try:
        return M.run(_Args()), posts, cleared
    finally:
        (M._read_blocked, M._read_goal_index, M._read_recent_escalations,
         M._load_threshold_hours, M._resolve_self_agent,
         M._post_board, M._clear_defer, M._cred_probe_module) = saved


def test_falsified_verdict_suppresses_the_notification():
    res, posts, cleared = _drive("credentials-required: LOD_MINT_KEY",
                                 _cred_stub(present=True))
    assert res["needs_user_notification"] == [], "a falsified blocker still emailed a human"
    assert len(res["reprobe_suppressed"]) == 1
    assert cleared == ["g-root"], "the falsified defer was not cleared"
    assert any("RE-PROBE FALSIFIED" in p for p in posts), \
        "no board correction naming the stale verdict"


def test_suppression_does_not_burn_the_cooldown_slot():
    """Case 5. The board post records the durable cooldown, so a suppressed
    escalation that had already been counted as `escalated` would stay silent
    on the NEXT sweep too — a correct suppression turned permanent."""
    res, _, _ = _drive("credentials-required: LOD_MINT_KEY", _cred_stub(present=True))
    assert res["escalated"] == [], \
        "a suppressed escalation was recorded as escalated and burned its cooldown"


def test_still_blocked_notifies_with_a_fresh_timestamp():
    res, _, cleared = _drive("credentials-required: LOD_MINT_KEY",
                             _cred_stub(present=False))
    assert len(res["needs_user_notification"]) == 1
    body = res["needs_user_notification"][0]["message"]
    assert "fresh measurement" in body and "LOD_MINT_KEY" in body
    assert cleared == [], "a still-failing probe must not clear the defer"


def test_unprobeable_root_notifies_with_the_body_unchanged():
    res, _, _ = _drive("user_action: click approve", _cred_stub())
    assert len(res["needs_user_notification"]) == 1
    body = res["needs_user_notification"][0]["message"]
    assert "Re-probed at" not in body and "WITHOUT fresh evidence" not in body


def test_probe_error_still_escalates():
    """Outcome 5: fail-open. A broken probe must not swallow the escalation."""
    res, _, cleared = _drive("credentials-required: LOD_MINT_KEY",
                             _cred_stub(raises=TimeoutError("probe timed out")))
    assert len(res["needs_user_notification"]) == 1
    assert "WITHOUT fresh evidence" in res["needs_user_notification"][0]["message"]
    assert cleared == []


def test_the_four_outcomes_do_not_collapse():
    """Anti-vacuity (guard-1220): four probe verdicts, four DISTINCT
    dispositions. A path that answered the same way for all of them would pass
    every case above individually while doing nothing."""
    seen = set()
    for dr, mod in (("credentials-required: K", _cred_stub(present=True)),
                    ("credentials-required: K", _cred_stub(present=False)),
                    ("credentials-required: K", _cred_stub(raises=RuntimeError("x"))),
                    ("user_action: approve", _cred_stub())):
        res, _, cleared = _drive(dr, mod)
        n = res["needs_user_notification"]
        seen.add((len(n), len(res["reprobe_suppressed"]), bool(cleared),
                  ("Re-probed at" in n[0]["message"]) if n else None,
                  ("WITHOUT fresh evidence" in n[0]["message"]) if n else None))
    assert len(seen) == 4, "the four probe verdicts collapsed to %d disposition(s)" % len(seen)
