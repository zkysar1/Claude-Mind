# Rationale: Who Deletes `iteration-checkpoint.json`, and Why Order Beats Idempotence

Referenced from `.claude/skills/aspirations-state-update/SKILL.md` Step 1.1.
Explains why that step's `rm` must NOT run before
`iteration-close.sh --phase state-update`.

## Why there are two deleters at all

`iteration-close.sh` `do_productivity_check()` runs `rm -f "$AGENT_DIR/session/iteration-checkpoint.json"`.
Its own comment records the intent: the deletion was MOVED into bash so anchor
cleanup is script-enforced (the g-255-03 anchor showed `phase=selected` while
disk truth showed `status=completed`), and the LLM-driven `rm` was deliberately
**kept** as defense-in-depth, on the stated ground that it is *"idempotent — file
already gone is rm's success path."* That follows the same SSOT pattern as
rb-254 (`last_maintain_at`), guard-155 (cadence timestamps), and g-248-75
(tree-encoding-drift-gate), where bash became the single writer/cleaner for
previously LLM-discretionary state.

The reasoning is sound and the retention is correct. The gap is that
**idempotence is a property of `rm`, not of the ORDER**, and the comment only
considered the case where the LLM's `rm` runs *after* the script's — or where
the file is already gone. The SKILL.md places Step 1.1 *before* the steps that
call `iteration-close`, so an agent following the pseudocode literally deletes
the anchor while three downstream probes still need it.

## Why order matters: three readers sit between Step 1.1 and line 3687

All three live inside `do_state_update`, i.e. they run in the SAME invocation
that later deletes the file:

| site | what it reads | what it does with it |
|---|---|---|
| `iteration-close.sh` `SELECTED_AT=` (in `do_state_update`) | `selected_at` | tree-updated validation when `--tree-updated` was NOT passed |
| `iteration-close.sh` `VAL_SELECTED_AT=` (in `do_state_update`) | `selected_at` | tree-updated VALIDATION when `--tree-updated` WAS passed |
| `iteration-close.sh` `force_metric_encoding_pending` probe | `selected_at` | metric-encoding probe → `force_metric_encoding_pending` |

Each is guarded by `[[ -f ... ]]`, so a missing anchor is not an error — it is a
**silent downgrade to "unknown"**, which is the failure shape this framework
treats as worse than a crash (guard-1760: report what you declined to look at).

## Measured consequence (g-115-5489, zeta, cc-02, 2026-08-22)

Following Step 1.1 literally, then calling
`iteration-close.sh --phase state-update --tree-updated`:

- two `[loop-state-save] WARN: update against a MISSING iteration-checkpoint
  ... wrote nothing` lines (keys `outcome_class`, then `phase_completed` +
  `last_updated`);
- `METRIC-ENCODING: ... tree-edit probe UNAVAILABLE (no iteration-checkpoint
  anchor; Phase 2.95 likely skipped this iteration) -- encoding state UNKNOWN,
  not verified-absent`, which SET `force_metric_encoding_pending` and queued a
  next-iteration re-encode dispatch against a goal that **had** encoded (a new
  tree node, a decision rule, and a propagate had all landed minutes earlier);
- the incidental phantom-tree-node detector that `guard-3467` tells readers to
  rely on (`--tree-updated` + "no tree-file change" output) was disabled for
  that close.

## The diagnostic misattributes its own cause — do not trust it

The metric-encoding line says *"Phase 2.95 likely skipped this iteration"*, and
the `loop-state-save` warning says *"Cause is almost always a skipped Phase
2.95."* In the measured incident 2.95 ran normally and wrote the anchor; the
anchor was **hand-deleted at Step 1.1**. Both messages name the frequent cause
rather than the observed one, so an agent debugging from the message alone is
routed to re-anchor a phase that never failed. If you see these warnings,
check for an early `rm` before concluding anything about `/aspirations-select`.

## The rule

- **iteration-close is the executor (the normal loop path)** → SKIP the `rm`
  entirely. The script owns it and deletes it at the right moment.
- **ad-hoc callers that bypass iteration-close** (`/reflect`,
  `/aspirations-consolidate`, hand-driven closes) → run the `rm`, and run it
  AFTER the phase work, never before.

## The sibling site is CORRECT — do not "fix" it too

`core/config/aspirations-loop-digest.md:504` carries the same `rm -f
agents/<agent>/session/iteration-checkpoint.json` line, and it is the "LLM-driven
rm in the loop digest" that `do_productivity_check()`'s comment names. Checked
2026-08-22: the digest places it **after** `iteration-close.sh --phase
productivity-check`, i.e. after the phase in which line 3687 already ran — so
there it is a genuine idempotent no-op and the defense-in-depth argument holds
exactly as written.

The defect was unique to `aspirations-state-update/SKILL.md`, where Step 1.1
sits **before** the steps that call iteration-close. An agent sweeping for
sibling copies of this instruction will find the digest; leave it alone. The
discriminator is not the `rm` — it is what runs between it and the script's own delete.

Other `rm`s of this file (`start/SKILL.md:1127`,
`core/config/start-phase-c.md:497`, `aspirations-graceful-stop/SKILL.md:359`,
`core/config/conventions/session-state.md:273`) are crash-recovery and
stop-path cleanups with no in-flight probes behind them, and are also out of
scope.

## Cross-references

- `.claude/skills/aspirations-state-update/SKILL.md` Step 1.1 — the consumer
- `core/scripts/iteration-close.sh` — readers `SELECTED_AT=`, `VAL_SELECTED_AT=`,
  `force_metric_encoding_pending` (all in `do_state_update`); owning deleter in
  `do_productivity_check()`. Cited by SYMBOL not line (guard-4398/guard-2310):
  the original `:2457`/`:2483`/`:2551`/`:3687` had drifted ~140-180 lines within
  four days of being written.
- `core/scripts/postcompact-restore.py` — the post-autocompact read side
- `guard-3467` — the incidental phantom-tree-node detector this blinds
- `guard-2666` — a compact-hook in-flight assertion is a claim read out of this
  same file, not a reading of goal status
- `guard-1760` — a checker reports what it RAN, never what it declined to look
  for; a `[[ -f ]]`-guarded probe that silently reports UNKNOWN is that shape
- `prose-mandate-reporting-rate` (tree) — why the imperative stays at the call
  site and only the mechanism moved here
