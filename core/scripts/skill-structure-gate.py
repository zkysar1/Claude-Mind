#!/usr/bin/env python3
"""Skill-Structure Gate — dynamic enforcement of SKILL.md invariants.

Replaces ~50 static `Check: X/SKILL.md has Y section` lines in
.claude/skills/verify-learning/SKILL.md that go stale on every sub-skill
extraction. Walks the skill directory tree and enforces four invariants
declared by .claude/rules/return-protocol.md and CLAUDE.md:

  1. return_protocol        -- `## Return Protocol` section required iff the
                               skill is NON-user-invocable (`user-invocable:
                               false` in front matter) OR listed in
                               world/forged-skills.yaml. User-invocable control
                               skills (start, stop, verify-learning, etc.) are
                               explicitly exempt per the rule's own "Applies To"
                               section.
  2. minimum_mode_valid     -- front matter `minimum_mode`, if present, must be
                               one of {reader, assistant, autonomous}.
  3. bash_scripts_exist     -- every literal `bash core/scripts/<name>.sh`,
                               `bash world/scripts/<name>.sh`, and
                               `python3 core/scripts/<name>.py` reference in the
                               SKILL.md body must resolve to a file on disk.
                               Catches broken companion_scripts after rename.
  4. skill_invocations_valid-- every `Skill(<name>)` call names an existing
                               skill directory under .claude/skills/ or a
                               forged skill in world/forged-skills.yaml.

Design discipline (matches capability-gate.py / audit-schema-gate.py):
- Fail-open on missing files or parse errors. This is a correctness linter,
  not a security boundary — a broken gate must not block shipping.
- Each check is independently addressable via --check. Default: all four.
- `--skill NAME` scopes to one skill; `--all` scans everything. One must be set.
- Exit 0 = pass for the target scope, 1 = at least one violation.
- Structured JSON stdout for downstream consumers.

Contract
  --skill <name>     check one skill by directory name (no leading slash)
  --all              check every SKILL.md under .claude/skills/
  --check <names>    comma-separated subset of checks to run (default: all four)
  --json             emit JSON to stdout (default). --text prints a human table.
  --text             human-readable summary on stdout; JSON still on stderr.

Exit codes
  0 pass, 1 block, 2 argument error (handled by argparse).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Reuse shared helpers (per rb/Invest-1 plan — single source of truth for
# front matter parsing across every gate).
from _paths import CORE_ROOT, PROJECT_ROOT, WORLD_DIR
from _skill_md import parse_front_matter
# Two-layer prose filter shared with signal-lifecycle-gate.py
# (rb-349, guard-319) — prevents false-positive matches inside SKILL.md
# comment lines and inline-backtick prose references.
from _prose_filter import is_prose_line, strip_prose_refs

SKILLS_DIR = PROJECT_ROOT / ".claude" / "skills"
FORGED_REGISTRY = Path(WORLD_DIR) / "forged-skills.yaml" if WORLD_DIR else None

# Canonical modes from CLAUDE.md "Mode System" table PLUS two framework-control
# escape hatches observed in start/stop/prime: "any" (runs in all modes,
# legitimate for /start before a mode is even bound) and "internal" (framework
# orchestrator skills not subject to mode gating). Expanding silently is the
# WRONG fix — these values should be explicitly documented when added.
# NOTE: This is a SUPERSET of session.py:47 VALID_MODES (the runtime triad
# {reader, assistant, autonomous}). "any" and "internal" appear ONLY in skill
# front-matter `minimum_mode:` fields — they are NEVER valid as runtime
# session/agent-mode values. When adding a new runtime mode, update both
# sets. Runtime source-of-truth: core/scripts/session.py:47.
VALID_MODES = {"reader", "assistant", "autonomous", "any", "internal"}
ALL_CHECKS = ("return_protocol", "minimum_mode_valid", "bash_scripts_exist",
              "skill_invocations_valid", "forged_triggers_present")

# Match `## Return Protocol` or `### Return Protocol` at any heading depth.
_RETURN_PROTO_RE = re.compile(r"^#+\s*Return Protocol\b", re.MULTILINE)

# Conservative literal matchers. Intentionally tight — a false positive here
# produces a FAIL on a perfectly-working reference, whereas a false negative
# just means we miss catching one broken reference form. Prefer misses.
_BASH_SCRIPT_RE = re.compile(
    r"bash\s+(core/scripts/[A-Za-z0-9_./-]+\.sh)", re.IGNORECASE
)
_WORLD_SCRIPT_RE = re.compile(
    r"bash\s+(world/scripts/[A-Za-z0-9_./-]+\.sh)", re.IGNORECASE
)
_PYTHON_SCRIPT_RE = re.compile(
    r"python3?\s+(core/scripts/[A-Za-z0-9_./-]+\.py)", re.IGNORECASE
)
# Match Skill(aspirations-verify), Skill('aspirations-verify'), Skill("aspirations-verify").
# CASE-SENSITIVE on the leading `S` (no re.IGNORECASE) — otherwise pseudocode
# like `infer_skill(sg)` or `my_skill(x)` produces false positives by matching
# the lowercase-s suffix. The canonical framework form is always capital-S.
# \b anchors at a word boundary so we never match mid-identifier.
_SKILL_INVOKE_RE = re.compile(
    r"\bSkill\(\s*['\"]?([a-z0-9][a-z0-9_-]*)['\"]?"
)


def _load_forged_skill_rows() -> dict:
    """Return {name: row} for every forged skill in world/forged-skills.yaml.

    Empty dict on any read/parse error (fail-open). The registry is authoritative
    per .claude/rules/forged-skill-resolution.md."""
    if not FORGED_REGISTRY or not FORGED_REGISTRY.is_file():
        return {}
    try:
        import yaml
        data = yaml.safe_load(FORGED_REGISTRY.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    skills = data.get("skills") if isinstance(data, dict) else None
    if not isinstance(skills, dict):
        return {}
    return skills


def _load_forged_skill_names() -> set:
    """Names only — the shape most callers here want."""
    return set(_load_forged_skill_rows().keys())


def _enumerate_skill_dirs() -> list:
    """All child directories of .claude/skills/ that contain a SKILL.md."""
    if not SKILLS_DIR.is_dir():
        return []
    out = []
    for child in sorted(SKILLS_DIR.iterdir()):
        if child.is_dir() and (child / "SKILL.md").is_file():
            out.append(child)
    return out


def _is_user_invocable(fm: dict) -> bool:
    """Return True if front matter explicitly marks user-invocable: true.

    The rule's 'Applies To' section treats absence of the field as
    non-user-invocable by default, EXCEPT for the three control skills
    (start, stop, verify-learning) which are named explicitly as user-invocable
    regardless of field presence. We honor the explicit exemption below via
    EXEMPT_BY_NAME — never via silent defaults.

    Only the hyphen spelling is recognized — it matches Claude Code's own
    spec (code.claude.com/docs/en/skills). Underscore is not a valid key.
    The enforcement loop in `check_invariants.py` asserts no SKILL.md uses
    the underscore form, so this reader never needs to fall back."""
    val = fm.get("user-invocable")
    if isinstance(val, bool):
        return val
    return False


# Per .claude/rules/return-protocol.md "User-invocable control skills":
# these produce chat responses inside a user turn and are explicitly exempt
# from the Return Protocol requirement even though their front matter lacks
# a user-invocable field.
EXEMPT_FROM_RETURN_PROTOCOL = {
    "start", "stop", "verify-learning", "open-questions",
    "priority-review", "backlog-report", "agent-completion-report",
}


def _check_return_protocol(skill_name: str, skill_path: Path,
                            fm: dict, body: str,
                            forged_names: set) -> list:
    """Invariant 1: non-user-invocable + forged skills require ## Return Protocol."""
    if skill_name in EXEMPT_FROM_RETURN_PROTOCOL:
        return []
    is_non_user = not _is_user_invocable(fm) or skill_name in forged_names
    # Non-user-invocable is the DEFAULT — only exempt if user-invocable: true
    # is explicitly declared AND the skill isn't forged.
    if _is_user_invocable(fm) and skill_name not in forged_names:
        return []
    if is_non_user and not _RETURN_PROTO_RE.search(body):
        return [{
            "skill": skill_name,
            "check": "return_protocol",
            "detail": "missing `## Return Protocol` section — "
                      "required for non-user-invocable + forged skills per "
                      ".claude/rules/return-protocol.md",
        }]
    return []


