# Rationale: /start — the IDLE branch: runner claim, body fork and first boot

Referenced from `.claude/skills/start/SKILL.md`. Extracted 2026-08-25 (g-115-7706):
that skill was 89,106 B against the 65,536 B on-demand injection ceiling, so roughly
its last 27% never reached the model at all. Every block below is VERBATIM — this was
a relocation, not a rewrite. The skill retains ALL procedure: its YAML front matter,
every `Bash:` line, every HALT/refusal branch, every `>` user-facing display line,
every bold directive, and every test-pinned literal. Only explanation moved here.
(That claim was false for IDLE 0-pre, whose branch bodies had moved here; g-115-10839
put them back, and the 0-pre section below is a WHY summary, not a verbatim block.)

## Why IDLE step 0-pre (the interrupted-stop check) looks the way it does

*(the step is `start/SKILL.md` IDLE 0-pre. g-115-7706 relocated its branch bodies
here, where `/start` never read them; g-115-10839 put them back in the skill. Only
the WHY stays here — every branch command lives in the skill.)*

**Why it exists.** It is the explicit-resume twin of the Session Start Protocol
IDLE-branch check (CLAUDE.md, g-317-09): the passive session-start path probes the
same sentinel, but a user who re-engages via `/start` instead of a chat message
would otherwise skip straight to the IDLE→RUNNING flip and strand the
half-finished stop's learning. A `/stop` is interrupted most often during the
long D4 consolidate. Source: core/scripts/stop_checkpoint.py module docstring
(FW-11, lines 4-11).

**Why the explicit `MIND_AGENT=` prefix is REQUIRED.** The Phase 2.6 binding is
not written until Step 0, so the PreToolUse[Bash] auto-inject hook cannot resolve
the agent yet, and `stop_checkpoint.py` takes the agent from `MIND_AGENT` alone —
unset, it exits. Source: core/scripts/stop_checkpoint.py `_agent_name()`
(lines 72-78); .claude/skills/start/SKILL.md IDLE Step 0 (the binding write).

**Why the on-disk mode picks resume or clear.** Graceful-stop D7 is the step that
sets the post-stop mode, and D4 consolidate runs before it. A still-`autonomous`
mode therefore means the stop never reached D7 and its consolidation/handoff may be
incomplete, which `/aspirations-graceful-stop --resume` completes idempotently
(consolidate, handoff, set target mode, clear the checkpoint). Inside D7's single
Bash call the checkpoint clear rides the mode flip's `&&` as
`{ stop-checkpoint.sh clear || true; }`, and D7.1 clears it again. An
`assistant`/`reader` mode beside a surviving sentinel therefore means D4 already
landed and only that clear was lost; retiring the sentinel is all that is left,
and a sentinel left in place keeps `resume-needed` answering 0 for a stop that
already finished. Source: .claude/skills/aspirations-graceful-stop/SKILL.md D7
(line 475, SIGN-OFF note lines 472-474) and D7.1; core/scripts/stop_checkpoint.py
docstring lines 17-18.

**Why exit 1 and exit 2 both continue.** Exit 1 covers both "no interrupted stop"
and the resume-count breaker (MAX_RESUME_ATTEMPTS = 3), which exists to stop
auto-resuming a persistently failing stop. Exit 2 is the script's error code: a
sentinel the script cannot read cannot be resumed from either, and a branch table
with no row for the value the agent meets is the guard-6447 shape. Source:
core/scripts/stop_checkpoint.py lines 37-38 and 65.

## initial status=active record for THIS session so it is vis

*(was `start/SKILL.md` L756-766)*

   initial `status=active` record for THIS session so it is visible in the
   live-sessions view before it closes. The matching close is WP3 (graceful-stop
   D6.6) for the autonomous runner path, or WP2 (/stop IDLE branch) for a
   reader/assistant start — both finalize the same world/telemetry/session-records/<agent-name>/$MIND_SID.json
   record. World is already configured here (resumed/existing agent), so WORLD_DIR
   resolves. `<target-mode>` is the determined target mode (the same value the
   binding-write above used — `reader`, `assistant`, or `autonomous`). For the
   autonomous path the runner claim (Step 3) writes latest-session-id == $MIND_SID,
   so the close targets this same record. write_open is idempotent and never
   raises. guard-165: SID/agent/mode via ENV, python source single-quoted.
   `py -3` (Bash-tool context). Fire-and-forget (|| true).

