#!/usr/bin/env python3
"""test_precheck_temp_pressure.py — precheck-eval.py cmd_temp_pressure contract
(file-model normalization Phase 5).

Pins the temp/ accumulation-pressure check that keeps temp/ from becoming the
new slush directory: it counts UNDRAINED working docs directly under the bound
agent's temp/ (excluding the drained/ audit subdir) and emits

  - no flag                  below warn_threshold
  - temp_pressure_warn       at >= warn_threshold (visible nudge, no goal)
  - temp_drain_needed        at >= drain_goal_threshold (+ suggested HIGH goal)
  - temp_drain_pending       at >= drain_goal_threshold when an open drain goal
                             already exists (deduped — no second goal filed)

AGENT_DIR is a module global imported from _paths; the tests monkeypatch it to a
tmp dir so the count targets a controlled temp/ rather than the live agent.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

spec = importlib.util.spec_from_file_location("precheck_eval", SCRIPT_DIR / "precheck-eval.py")
pe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pe)

CONFIG = {"temp_pressure": {"warn_threshold": 10, "drain_goal_threshold": 20}}


class _Args:
    pass


def _seed_temp(tmp_path, n_flat, n_drained=0, n_ephemera=0):
    """Create tmp_path/temp/ with n_flat working docs (.md) + n_drained in
    drained/ + n_ephemera pure-ephemera .log/.txt files in temp/ root."""
    temp = tmp_path / "temp"
    temp.mkdir(parents=True, exist_ok=True)
    for i in range(n_flat):
        (temp / f"design-2026-06-02T00-00-{i:02d}.md").write_text("doc", encoding="utf-8")
    if n_drained:
        (temp / "drained").mkdir(exist_ok=True)
        for i in range(n_drained):
            (temp / "drained" / f"old-{i:02d}.md").write_text("drained", encoding="utf-8")
    for i in range(n_ephemera):
        # alternate .log / .txt so both ephemera suffixes are exercised
        suffix = ".log" if i % 2 == 0 else ".txt"
        (temp / f"suite-{i:02d}{suffix}").write_text("ephemera", encoding="utf-8")
    return temp


def _compact(goals=None):
    return {"aspirations": [{"id": "asp-001", "status": "active", "goals": goals or []}]}


def _run(tmp_path, monkeypatch, n_flat, n_drained=0, goals=None, n_ephemera=0):
    _seed_temp(tmp_path, n_flat, n_drained, n_ephemera)
    monkeypatch.setattr(pe, "AGENT_DIR", tmp_path)
    return pe.cmd_temp_pressure(_Args(), CONFIG, _compact(goals))


def test_temp_pressure_clean(tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, n_flat=0)
    assert r["count"] == 0 and r["flags"] == []
    assert r["suggested_goal"] is None


def test_temp_pressure_below_warn_no_flag(tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, n_flat=9)
    assert r["count"] == 9 and r["flags"] == []


def test_temp_pressure_warn_at_threshold(tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, n_flat=10)
    assert r["count"] == 10 and r["flags"] == ["temp_pressure_warn"]
    assert r["suggested_goal"] is None  # warn never files a goal


def test_temp_pressure_drain_needed_at_threshold(tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, n_flat=20)
    assert r["count"] == 20 and r["flags"] == ["temp_drain_needed"]
    g = r["suggested_goal"]
    assert g is not None and g["priority"] == "HIGH"
    assert g["participants"] == ["agent"]          # capability-routing: agent, not user
    assert "drain" in g["title"].lower() and "temp" in g["title"].lower()
    # : routes to the temp OWNER (AGENT_DIR.name, monkeypatched to tmp_path),
    # NOT the content classifier — without this, capability_route's "knowledge tree"
    # Tier-3 heuristic misroutes the drain to bravo and it no-ops on the wrong store.
    assert g["intended_agent"] == tmp_path.name


def test_temp_pressure_drained_subdir_excluded(tmp_path, monkeypatch):
    # 5 live + 50 already-drained -> only the 5 live count (drained/ is the
    # audit archive, already encoded into the tree).
    r = _run(tmp_path, monkeypatch, n_flat=5, n_drained=50)
    assert r["count"] == 5 and r["flags"] == []


def test_temp_pressure_dedup_existing_drain_goal(tmp_path, monkeypatch):
    # 25 undrained docs BUT an open drain-temp goal already exists -> no second
    # goal filed; emits temp_drain_pending instead.
    goals = [{"id": "g-001-99", "status": "pending",
              "title": "Maintain: drain accumulated temp/ working docs"}]
    r = _run(tmp_path, monkeypatch, n_flat=25, goals=goals)
    assert r["count"] == 25
    assert r["flags"] == ["temp_drain_pending"]
    assert r["existing_drain_goal"] == "g-001-99"
    assert r["suggested_goal"] is None


def test_temp_pressure_other_agent_drain_goal_not_deduped(tmp_path, monkeypatch):
    # : the undrained-doc COUNT is scoped to AGENT_DIR/temp (the bound
    # agent's store), so the existing-drain-goal DEDUP must ALSO be agent-scoped.
    # World-queue drain goals appear in every agent's compact — a drain goal filed
    # by ANOTHER agent must NOT suppress this agent's suggestion, else while any ONE
    # agent has an open drain goal, every OTHER agent's temp/ grows unbounded
    # (temp_drain_pending with no goal ever filed). filed_by_agent != AGENT_DIR.name
    # (== tmp_path.name here) => not ours => still temp_drain_needed + a suggestion.
    goals = [{"id": "g-001-88", "status": "pending",
              "title": "Maintain: drain accumulated temp/ working docs",
              "filed_by_agent": "some-other-agent"}]
    r = _run(tmp_path, monkeypatch, n_flat=25, goals=goals)
    assert r["count"] == 25
    assert r["flags"] == ["temp_drain_needed"]         # NOT temp_drain_pending
    assert r["existing_drain_goal"] is None             # other agent's goal is not ours
    assert r["suggested_goal"] is not None


def test_temp_pressure_own_agent_drain_goal_deduped(tmp_path, monkeypatch):
    # Companion to the cross-agent test: this agent's OWN open drain goal
    # (filed_by_agent == the bound agent, i.e. AGENT_DIR.name == tmp_path.name)
    # still dedups -> temp_drain_pending, no duplicate goal filed. Proves the
    #  scoping did not break same-agent dedup.
    goals = [{"id": "g-001-77", "status": "pending",
              "title": "Maintain: drain accumulated temp/ working docs",
              "filed_by_agent": tmp_path.name}]
    r = _run(tmp_path, monkeypatch, n_flat=25, goals=goals)
    assert r["count"] == 25
    assert r["flags"] == ["temp_drain_pending"]
    assert r["existing_drain_goal"] == "g-001-77"
    assert r["suggested_goal"] is None


def test_temp_pressure_investigate_goal_not_treated_as_drain_goal(tmp_path, monkeypatch):
    #  regression: an ANALYSIS goal (Investigate:) whose title happens to
    # contain "drain"+"temp" must NOT satisfy the action-goal dedup — else it falsely
    # counts as the open drain goal and permanently suppresses the real "Maintain:
    # drain..." goal from ever filing (temp/ grows unbounded). At the drain threshold
    # with ONLY an Investigate goal present, we must still emit temp_drain_needed +
    # a suggested_goal, with no false existing_drain_goal.
    goals = [{"id": "g-115-1780", "status": "pending",
              "title": "Investigate: temp-drain goal not auto-surfaced by goal-selector"}]
    r = _run(tmp_path, monkeypatch, n_flat=25, goals=goals)
    assert r["count"] == 25
    assert r["flags"] == ["temp_drain_needed"]
    assert r["existing_drain_goal"] is None
    assert r["suggested_goal"] is not None
    assert "drain" in r["suggested_goal"]["title"].lower()


def test_temp_pressure_idea_goal_not_treated_as_drain_goal(tmp_path, monkeypatch):
    # Companion to the Investigate case: an Idea: goal about the temp drain is also
    # analysis, not an action goal, and must not trip the dedup ().
    goals = [{"id": "g-115-9001", "status": "pending",
              "title": "Idea: pressure-boost the temp drain goal's selector score"}]
    r = _run(tmp_path, monkeypatch, n_flat=25, goals=goals)
    assert r["flags"] == ["temp_drain_needed"]
    assert r["existing_drain_goal"] is None


def test_temp_pressure_real_drain_goal_found_despite_analysis_goal(tmp_path, monkeypatch):
    # No over-correction (): when BOTH an analysis goal AND a real Maintain
    # drain goal are open, the dedup must SKIP the analysis goal and still find the
    # real action goal — so an existing drain goal is correctly deduped even when an
    # Investigate goal precedes it in iteration order.
    goals = [
        {"id": "g-115-1780", "status": "pending",
         "title": "Investigate: temp-drain goal not auto-surfaced"},
        {"id": "g-001-99", "status": "pending",
         "title": "Maintain: drain accumulated temp/ working docs"},
    ]
    r = _run(tmp_path, monkeypatch, n_flat=25, goals=goals)
    assert r["flags"] == ["temp_drain_pending"]
    assert r["existing_drain_goal"] == "g-001-99"
    assert r["suggested_goal"] is None


def test_temp_pressure_maintain_about_drain_not_treated_as_drain_goal(tmp_path, monkeypatch):
    # : the  skip covered Investigate:/Idea: analysis goals, but a
    # "Maintain:" goal merely ABOUT the temp drain (e.g. a verify-learning check on the
    # drain FILING) starts with "Maintain:" — NOT investigate:/idea: — yet is NOT the
    # real drain ACTION goal. The old keyword denylist ('"drain" in t and "temp" in t')
    # falsely matched it (surfaced when 's ORIGINAL title tripped it): at the
    # drain threshold it would count as the open drain goal and permanently suppress the
    # real "Maintain: drain N accumulated temp/ working docs" goal (the same unbounded-
    # growth failure as ). The positive drain-action signature excludes it BY
    # CONSTRUCTION (it does not start with "Maintain: drain " + the template infix).
    goals = [{"id": "g-115-2980", "status": "pending",
              "title": "Maintain: add verify-learning check that precheck "
                       "temp-drain filing carries intended_agent"}]
    r = _run(tmp_path, monkeypatch, n_flat=25, goals=goals)
    assert r["count"] == 25
    assert r["flags"] == ["temp_drain_needed"]           # NOT temp_drain_pending
    assert r["existing_drain_goal"] is None               # Maintain-ABOUT-drain is not the action
    assert r["suggested_goal"] is not None


def test_temp_pressure_purge_only_drain_goal_deduped(tmp_path, monkeypatch):
    # : the template also fires for a purge-only close (count==0, ephemera>0)
    # and still emits a "Maintain: drain 0 accumulated temp/ working docs ... + purge N
    # ..." title. The positive signature MUST match that variant too, so a pending
    # purge-only drain goal correctly dedups. Guards the signature against being
    # over-narrowed to only the count>0 form.
    goals = [{"id": "g-001-66", "status": "pending",
              "title": "Maintain: drain 0 accumulated temp/ working docs to the "
                       "knowledge tree + purge 12 stale ephemera file(s)"}]
    r = _run(tmp_path, monkeypatch, n_flat=25, goals=goals)
    assert r["flags"] == ["temp_drain_pending"]
    assert r["existing_drain_goal"] == "g-001-66"


def test_temp_pressure_warn_range_ignores_existing_drain_goal(tmp_path, monkeypatch):
    # In the warn range (10-19) an existing drain goal is irrelevant — dedup only
    # gates the drain-threshold goal-filing, so this still emits temp_pressure_warn
    # (NOT temp_drain_pending, which is a drain-threshold-only signal).
    goals = [{"id": "g-001-99", "status": "pending",
              "title": "Maintain: drain accumulated temp/ working docs"}]
    r = _run(tmp_path, monkeypatch, n_flat=15, goals=goals)
    assert r["count"] == 15
    assert r["flags"] == ["temp_pressure_warn"]
    assert r["suggested_goal"] is None


def test_temp_pressure_json_files_count(tmp_path, monkeypatch):
    # Working docs may be .md or .json; both count toward pressure.
    temp = tmp_path / "temp"
    temp.mkdir(parents=True)
    for i in range(6):
        (temp / f"a-{i}.md").write_text("x", encoding="utf-8")
    for i in range(6):
        (temp / f"b-{i}.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(pe, "AGENT_DIR", tmp_path)
    r = pe.cmd_temp_pressure(_Args(), CONFIG, _compact())
    assert r["count"] == 12 and r["flags"] == ["temp_pressure_warn"]


def test_temp_pressure_missing_config_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "AGENT_DIR", tmp_path)
    with pytest.raises(KeyError):
        pe.cmd_temp_pressure(_Args(), {}, _compact())


# ── Pure-ephemera (.log/.txt) counting () ───────────────────────
# Pre-fix, .log/.txt files were invisible to BOTH the drain glob and this
# metric, so ephemera-only accumulation emitted NO flag and grew unbounded.
# The metric now counts ephemera separately and folds it into the combined
# pressure that drives the threshold flags.

def test_temp_pressure_ephemera_counted_separately(tmp_path, monkeypatch):
    # 3 docs + 4 ephemera -> count=3, ephemera_count=4, pressure_count=7,
    # below warn(10) so no flag; the two counts are NOT conflated.
    r = _run(tmp_path, monkeypatch, n_flat=3, n_ephemera=4)
    assert r["count"] == 3
    assert r["ephemera_count"] == 4
    assert r["pressure_count"] == 7
    assert r["flags"] == []


def test_temp_pressure_ephemera_only_triggers_warn(tmp_path, monkeypatch):
    # 0 docs + 12 ephemera -> pressure_count=12 >= warn(10) -> temp_pressure_warn.
    # This is the exact  bug: pre-fix, 12 invisible ephemera emitted
    # NO flag; now they are seen.
    r = _run(tmp_path, monkeypatch, n_flat=0, n_ephemera=12)
    assert r["count"] == 0 and r["ephemera_count"] == 12
    assert r["flags"] == ["temp_pressure_warn"]
    assert r["suggested_goal"] is None


def test_temp_pressure_ephemera_only_triggers_drain(tmp_path, monkeypatch):
    # 0 docs + 20 ephemera -> pressure_count=20 >= drain(20) -> temp_drain_needed;
    # the suggested goal names the ephemera purge.
    r = _run(tmp_path, monkeypatch, n_flat=0, n_ephemera=20)
    assert r["count"] == 0 and r["ephemera_count"] == 20
    assert r["flags"] == ["temp_drain_needed"]
    g = r["suggested_goal"]
    assert g is not None and g["priority"] == "HIGH"
    assert g["participants"] == ["agent"]          # capability-routing: agent, not user
    assert "purge" in g["title"].lower() and "20" in g["title"]


def test_temp_pressure_docs_plus_ephemera_combined(tmp_path, monkeypatch):
    # 15 docs + 6 ephemera: neither alone crosses drain(20), combined
    # pressure_count=21 does -> temp_drain_needed. The goal names both the
    # drain (15 docs) and the purge (6 ephemera).
    r = _run(tmp_path, monkeypatch, n_flat=15, n_ephemera=6)
    assert r["count"] == 15 and r["ephemera_count"] == 6 and r["pressure_count"] == 21
    assert r["flags"] == ["temp_drain_needed"]
    g = r["suggested_goal"]
    assert "drain 15" in g["title"] and "purge 6" in g["title"].lower()


def test_temp_pressure_ephemera_clean_when_zero(tmp_path, monkeypatch):
    # No docs, no ephemera -> clean.
    r = _run(tmp_path, monkeypatch, n_flat=0, n_ephemera=0)
    assert r["count"] == 0 and r["ephemera_count"] == 0 and r["pressure_count"] == 0
    assert r["summary"] == "temp-pressure: clean"
    assert r["flags"] == []


def test_temp_pressure_ephemera_dedup_existing_goal(tmp_path, monkeypatch):
    # ephemera pushes combined pressure over drain BUT an open drain goal exists
    # -> temp_drain_pending, no second goal filed.
    goals = [{"id": "g-001-99", "status": "pending",
              "title": "Maintain: drain accumulated temp/ working docs"}]
    r = _run(tmp_path, monkeypatch, n_flat=10, n_ephemera=12, goals=goals)
    assert r["pressure_count"] == 22
    assert r["flags"] == ["temp_drain_pending"]
    assert r["existing_drain_goal"] == "g-001-99"
    assert r["suggested_goal"] is None


# ── One-shot scratch-script ephemera (.py/.sh/.err) counting () ──
# Pre-fix, one-shot scratch scripts (build-*.py, orphan-*.py, restart-poller.sh,
# gs.err) in temp/ root were invisible to BOTH the drain glob and this metric,
# so scratch-only accumulation emitted NO flag and grew unbounded — the exact
#  gap for a different file class. EPHEMERA_SUFFIXES now includes
# .py/.sh/.err so they count as ephemera alongside .log/.txt.

def test_temp_pressure_scratch_scripts_counted_as_ephemera(tmp_path, monkeypatch):
    # 4 scratch scripts (.py/.sh/.err) + 1 legacy .log = 5 ephemera, 0 docs.
    temp = tmp_path / "temp"
    temp.mkdir(parents=True)
    (temp / "build-fix.py").write_text("x", encoding="utf-8")
    (temp / "orphan-scan.py").write_text("x", encoding="utf-8")
    (temp / "restart-poller.sh").write_text("x", encoding="utf-8")
    (temp / "gs.err").write_text("x", encoding="utf-8")
    (temp / "suite.log").write_text("x", encoding="utf-8")  # legacy class still counts
    monkeypatch.setattr(pe, "AGENT_DIR", tmp_path)
    r = pe.cmd_temp_pressure(_Args(), CONFIG, _compact())
    assert r["count"] == 0
    assert r["ephemera_count"] == 5
    assert r["pressure_count"] == 5
    assert r["flags"] == []  # below warn(10)


def test_temp_pressure_scratch_scripts_not_conflated_with_docs(tmp_path, monkeypatch):
    # A .py/.sh/.err in temp/ root is ephemera, NOT a drainable working doc
    # (.md/.json). The two classes must stay distinct: 2 docs + 3 scratch.
    temp = tmp_path / "temp"
    temp.mkdir(parents=True)
    (temp / "design.md").write_text("doc", encoding="utf-8")
    (temp / "plan.json").write_text("{}", encoding="utf-8")
    (temp / "a.py").write_text("x", encoding="utf-8")
    (temp / "b.sh").write_text("x", encoding="utf-8")
    (temp / "c.err").write_text("x", encoding="utf-8")
    monkeypatch.setattr(pe, "AGENT_DIR", tmp_path)
    r = pe.cmd_temp_pressure(_Args(), CONFIG, _compact())
    assert r["count"] == 2           # .md + .json only
    assert r["ephemera_count"] == 3  # .py + .sh + .err
    assert r["pressure_count"] == 5


# ---------------------------------------------------------------------------
# : the third file class + the purge-scope git cross-check.
#
# WHY THESE EXIST: the two classes above are extension ALLOWLISTS, so every
# other suffix in temp/ root was counted by nothing and reported by nothing.
# Measured on a live agent: 26 files in temp/ root, metric returned 7 (3.7x).
# The fix reports the remainder as `unclassified_count` and pins the total via
# `temp_root_total`, WITHOUT feeding pressure_count — those files are neither
# drainable nor purgeable, so counting them toward the drain threshold would
# fire drain goals that cannot drain anything. test_pressure_count_excludes_
# unclassified is the guard for that specific decision.
# ---------------------------------------------------------------------------


def test_unclassified_counts_non_allowlisted_suffixes(tmp_path, monkeypatch):
    temp = tmp_path / "temp"
    temp.mkdir(parents=True)
    (temp / "a.md").write_text("doc", encoding="utf-8")       # counted: doc
    (temp / "b.log").write_text("x", encoding="utf-8")        # counted: ephemera
    for name in ("vol2.pdf", "brief.docx", "cfg.yaml", "led.jsonl", "s.ps1", "r.tsv", "NOEXT"):
        (temp / name).write_text("x", encoding="utf-8")       # counted: unclassified
    monkeypatch.setattr(pe, "AGENT_DIR", tmp_path)
    r = pe.cmd_temp_pressure(_Args(), CONFIG, _compact())
    assert r["count"] == 1
    assert r["ephemera_count"] == 1
    assert r["unclassified_count"] == 7
    # temp_root_total must reconcile with an independent enumeration of temp/ ROOT
    assert r["temp_root_total"] == len([f for f in temp.iterdir() if f.is_file()]) == 9


def test_pressure_count_excludes_unclassified(tmp_path, monkeypatch):
    """Unclassified files must NOT move the drain threshold: they are neither
    drainable nor purgeable, so a drain goal fired by them could not act."""
    temp = tmp_path / "temp"
    temp.mkdir(parents=True)
    for i in range(40):                        # far past drain_goal_threshold=20
        (temp / f"deliverable-{i:02d}.pdf").write_text("x", encoding="utf-8")
    monkeypatch.setattr(pe, "AGENT_DIR", tmp_path)
    r = pe.cmd_temp_pressure(_Args(), CONFIG, _compact())
    assert r["unclassified_count"] == 40
    assert r["pressure_count"] == 0
    assert r["flags"] == []                    # no warn, no drain_needed
    assert r["suggested_goal"] is None


def test_unclassified_surfaces_in_summary(tmp_path, monkeypatch):
    """A count that lives only in the JSON body is the same invisibility this
    fix removes — the summary is what the precheck actually prints."""
    temp = tmp_path / "temp"
    temp.mkdir(parents=True)
    (temp / "vol2.pdf").write_text("x", encoding="utf-8")
    monkeypatch.setattr(pe, "AGENT_DIR", tmp_path)
    r = pe.cmd_temp_pressure(_Args(), CONFIG, _compact())
    assert "not-drainable" in r["summary"]
    assert r["summary"] != "temp-pressure: clean"


def _git(cwd, *args):
    import subprocess
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def _seed_git_repo(tmp_path):
    """A real repo so the cross-check exercises real `git ls-files` output."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")


