"""Allowlist / enumeration parity -- BATCH 3 (, zeta allowlist audit).

Four "acknowledged-duplicate" enumerations from the audit. Each carried a
prose "a parity test could enforce" comment but HAD no enforcement -- this
module is that enforcement, the BATCH-1 test_terminal_goal_states_parity.py
pattern (audit reference 5c test_daemon_cli_mirror_parity.py) applied per-site
with the exact SSOT relationship the audit specified for each:

  2b  gates/user_leg_scope.VALID_USER_LEG_SCOPES
        == aspirations.VALID_USER_LEG_SCOPES                 (EQUALITY; SSOT aspirations.py)
  2e  blocker-recheck.HUMAN_ONLY_BLOCKER_TYPES
        <= gates/blocker_ref.BLOCKER_REF_TYPES               (SUBSET; SSOT blocker_ref.py)
  1c  every class core/config/work-class-mapping.yaml emits
        in test_value_framing.WORK_CLASSES (or the default)  (CONTAINMENT; SSOT the yaml)
  5b  mind_api pipeline_write.{VALID_STAGES,HORIZONS,TYPES,OUTCOMES}
        == core/scripts/pipeline.{...}                       (EQUALITY; SSOT CLI pipeline.py)

Why path (ii) parity-test and NOT path (i) consolidation: these duplicates
exist for module-independence reasons the audit classed as legitimate -- the
gate module is standalone, the daemon must not import the CLI. The audit's
implementation discipline says acknowledged-duplicate sites get a divergence
test, not a refactor that erases the boundary.

Pure-ast + yaml.safe_load by design: reads the source files by path, no import
of the target modules, no daemon spawn -- hermetic (needs no WORLD_DIR) and
runs identically from core/scripts/tests or mind_api/tests.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml


def _find_repo_root() -> Path:
    """Walk upward for the dir holding every BATCH-3 parity anchor, so this test
    is independent of which suite directory it runs from."""
    here = Path(__file__).resolve()
    needed = (
        ("core", "scripts", "aspirations.py"),
        ("core", "scripts", "gates", "user_leg_scope.py"),
        ("core", "scripts", "blocker-recheck.py"),
        ("core", "scripts", "gates", "blocker_ref.py"),
        ("core", "scripts", "tests", "test_value_framing.py"),
        ("core", "config", "work-class-mapping.yaml"),
        ("core", "scripts", "pipeline.py"),
        ("mind_api", "src", "world", "pipeline_write.py"),
        ("mind_api", "src", "world", "pipeline.py"),
    )
    for anc in [here] + list(here.parents):
        if all((anc.joinpath(*parts)).exists() for parts in needed):
            return anc
    raise RuntimeError(
        "repo root not found (need the nine BATCH-3 parity anchor files)"
    )


REPO_ROOT = _find_repo_root()
SCRIPTS = REPO_ROOT / "core" / "scripts"
MIND_API_WORLD = REPO_ROOT / "mind_api" / "src" / "world"

_WRAP_CALLS = {"frozenset", "set", "tuple", "list"}


def _module_collection(path: Path, name: str) -> set:
    """Return ``set(value)`` of a module-level ``name = <collection>``.

    Handles bare set/tuple/list literals AND a ``frozenset(...)`` / ``set(...)``
    / ``tuple(...)`` / ``list(...)`` wrapper around a single literal -- the two
    spellings BATCH-3 sites actually use (aspirations.py uses a bare set,
    gates/user_leg_scope.py wraps the same set in ``frozenset(...)``).

    Raises AssertionError (not a silent None) when the name is absent, so a
    rename/move of either side fails loudly here rather than passing a parity
    check against nothing -- the same fail-loud contract as the BATCH-1 helper.
    """
    mod = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in mod.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    val = node.value
                    if (isinstance(val, ast.Call)
                            and isinstance(val.func, ast.Name)
                            and val.func.id in _WRAP_CALLS):
                        if not val.args:
                            return set()
                        return set(ast.literal_eval(val.args[0]))
                    return set(ast.literal_eval(val))
    raise AssertionError(
        f"module-level assignment {name!r} not found in {path.name}"
    )


# ---- 2b: gates/user_leg_scope mirrors aspirations.py (EQUALITY) -------------

def test_2b_user_leg_scopes_equal():
    """The standalone gate's scope set EQUALS the aspirations.py SSOT. Fails on
    any drift in either direction (gate copy stale, or SSOT extended without
    mirroring)."""
    ssot = _module_collection(SCRIPTS / "aspirations.py", "VALID_USER_LEG_SCOPES")
    copy = _module_collection(SCRIPTS / "gates" / "user_leg_scope.py",
                              "VALID_USER_LEG_SCOPES")
    assert copy == ssot, (
        f"gates/user_leg_scope.py VALID_USER_LEG_SCOPES={sorted(copy)} drifted from "
        f"SSOT aspirations.py VALID_USER_LEG_SCOPES={sorted(ssot)}. Re-sync the gate "
        f"copy (allowlist rot class; g-303-21 / zeta audit 2b)."
    )


# ---- 2e: blocker-recheck HUMAN_ONLY is a SUBSET of blocker_ref types --------

def test_2e_human_only_subset_of_blocker_ref_types():
    """Every human-only blocker type MUST be a real blocker_ref type. A SUBSET
    (not equality) relationship: blocker_ref.BLOCKER_REF_TYPES is the full
    vocabulary; the human-only set is the agent-cannot-provision subset. A
    human-only type absent from the vocabulary can never match a live blocker."""
    human_only = _module_collection(SCRIPTS / "blocker-recheck.py",
                                    "HUMAN_ONLY_BLOCKER_TYPES")
    all_types = _module_collection(SCRIPTS / "gates" / "blocker_ref.py",
                                   "BLOCKER_REF_TYPES")
    assert human_only, (
        "HUMAN_ONLY_BLOCKER_TYPES is empty -- refusing a vacuously-true subset "
        "check (g-303-21 / zeta audit 2e)."
    )
    extra = human_only - all_types
    assert not extra, (
        f"blocker-recheck.py HUMAN_ONLY_BLOCKER_TYPES has type(s) {sorted(extra)} "
        f"not in gates/blocker_ref.py BLOCKER_REF_TYPES={sorted(all_types)}. A "
        f"human-only blocker type that is not a valid blocker_ref type can never "
        f"match a live blocker (allowlist rot class; g-303-21 / zeta audit 2e -- "
        f"SUBSET relationship)."
    )


# ---- 1c: WORK_CLASSES covers every class the core yaml mapping emits --------

def test_1c_work_classes_cover_core_mapping():
    """Every work-class the CORE mapping yaml can emit is a member of the test's
    WORK_CLASSES enumeration (or the yaml default). Containment, not equality:
    WORK_CLASSES legitimately includes 'product' which the CORE yaml never emits
    (product categories live in the WORLD overlay), so WORK_CLASSES is allowed to
    be a superset. The rot caught is a NEW class value added to the yaml mapping
    that value-framing tests would then silently never exercise."""
    work_classes = _module_collection(SCRIPTS / "tests" / "test_value_framing.py",
                                      "WORK_CLASSES")
    ycfg = yaml.safe_load(
        (REPO_ROOT / "core" / "config" / "work-class-mapping.yaml")
        .read_text(encoding="utf-8")
    )
    mapping_values = set((ycfg.get("mapping") or {}).values())
    default = ycfg.get("default", "unclassified")
    uncovered = mapping_values - (work_classes | {default})
    assert not uncovered, (
        f"core/config/work-class-mapping.yaml emits class(es) {sorted(uncovered)} "
        f"not in test_value_framing.WORK_CLASSES={sorted(work_classes)} (and not the "
        f"default {default!r}). Extend WORK_CLASSES so value-framing tests cover the "
        f"new class (allowlist rot class; g-303-21 / zeta audit 1c)."
    )


# ---- 5b: daemon pipeline_write mirrors the CLI pipeline.py vocab (EQUALITY) -

_PIPELINE_VOCAB = ["VALID_STAGES", "VALID_HORIZONS", "VALID_TYPES", "VALID_OUTCOMES"]


@pytest.mark.parametrize("const", _PIPELINE_VOCAB)
def test_5b_daemon_pipeline_write_mirrors_cli(const):
    """Each of the four daemon-writer vocab constants EQUALS its CLI SSOT. The
    canonical pipeline vocabulary lives in core/scripts/pipeline.py (the CLI
    validator); mind_api/src/world/pipeline_write.py is the daemon mirror -- the
    daemon-mirrors-CLI pattern of audit reference 5c (tree_write). NOTE: the
    audit said VALID_OUTCOMES had 'no upstream', having only inspected the daemon
    READER (pipeline.py); the CLI defines all four, so all four are enforceable."""
    cli = _module_collection(SCRIPTS / "pipeline.py", const)
    daemon = _module_collection(MIND_API_WORLD / "pipeline_write.py", const)
    assert daemon == cli, (
        f"mind_api/src/world/pipeline_write.py {const}={sorted(daemon)} drifted from "
        f"CLI SSOT core/scripts/pipeline.py {const}={sorted(cli)}. Re-sync the daemon "
        f"writer copy (allowlist rot class; g-303-21 / zeta audit 5b -- daemon mirrors "
        f"CLI, cf. 5c tree_write)."
    )


def test_5b_daemon_reader_stages_match_cli():
    """The daemon READER's VALID_STAGES (the one constant it also carries) equals
    the CLI SSOT, closing the third corner of the STAGES triangle
    (CLI / daemon-writer / daemon-reader)."""
    cli = _module_collection(SCRIPTS / "pipeline.py", "VALID_STAGES")
    reader = _module_collection(MIND_API_WORLD / "pipeline.py", "VALID_STAGES")
    assert reader == cli, (
        f"mind_api/src/world/pipeline.py VALID_STAGES={sorted(reader)} drifted from "
        f"CLI SSOT core/scripts/pipeline.py VALID_STAGES={sorted(cli)} "
        f"(allowlist rot class; g-303-21 / zeta audit 5b)."
    )
