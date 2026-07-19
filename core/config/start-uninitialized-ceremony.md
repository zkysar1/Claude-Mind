**Phase A-0: Transplant-Resume Detection (cloned agent landing on a new machine)**

UNINITIALIZED has TWO causes that need OPPOSITE handling:
- **Genuine first run** — brand-new agent, nothing on disk → full init (Phase A/B/C).
- **Transplant/clone** — the agent dir arrived via `git clone` with its tracked
  content intact (`.initialized`, `self.md`, `aspirations.jsonl`,
  `curriculum.yaml`, journal/, experience/), but `session/agent-state` is absent
  because `session/` and `local-paths.conf` are gitignored (machine-local, never
  travel). Running full init here would re-elicit identity and **overwrite the
  cloned `self.md`/`curriculum.yaml`** → resume as an EXISTING agent instead.

`session-state-get.sh` only inspects `agent-state`, so it can't tell these apart.
The tracked `.initialized` marker can: it clones with the agent, so its presence
on an otherwise-UNINITIALIZED agent means "already initialized, just not started
on THIS machine."

Bash: `bash core/scripts/agent-resume-scaffold.sh "<agent-name>"; echo "rc=$?"`

The scaffold (idempotent, verified) writes a default `local-paths.conf` (local
own-cloud cache under `<cache-root>/<env-id>/`, override via `RUNTIME_CACHE_ROOT`)
and creates `session/`. It NEVER writes `agent-state`/`agent-mode` (those stay
/start's job — guard-340) and NEVER touches tracked content. Branch on rc:

- **rc=2** (no agent dir OR no `.initialized` — genuine first run): proceed to
  **Phase A** below. The rest of this UNINITIALIZED branch is unchanged.

- **rc=1** (scaffold error): STOP, display stderr, do not proceed (guard-372 — fail loud).

- **rc=0** (transplanted agent — scaffolded): do NOT run Phase A/B/C init.
  Resume it as an EXISTING agent. **Resume mode = the parsed `--mode` value
  (default `autonomous`)** — a transplant-resume is treated exactly like a bare
  `/start` on any IDLE agent: bare `/start <agent>` runs the loop; `--mode
  reader`/`assistant` is honored for the cautious first-boot-on-a-new-machine
  case. (The earlier reader-first default for bare transplant-resume was removed
  2026-06-04: it forced a two-step `/start` dance — first call landed reader,
  a second was needed to actually run — which the user found annoying. The
  dual-runner risk it guarded against is covered by the ownership warning below
  plus the own-cloud write lock.)

  IF the resume mode is `autonomous`, FIRST print this ownership warning, then
  proceed:
  "⚠ One machine per agent: starting `<agent-name>`'s autonomous loop here. If
  `<agent-name>` is still RUNNING on its origin machine, `/stop` it there NOW —
  two runners of the SAME agent on one own-cloud world claim and release each
  other's goals (the DDB lock prevents file corruption, not this semantic
  collision). Also confirm `.env.local` is configured for own-cloud (the one
  manual step — secrets never travel in git)."

  **Prerequisites check (ALL resume modes — transplant gap fix, g-115-1334).**
  This rc=0 path does NOT run Phase A's A0 check (it skips Phase A/B/C init),
  so a freshly-cloned machine can reach the daemon start (Step 2 → IDLE branch)
  with Python deps missing — surfacing as a dead daemon instead of a friendly
  error (the 2026-06-04 machine-2 bring-up: PyYAML, psutil, and the own-cloud
  cloud-SDK all absent). Run the check here, before any state write or daemon
  start:

  Bash: `bash core/scripts/check-prerequisites.sh`

  **HALT ON FAILURE** — if exit code ≠ 0, STOP, display the script's stderr
  verbatim, and do NOT proceed to the Steps below. On an own-cloud machine
  (`.env.local` sets `STORAGE_BACKEND=own-cloud`) the check also fails loudly
  on a missing own-cloud cloud-SDK dependency; install per its hint
  (`pip install -r mind_api/requirements-owncloud.txt`) and re-run
  `/start <agent-name>`. (If `.env.local` is not configured for own-cloud yet,
  the check correctly skips that check — the default local backend never needs
  it.)

  Steps:
  1. Bash: `MIND_AGENT=<agent-name> bash core/scripts/session-state-set.sh IDLE`
     (UNINITIALIZED→IDLE — the same init endpoint reader/assistant first-boot
     already uses; authorized /start path, see `.claude/rules/user-interaction.md`.
     **HALT ON NON-ZERO EXIT**: STOP, display stderr.)
  2. Execute the **IDLE branch** (Step 0 onward, above) with `<target-mode>` set
     to the resume mode. It binds the session, pulls world/meta from S3
     (own-cloud), primes, and — for `autonomous` — claims the runner and hands
     off to `/boot`. NO identity prompts, NO clobber of tracked content, any mode.
  3. After the IDLE branch's mode output, append this notice (substitute
     `<world_path>` from the rc=0 JSON and `<chosen-mode>`):
     "✓ Resumed transplanted agent `<agent-name>` in `<chosen-mode>` mode — cloned
     identity + memory intact; scaffolded only the machine-local session + paths
     (world cache: `<world_path>`). world/meta rehydrate from S3 on first daemon
     read.{IF chosen-mode is reader or assistant, append: " (Once the pull looks
     right and `<agent-name>` is `/stop`-ped on its origin machine, `/start
     <agent-name> --mode autonomous` runs the loop here.)"} If `.env.local`
     isn't set up for own-cloud yet — the one manual step, secrets never travel
     in git — do that first."

