# Rationale: aspirations-execute

Referenced from `.claude/skills/aspirations-execute/SKILL.md`. WHY reasoning for
five structural choices across Phases 4.04, 4.05, 4.5, and 4.7.

## Why decision-rule counters must be encoded (Phase 4.04)

Tree nodes carry `## Decision Rules` lines (`- IF X THEN Y — source: ...`)
that the retrieval lane surfaces alongside the node body. When execution
actually relied on one of those rules, that fact must be encoded — otherwise
`reflect-maintain` Step 1d cannot distinguish dead rules from fresh ones, and
the active-forgetting pass leaves stale rules in place. Counter format:
`— applied: N (YYYY-MM-DD)` suffix appended to the rule line on first use,
incremented on subsequent uses. The increment is idempotent within a call
but NOT across calls (every cited use bumps the counter once).

## Why drift-check fires for long executions (Phase 4.05 — Mid-Execution Drift)

Phase 4's retrieval snapshot is taken before goal invocation. For goals that
take 30+ minutes wall-clock OR produce result text exceeding 4000 chars, the
world may have changed during execution — partner agents may have completed
related goals, new board posts may have arrived, the agent's own writes may
have invalidated retrieved nodes. Without a drift check, Phase 4.1's
guardrail consultation and Phase 4.2's domain steps run on stale context.

Per `.claude/rules/retrieve-before-deciding.md`: every consequential decision
should retrieve in the same turn. A long execution effectively spans multiple
decision points; the snapshot ages out.

## Why long results are chunked instead of bundled (Phase 4.05 Chunked-Encoding Producer)

When this branch fires (long execution), the result.text often contains
multiple distinct learnings — different files modified, different probes
run, different conclusions reached. Bundling all of those into ONE Phase 8
encoding payload collapses them into a single Key Insight paragraph; each
finding scores against the gate as a single average rather than on its
own merit.

The shared chunk schema (Section F of
`core/config/encoding-protocol-digest.md`) lets Phase 8 process N distinct
encoding decisions instead of one bundle. Producer here, consumer in
Phase 8.

## Why probe-outcome drift uses HIGH priority and dual-write (Phase 4.5 Probe-Outcome Surprise)

Probe drift is the specific failure mode behind multi-hour false-blocker
incidents (rb-334 14-hour stall, rb-389 silent ssh failure, rb-246
synthetic probe). Two surfaces ensure either Phase 8 (sensory_buffer →
encoding gate → tree update via high_surprise priority) OR
consolidation's Step 2.25 (knowledge_debt sweep) catches it; one of the
two will fire on any session-end path.

See `core/config/conventions/encoding-triggers.md` E7 row for the full trigger catalog entry.

## Why Phase 4.7 (Full-Suite Recommender) is advisory, not blocking

Origin: g-115-744 / g-115-746 incident — a deep code closure narrated
"All tests pass" based on the targeted new test only, missing a
`testSymmetry` regression that the full Java suite would have caught.
g-115-858 is the Idea that surfaced the gap; this phase is its Apply.

Posture: ADVISORY, fail-open, always exits 0. The gate's value is the
visible stdout banner; the LLM is expected to act on it (run the
recommended commands) BEFORE narrating "all tests pass" in Phase 5.
Mirrors the pre-apply consult gate (Phase 4) — visibility beats hard-block,
because (a) suite runs are 30s–5min wall-clock and should be a deliberate
LLM decision, and (b) some deep closures are documentation-only where the
suite adds no signal.

## Cross-references

- `reflect-maintain` Step 1d — active-forgetting pass that reads decision-rule counters (Phase 4.04)
- `.claude/rules/retrieve-before-deciding.md` — consequential-decision retrieval rule (Phase 4.05)
- `core/config/encoding-protocol-digest.md` Section F — chunk schema produced here, consumed by Phase 8 (Phase 4.05)
- rb-334, rb-389, rb-246 — probe-drift false-blocker incidents (Phase 4.5)
- `core/config/conventions/encoding-triggers.md` E7 — probe-outcome-divergence trigger catalog entry (Phase 4.5)
- g-115-744, g-115-746, g-115-858 — testSymmetry regression incident and Idea origin (Phase 4.7)
- `.claude/rules/run-full-suite-after-deep-code.md` — full-suite command reference (Phase 4.7)
- `.claude/skills/aspirations-execute/SKILL.md` — consumer of this rationale
