"""Approval-reference advisory — warn-first gate ().

Surfaces at goal-filing time the fabricated-approval shape verified in
g-115-2855: a HIGH-BLAST-RADIUS goal that ASSERTS prior approval but carries
NO verifiable approval reference. Canonical incident (2026-07-21): a goal
titled "Execute approved L1 split: promote <subtree> to a new top-level L1
(560 nodes)" was filed with goal_source=user — an INFERENCE ARTIFACT of its
pending_question: origin_signal prefix (_goal_source.py::infer maps that prefix
to "user" unconditionally), NOT a real approval — plus an LLM-authored
"<owner> APPROVED" description with ZERO evidentiary basis. It nearly executed a
near-irreversible fleet-wide knowledge-tree restructure that contradicted a
10-review taxonomy consensus.

WARN-only (NEVER blocks) — the description_length.py precedent. "High blast
radius" detection is inherently fuzzy, so this collects telemetry to validate
detector precision BEFORE any telemetry-driven promotion to a hard block. The
HARD defenses stay: guard-1328 (behavioral), rb-4513 (detection heuristic),
rb-4517 (the goal_source=inference-artifact mechanism), and the
verify-before-assuming discipline that caught the near-miss at selection time.

Detection: warns when ALL THREE hold (a narrow conjunction — a false positive
requires an approval-asserting, high-blast-radius goal that also names no
reference at all):
  1. APPROVAL ASSERTION  — title/description claims prior approval
  2. HIGH BLAST RADIUS   — title/description names an irreversible/structural op
  3. NO VERIFIABLE REF   — description lacks a decisions-board msg-id, a
                           user-directive id, a changelog reference, or an
                           "approved in g-NNN" approval goal-id

Recurring goals are exempt (title-as-spec pattern; a recurring sensor goal
never asserts a one-off approval).

Public API:
    evaluate(goal, *, source, meta_dir=None) -> dict

Return shape:
    {
      "warned": bool,             # True when all three conditions hold
      "message": str | None,      # Pre-formatted stderr message (None when not warned)
      "telemetry_written": bool,  # True when meta_dir given and append succeeded
      "triggers": {"approval_assertion": bool, "high_blast_radius": bool,
                   "verifiable_ref": bool},
    }

The CALLER (CLI or daemon endpoint) emits `message` — same side-effect-at-call
-site contract as description_length.py, so the daemon can surface it via the
HTTP response body instead of stderr.

Daemon safety:
  - Reads no env directly. meta_dir is explicit.
  - Telemetry append uses _fileops.locked_append_jsonl, fail-open on error.
  - Pure regex + string ops otherwise; no network, no subprocess.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Optional

# 1. APPROVAL ASSERTION — a claim that the work was already approved. Kept
# high-precision (no bare "\w+ approved" catch-all, which would match "not
# approved" / "auto approved"); the actor form requires "approved the/this".
_APPROVAL_ASSERTION = re.compile(
    r"\b(?:execute\s+approved"
    r"|(?:prior|explicit|user|owner)\s+approval"
    r"|approval\s+(?:granted|to\s+proceed)"
    r"|as\s+approved"
    r"|already\s+approved"
    r"|approved\s+(?:the\s+|this\s+)?"
    r"(?:split|restructure|restructuring|migration|deletion|plan|change|move|merge|promotion|proposal)"
    # Actor form "<who> approved the/this ..." — exclude negation actors
    # ("not/never/no/without approved") so a goal ABOUT a rejected approval
    # (e.g. "the split was not approved") does not trip the assertion detector.
    r"|\b(?!not\b|never\b|no\b|without\b)[a-z][\w-]*\s+approved\s+(?:the|this)\b)",
    re.IGNORECASE,
)

# 2. HIGH BLAST RADIUS — irreversible / structural / bulk operations. Framework-
# generic terms only (tree taxonomy L1, node counts, agent retirement, bulk
# store ops) — no domain product names (domain-free-examples.md).
_HIGH_BLAST = re.compile(
    r"\b(?:l1\s+split"
    r"|top-level\s+l1"
    r"|new\s+top-level"
    r"|re-?parent"
    r"|tree\s+restructure"
    r"|restructure\s+the\s+tree"
    r"|promote\s+\S+\s+to\s+(?:a\s+)?(?:new\s+)?(?:top-level|l1)"
    r"|bulk\s+delete"
    r"|purge"
    r"|retire\s+(?:the\s+)?agent"
    r"|migrate\s+\S+\s+rows"
    r"|irreversible"
    r"|\d{3,}\s+nodes)\b",
    re.IGNORECASE,
)

# 3. VERIFIABLE APPROVAL REFERENCE — a concrete, checkable pointer to a real
# approval. Searched in the DESCRIPTION only (the title asserts; the reference
# lives in the spec body).
_VERIFIABLE_REF = re.compile(
    r"(?:\bmsg-\d{8}-"
    r"|\bdecisions[-\s]board\b"
    r"|\buser[-\s]directive\b"
    r"|\bchangelog\b"
    r"|\bapproved\s+in\s+g-\d)",
    re.IGNORECASE,
)


def evaluate(goal: dict, *, source: str,
             meta_dir: Optional[Path] = None) -> dict:
    """Run the advisory. See module docstring.

    Args:
        goal: The goal dict being filed.
        source: One of "agent", "world", etc. Recorded in telemetry.
        meta_dir: Path for the telemetry sidecar. When None, telemetry is
            skipped (still returns a valid result dict).
    """
    if goal.get("recurring"):
        return {
            "warned": False,
            "message": None,
            "telemetry_written": False,
            "triggers": {"approval_assertion": False,
                         "high_blast_radius": False,
                         "verifiable_ref": False},
            "_reason": "recurring goals exempt",
        }

    title = goal.get("title") or ""
    desc = goal.get("description") or ""
    haystack = f"{title}\n{desc}"

    approval = bool(_APPROVAL_ASSERTION.search(haystack))
    blast = bool(_HIGH_BLAST.search(haystack))
    has_ref = bool(_VERIFIABLE_REF.search(desc))
    triggers = {
        "approval_assertion": approval,
        "high_blast_radius": blast,
        "verifiable_ref": has_ref,
    }

    if not (approval and blast and not has_ref):
        return {
            "warned": False,
            "message": None,
            "telemetry_written": False,
            "triggers": triggers,
        }

    message = (
        "[add-goal] APPROVAL-REFERENCE advisory: this goal ASSERTS prior "
        "approval for a HIGH-BLAST-RADIUS / irreversible operation but carries "
        "NO verifiable approval reference (decisions-board msg-id, user-directive "
        "id, changelog entry, or 'approved in g-NNN' goal-id). goal_source=user "
        "is INFERRED from origin_signal (rb-4517) and is NOT proof of approval — "
        "VERIFY the approval exists before executing (guard-1328, rb-4513; the "
        "g-115-2854 fabricated-approval near-miss)."
    )

    telemetry_written = False
    if meta_dir is not None:
        try:
            from _fileops import locked_append_jsonl
            record = {
                "filing_time": datetime.now().isoformat(timespec="seconds"),
                "goal_id": goal.get("id") or "<auto-assigned>",
                "title": title[:120],
                "source": source,
                "triggers": triggers,
                "decision": "warn",
            }
            locked_append_jsonl(
                meta_dir / "approval-reference-telemetry.jsonl", record)
            telemetry_written = True
        except Exception:
            # Best-effort — never block goal filing on telemetry. The caller's
            # emitted `message` is the primary signal.
            telemetry_written = False

    return {
        "warned": True,
        "message": message,
        "telemetry_written": telemetry_written,
        "triggers": triggers,
    }
