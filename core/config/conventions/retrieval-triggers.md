# Retrieval Triggers

Authoritative catalog of every point where the agent loads accumulated knowledge
into active context. The single source of truth for "where does retrieval
happen, where should it happen, and which stores does each trigger touch."

For the escalation policy (tree → codebase → web) see
`core/config/conventions/retrieval-escalation.md`. For the engine details
(matchers, ranking, caps) see `core/config/conventions/tree-retrieval.md`.
This file documents the **trigger surface**: WHO fires retrieval, WHEN, and
WHY.

## Stores

Abbreviations used in the matrix below:

| Code | Store | Path |
|------|-------|------|
| T | Knowledge tree | `world/knowledge/tree/_tree.yaml` + node `.md` files |
| R | Reasoning bank | `world/reasoning-bank.jsonl` |
| G | Guardrails | `world/guardrails.jsonl` |
| P | Pattern signatures | `world/pattern-signatures.jsonl` |
| E | Experience archive | `agents/<agent>/experience.jsonl` + `agents/<agent>/experience/` |
| B | Beliefs | `world/knowledge/beliefs.yaml` |
| X | Experiential index | `agents/<agent>/experiential-index.yaml` |
| F | Framework rules + conventions | `.claude/rules/*.md`, `core/config/conventions/*.md` |
| C | Codebase | primary workspace (Grep/Glob/Read) |
| W | Web | WebSearch / WebFetch |

The unified retrieval script (`core/scripts/retrieve.sh`) reads T+R+G+P+E+B+X
in one call. F is loaded two ways: by exact convention key via
`core/scripts/load-conventions.sh` keyed to skill front matter, AND
content-searchable by topic via `retrieve.sh --include-framework`
(returned under the `framework_rules` key — closes G8 below). C and W
are escalated tiers (per retrieval-escalation.md).

## Status legend

| Symbol | Meaning |
|--------|---------|
| ✓ | Fires retrieval today |
| ◐ | Partial — fires for some sub-paths, not all |
| ✗ | Does not fire today |
| → R{N} | Implemented by improvement R{N} in this catalog |

## Active triggers (what fires today)

| Trigger | Status | Stores | Sync? | Implementation site |
|---------|--------|--------|-------|---------------------|
| Session start (prime) | ✓ | T+R+G+P+E+B+X + universal-RB | Sync | `.claude/skills/prime/SKILL.md` |
| Goal execution — Phase 4 Intelligent Retrieval | ✓ | T + R+G+P+E+B via `--supplementary-only` | Sync | `.claude/skills/aspirations-execute/SKILL.md`, `core/config/execute-protocol-digest.md` |
| User message — `/respond` Step 4 | ✓ | Tier 1: T+R+G+P+E; Tier 2: C; Tier 3: W (mode-gated) | Sync, escalated | `.claude/skills/respond/SKILL.md` |
| Decomposition — `/decompose` Step 3.5 | ✓ | T+R+G+P+E via `retrieve.sh --category {cat} --depth shallow` | Sync | `.claude/skills/decompose/SKILL.md` |
| Hypothesis resolve — `/review-hypotheses` Step 1.5 | ✓ | T+R+G+P+E+B per category, cached | Sync | `.claude/skills/review-hypotheses/SKILL.md` |
| Hypothesis reflection — `/reflect-on-outcome` Step 1 | ✓ | Experience via `experience_ref`; R/G category dedup | Sync | `.claude/skills/reflect-on-outcome/SKILL.md` |
| Replay — `/replay` Step 1.5, Step 5 | ✓ | T+R+G+P+E+B per category | Sync, cached | `.claude/skills/replay/SKILL.md` |
| State update — `/aspirations-state-update` Step 8r (every 5 routines) | ◐ | T via `tree-find-node` | Sync | `.claude/skills/aspirations-state-update/SKILL.md` |
| Learning gate — `/aspirations-learning-gate` Phase 9.5b | ◐ Audit; forces retroactive retrieve only when skipped entirely | T+R+G+P+E+B | Sync | `.claude/skills/aspirations-learning-gate/SKILL.md` |
| Encoding (chat) — `/encode-session` Lane 1 | ◐ Dedup-only retrieval (`tree-find-node` + RB/G category reads) | T+R+G | Sync | `.claude/skills/encode-session/SKILL.md` |
| Spark — `/aspirations-spark` sq-009 hypothesis gen | ✓ | T+R+G+P+E via `retrieve.sh --depth shallow` + pipeline-read | Sync | `.claude/skills/aspirations-spark/SKILL.md` |
| Spark — sq-c05 data acquisition | ◐ | T (entity_index scan) | Sync | same |
| Spark — sq-c04 contradiction check | ◐ | T (leaves under category) | Sync | same |
| Spark — sq-013 work discovery | ◐ | aspirations-read for dedup; no R/G consultation | Sync | same |
| Goal selection — `/aspirations-select` Phase 2.25 | ◐ | T via `load-tree-summary.sh` (selection-context only — NOT R/G/P/E) | Sync | `.claude/skills/aspirations-select/SKILL.md` — see Gap G1 |
| Before negation — exhaustive-search-before-negation | ✓ | All stores via 3+ query variations | Sync | `core/config/conventions/exhaustive-search-before-negation.md` |
| Before applying framework-file fix | ✓ | R+G via `retrieve.sh --category "<free-text>"` (token-overlap) | Sync | `.claude/rules/code-review-protocol.md` step 4 |
| Before discovering stable fact | ✓ | F (resource-locator convention files) | Sync | `.claude/rules/encode-stable-facts.md` |
| Boot — `/boot` Step 2.7 | ✓ | Delegates to `/prime` | Sync | `.claude/skills/boot/SKILL.md` |
| Knowledge reconciliation — Phase 4.5 | ◐ | T (identifies affected nodes from external_changes) | Sync | `.claude/skills/aspirations-execute/SKILL.md` Phase 4.5 |

