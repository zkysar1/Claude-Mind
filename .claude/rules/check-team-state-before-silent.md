# Check Team-State Before Concluding Partner Silent

## Principle

Before any code path or narrative conclusion declares a partner agent
silent, absent, crashed, or unresponsive — read
`world/team-state.yaml` `agent_status.<partner>.last_active` first.
"I haven't heard from bravo this session" is not evidence of silence
when the team-state file already records bravo's last activity 30
minutes ago. Trust the signal you already have.

## The Threshold

Default: **6 hours**. If `agent_status.<partner>.last_active` is within
6h of now, the partner is NOT silent — they are working in another
session, between iterations, or finishing a long goal. Use a longer
threshold only when the partner's normal cadence is documented as
slower (e.g., a daily-cadence reviewer agent).

## Rules

1. **Probe before concluding**: Run the canonical probe before any
   sentence (in narration, in a board post, in a goal description,
   in pseudocode) that asserts the partner is silent / absent /
   crashed / unresponsive / inactive / stalled. The canonical probe is:
   ```bash
   bash core/scripts/team-state-read.sh --field agent_status.<partner>.last_active --json
   ```
2. **Compare against threshold**: If the returned timestamp is within
   6h of now, do NOT conclude silence. The signal is positive
   evidence of recent activity.
3. **Missing field is silence**: If the field is missing or null,
   AND no other liveness signal exists, then silence is a valid
   conclusion. The pre-silence check passes negatively.
4. **Probe applies to all consumers**: Backoff escalation, take-back
   triggers, "partner is dead, change strategy" branches, and casual
   narrative diagnostics all route through the same probe. There is
   no exemption for "I'm just thinking out loud" — the probe is one
   shell call.
5. **Single source of truth**: `last_active` is the authoritative
   liveness signal at the team-state layer. Do not cross-reference
   board post timestamps as a substitute — board posts can be absent
   when an agent is mid-execution. team-state is the snapshot.

## Anti-patterns

- "<partner> hasn't posted in a while, must be silent" — without reading
  `agent_status.<partner>.last_active`
- "<partner> is unresponsive, taking back the goal" — when the team-state
  probe would have shown the partner mid-execution at phase 4
- Backoff escalation that ramps "because the partner is silent"
  without checking the team-state field that would falsify the
  premise
- Treating session-start as the last evidence of partner activity —
  the team-state file outlives sessions

## Status

Audit on 2026-04-19 found NO committed "partner silent" branches in
any skill at the time of writing — the incident that produced this
rule occurred in narrative reasoning, not in pseudocode. This rule
is preventive: it gates BOTH future pseudocode authors AND in-session
LLM narration from concluding silence without the probe. The
guardrail (`guard-321`) fires whenever the agent's narrative or
execution path approaches the conclusion.

## Cross-references

- `world/guardrails.jsonl` → `guard-321` (the active guardrail
  enforcing this rule at execution time)
- `world/reasoning-bank.jsonl` → `rb-350` (the 2026-04-19 incident
  trace with rb-245 + rb-258 "trust the signal you already have"
  lineage)
- `core/config/conventions/coordination.md` → "Team State Protocol"
  section, `agent_status.<agent>.last_active` field documentation
- `core/scripts/team-state-read.sh` → the canonical probe script
