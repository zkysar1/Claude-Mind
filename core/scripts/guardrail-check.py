#!/usr/bin/env python3
"""Guardrail matching engine — deterministic keyword-based guardrail selection.

Given a context (infrastructure/testing/local/any), outcome (succeeded/failed/any),
and phase (post-execution/pre-selection), returns guardrails whose text matches.
This replaces the LLM's manual "read all guardrails and decide which apply" step.

Output is JSON with matched guardrails and action hints extracted from rule text.
Side effect: increments utilization.times_active on matched, times_skipped on
unmatched (unless --dry-run).

All JSONL I/O goes through reasoning-bank.py (the shared guardrails data layer).
"""

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

# Ensure stdout/stderr handle unicode on all platforms (Windows cp1252 fix)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Import reasoning-bank.py (hyphenated name requires importlib)
# This is the authoritative data layer for guardrails JSONL I/O.
_rb_spec = importlib.util.spec_from_file_location(
    "reasoning_bank",
    Path(__file__).resolve().parent / "reasoning-bank.py",
)
_rb = importlib.util.module_from_spec(_rb_spec)
_rb_spec.loader.exec_module(_rb)

GUARD_PATH = _rb.GUARD_PATH


# ---------------------------------------------------------------------------
# Keyword sets for matching
# These keywords determine which guardrails fire at each phase.
# If a guardrail is not being found, add its distinctive words here
# OR add a matching tag to the guardrail itself (tags are searchable).
# ---------------------------------------------------------------------------

INFRASTRUCTURE_KEYWORDS = [
    "infrastructure", "server", "deploy", "cloud",
    "api", "database", "ssh", "connectivity",
    "health", "error", "timeout", "outage",
    "error emails",
]

PHASE_POST_EXECUTION_KEYWORDS = [
    "post-execution", "after any", "just completed",
    "after goal", "success or failure",
    "after.*infrastructure", "goal.*completed",
    "goal involved", "goal execution",
    "unavailab", "recovery", "after.*skill.*fail",
]

PHASE_PRE_SELECTION_KEYWORDS = [
    "pre-selection", "before goal selection",
    "before selecting", "before choosing",
    "before each goal",
    "about to skip", "about to block",
    "deprioritiz", "considering.*skip",
    "before.*block", "during goal selection",
]

OUTCOME_SUCCEEDED_KEYWORDS = [
    "success", "regardless", "success or failure",
    "succeeded", "successful",
]

OUTCOME_FAILED_KEYWORDS = [
    "fail", "error", "unavailab", "refused",
    "timeout", "unexpected", "status code",
]

TESTING_KEYWORDS = [
    "test", "testing", "unit test", "integration test",
    "test creation", "test review", "coverage",
    "integration path", "event bus", "verification",
]