## Gap triggers (what should fire but doesn't)

Each entry below names where retrieval is missing and the improvement that
adds it. The numbered Gxx labels are stable identifiers for cross-reference
from skills, rules, and reasoning-bank entries. Status column shows the
implementation state: ✓=implemented this session, ✗=open, ◐=partial.

| Gap | Status | Trigger | What was missing | Resolution |
|-----|--------|---------|------------------|------------|
| G1 | ✓ R8 | `aspirations-select` scoring | Cross-cutting R/G that should inform "is this the right goal now"; the selector read goal metadata only | `aspirations-select/SKILL.md` Phase 2.27 — Cross-Cutting Guardrail Probe added before Phase 2.5 metacognitive assessment |
| G2 | ✓ R6 | `aspirations-verify` Q1/Q2/Q3 escalation | Guardrails/R about verification anti-patterns; sets the LLM up to escalate blindly | `aspirations-verify/SKILL.md` — Pre-Escalation Retrieval added before Empty-Checks Escalation Protocol |
| G3 | ✓ R5 | Surprise → broad re-retrieve | `surprise_level >= 7` should pull category-broad B/R that may have been falsified by this resolution | `review-hypotheses/SKILL.md` Step 3.5 — Broad Re-Retrieve on High Surprise added before atomic move |
| G4 | ✓ R11 | Mid-execution drift | Long-running goals (>30 min wall-clock OR >4000 chars result) should re-retrieve fresh context | `aspirations-execute/SKILL.md` Phase 4.05 — Mid-Execution Drift Check added between Phase 4 and Phase 4-post |
| G5 | ✓ R7 | Blocker re-probe (`aspirations-precheck` Phase 0.5b) | R about prior probe attempts and canonical-probe pattern should inform probe shape | `aspirations-precheck/SKILL.md` Phase 0.5b — Pre-probe retrieval added inside FOR-EACH-blocker loop, before infra-health probe |
| G6 | ✓ R10 | Inbound board post / email | New external signal should trigger a delta-retrieve for relevant context before the agent acts | `access-email/SKILL.md` — Inbound Signal Retrieval section added; `aspirations-execute/SKILL.md` Phase 3.97 — Inbound Signal Sweep added |
| G7 | ✓ R9 | `create-aspiration` dedup | R/G that constrain or refine the new aspiration; today only title-matches existing aspirations | `create-aspiration/SKILL.md` Step 5.1.5 — Cross-store contradiction check added inside Step 5 |
| G8 | ✓ R13 | Framework rules + conventions not retrievable | F store is loaded by skill-frontmatter convention key, not searchable by content; the agent cannot retrieve a rule by topic | `core/scripts/retrieve.py` — `load_framework_rules()` walks `.claude/rules/*.md`, `core/config/conventions/*.md`, `world/conventions/*.md`; parses title + section headers + first 500 chars of body per file; reuses `_entry_matches_text` token-overlap. Opt-in via new `--include-framework` flag; result returned under new `framework_rules` key. Index rebuilt every call (corpus is ~94 files, body samples ~50KB total — O(ms) cost; no parallel YAML). **Wired (g-001-232, 2026-05-12)**: `.claude/skills/respond/SKILL.md` Tier 1 retrieve passes `--include-framework` for all user queries; e2e probe confirmed query "implementation discipline avoid scope creep" surfaces `.claude/rules/implementation-discipline.md`. |
| G9 | ✓ R3 | Supplementary-store matcher is bimodal with tree nodes | T uses token-overlap; R/G/P used `_entry_matches_category` (substring on category field only); free-text queries returned zero supplementary hits | `core/scripts/retrieve.py` — `_entry_matches_text` token-overlap fallback added; 3 loaders updated to use `_entry_matches` combined predicate. Verified: all 5 paraphrased queries now return rb=8-40, g=14-40. **R3 was a PARTIAL application, completed 2026-07-30 by g-115-3855.** It updated the three engine LOADERS and missed a fourth consumer in another layer: the daemon session writer (`mind_api/src/endpoints/retrieve.py`) kept re-deriving `supplementary_items` membership with the narrow `_entry_matches_category`. So free-text-matched entries were returned and counter-bumped but dropped from the attestation manifest — `utilization-feedback --helpful` could never credit them, and `utility_ratio` drove them out of ranking. Membership is now taken from the loaders' return set (computed once). Measured on one query: 48 returned / 14 recorded → 48/48; attestation `helpful` 0 → 4. Note the sweep that missed it grepped the NEW predicate name, which structurally cannot find sites still calling the OLD one — grep `_entry_matches_category` (rb-5824, guard-1455). |
| G10 | ✓ R12 | "Retrieval influence:" not captured in journal | Phase 4 Step 5c output existed in protocol but never reached journal entries | `core/config/execute-protocol-digest.md` Step 5c — writes `retrieval_influence_last` to working memory; `core/scripts/iteration-close.sh` — reads slot and passes via new `--retrieval-influence` flag; `core/scripts/journal-append.sh` — accepts flag and emits "Retrieval influence:" line |
| G11 | ✓ R14 | `/respond` Step 5 directive writes | Self update / new aspiration / paused aspiration / remembered fact / recurring task all write to a store without first retrieving to check for contradictions, constraints, or duplicates. Non-autonomous-mode counterpart to G7. | `.claude/skills/respond/SKILL.md` Step 5.0 — Pre-Write Retrieval added between Mode Gate and directive routing. Reuses Step 4 retrieval when subject matches; re-retrieves for divergent subjects. Surfaces contradictions to user; user is authoritative. |
| G12 | ✓ R15 | `/respond` Step 6 Knowledge Freshness | User correction triggered `_tree.yaml` scan only — missed RB/guards/beliefs/patterns that depend on the falsified knowledge. Sister mechanism to G3 surprise→broad-retrieve. | `.claude/skills/respond/SKILL.md` Step 6.1.a' — Broad re-retrieve added; builds reconciliation_candidates set across all stores; sub-step e' marks contradicting rb/guard/bel/sig for review (retire via existing `*-update.sh` scripts). |
| G13 | ✓ R16 | `/encode-session` Lane 1 dedup quality | Lane 1 used `tree-find-node` (substring) + `reasoning-bank-read --category` (unfiltered category dump) — missed semantic matches and entries in sibling categories. | `.claude/skills/encode-session/SKILL.md` Lane 1.0 — Pre-Encoding Retrieval added; runs `retrieve.sh --depth medium --read-only` per topic and stashes snapshot. Sub-lanes 1.1/1.2/1.3 consult snapshot first; narrow tools are now fallback only. `--read-only` avoids distorting topic utility_ratio during chat-mode encoding. |
| G14 | ✓ | Universal read-target-artifact-before-edit | No gate ensuring the agent has Read a file before Editing it — stale-context edits land on wrong lines or overwrite concurrent changes | `.claude/rules/read-before-edit.md` — behavioral rule (Layer A, all files). Advisory PreToolUse[Edit\|MultiEdit] gate `core/scripts/pre-edit-context-gate.sh` (Layer B) delegates to `context-reads.py check-file`, so it fires ONLY for the manifest's trackable subset (`core/config`, `.claude/skills`, `world/knowledge/tree`, `world/conventions`) and is silent for out-of-scope files (`core/scripts`, `.claude/rules`, agent files, product code) to avoid guaranteed false positives — never blocks (always exit 0). SKILL pre-read patches in `respond/SKILL.md` (self-update, remember-fact), `aspirations-state-update/SKILL.md` (deep path), `felt-sense-checkin/SKILL.md` (Phase 7). |
| G15 | ✓ | Assistant-mode mid-directive drift re-retrieve | `/respond` Step 5 directive writes could drift from the retrieval done in Step 4 when the directive subject diverges from the original query | `respond/SKILL.md` Step 5.5 — mid-directive drift re-retrieve added between directive routing steps. |
| G16 | ✓ | Board/team-state refresh across assistant/reader turns | `/respond` in assistant and reader modes did not refresh board or team-state context between user turns — stale coordination state informed answers | `respond/SKILL.md` Step 4c — board/team-state refresh added before the Mode Gate in assistant/reader turns (both probes read-only, so reader benefits too). |
| G17 | ✓ | `encode-stable-facts-gate.py` wired into zero paths | The gate existed but was not invoked from any execution path — stable facts were never checked at discovery time | Wired into `aspirations-execute` Phase 4 pre-execution (between the Unblock-intake probe and the Intelligent Retrieval Protocol) and `research-topic` Step 1.5 (before Step 2 Research). |
| G18 | ✓ | State-update deep encoding pre-read | `/aspirations-state-update` deep-path encoding wrote tree nodes without first reading the target node's current content | `aspirations-state-update/SKILL.md` deep-path — "Read node.file" step added before tree-node write. |
| G19 | ✓ | Phase 4 missing Tier-2 codebase probe | Goal execution Intelligent Retrieval fired Tier 1 (tree) but skipped Tier 2 (codebase grep/glob) for implementation goals that modify code | `core/config/execute-protocol-digest.md` Step 5d — Tier-2 codebase probe added for goals whose primary action targets source files. |
| G20 | ✓ | CREATE_BLOCKER missing pre-filing retrieval | Blockers filed without retrieving R/G about the failure category — prior lessons about the same failure mode were ignored | `core/config/create-blocker-protocol-digest.md` — pre-evidence `retrieve.sh` call added before blocker evidence assembly. |
| G21 | ✓ | Accepting a background-task verdict | A `<task-notification>` reports the exit code of the process the harness launched, which is routinely NOT the one that matters — a trailing pipe substitutes the pipe's status (guard-1150), a self-classifying runner writes its verdict to a LOG not to `$?`, and a fail-open wrapper exits 0 by contract. Measured: a notification read exit 0 while the run's log read `RUNNER_EXIT=2 VERDICT=INVALID`. guard-1431 / guard-1341 / guard-1150 / guard-1096 all encode this and it still landed a 4th time in one session — so it is a retrieval gap, not a knowledge gap: nothing surfaces those four at the moment of acceptance. | Layer A: `.claude/rules/verify-before-assuming.md` rule 4a — read the log before accepting the verdict. **Layer B built 2026-08-21**: `core/scripts/task-output-read-advisory.sh` (PreToolUse[Read], registered in `.claude/settings.json`) fires ONLY on `tasks/*.output` paths — the one chokepoint that IS a tool call (a notification is not one, so this is the entire buildable surface) — and surfaces guard-1150/1431/1341/1096 at the moment of acceptance. Advisory-only, never blocks, pure-bash prefilter so every other Read exits instantly. |

