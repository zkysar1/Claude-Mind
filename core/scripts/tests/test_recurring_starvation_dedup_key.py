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


def _row(goal_id: str, source: str) -> dict:
    """One starved row in the exact shape scan() emits."""
    return {
        "goal_id": goal_id, "aspiration_id": "asp-001", "source": source,
        "title": "Recurring: synthetic sweep", "age_hours": 150.0,
        "anchor_field": "lastAchievedAt", "interval_hours": 24,
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

    dedup_key = rsc._origin_signal(row["goal_id"], row["source"])
    assert captured["sig"] == dedup_key, (
        "filing site and dedup site disagree — the sweep would re-file every "
        "run while every per-function test still passed (rb-3879)"
    )


# ── Dedup behaviour across the transition ────────────────────────────────

def test_own_legacy_key_still_dedups(monkeypatch):
    """`existing` is built from THIS agent's queues only, so a legacy key in
    it is necessarily our own prior filing. Without this the first post-fix
    run re-files every Unblock the agent already has."""
    monkeypatch.setenv("MIND_AGENT", "bravo")
    row = _row("g-001-01", "agent")
    filed = []
    monkeypatch.setattr(rsc._rt, "aspirations_add_goal",
                        lambda a, p, source=None: filed.append(p) or {"id": "g-1"})
    monkeypatch.setattr(rsc, "_existing_origin_signals",
                        lambda: {"unblock:recurring-starved-g-001-01"})
    monkeypatch.setattr(rsc, "scan", lambda m, breaks=None: ([row], dict(STATS)))
    monkeypatch.setattr(sys, "argv",
                        ["recurring-starvation-check.py", "--apply", "--output", "json"])
    assert rsc.main() == 0
    assert filed == [], "own legacy key must still suppress a re-file"


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
    assert filed[0]["origin_signal"] == "unblock:recurring-starved-echo-g-001-02"
