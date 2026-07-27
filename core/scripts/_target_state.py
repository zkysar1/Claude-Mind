"""Shared library: extract target files + identifiers from a goal
description, then probe whether those identifiers already appear in the
target files.

Consumers:
  - goal-duplication-gate.py (filing-time hard block, 4th check)
  - target-state-probe.py    (execution-time advisory)

Fail-open: missing files, unreadable text, and failed extraction all
produce verdict="unknown" — callers must treat "unknown" as no-op.

Origin: 2026-04-20 g-115-141 incident (fix predated goal filing; grep
would have caught the already-done state before execution).
"""

import os
import re
from pathlib import Path

from _gate_log import log as _gate_log

# File-path regex aligned with goal-duplication-gate's _FILE_PATH_RE:
# word boundary, word-char start, common source extensions.
_FILE_PATH_RE = re.compile(
    r"\b([\w][\w./-]*\.(?:py|sh|md|yaml|yml|json|jsonl|ts|tsx|js|lua|go|java|toml))\b"
)

# file.ext:N line-hint regex (re-uses FILE_PATH + ":" + digits).
_LINE_HINT_RE = re.compile(
    r"\b([\w][\w./-]*\.(?:py|sh|md|yaml|yml|json|jsonl|ts|tsx|js|lua|go|java|toml))"
    r":(\d{1,6})\b"
)

# Backtick-quoted tokens. Non-greedy content up to 120 chars so we don't
# swallow a whole code block.
_BACKTICK_RE = re.compile(r"`([^`\n]{2,120})`")

# Zero-arg call in prose: `frobnicate()` style. Requires leading letter or
# underscore, length >= 4, followed by "()".
_CALL_RE = re.compile(r"\b([A-Za-z_][\w]{3,})\(\)")

# snake_case identifier with >=1 underscore, length >= 5.
_SNAKE_RE = re.compile(r"\b([a-z_][a-z0-9]*(?:_[a-z0-9]+){1,})\b")

# CamelCase identifier, length >= 6 (avoids "HTTP", "AWS", "JSON", etc.).
_CAMEL_RE = re.compile(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+){1,})\b")

# Stopwords applied to snake_case + CamelCase extraction. Backticked and
# call-shaped tokens are trusted verbatim (the author used quotes for a
# reason).
_STOPWORDS = {
    "this_goal", "this_aspiration", "goal_id", "aspiration_id",
    "knowledge_tree", "reasoning_bank", "reasoning_banks",
    "world_dir", "world_path", "meta_path",
    "working_memory", "session_state",
    "todo_list", "sub_goal", "line_number",
    "SkillSet", "SkillDef", "GoalSet", "GoalDef",
}

# Guardrails: keep extraction bounded.
_MAX_FILES = 6
_MAX_IDENTIFIERS = 12
_MAX_FILE_BYTES = 512 * 1024  # 512 KB — skip bigger files rather than hang.


# Paths that DESCRIBE work rather than IMPLEMENT it. When these appear
# alongside implementation files in target_files, probe_target_state's
# union-across-files aggregation produces false-positive "already_present"
# verdicts: the report describes what to do; the impl file is where the
# work goes. Exclude these from probe targets so the aggregate hit_ratio
# reflects only implementation files.
#
# Discovered 2026-04-25 () when filing  follow-up to
#  audit — bravo/reports/vertx-process-audit-2026-04-25.md had
# all 5 identifiers, Driver.java had 0, aggregate hit_ratio=1.0 blocked
# the impl goal. Per-file breakdown showed the problem clearly but the
# verdict aggregated across files.
_DOCUMENTATION_ONLY_PATTERNS = [
    # temp/ briefings (fresh-eyes, felt-sense) are description-of-work staging
    # files, not implementation targets. (This superseded the legacy reports/
    # pattern: the file-model normalization moved briefings reports/ -> temp/,
    # and reports/ was abolished 2026-06-02 — git history is its archive.)
    re.compile(r"(?:^|/)temp/.*\.md$"),
]


def _is_documentation_only_path(fp):
    """True iff fp is a description-of-work file, not an implementation target.

    Used to filter target_files in extract_targets so audit reports cannot
    contaminate the probe's aggregate hit_ratio (g-001-191).
    """
    return any(p.search(fp) for p in _DOCUMENTATION_ONLY_PATTERNS)


# READ-intent goal title detector (rb-398 follow-through).
#
# Some goal types READ their target files rather than write to them:
# Investigate, Audit, Review, Observe, Research, Analyze — plus compound
# "<agent>'s work review:" titles. For these the target_state check is
# semantically INVERTED: identifiers appearing in target files is a
# PRECONDITION of the work (you need code to audit it), not a "fix already
# shipped" signal. Without this carve-out, a well-formed Investigate goal
# that lists 12 real identifiers in 3 real files trips hit_ratio=1.0 and
# gets blocked — the opposite of the intended semantic.
#
# Match rule: ANY whitespace-separated word in the pre-colon title segment
# matches READ_INTENT_VERBS, case-insensitive, after stripping possessive
# "'s" / "'" suffixes. Catches:
#   "Investigate: X"                 → word "investigate"  → exempt
#   "Review hypothesis: X"           → word "review"       → exempt
#   "Alpha's work review: X"         → word "review"       → exempt
#   "Fix: X"                         → word "fix"          → apply check
#   "Idea: add Y"                    → word "idea"         → apply check
#   "Fix: review the retry logic"    → prefix "Fix", no colon match → apply
#                                       check (review is AFTER colon,
#                                       primary intent is Fix)
#
# Single source of truth — both goal-duplication-gate.py (filing-time
# blocker) and target-state-probe.py (execution-time advisory) call
# is_read_intent() so they cannot diverge on what counts as READ-intent.

READ_INTENT_VERBS = frozenset({
    "investigate", "audit", "review", "observe", "research", "analyze",
    # Added 2026-05-10 () per  weekly classifier-accuracy
    # scan: 28 'probe' + 18 'scan' read-intent goal completions in 7 days
    # were missed (FN). Both verbs consistently used for diagnostic/detection
    # actions in observed sample. 'check' (14 instances) intentionally NOT
    # added — ambiguous between read-intent and 'check and fix' write-intent.
    "probe", "scan",
})


def is_read_intent(title, _caller="unknown"):
    """Return True if the goal's title prefix segment contains a READ-verb.

    Prefix segment is everything before the first ':'. If no colon, the
    whole title. Words are lowercased and possessive suffixes stripped
    before matching READ_INTENT_VERBS.

    `_caller` is a callsite label used only for telemetry (gate firing log).
    Pass a stable string from each consumer so the dashboard can attribute
    matches/misses to goal-duplication-gate vs target-state-probe.
    """
    if not title:
        return False
    prefix = title.split(":", 1)[0]
    # Positional `check` rule (, 2026-05-17): `check` as the FINAL word
    # of the title prefix is read-intent. Prior cycle (, 2026-05-10)
    # excluded `check` from READ_INTENT_VERBS due to ambiguity ('Strategic
    # vision check' = read-intent vs 'Added verify-learning check' =
    # write-intent).  classifier scan 2026-05-17 found 15 FN on
    # `check`; disambiguating pattern: read-intent when LAST word of prefix.
    # Cross-ref: rb-648 (verify named hook target),  (FN measurement).
    words = prefix.split()
    if words:
        last_w = words[-1].lower()
        if last_w.endswith("'s"):
            last_w = last_w[:-2]
        elif last_w.endswith("'"):
            last_w = last_w[:-1]
        if last_w == "check":
            _gate_log("read-intent-verbs", "pass",
                      caller=_caller,
                      trigger_matched="check",
                      payload=title[:200],
                      extra={"position": "final-prefix-word"})
            return True
    # Verb-matching ( finding 4): a COLON title matches any pre-colon
    # (label-segment) word; a NO-COLON title matches the LEADING word only — a
    # read verb in a subordinate clause of a colon-less title ("Refactor and
    # review the API") is not the primary action and must not over-exempt the
    # check. The check-positional rule above is SEPARATE and stays whole-title
    # (test_strategic_vision_check_no_colon pins "Strategic vision check" True).
    verb_words = prefix.split() if ":" in title else prefix.split()[:1]
    for word in verb_words:
        w = word.lower()
        if w.endswith("'s"):
            w = w[:-2]
        elif w.endswith("'"):
            w = w[:-1]
        if w in READ_INTENT_VERBS:
            # gate_id MUST match core/config/gates.yaml id.
            # "pass" = read-intent detected → caller will exempt downstream dup check.
            _gate_log("read-intent-verbs", "pass",
                      caller=_caller,
                      trigger_matched=w,
                      payload=title[:200])
            return True
    # "noop" = no verb match → caller proceeds with default (non-exempted) behavior.
    _gate_log("read-intent-verbs", "noop",
              caller=_caller,
              trigger_matched=None,
              payload=title[:200])
    return False


