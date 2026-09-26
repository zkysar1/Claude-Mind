---
description: "A deep code closure may claim tests pass only after the area's FULL suite (run-full-suite.sh, gradlew, npm test); read the VERDICT first."
paths:
  - "core/scripts/**"
  - "core/tests/**"
  - "core/config/**"
  - "mind_api/**"
  - ".claude/skills/**"
  - ".claude/rules/**"
  - "pytest.ini"
---

<!--
  Path-scoped (g-115-6469) ONLY because core/scripts/full-suite-imperative-gate.{sh,py},
  a PreToolUse[Bash] hook on pytest / run-full-suite / gradlew, re-delivers this
  rule's behavioural heads at the moment of use: a path-scoped rule is NOT
  re-injected after a compaction, which is exactly when a closure can land.
  IF THAT HOOK IS EVER REMOVED OR DISABLED, UNSCOPE THIS RULE IN THE SAME CHANGE
  (pinned by test_full_suite_imperative_gate.py). Why: the rationale file below.
-->

# Run Full Suite After Deep Code Closure

The incidents and measurements behind every section live in
`core/config/rationale/run-full-suite-after-deep-code.md`, and dated per-run
records live in `core/config/run-full-suite-baselines.md`. Add evidence there,
never here.

## Principle

When closing a deep code goal that modifies production logic, "all tests pass"
must mean the FULL test suite for the module/package — not just the targeted
new tests written for the change. Targeted tests are necessary but not
sufficient: they confirm the new behavior is right, but they cannot detect
regressions in adjacent code paths that the change broke as a side effect.
Canonical failure (g-115-744 / g-115-746): the targeted test passed, an existing
`testSymmetry` test was broken by the change, and the regression shipped.

## The second axis: ENVIRONMENT (guard-1515)

Everything below this line is about breadth of TEST SELECTION. A regression can
satisfy that axis completely and still ship, because a suite result is a claim
about **one box's environment**, not about the code (g-335-264: a full Gradle
suite green on a box with a populated API key, red wherever it is unset).

**When a change reads any environment input — env var, credential, locale, TZ,
`$HOME`, a file outside the repo — a green suite on your box is not evidence
until you re-run the affected tests with that input ABSENT:**

```bash
env -u OPENAI_API_KEY -u GROQ_API_KEY ./gradlew test --no-daemon
```

Generalise per input, not per name: `env -u <VAR>` for each var the diff reads.
The unset case is the one CI runs and the one your box hides — a populated value
masks the failure, an absent one cannot mask a success. This axis is ORTHOGONAL
to test selection: a closure claiming "all tests pass" on an env-reading change
owes both.

## Scope

Applies when ALL of the following hold:

1. The goal's outcome class is `deep` (not routine).
2. The goal touched production code under one of:
   - **Mind framework**: `core/scripts/*.py`, `mind_api/src/*.py`,
     `core/scripts/*.sh` (production wrappers, not test scripts),
     `.claude/skills/*/SKILL.md` (skill pseudocode that scripts execute),
     `.claude/rules/*.md` (behavioral rules — qualitative review only).
   - **Product workspace** (`AGENT_WRITE_PATH` — sibling repos the agent
     is permitted to write to): any repo with uncommitted changes from
     this goal.
3. The closure is about to claim "all tests pass," "tests green,"
   "verification successful," or equivalent in Phase 5 verify.

The rule does NOT apply to pure documentation goals (changelog, journal,
tree node edits without script behavior changes) or routine closures
(simple presence checks).

## Which tier satisfies this rule (g-115-9602, 2026-09-10)

This rule says a deep-code closure owes more than its targeted tests. It never
said the only way to pay is the ~187-minute full run (USER DIRECTIVE: smaller and
self-serve for each agent — verbatim in the rationale). There are two tiers.
Choose with the rule below, not by reflex.

