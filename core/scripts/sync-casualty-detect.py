#!/usr/bin/env python3
"""sync-casualty-detect.py — receiving-side detector for framework-sync casualties.

WHY THIS EXISTS (g-029-96, from the g-029-94 incident)
------------------------------------------------------
Framework syncs into this repo are authored on ANOTHER machine and land as a
single large commit. `.claude/rules/promotion-cycle.md` plus guard-117 and
guard-119 all mandate a preflight audit before overwriting living-prod
framework, and all three were in force on 2026-07-27 when a sync silently
reverted two target-ahead files (a `.gitignore` durability stanza and a
`session-manifest.yaml` registry entry). They could not prevent it: they govern
the AGENT's promotion behaviour, and nothing agent-side gates a human's push
from elsewhere. The loss was found 3 days later, by accident.

This tool is the safety NET, not a gate. It cannot stop the sync; it detects
casualties immediately so they are restored deliberately rather than
rediscovered. `promotion-preflight.py` remains the right tool at the ORIGIN,
before the overwrite — it takes two repo paths and compares content. This one
is single-repo and post-hoc, driven entirely by git history.

THE FUNNEL, measured on 7fc89e8e (the 2026-07-27 sync)
------------------------------------------------------
    721 files changed
    670 framework files
      4 target-ahead (a local commit in the lookback window before the sync)
      2 genuine casualties
Filtering to target-ahead FIRST is what makes this cheap. Reporting all 670
differing files would be unusable, which is why a content-diff is not the
entry point.

FUNCTIONAL SURFACE, NOT ADDED LINES (rb-809)
--------------------------------------------
The obvious test — "are the lines the local commit ADDED still present at
HEAD?" — false-alarms badly. On the same 4 files it reported 13/13, 8/10 and
2/3 lines "removed" and yielded a verdict of 4 casualties. Two of those were
comment and docstring churn around code that survived untouched: env.py's key
regexes are still the case-insensitive form the fix installed, and
test_owncloud_baseline_stamp.py kept all 5 of its test functions.

Comments are the highest-churn lines in any file AND the place rationale
lives, so a well-documented fix looks MOST reverted. The bias therefore runs
toward false CLOBBERED — the dangerous direction for a tool whose output is
"restore these files", since restoring a file that was never broken can itself
revert whatever legitimately superseded it.

So each artifact type is compared on the unit that actually carries behaviour:
    test_*.py        set of test-function names
    *.py             def/class names + compiled-regex literals + CONSTANTS
    *.yaml / *.yml   top-level keys + `file:`-style list-entry identity keys
    *.md             section headers
    everything else  line-set membership (for .gitignore a line IS the
                     functional unit — a pattern is behaviour)

KNOWN LIMITATION — HEAD-comparison conflates CLOBBERED with SUPERSEDED
---------------------------------------------------------------------
The default compares against HEAD, which answers "is this content missing
NOW?" — NOT "did THIS sync remove it". When several promotions have landed
since, those differ. Auditing the 2026-06-25 sync against HEAD reports 3
casualties, but the files have since passed through FIVE more incoming
promotions (v2.2.0, v2.3.1, v2.4.0, v2.5.0, 2026-07-27), and the lost units
read like deliberate progress: runner-claim.sh lost its
`OWNERSHIP_MODE != dynamic` no-op guard, which is exactly what an own-cloud
cutover would REMOVE on purpose.

So: use the DEFAULT (HEAD) to ask "is anything missing that we should look
at?", and use `--at <sync-sha>` to ATTRIBUTE a loss to a specific sync. Only
the backtest form supports the claim "this sync destroyed this". Reporting a
HEAD-diff as a per-sync casualty list is the same over-attribution the
functional-surface rule above exists to prevent, moved from lines to time.

READ-ONLY and FAIL-OPEN. Every probe is a `git show` / `git log`. Any failure
degrades that file to `unknown` rather than aborting: a detector that dies on
one odd file is worse than one that reports what it could determine.

USAGE
    py -3 core/scripts/sync-casualty-detect.py                  # newest sync commit
    py -3 core/scripts/sync-casualty-detect.py --commit <sha>
    py -3 core/scripts/sync-casualty-detect.py --all --json
    py -3 core/scripts/sync-casualty-detect.py --lookback-days 7 --selftest

EXIT CODES
    0  no casualties found (or nothing to audit)
    1  at least one CASUALTY / PARTIAL — act on it
    2  bad usage / no git
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent

# Sync-shaped commit subjects — the commits we AUDIT. Deliberately narrow: broad
# matching would sweep in ordinary "chore:" commits and every one of their files
# becomes a candidate.
SYNC_SUBJECT_RE = re.compile(r"sync\s+framework", re.I)

# INCOMING-shaped subjects — commits that are NOT local work and must never be
# treated as target-ahead. Strictly WIDER than SYNC_SUBJECT_RE.
#
# WHY THIS IS SEPARATE, and it is the whole correctness of the tool: the first
# version reused SYNC_SUBJECT_RE for both roles, so `e41baa19`
# ("chore(promote): transplant framework seed v2.4.0 from Claude-Mind", 172
# files) counted as LOCAL WORK. The next promotion (v2.5.0) then looked like it
# had clobbered 74 target-ahead files, and the tool reported 9 casualties. All 9
# were version progression: v2.5.0 legitimately superseding v2.4.0. Verified on
# core/scripts/aspirations-precheck-budget-meter.sh, whose only commits since
# v2.4.0 are the two syncs themselves — zero local work, so nothing could have
# been clobbered.
#
# That is rb-809's false-CLOBBERED bias reproduced INSIDE the tool built to
# avoid it, which is why the funnel must be validated against a sync with a
# known answer (--at) and not trusted from a plausible-looking count.
INCOMING_SUBJECT_RE = re.compile(
    r"sync\s+framework"
    r"|transplant\s+framework"
    r"|framework\s+seed"
    r"|chore\(promote\)"
    r"|promote.*from\s+\S*-?Mind",
    re.I,
)

# Reuse promotion-preflight's classification rather than re-deriving it — it is
# the single source of truth for what counts as framework. Loaded via importlib
# because the filename contains a hyphen.
#
# ONE ADDITION, and it is load-bearing: `.gitignore`. It is NOT in preflight's
# FRAMEWORK_PATHS, so preflight would NOT have flagged the .gitignore casualty
# even if it had been run at the origin — 1 of the 2 confirmed casualties sits
# outside the tool built to catch them. Tracked as a preflight coverage gap.
EXTRA_FRAMEWORK_PATHS = [".gitignore"]


def _load_preflight():
    p = SCRIPT_DIR / "promotion-preflight.py"
    if not p.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("_preflight", p)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    except Exception:
        return None


_PF = _load_preflight()
_FALLBACK_FRAMEWORK = ["CLAUDE.md", "core/config", "core/scripts",
                       ".claude/skills", ".claude/rules", ".claude/settings.json"]
FRAMEWORK_PATHS = list(getattr(_PF, "FRAMEWORK_PATHS", _FALLBACK_FRAMEWORK)) + EXTRA_FRAMEWORK_PATHS
EXCLUDE_DIRS = set(getattr(_PF, "EXCLUDE_DIRS", {"__pycache__", ".git", ".pytest_cache"}))
EXCLUDE_SUBSTR = list(getattr(_PF, "EXCLUDE_SUBSTR", ["_tmp_"]))
# Files preflight classifies as legitimately per-deployment ("each repo's own copy
# is correct; never counted as blocking drift"). A sync overwriting THESE is the
# system working, not a casualty — reported separately so the signal stays clean.
DEPLOYMENT_LOCAL = set(getattr(_PF, "DEPLOYMENT_LOCAL", {
    "CLAUDE.md", ".claude/settings.json", ".claude/settings.local.json",
    ".claude/rules/promotion-cycle.md"}))


def _git(*args, cwd=None):
    """Read-only git. Returns stdout ('' on any failure) — never raises."""
    try:
        r = subprocess.run(["git", *args], cwd=str(cwd or PROJECT_ROOT),
                           capture_output=True, text=True, timeout=60)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def is_framework(rel: str) -> bool:
    rel = rel.replace("\\", "/")
    segs = rel.split("/")
    if any(s in EXCLUDE_DIRS for s in segs):
        return False
    if any(sub in rel for sub in EXCLUDE_SUBSTR):
        return False
    return any(rel == fp or rel.startswith(fp.rstrip("/") + "/") for fp in FRAMEWORK_PATHS)


# --- functional-surface extractors (rb-809) ---------------------------------

_TEST_FN = re.compile(r"^\s*def (test_\w+)", re.M)
_PY_DEF = re.compile(r"^\s*(?:def|class) (\w+)", re.M)
_PY_CONST = re.compile(r"^([A-Z][A-Z0-9_]{2,})\s*=", re.M)
_PY_REGEX = re.compile(r"re\.compile\(\s*r?['\"](.+?)['\"]", re.S)
_YAML_TOPKEY = re.compile(r"^([A-Za-z_][\w-]*):", re.M)
_YAML_ENTRY = re.compile(r"^\s*-\s+(?:file|id|name|key):\s*(\S+)", re.M)
_MD_HEADER = re.compile(r"^(#{1,6}\s+.+)$", re.M)


def functional_surface(rel: str, text: str) -> tuple[str, set]:
    """(unit_name, surface_set) for the artifact type. Never raises."""
    if not text:
        return ("absent", set())
    base = rel.rsplit("/", 1)[-1]
    try:
        if base.startswith("test_") and base.endswith(".py"):
            return ("test-function names", set(_TEST_FN.findall(text)))
        if rel.endswith(".py"):
            return ("defs+constants+regexes",
                    set(_PY_DEF.findall(text)) | set(_PY_CONST.findall(text))
                    | set(_PY_REGEX.findall(text)))
        if rel.endswith((".yaml", ".yml")):
            return ("yaml keys+entry ids",
                    set(_YAML_TOPKEY.findall(text)) | set(_YAML_ENTRY.findall(text)))
        if rel.endswith(".md"):
            return ("section headers", {h.strip() for h in _MD_HEADER.findall(text)})
        # .gitignore and friends: a line IS the functional unit.
        return ("significant lines",
                {l.strip() for l in text.splitlines()
                 if l.strip() and not l.strip().startswith("#")})
    except Exception:
        return ("unknown", set())


def audit_file(rel: str, local_sha: str, sync_sha: str, at: str = "HEAD") -> dict:
    """Did the sync actually remove functional surface the local commit added?

    `at` is the state to compare AGAINST — HEAD normally, but a historical ref
    lets a known sync be replayed (backtest). Comparing against HEAD after a
    casualty has been restored correctly reports INTACT, which validates the
    restore but proves nothing about detection; `--at <sync>` proves detection.
    """
    at_local = _git("show", f"{local_sha}:{rel}")
    at_head = _git("show", f"{at}:{rel}")
    # _git() returns '' on ANY failure, so an empty at_before is ambiguous: the file
    # may be NEW (correct: everything counts as added) or git may simply have failed
    # (wrong: everything counts as added anyway, inflating the added baseline and
    # biasing toward false CASUALTY — the rb-809 direction). Distinguish them by
    # asking git whether the path EXISTED in the parent tree, which answers the
    # question directly instead of inferring it from empty output.
    parent = _git("rev-parse", f"{local_sha}^").strip()
    baseline_known = True
    if parent:
        listed = _git("ls-tree", "-r", "--name-only", parent, "--", rel).strip()
        at_before = _git("show", f"{parent}:{rel}") if listed else ""
        if listed and not at_before:
            baseline_known = False        # existed but unreadable -> do not guess
    else:
        at_before = ""
        baseline_known = False            # no parent (root commit) -> unknown baseline

    unit, surf_local = functional_surface(rel, at_local)
    _, surf_before = functional_surface(rel, at_before)
    _, surf_head = functional_surface(rel, at_head)

    added = surf_local - surf_before          # what the local commit introduced
    lost = added - surf_head                  # of that, what is gone at HEAD

    if not baseline_known and parent:
        verdict = "UNKNOWN-BASELINE"      # git could not read the pre-change state
    elif not at_head:
        verdict = "FILE-GONE"
    elif unit in ("unknown", "absent"):
        verdict = "UNKNOWN"
    elif not added:
        verdict = "NO-FUNCTIONAL-CHANGE"      # local commit was comments only
    elif not lost:
        verdict = "INTACT"
    elif lost == added:
        verdict = "CASUALTY"
    else:
        verdict = "PARTIAL"

    return {
        "file": rel,
        "local_commit": local_sha,
        "compared_on": unit,
        "added_units": len(added),
        "lost_units": len(lost),
        "verdict": verdict,
        "lost_sample": sorted(str(x)[:100] for x in list(lost)[:5]),
    }


def find_sync_commits(limit: int = 20) -> list[dict]:
    out = _git("log", "--all", f"-n{limit * 40}", "--format=%H%x1f%cI%x1f%an%x1f%s")
    rows = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 4:
            continue
        sha, when, who, subj = parts
        if SYNC_SUBJECT_RE.search(subj) and not subj.lower().startswith(("revert", "fix(")):
            rows.append({"sha": sha, "date": when, "author": who, "subject": subj})
        if len(rows) >= limit:
            break
    return rows


def audit_sync(sync: dict, lookback_days: int, at: str = "HEAD") -> dict:
    sha, when = sync["sha"], sync["date"]
    touched = [f for f in _git("show", "--name-only", "--format=", sha).split() if f]
    fw = sorted({f for f in touched if is_framework(f)})

    # Compute the window bound in Python. git's date parser does NOT understand
    # "N days before <date>" — it silently yields an EMPTY log rather than an
    # error, so the whole candidate set came back 0 while the manual audit found
    # 4. Caught only by replaying against a sync with a known answer; the unit
    # selftest passed the entire time. An absolute ISO date parses fine.
    from datetime import datetime, timedelta, timezone
    try:
        base = datetime.fromisoformat(when.replace("Z", "+00:00"))
    except Exception:
        base = datetime.now(timezone.utc)
    # No fallback: strftime always returns a git-parseable string here (verified —
    # git accepts both the +0000 and the offset-less form), so an `or` branch would
    # be dead code.
    since_abs = (base - timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%S%z")

    candidates = []
    for f in fw:
        log = _git("log", f"--since={since_abs}",
                   f"--until={when}", "--format=%H%x1f%s", f"{sha}^", "--", f)
        for line in log.splitlines():
            p = line.split("\x1f")
            if len(p) != 2:
                continue
            lsha, lsubj = p
            if INCOMING_SUBJECT_RE.search(lsubj):
                continue     # another INCOMING promotion/transplant, not local work
            candidates.append((f, lsha))
            break                             # newest local commit per file
    results = []
    for f, lsha in candidates:
        r = audit_file(f, lsha, sha, at)
        if f in DEPLOYMENT_LOCAL and r["verdict"] in ("CASUALTY", "PARTIAL", "FILE-GONE"):
            r["verdict"] = "DEPLOYMENT-LOCAL"   # expected divergence, not a loss
        results.append(r)
    bad = [r for r in results if r["verdict"] in ("CASUALTY", "PARTIAL", "FILE-GONE")]
    return {
        "sync": sync,
        "files_changed": len(touched),
        "framework_files": len(fw),
        "compared_against": at,
        "target_ahead_candidates": len(candidates),
        "casualties": len(bad),
        "results": results,
    }


def selftest() -> int:
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        ok = ok and bool(cond)

    check("is_framework(core/scripts/x.py)", is_framework("core/scripts/x.py"))
    check("is_framework(.gitignore) — the preflight coverage gap",
          is_framework(".gitignore"))
    check("is_framework(.mind-data/world/x.md) is False",
          not is_framework(".mind-data/world/x.md"))
    check("is_framework(core/scripts/__pycache__/x.py) is False",
          not is_framework("core/scripts/__pycache__/x.py"))

    # rb-809: comment churn must NOT read as a casualty.
    before = "X_RE = re.compile(r'[A-Z]+')\ndef f():\n    pass\n"
    local = "# a long explanatory comment\nX_RE = re.compile(r'[A-Za-z]+')\ndef f():\n    pass\n"
    head = "# totally reworded comment block\nX_RE = re.compile(r'[A-Za-z]+')\ndef f():\n    pass\n"
    u, s_local = functional_surface("core/scripts/a.py", local)
    _, s_before = functional_surface("core/scripts/a.py", before)
    _, s_head = functional_surface("core/scripts/a.py", head)
    added, lost = s_local - s_before, (s_local - s_before) - s_head
    check("comment churn around a surviving regex -> nothing lost",
          added and not lost)

    # positive control: the regex actually reverted
    reverted = "# comment\nX_RE = re.compile(r'[A-Z]+')\ndef f():\n    pass\n"
    _, s_rev = functional_surface("core/scripts/a.py", reverted)
    check("genuinely reverted regex -> detected as lost",
          (s_local - s_before) - s_rev == (s_local - s_before))

    # test files compare on test-function names
    u2, s2 = functional_surface("core/scripts/tests/test_x.py",
                                '"""doc"""\ndef test_a(): pass\ndef test_b(): pass\n')
    check("test file compares on test-function names",
          u2 == "test-function names" and s2 == {"test_a", "test_b"})
    # docstring-only churn on a test file
    _, s3 = functional_surface("core/scripts/tests/test_x.py",
                               '"""COMPLETELY different doc"""\ndef test_a(): pass\ndef test_b(): pass\n')
    check("docstring churn on a test file -> identical surface", s2 == s3)

    # yaml entry identity
    u4, s4 = functional_surface("core/config/m.yaml",
                                "files:\n  - file: a.md\n    purpose: x\n  - file: b.md\n")
    check("yaml compares on keys + entry ids",
          "a.md" in s4 and "b.md" in s4 and "files" in s4)

    # gitignore: a pattern line IS functional
    u5, s5 = functional_surface(".gitignore", "# c\nagents/*/temp/*.log\n")
    check("gitignore compares on significant lines",
          s5 == {"agents/*/temp/*.log"})

    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--commit", help="audit this sync commit (default: newest)")
    ap.add_argument("--all", action="store_true", help="audit every sync commit found")
    ap.add_argument("--lookback-days", type=int, default=7,
                    help="how far back a local commit counts as target-ahead (default 7)")
    ap.add_argument("--at", default="HEAD",
                    help="ref to compare against (default HEAD). Use the sync sha itself to "
                         "BACKTEST what it destroyed at the time, after casualties were restored.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return selftest()
    if not _git("rev-parse", "--git-dir"):
        print("sync-casualty-detect: not a git repo", file=sys.stderr)
        return 2

    if a.commit:
        subj = _git("show", "-s", "--format=%s", a.commit).strip()
        when = _git("show", "-s", "--format=%cI", a.commit).strip()
        who = _git("show", "-s", "--format=%an", a.commit).strip()
        if not when:
            print(f"sync-casualty-detect: unknown commit {a.commit}", file=sys.stderr)
            return 2
        syncs = [{"sha": a.commit, "date": when, "author": who, "subject": subj}]
    else:
        syncs = find_sync_commits()
        if not a.all:
            syncs = syncs[:1]

    reports = [audit_sync(s, a.lookback_days, a.at) for s in syncs]
    total = sum(r["casualties"] for r in reports)

    if a.json:
        print(json.dumps({"reports": reports, "total_casualties": total}, indent=1))
    else:
        if not reports:
            print("sync-casualty-detect: no sync-shaped commits found — nothing to audit")
        for r in reports:
            s = r["sync"]
            print(f"\n=== {s['sha'][:8]}  {s['date'][:10]}  {s['author']}  {s['subject'][:56]}")
            print(f"    compared against {r['compared_against']}")
            print(f"    {r['files_changed']} changed -> {r['framework_files']} framework "
                  f"-> {r['target_ahead_candidates']} target-ahead -> {r['casualties']} casualties")
            for x in r["results"]:
                mark = "  !!" if x["verdict"] in ("CASUALTY", "PARTIAL", "FILE-GONE") else "    "
                print(f"{mark} {x['verdict']:<20} {x['file']}")
                print(f"       compared on {x['compared_on']}: "
                      f"{x['lost_units']}/{x['added_units']} units lost  (local {x['local_commit'][:8]})")
                for smp in x["lost_sample"]:
                    print(f"         LOST: {smp}")
        print(f"\nTOTAL CASUALTIES: {total}")
        if total:
            print("Restore via `git show <local_commit>:<file>`, then re-verify the "
                  "KEEP-IN-SYNC contracts the file names before committing.")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
