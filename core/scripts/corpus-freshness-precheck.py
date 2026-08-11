#!/usr/bin/env python3
"""Refuse a repo-wide negative until the corpus behind it is proven current.

WHY (gap-056, g-115-4459). Before asserting "nothing calls X" / "no test covers
Y" / "the symbol does not exist", the claim rests on a grep, and the grep rests
on a checkout. If the checkout is stale the negative is unfalsifiable-by-
construction: the evidence is missing because the code is missing, and the
result looks exactly like a genuine absence.

Both registered encounters are the same agent, which is what makes this a
mechanization problem rather than a discipline problem:
  g-335-607 (2026-07-31) published a PROVEN NEGATIVE from a grep over an
    18-commit-stale repo, and retracted it after a partner cited line numbers
    that did not exist locally.
  g-335-651 (2026-08-01) ran the same check FIRST: the repo was 10 behind, one
    missing commit WAS the one that created the surface under evaluation, and
    2 of 5 stated premises had moved.
The difference between the two runs was SEQUENCE, not skill. Encoded rules
already existed (guard-1745, guard-1792, rb-3365, rb-5396) and all four are
honor-system -- guard-1745's times_active was 0 at the moment it would have
caught the first one.

WHY THIS REUSES product-repo-freshness.py RATHER THAN RE-IMPLEMENTING IT.
That script already owns the git half correctly: enumeration through the
`_path_roots.compute_allowed_roots()` SSOT (so an AGENT_WRITE_PATH edit reaches
here with no second list), repo detection, fetch-only freshness, and two
separate loud CANNOT-CHECK branches. Re-deriving any of that would create the
duplicate-list failure its own docstring argues against for gap-018. What it
does NOT do -- and deliberately should not, since it sits on the Phase 3.9 hot
path -- is bind to a CLAIM instead of a goal-id, ask whether the missing
commits touch the claimed surface, or run the grep at all. That is this file.

THE TWO CONSTRAINTS, both learned in encounter 2 and both load-bearing:

  1. REPORT THE MATCHES, NEVER THE MATCH COUNT. The re-check found 3 files
     matching a jsdom grep where the premise said 0, which reads as a flat
     refutation. Reading the matches showed two were COMMENTS asserting the
     absence ("this repo has no jsdom") and the third a lockfile with no such
     entry -- so the premise was RIGHT and a count-only probe would have
     inverted a correct finding. A count cannot be adjudicated; a match can.
     Hence every match is printed with file:line:text and the verdict on a
     non-empty match set is REVIEW, never REFUTED.

  2. A SHARED CHECKOUT MAY HAVE LIVE PARTNERS IN IT. The refresh step must not
     assume it may checkout, pull, reset or stash. This never writes to a
     worktree: it fetches, then greps the REMOTE ref directly via
     `git grep <pattern> <upstream>`, which reads fresh content out of the
     object store while leaving the working tree untouched. See g-115-3689 and
     g-115-4292 for the same hazard on the PR side.

EXIT CODES are the point of the tool, so they are not advisory-flat:
    0  SAFE TO ASSERT -- corpus verified current AND zero matches on fresh ref
    1  DO NOT ASSERT  -- stale corpus, or matches found that need reading
    2  CANNOT CHECK   -- nothing was examined; NOT an all-clear
Silence is never the all-clear signal here; the verdict line always prints.

Usage:
    corpus-freshness-precheck.py --pattern <regex> [--repo <path>]...
                                 [--path <pathspec>]... [--json] [--no-fetch]
                                 [--max-matches N]
"""

import argparse
import importlib.util
import json
import sys
from pathlib import Path

MAX_MATCHES_DEFAULT = 40
_SIBLING = "product-repo-freshness.py"


