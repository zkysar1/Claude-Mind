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

`--test-ids` ADDS A SECOND, FINER READOUT over the same runs (gap-183). Each
state's booleans come from ONE exit code, so a `--check` that runs MANY tests
collapses to one bit and `PRE_EXISTING` becomes structurally unable to see a
change that FIXED three old failures and INTRODUCED two new ones -- red at both
ends, "not yours", and two of them are yours. With the flag the module also
captures the failing TEST IDs per state and two-way diffs them:
`new_at_last` (not pre-existing, whatever the boolean verdict says),
`gone_at_last` (fixed, or MASKED), `common` (genuinely pre-existing). The set
diff never overrides the boolean verdict -- it sits beside it, because callers
branch on the exit code and that contract predates this mode.

`--confirm-with` pins the LOAD as well as the sha: the two states ran at
different times under different machine load, so a contention-flaky test looks
like a brand-new failure. Measured 2026-08-31 (echo): an uncontrolled A/B of
this exact shape produced SIX phantom NEW-REDs. The flag re-runs ONLY the
candidates at the first state and retracts any that fail there too.

WHAT THIS DOES **NOT** COVER, stated because a fixture seam is a silent scope
declaration (guard-1462). The fourth shape in gap-089's note -- "green solo but red
in suite = environmental" -- is a SOLO-VS-SUITE axis at ONE state, not an
across-state axis. It is structurally invisible here: this module runs one command
per state and compares states to each other, so it can never see the solo/suite
split. That axis belongs to the existing triage path and is deliberately left there.
Do not read a STILL_GREEN verdict as "not environmental" -- this module did not look.
`--test-ids` does not change that: it refines WHICH tests are red at each state,
never WHY, so a set-level `NO_NEW` is still silent on the environmental axis.

The set-level readout's own limits, since it invites over-reading: the IDs come
from pytest's short-summary block, so a `--check` that is not pytest (or that
suppresses that block with -rN) yields `ids_source: "absent"` and the diff
REFUSES with `status: "unknown"` rather than reporting an empty new-failure set.
`gone_at_last` is NOT proof of a fix -- a collection error stops a test running
at all, which is indistinguishable from a pass at set level.

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
import re
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


_SUMMARY_HEADER_RE = re.compile(r"^=+ *short test summary info *=+", re.M)
_FAILING_ID_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.M)


def parse_failing_ids(text: str, returncode: int):
    """Return (failing_test_ids, source) for ONE state's captured output.

    THREE-VALUED ON PURPOSE, and that IS the function (verify-before-assuming.md,
    the wrong-surface-zero class):

        ([],    "clean-exit")   exited 0 -- genuinely no failures
        ([...], "summary")      parsed from pytest's own short-summary block
        (None,  "absent")       failed, and no summary block was found
        (None,  "summary-empty") failed, summary present, but it named no test

    An UNKNOWN set must never collapse into an EMPTY one. `PRE_EXISTING` beside
    an empty new-failure set reads as "proved not yours"; the same sentence built
    from an unparsed log proves nothing at all, and that is the exact false
    negative this mode exists to remove. Both None cases say "I could not look".

    ANCHORED, never a token grep (guard-3918). `FAILED`/`ERROR` matches TEST
    NAMES too -- `test_failed_state_is_skipped` carries the token -- so the match
    is pinned to the START of a line INSIDE the short-summary block, which is
    where pytest writes the outcome as the line's FIRST field. A test whose name
    contains the token can only ever appear as the second field, and cannot match.
    """
    if returncode == 0:
        return [], "clean-exit"
    heads = list(_SUMMARY_HEADER_RE.finditer(text or ""))
    if not heads:
        return None, "absent"
    tail = (text or "")[heads[-1].end():]
    ids = sorted({m.group(1) for m in _FAILING_ID_RE.finditer(tail)})
    if not ids:
        # Non-zero exit whose summary names no failing test: the command died for
        # a reason the summary does not describe. Reporting [] here would assert
        # "zero failures" over a failed run -- the wrong-surface zero again.
        return None, "summary-empty"
    return ids, "summary"


