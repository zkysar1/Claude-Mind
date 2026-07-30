"""test_goal_selector_cross_agent_pull.py —  regression.

Exercises collect_cross_agent_candidates — the helper added to goal-selector.py
to fix cross-agent goal stranding. When the capability-route gate stamps
intended_agent on a newly-filed goal, the goal lands in the FILER's private
queue but was previously invisible to the routed-to TARGET. This helper adds
a third read pass over sibling agent dirs at selection time.

Tested invariants (from g-115-946 verification.checks):
  1. Sibling-routed goal appears in selector candidates for the target agent
  2. Own-queue goals do not double-count (sib_dir == agent_dir is skipped)
  3. Unreadable sibling file fails open (skip, do not abort)
  4. Strict-match contract — only intended_agent == AGENT_NAME pulls
     ("either", None, and other-agent values do NOT)

Pattern: build a temp project_root with sibling dirs, each with a synthetic
aspirations.jsonl. Import collect_cross_agent_candidates and call directly.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# goal-selector.py requires MIND_AGENT to load (paths derive AGENT_DIR).
# Capture-restore around the module-level mutation so collection-time env
# pollution cannot leak to other tests (Layer 1 test-pollution defense —
# rb-1096, guard-588, tree node test-pollution-defense). Tests pass
# agent_name explicitly to the helper, so any binding works here.
_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "bravo")

gs = importlib.import_module("goal-selector")
collect_cross_agent_candidates = gs.collect_cross_agent_candidates
score_goal = gs.score_goal  # : emission-side test target

# Live intended_agent vocabulary — used to derive a peer name rather than
# hardcoding one that a future retirement would silently rot (guard-1699).
from aspirations import _valid_intended_agents as _vocab  # noqa: E402

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT


def _write_aspirations(agent_dir: Path, aspirations: list[dict]) -> None:
    """Write aspirations.jsonl with one record per aspiration."""
    agent_dir.mkdir(parents=True, exist_ok=True)
    asp_path = agent_dir / "aspirations.jsonl"
    with asp_path.open("w", encoding="utf-8") as fh:
        for asp in aspirations:
            fh.write(json.dumps(asp) + "\n")


def _make_goal(goal_id: str, intended_agent: str | None,
               status: str = "pending") -> dict:
    """Minimal goal record passing all eligibility filters."""
    g = {
        "id": goal_id,
        "title": f"test goal {goal_id}",
        "status": status,
        "priority": "MEDIUM",
        "category": "framework-architecture",
        "participants": ["agent"],
        "recurring": False,
    }
    if intended_agent is not None:
        g["intended_agent"] = intended_agent
    return g


def _make_aspiration(asp_id: str, goals: list[dict]) -> dict:
    return {
        "id": asp_id,
        "title": f"test aspiration {asp_id}",
        "status": "active",
        "goals": goals,
        "priority": "MEDIUM",
    }


# goal-selector.py uses module-level AGENT_NAME for the intended_agent filter
# inside collect_candidates. Production calls collect_cross_agent_candidates
# with agent_name == AGENT_NAME — tests follow the same contract by routing
# goals to AGENT_NAME (set via MIND_AGENT env at module import above).
TARGET_AGENT = gs.AGENT_NAME  # "bravo" per the os.environ.setdefault above


def case_sibling_routed_goal_appears() -> tuple[bool, str]:
    """Goal in foo/ routed to TARGET_AGENT appears in target's candidates."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # Phase 2.5.D: agent dirs live under PROJECT_ROOT/agents/.
        foo_dir = root / "agents" / "foo"
        target_dir = root / "agents" / TARGET_AGENT
        _write_aspirations(foo_dir, [
            _make_aspiration("asp-test", [
                _make_goal("g-test-01", intended_agent=TARGET_AGENT),
            ]),
        ])
        _write_aspirations(target_dir, [])  # own queue is empty

        results = collect_cross_agent_candidates(root, target_dir, TARGET_AGENT)

        if len(results) != 1:
            return False, f"expected 1 result, got {len(results)}"
        c = results[0]
        if c.get("goal", {}).get("id") != "g-test-01":
            return False, f"wrong goal_id: {c.get('goal', {}).get('id')}"
        if c.get("source") != "cross-agent:foo":
            return False, f"wrong source: {c.get('source')!r}"
        return True, "ok"


