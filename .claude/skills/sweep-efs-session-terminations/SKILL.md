---
name: sweep-efs-session-terminations
description: >-
  Classifies recently-terminated experiment sessions on the remote shared filesystem into
  their termination lanes — never-streamed readiness reap, step-0 main-loop watchdog, driver
  startup timeout, or normal — in a single remote round trip. Invoke this whenever a goal asks
  how recent sessions ended, whether a watchdog or readiness fix actually landed in the
  deployed build, why sessions are terminating early, or needs a breadth check across the last
  N sessions, INSTEAD of hand-rolling find/grep over session logs. Invoke it ALSO before
  concluding a termination class is absent: it carries the positive and discriminating controls
  that stop a zero from being misread as evidence, and it reports the watchdog threshold
  verbatim so a pre-fix death cannot be mistaken for a post-fix one.
forged: true
forged_by: zeta
forged_date: "2026-07-26"
forged_from: gap-024
user-invocable: false
triggers:
  - classify session terminations
  - why did sessions terminate
  - session termination breadth check
  - did the watchdog fix land
  - readiness reap check
  - recent session log sweep
parameters:
  limit: "newest-first session cap (default 10)"
  account: "restrict to a single account uuid (default: all accounts)"
  since: "time floor on the {unix-millis} session-id prefix (ISO or raw millis); REQUIRED when scoring a forward-phrased hypothesis"
tools_used: [Bash, Read]
companion_scripts:
  - world/scripts/efs-session-classify.sh
  - world/scripts/efs-ssh.sh
conventions:
  - efs-session-paths
---

# /sweep-efs-session-terminations

Reads the newest N experiment-session directories on the remote shared filesystem and
classifies how each one ended. Replaces the hand-rolled `find | grep` pipeline that was
re-derived twice (gap-024) — once for a 49-session breadth validation, once for a
10-session window — with the same four-signal read done once, correctly.

## Step 0: Load Conventions

`Bash: load-conventions.sh efs-session-paths` — read the paths returned that are not
already in context. The convention owns the canonical session-directory layout and the
log-line schema; this skill owns the classification.

## When to invoke

- A goal asks how recent sessions ended, or why they are ending early.
- A fix shipped and you need to know whether sessions still die the old way.
- A breadth check: "is this one session or a pattern?"
- **Before concluding a termination class is absent.** This is the highest-value case
  and the reason the script exists — see Zero-Count Discipline below.

## When NOT to invoke

- You need the *content* of one specific session's log — read it directly instead.
- The question is about scoring or gameplay outcomes, not termination.
- No remote access is configured; the skill reports `unavailable` and stops.

## Restricted Operations

All remote access **MUST** go through `world/scripts/efs-session-classify.sh`, which
itself reaches the host only via `world/scripts/efs-ssh.sh`. **Never raw `ssh`.** The
wrapper carries `StrictHostKeyChecking=no`, `UserKnownHostsFile=/dev/null`, a connect
timeout, and credentials loaded from the environment store — a raw `ssh` respects
`~/.ssh/known_hosts` and can fail on a host-key rotation the wrapper ignores, producing
a "connection dead" false positive and a blocker for a problem that does not exist
(`.claude/rules/probe-with-canonical-code-path.md`, guard-147, rb-246).

The classify script is read-only: it runs `find`, `grep`, `head`, `tail`, and `ls` on
the remote host and writes nothing there.

## Procedure

1. **Run the sweep.**
   ```
   Bash: source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/efs-session-classify.sh" --limit 10 --output json
   ```
   `$WORLD_PATH` resolution is load-bearing, not cosmetic: `world/` is an external path
   and the Bash hooks do not rewrite path arguments, so a bare `bash world/scripts/...`
   dies rc=127 — which reads exactly like a dead connection
   (`.claude/rules/path-resolution.md`).

2. **Branch on `status` FIRST, before reading any counts.**

   | `status` | Meaning | Action |
   |---|---|---|
   | `ok` | Root enumerated, sessions read | Proceed to step 3 |
   | `no_sessions` | Root enumerated, held zero session dirs | Genuine empty — report it |
   | `unavailable` | Remote unreachable, or canonical root did not enumerate | **STOP.** `sessions: []` here means UNKNOWN, not clean. Report unavailability; do NOT report "no abnormal terminations" |

3. **Discard every `control_failed` session before interpreting the mix.** A
   control-failed row is a failed *measurement*, not a quiet session — its zeros say
   nothing about how that session ended. `classified` (not `scanned`) is the denominator
   for any ratio you report.