## 1. Determine target mode

*(was `start/SKILL.md` L769-771)*

1. Determine target mode:
   - If `--mode` flag provided: use that mode
   - Else: `autonomous` (always — regardless of previous mode)

## The explicit AYOAIAGENT= prefix is belt-and-suspenders: it

*(was `start/SKILL.md` L787-791)*

   The explicit `MIND_AGENT=` prefix is belt-and-suspenders: it bypasses the
   PreToolUse[Bash] hook's auto-inject path, so even if the hook is still
   cold-starting Python and times out, the script still receives the env var.
   Without this, the very first Bash call after writing `.active-agent-<SID>`
   can race the hook and fail with "no agent active (MIND_AGENT not set)".

## The read-side complement of /stop's D6.7 flush (session-co

*(was `start/SKILL.md` L809-819)*

   The read-side complement of /stop's D6.7 flush (session-continuity redesign,
   2026-06-02). Under the own-cloud backend this materializes the agent's
   continuity-tier session files (handoff.yaml, working-memory.yaml,
   execution-diary.jsonl, reasoning-snapshot.yaml, pending-questions.yaml, ...)
   from S3 to local NOW — BEFORE Step 3's `/prime` (reader/assistant) or `/boot`
   (autonomous) does its raw Read of those files. Without it, an agent moved to a
   new machine would resume from a STALE or absent local copy and lose everything
   the previous machine learned. The endpoint (owncloud_sync.pull_continuity) is
   freshness-aware: it NEVER clobbers a local file carrying unpushed local writes
   (the same-machine crash-restart case) — the manifest baseline gates every
   overwrite. Under the local backend it is a clean no-op.

## Scope: the CONTINUITY SET ONLY (~17 objects, enumerated fr

*(was `start/SKILL.md` L821-831)*

   Scope: the CONTINUITY SET ONLY (~17 objects, enumerated from
   session-manifest.yaml). It does NOT sweep `agents/<agent>/temp/`, which is not
   continuity-tier — the manifest does not list it. It used to, and that made this
   step's cost scale with scratch population instead of the manifest, pushing it
   past its own RT_CURL_TIMEOUT ceiling exactly in the machine-move case it exists
   for: measured cc-04 2026-08-02, 164.6s / scanned=1590 / pulled=125 of which
   only 2 were continuity; after, 1.7s / scanned=17 (g-115-4574). A machine-moved
   agent therefore does not auto-resume its temp/ working docs — pass
   `--with-temp` to fetch them (they are untouched in S3). Acceptable because
   durable records may never point into temp/ (guard-1373) and box-dependent
   temp-citation dangling is already ratified in artifact-reference-integrity.md.

## Placement rationale: runs for ALL modes after the binding

*(was `start/SKILL.md` L833-842)*

   Placement rationale: runs for ALL modes after the binding (Step 0) and
   mode-set (Step 2) but while state is still IDLE — so it is OUTSIDE the
   autonomous "nothing stoppable between RUNNING and /boot" critical section
   (it precedes the RUNNING flip at the autonomous sub-path below), and it runs
   BEFORE the autonomous `wm-set session_start` so this session's stamp lands on
   the freshly-pulled working-memory rather than being clobbered by the pull.
   Non-blocking (`|| echo WARN`): a pull error must never block /start — local
   state is the fallback and the daemon's periodic sweep reconciles. The daemon
   need not be up yet (the wrapper auto-spawns it); Step 3's `mind-api-start.sh`
   is then a no-op.

## (Ensure runtime daemon is up before any wrapper call. Idem

*(was `start/SKILL.md` L876-879)*

     (Ensure runtime daemon is up before any wrapper call. Idempotent — no-op
     if already running. Fail-open: if spawn fails, the wrapper layer's
     rt_ensure_running handles it on first call. Do NOT make /start fail on
     daemon spawn failure.)

## - DDB runner-claim acquire (single-runner lifecycle, desig