# REMOVAL-intent goal title detector (, sibling of READ_INTENT_VERBS).
#
# Removal goals (retire / remove / delete / deprecate / strip / drop / purge)
# invert the target_state semantic the same way READ goals do, from the other
# side: the named identifiers are present in the target files BECAUSE THEY ARE
# THE REMOVAL TARGET. Presence means the work is NOT done; absence would mean
# it is. Without this carve-out every retirement goal trips hit_ratio=1.0 and
# needs a manual --override-duplication (3 FP overrides in session 104 alone:
# , ,  — the canonical shape is 
# "Apply: retire stale-read-gate", where the full removal scope list IS the
# identifier set).
#
# Match rule differs from READ-intent in ONE position: removal verbs are
# ACTION verbs, not prefix LABELS. Observed removal titles carry a generic
# label prefix ("Apply:", "Maintain:", "Unblock:") with the removal verb as
# the FIRST word after the colon — the position that names the goal's primary
# action. So is_removal_intent matches:
#   (a) any word in the pre-colon segment (mirrors is_read_intent — covers
#       "Retire stale-read-gate" / "Remove X: scope"), OR
#   (b) the FIRST word immediately after the first colon (covers
#       "Apply: retire stale-read-gate", "Maintain: remove dead flag").
# Deliberately NOT any-word-after-colon: "Fix: review then remove the flag"
# keeps the check (primary intent is the leading verb, not a later clause) —
# same conservatism as is_read_intent's "Fix: review the retry logic" example.
# Retirement work is a first-class lane (learning-philosophy rule 5); this
# keeps it filing-friction-free without loosening the gate for mixed intent.

REMOVAL_INTENT_VERBS = frozenset({
    "retire", "remove", "delete", "deprecate", "strip", "drop", "purge",
})


def _normalize_title_word(word):
    """Lowercase + strip possessive suffixes and trailing punctuation."""
    w = word.lower().strip(",.;:!?—–-")
    if w.endswith("'s"):
        w = w[:-2]
    elif w.endswith("'"):
        w = w[:-1]
    return w


def is_removal_intent(title, _caller="unknown"):
    """Return True if the goal title's primary action is a REMOVAL verb.

    Matches a removal verb (a) anywhere in the pre-colon segment, or (b) as
    the first word after the first colon. See block comment above for why
    position (b) exists (removal verbs are action verbs behind generic
    label prefixes, unlike READ verbs which ARE the label).

    `_caller` is a callsite label for telemetry (gate firing log), same
    contract as is_read_intent.
    """
    if not title:
        return False
    if ":" in title:
        prefix, _, rest = title.partition(":")
        candidates = [(_normalize_title_word(w), "pre-colon") for w in prefix.split()]
        rest_words = rest.split()
        if rest_words:
            candidates.append((_normalize_title_word(rest_words[0]),
                               "leading-post-colon"))
            # Adverb-prefixed removal (): an adverbial modifier can
            # delay the removal verb into the SECOND post-colon slot
            # ("Apply: fully retire X", "Maintain: safely delete Y" — sq-016
            # surfaced this FP class re-opening). Admit the 2nd word as a
            # candidate ONLY when the 1st post-colon word is an adverbial
            # modifier (ends in "ly"). Gating on -ly is deliberately narrower
            # than the blanket "first-two-post-colon-words" widen the goal
            # proposed: the blanket form over-exempts noun-phrase IMPLEMENTATION
            # titles where a removal verb is the second word of a compound noun
            # — "Add: soft delete support", "Fix: hard delete perf" ("soft" /
            # "hard" are adjectives, not -ly adverbs) — which are genuine
            # target_state goals, not removals. (guard-958: prefer a surgical
            # context-disqualifier over a broad widen; verify precision with an
            # adversarial control — test_add_soft_delete_not_removal below.)
            if len(rest_words) >= 2 and _normalize_title_word(rest_words[0]).endswith("ly"):
                candidates.append((_normalize_title_word(rest_words[1]),
                                   "adverb-delayed-post-colon"))
    else:
        # No colon ( fresh-eyes finding 4, board msg-3199/3200): a
        # removal verb ANYWHERE in a colon-less title is a subordinate clause,
        # not the primary action — matching any word over-exempts the check
        # ("Scan and remove orphaned rows" is a scan-and-fix goal, not a
        # removal). The primary action of a colon-less title is its LEADING
        # word only, mirroring the finding-4 fix applied to is_read_intent.
        words = title.split()
        candidates = ([(_normalize_title_word(words[0]), "no-colon-first-word")]
                      if words else [])
    for w, position in candidates:
        if w in REMOVAL_INTENT_VERBS:
            # "pass" = removal-intent detected → caller exempts the dup check.
            _gate_log("removal-intent-verbs", "pass",
                      caller=_caller,
                      trigger_matched=w,
                      payload=title[:200],
                      extra={"position": position})
            return True
    # "noop" = no removal verb in matched positions → default behavior.
    _gate_log("removal-intent-verbs", "noop",
              caller=_caller,
              trigger_matched=None,
              payload=title[:200])
    return False


# MODIFY-intent goal title detector (, sibling of REMOVAL_INTENT_VERBS).
#
# Modify goals (fix / extend / wire / harden / consolidate / refactor / ...)
# NAME the existing symbols they change — those identifiers are the modification
# SUBJECT, present in the target file BEFORE the work (that IS what is being
# changed), not a completion signal. echo's  quantification: 71%
# (37/52) of target_state SOLO-block overrides since 2026-07-04 were
# subject-not-deliverable FPs on modify-verb goals (citation-shape 16,
# modification-surface 11, test-absence 6, union-masks-miss 4).
#
# CRITICAL DIFFERENCE from read/removal intent — the caller DEMOTES, not skips.
# Read/removal presence INVERTS the completion semantic (identifiers present
# means the work is NOT done), so a full skip is safe. Modify-presence is
# AMBIGUOUS: the symbol is present both before AND after the modification, so
# presence proves neither duplication NOR completion. _check_target_state
# therefore demotes the target_state BLOCK to a visible advisory for
# modify-intent (keeps the match visible, drops the hard --override requirement)
# rather than skipping the probe like is_read_intent / is_removal_intent do.
#
# Match rule mirrors is_removal_intent (modify verbs are ACTION verbs behind
# generic label prefixes like Apply:/Maintain:): matches (a) any word in the
# pre-colon segment, (b) the first word after the first colon, or (c) an
# adverb-delayed verb in the second post-colon slot.