4. **Read the mix**, then report per class:

   | Class | Signature | What it means |
   |---|---|---|
   | `A_readiness_reap` | `No updates for Ns`, no error, no `ENV` appSource, step 0 | The client never streamed, so its main loop never started, so readiness never completed |
   | `B_step_watchdog` | user-environment error carrying `Main loop exceeded Ns` | Main loop started, then stalled at the step watchdog |
   | `driver_startup_timeout` | `Startup timeout - missing components` | Driver never assembled its components |
   | `normal` | `ENV` appSource present with a rising step count | Scoreable, streamed session |
   | `edge` | none of the above matched cleanly | Read the row's raw fields before drawing a conclusion |

5. **Always quote `watchdog_verbatim` verbatim — never reduce it to a boolean.** The N
   in `Main loop exceeded Ns` dates the build against the fix deploy. A session reading
   `7s` when the shipped grace period is `20s` died on a PRE-fix build, and reporting only
   "the watchdog fired" hides that the fix may be working fine on everything built after
   it. Presence answers the wrong question; the value answers the right one.

6. **Scoring a forward-phrased hypothesis? Pass `--since` — a bare sweep CANNOT do it.**
   `--limit N` returns the **newest N**, which is not **the next N after T**. Both return
   N rows and nothing in the output announces which frame produced it, so an unfloored
   window keeps re-including pre-fix sessions until enough new ones accumulate — and the
   claim gets scored against rows its own basis already cites.

   This is not hypothetical. On 2026-07-26 a bare `--limit 12` returned 10 abnormal
   sessions with `A_readiness_reap`=7 and `B_step_watchdog`=1 — numerically a clean
   CONFIRMED for `2026-07-25_readiness-lane-dominates-post-grace-failures` (criterion:
   class-A >=7, class-B <=1 of the next 10). It was not evidence: the lone class-B row
   was session `1784784476282_964`, the exact session that hypothesis names in its own
   basis as pre-fix. The single row deciding the class-B conjunct was the one row
   guaranteed to be in-sample.

   Pass the deploy or formation timestamp as the floor:
   ```
   Bash: source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/efs-session-classify.sh" \
           --limit 12 --since "2026-07-23T12:25" --output json
   ```
   Then read `excluded_by_floor` before reading the mix — a short result under a floor
   means the floor filtered, NOT that the fleet went quiet. Measured on that date: floor
   `1784809500000` excluded **65** sessions and left **3**, all `A_readiness_reap`, zero
   class-B. Directionally consistent with the hypothesis, but n=3 < 10 — so it still does
   not resolve, now for the honest reason (sample too small) rather than a circular one.

   The floor keys off the session-id millis, **not** the directory mtime the walk sorts
   by. Those genuinely disagree — the unfloored top-12-by-mtime contained sub-floor ids —
   and the id is what "the next N after T" means. Lesson: rb-5170.

## Zero-Count Discipline

The point of this skill. Three guards, each catching a different way a zero lies. All
three are enforced inside the script — none is left to judgment at the call site.

- **guard-467 — canonical path.** The session root comes from
  `world/conventions/efs-session-paths.md`. A wrong path yields zeros exactly as
  convincingly as a healthy fleet does, so the script proves the root enumerates before
  reading anything under it, and reports `unavailable` when it does not.
- **guard-1214 — same-root positive control.** Per session, in the same remote read, the
  script counts a marker known to be present in every session log. A count of zero means
  the *log read* failed; the session is emitted `control_failed` and never classified.
  Zero targets plus a non-zero control is a measurement. Zero targets alone is
  indistinguishable from a search that ran against nothing.
- **guard-1419 — discriminating control.** Class A's signature is the *absence* of the
  streaming appSource, and that absence has two explanations implying opposite fixes:
  "the client never streamed" (chase the readiness bug) versus "this log stream carries
  no non-JAVA appSource at all" (chase the log channel). A generic control passes under
  both. So the script requires an *adjacent* appSource — emitted by a poller, independent
  of anything the client does — to be present before it will call a session class A. Both
  absent means the discriminator itself is dead, and the row is `control_failed`.

Corollary for the caller: **an empty result is not a clean bill of health until you have
confirmed `status == "ok"` and `control_failed == 0`.** Both are in the output for exactly
this reason.

## Output contract

JSON on stdout: `{status, root, limit, scanned, classified, control_failed, mix{}, sessions[]}`,
plus — **only when `--since` was passed** — `floor_millis`, `excluded_by_floor`, and
`floor_note`. Those three keys are absent on an unfloored run, so the default path emits
the exact pre-`--since` key set and every existing fixture keeps working unchanged.
`excluded_by_floor` is what makes an empty floored result honest: without it, "the floor
filtered everything" and "the fleet is quiet" are the same output.
Each session carries `session_id, account, class, java, bitnet, env, user_environment_error,
watchdog_verbatim, no_updates_verbatim, startup_timeout, max_step, first_ts, last_ts,
termination_notes`, plus a `control_note` when a control failed and a `lane_note` on class B.
`--output text` gives a one-line-per-session summary for quick reads.

