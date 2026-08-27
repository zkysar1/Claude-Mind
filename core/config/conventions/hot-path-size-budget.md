# Hot-Path Size Budget

The always-loaded prose surface may not grow. Landed 2026-08-18 (g-115-6470) as
the structural half of the context-diet family (g-115-6472 report, g-115-6581
rule thinning); it turns `learning-philosophy.md` rule 5 — "subtraction is
learning" — from advisory into a commit-time refusal.

## What is budgeted

The SETS live in `core/config/hot-path-budget.yaml` (never in the gate script).
There are **two tiers with two different rules** — which rule a file gets is
decided by whether its set carries a `ceiling:` key.

### Tier 1 — the hot path (RATCHET: no growth at all)

| set | paths | why it is hot |
|---|---|---|
| `claude-md` | `CLAUDE.md` | every turn, every agent |
| `rules` | `.claude/rules/*.md` | every turn unless `paths:`-scoped in front matter |
| `loop-skills` | `.claude/skills/aspirations*/SKILL.md`, `worker-loop` | every loop iteration |
| `session-skills` | `boot`, `prime`, `respond` SKILL.md | every session start / user turn |
| `loop-digest` | `core/config/aspirations-loop-digest.md` | every iteration |

### Tier 2 — on-demand skills (CEILING: free growth below a fixed line)

| set | paths | rule |
|---|---|---|
| `on-demand-skills` | `.claude/skills/*/SKILL.md` (everything Tier 1 did not claim) | 64 KB `ceiling` |

Added 2026-08-18 (g-115-6690). **This set MUST stay LAST in the file** — `set_for`
is first-match, so Tier 1 claims the loop/session skills before this broad glob
sees them and they keep the stricter ratchet. Move it up and every loop skill
silently converts to a 64 KB ceiling with free growth beneath it; nothing else
would notice, because sizes stay legal and the gate stays green. Pinned by
`test_hot_skills_keep_the_ratchet_and_on_demand_skills_get_the_ceiling`.

Still deliberately NOT budgeted: `core/config/conventions/**`,
`core/config/rationale/**`, the tree, the reasoning bank — the on-demand homes
prose is supposed to move TO. Budgeting them would defeat the migration.

## Why a skill needs a CEILING and not a ratchet

The two tiers are paid at different moments, so they have different binding
constraints:

- A hot-path file is paid on **every turn of every agent, forever**. The
  constraint is the marginal byte, so the rule is a ratchet.
- A skill is paid **only when injected**. Its marginal byte costs nothing on
  turns that never load it. The constraint is instead a hard one: *does it fit
  in a single injection at all?*

A ratchet over ~120 skills would refuse ordinary edits that cost nothing and
generate constant override noise — which is worse than useless, because it
trains readers to reach for the trailer on the same gate that guards the hot
path. A ceiling refuses only what actually breaks.

**Why 64 KB — measured, not chosen.** On 2026-08-18 four observed skill
injections averaged 63,515 B, and an 88,887 B skill (`start/SKILL.md`) arrived
in context *explicitly truncated*. Past that line a skill reaches the model with
its later content silently absent. For `verify-learning/SKILL.md` at 1,208,426 B
that is most of the file — which is how a VERIFICATION skill came to be unable to
see most of its own checks (g-115-6689).

**Over-ceiling files may shrink and hold flat, only GROWTH is refused.** This is
load-bearing, not a courtesy: 16 of 120 skills were already over the line the day
the tier shipped, holding 62% of all skill bytes. A rule that refused any commit
touching them would freeze them against the very extraction that fixes them.
Pinned by `test_ceiling_tier_end_to_end_through_the_real_hook` case 2.

Blast radius when introduced: of those 16, nine were already Tier-1 governed, so
the tier newly constrains **seven files totalling 1,850,572 B that nothing
governed before** — verify-learning (1,208,426), analyze-npc-behavior (170,251),
felt-sense-checkin (136,872), reflect-on-outcome (102,931), start (88,887),
fresh-eyes-review (76,105), tree (67,100).

This is NOT the "per-file ceilings" the override-noise watch below anticipated.
That watch is about caps being treated as a toll; this tier answers a different
question (injection truncation) and would have been correct on day one. The watch
still stands on its own terms.

## The rule

`core/githooks/commit-msg` → `core/scripts/hot-path-size-gate.py` refuses a commit when:

1. a **Tier-1** file is **larger in the commit than at HEAD** — the cap IS the size
   at HEAD, so every shrink tightens the cap by itself, on every box, with no
   registry number to keep in sync;