def test_tracked_ephemera_excluded_from_purge_scope(tmp_path, monkeypatch):
    """POSITIVE CONTROL for the git cross-check: extension alone cannot tell a
    business record from scratch, so a TRACKED .txt/.py must leave purge scope."""
    agent = tmp_path / "agents" / "agent-a"
    temp = agent / "temp"
    temp.mkdir(parents=True)
    _seed_git_repo(tmp_path)
    (temp / "deliverable-notes.txt").write_text("tracked record", encoding="utf-8")
    (temp / "build-helper.py").write_text("tracked script", encoding="utf-8")
    (temp / "scratch.log").write_text("real scratch", encoding="utf-8")
    _git(tmp_path, "add", "-f", "agents/agent-a/temp/deliverable-notes.txt",
         "agents/agent-a/temp/build-helper.py")
    _git(tmp_path, "commit", "-qm", "seed")
    monkeypatch.setattr(pe, "AGENT_DIR", agent)
    monkeypatch.setattr(pe, "PROJECT_ROOT", tmp_path)
    r = pe.cmd_temp_pressure(_Args(), CONFIG, _compact())
    assert r["ephemera_tracked_excluded"] == 2
    assert r["ephemera_count"] == 1            # only the untracked .log stays purgeable
    assert r["unclassified_count"] == 2        # reclassified, NOT dropped
    assert r["temp_root_total"] == 3           # conservation


