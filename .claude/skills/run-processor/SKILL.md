---
name: run-processor
forged: true
forged_by: alpha
forged_date: "2026-03-28"
description: "Runs the offline AyoAI NPC-intelligence Processor against live game-session data on the GPU EC2 (llama-server on port 8082) to regenerate aspirations, behavior trees, memories, and experiments. A 3-7 hour background job with sub-commands launch / monitor / collect / status / stop / recover. Use whenever fresh cell data needs to be compiled into NPC intelligence updates, the pipeline requires a new Processor output before the next game session, or the user says \"run processor\" / \"regenerate BTs\"."
user-invocable: false
triggers: [processor, processor-run, llama-server-8082, gpu-processor, behavior-tree-generation, npc-intelligence-processor, offline-processor]
sub_commands: [launch, monitor, collect, status, stop, recover]
tools_used: [Bash]
companion_scripts: [world/scripts/processor-run.sh, core/scripts/background-jobs.sh]
conventions: [secrets, infrastructure]
minimum_mode: autonomous
revision_id: "skill-bootstrap-run-processor-a43686"
previous_revision_id: null
---

# /run-processor — Offline NPC Intelligence Pipeline

Run the Environment Processor against live game session data. This is the offline learning
loop that makes NPCs smarter between sessions: generates aspirations, behavior trees,
memories, and experiments from raw game data.

**This runs LOCALLY on this machine (not on AWS).** Requires RTX 4070 GPU (12GB VRAM).
Takes ~1 hour per run in server mode (~430 LLM calls total across all characters).

**IMPORTANT: Server mode is REQUIRED on RTX 4070 with Qwen2.5-14B.** In-process mode
(llama-cpp-python) uses F16 KV cache (Q4_0 crashes the library), which overflows 12GB VRAM.
Use `start-server.sh` in the Processor repo to launch llama-server.exe first, then
`processor-run.sh run-server` instead of `run-and-notify`. See guard-103.

## Companion Script

`world/scripts/processor-run.sh <sub-command> [args]`

## Architecture

```
EFS (live game data) → SSH download → Local GPU processing → SSH deploy back to EFS
                                          ↓
                              4 phases × N characters × ~50 LLM calls each
                              Phase 1: Aspirations (evolve, gap analysis, MNLI dedup)
                              Phase 2: Behavior Trees (BFS sampling, Reflexion)
                              Phase 3: Memory (episodes, reflections, forgetting)
                              Phase 4: Experiments (hypotheses, correlations, rewards)
```

## Prerequisites

- GPU available (RTX 4070 / 12GB VRAM) — check with `processor-run.sh gpu-check`
- CUDA-enabled llama-cpp-python — check with `processor-run.sh cuda-check`
- EFS SSH access working (same creds as access-efs-data skill)
- Game session data exists on EFS (at least one completed server session)
- Models available: GGUF model file, bart-large-mnli, all-MiniLM-L6-v2

## CUDA / GPU Setup

**Critical**: The default `pip install llama-cpp-python` installs a **CPU-only** wheel. There is
no error — inference silently runs on CPU at ~20x slower speed (19+ min per call vs ~90s on GPU).

**Detection**: If LLM calls take >5 min each, or `processor-run.sh cuda-check` reports "MISSING",
the CUDA wheel is not installed.

**Fix**: Install the prebuilt CUDA 12.3 wheel (only up to v0.3.4 for Windows cp312):
```
powershell.exe -Command "py -m pip install llama-cpp-python==0.3.4 --force-reinstall --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu123"
```

**Startup guard**: `llm_service.py` raises `RuntimeError` if `n_gpu_layers != 0` but no `ggml-cuda`
library is found. The error message includes the exact reinstall command.

**Building from source**: Requires CUDA Toolkit 12.4+ and compatible MSVC. CUDA 12.3 + MSVC 14.44
are incompatible (both sides reject each other). Use the prebuilt wheel instead.

## External Server Mode (REQUIRED for RTX 4070)

Server mode is the **only viable** inference path for Qwen2.5-14B on RTX 4070 (12GB VRAM).
In-process mode uses F16 KV cache (Q4_0 crashes llama-cpp-python 0.3.4), which overflows
VRAM with Windows system apps running. See guard-103.