Exit codes: `0` ok/no_sessions · `3` unavailable · `2` usage error. The script never exits
non-zero for "found abnormal terminations" — it measures; the verdict is made in context.

## Error handling

| Failure | Script behavior | Your action |
|---|---|---|
| Remote unreachable | `status: unavailable`, exit 3, stderr tail included | Report unknown, not clean. Do not file a blocker without re-probing via `efs-ssh.sh "echo ok"` |
| Canonical root missing | `status: unavailable`, exit 3 | The layout moved — update `world/conventions/efs-session-paths.md` first, then re-point the script's root constant |
| A session has no log file | that row is `control_failed` | Exclude from ratios; the rest of the sweep is still valid |
| All rows `control_failed` | `classified: 0` | Treat the whole sweep as UNKNOWN — this is the pipeline failing, not a quiet fleet |

## Cost

One remote round trip regardless of `--limit`; the per-session loop runs host-side because
per-session round trips dominate the cost. `--limit 10` is the default working window;
a 50-session breadth check is roughly linear in remote log-grep time. Prefer a small limit
for a spot check and raise it only when the question is genuinely "is this a pattern?"

`--limit` counts **sessions**, not directories: the walk filters to the documented
`{unix-millis}_{3-digit-suffix}` session-id pattern before applying the cap, because the
experiment root also holds non-session directories. Without that filter a non-session
directory both consumes a slot and trips the positive control, and a control that cries wolf
on directories that were never sessions teaches the reader to ignore the rows where it is
right.

## Testing

`bash "$WORLD_PATH/scripts/tests/test_efs_classify_selection.sh"` — 17 assertions, no EFS
and no SSH. Run it after any edit to the classifier.

There are **two** fixture seams, at different points in the pipeline, and which one you
reach for is decided by what you are trying to falsify. They are mutually exclusive: set
both and the script exits 2 rather than silently honouring one.

**Downstream — `EFS_CLASSIFY_FIXTURE=<file>` (classification).** Substitutes the recorded
remote payload *after* enumeration, so it exercises the five-class verdict logic and both
controls without live access. Fixtures proving all five classes plus the unavailable path
are the forge-time dogfood set (Step 3.6); the decisive pair differs only in the
discriminating control's count and must yield different verdicts. Format: a `ROOT_OK <n>`
line followed by tab-separated session rows of exactly 15 fields: `S`, session_id, account,
`LOG`|`NOLOG`, java, bitnet, env, user_env_error, watchdog_verbatim, no_updates_verbatim,
startup_timeout, max_step, first_ts, last_ts, `yes`|`no`. Omitting the `ROOT_OK` line is
the unavailable fixture. Kept at `world/scripts/tests/efs-classify-payload-5class.tsv`.

**Upstream — `EFS_CLASSIFY_DIRS_FIXTURE=<file>` (selection).** Substitutes only the
`find | sort -rn` directory listing — one path per line, `#`/blank lines ignored — and then
runs the **same** `$CAND_PIPELINE` the live remote walk runs. That shared-constant detail is
what makes the tests evidence about the live path rather than about a re-implementation: the
pipeline is one string, interpolated into both the remote body and this seam. It covers the
session-id pattern filter, the `--since` floor, the `--limit` cap, and — the part no
after-the-fact seam can reach — their **order**. Kept at
`world/scripts/tests/efs-classify-dirs-basic.txt` and `-mtime-skew.txt`; the skew fixture
pins the g-250-271 trap that the walk sorts by directory mtime while the floor keys off the
session id, so "newest N" and "newest N by id" genuinely differ.

**`--since` is refused with the downstream seam only (exit 2), and that refusal is now
narrow rather than absolute.** Against the downstream seam the floor would emit
`floor_millis` with `excluded_by_floor: 0` — indistinguishable from "the floor ran and
nothing was older", the rb-5170 shape reproduced inside the tool built to prevent it. So it
still fails loudly there. Against the upstream seam the floor filters real candidate paths
and the exclusion counts are true, so the combination is permitted and is what the harness
uses. This is the correction to the forge-time claim that the floor could only be verified
against live EFS: the limit was never `--since`, it was **seam placement** — the same
boundary that let the non-session-directory bug through. A seam's placement silently
declares what the suite can never falsify, so when a real defect lands outside the reachable
set, move the seam rather than accepting the blind spot.

## Chaining

- **Called by**: `/aspirations-execute` Phase 4 when a goal concerns session terminations.
- **Feeds**: the streaming-inactivity-termination-watchdog knowledge node — a sweep that
  changes the known lane mix, or that surfaces a pre-fix watchdog value after the fix
  deployed, is a tree-reconciliation trigger.

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is a Bash call handing control back to the orchestrator, e.g.
`echo "Return to orchestrator — continue to next phase"`. Never end with a text summary.