MODIFY_INTENT_VERBS = frozenset({
    "fix", "extend", "wire", "harden", "consolidate", "serialize", "repair",
    "persist", "tune", "migrate", "refactor", "integrate", "rewire",
    #  (2026-07-22, zeta): evidence-based expansion. The original 13
    # verbs demoted only 99 of 565 target_state SOLO-block overrides in the
    # ledger; 386 stayed UNCOVERED and kept HARD-blocking. Classifying every
    # target_state override by action verb isolated a modify-not-create tail
    # absent from the set — each NAMES an existing symbol it changes (present
    # pre- AND post-edit, the SAME ambiguity the original 13 handle), so DEMOTE
    # (not skip) is the correct treatment. Added = the goal's own cited examples
    # (edit / tighten / refine / modify) + the ledger verbs carrying >=2 override
    # FPs (update / replace / normalize / move / bump / restore / flip /
    # instrument / backfill / hydrate). CREATE verbs (add / create / implement /
    # introduce / build) stay DELIBERATELY EXCLUDED — for those, identifier
    # presence IS duplication evidence and must still hard-block (see the
    # create-exclusion negatives in test_target_state_modify_intent.py). The
    # remaining uncovered classes are DISTINCT problems handled separately, NOT
    # folded here: "add" (31 FPs, create-with-cited-context), noun-led titles
    # (action verb is not the first post-colon word), and run-intent (13 FPs,
    # execute-an-existing-script).
    "edit", "tighten", "refine", "modify",
    "update", "replace", "normalize", "move", "bump", "restore",
    "flip", "instrument", "backfill", "hydrate",
    #  (2026-07-23, zeta): prose-led residual catalog additions. Both
    # NAME an existing subject they change (present pre- AND post-edit — the
    # modify DEMOTE ambiguity), isolated from the target_state SOLO residual:
    #   'switch' — "Switch utilization-gate.sh to call ..." (change a call target)
    #   'document' — "Document mode-name divergence between session.py ... and
    #     skill-structure-gate.py" (the named code symbols are the SUBJECT,
    #     present before the doc is written). Routed to MODIFY (DEMOTE), NOT the
    #     goal's suggested READ: is_read_intent SKIPS (full exempt), which would
    #     wrongly bypass target_state for a "Document <doc-that-exists>" dup;
    #     DEMOTE keeps the match visible while dropping only the hard --override.
    "switch", "document",
})


def is_modify_intent(title, _caller="unknown"):
    """Return True if the goal title's primary action is a MODIFY verb.

    Matches a modify verb (a) anywhere in the pre-colon segment, (b) as the
    first word after the first colon, or (c) an adverb-delayed verb in the
    second post-colon slot. Same position contract + normalization as
    is_removal_intent (single source of truth in _target_state). The caller
    DEMOTES the target_state block to a visible advisory rather than skipping,
    because modify-presence is ambiguous (pre- vs post-modification); g-115-2565.

    `_caller` is a callsite label for telemetry (gate firing log), same
    contract as is_read_intent / is_removal_intent.
    """
    if not title:
        return False
    if ":" in title:
        prefix, _, rest = title.partition(":")
        candidates = [(_normalize_title_word(w), "pre-colon") for w in prefix.split()]
        rest_words = rest.split()
        if rest_words:
            candidates.append((_normalize_title_word(rest_words[0]),
                               "leading-post-colon"))
            if len(rest_words) >= 2 and _normalize_title_word(rest_words[0]).endswith("ly"):
                candidates.append((_normalize_title_word(rest_words[1]),
                                   "adverb-delayed-post-colon"))
    else:
        words = title.split()
        candidates = ([(_normalize_title_word(words[0]), "no-colon-first-word")]
                      if words else [])
    for w, position in candidates:
        if _matches_intent_verb(w, MODIFY_INTENT_VERBS):
            # "pass" = modify-intent detected → caller DEMOTES the dup block to advisory.
            _gate_log("modify-intent-verbs", "pass",
                      caller=_caller,
                      trigger_matched=w,
                      payload=title[:200],
                      extra={"position": position})
            return True
    # "noop" = no modify verb in matched positions → default behavior.
    _gate_log("modify-intent-verbs", "noop",
              caller=_caller,
              trigger_matched=None,
              payload=title[:200])
    return False


# BUILD- / TEST-AUTHORING-intent goal title detector (, DEMOTE sibling
# of MODIFY_INTENT_VERBS).
#
# A goal to BUILD a NEW artifact (gate / check / module / script / detector ...)
# or to ADD an integration test names the EXISTING file it will touch — and that
# file's symbols are the integration SURFACE, present in the target file BEFORE
# the work, not the deliverable. Presence proves neither duplication NOR
# completion (the NEW artifact / test is absent; the named existing symbols are
# just what it integrates with or exercises), the SAME ambiguity is_modify_intent
# handles — so the caller DEMOTES the target_state block to a visible advisory
# rather than skipping. Two 2026-07 filings hard-blocked (400) on this exact FP
# and only cleared after re-wording:  ("Idea: goal-creation gate
# refusing ...") and  ("Idea: integration test proving ...").
#
# CRITICAL DIFFERENCE from read/removal/modify — those are VERB-led (the primary
# ACTION verb names the intent). Build/test titles are frequently NOUN-led: the
# deliverable NOUN names the intent with no explicit build verb ("goal-creation
# gate refusing X", "integration test proving Y"). So this detector matches a
# build VERB *or* a deliverable NOUN (or the noun "test") in the LEADING segment.
# The post-colon window is WIDER than the verb detectors' word-1 window — the
# first THREE post-colon words — because a deliverable noun is commonly preceded
# by a compound adjectival modifier ("goal-creation gate", "integration test",
# "the admission gate"). Colon-less titles match the leading word only (same
# subordinate-clause conservatism as is_read/removal/modify_intent). The wider
# window's FP risk is bounded by DEMOTE (not skip): a misread stays a visible
# advisory and the other 4 dup checks still apply.

BUILD_INTENT_VERBS = frozenset({
    "build", "create", "implement", "scaffold", "introduce", "author",
})
# Deliverable nouns naming a NEW framework artifact (the goal's own examples:
# gate / check / module). Matched only in the leading segment so a noun deep in
# a prose clause does not over-exempt.
BUILD_INTENT_NOUNS = frozenset({
    "gate", "check", "module", "script", "helper", "detector", "validator",
    "wrapper", "scanner", "linter", "endpoint", "handler",
})
TEST_AUTHORING_NOUNS = frozenset({"test", "tests"})


def is_build_or_test_authoring_intent(title, _caller="unknown"):
    """Return "build-intent" | "test-authoring" if the goal's deliverable is a
    NEW artifact (build noun/verb) or a test (test noun) that TOUCHES an existing
    file, else None. Caller DEMOTES the target_state block to a visible advisory
    (same ambiguity contract as is_modify_intent — the named existing symbols are
    the integration surface, present before the work; g-115-2869).

    Candidate positions: every pre-colon word + the first THREE post-colon words
    (wider than the verb detectors because a deliverable noun carries a compound
    modifier), or — for a colon-less title — the leading word only. `_caller` is
    a telemetry callsite label, same contract as the sibling intent detectors.
    """
    if not title:
        return None
    if ":" in title:
        prefix, _, rest = title.partition(":")
        cand = [_normalize_title_word(w) for w in prefix.split()]
        cand += [_normalize_title_word(w) for w in rest.split()[:3]]
    else:
        words = title.split()
        cand = [_normalize_title_word(words[0])] if words else []
    # Test-authoring is the more specific class — check it first so a title that
    # names both a test and a generic build noun classifies as test-authoring.
    for w in cand:
        if w in TEST_AUTHORING_NOUNS:
            _gate_log("build-test-authoring-intent", "pass",
                      caller=_caller,
                      trigger_matched=w,
                      payload=title[:200],
                      extra={"intent_class": "test-authoring"})
            return "test-authoring"
    for w in cand:
        if w in BUILD_INTENT_VERBS or w in BUILD_INTENT_NOUNS:
            _gate_log("build-test-authoring-intent", "pass",
                      caller=_caller,
                      trigger_matched=w,
                      payload=title[:200],
                      extra={"intent_class": "build-intent"})
            return "build-intent"
    _gate_log("build-test-authoring-intent", "noop",
              caller=_caller,
              trigger_matched=None,
              payload=title[:200])
    return None