def case_own_queue_not_double_counted() -> tuple[bool, str]:
    """Own dir (sib_dir == agent_dir) is skipped — no duplicates from same dir."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # Phase 2.5.D: agent dirs live under PROJECT_ROOT/agents/.
        foo_dir = root / "agents" / "foo"
        target_dir = root / "agents" / TARGET_AGENT
        # target's own queue has a self-routed goal
        _write_aspirations(target_dir, [
            _make_aspiration("asp-test", [
                _make_goal("g-own-01", intended_agent=TARGET_AGENT),
            ]),
        ])
        # foo has a target-routed goal
        _write_aspirations(foo_dir, [
            _make_aspiration("asp-test", [
                _make_goal("g-foo-01", intended_agent=TARGET_AGENT),
            ]),
        ])

        results = collect_cross_agent_candidates(root, target_dir, TARGET_AGENT)

        ids = sorted(c.get("goal", {}).get("id") for c in results)
        if ids != ["g-foo-01"]:
            return False, f"expected only g-foo-01, got {ids}"
        if results[0].get("source") != "cross-agent:foo":
            return False, f"wrong source: {results[0].get('source')!r}"
        return True, "ok"


def case_unreadable_sibling_fails_open() -> tuple[bool, str]:
    """Corrupt aspirations.jsonl in one sibling does not abort the sweep."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # Phase 2.5.D: agent dirs live under PROJECT_ROOT/agents/.
        foo_dir = root / "agents" / "foo"
        target_dir = root / "agents" / TARGET_AGENT
        baz_dir = root / "agents" / "baz"

        # foo: corrupt jsonl (binary garbage)
        foo_dir.mkdir(parents=True)
        (foo_dir / "aspirations.jsonl").write_bytes(b"\xff\xfe\xfd not json {{{ [[[ ")

        # baz: valid goal routed to target
        _write_aspirations(baz_dir, [
            _make_aspiration("asp-test", [
                _make_goal("g-baz-01", intended_agent=TARGET_AGENT),
            ]),
        ])

        # target: empty
        _write_aspirations(target_dir, [])

        try:
            results = collect_cross_agent_candidates(root, target_dir, TARGET_AGENT)
        except Exception as e:
            return False, f"raised {type(e).__name__}: {e}"

        ids = sorted(c.get("goal", {}).get("id") for c in results)
        if ids != ["g-baz-01"]:
            return False, f"expected only g-baz-01 (foo skipped), got {ids}"
        return True, "ok"


