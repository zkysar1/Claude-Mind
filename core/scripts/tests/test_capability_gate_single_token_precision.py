"""test_capability_gate_single_token_precision.py —  both-ways matrix.

The capability gate's _find_matches matched on ANY single shared token, so
generic prose vocabulary in trigger/row text false-matched unrelated skills
(zeta, 3x forced --override in one session: bridge->access-roblox-studio,
analysis->analyze-npc-behavior, reachable->operator-api — the sig-30
hardcoded-list-under-coverage shape, sibling of g-248-104 goal_duplication).

Fix under test: a SOLE shared token must be distinctive — structurally
compound (hyphen/underscore/digit), an imperative capability verb
(_IMPERATIVE_VERBS: "commit"/"push" name deliberate actions — dropping them
re-opened the g-115-1883 recall regressions during development), or an
identifier part of the entry's skill/script NAMES. Multi-token overlaps
always survive. Extraction is untouched (NOT more stopwords), so demoted
tokens still count inside 2+-token matches.

SAFETY DIRECTION (why both ways): dropping a match LOOSENS the gate — fewer
refusals of user-routing — risking the g-115-792 anti-pattern (wrongly
user-gated agent work). The genuine-match half of this matrix is therefore
as load-bearing as the FP half.

Tests are PURE-function level with synthetic entries (box-independent: the
observed FPs came from cc-02's world-store registrations, which differ per
box; the mechanism is what must stay pinned). The subprocess e2e recall
guards live in test_capability_gate_prose_collision.py and stay green.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
GATES_CAPABILITY_PY = SCRIPT_DIR.parent / "gates" / "capability.py"


def _load_mod():
    spec = importlib.util.spec_from_file_location(
        "capability_gate_stp", GATES_CAPABILITY_PY)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load_mod()


# Synthetic entries reproducing the three observed FP shapes: the generic
# token appears only as trigger/row PROSE vocabulary, never in the skill's
# identifier surface.
_ROBLOX_SKILL = {"source": "forged-skills.yaml", "skill": "access-roblox-studio",
                 "triggers": ["run the roblox bridge", "studio session"],
                 "scripts": ["world/scripts/roblox-bridge.py"]}
_NPC_SKILL = {"source": "forged-skills.yaml", "skill": "analyze-npc-behavior",
              "triggers": ["behavioral analysis of npc sessions"],
              "scripts": []}
_OPERATOR_ROW = {"source": "capability-routing.md",
                 "row": "operator endpoint reachable check via rest health"}
# NB: _TOKEN_RE keeps hyphenated compounds whole ("access-efs-data" is ONE
# entry token), so a bare keyword only ever intersects an entry via a BARE
# word in its trigger/row prose. The identifier-part rule then decides
# whether that bare word is the skill's own name-part (deliberate reference)
# or mere vocabulary.
_EFS_SKILL = {"source": "forged-skills.yaml", "skill": "access-efs-data",
              "triggers": ["efs host unreachable", "remote filesystem"],
              "scripts": ["world/scripts/efs-ssh.sh"]}
_COMMIT_ROW = {"source": "capability-routing.md",
               "row": "commit and push repository changes to main"}


def _match_skills(keywords, entries):
    return {(m.get("skill") or m.get("row")) for m in
            M._find_matches(set(keywords), entries)}


# ---------------------------------------------------------------------------
# FP half — the three observed shapes stop matching on a sole generic token
# ---------------------------------------------------------------------------

def test_sole_bridge_prose_token_does_not_match():
    # "diagnostic bridge output is stale" -> sole shared token "bridge"
    # (trigger prose vocabulary, not an identifier part of the skill).
    assert _match_skills({"bridge", "diagnostic", "output", "stale"},
                         [_ROBLOX_SKILL]) == set()


def test_sole_analysis_prose_token_does_not_match():
    assert _match_skills({"analysis", "prose", "incidental"},
                         [_NPC_SKILL]) == set()


def test_sole_reachable_row_prose_token_does_not_match():
    assert _match_skills({"reachable", "box", "network"},
                         [_OPERATOR_ROW]) == set()


# ---------------------------------------------------------------------------
# Genuine half — real references still fire (the  safety direction)
# ---------------------------------------------------------------------------

def test_identifier_part_single_token_still_matches():
    # "cannot access " -> sole token "efs" IS a name-part of
    # access-efs-data (and of efs-ssh.sh) — a deliberate reference.
    skills = _match_skills({"efs"}, [_EFS_SKILL])
    assert "access-efs-data" in skills


def test_multi_token_overlap_always_matches():
    # "run the roblox bridge" -> roblox + bridge both shared: multi-token
    # overlap survives even though "bridge" alone would not.
    skills = _match_skills({"roblox", "bridge"}, [_ROBLOX_SKILL])
    assert "access-roblox-studio" in skills


def test_compound_single_token_still_matches():
    # Structurally compound tokens only exist where someone named a real
    # thing (the guard-958 recall-control class: backend-cat).
    entry = {"source": "forged-skills.yaml", "skill": "probe-governed-store",
             "triggers": ["verify backend-cat output"], "scripts": []}
    assert _match_skills({"backend-cat"}, [entry]) == {"probe-governed-store"}


def test_imperative_verb_single_token_still_matches():
    # 3 recall guards: "commit the hotfix. Confirm ..." survives on
    # the sole imperative capability verb "commit" against the row.
    skills = _match_skills({"commit", "hotfix"}, [_COMMIT_ROW])
    assert _COMMIT_ROW["row"] in skills


def test_qualifier_predicate_directly():
    q = M._single_token_qualifies
    # compound / digit
    assert q("backend-cat", _NPC_SKILL)
    assert q("s3", _NPC_SKILL)
    assert q("zeta_deploy", _NPC_SKILL)
    # imperative capability verbs
    assert q("commit", _OPERATOR_ROW)
    assert q("push", _OPERATOR_ROW)
    # identifier parts (skill NAME only, split on -_./)
    assert q("efs", _EFS_SKILL)
    assert q("roblox", _ROBLOX_SKILL)   # name-part of access-roblox-studio
    # generic prose vocabulary — the FP class. "bridge" is in the trigger
    # prose AND the companion-script name (roblox-bridge.py): script names
    # are deliberately excluded from the identifier surface, else this FP
    # would survive.
    assert not q("bridge", _ROBLOX_SKILL)
    assert not q("analysis", _NPC_SKILL)
    assert not q("reachable", _OPERATOR_ROW)


def test_identifier_parts_exclude_trigger_prose():
    # The identifier surface is skill + script NAMES only — trigger prose
    # must not leak into it (else every vocabulary word qualifies).
    parts = M._identifier_parts(_NPC_SKILL)
    assert "analysis" not in parts
    assert {"analyze", "npc", "behavior"} <= parts