**FAST — `bash core/scripts/run-scoped-suite.sh`.** Runs only the tests that
reference what changed. Self-serve: your own box, no quiet window, no tree lock,
no queue, no peer coordination — safe beside a live daemon and from a worker
Body (it pins `STORAGE_BACKEND=local` itself and logs outside the synced tree).
The verdict is TRI-STATE and **an empty selection is NOT a pass**: `0 PASS |
1 FAIL | 2 INCONCLUSIVE | 3 setup`.

**THE FAST TIER SUFFICES when ALL FOUR hold:**
(a) verdict is PASS — a NON-EMPTY selection ran green;
(b) `unmapped_files` is empty — every changed file mapped to at least one test
    (`PASS_WITH_GAPS` does NOT qualify, which is why it exits 2);
(c) `selected_share_pct` is small — a hub change fanning out to a large share is
    the tier telling you to run the full one;
(d) the change touches none of the machinery in the next paragraph.

**THE FULL RUN IS GENUINELY REQUIRED when ANY holds:** the tier returns
INCONCLUSIVE for any reason (empty selection, an unmapped file, timeout); the
change touches `pytest.ini`, a `conftest.py`, `_paths.{py,sh}`, the storage
backend, or the runner scripts THEMSELVES (`run-full-suite.*`,
`run-scoped-suite.*`) — **a tier cannot certify a change to itself**; or a
release/promotion is being cut.

Record WHICH tier you ran and why, exactly as you would record a full run. A
fast PASS cited while `unmapped_files` is non-empty is a false closure claim in
precisely the way "all tests pass" from targeted tests alone is.

## Scope: THREE testpaths, not one (g-115-3748, 2026-07-31)

`pytest.ini` declares THREE testpaths: `core/scripts/tests`, `core/tests/gates`
and `mind_api/tests`. `bash core/scripts/run-full-suite.sh` resolves its roots
from that config, so a new test tree joins by being declared there, and it has
collected all three since 2026-08-20 (g-115-6942). Prefer it over every bare
`pytest <path>` command below: those are accurate for targeted runs and NOT
sufficient for a deep-code closure claim. Until 2026-07-31 the runner collected
only the first path: 1,448 gate and daemon-endpoint tests never ran, and 12 of
them were red.

Lasting lesson (guard-1760): the runner reports what it RAN, never what it
declined to look for. When a suite's scope is configurable, check the config
against the runner before trusting a green. `DEFERRED_TESTPATHS`
(announced-and-skipped, opt-in `RUN_DEFERRED=1`) stays for future trees; empty
is its designed end state.

Two traps that bite before any triage are stated once, in the method below: a
second run live at the same time (item 9) and a `VERDICT: GENUINE` that is false
(item 2).

## Live-Daemon Exception (own-cloud, 2026-05-31)

When a **live own-cloud daemon is serving autonomous agents on this repo**
(`mind_api/state/daemon.port` present + healthy), do NOT run the unrestricted
`pytest core/scripts/tests` suite. The daemon-lifecycle integration tests (e.g.
`test_daemon_orphan_prevention.py`, which spawns daemons against the real
`mind_api/state/`) hijack the live `daemon.port`, route the running agents onto
a transient `LocalBackend`, and leave local-only write residue (split-brain).

