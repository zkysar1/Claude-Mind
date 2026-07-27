#!/usr/bin/env python3
"""Audit SKILL.md `Bash:` call sites against each wrapper's real flag surface.

WHY THIS EXISTS (g-115-3112, root cause in rb-5106)
---------------------------------------------------
SKILL.md pseudocode is LLM-EXECUTED but never VALIDATED. A `Bash:` line naming a
`core/scripts` wrapper is never syntax-checked, never imported, and never covered
by pytest. A wrapper can add, rename, or drop a flag and every SKILL.md call site
keeps its old spelling with ZERO signal.

Script-side tests do NOT protect this seam: `pending-questions-read.sh` had a green
10-test suite the entire time its SKILL.md call site passed a `--prefix` the parser
never accepted, because the tests call the script correctly and the SKILL.md did not.

The failure is silent in BOTH directions — the wrapper prints its diagnostic to
STDERR and exits non-zero, while the consumer reads only STDOUT, and empty stdout is
indistinguishable from a legitimate "nothing matched" (guard-487: suppression gates
must fail CLOSED).

DESIGN: CONSERVATIVE BY CONSTRUCTION
------------------------------------
The goal explicitly warns that prose and pseudocode placeholders will generate false
positives, and that findings must be triaged rather than auto-fixed. So every
ambiguity here resolves toward SILENCE, and every skip is COUNTED and reportable via
`--show-skips`. An audit that cries wolf gets ignored, which would leave the class
exactly as unprotected as it is today. A missed mismatch costs one more instance; a
false positive costs the check's credibility.

Specifically, a call site is skipped (not flagged) when:
  - the line names more than one wrapper (flag ownership is ambiguous)
  - the line contains a shell pipe/redirect/substitution before the flag
  - the wrapper's flag surface could not be parsed at all (unknown != wrong)
  - the wrapper forwards "$@" to something this script cannot resolve
  - the flag appears inside backticks/quotes as prose rather than as an argument

FLAG SURFACE EXTRACTION
-----------------------
  .sh  — `case` arms: `--flag)`, `--flag|-f)`, `--flag=*)`, and `-f|--flag)`
  .py  — argparse `add_argument("--flag", ...)` via AST (not regex: a docstring
         mentioning add_argument must not register a flag)
  Union — when a .sh execs/delegates to a sibling .py, the accepted set is the
         union of both surfaces (the wrapper may parse some flags and forward
         the rest).

Exit codes: 0 = no findings (or --output json), 1 = findings present (for CI/gates).
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import os
import sys
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PROJECT_ROOT = _HERE.parent.parent
SKILLS_DIR = PROJECT_ROOT / ".claude" / "skills"
SCRIPTS_DIR = PROJECT_ROOT / "core" / "scripts"

# A wrapper that takes free-form or forwarded args — flags cannot be validated.
# Keep this list SHORT and justified; every entry is a hole in the audit.
UNVALIDATABLE = {
    # forwards arbitrary remaining args to an inner command
    "aws-exec.sh",
    "efs-ssh.sh",
}

# Flags accepted by essentially every wrapper via common plumbing.
UNIVERSAL_FLAGS = {"--help", "-h", "--json", "--output", "--source", "--agent"}

_CASE_ARM = re.compile(r"^\s*\(?\s*((?:-{1,2}[A-Za-z0-9][A-Za-z0-9_-]*\s*\|\s*)*-{1,2}[A-Za-z0-9][A-Za-z0-9_-]*)(?:=\*)?\s*\)")
_FLAG_TOKEN = re.compile(r"(?<![\w-])(--[A-Za-z][A-Za-z0-9-]*)")
_WRAPPER_REF = re.compile(r"(?:core/scripts/)?([a-z0-9][a-z0-9_-]*\.(?:sh|py))")
# `[ "$1" = "--flag" ]` / `[[ "${1:-}" != --flag ]]` — the non-getopts way a
# wrapper declares it accepts a flag. heartbeat-tick.sh uses only this form.
_SH_COMPARE = re.compile(r"[=!]=?\s*[\"']?(--[A-Za-z][A-Za-z0-9-]*)")


def sh_flags(path: Path) -> set[str] | None:
    """Flags a bash wrapper accepts. None if unparseable.

    TWO declaration forms, both real in this codebase — parsing only the first
    produces false positives:
      1. `case` arms:            `--flag)` / `--flag|-f)` / `--flag=*)`
      2. string comparison:      `if [ "${1:-}" != "--flag" ]` / `[[ "$1" == --flag ]]`
    `heartbeat-tick.sh` uses form 2 exclusively for `--bypass-state`, so a
    case-arm-only parser reported both of its legitimate call sites as unknown-flag
    mismatches. Comment lines are excluded so a `# ... --flag ...` note does not
    register a flag the parser never accepts.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    found: set[str] = set()
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        m = _CASE_ARM.match(line)
        if m:
            for alt in m.group(1).split("|"):
                alt = alt.strip()
                if alt.startswith("-"):
                    found.add(alt)
            continue
        # form 2 — only inside a test/conditional, so a flag being PASSED to an
        # inner command on an ordinary line is not mistaken for one being ACCEPTED.
        if re.search(r"\b(if|elif|while)\b|\[\[|\[ ", line):
            for m2 in _SH_COMPARE.finditer(line):
                found.add(m2.group(1))
    return found


