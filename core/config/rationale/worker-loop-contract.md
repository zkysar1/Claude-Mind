# Rationale: Worker-loop contract — activation history and the goal-filing ruling

Referenced from `.claude/skills/worker-loop/SKILL.md`: its opening activation
note and its `## May a worker file a goal?` ruling. The passages below are the
dated history behind both, moved here verbatim on 2026-09-23 (g-115-8214) so the
skill fits under the 65,536 B injection ceiling. The ruling itself (its table,
its three obligations, and the reducer complement) stayed in the skill.

## Activation status as of 2026-08-05

<!-- POST_RECOVERY_EDIT_OVERRIDE="user-directed fresh-eyes doc fix from a live assistant session; on-disk mode file wrongly reads autonomous (anomaly filed as world goal), no loop is running" -->
**Activation status (updated 2026-08-05, fresh-eyes review):** ACTIVATION IS
LANDED — g-306-119-a (/start worker auto-join branch), g-306-119-b (close-body
staging+push), g-306-119-c (baseline-aware merge consume) and g-306-125
(safety rails) are all completed, and a live worker executed a real goal via
this loop (g-315-518 soak, DESKTOP-O91DLK2). The prior "until Phase 2C wires
fork-activation" wording predated those landings. Still OPEN before trusting
multi-body at scale: g-306-120 (cross-box activation dry-run), g-306-126
(live two-box soak), g-306-128 (kill-tests), and g-306-131 (three fail-safe
inversions in the reducer-liveness poll this loop runs every cycle). Design
SSOT: the `mind-engine-identity-bridge` tree node (Phase 2).

(The HTML comment above was an edit-time override token for
`post-recovery-edit-gate.py`, which reads the EDIT content, not the file, so it
carried no meaning once that edit landed. It moved with the paragraph.)

## Why the lifecycle split exists (g-306-212)

The phase split above answers "which PHASES does a worker run". It does not
answer "what does a worker do at each session LIFECYCLE stage", and for a long
time nothing did — so every lifecycle asymmetry was discovered by surprise, one
at a time: prime never runs for workers (g-306-211), the per-body heartbeat
cannot write on an IDLE worker box (g-306-208), compact restore rejected
body-keyed checkpoints (g-306-174). Same defect class each time: a reducer
lifecycle stage with **no declared worker disposition**.

## Why the goal-filing ruling exists (g-306-250)

The prior contract was one sentence — "a worker never fabricates goals" — and it did
not settle the live cases. Four instances accumulated where a worker measured
something real and had no sanctioned move, and three separate agents recorded "this
deserves its own goal" without filing one. This is the ruling; do not re-derive it.

## Case B's delay bound, measured and falsified

Case B's 12.5h bound (g-306-238) is FALSIFIED: re-measured 2026-09-18, the
lane's last filing was g-115-9648 on 09-10 — 7d8h and zero filings, with six
would-be owners pending (g-115-9921). A stalled replay costs the WORK. B/C
splits on RECOVERABILITY, not delay: any Body can see a B finding again; a
dropped machine-local one is not late, it is gone.

## Cross-references

- `.claude/skills/worker-loop/SKILL.md` — the activation note and the filing ruling
- `worker-spark-replay-bounded-drain.md` — the reducer's replay that files Case-B relays
- g-115-9921 — the stalled-replay measurement's owner
- guard-1204 — dedup before filing; guard-2783 — state the complement when a ruling acts on a role