1. Start server: `cd C:/ZakNoCloud/GitHub/Ayoai/Ayoai-Environment-Processor && bash start-server.sh`
   - Uses: `-ngl 99 -c 16384 -np 1 -fa on --cache-type-k q4_0 --cache-type-v q4_0 -ub 256`
   - WARNING: Do NOT use `-c 32768` or `-np 4` — exceeds VRAM with system apps (0.12 tok/s)
2. Run: `processor-run.sh run-server <server-key> [url]` (default url: `http://127.0.0.1:8082`)

`--model-path` is still required for run summary tracking even in server mode.

**Performance**: 462 tok/s avg, ~1 hour per full run (vs 20h+ in-process). See run proc-1775603779.

## Commands

### Discover Available Sessions
```bash
# List server sessions on EFS for NPCDemoExperiment
Bash: world/scripts/processor-run.sh list-sessions

# Returns: session keys (timestamp_id format), sorted newest first
```

### Run the Processor
```bash
# Run against a specific server session (background, no callback)
Bash: world/scripts/processor-run.sh run <server-key>

# Run + email notification on completion (preferred for autonomous mode)
Bash: world/scripts/processor-run.sh run-and-notify <server-key>

# Run against the most recent session
Bash: world/scripts/processor-run.sh run-latest

# Run with external llama-server (REQUIRED for RTX 4070 — see guard-103)
Bash: world/scripts/processor-run.sh run-server <server-key> [url]

# Dry run (validate inputs, don't generate)
Bash: world/scripts/processor-run.sh dry-run <server-key>
```

### Monitor Progress
```bash
# Structured progress report (phase, character, LLM calls, errors, elapsed)
Bash: world/scripts/processor-run.sh status

# Raw log tail (last 20 JSON lines)
Bash: world/scripts/processor-run.sh logs
```

**Progress estimation**: ~70 LLM calls per character, ~430 total for 6 chars. Each phase
processes all characters sequentially. Server mode: ~1 hour. In-process mode: 20+ hours (DO NOT USE on RTX 4070).

**Key log markers** (in `run_log.jsonl`):
- Phase start: `=== Phase N: Name (M characters) ===`
- Character: `Evolving aspirations for CharKey`
- Each LLM call: `LLM complete: template=X character=Y tokens=Z latency=Wms`
- Completion: `Pipeline Complete`, `Run finished: status=X llm_calls=N`

**`run_summary.json` only exists after completion** — do not check it while running.
For detailed log analysis, open a session in the Processor repo and use its `/status` command.

### Verify Results
```bash
# Detailed output file inventory with line counts and per-character breakdown
Bash: world/scripts/processor-run.sh check-output

# Run summary (duration, LLM calls, tokens, error rate) — only after completion
Bash: world/scripts/processor-run.sh summary
```

### GPU & CUDA Health
```bash
# Check GPU availability and VRAM
Bash: world/scripts/processor-run.sh gpu-check

# Check CUDA support in llama-cpp-python (ggml-cuda library presence)
Bash: world/scripts/processor-run.sh cuda-check
```

## Execution Modes

This skill operates in three modes based on the goal context. The aspirations loop
invokes it via Phase 4 — the skill detects which mode to use automatically.

### Mode Detection

```
IF goal.metadata has "monitor_job_id":
    → MONITOR mode
ELIF goal.title starts with "Monitor:":
    → MONITOR mode
ELSE:
    → LAUNCH mode
```

COLLECT is not a standalone mode — it runs as the final step of MONITOR when a job completes.

---

### LAUNCH Mode (default)

Pre-flight checks + launch + register background job + create monitor goal.
**Returns immediately** — the agent resumes other work.

