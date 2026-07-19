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

    # ── F-I: git_log lineage exemption (2) ────────────────────────
    # A follow-up filed from a just-closed goal matches the parent's OWN
    # tagged commit — 5th lineage false-positive shape, observed live when
    # filing 1 against parent 6's commit e6032338. Build a
    # second repo whose commit carries the conventional tag.
    with tempfile.TemporaryDirectory() as td:
        repo2 = Path(td) / "repo2"
        repo2.mkdir()
        _git(repo2, "init", "-q")
        _git(repo2, "config", "user.email", "test@example.com")
        _git(repo2, "config", "user.name", "git-log-test")
        target = repo2 / "core" / "scripts" / "widget_xyz.py"
        target.parent.mkdir(parents=True)
        target.write_text("# widget\n", encoding="utf-8")
        _git(repo2, "add", "-A")
        _git(repo2, "commit", "-q", "-m", "feat(g-888-77): harden widget_xyz")

        # F: discovered_by == the commit's tag -> PASS with lineage advisory
        rf = goal_duplication._check_git_log(
            {"discovered_by": "g-888-77"},
            {"core/scripts/widget_xyz.py"}, repo2)
        if rf.get("passed") is not True:
            failures.append(
                "F: expected PASS on discovered_by-parent commit tag; got "
                f"passed={rf.get('passed')} reason={rf.get('reason')!r}")
        elif not any(a.get("lineage_exempt") for a in rf.get("advisories", [])):
            failures.append(
                "F: passed but no lineage_exempt advisory recorded; "
                f"advisories={rf.get('advisories')}")

        # G: UNRELATED discovered_by -> the same tagged commit still BLOCKS
        rg = goal_duplication._check_git_log(
            {"discovered_by": "g-888-78"},
            {"core/scripts/widget_xyz.py"}, repo2)
        if rg.get("passed") is not False:
            failures.append(
                "G: expected BLOCK when discovered_by does not match the "
                f"commit tag; got passed={rg.get('passed')}")

        # H: origin_signal-embedded parent id -> PASS with lineage advisory
        rh = goal_duplication._check_git_log(
            {"origin_signal": "idea:g-888-77-follow-up-hardening"},
            {"core/scripts/widget_xyz.py"}, repo2)
        if rh.get("passed") is not True:
            failures.append(
                "H: expected PASS on origin_signal-embedded parent id; got "
                f"passed={rh.get('passed')} reason={rh.get('reason')!r}")

        # I: PREFIX guard — discovered_by  must NOT exempt the
        #  tag (shorter-id-inside-longer-id class, fresh-eyes F1 of
        # 6); the block must hold.
        ri = goal_duplication._check_git_log(
            {"discovered_by": "g-888-7"},
            {"core/scripts/widget_xyz.py"}, repo2)
        if ri.get("passed") is not False:
            failures.append(
                "I: expected BLOCK — prefix id g-888-7 must not exempt tag "
                f"g-888-77; got passed={ri.get('passed')}")

    # ── J-O: git_log self-completion demotion (5) ─────────────────
    # A commit whose tag maps to a goal the FILING agent itself completed
    # (per team-state recent_completions) demotes to a visible advisory;
    # partner-completed tags and untagged commits still hard-block. Basis:
    # 4 telemetry (19 solo git_log attempts / 9 overridden all
    # verified-FP / 0 demonstrated TPs).
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        repo3 = base / "repo3"
        repo3.mkdir()
        _git(repo3, "init", "-q")
        _git(repo3, "config", "user.email", "test@example.com")
        _git(repo3, "config", "user.name", "git-log-test")
        target = repo3 / "core" / "scripts" / "widget_xyz.py"
        target.parent.mkdir(parents=True)
        target.write_text("# widget\n", encoding="utf-8")
        _git(repo3, "add", "-A")
        _git(repo3, "commit", "-q", "-m", "feat(g-888-90): ship widget_xyz")

        world = base / "world"
        world.mkdir()
        (world / "team-state.yaml").write_text(
            "recent_completions:\n"
            "  - goal_id: g-888-90\n"
            "    completed_by: zeta-test\n"
            "  - goal_id: g-888-92\n"
            "    completed_by: other-agent\n",
            encoding="utf-8",
        )
        fp = {"core/scripts/widget_xyz.py"}

        # J: self-completed tag -> PASS with self_completion_exempt advisory
        rj = goal_duplication._check_git_log(
            {}, fp, repo3, self_agent="zeta-test", world_dir=world)
        if rj.get("passed") is not True:
            failures.append(
                "J: expected PASS on self-completed commit tag; got "
                f"passed={rj.get('passed')} reason={rj.get('reason')!r}")
        elif not any(a.get("self_completion_exempt")
                     for a in rj.get("advisories", [])):
            failures.append(
                "J: passed but no self_completion_exempt advisory recorded; "
                f"advisories={rj.get('advisories')}")

        # K: same commit, DIFFERENT filing agent (tag completed by
        # zeta-test, filer is other-agent) -> BLOCK (partner-completed tag;
        # N-agent invariant's cross-agent detection intact)
        rk = goal_duplication._check_git_log(
            {}, fp, repo3, self_agent="other-agent", world_dir=world)
        if rk.get("passed") is not False:
            failures.append(
                "K: expected BLOCK when the tagged goal was completed by a "
                f"DIFFERENT agent; got passed={rk.get('passed')}")

        # N: tag present but goal-id absent from recent_completions -> BLOCK
        # ( is other-agent's; the commit tag  IS in
        # recent_completions — so use a fresh world with neither id)
        world2 = base / "world2"
        world2.mkdir()
        (world2 / "team-state.yaml").write_text(
            "recent_completions:\n"
            "  - goal_id: g-777-01\n"
            "    completed_by: zeta-test\n",
            encoding="utf-8",
        )
        rn = goal_duplication._check_git_log(
            {}, fp, repo3, self_agent="zeta-test", world_dir=world2)
        if rn.get("passed") is not False:
            failures.append(
                "N: expected BLOCK when commit tag maps to no self "
                f"recent_completion; got passed={rn.get('passed')}")

        # L: untagged self commit -> BLOCK (no tag = no attribution = no
        # demotion, fail-conservative)
        repo4 = base / "repo4"
        repo4.mkdir()
        _git(repo4, "init", "-q")
        _git(repo4, "config", "user.email", "test@example.com")
        _git(repo4, "config", "user.name", "git-log-test")
        t4 = repo4 / "core" / "scripts" / "widget_xyz.py"
        t4.parent.mkdir(parents=True)
        t4.write_text("# widget\n", encoding="utf-8")
        _git(repo4, "add", "-A")
        _git(repo4, "commit", "-q", "-m", "ship widget_xyz untagged")
        rl = goal_duplication._check_git_log(
            {}, fp, repo4, self_agent="zeta-test", world_dir=world)
        if rl.get("passed") is not False:
            failures.append(
                "L: expected BLOCK on untagged commit despite self_agent + "
                f"world args; got passed={rl.get('passed')}")

        # M: mixed self-tagged + partner-tagged commits -> BLOCK overall,
        # self match demoted to advisory, partner match in matches
        t4b = repo3 / "core" / "scripts" / "widget_xyz.py"
        t4b.write_text("# widget v2\n", encoding="utf-8")
        _git(repo3, "add", "-A")
        _git(repo3, "commit", "-q", "-m", "feat(g-888-92): rework widget_xyz")
        rm_ = goal_duplication._check_git_log(
            {}, fp, repo3, self_agent="zeta-test", world_dir=world)
        if rm_.get("passed") is not False:
            failures.append(
                "M: expected BLOCK on mixed self+partner tagged commits; got "
                f"passed={rm_.get('passed')}")
        else:
            if not any(a.get("self_completion_exempt")
                       for a in rm_.get("advisories", [])):
                failures.append(
                    "M: blocked but the self-completed match was not demoted "
                    f"to an advisory; advisories={rm_.get('advisories')}")
            if not any("g-888-92" in (m.get("commit") or "")
                       for m in rm_.get("matches", [])):
                failures.append(
                    "M: blocking matches must contain the partner-tagged "
                    f"commit; matches={rm_.get('matches')}")

        # O: parent-lineage demotion unaffected when new args are passed —
        # discovered_by parent tag still demotes even with self_agent +
        # world_dir supplied (and priority order lineage-before-self holds:
        #  is BOTH lineage parent and self-completed; it must carry
        # the lineage attribution)
        repo5 = base / "repo5"
        repo5.mkdir()
        _git(repo5, "init", "-q")
        _git(repo5, "config", "user.email", "test@example.com")
        _git(repo5, "config", "user.name", "git-log-test")
        t5 = repo5 / "core" / "scripts" / "widget_xyz.py"
        t5.parent.mkdir(parents=True)
        t5.write_text("# widget\n", encoding="utf-8")
        _git(repo5, "add", "-A")
        _git(repo5, "commit", "-q", "-m", "feat(g-888-90): ship widget_xyz")
        ro = goal_duplication._check_git_log(
            {"discovered_by": "g-888-90"}, fp, repo5,
            self_agent="zeta-test", world_dir=world)
        if ro.get("passed") is not True:
            failures.append(
                "O: expected PASS via lineage demotion with new args present; "
                f"got passed={ro.get('passed')} reason={ro.get('reason')!r}")
        elif not any(a.get("lineage_exempt")
                     for a in ro.get("advisories", [])):
            failures.append(
                "O: lineage attribution must win over self-completion when "
                "the tag is both (most-specific-first); "
                f"advisories={ro.get('advisories')}")

    if failures:
        print(f"FAIL ({len(failures)} cases)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS (15/15 cases)")
    return 0


def test_git_log_date_and_specificity():
    """Pytest-collectable wrapper so this regression joins the
    `pytest core/scripts/tests` suite, not only the standalone run."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
