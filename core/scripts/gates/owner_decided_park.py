"""Owner-decided park marker — the declared exemption for human-gated digests ().

WHAT THIS IS FOR
----------------
Two consumers put human-gated goals in front of the owner: the 72h escalation
digest (`user-blocker-escalation-check.py`) and the /stop completion report
(`completion_digest.py`). Both read goals carrying a live `human_blocked:`
defer. Some of those goals are waiting on the owner because the owner DECIDED
they should wait — "i will do prod key later", "Do not re-ask, re-file or
re-email". Listing those in an owner-facing email re-asks a settled question,
which is the expensive direction on an owner-facing surface (guard-6754).

This module is the ONE predicate that says "the owner decided this one".
Both consumers import it. Neither re-derives it.

WHY ONE MODULE AND NOT TWO CORRECT-LOOKING COPIES
-------------------------------------------------
guard-4015's measured corollary: when two consumers read one shared exemption
set, the thing that diverges is their MATCHING SEMANTICS, not their inputs —
and both halves stay individually correct, which is why it survives review.
guard-2275 is the same finding stated as a rule: prefer one shared predicate
module. `human-blocked-defer-join.py::is_live_human_blocked` already carries
that lesson in its own docstring for the POPULATION half; this is its
exemption-half sibling.

WHY A DECLARED MARKER AND NOT A PROSE MATCH
-------------------------------------------
guard-4015, which g-353-102's own description names as binding: an exemption
harvested by scraping free text inherits the SCRAPER's precision, not the
author's intent, and it fails as a silent MISS that DISABLES the protection —
the opposite direction from the self-reference family it resembles. So:

  * a defer that merely SAYS "do not re-ask" in prose is NOT exempt;
  * a defer that mentions the words "owner decided" is NOT exempt;
  * only the literal bracketed token, carrying a record id of a recognised
    shape, is exempt.

The RULE clause of guard-4015 is "validate the SHAPE of each pattern before
honoring it". `OWNER_DECIDED_RE` is that validation: the ref must be a real
record-id shape (`msg-` board message, `pq-` pending question, `g-` goal,
`asp-` aspiration). A bare `[owner-decided: yes]` does not match, because
"yes" names no record anyone can open.

FAIL-SAFE DIRECTION — the property that makes a marker-in-the-defer safe
-----------------------------------------------------------------------
Every failure of this module returns "not a park", and "not a park" means the
goal IS emailed. A missing marker, a malformed ref, a rewritten defer, an
unreadable goal, a None: all of them fail toward TELLING the owner. That is
the inverse of guard-4015's incident, where the exempter's failure silently
removed protection. Here the exempter's failure restores it.

That is also why the marker lives INSIDE the defer_reason it qualifies rather
than in a separate field: a defer rewrite that changes the premise necessarily
drops or restates the marker, and dropping it means one more email — never one
fewer.

SCOPE — deliberately narrow
---------------------------
Honoured ONLY on a goal whose `defer_reason` starts with `human_blocked:`.
The marker cannot suppress anything else: not a `precondition_unmet:` defer,
not a bare narrative defer, not a goal with no defer at all. g-353-102's
constraint list forbids changing how the selector, lane H, or the precheck
age-escalation treat `human_blocked` — none of them import this module, and
this module reads nothing but the goal dict it is handed.

WHY NOT `origin_signal`, THE FIELD THE FIRST LEG USES
-----------------------------------------------------
`user-blocker-escalation-check.py`'s module docstring settled that in
g-115-6991: `origin_signal` is PROVENANCE (who asked for the work) and a park
is ROUTING (who the work waits on). Filtering the digest on provenance made
the strongest claim on the owner's attention into the suppression criterion.
This marker is neither — it is an explicit statement that a DECISION was taken,
and it carries the decision's record id so a reader can go and check it.

Daemon safety: pure functions, no I/O, no env reads, no globals.
"""
from __future__ import annotations

import re
from typing import Any

# The prefix this marker is scoped to. Written literally rather than imported
# from gates/defer_classifier.STRUCTURED_DEFER_PREFIXES on purpose: that tuple
# is the set of prefixes that BYPASS THE CAPABILITY GATE, and widening it must
# never silently widen this exemption. The two lists answer different questions
# and are free to diverge (the same reasoning audit-deferred-defers.py:74-84
# records for its own copy).
HUMAN_BLOCKED_PREFIX = "human_blocked:"

# The declared marker. Case-insensitive on the keyword (LLM-authored defers
# drift casing across rewrites — the same reasoning defer_classifier.py gives
# for its lowercase compare), strict on the SHAPE of the reference.
#
# Positive control for any future edit here:
#   "human_blocked: prod-api-key ... [owner-decided: msg-20260902-231119-alpha-599]"
#     -> ref == "msg-20260902-231119-alpha-599"
# Negative controls that MUST keep failing:
#   "human_blocked: ... the owner said do not re-ask"      -> None (prose)
#   "human_blocked: ... [owner-decided: yes]"              -> None (not a record id)
#   "human_blocked: ... owner-decided: msg-123"            -> None (no brackets)
#   "precondition_unmet: ... [owner-decided: msg-123]"     -> None (wrong prefix)
#   "human_blocked: ... [owner-decided: G-363-81]"         -> None (record ids in
#     this framework are lowercase, so an uppercase ref opens NOTHING)
#
# The case-insensitivity is SCOPED to the keyword with an inline group. A global
# re.IGNORECASE relaxes the ref alternation and character classes too, which is
# the one direction this module must never widen in: every other failure path
# here returns None and the goal IS emailed, so a widened SHAPE is the only path
# that suppresses an owner-facing line on a malformed ref.
OWNER_DECIDED_RE = re.compile(
    r"\[\s*(?i:owner-decided):\s*((?:msg|pq|g|asp)-[A-Za-z0-9][A-Za-z0-9._-]*)\s*\]",
)


def owner_decided_ref(goal: Any) -> str | None:
    """Return the owner-decision record id when `goal` is a declared park, else None.

    `goal` is a goal dict. Anything else — None, a string, a list — returns
    None rather than raising: this runs inside two fail-open sweeps and an
    exception here would cost a whole population leg.
    """
    if not isinstance(goal, dict):
        return None
    defer = goal.get("defer_reason")
    if not isinstance(defer, str) or not defer:
        return None
    if not defer.lower().startswith(HUMAN_BLOCKED_PREFIX):
        return None
    m = OWNER_DECIDED_RE.search(defer)
    if not m:
        return None
    return m.group(1)


def is_owner_decided_park(goal: Any) -> bool:
    """True iff `goal` carries a well-formed owner-decided park marker."""
    return owner_decided_ref(goal) is not None
