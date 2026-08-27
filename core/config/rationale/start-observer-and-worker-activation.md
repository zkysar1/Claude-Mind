# Rationale: /start — the RUNNING branch: observer sessions and worker-Body activation

Referenced from `.claude/skills/start/SKILL.md`. Extracted 2026-08-25 (g-115-7706):
that skill was 89,106 B against the 65,536 B on-demand injection ceiling, so roughly
its last 27% never reached the model at all. Every block below is VERBATIM — this was
a relocation, not a rewrite. The skill retains ALL procedure: its YAML front matter,
every `Bash:` line, every HALT/refusal branch, every `>` user-facing display line,
every bold directive, and every test-pinned literal. Only explanation moved here.

## The agent is in autonomous mode. This could mean another C

*(was `start/SKILL.md` L287-288)*

The agent is in autonomous mode. This could mean another Claude Code window is
actively running the loop, OR a previous session crashed/closed without `/stop`.

## Two scenarios produce RUNNING-on-disk

*(was `start/SKILL.md` L294-297)*

Two scenarios produce RUNNING-on-disk:
  1. **Live runner** — another Claude Code window is actively running the loop.
  2. **Zombie** — the previous session crashed without `/stop` and the state
     file is stale; no live runner exists.

## Gate fires when ALL SIX hold

*(was `start/SKILL.md` L302-308)*

Gate fires when ALL SIX hold:
  (1) `agent-state == RUNNING`            (already established by Step 1)
  (2) `heartbeat-stale.sh` returns `stale`
  (2.5) `runner-recent-block.sh` returns 1 (no BLOCK in last 5 min)
  (2.7) `execution-diary.jsonl` mtime older than 15 min (DIARY_STALE_MINUTES)
  (3) `stop-requested` is NOT set
  (4) `background-jobs.sh has-pending` exits 1 (no Tier-A registered job)

## At this point in /start, .active-agent-<SID has not been w

*(was `start/SKILL.md` L311-317)*

At this point in `/start`, `.active-agent-<SID>` has not been written yet
(that happens in the IDLE branch's Step 0). Without the prefix, the
PreToolUse hook cannot auto-inject MIND_AGENT, and `_paths.sh` falls
through to its no-agent path: `AGENT_DIR` is empty, all probes read from
bogus paths, and EVERY probe returns the auto-recovery-passing value
regardless of actual agent health. That would auto-recover live runners.
Same warning as Step 0.7 (lines 44-45).

## terminal activates as a Worker Body (asp-306 / g-306-73 on

*(was `start/SKILL.md` L324-325)*

terminal activates as a **Worker Body** (asp-306 / g-306-73 one-mind-two-bodies).
DO NOT auto-recover the reducer — that would clobber the active loop.

## Step 0-pre2 below; that guard's prose already claims to be

*(was `start/SKILL.md` L336-337)*

    Step 0-pre2 below; that guard's prose already claims to be mode-wide, but it
    is placed in the IDLE branch and so never reached this path).

## A SID that previously ran as a Worker Body still has its f

*(was `start/SKILL.md` L339-351)*

    A SID that previously ran as a Worker Body still has its fork file, because
    the fork survives wind-down BY DESIGN (0-pre2 explains why). Re-activating
    the SAME terminal as a worker destroys the earlier Body's unmerged
    divergence TWICE over, and both writes are silent:
      - W0.4 `body-manifest.sh write` is **idempotent on `body_state` — a
        re-write RESETS the Body to `active`** (`body-manifest.py`
        `write_manifest` docstring) and replaces `forked_wm_hash` with the new
        fork's. The crashed Body's state record is gone.
      - W1's `cp` is unconditional and overwrites the fork file itself.
    After both, `cleanup-stale-bindings.sh::_preserve_unmerged_body_wm` can no
    longer reclaim anything: it keys its skip on `body_state == merged`, so it
    happily stages a WM that is now the NEW fork, against a hash that no longer
    describes the divergence it was meant to protect.

## only side-effect-free when it fires before the destructive

*(was `start/SKILL.md` L354-355)*

    only side-effect-free when it fires before the destructive write (guard-1813);
    placed at W1 it would fire *after* W0.4 had already reset the manifest.

## Refuse rather than stage-then-re-fork, for 0-pre2's stated

