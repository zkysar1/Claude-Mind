---
description: "Before a consequential decision (goal pick, verify, hypothesis, blocker, aspiration, framework fix, negation, census) run retrieve.sh first."
---

# Retrieve Before Deciding

## Principle

The agent accumulates knowledge across sessions in the knowledge tree,
reasoning bank, guardrails, pattern signatures, beliefs, and experience
archive. That knowledge is worth nothing if it is not consulted before
the decisions it could inform. The default for every consequential
decision is to retrieve first, decide second.

This rule names the decision points where retrieval should fire. The
canonical catalog of retrieval triggers (active and missing), plus the
measured evidence behind the least-obvious points below, lives in
`core/config/conventions/retrieval-triggers.md` (`load-conventions.sh
retrieval-triggers`) — refer to it for the authoritative status of any
specific trigger.

## What counts as a "consequential decision"

Any in-loop action where the wrong call costs work, regresses learning,
or has to be undone. In particular:

1. **Picking the next goal** — cross-cutting guardrails about prior failed
   attempts in the same category should inform whether to pick THIS goal NOW.
2. **Verifying a goal's outcome** — retrieve verification-related guardrails
   before deciding whether to escalate or accept (Q1/Q2/Q3).
3. **Resolving a surprising hypothesis** — at `surprise_level >= 7` the
   resolution likely invalidates downstream beliefs; retrieve the affected
   category broadly before recording the outcome.
4. **Re-probing a blocker** — retrieve diagnostic-context RB about prior
   probes BEFORE running the canonical companion script; the wrong probe
   shape produces a false negative.
5. **Adding a new aspiration** — retrieve RB/guardrails about its category
   and check for contradictions before writing.
6. **Acting on an inbound signal** — a board post or email mid-session:
   retrieve the relevant context BEFORE routing, responding, or filing.
7. **Applying a framework-file fix** — `core/`, `.claude/`, `core/config/`,
   `world/conventions/`. The pre-apply consultation (TWO queries — subject
   and mechanism) is mandatory; see `code-review-protocol.md` step 4.
8. **Declaring a negative conclusion** — "X doesn't exist", "Y isn't built",
   "Z can't be done". See `verify-before-assuming.md` and
   `core/config/conventions/exhaustive-search-before-negation.md`.
9. **Discovering a stable fact** — a resource locator (path, endpoint,
   account ID): check `world/conventions/` for an existing locator first.
   See `encode-stable-facts.md`.
10. **Editing or modifying an existing file** — you must have Read the file
   in this session before any Edit/MultiEdit. See `read-before-edit.md`.
11. **Prescribing a fix to anyone else** — retrieve against the *remedy*,
   not just the diagnosis (rb-5669, guard-1719, rb-9087).
12. **Running a probe whose EMPTY result will authorize an action** — retrieve
   against the PROBE ITSELF, not only the subject (guard-3362).
13. **Computing a census or aggregate over a store** — retrieve on the
   MECHANISM, not only the subject (count-hazard guardrails are indexed on
   the operation). Detail: `retrieval-triggers.md` § "Why TWO queries".

If you find yourself making one of these decisions without having
retrieved in the same turn, STOP and retrieve first.

## Retrieval entry point

`core/scripts/retrieve.sh`. Key shapes: `--category <cat> --depth medium`
(categorized), `--category "<free text>" --depth shallow` (free-text),
`--include-framework` (REQUIRED for framework-file fixes; g-115-3777),
`--read-only` (reader/observer mode). Full invocation table and footnotes:
`retrieval-triggers.md` § "Invocation table".

## What counts as "deciding"

Any state-mutating action: writing a goal/aspiration/hypothesis, Edit/Write,
filing a blocker, setting `defer_reason`, posting to the board, sending a
notification, or answering the user beyond echoing a fresh read.

## When retrieval is NOT required

- Pure mechanical operations: file renames, formatting fixes, removing
  trailing whitespace, replacing a known-literal-value
- Routine recurring goals whose verification is a simple presence check
  (`outcome_class: routine`; retrieval can be similarly light)
- Reading a file the user just pointed at (the file IS the source of truth)
- Cleanup of artifacts the agent itself just created in the same turn

## Anti-patterns

- Picking the next goal because the scorer ranked it first, without
  retrieving cross-cutting guardrails
- Verifying a goal's outcome by re-reading the artifact only
- Recording a high-surprise hypothesis resolution without retrieving the
  beliefs / RB entries it may have falsified
- Responding to an inbound board post by acting on its text alone
- Writing a new aspiration without retrieving guardrails about
  recently-failed aspirations in the same category
- Re-probing a blocker by running the canonical script alone
- Applying a framework-file fix without the pre-apply consultation
- Prescribing a fix in a goal, note or post, having retrieved against
  the problem but never the remedy
- Retrieving on what a census is ABOUT and never on the act of counting —
  the tell is a clean-looking number nobody positive-controlled

## Enforcement

- `code-review-protocol.md` step 4 — gates framework-file fixes
- `verify-before-assuming.md` — gates negative conclusions
- `aspirations-learning-gate` Phase 9.5b — audits that retrieval happened
  during goal execution; forces retroactive retrieval when Phase 4 skipped it
- `exhaustive-search-before-negation.md` — gates "doesn't exist" claims
- Advisory PreToolUse[Edit] gate `core/scripts/pre-edit-context-gate.sh` —
  partial by design; `read-before-edit.md` Rules 1-3 are the real safeguard
  (retrieval-triggers.md G14 and § Enforcement note)

## Cross-references

- `core/config/conventions/retrieval-triggers.md` — canonical trigger catalog + moved evidence for points 11–13
- `core/config/conventions/retrieval-escalation.md` — three-tier escalation
- `core/config/conventions/tree-retrieval.md` — engine details
- `core/config/conventions/exhaustive-search-before-negation.md` — negation protocol
- `.claude/rules/verify-before-assuming.md` — multi-signal rule
- `.claude/rules/code-review-protocol.md` — pre-apply consultation step
- `.claude/rules/encode-stable-facts.md` — retrieve-before-discovery
