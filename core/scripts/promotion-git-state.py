#!/usr/bin/env python3
"""Promotion git-state integration — the two ends promote-to-upstream.sh does not own.

Standalone cross-repo git tool (NOT a daemon endpoint, so no runtime dependency
and no Python-CLI-fallback concern — it never touches agent state). Sibling of
`promotion-preflight.py`, which audits CONTENT drift; this one audits GIT STATE.

Two subcommands, named for the two halves of g-115-5943:

  freshness   BEFORE the hop. Is the target clone fresh, and is fast-forwarding
              it safe given what is already dirty there?
  postflight  AFTER the merge. Did it land, and what git state did the hop leave
              behind that nothing else deletes?

WHY A SCRIPT AND NOT A RUNBOOK CHECKLIST (guard-399). "A hook whose invocation
is prose does not fire." The runbook's Phase 0 and Phase 7 each name ONE command
— this one — rather than a list of git incantations for a reader to type. The
prose steps that remain in the runbook are the ones that genuinely cannot be
mechanised, and they are marked as such in both places (guard-365: keep
category (b) visually distinct from category (a)).

STEP CLASSIFICATION (guard-365), carried here so the two documents cannot drift:

  (a) MECHANICAL — done or checked by this script:
        freshness:  fetch --prune, ahead/behind, dirty set, incoming-diff set,
                    the disjointness proof, the ff decision
        postflight: merge-landed verdict, main-run-by-headSha, branch deletion
                    (local + remote), stale-worktree scan, tag reachability,
                    final fetch-and-confirm across all named repos
  (b) LLM-PAYLOAD-REQUIRED — DETECTED here, never resolved here. Printed under a
      separate `LLM OBLIGATIONS` heading so it cannot be read as done:
        - resolving a pre-promotion stash (apply-or-archive; never silent-drop)
        - deleting a plant clone (archive-before-delete governs; the daemon
          orphan sweep must run FIRST — rb-7489)
  (c) OBSOLETE / ALREADY OWNED — deliberately NOT re-implemented:
        - `git worktree remove` + `prune`: promote-to-upstream.sh already calls
          worktree-teardown.sh at its `_wt_teardown` site. This script only
          VERIFIES no stale worktree survived, and says so.
        - the `--auto-merge` CRLF status-check parse: owned by g-115-5645. This
          script does not read or modify that path.

THREE GIT FACTS THIS TOOL IS BUILT AROUND, each measured, each of which silently
inverts a naive check:

  1. `git fetch` DOES NOT PRUNE DELETED BRANCHES (guard-4463). A stale local
     `remotes/origin/promote/*` ref keeps an orphaned tip alive, so an
     "unlanded commit" scan reports commits that shipped weeks ago. Every fetch
     here passes `--prune`, and no verdict is taken from a local remote ref.
  2. A SQUASH MERGE lands the content under a NEW sha, so the branch tip can
     NEVER become an ancestor of main — not "not yet", permanently. Ancestry is
     therefore CORROBORATION here and never the verdict; the verdict is the PR's
     own `state == MERGED` plus its recorded merge commit.
  3. `gh pr checks` green proves the PR gate only (guard-5017). The merge commit
     is a new commit with its own workflow run, and main-only jobs exist ONLY
     there. Matching that run BY RECENCY grabs the PREVIOUS push's run — already
     complete, reading as an instant green for a commit it never touched. This
     matches by headSha or reports that no run exists yet.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

# ─────────────────────────────────────────────────────────────────────────────
# git / gh plumbing


def _run(args, cwd=None, timeout=90, preserve_columns=False):
    """Run a command and return (rc, stdout, stderr) with output PRESERVED.

    No `2>/dev/null`, no `|| true`: a silently-failed command that returns empty
    output has told you nothing (verify-before-assuming.md rule 4), and every
    caller here branches on rc explicitly rather than on emptiness.

    `preserve_columns=True` rstrips ONLY, and it is mandatory for any format
    whose FIRST COLUMN is significant. Measured while writing this file: the
    convenient `.strip()` ate the leading space of `git status --porcelain`'s
    two-character status field, so ` M shared.txt` became `M shared.txt` and the
    `line[3:]` path slice returned `hared.txt`. Every dirty path was silently
    mangled by one character — the COUNT stayed correct (2 lines in, 2 paths
    out), so `dirty_count` looked right while the set intersection that decides
    whether a fast-forward may clobber uncommitted work came back EMPTY on a
    genuine collision. A whitespace convenience turned the safety gate into a
    rubber stamp, and only a positive control caught it: the UNSAFE fixture
    reported SAFE. Do not remove `preserve_columns` from `_dirty_set`.
    """
    try:
        p = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        out = p.stdout or ""
        out = out.rstrip("\r\n") if preserve_columns else out.strip()
        return p.returncode, out, (p.stderr or "").strip()
    except FileNotFoundError as e:
        return 127, "", f"not found: {e}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"


def _git(repo, *args, timeout=90, preserve_columns=False):
    return _run(
        ["git", "-C", repo, *args],
        timeout=timeout,
        preserve_columns=preserve_columns,
    )


def _gh_bin():
    return shutil.which("gh")


def _gh(repo, *args, timeout=90):
    gh = _gh_bin()
    if not gh:
        return 127, "", "gh not on PATH"
    return _run([gh, *args], cwd=repo, timeout=timeout)


def _is_repo(path):
    if not path or not os.path.isdir(path):
        return False
    rc, out, _ = _git(path, "rev-parse", "--is-inside-work-tree")
    return rc == 0 and out == "true"


def _fetch_pruned(repo):
    """The ONLY fetch shape this tool uses. See git fact 1 in the docstring."""
    return _git(repo, "fetch", "--prune", "--tags", timeout=180)


def _dirty_set(repo):
    """Paths with uncommitted changes, as a set of repo-relative paths."""
    # preserve_columns: porcelain's status field is TWO characters wide and its
    # first may be a space (` M path`). Stripping shifts every path by one.
    rc, out, err = _git(repo, "status", "--porcelain", preserve_columns=True)
    if rc != 0:
        return None, f"git status failed rc={rc}: {err}"
    paths = set()
    for line in out.splitlines():
        if len(line) < 4:
            continue
        p = line[3:]
        # Rename/copy entries read "old -> new"; the NEW path is the one at risk.
        if " -> " in p:
            p = p.split(" -> ", 1)[1]
        paths.add(p.strip().strip('"'))
    return paths, None


def _incoming_set(repo, upstream_ref):
    """Paths a fast-forward to `upstream_ref` would touch."""
    rc, out, err = _git(repo, "diff", "--name-only", f"HEAD..{upstream_ref}")
    if rc != 0:
        return None, f"git diff failed rc={rc}: {err}"
    return {ln.strip() for ln in out.splitlines() if ln.strip()}, None


def _ahead_behind(repo, upstream_ref):
    rc, out, err = _git(
        repo, "rev-list", "--left-right", "--count", f"HEAD...{upstream_ref}"
    )
    if rc != 0:
        return None, None, f"rev-list failed rc={rc}: {err}"
    parts = out.split()
    if len(parts) != 2:
        return None, None, f"unparseable rev-list output: {out!r}"
    return int(parts[0]), int(parts[1]), None


# ─────────────────────────────────────────────────────────────────────────────
# freshness (BEFORE the hop)


def cmd_freshness(a):
    """Is the target clone fresh, and is fast-forwarding it safe?

    The disjointness proof is the load-bearing part and it is why this is not
    just `git pull`: fleet agents dirty shared ledgers in the target clone
    continuously, so a blind ff either refuses or clobbers. What makes an ff
    SAFE is that the dirty set and the incoming set do not intersect — the same
    proof that protected 14 dirty files on a live production clone.
    """
    r = {
        "subcommand": "freshness",
        "target": a.target,
        "upstream": a.upstream,
        "verdict": None,
        "reason": None,
        "fetched": False,
        "ahead": None,
        "behind": None,
        "dirty_count": None,
        "incoming_count": None,
        "intersection": [],
        "applied_ff": False,
        "errors": [],
    }
    if not _is_repo(a.target):
        r["verdict"] = "UNREADABLE"
        r["reason"] = f"--target is not a git work tree: {a.target}"
        r["errors"].append(r["reason"])
        return r, 3

    rc, _, err = _fetch_pruned(a.target)
    if rc != 0:
        r["verdict"] = "UNREADABLE"
        r["reason"] = f"fetch --prune failed rc={rc}: {err}"
        r["errors"].append(r["reason"])
        return r, 3
    r["fetched"] = True

    ahead, behind, e = _ahead_behind(a.target, a.upstream)
    if e:
        r["verdict"] = "UNREADABLE"
        r["reason"] = e
        r["errors"].append(e)
        return r, 3
    r["ahead"], r["behind"] = ahead, behind

    dirty, e = _dirty_set(a.target)
    if e:
        r["verdict"] = "UNREADABLE"
        r["reason"] = e
        r["errors"].append(e)
        return r, 3
    r["dirty_count"] = len(dirty)

    incoming, e = _incoming_set(a.target, a.upstream)
    if e:
        r["verdict"] = "UNREADABLE"
        r["reason"] = e
        r["errors"].append(e)
        return r, 3
    r["incoming_count"] = len(incoming)

    inter = sorted(dirty & incoming)
    r["intersection"] = inter

    if behind == 0:
        r["verdict"] = "FRESH"
        r["reason"] = f"already at {a.upstream} (ahead {ahead}, behind 0)"
        return r, 0
    if ahead > 0:
        # A diverged target cannot fast-forward at all; that is a reconcile-UP
        # question (runbook Phase 1), not a freshness one. Refuse rather than
        # inventing a merge here.
        r["verdict"] = "DIVERGED"
        r["reason"] = (
            f"target is ahead {ahead} and behind {behind} — cannot fast-forward. "
            f"Reconcile UP first (runbook Phase 1, guard-119)."
        )
        return r, 2
    if inter:
        r["verdict"] = "UNSAFE"
        r["reason"] = (
            f"{len(inter)} path(s) are BOTH dirty in the target AND touched by "
            f"the incoming {behind}-commit ff — fast-forwarding would clobber "
            f"uncommitted work. Commit those as a `chore:` (never discard) first."
        )
        return r, 2

    r["verdict"] = "SAFE"
    r["reason"] = (
        f"behind {behind}, {len(dirty)} dirty path(s), {len(incoming)} incoming "
        f"path(s), intersection EMPTY — ff is safe"
    )
    if a.apply:
        rc, _, err = _git(a.target, "merge", "--ff-only", a.upstream)
        if rc != 0:
            r["verdict"] = "FF_FAILED"
            r["reason"] = f"git merge --ff-only failed rc={rc}: {err}"
            r["errors"].append(r["reason"])
            return r, 2
        r["applied_ff"] = True
    return r, 0


# ─────────────────────────────────────────────────────────────────────────────
# postflight (AFTER the merge)


def _pr_facts(repo, pr):
    """The MERGE VERDICT. PR state is authoritative; ancestry never is."""
    rc, out, err = _gh(
        repo,
        "pr",
        "view",
        pr,
        "--json",
        "state,mergedAt,mergeCommit,headRefName,url",
    )
    if rc != 0:
        return None, f"gh pr view failed rc={rc}: {err}"
    try:
        return json.loads(out), None
    except json.JSONDecodeError as e:
        return None, f"gh pr view returned unparseable JSON: {e}"


def _main_run_by_sha(repo, sha):
    """guard-5017: match by headSha, NEVER by recency."""
    rc, out, err = _gh(
        repo,
        "run",
        "list",
        "--branch",
        "main",
        "--limit",
        "40",
        "--json",
        "databaseId,headSha,status,conclusion",
    )
    if rc != 0:
        return None, f"gh run list failed rc={rc}: {err}"
    try:
        runs = json.loads(out)
    except json.JSONDecodeError as e:
        return None, f"gh run list returned unparseable JSON: {e}"
    for run in runs:
        if run.get("headSha") == sha:
            return run, None
    # Absence is a real, reportable state: immediately after a merge the run is
    # queued and not yet listed. It is NOT a green.
    return None, None


def cmd_postflight(a):
    r = {
        "subcommand": "postflight",
        "target": a.target,
        "branch": a.branch,
        "pr": a.pr,
        "apply": bool(a.apply),
        "mechanical": {},
        "llm_obligations": [],
        "not_reimplemented": [],
        "errors": [],
    }
    if not _is_repo(a.target):
        r["errors"].append(f"--target is not a git work tree: {a.target}")
        return r, 3

    m = r["mechanical"]

    # (a) final fetch-and-confirm — FIRST, so every later read is against
    # pruned refs (guard-4463).
    rc, _, err = _fetch_pruned(a.target)
    m["fetch_pruned"] = {"ok": rc == 0, "rc": rc, "stderr": err if rc else ""}
    if rc != 0:
        r["errors"].append(f"fetch --prune failed rc={rc}: {err}")

    # (a) merge-landed verdict
    merged = None
    merge_sha = None
    if a.pr:
        facts, e = _pr_facts(a.target, a.pr)
        if e:
            m["merge_landed"] = {"verdict": "UNREADABLE", "reason": e}
            r["errors"].append(e)
        else:
            state = facts.get("state")
            merge_sha = (facts.get("mergeCommit") or {}).get("oid")
            merged = state == "MERGED"
            m["merge_landed"] = {
                "verdict": "MERGED" if merged else state,
                "merged_at": facts.get("mergedAt"),
                "merge_commit": merge_sha,
                "head_ref": facts.get("headRefName"),
                "basis": "gh pr state (authoritative); ancestry is NOT the verdict "
                "— a squash merge lands new shas, so the branch tip can never "
                "become an ancestor of main (guard-4463)",
            }
    else:
        m["merge_landed"] = {
            "verdict": "NOT_CHECKED",
            "reason": "no --pr given; the merge verdict requires the PR, not ancestry",
        }

    # (a) main workflow run, matched BY HEADSHA (guard-5017)
    if merge_sha:
        run, e = _main_run_by_sha(a.target, merge_sha)
        if e:
            m["main_run"] = {"verdict": "UNREADABLE", "reason": e}
            r["errors"].append(e)
        elif run is None:
            m["main_run"] = {
                "verdict": "NO_RUN_YET",
                "head_sha": merge_sha,
                "reason": "no run listed for this sha yet — queued, or none exists. "
                "This is NOT a green; re-run postflight, or watch it by id.",
            }
        else:
            m["main_run"] = {
                "verdict": run.get("conclusion") or run.get("status"),
                "run_id": run.get("databaseId"),
                "head_sha": merge_sha,
            }
    else:
        m["main_run"] = {
            "verdict": "NOT_CHECKED",
            "reason": "no merge commit sha available to match on",
        }

    # (a) branch deletion — gated on the MERGED verdict, never on ancestry
    br = {"branch": a.branch, "local": None, "remote": None, "deleted": []}
    if a.branch:
        rc, _, _ = _git(a.target, "rev-parse", "--verify", f"refs/heads/{a.branch}")
        br["local"] = rc == 0
        rc, _, _ = _git(
            a.target, "rev-parse", "--verify", f"refs/remotes/origin/{a.branch}"
        )
        br["remote"] = rc == 0
        if not merged:
            br["action"] = "REFUSED — merge not confirmed MERGED; not deleting"
        elif not (br["local"] or br["remote"]):
            br["action"] = "already gone (local and remote both absent)"
        elif not a.apply:
            br["action"] = (
                "WOULD DELETE local=%s remote=%s — re-run with --apply"
                % (br["local"], br["remote"])
            )
        else:
            if br["local"]:
                rc, _, err = _git(a.target, "branch", "-D", a.branch)
                br["deleted"].append({"where": "local", "rc": rc, "stderr": err})
            if br["remote"]:
                rc, _, err = _git(a.target, "push", "origin", "--delete", a.branch)
                br["deleted"].append({"where": "remote", "rc": rc, "stderr": err})
            br["action"] = "deleted"
    else:
        br["action"] = "NOT_CHECKED — no --branch given"
    m["branch_cleanup"] = br

    # (a) stale-worktree scan — VERIFY only; teardown is category (c)
    rc, out, err = _git(a.target, "worktree", "list", "--porcelain")
    if rc != 0:
        m["worktrees"] = {"verdict": "UNREADABLE", "reason": err}
    else:
        wts = [
            ln.split(" ", 1)[1]
            for ln in out.splitlines()
            if ln.startswith("worktree ")
        ]
        extra = [w for w in wts if os.path.abspath(w) != os.path.abspath(a.target)]
        m["worktrees"] = {
            "total": len(wts),
            "extra": extra,
            "verdict": "CLEAN" if not extra else "STALE",
        }
    r["not_reimplemented"].append(
        "worktree remove + prune — promote-to-upstream.sh already calls "
        "worktree-teardown.sh at its _wt_teardown site; this only verifies."
    )
    r["not_reimplemented"].append(
        "--auto-merge CRLF status-check parse — owned by g-115-5645; untouched here."
    )

    # (a) tag reachability ( scheme landed)
    if a.tag:
        rc, _, _ = _git(a.target, "rev-parse", "--verify", f"refs/tags/{a.tag}")
        local_tag = rc == 0
        rc, out, err = _git(a.target, "ls-remote", "--tags", "origin", a.tag)
        remote_tag = rc == 0 and bool(out.strip())
        m["tag"] = {
            "tag": a.tag,
            "local": local_tag,
            "remote": remote_tag,
            "verdict": "OK" if (local_tag and remote_tag) else "MISSING",
        }
    else:
        m["tag"] = {"verdict": "NOT_CHECKED", "reason": "no --tag given"}

    # (a) ahead/behind confirm across every named repo
    confirm = {}
    for repo in [a.target] + list(a.also_confirm or []):
        if not _is_repo(repo):
            confirm[repo] = {"verdict": "NOT_A_REPO"}
            continue
        rc, _, err = _fetch_pruned(repo)
        if rc != 0:
            confirm[repo] = {"verdict": "UNREADABLE", "reason": err}
            continue
        ahead, behind, e = _ahead_behind(repo, a.upstream)
        confirm[repo] = (
            {"verdict": "UNREADABLE", "reason": e}
            if e
            else {
                "ahead": ahead,
                "behind": behind,
                "verdict": "SYNCED" if (ahead == 0 and behind == 0) else "DIVERGED",
            }
        )
    m["final_confirm"] = confirm

    # (b) LLM obligations — DETECTED, never resolved here.
    for repo in [a.target] + list(a.also_confirm or []):
        if not _is_repo(repo):
            continue
        rc, out, _ = _git(repo, "stash", "list")
        if rc == 0 and out.strip():
            entries = out.splitlines()
            r["llm_obligations"].append(
                {
                    "kind": "stash",
                    "repo": repo,
                    "count": len(entries),
                    "entries": entries[:5],
                    "obligation": "APPLY OR ARCHIVE — never silent-drop. "
                    "`git stash show -p stash@{N}` then pop, or record the patch "
                    "somewhere durable before dropping (archive-before-delete).",
                }
            )
    if a.plant_clone:
        r["llm_obligations"].append(
            {
                "kind": "plant_clone",
                "path": a.plant_clone,
                "exists": os.path.isdir(a.plant_clone),
                "obligation": "Run `core/scripts/daemon-orphan-sweep.sh` FIRST "
                "(rb-7489), confirm the ledger is archived, THEN delete. "
                "archive-before-delete governs; this script never deletes a clone.",
            }
        )

    rc_out = 0
    if r["errors"]:
        rc_out = 3
    elif (
        m["merge_landed"].get("verdict") not in ("MERGED", "NOT_CHECKED")
        or m.get("worktrees", {}).get("verdict") == "STALE"
        or any(v.get("verdict") == "DIVERGED" for v in confirm.values())
        or r["llm_obligations"]
    ):
        rc_out = 2
    return r, rc_out


# ─────────────────────────────────────────────────────────────────────────────
# rendering


def _render(r):
    out = []
    if r["subcommand"] == "freshness":
        out.append(
            f"[promotion-git-state] freshness: {r['verdict']} — {r['reason']}"
        )
        out.append(
            f"  target={r['target']} upstream={r['upstream']} "
            f"ahead={r['ahead']} behind={r['behind']} "
            f"dirty={r['dirty_count']} incoming={r['incoming_count']} "
            f"intersection={len(r['intersection'])}"
        )
        for p in r["intersection"][:10]:
            out.append(f"    CLOBBER-RISK {p}")
        if r["applied_ff"]:
            out.append("  fast-forward APPLIED")
        return "\n".join(out)

    m = r["mechanical"]
    out.append(f"[promotion-git-state] postflight: target={r['target']}")
    out.append("  ── (a) MECHANICAL — done or checked by this script ──")
    out.append(f"    merge-landed : {json.dumps(m.get('merge_landed'))}")
    out.append(f"    main-run     : {json.dumps(m.get('main_run'))}")
    out.append(f"    branch       : {json.dumps(m.get('branch_cleanup'))}")
    out.append(f"    worktrees    : {json.dumps(m.get('worktrees'))}")
    out.append(f"    tag          : {json.dumps(m.get('tag'))}")
    out.append(f"    final-confirm: {json.dumps(m.get('final_confirm'))}")
    out.append("  ── (b) LLM OBLIGATIONS — detected here, NOT done here ──")
    if not r["llm_obligations"]:
        out.append("    none outstanding")
    for o in r["llm_obligations"]:
        out.append(f"    [{o['kind']}] {json.dumps(o)}")
    out.append("  ── (c) NOT RE-IMPLEMENTED (already owned elsewhere) ──")
    for n in r["not_reimplemented"]:
        out.append(f"    {n}")
    for e in r["errors"]:
        out.append(f"  ERROR: {e}")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="promotion-git-state",
        description="Promotion git-state integration: preflight freshness + postflight cleanup.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("freshness", help="BEFORE the hop: is the target clone safely ff-able?")
    f.add_argument("--target", required=True)
    f.add_argument("--upstream", default="origin/main")
    f.add_argument("--apply", action="store_true", help="perform the ff when SAFE")
    f.add_argument("--json", action="store_true")

    p = sub.add_parser("postflight", help="AFTER the merge: did it land, what git state is left?")
    p.add_argument("--target", required=True)
    p.add_argument("--branch", default="", help="the promote/* branch to clean up")
    p.add_argument("--pr", default="", help="PR url or number — the merge verdict")
    p.add_argument("--tag", default="", help="release tag to verify (g-360-04 scheme)")
    p.add_argument("--upstream", default="origin/main")
    p.add_argument("--plant-clone", default="", help="plant clone path to report (never deleted here)")
    p.add_argument("--also-confirm", action="append", default=[], metavar="REPO")
    p.add_argument("--apply", action="store_true", help="perform branch deletion")
    p.add_argument("--json", action="store_true")

    a = ap.parse_args(argv)
    r, rc = (cmd_freshness if a.cmd == "freshness" else cmd_postflight)(a)
    print(json.dumps(r, indent=2) if a.json else _render(r))
    return rc


if __name__ == "__main__":
    sys.exit(main())