1. Run the daemon-SAFE suite, **prepending `STORAGE_BACKEND=local`**. The prefix
   is MANDATORY, not optional, whenever the box runs `STORAGE_BACKEND=own-cloud`:
   `STORAGE_BACKEND=local python -m pytest core/scripts/tests -q -m "not daemon_integration"`.
   - The `daemon_integration` marker (registered in `pytest.ini`) tags the tests
     that spawn REAL subprocess daemons deliberately and/or count system-wide
     `mind_api.src` processes. **It does NOT bound the set of tests that CAN spawn
     one.** Any daemon-backed wrapper reaches `rt_ensure_running` → rc=3 →
     `rt_spawn`, or `mind-api-start.sh` directly. With `RUNTIME_DIR` unset, that
     claims the SHARED `mind_api/state/daemon.port` and force-kills the live
     daemon. Both chokepoints now REFUSE the spawn when `PYTEST_CURRENT_TEST` is
     set and `RUNTIME_DIR` is not (g-115-3329), so a test that needs its own daemon
     MUST set `RUNTIME_DIR`.
   - Without the pin, the suite is NOT hermetic on an own-cloud box. A test
     subprocess inherits own-cloud through `env = os.environ.copy()`, and
     `OwnCloudBackend._s3_key` derives the S3 key from `customer_prefix+env_id+`
     filename, NOT from the `MIND_WORLD` tmp override. So a tmp fixture write
     lands on the PRODUCTION key and truncates the real store (2026-07-09).
     `STORAGE_BACKEND=local` keeps every tmp write on the tmp filesystem.
   - **Pin it for ANY test runner, not only pytest**: a bash aggregator OR a
     direct `python3 test_*.py` too. Bash aggregators that exec `main()`-style
     world-writing tests MUST pin it themselves (`run-asp-257-suite.sh`
     `export`s it at the top). The conftest autouse pin (g-115-1875) protects
     ONLY pytest-collected tests. See guard-955 and rb-2983.
2. Defer ONLY the `daemon_integration` subset to a quiescent window (agents
   stopped) or a separate clone / CI:
   `python -m pytest core/scripts/tests -q -m daemon_integration`.
   Narrate "daemon_integration subset deferred to quiescent window" — NOT "full
   suite deferred" (the rest ran).

`RUNTIME_DIR` (honored by `lifecycle.runtime_dir`, `mind-api-start.sh`'s
`RT_DIR`, and `owncloud_sync.py`) lets a test spawn an isolated daemon whose
`daemon.pid/port` live in a tmp dir. It does NOT make the system-wide
process-counting orphan test safe, so that one keeps the marker. This is a scoped
exception, not a repeal: the full unrestricted suite still runs whenever no live
daemon is present. Enforced by `guard-672`.

### Progress-visible invocation (g-115-1496, 2026-06-17)

