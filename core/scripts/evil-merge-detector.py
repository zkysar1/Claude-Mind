#!/usr/bin/env python3
"""evil-merge-detector — flag merge commits whose conflict resolution dropped
feature/gate tokens from framework files (g-115-2464).

THE BLIND SPOT (rb-3692 / rb-3783 rule 2, 2026-07-16): a merge resolution that
takes a stale side can silently remove lines no branch intended to delete.
`git log -S/-G` (pickaxe) walks commit diffs and SKIPS merges by default, so
the removal is invisible to the standard history tools — the provenance-stamp
strip was caught only because a downstream gate refused loudly hours later.

ALGORITHM (per 2-parent merge M, parents P1/P2, base B = merge-base):
For each framework file F (core/scripts | core/config | .claude) present in
any of B/P1/P2/M, compare LINE SETS and flag token-bearing lines where:

  drop class        predicate                              meaning
  ----------------  -------------------------------------  ------------------------
  both-parents      l in P1 and l in P2 and l not in M     neither branch deleted it;
                                                           the RESOLUTION dropped it
  p1-addition       l in P1, l not in B, l not in M        mainline's own addition
                    (and, by construction, l not in P2)    thrown away by the merge —
                                                           the stale-side stamp shape
  p2-addition       l in P2, l not in B, l not in M        symmetric for the branch side

A line in P1 that P2's branch deliberately deleted (in B, absent from P2) and
that the merge honors is HEALTHY and never flagged — that is the everyday
merge shape the naive "present in parent, absent from result" rule would
drown in.

SECOND DAMAGE CLASS — DOUBLE (gap-061, g-115-5936, 2026-08-22):

  resolution-duplicated   count(l in M) > max(count in P1, P2, B)

Everything above is SET algebra, which is structurally blind to a resolution
that keeps BOTH sides: duplicating a line leaves every line in exactly the set
it was already in, so the drop predicates see a clean merge. Only the COUNT
moves — hence `_blob_line_counts` alongside `_blob_lines`. The predicate is
deliberately strict (MORE copies than ANY input held), so no input justifies
the multiplicity and there is no "a branch meant it" reading; that keeps the
recurring evil-merge audit (g-115-2473) from getting noisier, which matters
because its flags already adjudicate mostly benign.

The two classes are complementary halves of one question — "is the merged
result what both branches intended?" — and neither sees the other's shape:
DROP cannot see a duplication, DOUBLE cannot see a deletion.

SCOPE: `--paths` overrides FRAMEWORK_PREFIXES (`--paths ''` = whole tree).
gap-061's own encounters landed in product TypeScript (g-335-622) and a
GitHub Actions YAML (g-350-225) — both outside the framework default, so the
default scope cannot see them however correct the algebra is. NOT-COVERED,
stated because a green run announces nothing about where the line falls
(guard-1462): the CANCEL shape (a producer added by one side whose consumer
sites the other side removed — g-335-622's shape) needs cross-file symbol
analysis and is NOT implemented here; this file sees only within-file line
algebra. Do not read a clean report as "no semantic damage".

USAGE
  py -3 core/scripts/evil-merge-detector.py --since 2026-07-14 [--until X]
  py -3 core/scripts/evil-merge-detector.py --range A..B [--repo PATH] [--json]
  py -3 core/scripts/evil-merge-detector.py --since X --paths 'src/,.github/'
  Exit 0 = ran clean (flags or not); use --exit-on-hits for exit 2 on flags.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

FRAMEWORK_PREFIXES = ("core/scripts/", "core/config/", ".claude/")

# Feature-shaped token patterns — a dropped line must match >=1 to flag.
TOKEN_PATTERNS = [
    re.compile(r"\b(?:guard|rb|sig|sq|asp|pt)-\d+\b"),      # store ids
    re.compile(r"\bg-\d+-\d+\b"),                            # goal ids
    re.compile(r"^\s*(?:def|class)\s+\w+", re.M),            # py definitions
    re.compile(r"^\s*(?:function\s+)?\w[\w-]*\s*\(\)\s*\{"),  # sh functions
    re.compile(r"[A-Za-z0-9_-]+[-_]gate\b|\bgate[-_][A-Za-z0-9_-]+"),
    re.compile(r"^\s*[A-Za-z0-9_][A-Za-z0-9_-]*:(?:\s|$)"),  # YAML keys
    re.compile(r"(?:[A-Z][a-z0-9]+){2,}"),                   # CamelCase feature tokens (XPayloadProvenance, SendInfoAlert)
]

# Lines that pass the token filter but are structural noise (comment-only
# markers, log strings) still flag — precision tuning happens on evidence,
# not speculation. Only truly trivial lines are dropped here.
_TRIVIAL = re.compile(r"^[\s#/*'\"`{}()\[\],;-]*$")


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(["git", *args], cwd=str(repo), check=True,
                         capture_output=True, text=True, errors="replace")
    return res.stdout


def _blob_lines(repo: Path, rev: str, path: str) -> set[str]:
    """Line SET of path at rev; empty set when absent. Whitespace-stripped so
    indentation-only conflicts don't mask a dropped payload line."""
    try:
        out = _git(repo, "show", f"{rev}:{path}")
    except subprocess.CalledProcessError:
        return set()
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


