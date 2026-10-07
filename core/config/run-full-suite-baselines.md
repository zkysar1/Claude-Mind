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

### 2026-09-03T03:33 — alpha, `hostname` cc-14, `uname -r` 6.8.0-138-generic, STORAGE_BACKEND=local pin, live fleet (5 agent dirs), 4 chunks (runner default), for the g-357-89 deep-code closure (65b9b516f)
`TOTAL: 19969 passed, 29 failed, 0 errors` / `VERDICT: GENUINE`; invisible-suites 125/125; domain-tests 81/81 (1 self-skipped).
Distribution chunk 00: 5 · 01: 17 · 02: 4 · 03: 3 (spread, not tail-confined). `--triage`: 17 failing files;
**1 environmental** (`test_learning_routing_world_scope` — solo 3/3 green), **every genuine red OWNED** — the
2026-09-02 unowned set now sits under **g-115-8698** (triage goal), the rest under g-115-6805 / g-115-7127 /
g-115-4130 / g-115-8624 / g-115-3195 / g-115-3803 / g-115-4626 / g-115-6350; **0 genuine-UNOWNED, nothing to
file.** Zero reds in the change's blast radius (no failure in the interruptible-sleep, idle-tick, cycle-cache or
harness-capabilities tests). HEAD did not move during the run (launched at 65b9b516f, verified before reading).

### 2026-09-03T05:10 — alpha, `hostname` cc-14, `uname -r` 6.8.0-138-generic, STORAGE_BACKEND=local pin, live fleet, runner-default chunking, for the g-357-87 deep-code closure (6f2b656c2)
`TOTAL: 20031 passed, 29 failed, 0 errors` / `VERDICT: GENUINE`; invisible-suites 125/125; domain half 2325 passed / 0 failed
(8/8 chunks). `--triage`: **1 environmental | 16 genuine-owned | 0 genuine-UNOWNED** — owners g-115-8698 (the 2026-09-02
unowned set), g-115-7127, g-115-8624; **nothing to file.** Same 29-red set as the 03:33 row above, +62 passed (the six new
supply-gate tests and the rest from chunk-boundary drift). Zero reds in the change's blast radius (no failure in
`test_aspiration_supply_gate`, `test_runtime_aspiration_supply_gate`, `test_runtime_aspirations_add`). HEAD did not
move during the run (launched at 6f2b656c2, verified `6f2b656c2` before reading).

### 2026-09-03T06:05 — alpha, `hostname` cc-14, `uname -r` 6.8.0-138-generic, STORAGE_BACKEND=local pin, live fleet, runner-default chunking (4), for the g-357-86 deep-code closure (2ba6bc3b7)
`TOTAL: 20083 passed, 34 failed, 0 errors` / `VERDICT: GENUINE`; invisible-suites 125/125; domain half 2325 passed / 0 failed
(8/8 chunks). `--triage`: **2 environmental | 16 genuine-owned | 0 genuine-UNOWNED** — `test_embedding_index_freshness.py`
solo 14/14 → environmental; the owned set is the same g-115-8698 / g-115-7127 / g-115-8624 population as the 05:10 row,
+52 passed (the closure-gate tests: `test_aspiration_supply_close_gate`, `test_runtime_aspiration_supply_close_gate`,
`test_wrapper_aspirations_complete_needle`). Zero reds in the change's blast radius. HEAD did not move during the run
(launched at 2ba6bc3b7, no commit or pull until the VERDICT was read).

### 2026-09-06T13:58 — alpha WORKER Body, `hostname` cc-08, `uname -r` 6.8.0-138-generic, STORAGE_BACKEND=local pin, live fleet, LIVE DAEMON, chunk rung 16, for the g-358-62 outcome-5 gate (HEAD 2573504acd)
`TOTAL: 21672 passed, 27 failed, 0 errors` / `VERDICT: GENUINE`; invisible-suites 128/128 files, 0 quarantined; domain half
87/87 units, 1 skipped, 2440 passed / 0 failed (8/8 chunks); **zero `^FAIL` lines in either non-chunked half.** `--triage`:
**0 environmental | 16 genuine-owned | 0 genuine-UNOWNED** — *"Nothing to file: every genuine red already has an owning
goal"*; every file reported `recent commits (7d): none`. Wall clock ~49 min (13:58:22 → 14:47). Largest `passed` total
recorded in this ledger to date (21,672 vs the 20,083 of 09-03), consistent with the `mind_api/tests` fold plus corpus growth
— note item 5's warning that the TOTAL is not a cross-run comparison metric, so judge by the failing FILE SET, which is the
same g-115-8698 / g-115-4336 / g-115-7218 population as the 09-03 rows.

Two things worth carrying from this run. **(a) The distribution was SPREAD, not chunk-confined** — 12 of 16 chunks carried at
least one failure (06 dominant at 10, 09 at 4, chunks 10/11/12 clean between them), which is the cc-08 2026-08-16 shape rather
than the chunk-09-confined signature; the chunk-confinement tell would have UNDER-fired here, and `--triage`'s per-file solo
re-runs are what carried the call instead (every one returned GENUINE, so the 0-environmental verdict is measured, not
inferred). **(b) `guard-4774` and `guard-5866` are in DIRECT TENSION on a live-daemon box, and both came back from the same
mandated MECHANISM query.** guard-4774 says a busy fleet box cannot gate a deep-code close from the live tree and prescribes a
pinned worktree; guard-5866 says never pin a worktree where a `mind_api` daemon is live. An agent that retrieves guard-4774
and stops does the harmful thing while believing it followed a guardrail. This run took the daemon-safe MAIN-REPO route and
the live daemon was never disturbed — no worktree, no daemon kill, 0 errors — which is the empirical resolution: on a
live-daemon box guard-5866 wins and guard-4774's remedy clause does not apply.

---

**2026-09-03 (g-115-8738 — the worktree route DISTURBS the shared daemon; bravo cc-05 control + zeta cc-02 mechanism).** A pinned-worktree full-suite run (zeta, during g-115-8638) killed the live fleet daemon 3x (03:19:52 / 03:25:03 / 03:31:26, ~chunk boundaries) and threw 12 stale-port ERRORS in chunk 02. **Pre-registered one-variable control (bravo, cc-05, 04:00):** the SAME suite in the MAIN REPO — same commit/runner/4-chunk-split/box/live-daemon — killed the daemon **0x** and threw **0 errors**. Exact contrast: worktree chunk 02 `5617p/5f/12E` vs main-repo chunk 02 `5631p/5f/0E`. The worktree is the measured differentiator for BOTH symptoms, predicted in advance not fitted after. **Kill mechanism (zeta, CODE-confirmed, not real-time-traced):** `mind-api-start.sh:303 _sweep_orphan_daemons` matches `mind_api.src` processes purely by COMMAND LINE (`pgrep -f 'python.* -m mind_api\.src'` POSIX / `CommandLine -match 'mind_api\.src'` Windows) with NO runtime-dir/cwd filter, killing any PID not in the current spawn's {child,parent} pair (empty keep-args = "kill them all"). A worktree daemon spawn therefore reaps the live fleet daemon in any directory — so item 3's leading hypothesis is confirmed at the code path, though the kill itself was not caught in a live trace. **[CORRECTED 2026-09-10 (echo, cc-03, g-115-9602): this attribution is FALSIFIED. `_sweep_orphan_daemons` has had ZERO executable call sites since 8ef51809b8 (2026-05-22, g-115-764) — 104 days BEFORE this entry — and the refusal is pinned green by `test_daemon_pinned_port_wedge.py`; `rt_sweep_orphan_daemons` has none either; and `mind_api/state/` is gitignored in full, so a worktree inherits no PID files for `_force_kill_tree` to use. The code read was of a function nothing calls — which is exactly how 'CODE-confirmed, not real-time-traced' fails. The MEASURED kills and the pre-registered control above are UNTOUCHED; only the attribution falls. Which process kills is UNKNOWN — capture it AT KILL TIME on the next reproduction. Behavioral rail is now guard-6394.]** **Stale-port half (measured):** the copied `daemon.port` is a one-time snapshot (guard-5702); when the live daemon recycles, the port changes and nothing refreshes the copy → probes hit an empty port → in-pytest spawn refusal (the 12 errors). **RECONCILES the alpha entry directly above** (which recommended the worktree-with-`daemon.port` route to avoid a *bare-solo-rerun* spawn storm): that remedy holds only while the copied port stays fresh — once it goes stale the worktree spawns its own daemon and the orphan sweep kills the live one. Neither non-main-repo route is safe on a live-daemon box. **REMEDY:** the daemon-safe MAIN-REPO route (`STORAGE_BACKEND=local`, chunked, `-m 'not daemon_integration'`) spawns no daemon, so it neither hijacks nor kills the live one — bravo's control IS that route. Behavioral rail: **guard-5866**. Bonus (bravo): failure COUNTS were identical across both environments (chunk 00: 5/5, 01: 14/14, 02: 5/5) — those 24 failures are environment-independent; only the 12 ERRORS were worktree artifacts, so a reader who dismissed the whole run as "worktree noise" would have discarded 24 real signals.

---

### 2026-09-08T22:57 — zeta, `hostname` cc-02, `uname -r` 6.8.0-138-generic, own-cloud, live fleet, LIVE DAEMON + LIVE REDUCER (same sid), chunk rung 4, HEAD 64ebabd346 (pre-change baseline for g-369-194)

`TOTAL: 22355 passed, 62 failed, 0 errors` / `VERDICT: GENUINE failures -- trustworthy, act on them`. `--triage`:
**1 environmental | 22 genuine-owned | 0 genuine-UNOWNED**. Wall clock **47.5 min** (22:57:53Z `chunk-00.args` →
23:45:27Z `halves.jsonl`); log dir `/tmp/ayoai-suite-run-zeta`, i.e. the env vars arrived.

**THIS ROW EXISTS FOR ONE REASON: it is the first entry that measures the HEAD-STILL WINDOW ALONGSIDE the run
duration, and that pairing — not the daemon/reducer predicate — is what decides whether a verdict is valid.**

`guard-6242` (bravo, 2026-09-07, from cc-05) states that on a box with BOTH a live `mind_api` daemon AND a live
reducer loop the full suite "CANNOT produce a valid verdict", because the reducer's own turn-end `iteration-push`
moves HEAD under the same `MIND_SID` and `run-full-suite` voids a run whose tree moved. Both halves of that
predicate held on cc-02 during this run — `daemon.port` present (19002), `running-session-id == MIND_SID` — and the
run still produced a usable, triage-confirmed GENUINE verdict.

