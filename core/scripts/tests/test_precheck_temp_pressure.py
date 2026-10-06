#!/usr/bin/env python3
"""test_precheck_temp_pressure.py — precheck-eval.py cmd_temp_pressure contract.

Pins the temp/ accumulation-pressure check that keeps temp/ from becoming a
slush directory. Since 2026-10-05 (user directive: nothing is deleted until a
review has seen it) it counts what /drain-temp reviews: every top-level item in
the bound agent's temp/ (files of any suffix and folders) with no decision in
force that is no longer in flight, via temp_decisions.pressure_counts, the same
census the review uses. drained/, dotfiles, git-tracked files, receipted
archives, kept and to-be-purged items, and items touched within the purge's age
guard are not counted; the summary names them instead. It emits

  - no flag                  below warn_threshold
  - temp_pressure_warn       at >= warn_threshold (visible nudge, no goal)
  - temp_drain_needed        at >= drain_goal_threshold (+ suggested HIGH goal)
  - temp_drain_pending       at >= drain_goal_threshold when an open drain goal
                             already exists (deduped — no second goal filed)
  - temp_drain_stalled       ... when that goal outlived drain_goal_max_age_hours

AGENT_DIR is a module global imported from _paths; the tests monkeypatch it to a
tmp dir so the count targets a controlled temp/ rather than the live agent.
Fixtures are AGED past the in-flight window: an item written "now" is in flight
and is correctly not counted.
"""

import importlib.util
import os
import sys
import time
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


def _age(p, hours=3):
    """Set p and everything under it to `hours` ago (out of the in-flight window)."""
    t = time.time() - hours * 3600
    p = Path(p)
    if p.is_dir():
        for root, dirs, files in os.walk(p, topdown=False):
            for n in files + dirs:
                os.utime(os.path.join(root, n), (t, t))
    os.utime(p, (t, t))


def _seed_temp(tmp_path, n_flat, n_drained=0, names=()):
    """tmp_path/temp/ with n_flat working docs, n_drained files in drained/, and
    one entry per extra name (a trailing '/' makes a folder), all aged."""
    temp = tmp_path / "temp"
    temp.mkdir(parents=True, exist_ok=True)
    for i in range(n_flat):
        (temp / f"design-2026-06-02T00-00-{i:02d}.md").write_text("doc", encoding="utf-8")
    if n_drained:
        (temp / "drained").mkdir(exist_ok=True)
        for i in range(n_drained):
            (temp / "drained" / f"old-{i:02d}.md").write_text("drained", encoding="utf-8")
    for name in names:
        if name.endswith("/"):
            d = temp / name.rstrip("/")
            d.mkdir(exist_ok=True)
            (d / "inner.txt").write_text("x", encoding="utf-8")
        else:
            (temp / name).write_text("x", encoding="utf-8")
    for entry in temp.iterdir():
        _age(entry)
    return temp


def _compact(goals=None):
    return {"aspirations": [{"id": "asp-001", "status": "active", "goals": goals or []}]}


def _run(tmp_path, monkeypatch, n_flat, n_drained=0, goals=None, names=()):
    _seed_temp(tmp_path, n_flat, n_drained, names)
    monkeypatch.setattr(pe, "AGENT_DIR", tmp_path)
    return pe.cmd_temp_pressure(_Args(), CONFIG, _compact(goals))


def _git(cwd, *args):
    import subprocess
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def test_temp_pressure_clean(tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, n_flat=0)
    assert r["pressure_count"] == 0 and r["flags"] == []
    assert r["suggested_goal"] is None
    assert r["summary"] == "temp-pressure: clean"


def test_temp_pressure_below_warn_no_flag(tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, n_flat=9)
    assert r["pressure_count"] == 9 and r["flags"] == []


def test_temp_pressure_warn_at_threshold(tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, n_flat=10)
    assert r["pressure_count"] == 10 and r["flags"] == ["temp_pressure_warn"]
    assert r["suggested_goal"] is None  # warn never files a goal


def test_temp_pressure_drain_needed_at_threshold(tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, n_flat=20)
    assert r["pressure_count"] == 20 and r["flags"] == ["temp_drain_needed"]
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
    assert r["pressure_count"] == 5 and r["flags"] == []


def test_temp_pressure_dedup_existing_drain_goal(tmp_path, monkeypatch):
    # 25 undrained docs BUT an open drain-temp goal already exists -> no second
    # goal filed; emits temp_drain_pending instead.
    goals = [{"id": "g-001-99", "status": "pending",
              "title": "Maintain: drain accumulated temp/ working docs"}]
    r = _run(tmp_path, monkeypatch, n_flat=25, goals=goals)
    assert r["pressure_count"] == 25
    assert r["flags"] == ["temp_drain_pending"]
    assert r["existing_drain_goal"] == "g-001-99"
    assert r["suggested_goal"] is None