**Phase A: Agent Name and Session Binding**

The agent name from the `/start <name>` command becomes the directory name.
The agent directory must exist before path configuration (since `local-paths.conf` lives inside it).

A0. **Prerequisites Check** — verify the runtime environment BEFORE writing any state:

   Bash: `bash core/scripts/check-prerequisites.sh`

   The script verifies Python 3.10+, PyYAML, bash 4+ (required) and warns
   on missing git or psutil (optional — framework still works). On failure
   it prints one consolidated friendly error block with copy-pasteable fix
   commands and exits 2.

   **HALT ON FAILURE** — if exit code ≠ 0, STOP. Display the script's stderr
   to the user verbatim and do NOT proceed to A1. The user must install the
   missing prerequisites and re-run `/start <agent-name>`.

   Rationale: pre-2026-05-17 a fresh install would crash 4 scripts deep
   with a cryptic `ModuleNotFoundError: No module named 'yaml'` from
   init-meta.sh → meta-init.py. The wife (a non-technical user) had no
   path to diagnose this. The prerequisites check surfaces all missing
   pieces in one pass with one error message.

A1. Validate the agent name (from the `/start <name>` argument):

   Bash: `bash core/scripts/validate-agent-name.sh "<agent-name>"`

   The script enforces:
   - Lowercase kebab-case: `^[a-z][a-z0-9-]*$` (must start with a letter,
     then letters / digits / hyphens only)
   - Not in the reserved-name list (`core`, `meta`, `world`, `node_modules`,
     `.git`, `.claude`, `.github`)

   Exit codes:
   - `0` — valid, proceed to A2
   - `2` — invalid format
   - `3` — reserved name

   **HALT ON FAILURE** — if exit code != 0, STOP. Display the script's stderr
   verbatim to the user. Do NOT proceed to A2. The user must re-run
   `/start <valid-name>` with a corrected name.

   Rationale: failing fast here prevents the user from spending minutes on
   path elicitation (Phase B) and identity capture (Phase C2-C5) only to
   have init-agent.sh:33-46 reject the name at C0. Single source of truth
   caveat: `validate-agent-name.sh` and `init-agent.sh:33-46` MUST stay in
   sync — they implement the same regex and reserved-name list. Defense in
   depth: A1 catches typos early; init-agent re-validates in case A1 was
   bypassed.

