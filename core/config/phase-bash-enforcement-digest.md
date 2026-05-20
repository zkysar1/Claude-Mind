# Phase Bash-Enforcement Digest — LLM Residue

Companion to three wrappers that replace LLM-assembled writes in the
aspirations loop with single-call bash dispatches. Each wrapper handles
mechanical bookkeeping (file placement, JSONL append, dedup, schema
validation, staleness signals, meta refresh). This digest covers what
ONLY the LLM can do at each phase — the judgment calls that were
previously embedded in multi-step SKILL.md pseudocode and that silently
disappeared under context pressure (rb-428 "bash-consolidation drift";
guard-365 enforcement taxonomy).

Read once per session. Reference at each of the three phases below.

Extension of the pattern proven in `iteration-close-digest.md`. The
two digests are non-overlapping: `iteration-close-digest.md` covers
what remains after `iteration-close.sh` (verify, state-update,
learning-gate, productivity-check). THIS digest covers what remains
after the three phase-specific wrappers called from their own
SKILL.md phases.

---

## § PHASE 4.25 — Goal-Execution Experience Archival

**Wrapper**: `core/scripts/experience-archive-goal.sh`
(dispatches to `experience.py archive-goal`)

**Called from**: `.claude/skills/aspirations-execute/SKILL.md` Phase 4.25

**Wrapper handles**: canonical `.md` placement under
`agents/<agent>/experience/exp-{goal.id}-{skill-slug}.md`, duplicate rejection
(refuses overwrite), JSONL append, schema validation, meta refresh,
short-trace (<200B) and short-summary (<20 chars) drift warnings to
stderr.

**LLM residue** — compose BEFORE the wrapper call:

1. **Reasoning trace (.md content)** — write a reasoning-trace markdown
   file to any path before invoking the wrapper. Include: tool outputs
   consulted, decisions made and rejected, verification artifact, the
   outcome, surprises vs. priors. The wrapper moves this file to the
   canonical path — the LLM supplies the content.

2. **Verbatim anchors** — exact technical values that future retrieval
   or transfer should match literally: error codes, limits, timeouts,
   file paths with line numbers, commit hashes, latencies, API
   response bodies. Format: `[{key, content}, ...]`. These are the
   hooks the experiential index uses to fire this record on
   similar future goals.

3. **One-line summary** — the `--summary` arg. Keep it concrete
   (`"Patched regex in findings-gate.py to catch resolution-filter
   false positives on greedy matches"`, NOT `"Fixed bug"`). Wrapper
   emits drift warning when <20 chars.

4. **Optional enrichment via stdin JSON** — any subset of:
   `verbatim_anchors` (list), `tree_nodes_related` (list),
   `retrieval_audit` (manifest stats), `enabled_by` (filled by
   Phase 4.27 — leave `[]` here), `reasoning_chain`, `hypothesis_id`,
   `temporal_credit`.

**Drift indicators** (via stderr WARN):
- Trace file <200 bytes → LLM produced a stub, not a trace.
- Summary <20 chars → LLM produced a label, not a summary.
- Staleness probe (separate: `experience-staleness-check.sh`) fires
  when no experience record has been written in >12h despite
  productive goals — the retroactive safety net; see
  `iteration-close-digest.md` § LEARNING-GATE step 6.

---

## § PHASE 8.5 — Actionable Findings Gate

**Wrapper**: `core/scripts/findings-gate.sh`
(dispatches to `findings-gate.py`)

**Called from**: `.claude/skills/aspirations-state-update/SKILL.md` Step 8.5

**Wrapper handles**: 4-signal keyword scan (`root_cause`,
`bug_identified`, `proposed_fix`, `unimplemented_action`) with dual
resolution-filter (match content AND 50-char lookahead), dedup
against active + sibling-completed titles via
`agents/<agent>/session/aspirations-compact.json`, goal JSON assembly
(cognitive-primitive prefix per priority), dispatch via
`aspirations-add-goal.sh`, emits `findings_count=N created=M` for
downstream accounting.

**LLM residue** — supply BEFORE the wrapper call:

1. **Insight text** — the content just written to the tree node's Key
   Insights section in Step 8. The wrapper reads it from
   `--insight-file <path>`; the LLM's job is writing that text in
   Step 8 and pointing the wrapper at it here.

2. **Investigation-override binary** — for `Investigate:` goals ONLY,
   and ONLY when no keyword signal fires: answer the single question
   "does this finding purely inform, or does it need action?" Pass
   the answer as `--investigation-needs-action` (present = needs
   action). If uncertain, omit — default is "no signal → no goal
   created." Conservative bias: do NOT create goals from
   speculative investigation conclusions.

**Drift indicators**:
- `findings_count=0 created=0` is legitimate when the insight
  describes a resolved issue. The wrapper output is its own audit
  trail — no separate staleness probe needed; invocation count ≈
  deep-state-update count.
- If the wrapper is NOT invoked at all (SKILL.md drift), Step 8.5
  becomes invisible — the orchestrator tracks this via
  `state-update-audit.sh` and the per-iteration journal.

---

## § PHASE 8e — Decision Rules Extraction

**Wrapper**: `core/scripts/decision-rules-append.sh`
(dispatches to `decision-rules-append.py`)

**Called from**: `.claude/skills/aspirations-state-update/SKILL.md`
Step 8 (deep outcomes only) → sub-step e

**Wrapper handles**: rule formatting (`- IF {if} THEN {then} — source:
{goal-id}`), token-overlap dedup against existing rules in the same
node (>=70% overlap → skip), section creation or append (creates
`## Decision Rules` if absent), per-rule stdout trace, final
`decision_rules_count=<N> appended=<M> skipped=<K>` summary. Empty
stdin is legitimate — emits `reason=no_rule_passed` and stderr
staleness signal for aggregate drift detection.

**LLM residue** — compose BEFORE the wrapper call (or explicitly
signal "no rule"):

1. **IF clause** — the observable condition. Concrete, testable,
   measurable at runtime. Bad: "IF things look wrong". Good: "IF
   goal.title startsWith 'Investigate:' AND no keyword signal fires".

2. **THEN clause** — the specific action. No vague "be careful" or
   "consider X". Bad: "THEN pay attention to the result". Good:
   "THEN ask the investigation-override binary and pass
   --investigation-needs-action when yes".

3. **Judgment on whether a rule emerged at all** — not every goal
   produces a rule. Signal "no rule" by piping empty stdin to the
   wrapper rather than skipping the call entirely. The empty-stdin
   signal tells the staleness probe this phase ran; a missing call
   is indistinguishable from SKILL.md drift.

**Drift indicators**:
- Empty-stdin emits `reason=no_rule_passed` to stdout + stderr
  `STALENESS: no_rule_passed` — confirms the wrapper ran and the
  LLM responsibly signalled "no rule this iteration."
- **Primary drift mode**: wrapper never invoked at all (SKILL.md
  Step 8e skipped). The per-call signal above is silent in this
  case, so the backstop is a time-based probe on the last-call
  marker that the wrapper bumps on every invocation
  (`agents/<agent>/session/decision-rules-last-call`). Run from
  `iteration-close.sh do_productivity_check` alongside
  `experience-staleness-check.sh`: `decision-rules-staleness.sh`
  warns when the marker is absent (never called) or older than
  `DECISION_RULES_STALENESS_HOURS` (default 24). Warn-only — the
  per-call `no_rule_passed` signal already covers the legitimate
  "no rule emerged" case, so a force-gate sentinel would cause
  false-positive retro-composition.

---

## Cross-references

- `core/config/iteration-close-digest.md` — parallel digest for
  `iteration-close.sh` (Phase 5 verify, Phase 8 state-update, Phase
  12 learning-gate).
- `core/config/conventions/decision-rules.md` — full format spec for
  Phase 8e rules and `Decision Rules` section conventions.
- `core/config/conventions/experience.md` — experience archive
  schema and script APIs.
- `world/reasoning-bank.jsonl` → rb-428 — "bash-consolidation drift"
  origin, the pattern these wrappers address.
- `world/guardrails.jsonl` → guard-365 — enumeration taxonomy
  (bash-portable / LLM-only needing backstop / obsolete) that
  drove this extraction.
