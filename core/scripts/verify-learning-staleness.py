#!/usr/bin/env python3
"""verify-learning-staleness — detect stale Check:/Bash: assertions.

Scans a SKILL.md (default verify-learning, or any path via --skill-md, or
the entire .claude/skills/*/SKILL.md surface via --all-skills) for
assertions that reference files, phases, grep patterns, or CLI flags
that no longer exist in the codebase. Catches the "refactor moved the
target but nobody updated the SKILL pseudocode" failure mode.

Four detection lanes (each fails open on parse error):

  L1 — Path references:
    Any `core/scripts/foo.{py,sh}`, `.claude/skills/<n>/SKILL.md`,
    `core/config/<n>.{md,yaml}`, `world/conventions/<n>.md`,
    `meta/<n>.{yaml,jsonl}` mentioned inside a `Check:` or `Bash:` line
    must resolve to an existing file.

  L2 — Phase/Step references:
    A line that names a SKILL.md path AND a phrase like "Phase 8.5",
    "Step 8.77", "Phase 0.5.0a", or "Step 2.55" must be matchable by
    grep inside that file. Stale = the phase/step header doesn't appear.

  L3 — Grep-target references:
    A line of the form `Grep <FILE> for <PATTERN>` or `grep ... <FILE>`
    where PATTERN is in backticks must produce ≥1 match in FILE.

  L4 — Argparse-flag references (g-001-207):
    A `Bash: <script>.{sh,py} --flag <args>` invocation where `--flag`
    is a literal flag (not a `<placeholder>`). Stale = flag string is
    absent from the script's source AND from any python script the
    shell wrapper forwards via `exec python3 ... "$@"`. Catches the
    SKILL-pseudocode-vs-actual-script-API drift class (rb-412, rb-485,
    guard-359 cover the proactive author side; this is the post-write
    detection layer). Surfaced from felt-sense 2026-04-26 hitting
    `aspirations-query.sh --status` (actual flag is `--goal-status`).

Output: JSON to stdout. Exit 0 if 0 stale, exit 1 if any stale found.
Use --text for human summary on stdout.

Invocation:
  py -3 core/scripts/verify-learning-staleness.py
  py -3 core/scripts/verify-learning-staleness.py --text
  py -3 core/scripts/verify-learning-staleness.py --skill-md <path>
  py -3 core/scripts/verify-learning-staleness.py --all-skills
  py -3 core/scripts/verify-learning-staleness.py --all-skills --text
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
DEFAULT_SKILL_MD = REPO_ROOT / ".claude" / "skills" / "verify-learning" / "SKILL.md"

# Regexes anchored to the framework's path conventions. Deliberately
# narrow — we only flag references with known framework prefixes to avoid
# false positives on prose like "the script foo.py" without context.
#
# CRITICAL: `meta/` and `world/` are EXTERNAL paths configured per-agent
# in <agent>/local-paths.conf and not committed to the local repo. They
# are deliberately excluded from L1 — checking their existence against
# REPO_ROOT would always fail. (See CLAUDE.md "External paths" section.)
_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])"
    r"((?:core/scripts|core/config|core/config/conventions|core/config/rationale|"
    r"core/config/modes|"
    r"\.claude/skills/[a-z0-9-]+|\.claude/rules)"
    r"/[A-Za-z0-9_.-]+\.[a-z]+)"
)

# `Grep Phase X for \`pattern\` must match` / `Grep Step X for ...` —
# the Check: line will also mention a SKILL.md path, which we resolve
# via _PATH_RE; the Phase/Step is then the section anchor inside that
# SKILL.md. Stale detection: pattern absent from the WHOLE skill file
# (don't try to bound by section — too brittle, and a moved-out string
# usually moved to a different file entirely).
_GREP_PHASE_RE = re.compile(
    r"[Gg]rep\s+(?:Phase|Step)\s+[\d.]+[a-zA-Z]*\s+for\s+`([^`]+)`"
)

# Phase/Step phrases. Matches "Phase 0.5.0a", "Step 8.77", "Step 2.55",
# "Phase 9.5d". Captures the leading verb (Phase|Step) and the identifier.
_PHASE_RE = re.compile(r"\b(Phase|Step)\s+([\d.]+[a-zA-Z]*)\b")

# A `Check:` or `Bash:` line is what we evaluate. Markdown bullet `-` or
# leading whitespace allowed.
_CHECK_LINE_RE = re.compile(r"^\s*-?\s*(Check|Bash):\s*(.+)$")

# `Grep <FILE> for <PATTERN>` — PATTERN is backticked.
# Variant form: `grep ... <PATTERN> ... <FILE>` — harder to parse
# reliably; we only handle the explicit "Grep <FILE> for `pattern`" form
# plus the common "Grep <PATH> for `<phrase>` must match" idiom used in
# the existing checklist.
_GREP_TARGET_RE = re.compile(
    r"[Gg]rep\s+([A-Za-z0-9_./-]+\.[a-z]+|[A-Za-z0-9_-]+/SKILL\.md|"
    r"`[^`]+`)"
    r"\s+for\s+`([^`]+)`"
)

# L4: Bash: <script>.sh|<script>.py invocation extraction.
# Matches `Bash: <runner>? <full-script-path>.{sh,py} <args-until-shell-meta-or-paren>`.
# Captures the full path and the args-tail. The args capture STOPS at the
# first shell metacharacter (`|`, `&`, `;`, `>`, `<`) OR opening paren `(`
# so flags belonging to (a) a downstream piped command (e.g., `grep -q`
# after `script.sh ... |`) or (b) parenthesized prose comments (e.g.,
# `wm-read.sh encoding_queue --json  (if --selective mode)`) are NOT
# mis-attributed to the script. Quoted scripts (`'foo.sh'`) are not
# matched — pseudocode rarely uses them; a known limitation. Quote-aware
# arg parsing is also out of scope (a regex pattern with `(` inside `"..."`
# would be truncated; rare enough to accept).
_BASH_SCRIPT_RE = re.compile(
    r"^\s*-?\s*Bash:\s+"
    r"(?:bash\s+|py\s+-3\s+|python\d?\s+)?"   # optional runner prefix
    r"(\S+\.(?:sh|py))\b"                      # full script path (capture 1)
    r"(\s[^|&;<>(\n]*)?"                       # args tail, stops at shell meta or `(`
)

# L4: literal flag tokens. Matches `--name` or `-x` not preceded by `<`,
# `{`, alphanumeric, or `_` (i.e., not inside a placeholder or token).
# Excludes uppercase-led flags (rare in CLI conventions; reduces false
# positives on prose like "X--Y" or `<--marker>`).
_LITERAL_FLAG_RE = re.compile(
    r"(?<![<{a-zA-Z0-9_])(-{1,2}[a-z][a-z0-9-]*)\b"
)

# L4: detect `exec python3 <path>.py` or `python3 "$VAR/path/foo.py"`
# patterns inside shell wrappers. The path may be quoted ("$CORE_ROOT/..."),
# variable-prefixed ($CORE_ROOT/...), or bare. We match through any
# non-whitespace prefix to capture the .py basename. When a shell wrapper
# has no flag in its own body, check whether it forwards to a python
# script — if so, follow the forward and check that script too.
_FORWARD_PY_RE = re.compile(
    r"(?:python3?|py)\s+(?:-3\s+)?"
    r"\S*?([a-z][a-z0-9_-]*\.py)\b"
)

# L4: detect `exec "$..."/<basename>.sh "$@"` shell-wrapper-to-shell
# delegations. Without this, wrappers like agent-aspirations-read.sh
# (which `exec`s aspirations-read.sh) produce false-positive stale
# findings — the flag lives in the underlying sibling, not the wrapper.
#  (2026-05-22).
_FORWARD_SH_RE = re.compile(
    r"^\s*exec\s+.*?/?([a-z][a-z0-9_-]*\.sh)\b",
    re.MULTILINE,
)


def load_skill_md(path: Path) -> list[tuple[int, str]]:
    """Return [(lineno, raw_line)] from the verify-learning SKILL.md."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"[verify-learning-staleness] cannot read {path}: {e}",
              file=sys.stderr)
        return []
    return list(enumerate(text.splitlines(), start=1))


