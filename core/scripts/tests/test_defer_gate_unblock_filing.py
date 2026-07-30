"""test_defer_gate_unblock_filing.py — regression test for .

Asserts that aspirations.py's defer-time gate, when capability-gate returns
would_block=True with unblock_suggested fields populated, atomically files
an Unblock goal into asp-001 BEFORE refusing the defer write. Tests the
in-process helper `_file_unblock_under_existing_lock` directly with
synthetic items + gate_result so no real aspirations.jsonl is touched.

Verification outcomes covered (g-257-03; cases 3, 6, 7 updated by g-115-334 / rb-655):
  1. Helper appends Unblock goal to asp-001 when gate.unblock_suggested=true
  2. Filed goal has origin_signal=unblock:<original-goal-id>
  3. Idempotency: existing pending Unblock with the same origin_signal blocks
     a duplicate file (returns "existing ... idempotent skip" message)
  4. Filed goal carries title/description from gate_result fields
  5. Missing asp-001 + no original_asp + active asps present → strategy (c)
     "first-active-asp" routes Unblock to first active aspiration (rb-655
     three-strategy fallback; previously this case was a silent failure)
  6. Auto-id generates next g-001-NN slot
  7. Strategy (b): no asp-001 BUT original_asp passed → routes Unblock to
     the original goal's parent aspiration (rb-655)
  8. Truly no target available: no asp-001, no original_asp, no active asps
     → returns (None, "no target aspiration available" message)

Pattern: same importlib + sys.path shape as test_capability_gate_narrative.py.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))


def _import_aspirations_helper():
    """Load aspirations.py via importlib (hyphen-free attribute name)."""
    spec = importlib.util.spec_from_file_location(
        "aspirations_mod", CORE_SCRIPTS / "aspirations.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load spec for aspirations.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_synthetic_items() -> list:
    """Return a minimal items list with asp-001 + asp-115 + sample goals."""
    return [
        {
            "id": "asp-001",
            "title": "Recurring meta-cadence",
            "status": "active",
            "goals": [
                {"id": "g-001-01", "title": "Reflect and journal",
                 "status": "pending", "category": "meta", "type": "recurring"},
                {"id": "g-001-05", "title": "Hippocampal replay",
                 "status": "pending", "category": "meta", "type": "recurring"},
            ],
        },
        {
            "id": "asp-115",
            "title": "Framework health",
            "status": "active",
            "goals": [
                {"id": "g-115-149", "title": "Some original goal",
                 "status": "pending", "category": "framework-maintenance",
                 "type": "idea"},
            ],
        },
    ]


def _gate_result_with_unblock(verb: str = "deploy",
                              for_goal: str = "g-115-149") -> dict:
    """Synthetic gate_result mirroring capability-gate.py output when
    --suggest-unblock + --for-goal-id are passed and would_block=true."""
    return {
        "would_block": True,
        "unblock_suggested": True,
        "unblock_title": f"Unblock: {verb} for {for_goal}",
        "unblock_description": (
            f"Capability gate matched 'sample' against forged-skills.yaml: "
            f"some-skill. Action required: '{verb}'. "
            f"Invoke the matched capability."
        ),
        "matched_capability": {
            "source": "forged-skills.yaml",
            "skill": "some-skill",
            "matched_keyword": "sample",
        },
        "matches": [{"skill": "some-skill", "matched_keyword": "sample",
                     "source": "forged-skills.yaml"}],
        "match_count": 1,
        "narrative_framing_detected": False,
    }


def main() -> int:
    failures = []
    cases_run = 0

    mod = _import_aspirations_helper()
    helper = mod._file_unblock_under_existing_lock

    # Case 1: clean filing — no existing Unblock, asp-001 present
    cases_run += 1
    items = _make_synthetic_items()
    pre_count = len(items[0]["goals"])  # asp-001 has 2 goals initially
    gate = _gate_result_with_unblock("deploy", "g-115-149")
    filed_id, status = helper(items, "g-115-149", gate)
    post_count = len(items[0]["goals"])
    if filed_id is None:
        failures.append(f"case1: expected filed_id, got None (status: {status})")
    elif not filed_id.startswith("g-001-"):
        failures.append(f"case1: filed_id should start with 'g-001-', got {filed_id!r}")
    if post_count != pre_count + 1:
        failures.append(
            f"case1: asp-001 should gain exactly 1 goal, went {pre_count} → {post_count}"
        )
    if "Filed Unblock goal" not in status:
        failures.append(f"case1: status should announce filing, got {status!r}")
    new_goal = items[0]["goals"][-1]
    if new_goal.get("origin_signal") != "unblock:g-115-149":
        failures.append(
            f"case1: origin_signal should be 'unblock:g-115-149', got "
            f"{new_goal.get('origin_signal')!r}"
        )
    if "deploy" not in (new_goal.get("title") or ""):
        failures.append(
            f"case1: filed title should contain 'deploy', got {new_goal.get('title')!r}"
        )
    if new_goal.get("priority") != "HIGH":
        failures.append(f"case1: priority should be HIGH, got {new_goal.get('priority')!r}")
    if new_goal.get("participants") != ["agent"]:
        failures.append(
            f"case1: participants should be ['agent'], got {new_goal.get('participants')!r}"
        )
    print(f"  [{'PASS' if filed_id and 'deploy' in (new_goal.get('title') or '') else 'FAIL'}] "
          f"clean-filing: filed={filed_id} title={new_goal.get('title')!r}")

    # Case 2: idempotency — existing pending Unblock with matching origin_signal
    # blocks a duplicate file. Re-run on the same items; should NOT add another.
    cases_run += 1
    items_dup = _make_synthetic_items()
    items_dup[0]["goals"].append({
        "id": "g-001-99",
        "title": "Unblock: deploy for g-115-149",
        "description": "Pre-existing pending Unblock",
        "status": "pending",
        "origin_signal": "unblock:g-115-149",
        "type": "idea",
        "category": "framework-maintenance",
    })
    pre_count2 = len(items_dup[0]["goals"])
    filed_id2, status2 = helper(items_dup, "g-115-149", gate)
    post_count2 = len(items_dup[0]["goals"])
    if filed_id2 is not None:
        failures.append(f"case2 idempotency: expected filed_id=None, got {filed_id2!r}")
    if "idempotent skip" not in status2 and "existing Unblock" not in status2:
        failures.append(
            f"case2: status should announce idempotent skip, got {status2!r}"
        )
    if post_count2 != pre_count2:
        failures.append(
            f"case2: asp-001 goal count should be unchanged, went {pre_count2} → {post_count2}"
        )
    print(f"  [{'PASS' if filed_id2 is None and post_count2 == pre_count2 else 'FAIL'}] "
          f"idempotency: status={status2!r}")

    # Case 3: missing asp-001 → strategy (c) "first-active-asp" fires (rb-655)
    # Previously this was a silent-failure path (filed_id=None,
    # "target aspiration asp-001 not found"). After the rb-655 three-strategy
    # fallback, when asp-001 is absent AND original_asp is not provided AND
    # at least one active aspiration exists, the Unblock routes to the first
    # active aspiration so it surfaces somewhere readable instead of vanishing.
    cases_run += 1
    items_no_asp1 = [items[1]]  # only asp-115, no asp-001
    pre_count3 = len(items_no_asp1[0]["goals"])
    filed_id3, status3 = helper(items_no_asp1, "g-115-149", gate)
    post_count3 = len(items_no_asp1[0]["goals"])
    if filed_id3 is None:
        failures.append(
            f"case3 missing-asp-001: expected filed_id (strategy-c fallback), "
            f"got None (status: {status3!r})"
        )
    if not filed_id3 or not filed_id3.startswith("g-115-"):
        failures.append(
            f"case3: filed_id should start with 'g-115-' (routed to asp-115), "
            f"got {filed_id3!r}"
        )
    if "asp-115" not in status3:
        failures.append(
            f"case3: status should mention asp-115 (strategy-c routing target), "
            f"got {status3!r}"
        )
    if post_count3 != pre_count3 + 1:
        failures.append(
            f"case3: asp-115 should gain exactly 1 goal (rb-655 fallback), "
            f"went {pre_count3} → {post_count3}"
        )
    ok3 = (filed_id3 is not None and filed_id3.startswith("g-115-")
           and "asp-115" in status3 and post_count3 == pre_count3 + 1)
    print(f"  [{'PASS' if ok3 else 'FAIL'}] missing-asp-001-strategy-c: "
          f"filed={filed_id3!r}")

    # Case 4: auto-id generates next g-001-NN — bumps highest existing seq
    cases_run += 1
    items_high = _make_synthetic_items()
    items_high[0]["goals"].append({
        "id": "g-001-42",  # highest existing seq
        "title": "Some unrelated goal",
        "status": "completed",
        "type": "idea",
    })
    filed_id4, _ = helper(items_high, "g-test-id-1", gate)
    if filed_id4 != "g-001-43":
        failures.append(
            f"case4 auto-id: expected g-001-43 (after max=42), got {filed_id4!r}"
        )
    print(f"  [{'PASS' if filed_id4 == 'g-001-43' else 'FAIL'}] auto-id: "
          f"new_id={filed_id4!r}")

    # Case 5: gate without unblock_title falls back to a default
    cases_run += 1
    items5 = _make_synthetic_items()
    minimal_gate = {
        "would_block": True,
        "unblock_suggested": True,
        # No unblock_title, no unblock_description
    }
    filed_id5, status5 = helper(items5, "g-115-200", minimal_gate)
    if filed_id5 is None:
        failures.append(
            f"case5 fallback: should still file with default title/desc, got "
            f"None (status: {status5})"
        )
    else:
        new5 = items5[0]["goals"][-1]
        if "g-115-200" not in (new5.get("title") or ""):
            failures.append(
                f"case5: fallback title should mention original goal, got "
                f"{new5.get('title')!r}"
            )
    print(f"  [{'PASS' if filed_id5 else 'FAIL'}] fallback-title: filed={filed_id5}")

    # Case 6: rb-655 strategy (b) — original_asp routes Unblock to parent
    # When asp-001 is absent BUT the caller passes original_asp, strategy (b)
    # fires before strategy (c). The Unblock lands on the original goal's
    # parent aspiration, not a random first-active-asp pick. This is the
    # primary cross-source fix path: cmd_update_goal at the call site
    # always has the parent asp object in scope and now passes it through.
    #
    # Uses a fresh fixture (not items[1] from earlier cases — those mutate
    # asp-115's goals list and would trip the dedup helper for case 6's
    # synthetic goal id ) AND a unique goal id  so the
    # dedup scan against any leftover Unblocks misses cleanly.
    cases_run += 1
    fresh_items = _make_synthetic_items()
    items_strat_b = [fresh_items[1]]  # only asp-115 — fresh, no mutations
    parent_asp = items_strat_b[0]
    pre_count6 = len(parent_asp["goals"])
    gate6 = _gate_result_with_unblock("deploy", "g-115-300")
    filed_id6, status6 = helper(items_strat_b, "g-115-300", gate6,
                                original_asp=parent_asp)
    post_count6 = len(parent_asp["goals"])
    if filed_id6 is None:
        failures.append(
            f"case6 strategy-b: expected filed_id (parent-asp fallback), "
            f"got None (status: {status6!r})"
        )
    if filed_id6 and not filed_id6.startswith("g-115-"):
        failures.append(
            f"case6: filed_id should start with 'g-115-' (asp-115 is parent), "
            f"got {filed_id6!r}"
        )
    if post_count6 != pre_count6 + 1:
        failures.append(
            f"case6: asp-115 should gain exactly 1 goal, went "
            f"{pre_count6} → {post_count6}"
        )
    ok6 = (filed_id6 is not None and filed_id6.startswith("g-115-")
           and post_count6 == pre_count6 + 1)
    print(f"  [{'PASS' if ok6 else 'FAIL'}] strategy-b-parent-asp: "
          f"filed={filed_id6!r}")

    # Case 7: truly-no-target — no asp-001, no original_asp, no active asps
    # When all three strategies fail, the helper returns a clean error tuple
    # rather than crashing. This is the only remaining "filing skipped" path
    # after rb-655; it is unreachable in production because the call site
    # always passes original_asp from a found goal (which means its parent
    # asp exists in items) — but the defensive return preserves a graceful
    # exit if a future refactor breaks that invariant.
    cases_run += 1
    items_empty = [
        {"id": "asp-paused-only", "title": "Paused asp",
         "status": "paused", "goals": []},
    ]
    filed_id7, status7 = helper(items_empty, "g-115-149", gate)
    if filed_id7 is not None:
        failures.append(
            f"case7 truly-no-target: expected filed_id=None, got {filed_id7!r}"
        )
    if "no target aspiration available" not in status7:
        failures.append(
            f"case7: status should announce 'no target aspiration available', "
            f"got {status7!r}"
        )
    ok7 = (filed_id7 is None
           and "no target aspiration available" in status7)
    print(f"  [{'PASS' if ok7 else 'FAIL'}] truly-no-target: status={status7!r}")

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"\nAll {cases_run} defer-gate-unblock-filing cases verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
