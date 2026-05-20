# Capability Check Before User Routing (MANDATORY)

> **Enforcement**: The CREATE_BLOCKER protocol now runs `capability-gate.py` as
> an automated cross-check after the LLM-side checklist below. If the gate
> matches an agent-provisionable capability while `participants:[user]` was
> intended, it exits 1 and refuses to proceed without either revised
> participants or an explicit `--override-agent-match "<justification>"`.
> See `.claude/skills/aspirations-execute/SKILL.md` Step 2.6 and
> `core/scripts/capability-gate.py`. The checklist below is still required —
> the gate is a safety net, not a replacement.

Before assigning `participants: [user]` to ANY goal — in CREATE_BLOCKER,
create-aspiration, notification fallbacks, or any other code path:

## Required Checklist

1. **Skill registry**: Does a skill in `.claude/skills/` handle this action?
2. **Forged skills**: Does `world/forged-skills.yaml` have a skill with matching triggers?
3. **Companion scripts**: Does the relevant skill list scripts that self-service this?
4. **Provisionability**: Can the agent start the service, reconnect, or retry itself?
5. **Domain convention**: Does `world/conventions/capability-routing.md` list this as
   agent-provisionable? (Load via `world-cat.sh conventions/capability-routing.md`.)

## Decision Rule

- If ANY of checks 1-5 finds an agent-capable path: `participants: [agent]`
- If action needs BOTH agent AND human work: `participants: [agent, user]`
  (agent portion proceeds; human portion surfaced via pending-questions)
- ONLY if genuinely human-only (no API, no script, no bridge): `participants: [user]`

## What "Human-Only" Means (Framework-Level)

- Granting credentials or API keys the agent does not possess
- Opening a GUI application when no headless/CLI/API alternative exists
- Strategic product decisions requiring human values/judgment
- Physical hardware actions (reboot, cable, hardware token)

**Framework-file edits are NOT human-only.** Editing `.claude/skills/**`,
`.claude/rules/**`, `core/scripts/**`, `core/config/**`, `CLAUDE.md`, or
`.claude/settings.json` is an **agent-capable** action — the deny-list was
loosened (2026-05-14, g-115-732) so the loop self-evolves the framework;
git-tracking + loop-commit + the fail-closed `settings-structural-validator`
are the safety net. Applying a verified framework patch (e.g. a
`/verify-learning` check, a SKILL.md fix) MUST route `participants: [agent]`,
never `[user]`. The ONLY genuinely agent-forbidden framework paths are the
**constitutional anchor**: `.claude/settings.local.json` and
`core/scripts/settings-structural-validator.{py,sh}` — hard-denied by a
self-referential deny, changeable only via a user-authorized maintenance
path. Routing an *anchor* change to the user is correct; routing any OTHER
framework patch to the user is the capability-routing violation this rule
exists to prevent (canonical incident: g-115-792, a ~30s verify-learning
patch wrongly user-gated). See `CLAUDE.md` "two-file settings rule" +
`core/config/conventions/constitutional-rings.md`.

For domain-specific human-only and agent-provisionable lists:
see `world/conventions/capability-routing.md`.

## Anti-Pattern

Creating `participants: [user]` because an infrastructure probe failed ONCE,
without checking whether the agent can provision/restart that infrastructure itself.
Failure does not mean impossible. Check provisionability before routing.

## Notification Fallbacks

When creating goals as notification fallbacks (e.g., notification delivery failed,
need user awareness): use `participants: [agent, user]`, NOT `[user]`.
Informational goals must remain visible to agents — they may be able to
resolve the underlying issue.

## Sibling Rule: Probe Before Defer

`participants: [user]` is not the only way an agent routes work to the user.
Writing `defer_reason: "blocked on user-initiated X"` on a goal has the same
effect — the goal freezes, work doesn't happen, the user is implicitly on the
hook — but without going through `capability-gate.py`. See
`.claude/rules/probe-before-defer.md` for the defer-time chokepoint, enforced
by `capability-gate.py` invoked from `core/scripts/aspirations.py cmd_update_goal`
when `field == defer_reason` and value is non-null.