2. a budgeted file that is **new at HEAD** exceeds its set's `new_file_cap`
   (a rename keeps the old path's HEAD size as its cap); or
3. a **Tier-2** file that is **already over its set's `ceiling`** grows further
   (`grew_over_ceiling`). Below the ceiling, growth is unrestricted and HEAD is
   not consulted at all.

Merge commits are skipped (MERGE_HEAD present): a merge combines commits that
were each gated where they were made, and gating it would wedge every fleet
pull the moment any box overrode. Plumbing failures (registry unreadable, sizes
unreadable, ledger unwritable) WARN and allow — a commit is never wedged on the
gate's own machinery; `--check` surfaces the breakage.

## The bypass, and why it is a trailer

```
size-budget-override: <why this must live in the hot path>
```

anywhere in the commit message (≥ 8 chars of justification; `#` comment lines
do not count). With `iteration-commit.sh`: `--message "size-budget-override: <why>"`.
It is a commit-message trailer rather than an env var because the justification
then lives in `git log` forever and travels through promotion — which is also
why the gate is a `commit-msg` hook: `pre-commit` runs before the message exists.

A `git revert` of a diet commit regrows the file and is gated like any other
commit: use `git revert --no-commit <sha>`, then commit with the trailer naming
what the revert repairs. Rebase and cherry-pick do not run commit-msg (git skips
hooks there); a merge commit is skipped by the gate itself.

Every accepted override appends one record to `world/override-bypass-ledger.jsonl`
with `gate: hot-path-size-gate` (the single-gate shape from `gate-overrides.md`:
no `slots_filled`; `context.files[]` carries path/head/staged/delta, and
`context.net_bytes` the total). Count them:

```bash
grep -c '"gate": "hot-path-size-gate"' "$WORLD_PATH/override-bypass-ledger.jsonl"
```

An override moves HEAD, so it permanently loosens that file's cap by the bytes
it added — deliberate (a file kept "over budget" would demand a trailer on every
later touch and drown the ledger in noise), and exactly why the ledger count and
the ratchet below exist. **WATCH after the first week: if overrides run at more
than a handful per day, the caps are being treated as a toll rather than a
signal — tighten the refusal text or add per-file ceilings then, not before.**

### WATCH RESULT — week one, measured 2026-08-25 (g-115-6639, bravo, cc-05)

**Verdict: keep the mechanism as built. Do NOT add per-file ceilings.**

26 override rows over the 7 days 08-18..08-24 (6, 8, 2, 3, 3, 2, 2) = 3.7/day
average but **2.4/day over the last five**, i.e. declining after the two ship
days. Authored growth through the trailer: **+35,488 B**. By agent: alpha 15,
zeta 6, echo 3, foxtrot 2.

The "repeated overrides on the same file" trigger IS met — strategic-scan 7,
felt-sense-checkin 4, worker-loop 4 — and per-file ceilings are still the wrong
remedy, for the reason the module docstring already gives: a cap stored in YAML
must be rewritten by the very hook that cannot reliably stage a file into a
pathspec commit, so it drifts; size-at-HEAD does not. Set against the 25 of 55
ratcheted members (45%) already sitting above their set's `new_file_cap` and
carrying 86% of hot-path bytes, any ceiling would be either a no-op (set above
current) or a wedge (set below).

**The mechanism is already working, and that is the load-bearing measurement.**
Against the 2026-08-19 reading recorded in g-115-6639's own description:
`aspirations-strategic-scan/SKILL.md` **134,121 → 117,456 B (−16,665)**;
hot_path_total **1,554,344 → 1,539,528 B (−14,816)** while on_demand_skill_bytes
went **1,951,156 → 2,033,414 B (+82,258)**. Prose is moving OUT of ratcheted
files INTO ceiling-governed ones — the designed direction. Both ratchets still
read `regressed` against their seed baselines, so the FAIL line is expected
state, not new drift; re-seed the baselines rather than reading FAIL as a fresh
regression.

**ROUTING VERDICT for the overrides themselves.** 11 of 26 rows (42%) carrying
**+22,862 B = 64% of all override bytes** are dated-measurement-series appends
on exactly TWO files (strategic-scan, felt-sense-checkin). The other 15 rows
(+12,626 B) are imperatives — a new lane, a call site, the fix itself. So the
override population is bimodal, and only one mode is prose. Note
`felt-sense-checkin/SKILL.md` is **not ratchet-governed at all** (it matches only
`on-demand-skills`, which is CEILING-governed) — its 4 rows are ceiling breaches
and belong to a different question.

**STEP 4's refusals:overrides RATIO IS NOT COMPUTABLE, and finding out why is
the most important result here.** `meta/gate-firings.jsonl` holds **0** rows for
`hot-path-size-gate` across 174,421 firings and 46 distinct gate names. That is
not "no refusal ever happened": `gate-log.sh` states its own precondition —
*"gate-id must match an id in core/config/gates.yaml"* — and neither
`hot-path-size-gate` nor its commit-msg sibling `goal-claim-commit-gate` is among
the **34** registered ids. `gate-log.sh` runs under `2>/dev/null` and `exit 0`
("telemetry must not break gates"), so the rejection is silent and permanent.
Positive control that makes this decisive: the sibling hook `core/githooks/pre-commit`
has **55** firing rows; `core/githooks/commit-msg` has **0**, for **both** of its
gates. Both Layer-B commit-msg gates have therefore been blind since they
shipped. Owner: **g-115-3488**, which PREDICTED this exact defect
("implemented-but-unregistered is the QUIET one") and now carries it as its first
measured instance — nothing new was filed. Until it lands, the ledger count is the ONLY
visible half of this gate's behaviour, and it counts bypasses — never refusals.


## Reporting

```bash
bash core/scripts/hot-path-size-gate.sh --check          # HEAD sizes per set + ratchet
bash core/scripts/hot-path-size-gate.sh --check --no-ratchet
bash core/scripts/hot-path-size-gate.sh --explain <path> # which set / cap a path gets now
```

`--check` measures HEAD blobs (committed truth, identical on every box) and
prints one `PASS:`/`FAIL:` line, which the `/verify-learning` check
`hot-path-size-ratchet` greps. `FAIL` means an override or a merge added prose
since the baseline — route it out.

**The two tiers are counted and ratcheted SEPARATELY**, and the line reports
both: `hot <N> B + on-demand <M> B = <total> B`. `hot_path_total_bytes` in
`meta/audit-baselines.yaml` keeps counting **only Tier 1**, so its history stays
continuous across this change — adding the second tier moved no number that any
existing baseline was tracking. Tier 2 ratchets separately as
`on_demand_skill_bytes` (both `lower_is_better`, `audit-baselines.md` schema).
Merging them would have made a 3.2 MB step-change look like a regression on a
metric whose whole value is the trend.

## Adding a surface

Add a set (or a path to a set) in `core/config/hot-path-budget.yaml`. Globs:
`*`/`?` never cross `/`, `**` does, no wildcard = literal member. Never edit the
gate for a new file. Give the set a `ceiling:` to get Tier-2 semantics, omit it
for the Tier-1 ratchet — and if the set's glob is broad, **append it at the end**
(first-match ordering, above).

**A ceiling set does not take a `new_file_cap`.** The ceiling bounds new and
existing files alike, so a second number would be accepted, validated, and then
never consulted — measured: `ceiling 65536` with `new_file_cap 8192` admitted a
brand-new 65,536 B file. The loader defaults `new_file_cap` to the ceiling when
omitted and **refuses** a ceiling set that declares a different one, rather than
silently preferring one of the two. If you genuinely want new files held below
the ceiling, that is a second policy and needs its own set. A Tier-1 ratchet set
still requires `new_file_cap` — the relaxation is ceiling-only.

Regression pins: `core/scripts/tests/test_hot_path_size_gate.py` (refuse-growth,
tighten-on-shrink, new-file cap, rename, override → ledger, merge skip,
pathspec-commit index visibility, fail-open, `--check` ratchet; plus the Tier-2
truth table, the tier-discrimination pin, and an end-to-end ceiling run through
the real hook). Both ceiling tests were mutation-verified — a `decide()` that
never refuses over-ceiling growth turns them red.

A trap worth knowing before adding a key: `load_budget` rebuilds each set from an
explicit key list, so a new key in the YAML is **silently dropped** unless it is
carried through there. `ceiling` was, during development — the only symptom was
the `--check` line reading `on-demand 0 B`, with every test still green.

## Related

- `.claude/rules/rationale-extraction.md` — the pointer format for narrative moved
  out of budgeted pseudocode
- `core/config/conventions/audit-baselines.md` — the ratchet store
- `core/config/conventions/gate-overrides.md` — the ledger record shape
- `core/scripts/context-diet-report.py` — the before/after instrument this gate
  protects (fixed preamble bytes + closes-per-compaction)
- `core/scripts/doc-pin-map.py --file <path>` — which lines of a doc a
  verify-learning or test pin reads; run it before cutting prose to make room