## Engine behavior notes

The matcher in `core/scripts/retrieve.py` works in two layers:

1. **Tree nodes** — multi-channel matching: Substring, Entity-index,
   Word-prefix, Concept (`.md` front-matter entities). TF-IDF cosine bonus,
   recency decay, MMR diversity rerank when top-K cap binds. Token-overlap
   works for any free-text query.
2. **Supplementary stores (R/G/P)** — single-channel matching:
   `_entry_matches_category` does bidirectional substring against the
   entry's `category` field only. Free-text queries that don't match a
   category exactly return zero supplementary hits even when title /
   content / tags / when_to_use would obviously match.

This bimodal behavior is **G9**. R3 adds a text-fallback layer that activates
when category matching returns empty — restoring symmetry between tree-node
and supplementary-store free-text retrieval.

**Two predicates exist, and only one of them is the membership answer.**
`_entry_matches_category` (narrow, category-substring) is still callable and is
still correct for code paths that genuinely want exact-category semantics.
`_entry_matches` (= category OR text-overlap) is what the three loaders use to
SELECT the return set. Anything downstream that needs to know "which entries did
this retrieval return?" must take the loaders' return set directly — never
re-compute it, with either predicate. Re-deriving is what broke the attestation
manifest for months (see G9 above): the narrow predicate silently dropped every
text-fallback entry, and because the bump-set follows the RETURN set, those
entries accrued `retrieval_count` they could never offset with `times_helpful`.
`load_reasoning_bank`'s docstring states the invariant — the bump set MUST equal
the return set — and it binds every consumer of that set, not just the bumper.