# Extracts script commands from rule text (e.g., "world/scripts/domain-check.sh check --since 30").
# Captures: optional world/scripts/ prefix + script name + optional subcommand + optional --flag value.
ACTION_HINT_RE = re.compile(
    r'((?:world/scripts/)?(?:[\w-]+\.sh)'
    r'(?:\s+(?:check|status|has|value|check-all|stale|read)'  # subcommand
    r'(?:\s+--\w+\s+\d+)?)?)',  # optional --flag value
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Searchable text extraction
# ---------------------------------------------------------------------------

def get_searchable_text(guard):
    """Combine all text fields of a guardrail into one lowercase string."""
    parts = [
        guard.get("rule", ""),
        guard.get("trigger_condition", ""),
        guard.get("category", ""),
    ]

    when = guard.get("when_to_use")
    if isinstance(when, dict):
        conditions = when.get("conditions", [])
        if isinstance(conditions, list):
            parts.extend(conditions)
        wcat = when.get("category", "")
        if wcat:
            parts.append(wcat)

    tags = guard.get("tags", [])
    if isinstance(tags, list):
        parts.extend(tags)

    return " ".join(str(p) for p in parts).lower()


# ---------------------------------------------------------------------------
# Matching logic
# ---------------------------------------------------------------------------

def text_matches_keywords(text, keywords):
    """Check if text matches ANY keyword (supports regex patterns)."""
    for kw in keywords:
        if re.search(kw, text, re.IGNORECASE):
            return True
    return False


def matches_context(text, context):
    if context == "any":
        return True
    if context == "infrastructure":
        return text_matches_keywords(text, INFRASTRUCTURE_KEYWORDS)
    if context == "testing":
        return text_matches_keywords(text, TESTING_KEYWORDS)
    if context == "local":
        return not text_matches_keywords(text, INFRASTRUCTURE_KEYWORDS)
    return True


def matches_phase(text, phase):
    if phase is None:
        return True
    if phase == "post-execution":
        return text_matches_keywords(text, PHASE_POST_EXECUTION_KEYWORDS)
    if phase == "pre-selection":
        return text_matches_keywords(text, PHASE_PRE_SELECTION_KEYWORDS)
    return True


def matches_outcome(text, outcome):
    if outcome is None or outcome == "any":
        return True
    if outcome == "succeeded":
        return text_matches_keywords(text, OUTCOME_SUCCEEDED_KEYWORDS)
    if outcome == "failed":
        return text_matches_keywords(text, OUTCOME_FAILED_KEYWORDS)
    return True


def extract_action_hint(rule_text):
    """Extract recognizable script commands from rule text.

    Guardrail text should use full paths: 'scripts/infra-health.sh' for
    framework scripts, 'world/scripts/my-check.sh' for domain scripts.
    No auto-prefix magic — the path in the text is the path that runs.
    """
    match = ACTION_HINT_RE.search(rule_text)
    if match:
        hint = match.group(1).strip()
        hint = re.sub(r'[.,;:]+$', '', hint)
        return hint
    return None


def check_guardrails(context, outcome, phase, dry_run):
    """Match guardrails against the given filters. Returns matched list."""
    all_guards = _rb.read_jsonl(GUARD_PATH)
    active = [g for g in all_guards if g.get("status") == "active"]

    matched = []
    matched_ids = set()

    for guard in active:
        text = get_searchable_text(guard)

        if (matches_context(text, context)
                and matches_phase(text, phase)
                and matches_outcome(text, outcome)):
            entry = {
                "id": guard["id"],
                "rule": guard.get("rule", ""),
                "category": guard.get("category", ""),
            }
            hint = extract_action_hint(guard.get("rule", ""))
            if hint:
                entry["action_hint"] = hint
            matched.append(entry)
            matched_ids.add(guard["id"])

    # Side effect: update utilization counters (unless dry-run).
    # Must iterate all_guards (not active) so retired records survive the write.
    if not dry_run:
        modified = False
        for guard in all_guards:
            if guard.get("status") != "active":
                continue
            util = guard.get("utilization")
            if not isinstance(util, dict):
                continue
            if guard["id"] in matched_ids:
                util["times_active"] = util.get("times_active", 0) + 1
                modified = True
            else:
                util["times_skipped"] = util.get("times_skipped", 0) + 1
                modified = True
            _rb.recompute_utilization_score(guard)
        if modified:
            _rb.write_jsonl(GUARD_PATH, all_guards)

    return {
        "matched": matched,
        "matched_count": len(matched),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Deterministic guardrail matching engine"
    )
    parser.add_argument(
        "--context",
        choices=["infrastructure", "testing", "local", "any"],
        required=True,
        help="Filter by context type",
    )
    parser.add_argument(
        "--outcome",
        choices=["succeeded", "failed", "any"],
        default=None,
        help="Filter by goal outcome",
    )
    parser.add_argument(
        "--phase",
        choices=["post-execution", "pre-selection"],
        default=None,
        help="Filter by enforcement phase",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Match without updating utilization counters",
    )

    args = parser.parse_args()
    result = check_guardrails(args.context, args.outcome, args.phase, args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