def case_strict_match_contract() -> tuple[bool, str]:
    """Only intended_agent == TARGET_AGENT pulls — 'either', None, other are filtered."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # Phase 2.5.D: agent dirs live under PROJECT_ROOT/agents/.
        foo_dir = root / "agents" / "foo"
        target_dir = root / "agents" / TARGET_AGENT
        # pick a different agent name for the "other" case so it doesn't match.
        # NOT hardcoded (guard-1699): this used `"delta" if TARGET_AGENT !=
        # "delta" else "alpha"`, and delta was RETIRED 2026-07-07 -- so the
        # fixture had quietly stopped meaning "another agent" and started
        # meaning "a nonexistent agent". Harmless HERE, because
        # collect_cross_agent_candidates enforces a strict-match contract
        # (intended_agent == agent_name) and never consults the roster -- but
        # the identical idiom in test_goal_selector_world_source_derivation.py
        # DID rot into a false regression once  split the two cases.
        # Derive from the live vocabulary so it cannot rot again.
        other_name = next((n for n in sorted(_vocab())
                           if n not in (TARGET_AGENT, "either")), "alpha")
        _write_aspirations(foo_dir, [
            _make_aspiration("asp-test", [
                _make_goal("g-match",  intended_agent=TARGET_AGENT),  # included
                _make_goal("g-either", intended_agent="either"),       # excluded
                _make_goal("g-none",   intended_agent=None),           # excluded
                _make_goal("g-other",  intended_agent=other_name),     # excluded
            ]),
        ])
        _write_aspirations(target_dir, [])

        results = collect_cross_agent_candidates(root, target_dir, TARGET_AGENT)

        ids = sorted(c.get("goal", {}).get("id") for c in results)
        if ids != ["g-match"]:
            return False, f"strict-match broke: got {ids}, expected ['g-match']"
        return True, "ok"


def case_non_agent_dir_skipped() -> tuple[bool, str]:
    """Sibling dirs without aspirations.jsonl (world/, core/, tests/) are skipped."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target_dir = root / "agents" / TARGET_AGENT
        # Phase 2.5.D: non-agent siblings live under agents/ alongside real agents.
        non_agent_1 = root / "agents" / "world-fake"
        non_agent_2 = root / "agents" / "tests-fake"
        non_agent_3 = root / "agents" / "__pycache__"
        for d in (non_agent_1, non_agent_2, non_agent_3):
            d.mkdir(parents=True)
        # target exists too (empty)
        _write_aspirations(target_dir, [])

        try:
            results = collect_cross_agent_candidates(root, target_dir, TARGET_AGENT)
        except Exception as e:
            return False, f"raised {type(e).__name__}: {e}"

        if results != []:
            return False, f"expected empty, got {len(results)} results"
        return True, "ok"


def case_invalid_inputs_fail_open() -> tuple[bool, str]:
    """None/empty agent_name or None project_root/agent_dir returns [] without raising."""
    target = Path("/nonexistent/target")
    cases = [
        (None, target, TARGET_AGENT),
        (Path("/nonexistent"), None, TARGET_AGENT),
        (Path("/nonexistent"), target, ""),
        (Path("/nonexistent"), target, None),
    ]
    for pr, ad, an in cases:
        try:
            r = collect_cross_agent_candidates(pr, ad, an)
        except Exception as e:
            return False, f"raised on inputs ({pr}, {ad}, {an!r}): {type(e).__name__}: {e}"
        if r != []:
            return False, f"expected [] on guard input, got {r}"
    return True, "ok"