def _blob_line_counts(repo: Path, rev: str, path: str) -> Counter:
    """Line MULTISET of path at rev; empty Counter when absent.

    Same normalization as _blob_lines (strip, drop blanks) so the two agree
    line-for-line. The multiplicity is the whole point: _blob_lines returns a
    SET, and a set cannot see the DOUBLE shape (gap-061) at all — a conflict
    resolution that keeps BOTH sides leaves every line present in exactly the
    same set it was already in, so set algebra reports a clean merge. Only the
    COUNT moves. Kept as a separate helper rather than converting _blob_lines,
    because every existing drop-class predicate is set algebra and would be
    silently re-typed by a swap.
    """
    try:
        out = _git(repo, "show", f"{rev}:{path}")
    except subprocess.CalledProcessError:
        return Counter()
    return Counter(ln.strip() for ln in out.splitlines() if ln.strip())


def _changed_framework_files(repo: Path, merge: str, p1: str, p2: str,
                             base: str,
                             prefixes: tuple[str, ...] = FRAMEWORK_PREFIXES
                             ) -> set[str]:
    """Files touched between any parent/base and the merge, limited to
    `prefixes`. An EMPTY prefixes tuple means the whole tree — `git diff -- `
    with no pathspec is unrestricted, which is what --paths '' selects."""
    files: set[str] = set()
    for a, b in ((p1, merge), (p2, merge), (base, merge)):
        try:
            out = _git(repo, "diff", "--name-only", a, b, "--", *prefixes)
        except subprocess.CalledProcessError:
            continue
        files.update(f for f in out.splitlines() if f.strip())
    return files


def _token_hits(line: str) -> list[str]:
    if _TRIVIAL.match(line):
        return []
    hits = []
    for pat in TOKEN_PATTERNS:
        m = pat.search(line)
        if m:
            hits.append(m.group(0).strip())
    return hits


def inspect_merge(repo: Path, merge_sha: str,
                  prefixes: tuple[str, ...] = FRAMEWORK_PREFIXES) -> dict:
    """Analyze one merge commit; returns {sha, parents, flags: [...]}.
    Octopus merges (>2 parents) are reported skipped — rare, and the
    2-parent algebra does not extend cleanly."""
    parents = _git(repo, "log", "-1", "--format=%P", merge_sha).split()
    rec = {"sha": merge_sha, "parents": parents, "flags": [], "skipped": None}
    if len(parents) != 2:
        rec["skipped"] = f"{len(parents)}-parent merge (only 2-parent supported)"
        return rec
    p1, p2 = parents
    try:
        base = _git(repo, "merge-base", p1, p2).strip()
    except subprocess.CalledProcessError:
        rec["skipped"] = "no merge-base (disjoint histories)"
        return rec
    for path in sorted(_changed_framework_files(repo, merge_sha, p1, p2,
                                                base, prefixes)):
        l_m = _blob_lines(repo, merge_sha, path)
        l_p1 = _blob_lines(repo, p1, path)
        l_p2 = _blob_lines(repo, p2, path)
        l_b = _blob_lines(repo, base, path)
        dropped = []
        for ln in (l_p1 | l_p2) - l_m:
            if ln in l_p1 and ln in l_p2:
                cls = "both-parents-drop"
            elif ln in l_p1 and ln not in l_b:
                cls = "p1-addition-dropped"
            elif ln in l_p2 and ln not in l_b:
                cls = "p2-addition-dropped"
            else:
                continue  # a branch deliberately deleted it — healthy merge
            tokens = _token_hits(ln)
            if tokens:
                # Store the FULL line — truncation is a display concern
                # (main() clips at 100 chars). A truncated record forces every
                # downstream adjudicator back to re-deriving blobs from git:
                # the first HEAD-survival adjudication of this detector's own
                # output failed exactly that way (long lines could never
                # exact-match HEAD through a [:240] clip).
                dropped.append({"line": ln, "class": cls,
                                "tokens": tokens[:5]})
        # --- DOUBLE (gap-061): the resolution kept BOTH sides -------------
        # Set algebra above is structurally blind to this: duplicating a line
        # changes no set, only a count. Predicate is deliberately strict —
        # the merge holds MORE copies than ANY input did, so no input
        # justifies the multiplicity and there is no "a branch meant it"
        # reading. Encounter 2 () is exactly this shape: one
        # conflict hunk in LlmAPIService.java kept both sides, tests green.
        c_m = _blob_line_counts(repo, merge_sha, path)
        c_p1 = _blob_line_counts(repo, p1, path)
        c_p2 = _blob_line_counts(repo, p2, path)
        c_b = _blob_line_counts(repo, base, path)
        duplicated = []
        for ln, n_m in c_m.items():
            ceiling = max(c_p1[ln], c_p2[ln], c_b[ln])
            if n_m <= ceiling:
                continue
            tokens = _token_hits(ln)
            if tokens:
                duplicated.append({
                    "line": ln, "class": "resolution-duplicated",
                    "count_merge": n_m, "count_p1": c_p1[ln],
                    "count_p2": c_p2[ln], "count_base": c_b[ln],
                    "tokens": tokens[:5],
                })
        if dropped or duplicated:
            rec["flags"].append({"file": path, "dropped": dropped,
                                 "duplicated": duplicated})
    return rec


