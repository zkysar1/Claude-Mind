"""Tests for audit-deferred-defers.py classify() stale-structured downgrade.

Regression guard for the "well-formed is not valid" defect
(.claude/rules/reclaim-routed-work.md rule 2):

    classify() used to UNCONDITIONALLY early-return category "a" (genuine)
    for any defer_reason starting with a structured prefix, before consulting
    any other signal. Measured on the live queue 2026-07-28: 29 of 40 defers
    (72.5%) took that return, including defers frozen 83 and 95 days. The
    prefix attests that the author FORMATTED the defer, not that the reason
    is still a valid reason to stay stopped.

The fast path for FRESH structured defers is deliberately preserved — this
suite pins both halves so a future edit cannot restore the unconditional
return without going red, and cannot over-correct into flagging every
structured defer either.

Pattern: importlib + sys.path (the script name has hyphens, so it cannot be
a plain `import`) — same shape as test_defer_drift_check.py.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "audit-deferred-defers.py"


def _import():
    sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("audit_deferred_defers", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["audit_deferred_defers"] = mod
    spec.loader.exec_module(mod)
    return mod


MOD = _import()


def _ago(days: float) -> str:
    """A naive-ISO defer_reason_set_at `days` in the past (store format)."""
    return (dt.datetime.now() - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


STRUCTURED = "precondition_unmet: the shared service reports zero live instances"


# --- the preserved fast path ------------------------------------------------

def test_fresh_structured_defer_stays_genuine():
    """A recently-set structured defer keeps category 'a' (unchanged behavior)."""
    r = MOD.classify(STRUCTURED, ["agent"], defer_set_at=_ago(3))
    assert r["category"] == "a"
    assert any(e.startswith("structured-prefix:") for e in r["evidence"])
    assert not any("stale-structured" in e for e in r["evidence"])


def test_every_genuine_prefix_has_a_fast_path():
    """All three GENUINE_PREFIXES still short-circuit while fresh."""
    for pfx in MOD.GENUINE_PREFIXES:
        r = MOD.classify(f"{pfx} some condition", ["agent"], defer_set_at=_ago(1))
        assert r["category"] == "a", f"{pfx} lost its fresh fast path"


# --- the regression guard ---------------------------------------------------

def test_stale_structured_defer_downgrades_to_b():
    """THE guard: a well-formed but long-frozen defer must surface for re-check.

    Reverting classify() to the unconditional `return {"category": "a"}` turns
    this red — that is the mutation this test exists to catch.
    """
    r = MOD.classify(STRUCTURED, ["agent"], defer_set_at=_ago(40))
    assert r["category"] == "b", "stale structured defer was laundered as genuine"
    assert any("stale-structured" in e for e in r["evidence"])
    assert any(e.startswith("structured-prefix:") for e in r["evidence"]), \
        "the prefix must be preserved as evidence, not discarded"


def test_threshold_is_a_boundary_not_a_cliff():
    """Just under the threshold is genuine; well past it is not."""
    assert MOD.classify(STRUCTURED, ["agent"], defer_set_at=_ago(1),
                        stale_days=10)["category"] == "a"
    assert MOD.classify(STRUCTURED, ["agent"], defer_set_at=_ago(30),
                        stale_days=10)["category"] == "b"


def test_stale_days_is_tunable():
    """The same defer flips category purely on the caller's threshold."""
    aged = _ago(20)
    assert MOD.classify(STRUCTURED, ["agent"], defer_set_at=aged,
                        stale_days=60)["category"] == "a"
    assert MOD.classify(STRUCTURED, ["agent"], defer_set_at=aged,
                        stale_days=5)["category"] == "b"


# --- fail-open on a bad/absent timestamp (guard-142) ------------------------

def test_missing_timestamp_fails_open_to_genuine():
    """No defer_set_at => cannot be proven stale => keep the old verdict."""
    r = MOD.classify(STRUCTURED, ["agent"], defer_set_at=None)
    assert r["category"] == "a"
    assert "age:unknown" in r["evidence"]


def test_unparseable_timestamp_fails_open_to_genuine():
    """An audit heuristic must never manufacture staleness from a parse error."""
    r = MOD.classify(STRUCTURED, ["agent"], defer_set_at="not-a-timestamp")
    assert r["category"] == "a"
    assert "age:unknown" in r["evidence"]


