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

# Flags suppressed on EVERY call site.
#
# THE ORIGINAL JUSTIFICATION — "accepted by essentially every wrapper via common
# plumbing" — IS MEASURABLY FALSE. Measured 2026-08-10 (bravo, hostname cc-05,
# uname -r 6.8.0-136-generic) over the 467 parseable wrappers actually referenced
# by a SKILL.md `Bash:` call site, which is the only population this suppression
# affects:
#     --source   50/467  10.7%      --agent    62/467  13.3%
#     --output   71/467  15.2%      --json    114/467  24.4%
# Not one is near-universal; the best is under a quarter. So this set is not
# describing common plumbing, it is blanket-suppressing the four most frequently
# typed flags in the codebase, and any real mismatch on them is invisible.
#
# THE FOUR ARE NOW REMOVED (, measured 2026-08-11, alpha, hostname
# cc-08, uname -r 6.8.0-137-generic). The prior note deferred this because
# "removing entries here RAISES the finding count" and the FP rate was UNMEASURED.
# Both premises were tested one flag at a time, patching only this constant and
# running the real main():
#
#     removed alone   --source   --agent   --output   --json     ALL FOUR
#     newly surfaced      0          0         0         0           0
#     finding_count       5          5         5         5           5
#
# THE FP RATE IS 0/0 — UNDEFINED, NOT LOW, and the distinction is the whole
# result. Removal surfaces nothing to classify, so there was never a false
# positive to fear; there was also never a real mismatch being hidden. The prior
# note's "any real mismatch on them is invisible" is therefore measurably false
# ON THIS CORPUS, exactly as its own "not near-universal" measurement was false
# in the other direction.
#
# THAT ZERO IS NOT "NO OPPORTUNITY" (rb-245 — a zero-count claim needs a probe
# that the number can move at all). Two controls: adding a flag a real finding
# names drops the count 5 -> 4, so the harness is wired; and 394 SKILL.md call
# sites pass one of these four (--source 169, --json 144, --output 48,
# --agent 33), so the suppression had 394 chances to hide a mismatch and hid
# none. Every one of those call sites passes a flag its callee genuinely accepts.
#
# CONSEQUENCE FOR THE FLOOR: the count does not move, so 's sequencing
# rule is satisfied with ZERO re-seeds — the blocker that deferred this work did
# not exist. Do not re-seed for this change; meta/audit-baselines.yaml
# skillmd_flag_mismatches stays at 5.
#
# --help/-h STAY, and are the only genuinely load-bearing members: emptying the
# whole set surfaces exactly ONE finding, blocker-create-gate.py --help, which is
# the argparse auto-flag FP (py_flags reads add_argument via AST and argparse
# never declares --help explicitly). Removing them would report a real accepted
# flag as unknown. sh_flag_surface already exempts them from refusal
# classification for the same reason.
#
# Re-run before trusting this on a changed corpus: the numbers above are a
# property of today's call sites, not of the flags.
UNIVERSAL_FLAGS = {"--help", "-h"}

_CASE_ARM = re.compile(r"^\s*\(?\s*((?:-{1,2}[A-Za-z0-9][A-Za-z0-9_-]*\s*\|\s*)*-{1,2}[A-Za-z0-9][A-Za-z0-9_-]*)(?:=\*)?\s*\)")
_FLAG_TOKEN = re.compile(r"(?<![\w-])(--[A-Za-z][A-Za-z0-9-]*)")
_WRAPPER_REF = re.compile(r"(?:core/scripts/)?([a-z0-9][a-z0-9_-]*\.(?:sh|py))")
# `[ "$1" = "--flag" ]` / `[[ "${1:-}" != --flag ]]` — the non-getopts way a
# wrapper declares it accepts a flag. heartbeat-tick.sh uses only this form.
_SH_COMPARE = re.compile(r"[=!]=?\s*[\"']?(--[A-Za-z][A-Za-z0-9-]*)")

