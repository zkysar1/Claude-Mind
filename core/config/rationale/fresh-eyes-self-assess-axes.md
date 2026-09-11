# Rationale: fresh-eyes-review Phase 5.5 Self-Assess Axes

Referenced from `.claude/skills/fresh-eyes-review/SKILL.md` Phase 5.5. Why the
`self-assess-and-decide.sh` verdict has THREE independently-sufficient axes, why
`P` must be computed before the beliefs are read, and why the helper's own printed
`net` cannot be quoted as a margin.

## Why sweeps must neutralize all three axes (guard-3295)

Measured 2026-08-10, zeta N=54, `hostname` cc-02, `uname -r` Linux 6.8.0-136-generic.

Several agents adopted a practice of sweeping `confirming_signal_fraction` and
reporting "ROBUST — returns the same verdict at EVERY value" as evidence the
belief-classification judgment does not move the outcome. Measured on this helper,
that report is true and carries no information: `drift >= 0.40` and
`net-divergent >= 2.0` are each SUFFICIENT ALONE, so sweeping
`confirming_signal_fraction` (0.00–1.00), `portfolio_drift_score` (0.10–0.70), AND
`self_evolution_signals_count` (0–8) each returned `act_later` at every point —
three constant sweeps, because in each one the OTHER axis was independently
carrying the verdict. Only neutralizing both at once returned `no_change`.