```
1. Discover: Bash: world/scripts/processor-run.sh list-sessions
   → Pick the target server session (latest, or as specified by goal)

2. Pre-check: Bash: world/scripts/processor-run.sh gpu-check
   → IF GPU unavailable or VRAM critical: FAIL (create blocker)

3. CUDA verify: Bash: world/scripts/processor-run.sh cuda-check
   → IF CUDA missing: FAIL (log fix instructions, create blocker)

4. Start server (idempotent — rb-571 stability-oracle pattern):
   Bash: cd C:/ZakNoCloud/GitHub/Ayoai/Ayoai-Environment-Processor && bash start-server.sh --ensure-running
   → start-server.sh handles all cases internally: skip-if-healthy (with body
     validation — '"status":"ok"' grep, NOT bare curl -sf — see rb-571),
     otherwise nohup-launch + poll /health for ≤90s. Exits 0 only when /health
     returns valid JSON containing '"status":"ok"'. The conditional probe in
     SKILL.md was removed deliberately: the iter-29 false-positive (proc-
     1777215717293) was curl exit 0 with empty body, which only an in-script
     body-validating check can catch.
   → If exit non-0: FAIL (create blocker for GPU/VRAM issue — likely VRAM saturation
     or model file missing; check $SCRIPT_DIR/llama-server.log for details)

4a. Launch: Bash: world/scripts/processor-run.sh run-server <server-key> http://127.0.0.1:8082
   → Process starts in background using external llama-server for inference.

4.5. Speed gate (tokens/second check):
   Wait ~3 min for the first LLM calls to appear in run_log.jsonl:
     Bash: sleep 180
   Then check throughput:
     Bash: world/scripts/processor-run.sh speed-check 20 5
   → IF verdict == "fail" (avg tok/s below 20):
       Output: "▸ SPEED GATE FAILED: {avg_tps} tok/s (threshold: 20)"
       Bash: world/scripts/processor-run.sh stop
       Bash: world/scripts/processor-run.sh gpu-recover
       Bash: world/scripts/processor-run.sh gpu-check
       → IF GPU healthy after recovery: restart from step 4 (re-launch)
       → IF GPU still degraded: create blocker "Unblock: GPU VRAM stuck — reboot required"
       → FAIL (do not continue to step 5)
   → IF verdict == "pass": continue normally
   → IF verdict == "unknown" (no LLM calls yet): wait 2 more min, retry once

5. Read PID: Bash: cat world/scripts/.processor-run.pid
   → PROC_PID = <pid from file>

6. Generate job ID from the run_dir created by the running processor:
   By this step, Step 4.5's speed gate has confirmed the processor is producing
   tokens, so output/runs/<run_dir>/ exists and is the newest run directory.
   Aligning JOB_ID with run_dir means the goal title is human-findable on disk
   and EFS — anyone reading "Monitor: processor run proc-1777091341578" can
   locate the matching directory without a separate lookup. (g-248-71)
   LATEST_RUN_DIR=$(basename $(ls -td "C:/ZakNoCloud/GitHub/Ayoai/Ayoai-Environment-Processor/output/runs/"*/ 2>/dev/null | head -1))
   IF [ -z "$LATEST_RUN_DIR" ]: FAIL with "Cannot align JOB_ID — no run_dir under output/runs/. Investigate processor startup."
   JOB_ID="proc-$LATEST_RUN_DIR"

7. Create recurring monitor goal (add to SAME aspiration as the launch goal):
   echo '<goal JSON>' | Bash: aspirations-add-goal.sh --source {source} <aspiration-id>
   Goal JSON:
   {
     "title": "Monitor: processor run <JOB_ID>",
     "status": "pending",
     "priority": "HIGH",
     "recurring": true,
     "interval_hours": 0.5,
     "skill": "/run-processor",
     "category": "infrastructure",
     "description": "Periodic check on background processor job. Auto-completes when processor finishes.",
     "origin_signal": "recurring_cadence:processor-monitor-<JOB_ID>",
     "metadata": {
       "monitor_job_id": "<JOB_ID>",
       "parent_goal_id": "<current_goal_id>",
       "server_key": "<server-key>"
     }
   }
   → MONITOR_GOAL_ID = <id assigned by aspirations-add-goal.sh>

8. Register background job (after goal creation so monitor_goal_id is known).

   The --output-artifacts JSON is the 0-byte-file defense: when the PID dies
   and check-complete exits 0, the framework also verifies these files exist,
   are non-empty, and have parseable first lines. Any failure flips status
   from "completed" to "failed" with an output_check_failures payload that
   MONITOR then surfaces as an Investigate goal. Drops a phantom "success"
   from a crashed merge phase into a loud failure (rb-061, rb-085, guard-156).

   Bash: background-jobs.sh register \
     --id "$JOB_ID" \
     --type processor \
     --goal "<current_goal_id>" \
     --pid "$PROC_PID" \
     --monitor-goal "$MONITOR_GOAL_ID" \
     --completion-check "world/scripts/processor-run.sh check-complete" \
     --metadata '{"server_key":"<server-key>"}' \
     --output-artifacts '[
       {"path":"C:/ZakNoCloud/GitHub/Ayoai/Ayoai-Environment-Processor/output/ConsolidatedMemory.jsonl","min_bytes":1,"format":"jsonl"},
       {"path":"C:/ZakNoCloud/GitHub/Ayoai/Ayoai-Environment-Processor/output/BehaviorTrees.jsonl","min_bytes":1,"format":"jsonl"},
       {"path":"C:/ZakNoCloud/GitHub/Ayoai/Ayoai-Environment-Processor/output/Aspirations.jsonl","min_bytes":1,"format":"jsonl"}
     ]'

9. Return:
   outcome: "Processor launched as background job <JOB_ID>"
   outcome_class: "routine"
   # Launch goal is marked completed by the aspirations loop.
   # Monitor goal takes over periodic checking.
```

