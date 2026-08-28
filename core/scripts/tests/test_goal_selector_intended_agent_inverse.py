"""test_goal_selector_intended_agent_inverse.py —  regression.

Verifies the intended_agent select-time filter has a collect_blocked INVERSE.

Census g-115-3651 (hypothesis 2026-07-28_more-queue-filters-lack-blocked-inverse,
CONFIRMED) identified intended_agent as the ONLY PERMANENT drop in
collect_candidates with no complementary blocked classification. A goal routed to
a peer vanished from the ranked candidate list AND from the blocked list — no
agent and no audit could see it (guard-1698; two goals sat unreachable 71 days
in the sibling g-115-3482 incident).

The SYMMETRY invariant this file guards: a goal is a candidate XOR
routed_to_agent-blocked, never both and never neither. The candidate side
ESCAPES (surfaces the goal) only when ALL THREE hold — owner idle, goal
unclaimed, goal not owner-scoped — so the blocked side must fire unless all
three hold.

Deliberately NOT covered: cooldown/cadence (Class 1) and claimed_by/abstained_by
(Class 2) filters. The census found those are correctly inverse-free — they
self-heal via timeouts. Adding inverses there is explicitly the wrong fix.

Pattern: direct module import, synthetic aspiration fixtures. Mirrors
test_goal_selector_fire_when.py and test_goal_selector_world_source_derivation.py.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# goal-selector.py needs MIND_AGENT to load (AGENT_DIR derivation) and reads
# module-level AGENT_NAME at call time for the routing filter. Capture-restore
# around the mutation so env pollution cannot leak (rb-1096, guard-588).
_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "bravo")

gs = importlib.import_module("goal-selector")

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT

SELF_AGENT = gs.AGENT_NAME

# OTHER_AGENT must name a LIVE peer — an off-roster name names nobody who can
# honor the routing and correctly falls THROUGH (), which is a
# different case, asserted separately below. Derive from the live vocabulary so
# a future retirement cannot silently convert one case into the other.
from aspirations import _valid_intended_agents as _vocab  # noqa: E402

_LIVE_PEERS = sorted(n for n in _vocab() if n not in (SELF_AGENT, "either"))
OTHER_AGENT = _LIVE_PEERS[0] if _LIVE_PEERS else "alpha"
OFFROSTER_AGENT = "delta"  # retired 2026-07-07

# Module-level names a caller may reference without a local binding.
MODULE_LEVEL_NAMES = {n for n in dir(gs) if not n.startswith("__")}


def _asp(goal_overrides: dict) -> list[dict]:
    base = {
        "id": "g-test-01",
        "title": "Test goal",
        "status": "pending",
        "priority": "MEDIUM",
        "skill": None,
        "category": "test",
    }
    base.update(goal_overrides)
    return [{
        "id": "asp-test",
        "title": "Test aspiration",
        "status": "active",
        "scope": "sprint",
        "goals": [base],
    }]


def _cand_ids(seq) -> set:
    return {c["goal"]["id"] for c in seq}


def _blocked_ids(seq) -> set:
    return {e["goal_id"] for e in seq}


def _entry(blocked, goal_id):
    for e in blocked:
        if e["goal_id"] == goal_id:
            return e
    return None


def check_call_site_kwargs_are_bound() -> list[str]:
    """Every kwarg NAME passed to collect_blocked must be bound in its caller.

    Regression for a defect introduced while adding the routed_to_agent inverse
    (g-115-3679): `reallocation_hours=reallocation_hours` was added to BOTH
    collect_blocked call sites, but the two callers are different functions --
    cmd_select binds reallocation_hours from config, cmd_blocked did not. Scope
    was inferred from LINE ORDER (the binding sits at a lower line number than
    both call sites) rather than from function boundaries, so `goal-selector.sh
    blocked` died with NameError on every invocation.

    Nothing in the suite caught it: every existing test calls collect_blocked()
    directly and none exercises the cmd_blocked CLI path. A smoke test of that
    one command would pin this instance; this checks the whole class instead,
    for both call sites and every kwarg, so the next threaded parameter cannot
    repeat it.
    """
    import ast

    failures: list[str] = []
    tree = ast.parse((CORE_SCRIPTS / "goal-selector.py").read_text(encoding="utf-8"))

    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        # Names bound anywhere in this function: params, assignments, for/with
        # targets, comprehensions, imports. Globals resolve at module level, so
        # only flag a name that is neither local nor module-level.
        bound = {a.arg for a in fn.args.args}
        bound |= {a.arg for a in fn.args.kwonlyargs}
        if fn.args.vararg:
            bound.add(fn.args.vararg.arg)
        if fn.args.kwarg:
            bound.add(fn.args.kwarg.arg)
        for node in ast.walk(fn):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                bound.add(node.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for al in node.names:
                    bound.add((al.asname or al.name).split(".")[0])

        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "collect_blocked"):
                continue
            for kw in node.keywords:
                if kw.arg is None or not isinstance(kw.value, ast.Name):
                    continue
                name = kw.value.id
                if name in bound or name in MODULE_LEVEL_NAMES:
                    continue
                failures.append(
                    f"[FAIL] call-site-kwargs: {fn.name}() passes "
                    f"{kw.arg}={name} to collect_blocked at line {node.lineno}, "
                    f"but {name} is not bound in {fn.name}() or at module level "
                    "-- NameError at runtime (g-115-3679)")
    if not failures:
        print("  [PASS] call-site-kwargs: every collect_blocked kwarg is bound in its caller")
    return failures


def run() -> list[str]:
    failures: list[str] = []

    # ── Case 1 — THE FIX. Routed to a live peer, owner NOT idle (default
    # reallocation_hours=None -> empty idle set): dropped from candidates AND
    # now classified blocked. This is guard-1698's plant-and-assert.
    asps = _asp({"intended_agent": OTHER_AGENT})
    cands = gs.collect_candidates(asps, source="world")
    blocked = gs.collect_blocked(asps)
    if "g-test-01" in _cand_ids(cands):
        failures.append("[FAIL] routed-away: goal should NOT be a candidate")
    if "g-test-01" not in _blocked_ids(blocked):
        failures.append(
            "[FAIL] routed-away: goal missing from blocked — the PERMANENT "
            "drop is back (invisible in both directions, guard-1698)")
    else:
        e = _entry(blocked, "g-test-01")
        if e.get("block_reason") != "routed_to_agent":
            failures.append(
                f"[FAIL] routed-away: block_reason={e.get('block_reason')!r}, "
                "expected 'routed_to_agent'")
        elif e.get("intended_agent") != OTHER_AGENT:
            # guard-1362: routing fields must reach the consuming LLM.
            failures.append(
                "[FAIL] routed-away: entry omits intended_agent (guard-1362)")
        elif not isinstance(e.get("blocker_ref"), dict):
            # quiescence C2 requires a DICT ref or the queue fails C2 -> B7 churn.
            failures.append(
                "[FAIL] routed-away: blocker_ref is not a dict (quiescence C2)")
        else:
            print("  [PASS] routed-away: blocked, reason=routed_to_agent, ref synthesized")

    # ── Case 2 — guard-3644. The escape is an AND of three conditions; the
    # detail must name EVERY unmet conjunct, not just the first one checked.
    asps = _asp({"intended_agent": OTHER_AGENT, "claimed_by": OTHER_AGENT})
    blocked = gs.collect_blocked(asps)
    e = _entry(blocked, "g-test-01")
    if e is None:
        failures.append("[FAIL] all-conjuncts: goal missing from blocked")
    else:
        unmet = e.get("unmet_escape_conditions") or []
        detail = e.get("block_detail", "")
        if len(unmet) < 2:
            failures.append(
                f"[FAIL] all-conjuncts: only {unmet} reported; both 'owner not "
                "idle' AND 'claimed by' are unmet (guard-3644)")
        elif "claimed by" not in detail or "owner not idle" not in detail:
            failures.append(
                f"[FAIL] all-conjuncts: detail names one conjunct only: {detail!r}")
        else:
            print("  [PASS] all-conjuncts: detail names every unmet escape condition")

    # ── Case 3 — XOR. When ALL THREE escape conditions hold, collect_candidates
    # surfaces the goal, so collect_blocked must NOT claim it. Patch the shared
    # idle-agent accessor so the case is deterministic (no team-state dependency).
    _real_idle = gs._get_idle_agents
    try:
        gs._get_idle_agents = lambda _h: {OTHER_AGENT}
        asps = _asp({"intended_agent": OTHER_AGENT})  # unclaimed, not owner-scoped
        cands = gs.collect_candidates(asps, source="world", reallocation_hours=1.0)
        blocked = gs.collect_blocked(asps, reallocation_hours=1.0)
        in_c = "g-test-01" in _cand_ids(cands)
        in_b = "g-test-01" in _blocked_ids(blocked)
        if in_c and in_b:
            failures.append(
                "[FAIL] xor: goal is BOTH candidate and blocked — the inverse "
                "is not the logical complement")
        elif not in_c:
            failures.append(
                "[FAIL] xor: idle-owner reallocation escape did not surface the goal")
        else:
            print("  [PASS] xor: idle-owner escape -> candidate, not blocked")
    finally:
        gs._get_idle_agents = _real_idle

    # ── Case 4 — no false positives. Self-routed, "either", and unrouted goals
    # are ordinary candidates and must never be routed_to_agent-blocked.
    for label, val in (("self", SELF_AGENT), ("either", "either"), ("none", None)):
        asps = _asp({"intended_agent": val})
        blocked = gs.collect_blocked(asps)
        e = _entry(blocked, "g-test-01")
        if e is not None and e.get("block_reason") == "routed_to_agent":
            failures.append(
                f"[FAIL] no-false-positive[{label}]: intended_agent={val!r} "
                "must not be routed_to_agent-blocked")
        else:
            print(f"  [PASS] no-false-positive[{label}]: not routed-blocked")

    # ── Case 5 —  preserved. An OFF-ROSTER target names nobody who
    # can honor the routing, so it falls THROUGH to visible. The new inverse
    # must not resurrect the vanish by blocking it instead.
    if not _LIVE_PEERS:
        print("  [SKIP] off-roster: roster unresolvable (vocabulary check disabled)")
    else:
        asps = _asp({"intended_agent": OFFROSTER_AGENT})
        cands = gs.collect_candidates(asps, source="world")
        blocked = gs.collect_blocked(asps)
        e = _entry(blocked, "g-test-01")
        if e is not None and e.get("block_reason") == "routed_to_agent":
            failures.append(
                "[FAIL] off-roster: blocked instead of falling through — "
                "g-115-3482 regression")
        elif "g-test-01" not in _cand_ids(cands):
            failures.append("[FAIL] off-roster: goal should stay visible")
        else:
            print("  [PASS] off-roster: falls through to candidates, not blocked")

    failures.extend(check_call_site_kwargs_are_bound())

    return failures


if __name__ == "__main__":
    print("test_goal_selector_intended_agent_inverse (g-115-3679)")
    print(f"  self={SELF_AGENT!r} peer={OTHER_AGENT!r} live_peers={_LIVE_PEERS}")
    fails = run()
    if fails:
        print()
        for f in fails:
            print(f)
        print(f"\n{len(fails)} failure(s)")
        sys.exit(1)
    print("\nAll cases passed.")
