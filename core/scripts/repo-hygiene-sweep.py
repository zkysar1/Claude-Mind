#!/usr/bin/env python3
"""Report-only git/GitHub hygiene sweep across every reachable clone ().

PHASE A ONLY. This script NEVER deletes, prunes, pushes, checks out, or
otherwise mutates a repository. Every lane emits CANDIDATES. That is the
`improvement-instructions.md` phased-rollout doctrine the goal mandates:
report-only for >= 3 cycles, and only then a gated deletion path behind a
config toggle with a per-run cap. There is deliberately NO `--apply` flag and
no deletion code to gate -- adding one before the report has been read three
times would be the whole point of the doctrine skipped.

WHY IT EXISTS. `promote-to-upstream.sh` creates and pushes `promote/*` with
ZERO deletion sites, and the per-goal PR workflow never deletes merged heads,
so git residue accumulates with no owner. Measured baseline at filing (bravo,
2026-08-12): 9 stale local promote/* branches on the staging clone, 4
promotion-window stashes on dev, 2 stashes + 7 dirty files on prod, and heavy
merged goal-branch residue across the product ring (one service repo alone
carried 25 remote heads).

FIVE THINGS THAT ARE LOAD-BEARING, EACH ONE A MEASURED INCIDENT:

1. `git fetch --prune` IS THE FIRST GIT ACTION PER REPO, and plain `fetch` is
   not a substitute. Plain fetch does not remove deleted-branch tracking refs,
   so a sweep classifying unpruned `git branch -r` state re-discovers
   already-deleted branches as work -- measured, 4 `promote/v*` ghosts on a
   downstream deployment clone survived a fetch a full day after their real
   deletion (rb-7719).
   Separately, on a multi-box fleet the box-local origin/* refs are stale for
   anything pushed from another box, so an unfetched probe false-concludes
   "not on remote" (guard-1250). `--no-fetch` exists for a fast re-run over an
   already-fetched estate and SAYS SO in the report, because a report that
   cannot distinguish fetched from stale is the defect above.

2. ANCESTRY IS NOT ENOUGH for the merged-branch lane. A squash-merged PR
   branch reads UNMERGED to `git merge-base --is-ancestor` -- measured on
   staging promote/v2.3.1, promote/v2.4.0 and promote/zds-reconcile-2026-07-31,
   all ancestry-UNMERGED while their PRs #6/#7/#15 were MERGED. The classifier
   is therefore `ancestry-merged OR gh-PR-state-MERGED`, and every candidate
   records its tip SHA as the recovery handle.

3. THE GOAL-STATUS JOIN IS MANDATORY, NOT AN ENRICHMENT. A branch name that
   matches a goal id with a NON-TERMINAL goal is untouchable regardless of age.
   Live example from the filing: `recover/orphan-chain-20260809` looks stale by
   every git signal and is load-bearing for open g-115-5637. A candidate list
   without this join is not a smaller list, it is a WRONG one.

4. AHEAD/BEHIND IS THE WRONG FRESHNESS PREDICATE ON A PROTECTED-BRANCH REPO
   (guard-1996). Main is protected, changes land SQUASHED via PR, while the
   local checkout reconciles with `git merge origin/main` -- so every merge
   adds a commit upstream will never contain and the repo reports a PERMANENT
   false divergence. The property that actually matters is CONTENT identity, so
   this lane reports the TREE-HASH comparison alongside the counts and marks
   `tree_identical` when `HEAD^{tree}` equals `origin/<branch>^{tree}`. Measured
   across 57 repos: exactly 2 showed the false-ahead shape, and they were
   exactly the 2 that receive PR traffic.

5. A NON-ACTION IS A DECISION (guard-3628). Lane 3 (unmerged branches and
   stashes) is REPORT-ONLY PERMANENTLY -- not "report-only until Phase B". It
   is listed here rather than omitted so that a future reader does not read its
   absence from the deletion lanes as an oversight and "finish" it.

SCOPE. PROJECT_ROOT (this repo) + every clone enumerated by
`product-repo-freshness.py --list`, which routes through
`_path_roots.compute_allowed_roots()` -- the SSOT. It is deliberately NOT a
second hand-maintained root list: a hand-maintained copy is exactly what made
the pre-execution freshness step iterate an EMPTY SET on every Linux box while
reporting success. ZDS-Mind *deployment clone* lanes are excluded per the goal
(operations, route to that deployment's own queue); the same customer's PRODUCT
repos are product sub-repos and ARE in scope -- that reading is stated because
the goal's
phrase naming the excluded deployment is ambiguous between a downstream Mind
CLONE and that customer's PRODUCT repos, and a silent choice here would be
unreviewable. The concrete deployment names live in the goal record, not in
framework code (domain-free-examples.md).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

GIT = shutil.which("git") or "git"
GH = shutil.which("gh") or "gh"

# A branch whose name embeds a goal id. Covers g-NNN-NN.. and asp-NNN.
GOAL_ID_RE = re.compile(r"\b(g-\d{1,4}-\d{1,4}|asp-\d{1,4})\b")

# Names that are never candidates for anything, in any lane.
PROTECTED_BRANCHES = {"main", "master", "HEAD", "develop"}

# A user's own namespace is never touched, ever. From the goal's live-run
# addendum: `zakcc/deterministic-driver` is the user's, not the fleet's.
USER_NAMESPACES = ("zakcc/",)

# `backup/`-named refs are user-blessed-deletion-only even when ancestry says
# they are fully merged with zero unique commits (goal addendum, ZDS
# backup/cc01-betaworks-snapshot-20260806).
BACKUP_PREFIXES = ("backup/",)

JUNK_GLOBS = ("*.stackdump", "*.orig", "*.rej")

# Non-terminal goal statuses. A branch joined to a goal in ANY of these is
# untouchable. Deliberately NOT the complement of {completed, skipped}: a status
# this sweep has never heard of must read as non-terminal, which the membership
# test below gives for free.
NON_TERMINAL = {"pending", "in-progress", "blocked", "active", "discovered"}


def run(args, cwd=None, timeout=120):
    """Run a command, never raise. Returns (rc, stdout, stderr)."""
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout or ""), (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "", "timeout after %ss" % timeout
    except Exception as exc:  # noqa: BLE001 - a sweep must never die on one repo
        return 125, "", "%s: %s" % (type(exc).__name__, exc)


def git(repo, *args, timeout=120):
    return run([GIT, "-C", str(repo), *args], timeout=timeout)


# ---------------------------------------------------------------- roster ----

def enumerate_repos(project_root):
    """Every clone to sweep. PROJECT_ROOT first, then the product estate.

    The product half is delegated to product-repo-freshness.py --list rather
    than re-derived, per the no-transcription contract (guard-2676): that
    script already routes roots through compute_allowed_roots(), and a second
    enumeration here would drift from it silently.
    """
    repos = [project_root]
    rc, out, err = run([sys.executable,
                        str(project_root / "core" / "scripts" /
                            "product-repo-freshness.py"),
                        "--list", "--json"], timeout=180)
    if rc != 0:
        return repos, ("product-repo-freshness --list failed rc=%s: %s"
                       % (rc, (err or out)[:300]))
    try:
        payload = json.loads(out)
    except Exception as exc:  # noqa: BLE001
        return repos, "could not parse --list json: %s" % exc
    for r in payload.get("enumerated") or []:
        p = Path(r)
        if p != project_root and (p / ".git").exists():
            repos.append(p)
    if len(repos) == 1:
        # LOUD, for the same reason product-repo-freshness.py is loud: an empty
        # enumeration must never render as a clean sweep.
        return repos, ("enumerated 0 product repos -- this is NOT an all-clear, "
                       "nothing outside PROJECT_ROOT was examined")
    return repos, None


# ------------------------------------------------------------ goal index ----

def load_open_goal_ids(project_root):
    """NON-TERMINAL goal ids AND the concatenated TEXT of those goals.

    Returns (ids, text_blob, note). BOTH halves are needed because the join runs
    in two directions and only one of them was obvious:

      (a) goal id embedded in the BRANCH NAME, that goal non-terminal
      (b) the BRANCH NAME appearing anywhere in a non-terminal goal's TEXT

    Direction (b) is the one the goal actually specifies ("grep branch name
    against world+agent queues") and it is the one that catches the canonical
    case. Measured on the first smoke run of this script: `origin/recover/
    orphan-chain-20260809` was classified a deletion CANDIDATE under (a) alone,
    because its name embeds no goal id at all -- while the goal that makes it
    load-bearing, g-115-5637, names the branch verbatim in its own text. That
    branch is the exact live example the filing goal calls out as untouchable,
    so an (a)-only join gets the one case it was warned about wrong.

    A short or generic branch name can match unrelated goal prose under (b).
    That is accepted deliberately: a false match KEEPS a branch, and over-keeping
    is the safe direction here (guard-3628 -- a non-action is a decision, and the
    cost of keeping a dead branch one more cycle is nothing against deleting a
    load-bearing one).

    An EMPTY index DISABLES the lane rather than emptying its keep-set -- an
    index that failed to load must not read as "no branch is owned".
    """
    ids = set()
    texts = []
    files = []
    try:
        sys.path.insert(0, str(project_root / "core" / "scripts"))
        from _paths import WORLD_DIR, agents_root  # type: ignore
        wp = Path(WORLD_DIR) / "aspirations.jsonl"
        if wp.exists():
            files.append(wp)
        for conf in sorted(Path(agents_root()).glob("*/aspirations.jsonl")):
            files.append(conf)
    except Exception as exc:  # noqa: BLE001
        return ids, "", "could not resolve queues: %s" % exc

    TEXT_FIELDS = ("title", "description", "origin_signal", "outcome_note",
                   "defer_reason", "failure_reason")
    for f in files:
        try:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                for g in (rec.get("goals") or []) if isinstance(rec, dict) else []:
                    if not isinstance(g, dict):
                        continue
                    if (g.get("status") or "pending") in NON_TERMINAL:
                        gid = g.get("goal_id") or g.get("id")
                        if gid:
                            ids.add(str(gid))
                        texts.append(" ".join(str(g.get(k) or "") for k in TEXT_FIELDS))
                if isinstance(rec, dict) and rec.get("status") in NON_TERMINAL:
                    aid = rec.get("aspiration_id") or rec.get("id")
                    if aid:
                        ids.add(str(aid))
                    texts.append(" ".join(str(rec.get(k) or "") for k in TEXT_FIELDS))
        except Exception:  # noqa: BLE001
            continue
    if not ids:
        return ids, "", ("goal index is EMPTY across %d queue file(s) -- the "
                         "merged-branch lane is DISABLED for this run rather "
                         "than run with an empty keep-set" % len(files))
    return ids, "\n".join(texts), None


def branch_goal_refs(name, open_ids):
    """Direction (a): goal ids embedded in a branch name that are NON-TERMINAL."""
    return sorted({m for m in GOAL_ID_RE.findall(name) if m in open_ids})


# A branch name too short or too generic to be evidence of anything. Matching
# "main" or "dev" against 14MB of goal prose would keep every branch forever,
# which is over-keeping past the point of usefulness rather than fail-safe.
_MIN_TEXT_JOIN_LEN = 8


def branch_named_in_goal_text(name, goal_text):
    """Direction (b): the branch name appears verbatim in non-terminal goal text.

    Tries the full short name first, then the name with any `origin/` prefix
    stripped -- a goal writes `recover/orphan-chain-20260809`, never
    `origin/recover/orphan-chain-20260809`, so matching only the remote-qualified
    form finds nothing and silently reduces this to direction (a).
    """
    if not goal_text:
        return None
    for cand in (name, name.split("origin/", 1)[-1]):
        if len(cand) >= _MIN_TEXT_JOIN_LEN and cand in goal_text:
            return cand
    return None


# ------------------------------------------------------------------ lanes ----

def lane_worktree(repo):
    """Lane 1 -- worktree residue. Report only; never prunes.

    `git worktree prune --dry-run` names what a real prune WOULD remove. It is
    reported, never executed: the goal requires daemon-orphan-sweep.sh to run
    FIRST before any real worktree dir is removed, because each checkout spawns
    its own daemon pair (rb-7489), and this script does not own that ordering.
    """
    out = {"prunable": [], "worktrees": [], "error": None}
    rc, so, se = git(repo, "worktree", "list", "--porcelain", timeout=60)
    if rc != 0:
        out["error"] = (se or so).strip()[:200]
        return out
    for block in so.split("\n\n"):
        d = {}
        for line in block.splitlines():
            if line.startswith("worktree "):
                d["path"] = line[len("worktree "):]
            elif line.startswith("branch "):
                d["branch"] = line[len("branch "):]
            elif line.strip() == "prunable":
                d["prunable"] = True
        if d.get("path"):
            out["worktrees"].append(d)
    rc, so, _ = git(repo, "worktree", "prune", "--dry-run", timeout=60)
    if rc == 0 and so.strip():
        out["prunable"] = [l.strip() for l in so.splitlines() if l.strip()]
    return out


OWNER_RE = re.compile(r"[:/]([^/:]+)/[^/]+?(?:\.git)?$")


def repo_owner(repo):
    """The origin remote's owner, or None. Offline -- parses the URL, no API."""
    rc, so, _ = git(repo, "remote", "get-url", "origin", timeout=30)
    if rc != 0:
        return None
    m = OWNER_RE.search(so.strip())
    return m.group(1) if m else None


def _default_branch(repo):
    rc, so, _ = git(repo, "symbolic-ref", "--quiet", "--short",
                    "refs/remotes/origin/HEAD", timeout=30)
    if rc == 0 and so.strip():
        return so.strip().split("/", 1)[-1]
    for cand in ("main", "master"):
        rc, _, _ = git(repo, "rev-parse", "--verify", "--quiet",
                       "refs/remotes/origin/%s" % cand, timeout=30)
        if rc == 0:
            return cand
    return "main"


def _gh_merged_heads(repo):
    """{headRefName: {number, mergeCommit}} for MERGED PRs.

    ONE call per repo, not one per branch: the per-branch shape would be ~25x
    the API traffic on a heavily-branched service repo and would push the
    sweep into rate-limit territory for no extra information.
    """
    if not shutil.which("gh"):
        return {}, "gh not on PATH"
    rc, so, se = run([GH, "pr", "list", "--state", "merged", "--limit", "100",
                      "--json", "number,headRefName,mergeCommit,mergedAt"],
                     cwd=str(repo), timeout=120)
    if rc != 0:
        return {}, (se or so).strip()[:200]
    try:
        rows = json.loads(so or "[]")
    except Exception as exc:  # noqa: BLE001
        return {}, "unparseable gh json: %s" % exc
    heads = {}
    for r in rows:
        h = r.get("headRefName")
        if h:
            heads[h] = {"pr": r.get("number"),
                        "merge_commit": (r.get("mergeCommit") or {}).get("oid"),
                        "merged_at": r.get("mergedAt")}
    return heads, None


def lane_branches(repo, open_ids, goal_text, goal_index_ok, merged_heads):
    """Lanes 2 and 3 -- merged-branch candidates, unmerged-branch report.

    Every branch lands in exactly one bucket and carries its tip SHA, so a
    Phase-B deletion has a recovery handle without re-deriving one.
    """
    res = {"merged_candidates": [], "unmerged_report": [], "kept": [],
           "error": None}
    base = _default_branch(repo)
    # %(refname) as well as the short form. git abbreviates
    # `refs/remotes/origin/HEAD` to the short name `origin`, which matches
    # nothing in PROTECTED_BRANCHES and sailed through the first smoke run as a
    # deletion candidate for a branch that does not exist. The FULL refname is
    # the only unambiguous discriminator, so the skip test reads that.
    rc, so, se = git(repo, "for-each-ref",
                     "--format=%(refname)\t%(refname:short)\t%(objectname)",
                     "refs/heads", "refs/remotes/origin", timeout=60)
    if rc != 0:
        res["error"] = (se or so).strip()[:200]
        return res

    for line in so.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        full, name, sha = parts
        if full.endswith("/HEAD") or full in ("refs/remotes/origin",):
            continue
        short = name.split("origin/", 1)[-1] if name.startswith("origin/") else name
        if short in PROTECTED_BRANCHES or short == base:
            continue
        if any(short.startswith(p) for p in USER_NAMESPACES):
            res["kept"].append({"branch": name, "sha": sha,
                                "reason": "user namespace -- never touched"})
            continue

        refs = branch_goal_refs(short, open_ids) if goal_index_ok else []
        if refs:
            res["kept"].append({"branch": name, "sha": sha, "join": "id-in-name",
                                "reason": "names NON-TERMINAL goal(s) %s -- "
                                          "untouchable regardless of age" % ",".join(refs)})
            continue
        hit = branch_named_in_goal_text(name, goal_text) if goal_index_ok else None
        if hit:
            res["kept"].append({"branch": name, "sha": sha, "join": "name-in-goal-text",
                                "reason": "branch name %r appears in NON-TERMINAL "
                                          "goal text -- untouchable regardless of "
                                          "age" % hit})
            continue
        if not goal_index_ok and GOAL_ID_RE.search(short):
            res["kept"].append({"branch": name, "sha": sha,
                                "reason": "goal index unavailable and branch "
                                          "names a goal id -- kept, fail-safe"})
            continue

        anc_rc, _, _ = git(repo, "merge-base", "--is-ancestor", sha,
                           "refs/remotes/origin/%s" % base, timeout=30)
        ancestry_merged = (anc_rc == 0)
        pr = merged_heads.get(short)
        if ancestry_merged or pr:
            entry = {"branch": name, "sha": sha, "base": base,
                     "ancestry_merged": ancestry_merged,
                     "pr_merged": bool(pr),
                     "pr": (pr or {}).get("pr"),
                     "merge_commit": (pr or {}).get("merge_commit"),
                     "classifier": ("ancestry" if ancestry_merged and not pr
                                    else "gh-pr-state" if pr and not ancestry_merged
                                    else "both")}
            if any(short.startswith(p) for p in BACKUP_PREFIXES):
                entry["blocked"] = ("backup-named -- user-blessed deletion only, "
                                    "never an automatic candidate")
                res["kept"].append(entry)
            else:
                res["merged_candidates"].append(entry)
        else:
            res["unmerged_report"].append({"branch": name, "sha": sha,
                                           "base": base})
    return res


def lane_stash(repo):
    """Lane 3 (stash half) -- REPORT-ONLY PERMANENTLY. Never a candidate."""
    rc, so, _ = git(repo, "stash", "list", "--date=iso", timeout=60)
    if rc != 0:
        return []
    return [l.strip() for l in so.splitlines() if l.strip()]


def lane_freshness(repo):
    """Lane 4 -- behind/ahead PLUS the tree-hash identity that actually matters.

    guard-1996: on a protected-branch repo the counts report a permanent false
    divergence, so `ahead` alone must never drive a conclusion. `tree_identical`
    is the content-level answer and is the field a reader should believe.
    """
    base = _default_branch(repo)
    out = {"base": base, "head_branch": None, "on_default": None,
           "behind": None, "ahead": None, "dirty": None,
           "tree_identical": None, "tree_compare_meaningful": None,
           "error": None}
    # WHICH BRANCH HEAD IS ON is not decoration -- without it the ahead/behind
    # pair is UNINTERPRETABLE, and it reads exactly like divergence. Measured on
    # the first full run: 20 of 59 repos reported ahead>0 with differing trees
    # and were read as "genuinely diverged"; AcceptTosLambda's `ahead=1
    # behind=2` was HEAD sitting on the feature branch
    # fix/-zip-cache-exclusions, one commit ahead of origin/master.
    # Correct arithmetic, wrong question. A checkout parked on a stale feature
    # branch is its own residue class and is reported as such.
    rc, so, _ = git(repo, "rev-parse", "--abbrev-ref", "HEAD", timeout=30)
    if rc == 0:
        out["head_branch"] = so.strip()
        out["on_default"] = (so.strip() == base)
    rc, so, se = git(repo, "rev-list", "--left-right", "--count",
                     "HEAD...refs/remotes/origin/%s" % base, timeout=60)
    if rc == 0 and so.strip():
        parts = so.split()
        if len(parts) == 2:
            out["ahead"], out["behind"] = int(parts[0]), int(parts[1])
    else:
        out["error"] = (se or so).strip()[:160]
    rc, so, _ = git(repo, "status", "--porcelain", timeout=60)
    if rc == 0:
        out["dirty"] = len([l for l in so.splitlines() if l.strip()])
    rc1, t1, _ = git(repo, "rev-parse", "HEAD^{tree}", timeout=30)
    rc2, t2, _ = git(repo, "rev-parse", "refs/remotes/origin/%s^{tree}" % base,
                     timeout=30)
    if rc1 == 0 and rc2 == 0:
        out["tree_identical"] = (t1.strip() == t2.strip())
    # guard-1996's tree test answers "is this checkout content-forked from
    # upstream" ONLY when HEAD is on the default branch AND is not behind. If
    # the repo is BEHIND, the trees differ because upstream moved -- a true and
    # entirely uninformative fact. If HEAD is on a feature branch, they differ
    # because that is what a feature branch is. Reporting tree_identical without
    # this flag manufactures a divergence finding out of normal state.
    out["tree_compare_meaningful"] = bool(out["on_default"]
                                          and (out["behind"] == 0))
    return out


def lane_junk(repo):
    """Lane 5 -- stray junk files. Uses git's own ignore rules via ls-files."""
    hits = []
    rc, so, _ = git(repo, "ls-files", "--others", "--exclude-standard",
                    "--", *JUNK_GLOBS, timeout=60)
    if rc == 0:
        hits.extend(l.strip() for l in so.splitlines() if l.strip())
    rc, so, _ = git(repo, "ls-files", "--", *JUNK_GLOBS, timeout=60)
    if rc == 0:
        hits.extend(l.strip() + " (TRACKED)" for l in so.splitlines() if l.strip())
    return sorted(set(hits))


def lane_open_prs(repo, stale_days=14):
    """Lane 6 -- open-PR staleness report."""
    if not shutil.which("gh"):
        return [], "gh not on PATH"
    rc, so, se = run([GH, "pr", "list", "--state", "open", "--limit", "100",
                      "--json", "number,title,updatedAt,isDraft,headRefName"],
                     cwd=str(repo), timeout=120)
    if rc != 0:
        return [], (se or so).strip()[:200]
    try:
        rows = json.loads(so or "[]")
    except Exception as exc:  # noqa: BLE001
        return [], "unparseable gh json: %s" % exc
    return rows, None


# ------------------------------------------------------------------- main ----

def sweep_repo(repo, open_ids, goal_text, goal_index_ok, do_fetch, stale_days,
               home_owner=None):
    rec = {"repo": str(repo), "name": repo.name, "fetched": False,
           "fetch_error": None, "owner": repo_owner(repo),
           "third_party": None}
    # THIRD-PARTY CLONES ARE IN THE ENUMERATED ESTATE AND ARE NOT OURS TO TIDY.
    # Measured on the first full run: 2 of 59 repos belong to an upstream
    # project, and those 2 contributed 13 of the 14 open PRs older than 30 days
    # -- all of them other people's contributions, on branches like
    # `<contributor>/<feature>`. Counting them as fleet residue does not merely
    # inflate a number; it invites filing work to close PRs the fleet has no
    # standing to close. The owner comparison is DERIVED from PROJECT_ROOT's own
    # origin rather than hardcoded, so it needs no edit when the estate changes
    # and leaks no domain name into framework code.
    if home_owner and rec["owner"]:
        rec["third_party"] = (rec["owner"] != home_owner)
    if do_fetch:
        # --prune is not optional. See docstring point 1.
        rc, _, se = git(repo, "fetch", "--prune", "origin", timeout=180)
        rec["fetched"] = (rc == 0)
        if rc != 0:
            rec["fetch_error"] = (se or "").strip()[:200]

    merged_heads, gh_err = _gh_merged_heads(repo)
    rec["gh_error"] = gh_err
    rec["worktree"] = lane_worktree(repo)
    rec["branches"] = lane_branches(repo, open_ids, goal_text, goal_index_ok,
                                    merged_heads)
    rec["stashes"] = lane_stash(repo)
    rec["freshness"] = lane_freshness(repo)
    rec["junk"] = lane_junk(repo)
    prs, pr_err = lane_open_prs(repo, stale_days)
    rec["open_prs"] = prs
    rec["open_prs_error"] = pr_err
    return rec


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help="JSONL report path (default: "
                                  "world/audit-reports/repo-hygiene-<date>.jsonl)")
    ap.add_argument("--no-fetch", action="store_true",
                    help="skip the mandatory `git fetch --prune`. Marked in the "
                         "report -- an unfetched run's branch verdicts are STALE.")
    ap.add_argument("--repo", action="append", default=[],
                    help="limit to repos whose directory name matches (repeatable)")
    ap.add_argument("--stale-days", type=int, default=14)
    ap.add_argument("--json", action="store_true", help="summary as JSON")
    ap.add_argument("--date", help="report date stamp (no clock call in this script)")
    args = ap.parse_args(argv)

    # PROJECT_ROOT from the _paths SSOT, never a .parent hop count -- the
    # latter is the  bug class (a helper that re-derived the root
    # by counting .parent silently 404'd its own config for weeks).
    try:
        from _paths import PROJECT_ROOT as _PR  # type: ignore
        project_root = Path(_PR)
    except Exception:  # noqa: BLE001
        project_root = Path(__file__).resolve().parent.parent.parent
    repos, roster_note = enumerate_repos(project_root)
    if args.repo:
        want = {r.lower() for r in args.repo}
        repos = [r for r in repos if r.name.lower() in want]

    open_ids, goal_text, goal_note = load_open_goal_ids(project_root)
    home_owner = repo_owner(project_root)
    goal_index_ok = bool(open_ids)

    out_path = Path(args.out) if args.out else None
    if out_path is None:
        try:
            sys.path.insert(0, str(project_root / "core" / "scripts"))
            from _paths import WORLD_DIR  # type: ignore
            stamp = args.date or "undated"
            out_path = Path(WORLD_DIR) / "audit-reports" / ("repo-hygiene-%s.jsonl" % stamp)
        except Exception:  # noqa: BLE001
            out_path = project_root / "repo-hygiene.jsonl"

    records = []
    for repo in repos:
        records.append(sweep_repo(repo, open_ids, goal_text, goal_index_ok,
                                  not args.no_fetch, args.stale_days,
                                  home_owner=home_owner))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, sort_keys=True) + "\n")

    ours = [r for r in records if not r.get("third_party")]
    foreign = [r for r in records if r.get("third_party")]
    totals = {
        "repos": len(records),
        "third_party_repos_excluded_from_candidates": len(foreign),
        "fetched": sum(1 for r in records if r["fetched"]),
        "fetch_failed": sum(1 for r in records if r["fetch_error"]),
        "merged_candidates": sum(len(r["branches"]["merged_candidates"]) for r in ours),
        "unmerged_report": sum(len(r["branches"]["unmerged_report"]) for r in ours),
        "kept_by_join": sum(len(r["branches"]["kept"]) for r in records),
        "kept_by_name_in_goal_text": sum(
            1 for r in ours for k in r["branches"]["kept"]
            if k.get("join") == "name-in-goal-text"),
        "stashes": sum(len(r["stashes"]) for r in records),
        "prunable_worktrees": sum(len(r["worktree"]["prunable"]) for r in records),
        "junk_files": sum(len(r["junk"]) for r in records),
        "open_prs": sum(len(r["open_prs"]) for r in ours),
        "behind_repos": sum(1 for r in records
                            if (r["freshness"].get("behind") or 0) > 0),
        # guard-1996's shape requires HEAD to be ON the default branch. Without
        # that clause this counter reported 2 repos as protected-branch topology
        # when both were simply parked on a feature branch -- the same
        # misreading, one level up, in the SUMMARY rather than the record.
        "false_ahead_repos": sum(1 for r in records
                                 if (r["freshness"].get("ahead") or 0) > 0
                                 and r["freshness"].get("on_default") is True
                                 and r["freshness"].get("tree_identical") is True),
        "off_default_checkouts": sum(1 for r in records
                                     if r["freshness"].get("on_default") is False),
        "content_forked_repos": sum(1 for r in records
                                    if r["freshness"].get("tree_compare_meaningful")
                                    and r["freshness"].get("tree_identical") is False),
        "dirty_repos": sum(1 for r in records if (r["freshness"].get("dirty") or 0) > 0),
    }
    summary = {"phase": "A (report-only)", "report": str(out_path),
               "home_owner": home_owner,
               "third_party": [r["name"] for r in foreign],
               "fetched_this_run": not args.no_fetch,
               "roster_note": roster_note, "goal_index_note": goal_note,
               "goal_index_size": len(open_ids), "totals": totals}

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0

    print("[repo-hygiene] PHASE A -- REPORT ONLY. Nothing was deleted, pruned, "
          "or pushed.")
    if roster_note:
        print("[repo-hygiene] ROSTER: %s" % roster_note)
    if goal_note:
        print("[repo-hygiene] GOAL INDEX: %s" % goal_note)
    if args.no_fetch:
        print("[repo-hygiene] --no-fetch: branch verdicts below are STALE for "
              "anything pushed from another box (guard-1250).")
    print("[repo-hygiene] report: %s" % out_path)
    for k, v in totals.items():
        print("  %-22s %s" % (k, v))
    print("[repo-hygiene] 'false_ahead_repos' counts repos reporting ahead>0 "
          "with an IDENTICAL tree -- protected-branch topology, not divergence "
          "(guard-1996).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