def _load_sibling():
    """Import product-repo-freshness.py by path (its name is not importable).

    Returns the module, or None when it cannot be loaded. A failure here is a
    CANNOT CHECK, never a silent degrade to a hand-rolled fallback -- a second
    copy of the enumeration is the exact drift this reuse exists to avoid.
    """
    p = Path(__file__).resolve().parent / _SIBLING
    try:
        spec = importlib.util.spec_from_file_location("_prf", p)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def head_date(prf, repo):
    """Committer date of HEAD. gap-056 asks for this alongside the behind count:
    'behind=0' on a repo whose HEAD is months old means the REMOTE is idle, which
    is a different situation from a fresh remote you are level with."""
    rc, out, _ = prf._git(repo, "log", "-1", "--format=%cI", "HEAD")
    return out if rc == 0 and out else None


def missing_commits_touching(prf, repo, upstream, pathspecs):
    """The commits you are missing that TOUCH the claimed surface.

    This is the question that separates 'my checkout is behind' from 'my
    checkout is behind IN A WAY THAT INVALIDATES THIS CLAIM'. In encounter 2 the
    repo was 10 behind and exactly one of those commits created the surface
    under evaluation -- a bare behind-count would not have said so.

    Empty pathspecs means the whole tree, which is the honest default: if the
    caller cannot name the surface, every missing commit is potentially relevant.
    """
    args = ["log", "--oneline", "--no-decorate", "%s..%s" % ("HEAD", upstream)]
    if pathspecs:
        args += ["--", *pathspecs]
    rc, out, err = prf._git(repo, *args, timeout=20)
    if rc != 0:
        return None, err or "rc=%d" % rc
    return ([ln for ln in out.splitlines() if ln.strip()], None)


def grep_fresh_ref(prf, repo, ref, pattern, pathspecs, max_matches):
    """Grep the FRESH ref, not the working tree. Never mutates the checkout.

    `git grep <pattern> <ref>` reads blobs straight out of the object store, so
    a partner's in-flight edits in the shared worktree are neither disturbed nor
    counted. Returns (matches, truncated, error).
    """
    args = ["grep", "-n", "-I", "-E", pattern, ref]
    if pathspecs:
        args += ["--", *pathspecs]
    rc, out, err = prf._git(repo, *args, timeout=30)
    # git grep: rc=0 matches found, rc=1 no matches, rc>1 real error.
    if rc > 1:
        return None, False, (err or "rc=%d" % rc)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    truncated = len(lines) > max_matches
    return lines[:max_matches], truncated, None