def _check_minimum_mode(skill_name: str, fm: dict) -> list:
    """Invariant 2: minimum_mode, if set, must be a valid mode."""
    mode = fm.get("minimum_mode") or fm.get("minimum-mode")
    if mode is None:
        return []  # Optional field; absence is allowed.
    if not isinstance(mode, str) or mode not in VALID_MODES:
        return [{
            "skill": skill_name,
            "check": "minimum_mode_valid",
            "detail": f"minimum_mode={mode!r}, must be one of {sorted(VALID_MODES)}",
        }]
    return []


def _check_bash_scripts(skill_name: str, body: str, path: Path) -> list:
    """Invariant 3: every bash/python script reference must resolve on disk.

    Uses the shared two-layer prose filter (rb-349, guard-319): skip whole
    comment lines, then strip inline-backtick spans in .md before regex
    matching. Without this, a commented-out reference to a nonexistent
    script path would produce a false-positive bash_scripts_exist violation.
    """
    violations = []
    # Build a single (pattern_label, rel_path) stream, line-by-line so the
    # prose filter can skip comment lines and strip backtick refs.
    refs = []
    for line in body.splitlines():
        if is_prose_line(line, path):
            continue
        effective = strip_prose_refs(line, path)
        for m in _BASH_SCRIPT_RE.finditer(effective):
            refs.append(("bash", m.group(1)))
        for m in _WORLD_SCRIPT_RE.finditer(effective):
            refs.append(("world-bash", m.group(1)))
        for m in _PYTHON_SCRIPT_RE.finditer(effective):
            refs.append(("python", m.group(1)))
    seen = set()
    for kind, rel in refs:
        if rel in seen:
            continue
        seen.add(rel)
        # Strip any trailing punctuation that slipped into the regex capture
        # (the SKILL.md may have `bash foo.sh` followed by backtick in prose).
        clean = rel.rstrip("`),;.")
        if kind == "world-bash":
            # world/ paths resolve relative to WORLD_DIR, not PROJECT_ROOT.
            if WORLD_DIR:
                abs_path = Path(WORLD_DIR) / Path(clean).relative_to("world")
            else:
                # No agent bound — can't verify, fail-open.
                continue
        else:
            abs_path = PROJECT_ROOT / clean
        if not abs_path.is_file():
            violations.append({
                "skill": skill_name,
                "check": "bash_scripts_exist",
                "detail": f"references {clean!r} ({kind}) but no file at {abs_path}",
            })
    return violations


