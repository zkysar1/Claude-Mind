"""test_recurring_starvation_dedup_key.py —  pins.

The starvation sweep's dedup key was `unblock:recurring-starved-<goal-id>`,
unqualified. Per-agent asp-001 queues REUSE the `g-001-NN` id space, so
`g-001-02` names a different goal in every agent's queue and the key collided
fleet-wide.

The collision was invisible locally and fatal remotely, because the two dedup
scopes differ: `_existing_origin_signals()` reads only THIS agent's queues,
while `goal-duplication` scans EVERY agent's. Local dedup passed, the filing
was attempted, the gate refused. Net effect: only the FIRST agent in the fleet
to detect a starved `g-001-NN` could ever file it; every later agent's
starvation was permanently un-filable while the sweep printed REFUSED forever.

Measured twice, independently, on two boxes:
  - echo / cc-03: g-001-02 starved 154.1h and g-001-05 starved 145.3h, both
    refused against foxtrot's identically-keyed g-001-61.
  - bravo / cc-05: g-001-01 refused against alpha's g-001-349 and zeta's
    g-001-70, all three carrying the same unqualified key.

THE LOAD-BEARING PIN is `test_both_call_sites_agree`. There are exactly two
consumers of this key — the pre-file dedup check in `main()` and the payload
built by `_file_unblock()` — and before this fix they were independently
written f-strings. Qualifying one and not the other does not fail loudly: the
local dedup would simply stop matching its own prior filing and re-file every
run, which is a WORSE bug than the one being fixed and would pass every
per-function test. That is rb-3879 ("grep ALL publishers before wiring a
change at one call site") stated as a test.

Only the daemon boundary is stubbed, so the real `_file_unblock` payload
construction and the real `main()` dedup branch both execute.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

_spec = importlib.util.spec_from_file_location(
    "recurring_starvation_dedup_key",
    str(SCRIPT_DIR / "recurring-starvation-check.py"),
)
rsc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rsc)

STATS = {"examined": 1, "shelved": 0, "basis_suppressed": 0,
         "unreadable_anchor": 0, "sources_seen": 1}


def _row(goal_id: str, source: str, anchor: str = "2026-08-01T00:00:00") -> dict:
    """One starved row in the exact shape scan() emits.

    `anchor` is production shape as of g-115-6398 — scan() emits the anchor
    VALUE alongside `anchor_field`, and the dedup key is built from it. A
    fixture without it would exercise the None-anchor fallback on every test
    while claiming to be production-shaped, which is guard-920 exactly.
    """
    return {
        "goal_id": goal_id, "aspiration_id": "asp-001", "source": source,
        "title": "Recurring: synthetic sweep", "age_hours": 150.0,
        "anchor_field": "lastAchievedAt", "anchor": anchor,
        "interval_hours": 24,
        "basis_hours": 30.0, "basis_reason": "interval", "ratio": 5.0,
        "declared_ratio": 6.25, "intended_agent": "either",
    }


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("MIND_AGENT", raising=False)
    monkeypatch.setattr(rsc, "_read_active", lambda source: [])


# ── The key itself ────────────────────────────────────────────────────────

def test_world_source_key_is_unchanged():
    """World ids are globally unique, so re-keying them would only orphan
    every Unblock already filed under the old form."""
    assert (rsc._origin_signal("g-115-67", "world")
            == "unblock:recurring-starved-g-115-67")


def test_agent_source_key_is_qualified_by_owner():
    assert (rsc._origin_signal("g-001-01", "agent", "bravo")
            == "unblock:recurring-starved-bravo-g-001-01")


def test_same_goal_id_yields_distinct_keys_across_agents():
    """THE bug, stated directly:  exists in every agent's queue."""
    keys = {rsc._origin_signal("g-001-02", "agent", a)
            for a in ("alpha", "bravo", "echo", "foxtrot", "zeta")}
    assert len(keys) == 5, f"agent ids must not collide, got {keys}"


def test_blank_agent_falls_back_to_legacy_not_a_blank_qualifier():
    """`...-<blank>-<id>` would read as qualified while colliding fleet-wide
    in a NEW way — strictly worse than the original bug."""
    assert (rsc._origin_signal("g-001-01", "agent", "")
            == "unblock:recurring-starved-g-001-01")


def test_agent_falls_back_to_env_when_not_passed(monkeypatch):
    monkeypatch.setenv("MIND_AGENT", "foxtrot")
    assert (rsc._origin_signal("g-001-02", "agent")
            == "unblock:recurring-starved-foxtrot-g-001-02")


# ── The load-bearing pin: the two call sites cannot drift apart ───────────

