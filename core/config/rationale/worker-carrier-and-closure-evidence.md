# Rationale: Worker carriers and closure evidence — how a unit's outputs leave the box

Referenced from `.claude/skills/worker-loop/SKILL.md` Phases 3.7 (carrier check),
3.8 (carrier push), 3.9 (closure evidence) and Phase 4 (the 4b hand-off row, the
in_flight and sentinel notes). Each section below is that phase's WHY commentary,
moved here verbatim on 2026-09-23 (g-115-8214) so the skill fits under the
65,536 B injection ceiling. The rules and their ordering stayed at their call
sites; what moved is the provenance.

## Phase 3.7 — why a carrier check exists (g-306-263)

Phase 3.7 — CARRIER CHECK (g-306-263). Numbered 3.7, NOT 3.9: Phase 3 above
delegates to the EXECUTE PROTOCOL's "Phase 3.9 .. 4.5", so a worker-loop phase
also called 3.9 would put two different Phase 3.9s in one file, thirty lines
apart, naming different documents. Sits between 3.5 (spark capture) and 4 (end
of work unit) in this loop's own numbering.

The hand-off named in Phase 4 below is
NOT universal: it carries the worker's WM and its goal record, and it carries
NOTHING ELSE. A framework file edit made on a worker box reaches the reducer
via no channel at all — measured on g-115-5147, whose finished fix sat on
cc-07 and was 0% present on cc-04, reported COMPLETE the whole time. Nothing
was broken; there was simply no carrier, and no moment at which that was said
out loud. This is that moment.

(The numbering argument above is as the skill stated it. Phase 3.9 CLOSURE
EVIDENCE, below, was added later under the same number, so the skill now does
carry two different Phase 3.9s — the execute protocol's and its own.)

## Phase 3.8 — why the carrier push is not a contradiction of --no-push (g-306-264)

Pushes HEAD to refs/workers/<agent>/<sid>, then STOPS — it never touches the
shared branch. This does NOT contradict Phase -0.3's --no-push: that flag's
rationale is contention on shared store files, and a ref whose path contains
this Body's sid has exactly ONE writer by construction, so the rationale does
not reach it. Fail-soft like every other iteration-push call — never branch on
the rc, and never let a failed push stop the cycle.

The REDUCER side is `bash core/scripts/worker-ref-consume.sh` (fetch + report;
--merge <ref> to take one). A worker does NOT run the consumer: merging another
Body's framework edits into the shared tree is a reducer act, and report-only
is deliberate — a framework change that applies to drifted context is worse
than one that is lost.

## Phase 3.9 — why closure evidence has a producer, and why it runs after 3.7/3.8 (g-115-5158)

WHY IT EXISTS: closure evidence is the `outcome_note` field, and exactly one
thing produced it on the close path — iteration-close do_verify Step 3, which
a worker skipped entirely until 2026-08-16 (Phase 4a below calls do_verify
with a ONE-LINE --summary; it passes --no-supersede on the worker path, so
THIS phase stays the rich-narrative producer and 4a only backfills when
this phase did not run — g-115-6633). Before that a worker had
NO producer at all. Its notes were written by hand or not at all, which means
the rate was DISPOSITIONAL, not mechanical. Measured 2026-08-09 on asp-115: the
live worker sat at 48/48 and every other SID at 24/26, so no asymmetry was
visible — and that is exactly the trap. One disciplined agent's 100% says
nothing about the next Body, and arming any enforcement gate on outcome_note
would have refused 100% of worker closures while passing reducer ones,
MANUFACTURING the disparity it was meant to remove. Fifth instance of the
inheritance gap (workers never pulled, skill-dedup, deadman, watchdog --tick).

RUNS AFTER 3.7/3.8 ON PURPOSE. Phase 3.7's STRANDED branch also writes
outcome_note, and this helper is write-if-absent/never-clobber — so placing
this before 3.7 would silently prevent a stranding from ever being recorded.
Ordered this way, a stranding note wins and this call declines, which is the
correct precedence: a stranded output is the more urgent artifact.

## Phase 4b — why the hand-off row matters

4b. HAND-OFF ROW. Append the completion row body-merge.py reads
    (`_completed_goal_ids` -> `merged_goal_ids` -> worker_retrospective.py).
    Without it merged_goal_ids is ALWAYS empty and the consolidate Step -0.9
    retrospective (team-state / journal / findings / experience / imp@k lanes)
    has nothing to run over — this file carried 0 references to the slot
    before 2026-08-16, so that lane had never fired once. Same row shape as
    aspirations-state-update Step 3 (goal-selector reads these keys — do not
    rename); omit work_class when the goal record has none. Only for
    status=completed. Routes to the Body WM (BODY_WM_PATH), never agent-wide.

## Phase 4 — why there is no unconditional in_flight clear, and no sentinel between units

team-state in_flight: 4a's --if-goal clear is the ONLY clear you perform, and
it fires only when the shared row names this goal. Do NOT add an unconditional
clear (g-306-132-d): Phase 2's claim WRITES in_flight, and in_flight is
AGENT-keyed with no sid, so a worker and its reducer share one row — an
unconditional clear would blank a live reducer's row, worse than the stale row
it fixes. The stop-hook additionally calls worker_close_in_flight_clear.py after
a genuine close (result marked/marked-push-failed); it clears ONLY when the goal
named by the live in_flight row carries THIS Body's claimed_by_sid, and a second
hand-rolled clear on this path would defeat that ownership test.
Do NOT write the body-closing sentinel here — finishing ONE work-unit is NOT a
genuine close; Phase 5 re-enters this loop for the next unit, and a sentinel
left here would make a turn-end between units mark the Body closed
prematurely, losing later divergence (g-306-70). The sentinel is written ONLY
when SELECT finds no work (Phase 1) — the unambiguous genuine close. A worker
that ends abruptly without reaching Phase 1 (crash, terminal closed) leaves no
sentinel; cleanup-stale-bindings then stages its WM via the stale-binding path,
so no divergence is lost either way.

(Corrected in the skill on 2026-09-23: "written ONLY when SELECT finds no work"
predates g-353-73. SELECT finding no work now PARKS; the sentinel is written only
by Phase 1's expired-park close, which the skill's Phase 1 already states as the
single close condition.)

## Cross-references

- `.claude/skills/worker-loop/SKILL.md` Phases 3.7–4 — the steps these sections explain
- `capture-carrier-delivery-check.md` — Phase 3.7's second check (capture delivery)
- `suite-run-voided-by-loop-merge.md` — why Phase 3.8 consults the tree lock first
- `worker-verify-own-unit.md` — Phase 4a's two calls
- `worker-defer-predicate.md` — the release-with-defer path in Phase 4a
- guard-2676 — the no-transcription contract (3.7, 3.9)
