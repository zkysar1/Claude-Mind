---
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
  Path-scoped (g-115-6469). At 37 KB this was the single largest item in the
  fixed per-turn preamble — 12% of all rule bytes — paid by every agent on
  every turn whether or not it would ever run a suite. The globs are this
  rule's own Scope section: it applies when a deep goal TOUCHED production
  code, and those are the surfaces.

  SCOPING THIS ONE WOULD HAVE BEEN A REGRESSION ON ITS OWN, and that is the
  part to understand before touching it. A path-scoped rule loads when a
  matching file is touched and is NOT re-injected after a compaction. The
  moment this rule matters most is the CLOSURE — which can land in a turn after
  an autocompact, with the rule absent and "all tests pass" about to be
  written. Scoping alone would have removed the rule from exactly the turn it
  exists to govern.

  So it is scoped only because the imperative now has a second carrier that
  does not depend on the preamble at all:
  core/scripts/full-suite-imperative-gate.{sh,py} is a PreToolUse[Bash] hook
  that fires on the COMMAND (pytest / run-full-suite / gradlew) and delivers
  the five behavioural heads — VERDICT-first, GENUINE-can-be-false, the ladder
  is a retry protocol, never pipe the runner, CLEAN scopes to the pytest chunks
  only — plus the guard-955 STORAGE_BACKEND=local requirement. Verified firing
  live in-session, not merely unit-tested.

  IF THAT HOOK IS EVER REMOVED OR DISABLED, UNSCOPE THIS RULE IN THE SAME
  CHANGE. The two are one mechanism. Pinned by
  test_full_suite_imperative_gate.py, which asserts the hook is registered in
  .claude/settings.json — a gate nothing calls is indistinguishable from one
  that always passes (guard-1943).

  See core/config/conventions/rules-loading.md.
-->

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

## Scope: THREE testpaths, not one (g-115-3748, 2026-07-31)

Every `pytest core/scripts/tests` invocation written below names **one of the
three testpaths `pytest.ini` declares**. The other two are `mind_api/tests`
and `core/tests/gates`. Until 2026-07-31 `run-full-suite.sh` collected only
the first, so an agent could follow this rule perfectly, read `VERDICT: CLEAN`,
and have executed **zero** gate tests and zero daemon-endpoint tests.

Measured that day: 109 files / **1,448 tests** never ran, and **12 of them were
RED** — 5 in `core/tests/gates` (one for 36 days) and 7 in `mind_api/tests`.
Being red is the smaller half. These are the GATE and daemon-endpoint suites:
the layer the framework trusts to refuse bad writes and to keep CLI/daemon
output in parity. An unverified enforcement layer fails silently and upward.

The runner now resolves its roots from `pytest.ini` `testpaths` rather than a
hardcoded dir, so a future test tree joins the suite by being declared in the
config — no edit here, and no second source of truth to drift (that drift IS
this defect: the runner shipped 2026-07-26, five weeks after the config
already declared three paths). `bash core/scripts/run-full-suite.sh` now covers
`core/scripts/tests` and `core/tests/gates` — prefer it over every bare
`pytest <path>` command below, which remain accurate for targeted runs and are
NOT sufficient for a deep-code closure claim.

**`mind_api/tests` is IN the chunked pool since 2026-08-20 (g-115-6942)** — a
green `run-full-suite.sh` IS evidence about `mind_api/src`. History, kept
because its lessons generalize: the tree spent 2026-07-31→08-20 in
`DEFERRED_TESTPATHS` (announced-and-skipped; opt-in `RUN_DEFERRED=1`) after
failing en masse at end-of-invocation — 411 reds at rung 16, 271 at rung 20 —
while passing alone; neither ladder escalation nor an own process fixed it.
The cause landed as **g-115-5651**: `get_backend()` memoizes `_ACTIVE_BACKEND`
process-wide while conftest restored only the env VAR, so one own-cloud test
poisoned every later test in its process. The reset fixture now lives in BOTH
test-tree conftests (the mind_api mirror closed the mixed-chunk vector), and
the fold-back was accepted on measurement (cc-10, 6.8.0-137-generic,
2026-08-20): standalone 1,386/1,386 green; own-process at end-of-invocation
green; folded acceptance run 16,099 passed / 5 failed with every red pre-owned
and none in `mind_api/tests`. Four genuine reds the measurement surfaced were
fixed, not skipped (set_at daemon/CLI parity port, claim-sid harness pin,
citation lane pin, conftest MIND_SID coverage). The `DEFERRED_TESTPATHS`
mechanism stays for future trees — empty is its designed end state. Lasting
lesson (guard-1760): the runner reports what it RAN, never what it declined to
look for; when a suite's scope is configurable, check the config against the
runner before trusting a green.

