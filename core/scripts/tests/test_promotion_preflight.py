# Regression tests for the promotion preflight drift gate
# (core/scripts/promotion-preflight.py). Runnable two ways:
#   py -3 core/scripts/tests/test_promotion_preflight.py     (standalone)
#   py -3 -m pytest core/scripts/tests/test_promotion_preflight.py -q
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "promotion-preflight.py"


def _run(src: Path, tgt: Path, *extra: str) -> subprocess.CompletedProcess:
    """Run the gate via the SAME interpreter (portable; no py-launcher dep)."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--source", str(src), "--target", str(tgt), *extra],
        capture_output=True, text=True,
    )


def _rc(src: Path, tgt: Path, *extra: str) -> int:
    """Shorthand: return code only."""
    return _run(src, tgt, *extra).returncode


def _mk(base: Path, rel: str, content: str = "x\n") -> None:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def _git_init_and_commit(repo: Path, rel: str, content: str,
                         commit_date: str = "2025-01-01T00:00:00") -> None:
    """Create a git repo (if needed), write a file, and commit with a controlled timestamp."""
    env = {**os.environ, "GIT_COMMITTER_DATE": commit_date, "GIT_AUTHOR_DATE": commit_date}
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"],
                        cwd=str(repo), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test"],
                        cwd=str(repo), capture_output=True, check=True)
    _mk(repo, rel, content)
    subprocess.run(["git", "add", rel], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", f"add {rel}"],
                    cwd=str(repo), capture_output=True, check=True, env=env)


def test_clean_subset_exits_0(tmp_path):
    src, tgt = tmp_path / "src", tmp_path / "tgt"
    _mk(src, "core/scripts/foo.sh", "echo hi\n")
    _mk(tgt, "core/scripts/foo.sh", "echo hi\n")
    assert _rc(src, tgt) == 0


def test_target_only_framework_file_is_orphan_drift_exit_2(tmp_path):
    src, tgt = tmp_path / "src", tmp_path / "tgt"
    _mk(src, "core/scripts/foo.sh", "echo hi\n")
    _mk(tgt, "core/scripts/foo.sh", "echo hi\n")
    _mk(tgt, "core/scripts/bar.sh", "echo extra\n")  # target leads -> would be orphaned
    assert _rc(src, tgt) == 2


def test_pyc_and_pycache_noise_excluded_exit_0(tmp_path):
    src, tgt = tmp_path / "src", tmp_path / "tgt"
    _mk(src, "core/scripts/foo.sh", "echo hi\n")
    _mk(tgt, "core/scripts/foo.sh", "echo hi\n")
    _mk(tgt, "core/scripts/__pycache__/foo.cpython-312.pyc")  # build artifact
    _mk(tgt, "core/scripts/tests/_tmp_run_test/out.txt")       # temp test artifact
    assert _rc(src, tgt) == 0


def test_differing_file_does_not_block_by_default_but_blocks_strict(tmp_path):
    """Non-git dirs -> direction is ambiguous -> default: exit 0, strict: exit 2."""
    src, tgt = tmp_path / "src", tmp_path / "tgt"
    _mk(src, "core/scripts/foo.sh", "echo SOURCE\n")
    _mk(tgt, "core/scripts/foo.sh", "echo TARGET\n")  # differs, direction ambiguous (no git)
    assert _rc(src, tgt) == 0            # default: ambiguous alone is review-only
    assert _rc(src, tgt, "--strict") == 2  # strict: ambiguous blocks


def test_deployment_local_difference_not_blocking(tmp_path):
    src, tgt = tmp_path / "src", tmp_path / "tgt"
    _mk(src, "CLAUDE.md", "dev deployment\n")
    _mk(tgt, "CLAUDE.md", "prod deployment\n")  # legit per-deployment difference
    _mk(src, "core/scripts/foo.sh", "echo hi\n")
    _mk(tgt, "core/scripts/foo.sh", "echo hi\n")
    assert _rc(src, tgt) == 0            # deployment-local diff never blocks
    assert _rc(src, tgt, "--strict") == 0


def test_target_only_skill_is_not_core_orphan_drift(tmp_path):
    # A target-only SKILL (usually domain/forged) must not trip the core
    # orphan-risk blocker by default (it lands in the "verify domain-local" bucket).
    src, tgt = tmp_path / "src", tmp_path / "tgt"
    _mk(src, "core/scripts/foo.sh", "echo hi\n")
    _mk(tgt, "core/scripts/foo.sh", "echo hi\n")
    _mk(tgt, ".claude/skills/manage-website/SKILL.md", "---\n---\ndomain skill\n")
    assert _rc(src, tgt) == 0


# ---- Direction-aware tests (git timestamp classification) ----

def test_target_ahead_by_git_timestamp_blocks_default(tmp_path):
    """A core file committed more recently in the target -> gate exits 2 (DRIFT)
    even without --strict, and lists it as target_ahead in JSON output."""
    src, tgt = tmp_path / "src", tmp_path / "tgt"
    # Commit same file in both repos, but target has a NEWER commit timestamp
    _git_init_and_commit(src, "core/scripts/foo.sh", "echo OLD\n",
                         commit_date="2025-01-01T00:00:00")
    _git_init_and_commit(tgt, "core/scripts/foo.sh", "echo NEW\n",
                         commit_date="2025-06-01T00:00:00")
    result = _run(src, tgt, "--json")
    assert result.returncode == 2, f"Expected exit 2, got {result.returncode}. stderr: {result.stderr}"
    data = json.loads(result.stdout)
    assert data["verdict"] == "DRIFT"
    assert "core/scripts/foo.sh" in data["target_ahead_core"]
    # Also confirm human-readable output mentions CLOBBER RISK
    result_human = _run(src, tgt)
    assert result_human.returncode == 2
    assert "CLOBBER RISK" in result_human.stdout


def test_source_ahead_by_git_timestamp_exits_0(tmp_path):
    """A core file committed more recently in the source -> safe, gate exits 0."""
    src, tgt = tmp_path / "src", tmp_path / "tgt"
    # Source has a NEWER commit timestamp
    _git_init_and_commit(src, "core/scripts/foo.sh", "echo NEWER SOURCE\n",
                         commit_date="2025-06-01T00:00:00")
    _git_init_and_commit(tgt, "core/scripts/foo.sh", "echo OLD TARGET\n",
                         commit_date="2025-01-01T00:00:00")
    result = _run(src, tgt, "--json")
    assert result.returncode == 0, f"Expected exit 0, got {result.returncode}. stderr: {result.stderr}"
    data = json.loads(result.stdout)
    assert data["verdict"] == "CLEAN"
    assert "core/scripts/foo.sh" in data["source_ahead_core"]
    assert data["target_ahead_core"] == []


def test_ambiguous_without_git_exits_0_default_and_2_strict(tmp_path):
    """Non-git dirs with differing files (direction ambiguous) ->
    exit 0 in default mode, exit 2 with --strict."""
    src, tgt = tmp_path / "src", tmp_path / "tgt"
    _mk(src, "core/scripts/foo.sh", "echo A\n")
    _mk(tgt, "core/scripts/foo.sh", "echo B\n")
    # No .git in either -> git_last_commit_ts returns None -> falls to mtime
    # mtime difference < 60s (both just written) -> ambiguous
    result_default = _run(src, tgt, "--json")
    assert result_default.returncode == 0
    data_default = json.loads(result_default.stdout)
    assert data_default["verdict"] == "CLEAN"
    assert "core/scripts/foo.sh" in data_default["ambiguous_core"]

    result_strict = _run(src, tgt, "--strict", "--json")
    assert result_strict.returncode == 2
    data_strict = json.loads(result_strict.stdout)
    assert data_strict["verdict"] == "DRIFT"


def test_target_ahead_json_output_shape(tmp_path):
    """Verify the JSON output includes the new direction-classified keys."""
    src, tgt = tmp_path / "src", tmp_path / "tgt"
    _git_init_and_commit(src, "core/scripts/a.sh", "echo src\n",
                         commit_date="2025-01-01T00:00:00")
    _git_init_and_commit(tgt, "core/scripts/a.sh", "echo tgt\n",
                         commit_date="2025-06-01T00:00:00")
    result = _run(src, tgt, "--json")
    data = json.loads(result.stdout)
    # All direction-aware keys must be present
    for key in ("target_ahead_core", "target_ahead_skills",
                "source_ahead_core", "source_ahead_skills",
                "ambiguous_core", "ambiguous_skills"):
        assert key in data, f"Missing key: {key}"


def test_mixed_directions_only_target_ahead_blocks(tmp_path):
    """When some files are source-ahead and some target-ahead, only target-ahead
    files cause the gate to block."""
    src, tgt = tmp_path / "src", tmp_path / "tgt"
    # File A: source ahead (safe)
    _git_init_and_commit(src, "core/scripts/a.sh", "echo newer-src\n",
                         commit_date="2025-06-01T00:00:00")
    _git_init_and_commit(tgt, "core/scripts/a.sh", "echo older-tgt\n",
                         commit_date="2025-01-01T00:00:00")
    # File B: target ahead (blocking)
    # Need to commit B after initial commits in both repos
    _git_init_and_commit(src, "core/scripts/b.sh", "echo older-src\n",
                         commit_date="2025-01-01T00:00:00")
    _git_init_and_commit(tgt, "core/scripts/b.sh", "echo newer-tgt\n",
                         commit_date="2025-06-01T00:00:00")
    result = _run(src, tgt, "--json")
    assert result.returncode == 2
    data = json.loads(result.stdout)
    assert "core/scripts/b.sh" in data["target_ahead_core"]
    assert "core/scripts/a.sh" in data["source_ahead_core"]


# ---- Recency-shadow baseline tests () ----

def _git_commit(repo: Path, rel: str, content: str, commit_date: str, message: str) -> None:
    """Write+commit a file with a controlled timestamp AND commit message.

    Extends _git_init_and_commit with an explicit message so a test can author the
    'chore: sync framework (<TS>)' transplant baseline commit the shadow guard keys on.
    """
    env = {**os.environ, "GIT_COMMITTER_DATE": commit_date, "GIT_AUTHOR_DATE": commit_date}
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"],
                        cwd=str(repo), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test"],
                        cwd=str(repo), capture_output=True, check=True)
    _mk(repo, rel, content)
    subprocess.run(["git", "add", rel], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", message],
                    cwd=str(repo), capture_output=True, check=True, env=env)


def test_transplant_shadow_not_target_ahead(tmp_path):
    """: a file planted by a bulk 'chore: sync framework' transplant commit
    (newer than the since-untouched source) must NOT classify target_ahead. The
    transplant date is the baseline; a per-file tgt_ts at/before it is a shadow."""
    src, tgt = tmp_path / "src", tmp_path / "tgt"
    # Source: full file committed long ago; source untouched since.
    _git_init_and_commit(src, "core/scripts/foo.sh", "echo BASE\nDOMAIN LINE\n",
                         commit_date="2025-01-01T00:00:00")
    # Target: the transplant plants a domain-stripped subset in ONE bulk commit
    # 'chore: sync framework' dated AFTER the source's last edit -> the shadow.
    _git_commit(tgt, "core/scripts/foo.sh", "echo BASE\n",
                commit_date="2025-06-01T00:00:00",
                message="chore: sync framework (2025-06-01T00:00:00)")
    result = _run(src, tgt, "--json")
    data = json.loads(result.stdout)
    # The shadow must NOT be flagged target_ahead (the pre-fix false CLOBBER RISK).
    assert "core/scripts/foo.sh" not in data["target_ahead_core"], \
        f"shadow wrongly classified target_ahead: {data['target_ahead_core']}"
    assert data["baseline_transplant_ts"] is not None  # baseline detected + surfaced
    # Domain-stripped subset -> content heuristic -> source_ahead -> gate CLEAN.
    assert result.returncode == 0, f"expected CLEAN, got {result.returncode}"
    assert "core/scripts/foo.sh" in data["source_ahead_core"]


def test_genuine_target_edit_after_transplant_still_blocks(tmp_path):
    """ counter-test: a file edited at the TARGET *after* the transplant
    baseline is a GENUINE target-ahead and must still block. The shadow guard must
    not blind the gate to real downstream edits."""
    src, tgt = tmp_path / "src", tmp_path / "tgt"
    _git_init_and_commit(src, "core/scripts/bar.sh", "echo SRC\n",
                         commit_date="2025-01-01T00:00:00")
    # Transplant baseline commit plants bar.sh (matches source at this point).
    _git_commit(tgt, "core/scripts/bar.sh", "echo SRC\n",
                commit_date="2025-06-01T00:00:00",
                message="chore: sync framework (2025-06-01T00:00:00)")
    # bar.sh genuinely edited at the target AFTER the transplant baseline.
    _git_commit(tgt, "core/scripts/bar.sh", "echo TGT GENUINE EDIT\n",
                commit_date="2025-07-01T00:00:00",
                message="fix: genuine downstream edit")
    result = _run(src, tgt, "--json")
    data = json.loads(result.stdout)
    assert data["baseline_transplant_ts"] is not None
    assert "core/scripts/bar.sh" in data["target_ahead_core"], \
        "genuine post-transplant target edit must still be target_ahead"
    assert result.returncode == 2


def test_no_transplant_baseline_preserves_legacy_target_ahead(tmp_path):
    """No 'chore: sync framework' commit -> baseline None -> raw per-file timestamps
    drive classification (legacy behavior, zero regression)."""
    src, tgt = tmp_path / "src", tmp_path / "tgt"
    _git_init_and_commit(src, "core/scripts/foo.sh", "echo OLD\n",
                         commit_date="2025-01-01T00:00:00")
    _git_init_and_commit(tgt, "core/scripts/foo.sh", "echo NEW\n",
                         commit_date="2025-06-01T00:00:00")
    result = _run(src, tgt, "--json")
    data = json.loads(result.stdout)
    assert data["baseline_transplant_ts"] is None
    assert "core/scripts/foo.sh" in data["target_ahead_core"]
    assert result.returncode == 2


# ── Phase 0 zone-classifier tests () ─────────────────────────────

def _load_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("promotion_preflight", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_zone_classifier_covers_one_file_per_zone(tmp_path):
    # Pure classify_zone — at least one file per zone (KERNEL / parametric /
    # structural / NICHE). tmp_path unused (pure string classification).
    pp = _load_module()
    assert pp.classify_zone(".claude/settings.local.json") == "KERNEL"
    assert pp.classify_zone("core/config/seed-manifest.yaml") == "KERNEL"
    assert pp.classify_zone(".claude/skills/seed/SKILL.md") == "KERNEL"
    assert pp.classify_zone("core/config/aspirations.yaml") == "PHENOTYPE-parametric"
    assert pp.classify_zone("core/scripts/goal-selector.py") == "PHENOTYPE-structural"
    assert pp.classify_zone(".claude/rules/self.md") == "PHENOTYPE-structural"
    assert pp.classify_zone("CLAUDE.md") == "PHENOTYPE-structural"
    assert pp.classify_zone("world/knowledge/tree/x.md") == "NICHE"


def test_zone_column_in_json_is_report_only(tmp_path):
    # A differing PHENOTYPE-structural file is non-blocking by default (exit 0).
    # The zone column must appear in JSON AND must not change that exit code —
    # this is the report-only guarantee (zero gating effect).
    src = tmp_path / "src"; tgt = tmp_path / "tgt"
    _mk(src, ".claude/rules/example.md", "rule A\n")
    _mk(tgt, ".claude/rules/example.md", "rule B\n")   # differs -> non-blocking default
    r = _run(src, tgt, "--json")
    assert r.returncode == 0, r.stdout + r.stderr       # report-only: gating unchanged
    data = json.loads(r.stdout)
    zc = data["zone_classification_report_only"]
    assert zc[".claude/rules/example.md"] == "PHENOTYPE-structural"
    assert data["zone_summary"].get("PHENOTYPE-structural", 0) >= 1
    assert data["verdict"] == "CLEAN"


def test_zone_column_classifies_orphan_drift_without_altering_verdict(tmp_path):
    # A target-only core file is orphan-risk drift (exit 2). The zone column
    # classifies it (PHENOTYPE-parametric here) but the drift verdict is set by
    # the pre-existing orphan logic, NOT the zone column.
    src = tmp_path / "src"; tgt = tmp_path / "tgt"
    _mk(src, "core/config/aspirations.yaml", "w: 1\n")
    _mk(tgt, "core/config/aspirations.yaml", "w: 1\n")   # identical baseline
    _mk(tgt, "core/config/thresholds.yaml", "t: 5\n")    # target-only parametric orphan
    r = _run(src, tgt, "--json")
    assert r.returncode == 2, r.stdout + r.stderr        # orphan drift (unchanged)
    data = json.loads(r.stdout)
    assert data["zone_classification_report_only"]["core/config/thresholds.yaml"] == "PHENOTYPE-parametric"
    assert data["verdict"] == "DRIFT"


# ---- Phase 2 ENFORCEMENT tests (): content-divergence gates verdict ----

def test_comment_only_parametric_target_ahead_excused_exit_0(tmp_path):
    """A PHENOTYPE-parametric file the TARGET leads on (would be CLOBBER RISK ->
    exit 2) whose diff is COMMENT/PROVENANCE-ONLY (downstream goal-id scrub, same
    parsed value) is EXCUSED from blocking -> exit 0, CLEAN. This is the
    g-115-2867 Phase-1 finding made enforcement: reconciling comment drift up
    would strip provenance, so it must not block."""
    src, tgt = tmp_path / "src", tmp_path / "tgt"
    # SAME value (threshold: 0.5); target's comment strips the private goal-id.
    _git_init_and_commit(src, "core/config/thresholds.yaml",
                         "# g-306-02 provenance note\nthreshold: 0.5\n",
                         commit_date="2025-01-01T00:00:00")
    _git_init_and_commit(tgt, "core/config/thresholds.yaml",
                         "# provenance note\nthreshold: 0.5\n",
                         commit_date="2025-06-01T00:00:00")  # target ahead
    result = _run(src, tgt, "--json")
    assert result.returncode == 0, f"expected exit 0 (excused), got {result.returncode}. stderr: {result.stderr}"
    data = json.loads(result.stdout)
    assert data["verdict"] == "CLEAN"
    assert "core/config/thresholds.yaml" in data["comment_only_excused_from_blocking"]
    assert data["content_divergence"]["core/config/thresholds.yaml"]["kind"] == "comment"
    # Human output labels it EXCUSED, not CLOBBER RISK.
    human = _run(src, tgt)
    assert human.returncode == 0
    assert "EXCUSED" in human.stdout


def test_value_change_parametric_target_ahead_still_blocks_exit_2(tmp_path):
    """Same target-ahead parametric setup but with a REAL value change (0.5 ->
    0.9) is NOT excused -> exit 2, DRIFT. A value clobber must still block."""
    src, tgt = tmp_path / "src", tmp_path / "tgt"
    _git_init_and_commit(src, "core/config/thresholds.yaml",
                         "# same comment\nthreshold: 0.5\n",
                         commit_date="2025-01-01T00:00:00")
    _git_init_and_commit(tgt, "core/config/thresholds.yaml",
                         "# same comment\nthreshold: 0.9\n",
                         commit_date="2025-06-01T00:00:00")  # target ahead + value change
    result = _run(src, tgt, "--json")
    assert result.returncode == 2, f"expected exit 2 (value change), got {result.returncode}. stderr: {result.stderr}"
    data = json.loads(result.stdout)
    assert data["verdict"] == "DRIFT"
    assert "core/config/thresholds.yaml" in data["target_ahead_core"]
    assert data["comment_only_excused_from_blocking"] == []
    assert data["content_divergence"]["core/config/thresholds.yaml"]["kind"] == "value"


def _main() -> int:
    import tempfile
    failures = 0
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        with tempfile.TemporaryDirectory() as d:
            try:
                t(Path(d))
                print(f"PASS  {t.__name__}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL  {t.__name__}  {e}")
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"ERROR {t.__name__}  {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
