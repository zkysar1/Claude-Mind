# domain-leak-exempt: regex patterns enumerate the capability-absence claim
# family — functional detector patterns, not pedagogical examples.
"""Single source of truth for the capability-absence claim pattern family.

A capability-absence claim asserts that something CANNOT be done, does not
exist, or requires a human — "no agent-provisionable path", "only a human can",
"needs credentials I do not hold". `.claude/rules/verify-before-assuming.md`
§ "Capability-Absence Claims" treats these as negative conclusions requiring
the multi-signal rule plus an exhaustive search, because the symmetric failure
is expensive in both directions: a wrong claim freezes real work, and its
build-side twin ("Y needs to be built") duplicates something that already
exists.

WHY A WRITE-TIME ADVISORY AND NOT A THIRD GATE (measured, g-115-3181,
2026-07-30, meta/gate-firings.jsonl, 120,219 rows / 37 gate_ids):

    capability-gate              8,530 firings   (write chokepoints, in code)
    exhaustive-search-gate           5 firings   (all noop; SKILL.md step only)
    verify-before-assuming-gate      0 firings   (SKILL.md step only)

Both zero-ish gates DO log to the ledger (`_gate_log` at
exhaustive-search-gate.py:284 and verify-before-assuming-gate.py:261), so those
counts mean "did not fire", not "does not log". They are invoked only from
`aspirations-verify` Q2 lines 294-304 — an LLM-discretionary step. capability-gate
is invoked from real chokepoints in `aspirations.py`. That 8530-vs-5-vs-0 split is
the cost of LLM-gated versus script-gated enforcement, measured on one corpus.

So the detection this module powers is deliberately NOT a new gate competing with
those two. It is an advisory fired from the code path that already runs, whose
whole job is to say "you are writing a capability-absence claim; here is the
retrieval command and the recency question to answer first."

WHY PHRASES, NOT TOKENS: capability-gate's own `trigger_matched` values in the
same ledger are single generic words — `npc` 878, `clean` 268, `against` 233,
`verify` 236, `commit` 215. Matching "against" as a capability keyword is
over-matching. Every pattern here is therefore multi-word and anchored, and the
module is FN-tolerant by design: a missed advisory costs one un-prompted
retrieval, a false one trains the reader to ignore the banner.

Scope: the capability-absence family ONLY. Sibling module
`_git_state_absence_patterns.py` carries the git-state family and is spliced
into two gates the same way; keep the families separate so each stays
independently auditable.
"""

import re

CAPABILITY_ABSENCE_PATTERNS = [
    # Explicit routing-away phrasings — the forms `.claude/rules/probe-before-defer.md`
    # and `capability-before-user.md` exist to prevent.
    re.compile(r"\bno agent[- ]provisionable\b", re.IGNORECASE),
    re.compile(r"\bonly a human can\b", re.IGNORECASE),
    re.compile(r"\brequires? a (real )?(human|user|person)\b", re.IGNORECASE),
    re.compile(r"\bhuman[- ]only\b", re.IGNORECASE),
    re.compile(r"\bneeds? (a )?human (intervention|action|approval)\b", re.IGNORECASE),
    # Credential / permission absence.
    re.compile(r"\b(needs?|requires?) credentials (i|we) (do not|don't) (hold|have)\b", re.IGNORECASE),
    re.compile(r"\b(i|we) (do not|don't) have (the )?(credentials|permission|access)\b", re.IGNORECASE),
    re.compile(r"\bno (credentials|permission|access) (to|for)\b", re.IGNORECASE),
    # Capability absence proper.
    re.compile(r"\bcannot be (generated|created|produced|automated)\b", re.IGNORECASE),
    re.compile(r"\bcannot generate\b", re.IGNORECASE),
    re.compile(r"\bungeneratable\b", re.IGNORECASE),
    re.compile(r"\bnot possible without\b", re.IGNORECASE),
    re.compile(r"\bno (way|means|mechanism) to\b", re.IGNORECASE),
    re.compile(r"\bthere is no (script|tool|skill|command) (that|to|for)\b", re.IGNORECASE),
    # Build-side twin — verify-before-assuming.md § "Capability-Absence Claims"
    # names these explicitly as the same class.
    re.compile(r"\bneeds? to be built\b", re.IGNORECASE),
    re.compile(r"\bmust be (built|created|written) (first|from scratch)\b", re.IGNORECASE),
    re.compile(r"\bno support for\b", re.IGNORECASE),
    re.compile(r"\bdoes ?n[o']?t exist yet\b", re.IGNORECASE),
]


def detect(text):
    """Return the list of matched pattern SOURCES for `text`.

    Empty list means no capability-absence phrasing was found. Non-string or
    empty input returns [] — this is an advisory helper and must never raise
    into a caller whose real job is writing a durable record.
    """
    if not text or not isinstance(text, str):
        return []
    return [p.pattern for p in CAPABILITY_ABSENCE_PATTERNS if p.search(text)]


def advise(text, field=None, goal_id=None):
    """Compose the advisory banner for `text`, or None when nothing matched.

    Returns a string; the caller decides where it goes. Callers inside a
    Bash-invoked script should print it to stderr, which DOES reach the model
    in tool output — unlike a non-blocking PreToolUse hook's stderr, which does
    not (guard-1680; the 59-day inert pre-edit-context-gate is the canonical
    cost of getting that wrong).

    NEVER raises and NEVER blocks. The write proceeds either way: a fail-closed
    gate on a text pattern would wedge legitimate writes, and `capability-gate`
    already hard-blocks the subset it can actually prove.
    """
    try:
        matched = detect(text)
    except Exception:
        return None
    if not matched:
        return None

    where = " in %s" % field if field else ""
    subject = " on %s" % goal_id if goal_id else ""
    lines = [
        "[capability-absence] ADVISORY: this write%s%s asserts a capability is "
        "ABSENT. That is a negative conclusion, so it needs the multi-signal rule "
        "before it lands (.claude/rules/verify-before-assuming.md "
        "§ Capability-Absence Claims)." % (where, subject),
        "  matched: %s" % ", ".join(matched[:4]),
        "  1. RECENCY — the cheapest disproof, and nothing else asks it: did you "
        "or a partner perform this exact action in the last few hours? Check "
        "agents/<agent>/session/execution-diary.jsonl and your own journal before "
        "asserting it cannot be done.",
        "  2. RETRIEVE — run this before the claim, not after:",
        "     bash core/scripts/retrieve.sh --category \"<the capability you are "
        "calling absent>\" --depth shallow --include-framework",
        "     (--include-framework is load-bearing: without it the response "
        "carries no framework_rules key at all, so the rule or convention that "
        "already grants the capability is silently absent — g-115-3777.)",
        "  3. GRANTS — a standing grant is a permission that CHANGED at a point "
        "in time, so a static capability catalog cannot see it: "
        "bash core/scripts/world-cat.sh conventions/capability-routing.md",
        "  Advisory only — this write is NOT blocked.",
    ]
    return "\n".join(lines)
