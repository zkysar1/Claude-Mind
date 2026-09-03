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
import datetime
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# : force utf-8 on stdin/stdout/stderr (covers Windows cp1252 fallback
# when callers bypass the _platform.sh PYTHONIOENCODING=utf-8 shim).
from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

from _paths import PROJECT_ROOT, AGENT_DIR  # noqa: E402
from tree import resolve_node_path  # noqa: E402

SECTION_HEADING = "## Decision Rules"
OVERLAP_THRESHOLD = 0.70


def _stamp_front_matter(node_path):
    """Stamp the node's front matter after a direct write ().

    THE PRODUCER/CONSUMER CONTRACT THIS SATISFIES. tree-edit-since.py decides
    whether a modified node counts as THIS session's encoding by matching
    `^\\s*session:\\s*(\\S+)\\s*$` against the node's front matter. It fails
    OPEN only for a node carrying no session stamp at all; a node already
    stamped by a DIFFERENT session — i.e. most nodes in a multi-agent fleet,
    where agents append to each other's nodes constantly — is otherwise
    permanently unattributable to any later appender. The visible symptom was
    iteration-close printing "--tree-updated passed but no tree-file change
    detected ... IGNORING flag" on an iteration that genuinely encoded, which
    silently under-credits real encoding work and lets the
    tree-encoding-drift-gate force-fire as though nothing had been written.

    WHY THE STAMP WAS MISSING HERE AND NOWHERE ELSE. The canonical stamper is
    tree-front-matter-sync.py, wired in .claude/settings.json as a PostToolUse
    hook on Write|Edit|MultiEdit. This script writes the node with a direct
    Python `write_text()`, which is not a tool call, so no hook has ever fired
    for it. The consumer was never wrong and must NOT be widened — its
    docstring is correct that an unread node must not be credited, and
    fail-opening it would credit genuinely foreign edits.

    So this CALLS the one canonical stamper rather than re-implementing the
    stamping rules (guard-2676): a second copy of the front-matter contract
    would drift from the hook's copy the first time either changed, silently.

    FAIL-OPEN, DELIBERATELY. Appending the rule is this script's job; stamping
    is a courtesy to a downstream consumer. Any failure here — no virtual path
    derivable, stamper missing, non-zero exit, timeout — must leave the
    already-written rule intact and must not fail the caller.
    """
    try:
        parts = node_path.as_posix().split("/knowledge/tree/")
        if len(parts) < 2:
            return  # not a tree node — nothing the consumer would look at
        virtual = "world/knowledge/tree/" + parts[-1]
        stamper = Path(__file__).parent / "tree-front-matter-sync.py"
        if not stamper.is_file():
            return
        import subprocess  # local: keeps the no-rule/dry-run paths import-free
        subprocess.run(
            [sys.executable, str(stamper),
             "--file", str(node_path), "--virtual-path", virtual],
            capture_output=True, timeout=20, check=False,
        )
    except Exception:
        return


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


# Mirrors core/scripts/tree-edit-since.py::_SESSION_RE exactly -- the consumer
# whose predicate this writer must satisfy. It is MULTILINE and takes the FIRST
# match, so it reads the `session:` nested under `last_update_trigger:`, which
# is where the ordinary tree-edit path puts it. If these two regexes ever
# diverge, this stamp stops counting and the failure is SILENT in the
# under-crediting direction -- which is the whole defect being fixed here.
_FM_SESSION_RE = re.compile(r"^(\s*)session:\s*\S+\s*$", re.MULTILINE)
_FM_LAST_UPDATED_RE = re.compile(r"^(\s*)last_updated:\s*.*$", re.MULTILINE)


def stamp_session(body, sid, today=None):
    """Refresh the node's front-matter `session` + `last_updated` stamps.

    WHY THE WRITER DOES THIS (g-115-5831). `tree-edit-since.py` decides whether
    a modified node counts as THIS session's encoding by reading the node's
    `session:` stamp. This script appends a rule and touches no front matter, so
    a node already stamped by ANY other session could never be attributed to the
    appending one -- and in a multi-agent fleet where agents append to each
    other's nodes constantly, that is the common case, not an edge. The effect is
    silent and one-directional: real encoding goes uncredited, `--tree-updated`
    is ignored, and the tree-encoding-drift gate force-fires as though nothing
    was written.

    THE FIX IS AT THE WRITER, DELIBERATELY. Fail-opening the consumer would be
    the easy change and the wrong one: its docstring is right that an unread or
    foreign node must not be credited, and widening it would credit genuinely
    foreign edits. A producer that cannot satisfy its consumer's predicate is
    the producer's bug.

    Only the front-matter block is touched, never the body -- a `session:` line
    can legitimately appear in prose, and rewriting that would corrupt content.
    A node with NO session stamp is left alone on purpose: the consumer
    fail-opens for exactly that case, so the append is already credited and
    adding a key would be a change with no reader.
    """
    if not sid or not body.startswith("---"):
        return body
    end = body.find("\n---", 3)
    if end == -1:            # unterminated front matter -- do not guess
        return body
    fm, rest = body[:end], body[end:]
    if not _FM_SESSION_RE.search(fm):
        return body
    today = today or datetime.date.today().isoformat()
    fm = _FM_SESSION_RE.sub(
        lambda m: "%ssession: %s" % (m.group(1), sid), fm, count=1)
    fm = _FM_LAST_UPDATED_RE.sub(
        lambda m: "%slast_updated: '%s'" % (m.group(1), today), fm, count=1)
    return fm + rest


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

    # Virtual `world/knowledge/tree/...` and bare `<cat>/<node>.md` forms resolve
    # to WORLD_DIR (external worlds); repo-relative and absolute forms are unchanged.
    node_path = resolve_node_path(args.node_path)
    if not node_path.exists():
        print(f"ERROR: node path does not exist: {node_path} (from --node-path {args.node_path!r})",
              file=sys.stderr)
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
    # Claim the edit for THIS session so tree-edit-since.py can attribute it
    # (). Only fires when the node already carries a stamp; see
    # stamp_session for why the no-stamp case is deliberately left alone.
    new_body = stamp_session(new_body, os.environ.get("MIND_SID", "").strip())
    node_path.write_text(new_body, encoding="utf-8")
    _stamp_front_matter(node_path)
    print(f"decision_rules_count={len(existing_rules) + len(appended)} "
          f"appended={len(appended)} skipped={len(skipped)}")


if __name__ == "__main__":
    main()