def diff_failing_ids(results: list) -> dict:
    """Two-way set diff of failing test IDs between the first and last usable state.

    PURE. The companion to `classify()`, and the reason this module can answer
    gap-183 at all: `classify()` reduces each state to ONE BOOLEAN, so a span that
    is red at both ends reads `PRE_EXISTING` even when the two reds are DIFFERENT
    TEST SETS. The boolean cannot see that; the sets can. Concretely, a change
    that fixes three old failures and introduces two new ones is red-at-both, and
    a boolean-only reading calls the two new ones "not yours".

        new_at_last   failing at the LAST state, not at the first
                      -> CANDIDATES caused by this span, hiding under PRE_EXISTING
        gone_at_last  failing at the FIRST state, not at the last
                      -> fixed by the span, OR MASKED (a collection error stops a
                         test running at all, which is indistinguishable from a fix
                         at set level -- non-empty here is a prompt to look, not a
                         victory)
        common        failing at both -> genuinely pre-existing

    REFUSES on an unknown set at either end rather than differencing against None.
    `status: "unknown"` is a statement of ignorance and must NOT be read as
    `new_at_last: []` -- BOTH BUCKETS are reported on every path so a caller
    cannot derive one from the other (guard-4374).
    """
    empty = {"new_at_last": [], "gone_at_last": [], "common": []}
    usable = [r for r in results if r.get("passed") is not None]
    if len(usable) < 2:
        return dict(empty, status="unknown", set_verdict="UNKNOWN",
                    reason=("need >= 2 evaluated states to diff failing sets, got "
                            f"{len(usable)}"))
    first, last = usable[0], usable[-1]
    a, b = first.get("failing_ids"), last.get("failing_ids")
    if a is None or b is None:
        blind = ", ".join(s["short"] for s in (first, last)
                          if s.get("failing_ids") is None)
        return dict(
            empty, status="unknown", set_verdict="UNKNOWN",
            reason=(
                f"failing-test IDs are UNREADABLE at state(s) {blind} "
                f"(ids_source={first.get('ids_source')}/{last.get('ids_source')}) "
                "-- the check failed there without emitting a parseable pytest "
                "short-summary block, so the failing SET is unknown. AN UNKNOWN "
                "SET IS NOT AN EMPTY ONE: this run cannot prove any failure "
                "pre-existing. Re-run with a check that prints the short test "
                "summary (do not pass -rN / -p no:terminal)."),
        )
    sa, sb = set(a), set(b)
    new, gone, common = sorted(sb - sa), sorted(sa - sb), sorted(sa & sb)
    if new:
        verdict, reason = "NEW_FAILURES", (
            f"{len(new)} test(s) fail at {last['short']} and NOT at "
            f"{first['short']} -- these are NOT pre-existing, whatever the "
            f"boolean verdict says. CONTENTION CAN MANUFACTURE THEM: the two "
            f"states ran at different wall-clock times under different machine "
            f"load, and an uncontrolled A/B has produced phantom NEW-REDs before "
            f"(6 of them, 2026-08-31, echo). Pin the LOAD as well as the sha -- "
            f"pass --confirm-with to re-run these {len(new)} at "
            f"{first['short']} before attributing any of them to this span.")
    elif common:
        verdict, reason = "NO_NEW", (
            f"the same {len(common)} failure(s) at {first['short']} and "
            f"{last['short']} with none added -- genuinely pre-existing at set "
            f"level, not merely at boolean level"
            + (f"; {len(gone)} additional failure(s) present at "
               f"{first['short']} are gone at {last['short']} (fixed, or MASKED "
               f"-- confirm they still run)" if gone else ""))
    else:
        verdict, reason = "NO_NEW", (
            f"no failing test at {last['short']} that was not already failing at "
            f"{first['short']}"
            + (f"; {len(gone)} failure(s) at {first['short']} are gone (fixed, or "
               f"MASKED -- confirm they still run)" if gone else ""))
    return {"status": "ok", "set_verdict": verdict, "reason": reason,
            "new_at_last": new, "gone_at_last": gone, "common": common}


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
           keep: bool = False, quiet: bool = False, want_ids: bool = False,
           confirm_with: str = None) -> dict:
    scripts = repo / "core" / "scripts"
    conf_rel = Path("agents") / agent / "local-paths.conf"
    conf_src = repo / conf_rel
    results = []

    for sha in shas:
        short = sha[:9]
        entry = {"sha": sha, "short": short, "passed": None, "rc": None,
                 "invalid_reason": None, "output_tail": "",
                 "failing_ids": None, "ids_source": None}
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
                if want_ids:
                    # Parsed from the FULL captured stdout, never from
                    # output_tail -- a 15-line tail truncates the summary block
                    # on any real suite, and a truncated parse yields a short
                    # set that looks like a real one.
                    entry["failing_ids"], entry["ids_source"] = parse_failing_ids(
                        r.stdout or "", r.returncode)
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
    rep = {
        "check": check,
        "repo": str(repo),
        "states": results,
        "evaluated": sum(1 for r in results if r["passed"] is not None),
        "verdict": verdict,
        "verdict_reason": reason,
    }
    if want_ids:
        delta = diff_failing_ids(results)
        if confirm_with and delta["new_at_last"]:
            delta["confirmation"] = confirm_new_at_baseline(
                repo, shas[0], agent, timeout, keep, quiet, confirm_with,
                delta["new_at_last"])
            c = delta["confirmation"]
            if c["status"] == "ok":
                delta["retracted_by_confirm"] = c["retracted"]
                delta["new_at_last"] = c["confirmed"]
                if not c["confirmed"]:
                    delta["set_verdict"] = "NO_NEW"
                    delta["reason"] = (
                        "every candidate new failure ALSO fails at "
                        f"{shas[0][:9]} when re-run there -- they were "
                        "contention or ordering artifacts, not this span's "
                        "work. This is the phantom-NEW-RED case the confirm "
                        "run exists to catch.")
        rep["id_delta"] = delta
    return rep