# A `case` arm that names a flag in order to REJECT it. Syntactically identical
# to an accepting arm, so without this the parser reports a guard as a feature —
# see sh_flag_surface's docstring for the measured inversion. `exit 0` is
# deliberately NOT a refusal: `--help)` arms legitimately print usage and exit 0.
_ARM_REFUSES = re.compile(r"\bexit\s+[1-9]")
# Any sign the arm CONSUMES the flag rather than dying on it: consuming argv,
# appending to a passthrough array, or assigning a variable.
_ARM_ACCEPTS = re.compile(r"\bshift\b|\+=|[A-Za-z_][A-Za-z0-9_]*=")
# Help flags are never "refused": a `--help)` arm that prints usage and exits
# non-zero is the flag WORKING, not the flag being rejected. `platform-check.sh:70`
# (`-h|--help) usage; exit 2 ;;`) is the shape — exit status is a style choice
# there, not a verdict on the flag.
_HELP_FLAGS = {"--help", "-h"}


def _arm_body(lines: list[str], i: int, limit: int = 40) -> str | None:
    """Text of the case arm starting at lines[i], through its `;;` terminator.

    Returns None on EITHER unresolvable-extent condition — the arm's extent is
    then UNKNOWN, and an unknown body must never be classified as a refusal:
      1. no `;;` anywhere inside the window, and
      2. a NEW arm header appears before this arm's own `;;`.
    (2) is the one a wider window cannot fix, and it is why widening is not a
    remedy on its own — see the inline comment on the check.

    That guard is the whole reason this returns Optional. With a 15-line window
    and a silent truncation, a long arm (comment block + heredoc usage text) ran
    past its own `;;` and swept in the NEXT arm's `exit 2`, so the classifier
    read a neighbour's exit as this arm's. Measured on `aspirations-query.sh:65`,
    whose help arm carries a comment block explicitly stating "Help exits 0" and
    was still reported refused. Bounded windows fail toward the neighbour, so the
    failure has to be detected rather than absorbed.
    """
    body = []
    for j in range(i, min(i + limit, len(lines))):
        # A NEW arm header before this arm's own `;;` means THIS arm is
        # unterminated — stop, do not absorb the neighbour. Widening the window
        # alone does NOT fix the overrun, it only makes it rarer: the scan then
        # finds the NEXT arm's `;;` and attributes that arm's `exit` to this
        # flag. Measured after the 15->40 widening had already cut refusals from
        # 27-across-10 to 9-in-1 — an unterminated `--first` followed 12 lines
        # later by `--second) exit 1 ;;` still reported `--first` REFUSED.
        if j > i and _CASE_ARM.match(lines[j]):
            return None
        body.append(lines[j])
        if ";;" in lines[j]:
            return "\n".join(body)
    return None