# ─── : target_state FP classes 2-3 (DEMOTE carve-outs) ─────────────
# Ledger analysis of world/goal-duplication-overrides.jsonl (142 SOLO / 565 ANY
# target_state overrides) after  fixed the modify-verb tail isolated
# THREE more distinct FP classes a verb-list extension cannot reach. Each is an
# ORTHOGONAL detector wired as its own DEMOTE branch in
# gates/goal_duplication.py::_check_target_state — none reverses an existing
# carve-out (is_build_or_test_authoring's "add X to Y -> None" boundary tests
# stay valid; the add-to-surface detector is separate). All DEMOTE (keep the
# match visible, drop the hard --override requirement) rather than SKIP, because
# the cited identifier's presence is AMBIGUOUS (surface/precondition vs
# deliverable), the same posture as is_modify_intent / is_build_or_test_authoring.


# RUN-intent (class 3a; 4 SOLO / 16 ANY). "Execute provision_aws.py ...",
# "Apply: run -style forced-action verification ..." — the named script's
# presence is a PRECONDITION (you cannot run a script that does not exist), not
# run-completion. Unlike read/removal (which SKIP because presence INVERTS
# completion), a run does not change the script's presence, so presence is simply
# IRRELEVANT to run-completion -> DEMOTE. Match positions mirror is_modify_intent.
RUN_INTENT_VERBS = frozenset({
    "run", "execute", "rerun", "re-run", "invoke", "trigger", "launch",
    "dispatch", "exec", "fire",
})


def is_run_intent(title, _caller="unknown"):
    """Return True if the title's primary action is a RUN/EXECUTE verb (the named
    script's presence is a precondition, not run-completion; caller DEMOTES —
    g-248-119). Positions mirror is_modify_intent."""
    if not title:
        return False
    if ":" in title:
        prefix, _, rest = title.partition(":")
        candidates = [(_normalize_title_word(w), "pre-colon") for w in prefix.split()]
        rest_words = rest.split()
        if rest_words:
            candidates.append((_normalize_title_word(rest_words[0]), "leading-post-colon"))
            if len(rest_words) >= 2 and _normalize_title_word(rest_words[0]).endswith("ly"):
                candidates.append((_normalize_title_word(rest_words[1]), "adverb-delayed-post-colon"))
    else:
        words = title.split()
        candidates = ([(_normalize_title_word(words[0]), "no-colon-first-word")] if words else [])
    for w, position in candidates:
        if _matches_intent_verb(w, RUN_INTENT_VERBS):
            _gate_log("run-intent-verbs", "pass", caller=_caller, trigger_matched=w,
                      payload=title[:200], extra={"position": position})
            return True
    _gate_log("run-intent-verbs", "noop", caller=_caller, trigger_matched=None,
              payload=title[:200])
    return False


# ADD-TO-SURFACE (class 2; 14 SOLO / 48 ANY). "Idea: add active-movement composite
# to MovementAnalyzer", "Apply: add --outcome to recurring-close.sh verify call" —
# ADDS a NEW deliverable but names an EXISTING integration surface (the cited
# target, present before the work). 'add' is DELIBERATELY excluded from BOTH
# MODIFY_INTENT_VERBS and BUILD_INTENT_VERBS (for a create verb, identifier
# presence CAN be duplication evidence — "Add: new feature flag" where the flag
# exists IS a dup). The discriminator between create-with-cited-CONTEXT and
# create-that-IS-a-dup is the INTEGRATION PREPOSITION: it marks the cited target
# as the surface X integrates INTO. A bare "Add: new feature flag" (no
# preposition) has no cited surface, so it stays blockable. ORTHOGONAL to
# is_build_or_test_authoring_intent (whose "add X to Y -> None" contract is
# UNCHANGED — the two test_add_*_not_build boundary tests stay valid), so no
# existing carve-out is reversed.
ADD_TO_SURFACE_VERBS = frozenset({"add", "append", "register"})
_INTEGRATION_PREPOSITIONS = frozenset({"to", "into", "onto", "for", "in", "within", "under"})


# ── Past-tense completed-work verb stemming () ─────────────────────
# The target_state prose-led residual includes completed-work records that lead
# with a PAST-TENSE verb the present-tense verb sets miss — canonically
# "Maintain: Replaced productivity-stop-gate encoding_ratio ..." ('replace' is
# catalogued in MODIFY_INTENT_VERBS, 'replaced' is not), so the goal STILL
# hard-blocks as a false positive. A verb-aware stemmer maps a past-tense form
# to its base ONLY WHEN that base is a known intent verb — so it can NEVER
# fabricate a verb from a non-verb (the adversarial-negative constraint from the
# goal: "Add: embed-ded config" -> 'embedded'->'embed' ∉ any set stays put;
# 'need'/'speed'/'proceed'/'field' likewise never become verbs). Consumed by
# is_modify_intent + is_run_intent (both DEMOTE — low-harm advisory) via
# _matches_intent_verb; deliberately NOT wired into is_read/is_removal (which
# SKIP = full exempt, higher stakes, and carry zero past-tense residual in the
# ledger). _ALL_INTENT_VERBS is the base-membership oracle: a candidate base
# absent from it leaves the word unchanged.
_ALL_INTENT_VERBS = (
    REMOVAL_INTENT_VERBS | MODIFY_INTENT_VERBS | RUN_INTENT_VERBS
    | READ_INTENT_VERBS | ADD_TO_SURFACE_VERBS | BUILD_INTENT_VERBS
)


def _past_tense_base(word):
    """Return the base form of a regular past-tense verb IFF that base is a
    known intent verb, else return `word` unchanged.

    Handles three regular-inflection shapes, checked against _ALL_INTENT_VERBS:
      - strip 'd'  (base ends in 'e'):  replaced->replace, moved->move, tuned->tune
      - strip 'ed' (base ends in cons): fixed->fix, hardened->harden, bumped->bump
      - doubled final consonant:        flipped->flip, dropped->drop, stripped->strip
    Never fabricates a verb: if no candidate base is in _ALL_INTENT_VERBS the
    original `word` is returned, so a non-verb ('embedded', 'need', 'speed')
    can never be turned into a verb. Assumes `word` is already lowercased +
    stripped by _normalize_title_word / the caller's inline normalization.
    """
    if not word or word in _ALL_INTENT_VERBS or not word.endswith("d"):
        return word
    cands = [word[:-1]]                       # strip 'd' (base ends in 'e')
    if word.endswith("ed"):
        stem = word[:-2]                      # strip 'ed'
        cands.append(stem)
        if len(stem) >= 2 and stem[-1] == stem[-2] and stem[-1].isalpha():
            cands.append(stem[:-1])           # collapse doubled final consonant
    for c in cands:
        if c in _ALL_INTENT_VERBS:
            return c
    return word


def _matches_intent_verb(word, verb_set):
    """True if `word` — or its verb-aware past-tense base — is in `verb_set`.

    The single membership primitive for the DEMOTE verb detectors so present-
    and past-tense forms match identically (g-248-120). Falls back to plain
    membership for non-past-tense words (the stemmer returns them unchanged).
    """
    return word in verb_set or _past_tense_base(word) in verb_set


