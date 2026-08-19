#!/usr/bin/env python3
"""Commit-reachability triage — where does a cited commit ACTUALLY live?

Given a sha a goal/report cites as "landed", answer two questions in one pass:

  1. Is it reachable from the target ref (default origin/main)?
  2. If NOT, which ref namespace DOES contain it — and therefore what is the
     landing path?

WHY THIS EXISTS (gap-128, 3 encounters, all zeta/cc-02 2026-08-11):
  g-115-3361  worker-ref hunk carry  — f21ded105 lived on
              refs/workers/alpha/7f7f3513, rescued into main as 824cf68aa
  g-326-133   unmerged PR            — 93fc5b1 sat on an OPEN mergeable PR
  g-115-5765  unmerged PR again      — plus a sign-off gate on the PR body

THE GAP THIS FILLS, AND WHAT IT DELIBERATELY DOES NOT DUPLICATE
(measured 2026-08-12, zeta, hostname cc-02, uname -r 6.8.0-137-generic):

  * `completed-not-committed-sweep.py` already implements remote-branch
    containment (tier 1) and unmerged-PR stranding (tier 2). It probes with
    `git branch -r --contains`, which enumerates refs/remotes/** ONLY, so it
    is STRUCTURALLY BLIND to refs/workers/**. Five such refs were live on
    origin at authoring time, including the two gap-128 names as stranded.
    That sweep is also a DETECTIVE over recently-completed goals; this is an
    on-demand probe for one sha at any time.
  * `/is-change-live` answers the DEPLOY-SURFACE question (is sha live on a
    running service / static deploy). Its own doc excludes repo-vs-main
    reachability. When you need "did it reach production", use that skill;
    when you need "where is it at all", use this one.

THE FAIL-SAFE DIRECTION IS THE WHOLE DESIGN (mirrors /is-change-live's
three-valued contract, which held under a real defect):
  A false STRANDED costs a redundant look — wasteful, safe.
  A false LANDED ENDS THE INVESTIGATION and lets a goal close on a claim that
  never landed (guard-3398), or sends an agent to spend a scarce resource
  against a build that cannot contain the fix (guard-3401).
  So NO unreadable signal may ever resolve to LANDED. Every probe that cannot
  run resolves to INCONCLUSIVE.

Read `verdict`, never the exit code. Exit 0 means THE PROBE RAN. A non-LANDED
verdict is deliberately not a non-zero exit — collapsing six values into two
is how the distinction gets lost at the call site.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

# Verdicts, ordered most-to-least resolved.
LANDED = "LANDED"                          # ancestor of target ref — nothing to do
STRANDED_WORKER_REF = "STRANDED_WORKER_REF"  # only on refs/workers/** — hunk carry
STRANDED_REMOTE_BRANCH = "STRANDED_REMOTE_BRANCH"  # on a remote branch — PR merge
STRANDED_LOCAL_ONLY = "STRANDED_LOCAL_ONLY"  # exists locally, on no remote — push
ABSENT = "ABSENT"                          # valid nowhere reachable — genuinely gone
INCONCLUSIVE = "INCONCLUSIVE"              # a probe could not run — NOT an answer

# The landing path each verdict implies. Deliberately imperative and short:
# the caller routes on this string, so vagueness here is a routing failure.
LANDING_PATH = {
    LANDED: "none — the commit is already reachable from the target ref",
    STRANDED_WORKER_REF: (
        "worker-ref hunk carry: read the diff off the worker ref and re-apply it "
        "onto a branch off the target ref, then land that. Do NOT cherry-pick the "
        "worker commit wholesale — worker refs carry unrelated co-resident work."
    ),
    STRANDED_REMOTE_BRANCH: (
        "branch/PR merge: find the PR for the containing branch and merge it. "
        "Resolve any sign-off gate named in the PR body against the GOVERNING "
        "record first (a named gate may have expired). Merge with --merge, never "
        "--squash/--rebase — downstream sweeps test SHA containment (guard-3465)."
    ),
    STRANDED_LOCAL_ONLY: (
        "push: the commit exists in this clone and on no remote ref at all. "
        "Push the containing branch before anything can consume it."
    ),
    ABSENT: (
        "none available from here — the sha is reachable from no ref this clone "
        "can see. Re-fetch, or treat the citation as unverified (guard-3320: do "
        "NOT redo work on the strength of an unreachable sha)."
    ),
    INCONCLUSIVE: "unknown — a probe failed; fix the probe before routing",
}


def _git(repo: str, *args: str, timeout: int = 60):
    """Run a git command. Returns (rc, stdout, stderr) — never raises."""
    try:
        p = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout after {timeout}s"
    except Exception as exc:  # pragma: no cover - defensive
        return -1, "", f"{type(exc).__name__}: {exc}"


def object_exists(repo: str, sha: str) -> bool:
    """True iff `sha` names a commit object in this clone."""
    rc, out, _ = _git(repo, "cat-file", "-t", sha)
    return rc == 0 and out == "commit"


def is_ancestor(repo: str, sha: str, ref: str):
    """Three-valued ancestry test.

    Returns True / False / None. The None is the entire point and is the
    single most-copied defect in this family (/is-change-live's Restricted
    Operations #2): `merge-base --is-ancestor` returns rc=1 for a genuine
    "not an ancestor" and rc=128 when an argument is not a known object.
    Code that tests only `rc != 0` reports an UNFETCHED sha as "not landed",
    which is a false negative pointing at the destructive direction once a
    caller inverts it. rc=128 is NOT an answer -> None -> INCONCLUSIVE.
    """
    rc, _, _ = _git(repo, "merge-base", "--is-ancestor", sha, ref)
    if rc == 0:
        return True
    if rc == 1:
        return False
    return None


def containing_refs(repo: str, sha: str, pattern: str):
    """Refs matching `pattern` from which `sha` is reachable.

    Uses `for-each-ref --contains`, NOT `branch -r --contains`. That choice is
    the reason this script exists: `branch -r` enumerates refs/remotes/** only,
    so refs/workers/** is invisible to it no matter how the pattern is written.
    """
    rc, out, _ = _git(
        repo, "for-each-ref", "--contains", sha, "--format=%(refname)", pattern
    )
    if rc != 0:
        return None  # unreadable -> caller must not treat as "no refs"
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def fetch_worker_refs(repo: str, remote: str, namespace: str):
    """Mirror remote worker refs locally so containment can be tested.

    Containment needs the ref TIP OBJECT present locally; `ls-remote` alone
    only proves the ref exists. Returns (ok, detail).
    """
    rc, _, err = _git(
        repo, "fetch", "--quiet", remote,
        f"+refs/{namespace}/*:refs/remotes/_reach_{namespace}/*",
        timeout=180,
    )
    return (rc == 0), (err or "")


def triage(repo: str, sha: str, target_ref: str = "origin/main",
           remote: str = "origin", worker_namespace: str = "workers",
           do_fetch: bool = True) -> dict:
    """Classify where `sha` lives and what the landing path is."""
    result = {
        "sha": sha,
        "repo": repo,
        "target_ref": target_ref,
        "verdict": INCONCLUSIVE,
        "landed": None,
        "reason": "",
        "containing_refs": {},
        "fetch": {"attempted": do_fetch, "ok": None, "detail": ""},
    }

    rc, _, err = _git(repo, "rev-parse", "--git-dir")
    if rc != 0:
        result["reason"] = f"not a git repository: {err or repo}"
        return result

    if do_fetch:
        ok, detail = fetch_worker_refs(repo, remote, worker_namespace)
        result["fetch"]["ok"] = ok
        result["fetch"]["detail"] = detail
        # A failed worker-ref fetch is NOT fatal: the object may already be
        # present, and the target-ref test below is independent of it. But it
        # is recorded, because a STRANDED verdict reached without it may have
        # missed the namespace that actually contains the commit.

    if not object_exists(repo, sha):
        # Deliberately NOT "ABSENT". An object this clone has never fetched and
        # an object that exists nowhere are indistinguishable from here, and
        # only one of them is an answer.
        result["reason"] = (
            f"{sha} is not a commit object in this clone — fetch it and retry. "
            "Not reported as ABSENT: unfetched and nonexistent look identical "
            "from here, and only one is an answer."
        )
        return result

    anc = is_ancestor(repo, sha, target_ref)
    if anc is None:
        result["reason"] = (
            f"ancestry test against {target_ref} could not run (is the ref "
            "present in this clone?) — rc was neither 0 nor 1"
        )
        return result
    if anc is True:
        result["verdict"] = LANDED
        result["landed"] = True
        result["reason"] = f"{sha} is an ancestor of {target_ref}"
        result["landing_path"] = LANDING_PATH[LANDED]
        return result

    # Not an ancestor. Locate it. Order matters: worker refs are checked FIRST
    # because they are the namespace every neighbouring tool is blind to, and a
    # commit can legitimately sit in both (a worker ref whose tip was later
    # pushed to a branch) — the more specific landing path wins.
    worker = containing_refs(repo, sha, f"refs/remotes/_reach_{worker_namespace}/")
    local_worker = containing_refs(repo, sha, f"refs/{worker_namespace}/")
    remote_branches = containing_refs(repo, sha, "refs/remotes/")
    local_branches = containing_refs(repo, sha, "refs/heads/")

    if any(v is None for v in (worker, local_worker, remote_branches, local_branches)):
        result["reason"] = "one or more ref enumerations failed — cannot classify"
        return result

    # `refs/remotes/` is a superset that includes our mirrored worker namespace;
    # subtract it so a worker-only commit is never miscounted as branch-landed.
    mirror_prefix = f"refs/remotes/_reach_{worker_namespace}/"
    remote_branches = [r for r in remote_branches if not r.startswith(mirror_prefix)]

    all_worker = sorted(set(worker) | set(local_worker))
    result["containing_refs"] = {
        "worker": all_worker,
        "remote_branches": remote_branches,
        "local_branches": local_branches,
    }
    result["landed"] = False

    if all_worker:
        result["verdict"] = STRANDED_WORKER_REF
        result["reason"] = (
            f"not an ancestor of {target_ref}; reachable from {len(all_worker)} "
            f"worker ref(s): {', '.join(all_worker[:3])}"
            f"{' ...' if len(all_worker) > 3 else ''}. Neighbouring tools that "
            "probe with `git branch -r --contains` CANNOT see this."
        )
    elif remote_branches:
        result["verdict"] = STRANDED_REMOTE_BRANCH
        result["reason"] = (
            f"not an ancestor of {target_ref}; reachable from "
            f"{len(remote_branches)} remote branch(es): "
            f"{', '.join(remote_branches[:3])}"
            f"{' ...' if len(remote_branches) > 3 else ''}"
        )
    elif local_branches:
        result["verdict"] = STRANDED_LOCAL_ONLY
        result["reason"] = (
            f"not an ancestor of {target_ref}; reachable only from local "
            f"branch(es): {', '.join(local_branches[:3])} — never pushed"
        )
    else:
        result["verdict"] = ABSENT
        result["reason"] = (
            f"{sha} is a valid object but reachable from NO ref in this clone "
            "(dangling, or its only ref was deleted)"
        )

    result["landing_path"] = LANDING_PATH[result["verdict"]]
    return result


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Triage where a cited commit actually lives; classify its landing path."
    )
    ap.add_argument("--sha", required=True, help="the commit a goal/report cites as landed")
    ap.add_argument("--repo", default=".", help="path to the local clone (default: cwd)")
    ap.add_argument("--target-ref", default="origin/main",
                    help="the ref it is supposed to have reached (default: origin/main)")
    ap.add_argument("--remote", default="origin")
    ap.add_argument("--worker-namespace", default="workers",
                    help="ref namespace for worker refs, without refs/ (default: workers)")
    ap.add_argument("--no-fetch", action="store_true",
                    help="skip mirroring remote worker refs (offline / already fetched)")
    args = ap.parse_args()

    out = triage(
        repo=args.repo, sha=args.sha, target_ref=args.target_ref,
        remote=args.remote, worker_namespace=args.worker_namespace,
        do_fetch=not args.no_fetch,
    )
    print(json.dumps(out))
    # Exit 0 whenever the probe RAN. See the module docstring: the verdict is
    # the output, and a non-LANDED verdict is not an error.
    return 0


if __name__ == "__main__":
    sys.exit(main())
