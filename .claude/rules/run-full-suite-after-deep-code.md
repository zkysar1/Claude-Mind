# Run Full Suite After Deep Code Closure

## Principle

When closing a deep code goal that modifies production logic, "all tests pass"
must mean the FULL test suite for the module/package — not just the targeted
new tests written for the change. Targeted tests are necessary but not
sufficient: they confirm the new behavior is right, but they cannot detect
regressions in adjacent code paths that the change broke as a side effect.

The failure mode (canonical: g-115-744 / g-115-746, 2026-05-14): a deep code
goal modified production logic (`Math.max(b, raw)` zero-clamp), the targeted
new test for the change passed, and the closure narrated "All tests pass."
A separate existing test (`testSymmetry`) was actually broken by the change
— it would have caught that `Math.max(b, raw)` was too aggressive, and the
correct fix was a conditional (`if raw < 0: raw = b`). The regression
shipped because the closure trusted targeted-only test results.

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

## Live-Daemon Exception (own-cloud, 2026-05-31)

When a **live own-cloud daemon is serving autonomous agents on this repo**
(`mind_api/state/daemon.port` present + healthy), do NOT run the full
`pytest core/scripts/tests` suite to satisfy this rule. The daemon-lifecycle
integration tests (e.g. `test_daemon_orphan_prevention.py`, which spawns
subprocess daemons against the real `mind_api/state/`) hijack the live
`daemon.port`, route the running agents onto a transient `LocalBackend`, and
leave local-only write residue (split-brain). This caused two daemon storms on
2026-05-31 (the second was an agent running this suite to verify its own deep-code change).

Resolution while a live daemon is present (B16 durable fix, landed 2026-06-01):
1. Run the daemon-SAFE full suite, **prepending `STORAGE_BACKEND=local`** (see
   the own-cloud S3-key-collision hazard below — this prefix is MANDATORY, not
   optional, whenever the box runs `STORAGE_BACKEND=own-cloud`):
   `STORAGE_BACKEND=local python -m pytest core/scripts/tests -q -m "not daemon_integration"`.
   The `daemon_integration` marker (registered in `pytest.ini`) tags the tests
   that spawn REAL subprocess daemons **deliberately** and/or count system-wide
   `mind_api.src` processes — currently just `test_daemon_orphan_prevention.py`.
   **The marker does NOT bound the set of tests that CAN spawn one.** Any test
   invoking a daemon-backed wrapper reaches `rt_ensure_running` → rc=3 →
   `rt_spawn`, or `mind-api-start.sh` directly; with `RUNTIME_DIR` unset either
   path claims the SHARED `mind_api/state/daemon.port` and force-kills the live
   daemon. Observed 2026-07-26: an unmarked, ostensibly-hermetic test
   (`test_post_state_update_metric_gate_category.py`) recycled the live daemon
   out from under the running fleet — its tmp `local-paths.conf` did not isolate
   it, because `.mind-data/` outranks the conf in the resolution chain. Both
   chokepoints now REFUSE the spawn when `PYTEST_CURRENT_TEST` is set and
   `RUNTIME_DIR` is not (g-115-3329), so a test needing its own daemon MUST set
   `RUNTIME_DIR` — the failure is loud instead of a silent fleet-wide repoint.
   Excluding the marked tests, the rest of the suite is hermetic in its
   filesystem resolution (the in-process `_daemon_fixture.py` / `running_daemon`
   fixtures bind a thread-local daemon in a tmp project root and set `RT_DIR`
   for their subprocesses) and is safe to run with a live daemon present —
   **but ONLY with `STORAGE_BACKEND=local` prepended (as shown above).** On an
   own-cloud box (`STORAGE_BACKEND=own-cloud`, this repo's default when a live
   daemon serves agents) the "hermetic" claim is FALSE: tests that seed a
   tempfile world and write via a subprocess (e.g.
   `test_defer_to_unblock_integration.py`) inherit own-cloud (their subprocess
   spawn does `env = os.environ.copy()`), and `OwnCloudBackend._s3_key` derives
   the S3 key from `customer_prefix+env_id+`filename — NOT the `MIND_WORLD`
   tmp-dir override — so the tmp write collides on the PRODUCTION S3 key and
   truncates the real store. This happened 2026-07-09: `world/aspirations.jsonl`
   was truncated from 22 aspirations/1366 goals to a lone `asp-555` fixture
   (recovered from a `.history` snapshot via a fenced re-PUT).
   `STORAGE_BACKEND=local` forces LocalBackend so every tmp write stays on the
   tmp filesystem.

   **"Prepend to pytest" is too narrow — pin it for ANY test runner.** The
   2026-07-09 truncation did NOT come from `pytest core/scripts/tests`:
   `test_defer_to_unblock_integration.py` is a `main()`-style file with zero
   `test_` functions, so pytest collects 0 from it and never runs it. The real
   runner was the bash aggregator `core/scripts/tests/run-asp-257-suite.sh`
   (suite 6/6 = `python3 …/test_defer_to_unblock_integration.py`), invoked to
   validate a capability-gate change. So pin `STORAGE_BACKEND=local` for pytest,
   a bash aggregator, OR a direct `python3 test_*.py`. Bash aggregators that exec
   `main()`-style world-writing tests MUST pin it themselves
   (`run-asp-257-suite.sh` now `export`s it at the top) — a conftest autouse
   fixture (g-115-1875) protects ONLY pytest-collected tests, never
   `main()`-style files run outside pytest. (~18 pytest-collected world-writers
   in `core/scripts/tests` do `os.environ.copy()` and are S3-collision-capable
   under own-cloud; the conftest pin covers those.) See guard-955, rb-2983, and
   `exp-owncloud-s3-collision-truncation-2026-07-09`.
