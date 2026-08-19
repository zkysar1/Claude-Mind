#!/usr/bin/env python3
"""Layer-C detective for guard-793 — vendored/generated content in commits.

guard-793 ("confirm staged content contains ONLY the current goal's hunks
before staging in a shared product working tree") is honor-system with no
automated layer. This script is the observing half: it re-reads the committed
corpus after the fact and reports commits that ADDED generated content.

REPORT-ONLY, STRUCTURALLY. Every git invocation in this file is a read
(`rev-parse`, `log`, `diff-tree`). There is no commit, amend, rebase, filter,
reset, add, or push path, and there must never be one — history rewriting is
out of scope by the goal that commissioned this (g-115-5626) and would be
unrecoverable across a fleet that shares these working trees.

Usage:
    generated-content-commit-audit.py                  # 90d, all roots
    generated-content-commit-audit.py --days 30
    generated-content-commit-audit.py --json
    generated-content-commit-audit.py --repo <path>    # REPLACES the roots
    generated-content-commit-audit.py --include-ambiguous
    generated-content-commit-audit.py --exit-on-hits   # rc=1 when findings

Exit codes: 0 = clean (or findings without --exit-on-hits), 1 = findings with
--exit-on-hits, 2 = operational error.

READ `roots_unreachable` BEFORE READING `findings`. AGENT_WRITE_PATH is a
semicolon-separated list and an entry naming a path that does not exist on THIS
box is normal, not exceptional — measured 2026-08-11 on cc-03, 1 of the 3
configured roots was absent. A zero-finding run over 2 of 3 roots and a
zero-finding run over 3 of 3 print the same `findings: []`, so the coverage
line is what makes the zero falsifiable (guard-1760, rb-245).
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _generated_content_predicate import evaluate_commit  # noqa: E402

DEFAULT_DAYS = 90
GIT_TIMEOUT = 60


def _git(repo, *args):
    """Run a READ-ONLY git command. Returns (rc, stdout) — never raises."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo)] + list(args),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
        )
        return proc.returncode, proc.stdout
    except (OSError, subprocess.SubprocessError):
        return 127, ""


def discover_repos(explicit_repos=None):
    """Enumerate git repos, and report roots that could not be reached.

    AGENT_WRITE_PATH is SEMICOLON-separated, and each entry may itself be a
    repo OR a parent directory holding many. Treating it as a single parent
    silently covers a fraction of the corpus — the first draft of this sweep
    did exactly that and enumerated zero repos while reporting success.
    """
    if explicit_repos:
        repos, unreachable = [], []
        for r in explicit_repos:
            p = Path(r).expanduser()
            (repos if (p / ".git").exists() else unreachable).append(str(p))
        return sorted(set(repos)), [{"root": u, "reason": "not a git repo"} for u in unreachable]

    raw = os.environ.get("AGENT_WRITE_PATH", "")
    roots = [r.strip() for r in raw.split(";") if r.strip()]

    repos, unreachable = [], []
    for root in roots:
        p = Path(root).expanduser()
        if not p.exists():
            unreachable.append({"root": str(p), "reason": "path does not exist on this box"})
            continue
        if (p / ".git").exists():
            repos.append(str(p))
            continue
        children = []
        try:
            for child in sorted(p.iterdir()):
                if child.is_dir() and (child / ".git").exists():
                    children.append(str(child))
        except OSError as exc:
            unreachable.append({"root": str(p), "reason": "unreadable: %s" % exc})
            continue
        if children:
            repos.extend(children)
        else:
            unreachable.append({"root": str(p), "reason": "no git repos found under root"})

    return sorted(set(repos)), unreachable


