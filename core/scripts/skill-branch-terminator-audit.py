#!/usr/bin/env python3
"""skill-branch-terminator-audit.py — static audit of SKILL.md procedural fences.

For every non-exempt .claude/skills/*/SKILL.md, find each procedural fenced
code block (``` ... ```), identify the block's *effective terminator* — the
last statement-starting line, skipping trailing flow markers and multi-line
continuations — and classify it:

  FAIL  — explicit text-output keyword (Output:/Report:/Log:/Record:)
  SAFE  — tool call (Bash:/Skill(/invoke /) or typed RETURN(values) or
          abort-like terminator (ABORT/BREAK) or bare Read/Write/Edit
  WARN  — unrecognized terminator shape (manual review suggested)

MVP scoping: we flag ONLY the obvious text-ending bug class. The RP section
check in verify-learning catches the common case; this scanner catches the
rarer "section present, but this specific branch ends in Output:" variant.
Minimum false positives is a non-negotiable design goal — a noisy scanner
gets ignored, and the RP check is already strong enough to stand alone.
--fail-on warn can be enabled to get stricter scrutiny once the baseline
is clean.

Design notes preserved in-code to prevent drift:
  - A fence is "procedural" only if at least one line matches a statement
    keyword (Bash:, Skill(, IF, FOR, RETURN, etc.). YAML/JSON examples skip.
  - The ## Return Protocol section body is skipped entirely — those
    sections quote Bash: and Output: as documentation, not pseudocode.
  - Multi-line Bash commands and JSON/YAML payloads inside a procedural
    fence: the backwards walk skips continuation lines (lines that do
    not start with a known statement keyword), so the effective terminator
    is always a statement line, never a continuation.
  - Exempt list lives in EXEMPT_SKILLS below and mirrors the list in
    .claude/skills/verify-learning/SKILL.md case branch. Keep in sync.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

EXEMPT_SKILLS = {
    "start", "stop", "open-questions",
    "init", "review", "security-review",
    "tree-reader", "verify-learning",
}

# A fence is procedural if it contains at least one statement-start line.
# We reuse STATEMENT_START below — defined later — via a helper. The
# indirection is so STATEMENT_START stays the single source of truth for
# what "begins a statement" means.
def _is_procedural_fence(lines: list[tuple[int, str]]) -> bool:
    return any(STATEMENT_START.search(t) for _, t in lines)

# STATEMENT_START: lines that begin a logical statement (not a continuation).
# Used to distinguish terminators from multi-line string / JSON / Bash
# continuation lines. If a line does NOT match this, it is a continuation
# and is skipped when walking backwards for the effective terminator.
STATEMENT_START = re.compile(
    r"^\s*("
    r"Bash:|Skill\(|invoke\s+/|→"
    r"|IF\b|ELIF\b|ELSE\b|FOR\b|WHILE\b|DO\b"
    r"|RETURN\b|ABORT\b|BREAK\b|CONTINUE\b|LOOP_CONTINUE\b"
    r"|Output:|Report:|Log:|Record:|Append"
    r"|Read\b|Write\b|Edit\b"
    r"|Check:|Verify:|Assert:|Ensure:|Note:"
    r"|echo\s"
    # \d+(?:\.\d+)*\.\s matches step prefixes like `7. `, `7.5. `, `1.2.3. ` —
    # the previous `\d+\.\s` form missed sub-numbered labels because `7.5.`
    # is digit-period-DIGIT, not digit-period-WHITESPACE ().
    r"|\d+(?:\.\d+)*\.\s|[-*]\s"
    r")"
)

# Flow-only markers: trailing glue that doesn't itself emit a tool call.
# `→ DONE`, bare `RETURN`. Skipped when walking backwards for a terminator.
# Note: RETURN(values) is NOT flow-only (carries data) — handled separately.
# NOT included here — these are NOT flow-only:
#   LOOP_CONTINUE — expands to Skill('aspirations'); IS a tool call. SAFE.
#   BREAK / continue — loop-level flow, NOT skill-level exits. The skill
#     keeps executing past the enclosing loop, so these should not mark a
#     skill-exit point; scanner ignores them entirely.
FLOW_ONLY = re.compile(
    r"^\s*(RETURN\s*$|→\s*(DONE|STOP|RETURN|Fall\s+through)\s*$)"
)

# FAIL: explicit text-output keyword. Narrowed to only `Output:` — the most
# unambiguous text-emission shape. Other keywords (Log, Report, Record, Return)
# are used ambiguously across skills:
#   - `Log: "msg"` can mean "print" OR be pseudocode for Bash: journal-add.sh
#   - `Log <target>: ...` is almost always a tool call shorthand
#   - `Report:` tends to be print-like but some skills use it for structured
#     handoff to a parent.
# Those stay as WARN (see classify_terminator) — surface but don't fail.
# "Return:" as a label is distinct from RETURN (Python-like statement).
#
# Print: classification decision ( audit, 2026-05-09):
# FAIL_TEXT_OUTPUT detects 'Output:' as a procedural fence terminator that
# emits text (kills loop per .claude/rules/return-protocol.md). 'Print:' is
# also a text-emission keyword, but as of 2026-05-09 ( audit) it
# appears only in encode-session/SKILL.md (21 occurrences) as mid-phase
# status echoes — none are fence terminators. Print: is therefore matched
# by FAIL_TEXT_OUTPUT_WARN (advisory) rather than FAIL_TEXT_OUTPUT (hard
# fail). Re-evaluate this if a Print: appears as a terminal pseudocode
# line in any SKILL.md (see  decision finding for protocol).
FAIL_TEXT_OUTPUT = re.compile(r"^\s*Output:(?!\s*\()")

# SAFE: tool calls and tool-call-equivalent terminators.
# The `echo ... | Bash:` form is the documented SKILL.md idiom for piping
# stdin into a bash tool-call (e.g., `echo '<json>' | Bash: wm-set.sh ...`).
# Match BOTH `| bash ` (raw shell pipe) AND `| Bash:` (tool-call marker) —
# the latter is the one actually written across SKILL.md files ().
SAFE_TOOL_CALL = re.compile(
    r"^\s*("
    r"Bash:|Skill\(|invoke\s+/|→\s*Skill\("
    r"|echo\s+.*\|\s*(bash\s|Bash:)"
    r"|cat\s+.*\|\s*Bash:"
    r"|Read\s+|Write\s+|Edit\s+"
    r"|ABORT\b"               # ABORT is an explicit error-exit signal — caller handles
    r"|LOOP_CONTINUE\s*$"     # expands to Skill('aspirations') with args='loop'
    r")"
)
SAFE_TYPED_RETURN = re.compile(r"^\s*RETURN\s*\(")  # Type D: sub-skill handoff with values

# Optional sub-numbered step label that can prefix a terminator. SKILL.md
# files numbered with `7.5. Bash: ...`, `1.2.3. Skill(...)` should classify
# the same as the bare `Bash: ...` / `Skill(...)` form — the step label is
# narrative scaffolding, not part of the terminator content. classify_
# terminator strips this prefix before the SAFE/FAIL regexes ().
STEP_PREFIX = re.compile(r"^(\s*\d+(?:\.\d+)*\.\s+)")


@dataclass
class Finding:
    file: str
    line: int  # 1-indexed line of the terminator
    skill: str
    fence_start_line: int
    severity: str  # SAFE | WARN | FAIL
    captured: str  # the terminator line verbatim (stripped)
    reason: str

    def to_json(self) -> dict:
        return {
            "file": self.file,
            "line": self.line,
            "skill": self.skill,
            "fence_start": self.fence_start_line,
            "severity": self.severity,
            "captured": self.captured,
            "reason": self.reason,
        }


def classify_terminator(line: str) -> tuple[str, str]:
    """Return (severity, reason) for a single terminator line.

    MVP policy: FAIL only on explicit text-output keywords. Anything else
    that isn't clearly a tool call is WARN — flagged for human review,
    does not fail the build by default. Minimum false positives wins here;
    the RP section check is already the primary defense.
    """
    # Strip optional step label (e.g., `7.5. ` from `7.5. Bash: foo.sh`)
    # before classification — see STEP_PREFIX ().
    m = STEP_PREFIX.match(line)
    bare = line[m.end():] if m else line
    if FAIL_TEXT_OUTPUT.match(bare):
        return "FAIL", "explicit Output: as last action — LLM emits text, loop dies"
    if SAFE_TOOL_CALL.match(bare):
        return "SAFE", "tool call"
    if SAFE_TYPED_RETURN.match(bare):
        return "SAFE", "typed sub-skill return (caller owns terminal)"
    return "WARN", "unrecognized terminator shape — manual review"


def _find_terminator_before(
    procedural_lines: list[tuple[int, str]],
    exit_index: int,
) -> Optional[tuple[int, str]]:
    """Walk backwards from exit_index, return the first statement-start line.

    Skips blanks, comments, flow-only markers, and continuation lines.
    A typed RETURN(...) at exit_index itself IS a terminator (Type D return
    with values) — handled by the caller, not here.
    """
    for i in range(exit_index - 1, -1, -1):
        line_no, text = procedural_lines[i]
        stripped = text.strip()
        if not stripped:
            continue
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        if FLOW_ONLY.match(text):
            continue
        if not STATEMENT_START.match(text):
            continue
        return (line_no, text)
    return None


def analyze_skill_exits(
    procedural_lines: list[tuple[int, str]],
    skill: str,
    file: str,
) -> list[Finding]:
    """Find every exit point and classify the statement that produces each one.

    Branch terminators in a SKILL.md are the explicit exits — RETURN, → DONE,
    → STOP — plus the implicit exit at EOF (the last statement of the last
    procedural block in the file). Every statement that is immediately
    before one of those exits is a branch terminator that the LLM emits.

    Fences are not branches — a procedural flow can span multiple fences
    with English prose between. We collect all procedural lines (with
    line numbers preserved) into one flat sequence and scan it for exits.
    """
    findings: list[Finding] = []
    if not procedural_lines:
        return findings

    seen: set[int] = set()  # (line_no) of terminators we've already reported

    def _record(term: Optional[tuple[int, str]], exit_context: str) -> None:
        if term is None:
            return
        if term[0] in seen:
            return
        seen.add(term[0])
        severity, reason = classify_terminator(term[1])
        findings.append(Finding(
            file=file, line=term[0], skill=skill, fence_start_line=0,
            severity=severity, captured=term[1].strip(),
            reason=f"{reason} (exit: {exit_context})",
        ))

    for i, (line_no, text) in enumerate(procedural_lines):
        # Type D: RETURN with values is itself a terminator — the return
        # values ARE the tool call in the sense that the sub-skill returns
        # structured data to the caller. Mark SAFE at its own line.
        if SAFE_TYPED_RETURN.match(text):
            if line_no not in seen:
                seen.add(line_no)
                findings.append(Finding(
                    file=file, line=line_no, skill=skill, fence_start_line=0,
                    severity="SAFE", captured=text.strip(),
                    reason="typed sub-skill return (caller owns terminal)",
                ))
            continue
        # Bare exit markers — look backwards for the effective terminator.
        if FLOW_ONLY.match(text):
            term = _find_terminator_before(procedural_lines, i)
            _record(term, f"precedes {text.strip()}")

    # Implicit exit at EOF — last statement of the last procedural line.
    term = _find_terminator_before(procedural_lines, len(procedural_lines))
    _record(term, "EOF")

    return findings


def audit_skill(path: Path) -> list[Finding]:
    """Return all findings for one SKILL.md."""
    skill = path.parent.name
    if skill in EXEMPT_SKILLS:
        return []

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        return [Finding(
            file=str(path), line=0, skill=skill, fence_start_line=0,
            severity="WARN", captured=f"<read error: {e}>",
            reason="could not read file",
        )]

    # Relative path for cleaner output.
    try:
        rel = str(path.relative_to(Path.cwd()))
    except ValueError:
        rel = str(path)

    # Collect all procedural lines across all fences in the file, skipping
    # ## Return Protocol section bodies. Retain original line numbers so
    # findings point at real SKILL.md locations.
    procedural_lines: list[tuple[int, str]] = []
    current_fence_lines: list[tuple[int, str]] = []
    in_fence = False
    in_rp_section = False

    for idx, raw in enumerate(lines, start=1):
        if raw.startswith("## "):
            in_rp_section = raw.strip().lower() == "## return protocol"
            if in_fence:
                # Malformed — fence crossing section boundary. Drop the
                # partial fence; don't pollute procedural stream.
                in_fence = False
                current_fence_lines = []
            continue

        if in_rp_section:
            continue

        stripped = raw.strip()
        if stripped.startswith("```"):
            if not in_fence:
                in_fence = True
                current_fence_lines = []
            else:
                # Closing fence — promote to procedural stream ONLY if it
                # looks procedural (contains any statement-start line).
                # Payload / YAML / JSON fences are skipped entirely.
                if _is_procedural_fence(current_fence_lines):
                    procedural_lines.extend(current_fence_lines)
                in_fence = False
                current_fence_lines = []
            continue

        if in_fence:
            current_fence_lines.append((idx, raw))

    return analyze_skill_exits(procedural_lines, skill, rel)


def main() -> int:
    # Force utf-8 stdout so `→` and other non-cp1252 chars don't crash on Windows.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Python 3.7+
    except (AttributeError, OSError):
        pass

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true",
                    help="emit JSON lines (one per finding) instead of text")
    ap.add_argument("--only", metavar="SKILL_NAME",
                    help="audit only one skill directory")
    ap.add_argument("--fail-on", choices=["fail", "warn"], default="fail",
                    help="exit 1 on FAIL (default) or on FAIL|WARN")
    ap.add_argument("--skills-dir", default=".claude/skills",
                    help="root of skills directory")
    args = ap.parse_args()

    skills_root = Path(args.skills_dir)
    if not skills_root.is_dir():
        print(f"error: skills dir not found: {skills_root}", file=sys.stderr)
        return 2

    all_findings: list[Finding] = []
    skill_files = sorted(skills_root.glob("*/SKILL.md"))
    for f in skill_files:
        skill = f.parent.name
        if args.only and skill != args.only:
            continue
        all_findings.extend(audit_skill(f))

    # Emit.
    if args.json:
        for finding in all_findings:
            print(json.dumps(finding.to_json()))
    else:
        by_sev = {"FAIL": [], "WARN": [], "SAFE": []}
        for f in all_findings:
            by_sev[f.severity].append(f)
        print(f"skill-branch-terminator-audit: {len(skill_files)} skills scanned "
              f"(FAIL={len(by_sev['FAIL'])}, WARN={len(by_sev['WARN'])}, "
              f"SAFE-findings={len(by_sev['SAFE'])})")
        for severity in ("FAIL", "WARN"):
            if not by_sev[severity]:
                continue
            print(f"\n-- {severity} --")
            for f in by_sev[severity]:
                print(f"  {f.file}:{f.line}  [{f.skill}]  {f.reason}")
                print(f"    > {f.captured}")

    # Exit code.
    fails = [f for f in all_findings if f.severity == "FAIL"]
    warns = [f for f in all_findings if f.severity == "WARN"]
    if fails:
        return 1
    if args.fail_on == "warn" and warns:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