> The METHOD, which is what a reader needs at the moment of use. The
> confirmations behind each item are in the rationale file; the dated per-run
> records are in `core/config/run-full-suite-baselines.md`.
>
> **1. READ THE `VERDICT` LINE FIRST, AND LET IT DECIDE WHETHER THE NUMBERS
> ABOVE IT MEAN ANYTHING.** A run can print a fully clean-looking
> `TOTAL: N passed, 0 failed, 0 errors` over per-chunk lines that ALL read
> `0 failed`, and still be
> `VERDICT: INVALID (contended) -- this number means NOTHING`. Per-chunk lines
> cannot be trusted; the verdict can.
>
> **2. `VERDICT: GENUINE` CAN BE FALSE — and a SMALL count is more suspicious,
> not less.** The verdict is fail-safe for `INVALID` and NOT for `GENUINE`. The
> positional profile buckets by pytest's `[NN%]`, but each chunk emits its own
> 0→100%. So a cluster confined to the tail, or to one chunk, is smeared flat
> and reported GENUINE with no reason at all (g-115-4336). Apply the guard-1448
> discriminators yourself on ANY non-zero count: **bucket by CHUNK, not by
> position, and re-run the worst-hit chunk's file list SOLO.** Green solo ⇒
> environmental. Check two things before triaging anything: are the failures
> confined to one chunk, and is each assertion a LOGIC mismatch or a bare
> process rc? The chunk-09 signature's cause is FIXED (g-115-5651). A fresh
> occurrence is a REGRESSION: re-run solo and file a NEW goal.
>
> **3. `INVALID` HAS TWO CAUSES AND CLIMBING THE LADDER ONLY FIXES ONE.** The
> other is log corruption: the sync layer replaces a log file while the writer
> still holds the old one, so a long write trickles into an orphaned copy.
> RESOLVED (g-115-6409): the default log dir moved off the fleet-synced tree to
> `<tmpdir>/ayoai-suite-run-<agent>`, so you pass nothing. `--print-out-dir` is
> a **`.py`** flag; on the `.sh` it rides into a REAL run that looks hung. Older
> builds: `--out /tmp/<non-synced-dir>`.
>
> ⚠ **The NUL-byte check is ONE-DIRECTIONAL. Any NULs ⇒ corruption; ZERO NULs is
> NOT evidence against it.** The common variant has a clean prefix, zero NULs and
> rc=0, byte-indistinguishable from a short run.
>
> ```bash
> for f in <logdir>/chunk-*.log; do n=$(tr -dc '\0' < "$f" | wc -c); \
>   [ "$n" -gt 0 ] && echo "$(basename $f): $n NUL"; done
> ```
>
> **4. THE CHUNK LADDER (8 → 12 → 16 → 20 → 24 → 28 → 32 → 36) IS A RETRY
> PROTOCOL, NOT A SETTING — AND IT IS NEVER INHERITABLE**: not from another
> agent, another box, or your own earlier run on the same machine, and it does
> not track partner count. Enter the ladder anywhere, read the VERDICT, and
> escalate only when it says to. Do not read a contended run's totals as a
> regression.
>
> **5. THE `TOTAL` LINE IS NOT A CROSS-RUN COMPARISON METRIC.** Judge by the
> FAILING FILE SET. The summary reports only `passed`, so xfail/xpass/skip sit
> silently outside it. `failed` and `errors` are the trustworthy fields; for a
> population figure use `--collect-only`.
>
> **6. `VERDICT: CLEAN` SCOPES TO THE CHUNKED PYTEST HALF ONLY.** It is not a
> whole-suite all-clear. Also `grep '^FAIL'` for the invisible (`main()`-style
> and shell) half and the domain half, which the runner reports separately.
>
> **7. WHEN THE VERDICT IS NOT CLEAN, RUN `--triage`.** It re-reads the chunk
> logs the run already wrote (no re-run) and chains position-bucket → solo
> re-run → **ownership**. Only reds that are both genuine AND unowned appear
> under FILE THESE. A red whose file, but not its test, is named by a goal lands
> in VERIFY: open it. `NOT RECORDED` in the `SCOPE` block is ignorance, never a
> pass.
>
> **8. NEVER PIPE THE RUNNER — not even a finished run.** A trailing pipe
> replaces the exit code with the pipe's (guard-1150), destroying the exit-2
> INVALID signal, and `| tail -40` discards the VERDICT line. Redirect to a file
> and Read it.
>
> **9. CHECK FOR A CONCURRENT RUN AS ITS OWN COMMAND:
> `bash core/scripts/proc-match.sh run-full-suite`.** NOT `pgrep`, which is
> absent on Windows/MSYS and has no `-a` on BSD/macOS. Its bracket idiom also
> misses a live run and matches an enclosing wrapper. Corroborate a "finished"
> reading with the log's mtime: a verdict-less tail plus a fresh mtime means
> STILL RUNNING.
>
> **10. RECORD `hostname` AND `uname -r` VERBATIM, NEVER A NICKNAME.** One
> nickname has named two different machines. A baseline you cannot attribute is
> a baseline you cannot trust.
>
> **11. BEFORE RECORDING A CROSS-BOX RED/GREEN SPLIT AS PORTABILITY, DIFF THE
> ENV.** Env-dependence reproduces cross-platform; genuine platform-dependence
> does not. That asymmetry is the whole discriminator, and it is one command.
>
> **12. RE-RUN A NAMED RED SOLO BEFORE TRIAGING FROM PROSE.** A prose red is a
> lead, not a finding. A solo re-run cannot falsify an IN-SUITE claim, because
> test-order pollution is real. One solo measurement is not a verdict either:
> repeat it before labelling anything GENUINE.
>
> **13. "PRE-EXISTING" IS NOT "TRACKED".** A failure that is not yours still
> needs an owner. Open the cited goal and confirm it names the failing TESTS; a
> shared file path is not ownership.