def test_untracked_ephemera_stays_in_purge_scope(tmp_path, monkeypatch):
    """NEGATIVE CONTROL: same tree, nothing tracked -> the check must stay
    silent. Without this, a broken cross-check that excluded everything would
    still pass the positive control above."""
    agent = tmp_path / "agents" / "agent-a"
    temp = agent / "temp"
    temp.mkdir(parents=True)
    _seed_git_repo(tmp_path)
    for name in ("deliverable-notes.txt", "build-helper.py", "scratch.log"):
        (temp / name).write_text("x", encoding="utf-8")
    monkeypatch.setattr(pe, "AGENT_DIR", agent)
    monkeypatch.setattr(pe, "PROJECT_ROOT", tmp_path)
    r = pe.cmd_temp_pressure(_Args(), CONFIG, _compact())
    assert r["ephemera_tracked_excluded"] == 0
    assert r["ephemera_count"] == 3
    assert r["unclassified_count"] == 0


def test_git_cross_check_fails_open(tmp_path, monkeypatch):
    """No repo at PROJECT_ROOT -> `git ls-files` fails. A precheck advisory must
    never break the loop over unavailable git, so counts stay untouched."""
    agent = tmp_path / "agents" / "agent-a"
    temp = agent / "temp"
    temp.mkdir(parents=True)
    (temp / "scratch.log").write_text("x", encoding="utf-8")
    (temp / "notes.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(pe, "AGENT_DIR", agent)
    monkeypatch.setattr(pe, "PROJECT_ROOT", tmp_path)   # not a git repo
    r = pe.cmd_temp_pressure(_Args(), CONFIG, _compact())
    assert r["ephemera_count"] == 2                     # unchanged — failed open
    assert r["ephemera_tracked_excluded"] == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
