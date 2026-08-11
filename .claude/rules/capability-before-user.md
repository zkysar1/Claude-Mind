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

Three surfaces route work to the user through a WRITTEN RECORD, and each has
a gate: `participants:[user]` (capability-gate at CREATE_BLOCKER),
`defer_reason` (capability-gate at `cmd_update_goal`), and outbound email
(`/notify-user` Step 1.5). **Writing "here, run these commands" in a chat
reply routes identical work to the identical human and passes through NONE of
them**, because nothing is ever written and no tool is called. Chat is the
highest-bandwidth agent-to-principal channel, so the one lane with no
chokepoint carries the most traffic. It is honor-system by construction — see
`probe-before-defer.md` § Enforcement for why no gate is possible and what
post-hoc detection remains.

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

Absolutes that the governing document does not actually state are the second
half of the same failure. `CLAUDE.md` does not forbid all constitutional-anchor
changes — it requires "a user-authorized maintenance path, never an autonomous
edit". When the principal is present and offering exactly that path, the path
is open, and treating the restriction as absolute converts a two-minute action
into a multi-day block. Before declining on a governing-doc restriction, re-read
what it actually says and check whether its named exception is available right
now.

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

Canonical incident (2026-08-03, production): a HIGH finding said a container
lacked its constitutional anchor. The agent could have written it — it had
ssh-ed into that container three times in the same conversation — and declined
on the reasoning that the anchor's value depends on the agent not having
written it. Sound in isolation, applied as an absolute the governing doc does
not state, while the principal was present offering the authorized path. The
agent then resolved "I am at that computer now" against its own last topic and
handed over container-specific paths; the principal was at a different machine,
different deployment, non-root. Permission denied → `sudo -i` → root shell →
"did I just break production". He had not. Cost: about an hour of his evening,
a real scare, and a two-day delay on a two-minute fix. All four guards above
would have broken that chain, and the one-sentence voiced decline would have
prevented it entirely.

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
