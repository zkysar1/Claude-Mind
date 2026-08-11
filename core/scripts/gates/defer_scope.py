"""Defer-scope vocabulary — ONE scope set, four lanes ().

Four recheck lanes each report a nonzero exclusion count that no sweep can
act on, because the thing being excluded is UN-KEYABLE free text:

    lane          sweep                          exclusion field
    user-leg      audit-user-to-agent            (undeclared user_leg_scope)
    grant         audit-user-to-agent            (unkeyable grant scope)
    precondition  precondition-defer-recheck     skipped_free_form
    credential    credential-defer-recheck       skipped_no_key

In every case the zero is un-keyable INPUT, not a clean queue — the
guard-1802 / reclaim-rule-7 class, where a zero-result sweep and a genuinely
clean queue produce identical output.

THE ONE DECISION (goal outcome 3). The four lanes do NOT get four
vocabularies. They share ONE scope set with per-lane VALID SUBSETS. That is
not a tidiness preference — it is what the live population says. Sampling the
17 credential-lane defers this box holds, the recurring shapes include
`deployment-approval` and `credential-grant`, which lane P's
VALID_USER_LEG_SCOPES ALREADY declares. Four separate enums would have
re-declared those two under three more spellings, which is exactly the
"three incompatible vocabularies" the originating goal warned about.

Lane P's subset is IMPORTED from gates.user_leg_scope, never re-typed, so the
sharing is mechanical rather than aspirational. `aspirations.py` remains the
SSOT for that set; `gates/user_leg_scope.py` mirrors it and
tests/test_allowlist_parity_batch3.py::test_2b_user_leg_scopes_equal pins the
equality. This module is a THIRD reader of that same set and adds no fourth
copy.

WHAT THIS MODULE IS NOT. It is not the cross-repo contract from the
`establish-vocabulary-contract` skill: there is no deploy boundary here, no
mirror, and no sync guard to wire — every consumer imports this file directly.
What IS borrowed from that skill is the half that generalizes:

  * step 4 — `classify()` is a TOTAL function. Every input maps to a declared
    token or to ONE explicit sentinel with the same name in every lane. An
    unrecognized value is never passed through and never coerced to the most
    common token.
  * step 5 — `undeclared()` surfaces the observed TEXT, not merely a count.
    The value is the diagnostic; a count tells you drift exists and nothing
    about what it is.

Daemon safety: pure functions, no I/O, no env reads.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

# Resolve the sibling user_leg_scope.py without a package-relative import.
# This module is reached BOTH ways: CLI consumers put `gates/` on sys.path and
# `import defer_scope`, while the daemon imports the `gates` PACKAGE — under
# which only `core/scripts` is on the path, so a bare `from user_leg_scope
# import ...` raises ModuleNotFoundError (measured, not assumed). A
# `from .user_leg_scope import ...` would fail the CLI path for the mirror
# reason. Inserting the module's own directory satisfies both. Same shape and
# same rationale as gates/capability_route.py — which inserts parent.parent
# because ITS siblings live in core/scripts; this one's sibling is in `gates/`
# itself, so it is parent.
_GATES_DIR = Path(__file__).resolve().parent
if str(_GATES_DIR) not in sys.path:
    sys.path.insert(0, str(_GATES_DIR))
from user_leg_scope import VALID_USER_LEG_SCOPES  # noqa: E402

# ONE sentinel, identical spelling in every lane (step 4). Never rename per
# lane — a per-lane sentinel makes "is this drift or that drift?" a lookup.
SENTINEL = "UNKNOWN"

# The shared scope superset. Every lane's subset is drawn from here, so a
# token means the same thing in all four lanes.
#
# Derived from the LIVE defer corpus on 2026-08-09 (bravo, hostname cc-05,
# uname -r 6.8.0-136-generic), not invented: the 17 credential-lane eligible
# defers and the precondition lane's free-form population were read and
# grouped. Tokens that already existed in VALID_USER_LEG_SCOPES were REUSED
# rather than re-spelled — that reuse is the whole point of the shared set.
DEFER_SCOPES = frozenset(VALID_USER_LEG_SCOPES) | frozenset({
    # ── observed in the credential lane ──
    "iam-permission",        # a permission is denied; the key is an ACTION string,
                             # not an env var (: s3:DeleteObjectVersion
                             # appeared verbatim in BOTH the defer text and the
                             # capability-routing RESOLVED register, and the
                             # env-var-shaped predicate could not see it)
    "env-var",               # a named secret/config value is absent
    "account-access",        # requires signing in as a specific human account
    "policy-prohibition",    # a standing rule forbids the act (not a missing grant)
    # ── observed in both the credential and precondition lanes ──
    "human-window",          # needs a quiesced / scheduled window a human opens
    "human-decision",        # a values or compliance judgment is the deliverable
    "upstream-completion",   # waiting on another goal / run / soak to finish
    "hardware-resource",     # needs a machine or device that does not exist yet
    # ── observed in the precondition lane ──
    "artifact-exists",       # a file / build / record must be present first
    "service-reachable",     # an external service must be up
    "data-available",        # an input dataset must have landed
})

# Per-lane valid subsets. A lane declares WHICH shared tokens it can legally
# carry; it never declares tokens of its own.
#
# Lane `user-leg` is exactly VALID_USER_LEG_SCOPES — imported, so it cannot
# drift from lane P's SSOT without the parity test going red.
LANE_SCOPES = {
    "user-leg": frozenset(VALID_USER_LEG_SCOPES),
    "grant": frozenset(VALID_USER_LEG_SCOPES),
    "precondition": frozenset({
        "artifact-exists", "service-reachable", "data-available",
        "upstream-completion", "human-window", "human-decision",
        "deployment-approval",
    }),
    "credential": frozenset({
        "env-var", "iam-permission", "credential-grant",
        "account-access", "policy-prohibition", "human-window",
        "human-decision", "deployment-approval", "hardware-resource",
    }),
}

# Recognition patterns, ordered MOST-SPECIFIC FIRST. `classify` returns the
# first match, so a generic pattern placed above a specific one would shadow
# it — the ordering is load-bearing, not cosmetic.
#
# These recognize a scope in FREE TEXT. They are the migration path for the
# ~24 existing un-keyable defers; new defers should carry an explicit scope
# field instead of relying on recognition.
_PATTERNS = (
    # IAM action strings are the highest-signal token in the whole corpus:
    # `<service>:<PascalCaseAction>` is unambiguous and appears verbatim in
    # the capability-routing RESOLVED register, which is indexed by permission.
    ("iam-permission", re.compile(r"\b[a-z][a-z0-9-]{1,30}:[A-Z][A-Za-z0-9]{2,}\b")),
    ("iam-permission", re.compile(r"\b(iam|permission|denied to|access ?denied)\b", re.I)),
    ("env-var", re.compile(r"\benv-read(\.sh)?\b|\b[A-Z][A-Z0-9]{2,}(_[A-Z0-9]+)+\b")),
    ("account-access", re.compile(r"\b(sign(ing)? in to|account[- ]owner|owner-action)\b", re.I)),
    # `policy-prohibition` HAS NO PATTERN, deliberately. It stays a DECLARABLE
    # token in DEFER_SCOPES and in the credential lane — a goal may legitimately
    # carry it as a declared scope — but it is NOT recognizable from free text,
    # and that is a measured conclusion rather than a caution.
    #
    # It started as r"\b(guard-\d+|prohibited|forbidden|must not)\b". The
    # `guard-\d+` half came out first: defers CITE guardrails constantly as
    # supporting evidence for an unrelated block, so the citation is not the
    # prohibition (guard-2860). Removing it was necessary and not sufficient.
    # Measured over all 41 non-terminal defers carrying a defer_reason (bravo,
    # hostname cc-05, uname -r 6.8.0-136-generic, 2026-08-09), the surviving
    # alternation won on 4 and was WRONG on all 4 — zero true positives:
    #     'must not'   an ssh-unreachable block; the phrase is in a
    #                            note saying a future sweep must not kill this
    #                            box's own bridges
    #      'must not'   same shape
    #      'MUST NOT'   really hardware-resource (needs a second box)
    #       'prohibited' same shape
    # The cause generalises past this token: a defer_reason is a NARRATIVE. Its
    # blocking reason sits in the prefix, and the body carries progress notes and
    # constraints on HOW to do the work. A prohibition phrase in a constraint note
    # is not the reason the goal is blocked, and no wording of this pattern can
    # tell the two apart.
    #
    # THE OBVIOUS REMEDY WAS TESTED AND IS WORSE — do not re-derive it. Matching
    # only the declarative HEAD (first sentence, 220 chars) is the discipline
    # guard-1802/rb-5650 established for standing-grant scope cells, and applied
    # here it changes 16 of 41 verdicts: 12 whole-text HITs become head MISSES and
    # ZERO are gained. Grant cells are short declarative prose; defer narratives
    # are not, and they bury the joinable key (the IAM action, the service name)
    # deep in the body. Same discipline, opposite corpus, opposite answer.
    # (Removed 2026-08-09 by the  fresh-eyes pass.)
    ("human-window", re.compile(r"\b(quiesc\w*|maintenance window|merge-and-deploy window)\b", re.I)),
    ("deployment-approval", re.compile(r"\b(deploy\w*[- ]approval|held until|go-ahead|greenlit|greenlight)\b", re.I)),
    ("upstream-completion", re.compile(r"\b(after .{0,24}completes?|soak ?#?\d|blocked on g-\d+)\b", re.I)),
    ("hardware-resource", re.compile(r"\b(physical box|second machine|hardware)\b", re.I)),
    ("human-decision", re.compile(r"\b(values|compliance|judgment call|judgement call)\b", re.I)),
    ("artifact-exists", re.compile(r"\b(file .{0,20}exists?|artifact .{0,20}present|build output)\b", re.I)),
    ("service-reachable", re.compile(r"\b(reachable|unreachable|endpoint .{0,16}(up|down))\b", re.I)),
    ("data-available", re.compile(r"\b(dataset|data .{0,16}(landed|available))\b", re.I)),
)


def lanes():
    """Declared lane names, sorted. The four-lane roster is this module's."""
    return sorted(LANE_SCOPES)


