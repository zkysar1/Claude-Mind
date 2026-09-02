# Run-Full-Suite Baseline Ledger

Extracted 2026-08-17 (g-115-6469) from `.claude/rules/run-full-suite-after-deep-code.md`,
where these rows occupied 46,594 bytes — **12.8% of the entire always-on fleet preamble**
(362,480 bytes across CLAUDE.md + 33 unconditional rules, measured on alpha/cc-04).

These are DATED MEASUREMENT WAYPOINTS, not behavioral rules. The rule file kept the
behavior (the ladder-is-a-retry-protocol lesson, VERDICT-first, TOTAL-is-not-comparable,
the NUL-byte log-corruption discriminator, the false-GENUINE chunk-confinement tell) and
points here for the evidence behind each.

**Read this when**: you are triaging a specific suite failure, checking whether a named
red is known, or adding a new baseline row. Do NOT read it to learn the rules — those
stayed in the rule file, which is the SSOT for behavior.

**Adding a row**: record `hostname` and `uname -r` VERBATIM, never a nickname — the
nickname-collision finding below is why. Prefer folding a correction into an existing
row over appending an eleventh.

---

> **RE-BASELINED 2026-07-26 (g-115-3085 Layer 2 landed, alpha). The 2026-06-17
> AND 2026-07-25 figures are both HISTORICAL — do not compare against either.**
>
> **NAMED-RED ROSTER — re-measured 2026-08-09 (alpha, `hostname` cc-04, `uname -r`
> 6.8.0-136-generic, own-cloud, live fleet). Read this BEFORE triaging any failure
> named in the rows below.** Those rows accumulate red claims in prose scattered
> across ten baseline entries, and nothing ever re-checked them — so a reader
> inherits a twelve-day-old red as current. Targeted solo re-runs of every file the
> rows still name as red, `STORAGE_BACKEND=local`: all four **GREEN on this box**.
>
> | file | tracker | what the rows below say | measured 2026-08-09 cc-04 |
> |---|---|---|---|
> | `test_fleet_config_parity` | g-115-3803 | RED 7x on a box called "cc-04" | **57 passed / 0 failed** |
> | `test_completed_not_committed_sweep` | g-115-4269 | green solo, red in-suite | 63 passed solo — consistent; solo cannot falsify an in-suite claim |
> | `test-wm-prune-cadence-protection.sh` | g-115-3799 | "fails SOLO ⇒ genuine" | **INTERMITTENT, not box-split** — cc-03 flipped RED→GREEN solo in 24h, see below |
> | `test_email_read_listing_assertion.sh` (domain) | g-335-586 lane | 9/15 sub-assertions red | **rc=0, 19 passed / 0 failed** |
>
> **`test-wm-prune-cadence-protection.sh` — four SOLO measurements, and the fourth
> retires the "box-split" reading this block previously carried.** All solo, all
> `STORAGE_BACKEND=local`:
>
> | date | box | agent | `uname -r` | result |
> |---|---|---|---|---|
> | 2026-08-09 | cc-04 | alpha | 6.8.0-136-generic | rc=0, 5/5 |
> | 2026-08-10 | cc-03 | echo | 6.8.0-136-generic | rc=1, `CASE last_goal_category FAIL: expected evicted, got val='infrastructure'` |
> | 2026-08-11 | cc-02 | zeta | 6.8.0-136-generic | rc=1, **byte-identical** assertion |
> | 2026-08-11 | cc-03 | echo | **6.8.0-137-generic** | **rc=0, 5/5** — incl. `last_goal_category PASS: evicted` |
> | 2026-08-13 | cc-04 | alpha | **6.8.0-137-generic** | **rc=1**, byte-identical assertion, `val='infrastructure'` |
>
> ⚠ **THE KERNEL TERM IS FALSIFIED, AND THE MECHANISM IS NOW MEASURED — row 5 answers
> the prediction this block used to carry.** cc-03 went red@136 → green@137; cc-04 went
> green@136 → **red@137**. Both kernels now hold a green AND a red, and two boxes reverse
> across the same bump in OPPOSITE directions. No platform term survives that, so the
> confound the 4th row raised is closed: stop re-measuring `uname -r` here.
>
> **Read the failing VALUE, not the rc — it is the whole diagnosis.** The harness seeds
> all five slots as `test_value_for_<slot>` in the **LIVE** wm file (`wm_path = r'$WM_FILE'`,
> line 46), runs the real `wm-prune.sh`, then restores a `cp` backup via an EXIT trap. The
> red returns `'infrastructure'` — a live category, **NOT** the seeded string. A prune that
> merely failed to evict would return the SEED. So the live loop rewrote that slot between
> seed and read: a race, not a prune defect. Confirmed on cc-04 the same minute — live
> `wm-read.sh last_goal_category` was `infrastructure`, and the four PASS cases matched
> their seeded strings exactly. Those four are cadence slots written every ~25 goals;
> `last_goal_category` is the only one of the five written on EVERY goal close, which is
> why it alone loses the race. Busy agent ⇒ red, quiet agent ⇒ green, on any box.
>
> **CORRECTED 2026-08-17 (alpha, `hostname` cc-04, `uname -r` 6.8.0-137-generic): the
> race reading above is RETIRED — the collision is STRUCTURAL, and no write inside the
> seed→read window is needed at all.** `last_goal_category` is a TOP-LEVEL WM key
> (`wm.py:109 TOP_LEVEL_KEYS`), written top-level by every reducer state-update, and
> `wm-read.sh` returns the top-level value regardless of the slot. Measured: the red
> fired with `val='ayoai-platform-services'` — a value written HOURS earlier, with zero
> goal closes between seed and read — and a direct yaml read of the file showed
> `slots.last_goal_category` ABSENT post-prune, i.e. **the eviction succeeded and the
> check read a different key**. So: busy-vs-quiet tracks whether the box's session ever
> wrote the top-level key, not a race window; the seed string can never be returned on a
> box where the top-level key is non-empty; and the four sibling cases pass only because
> their names are not in TOP_LEVEL_KEYS. Fix shape unchanged (isolation) PLUS assert on
> `slots.<name>` directly, or seed a name outside TOP_LEVEL_KEYS. Full trace on
> g-115-3799's progress_note.
>
> ⚠ **It also mutates production WM.** The trap restores a snapshot taken BEFORE the run,
> so any live loop write inside the window is silently clobbered (guard-1646 class). So
> the fix is isolation — point the harness at its own WM path — not "seed its own slot".
>
> **What the flip DOES buy is a mechanism, which three consistent reds could not.** The
> failing case asserts a value was EVICTED, and the red runs report the slot still
> holding `'infrastructure'` — a real live category, not a fixture string. So the test
> reads LIVE working memory: it passes when the slot happens to be stale-enough to
> evict and fails when a recent goal has just refreshed it. That is env-dependence, and
> it explains every row above WITHOUT invoking platform at all — which matters, because
> the byte-identical cc-02/cc-03 assertion was previously read as evidence of a shared
> defect when it is equally the signature of a shared ambient condition. `g-115-3799`
> owns it; the env-diff discriminator (guard-2015 / rb-5907) is now a mechanism CHECK
> rather than a portability question, and the cheaper probe is to make the test seed
> its own slot instead of reading the agent's.
>
> **`test-infra-health-streak.sh` reproduces the same shape on a SECOND box, with the
> payload proving it** (echo, cc-03, 2026-08-11, solo): `rc=1`, `CASE 2/default FAIL:
> exit=0 (expected 1)` — byte-identical to zeta's cc-02 run — and the emitted JSON is
> `{"threshold": 3, "alert_count": 0, "components": []}`. The test expects a streak
> alert; live health data has none to give. **g-115-4316** already hypothesises "asserts
> against LIVE health data"; this is that hypothesis measured, not merely restated, and
> `g-115-3367` is the sibling owner. **`rb-4013` names the mechanism** (itself marked
> inferred-not-verified, so carry that caveat): the streak is computed from ACCUMULATED
> infra-health probe history, so without a fresh `infra-health check-all` writing an
> in-window failure record it returns 0 even for a component down for days — measured
> once as a 10-day-old failing streak reported as "healthy". That makes the red a
> FIXTURE-FRESHNESS failure, not a code defect, and predicts the test goes green if a
> `check-all` runs first. **Prediction run, and the result is stronger than the
> prediction** (echo, cc-03, 2026-08-11, solo, `STORAGE_BACKEND=local`):
>
> | case | stale health data | after a fresh `check-all` |
> |---|---|---|
> | 2/default (expects alerts) | **FAIL** exit=0, alert_count=0 | **PASS** exit=1, alert_count=2 |
> | 3/tight-window (expects none) | *not reached* | **FAIL** exit=1, expected 0 |
>
> So CASE 2 needs in-window failures to EXIST and CASE 3 needs them ABSENT — the two
> cases have contradictory freshness requirements, and **no state of the live health
> store satisfies both.** The test is therefore not flaky-but-fixable-by-environment; it
> is unsatisfiable against ambient data by construction, so seeding is the only remedy
> rather than the preferred one. Anyone tempted to "fix" it by running `check-all` first
> will convert a red CASE 2 into a red CASE 3 and think they regressed something.
> Two things that shorten the fix, and one correction: the test's OWN header (line 15)
> already says "test needs a seeded fixture file + `--health-file` override", so the
> fixture need is a documented pre-existing limitation, not a discovery — only the
> unsatisfiability is new. And a seam ALREADY EXISTS at `infra-streak-notify.sh:199`
> (`FRESHNESS_JSON='{}'`, commented "test seam: injected alerts"), so wiring cases 2/3
> through it may beat building a harness. Unmeasured: whether that seam reaches the
> alert-count assertion or only the freshness gate. (Corollary for the sibling above: a `check-all` also surfaced a `bridge` /
> `roblox-studio` streak dating to 2026-07-03 that was invisible beforehand — 39 days of
> a real failing streak reported as 0 alerts. It is a known `human_gated` condition, not
> a new incident, but it is rb-4013's false-negative reproduced at fleet scale.) Both invisible-suite reds are owned; neither is new.
> Note both files fail the SAME way — asserting against ambient agent state rather than
> a seeded fixture — so they are likely one fix, not two.
>
> **RESOLVED — the g-115-6522 trio (2026-08-18, alpha, `hostname` cc-04, `uname -r`
> 6.8.0-137-generic): `test_recurring_loop_state_mutate.py`, `test_wm_advisory_lock.py`,
> and `test_class_balance_cross_session.py::test_empty_journal_fallback` were all one
> mechanism, and it is NOT the ambient-state shape the paragraph above predicts.** All
> three were GREEN solo on cc-04 and red only on worker-Body boxes (cc-07), because
> bash-agent-inject injects `BODY_WM_PATH` there and `wm.wm_path()` checks it FIRST —
> outranking `MIND_AGENT`, `MIND_AGENT_DIR`, and every tempdir fixture (guard-3375's
> measured mechanism). Reproduced on cc-04 by exporting a fake `BODY_WM_PATH`: pre-fix
> code failed 8/8 with live-like counters AND mutated the pointed-at WM (657→665);
> post-fix code passed 8/8 with the file untouched. Fix: each test now pins or pops
> `BODY_WM_PATH` per guard-862. The `_mw1-test-<hex>` polluter warning named in
> g-115-6522 is COSMETIC — it fires in green and red runs alike (WORLD/META
> fall-through for a temp agent without local-paths.conf), and is not a failure cause.
> Note for the sibling rows above: `test-wm-prune-cadence-protection.sh` (g-115-3799)
> and `test-infra-health-streak.sh` (g-115-3367) remain OPEN — their mechanisms
> (TOP_LEVEL_KEYS collision; live health data) are distinct and NOT closed by this fix.
>
> A green here does **not** close any of those goals: the nickname-collision row
> below establishes that "cc-04" names at least two machines, and one box's green
> is not evidence about another's red (guard-2015). What it does mean is that you
> must not begin a triage from the prose alone — re-run the file solo first. It
> costs seconds, and two of these four contradict what the rows assert about them.
>
> **Keep this roster current instead of adding an eleventh baseline row.** The rows
> below already establish that a fresh TOTAL is not comparable across runs and that
> the chunk rung is not inheritable, so another whole-suite number buys nothing
> while the file sits under read-cap pressure (the g-115-4058 folding practice).
>
> | | 2026-06-17 | 2026-07-25 | 2026-07-26 | **2026-07-27 (cc-04, Linux)** |
> |---|---|---|---|---|
> | tests run | 2,234 | 5,226 | 5,969 | **6,223** |
> | passed | 2,231 | 5,199 | 5,937 | **6,223** |
> | failed | 2 | 20 | 32 | **0** |
> | errors | 0 | 2 | 0 | **0** |
> | run completes? | yes | **no** — needed `--ignore`, a chunk died at 51% | yes, all 6 chunks 100% | **yes, all 4 chunks, VERDICT: CLEAN** |
>
> **cc-06 FIRST BASELINE (2026-07-30, omni, g-029-87). 86 failed / 0 errors, and the
> runner classified them GENUINE rather than contended — so this is NOT the
> progressive-exhaustion profile. Do NOT diff cc-06 against the cc-04 column: they
> are different boxes, which is precisely why the row above demands the box+OS
> field.** Two facts to carry:
>
> 1. **pytest here is 7.4.4 from the distro package (`apt install python3-pytest`),
>    not pip.** This box has no pytest in the base image and pip refuses under PEP 668
>    (externally-managed); `--break-system-packages` was deliberately NOT used, since a
>    live daemon serves the fleet off this same interpreter. 7.4.4 warns
>    `Unknown config option: faulthandler_exit_on_timeout`, so **the hang-bounding
>    described below does NOT apply on cc-06** — a genuinely hung test will buffer
>    rather than abort with a traceback. That warning is config-only and is *not* a
>    failure cause; do not attribute failures to it without evidence.
> 2. **30 of the 86 are in files this rule ALREADY names as pre-existing**
>    (`test_iteration_push` 7, `test_provision_from_vault_agent_scope` 13,
>    `test_provision_github_from_vault` 7, `test_provision_from_vault_default_out` 2,
>    `test_monitor_tick` 1). The remaining 56 across 31 files are new-to-this-box and
>    untriaged. At least some are **domain-coupled, not broken**:
>    `test_capability_gate_imperative_noun` (5) asserts on fixtures naming an
>    upstream-domain service that has 0 occurrences in this deployment's world
>    convention, so it cannot pass against this world at all. Triage by asking "does
>    this fixture assume the upstream domain?" before calling it a regression.
>    Chasing that question is what surfaced g-029-93, a real live defect the test
>    itself was not reporting.
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
> AMENDED 2026-07-31 (g-115-4140, same box, folded per the g-115-4058 size
> practice): **the ladder extends to 24** — 20 chunks INVALID (chunk 02 stopped
> at 96% behind a clean-looking `TOTAL: 7643 passed, 2 failed`) → 24 chunks
> VERDICT GENUINE at 7511 passed / 2 failed / 0 errors, ~40 min apart, live
> fleet. So 8 → 12 → 16 → 20 → 24, still open at the top, and a rung that was
> CLEAN on this box yesterday (20, the row above) contended today — third
> same-box confirmation that no rung is inheritable, including from your own
> prior run. The 2 fails were the `test_fleet_config_parity` pair again
> (**g-115-3803**, chunk-07 log). Also the first row where the DOMAIN half
> carried a pre-existing owned red (`test_email_read_listing_assertion.sh`,
> 9/15 sub-assertions, identical across both runs): rc=1 split per item 2
> localized it, and an aspirations-query by test name found the owning pending
> Fix goal — grep the failing FILE name against the world queue before filing.
> RE-AMENDED hours later (g-115-4137, same box): **the ladder extends to 28** —
> 20 INVALID → 24 harness-killed → 24 INVALID (chunk 04 stopped at 94%) →
> 28 chunks VERDICT GENUINE at 7537 passed / 2 failed / 0 errors. The rung
> that was GENUINE on this box ~12h earlier (24, this row) contended twice
> today; fourth same-box confirmation that no rung is inheritable. The 2
> fails were the same `test_fleet_config_parity` pair (**g-115-3803**, red
> solo again — 4th+5th consecutive red measurement on this box).
>
> **STOP CLIMBING THE LADDER FIRST — INVALID has TWO causes and escalating the
> rung only fixes one.** Every row above treats INVALID as contention, so the
> prescribed response is a higher rung. On an own-cloud box that advice can
> loop forever, because the runner writes its chunk logs into
> `agents/<agent>/temp/suite-run` — *inside the synced tree* — and the sync
> rewrites logs mid-run. The runner then reads a truncated log, sees a chunk
> that never reached 100%, and returns INVALID for a run that actually
> COMPLETED. No rung can fix that.
>
> Measured 2026-07-31 (foxtrot, `hostname` = LAPTOP-3IOFCNEO, `uname -r` =
> 6.6.87.2-microsoft-standard-WSL2, `STORAGE_BACKEND=own-cloud`, 5 agents
> active within 18 min), same tree, same box, same load:
>
> | logs | rung | verdict |
> |---|---|---|
> | inside synced tree (default) | 28 | INVALID |
> | inside synced tree (default) | 32 | INVALID |
> | inside synced tree (default) | 36 | INVALID (`chunk 15 stopped at 82%`) |
> | **outside synced tree (`--out`)** | **32** | **GENUINE — trustworthy** |
>
> That valid run: **7942 passed / 3 failed / 0 errors**, invisible-suites 94/94,
> 0 quarantined. Both failing files are owned and neither is new — the
> `test_fleet_config_parity` pair is **g-115-3803** (6th+7th consecutive red on
> this box), and `test_completed_not_committed_sweep` is **g-115-4269**, filed
> from this run: it passes SOLO (63/63) and fails only in-suite, so it is
> test-order pollution — a third category this rule does not otherwise name,
> and one where guard-1448's "green solo ⇒ environmental" discriminator and the
> runner's GENUINE verdict disagree while both are right about what they measure.
>
> **The discriminator is NUL bytes, and it is one command.** The 36-chunk run
> carried 8 NUL bytes total, every one of them in the single chunk the runner
> flagged (`chunk-15.log`, truncated to 320 bytes); the non-synced run carried
> **zero across all 32**. Corroborate with mtime: chunk-15 was stamped 11:39:20
> while chunks 16 and 17 were 11:39:19, though chunks run sequentially — the
> file was rewritten *after* the runner read it. The flagged chunk's surviving
> text even contains `100%`.
>
> ```bash
> for f in <logdir>/chunk-*.log; do n=$(tr -dc '\0' < "$f" | wc -c); \
>   [ "$n" -gt 0 ] && echo "$(basename $f): $n NUL"; done
> ```
>
> Any NUL bytes ⇒ suspect log corruption, not contention.
>
> ⚠ **BUT DO NOT READ THE CONVERSE. Zero NULs is NOT evidence of contention —
> the commonest form of this corruption carries none at all.** Measured
> 2026-08-17 (alpha, `hostname` cc-04, `uname -r` 6.8.0-137-generic, own-cloud,
> g-115-6409): every truncated capture had a **clean prefix, ZERO NUL bytes, and
> rc=0**, which is indistinguishable from a short run. The NUL check is a
> one-directional tell; treating it as a filter is what lets the silent variant
> through, and a reader who runs the loop above and sees nothing will climb the
> chunk ladder for hours against a cause no rung can fix.
>
> **RESOLVED 2026-08-17 — the default log dir MOVED, so `--out` is now a
> preference, not a remedy.** `run-full-suite.py` defaults to
> `<tmpdir>/ayoai-suite-run-<agent>`, off the synced tree; the bash wrapper
> follows automatically because it ASKS via `--print-out-dir` instead of
> re-deriving. Nothing to pass. If you are on a build that predates this, the
> old workaround still applies:
> `bash core/scripts/run-full-suite.sh --chunks 32 --confirm-solo --out /tmp/<non-synced-dir>`
>
> **The mechanism, measured rather than inferred: the sync layer REPLACES the
> file at a NEW INODE while the writer still holds an fd on the old one.** An
> inode watch caught it directly — `ino=2010435 size=0` → `ino=2009953 size=551`,
> then frozen while the producer ran 71 more seconds into the orphaned inode.
> That explains every symptom at once: clean prefix, no NULs, rc=0, and why
> **duration is the discriminator and size is not** — a 13.2 MB fast write
> survives intact while a 60-second trickle does not. Paired control, same
> producer and flags, ~1 min apart: synced sink **0 bytes**, non-synced sink
> **129,157 bytes**, both rc=0. It reproduced spontaneously on an unrelated
> framework script mid-investigation, so it is not specific to the runner.
>
> This was the CAUSE behind the detection `run-full-suite.py`'s own g-115-3387
> comment documents. **g-115-3253** filed it LOW on the belief the effect was
> cosmetic ("a reader cannot see how many ran"). It was not: it corrupted the
> runner's completeness check, the one field this whole rule tells you to
> believe. Cost on first encounter: 3 false INVALIDs, ~2.5h. Evidence: board
> `msg-20260731-121551-foxtrot-5368`. Still unmeasured: whether the NUL-carrying
> variant foxtrot saw is the same swap caught mid-rewrite or a second signature.
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
> **So: do NOT excuse a failure in those files as environmental any more** — they
> carry no timeout signature, so when they DO fail the failure is real. The six
> that were red here (`test_pending_deploys_gate`, `test_pre_apply_consult_gate_scope`,
> `test_pending_deploys_stop_hook`, `test_iteration_push`, `test_infra_streak_dedup_sh`,
> `test_git_merge_ayoai_ledger`) are **RESOLVED** — all pass on both platforms, see
> the Windows row below. The instruction is about the CLASS, not a live hunt list.
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
> **When the verdict is NOT clean, run `bash core/scripts/run-full-suite.sh --triage`**
> (g-115-4321). It re-reads the chunk logs the run already wrote — it does NOT re-run
> the suite — and chains the triage every row below does by hand: position-bucket (via
> the same `classify()`, so it cannot disagree with the printed verdict) → solo re-run
> per candidate (green solo ⇒ ENVIRONMENTAL, red solo ⇒ GENUINE) → **ownership** →
> reports only genuine-AND-unowned as FILE THESE. Ownership now also runs inline on a
> GENUINE verdict, because that is the step these rows keep skipping: it queries the
> failing file's stem **both with and without the `test_` prefix**, since
> `--title-contains` matches TITLES only and titles routinely drop that prefix —
> `test_fleet_config_parity` returns 0 hits where `fleet_config_parity` returns 3,
> including its open owner. Keying on the stem alone reports a tracked test as unowned
> and files a duplicate.
>
> **`--triage` now DECLARES the halves it did not read (g-115-4710).** It globs
> `chunk-*.log` and nothing else, so its verdict was scoped to the chunked pytest half
> while saying nothing about the other three — and a silent exclusion reads as coverage.
> Measured 2026-08-02 (g-115-4447, echo, cc-03): it printed `2 environmental | 0 genuine`
> while two shell files in the invisible half were red SOLO, i.e. genuine. Every triage
> report now opens with a `SCOPE` block naming the invisible, deferred and domain halves
> with each one's recorded PASS / `FAIL(rc=N)` / `DID NOT RUN` / **`NOT RECORDED`**, and
> a recorded failure both restates itself at the "Nothing to file" line and forces a
> non-zero exit. Two things this does NOT change: `NOT RECORDED` (a log dir written
> before this landed, or a direct `run-full-suite.py` call) is a statement of ignorance,
> never a pass — do not read it as either; and the SCOPE block is the only structural
> part, so the separate `^FAIL` grep below is still the way to read a *run* log, since a
> run and a triage are different invocations.
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

> **2026-08-17 run record (alpha, `hostname` cc-04, `uname -r` 6.8.0-137-generic,
> own-cloud, live fleet, auto-chunked at 4, logs at the new tmpdir default).**
> `TOTAL: 14147 passed, 42 failed, 0 errors` / `VERDICT: GENUINE` — **false, again,
> at the LOWEST rung yet**: all 42 confined to chunk 02 (chunks 00/01/03 read 0
> failed, chunk 03 clean AFTER the peak), and the full 8-file failing set re-ran
> solo **90/90 green** in one process. The known trio reproduced at its exact
> byte-identical counts (`test_pipeline_tombstone_archival` 15,
> `test_pipeline_provenance_stamps` 8, `test_pending_questions_close` 6) — fifth
> box-occurrence — plus five NEW faces in the same chunk
> (`test_reflection_quality_log_producer` 4,
> `test_prose_verification_drift_daemon_parity` 4, three `test_retrieve_*` at
> 2/1/1), so the signature's file set GROWS with chunk size (240 files/chunk at
> rung 4 vs 59 at rung 16) while the trio's counts stay fixed. Same-run shell
> half: `test-wm-prune-cadence-protection.sh` red — that one is NOT contention;
> see the CORRECTED top-level-key-collision paragraph in the named-red roster
> above (structural false red, owner g-115-3799).