def sh_flag_surface(path: Path) -> tuple[set[str], set[str]] | None:
    """(accepted, refused) flags for a bash wrapper. None if unparseable.

    TWO declaration forms, both real in this codebase — parsing only the first
    produces false positives:
      1. `case` arms:            `--flag)` / `--flag|-f)` / `--flag=*)`
      2. string comparison:      `if [ "${1:-}" != "--flag" ]` / `[[ "$1" == --flag ]]`
    `heartbeat-tick.sh` uses form 2 exclusively for `--bypass-state`, so a
    case-arm-only parser reported both of its legitimate call sites as unknown-flag
    mismatches. Comment lines are excluded so a `# ... --flag ...` note does not
    register a flag the parser never accepts.

    THIRD FORM, and it points the OPPOSITE way from the two above (FIX 4,
    g-115-3122): an arm can name a flag in order to REFUSE it, and that is
    syntactically indistinguishable from acceptance. Measured on
    `aspirations-add-goal.sh:113` — ONE arm listing NINE field-shaped flags
    (`--title|--description|--priority|--status|--participants|--category|
    --skill|--asp-id|--asp_id`) whose entire body echoes an error to stderr and
    `exit 2`. Its L117 comment records exactly why it exists: an LLM typed
    `--title`, it reached the daemon, and the failure was opaque. So the parser
    read a guard built to stop LLMs typing `--title` and told the next LLM that
    `--title` was accepted — it INVERTED the guard.

    This is worse in kind than the under-reporting elsewhere in this file: an
    under-report makes you look further, an over-report makes you confident.
    Refused flags are RETURNED, not dropped, because "this flag is actively
    rejected" is strictly more useful to a caller than "unknown" — and dropping
    them silently would recreate the (none)-means-unknown collapse that
    wrapper-surface.py's KNOWN LIMITS already warns about.

    Classification is deliberately NARROW, per this file's conservative-by-
    construction design: an arm counts as refusing only when it contains a
    literal non-zero `exit` AND shows no sign of consuming the flag (no `shift`,
    no `+=`, no assignment). An arm that delegates to a `usage`/`die` helper
    without a literal exit stays classified as accepting — that is the status
    quo, whereas a false refusal would be a NEW defect.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    accepted: set[str] = set()
    refused: set[str] = set()
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("#"):
            continue
        m = _CASE_ARM.match(line)
        if m:
            body = _arm_body(lines, i)
            # body is None => arm extent unknown => treat as accepting (status quo).
            is_refusal = bool(body) and bool(_ARM_REFUSES.search(body)) \
                and not _ARM_ACCEPTS.search(body)
            for alt in m.group(1).split("|"):
                alt = alt.strip()
                if not alt.startswith("-"):
                    continue
                # A help flag is never refused, however the arm exits.
                (refused if (is_refusal and alt not in _HELP_FLAGS)
                 else accepted).add(alt)
            continue
        # form 2 — only inside a test/conditional, so a flag being PASSED to an
        # inner command on an ordinary line is not mistaken for one being ACCEPTED.
        if re.search(r"\b(if|elif|while)\b|\[\[|\[ ", line):
            for m2 in _SH_COMPARE.finditer(line):
                accepted.add(m2.group(1))
    # A flag both accepted somewhere and refused elsewhere is ACCEPTED — the
    # accepting arm is reachable, so calling it a mismatch would be a false
    # positive.
    return accepted, (refused - accepted)


def sh_flags(path: Path) -> set[str] | None:
    """Flags a bash wrapper ACCEPTS (refusal arms excluded). None if unparseable.

    Thin wrapper over sh_flag_surface so existing callers — including
    wrapper-surface.py, which imports this engine by file path — keep their
    signature. Use sh_flag_surface directly when you need the refused set.
    """
    surface = sh_flag_surface(path)
    return None if surface is None else surface[0]


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


# FIX 1 (): the `^` alternative used to sit at the head of this
# alternation, and it made the invocation-keyword requirement a NO-OP — `^`
# matches every non-comment line, and the lazy `[^#\n]*?` then reaches any
# basename anywhere on it. So a bare mention registered as a delegation, and the
# delegate's whole flag surface was unioned into the caller's, HIDING real
# mismatches. Worst case measured: `cross-agent-write.sh` absorbed the surfaces
# of aspirations-claim.sh, board-post.sh, heartbeat-tick.sh,
# team-state-in-flight.sh and team-state-clear-in-flight.sh — five wrappers it
# names in a REFUSAL whitelist, i.e. the exact opposite of delegating to them.
#
# WHY THE KEYWORD SET IS WIDER THAN THE OBVIOUS FIX. Removing `^` alone is
# wrong: measured over core/scripts/*.sh it drops 68 edges that resolve to REAL
# files, and the dominant class is a LEGITIMATE delegation written as an
# assignment — `SCRIPT_PATH="$PROJECT_ROOT/core/scripts/foo-gate.py"`,
# `GATE="$SCRIPT_DIR/capability-gate.py"`, `ENGINE=".../guardrail_retire.py"` —
# the standard shape of every hook wrapper here. (The other 419 "lost" edges are
# `source "..._platform.sh"` lines whose captured basename has no leading
# underscore and so names a file that does not exist; those were already no-ops
# at the `.exists()` guard in wrapper_surface.)
#
# The asymmetry decides the trade. A surviving phantom edge makes a surface too
# WIDE and hides a mismatch — an under-report, which makes a reader look
# further. A dropped legitimate edge makes a surface too NARROW and flags a
# correct call site — an over-report, which makes a reader confident and wrong
# (the same argument sh_flag_surface makes about refusal arms). So this admits
# the assignment / source / comma-in-argv-list forms rather than dropping them,
# and accepts that some prose survives.
_INVOKES = re.compile(
    r"(?:[;&|(,]|\bexec\s|\bbash\s|\bsh\s|\bpython3?\s|\bpy\s+-3\s"
    r"|\bsource\s|=\s*[\"']?)"
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
    surface, _provisional = _wrapper_surface(name, cache, _seen)
    return surface


def _wrapper_surface(name: str, cache: dict,
                     _seen: frozenset) -> tuple[set[str] | None, bool]:
    """(surface, provisional). provisional=True => truncated by a cycle; DO NOT cache.

    FIX 3 (g-115-3122). The cycle guard itself is correct — a frame already on
    the stack contributes nothing, which terminates. The defect was that the
    CALLER then memoized its own truncated union as if complete. For a.sh <-> b.sh
    with a SHARED cache: resolving a.sh recurses into b.sh, b.sh's recursion back
    into a.sh correctly returns empty, so b.sh caches as sh_flags(b.sh) ALONE —
    and a later top-level lookup of b.sh reads that truncated entry and reports
    every flag a.sh would have contributed as unknown. False positives, which is
    the direction this detector exists to avoid.

    Was filed LATENT because `core/scripts/*.sh` has no delegation cycle today, so
    nothing reaches it. It is nonetheless reachable the moment one appears, and
    `main()` uses exactly one shared cache across every call site — the condition
    the bug needs.

    Note the existing `test_mutual_delegation_terminates` cannot see this: it
    passes a FRESH cache dict per call, so it proves termination only. The
    shared-cache pin lives in test_shared_cache_does_not_memoize_cycle_truncation.
    """
    if name in cache:
        return cache[name], False
    if name in _seen:                      # cycle — contribute nothing, don't fail
        return set(), True
    path = SCRIPTS_DIR / name
    if not path.exists():
        cache[name] = None
        return None, False

    provisional = False
    if name.endswith(".py"):
        surface = py_flags(path)
    else:
        surface = sh_flags(path)
        if surface is not None:
            for tgt in delegate_targets(path):
                if not (SCRIPTS_DIR / tgt).exists():
                    continue
                sub, sub_provisional = _wrapper_surface(tgt, cache, _seen | {name})
                provisional |= sub_provisional
                if sub is None:
                    # Unparseable delegate => unknown surface, not a wrong one.
                    # Not cycle-dependent, so this IS safe to cache.
                    cache[name] = None
                    return None, False
                surface |= sub
    # Cache only a COMPLETE result. A provisional one is correct for this call
    # (the caller above it supplies the missing hop) but wrong to hand to a later
    # top-level lookup, so it is returned and discarded.
    if not provisional:
        cache[name] = surface
    return surface, provisional


_SKILL_NAME_CACHE: dict[str, frozenset[str]] = {}


def known_skill_names(skills_dir: Path | None = None) -> frozenset[str]:
    """Directory names under the skills dir — i.e. the real `/slash-command` set.

    Used to anchor the slash-command truncation in audit_line. Cached per
    directory so the glob runs once, not once per scanned line.
    """
    d = skills_dir or SKILLS_DIR
    key = str(d)
    if key not in _SKILL_NAME_CACHE:
        try:
            _SKILL_NAME_CACHE[key] = frozenset(
                p.parent.name for p in d.glob("*/SKILL.md"))
        except OSError:
            _SKILL_NAME_CACHE[key] = frozenset()
    return _SKILL_NAME_CACHE[key]


def audit_line(line: str, cache: dict, skill_names: frozenset[str] | None = None):
    """Return (finding|None, skip_reason|None) for one SKILL.md line."""
    if skill_names is None:
        skill_names = known_skill_names()
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
    #
    # ANCHORED ON THE REAL SKILL SET (FIX 2, ). The prior pattern was a
    # bare `(?<![\w/])/[a-z][a-z0-9-]+`, which matches the first segment of ANY
    # absolute UNIX path: in `--json /opt/ayoai-mind/x --bad-flag` it matched
    # `/opt` and truncated the tail at offset 8, so `--bad-flag` was never
    # scanned. Every flag appearing after any absolute path on a call line went
    # unexamined, silently and with no skip recorded.
    #
    # Requiring the token to name an actual skill directory is the "boundary a
    # path cannot satisfy" the fix calls for: `/opt` is not a skill, so it no
    # longer truncates, while a genuine `/review-hypotheses --learn` still does.
    # An empty skill set (a --skills-dir override with no SKILL.md) truncates
    # nothing, which is the honest reading — with no known skills, no token on
    # the line can be shown to be a slash-command.
    for m_skill in re.finditer(r"(?<![\w/])/([a-z][a-z0-9-]+)", tail):
        if m_skill.group(1) in skill_names:
            tail = tail[: m_skill.start()]
            break
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
                     "RE-SEEDED at 5 on 2026-08-10 (g-115-3122) after all six "
                     "accuracy fixes landed together; the prior key was ABSENT "
                     "from this file, so every run reported SEEDED and never "
                     "REGRESSED. Do NOT drive this to 0: all 5 are a FLOOR of "
                     "prose false positives (a flag belonging to a different "
                     "command on the same line), enumerated in "
                     "verify-learning/SKILL.md. Investigate any INCREASE."),
            "history": history[-50:],
        }
        captured.update(verdict=verdict, new_baseline=new_baseline,
                        message=message)
        return baselines

    try:
        locked_modify_yaml(META_DIR / "audit-baselines.yaml", _modify, initial={})
    except Exception as e:  # never block the loop on a baseline-write failure
        print(f"WARN: could not persist baseline: {e}", file=sys.stderr)
        # OVERWRITE, never setdefault. _modify runs INSIDE locked_modify_yaml
        # and populates `captured` before the write; if the write then fails
        # (disk full, conflict-retry exhausted, validation), setdefault is a
        # no-op and this would report the COMPUTED verdict as though it had
        # persisted. stderr is the only contradicting signal and no JSON
        # consumer reads it. A tool must not claim a write it did not make.
        computed = captured.get("verdict")
        captured["verdict"] = "error"
        captured["new_baseline"] = None
        captured["message"] = (
            f"baseline operation FAILED and nothing was persisted: {e}"
            + (f" (the computed verdict was '{computed}' — it did NOT "
               f"take effect)" if computed else ""))

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
    skill_names = known_skill_names(skills)
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
            finding, skip = audit_line(line, cache, skill_names)
            if skip:
                skips[skip] = skips.get(skip, 0) + 1
            # FIX 6 () — this scanner reads ONE line per call site, and
            # does not follow backslash continuations. A call site whose first
            # line ends in `\` has every subsequent flag invisible, yet was
            # counted as fully resolved with no skip: `--show-skips` exists for
            # "honest coverage accounting" and was reporting FULL coverage on
            # call sites it had read a fraction of.
            #
            # Recorded as a skip rather than fixed by joining, deliberately. A
            # joined line reintroduces the multi-command false positive that the
            # slash-command and `&&`/`;` truncations above exist to fight —
            # continuation blocks are exactly where `git merge`/`git diff`
            # sub-commands sit beside a wrapper call, and three of the five live
            # findings are already that class. Converting a silent blind spot
            # into a COUNTED one is the strictly-honest move; joining is a
            # separate change that must be measured against the FP count.
            #
            # This is additive to any skip above: the visible first line is still
            # audited normally, so a real finding on it is still reported. The
            # counter says "coverage here was partial", not "nothing was checked".
            if line.rstrip().endswith("\\") and any(
                    (SCRIPTS_DIR / m.group(1)).exists()
                    for m in _WRAPPER_REF.finditer(line)):
                skips["line-continuation-partial"] = \
                    skips.get("line-continuation-partial", 0) + 1
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