---

### MONITOR Mode (recurring goal — check job status)

Invoked every ~30 minutes when the recurring monitor goal is selected.
Lightweight: one background-jobs check per invocation.

```
1. Extract job_id from goal metadata: monitor_job_id
   IF no monitor_job_id found:
       Log warning: "Monitor goal missing monitor_job_id metadata"
       → Set goal: recurring=false, mark completed
       Return: {outcome: "orphaned-monitor-cleaned", outcome_class: "routine"}

2. Check job status:
   Bash: background-jobs.sh check --id <job_id>
   → Parse JSON output

3. Branch on status:
```

**IF status == "running":**
```
   Output: "▸ Processor still running ({elapsed_hours}h elapsed)"
   # Optional progress detail (only if elapsed > 1h to avoid noise):
   IF elapsed_hours > 1:
       Bash: world/scripts/processor-run.sh status
   Return: {outcome: "still-running", outcome_class: "routine"}
   # Recurring goal resets to pending after interval_hours (Phase 0).
   # Goal selector will re-select it ~30 min later.
```

**IF status == "completed":**
```
   Output: "▸ Processor completed after {elapsed_hours}h!"
   → Execute COLLECT steps (see below)
   Bash: background-jobs.sh deregister --id <job_id>
   Bash: aspirations-update-goal.sh --source {source} <goal_id> recurring false
   # Clear recurring-shape fields — without this the goal lands in the
   # shape-corruption pattern (recurring=false + interval_hours + lastAchievedAt)
   # that aspirations.py find_shape_recurring_corrupted now recovers (rb-295,
   # g-001-138). Clearing on termination prevents the false-positive recovery.
   Bash: aspirations-update-goal.sh --source {source} <goal_id> interval_hours null
   Bash: aspirations-update-goal.sh --source {source} <goal_id> lastAchievedAt null
   Return: {outcome: "collected", outcome_class: "deep"}
```

**IF status == "failed" or "unknown":**
```
   Output: "▸ Processor FAILED after {elapsed_hours}h"
   Bash: world/scripts/processor-run.sh status    # Get failure details
   Bash: world/scripts/processor-run.sh logs       # Get last log lines

   # Read output_check_failures from the check result (if the output-sanity
   # gate overrode a completed status to failed — this is the 0-byte-file
   # defense path; see session-state.md "Output-Sanity Gate").
   # The failure list is in the check JSON under output_check_failures;
   # include it verbatim in the investigation goal description so the
   # follow-up has the exact diagnostic payload.
   FAIL_KIND = "output-gate" IF check_result.output_check_failures ELSE "runtime"
   FAIL_DETAIL = check_result.output_check_failures OR "see processor-run.sh status/logs"

   Bash: background-jobs.sh deregister --id <job_id>
   Bash: aspirations-update-goal.sh --source {source} <goal_id> recurring false
   Bash: aspirations-update-goal.sh --source {source} <goal_id> interval_hours null
   Bash: aspirations-update-goal.sh --source {source} <goal_id> lastAchievedAt null
   # Create investigation goal — embed FAIL_KIND + FAIL_DETAIL in description
   # so the next executor does not re-discover what the gate already found.
   echo '{
     "title":"Investigate: processor run <job_id> failed (<FAIL_KIND>)",
     "description":"Kind: <FAIL_KIND>. Details: <FAIL_DETAIL>. Job elapsed: <elapsed_hours>h.",
     "status":"pending",
     "priority":"HIGH",
     "origin_signal":"investigate:processor-<job_id>-<FAIL_KIND>"
   }' | Bash: aspirations-add-goal.sh --source {source} <aspiration-id>
   Return: {outcome: "failed", outcome_class: "deep"}
```