One consequence worth stating because it reads as a workaround and is a weak
one: leading a free-text `--category` string with a real category key does NOT
restore membership generally. The narrow predicate is a bidirectional substring
test against the whole query, so an anchor rescues only entries whose own
category literally appears in the string — measured 6 of 20 guardrails and 0 of
20 reasoning-bank rows on one live query. Post-fix the anchor is unnecessary;
pre-fix it was never sufficient.

## Cross-references

- `core/config/conventions/retrieval-escalation.md` — three-tier escalation policy
- `core/config/conventions/tree-retrieval.md` — engine details (matchers, caps, ranking)
- `core/config/conventions/exhaustive-search-before-negation.md` — protocol for negative conclusions
- `.claude/rules/retrieve-before-deciding.md` — decision tree for "should I retrieve now"
- `.claude/rules/code-review-protocol.md` — Step 4 pre-apply consultation
- `.claude/rules/verify-before-assuming.md` — multi-signal rule for negative claims
- `.claude/rules/encode-stable-facts.md` — retrieve-before-discovery for resource locators

## Maintenance

When adding a new retrieval trigger (any new skill or skill phase that calls
`retrieve.sh`, `tree-find-node.sh`, `reasoning-bank-read.sh`,
`guardrails-read.sh`, or similar), add a row to the **Active triggers**
table above. When identifying a place where retrieval should fire but
doesn't, add a row to the **Gap triggers** table with a stable Gxx
identifier. Stable identifiers prevent renumber-drift when the catalog
grows.


