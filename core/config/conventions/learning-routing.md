# Learning Routing — Where Does This Learning Go?

## Purpose

When the agent (human author or LLM) has just learned something, the encoding
question is: **which store does this belong in?** The framework has a small
set of learning stores, each with its own convention file and schema (see the
table below for the authoritative list). Prior to this
file, the routing logic lived implicitly across `no-auto-memory.md`,
`reasoning-guardrails.md`, `experience.md`, and the sensory-buffer section
of `memory-pipeline.yaml`. Authors assembled it on the fly.

This file is the single place to answer the question. Detail schemas and
script APIs live in each store's detail convention file — this file only
routes.

## The Learning Stores (at a glance)

| Shape of learning | Primary store | Detail file |
|-------------------|---------------|-------------|
| Reusable diagnostic ("when you see X, cause is Y") | Reasoning bank (`world/reasoning-bank.jsonl`) | `reasoning-guardrails.md` |
| Prescriptive rule ("always do X / never do Y") | Guardrails (`world/guardrails.jsonl`) | `reasoning-guardrails.md` |
| Prediction with outcome-in-future | Pipeline (`world/pipeline.jsonl`) | `pipeline.md`, `hypothesis-conventions.md` |
| Named recurring pattern with outcome stats | Pattern signatures (`world/pattern-signatures.jsonl`) | `pattern-signatures.md` |
| Compressed domain fact ("X is Y") | Knowledge tree (`world/knowledge/tree/`) | `tree-retrieval.md`, `knowledge-conventions.md` |
| Full-fidelity trace (tool outputs, evidence) | Experience archive (`agents/<agent>/experience.jsonl` + `experience/`) | `experience.md` |
| Stable operational value (path, ID, endpoint) | Resource locators (`world/conventions/*.md`) | `resource-locators.md`, rule `encode-stable-facts.md` |
| Session narrative ("today we did A, B, C") | Journal (`agents/<agent>/journal.jsonl` + `journal/`) | `journal.md` |
| Live-session RAM (micro-hyp, blockers, debt) | Working memory (`agents/<agent>/session/working-memory.yaml`) | `working-memory.md` |
| Reusable spark prompt | Spark questions (`meta/spark-questions.jsonl`) | `spark-questions.md` |
| Cross-agent casual musing | Reasoning channel (`world/board/reasoning.jsonl`) | `board.md` |

## Decision Tree

Run top-down. Stop at the first match that fits. Many learnings match
more than once — see "Multi-store encodings" below for how to split.

1. **Is this a stable operational value** (path, ID, URL, endpoint, resource name)?
   → Resource locator. Append to the appropriate `world/conventions/*.md`.
   Rule: `.claude/rules/encode-stable-facts.md`. Three-probe threshold:
   if you ran 3+ discovery commands, STOP and encode.

2. **Is this session-scoped transient state** (active goal context,
   micro-hypothesis, knowledge debt, in-flight blocker)?
   → Working memory. Evicted at session end — do NOT promote here anything
   that needs to survive the session.

3. **Does this predict a future outcome** (testable by time `T`)?
   → Pipeline (hypothesis). Choose horizon: `micro`, `session`, `short`,
   `long`. Record `context_manifest` so the resolution can cite what was
   consulted when the prediction was formed.

4. **Is this a reusable diagnostic or prescriptive rule?**
   - Diagnostic ("when you see X, the cause is Y") → reasoning bank
   - Prescriptive ("always do X / never do Y") → guardrail
   - **Often both** — see Multi-store encodings below

5. **Is this a named recurring pattern with outcome stats** (confirmed /
   total / accuracy), minable across hypotheses?
   → Pattern signature. Track via `pattern-signatures-record-outcome.sh`.

6. **Is this a compressed domain fact** ("X is Y" that readers need in
   hierarchical context, with confidence and capability tracking)?
   → Knowledge tree. Run through the encoding gate (threshold 0.40 per
   `memory-pipeline.yaml`). Durable; not session-scoped.

