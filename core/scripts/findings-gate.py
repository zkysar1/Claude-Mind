#!/usr/bin/env python3
"""Phase 8.5 Actionable Findings Gate — bash-enforced keyword scan + dedup + goal creation.

Replaces the LLM-assembled keyword scan described in aspirations-state-update/SKILL.md
Step 8.5 with a single script call. The LLM residue that remains is ONLY the
investigation-override binary: when goal.title starts with "Investigate:" AND the
keyword scan produced zero signals, the LLM decides whether the finding is
informational (skip) or action-requiring (pass --investigation-needs-action).

Exit codes:
  0  — ran the gate (signals detected and dispatched, OR none detected); stdout
       includes one line per signal and a trailing `findings_count=N created=M`.
  2  — reserved for future use (no current path returns 2).
  1  — usage / argument error.

The `findings_count` line is stable machine-parseable output for downstream
audits (state-update-audit.sh run-all reads it).
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Unicode-safe stdout/stderr on Windows (cp1252).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# We need PROJECT_ROOT + AGENT_DIR to find aspirations-compact.json. Reuse
# the same _paths shim used across the rest of core/scripts/.
sys.path.insert(0, str(Path(__file__).parent))
from _paths import PROJECT_ROOT, AGENT_DIR, WORLD_DIR  # noqa: E402
from _gate_log import log as _gate_log  # noqa: E402  # gate-firing telemetry ()
from _runtime_bash import bash_cmd  # noqa: E402  # : Windows-safe bash resolution

RESOLUTION_SUPPRESSION_CHARS = 50  # window after a match to check for resolution language

# Four structural signal patterns. Each is (name, match_pattern, resolution_filter_pattern).
# match_pattern is compiled case-insensitive. resolution_filter_pattern is also
# case-insensitive and checked in the RESOLUTION_SUPPRESSION_CHARS window
# immediately after the match.
SIGNAL_PATTERNS = [
    (
        "root_cause",
        re.compile(
            r"\b(root cause|caused by|due to|because of|stems from)\b[^.!?\n]{0,200}",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(fixed|resolved|applied|addressed|patched|corrected|updated|removed)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "bug_identified",
        re.compile(
            r"\b(bug|defect|mismatch|incorrect|wrong|broken)\b\s+(in|at|of)\b[^.!?\n]{0,200}",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(fixed|resolved|patched|corrected)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "proposed_fix",
        re.compile(
            r"\b(fix by|should be changed|needs to be|replace with|update to)\b[^.!?\n]{0,200}",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(done|completed|applied|implemented|changed|updated)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "unimplemented_action",
        re.compile(
            r"\b(needs|requires|must|should)\s+(to be|updating|fixing|adding|removing)\b[^.!?\n]{0,200}",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(done|completed|applied|implemented|resolved)\b",
            re.IGNORECASE,
        ),
    ),
]


def scan_signals(insight_text):
    """Return list of {type, match} dicts for signals present in insight_text.

    Each signal type is emitted at most once — the first non-suppressed match
    wins, then we move to the next pattern.
    """
    signals = []
    for name, match_re, resolution_re in SIGNAL_PATTERNS:
        for m in match_re.finditer(insight_text):
            # DO NOT SIMPLIFY: the resolution-language filter MUST check
            # BOTH the match content AND the 50-char window after. Checking
            # only the window misses resolution language inside the greedy
            # [^.!?\n]{0,200} span (e.g., "root cause was X, fixed by Y" —
            # "fixed" lives inside the match, not after it). This was the
            # bug fixed during initial wrapper implementation. If you
            # tighten the match regex to non-greedy, re-verify the dual
            # check is still needed before removing.
            match_text = m.group(0)
            if resolution_re.search(match_text):
                # Resolution language INSIDE the match span — pattern fired but
                # is already-resolved. Telemetry: noop with decision_path so the
                # retirement evaluator can distinguish suppressed-by-resolution
                # from never-matched ().
                _gate_log(
                    "findings-gate",
                    "noop",
                    trigger_matched=f"{name}:resolution-in-match",
                    caller="findings-gate.py:scan_signals",
                    extra={"decision_path": "resolution-suppression-in-match"},
                )
                continue
            window = insight_text[m.end():m.end() + RESOLUTION_SUPPRESSION_CHARS]
            if resolution_re.search(window):
                _gate_log(
                    "findings-gate",
                    "noop",
                    trigger_matched=f"{name}:resolution-in-window",
                    caller="findings-gate.py:scan_signals",
                    extra={"decision_path": "resolution-suppression-in-window"},
                )
                continue
            # Extract a 50-char reference for the goal title.
            reference = match_text.strip()
            if len(reference) > 50:
                reference = reference[:50].rstrip() + "…"
            signals.append({"type": name, "match": reference})
            # Pattern matched + resolution filter passed → this becomes a
            # child goal. "block" in telemetry semantics = gate fired and
            # caller acted on it ().
            _gate_log(
                "findings-gate",
                "block",
                trigger_matched=name,
                caller="findings-gate.py:scan_signals",
                extra={"decision_path": "signal-found", "match_ref": reference},
            )
            break
    return signals


def _title_similar(a, b):
    """Rough similarity test for dedup. Titles match if one contains the other's
    core content (case-insensitive substring test on the match text)."""
    a_low = a.lower().strip()
    b_low = b.lower().strip()
    if not a_low or not b_low:
        return False
    if a_low == b_low:
        return True
    # If either is a substring of the other (minus the leading "Unblock: "/"Idea: "),
    # treat as duplicate.
    def strip_prefix(s):
        for pfx in ("unblock: ", "idea: ", "investigate: "):
            if s.startswith(pfx):
                return s[len(pfx):]
        return s
    a_core = strip_prefix(a_low)
    b_core = strip_prefix(b_low)
    if not a_core or not b_core:
        return False
    # Token-overlap dedup: >70% of tokens shared counts as duplicate
    a_tok = set(re.findall(r"[a-z0-9]+", a_core))
    b_tok = set(re.findall(r"[a-z0-9]+", b_core))
    if not a_tok or not b_tok:
        return False
    overlap = len(a_tok & b_tok) / max(len(a_tok), len(b_tok))
    return overlap >= 0.70


def load_dedup_titles(aspiration_id):
    """Build the dedup title set per SKILL.md Step 8.5:
      - All aspirations: goals with status pending/in-progress
      - Parent aspiration only: goals with status completed (sibling completed)
    Reads aspirations-compact.json via load-aspirations-compact.sh to get a fresh snapshot.
    """
    if AGENT_DIR is None:
        return []
    compact_path = AGENT_DIR / "session" / "aspirations-compact.json"
    # Regenerate compact if loader is available (best-effort; scanner still works
    # on stale data if the loader fails).
    loader = PROJECT_ROOT / "core" / "scripts" / "load-aspirations-compact.sh"
    if loader.exists():
        try:
            subprocess.run(
                bash_cmd(loader),
                env={**os.environ, "MIND_AGENT": os.environ.get("MIND_AGENT", "")},
                capture_output=True,
                timeout=10,
                check=False,
            )
        except Exception:
            pass
    if not compact_path.exists():
        return []
    try:
        with open(compact_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    # compact may be a list of aspirations OR a dict — handle both shapes.
    aspirations = data if isinstance(data, list) else data.get("aspirations", [])
    titles = []
    for asp in aspirations:
        if not isinstance(asp, dict):
            continue
        asp_id = asp.get("id")
        goals = asp.get("goals", []) or []
        for g in goals:
            if not isinstance(g, dict):
                continue
            title = g.get("title") or ""
            status = g.get("status") or ""
            if not title:
                continue
            if status in ("pending", "in-progress"):
                titles.append(title)
            elif status == "completed" and asp_id == aspiration_id:
                titles.append(title)
    return titles


def make_child_goal(signal, source_goal, source_category, insight_text):
    """Assemble the child-goal JSON per SKILL.md Step 8.5."""
    high_signal_types = {"root_cause", "bug_identified", "investigation_finding"}
    if signal["type"] in high_signal_types:
        title = f"Unblock: Fix {signal['match']}"
        priority = "HIGH"
        origin_prefix = "unblock:"
    else:
        title = f"Idea: {signal['match']}"
        priority = "MEDIUM"
        origin_prefix = "idea:"
    return {
        "title": title,
        "status": "pending",
        "priority": priority,
        "skill": None,
        "participants": ["agent"],
        "category": source_category,
        "description": (
            f"Found during {source_goal}: {signal['match']}\n\n"
            f"Source: {insight_text}\n\n"
            f"Discovered by: Step 8.5 Actionable Findings Gate"
        ),
        "verification": {
            "outcomes": ["Finding addressed — fix applied or determined not actionable with reasoning"],
            "checks": [],
        },
        "discovered_by": source_goal,
        "discovery_type": signal["type"],
        "origin_signal": f"{origin_prefix}{source_goal}",
    }


def dispatch_goal(goal_json, aspiration_id, source):
    """Invoke aspirations-add-goal.sh via subprocess. Returns True on success."""
    script = PROJECT_ROOT / "core" / "scripts" / "aspirations-add-goal.sh"
    if not script.exists():
        print(f"ERROR: aspirations-add-goal.sh not found at {script}", file=sys.stderr)
        return False
    cmd = bash_cmd(script, "--source", source, aspiration_id)
    try:
        result = subprocess.run(
            cmd,
            input=json.dumps(goal_json),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception as e:
        print(f"ERROR: aspirations-add-goal.sh subprocess failed: {e}", file=sys.stderr)
        return False
    if result.returncode != 0:
        print(f"ERROR: aspirations-add-goal.sh exit {result.returncode}: {result.stderr.strip()}",
              file=sys.stderr)
        return False
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Phase 8.5 Actionable Findings Gate — keyword scan + goal creation."
    )
    parser.add_argument("--goal", required=True, help="Source goal ID (discovering goal)")
    parser.add_argument("--insight-file", required=True,
                        help="Path to file containing the just-written insight text")
    parser.add_argument("--aspiration", required=True,
                        help="Parent aspiration ID (where child goals will be filed)")
    parser.add_argument("--category", required=True,
                        help="Category for child goals (usually source goal's category)")
    parser.add_argument("--source", default="world", choices=["world", "agent"],
                        help="Aspiration source for aspirations-add-goal.sh (default: world)")
    parser.add_argument("--is-investigation", action="store_true",
                        help="Set when the source goal's title starts with 'Investigate:'. "
                             "Enables the investigation-override path.")
    parser.add_argument("--investigation-needs-action", action="store_true",
                        help="LLM residue: when is-investigation AND keyword scan produced no "
                             "signals, caller passes this flag to convert the finding into an "
                             "investigation_finding signal. Omit to skip (informational).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scan and report but do NOT create goals (for testing)")
    args = parser.parse_args()

    insight_path = Path(args.insight_file)
    if not insight_path.is_absolute():
        insight_path = PROJECT_ROOT / insight_path
    if not insight_path.exists():
        print(f"ERROR: insight-file does not exist: {insight_path}", file=sys.stderr)
        sys.exit(1)
    insight_text = insight_path.read_text(encoding="utf-8").strip()
    if not insight_text:
        print("findings_count=0 created=0 reason=empty_insight")
        return

    # Step 1: keyword scan.
    signals = scan_signals(insight_text)

    # Step 2: investigation override.
    if args.is_investigation and len(signals) == 0 and args.investigation_needs_action:
        signals.append({
            "type": "investigation_finding",
            "match": insight_text[:50].strip() + ("…" if len(insight_text) > 50 else ""),
        })
        _gate_log(
            "findings-gate",
            "block",
            trigger_matched="investigation_finding",
            caller="findings-gate.py:main",
            extra={"decision_path": "investigation-override"},
        )

    if len(signals) == 0:
        print("▸ Step 8.5: No actionable signals — passed")
        print("findings_count=0 created=0")
        # Whole-scan noop: zero signals after pattern scan + investigation
        # override. Counts toward invocation total but not as "fired"
        # (, retirement-evaluator semantics).
        _gate_log(
            "findings-gate",
            "noop",
            trigger_matched="no-actionable-signals",
            caller="findings-gate.py:main",
            extra={"decision_path": "scan-clean"},
        )
        return

    # Step 3: dedup.
    dedup_titles = load_dedup_titles(args.aspiration)

    created = 0
    for signal in signals:
        goal_json = make_child_goal(signal, args.goal, args.category, insight_text)
        if any(_title_similar(goal_json["title"], t) for t in dedup_titles):
            print(f"▸ Step 8.5: {signal['type']} detected but goal already exists — skipped")
            continue
        if args.dry_run:
            print(f"▸ DRY-RUN: would create {goal_json['title']} in {args.aspiration}")
            created += 1
            continue
        if dispatch_goal(goal_json, args.aspiration, args.source):
            print(f"▸ FINDINGS GATE: Created {goal_json['title']} in {args.aspiration} from {args.goal}")
            created += 1

    # Final machine-parseable summary line (state-update-audit reads this).
    print(f"findings_count={len(signals)} created={created}")


if __name__ == "__main__":
    main()