def check_repo(prf, repo, pattern, pathspecs, do_fetch, max_matches):
    """One repo -> one record. Never raises.

    The fetch is run HERE rather than delegated to `freshness(do_fetch=...)` so
    its success is a first-class fact (`fetch_verified`) instead of something a
    caller has to infer by string-matching the `detail` prose. That distinction
    is load-bearing: without it, BOTH `--no-fetch` and a FAILED fetch produce a
    remote-tracking ref that is not current, `freshness()` continues to the
    count anyway (correctly — a stale count beats no count), and the grep then
    reads a stale ref while the verdict says CLEAN.

    Measured on the fresh-eyes review of this file's own first version: with
    `--no-fetch` against a clone whose remote had advanced, the tool printed
    "0 matches on a VERIFIED-CURRENT ref", "VERDICT: SAFE TO ASSERT", and
    exit 0 — for a symbol that demonstrably existed on the remote. That is the
    exact false-negative this tool exists to prevent, reproduced inside the
    tool, and the `note:` line about no-fetch did not stop it because the
    verdict and the exit code both said go.
    """
    fetch_verified = False
    fetch_note = ""
    if do_fetch:
        rc, _, ferr = prf._git(repo, "fetch", "--quiet", timeout=prf.FETCH_TIMEOUT_S)
        fetch_verified = (rc == 0)
        if not fetch_verified:
            fetch_note = ("fetch FAILED (%s) — the remote ref is NOT verified "
                          "current, so a clean result cannot be trusted"
                          % (ferr or "rc=%d" % rc))
    else:
        fetch_note = ("--no-fetch: the remote ref is NOT verified current, so a "
                      "clean result cannot be trusted")

    # do_fetch=False: the fetch (or the deliberate skip) is already handled above.
    rec = prf.freshness(repo, do_fetch=False)
    if fetch_verified and str(rec.get("detail") or "").startswith("no-fetch:"):
        # freshness() stamps its own no-fetch note whenever it is told not to
        # fetch. We DID fetch, just one level up, so that note would be false.
        rec["detail"] = ""
    rec["fetch_verified"] = fetch_verified
    if fetch_note:
        rec["detail"] = (rec["detail"] + " | " if rec.get("detail") else "") + fetch_note
    rec["head_date"] = head_date(prf, repo)
    rec["missing_commits_touching_surface"] = None
    rec["missing_commits_error"] = None
    rec["matches"] = None
    rec["matches_truncated"] = False
    rec["grep_error"] = None
    rec["grep_ref"] = None

    upstream = rec.get("upstream")
    if upstream and rec.get("behind"):
        commits, err = missing_commits_touching(prf, repo, upstream, pathspecs)
        rec["missing_commits_touching_surface"] = commits
        rec["missing_commits_error"] = err

    # Grep the freshest ref available. With no upstream there is no fresher
    # content than HEAD -- say which ref was used rather than implying freshness.
    ref = upstream or "HEAD"
    rec["grep_ref"] = ref
    matches, truncated, gerr = grep_fresh_ref(
        prf, repo, ref, pattern, pathspecs, max_matches)
    rec["matches"] = matches
    rec["matches_truncated"] = truncated
    rec["grep_error"] = gerr
    return rec


def verdict_for(rec):
    """Per-repo verdict. Ordered so the worst honest answer wins.

    CANNOT-CHECK outranks everything: an unreadable repo must never contribute
    a reassuring answer. STALE outranks REVIEW because a match set read off a
    stale ref is not evidence either way.
    """
    if rec.get("grep_error") or rec.get("verdict") == "unknown":
        return "CANNOT-CHECK"
    if rec.get("verdict") in ("behind", "diverged"):
        return "STALE"
    if rec.get("verdict") == "no-upstream":
        # Local-only or detached: legitimately not stale, but nothing proves it
        # current either. Never fold this into a clean answer.
        return "CANNOT-CHECK"
    if rec.get("matches"):
        # A match on a not-yet-verified ref is still a real match — the symbol
        # existed as of that ref — so REVIEW stays the more informative answer
        # than CANNOT-CHECK, and both block the assertion anyway.
        return "REVIEW"
    if not rec.get("fetch_verified", False):
        # ZERO matches on a ref nobody verified is current is the dangerous
        # shape, not a clean one: absence of evidence produced by a stale
        # corpus is exactly the false negative this tool exists to refuse.
        return "CANNOT-CHECK"
    return "CLEAN"


def render(records, pattern):
    out = ["[corpus-freshness-precheck] pattern=%r across %d repo(s)"
           % (pattern, len(records))]
    for r in records:
        v = verdict_for(r)
        head = r.get("head_date") or "unknown-date"
        out.append("")
        out.append("  %-12s %s (branch=%s, HEAD %s)"
                   % (v, r.get("name"), r.get("branch") or "?", head))
        if r.get("detail"):
            out.append("      note: %s" % r["detail"])
        if v == "STALE":
            out.append("      BEHIND by %s commit(s) — this grep reads the PAST. "
                       "Do not assert a negative from it."
                       % (r.get("behind"),))
            mc = r.get("missing_commits_touching_surface")
            if mc:
                out.append("      %d missing commit(s) TOUCH the claimed surface:" % len(mc))
                for ln in mc[:10]:
                    out.append("        %s" % ln)
                if len(mc) > 10:
                    out.append("        ... and %d more" % (len(mc) - 10))
            elif mc == []:
                out.append("      none of the missing commits touch the claimed "
                           "surface — the staleness may not bear on this claim, "
                           "but confirm the pathspec actually names it")
        if r.get("grep_error"):
            out.append("      grep FAILED on %s: %s — nothing was examined here"
                       % (r.get("grep_ref"), r["grep_error"]))
        matches = r.get("matches")
        if matches:
            out.append("      %d match(es) on %s — READ THEM, do not count them. "
                       "A match can be a comment asserting the very absence you "
                       "are claiming (gap-056 encounter 2: 2 of 3 hits were)."
                       % (len(matches), r.get("grep_ref")))
            for ln in matches:
                out.append("        %s" % ln)
            if r.get("matches_truncated"):
                out.append("        ... truncated; re-run with --max-matches to see all")
        elif matches == [] and v == "CLEAN":
            out.append("      0 matches on a VERIFIED-CURRENT ref — the negative "
                       "is supported for this repo")
    return "\n".join(out)