The mechanism is not wrong; the DURATION premise is. Measured on this box the same night
(`git reflog --date=unix -40 --format='%cd'`, which is guard-6242's own `action_hint`): 40 moves over 12 h, **median
inter-move gap 668 s (11.1 min)**, p90 2686 s, max 4399 s (1.22 h), **0 of 39 gaps ≥ 3 h** — essentially bravo's cc-05
profile (median 443 s / 7.4 min, 0/39 ≥ 3 h) reproduced on a third box. What differs is that guard-6242 assumes the
run "needs 3-5h". At `--chunks 4` it needed **47.5 minutes**, and it landed inside a single **3322 s (55.4 min)**
still-window: **ZERO reflog moves strictly inside 22:57:53Z–23:45:27Z**, nearest move 11 s BEFORE the start and 457 s
AFTER the end. So the verdict was valid because the tree provably did not move — verified, not assumed, and worth
verifying before leaning on any GENUINE from a busy box.

**Operative predicate: `run duration > HEAD-still window`, not `live daemon + live reducer`.** Note this does NOT
invert the decision on cc-02 — 11.1 min median < 47.5 min run, so guard-6242's own hint still says DO NOT LAUNCH here,
and a fresh launch is a gamble that happened to pay off once. It makes the call a COMPARISON rather than a foregone
conclusion, and the comparison needs YOUR chunk rung: the 3-5 h figure appears to come from a 16-chunk rung, and rung
4 on this corpus is ~6x faster. **Measure your own duration before quoting anyone's.**

Cost of the overstated reading, recorded because it was paid: two agents (bravo cc-05, alpha cc-04) deferred
g-369-194 to a scarce owner-committed quiet window on this predicate. Each correctly probed the predicate on their own
box; neither ran the `action_hint`. The goal closed opportunistically mid-loop in ~20 min using guard-6242's own
prescribed alternative — a scoped set over every consumer of the changed module (201 tests, rc=0), with
`git rev-parse HEAD` captured either side and unchanged. guard-6242's `action_hint` now carries this correction
(its `rule` is immutable by design); see also rb-10448 and guard-6327.

### 2026-09-09T02:23 — alpha (assistant-mode chat, loop IDLE), `hostname` cc-07, `uname -r` 6.8.0-138-generic, STORAGE_BACKEND=local pin, live fleet (5 agent dirs), LIVE DAEMON on the box, MAIN REPO, `nohup env MIND_AGENT=alpha MIND_SID=<sid> …` launch (log dir `ayoai-suite-run-alpha` — the vars arrived), runner-default 4 chunks, for the g-372-01 closure (commit 2459d47107)

`TOTAL: 22365 passed, 55 failed, 0 errors` / `VERDICT: GENUINE`. Per chunk 00–03: 7 / 14 / 31 / 3 failed, ~7.5 min each. Invisible half 131/132 (the one red, `test_aspirations_update_goal_source_value.sh`, re-ran solo 6/6 green); domain half 89/89, 1 skipped.
`--triage` (run after committing a leftover store append, tree clean): **1 environmental | 19 genuine-owned | 0 genuine-UNOWNED — nothing to file.** Every red is pre-owned (the g-115-8698 batch, g-115-6760 for the 22 `test_promote.py`, g-115-9296, g-115-7127, g-115-6967, g-115-8300); the environmental one was `test_learning_routing_world_scope.py` (3/3 solo). Two readings worth keeping: (1) the chunked run printed `[promote] ERROR: working tree is dirty` 21× while `agents/alpha/aspirations.jsonl` was modified, yet `test_promote.py` still failed 22 solo on the CLEAN tree — the dirty tree was incidental, not the cause; do not stop at the first cause a log names. (2) Zero failures in the owncloud / liveness / lease / storage-backend families, which is what this run existed to establish. Wall clock: chunked half 02:23 → ~03:03, invisible + domain halves to 03:11, triage 03:11 → 03:14.

## Chunk-09 GENUINE-but-false signature (folded from `.claude/rules/run-full-suite-after-deep-code.md` 2026-09-10, g-115-9602)

Four dated per-box reproduction blocks of ONE incident, moved here verbatim when its root cause closed (g-115-5651 — the memoized `_ACTIVE_BACKEND` poisoning; reset fixture landed in both conftests). The rule keeps the METHOD; this ledger keeps the EVIDENCE. A FRESH occurrence is a REGRESSION: re-run solo and file a NEW goal.

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

### 2026-09-10T14:48 — bravo (assistant-mode chat, loop IDLE, NO reducer and NO worker Body on the box), `hostname` cc-13, `uname -r` 6.8.0-139-generic, 20 cores / 4 GB RAM, STORAGE_BACKEND=local pin, LIVE DAEMON, MAIN REPO, `nohup env MIND_AGENT=bravo MIND_SID=<sid> …` launch (log dir `ayoai-suite-run-bravo` — the vars arrived), runner-default 4 chunks, HEAD f5965d6625 (= origin/main: 9746b4d1c9 cold-snapshot storage-backend change for g-372-13, echo's fast tier d2a56feb0a / 044a959ddd / 585c2af134 landed via g-115-9620, and 4238c46598 for g-115-8820)

`TOTAL: 22456 passed, 59 failed, 0 errors` / `VERDICT: GENUINE failures -- trustworthy, act on them`. Per chunk 00–03: 8 / 14 / 32 / 5 failed. Wall clock 14:48:39Z → 15:23:37Z = **35 min chunked** (chunks 7.5 / 4.3 / 5.3 / **17.8** min — chunk 03 carries mind_api/tests, which sorts last), invisible half 132/132 (~5 min), domain half 96/96 + 1 skipped, **47 min total**, runner rc=1 (framework half). Tree provably still: this session was the only committer on the box and made no commit between launch and exit; the only dirt at exit was `agents/alpha/aspirations.jsonl` (own-cloud mirror churn at 15:32Z, restored with `git checkout --`). No HEAD-still window to compare against — there is no loop on this box, which is the point of running it here.

`--triage` (2.5 min): **1 environmental | 22 genuine-owned | 0 genuine-UNOWNED — read the next paragraph before believing that zero.** Environmental: `test_learning_routing_world_scope.py` (3/3 solo; the same file cc-07 09-09 classified the same way). Owned: g-115-6760 `test_promote.py` 22, g-115-9181 `test_iteration_close_quality_flag_carry.py` 9, and the g-115-8698 / g-115-9296 / g-115-6805 / g-115-8624 / g-115-8004 / g-115-6967 / g-115-8300 / g-115-8407 / g-115-8544 set.

TWO REDS THE OWNERSHIP PROBE MIS-CLASSIFIED AS OWNED — the g-115-9425 defect class in the OTHER direction, over-matching on a subsystem word ('release', 'promotion') and counting 79 WEAK hits as ownership:
1. `test_release.py` (3, the Section-8 ledger tests) + `test_release_atomicity.py` (2, same fixture) = 5 reds with 79 weak owners and no real one. Root cause measured with `--basetemp`: the fixture repo's only dirt was `?? core/scripts/.platform-memo.sh` — `_paths.sh` writes its platform memo beside itself on first source since fe4df6c6fe (2026-09-06), the fixture's `.gitignore` predates it, and release.sh Step 1 refuses "working tree is dirty". Red on every Linux run since 09-06 and invisible under weak ownership (cc-07 09-09's "0 unowned" included them). FIXED in the fixture — one `.gitignore` line, commit 2a5c43786c: both files green solo, 0 FAILED.
2. `test_reducer_promotion.py::test_shipped_config_is_default_off` (1): the shipped `reducer_promotion` block has listed 9 eligible machines since 2fe2da761a0 (2026-09-06 owner-proxy decision) while the test pins `()`; `enabled: false` and `fence_verified_at: null` still hold, so the kill switch is off and only the pin is stale. OWNED by g-306-294 — alpha recorded exactly this there on 2026-09-07 ([appended:alpha-occ182-pin-stale-20260907]); the probe missed it because that goal never names the test file.

Honest split: **1 environmental | 23 genuine-owned (22 by probe + g-306-294 by hand) | 5 fixture-defect, fixed | 0 unowned**. No failure traces to cold_snapshot.py, owncloud_backend.py, cold-snapshot-tick.py, run-scoped-suite.* or the g-115-8820 evolution files (test_cold_snapshot*, test_owncloud_endpoint_override, test_run_scoped_suite all green in-suite; no test_evolution* in the failed set). DISCHARGES: the g-372-13 full-suite obligation (storage-backend change = FULL-REQUIRED under the two-tier rule), the g-115-9620 obligation for the fast-tier commits, and g-115-8820.

For the owner's "four hours" question: this is the third Linux rung-4 run in three days inside 36–48 min on ~4 GB boxes (cc-02 47.5, cc-07 ~30 + halves, cc-13 47 total). The 3–5 h figure is the Windows rung-16 form. Chunk 03's 17.8 min is mind_api/tests (daemon HTTP), not file count (360 files, 5724 passed); the other three chunks average 5.7 min. Concurrent chunks (g-115-9624) would bound the chunked half by chunk 03 alone, ~18 min.

### 2026-09-11T03:21 — bravo (assistant-mode chat, loop IDLE, NO reducer and NO worker Body on the box), `hostname` cc-13, `uname -r` 6.8.0-139-generic, 20 cores / 4 GB RAM, STORAGE_BACKEND=local pin, LIVE DAEMON, MAIN REPO, `nohup env MIND_AGENT=bravo MIND_SID=<sid> …` launch (log dir `ayoai-suite-run-bravo` — the vars arrived), runner-default 4 chunks, HEAD f5777a8348 (= origin/main at launch; carries the night's g-372 commits 0910903ddf, 3c93119899, e1b464b8f0, 5ad1d8a950)

`TOTAL: 22595 passed, 53 failed, 0 errors` / `VERDICT: GENUINE failures -- trustworthy, act on them`. Per chunk 00–03: 7 / 14 / 27 / 5 failed. Wall clock 03:21:42Z → 04:09:29Z = **47.8 min total** (chunk ends 03:29:00 / 03:33:27 / 03:39:38 / 03:57:34Z; invisible half 132/132 files, 0 quarantined; domain half 97/97 units + 1 skipped), runner rc=1 (framework half). Tree provably still: this session was the only committer on the box, its last commit (5ad1d8a950, 03:13Z) predates the launch, and no commit or merge happened between launch and exit; the only dirt at exit was the two agent work-queue files on this box (alpha's and bravo's — own-cloud mirror churn). Launched while the scoped tier over the night's 15 framework files read `PASS_WITH_GAPS` (7 shell/probe scripts with no test mapping) — the FULL-REQUIRED branch of the two-tier rule.

`--triage` (04:10 → 04:11:54Z): **1 environmental | 19 genuine-owned | 0 genuine-UNOWNED.** Environmental: `test_learning_routing_world_scope.py` (3/3 solo — the third run in a row to classify it so: cc-07 09-09, cc-13 09-10, cc-13 09-11). Owned: g-115-6760 `test_promote.py` 22, g-115-9181 `test_iteration_close_quality_flag_carry.py` 9, g-115-8388 `test_cygpath_wrapper_pattern.py` 2, g-115-8099 `test_completed_not_closed_slate.py` 2, g-115-8624 `test_post_status_stamps_are_non_fatal.py` 2, and one each for `test_capability_gate_table_token_noise`, `test_completion_digest`, `test_dependency_supersession_resolution`, `test_failopen_filing_branches`, `test_goal_selector_class_balance_directive_ordering`, `test_inferred_unknown_autoflag`, `test_meta_write_class_conflict_retry`, `test_pending_phase_6_spark_sentinel`, `test_recovery_yank`, `test_reducer_promotion` (g-306-294, the stale eligible-machines pin), `test_utilization_seam_consumers`, `test_weakness_signals`, `mind_api/tests/test_runtime_update_goal_cascade` (2), `mind_api/tests/test_store_author_stamp` (1). The 09-10 run's five `test_release*` fixture reds are gone (fixed that day); 59 → 53.

None of the night's files is in the failed set: `_daemon_env_scrub.sh`, `mind_api/src/__main__.py`, `mind_api/src/endpoints/health.py`, `owncloud-store-enumerate.py`, `minio-standup.sh`, `cold-snapshot-target-provision.sh`, `owncloud-endpoint-flip.sh`, `owncloud-endpoint-probe.py`, `owncloud-fence-contention-probe.py`, `object-store-conformance.py` — and their tests (`test_daemon_start_env_scrub`, `test_runtime_spawn_env_scrub`, `test_env_local_loader`, `test_runtime_health`, `test_owncloud_store_enumerate_diff`) appear in no chunk's failed lines. DISCHARGES: the g-372-07/04/16 closures' daemon-loader commit (0910903ddf, FULL-REQUIRED) and the g-372-06 copier commits (fast tier PASS with no gaps, 8/8 in-suite).

### 2026-09-11T13:25 + 14:06 — alpha WORKER Body (autonomous loop, g-115-9624), `hostname` cc-09, `uname -r` 6.8.0-139-generic, 20 cores / 4 GB RAM + 4 GB swap, STORAGE_BACKEND=local pin, LIVE DAEMON, MAIN REPO, NO reducer and no second Body on the box, HEAD d24a8c0f07 (unchanged across BOTH runs — verified by `git rev-parse` before each and after both, and by the runner's own tree-move check passing), 4 chunks, 1453 files — **THE SERIAL-vs-CONCURRENT PAIR. One box, one tree, one day, one HEAD.**

**THE HEADLINE: concurrency is CORRECT and gives 1.98x — and 1.98x is the CEILING, because the chunks are IMBALANCED. Chunk count is not the lever; chunk BALANCE is.**

| | serial | concurrent | delta |
|---|---|---|---|
| wall clock (chunked half) | 13:25:41→14:04:53 = **2352s / 39.2 min** | 14:06:51→14:26:39 = **1188.1s / 19.8 min** | **1.98x** |
| chunk 00 (364 files) | 475s | 526.0s | +10.7% |
| chunk 01 (363) | 350s | 368.0s | +5.1% |
| chunk 02 (363) | 372s | 408.0s | +9.7% |
| chunk 03 (363) | **1155s** | **1188.1s** | +2.9% |
| TOTAL | 22439 passed, 56 failed, 0 errors | 22439 passed, 56 failed, 0 errors | **identical** |
| VERDICT | GENUINE | GENUINE | **identical** |
| peak RSS | 277 / 352 / 409 / **1084** MB per chunk | **1131 MB** aggregate | — |
| min avail / peak swap | 1325 MB / 153 MB | **1144 MB / 1324 MB** | swap +1171 MB |

WHY 1.98x AND NOT 4x — READ THIS BEFORE DESIGNING ANY `--concurrent N`. Chunk 03 alone is **1155s of the 2352s serial total (49%)**: it carries `mind_api/tests` (135 of its 363 files), whose conftest spawns a fresh in-process daemon on a free port PER TEST. Concurrency can only compress the OTHER three chunks into chunk 03's shadow, so the floor is chunk 03 alone and the speedup ceiling is `total / max_chunk` = 2352/1155 = 2.04x. Measured 1.98x, i.e. the model is right to within 3%. **The prize is not concurrency, it is REBALANCING**: at four equal ~588s chunks the same concurrent run would finish in ~10 min (4x). Concurrency on TODAY's split buys 19.4 min; balancing first and then running concurrently buys ~29 min. Anyone implementing `--concurrent N` without first splitting `mind_api/tests` across chunks is buying the smaller half of the win.

FAILED-SET DIFF: **EMPTY.** Both runs produced 56 `FAILED` lines, byte-identical (5898 B each) after stripping the ` - <assertion text>` tails, across the same 22 files with the same per-file counts (`test_promote` 22, `test_iteration_close_quality_flag_carry` 9, ...). The only textual differences anywhere in the 4 log pairs are pytest's own duration line and heap addresses (`0x75856e24ba60` vs `0x7abdf18e4ea0`) — i.e. two genuinely distinct processes computing the same answer. Positive control that these are two real runs and not one directory read twice: log mtimes 13:33–14:04 vs 14:12–14:26, and pytest's summary durations 469.14/349.00/371.74/1154.83s vs 524.15/356.56/406.90/1185.64s.

NEGATIVE CONTROL — **PASSED IN BOTH MODES.** A temporary `core/scripts/tests/test_zz_seeded_negative_control.py` was seeded BEFORE both runs (guard-1096 forbids adding test files mid-run; a control present in only one run measures the tree, not the concurrency) and deleted after. It carried **TWO** cases, one expected-PASS and one expected-FAIL, deliberately: with one failing case only, "dropped the file" and "ran it and reported nothing" are indistinguishable, whereas with a pass/fail pair a dropped file moves NEITHER count. Result: the `FAILED ...::test_zz_seeded_control_fails` line appears exactly once in BOTH runs, in **chunk 03 only** (0 mentions in chunks 00/01/02 in both), and the passed counts match exactly. So the concurrent run is not "fast and green with the seed unreported". Blast radius of the seed was measured at zero beforehand (174 tests across `test_release`, `test_release_atomicity`, `test_provenance_manifest`, `test_fixture_tell`, `test_cross_agent_glob_audit`, `test_conftest_world_meta_cleared`, `test_run_full_suite` all green with it present) — worth doing, because an UNTRACKED file in this repo has previously turned `test_release*` red by dirtying a fixture-repo copy (the `?? core/scripts/.platform-memo.sh` incident, 09-10 row above).

MEMORY IS THE REAL CEILING, AND IT SHOWS UP AS **SWAP, NOT OOM**. Zero OOM kills, zero `MemoryError`, zero `Cannot allocate memory`. But swap went **152 MB → 1324 MB** (+1171 MB) while `avail` bottomed at 1144 MB. That swapping is the most likely cause of the +5–11% per-chunk slowdowns. Note the aggregate pytest RSS peaked at only 1131 MB — well UNDER the naive "sum of per-chunk peaks" estimate of 2069 MB — because the three short chunks exit before chunk 03 reaches its 1084 MB peak. **So sum-of-peaks over-estimates concurrent memory on an IMBALANCED split and would UNDER-estimate it on a balanced one**: rebalancing removes the staggering that is currently protecting this box, and four equal chunks all peaking together is the case nobody has measured. On a 4 GB box, balance the chunks and you may buy the 4x with an OOM.

COLLISIONS: `Address already in use` 0, `FileExistsError` 0, `OSError` 0, `ResourceWarning` 0, `Timeout (` 0 — in both runs. `REFUSED` appears 2x in BOTH, so it is pre-existing and not concurrency-induced. **The zero on ports is STRUCTURAL, NOT EVIDENCE THAT CONCURRENCY IS PORT-SAFE**: only chunk 03 holds `mind_api/tests`, so exactly ONE concurrent process ever binds a port. At a higher chunk rung — or after the rebalancing recommended above — those 135 files split ACROSS chunks and multiple concurrent processes would bind free ports simultaneously (bind :0, close, reuse is a TOCTOU race). That case is UNMEASURED.

CLASSIFIER (the goal's item 4) — **THE GOAL'S PREMISE WAS WRONG, AND THE REAL ANSWER IS WORSE.** The goal states `classify()` reads "failures confined to one chunk with later chunks clean" as CONTENTION, "a heuristic that assumes chunks are sequential in time". It does not: `classify()` implements no chunk-confinement rule at all. That heuristic is human-facing prose in `_full_suite_imperative.py` item 2, applied by a READER. What `classify()` actually does positionally is `_positional_profile`, which buckets pytest progress percentages per chunk — and it returned **byte-identical values in both runs** (`early_rate=0.0529%`, `late_rate=0.1672%`, `n_early=7560`, `n_late=2392`, both under `LATE_FLOOR=0.02`), with `reasons=[]` and `GENUINE` in both. So concurrency cannot make it misfire; it is concurrency-INVARIANT by construction, because each chunk's 0→100% axis is internal to that chunk and the blob is joined in sorted chunk-INDEX order, never completion order.
**The finding that matters is the inverse one.** Per g-115-4336 (measured zeta cc-02: 411 of 434 failures in the last 2 of 16 chunks, `VERDICT: GENUINE`, `reasons=[]`) and guard-5270 (the distributed mirror), the classifier is ALREADY blind to CROSS-CHUNK contention. SERIAL chunks cannot contend with each other — they run one at a time, so cross-chunk contention is structurally impossible. CONCURRENT chunks can, and by construction do. **Concurrency therefore manufactures the exact failure mode the classifier cannot see, while printing GENUINE, which the rule text tells readers to trust above the numbers.** It did not bite in THIS run (identical failed sets prove contention produced zero extra failures on a quiet box with staggered memory peaks) — but that is one measurement under the mildest conditions, and the conditions that would make it bite are exactly the ones rebalancing creates. **g-115-4336 / g-115-4130 are a PREREQUISITE for shipping `--concurrent N`, not a parallel nice-to-have.**

METHOD NOTE for whoever re-runs this: the concurrent side used a throwaway harness (`subprocess.Popen` x4 on the serial run's OWN `chunk-NN.args` file lists, byte-identical argv and env to `run-full-suite.py`'s chunk loop at :2048-2091 / :1922-1924), and the verdict was computed by importing `run-full-suite.py` and calling its real `classify()` / `failing_files()` / `_parse_counts()` rather than reimplementing them — otherwise "does the classifier misfire?" would have been answered about a copy. Reusing the serial run's own `.args` files is what removes "different file lists" as an explanation for any difference. Note `--out <dir>` is the sanctioned way past `CONCURRENT_RUN_REFUSAL` (the refusal text names it); no override flag was needed or used. Also: this pytest does NOT expand `@argfile` (`fromfile_prefix_chars` unset), so paths ride on argv as repo-relative — the harness had to take the runner's fallback branch, not the `@argfile` one.

### 2026-09-12T18:3x — alpha REDUCER, `hostname` cc-04, `uname -r` 6.8.0-139-generic, STORAGE_BACKEND=own-cloud box with the mandatory `STORAGE_BACKEND=local` pin, LIVE DAEMON (pid 460943), live fleet, **FAST TIER** (`run-scoped-suite.sh`), for the g-306-284 deep recurring close (carrier-ref drain; pre-merge base `1041e48649`)

**FAST-TIER ROW, not a full run** — recorded here because the two-tier rule says record WHICH tier ran and why, and because the fast tier's *selectivity* is the input to its own sufficiency criterion (c) and has almost no measured values.

`VERDICT: PASS` over a NON-EMPTY selection: **7 test files selected, all green, 3.9s, `selected_share_pct` 0.48% of a ~1,450-file corpus**, `unmapped_files` EMPTY. All four sufficiency conditions held — (a) PASS on a non-empty selection, (b) no unmapped file, (c) 0.48% is small, (d) the change touched none of `pytest.ini`, a `conftest.py`, `_paths.{py,sh}`, the storage backend, or the runner scripts themselves. So the fast tier discharged this closure and no full run was owed.

Operator note on the INVOCATION, which is the part that cost time: the first call returned `INCONCLUSIVE — no changed source file found`, because the carrier merges were **already committed** when it ran and the default diff base is the working tree. `--since 1041e48649` (the pre-merge base) produced the PASS above. That is an invocation error, not a coverage gap, and the tri-state verdict behaved exactly as designed — an empty selection is not a pass, and it correctly refused to call one.

Calibration value of the 0.48%: criterion (c) says a small share is what licenses the fast tier and a hub change fanning out to a large share is the tier telling you to run the full one, but it names no number. Two share readings now exist in this ledger — 5/1442 and 22/1443 from the tier's own landing measurement (0.35% / 1.52%), and 0.48% here. Nothing yet establishes where "small" ends; do not read three points as a threshold.

### 2026-09-18T10:5x — bravo (assistant-mode chat on this box; bravo's REDUCER is live on cc-05, NOT here), `hostname` cc-13, `uname -r` 6.8.0-139-generic, 20 cores / 4 GB RAM, STORAGE_BACKEND=local pin, LIVE DAEMON, MAIN REPO, `nohup env MIND_AGENT=bravo MIND_SID=<sid> …` launch (log dir `ayoai-suite-run-bravo` — the vars arrived), runner-default 4 chunks, 1521 files, HEAD 9216e04a23 (= the v2.12.76 release 6c5f685b3c merged with github + rack main; carries the ZDS back-port 8791e74f05 — goal-field-append wrapped-marker refusal, store-field-append pipeline store, premise_supersession CLAIM_RE time units, experience-pipe/notifications-read/merge-record-audit). Chunk-00 log created ≈2026-09-18 10:53, run finished ≈2026-09-18 11:50.

`TOTAL: 23969 passed, 61 failed, 0 errors` / `VERDICT: GENUINE failures -- trustworthy, act on them`. Per chunk: 00 = 5376/4, 01 = 5663/17, 02 = 6831/34, 03 = 6099/6 (NUL count 0 in every chunk log). Invisible half `133/133 files passed, 0 quarantined`; domain half `107/107 unit(s) passed, 1 skipped`. Framework half rc=1.

`--triage` (same session, right after): **1 environmental | 23 genuine-owned | 0 genuine-UNOWNED — "Nothing to file"**. The one environmental is `test_learning_routing_world_scope.py` (3/3 green solo). The 22 reds in `test_promote.py` (chunk 02) are the standing promotion-preflight/worktree family (owners g-115-6760, g-115-6971, g-115-8740, g-115-10155) and reproduce solo 28 passed / 22 failed; `test_owncloud_integration.py` 6 reds are the no-moto class (g-115-10069); `test_iteration_close_quality_flag_carry.py` 9 reds solo 0/9 (g-115-9181). Two ownership goals were filed DURING the run from the chunk 00/01 solo re-runs, before triage: **g-115-10243** (`test_capability_gate_table_token_noise` — world-dependent: a forged skill named `zakpod1-ledger-census` matches the token-noise fixture) and **g-115-10245** (four unowned deterministic reds: `test_failopen_filing_branches`, `test_false_exhaustion_fixes_20260915`, `test_goal_selector_class_balance_directive_ordering`, `test_inferred_unknown_autoflag`). None of the 61 sits in the back-ported code: the six back-port test files ran green in the pre-commit scoped run and stay green here.

Read against the 2026-09-11 serial baseline on cc-09 (56 `FAILED` lines, 22 files): the failing-file set is the SAME family plus the two new ownership buckets above; `test_promote` 22 is unchanged. Nothing here contradicts the release: v2.12.76 was cut, promoted to Claude-Mind (PR #91, cd5adcd) and seed-published on this tree.

### 2026-09-22T06:2x–07:2x — alpha WORKER BODY (reducer live on cc-04, not here), `hostname` cc-07, `uname -r` 6.8.0-139-generic, STORAGE_BACKEND=local pin, LIVE DAEMON, MAIN REPO, 4 chunks, HEAD 79de29b7c9 held for the whole run, launched `nohup env -u BODY_WM_PATH -u BODY_ROLE MIND_AGENT=alpha MIND_SID=<sid> STORAGE_BACKEND=local bash core/scripts/run-full-suite.sh > /tmp/suite-alpha-10242.log 2>&1 < /dev/null &` (g-115-10242: the ownership-join rewrite, so the runner itself changed and the FULL run was required)

`TOTAL: 24482 passed, 41 failed, 0 errors` / `VERDICT: GENUINE failures -- trustworthy, act on them`. Per chunk: 00 = 5467/8, 01 = 5836/17, 02 = 6963/11, 03 = 6216/5 (spread, not tail-loaded). Invisible half `134/134 files passed, 0 quarantined`; domain half `108/108 unit(s) passed, 1 skipped`. Zero reds in `test_run_full_suite*`.

`--triage` (same session, same env shape): **1 environmental | 18 genuine-owned | 2 genuine-VERIFY | 1 genuine-UNOWNED** — the first run of the three-strength join (owner: only on a node-id match across title/description/progress_note/outcome_note; candidate:/VERIFY for file- or stem-only). Environmental: `test_learning_routing_world_scope.py` (3/3 green solo), same as cc-13 09-18. VERIFY, opened by hand: `test_iteration_close_quality_flag_carry.py` (9) IS owned by g-115-9181, which names the file, the count and the three parametrize ids but not the test functions (parametrize ids are stripped by `failing_tests()` — successor filed); `test_cross_agent_glob_audit.py::test_live_repo_is_clean` is NOT owned — `core/scripts/predicate.py:384` glob added by fa81948dd4 (g-353-108, 09-21) without regenerating the convention table (relayed). UNOWNED: `test_reflection_ownership_split_cli.py::test_identity_is_taken_from_the_caller_not_guessed` — fixture pins `resolved_at` 2026-09-17T10:00:00 and `_reflectable.py` turns a held record reclaimable past `stranded_hours`, so it reddened by calendar time (relayed). The other 18 files are the standing family with an exact owner each.

SIDE-EFFECT ON A WORKER BODY, g-115-6065 signature, third box: the suite advanced `last_fresh_eyes_review` (06:32:05) and `last_fresh_eyes_tree_review` (06:32:11), both 15271, in THIS BODY's WM — `env -u BODY_WM_PATH` does not stop the daemon-hop write, but the literally-passed MIND_SID is what routed it to the Body WM (repairable, restored via wm-set.sh to the reducer baseline; diff vs the pre-launch snapshot = slot_meta only) instead of the agent-wide WM. The `--claim` shape also stamped `shared_cadences.last_fresh_eyes_tree_review` = 06:32:11 / 14882 / alpha plus a 30-min inflight claim; prior value unrecoverable here (team-state history ends 2026-08-09). Snapshot the Body WM before launching and diff after — the diff is the detector.

### 2026-09-28T17:47Z — alpha (assistant-mode chat; alpha's REDUCER is live on cc-04, NOT here), `hostname` DESKTOP-O91DLK2, `uname -r` 3.4.10-87d57229.x86_64 (MSYS/Git Bash, Windows 10 19045), STORAGE_BACKEND=local pin, LIVE DAEMON, MAIN REPO, `nohup env MIND_AGENT=alpha MIND_SID=<sid> STORAGE_BACKEND=local …` launch (log dir `ayoai-suite-run-alpha` — the vars arrived and the tree lock was granted), runner-default 4 chunks, 1615 files, HEAD 6a7c900127, for the g-115-7257 / g-115-11323 deep-code closure (6fb265239b)

**VOIDED, stopped before its verdict.** Chunk 00 (404 files) = 5762 passed / 35 failed / 12 errors in 77 min, finished 19:04Z on the launch tree; chunk 01 was at 12% after 24 min, projecting 5-6 h for the chunked half alone. The tree lock (TTL 5400 s, never renewed) expired at ~19:17Z; `tree-lock.sh status` at ~19:36Z read `mine, live=false` while the wrapper was alive (g-115-8856). HEAD then moved at 19:31:11Z (6821193fdd, an encode-session Final.5 commit by a co-resident alpha assistant session: `iteration-commit.sh` never consults the lock, so a live lock would not have stopped it) and at 19:32:16Z (7cb3b29144, that session's iteration-push merge, which a live lock would have skipped). Killed at ~19:40Z; lock released. Chunk 00's reds are all standing families or outside the change: none in `test_owncloud_*`, and `test_domain_suite_gate` x4, `test_cross_world_target_resolution` x6 and the `test_backpressure_owner_mandated` errors match the 09-01/09-02 rows above.

Lesson for this box: the reflog shows 52 HEAD moves between 09-27T00:00Z and 09-28T20:00Z (31 commits, 16 origin merges, 5 other), with the longest gap on 09-28 about 4 h (01:27→05:39Z), while one chunked run takes 5-6 h here. A main-repo full suite on DESKTOP-O91DLK2 during the day will almost always void, and the worktree remedy is barred by the live daemon (guard-6394). Take the full-suite verdict for a Windows-authored change from an in-turn Linux run (~1 h), and keep this box for the Windows-only targeted tests.

### 2026-09-29T19:52Z–20:56Z — alpha WORKER BODY (reducer live on cc-04, not here), `hostname` cc-08, `uname -r` 6.8.0-142-generic, STORAGE_BACKEND=local pin, LIVE DAEMON, MAIN REPO, runner-default 4 chunks, 1627 files, HEAD 2029789115 held for the whole run (contains 6fb265239b), launched IN-TURN — foreground, auto-backgrounded by the Bash tool at 120 s and harness-tracked, never detached — as `env -u BODY_WM_PATH -u BODY_ROLE -u MIND_GOAL_ID MIND_AGENT=alpha MIND_SID=<sid> STORAGE_BACKEND=local bash core/scripts/run-full-suite.sh > /tmp/suite-alpha-11323.log 2>&1 < /dev/null` (log dir `ayoai-suite-run-alpha`, tree lock granted), for the g-115-11323 / g-115-7257 deep-code closure

`TOTAL: 26075 passed, 32 failed, 0 errors` / `VERDICT: GENUINE failures -- trustworthy, act on them`, runner rc=1. Per chunk: 00 = 5869/8, 01 = 6167/14, 02 = 7395/3, 03 = 6644/7 (spread). Invisible half `83/84 files passed, 0 quarantined` (red: `test_aspirations_update_goal_source_value.sh`); domain half `116/117 unit(s) passed, 1 skipped` (red: `test_s3_fallback_truncation_signal.sh`). **Zero reds in `test_owncloud_*`** (all 30 files ran in chunk 02) and none in the changed-code files (`test_mirror_health`, `test_mirror_wedge_goal_close`, `test_watchdog_box_scoped_signal`, all chunk 01). Wall clock ~65 min; chunked half done by ~20:45.

`--triage` (same env shape): **2 environmental | 12 genuine-owned | 2 genuine-VERIFY | 1 genuine-UNOWNED**. Environmental: `test_goal_field_append` (52/52 solo), `test_learning_routing_world_scope` (3/3 solo, as on cc-13 09-18 and cc-07 09-22). VERIFY, opened by hand: `test_iteration_close_quality_flag_carry` is named by g-115-9181; `test_post_status_stamps_are_non_fatal` is listed by g-115-8698 (the 09-02 unowned-reds triage), so it predates this change. UNOWNED: `test_board_write_durability` is caused by the FLEET ENV, not code. `.claude/settings.json:52` has set `BOARD_SEGMENTED_CHANNELS=coordination` since 306e76f9e7 (09-27, g-358-183 U5 flip), so `board.py` appends coordination posts to `coordination-2026-09-29.jsonl` while the test reads only `coordination.jsonl`. One-variable A/B: rc=1 with the var, rc=0 under `env -u BOARD_SEGMENTED_CHANNELS`. `mind_api/tests/test_runtime_board_write.py::test_post_coordination_succeeds` (owned by g-115-11500 by file only) has the same cause and the same A/B. The invisible red reproduces SOLO in the `--source world` case, where g-115-8739 describes an in-suite-only `--source agent` signature; `aspirations-update-goal.sh` changed today in c153fd9d73. The domain red is in-suite only (case M), green solo twice. No red traces to 6fb265239b / 2029789115: no red test file references a module either commit touched, and every owned red has at least one owner filed before them.

SIDE-EFFECT, g-115-6065 signature, fourth box: at 20:04:34 / 20:04:38 the suite advanced `last_fresh_eyes_review` and `last_fresh_eyes_tree_review` (13452 → 16071) in THIS Body's WM despite `env -u BODY_WM_PATH`, and stamped team-state `shared_cadences.last_fresh_eyes_tree_review` = 20:04:38 / 15677 / alpha. Body WM restored with wm-set.sh from the pre-launch snapshot (read-back diff empty apart from update counters); the shared stamp's prior value is unrecoverable (no team-state history for these dates). A THIRD write, which the WM snapshot cannot see: at 20:40:06 (chunk 03) a test rewrote THIS Body's `sessions/<sid>/iteration-checkpoint.json` to fixture goal `g-001-01`, phase `selected`. Every later verify/close checkpoint write for the real goal was then REFUSED on anchor mismatch. And `postcompact-restore.py:45` reads that file for its IN-FLIGHT GOAL banner, so a compaction would have told the Body to resume `g-001-01`. None of the five core/scripts tests that name `g-001-01` ran in chunk 03, so the writer builds the id dynamically. The file was cleared with `loop-state-save.sh clear` after the close (copy kept in session scratch). Snapshot that file as well as the WM, and run `loop-state-save.sh read` after the run. Pacing note: `EXTERNAL_WAIT=1 interruptible-sleep.sh 480` exited after ~2 min on a classic wake signal; passing `WAKE_DEBOUNCE_SECONDS=3600` held the later sleeps to their full length (stop signals still bypass it).

### 2026-09-29T23:09Z–2026-09-30T00:20Z — alpha REDUCER, `hostname` cc-04, `uname -r` 6.8.0-142-generic, STORAGE_BACKEND=local pin, LIVE DAEMON, MAIN REPO, runner-default 4 chunks, HEAD c30229aaf4 held for the whole run, DETACHED as `nohup env MIND_AGENT=alpha MIND_SID=<sid> STORAGE_BACKEND=local bash core/scripts/run-full-suite.sh > <log> 2>&1 < /dev/null &` (log dir `ayoai-suite-run-alpha`), for g-306-284 occ258 (carrier merges dd5e5c1168, b035c85b0d and e0a597846c; code 762202755a, 7054a09609 and 948f532686)

`TOTAL: 26416 passed, 34 failed, 0 errors` / `VERDICT: GENUINE failures -- trustworthy, act on them`.
- Per chunk (failed / passed): 00 = 6/5912, 01 = 14/6227, 02 = 8/7630, 03 = 6/6647 (spread).
- Chunk 03 took 24m57s, against 7-11 min for the others.
- Invisible half `83/84 files passed`; domain half `116/117 unit(s) passed` (the red is the pytest-batch unit).
- No red traces to the carriers. Every red is one of:
  - owned by a goal filed before 762202755a;
  - environmental: `test_goal_field_append` x2 green solo, and `test_learning_routing_world_scope` as on 09-18/09-22/09-29;
  - order pollution: `test_no_override_omits_flag` (the iteration-close quality-flag file, owned by g-115-8678/g-115-9181; appended to g-115-4216).
- The fleet-env pair `test_board_write_durability` + `test_runtime_board_write::test_post_coordination_succeeds` was reported UNOWNED by the 09-29 triage above. Its A/B was re-run for both (rc 1 with `BOARD_SEGMENTED_CHANNELS`, rc 0 under `env -u`) and it is now owned by g-115-11618.

SIDE-EFFECTS on the REDUCER's own state (this run was launched from the reducer shell, not a Body):
1. **Cadence stamps (g-115-6065 signature).** At 23:22:36 / 23:22:44 (chunk 01, which contains `test_fresh_eyes_record_tick_unknown_flag.py`), the agent-wide WM `last_fresh_eyes_review` and `last_fresh_eyes_tree_review` advanced to 16107, and team-state `shared_cadences.last_fresh_eyes_tree_review` + its `__inflight_claim` were restamped. A review that was DUE now reads diff=10. The reducer WM had no pre-run snapshot, so it was not restored.
2. **Checkpoint anchor.** At 00:02:42 (chunk 03), the agent-wide `iteration-checkpoint.json` was anchored to `g-001-01`. This corrects the 09-29 record's inference that "the writer builds the id dynamically": chunk 03 contains `mind_api/tests/test_wrapper_aspirations_retire_release_claim.py`, which names `g-001-01` literally, and the 09-29 search covered core/scripts tests only (writer owned by g-115-11509).
   - A compaction LANDED on that anchor. The banner ordered "resume g-001-01" with no STALE/NOTE/AMBIGUOUS line, because the world copy is archived (read-side gap: g-115-11617).
   - `MIND_GOAL_ID=g-001-01` was injected into every reducer Bash call until the anchor was re-set by hand.
   - Snapshot BOTH the WM cadence slots and `loop-state-save.sh read` before launching from a reducer shell.

### 2026-10-01T05:40Z–06:50Z — alpha REDUCER, `hostname` cc-04, `uname -r` 6.8.0-142-generic, STORAGE_BACKEND=local pin, LIVE DAEMON, MAIN REPO, runner-default 4 chunks, 1646 files, HEAD 92177a35f7 held for the whole run (the next HEAD move, a fast-forward, came at 06:51:02, 8 s after `halves.jsonl`), DETACHED as `nohup env MIND_AGENT=alpha MIND_SID=<sid> STORAGE_BACKEND=local bash core/scripts/run-full-suite.sh > <log> 2>&1 < /dev/null &` (log dir `ayoai-suite-run-alpha`), for the g-115-11591 deep-code closure (carrier merge a79e9a191f, fix ff5a531166)

`TOTAL: 26693 passed, 44 failed, 0 errors` / `VERDICT: GENUINE failures -- trustworthy, act on them`.
- Per chunk (failed / passed): 00 = 6/5982, 01 = 15/6333, 02 = 17/7696, 03 = 6/6682 (spread). Chunk 03 took ~23 min.
- Invisible half `82/84`. Domain half `116/118`: `pytest-batch` and `test_efs_classify_halt.sh`, both world/ code that this change does not touch.
- `--triage`: 2 environmental | 16 genuine-owned | 2 genuine-VERIFY (g-115-11761, g-115-9181) | 0 genuine-UNOWNED.
- DIFFERENTIAL against the 09-30 run above, read from that run's rotated chunk logs in `<logdir>/prev/`: all 34 of its reds are still red, and 10 are new. None traces to the merge:
  - 1 is `test_goal_selector_world_source_derivation`, plus `test_goal_selector_intended_agent_inverse.py` in the invisible half. Both hardcode `OFFROSTER_AGENT = "delta"`, and delta is back on the live roster since a Body wrote `agent_status.delta` at 04:51:58 (g-115-11761).
  - 9 are `test_scorer_override_audit` x8 and `test_skill_discovery_companion_scripts::test_same_second_events_dedup_upstream`. `read_fleet_diaries` unions live worker-Body carrier rows whatever `base` is, since 34e9161d5f (g-306-575).
  - `test_aspirations_update_goal_source_value.sh` (invisible half) was already red on 09-30 and is now red SOLO (g-115-11764).
- SIDE-EFFECTS repeated exactly as recorded above. The cadence slots were advanced at 05:54:57 / 05:55:05 (chunk 01). The anchor was set to `g-001-01` at 06:33:26 (chunk 03). BOTH were restored after the run: the cadence values from the compaction checkpoint's `all_slots` (written 05:05:56, before launch), and the anchor from a `loop-state-save.sh read` snapshot taken before chunk 03.

### 2026-10-01T04:03Z–05:17Z — alpha WORKER BODY (reducer live on cc-04, not here), `hostname` cc-07, `uname -r` 6.8.0-142-generic, STORAGE_BACKEND=local pin, LIVE DAEMON, MAIN REPO, runner-default 4 chunks, 1646 files, HEAD 6fa9c9f88b held for the whole run, launched IN-TURN (harness-tracked background task, waited on in-turn, never detached) as `env -u BODY_WM_PATH -u BODY_ROLE MIND_AGENT=alpha MIND_SID=<sid> STORAGE_BACKEND=local bash core/scripts/run-full-suite.sh > /tmp/suite-alpha-11554.log 2>&1 < /dev/null` (log dir `ayoai-suite-run-alpha`), for the g-115-11554 closure. The change under test (`_paths.py` missing-root refusal) was UNCOMMITTED during the run and committed after it as 2aa2d6229d.

`TOTAL: 26692 passed, 45 failed, 0 errors` / `VERDICT: GENUINE failures -- trustworthy, act on them`, `SUITE-RC=1`.
- Per chunk (failed / passed): 00 = 8/5980, 01 = 14/6340, 02 = 16/7691, 03 = 7/6681. Wall clock ~74 min.
- Invisible half `82/84 files passed` (reds: `test_goal_selector_intended_agent_inverse.py`, `test_aspirations_update_goal_source_value.sh`). Domain half `117/118 unit(s) passed, 1 skipped` (red: `test_efs_classify_remote_body.sh`).
- New since 09-29 in chunk 02: `test_owncloud_integration` x6 (S3 HeadObject 403, owned by g-115-10069) and `test_scorer_override_audit` x8.

`--triage`: **2 environmental | 14 genuine-owned | 2 genuine-VERIFY | 1 genuine-UNOWNED**. Environmental: `test_goal_field_append` (52/52 solo) and `test_learning_routing_world_scope` (3/3 solo). VERIFY: `test_iteration_close_quality_flag_carry` (g-115-9181 names its three live param ids) and `test_scorer_override_audit` (g-115-7459 names the file only; the 8 reds read the LIVE agents root, `{'alpha': 50, 'bravo': 4}` against `{'bravo': 4}`). UNOWNED: `test_skill_discovery_companion_scripts::test_same_second_events_dedup_upstream`.

ATTRIBUTING AN UNCOMMITTED CHANGE. The triage's `recent commits (7d)` cannot see a change that is not committed, so attribution was a one-variable A/B. The 17 genuine red files ran in one pytest batch with the modified `_paths.py` (42 failed / 350 passed), then again with HEAD's `_paths.py` swapped in on disk under an EXIT-trap restore (42 / 350). The failing node-id sets were IDENTICAL, and the restore was hash-verified. The invisible and domain reds: `intended_agent_inverse` and `efs_classify_remote_body` fail identically in both arms; `source_value` is flaky in both (modified 1 of 3 runs failed, HEAD 3 of 3). A pytest plugin cannot run this control for `_paths.py`, because subprocess tests import the file from disk.

SIDE-EFFECTS on this Body, the 09-29 / 09-30 signatures again:
1. Body WM `last_fresh_eyes_review` / `last_fresh_eyes_tree_review` were stamped at 04:15:33 / 04:15:41 (chunk 01) to 16203, despite `env -u BODY_WM_PATH`. Restored with wm-set.sh to the pre-run values, read from this session's 03:48 `compact-checkpoint.yaml` (09-27T21:26:56 / 15801 and 09-27T08:35:17 / 15742).
2. team-state `shared_cadences.last_fresh_eyes_tree_review` was restamped at 04:56:37 / 15809 / alpha. No local copy holds its prior value: `world/.history` keeps no team-state versions, and the only on-disk backup (`team-state.yaml.rb671-test-backup.79624`) dates from 2026-08-05. Store-side object versions were not probed.
3. `sessions/<sid>/iteration-checkpoint.json` was anchored to `g-001-01` at 04:57:46 (chunk 03), and the Bash hook then injected `MIND_GOAL_ID=g-001-01` into every call. Re-anchored with `loop-state-save.sh init --goal-id g-115-11554 --source world`, the writer the claim itself uses.

### 2026-10-01T09:07Z–10:18Z — alpha REDUCER, `hostname` cc-04, `uname -r` 6.8.0-142-generic, STORAGE_BACKEND=local pin, LIVE DAEMON, MAIN REPO, runner-default 4 chunks, 1648 files, HEAD 1c10594b4e held for the whole run, DETACHED as `nohup env MIND_AGENT=alpha MIND_SID=<sid> STORAGE_BACKEND=local bash core/scripts/run-full-suite.sh > <log> 2>&1 < /dev/null &` (log dir `ayoai-suite-run-alpha`, `.run-lock.json` taken), for the g-306-284 occ277 consume of worker refs 9c5cc235 (merge 50f207152b, which carries the 2aa2d6229d `_paths.py` missing-root refusal) and dec9c5e6 (merge 64cd8b63a3), pushed in sync merge 1c10594b4e

`TOTAL: 26736 passed, 45 failed, 0 errors` / `VERDICT: GENUINE failures -- trustworthy, act on them`, `SUITE-RC=1`.
- Per chunk (failed / passed): 00 = 7/5981, 01 = 15/6341, 02 = 17/7698, 03 = 6/6716. Wall clock ~71 min.
- Invisible half `82/84`. Domain half `117/118`: `pytest-batch` only, the same 15 `test_usage_liveness_*` reds as the 05:40Z run. `test_efs_classify_halt.sh` was green this time.
- `--triage`: 2 environmental | 16 genuine-owned | 3 genuine-VERIFY | 0 genuine-UNOWNED. Each VERIFY file is named by an open goal: `test_goal_selector_world_source_derivation` (g-115-11761), `test_iteration_close_quality_flag_carry` (g-115-9181), `test_completed_not_committed_scoped_probe` (g-115-6350).
- DIFFERENTIAL against the 05:40Z run (`<logdir>/prev/`): 44 of 45 reds identical, none gone, 1 new. The new one is `test_completed_not_committed_scoped_probe::test_goal_scoping_narrows_the_population_to_one`, and it is LIVE-STATE coupled, not the merge. Its helper takes the first completed goal of the agent queue's asp-001: there were 3 at 6aa55c6c21 and 0 live after archival. With none, it falls back to a world goal, of which the sweep scans 0.
- SCOPED-TIER CONTRAST. Same change, `run-scoped-suite.sh --since 03dde50b88`, 243 files (14.75%) in ONE process: 40 reds, 36 of them test-order pollution that this chunked run did not show.
  - Polluter: `test_fileops_snapshot_blacklist_and_gzip.py`. Its `with_sandbox` re-imports `_paths` under sandbox roots, then deletes the sandboxes without restoring `sys.modules`.
  - Reproduced: polluter first gives 6 failed; the reverse order gives 35 passed. Identical on `git archive` exports of the pre-merge and post-merge trees.
  - The 05:40Z chunking kept the polluter (chunk 01, position 66) away from every victim; a scoped run's single process is the shape that exposes it (g-115-10434).
- SIDE-EFFECTS:
  1. WM `last_fresh_eyes_review` / `last_fresh_eyes_tree_review` stamped at 09:20:19 / 09:20:27 (chunk 01).
  2. The anchor was set to `g-001-01` at 09:58:08 (chunk 03).
  3. team-state `shared_cadences.last_fresh_eyes_tree_review` restamped at 09:20:27 / 15821 / alpha.
  Items 1 and 2 were restored from a pre-launch snapshot (13 WM slots plus `loop-state-save.sh read`) and read back equal. Item 3 was NOT restored: the snapshot did not cover team-state, so its prior value is unknown. Add `team-state-read.sh --field shared_cadences.last_fresh_eyes_tree_review` to the pre-launch snapshot.

### 2026-10-03T15:38Z–16:46Z — alpha WORKER BODY (reducer live on cc-04, not here), `hostname` cc-08, `uname -r` 6.8.0-142-generic, 20 cores / 12 GB RAM, STORAGE_BACKEND=local pin, LIVE DAEMON (pid 297564, not restarted), MAIN REPO, runner-default 4 chunks, 1696 files, HEAD a360f8f888 held for the whole run, launched IN-TURN (harness-tracked background task, Bash timeout 7200000, waited on in-turn, never detached) as `env -u BODY_WM_PATH -u BODY_ROLE MIND_AGENT=alpha MIND_SID=<sid> STORAGE_BACKEND=local bash core/scripts/run-full-suite.sh > /tmp/suite-alpha-u13.log 2>&1 < /dev/null` (log dir `ayoai-suite-run-alpha`), for the g-358-202 U13 unit. The change under test was COMMITTED (U1 to U12 and the g-115-10980 commits are ancestors of HEAD; none is on origin/main yet).

`TOTAL: 28187 passed, 33 failed, 0 errors` / `VERDICT: GENUINE failures -- trustworthy, act on them`, runner rc=1.
- Per chunk (failed / passed): 00 = 10/6331, 01 = 14/6785, 02 = 2/8010, 03 = 7/7061. Wall clock 67.6 min (15:38:38 to 16:46:11); chunks ended 15:49:01 / 15:56:28 / 16:06:30 / 16:28:31.
- Invisible half `81/84` (reds: `test-g3-worker-store-rails.sh`, `test_aspirations_claim_source_flag.sh`, `test_aspirations_update_goal_source_value.sh`). Domain half `121/122`, 1 skipped (red: `test_promotion_day_smoke_leg.sh`). The four reds outside the chunked half were green solo, 3 of 3 for three of them and 2 of 3 for `source_value` (flaky, as the 2026-10-01 entry says); the in-suite cause was not identified.
- `--triage`: **3 environmental | 13 genuine-owned | 3 genuine-VERIFY | 0 genuine-UNOWNED**, 19 files. Environmental: `test_goal_field_append` (56/56 solo), `test_learning_routing_world_scope` (3/3), `test_skill_attribution_join` (27/27). VERIFY, each opened: `test_completed_not_committed_scoped_probe` is named by g-115-6350 verbatim, `test_iteration_close_quality_flag_carry` is g-115-9181's carried 9, and `test_daemon_import_surface::test_every_loaded_module_is_in_the_pathspec` is named by no goal (`knowledge_projection` missing from the `mind-api-code-changed.sh` pathspec since c621d7cac1; relayed under g-358-202 U8, still queued).
- None of the 19 files tests the composite writer or the own-cloud backend. All 11 `test_owncloud_composite_*_g358202.py` files ran in chunk 02, which failed 2 tests in 2 other files.
- DIFFERENTIAL against the 2026-10-01T04:03Z worker entry (cc-07, HEAD 6fa9c9f88b, 1646 files; 26692 passed / 45 failed; chunks 8/14/16/7; invisible 82/84; domain 117/118): absent now are `test_owncloud_integration` x6, `test_scorer_override_audit` x8 (why not investigated), `test_skill_discovery_companion_scripts::test_same_second_events_dedup_upstream`, `test_goal_selector_intended_agent_inverse.py` and `test_efs_classify_remote_body.sh`. New with no earlier entry: `test_daemon_import_surface`, `test_wm_append_unknown_slot` (g-115-10207), `test_runtime_utilization`, `test_goal_id_five_digit_seq` (g-115-11201: `defer_self_artifact.py:114` bounds the goal-id sequence at 5 digits, from g-353-109's commits of 2026-09-26), `test_skill_attribution_join` (environmental), `test-g3-worker-store-rails.sh` and `test_promotion_day_smoke_leg.sh` (both green solo).
- THE MOTO SKIP TRAP (guard-6825), measured. This run's system python has no moto and runs pytest 7.4.4; `test_owncloud_integration` skips at module level, which is why the six g-115-10069 reds did not appear. The 18 moto-importing files give 169 passed / 145 skipped under it and 519 passed / 4 skipped under an isolated venv (moto 5.2.3, pytest 9.1.1, `STORAGE_S3_ENDPOINT_URL` unset), same HEAD: the chunked run could not establish 350 of those cases. All 7 `test_owncloud_integration` tests pass by name under the venv. The two instruments also differ in pytest version.
- HANG BOUND NOT HONORED HERE (g-115-6801 owns it): the system pytest ignores `faulthandler_exit_on_timeout` (warning once in each chunk log) and arms `faulthandler_timeout` as a dump with no exit, so a hung test would not have aborted at 600 s. None hung.
- SIDE-EFFECTS:
  1. WM `last_fresh_eyes_review` / `last_fresh_eyes_tree_review`: NOT stamped this time (read back equal to the pre-run values).
  2. The iteration checkpoint stayed on `g-358-202`.
  3. team-state `shared_cadences.last_fresh_eyes_tree_review` restamped at 15:50:54 / 16042 / alpha (its `__inflight_claim` at 15:50:57), in chunk 01. The writer is `test_fresh_eyes_record_tick_unknown_flag.py` (A/B: run solo it moved the live key to 16:57:37 / 16044; the other two cadence tests did not). It was not a real fire: the pre-run fire was 14:56:53 at count 16037 and the window is 25 goals (g-115-6065). RESTORED with `team-state-update.sh` from the pre-launch snapshot and read back byte-identical (582 B). The snapshot now covers team-state, which is what the 09:07Z entry asked for.
- METHOD NOTE: `chunk-NN.args` is written when chunk NN STARTS, so a membership probe run in the first minutes reports "NOT IN ANY CHUNK" for every file in a later chunk; predict membership from the sorted file list (chunk 00 was exactly the first 424 of `core/scripts/tests`) or read the args after the chunk begins.
- Evidence (42 files, store read-back identical): `world/audit-reports/g-358-202/u13-full-suite/`.

### 2026-10-04T03:12Z–04:23Z — alpha WORKER BODY (reducer live on cc-04, not here), `hostname` cc-08, `uname -r` 6.8.0-142-generic, 20 cores / 12 GB RAM, STORAGE_BACKEND=local pin, LIVE DAEMON (pid 3366516, not restarted), MAIN REPO, runner-default 4 chunks, 1703 files, HEAD 44f29bf0b5 held for the whole run (origin/main e777c42907 plus 39 carried non-merge commits, 34 files), launched IN-TURN (harness-tracked background task, Bash timeout 7200000, waited on with bounded polls), no commit, merge or tracked-file edit in the window, g-358-202 U19

`TOTAL: 28629 passed, 35 failed, 0 errors` / `VERDICT: GENUINE failures -- trustworthy, act on them`, runner rc=1.
- Per chunk (failed / passed): 00 = 10/6559, 01 = 14/6866, 02 = 2/8060, 03 = 9/7144. Wall clock 70.2 min (03:12:31 to 04:22:40); chunks ended 03:23:31 / 03:31:39 / 03:42:50 / 04:05:13.
- Invisible half `82/84` (reds: `test-g3-worker-store-rails.sh`, `test_aspirations_claim_source_flag.sh`; both green solo three of three, as in the 2026-10-03 entry). Domain half `123/123`, 1 skipped; its 8 pytest batches read 4103 passed.
- DIFFERENTIAL against the 2026-10-03T15:38Z worker entry (HEAD a360f8f888, 28187 passed / 33 failed): 32 node ids are red in both, 3 are new, 1 is gone. None of the 20 red files is one of the 34 carried files. GONE: `test_daemon_import_surface::test_every_loaded_module_is_in_the_pathspec` (13 passed solo). The two invisible/domain reds of that entry that did not recur: `test_aspirations_update_goal_source_value.sh`, `test_promotion_day_smoke_leg.sh`.
- NEW 1, THE RANGE'S OWN: `test_check_stderr_json_merge::test_repo_core_scripts_clean`. `check-stderr-json-merge.py` flagged `composite-gc-tick.sh` (variable `out`: the runner's output with `2>&1`, fed to `json.loads(lines[-1])`); the detector run over each committed version of the tick reads clean at U15a (709dad3066) and flags at U16 (ac995601c3) and U17 (1fcefde350). Fixed in the U19 commit (the router takes the last line that opens a JSON object).
- THE SCOPED TIER CANNOT SEE THIS CLASS, measured. `run-scoped-suite.sh --changed core/scripts/composite-gc-tick.sh --list-only --json` selects ONE file, the tick's own test; the scanner test reads every top-level `core/scripts/*.sh` and does not name the tick, so a script edit that turns it red is invisible to the by-reference selection and shows only in a full run. The scoped tier has no always-run or whole-tree list (grep of `run-scoped-suite.py`).
- NEW 2, ORDER-DEPENDENT AND NOT FROM THIS RANGE: `test_runtime_store_rbguard::test_guard_set_field_erase_blanks_the_rule_of_a_retired_record` and `::test_guard_set_field_erase_refuses_a_record_that_is_not_retired`, both `erase_not_local` from `store.py:587` `_backend_is_local()` = `isinstance(get_backend(), LocalBackend)`. The tests came with f3d3cf1bba (g-335-1726 u3b, on origin/main, not in the carried range, not in the 2026-10-03 tree). Green solo (11 erase tests) and green paired with each of 22 earlier chunk-03 files that touch the backend or its env. `test_tree_match_prefetch_wiring.py::test_local_backend_prefetch_is_a_real_no_op` does `importlib.reload(storage_backend)`; when `store.py` was imported before it, `store.py` holds the old `LocalBackend` class and the isinstance is False for the rest of the process. Reproduced in three runs (store.py imported at collection, then the reload test, then the two erase tests: both red; without the reload test: green; the reload test without the earlier import: green).
- THE MOTO SKIP TRAP (guard-6825) again: the 20 moto-importing files give 270 passed / 218 skipped under system python and 696 passed / 6 skipped under the venv (moto 5.2.3, pytest 9.1.1, `STORAGE_S3_ENDPOINT_URL` unset), same HEAD; the 6 skips are structural (page size and truncation injected into the in-memory double, moto's PUT-time `last_modified`, one Windows-only prefix). The list grew from 18 files to 20 (`test_audit_baselines_hand_raise_g115_9813.py`, `test_composite_gc_runner_g358202.py`).
- HANG BOUND NOT HONORED HERE (g-115-6801 owns it): same as the 2026-10-03 entry; none hung.
- SIDE-EFFECTS: (1) WM cadence slots and the iteration checkpoint read equal to the pre-launch snapshot. (2) team-state `shared_cadences.last_fresh_eyes_tree_review` restamped at 03:25:28 / 16076 / alpha and its `__inflight_claim` at 03:25:31 by `test_fresh_eyes_record_tick_unknown_flag.py` (the daemon hop, per its own docstring); both restored from the pre-launch snapshot with `team-state-update.sh`, read-back byte-identical (582 B). (3) The coordination board: 36 posts in the window, none from a test.
- `--triage` was NOT re-run; the 32 shared reds carry the 2026-10-03 entry's `--triage` (3 environmental, 13 genuine-owned, 3 genuine-VERIFY, 0 genuine-UNOWNED) and the ownership block in `suite-run.log`.
- Evidence (store read-back identical): `world/audit-reports/g-358-202/u19-full-suite/`.

### 2026-10-06T05:11Z–14:18Z (triage to 18:11Z; follow-up run 19:29Z–20:07Z) — bravo (assistant-mode chat; no loop on this box), `hostname` DESKTOP-O91DLK2, `uname -r` 3.4.10-87d57229.x86_64 (MSYS/Git Bash, Windows 10 19045), STORAGE_BACKEND=local pin, LIVE DAEMON (pid 37072, v2.12.93, not restarted during the run), MAIN REPO, `nohup env MIND_AGENT=bravo MIND_SID=<sid> STORAGE_BACKEND=local …` launch (log dir `ayoai-suite-run-bravo`, so the vars arrived), runner-default 4 chunks, 1721 files (431/430/430/430), HEAD 88cd17ecef held for the whole run, for the v2.12.94 promotion (the temp/ review lifecycle, 2811057a30)

**VERDICT: GENUINE, TOTAL 29647 passed / 293 failed / 37 errors**, runner log `suite-bravo-88cd17ecef.log`. Per chunk (passed / failed / errors): 00 = 6695/57/12, 01 = 6958/76/25, 02 = 8793/69/0, 03 = 7201/91/0, taking 74, 84, 88 and 89 min. The failures are spread over all four chunks, so this is not the one-chunk confinement that marks contention. The chunked half took 5 h 35 min; the invisible + shell + domain halves took a further 3 h 32 min (10:46Z to 14:18Z). Invisible half `76/83 files passed, 0 quarantined`; domain half `102/128 unit(s) passed, 2 skipped`. This is the first complete Windows verdict on this box in the ledger: the 2026-09-28 alpha attempt on the same box was voided by HEAD moves (row above), and this run held HEAD for 9 h 7 min.

**Triage (`--triage`, 14:40Z to 18:11Z): 93 failing files = 8 environmental | 33 genuine-owned | 30 genuine-VERIFY | 22 genuine-UNOWNED.** Not covered by the triage: the 37 ERROR entries (it reads FAILED only) and the invisible and domain halves. Its ownership search reads open goals only, so a red whose test shipped under a completed goal reads as unowned.

**Reading the reds.**
- 59 of the 93 files sit outside the blast radius of the v2.12.93..HEAD delta (`run-scoped-suite.sh --since v2.12.93 --list-only` selects 525 of 1721 files; 34 failing files fall inside it).
- One cause explains the history cluster (g-115-12136): `_history_store._unique_tmp` returned a `str` on Windows since 391f8a4d32 (v2.12.91), so every snapshot raised `AttributeError: 'str' object has no attribute 'write_bytes'` inside a best-effort writer that swallows it. It accounts for five runner-style suites (`test_history_store`, `test_history_cli_stage2`, `test_history_prune_legacy_stage3`, `test_history_shadow_mode`, `test_fileops_corruption_guards`) and the pytest files that assert a `.history/snapshots` entry. Fixed in 3a47f6a543. After it, `test_history_store` reads 32/32 (6 of its 31 checks failed before), `test_history_prune_legacy_stage3` 16/16, `test_history_shadow_mode` 11/11, `test_fileops_corruption_guards` 8/8, `test_history_cli_stage2` 10/12. The two left are g-115-12137: manifest names come from `datetime.now()`, which ticks every ~15.6 ms on this box, so two snapshots of one file inside a tick can share a name. That fits the two failures; it was not isolated with a controlled clock.
- The 37 ERRORs were two harness causes, fixed in c046543c7e and read green afterwards: node ids that embed whole source files overflow the 32,767-character environment-variable cap (16 entries, g-115-12113), and `mktemp -d` paths plus a fake `ssh` that Git Bash's PATH rebuild cannot shadow (21 entries, g-115-8767).
- 15 files / 73 tests had no owner (g-115-12138). The first failure line of each falls into five families: CRLF where the test compares LF, a multi-line `bash -c` harness, chmod- and signal-based simulation, test-environment assumptions, and a helper that returns `None`. Those mechanisms are inferred from the first failure line, not verified. One of them, `test_wm_write_yaml_temp_path` (`PermissionError` under concurrent writes), may be a product defect, because the writer is shared by every working-memory write.

**Follow-up at c046543c7e (19:29Z–20:07Z), the tested tip of v2.12.94.** Not the runner's serial method: 125 files (the 123 that `run-scoped-suite.sh --since 81a062e61a` selected plus `test_close_review_queue.py` and `mind_api/tests/test_goal_field_allowlist.py`) as three concurrent pytest processes of 32, 9 and 38 min, so it is a differential by test id, not a verdict. 2606 passed, 103 failed, 5 skipped. 101 of the 103 failing ids were red in the full run above. The other 2 are `test_goal_close_risk_tier.py::test_a_store_read_refusal_STAMPS_the_request_through_the_store_writer[no-request]` and `[answered-by-REJECT]`: the test arrived in 46037873bd (g-375-147) after the full run, reproduces solo, and fakes a landed write with `["/bin/true"]`, which native Windows Python cannot run (`FileNotFoundError: [WinError 2]`), so the gate records the write as not landed (filed on g-115-12138).

**Consequence for ZDS.** ZDS-Mind has a checkout with a live daemon on this box (v2.12.3), and omni also runs it on Linux hosts: the one real `--adopt --c4-baseline` attempt so far (v2.12.92, 2026-10-05) ran on cc-06 (Linux 6.8.0-142), and the v2.12.93 plan ran on cc-11. `framework_pull.py --adopt --c4-baseline` rolls back on any pytest node id that fails after adoption and did not fail before, and a test that exists only in the new release counts as new. So a Windows red above that is new or changed since v2.12.3 blocks an adoption that RUNS on a Windows host, and does not gate one that runs on a Linux host; which box runs the adoption that lands is not established here. A rough comparison with omni's 2026-09-29 run on this box (a tree whose version I could not identify) gives 289 ids red here and not there, in 87 files. That is not ZDS's own pre-adopt failing set, which has not been measured.

### 2026-10-06T22:28Z–2026-10-07T04:03Z (classification re-runs and a previous-plant control to 04:42Z) — bravo (assistant-mode chat; no loop on this box), `hostname` DESKTOP-O91DLK2, `uname -r` 3.4.10-87d57229.x86_64 (MSYS/Git Bash, Windows 10 19045), STORAGE_BACKEND=local pin, STAGING PLANT v2.12.94: the first full run of a plant in this ledger. An ISOLATED CLONE of the PR #105 head (133d542b; `git clone --local` of Claude-Mind in the system temp dir, detached), its own daemon (pid 6772, started outside pytest), `MIND_AGENT=seedcheck`, no `.env.local`, `nohup … run-full-suite.sh` detached launch, runner-default 4 chunks, 1721 files (431/430/430/430), `PYTEST_ADDOPTS=--continue-on-collection-errors`, HEAD held for the whole run, for the v2.12.94 promotion (the repo owner asked for the full suite on staging; the runbook's default for a staging hop is the bounded residue check)

**VERDICT: GENUINE, TOTAL 29298 passed / 495 failed / 2 errors**, runner rc=1. Per chunk (passed / failed / errors): 00 = 6534/128/0, 01 = 6914/145/0, 02 = 8724/123/1, 03 = 7126/99/1, taking 76, 81, 75 and 83 min; the halves took a further 19 min (03:43Z to 04:03Z). Wall clock 5 h 34 min, against 9 h 7 min for the dev run at 88cd17ecef on the same box. No NUL bytes in any chunk log. Invisible half `70/83 files passed, 0 quarantined`; deferred set empty; the domain half did not run (a seed world has no domain runner).

**Setup record.** Run 1 (21:25Z) was VOID, not a result: the clone's meta tier had never been initialized, so `goal-selector.py` raised `MetaNotReadyError` at import in two files, pytest stopped at collection (rc=2), and the runner printed `VERDICT: INVALID` with 0 of 1721 files run; run 2 followed `init-mind.sh` on the clone. Run 2 ALSO lacked `agents/seedcheck/local-paths.conf`, which `goal-selector.py::_agent_is_resident()` and `capability-gate.py::_resolve_world_dir()` both read, so it measured a deployment no real Mind is; the re-runs below add the file. Two collection errors were run past with the flag: `mind_api/tests/test_provision_aws.py` (the seed ships the test without `mind_api/scripts/provision_aws.py`) and `core/scripts/tests/test_recurring_close_interval_review.py` (module-level anchors embed goal ids that the plant scrubs). Both are fixed in dev by a176af2b96, which v2.12.94 does not carry, and both exist at the previous plant too; without the flag the runner stops at rc=2 and `framework_pull`'s C4 refuses the INVALID run.

**Reading the 497 failing ids** (495 failed plus 2 errors). The dev run at 88cd17ecef has 322 failing ids. 264 ids are red at the plant and not in that set. 89 ids in 30 files are red in the dev run and not at the plant: 21 efs-ssh harness ids (g-115-8767, fixed in c046543c7e), 11 ids in the file that cannot collect at the plant, 10 in the moto file below, and 47 in 26 other files, the history and runtime-store files among them (not re-run).
- 62 ids in 22 files are test defects that the plant's own transform or manifest creates: goal-id anchors against scrubbed comments, path-scrub literals, a fixture prefix the `MIND_` to `MIND_` rewrite changes, a script the manifest excludes that an allowlist names. Fixed in dev (g-360-31 batches 1-4: a83591d642, d9dff51317, 45b0eb7593, b13d0b08cf; 16 + 11 + 2 + 33 ids). Run as a plant holds them (real seed transform, written into this clone, `local-paths.conf` present): 62 of 62 pass. The one other red in those files, `test_msys_phantom_root::test_absolutize_never_rewrites_on_a_posix_host`, is red in dev too.
- The other 50 files with plant-only candidates were re-run after the run in 4 shards each: in the plant clone with `local-paths.conf`, and at dev main (82c41ad646). They carried 223 failing ids in the staging run. **22 are red at dev main too** (not plant effects). **69 pass in both re-runs**, artifacts of the first run: 30 are the midnight-crossing class (g-115-12182; five knowledge-apply files; the run crossed 00:00Z), 27 are goal-selector and capability-gate ids that fail only WITHOUT the residence file (measured: the four files holding them, run in the clone with `local-paths.conf` moved aside, fail 20 + 3 + 1 + 3 of them, plus 12 ids in `test_capability_gate_generic_name_parts` that stay red with the file present), and 12 are the moto file below. **132 ids in 40 files fail in the clone and pass at dev main.** By their first failure lines and the test sources (a reading, not an isolation of each): 62 ids in 19 files need the dev world's agent tree (no `agents/alpha` or `agents/bravo`, no `self.md`, no residence file, empty stores); 61 ids in 15 files need the dev world's capability-routing convention and forged-skill registry, which a fresh seed world lacks; 5 ids in 4 files read a dev-only file the seed does not ship (`.claude/settings.local.json`, the forged `notify-user` skill; fixed in g-360-31 batch 5, 4d9e9ac8e0); 2 are the moto file; 2 are the collection errors above.
- **CONTROL AT THE PREVIOUS PLANT (37ba7716).** The same 62 files (the 40 above and the 22 fixed ones) ran in the same clone with HEAD moved to the previous plant and the clone's daemon recycled to that code (`mind-api-start.sh --restart`, outside pytest; it otherwise held v2.12.94 code in memory), in 4 shards in 6 min (04:36Z to 04:42Z), then HEAD and the daemon were returned. **131 of the 132 plant-only ids and all 62 fixed ids are red at the previous plant too.** All 62 files exist there, and only two changed in the v2.12.93..v2.12.94 source delta (`test_claim_worker_pull.py` and `test_owncloud_composite_gc_prune_exec_g358202.py`). The single id green at the previous plant is in that moto file, which is not stable: 25 distinct ids were red across its four runs (staging run, plant re-run, dev main re-run, control), a different set each time (g-115-12138). So nothing that the plant fails and dev does not is attributable to v2.12.94 except that flake: the stock predates the hop, and no earlier run could show it because no staging full run existed.
- Invisible half: 13 files red at the plant, 7 at 88cd17ecef in dev. At the plant: `test_capability_gate_{narrative,suggest_unblock,user_only_precondition}`, `test_cross_lane_claim`, `test_defer_to_unblock_integration`, `test_goal_selector_intended_agent_inverse`, `test_history_cli_stage2`, `test_layer_d_telemetry`, `test-g3-worker-store-rails.sh`, `test-invisible-suites-agent-resolution.sh`, `test_aspirations_claim_source_flag.sh`, `test_guardrails_update_field_argv.sh`, `test_temp_drain_purge.sh`. The four history suites that were red in dev are green at the plant. The ten that were not red in dev are named for the capability and roster families above; this half was NOT re-run with the conf or at the previous plant, so it is not classified.
- NOT MEASURED: Linux (no Linux on this box: WSL hangs, no Docker); ZDS's own pre-adopt failing set; which host runs the adoption that lands.
- METHOD NOTES. (1) A suite clone needs initialized tiers AND `agents/<agent>/local-paths.conf` before its first run: run 1 was void for the first, run 2 was skewed by the second. (2) `-q` together with the config's addopts suppresses pytest's summary line; judge a shard by its rc, its dot lines and its `FAILED` lines. (3) A control at another commit must recycle the clone's daemon to that commit's code, or wrapper-backed tests answer an old test with new endpoints. (4) A worktree reference run has no `mind_api/state/daemon.port` and crawls (guard-5702, guard-6394); the dev reference ran in the main repo with `STORAGE_BACKEND=local`.
- SIDE-EFFECTS: none on the live fleet. The dev daemon (pid 53504) and ZDS-Mind's (pid 52076) were alive at 04:42Z. The clone's daemon was recycled twice by this work (6772 to 59004 to 37312).