def py_flags(path: Path) -> set[str] | None:
    """Flags an argparse script accepts. AST-based so docstrings don't register."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return None
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "add_argument"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                    and arg.value.startswith("-"):
                found.add(arg.value)
    return found


_INVOKES = re.compile(
    r"(?:^|[;&|(]|\bexec\s|\bbash\s|\bsh\s|\bpython3?\s|\bpy\s+-3\s)"
    r"[^#\n]*?([a-z0-9][a-z0-9_-]*\.(?:py|sh))")


def delegate_targets(path: Path) -> set[str]:
    """Sibling script basenames a wrapper actually EXECS (not merely mentions).

    Two things this must get right, both found by dogfooding the audit on itself:

    1. NOT a bare mention. `utilization-gate.sh` names `tree.py` only in a comment;
       a whole-file basename regex unioned tree.py's entire flag surface into
       utilization-gate's, which both hides real mismatches (over-permissive) and,
       when the mentioned file is unparseable, produces spurious skips. So: skip
       comment lines and require an invocation keyword.

    2. `.sh` delegates COUNT, not just `.py`. `agent-aspirations-read.sh` is one
       line — `exec .../aspirations-read.sh --source agent "$@"` — so following
       only `.py` left its surface empty and silently skipped every call site
       against it. That was 31 of the skips on the first clean run.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    out: set[str] = set()
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        for m in _INVOKES.finditer(line):
            if m.group(1) != path.name:          # never self-recurse
                out.add(m.group(1))
    return out


# Lines that DOCUMENT a flag being wrong/absent rather than USING it. This file's
# largest false-positive source is verify-learning/SKILL.md, whose whole job is to
# name bad flags ("real flag is `--since`", "neither flag exists"). Matching the
# negation is far more robust than excluding the file wholesale, which would also
# hide that file's genuinely-stale Checks. Suppressions are COUNTED and reported so
# this stays auditable rather than becoming a silent filter.
_NEGATION = re.compile(
    r"no such|real flag is|neither flag exists|does not exist|doesn't exist|"
    r"no longer|is wrong|silently broken|actual API is|there is no |"
    r"\bno --|not a flag|never accepted|rejects|invalid flag|"
    # "must NOT pass a --x flag" / "(without --x)" both state the flag is NOT
    # being passed. Deliberately NOT including "unless --x": that form ASSERTS
    # the flag exists as an override, and one such line (verify-learning:2701,
    # `unless --force`) was a true finding — suppressing it would have hidden a
    # real stale check.
    r"must not pass|without --|"
    r"grep -rn|grep -q|if \[ -z", re.I)


def wrapper_surface(name: str, cache: dict, _seen: frozenset = frozenset()) -> set[str] | None:
    """Union flag surface for a wrapper basename. None = unparseable (never flag).

    Follows delegation transitively: a wrapper may parse some flags itself and
    forward the rest, and the chain can be .sh -> .sh -> .py (e.g.
    agent-aspirations-read.sh -> aspirations-read.sh). Union every hop, else each
    forwarded flag false-positives. `_seen` breaks cycles.
    """
    if name in cache:
        return cache[name]
    if name in _seen:                      # cycle — contribute nothing, don't fail
        return set()
    path = SCRIPTS_DIR / name
    if not path.exists():
        cache[name] = None
        return None

    if name.endswith(".py"):
        surface = py_flags(path)
    else:
        surface = sh_flags(path)
        if surface is not None:
            for tgt in delegate_targets(path):
                if not (SCRIPTS_DIR / tgt).exists():
                    continue
                sub = wrapper_surface(tgt, cache, _seen | {name})
                if sub is None:
                    # Unparseable delegate => unknown surface, not a wrong one.
                    cache[name] = None
                    return None
                surface |= sub
    cache[name] = surface
    return surface


