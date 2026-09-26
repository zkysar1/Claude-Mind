# Rationale: Run Full Suite After Deep Code Closure

Referenced from `.claude/rules/run-full-suite-after-deep-code.md`. Holds the
incidents and measurements behind that rule's imperatives. They were moved out of
the rule on 2026-09-24 (g-353-118, hot-path diet) so that the rule carries only
what a reader needs at the moment of use. **Nothing here is a new instruction.**
Every imperative stayed in the rule, and the rule is authoritative where the two
differ. Dated per-run records (box, kernel, chunk rung, VERDICT, TOTAL) live in a
third place, `core/config/run-full-suite-baselines.md`.

The text below is the rule's prose as it stood at extraction (git blob
`e0798b5d33b6` at commit `355f4d5a2907`, 43,623 B). It is kept verbatim where
possible, so a reader can check any condensed sentence in the rule against its
source (`git show e0798b5d33b6`).

## Why the rule is path-scoped (the hook it depends on)

Path-scoped (g-115-6469). At 37 KB this was the single largest item in the
fixed per-turn preamble — 12% of all rule bytes — paid by every agent on every
turn whether or not it would ever run a suite. The globs are this rule's own
Scope section: it applies when a deep goal TOUCHED production code, and those
are the surfaces.

SCOPING THIS ONE WOULD HAVE BEEN A REGRESSION ON ITS OWN, and that is the part
to understand before touching it. A path-scoped rule loads when a matching file
is touched and is NOT re-injected after a compaction. The moment this rule
matters most is the CLOSURE — which can land in a turn after an autocompact,
with the rule absent and "all tests pass" about to be written. Scoping alone
would have removed the rule from exactly the turn it exists to govern.

So it is scoped only because the imperative now has a second carrier that does
not depend on the preamble at all: core/scripts/full-suite-imperative-gate.{sh,py}
is a PreToolUse[Bash] hook that fires on the COMMAND (pytest / run-full-suite /
gradlew) and delivers the five behavioural heads — VERDICT-first,
GENUINE-can-be-false, the ladder is a retry protocol, never pipe the runner,
CLEAN scopes to the pytest chunks only — plus the guard-955
STORAGE_BACKEND=local requirement. Verified firing live in-session, not merely
unit-tested.

IF THAT HOOK IS EVER REMOVED OR DISABLED, UNSCOPE THIS RULE IN THE SAME CHANGE.
The two are one mechanism. Pinned by test_full_suite_imperative_gate.py, which
asserts the hook is registered in .claude/settings.json — a gate nothing calls is
indistinguishable from one that always passes (guard-1943). See
core/config/conventions/rules-loading.md.

## The canonical incident (g-115-744 / g-115-746, 2026-05-14)

A deep code goal modified production logic (`Math.max(b, raw)` zero-clamp), the
targeted new test for the change passed, and the closure narrated "All tests
pass." A separate existing test (`testSymmetry`) was actually broken by the
change — it would have caught that `Math.max(b, raw)` was too aggressive, and the
correct fix was a conditional (`if raw < 0: raw = b`). The regression shipped
because the closure trusted targeted-only test results.

## Why the ENVIRONMENT axis exists (guard-1515, g-335-264)

Measured (g-335-264): a deep change added a pre-dispatch guard reading
`System.getenv` for two API keys. The FULL Gradle suite ran — 4523 passed, 0
failed, BUILD SUCCESSFUL — and it pushed (96d8cbf). Two test classes point the
service at a local stub and need no real credential, but the guard runs before
dispatch regardless, so with those vars UNSET they fail. The author's box had a
populated key, so the suite was green; CI does not, so CI would have been red.
The regression broke 6 tests and was caught minutes later by an unrelated spark
(83d9c8f), not by the rule.

## Why there are two tiers (g-115-9602, 2026-09-10)

The rule never said the only way to pay is the ~187-minute full run, and that
reading had been stopping agents for hours. USER DIRECTIVE, verbatim: *"We need
it much smaller, and self serve for each agent. WE cannot have each of our agents
pausing for 4 hours after each deep goal... I will find 3 or 4 agents all running
this test and doing nothing while waiting."*

The fast tier (`run-scoped-suite.sh`), measured on a live box with the fleet
running: 5 of 1,442 test files in 0.3s, 22 of 1,443 in 8.2s.

## Why the scope names THREE testpaths (g-115-3748, 2026-07-31)