A separate trap discovered on this rule's history is still live: **two
`run-full-suite.sh` invocations running CONCURRENTLY** (measured 2026-07-31,
~11 min overlapped — the chunked-half `TOTAL:` line makes a run look finished
while post-chunk phases are still writing). Before diagnosing anything from a
suite run, confirm no other run is live:

```bash
pgrep -af "[r]un-full-suite"   # bracket prevents matching your own command line
```

The bracket is not cosmetic: `pgrep -f "run-full-suite"` matches the shell
running the pgrep, so the naive form reports phantom orphans and aborts your
probe. Measured the same day.

**`-f` is equally load-bearing, and dropping it fails in the OPPOSITE, more
dangerous direction.** Without `-f`, `pgrep` matches the process NAME only — and
the name of a live suite run is `bash`, not `run-full-suite.sh`. So
`pgrep -c "[r]un-full-suite"` returns **0 against a run that is actively
executing**. The bracket-only failure above is a false POSITIVE (it aborts a
probe, loudly). This one is a false NEGATIVE: it says "finished" about a run
still writing, which is exactly the premise under which someone reads a
verdict-less log as a dead run, or launches a second overlapping invocation —
the contention this whole paragraph exists to prevent. Measured 2026-08-11
(bravo, `hostname` cc-05, `uname -r` 6.8.0-137-generic): `pgrep -c` reported 0
while `pgrep -af` reported 2 live PIDs and the log mtime was 2 seconds old.
Corroborate a "finished" reading with the log's mtime before believing it; a
verdict-less tail plus a fresh mtime means STILL RUNNING, never died.

**Run the probe as its OWN command — the bracket does not save you otherwise.**
It only stops `pgrep` matching its own argv. Put the check and the launch in one
command (`if pgrep -af "[r]un-full-suite"; then abort; else run-full-suite.sh …`)
and the ENCLOSING wrapper shell's argv contains the literal script name — a
different process, which the bracket cannot defend against. It matches, and the
guard aborts the launch it was written to protect. Measured 2026-08-01 (alpha,
`hostname` cc-04, `uname -r` 6.8.0-136-generic): a clean process table reported
one phantom "live run" whose only cited PID was the guard's own wrapper. Two
separate calls, always. (guard-1238 is the general form — "never use a pattern
that appears in the probing command itself"; the bracket is a partial mitigation
of it, not an exemption from it.)

**`VERDICT: GENUINE` CAN BE FALSE — and this is the first row in this file that
says so. Read it before acting on any large failure count.** Every row below
tells you to trust the VERDICT above the numbers. That advice holds for
`INVALID`, which is fail-safe. It does NOT hold for `GENUINE`. Measured the
same day (g-115-3748, `cc-02` / Linux 6.8.0-136-generic, own-cloud, live
fleet, 16 chunks): `TOTAL: 8828 passed, 261 failed, 10 errors` /
`VERDICT: GENUINE failures -- trustworthy, act on them`, with **411 of 434
failures sitting in the last TWO of sixteen chunks** and chunk 15's exact
47-file list re-running **solo to 5** — the known byte-compat reds. ~233
failures were positional, and the classifier emitted **no reason at all**.

