# domain-leak-exempt: regex patterns enumerate the git-state-absence negation
# family (incl. the domain term "studio") — functional detector patterns, not
# pedagogical examples.
"""Single source of truth for the git-state-absence negation pattern family.

Both ``verify-before-assuming-gate.py`` and ``exhaustive-search-gate.py`` fire on
version-control capability-absence claims ("absent from git", "only in Studio",
"not committed"). "It's not committed / only in Studio" is a capability-absence
claim just like "isn't built" — it froze g-350-63 for ~2 days on a FALSE premise
(the code was committed 5 days earlier). Firing the gate forces a
``git log --all -- <path>`` / HEAD grep before the claim is accepted. Maps to
verify-before-assuming.md "Capability-Absence Claims".

These 7 patterns were previously duplicated verbatim in BOTH gates and hand-synced
("kept in sync with X" comments) — a manual-sync smell (communication-clarity.md
rule 5: single source of truth). g-248-116 added the base forms to both; g-115-2959
added the same 6 more to both (the dual-edit twice). This module makes the SOURCE
single; each gate splices ``*GIT_STATE_ABSENCE_PATTERNS`` into its own
``_TRIGGER_PATTERNS`` list. test_git_state_absence_negation_gate.py guards runtime
parity; this eliminates the drift risk structurally (g-115-2963).

Scope: the git-state-absence family ONLY. Each gate's broader pattern set
legitimately differs (verify carries infra/HTTP negations; exhaustive carries
knowledge-negation forms), so those stay inline per-gate.
"""

import re

GIT_STATE_ABSENCE_PATTERNS = [
    # Base forms (g-248-116)
    re.compile(r"\babsent from git\b", re.IGNORECASE),
    re.compile(r"\bnot (yet )?in git\b", re.IGNORECASE),
    re.compile(r"\bnot committed\b", re.IGNORECASE),
    re.compile(r"\bnever committed\b", re.IGNORECASE),
    re.compile(r"\bexists only (in )?studio\b", re.IGNORECASE),
    re.compile(r"\bstudio[- ]only\b", re.IGNORECASE),
    re.compile(r"\bnot tracked (in git|by git)\b", re.IGNORECASE),
    # g-115-2959: alternate phrasings of the SAME git-absence class the g-248-28
    # classifier-accuracy review (site #4 FN) found STILL escaping after the base
    # forms above — contraction / synonym / word-order variants. FN>>FP generous
    # bias (the class froze g-350-63 ~2 days on a false premise). FP-verified
    # clean against legitimate git mentions ("git status", "git log", "git history").
    re.compile(r"\bisn't (yet )?in git\b", re.IGNORECASE),
    re.compile(r"\b(isn't|is not|not) in (the )?repo(sitory)?\b", re.IGNORECASE),
    re.compile(r"\b(hasn't|has not|haven't|have not) been committed\b", re.IGNORECASE),
    # "pushed" needs a git anchor — bare "never pushed" FP's on "pushed the
    # button", so require an explicit remote target or the unambiguous "been pushed".
    re.compile(r"\b(never|not yet|not) pushed to (git|remote|origin|upstream|the repo(sitory)?|main|master)\b", re.IGNORECASE),
    re.compile(r"\b(hasn't|has not|haven't|have not) been pushed\b", re.IGNORECASE),
    re.compile(r"\bmissing from (git|the repo|the repository)\b", re.IGNORECASE),
    re.compile(r"\bonly exists in studio\b", re.IGNORECASE),
]