def _check_skill_invocations(skill_name: str, body: str,
                              all_skill_names: set,
                              forged_names: set) -> list:
    """Invariant 4: Skill(<name>) must resolve to a real skill dir."""
    known = all_skill_names | forged_names
    violations = []
    seen = set()
    for m in _SKILL_INVOKE_RE.finditer(body):
        called = m.group(1).strip().lower()
        if called in seen:
            continue
        seen.add(called)
        if called not in known:
            # Allow a small stoplist of pseudo-names that the pattern might
            # legitimately grep up (e.g., "Skill(name)" in prose). These are
            # parameter names, not invocations.
            if called in {"name", "x", "skill", "skillname"}:
                continue
            violations.append({
                "skill": skill_name,
                "check": "skill_invocations_valid",
                "detail": f"invokes Skill({called!r}) but no such skill exists "
                          f"under .claude/skills/ or in forged-skills.yaml",
            })
    return violations


def _check_forged_triggers(forged_rows: dict, only_name=None) -> list:
    """Invariant 5: every forged-skill registry row must carry a usable trigger.

    `.claude/rules/forged-skill-resolution.md` resolves a natural-language action
    ("notify the user") to a forged skill by matching the registry's `triggers`.
    A row whose `triggers` is absent, empty, or holds only blanks can therefore
    never be resolved — the skill is unreachable no matter how good its SKILL.md
    is. g-115-3858 had to clean exactly that state by hand, and nothing prevented
    it recurring on the next forge: forge-skill/SKILL.md asks for a triggers list
    in prose, which is not a gate.

    THIS IS AN EMPTINESS FLOOR ONLY, AND DELIBERATELY NOT A WORD-COUNT ONE.
    g-115-4436 also asked for a ">=2 words after normalization" matchability
    floor reusing forged-skill-surface.py's `_norm` + `MIN_TRIGGER_WORDS`. Both
    symbols were RETIRED hours later by g-115-4475 (commit bf314aceb, "no lexical
    matcher clears the bar"), and `test_no_matching_symbols_remain` fails if
    either returns — so that half of the request is not merely stale, it is
    guarded against. It is also inverted by the retirement: the consumer is now
    an LLM rather than a lexical matcher, and a distinctive single token
    ("journalctl" for ssm-run) is a HIGH-precision trigger, not dead weight. A
    word-count threshold added here would rebuild the retired matcher one gate
    over, where its own mutation guard cannot see it.

    Registry-wide, not per-directory: forged skills are rows, and a row can exist
    with no `.claude/skills/<name>/` directory at all.
    """
    violations = []
    for name in sorted(forged_rows):
        if only_name is not None and name != only_name:
            continue
        row = forged_rows.get(name)
        if not isinstance(row, dict):
            continue
        trigs = row.get("triggers")
        if isinstance(trigs, list) and any(
                isinstance(t, str) and t.strip() for t in trigs):
            continue
        if trigs is None:
            detail = "has no `triggers` key"
        elif not isinstance(trigs, list):
            detail = f"`triggers` is {type(trigs).__name__}, not a list"
        elif not trigs:
            detail = "`triggers` is an empty list"
        else:
            detail = "`triggers` holds no non-blank string"
        violations.append({
            "skill": name,
            "check": "forged_triggers_present",
            "detail": (f"forged-skills.yaml row {detail} — unreachable by "
                       ".claude/rules/forged-skill-resolution.md, which routes "
                       "natural-language actions to forged skills via `triggers`"),
        })
    return violations


