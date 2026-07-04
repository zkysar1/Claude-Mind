Phase C then adapts based on mode:

### Phase C for Reader Mode (simplified)

C2. AskUserQuestion:
   ```
   Setting up reader mode — read-only access to domain knowledge.

   Optional: Tell me who this agent is — its specific role and perspective.
   This helps me contextualize answers. Or say "skip" to use just The Program
   for context.
   ```

C3. If user provided an identity (not "skip"), write `agents/<agent>/self.md`:
   ```markdown
   ---
   created: "{today}"
   last_updated: "{today}"
   last_update_trigger: "initial_creation"
   source: "user"
   ---

   # Self

   {parsed Self content}
   ```

C4. Set mode and state:
   - Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-mode-set.sh reader`

     **HALT ON NON-ZERO EXIT (g-115-1032, 2026-05-21)** — if
     `session-mode-set.sh` exits non-zero, STOP. Do NOT proceed to the
     session-state-set.sh write below. Mode-set failure means the on-disk
     mode signal was NOT written; agent will fall back to the reader disk
     default per CLAUDE.md "Mode System" — that happens to match the
     intended `reader` here, but the asymmetric assistant/autonomous C8/C9.9
     siblings would silently land in reader. Halt for the contract
     uniformity; the session-state-set.sh below would also be misleading
     if mode-set silently failed. Display:
     > Cannot initialize agent `<agent-name>` in reader mode
     > (session-mode-set.sh exited non-zero). Investigate stderr above and
     > retry `/start <agent-name> --mode reader`.
   - Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-state-set.sh IDLE`

     **HALT ON NON-ZERO EXIT (G2, 2026-05-20)** — if `session-state-set.sh`
     exits non-zero, STOP. Do NOT proceed to C5/C6/C7. State remains
     UNINITIALIZED; the persona-set below is harmless. Display:
     > Cannot initialize agent `<agent-name>` in reader mode
     > (session-state-set.sh exited non-zero). Investigate stderr above and
     > retry `/start <agent-name> --mode reader`. Without this halt, C7's
     > "Agent initialized in reader mode" message would lie about success.
   - Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-persona-set.sh true`
   - Seed session_start: Bash: `date +%Y-%m-%dT%H:%M:%S | MIND_AGENT=<agent-name> bash core/scripts/wm-set.sh session_start`
     (Consumed by `session-artifacts-count.sh` → productivity-stop-gate.
     Helper exits non-zero if unset, which makes the gate treat total
     artifacts as 0. Seed on every session entry — IDLE-branch reader
     step 3 / assistant step 3 / autonomous step 3 all do the same.)

     **MIND_AGENT prefix rationale (G1, 2026-05-20)** — the three state setters
     above carry the explicit `MIND_AGENT=<agent-name>` prefix for the same
     belt-and-suspenders reason `wm-set.sh` does (PreToolUse hook cold-start
     race). The IDLE-branch reader/assistant variants (under the IDLE section
     above — Step 3 of the IDLE flow) already use this prefix consistently;
     the UNINITIALIZED variants now match. (H2, 2026-05-20: tightened from
     "Step 3 above" — the UNINITIALIZED branch has no Step 3 of its own.)

C5. Invoke `/prime` (reader context — pass `--read-only` to retrieve.sh)

C6. Load mode instructions: Read `core/config/modes/reader.md`

C7. Output: "Agent initialized in reader mode. I have access to all accumulated knowledge. Ask me anything."

### Phase C for Assistant Mode

**C1.9. BOOTSTRAP IDENTITY GATE — applies here too.** Assistant mode writes
`agents/<agent>/self.md` from user input exactly as Autonomous does. Before C2,
apply the full C1.9 gate documented under "### Phase C for Autonomous Mode":
check for a user-staged spec and use it verbatim; NEVER derive Self from The
Program / sibling self.md / inference; Decision-Authority + guard-380 do NOT
authorize bootstrap fabrication or skipping C5; C2→C5 explicit confirmation
is mandatory before any self.md / curriculum write. Assistant never flips to
RUNNING, so the stop-hook trap does not apply — but a fabricated Self is just
as wrong in assistant mode, and the no-inference + explicit-confirmation
rules are identical.

C2. Display the identity prompt:

   ```
   Now I need a few things from you:

   1. **My Self** — This is the agent's identity. It tells me WHO I am
   and WHAT my role is. This is separate from The Program (the world's
   shared purpose) — Self is about this specific agent.

   Examples:
   - "You are a QA engineer for Acme Corp."
   - "You are a personal research assistant focused on ML papers."

   2. **My Aspirations** — Your goals. I won't execute them autonomously,
   but they help me organize and prioritize when you give me directives.

   Note: the framework already auto-seeds a bootstrap aspiration
   ("Maintain Agent Health" — recurring housekeeping). Your aspirations
   are ADDED on top.

   Examples:
   - "Learn the codebase thoroughly."
   - "Research competitor platforms."

   3. **My Curriculum** (optional) — Staged learning plan with graduation
   gates before attempting more complex tasks. If omitted, the framework's
   default 3-stage curriculum (Foundation / Growth / Autonomy) is used —
   it includes concrete graduation gates, not a blank placeholder.

   Tell me these three — your Self, your Aspirations, and optionally
   your Curriculum. I'll learn when you teach me.
   ```

C3-C7. Same as autonomous Phase C steps C3-C7 (parse, echo, confirm,
curriculum, self.md) — INCLUDING the C5 HARD STOP: explicit user
confirmation is mandatory before the C6 curriculum / C7 self.md writes,
and Self is never derived from The Program (C1.9 rules 2-3).

C8. Set mode and state:
    - Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-mode-set.sh assistant`

      **HALT ON NON-ZERO EXIT (g-115-1032, 2026-05-21)** — if
      `session-mode-set.sh` exits non-zero, STOP. Do NOT proceed to the
      session-state-set.sh write below. Mode-set failure means the on-disk
      mode signal was NOT written; agent will fall back to the reader disk
      default per CLAUDE.md "Mode System" — so the user who asked for
      assistant mode would silently land in reader (no writes, no
      directives), while the C10 success message would falsely confirm
      assistant mode. This is the canonical silent capability mismatch the
      goal calls out. Display:
      > Cannot initialize agent `<agent-name>` in assistant mode
      > (session-mode-set.sh exited non-zero). Investigate stderr above and
      > retry `/start <agent-name> --mode assistant`.
    - Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-state-set.sh IDLE`

      **HALT ON NON-ZERO EXIT (G2, 2026-05-20)** — if `session-state-set.sh`
      exits non-zero, STOP. Do NOT proceed to C8.5/C9/C10. State remains
      UNINITIALIZED; the persona-set below is harmless. Display:
      > Cannot initialize agent `<agent-name>` in assistant mode
      > (session-state-set.sh exited non-zero). Investigate stderr above and
      > retry `/start <agent-name> --mode assistant`. Without this halt,
      > C10's success message would lie and C9's `/create-aspiration` would
      > also fire against an uninitialized agent.
    - Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-persona-set.sh true`
    - Seed session_start: Bash: `date +%Y-%m-%dT%H:%M:%S | MIND_AGENT=<agent-name> bash core/scripts/wm-set.sh session_start`
      (Same rationale as Phase C reader mode C4. MIND_AGENT prefix on the
      three state setters above matches G1's reader-C4 fix.)

C8.5. Invoke `/prime` — load domain context before aspiration creation.

C9. ASPIRATION-FROM-USER INVOCATION (rb-797 silent-skip mitigation).

   IF aspiration text was extracted from C3 (i.e., the user provided one or
   more aspiration descriptions in their reply): you MUST invoke
   `/create-aspiration from-user` with that text BEFORE proceeding to C10.
   Do NOT skip this phase. The single-line imperative form was historically
   pattern-skipped after dense-Bash C8 (rb-797 / g-115-522 — failure mode:
   user-provided aspiration text silently dropped, only auto-seeded asp-001/
   asp-003 landing in the agent's queue).

   Invoke `/create-aspiration from-user` with the extracted aspiration text.

C10. Load mode instructions: Read `core/config/modes/assistant.md`

C10.5. POST-INIT ASPIRATION VERIFICATION (rb-797 silent-skip detector).

   Counts the agent's non-bootstrap aspirations and warns loud if user
   provided aspiration text in C3 but the C9 invocation produced no
   non-bootstrap aspiration. This catches the pattern-skip failure mode
   after the fact and gives the agent a chance to re-invoke before C11.

   Bash: MIND_AGENT=<agent-name> bash core/scripts/aspirations-read.sh --source agent --active-compact 2>/dev/null \
     | py -3 -c "import json,sys; d=json.load(sys.stdin); asps=d if isinstance(d,list) else d.get('aspirations',[]); nonboot=[a for a in asps if a.get('id') not in ('asp-001','asp-003')]; print('NONBOOT='+str(len(nonboot)))"

   IF user provided aspiration text in C3 AND NONBOOT == 0:
       Output (LOUD): "▸ ⚠ POST-INIT WARNING: User provided aspiration text in C3 but agent has 0 non-bootstrap aspirations. C9 (/create-aspiration from-user) likely silent-skipped — rb-797 failure mode. RE-INVOKE /create-aspiration from-user explicitly with the extracted text NOW before C11."
       Bash: source core/scripts/_paths.sh && mkdir -p "$WORLD_DIR/audit-reports" && printf '{"agent":"%s","detected_at":"%s","c3_extracted":true,"nonboot_count":0,"reason":"rb-797 silent-skip"}\n' "<agent-name>" "$(date +%Y-%m-%dT%H:%M:%S)" >> "$WORLD_DIR/audit-reports/start-c9-skip-detections.jsonl"
   ELIF user provided aspiration text in C3 AND NONBOOT >= 1:
       Output: "Post-init verification: $NONBOOT non-bootstrap aspiration(s) present — C9 landed user aspirations correctly."
   ELSE:
       Output: "Post-init verification: no user aspiration text extracted in C3, no verification needed."

C11. Output: "Agent initialized in assistant mode. I'll learn when you teach me — give me directives like 'learn about X' or 'remember that Y'."

### Phase C for Autonomous Mode (current behavior)

**C1.9. BOOTSTRAP IDENTITY GATE — NON-SKIPPABLE (applies to Assistant Phase C too).**

This gate exists because of the 2026-05-15 charlie incident: the agent
fabricated `charlie/self.md` by "deriving" it from The Program, skipped the
C5 confirmation by invoking the self.md Decision-Authority model, flipped to
RUNNING, and then the stop hook (which forces the aspirations loop whenever
state==RUNNING) made interactive correction impossible — the user's
corrections kept getting interrupted and `/boot` never ran.

Rules — ALL mandatory, in order, BEFORE C2:

1. **Check for a user-staged spec FIRST.** Ask the user (plainly, in text)
   whether a prepared Self / identity / start-block spec already exists and,
   if so, its file path(s). If a path is given: `Read` it and use it
   **verbatim** (`cp` for exact fidelity when it is itself the target file —
   no transcription drift). The staged file IS the Self. Do not paraphrase,
   summarize, "faithfully derive", or improve it.

2. **NEVER author Self by inference.** You MUST NOT derive Self from
   `world/program.md`, from sibling `agents/<agent>/self.md` files, from the team
   model, or from any reasoning about "what this agent obviously is." Bootstrap
   identity is a user-input gate, full stop. "The Program already describes
   this agent" is INPUT to show the user for confirmation — never license to
   author it yourself.

3. **Decision-Authority / guard-380 do NOT apply here.** Those govern
   *evolving an EXISTING Self during the autonomous loop* (post-notification,
   revert-if-wrong). They do **not** authorize fabricating a NEW agent's
   initial Self at `/start`, and they do **not** permit skipping C5. If you
   catch yourself reasoning "self.md material writes are act-and-report, so
   I'll derive it and surface for review" — STOP. That is the exact charlie
   rationalization. Bootstrap ≠ evolution.

4. **C2→C5 must complete with EXPLICIT user confirmation before ANY
   state-mutating step** (C6 curriculum, C7 self.md, C8 mode-set, the C9.9
   runner claim). If `AskUserQuestion` is unavailable, ask in plain text and
   **wait** for the reply. Do not proceed on assumption. Do not batch past
   the confirmation.

5. **Why stopping here is safe (and why later is not).** At C1.9–C5 the
   agent-state is still UNINITIALIZED/IDLE. The stop hook only force-enters
   the aspirations loop when state==RUNNING (stop-hook.sh Gate 1: state !=
   RUNNING → ALLOW stop). So pausing for the user here is fully interruptible
   and the user can reply normally. After the C9.9 runner claim flips RUNNING,
   that is no longer true — which is precisely why identity MUST be settled,
   confirmed, and written before the claim (Fix 2 reorders the claim to sit
   immediately before `/boot` for the same reason).

Only after this gate passes, proceed to C2.

C2. Display the identity and aspirations prompt:

   ```
   Now I need three things from you:

   **My Self** — This is the agent's identity. It tells me WHO I am
   and WHAT I'm for. It's the fundamental drive that shapes every decision
   I make. Think of it as the soul of the agent. This is separate from
   The Program (the world's shared purpose) — Self is about this specific agent.

   Examples:
   - "You are an autonomous QA engineer for Acme Corp. Always be looking
     for the next improvement."
   - "You need to make money or die. Find every revenue opportunity."
   - "You are a personal research assistant focused on machine learning
     papers and implementations."

   **My Aspirations** — These are your goals. Think of them as a feature
   list, or life goals, or a to-do list. They can be literally anything —
   learn something, build something, analyze something, fix something.
   I can have multiple at once and I'll break each into actionable steps.

   Note: the framework already auto-seeds two bootstrap aspirations:
     - "Maintain Agent Health" (asp-001) — recurring housekeeping goals
       (reflect, review hypotheses, tree maintenance, replay, archival)
     - "Explore and Learn" (asp-001 world) — initial domain exploration
   Your aspirations are ADDED on top of these — you are not writing on
   a blank slate.

   Examples:
   - "Learn the codebase and API surface thoroughly."
   - "Improve test coverage to 80%."
   - "Research competitor platforms and identify opportunities."

   **My Curriculum** (optional) — This is your staged learning plan.
   It defines what capabilities I unlock as I demonstrate competence.

   If you don't provide one, I'll use the framework's default 3-stage
   curriculum (from `core/config/curriculum.yaml`):
     Stage 1 (Foundation): Learn and explore (no Self edits, no forging)
     Stage 2 (Growth): Apply knowledge (Self edits + forging enabled)
     Stage 3 (Autonomy): Full capabilities (parallel execution enabled)
   The default includes concrete graduation gates (10 completed goals +
   competence >= 0.25 for Stage 1, etc.) — not a blank placeholder.

   Tell me all three — your Self, your Aspirations, and optionally
   your Curriculum. The more detail, the better I can act autonomously.
   ```

C3. Parse response:
   - Extract Self (identity/purpose/drive)
   - Extract aspiration descriptions (one or more goals/directions)
   - Extract curriculum stages (if provided). If user omits curriculum or
     says "default": note "use defaults"

C4. Echo back understanding:

   ```
   Here's what I understand:

   **My Self**
   [parsed Self — the agent's own words summarizing the user's intent]

   **Aspirations I'll create:**
   1. [title] — [brief description with initial goals]
   2. [title] — ...

   **Curriculum (Learning Stages):**
   1. [Stage name] — [description]. Unlocks: [none / self-edits / etc.]
      Graduation: [gate descriptions in plain language]
   2. [Stage name] — ...
   (or: "Using default 3-stage curriculum: Foundation → Growth → Autonomy")

   Does this look right?
   ```

C5. AskUserQuestion for confirmation (yes / adjust) — **HARD STOP (C1.9 gate)**
   - If adjust: re-parse and echo again
   - If yes: proceed
   - This is the non-skippable confirmation from C1.9 rule 4. You MUST NOT
     advance to C6/C7/C8/C9.9 until the user has explicitly confirmed Self +
     aspirations + curriculum. No state mutation, no self.md write, no
     mode-set, no runner claim before an explicit "yes". If the user staged
     a spec (C1.9 rule 1), echo back that you are using it verbatim and still
     get the explicit confirm — a staged file does not waive C5, it just
     makes C2–C4 a read-back instead of an elicitation.

C6. Write curriculum to `agents/<agent>/curriculum.yaml`:
   ```
   IF user provided custom stages:
     Parse into stage objects following the schema:
       - id: cur-01, cur-02, ... (sequential)
       - name: parsed stage name
       - description: parsed description
       - unlocks: infer from user intent (default all false for early stages,
         progressively enable for later stages)
         - allow_self_edits: false/true
         - allow_forge_skill: false/true
         - allow_multi_goal_parallelism: false/true
       - graduation_gates: infer from user criteria, using gate types:
         - metric_threshold (for competence/numeric targets)
         - count_check (for goal completion counts)
         - log_scan (for event counts)
         - command_check (for script-based checks)
         If user gives vague criteria: use reasonable defaults
         (e.g., "after mastering basics" → competence >= 0.30)
       - gate_status: initialize all as {passed: false, last_checked: null, current_value: null}

   IF user said "default" or omitted curriculum:
     Read core/config/curriculum.yaml → default_stages
     Use those stages directly

   Write agents/<agent>/curriculum.yaml (Edit the file seeded by init-mind.sh):
     current_stage: first stage ID (cur-01)
     stage_history:
       - stage_id: cur-01
         entered: "{today}"
         exited: null
     stages: [the parsed or default stage array]
   ```

C7. Write `agents/<agent>/self.md` with parsed Self (where `<agent>` is the active agent directory):
   ```markdown
   ---
   created: "{today}"
   last_updated: "{today}"
   last_update_trigger: "initial_creation"
   source: "user"
   ---

   # Self

   {parsed Self content}
   ```

C7.7. Layer-B output-style gate (g-115-316 / guard-454 / rb-629).
    Refuses autonomous mode + Explanatory output style — documented loop killer.
    Runs BEFORE C8 so a refusal leaves agent-state untouched.
    Step 0.6 already fires the same check pre-Phase-A so most users hit the
    early bail; this is the defense-in-depth layer in case Step 0.6 fail-opened
    (missing settings file, no py launcher).
    - Bash: `MIND_AGENT=<agent-name> bash core/scripts/output-style-gate.sh --mode autonomous`
      (Append ` --override "<justification>"` when the Step 0.5 parser captured
      `--override-output-style <justification>`. Pass the justification string
      verbatim — it lands in `world/output-style-overrides.jsonl`.)
      Exit codes: 0=proceed, 2=REFUSE (STOP /start, ask user to run
      `/output-style default` first, then re-issue `/start <agent-name>`),
      3=override accepted (proceed; audit logged to
      `world/output-style-overrides.jsonl`). Fail-open if gate is missing —
      Layer A (Return Protocol) and Layer C (stop-hook trailing-text-detector)
      remain in effect.

C8. Set MODE + explicit IDLE state — agent-state RUNNING flip is deferred to C9.9.
    (Fix 2, 2026-05-15: the RUNNING flip is deliberately deferred until
    everything interactive/long — identity confirmation C5, prime C8.5,
    aspiration creation C9, verification C9.3 — is DONE. A turn-end anywhere
    in C8–C9.3 then leaves state=IDLE, so the stop hook ALLOWS the stop and
    the user can still reply. The stop hook only force-enters the aspirations
    loop when state==RUNNING.)
    - Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-mode-set.sh autonomous`
      (Mode signal ONLY — this is NOT the agent-state RUNNING flip (that is
      C9.9). Explicit `MIND_AGENT=` prefix — see IDLE step 2 belt-and-
      suspenders rationale. Setting mode=autonomous now makes the C8.5 prime
      run as an autonomous counter-bump retrieve, the intended bootstrap-prime
      behavior.)

      **HALT ON NON-ZERO EXIT (g-115-1032, 2026-05-21)** — if
      `session-mode-set.sh` exits non-zero, STOP. Do NOT proceed to the
      session-state-set.sh write below or to C8.5 /prime / C9 aspiration
      creation / C9.9 RUNNING flip. Mode-set failure means the on-disk
      mode signal was NOT written; agent will fall back to the reader disk
      default per CLAUDE.md "Mode System" — and a subsequent autonomous
      loop entry (C9.9) against a reader-default agent would mis-route
      /prime's retrieve, fire `/create-aspiration` against an
      uninitialized-mode agent, and the autonomous bootstrap would lie
      about being autonomous while reader-mode capabilities applied.
      Display:
      > Cannot transition agent `<agent-name>` to mode `autonomous` at C9
      > (session-mode-set.sh exited non-zero). Investigate stderr above and
      > retry `/start <agent-name>` (autonomous is the default mode).
    - Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-state-set.sh IDLE`
      (Fresh-install fix, 2026-05-20: on UNINITIALIZED→autonomous, agent-state
      never existed before — the "state stays IDLE" comment above is only
      literally true if IDLE was already set. Without this explicit write,
      C8.5 /prime sees state=UNINITIALIZED and Phase 0.5 either short-circuits
      or branches wrong. The reader and assistant flows (Phase C lines ~1152,
      ~1235) already do this; the autonomous flow's omission was a state-
      machine gap. **HALT ON NON-ZERO EXIT** — if the script exits non-zero,
      STOP. Investigate stderr and retry.)

C8.5. Invoke `/prime` — load domain context before aspiration creation.
    When connecting to an existing world, this ensures goal decomposition
    benefits from accumulated knowledge. On a fresh world, prime loads
    empty stores harmlessly. Runs at state=IDLE — prime explicitly supports
    IDLE (Phase 0.5: "IDLE or RUNNING: PROCEED"); no RUNNING dependency.

C9. ASPIRATION-FROM-USER INVOCATION (rb-797 silent-skip mitigation).

   IF aspiration text was extracted from C3 (i.e., the user provided one or
   more aspiration descriptions in their reply): you MUST invoke
   `/create-aspiration from-user` with that text BEFORE the C9.9 runner
   claim. Do NOT skip this phase. The single-line imperative form was
   historically pattern-skipped (rb-797 / g-115-522 — failure mode:
   user-provided aspiration text silently dropped, only auto-seeded
   asp-001/asp-003 landing in the agent's queue). The Fix 2 reorder
   de-risks this further: C9 now runs while state is still IDLE and BEFORE
   the dense triple-write (moved to C9.9), so a silent-skip caught by C9.3
   can be re-invoked without the stop hook interfering.

   Invoke `/create-aspiration from-user` with the extracted aspiration text.

C9.3. POST-INIT ASPIRATION VERIFICATION (rb-797 silent-skip detector).
   Runs BEFORE the C9.9 runner claim so a detected silent-skip can be
   re-invoked while state is still IDLE (interruptible — user can reply)
   instead of after RUNNING (where the stop hook forces the loop).

   Bash: MIND_AGENT=<agent-name> bash core/scripts/aspirations-read.sh --source agent --active-compact 2>/dev/null \
     | py -3 -c "import json,sys; d=json.load(sys.stdin); asps=d if isinstance(d,list) else d.get('aspirations',[]); nonboot=[a for a in asps if a.get('id') not in ('asp-001','asp-003')]; print('NONBOOT='+str(len(nonboot)))"

   IF user provided aspiration text in C3 AND NONBOOT == 0:
       Output (LOUD): "▸ ⚠ POST-INIT WARNING: User provided aspiration text in C3 but agent has 0 non-bootstrap aspirations. C9 (/create-aspiration from-user) likely silent-skipped — rb-797 failure mode. RE-INVOKE /create-aspiration from-user explicitly with the extracted text NOW before the C9.9 runner claim."
       Bash: source core/scripts/_paths.sh && mkdir -p "$WORLD_DIR/audit-reports" && printf '{"agent":"%s","detected_at":"%s","c3_extracted":true,"nonboot_count":0,"reason":"rb-797 silent-skip","mode":"autonomous"}\n' "<agent-name>" "$(date +%Y-%m-%dT%H:%M:%S)" >> "$WORLD_DIR/audit-reports/start-c9-skip-detections.jsonl"
   ELIF user provided aspiration text in C3 AND NONBOOT >= 1:
       Output: "Post-init verification: $NONBOOT non-bootstrap aspiration(s) present — C9 landed user aspirations correctly."
   ELSE:
       Output: "Post-init verification: no user aspiration text extracted in C3, no verification needed."

C9.9. RUNNER CLAIM — the agent-state RUNNING flip (Fix 2 critical section).

   **Everything interactive/long is DONE by here** (identity confirmed C5,
   prime C8.5, aspirations created C9, verified C9.3). From the first command
   in C9.9 through `Invoke /boot` (C11) there must be NOTHING stoppable,
   interactive, or long. Execute C9.9 → C10 → C11 in immediate succession in
   a single turn. If a turn-end (autocompact, a text summary, a question to
   the user) lands between `session-state-set RUNNING` and `/boot`, the stop
   hook force-enters the aspirations loop and `/boot` never runs — the
   2026-05-15 charlie incident. Do NOT pause to report, ask, or summarize
   inside C9.9–C11.

    - Bash: `MIND_AGENT=<agent-name> bash core/scripts/heartbeat-tick.sh --bypass-state`
      (Seeds `runner-heartbeat` mtime immediately before the RUNNING
      transition. `--bypass-state` is REQUIRED because state is still IDLE
      here — heartbeat-tick.sh's IDLE-state gate refuses bare ticks; /start
      is the one legitimate caller that ticks against an about-to-flip IDLE
      state. Liveness is pure mtime — see `core/config/conventions/compact-recovery.md`.)
    - Bash: `if [ -z "$MIND_SID" ]; then echo "ERROR:EMPTY_MIND_SID"; exit 1; fi; RUNNER_TOKEN=$(py -3 -c "import uuid;print(uuid.uuid4())" 2>/dev/null || python3 -c "import uuid;print(uuid.uuid4())" 2>/dev/null); [ -n "$RUNNER_TOKEN" ] || { echo "ERROR:RUNNER_TOKEN_GEN_FAILED"; exit 3; }; AGENT_STATE_DIR="agents/<agent-name>/session"; mkdir -p "$AGENT_STATE_DIR" && echo "$MIND_SID" > "$AGENT_STATE_DIR/running-session-id.tmp" && mv "$AGENT_STATE_DIR/running-session-id.tmp" "$AGENT_STATE_DIR/running-session-id" && echo "$MIND_SID" > "$AGENT_STATE_DIR/latest-session-id.tmp" && mv "$AGENT_STATE_DIR/latest-session-id.tmp" "$AGENT_STATE_DIR/latest-session-id" && echo "$RUNNER_TOKEN" > "$AGENT_STATE_DIR/runner-token.tmp" && mv "$AGENT_STATE_DIR/runner-token.tmp" "$AGENT_STATE_DIR/runner-token" && echo "RUNNER_TOKEN=$RUNNER_TOKEN"`
      (Triple-write parallel to IDLE Step 3 — runner-token rationale + the
      rb-323/guard-403 reason this MUST run BEFORE state-set RUNNING below.
      heartbeat-tick directly above + this triple-write are the observer-
      paired signals seeded first. The `agents/<agent-name>/session/` Phase
      2.5.D prefix in the heredoc is REQUIRED — without it the writes land
      at PROJECT_ROOT/`agents/<agent-name>/session/` and create the 2026-05-19
      bravo/ cruft.)
      **HALT ON RUNNER_TOKEN_GEN_FAILED** — same as IDLE Step 3. State is
      still IDLE here, so a halt is a clean retry (no half-claimed zombie).
    - Bash: `rm -f agents/<agent-name>/session/iteration-checkpoint.json`
      (F4 reorder, 2026-05-20: moved BEFORE the state-set RUNNING below so
      the critical section between RUNNING and /boot is truly empty — making
      the C9.9 comment "NOTHING stoppable between RUNNING and /boot" literal,
      not just spirit. Safe to run at IDLE.)
    - Seed session_start: Bash: `date +%Y-%m-%dT%H:%M:%S | MIND_AGENT=<agent-name> bash core/scripts/wm-set.sh session_start`
      (F4 reorder: moved before the state flip. Same rationale as IDLE→autonomous
      step 3 — wm-set is a pure-Bash write to a top-level WM key, safe at IDLE.)
    - **DDB runner-claim acquire (single-runner lifecycle, design §4).**
      Bash: `MIND_AGENT=<agent-name> bash core/scripts/runner-claim.sh acquire --agent <agent-name>; echo "ACQUIRE_RC=$?"`
      (Cross-machine claim taken just before the local state-set below, using the
      runner-token from the triple-write above. Under STORAGE_BACKEND=own-cloud the
      daemon does the real DDB IDLE->RUNNING CAS; on any other backend it no-ops
      (exit 0). **HALT ON ACQUIRE_RC=4** (a peer holds a live DDB
      claim) — do NOT proceed to RUNNING; state stays IDLE. Any OTHER non-zero rc
      is FAIL-OPEN: log and PROCEED. Mirrors the IDLE→autonomous step-3 acquire.)
    - Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-state-set.sh RUNNING`
      (State flip — final write in the RUNNING-claim sequence. heartbeat-tick
      + triple-write are seeded first per rb-323/guard-403 so observers never
      see RUNNING with a stale heartbeat or empty SID files. From THIS line
      the stop hook is armed — C10 + C11 MUST follow with no interruption.)

      **HALT ON NON-ZERO EXIT (F1, 2026-05-20)** — if the script exits non-zero,
      STOP. Do NOT proceed to C10/C11. State remains IDLE; the observer-paired
      signals seeded above are harmless (fresh heartbeat at IDLE means nothing
      to observers). Display:
      > Cannot transition agent `<agent-name>` to RUNNING at C9.9 (session-state-set.sh
      > exited non-zero). The agent stays IDLE; investigate stderr above and retry
      > `/start <agent-name>`. Without this halt, `/boot` (C11) would read state=IDLE
      > and abort with "Agent is stopped" — confusing failure mode.

C10. Output: "Agent initialized. Learning loop starting."

C11. Invoke `/boot` — immediately. No tool call, pause, question, or text
     between C9.9's `session-state-set RUNNING` and this invocation.