**IF status == "not_found"** (background-jobs.yaml missing or job deregistered):
```
   Output: "▸ Monitor goal has no matching background job — cleaning up"
   Bash: aspirations-update-goal.sh --source {source} <goal_id> recurring false
   Bash: aspirations-update-goal.sh --source {source} <goal_id> interval_hours null
   Bash: aspirations-update-goal.sh --source {source} <goal_id> lastAchievedAt null
   Return: {outcome: "orphaned-monitor-cleaned", outcome_class: "routine"}
```

---

### COLLECT Steps (invoked by MONITOR on completion)

Post-completion verification and knowledge update.

```
1. Verify output: Bash: world/scripts/processor-run.sh check-output
   → Log output file inventory (merged files, per-character breakdown)

2. Get summary: Bash: world/scripts/processor-run.sh summary
   → Parse: duration, LLM calls, tokens, error rate, trees generated

3. Analyze results:
   - Error rate > 50%? → Flag for investigation
   - Characters processed count matches expected?
   - Deployment gates passed? (merge + error rate + validation)

4. Update knowledge tree (if relevant node exists):
   → Edit the processor/environment-processing node with latest run stats

5. Post findings:
   echo 'Processor run completed: {duration}h, {llm_calls} LLM calls, {errors} errors' \
       | Bash: board-post.sh --channel findings
```

---

## Completion Callback (Email — supplementary)

`run-and-notify` also sends an email via `email-send.sh` when the processor finishes.
This is supplementary to the monitor goal mechanism — the monitor goal is the primary
detection method. The email is a backup notification for the user.

## Output Directory

```
output/
  runs/{timestamp}/
    run_log.jsonl              # Event log (~850 lines for full run, JSON per line)
    run_summary.json           # Stats — only written at run completion
    llm_calls/                 # Per-call JSON files (only with --verbose)
  RunManifest.jsonl            # Append-only history of all runs (for cross-run comparison)
  Aspirations.jsonl            # Merged output (all characters)
  BehaviorTrees.jsonl
  ConsolidatedMemory.jsonl
  PrivateNotes.jsonl
  {charKey}_Aspirations.jsonl  # Per-character breakdown files
  {charKey}_CellArchive.jsonl
  {charKey}_Experiments.jsonl
  {charKey}_ExtendedStateFields.json
  {charKey}_ConsolidatedMemory.jsonl
  {charKey}_RetainedMemories.jsonl
```

Note: `_deploy/` is **ephemeral** — deleted by main.py after EFS upload. Do not expect it to persist.

## Operational Quirks

1. **Runs locally, not on AWS.** The Processor is at `C:/ZakNoCloud/GitHub/Ayoai/Ayoai-Environment-Processor/`.
   No CI/CD deployment. No cloud instance. It runs on the developer's machine with a local GPU.

2. **Uses `py` launcher, not `python`.** On Windows, `python` resolves to the Store stub.
   All commands use `powershell.exe -Command "cd '...'; py ..."` to get the real interpreter.

3. **Separate .env.local.** The Processor has its own `.env.local` at its repo root with the same
   4 SSH keys (EFS_SSH_HOST, EFS_SSH_KEY_PATH, EFS_SSH_USER, REMOTE_DATA_PATH). These are the
   same values as Ayoai-Mind's `.env.local` but in a separate file.

