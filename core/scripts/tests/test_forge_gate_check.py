"""Tests for forge-gate-check.sh () -- /forge-skill's readiness gate.

Every run goes through the real wrapper in a subprocess (guard-7025) against a
throwaway world, gap store and agent dir. tree-read.sh and
curriculum-contract-check.sh answer from the shared in-process DaemonFixture,
never from the live fleet. Every expected threshold, gate and status set is
READ from the config the gate itself reads (guard-1220), never restated here.

Each BLOCK/WAIVED pin sits beside a control run under the same conditions that
comes out the other way, so a gate that stopped deciding (always PASS, always
BLOCK) fails here rather than passing quietly (guard-4166).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

from _competence import COMPETENCE_MAPPING  # noqa: E402
from _daemon_fixture import DaemonFixture  # noqa: E402
from _runtime_bash import bash_cmd  # noqa: E402

GATE = CORE_SCRIPTS / "forge-gate-check.sh"
CONFIG = CORE_SCRIPTS.parent / "config"

# The sources the gate reads -- the expectations come from them too.
SG = yaml.safe_load((CONFIG / "skill-gaps.yaml").read_text(encoding="utf-8"))
TREE_SCALE = yaml.safe_load((CONFIG / "tree.yaml").read_text(encoding="utf-8"))[
    "domain_health"]["competence_mapping"]
FORGE_THRESHOLD = SG["config"]["forge_threshold"]
UTILITY_GATE = SG["gap_types"]["utility"]["forge_gate"]
ANALYTICAL_GATE = SG["gap_types"]["analytical"]["forge_gate"]
SUPPRESSING = sorted(s for s, v in SG["gap_statuses"].items() if v.get("suppresses_forge"))
OPEN_STATUSES = sorted(s for s, v in SG["gap_statuses"].items() if not v.get("suppresses_forge"))

LEVELS = sorted(TREE_SCALE, key=TREE_SCALE.get)
assert LEVELS.index(UTILITY_GATE) > 0, "the utility gate must have a level below it"
BELOW_UTILITY = LEVELS[LEVELS.index(UTILITY_GATE) - 1]
# Sits at the utility gate and under the analytical one on BOTH scales.
MID_MATURITY = (COMPETENCE_MAPPING[UTILITY_GATE] + COMPETENCE_MAPPING[ANALYTICAL_GATE]) / 2

STAGES = [{"id": "cur-t1", "name": "Test Foundations", "unlocks": {"allow_forge_skill": False}},
          {"id": "cur-t2", "name": "Test Growth", "unlocks": {"allow_forge_skill": True}}]
BLOCKING_CURRICULUM = {"current_stage": "cur-t1", "stages": STAGES}
PERMITTING_CURRICULUM = {"current_stage": "cur-t2", "stages": STAGES}


def _node(parent, level, confidence):
    return {"file": None, "depth": 2, "parent": parent, "children": [], "child_count": 0,
            "summary": "test category", "capability_level": level, "confidence": confidence}


def _tree():
    nodes = {
        "root": {"file": None, "depth": 0, "parent": None, "children": ["test-domain"],
                 "child_count": 1, "summary": "root"},
        "test-domain": {"file": None, "depth": 1, "parent": "root",
                        "children": ["cat-low", "cat-mid", "cat-unmapped"],
                        "child_count": 3, "summary": "test domain"},
        "cat-low": _node("test-domain", BELOW_UTILITY, TREE_SCALE[BELOW_UTILITY] + 0.01),
        "cat-mid": _node("test-domain", UTILITY_GATE, TREE_SCALE[UTILITY_GATE] + 0.01),
        # A level off the competence scale: the gate must derive one from confidence.
        "cat-unmapped": _node("test-domain", "REFERENCE", TREE_SCALE[UTILITY_GATE] + 0.01),
    }
    return {"last_updated": "2026-09-29", "tree_growth_log": [], "nodes": nodes}


def gap(gid, **overrides):
    g = {"id": gid, "procedure_name": gid, "type": "utility", "status": "registered",
         "times_encountered": FORGE_THRESHOLD, "estimated_value": "medium", "encounter_log": []}
    g.update(overrides)
    return {k: v for k, v in g.items() if v is not None}


@pytest.fixture
def box(tmp_path):
    # Function scope on purpose: conftest's autouse fixture restores
    # MIND_META / MIND_WORLD before EVERY test, which would undo a
    # module-scoped DaemonFixture's pins and point the gate at the live store.
    world = tmp_path / "world"
    tree_dir = world / "knowledge" / "tree"
    tree_dir.mkdir(parents=True)
    (tree_dir / "_tree.yaml").write_text(yaml.safe_dump(_tree(), sort_keys=False), encoding="utf-8")
    with DaemonFixture(world) as df:
        yield {"agent": df.project_root / "agents" / df.agent,
               "meta": Path(os.environ["MIND_META"])}


def run(box, gaps, *args, maturity=MID_MATURITY, curriculum=None):
    (box["meta"] / "skill-gaps.yaml").write_text(
        yaml.safe_dump({"last_updated": None, "gaps": gaps}, sort_keys=False), encoding="utf-8")
    assessment = {} if maturity is None else {"tree_maturity": maturity}
    (box["agent"] / "developmental-stage.yaml").write_text(
        yaml.safe_dump({"current_assessment": assessment}), encoding="utf-8")
    cur = box["agent"] / "curriculum.yaml"
    if curriculum is None:
        cur.unlink(missing_ok=True)
    else:
        cur.write_text(yaml.safe_dump(curriculum, sort_keys=False), encoding="utf-8")
    env = dict(os.environ, MIND_AGENT_DIR=str(box["agent"]), STORAGE_BACKEND="local")
    return subprocess.run(bash_cmd(GATE, *args), capture_output=True, text=True,
                          env=env, timeout=180)


def parse(proc):
    """The single-gap verdict line -> {verdict, gap_id, key: value..., reasons}."""
    lines = proc.stdout.strip().splitlines()
    assert len(lines) == 1, f"want one verdict line, got {len(lines)}: {proc.stdout!r} {proc.stderr!r}"
    head, _, reasons = lines[0].partition(" reasons=")
    toks = head.split()
    out = {"verdict": toks[0], "gap_id": toks[1], "reasons": reasons}
    out.update(t.split("=", 1) for t in toks[2:])
    return out


def run_all_json(box, gaps, **kw):
    proc = run(box, gaps, "--all", "--json", **kw)
    assert proc.returncode in (0, 1), proc.stderr
    doc = json.loads(proc.stdout)
    return proc, {v["gap_id"]: v for v in doc["verdicts"]}


def test_pass_utility_gap_in_a_category_at_its_gate(box):
    p = run(box, [gap("gap-t01")], "gap-t01", "--category", "cat-mid")
    assert p.returncode == 0, p.stdout + p.stderr
    f = parse(p)
    assert f["verdict"] == "PASS" and f["reasons"] == ""
    assert f["type"] == "utility" and f["forge_gate"] == UTILITY_GATE
    assert float(f["threshold"]) == TREE_SCALE[UTILITY_GATE] and f["scale"] == "tree.yaml"
    assert f["capability_level"] == UTILITY_GATE and f["level_source"] == "stored"
    assert float(f["confidence"]) == pytest.approx(TREE_SCALE[UTILITY_GATE] + 0.01)
    assert f["capability_source"] == "category:cat-mid"
    assert f["times_encountered"] == str(FORGE_THRESHOLD) == f["forge_threshold"]
    assert f["estimated_value"] == "medium" and f["contract"].startswith("permitted")


def test_block_utility_gap_in_a_category_below_its_gate(box):
    p = run(box, [gap("gap-t02")], "gap-t02", "--category", "cat-low")
    assert p.returncode == 1, p.stdout + p.stderr
    f = parse(p)
    assert f["verdict"] == "BLOCK" and f["capability_level"] == BELOW_UTILITY
    assert float(f["threshold"]) == TREE_SCALE[UTILITY_GATE]
    assert f"< forge_gate {UTILITY_GATE}" in f["reasons"]
    # Control: the same gap one category up passes (test above) -- so the
    # block is the capability axis, not a gate that blocks everything.


def test_waived_for_a_user_request_despite_capability_and_contract(box):
    user_gap = gap("gap-t03", requested_by="user")
    p = run(box, [user_gap], "gap-t03", "--category", "cat-low", curriculum=BLOCKING_CURRICULUM)
    assert p.returncode == 0, p.stdout + p.stderr
    f = parse(p)
    assert f["verdict"] == "WAIVED" and f["reasons"] == ""
    assert f["capability"] == "waived" and f["contract"] == "waived"
    # Control: the same conditions without the user request block on BOTH
    # waived axes -- proving the waiver is what let it through.
    c = run(box, [gap("gap-t03")], "gap-t03", "--category", "cat-low", curriculum=BLOCKING_CURRICULUM)
    assert c.returncode == 1
    cf = parse(c)
    assert "capability" in cf["reasons"] and "curriculum blocks" in cf["reasons"]


def test_waiver_does_not_cover_the_other_criteria(box):
    short = gap("gap-t04", requested_by="user", times_encountered=FORGE_THRESHOLD - 1)
    p = run(box, [short], "gap-t04")
    assert p.returncode == 1
    assert f"< forge_threshold {FORGE_THRESHOLD}" in parse(p)["reasons"]
    done = gap("gap-t05", requested_by="user", status=SUPPRESSING[0])
    p = run(box, [done], "gap-t05")
    assert p.returncode == 1
    assert f"status {SUPPRESSING[0]} suppresses forging" in parse(p)["reasons"]


def test_typeless_gap_defaults_to_utility(box):
    p = run(box, [gap("gap-t06", type=None)], "gap-t06", "--category", "cat-mid")
    assert p.returncode == 0, p.stdout + p.stderr
    f = parse(p)
    assert (f["type"], f["type_source"], f["forge_gate"]) == ("utility", "default", UTILITY_GATE)
    # Control: an explicitly analytical gap in the same category blocks, so
    # the typeless pass comes from the default, not from a dead type axis.
    c = run(box, [gap("gap-t06", type="analytical")], "gap-t06", "--category", "cat-mid")
    assert c.returncode == 1
    cf = parse(c)
    assert cf["forge_gate"] == ANALYTICAL_GATE and cf["type_source"] == "stored"
    assert f"< forge_gate {ANALYTICAL_GATE}" in cf["reasons"]


def test_status_set_is_read_from_gap_statuses(box):
    gaps = [gap(f"gap-s-{s}", status=s) for s in SUPPRESSING + OPEN_STATUSES]
    gaps.append(gap("gap-s-undeclared", status="made-up-status"))
    _, v = run_all_json(box, gaps)
    for s in SUPPRESSING:
        assert v[f"gap-s-{s}"]["verdict"] == "BLOCK", s
        assert v[f"gap-s-{s}"]["reasons"] == [f"status {s} suppresses forging"]
    for s in OPEN_STATUSES:
        assert v[f"gap-s-{s}"]["verdict"] == "PASS", s
    assert "not declared in gap_statuses" in v["gap-s-undeclared"]["reasons"][0]


def test_estimated_value_is_ranked_by_its_leading_word(box):
    gaps = [gap("gap-v-medium"), gap("gap-v-high-prose", estimated_value="HIGH — ran it twice"),
            gap("gap-v-low", estimated_value="low"),
            gap("gap-v-prose", estimated_value="Eliminates a manual step"),
            gap("gap-v-missing", estimated_value=None)]
    _, v = run_all_json(box, gaps)
    assert v["gap-v-medium"]["verdict"] == "PASS"
    assert v["gap-v-high-prose"]["verdict"] == "PASS"
    assert v["gap-v-high-prose"]["estimated_value"] == "high"
    assert v["gap-v-low"]["reason_codes"] == ["value"]
    assert v["gap-v-prose"]["reason_codes"] == ["value-unrankable"]
    assert v["gap-v-missing"]["reason_codes"] == ["value-unrankable"]


def test_no_category_gates_on_the_agent_stage(box):
    p = run(box, [gap("gap-t07")], "gap-t07")
    assert p.returncode == 0, p.stdout + p.stderr
    f = parse(p)
    assert f["capability_source"] == "agent-stage" and f["scale"] == "_competence"
    assert float(f["threshold"]) == COMPETENCE_MAPPING[UTILITY_GATE]
    assert float(f["confidence"]) == pytest.approx(MID_MATURITY)
    # Control: the same maturity is below the analytical gate.
    c = run(box, [gap("gap-t07", type="analytical")], "gap-t07")
    assert c.returncode == 1
    assert float(parse(c)["threshold"]) == COMPETENCE_MAPPING[ANALYTICAL_GATE]
    # No tree_maturity to read: the capability cannot be shown, so it blocks.
    m = run(box, [gap("gap-t07")], "gap-t07", maturity=None)
    assert m.returncode == 1
    assert "no numeric current_assessment.tree_maturity" in parse(m)["reasons"]


def test_curriculum_contract_is_read_from_the_daemon(box):
    p = run(box, [gap("gap-t08")], "gap-t08", "--category", "cat-mid", curriculum=BLOCKING_CURRICULUM)
    assert p.returncode == 1, p.stdout + p.stderr
    f = parse(p)
    assert f["contract"] == "blocked:cur-t1"
    assert "curriculum blocks allow_forge_skill at Test Foundations, unlocks at Test Growth" in f["reasons"]
    c = run(box, [gap("gap-t08")], "gap-t08", "--category", "cat-mid", curriculum=PERMITTING_CURRICULUM)
    assert c.returncode == 0
    assert parse(c)["contract"] == "permitted:cur-t2"


def test_level_off_the_scale_is_derived_from_confidence(box):
    p = run(box, [gap("gap-t09")], "gap-t09", "--category", "cat-unmapped")
    assert p.returncode == 0, p.stdout + p.stderr
    f = parse(p)
    assert f["capability_level"] == UTILITY_GATE and f["level_source"] == "derived"


def test_unevaluable_input_exits_2(box):
    for args in (["gap-nope"], ["gap-t10", "--category", "no-such-node"],
                 ["--all", "--category", "cat-mid"], []):
        p = run(box, [gap("gap-t10")], *args)
        assert p.returncode == 2, (args, p.stdout, p.stderr)
        assert p.stdout == "", args


def test_a_malformed_record_blocks_alone_and_hides_nothing(box):
    # A list-valued status or type once crashed the whole run with exit 1 --
    # the BLOCK code -- so under --all one bad record hid every other gap.
    gaps = [gap("gap-m-ok"), gap("gap-m-status", status=["registered", "forged"]),
            gap("gap-m-type", type=["utility"])]
    p, v = run_all_json(box, gaps)
    assert p.returncode == 0, p.stderr
    assert v["gap-m-ok"]["verdict"] == "PASS"
    assert v["gap-m-status"]["reason_codes"] == ["status"]
    assert v["gap-m-type"]["reason_codes"] == ["type"]
    one = run(box, gaps, "gap-m-status")
    assert one.returncode == 1 and parse(one)["verdict"] == "BLOCK"


def test_all_lists_only_forge_ready_gaps(box):
    gaps = [gap("gap-a1"), gap("gap-a2", times_encountered=0), gap("gap-a3", status=SUPPRESSING[0])]
    p = run(box, gaps, "--all")
    assert p.returncode == 0, p.stdout + p.stderr
    lines = p.stdout.strip().splitlines()
    assert lines[0].startswith("forge-gate-check --all: 3 gap(s) evaluated, 1 forge-ready, 2 BLOCK")
    assert [ln.split()[1] for ln in lines if ln.startswith(("PASS", "WAIVED"))] == ["gap-a1"]
    assert any(ln.startswith("BLOCK reasons") for ln in lines)
    assert lines[-1].startswith("shared inputs") and f"{UTILITY_GATE}=" in lines[-1]
    # Nothing forge-ready -> exit 1.
    none = run(box, gaps[1:], "--all")
    assert none.returncode == 1
    assert "0 forge-ready" in none.stdout.splitlines()[0]