def _check_one_skill(skill_dir: Path, checks: set,
                      all_skill_names: set, forged_names: set) -> list:
    """Run the selected checks against one skill. Returns list of violation dicts."""
    skill_name = skill_dir.name
    skill_md = skill_dir / "SKILL.md"
    fm = parse_front_matter(skill_md)
    try:
        body = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        body = ""
    violations = []
    if "return_protocol" in checks:
        violations.extend(_check_return_protocol(skill_name, skill_md, fm, body, forged_names))
    if "minimum_mode_valid" in checks:
        violations.extend(_check_minimum_mode(skill_name, fm))
    if "bash_scripts_exist" in checks:
        violations.extend(_check_bash_scripts(skill_name, body, skill_md))
    if "skill_invocations_valid" in checks:
        violations.extend(_check_skill_invocations(skill_name, body, all_skill_names, forged_names))
    return violations


def main():
    ap = argparse.ArgumentParser(
        description="Skill-Structure Gate: verify SKILL.md invariants dynamically.",
    )
    scope = ap.add_mutually_exclusive_group(required=True)
    scope.add_argument("--skill", help="Skill directory name (no leading slash).")
    scope.add_argument("--all", action="store_true", help="Scan every skill.")
    ap.add_argument("--check", default=",".join(ALL_CHECKS),
                    help=f"Comma-separated checks to run. Default: all. Options: {ALL_CHECKS}")
    output_mode = ap.add_mutually_exclusive_group()
    output_mode.add_argument("--json", action="store_true", default=True,
                             help="Emit JSON to stdout (default).")
    output_mode.add_argument("--text", action="store_true",
                             help="Human-readable summary on stdout.")
    args = ap.parse_args()

    checks = {c.strip() for c in args.check.split(",") if c.strip()}
    unknown = checks - set(ALL_CHECKS)
    if unknown:
        print(f"[skill-structure-gate] error: unknown check(s): {sorted(unknown)}",
              file=sys.stderr)
        sys.exit(2)

    all_dirs = _enumerate_skill_dirs()
    all_names = {d.name for d in all_dirs}
    forged_rows = _load_forged_skill_rows()
    forged_names = set(forged_rows.keys())

    if args.skill:
        target = SKILLS_DIR / args.skill
        if not (target / "SKILL.md").is_file():
            print(f"[skill-structure-gate] error: no SKILL.md at {target}",
                  file=sys.stderr)
            sys.exit(2)
        targets = [target]
    else:
        targets = all_dirs

    violations = []
    for d in targets:
        violations.extend(_check_one_skill(d, checks, all_names, forged_names))

    # Registry-wide, so it runs ONCE rather than per skill dir. Under --skill it
    # narrows to that one row (which may not exist — a forged row needs no dir).
    if "forged_triggers_present" in checks:
        violations.extend(_check_forged_triggers(
            forged_rows, only_name=args.skill if args.skill else None))

    result = {
        "scope": "skill" if args.skill else "all",
        "target": args.skill if args.skill else f"{len(targets)} skills",
        "checks_run": sorted(checks),
        "skills_checked": len(targets),
        "violation_count": len(violations),
        "violations": violations,
        "would_block": bool(violations),
    }

    if args.text:
        # Human summary on stdout; JSON echoed to stderr for piping.
        print(f"skill-structure-gate: {len(targets)} skills, {len(violations)} violations")
        if violations:
            for v in violations:
                print(f"  [{v['check']}] {v['skill']}: {v['detail']}")
        print(json.dumps(result), file=sys.stderr)
    else:
        print(json.dumps(result))

    sys.exit(1 if violations else 0)


if __name__ == "__main__":
    main()