def classify(lane: str, text: Optional[str]) -> str:
    """TOTAL function: a declared token for `lane`, or SENTINEL.

    Never returns an undeclared value and never coerces to the most common
    token. An unknown lane is itself SENTINEL rather than an exception — the
    caller is usually a sweep that must not crash on a new lane name.
    """
    allowed = LANE_SCOPES.get(lane)
    if not allowed or not text:
        return SENTINEL
    blob = str(text)
    for token, pattern in _PATTERNS:
        if token in allowed and pattern.search(blob):
            return token
    return SENTINEL


def classify_any(text: Optional[str]) -> str:
    """Lane-agnostic classify: the first declared token any lane would accept.

    Exists for text whose LANE cannot be determined. Knowing "the scope is
    recognizable, only the lane is not" is strictly more information than a
    bare unrouted count, and it is reachable WITHOUT widening the lane router
    — widening that router would mean keying the lane off the same prose whose
    un-keyability is the finding.

    Not a substitute for `classify`: it can return a token the goal's real
    lane does not accept, so never store its output as a lane scope.
    """
    if not text:
        return SENTINEL
    blob = str(text)
    for token, pattern in _PATTERNS:
        if pattern.search(blob):
            return token
    return SENTINEL


def undeclared(lane: str, text: Optional[str], *, excerpt: int = 120) -> Optional[dict]:
    """Step 5: surface the TEXT behind a sentinel classification, not a count.

    Returns None when `text` classifies to a declared token (nothing to
    surface). Otherwise returns the lane, the excerpt, and the lane's allowed
    tokens, so the reader can see what a producer is actually writing that the
    vocabulary does not cover.
    """
    if classify(lane, text) != SENTINEL:
        return None
    return {
        "lane": lane,
        "verdict": SENTINEL,
        "observed": (str(text or "")[:excerpt]),
        "allowed": sorted(LANE_SCOPES.get(lane) or ()),
    }