def is_add_to_surface_intent(title, _caller="unknown"):
    """Return True for "add/append/register X <prep> <cited surface>" — a NEW
    deliverable integrated into an EXISTING cited surface, so the surface
    identifiers are context (present pre-work), not duplication. Caller DEMOTES
    (g-248-119). Leading verb mirrors is_modify_intent; the integration
    preposition may appear anywhere after the verb. A bare add with no
    integration preposition returns False (stays blockable)."""
    if not title:
        return False
    if ":" in title:
        prefix, _, rest = title.partition(":")
        lead_candidates = [_normalize_title_word(w) for w in prefix.split()]
        rest_words = rest.split()
        if rest_words:
            lead_candidates.append(_normalize_title_word(rest_words[0]))
        body_words = [_normalize_title_word(w) for w in rest_words]
    else:
        words = title.split()
        lead_candidates = [_normalize_title_word(words[0])] if words else []
        body_words = [_normalize_title_word(w) for w in words]
    has_add = any(w in ADD_TO_SURFACE_VERBS for w in lead_candidates)
    has_prep = any(w in _INTEGRATION_PREPOSITIONS for w in body_words)
    if has_add and has_prep:
        _gate_log("add-to-surface-intent", "pass", caller=_caller,
                  trigger_matched="add+prep", payload=title[:200])
        return True
    _gate_log("add-to-surface-intent", "noop", caller=_caller,
              trigger_matched=None, payload=title[:200])
    return False


# CODE-TARGET-LEAD (class 3b; the LARGEST uncovered class — 48 of 93 noun-led
# SOLO / correspondingly the biggest ANY slice). When a title LEADS (first
# post-colon token, or first word if colon-less) with a CODE IDENTIFIER — a
# filename (foo.py / bar.sh), a dotted symbol path
# (self_drift_gate.target_aspiration_id), a hyphenated script/compound
# (blocker-recheck.py, tree-fm-backfill), snake_case, or CamelCase — the goal is
# NAMING THE FILE/SYMBOL IT OPERATES ON. That target is present BEFORE and AFTER
# the change (it IS what is edited), so a solo target_state block is a
# subject-not-deliverable FP, the same ambiguity is_modify_intent handles ->
# DEMOTE. This corrects rb-4732's estimate that class 3 was the SMALLEST tail (13
# FPs): the ledger shows it is the LARGEST (93 SOLO / 355 ANY). Create-guard: a
# CREATE verb in the pre-colon segment ("Build: new_module.py") means the file is
# the deliverable, not an edit target — return False (genuine creation stays
# blockable; build-intent already demotes true build titles upstream).
_CODE_ID_CREATE_GUARD = frozenset({
    "add", "create", "implement", "introduce", "build", "scaffold",
    "generate", "author", "design", "forge", "new",
})


def _looks_like_code_identifier(tok):
    """True if a leading token has a code-identifier SHAPE: file extension,
    dotted symbol path, hyphenated compound (>=2 parts), snake_case, or
    CamelCase. A bare single lowercase English word returns False. NOT
    lowercased by the caller (CamelCase detection needs original case)."""
    if not tok:
        return False
    if re.search(r"\.(py|sh|lua|json|ya?ml|md|js|ts|txt|cfg|ini|toml)$", tok):
        return True
    if "." in tok and re.match(r"^[A-Za-z_][\w.]*\w$", tok):
        return True   # dotted symbol path (a.b.c)
    if "-" in tok and len([p for p in tok.split("-") if p]) >= 2:
        return True   # hyphenated compound (blocker-recheck, tree-fm-backfill)
    if "_" in tok:
        return True   # snake_case
    if re.match(r"^[A-Z][a-z]+[A-Z]", tok):
        return True   # CamelCase (MovementAnalyzer)
    return False


def is_code_target_lead_intent(title, _caller="unknown"):
    """Return True if the title LEADS with a code-identifier token (the file/
    symbol it operates on) and NO create verb precedes the colon. Caller DEMOTES
    the target_state block to a visible advisory (g-248-119)."""
    if not title:
        return False
    if ":" in title:
        prefix, _, rest = title.partition(":")
        pre_words = [_normalize_title_word(w) for w in prefix.split()]
        rest_words = rest.split()
        lead = rest_words[0] if rest_words else ""
    else:
        pre_words = []
        words = title.split()
        lead = words[0] if words else ""
    # Create-guard: a create verb before the colon means the leading code-id is
    # the NEW deliverable, not an edit target — keep it blockable.
    if any(w in _CODE_ID_CREATE_GUARD for w in pre_words):
        _gate_log("code-target-lead-intent", "noop", caller=_caller,
                  trigger_matched=None, payload=title[:200],
                  extra={"reason": "create-verb-pre-colon"})
        return False
    # Strip only surrounding quotes/backticks + trailing sentence punctuation —
    # NOT _normalize_title_word (it lowercases, breaking CamelCase detection).
    lead_clean = lead.strip("`'\"").rstrip(",;")
    if _looks_like_code_identifier(lead_clean):
        _gate_log("code-target-lead-intent", "pass", caller=_caller,
                  trigger_matched=lead_clean[:60], payload=title[:200])
        return True
    _gate_log("code-target-lead-intent", "noop", caller=_caller,
              trigger_matched=None, payload=title[:200])
    return False


def _clean_identifier(raw):
    """Normalize a backtick-extracted token to a bare identifier or None.

    `foo.bar()` -> "foo.bar"; `foo = 1` -> "foo"; `foo bar baz` -> None
    (whitespace-containing phrases are prose, not identifiers).
    """
    s = raw.strip()
    if not s:
        return None
    # Drop trailing () or () {} — keep the name.
    s = re.sub(r"\(\s*\)\s*\{?\s*$", "", s).strip()
    # If it still contains whitespace, it's a phrase, not an identifier.
    if re.search(r"\s", s):
        return None
    # Require at least one letter or underscore as the first char, length >= 3.
    if len(s) < 3 or not re.match(r"^[A-Za-z_]", s):
        return None
    # Permit dotted paths (module.func) and hyphenated names.
    if not re.match(r"^[A-Za-z_][\w\-.]*$", s):
        return None
    return s


# Maintain-CHECK-ABOUT pattern (, design from ).
#
# Goals of the shape "Maintain: add/wire/ensure [<word>] check ..." describe
# the addition of a verification assertion. The target_files extractor
# naturally picks the ASSERTION-TARGET files (the wiring/source the new check
# inspects) because those filenames appear in the goal text — but the
# ASSERTION-HOST file (the SKILL.md where the check itself lives) usually
# doesn't appear as a literal path. Identifiers in assertion-target files are
# tautologically present (that's why they're being asserted on), so
# probe_target_state's union-across-files aggregation produces hit_ratio=1.0
# false-positive "already_present" verdicts.
#
# Canonical incident:  — the probe scanned orphan-root-sweep.sh +
# _orphan_root_helpers.py (where the assertion target lives) but never
# .claude/skills/verify-learning/SKILL.md (where the new check needed to
# land), blocking a legitimate Maintain goal at the duplication gate.
#
# Fix: when the predicate matches, REPLACE target_files with [edit_target]
# (not augment — augmenting keeps hit_ratio=1.0 because total_hits sums
# across all files). Only replacement isolates the edit-target signal.
# NOTE on the optional-word group: the spec from  says "X is
# word-character class" but the canonical incident  has title
# "Maintain: add verify-learning check ..." where "verify-learning" is
# hyphenated. Bare \w+ fails on hyphens, so use [\w-]+ for the optional
# token between the verb and "check". This is the minimum extension
# needed to match canonical Maintain-CHECK-ABOUT goal phrasing in the wild
# ("verify-learning check", "post-state-update check", etc.). \S+ would
# work too but [\w-]+ is more conservative (won't accidentally match dots
# or special chars).
_MAINTAIN_CHECK_ABOUT_RE = re.compile(
    r"^Maintain:.*\b(?:add|wire|ensure)\b\s+(?:[\w-]+\s+)?check\b",
    re.IGNORECASE,
)

# Explicit .claude/skills/<name>/SKILL.md path in goal text. Takes priority
# over phrase-based inference — if the author named the file explicitly,
# trust the author.
_SKILL_MD_PATH_RE = re.compile(
    r"\.claude/skills/([a-z0-9_-]+)/SKILL\.md",
    re.IGNORECASE,
)

# "add/wire/ensure verify-learning check" — the most common shape (rb-917 /
# guard-343 lineage routes encoding work through verify-learning SKILL.md).
# Allows hyphen OR space between verify and learning.
_VERIFY_LEARNING_TRIGGER_RE = re.compile(
    r"\b(?:add|wire|ensure)\s+verify[- ]?learning\s+check\b",
    re.IGNORECASE,
)

