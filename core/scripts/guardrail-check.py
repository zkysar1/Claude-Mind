#!/usr/bin/env python3
"""Guardrail matching engine — deterministic keyword-based guardrail selection.

Given a context (infrastructure/testing/local/any), outcome (succeeded/failed/any),
and phase (post-execution/pre-selection), returns guardrails whose text matches.
This replaces the LLM's manual "read all guardrails and decide which apply" step.

Output is JSON with matched guardrails and action hints extracted from rule text.
Side effect: increments utilization.times_active on matched (unless --dry-run).

PRE-2026-05-09 also incremented `times_skipped` on every non-matching active
entry per call. Knowledge-system audit found this fires hundreds of times per
session (every check call × every non-matching active guardrail) and inflates
the skip counter by 2-15x the retrieval count. The semantic was "evaluated and
not chosen by the engine" but in practice it dominated all other skip signals.
The semantically correct `times_skipped` writer is `reflect-bookkeeping.py`'s
`cmd_utilization_delta` (LLM deliberation marks items as skipped). This module
no longer writes `times_skipped` — only `times_active` on matches.

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
RB_PATH = _rb.RB_PATH


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

def get_searchable_text(record, record_type="guardrail"):
    """Combine text fields of a guardrail or reasoning-bank entry into one lowercase string.

    Guardrails surface `rule` + `trigger_condition`; reasoning-bank entries surface
    `title` + `content` + free-form outcome fields. Category, when_to_use, and tags
    are shared across both types.
    """
    parts = []
    if record_type == "reasoning_bank":
        parts.extend([
            record.get("title", ""),
            record.get("content", ""),
            record.get("failure_lesson", "") or "",
            record.get("category", ""),
        ])
        wtu = record.get("when_to_use")
        if isinstance(wtu, str):
            parts.append(wtu)
    else:
        parts.extend([
            record.get("rule", ""),
            record.get("trigger_condition", ""),
            record.get("category", ""),
        ])

    when = record.get("when_to_use")
    if isinstance(when, dict):
        conditions = when.get("conditions", [])
        if isinstance(conditions, list):
            parts.extend(conditions)
        wcat = when.get("category", "")
        if wcat:
            parts.append(wcat)

    tags = record.get("tags", [])
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


def _check_store(path, record_type, context, outcome, phase, dry_run):
    """Generic matcher for one store (guardrails.jsonl or reasoning-bank.jsonl).

    Returns list of matched entries. Side effect: increments times_active on
    matched records (only). Pre-2026-05-09 also incremented times_skipped on
    non-matching records — see module docstring for the audit-driven removal.

    The matching pass runs on a snapshot read OUTSIDE the lock so the
    returned `matched` list reflects the records as they were when the
    caller asked. The counter-increment write runs INSIDE the lock via
    locked_modify_jsonl so concurrent guardrail checks from alpha+bravo
    don't clobber each other's times_active increments.
    The matched IDs from the snapshot are the increment targets — if a
    record's status changed between snapshot and lock acquisition, the
    "active" filter inside the modifier still gates the increment, so
    the worst case is a guardrail that flipped to retired during the
    iteration receives no increment (correct).
    """
    all_records = _rb.read_jsonl(path)
    active = [r for r in all_records if r.get("status") == "active"]

    matched = []
    matched_ids = set()

    for rec in active:
        text = get_searchable_text(rec, record_type)

        if (matches_context(text, context)
                and matches_phase(text, phase)
                and matches_outcome(text, outcome)):
            if record_type == "reasoning_bank":
                entry = {
                    "id": rec["id"],
                    "type": "reasoning_bank",
                    "title": rec.get("title", ""),
                    "category": rec.get("category", ""),
                }
            else:
                entry = {
                    "id": rec["id"],
                    "type": "guardrail",
                    "rule": rec.get("rule", ""),
                    "category": rec.get("category", ""),
                }
                hint = extract_action_hint(rec.get("rule", ""))
                if hint:
                    entry["action_hint"] = hint
            matched.append(entry)
            matched_ids.add(rec["id"])

    if not dry_run:
        from _fileops import locked_modify_jsonl
        defaults = _rb.RB_DEFAULT_FIELDS if record_type == "reasoning_bank" else _rb.GUARD_DEFAULT_FIELDS

        def _modifier(records):
            modified = False
            for rec in records:
                if rec.get("status") != "active":
                    continue
                # Increment fires only on MATCHED records (2026-05-09 audit).
                # Pre-fix iterated every active record incrementing
                # times_skipped on non-matches; that was 99% of records per
                # call and inflated the skip counter dramatically. We now skip
                # entirely (no normalize, no recompute) on non-matches —
                # cheaper and avoids touching records that shouldn't change.
                if rec["id"] not in matched_ids:
                    continue
                # Normalize before recompute — recompute_utilization_score
                # contract requires all counter keys present
                # (reasoning-bank.py:234). Without this, records missing
                # times_helpful/times_inferred_helpful raise KeyError and
                # Phase 0.5a's 2>/dev/null silences it ().
                _rb.normalize_record(rec, defaults)
                util = rec.get("utilization")
                if not isinstance(util, dict):
                    continue
                util["times_active"] = util.get("times_active", 0) + 1
                modified = True
                _rb.recompute_utilization_score(rec)
            return records if modified else None

        locked_modify_jsonl(path, _modifier)

    return matched


def check_guardrails(context, outcome, phase, dry_run, types=("guardrail",)):
    """Match against one or more stores. Returns guardrail matches in output.

    Side effect (times_active) fires for EVERY store in `types`. Pre-2026-05-09
    also touched times_skipped on non-matches; that increment was removed in
    P0 #4 (audit-driven — see module docstring).
    The returned `matched` list, however, only includes guardrail entries —
    callers iterate this list to execute `action_hint` commands, and rb entries
    have no action_hint (they are lessons, not commands). Surfacing rb in the
    output would force every caller to null-check action_hint; cleaner to keep
    rb purely as a counter side-effect here. To enumerate rb matches, call
    with types=("reasoning_bank",) exclusively.
    """
    matched = []
    if "guardrail" in types:
        matched.extend(_check_store(GUARD_PATH, "guardrail",
                                    context, outcome, phase, dry_run))
    if "reasoning_bank" in types:
        rb_matches = _check_store(RB_PATH, "reasoning_bank",
                                  context, outcome, phase, dry_run)
        # Only surface rb matches when rb was the ONLY store requested.
        if "guardrail" not in types:
            matched.extend(rb_matches)

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
    parser.add_argument(
        "--type",
        choices=["guardrail", "reasoning-bank", "both"],
        default="guardrail",
        help="Which store(s) to match against (default: guardrail only). "
             "'both' also increments reasoning-bank entries' times_active — "
             "closes the symmetric feedback loop that rb entries previously lacked.",
    )

    args = parser.parse_args()
    if args.type == "both":
        types = ("guardrail", "reasoning_bank")
    elif args.type == "reasoning-bank":
        types = ("reasoning_bank",)
    else:
        types = ("guardrail",)
    result = check_guardrails(args.context, args.outcome, args.phase, args.dry_run, types)
    print(json.dumps(result, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
