"""test_promote_source_provenance_toctou.py —  TOCTOU close.

promote-to-upstream.sh asserted clean-tree + HEAD==v$LOCAL at the START of a
run, then planted from the WORKING TREE 15+ minutes later. Anything committed
in that window shipped downstream wearing the old tag's label (measured
2026-07-27: the ZDS payload for v2.7.1 contains code that is not in the v2.7.1
tag).

Two layers, and BOTH are required:

  1. BEHAVIOR — `source_provenance_drift` detects each drift kind against real
     git repos (clean / committed-after-tag / dirtied-after-tag / no-tag), and
     is correctly role-gated so a non-frontier role is not tag-gated.
  2. WIRING — the predicate is actually CALLED a second time, and that call sits
     between the --plan step and the seed-transplant plant. guard-1943: a green
     behavior suite certifies the FUNCTION and says nothing about whether the
     production path reaches it. The original defect was pure wiring — the
     check existed and ran in the wrong place — so a behavior-only test here
     would pass just as happily against the broken script.

These tests NEVER plant, NEVER open a PR, and NEVER mutate the real repo. The
behavior layer extracts the shell function and runs it against throwaway repos
in tmp_path; the wiring layer is a static read of the script.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
for _p in (str(CORE_SCRIPTS), str(SCRIPT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _bash_helpers import BASH  # noqa: E402

PROMOTE_SH = CORE_SCRIPTS / "promote-to-upstream.sh"

FUNC_NAME = "source_provenance_drift"


def _extract_function() -> str:
    """Pull the predicate out of the real script.

    Extraction rather than a copy: a copied predicate would pass forever while
    the shipped one drifted, which is the exact failure mode the single-predicate
    refactor exists to prevent.
    """
    lines = PROMOTE_SH.read_text(encoding="utf-8").splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^{FUNC_NAME}\(\)\s*\{{", line):
            start = i
            break
    assert start is not None, f"{FUNC_NAME}() not found in {PROMOTE_SH.name}"
    for j in range(start + 1, len(lines)):
        if lines[j] == "}":
            return "\n".join(lines[start : j + 1])
    pytest.fail(f"unterminated {FUNC_NAME}() in {PROMOTE_SH.name}")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _make_repo(tmp_path: Path, name: str, version: str = "9.9.9") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    (repo / "payload.txt").write_text("tagged content\n", encoding="utf-8")
    _git(repo, "add", "payload.txt")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "tag", f"v{version}")
    return repo


def _run_predicate(repo: Path, role: str = "frontier", version: str = "9.9.9"):
    """Run the extracted predicate; return (rc, kind, detail)."""
    harness = "\n".join(
        [
            "set -uo pipefail",
            f'PROJECT_ROOT="{repo.as_posix()}"',
            f'SELF_ROLE="{role}"',
            f'LOCAL="{version}"',
            _extract_function(),
            f"if {FUNC_NAME}; then RC=0; else RC=$?; fi",
            'printf "%s|%s|%s" "$RC" "${SRC_DRIFT_KIND:-}" "${SRC_DRIFT_DETAIL:-}"',
        ]
    )
    proc = subprocess.run(
        [BASH, "-c", harness], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, f"harness failed: {proc.stderr}"
    rc, kind, detail = proc.stdout.split("|", 2)
    return int(rc), kind, detail


# ---------------------------------------------------------------- behavior --


def test_clean_tree_at_tag_is_not_drift(tmp_path):
    """The happy path: HEAD is the tag and nothing is uncommitted."""
    repo = _make_repo(tmp_path, "clean")
    rc, kind, _ = _run_predicate(repo)
    assert rc == 0
    assert kind == ""


def test_commit_after_tag_is_drift(tmp_path):
    """THE MEASURED INCIDENT: a commit lands after the Step 1 assertion.

    This is the case that shipped untagged code as v2.7.1. HEAD moves off the
    tag while the tree stays clean, so a dirty-only check cannot see it.
    """
    repo = _make_repo(tmp_path, "committed")
    (repo / "payload.txt").write_text("content committed AFTER the tag\n", encoding="utf-8")
    _git(repo, "add", "payload.txt")
    _git(repo, "commit", "-q", "-m", "sneaks in mid-promotion")

    rc, kind, detail = _run_predicate(repo)
    assert rc == 1
    assert kind == "head-not-tag"
    assert "HEAD=" in detail and "v9.9.9=" in detail


def test_dirtied_after_tag_is_drift(tmp_path):
    """The complement: tree dirtied mid-run while HEAD stays on the tag.

    Re-checking HEAD==tag ALONE would pass here and still plant untagged
    content, which is why the predicate asserts both conditions together.
    """
    repo = _make_repo(tmp_path, "dirtied")
    (repo / "payload.txt").write_text("uncommitted edit\n", encoding="utf-8")

    rc, kind, detail = _run_predicate(repo)
    assert rc == 1
    assert kind == "dirty"
    assert "payload.txt" in detail


def test_multiple_drift_kinds_are_all_reported(tmp_path):
    """Both conditions are evaluated independently, not first-match-wins.

    A dry-run exists to report everything wrong BEFORE an operator commits to a
    15-minute run. Short-circuiting after the first kind would make them fix one
    problem, re-run, and only then discover the next — a regression introduced
    (and caught in pre-completion review) while refactoring the two original
    independent `if` blocks into one predicate.
    """
    repo = _make_repo(tmp_path, "both")
    (repo / "payload.txt").write_text("committed after tag\n", encoding="utf-8")
    _git(repo, "add", "payload.txt")
    _git(repo, "commit", "-q", "-m", "moves HEAD off the tag")
    (repo / "payload.txt").write_text("and then dirtied too\n", encoding="utf-8")

    rc, kind, detail = _run_predicate(repo)
    assert rc == 1
    kinds = kind.split()
    assert "dirty" in kinds, f"expected dirty among {kinds}"
    assert "head-not-tag" in kinds, f"expected head-not-tag among {kinds}"
    assert "payload.txt" in detail and "HEAD=" in detail


def test_missing_tag_is_drift(tmp_path):
    """A frontier promote with no matching v-tag cannot be provenance-checked."""
    repo = _make_repo(tmp_path, "notag", version="9.9.9")
    rc, kind, detail = _run_predicate(repo, version="8.8.8")
    assert rc == 1
    assert kind == "no-tag"
    assert detail == "v8.8.8"


@pytest.mark.parametrize("role", ["seed", "downstream"])
def test_non_frontier_is_not_tag_gated(tmp_path, role):
    """Role-conditional gating survives the refactor ().

    A non-frontier role re-transplants adopted framework and has no v-tag by
    design, so a missing tag must NOT read as drift there.
    """
    repo = _make_repo(tmp_path, f"role-{role}", version="9.9.9")
    rc, kind, _ = _run_predicate(repo, role=role, version="8.8.8")
    assert rc == 0
    assert kind == ""


def test_non_frontier_still_dirty_gated(tmp_path):
    """Cleanliness is enforced for ALL roles — promoting an uncommitted tree is
    wrong regardless of role."""
    repo = _make_repo(tmp_path, "role-dirty")
    (repo / "payload.txt").write_text("uncommitted\n", encoding="utf-8")
    rc, kind, _ = _run_predicate(repo, role="seed")
    assert rc == 1
    assert kind == "dirty"


# ------------------------------------------------------------------ wiring --


def _script_lines() -> list[str]:
    return PROMOTE_SH.read_text(encoding="utf-8").splitlines()


def test_predicate_is_called_twice(tmp_path):
    """Once at Step 1 preflight, once again immediately before the plant.

    A single call site means the TOCTOU is back regardless of how green the
    behavior tests above are.
    """
    calls = [
        i
        for i, line in enumerate(_script_lines())
        if f"{FUNC_NAME}" in line and not line.lstrip().startswith("#")
        and not re.match(rf"^{FUNC_NAME}\(\)", line.lstrip())
    ]
    assert len(calls) >= 2, (
        f"expected >=2 live call sites for {FUNC_NAME}, found {len(calls)} — "
        "the plant-time re-assert is the whole fix for g-115-3514"
    )


def test_reassert_sits_between_plan_and_plant():
    """THE WIRING PROOF, and the one that would have failed before the fix.

    The re-assert must come AFTER the --plan blast-radius pass (so it captures
    drift accumulated across the slow part of the run) and BEFORE the plant
    command that copies the working tree. A re-assert placed after the plant
    would be a green test over a shipped defect.
    """
    lines = _script_lines()

    def _find(pred) -> int:
        for i, line in enumerate(lines):
            if pred(line):
                return i
        return -1

    # Match the real INVOCATIONS, never the dry-run narration of them. The
    # script's dry-run branch emits `say "[dry-run] would: seed-transplant.sh
    # ... --force --commit"`, which contains every token the plant command does
    # and sits ~80 lines EARLIER — so a token-only matcher silently anchors on
    # the echo and the ordering assertion inverts. (Cost the first run of this
    # test; same class as guard-1685 — a referent that survives inside a string
    # describing it.) A real invocation starts with `bash `.
    def _invocation(*tokens):
        def _pred(ln: str) -> bool:
            s = ln.lstrip()
            return s.startswith("bash ") and all(t in s for t in tokens)

        return _pred

    plan_idx = _find(_invocation("seed-transplant.sh", "--plan"))
    plant_idx = _find(_invocation("seed-transplant.sh", "--force", "--commit"))
    reassert_idx = _find(lambda ln: "SOURCE DRIFTED MID-PROMOTION" in ln)

    assert plan_idx != -1, "could not locate the --plan step"
    assert plant_idx != -1, "could not locate the plant (seed-transplant --force --commit)"
    assert reassert_idx != -1, "could not locate the plant-time re-assert failure message"

    assert plan_idx < reassert_idx < plant_idx, (
        f"re-assert must sit between --plan (line {plan_idx + 1}) and the plant "
        f"(line {plant_idx + 1}); found it at line {reassert_idx + 1}"
    )


def test_reassert_has_no_override_flag():
    """The plant-time re-assert is a hard fail.

    Every other gate in this script has a documented override
    (--force-past-plan, PROMOTE_ALLOW_DRIFT, --skip-preflight). This one must
    not: an operator who wants the newer code should cut a new tag, which is
    one release.sh run and keeps the label honest. An override here would
    re-open the exact hole — shipping untagged content under a tag.
    """
    lines = _script_lines()
    # Anchor on the SECTION HEADER, not the bare token: Step 1's comment
    # cross-references "Step 4a.9" by name, so a bare-token search starts the
    # block ~190 lines early and sweeps in the promotion-preflight gate's
    # legitimate PROMOTE_ALLOW_DRIFT escape — reporting a violation in a
    # different gate as if it were this one.
    start = next(
        (i for i, ln in enumerate(lines) if ln.startswith("# --- Step 4a.9:")), -1
    )
    plant = next(
        (
            i
            for i, ln in enumerate(lines)
            if ln.lstrip().startswith("bash ")
            and "seed-transplant.sh" in ln
            and "--force" in ln
            and "--commit" in ln
        ),
        -1,
    )
    assert start != -1 and plant != -1 and start < plant

    block = "\n".join(lines[start:plant])
    for escape in ("FORCE_PAST_PLAN", "PROMOTE_ALLOW_DRIFT", "ALLOW_DRIFT", "--force-past"):
        assert escape not in block, (
            f"plant-time re-assert must not honor {escape} — it is a hard fail by design"
        )


def test_script_is_syntactically_valid():
    """Cheap end-to-end guard on the refactor itself."""
    proc = subprocess.run(
        [BASH, "-n", str(PROMOTE_SH)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, f"bash -n failed: {proc.stderr}"