def extract_assertions(lines: list[tuple[int, str]]) -> list[tuple[int, str, str]]:
    """Return [(lineno, kind, body)] for every Check:/Bash: line."""
    out = []
    for lineno, raw in lines:
        m = _CHECK_LINE_RE.match(raw)
        if m:
            out.append((lineno, m.group(1), m.group(2)))
    return out


# Negative-assertion phrases. When the Check: body contains any of these,
# the missing artifact IS the expected state — flagging it as stale would
# be a false positive. Match case-sensitively because "must not" and
# "MUST NOT" carry different verification weights elsewhere in the
# codebase, but for guard purposes either is sufficient evidence of a
# negative assertion.
_NEGATIVE_PHRASES = (
    "does NOT exist",
    "does not exist",
    "DOES NOT exist",
    "must not exist",
    "MUST NOT exist",
    "MUST NOT contain",
    "must not contain",
    "is absent",
    "absent.",  # "Phase X absent."
    "absent (",
    "deleted deliberately",
    "deleted)",
    "has been deleted",
    "have been deleted",
    "removed)",
    "has been removed",
    "have been removed",
    "test ! -f",
    "test ! -d",
    "echo FAIL || echo PASS",  # the inverted-success idiom
    "&& echo FAIL ||",          # ditto
    "must not",
    "MUST NOT",
    "do NOT",
    "DO NOT",
    "does NOT",
    "DOES NOT",
    "no longer exist",
    "should not exist",
)