# "add/wire/ensure check to/in/into <skill-name>" — resolves to
# .claude/skills/<skill-name>/SKILL.md. Strips optional leading slash
# from the skill name (callers sometimes write "/respond").
_ADD_CHECK_TO_SKILL_RE = re.compile(
    r"\b(?:add|wire|ensure)\s+(?:\w+\s+)?check\s+(?:to|in|into)\s+/?([a-z0-9_-]+)\b",
    re.IGNORECASE,
)


def _is_maintain_check_about_goal(title):
    """True iff title is a Maintain-CHECK-ABOUT goal that needs edit_target replacement.

    Matches: '^Maintain:<anything> (add|wire|ensure) [<word> ]check<anything>'
    case-insensitive. The "<word> " between the verb and "check" is optional
    (covers both "add check" and "add verify-learning check").
    """
    if not title:
        return False
    return bool(_MAINTAIN_CHECK_ABOUT_RE.search(title))


def _extract_edit_target(title, description):
    """Resolve the ASSERTION-HOST SKILL.md path for a Maintain-CHECK-ABOUT goal.

    Priority chain (highest wins — author explicitness > phrase inference > default):
      (a) explicit '.claude/skills/<name>/SKILL.md' literal in title/description
      (b) 'add/wire/ensure verify-learning check' phrase -> verify-learning SKILL.md
      (c) 'add/wire/ensure check to/in/into <skill-name>' -> .claude/skills/<name>/SKILL.md
      (d) generic Maintain-CHECK-ABOUT (no specific signal) -> verify-learning SKILL.md (default)

    Returns the relative path string. The default (d) preserves the behavior
    "if we don't know which SKILL.md hosts the assertion, route through
    verify-learning" — matches the rb-917 / guard-343 convention.
    """
    text = (title or "") + "\n" + (description or "")

    # (a) explicit path wins
    m = _SKILL_MD_PATH_RE.search(text)
    if m:
        return f".claude/skills/{m.group(1).lower()}/SKILL.md"

    # (b) verify-learning trigger phrase
    if _VERIFY_LEARNING_TRIGGER_RE.search(text):
        return ".claude/skills/verify-learning/SKILL.md"

    # (c) 'add/wire/ensure check to <skill-name>'
    m = _ADD_CHECK_TO_SKILL_RE.search(text)
    if m:
        return f".claude/skills/{m.group(1).lower()}/SKILL.md"

    # (d) default — verify-learning hosts the assertion lane
    return ".claude/skills/verify-learning/SKILL.md"


def extract_targets(title, description):
    """Extract target files + identifiers + line hints from goal text.

    Returns a dict:
      {
        "target_files": [str],             # unique, order-preserved
        "identifiers": [str],              # unique, order-preserved
        "line_hints": {path: [int, ...]},  # per-file hinted line numbers
        "confidence": "high|medium|low|none",
        "target_kind": str|None,           # "maintain-check-about" when
                                           #   _is_maintain_check_about_goal
                                           #   fired and target_files was
                                           #   REPLACED with [edit_target];
                                           #   None for all other goals.
      }
    """
    text = (title or "") + "\n" + (description or "")

    # Line hints FIRST — before file paths get deduped.
    line_hints = {}
    for m in _LINE_HINT_RE.finditer(text):
        fp = m.group(1)
        line = int(m.group(2))
        line_hints.setdefault(fp, []).append(line)

    # File paths.
    target_files_seen = []
    seen_files = set()
    for fp in _FILE_PATH_RE.findall(text):
        if fp in seen_files:
            continue
        # : skip audit/report description paths — they describe
        # work, not implement it; including them in target_files lets the
        # probe's aggregate hit_ratio false-positive "already_present" when
        # the audit report cites identifiers that the impl file does not
        # yet contain.
        if _is_documentation_only_path(fp):
            continue
        seen_files.add(fp)
        target_files_seen.append(fp)
        if len(target_files_seen) >= _MAX_FILES:
            break

    # Identifiers. Order:
    #   1. Backticked tokens (highest confidence — author quoted them)
    #   2. Zero-arg calls in prose
    #   3. snake_case with >=1 underscore
    #   4. CamelCase
    identifiers_seen = []
    seen_ids = set()

    def _add(raw):
        ident = _clean_identifier(raw)
        if ident and ident not in seen_ids and ident not in _STOPWORDS:
            # Don't re-add a file path as if it were an identifier.
            if ident in seen_files:
                return
            seen_ids.add(ident)
            identifiers_seen.append(ident)

    # Remove file-path substrings first so "retrieve.py" doesn't become
    # the "retrieve" identifier when searched as CamelCase/snake.
    text_for_ids = text
    for fp in target_files_seen:
        text_for_ids = text_for_ids.replace(fp, " ")

    for m in _BACKTICK_RE.finditer(text_for_ids):
        _add(m.group(1))
    for m in _CALL_RE.finditer(text_for_ids):
        _add(m.group(1))
    for m in _SNAKE_RE.finditer(text_for_ids):
        _add(m.group(1))
    for m in _CAMEL_RE.finditer(text_for_ids):
        _add(m.group(1))

    # Cap identifier list.
    identifiers_seen = identifiers_seen[:_MAX_IDENTIFIERS]

    # : Maintain-CHECK-ABOUT replacement. When the title matches
    # the assertion-addition pattern, REPLACE target_files with the single
    # assertion-HOST SKILL.md so probe_target_state checks for the check's
    # presence in SKILL.md, not in the assertion-target source files where
    # the identifiers are tautologically present. See _is_maintain_check_about_goal
    # docstring +  /  (canonical incident) for the design
    # rationale and the "replace not augment" decision.
    target_kind = None
    if _is_maintain_check_about_goal(title):
        edit_target = _extract_edit_target(title, description)
        if edit_target:
            target_files_seen = [edit_target]
            target_kind = "maintain-check-about"

    # Confidence.
    has_file = bool(target_files_seen)
    n_ids = len(identifiers_seen)
    has_line_hint = bool(line_hints)

    if not has_file:
        confidence = "none"
    elif n_ids >= 2 or (n_ids >= 1 and has_line_hint):
        confidence = "high"
    elif n_ids == 1:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "target_files": target_files_seen,
        "identifiers": identifiers_seen,
        "line_hints": line_hints,
        "confidence": confidence,
        "target_kind": target_kind,
    }


# Directories to skip when basename-searching the repo — VCS, build output,
# vendor dirs, history snapshots, agent private state.
_SEARCH_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__",
    ".venv", "venv", ".tox", "dist", "build", ".next",
    ".history", ".cache", ".pytest_cache", ".mypy_cache",
}
_SEARCH_MAX_MATCHES = 6  # cap basename-search output to keep probe cheap.

#  inference fallback: when the goal text mentions class-shaped
# identifiers but no explicit file path (the  failure mode), walk
# search_roots looking for files where >=_INFER_MIN_IDENTIFIERS distinct
# identifiers co-occur. Self-reference (class file containing its own
# basename) is excluded to neutralize the trivial "StuckDetector.java
# mentions 'StuckDetector'" case.
_INFER_FILE_EXTS = {".py", ".sh", ".java", ".kt", ".cs", ".ts", ".tsx",
                    ".js", ".lua", ".go", ".rs"}
# Per-root cap on os.walk file inspection. Tuned for the framework repo
# (~500 .py + .sh files = sub-second) AND a single sibling product repo
# (typical 1k-3k source files = a few seconds). When a goal references
# identifiers that ONLY exist in a deep monorepo, the cap will be hit
# before exhaustive coverage — that's an acceptable trade-off given the
# advisory nature of the probe.
_INFER_MAX_FILES_PER_ROOT = 2500
# Skip files larger than this — class definitions are rarely >256KB and
# huge generated files (vendor bundles, autogen schemas) waste budget.
_INFER_MAX_FILE_BYTES = 256 * 1024
_INFER_MIN_IDENTIFIERS = 2  # require >=2 hits per file (excludes self-ref noise)


