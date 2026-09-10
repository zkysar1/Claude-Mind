# Why worker-loop Phase -0 stands down on the session-scoped stop-requested

On-demand rationale for the `Phase -0-stop` gate at the top of
`.claude/skills/worker-loop/SKILL.md`. The gate itself is four lines of
control flow in the hot path; everything explaining it lives here, because
`worker-loop/SKILL.md` is a budgeted hot-path member
(`core/config/hot-path-budget.yaml`, set `loop-skills`) and a byte added
there is paid on every compaction cycle of every agent, forever.

Filed and fixed as g-115-9461 (asp-115, HIGH). Discovered by
`encode-session-2026-09-08`.

## The defect

A worker Body `/stop` (`stop/SKILL.md` Step 0.6) arms
`agents/<agent>/sessions/<SID>/stop-requested`. That file was a **TURN-END
PERMIT ONLY**: `stop-hook.sh:387` reads it to emit
`ALLOW gate=worker-net-stop-requested-session`, letting the turn end.
**Nothing on the loop RE-ENTRY path read it.**

Measured twice, two boxes, `grep -c stop-requested`:

| file | 2026-09-08 (alpha, cc-07) | 2026-09-09 (alpha, cc-04) |
|---|---|---|
| `.claude/skills/worker-loop/SKILL.md` | 0 | 0 |
| `core/scripts/body-manifest.py` | 0 | 0 |
| `core/scripts/stop-hook.sh` | 8 | 9 |

(The stop-hook count moved 8 → 9 between the two readings; the two zeros
did not. Only the zeros are load-bearing — they are the defect.)

**Consequence.** Any armed `ScheduleWakeup` — the parked-Body re-poll (up to
3600s) or the deadman net (600s) — fires after the user stop, re-enters
Phase -0, reads `body_state` still `parked` or `active` (a worker `/stop`
deliberately does NOT retire the Body, and correctly so), and resumes the
loop the user just stopped. Observed live: after a `/stop` the park wakeup
was still armed and had to be cancelled by hand.

Until this gate existed the only defense was the model remembering to call
`ScheduleWakeup(stop: true)` at stop time — exactly the LLM-only-step shape
`guard-399` forbids. The fix does **not** belong in `stop/SKILL.md`, because
bash cannot cancel a model tool call; it belongs on the re-entry path, which
is Phase -0.

## Why FIRST, and why `body-manifest.py park-due` was left alone

The filing goal suggested *also* teaching `park-due` a stop-aware verdict so
the park orbit could not re-poll a stopped Body. Placing the stand-down at
the very top of Phase -0 makes that unnecessary: a stopped Body never
reaches the park branch at all, because the turn has already ended.

One predicate, one place, nothing to keep in sync
(`communication-clarity.md` rule 4 — elegance is subtraction). Do not add a
second copy to `park-due`: two readers of one signal is the drift shape
`guard-426` exists for, and `park-due`'s exit-code contract (0 due / 1 not
due / 2-3 error-fails-toward-polling) has no room for a third verdict
without changing every caller's branch.

## Why the two branches carry DIFFERENT labels (guard-6178)

The predicate is `test -n "$MIND_SID" && test -f ... && echo A || echo B`.
A `[ -n "$VAR" ] && [ -f ... ]` chain returns the **else-branch** label when
`$VAR` is empty — so a guard that *cannot evaluate* reports the NEGATIVE.
That is `guard-6178` exactly, and it is why the negative label carries
`sid=${MIND_SID:-EMPTY}` instead of a bare "no stop": an un-evaluatable
check must never be readable as an all-clear.

Continuing on `sid=EMPTY` is still the right direction — Phase -0's
CLOSED-SET discipline holds that an unrecognised state resolves toward
RUNNING, because a wrong close is the unrecoverable direction and a wrong
continue is not. But `sid=EMPTY` is a defect in the caller's environment,
not a clean read, and the label says so.

## Scope: session-scoped only

The gate is keyed on `$MIND_SID`, so it cannot leak to a sibling: a `/stop`
typed on box A leaves box B's worker Body running. That is the whole point
of the session-scoped file (g-115-7309). The AGENT-WIDE
`session/stop-requested` is a different signal with a different consumer —
the reducer's Phase -1.4 — and is deliberately NOT read here.

`session-signal-exists.sh` is not used: it resolves only the agent-wide
`agents/<agent>/session/<name>` path and has no session-scoped mode. A
direct file test also matches the idiom of the closure gate immediately
below it, which reads `agents/$MIND_AGENT/sessions/$MIND_SID/body-manifest.yaml`
the same way, and matches `stop-hook.sh`'s own documented choice at line 385.

## Cross-references

- `guard-6251` — the finding this gate closes (the file is a turn-end permit only)
- `guard-399` — no LLM-only steps for safety-critical control flow
- `guard-6178` — a shell guard that cannot evaluate reports the negative
- `guard-2676` — add worker-loop capability as a scoped call, never a restatement
- `core/config/conventions/hot-path-size-budget.md` — why this file exists
