# Rationale: Dry-Spin Guard (Phase -0.5e.0c)

Referenced from `.claude/skills/aspirations/SKILL.md` Phase -0.5e.0c. Explains
why a fourth loop-entry short-circuit exists beside the two cycle caches and
idle-tick, and why its predicate is shaped the way it is (g-357-88).

## Why a fourth check, when three short-circuits already run

The three that precede it are all *consumers*:

| check | reads state written by |
|---|---|
| `idle-tick.sh` | `blocked_sleep_until` (B7) |
| `quiescence-cycle-cache.py` | `quiescence-last-cycle.json` (quiescence-gate) |
| `dry-idle-cycle-cache.py` | the baseline cache (dry-idle-tick) |

Each therefore assumes the previous cycle *wrote something*. A cycle that routed
to all_blocked and then wrote **nothing** is, at loop entry, byte-for-byte
indistinguishable from a productive cycle — so the loop does the only thing left
and reloads the full ~75-minute all-blocked handler.

That is not hypothetical. Measured on coach (claude-mind, zc-03) 2026-09-03
02:10Z, the third all-blocked cycle of the night: the pane reported *"Step B7.2
yield complete — ScheduleWakeup armed for 600s"* and `[x] 23 Step B7.2`, while
the state read one minute later showed `consecutive_blocked_sleeps=0`,
`blocked_sleep_until` unset, `quiescence.last_check_at` 16 hours stale, no live
sleep job, and **zero execution-diary rows across the whole 66-minute handler**.
The steps were narrated, not run. `aspirations-all-blocked/SKILL.md` B7.2
forbids exactly that shape in prose — but prose is not a gate, and the
guardrails around it (guard-967, guard-1230) are behavioral too.

## Why the marker had to be written by goal-selector

A backstop against a narrated handler cannot itself depend on the handler
writing anything, or it inherits the failure it exists to catch. The one actor
that provably runs on such a cycle is the **selector**, because the all_blocked
route *is* its verdict. So `goal-selector.py` writes
`loop_state.signals.last_all_blocked` at the moment it emits `all_blocked: true`
— a script, at the moment of the route, through the loop_state single writer.
A narrated handler can neither fake that marker nor suppress it.

Coverage is deliberately the **script-emitted route only**. `aspirations-select`
also returns `selection_reason: "all_blocked"` when a partner holds the only
candidate, and `"all_blocked_by_gate"` when the blocker gate exhausts the
ranking; both are LLM-narrated returns, so no script runs at that moment and
marking them would reintroduce the very LLM-discretionary step this design
removes. Those cycles keep today's behavior — absent marker, guard no-ops,
normal entry — which is the goal's own negative control #5.

## Why the predicate is three independent absences

A correctly-executed all-blocked handler leaves three separate traces, written
by three different steps: `blocked_sleep_until` (B7), a registered Tier-A sleep
job (B7.2), and execution-diary rows (B6.5/B7/B7.2). The guard fires only when
the route is fresh **and all three are absent**. Any one being present stands
the guard down.

The redundancy is not belt-and-braces for its own sake — it fixes the direction
of failure. A missed detection costs one slow cycle, which is exactly today's
behavior and therefore not a regression. A *false* detection would sleep through
live work. So every probe fails open toward MISS: an unreadable diary or an
unanswerable job query is treated as "there was activity / there is a sleep",
never as licence to sleep.

The diary probe is the one signal with an **independent writer**. WM and
team-state can be written by the same narrated step that lied about the yield; a
diary row exists only because a script ran. Once the B-steps' diary rows land,
a genuinely-executed handler trips this probe and the guard stands down on its
own.

## Why the age gate is short, fixed, and not derived from base_seconds

`min_reentry_gap_s` defaults to 120 — deliberately equal to the dry-idle
`base_seconds` **DEFAULT**, not to the live `base_seconds`. A deployment that
raises `base_seconds` to 7200 for flat two-hour idle blocks (g-357-90) must not
thereby widen this window to two hours, because the window bounds how long a
*stale* marker can keep firing the guard. Past the gap, a genuine long sleep
elapsed and normal entry is the correct behavior.

## Why it is self-retiring

On a fire the guard calls `dry-idle-tick.py` rather than computing a sleep
itself. That single call advances `loop_state.signals.dry_idle` **and** writes
the dry-idle baseline cache, so the next loop entry short-circuits through the
ordinary Phase -0.5e.0b fast path instead of returning here. The guard handles
the first cycle of a trough and hands the rest to the existing machinery.

It also stamps `sleep_registered` on the marker through the single writer. That
is what makes a fire idempotent: without the stamp the guard would re-fire
against the same marker on every entry inside the gap window.

## A reader/writer hazard this file must record

The guard reads loop_state through `wm-read.sh`, **not** through
`quiescence-gate._wm_read_loop_state`, despite the latter being documented as
the canonical reader. That reader goes through `_rt.wm_read`, and the **python**
daemon client does not send the `X-Mind-Sid` header the **shell** client sends
(`_runtime.sh` rt_curl). The daemon needs that header to resolve a Body's
per-session WM. Measured 2026-09-04 (cc-07), same endpoint, same query:

```
bash   rt_call GET /v1/wm/read?slot=loop_state&json=1   -> {"goals_completed": 82, ...}
python _rt.rt_call(same)                                -> 'null\n'
python _rt.rt_call(same, headers={"X-Mind-Sid": ...})  -> {"goals_completed": 82, ...}
```

The marker lives in the Body WM, so the canonical reader returns `{}` on a
worker and the guard could never fire on the role that runs the loop hardest.
The same defect pins `dry-idle-cycle-cache._dry_signal()` at `{}` on a worker,
which holds that Layer-4 fast path's `dry_active` gate permanently False there —
a consequence wider than this guard, relayed rather than fixed inline because
changing `_rt.rt_call`'s headers touches every python daemon client in the
framework.

## Cross-references

- `.claude/skills/aspirations/SKILL.md` Phase -0.5e.0c — the call site
- `core/scripts/dry-spin-guard.py` — the guard; `core/scripts/tests/test_dry_spin_guard.py` — its controls
- `core/scripts/goal-selector.py` `_write_allblocked_marker` — the marker writer
- `core/scripts/loop-state-bump-counters.py` — the loop_state single writer (`--all-blocked-marker`, `--all-blocked-sleep`)
- `core/config/rationale/dry-idle-backoff.md` siblings / `_dry_idle.py` — the backoff curve this defers to
- guard-967, guard-1230 — the registered-sleep contract the directive satisfies
- guard-4870 — a re-entry watch is valid only against a signal re-measured between the two reads
- g-357-89 — sibling: makes the B7.2 yield completable on a no-notify harness
