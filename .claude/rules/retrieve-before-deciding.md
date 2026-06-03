# Retrieve Before Deciding

## Principle

The agent accumulates knowledge across sessions in the knowledge tree,
reasoning bank, guardrails, pattern signatures, beliefs, and experience
archive. That knowledge is worth nothing if it is not consulted before
the decisions it could inform. The default for every consequential
decision is to retrieve first, decide second.

This rule names the decision points where retrieval should fire. The
canonical catalog of retrieval triggers (active and missing) lives in
`core/config/conventions/retrieval-triggers.md` — refer to it for the
authoritative status of any specific trigger.

## What counts as a "consequential decision"

Any in-loop action where the wrong call costs work, regresses learning,
or has to be undone. In particular:

1. **Picking the next goal** — `aspirations-select` scoring runs on goal
   metadata; cross-cutting guardrails about prior failed attempts in the
   same category should inform whether to pick THIS goal NOW.
2. **Verifying a goal's outcome** — `aspirations-verify` Q1/Q2/Q3
   escalation should retrieve verification-related guardrails before
   deciding whether to escalate or accept.
3. **Resolving a surprising hypothesis** — when `surprise_level >= 7`,
   the resolution likely invalidates one or more downstream beliefs.
   Retrieve the affected category broadly before recording the outcome
   so reconciliation can fire on the right nodes.
4. **Re-probing a blocker** — `aspirations-precheck` Phase 0.5b should
   retrieve diagnostic-context RB about prior probes BEFORE running
   the canonical companion script. The wrong probe shape produces a
   false negative; RB on prior probe attempts catches that.
5. **Adding a new aspiration** — `create-aspiration` Step 5 should
   retrieve RB/guardrails about the aspiration's category and check
   for contradictions before writing.
6. **Acting on an inbound signal** — when a board post or email
   arrives mid-session, retrieve the relevant context BEFORE deciding
   how to route, respond, or file.
7. **Applying a framework-file fix** — `core/`, `.claude/`,
   `core/config/`, `world/conventions/`. The pre-apply consultation
   step is mandatory; see `code-review-protocol.md` step 4.
8. **Declaring a negative conclusion** — "X doesn't exist", "Y isn't
   built", "Z can't be done". See `verify-before-assuming.md` and
   `core/config/conventions/exhaustive-search-before-negation.md`.
9. **Discovering a stable fact** — if the value is a resource locator
   (path, endpoint, account ID), check `world/conventions/` for an
   existing locator before discovering. See `encode-stable-facts.md`.
10. **Editing or modifying an existing file** — before any Edit or
   MultiEdit on a file, you must have Read that file in this session.
   Editing from stale context (prior session memory, summary, or
   model prior) lands changes on wrong lines, overwrites concurrent
   modifications, or targets content that no longer exists. See
   `.claude/rules/read-before-edit.md`.

If you find yourself making one of these decisions without having
retrieved in the same turn, STOP and retrieve first.

## What counts as a "retrieval"

The unified entry point is `core/scripts/retrieve.sh`. Pick the
right shape:

| Decision shape | Invocation |
|----------------|------------|
| Goal/topic is clearly categorized | `retrieve.sh --category <cat> --depth medium` |
| Free-text query (no exact category) | `retrieve.sh --category "<free text>" --depth shallow` (engine does token-overlap; supplementary stores fall back to title/content/tags matching when category match returns empty — see retrieval-triggers.md G9 / R3) |
| Pre-apply consultation for framework-file fix | `retrieve.sh --category "<one-line fix description>" --depth shallow` |
| Reader mode / observer session (side-effect-free) | `retrieve.sh --category <q> --read-only` |
| Need full-body content of supplementary entries | `retrieve.sh --category <cat> --full-content` (opt-in; default is metadata-only) |
| Goal-execution retrieval (writes retrieval-session.json) | `retrieve.sh --category <cat> --goal <goal-id> --tree-nodes "<comma-keys>"` |
| Browsing tree structure without scoring | `tree-find-node.sh --text <q>` (substring-only; weaker than retrieve.sh) |
| Reading framework rules / conventions by name | `load-conventions.sh <name>` (exact key lookup) |
| Retrieving framework rules / conventions by topic | `retrieve.sh --category "<free text>" --include-framework` (token-overlap on title + section headers + first 500 chars; returns under `framework_rules` key — closes retrieval-triggers.md G8) |

