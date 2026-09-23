# Rationale: Directive-Honor Read Window (why ACK reads 24h and HONOR reads 96h)

Referenced from `.claude/skills/aspirations-select/SKILL.md` Phase 2.07 and its
all-blocked twin. Explains why the two directive reads use different windows, why the
wider one carries a filter, and why 96h rather than "the whole channel".

## Why the two reads differ at all

They answer different questions. The ACK read asks *"have I seen this before?"* — a
question about FIRST SIGHT, which is correctly bounded by recency and correctly deduped
by `--unread-only --mark-read`. The HONOR read asks *"is this still binding on me?"* —
a question about the directive's LIFETIME, which the writer declares itself.

g-115-2990 established the scope split (ack dedups, honor does not). g-115-10429 found
that the split stopped at scope and never reached the WINDOW: both reads were `--since
24h`, so the honor read inherited a recency bound that answers the ack read's question.

## Why the narrow honor window was wrong

A directive declares its own lifetime in an `expires:<ISO>` tag, and observed values run
to ~72h (`scope:until_completed` says the same thing in words). A reader whose window
(24h) is narrower than the writer's declared lifetime reports a clean empty honor set for
the majority of each directive's life. That is `reclaim-routed-work.md` rule 7 exactly —
the reader's predicate is narrower than the population the creating gate makes — and it
is invisible because the correct operation of both halves produces it.

Measured (zeta, cc-02, 2026-09-20): 24h returned 2 directives, 96h returned 12, seven of
the ten extra still active by their own `expires:`. Re-measured (echo, cc-03,
2026-09-21T17:09): 24h → 4, 96h → 14.

## Why widening ALONE would be a regression

This is the half the filing goal's option (a) named but did not quantify, and it is the
reason the filter is not optional. At the 2026-09-21 re-measurement, of the ten
directives visible only at 96h, **six had already expired** (`expires:2026-09-21T16:00:00`,
read at 17:09). A bare `--since 96h` re-admits all six into the honor set and into the
all-blocked generation scope — turning a missed-obligation bug into a
resurrected-stale-obligation bug. The window and the filter are one change.

## Why a MISSING `expires:` tag is ADMITTED, not dropped

Three of the same ten carried no `expires:` tag at all. The predicate must fail OPEN on
absence: a writer who declares no horizon has not declared a SHORT one. Dropping them
would be a second, opposite regression — and it would diverge from the canonical
predicate below.

## Why this predicate and not a new one

`goal-selector.py::parse_directive_admission` is THE shared admission predicate
(g-115-4639). Its expiry clause is already exactly this: a past `expires:` returns None,
a missing tag falls through and is admitted. Two consumers already share it —
`load_active_directives` (scoring) and `emit_directive_honor_banner` (the guard-1310
banner) — and its docstring says "scoring and the banner cannot diverge."

That claim is true of the PREDICATE and was false of the POPULATION. Both selector
consumers read `_coord_rows()`, the whole coordination channel with no time window; the
SKILL.md is a THIRD consumer that shared neither the predicate nor the population. This
is guard-2392 in the field — a docstring asserting shared semantics is the most
convincing possible cover for divergence — and guard-3758's core claim, that the consumer
set of a read contract includes `.claude/skills/*/SKILL.md` pseudocode the loop executes
as instructions.

**Consequence worth keeping in view:** the compaction-proof half of guard-1310's defense
was never blind. `emit_directive_honor_banner` reads the unwindowed channel, so a >24h
unexpired directive whose target is an executable candidate DOES raise the bash banner
today. Only the LLM-executed honor set was window-limited. That narrows the severity of
the original finding — it was never a total blind spot — without making the divergence
acceptable: the banner fires only when the target is in `scored`, and the LLM path is
what builds `directive_targeted_goals`.

## Why 96h and not the whole channel

The selector can afford `_coord_rows()` because it is Python reading files directly. The
SKILL.md path is a board-read round trip whose result the model then reads: at 720h the
channel returns ~100 directives, at 96h it returned 40,701 B / 14 records. 96h is the
smallest bound that comfortably exceeds the longest lifetime anyone declares.

**That makes 96h an invariant with a tripwire, not a magic number:** it must exceed the
longest `expires:` horizon in use (observed max 72h). If a directive ever declares a
longer one, this window must grow with it, or the defect returns in the same shape. A
future reader changing directive lifetimes owns this constant.

## Cross-references
- guard-1310 — the DIRECTIVE-HONOR hard rule this read feeds
- guard-2392 — a docstring claiming shared semantics is a claim, not a mechanism
- guard-3758 — a read contract's consumer set includes SKILL.md pseudocode
- guard-6515 — a directive naming you with no `target:` tag has no honor action
- `.claude/rules/reclaim-routed-work.md` rule 7 — reader predicate narrower than the
  creating gate
- `core/config/rationale/directive-honor-addressee-set.md` — sibling: WHO a directive
  binds (this file is WHEN it still binds)
- `goal-selector.py::parse_directive_admission` — the canonical predicate
- g-115-2990 (the scope split), g-115-10429 (this window fix)