A long suite run has three traps that make a healthy run look hung. Know them
before you kill a run or file a false "suite hangs" blocker:

1. **Collection is silent for >50s** before the first result. "No output yet"
   in the first minute is NOT a hang.
2. **Do NOT pipe a live run through `tail`.** Pipe buffering can hold output
   until EOF (notably on Windows). Redirect to a file and Read that file directly,
   since the Read tool shows partial content mid-run. Use unbuffered flushes:
   ```
   STORAGE_BACKEND=local PYTHONUNBUFFERED=1 python -u -m pytest core/scripts/tests -m "not daemon_integration" \
     > /tmp/suite-$MIND_AGENT.log 2>&1
   ```
   Then Read `/tmp/suite-$MIND_AGENT.log` — NEVER a synced path (guard-6416).
   `-v` gives one line per test.
3. **A backgrounded run persists — don't trust a waiter or empty task-stdout to
   say otherwise.** Set any waiter bound LONGER than the run's measured runtime.
   Never conclude "hung/killed" from a waiter timeout or empty task-stdout alone:
   Read the redirect file (`verify-before-assuming.md`). Foreground-in-one-turn is
   also fine, because the Bash tool auto-backgrounds >2min commands but keeps
   them bound to the turn.

**Sanctioned pacing for an in-turn wait: `EXTERNAL_WAIT=1` (g-115-2678).**
NEVER launch the suite with `run_in_background`. The Bash `timeout` caps at
600000ms and kills the whole tree mid-chunk, leaving a log with no VERDICT that
is byte-identical to one still running (guard-6148). Detach instead:
`nohup env MIND_AGENT=.. MIND_SID=.. STORAGE_BACKEND=local bash
core/scripts/run-full-suite.sh > LOG 2>&1 < /dev/null &`. That shape commits you
to two things. First, the `< /dev/null`: an inherited never-EOF stdin degrades
the run into zero-shaped output (guard-5140). Second, POLLING via ScheduleWakeup,
because detaching forfeits the completion notification. That polling is
schedule-wakeup-correctness Anti-pattern D (an untracked external wait), NOT the
Anti-pattern A prohibition, which covers only harness-TRACKED jobs. Never combine
the two shapes (guard-3892). **A WORKER MUST NOT DETACH: it VOIDS the run. Use
trap 3's in-turn route** (`rationale/suite-run-voided-by-loop-merge.md`).
To pace an in-turn sleep, use `EXTERNAL_WAIT=1 bash
core/scripts/interruptible-sleep.sh <seconds>`. A BARE interruptible-sleep
registers no background job, so `background-jobs.sh has-pending` returns rc=1.
Stop-hook Gate 2.6 then BLOCKs the turn-end and the loop busy-spins.
`EXTERNAL_WAIT=1` registers a Tier-A `external-wait-sleep` job, so Gate 2.6
ALLOWs the turn-end and the sleep paces its full duration. Never pace a mid-goal
external wait with a bare sleep.

A true hang is bounded by `faulthandler_timeout = 600` +
`faulthandler_exit_on_timeout = true` in `pytest.ini` (g-115-1496). Any single
test that runs past 600s dumps all-thread tracebacks and aborts, so it fails loud.

### Live-Fleet Exception — chunk the run, or the result is garbage (g-115-3085, 2026-07-25)

Sibling to the Live-Daemon Exception above, and independent of it. Running the
whole suite in ONE process while the live fleet runs on the same Windows box
exhausts process/desktop-heap resources partway through. Spawns then fail with
**rc=3221225794 (`0xC0000142` STATUS_DLL_INIT_FAILED)**, even `git init`, and the
run reports hundreds of bogus failures that look completely real up close.

**Never conclude a regression from a large failure count without running these
two discriminators first:**

