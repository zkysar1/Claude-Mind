# Rationale: Owed-Close Sentinel

Referenced from `core/scripts/iteration-close.sh` (`_write_close_owed_sentinel`, `_clear_close_owed_sentinel`), `core/scripts/close-owed-sentinel.py`, and the `pending_close_owed` entry in `core/scripts/orchestrator-entry-battery.py`. Explains why a verified deep goal gets a durable "close still owed" record, and why that record has the lifetime, writer rules and dispatch it has.

## Why a sentinel at all

`iteration-close.sh --phase verify` marks a deep, non-recurring goal `completed` before its spark, state-update and learning gate have run. Everything that said those steps were owed lived in places a compaction erases: the transcript (the stdout imperative "invoke the spark BEFORE the state-update phase") and the iteration checkpoint (cleared on loop continue). The one durable record, `pending_phase_6_spark`, is written by `do_state_update` (g-115-2848 moved it there), so it does not exist yet during the window that needs it (guard-4245).

The failure: compact between verify and state-update, re-enter the loop, find no sentinel, select a new goal. The closed goal ends `completed` with no tree encoding, no experience record and no learning-gate pass, and nothing reports the loss (g-374-415). `postcompact-restore.py` prints a CLOSE OWED banner derived from the execution diary, but it fires only on the restore path, never at the loop's own entry battery, and a diary-derived banner cannot be listed beside the other entry sentinels.

The fix is one WM slot, `pending_close_owed`, written at the verify status write and cleared at state-update, so the entry battery can list the owed close the same way it lists every other recoverable obligation.

## Why it clears at state-update and not at the spark

The spark is one of three owed steps (spark, state-update, learning gate). `pending_phase_6_spark` is consumed when the spark fires, and that is correct for the spark. If the owed-close slot cleared on the same event, a compaction after the spark and before state-update would drop the other two steps with nothing left to name them: the same loss, one step later. So the slot clears only when `do_state_update` lands, at the `_checkpoint_refresh state_update` stamp, which is the checkpoint's own durable "state-update completed" record. Siting the clear on that stamp keeps the two records from disagreeing.

A spark firing alone never touches the slot. `test_close_owed_sentinel.py` pins that no spark-side file (`spark-fire-dedup.py`, `recurring-close.sh`, the spark and loop skills) mentions it.

The slot is a separate slot, not an extension of `pending_phase_6_spark`, for the same reason: that slot's consumers clear it on a spark firing, which is exactly the lifetime this obligation must not have.

## Why the write refuses to overwrite another goal's payload

The slot holds one payload and the payload is derived from one close, so it is loss-bearing (guard-2104). An unconditional write would cancel another goal's unconsumed obligation and nothing would report it. `close-owed-sentinel.py check-write` therefore returns `refuse:<other-goal>` when the slot names a different goal, and `_write_close_owed_sentinel` leaves the slot intact and says so on stderr. The refused close is not left unprotected: it still has the stdout imperative and the postcompact banner. In the normal flow the loop closes one goal at a time, so a second owed close while the first is unconsumed means a close was started without the battery having cleared the previous one; that case reports itself (the REFUSING line) instead of dropping one silently.

`check-clear` is the mirror: it removes only its own goal's payload and returns `keep:<other>` otherwise. The helper fails open in the direction that loses nothing. An error on check-write means write; an error on check-clear means leave the slot. A stuck sentinel nags and has a manual clear; a wrongly cleared one drops the obligation silently.

Both the write and the clear go through `verified-wm-set.sh` (write, read back, retry once), the framework's single writer for sentinel clears (g-115-3698). `sentinel-clear-guarded.sh` is not used: it needs a producing command with a readable artifact, and here the artifact is state-update's own checkpoint stamp, which the clear is already sited on. The slot is nonetheless valid for that script, since it unions the entry battery's `wm_slot` entries.

## Why the writer is sited right after the status write

The write sits directly after the `"${update_cmd[@]}"` status write in `do_verify`, not beside the stdout imperative roughly 600 lines later. Everything between is bookkeeping (outcome stamp, board post, in-flight clear, role stamps) that can abort the phase. An abort past the status write must not cost the sentinel, because the status write is what made the goal look finished (g-115-7663). The write is non-fatal for the same reason: the status write is the close, the sentinel is the net behind it.

The gate mirrors the spark sentinel's, narrowed to the one path that owes state-update: status `completed`, outcome `deep`, not a drain, and not a worker Body. A worker runs no state-update (its spark is the Phase 3.5 capture lane, consumed by the reducer at generalize-down), a drained goal belongs to another session's unit, and a recurring goal's close is `recurring-close.sh`'s. For any of those the slot would be written and never cleared. The siting and gate are source-pinned in the test file.

## Why a pure helper plus bash I/O

`close-owed-sentinel.py` does no working-memory I/O. A python process must not spawn `bash wm-*.sh` (the python-to-bash subprocess hang, rb-225 / rb-247), so `iteration-close.sh` reads the slot, pipes it through the helper for a one-line verdict, and does the write or clear itself. This is the shape `spark-fire-dedup.py` already uses.

## Why the dispatch text lives in the entry battery