*(was `start/SKILL.md` L910-924)*

   - **DDB runner-claim acquire (single-runner lifecycle, design §4).** Fires HERE —
     immediately after the triple-write above, and BEFORE every shared or synced
     mutation below — because the acquire's ONLY dependency is the runner-token the
     triple-write just produced, and a refusal must leave no trace a peer can observe.
     Until g-115-4653 it sat four steps lower (below body-manifest, the stale-file
     `rm -f`, the loop-active clear, and the `wm-set.sh session_start` seed), and both
     `team-state-update.sh` writes ran ABOVE the triple-write — so an rc=4 refusal on
     box B had ALREADY blanked the LIVE box-A runner's `current_focus`, reset its
     `session_ended`, and overwritten the continuity WM `session_start`. A start that
     correctly refused to displace a running peer still corrupted it on the way out.
     Fixing this needed BOTH moves: hoisting the acquire alone would not have helped,
     because the two team-state writes preceded even the triple-write the acquire
     depends on. Every step now above this point writes only `sync_tier: machine_local`
     session files (`core/config/session-manifest.yaml`) — nothing above describes this
     runner to a peer.

## (Cross-machine half of single-runner enforcement: a condit

*(was `start/SKILL.md` L926-932)*

     (Cross-machine half of single-runner enforcement: a conditional DDB IDLE->RUNNING
     claim using the runner-token from the triple-write above. Under
     STORAGE_BACKEND=own-cloud the daemon does the real DDB CAS; on any other backend
     runner-claim.sh no-ops (exit 0). **ACQUIRE_RC=4** means a peer machine holds a
     live DDB claim for `<agent-name>`. It BRANCHES on `reducer_only` (Step 0.5):
     either way, do NOT proceed to any step below in THIS branch — `agent-state`
     stays IDLE and nothing shared or synced has been touched yet.

## the CW sequence below (cross-box worker). The body role is

*(was `start/SKILL.md` L936-942)*

     the CW sequence below (cross-box worker). The body role is DERIVED, not
     declared (user directive 2026-08-03): rc=4 IS the detection that a live
     reducer exists elsewhere — the acquire auto-breaks stale claims, so rc=4 ⇒
     the holder heartbeated within OWNERSHIP_STALE_SECONDS. Same one rule as the
     same-box RUNNING branch, which has always derived the worker role without a
     flag. CW0's status read doubles as the LOUD derivation announcement —
     render it as:**

## The --reducer-only refusal must NAME THE HOLDER, not just

*(was `start/SKILL.md` L950-954)*

     The `--reducer-only` refusal must NAME THE HOLDER, not just assert one exists — "another machine"
     with no machine_id leaves the user unable to act on any of the options it then
     offers. Read the holder identity first (rc is already known; this is purely for
     the message, so it is fail-open — on any error print the message without the
     holder line rather than suppressing the refusal):

## (Prints e.g. status: LIVE (backend=own-cloud) — 'echo' is

*(was `start/SKILL.md` L956-959)*

     (Prints e.g. `status: LIVE (backend=own-cloud) — 'echo' is RUNNING on 'cc-03',
     heartbeat 520s old (threshold 3900s)`. Landed g-306-118-b; it is token-free by
     contract precisely so a box that holds no runner-token for this agent — which is
     every box in this situation — can still read the holder.)

## (CORRECTION, g-306-119-a: this message previously said the

*(was `start/SKILL.md` L980-986)*

     (CORRECTION, g-306-119-a: this message previously said the "RUNNING-observer /
     Worker-Body branch is unreachable cross-box". That was true when written and is
     now HALF false — the Worker-Body IS reachable cross-box, and a bare `/start`
     (role derivation, 2026-08-03; previously the explicit `--body worker`) is
     exactly how. The observer half remains true and is kept. Left uncorrected it
     would be the worst kind of stale text: an authoritative-sounding sentence
     telling the user the feature they were just offered does not exist.)

## Any OTHER non-zero rc (1 = daemon/DDB error) is FAIL-OPEN

*(was `start/SKILL.md` L988-989)*

     Any OTHER non-zero rc (1 = daemon/DDB error) is FAIL-OPEN: log and PROCEED — a
     transient DDB hiccup must not block a legitimate start.)

## - CW: Cross-box worker activation (ACQUIRERC=4 unless redu

*(was `start/SKILL.md` L991-994)*

   - **CW: Cross-box worker activation** (ACQUIRE_RC=4 unless `reducer_only`; design
     `world/docs/cross-box-two-bodies-design.md` §2). Skip this whole block on every
     other path. The daemon was already ensured earlier in the IDLE flow, so a cold
     box is fine here.