def test_default_call_shape_is_backward_compatible():
    """Two-arg callers (the pre-change signature) must still work."""
    r = MOD.classify(STRUCTURED, ["agent"])
    assert r["category"] == "a"
    assert "age:unknown" in r["evidence"]


# --- unrelated classification paths are untouched ---------------------------

def test_empty_defer_reason_still_unknown():
    assert MOD.classify("", ["agent"], defer_set_at=_ago(99))["category"] == "unknown"


def test_non_structured_defer_ignores_age():
    """Age only gates the structured-prefix branch; other paths are unchanged.

    An old free-text defer that matches nothing still lands in the
    'unmatched: review-by-hand' bucket, not the stale-structured one.
    """
    r = MOD.classify("some free-text reason with no marker", ["agent"],
                     defer_set_at=_ago(99))
    assert not any("stale-structured" in e for e in r["evidence"])


def test_age_days_helper_is_fail_open():
    assert MOD._defer_age_days(None) is None
    assert MOD._defer_age_days("garbage") is None
    assert MOD._defer_age_days(_ago(10)) > 9.0


# ---- load_deferred(): terminal-status goals are not routed-away work --------
#
# A defer on an already-terminal goal is not reclaimable — there is nothing left
# to route. Reporting it anyway is worse than noise: measured on the live queue
# 2026-07-29, lane B reported 3 stale-structured defers of which 2 were `retired`,
# so 67% of the lane's output was permanent residue that reappears identically
# every sweep. That is what trains a reader to stop checking the lane, which is
# how the ONE real item (foxtrot's ) stayed surfaced-and-never-routed.


def _write_world(tmp_path, goals):
    """Build a one-aspiration tmp world and point the module at it."""
    world = tmp_path / "world"
    world.mkdir()
    (world / "aspirations.jsonl").write_text(
        json.dumps({"id": "asp-999", "goals": goals}) + "\n", encoding="utf-8"
    )
    return world


def _load_from(monkeypatch, world):
    monkeypatch.setattr(MOD, "WORLD_DIR", world)
    # Agent queues are a separate source; isolate the world path under test.
    monkeypatch.setattr(MOD, "_enumerate_agents", lambda: [])
    return MOD.load_deferred()


def _goal(gid, status):
    return {
        "id": gid,
        "title": f"goal {gid}",
        "status": status,
        "defer_reason": "precondition_unmet:something",
        "defer_reason_set_at": _ago(90),
        "participants": ["agent"],
    }


def test_terminal_status_defers_are_excluded(tmp_path, monkeypatch):
    """The mutation target: drop the filter and these four come back."""
    world = _write_world(tmp_path, [
        _goal("g-1-1", "pending"),
        _goal("g-1-2", "retired"),      # both live phantoms carried this
        _goal("g-1-3", "completed"),
        _goal("g-1-4", "skipped"),
        _goal("g-1-5", "expired"),
    ])
    ids = {r["goal_id"] for r in _load_from(monkeypatch, world)}
    assert ids == {"g-1-1"}, f"terminal-status defers leaked: {ids - {'g-1-1'}}"


def test_non_terminal_defers_all_survive(tmp_path, monkeypatch):
    """SPECIFICITY control — the filter must not over-reach.

    Stays GREEN under the mutation above, so a file-level pass cannot be
    mistaken for this case discriminating anything (guard-1660).
    """
    world = _write_world(tmp_path, [
        _goal("g-2-1", "pending"),
        _goal("g-2-2", "in-progress"),
        _goal("g-2-3", "blocked"),
    ])
    ids = {r["goal_id"] for r in _load_from(monkeypatch, world)}
    assert ids == {"g-2-1", "g-2-2", "g-2-3"}


def test_missing_status_is_kept_not_dropped(tmp_path, monkeypatch):
    """Absent status is unknown, not terminal — dropping it would hide real work."""
    g = _goal("g-3-1", "pending")
    del g["status"]
    ids = {r["goal_id"] for r in _load_from(monkeypatch, _write_world(tmp_path, [g]))}
    assert ids == {"g-3-1"}


def test_terminal_match_tolerates_case_and_whitespace(tmp_path, monkeypatch):
    world = _write_world(tmp_path, [
        _goal("g-4-1", "  Completed "),
        _goal("g-4-2", "RETIRED"),
        _goal("g-4-3", "pending"),
    ])
    ids = {r["goal_id"] for r in _load_from(monkeypatch, world)}
    assert ids == {"g-4-3"}


