"""Tests for goal-pickup-coordination-check.py ( / US-03).

Advisory same-surface-race probe at goal-pickup. These tests pin the pure
classifier contract — path/keyword extraction, commit-goal-id parse, path
overlap, and the overlap classifier including the canonical 2026-05-13 race
(partner already shipped) and the own-goal exclusion. The git/daemon/team-state
reads in main() are impure and exercised only at runtime, not here.

Pattern: importlib + sys.path (the script name has hyphens, so it cannot be a
plain `import`), per test_defer_drift_check.py.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "goal-pickup-coordination-check.py"


def _import():
    spec = importlib.util.spec_from_file_location(
        "goal_pickup_coordination_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["goal_pickup_coordination_check"] = mod
    spec.loader.exec_module(mod)
    return mod


M = _import()


def _commit(h, subject, files):
    return {"hash": h, "subject": subject, "files": files}


# ── extract_paths ────────────────────────────────────────────────────────────

def test_extract_paths_governed_roots():
    text = "Edit core/scripts/aspirations-select/SKILL.md and .claude/rules/foo.md"
    paths = M.extract_paths(text)
    assert "core/scripts/aspirations-select/SKILL.md" in paths
    assert ".claude/rules/foo.md" in paths


def test_extract_paths_bare_filename():
    paths = M.extract_paths("the fix lives in goal-selector.py and team-state.yaml")
    assert "goal-selector.py" in paths
    assert "team-state.yaml" in paths


def test_extract_paths_rejects_bare_extension():
    # A bare extension with no filename stem must NOT be captured (too broad).
    assert M.extract_paths("update the .py files and .md docs") == set()


# ── extract_keywords ─────────────────────────────────────────────────────────

def test_extract_keywords_keeps_compounds_drops_filler():
    kw = M.extract_keywords(
        "Add coordination check at goal-pickup to prevent same-surface races")
    assert {"coordination", "goal-pickup", "same-surface", "races"} <= kw
    for filler in ("add", "check", "at", "to", "prevent"):
        assert filler not in kw


def test_extract_keywords_strips_goal_id_and_scope():
    kw = M.extract_keywords("feat(g-305-03): coordination guard")
    assert "coordination" in kw and "guard" in kw
    assert not any(t.startswith("g-305") for t in kw)
    assert "feat" not in kw  # conventional-commit scope stripped


# ── commit_goal_id ───────────────────────────────────────────────────────────

def test_commit_goal_id_from_scope():
    assert M.commit_goal_id("feat(g-115-697): same-surface fix") == "g-115-697"


def test_commit_goal_id_none_when_absent():
    assert M.commit_goal_id("chore(gate-d): validator tweak") is None
    assert M.commit_goal_id("") is None


# ── _path_overlap ────────────────────────────────────────────────────────────

def test_path_overlap_same_basename():
    assert M._path_overlap("core/scripts/goal-selector.py",
                           "core/scripts/goal-selector.py")
    assert M._path_overlap("goal-selector.py", "core/scripts/goal-selector.py")


def test_path_overlap_dir_prefix():
    assert M._path_overlap("core/scripts", "core/scripts/foo.py")


def test_path_overlap_bare_dir_no_false_basename():
    # A dot-less directory token must not basename-match an unrelated file.
    assert not M._path_overlap("docs", "core/scripts/foo.py")


def test_path_overlap_rejects_partial_filename_suffix():
    # A bare basename must NOT match a different file whose name merely ENDS with
    # the same suffix without a path boundary (regression for the dropped
    # `c.endswith(a)` over-match — fresh-eyes  / board msg-1989).
    assert not M._path_overlap("test.py", "core/scripts/tests/contest.py")
    assert not M._path_overlap("self.md", "agents/bravo/myself.md")
    assert not M._path_overlap("bar.py", "core/scripts/foobar.py")
    # A genuine path-boundary suffix still matches.
    assert M._path_overlap("test.py", "core/scripts/test.py")


# ── classify_overlap ─────────────────────────────────────────────────────────

def test_classify_overlap_canonical_2026_05_13_path_match():
    # The incident: a partner shipped  touching the same file that
    #  (about to be claimed) would. Surface (file) overlap → race.
    affected = {"core/scripts/aspirations-select.py"}
    kw = M.extract_keywords("Refactor aspirations-select scoring")
    commits = [_commit("abc123",
                       "feat(g-115-697): rework aspirations-select scoring",
                       ["core/scripts/aspirations-select.py"])]
    race, overlapping = M.classify_overlap(affected, kw, commits, "g-115-696")
    assert race is True
    assert overlapping[0]["committed_goal_id"] == "g-115-697"
    assert "core/scripts/aspirations-select.py" in overlapping[0]["matched_paths"]


def test_classify_overlap_keyword_only():
    affected = set()
    kw = {"coordination", "goal-pickup", "same-surface"}
    commits = [_commit("d1",
                       "feat(g-200-01): goal-pickup coordination same-surface guard",
                       [])]
    race, overlapping = M.classify_overlap(affected, kw, commits, "g-305-03")
    assert race is True
    assert len(overlapping[0]["matched_keywords"]) >= 2


def test_classify_overlap_excludes_own_goal():
    # A commit whose scope IS the claiming goal id is the agent's own WIP, not
    # a race (e.g. autocompact-resume re-claim) — must be excluded.
    affected = {"core/scripts/foo.py"}
    commits = [_commit("self1", "feat(g-305-03): wip on this very goal",
                       ["core/scripts/foo.py"])]
    race, overlapping = M.classify_overlap(affected, set(), commits, "g-305-03")
    assert race is False
    assert overlapping == []


def test_classify_overlap_no_overlap():
    affected = {"core/scripts/foo.py"}
    kw = {"coordination"}
    commits = [_commit("x1", "feat(g-999-99): unrelated client runtime change",
                       ["src/client/Bar.lua"])]
    race, _ = M.classify_overlap(affected, kw, commits, "g-305-03")
    assert race is False


def test_classify_overlap_single_keyword_below_threshold():
    affected = set()
    kw = {"coordination", "races"}
    commits = [_commit("y1", "feat(g-1-1): coordination of something unrelated", [])]
    # Only "coordination" is shared (1) — below the default threshold of 2.
    race, _ = M.classify_overlap(affected, kw, commits, "g-305-03",
                                 min_shared_keywords=2)
    assert race is False


# ── 5: boilerplate goal-vocabulary stopwording ──────────────────────

def test_extract_keywords_drops_boilerplate_goal_vocab():
    # 5: agent/user/participants/field/etc. are boilerplate goal-record
    # vocabulary with no same-surface identity; they must be stopworded so
    # race_risk fires on substantive overlap, not goal boilerplate.
    kw = M.extract_keywords(
        "Idea: set participants agent user field verification status priority")
    for boilerplate in ("agent", "user", "participant", "participants", "field",
                        "verification", "status", "priority", "source",
                        "aspiration", "category"):
        assert boilerplate not in kw, f"{boilerplate} must be stopworded (g-115-1425)"


def test_classify_overlap_boilerplate_only_no_race():
    # 5 canonical FP (): when a goal's only keyword overlap with
    # a recent commit is boilerplate goal-vocabulary ("agent", "user"), it must
    # NOT flag a same-surface race. Mirrors the merge-authority 286090d7d
    # incident (matched_paths empty, "agent"+"user" the sole shared tokens).
    affected = set()
    kw = M.extract_keywords("Set participants agent user on this goal")
    commits = [_commit(
        "ma1", "feat(g-999-01): merge-authority grants agent user participants", [])]
    race, overlapping = M.classify_overlap(affected, kw, commits, "g-115-1425")
    assert race is False
    assert overlapping == []


def test_classify_overlap_substantive_token_still_races():
    # Guard against over-stopping: a SUBSTANTIVE shared token alongside
    # boilerplate must still flag (2-keyword threshold met by real tokens).
    affected = set()
    kw = M.extract_keywords("Refactor coordination scoring for agent user")
    commits = [_commit(
        "s1", "feat(g-888-02): coordination scoring rework for the agent", [])]
    race, overlapping = M.classify_overlap(affected, kw, commits, "g-115-1425")
    assert race is True
    assert {"coordination", "scoring"} <= set(overlapping[0]["matched_keywords"])


# ── _basename_stem (5) ──────────────────────────────────────────────

def test_basename_stem_strips_dir_and_extension():
    assert M._basename_stem(
        "src/main/java/com/ayoai/IntentEngineVerticle.java") == "intentengineverticle"
    assert M._basename_stem("core/scripts/goal-selector.py") == "goal-selector"
    assert M._basename_stem("team-state.yaml") == "team-state"


def test_basename_stem_edge_cases():
    assert M._basename_stem("") == ""
    assert M._basename_stem("noext") == "noext"          # no extension
    assert M._basename_stem("foo.test.py") == "foo.test"  # only final ext stripped


# ── classify_uncommitted_overlap (5) ────────────────────────────────

def test_classify_uncommitted_bare_class_name_stem_match():
    # THE incident: a goal names a class WITHOUT an extension
    # ('IntentEngineVerticle'), so extract_paths returns empty and the commit
    # probe's path signal can't fire. A partner is editing
    # 'IntentEngineVerticle.java' uncommitted (in_flight). The basename-stem
    # match — file stem 'intentengineverticle' ∩ goal keyword set — catches it.
    affected = set()
    kw = M.extract_keywords(
        "Investigate: probe missed concurrent IntentEngineVerticle edit")
    assert "intentengineverticle" in kw          # extension-less class name survives
    uncommitted = ["src/main/java/com/ayoai/IntentEngineVerticle.java"]
    matched = M.classify_uncommitted_overlap(affected, kw, uncommitted)
    assert len(matched) == 1
    assert matched[0]["matched_stem"] == "intentengineverticle"
    assert matched[0]["matched_paths"] == []


def test_classify_uncommitted_path_match():
    # When the goal DID name a path, the path signal fires (same as commits),
    # and unrelated uncommitted files (telemetry) are not matched.
    affected = {"core/scripts/goal-selector.py"}
    uncommitted = ["core/scripts/goal-selector.py", "agents/zeta/journal.jsonl"]
    matched = M.classify_uncommitted_overlap(affected, set(), uncommitted)
    assert len(matched) == 1
    assert matched[0]["file"] == "core/scripts/goal-selector.py"
    assert "core/scripts/goal-selector.py" in matched[0]["matched_paths"]


def test_classify_uncommitted_no_match():
    affected = {"core/scripts/foo.py"}
    kw = {"coordination"}
    uncommitted = ["agents/zeta/health/2026-06-16.jsonl", "world/changelog.jsonl"]
    assert M.classify_uncommitted_overlap(affected, kw, uncommitted) == []


def test_classify_uncommitted_stem_carries_no_boilerplate():
    # The stem path matches against the goal KEYWORD set, which extract_keywords
    # already stripped of stopwords / sub-4-char tokens. So a boilerplate-only
    # title yields an empty keyword set and a stem like 'status' cannot match
    # status.py — no self-FP on goal-record vocabulary (5 family).
    affected = set()
    kw = M.extract_keywords("Set status field on the goal")  # -> empty
    assert kw == set()
    uncommitted = ["core/scripts/status.py"]
    assert M.classify_uncommitted_overlap(affected, kw, uncommitted) == []