## (The design doc §1 says "delete the just-written runner-to

*(was `start/SKILL.md` L997-1008)*

     (The design doc §1 says "delete the just-written runner-token"; deleting all
     three is strictly stronger and is what the design's own stated property
     actually requires — the Gate-0 mismatch it relies on keys on
     `running-session-id`, not on the token.) The
     triple-write above already created `running-session-id`, `latest-session-id`
     and `runner-token` on this box. A worker box must carry NONE of them: with no
     `running-session-id`, stop-hook Gate 0 always mismatches (turn-ends route to
     the body-close path), `is_reducer()` is false, and recovery-gate is indifferent
     because `agent-state` stays IDLE. This is the first action in the branch
     specifically so an interrupt anywhere later leaves the box NON-reducer-shaped —
     the safe direction. Removing all three is what makes "the worker is invisible to
     box-B recovery machinery" true rather than aspirational.

## (The agents/ prefix is load-bearing here for the same reas

*(was `start/SKILL.md` L1010-1013)*

     (The `agents/` prefix is load-bearing here for the same reason as the
     triple-write above — dropping it leaves the agent name as the FIRST path
     segment, which resolves at PROJECT_ROOT and silently deletes nothing.
     `rm -f` so a partially-written triple-write cannot wedge the branch.)

## Then the collision gate — note it takes POSITIONAL args, <

*(was `start/SKILL.md` L1021-1022)*

     Then the collision gate — note it takes POSITIONAL args, `<agent> <sid>`, and
     has NO flag form:

## ERROR:SIDCOLLISION cross-agent or ERROR:SIDCOLLISIONSAMEAG

*(was `start/SKILL.md` L1025-1040)*

     `ERROR:SID_COLLISION` cross-agent or `ERROR:SID_COLLISION_SAME_AGENT`), and on
     `SIDCOL_RC=1` (script/usage error).** Only rc=0 continues. Treating rc=1 as
     passable would convert a usage mistake into a silent bypass of the one gate that
     stops two bodies sharing a unitKey — and rc=1 is exactly what a wrong arg shape
     returns. Do not continue into a WM fork with an ambiguous unitKey: the SID IS the
     unitKey, so a collision would make two bodies write one manifest and one forked
     WM. The gate has no override flag by deliberate design — do not add one.
     SCOPE NOTE — know what this gate can still see here. CW-pre just deleted
     `running-session-id`, and the gate's SAME-AGENT branch short-circuits to rc=0
     when that file is missing. So on a worker box the same-agent half is VACUOUS by
     construction — which is correct (there is no local runner to stomp), but it means
     rc=0 here attests only the CROSS-AGENT property: no OTHER agent's live runner is
     bound to this SID. Do not read a pass as "no collision of any kind." The
     same-agent risk this branch actually carries is two workers for the same agent on
     the same box sharing a SID, which the gate cannot see and which the per-session
     dir naming makes self-evident instead.

## (--reducer-sid remote is a SENTINEL, not a SID — the reduc

*(was `start/SKILL.md` L1062-1072)*

     (`--reducer-sid remote` is a SENTINEL, not a SID — the reducer's SID is
     UNOBTAINABLE from this machine (`running-session-id` is `sync_tier:
     machine_local`, and the DDB row stores a runner-token, not a SID), and the CLI
     rejects any other value rather than let a caller invent a plausible one. It
     forces `fork_needed=true`, bypassing the local `running-session-id` read that
     would otherwise decide FALSE here — CW-pre just deleted that file, and a worker
     box never has one — and records `reducer_sid: remote`, `remote_body: true`,
     `machine_id`, `forked_wm_hash`. The fork copies the fresh WM to
     `sessions/$SID/working-memory.yaml` plus `forked-wm-baseline.yaml`; the
     `BODY_WM_PATH` hook then routes every `wm-*.sh` write to the fork by the existing
     unchanged mechanism.)

## including guard-165 — SID/agent/mode travel by ENV, never

*(was `start/SKILL.md` L1075-1076)*

     including guard-165 — SID/agent/mode travel by ENV, never interpolated into the
     Python source text):

## running-session-id/runner-token, do NOT touch the DDB clai

*(was `start/SKILL.md` L1081-1083)*

     `running-session-id`/`runner-token`, do NOT touch the DDB claim, and do NOT run
     `/boot`. State after CW3: `agent-state` IDLE, no reducer-shaped files on this
     box, DDB untouched.

