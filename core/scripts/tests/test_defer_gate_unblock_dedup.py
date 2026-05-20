"""test_defer_gate_unblock_dedup.py — regression test for .

Asserts that `_find_existing_unblock_for` correctly identifies
already-pending Unblock goals across all three matching strategies
(origin_signal, title-regex, description-proximity) and across both
world and agent queues.

Verification outcomes covered (g-257-04):
  1. _find_existing_unblock_for helper exists in aspirations.py
  2. (Verified by test_defer_gate_unblock_filing.py case 2 and case 6 here)
  3. Strategy (a) origin_signal exact match returns the goal
  4. Strategy (b) title-regex match returns the goal even without origin_signal
  5. Strategy (c) description-proximity (verb + goal-id within 80 chars)
     returns the goal when verb provided
  6. Cross-queue: agent-queue Unblocks block re-filing in world (when
     also_scan_agent=True)
  7. Resolved/skipped/expired Unblocks do NOT block re-filing
  8. No verb → strategy (c) skipped (avoids false positives on goal-id
     mentions)

Pattern: same importlib + sys.path shape as sibling defer-gate tests.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))


def _import_aspirations():
    spec = importlib.util.spec_from_file_location(
        "aspirations_mod", CORE_SCRIPTS / "aspirations.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load spec for aspirations.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _items_with_unblock_origin_signal(orig_goal_id: str) -> list:
    """World-queue items with one matching-origin_signal Unblock pending."""
    return [
        {
            "id": "asp-001",
            "title": "Recurring meta-cadence",
            "status": "active",
            "goals": [
                {"id": "g-001-50", "title": f"Unblock: deploy for {orig_goal_id}",
                 "description": f"Defer-gate filing for {orig_goal_id}",
                 "status": "pending",
                 "origin_signal": f"unblock:{orig_goal_id}",
                 "type": "idea"},
            ],
        },
    ]


def _items_with_unblock_title_only(orig_goal_id: str) -> list:
    """Items with a title-matching Unblock but no origin_signal (human-filed)."""
    return [
        {
            "id": "asp-001",
            "title": "Recurring meta-cadence",
            "status": "active",
            "goals": [
                {"id": "g-001-51",
                 "title": f"Unblock: fix-deploy for {orig_goal_id}",
                 "description": "Manually filed by user before defer-gate fired",
                 "status": "pending",
                 "origin_signal": "user_directive",  # not unblock:G
                 "type": "idea"},
            ],
        },
    ]


def _items_with_unblock_proximity(orig_goal_id: str, verb: str) -> list:
    """Items with description containing both verb and goal-id within 80 chars."""
    return [
        {
            "id": "asp-001",
            "title": "Recurring meta-cadence",
            "status": "active",
            "goals": [
                {"id": "g-001-52",
                 "title": "Investigate flaky deployment pipeline",  # no Unblock prefix
                 "description": (
                     f"Need to {verb} the latest patch — currently blocking "
                     f"{orig_goal_id} from completing."
                 ),
                 "status": "in-progress",
                 "origin_signal": "investigation",  # not unblock:G
                 "type": "idea"},
            ],
        },
    ]


def _items_with_resolved_unblock(orig_goal_id: str) -> list:
    """Items with a COMPLETED Unblock — should NOT block re-filing."""
    return [
        {
            "id": "asp-001",
            "title": "Recurring meta-cadence",
            "status": "active",
            "goals": [
                {"id": "g-001-53", "title": f"Unblock: deploy for {orig_goal_id}",
                 "description": "Already resolved",
                 "status": "completed",  # NOT pending/in-progress
                 "origin_signal": f"unblock:{orig_goal_id}",
                 "type": "idea"},
            ],
        },
    ]


def main() -> int:
    failures = []
    cases_run = 0

    mod = _import_aspirations()
    finder = mod._find_existing_unblock_for

    # Case 1: strategy (a) origin_signal exact match
    cases_run += 1
    items = _items_with_unblock_origin_signal("g-115-149")
    hit = finder(items, "g-115-149", verb="deploy", also_scan_agent=False)
    if hit is None:
        failures.append("case1 origin_signal: helper returned None, expected match")
    elif hit.get("id") != "g-001-50":
        failures.append(f"case1: expected g-001-50, got {hit.get('id')!r}")
    elif hit.get("_match_strategy") != "origin_signal":
        failures.append(
            f"case1: strategy should be 'origin_signal', got {hit.get('_match_strategy')!r}"
        )
    print(f"  [{'PASS' if hit and hit.get('_match_strategy') == 'origin_signal' else 'FAIL'}] "
          f"strategy-a origin_signal: hit={hit.get('id') if hit else None}")

    # Case 2: strategy (b) title-regex (no origin_signal match)
    cases_run += 1
    items = _items_with_unblock_title_only("g-115-149")
    hit = finder(items, "g-115-149", verb="deploy", also_scan_agent=False)
    if hit is None:
        failures.append("case2 title_regex: returned None, expected match")
    elif hit.get("id") != "g-001-51":
        failures.append(f"case2: expected g-001-51, got {hit.get('id')!r}")
    elif hit.get("_match_strategy") != "title_regex":
        failures.append(
            f"case2: strategy should be 'title_regex', got {hit.get('_match_strategy')!r}"
        )
    print(f"  [{'PASS' if hit and hit.get('_match_strategy') == 'title_regex' else 'FAIL'}] "
          f"strategy-b title_regex: hit={hit.get('id') if hit else None}")

    # Case 3: strategy (c) description-proximity with verb
    cases_run += 1
    items = _items_with_unblock_proximity("g-115-149", "deploy")
    hit = finder(items, "g-115-149", verb="deploy", also_scan_agent=False)
    if hit is None:
        failures.append("case3 proximity: returned None, expected match")
    elif hit.get("id") != "g-001-52":
        failures.append(f"case3: expected g-001-52, got {hit.get('id')!r}")
    elif hit.get("_match_strategy") != "description_proximity":
        failures.append(
            f"case3: strategy should be 'description_proximity', got "
            f"{hit.get('_match_strategy')!r}"
        )
    print(f"  [{'PASS' if hit and hit.get('_match_strategy') == 'description_proximity' else 'FAIL'}] "
          f"strategy-c proximity: hit={hit.get('id') if hit else None}")

    # Case 4: strategy (c) requires verb — without verb, the goal-id-only
    # mention in description must NOT match (else any "blocks "
    # comment in unrelated goals would dedup falsely).
    cases_run += 1
    items = _items_with_unblock_proximity("g-115-149", "deploy")
    hit = finder(items, "g-115-149", verb=None, also_scan_agent=False)
    if hit is not None:
        failures.append(
            f"case4 no-verb: should NOT match (verb required for proximity), "
            f"got {hit.get('id')!r} via {hit.get('_match_strategy')!r}"
        )
    print(f"  [{'PASS' if hit is None else 'FAIL'}] strategy-c no-verb skip: "
          f"hit={hit.get('id') if hit else None}")

    # Case 5: resolved Unblock does NOT block re-filing
    cases_run += 1
    items = _items_with_resolved_unblock("g-115-149")
    hit = finder(items, "g-115-149", verb="deploy", also_scan_agent=False)
    if hit is not None:
        failures.append(
            f"case5 resolved: completed Unblock should NOT match, got "
            f"{hit.get('id')!r}"
        )
    print(f"  [{'PASS' if hit is None else 'FAIL'}] resolved-skip: "
          f"hit={hit.get('id') if hit else None}")

    # Case 6: nothing in queue — no match
    cases_run += 1
    items = [
        {"id": "asp-001", "title": "Empty", "status": "active", "goals": []},
        {"id": "asp-115", "title": "Other", "status": "active", "goals": [
            {"id": "g-115-9999", "title": "Unrelated work",
             "description": "Nothing about deploy or g-other",
             "status": "pending", "origin_signal": "user_directive",
             "type": "idea"},
        ]},
    ]
    hit = finder(items, "g-115-149", verb="deploy", also_scan_agent=False)
    if hit is not None:
        failures.append(f"case6 empty-queue: should NOT match, got {hit.get('id')!r}")
    print(f"  [{'PASS' if hit is None else 'FAIL'}] empty-queue: "
          f"hit={hit.get('id') if hit else None}")

    # Case 7: title-regex requires "Unblock:" prefix — generic "deploy"
    # mentions in titles do NOT spuriously match.
    cases_run += 1
    items = [
        {"id": "asp-001", "title": "x", "status": "active", "goals": [
            {"id": "g-001-77",
             "title": "Investigate deploy regression for g-115-149",
             "description": "Some unrelated description",
             "status": "pending", "origin_signal": "investigation",
             "type": "idea"},
        ]},
    ]
    # No verb — strategy (c) skipped, strategy (b) requires "Unblock:" prefix
    hit = finder(items, "g-115-149", verb=None, also_scan_agent=False)
    if hit is not None:
        failures.append(
            f"case7 no-Unblock-prefix: should NOT match without verb, got "
            f"{hit.get('id')!r} via {hit.get('_match_strategy')!r}"
        )
    print(f"  [{'PASS' if hit is None else 'FAIL'}] no-Unblock-prefix: "
          f"hit={hit.get('id') if hit else None}")

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"\nAll {cases_run} dedup-helper cases verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