4. **GPU memory is fragile.** Any process kill on Windows orphans CUDA contexts (there is no
   graceful shutdown for console apps — `TerminateProcess()` always runs). After killing the
   Processor, always run `processor-run.sh gpu-recover` to reset the GPU driver and reclaim
   VRAM. Only reboot if `gpu-recover` fails.

5. **Deployment has 3 safety gates.** Output only deploys to EFS if:
   - Merge write succeeded
   - Error rate <= 50%
   - Zero CRITICAL validation issues

6. **Task vocabulary.** NPCDemoExperiment supports 9 task types loaded dynamically from `Tasks.jsonl`:
   `moveTo`, `jump`, `speak`, `whisper`, `shout`, `wait`, `sit`, `examine`, `dance`.
   All generated artifacts should use diverse task selection from this vocabulary.

7. **Testing environment.** Account: `b1fb6520-c051-70df-ca6e-6ce15efd8d47`,
   Environment: `NPCDemoExperiment`.

## Failure Diagnosis

| Symptom | Cause | Fix |
|---------|-------|-----|
| `_ArrayMemoryError`, `Failed to create llama_context`, `WinError 0xe06d7363` | GPU memory exhausted or orphaned CUDA contexts | `processor-run.sh stop` then `processor-run.sh gpu-recover`. Reboot only if gpu-recover fails. |
| `JSON extraction failed` followed by retry | LLM output parsing failure | Recoverable — monitor retry count |
| CUDA `RuntimeError` at startup | CPU-only llama-cpp-python wheel | Reinstall with CUDA index (see CUDA section) |
| LLM calls >5 min each | Silent CPU fallback | Run `cuda-check`, fix CUDA install |
| No `run_summary.json` after process exits | Run crashed before completion | Check last ERROR entry in `run_log.jsonl` |
| `Pipeline failed with error` | Unhandled exception | Read the traceback in `run_log.jsonl` |
| Empty output files (0 lines) | Phase crashed before writing | Check which phase marker was last in log |

### STOP Mode

When the Processor needs to be terminated (stuck, crashed, or manual stop):

```
1. Bash: world/scripts/processor-run.sh stop
   → Calls TerminateProcess (immediate kill on Windows — no graceful shutdown for console apps)
   → Also stops llama-server if running
   → Cleans up PID files
   → VRAM will be orphaned — always run gpu-recover afterward
2. Deregister background job: Bash: background-jobs.sh deregister --id <job_id>
3. Disable recurring monitor goal if active
```

Use `stop` instead of `taskkill /F` for consistent PID file cleanup and llama-server handling.

### RECOVER Mode

When `gpu-check` shows CRITICAL after a crash (VRAM orphaned, no process running):

```
1. Bash: world/scripts/processor-run.sh stop         # Ensure nothing running
2. Bash: world/scripts/processor-run.sh gpu-recover   # Driver teardown + reinit
   → Screen flashes black ~5 seconds (normal)
   → Requires admin privileges
3. Bash: world/scripts/processor-run.sh gpu-check     # Verify >8GB free
4. IF gpu-recover failed (access denied or VRAM still low):
   → Create blocker goal: "Unblock: GPU VRAM stuck — machine reboot required"
   → Do NOT retry gpu-recover in a loop
Bash: echo "run-processor phase documented"
```

## Workflow Summary

The skill operates as a multi-mode async pipeline:

1. **LAUNCH** (Phase 4, ~2 min): Pre-flight → launch → register background job → create monitor goal → return to loop
2. **MONITOR** (recurring, ~30s each): Check job status → if done, COLLECT results → clean up
3. **STOP** (on-demand): Graceful termination with CUDA cleanup
4. **RECOVER** (after crash): GPU driver reset to reclaim orphaned VRAM

The agent does other productive work between monitor checks. A full processor run
(3-7 hours) consumes ~2 min of agent time for launch + ~30s per check + ~2 min for collection.

## Infra-Health Component

Component: `processor-gpu`. Probe: `processor-run.sh gpu-check`.

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action depends on phase: LAUNCH → `background-jobs.sh register`; MONITOR →
`background-jobs.sh check` or `aspirations-update-goal.sh`; COLLECT → `experience-add.sh`
or `aspirations-add-goal.sh` for follow-up work. Never end with a text summary.