## (F4 reorder, 2026-05-20: moved BEFORE the state-set RUNNIN

*(was `start/SKILL.md` L1130-1132)*

     (F4 reorder, 2026-05-20: moved BEFORE the state-set RUNNING below so the
     critical section between RUNNING and /boot is truly empty. These are
     pure-Bash cleanups of stale per-session files; safe to run at IDLE.)

## The UNINITIALIZED first-boot ceremony (Phase A-0 transplan

*(was `start/SKILL.md` L1191-1196)*

The UNINITIALIZED first-boot ceremony (Phase A-0 transplant detection, Phase A
agent-name binding, Phase B path configuration, the B0.5/C0.5 bootstrap gates,
and program.md setup) is extracted to an on-demand digest so this hot-path
SKILL.md stays lean: it runs ONLY on brand-new agent creation, never on the
common IDLE->RUNNING resume path, yet its ~627 lines were loaded into context on
EVERY /start invocation (the boot-footprint that g-115-1723 targets).

## (Phase A-0 - A - B - B0.5 - C0 - C0.5 - program.md), THEN

*(was `start/SKILL.md` L1199-1200)*

(Phase A-0 -> A -> B -> B0.5 -> C0 -> C0.5 -> program.md), THEN continue to the
Phase C dispatch immediately below for the mode-specific first-boot flow.

## Phase C then adapts based on mode. The three mode-specific

*(was `start/SKILL.md` L1204-1209)*

Phase C then adapts based on mode. The three mode-specific first-boot flows
(Reader / Assistant / Autonomous, steps C1.9-C11) are extracted to an on-demand
digest so this hot-path SKILL.md stays lean: they run ONLY during UNINITIALIZED
first-boot, never on the common IDLE->RUNNING resume path, yet their ~530 lines
were loaded into context on EVERY /start invocation (the boot-footprint that
g-115-1723 targets).

## confirmed above (Reader / Assistant / Autonomous). That di

*(was `start/SKILL.md` L1212-1213)*

confirmed above** (Reader / Assistant / Autonomous). That digest holds the full
C1.9-C11 first-boot sequence for all three modes.

## - Calls: /boot (autonomous mode), /prime (all modes during

*(was `start/SKILL.md` L1216-1217)*

- Calls: /boot (autonomous mode), /prime (all modes during init; reader/assistant resume)
- Called by: User only. NEVER by Claude.


## Why the first heartbeat waits for the DDB acquire

*(was `start/SKILL.md` L719-726; moved verbatim by g-115-10839 to bring the skill under the 65,536 B injection ceiling)*

   - (DDB-heartbeat ordering, g-328-31: the first `heartbeat-tick.sh` — which
     under own-cloud ALSO fires the DDB `runner-claim.sh heartbeat` — is
     deliberately DEFERRED to AFTER the DDB acquire below (and before the RUNNING
     flip). A DDB heartbeat run BEFORE the acquire, using a leftover runner-token
     from a prior session whose release did not confirm, refreshes a STALE claim's
     `heartbeat_at` and defeats the acquire's §5 stale-lock-break — pinning this
     /start at rc=4 on its own stale claim. See the heartbeat-tick step just
     before `session-state-set.sh RUNNING` below.)

## Why the runner claim is a triple-write with a framework-owned token

*(was `start/SKILL.md` L728-728; moved verbatim by g-115-10839 to bring the skill under the 65,536 B injection ceiling)*

     (Canonical runner-claim: writes THREE files atomically — `running-session-id`, `latest-session-id`, and `runner-token` — into `agents/<agent-name>/session/`. The Phase 2.5.D `agents/` parent prefix MUST be in the heredoc path; without it, the writes land at `agents/<agent-name>/session/` at PROJECT_ROOT (the 2026-05-19 bravo/ cruft incident — the L1 hook only gates Write/Edit, not Bash heredoc writes, so a missing `agents/` prefix silently creates a directory at the wrong root). The first two files hold the Claude Code SID (routing identity used by stop-hook). The third is a FRAMEWORK-OWNED UUID4 (uniqueness identity) — protects against Claude Code reusing a session_id across windows via `claude --continue` / `--resume`. With the token, every BLOCK and watchdog event records the runner-instance identity, so a SID-collision shows up as "same SID, different runner-token" in `core/logs/stop-hook.log` and watchdog events instead of silent corruption. The 2026-05-12 cross-binding incident was invisible to forensics without this signal. DO NOT split these writes into separate Bash commands — the triple-write is the atomic unit. DO NOT remove the `RUNNER_TOKEN_GEN_FAILED` halt; without a token, the loop runs with no uniqueness anchor. Per rb-323/guard-403, observer-paired signals MUST be seeded BEFORE the state-set RUNNING below — same race rb-323 identified for heartbeat-tick. If RUNNER_TOKEN_GEN_FAILED here, state stays IDLE (clean retry); if state-set ran first, state would be RUNNING with no SID files and Path B would have to recover.)