## What counts as "deciding"

Any of the following marks the decision boundary:

- Writing a new goal, aspiration, or hypothesis
- Calling a skill that mutates state (Edit, Write, `*-update*.sh`,
  `pipeline-move.sh`, `aspirations-add-goal.sh`, etc.)
- Filing a blocker via CREATE_BLOCKER
- Setting `defer_reason` on a goal
- Posting to the coordination or findings board
- Sending a notification via the forged notification skill
- Responding to the user with an answer that goes beyond echoing a
  fresh read

If retrieval did not fire in the same turn that the decision lands,
the decision was made from memory or amnesia — not from accumulated
knowledge.

## When retrieval is NOT required

- Pure mechanical operations: file renames, formatting fixes, removing
  trailing whitespace, replacing a known-literal-value
- Routine recurring goals whose verification is a simple presence check
  (the loop classifies these as `outcome_class: routine` and explicitly
  bypasses the encoding lane; retrieval can be similarly light)
- Reading a file the user just pointed at (the file IS the source of
  truth; retrieving knowledge ABOUT the file is appropriate, but not
  required for the read itself)
- Cleanup of artifacts the agent itself just created in the same turn

## Anti-patterns

- Picking the next goal because the scorer ranked it first, without
  retrieving cross-cutting guardrails that might constrain or
  re-prioritize the work
- Verifying a goal's outcome by re-reading the artifact only, without
  retrieving guardrails about verification anti-patterns
- Recording a high-surprise hypothesis resolution without retrieving the
  beliefs / RB entries that the resolution may have falsified
- Responding to an inbound board post by acting on its text alone,
  with no retrieval of context the post relates to
- Writing a new aspiration without retrieving guardrails about
  recently-failed aspirations in the same category
- Re-probing a blocker by running the canonical script alone, with no
  retrieval of RB about prior probe attempts and shape
- Applying a framework-file fix without running the pre-apply
  consultation step in `code-review-protocol.md`

## Enforcement

Today, this rule is enforced via:

- `code-review-protocol.md` step 4 — gates framework-file fixes
- `verify-before-assuming.md` — gates negative conclusions
- `aspirations-learning-gate` Phase 9.5b — audits that retrieval
  happened during goal execution; forces retroactive retrieval when
  Phase 4 skipped it entirely
- `exhaustive-search-before-negation.md` — gates "doesn't exist" claims
- Advisory PreToolUse[Edit] gate `core/scripts/pre-edit-context-gate.sh` —
  checks `context-reads.txt` for a prior Read of the target file and prints a
  stderr advisory if absent. NEVER blocks (always exits 0). Fires only for the
  manifest's trackable subset (`core/config`, `.claude/skills`,
  `world/knowledge/tree`, `world/conventions`); silent for out-of-scope files
  like `core/scripts`/`.claude/rules`/agent files, where Rules 1-3 are the only
  safeguard (see `.claude/rules/read-before-edit.md` Rule 4, retrieval-triggers.md G14)

The catalog in `core/config/conventions/retrieval-triggers.md` lists
the additional decision points where retrieval should fire but
currently doesn't. Each gap is named with a stable Gxx identifier
and a pointer to its proposed resolution.

## Cross-references

- `core/config/conventions/retrieval-triggers.md` — canonical trigger catalog
- `core/config/conventions/retrieval-escalation.md` — three-tier escalation
- `core/config/conventions/tree-retrieval.md` — engine details
- `core/config/conventions/exhaustive-search-before-negation.md` — negation protocol
- `.claude/rules/verify-before-assuming.md` — multi-signal rule
- `.claude/rules/code-review-protocol.md` — pre-apply consultation step
- `.claude/rules/encode-stable-facts.md` — retrieve-before-discovery