def test_both_call_sites_agree(monkeypatch):
    """The filed payload's origin_signal MUST equal the key main() deduped on.

    If these diverge, dedup silently stops matching its own prior filing and
    the sweep re-files every run — a failure no per-function test would catch.
    """
    monkeypatch.setenv("MIND_AGENT", "bravo")
    row = _row("g-001-01", "agent")

    captured = {}

    def _fake(asp_id, payload, source=None):
        captured["sig"] = payload.get("origin_signal")
        return {"id": "g-115-9001"}

    monkeypatch.setattr(rsc._rt, "aspirations_add_goal", _fake)
    monkeypatch.setattr(rsc, "_existing_origin_signals", lambda: set())
    monkeypatch.setattr(rsc, "scan", lambda m, breaks=None: ([row], dict(STATS)))
    monkeypatch.setattr(sys, "argv",
                        ["recurring-starvation-check.py", "--apply", "--output", "json"])
    assert rsc.main() == 0

    # Mirror main()'s call shape EXACTLY, anchor included (guard-920). If this
    # is computed without the anchor while main() passes one, the pin reports a
    # disagreement that does not exist in production — and, worse, a real
    # divergence in the anchor argument would hide behind the false alarm.
    dedup_key = rsc._origin_signal(row["goal_id"], row["source"],
                                   anchor=row.get("anchor"))
    assert captured["sig"] == dedup_key, (
        "filing site and dedup site disagree — the sweep would re-file every "
        "run while every per-function test still passed (rb-3879)"
    )


# ── Dedup behaviour across the transition ────────────────────────────────

def _run_apply(monkeypatch, row, existing=frozenset(), open_sigs=frozenset()):
    """Drive main() --apply with both dedup lookups stubbed. Returns payloads.

    The two lookups are patched SEPARATELY on purpose: they carry different
    status rules (g-115-6398), and a helper that fed one set to both would make
    every test below unable to tell them apart — which is precisely the
    conflation the fix removes.
    """
    filed = []
    monkeypatch.setattr(rsc._rt, "aspirations_add_goal",
                        lambda a, p, source=None: filed.append(p) or {"id": "g-1"})
    monkeypatch.setattr(rsc, "_existing_origin_signals", lambda: set(existing))
    monkeypatch.setattr(rsc, "_open_origin_signals", lambda: set(open_sigs))
    monkeypatch.setattr(rsc, "scan", lambda m, breaks=None: ([row], dict(STATS)))
    monkeypatch.setattr(sys, "argv",
                        ["recurring-starvation-check.py", "--apply", "--output", "json"])
    assert rsc.main() == 0
    return filed


def test_own_open_bare_legacy_key_still_dedups(monkeypatch):
    """Generation-1 key, still OPEN: must suppress.

    Original g-115-4241 intent, preserved: without this the first post-fix run
    re-files every Unblock the agent already has open. What changed in
    g-115-6398 is only that the legacy match is now OPEN-scoped — an open
    holder suppresses exactly as before.
    """
    monkeypatch.setenv("MIND_AGENT", "bravo")
    filed = _run_apply(monkeypatch, _row("g-001-01", "agent"),
                       open_sigs={"unblock:recurring-starved-g-001-01"})
    assert filed == [], "own OPEN legacy key must still suppress a re-file"


def test_own_open_agent_qualified_key_still_dedups(monkeypatch):
    """Generation-2 key, still OPEN: must suppress.

    REGRESSION PIN. The first cut of g-115-6398 checked only the BARE
    unqualified legacy against the open set, so an agent-source row whose open
    holder carried the agent-QUALIFIED form (the g-115-4241 shape, which is
    what live holders actually carry) fell through and re-filed. Measured
    2026-08-16 on cc-07: g-001-08 (open holder g-001-365, pending) and
    g-001-120 (open holder g-001-363, in-progress) both flipped to WOULD FILE.
    There are THREE key generations, not two.
    """
    monkeypatch.setenv("MIND_AGENT", "bravo")
    filed = _run_apply(monkeypatch, _row("g-001-01", "agent"),
                       open_sigs={"unblock:recurring-starved-bravo-g-001-01"})
    assert filed == [], (
        "an OPEN agent-qualified holder must suppress — checking only the bare "
        "unqualified form re-files against live open Unblocks"
    )


def test_terminal_legacy_holder_no_longer_silences(monkeypatch):
    """THE FIX, stated as behaviour: a terminal pre-anchor holder must release.

    A terminal holder is in `existing` (any status) but NOT in `open_sigs`.
    Before g-115-6398 the legacy key was matched against the any-status set, so
    one completed or SKIPPED Unblock silenced that anchor forever — the first
    filing was the last filing (guard-3419). Live on cc-07 2026-08-16: g-326-85
    silenced by a COMPLETED g-326-293, g-326-84 by a SKIPPED g-326-135.
    """
    monkeypatch.setenv("MIND_AGENT", "bravo")
    filed = _run_apply(
        monkeypatch, _row("g-001-01", "agent"),
        existing={"unblock:recurring-starved-g-001-01",
                  "unblock:recurring-starved-bravo-g-001-01"},
        open_sigs=set(),          # every holder terminal
    )
    assert len(filed) == 1, (
        "a terminal-only pre-anchor holder must NOT silence a live starvation"
    )