# ---- : the canonical terminal set --------------------------------
#
# This set had diverged from the framework's canonical terminal statuses, which
# five other modules write identically. These two members exclude ZERO rows on
# today's live corpus -- and so do three of the four incumbents, because rare
# statuses on DEFERRED goals is the normal state of a defensive predicate. The
# test is therefore the only place the members are exercised at all.

def test_canonical_terminal_statuses_are_excluded(tmp_path, monkeypatch):
    """decomposed + superseded are terminal everywhere else in the framework."""
    world = _write_world(tmp_path, [
        _goal("g-5-1", "pending"),
        _goal("g-5-2", "decomposed"),
        _goal("g-5-3", "superseded"),
    ])
    ids = {r["goal_id"] for r in _load_from(monkeypatch, world)}
    assert ids == {"g-5-1"}, f"canonical terminal statuses leaked: {ids - {'g-5-1'}}"


def test_terminal_set_matches_the_five_site_consensus_plus_retired():
    """Pins the SHAPE of the divergence, so a future edit must be deliberate.

    The five sibling modules (aspirations.py, insight-trigger-gate.py,
    insight-trigger-sweep.py, precheck-eval.py, unblock-parent-status-sweep.py)
    all declare the same five. This lane needs `retired` on top -- measured live,
    it is the ONLY member with a real population here.
    """
    canonical = {"completed", "skipped", "expired", "decomposed", "superseded"}
    assert canonical <= set(MOD.TERMINAL_STATUSES), \
        f"drifted below the canonical set: missing {canonical - set(MOD.TERMINAL_STATUSES)}"
    assert "retired" in MOD.TERMINAL_STATUSES, \
        "retired dropped -- it is the only member with a live population in this lane"


# ---- : human_blocked: reaches the age path -----------------------
#
# The 14-day downgrade lives INSIDE `if pfx:`, so an unrecognised prefix could
# never become a stale-structured re-check candidate AT ANY AGE. That was
# pointed at the one prefix `probe-before-defer.md` rule 1e says never
# auto-clears -- i.e. the defer MOST exposed to the RULE axis was the one this
# lane was structurally blind to. Measured on the live queue 2026-08-10: 11 of
# 36 rows, 2 already past threshold.

HUMAN_BLOCKED = "human_blocked: requires a fleet-quiesced window only the user can create"


def test_human_blocked_prefix_is_recognised():
    """MUTATION TARGET: drop `human_blocked:` from GENUINE_PREFIXES -> red."""
    r = MOD.classify(HUMAN_BLOCKED, ["agent", "user"], defer_set_at=_ago(3))
    assert r["category"] == "a"
    assert any(e == "structured-prefix:human_blocked:" for e in r["evidence"]), \
        "human_blocked: did not reach the structured-prefix branch"


def test_stale_human_blocked_becomes_recheck_candidate():
    """THE guard for this sub-case, and a mutation proof rather than a presence check.

    Removing `human_blocked:` from GENUINE_PREFIXES turns this red: without the
    prefix the row falls through to the pattern matchers, `pfx` is None, and the
    age branch that produces `stale-structured` is never reached at any age.
    """
    r = MOD.classify(HUMAN_BLOCKED, ["agent", "user"], defer_set_at=_ago(40))
    assert r["category"] == "b", "a 40d human-gated defer never became a re-check candidate"
    assert any("stale-structured" in e for e in r["evidence"])
    assert any(e == "structured-prefix:human_blocked:" for e in r["evidence"])


def test_human_blocked_defer_is_not_read_as_narrative_excuse():
    """The side-effect half -- VERIFIED, not assumed.

    A legitimate human-gate defer describes its gate in exactly the vocabulary
    DEFER_NARRATIVE_PATTERNS matches ("user approved", "user must"), so the
    better it documented what the human must do, the likelier this lane called
    it an excuse. The prefix branch returns before c_hits is computed.
    Live instance: g-115-4742 moved c -> a on this change.
    """
    text = ("human_blocked: USER DIRECTIVE 2026-08-04 -- user approved both remedies; "
            "the user must run the one remaining physical action on their own box")
    assert any(p in text.lower() for p in MOD.DEFER_NARRATIVE_PATTERNS), \
        "fixture no longer trips a narrative pattern -- it cannot prove anything"
    r = MOD.classify(text, ["agent", "user"], defer_set_at=_ago(6))
    assert r["category"] == "a", "a well-documented human gate was still read as an excuse"
    assert not any(e.startswith("narrative:") for e in r["evidence"])


