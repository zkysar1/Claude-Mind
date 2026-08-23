"""Tests for gates.defer_target_existence ().

The gate warns when a `defer_reason` names a DEPENDENCY goal id that resolves
in no queue and no archive. Three properties carry the whole design, and each
one failed at least once during authoring, so each gets a test:

  1. ROLE-AWARE, not wide. A context MENTION of a sibling id is not a
     dependency and must not warn (31 of 34 live flags were mentions).
  2. EMPTY UNIVERSE IS SILENCE. A resolver that reads nothing must not report
     every dependency as a phantom.
  3. BOTH CALL SITES ARE WIRED. The CLI half is inert in production — the
     wrapper is daemon-only — so a test that only exercises the CLI would pass
     against a defect that reaches every real caller (guard-742/guard-2323).
     The last test pins the daemon half by source inspection, which is what
     caught the original miss.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "core" / "scripts"))

from gates.defer_target_existence import (  # noqa: E402
    MESSAGE_PREFIX,
    evaluate,
    known_goal_ids,
    sources_for,
)


def _write_world(tmp_path, goal_ids, archive_ids=()):
    world = tmp_path / "world"
    world.mkdir(exist_ok=True)
    (world / "aspirations.jsonl").write_text(
        json.dumps({"id": "asp-001", "goals": [{"id": g} for g in goal_ids]}) + "\n",
        encoding="utf-8",
    )
    (world / "aspirations-archive.jsonl").write_text(
        json.dumps({"id": "asp-002", "goals": [{"id": g} for g in archive_ids]}) + "\n",
        encoding="utf-8",
    )
    return world


def test_warns_on_a_dependency_id_that_resolves_nowhere(tmp_path):
    world = _write_world(tmp_path, ["g-115-100"])
    r = evaluate("g-115-100",
                 "blocked_on_dependency: g-999-9999 must land first",
                 sources_for(world, None))
    assert r["warn"] is True
    assert r["missing"] == ["g-999-9999"]
    assert MESSAGE_PREFIX in r["message"]


def test_live_dependency_is_silent(tmp_path):
    world = _write_world(tmp_path, ["g-115-100", "g-115-200"])
    r = evaluate("g-115-100",
                 "blocked_on_dependency: g-115-200 must land first",
                 sources_for(world, None))
    assert r["warn"] is False and r["message"] is None


def test_archived_dependency_is_accepted_silently(tmp_path):
    """An archived target is a legitimate defer target — the goal it names ran."""
    world = _write_world(tmp_path, ["g-115-100"], archive_ids=["g-004-07"])
    r = evaluate("g-115-100",
                 "blocked_on_dependency: g-004-07 must land first",
                 sources_for(world, None))
    assert r["warn"] is False and r["message"] is None


def test_context_mention_is_not_a_dependency(tmp_path):
    """Property 1. The wide predicate flags this; the role-aware one must not.

    Measured on the live corpus: a wide 'any goal id in the text' rule flagged
    34 goals where the role-aware rule flagged 3. Warning on the other 31
    correct defers is how an advisory gets trained away into noise.
    """
    world = _write_world(tmp_path, ["g-115-100"])
    r = evaluate("g-115-100",
                 "precondition_unmet: may be legitimately subset-scoped to "
                 "the g-004-07 composite, which is not yet decided",
                 sources_for(world, None))
    assert r["cited"] == []
    assert r["warn"] is False and r["message"] is None


def test_self_reference_is_never_a_missing_target(tmp_path):
    world = _write_world(tmp_path, [])
    r = evaluate("g-115-100",
                 "blocked_on_dependency: g-115-100 must land first",
                 sources_for(world, None))
    assert r["warn"] is False


def test_empty_universe_is_silence_not_a_universal_phantom(tmp_path):
    """Property 2 — the positive control in code.

    Point the gate at nothing readable. A resolver that silently built an empty
    id set would report EVERY dependency as unresolvable, which is precisely the
    shape of the claim this gate was written to re-measure.
    """
    r = evaluate("g-115-100",
                 "blocked_on_dependency: g-115-200 must land first",
                 [tmp_path / "nope" / "aspirations.jsonl"])
    assert r["known_count"] == 0
    assert r["warn"] is False and r["message"] is None
    assert r["cited"] == ["g-115-200"]  # extraction still worked


def test_sources_for_covers_every_agent_queue_not_just_one(tmp_path):
    """A world-queue defer legitimately cites an agent-queue goal."""
    world = _write_world(tmp_path, ["g-115-100"])
    agents = tmp_path / "agents"
    for name in ("alpha", "zeta"):
        d = agents / name
        d.mkdir(parents=True)
        (d / "aspirations.jsonl").write_text(
            json.dumps({"id": "asp-900",
                        "goals": [{"id": f"g-900-{name[0]}1"}]}) + "\n",
            encoding="utf-8")
    srcs = sources_for(world, agents)
    assert len(srcs) == 4  # world live+archive, two agent live files
    known = known_goal_ids(srcs)
    assert {"g-115-100", "g-900-a1", "g-900-z1"} <= known
    r = evaluate("g-115-100",
                 "blocked_on_dependency: g-900-z1 must land first", srcs)
    assert r["warn"] is False


def test_never_raises_on_garbage_input(tmp_path):
    world = _write_world(tmp_path, ["g-115-100"])
    (world / "aspirations.jsonl").write_text("not json\n\n{bad\n", encoding="utf-8")
    for text in (None, "", 12345, {"a": 1}, "g-115-999 " * 500):
        r = evaluate("g-115-100", text, sources_for(world, None))
        assert set(r) == {"warn", "cited", "missing", "known_count", "message"}


# --- Property 3: both halves of the twin are wired -------------------------

def test_cli_half_calls_the_gate():
    src = (REPO / "core" / "scripts" / "aspirations.py").read_text(encoding="utf-8")
    assert "gates.defer_target_existence" in src
    assert "_warn_unresolvable_defer_targets(goal_id, value)" in src


def test_daemon_half_calls_the_gate_and_appends_to_warnings():
    """The live half. aspirations-update-goal.sh is daemon-only, so a check
    wired ONLY into the CLI reaches no production caller — measured by
    end-to-end probe during authoring, which emitted nothing.
    """
    src = (REPO / "mind_api" / "src" / "endpoints" / "aspirations_write.py"
           ).read_text(encoding="utf-8")
    assert "gates.defer_target_existence" in src
    # Must reach the caller via warnings[], not a daemon-side stderr print
    # (daemon stderr goes to the daemon log, never to the model).
    block = src[src.index("gates.defer_target_existence"):]
    block = block[:block.index("# Structured-check schema")]
    assert 'warnings.append(_dt["message"])' in block
    assert "print(" not in block


def test_neither_half_gates_on_is_narrative_defer():
    """The guard-1802 trap this gate walked into once.

    is_narrative_defer is False for every STRUCTURED_DEFER_PREFIXES value, and
    79 of 79 live defers citing a goal id were structured. Gating the advisory
    on it fires on zero of the real population while reading as correct.
    """
    for rel in ("core/scripts/aspirations.py",
                "mind_api/src/endpoints/aspirations_write.py"):
        src = (REPO / rel).read_text(encoding="utf-8")
        i = src.index("_warn_unresolvable_defer_targets(goal_id, value)") \
            if "aspirations.py" in rel and "mind_api" not in rel \
            else src.index("gates.defer_target_existence")
        # the 400 chars preceding the call must not contain the narrative gate
        window = src[max(0, i - 400):i]
        assert "_is_narrative_defer(field, value):" not in window, rel


def test_structured_prefix_defer_still_extracts_its_dependency(tmp_path):
    """The population that matters is structured, so prove extraction works
    through each STRUCTURED_DEFER_PREFIXES shape the framework writes."""
    world = _write_world(tmp_path, ["g-115-100"])
    for text in (
        "blocked_on_dependency: g-999-9999 must land first",
        "precondition_unmet: gated on g-999-9999 — the check is unmeasurable",
    ):
        r = evaluate("g-115-100", text, sources_for(world, None))
        assert r["warn"] is True, text
        assert "g-999-9999" in r["missing"], text
