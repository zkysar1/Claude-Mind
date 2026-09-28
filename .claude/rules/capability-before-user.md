---
description: "Before routing work to the user, check skills, scripts, forged skills, provisionability; framework edits are agent work, never user-gated."
---

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

## The Fourth Surface: handing the user a command in chat

Writing "here, run these commands" in chat routes work to the user through
NONE of the three gated surfaces (`participants`, `defer_reason`, outbound
email). Honor-system by construction — no gate is possible; see
`probe-before-defer.md` § Enforcement.

### State a principled decline AS A CHOICE

This rule is otherwise written against laziness and unexamined capability
gaps. The hard case is neither: a *reasoned* decline, with a real argument,
where the agent genuinely can act and elects not to. A principled refusal
feels more defensible than "I cannot", so it draws less scrutiny and survives
longer — and if the agent never says *"I can do this, I am choosing not to"*,
the refusal is **indistinguishable from incapability** and the user never
thinks to overrule it.

So: when declining to do something you are capable of, say plainly that you
are capable and that this is a choice, and name what would change it. One
sentence. It costs nothing and it hands the reversal back to the user, who can
take it in one word.

### Where a governing doc names an authorized path and the user is present, that path is OPEN

Before declining on a governing-doc restriction, re-read what it actually
says and check whether its named exception is available right now. Treating
the restriction as absolute when the principal is present offering the
authorized path converts a two-minute action into a multi-day block.
Incident: `core/config/rationale/capability-before-user.md`.

### Hand-command hygiene (when you do hand over a command block)

For any command aimed at a machine you cannot see, the block MUST:

1. **Assert the expected host and path, and abort otherwise.** Never resolve a
   deictic reference ("I'm at that computer now") against your own last topic —
   the user's "that computer" is a claim about THEIR location, and you have no
   way to check it. Make the command check.
2. **Assert the expected user, and refuse root** where the service runs as
   another user. A `sudo -i` reached for because a path was wrong is how a
   wrong-host command becomes root-owned files in the right place.
3. **Refuse to clobber an existing file.** Write only if absent, or write beside
   and diff.
4. **Verify by an independent read-back, not by the write echo.** A successful
   write echo says the command ran, not that the intended content is there.

Canonical incident (2026-08-03, production): `core/config/rationale/capability-before-user.md`.

## What "Human-Only" Means (Framework-Level)

- Granting credentials or API keys the agent does not possess
- Opening a GUI application when no headless/CLI/API alternative exists
- Strategic product decisions requiring human values/judgment
- Physical hardware actions (reboot, cable, hardware token)

**Framework-file edits are NOT human-only.** `.claude/skills/**`,
`.claude/rules/**`, `core/scripts/**`, `core/config/**`, `CLAUDE.md`,
`.claude/settings.json` — agent-capable (git is the safety net). Verified
framework patches MUST route `participants: [agent]`, never `[user]`
(g-115-792). The ONLY agent-forbidden framework paths are the
**constitutional anchor** (`.claude/settings.local.json` and
`settings-structural-validator.{py,sh}`). See `CLAUDE.md` "two-file
settings rule" + `core/config/conventions/constitutional-rings.md`.

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

`defer_reason: "blocked on user-initiated X"` freezes a goal exactly as
`participants: [user]` does; `.claude/rules/probe-before-defer.md` is its
chokepoint (`capability-gate.py` on the `defer_reason` field write).
A FIFTH surface routes work to NOBODY: a defer waiting on the goal's OWN
unlanded artifact passes every human-routing gate (g-373-12 froze behind its
own open PR). `gates/defer_self_artifact.py` refuses it at that same write.
