#!/usr/bin/env python3
"""closed_against_own_note.py — pure classifier for the closed-but-note-says-otherwise
detector (g-115-8309). Kept separate from the CLI so it is unit-testable with no store.

THE DEFECT IT COVERS. A goal can sit at a TERMINAL status while its own
outcome_note/progress_note argues it is NOT done. Status is the field that governs
selection, dedup suppression and every downstream sweep, so the note loses silently.
Measured 2026-08-29 (bravo, cc-05): g-326-736 read `completed` while its note opened
"REOPENED BY ITS OWN CRITERIA - do not re-close on a diagnosis"; it is the sole owner
of a product world-start smoke test, which went dark 2026-08-26..29.

WHY NEITHER NEIGHBOUR COVERS IT: `unblock-parent-status-sweep` fires when a parent goes
terminal (wrong direction); `completed-not-closed-slate` scans OPEN goals carrying a
note (the exact inverse). This is note-vs-STATUS, not note-vs-note.

REPORT-ONLY BY CONSTRUCTION. There is no apply path here and none should be added
without measuring the population first: a wrong reopen is cheap, a wrong auto-close is
not. Reopening also pushes the goal straight into the completed-not-closed population,
where it presents as "open but already has closure evidence" and invites a re-close.

THE FALSE POSITIVE THAT MATTERS, and why `in_quotes` is not decoration. A note
routinely QUOTES a not-done phrase while asserting the opposite -- measured the same
day on g-250-345: "SUPERSEDES the prior note, which ended 'NOT YET APPLIED'. That is no
longer true." A bare substring match calls that closed-against-its-own-note; it is the
opposite. So every hit carries `in_quotes` and `position`, and confidence is computed
from them rather than from the marker alone.
"""
from __future__ import annotations
import re
from typing import Any, Dict, List, Optional

TERMINAL_STATUSES = ("completed", "skipped", "expired", "superseded")

# (compiled pattern, strength, label). STRONG = the phrase is an act of reopening or a
# direct instruction about this record. MEDIUM = a not-done assertion that is common in
# ordinary progress prose, so it needs position/quote context to mean anything.
_MARKERS = [
    (re.compile(r"\bREOPEN(?:ED|ING)?\b", re.I), "strong", "reopened"),
    (re.compile(r"\bdo\s+not\s+(?:re-?)?close\b", re.I), "strong", "do-not-close"),
    (re.compile(r"\bcriteri(?:a|on)[^.\n]{0,40}?\b(?:un-?met|not\s+met)\b", re.I), "strong", "criteria-unmet"),
    (re.compile(r"\b(?:un-?met|not\s+met)\b[^.\n]{0,30}?\bcriteri(?:a|on)\b", re.I), "strong", "criteria-unmet"),
    (re.compile(r"\bNOT\s+YET\s+APPLIED\b", re.I), "medium", "not-yet-applied"),
    (re.compile(r"\bSTILL\s+OPEN\b", re.I), "medium", "still-open"),
    # Negative lookbehind measured 2026-08-29, not anticipated: on a `skipped` goal the
    # note routinely reads "closing as skipped, not completed" / "STATUS: skipped, not
    # completed" — that explains the STATUS CHOICE and is the opposite of a not-done
    # claim. Both flagged rows on the first live `skipped` run were this exact shape.
    (re.compile(r"\bnot\s+(?:yet\s+)?(?:done|complete|completed|finished)\b", re.I),
     "medium", "not-done"),
    (re.compile(r"\bremains?\s+(?:open|unmet|incomplete|outstanding)\b", re.I), "medium", "remains-open"),
]

