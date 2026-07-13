"""test_goal_duplication_gate_git_log.py — regression test for the
git_log_48h check date fix + bare-basename specificity floor (g-115-1166).

Two coupled bugs in core/scripts/gates/goal_duplication.py `_check_git_log`,
both surfaced by probing the check at its ACTUAL corpus size:

  1. DATE (dead check): `--since=48h` is NOT a valid git approxidate. git
     2.45.1 returns 0 commits for `48h` (81 for `48.hours.ago`), silently
     disabling the entire check — telemetry showed 0/15155 dup-gate firings
     ever tripped git_log_48h while every sibling check fired 100s-1000s of
     times. Fixed to `--since=48.hours.ago`.
  2. SPECIFICITY (over-fire once un-broken): the file-path match was a
     bidirectional substring (`fp in line or line in fp`). On the now-live
     48h corpus (~200 paths, dominated by frequently-churned state files
     whose basenames repeat across dirs) a bare basename like a generic
     `.md`/`.jsonl` name over-fires against every committed path sharing it.
     Fixed to require a qualified path (contains "/") for substring matching;
     bare basenames need an exact match.

These two interact: fixing the date without the specificity floor would turn
a dead check straight into a noisy one, so both land together.

The check reads the REAL git history of its `project_root`, so — unlike the
subprocess-against-CLI sibling tests (structural_co_signal, partner_in_flight,
pending_queue) — this test calls `_check_git_log` directly against a throwaway
git repo it builds, giving a deterministic 48h corpus.

Cases:
  A  qualified path matching the committed file  -> BLOCK (passed=False)
     Proves bug #1 fix: old `48h` saw 0 commits, so this would have PASSED.
  B  bare basename colliding with the committed   -> PASS  (passed=True)
     Proves bug #2 fix: old substring matched "widget_xyz.py" inside the
     committed "core/scripts/widget_xyz.py" -> false-positive block.
  C  qualified partial path (trailing segment)    -> BLOCK (passed=False)
     Qualified paths still substring-match either direction.
  D  completed-Maintain goal, qualified-path overlap -> PASS (passed=True)
     g-115-1813: a status=completed Maintain goal names files touched by its
     OWN just-shipped commit within 48h — that overlap IS the completion
     evidence, not duplication. Mirrors _check_target_state's g-115-836 skip.
  E  completed NON-Maintain goal, same overlap     -> BLOCK (passed=False)
     Symmetry guard: the carve-out is Maintain-title-scoped, not a blanket
     completed-goal exemption.

Filed by g-115-1166 (corpus-size co-signal calibration audit, zeta).
Cases D/E added by g-115-1813 (git_log completed-Maintain carve-out, alpha).
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))
sys.path.insert(0, str(CORE_SCRIPTS / "gates"))

import goal_duplication  # type: ignore  # noqa: E402


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(repo), check=True,
        capture_output=True, text=True,
    )


def _build_repo(tmp: Path) -> Path:
    """git init a throwaway repo and commit one qualified file (timestamp
    = now, so it lands inside the 48-hour window)."""
    repo = tmp / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "git-log-test")
    target = repo / "core" / "scripts" / "widget_xyz.py"
    target.parent.mkdir(parents=True)
    target.write_text("# widget\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add widget_xyz.py")
    return repo


def _check(repo: Path, file_paths):
    # A/B/C pass {} as the goal — an empty dict never triggers the 3
    # completed-Maintain carve-out (status is None), so these cases exercise
    # the file-path matching path. Cases D/E build explicit goal dicts to test
    # the carve-out (which now reads goal.status + goal.title).
    return goal_duplication._check_git_log({}, set(file_paths), repo)


def main() -> int:
    failures = []
    with tempfile.TemporaryDirectory() as td:
        repo = _build_repo(Path(td))

        # ── A: qualified path matching the committed file → BLOCK ──────────
        # Old `--since=48h` returned 0 commits, so the check would have
        # passed (no overlap). With the date fixed it sees the commit and the
        # exact-path match fires.
        ra = _check(repo, {"core/scripts/widget_xyz.py"})
        if ra.get("passed") is not False:
            failures.append(
                "A: expected BLOCK (passed=False) on qualified-path overlap; "
                f"got passed={ra.get('passed')} reason={ra.get('reason')!r} "
                "(date filter may still be broken — `48h` regression)"
            )

        # ── B: bare basename colliding with committed path → PASS ──────────
        # "widget_xyz.py" is a substring of "core/scripts/widget_xyz.py".
        # The old bidirectional substring over-fired here; the specificity
        # floor requires a qualified path, so a bare name needs exact match.
        rb = _check(repo, {"widget_xyz.py"})
        if rb.get("passed") is not True:
            failures.append(
                "B: expected PASS (passed=True) on bare-basename collision; "
                f"got passed={rb.get('passed')} matches={rb.get('matches')} "
                "(specificity floor missing — bare basename over-fires)"
            )

        # ── C: qualified partial path (trailing segment) → BLOCK ───────────
        # "scripts/widget_xyz.py" contains "/" and is a substring of the
        # committed full path — qualified paths still match.
        rc = _check(repo, {"scripts/widget_xyz.py"})
        if rc.get("passed") is not False:
            failures.append(
                "C: expected BLOCK (passed=False) on qualified partial-path "
                f"overlap; got passed={rc.get('passed')} reason={rc.get('reason')!r}"
            )

        # ── D: completed-Maintain goal w/ qualified-path overlap → PASS ────
        # 3: a status=completed Maintain goal names framework files
        # touched by its OWN just-shipped commit in the 48h window — that
        # overlap IS the completion evidence, not duplication. The carve-out
        # (mirroring _check_target_state's  skip) passes it despite
        # the identical qualified-path overlap that BLOCKS in case A.
        maintain_goal = {"status": "completed",
                         "title": "Maintain: harden widget_xyz.py"}
        rd = goal_duplication._check_git_log(
            maintain_goal, {"core/scripts/widget_xyz.py"}, repo)
        if rd.get("passed") is not True:
            failures.append(
                "D: expected PASS (passed=True) on completed-Maintain carve-out "
                f"despite qualified-path overlap; got passed={rd.get('passed')} "
                f"reason={rd.get('reason')!r} (g-115-1813 skip missing)"
            )
        elif "Maintain" not in (rd.get("reason") or ""):
            failures.append(
                "D: carve-out passed but reason does not cite the Maintain "
                f"skip; got reason={rd.get('reason')!r}"
            )

        # ── E: completed NON-Maintain goal w/ overlap → BLOCK (symmetry) ───
        # The carve-out is scoped to Maintain: titles. A completed goal with a
        # different title prefix must STILL block on the same overlap — proves
        # the 3 skip is Maintain-scoped, not a blanket completed-goal
        # exemption (run-full-suite testSymmetry discipline).
        nonmaintain_goal = {"status": "completed",
                            "title": "Fix: rewrite widget_xyz.py"}
        re_ = goal_duplication._check_git_log(
            nonmaintain_goal, {"core/scripts/widget_xyz.py"}, repo)
        if re_.get("passed") is not False:
            failures.append(
                "E: expected BLOCK (passed=False) on completed NON-Maintain "
                "goal (carve-out must be Maintain-scoped); got "
                f"passed={re_.get('passed')}"
            )

    if failures:
        print(f"FAIL ({len(failures)} cases)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS (5/5 cases)")
    return 0


def test_git_log_date_and_specificity():
    """Pytest-collectable wrapper so this regression joins the
    `pytest core/scripts/tests` suite, not only the standalone run."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
