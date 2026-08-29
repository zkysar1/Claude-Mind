# Rationale: fresh-eyes-review SIGNALS_JSON Field Derivations

Referenced from `.claude/skills/fresh-eyes-review/SKILL.md` Phase 5.5. The measured
evidence behind two `SIGNALS_JSON` field specs whose operative rules are stated
inline at the call site.

## Why `completion_health` excludes single-goal `asp-xw-` imports

Measured 2026-08-06 (bravo, `hostname` cc-05): 27 active world aspirations, 9 of
them single-goal `asp-xw-` cross-world imports -> raw mean **0.5025** vs filtered
**0.7537**, a 0.25 swing from records that represent no completable work. Each
such import is one goal wearing an aspiration's clothes: it contributes a hard 0.0
carrying the same weight as an 897-goal aspiration, so it does not measure
portfolio health, it dilutes it.

WHY THE EXCLUSION IS STATED INLINE AND NOT LEFT TO THE GUARDRAIL (guard-1984):
guard-2829 names this exact field verbatim and is retrievable, and two consecutive
bravo passes (N=20, N=21) still passed the RAW figure, each catching it only
afterwards by reading the prior pass's handoff. A guardrail cannot outvote the
instrument it guards — it fires when a reader happens to retrieve it, while the
inline spec is read by everyone who runs the ritual. Same shape guard-1877 /
Phase 2.3b already demonstrated one phase up.

## Why an affirming partner-belief counts as CONFIRMING

`confirming_signal_fraction` exists so the `act_later` gate fires on NET-DIVERGENT
signal rather than gross volume — it PULLS the g-115-1680 lever
`self-assess-and-decide.sh` already had but that Phase 5.5 was leaving at the 0.0
default.

WHY (g-115-1742, alpha 2026-07-03): an affirming partner-belief — a correct
external read of a stable, in-lane Self — is STABILITY evidence, not
change-pressure; counting it toward `act_later` was a false-positive treadmill.
Measured: evo=5 (3 fresh-affirming + 2 stale), self 2d fresh, portfolio in-lane →
wrongly returned `act_later`, re-filing a follow-up Idea every review for the sole
reason that partners correctly read this agent.

A STALE belief (staleness_days > 14) is likewise confirming: an aged partner read
is not current drift-pressure.

## Cross-references

- guard-2829, guard-2804 — the `asp-xw-` exclusion
- guard-1877 — the Phase 2.3b instance of the same inline-vs-guardrail shape
- guard-1984 — a guardrail cannot outvote the instrument it guards
- g-115-1680 — the lever; g-115-1742 — the treadmill incident
- `.claude/skills/fresh-eyes-review/SKILL.md` Phase 5.5 — consumer
- `core/config/rationale/fresh-eyes-self-assess-axes.md` — sibling extraction