7. **Is this full-fidelity evidence** (tool outputs, verbatim anchors,
   exact commands and their results) that supports a goal or hypothesis?
   → Experience archive. The store of record for "what did we actually try
   and see." Link back to `goal_id` and `hypothesis_id`.

8. **Is this a reusable prompt that sparks new hypotheses?**
   → Spark questions (`meta/spark-questions.jsonl`).

9. **Is this a short narrative I want another agent to see right now,
   without asserting any claim?**
   → Reasoning channel on the board. Read-always, write-voluntary.

10. **Default for session-level summary** ("today N goals completed, these
    key events happened")
    → Journal. Narrative, not structured.

11. **Is this a NEW behavioral rule, protocol, or convention I want to
    encode as a file in the framework** (not just an RB entry — a durable
    `.md` doc that other skills and rules read)?
    Apply the **Rules vs Conventions** decision sub-tree:
    - **Domain-agnostic behavioral imperative** ("the agent MUST do X before Y",
      "never assume Z without verifying"). Universal — applies regardless of
      what the agent is learning about.
      → `.claude/rules/<kebab-case>.md`. Imperative voice. No
      brand/product/service/endpoint names. Pedagogical examples may use
      generic placeholders (`agent-a`, `service-x`, `the framework`) but
      MUST NOT name domain-specific things.
    - **Domain-specific operational rule** (specific service endpoint, branded
      workflow, product-specific protocol, named API/integration). Only
      applies inside ONE world/domain.
      → `world/conventions/<kebab-case>.md`. The domain's own conventions
      dir, not core. The framework loads core conventions first
      (`load-conventions.sh` → framework priority), then falls back to
      world's overlay (per `domain-overlay-pattern.md`).
    - **Framework structural protocol/schema/API contract** (JSONL field
      definitions, script CLI signatures, integration catalogs across
      multiple subsystems). Describes WHAT a framework subsystem looks
      like, not HOW the agent should think.
      → `core/config/conventions/<kebab-case>.md`. Declarative voice.
      Domain-agnostic. Catalog-style. Skills load via `conventions:` front
      matter.

    Three layers of enforcement back this routing:
    - **Layer A (preventive)**: Skill-level PLACEMENT CHECK injections in
      `/aspirations-state-update` Step 8, `/aspirations-spark` Phase 6.5,
      `/encode-session` Lane 1.3, `/forge-skill` Step 3, `/respond` Step 5.
    - **Layer B (gate)**: `core/scripts/rule-vs-convention-gate.{py,sh}`
      runs as PreToolUse[Write/Edit/MultiEdit] in `.claude/settings.json`.
      Refuses writes to `.claude/rules/*.md` that introduce
      domain-blocklist content. Override: include the marker
      `domain-leak-exempt: <rationale>` in the file (or in the proposed
      content) — the gate honors the marker and approves the write. The
      sibling marker-placement-gate then prevents over-applying that same
      marker to SKILL.md / convention files (see § Phase 5).
    - **Layer C (detective)**: `core/scripts/domain-leak-check.sh` scans
      `.claude/rules/`, `core/config/conventions/`, AND `mind_api/src` +
      `mind_api/tests` (Phase 3.2 extension) for domain blocklist tokens.
      Files carrying the `domain-leak-exempt:` marker are skipped (see
      `.claude/rules/domain-free-examples.md` "Marker Placement"); the
      marker is reserved for executable code where domain strings are
      functional (regex patterns, fixtures, sentinel arrays).

    Discovery (which convention covers X?): see the conventions-registry
    tree node at `world/knowledge/tree/system/conventions-registry.md`.

If NOTHING matches, the observation is likely too low-signal to encode. Let
it drop. Over-encoding bloats retrieval and costs future cycles.

## Multi-store Encodings (the normal case)

A single observation frequently needs to land in two or three stores, each
capturing a different dimension. The framework provides **linking fields**
so the stores stay connected. Use them; free-text `source: "..."` strings
decay faster than structured refs.

| Pair | Linking field (canonical) | When |
|------|---------------------------|------|
| Reasoning bank + guardrail | `rb.preventive_guardrail: guard-NNN` | Diagnostic has a prescriptive counterpart (e.g., rb-334 + guard-308) |
| Reasoning bank + experience | `rb.experience_ref: exp-...` | Lesson came from a specific execution trace |
| Guardrail + experience | `guardrail.experience_ref: exp-...` | Rule was derived from a specific execution trace |
| Pipeline + experience | `pipeline.experience_ref: exp-...` | Hypothesis has supporting evidence trace |
| Experience + knowledge tree | `tree_node.md experience_refs: [exp-...]` AND `experience.tree_nodes_related: [node_key]` | Evidence supports a domain fact |
| Guardrail + pattern signature | `guardrail.related_patterns: [sig-NNN]` | Rule triggered by a tracked pattern |
| Working memory + any durable store | `wm.encoding_queue[].target_article` (tree) / `wm.knowledge_debt[].node_key` (tree) | Session captured something that must be consolidated later |
| Board post + goal / hypothesis | tags like `goal:g-NNN-NN`, `phase:X`, `because:rb-NNN` | Musing references existing work |

With `experience_ref` live on both RB and guardrail records, the evidence
chain is now whole: `guardrail → reasoning bank → experience → goal`.

## Experience vs Journal — the definitional pair

Both stores describe "things that happened." They operate at different
granularities and different fidelities and serve different readers.

| Axis | Experience | Journal |
|------|-----------|---------|
| Unit | One goal or hypothesis | One session |
| Fidelity | Full — tool outputs, verbatim anchors, evidence | Narrative summary only |
| Structured refs | `goal_id`, `hypothesis_id`, `tree_nodes_related`, `enabled_by[]` | Free-text `key_events` |
| Retrieval | By category, goal, hypothesis, type; utility-tracked | By session number or date |
| Archival | Staleness-based (30d unused, 90d low-utility) | Never archived |
| Purpose | "What did we try and see?" — evidence for calibration | "What happened today?" — log for humans / cross-session continuity |

**Mnemonic: experience is evidence; journal is narrative.** They are
additive, not alternatives. A session-end write produces one journal record
AND many experience records.

**Guardrails should link to experiences, not journal entries.** Experiences
have structured `goal_id` / `hypothesis_id` refs enabling precise
traceability; journal entries compress across a whole session and have only
free-text `key_events`. The structured `experience_ref` field on guardrails
and RB records closes this evidence loop.

## Worked example: rb-334

**Observation**: `probe-bridge.sh` had a stale default port 3001, but
`roblox-bridge.py` listens on PORT=28080. Every probe failed silently. A
canonical-probe blocker was filed on the spurious failure and the flywheel
stalled for 14 hours before the port mismatch was noticed and fixed.

**Walking the decision tree**:

1. Stable operational value? No — the *bug* is that the stale default
   value contradicts the truth. The true port (28080) IS a stable
   operational value; it belongs in `world/conventions/service-endpoints.md`
   if not already there. (That encoding is separate from the lesson.)
2. Session-scoped? No — this is a reusable pattern.
3. Predicts a future outcome? No — post-hoc explanation.
4. Reusable diagnostic or prescriptive rule? **Both.**
   - Diagnostic ("when canonical probe keeps failing, check for port or
     constant drift between probe script default and the service it
     probes") → reasoning bank → **`rb-334`**
   - Prescriptive ("before trusting a probe failure, verify the probe's
     hardcoded defaults match the current service constants") → guardrail
     → **`guard-308`**
5. Named recurring pattern? Potentially — "stale default drift" could
   become `sig-NNN` after N more occurrences. Leave as RB + guardrail
   for now; promote to pattern signature if it recurs.
6. Compressed domain fact? No — too contingent to warrant a tree node.
7. Full-fidelity trace? The original failing-probe execution IS an
   experience record already, via Phase 4 archival. Link the new RB
   entry's `source_goal` to that goal id — no new experience needed.

**Routing outcome**: `rb-334` (RB) + `guard-308` (guardrail), linked via
`rb-334.preventive_guardrail: guard-308`. No new tree node. No new
experience (the failing goal's experience record already carries the
evidence).

## Why the Phase 1b classification subset is narrower than the full Decision Tree

The 11-store table at the top of this convention covers every learning
destination in the framework. Phase 1b of `/felt-sense-checkin`, by
contrast, operates on entries already in `agents/<agent>/insights.jsonl` and
routes them to a SUBSET of those stores: **tree, reasoning_bank,
guardrails, experience, drop**.

The other six stores (pipeline, working memory, pattern signatures,
journal, resource locators, board, spark questions) are not Phase 1b
targets because:
- Pipeline / pattern signatures are written via hypothesis resolution paths
- Working memory is session-scoped and clears at session end
  (insights.jsonl outlives WM)
- Journal, locators, board, sparks have their own dedicated capture paths
  (`journal-add.sh`, locator conventions, `board-post.sh`, spark questions
  JSONL)

So "Phase 1b classification" is the curation pass that drains
`insights.jsonl` into the four DURABLE knowledge stores, plus a deliberate
`drop` outcome when the entry doesn't warrant any of them.

## Phase 1b Classification — Worked Examples

Each example: short insight snippet + correct route + 1-sentence rationale.
Use these as anchors when the LLM judgment in Phase 1b is uncertain.

### Route: `tree` — domain fact / architectural observation

> "The widget-pipeline's reward_computer module skips reward attribution
> when the data-unit's startState field is empty — observed at 17% of
> units (63/373) in run 1776869591071."

→ `tree`. Quantitative architectural fact about pipeline reward
attribution; durable, hierarchical, retrieval-relevant for downstream
behavior-modeling goals.

> "ConsolidatedMemory writes occur post-batch, not per-step. Per-step state
> churn does NOT trigger consolidation passes."

→ `tree`. Mechanism-level fact about memory-consolidation timing; readers
of the cognition-modeling tree branch need this in hierarchical context.

> "Cold-start latency for the deploy-pipeline endpoint averages 11.4s;
> warm invocations average 0.4s. Cold-start dominates first-call response
> time."

→ `tree`. Quantitative performance characteristic; belongs under a
production-services tree branch with confidence + capability tracking.

### Route: `reasoning_bank` — recurring diagnostic / ABC chain / meta-pattern

> "When `subprocess.run(['bash', script])` runs on Windows, Python's PATH
> resolution may pick `/mnt/c/-rooted WSL bash.exe` instead of Git Bash.
> Symptom: silent partial output. Fix: prefer
> `os.environ.get('MIND_SHELL') or sys.executable`."

→ `reasoning_bank`. Diagnostic chain (when X → cause Y → fix Z) reusable
across any cross-platform subprocess wrapper.

> "When goal-selector returns the same alpha-handoff goal at top across
> N consecutive bravo iterations, the `agent_executable` criterion does not
> distinguish 'any agent' from 'this agent' — capability mismatch slips
> through. Workaround: self-abstain on capability-mismatched candidates."

→ `reasoning_bank`. Recurring multi-agent selection-drift pattern with
diagnostic-and-correction structure.

> "When `efs-ssh.sh` returns silent empty output AND `~/.ssh/known_hosts`
> shows recent rotation, the symptom is host-key mismatch, not network
> failure. Re-add via `ssh-keyscan` clears it without provisioning a new
> credential."

→ `reasoning_bank`. ABC chain (Antecedent: silent ssh failure +
known_hosts age; Behavior: blocked goal; Consequence: agent-provisionable
fix that doesn't need user).

### Route: `guardrails` — prescriptive rule with explicit trigger condition

> "Before declaring a service unavailable based on one failed probe, run
> the canonical companion script TWICE with 30s backoff. One probe is
> insufficient evidence (rb-389)."

→ `guardrails`. Rule with concrete trigger ("about to declare unavailable
from one probe") and actionable correction; catches recurring drift.

> "Before editing SKILL.md pseudocode that names a script's flags, dict
> keys, or output fields, grep the script for those identifiers and confirm
> verbatim match. Drift at the skill-script boundary produces silent no-ops
> at LLM decision points."

→ `guardrails`. Trigger ("editing prose that names script identifiers") +
prescriptive verification step + named failure mode.

> "Before writing `participants: [user]` on a goal OR setting `defer_reason`
> naming an agent-provisionable capability, invoke `capability-gate.py` to
> confirm no agent path exists. If a path exists, choose it."

→ `guardrails`. Already enforced at script level (capability-gate.py); the
guardrail keeps the LLM-side decision aligned with the gate's logic.

### Route: `experience` — full-fidelity execution trace with structured refs

> "Iter-32 fresh-eyes-code review of 17 alpha core/ files. Findings: 13
> total — 3 invalidates (FE-001 outcome_class missing from
> loop-state-save SCHEMA, FE-002 WSL-bash binary-selection, FE-003
> self-drift-gate inconsistency at line 395), 5 constrains, 4 informs.
> Root-cause cluster: `subprocess.run(['bash',...])` Windows resolution.
> Routed FE-001 to g-248-59 Unblock; alpha auto-filed within 3min via
> insight-trigger-gate.py."

→ `experience`. Full review trace with goal_id (g-248-58), structured
findings array, cross-agent routing chain — calibration evidence for
"how does fresh-eyes-code perform" downstream analysis.

> "Production health-check probe: rc=124 (timeout), zero stdout, no
> stderr trace, hung silently. Remote-shell probe rc=0 (200 ok).
> Service-monitor service-count=14 healthy. Conclusion: probe-script bug,
> not service outage. Filed g-115-197 Unblock for alpha after
> canonical-probe re-run (per rb-389 two-probe rule)."

→ `experience`. Production probe trace combining multiple data points,
explicit elimination of competing hypotheses, structured outcome.

> "Tree encoding for WSL-bash subprocess trap: appended one Decision Rule
> + one Verified Value to
> `world/knowledge/tree/execution/ayoai-development-patterns/framework-patterns/framework-guardrails-and-gates.md`
> (165 → 169 lines). Cross-linked to g-115-178 MSYS sweep and FE-001/FE-002/
> FE-003 fresh-eyes findings."

→ `experience`. Encoding action trace with file diff + cross-references —
tells next reader "what did this encoding actually change" beyond the
node's own front matter `last_update_trigger`.

### Route: `drop` — already captured / too thin / stale

> "Felt frustrated when goal-selector kept returning g-248-59 across
> consecutive iterations."

→ `drop`. Emotional reaction without durable lesson; the underlying
selection-drift pattern is already encoded as a `reasoning_bank` example
above. Re-encoding the feeling adds noise.

> "The cargo-cult-detector fired today on g-115-106 at threshold=3 and
> filed an audit-all goal."

→ `drop`. Routine system event; `consecutive_routine` and threshold are
tracked structurally on the goal record. Encoding it elsewhere duplicates.

> "The clock says it's 8 PM and the session has been running for hours."

→ `drop`. Time-context observation with no actionable signal. Working
memory and team-state already record session timing.

## Drop is a positive choice, not a fallback

The first-pass tendency under context pressure is to over-route — every
insight feels like it should land somewhere. `drop` is the deliberate
counter-pressure: insights that already live elsewhere, that re-state a
known reasoning_bank entry in different words, or that record an emotion
without a teaching moment, all belong on the `drop` path. Over-encoding
inflates retrieval cost forever; under-encoding loses signal once. The
asymmetry favors dropping.

## Phase 1b cross-references

- `.claude/skills/felt-sense-checkin/SKILL.md` Phase 1b — the consumer
- `core/scripts/insights-read.sh` — the queue interface
- `core/scripts/capture-insights.py` — the writer
- `world/reasoning-bank.jsonl` rb-389, rb-350 — sources of the
  reasoning-bank examples
- `world/guardrails.jsonl` guard-308, guard-359, guard-389 — sources of
  the guardrails examples

## Cross-reference graph

Live schema links between stores (left → right):

- Working memory → Knowledge tree: `encoding_queue[].target_article`, `knowledge_debt[].node_key`
- Pipeline → Experience: `pipeline.experience_ref`
- Experience → Pipeline: `experience.hypothesis_id`
- Pipeline → Knowledge tree: `pipeline.context_manifest.tree_nodes_read`
- Experience → Knowledge tree: `experience.tree_nodes_related`
- Knowledge tree → Experience: tree node `.md` front matter `experience_refs`
- Reasoning bank → Guardrail: `rb.preventive_guardrail`
- Reasoning bank → Experience: `rb.experience_ref`
- Reasoning bank → Pipeline: `rb.source_hypothesis`
- Guardrail → Experience: `guardrail.experience_ref`
- Guardrail → Pattern signature: `guardrail.related_patterns`
- Board post → Goal / hypothesis (informal): `tags: [goal:g-NNN-NN, because:rb-NNN]`

Pattern signatures, Journal, and Spark questions have no structured
outbound refs — they are terminal receivers (or, for journal, a
narrative log).

Every durable store now has at least one structured outbound link to
evidence (experience), pipeline, tree, or goal.

## Anti-patterns

- **"Operational value encoded to tree."** Paths, URLs, and resource IDs
  belong in `world/conventions/*.md` as locators, not in tree nodes.
  Rule: `.claude/rules/encode-stable-facts.md`.
- **"Ops-gotcha silently fixed."** A 14-hour stall dressed up as "just a
  port fix" leaves zero encoding behind, guaranteeing recurrence. If a
  fix required investigation that would help next time, it's an RB entry
  at minimum.
- **"Guardrail sourced to a journal entry."** Journal entries compress
  away the evidence. Use `guardrail.experience_ref` to link to the
  specific execution trace so the evidence chain survives.
- **"Tree node for a session-scoped fact."** Tree nodes are durable;
  working-memory slots evict at session end. Wrong destination wastes
  retrieval budget forever.
- **"One observation, one store."** Rare. Most operational gotchas are
  RB + guardrail. Most domain discoveries are tree + experience. Be
  willing to write two linked records.
- **"Re-encode rather than update."** When a tree node is stale, Edit it
  in place with updated `last_updated` + `last_update_trigger` — don't
  write a new node.

## Follow-ups (out of scope for this pass)

- **Retroactive population of `experience_ref`.** The field is live on new
  records (and auto-backfilled to null by `normalize_record` when existing
  records pass through any write path). High-utility RB entries and
  guardrails that reference a specific goal or session could have their
  `experience_ref` filled in from the source goal's archived experience —
  would require a one-off script that walks RB/guardrail records, resolves
  the source_goal/source to an experience ID, and calls `update-field`.
- **Add rendered cross-reference audit.** A simple script that walks all
  stores and reports dangling refs (`source_goal` pointing at a deleted
  goal, `preventive_guardrail` pointing at a retired guardrail,
  `experience_ref` pointing at a missing experience, etc.).

## Cross-references

- `core/config/conventions/reasoning-guardrails.md` — RB / guardrail schemas, Store Selection subsection
- `core/config/conventions/experience.md` — experience archive schema
- `core/config/conventions/journal.md` — journal index schema
- `core/config/conventions/pipeline.md` — pipeline schema, atomic resolve
- `core/config/conventions/pattern-signatures.md` — pattern signature schema
- `core/config/conventions/spark-questions.md` — spark schema
- `core/config/conventions/tree-retrieval.md` — tree retrieval + script API
- `core/config/conventions/working-memory.md` — WM slots + wm-*.sh scripts
- `core/config/conventions/resource-locators.md` — locator lane schema
- `core/config/conventions/board.md` — casual reasoning channel
- `core/config/knowledge-conventions.md` — tree node .md schema, compression, capability levels
- `core/config/memory-pipeline.yaml` — encoding-gate formulas, consolidation priorities
- `.claude/rules/encode-stable-facts.md` — three-probe threshold for locators
- `.claude/rules/knowledge-freshness.md` — when to reconcile tree nodes after world-changing actions
- `.claude/rules/no-auto-memory.md` — do not write to platform auto-memory