## Decision-point evidence (moved from `.claude/rules/retrieve-before-deciding.md`, 2026-08-17, g-115-6581)

The rule keeps each decision point as a one-to-three-line imperative; the
measured narratives that justify the three least-obvious points live here.

### Point 11 — filing a discovered-work goal that prescribes a fix

11. **Filing a discovered-work goal that prescribes a fix** — the
sibling of #5, and the one most often skipped, because filing feels
like recording rather than deciding. A goal written the moment a
problem lands splits into a measured DIAGNOSIS and an unmeasured
REMEDY: the diagnosis carries the evidence that motivated the filing,
the remedy carries only momentum. Whoever executes it then inherits
that remedy as scope and implements it, because the goal reads as
evidence-backed throughout. Retrieve against the *remedy* before
writing it down — the cheaper fix is often already encoded. See
`rb-5669` (a runtime-provisioning goal whose one-line remedy was
already in the knowledge tree, and collapsed to one command on
execution) and `guard-1719` (the same diagnosis-vs-remedy split seen
from the guardrail-reading side).

### Point 12 — running a probe whose EMPTY result will authorize an action

12. **Running a probe whose EMPTY result will authorize an action** — an
ownership check before filing, a duplicate scan, a "does this exist
yet" grep, a suppression gate's lookup. This is the one that hides
best, because a probe feels like gathering information rather than
deciding. It is not: a probe whose zero authorizes a write has already
made the decision, and the retrieval that would have told you the
instrument's contract is exactly the retrieval nobody runs. Retrieve
against the PROBE ITSELF — the tool, its flags, its failure modes —
not only against the subject you are probing for.

Measured twice on the same script by the same agent, three days apart
(2026-08-10 g-306-199, 2026-08-13): `aspirations-query.sh` takes no
`--source` flag and refuses it. Both times the refusal was piped into
a parser, read as a clean empty, and used as evidence of no owner;
both times a duplicate goal was filed against a live pre-existing one
(the second was g-335-1187 against g-335-1185, both product goals in
the same aspiration — so the cost is two agents doing the same product
work). `guard-3362` was written after the first occurrence, is
correct, is about this exact script and flag, and did not fire the
second time — because the discipline it carries is only reachable by
someone who retrieves before probing. A guardrail cannot defend a
moment its owner does not recognize as a decision.

### Point 13 — computing a census or aggregate over a store

12. **Computing a census or aggregate over a store** — any count, tally,
distribution, percentage, or "N of M" you intend to report or act on.
Retrieve on the MECHANISM ("counting records in a JSONL store by
tallying one field", "producing a zero or empty count from a filter I
wrote this turn"), not only on the subject the census is about. The
two-query discipline and the dilution defect behind it are documented
in § "Why TWO queries" below (this file is now the SSOT for the mechanism;
the rule entry only widens WHEN it applies).

The mechanism was measured on framework-file fixes (16/16) and its
requirement lived inside a step scoped to those, so it never reached
an agent about to count rows. Measured on four non-framework censuses
(goals-by-status, guardrails-by-category, tree-nodes-by-capability,
board-messages-by-channel; cc-07, 2026-08-10, `--read-only`, same
flags and depth, only phrasing varied), the mechanism query returned
**15, 19, 20 and 19 guardrails of 20 that the subject query did not**,
including the ones that decide whether a count is trustworthy at all:
run a known-non-empty positive control before believing a zero
(`guard-2421`), print the unfiltered population beside the filtered
count (`guard-2273`), never conclude zero from truncated output
(`guard-1941`), a 0 is ambiguous between "counted zero" and "never ran"
(`guard-1641`), a status filter is a valid filter but an invalid
denominator (`guard-1700`). None of these are about any census's
subject, so no subject phrasing reaches them.

The tree-maturity census is the sharpest case: its subject query
returned **4** guardrails total against the mechanism query's 20, with
zero overlap.


### Mechanism phrasing is necessary but NOT sufficient — the operative variable is VOCABULARY

The subject-vs-mechanism axis above is about what the query is *about*. A
second, independent axis decides whether a well-aimed query lands at all:
whether its WORDS are the ones the entry literally contains. `retrieve.sh`
ranks by token overlap, so it is nearly blind to a paraphrase that describes
the same idea in different words — including a paraphrase that is correctly
mechanism-phrased.

Measured 2026-08-26 (bravo, cc-05) against `guard-1826` (`times_active`
2101), whose rule ends *"A sweep hit is evidence that a condition HOLDS,
never evidence that it is UNREPORTED"*. Same flags, same depth, only
phrasing varied:

| query | tokens | phrasing | result |
|---|---|---|---|
| "stateless sweep re-surfaces same item every cycle repeated board mentions are lane cadence not evidence of neglect" | 16 | mechanism, my words | **absent** from top-8 |
| "stateless sweep resurfaces hits" | 4 | mechanism, my words | **absent** — though `STATELESS` appears verbatim in the rule's first clause |
| "sweep hit evidence condition holds not unreported" | 7 | mechanism, **the rule's words** | **rank 1** |

The 4-token miss is the load-bearing row: it falsifies token-dilution-by-
length as the whole explanation, because a short query missed too. What the
winning query has is the entry's own closing sentence, near-verbatim.

**Consequence for the two-query discipline:** the second query should differ
in VOCABULARY, not only in subject-vs-mechanism framing. Compose it from the
words a *rule* would use — imperative verbs, store nouns, the exact failure
terms (`empty`, `stale`, `zero`, `holds`, `unreported`) — rather than from
the words of your problem.

**Honest limit:** composing the winning query often requires already knowing
the answer, so this is a structural property of token-overlap retrieval over
a paraphrase-rich corpus, not a skill deficit. The instruction is therefore
"query twice in different vocabularies", never "phrase it better" — the
latter is unfollowable. `guard-5231` carries the behavioural rule and the
distinction from `guard-3665` (whose empty comes from a TIMEOUT and whose
remedy, re-run it, cannot fix a vocabulary miss).

### Invocation-table footnotes

- Free-text queries: supplementary stores fall back to matching `title` /
  `content` / **`rule`** / `summary` + `tags` + `when_to_use.conditions` when
  the category match returns empty (G9 / R3). `rule` is the load-bearing field
  for guardrails: measured 2026-08-10, **0 of 3004 guardrails carry `title` or
  `content`** and all 3004 carry `rule`, so an earlier "title/content/tags"
  gloss named two fields that never exist on a guardrail and omitted the one
  holding every word of it.
- `--include-framework` on the pre-apply consultation is REQUIRED, not
  optional: without it the response carries no `framework_rules` key at all,
  so the rules/conventions most likely to already prescribe the fix are
  silently absent (g-115-3777). Framework retrieval is token-overlap on title +
  section headers + first 500 chars, returned under `framework_rules` (closes
  G8).

### Enforcement note — the advisory pre-edit gate

- Advisory PreToolUse[Edit] gate `core/scripts/pre-edit-context-gate.sh` —
  checks `context-reads.txt` for a prior Read of the target file and prints a
  stderr advisory if absent. NEVER blocks (always exits 0). Fires only for the
  manifest's trackable subset (`core/config`, `.claude/skills`,
  `world/knowledge/tree`, `world/conventions`); silent for out-of-scope files
  like `core/scripts`/`.claude/rules`/agent files, where Rules 1-3 are the only
  safeguard (see `.claude/rules/read-before-edit.md` Rule 4, retrieval-triggers.md G14)