The battery entry's `skill_section` IS the protocol: there is no SKILL.md phase body. The always-loaded loop skill is under a no-growth ratchet (the `commit-msg` hook refuses growth), so the instructions ride the battery output, which is read at the moment they apply. The text carries three things a reader cannot recover from the slot alone:

1. **Process state first.** The slot records an obligation, not liveness. A close process may still be running, and starting a phase it will also run duplicates it (rb-1906, guard-2638). The text sends the reader to `proc-match.sh iteration-close` and `proc-match.sh recurring-close` before acting.
2. **State-update is not idempotent** (guard-5303). A re-fired sentinel does not establish that the work was skipped, and re-dispatching is not always the repair (guard-3306). The text sends the reader to the execution diary: a `phase-8-state-update` `phase_end` after the verify means state-update already ran, so the correct move is a hand clear, with the reason noted in the diary.
3. **A `pending_phase_6_spark` line for the same goal is not "done".** It means state-update started and has not landed.

## Why prune, reset and merge all know about it (guard-2552)

A slot with no re-arming producer is exposed to every WM maintenance path. Two would erase this one and a third would bring back a copy that should be gone:

- **`wm-prune`** nulls non-protected scalar slots idle longer than `evict_threshold_minutes` (120). An owed close is written once at verify and nothing refreshes it, so a long wait before the loop resumes would evict it while the close is still owed. The authoritative list is `working_memory_pruning.protected_slots` in `core/config/memory-pipeline.yaml`; the code defaults are only fallbacks.
- **`wm-reset`** (consolidation) nulls every slot not in `RESET_SURVIVING_SLOTS`. That constant is hand-mirrored in `core/scripts/wm.py` and `mind_api/src/endpoints/wm_write.py`, and the daemon copy is the LIVE path because `wm-reset.sh` is daemon-only. A one-sided edit is inert in production.
- **`body-merge` (generalize-down)** is the path guard-2552 does not name. `_merge_value`'s absence rule returns the Body's value whenever the reducer's is `None`, so a worker that forked while the slot was set still holds the payload and hands it back after the reducer cleared it at state-update. The slot is reset- and prune-protected, so that resurrected copy would never go away: a phantom owed close for a goal that is already encoded. `REDUCER_AUTHORITATIVE_SLOTS` in `core/scripts/body-merge.py` makes `merge_wm`'s slot loop skip the slot, so the reducer's value stands, `None` included. It is a name-based exemption, like `REDUCER_WINS_KEYS` beside it. The type-driven general fix (rb-2302, rb-2327: a Body value equal to its fork-time baseline contributes nothing) would change merge behaviour for every slot, and is not made here.

`test_close_owed_sentinel.py` pins the lists and drives the real CLI reset and prune and `merge_wm` against an unregistered same-shaped control slot, so a cleanup or a merge that silently did nothing cannot pass.

## What it does not cover

- **Learning gate and productivity check after state-update.** The slot clears when state-update lands. Those two steps are named in the dispatch text and the iteration-close imperative, but this slot does not record them; whatever covered a compaction after state-update before this change (the checkpoint's phase stamps, the postcompact banner) still does, and no more.
- **A running close.** The slot cannot say whether a close process is alive. Step 1 of the dispatch text does.
- **A re-verify of an already-closed goal** writes the slot again. If state-update does not follow, the battery lists a close that is not really owed; the diary check in step 2 of the dispatch text is what tells the cases apart.
- **A stale slot** (the goal's close finished by hand, or abandoned) is cleared by hand with `echo null | bash core/scripts/verified-wm-set.sh pending_close_owed`, noting why in the diary.
- **Live effect of the reset list** needs the daemon to run the new `wm_write.py` (restart or promotion). The prune list is read from the config inside the request handler.
- **Other reducer-written sentinels have the same merge exposure.** Measured on cc-09 with the real `merge_wm` (reducer `None`, Body holding a stale copy): `pending_phase_6_spark` and `blocked_sleep_until` are re-delivered, as is an unregistered control slot; `pending_close_owed` stays cleared. The exemption covers this slot only.

## Cross-references

- guard-4245 — names the gap (a null `pending_phase_6_spark` is not evidence no spark is owed) and points at this fix
- guard-2104 — loss-bearing single-slot sentinels: classify before writing, refuse to overwrite
- guard-2552 — WM-slot audit: prune eviction and reset survival
- guard-5303, guard-3306 — state-update is not idempotent; a re-fired sentinel is not proof the work was skipped
- rb-1906, guard-2638 — process state before starting a phase another process may be running
- rb-225, rb-247 — python must not spawn `bash wm-*.sh`
- rb-2302, rb-2327 — type-driven merge policy and baseline-content (3-way) merging: the general fix the name-based merge exemption stands in for
- g-115-3698 — `verified-wm-set.sh` as the single writer of sentinel clears
- `core/scripts/body-merge.py` — `REDUCER_AUTHORITATIVE_SLOTS`, the generalize-down exemption
- `core/scripts/recurring-close.sh` — the guard-2104 refuse-guard precedent for the recurring path
- `core/scripts/postcompact-restore.py` — the diary-derived CLOSE OWED banner (the restore-path backstop)
- `core/scripts/tests/test_close_owed_sentinel.py` — helper, glue, siting, battery and survival pins
- `core/config/iteration-close-digest.md` — `§ VERIFY`, `§ STATE-UPDATE`, `§ LEARNING-GATE`
