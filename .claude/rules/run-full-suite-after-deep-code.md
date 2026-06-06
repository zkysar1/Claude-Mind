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
1. Run the daemon-SAFE full suite:
   `python -m pytest core/scripts/tests -q -m "not daemon_integration"`.
   The `daemon_integration` marker (registered in `pytest.ini`) tags the only
   tests that spawn REAL subprocess daemons and/or count system-wide
   `mind_api.src` processes — currently just `test_daemon_orphan_prevention.py`.
   Excluding them, the rest of the suite is hermetic (the in-process
   `_daemon_fixture.py` / `running_daemon` fixtures bind a thread-local daemon
   in a tmp project root) and is safe to run with a live daemon present.
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

## Required Full-Suite Commands (per code area)

### Mind framework

| Path touched | Full-suite command | Pass criterion |
|---|---|---|
| `core/scripts/*.py` (non-test) | `cd PROJECT_ROOT && python -m pytest core/scripts/tests -q` | exit code 0, all collected tests pass |
| `mind_api/src/*.py` | `python -m pytest core/scripts/tests -q` (runtime is exercised by daemon-aware wrappers in core/scripts/tests) | exit 0 |
| `core/scripts/*.sh` (production wrapper) | Whatever the wrapper's daemon endpoint suite covers — typically `python -m pytest core/scripts/tests -q -k <endpoint>` | exit 0 |
| `.claude/skills/*/SKILL.md` | `bash core/scripts/skill-evaluate.sh <skill-name>` AND consider `/verify-learning` for cross-skill grep checks | five-dim score ≥ acceptance threshold |
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
