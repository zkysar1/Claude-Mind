#!/usr/bin/env python3
"""E8: Decision Rule times_applied counter.

Increments `applied: N (date)` suffix on Decision Rule lines inside a tree
node's `## Decision Rules` section. Used by aspirations-execute Phase 4 (after
retrieval, when the agent cited a rule that informed the next step) and
reflect-maintain Step 1d (active-forgetting pass for dead rules).

Line format:

    Before first increment:
        - IF X THEN Y — source: g-NNN
    After:
        - IF X THEN Y — source: g-NNN — applied: 1 (2026-05-12)
    Subsequent:
        - IF X THEN Y — source: g-NNN — applied: 3 (2026-05-12)

Contract:

- Reads stdin JSON: {"rules": ["IF X THEN Y", "IF A THEN B"]}
  (just the IF-THEN body — the source/applied suffixes are ignored for matching)
- Matches by tokenized overlap with existing rule lines (>=70% — same threshold
  as decision-rules-append.py for consistency)
- Idempotent within a SINGLE invocation: two identical rule strings in the same
  payload increment the counter once (the second is treated as a duplicate
  match against the just-updated line)
- Writes back ONLY if at least one rule matched
- Exits non-zero if no matches (caller must know the rule actually exists)
"""

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

from _paths import PROJECT_ROOT  # noqa: E402

SECTION_HEADING = "## Decision Rules"
OVERLAP_THRESHOLD = 0.70
APPLIED_PATTERN = re.compile(r" — applied: (\d+) \(\d{4}-\d{2}-\d{2}\)$")


def tokenize(s):
    return set(re.findall(r"[a-z0-9]+", s.lower()))


def normalize_for_match(s):
    """Strip the `— applied: N (date)` suffix before similarity check.

    DO NOT REMOVE. Without this, repeated increments silently fail: the
    first call appends "applied: 1 (date)" which adds tokens; on the
    second call the suffix tokens drop token-overlap below 70% and the
    rule looks "different" from itself. The increment never lands and
    the counter sticks at 1 forever. The active-forgetting pass
    (reflect-maintain Step 2.55) then misclassifies the rule as dead.
    """
    return APPLIED_PATTERN.sub("", s)


def rule_similar(a, b):
    a_tok = tokenize(normalize_for_match(a))
    b_tok = tokenize(normalize_for_match(b))
    if not a_tok or not b_tok:
        return False
    overlap = len(a_tok & b_tok) / max(len(a_tok), len(b_tok))
    return overlap >= OVERLAP_THRESHOLD


def find_section(lines):
    """Return (section_start, section_end) line indices for ## Decision Rules,
    or (None, None) if absent. section_end is exclusive."""
    section_start = None
    section_end = None
    for i, line in enumerate(lines):
        if line.strip() == SECTION_HEADING:
            section_start = i
            continue
        if section_start is not None and line.startswith("## ") and i > section_start:
            section_end = i
            break
    if section_start is not None and section_end is None:
        section_end = len(lines)
    return section_start, section_end


def increment_line(line, today_iso):
    """Return the updated rule line with applied counter incremented.
    Idempotent on the suffix slot — overwrites prior count if present."""
    m = APPLIED_PATTERN.search(line)
    if m:
        old = int(m.group(1))
        new_suffix = f" — applied: {old + 1} ({today_iso})"
        return APPLIED_PATTERN.sub(new_suffix, line)
    return line.rstrip() + f" — applied: 1 ({today_iso})"


def main():
    parser = argparse.ArgumentParser(
        description="E8 decision-rule applied-counter increment."
    )
    parser.add_argument("--node-path", required=True,
                        help="Path to the tree node .md file (relative to PROJECT_ROOT or absolute)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    raw = ""
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()

    if not raw:
        print("ERROR: empty stdin — pass {\"rules\": [...]}", file=sys.stderr)
        sys.exit(1)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: invalid stdin JSON: {e}", file=sys.stderr)
        sys.exit(1)

    rules_in = payload.get("rules", [])
    if not isinstance(rules_in, list) or not rules_in:
        print("ERROR: payload must be {\"rules\": [\"IF X THEN Y\", ...]}",
              file=sys.stderr)
        sys.exit(1)

    node_path = Path(args.node_path)
    if not node_path.is_absolute():
        node_path = PROJECT_ROOT / node_path
    if not node_path.exists():
        print(f"ERROR: node path does not exist: {node_path}", file=sys.stderr)
        sys.exit(1)

    body = node_path.read_text(encoding="utf-8")
    lines = body.splitlines(keepends=False)
    section_start, section_end = find_section(lines)
    if section_start is None:
        print(f"ERROR: no '## Decision Rules' section in {node_path}",
              file=sys.stderr)
        sys.exit(1)

    today_iso = date.today().isoformat()
    matched_indices = set()
    matched_count = 0

    for rule_text in rules_in:
        if not isinstance(rule_text, str) or not rule_text.strip():
            continue
        # Find first matching rule line we haven't already incremented this call
        for i in range(section_start + 1, section_end):
            if i in matched_indices:
                continue
            line = lines[i]
            if not line.strip().startswith("- IF "):
                continue
            if rule_similar(rule_text, line):
                lines[i] = increment_line(line, today_iso)
                matched_indices.add(i)
                matched_count += 1
                print(f"▸ APPLIED: {lines[i]}")
                break

    if matched_count == 0:
        print(f"decision_rules_incremented=0 matched_count=0", file=sys.stderr)
        sys.exit(2)

    if args.dry_run:
        print(f"▸ DRY-RUN: would increment {matched_count} rule(s) in {args.node_path}")
        print(f"decision_rules_incremented={matched_count}")
        return

    new_body = "\n".join(lines) + ("\n" if body.endswith("\n") else "")
    node_path.write_text(new_body, encoding="utf-8")
    print(f"decision_rules_incremented={matched_count}")


if __name__ == "__main__":
    main()
