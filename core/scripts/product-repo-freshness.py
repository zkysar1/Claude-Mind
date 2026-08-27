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
# --pull throttle: skip the NETWORK fetch for a repo fetched within this many
# minutes. Local evaluation and the ff-only advance always run (see
# pull_status). 0 = always fetch. Measured cost of an unthrottled all-repo
# fetch on cc-08 2026-08-20: 44s across 61 repos.
PULL_INTERVAL_MIN = 120


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


def goal_text(goal_id, source, meta=None):
    """Returns (text, lookup_ok); fills `meta` with side-channel goal fields.

    `meta` is an OPTIONAL out-dict rather than a third return value precisely so
    the (text, lookup_ok) shape stays byte-identical for every existing caller
    and test. main() needs `work_class` to decide whether an empty selection is
    worth reporting (see the zero-selection notice below), and re-reading the
    goal through a second wrapper spawn to get one field would double the cost
    of the slowest step in this script.

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
                    if meta is not None:
                        meta["work_class"] = g.get("work_class") or ""
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
           "branch": None, "upstream": None, "verdict": "unknown", "detail": "",
           "tree_identical": None, "default_branch": "", "off_default": None}

    rc, branch, err = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0:
        rec["detail"] = "cannot read HEAD: %s" % (err or "rc=%d" % rc)
        return rec
    rec["branch"] = branch

    # WHICH BRANCH the counts below are measured against (). Every
    # number in this function is computed versus THIS branch's upstream, so a
    # checkout parked on a stale feature branch pulls that branch, reports
    # in-sync, and IS in-sync -- for the wrong ref. That is correct arithmetic
    # to a question nobody asked, and it reads as "fresh". Measured 2026-08-16
    # by the repo-hygiene sweep: 22 of 59 product checkouts were parked, and
    # the shape had already produced a FALSE "this code does not exist" finding
    # about product code that was present on origin/main the whole time.
    #
    # sweep_status() has carried `on_default` since it was written; freshness()
    # -- the MOMENT-OF-ACTION path, the one that runs right before a goal reads
    # a tree -- never had it. The periodic sweep saw the estate and the advisory
    # standing at the point of use did not.
    #
    # TRI-STATE ON PURPOSE, and the None is the load-bearing part.
    # `_default_branch` returns "" for "cannot tell" and its own docstring
    # forbids callers reading that as "not the default branch", so an unknown
    # default yields None and NEVER escalates. Only a positive comparison sets
    # True. This narrowing is deliberate and is what keeps the advisory quiet
    # enough to be read (guard-4031: when a predicate is narrowed to kill false
    # positives, say what the narrowing drops -- here it drops repos with no
    # discoverable default branch, which stay silent rather than guessing).
    rec["default_branch"] = _default_branch(repo)
    if rec["default_branch"]:
        # A detached HEAD reports the literal "HEAD" here and compares unequal,
        # which is the right answer: reading from a detached tree carries the
        # same false-negative risk as reading from a feature branch.
        rec["off_default"] = (branch != rec["default_branch"])

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
        # guard-1996: on a PROTECTED-branch estate an ahead-count measures
        # TOPOLOGY, not unshipped content. main is protected so every real
        # change lands via PR and lands SQUASHED (a NEW sha), while the local
        # checkout reconciles with `git merge origin/main` -- so each merge adds
        # a commit upstream will never contain and `ahead` only ever grows. The
        # count is therefore permanently non-zero on exactly the repos that are
        # healthiest (the ones receiving PR traffic), and "N unpushed commits"
        # is FALSE there: the push contract was honoured, via PR.
        #
        # The property that actually matters is CONTENT identity, and it is one
        # command. Equal tree hashes mean zero content divergence -- nothing is
        # stranded, there is nothing to push, and a PR would carry an empty diff.
        #
        # Scoped to the ahead-ONLY branch deliberately. When `behind` is also
        # non-zero the trees legitimately differ (upstream holds commits this
        # checkout lacks), and `diverged` already prescribes the right action
        # ("reconcile first"), so widening this check there would trade a true
        # verdict for a confusing one.
        rc_h, head_tree, _ = _git(repo, "rev-parse", "HEAD^{tree}")
        rc_u, up_tree, _ = _git(repo, "rev-parse", "%s^{tree}" % upstream)
        if rc_h == 0 and rc_u == 0 and head_tree and head_tree == up_tree:
            rec["tree_identical"] = True
            rec["verdict"] = "ahead-topological"
            rec["detail"] = (rec["detail"] + " | " if rec["detail"] else "") + \
                            ("%d local commit(s) not upstream, but HEAD^{tree} == "
                             "%s^{tree} (%s) -- zero content divergence; squash-merge "
                             "topology, nothing to push (guard-1996)"
                             % (ahead, upstream, head_tree[:12]))
        else:
            # Trees differ, OR the comparison itself failed. A FAILED comparison
            # must not read as "content is stranded" nor as "content is safe" --
            # fall through to the honest `ahead` verdict and say the check could
            # not run, so the reader knows which of the two answers is missing.
            rec["tree_identical"] = False if (rc_h == 0 and rc_u == 0) else None
            rec["verdict"] = "ahead"
            if rec["tree_identical"] is None:
                rec["detail"] = (rec["detail"] + " | " if rec["detail"] else "") + \
                                "tree-identity check could not run (rev-parse rc " \
                                "%d/%d) -- treating as genuinely ahead" % (rc_h, rc_u)
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
    """Paths git reports as dirty, or None when the probe itself FAILED.

    Uses -z so filenames are never re-quoted. Without -z, git quotes paths
    containing spaces/unicode and the parse silently drops them — which would
    under-report exactly the trees most likely to hold hand-edited work. With
    -z the record is `XY <path>` and a rename adds a bare second token; a chunk
    without the 3-char prefix IS that token, so both shapes are accepted rather
    than assumed away.

    NONE-VS-EMPTY IS THE POINT (g-115-5013 defect A). This returned `[]` for
    BOTH `rc != 0` and a genuinely clean tree, so a `git status` that could not
    run at all was indistinguishable from "nothing is dirty here" — and the
    caller turned that into `dirty_files: 0, severity: clean`. That is the same
    vacuity the enclosing file already guards twice at the whole-scan level
    (CANNOT CHECK on a zero enumeration, and on a failed goal lookup); it
    simply survived one level down, per-repo, where nothing was looking.
    The distinction has to live HERE because this is the only frame that still
    knows the rc — collapse it into a list and no caller can recover it.
    """
    rc, out, _ = _git(repo, "status", "--porcelain", "-z")
    if rc != 0:
        return None
    if not out:
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

    NECESSARY BUT NOT SUFFICIENT, and this docstring claimed otherwise until
    2026-08-16. Patch equivalence recognises a squash only when the branch was a
    SINGLE commit; squash N and the combined patch matches none of the N
    originals, so all N still report `+`. `_branch_tree_identical_upstream`
    below is the second filter that catches what this one structurally cannot
    (g-115-6355) — do not read a `+` from here as "content is stranded".

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


def _branch_tree_identical_upstream(repo, branch, default_branch):
    """Does `branch` hold ZERO content divergence from a remote ref? Tri-state.

    Returns ``(verdict, upstream_ref, tree)``. Verdict is True (identical),
    False (differs), or **None when the comparison could not be made** — the
    same three-state contract `_dirty_paths` uses one function up, and for the
    same reason: collapse None into False and a tooling failure renders
    byte-identically to "this branch genuinely holds unpushed work"; collapse
    it into True and the failure promotes a branch to clean. Only this frame
    still knows which of the two happened.

    WHY THIS EXISTS BESIDE `_patches_absent_upstream`. `git cherry` compares
    PATCH equivalence, which catches a squash-merge only when the branch was a
    SINGLE commit: squashing N commits produces one combined patch whose id
    matches none of the N originals, so every one of them reports `+` and the
    branch survives that filter intact. Not theoretical — the g-115-6355
    measurement. A live product repo reported HIGH at `ahead 9, behind 0` while
    `HEAD^{tree}` and `origin/main^{tree}` were the SAME hash and `git diff
    --name-only origin/main HEAD` listed zero files. The false HIGH then
    generated a downstream product goal whose acceptance criteria would have
    opened an EMPTY-diff PR against a protected production repo, so the cost of
    this false positive is not a noisy line — it is manufactured work aimed at
    a protected branch.

    Equal tree hashes mean zero content divergence: nothing is stranded and a
    push would carry an empty diff (guard-1996 — on a protected-branch estate
    an ahead-count measures TOPOLOGY, not unshipped content, because every real
    change lands squashed via PR while the local checkout reconciles by merge).

    `origin/<branch>` is preferred with `origin/<default_branch>` as the
    fallback, mirroring `_patches_absent_upstream`'s base choice rather than
    inventing a second convention: a feature branch is destined for the default
    branch, so content already sitting there is content nobody can lose.
    """
    rc_b, br_tree, _ = _git(repo, "rev-parse", "--verify", "--quiet",
                            "%s^{tree}" % branch)
    if rc_b != 0 or not br_tree:
        return None, "", ""

    compared, seen = False, set()
    for cand in (branch, default_branch):
        if not cand or cand in seen:
            continue
        seen.add(cand)
        up = "origin/%s" % cand
        rc_u, up_tree, _ = _git(repo, "rev-parse", "--verify", "--quiet",
                                "%s^{tree}" % up)
        if rc_u != 0 or not up_tree:
            continue          # no such remote ref — try the next candidate
        compared = True
        if br_tree == up_tree:
            return True, up, br_tree
    # `compared` is the whole point of the flag: it separates "every candidate
    # remote ref was missing, so nothing was measured" from "the refs resolved
    # and the trees genuinely differ". Without it both return False and an
    # unmeasurable repo silently reports a measured divergence.
    return (False if compared else None), "", br_tree


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
    # `dirty_probe_ok` / `branch_probe_failures` answer a DIFFERENT question
    # from the counts beside them — not "how much was found" but "was the
    # search able to run" — so they are separate fields rather than a sentinel
    # value smuggled into `dirty_files` (guard-3116: derive a field's guard
    # from the question it answers, never by mirroring its neighbour). A
    # reader, and `render_sweep`, must be able to tell 0-because-clean from
    # 0-because-unmeasured, and only these fields carry that.
    rec = {"repo": str(repo), "name": repo.name, "no_remote": False,
           "unpushed": [], "unpushed_total": 0, "merged_equivalent": [],
           "tree_identical_branches": [],
           "dirty_files": 0, "dirty_age_h": None, "default_branch": "",
           "dirty_probe_ok": True, "branch_probe_failures": [],
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
                # A branch whose count could not be read is UNMEASURED, not
                # zero. `continue` alone drops it from `unpushed`, which renders
                # byte-identically to "this branch has nothing unpushed" — the
                # per-branch instance of the same vacuity as _dirty_paths above.
                rec["branch_probe_failures"].append(br)
                continue
            try:
                cnt = int(n)
            except Exception:
                rec["branch_probe_failures"].append(br)
                continue
            if cnt > 0:
                absent = _patches_absent_upstream(repo, br, rec["default_branch"], cnt)
                if absent == 0:
                    # Every commit here has an equivalent patch upstream: the
                    # branch was squash- or rebase-merged, so its local commits
                    # can never appear on a remote by sha. Not unpushed work.
                    rec["merged_equivalent"].append(br)
                    continue
                # TREE IDENTITY — AFTER the patch-id filter, never before it.
                # The two tests answer the same question with different power,
                # and the ORDER is what keeps `merged_equivalent` meaning what
                # it has always meant: a single-commit squash satisfies BOTH,
                # so running this first would silently re-bucket every record
                # the cherry filter is already pinned to classify. This branch
                # therefore only ever catches what `git cherry` CANNOT see.
                tree_same, tree_up, br_tree = _branch_tree_identical_upstream(
                    repo, br, rec["default_branch"])
                if tree_same is True:
                    rec["tree_identical_branches"].append({
                        "branch": br, "count": cnt, "upstream": tree_up,
                        "tree": br_tree[:12],
                    })
                    continue
                rec["unpushed"].append({
                    "branch": br, "count": cnt, "patches_absent": absent,
                    # False = measured, and the content genuinely diverges.
                    # None = the compare could not run, and the branch STAYS
                    # REPORTED: a check that failed is never a promotion to
                    # clean, which is the direction this file guards everywhere
                    # else (CANNOT CHECK on a failed status/branch probe).
                    "tree_identical": tree_same,
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
    if dirty is None:
        rec["dirty_probe_ok"] = False
        dirty = []
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

    # A repo whose probes partly FAILED must never render as `clean` — that is
    # the state this whole change exists to make unreachable. ONLY the clean
    # label is promoted: a real high/medium finding outranks "part of the search
    # did not run", and render_sweep emits the CANNOT CHECK line at every
    # severity, so demoting a genuine finding to `unknown` would hide the
    # louder signal to surface the quieter one.
    if rec["severity"] == "clean" and (
            not rec["dirty_probe_ok"] or rec["branch_probe_failures"]):
        rec["severity"] = "unknown"
        rec["detail"] = "probe failure — see the CANNOT CHECK line(s) below"
    return rec


def pull_status(repo, interval_min=PULL_INTERVAL_MIN, do_fetch=True):
    """ACTUATE the advice this script has only ever printed ().

    Every other mode here DETECTS. Line 825 renders the literal remedy
    `git -C <repo> pull --ff-only` as text, and nothing in the tree ever runs
    it: measured on cc-08 2026-08-20, 35 of 61 enumerated repos were behind
    origin (worst 33), 0 ahead, 0 dirty, with 43 last fetched at the
    provisioning clone 6.1 days earlier. The framework's response to that gap
    has been NINE guardrails (guard-3822 / 2000 / 1385 / 3566 / 2204 / 1044 /
    2528 / 2005 / 1805) all restating "fetch before trusting a product-repo
    read". Nine restatements of one imperative is the signature of a
    behavioural rule doing a mechanical job; this is the mechanical half.

    WHY THE THROTTLE GATES THE FETCH AND NOT THE PULL. The obvious design --
    "skip the repo if FETCH_HEAD is young" -- is WRONG here, and subtly:
    `--sweep` already fetches all repos, so it refreshes FETCH_HEAD while
    leaving every working tree exactly as stale as before. A FETCH_HEAD-keyed
    pull throttle would therefore be SILENCED by the very sweep that just
    proved the trees are behind. So the split follows iteration-push.sh's
    precedent instead: the NETWORK fetch is throttled (expensive, ~44s across
    61 repos measured), and the LOCAL evaluation always runs. The advance is
    `merge --ff-only <upstream>`, never `git pull` -- pull would perform its
    own unthrottled fetch and defeat the throttle it sits behind.

    THE SKIP LADDER IS ORDERED BY WHAT IT PROTECTS, most irreversible first.
    Two of its rungs encode contracts this module documents elsewhere and that
    a naive reading inverts:

      * `_dirty_paths` returns None for a FAILED PROBE and [] for a clean
        tree. Treating None as clean would pull over a tree whose state is
        unknown -- reintroducing the g-115-5013 vacuity one level up.
      * `_default_branch` returns "" for UNKNOWN, and its own docstring warns
        callers not to read that as "not the default branch". Unknown skips.

    A dirty tree is NEVER touched: on a shared box those edits may be a
    same-box partner's in-flight work, and pre-execution.md's Step 2 forbids
    stashing or checking out over them.

    OFF-DEFAULT IS A SKIP *AND* THE LOUDEST REPORT, which looks contradictory
    until you see what it protects. 13 of 61 repos here sit on leftover
    feature branches. Fast-forwarding one advances the FEATURE branch, so the
    working tree still does not contain origin/main's content and a grep still
    returns the inverted answer -- the pull would consume the anomaly while
    fixing nothing. Switching branches is worse: the branch may belong to a
    live goal or another agent. So the only correct action is to leave it and
    say so loudly, which is what outcome 3 asks for.

    Returns a record; never raises, never blocks.
    """
    name = Path(repo).name
    rec = {"repo": str(repo), "name": name, "action": None, "detail": "",
           "branch": None, "default_branch": None, "behind": None,
           "ahead": None, "fetched": False}

    rc, cur, _ = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    rec["branch"] = cur if rc == 0 else None
    rc_u, upstream, _ = _git(repo, "rev-parse", "--abbrev-ref", "@{u}")
    if rc_u != 0 or not upstream:
        rec["action"] = "skipped-no-upstream"
        rec["detail"] = "no upstream tracking ref"
        return rec

    dirty = _dirty_paths(repo)
    if dirty is None:
        rec["action"] = "skipped-dirty-probe-failed"
        rec["detail"] = ("`git status` could not run — NOT an all-clear, so "
                         "nothing was advanced")
        return rec
    if dirty:
        rec["action"] = "skipped-dirty"
        rec["detail"] = ("%d uncommitted path(s) — may be a same-box partner's "
                         "in-flight work; never pulled over" % len(dirty))
        return rec

    default = _default_branch(repo)
    rec["default_branch"] = default or None
    if not default:
        rec["action"] = "skipped-default-unknown"
        rec["detail"] = "default branch undeterminable — not read as off-default"
        return rec
    if cur != default:
        # MERGED-vs-UNMERGED is what makes this warning actionable instead of
        # ambient. 13 undifferentiated "off-default" lines train a reader to
        # skip the section; 3 safe-to-return plus 10 explained does not.
        # `merge-base --is-ancestor HEAD origin/<default>` is exact: when it
        # holds, EVERY commit on this branch is already contained in the
        # default branch, so returning the checkout would lose nothing. When
        # it does not, the tree holds work origin lacks and must be left
        # alone. Reported either way -- this mode still never switches a
        # branch, because deciding to move someone else's checkout is not a
        # decision a per-goal precondition should be making.
        rc_a, _, _ = _git(repo, "merge-base", "--is-ancestor", "HEAD",
                          "origin/%s" % default)
        rec["branch_merged_into_default"] = (rc_a == 0)
        rc_b, bm, _ = _git(repo, "rev-list", "--count",
                           "HEAD..origin/%s" % default)
        rec["behind_default"] = int(bm) if rc_b == 0 and bm.isdigit() else None
        rec["action"] = "skipped-off-default"
        rec["detail"] = (
            "on %s, not %s (%s behind origin/%s) — a read of this tree can "
            "return a FALSE 'this code does not exist'. Fast-forwarding would "
            "advance %s and leave that hazard intact, so it is reported, not "
            "pulled. %s"
            % (cur, default,
               "?" if rec["behind_default"] is None else rec["behind_default"],
               default, cur,
               "Branch is fully merged into %s — dead residue, safe to return."
               % default if rec["branch_merged_into_default"] else
               "Branch holds UNMERGED work — leave it alone."))
        return rec

    gitdir = Path(repo) / ".git"
    fetch_head = gitdir / "FETCH_HEAD" if gitdir.is_dir() else None
    fresh = False
    if interval_min > 0 and fetch_head is not None and fetch_head.exists():
        age_min = (time.time() - fetch_head.stat().st_mtime) / 60.0
        fresh = age_min < interval_min
    if do_fetch and not fresh:
        frc, _, ferr = _git(repo, "fetch", "--quiet", "origin",
                            timeout=FETCH_TIMEOUT_S)
        rec["fetched"] = (frc == 0)
        if frc != 0:
            rec["action"] = "skipped-fetch-failed"
            rec["detail"] = "fetch failed: %s" % (ferr or "rc=%s" % frc)[:160]
            return rec

    rc_c, counts, _ = _git(repo, "rev-list", "--left-right", "--count",
                           "HEAD...%s" % upstream)
    if rc_c != 0 or len(counts.split()) != 2:
        rec["action"] = "skipped-count-failed"
        rec["detail"] = "could not compute ahead/behind against %s" % upstream
        return rec
    ahead, behind = (int(x) for x in counts.split())
    rec["ahead"], rec["behind"] = ahead, behind

    if ahead:
        rec["action"] = "skipped-ahead"
        rec["detail"] = ("%d local commit(s) origin lacks — never fast-forwarded "
                         "over" % ahead)
        return rec
    if behind == 0:
        rec["action"] = "current"
        return rec

    mrc, _, merr = _git(repo, "merge", "--ff-only", upstream, timeout=60)
    if mrc != 0:
        rec["action"] = "ff-failed"
        rec["detail"] = (merr or "rc=%s" % mrc)[:160]
        return rec
    rec["action"] = "pulled"
    rec["detail"] = "fast-forwarded %d commit(s) from %s" % (behind, upstream)
    return rec


def render_pull(records):
    """Pull banner. ALWAYS speaks, for the same reason render_sweep does.

    This mode MUTATES working trees, so silence would make an actuating pass
    indistinguishable from one that never ran -- the failure this whole file
    exists to prevent, in its most consequential mode.
    """
    from collections import Counter
    tally = Counter(r["action"] for r in records)
    out = ["[product-repo-pull] %d repo(s): %s" % (
        len(records),
        ", ".join("%s=%d" % (k, v) for k, v in sorted(tally.items())) or "none")]

    pulled = [r for r in records if r["action"] == "pulled"]
    if pulled:
        out.append("  ADVANCED (working trees moved):")
        for r in sorted(pulled, key=lambda x: -(x["behind"] or 0)):
            out.append("    %-42s +%d commit(s)" % (r["name"], r["behind"]))

    offd = [r for r in records if r["action"] == "skipped-off-default"]
    if offd:
        out.append("  OFF-DEFAULT — READ HAZARD, not pulled (a grep here can "
                   "return a false negative):")
        for r in offd:
            bd = r.get("behind_default")
            out.append("    %-42s on %-38s %s behind %s  [%s]"
                       % (r["name"], r["branch"],
                          "?" if bd is None else bd, r["default_branch"],
                          "MERGED — safe to return"
                          if r.get("branch_merged_into_default")
                          else "UNMERGED — leave alone"))

    for label, key in (("DIRTY — left alone", "skipped-dirty"),
                       ("AHEAD — local commits, left alone", "skipped-ahead"),
                       ("COULD NOT CHECK", "skipped-dirty-probe-failed"),
                       ("COULD NOT CHECK", "skipped-default-unknown"),
                       ("FETCH FAILED", "skipped-fetch-failed"),
                       ("FF REFUSED", "ff-failed")):
        rows = [r for r in records if r["action"] == key]
        if rows:
            out.append("  %s:" % label)
            for r in rows:
                out.append("    %-42s %s" % (r["name"], r["detail"]))
    return "\n".join(out)


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
        # PROBE-FAILURE LINES, emitted at EVERY severity rather than only under
        # `unknown` ( defect A). A repo can carry a genuine high
        # finding AND a failed dirty probe; gating these on the severity label
        # would drop the caveat exactly where the record looks most
        # authoritative. `dirty_files: 0` and `unpushed: []` are the two numbers
        # a reader trusts, so when either is unmeasured it has to be said in the
        # same breath as the number itself.
        if not r.get("dirty_probe_ok", True):
            lines.append("  CANNOT CHECK %s: `git status` failed — the dirty-file "
                         "count for this repo is UNMEASURED, not 0" % r["name"])
        if r.get("branch_probe_failures"):
            lines.append("  CANNOT CHECK %s: unpushed count unreadable on %d branch(es) "
                         "(%s) — those branches are UNMEASURED, not clean"
                         % (r["name"], len(r["branch_probe_failures"]),
                            ", ".join(r["branch_probe_failures"][:5])))
        if r["severity"] == "unknown":
            lines.append("  UNKNOWN %s: %s" % (r["name"], r["detail"] or "no detail"))
    return "\n".join(lines)


def render(records, selected_count):
    """Human banner. Silent when every selected repo is in-sync — an advisory
    that speaks on the clean path gets tuned out, and then it is not an
    advisory at all (the pre-edit-context-gate desensitization lesson)."""
    # "ahead-topological" is a CLEAN verdict, not a quiet problem: trees are
    # byte-identical to upstream, so there is no action for the reader to take.
    # It joins "in-sync" here for the same reason the docstring gives -- an
    # advisory that speaks on the clean path gets tuned out. The record still
    # carries the ahead count and tree_identical=True for any JSON consumer.
    CLEAN_VERDICTS = ("in-sync", "ahead-topological")
    # An OFF-DEFAULT checkout is reported even when its verdict is clean, and
    # that is the whole point of the field (): the dangerous case is
    # precisely the one that looks healthy, because "in-sync" is computed
    # against the parked branch's own upstream. Before this, such a repo fell
    # in CLEAN_VERDICTS and render() returned "" -- the advisory was SILENT on
    # the one shape that produces a false "this code does not exist".
    noisy = [r for r in records
             if r["verdict"] not in CLEAN_VERDICTS or r.get("off_default") is True]
    if not records:
        return ""
    if not noisy:
        return ""
    lines = ["[product-repo-freshness] %d of %d repo(s) need attention "
             "BEFORE you read or edit them:" % (len(noisy), selected_count)]
    for r in noisy:
        if r.get("off_default") is True:
            # Emitted BEFORE and IN ADDITION TO any verdict line below, never
            # instead of it. The two facts are independent and have different
            # remedies -- "behind on the default branch" is a pull, "on another
            # branch entirely" is not -- and collapsing them would lose the one
            # the reader cannot recover from the numbers.
            lines.append("  OFF-DEFAULT %s is on %s, not %s — every count here is "
                         "measured against %s, so a clean verdict does NOT mean the "
                         "default branch's content is present. A read of this tree can "
                         "produce a FALSE 'this code does not exist'; confirm against "
                         "origin/%s before asserting any negative."
                         % (r["name"], r["branch"], r["default_branch"],
                            r.get("upstream") or "its own upstream", r["default_branch"]))
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
        elif r["verdict"] not in CLEAN_VERDICTS:
            # Guarded rather than a bare `else` because the noisy set now admits
            # repos whose verdict IS clean (off-default only). A bare else would
            # print "IN-SYNC <repo> — no detail" directly beneath the OFF-DEFAULT
            # warning, which reads as a contradiction and undercuts it.
            lines.append("  %-7s %s — %s" % (r["verdict"].upper(), r["name"],
                                             r["detail"] or "no detail"))
    return "\n".join(lines)


def _force_lf_stdout():
    r"""Emit LF, never CRLF — on every platform and in every output mode.

    Python's text-mode stdout translates "\n" to os.linesep, so on Windows
    `--list` emitted one trailing CR per repo path. `IFS= read -r r` PRESERVES
    that CR, so every consumer then ran `git -C '<path><CR>'` -> "cannot change
    to '...': Invalid argument", rc=128, across all 57 repos. No finding
    printed, and the count line still read a healthy "scanning 57 repo(s)" — so
    by the convention's own rule the reader correctly concluded ALL CLEAN while
    literally nothing had been examined. Measured 2026-08-03 on
    DESKTOP-O91DLK2 (Windows 10 / MSYS2); board msg-20260803-155838-alpha-5117.

    FIXED IN THE PRODUCER, NOT THE CONSUMERS. Five convention steps read this
    output; five `| tr -d '\r'` pipes are five things to keep in sync, and they
    had already drifted — only ONE of the five carried the pipe (g-115-5056
    audited the other four and found them to be LLM-read invocations and prose,
    with no shell capture to strip). One line here makes "this script never
    emits CR" an invariant of the script instead of a habit of its readers.

    Applied to EVERY mode rather than just --list: --json and the banners have
    no use for CR either, and scoping it to one branch leaves the next output
    mode to rediscover this. Fail-open — `reconfigure` needs a real
    TextIOWrapper, and a caller that has replaced sys.stdout (a test harness
    capturing into StringIO) must not crash an advisory probe.

    THIS MUTATES PROCESS-GLOBAL STATE, which is why it lives in main() and not
    at import: main() runs in a process dedicated to this script. Under pytest
    sys.stdout IS a real TextIOWrapper (measured), so an in-process main() call
    genuinely reconfigures the HOST's captured stdout — checked and harmless
    (it sets the value Linux already uses), and `corpus-freshness-precheck.py`,
    the one production importer of this module, calls helpers only and never
    main(). If a future caller starts invoking main() in-process on Windows,
    re-check that assumption before trusting this note.
    """
    try:
        sys.stdout.reconfigure(newline="\n")
    except Exception:
        pass


def vacuity(enumerated_count, selected_count, lookup_ok=True):
    """The ONE place that decides whether a run actually EXAMINED anything.

    Returns ``(cannot_check, reason)``. This is the class fix requested by
    g-115-6001 after a THIRD vacuity was filed against this script: the first
    two (g-115-5013 CRLF probes, g-115-5056 unaudited --list call sites) were
    point-fixed, and the recurring failure is not any single predicate but the
    script's DEFAULT DIRECTION on an unproductive path — it kept finding new
    ways to render "checked nothing" as "checked and clean". Routing every exit
    through one predicate means a future path that selects nothing inherits the
    loud answer instead of having to remember to ask for it.

    WHY THE JSON CHANNEL NEEDS THIS WHEN STDERR ALREADY WARNS. The stderr
    notice added by g-115-5013 fires correctly (verified on cc-08 2026-08-12:
    `--goal-id g-326-98` emits the full CANNOT CHECK line, `goal_lookup_ok`
    True, `selected_count` 0). But `--json` is a SEPARATE CHANNEL on a separate
    stream, and it carried only `selected_count: 0` — so a caller that consumes
    stdout, or captures the two streams apart, had to DERIVE the vacuity from a
    count. That derivation is exactly what the text channel refuses to leave to
    its reader, and the goal that filed this quoted the JSON payload as evidence
    of silence while the stderr line was being printed the whole time.

    DELIBERATELY UNGATED, unlike the stderr notice. That notice is gated on
    ``work_class == "product"`` because an ungated advisory fires on 80.7% of
    goals and stops being read (the measured g-115-5013 tie-break). Noise
    tuning is a HUMAN-attention concern; a machine consumer asked for this data
    explicitly and must always be able to tell "examined nothing" from
    "examined, all clean". So the two channels answer the same question with
    the same predicate but different exposure rules, and that asymmetry is
    intended rather than an oversight.
    """
    if not enumerated_count:
        return True, "no repos were enumerated"
    if not lookup_ok:
        return True, "the goal could not be read, so its repos are unknown"
    if not selected_count:
        return True, ("the goal matched none of the %d enumerated repo(s) — "
                      "selection is by repository DIRECTORY NAME, so a goal "
                      "citing paths, packages or class names matches nothing"
                      % enumerated_count)
    return False, None


def _repo_for_path(path, enumerated):
    """The enumerated product repo containing `path`, or None.

    Walks UP from the path — which need NOT exist, since a prior-art probe
    often names a file precisely because it may be absent — and returns the
    first ancestor that is both a git repo and a MEMBER of `enumerated`.
    Membership is required rather than incidental: the Mind framework repo is
    itself a git repo and is already kept current by iteration-push, so a
    check against a `core/scripts/...` path must decline to judge instead of
    re-reporting a repo this mode does not own.
    """
    try:
        p = Path(path).resolve()
    except Exception:
        return None
    members = {str(Path(r).resolve()) for r in enumerated}
    for cand in [p] + list(p.parents):
        if str(cand) in members and _is_repo(cand):
            return cand
    return None


def check_read(path, interval_min=PULL_INTERVAL_MIN, do_fetch=True,
               enumerated=None):
    """May `path` be read as EVIDENCE about what a product repo contains?

    The mechanical half of a rule the fleet has now written down TEN times
    (guard-5217, and guard-1759 / 2000 / 2005 / 2204 / 2311 / 2528 / 3822 /
    4280 before it). `--pull` above actuated the on-default case; this covers
    the one class it deliberately declines — a checkout parked on a FEATURE
    BRANCH, which cannot be fast-forwarded into safety because advancing the
    branch would leave the tree still missing origin/<default>'s content.

    MEASURED 2026-08-26 on cc-08: 16 of 63 enumerated repos were off-default,
    among them the very repo whose stale read produced the incident that filed
    g-306-370 (StartAyoServerEnvironment, on a feature branch, 5 behind main).
    A `--pull` pass reports those repos and moves on, by design. Nothing then
    stops the next unit grepping one of them and banking the miss.

    FETCH ORDERING IS THE WHOLE POINT, and it is why this cannot simply call
    `pull_status` and read its verdict. That function returns on the
    off-default rung BEFORE it fetches, so its `behind_default` is computed
    against whatever `origin/<default>` happened to be on disk — and a zero
    from an unfetched remote-tracking ref is byte-identical to a genuine zero
    (guard-4280). So the network refresh happens HERE, first, under the same
    FETCH_HEAD throttle; `pull_status` is then called with do_fetch=False so
    the fetch is not paid twice, and it evaluates against a ref that is
    actually current.

    Returns a record; `safe` is the verdict and the caller maps it to an exit
    code. Safe means the working tree at `path` is known to match
    origin/<default> — either it already did (`current`) or this call
    fast-forwarded it (`pulled`). Every other outcome is a READ HAZARD, and
    that includes the probe FAILING: an unreachable remote or a broken status
    probe yields "cannot tell", which must never render as permission to
    trust the tree (guard-1760 — a checker must not report what it declined
    to look at as a pass).
    """
    enumerated = enumerate_repos() if enumerated is None else enumerated
    repo = _repo_for_path(path, enumerated)
    rec = {"path": str(path), "in_product_repo": repo is not None,
           "repo": str(repo) if repo else None, "safe": True,
           "verdict": "not-a-product-repo", "fetched": None,
           "branch": None, "default_branch": None, "behind": None,
           "remedy": None}
    if repo is None:
        return rec

    if do_fetch:
        gitdir = Path(repo) / ".git"
        fetch_head = gitdir / "FETCH_HEAD" if gitdir.is_dir() else None
        fresh = False
        if interval_min > 0 and fetch_head is not None and fetch_head.exists():
            fresh = ((time.time() - fetch_head.stat().st_mtime) / 60.0) < interval_min
        if not fresh:
            frc, _, _ = _git(repo, "fetch", "--quiet", "origin",
                             timeout=FETCH_TIMEOUT_S)
            rec["fetched"] = (frc == 0)
        else:
            rec["fetched"] = "throttled"

    # OFF-DEFAULT IS DECIDED HERE, NOT INHERITED FROM `pull_status`, because the
    # two modes ask different questions and their skip ladders are ordered for
    # the OTHER one. `pull_status` asks "may I fast-forward this?", so it
    # returns `skipped-no-upstream` BEFORE it ever compares branches — correct
    # for an actuator, useless here: whether a feature branch happens to track
    # a remote has no bearing on whether reading its tree can produce a false
    # negative about origin/<default>'s content. Measured while writing the
    # tests: a LOCAL-ONLY feature branch (the shape "WIP DO NOT MERGE" work
    # takes) reported `skipped-no-upstream`, which names no remedy, where the
    # actionable fact is that the tree is not the default branch at all.
    cur_rc, cur_b, _ = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    default_b = _default_branch(repo)
    if cur_rc == 0 and cur_b and default_b and cur_b != default_b:
        rc_b, bm, _ = _git(repo, "rev-list", "--count",
                           "HEAD..origin/%s" % default_b)
        rec["branch"] = cur_b
        rec["default_branch"] = default_b
        rec["behind"] = int(bm) if rc_b == 0 and bm.isdigit() else None
        rec["action"] = "skipped-off-default"
        rec["safe"] = False
        rec["verdict"] = "read-hazard"
        rec["detail"] = (
            "on %s, not %s (%s behind origin/%s) — fast-forwarding would "
            "advance %s and leave the hazard intact, so this is reported, "
            "never pulled."
            % (cur_b, default_b,
               "?" if rec["behind"] is None else rec["behind"],
               default_b, cur_b))
        try:
            rel = Path(path).resolve().relative_to(Path(repo).resolve()).as_posix()
        except Exception:
            rel = "<path-in-repo>"
        rec["remedy"] = "git -C %s show origin/%s:%s" % (repo, default_b, rel)
        return rec

    inner = pull_status(repo, interval_min=interval_min, do_fetch=False)
    action = inner.get("action")
    rec["action"] = action
    rec["branch"] = inner.get("branch")
    rec["default_branch"] = inner.get("default_branch")
    rec["behind"] = inner.get("behind_default", inner.get("behind"))
    rec["detail"] = inner.get("detail")
    rec["safe"] = action in ("current", "pulled")
    rec["verdict"] = "safe-matches-origin" if rec["safe"] else "read-hazard"

    if not rec["safe"]:
        default = rec["default_branch"] or "main"
        try:
            rel = Path(path).resolve().relative_to(Path(repo).resolve()).as_posix()
        except Exception:
            rel = "<path-in-repo>"
        rec["remedy"] = ("git -C %s show origin/%s:%s" % (repo, default, rel))
    return rec


def render_check_read(rec):
    """ALWAYS speaks — a gate that is silent on the safe path teaches a reader
    that no output means it ran, which is the vacuity this file exists to
    prevent (see the empty-enumeration banner in main)."""
    if not rec["in_product_repo"]:
        return ("[check-read] %s is not inside an enumerated product repo — no "
                "freshness claim is made about it. This is NOT an all-clear "
                "about product code." % rec["path"])
    if rec["safe"]:
        return ("[check-read] SAFE: %s is on %s and matches origin/%s — a read "
                "of this tree is a read of origin."
                % (rec["repo"], rec["branch"], rec["default_branch"]))
    return ("[check-read] READ HAZARD (%s): %s\n"
            "  A grep or `git show` here can return a FALSE 'this code does not "
            "exist'.\n"
            "  %s\n"
            "  Read the authoritative ref instead:\n    %s"
            % (rec.get("action"), rec["repo"], rec.get("detail") or "",
               rec["remedy"]))


def main(argv=None):
    _force_lf_stdout()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--goal-id")
    ap.add_argument("--source", choices=["world", "agent"])
    ap.add_argument("--repo", action="append", default=[])
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--check-read", metavar="PATH",
                    help="May PATH be read as EVIDENCE about product-repo "
                         "contents? Fetches that repo FIRST (throttled), then "
                         "answers. exit 0 = tree matches origin/<default>; "
                         "exit 1 = READ HAZARD, and the remedy command is "
                         "printed. The mechanical half of guard-5217.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--sweep", action="store_true",
                    help="unpushed-on-any-branch + stale-dirty across ALL enumerated "
                         "repos (g-335-833). Ignores --goal-id selection by design: "
                         "the incident class is work in a repo NO goal named.")
    ap.add_argument("--pull", action="store_true",
                    help="fast-forward every enumerated repo that is behind "
                         "origin on its DEFAULT branch (g-115-6937). Ignores "
                         "--goal-id by design — the incident class is a read "
                         "of a repo no goal named. Skips dirty, ahead, and "
                         "off-default trees and reports them instead. Never "
                         "blocks; always exits 0.")
    ap.add_argument("--pull-interval-min", type=int, default=PULL_INTERVAL_MIN,
                    help="throttle the NETWORK fetch per repo (default %d min; "
                         "0 = always fetch). The local ff-only advance is NOT "
                         "throttled." % PULL_INTERVAL_MIN)
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

    if args.check_read:
        rec = check_read(args.check_read, args.pull_interval_min,
                         not args.no_fetch, enumerated)
        print(json.dumps(rec, indent=2) if args.json
              else render_check_read(rec))
        return 0 if rec["safe"] else 1

    if args.list:
        payload = {"enumerated": [str(r) for r in enumerated], "count": len(enumerated)}
        print(json.dumps(payload, indent=2) if args.json
              else "\n".join(str(r) for r in enumerated))
        return 0

    if args.pull:
        targets = [Path(r) for r in args.repo if _is_repo(r)] if args.repo else enumerated
        records = [pull_status(r, args.pull_interval_min, not args.no_fetch)
                   for r in targets]
        if args.json:
            cannot, why = vacuity(len(enumerated), len(targets))
            print(json.dumps({"mode": "pull", "scanned": len(targets),
                              "fetched": not args.no_fetch,
                              "pull_interval_min": args.pull_interval_min,
                              "cannot_check": cannot,
                              "cannot_check_reason": why,
                              "records": records}, indent=2))
        else:
            print(render_pull(records))
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
            cannot, why = vacuity(len(enumerated), len(targets))
            print(json.dumps({"mode": "sweep", "scanned": len(targets),
                              "fetched": not args.no_fetch,
                              "dirty_age_hours": DIRTY_AGE_HOURS,
                              "cannot_check": cannot,
                              "cannot_check_reason": why,
                              "records": records}, indent=2))
        else:
            print(render_sweep(records, len(targets)))
            if args.no_fetch:
                print("[unpushed-sweep] NOTE: --no-fetch — counts are against the last "
                      "known remote refs, so a commit a partner already pushed can "
                      "still appear here. Re-run with a fetch before acting.")
        return 0

    lookup_ok = True
    goal_meta = {}
    if args.repo:
        selected = [Path(r) for r in args.repo if _is_repo(r)]
    else:
        text, lookup_ok = goal_text(args.goal_id, args.source, goal_meta)
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

    if enumerated and not selected and (
            args.repo or (lookup_ok and goal_meta.get("work_class") == "product")):
        # DEFECT B (). The goal was read FINE and simply names no
        # repo, so both guards above stay quiet — and `render()` returns "" for
        # an empty record list, so the plain-text channel printed ZERO BYTES at
        # rc=0. Measured on cc-05: --goal-id  (selected=1, verdict
        # in-sync) and --goal-id  (selected=0, nothing examined) were
        # byte-identical on stdout. Silence could not distinguish "checked,
        # fresh" from "matched nothing, checked nothing" — the same failure
        # grammar as the two guards above, one layer further down.
        #
        # WHY `work_class == "product"` GATES THIS, when  asked for it
        # on every zero selection. Both that goal and the test directly below
        # (`..._stays_silent`, from the  fresh-eyes pass) are right,
        # and they collide: one says an unexplained zero is a vacuity, the other
        # says an advisory that speaks on the common path gets tuned out and
        # stops being an advisory at all. The tiebreaker is a count neither had.
        # Measured over all 4,355 completed goals with the live matcher: an
        # ungated notice fires on 3,516 of them — 80.7%, including 97.3% of
        # `framework` goals, which have no product repo to check and for which
        # silence is the CORRECT answer, exactly as that test argues. Gating on
        # work_class fires on 727 (16.7%), and every one of those is a product
        # goal whose repo genuinely went unexamined. Same defect closed, 4.8x
        # less noise, and the older test passes unchanged rather than being
        # overridden — which is the sign this reconciles the two rather than
        # picking a winner.
        #
        # Selection matches a repo's DIRECTORY NAME against the goal text, and
        # code goals routinely cite paths, packages and class names instead:
        #  pushed a 10-file commit to Ayoai-Environment-Server while
        # naming only the Java package `AyoServer`, and selected 0. WHETHER to
        # widen the match is a separate question, measured and decided against
        # under  (b2) — making the zero audible is correct either way
        # and deliberately does not presuppose that answer.
        if args.repo:
            print("[product-repo-freshness] CANNOT CHECK: --repo named %d path(s), "
                  "none of which is a git repo, so NOTHING was examined. This is "
                  "NOT an all-clear." % len(args.repo), file=sys.stderr)
        else:
            print("[product-repo-freshness] CANNOT CHECK: goal %r names none of "
                  "the %d enumerated repo(s), so NOTHING was examined. This is NOT "
                  "an all-clear. Selection is by repository DIRECTORY NAME, so a "
                  "goal citing paths, packages or class names instead matches "
                  "nothing. Pass --repo <path> to check specific repos regardless."
                  % (args.goal_id, len(enumerated)), file=sys.stderr)

    records = [freshness(r, do_fetch=not args.no_fetch) for r in selected]

    if args.json:
        cannot, why = vacuity(len(enumerated), len(selected), lookup_ok)
        print(json.dumps({"enumerated_count": len(enumerated),
                          "selected_count": len(selected),
                          "goal_lookup_ok": lookup_ok,
                          "cannot_check": cannot,
                          "cannot_check_reason": why,
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