## Why TWO queries — the pre-apply consultation's subject+mechanism discipline (SSOT; moved from `.claude/rules/code-review-protocol.md`, 2026-08-17, g-115-6581)

**Scope note: the dilution defect is not specific to framework-file fixes.**
This section is the SSOT for the mechanism, and it sits inside step 4, which is
scoped to framework edits — so for a long time the requirement only reached an
agent about to edit a framework file. It was re-measured on four non-framework
CENSUSES (cc-07, 2026-08-10) with the same result, and
`retrieve-before-deciding.md` decision point 13 (the census point) now carries that trigger. Treat
the mechanism below as applying wherever you are about to act on a retrieval,
not only here.

A subject-phrased query systematically misses guardrails indexed on the
MECHANISM. Measured over the 16 most recent completed framework goals carrying
a recorded consult query (`world/retrieval-trace.jsonl`, joined on
`work_class: framework`), holding flags constant and varying only phrasing:
**16/16 had at least one mechanism-only guardrail that would have changed how
the fix was made** — the bar was fixed before looking, and topical adjacency did
not count. Threshold for acting was 30%.

The cause is **token DILUTION under the top-20 cap, not token disjointness.**
This distinction is load-bearing and the originating hypothesis had it wrong:
g-115-4521's subject query *contained the word "daemon"* ("refuse claim on
terminal-status goal in **daemon** claim endpoint...") and still did not return
guard-742; the mechanism query returned it at rank 1. One mechanism token among
twelve subject tokens cannot lift its guardrail past the cap. This matters for
the fix direction: were the cause disjointness, the remedy would be a retrieval
change (synonym expansion). Because it is dilution, a dedicated second query —
which gives the mechanism clause 100% of a query's weight — is the right shape,
and raising the cap would not reach it.

**Do not collapse this into one combined query — measured and rejected.**
Concatenating subject + mechanism recovered the target in only 4 of 6 spot
checks, and where it survived the rank collapsed: 1→18 (one slot from falling
off), 1→12, 4→9, 2→7, with two rank-1 hits vanishing entirely. Adding subject
tokens dilutes the mechanism tokens by the very mechanism the defect describes.

Worked examples — the mechanism is the OPERATION, not the topic:

| Fix (subject) | Mechanism query | Recovered |
|---|---|---|
| refuse claim on terminal-status goal in daemon claim endpoint | `editing logic behind a daemon-routed wrapper where the live path is the daemon reimplementation` | guard-742, guard-547 |
| align a date-only deadline comparison between two sweeps | `changing a shared predicate that two independent consumers both read` | guard-2275 |
| conf-based test world-isolation defeated by .mind-data | `editing pytest conftest fixtures and test environment isolation` | guard-1165, guard-588 |
| phantom tree nodes recurred | `writing a knowledge tree node and registering it in _tree.yaml` | guard-2317, guard-1195, guard-610 |
| pre-apply-consult-gate skips self-filed goals | `editing a gate predicate under core/scripts/gates` | guard-502, guard-142 |
| generalize the embedded-python-block compile guard | `authoring a new repo-wide scanner and wiring it into the pre-commit gate chain` | guard-1426, guard-914 |

Control run: pairing each goal's subject query against a MISMATCHED goal's
mechanism query returned guardrails relevant to the other goal's operation and
irrelevant to this one — so the effect is the mechanism framing, not an artifact
of the 20-cap. (The one partial overlap was two genuinely adjacent mechanisms,
both "editing a bash wrapper", which is the control behaving correctly.)

**Enforcement reality (guard-302 — name the real mechanism, not an inferred
one):** no gate counts queries. `pre-apply-consult-gate.py` fires per-goal on
framework-file prose, and `pre-apply-consult-drift-gate.py` keys the Phase
0-pre6 sentinel on `retrieval-summary: performed=false` — a boolean. Running one
query satisfies both gates exactly as running two does. The second query is
honor-system, like the rest of step 4; it is written down because it was
measured, not because anything will refuse you for skipping it.

Baseline for whoever checks whether this rule changed anything (measured
2026-08-02 from `world/retrieval-trace.jsonl`, all 9,309 rows): of 2,125 goals
with at least one goal-tied, non-read-only consult, **508 — 23.9% — issued 2+
distinct queries; 76.1% issued exactly one.** Recent windows run higher (39.9%
over 7d, 32.4% over 14d, 25.0% over 30d), so re-measure the window rather than
diffing against the all-time figure. Caveat that biases this number DOWNWARD:
only goal-tied rows can be attributed, so a second query issued without
`--goal` is invisible here — treat 23.9% as a floor, not a point estimate. The
comparison that survives the bias is before/after on the same window width,
since the undercount applies equally to both.

## Why the pre-apply consultation exists at all (moved from `code-review-protocol.md` § "Why step 4 matters")

Without consultation, ad-hoc fixes can violate existing learnings.
Canonical incident (2026-05-09, rb-774): a fresh-eyes review fixed an
`os.environ['WORLD_DIR']` KeyError in a SKILL.md by switching to
`'$WORLD_DIR'` bash interpolation inside `python3 -c` source. That fix
DIRECTLY violated `guard-165` ("never interpolate bash variables into the
Python source text — pass values via env, single-quote python source").
The wrong-direction fix was caught only by a subsequent /encode-session
retrospective. Step 4 closes that gap.

Use `retrieve.sh --category "<free-text>"` for token-overlap retrieval,
not a strict category key. The parameter name is historical — the
engine treats the value as free text and runs Substring/Entity-index/
Word-prefix/Concept matching on tree nodes. Categorization is exactly
where the canonical incident failed: `guard-165` lives under
`framework-architecture`, not where one would naturally look for a
SKILL.md python-in-bash fix. Token-overlap on free-text retrieves
tree nodes regardless of category key.

For supplementary stores (reasoning bank, guardrails, pattern
signatures), the supplementary matcher historically required a
category-key substring match — free-text queries returned zero
supplementary hits. The fallback added 2026-05-12 (see
`core/config/conventions/retrieval-triggers.md` G9 / R3) now matches
free text against `title` / `content` / **`rule`** / `summary`, plus
`tags` and `when_to_use.conditions`, when category match returns empty —
restoring symmetry with tree-node matching (`retrieve.py` `_entry_matches_text`).

`rule` is the load-bearing field and was missing from this list until
2026-08-10. Measured over the live corpus that day: **0 of 3004 guardrails
carry `title` or `content`; all 3004 carry `rule`.** So the earlier gloss
named two fields that never exist on a guardrail and omitted the one holding
every word of it — which sends anyone debugging a guardrail that failed to
surface to inspect fields that are structurally absent.


### The pre-edit context gate — full history and scope (moved from `.claude/rules/read-before-edit.md` rule 4, 2026-08-17, g-115-6581)

4. **The automated safety net is PARTIAL — Rules 1-3 are the real guarantee**:
`core/scripts/pre-edit-context-gate.sh` is wired as a PreToolUse[Edit|MultiEdit]
advisory hook. It consults the session's context-reads manifest (via
`context-reads.py check-file`) and, when the target has not been Read, emits
the advisory on two channels: a stderr banner (what a human watching the
terminal sees) and a structured `permissionDecision: "allow"` payload, which
is the only channel that reaches the model. It never denies and never blocks
— seeing the advisory means stop and Read the file first.