The mechanism, measured by calling the classifier on its own logs:
`_positional_profile` buckets by the `[NN%]` in pytest progress lines, but each
chunk is its own run emitting its own 0→100%, so on an N-chunk concatenation
the percentage resets N times and the "first third" is sampled from ALL N
chunks. A cluster confined to the tail chunks is smeared uniformly across every
bucket — here `early 2.85%` vs `late 5.57%`, a 1.96x ratio under the 5x
threshold. Chunking (the exhaustion *remedy*) is what blinds the exhaustion
*detector*, and the blinding scales with the chunk count. Tracked by
**g-115-4336**; until it lands, apply the guard-1448 discriminators yourself on
ANY non-zero count — **not merely a large one**: bucket by CHUNK (not by the
blob), and re-run the worst-hit chunk's file list alone. A tail-loaded
distribution is contention no matter what the verdict says — and so is a SMALL
MID-RUN POCKET, which a positional profile misses for the opposite reason: it is
not positional at all, so there is no skew to detect however the buckets are
computed. Measured 2026-08-12 (alpha, `hostname` cc-04, `uname -r`
6.8.0-137-generic, own-cloud, 16 chunks): `TOTAL: 11673 passed, 14 failed` /
`VERDICT: GENUINE`, with all 14 in chunk 09 and chunks 10–15 clean after it —
every failure a uniform `rc=4` (daemon-unreachable) across two whole files, and
**23/23 green solo**. Read a small count as MORE suspicious, not less: 14
failures look individually plausible enough to triage one by one, which is
exactly how a reader spends an hour on a daemon blip. Two free tells before
triaging anything: are the failures confined to one chunk, and is the assertion
a LOGIC mismatch or a bare process rc?

REPRODUCED ON A SECOND BOX, and the chunk INDEX repeated — 2026-08-15 (echo,
`hostname` cc-03, `uname -r` 6.8.0-137-generic, own-cloud, live fleet, 16
chunks): `TOTAL: 13016 passed, 29 failed, 0 errors` / `VERDICT: GENUINE`, with
all 29 in **chunk 09** and chunks 10–15 clean after it. Three files
(`test_pipeline_tombstone_archival` 15, `test_pipeline_provenance_stamps` 8,
`test_pending_questions_close` 6), **44/44 green solo** in 0.12s. So the
false-GENUINE call is not one box's quirk, and 29 is twice the count that fooled
a reader last time — do not treat a bigger number as more credible. Two things
this adds. The `rc=4` tell above did NOT apply here (these are ordinary
assertions, not a bare process rc), so a single tell is not a filter: the
CHUNK-CONFINEMENT tell carried it alone. And chunk 09 landing twice out of two
is worth noting rather than explaining — with `--chunks 16` the same index is a
similar slice of a sorted file list, so a chunk-local resource collision is a
better first hypothesis than progressive exhaustion, which would load the TAIL.
Do not infer a cause from n=2; do check chunk 09 first.

**FOURTH OCCURRENCE, and the chunk-local-collision hypothesis directly above is
now FALSIFIED — stop reaching for it** (2026-08-17, alpha, `hostname` cc-04,
`uname -r` 6.8.0-137-generic, own-cloud, live fleet, 16 chunks): `TOTAL: 13800
passed, 29 failed, 0 errors` / `VERDICT: GENUINE`, all 29 in **chunk 09**, the
same three files at the same **15 / 8 / 6**, chunks 10–15 clean, 44/44 green
solo. Four boxes now, byte-identical counts — the signature is stable enough to
recognise on sight, which is exactly why the tempting inference needs killing.

The advice "check chunk 09 first" is GOOD and I followed it. What it does not
license is the chunk-local reading. Reconstructed chunk 09's exact 59-file list
from the runner's own `_chunk()` and re-ran it **in the same order, same
process, same pin: 0 failures.** Two narrower controls also passed
(`test_owncloud_backend.py` first, then all 14 `owncloud` files first — the
obvious poisoner, since sorted order does put them immediately before the
failing `test_p*` files). So the collision is NOT reproducible from the chunk's
file set, which means it is not a property of the chunk, the ordering, or the
index. **Chunk 09 recurring across boxes is the alphabet, not the cause** — it
is simply where the handful of tmp-world-plus-lock tests sort to.