> **2026-08-17 run record (alpha assistant session, `hostname` cc-10, `uname -r`
> 6.8.0-137-generic, own-cloud, live fleet, auto-chunked at 4, tmpdir default logs;
> tree = the g-358-11 gzip-codec commit ad2ae3207).** 39 failed / `VERDICT: GENUINE`
> — **false, sixth box-occurrence of the chunk-02-at-rung-4 signature**, three hours
> after the cc-04 record directly above and with the SAME file set: the trio at its
> byte-identical 15/8/6 (`test_pipeline_tombstone_archival`,
> `test_pipeline_provenance_stamps`, `test_pending_questions_close`) plus
> `test_reflection_quality_log_producer` 4, `test_prose_verification_drift_daemon_parity`
> 4, `test_retrieve_daemon_readonly_false` 1, `test_retrieve_entry_type_endpoint_e2e` 1;
> chunks 00/01/03 read 0 failed; the 7-file set re-ran solo **71/71 green** in one
> process. Every red was the `ValueError: <tmp>/world/... is not under any configured
> root` raise from `owncloud_backend._rel`. **The open root cause named in the rule
> now has an owner: g-115-5651** — `get_backend()` memoizes `_ACTIVE_BACKEND`
> process-wide, so an own-cloud-shaped test earlier in the chunk poisons every later
> tmp-world test in that process regardless of the `STORAGE_BACKEND=local` pin (the pin
> reaches backend SELECTION, but selection runs once per process). Same-run other
> halves: invisible 105/105, domain 53/54 (the pre-existing contract-deadline Java
> shape drift, unrelated), `mind_api/tests` run separately after: 1 failed
> (`test_runtime_team_state_write::test_byte_compat_update`, tracked by its own
> pending Fix goal — CLI stamps `strategic_focus.set_at`, daemon leaves it null; not
> a codec path) — the byte-compat baseline this rule quotes as "7 known reds" read 1
> on this box.