2. Defer ONLY the `daemon_integration` subset to a quiescent window (agents
   stopped) or a separate clone / CI:
   `python -m pytest core/scripts/tests -q -m daemon_integration`.
   Narrate "daemon_integration subset deferred to quiescent window" — NOT "full
   suite deferred" (the rest ran).

`RUNTIME_DIR` (honored by `lifecycle.runtime_dir`, `mind-api-start.sh`'s
`RT_DIR`, and `owncloud_sync.py`) lets a future test spawn an isolated daemon
whose `daemon.pid/port` live in a tmp dir, so a spawn-and-check-own-files test
need not hijack the live daemon's `mind_api/state`. It does NOT make the
system-wide-process-counting orphan test safe (that counts by command line, not
runtime dir) — hence that one keeps the marker.

This is a scoped exception, not a repeal — the full unrestricted suite still
runs whenever no live daemon is present. Enforced by `guard-672`.

### Progress-visible invocation (g-115-1496, 2026-06-17)

> **RE-BASELINED 2026-07-26 (g-115-3085 Layer 2 landed, alpha). The 2026-06-17
> AND 2026-07-25 figures are both HISTORICAL — do not compare against either.**
>
> | | 2026-06-17 | 2026-07-25 | 2026-07-26 | **2026-07-27 (cc-04, Linux)** |
> |---|---|---|---|---|
> | tests run | 2,234 | 5,226 | 5,969 | **6,223** |
> | passed | 2,231 | 5,199 | 5,937 | **6,223** |
> | failed | 2 | 20 | 32 | **0** |
> | errors | 0 | 2 | 0 | **0** |
> | run completes? | yes | **no** — needed `--ignore`, a chunk died at 51% | yes, all 6 chunks 100% | **yes, all 4 chunks, VERDICT: CLEAN** |
>
> **2026-07-27 (g-115-3471, alpha on cc-04/Linux): all 12 files the 07-26 entry
> named as failing PASS here** — re-run explicitly, 91 passed / 1 skipped / 0
> failed. That covers both the 6 called GENUINE and the 6 called newly-visible.
> Do NOT read this as "the 07-26 entry was wrong": **that entry does not record
> which box or OS it measured**, and its own root-cause narrative is about
> Windows `CreateProcess`/System32/WSL mechanics, so the two runs may simply be
> different platforms. Unmeasured by me: whether those 12 still fail on a Windows
> box. Useful for g-115-3180's triage either way — a failure that reproduces on
> one platform and not another is a portability finding, not a broken test.
>
> **Record the box and OS with every future baseline row.** The inability to
> reconcile 32-failed with 0-failed comes entirely from that field being absent,
> and a baseline you cannot attribute is a baseline you cannot trust.
>
> **The TOTAL line is not a cross-run comparison metric — this is why the rule
> above says judge by FAILING FILE SET, never the count.** Measured the same day,
> same tree, ~40 minutes apart: a 4-chunk run reported 6,223 passed and an 8-chunk
> run reported 6,156, while `--collect-only` counted 6,234 tests across 513 files.
> Those three numbers do not reconcile. The runner's summary reports only
> `passed`, so xfail/xpass/skip (chunk logs show `X`/`x` markers) are silently
> outside it, and the residual still does not close. Do NOT read a moved TOTAL as
> tests appearing or vanishing, and do NOT quote it as a baseline others will
> diff against. `failed` and `errors` are the trustworthy fields; for a
> population figure use `--collect-only`, which counts one thing and counts it
> the same way every run.
>
> **A CLEAN verdict under a live fleet is possible but not reliable — re-run with
> more chunks rather than reading a contended run.** Same tree, three runs in one
> hour: 4 chunks CLEAN, 4 chunks **INVALID (contended)** with chunk 02 stopping at
> 96%, then 8 chunks CLEAN. The INVALID run's per-chunk line read
> `chunk 02: 1799 passed, 0 failed, 0 errors` and looked completed; only the
> runner's own exit-2 classification caught that it never finished. Trust the
> VERDICT, not the per-chunk lines, and reach for `--chunks 8` before concluding
> anything about the tree. (g-115-3471, alpha/cc-04.)
>
> **The environmental-timeout class is GONE, and the previous entry's guidance is
> now REVERSED.** The 2026-07-25 baseline told you to treat failures in 9 named
> files as "a machine signal, NOT a code regression." That was true then and is
> FALSE now. Root cause was found and fixed: a bare `"bash"` argv[0] resolves via
> `CreateProcess`, which searches System32 **before** PATH, reaching the WSL
> launcher and blocking forever on a wedged `LxssManager`. Swept out of 12
> production sites plus the test side. Measured on this run: **0 occurrences of
> `TimeoutExpired` or `assert 124 == 0`** anywhere — the exact signature that
> accounted for all 20 prior failures. `test_monitor_tick`, `test_init_backfill`
> and `test_history_vacuum_archive` are now fully **GREEN**.
>
> **So: do NOT excuse a failure in those files as environmental any more.** The 6
> still failing (`test_pending_deploys_gate`, `test_pre_apply_consult_gate_scope`,
> `test_pending_deploys_stop_hook`, `test_iteration_push`,
> `test_infra_streak_dedup_sh`, `test_git_merge_ayoai_ledger`) carry no timeout
> signature and are GENUINE — triage them, don't dismiss them.
>
> **Why failures rose 20 → 32 while the box got healthier**: +743 tests that had
> never executed now run. Judge by FAILING FILE SET, never the count. The newly
> visible failures are pre-existing, not regressions — the provision-from-vault
> family (`test_provision_from_vault_agent_scope`, `..._default_out`,
> `test_provision_github_from_vault`), plus `test_owncloud_pull_fleet`,
> `test_retrieve_as_of_endpoint_e2e`, `test_retrieve_daemon_readonly_false`.
> Triage tracked in g-115-3180.
>
> **`--ignore=...test_provision_github_from_vault.py` is NO LONGER REQUIRED.**
> That file now runs to completion. Use `bash core/scripts/run-full-suite.sh`,
> which pins `STORAGE_BACKEND=local`, excludes `daemon_integration`, chunks into
> fresh processes, and returns **exit 2 = INVALID/contended** so a resource-starved
> run can never be mistaken for a pass or a regression.