def test_temp_pressure_stalled_goal_escalates(tmp_path, monkeypatch):
    #  +  (2026-08-21): an open drain goal OLDER than
    # drain_goal_max_age_hours with pressure still >= threshold is, by
    # observation, never getting picked (rank #120/151 for weeks). The flag
    # must flip pending -> stalled and carry the escalation payload the
    # precheck SKILL acts on (invoke /drain-temp this iteration).
    import datetime as dt
    old = (dt.datetime.now() - dt.timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%S")
    goals = [{"id": "g-001-99", "status": "pending",
              "title": "Maintain: drain accumulated temp/ working docs",
              "created_at": old, "priority": "HIGH"}]
    r = _run(tmp_path, monkeypatch, n_flat=25, goals=goals)
    assert r["flags"] == ["temp_drain_stalled"]
    assert r["suggested_goal"] is None, "escalation must NEVER file a second goal"
    esc = r["escalation"]
    assert esc["goal_id"] == "g-001-99"
    assert esc["age_hours"] > 48
    assert esc["max_age_hours"] == 48
    assert esc["pressure"] == 25
    assert "STALLED" in r["summary"]


def test_temp_pressure_fresh_goal_still_pending(tmp_path, monkeypatch):
    # A drain goal YOUNGER than the max age keeps the original dedup behavior:
    # pending, no escalation — the scorer/drain-lane still gets its chance.
    import datetime as dt
    recent = (dt.datetime.now() - dt.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")
    goals = [{"id": "g-001-99", "status": "pending",
              "title": "Maintain: drain accumulated temp/ working docs",
              "created_at": recent}]
    r = _run(tmp_path, monkeypatch, n_flat=25, goals=goals)
    assert r["flags"] == ["temp_drain_pending"]
    assert r["escalation"] is None


def test_temp_pressure_unstamped_age_stays_pending(tmp_path, monkeypatch):
    # No created_at on the goal (the pre-existing fixtures' shape): age is
    # unverifiable, so escalation is conservatively withheld — never escalate
    # on an age you cannot read (verify-before-assuming).
    goals = [{"id": "g-001-99", "status": "pending",
              "title": "Maintain: drain accumulated temp/ working docs"}]
    r = _run(tmp_path, monkeypatch, n_flat=25, goals=goals)
    assert r["flags"] == ["temp_drain_pending"]
    assert r["escalation"] is None


def test_temp_pressure_stalled_respects_config_override(tmp_path, monkeypatch):
    # drain_goal_max_age_hours is config-tunable; a 100h ceiling keeps a 72h
    # goal in pending. Also pins the threshold surfacing in the result.
    import datetime as dt
    old = (dt.datetime.now() - dt.timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%S")
    goals = [{"id": "g-001-99", "status": "pending",
              "title": "Maintain: drain accumulated temp/ working docs",
              "created_at": old}]
    cfg = {"temp_pressure": {"warn_threshold": 10, "drain_goal_threshold": 20,
                             "drain_goal_max_age_hours": 100}}
    _seed_temp(tmp_path, 25)
    monkeypatch.setattr(pe, "AGENT_DIR", tmp_path)
    r = pe.cmd_temp_pressure(_Args(), cfg, _compact(goals))
    assert r["flags"] == ["temp_drain_pending"]
    assert r["thresholds"]["drain_goal_max_age_hours"] == 100


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
    assert r["pressure_count"] == 25
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
    assert r["pressure_count"] == 25
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
    assert r["pressure_count"] == 25
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
    assert r["pressure_count"] == 25
    assert r["flags"] == ["temp_drain_needed"]           # NOT temp_drain_pending
    assert r["existing_drain_goal"] is None               # Maintain-ABOUT-drain is not the action
    assert r["suggested_goal"] is not None


def test_temp_pressure_purge_only_drain_goal_deduped(tmp_path, monkeypatch):
    # : a goal filed under the PRE-2026-10-05 template ("... to the
    # knowledge tree + purge N stale ephemera file(s)", even with a count of 0) can
    # still be open when this code lands. The positive signature (prefix + infix,
    # unchanged) MUST keep matching it, or the first precheck after the upgrade
    # files a duplicate HIGH drain goal on every box that has one open.
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
    assert r["pressure_count"] == 15
    assert r["flags"] == ["temp_pressure_warn"]
    assert r["suggested_goal"] is None


def test_temp_pressure_missing_config_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "AGENT_DIR", tmp_path)
    with pytest.raises(KeyError):
        pe.cmd_temp_pressure(_Args(), {}, _compact())


# ── The review's population (user directive, 2026-10-05) ──────────────────
# Before it, .md/.json were "drainable", .log/.txt/.py/.sh/.err/.raw/.out/.bak
# were "ephemera" the purge deleted unseen (, ), any other
# suffix was reported but never scheduled (), and folders were invisible
# to the count while the purge deleted them. Now the review covers every item,
# so the count does too, and it moves when the review decides (guard-5329).


def test_temp_pressure_counts_every_suffix_and_folders(tmp_path, monkeypatch):
    names = ("a.md", "b.json", "build.py", "run.sh", "suite.log", "notes.txt",
             "dump.raw", "vol2.pdf", "cfg.yaml", "NOEXT", "gs.err", "proj/")
    r = _run(tmp_path, monkeypatch, n_flat=0, names=names)
    assert r["pressure_count"] == 12
    assert r["census"]["by_class"] == {"doc": 2, "script": 2, "run-output": 3,
                                       "other": 4, "dir": 1}
    assert r["flags"] == ["temp_pressure_warn"]
    assert "12 item(s) awaiting review [1 dir, 2 doc, 4 other, 3 run-output, 2 script]" \
        in r["summary"]


def test_temp_pressure_in_flight_items_are_not_counted(tmp_path, monkeypatch):
    # Touched within the purge's age guard = in flight: not reviewed, not
    # counted, never purged. For a folder, an entry at ANY depth counts.
    temp = _seed_temp(tmp_path, n_flat=0, names=("old.md", "busy/"))
    (temp / "new.md").write_text("x", encoding="utf-8")
    os.utime(temp / "busy" / "inner.txt", None)
    monkeypatch.setattr(pe, "AGENT_DIR", tmp_path)
    r = pe.cmd_temp_pressure(_Args(), CONFIG, _compact())
    assert r["pressure_count"] == 1 and r["census"]["fresh"] == 2
    assert "not counted: 2 in flight" in r["summary"]


def test_temp_pressure_moves_when_the_review_decides(tmp_path, monkeypatch):
    # guard-5329: a metric that schedules a remedy must MOVE when the remedy
    # runs. Over threshold; a review then decides every item and the count
    # falls to zero, with the decided items named instead.
    temp = _seed_temp(tmp_path, n_flat=25)
    monkeypatch.setattr(pe, "AGENT_DIR", tmp_path)
    assert pe.cmd_temp_pressure(_Args(), CONFIG, _compact())["flags"] == ["temp_drain_needed"]
    td = pe.temp_decisions
    rows = []
    for i, f in enumerate(sorted(temp.glob("*.md"))):
        fp, size, _ = td.stats(f, "file")
        rows.append({"ts": td._now_ts(), "item": f.name, "kind": "file", "bytes": size,
                     "fp": fp, "decision": "keep" if i % 2 else "discard", "why": "test"})
    td.append_rows(temp, rows)
    r = pe.cmd_temp_pressure(_Args(), CONFIG, _compact())
    assert r["pressure_count"] == 0 and r["flags"] == []
    assert (r["census"]["kept"], r["census"]["awaiting_purge"]) == (12, 13)
    assert "12 kept" in r["summary"] and "13 awaiting purge" in r["summary"]


def test_temp_pressure_decided_but_unmoved_items_still_count(tmp_path, monkeypatch):
    # encode/promote/archive record WHERE an item goes; until the drain has
    # moved it there it is still in temp/ and still the review's work.
    temp = _seed_temp(tmp_path, n_flat=0, names=("tool.sh",))
    td = pe.temp_decisions
    fp, size, _ = td.stats(temp / "tool.sh", "file")
    td.append_rows(temp, [{"ts": td._now_ts(), "item": "tool.sh", "kind": "file",
                           "bytes": size, "fp": fp, "decision": "promote",
                           "why": "reusable", "where": "world/scripts/tool.sh"}])
    monkeypatch.setattr(pe, "AGENT_DIR", tmp_path)
    assert pe.cmd_temp_pressure(_Args(), CONFIG, _compact())["pressure_count"] == 1


def test_temp_pressure_dotfiles_named_never_counted(tmp_path, monkeypatch):
    # No lane deletes a dotfile (the purge's Lane 0 only reports them), so they
    # cannot drive the drain (guard-5329); an unmanaged one is still named. The
    # framework's own markers (the decision log, .gitkeep) are not.
    r = _run(tmp_path, monkeypatch, n_flat=0,
             names=(".launch-notes.json", ".gitkeep", ".temp-decisions.jsonl"))
    assert r["pressure_count"] == 0 and r["flags"] == []
    assert r["census"]["unmanaged_dotfiles"] == 1
    assert "1 unmanaged dotfile(s)" in r["summary"]


def test_temp_pressure_receipted_archive_not_counted(tmp_path, monkeypatch):
    temp = _seed_temp(tmp_path, n_flat=0, names=("arc/",))
    (temp / "arc" / "RECEIPT.md").write_text("restore notes", encoding="utf-8")
    _age(temp / "arc")
    monkeypatch.setattr(pe, "AGENT_DIR", tmp_path)
    r = pe.cmd_temp_pressure(_Args(), CONFIG, _compact())
    assert r["pressure_count"] == 0 and r["census"]["receipted_dirs"] == 1
    assert "1 receipted archive(s)" in r["summary"]


def test_temp_pressure_tracked_files_not_counted(tmp_path, monkeypatch):
    # A git-tracked file under temp/ (legacy deployments) is not scratch: the
    # review cannot record a decision for it, so it must not schedule one.
    agent = tmp_path / "agents" / "agent-a"
    _seed_temp(agent, n_flat=0, names=("deliverable-notes.txt", "scratch.log"))
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-f", "agents/agent-a/temp/deliverable-notes.txt")
    monkeypatch.setattr(pe, "AGENT_DIR", agent)
    r = pe.cmd_temp_pressure(_Args(), CONFIG, _compact())
    assert r["pressure_count"] == 1 and r["census"]["tracked_skipped"] == 1
    assert "1 git-tracked" in r["summary"]


def test_temp_pressure_counts_untracked_files(tmp_path, monkeypatch):
    # NEGATIVE CONTROL for the case above: nothing tracked, everything counts.
    r = _run(tmp_path, monkeypatch, n_flat=0, names=("deliverable-notes.txt", "scratch.log"))
    assert r["pressure_count"] == 2 and r["census"]["tracked_skipped"] == 0


def test_temp_pressure_suggested_goal_names_the_review(tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, n_flat=18, names=("helper.py", "proj/"))
    assert r["flags"] == ["temp_drain_needed"] and r["pressure_count"] == 20
    g = r["suggested_goal"]
    assert g["title"].startswith("Maintain: drain 20 accumulated temp/ working docs")
    assert pe.is_drain_action_title(g["title"])      # the dedup SSOT still matches it
    assert "(1 dir, 18 doc, 1 script)" in g["description"]
    assert "records every decision" in g["description"]


# ── full-depth footprint () ────────────────────────────────────────
# The footprint is REPORTED, never thresholded. The first test below is the
# load-bearing one: it pins the SEPARATION, so a future change that wires the
# footprint into pressure_count fails here rather than shipping the guard-5329
# defect (a metric that schedules a remedy the remedy cannot move).


def test_footprint_never_moves_pressure_count(tmp_path, monkeypatch):
    """A deep subtree adds mass and files to the footprint and no more than ONE
    item to the scheduling signal: the review decides a folder whole, so a
    full-depth pressure_count would schedule a drain against entries no review
    decides one by one (guard-5329)."""
    agent = tmp_path / "agents" / "agent-a"
    temp = agent / "temp"
    temp.mkdir(parents=True)
    (temp / "flat.md").write_text("d", encoding="utf-8")
    deep = temp / "sub" / "deeper"
    deep.mkdir(parents=True)
    for i in range(30):                      # well past drain_goal_threshold=20
        (deep / f"buried-{i:02d}.md").write_text("x" * 100, encoding="utf-8")
    for entry in temp.iterdir():
        _age(entry)
    monkeypatch.setattr(pe, "AGENT_DIR", agent)
    monkeypatch.setattr(pe, "PROJECT_ROOT", tmp_path)
    r = pe.cmd_temp_pressure(_Args(), CONFIG, _compact())
    assert r["pressure_count"] == 2          # flat.md + the sub/ folder, as the review sees them
    assert r["flags"] == []                  # 30 buried files trigger nothing
    fp = r["footprint"]
    assert fp["files"] == 31                 # the footprint DOES see them
    assert fp["depth1_files"] == 1
    assert fp["deeper_files"] == 30
    assert "footprint(advisory, not thresholded)" in r["summary"]


def test_footprint_depth_split_and_byte_totals(tmp_path):
    """files/bytes are the sum of the depth-1 and deeper halves — the split is
    the point (a count of N says nothing about mass; guard-3260)."""
    temp = tmp_path / "temp"
    (temp / "a" / "b").mkdir(parents=True)
    (temp / "root.md").write_text("x" * 10, encoding="utf-8")
    (temp / "a" / "mid.md").write_text("x" * 100, encoding="utf-8")
    (temp / "a" / "b" / "leaf.md").write_text("x" * 1000, encoding="utf-8")
    fp = pe._temp_footprint(temp)
    assert (fp["files"], fp["bytes"]) == (3, 1110)
    assert (fp["depth1_files"], fp["depth1_bytes"]) == (1, 10)
    assert (fp["deeper_files"], fp["deeper_bytes"]) == (2, 1100)
    assert fp["files"] == fp["depth1_files"] + fp["deeper_files"]
    assert fp["bytes"] == fp["depth1_bytes"] + fp["deeper_bytes"]
    assert fp["truncated"] is False and fp["error"] is None


def test_footprint_prunes_clone_subtrees_dir_and_file_dotgit(tmp_path):
    """Lane 3's own predicate is `-e <dir>/.git`, which catches a .git DIR and a
    worktree/submodule .git FILE. Lane 3 PRESERVES such dirs, so counting them
    would let a subtree no lane can remove dominate the number. Pruned dirs are
    NAMED, never silently skipped."""
    temp = tmp_path / "temp"
    temp.mkdir(parents=True)
    (temp / "kept.md").write_text("x" * 5, encoding="utf-8")
    clone = temp / "some-clone"
    (clone / ".git").mkdir(parents=True)
    (clone / "huge.bin").write_text("x" * 9999, encoding="utf-8")
    wt = temp / "a-worktree"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: /elsewhere", encoding="utf-8")
    (wt / "also-huge.bin").write_text("x" * 9999, encoding="utf-8")
    fp = pe._temp_footprint(temp)
    assert (fp["files"], fp["bytes"]) == (1, 5)      # only kept.md counted
    assert fp["clone_dirs_excluded"] == 2
    assert sorted(fp["clone_dirs"]) == ["a-worktree", "some-clone"]


def test_footprint_scan_cap_reports_truncated(tmp_path):
    """The cap is a COST bound, not a tuning knob (153,453 files were measured
    under one scratch dir). A capped scan must never read as a whole one —
    guard-1760: a checker may not report what it declined to look at."""
    temp = tmp_path / "temp"
    temp.mkdir(parents=True)
    for i in range(12):
        (temp / f"f-{i:02d}.md").write_text("x", encoding="utf-8")
    fp = pe._temp_footprint(temp, file_cap=5)
    assert fp["truncated"] is True
    assert fp["files"] == 5
    uncapped = pe._temp_footprint(temp)
    assert uncapped["truncated"] is False and uncapped["files"] == 12


def test_footprint_reaches_the_summary_on_a_CLEAN_tree(tmp_path, monkeypatch):
    """A tree with mass at depth 3 and ZERO depth-1 docs is "clean" for
    SCHEDULING and is exactly the shape this metric exists to make visible
    (bravo/cc-05: ~1.1 GB under drained/). "clean" must therefore not be the
    whole sentence — a number that lives only in the JSON body is the same
    invisibility g-029-87 rejects. Caught by the Q1.5 generated checklist after
    the clause was first built inside the pressure branch only."""
    agent = tmp_path / "agents" / "agent-a"
    deep = agent / "temp" / "drained" / "x"
    deep.mkdir(parents=True)
    (deep / "huge.json").write_text("y" * 5000, encoding="utf-8")
    monkeypatch.setattr(pe, "AGENT_DIR", agent)
    monkeypatch.setattr(pe, "PROJECT_ROOT", tmp_path)
    r = pe.cmd_temp_pressure(_Args(), CONFIG, _compact())
    assert r["flags"] == [] and r["pressure_count"] == 0        # genuinely clean to schedule
    assert r["summary"] != "temp-pressure: clean"      # but not a bare "clean"
    assert "footprint(advisory, not thresholded)" in r["summary"]
    assert "5000 B full-depth" in r["summary"]
    assert r["footprint"]["deeper_bytes"] == 5000


def test_footprint_missing_temp_dir_is_zero_not_error(tmp_path):
    """Fail-open by contract: an absent temp/ is zeros with no error, so no
    caller can ever branch on a footprint failure."""
    fp = pe._temp_footprint(tmp_path / "nope")
    assert fp["files"] == 0 and fp["bytes"] == 0
    assert fp["truncated"] is False and fp["error"] is None
    assert pe._temp_footprint(None)["files"] == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
