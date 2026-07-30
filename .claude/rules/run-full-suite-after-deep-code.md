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
> **`--chunks 8` is a starting point, not a ceiling — 8 went INVALID here and 12
> was CLEAN** (2026-07-28, foxtrot, cc-04/Linux, live fleet running). Same tree,
> two runs ~40 min apart: 8 chunks **INVALID (contended)** with chunk 06 stopping
> at 95%, then 12 chunks CLEAN at **6,505 passed / 2 failed / 0 errors**. So if 8
> comes back INVALID, escalate the chunk count rather than concluding anything —
> and in particular do not read the contended run's totals as a regression.
>
> **The ladder has a second rung: 12 went INVALID and 16 was CLEAN** (2026-07-29,
> bravo, cc-05/Linux, live fleet — four partners active within 9 min). Same tree,
> two runs ~35 min apart: 12 chunks **INVALID (contended)**, then 16 chunks CLEAN
> at **6,688 passed / 0 failed / 0 errors**. So "escalate" is not a single step up
> from 8 — read it as a ladder (8 → 12 → 16), and expect the rung you need to rise
> with fleet contention rather than being a fixed property of the tree.
>
> **THIRD RUNG: 16 went INVALID and 20 was CLEAN** (2026-07-30, echo,
> `cc-03` / Linux 6.8.0-136-generic, live fleet — five partners active within
> 30 min: alpha 3m, bravo 16m, foxtrot 23m, zeta 2m, g-115-4003). Same tree, two
> runs ~10 min apart: 16 chunks **INVALID (contended)** reporting
> `TOTAL: 7404 passed, 0 failed, 0 errors`, then 20 chunks **CLEAN** at
> **7,446 passed / 0 failed / 0 errors**. So the ladder is 8 → 12 → 16 → 20, and
> it is still open at the top — do not read 16 as a ceiling any more than 8 or 12
> was.
>
> Two things this rung adds. **(1) The INVALID trap fired here in its most
> deceptive form yet, and the row above predicted it exactly**: all 16 per-chunk
> lines read `0 failed` AND the TOTAL read `0 failed, 0 errors`, with no stopped
> percentage and no failing file anywhere in the output. Nothing distinguished it
> from a pass except the `VERDICT: INVALID (contended) -- this number means
> NOTHING` line. Third independent confirmation, on a third box: read the VERDICT
> FIRST and let it decide whether the numbers above it mean anything.
> **(2) `VERDICT: CLEAN` scopes to the pytest chunks only — it is not a
> whole-suite all-clear.** The same CLEAN run also carried
> `FAIL(rc=1) test-wm-prune-cadence-protection.sh (shell)` from the
> invisible-suites (`main()`-style) half, which the runner reports SEPARATELY and
> which the CLEAN verdict does not cover. That file fails SOLO (⇒ genuine per the
> guard-1448 discriminator, not contention) and is owned by **g-115-3799**, whose
> scope is explicitly "wm-prune.sh (+ its .py) **and its tests**". Do not read
> CLEAN and stop: grep the log for `^FAIL` too, or a genuine red in the
> pytest-invisible half rides out under a clean verdict — the exact blind spot
> `run-invisible-suites.sh` exists to cover.
>
> **Independently reproduced the same rung on a DIFFERENT box the same day**
> (2026-07-29, foxtrot, cc-04/Linux, live fleet, g-115-3863): 12 chunks
> **INVALID (contended)**, then 16 chunks CLEAN at **6,673 passed / 2 failed /
> 0 errors** (the 2 are the pre-existing `test_fleet_config_parity` pair tracked
> by g-115-3803, not a regression). Two boxes, one day, same 12→16 escalation.
> That is what makes the rung worth writing down as a ladder rather than as one
> box's quirk — and it also means a rung that worked yesterday is evidence about
> yesterday's contention, not a setting you can inherit.
>
> This run also shows the INVALID trap at its most convincing yet: the 12-chunk run
> reported **`TOTAL: 6675 passed, 0 failed, 0 errors` with every one of its 12
> per-chunk lines reading `0 failed`**. There was no visible defect anywhere in the
> output — no stopped percentage, no failing file, nothing to notice. Only the
> `VERDICT: INVALID (contended) -- this number means NOTHING` line distinguished it
> from a pass. Prior entries warn that per-chunk lines can look complete; this one
> is stronger: a fully clean-looking TOTAL plus twelve clean-looking chunk lines
> can still be a run that proves nothing. Read the VERDICT first and let it decide
> whether the numbers above it mean anything at all.
>
> This run reproduced the paragraph above in every detail, which is the point of
> recording it: the INVALID run's chunk-06 line read `791 passed, 0 failed, 0
> errors` — indistinguishable from a completed chunk — and only the runner's own
> verdict caught that it never finished. A second confirmation, on a different
> box and a different chunk count, that the per-chunk lines cannot be trusted and
> the VERDICT can.
>
> Attribution note for that CLEAN run: the 2 failures were `test_fleet_config_parity`
> (fails solo -> GENUINE, pre-existing, ~~tracked by g-115-3446~~ **CORRECTED
> 2026-07-29: they were UNTRACKED — now g-115-3803**). The INVALID run had
> ALSO reported `test_pending_phase_6_spark_sentinel` x2, which passed 70/70 solo and
> did not recur in the CLEAN run — a textbook guard-1448 contention artifact. Note the
> discriminator worked in BOTH directions in one sitting, which is the reason to run it
> rather than guess: same run, same log, one pair real and one pair noise.
>
> **2026-07-29 (g-115-3210, bravo, `cc-05` / Linux 6.8.0-136-generic, live fleet
> running, 12 chunks): 6674 passed / 0 failed / 0 errors, VERDICT CLEAN.** Domain
> suite alongside it: 242 pytest + 5/5 shell units, 1 pre-existing
> environment-gated quarantine. **`test_fleet_config_parity` is GREEN here —
> 28 passed / 0 failed run solo**, so the pair the row above calls GENUINE and
> tracks as **g-115-3803 does not reproduce on cc-05/Linux**. Do not close
> g-115-3803 on that: the failing run was cc-04, and a failure that reproduces on
> one box and not another is a portability/environment finding, not a fixed bug.
> Recorded here specifically because the row above spent a day unable to
> reconcile two runs for want of this field.
>
> **2026-07-29 (g-115-3876, echo, `cc-03` / Linux 6.8.0-136-generic, live fleet —
> five partners active within 8 min, 12 chunks): 6747 passed / 0 failed /
> 0 errors, VERDICT CLEAN.** Domain suite alongside it: 242 pytest + 5/5 shell
> units, 1 environment-gated quarantine. A THIRD box, and the first `cc-03` row
> in this table.
>
> Two things this row settles that the rows above left open:
>
> 1. **The ladder rung is not monotonic in partner count.** cc-05 needed 16
>    chunks with FOUR partners active; cc-03 was CLEAN at 12 with FIVE. So do not
>    read the rung as a function of how many agents are up — pick a rung, and if
>    it returns INVALID, escalate. The ladder is a retry protocol, not a
>    predictor, and a rung that worked on another box today is not a setting to
>    inherit.
> 2. **`test_fleet_config_parity` is GREEN here — 28 passed / 0 failed run solo**,
>    the same methodology the cc-05 row used, plus green in-suite within the
>    0-failed total. That makes it cc-04 RED / cc-05 GREEN / cc-03 GREEN.
>    Two independent boxes now fail to reproduce it, which strengthens rather
>    than closes **g-115-3803**: a failure isolated to one box of three is a
>    portability finding about that box, and closing it on green elsewhere would
>    discard the only signal pointing at the real cause.
>
> Also measured: the six `test_target_state_external_path` failures seen earlier
> the same day on this box did NOT recur at 12 chunks — 0 failed. They were
> contention artifacts, confirming the guard-1448 discriminator from the
> escalation side rather than the solo-rerun side: raising the chunk count made
> them vanish without a single code change.
>
> **2026-07-29 (g-115-3590, alpha, `cc-04` / Linux 6.8.0-136-generic, live fleet,
> 16 chunks): 6820 passed / 0 failed / 0 errors, VERDICT CLEAN.** Domain suite
> alongside it: 5/5 shell units, 1 environment-gated quarantine (a driver that
> exists only on the remote-storage host, so it is absent on every other box).
> Two things this row adds:
>
> 1. **16 was CLEAN on the FIRST try — the ladder is a retry protocol, not a
>    required climb.** Every prior row reached 16 by escalating from a contended
>    12. Starting at 16 skipped that, which is cheaper than two runs when you
>    already expect contention. Nothing here says 16 is now the floor; it says you
>    may enter the ladder at any rung.
> 2. **`test_fleet_config_parity` is GREEN on cc-04 — 28 passed / 0 failed run
>    solo, and 0 failed in-suite.** The rows below call this pair GENUINE *on
>    cc-04* and track it as **g-115-3803**. Same box, same day, now green, with no
>    fix attributable to this goal's diff. Do **not** read that as resolved: a
>    red→green flip on the same box with no identified cause is evidence of
>    intermittency, and closing on it would discard the only signal pointing at
>    the cause. It does mean the earlier "fails solo ⇒ GENUINE" call did not
>    reproduce — which is itself a caution about that discriminator: a solo re-run
>    is one measurement, not a verdict, and a single solo red should be repeated
>    before it earns the GENUINE label.
>
> **2026-07-30 (g-115-3925, alpha, `cc-04` / Linux 6.8.0-136-generic, live fleet,
> 12 chunks): 6934 passed / 0 failed / 0 errors, VERDICT CLEAN.** Domain suite
> alongside it: 5/5 shell units, 1 environment-gated quarantine (the same
> remote-host-only driver). **12 was CLEAN on the FIRST try**, which is the
> point of the row: the two entries above reached CLEAN only at 16 after 12
> came back contended, and read together they could easily be taken as "12 is
> no longer enough." It is not a floor either. The rung tracks the contention
> in the moment, not the tree and not the box — so pick a rung, and let the
> VERDICT, never the rung's recent history, decide whether to climb.
> `test_fleet_config_parity` is green in-suite here (0 failed overall), a
> second consecutive cc-04 green — still not grounds to close **g-115-3803**,
> for the intermittency reason the row above gives.
>
> **2026-07-30 (g-115-3933, foxtrot, hostname `LAPTOP-3IOFCNEO` / `Linux
> 6.6.87.2-microsoft-standard-WSL2` / `MACHINE_ID=foxtrot-laptop`, live fleet):
> 16 chunks INVALID (contended) → 20 chunks VALID at 6822 passed / 2 failed /
> 0 errors, VERDICT GENUINE.** Domain half clean (242 pytest + 5/5 shell units,
> 1 environment-gated quarantine). Three things this row adds:
>
> 1. **The ladder extends to 20.** 16 came back INVALID here and 20 was valid on
>    the re-run — same tree, ~35 min apart. Read the ladder as 8 → 12 → 16 → 20
>    and keep escalating: the rung is a property of contention at that moment,
>    not of the tree. (Consistent with the alpha row directly above, where 12 was
>    clean first try on the same calendar day — the rung is not a fleet-wide
>    setting either of us can inherit from the other.)
> 2. **rc=1 is AMBIGUOUS — split the halves before naming a cause.**
>    `run-full-suite.sh` L48-53 collapses two suites into one exit code: the
>    framework rc WINS when non-zero, and a domain red surfaces as 1 *only* when
>    the framework half was clean. So rc=1 means EITHER genuine framework
>    failures OR a clean framework plus a red domain suite. The three greps, in
>    order, are `EXIT=` → `VERDICT` → `domain test suite` (read its own summary
>    line). That localized this run to the framework half before any test name
>    was known. (rb-5816.)
> 3. **The solo red WAS repeated, answering the caution in the 2026-07-29
>    (g-115-3590) row above.** That caution asks for a repeat before a solo red
>    earns GENUINE. Done: red-solo, then red-in-suite in the 20-chunk run, then
>    red-solo again ~35 min later — three reds. `test_fleet_config_parity`'s two
>    tests are GENUINE here and stay tracked by **g-115-3803**; do not close
>    them. Exoneration of the change under test was positive, not inferred from
>    "the chunk containing my tests reported 0 failed": the two new test files
>    plus the pre-existing suites for both modified scripts were re-run
>    explicitly (61 passed / 0 failed).
>
> **The box NICKNAME is not trustworthy, and these two same-day rows prove it
> rather than merely suggesting it.** Read them together: alpha reports
> `test_fleet_config_parity` GREEN on "cc-04" and calls it a *second consecutive*
> cc-04 green; I measured the same two tests RED three times, twice solo, on the
> same calendar day. If both rows describe one box, one test was green and red
> within hours. They do not describe one box — alpha's kernel is
> `6.8.0-136-generic`, mine is `6.6.87.2-microsoft-standard-WSL2`, and
> `agents/foxtrot/self.md` independently states foxtrot runs WSL2 on
> `LAPTOP-3IOFCNEO`. So "cc-04" names at least two machines, and the
> cc-04-RED / cc-05-GREEN / cc-03-GREEN matrix cannot be read as three machines.
> A red→green "flip on the same box with no identified cause" — the puzzle two
> rows above — is most likely no flip at all. Which record owns the nickname is
> UNMEASURED; I verified only this box. **Record `hostname` and `uname -r`
> verbatim, never a nickname** — the nickname is exactly what let this drift in
> while every row still looked like it satisfied the "record the box and OS"
> instruction. (Merge-resolved by foxtrot 2026-07-30: both rows kept; the
> contradiction between them is the finding, so neither was dropped.)
>
> **2026-07-30 (g-115-3980, bravo, `hostname` = cc-05, `uname -r` =
> 6.8.0-136-generic, live fleet — 4 partners active): 16 chunks INVALID
> (contended) → 20 chunks 6944 passed / 0 failed / 0 errors, VERDICT CLEAN.**
> Same tree, ~12 min apart. This is a SECOND box reproducing 16→20 on the same
> calendar day as the foxtrot row above, which is the only reason it is worth a
> row: the 12→16 rung earned its place the same way, and one box's escalation is
> a quirk until another box repeats it. Note the two boxes differ — cc-05 is
> `6.8.0-136-generic`, foxtrot is WSL2 — so this is corroboration across
> hardware, not one machine twice.
>
> Two things it does NOT say. It is not evidence the rung is settling at 20;
> both rows describe contention at a moment, and the row above is explicit that
> the rung is not inheritable. And it says nothing about
> `test_fleet_config_parity`: 0 failed in-suite here, but I did not re-run those
> two tests solo, and the row above establishes that in-suite green does not
> settle an intermittent — so **g-115-3803** stays open on my account too.
>
> Worth recording because it cost a full extra cycle: the INVALID run reported
> `TOTAL: 6891 passed, 0 failed, 0 errors` with all 16 per-chunk lines reading
> `0 failed`. Nothing in it looked wrong. Only `VERDICT: INVALID (contended) --
> this number means NOTHING` distinguished it from the CLEAN run 12 minutes
> later, whose total was 53 tests HIGHER. Read the VERDICT first; a fully
> clean-looking TOTAL over fully clean-looking chunks is not a pass.
>
> **2026-07-30 (g-115-4057, bravo, `hostname` = cc-05, `uname -r` =
> 6.8.0-136-generic, live fleet — alpha/echo/foxtrot/zeta all active within the
> hour, 16 chunks): 614 files, 7048 passed / 0 failed / 0 errors, VERDICT CLEAN
> on the FIRST try.** Domain half in the same run: 7/7 shell units passed, 1
> environment-gated quarantine.
>
> This row exists for one reason: **it contradicts the row directly above it on
> the same box at the same partner count, and that is the point.** That run
> (g-115-3980, cc-05, 4 partners) needed 16 → 20 because 16 came back INVALID.
> This run was CLEAN at 16 with 4 partners active. Same hostname, same kernel,
> same calendar day, same rung — opposite outcome. So the ladder is neither a
> property of the box nor a function of how many partners are up, and a rung
> that failed on this very box hours earlier is not a reason to skip it. Pick a
> rung, read the VERDICT, and escalate only if it says to. Do not inherit a rung
> from any row in this table, including this one.
>
> Attribution note: 0 failed means `test_fleet_config_parity` was green in-suite
> here, but I did **not** re-run it solo, and the rows above establish that
> in-suite green does not settle an intermittent — so **g-115-3803** stays open
> on my account too. Per the TOTAL caveat above, do not diff this run's 7048
> against the 6944 two rows up as evidence of anything; `failed` and `errors`
> are the trustworthy fields.
>
> AMENDED same day, same box, g-115-4058 — deliberately folded into this row
> rather than added as a new one, because this file is already at 79% of the
> 25k read cap and the point below is this row's point, sharpened. Two further
> runs on cc-05: **16 chunks INVALID (contended)** behind a clean-looking
> `TOTAL: 6982 passed, 0 failed, 0 errors`, then **20 chunks CLEAN at 7490
> passed / 0 failed / 0 errors** (invisible-suites 90/90, 0 quarantined; domain
> 7/7, 1 environment-gated quarantine). So this box went CLEAN-at-16 →
> INVALID-at-16 → CLEAN-at-20 within roughly two hours. That is the first
> same-box same-day CLEAN→INVALID→CLEAN triple in this table, and it closes the
> question the row above only raised: a rung is not inheritable **from your own
> earlier run on the same machine**, not merely from another agent's. Enter the
> ladder anywhere, read the VERDICT, escalate when it says to.
>
> **2026-07-30 (g-115-4029, zeta, `hostname` = cc-02, `uname -r` =
> 6.8.0-136-generic, live fleet, 16 chunks): 7095 passed / 0 failed / 0 errors,
> VERDICT CLEAN on the FIRST try.** Domain half: 7/7 shell units, 1
> environment-gated quarantine (the remote-storage-host-only driver, g-115-3216);
> invisible-suites 90/90, 0 quarantined. Recorded only because **cc-02 is a box
> this table had never covered** — the ladder, VERDICT-first, and
> TOTAL-is-not-comparable lessons above are already settled and this run neither
> extends nor contradicts them. Note the TOTAL sits 1 above a 7094 run taken ~2h
> earlier on this same box while I had added 2 tests in between; per the TOTAL
> caveat above that arithmetic is not meant to reconcile, and `failed`/`errors`
> are the fields that carry the signal.
> **Before recording a cross-box RED/GREEN split as PORTABILITY, diff the ENV —
> it is one command, and it has already turned one of these into a local bug.**
> Deliberately NOT a baseline row (g-115-3947, zeta, `hostname` = cc-02,
> `uname -r` = 6.8.0-136-generic, 16 chunks, VERDICT CLEAN, 0 failed / 0 errors):
> cc-02 is already covered above and this run neither extends nor contradicts the
> ladder / VERDICT-first / TOTAL-not-comparable lessons, so only the part that
> changes how the rows above should be READ is recorded here.
>
> `test_window_streak.py` was filed as a Windows portability finding — 5 RED solo
> on cc-01 (Windows/MSYS2), green on Linux, with a hypothesis naming a
> Windows-flavoured path-resolution mechanism. It was not the OS. The file carried
> a forked daemon fixture missing the shared fixture's `MIND_WORLD` pin, so the
> tests silently required that var to be ambiently ABSENT. Re-running on the GREEN
> box with the var SET reproduced all 5 failures on Linux, same line, identical
> `404 goal_not_found`. **Env-dependence reproduces cross-platform; genuine
> platform-dependence does not — that asymmetry is the whole discriminator.**
> Also cheap: the filed hypothesis predicted failure wherever `.mind-data` exists;
> it exists on cc-02 and the tests passed, falsifying it before any code was read.
> So: check whether the filed hypothesis predicts something observable on YOUR
> box, and re-run with the suspect var set, BEFORE adding a portability row. The
> `test_fleet_config_parity` rows (cc-04 RED / cc-05 GREEN / cc-03 GREEN,
> g-115-3803) have NOT had this discriminator applied — that is not a claim they
> are env-dependent, only that the cheaper test has not been run. (guard-2015,
> rb-5907.)
>
> **"Pre-existing" is not "tracked" — verify the tracking ID, do not inherit it.**
> The row above carried a wrong ID for a day. `g-115-3446` is a COVERAGE-gap goal
> (add a pin for an untested branch); `g-115-3443` tracks two red contract-pins in
> *different* files and only CITES `test_fleet_config_parity.py` to record it was
> 28/28 green on 07-27 — which dates the regression rather than owning it. Neither
> tracked these two tests, so a GENUINE failure sat unowned while every reader of
> this row was told it was handled. Establishing "not caused by my change" is the
> easy half and it is where the check usually stops; a failure you have correctly
> exonerated yourself of still needs an owner. Open the cited goal and confirm it
> names the failing TESTS — a shared file path is not ownership.
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
> named below carry no timeout signature — when they DO fail, the failure is real.
> ~~still failing~~ **RESOLVED — see the Windows row below; all 6 now pass on both
> platforms.** (`test_pending_deploys_gate`, `test_pre_apply_consult_gate_scope`,
> `test_pending_deploys_stop_hook`, `test_iteration_push`,
> `test_infra_streak_dedup_sh`, `test_git_merge_ayoai_ledger`)
>
> **2026-07-27 — WINDOWS row, closing the portability question the row above left
> open** (alpha, `DESKTOP-O91DLK2`, Windows 10 19045 / MSYS2 MINGW64, `sys.platform
> = win32`, 4 chunks, fleet quiet — omni stopped for the promotion):
> **6,144 passed / 0 failed / 0 errors.** The cc-04 entry above states
> *"Unmeasured by me: whether those 12 still fail on a Windows box."* Measured now:
> the 6 called GENUINE were re-run explicitly on Windows — **59 passed, 0 failed.**
>
> So this is **NOT** a portability finding. Both platforms are green, which means
> the 07-26 Windows entry (32 failed) and the 07-27 Linux entry (0 failed) are
> reconciled by TIME, not by OS: fixes landed in between. At least one is directly
> attributable — `test_infra_streak_dedup_sh` was one of 9 bare-`bash` argv[0]
> sites repaired during the v2.6.0→v2.7.1 promotion earlier the same day
> (`2b3f3ce84`, `198d29685`), which is the same `CreateProcess`/System32/WSL root
> cause the 07-25 → 07-26 narrative above describes. The remaining 5 were not
> individually attributed.
>
> Note this row obeys the "record the box and OS" instruction two paragraphs up,
> and it is the reason the reconciliation was possible at all. Do not drop that
> field. Also note the TOTAL caveat applies here too: 6,144 (Windows, 4 chunks)
> vs 6,223 (Linux, 4 chunks) is **not** evidence of missing tests — judge by the
> FAILING FILE SET, which is empty on both.
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
>
> **Never pipe that runner — not even on a finished run.** Trap 2 below forbids
> piping a LIVE run through `tail` for a buffering reason; this is a second,
> independent reason that applies to a COMPLETED run as well, and it defeats both
> safeguards named in the paragraph above at once. A trailing pipe replaces the
> runner's exit code with the pipe's (`guard-1150`), so the exit-2 INVALID signal
> is destroyed — a background-task notification will cheerfully report "exit code
> 0" for a contended run. And a bounded window (`| tail -40`) discards the
> `VERDICT` line, which every baseline row above insists is the ONLY authority on
> whether the numbers mean anything. Committed live 2026-07-30 (g-115-3855):
> `run-full-suite.sh --chunks 16 --confirm-solo 2>&1 | tail -40` produced a
> notification reading exit 0 with no verdict anywhere in the captured output.
> The result was recoverable only because all 16 chunk logs happened to reach
> `[100%]` — had one stopped short, the run would have been indistinguishable
> from a pass. Redirect to a file and Read it (as trap 2 already prescribes);
> never pipe.

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
| Any change whose test coverage lives in a pytest-INVISIBLE file — a `main()`-style `.py` (no top-level `def test_`) **or any `.sh`, which pytest cannot collect at all**. Measured 2026-07-29 (cc-05): 71 `.py` + 19 shell = 90 files. Do not trust that count; re-derive with `bash core/scripts/tests/run-invisible-suites.sh --list`, which prints the split. | `bash core/scripts/tests/run-invisible-suites.sh` — dynamic population runner; known-reds are quarantined inline with their tracking goal IDs (g-115-2349 baseline sweep found 9 silent reds of 69). **Since g-115-3957 this runner is invoked automatically by `core/scripts/run-full-suite.sh`**, so a full-suite run already covers it; invoke it directly only when you want the invisible half alone. | runner exits 0 (`N/N files passed, M quarantined`) |
| `mind_api/src/*.py` | `python -m pytest core/scripts/tests -q` (runtime is exercised by daemon-aware wrappers in core/scripts/tests) | exit 0 |
| `core/scripts/*.sh` (production wrapper) | Whatever the wrapper's daemon endpoint suite covers — typically `python -m pytest core/scripts/tests -q -k <endpoint>` | exit 0 |
| `.claude/skills/*/SKILL.md` | Re-read the edited pseudocode + `bash core/scripts/domain-leak-check.sh`; if the change alters skill BEHAVIOR (not just prose), also `/verify-learning` for cross-skill grep checks. (Do NOT use `skill-evaluate.sh` here. A bare `skill-evaluate.sh <skill-name>` errors `unknown subcommand`: it needs a subcommand (read/report/underperforming/score), and `score --skill <s> --goal <g>` rates RUNTIME skill-on-goal performance, not a static SKILL.md edit.) | re-read confirms intent; domain-leak-check clean; verify-learning passes if behavior changed |
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