The daemon-safe full suite takes ~32min (measured: 1916s; 2231 passed / 2 failed
/ 1 skipped over 2234 selected). The runtime concentrates in a handful of
subprocess/integration tests that shell out to real git/bash/filesystem ops
under OneDrive contention — NOT primarily the daemon round-trips one might
assume. The slowest 20 sum ~880s (~46% of total) over <1% of tests: `test_promote`
seed-preflight/PR dry-runs (139s + 135s), `test_utilization_stats` real-repo
audit (77s), `test_orphan_root_sweep_mode_d_integration` filesystem scans (~180s
across 5), `test_post_state_update_gate_committed_files_only` daemon round-trips
(~60s across 3). Three traps make a healthy-but-slow run look hung — know them
before you kill a run or file a false "suite hangs" blocker:

1. **Collection is silent for >50s** before the first result (heavy
   module-level imports across 265 files). "No output yet" in the first minute
   is NOT a hang — wait past collection before suspecting trouble.
2. **Do NOT pipe a live run through `tail`** — `tail -f` (and most pipe
   buffering) holds output until EOF on Windows, so you see nothing until the
   run finishes, defeating the point. Instead redirect to a file and Read that
   file directly (the Read tool shows partial content mid-run), forcing
   unbuffered flushes so per-test dots land immediately:
   ```
   STORAGE_BACKEND=local PYTHONUNBUFFERED=1 python -u -m pytest core/scripts/tests -m "not daemon_integration" \
     > agents/<agent>/temp/suite.log 2>&1
   ```
   Then Read `agents/<agent>/temp/suite.log` to watch progress (add `-v` for one
   line per test instead of dots).
