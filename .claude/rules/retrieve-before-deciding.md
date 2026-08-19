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
11. **Filing a discovered-work goal that prescribes a fix** — the sibling of
   #5 and the one most often skipped. A goal filed the moment a problem lands
   splits into a measured DIAGNOSIS and an unmeasured REMEDY, and whoever
   executes it inherits the remedy as scope. Retrieve against the *remedy*
   before writing it down — the cheaper fix is often already encoded
   (rb-5669, guard-1719).
12. **Running a probe whose EMPTY result will authorize an action** — an
   ownership check before filing, a duplicate scan, a "does this exist yet"
   grep, a suppression gate's lookup. A probe whose zero authorizes a write
   has already made the decision. Retrieve against the PROBE ITSELF — the
   tool, its flags, its failure modes — not only the subject (guard-3362:
   an unsupported flag refused twice, piped into a parser as a clean empty,
   two duplicate goals filed).
13. **Computing a census or aggregate over a store** — any count, tally,
   distribution or "N of M" you intend to report or act on. Retrieve on the
   MECHANISM ("counting records in a JSONL store by tallying one field",
   "producing a zero from a filter I wrote this turn"), not only on the
   subject — the count-hazard guardrails (positive control before believing
   a zero, unfiltered population beside the filtered count, never a zero from
   truncated output) are indexed on the operation; measured, subject queries
   missed 15–20 of 20 across four censuses. SSOT for the two-query
   mechanism: `core/config/conventions/retrieval-triggers.md` § "Why TWO queries".

If you find yourself making one of these decisions without having
retrieved in the same turn, STOP and retrieve first.

## What counts as a "retrieval"

The unified entry point is `core/scripts/retrieve.sh`. Pick the
right shape (footnotes on the fallback fields and flags: retrieval-triggers.md
§ Invocation-table footnotes):

| Decision shape | Invocation |
|----------------|------------|
| Goal/topic is clearly categorized | `retrieve.sh --category <cat> --depth medium` |
| Free-text query (no exact category) | `retrieve.sh --category "<free text>" --depth shallow` (token-overlap; supplementary stores fall back to `rule` / `summary` / `tags` / `when_to_use.conditions` — `rule` is the field guardrails actually carry) |
| Pre-apply consultation for framework-file fix | `retrieve.sh --category "<one-line fix description>" --depth shallow --include-framework` (the flag is REQUIRED — without it there is no `framework_rules` key at all; g-115-3777) |
| Reader mode / observer session (side-effect-free) | `retrieve.sh --category <q> --read-only` |
| Need full-body content of supplementary entries | `retrieve.sh --category <cat> --full-content` (opt-in; default is metadata-only) |
| Goal-execution retrieval (writes retrieval-session.json) | `retrieve.sh --category <cat> --goal <goal-id> --tree-nodes "<comma-keys>"` |
| Browsing tree structure without scoring | `tree-find-node.sh --text <q>` (substring-only; weaker than retrieve.sh) |
| Reading framework rules / conventions by name | `load-conventions.sh <name>` (exact key lookup) |
| Retrieving framework rules / conventions by topic | `retrieve.sh --category "<free text>" --include-framework` (returns under `framework_rules`) |

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
- Filing a goal whose description prescribes a fix, having retrieved
  against the problem but never against the proposed remedy
- Retrieving on what a census is ABOUT and never on the act of counting —
  the tell is a clean-looking number nobody positive-controlled

## Enforcement

- `code-review-protocol.md` step 4 — gates framework-file fixes
- `verify-before-assuming.md` — gates negative conclusions
- `aspirations-learning-gate` Phase 9.5b — audits that retrieval happened
  during goal execution; forces retroactive retrieval when Phase 4 skipped it
- `exhaustive-search-before-negation.md` — gates "doesn't exist" claims
- Advisory PreToolUse[Edit] gate `core/scripts/pre-edit-context-gate.sh` —
  advisory only, fires for the manifest's trackable subset; silent elsewhere,
  where Rules 1-3 of `read-before-edit.md` are the only safeguard
  (retrieval-triggers.md G14 and § Enforcement note)

The catalog in `core/config/conventions/retrieval-triggers.md` lists the
additional decision points where retrieval should fire but currently doesn't
(stable Gxx identifiers).

## Cross-references

- `core/config/conventions/retrieval-triggers.md` — canonical trigger catalog + moved evidence for points 11–13
- `core/config/conventions/retrieval-escalation.md` — three-tier escalation
- `core/config/conventions/tree-retrieval.md` — engine details
- `core/config/conventions/exhaustive-search-before-negation.md` — negation protocol
- `.claude/rules/verify-before-assuming.md` — multi-signal rule
- `.claude/rules/code-review-protocol.md` — pre-apply consultation step
- `.claude/rules/encode-stable-facts.md` — retrieve-before-discovery