def audit_line(line: str, cache: dict):
    """Return (finding|None, skip_reason|None) for one SKILL.md line."""
    refs = [m.group(1) for m in _WRAPPER_REF.finditer(line)]
    refs = [r for r in refs if (SCRIPTS_DIR / r).exists()]
    if not refs:
        return None, None
    if len(set(refs)) > 1:
        return None, "multiple-wrappers-on-line"
    name = refs[0]
    if name in UNVALIDATABLE:
        return None, "unvalidatable-wrapper"
    if _NEGATION.search(line):
        return None, "documents-flag-absence"

    # Only consider the segment AFTER the wrapper name — flags before it belong to
    # something else (`echo --x | tool.sh`), and a pipe after it hands the rest away.
    tail = line.split(name, 1)[1]
    for sep in ("|", ">", "<", "&&", ";", "$("):
        if sep in tail:
            tail = tail.split(sep, 1)[0]

    # Two ways a flag on this line belongs to a DIFFERENT command. Both are
    # narrow on purpose: over-suppressing hides a real defect forever, so only
    # constructs that name another command explicitly are cut. In particular
    # `(unless --force)` / `(without --x)` are NOT cut — those are assertions
    # about THIS wrapper, and two of them were true findings.
    #   1. a slash-command later on the line owns its own flags
    #      ("calls `pipeline-read.sh --unreflected` then invokes `/review-hypotheses --learn`")
    m_skill = re.search(r"(?<![\w/])/[a-z][a-z0-9-]+", tail)
    if m_skill:
        tail = tail[: m_skill.start()]
    #   2. a spaced-off parenthetical is an annotation, not argv
    #      ("wm-read.sh encoding_queue --json  (if --selective mode)")
    m_note = re.search(r"\s{2,}\(", tail)
    if m_note:
        tail = tail[: m_note.start()]

    passed = {m.group(1) for m in _FLAG_TOKEN.finditer(tail)}
    passed -= UNIVERSAL_FLAGS
    if not passed:
        return None, None

    accepted = wrapper_surface(name, cache)
    if accepted is None:
        return None, "unparseable-flag-surface"
    if not accepted:
        return None, "empty-flag-surface"

    unknown = sorted(f for f in passed if f not in accepted)
    if not unknown:
        return None, None
    return {"wrapper": name, "unknown_flags": unknown,
            "accepted_sample": sorted(accepted)[:12]}, None


RATCHET_KEY = "skillmd_flag_mismatches"