Every `pytest core/scripts/tests` invocation in the rule names **one of the three
testpaths `pytest.ini` declares**. The other two are `mind_api/tests` and
`core/tests/gates`. Until 2026-07-31 `run-full-suite.sh` collected only the
first, so an agent could follow the rule perfectly, read `VERDICT: CLEAN`, and
have executed **zero** gate tests and zero daemon-endpoint tests.

Measured that day: 109 files / **1,448 tests** never ran, and **12 of them were
RED** — 5 in `core/tests/gates` (one for 36 days) and 7 in `mind_api/tests`.
Being red is the smaller half. These are the GATE and daemon-endpoint suites:
the layer the framework trusts to refuse bad writes and to keep CLI/daemon output
in parity. An unverified enforcement layer fails silently and upward.

The runner now resolves its roots from `pytest.ini` `testpaths` rather than a
hardcoded dir, so a future test tree joins the suite by being declared in the
config — no edit to the rule, and no second source of truth to drift (that drift
IS this defect: the runner shipped 2026-07-26, five weeks after the config
already declared three paths).

**`mind_api/tests` has been IN the chunked pool since 2026-08-20 (g-115-6942).**
History, kept because its lessons generalize: the tree spent 2026-07-31→08-20 in
`DEFERRED_TESTPATHS` (announced-and-skipped; opt-in `RUN_DEFERRED=1`) after
failing en masse at end-of-invocation — 411 reds at rung 16, 271 at rung 20 —
while passing alone; neither ladder escalation nor an own process fixed it. The
cause landed as **g-115-5651**: `get_backend()` memoizes `_ACTIVE_BACKEND`
process-wide while conftest restored only the env VAR, so one own-cloud test
poisoned every later test in its process. The reset fixture now lives in BOTH
test-tree conftests (the mind_api mirror closed the mixed-chunk vector), and the
fold-back was accepted on measurement (cc-10, 6.8.0-137-generic, 2026-08-20):
standalone 1,386/1,386 green; own-process at end-of-invocation green; folded
acceptance run 16,099 passed / 5 failed with every red pre-owned and none in
`mind_api/tests`. Four genuine reds the measurement surfaced were fixed, not
skipped (set_at daemon/CLI parity port, claim-sid harness pin, citation lane pin,
conftest MIND_SID coverage).

**Correction made at extraction.** The rule's Mind-framework table still said
`run-full-suite.sh` covered "NOT `mind_api/tests`", which contradicted the
paragraph above. Re-measured 2026-09-24 (alpha, cc-04): `DEFERRED_TESTPATHS =
set()` at `core/scripts/run-full-suite.py:104`, and `pytest.ini` declares
`mind_api/tests`, `core/tests/gates` and `core/scripts/tests`. The row now says
the runner covers all three.

## Why `proc-match.sh` and not `pgrep`

The concurrent-run trap was found on this rule's own history: **two
`run-full-suite.sh` invocations running CONCURRENTLY** (measured 2026-07-31, ~11
min overlapped — the chunked-half `TOTAL:` line makes a run look finished while
post-chunk phases are still writing).

`pgrep -af "[r]un-full-suite"` stood in the rule until 2026-08-31 and ran on ONE
of three platforms: Windows/MSYS has no pgrep (measured — `pkill` IS present, so
the gap is invisible at a glance), BSD/macOS lacks `-a`. The obvious Windows
fallback is worse: MSYS `ps -ef` prints no arguments, so `ps | grep` returned
**0 while 4 matching processes were live**. `proc-match.sh` branches
PowerShell/`Win32_Process` vs POSIX `ps -eo pid=,args=` and prints the
`pgrep -af` shape everywhere.