def test_genuine_narrative_defer_still_classifies_c():
    """SPECIFICITY control (guard-1660) -- the narrative matcher is NOT disabled.

    Stays GREEN under the mutation above, so a file-level pass cannot be
    mistaken for the human_blocked cases discriminating anything. Live cat-c
    count went 1 -> 0 on this change; this proves that is the one false positive
    leaving, not the category becoming unreachable.
    """
    r = MOD.classify("waiting for user decision on the rollout", ["agent", "user"],
                     defer_set_at=_ago(2))
    assert r["category"] == "c"
    assert any(e.startswith("narrative:") for e in r["evidence"])


# ---- : a defer that names its own future window is not stale -----
#
# Staleness was age-since-defer_set_at ONLY, so a defer carrying an explicit
# machine-readable future window was flagged identically to one carrying no date
# -- then demanded a two-axis re-derivation every precheck iteration until the
# window closed. Measured 2026-08-10: 0 live firings TODAY, but three live
# defers will cross 14d while their own window is open ( by 1 day,
#  by 23,  by 38). These tests are the only current exercise.

STRUCTURED_GATED = ("precondition_unmet: hypothesis observation window not elapsed "
                    "(resolves_by {date})")


def _iso_in(days: int) -> str:
    return (dt.datetime.now() + dt.timedelta(days=days)).strftime("%Y-%m-%d")


def test_future_keyed_date_suppresses_stale_structured():
    """Past the age threshold, but the declared window is still open."""
    text = STRUCTURED_GATED.format(date=_iso_in(30))
    r = MOD.classify(text, ["agent"], defer_set_at=_ago(40))
    assert r["category"] == "a", "a defer inside its own declared window was flagged stale"
    assert not any("stale-structured" in e for e in r["evidence"])
    assert any(e.startswith("date-gated:resolves_by:") for e in r["evidence"])


def test_closed_window_does_NOT_suppress():
    """A window that has already closed is exactly when staleness IS the finding."""
    text = STRUCTURED_GATED.format(date=_iso_in(-5))
    r = MOD.classify(text, ["agent"], defer_set_at=_ago(40))
    assert r["category"] == "b", "a CLOSED window laundered the defer as genuine"
    assert any("stale-structured" in e for e in r["evidence"])


def test_bare_date_without_a_named_key_does_NOT_suppress():
    """The semantic-inversion guard: a due-date is not a defer-until date.

    A loose date scan would read an urgency DEADLINE as a licence to stay
    stopped -- inverting the meaning of the very text it matched.
    """
    r = MOD.classify(f"precondition_unmet: ship this by {_iso_in(30)} at the latest",
                     ["agent"], defer_set_at=_ago(40))
    assert r["category"] == "b"
    assert any("stale-structured" in e for e in r["evidence"])
    assert not any(e.startswith("date-gated:") for e in r["evidence"])


def test_unparseable_keyed_date_does_NOT_suppress():
    """Fail-open posture (guard-142): never launder on a parse failure."""
    r = MOD.classify("precondition_unmet: resolves_by 2026-13-45", ["agent"],
                     defer_set_at=_ago(40))
    assert r["category"] == "b"
    assert any("stale-structured" in e for e in r["evidence"])


def test_digit_between_key_and_date_does_NOT_match():
    """`\\D{0,32}` cannot cross a digit -- under-matching is the safe direction.

    A missed suppression costs one re-derivation; a wrong one hides a genuinely
    stale defer.
    """
    r = MOD.classify(f"precondition_unmet: deferred_until the 3rd review, {_iso_in(30)}",
                     ["agent"], defer_set_at=_ago(40))
    assert r["category"] == "b"
    assert not any(e.startswith("date-gated:") for e in r["evidence"])


def test_every_date_gate_key_is_reachable():
    """All three declared keys, exercised (guard-2616: no dead declared member)."""
    for key in ("resolves_by", "deferred_until", "window closes"):
        r = MOD.classify(f"precondition_unmet: {key} {_iso_in(30)}", ["agent"],
                         defer_set_at=_ago(40))
        assert r["category"] == "a", f"{key!r} did not suppress"
        assert any(e.startswith(f"date-gated:{key}:") for e in r["evidence"]), \
            f"{key!r} produced no date-gated evidence"


