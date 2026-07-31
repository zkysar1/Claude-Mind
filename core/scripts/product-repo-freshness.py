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

Usage:
    product-repo-freshness.py --goal-id g-NNN-NN [--source world|agent]
    product-repo-freshness.py --repo <path> [--repo <path> ...]
    product-repo-freshness.py --list          # enumerate, no fetch
Options:
    --json        machine-readable output
    --no-fetch    report against the already-known remote ref (no network)
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

FETCH_TIMEOUT_S = 25


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