*(was `start/SKILL.md` L362-367)*

    Refuse rather than stage-then-re-fork, for 0-pre2's stated reason: staging
    another Body's WM from inside a mislabeled session is a reducer-only
    operation performed by a non-reducer, with unmerged divergence as the stake.
    A fresh terminal has a fresh SID, no fork file, and reaches the same worker
    activation with nothing at risk — and the existing Body is reclaimed on its
    own schedule by the cleanup sweep's preserve path.

## (Records this terminal as a Worker Body. The Reducer's run

*(was `start/SKILL.md` L383-385)*

    (Records this terminal as a Worker Body. The Reducer's `running-session-id` is
    already on disk — body-manifest.sh reads it to verify this session is NOT the
    reducer. Fail-open — must never block the bind.)

## (The bash-agent-inject.py PreToolUse[Bash] hook (Phase 1A

*(was `start/SKILL.md` L391-398)*

    (The `bash-agent-inject.py` PreToolUse[Bash] hook (Phase 1A, g-306-68) detects
    this forked file and exports `BODY_WM_PATH` pointing to it on every Bash call,
    so all `wm-*.sh` writes land in the forked copy, never the canonical agent-wide
    WM. Reducers never have a per-SID WM file, so `BODY_WM_PATH` is never injected
    for them — backward-compat preserved. Merged back into the Reducer's WM at
    `aspirations-consolidate` Step -1 via `body-merge.py`. If the fork fails, the
    worker proceeds with an empty WM — degraded but operational; body-merge Step -1
    is a no-op for an unforked worker.)

## Exit-code semantics (matches recovery-gate.sh Cond 2.5)

*(was `start/SKILL.md` L421-424)*

Exit-code semantics (matches recovery-gate.sh Cond 2.5):
- `runner-recent-block.sh`: 0=recent BLOCK present (alive), 1=none in last 5 min, 2+=script error.
  Treat `recent_block_rc != 1` as "hold back" (conservative — same pattern as
  Cond 4 `bg_jobs_rc != 1`; script errors must NOT stomp a possibly-live runner).

## Exit-code semantics (matches recovery-gate.sh)

*(was `start/SKILL.md` L435-439)*

Exit-code semantics (matches recovery-gate.sh):
- `session-signal-exists.sh stop-requested`: 0=signal SET, 1=signal absent.
- `background-jobs.sh has-pending`: 0=jobs PRESENT, 1=none, 2+=script error.
  Treat `bg_jobs_rc != 1` as "hold back" (conservative — script errors
  must not trigger recovery).

## On success, output

*(was `start/SKILL.md` L483-486)*

  On success, output:
  "Auto-recovered crashed runner session for <agent-name> (heartbeat stale, no
   pending obligations, no graceful stop in flight). Cleared stale signals and
   session files (manifest-driven). Proceeding with normal start."

## The observer session coexists with the autonomous loop. It

*(was `start/SKILL.md` L516-517)*

The observer session coexists with the autonomous loop. It does NOT write to
agent-state, agent-mode, persona-active, or running-session-id.

## 0-pre2 below; 0-pre2's own text says "Reader/assistant bin

*(was `start/SKILL.md` L520-524)*

   0-pre2 below; 0-pre2's own text says "Reader/assistant binds inherit the same
   mislabel, so the refusal is mode-wide", but it sits in the IDLE branch and a
   reader/assistant bind taken while the agent is RUNNING never reached it.
   guard-530 is this exact shape: verify a per-session predicate against EVERY
   session mode it can encounter.)

## initial status=active record so this observer session is v

*(was `start/SKILL.md` L578-585)*

   initial `status=active` record so this observer session is visible in the
   live-sessions view BEFORE it closes (the close is WP2 in /stop's IDLE branch).
   World is already configured here (the agent is RUNNING), so WORLD_DIR resolves
   and the record lands at world/telemetry/session-records/<agent-name>/$MIND_SID.json.
   write_open is idempotent (returns without clobbering if the record exists) and
   never raises. `<target-mode>` is the observer mode (reader/assistant). guard-165:
   SID/agent/mode via ENV, python source single-quoted. `py -3` (Bash-tool context).
   Fire-and-forget (|| true) — telemetry must never block the bind.