**CAUSE FOUND AND FIXED (g-115-5651, 2026-08-19).** `ValueError: <tmp>/world/pipeline.lock
is not under any configured root` meant `get_backend()`'s process-wide `_ACTIVE_BACKEND`
had frozen an EARLIER test's tmp-world root map into the cached instance — conftest
restored the env VAR, not the derived object. The fixture now resets it —
mutation-proved, and all three victims ran together cleanly: the trio is
verified, not inferred.
Reproducing needs FOUR conditions, not two: cache empty, `own-cloud` in-process,
`MIND_WORLD`/`MIND_META` SET (else `from_env()` raises and nothing caches), and a
later test on a DIFFERENT tmp world — why ordered chunk replays and solo re-runs
read green against a live defect.

THIRD BOX, and the three files reproduce with IDENTICAL counts — 2026-08-16
(alpha WORKER Body, `hostname` cc-08, `uname -r` 6.8.0-137-generic, own-cloud,
live fleet, 16 chunks, logs via `--out` outside the synced tree): `TOTAL: 13190
passed, 59 failed, 0 errors` / `VERDICT: GENUINE`, chunk 09 carrying **33 of
59**. The same three files came back in the same sizes as the cc-03 row above —
`test_pipeline_tombstone_archival` 15, `test_pipeline_provenance_stamps` 8,
`test_pending_questions_close` 6 — and all three were **green solo** (21/15/8).
An exact count-for-count reproduction across three boxes makes this a stable
signature you can recognise on sight, not a coincidence to re-derive each time.

Two refinements, both of which cut against reading chunk 09 as the whole story.
**Failures were NOT chunk-confined here**: 02(2) 03(1) 08(7) 09(33) 11(3)
13(13), with 10/12/14/15 clean after the peak. So the chunk-confinement tell
that carried the cc-03 call alone would have UNDER-fired here — a spread
distribution does not exonerate a run, and chunk 09 dominating inside a spread
is still the tell. And the split was genuinely mixed: `--triage` returned **4
environmental | 6 genuine-owned | 0 genuine-unowned**, so 30 of the 59 were real
reds that simply already had owners. Do not let a confirmed-environmental
majority talk you out of triaging the rest; run `--triage` and let it separate
them rather than judging the whole run by its largest cluster.

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

