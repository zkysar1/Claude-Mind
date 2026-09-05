#!/usr/bin/env python3
"""symbol_birth_archaeology.py — BORN-UNUSED vs LOST-CALLER-AT-<sha>.

gap-147. Given a symbol that has ZERO callers NOW, decide which of two OPPOSITE
fixes applies:

  BORN-UNUSED        -> the symbol never had a caller. It is residue; delete it.
  LOST-CALLER-AT-sha -> it HAD callers and they went away. The caller's job is
                        now undone -- a BEHAVIOUR BUG. Deleting the symbol
                        BURIES that bug instead of fixing it.

Scope boundary (deliberate, do not widen): this tool does NOT decide whether the
symbol has zero callers today. `/call-shape-census` (gap-048) already does that
with an unfiltered positive control and fatal-on-rc>=2, so re-implementing it
here would be a second, weaker copy. Run that FIRST; this tool starts from its
verdict. guard-1957 covers the prior fork (exists-and-unused vs does-not-exist)
and stops there; this is the fork after it.

WHY STEP 3 EXISTS (the whole point of the tool). `git log -S` lists only commits
where the OCCURRENCE COUNT CHANGED. It can NEVER report what the count STARTED
at, so the introducing commit alone cannot distinguish "born with a caller" from
"born alone". Only a count taken AT that commit separates them.

WHY STEP 4 EXISTS. A zero from step 3 is ambiguous in exactly the way the
present-tense zero was: it can mean "no callers then" or "the probe did not work
at that revision" (path renamed, submodule, grep pathspec fatal). Without a
SAME-COMMIT positive control the historical zero is the identical ambiguity
transposed onto an old revision. So a zero with no passing control is refused,
never reported as BORN-UNUSED.

Controls follow guard-2930 (positive control on a live sibling AND negative
control on a nonexistent name) and guard-2943 (count against
`git show origin/<branch>:<path>`, never a dirty working tree).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

NONEXISTENT = "zzz_symbol_that_cannot_exist_gap147_negctl"


class GitFatal(RuntimeError):
    """git exited >=2: a FATAL, never an empty result (guard-1926 class)."""


def _git(repo: str, *args: str, allow_1: bool = True) -> str:
    """Run git. NEVER redirects stderr -- a failed search must not read as zero."""
    p = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True,
    )
    if p.returncode >= 2:
        raise GitFatal(
            f"git {' '.join(args)} -> rc={p.returncode}: {p.stderr.strip()[:300]}"
        )
    if p.returncode == 1 and not allow_1:
        raise GitFatal(f"git {' '.join(args)} -> rc=1 unexpectedly")
    return p.stdout


def count_at(repo: str, rev: str, symbol: str) -> int:
    """Repo-wide occurrence count of `symbol` at a revision. rc=1 == 0 hits."""
    out = _git(repo, "grep", "-c", "-F", "--", symbol, rev)
    total = 0
    for line in out.splitlines():
        # format: <rev>:<path>:<count>
        m = re.search(r":(\d+)$", line.strip())
        if m:
            total += int(m.group(1))
    return total


def changing_commits(repo: str, symbol: str) -> list[dict]:
    out = _git(repo, "log", "-S", symbol, "--all", "--format=%H\t%ad\t%s",
               "--date=short")
    rows = []
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            rows.append({"sha": parts[0], "date": parts[1], "subject": parts[2]})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", required=True, help="the zero-caller symbol")
    ap.add_argument("--path", required=True, help="file that DEFINES the symbol")
    ap.add_argument("--sibling", required=True,
                    help="a symbol in --path known live BOTH now and at the "
                         "introducing commit; it is the positive control and "
                         "there is no default because a wrong one silently "
                         "converts a refusal into a false BORN-UNUSED")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    r: dict = {"symbol": a.symbol, "path": a.path, "sibling": a.sibling,
               "branch": a.branch, "verdict": None, "controls": {}, "steps": {}}

    # REFUSE a DECLARATION-SHAPED symbol. `def foo` / `class Foo` / `func foo`
    # occurs exactly ONCE by construction -- at the definition -- because callers
    # write `foo(...)`. So the count at birth is ALWAYS 1 and the verdict is
    # ALWAYS a false BORN-UNUSED, which is the delete-a-live-symbol direction.
    # Measured while forging this skill: `def resolve_binding` returned
    # BORN-UNUSED against a symbol CLAUDE.md documents as the live resolver.
    # Pass the BARE identifier; the tool counts declaration + calls together and
    # the <=1 test is what separates "definition only" from "definition + caller".
    if re.match(r"^\s*(def|class|func|fn|function|sub|proc)\s", a.symbol):
        r["verdict"] = "REFUSED"
        r["reason"] = (f"symbol {a.symbol!r} is DECLARATION-SHAPED. Such a string "
                       f"occurs once by construction, so this tool would always "
                       f"answer BORN-UNUSED regardless of the truth. Pass the "
                       f"bare identifier instead (e.g. "
                       f"{a.symbol.split()[-1]!r}).")
        return _emit(r, a)

    try:
        head_rev = f"origin/{a.branch}"
        # -- step 1: present-tense counts at origin/<branch>, NOT the work tree.
        r["steps"]["1_head_count"] = count_at(a.repo, head_rev, a.symbol)
        r["controls"]["head_positive_sibling"] = count_at(a.repo, head_rev, a.sibling)
        r["controls"]["head_negative_nonexistent"] = count_at(a.repo, head_rev, NONEXISTENT)

        if r["controls"]["head_positive_sibling"] == 0:
            r["verdict"] = "REFUSED"
            r["reason"] = (f"positive control failed at {head_rev}: sibling "
                           f"{a.sibling!r} counts 0, so a zero for the target "
                           f"proves nothing about the target.")
            return _emit(r, a)
        if r["controls"]["head_negative_nonexistent"] != 0:
            r["verdict"] = "REFUSED"
            r["reason"] = ("negative control failed: a nonexistent name counts "
                           ">0, so the counter over-reports.")
            return _emit(r, a)

        # -- step 2: commits where the COUNT CHANGED (never what it started at).
        rows = changing_commits(a.repo, a.symbol)
        r["steps"]["2_changing_commits"] = rows
        if not rows:
            r["verdict"] = "REFUSED"
            r["reason"] = ("git log -S found no count-changing commit. The "
                           "symbol may be spelled differently, live only in an "
                           "unreferenced branch, or predate this history.")
            return _emit(r, a)

        birth = rows[-1]          # git log is newest-first; oldest == introduction
        r["steps"]["birth"] = birth

        # -- step 3: THE SEPARATING PROBE -- the count AT the introducing commit.
        born_count = count_at(a.repo, birth["sha"], a.symbol)
        r["steps"]["3_count_at_birth"] = born_count

        # -- step 4: SAME-COMMIT positive control; without it a zero is ambiguous.
        sib_at_birth = count_at(a.repo, birth["sha"], a.sibling)
        r["controls"]["birth_positive_sibling"] = sib_at_birth
        if sib_at_birth == 0:
            r["verdict"] = "REFUSED"
            r["reason"] = (f"same-commit positive control failed at "
                           f"{birth['sha'][:12]}: sibling {a.sibling!r} counts 0 "
                           f"there, so the probe did not work at that revision "
                           f"and the historical zero is the same ambiguity "
                           f"transposed onto an old commit -- not evidence.")
            return _emit(r, a)

        # A symbol alone in the world occurs once: its own definition.
        if born_count <= 1:
            r["verdict"] = "BORN-UNUSED"
            r["reason"] = (f"at its introducing commit {birth['sha'][:12]} "
                           f"({birth['date']}) the repo-wide count was "
                           f"{born_count} -- the definition and nothing else. "
                           f"It never had a caller; the residue is safe to delete.")
        else:
            r["verdict"] = f"LOST-CALLER-AT-{birth['sha'][:12]}"
            r["reason"] = (f"at its introducing commit {birth['sha'][:12]} "
                           f"({birth['date']}) the repo-wide count was "
                           f"{born_count} (>1), so it HAD callers. They were "
                           f"removed later. The caller's job is now undone: "
                           f"treat as a BEHAVIOUR BUG, not as dead code. "
                           f"Deleting the symbol buries it.")
    except GitFatal as e:
        r["verdict"] = "REFUSED"
        r["reason"] = f"git fatal (never swallowed, never read as zero): {e}"

    return _emit(r, a)


def _emit(r: dict, a) -> int:
    if a.json:
        print(json.dumps(r, indent=1))
    else:
        print(f"VERDICT: {r['verdict']}")
        print(f"  {r.get('reason','')}")
        print(f"  controls: {json.dumps(r['controls'])}")
        b = r["steps"].get("birth")
        if b:
            print(f"  birth: {b['sha'][:12]} {b['date']} {b['subject'][:70]}")
            print(f"  count at birth: {r['steps'].get('3_count_at_birth')}")
        print(f"  count-changing commits: {len(r['steps'].get('2_changing_commits',[]))}")
    return 0 if str(r["verdict"]).startswith(("BORN-UNUSED", "LOST-CALLER")) else 2


if __name__ == "__main__":
    sys.exit(main())
