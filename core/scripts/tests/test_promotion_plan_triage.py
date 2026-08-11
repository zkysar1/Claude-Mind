"""Tests for promotion-plan-triage.py — mechanical plan-verdict triage.

Hermetic: builds throwaway source/target git repos under tmp_path. No world
access, no daemon, no network. The transform path uses the REAL
_seed_transforms.transform_file with a minimal global_regex rule, so the
byte-compare fidelity is the production code path, not a reimplementation.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import yaml

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "promotion_plan_triage", SCRIPTS / "promotion-plan-triage.py")
triage = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(triage)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@test")
    _git(path, "config", "user.name", "test")
    _git(path, "config", "core.autocrlf", "false")


MANIFEST = {
    "version": 1,
    "transformations": [{
        "id": "T1",
        "type": "global_regex",
        "pattern": "BRANDX_",
        "replacement": "MIND_",
        "applies_to": ["**/*.md"],
        "reason": "test de-brand rule",
    }],
}

REL = "core/config/conventions/sample.md"
PRIOR_BODY = "# Sample\n\nUses BRANDX_TOKEN for auth.\nStable line.\n"
# What the prior plant wrote at dest = transform(PRIOR_BODY)
PLANTED_BODY = PRIOR_BODY.replace("BRANDX_", "MIND_")


def _build_source(tmp_path: Path) -> Path:
    src = tmp_path / "source"
    _init_repo(src)
    mf = src / "core" / "config" / "seed-manifest.yaml"
    mf.parent.mkdir(parents=True, exist_ok=True)
    mf.write_text(yaml.dump(MANIFEST), encoding="utf-8")
    f = src / REL
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(PRIOR_BODY, encoding="utf-8", newline="")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "release: v1.0.0")
    _git(src, "tag", "v1.0.0")
    # frontier moves on after the tag (this is what makes the plan flag files)
    f.write_text(PRIOR_BODY + "\nNew frontier line.\n", encoding="utf-8", newline="")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "feat: frontier moved after v1.0.0")
    return src


def _build_target(tmp_path: Path, planted_body: str = PLANTED_BODY) -> Path:
    tgt = tmp_path / "target"
    _init_repo(tgt)
    f = tgt / REL
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(planted_body, encoding="utf-8", newline="")
    _git(tgt, "add", "-A")
    _git(tgt, "commit", "-q", "-m", "Merge pull request #7 from owner/promote/v1.0.0")
    return tgt


def _plan_log(tmp_path: Path, rels=(REL,)) -> Path:
    log = tmp_path / "plan.log"
    lines = [f"   ⛔ {r}  (3 dest-only line(s)) — DO NOT PROMOTE OVER"
             for r in rels]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log


def _run(src, tgt, log, *extra):
    argv = ["--source", str(src), "--target", str(tgt),
            "--plan-log", str(log), *extra]
    return triage.main(argv)


def test_parse_plan_flags_dedup(tmp_path):
    log = tmp_path / "l.log"
    log.write_text(
        "   ⛔ a/b.md  (3 dest-only line(s)) — DO NOT PROMOTE OVER\n"
        "   ⛔ a/b.md  (3 dest-only line(s)) — DO NOT PROMOTE OVER\n"
        "   ⛔ c/d.sh  (1 dest-only line(s)) — DO NOT PROMOTE OVER\n"
        "unrelated line\n", encoding="utf-8")
    flags = triage.parse_plan_flags(log)
    assert [f["path"] for f in flags] == ["a/b.md", "c/d.sh"]
    assert flags[0]["dest_only_lines"] == 3


def test_prior_tag_autodetect(tmp_path):
    tgt = _build_target(tmp_path)
    assert triage.detect_prior_tag(tgt) == "v1.0.0"


def test_seed_motion_byte_equal(tmp_path, capsys):
    """Dest == transform(prior-tag) -> SEED_MOTION, exit 0. Proves the compare
    runs the transform: raw prior contains BRANDX_ and would NOT match."""
    src = _build_source(tmp_path)
    tgt = _build_target(tmp_path)
    # dirty the target tree so the repo-frozen upgrade cannot mask a
    # byte-compare failure (isolates the per-file lane)
    (tgt / "scratch.txt").write_text("x", encoding="utf-8")
    rc = _run(src, tgt, _plan_log(tmp_path))
    out = capsys.readouterr().out
    assert rc == 0
    assert "[SEED_MOTION]" in out
    assert "byte-equal transform(v1.0.0" in out
    assert "force-past-plan-ready" in out


def test_authored_residue_exits_2(tmp_path, capsys):
    src = _build_source(tmp_path)
    tgt = _build_target(tmp_path)
    f = tgt / REL
    f.write_text(PLANTED_BODY + "\nResident agent addition.\n",
                 encoding="utf-8", newline="")
    _git(tgt, "add", "-A")
    _git(tgt, "commit", "-q", "-m", "fix(g-001-99): resident agent work")
    rc = _run(src, tgt, _plan_log(tmp_path))
    out = capsys.readouterr().out
    assert rc == 2
    assert "[AUTHORED]" in out
    assert "g-001-99" in out
    assert "DO NOT use this ledger" in out


def test_sync_vintage_last_writer(tmp_path, capsys):
    """File differs from transform(prior) AND from HEAD-frozen proof, but its
    last writer is a sync commit -> SYNC_VINTAGE (older-plant vintage)."""
    src = _build_source(tmp_path)
    tgt = _build_target(tmp_path, planted_body="older plant content\n")
    # a later resident commit on ANOTHER file so HEAD is not the promote merge
    (tgt / "other.txt").write_text("y", encoding="utf-8")
    _git(tgt, "add", "-A")
    _git(tgt, "commit", "-q", "-m", "chore(agent): unrelated resident work")
    # rewrite the flagged file via a sync commit
    (tgt / REL).write_text("even older sync content\n", encoding="utf-8", newline="")
    _git(tgt, "add", "-A")
    _git(tgt, "commit", "-q", "-m", "chore: sync framework (2026-07-27)")
    rc = _run(src, tgt, _plan_log(tmp_path))
    out = capsys.readouterr().out
    assert rc == 0
    assert "[SYNC_VINTAGE]" in out


def test_repo_frozen_upgrades_everything(tmp_path, capsys):
    """Dest HEAD == promote merge + clean tree: even a file whose bytes do NOT
    match transform(prior) is excused by the repo-level proof."""
    src = _build_source(tmp_path)
    # planted content deliberately NOT byte-equal to transform(prior-tag)
    tgt = _build_target(tmp_path, planted_body="drifted transform-config output\n")
    rc = _run(src, tgt, _plan_log(tmp_path))
    out = capsys.readouterr().out
    assert rc == 0
    assert "REPO-LEVEL PROOF" in out
    assert "SEED_MOTION" in out
    assert "AUTHORED" not in out


def test_missing_log_and_empty_flags_exit_3(tmp_path, capsys):
    src = _build_source(tmp_path)
    tgt = _build_target(tmp_path)
    rc = _run(src, tgt, tmp_path / "nope.log")
    assert rc == 3
    empty = tmp_path / "empty.log"
    empty.write_text("no flags here\n", encoding="utf-8")
    rc = _run(src, tgt, empty)
    assert rc == 3