def confirm_new_at_baseline(repo: Path, base_sha: str, agent: str, timeout: int,
                            keep: bool, quiet: bool, confirm_with: str,
                            candidates: list) -> dict:
    """Re-run the CANDIDATE new failures at the FIRST state -- pin LOAD, not sha.

    The two-way diff compares two runs taken at different wall-clock times under
    different machine load, so a test that is merely flaky-under-contention
    appears as a brand-new failure caused by the span. Measured 2026-08-31
    (echo): an uncontrolled A/B of this exact shape produced SIX phantom
    NEW-REDs. Re-running just the candidates at the baseline sha is the cheap
    control -- it costs len(candidates) tests, not a second full suite.

    Reuses `replay()` at a single sha rather than re-implementing the worktree
    setup, so the four setup traps stay encoded in exactly one place. The
    one-state INDETERMINATE verdict from that inner call is expected and unused;
    only its failing-ID set is read.
    """
    cmd = confirm_with.replace("{ids}", " ".join(candidates))
    sub = replay(repo, [base_sha], cmd, agent, timeout, keep=keep, quiet=True,
                 want_ids=True)
    st = sub["states"][0]
    ids = st.get("failing_ids")
    if ids is None:
        return {"status": "unknown", "confirmed": list(candidates), "retracted": [],
                "baseline_failing": None,
                "reason": (
                    f"the confirm run at {base_sha[:9]} produced no readable "
                    f"failing set (ids_source={st.get('ids_source')}, rc="
                    f"{st.get('rc')}, invalid_reason={st.get('invalid_reason')}). "
                    "A candidate that does not EXIST at the baseline state also "
                    "lands here -- pytest exits on a collection error and prints "
                    "no summary. Nothing was retracted; the candidates remain "
                    "UNCONFIRMED, which is not the same as confirmed.")}
    base = set(ids)
    retracted = sorted(c for c in candidates if c in base)
    confirmed = sorted(c for c in candidates if c not in base)
    return {"status": "ok", "confirmed": confirmed, "retracted": retracted,
            "baseline_failing": ids,
            "reason": (f"{len(confirmed)} of {len(candidates)} candidate(s) "
                       f"survive a re-run at {base_sha[:9]}; {len(retracted)} "
                       f"also fail there and were contention/ordering artifacts, "
                       f"not this span's work")}


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
    d = rep.get("id_delta")
    if d:
        lines.append("")
        lines.append(f"SET-LEVEL: {d['set_verdict']} — {d['reason']}")
        # ALL THREE BUCKETS PRINT ON EVERY PATH, including the empty ones
        # (guard-4374): a reader must never have to derive one bucket from
        # another, and an absent line reads as zero when it means unknown.
        for label, key in (("new at last ", "new_at_last"),
                           ("gone at last", "gone_at_last"),
                           ("common      ", "common")):
            v = d.get(key) or []
            shown = ", ".join(v[:8]) + (" …" if len(v) > 8 else "") if v else "-"
            lines.append(f"  {label} ({len(v)}): {shown}")
        c = d.get("confirmation")
        if c:
            lines.append(f"  CONFIRM {c['status']}: {c['reason']}")
            if d.get("retracted_by_confirm"):
                lines.append("  retracted: " + ", ".join(d["retracted_by_confirm"]))
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
    ap.add_argument("--test-ids", action="store_true",
                    help="also capture the failing TEST IDs at each state and "
                         "two-way diff them. A red-at-both span whose new_at_last "
                         "is NON-EMPTY is NOT pre-existing, however confidently "
                         "the boolean verdict says PRE_EXISTING")
    ap.add_argument("--confirm-with", default=None, metavar="TEMPLATE",
                    help="shell template containing {ids}; re-runs the candidate "
                         "new failures at the FIRST state so LOAD is pinned as "
                         "well as sha. Requires --test-ids")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    repo = Path(a.repo) if a.repo else Path(__file__).resolve().parents[2]
    if a.confirm_with and not a.test_ids:
        print("ERROR: --confirm-with requires --test-ids — there are no "
              "candidate new failures to confirm without the set-level diff",
              file=sys.stderr)
        return 2
    if a.confirm_with and "{ids}" not in a.confirm_with:
        print("ERROR: --confirm-with template must contain {ids} — that is where "
              "the candidate test IDs are substituted", file=sys.stderr)
        return 2
    if not a.agent:
        print("ERROR: --agent (or MIND_AGENT) required — it names whose "
              "local-paths.conf gets copied into each worktree", file=sys.stderr)
        return 2

    shas = resolve_states(repo, a.states, a.frm, a.to)
    rep = replay(repo, shas, a.check, a.agent, a.timeout,
                 keep=a.keep_worktrees, quiet=a.json, want_ids=a.test_ids,
                 confirm_with=a.confirm_with)
    print(json.dumps(rep, indent=2) if a.json else render(rep))
    # rc mirrors the VERDICT so a caller can branch without parsing:
    #   0 nothing broken here (STILL_GREEN / FIXED)
    #   1 something to act on (REGRESSION / PRE_EXISTING / MIXED)
    #   2 the measurement itself is invalid
    # A caller that ASKED for the set-level answer and did not get one has an
    # invalid measurement for the question it asked, even when the boolean
    # verdict is clean -- so the unknown outranks the 0 below (rc 2 = invalid).
    if a.test_ids and rep.get("id_delta", {}).get("status") == "unknown":
        return 2
    if rep["verdict"] in ("STILL_GREEN", "FIXED"):
        return 0
    if rep["verdict"] == "INDETERMINATE":
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