def is_negative_assertion(body: str) -> bool:
    """Return True if the assertion EXPECTS the artifact to be missing."""
    return any(phrase in body for phrase in _NEGATIVE_PHRASES)


def check_paths(lineno: int, body: str) -> list[dict]:
    """Lane 1: every framework path mentioned must resolve.
    Skips bodies that assert non-existence (negative assertions)."""
    if is_negative_assertion(body):
        return []
    findings = []
    for m in _PATH_RE.finditer(body):
        rel_path = m.group(1)
        abs_path = REPO_ROOT / rel_path
        if not abs_path.exists():
            findings.append({
                "line": lineno,
                "lane": "L1_path",
                "stale_ref": rel_path,
                "detail": f"path {rel_path!r} does not exist",
                "body": body[:160],
            })
    return findings


def check_phases(lineno: int, body: str) -> list[dict]:
    """Lane 2: every Phase/Step reference next to a SKILL.md path must
    be matchable inside that file. Skips negative-assertion bodies."""
    if is_negative_assertion(body):
        return []
    findings = []
    paths = [m.group(1) for m in _PATH_RE.finditer(body)
             if m.group(1).endswith("SKILL.md")]
    if not paths:
        return findings
    phases = list(_PHASE_RE.finditer(body))
    if not phases:
        return findings
    for skill_rel in paths:
        skill_abs = REPO_ROOT / skill_rel
        if not skill_abs.exists():
            continue  # Lane 1 will already flag this
        try:
            skill_text = skill_abs.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pm in phases:
            verb, ident = pm.group(1), pm.group(2)
            patterns = [
                rf"\b{verb}\s+{re.escape(ident)}\b",
                rf"##+\s*{verb}\s+{re.escape(ident)}\b",
            ]
            if not any(re.search(p, skill_text) for p in patterns):
                findings.append({
                    "line": lineno,
                    "lane": "L2_phase",
                    "stale_ref": f"{verb} {ident} in {skill_rel}",
                    "detail": (
                        f"{verb} {ident!r} not found in {skill_rel} — "
                        f"phase/step was renumbered, removed, or extracted"
                    ),
                    "body": body[:160],
                })
    return findings


def check_grep_phase(lineno: int, body: str) -> list[dict]:
    r"""Lane 3b: `Grep Phase X for \`pattern\` must match` — the SKILL.md
    target is mentioned earlier in the same Check: line. Stale = pattern
    absent from that file. Skips negative-assertion bodies."""
    if is_negative_assertion(body):
        return []
    findings = []
    skill_paths = [m.group(1) for m in _PATH_RE.finditer(body)
                   if m.group(1).endswith("SKILL.md")]
    if not skill_paths:
        return findings
    for gm in _GREP_PHASE_RE.finditer(body):
        pattern = gm.group(1)
        for skill_rel in skill_paths:
            skill_abs = REPO_ROOT / skill_rel
            if not skill_abs.exists():
                continue
            try:
                text = skill_abs.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            try:
                hit = bool(re.search(pattern, text))
            except re.error:
                hit = pattern in text
            if not hit:
                findings.append({
                    "line": lineno,
                    "lane": "L3b_grep_phase",
                    "stale_ref": f"`{pattern}` in {skill_rel}",
                    "detail": (
                        f"`{pattern}` not found anywhere in {skill_rel} — "
                        f"likely extracted to a script or removed"
                    ),
                    "body": body[:160],
                })
    return findings