def case_callsite_shape_project_root_is_agents_parent() -> tuple[bool, str]:
    """REGRESSION : the WIRED call site (goal-selector.py cmd_select)
    passes AGENT_DIR.parent — the agents-parent (PROJECT_ROOT/agents), NOT
    PROJECT_ROOT — as the project_root arg. The helper MUST still surface
    sibling-routed goals from that arg shape.

    Pre-fix the helper did `project_root / "agents"`, so this shape computed
    <agents-parent>/agents (nonexistent) -> the is_dir() guard returned [] on
    EVERY real call, leaving g-115-946's cross-agent stranding fix INERT from
    the Phase 2.5.D relocation onward (empirically: alpha's live call returned 0
    while a corrected call surfaced g-001-282). The other cases here all pass the
    CONTRACT-correct project_root (=PROJECT_ROOT), so they never exercised the
    call-site shape and the inert fix passed its own regression suite. Post-fix
    the helper derives agents_parent = agent_dir.parent, so the call-site shape
    works. This case pins the real-call path: a revert to `project_root/"agents"`
    fails here."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        agents_parent = root / "agents"
        foo_dir = agents_parent / "foo"
        target_dir = agents_parent / TARGET_AGENT
        _write_aspirations(foo_dir, [
            _make_aspiration("asp-test", [
                _make_goal("g-callsite-01", intended_agent=TARGET_AGENT),
            ]),
        ])
        _write_aspirations(target_dir, [])
        # Replicate the WIRED call site EXACTLY: project_root = agent_dir.parent
        # (the agents-parent), NOT PROJECT_ROOT.
        results = collect_cross_agent_candidates(target_dir.parent, target_dir, TARGET_AGENT)
        if len(results) != 1:
            return False, (f"call-site arg shape returned {len(results)} — pre-fix "
                           f"bug: <agents-parent>/agents is nonexistent -> 0")
        if results[0].get("goal", {}).get("id") != "g-callsite-01":
            return False, f"wrong goal: {results[0].get('goal', {}).get('id')}"
        if results[0].get("source") != "cross-agent:foo":
            return False, f"wrong source: {results[0].get('source')!r}"
        return True, "ok"


def case_score_goal_stamps_routed_to_me() -> tuple[bool, str]:
    """: score_goal MUST preserve intended_agent AND stamp
    routed_to_me on the EMITTED candidate dict, so the target agent's LLM
    selection path (aspirations-select Phase 2.5 / 2.55) sees a cross-agent
    candidate is routed TO IT — not "someone else's goal" to abstain on.

    By collect_cross_agent_candidates' strict-match contract
    (intended_agent == agent_name), EVERY source='cross-agent:<owner>'
    candidate is BY CONSTRUCTION routed to the selecting agent, so a
    'cross-agent'/not-my-lane abstention on one is ALWAYS wrong. Dropping the
    field from the emission made bravo abstain 13x from its own HIGH-routed
    g-001-339. The other cases here test the RAW candidate off
    collect_cross_agent_candidates; this pins the DOWNSTREAM score_goal
    emission that the LLM actually reads."""
    goal = _make_goal("g-001-339", intended_agent=TARGET_AGENT)
    goal["priority"] = "HIGH"
    asp = _make_aspiration("asp-001", [goal])
    wm, resolved, sess = {}, {}, []

    # cross-agent candidate: routed to the selecting agent by construction
    xc = score_goal(
        {"goal": goal, "aspiration": asp, "source": "cross-agent:foo"},
        wm, resolved, sess,
    )
    if xc.get("routed_to_me") is not True:
        return False, f"cross-agent routed_to_me={xc.get('routed_to_me')!r}, expected True"
    if xc.get("intended_agent") != TARGET_AGENT:
        return False, f"intended_agent not preserved: {xc.get('intended_agent')!r}"

    # native world candidate: not routed, no intended_agent (key absent)
    native = _make_goal("g-native-01", intended_agent=None)
    nc = score_goal(
        {"goal": native, "aspiration": _make_aspiration("asp-002", [native]), "source": "world"},
        wm, resolved, sess,
    )
    if nc.get("routed_to_me") is not False:
        return False, f"native routed_to_me={nc.get('routed_to_me')!r}, expected False"
    if nc.get("intended_agent") is not None:
        return False, f"native intended_agent leaked: {nc.get('intended_agent')!r}"

    return True, "ok"


CASES = [
    ("sibling-routed-goal-appears",     case_sibling_routed_goal_appears),
    ("score-goal-stamps-routed-to-me",  case_score_goal_stamps_routed_to_me),
    ("callsite-shape-agents-parent",    case_callsite_shape_project_root_is_agents_parent),
    ("own-queue-not-double-counted",    case_own_queue_not_double_counted),
    ("unreadable-sibling-fails-open",   case_unreadable_sibling_fails_open),
    ("strict-match-contract",           case_strict_match_contract),
    ("non-agent-dir-skipped",           case_non_agent_dir_skipped),
    ("invalid-inputs-fail-open",        case_invalid_inputs_fail_open),
]


def main() -> int:
    failures = []
    for cid, fn in CASES:
        try:
            ok, msg = fn()
        except Exception as e:
            failures.append(f"[FAIL] {cid}: raised {type(e).__name__}: {e}")
            continue
        if not ok:
            failures.append(f"[FAIL] {cid}: {msg}")
        else:
            print(f"[PASS] {cid}")

    if failures:
        for f in failures:
            print(f)
        print(f"\n{len(failures)} / {len(CASES)} FAILED")
        return 1
    print(f"\nAll {len(CASES)} passed.")
    return 0


# pytest entrypoint mirrors role-affinity test pattern
def test_cross_agent_pull() -> None:
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