> **2026-08-18, alpha, `hostname` cc-04, `uname -r` 6.8.0-137-generic, own-cloud,
> live fleet, chunk rung 4 (working tree = the g-115-6538 iteration-push commit
> 3d2bf52a8).** `TOTAL: 14293 passed, 40 failed, 0 errors` / `VERDICT: GENUINE`
> — **false, SEVENTH occurrence of the chunk-02-at-rung-4 signature**, and it adds
> nothing new to the diagnosis: same file set, same trio at its byte-identical
> **15/8/6** (`test_pipeline_tombstone_archival`, `test_pipeline_provenance_stamps`,
> `test_pending_questions_close`) plus `test_reflection_quality_log_producer` 4,
> `test_prose_verification_drift_daemon_parity` 4, and one each from
> `test_retrieve_supp_membership_e2e` / `test_retrieve_entry_type_endpoint_e2e` /
> `test_retrieve_daemon_readonly_false`. Per-chunk: 00 `3224 passed, 0 failed`,
> 01 `3208, 0`, 02 `3626, 40`, 03 `4235, 0`. All 8 files re-ran solo **72/72 green**
> in two processes. 62 occurrences of the `ValueError: <tmp>/world/pipeline.lock is
> not under any configured root` raise from `owncloud_backend._rel` — the
> `get_backend()` `_ACTIVE_BACKEND` process-wide memoization owned by **g-115-5651**.
>
> **The row is worth keeping only for what it says about the ROW COUNT.** Seven
> occurrences across at least three boxes, two chunk rungs and two chunk indices,
> with a byte-identical 15/8/6 core and a named owner, is no longer evidence being
> gathered — it is the same measurement re-paid at ~20 min of wall clock per deep
> closure, by every agent that touches framework code. What is NOT yet recorded
> anywhere is whether the classifier could name this cheaply: the raise is a single
> distinctive string in the chunk log, and a run whose failures are 100%
> `owncloud_backend._rel` under a `local` pin is mechanically distinguishable from
> one that is not. Until that exists, the standing advice holds and the eighth
> reader should re-run solo rather than trust GENUINE — but should file against
> g-115-5651 rather than add a row here.
>
> Same-run other halves: invisible **103/105** — `test-wm-prune-cadence-protection.sh`
> (RED solo too, genuine, g-115-3799 TOP_LEVEL_KEYS) and, in an immediately-prior
> run on this box 40 min earlier, `test_aspirations_claim_source_flag.sh`, which was
> **GREEN SOLO 6/6** and did NOT recur in this run. That second file is owned by
> g-115-3376 + g-115-3692, whose premise reads "4/6 red on stale expectations" —
> that premise now looks stale itself and wants a re-derive by whoever holds it.
> Domain **54/54**, 1 skipped. `mind_api/tests` deferred, not run.