def check_grep_targets(lineno: int, body: str) -> list[dict]:
    """Lane 3: `Grep <FILE> for <PATTERN>` must produce ≥1 match.
    Skips negative-assertion bodies."""
    if is_negative_assertion(body):
        return []
    findings = []
    for m in _GREP_TARGET_RE.finditer(body):
        target_raw = m.group(1).strip("`")
        pattern = m.group(2)
        # Resolve target — accept relative path or basename
        candidates = []
        if "/" in target_raw or "\\" in target_raw:
            candidates.append(REPO_ROOT / target_raw)
        else:
            for sub in (".claude/skills", "core/scripts", "core/config",
                        "world/conventions"):
                p = REPO_ROOT / sub
                if p.exists():
                    candidates.extend(p.rglob(target_raw))
        target_path = None
        for cand in candidates:
            if cand.exists():
                target_path = cand
                break
        if target_path is None:
            findings.append({
                "line": lineno,
                "lane": "L3_grep",
                "stale_ref": f"target {target_raw!r}",
                "detail": f"grep target {target_raw!r} could not be resolved",
                "body": body[:160],
            })
            continue
        try:
            text = target_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            if not re.search(pattern, text):
                findings.append({
                    "line": lineno,
                    "lane": "L3_grep",
                    "stale_ref": f"`{pattern}` in {target_raw}",
                    "detail": (
                        f"grep pattern {pattern!r} returns 0 matches in "
                        f"{target_path.relative_to(REPO_ROOT)}"
                    ),
                    "body": body[:160],
                })
        except re.error:
            # Treat as literal string when not a valid regex.
            if pattern not in text:
                findings.append({
                    "line": lineno,
                    "lane": "L3_grep",
                    "stale_ref": f"`{pattern}` in {target_raw}",
                    "detail": (
                        f"literal {pattern!r} not found in "
                        f"{target_path.relative_to(REPO_ROOT)}"
                    ),
                    "body": body[:160],
                })
    return findings


_SCRIPT_SEARCH_DIRS = (
    "core/scripts",
    "world/scripts",
)


def _resolve_script(basename: str) -> Path | None:
    """Return repo-relative Path to the script, or None if not found.
    Searches core/scripts/ and world/scripts/ in order."""
    for sub in _SCRIPT_SEARCH_DIRS:
        cand = REPO_ROOT / sub / basename
        if cand.is_file():
            return cand
    return None


def _script_accepts_flag(script_path: Path, flag: str) -> bool:
    """Return True if the literal flag string appears in script_path,
    OR in any python script that script_path forwards to via
    `exec python3 ... <forwarded>.py`. Follows up to 2 hops to keep
    runtime bounded; in practice wrappers forward at most once.

    Match policy: literal substring search. Argparse `add_argument`
    calls and shell `case` clauses both contain the literal flag
    string, and false-positive risk (mention in a comment that doesn't
    correspond to actual support) is acceptable for this lane —
    findings are advisory, not blocking."""
    visited: set[Path] = set()
    queue: list[Path] = [script_path]
    hops = 0
    while queue and hops < 3:
        path = queue.pop()
        if path in visited:
            continue
        visited.add(path)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if flag in text:
            return True
        if path.suffix == ".sh":
            for m in _FORWARD_PY_RE.finditer(text):
                forwarded = _resolve_script(m.group(1))
                if forwarded and forwarded not in visited:
                    queue.append(forwarded)
            for m in _FORWARD_SH_RE.finditer(text):
                forwarded = _resolve_script(m.group(1))
                if forwarded and forwarded not in visited:
                    queue.append(forwarded)
        hops += 1
    return False


def check_argparse_flags(lineno: int, body: str) -> list[dict]:
    """Lane 4: every `--flag` mentioned in a `Bash: <script>.sh` line
    must appear in the script's source (or in any python script the
    shell wrapper forwards to). Skips negative-assertion bodies.

    The body of the Bash: line was already split off by
    extract_assertions; reconstruct the full line for matching."""
    if is_negative_assertion(body):
        return []
    full_line = f"Bash: {body}"
    m = _BASH_SCRIPT_RE.match(full_line)
    if not m:
        return []
    script_full = m.group(1)
    # Strip trailing shell-meta (rare — regex anchors via `\b` already)
    # and extract basename from the full path. Handle both POSIX `/`
    # and Windows `\` separators since SKILL pseudocode is path-agnostic.
    script_basename = script_full.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    rest = (m.group(2) or "").strip()
    if not rest:
        return []
    script_path = _resolve_script(script_basename)
    if script_path is None:
        # L1 already flags missing scripts via the path lane; don't
        # double-report under L4.
        return []
    findings = []
    seen_flags: set[str] = set()
    for fm in _LITERAL_FLAG_RE.finditer(rest):
        flag = fm.group(1)
        if flag in seen_flags:
            continue
        seen_flags.add(flag)
        if not _script_accepts_flag(script_path, flag):
            findings.append({
                "line": lineno,
                "lane": "L4_argparse_flag",
                "stale_ref": f"{flag} on {script_basename}",
                "detail": (
                    f"flag {flag!r} not found in {script_basename} "
                    f"or any forwarded python script — "
                    f"argparse drift or wrapper-API mismatch"
                ),
                "body": body[:160],
            })
    return findings