def _ratchet(result: dict, output: str) -> int:
    """Advisory baseline ratchet, mirroring eviction-conservation-ratchet.py.

    Baselines rather than hard-gates because the drift PRE-DATES the check
    (11 real mismatches at seed time, each needing its own verified fix), which
    is exactly the case audit-baselines.md says to baseline. The tripwire the
    goal wanted still fires: any NEW mismatch reads REGRESSED.

    Imports are local so the plain audit path stays dependency-free (it must run
    in a bare checkout and under tests without _paths/_fileops resolving).
    """
    sys.path.insert(0, str(SCRIPTS_DIR))
    from _paths import META_DIR  # type: ignore
    from _fileops import locked_modify_yaml  # type: ignore

    current = result["finding_count"]
    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    captured: dict = {}

    def _modify(baselines):
        # Locked RMW — read the baseline INSIDE the lock; sibling ratchets share
        # this file ( sibling-stomp pattern, audit-baselines.md).
        if not isinstance(baselines, dict):
            baselines = {}
        entry = baselines.get(RATCHET_KEY) or {}
        prior = entry.get("baseline")
        history = entry.get("history") or []

        if prior is None:
            verdict, new_baseline = "seeded", current
            message = (f"Seeded baseline at {current} SKILL.md flag mismatch(es). "
                       f"Any increase is a NEW call site passing a flag its "
                       f"wrapper does not accept.")
        elif current > prior:
            verdict, new_baseline = "regressed", prior  # never raise the baseline
            message = (f"WARN: mismatches grew from baseline {prior} to {current} "
                       f"(+{current - prior}). A SKILL.md now invokes a wrapper "
                       f"with a flag it does not accept. Inspect with "
                       f"`py -3 core/scripts/skillmd-flag-audit.py`.")
        elif current < prior:
            verdict, new_baseline = "ratcheted", current
            message = (f"OK: mismatches shrank from {prior} to {current} "
                       f"(-{prior - current}); baseline lowered.")
        else:
            verdict, new_baseline = "stable", prior
            message = f"OK: mismatches stable at baseline {current}."

        history.append({"recorded_at": now_iso, "count": current,
                        "verdict": verdict})
        baselines[RATCHET_KEY] = {
            "baseline": new_baseline,
            "last_recorded": now_iso,
            "last_verdict": verdict,
            "note": ("Count of SKILL.md `Bash:` call sites passing a flag the "
                     "invoked core/scripts wrapper's own parser does not accept. "
                     "Producer: skillmd-flag-audit.py --ratchet (g-115-3112). "
                     "Seeded at 11 real + 1 known prose FP; drive to 0 via "
                     "per-site verified fixes, then the baseline ratchets down."),
            "history": history[-50:],
        }
        captured.update(verdict=verdict, new_baseline=new_baseline,
                        message=message)
        return baselines

    try:
        locked_modify_yaml(META_DIR / "audit-baselines.yaml", _modify, initial={})
    except Exception as e:  # never block the loop on a baseline-write failure
        print(f"WARN: could not persist baseline: {e}", file=sys.stderr)
        captured.setdefault("verdict", "error")
        captured.setdefault("new_baseline", None)
        captured.setdefault("message", f"baseline operation failed: {e}")

    if output == "json":
        print(json.dumps({**result, "verdict": captured["verdict"],
                          "baseline": captured["new_baseline"],
                          "message": captured["message"]}, indent=2))
    else:
        print(f"[skillmd-flag-ratchet] {captured['verdict'].upper()}: "
              f"{captured['message']}")

    if os.environ.get("VERIFY_LEARNING_DRIFT_HARD_GATE") == "1":
        return 1 if captured["verdict"] == "regressed" else 0
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--output", choices=["text", "json"], default="text")
    ap.add_argument("--show-skips", action="store_true",
                    help="Report skipped call sites (honest coverage accounting).")
    ap.add_argument("--skills-dir", default=None, help="Override (tests).")
    ap.add_argument("--ratchet", action="store_true",
                    help="Compare finding count against meta/audit-baselines.yaml "
                         "and report seeded/stable/ratcheted/regressed. Advisory "
                         "(exit 0) unless VERIFY_LEARNING_DRIFT_HARD_GATE=1.")
    args = ap.parse_args()

    skills = Path(args.skills_dir) if args.skills_dir else SKILLS_DIR
    cache: dict = {}
    findings, skips = [], {}
    scanned = 0

    for md in sorted(skills.glob("*/SKILL.md")):
        try:
            lines = md.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for n, line in enumerate(lines, 1):
            if "core/scripts/" not in line and ".sh" not in line and ".py" not in line:
                continue
            scanned += 1
            finding, skip = audit_line(line, cache)
            if skip:
                skips[skip] = skips.get(skip, 0) + 1
            if finding:
                finding.update({
                    "skill": md.parent.name,
                    # relative_to() raises for a --skills-dir outside the repo
                    # (the test-override case) — fall back to the absolute path.
                    "file": str(md.relative_to(PROJECT_ROOT))
                            if md.is_relative_to(PROJECT_ROOT) else str(md),
                    "line": n,
                    "text": line.strip()[:160],
                })
                findings.append(finding)

    result = {"scanned_lines": scanned, "finding_count": len(findings),
              "findings": findings, "skipped": skips,
              "wrappers_resolved": sum(1 for v in cache.values() if v)}

    if args.ratchet:
        return _ratchet(result, args.output)

    if args.output == "json":
        print(json.dumps(result, indent=2))
        return 0

    print(f"skillmd-flag-audit: scanned {scanned} candidate lines across "
          f"{len(list(skills.glob('*/SKILL.md')))} SKILL.md files; "
          f"{len(findings)} finding(s); {result['wrappers_resolved']} wrapper "
          f"surfaces resolved")
    for f in findings:
        print(f"  {f['file']}:{f['line']} [{f['wrapper']}] "
              f"unknown: {', '.join(f['unknown_flags'])}")
        print(f"      {f['text']}")
        print(f"      accepts: {', '.join(f['accepted_sample'])}")
    if args.show_skips and skips:
        print("  skipped (conservative — unknown is not wrong):")
        for k, v in sorted(skips.items(), key=lambda kv: -kv[1]):
            print(f"      {k}: {v}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