def audit_repo(repo, days, include_ambiguous, min_added):
    """Scan one repo's recent commits.

    Returns (findings, error_or_None, unscanned) — `unscanned` is the list of
    commits whose path enumeration failed. It is a THIRD return value rather
    than a swallowed error because a commit this tool could not read is neither
    clean nor dirty, and reporting it as either is a lie.
    """
    unscanned = []
    rc, _ = _git(repo, "rev-parse", "--is-inside-work-tree")
    if rc != 0:
        return [], {"repo": repo, "reason": "git rev-parse failed (rc=%d)" % rc}, unscanned

    rc, out = _git(
        repo, "log", "--since=%d.days" % days, "--no-merges",
        "--format=%H%x1f%h%x1f%an%x1f%ad%x1f%s", "--date=short",
    )
    if rc != 0:
        return [], {"repo": repo, "reason": "git log failed (rc=%d)" % rc}, unscanned

    findings = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\x1f")
        if len(parts) != 5:
            continue
        sha, short, author, date, subject = parts

        # `--root` IS LOAD-BEARING. Without it `diff-tree` emits ZERO paths for a
        # ROOT commit (nothing to diff against), so a repo's initial commit is
        # invisible to this sweep — and an initial import is the single
        # highest-risk moment for vendoring a whole .venv/node_modules. A repo
        # whose first-and-only commit carried 545 vendored files would have been
        # reported clean. Measured 2026-08-11 on cc-03 by the end-to-end positive
        # control in test_generated_content_predicate.py, which failed exactly
        # here; `--root` leaves non-root commits byte-identical (verified side by
        # side), so it is free on every other commit.
        rc_all, out_all = _git(
            repo, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", sha,
        )
        rc_add, out_add = _git(
            repo, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r",
            "--diff-filter=A", sha,
        )

        # A FAILURE OF EITHER CALL IS A COVERAGE HOLE, NEVER A VERDICT — and the
        # second one is the dangerous half. Defaulting added_paths to [] when the
        # --diff-filter=A call fails does not merely lose the commit: it makes
        # `generated_added` zero while `generated_total` stays high, which is the
        # exact signature of a CLEANUP. Measured 2026-08-11 (cc-03, fresh-eyes
        # review of this file): a commit adding 545 vendored files reports
        # flagged=False, cleanup_only=True — the GOOD outcome — with no error
        # anywhere. That inverts the very signal the --diff-filter=A split exists
        # to protect. Skipping the first call silently was the milder twin: the
        # commit vanished while repos_scanned still counted its repo, overstating
        # coverage in a tool whose whole contract is a falsifiable denominator.
        if rc_all != 0 or rc_add != 0:
            unscanned.append({
                "repo": repo, "sha": short, "date": date,
                "subject": subject[:60], "rc_all": rc_all, "rc_add": rc_add,
            })
            continue

        all_paths = [p for p in out_all.splitlines() if p.strip()]
        added_paths = [p for p in out_add.splitlines() if p.strip()]

        verdict = evaluate_commit(
            all_paths, added_paths,
            include_ambiguous=include_ambiguous, min_added=min_added,
        )
        if not verdict["flagged"]:
            continue

        verdict.update({
            "repo": repo, "sha": sha, "short_sha": short,
            "author": author, "date": date, "subject": subject,
        })
        findings.append(verdict)

    return findings, None, unscanned


def main():
    ap = argparse.ArgumentParser(description="Layer-C detective for guard-793.")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--repo", action="append", dest="repos",
                    help="Audit this repo; REPLACES the AGENT_WRITE_PATH roots. Repeatable.")
    ap.add_argument("--include-ambiguous", action="store_true",
                    help="Also flag build/dist/env/out/target/coverage segments.")
    # Lower-bounded deliberately: `flagged = n_added >= min_added`, so --min-added 0
    # flags EVERY commit including clean ones (0 >= 0). A threshold flag whose
    # boundary value silently turns the detector into a firehose is a footgun, and
    # a firehose detector is one readers learn to skip.
    ap.add_argument("--min-added", type=int, default=1,
                    help="Minimum ADDED generated paths to flag a commit (must be >= 1).")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--exit-on-hits", action="store_true")
    args = ap.parse_args()

    if args.min_added < 1:
        ap.error("--min-added must be >= 1 (0 flags every commit, including clean ones)")

    try:
        repos, unreachable = discover_repos(args.repos)
    except Exception as exc:  # noqa: BLE001 - operational failure must be loud
        print("[generated-content-audit] ERROR: %s" % exc, file=sys.stderr)
        return 2

    findings, repo_errors, unscanned = [], [], []
    for repo in repos:
        got, err, un = audit_repo(repo, args.days, args.include_ambiguous, args.min_added)
        findings.extend(got)
        unscanned.extend(un)
        if err:
            repo_errors.append(err)

    findings.sort(key=lambda f: f["generated_added"], reverse=True)

    report = {
        "window_days": args.days,
        "repos_scanned": len(repos),
        "roots_unreachable": unreachable,
        "repo_errors": repo_errors,
        "unscanned_commits": unscanned,
        "include_ambiguous": args.include_ambiguous,
        "findings_count": len(findings),
        "findings": findings,
    }

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("[generated-content-audit] window=%dd repos_scanned=%d findings=%d"
              % (args.days, len(repos), len(findings)))
        for u in unreachable:
            print("  ! ROOT UNREACHABLE: %s (%s) — coverage is partial"
                  % (u["root"], u["reason"]))
        for e in repo_errors:
            print("  ! REPO ERROR: %s (%s)" % (e["repo"], e["reason"]))
        if unscanned:
            print("  ! %d COMMIT(S) UNSCANNED — verdict below is over a partial corpus"
                  % len(unscanned))
            for u in unscanned[:5]:
                print("      %s %s | %s (rc_all=%d rc_add=%d)"
                      % (u["short_sha"] if "short_sha" in u else u["sha"],
                         u["date"], Path(u["repo"]).name, u["rc_all"], u["rc_add"]))
        if not repos:
            print("  ! ZERO repos scanned — this is a coverage failure, not a clean corpus.")
        for f in findings:
            print("  %s %s | %s | %s"
                  % (f["short_sha"], f["date"], Path(f["repo"]).name, f["subject"][:60]))
            print("     %d files, %d generated ADDED (%d non-generated) | markers: %s"
                  % (f["total_files"], f["generated_added"],
                     f["non_generated_files"],
                     ", ".join("%s x%d" % (k, v) for k, v in f["markers"].items())))
            for s in f["sample"]:
                print("       %s" % s)
        if not findings and repos:
            print("  clean — no commit added generated content in this window")

    if findings and args.exit_on_hits:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