> **The dated per-run baseline rows that used to live here — every box, kernel,
> chunk rung and TOTAL, verbatim — moved to
> `core/config/run-full-suite-baselines.md` on 2026-08-17 (g-115-6469).**
> Nothing was deleted; 46,595 B of run records were 12.9% of the fixed preamble
> that loads on EVERY turn of EVERY agent, and almost no turn needs them. Read
> the ledger when you are triaging a named failure, checking whether a red is
> stale, or adding a run record. **Add new rows THERE, never here** — this
> block asked for exactly that while offering nowhere else to put one, which is
> why eleven more arrived anyway.
>
> What follows is the METHOD, which is what a reader needs at the moment of use.
> Each item carries the number of independent confirmations behind it; the
> individual runs are in the ledger.
>
> **1. READ THE `VERDICT` LINE FIRST, AND LET IT DECIDE WHETHER THE NUMBERS
> ABOVE IT MEAN ANYTHING.** A run can print a fully clean-looking
> `TOTAL: N passed, 0 failed, 0 errors` over per-chunk lines that ALL read
> `0 failed`, with no stopped percentage and no failing file anywhere, and still
> be `VERDICT: INVALID (contended) -- this number means NOTHING`. Six independent
> confirmations across four boxes. Per-chunk lines cannot be trusted; the verdict
> can.
>
> **2. `VERDICT: GENUINE` CAN BE FALSE — and a SMALL count is more suspicious,
> not less.** The verdict is fail-safe for `INVALID` and NOT for `GENUINE`. The
> positional profile that classifies exhaustion buckets by pytest's `[NN%]`, but
> each chunk emits its own 0→100%, so a cluster confined to the tail — or to one
> chunk — is smeared flat and reported GENUINE with no reason at all
> (g-115-4336). Apply the guard-1448 discriminators yourself on ANY non-zero
> count: **bucket by CHUNK, not by position, and re-run the worst-hit chunk's
> file list SOLO.** Green solo ⇒ environmental. Four boxes have now hit one
> stable signature — chunk 09, three pipeline/pending files at 15/8/6, all green
> solo. The tempting chunk-local-collision reading of it was **FALSIFIED**
> 2026-08-17: chunk 09's exact 59-file list re-ran in the same order, same
> process, same pin, with 0 failures. Chunk 09 is where those files sort to, not
> the cause. **The cause is known and FIXED** — the memoized-`_ACTIVE_BACKEND`
> poisoning above, closed by **g-115-5651** 2026-08-19. A fresh occurrence is a
> REGRESSION: re-run solo and file a NEW goal.
>
> **3. `INVALID` HAS TWO CAUSES AND CLIMBING THE LADDER ONLY FIXES ONE.** The
> other is log corruption. **RESOLVED 2026-08-17 (g-115-6409): the default log
> dir moved off the fleet-synced tree** to `<tmpdir>/ayoai-suite-run-<agent>`, so
> there is nothing to pass. `--print-out-dir` is a **`.py`** flag; on the `.sh`
> it rides into a REAL run that looks hung. Older builds:
> `--out /tmp/<non-synced-dir>`. Mechanism, measured not inferred: the sync layer
> REPLACES the log at a new inode while the writer still holds an fd on the old
> one, so the writer trickles into an orphaned inode. **Duration is the
> discriminator, not size** — a 13.2 MB fast write survives; a 60-second trickle
> does not.
>
> ⚠ **The NUL-byte check is ONE-DIRECTIONAL. Any NULs ⇒ corruption; ZERO NULs is
> NOT evidence against it.** The common variant has a clean prefix, zero NULs and
> rc=0 — byte-indistinguishable from a short run. Treating the check as a filter
> is what lets the silent variant through and sends a reader up the chunk ladder
> for hours against a cause no rung can fix.
>
> ```bash
> for f in <logdir>/chunk-*.log; do n=$(tr -dc '\0' < "$f" | wc -c); \
>   [ "$n" -gt 0 ] && echo "$(basename $f): $n NUL"; done
> ```
>
> **4. THE CHUNK LADDER (8 → 12 → 16 → 20 → 24 → 28 → 32 → 36) IS A RETRY
> PROTOCOL, NOT A SETTING — AND IT IS NEVER INHERITABLE.** Not from another
> agent, not from another box, and **not from your own earlier run on the same
> machine**: one box went CLEAN-at-16 → INVALID-at-16 → CLEAN-at-20 inside two
> hours. It does not track partner count either (16 was INVALID with 4 partners
> and CLEAN with 5, on different days). Enter the ladder anywhere, read the
> VERDICT, escalate only when it says to, and do not read a contended run's
> totals as a regression.
>
> **5. THE `TOTAL` LINE IS NOT A CROSS-RUN COMPARISON METRIC.** Judge by the
> FAILING FILE SET. The summary reports only `passed`, so xfail/xpass/skip sit
> silently outside it and three same-tree runs will not reconcile. `failed` and
> `errors` are the trustworthy fields; for a population figure use
> `--collect-only`, which counts one thing the same way every run.
>
> **6. `VERDICT: CLEAN` SCOPES TO THE CHUNKED PYTEST HALF ONLY.** It is not a
> whole-suite all-clear. Also `grep '^FAIL'` for the invisible (`main()`-style
> and shell) half and the domain half, which the runner reports separately. A
> genuine red in those halves rides out under a clean verdict otherwise.
>
> **7. WHEN THE VERDICT IS NOT CLEAN, RUN `--triage`.** It re-reads the chunk
> logs the run already wrote (it does not re-run the suite) and chains
> position-bucket → solo re-run → **ownership**, reporting only genuine-AND-
> unowned as FILE THESE. It queries the failing file's stem both with and without
> the `test_` prefix, because `--title-contains` matches titles and titles drop
> that prefix. Read its `SCOPE` block: `NOT RECORDED` for a half is a statement
> of ignorance, never a pass.
>
> **8. NEVER PIPE THE RUNNER — not even a finished run.** A trailing pipe
> replaces the exit code with the pipe's (guard-1150), destroying the exit-2
> INVALID signal, and a bounded window (`| tail -40`) discards the VERDICT line
> that items 1-2 tell you is the only thing worth reading. Redirect to a file and
> Read it. Committed live once: a notification reported exit 0 for a contended
> run with no verdict anywhere in the captured output.
>
> **9. CHECK FOR A CONCURRENT RUN AS ITS OWN COMMAND, WITH `pgrep -af`.**
> `pgrep -af "[r]un-full-suite"`. Both flags are load-bearing and they fail in
> OPPOSITE directions: the bracket stops `pgrep` matching its own argv (a false
> POSITIVE that aborts your probe), and `-f` is required because a live run's
> process NAME is `bash` — so `pgrep -c` returns **0 against a run that is
> actively executing**, the false NEGATIVE that causes two overlapping
> invocations. Run it as its own command; folding the check and the launch into
> one compound command puts the literal name in the wrapper shell's argv, which
> the bracket cannot defend against. Corroborate a "finished" reading with the
> log's mtime — a verdict-less tail plus a fresh mtime means STILL RUNNING.
>
> **10. RECORD `hostname` AND `uname -r` VERBATIM, NEVER A NICKNAME.** "cc-04"
> has named at least two different machines (one Linux 6.8.0-136-generic, one
> WSL2 6.6.87.2), which is how a same-day RED and GREEN for one test on "the same
> box" turned out to be two boxes. A baseline you cannot attribute is a baseline
> you cannot trust.
>
> **11. BEFORE RECORDING A CROSS-BOX RED/GREEN SPLIT AS PORTABILITY, DIFF THE
> ENV.** Env-dependence reproduces cross-platform; genuine platform-dependence
> does not — that asymmetry is the whole discriminator, and it is one command. A
> filed "Windows portability" finding turned out to be a forked fixture missing
> an `MIND_WORLD` pin, reproduced on the GREEN box by setting that one var.
>
> **12. RE-RUN A NAMED RED SOLO BEFORE TRIAGING FROM PROSE.** Reds recorded in
> prose go stale and nothing re-checks them; on one re-measurement all four files
> the rows named as red were GREEN. A prose red is a lead, not a finding. Note
> also that a solo re-run cannot falsify an IN-SUITE claim (test-order pollution
> is real: one file passes 63/63 solo and fails only in-suite), and that one solo
> measurement is not a verdict — repeat before labelling anything GENUINE.
>
> **13. "PRE-EXISTING" IS NOT "TRACKED".** Establishing that a failure is not
> yours is the easy half and is where the check usually stops; it still needs an
> owner. Open the cited goal and confirm it names the failing TESTS — a shared
> file path is not ownership. One pair sat unowned for a day behind a cited goal
> that merely mentioned their file.

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
   ON A REDUCER: background the suite (`run_in_background`), END the turn;
   harness notifies (guard-1230). **A WORKER MUST NOT — it VOIDS the run;
   use item 3's in-turn route** (`rationale/suite-run-voided-by-loop-merge.md`).
   To pace an in-turn sleep, use the flag: `EXTERNAL_WAIT=1 bash
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
| `core/scripts/*.py` (non-test) | `bash core/scripts/run-full-suite.sh` (covers `core/scripts/tests` + `core/tests/gates` + the invisible and domain halves; NOT `mind_api/tests`). The narrower `python -m pytest core/scripts/tests -q` is fine for a targeted re-run but is NOT sufficient for a closure claim — see § Scope: THREE testpaths. | exit code 0, all collected tests pass |
| `core/scripts/gates/capability.py`, `capability-gate.py`, or the defer→Unblock path in `aspirations.py` | ALSO run `bash core/scripts/tests/run-asp-257-suite.sh` — 4 of its 6 suites are `main()`-style files pytest collects 0 tests from, so pytest-green says NOTHING about them (they sat red 3 days undetected, masking a real NameError — g-115-2343 / rb-3678) | aggregator prints `6/6 suites passed` |
| Any change whose test coverage lives in a pytest-INVISIBLE file — a `main()`-style `.py` (no top-level `def test_`) **or any `.sh`, which pytest cannot collect at all**. Measured 2026-07-29 (cc-05): 71 `.py` + 19 shell = 90 files. Do not trust that count; re-derive with `bash core/scripts/tests/run-invisible-suites.sh --list`, which prints the split. | `bash core/scripts/tests/run-invisible-suites.sh` — dynamic population runner; known-reds are quarantined inline with their tracking goal IDs (g-115-2349 baseline sweep found 9 silent reds of 69). **Since g-115-3957 this runner is invoked automatically by `core/scripts/run-full-suite.sh`**, so a full-suite run already covers it; invoke it directly only when you want the invisible half alone. | runner exits 0 (`N/N files passed, M quarantined`) |
| `mind_api/src/*.py` | `STORAGE_BACKEND=local python -m pytest mind_api/tests -q -m "not daemon_integration"` — ~1,386 tests that test this code directly (the fast targeted arm). Since 2026-08-20 (g-115-6942) `run-full-suite.sh` also collects this tree in its chunked pool, so a green full-suite run IS evidence about `mind_api/src`; before that it was deferred and this command was the whole coverage (g-115-3748 history in § Scope: THREE testpaths). | exit 0 |
| `core/scripts/*.sh` (production wrapper) | Whatever the wrapper's daemon endpoint suite covers — typically `python -m pytest core/scripts/tests -q -k <endpoint>` | exit 0 |
| `.claude/skills/*/SKILL.md` | Re-read the edited pseudocode + `bash core/scripts/domain-leak-check.sh`; if the change alters skill BEHAVIOR (not just prose), also `/verify-learning` for cross-skill grep checks. For BULK prose edits (extraction/reflow passes) ALSO run `py -3 core/scripts/line-class-diff-check.py <paths>` — report-only, per-class set-diff vs HEAD. g-115-7706: a bulk pass relocated front matter, 8 blockquotes and 10 bold directives in start/SKILL.md while 94 targeted tests, domain-leak-check AND the pre-completion re-read were all green. (Do NOT use `skill-evaluate.sh` here. A bare `skill-evaluate.sh <skill-name>` errors `unknown subcommand`: it needs a subcommand (read/report/underperforming/score), and `score --skill <s> --goal <g>` rates RUNTIME skill-on-goal performance, not a static SKILL.md edit.) | re-read confirms intent; domain-leak-check clean; verify-learning passes if behavior changed; line-class diff reports no class removals |
| `.claude/rules/*.md` | No automated check — re-read the rule and confirm wording matches intent | manual review |
| `core/config/*.yaml` / `core/config/*.md` | Re-parse via affected consumers — `bash core/scripts/<consumer>.sh --dry-run` if available, otherwise `python -c "import yaml; yaml.safe_load(open('<path>'))"` | parse succeeds, no schema break |
| **External domain + meta paths** — `world/scripts/**`, `world/conventions/**`, `meta/**`. These are neither git-tracked framework nor a sibling product repo, so before g-356-02 they had **no row in either table**. | `STORAGE_BACKEND=local python3 -m pytest "$WORLD_PATH/scripts/tests" -q` (the pin is mandatory — guard-955), plus whatever shell-unit runner the domain provides. **`full-suite-recommender.sh` CANNOT SEE THESE PATHS**: they are external and gitignored, and the recommender detects changes via git, so it reports `no code changes detected` for every domain-script and meta-strategy edit ever made, by any agent, on any box. Its silence there means "I cannot see", rendered identically to "nothing changed" — pick the suite yourself and say in the verify summary that the recommender was *blind*, not quiet (guard-1947). Read-side inverse of rb-1699, where the same tool OVER-attributes partner changes inside the tracked tree. | domain pytest exits 0; shell units pass except pre-existing environment-gated quarantines, which must be named |

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
- `core/config/run-full-suite-baselines.md` — the dated per-run ledger
  (box, kernel, chunk rung, VERDICT, TOTAL) extracted from this rule
  2026-08-17. The rule keeps the METHOD; the ledger keeps the EVIDENCE. Add
  run records there, never here.
