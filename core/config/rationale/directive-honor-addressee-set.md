# Directive honor — who a directive is DIRECTED AT

WHY-narrative behind the `directed at this agent` predicate in
`.claude/skills/aspirations-select/SKILL.md` Phase 2.07 and
`core/scripts/goal-selector.py` `emit_directive_honor_banner`. The normative
three-step predicate lives at those two sites plus `guard-1310`
(`trigger_condition` + `action_hint`); this file carries only the reasoning.
Canonical implementation of the tag comparison itself:
`peer_surface.routing_tag_targets_agent` — import it, never re-derive it.

## Step 1-2: an explicit addressee is the WHOLE addressee set (g-115-8827)

`board-post.sh` stamps the AUTHOR's own name into every post's tags. So EVERY
directive an agent writes carries that agent's own bare name, and a bare-name
disjunct therefore matches the author of every directive they ever sent.

Measured 2026-09-03 on cc-09, `msg-20260903-065244-alpha-5364`:

    tags: ['requires_action_by:foxtrot', 'target:g-115-7744',
           'action_type:restart-daemon-and-sweep', 'alpha', 'sprint-planning']
    text: '@foxtrot - g-115-7744 needs one leg on YOUR box.'
    banner: "(directed at alpha, UNACKED)"

alpha wrote that directive FOR foxtrot and was then compelled to honor it
itself.

**The severity is INVERTED from an ordinary false positive.** This banner
FORBIDS a silent skip (guard-1310), so the louder it shouts the more likely a
reader complies rather than investigates — and complying means doing a peer's
assigned work while the real addressee's own banner still points at the same
goal. A false positive on an ordinary advisory costs one dismissible line; a
false positive here costs a mis-assigned unit of work on two boxes.

`requires_action_by:` is a deliberate routing decision, so the bare-name form
must not widen it. With NO such tag the bare form still routes — that is the
g-115-2870 path
(`test_fires_on_unacked_directive_targeting_scored_goal`) and it must not
regress. The prose fallback (step 3) is gated on `has_routing_tag`, which is
unchanged, so a directive with an addressee tag still suppresses the fallback
for everyone it does not name.

The `startswith("requires_action_by:")` filter selects the tag KIND, never the
agent: the agent comparison stays component-wise inside
`routing_tag_targets_agent`, so guard-2860 ("never relax an ownership predicate
to a pattern") still holds.

`guard-1310`'s `rule` field still carries the older, broader phrasing "tagged
with the agent name or requires_action_by:<agent>". `rule` is IMMUTABLE by
design — it is half the guardrail merge identity, so an in-place edit FORKS the
record across bodies (g-115-8396; rb-5511 measured 11 forked pairs in the live
store). The narrowing therefore lives in that guardrail's amendable
`trigger_condition` and `action_hint`, both of which say explicitly that they
govern over the `rule` head.

## Step 3: an explicit routing tag outranks a prose mention (g-115-2870)

An explicit routing tag (any `requires_action_by:*` tag, OR a bare tag that is
another known agent's name) takes PRECEDENCE over a loose prose mention. A
directive routed to agent X but naming agent Y in an exclusionary clause ("X
please claim; Y cannot do it") must NOT flag Y, and a self-authored directive
(author names self in prose) must not flag the author. The prose-mention
fallback fires ONLY when no routing tag is present at all.

## Both addressing forms count (g-115-4188)

A routing tag may be bare (`<AGENT>`, `requires_action_by:<AGENT>`) or
@env-QUALIFIED (`<AGENT>@<ENV-ID>`, `requires_action_by:<AGENT>@<ENV-ID>`).
Split on the FIRST `@` — every registry env-id contains a hyphen, so a
hyphen-joined form cannot be split back unambiguously. Then:

| tag shape | verdict |
|---|---|
| agent-part != AGENT_NAME | not for me |
| no `@` qualifier (bare) | for me |
| `@`qualifier == this deployment's `ENVIRONMENT_ID` | for me |
| `@`qualifier == some OTHER deployment's env-id | NOT for me, it is a peer |

Do NOT shortcut this by comparing only the text before the `@`: that admits a
PEER deployment's same-named agent as if it were the local one (guard-2860).

If `ENVIRONMENT_ID` is unresolvable, treat a qualified tag as FOR ME. That
fail-safe direction is chosen deliberately: this path only prints a MUST-SELECT
advisory, so a false positive is one dismissible line while a false negative is
the silent lane-skip guard-1310 exists to prevent. (Note this is the OPPOSITE
direction from the g-115-8827 narrowing above, and both are right: an
unresolvable env is a plumbing fault about a tag that WAS addressed to this
agent's name, whereas an author stamp is a tag that was never an address at
all.)

The qualified form is what `cross-deployment-channel.md` RECOMMENDS for an agent
name more than one deployment declares, and `insight-trigger-sweep.py` actively
tells posters to write it — so an exact-string test would make the very form the
convention recommends invisible here.

## Cross-references

- `guard-1310` — the directive-honor hard rule this predicate gates
- `guard-2860` — routing-tag matching is component-wise equality, never a
  prefix/glob/startswith on the agent name
- `core/scripts/tests/test_goal_selector_directive_honor_banner.py` — pins all
  three steps, including the author-tag case and the g-115-2870 no-regression
  case
- `core/config/conventions/cross-deployment-channel.md` — the
  `<agent>@<env-id>` author/tag format