**Two distinct advisories, matching Rule 1's conditional** (g-115-3747).
"has not been Read this session" means no read of any kind was recorded — Read
it. "was Read only in part this session (ranged read)" means the file WAS
opened with offset/limit/pages, so Rule 1's "count only if they cover the
region being edited" is now yours to evaluate: if your ranged read covered the
region you are about to change, proceed; if not, read that region first. Until
g-115-3747 ranged reads were discarded by the recorder outright, so they
produced the *first* message — a false claim, fired on every large file, which
is exactly the file whose advisory most needs to be believed. Note the gate
deliberately does NOT go silent on a ranged read: silence would assert full
context the manifest cannot vouch for, trading a false alarm for a false
all-clear.

**It did nothing at all from 2026-05-30 to 2026-07-28** (g-115-3731). Two
independent defects, either sufficient alone: it bailed on an unset
`MIND_AGENT`, which PreToolUse[Edit] never provides, so it exited before its
own check on every real invocation; and it wrote only to stderr, which a
non-blocking PreToolUse hook cannot deliver to the model (guard-1680). It
hand-tested green the whole time, because a hand-run shell HAS `MIND_AGENT`
set — the only environment where it failed was the only environment where it
ran. If you are reading a version of this rule dated before that fix, it was
describing a net that was not there. Both defects are now mutation-proofed by
production-shape tests in `core/scripts/tests/test_pre_edit_context_gate.py`.

**On Windows the 2026-07-28 revival did not take effect until 2026-07-29**
(g-115-3820). Three further defects, each Windows-only and each silent, kept
the gate 100% inert on this platform after it was declared fixed — so for one
more day the paragraph above was still describing a net that was not there,
just on fewer boxes. (1) The cheap path pre-filter added the SAME DAY as the
revival matched forward-slash globs, but Claude Code sends `file_path` in
native form, so every backslashed Windows path fell through to `*) exit 0` —
a false REJECT on 100% of Windows edits, violating that pre-filter's own
stated invariant, and killing the gate in BOTH the hand-test and production
shapes. (2) `source _platform.sh` ran before agent resolution; it exports
`MSYS_NO_PATHCONV=1`, under which `session-binding-read.sh` resolves to empty
on Git Bash (g-304-19) — the three sibling context-reads hooks already carry
the ordering fix and its comment, and this gate was the family member that
missed it. (3) `tree-write-fence.sh` redirected to `/dev/stderr`, which does
not resolve when stderr is a pipe, so `|| exit 0` ate the entire fence
invocation under every captured-output caller.

The through-line worth carrying: all three hand-tested green for the same
reason the original 59-day inertia did — an interactive shell has no
`MSYS_NO_PATHCONV`, a terminal has a real `/dev/stderr`, and a hand-typed
path uses forward slashes. **The only environment where the gate failed
remained the only environment where it actually ran.** A green suite on one
OS is not evidence a hook works; this gate has now been declared fixed twice
while inert. Treat platform as part of the production shape (guard-920), and
record the box and OS with any claim that a hook is live.

The gate fires ONLY for the path classes the context-reads manifest
advisory-tracks: `core/config/**`, `.claude/skills/**`,
`world/knowledge/tree/**`, `world/conventions/**`, `aspirations-compact.json`,
and — since g-115-2210 — `core/scripts/**` (framework code, the surface where
loop self-evolution lands). For everything else — `.claude/rules/**`,
`agents/<agent>/**` (including `self.md`), and all product-code / external
files — the gate stays **silent by design**: a read of those is never
recorded in the manifest, so a "has not been Read" warning there would be a
guaranteed false positive that desensitizes you to the banner. The absence of
a warning is therefore NOT evidence you have current context. Rules 1-3 are
honor-system for the out-of-scope majority — the gate backstops only the
trackable subset.

Two further exclusions, both silent and both deliberate. A cheap bash
path pre-filter short-circuits out-of-scope paths before any subprocess
spawn, so the gate adds no measurable cost to an edit it will not act on
(measured cc-05: 53ms before the fix, 55ms after, on an out-of-scope path;
~156ms in-scope, paid only where the gate does its job). And the
constitutional anchor (`.claude/settings.local.json`,
`settings-structural-validator.{py,sh}`) is excluded outright — the payload's
`allow` short-circuits the permission system, and the anchor must never
receive one.

Scope-split caveat (g-115-2210): `core/scripts/**` is *advisory-only*. Its
reads are recorded and the edit advisory fires there, but the separate
PreToolUse[Read] re-read dedup gate (which BLOCKS a redundant whole-file
re-read) keeps the NARROWER pre-2210 scope — so a mandated whole-file
re-verify of a script after a linter/user touch (verify-before-assuming.md)
is never refused as "already in context." The `is_in_scope` (narrow, dedup)
vs `is_in_scope_advisory` (wide, recorder+advisory) split in
`context-reads.py` is the single source of truth for this.
