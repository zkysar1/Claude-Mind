#!/usr/bin/env python3
"""Run ONE check at N tree states and emit a per-state pass/fail table.

THE MISSING AXIS. The existing triage path (`/run-test-circuit`, the solo-vs-suite
discriminators) answers "is this red real, and whose is it?" AT A SINGLE TREE STATE.
Nothing in the tree ran a check ACROSS states -- verified 2026-08-28: only three
scripts call `git worktree add` (promote-to-upstream, promotion-preflight,
_full_suite_imperative) and none replays a check over a sha list. That across-state
axis is what this module adds, and it is the whole of its contribution.

WHY IT PAYS FOR ITSELF (gap-089, both encounters measured):
  * g-306-284, 2026-08-28 -- after merging two worker-carrier refs a red had the
    exact shape of a self-inflicted regression. Replaying the one test at the two
    shas INVERTED the reading: red BEFORE, 9/9 green AFTER. The merge had FIXED
    nine pre-existing reds. Reporting them as regressions would have been wrong in
    the most expensive direction -- reverting good work.
  * g-335-422, 2026-08-05 -- per-commit replay across 23 merged tree states.

THE FOUR-OUTCOME CLASSIFIER is the reusable core, and each outcome routes the
reader somewhere DIFFERENT, which is why collapsing them loses the value:
    green -> red   REGRESSION    you broke it; bisect the span
    red   -> green FIXED         you fixed it; do NOT report as a regression
    red   -> red   PRE_EXISTING  not yours; find the owning goal
    green -> green STILL_GREEN   the check never reproduced the failure

WHAT THIS DOES **NOT** COVER, stated because a fixture seam is a silent scope
declaration (guard-1462). The fourth shape in gap-089's note -- "green solo but red
in suite = environmental" -- is a SOLO-VS-SUITE axis at ONE state, not an
across-state axis. It is structurally invisible here: this module runs one command
per state and compares states to each other, so it can never see the solo/suite
split. That axis belongs to the existing triage path and is deliberately left there.
Do not read a STILL_GREEN verdict as "not environmental" -- this module did not look.

SETUP TRAPS, each of which cost a real run to rediscover (see
_full_suite_imperative.py's WORKTREE clause, guard-4774/4940/5124/955):
  1. local-paths.conf is GITIGNORED, so a fresh worktree cannot inherit it and
     WORLD_DIR/META_DIR resolve EMPTY -- 0 passed / 106 errors, which reads as a
     catastrophic regression rather than an invalid run. It is COPIED IN per state.
  2. Do NOT export MIND_WORLD / MIND_META as a substitute: conftest.py pops both
     at module import, deliberately, so they cannot survive collection. The copied
     conf is the only channel that does.
  3. STORAGE_BACKEND=local is MANDATORY on an own-cloud box for any test runner
     (guard-955) or a tmp-world write collides on the PRODUCTION key.
  4. Worktrees live OUTSIDE the synced tree and are torn down through
     worktree-teardown.sh, which reaps the worktree's own daemon FIRST -- never a
     bare `git worktree remove` (g-328-08).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _runtime_bash import BASH, bash_cmd  # noqa: E402  guard-580

VERDICTS = ("REGRESSION", "FIXED", "PRE_EXISTING", "STILL_GREEN", "MIXED", "INDETERMINATE")


def classify(results: list) -> tuple:
    """Map an ordered list of per-state results to (verdict, reason).

    PURE -- no I/O, no git, no clock. This is the half the dogfood fixtures drive.
    `results` is ordered OLDEST-FIRST; each item needs a boolean `passed` or None
    when that state could not be evaluated.
    """
    usable = [r for r in results if r.get("passed") is not None]
    if len(usable) < 2:
        return "INDETERMINATE", (
            f"need >= 2 evaluated states to compare, got {len(usable)}"
        )

    first, last = usable[0], usable[-1]
    a, b = bool(first["passed"]), bool(last["passed"])
    flips = sum(
        1 for x, y in zip(usable, usable[1:]) if bool(x["passed"]) != bool(y["passed"])
    )

    # >2 states that oscillate: the endpoints alone would misdescribe the span.
    if flips > 1:
        return "MIXED", (
            f"{flips} pass/fail transitions across {len(usable)} states — the check "
            f"flips more than once, so no single endpoint verdict describes it; read "
            f"the per-state table and bisect each transition separately"
        )

    if a and not b:
        return "REGRESSION", (
            f"passed at {first['short']} and fails at {last['short']} — introduced "
            f"in this span"
        )
    if not a and b:
        return "FIXED", (
            f"failed at {first['short']} and passes at {last['short']} — this span "
            f"FIXED it; do NOT report as a regression"
        )
    if not a and not b:
        return "PRE_EXISTING", (
            f"fails at both {first['short']} and {last['short']} — predates this "
            f"span; find the owning goal rather than attributing it here"
        )
    return "STILL_GREEN", (
        f"passes at both {first['short']} and {last['short']} — this check never "
        f"reproduced the failure (it does not follow that nothing is wrong; the "
        f"check may not cover it, and the solo-vs-suite axis was not examined)"
    )


def _run(cmd: list, cwd=None, env=None, timeout=None):
    return subprocess.run(
        cmd, cwd=cwd, env=env, timeout=timeout,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def resolve_states(repo: Path, states: str = None, frm: str = None, to: str = None) -> list:
    """Return an ordered OLDEST-FIRST list of full shas."""
    if states:
        raw = [s.strip() for s in states.split(",") if s.strip()]
    elif frm and to:
        raw = [frm, to]
    else:
        raise SystemExit("need --states or both --from and --to")
    out = []
    for r in raw:
        p = _run(["git", "-C", str(repo), "rev-parse", r])
        if p.returncode != 0:
            raise SystemExit(f"cannot resolve state {r!r}: {p.stdout.strip()}")
        out.append(p.stdout.strip())
    return out


def replay(repo: Path, shas: list, check: str, agent: str, timeout: int,
           keep: bool = False, quiet: bool = False) -> dict:
    scripts = repo / "core" / "scripts"
    conf_rel = Path("agents") / agent / "local-paths.conf"
    conf_src = repo / conf_rel
    results = []

    for sha in shas:
        short = sha[:9]
        entry = {"sha": sha, "short": short, "passed": None, "rc": None,
                 "invalid_reason": None, "output_tail": ""}
        # OUTSIDE the synced tree, per the worktree clause.
        wt = Path(tempfile.mkdtemp(prefix=f"check-replay-{short}-"))
        try:
            os.rmdir(wt)  # `git worktree add` wants to create it
            p = _run(["git", "-C", str(repo), "worktree", "add", "--detach",
                      str(wt), sha])
            if p.returncode != 0:
                entry["invalid_reason"] = f"worktree add failed: {p.stdout.strip()[:300]}"
                results.append(entry)
                continue

            # TRAP 1 -- gitignored conf cannot be inherited; without it WORLD_DIR and
            # META_DIR resolve EMPTY and the run reads as a catastrophic regression.
            if conf_src.exists():
                dst = wt / conf_rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(conf_src, dst)
            else:
                entry["invalid_reason"] = (
                    f"{conf_rel} absent in {repo} — a worktree run without it "
                    f"resolves the external roots EMPTY and its failures are "
                    f"meaningless (guard-4774/4940/5124). Refusing to report a "
                    f"pass/fail for this state."
                )
                results.append(entry)
                continue

            env = dict(os.environ)
            env["MIND_AGENT"] = agent
            env["STORAGE_BACKEND"] = "local"          # TRAP 3 (guard-955)
            env.pop("MIND_WORLD", None)              # TRAP 2 — conftest pops these
            env.pop("MIND_META", None)
            env.setdefault("MIND_SID", os.environ.get("MIND_SID", "check-replay"))

            try:
                r = _run([BASH, "-c", check], cwd=str(wt), env=env, timeout=timeout)
                entry["rc"] = r.returncode
                entry["passed"] = (r.returncode == 0)
                entry["output_tail"] = "\n".join(
                    (r.stdout or "").strip().splitlines()[-15:]
                )
            except subprocess.TimeoutExpired:
                entry["invalid_reason"] = f"check exceeded {timeout}s"
        finally:
            if not keep:
                td = scripts / "worktree-teardown.sh"
                # TRAP 4 -- never a bare `git worktree remove` ().
                _run(bash_cmd(td, str(wt), "--owner", str(repo), "--force", "--quiet"))
            elif not quiet:
                print(f"[check-replay] kept worktree {wt}", file=sys.stderr)
        results.append(entry)

    verdict, reason = classify(results)
    return {
        "check": check,
        "repo": str(repo),
        "states": results,
        "evaluated": sum(1 for r in results if r["passed"] is not None),
        "verdict": verdict,
        "verdict_reason": reason,
    }


def render(rep: dict) -> str:
    lines = [f"check : {rep['check']}", f"repo  : {rep['repo']}", ""]
    lines.append(f"{'state':<12} {'result':<10} {'rc':<5} note")
    lines.append("-" * 72)
    for r in rep["states"]:
        if r["passed"] is None:
            res, note = "INVALID", (r["invalid_reason"] or "")[:44]
        else:
            res, note = ("pass" if r["passed"] else "FAIL"), ""
        lines.append(f"{r['short']:<12} {res:<10} {str(r['rc']):<5} {note}")
    lines.append("")
    lines.append(f"VERDICT: {rep['verdict']} — {rep['verdict_reason']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run one check at N tree states.")
    ap.add_argument("--check", required=True, help="shell command; exit 0 = pass")
    ap.add_argument("--states", help="comma-separated shas/refs, OLDEST FIRST")
    ap.add_argument("--from", dest="frm", help="2-point case: the earlier state")
    ap.add_argument("--to", dest="to", help="2-point case: the later state")
    ap.add_argument("--repo", default=None, help="defaults to PROJECT_ROOT")
    ap.add_argument("--agent", default=os.environ.get("MIND_AGENT", ""))
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--keep-worktrees", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    repo = Path(a.repo) if a.repo else Path(__file__).resolve().parents[2]
    if not a.agent:
        print("ERROR: --agent (or MIND_AGENT) required — it names whose "
              "local-paths.conf gets copied into each worktree", file=sys.stderr)
        return 2

    shas = resolve_states(repo, a.states, a.frm, a.to)
    rep = replay(repo, shas, a.check, a.agent, a.timeout,
                 keep=a.keep_worktrees, quiet=a.json)
    print(json.dumps(rep, indent=2) if a.json else render(rep))
    # rc mirrors the VERDICT so a caller can branch without parsing:
    #   0 nothing broken here (STILL_GREEN / FIXED)
    #   1 something to act on (REGRESSION / PRE_EXISTING / MIXED)
    #   2 the measurement itself is invalid
    if rep["verdict"] in ("STILL_GREEN", "FIXED"):
        return 0
    if rep["verdict"] == "INDETERMINATE":
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