def overall(records):
    """(exit_code, label). No records at all is CANNOT CHECK, never success."""
    if not records:
        return 2, "CANNOT CHECK"
    vs = [verdict_for(r) for r in records]
    if any(v == "CANNOT-CHECK" for v in vs):
        return 2, "CANNOT CHECK"
    if any(v == "STALE" for v in vs):
        return 1, "DO NOT ASSERT (stale corpus)"
    if any(v == "REVIEW" for v in vs):
        return 1, "DO NOT ASSERT (matches need reading)"
    return 0, "SAFE TO ASSERT"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pattern", required=True,
                    help="extended regex the negative claims matches nothing")
    ap.add_argument("--repo", action="append", default=[],
                    help="repo path; repeatable. Default: enumerate AGENT_WRITE_PATH")
    ap.add_argument("--path", action="append", default=[],
                    help="pathspec limiting the claimed surface; repeatable")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--max-matches", type=int, default=MAX_MATCHES_DEFAULT)
    args = ap.parse_args(argv)

    prf = _load_sibling()
    if prf is None:
        print("[corpus-freshness-precheck] CANNOT CHECK: could not load %s — "
              "the freshness/enumeration mechanism is unavailable. This is NOT "
              "an all-clear; nothing was examined." % _SIBLING, file=sys.stderr)
        return 2

    if args.repo:
        repos = [Path(r) for r in args.repo if prf._is_repo(r)]
        rejected = [r for r in args.repo if not prf._is_repo(r)]
        for r in rejected:
            print("[corpus-freshness-precheck] not a git repo, skipped: %s" % r,
                  file=sys.stderr)
    else:
        repos = prf.enumerate_repos()

    if not repos:
        print("[corpus-freshness-precheck] CANNOT CHECK: 0 repos to examine "
              "(agent=%r). This is NOT an all-clear — a negative asserted now "
              "rests on nothing. Pass --repo <path> explicitly, or confirm "
              "MIND_AGENT is set and its local-paths.conf names "
              "AGENT_WRITE_PATH." % (__import__("os").environ.get("MIND_AGENT") or "",),
              file=sys.stderr)
        return 2

    records = [check_repo(prf, r, args.pattern, args.path,
                          not args.no_fetch, args.max_matches)
               for r in repos]
    code, label = overall(records)

    if args.json:
        print(json.dumps({"pattern": args.pattern,
                          "pathspecs": args.path,
                          "verdict": label,
                          "exit_code": code,
                          "records": records}, indent=2))
    else:
        print(render(records, args.pattern))
        print("")
        print("[corpus-freshness-precheck] VERDICT: %s" % label)
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        # Even the crash path must not read as an all-clear: this tool's whole
        # purpose is to withhold confidence, so it fails to CANNOT CHECK (2).
        print("[corpus-freshness-precheck] CANNOT CHECK: probe crashed: %s: %s"
              % (type(exc).__name__, exc), file=sys.stderr)
        sys.exit(2)