def scan(skill_md: Path) -> dict:
    """Run all four lanes and return a result dict."""
    lines = load_skill_md(skill_md)
    assertions = extract_assertions(lines)
    findings = []
    for lineno, kind, body in assertions:
        findings.extend(check_paths(lineno, body))
        findings.extend(check_phases(lineno, body))
        findings.extend(check_grep_targets(lineno, body))
        findings.extend(check_grep_phase(lineno, body))
        # L4 only fires on Bash: lines — Check: lines that mention an
        # inner Bash snippet (e.g., `Check: foo.py works. Bash: \`grep -c ...\``)
        # would otherwise mis-attribute the inner command's flags
        # (here `-c` belongs to grep, not foo.py).
        if kind == "Bash":
            findings.extend(check_argparse_flags(lineno, body))
    return {
        "skill_md": str(skill_md),
        "assertions_scanned": len(assertions),
        "stale_count": len(findings),
        "findings": findings,
    }


def scan_all_skills() -> dict:
    """Iterate every .claude/skills/<name>/SKILL.md and aggregate
    findings. Each finding carries an extra `skill` field naming the
    skill directory it came from. Per-skill counts are also included
    in `per_skill` for at-a-glance triage."""
    skills_dir = REPO_ROOT / ".claude" / "skills"
    per_skill = {}
    aggregate_findings = []
    total_assertions = 0
    if not skills_dir.is_dir():
        return {
            "mode": "all-skills",
            "skills_scanned": 0,
            "assertions_scanned_total": 0,
            "stale_count_total": 0,
            "per_skill": {},
            "findings": [],
            "error": f"{skills_dir} not found",
        }
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        skill_name = skill_md.parent.name
        result = scan(skill_md)
        per_skill[skill_name] = {
            "skill_md": result["skill_md"],
            "assertions_scanned": result["assertions_scanned"],
            "stale_count": result["stale_count"],
        }
        total_assertions += result["assertions_scanned"]
        for f in result["findings"]:
            f_with_skill = dict(f)
            f_with_skill["skill"] = skill_name
            aggregate_findings.append(f_with_skill)
    return {
        "mode": "all-skills",
        "skills_scanned": len(per_skill),
        "assertions_scanned_total": total_assertions,
        "stale_count_total": len(aggregate_findings),
        "per_skill": per_skill,
        "findings": aggregate_findings,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skill-md", type=Path, default=DEFAULT_SKILL_MD,
                    help="Path to a single SKILL.md (default: verify-learning/SKILL.md). "
                         "Ignored when --all-skills is set.")
    ap.add_argument("--all-skills", action="store_true",
                    help="Scan every .claude/skills/<name>/SKILL.md and aggregate "
                         "findings. Each finding gains a `skill` field. Use this to "
                         "catch SKILL-pseudocode-vs-script-API drift across the whole "
                         "skill surface, not just verify-learning.")
    ap.add_argument("--text", action="store_true",
                    help="Human-readable summary on stdout (JSON on stderr).")
    args = ap.parse_args()

    if args.all_skills:
        result = scan_all_skills()
        stale_total = result["stale_count_total"]
        if args.text:
            print(json.dumps(result, indent=2), file=sys.stderr)
            print(f"verify-learning-staleness (all-skills): scanned "
                  f"{result['skills_scanned']} skills / "
                  f"{result['assertions_scanned_total']} assertions, "
                  f"found {stale_total} stale")
            for f in result["findings"]:
                print(f"  [{f['lane']}] {f['skill']}/SKILL.md "
                      f"line {f['line']}: {f['stale_ref']}")
                print(f"    {f['detail']}")
        else:
            print(json.dumps(result))
        return 0 if stale_total == 0 else 1

    result = scan(args.skill_md)

    if args.text:
        print(json.dumps(result, indent=2), file=sys.stderr)
        print(f"verify-learning-staleness: scanned "
              f"{result['assertions_scanned']} assertions, "
              f"found {result['stale_count']} stale")
        for f in result["findings"]:
            print(f"  [{f['lane']}] line {f['line']}: {f['stale_ref']}")
            print(f"    {f['detail']}")
    else:
        print(json.dumps(result))

    return 0 if result["stale_count"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
