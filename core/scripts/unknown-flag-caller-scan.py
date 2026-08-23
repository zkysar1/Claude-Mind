#!/usr/bin/env python3
"""unknown-flag-caller-scan — who would a wrapper's unknown-flag refusal BREAK?

g-115-5438. 22 wrappers still append every unrecognised flag to a write-only
PASSTHROUGH array nothing reads, so a caller's typo or stale flag silently
vanishes and the query answers a BROADER question than it was asked. Adopting
`argv_strict_refuse_unknown` fixes that — but the refusal turns a silent
over-broad ANSWER into a hard rc=2, so any live caller passing a now-refused
flag STARTS FAILING the moment it lands.

That is why the goal's procedure is ordered and this tool exists: enumerate the
callers FIRST, fix them, THEN add the refusal. g-115-5214 hit exactly this — its
single caller passed TWO unrecognised flags, and adding the refusal first would
have silently disarmed a never-clobber guard, because that caller reads an empty
result as "safe to write".

CONTINUATION-AWARE, and that is not a detail. A line-based grep sees
    bash core/scripts/aspirations-query.sh --source "$s" \\
        --goal-field id "$g" --json --full
as two lines and finds only `--source`. The goal's own description records that
this is how the SECOND offending flag was nearly missed. Physical lines are
joined on a trailing backslash before anything is matched.

WHAT IT CANNOT SEE, stated because a scanner that reports "clean" on what it
could not read is worse than no scanner (guard-1760):

  * flags supplied through a VARIABLE (`$FLAGS`, `"${ARGS[@]}"`) — reported as
    UNRESOLVED, never as clean. An UNRESOLVED call site needs a human read.
  * invocations assembled at runtime (eval, printf-into-a-string, python
    subprocess lists built from parts).
  * callers outside the scanned roots. `world/` is an external gitignored path;
    pass it explicitly with --root if you want it covered, and say in your
    report whether you did.

It also does NOT decide whether a refusal is SAFE — only who it breaks. A
caller that suppresses stderr and treats an empty result as permission to act
turns a refusal into a silent over-permissive ACTION, which is worse than the
over-broad answer being fixed. Those are flagged HAZARD, and they are the ones
to read by hand.

Usage:
    python3 core/scripts/unknown-flag-caller-scan.py                 # all write-only wrappers
    python3 core/scripts/unknown-flag-caller-scan.py --wrapper board-read.sh
    python3 core/scripts/unknown-flag-caller-scan.py --json
    python3 core/scripts/unknown-flag-caller-scan.py --root /abs/path/to/world
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = PROJECT_ROOT / "core" / "scripts"

DEFAULT_ROOTS = (
    PROJECT_ROOT / "core" / "scripts",
    PROJECT_ROOT / "core" / "config",
    PROJECT_ROOT / ".claude" / "skills",
    PROJECT_ROOT / ".claude" / "rules",
    PROJECT_ROOT / "mind_api" / "src",
)
SCAN_SUFFIXES = (".sh", ".py", ".md", ".yaml", ".yml")

# A flag reaching one of these is consumed as a VALUE and never sees the case
# statement, so a dash-prefixed token there is swallowed regardless of any
# refusal. Not this tool's job to fix (that is the goal's VALUE-POSITION
# RESIDUAL, decided once in the shared helper) but it must not be miscounted as
# an unknown flag either.
_VALUE_TAKING = re.compile(r'\$\{?2[-:}]')

_CONT = re.compile(r"\\\s*\n\s*")
_ARM = re.compile(r"^\s*(-[^)]*?)\)", re.M)
_STOP = {"|", ";", "&&", "||", ">", ">>", "2>", "&", ")", "}", "fi", "done", "then"}


def _join_continuations(text: str) -> str:
    return _CONT.sub(" ", text)


def accepted_flags(wrapper: Path) -> tuple[set[str], bool]:
    """(flags the wrapper's case arms accept, whether it has a catch-all `*)`).

    Parsed from the wrapper's OWN source rather than a hand-kept list — a
    maintained list is a second source of truth that drifts the first time an
    arm is added, which is the same class of defect this whole lane is about.
    """
    src = wrapper.read_text(encoding="utf-8", errors="replace")
    flags: set[str] = set()
    for m in _ARM.finditer(src):
        for alt in m.group(1).split("|"):
            alt = alt.strip().strip('"').strip("'")
            if alt.startswith("-"):
                flags.add(alt)
    return flags, bool(re.search(r"^\s*\*\)", src, re.M))


def write_only_passthrough_wrappers() -> list[Path]:
    """Wrappers that APPEND to PASSTHROUGH, never READ it, and have no refusal.

    The read check is what separates this population from the forwarding
    wrappers (tree-read.sh, aspirations-add.sh), which genuinely consume the
    array — a naive appends>0 filter counts those too and overstates the
    population by two.
    """
    out = []
    for f in sorted(SCRIPTS.glob("*.sh")):
        s = f.read_text(encoding="utf-8", errors="replace")
        if "PASSTHROUGH" not in s or "argv_strict_refuse_unknown" in s:
            continue
        if not re.search(r"PASSTHROUGH\+=", s):
            continue
        reads = re.findall(r'\$\{PASSTHROUGH\[', s) + re.findall(r'"\$\{PASSTHROUGH', s)
        if reads:
            continue
        out.append(f)
    return out


def _tokens_after(text: str, idx: int) -> list[str]:
    """Tokens of the invocation starting at idx, stopping at a shell break."""
    tail = text[idx:]
    nl = tail.find("\n")
    if nl != -1:
        tail = tail[:nl]
    toks = []
    for t in tail.split():
        if t in _STOP or any(t.startswith(s) for s in ("|", ";", "&&", "||", ">", "2>")):
            break
        toks.append(t)
    return toks


# An occurrence is a CALL SITE only if something invokes it. Without this, every
# backtick-quoted flag name in a convention or SKILL.md reads as an argument at a
# call site: the first run of this scanner reported 53 "blocking callers" for
# aspirations-read.sh, of which the overwhelming majority were prose like
# "`aspirations-read.sh --active`" in verification-checklist.md. Documented flags
# are real signal for a DIFFERENT question (stale docs); they are not callers,
# and counting them would have sent this goal's sweep to fix documentation.
#
# `Bash:` IS AN INVOCATION PREFIX (, measured 2026-08-21). The prose
# filter above is right, but it drew the line one form too wide: SKILL.md
# pseudocode `Bash: <wrapper> --flag` is EXECUTED — by an LLM reading the skill —
# so it is a caller in every sense that matters, and excluding it made the
# scanner blind to the majority of them. Measured over the live tree with
# .history excluded: 113 `Bash:`-form sites vs 57 seen, i.e. the scanner saw
# one third of its own population (aspirations-read.sh 27 blind / 9 seen,
# pipeline-read.sh 39/7, experience-read.sh 21/5, aspirations-meta-update.sh
# 12/0, aspirations-complete.sh 6/0 — two wrappers were 100% invisible).
# This is not theoretical: the aspirations-read.sh adoption shipped with
# `blocking=0` from this scanner while `.claude/skills/aspirations-consolidate/
# SKILL.md` documented `--json`, a flag the wrapper never had. Silently dropped
# before the refusal; rc=2 after it. A live regression, invisible to the tool
# whose whole job is to prevent exactly that.
#
# Deliberately NARROW. Bare-backtick prose stays excluded (that is the 53-FP
# case), and `_CLEAN_FLAG` below still strips markdown punctuation. This adds
# one prefix, not a category.
_INVOKED_BY = re.compile(r'(?:^|[\s;&|(`$]|\$\()\s*(?:[Bb]ash:|bash|sh|py|python3?|source|\.)\s+'
                         r'(?:-3\s+)?["\']?[^\s"\';|&]*$')
# A real flag token, after markdown/prose punctuation is stripped. Prose forms
# ("--active`," "--meta})." "--active-compact`)") never survive this.
_CLEAN_FLAG = re.compile(r'^--?[A-Za-z0-9][A-Za-z0-9-]*$')


def _is_invocation(joined: str, start: int) -> bool:
    """True when the wrapper name at `start` is being RUN, not merely named."""
    lo = joined.rfind("\n", 0, start) + 1
    return bool(_INVOKED_BY.search(joined[lo:start]))


def scan_callers(wrapper_name: str, accepted: set[str], roots) -> list[dict]:
    hits: list[dict] = []
    pat = re.compile(re.escape(wrapper_name))
    for root in roots:
        if not root.exists():
            continue
        for f in root.rglob("*"):
            if not f.is_file() or f.suffix not in SCAN_SUFFIXES:
                continue
            if f.name == wrapper_name:
                continue           # the wrapper is not its own caller
            try:
                raw = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if wrapper_name not in raw:
                continue
            joined = _join_continuations(raw)
            for m in pat.finditer(joined):
                if not _is_invocation(joined, m.start()):
                    continue
                toks = _tokens_after(joined, m.end())
                unknown, unresolved = [], []
                for t in toks:
                    if t.startswith("$") or t.startswith('"$') or "${" in t:
                        unresolved.append(t)
                        continue
                    if not t.startswith("-"):
                        continue
                    base = t.split("=", 1)[0].strip('`",\'.):;')
                    if not _CLEAN_FLAG.match(base):
                        continue          # prose punctuation, not an argument
                    if base not in accepted:
                        unknown.append(base)
                if unknown or unresolved:
                    line = joined[:m.start()].count("\n") + 1
                    hits.append({
                        "file": str(f.relative_to(PROJECT_ROOT))
                                if str(f).startswith(str(PROJECT_ROOT)) else str(f),
                        "line_in_joined": line,
                        "unknown_flags": sorted(set(unknown)),
                        "unresolved_tokens": sorted(set(unresolved)),
                        "stderr_suppressed": "2>/dev/null" in joined[m.start():m.start() + 400],
                    })
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wrapper", help="one wrapper basename; default = all write-only ones")
    ap.add_argument("--root", action="append", default=[],
                    help="extra root to scan (repeatable; use for external world/)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    roots = list(DEFAULT_ROOTS) + [Path(r) for r in args.root]
    wrappers = ([SCRIPTS / args.wrapper] if args.wrapper
                else write_only_passthrough_wrappers())

    report = []
    for w in wrappers:
        if not w.exists():
            print(f"no such wrapper: {w}", file=sys.stderr)
            return 2
        acc, catchall = accepted_flags(w)
        hits = scan_callers(w.name, acc, roots)
        report.append({
            "wrapper": w.name,
            "accepted_flags": sorted(acc),
            "has_catch_all_arm": catchall,
            "caller_hits": hits,
            "blocking_callers": sum(1 for h in hits if h["unknown_flags"]),
            "unresolved_callers": sum(1 for h in hits if h["unresolved_tokens"]
                                      and not h["unknown_flags"]),
            "hazard_callers": sum(1 for h in hits
                                  if h["unknown_flags"] and h["stderr_suppressed"]),
        })

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    ready = [r for r in report if not r["caller_hits"]]
    blocked = [r for r in report if r["caller_hits"]]
    print(f"scanned {len(report)} wrapper(s) over {len(roots)} root(s)\n")
    print("READY — no caller passes an unaccepted flag and none is unresolved:")
    for r in sorted(ready, key=lambda x: x["wrapper"]):
        print(f"  {r['wrapper']}")
    print("\nNEEDS CALLER WORK FIRST (fix these before adding the refusal):")
    for r in sorted(blocked, key=lambda x: -x["blocking_callers"]):
        print(f"  {r['wrapper']:<34} blocking={r['blocking_callers']} "
              f"unresolved={r['unresolved_callers']} hazard={r['hazard_callers']}")
        for h in r["caller_hits"]:
            if h["unknown_flags"]:
                mark = "  HAZARD (stderr suppressed)" if h["stderr_suppressed"] else ""
                print(f"      {h['file']}:~{h['line_in_joined']} "
                      f"{h['unknown_flags']}{mark}")
    print("\nUNRESOLVED means a variable-supplied argument this scanner CANNOT read.")
    print("It is not a clean result — read those call sites by hand before adopting.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