def test_same_episode_still_dedups_even_when_holder_is_terminal(monkeypatch):
    """The guard-895 half, which the fix must NOT trade away.

    Within ONE episode the anchored key is unchanged, so a completed or skipped
    Unblock for THIS episode must still suppress a duplicate. Only a NEW
    episode (a moved anchor) may re-file.
    """
    monkeypatch.setenv("MIND_AGENT", "bravo")
    row = _row("g-001-01", "agent")
    same_episode = rsc._origin_signal(row["goal_id"], row["source"],
                                      anchor=row["anchor"])
    filed = _run_apply(monkeypatch, row,
                       existing={same_episode}, open_sigs=set())
    assert filed == [], (
        "same-episode dedup must hold at ANY status — otherwise the fix trades "
        "permanent silence for per-run duplicate filing (guard-895)"
    )


def test_new_episode_mints_a_fresh_key_and_refiles(monkeypatch):
    """The anchor moving is what releases the lease, with no close-on-clear
    branch for anyone to forget to wire (guard-3419)."""
    monkeypatch.setenv("MIND_AGENT", "bravo")
    old = _row("g-001-01", "agent", anchor="2026-08-01T00:00:00")
    new = _row("g-001-01", "agent", anchor="2026-08-09T12:00:00")
    old_key = rsc._origin_signal(old["goal_id"], old["source"],
                                 anchor=old["anchor"])
    new_key = rsc._origin_signal(new["goal_id"], new["source"],
                                 anchor=new["anchor"])
    assert old_key != new_key, "a moved anchor must mint a different key"
    filed = _run_apply(monkeypatch, new, existing={old_key}, open_sigs=set())
    assert len(filed) == 1, "a genuinely new starvation episode must re-file"
    assert filed[0]["origin_signal"] == new_key


@pytest.mark.parametrize("status,expect_open", [
    ("pending", True),
    ("in-progress", True),
    ("blocked", True),        # live work with an owner — must still suppress
    (None, True),             # unreadable status: fail SAFE toward suppressing
    ("completed", False),
    ("skipped", False),       # criterion (b): the anchor demonstrably did NOT fire
    ("expired", False),
    ("archived", False),
])
def test_open_origin_signals_status_split(monkeypatch, status, expect_open):
    """`_open_origin_signals` is where completed/skipped stops silencing.

    Pinned directly rather than only through main(), because every other test
    in this file patches this function and therefore cannot see its predicate.
    The SKIPPED row is the sharp case: a skipped Unblock is precisely where the
    anchor did not fire, so it is where an any-status match is most wrong.
    """
    goal = {"origin_signal": "unblock:recurring-starved-g-001-01"}
    if status is not None:
        goal["status"] = status
    monkeypatch.setattr(rsc, "_sources", lambda: ["world"])
    monkeypatch.setattr(rsc, "_read_active", lambda source: [{"goals": [goal]}])
    got = rsc._open_origin_signals()
    assert (goal["origin_signal"] in got) is expect_open, (
        f"status={status!r} must be treated as "
        f"{'OPEN (suppresses)' if expect_open else 'TERMINAL (releases)'}"
    )
    # The any-status set must contain it either way — that asymmetry IS the fix.
    assert goal["origin_signal"] in rsc._existing_origin_signals()


def test_anchorless_row_falls_back_to_unanchored_key():
    """A blank anchor must NOT mint `...-None`, mirroring the blank-agent
    fallback: a key that READS qualified while colliding is worse than none."""
    assert (rsc._origin_signal("g-115-67", "world", anchor=None)
            == "unblock:recurring-starved-g-115-67")
    assert (rsc._origin_signal("g-115-67", "world", anchor="")
            == "unblock:recurring-starved-g-115-67")


def test_partner_legacy_key_no_longer_blocks(monkeypatch):
    """The fix, stated as behaviour. A partner's identically-keyed Unblock is
    never in OUR `existing` set, so it must not suppress our filing — that
    suppression is what made every later agent's starvation un-filable."""
    monkeypatch.setenv("MIND_AGENT", "echo")
    row = _row("g-001-02", "agent")
    filed = []
    monkeypatch.setattr(rsc._rt, "aspirations_add_goal",
                        lambda a, p, source=None: filed.append(p) or {"id": "g-1"})
    # Partner keys live in the partner's queue, which this agent never reads.
    monkeypatch.setattr(rsc, "_existing_origin_signals", lambda: set())
    monkeypatch.setattr(rsc, "scan", lambda m, breaks=None: ([row], dict(STATS)))
    monkeypatch.setattr(sys, "argv",
                        ["recurring-starvation-check.py", "--apply", "--output", "json"])
    assert rsc.main() == 0
    assert len(filed) == 1, "echo must now be able to file for its own g-001-02"
    # Owner-qualified AND anchor-suffixed (): the anchor is what
    # makes the key name ONE starvation episode rather than the goal forever.
    assert (filed[0]["origin_signal"]
            == "unblock:recurring-starved-echo-g-001-02-20260801000000")