It also closes three silent lies the bracket could not (guard-3159, guard-1238).
Without `-f` a live run's process NAME is `bash`, so `pgrep -c` returns **0
against a run that is executing** — a false NEGATIVE, the premise under which
someone reads a verdict-less log as dead or launches a second run (2026-08-11,
bravo, cc-05, 6.8.0-137-generic: `pgrep -c` said 0 while `pgrep -af` showed 2 live
PIDs, log mtime 2s old). The bracket stops the matcher matching its OWN argv but
never an ENCLOSING wrapper's, so folding the check into the launch aborts the
launch it protects — a false POSITIVE (2026-08-01, alpha, cc-04,
6.8.0-136-generic: a clean process table reported one phantom whose only cited
PID was the guard's own wrapper). And the matcher can match itself: the script
snapshots before matching and drops its own PID.

## Why `VERDICT: GENUINE` can be false (g-115-4336)

Every method item tells the reader to trust the VERDICT above the numbers. That
advice holds for `INVALID`, which is fail-safe. It does NOT hold for `GENUINE`.
Measured (g-115-3748, `cc-02` / Linux 6.8.0-136-generic, own-cloud, live fleet,
16 chunks): `TOTAL: 8828 passed, 261 failed, 10 errors` /
`VERDICT: GENUINE failures -- trustworthy, act on them`, with **411 of 434 failures sitting in the
last TWO of sixteen chunks** and chunk 15's exact 47-file list re-running **solo
to 5** — the known byte-compat reds. ~233 failures were positional, and the
classifier emitted **no reason at all**.

The mechanism, measured by calling the classifier on its own logs:
`_positional_profile` buckets by the `[NN%]` in pytest progress lines, but each
chunk is its own run emitting its own 0→100%, so on an N-chunk concatenation the
percentage resets N times and the "first third" is sampled from ALL N chunks. A
cluster confined to the tail chunks is smeared uniformly across every bucket —
here `early 2.85%` vs `late 5.57%`, a 1.96x ratio under the 5x threshold.
Chunking (the exhaustion *remedy*) is what blinds the exhaustion *detector*, and
the blinding scales with the chunk count. Tracked by **g-115-4336**.

A SMALL MID-RUN POCKET is missed for the opposite reason: it is not positional at
all, so there is no skew to detect however the buckets are computed. Measured
2026-08-12 (alpha, `hostname` cc-04, `uname -r` 6.8.0-137-generic, own-cloud, 16
chunks): `TOTAL: 11673 passed, 14 failed` / `VERDICT: GENUINE`, with all 14 in
chunk 09 and chunks 10–15 clean after it — every failure a uniform `rc=4`
(daemon-unreachable) across two whole files, and **23/23 green solo**. Fourteen
failures look individually plausible enough to triage one by one, which is
exactly how a reader spends an hour on a daemon blip.

Four boxes hit one stable signature — chunk 09, three pipeline/pending files at
15/8/6, all green solo. The tempting chunk-local-collision reading of it was
**FALSIFIED** 2026-08-17: chunk 09's exact 59-file list re-ran in the same order,
same process, same pin, with 0 failures. Chunk 09 is where those files sort to,
not the cause. The cause was the memoized-`_ACTIVE_BACKEND` poisoning (above),
closed by **g-115-5651** 2026-08-19. Four dated per-box reproduction blocks of
that signature — 2026-08-15 (cc-03), 2026-08-16 (cc-08), 2026-08-17 (cc-04), and
the g-115-5651 root-cause note — were FOLDED into
`core/config/run-full-suite-baselines.md` § "Chunk-09 GENUINE-but-false
signature" on 2026-09-10 (g-115-9602). Open the ledger only if a FRESH
occurrence needs the prior counts to compare against.

## Why the Live-Daemon Exception pins `STORAGE_BACKEND=local` for every runner (2026-05-31 → 2026-07-26)

The daemon-lifecycle integration tests caused two daemon storms on 2026-05-31
(the second was an agent running this suite to verify its own deep-code change).
The resolution while a live daemon is present is the B16 durable fix, landed
2026-06-01.

**The marker does not bound the spawners.** Observed 2026-07-26: an unmarked,
ostensibly-hermetic test (`test_post_state_update_metric_gate_category.py`)
recycled the live daemon out from under the running fleet — its tmp
`local-paths.conf` did not isolate it, because `.mind-data/` outranks the conf in
the resolution chain. With the g-115-3329 refusal the failure is loud instead of
a silent fleet-wide repoint. Excluding the marked tests, the suite is hermetic in
its FILESYSTEM resolution: the in-process `_daemon_fixture.py` / `running_daemon`
fixtures bind a thread-local daemon in a tmp project root and set `RT_DIR` for
their subprocesses.

**The storage-key collision (2026-07-09).** On an own-cloud box
(`STORAGE_BACKEND=own-cloud`, this repo's default when a live daemon serves
agents) the "hermetic" claim is FALSE. Tests that seed a tempfile world and write
via a subprocess (e.g. `test_defer_to_unblock_integration.py`) inherit own-cloud,
because their subprocess spawn does `env = os.environ.copy()`.
`OwnCloudBackend._s3_key` derives the S3 key from `customer_prefix+env_id+`
filename, NOT from the `MIND_WORLD` tmp-dir override, so the tmp write collides
on the PRODUCTION S3 key and truncates the real store. On 2026-07-09
`world/aspirations.jsonl` was truncated from 22 aspirations/1366 goals to a lone
`asp-555` fixture (recovered from a `.history` snapshot via a fenced re-PUT).

**Why "prepend to pytest" was too narrow.** The 2026-07-09 truncation did NOT
come from `pytest core/scripts/tests`. `test_defer_to_unblock_integration.py` is a
`main()`-style file with zero `test_` functions, so pytest collects 0 from it and
never runs it. The real runner was the bash aggregator
`core/scripts/tests/run-asp-257-suite.sh` (suite 6/6 =
`python3 …/test_defer_to_unblock_integration.py`), invoked to validate a
capability-gate change. About 18 pytest-collected world-writers in
`core/scripts/tests` do `os.environ.copy()` and are S3-collision-capable under
own-cloud; the conftest pin covers those. See guard-955, rb-2983, and
`exp-owncloud-s3-collision-truncation-2026-07-09`.

`RUNTIME_DIR` lets a spawn-and-check-own-files test avoid hijacking the live
daemon's `mind_api/state`. It does not make the orphan test safe, because that
test counts processes by command line, not by runtime dir.

## Evidence behind the method items (g-115-1496 block)

The dated per-run baseline rows that used to sit above the method moved to
`core/config/run-full-suite-baselines.md` on 2026-08-17 (g-115-6469). Nothing was
deleted: 46,595 B of run records were 12.9% of the fixed preamble that loads on
EVERY turn of EVERY agent, and almost no turn needs them. The block asked for new
rows to go to the ledger while offering nowhere else to put one, which is why
eleven more arrived anyway.

- **Item 1 (VERDICT first).** A clean-looking TOTAL with no stopped percentage and
  no failing file anywhere can still be `INVALID (contended)`. Six independent
  confirmations across four boxes.
- **Item 3 (log corruption).** The sync layer REPLACES the log at a new inode while
  the writer still holds an fd on the old one, so the writer trickles into an
  orphaned inode. Measured, not inferred. **Duration is the discriminator, not
  size**: a 13.2 MB fast write survives; a 60-second trickle does not. Treating
  the NUL check as a filter is what lets the silent variant through, and it sends
  a reader up the chunk ladder for hours against a cause no rung can fix.
- **Item 4 (ladder never inheritable).** One box went CLEAN-at-16 → INVALID-at-16
  → CLEAN-at-20 inside two hours. The ladder does not track partner count either:
  16 was INVALID with 4 partners and CLEAN with 5, on different days.
- **Item 5 (TOTAL).** Three same-tree runs will not reconcile on TOTAL. For a
  population figure use `--collect-only`, which counts one thing the same way every
  run.
- **Item 6 (CLEAN scope).** A genuine red in the invisible or domain half rides
  out under a clean verdict otherwise.
- **Item 7 (--triage).** Ownership is scored on the failing TEST's node id over
  four narrative fields (g-115-10242).
- **Item 8 (never pipe).** Committed live once: a notification reported exit 0 for
  a contended run with no verdict anywhere in the captured output.
- **Item 10 (hostname verbatim).** "cc-04" has named at least two different
  machines (one Linux 6.8.0-136-generic, one WSL2 6.6.87.2). That is how a same-day
  RED and GREEN for one test on "the same box" turned out to be two boxes.
- **Item 11 (diff the env).** A filed "Windows portability" finding turned out to
  be a forked fixture missing an `MIND_WORLD` pin. It reproduced on the GREEN box
  by setting that one var.
- **Item 12 (re-run solo).** On one re-measurement, all four files the rows named
  as red were GREEN. One file passes 63/63 solo and fails only in-suite.
- **Item 13 (pre-existing ≠ tracked).** Establishing that a failure is not yours
  is the easy half and is where the check usually stops. One pair sat unowned for
  a day behind a cited goal that merely mentioned their file.

## Why the traps (the g-115-1496 runtime measurement)

The daemon-safe full suite took ~32min (measured: 1916s; 2231 passed / 2 failed /
1 skipped over 2234 selected). The runtime concentrates in a handful of
subprocess/integration tests that shell out to real git/bash/filesystem ops under
OneDrive contention — NOT primarily the daemon round-trips one might assume. The
slowest 20 sum ~880s (~46% of total) over <1% of tests: `test_promote`
seed-preflight/PR dry-runs (139s + 135s), `test_utilization_stats` real-repo
audit (77s), `test_orphan_root_sweep_mode_d_integration` filesystem scans (~180s
across 5), `test_post_state_update_gate_committed_files_only` daemon round-trips
(~60s across 3). Collection was silent for >50s because of heavy module-level
imports across 265 files.

Trap 2: `tail -f` (and most pipe buffering) holds output until EOF on Windows, so
you see nothing until the run finishes.

Trap 3: under g-115-1496 the suite was backgrounded and ran to completion (1916s)
across turns; it was NOT killed. But a bounded waiter loop timed out at ~12.5min
("may be hung") because the suite needs ~32min, and the background task's own
stdout looked empty because output went to the redirect file. Both signals falsely
read as "dead". Ground truth was the redirect file, which accumulated steady
progress the whole time.

Trap 4: guard-6148 (measured 2026-09-06) retired the prior "reducer backgrounds
it, harness notifies" instruction, because the Bash `timeout` cap kills a
`run_in_background` suite mid-chunk. The bare interruptible-sleep busy-spin cost
~20 turns over a 32min wait, and that incident is what motivated `EXTERNAL_WAIT=1`.

The faulthandler bound: 600s is 10min, well past the 139.61s slowest legitimate
test. A true hang therefore fails loud with a stack pointing at the stall instead
of buffering forever.

## Why the Live-Fleet Exception (g-115-3085, 2026-07-25)

Running the ~5,200-test suite in ONE process while the live fleet runs on the same
Windows box exhausts Windows process/desktop-heap resources partway through.
Measured: one contended run reported **564 failed / 4,672 passed**. The same tree,
re-measured properly, was clean. `test_release.py` alone accounted for 37 of those
failures and passes **88/88 when run by itself**. Measured distribution of the 564:
0 failures across the first 1,368 tests, then 19–27% in the final decile. A genuine
regression fails from the START (changed scripts are used throughout), so an
all-late profile is near-conclusive evidence of exhaustion, not code.

The manual chunk recipe, from before `run-full-suite.sh` did this itself. Fresh
processes reset accumulated handles per chunk; a single process cannot recover
them.

```bash
ls core/scripts/tests/test_*.py | sort > /tmp/all-tests.txt
split -n l/4 -d /tmp/all-tests.txt /tmp/chunk-
for c in 00 01 02 03; do
  STORAGE_BACKEND=local python -m pytest $(cat /tmp/chunk-$c | tr '\n' ' ') \
    -q -m "not daemon_integration" > /tmp/chunk-$c.log 2>&1
  tail -1 /tmp/chunk-$c.log
done
```

## Details trimmed from the per-area table

- Capability-gate row: the four `main()`-style suites sat red 3 days undetected,
  masking a real NameError (g-115-2343 / rb-3678).
- Invisible-suite row: measured 2026-07-29 (cc-05), 71 `.py` + 19 shell = 90
  files. Do not trust that count; `run-invisible-suites.sh --list` re-derives it.
  The g-115-2349 baseline sweep found 9 silent reds of 69.
- `mind_api/src` row: ~1,386 tests exercise that code directly. Before
  2026-08-20 the tree was deferred from the runner, and this command was the whole
  coverage.
- SKILL.md row: a bare `skill-evaluate.sh <skill-name>` errors
  `unknown subcommand`. It needs a subcommand (read/report/underperforming/score),
  and `score --skill <s> --goal <g>` rates RUNTIME skill-on-goal performance, not
  a static SKILL.md edit. g-115-7706: a bulk pass relocated front matter, 8
  blockquotes and 10 bold directives in start/SKILL.md, while 94 targeted tests,
  domain-leak-check AND the pre-completion re-read were all green.

## Why the recommender is advisory

The advisory posture mirrors the pre-apply consult gate (g-115-826). Visibility
beats fail-loud here for two reasons. (a) Running a 60-test Python suite or a
`./gradlew test --no-daemon` is a 30s–5min wall-clock cost that should be a
deliberate LLM choice, not an automatic forced run on every deep closure. (b) Some
deep closures are documentation-only ("modified SKILL.md but the change is pure
narrative"), where the suite adds no signal.

## Cross-references

- `.claude/rules/run-full-suite-after-deep-code.md` — the rule this explains (authoritative)
- `core/config/run-full-suite-baselines.md` — the dated per-run ledger
- `core/config/rationale/suite-run-voided-by-loop-merge.md` — why a worker's suite must finish inside its unit
- `core/config/conventions/hot-path-size-budget.md` — why prose moves out of rules
- g-353-118 — the extraction; g-115-6469 — the path scoping and the ledger move
