""" -- preflight must flag prod-ahead lines the TARGET'S OWN HISTORY
authored after its last transplant, not only the ones the opcode heuristic sees.

Measured against ZDS (the goal's Finding 1), the g-115-4155 line-level check
missed 29 of 46 genuine ZDS-authored framework files by two mechanisms, and
each has a regression test below:
  1. an addition inside a region the source ALSO changed is a difflib
     `replace`, which target_only_functional_lines skips by design;
  2. skill files were never in the loop (`sa_skills` absent).
Then, even when the preflight flagged a file line-level, framework_pull's
collect_flagged never read that bucket, so the adopt overwrote it without
asking for a registered decision (the last two tests).

Subprocess-based for the reason test_promotion_preflight_line_level_prod_ahead.py
gives: the observable that broke is the process exit code (0 CLEAN, 2 DRIFT).
No test passes --strict (guard-1479).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "promotion-preflight.py"

PLANT = "2025-01-01T00:00:00"
TARGET_EDIT = "2025-02-01T00:00:00"
SOURCE_EDIT = "2025-06-01T00:00:00"
CODE = "core/scripts/widget.sh"
SKILL = ".claude/skills/widget/SKILL.md"


def _json(src: Path, tgt: Path) -> tuple[int, dict]:
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--source", str(src), "--target", str(tgt), "--json"],
        capture_output=True, text=True,
    )
    return r.returncode, json.loads(r.stdout)


def _commit(repo: Path, files: dict[str, str], when: str, msg: str) -> None:
    env = {**os.environ, "GIT_COMMITTER_DATE": when, "GIT_AUTHOR_DATE": when}
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        for cmd in (["git", "init"], ["git", "config", "user.email", "t@t.com"],
                    ["git", "config", "user.name", "T"]):
            subprocess.run(cmd, cwd=str(repo), capture_output=True, check=True)
    for rel, body in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
        subprocess.run(["git", "add", rel], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=str(repo),
                   capture_output=True, check=True, env=env)


def _pair(tmp_path, rel, plant_body, target_body, source_body,
          plant_msg="chore: sync framework (2025-01-01)"):
    """Target: plant commit, then its OWN edit. Source: committed last, so the
    direction heuristic says source_ahead and no file-level bucket blocks."""
    src, tgt = tmp_path / "src", tmp_path / "tgt"
    _commit(tgt, {rel: plant_body}, PLANT, plant_msg)
    if target_body is not None:
        _commit(tgt, {rel: target_body}, TARGET_EDIT, "downstream edit")
    _commit(src, {rel: source_body}, SOURCE_EDIT, "upstream edit")
    return src, tgt


def _file_level_buckets_empty(d: dict) -> None:
    # guard-2066: a 2 must have exactly one possible source.
    for key in ("orphan_risk_core", "target_ahead_core", "ambiguous_core"):
        assert d[key] == [], (key, d[key])


def test_addition_inside_a_region_the_source_also_changed_blocks(tmp_path):
    """Mechanism 1. The opcode is `replace`, so the pre-fix preflight exits 0."""
    src, tgt = _pair(tmp_path, CODE,
                     plant_body="echo a\necho b\necho c\n",
                     target_body="echo a\necho b\necho DOWNSTREAM_ONLY\necho c\n",
                     source_body="echo a\necho b_upstream\necho c\n")
    rc, d = _json(src, tgt)
    assert CODE in d["source_ahead_core"], d["source_ahead_core"]
    _file_level_buckets_empty(d)
    assert d["history_prod_ahead"] == {CODE: ["echo DOWNSTREAM_ONLY"]}
    assert CODE in d["line_level_prod_ahead"]
    assert d["history_baseline_sha"]
    assert rc == 2


def test_downstream_line_in_a_skill_blocks(tmp_path):
    """Mechanism 2. Skills were never in the line-level loop."""
    src, tgt = _pair(tmp_path, SKILL,
                     plant_body="# Widget\nstep one\n",
                     target_body="# Widget\nstep one\nDOWNSTREAM skill step\n",
                     source_body="# Widget\nstep one\nstep two upstream\n")
    rc, d = _json(src, tgt)
    assert SKILL in d["source_ahead_skills"], d["source_ahead_skills"]
    _file_level_buckets_empty(d)
    assert d["history_prod_ahead"] == {SKILL: ["DOWNSTREAM skill step"]}
    assert SKILL in d["line_level_prod_ahead"]
    assert rc == 2


def test_file_the_target_never_touched_after_the_plant_is_clean(tmp_path):
    """Negative control: the same replace shape with no downstream commit."""
    src, tgt = _pair(tmp_path, CODE,
                     plant_body="echo a\necho b\necho c\n",
                     target_body=None,
                     source_body="echo a\necho b_upstream\necho c\n")
    rc, d = _json(src, tgt)
    assert d["history_prod_ahead"] == {}
    assert d["line_level_prod_ahead"] == {}
    assert rc == 0


def test_downstream_line_the_source_already_carries_is_not_prod_ahead(tmp_path):
    """A downstream port of upstream content is not something a mirror deletes."""
    src, tgt = _pair(tmp_path, SKILL,
                     plant_body="# Widget\nstep one\n",
                     target_body="# Widget\nstep one\nshared step\n",
                     source_body="# Widget\nstep zero\nstep one\nshared step\n")
    rc, d = _json(src, tgt)
    assert d["history_prod_ahead"] == {}
    assert rc == 0


def test_no_transplant_commit_falls_back_to_the_opcode_check(tmp_path):
    """No plant, no history range: history adds nothing and the verdict is the
    opcode heuristic's alone (the documented fallback, unchanged)."""
    src, tgt = _pair(tmp_path, CODE,
                     plant_body="echo a\necho b\necho c\n",
                     target_body="echo a\necho b\necho DOWNSTREAM_ONLY\necho c\n",
                     source_body="echo a\necho b_upstream\necho c\n",
                     plant_msg="initial import")
    rc, d = _json(src, tgt)
    assert d["history_baseline_sha"] is None
    assert d["history_prod_ahead"] == {}
    assert rc == 0


def _framework_pull():
    sys.path.insert(0, str(SCRIPTS))
    import framework_pull
    return framework_pull


def test_pull_executor_counts_line_level_files_as_flagged():
    fp = _framework_pull()
    pf = {"target_ahead_core": ["core/scripts/b.sh"],
          "line_level_prod_ahead": {"core/scripts/a.sh": ["x"], SKILL: ["y"]}}
    assert fp.collect_flagged(pf) == sorted(["core/scripts/a.sh", "core/scripts/b.sh", SKILL])


def test_pull_executor_stops_on_an_unregistered_line_level_file():
    """THE SILENT CLOBBER. Before the fix this preflight produced no flagged
    path, so the gate proceeded and the adopt overwrote the file."""
    fp = _framework_pull()
    pf = {"line_level_prod_ahead": {SKILL: ["DOWNSTREAM skill step"]}}
    g = fp.gate_drift(pf, [])
    assert g["flagged"] == [SKILL]
    assert "unregistered-drift" in g["blockers"]