def _looks_like_class_name(ident):
    """PascalCase / CamelCase, alpha-numeric only, length >= 5.

    Restricts inference candidates to identifiers that look like type or
    class names. Excludes snake_case (likely a function or local), hyphens
    (likely a goal-id like 'rb-308'), and dotted paths (already file-like).
    """
    return bool(re.match(r"^[A-Z][a-zA-Z0-9]{4,}$", ident))


def _resolve_search_roots(agent_name=None):
    """Return search roots for inference: PROJECT_ROOT plus AGENT_WRITE_PATH
    when configured in <agent>/local-paths.conf.

    Sibling product repos under AGENT_WRITE_PATH are where g-250-10-class
    duplications hide — the framework can only catch them by walking outside
    PROJECT_ROOT. Empty list when neither is resolvable; caller treats
    empty as "skip inference."
    """
    try:
        import _paths  # late import — same lazy pattern as elsewhere
    except Exception:
        return []
    roots = [_paths.PROJECT_ROOT]
    agent = agent_name or os.environ.get("MIND_AGENT")
    if agent:
        conf = _paths.agent_dir(agent) / "local-paths.conf"
        if conf.is_file():
            try:
                lines = conf.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
            for line in lines:
                line = line.strip()
                if line.startswith("AGENT_WRITE_PATH=") and "=" in line:
                    # MULTI-ROOT (): ';'-separated, optionally quoted
                    # for bash-source safety. Each part is its own search root.
                    raw = line.split("=", 1)[1].strip().strip('"').strip("'")
                    for part in raw.split(";"):
                        part = part.strip()
                        if part:
                            roots.append(Path(part))
    return roots


def _scan_root_for_co_occurrence(root_resolved, id_patterns, max_files,
                                  max_bytes_per_file):
    """Walk a single resolved root looking for files where >=2 distinct
    identifiers co-occur. Self-reference (file stem == identifier) is
    excluded. Returns list of (abs_path_str, hits_count) for that root.
    Bounded by max_files; aborts early on cap.
    """
    if not root_resolved.is_dir():
        return []
    file_hits = []
    scanned = 0
    for cur, dirs, files in os.walk(root_resolved):
        dirs[:] = [d for d in dirs
                   if d not in _SEARCH_SKIP_DIRS and not d.startswith(".")]
        for f in files:
            _, ext = os.path.splitext(f)
            if ext.lower() not in _INFER_FILE_EXTS:
                continue
            p = os.path.join(cur, f)
            try:
                if os.path.getsize(p) > max_bytes_per_file:
                    continue
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
            except OSError:
                continue
            scanned += 1
            file_stem = os.path.splitext(f)[0]
            hits = {i for i, pat in id_patterns.items()
                    if i != file_stem and pat.search(content)}
            if len(hits) >= _INFER_MIN_IDENTIFIERS:
                file_hits.append((p, len(hits)))
            if scanned >= max_files:
                return file_hits
        if scanned >= max_files:
            return file_hits
    return file_hits


def _infer_targets_from_identifiers(search_roots, identifiers, max_matches=_MAX_FILES,
                                     max_files_per_root=_INFER_MAX_FILES_PER_ROOT):
    """Walk search_roots looking for files where >=_INFER_MIN_IDENTIFIERS
    distinct class-shaped identifiers co-occur, EXCLUDING trivial
    self-reference (a file named StuckDetector.java mentioning
    "StuckDetector" — that's the class definition, not evidence of feature
    completion).

    Two-pass strategy: scan roots IN ORDER, abort early when matches found.
    The framework repo (PROJECT_ROOT) is fast (~500 source files) so it
    lands first; sibling product repos (AGENT_WRITE_PATH) only get walked
    when the framework yields no hits. Keeps the common framework-goal
    case under ~1s while still catching cross-repo duplicates (g-250-10
    class).

    Returns list of (abs_path_str, hits_count) tuples sorted by hits desc.
    """
    class_idents = [i for i in identifiers if _looks_like_class_name(i)]
    if len(class_idents) < _INFER_MIN_IDENTIFIERS:
        return []

    # Inference uses a more lenient pattern than probe_target_state's
    # verdict regex: allow dotted prefix so `argparse.ArgumentParser`
    # matches a bare `ArgumentParser` from the goal description. The
    # verdict logic in probe_target_state stays strict (it must not
    # false-positive in the duplication call), but inference scanning
    # needs to be permissive — most class-name references in production
    # code use a module-qualified form while goal descriptions don't.
    id_patterns = {i: re.compile(r"\b" + re.escape(i) + r"\b")
                   for i in class_idents}

    aggregate = []
    for root in search_roots:
        try:
            root_resolved = Path(root).resolve()
        except (OSError, ValueError):
            continue
        per_root = _scan_root_for_co_occurrence(
            root_resolved, id_patterns, max_files_per_root, _INFER_MAX_FILE_BYTES,
        )
        aggregate.extend(per_root)
        if aggregate:
            # Found hits in this root — short-circuit. Cheaper roots are
            # walked first by convention (PROJECT_ROOT before AGENT_WRITE_PATH).
            break

    aggregate.sort(key=lambda x: -x[1])
    return aggregate[:max_matches]


def extract_and_infer_targets(title, description, search_roots=None, agent_name=None):
    """extract_targets + class-identifier inference fallback ().

    Bridges the gap when the goal description uses module/class identifiers
    but no explicit file path (g-250-10 class). When extract_targets returns
    no target_files but the description contains >=2 class-shaped
    identifiers, walks search_roots looking for files where those
    identifiers co-occur.

    Pure addition — extract_targets is unchanged. When search_roots is
    None or empty, behavior matches extract_targets exactly (no walk,
    no extra cost).

    Adds these keys to the returned dict:
      target_files_inferred: bool — True iff inference produced new files
      inference_hits: {abs_path_str: hit_count} — only when inferred
    """
    ex = extract_targets(title, description)
    ex["target_files_inferred"] = False

    if ex["target_files"] or not ex["identifiers"]:
        return ex

    if search_roots is None:
        search_roots = _resolve_search_roots(agent_name)
    if not search_roots:
        return ex

    matches = _infer_targets_from_identifiers(search_roots, ex["identifiers"])
    if not matches:
        return ex

    ex["target_files"] = [m[0] for m in matches]
    ex["target_files_inferred"] = True
    ex["inference_hits"] = {m[0]: m[1] for m in matches}
    # Inferred via co-occurrence — bump confidence from "none" to "medium"
    # (or "high" if line hints somehow exist, which is rare for inferred).
    ex["confidence"] = "high" if ex["line_hints"] else "medium"
    return ex


def _resolve_target_paths(project_root, rel_path, allowed_roots=None):
    """Resolve rel_path to one or more real file paths.

    Three modes:
      A) rel_path is absolute (e.g., from extract_and_infer_targets):
         validate it exists AND lives under one of allowed_roots (defaulting
         to [project_root]). Returns single-element list or []. Boundary
         check is preserved — refuses paths outside the allowed set.
      B) rel_path has slashes/backslashes (descriptive path):
         literal-resolve under project_root only.
      C) rel_path is a bare basename:
         literal-resolve, then bounded basename search under project_root.

    Returns list[Path]. Empty list means "not found" or "outside allowed roots".
    """
    project_root_resolved = Path(project_root).resolve()
    roots_resolved = []
    for r in (allowed_roots or [project_root_resolved]):
        try:
            roots_resolved.append(Path(r).resolve())
        except (OSError, ValueError):
            continue
    if not roots_resolved:
        roots_resolved = [project_root_resolved]

    p = Path(rel_path)

    # Mode A: absolute path (inferred from class-name search).
    if p.is_absolute():
        try:
            p_resolved = p.resolve()
        except OSError:
            return []
        if not p_resolved.is_file():
            return []
        for r in roots_resolved:
            try:
                p_resolved.relative_to(r)
                return [p_resolved]
            except ValueError:
                continue
        return []  # absolute path not under any allowed root — refuse.

    # Mode B/C: relative path under project_root.
    literal = (Path(project_root) / rel_path).resolve()
    try:
        literal.relative_to(project_root_resolved)
    except ValueError:
        return []
    if literal.is_file():
        return [literal]

    # Mode C: bare basename — bounded basename search.
    if "/" in rel_path or "\\" in rel_path:
        return []
    basename = Path(rel_path).name
    if not basename:
        return []
    matches = []
    for root, dirs, files in os.walk(project_root_resolved):
        dirs[:] = [d for d in dirs if d not in _SEARCH_SKIP_DIRS and not d.startswith(".")]
        if basename in files:
            matches.append(Path(root) / basename)
            if len(matches) >= _SEARCH_MAX_MATCHES:
                break
    return matches