A2. **Bind Agent to Session**

   First, ensure the agent dir + local-paths.conf placeholder exist so the binding writer can validate. The full conf is configured in Phase B; here we just create the directory shape so `session-binding-write.sh` doesn't fail its agent-dir-exists check.

   Bash: `if [ -z "$MIND_SID" ]; then echo "ERROR:EMPTY_MIND_SID"; exit 1; fi; mkdir -p "agents/<agent-name>" && touch "agents/<agent-name>/local-paths.conf" && bash core/scripts/session-binding-write.sh --sid "$MIND_SID" --agent "<agent-name>" --mode "<target-mode>" --retire-legacy >/dev/null && echo "BOUND:$MIND_SID" && printf '\n╔════════════════════════════════════════════════════════════╗\n║                                                            ║\n║    ✓  RACE_WINDOW_CLOSED                                   ║\n║       Safe to /start another agent in another terminal     ║\n║                                                            ║\n╚════════════════════════════════════════════════════════════╝\n\n'`

   `<target-mode>` substitution: the LLM substitutes the target mode (`reader`, `assistant`, or `autonomous` — default for UNINITIALIZED entry without `--mode` is `autonomous`).

   (No write to `agents/<agent-name>/session/latest-session-id` here. For autonomous mode, the Phase C9.9 runner claim atomically writes both `latest-session-id` and `running-session-id` together. Same runner-only-writes-both rule as IDLE Step 0.)

   **HALT ON EMPTY SID** — if output contains `ERROR:EMPTY_MIND_SID`, STOP. Do NOT proceed to Phase A3. Display to the user:
   > Cannot initialize agent `<agent-name>`: the PreToolUse[Bash] hook did not inject `MIND_SID`. Close this terminal, open a new one, and retry `/start <agent-name>`. If the issue persists, check `.claude/settings.json` for the PreToolUse hook registration and `core/scripts/bash-agent-inject.sh` / `.py`.

   Phase 2.6: the binding lives at `agents/<agent-name>/sessions/$MIND_SID/binding.yaml` — the per-session dir name IS the SID. One binding per session, no shared root file. The directory shape is also the queryable record of "how many sessions has this agent had" (count subdirs under `agents/<agent-name>/sessions/`).

A3. Create the agent directory (if it doesn't exist):

   Bash: `mkdir -p agents/<agent-name>`

**Phase B: Configure Paths** (only if `agents/<agent>/local-paths.conf` does not yet contain `WORLD_PATH=`).

Each agent stores its own path configuration. `world/` and `meta/` can live
inside the project root (simplest, single-machine) OR at external user-supplied
paths (shared drive / NAS / cloud-sync folder for multi-machine sharing).

**Why a content check, not an existence check (F3, 2026-05-20)**: A2 above
calls `touch local-paths.conf` to satisfy `session-binding-write.py:77`'s
"conf-file-must-exist" gate (the binding writer refuses to write the binding
without a conf file present, even if empty). That leaves an EMPTY conf on
disk after A2. A literal-minded reading of an existence check would conclude
"conf exists, skip Phase B" — bypassing path elicitation entirely and leaving
`_paths.sh:164` resolving `WORLD_DIR` to empty string. Phase B's check (here
and at "skip Phase B entirely" below) is content-aware: empty conf from A2
fails the WORLD_PATH= grep, so Phase B runs and B7 populates the conf.
Populated conf passes the grep, so Phase B skips on legitimate resume.

**B0.5. BOOTSTRAP PATHS GATE — NON-SKIPPABLE (mirrors C1.9 semantics).**

This gate exists because of the 2026-05-20 testy incident: the agent invented
`<project_root>-world` and `-meta` sibling paths as "reasonable defaults"
between A6 and B7, without prompting the user — because Auto Mode triggered
"make the reasonable call" reasoning. The invented paths then propagated
through B7 (conf write), B10 (permission grants), C0 (init), and the user
had to manually clean up the test repo to retry. Path choices are
unrecoverable once written: directories get created, settings.local.json
gets seeded with those paths, and reverting requires a manual delete.

Rules — ALL mandatory, in order, BEFORE B7 (the conf write):

1. **NEVER invent paths.** Even in auto mode, even with the suggested default
   visible in the prompt — the agent MUST NOT write `local-paths.conf`
   without explicit user authorization. "Reasonable default" reasoning is
   the documented testy-incident failure mode. The bar is identical to
   C1.9: "fabricating identity by inference" became "fabricating paths by
   inference" — same shape, same gate.

2. **Suggest, then ask.** Show the suggested default (`./world` and `./meta`
   inside the project root, alongside `core/`, `mind_api/`, `agents/`) AND
   the alternative shapes (shared remote / cloud-sync for multi-machine).
   Always pose the question — even if the user is expected to accept the
   default.