# A quote span is anything between matched ' " ` or a markdown blockquote line.
# A single quote is only a DELIMITER when it is not inside a word. Ordinary
# apostrophes ("the goal's criteria", "didn't finish") otherwise pair up and mark
# the prose between them as quoted — which DOWNGRADES a real finding to `low` and
# drops it from the default report. Measured 2026-08-29 in fresh-eyes review of
# this file: "This goal's three completion criteria are unmet; the executor didn't
# finish" scored `low`, i.e. the incident's own language was invisible. False
# NEGATIVES are the dangerous direction for a detector, so the boundary guards are
# load-bearing, not tidiness. Double quotes and backticks need no guard (they do
# not occur word-internally in English prose).
_QUOTE_SPAN = re.compile(
    r"""(?:(?<![A-Za-z0-9])'[^'\n]{0,400}'(?![A-Za-z0-9])|"[^"\n]{0,400}"|`[^`\n]{0,400}`)""")
_HEAD_CHARS = 300


def _quote_spans(text: str) -> List[tuple]:
    spans = [(m.start(), m.end()) for m in _QUOTE_SPAN.finditer(text)]
    for line_match in re.finditer(r"^[ \t]*>.*$", text, re.M):
        spans.append((line_match.start(), line_match.end()))
    return spans


# "closing as skipped, not completed" / "STATUS: superseded, not completed" explains the
# STATUS CHOICE and is the opposite of a not-done claim. A fixed-width lookbehind cannot
# express this (Python requires one) and the first attempt covered only the literal
# "skipped, " — a second space or any other terminal status slipped straight through
# (measured in fresh-eyes review). Checking the preceding text handles every status and
# any whitespace, in one place.
_STATUS_EXPLANATION = re.compile(
    r"\b(?:skipped|superseded|expired|blocked|cancelled|canceled|decomposed)\s*,\s*$", re.I)


def _explains_status(text: str, pos: int) -> bool:
    return bool(_STATUS_EXPLANATION.search(text[max(0, pos - 40): pos]))


def _in_quotes(pos: int, spans: List[tuple]) -> bool:
    return any(a <= pos < b for a, b in spans)


def scan_note(note: Optional[str]) -> List[Dict[str, Any]]:
    """Every not-done marker in `note`, with the context needed to judge it."""
    if not note:
        return []
    spans = _quote_spans(note)
    hits: List[Dict[str, Any]] = []
    for pat, strength, label in _MARKERS:
        for m in pat.finditer(note):
            pos = m.start()
            if label == "not-done" and _explains_status(note, pos):
                continue
            hits.append({
                "label": label,
                "strength": strength,
                "match": m.group(0),
                "pos": pos,
                "position": "head" if pos < _HEAD_CHARS else "body",
                "in_quotes": _in_quotes(pos, spans),
                "context": " ".join(note[max(0, pos - 90): pos + 110].split()),
            })
    return hits


def confidence(hits: List[Dict[str, Any]]) -> str:
    """high / medium / low / none — from position and quoting, never the marker alone.

    An UNQUOTED strong marker in the note's head is the incident's own shape and the
    only thing that earns `high`. A quoted marker never rises above `low`, because
    quoting is how a note reports someone else's not-done claim -- often to REFUTE it.
    """
    best = "none"
    for h in hits:
        if h["in_quotes"]:
            cand = "low"
        elif h["strength"] == "strong":
            cand = "high" if h["position"] == "head" else "medium"
        else:
            cand = "medium" if h["position"] == "head" else "low"
        if ["none", "low", "medium", "high"].index(cand) > ["none", "low", "medium", "high"].index(best):
            best = cand
    return best


def classify_goal(goal: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """None when the goal is not terminal or has no not-done marker in either note."""
    if (goal.get("status") or "") not in TERMINAL_STATUSES:
        return None
    per_field = {}
    all_hits: List[Dict[str, Any]] = []
    for field in ("outcome_note", "progress_note"):
        hits = scan_note(goal.get(field))
        if hits:
            per_field[field] = hits
            all_hits.extend(hits)
    if not all_hits:
        return None
    return {
        "goal_id": goal.get("id"),
        "aspiration_id": goal.get("asp_id") or goal.get("aspiration_id"),
        "status": goal.get("status"),
        "title": (goal.get("title") or "")[:110],
        "confidence": confidence(all_hits),
        "hit_count": len(all_hits),
        "fields": {k: v for k, v in per_field.items()},
    }