1. **Bucket failures by position in the run.** Progressive exhaustion shows
   ZERO failures early and 20%+ late. A genuine regression fails from the START,
   so an all-late profile is near-conclusive evidence of exhaustion, not code.
2. **Re-run the worst-hit file alone.** Green solo ⇒ the failures were
   environmental.

**Remedy: chunks in FRESH processes**, which `run-full-suite.sh` already does
(a single process cannot recover the handles; the manual recipe is in the
rationale). Or wait for a quiet window with the fleet stopped. Enforced by
`guard-1448`.

## Required Full-Suite Commands (per code area)

### Mind framework

| Path touched | Full-suite command | Pass criterion |
|---|---|---|
| `core/scripts/*.py` (non-test) | `bash core/scripts/run-full-suite.sh` (covers all three testpaths + the invisible and domain halves). The narrower `python -m pytest core/scripts/tests -q` is fine for a targeted re-run but is NOT sufficient for a closure claim — see § Scope: THREE testpaths. | exit code 0, all collected tests pass |
| `core/scripts/gates/capability.py`, `capability-gate.py`, or the defer→Unblock path in `aspirations.py` | ALSO run `bash core/scripts/tests/run-asp-257-suite.sh` — 4 of its 6 suites are `main()`-style files pytest collects 0 tests from, so pytest-green says NOTHING about them (g-115-2343 / rb-3678) | aggregator prints `6/6 suites passed` |
| Any change whose test coverage lives in a pytest-INVISIBLE file — a `main()`-style `.py` (no top-level `def test_`) **or any `.sh`, which pytest cannot collect at all**. List them with `bash core/scripts/tests/run-invisible-suites.sh --list`. | `bash core/scripts/tests/run-invisible-suites.sh` — dynamic population runner; known-reds are quarantined inline with their tracking goal IDs. `run-full-suite.sh` invokes it automatically (g-115-3957); run it directly only when you want the invisible half alone. | runner exits 0 (`N/N files passed, M quarantined`) |
| `mind_api/src/*.py` | `STORAGE_BACKEND=local python -m pytest mind_api/tests -q -m "not daemon_integration"` — the fast targeted arm. `run-full-suite.sh` also collects this tree (g-115-6942), so a green full-suite run IS evidence about `mind_api/src`. | exit 0 |
| `core/scripts/*.sh` (production wrapper) | Whatever the wrapper's daemon endpoint suite covers — typically `python -m pytest core/scripts/tests -q -k <endpoint>` | exit 0 |
| `.claude/skills/*/SKILL.md` | Re-read the edited pseudocode + `bash core/scripts/domain-leak-check.sh`; if the change alters skill BEHAVIOR (not just prose), also `/verify-learning` for cross-skill grep checks. For BULK prose edits (extraction/reflow passes) ALSO run `py -3 core/scripts/line-class-diff-check.py <paths>` — report-only, per-class set-diff vs HEAD (g-115-7706: a bulk pass relocated front matter, blockquotes and bold directives while targeted tests, domain-leak-check AND the re-read were all green). Do NOT use `skill-evaluate.sh` here: it rates RUNTIME skill-on-goal performance, not a static SKILL.md edit. | re-read confirms intent; domain-leak-check clean; verify-learning passes if behavior changed; line-class diff reports no class removals |
| `.claude/rules/*.md` | No automated check — re-read the rule and confirm wording matches intent | manual review |
| `core/config/*.yaml` / `core/config/*.md` | Re-parse via affected consumers — `bash core/scripts/<consumer>.sh --dry-run` if available, otherwise `python -c "import yaml; yaml.safe_load(open('<path>'))"` | parse succeeds, no schema break |
| **External domain + meta paths** — `world/scripts/**`, `world/conventions/**`, `meta/**`. Neither git-tracked framework nor a sibling product repo. | `STORAGE_BACKEND=local python3 -m pytest "$WORLD_PATH/scripts/tests" -q` (pin mandatory — guard-955), or the world's `run-domain-tests.sh` hook. **ENFORCED at close (g-353-75)**: `domain-suite-gate.py` in `iteration-close.sh do_verify` refuses `status=completed` when a code file under `$WORLD_PATH/scripts` is newer than the goal's claim and that suite is NEWLY red (per-box ratchet) or uncollectable (`--override-domain-suite "<why>"`, logged). **`full-suite-recommender.sh` CANNOT SEE THESE PATHS** (external, gitignored; it detects changes via git): its `no code changes detected` there means "I cannot see", not "nothing changed" — say the recommender was *blind*, not quiet (guard-1947; read-side inverse of rb-1699). | domain pytest exits 0; shell units pass except pre-existing environment-gated quarantines, which must be named |