A sweep whose output never changes is measuring the HELD axes, not the swept one,
and the constancy is what makes it look like a strong result — which is why three
prior fires (N=44/45/47) each booked it as robustness and none re-examined it. The
valid form: hold every other axis at a value that CANNOT produce the verdict, then
sweep, and report the neutralized flip point rather than the constancy. On this
helper those boundaries are `drift` flips at **0.40** with `confirming = 1.00`, and
`confirming` flips to `no_change` where **`N − confirming_count < 2`** with
`drift = 0.05` — an inequality in `N`, NOT a fixed fraction. (That subtraction was
a float product `N·(1−confirming)` until g-115-9566; see "The arithmetic went
integer" below for what moved and what did not.)

⚠ This line read "`confirming` flips to `no_change` at **0.75**" until 2026-09-01
(echo, N=120, `hostname` cc-03, `uname -r` 6.8.0-137-generic). 0.75 is only that
measurement's own `N = 8` sample point, and it contradicted THIS FILE's own
"Why the `confirming` boundary is an inequality, not a fraction" section below —
which derives `> 0.50` at `N = 4` and `> 5/7 = 0.7143` at `N = 7`, and says in so
many words "neither 0.75 nor 5/7". `guard-3311` already carried the general form
(`1 − 2.0/self_evolution_signals_count`); this SUMMARY restatement had drifted from
the derivation it summarizes, which is why the contradiction survived — a reader
who stops at the summary never reaches the section that refutes it. Class:
`guard-5724` (a sample-specific bound written as a universal one, ratcheting once
per reader) crossed with `guard-5556` (an early summary going stale relative to a
later section of the SAME document). Flagged by fresh-eyes N=118 item 6, recorded
still-unaddressed at N=119 item 4, corrected here.

## Why the third axis went unrecorded for 69 fires

`signal_actionable_score >= 0.40` fires `act_later` ON ITS OWN. It is a plain
`>=` cutoff, NOT an interval. Measured 2026-08-12 (alpha, N=69, `hostname` cc-04, `uname -r`
6.8.0-137-generic) with `drift` neutralized to 0.05 and `confirming` held at 1.00:
0.20 / 0.30 / **0.35 → `no_change`**, **0.40** / 0.49 / 0.50 / 0.55 → `act_later`,
the rationale reading `weak-but-present signal: actionable=<v>` with no other axis
named. It went unrecorded for 69 fires because this field is scored by hand from
"how clearly do the signals map to a specific Self edit", and a review that finds
diffuse signal scores it ~0.20 — so it had never crossed.

**This paragraph read "boundary in (0.35, 0.40]" until echo N=118 (2026-09-01,
cc-03), and that notation is contradicted by the very measurement in the line
above it** — 0.20 and 0.30 are recorded here as `no_change` too, so 0.35 was
never a lower BOUND, only the lowest point alpha happened to sample. An interval
invites the reader to test one point below it and raise the bound; that is
exactly what happened. echo N=117 (2026-08-31) tested 0.36, got `no_change`,
published "(0.36, 0.40] — NARROWER than the skill's stated (0.35, 0.40]", and
left a standing instruction to propagate that into `fresh-eyes-review/SKILL.md`.
N=118 swept 0.34/0.35/0.36/0.37/0.38/0.39 (all `no_change`) and 0.40/0.41
(`act_later`) with drift=0.05 and confirming=1.00 held non-firing, which settles
it: the cutoff is `>= 0.40` and there is no lower bound to find.

The generalizable half — a threshold written as a half-open interval creates a
phantom lower bound that ratchets upward once per reader, because each reader's
own no_change sample looks like evidence the bound moved. Both drifts were made
by careful passes that measured correctly and reported the interval their sample
supported. State a cutoff as a cutoff. (Sibling: `guard-3297`, thresholds
without a designed variance estimate.)

**It also MASKS the drift boundary.** At `actionable = 0.55` the documented
0.39/0.40 drift sweep returns `act_later` at BOTH points, so a reader running that
control on a high-actionable pass sees a constant sweep and books it as robustness
— the exact vacuity guard-3295 exists to prevent, reached through an axis
guard-3295 does not name. So when neutralizing for ANY sweep, neutralize
`signal_actionable_score` to <= 0.35 as well. Three axes, not two.

## Why `P >= 2` decides before any belief is read (guard-3390)

Measured 2026-08-11, zeta N=57, `hostname` cc-02, `uname -r` Linux 6.8.0-136-generic.

The `confirming_signal_fraction` spec declares `pq_signals + board_signals`
**never confirming**, so the confirming count can never exceed the belief count
`B`. With `N = P + B`, that makes

> `net_divergent = N − confirming_count >= N − B = P`, for **every** possible
> classification of **every** belief.

The helper fires `act_later` at `net >= 2`. So `P >= 2` forces `act_later`
before a single belief is read, and `P <= 1` is the only regime where Phase 2.6b
decides anything (and then only while `drift < 0.40`, which is independently
sufficient). Verified across the full `(P, B)` grid at max-honest
`confirming = B/N` with drift neutralized: `P=0,1` → `no_change` at every `B`;
`P=2,3` → `act_later`. This **supersedes** the single-instance form N=56
recorded ("`no_change` was unreachable this fire").

⚠ **This section read `P >= 3` forces, with `P == 2` as "one boundary cell",
until 2026-09-10 (g-115-9566).** The cell existed only because the float product
put `net` at EXACTLY 2.0 there, so typed precision decided it — the knife-edge
the section below documents. With an integer bar, `confirming_count <= B` gives
`net = N − confirming_count >= P` exactly, and `P == 2` fires like any other
value. The derivation was always stated in COUNT space; only the implementation
was not, which is why it and the float path disagreed at this one cell.

This is a **recipe** constraint, not a helper bug — the helper accepts
`confirming = 1.00` happily; the never-confirming rule is what makes `net >= P`
unavoidable, so that is where a fix would go. Keep reading the beliefs to full
length when you will ACT on their content (guard-1421/2043 still bind) — just do
not report the classification as having determined a verdict it could not reach.
With a 5-agent fleet `B` is capped near 4 (one belief per partner,
supersede-not-grow) while `P >= 2` is the ordinary case, since board counts
own-authored ritual output and pq counts any own-scope decision in 30d. The step
is usually inert, not usually decisive.

## The arithmetic went integer (g-115-9566, 2026-09-10)

Measured alpha, `hostname` cc-10, `uname -r` Linux 6.8.0-139-generic.

The two sections below this one describe a helper that no longer exists, and
they are kept because their measurements are what motivated the change. **Read
them as history, not as current behaviour** — a citation of either that does not
carry this paragraph is now wrong.

`confirming_signal_fraction` is a LOSSY ENCODING OF A COUNT. Every caller
computes it as `confirming_beliefs / self_evolution_signals_count`, i.e. divides
two integers, and the helper then multiplied it back out in floating point and
compared against `2.0`. At `net == 2` that round trip put the verdict at the
mercy of typed decimal precision. The helper now accepts
`confirming_signal_count` directly and, when only the fraction is given,
recovers the numerator by **half-up rounding** (`floor(N·f + 0.5)`, clamped to
`[0, N]`), so the gate is `N − confirming_count >= 2` between two integers.

Three consequences, all measured against the live helper:

1. **The knife-edge is gone.** The four spellings of 4/6 in the next section
   (`0.6666` / `0.6667` / `0.666667` / `0.6666666666666666`) now ALL return
   `act_later`. They also now agree with the `P >= 2` derivation above, which
   the float path had been quietly contradicting: `P = 2` forces `act_later` for
   every classification of every belief, yet two of those four spellings
   returned `no_change`.
2. **The flip in FRACTION space moved from `1 − 2.0/N` to `1 − 1.5/N`** — 0.625
   at `N=4` (measured: 0.62 → `act_later`, 0.625 → `no_change`), 0.50 at `N=3`
   (0.49 → `act_later`, 0.50 → `no_change`). `guard-3311` carries the old
   formula and its `rule` is immutable; the superseding record is `guard-6428`.
   The move is confined to fractions that name no whole number of signals: for
   every `k/n` a real caller can emit, half-up returns `k` exactly, pinned over
   `n = 1..12, k = 0..n` by `test_exact_integer_ratio_recovers_its_own_numerator`.
3. **Half-up is the documented tie-break, and it is chosen over `ceil` on
   robustness grounds.** `ceil` would have preserved the old boundary exactly,
   but `ceil` is sensitive to the ±1e-16 error in the very ratios callers emit
   (`49 · (7/49)` evaluates to `7.000000000000001`, which `ceil`s to 8 and
   invents a confirming signal). Half-up tolerates that error by half a signal
   in both directions, which is the whole point of the change.

Emit `confirming_signal_count` and none of this applies to you.

## Why the printed `net` cannot be quoted as a margin (HISTORICAL — pre-g-115-9566)

**`P == 2` is a knife-edge: `net` is then EXACTLY 2.0, so the verdict is decided
by the decimal precision you type.** Measured at `P=2, B=4, N=6`, the same true
quotient 4/6: `0.6666` → `act_later`; `0.6667` → `no_change`; `0.666667` →
`no_change`; `0.6666666666666666` → `act_later`. **The rationale string prints
`net=2.0 @67%conf` in all four cases** — byte-identical across a verdict flip, so
a reader auditing the decision from its own stated rationale cannot see which way
it went, or that precision decided it. The spec gives the quotient and never the
precision; at `P == 2` that unstated choice *is* the verdict.

**But the rounding is a property of the DISPLAY, at ANY `N`.** Measured 2026-08-12
(echo, N=54, `hostname` cc-03, `uname -r` Linux 6.8.0-137-generic) at
`P=0, B=4, N=4`, drift neutralized to 0.05: `confirming=0.50` → `act_later`;
`confirming=0.51` → **`no_change` with the rationale reading `net=2.0@51%conf`**.
True net is 4·0.49 = 1.96, displayed as 2.0. So a reader auditing a decision from
its own stated rationale can see the firing threshold printed on a run that did
not fire. `P == 2` is only where it also happens to decide the verdict.

REPLICATED on a second box 2026-08-12 (bravo, N=43, `hostname` cc-05, `uname -r`
Linux 6.8.0-137-generic): same `P=0, B=4, N=4`, same 0.50 → `act_later` / 0.51 →
`no_change` pair, byte-identical `net=2.0@51%conf`. Same parameters, not a second
parameter point — it rules out a one-box parse artifact and says nothing about
other `N`.

**The band is SYMMETRIC — measured, same run:** `net=2.0` is printed across
true_net ∈ **[1.95, 2.05]**, i.e. for `confirming` 0.4875→`act_later` through
0.5125→`no_change`. So the printed `net` spans BOTH verdicts and cannot
discriminate at all. The confirming % is rounded too (0.4875 prints `49%`, 0.505
prints `50%`), so `confirming=0.50` (fires) and `confirming=0.505` (does not) emit
**byte-identical numbers** — `net=2.0`, `50%conf` — for opposite decisions. The
`decision` field still tells you WHAT was decided; what the rationale cannot tell
you is that the margin was ±0.05 rather than exact.

## Why the `confirming` boundary is an inequality, not a fraction

The inequality survives g-115-9566; only its float form does not. Current form:
`N − confirming_count >= 2`, i.e. `1 − 1.5/N` in fraction space. The measurement
below is the pre-change float form, retained because it is what established that
the boundary depends on `N` at all.

Same run as the rounding measurement: at `N = 4` the flip is at
`confirming > 0.50` (4·0.50 = 2.0 fires, 4·0.49 = 1.96 does not), which is
`N·(1−confirming) < 2.0` and neither 0.75 nor 5/7. At `N = 7`, `confirming` flips
at **`> 5/7 = 0.7143`** (0.714 → `act_later`, 0.72 → `no_change`). The boundary
depends on `N`, so quote it as that inequality rather than as a fixed fraction.
`drift` re-verified at 0.40 across both runs (0.39 → `no_change`, 0.40 →
`act_later`) with `confirming` held at 1.00.

## Cross-references

- guard-3295 — neutralize the other axes before sweeping one
- guard-3390 — compute `P` before reading the beliefs
- guard-1984 — a guardrail cannot outvote the instrument it guards (why the
  operative half of each finding stays inline in Phase 5.5 rather than living
  only in its guardrail)
- guard-1421 / guard-2043 — read beliefs to full length when acting on content
- guard-3311 — the pre-g-115-9566 flip formula `1 − 2.0/N`; superseded by
  guard-6428 (`1 − 1.5/N`, or just pass `confirming_signal_count`)
- `.claude/skills/fresh-eyes-review/SKILL.md` Phase 5.5 — consumer
- `.claude/skills/fresh-eyes-program/SKILL.md` Phase 5 — second consumer
- `core/scripts/self-assess-and-decide.sh` — the helper these axes describe
- `core/scripts/tests/test_self_assess_and_decide.py` — the boundary pins