3. **What counts as user authorization** (any one is sufficient):
   - **Explicit path**: "use /Users/me/foo" — proceed with that path
   - **Confirm suggestion**: "yes, use the default", "go with `./world`",
     "the suggestion is fine" — proceed with default
   - **Explicit delegation**: "you pick", "I don't care", "this is a test,
     do whatever", "make it simple", "use whatever makes sense" — proceed
     with default
   - **NOT sufficient**: silence, prior unrelated permissions, the
     existence of Auto Mode, or any inferred preference — STOP and ask.

4. **Auto Mode does NOT override this gate.** When the system reminder says
   "Work without stopping for clarifying questions" — paths are the
   exception, same class as bootstrap identity (C1.9). Stop and ask anyway.
   Proceed only if rule 3 conditions are met.

5. **Why stopping here is safe.** At Phase B, agent-state is still
   UNINITIALIZED. The stop hook does not force-enter the loop until C9.9.
   Pausing for user input is fully interruptible. After B7/B10 write the
   conf + seed permissions, reverting the path choice requires manual
   filesystem cleanup — which is exactly the cost the testy incident paid.

Only after this gate passes (rules 1-4 satisfied), proceed to B1.

B1. Ask for the **world directory** path. NON-SKIPPABLE per B0.5 gate:

   ```
   First, I need to know where to store collective knowledge.

   **World Directory** — This is where all shared domain knowledge lives:
   the knowledge tree, hypotheses, reasoning bank, aspirations, and more.
   Multiple agents and machines can share this directory.

   **Suggested default** (simplest — single machine, single repo):
     .mind-data/world  →  expands to <project_root>/.mind-data/world

   `.mind-data/` is the standard local storage root (asp-330 convention):
   world -> .mind-data/world, meta -> .mind-data/meta, all under one
   gitignored dir that `_paths.py`/`_paths.sh` auto-detect (M1). Everything
   stays inside the project — no external paths to manage, nothing committed.
   The trade-off: agents in OTHER repos or on OTHER machines can't see this
   world. If you want multi-repo or multi-machine collaboration, put world/
   on a shared remote (NAS, OneDrive, SharePoint, Dropbox, iCloud) instead —
   that is the external-path case (B7 writes its WORLD_PATH into
   local-paths.conf, the legacy/external mechanism).

   Other valid examples (use FORWARD slashes on every platform —
   backslashes get interpreted as escape sequences when bash sources the
   path file):
   - C:/Users/you/OneDrive/my-mind-world   (Windows, OneDrive — sharable across machines)
   - /Users/you/Documents/my-mind-world    (macOS — local single-machine)
   - /home/you/mind-world                  (Linux — local single-machine)

   Where should the world directory be?
   - Reply `.mind-data/world` (or just "default", "use the suggestion", "yes")
     to accept the in-project `.mind-data/` default
   - Reply with an absolute/relative path to use a different location
   - Reply "you pick" / "I don't care" / "make it simple" to delegate
     (= proceed with the `.mind-data/` default per B0.5 rule 3)

   When the chosen path is under `.mind-data/`, B3 creates it with `mkdir -p`
   (so the `.mind-data/` parent is created too); `.mind-data/` is gitignored
   (M3). When `.mind-data/` exists it OVERRIDES local-paths.conf per the M1
   precedence chain (`_paths.py _resolve_tier`: env > .mind-data/.env.local >
   .mind-data/{world,meta} > local-paths.conf), so for the local default the
   conf B7 writes is a documented mirror, not the resolution authority.
   ```

   If the user pastes a Windows path with backslashes (e.g.,
   `C:\Users\you\OneDrive\my-mind-world` from Explorer's address bar),
   silently convert backslashes to forward slashes before validation in B3.
   Do not bounce the user back — the conversion is part of normalization.

B2. AskUserQuestion (allowed — agent-state is not RUNNING yet).

   **DO NOT PRESUME** — per B0.5 gate: even in auto mode, do NOT advance
   past B2 without an explicit response that matches one of the
   authorization shapes in B0.5 rule 3. The instinct to "make the
   reasonable call" is the testy-incident failure mode; resist it.

B3. Validate the world path:
   - Resolve relative paths against PROJECT_ROOT
   - Check directory exists (or parent exists and is writable)
   - If **doesn't exist**: create it, confirm "Created new directory at {path}"
   - If **empty**: confirm "Empty directory — I'll set up a fresh world"
   - If **populated** (has `knowledge/` or `.initialized`): confirm "Found an existing world at {path} — I'll connect to it"
   - If **not writable**: tell user, ask for a different path

B4. Ask for the **meta directory** path. NON-SKIPPABLE per B0.5 gate:

   ```
   **Meta Directory** — This is where domain-agnostic improvement strategies
   live. It tracks how the agent gets better at learning itself, independent
   of any specific domain.

   Same rules as the world path: empty directory for fresh start, or existing
   meta directory; forward slashes only.

   **Suggested default** (matches world layout — same `.mind-data/` root):
     .mind-data/meta  →  expands to <project_root>/.mind-data/meta

   Keeps world + meta together under the one gitignored `.mind-data/` root
   (asp-330 convention). B6 creates it with `mkdir -p` when chosen.

   Other valid examples (typically next to the world directory):
   - C:/Users/you/OneDrive/my-mind-meta    (Windows)
   - /Users/you/Documents/my-mind-meta     (macOS)

   Where should the meta directory be?
   - Reply `.mind-data/meta` / "default" / "yes" to accept the in-project default
   - Reply with an absolute/relative path for a different location
   - Reply "you pick" / "same place as world" to delegate (per B0.5 rule 3)
   ```

B5. AskUserQuestion.

   **DO NOT PRESUME** — same enforcement as B2: per B0.5 gate, even in auto
   mode, an explicit user response matching one of the B0.5 rule 3
   authorization shapes is required before B6/B7.

B6. Validate the meta path (same rules as B3)

B7. Write `agents/<agent>/local-paths.conf`:
   ```bash
   # Paths to external world and meta directories
   # Written by /start — edit manually to change locations
   WORLD_PATH={validated_world_path}
   META_PATH={validated_meta_path}
   ```
   IMPORTANT: Use forward slashes on all platforms (e.g., `C:/Users/Shared/world`,
   not `C:\Users\Shared\world`). Backslashes are interpreted as escape sequences
   when bash sources the file. Python handles both slash styles.

B8. Confirm paths:
   ```
   Paths configured:
     World: {world_path}
     Meta:  {meta_path}
   ```

B9. **Add permissions for external paths** — Ask for confirmation:
   ```
   I need to add read/write permissions for these directories to your local
   settings (.claude/settings.local.json). This file is local to your
   machine and not committed to git.

   Permissions to add (recursive subtree, all relevant tools):
     Read / Edit / Write / MultiEdit  on  {world_path}/**
     Read / Edit / Write / MultiEdit  on  {meta_path}/**
     Read / Edit / Write / MultiEdit  on  {project_root}/**

   If your settings.local.json doesn't exist yet, I'll create it with the
   framework's broad allows (Bash, Read, Glob, Grep, WebSearch, WebFetch)
   plus the constitutional deny baseline (rb-931) plus the path allows above.

   If it already exists, I'll only ADD missing rules — your existing config
   (env vars, statusLine, outputStyle, etc.) is preserved verbatim.

   OK to add these?
   ```

B10. AskUserQuestion for confirmation
   - If yes: Bash: `MIND_AGENT=<agent-name> bash core/scripts/permissions-add.sh`

     The explicit `MIND_AGENT=<agent-name>` prefix is belt-and-suspenders
     (matches IDLE Step 2 / Step 0.7 / runner-dead-check pattern). The
     PreToolUse[Bash] hook normally auto-injects by resolving the binding
     written in A2 (Phase 2.6 layout: `agents/<agent-name>/sessions/$MIND_SID/binding.yaml`,
     with legacy `.active-agent-<SID>` fallback). If the hook times out
     cold-starting Python (Windows `bash-agent-inject.sh` failure mode —
     see `core/config/conventions/python-invocation.md`) the script would
     fall back to `_paths.sh:146-154` first-available-conf. On
     UNINITIALIZED first-run this is the ONLY conf so it resolves
     correctly anyway — but hardening the call site against future
     multi-agent installs costs nothing.

     The sanctioned wrapper reads `WORLD_DIR` + `META_DIR` + `PROJECT_ROOT`
     from `_paths.sh` (resolved from the agent's `local-paths.conf` written
     in B7), then merges the path-specific allows into
     `.claude/settings.local.json` AND ensures the constitutional deny
     baseline is present. Atomic write (tempfile + rename). Idempotent on
     re-run.

     **Why a script instead of direct Write/Edit**: `.claude/settings.local.json`
     is the constitutional anchor (rb-931, CLAUDE.md "two-file settings rule").
     The file's own `permissions.deny[]` hard-blocks `Edit`/`Write`/`MultiEdit`
     tool calls on itself — the LLM cannot edit it through Claude's editing
     tools. The script writes via Bash, which the deny patterns do not match.
     This is the user-authorized maintenance path.

     **Exit codes**:
     - `0` — success
     - `2` — required state missing (rare — A2 binding should have populated this)
     - `3` — existing settings.local.json is malformed (file is left untouched)
     - `4` — Python launcher unavailable (re-run check-prerequisites.sh)

     **HALT ON NON-ZERO EXIT** — if exit code != 0, display the script's
     stderr and ask the user whether to: (a) abort /start and fix the issue
     manually, or (b) continue without permissions (the user will then see
     per-call permission dialogs throughout the session). Default
     recommendation: abort + fix.

   - If no: warn that file access to external paths may require per-call
     permission approval throughout the session.

If `agents/<agent>/local-paths.conf` already contains `WORLD_PATH=`, skip Phase B entirely — paths are already configured. Use a content check, not bare existence (see "Why a content check" rationale at Phase B header — A2's `touch` leaves an empty conf on disk before Phase B runs). Concrete probe: `grep -q '^WORLD_PATH=' agents/<agent>/local-paths.conf` — exit 0 means populated, skip; exit 1 means empty or absent, run Phase B.

**Phase C: The Program and Agent Identity**

Phase C establishes two separate things:
- **The Program** (`world/program.md`) — The overarching mission shared by ALL agents in this
  world. Written once, shared across agents. Answers: "Why does this world exist?"
- **Self** (`agents/<agent>/self.md`) — This specific agent's identity, role, and perspective.
  Unique per agent. Answers: "Who am I? What is my role?"

These are NOT the same thing. The Program is the world's purpose. Self is the agent's identity.

**C0. Initialize infrastructure** (all modes):
`bash core/scripts/init-mind.sh`

**C0.1. Ensure daemon is running** (all modes, fail-open):
`bash core/scripts/mind-api-start.sh || echo "[start] daemon-start failed (non-fatal)" >&2`
(Idempotent. Runs after init-mind so mind_api/state/ directory exists. Fail-open so
init can complete even if daemon spawn fails — wrapper auto-spawn is fallback.)

**C0.5. Configure domain conventions** (per-slot existence detection — seed each
missing canonical Pattern B hook slot from the framework templates in
`core/config/templates/`).

Per-slot detection (NOT whole-directory): C0.5 used to short-circuit if
`world/conventions/` had ANY `.md` file, which silently skipped seeding even
when the canonical `post-execution.md` / `pre-execution.md` slots were missing.
Now each slot is checked independently. Canonical slot table:
`core/config/conventions/domain-hooks.md` → "Canonical Hook Slots (Pattern B)".

**C0.5 GATE — NON-SKIPPABLE (mirrors B0.5 / C1.9 semantics).** Applies ONLY
when at least one canonical slot is missing (i.e., the AskUserQuestion below
is going to fire). When both slots already exist from a prior /start, this
gate is moot — the procedure short-circuits at the "skip prompt-and-seed"
branch and no question is asked.

This gate exists because of the 2026-05-20 hooks-prompt-skipped observation:
the user explicitly designed C0.5 (commits `d0f32aa5` / `0aaf3163`, May 16) so
they would be ASKED whether to add domain-specific pre/post-execution steps
on first-touch. But Auto Mode caused the agent to treat the AskUserQuestion at
C0.5 as a "make the reasonable call" moment and silently pick "no" — the
question was never surfaced to the user. Unlike B0.5 (paths) and C1.9
(identity), the underlying choice ("no domain additions, defaults only") is
technically safe for correctness — the framework defaults install by
construction either way. But the user explicitly wanted the **opportunity to
decide** at this moment, and silently answering for them denies that
opportunity. That alone is the failure.

Rules — ALL mandatory, in order, BEFORE the AskUserQuestion at the
"Optional: add domain-specific steps?" prompt below:

1. **NEVER auto-answer the C0.5 prompt.** Even in auto mode, even when "no"
   would be a perfectly safe answer, the agent MUST surface the prompt to
   the user and wait for an explicit reply. "Make the reasonable call"
   reasoning is the documented failure mode. The bar is the same as B0.5
   and C1.9: a moment the user explicitly designed to be theirs.

2. **The defaults install BEFORE the question runs — that is by design.**
   The `cp` commands below install framework-essential conventions by
   construction (per `d0f32aa5`'s design: verify-learning Section DC
   structural invariants hold ONLY because the template is installed
   verbatim). The question is purely about whether the user wants to ADD
   domain layers under `## Domain Additions`, NOT whether the defaults
   install. This is why "no" is safe — but still must be asked.

3. **What counts as user authorization for the answer** (any one is sufficient):
   - **Explicit yes**: "yes", "add steps", "I want to customize" — proceed
     to collect domain content
   - **Explicit no**: "no", "defaults are fine", "skip", "later" — proceed
     without additions
   - **Show first**: "show me", "show-me-the-defaults" — display files,
     then re-ask (loop is built into the procedure below)
   - **Explicit delegation**: "you pick", "I don't care", "this is a test,
     do whatever", "make it simple" — proceed with "no" (defaults
     installed, no additions)
   - **NOT sufficient**: silence, prior unrelated permissions, the
     existence of Auto Mode, or any inferred preference — STOP and ask.

4. **Auto Mode does NOT override this gate.** When the system reminder says
   "Work without stopping for clarifying questions" — the domain-conventions
   prompt is an exception, same class as B0.5 and C1.9. Stop and ask anyway.
   Proceed only if rule 3 conditions are met.

5. **Why stopping here is safe.** At C0.5, agent-state is still
   UNINITIALIZED. The stop hook does not force-enter the aspirations loop
   until C9.9 (per the same reasoning as C1.9 rule 5). Pausing for user
   input is fully interruptible. Unlike paths and identity, the cost of an
   auto-picked "no" is NOT data corruption — it's the loss of a
   deliberately-designed user moment. That alone justifies the gate.

Only after this gate is acknowledged, proceed to the existence check below.
The gate triggers ONLY when the existence check finds at least one missing
slot; when both slots exist, the procedure skips the AskUserQuestion entirely
(line "IF both slots already exist" below) and rules 1-4 do not apply.

Bash: `source core/scripts/_paths.sh && \
  pre_missing=$([ -f "$WORLD_DIR/conventions/pre-execution.md" ] && echo "no" || echo "yes") && \
  post_missing=$([ -f "$WORLD_DIR/conventions/post-execution.md" ] && echo "no" || echo "yes") && \
  echo "pre_missing=$pre_missing post_missing=$post_missing"`

IF both slots already exist (`pre_missing=no post_missing=no`):
  Skip the prompt-and-seed portion of C0.5 — both canonical slots are already
  configured (existing world, or a prior /start seeded them). Fall through to
  the on-demand-slot informational note below; do NOT exit C0.5 early.

IF either slot is missing:

  Output to user (informational notice, not a question). List ONLY the
  slots whose <prefix>_missing == "yes":
  ```
  Installing framework-essential convention structure(s):
    {if pre_missing == "yes":}
    - pre-execution.md (5 steps): Curriculum stage check, Pull latest,
      Fix scope coverage, Causal isolation, Dependency chain verification
    {if post_missing == "yes":}
    - post-execution.md (4 steps): Infrastructure health recording,
      Run testing circuits, Fresh-eyes code review, Commit and push

  These are installed by construction — framework invariants (Step ordering,
  fresh-eyes wiring, --author $MIND_AGENT filter, step count limit) are
  guaranteed. You can edit world/conventions/<slot>.md anytime to add
  domain layers.
  ```

  # CRITICAL — DO NOT switch back to LLM-authored "custom-from-scratch".
  # The verify-learning Section DC structural invariants (Step 1.5/1.75/2
  # ordering, fresh-eyes wiring, --author $MIND_AGENT filter, step-count
  # cap) hold ONLY because the template is installed verbatim by cp here.
  For each missing slot (pre-execution if pre_missing=="yes", post-execution
  if post_missing=="yes"):
    Bash: `cp core/config/templates/<slot>-default.md "$WORLD_DIR/conventions/<slot>.md"`
    Log: "Seeded $WORLD_DIR/conventions/<slot>.md from framework default
          (core/config/templates/<slot>-default.md)."

  AskUserQuestion (one prompt covering whichever slots were just installed).
  **DO NOT AUTO-ANSWER** — per C0.5 gate rule 1: even in auto mode, do NOT
  pick "no" silently. Surface the question and wait for an explicit response
  matching one of rule 3's authorization shapes.
  ```
  Optional: add domain-specific steps to the convention(s) just installed?
    - yes:  I'll ask what to add and append under '## Domain Additions'
    - no:   defaults installed; edit anytime
    - show-me-the-defaults: I'll display the installed files, then re-ask

  Note: domain additions use `## Step` headers that count toward the
  step-limit cap (currently 12). Keep additions tight — the framework
  defaults use 5 pre-execution + 4 post-execution steps already.
  ```

  IF "show-me-the-defaults":
    Bash: `bash core/scripts/world-cat.sh conventions/pre-execution.md && \
           bash core/scripts/world-cat.sh conventions/post-execution.md`
    Loop back to the AskUserQuestion above.

  IF "yes": (ask only for slots that were just installed in this run)
    IF pre_missing == "yes":
      AskUserQuestion: "What domain-specific steps to add to pre-execution?
      (free text, or 'none')"
        IF non-'none':
          Bash: `printf '\n## Domain Additions\n\n%s\n' "<user content>" >> "$WORLD_DIR/conventions/pre-execution.md"`

    IF post_missing == "yes":
      AskUserQuestion: "What domain-specific steps to add to post-execution?
      (free text, or 'none')"
        IF non-'none':
          Bash: `printf '\n## Domain Additions\n\n%s\n' "<user content>" >> "$WORLD_DIR/conventions/post-execution.md"`

    Log: "Domain additions appended under '## Domain Additions' header."

  IF "no":
    Log: "Defaults installed. Edit world/conventions/<slot>.md anytime to
          add domain layers."

  After seeding (regardless of yes/no/show), ALWAYS print the on-demand-slot
  informational note below.

Print on-demand-slot informational note (every C0.5 entry, regardless of
which slots were seeded):

```
Two additional Pattern B hook slots exist on-demand and were NOT seeded
automatically (they require domain-specific scripts to be meaningful):

  - world/conventions/signal-refresh.md
      Consumer: aspirations-precheck/SKILL.md Phase 0.5.0-pre
      Use when: the domain has a user-signal source to scan before goal
      scoring (inbound email, external queue, directive board count).

  - world/conventions/outcome-observation.md
      Consumer: aspirations-state-update/SKILL.md Step 8.12
      Use when: the domain has measurable real-world outcomes beyond
      goal-completion counts (CI pass rate, service health, business KPI).

Create either file later when you have something concrete to put in it.
The runtime call sites no-op when the file is absent.
See core/config/conventions/domain-hooks.md for the full slot catalog.
```

**C1. The Program** (all modes):
Read `world/program.md`. If empty or only whitespace:

```
What is **The Program** for this world?

The Program is the shared purpose — the overarching mission that all agents
in this world work toward. It lives in world/program.md and is shared across
every agent.

There is no default — The Program is entirely yours. The framework provides
the learning loop; The Program tells it WHAT to learn about and WHY.

Examples:
- "Build and ship the best project management tool in the market."
- "Research and synthesize machine learning papers into actionable knowledge."
- "Develop a multiplayer game with intelligent AI characters."

What should The Program be? (Or say "skip" to leave it blank for now —
you can write it later via `world/program.md`.)
```

- AskUserQuestion
- If user provides content (not "skip"): Write to `world/program.md`
- If `world/program.md` was already populated: display it briefly and proceed