### Product workspace (sibling repos under `AGENT_WRITE_PATH`)

| Repo type | Full-suite command | Pass criterion |
|---|---|---|
| Java / Gradle | `./gradlew test --no-daemon` | `BUILD SUCCESSFUL` |
| Node.js / npm | `npm test` | exit code 0 |
| Python / pytest | `python -m pytest tests/ -v` | exit code 0 |
| Lua / Lune (if `tests/` exists) | `lune run tests/` | exit code 0 |
| (other runtimes) | Whatever the repo's CLAUDE.md or README documents as the full test command | exit code 0 |

Note: `world/conventions/post-execution.md` Step 2.b.1 already mandates
the product-repo full-suite as a pre-push build gate — but Step 2 fires
AFTER commit, when verify already claimed "all tests pass." This rule
fires BEFORE Phase 5 verify, in the window where false claims would land.

## Advisory Enforcement

`core/scripts/full-suite-recommender.sh` emits a banner during
`aspirations-execute` Phase 4 close (after the primary action, before
`phase_4_completed_at`). The banner lists detected file changes per
area and the recommended full-suite commands. The gate is ADVISORY
ONLY — it exits 0 unconditionally. The LLM is expected to act on the
banner BEFORE Phase 5. The posture mirrors the pre-apply consult gate
(g-115-826): a suite run is a deliberate LLM choice, not an automatic forced run
on every deep closure.

## Anti-patterns

- "All tests pass" in a Phase 5 verify narrative when only the targeted
  new test was run.
- Closing deep on `core/scripts/<wrapper>.sh` after running only the
  daemon roundtrip for that one endpoint — the suite catches regressions
  in OTHER endpoints the wrapper interacts with.
- Closing deep on a Java change after running `./gradlew test --tests
  <ChangedTestClass>` (single test class) — the full suite catches
  symmetry / contract tests in OTHER classes.
- Skipping the recommender banner because "I ran tests already" — if
  the banner asks for `pytest core/scripts/tests`, that exact invocation
  is the signal, not whatever subset ran during execution.
- Auto-running the suite from the gate (out of scope — deliberate LLM
  decision, advisory only).

## Cross-references

- `g-115-744`, `g-115-746` — originating incident (testSymmetry regression
  shipped because closure trusted targeted-only tests).
- `g-115-858` — the Idea goal that surfaced this rule.
- `world/conventions/post-execution.md` Step 2.b.1 — sibling rule for
  product-repo pre-push build gate (fires after commit; this rule fires
  before Phase 5 verify).
- `.claude/rules/pre-completion-review.md` — re-read your own work before
  declaring done; this rule is the test-suite analog.
- `.claude/rules/verify-before-assuming.md` — "all tests pass" without
  the full-suite run is an unverified positive claim.
- `core/scripts/full-suite-recommender.sh` / `.py` — the advisory gate.
- `core/config/rationale/run-full-suite-after-deep-code.md` — the incidents and
  measurements behind every section (extracted by g-353-118).
- `core/config/run-full-suite-baselines.md` — the dated per-run ledger
  (box, kernel, chunk rung, VERDICT, TOTAL) extracted from this rule
  2026-08-17. The rule keeps the METHOD; the ledger keeps the EVIDENCE. Add
  run records there, never here.
