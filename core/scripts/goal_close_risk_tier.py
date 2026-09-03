#!/usr/bin/env python3
"""goal_close_risk_tier.py — pure risk-tier classifier for the close-review gate
(g-357-40). Kept separate from the CLI/gate so it is unit-testable with no store.

WHY A TIER AT ALL. User directive 2026-08-31: before a goal can be closed it must be
fully reviewed for accuracy — "go all out, burn tokens to save humans time". Motivating
incident: coach g-012-02 closed green with 6/16 wrong entity identities because the
author self-graded against count-based criteria (g-357-39). SDLC principle: the author
must not approve their own close. But review is expensive, and applying it to every
close would tax the recurring-sweep cadence that the loop runs dozens of times a day
for no accuracy gain. The tier is what makes "review everything risky" affordable:

  tier 0 — recurring + outcome_class routine + no new artifacts. Zero added cost.
  tier 1 — default. No review artifact required.
  tier 2 — ANY trigger below. Review artifact required (when the gate is enabled).

TIER-2 TRIGGERS (from the goal's own specification, each independently sufficient):
  entities      description/source enumerates >= 3 named entities
  user_truth    user-relayed ground truth (participants include user, or directive/
                mission-sourced)
  deliverable   new tree node or user-facing deliverable produced
  framework     framework files touched (core/, .claude/)
  high_prio     HIGH priority, non-recurring
  first_of_asp  first goal of a new aspiration

FAIL-TO-TIER-1, NEVER FAIL-CLOSED (guard-142). A gate that blocks work because of its
own bugs is worse than the problem it catches, so every unparseable or missing input
degrades to tier 1 (the no-review default) rather than tier 2. The ONE thing that must
never fail open is the verdict-ABSENCE check in the gate itself — absence of review is
exactly the condition the gate exists to catch — but that is the gate's decision, not
this classifier's.

ORDER MATTERS: tier 0 is checked FIRST and short-circuits. A recurring routine sweep
that happens to touch a framework file is still tier 0 — otherwise every recurring
framework-hygiene goal would demand a review artifact and the cadence would stall.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# Paths whose modification makes a close framework-risky.
FRAMEWORK_PREFIXES = ("core/", ".claude/")

# A "named entity" for the >=3 trigger: an id-shaped token. Deliberately NARROW —
# this counts things a reviewer would have to check one by one (goal ids, guardrail
# ids, rb ids, hypothesis slugs, shas), not English nouns. Widening it would push
# ordinary prose goals into tier 2 and make the gate the thing people route around.
_ENTITY_RE = re.compile(
    r"\b("
    r"g-\d+-\d+"            # goal ids
    r"|guard-\d+"           # guardrails
    r"|rb-\d+"              # reasoning bank
    r"|asp-\d+"             # aspirations
    r"|sq-\d+|sig-\d+|bel-\d+|pq-\d+|pt-\d+"
    r"|[0-9a-f]{7,40}"      # git shas
    r")\b"
)

# Directive/mission provenance markers. Matched case-insensitively against the
# goal's description + title.
_DIRECTIVE_RE = re.compile(
    r"(user directive|owner directive|per the user|user-relayed|mission-sourced"
    r"|standing directive|verbatim intent|user said|owner said)",
    re.IGNORECASE,
)

# A user-facing deliverable produced by this goal.
_DELIVERABLE_RE = re.compile(
    r"(new tree node|tree node added|user-facing|customer-facing|published"
    r"|deliverable|report delivered|sent to the user|notify the user)",
    re.IGNORECASE,
)


def _text_of(goal: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key in ("title", "description", "source_message"):
        v = goal.get(key)
        if isinstance(v, str):
            parts.append(v)
    return "\n".join(parts)


def named_entities(text: Optional[str]) -> set:
    """The distinct id-shaped tokens themselves, lowercased.

    Exists so the close-review verdict producer can DIFF two entity sets
    (source vs artifact) against the same regex the tier classifier counts with
    (g-357-41). The alternative — a second regex in the producer — is the
    failure this function prevents: the classifier would route a goal to tier 2
    for enumerating entities its reviewer then could not see, and the two
    definitions would drift with nothing failing when they did.

    Lowercased because id families are written both ways in prose and an
    identity check must not report a case difference as a substitution.
    """
    if not isinstance(text, str) or not text:
        return set()
    return {m.group(0).lower() for m in _ENTITY_RE.finditer(text)}


def count_named_entities(text: Optional[str]) -> int:
    """Distinct id-shaped tokens in text. Distinct, not total: a description that
    repeats one goal id eight times names ONE thing to check, not eight."""
    return len(named_entities(text))


def touches_framework(files: Optional[List[str]]) -> bool:
    if not files:
        return False
    for f in files:
        if not isinstance(f, str):
            continue
        # Strip a literal "./" PREFIX only. NOT .lstrip("./") — that takes a
        # character SET, so it eats the leading dot of ".claude/..." and the
        # framework trigger silently misses every rule/skill edit. Caught by
        # test_trigger_framework_files; "core/..." passed either way, which is
        # exactly why a one-path test would not have found it.
        norm = f.replace("\\", "/")
        while norm.startswith("./"):
            norm = norm[2:]
        if norm.startswith(FRAMEWORK_PREFIXES):
            return True
    return False


def _truthy_recurring(goal: Dict[str, Any]) -> bool:
    v = goal.get("recurring")
    return v is True or (isinstance(v, str) and v.strip().lower() == "true")


def classify(
    goal: Dict[str, Any],
    *,
    files_touched: Optional[List[str]] = None,
    artifacts_count: Optional[int] = None,
    is_first_of_aspiration: bool = False,
) -> Dict[str, Any]:
    """Return {"tier": 0|1|2, "reasons": [...], "triggers": {...}}.

    `goal` is the goal record. `files_touched` / `artifacts_count` /
    `is_first_of_aspiration` are execution-time facts the record does not carry;
    all are optional and their absence degrades toward tier 1, never toward tier 2.
    """
    if not isinstance(goal, dict):
        return {"tier": 1, "reasons": ["classifier-input-not-a-dict (fail-to-tier-1)"],
                "triggers": {}}

    recurring = _truthy_recurring(goal)
    outcome_class = (goal.get("outcome_class") or "").strip().lower()

    # ── tier 0 — checked FIRST and short-circuits (see module docstring) ──
    no_new_artifacts = (artifacts_count is None) or (artifacts_count == 0)
    if recurring and outcome_class == "routine" and no_new_artifacts:
        return {
            "tier": 0,
            "reasons": ["recurring + outcome_class=routine + no new artifacts"],
            "triggers": {},
        }

    text = _text_of(goal)
    participants = goal.get("participants") or []
    if not isinstance(participants, (list, tuple)):
        participants = []

    entity_count = count_named_entities(text)
    triggers = {
        "entities": entity_count >= 3,
        "user_truth": ("user" in [str(p).lower() for p in participants])
                      or bool(_DIRECTIVE_RE.search(text)),
        "deliverable": bool(_DELIVERABLE_RE.search(text)),
        "framework": touches_framework(files_touched),
        "high_prio": (str(goal.get("priority") or "").upper() == "HIGH") and not recurring,
        "first_of_asp": bool(is_first_of_aspiration),
    }

    fired = [name for name, hit in triggers.items() if hit]
    if fired:
        reasons = []
        for name in fired:
            if name == "entities":
                reasons.append(f"entities: {entity_count} distinct named entities (>=3)")
            elif name == "user_truth":
                reasons.append("user_truth: participants include user, or directive/mission-sourced")
            elif name == "deliverable":
                reasons.append("deliverable: new tree node or user-facing deliverable")
            elif name == "framework":
                reasons.append("framework: touched core/ or .claude/")
            elif name == "high_prio":
                reasons.append("high_prio: HIGH priority, non-recurring")
            elif name == "first_of_asp":
                reasons.append("first_of_asp: first goal of a new aspiration")
        return {"tier": 2, "reasons": reasons, "triggers": triggers}

    return {"tier": 1, "reasons": ["no tier-2 trigger fired"], "triggers": triggers}