## 2026-08-20 — alpha, `hostname` cc-10, `uname -r` 6.8.0-137-generic (the fold-back acceptance runs, g-115-6942)

> **`mind_api/tests` left `DEFERRED_TESTPATHS` this day** — these are the runs
> that justified it. (1) Standalone post-fix baseline: **1,386/1,386 green**
> (the tree's first fully green standalone; the prior session's baseline had 4
> genuine reds — set_at daemon/CLI parity, 2×claim-sid harness, citation lane —
> all fixed, not skipped, in `a2b8d94d7`). (2) `RUN_DEFERRED=1` full run:
> `VERDICT: INVALID (tree-moved)` — self-inflicted, a commit landed mid-run
> (rb-8554: commit FIRST, then measure) — but its deferred half launched
> post-commit in its own process at END of invocation, the historically fatal
> position, and was **green at [100%]**. (3) Folded acceptance run (default
> path, 4×275-file chunks): `TOTAL: 16,099 passed, 5 failed, 0 errors` /
> `VERDICT: GENUINE`; `--triage`: **1 environmental, 4 genuine-owned
> (g-115-6805, g-115-6759, g-115-6840, g-115-5637), 0 unowned — none in
> `mind_api/tests`**. The new `deferred PASS — "deferred set empty"` half
> record fired.
>
> Same-run other halves: invisible 108/109 — `test-infra-health-streak.sh`, a
> PREDICTED live-state decay (its own header foresaw it: if future maintenance
> clears the tracked live component, the test needs a seeded fixture +
> --health-file override); fixed
> that way this day (hermetic fixture + `--health-file` on streak-alert),
> invisible re-run **109/109**. Domain 55/56 + 1 skipped — the red is
> `test_contract_deadline_alert_discrimination.py::test_producer_shape_has_not_drifted`,
> owned g-115-6952.

