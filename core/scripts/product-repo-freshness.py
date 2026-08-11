#!/usr/bin/env python3
"""Advisory behind/ahead report for the product repos a goal will actually touch.

WHY (g-115-4041, sig-48). `guard-1939` says "fetch the product repo before
executing its goal", and the domain `pre-execution.md` Step 2 already makes a
pull MANDATORY for any shared-checkout goal. Both are correct, both are
retrievable, and neither fired on the g-335-572 near-miss: a clone one commit
behind produced a confident wrong finding, corroborated by three further LOCAL
signals that were all downstream of the same staleness and therefore agreed
with each other.

The reason it did not fire is measurable rather than a matter of discipline.
Step 2 enumerated its shared checkouts as two hardcoded absolute roots. On a
Linux box neither path exists, and the documented escape hatch ("any other root
named in the executing agent's self.md Primary Workspace") is empty too --
`self.md` files here carry no such heading. So the MANDATORY step iterated an
EMPTY SET and reported success, on every Linux box in the fleet, while the
correct roots sat in `AGENT_WRITE_PATH` in each agent's `local-paths.conf` --
a config the step never read.

That is why this ships as an enumeration routed through the existing
`_path_roots.compute_allowed_roots()` SSOT rather than as another literal list:
a second hand-maintained copy of the roots is what produced the empty set in
the first place, and it would go stale the same way.

WHY A NEW SCRIPT RATHER THAN AN EXTENSION (the question g-115-4041 asked first,
and the answer is not "because writing one was easier"). `gap-018` is terminal
with status `satisfied-by-extension`, and its satisfier is
`bash core/scripts/backend-cat.sh head <path>` (forged skill `probe-governed-store`).
That mechanism answers "is this GOVERNED-STORE object fresh" by heading an S3
key. It has no git notion at all -- no remote, no branch, no ahead/behind -- so
it cannot carry the product-repo case no matter how far it is extended. What
IS extended, deliberately, is everything that already existed and fits:
`_path_roots.compute_allowed_roots` for the roots, `pre-execution.md` Step 2 for
the call site, and `guard-1939` for the prescription. No new protocol, no new
registry entry, no new gap.

POSTURE: advisory, cheap, and silent when it has nothing to say -- deliberately
matching `pre-edit-context-gate` / `full-suite-recommender`. It never blocks,
never mutates, and always exits 0.

VERIFIED IN THE PRODUCTION INVOCATION SHAPE on cc-03, Linux 6.8.0-136-generic,
2026-07-31 -- box and OS recorded because a portability claim without them is
unreconcilable later (the run-full-suite baseline table spent a day unable to
reconcile two runs for exactly this omission). Production shape means: invoked
from PROJECT_ROOT via the Bash tool with `MIND_AGENT` injected by the
PreToolUse hook, which is how Phase 3.9 reaches it through
`load-conventions.sh pre-execution` (execute-protocol-digest.md:26-28). Three
shapes were measured side by side, and the second is the one that mattered:
with `MIND_AGENT` unset the first build printed NOTHING and exited 0 --
byte-identical to an all-clear -- which is the defect this script exists to fix,
reproduced inside it. Hence the CANNOT CHECK branch in main(). First real run
found StartAyoServerEnvironment 3 commits behind on a repo a live pending goal
names.

COST CONTROL is why `--goal-id` filtering is load-bearing, not a convenience.
`AGENT_WRITE_PATH` here spans roots holding dozens of repos; fetching all of
them on every goal would be a per-iteration network tax large enough that the
step would (rightly) get skipped, which is the failure mode being fixed. So the
default is to fetch ONLY repos whose directory name appears in the goal's
title/description, and to do nothing at all when none do.

SWEEP MODE (g-335-833) answers a DIFFERENT question and is deliberately not
folded into the above. The per-goal path asks "is the repo I am about to edit
stale", scoped to repos the goal names. `--sweep` asks "is there work on this
box that exists nowhere else", across every enumerated repo and every local
branch — the a55add9 class, where a completed fix was committed to a local
main, never pushed, and found five days later after the same work had been
redone upstream. The scoping is the whole point: that incident happened in a
repo no goal named, so a goal-scoped check could never have seen it.

Usage:
    product-repo-freshness.py --goal-id g-NNN-NN [--source world|agent]
    product-repo-freshness.py --repo <path> [--repo <path> ...]
    product-repo-freshness.py --list          # enumerate, no fetch
    product-repo-freshness.py --sweep         # unpushed + stale-dirty, ALL repos
Options:
    --json        machine-readable output
    --no-fetch    report against the already-known remote ref (no network)
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

FETCH_TIMEOUT_S = 25
DIRTY_AGE_HOURS = 24


def _git(repo, *args, timeout=10):
    """Run git in `repo`. Returns (rc, stdout, stderr); never raises."""
    try:
        p = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as exc:  # timeout, missing git, permission — all advisory
        return 1, "", "%s: %s" % (type(exc).__name__, exc)


def _is_repo(p):
    return (Path(p) / ".git").exists()


def enumerate_repos():
    """Every git repo reachable from AGENT_WRITE_PATH, via the shared SSOT.

    Routed through `_path_roots.compute_allowed_roots()` — the SAME function the
    L1 path-resolution hook uses to decide what this agent may write to — and
    filtered to its `AGENT_WRITE_PATH` labels. So the semicolon-split lives in
    exactly one place (`_path_roots.py:153`), and an AGENT_WRITE_PATH edit
    reaches this call site with no second list to update. A root may itself be
    a repo (a single checkout) or a parent holding many; both shapes appear in
    live confs, so handle both rather than assuming one.
    """
    repos = []
    try:
        from _path_roots import compute_allowed_roots
        from _paths import PROJECT_ROOT, read_agent_conf
        roots = [Path(v) for label, v
                 in compute_allowed_roots(str(PROJECT_ROOT), read_agent_conf())
                 if label == "AGENT_WRITE_PATH"]
    except Exception:
        raw = os.environ.get("AGENT_WRITE_PATH", "")
        roots = [Path(x.strip()) for x in raw.split(";") if x.strip()]
    for root in roots:
        try:
            if not root.is_dir():
                continue
            if _is_repo(root):
                repos.append(root)
                continue
            for child in sorted(root.iterdir()):
                if child.is_dir() and _is_repo(child):
                    repos.append(child)
        except Exception:
            continue
    # De-dup while preserving order (roots can nest).
    seen, out = set(), []
    for r in repos:
        k = str(r.resolve()) if r.exists() else str(r)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def goal_text(goal_id, source):
    """Returns (text, lookup_ok).

    `lookup_ok` is the load-bearing half and the reason this does not just
    return a string. "The goal names no repo" and "the goal could not be read"
    both yield an empty selection, and the first draft collapsed them -- so a
    wrapper that failed to spawn produced silence indistinguishable from a
    clean all-in-sync result. Distinguishing them is what lets main() say
    CANNOT CHECK on the second while staying quiet on the first, which is the
    whole contract: silence means checked-and-clean, never could-not-check.

    lookup_ok is True when a read SUCCEEDED, whether or not the goal was found
    in it -- a successfully-read aspiration that simply lacks this goal id is a
    real answer, not a failure.
    """
    if not goal_id:
        return "", True          # no goal named: nothing to look up, not a failure
    sources = [source] if source else ["world", "agent"]
    any_read_ok = False
    for src in sources:
        for asp in _aspiration_ids_for(goal_id):
            rc, out, err = _run_wrapper(
                ["core/scripts/aspirations-read.sh", "--source", src, "--id", asp])
            if rc != 0 or not out:
                continue
            try:
                data = json.loads(out[out.find("{"):])
            except Exception:
                continue
            any_read_ok = True
            for g in data.get("goals") or []:
                if g.get("id") == goal_id:
                    return ("%s %s" % (g.get("title") or "",
                                       g.get("description") or ""), True)
    return "", any_read_ok


def _aspiration_ids_for(goal_id):
    """g-NNN-MM -> asp-NNN. Returns a list so a future id shape can widen it."""
    parts = str(goal_id).split("-")
    if len(parts) >= 3 and parts[0] == "g":
        return ["asp-%s" % parts[1]]
    return []


def _run_wrapper(argv):
    """Run a core/scripts/*.sh wrapper. Returns (rc, stdout, stderr).

    Routed through `_runtime_bash.BASH` rather than executing the `.sh`
    directly. The first draft passed the script path as argv[0] with no
    interpreter, which works ONLY where the exec bit and shebang are both
    honored -- measured: the same file raises PermissionError without the exec
    bit, and Windows honors neither. On the fleet's Windows box this call
    raised, the bare `except` swallowed it, and the caller silently selected
    ZERO repos while exiting 0 -- byte-identical to "all repos in sync", which
    is the exact vacuity this script exists to prevent, one layer below the
    CANNOT CHECK guard (that guard covers an empty ENUMERATION; this was an
    empty SELECTION). Found by the fresh-eyes review this goal's own close
    dispatched; guard-1977 predicted it verbatim -- a diagnostic added to end a
    silent failure becomes the next silent layer.

    Note guard-580's gate (check-no-bare-bash.py) does NOT catch this shape: it
    greps for `subprocess.run(["bash", ...])` literals, and passing no
    interpreter at all slips underneath it.

    Returns rc=127 with the exception text on spawn failure, so callers can
    tell "wrapper could not run" from "wrapper ran and said nothing".
    """
    try:
        from _runtime_bash import BASH
        cmd = [BASH, *argv] if str(argv[0]).endswith(".sh") else list(argv)
    except Exception:
        cmd = list(argv)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                           cwd=str(PROJECT_ROOT_FOR_WRAPPERS()))
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as exc:
        return 127, "", "%s: %s" % (type(exc).__name__, exc)


def PROJECT_ROOT_FOR_WRAPPERS():
    """PROJECT_ROOT from the SSOT, not a re-derived `.parent` chain.

    `Path(__file__).resolve().parents[2]` is correct today and was what the
    first draft used, but it is the re-derivation class CLAUDE.md keeps an
    audit grep for (g-115-1279) -- and that class already bit this goal once,
    in its own test file, where a one-level-short chain made an assertion that
    could never fail. Falls back to the chain only if the import fails.
    """
    try:
        from _paths import PROJECT_ROOT
        return PROJECT_ROOT
    except Exception:
        return Path(__file__).resolve().parents[2]


def select_repos(repos, text):
    """Repos whose directory name is named in the goal text.

    Matching on the BASENAME rather than the full path: goals name repos the
    way humans do ("Vinheim-Web-App"), not by absolute path, and the absolute
    path differs per box anyway — which is the very coupling this whole script
    exists to remove.
    """
    if not text:
        return []
    low = text.lower()
    return [r for r in repos if r.name.lower() in low]


def freshness(repo, do_fetch=True):
    """behind/ahead vs the tracked upstream. Every failure is reported, never raised."""
    rec = {"repo": str(repo), "name": repo.name, "behind": None, "ahead": None,
           "branch": None, "upstream": None, "verdict": "unknown", "detail": ""}

    rc, branch, err = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0:
        rec["detail"] = "cannot read HEAD: %s" % (err or "rc=%d" % rc)
        return rec
    rec["branch"] = branch

    rc, upstream, err = _git(repo, "rev-parse", "--abbrev-ref", "@{upstream}")
    if rc != 0:
        # No tracking branch is a legitimate state (detached, local-only work),
        # NOT a staleness signal — say so rather than implying the repo is fine.
        rec["verdict"] = "no-upstream"
        rec["detail"] = "no tracking branch for %s" % branch
        return rec
    rec["upstream"] = upstream

    if do_fetch:
        rc, _, err = _git(repo, "fetch", "--quiet", timeout=FETCH_TIMEOUT_S)
        if rc != 0:
            # Report the fetch failure and CONTINUE to the count: a stale
            # remote-tracking ref still beats no answer, but the caller must
            # know the numbers predate this moment.
            rec["detail"] = "fetch failed (counts are from the last known " \
                            "remote ref, not from now): %s" % (err or "rc=%d" % rc)
    else:
        rec["detail"] = "no-fetch: counts are from the last known remote ref"

    rc, counts, err = _git(repo, "rev-list", "--left-right", "--count",
                           "%s...HEAD" % upstream)
    if rc != 0:
        rec["detail"] = (rec["detail"] + " | " if rec["detail"] else "") + \
                        "rev-list failed: %s" % (err or "rc=%d" % rc)
        return rec
    try:
        behind, ahead = (int(x) for x in counts.split())
    except Exception:
        rec["detail"] = (rec["detail"] + " | " if rec["detail"] else "") + \
                        "unparseable rev-list output: %r" % counts
        return rec

    rec["behind"], rec["ahead"] = behind, ahead
    if behind and ahead:
        rec["verdict"] = "diverged"
    elif behind:
        rec["verdict"] = "behind"
    elif ahead:
        rec["verdict"] = "ahead"
    else:
        rec["verdict"] = "in-sync"
    return rec


def _default_branch(repo):
    """Best-effort default-branch name. Never raises; "" when unknown.

    `origin/HEAD` is the right answer when it exists, but it is NOT reliably
    set: a `git clone` of an empty repo leaves it UNSET, and so does any
    checkout created by `git init` + `git remote add` (measured on this box
    2026-08-06 — probe B returned UNSET on a freshly-cloned repo). So the
    name fallback is the common path, not the exotic one. Returning "" is a
    real answer meaning "cannot tell", and callers must NOT read it as "not
    the default branch" — severity treats unknown as still-worth-reporting.
    """
    rc, out, _ = _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD")
    if rc == 0 and out:
        return out.rsplit("/", 1)[-1]
    for cand in ("main", "master"):
        rc, _, _ = _git(repo, "rev-parse", "--verify", "--quiet", "refs/heads/%s" % cand)
        if rc == 0:
            return cand
    return ""


def _dirty_paths(repo):
    """Paths git reports as dirty. Uses -z so filenames are never re-quoted.

    Without -z, git quotes paths containing spaces/unicode and the parse
    silently drops them — which would under-report exactly the trees most
    likely to hold hand-edited work. With -z the record is `XY <path>` and a
    rename adds a bare second token; a chunk without the 3-char prefix IS
    that token, so both shapes are accepted rather than assumed away.
    """
    rc, out, _ = _git(repo, "status", "--porcelain", "-z")
    if rc != 0 or not out:
        return []
    paths = []
    for chunk in out.split("\0"):
        if not chunk:
            continue
        paths.append(chunk[3:] if len(chunk) > 3 and chunk[2] == " " else chunk)
    return [p for p in paths if p]


def _patches_absent_upstream(repo, branch, default_branch, fallback):
    """Commits on `branch` with NO equivalent patch upstream. Never raises.

    `rev-list --not --remotes` matches on SHA, so it cannot see that a
    squash- or rebase-merged branch is already upstream: the merge rewrote
    the commits, and the local originals will never appear on a remote by
    sha for as long as the branch exists. `git cherry` compares PATCH
    equivalence instead — `+` genuinely absent, `-` already upstream.

    This is not a theoretical refinement. On the first real run of this sweep
    (cc-08, 56 repos, 2026-08-06) FOUR branches were flagged and `git cherry`
    classified TWO of them as already-merged — a 50% false-positive rate on
    day one, against exactly the population a reader would check first. A
    periodic sweep that is half noise on its first outing does not get a
    second reading, so the precision is what makes the two REAL findings
    (unpushed since 2026-07-19 and 2026-07-21) worth surfacing at all.

    Returns `fallback` (the raw count) whenever the comparison cannot be made
    — no default branch, no matching remote ref, cherry failing. Degrading to
    the less precise answer keeps a genuine finding visible; silently
    returning 0 would DROP the branch from the report, turning a tooling
    failure into a false all-clear.
    """
    if not default_branch:
        return fallback
    upstream = "origin/%s" % default_branch
    rc, _, _ = _git(repo, "rev-parse", "--verify", "--quiet", upstream)
    if rc != 0:
        return fallback
    rc, out, _ = _git(repo, "cherry", upstream, branch)
    if rc != 0:
        return fallback
    return sum(1 for ln in out.splitlines() if ln.startswith("+"))


def sweep_status(repo):
    """Unpushed-on-any-branch + stale-dirty for ONE repo. Never raises.

    This is the g-335-833 half, and it is deliberately NOT what `freshness()`
    measures. freshness() compares the CURRENT branch to ITS upstream, which
    is the right question before you edit a repo. It is the wrong question for
    the a55add9 class, and measurably so: a fixture holding three unpushed
    commits across two branches reports `verdict: in-sync, ahead: 0` from
    freshness(), because the branch that is checked out genuinely IS in sync
    (measured 2026-08-06, probe C). Unpushed work on any OTHER local branch —
    or on a branch with no upstream at all, which freshness() early-returns as
    the legitimate state `no-upstream` — is invisible to it. Hence a second
    reading over the same enumeration rather than a widened `ahead`.

    THE NO-REMOTE GUARD IS LOAD-BEARING, NOT DEFENSIVE TIDINESS. `rev-list
    --not --remotes` excludes nothing when there are no remote refs, so a
    remoteless checkout reports its ENTIRE history as unpushed — measured: a
    2-commit repo returns 2. That would emit a HIGH finding naming hundreds of
    commits for every vendored or scratch clone, and a sweep that cries wolf
    on its first run is a sweep nobody reads again. A remoteless repo is a
    real (different) finding — the `backup-git-repo-offbox` skill's territory
    — so it is reported as `no_remote` with NO commit count attached.
    """
    rec = {"repo": str(repo), "name": repo.name, "no_remote": False,
           "unpushed": [], "unpushed_total": 0, "merged_equivalent": [],
           "dirty_files": 0, "dirty_age_h": None, "default_branch": "",
           "severity": "clean", "detail": ""}

    rc, remotes, _ = _git(repo, "remote")
    if rc != 0:
        rec["severity"] = "unknown"
        rec["detail"] = "cannot read remotes"
        return rec
    rec["no_remote"] = not remotes.strip()

    rc, branches, _ = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    if rc != 0:
        rec["severity"] = "unknown"
        rec["detail"] = "cannot enumerate local branches"
        return rec

    rec["default_branch"] = _default_branch(repo)

    if not rec["no_remote"]:
        for br in [b for b in branches.splitlines() if b.strip()]:
            rc, n, _ = _git(repo, "rev-list", "--count", br, "--not", "--remotes")
            if rc != 0:
                continue
            try:
                cnt = int(n)
            except Exception:
                continue
            if cnt > 0:
                absent = _patches_absent_upstream(repo, br, rec["default_branch"], cnt)
                if absent == 0:
                    # Every commit here has an equivalent patch upstream: the
                    # branch was squash- or rebase-merged, so its local commits
                    # can never appear on a remote by sha. Not unpushed work.
                    rec["merged_equivalent"].append(br)
                    continue
                rec["unpushed"].append({
                    "branch": br, "count": cnt, "patches_absent": absent,
                    "on_default": bool(rec["default_branch"]) and br == rec["default_branch"],
                })

        # DISTINCT union over the REPORTED branches only — two corrections at
        # once, because the naive number is wrong in both directions.
        #
        # Summing per-branch counts double-counts shared ancestry: a branch cut
        # from another unpushed branch contains its commits too, so a 3-commit
        # fixture reports 5 (measured). And spanning ALL branches would re-admit
        # the squash-merged ones that `_patches_absent_upstream` just excluded.
        # Restricting a single rev-list to the reported branch names fixes both:
        # git does the de-duplication, and merged-equivalent branches are simply
        # not in the argument list.
        #
        # The per-branch numbers above stay as they are — each is individually
        # correct and tells the reader WHICH branch to push. This field answers
        # the different question a reader asks of a sweep: how many commits on
        # this box are on no remote. An inflated answer there erodes trust in
        # every other number in the report.
        if rec["unpushed"]:
            rc, n, _ = _git(repo, "rev-list", "--count",
                            *[u["branch"] for u in rec["unpushed"]],
                            "--not", "--remotes")
            try:
                rec["unpushed_total"] = int(n) if rc == 0 else sum(
                    u["count"] for u in rec["unpushed"])
            except Exception:
                rec["unpushed_total"] = sum(u["count"] for u in rec["unpushed"])

    dirty = _dirty_paths(repo)
    rec["dirty_files"] = len(dirty)
    if dirty:
        newest = None
        for p in dirty:
            try:
                m = (repo / p).stat().st_mtime
            except Exception:
                continue          # deleted/unreadable entries carry no mtime
            newest = m if newest is None else max(newest, m)
        if newest is not None:
            rec["dirty_age_h"] = round((time.time() - newest) / 3600.0, 1)

    # SEVERITY. HIGH is reserved for the measured incident shape — an unpushed
    # commit on the default branch, which is invisible to the fleet AND cannot
    # be recovered by anyone else's checkout. A stale-dirty tree is LOW because
    # uncommitted work is visible to whoever sits at that box; the goal asks
    # for it as a hygiene signal, not as an alarm. Reading of "older than 24h":
    # the NEWEST dirty file is >24h old, i.e. nobody has touched this tree in a
    # day — an actively-edited tree is not stale merely for being dirty.
    if any(u["on_default"] for u in rec["unpushed"]):
        rec["severity"] = "high"
    elif rec["unpushed"]:
        rec["severity"] = "medium"
    elif rec["no_remote"]:
        rec["severity"] = "medium"
        rec["detail"] = "no remote configured — nothing here is pushed anywhere"
    elif rec["dirty_age_h"] is not None and rec["dirty_age_h"] > DIRTY_AGE_HOURS:
        rec["severity"] = "low"
    return rec


def render_sweep(records, scanned):
    """Sweep banner. Unlike render(), this ALWAYS speaks — even on a clean run.

    The two are deliberately opposite and the asymmetry is the point.
    render() is a per-goal advisory fired before every edit, so silence-when-
    clean is what keeps it readable. This is a periodic sweep whose whole
    output is a board finding, and a sweep that prints nothing on a clean run
    is indistinguishable from a sweep that did not run, silently mis-parsed
    its enumeration, or crashed under a caller that swallowed the rc. That
    ambiguity is the vacuous-zero class this file already guards twice
    (CANNOT CHECK / goal-lookup); a zero that would SATISFY the reader needs
    to state what it scanned to earn belief (rb-245, guard-2421).
    """
    order = {"high": 0, "medium": 1, "low": 2, "unknown": 3}
    noisy = sorted([r for r in records if r["severity"] != "clean"],
                   key=lambda r: order.get(r["severity"], 9))
    if not noisy:
        return ("[unpushed-sweep] CLEAN: %d repo(s) scanned, 0 with unpushed "
                "commits, 0 remoteless, 0 dirty >%dh." % (scanned, DIRTY_AGE_HOURS))
    lines = ["[unpushed-sweep] %d of %d repo(s) need attention:" % (len(noisy), scanned)]
    for r in noisy:
        for u in r["unpushed"]:
            # Report the PATCH-absent count, not the raw sha count: the two
            # differ whenever a branch carries merge commits, and the patch
            # count is the one that answers "how much work would be lost".
            n = u.get("patches_absent", u["count"])
            extra = ""
            if n != u["count"]:
                extra = " (%d local commit(s), %d carrying work not upstream)" % (u["count"], n)
            lines.append(
                "  %-6s %s: %d commit(s) on %s not upstream%s%s"
                % (r["severity"].upper(), r["name"], n, u["branch"], extra,
                   " — DEFAULT BRANCH, invisible to the fleet" if u["on_default"] else ""))
        if r["no_remote"]:
            lines.append("  %-6s %s: no remote configured — nothing here is pushed anywhere"
                         % (r["severity"].upper(), r["name"]))
        if r["dirty_age_h"] is not None and r["dirty_age_h"] > DIRTY_AGE_HOURS:
            lines.append("  %-6s %s: working tree dirty (%d file(s)), untouched for %.0fh"
                         % ("LOW", r["name"], r["dirty_files"], r["dirty_age_h"]))
        if r["severity"] == "unknown":
            lines.append("  UNKNOWN %s: %s" % (r["name"], r["detail"] or "no detail"))
    return "\n".join(lines)


def render(records, selected_count):
    """Human banner. Silent when every selected repo is in-sync — an advisory
    that speaks on the clean path gets tuned out, and then it is not an
    advisory at all (the pre-edit-context-gate desensitization lesson)."""
    noisy = [r for r in records if r["verdict"] != "in-sync"]
    if not records:
        return ""
    if not noisy:
        return ""
    lines = ["[product-repo-freshness] %d of %d repo(s) need attention "
             "BEFORE you read or edit them:" % (len(noisy), selected_count)]
    for r in noisy:
        if r["verdict"] == "behind":
            lines.append("  BEHIND  %s by %d commit(s) on %s — `git -C %s pull --ff-only` "
                         "first; a read of this tree right now is a read of the past"
                         % (r["name"], r["behind"], r["branch"], r["repo"]))
        elif r["verdict"] == "diverged":
            lines.append("  DIVERGED %s: %d behind / %d ahead on %s — do NOT stash or reset "
                         "(a same-box partner's in-flight work may be here); reconcile first"
                         % (r["name"], r["behind"], r["ahead"], r["branch"]))
        elif r["verdict"] == "ahead":
            lines.append("  AHEAD   %s by %d unpushed commit(s) on %s — the post-execution "
                         "push contract was missed somewhere" % (r["name"], r["ahead"], r["branch"]))
        else:
            lines.append("  %-7s %s — %s" % (r["verdict"].upper(), r["name"],
                                             r["detail"] or "no detail"))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--goal-id")
    ap.add_argument("--source", choices=["world", "agent"])
    ap.add_argument("--repo", action="append", default=[])
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--sweep", action="store_true",
                    help="unpushed-on-any-branch + stale-dirty across ALL enumerated "
                         "repos (g-335-833). Ignores --goal-id selection by design: "
                         "the incident class is work in a repo NO goal named.")
    args = ap.parse_args(argv)

    enumerated = enumerate_repos()

    if not enumerated:
        # LOUD, because this is the exact defect the script exists to fix, and
        # it reproduced INSIDE the script on first measurement ():
        # with MIND_AGENT unset, `read_agent_conf()` fails open to {}, so the
        # enumeration is empty and the production-shape call printed NOTHING
        # and exited 0 -- byte-identical to "checked, all repos in sync". A
        # freshness check whose silence can mean "could not check" is worse
        # than no check, because it manufactures the confidence it should be
        # withholding. Silence from this script must mean CHECKED-AND-CLEAN,
        # so an empty enumeration has to speak.
        print("[product-repo-freshness] CANNOT CHECK: AGENT_WRITE_PATH "
              "enumerated 0 repos (agent=%r). This is NOT an all-clear -- "
              "nothing was examined. Confirm MIND_AGENT is set and its "
              "local-paths.conf names AGENT_WRITE_PATH."
              % (os.environ.get("MIND_AGENT") or "",), file=sys.stderr)

    if args.list:
        payload = {"enumerated": [str(r) for r in enumerated], "count": len(enumerated)}
        print(json.dumps(payload, indent=2) if args.json
              else "\n".join(str(r) for r in enumerated))
        return 0

    if args.sweep:
        targets = [Path(r) for r in args.repo if _is_repo(r)] if args.repo else enumerated
        # FETCH FIRST, and say so when we did not. `--not --remotes` resolves
        # against REMOTE-TRACKING refs, which are only as fresh as the last
        # fetch — so a stale ref makes work that a PARTNER already pushed read
        # as unpushed-here. That false positive is the more damaging direction
        # for this sweep: it sends a reader hunting for divergence that does
        # not exist, and a first run full of phantoms is how a periodic check
        # loses its audience. --no-fetch stays available for an offline run,
        # but the banner must then disclaim the counts rather than imply them.
        if not args.no_fetch:
            for r in targets:
                _git(r, "fetch", "--quiet", "--all", timeout=FETCH_TIMEOUT_S)
        records = [sweep_status(r) for r in targets]
        if args.json:
            print(json.dumps({"mode": "sweep", "scanned": len(targets),
                              "fetched": not args.no_fetch,
                              "dirty_age_hours": DIRTY_AGE_HOURS,
                              "records": records}, indent=2))
        else:
            print(render_sweep(records, len(targets)))
            if args.no_fetch:
                print("[unpushed-sweep] NOTE: --no-fetch — counts are against the last "
                      "known remote refs, so a commit a partner already pushed can "
                      "still appear here. Re-run with a fetch before acting.")
        return 0

    lookup_ok = True
    if args.repo:
        selected = [Path(r) for r in args.repo if _is_repo(r)]
    else:
        text, lookup_ok = goal_text(args.goal_id, args.source)
        selected = select_repos(enumerated, text)

    if not lookup_ok:
        # The SECOND vacuity, one layer below the CANNOT CHECK above, and the
        # one that actually shipped: enumeration succeeds (so the guard above
        # stays quiet) while the goal lookup fails, yielding an empty selection
        # that renders as silence. Measured on the fleet's Windows box, where
        # _run_wrapper could not spawn the .sh at all. Both layers must speak,
        # because either one alone leaves a way for "could not check" to look
        # exactly like "checked and clean".
        print("[product-repo-freshness] CANNOT CHECK: could not read goal %r "
              "(the aspirations wrapper did not return readable output). This "
              "is NOT an all-clear -- %d repo(s) were enumerated but NONE were "
              "examined, because the goal's named repos are unknown. Pass "
              "--repo <path> to check specific repos regardless."
              % (args.goal_id, len(enumerated)), file=sys.stderr)

    records = [freshness(r, do_fetch=not args.no_fetch) for r in selected]

    if args.json:
        print(json.dumps({"enumerated_count": len(enumerated),
                          "selected_count": len(selected),
                          "goal_lookup_ok": lookup_ok,
                          "records": records}, indent=2))
    else:
        banner = render(records, len(selected))
        if banner:
            print(banner)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # advisory: a crash here must never block a goal
        print("[product-repo-freshness] advisory probe failed (non-fatal): %s: %s"
              % (type(exc).__name__, exc), file=sys.stderr)
        sys.exit(0)
