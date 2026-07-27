"""test_unblock_circuit_breaker.py —  circuit breaker for defer->Unblock churn.

Tests the standing-blocker circuit breaker in aspirations.py: after N=3 RESOLVED
same-parent Unblocks, the parent is escalated to a tracked blocker (blocker_ref +
structured 'Circuit breaker:' defer_reason) WHILE the Nth+1 Unblock is STILL
filed. The non-suppression (fail-OPEN) invariant is the load-bearing test —
guard-487 warns that suppressing the re-file inverts fail-closed on an
unverifiable continuous-vs-recurred signal (the naive fix zeta's g-115-2698
investigation REJECTED). This breaker ESCALATES instead of suppressing.

Covers:
  - _count_resolved_unblocks_for counts only RESOLVED Unblocks whose
    origin_signal matches the parent (active ones + other-parent ones excluded)
  - below threshold (2 resolved) -> no escalation, Unblock filed normally
  - at threshold (3 resolved) -> parent escalated (blocker_ref + structured
    defer) AND the Unblock is STILL filed (guard-487 non-suppression invariant)
  - idempotent: parent already carries a blocker_ref -> no re-escalation
    (existing ref preserved), Unblock still files
  - _escalate_standing_blocker: parent-not-found -> None (fail-open, no crash)

Pattern: same importlib + sys.path shape as test_defer_gate_unblock_filing.py.
Operates on synthetic in-memory items only — no real aspirations.jsonl touched,
no disk writes (the helper mutates the passed list; the caller does the write).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))


def _import_asp():
    """Load aspirations.py via importlib (hyphen-free attribute name).

    Sets AGENT_DIR = None so _count_resolved_unblocks_for / _find_existing_unblock_for
    never read a live agent queue — the count comes ONLY from the synthetic items,
    keeping the tests hermetic under any MIND_AGENT binding.
    """
    spec = importlib.util.spec_from_file_location(
        "aspirations_mod_cb", CORE_SCRIPTS / "aspirations.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load spec for aspirations.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.AGENT_DIR = None
    return mod


def _items_with_resolved_unblocks(n_resolved: int, parent_id: str = "g-115-149",
                                  parent_extra: dict | None = None) -> list:
    """asp-001 (Unblock route target) + asp-115 with the parent goal + n resolved
    Unblocks (origin_signal='unblock:{parent}', status completed/skipped)."""
    parent = {"id": parent_id, "title": "Some original goal", "status": "pending",
              "category": "framework-maintenance", "type": "idea"}
    if parent_extra:
        parent.update(parent_extra)
    unblocks = []
    for i in range(n_resolved):
        unblocks.append({
            "id": f"g-115-90{i}",
            "title": f"Unblock: deploy for {parent_id}",
            "status": "completed" if i % 2 == 0 else "skipped",
            "origin_signal": f"unblock:{parent_id}",
            "type": "idea",
            "category": "framework-maintenance",
        })
    return [
        {"id": "asp-001", "title": "Meta", "status": "active",
         "goals": [{"id": "g-001-01", "title": "x", "status": "pending",
                    "category": "meta", "type": "recurring"}]},
        {"id": "asp-115", "title": "Framework health", "status": "active",
         "goals": [parent] + unblocks},
    ]


def _gate() -> dict:
    """Synthetic capability-gate result with unblock_suggested (mirrors the
    shape _file_unblock_under_existing_lock consumes)."""
    return {
        "would_block": True,
        "unblock_suggested": True,
        "unblock_title": "Unblock: deploy for g-115-149",
        "unblock_description": "Capability gate matched. Action required: 'deploy'.",
        "matched_capability": {"skill": "some-skill", "matched_keyword": "sample"},
        "matches": [{"skill": "some-skill", "matched_keyword": "sample"}],
        "match_count": 1,
        "narrative_framing_detected": False,
    }


def _parent(items: list, parent_id: str = "g-115-149") -> dict:
    return next(g for asp in items for g in asp.get("goals", [])
               if g.get("id") == parent_id)


def test_count_resolved_unblocks_only_matching():
    """Counts only RESOLVED Unblocks whose origin_signal matches the parent —
    active same-parent Unblocks and resolved OTHER-parent Unblocks are excluded."""
    mod = _import_asp()
    items = _items_with_resolved_unblocks(3)
    # Noise 1: an ACTIVE (pending) Unblock for the same parent — must NOT count.
    items[1]["goals"].append({
        "id": "g-115-950", "title": "Unblock: deploy for g-115-149",
        "status": "pending", "origin_signal": "unblock:g-115-149", "type": "idea"})
    # Noise 2: a RESOLVED Unblock for a DIFFERENT parent — must NOT count.
    items[1]["goals"].append({
        "id": "g-115-951", "title": "Unblock: x for g-999-99",
        "status": "completed", "origin_signal": "unblock:g-999-99", "type": "idea"})
    n = mod._count_resolved_unblocks_for(items, "g-115-149", also_scan_agent=False)
    assert n == 3, f"expected 3 resolved matching Unblocks, got {n}"


def test_below_threshold_no_escalation_unblock_filed():
    """2 resolved (< 3): no escalation, Unblock files normally."""
    mod = _import_asp()
    items = _items_with_resolved_unblocks(2)
    filed_id, status = mod._file_unblock_under_existing_lock(items, "g-115-149", _gate())
    assert filed_id is not None, \
        f"below threshold: Unblock must still file, got None ({status})"
    parent = _parent(items)
    assert not parent.get("blocker_ref"), "below threshold: parent must NOT be escalated"
    assert not parent.get("defer_reason"), "below threshold: parent must NOT get a defer_reason"


def test_at_threshold_escalates_and_still_files():
    """3 resolved (>= 3): parent escalated to a tracked blocker AND the Unblock
    is STILL filed — the guard-487 non-suppression invariant (load-bearing)."""
    mod = _import_asp()
    items = _items_with_resolved_unblocks(3)
    filed_id, status = mod._file_unblock_under_existing_lock(items, "g-115-149", _gate())
    # Non-suppression: the Nth+1 Unblock STILL files (fail-OPEN).
    assert filed_id is not None, \
        f"guard-487: Unblock MUST still file at threshold, got None ({status})"
    # Escalation: parent gains a valid blocker_ref + structured Circuit-breaker defer.
    parent = _parent(items)
    ref = parent.get("blocker_ref")
    assert ref, "at threshold: parent must be escalated with a blocker_ref"
    assert ref["type"] == "infrastructure", f"blocker_ref.type should be infrastructure, got {ref['type']!r}"
    assert ref["external_id"] == "standing-unblock-churn:g-115-149", \
        f"unexpected external_id {ref['external_id']!r}"
    assert ref.get("expires_at"), "blocker_ref must carry an auto-populated expires_at"
    assert (parent.get("defer_reason") or "").startswith("Circuit breaker:"), \
        "at threshold: defer_reason must use the structured 'Circuit breaker:' prefix"
    assert parent.get("defer_reason_set_at"), "at threshold: defer_reason_set_at must be stamped"


def test_idempotent_when_parent_already_tracked():
    """Parent already carries a blocker_ref: no re-escalation (existing ref
    preserved), Unblock still files."""
    mod = _import_asp()
    existing_ref = {"type": "infrastructure", "external_id": "prior-block",
                    "state_hash": None, "created_at": "2026-01-01T00:00:00",
                    "expires_at": "2026-01-06T00:00:00"}
    items = _items_with_resolved_unblocks(3, parent_extra={"blocker_ref": existing_ref})
    filed_id, status = mod._file_unblock_under_existing_lock(items, "g-115-149", _gate())
    assert filed_id is not None, f"idempotent: Unblock still files ({status})"
    parent = _parent(items)
    assert parent["blocker_ref"]["external_id"] == "prior-block", \
        "idempotent: existing blocker_ref must NOT be overwritten"


def test_escalate_parent_not_found_returns_none():
    """Parent id absent from items: _escalate_standing_blocker returns None
    (fail-open, no crash)."""
    mod = _import_asp()
    items = _items_with_resolved_unblocks(3)
    result = mod._escalate_standing_blocker(items, "g-999-does-not-exist", 3)
    assert result is None, "parent-not-found must return None (fail-open, no crash)"


def main() -> int:
    """Direct-run aggregator (belt-and-suspenders; pytest collects the test_* fns)."""
    tests = [
        test_count_resolved_unblocks_only_matching,
        test_below_threshold_no_escalation_unblock_filed,
        test_at_threshold_escalates_and_still_files,
        test_idempotent_when_parent_already_tracked,
        test_escalate_parent_not_found_returns_none,
    ]
    failures = []
    for t in tests:
        try:
            t()
            print(f"  [PASS] {t.__name__}")
        except AssertionError as e:
            failures.append(f"{t.__name__}: {e}")
            print(f"  [FAIL] {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failures.append(f"{t.__name__}: unexpected {e!r}")
            print(f"  [ERROR] {t.__name__}: {e!r}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