def test_date_gate_is_case_insensitive():
    r = MOD.classify(f"precondition_unmet: RESOLVES_BY {_iso_in(30)}", ["agent"],
                     defer_set_at=_ago(40))
    assert r["category"] == "a"
    assert any(e.startswith("date-gated:resolves_by:") for e in r["evidence"]), \
        "the key must be normalised to lowercase in evidence"


def test_fresh_date_gated_defer_is_unchanged():
    """MINIMALITY: under the threshold nothing was stale, so nothing is suppressed."""
    r = MOD.classify(STRUCTURED_GATED.format(date=_iso_in(30)), ["agent"],
                     defer_set_at=_ago(2))
    assert r["category"] == "a"
    assert not any(e.startswith("date-gated:") for e in r["evidence"])
    assert r["evidence"] == ["structured-prefix:precondition_unmet:", "age:2.0d"]


def test_keyed_future_date_helper_is_fail_open():
    assert MOD._keyed_future_date(None) is None
    assert MOD._keyed_future_date("") is None
    assert MOD._keyed_future_date("no date here at all") is None
    assert MOD._keyed_future_date("resolves_by not-a-date") is None
    assert MOD._keyed_future_date(f"resolves_by {_iso_in(-1)}") is None   # closed
    got = MOD._keyed_future_date(f"resolves_by {_iso_in(9)}")
    assert got is not None and got[0] == "resolves_by"


def test_suppressed_rows_never_carry_the_selector_token():
    """A suppression must not re-select the row through its own prose.

    Lane B (aspirations-precheck Phase 0.5b.13) picks rows by substring-testing
    evidence for `stale-structured`. The first draft of the date-gated evidence
    read "stale-structured suppressed", which put the selector token back into
    the payload and made the whole suppression a no-op at the consumer -- the
    row would have been exempted and re-selected in the same breath.

    Generalised deliberately: ANY row this classifier does not intend as a
    stale-structured finding must be free of the token, so a future explanatory
    string cannot reintroduce the defect from a different branch.
    """
    SELECTOR = "stale-structured"
    not_findings = [
        (STRUCTURED_GATED.format(date=_iso_in(30)), _ago(40)),   # date-gated
        (STRUCTURED, _ago(3)),                                   # fresh structured
        (STRUCTURED, None),                                      # age unknown
        (HUMAN_BLOCKED, _ago(3)),                                # fresh human gate
        ("waiting for user decision", _ago(2)),                  # narrative
        ("some free-text reason", _ago(2)),                      # unmatched
    ]
    for text, when in not_findings:
        r = MOD.classify(text, ["agent"], defer_set_at=when)
        assert not any(SELECTOR in e for e in r["evidence"]), (
            f"non-finding row leaked the lane-B selector token: {r['evidence']}")

    # POSITIVE CONTROL -- the token must still be present where it IS the
    # finding, or this test would pass on a classifier that never emits it.
    r = MOD.classify(STRUCTURED, ["agent"], defer_set_at=_ago(40))
    assert any(SELECTOR in e for e in r["evidence"]), \
        "the selector token vanished entirely -- lane B would report clean forever"


def test_all_evidence_strings_are_ascii():
    """Evidence is DATA that reaches shell args downstream (guard-607, guard-606).

    Covers every branch that emits evidence, not just the new ones -- a
    multi-byte sequence anywhere in this payload fails argv parsing.
    """
    cases = [
        (STRUCTURED, _ago(3)),                                   # fresh structured
        (STRUCTURED, _ago(40)),                                  # stale structured
        (STRUCTURED, None),                                      # age unknown
        (HUMAN_BLOCKED, _ago(3)),                                # fresh human gate
        (HUMAN_BLOCKED, _ago(40)),                               # stale human gate
        (STRUCTURED_GATED.format(date=_iso_in(30)), _ago(40)),   # date-gated
        ("waiting for user decision", _ago(2)),                  # narrative
        ("some free-text reason", _ago(2)),                      # unmatched
        ("", _ago(2)),                                           # unknown
    ]
    for text, when in cases:
        for e in MOD.classify(text, ["agent"], defer_set_at=when)["evidence"]:
            e.encode("ascii")   # raises UnicodeEncodeError on any non-ASCII