3. **A backgrounded run persists — don't trust a waiter or empty task-stdout to
   say otherwise.** Under g-115-1496 the suite was backgrounded and ran to
   completion (1916s) across turns — it was NOT killed. But a bounded waiter
   loop timed out at ~12.5min ("may be hung") because the suite needs ~32min,
   and the background task's own stdout looked empty because output went to the
   redirect file. Both signals falsely read as "dead." Ground truth was the
   redirect file, which accumulated steady progress the whole time. So: set any
   waiter bound LONGER than the measured ~32min runtime, and never conclude
   "hung/killed" from a waiter timeout or empty task-stdout alone — Read the
   redirect file (`verify-before-assuming.md`: one signal is not enough for a
   negative conclusion). Foreground-in-one-turn is also fine (the Bash tool
   auto-backgrounds >2min commands but keeps them bound to the turn).

4. **Sanctioned pacing for an in-turn wait: `EXTERNAL_WAIT=1` (g-115-2678).**
   The PRIMARY path is to background the suite (`run_in_background`) and END the
   turn — the harness auto-notifies on completion, so no polling and no sleep is
   needed (guard-1230). But if you deliberately pace with a bounded in-turn
   sleep, use the sanctioned flag: `EXTERNAL_WAIT=1 bash
   core/scripts/interruptible-sleep.sh <seconds>`. A BARE interruptible-sleep
   registers no background job, so `background-jobs.sh has-pending` returns rc=1,
   stop-hook Gate 2.6 BLOCKs the turn-end, and the loop busy-spins (~20 turns
   over a 32min wait — the incident that motivated the flag). `EXTERNAL_WAIT=1`
   registers a Tier-A `external-wait-sleep` job so Gate 2.6 ALLOWs the turn-end
   and the sleep paces its full duration. Never pace a mid-goal external wait
   with a bare sleep.

The hang itself is now bounded by `faulthandler_timeout = 600` +
`faulthandler_exit_on_timeout = true` in `pytest.ini` (g-115-1496): any single
test exceeding 600s (10min — well past the 139.61s slowest legit test) dumps
all-thread tracebacks and aborts the process, so a true hang fails loud with a
stack pointing at the stall instead of buffering forever.

### Live-Fleet Exception — chunk the run, or the result is garbage (g-115-3085, 2026-07-25)

Sibling to the Live-Daemon Exception above, and independent of it. Running the
~5,200-test suite in ONE process while the live fleet is running on the same
Windows box exhausts Windows process/desktop-heap resources partway through.
Spawns then fail with **rc=3221225794 (`0xC0000142` STATUS_DLL_INIT_FAILED)** —
even `git init` fails — and the run reports hundreds of bogus failures.

Measured: one contended run reported **564 failed / 4,672 passed**. The same
tree, re-measured properly, was clean. `test_release.py` alone accounted for 37
of those failures and passes **88/88 when run by itself**.

**Never conclude a regression from a large failure count without running these
two discriminators first** — the failures look completely real up close:

1. **Bucket failures by position in the run.** Progressive exhaustion shows
   ZERO failures early and 20%+ late. Measured distribution of the 564: 0
   failures across the first 1,368 tests, then 19–27% in the final decile. A
   genuine regression fails from the START (changed scripts are used
   throughout), so an all-late profile is near-conclusive evidence of
   exhaustion, not code.
2. **Re-run the worst-hit file alone.** Green solo ⇒ the failures were
   environmental.

**Remedy — run the suite as ~4 sequential chunks in FRESH processes**, which
resets accumulated handles per chunk (a single process cannot recover them):

```bash
ls core/scripts/tests/test_*.py | sort > /tmp/all-tests.txt
split -n l/4 -d /tmp/all-tests.txt /tmp/chunk-
for c in 00 01 02 03; do
  STORAGE_BACKEND=local python -m pytest $(cat /tmp/chunk-$c | tr '\n' ' ') \
    -q -m "not daemon_integration" > /tmp/chunk-$c.log 2>&1
  tail -1 /tmp/chunk-$c.log
done
```

Or wait for a quiet window with the fleet stopped. Enforced by `guard-1448`.

## Required Full-Suite Commands (per code area)

### Mind framework

| Path touched | Full-suite command | Pass criterion |
|---|---|---|
| `core/scripts/*.py` (non-test) | `cd PROJECT_ROOT && python -m pytest core/scripts/tests -q` | exit code 0, all collected tests pass |
| `core/scripts/gates/capability.py`, `capability-gate.py`, or the defer→Unblock path in `aspirations.py` | ALSO run `bash core/scripts/tests/run-asp-257-suite.sh` — 4 of its 6 suites are `main()`-style files pytest collects 0 tests from, so pytest-green says NOTHING about them (they sat red 3 days undetected, masking a real NameError — g-115-2343 / rb-3678) | aggregator prints `6/6 suites passed` |
| Any change whose test coverage lives in a `main()`-style file (69 of the `test_*.py` files here — enumerate with `bash core/scripts/tests/run-invisible-suites.sh --list`) | `bash core/scripts/tests/run-invisible-suites.sh` — dynamic population runner over every pytest-invisible file; known-reds are quarantined inline with their tracking goal IDs (g-115-2349 baseline sweep found 9 silent reds) | runner exits 0 (`60/60 files passed, N quarantined`) |
| `mind_api/src/*.py` | `python -m pytest core/scripts/tests -q` (runtime is exercised by daemon-aware wrappers in core/scripts/tests) | exit 0 |
| `core/scripts/*.sh` (production wrapper) | Whatever the wrapper's daemon endpoint suite covers — typically `python -m pytest core/scripts/tests -q -k <endpoint>` | exit 0 |
| `.claude/skills/*/SKILL.md` | Re-read the edited pseudocode + `bash core/scripts/domain-leak-check.sh`; if the change alters skill BEHAVIOR (not just prose), also `/verify-learning` for cross-skill grep checks. (Do NOT use `skill-evaluate.sh` here. A bare `skill-evaluate.sh <skill-name>` errors `unknown subcommand`: it needs a subcommand (read/report/underperforming/score), and `score --skill <s> --goal <g>` rates RUNTIME skill-on-goal performance, not a static SKILL.md edit.) | re-read confirms intent; domain-leak-check clean; verify-learning passes if behavior changed |
| `.claude/rules/*.md` | No automated check — re-read the rule and confirm wording matches intent | manual review |
| `core/config/*.yaml` / `core/config/*.md` | Re-parse via affected consumers — `bash core/scripts/<consumer>.sh --dry-run` if available, otherwise `python -c "import yaml; yaml.safe_load(open('<path>'))"` | parse succeeds, no schema break |

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
banner BEFORE Phase 5.

The advisory posture mirrors the pre-apply consult gate (g-115-826):
visibility beats fail-loud here, because (a) running a 60-test Python
suite or a `./gradlew test --no-daemon` is a 30s–5min wall-clock cost
that should be a deliberate LLM choice, not an automatic forced run on
every deep closure; (b) some deep closures are documentation-only
("modified SKILL.md but the change is pure narrative") where the suite
add no signal.

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