def scan(repo: Path, rev_args: list[str],
         prefixes: tuple[str, ...] = FRAMEWORK_PREFIXES) -> dict:
    shas = [s for s in _git(repo, "log", "--merges", "--format=%H",
                            *rev_args).splitlines() if s.strip()]
    results = [inspect_merge(repo, s, prefixes) for s in shas]
    flagged = [r for r in results if r["flags"]]
    skipped = [r for r in results if r["skipped"]]
    return {
        "merges_scanned": len(shas),
        "merges_flagged": len(flagged),
        "merges_skipped": len(skipped),
        "flagged": flagged,
        "skipped": [{"sha": r["sha"], "reason": r["skipped"]} for r in skipped],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=".", help="repo root (default cwd)")
    ap.add_argument("--since", help="git --since bound (e.g. 2026-07-14)")
    ap.add_argument("--until", help="git --until bound")
    ap.add_argument("--range", dest="rev_range",
                    help="explicit rev range A..B (overrides since/until)")
    ap.add_argument("--paths", default=None,
                    help="comma-separated pathspec prefixes to scan "
                         "instead of the framework default; empty string "
                         "( --paths '' ) scans the WHOLE tree. gap-061 "
                         "encounters 1 and 4 landed in product TypeScript "
                         "and a GitHub Actions YAML, both outside "
                         "FRAMEWORK_PREFIXES and therefore unreachable "
                         "by the default scope.")
    ap.add_argument("--json", action="store_true", help="machine output")
    ap.add_argument("--exit-on-hits", action="store_true",
                    help="exit 2 when any merge is flagged")
    args = ap.parse_args()

    rev_args: list[str] = []
    if args.rev_range:
        rev_args.append(args.rev_range)
    else:
        if args.since:
            rev_args.append(f"--since={args.since}")
        if args.until:
            rev_args.append(f"--until={args.until}")
    if not rev_args:
        ap.error("give --range or --since (unbounded scans are never wanted)")

    if args.paths is None:
        prefixes = FRAMEWORK_PREFIXES
    else:
        prefixes = tuple(x.strip() for x in args.paths.split(',') if x.strip())
    report = scan(Path(args.repo).resolve(), rev_args, prefixes)
    if args.json:
        print(json.dumps(report, indent=1))
    else:
        print(f"[evil-merge] scanned {report['merges_scanned']} merges — "
              f"flagged {report['merges_flagged']}, "
              f"skipped {report['merges_skipped']}")
        for r in report["flagged"]:
            print(f"  MERGE {r['sha'][:12]}")
            for f in r["flags"]:
                for d in f.get("dropped", []):
                    print(f"    {f['file']} [{d['class']}] "
                          f"tokens={','.join(d['tokens'])}: {d['line'][:100]}")
                for d in f.get("duplicated", []):
                    print(f"    {f['file']} [{d['class']}] "
                          f"x{d['count_merge']} (p1={d['count_p1']} "
                          f"p2={d['count_p2']} base={d['count_base']}) "
                          f"tokens={','.join(d['tokens'])}: {d['line'][:100]}")
    if args.exit_on_hits and report["merges_flagged"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
