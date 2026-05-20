#!/usr/bin/env python3
"""Phase 8e bash-enforced decision-rules append.

Appends to or creates the `## Decision Rules` section of a tree node .md file.
Consumes stdin JSON — either a single `{if, then}` object or an array of them.

Contract (matches core/config/conventions/decision-rules.md):

- Format: `- IF {observable condition} THEN {specific action} — source: {goal-id}`
- Dedup: token-overlap against existing rules in the same node (>=70% overlap → skip)
- Empty stdin (LLM signaled "no rule emerged"): exit 0, emit stderr "no_rule_passed"
  for the staleness check to count toward drift-detection
- Non-empty stdin: emit one line per rule: `▸ DECISION RULE: {appended|skipped}: {title}`
- Final line: `decision_rules_count=<total> appended=<N> skipped=<M>`
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# : force utf-8 on stdin/stdout/stderr (covers Windows cp1252 fallback
# when callers bypass the _platform.sh PYTHONIOENCODING=utf-8 shim).
from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

from _paths import PROJECT_ROOT, AGENT_DIR  # noqa: E402

SECTION_HEADING = "## Decision Rules"
OVERLAP_THRESHOLD = 0.70


def bump_last_call_marker():
    """Write an ISO timestamp to <agent>/session/decision-rules-last-call.
    Called on EVERY invocation (including empty stdin and dry-run) so the
    staleness backstop (decision-rules-staleness.sh) can distinguish
    "wrapper never invoked" (drift) from "invoked but no rule emerged"
    (legitimate). Best-effort — failure here must not break the call."""
    if AGENT_DIR is None:
        return
    try:
        from datetime import datetime
        marker = AGENT_DIR / "session" / "decision-rules-last-call"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            datetime.now().isoformat(timespec="seconds"),
            encoding="utf-8",
        )
    except Exception:
        pass


def tokenize(s):
    return set(re.findall(r"[a-z0-9]+", s.lower()))


def rule_similar(a, b):
    """Token-overlap similarity — >= OVERLAP_THRESHOLD → duplicate."""
    a_tok = tokenize(a)
    b_tok = tokenize(b)
    if not a_tok or not b_tok:
        return False
    overlap = len(a_tok & b_tok) / max(len(a_tok), len(b_tok))
    return overlap >= OVERLAP_THRESHOLD


def extract_existing_rules(body):
    """Return list of existing `- IF ... THEN ...` rule lines inside the
    `## Decision Rules` section. Empty list if the section doesn't exist."""
    rules = []
    in_section = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped == SECTION_HEADING:
            in_section = True
            continue
        if in_section and stripped.startswith("## "):
            break  # next section — stop
        if in_section and stripped.startswith("- IF "):
            rules.append(stripped)
    return rules


def format_rule(rule, goal_id):
    if_clause = rule.get("if", "").strip()
    then_clause = rule.get("then", "").strip()
    if not if_clause or not then_clause:
        return None
    return f"- IF {if_clause} THEN {then_clause} — source: {goal_id}"


def insert_rules(body, new_rules):
    """Insert new_rules (list of strings) into body.

    If `## Decision Rules` exists: append at end of that section (before next `## `).
    Else: append the whole section at end of body.
    """
    lines = body.splitlines(keepends=False)
    section_start = None
    section_end = None
    for i, line in enumerate(lines):
        if line.strip() == SECTION_HEADING:
            section_start = i
            continue
        if section_start is not None and line.startswith("## ") and i > section_start:
            section_end = i
            break
    if section_start is None:
        # Create new section at end
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(SECTION_HEADING)
        lines.append("")
        for r in new_rules:
            lines.append(r)
        lines.append("")
    else:
        if section_end is None:
            section_end = len(lines)
        # Insert rules just before section_end. Keep a trailing blank line.
        insert_at = section_end
        # Walk back over blank lines so we insert just after the last non-blank rule.
        while insert_at - 1 > section_start and not lines[insert_at - 1].strip():
            insert_at -= 1
        new_block = list(new_rules)
        for idx, r in enumerate(new_block):
            lines.insert(insert_at + idx, r)
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(
        description="Phase 8e Decision Rules append — dedup + insert."
    )
    parser.add_argument("--goal", required=True, help="Source goal ID")
    parser.add_argument(
        "--node-path", required=True,
        help="Path to the tree node .md file (relative to PROJECT_ROOT or absolute)"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Scan and report but do NOT write (for testing)")
    args = parser.parse_args()

    # DO NOT MOVE below the stdin branch. Empty-stdin calls (legitimate "no
    # rule emerged" signal from the LLM) MUST bump this marker, otherwise
    # decision-rules-staleness.sh false-positives every session where the
    # LLM responsibly signalled no-rule N times in a row. Parity with
    # experience-add.sh's staleness-check.sh pairing: the marker proves the
    # wrapper was invoked, orthogonal to whether any append happened.
    bump_last_call_marker()

    # Read stdin — either empty (no-rule passed), a single object, or an array.
    raw = ""
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()

    if not raw:
        # LLM signaled "no rule emerged" — this is legitimate.
        # Emit a staleness-tracking signal but exit cleanly.
        print("decision_rules_count=0 appended=0 skipped=0 reason=no_rule_passed")
        print("STALENESS: no_rule_passed (Phase 8e signalled no-rule for goal {})".format(args.goal),
              file=sys.stderr)
        return

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: invalid stdin JSON: {e}", file=sys.stderr)
        sys.exit(1)

    if isinstance(payload, dict):
        rules_in = [payload]
    elif isinstance(payload, list):
        rules_in = payload
    else:
        print("ERROR: stdin JSON must be an object or array", file=sys.stderr)
        sys.exit(1)

    node_path = Path(args.node_path)
    if not node_path.is_absolute():
        node_path = PROJECT_ROOT / node_path
    if not node_path.exists():
        print(f"ERROR: node path does not exist: {node_path}", file=sys.stderr)
        sys.exit(1)

    body = node_path.read_text(encoding="utf-8")
    existing_rules = extract_existing_rules(body)

    appended = []
    skipped = []
    for rule in rules_in:
        formatted = format_rule(rule, args.goal)
        if formatted is None:
            print(f"▸ DECISION RULE: skipped (missing if/then): {json.dumps(rule)}",
                  file=sys.stderr)
            continue
        # Dedup against existing rules AND against rules we're about to append
        # (if the caller passed two near-duplicates in one call).
        candidates = existing_rules + appended
        if any(rule_similar(formatted, r) for r in candidates):
            print(f"▸ DECISION RULE: skipped (duplicate): {formatted}")
            skipped.append(formatted)
            continue
        appended.append(formatted)
        print(f"▸ DECISION RULE: appended: {formatted}")

    if not appended:
        print(f"decision_rules_count={len(existing_rules)} appended=0 skipped={len(skipped)}")
        return

    if args.dry_run:
        print(f"▸ DRY-RUN: would write {len(appended)} rule(s) to {args.node_path}")
        print(f"decision_rules_count={len(existing_rules) + len(appended)} "
              f"appended={len(appended)} skipped={len(skipped)}")
        return

    new_body = insert_rules(body, appended)
    node_path.write_text(new_body, encoding="utf-8")
    print(f"decision_rules_count={len(existing_rules) + len(appended)} "
          f"appended={len(appended)} skipped={len(skipped)}")


if __name__ == "__main__":
    main()
