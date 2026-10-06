# Audit Baselines (`meta/audit-baselines.yaml`)

An **advisory ratchet** for drift metrics that can be measured but shouldn't
hard-gate everyday work. Each baseline records a single drift count that is
allowed to shrink but never grow.

## When to use

Create a new baseline entry ONLY when all three hold:

1. The metric is a **non-negative integer count of drift items** (dangling
   references, schema violations, stale docs — things that monotonically
   improve as they're fixed).
2. There is a **canonical audit script** that computes the current count
   deterministically in bounded time.
3. Hard-gating would be premature (historical drift exists; fixing it is
   someone's future work, not a release-blocker).

If the metric is a ratio, latency, or anything continuous — use a different
mechanism (gates, thresholds, alerts). Not this file.

## Seeding

**Measure the seed with the EXACT predicate that ships — never an exploratory
one.** While developing a ratchet you will run several throwaway greps to size
the problem. The number one of those produced is not the seed. Run the shipped
check, read the number IT reports, and seed that.

A seed measured by a different predicate is an answer to a question nobody will
ask again. The lucky failure is what happened in g-115-3560: seeded at 12 from
an exploratory regex, shipped a slightly broader one, and the check landed
**FAIL at 13/12** on its first run — caught immediately because the discrepancy
was loud. The unlucky failure is a seed measured by a *narrower* predicate,
which lands GREEN and silently encodes the wrong population as "no drift".

Seed **after** any repairs the same goal makes, not before, or the baseline
memorialises drift you already fixed. Then re-run the shipped check once and
confirm it reports `STABLE:` against the value you just wrote — a seed you have
not read back is a claim, not a measurement.

Same root as `guard-920` (a regression test must replicate the literal shape its
production call site passes, not the contract-ideal shape) and `rb-245` (verify
the population exists before believing a zero) — measure the real thing, not the
convenient stand-in.

**And check the PREDICATE before you check the number.** Everything above assumes
the shipped check is correct and only the seed is in question. A goal that
commissions a ratchet usually names its check in prose, and that phrasing has
typically never been executed — it was written to describe the defect, not to
bound a population. Measured g-115-9541: the goal prescribed "grep for
BASH_SOURCE or `rev-parse --show-toplevel` under the world scripts dir", which
matches **174 files** on the live corpus, because nearly every script
legitimately uses BASH_SOURCE to locate ITSELF. Seeding that faithfully would
have shipped a permanently-red check (`guard-329`, `guard-574`) whose first
honest reading is "everything is broken". The narrowed four-condition predicate
that shipped reports 8. One `--list` run against the real corpus separates a
wrong number from a wrong question (`guard-5994`).

## Schema

```yaml
<metric_key>:                    # unique, kebab-case (e.g., learning_routing_drift)
  baseline: <int>                # lowest count ever recorded
  last_recorded: <ISO timestamp> # local system time, %Y-%m-%dT%H:%M:%S
  last_verdict: seeded | stable | ratcheted | regressed
  history:                       # bounded — last 50 entries
    - recorded_at: <ISO>
      drift_total: <int>
      verdict: <string>
      hostname: <name>                      # box that recorded the row; absent on older rows
      breakdown: {<component>: <int>, ...}  # optional, domain-specific
                 # a tree-derived ratchet also records head: <git sha>, dirty: <int> (see below)
```

Multiple metric keys coexist in one file. Writers append to `history` and
rewrite `baseline` / `last_recorded` / `last_verdict` atomically
(`.yaml.tmp → rename`).

## Verdicts

- `seeded` — first run; baseline = current count. Future runs compare against it.
- `stable` — current == baseline. No change.
- `ratcheted` — current < baseline. Baseline shrinks to current (one-way).
- `regressed` — current > baseline. Baseline **does not grow**. Surfaces as a warning.

## Merge across boxes

The file is merge-protected: when two boxes' copies diverge,
`coordination_merge.merge_audit_baselines` merges them per metric key. That handler is
the source of truth; each row below was confirmed by running it on synthetic entries.

| Field | Merge rule |
|---|---|
| `baseline` | MIN of the two sides. A merge never grows it. |
| `history` | Content-union of the two lists; identical rows collapse, and rows that differ in any field (a `breakdown.head`, say) both stay. |
| `last_recorded` | The later of the two. |
| `last_verdict` | The verdict of the side that recorded last. |
| Every other key (`matcher`, `unit`, anything new) | Taken WHOLE from the side whose canonical JSON sorts higher; a key on one side only is kept. There is no per-key rule. |

The last row is the trap for a new field. `baseline` sorts first in that canonical JSON,
so the winning side is typically the one with the HIGHER baseline, and a value stored
beside the scalar can come from a different reading than the MIN baseline next to it.
Measured: with baselines 444 and 446 the merged baseline was 444 and the merged extra
key was the 446 side's. A field that must survive a merge needs its own rule in the
handler first, and that rule must reach every box before any box writes the field.

A field INSIDE a history row is not that trap: the row is kept or collapsed whole, so what
it carries rides along and nothing has to reach a box first. The unchecked-write ratchet
records `head` and `dirty` there (see "Localising a regression"); a test merges rows that
carry them and checks both sides' rows stay and the baseline is still the MIN.

## Correcting a baseline

No ratchet writer raises a baseline (a regression keeps the old floor) and the cross-box
merge keeps the lower side, so a baseline moves only down. A `regressed` verdict clears
when the count comes back to the baseline: fix the drift and the next run reads `stable`
or `ratcheted`.

**Do not hand-edit `baseline` to a higher number.** The Edit tool reports success and the
number comes back (g-115-9813). Two mechanisms undo it, both in the own-cloud sync lanes:

- If a peer has moved the file since your last sync, the push of your edit is a union
  merge. `merge_audit_baselines` keeps the lower `baseline`, the merged bytes are written
  over your local file, and the push reports a landing counter (`*_merged`) that the
  PostToolUse hook counts as landed. Before 2026-09-15 (g-115-8029) the hook lane passed no
  manifest baseline, so EVERY edit of an existing object took this path, with no peer
  involved.
- If no peer had moved it, the edit lands, and the next peer push undoes it: peers write
  this file continuously, and a push from a copy that still carries the old floor merges
  back to the MIN.

`core/scripts/tests/test_audit_baselines_hand_raise_g115_9813.py` pins both against the real
sync lanes (simulated store, one manifest per machine), with a control (no handler: the
divergence freezes and nothing is lowered) and a counterfactual (a MAX handler lets the edit
stick). A local backend has no merge, so an edit there sticks and says nothing about the
fleet (guard-1943).

When the count moved because the PREDICATE did (a matcher widened, a population derived
differently, a parser re-spelled), the stored floor and the new reading measure different
things, and no writer can repair that in place:

| The new predicate counts | What to do |
|---|---|
| LOWER | Land the measuring code on every box before any box records (guard-6633). The first box to record sinks the shared floor and pins every un-upgraded peer at `regressed`. |
| HIGHER | Start a NEW metric key in the same change, named for the predicate generation. It seeds on its first run and is MIN-merged only against readings of the same predicate; the old key stays as history. Re-seeding the SAME key holds only while no peer still carries the old entry; a peer that does merges it back to the lower baseline (`experience_orphan_traces` was re-seeded on 2026-09-15, its entry now starting at a `seeded` row of 422, and has held). A new key has no old entry to merge against. It has been used once on this file: `goal_field_distinct_keys` became `goal_field_undeclared_names` in g-115-8691 (commit 4784d47465, 2026-10-03). The new key seeded at 3 on 2026-10-03T10:11:25 and its 15 rows through 19:07:22 are one `seeded` and 14 `stable`; the old key stays in the file, its last row at 10:54:55. |
| DIFFERENT PER BOX for a reason other than drift (each box's checkout, an uncommitted edit) | Measure something every box computes identically (rb-6062). Otherwise the floor is the lowest reading any box ever took, from whatever tree it had. |

## Change since the last reading

A REGRESSED line is a distance from a baseline that only shrinks, so a count that worsened
overnight and one that has sat unchanged for two weeks print the same shape. A lane that
records a reading also prints a `since last reading:` line saying whether the count moved
since THIS box last recorded one:

```
unchanged since 2026-10-03T16:50:38 (1h21m ago) on <host>: 25
worsened since 2026-10-03T16:50:38 (1h21m ago) on <host>: 22 -> 25 (+3)
improved since 2026-10-03T16:50:38 (1h21m ago) on <host>: 25 -> 22 (-3)
no earlier reading from this box (<host>) among the 50 recorded; 50 earlier row(s) carry no hostname
```

`core/scripts/_ratchet_delta.py` computes it from `history`, so nothing is stored beside
`baseline`. Four rules hold it together:

- **Per box.** The merged history interleaves boxes whose populations differ, so the last row
  is often another box's. Only rows carrying this box's `hostname` count, and rows with none
  (written before the attribution) match no box.
- **By time.** The merge sorts `history` by canonical content, so the newest row of this box
  is found by `recorded_at`, never by position.
- **No goal id in the line.** A tracking-goal id printed in tool output outlives the goal and
  tells later readers the defect is covered (guard-3263).
- **Read before the append.** The comparison is made inside the lane's lock, before the new
  row is added, so a row is never its own predecessor.

Wired into the four lanes of the precheck deferrable tier that keep a baseline:
unchecked-write, goal-field-census, stalled-goal and domain-term. A lane with no baseline
has no history to compare and prints no such line.

Rollout is order-independent. `hostname` sits inside a history row, which the merge unions by
content, and a box still on the old code writes rows without it: the lookup skips them and the
first run after an upgrade prints the "no earlier reading" form until that box has recorded a
row of its own. Nothing here changes a matcher, a predicate or a count, so the
land-fleet-wide-first rule for a change that lowers a MIN-merged metric (guard-6633) does not
apply.

## Localising a regression

A ratchet records a count, so a regression arrives as "+N" with nothing naming the sites.
Do not persist the members (see above). When the counted corpus is tracked in git,
rebuild the old corpus from its revision, run the CURRENT audit over both, and diff the
audit's own records. For the unchecked-write ratchet,
`bash core/scripts/unchecked-write-audit.sh --new-since baseline` names the sites that
joined (and left) the unverified set since the recorded baseline reading, and the
ratchet's REGRESSED line prints that command. Each history row records the checkout it
was read from: `breakdown.head` (the git HEAD) and `breakdown.dirty` (how many audited
inputs differed from it, untracked included), so a floor traces to the tree that set it
and `baseline` rebuilds exactly that head. A row written before those fields existed
carries none, and the last commit at or before its time stands in; a head this checkout
does not hold falls back the same way and says so. `--new-since <rev>` and
`--until <rev>` name the revisions explicitly. The command reads this file and writes
nothing. Why this design: `core/config/rationale/unchecked-write-delta-localisation.md`.

## Integration with /verify-learning

Each baseline gets one check line in `.claude/skills/verify-learning/SKILL.md`:

```
Check: <metric> stable or ratcheted down. Bash: `bash core/scripts/<name>-ratchet.sh`
→ expect exit 0 and a status line starting with `STABLE:` or `RATCHETED:`.
A `REGRESSED:` line means new drift was introduced since the last baseline.
```

Default exit-0-always keeps verify-learning runs unblocked. Opt-in hard-gating
via `VERIFY_LEARNING_DRIFT_HARD_GATE=1` in the env if a specific metric has
matured enough to be load-bearing.

## Reference implementation

`core/scripts/learning-routing-ratchet.{py,sh}` — the first baseline, tracking
cross-reference drift across reasoning-bank, guardrails, pipeline, experience,
pattern-signatures, and the knowledge tree. Baseline seeded 2026-04-23 at 0.

## Anti-patterns

- Baselining a ratio or continuous metric (wrong tool — use a gate)
- Seeding from an exploratory measurement instead of the shipped predicate's own
  output (see § Seeding — a narrower stand-in seeds GREEN and hides the drift)
- Seeding a predicate the commissioning goal NAMED but nobody RAN — that is a
  wrong question, not a wrong number, and it ships permanently red (`guard-5994`)
- Letting the baseline grow on regression (defeats the ratchet)
- Hand-editing `baseline` upward, or re-seeding the same key after a predicate change: the
  merge reverts it (see § Correcting a baseline)
- Storing a member set, or any new key, beside `baseline`: the merge takes every key it
  has no rule for whole from one side, so it can disagree with the MIN baseline it sits
  next to (see § Merge across boxes)
- Keeping unbounded history (current cap: 50 entries, enforced by writer)
- Reading `history[-1]` as "the last reading": after a merge the list is ordered by content, not
  by time, and it interleaves boxes (see § Change since the last reading)
- Using this file as a dashboard replacement (it's a guard, not a feed)
