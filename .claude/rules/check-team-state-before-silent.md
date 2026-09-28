---
description: "Never call a partner silent without liveness-check.sh: fresh last_active proves life; stale is ambiguous, corroborate independently."
---

# Check Team-State Before Concluding Partner Silent

## Principle

Before any code path or narrative conclusion declares a partner agent
silent, absent, crashed, or unresponsive — read
`world/team-state.yaml` `agent_status.<partner>.last_active` first.
"I haven't heard from bravo this session" is not evidence of silence
when the team-state file already records bravo's last activity 30
minutes ago. Trust the signal you already have.

Mechanism, verdict semantics, the two-branch corroboration protocol with its
code shape, and the incident record live in
`core/config/conventions/partner-liveness.md` (`load-conventions.sh
partner-liveness`). Read it before wiring any code path that ACTS on partner
staleness (backoff, take-back, reallocation). This rule keeps the imperatives.

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
   crashed / unresponsive / inactive / stalled.

   Preferred (multi-signal, cross-box-safe — g-115-2149):
   ```bash
   bash core/scripts/liveness-check.sh --agent <partner> --json
   ```
   Returns `alive` | `dormant` | `unknown` | `retired`. **Conclude silence
   ONLY on `dormant`.** `unknown` means the fresh signal was unreadable or
   contradicted (mirror-sourced value, or a fresh shard OBJECT whose
   authoritative `last_active` VALUE is stale — a Body can write the shard
   while the Mind is dead) — never conclude dormant on `unknown`. `retired`
   means DECOMMISSIONED, not quiet: do not route work to it, wait on it, or
   file a "partner silent" finding about it. Read
   `authoritative_last_active_provenance` when a verdict hangs on the
   authoritative read.

   Underlying raw signal (use only when you need the pushed snapshot value,
   not a liveness verdict):
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
5. **THE SIGNAL IS ASYMMETRIC.** FRESH `last_active` IS evidence of life —
   trust it (exception: guard-3604 cross-agent clear). STALE is NOT evidence
   of death — never conclude silence from stale alone.
6. **Stale obliges the 2-branch tree:** Branch 1 (every peer stale = suspect
   your pull path); Branch 2 (some stale = corroborate from the store of
   record, never the local cache — guard-980). Full protocol:
   `core/config/conventions/partner-liveness.md`.

## Anti-patterns

- "<partner> hasn't posted in a while, must be silent" — without reading
  `agent_status.<partner>.last_active`
- "<partner> is unresponsive, taking back the goal" — when the team-state
  probe would have shown the partner mid-execution at phase 4
- Backoff escalation that ramps "because the partner is silent" without
  checking the team-state field that would falsify the premise
- Treating session-start as the last evidence of partner activity — the
  team-state file outlives sessions
- Concluding a partner is dead from a stale `last_active` alone — a broken
  heartbeat writer and an idle agent are indistinguishable at that field
  (rule 6)
- Probing the partner's execution-diary on the LOCAL filesystem — under
  own-cloud the local tree is a read-through cache (`Path.exists()` on the
  own-cloud mirror proves nothing; guard-980, and the
  `owncloud-local-cache-staleness` tree node)

## Cross-references

- `core/config/conventions/partner-liveness.md` — verdict semantics and
  provenance, the guard-3604 narrative, the two-branch protocol with code,
  the 2026-07-14 incident (g-115-2181), code consumers (goal-selector
  `_liveness_confirms_dormant`, g-115-2315)
- `guard-321` — the active guardrail enforcing this rule at execution time;
  `guard-3604` — rule 5's known false-positive generator
- `rb-350` — the 2026-04-19 incident trace (rb-245 + rb-258 "trust the
  signal you already have" lineage); `rb-3150` — the pull-wedge mechanism
  behind rule 6 Branch 1
- `core/scripts/liveness-check.sh` + `liveness_check.py`; `core/scripts/team-state-read.sh`
- `core/config/conventions/coordination.md` → "Team State Protocol",
  `agent_status.<agent>.last_active`