> **2026-08-21 (temp/scratchpad plan closure, bravo, `hostname` DESKTOP-O91DLK2,
> `uname -r` = MSYS2 3.5.7-2.x86_64 / Windows 10 19045, `sys.platform = win32`,
> assistant session, no local autonomous fleet, 4 chunks × ~281 files):
> `TOTAL: 16,588 passed, 62 failed, 0 errors` / `VERDICT: GENUINE`, counts
> rising toward the tail (4/11/17/30). `--triage`: **1 environmental |
> 20 genuine-owned | 0 genuine-UNOWNED** — owners g-115-6805, g-115-7097
> (filed for this same box's earlier runs), g-115-6967. Domain+invisible
> failing families (`probe_web_surface`, `secret_scope_census`,
> `deploy-hold-check`, `stale_jobs_scan_probe`, `check-sh-exec-bits`) all
> carry live pending owners too — swept by id, none unowned. The four
> commits under test (aae4570e0..9ea1ffe71: purge watermark + git guard,
> housekeeping-tick, temp_drain_stalled escalation, scratchpad closure)
> appear in NO failing set; their targeted suites (purge shell suite,
> 20 tick tests, 34 precheck tests, 26 hook tests) all green. Note the
> tail-rising distribution was NOT contention this time: solos stayed red
> and every red was pre-owned — the discriminator did its job in the other
> direction.

> **2026-08-24 (g-367-14 confirmatory suite, alpha WORKER Body, `hostname` cc-07,
> `uname -r` 6.8.0-137-generic, own-cloud box with `STORAGE_BACKEND=local` pinned,
> HEAD 277ad3fdf, 4 chunks × ~291 files, 1166 files across mind_api/tests +
> core/tests/gates + core/scripts/tests, logs via `--out /tmp/suite-g367-14`):
> `TOTAL: 17,372 passed, 34 failed, 0 errors` / `VERDICT: GENUINE`, distribution
> **0/9/22/3** — spread across three chunks with chunk 02 dominating, i.e. NOT
> chunk-confined, so the confinement tell would have under-fired (the cc-08
> pattern, not the cc-03 one). `--triage`: **1 environmental | 9 genuine-owned |
> 0 genuine-UNOWNED**, owners g-115-7127 and g-115-5210. The other two halves,
> which never ride under the chunked verdict: invisible **108/111**, 3 reds all
> owned — `test_capability_gate_narrative.py` (g-115-7346),
> `test_stale_sentinel_canary.py` (g-115-5280),
> `test-wm-prune-cadence-protection.sh` (g-115-7389); domain **64/65 units + 1
> skipped**, its one red unit being the pytest batch's 2 pre-owned tests
> (`test_email_send_outreach_gate` g-115-7297, `test_emitter_header_census`
> g-350-316). **Zero unowned reds anywhere, and zero failures touching
> `category_suggest`** — the commit under test (1197482fd, the fourth
> daemon-reachable `build_concept_index` call site) appears in no failing set,
> and its gate file is 16/16 green solo.
>
> Two method notes measured here. (1) The **task-notification exit code lied
> again**: it reported "completed (exit code 0)" against `RUNNER_EXIT=1`, because
> a trailing `echo` in the backgrounded command replaced the runner's status.
> The PreToolUse trailing-echo advisory PREDICTED this at launch time and was
> correct — that advisory is the only warning you get, since the notification
> itself carries no signal (guard-1150, verify-before-assuming 4a). (2) On a
> WORKER Body the rule's "PRIMARY path — background the suite and END the turn"
> **does not work**: the harness's `run_in_background` registers nothing with
> `background-jobs.sh` (`has-pending` measured rc=1 while three suite PIDs were
> live), so stop-hook Gate 2.6 BLOCKs the turn-end and the worker-net demands a
> `Skill(worker-loop)` re-entry — whose Phase -0.3 merge would VOID the running
> suite. The working pattern is an IN-TURN bounded wait loop
> (`EXTERNAL_WAIT=1 interruptible-sleep.sh`, which does pace accurately —
> asked 30s, got 30s), repeated across turns without ever ending the turn.

### 2026-08-28T03:0x — zeta, `hostname` cc-02, `uname -r` 6.8.0-137-generic, own-cloud, live fleet, 16 chunks

`VERDICT: GENUINE failures -- trustworthy, act on them` · `TOTAL: 17999 passed, 30 failed, 0 errors` ·
invisible + domain halves `grep -c '^FAIL'` = **0**.

Run in a **detached worktree pinned at a7e0aa7ad** with `agents/zeta/local-paths.conf` copied in,
`STORAGE_BACKEND=local`, `--out /tmp/zeta-suite-log` (off the synced tree). Env verified from
`/proc/<pid>/environ` rather than assumed — the launch's collapsed argv (`cd "$WT" VAR=... \` + continuation)
*looked* like the vars had bound to the `cd`, and on an own-cloud box a missing `STORAGE_BACKEND=local` is the
S3-key-collision class that truncated the production store on 2026-07-09. cwd and all three vars confirmed
correct. Worth doing: reading the argv would have produced a false alarm, reading `/proc` settled it in one call.

**Applied the guard-1448 discriminators rather than trusting `GENUINE`** (item 2: the verdict is fail-safe for
INVALID and NOT for GENUINE, and a small count is more suspicious). Per-chunk buckets:

    00:14  01:0  02:4  03:0  04:0  05:1  06:2  07:1
    08:1   09:3  10:0  11:1  12:0  13:2  14:0  15:1

Spread across **11 of 16 chunks, PEAKING AT CHUNK-00**, with 10/12/14 clean *after* the peak. That is
**front-loaded** — the opposite of the tail-loaded progressive-exhaustion signature and not chunk-confined — so
GENUINE is credible here. Note this is the second consecutive night this box has produced the same shape (31
failed on 2026-08-27, buckets 14/4/1/2/1/1/3/1/2/1/1, same chunk-00 peak); a stable front-loaded distribution
across nights is a standing red population, not contention.

Top failing files: `test_aspirations_query_flaglike_value` 6, `test_blocker_recheck_producer_managed_exempt` 5,
`test_completed_not_committed_scoped_probe` 4, `test_pull_signal_producer` 2, `test_agent_watchdog_worker_role` 2,
then eleven singletons incl. one in `mind_api/tests`. **None is in a file this run's change touched** — the
closure claim for g-115-6641 was scoped to that, explicitly not to "all tests pass".

**METHOD NOTE — absence from a FAILED list is not evidence a test RAN** (guard-1715). `-q` names only failures,
so `grep -c completed_not_closed` returning 0 across all 16 chunk logs is silence, not a pass. The chain that
actually closes it: the pinned worktree was confirmed to contain all 3 new tests and both `_prior_keyed` sites
(`grep -c` in the worktree, not the main repo); the file is in the collected set at index 202/1194; and its
chunk reported failures only in a *differently named* file — `test_completed_not_COMMITTED_scoped_probe`, one
letter-cluster from `completed_not_CLOSED`, which is exactly the confusion to guard against when eyeballing a
failure list.

**Timing, for anyone sizing a wait:** launched 02:27, chunk logs complete ~02:56, `VERDICT` at ~03:03 — ~36 min
total, with the last ~7 minutes spent in `mind_api/tests` (the tree folded into the chunked pool by g-115-6942),
whose pytest child buffers and writes nothing to the run log. **A flat run-log byte count for several minutes at
that stage is normal**, and reading it as a stall is the mistake to avoid; corroborate with
`pgrep -P <runner-pid>` and the child's `etimes`, which showed a live pytest at 282s then 331s. Also
re-confirmed live: `pgrep -c "[r]un-full-suite"` returns **0** against an actively running suite because the
process name is `python3` — only `pgrep -af` sees it (item 9's false-negative direction).

**2026-08-29 (alpha, `hostname` cc-14, `uname -r` 6.8.0-137-generic, local backend, pinned worktree at
a683f3f38 — the DependencyFunnelProbe commit — default rung → 4 chunks of ~310 files, launched ~15:40, VERDICT
~16:50):** `TOTAL: 18391 passed, 46 failed, 0 errors` / `VERDICT: GENUINE`, spread 20 / 18 / 3 / 5 across the four
chunks (not tail-loaded, not one-chunk-confined). `--triage`: **6 environmental | 15 genuine-owned | 1
genuine-UNOWNED** — `test_dependency_supersession_resolution.py::test_every_done_ids_build_site_is_expanded`,
solo `assert 0 == 3` ("build sites moved: found 0, expected 3", a source-grep pin drifted after 1fbe35d92) →
filed **g-115-8300**. The 2 `test_agent_watchdog_worker_role.py` reds are the pre-owned FreshnessProbe
`canonical_missing` pair (g-115-8133); every other red carried an owner. Outside the triage's scope: invisible
half 114/115 with `FAIL(rc=1) test_capability_gate_narrative.py`; domain half red
`test_email_send_outreach_gate.py::test_first_send_records_then_same_topic_from_other_agent_is_refused_rc4` —
ownership of those two NOT established in this session (item 13: pre-existing is not tracked).

**2026-08-30 (alpha, `hostname` cc-14, `uname -r` 6.8.0-137-generic, local backend, pinned worktree at
ae417b9d0 — the v2.12.38 `framework_origin` release — default rung → 4 chunks of ~313 files, launched 01:52,
VERDICT 02:38):** `TOTAL: 18663 passed, 47 failed, 12 errors` / `VERDICT: GENUINE`, spread 20 / 18 / 4+12err / 5
across the four chunks (not tail-loaded, not one-chunk-confined). `--triage`: **6 environmental | 16
genuine-owned | 1 genuine-UNOWNED** — `test_precommit_gate_coverage.py::test_gate_argv_shape_is_pinned`, the
argv pin lacking the two gates this window added (14 `check-repo-root-entries`, 15
`check-framework-origin-writes`) → fixed in 8d4097a78 instead of filed, since the drift was this session's own.
Outside the triage's scope: invisible half 114/115 with `FAIL(rc=1) test_capability_gate_narrative.py`
(g-115-148); domain half 76/76 + 1 skipped. A second run on 2bbac6343 (the pin fix + gates) was chained
behind the triage in the same out dir — the chunk logs there are overwritten per run, so read a chunk log's
mtime against the run you mean before quoting it.

**2026-08-30 (alpha, `hostname` cc-14, `uname -r` 6.8.0-137-generic, local backend, pinned worktree at
2bbac6343 — the gate-coverage pin fix on top of v2.12.38 — default rung → 4 chunks of ~313 files, launched
02:43 chained behind the triage above, VERDICT 03:33):** `TOTAL: 18681 passed, 46 failed, 12 errors` /
`VERDICT: GENUINE`, spread 20 / 18 / 3+12err / 5 — the run above minus exactly the pin red in chunk 02 (4 → 3),
everything else count-for-count identical, so no fresh `--triage` was run: the ownership map is the one
measured 50 minutes earlier. Invisible half 114/115 (the same `test_capability_gate_narrative.py`); domain half
76/76 + 1 skipped. The chained command's exit was 1 (SUITE_EXIT=1) — that is the runner reporting genuine reds,
not a run failure; read the VERDICT, not the task's exit code (item 8).

**2026-08-30 (alpha WORKER Body, `hostname` cc-07, `uname -r` 6.8.0-137-generic, own-cloud box with
`STORAGE_BACKEND=local` pinned, pinned worktree at ac534c3ff — the g-115-3128 recommender fix — default rung
→ 4 chunks of ~315 files, launched 06:15, VERDICT 07:05):** `TOTAL: 18888 passed, 47 failed, 12 errors` /
`VERDICT: GENUINE`, spread 20 / 15 / 6+12err / 6 across the four chunks — not tail-loaded, not one-chunk-confined.
`--triage`: **6 environmental | 18 genuine-owned | 0 genuine-UNOWNED** ("Nothing to file"). Domain half 77/77
+ 1 skipped.

THIS ROW CLOSES THE ITEM-13 GAP THE cc-14 ROWS ABOVE LEFT OPEN, and that is its main reason for existing.
Both of those recorded `FAIL(rc=1) test_capability_gate_narrative.py` and the first says ownership "NOT
established in this session". It is now established both ways. PRE-EXISTING, measured rather than argued: a
second worktree at `ac534c3ff~1` reproduces it **byte-identically** — same test, same 2 failures, same payload
(`matched_keyword: 'land'` from the forged skill `land-stranded-pr`, `would_block=True` where the fixture
expects False). And it is TRACKED: `g-115-7335` ("capability-gate matches the bare token 'land' from a forged
skill") names precisely that mechanism, with `g-115-7346` covering it as a suite red. So it is neither mine nor
unowned — nothing to file, and the two-point worktree diff is the cheap discriminator worth copying whenever a
red needs separating from your own change.

TWO METHOD NOTES FROM THIS RUN, both live instances of items already in the rule. (1) Item 8, in its
task-notification form: the harness reported the backgrounded run as **"completed (exit code 0)"** while the log
itself printed `=== !!! FRAMEWORK HALF DID NOT PASS (rc=1) !!! ===`. The notification's exit code is the
wrapper's, never the runner's — read the log (guard-1431). (2) Item 6's backgrounding hazard fires even when you
did not choose it: a foreground launch that exceeds the tool's 600s cap is **auto-backgrounded by the harness**,
and an auto-backgrounded run inherits no `MIND_SID`, so it silently takes no tree lock. A first attempt from the
live tree was discarded for exactly that (it had printed authoritative-looking chunk counts: 3 and 13 failures)
and relaunched in the pinned worktree. Pin the tree BEFORE launching, not after the cap surprises you.

**2026-08-30 (alpha, `hostname` cc-14, `uname -r` 6.8.0-137-generic, local backend, pinned worktree at
62e9d9d83 — the v2.12.44 release — default rung → 4 chunks of ~314 files, launched 05:14, VERDICT 06:03):**
`TOTAL: 18720 passed, 46 failed, 12 errors` / `VERDICT: GENUINE`, spread 20 / 18 / 3+12err / 5 — **count-for-count
identical to the 2bbac6343 cc-14 row above** across all four chunks, so no fresh `--triage`: the ownership map is
the one measured that morning, and the three releases between them (v2.12.42 skill_edit_gate exit-2, v2.12.43
stranded-claim-sweep, v2.12.44 start-gate binding check) added no reds. Invisible half 114/115 — the red is
`test_capability_gate_narrative.py`, whose ownership the cc-07 row directly above now settles as PRE-EXISTING by
byte-identical reproduction at `ac534c3ff~1`; that closes the item-13 gap this row would otherwise have left open
for the third time. Domain half 77/77 + 1 skipped — note the domain half GREW (76→77 units) as coach's world
scripts landed, so a unit count that moves between rows is growth, not drift. The runner's own tail is worth
quoting because it is item 6 in one line: `=== !!! FRAMEWORK HALF DID NOT PASS (rc=1) !!! === / Any green printed
above covers the invisible-suite and domain halves ONLY.`


**2026-08-30 (alpha, `hostname` cc-14, `uname -r` 6.8.0-137-generic, local backend, pinned worktree at the
v2.12.45 tag → default rung, 4 chunks of ~314 files, launched 10:12, VERDICT 11:01):**
`TOTAL: 18723 passed, 46 failed, 12 errors` / `VERDICT: GENUINE`, spread **20 / 18 / 3+12err / 5** — the same
four-chunk distribution as the two cc-14 rows above, so again no fresh `--triage`. Two things make this row worth
keeping rather than folding into its predecessors.

**The passed count moved +3 (18720 → 18723) and that is the whole delta.** v2.12.45 added exactly three tests to
`core/scripts/tests/test_worker_reducer_liveness.py` (same-box restart ADOPTs the new fp; cross-box takeover stays
LATCHED; the poll rejoins under the new runner one poll later). A passed-count delta that equals the number of
tests you added, with the failure distribution byte-identical across every chunk, is the cheapest available
evidence that a change added no reds — and it is the one comparison the `TOTAL` line CAN support. Item 5 still
holds for everything else: judge by the failing FILE SET, which here is unchanged and **fully owned — zero
unowned files across all 46+12**.

**SCOPE CAVEAT, stated because the ledger is read as coverage: this run does NOT cover what shipped.** The tag
under test is v2.12.45; the payload actually promoted to Claude-Mind (PR #67) and pulled by coach is **v2.12.46**,
which is v2.12.45 plus a merge of `origin/main` carrying a peer's work. v2.12.46 was cut only because the v2.12.45
promotion was REFUSED by seed-preflight (`registered-but-untagged: ['call-shape-census']` — the tag predated the
peer's merge, so the worktree-at-tag lacked a skill the unversioned external registry already listed; hence
guard-5583, merge origin/main BEFORE cutting the release). Invisible half 114/115, same
`test_capability_gate_narrative.py` red the cc-07 row settled as pre-existing. Domain half 77/77 + 1 skipped.
And item 8's task-notification form fired a THIRD time in a row: the harness reported **"completed (exit code
0)"** over a log whose own last line is `SUITE_EXIT=1`. Three rows, three identical misreports — treat the
notification's exit code as carrying no information about the runner at all (guard-1431).

**2026-08-30 (alpha WORKER Body, `hostname` cc-07, `uname -r` 6.8.0-137-generic, own-cloud box with
`STORAGE_BACKEND=local` pinned, TWO pinned-worktree runs bracketing one fix — g-306-379 claim-continuity —
default rung → 4 chunks of ~315 files):**

- **Run A @ `0af373baf`** (the change): `TOTAL: 18938 passed, 62 failed, 12 errors` / `VERDICT: GENUINE`,
  spread 20 / 22 / 14+12err / 6. `--triage`: **6 environmental | 21 genuine-owned | 1 genuine-UNOWNED** —
  `test_owncloud_sid_carrier_carveout.py`.
- **Run B @ `6d4e47ac0`** (the fix): `TOTAL: 18943 passed, 57 failed, 12 errors` / `VERDICT: GENUINE`,
  spread 20 / 22 / 9 / 6. `--triage`: **6 environmental | 20 genuine-owned | 0 genuine-UNOWNED**
  ("Nothing to file"). Domain half 77/77 + 1 skipped in both.

THE UNOWNED RED IN RUN A WAS MINE, AND ONLY THE FULL SUITE CAUGHT IT — that is the row's point. 44 targeted
tests (9 new + 6 new + 29 pre-existing across every file touching the changed surfaces) were GREEN over the
defect. The change had moved `sweep()`'s ownership call from `_owned_agents()` to a new combined
`_owned_claims()`, which silently BYPASSED the `_owned_agents` seam that callers and tests substitute: the
carveout test's monkeypatch stopped taking effect and the sweep ran a real claim read instead of the injected
ownership. Chunks 00/01 were **count-for-count identical** across the two runs (4328/20, 4260/22) and chunk 02
moved exactly +5 passed / −5 failed, so the delta is attributable to the fix alone and to nothing else. Lesson
worth copying: when a refactor introduces a new entry point to an existing resolver, the old function is a SEAM
— check what substitutes it before routing production past it.

**NEW, AND NOT IN ITEM 6: THE PINNED-WORKTREE PROTOCOL ITSELF MANUFACTURES INVISIBLE-HALF FAILURES.** Item 6
prescribes copying `local-paths.conf` into the worktree and stops there. Run A's invisible half reported THREE
`^FAIL` lines; two of them — `test_wm_advisory_lock.py` (rc=2, "working memory not initialized") and
`test-wm-prune-cadence-protection.sh` (rc=1, `cp: cannot stat .../working-memory.yaml`) — are pure worktree
artifacts: `agents/<agent>/session/working-memory.yaml` is gitignored, so a fresh worktree has none. Both
reproduce identically at `0af373baf~1`, and both DISAPPEARED in Run B after one extra
`cp agents/<agent>/session/working-memory.yaml` into the worktree — a positive control, not an inference. So
copy the agent session state alongside the conf, or the invisible half reports two phantom reds on every
worktree-pinned run, which trains readers to discount the one half that has no VERDICT line to protect it.
Run B's invisible half was then a single `FAIL(rc=1) test_capability_gate_narrative.py` — the known red already
established PRE-EXISTING and TRACKED (g-115-7335 / g-115-7346) in the cc-07 / `ac534c3ff` row above.

**2026-08-30 (alpha, `hostname` cc-14, `uname -r` 6.8.0-137-generic, local-backend box, TWO pinned-worktree runs
bracketing the g-115-8357 + g-115-8360 gate fixes, default rung → 4 chunks of ~315 files):**

- **Run A @ `7c7fe4272`** (pre-fix): `TOTAL: 18804 passed, 45 failed, 12 errors` / `VERDICT: GENUINE`, spread
  20 / 17 / 3+12err / 5. Invisible half **115/116** — the one FAIL the known pre-existing
  `test_capability_gate_narrative.py` (g-115-7335 / g-115-7346). Domain half **76/77 + 1 skipped** — the one
  FAILED `test_alert_dedup_pii_shape.py::test_editable_world_stores_carry_the_operator_address_in_shape_only`.
- **Run B @ `620d85d2c`** (the fixes: domain-suite-gate refusal-text + `blocking_units` ledger fields,
  capability-gate `land` → `_GENERIC_NAME_PARTS`, census stray-NAMING): `TOTAL: 18810 passed, 44 failed,
  12 errors` / `VERDICT: GENUINE`, spread 20 / 16 / 3+12err / 5. Chunk-diff by failing SET: 00/02/03
  identical, chunk 01 −1. Zero new failures. Invisible half **116/116** — the narrative-gate red went GREEN at
  the fix commit (its subject is the capability gate this change touched). Domain half **77/77 + 1 skipped** —
  the pii-shape red also green (not touched by the change; treat as flaky/environmental until it recurs).

**RUN B READ AS DEAD MID-RUN AND WAS NOT — item 9's mtime rule, measured from the failing side.** At diagnosis
time the harness task had completed with EMPTY output and no `EXIT=` line, the log sat at 168 lines with no
runner footer, and `grep -c "^FAIL"` returned 0 — which nearly read as a clean run and was actually ABSENCE OF
EXECUTION (caught by positive control: Run A's log carries 2 `^FAIL` lines at the same phase). A kill was issued,
but the runner's detached children survived it and went on to complete BOTH post-chunk halves; the footer landed
~35 min later and the log grew 168 → 310 lines. Two lessons, one per direction: (a) half-level greens mean
nothing until the runner's own footer is present (guard-5599); (b) a dead-looking task + verdict-less log is
STILL-RUNNING until the log mtime goes stale — re-read the log LATER before recording a run as killed, or you
under-report a run that finished on its own (this row's first draft would have said "domain half never ran").
Box context worth carrying: cc-14 has 4 GB RAM with a ~2 GB resident claude process; a solo invisible-half
re-run during the confusion independently reported 116/116.

**2026-08-31→09-01 (alpha, `hostname` DESKTOP-O91DLK2, Windows 10 MSYS, own-cloud box, g-358-36 closure — three
attempts, two new Windows lessons):**

- **Run 1 @ main repo** (4 chunks): `TOTAL: 19442 passed, 89 failed, 12 errors` / `VERDICT: INVALID (tree-moved)`
  — bravo's live session on the SAME box committed + merged mid-run. Item 6's busy-box warning applies to a
  second agent sharing the clone, not just your own pushes.
- **Run 2 @ pinned worktree d75935d92** (default 4 chunks): chunk 00 DIED AT SPAWN —
  `[WinError 206] The filename or extension is too long` from `subprocess.run` composing 322 file paths as argv.
  **NEW LESSON: the worktree remedy interacts with guard-5634 on Windows** — a worktree under
  `%LOCALAPPDATA%\Temp\<name>` lengthens every one of the ~322 per-chunk paths ~16 chars and pushes the composed
  command line over the ~32,767 CreateProcess cap. Remedy that worked: MORE chunks (161 files/chunk), not a
  shorter path.
- **Run 3 @ same worktree, `--chunks 8`**: `TOTAL: 19443 passed, 85 failed, 12 errors` / `VERDICT: GENUINE`,
  spread 1+12err / 10 / 7 / 12 / 4 / 5 / 12 / 34. Every named FAILED is a WORLD domain test
  (`$WORLD_PATH/scripts/tests`) — **env-degraded by the worktree itself**: `.env.local` is gitignored so the
  worktree has none, and the email/flywheel/ohs/usage-liveness/launch-env-key families need it. The `12 errors`
  in chunk 00 match the standing cc-14 signature above (both its runs: `3+12err`). Zero failures in the
  g-358-36 blast radius (update_goal / aspirations_write / iteration-close / cascade) across all three
  executions. mind_api arm run separately in the MAIN repo (env present): 1,377 passed / 9 failed, all nine
  pre-existing Windows wrapper-subprocess classes (5 = claim wrapper's scorer-verdict-gate invoked via a
  POSIX-style path Windows Python reads as `C:\c\...` — g-115-3376-adjacent; 4 = wm-write/utilization/
  store-author/history-cas wrapper files untouched by the change).

**2026-09-01 (alpha, `hostname` DESKTOP-O91DLK2, MINGW64_NT-10.0-19045, own-cloud box, g-358-41 closure — pinned
worktree `/tmp/wt-g35841` @ d5c2ecdcb3, `--chunks 8`, conf + daemon.port both copied per item 6):**

- `TOTAL: 19538 passed, 88 failed, 24 errors` / `VERDICT: GENUINE`, spread 1 / 11 / 8 / 12 / 6+12err / 4 / …
  across all 8 chunks (no confinement). Failing-file set matches the 08-31 run-3 profile: same standing
  families (iteration_close_quality_flag_carry 9, cross_world_target_resolution 6, goal_selector_silent_empty 5,
  stop_hook_gate_integration 5), domain half again env-degraded by the worktree (`.env.local` gitignored →
  alert_sweep/email/product_pr_flow/ddb/efs families red exactly as 08-31). Errors 24 vs 12: +12 =
  test_backpressure_owner_mandated (chunk 04), pre-existing family.
- **Zero failures in the g-358-41 blast radius**: test_merge_noop_identical (9/9 in-suite),
  test_owncloud_backend, test_owncloud_codec_backend, test_owncloud_sync*, test_tree_node_md_merge all green.
  Two merge-ADJACENT reds, both pre-owned: test_owncloud_atomic_materialize (1) → g-115-5583 (this box's
  tracked red set; WinError 5 sharing violation in `os.replace` under a concurrent reader — production
  `_atomic_write_local` has no Windows retry; path untouched by the change), test_iteration_push CRLF case →
  g-115-7311 (Windows-only).

**2026-09-01→09-02 (alpha, `hostname` DESKTOP-O91DLK2, `uname -r` 3.4.10-87d57229.x86_64 (MSYS/Git Bash, Windows 10
19045), own-cloud box, g-357-51 + g-357-31 closure — recovery-gate hardening, 35 framework files).** Run 1 from the
live checkout: `VERDICT: INVALID (tree-moved)` — a SECOND live session (bravo's session-close flush) commits into
this checkout, so the tree cannot be held still; voided, not read. Run 2 in a **detached worktree pinned at
e7a350e536** (private-index pin commit of the change) with `agents/alpha/local-paths.conf` +
`mind_api/state/daemon.port` copied in, `STORAGE_BACKEND=local`: the default chunking died at launch with
WinError 206 (command line too long — the Windows argv ceiling, not the suite); `--chunks 8` launched.
`VERDICT: GENUINE` · `TOTAL: 19717 passed, 91 failed, 24 errors` · invisible half 115/122 · domain half 63/80
(world scripts; `.env.local`-gated families as on 08-31). Failures spread across all 8 chunks (no confinement).
`--triage`: **3 environmental | 30 genuine-owned | 4 genuine-UNOWNED** → the four filed as **g-115-8624**
(goal_selector_silent_empty_guard `/tmp` capture path, world_script_crlf_check fixture runs clean on git-bash,
post_status_stamps_are_non_fatal do_verify anchor drift, full_suite_recommender mutex release). **Zero reds in the
change's blast radius**: every red in the 41 failing files either reproduces on a pristine pre-change worktree
(quality_flag_carry 9, post_status_stamps 2, iteration_push, domain_suite_gate 3, stop_hook_gate_integration 5,
worker_closure_evidence 21, wrapper_retire 4), is Windows/env (exec bits, path forms, CRLF, /tmp, suite mutex), or
was daemon-down: 17 daemon-class reds traced to a daemon SPAWN STORM my own solo re-runs of wrapper-heavy files
caused (00:32–00:34) — all green after a daemon restart, and `daemon-orphan-sweep.sh` later found and reaped 2
orphan daemon processes from that window. Lesson: on this box run solo re-runs through the pinned worktree with
`daemon.port` copied in, never bare from the live checkout, or the re-run itself manufactures the reds it is
meant to triage.