## Why current_focus is cleared, and why below the acquire

*(was `start/SKILL.md` L806-818; moved verbatim by g-115-10839 to bring the skill under the 65,536 B injection ceiling)*

     (Clear stale current_focus from the previous session's shutdown — without this,
     a partner reading team-state.yaml sees the prior session's "session ended" or
     stale focus value indefinitely. Convention coordination.md:275 is retrospective
     ("set on completion"), so we can't write a prospective "starting" — clearing to
     "" is the convention-aligned signal of "no completion yet this session". The
     first aspirations-state-update or aspirations-consolidate write populates
     current_focus with the first real completion. Fail-open with `|| true` so a
     team-state write failure never blocks the RUNNING transition — stderr is NOT
     suppressed, so write errors surface. ORDERING (g-115-4653): this is the FIRST
     shared/synced write in the sequence and MUST stay below the DDB acquire above.
     It was previously the first write of the whole autonomous branch, so an rc=4
     refusal blanked the LIVE owning box's focus before ever discovering it had lost
     the claim.)

## Why the first heartbeat sits between the acquire and the RUNNING flip

*(was `start/SKILL.md` L866-885; moved verbatim by g-115-10839 to bring the skill under the 65,536 B injection ceiling)*

     (FIRST heartbeat — seeds `runner-heartbeat` mtime AND stamps team-state
     `last_active` NOW, and under own-cloud ALSO fires the DDB
     `runner-claim.sh heartbeat`. MOVED here from before the triple-write
     (g-328-31) so it runs AFTER the DDB acquire above and BEFORE the RUNNING
     flip below. Ordering rationale: the DDB heartbeat MUST NOT precede the
     acquire — a heartbeat carrying a leftover token from a prior session
     refreshes a STALE claim's `heartbeat_at`, defeating the acquire's §5
     stale-lock-break and pinning the next /start at rc=4 (stale-self-claim).
     Acquiring first lets §5 reclaim the genuinely-stale claim; THIS heartbeat
     then refreshes the just-acquired claim with the fresh token from the
     triple-write. Still precedes the RUNNING transition to close the
     observer-probe race (state=RUNNING with a stale heartbeat/last_active) per
     rb-323/guard-403 — both observer-paired signals (heartbeat here, triple-write
     above) are seeded before the flip. `--bypass-state` is REQUIRED because state
     is still IDLE here; the gate in `heartbeat-tick.sh` refuses bare ticks against
     IDLE (the `heartbeat_without_running` desync class, alpha 2026-05-13
     cbb27ab3). DO NOT add a separate `team-state-update.sh ... last_active` line;
     it duplicates the write heartbeat-tick just performed. On an acquire HALT
     (rc=4) above, this heartbeat never runs — a failed acquire leaves no
     heartbeat side-effect, which is the point.)

## (State flip — observable to /stop, recovery-gate, partner agents.

*(was `start/SKILL.md` L896 — relocated 2026-09-25, same reason as above)*

     (State flip — observable to /stop, recovery-gate, partner agents. Per rb-323/guard-403, this MUST be the last write in the RUNNING-claim sequence: every observer-paired signal — heartbeat above, triple-write directly above — is seeded first, so the invariant "state=RUNNING implies fresh heartbeat AND non-empty SID files" holds from the transition moment. Script-enforced since 2026-08-29 (rb-9643): `session.py` REFUSES `RUNNING` — exit 1, `REJECTED: … running-session-id` — when `running-session-id` is missing/empty or names another session than `$MIND_SID`; a refusal means the triple-write above was skipped, so go back and run it, never write `agent-state` by hand.)
