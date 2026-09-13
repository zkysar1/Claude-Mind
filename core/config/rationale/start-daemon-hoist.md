# Why /start hoists the daemon start above the ex-worker fork guard

**g-115-9671.** Filed from a principal forward: four run-recap emails from the
Vinheim resident `alien-rescher-2` arrived inside 35 seconds with no added
commentary. The forward was the message — four copies of one agent telling the
principal it had not acted.

## What the resident did

It ran `/start`, correctly diagnosed TWO independent blockers — a worker fork
file on the terminal's SID, and a local daemon that was unreachable and could
not be auto-spawned — and **named the remedy itself**: *"first ensure the daemon
is running (bash core/scripts/mind-api-start.sh)"*. Then it closed with *"I did
not perform any of those follow-up actions in this run."*

That reads as timidity. It was not.

## The actual cause: control flow, not judgement

The ex-worker fork guard issued a total `STOP`, and `mind-api-start.sh` sat
LATER in every branch that contained it:

| branch | guard STOP | daemon start |
|---|---|---|
| RUNNING + autonomous (W-pre) | :276 | :306 (W2) |
| reader / assistant observer | :429 | :471 |
| IDLE (0-pre2) | :558 | :659 |

So whenever a terminal carried a fork file, the daemon start was **structurally
unreachable** — excluded by the skill's own control flow. Re-running could only
reproduce the halt, which is why the principal received four copies of one
non-action rather than an escalating series. The resident's judgement was sound;
the branch it was executing says `STOP … DONE` above the line that would have
acted.

## Why hoisting is safe

The guard is right about what it protects — a session whose Bash hook keys
`BODY_ROLE=worker` for its lifetime must not become the reducer, with unmerged
divergence as the stake (g-306-210). Its SCOPE was wrong. Starting the daemon:

- is **box-level and agent-agnostic** — it binds no session, touches no
  `running-session-id`, and cannot promote a fork to reducer;
- is **agent-provisionable**, so routing it to a human is the
  `capability-before-user.md` violation this goal was filed about;
- is already **fail-open by contract** (`|| echo … non-fatal`) at every site.

Nothing the guard protects is weakened by running it. **The guard refuses the
PROMOTION, not the PROCESS.**

## Why all three sites, not one

Sites W-pre and the observer branch reuse 0-pre2's **MESSAGE** but not its
**STEPS**, so a hoist written only in 0-pre2 would leave both of them halting
above their own daemon start (guard-3448: a gate is only as broad as its entry
points). The IDLE branch is the one a resident's plain `/start` actually hits,
so fixing only the RUNNING branch would have left the real path broken.

## Scope

This closes outcome 1 of the goal. Outcome 2 — explaining the daemon auto-spawn
failure on env `debc47de` with a log line, exit code, or reproduction — needs
evidence from the Vinheim environment host. The two blockers are INDEPENDENT (the
resident said so itself), so making the daemon start reachable does not explain
why it failed there; it may still fail, and now it will fail *visibly* with its
rc on stderr instead of never running.