def _read_target_file(project_root, rel_path):
    """Read target file content if it exists and is within size cap.
    Returns (content_str_or_None, existed_bool).

    This single-file reader is kept for line-hint verification where the
    caller already owns a specific path. For multi-path resolution, use
    _resolve_target_paths + _read_file_content.
    """
    p = (Path(project_root) / rel_path).resolve()
    try:
        project_root_resolved = Path(project_root).resolve()
        p.relative_to(project_root_resolved)
    except ValueError:
        return (None, False)
    if not p.is_file():
        return (None, False)
    return _read_file_content(p)


def _read_file_content(path):
    """Read a resolved Path with size cap. Returns (content, existed)."""
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return (None, True)
        return (path.read_text(encoding="utf-8", errors="replace"), True)
    except OSError:
        return (None, False)


def probe_target_state(project_root, target_files, identifiers, line_hints=None,
                        allowed_roots=None, lenient_match=False):
    """Check each identifier against each target file via substring-in-line match.

    Returns:
      {
        "verdict": "already_present|partially_present|absent|unknown",
        "per_file": [{"file":str, "exists":bool, "readable":bool,
                      "hits":[str], "misses":[str]}],
        "total_hits": int,             # unique identifiers found in ANY target
        "total_identifiers": int,
        "hit_ratio": float,            # total_hits / total_identifiers
        "line_hint_verifications": [
            {"file":str, "line":int, "anchor_found":bool}
        ],
      }

    Verdict thresholds: >=0.75 "already_present", >=0.25 "partially_present",
    >0 "partially_present", 0 "absent". No files/ids -> "unknown".
    """
    line_hints = line_hints or {}
    if not target_files or not identifiers:
        return {
            "verdict": "unknown",
            "per_file": [],
            "total_hits": 0,
            "total_identifiers": len(identifiers or []),
            "hit_ratio": 0.0,
            "line_hint_verifications": [],
            "reason": "no target files or no identifiers extracted",
        }

    # Compile identifier regexes once. Two pattern modes:
    #   strict (default): refuse to match `module.ClassName` when looking
    #     for `ClassName` — preserves the original probe semantics for
    #     explicit target_files (where dotted-prefix could be unrelated).
    #   lenient (caller opt-in): allow dotted prefix — needed when
    #     target_files were chosen by extract_and_infer_targets's
    #     co-occurrence walk, which used a lenient pattern; without this
    #     the probe would say verdict=absent on the very files inference
    #     said were strong matches. Caller passes lenient_match=True
    #     when target_files came from inference (target_files_inferred).
    if lenient_match:
        id_patterns = {i: re.compile(r"\b" + re.escape(i) + r"\b") for i in identifiers}
    else:
        id_patterns = {i: re.compile(r"(?<![\w.])" + re.escape(i) + r"(?![\w])") for i in identifiers}

    per_file = []
    found_anywhere = set()
    any_readable = False

    project_root_resolved = Path(project_root).resolve()
    for fp in target_files:
        resolved_paths = _resolve_target_paths(project_root, fp, allowed_roots=allowed_roots)
        if not resolved_paths:
            # Literal miss + no basename matches. Record as unreadable.
            per_file.append({
                "file": fp,
                "exists": False,
                "readable": False,
                "hits": [],
                "misses": list(identifiers),
            })
            continue
        # Union hits across all resolved copies of this basename (rare but
        # possible — e.g. retrieve.py exists in both core/scripts/ and
        # world/scripts/). We want the OR of hits, not the intersection:
        # if the fix landed in one copy, the goal is effectively already
        # implemented.
        union_hits = set()
        any_copy_readable = False
        any_copy_existed = False
        resolved_rel = []
        for p in resolved_paths:
            any_copy_existed = True
            content, existed = _read_file_content(p)
            try:
                rel = str(p.resolve().relative_to(project_root_resolved))
            except ValueError:
                rel = str(p)
            resolved_rel.append(rel)
            if content is None:
                continue
            any_copy_readable = True
            for i, pat in id_patterns.items():
                if pat.search(content):
                    union_hits.add(i)
        if any_copy_readable:
            any_readable = True
        hits = [i for i in identifiers if i in union_hits]
        misses = [i for i in identifiers if i not in union_hits]
        found_anywhere.update(union_hits)
        per_file.append({
            "file": fp,
            "resolved": resolved_rel,
            "exists": any_copy_existed,
            "readable": any_copy_readable,
            "hits": hits,
            "misses": misses,
        })

    # Line hint verification (anchor check): for each hinted line, confirm
    # that at least one identifier (or its stem) appears within +/- 5 lines
    # of the hint. Cheap sanity check that the line number is still roughly
    # accurate — catches "goal says line 82, but file now has 200 lines and
    # identifier is at line 150".
    line_hint_verifications = []
    for fp, lines in line_hints.items():
        content, existed = _read_target_file(project_root, fp)
        if content is None:
            for ln in lines:
                line_hint_verifications.append({
                    "file": fp, "line": ln, "anchor_found": False,
                    "reason": "file unreadable" if not existed else "file too large",
                })
            continue
        content_lines = content.splitlines()
        total_lines = len(content_lines)
        for ln in lines:
            if ln < 1 or ln > total_lines:
                line_hint_verifications.append({
                    "file": fp, "line": ln, "anchor_found": False,
                    "reason": "line out of range (file has " + str(total_lines) + " lines)",
                })
                continue
            lo = max(0, ln - 6)
            hi = min(total_lines, ln + 5)
            window = "\n".join(content_lines[lo:hi])
            anchor_found = any(pat.search(window) for pat in id_patterns.values())
            line_hint_verifications.append({
                "file": fp, "line": ln, "anchor_found": anchor_found,
            })

    total_ids = len(identifiers)
    total_hits = len(found_anywhere)
    hit_ratio = total_hits / total_ids if total_ids else 0.0

    # ADVISORY VERDICT — NEVER A HARD SKIP.
    # The execution-time caller (Phase 4-pre of aspirations-execute) treats
    # the whole probe as advisory: Phase 5 verification is ground truth and
    # will re-run regardless. The filing-time caller (goal-duplication-gate)
    # blocks ONLY on "already_present" (hit_ratio >= 0.75) AND exposes an
    # --override-duplication escape hatch. Do not add a fifth verdict, do
    # not raise the "absent" floor above 0, and do not lower the 0.75
    # threshold without revisiting both consumers. Fail-open = false
    # "unknown", never a false "already_present".
    if not any_readable:
        verdict = "unknown"
    elif hit_ratio >= 0.75:
        verdict = "already_present"
    elif total_hits > 0:
        verdict = "partially_present"
    else:
        verdict = "absent"

    return {
        "verdict": verdict,
        "per_file": per_file,
        "total_hits": total_hits,
        "total_identifiers": total_ids,
        "hit_ratio": round(hit_ratio, 3),
        "line_hint_verifications": line_hint_verifications,
    }
