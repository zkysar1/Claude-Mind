"""Deadline-date gate — refuse a filing that PROMISES a deadline but carries none.

Origin: user directive, 2026-09-04 06:05:46 -0400 (alert email
nalm7rmoajb4mllnpv1a39ln1rnn75ttevd4cu01, goal g-115-8906), verbatim:

    "You need to improve your processes to where you get an error if you
     add something without a date."

The incident that produced it, in the operator's own post-mortem from the
same thread: an opportunity was screened and recommended on 2026-08-26,
"Nothing carried that forward. The record for it was missing its due date,
so no deadline alarm could see it, and it sat for nine days." It surfaced
with ~10h left on a hard external deadline.

The failure chain is: record filed -> no date field -> every deadline alarm
is structurally blind to it -> silence -> near-miss. Note where the break
is. Nothing malfunctioned; the alarm worked perfectly against the fields it
had. An alarm can only see what the WRITE recorded, so the only place this
class can be fixed is at filing time. That is this gate.

DETECTOR, NOT RESOLVER — the load-bearing distinction
-----------------------------------------------------
`gates/defer_date.py` already turns narrative date language into an ISO
timestamp. It is tempting to build this gate on it: "if extract() matches,
require a date field." That is WRONG, and measurably so — it inverts the
question and would have MISSED THE VERY RECORD THAT CAUSED THE INCIDENT.

Measured 2026-09-04 against the incident's own words:

    extract("bid closes 4pm ET today")            -> matched=False
    extract("Quotes are due at 4:00pm Eastern today") -> matched=False
    extract("submission deadline September 12 2026")  -> matched=True

`extract` answers "can I COMPUTE a date from this text?" This gate must
answer "does this text PROMISE a deadline?" Those are different predicates,
and the dangerous records live exactly in the gap: text that commits to a
deadline while naming no absolute date is both the most urgent case and the
one a resolver scores as silent. Building the detector out of the resolver
is the guard-3179 class — a predicate that measures a neighbouring property
and reads as though it measured the intended one.

So: cue-matching decides whether to FIRE; `extract` is called only to
SUGGEST a value in the refusal message when it happens to resolve. A
suggestion that comes back empty never weakens the refusal.

CEILINGS vs FLOORS (guard-2073 / guard-2458)
--------------------------------------------
Not every date field satisfies a deadline. `resolves_no_earlier_than` and
`deferred_until` are FLOORS — "not before X" — and a floor cannot raise a
deadline alarm; a record carrying only a floor is exactly as invisible as
one carrying nothing. Only CEILING fields (`resolves_by`, `expires_at`,
`deadline`, `due_date`) discharge the requirement. A floor-only record is
still refused, and the message says why, because accepting it would let the
gate report success while reproducing the incident.

FIRES ON ADD ONLY (guard-2475)
------------------------------
This is a filing-time gate. It MUST NOT be wired into the field-update path.
guard-2475: when a store validates the whole record on every field update, a
validation rule added after records were written applies retroactively, and
any later update to ANY field is refused because an unrelated field fails.
The live corpus holds thousands of dateless goals; validating on update
would wedge every one of them out of all future writes. Add-only keeps the
new rule off every record that predates it.

FAIL-OPEN (guard-142)
---------------------
A gate whose job is to block gets its own dependency errors wrong sometimes.
Any internal error returns would_block=False with decision "fail_open". A
gate that fails closed on its own bug stops all filing; this one degrades to
the status quo ante, which is a world without this gate.

Public API:
    evaluate(payload, *, override_deadline=None, agent_name="",
             world_dir=None, now=None) -> dict

Return shape:
    {"would_block": bool, "reason": str, "cue": str|None,
     "suggested": str|None, "override_applied"?: str}

Daemon safety: reads no env directly; `world_dir`/`agent_name` are explicit
args; `now` is injectable for deterministic tests.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

try:  # pragma: no cover - import shim, exercised by both callers
    from gates.defer_date import extract as _extract_date
except Exception:  # pragma: no cover
    from defer_date import extract as _extract_date  # type: ignore

try:  # pragma: no cover
    from _gate_log import log as _gate_log
except Exception:  # pragma: no cover
    def _gate_log(*_a, **_kw):  # type: ignore
        return None


GATE_ID = "deadline-date-gate"

# Fields that discharge the requirement. CEILINGS only — see module docstring.
CEILING_FIELDS = ("resolves_by", "expires_at", "deadline", "due_date")

# Recognised but NOT sufficient. Named so the refusal can say why.
FLOOR_FIELDS = ("resolves_no_earlier_than", "deferred_until")

# A deadline is a COMMITMENT BOUND TO A TIME, and both halves must be
# ADJACENT. Cue-word-anywhere + time-token-anywhere was measured on the live
# corpus 2026-09-04 and fired on 513 of 2423 live goals (21.2%), with 14 of 14
# hand-sampled firings false: "closes/closing" is this framework's single most
# common verb (goal close), "EXPIRED" is a goal STATUS, "before" is ordinary
# prose, and every description carries an ISO date somewhere — so a proximity
# window over a corpus whose SUBJECT MATTER is goals closing and windows ending
# measures the vocabulary, not the deadline (guard-3179).
#
# Adjacency is what separates them: "closes 4pm ET today" is a deadline,
# "closes the goal after verify" is not, and no amount of window-widening
# tells those apart — widening strictly worsens it.

# A strict time EXPRESSION. Deliberately excludes a bare ISO date, which is
# ambient in framework prose (ids, stamps, incident dates); an ISO date counts
# only when a cue binds it (see _CUE_PATTERNS).
_TIME = (
    r"(?:\d{1,2}(?::\d{2})?\s*(?:am|pm)"
    r"|\d{1,2}:\d{2}\s*(?:am|pm)?"
    r"|today|tomorrow|tonight|noon|midnight"
    r"|end\s+of\s+(?:day|week|month|business)"
    r"|\d{4}-\d{2}-\d{2}"
    r"|(?:mon|tues|wednes|thurs|fri|satur|sun)day"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}"
    r"|\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*)"
)

# Optional connective between cue and time ("due AT 4pm", "closes ON Friday").
# A hyphen must be a DASH (whitespace on at least one side), never the joint
# of a compound token: "due-today" / "deadline-today" / "WAKE-DUE TIME" are
# identifiers being DISCUSSED, not obligations bound to a time. Measured on
# the live corpus — this distinction alone removes a whole false-positive
# class without touching any genuine deadline.
_CONN = r"(?:\s+(?:at|by|on|is|of|before|no\s+later\s+than)\b|\s*:|\s+-|-\s+)?\s*"

# Each pattern is one bound construction. `due` still excludes causal "due to".
_CUE_PATTERNS = (
    (re.compile(r"\bdeadlines?\b" + _CONN + _TIME, re.I), "deadline"),
    (re.compile(r"\bdue\b(?!\s+to\b)" + _CONN + _TIME, re.I), "due"),
    (re.compile(r"\bno\s+later\s+than" + _CONN + _TIME, re.I), "no-later-than"),
    (re.compile(r"\bcut-?off\b" + _CONN + _TIME, re.I), "cutoff"),
    (re.compile(r"\bexpir(?:es|ing|y|ation)\b" + _CONN + _TIME, re.I), "expires"),
    (re.compile(r"\bclos(?:es|ing)\b" + _CONN + _TIME, re.I), "closes"),
    (re.compile(r"\blast\s+day\b" + _CONN + _TIME, re.I), "last-day"),
    # Reverse order: "4pm ET deadline", "Friday cutoff".
    (re.compile(_TIME + r"[^.\n]{0,12}?\b(?:deadline|cut-?off)\b", re.I), "deadline-rev"),
)

def _has_ceiling(payload: dict) -> Optional[str]:
    for f in CEILING_FIELDS:
        v = payload.get(f)
        if v not in (None, "", [], {}):
            return f
    return None


def _has_floor(payload: dict) -> Optional[str]:
    for f in FLOOR_FIELDS:
        v = payload.get(f)
        if v not in (None, "", [], {}):
            return f
    return None


def find_cue(text: str):
    """Return (cue_name, matched_text) when `text` binds an obligation to a time."""
    if not text:
        return None
    for pat, name in _CUE_PATTERNS:
        m = pat.search(text)
        if m:
            return (name, m.group(0).strip())
    return None


def _audit_override(world_dir, agent_name: str, reason: str, payload: dict) -> None:
    """Best-effort append to the bypass ledger. Never raises."""
    try:
        if not world_dir:
            return
        p = Path(world_dir) / "deadline-date-overrides.jsonl"
        rec = {
            "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "agent": agent_name or "",
            "gate": GATE_ID,
            "reason": reason,
            "title": (payload.get("title") or "")[:200],
        }
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        return


def evaluate(payload: dict, *, override_deadline: Optional[str] = None,
             agent_name: str = "", world_dir=None,
             now: Optional[datetime] = None) -> dict:
    try:
        # Recurring goals carry their own cadence (interval_hours) and are
        # re-armed by the close path; a one-shot deadline does not apply.
        if payload.get("recurring") or payload.get("interval_hours"):
            _gate_log(GATE_ID, "noop", caller=agent_name or None,
                      extra={"decision_path": "recurring"})
            return {"would_block": False, "reason": "recurring goal — cadence, not deadline",
                    "cue": None, "suggested": None}

        have = _has_ceiling(payload)
        if have:
            _gate_log(GATE_ID, "pass", caller=agent_name or None,
                      extra={"decision_path": "ceiling_present", "field": have})
            return {"would_block": False, "reason": f"deadline field present ({have})",
                    "cue": None, "suggested": None}

        text = " ".join(str(payload.get(k) or "") for k in ("title", "description"))
        hit = find_cue(text)
        if not hit:
            _gate_log(GATE_ID, "noop", caller=agent_name or None,
                      extra={"decision_path": "no_cue"})
            return {"would_block": False, "reason": "no deadline language",
                    "cue": None, "suggested": None}

        cue, matched = hit
        got = _extract_date(text, now=now) or {}
        suggested = got.get("deferred_until") if got.get("matched") else None

        floor = _has_floor(payload)
        parts = [
            f"deadline-bearing text ({cue}: {matched!r}) with no deadline field.",
            f"Set one of {', '.join(CEILING_FIELDS)} so a deadline alarm can see it.",
        ]
        if floor:
            parts.append(
                f"NOTE: {floor} is set, but that is a FLOOR ('not before'), not a "
                "deadline — it raises no alarm (guard-2073/guard-2458)."
            )
        if suggested:
            parts.append(f"Parsed from the text, suggest resolves_by={suggested}.")
        else:
            parts.append(
                "No absolute date is parseable from the text, which is the "
                "high-risk shape — supply the date explicitly."
            )
        reason = " ".join(parts)

        if override_deadline:
            _audit_override(world_dir, agent_name, override_deadline, payload)
            _gate_log(GATE_ID, "override", caller=agent_name or None,
                      trigger_matched=cue, override_reason=override_deadline)
            return {"would_block": False, "reason": reason, "cue": cue,
                    "suggested": suggested, "override_applied": override_deadline}

        _gate_log(GATE_ID, "block", caller=agent_name or None, trigger_matched=cue,
                  extra={"decision_path": "cue_without_ceiling", "cue": cue})
        return {"would_block": True, "reason": reason, "cue": cue,
                "suggested": suggested}

    except Exception as exc:  # guard-142 — fail OPEN on our own errors
        _gate_log(GATE_ID, "fail_open", caller=agent_name or None,
                  gate_error=str(exc))
        return {"would_block": False, "reason": f"gate error, failing open: {exc}",
                "cue": None, "suggested": None}
