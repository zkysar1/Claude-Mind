---
name: verify-learning
description: "Runs post-test verification: checks the agent's state against a comprehensive checklist of learning artifacts (tree encodings, resolved hypotheses, reflections, skill return-protocols, guardrails, pattern signatures) and reports missing or drifted items. Use whenever the user says /verify-learning, after major framework changes to confirm the loop still produces learning, after a session with many commits and no encodings, or when framework-level health needs a diagnostic sweep. User-invocable AND agent-callable."
triggers:
  - "/verify-learning"
conventions: [aspirations, pipeline, experience, reasoning-guardrails, pattern-signatures, spark-questions, journal, tree-retrieval, goal-schemas, session-state, infrastructure, secrets, handoff-working-memory]
minimum_mode: reader
revision_id: "skill-bootstrap-verify-learning-e6052f"
previous_revision_id: null
---

# /verify-learning — Post-Test Verification

User-invocable AND agent-callable (hybrid skill).

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Step 1: Load Checklists

1. Read `core/config/verification-checklist.md` (framework checklist).
2. Read `core/config/verification-checklist-domain-specific.md` (foundational domain checklist template — see file header for the three-tier loading explanation).
3. IF `world/verification-checklist.md` exists:
   Read it (agent-discovered domain checks).
   ELSE: Note "No agent-discovered domain checks — skipping."

### Step 1.1: Load-Time Sanity (rot detection)

Counts active content lines per loaded file — BOTH `Check:`-prefixed lines
AND numbered-discovery lines (leading `\d+\. `). If any deployment-overlay
file contributes ZERO of BOTH AND the deployment has ≥10 completed goals,
emits a SOFT WARNING (does not fail verification). Catches the rot pattern
where a deployment-specific checklist has silently become a stub. Created
2026-05-17 (Phase 1.2 packaging cleanup) after the domain template had
silently been a 23-line stub since 2026-04-06 with no warning surfaced.

The OR-match on both prefix shapes was added 2026-05-19 (g-115-955) after
a populated-but-prose overlay (`world/verification-checklist.md` with 81
numbered-discovery lines from alpha-era encode-session) false-triggered the
"0 Check: lines" rot warning. Numbered ordered lists ARE substantive content
when they enumerate discovered checks — the previous `^\s*Check:`-only regex
treated them as zero. Real rot looks like a 23-line stub; populated prose
contributes either format and should PASS.

   Check: domain-overlay verification checklists are not rotted to placeholder content. Bash: `py -3 -c "import sys,re,pathlib,json; sys.path.insert(0,'core/scripts'); from _paths import WORLD_DIR; files=[('core/config/verification-checklist-domain-specific.md',pathlib.Path('core/config/verification-checklist-domain-specific.md')),('world/verification-checklist.md',WORLD_DIR/'verification-checklist.md')]; total=0; asp=WORLD_DIR/'aspirations.jsonl'; total = sum(sum(1 for g in __import__('json').loads(L).get('goals',[]) if g.get('status')=='completed') for L in asp.read_text(encoding='utf-8').splitlines() if L.strip()) if asp.exists() else 0; warns=[f'{label} loaded but 0 content lines (Check: or numbered-discovery) (deployment has {total} completed goals - likely rotted)' for label,p in files if p.exists() and sum(1 for L in p.read_text(encoding='utf-8').splitlines() if re.match(r'^\s*Check:|^\s*\d+\.\s',L))==0 and total>=10]; print('WARN: '+'; '.join(warns)) if warns else print(f'PASS: all loaded overlay checklists contribute content lines (deployment has {total} completed goals)')"`

## Step 2: Evaluate Each Section

For each item in ALL sections from all checklists (A through the last section):
1. Read the referenced file
2. Report **PASS**, **FAIL**, or **N/A** (if the agent didn't reach that stage)

For section G (Known Design Limitations):
- Confirm these are expected behaviors, not bugs

## Step 3: Evidence Check

Focus on what actually happened during the test — did the agent USE the new features, or did it just have them available? Look at:
- Resolved pipeline records: `pipeline-read.sh --stage resolved`
- Journal entries in `agents/<agent>/journal/`
- Reasoning bank entries: `reasoning-bank-read.sh --summary`

   # AUTHORITATIVE CHECK SOURCE: All actively-evaluated checks live here in Step 3.
   # core/config/verification-checklist.md is a comprehensive reference catalog
   # (2000+ items) but is too large to load in one context window. When adding new
   # checks for new features, add them HERE, not in the checklist file.
   # The checklist file remains useful for per-section deep dives via targeted reads.

   # 4-Tier Architecture evidence checks (Section 4T)
   Check: `world/.initialized` exists (init-world.sh ran successfully)
   Check: `world/program.md` is non-empty (The Program — shared purpose defined)
   Check: `<agent>/.initialized` exists (init-agent.sh ran successfully)
   Check: `agents/<agent>/self.md` is non-empty (agent identity defined)
   Check: `$MIND_AGENT` env var is set and matches agent directory name
   Check: `meta/spark-questions.jsonl` exists (moved from old mind/ to meta/)
   Check: `meta/skill-quality.yaml` exists (moved from old mind/ to meta/)
   Check: `meta/evolution-log.jsonl` exists (moved from old mind/ to meta/)
   Check: Knowledge tree lives in `world/knowledge/tree/` (collective, not per-agent)
   Check: Experience records live in `agents/<agent>/experience.jsonl` (per-agent, not world/)
   Check: `core/scripts/_paths.py` exports WORLD_DIR, AGENT_DIR, META_DIR
   Check: `core/scripts/_platform.sh` has `WORLD_DIR="$(cygpath -m "$WORLD_DIR")"` (Windows path fix)
   Check: No `mind/` directory exists (fully migrated to 4-tier)
   # Stray world/ or meta/ at repo root means a script wrote a literal
   # path instead of resolving via local-paths.conf (the canonical
   # external paths). 2026-05-07 cleanup found a 16-day-stale `world/`
   # at repo root alongside the 9.2MB OneDrive canonical — silent
   # divergence, no error surfaced. Bare-string `world/foo` paths
   # collapse to `./world/foo` when _paths.sh isn't sourced; the .gitignore
   # `/world/` and `/meta/` entries hide the divergence from `git status`.
   Bash (no-stray-roots): test ! -d world && test ! -d meta && echo "PASS: world/ and meta/ not at repo root" || echo "FAIL: stray world/ or meta/ at repo root — script wrote without resolving via _paths.sh; canonical paths are in agents/<agent>/local-paths.conf"

   # Context-budget zone anchoring (Section CB — rb-313, guard-301)
   # These checks keep the zone=distance-to-autocompact invariant stable over time.
   # If any fails, see agents/alpha/journal/2026/04/2026-04-19.md "Rebase context-budget zones".
   Check: `core/scripts/context-budget-status.py` `classify_zone` takes `pct_to_autocompact` (NOT `used_pct`) — grep `def classify_zone.pct_to_autocompact` must match
   Check: `core/scripts/context-budget-status.py` reads `CLAUDE_CODE_AUTO_COMPACT_WINDOW` and `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` from env (not hardcoded)
   Check: `core/scripts/context-budget-status.py` writes `env_seen`, `effective_window`, `autocompact_pct`, `autocompact_token_limit`, `headroom_tokens`, `pct_to_autocompact` fields into budget JSON
   Check: `agents/<agent>/session/context-budget.json` contains `env_seen` key (proves statusLine subprocess inherited env vars)
   Check: `core/scripts/context-budget-banner.sh` exists and emits the exact shape `CTX: raw N% | of-autocompact N% | zone WORD | headroom N tokens | env W/P | updated TS`
   Bash: `grep -qE 'CTX: raw .*of-autocompact.*zone.*headroom.*env.*updated' core/scripts/context-budget-banner.sh && echo "PASS: banner.sh source emits 6-field shape" || echo "FAIL: banner shape regressed in source"` — static check on banner.sh source (runs in non-session context where no context-budget.json exists yet; context-citation-audit.sh's banner_re check on line 70 validates the regex-level audit-regex compatibility)
   Check: `core/scripts/context-citation-audit.sh` exists; its `banner_re` matches banner.sh's print format (zone field captured)
   Check: `core/scripts/context-citation-audit.sh` splits journal blocks on `^#+\s` (not `^#\s`) so both `#` and `##` heading styles work
   Check: No hardcoded numeric zone thresholds in `core/config/obligation-schema.yaml`, `core/config/conventions/compact-recovery.md`, `core/config/conventions/working-memory.md`, `core/scripts/reasoning-snapshot.py` — all reference the script as source of truth. Grep `65%\|used_pct >= 65\|>=65%` across these files must return empty.
   Check: guard-301 exists and is active (`reasoning-bank.py guard read --id guard-301` returns a record)
   Check: rb-313 exists and is active (`reasoning-bank.py rb read --id rb-313` returns a record)
   Check: `core/scripts/abbreviated-obligation-audit.py` emits `claim_banner_zone` in its per-claim audit record
   Check: `core/scripts/context-budget-status.py` module docstring contains the `pq-034` design-intent note — prevents future maintainers from "correcting" an intentionally small effective_window. Grep the script for `pq-034` must match.
   # Schema-scope / enforcement-scope alignment (rb-315, guard-303)
   # These two invariants state scope as "per session" / "per claim" — the
   # implementation must iterate at the promised granularity, else the
   # threshold silently never fires / the audit falsely credits claims.
   Check: `core/scripts/obligation-audit.py` counts `session_false_claims` from obligation-audit.jsonl records
   Check: `core/scripts/context-citation-audit.sh` scopes banner search per-OBLIGATION (neighborhood bounded by next claim's start), not per-block. Grep the script for `matches[i + 1].start()` must match.

   # Agent resolution: MIND_AGENT env var is the ONLY mechanism (Section 4T continued)
   # No fallback files, no .active-agent global, no .latest-session-id.
   Check: `core/scripts/_paths.py` `_resolve_agent_name` reads only `os.environ.get("MIND_AGENT")`
   Check: `core/scripts/_paths.sh` resolves `AGENT_NAME="${MIND_AGENT:-}"` (one line, no fallbacks)
   Bash: MIND_AGENT=test-agent source core/scripts/_paths.sh && echo "$AGENT_NAME" → verify prints "test-agent"

   # Inlined-_APD drift detection (rb-1092, g-115-983)
   # AGENTS_PARENT_DIR is inlined at 5 sites for latency/import-cycle reasons
   # (see CLAUDE.md "Agent-dir Resolution" table). Drift between any inlined
   # copy and core/scripts/_paths.sh AGENTS_PARENT_DIR silently re-routes the
   # affected helper to the wrong root: session-state-get.sh would return
   # UNINITIALIZED for an existing agent (canonical rb-1092 incident), which
   # would route /start to the UNINITIALIZED branch and clobber a working
   # agent's state. The /start Step 1.5 drift-warning probe (skill: start)
   # catches this at entry; this check catches it on the verify-learning
   # cadence so drift is surfaced even when no /start re-entry happens.
   Bash (inlined-_APD-drift): canonical=$(grep -E '^AGENTS_PARENT_DIR=' core/scripts/_paths.sh | head -1 | sed -E 's/^AGENTS_PARENT_DIR=//;s/^"//;s/"$//') && fail=0 && for f in core/scripts/session-state-get.sh core/scripts/session-mode-get.sh core/scripts/session-signal-exists.sh core/scripts/cleanup-stale-bindings.sh; do v=$(grep -E '^_APD=' "$f" | head -1 | sed -E 's/^_APD=//;s/^"//;s/"$//'); if [ "$v" != "$canonical" ]; then echo "FAIL: $f _APD=$v != _paths.sh AGENTS_PARENT_DIR=$canonical"; fail=1; fi; done && py=$(grep -E '^_AGENTS_PARENT_DIR' core/scripts/_wake_signals.py | head -1 | sed -E 's/^_AGENTS_PARENT_DIR[[:space:]]*=[[:space:]]*"([^"]*)".*/\1/') && if [ "$py" != "$canonical" ]; then echo "FAIL: core/scripts/_wake_signals.py _AGENTS_PARENT_DIR=$py != _paths.sh AGENTS_PARENT_DIR=$canonical"; fail=1; fi && [ "$fail" = 0 ] && echo "PASS: all 5 inlined _APD/_AGENTS_PARENT_DIR sites match _paths.sh canonical \"$canonical\"" || true

   # Wrapper-test daemon stale-code isolation (commit 5cce91b8; g-115-1328)
   # In-process pytest wrapper daemons freeze git_head_sha at import; a commit
   # landing mid-run trips rt_check_staleness -> one-shot auto-restart, which
   # corrupts wrapper stdout (JSONDecodeError) — the ~70-failure incident
   # (2026-06-02). conftest.py pre-sets RT_STALENESS_WARNED=1 to short-circuit
   # _runtime.sh:169 before the restart arms. Guard against silent removal.
   # Full narrative: tree node daemon-lifecycle-windows "Test-Side Mirror".
   Bash (conftest-staleness-isolation): grep -q 'RT_STALENESS_WARNED' mind_api/tests/conftest.py && echo "PASS: conftest.py sets RT_STALENESS_WARNED (wrapper-test daemon stale-code isolation, commit 5cce91b8)" || echo "FAIL: RT_STALENESS_WARNED missing from mind_api/tests/conftest.py — the 74-failure wrapper-test daemon-staleness fix was removed (see g-115-1328, tree: daemon-lifecycle-windows)"

   # Generated-Checklist Verify Step (Section GCV — g-306-03 / BRD Gap 15, 2026-06-12)
   # aspirations-verify Q1.5 generates a 5-10 item yes/no checklist from the goal
   # and logs UNCOVERED gaps (failing items the goal's own verification never
   # declared) to meta/missing-verification-criteria.jsonl for later goal-template
   # improvement. Evidence: TICKing All the Boxes (2410.03608). Conservative
   # escalation — covered-criterion failures fail Q1; uncovered failures only log
   # (low-risk additive step). These checks pin the step + its companion logger.
   Bash (gcv-step-present): grep -q 'Q1.5 GENERATED CHECKLIST' .claude/skills/aspirations-verify/SKILL.md && echo "PASS: aspirations-verify has the Q1.5 generated-checklist step (BRD Gap 15)" || echo "FAIL: aspirations-verify SKILL.md lost the Q1.5 GENERATED CHECKLIST step (g-306-03 regressed)"
   Bash (gcv-logger-wired): grep -q 'missing-criteria-log.sh' .claude/skills/aspirations-verify/SKILL.md && echo "PASS: Q1.5 wires missing-criteria-log.sh for uncovered-gap recording" || echo "FAIL: aspirations-verify Q1.5 no longer calls missing-criteria-log.sh — uncovered checklist gaps go unrecorded (g-306-03)"
   Bash (gcv-logger-exists): test -f core/scripts/missing-criteria-log.sh && test -f core/scripts/missing-criteria-log.py && git ls-files --error-unmatch core/scripts/missing-criteria-log.py >/dev/null 2>&1 && echo "PASS: missing-criteria-log.{sh,py} exist + git-tracked" || echo "FAIL: missing-criteria-log companion script missing/untracked (g-306-03 lost-deliverable class; restore + git add)"
   Bash (gcv-digest-ref): grep -q 'Q1.5 GENERATED CHECKLIST' core/config/iteration-close-digest.md && echo "PASS: iteration-close-digest § VERIFY references Q1.5" || echo "FAIL: iteration-close-digest.md § VERIFY lost the Q1.5 item (g-306-03)"
   Bash (gcv-regression-test): test -f core/scripts/tests/test_missing_criteria_log.py && git ls-files --error-unmatch core/scripts/tests/test_missing_criteria_log.py >/dev/null 2>&1 && N=$(py -3 -m pytest core/scripts/tests/test_missing_criteria_log.py -o addopts= 2>&1 | grep -oE '[0-9]+ passed' | grep -oE '[0-9]+' | head -1) && [ -n "$N" ] && [ "$N" -ge 10 ] && echo "PASS: missing-criteria-log regression test exists + git-tracked + $N cases passing (>=10)" || echo "FAIL: missing-criteria-log regression test missing/untracked OR <10 passing (g-306-03; restore test + git add, or if the appender contract changed update the test + this check)"

   # Gap-16 never-summarize-summaries verifier (g-306-01, guard-745). The
   # consolidate-source-verify.sh fail-loud guard stops aspirations-consolidate
   # from regenerating any summary level FROM a prior summary file (GSD 2.0 /
   # BRD Gap 16). These checks pin the script, its fail-loud + raw-pass
   # behavior, the consolidate Step 0.6 wiring, and the guardrail. Synthetic
   # paths only — the verifier is a pure path/name classifier (the files need
   # not exist).
   Bash (gap16-verifier-exists): test -f core/scripts/consolidate-source-verify.sh && git ls-files --error-unmatch core/scripts/consolidate-source-verify.sh >/dev/null 2>&1 && echo "PASS: consolidate-source-verify.sh exists + git-tracked" || echo "FAIL: consolidate-source-verify.sh missing/untracked (g-306-01 lost-deliverable class; restore + git add)"
   Bash (gap16-fail-loud): bash core/scripts/consolidate-source-verify.sh "smoke/handoff.yaml" >/dev/null 2>&1; [ $? -eq 1 ] && echo "PASS: verifier fails loud (exit 1) on a summary artifact — Gap-16 fail-loud path exercised" || echo "FAIL: consolidate-source-verify.sh did NOT exit 1 on a summary path — fail-loud path broken (g-306-01)"
   Bash (gap16-raw-passes): bash core/scripts/consolidate-source-verify.sh "smoke/experience.jsonl" >/dev/null 2>&1; [ $? -eq 0 ] && echo "PASS: verifier passes (exit 0) a raw source — no false positive" || echo "FAIL: consolidate-source-verify.sh wrongly rejected a raw source path (g-306-01)"
   Bash (gap16-consolidate-wired): grep -q 'consolidate-source-verify.sh' .claude/skills/aspirations-consolidate/SKILL.md && echo "PASS: aspirations-consolidate Step 0.6 wires the Gap-16 verifier" || echo "FAIL: aspirations-consolidate lost the Step 0.6 source-integrity verifier (g-306-01 regressed)"
   Bash (gap16-guardrail): bash core/scripts/guardrails-read.sh --category framework-patterns 2>/dev/null | grep -q 'guard-745' && echo "PASS: guard-745 (never-summarize-summaries) registered" || echo "FAIL: guard-745 missing from guardrails (g-306-01 deliverable 1)"

   # Gate D Step 5e outside the trivial_mode skip region (g-115-1414, GATE-INTEGRITY).
   # Lightweight mode (g-305-15) added an IF-trivial_mode SKIP region around Phase-4
   # retrieval (Steps 1-5d) in BOTH core/config/execute-protocol-digest.md and
   # .claude/skills/aspirations-execute/SKILL.md. INVARIANT: Step 5e (Gate D) MUST
   # stay OUTSIDE that region and always run; the classifier never reads/infers the
   # arm. Both files position the sentinel line "Step 5e ALWAYS RUNS — NEVER gated by
   # trivial_mode" immediately BEFORE the "Step 5e: Gate D commons-pattern injection"
   # section header. The two checks below pin that line-order in each file (structure-
   # agnostic): if a future edit pulls Step 5e into the skip region, the sentinel
   # disappears or moves after the header → FAIL. If the marker wording is
   # deliberately reworded, update the grep pattern here in lockstep.
   Bash (gate-d-5e-outside-skip-digest): F=core/config/execute-protocol-digest.md; S=$(grep -nE 'Step 5e ALWAYS RUNS' "$F" | head -1 | cut -d: -f1); H=$(grep -nE 'Step 5e: Gate D commons-pattern injection' "$F" | head -1 | cut -d: -f1); { [ -n "$S" ] && [ -n "$H" ] && [ "$S" -lt "$H" ]; } && echo "PASS: execute-protocol-digest.md positions 'Step 5e ALWAYS RUNS' (L$S) before the Step 5e Gate D section (L$H) — Step 5e is outside the trivial_mode skip region (g-115-1414)" || echo "FAIL: execute-protocol-digest.md no longer positions the 'Step 5e ALWAYS RUNS — never gated by trivial_mode' sentinel before the Step 5e Gate D section — Step 5e may have been pulled into the g-305-15 trivial_mode skip region (GATE-INTEGRITY regression, g-115-1414)"
   Bash (gate-d-5e-outside-skip-skill): F=.claude/skills/aspirations-execute/SKILL.md; S=$(grep -nE 'Step 5e ALWAYS RUNS' "$F" | head -1 | cut -d: -f1); H=$(grep -nE 'Step 5e: Gate D commons-pattern injection' "$F" | head -1 | cut -d: -f1); { [ -n "$S" ] && [ -n "$H" ] && [ "$S" -lt "$H" ]; } && echo "PASS: aspirations-execute/SKILL.md positions 'Step 5e ALWAYS RUNS' (L$S) before the Step 5e Gate D section (L$H) — Step 5e is outside the trivial_mode skip region (g-115-1414)" || echo "FAIL: aspirations-execute/SKILL.md no longer positions the 'Step 5e ALWAYS RUNS — never gated by trivial_mode' sentinel before the Step 5e Gate D section — Step 5e may have been pulled into the g-305-15 trivial_mode skip region (GATE-INTEGRITY regression, g-115-1414)"

   # Micro-hypothesis resolves_when/consumer + auto-settle wiring (g-303-34, g-115-1665).
   # g-303-34 made resolves_when + consumer REQUIRED at the micro-hyp filing sites and
   # added the Step 1.5 auto-settle CONSUMER, fixing the dominant micro-hyp failure
   # (zeta audit g-303-14: 71% never-resolve, 0% consumed). These are behavior-changing
   # pseudocode edits a future edit could silently drop. The three checks pin: (1) the
   # aspirations-spark sq-016 filing JSON still carries BOTH fields, (2) the reflect-on-
   # outcome Step 1.5 consumer header still exists, (3) the sq-016 RB block still stamps
   # source_horizon: micro (rb-876 first-principles attribution-gap fix). If the wording
   # is deliberately reworded, update the grep pattern here in lockstep.
   Bash (microhyp-spark-resolves-consumer): F=.claude/skills/aspirations-spark/SKILL.md; grep -qE '"source_step":"sq-016".*"resolves_when"' "$F" && grep -qE '"source_step":"sq-016".*"consumer"' "$F" && echo "PASS: aspirations-spark sq-016 micro-hyp filing JSON carries both resolves_when and consumer (g-303-34 non-resolution/no-consumer fix)" || echo "FAIL: aspirations-spark sq-016 micro-hyp filing lost resolves_when and/or consumer -- the g-303-34 REQUIRED-fields fix regressed (71%-never-settle / 0%-consumed failure mode returns)"
   Bash (microhyp-autosettle-header): grep -q 'Auto-Settle Unresolved Micro-Hypotheses' .claude/skills/reflect-on-outcome/SKILL.md && echo "PASS: reflect-on-outcome has the Step 1.5 Auto-Settle Unresolved Micro-Hypotheses consumer (g-303-34)" || echo "FAIL: reflect-on-outcome lost the Step 1.5 Auto-Settle Unresolved Micro-Hypotheses step -- resolves_when is filed but never consumed (g-303-34 regressed)"
   Bash (microhyp-spark-source-horizon): grep -q 'source_horizon: micro' .claude/skills/aspirations-spark/SKILL.md && echo "PASS: aspirations-spark sq-016 RB block stamps source_horizon: micro (g-303-34 / rb-876 first-principles-spark attribution-gap fix)" || echo "FAIL: aspirations-spark sq-016 RB block lost the source_horizon: micro stamp -- first-principles micro-horizon RB origin no longer traceable (g-303-34 / rb-876 regressed)"

   # _world_config Mode G overlay loader (rb-1100, guard-590, Phase 2.5.D)
   # When _world_config.py used the pre-relocation root/agent/local-paths.conf
   # path (Mode G), it silently fell through to PROJECT_ROOT/world (nonexistent
   # in canonical layout) and returned None. load_world_config swallowed the
   # None as a dict(default) fallback, causing 25 routing-table-empty failures
   # for ~3 weeks until pytest collection collision surfaced the regression.
   # These two checks catch the same drift class at verify-learning cadence:
   # (1) _resolve_world_dir() returns non-None Path for a bound agent
   # (2) capability-routing overlay loads non-empty title_prefix_routes
   # rb-1100 calls this "silent safe-default degradation = sync-invariant audit missed a site".
   Check: `core/scripts/_world_config.py` `_resolve_world_dir()` returns non-None Path for a bound agent (Mode G regression — Phase 2.5.D missed sync site)
   Bash (world-config-resolves): MIND_AGENT=alpha py -3 -c "import sys; sys.path.insert(0, 'core/scripts'); from _world_config import _resolve_world_dir; p = _resolve_world_dir(); assert p is not None, 'FAIL: _resolve_world_dir() returned None (Mode G regression — silently falls through to PROJECT_ROOT/world)'; print(f'PASS: _world_config._resolve_world_dir() returned {p}')"
   Check: `core/scripts/_world_config.py` `load_world_config('capability-routing')` returns non-empty title_prefix_routes (overlay file load works end-to-end)
   Bash (capability-routing-non-empty): MIND_AGENT=alpha py -3 -c "import sys; sys.path.insert(0, 'core/scripts'); from _world_config import load_world_config; cfg = load_world_config('capability-routing'); routes = cfg.get('title_prefix_routes', {}) if cfg else {}; assert len(routes) > 0, 'FAIL: title_prefix_routes is empty — overlay loader silently degraded (rb-1100)'; print(f'PASS: capability-routing overlay loaded {len(routes)} title_prefix_routes')"

   # Hardcoded AGENTS_PARENT_DIR literal audit (g-115-1009, source: exp-encode-
   # session-2026-05-20-world-config-mode-g). Static-pattern companion to the
   # Mode G dynamic checks above. Inlined-_APD-drift guards the 5 documented
   # sync sites; the Mode G checks above probe runtime behavior of one loader;
   # THIS check guards completeness across the WHOLE Python tree — any Python
   # file silently doing `PROJECT_ROOT / "agents"` instead of routing through
   # agents_root() is a latent Phase-2.5.D regression that neither sibling
   # catches. Canonical example was context-budget-status.py:80, fixed in
   # g-115-1009 Part 1. Extracts canonical value from _paths.py dynamically
   # (not hardcoded "agents") so future AGENTS_PARENT_DIR relocations don't
   # re-introduce a false-pass when the literal-name changes.
   Bash (hardcoded-APD-literal): canonical=$(grep -E '^AGENTS_PARENT_DIR\s*=' core/scripts/_paths.py | head -1 | sed -E 's/.*=\s*"([^"]*)".*/\1/') && hits=$(grep -rEn "\bPROJECT_ROOT\s*/\s*\"${canonical}\"" core/scripts/*.py mind_api/src/*.py 2>/dev/null | grep -v '_paths.py' || true) && if [ -n "$hits" ]; then echo "FAIL: hardcoded PROJECT_ROOT / \"${canonical}\" outside _paths.py — use agents_root() helper"; echo "$hits"; else echo "PASS: no hardcoded PROJECT_ROOT / \"${canonical}\" outside _paths.py canonical declaration"; fi

   # Skill-discovery invocation-source glob-routing (g-115-1405, source: g-115-1403
   # skill-discovery 13-flagged audit). Sibling to hardcoded-APD-literal above but
   # for the GLOB surface: skill-discovery's journal + execution-diary invocation
   # sources MUST sweep agents_root()/ctx.paths.agents_root.glob("*/...") (DEPTH 2,
   # post-Phase-2.5.D), NEVER PROJECT_ROOT.glob("*/...") at DEPTH 1 — depth-1 matches
   # 0 files under agents/<name>/, silently zeroing 2 of 4 invocation sources for
   # EVERY skill and inflating silently_undertriggering (the under-logging defense
   # built by g-115-879/rb-314/g-115-798 is itself broken). Empirically 2026-06-11:
   # legacy '*/journal.jsonl' matched 0; 'agents/*/journal.jsonl' matched 6.
   # Asserts BOTH absence of the depth-1 bug pattern AND presence of the 4 correct
   # agents_root() globs (2 CLI + 2 daemon) — no brittle line anchors (rb-682,
   # tree node verify-learning-citation-drift). Quote-agnostic.
   Bash (skill-discovery-glob-routing): bug=$(grep -nE "(PROJECT_ROOT|project_root|ctx\.paths\.project_root)\.glob\(['\"]\*/(journal\.jsonl|session/execution-diary\.jsonl)['\"]\)" core/scripts/skill-discovery.py mind_api/src/endpoints/skill_discovery.py 2>/dev/null || true); ok_cli=$(grep -cE "agents_root\(\)\.glob\(['\"]\*/(journal\.jsonl|session/execution-diary\.jsonl)['\"]\)" core/scripts/skill-discovery.py 2>/dev/null || echo 0); ok_daemon=$(grep -cE "ctx\.paths\.agents_root\.glob\(['\"]\*/(journal\.jsonl|session/execution-diary\.jsonl)['\"]\)" mind_api/src/endpoints/skill_discovery.py 2>/dev/null || echo 0); if [ -n "$bug" ]; then echo "FAIL: depth-1 PROJECT_ROOT.glob('*/...') invocation-source residue (agents/ relocation zeroes journal+diary sources for all skills)"; echo "$bug"; elif [ "$ok_cli" -lt 2 ] || [ "$ok_daemon" -lt 2 ]; then echo "FAIL: expected agents_root() journal+diary globs missing (cli=$ok_cli/2 daemon=$ok_daemon/2)"; else echo "PASS: skill-discovery invocation-source globs route through agents_root()/ctx.paths.agents_root (cli=$ok_cli daemon=$ok_daemon, no depth-1 residue)"; fi

   # Skill-coinvocation-discovery ledger glob-routing (g-304-24, sibling to
   # skill-discovery-glob-routing above). skill-coinvocation-discovery.py mines the
   # cross-agent ledger via base.glob("*/skill-invocations.jsonl") where base defaults
   # to agents_root() (DEPTH 2, post-Phase-2.5.D) — NEVER a depth-1 PROJECT_ROOT.glob
   # ("*/...") which matches 0 files under agents/<name>/ and silently zeroes every
   # co-invocation candidate. This consumer is invisible to the three CLAUDE.md audit
   # greps (constant/literal/.parent), so the CLAUDE.md cross-agent glob consumers
   # table row + this regression guard are its only audit surface. Asserts absence of
   # the depth-1 bug AND presence of the agents_root()-routed ledger glob.
   # Quote-agnostic, no brittle line anchors (rb-682).
   Bash (skill-coinvocation-glob-routing): bug=$(grep -nE "(PROJECT_ROOT|project_root)\.glob\(['\"]\*/skill-invocations\.jsonl['\"]\)" core/scripts/skill-coinvocation-discovery.py 2>/dev/null || true); ok_glob=$(grep -cE "\.glob\(['\"]\*/skill-invocations\.jsonl['\"]\)" core/scripts/skill-coinvocation-discovery.py 2>/dev/null || echo 0); ok_root=$(grep -cE "agents_root\(\)" core/scripts/skill-coinvocation-discovery.py 2>/dev/null || echo 0); if [ -n "$bug" ]; then echo "FAIL: depth-1 PROJECT_ROOT.glob('*/skill-invocations.jsonl') residue (agents/ relocation zeroes all co-invocation candidates)"; echo "$bug"; elif [ "$ok_glob" -lt 1 ] || [ "$ok_root" -lt 1 ]; then echo "FAIL: expected agents_root()-routed skill-invocations glob missing (glob=$ok_glob root=$ok_root)"; else echo "PASS: skill-coinvocation-discovery ledger glob routes through agents_root() (glob=$ok_glob root=$ok_root, no depth-1 residue)"; fi

   # Skill-freshness-report integration smoke (Section SFR — g-115-1552, source: g-304-14
   # sq-018). Sibling to the two skill-telemetry checks above, but a RUN/smoke check (not a
   # glob-routing grep): skill-freshness-report.py is a standalone Layer-5d report
   # cross-referencing each .claude/skills/*/SKILL.md mtime against its last cross-agent
   # skill-invocations.jsonl invocation, splitting skills into stale_modified / fresh_stable
   # / never_invoked_in_window cohorts. The 16 hermetic tests (test_skill_freshness_report.py)
   # cover its cohort logic on synthetic fixtures but NEVER run it against the real repo, so a
   # _paths import change, a real-data edge case, or a SKILL.md front-matter shape change would
   # crash the live report while the unit tests stay green. This smoke check runs it against the
   # real SKILL.md mtimes + real cross-agent ledger and asserts BOTH exit 0 AND the two ALERT
   # cohort keys (stale_modified, fresh_stable) are present in the JSON. No brittle line anchors.
   Bash (skill-freshness-report-smoke): out=$(py -3 core/scripts/skill-freshness-report.py --output json 2>/dev/null); rc=$?; echo "$out" | py -3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if all(k in d for k in ('stale_modified','fresh_stable')) else 1)"; krc=$?; if [ "$rc" = 0 ] && [ "$krc" = 0 ]; then echo "PASS: skill-freshness-report.py --output json runs (exit 0) and emits stale_modified + fresh_stable cohorts"; else echo "FAIL: skill-freshness-report.py smoke check (exit=$rc keys_present_rc=$krc) — g-304-14 Layer-5d standalone report broken; check _paths import drift, real-data edge case, or SKILL.md front-matter shape change (regression class the 16 hermetic tests miss)"; fi

   # Tree-read --summary projection field-carry (Section TSP — g-115-1409, source: g-115-1408
   # strategic-scan-S2-silently-dead incident). Mirror-sync invariant for a SECOND projection
   # surface (sibling to skill-discovery-glob-routing above): the --summary node projection is
   # built in TWO mirrored places — core/scripts/tree.py (read --summary CLI) and
   # mind_api/src/world/tree_read.py (daemon /v1/tree/read summary). Both MUST emit
   # last_updated + article_count per node; dropping either field in either path silently
   # zeroes strategic-scan S2a (stale-node) + S2b (thin-node) knowledge-frontier detection
   # (g-115-1408 found S2 dead for exactly this reason — the fields existed on every node but
   # the projection omitted them). Asserts the projection-dict literal ("<key>": node.get("<key>"))
   # carries BOTH keys in BOTH files — quote-agnostic, no brittle line anchors (rb-682, tree
   # node verify-learning-citation-drift).
   Bash (tree-summary-projection-fields): fail=0; for f in core/scripts/tree.py mind_api/src/world/tree_read.py; do for k in last_updated article_count; do grep -qE "[\"']${k}[\"'][[:space:]]*:[[:space:]]*node\.get\([[:space:]]*[\"']${k}[\"']" "$f" || { echo "FAIL: $f --summary projection missing '${k}' key (strategic-scan S2a/S2b go dead — g-115-1408/g-115-1409)"; fail=1; }; done; done; [ "$fail" = 0 ] && echo "PASS: tree-read --summary projection carries last_updated + article_count in both paths (tree.py CLI + tree_read.py daemon)" || true

   # Bash-agent-inject hook evidence checks (Section 4T continued)
   # The PreToolUse[Bash] hook auto-injects MIND_AGENT from .active-agent-<SID> so
   # the LLM no longer needs to prefix every Bash call manually.
   Check: `core/scripts/bash-agent-inject.sh` and `core/scripts/bash-agent-inject.py` exist
   Check: `.claude/settings.json` has a PreToolUse hook with `matcher: "Bash"` pointing at `bash-agent-inject.sh`
   Check: `.claude/settings.json` has NO FileChanged hook for `.active-agent-*` (retired stanza) and `core/scripts/set-active-agent-env.sh` has been deleted
   Bash (hook-works): bash core/scripts/session-state-get.sh → verify output is RUNNING/IDLE/NO_AGENT for the currently-bound agent (if binding exists). A literal "NO_AGENT" output while `.active-agent-<current-SID>` contains a valid agent name means the hook is not wired up or not stamping correctly.
   Check: `core/scripts/bash-agent-inject.sh` sources both `_paths.sh` and `_platform.sh` (without _platform.sh, MSYS may hand python3 a /c/... path it cannot open — same rule as guard-051 applies)
   Bash (no-fallback-chain): grep -E '\|\|\s*(py|python)( |$)' core/scripts/*.sh → verify returns empty (no hook script uses python3||py||python fallback; single source of truth via _paths.sh shim)
   Bash (python-form-consistency): grep -E "(^|[^A-Za-z0-9_])py -(3|c)( |$)|\| py -(3|c) |exec py( |$)|[^A-Za-z0-9_]py - |[^A-Za-z0-9_]py \"\\\$" core/scripts/*.sh core/scripts/tests/*.sh → verify returns empty (inside .sh wrappers that source _paths.sh, use `python3` — never `py -3`, `py -c`, bare `py <file>`, `py -` stdin heredoc, or `py "$VAR"`; rb-471 audit-scope discipline companion to rb-370 — the five branches enumerate every legacy invocation variant so narrow greps do not mask scope)
   Bash (fail-open): echo 'not json' | bash core/scripts/bash-agent-inject.sh; echo "exit=$?" → verify exit=0 and empty stdout (malformed stdin must not block Bash tool)
   Bash (override-preserved): echo '{"session_id":"test","tool_input":{"command":"MIND_AGENT=alpha pwd"}}' | bash core/scripts/bash-agent-inject.sh → verify empty stdout (explicit MIND_AGENT= at command boundary must skip injection so cross-agent probes work)

   # Bash-inject diagnostic surface (rb-514, 2026-04-25)
   # When binding resolution returns no agent AND no explicit MIND_AGENT= override,
   # the hook surfaces the silent-no-injection failure class via a greppable artifact.
   # Without this surface, NO_AGENT sessions made silent no-op Bash calls invisible —
   # the diagnostic turns the silent failure class into one that grep can find.
   Check: `core/scripts/bash-agent-inject.py` defines `_log_binding_miss_once` — grep for `def _log_binding_miss_once` must match
   # 2026-05-19 (plan v1 step 0.13-0.14): both writers relocated from PROJECT_ROOT/
   # to core/logs/ (already-gitignored telemetry sink hosting watchdog logs).
   # Check confirms the writers point at the canonical sink, not the legacy root paths.
   Bash (writer-sink): grep -q '"core" / "logs"' core/scripts/bash-agent-inject.py && grep -q 'logs_dir / "bash-inject-sentinels"' core/scripts/bash-agent-inject.py && grep -q 'logs_dir / "bash-inject-misses.jsonl"' core/scripts/bash-agent-inject.py && echo "PASS: writers point at core/logs/" || echo "FAIL: writers still target PROJECT_ROOT — relocation regressed"
   Bash (helper-invoked): grep -qE '_log_binding_miss_once\(sid' core/scripts/bash-agent-inject.py && echo "PASS: helper invoked from main()" || echo "FAIL: helper defined but not called"

   # Bash-inject miss-rate surfacing (g-115-897, paired Apply of g-115-795)
   # Operational mitigation for the architecturally-locked hook miss modes
   # (timeout fail-open + no-binding). The injector cannot be made more
   # robust without violating the PreToolUse fail-open contract OR
   # `_paths.py`'s "one path, no fallbacks" design, so this check surfaces
   # the rate of misses instead of trying to prevent them. Tunable thresholds:
   # MISS_THRESHOLD_ADVISORY=5 (default), MISS_THRESHOLD_ALERT=20, MISS_WINDOW_HOURS=24.
   Check: `core/scripts/bash-inject-misses-recent.sh` exists and is executable
   Bash (miss-rate): bash core/scripts/bash-inject-misses-recent.sh → verify JSON output containing `verdict` field; report the verdict (clean PASS / advisory WARN / alert FAIL) — alert verdict means >=20 misses in 24h, suggests a session-binding misconfiguration worth investigating

   # Schema-on-error CLI tool conformance (Section SOE — rb-512, 2026-04-25)
   # RETIRED in H2 Wave 2 (2026-05-15): reasoning-bank.py CLI was gutted —
   # RB_ADD_SCHEMA_TEXT, GUARD_ADD_SCHEMA_TEXT, argparse, --schema flag, and
   # all CLI subcommands removed. The record contract now lives in
   # mind_api/src/store_registry.py + mind_api/src/endpoints/store.py.
   # journal.py was previously retired in H2 Wave 1 (2026-05-15).
   # No stdin-JSON CLI tools with the rb-512 pattern remain.
   # Section SOE checks are no longer applicable — all stores route through
   # the daemon generic store endpoint.

   # Session Manifest Integrity (Section SMI — asp-248, 2026-04-23)
   # Guards against the Phase -1.5 drift pattern: pseudocode that carries a
   # hardcoded allowlist parallel to an authoritative config file. Single
   # source of truth is core/config/session-manifest.yaml. Scripts consume
   # the manifest; pseudocode must call scripts, not inline the list.
   Check: `core/config/session-manifest.yaml` contains a top-level `files:` list (authoritative)
   Bash (no-parallel-allowlist): grep -nE '^\s*(WHITELISTED|VALID_SESSION_FILES|SESSION_FILE_ALLOWLIST)\s*=\s*[\(\[]' .claude/skills/boot/SKILL.md .claude/skills/start/SKILL.md .claude/skills/stop/SKILL.md core/scripts/*.sh → verify returns empty (no pseudocode or script carries a hardcoded parallel list; manifest is single source of truth — guard-426)
   # Orphan ratchet (g-254-04, 2026-04-26): Replaces the previous hardcoded count<=2
   # threshold which silently passed because session-desync-check.sh emits JSON warnings,
   # not '[info] orphan:' lines, so the grep matched zero. The ratchet invokes
   # session-manifest-orphan-ratchet.sh which subprocess-spawns session_snapshot.py,
   # counts orphans[], compares to meta/audit-baselines.yaml session_manifest_orphans.baseline,
   # and emits a verdict (seeded/stable/ratcheted/regressed). The ratchet is ADVISORY by
   # default (exit 0 always) — set VERIFY_LEARNING_ORPHAN_HARD_GATE=1 to make regression
   # exit non-zero. The baseline auto-seeds on first run and ratchets DOWN on monotonic
   # improvement, mirroring learning-routing-ratchet.py's pattern.
   Bash (orphan-ratchet): bash core/scripts/session-manifest-orphan-ratchet.sh --json | py -3 -c "import json,sys; d=json.load(sys.stdin); v=d.get('verdict'); ok = v in ('seeded','stable','ratcheted'); print(f\"{'PASS' if ok else 'FAIL'}: orphan-ratchet verdict={v} current={d['current']} baseline={d['baseline']}\")" → PASS unless verdict=regressed (current count exceeds baseline; either register the new orphan in core/config/session-manifest.yaml or file a Maintain goal to remove it)
   Check: meta/audit-baselines.yaml has a `session_manifest_orphans` entry with baseline + history (created on first ratchet run; auto-ratchets down as orphans get registered/removed)
   Check: `.claude/skills/boot/SKILL.md` Phase -1.5 calls `session-desync-check.sh` (advisory, does NOT delete) — NOT a `for F in a b c; do rm -f ...; done` loop. Grep Phase -1.5 for `rm -f` must return empty.
   # Pair-consumer entry-type coverage (Section SMI continued — g-115-416, 2026-05-08)
   # session-manifest.yaml entries fall into 3 entry-type combinations: regular file
   # (default), glob:true, type:dir. Both consumers (session_snapshot.py main()
   # and session-manifest-clear.sh inline-Python) MUST have a handler branch for
   # each type present in the manifest. guard-477 was the LLM-side enforcement;
   # session-manifest-coverage-audit.sh catches the same drift mechanically.
   Bash (pair-consumer-coverage): bash core/scripts/session-manifest-coverage-audit.sh → must exit 0 and print "PASS: every distinct entry-type" (FAIL means a consumer dropped a branch while manifest still has entries of that type, or manifest introduced a new type with no consumer wiring)

   # Third-consumer entry-type coverage — session-manifest-write-gate.py (g-115-861, 2026-05-17)
   # session-manifest-coverage-audit.sh above scopes the pair: session_snapshot.py main()
   # and session-manifest-clear.sh inline-Python. session-manifest-write-gate.py is the
   # THIRD consumer (g-115-840 added the type:dir dispatch block). This check pins the
   # gate's dispatch contract independently — if a new type:* value lands in the manifest
   # without a corresponding entry_type == "<value>" branch in the write-gate, dir-type
   # writes fall back silently to the unregistered-dispatch path. Distinct from the
   # 9-case test suite at mind_api/tests/test_session_manifest_write_gate.py (those
   # verify behavior; this is a structural-completeness pin).
   Bash (write-gate-type-coverage): MISS=$(grep -hE "^[[:space:]]+type:[[:space:]]+\w+" core/config/session-manifest.yaml | sed -E "s/^[[:space:]]+type:[[:space:]]+//; s/[[:space:]]+$//" | sort -u | grep -vE "^(file|)$" | while read t; do grep -qE "entry_type[[:space:]]*==[[:space:]]*\"$t\"" core/scripts/session-manifest-write-gate.py 2>/dev/null || echo "$t"; done); if [ -z "$MISS" ]; then echo "PASS: session-manifest-write-gate.py has dispatch branch for every distinct manifest type"; else echo "FAIL: session-manifest-write-gate.py missing dispatch for type(s): $MISS"; fi

   # Agent Watchdog lifecycle (Section MON — 2026-05-12)
   # Per-agent observability probes invoked as a periodic tick from
   # iteration-close.sh productivity-check. agent-watchdog.py hosts a Probe
   # registry (running-sid, heartbeat, background-job, stop-hook-block) with
   # state persisted across ticks in agents/<agent>/session/watchdog-prev-state.json.
   # The earlier daemon model was retired because nohup+disown didn't
   # reliably detach on Git Bash for Windows — see file's top-level docstring
   # for the rationale. The invariants below catch the most likely drift
   # modes: missing script, missing manifest entry, missing iteration-close
   # tick wiring, retired daemon files lingering.
   Check: `core/scripts/agent-watchdog.py` exists and contains the `build_probes(ctx)` registry call
   Check: `core/scripts/agent-watchdog.sh` exists (wrapper sources `_paths.sh` and execs python — used for ad-hoc human inspection)
   Check: Retired files DO NOT exist: `core/scripts/watchdog-start.sh`, `core/scripts/watchdog-stop.sh`, `core/scripts/watchdog-status.sh`, `core/scripts/_watchdog_lifecycle.py`, `core/scripts/running-sid-watcher.py`, `core/scripts/running-sid-watcher.sh`, `core/logs/running-sid-watcher.jsonl` (all superseded by agent-watchdog --tick — keeping daemon-era artifacts is the regression this section guards against)
   Bash (retired-watcher-gone): test ! -f core/scripts/running-sid-watcher.py && test ! -f core/scripts/running-sid-watcher.sh && test ! -f core/logs/running-sid-watcher.jsonl && echo "PASS: retired running-sid-watcher files absent" || echo "FAIL: superseded running-sid-watcher artifact lingering — agent-watchdog replaces it"
   Bash (retired-daemon-scripts-gone): test ! -f core/scripts/watchdog-start.sh && test ! -f core/scripts/watchdog-stop.sh && test ! -f core/scripts/watchdog-status.sh && test ! -f core/scripts/_watchdog_lifecycle.py && echo "PASS: retired watchdog daemon scripts absent" || echo "FAIL: daemon-era watchdog scripts still present — agent-watchdog --tick replaces them"
   Bash (manifest-watchdog-entry): grep -qE "^\s+- file: watchdog-prev-state\.json\s*$" core/config/session-manifest.yaml && echo "PASS: manifest registers watchdog-prev-state.json" || echo "FAIL: session-manifest.yaml missing watchdog-prev-state.json entry"
   Bash (manifest-watchdog-cleanup): ! grep -qE "^\s+- file: watchdog\.(pid|log)\s*$" core/config/session-manifest.yaml && echo "PASS: manifest no longer references retired watchdog.pid/log" || echo "FAIL: session-manifest.yaml still references retired daemon files"
   Bash (iteration-close-wires-tick): grep -q "agent-watchdog.py.*--tick" core/scripts/iteration-close.sh && echo "PASS: iteration-close.sh invokes agent-watchdog.py --tick" || echo "FAIL: iteration-close.sh does not invoke the watchdog tick"
   Bash (no-start-spawn): ! grep -q "watchdog-start.sh" .claude/skills/start/SKILL.md && echo "PASS: /start no longer spawns a watchdog daemon" || echo "FAIL: /start still references retired watchdog-start.sh"
   Bash (no-stop-kill): ! grep -q "watchdog-stop.sh" .claude/skills/aspirations-graceful-stop/SKILL.md && echo "PASS: graceful-stop no longer kills a watchdog daemon" || echo "FAIL: aspirations-graceful-stop still references retired watchdog-stop.sh"
   Bash (no-recovery-stop): ! grep -q "watchdog-stop.sh" core/scripts/recovery-gate.sh && echo "PASS: recovery-gate.sh no longer references retired watchdog-stop.sh" || echo "FAIL: recovery-gate.sh still references retired watchdog-stop.sh"
   Bash (tick-smoketest): LOG=core/logs/watchdog-alpha.jsonl; BEFORE=$(wc -l < "$LOG" 2>/dev/null | tr -d ' ' || echo 0); STATE=agents/alpha/session/watchdog-prev-state.json; touch agents/alpha/session/running-session-id 2>/dev/null; MIND_AGENT=alpha py -3 core/scripts/agent-watchdog.py --tick >/dev/null 2>&1; AFTER=$(wc -l < "$LOG" 2>/dev/null | tr -d ' ' || echo 0); test -f "$STATE" && echo "PASS: tick wrote state file" || echo "FAIL: tick did not write $STATE"
   Bash (tick-emits-on-transition): MIND_AGENT=alpha py -3 core/scripts/agent-watchdog.py --tick >/dev/null 2>&1; LOG=core/logs/watchdog-alpha.jsonl; BEFORE=$(wc -l < "$LOG" 2>/dev/null | tr -d ' ' || echo 0); touch agents/alpha/session/running-session-id 2>/dev/null; MIND_AGENT=alpha py -3 core/scripts/agent-watchdog.py --tick >/dev/null 2>&1; AFTER=$(wc -l < "$LOG" 2>/dev/null | tr -d ' ' || echo 0); test "$AFTER" -gt "$BEFORE" && echo "PASS: tick emitted transition event on mtime change (before=$BEFORE after=$AFTER)" || echo "FAIL: tick did not detect mtime transition"

   # Cross-Agent Attribution Deliverable Integrity (Section CAA — g-115-774, 2026-05-15)
   # g-115-741 found g-115-714's deliverables (_cross_agent_attribution_filter.py +
   # its test) missing from disk AND never git-committed despite g-115-714
   # deep-closing. post-state-update-gate.sh:122 fail-open meant cross-agent
   # attribution silently no-op'd ~37h with no surfaced error. This guards the
   # lost-deliverable regression class: a deep-closed goal whose code artifact
   # never reached disk/git. Distinct from the 25d6520 over-deletion tail
   # (compiled-but-never-committed pre-cutover) and from the goal-selector
   # import-smoke check (g-115-768 sibling). Asserts the deliverable exists,
   # is git-tracked, and its >=5 attribution tests still pass.
   # Pytest idiom (g-115-1182): `-o addopts= ... 2>&1`, NOT `-q ... 2>/dev/null`.
   # This repo's pytest.ini sets addopts=-q; a second explicit -q double-quiets
   # pytest and suppresses the "N passed" summary line, so the grep returns empty
   # → false FAIL. `-o addopts=` overrides the .ini default (restores summary);
   # 2>&1 captures it regardless of stream. (Was the broken `-q | grep passed`
   # idiom until g-115-1182 fixed it here + in sibling Section ATF below.)
   Bash (cross-agent-attribution-deliverable): test -f core/scripts/_cross_agent_attribution_filter.py && git ls-files --error-unmatch core/scripts/_cross_agent_attribution_filter.py >/dev/null 2>&1 && N=$(py -3 -m pytest core/scripts/tests/test_post_state_update_attribution.py -o addopts= 2>&1 | grep -oE '[0-9]+ passed' | grep -oE '[0-9]+' | head -1) && [ -n "$N" ] && [ "$N" -ge 5 ] && echo "PASS: _cross_agent_attribution_filter.py exists + git-tracked + $N attribution tests passing (>=5)" || echo "FAIL: cross-agent attribution deliverable regression - _cross_agent_attribution_filter.py missing/untracked OR test_post_state_update_attribution.py <5 passing (g-115-714 lost-deliverable class; restore file + git add + ensure >=5 tests)"

   # Attribution-Filter in_flight-null Robustness (Section ATF — g-115-1182, 2026-05-26)
   # g-115-1154 confirmed team-state-clear-in-flight runs in iteration-close.sh
   # do_verify Step 3 (g-284-06) BEFORE post-state-update-gate fires the filter in
   # do_state_update Step 8.78 — so self.in_flight=null (self_claimed_at=0) is the
   # NORMAL filter-time state, not an edge case. Under that state Source 3
   # (pre-claim-mtime) is disabled by its `self_claimed_at>0` guard; only Sources 1
   # (concurrent-partner) and 2 (partner uncommitted-log) survive as fallbacks. This
   # guards that the in_flight-null contract stays pinned: a regression test asserts
   # Sources 1+2 STILL drop partner files and documents the Source 3 fail-open gap
   # (tracked by g-115-1154 / g-115-1178 / g-115-1180). NOT a Source 4 fallback —
   # the sq-018 spark proposed one but it contradicts the filter's fail-open
   # philosophy and is not the chosen fix layer. Sibling to Section CAA.
   # NOTE on the pytest idiom: this repo's pytest.ini sets `addopts = -q`, so a
   # second explicit `-q` double-quiets pytest and SUPPRESSES the "N passed"
   # summary line entirely (output becomes just the progress dots). Grepping
   # `-q ... 2>/dev/null` for "N passed" therefore returns empty → false FAIL.
   # Use `-o addopts=` to override the .ini default (restores the summary) and
   # `2>&1` so the summary is captured regardless of stream. (g-115-1182 fixed
   # the same broken `-q | grep passed` idiom in sibling Section CAA above in
   # the same pass.)
   Bash (attribution-filter-in-flight-null): test -f core/scripts/tests/test_attribution_filter_no_self_inflight.py && git ls-files --error-unmatch core/scripts/tests/test_attribution_filter_no_self_inflight.py >/dev/null 2>&1 && N=$(py -3 -m pytest core/scripts/tests/test_attribution_filter_no_self_inflight.py -o addopts= 2>&1 | grep -oE '[0-9]+ passed' | grep -oE '[0-9]+' | head -1) && [ -n "$N" ] && [ "$N" -ge 4 ] && echo "PASS: test_attribution_filter_no_self_inflight.py exists + git-tracked + $N cases passing (>=4) — Sources 1+2 still drop partner files when self.in_flight=null; Source 3 gap documented" || echo "FAIL: attribution-filter in_flight-null regression — test missing/untracked OR <4 cases passing (g-115-1182; restore test + git add, or if a Source-4/gate-scope fix changed the contract update the test + this check)"

   # WM-Path Test-Isolation Guard (Section WMP — g-115-1627, 2026-06-24)
   # post-g-306-61, wm.WM_PATH / wm.WM_LOCK_PATH are dynamic PEP 562 __getattr__
   # module properties — read_wm/write_wm/cmd_init/cmd_reset resolve via wm_path()
   # (BODY_WM_PATH env else AGENT_DIR/session/working-memory.yaml). Patching the
   # MODULE ATTRIBUTE (`wm.WM_PATH = tmp`) is therefore an I/O no-op that silently
   # targets the LIVE bound-agent WM (conftest sets MIND_AGENT), clobbering live
   # working memory. Allowed test isolation: BODY_WM_PATH env (monkeypatch.setenv)
   # or a wm.AGENT_DIR patch. This grep is the AUTOMATED backstop to guard-862 (the
   # authoring steer). Regex discipline (verified at authoring, g-115-1627): the
   # `=[^=]` tail excludes `==` comparisons (`assert wm.WM_PATH == body`); the
   # `grep -v ':N:<space>*#'` pass drops full comment lines; and the `(wm|wm_module)\.`
   # scope means crs_mod.WM_PATH (compact-restore-slots' own separate legitimate
   # constant) is NOT false-flagged. Refs: guard-862, rb-2296, g-115-1626.
   Bash (no-dead-wm-path-patch): hits=$(grep -rnE '(^|[^A-Za-z0-9_])(wm|wm_module)\.(WM_PATH|WM_LOCK_PATH)[[:space:]]*=[^=]' core/scripts/tests/ 2>/dev/null | grep -vE ':[0-9]+:[[:space:]]*#' || true); [ -z "$hits" ] && echo "PASS: no dead wm.WM_PATH/wm_module.WM_PATH module-attribute patches in core/scripts/tests (post-g-306-61 PEP 562 no-op — use BODY_WM_PATH env or wm.AGENT_DIR patch; guard-862)" || { echo "FAIL: dead wm.WM_PATH attribute patch in test(s) — a PEP 562 no-op that silently targets the LIVE bound-agent WM (guard-862, rb-2296, g-115-1626); switch to BODY_WM_PATH env (monkeypatch.setenv) or wm.AGENT_DIR patch:"; echo "$hits"; }

   # No-Broken-Pytest-Idiom Guard (Section PYI — g-115-1182, 2026-05-26)
   # guard-656: any verify-learning Bash check that greps pytest output for the
   # "N passed" summary MUST pass `-o addopts=` (this repo's pytest.ini sets
   # `addopts = -q`; an inherited/second `-q` double-quiets pytest and
   # SUPPRESSES the summary line — output becomes bare progress dots — so the
   # grep returns empty -> silent FALSE-FAIL). g-115-1182's sq-018 spark added
   # this self-referential guard after finding the idiom silently FALSE-FAILing
   # in THREE checks (Section CAA, Section ATF, and the PMG `tests-pass` check
   # below — all fixed in the same pass). Detector scopes to `Bash (` check
   # lines (the `#` comments above are not matched); a check that runs pytest +
   # greps `passed` but lacks `addopts=` is the broken signature. This PYI line
   # contains `addopts=` and is therefore self-excluded.
   Bash (no-broken-pytest-idiom): BROKEN=$(grep -E '^[[:space:]]*Bash \(' .claude/skills/verify-learning/SKILL.md | grep -F 'pytest' | grep -F 'passed' | grep -v -F 'addopts='); [ -z "$BROKEN" ] && echo "PASS: every verify-learning pytest-summary-grep check uses -o addopts= (no silent FALSE-FAIL idiom)" || echo "FAIL: a Bash check greps pytest for 'passed' without -o addopts= — addopts=-q suppresses the summary, silent FALSE-FAIL (guard-656, g-115-1182). Offending: $BROKEN"

   # Cadence-Gate Slot-Shape Guard (Section CGI -- g-115-1684, sq-018, 2026-06-28)
   # The three cadence-check scripts read the goals_count_at_last_fire dict WM slot
   # via last.get(...). A legacy/restored bare-timestamp-STRING slot makes last.get()
   # raise AttributeError BEFORE the dict-writer runs, so the slot can never self-heal
   # (rb-2482 self-heal-deadlock). g-115-1681/1682 added isinstance(last, dict) guards
   # to all three (l1-skew-check.py, felt-sense-cadence-check.py,
   # fresh-eyes-cadence-check.py). This assertion catches a future 4th cadence gate or
   # a refactor reintroducing the unguarded read: any core/scripts/*.py reading
   # goals_count_at_last_fire via last.get() MUST also contain isinstance(last, dict).
   # Regex (not fixed-string) on the guard so a spacing variant still counts.
   Bash (cadence-gate-isinstance-guard): VIOLATORS=$(for f in $(grep -lF 'goals_count_at_last_fire' core/scripts/*.py 2>/dev/null); do grep -qE 'last\.get' "$f" && ! grep -qE 'isinstance\(\s*last\s*,\s*dict\s*\)' "$f" && echo "$f"; done); [ -z "$VIOLATORS" ] && echo "PASS: every core/scripts/*.py reading goals_count_at_last_fire via last.get() also guards with isinstance(last, dict) (rb-2482 self-heal-deadlock)" || echo "FAIL: a cadence-gate script reads goals_count_at_last_fire via last.get() WITHOUT an isinstance(last, dict) guard -- a legacy bare-string slot raises AttributeError before the dict-writer self-heals (rb-2482, g-115-1681/1682). Offending: $VIOLATORS"

   # Committed-Files-Only Gate Scoping (Section CFO — g-115-1178, 2026-05-26)
   # post-state-update-gate.sh now scopes its fresh-eyes file-detection to the
   # files a commit actually landed when iteration-close.sh extracts the
   # commit_sha from iteration-commit.sh's JSON and exports COMMIT_SHA (Option B
   # from the g-115-1154 stranded-partner-false-positive investigation). When
   # COMMIT_SHA is valid the gate uses git diff --name-only SHA~1..SHA AND skips
   # untracked detection (committed scope); when unset/empty/invalid it falls
   # back to the prior working-tree behavior. These checks guard all three
   # surfaces: gate consumption, iteration-close extraction+pass, and the
   # regression test (3 cases: committed-scope, unset-fallback, invalid-fallback).
   # Pytest idiom (guard-656): `-o addopts= ... 2>&1`, never `-q ... 2>/dev/null`
   # (this repo's pytest.ini sets addopts=-q; a second -q suppresses the summary
   # line -> false FAIL). The regression check below carries `addopts=` and is
   # therefore self-excluded from Section PYI's detector.
   Bash (cfo-gate-consumes-commit-sha): grep -qF 'COMMIT_SHA_VALID' core/scripts/post-state-update-gate.sh && grep -qF '"${COMMIT_SHA}~1" "${COMMIT_SHA}"' core/scripts/post-state-update-gate.sh && echo "PASS: post-state-update-gate.sh scopes to the COMMIT_SHA committed range (g-115-1178)" || echo "FAIL: post-state-update-gate.sh lost the COMMIT_SHA committed-scope branch — stranded-partner false-positive scoping regressed (g-115-1178); restore the COMMIT_SHA_VALID branch + the SHA~1..SHA range diff"
   Bash (cfo-iteration-close-extracts-commit-sha): grep -qF '_commit_sha="$(printf' core/scripts/iteration-close.sh && grep -qF 'COMMIT_SHA="${_commit_sha:-}"' core/scripts/iteration-close.sh && echo "PASS: iteration-close.sh extracts commit_sha from iteration-commit JSON and passes COMMIT_SHA to the gate (g-115-1178)" || echo "FAIL: iteration-close.sh no longer extracts commit_sha / passes COMMIT_SHA to post-state-update-gate.sh — committed-scope wiring regressed (g-115-1178)"
   Bash (cfo-regression-test): test -f core/scripts/tests/test_post_state_update_gate_committed_files_only.py && git ls-files --error-unmatch core/scripts/tests/test_post_state_update_gate_committed_files_only.py >/dev/null 2>&1 && N=$(py -3 -m pytest core/scripts/tests/test_post_state_update_gate_committed_files_only.py -o addopts= 2>&1 | grep -oE '[0-9]+ passed' | grep -oE '[0-9]+' | head -1) && [ -n "$N" ] && [ "$N" -ge 3 ] && echo "PASS: committed-files-only regression test exists + git-tracked + $N cases passing (>=3)" || echo "FAIL: committed-files-only regression test missing/untracked OR <3 passing (g-115-1178; restore test + git add, or if the scoping contract changed update the test + this check)"

   # Iteration-Commit Pathspec Invariant (Section ICP -- g-115-1498, sq-018, 2026-06-16)
   # iteration-commit.sh's terminal commit was changed from whole-index
   # `git commit -F -` to pathspec-scoped `git commit -F - -- "${staged_files[@]}"`
   # (~L1105) so a concurrent partner's pre-staged WIP in the shared multi-agent
   # index is never swept into this agent's commit (guard-741). A revert to the
   # bare form silently re-opens the Case-A cross-agent STAGED-bleed (the
   # namespace-droppable partner-file leak g-115-1498 closed). This check pins the
   # invariant: every `commit -F -` line MUST carry the `-- staged_files` pathspec
   # (bare-count == scoped-count, scoped >= 1). Sibling sweep: release.sh Step 9
   # was pathspec-scoped by the same class (g-115-1504); seed-transplant.sh:296's
   # bare add-A is reviewed-SAFE (foreign single-purpose publication $DEST).
   Bash (icp-iteration-commit-pathspec): B=$(grep -cF 'commit -F -' core/scripts/iteration-commit.sh); S=$(grep -cF 'commit -F - -- "${staged_files[@]}"' core/scripts/iteration-commit.sh); [ "${B:-0}" = "${S:-0}" ] && [ "${S:-0}" -ge 1 ] && echo "PASS: iteration-commit.sh terminal commit is pathspec-scoped (every 'commit -F -' carries '-- staged_files'); whole-index bleed closed (g-115-1498, guard-741)" || echo "FAIL: iteration-commit.sh has a bare 'commit -F -' (whole-index) OR lost the pathspec form (bare=$B scoped=$S) -- a concurrent partner's pre-staged WIP can be swept into this agent's commit (g-115-1498, guard-741); restore the pathspec form at ~L1105"

   # Fresh-Eyes Findings-Board Self-Evolution Read (Section FEF — g-115-1214, 2026-05-26)
   # fresh-eyes-review Phase 2 input assembly historically read only
   # pending-questions.yaml for self-evolution signals (Phase 2.3), never the
   # findings board. Incident (2026-05-24): a no_change verdict landed with
   # self_evolution_signals_count=0 while alpha self-drift finding
   # msg-20260523-091626-alpha-1586 sat unread on world/board/findings (later
   # actioned by hand as g-115-1213). The fix added Phase 2.3b (board-read
   # --channel findings, filtered to self_evolution/self-drift directed at this
   # agent) and folded board_signals into self_evolution_signals_count. See
   # rb-1279. This check guards that the findings-board read does not regress.
   Bash (fresh-eyes-reads-findings-board): grep -qE 'board-read\.sh --channel findings' .claude/skills/fresh-eyes-review/SKILL.md && grep -qF 'board_signals' .claude/skills/fresh-eyes-review/SKILL.md && echo "PASS: fresh-eyes-review Phase 2 reads the findings board for self_evolution/self-drift signals (g-115-1214)" || echo "FAIL: fresh-eyes-review Phase 2 no longer reads world/board/findings for self-evolution signals — the 2026-05-24 self_evolution_signals_count=0 blind spot regressed (g-115-1214, rb-1279); restore Phase 2.3b board-read + board_signals fold into self_evolution_signals_count"

   # Cross-agent review floor (Section CAR -- g-303-23): the cross-agent review
   # pattern (bravo<->alpha<->zeta mutual review; review-request gate Phase 5.7;
   # insight_trigger findings; goal-duplication gate; fresh-eyes-code) was the
   # single most valuable artifact named in the alpha session-60 retrospective,
   # but emergent + unmeasured until codified in world/conventions/cross-agent-
   # review.md. This check guards BOTH (a) the convention exists and (b) the live
   # review-request rate stays above the calibrated floor (20/30d, ~40% of the
   # 51/30d baseline) so a future refactor that scopes board posts or throttles
   # the review gate cannot weaken the pattern silently. Requires the daemon up
   # (board-read); an unexpected FAIL with the daemon down is a false positive --
   # re-run once the daemon is healthy. rb-871 / b7c4caa / rb-2374 are the cited
   # catches in the convention's Value section.
   Bash (cross-agent-review-floor): source core/scripts/_paths.sh 2>/dev/null; conv="$WORLD_DIR/conventions/cross-agent-review.md"; if [ ! -f "$conv" ]; then echo "FAIL: world/conventions/cross-agent-review.md missing -- cross-agent review pattern uncodified (g-303-23)"; else floor=20; cnt=$(bash core/scripts/board-read.sh --channel coordination --type review-request --since 720h --json 2>/dev/null | grep -c '"id"' 2>/dev/null); case "$cnt" in (*[!0-9]*|"") cnt=0;; esac; if [ "$cnt" -lt "$floor" ]; then echo "FAIL: cross-agent review-request floor breached -- ${cnt}/30d < ${floor} (review gate Phase 5.7 stopped firing or board posts scoped away; verify daemon is up first; g-303-23, world/conventions/cross-agent-review.md)"; else echo "PASS: cross-agent review pattern alive -- ${cnt} review-requests/30d >= floor ${floor} (g-303-23)"; fi; fi

   # Partner-belief Theory-of-Mind loop (g-306-28, rb-1989): fresh-eyes-review
   # Phase 2.6c WRITES one calibrated belief about the most-salient partner per
   # 25-goal review (team-belief-write.sh) and Phase 2.6b CONSUMES partners'
   # beliefs ABOUT this agent as confidence+staleness-weighted self-evolution
   # signals. Both halves are LLM-executed pseudocode with NO unit test (the
   # _team_belief.py module IS tested; the SKILL.md wiring is not), so a silent
   # removal breaks the loop undetected. This grep is the only guard.
   Bash (fresh-eyes-wires-partner-belief-loop): grep -qF 'team-belief-write.sh' .claude/skills/fresh-eyes-review/SKILL.md && grep -qF '2.6b CONSUMER' .claude/skills/fresh-eyes-review/SKILL.md && echo "PASS: fresh-eyes-review wires the ToM partner-belief write (2.6c team-belief-write.sh) + consume (2.6b) loop (g-306-28)" || echo "FAIL: fresh-eyes-review lost the Phase 2.6b consumer or the 2.6c team-belief-write.sh writer -- the Theory-of-Mind partner-belief loop is unwired (g-306-28, rb-1989); restore Phase 2.6b/2.6c + companion_scripts team-belief-write.sh"

   # Partner-belief contradiction trigger (g-306-29): aspirations-precheck Phase
   # 0-pre.0a runs belief-contradiction-check.sh (thin orchestrator over the pure,
   # unit-tested _belief_contradiction.py) once per iteration to detect a partner
   # action contradicting a held domain-belief and force a belief revision after N
   # consecutive observations (the no-false-trigger-on-first invariant lives in the
   # module's next_streak gate). The module has test_belief_contradiction.py, but
   # the precheck WIRING is LLM-executed pseudocode with no unit test -- a silent
   # removal unwires outcome 3 of the ToM loop undetected. This grep guards it.
   Bash (precheck-wires-belief-contradiction-check): grep -qF 'belief-contradiction-check.sh' .claude/skills/aspirations-precheck/SKILL.md && test -f core/scripts/_belief_contradiction.py && echo "PASS: aspirations-precheck Phase 0-pre.0a wires the ToM contradiction->forced-reflection trigger (belief-contradiction-check.sh + _belief_contradiction.py, g-306-29)" || echo "FAIL: aspirations-precheck lost the Phase 0-pre.0a belief-contradiction-check.sh hook or _belief_contradiction.py is missing -- outcome 3 of the Theory-of-Mind partner-belief loop is unwired (g-306-29); restore Phase 0-pre.0a + the pure module"

   # Handoff-aging escalation wiring (g-115-1524): aspirations-precheck Phase
   # 0.5b.2b runs handoff-aging-check.sh --apply once per iteration to post a
   # coordination-board escalation for cross-agent handoff goals aged past
   # handoff_aging.escalate_hours (default 72). It REPLACED LLM-only pseudocode
   # that silently skipped under abbreviation -- a 2026-06-18 fresh-eyes-review
   # found 6 handoffs aged 78-782h with an EMPTY proactive_escalation_log. The
   # precheck WIRING is LLM-executed pseudocode with no unit test (the
   # handoff-aging-check.py module IS unit-tested via test_handoff_aging_check.py),
   # so a silent revert to LLM-only pseudocode re-opens the gap undetected.
   # This grep guards it -- sibling of precheck-wires-belief-contradiction-check.
   Bash (precheck-wires-handoff-aging-check): grep -qF 'handoff-aging-check.sh --apply' .claude/skills/aspirations-precheck/SKILL.md && test -f core/scripts/handoff-aging-check.sh && echo "PASS: aspirations-precheck Phase 0.5b.2b wires the bash-enforced handoff-aging escalation (handoff-aging-check.sh --apply + core/scripts/handoff-aging-check.sh, g-115-1524)" || echo "FAIL: aspirations-precheck lost the Phase 0.5b.2b handoff-aging-check.sh --apply invocation or core/scripts/handoff-aging-check.sh is missing -- cross-agent handoff aging escalation regressed to LLM-only pseudocode (the 2026-06-18 empty-escalation-log gap, g-115-1524); restore Phase 0.5b.2b + the script"

   # SID-Collision Hardening (Section SID-COLLISION — 2026-05-12)
   # Four-tier defense against Claude Code session_id reuse across windows
   # (`claude --continue` / `--resume` SID-reuse, observed 2026-05-12 zeta-bravo
   # cross-binding incident). Each tier addresses a distinct failure mode the
   # Loop-Death Catalog (Agent 2 of the 3-agent research team) surfaced:
   #
   # Tier 1a — sid-collision-check.sh refuses /start (IDLE Step 0 + observer
   #   Step 0) AND session-save-id.sh compact branch (8th witness) when the
   #   SID is already bound to a live running agent.
   # Tier 1b — stale-binding cleanup in session-save-id.sh + stop-hook.sh
   #   strengthened from {mtime>24h && no-running-session-id-file} to a
   #   three-signal predicate (SID-content match + heartbeat-fresh).
   # Tier 1c — stop-hook Gate 2.6 (background-jobs.sh has-pending) mirrors
   #   recovery-gate's Path A Condition 4.
   # Tier 2a — stop-hook `set -e` removed + `|| true` on log appends; a
   #   transient OneDrive lock can no longer kill the hook → ALLOW.
   # Tier 2b — session-save-id.sh verify-after-write on running-session-id
   #   and latest-session-id, with one retry + .write-failures.jsonl log.
   # Tier 2c — recovery-gate retry counter; after 3 consecutive failed
   #   _perform_recovery, refuses with recovery-failed-permanent signal.
   # Tier 3a — framework-owned UUID4 `runner-token` triple-written at /start
   #   (with running-session-id + latest-session-id); logged by stop-hook +
   #   watchdog so collisions show up as same-SID-different-token in audit.
   # Tier 4a — autocompact precompact-serialize 60s timeout writes a poison
   #   marker; session-save-id source=compact refuses breadcrumb consumption.
   #
   # Each check below corresponds to one wired site. FAIL means a tier
   # regressed; don't fix the check, fix the underlying wiring.
   Bash (sid-collision-script-exists): test -x core/scripts/sid-collision-check.sh && echo "PASS: sid-collision-check.sh present and executable" || echo "FAIL: sid-collision-check.sh missing or non-executable (Tier 1a)"
   Bash (start-idle-wires-collision-check): grep -q "sid-collision-check.sh" .claude/skills/start/SKILL.md && echo "PASS: /start references sid-collision-check.sh" || echo "FAIL: /start does NOT call sid-collision-check.sh (Tier 1a regressed)"
   Bash (start-halt-on-collision): grep -q "HALT ON SID_COLLISION" .claude/skills/start/SKILL.md && echo "PASS: /start has HALT ON SID_COLLISION instruction" || echo "FAIL: /start missing HALT ON SID_COLLISION (Tier 1a)"
   Bash (save-id-eighth-witness): grep -q "sid-collision-check.sh" core/scripts/session-save-id.sh && echo "PASS: session-save-id.sh compact branch calls sid-collision-check.sh (8th witness)" || echo "FAIL: session-save-id.sh missing 8th-witness collision check (Tier 1a)"
   # Tier 1a (test-defense, g-115-1224) — the sid-collision GATE is itself guarded by
   # core/scripts/tests/test-sid-collision-check.sh, which carries two stub-drift
   # defenses: (1) the _resolve_agent_from_sid.py sandbox-stub heredoc (delimiter
   # RESOLVE_EOF) and (2) the 0-CANARY-harness-can-detect-collision case at the suite
   # head. If either is removed by a refactor, collision cases 7+9 short-circuit at the
   # resolver-missing exit and the suite silently reverts to vacuous-pass — the gate's
   # regression net vanishes with no failing test. Assert both survive (dfc27caa).
   Bash (sid-collision-test-stub-defenses): grep -q "RESOLVE_EOF" core/scripts/tests/test-sid-collision-check.sh && grep -q "0-CANARY-harness-can-detect-collision" core/scripts/tests/test-sid-collision-check.sh && echo "PASS: test-sid-collision-check.sh retains both stub-drift defenses (resolver-stub heredoc + 0-CANARY canary)" || echo "FAIL: test-sid-collision-check.sh missing resolver-stub heredoc OR 0-CANARY canary — suite may silently revert to vacuous-pass mode (cases 7+9 short-circuit at resolver-missing exit; g-115-1224)"
   # Tier 1b — stale-binding cleanup predicate (3-signal: mtime>24h + SID-mismatch +
   # heartbeat-stale). Extracted to shared core/scripts/cleanup-stale-bindings.sh on
   # 2026-05-12 (g-303-25 B2). Both stop-hook.sh and session-save-id.sh invoke the
   # helper. B1 fix (g-303-25): stop-hook touches its own binding before invoking the
   # helper, giving observer modes (assistant/reader) an mtime-based liveness signal —
   # observer modes write neither running-session-id (signal 2) nor heartbeat (signal 3).
   # B3 fix (g-303-25): silent `except Exception: return 5` fallbacks removed from
   # _read_flip_threshold + _read_global_ceiling in recurring-loop-state-mutate.py;
   # exceptions now propagate to caller's `|| echo "$ORIGINAL_OUTCOME"` fail-open.
   Bash (cleanup-helper-exists): test -f core/scripts/cleanup-stale-bindings.sh && grep -q "_BIND_SID=" core/scripts/cleanup-stale-bindings.sh && grep -q "heartbeat-stale.sh" core/scripts/cleanup-stale-bindings.sh && echo "PASS: cleanup-stale-bindings.sh has 3-signal predicate" || echo "FAIL: cleanup-stale-bindings.sh missing or predicate weakened (Tier 1b, g-303-25 B2)"
   Bash (cleanup-heartbeat-preserve-on-misconfig): grep -q '|| echo fresh' core/scripts/cleanup-stale-bindings.sh && echo "PASS: heartbeat-probe misconfig preserves binding (matches recovery-gate.sh pattern)" || echo "FAIL: cleanup may delete on heartbeat-probe misconfig — should use `|| echo fresh` like recovery-gate (g-303-26 B5)"
   Bash (stop-hook-uses-helper-and-touch): grep -q "cleanup-stale-bindings.sh" core/scripts/stop-hook.sh && grep -qE 'touch -c .*active-agent-\$HOOK_SID' core/scripts/stop-hook.sh && echo "PASS: stop-hook.sh invokes shared helper AND touches own binding (B1+B2)" || echo "FAIL: stop-hook.sh missing helper invocation or B1 touch-fix (Tier 1b, g-303-25 B1+B2)"
   Bash (save-id-uses-helper): grep -q "cleanup-stale-bindings.sh" core/scripts/session-save-id.sh && echo "PASS: session-save-id.sh invokes shared cleanup helper" || echo "FAIL: session-save-id.sh missing helper invocation (Tier 1b, g-303-25 B2)"
   Bash (threshold-no-silent-fallback): test "$(grep -c 'except Exception:' core/scripts/recurring-loop-state-mutate.py)" -eq 1 && echo "PASS: only import-guard except Exception remains (B3 silent fallbacks removed)" || echo "FAIL: extra except Exception found — silent threshold-helper fallbacks may have been re-added (g-303-25 B3)"
   Bash (stop-hook-gate-2-6): grep -q "gate=background-jobs" core/scripts/stop-hook.sh && echo "PASS: stop-hook has Gate 2.6 (background-jobs has-pending)" || echo "FAIL: stop-hook missing Gate 2.6 (Tier 1c)"
   Bash (stop-hook-no-set-e): ! grep -qE '^set -euo pipefail' core/scripts/stop-hook.sh && grep -qE '^set -uo pipefail' core/scripts/stop-hook.sh && echo "PASS: stop-hook uses set -uo pipefail (no -e)" || echo "FAIL: stop-hook has -e set (Tier 2a regressed)"
   Bash (stop-hook-log-fail-open): test "$(grep -c '>> "\$LOG" 2>/dev/null || true' core/scripts/stop-hook.sh)" -ge 9 && echo "PASS: stop-hook log appends are fail-open" || echo "FAIL: stop-hook log appends missing || true (Tier 2a)"
   Bash (save-id-verify-after-write): grep -q "_SID_ATTEMPT" core/scripts/session-save-id.sh && grep -q ".write-failures.jsonl" core/scripts/session-save-id.sh && echo "PASS: session-save-id.sh verifies after write with retry+log" || echo "FAIL: session-save-id.sh missing verify-after-write (Tier 2b)"
   Bash (recovery-retry-counter): grep -q "recovery-failure-count" core/scripts/recovery-gate.sh && grep -q "recovery-failed-permanent" core/scripts/recovery-gate.sh && echo "PASS: recovery-gate.sh has retry counter + permanent-fail signal" || echo "FAIL: recovery-gate.sh missing retry counter (Tier 2c)"
   Bash (manifest-runner-token): grep -q "^  - file: runner-token$" core/config/session-manifest.yaml && echo "PASS: manifest registers runner-token" || echo "FAIL: manifest missing runner-token entry (Tier 3a)"
   # /start B10 MIND_AGENT-prefix on permissions-add.sh (g-115-1014, rb-1105, guard-307)
   # B10 invokes `MIND_AGENT=<agent-name> bash core/scripts/permissions-add.sh` per the
   # post-aac64d27 hardening. If a future edit silently removes the prefix, the failure mode is
   # invisible on UNINITIALIZED first-run (only one local-paths.conf exists so first-conf
   # fallback resolves correctly) but produces wrong-agent path resolution on multi-agent
   # installs. This grep catches the regression at edit time.
   Bash (start-b10-ayoai-agent-prefix): grep -qE 'MIND_AGENT=<agent-name> bash core/scripts/permissions-add\.sh' .claude/skills/start/SKILL.md core/config/start-uninitialized-ceremony.md && echo "PASS: /start B10 invocation carries explicit MIND_AGENT=<agent-name> prefix on permissions-add.sh (g-115-1014, guard-307; B10 in core/config/start-uninitialized-ceremony.md digest since g-115-1723-b)" || echo "FAIL: /start B10 invocation missing MIND_AGENT=<agent-name> prefix on permissions-add.sh — silent multi-agent-install regression risk (g-115-1014, rb-1105)"
   Bash (start-writes-runner-token): test "$(cat .claude/skills/start/SKILL.md core/config/start-phase-c.md | grep -c 'RUNNER_TOKEN=\$(')" -ge 2 && echo "PASS: /start writes runner-token at IDLE Step 3 (SKILL.md) + UNINITIALIZED C8 (start-phase-c.md digest)" || echo "FAIL: /start missing runner-token write at one of the two canonical sites (Tier 3a; C8 lives in core/config/start-phase-c.md since g-115-1723-a extracted Phase C)"
   Bash (stop-hook-logs-runner-token): test "$(grep -c 'runner_token=' core/scripts/stop-hook.sh)" -ge 6 && echo "PASS: stop-hook logs runner_token in BLOCK/ALLOW lines" || echo "FAIL: stop-hook missing runner_token logging (Tier 3a)"
   Bash (watchdog-correlates-runner-token): grep -q '"runner_token":' core/scripts/agent-watchdog.py && echo "PASS: watchdog RunningSidProbe includes runner_token in correlated context" || echo "FAIL: watchdog missing runner_token (Tier 3a)"
   Bash (precompact-poison-write): grep -q "autocompact-serialize-poison" core/scripts/precompact-serialize.sh && echo "PASS: precompact-serialize.sh writes poison marker on timeout" || echo "FAIL: precompact-serialize.sh missing poison marker (Tier 4a)"
   Bash (save-id-poison-gate): grep -q "autocompact-serialize-poison" core/scripts/session-save-id.sh && echo "PASS: session-save-id.sh checks poison marker in compact branch" || echo "FAIL: session-save-id.sh missing poison gate (Tier 4a)"
   Bash (race-window-closed-message): test "$(grep -c 'RACE_WINDOW_CLOSED' .claude/skills/start/SKILL.md)" -ge 3 && echo "PASS: /start prints RACE_WINDOW_CLOSED at all 3 binding sites (IDLE Step 0 + observer Step 0 + UNINITIALIZED A2)" || echo "FAIL: /start missing RACE_WINDOW_CLOSED at one of the 3 binding sites — user cannot tell when safe to start next agent"
   # Triple-write-before-state-set ordering (rb-323 / guard-403) — observer-paired signals MUST be seeded BEFORE state-set RUNNING in /start. The 2026-05-12 fresh-eyes review found the triple-write was AFTER state-set, creating a partial-write window. Each site's state-set line MUST be preceded by the triple-write within ~50 lines so a future engineer cannot inadvertently re-flip the order. (Widened from 10 to 50 on 2026-05-20 after the F4 reorder legitimately inserted 23 lines of pure-Bash cleanups between the triple-write and state-set — see start/SKILL.md IDLE Step 3 for the rationale comment.)
   Bash (start-triple-write-before-state-set): py -3 -c "
import re, pathlib
lines = pathlib.Path('.claude/skills/start/SKILL.md').read_text(encoding='utf-8').splitlines()
state_set = [i for i, L in enumerate(lines) if 'session-state-set.sh RUNNING' in L and 'Bash:' in L]
fails = []
for ln in state_set:
    window = '\n'.join(lines[max(0, ln-50):ln])
    if 'RUNNER_TOKEN' not in window:
        fails.append(ln+1)
if fails:
    print('FAIL: state-set RUNNING at line(s)', fails, 'not preceded by triple-write within 50 lines (rb-323/guard-403 ordering regressed — partial-write window re-introduced)')
else:
    print('PASS: every /start state-set RUNNING is preceded by the runner-token triple-write (rb-323/guard-403 ordering preserved)')
"

   # Terminal Goal Well-Formedness Invariants (Section TGD — 2026-05-12)
   # A goal in terminal status (completed/skipped/expired/decomposed/superseded)
   # MUST satisfy BOTH invariants:
   #   (1) No residual deferral state: defer_reason, defer_reason_set_at,
   #       deferred_until, blocker_ref, blocked_since all cleared. Active
   #       state was meaningful while work was in flight; on close it
   #       becomes anomaly data that distorts goal-selector scoring and
   #       `aspirations-read --blocked` consumers (/encode-session Lane 3).
   #       — g-115-660 cluster, 2026-05-12.
   #   (2) completed_at is set: the terminal-transition timestamp must be
   #       stamped. Drives recency sorts (e.g. cmd_read --stepping-stones),
   #       audit trails, and team-state recent_completions. — g-115-661
   #       cluster, 2026-05-12 (zeta).
   #
   # Enforcement is at the disk-write boundary in aspirations.py via
   # _normalize_terminal_goal, called from:
   #   - _write_live_under_lock (every LIVE write)
   #   - cmd_complete / cmd_retire (before archive append_jsonl)
   #   - cmd_archive_sweep (before write_jsonl on the archive list)
   # Layer 1 transition stamps also live inline at cmd_update_goal (~2210),
   # cmd_recover_recurring Case 3 (~2803), and cmd_complete_by (~3447).
   #
   # The 2026-05-12 backfill (`py -3 core/scripts/normalize-terminal-defer.py`)
   # cleaned 120 pre-existing defer-state anomalies AND 533 completed_at gaps.
   # This check guards both invariants — a non-zero count implies a write path
   # that bypassed _normalize_terminal_goal.
   Bash (terminal-goal-invariants): py -3 core/scripts/normalize-terminal-defer.py --check

   # Skill Frontmatter Integrity (Section SFI — 2026-05-11 fresh-eyes finding)
   # _skill_md.parse_front_matter uses \A--- (strict start-of-file anchor).
   # ANY content above line 1 — HTML comment, blank line, BOM, stray text —
   # makes the regex miss and returns {} silently. Every consumer
   # (capability-gate, blocker-create-gate, Claude Code's own skill loader)
   # then sees empty metadata. Caught when 7 SKILL.md files had
   # `<!-- domain-leak-exempt -->` HTML comments inserted above `---` by an
   # auto-commit subsystem; also flushed out add-npc-task missing `name`.
   # Per-file metadata MUST live INSIDE the front matter as YAML `#`-comments.
   Bash (skill-frontmatter): bash core/scripts/skill-frontmatter-audit.sh → must exit 0 and print "PASS: all N SKILL.md files parse with non-empty name" (FAIL lists each broken file with parsed_keys so you can see whether `name` is missing entirely or whether the whole front matter is unparseable)

   # Pseudocode Script Validation (Section PSV — 2026-05-12 fresh-eyes finding)
   # Two fresh-eyes review passes caught 6 phantom-script/phantom-flag refs in
   # SKILL.md pseudocode that an automated check would have caught earlier:
   # `reasoning-bank-update.sh` (real name has `-field`), `pending-questions-list.sh`
   # (no such script), `board-read.sh --since-iso` (real flag is `--since`),
   # `aspirations-read.sh --goal --field` (neither flag exists),
   # `tree-find-node.sh --key` (real flag is `--node` via wrapper pass-through).
   # Each one would have surfaced at runtime as an argparse error or silent no-op.
   # The verifier scans every `(bash|py -3|python3) core/scripts/<name>.<ext>`
   # invocation in SKILL.md and checks (a) the script exists on disk and
   # (b) every long-form flag is in the script's --help output (or the
   # wrapper's case statement). The fast --scripts-only mode used here
   # runs in ~1s; the full flag check is ~3 minutes and runs out-of-band.
   Bash (psv-scripts-only): out=$(py -3 core/scripts/verify-pseudocode-scripts.py --scripts-only 2>&1); echo "$out" | grep -qE "^clean " && echo "PASS: no phantom-script references in SKILL.md pseudocode" || { echo "$out"; echo "FAIL: phantom scripts referenced in SKILL.md — fix the offending references or rename the scripts (run \`py -3 core/scripts/verify-pseudocode-scripts.py\` for full flag validation)"; }

   # Origin-signal conformance (Section OSC — asp-248, 2026-04-23)
   # Every aspiration's origin_signal must match a prefix in ALLOWED_PREFIXES
   # of origin-signal-gate.py. Stale prefixes (e.g., "self_observation") slip
   # past LLM review but fail the gate at create time.
   Check: `core/scripts/origin-signal-gate.py` defines a top-level `ALLOWED_PREFIXES` set (authoritative whitelist)
   Check: `.claude/skills/create-aspiration/SKILL.md` Step 1.2 origin_signal computation comments the ALLOWED_PREFIXES location — grep Step 1.2 for `origin-signal-gate.py` must match
   Bash (from-followup-smoke): grep -q 'from-followup' .claude/skills/create-aspiration/SKILL.md && grep -q 'user_directive\|idea:' .claude/skills/create-aspiration/SKILL.md && echo "PASS: from-followup mode present with valid origin_signal prefixes" || echo "FAIL: from-followup mode missing or uses unregistered prefix"

   # Prefix-table sync (g-115-1102, rb-1170): ALLOWED_PREFIXES in
   # core/scripts/gates/origin_signal.py and the prefix-mapping inside
   # core/scripts/_goal_source.py infer() must agree on every colon-suffix
   # prefix. Drift between the two tables produces goal_source=null entries
   # that pass the origin_signal gate at create time but fall through infer()
   # at write time. g-115-1100 audit found 7/11 new asp-115 goals had
   # goal_source=null because alert-email:, routing-mismatch:, and
   # insight_trigger: were absent from both tables. The check below extracts
   # ALLOWED_PREFIXES via AST and verifies every colon-suffix entry appears
   # as a literal substring in _goal_source.py — catches additions to the
   # whitelist that forget to extend infer().
   Bash (origin-signal-goalsource-sync): missing=$(py -3 -c 'import ast,pathlib;a=pathlib.Path("core/scripts/gates/origin_signal.py").read_text(encoding="utf-8");b=pathlib.Path("core/scripts/_goal_source.py").read_text(encoding="utf-8");pre=set();[pre.add(e.value) for n in ast.walk(ast.parse(a)) if isinstance(n,ast.Assign) and any(getattr(t,"id","")=="ALLOWED_PREFIXES" for t in n.targets) and isinstance(n.value,ast.Tuple) for e in n.value.elts if isinstance(e,ast.Constant) and isinstance(e.value,str)];print(" ".join(x for x in sorted(pre) if x.endswith(":") and x not in b))'); [ -z "$missing" ] && echo "PASS: origin_signal/goal_source prefix-table sync" || echo "FAIL: prefix-table drift -- '$missing' in ALLOWED_PREFIXES but absent from _goal_source.py infer()"

   # Recurring urgency cap + 6-agent role_multiplier table (g-115-1109, rb-1170 paired-table drift class).
   # Three regression-class checks for the zeta-1477 fix (urgency_max cap on log-scaled
   # recurring_urgency) and the 6-agent goal-selection-strategy table. Without these
   # checks, accidental removal of the cap during refactor or loss of an agent entry
   # would silently revert the fix — same regression class g-115-1106 closed for
   # min_session_goals gate. Source assertions (verified at goal close 2026-05-22):
   #   goal-selector.py:275 "urgency_max": 4.0 in RECURRING_CONFIG defaults dict
   #   goal-selector.py:1681 rec = min(rec, RECURRING_CONFIG["urgency_max"]) in criterion 7
   #   aspirations.yaml:673 urgency_max: 4.0 under recurring block
   #   aspirations.yaml:1171 recurring.urgency_max: {...} in modifiable bounds
   #   meta/goal-selection-strategy.yaml:29 agent_role_multipliers: block with 5 agents (alpha/bravo/zeta/foxtrot/echo) — re-keyed 2026-07-07 agent-merge (charlie+delta -> foxtrot)
   Bash (recurring-urgency-max-cap): defaults=$(grep -c '"urgency_max": ' core/scripts/goal-selector.py); applied=$(grep -c 'min(rec, RECURRING_CONFIG\["urgency_max"\])' core/scripts/goal-selector.py); [ "$defaults" -ge 1 ] && [ "$applied" -ge 1 ] && echo "PASS: goal-selector.py urgency_max cap (defaults=$defaults, applied=$applied)" || echo "FAIL: goal-selector.py urgency_max cap missing (defaults=$defaults, applied=$applied — zeta-1477 fix reverted?)"
   Bash (recurring-urgency-max-config): recur=$(grep -c '^  urgency_max: ' core/config/aspirations.yaml); bounds=$(grep -c '^  recurring\.urgency_max:' core/config/aspirations.yaml); [ "$recur" -ge 1 ] && [ "$bounds" -ge 1 ] && echo "PASS: aspirations.yaml urgency_max (recurring=$recur, bounds=$bounds)" || echo "FAIL: aspirations.yaml urgency_max missing (recurring=$recur, bounds=$bounds — config drift, zeta-1477 cap not configurable)"
   Bash (agent-role-multipliers-5): source core/scripts/_paths.sh; f="$META_DIR/goal-selection-strategy.yaml"; has_block=$(grep -c '^agent_role_multipliers:' "$f" 2>/dev/null || echo 0); missing=""; for a in alpha bravo zeta foxtrot echo; do grep -q "^  $a:" "$f" 2>/dev/null || missing="$missing $a"; done; [ "$has_block" -ge 1 ] && [ -z "$missing" ] && echo "PASS: agent_role_multipliers 5-agent table complete" || echo "FAIL: agent_role_multipliers issues (block=$has_block, missing=$missing — role-bonus drift, an agent's strategic boost lost)"

   # Windows platform-friction fixes (Section WPF — 2026-04-19)
   # Four root-cause fixes for Windows MSYS bash + Python ergonomics. If any of
   # these regress, the symptoms reappear distributed across many scripts (cp1252
   # mojibake in board posts, bare `python` failures in inline pipes, MIND_AGENT
   # missed by hot-start scripts, --goal vs --goal-id parse errors). Single
   # source of truth: each fix lives in ONE file — do not patch symptoms.
   Check: `.claude/settings.json` `env` block contains `PYTHONUTF8: "1"` and `PYTHONIOENCODING: "utf-8"` — replaces ~40 per-script `sys.stdout.reconfigure(encoding="utf-8")` and `open(..., encoding="utf-8")` patches
   Check: `core/scripts/_paths.sh` creates BOTH `.python-shim/python` and `.python-shim/python3` — bare `python` on PATH is required because LLM-issued inline pipes (`... | python -c`) often drop the `3`
   Check: `core/scripts/bash-agent-inject.py` prepends `export PATH="<shim>:$PATH"` AS PART of the same `expected_prefix` as `export MIND_AGENT=` — combined prefix means bare `python` works in EVERY hook-injected Bash call without requiring the LLM to source `_paths.sh` first
   # MIND_SID export is the ONLY way for skill pseudocode to know the current Claude Code session_id.
   # Removing it re-introduces the 2026-04-20 /stop hang (runner misidentified as observer — 101s idle).
   # Guard: guard-341. Do NOT drop this assertion when refactoring bash-agent-inject.py.
   Check: `core/scripts/bash-agent-inject.py` exports MIND_SID in the same expected_prefix as MIND_AGENT
   Bash: grep -c 'export MIND_SID=' core/scripts/bash-agent-inject.py → verify ≥1
   Check: `core/scripts/session-save-id.sh` invokes the shared `core/scripts/cleanup-stale-bindings.sh` helper for proactive `.active-agent-*` stale-binding sweep (3-signal predicate: mtime>24h + SID-mismatch + heartbeat-stale). Predicate extracted to the helper on 2026-05-12 (g-303-25 B2) so stop-hook.sh and session-save-id.sh share a single source of truth — drift between two copies was the structural risk this extraction removed.
   Check: `core/scripts/_goal-arg-normalize.sh` exists and does NOT contain `[[ "$1" =~ ^g-` (the greedy bare-positional regex was removed because it hijacked free-text flag values like `iteration-close.sh --summary "g-NNN-NN"`). If you re-add it, you re-introduce the bug — instead, use explicit `--goal`/`--goal-id` flags
   Bash (normalizer): grep -L "_goal-arg-normalize.sh" core/scripts/{aspirations-claim,aspirations-release,aspirations-complete-by,aspirations-update-goal,agent-aspirations-update-goal,goal-completion-evidence,iteration-close,predicate-eval,utilization-feedback,background-jobs,pending-agents,meta-impk}.sh — must return empty (all 12 wrappers source the normalizer)

   # NO_AGENT state evidence checks (Section 4T continued)
   Bash: MIND_AGENT="" python3 core/scripts/session.py state get → verify prints "NO_AGENT" (not crash)
   Bash: MIND_AGENT="" python3 core/scripts/session.py persona get → verify prints "no_agent" (not crash)
   Check: `core/scripts/session.py` has `require_agent()` function that exits with clear error
   Check: session.py `cmd_state_set`, `cmd_persona_set`, `cmd_signal_set` all call `require_agent()` before SESSION_DIR access

   # Intent-Satisfied Aspiration Closure checks (Section IS)
   # Framework primitive: aspirations can close when motivation is met by completed goals
   # even if trailing goals are blocked (zombie archival). Gates are structural, not LLM-trusted.
   # See core/config/conventions/aspirations.md "Intent-Satisfied Closure".
   Check: `core/scripts/aspirations.py` defines `TERMINAL_GOAL_STATUSES` module constant including `superseded`
   Check: `core/scripts/aspirations.py` `VALID_GOAL_STATUSES` includes `superseded`
   Check: `core/scripts/aspirations.py` `cmd_complete` accepts `--intent-satisfied` flag
   Check: `core/scripts/aspirations.py` `cmd_update_goal` rejects direct `status=superseded` writes (evidence gate preservation)
   Check: `core/scripts/aspirations.py` `_validate_intent_satisfaction` enforces: evidence cardinality (scope-aware + 50% floor), evidence quality (status=completed + non-empty verification.outcomes), superseded-goals non-terminal, post-supersession closure, rationale >=40 chars, motivation non-empty, rationale token overlap with motivation
   Check: `core/scripts/aspirations-complete-intent.sh` exists and is executable; invokes `aspirations.py complete --intent-satisfied`
   Check: `core/config/aspirations.yaml` has `intent_satisfaction` block with `min_evidence_by_scope` (sprint/project/initiative), `phase_7_4_min_blocked_hours`, `zombie_completion_ratio`
   Check: `core/scripts/aspirations.py` `_load_intent_satisfaction_config` reads yaml directly with NO hardcoded defaults (fails loud on missing block)
   Check: `core/scripts/goal-selector.py` imports `TERMINAL_GOAL_STATUSES` from `aspirations` (single source of truth across scripts)
   Check: `core/scripts/goal-selector.py` uses `SKIP_STATUSES` and `ABANDONED_STATUSES` (derived) — NOT hardcoded literal tuples
   # Runtime import-smoke for the loop-critical re-export chain (g-115-768, rb-947).
   # Line 355 greps the import STATEMENT; this proves the re-exported SYMBOL resolves.
   # 25d6520 over-deleted aspirations.py's `from gates.defer_classifier import
   # STRUCTURED_DEFER_PREFIXES` re-export → goal-selector.py:77 ImportError crashed
   # Phase 2 selection loop-wide, undetected until empty-output forced a stderr re-run.
   # Pure import (no daemon dependency). Verified discriminating: PASS healthy, FAIL on missing symbol.
   Bash (goal-selector-import-smoke): py -3 -c "import sys; sys.path.insert(0,'core/scripts'); from aspirations import TERMINAL_GOAL_STATUSES, STRUCTURED_DEFER_PREFIXES" && echo "PASS: goal-selector.py:77 re-export chain resolves" || echo "FAIL: ImportError — 25d6520 over-deleted-re-export class (rb-947); Phase 2 selection would crash loop-wide"
   Check: `.claude/skills/aspirations-complete-review/SKILL.md` has Phase 7.4 "Intent-Satisfaction Pre-Gate" running BEFORE the fully-complete gate
   Check: `.claude/skills/aspirations-complete-review/SKILL.md` Phase 7.4 triggers only when all unfinished goals are blocked AND `blocked_since` older than `phase_7_4_min_blocked_hours`
   Check: `.claude/skills/aspirations-complete-review/SKILL.md` Phase 7.5.9 "Learning Emission" fires after intent-satisfied closure; calls `reasoning-bank-add.sh`
   Check: `core/scripts/precheck-eval.py` cmd_zombies reads `zombie_completion_ratio` from aspirations.yaml
   Check: `.claude/rules/consolidate-before-expand.md` has rule stating zombies violate consolidation (routes to Phase 7.4 before new aspirations)
   Check: `core/config/conventions/aspirations.md` documents `intent_satisfaction` schema and includes `superseded` in premature-archival-protection terminal-status list
   Bash (positive regression): invoke 9 validator dry-run scenarios (see bravo session-67 journal) — positive, cardinality shortfall, short rationale, no token overlap, evidence not completed, evidence/superseded overlap, unfinished remaining post-supersession, sprint-ceil-wins, project-passes — all behave as designed
   Bash (backwards-compat): `aspirations.py complete asp-xxx` (without --intent-satisfied) still goes through the existing unfinished-goals guard unchanged
   Bash (isolation): `aspirations-update-goal.sh <goal> status superseded` exits non-zero with BLOCKED message (direct-write guard)

   # Prime world-only mode evidence checks (Section 4T continued)
   Check: `prime/SKILL.md` has Phase 0.5 "Agent Mode Detection"
   Check: Phase 0.5 handles NO_AGENT by loading world/program.md, guardrails, reasoning bank (no agent-specific files)
   Check: Phase 0.5 outputs "WORLD PRIME (no agent)" header

   # Session binding evidence checks (Section SB)
   Bash: echo $MIND_AGENT → verify matches the agent directory name (LLM prefix contract)
   Bash: echo $MIND_SID → verify non-empty (PreToolUse[Bash] hook injects this on every Bash call; guard-341)
   Check: `.gitignore` contains `.active-agent-*` pattern
   Check: No `.active-agent` global file exists (eliminated — one mechanism only)
   # The project-root `.latest-session-id` bridge file was retired 2026-04-20 (rb-386).
   # MIND_SID (PreToolUse hook export) is now the single source of truth for skill pseudocode.
   # session-save-id.sh MUST NOT write the bridge file (observer-clobber re-emergence).
   Check: `session-save-id.sh` does NOT write `.latest-session-id`
   Bash: grep -c '\.latest-session-id' core/scripts/session-save-id.sh → verify 0

   # Autocompact carry-forward evidence checks (Section SB continued)
   # Breadcrumb lives in agents/<agent>/session/compact-pending (per-agent, no shared file).
   # The canonical checks for this pattern are in Section SH (below).

   # Session-binding race-prevention checks (Section SB-RP — rb-348, guard-117, guard-320)
   # Three patterns prevent cross-agent SID swap incidents (alpha/bravo 2026-04-19T00:54):
   #   (1) source-field gating — non-compact events MUST NOT consume breadcrumbs
   #   (2) four-witness self-binding match — breadcrumb consumption only when
   #       compact-pending == running-session-id == latest-session-id == .active-agent-<old-SID>
   #   (3) atomic .tmp + mv writes — torn reads of running-session-id flip stop-hook Gate 0
   # If any of these regress, alpha+bravo concurrent operation silently kills both loops.
   Check: `session-save-id.sh` reads `source` from stdin via the same JSON parse as `session_id`
   Bash: grep -c "json.load(sys.stdin).get('source'" core/scripts/session-save-id.sh → verify ≥1
   Check: `session-save-id.sh` gates breadcrumb consumption on source=compact
   Bash: grep -c '\[ "\$SOURCE" = "compact" \]' core/scripts/session-save-id.sh → verify ≥1
   Check: `session-save-id.sh` four-witness match present (compact-pending + running-session-id + latest-session-id + .active-agent)
   Bash: grep -c '_BOUND_AGENT.*=.*_AGENT_NAME' core/scripts/session-save-id.sh → verify ≥1
   Bash: grep -c '_OLD_SID.*=.*_RUNNER_SID\|_OLD_SID.*=.*_LATEST_SID' core/scripts/session-save-id.sh → verify ≥2
   Check: `session-save-id.sh` uses atomic .tmp + mv for running-session-id write
   # Pattern may span lines (echo > .tmp \\\n && mv .tmp final). Counting .tmp
   # references is the line-split-tolerant evidence of atomic-write usage.
   Bash: grep -c "running-session-id\.tmp" core/scripts/session-save-id.sh → verify ≥1
   Bash: grep -c "mv.*running-session-id" core/scripts/session-save-id.sh → verify ≥1
   Check: `session-save-id.sh` uses atomic .tmp + mv for latest-session-id write
   Bash: grep -c "latest-session-id\.tmp" core/scripts/session-save-id.sh → verify ≥1
   Bash: grep -c "mv.*latest-session-id" core/scripts/session-save-id.sh → verify ≥1
   # Runner-legitimacy gate (guard-340): without it, observer-session autocompacts
   # clobber the runner's latest-session-id — 2026-04-20 /stop hang. Simplified
   # 2026-04-20 (rb-386) to two branches: COMPACT_AGENT OR SID==saved-runner. The
   # empty-running-session-id branch was dead — /start is the only legitimate
   # first-writer, and it pair-writes atomically in IDLE Step 3 / UNINITIALIZED C8.
   Check: `session-save-id.sh` gates latest-session-id write on runner-legitimacy (COMPACT_AGENT OR SID == saved runner only)
   Bash: grep -c '_SAVED_RUNNER=' core/scripts/session-save-id.sh → verify ≥1
   Bash: grep -cE 'SID.{0,3}=.{0,3}._SAVED_RUNNER' core/scripts/session-save-id.sh → verify ≥1
   # Anti-regression: the dead [ -z "$_SAVED_RUNNER" ] branch MUST NOT reappear
   # (it would re-open the observer-clobber path on first-write of a new agent).
   Bash: grep -c -- '-z "\$_SAVED_RUNNER"' core/scripts/session-save-id.sh → verify 0
   Check: `session-save-id.sh` uses atomic .tmp + mv for .active-agent-$SID write
   Bash: grep -c "ACTIVE_FILE\.tmp" core/scripts/session-save-id.sh → verify ≥1
   Check: `session-save-id.sh` uses atomic mv-based breadcrumb claim (only one concurrent hook can claim)
   Bash: grep -c 'mv "\$_CP" "\$_CLAIMED"' core/scripts/session-save-id.sh → verify ≥1
   Check: `session-save-id.sh` restores breadcrumb on witness mismatch (rightful owner can claim later)
   Bash: grep -c 'mv "\$_CLAIMED" "\$_CP"' core/scripts/session-save-id.sh → verify ≥1
   Check: do-not-touch comment guards source-gate (prevents future maintainer from removing it)
   Bash: grep -c "DO NOT REMOVE the source-gate" core/scripts/session-save-id.sh → verify ≥1
   # PreCompact serialization gate (added after rb-348 cleanup) — closes the
   # remaining concurrent-autocompact race window without requiring Claude Code
   # to expose previous_session_id. Acquire at PreCompact, release at compact-source
   # SessionStart. Removing the gate or its release line re-opens the swap window.
   Check: `core/scripts/precompact-serialize.sh` exists and is executable
   Bash: test -x core/scripts/precompact-serialize.sh && echo OK || echo MISSING
   Check: `precompact-serialize.sh` uses mkdir for atomic global lock claim
   Bash: grep -c 'mkdir "\$LOCK_DIR"\|mkdir.*autocompact-serialize-lock' core/scripts/precompact-serialize.sh → verify ≥1
   Check: `precompact-serialize.sh` has stale-lock cleanup (>5min orphan recovery)
   Bash: grep -c "HOLDER_TS\|NOW - HOLDER_TS" core/scripts/precompact-serialize.sh → verify ≥1
   Check: `precompact-serialize.sh` is fail-open everywhere (≥3 exit-0 paths — guards + timeout + success)
   Bash: grep -c "exit 0" core/scripts/precompact-serialize.sh → verify ≥3
   Check: `.claude/settings.json` PreCompact lists precompact-serialize.sh BEFORE precompact-checkpoint.sh
   # Source _paths.sh first so python3 resolves through the shim (Windows compat)
   Bash: source core/scripts/_paths.sh && python3 -c "import json,sys; h=json.load(open('.claude/settings.json'))['hooks']['PreCompact'][0]['hooks']; cmds=[x['command'] for x in h]; i=next((n for n,c in enumerate(cmds) if 'precompact-serialize' in c),-1); j=next((n for n,c in enumerate(cmds) if 'precompact-checkpoint' in c),-1); sys.exit(0 if 0<=i<j else 1)" && echo OK || echo FAIL
   Check: `.claude/settings.json` PreCompact serialize hook timeout ≥60s (must accommodate 60s wait window)
   Bash: source core/scripts/_paths.sh && python3 -c "import json,sys; h=json.load(open('.claude/settings.json'))['hooks']['PreCompact'][0]['hooks']; t=next((x['timeout'] for x in h if 'precompact-serialize' in x['command']),0); sys.exit(0 if t>=60 else 1)" && echo OK || echo FAIL
   Check: `session-save-id.sh` releases the lock on every source=compact SessionStart (after the witness walk, NOT inside the witness-match block)
   # Why "after, not inside": reader/assistant sessions autocompact too but
   # don't write compact-pending breadcrumbs. Releasing only on witness match
   # would strand the lock across every reader/assistant compact (rb-356).
   Bash: grep -c 'rm -rf.*autocompact-serialize-lock' core/scripts/session-save-id.sh → verify ≥1
   # The release line must sit OUTSIDE the four-witness `if` block but INSIDE
   # the `if [ "$SOURCE" = "compact" ]` block. Verify by line position:
   # the rm -rf must come AFTER the `unset _CP _CLAIMED ...` cleanup line.
   Bash: awk '/^if \[ "\$SOURCE" = "compact" \]/{f=1} f && /unset _CP _CLAIMED/{u=NR} f && /rm -rf.*autocompact-serialize-lock/{r=NR} END{exit (r>u && u>0)?0:1}' core/scripts/session-save-id.sh && echo OK || echo FAIL
   Check: `core/scripts/test-session-binding.sh` exists and is executable
   Bash: test -x core/scripts/test-session-binding.sh && echo OK || echo MISSING
   Check: regression test passes (15 assertions across 7 scenarios — new-window-hijack, valid four-witness, witness-mismatch, gate-bypassed concurrent-race informational, PreCompact serialization, stale-lock recovery, assistant-mode release + startup non-release)
   Bash: bash core/scripts/test-session-binding.sh 2>&1 | tail -1 | grep -q "0 failed" && echo OK || echo FAIL
   Check: guard-117 has action_hint with source-gate + four-witness enforcement (prevents principle drifting from code)
   Bash: bash core/scripts/guardrails-read.sh --id guard-117 | grep -c "four-witness\|SOURCE.*=.*compact" → verify ≥1
   Check: guard-320 exists (atomic per-agent writes — paired with guard-117)
   Bash: bash core/scripts/guardrails-read.sh --id guard-320 | grep -c "atomic .tmp.*mv\|tmp + mv" → verify ≥1
   Check: guard-325 exists (PreCompact ordering — serialize must run FIRST among PreCompact hooks)
   Bash: bash core/scripts/guardrails-read.sh --id guard-325 | grep -c "precompact-serialize.sh.*FIRST\|index 0" → verify ≥1
   Check: guard-328 exists (acquire/release scope symmetry — release predicate must be superset of acquire predicate; meta lesson)
   Bash: bash core/scripts/guardrails-read.sh --id guard-328 | grep -c "SUPERSET\|release-condition" → verify ≥1
   Check: rb-348 exists (cross-agent SID swap incident — failure_lesson connects to rb-333 family)
   Bash: bash core/scripts/reasoning-bank-read.sh --id rb-348 | grep -c "rb-333-family\|breadcrumb hijack" → verify ≥1
   Check: rb-356 exists (PreCompact serialization gate lesson — preserves multi-agent capability)
   Bash: bash core/scripts/reasoning-bank-read.sh --id rb-356 | grep -c "PreCompact serialization gate\|four-witness walk completes" → verify ≥1
   Check: rb-369 exists (adversarial-review methodology origin — caught the release-scope bug 15/15 tests missed)
   Bash: bash core/scripts/reasoning-bank-read.sh --id rb-369 | grep -c "adversarial.review\|trajectory-class holes\|release.scope" → verify ≥1
   # Lock-symmetry-lint enforces guard-328 at edit-time (PostToolUse hook).
   # Bash-enforced beats LLM-review (rb-333 family) — every Edit/Write to a
   # *.sh file under core/scripts or world/scripts surfaces orphan acquires.
   Check: `core/scripts/lock-symmetry-lint.sh` exists and is executable
   Bash: test -x core/scripts/lock-symmetry-lint.sh && echo OK || echo MISSING
   Check: `core/scripts/lock-symmetry-lint.py` exists (the backend)
   Bash: test -f core/scripts/lock-symmetry-lint.py && echo OK || echo MISSING
   Check: lock-symmetry-lint catches the canonical autocompact pair (acquire in precompact-serialize.sh, release in session-save-id.sh) — variable resolution from `$LOCK_DIR` to `.autocompact-serialize-lock` must succeed
   Bash: source core/scripts/_paths.sh && bash core/scripts/lock-symmetry-lint.sh --check-pair precompact-serialize session-save-id && echo OK || echo FAIL
   Check: lock-symmetry-lint is wired into PostToolUse[Write|Edit|MultiEdit] (3 entries, one per matcher)
   Bash: grep -c "lock-symmetry-lint.sh" .claude/settings.json → verify ≥3
   Check: lock-symmetry-lint surfaces orphan-acquire on a synthetic broken script (negative test — the lint actually fires)
   Bash: TMPDIR=$(mktemp -d) && mkdir -p "$TMPDIR/core/scripts" && printf '#!/usr/bin/env bash\nLOCK_DIR="$X/.test-orphan-lock"\nmkdir "$LOCK_DIR"\n' > "$TMPDIR/core/scripts/orphan.sh" && PROJECT_ROOT="$TMPDIR" python3 core/scripts/lock-symmetry-lint.py 2>&1 | grep -c "WARN no RELEASE" ; rc=$?; rm -rf "$TMPDIR"; [ "$rc" = 0 ] && echo OK || echo FAIL
   Check: tree node hook-source-discrimination registered under hook-authoring-patterns
   Bash: grep -c "hook-source-discrimination" world/knowledge/tree/_tree.yaml 2>/dev/null || (source core/scripts/_paths.sh && grep -c "hook-source-discrimination" "$WORLD_DIR/knowledge/tree/_tree.yaml") → verify ≥1
   Check: tree node markdown file documents all five patterns (1: source-field gating, 2: four-witness, 3: atomic writes, 4: PreCompact serialization, 5: acquire/release scope)
   Bash: source core/scripts/_paths.sh && grep -c "^## Pattern [12345]" "$WORLD_DIR/knowledge/tree/system/hook-authoring-patterns/hook-source-discrimination.md" → verify ≥5
   # Known limitation: Scenario 4 of test-session-binding.sh is informational only
   # (concurrent autocompact race). Resolves when Claude Code surfaces previous_session_id
   # in compact-source SessionStart stdin. Document this so future verify-learning runs
   # don't treat the informational output as a failure.
   Check: `core/scripts/test-session-binding.sh` Scenario 4 is marked informational, not asserted
   Bash: grep -c "Scenario 4 (informational)\|known limitation" core/scripts/test-session-binding.sh → verify ≥1

   # Compaction recovery evidence checks (Section CR)
   # Five-layer mid-task reasoning preservation across autocompact and graceful stop.
   # Write channel must be lit (diary + snapshot calls) or postcompact-restore has nothing to surface.
   Check: `core/config/conventions/compact-recovery.md` documents `phase_progress` dict schema
   Check: `core/config/conventions/compact-recovery.md` documents framework-forced reasoning-snapshot write sites
   Check: `aspirations-verify/SKILL.md` declares `prior_checks: dict (optional, default {})` in Inputs
   Check: `aspirations-verify/SKILL.md` has `IF prior_checks.q1_passed` / `q2_passed` / `q3_scope` / `standard_checks_passed` skip guards
   Bash: grep -c "execution-diary.sh append" .claude/skills/aspirations-verify/SKILL.md → expect ≥5
   Bash: grep -c "execution-diary.sh append" .claude/skills/aspirations-execute/SKILL.md → expect ≥4
   # The pre-verify-auto + pre-stop-resume-auto write sites relocated to
   # aspirations-graceful-stop/SKILL.md during MW-Item-2 extraction (2026-04-18).
   # Assert the SUM across the orchestrator + extracted graceful-stop sub-skill
   # is >= 1 (at least the pre-stop-resume-auto flush must live somewhere) —
   # do not demand an exact count that drifts every extraction.
   Bash: expr $(grep -c "reasoning-snapshot.sh write" .claude/skills/aspirations/SKILL.md) + $(grep -c "reasoning-snapshot.sh write" .claude/skills/aspirations-graceful-stop/SKILL.md) → expect >= 1
   Check: `aspirations/SKILL.md` OR `aspirations-graceful-stop/SKILL.md` Phase -1.4 Graceful Stop Handler reads `checkpoint.phase_progress` and passes `prior_checks` into re-invoked verify
   Check: snapshot flush sites (wherever they live) use `trigger: "pre-verify-auto"` and `trigger: "pre-stop-resume-auto"` (distinguishable from LLM-proactive writes)
   Check: `aspirations-verify/SKILL.md` phase_progress writes route through `bash core/scripts/loop-state-save.sh update --set "phase_progress.<key>=<value>"` (atomic-write guarantee now lives in the wrapper — see Section BE rb-428 extension for the single-writer invariants)
   Check: `aspirations/SKILL.md` LOOP_CONTINUE path deletes `iteration-checkpoint.json` (phase_progress auto-cleans)
   # iteration-close.sh script-enforced anchor cleanup (g-115-221, session-81 framework changes regression).
   # productivity-stop-gate.sh is the ONLY authorized stop-requested setter; immediately after that call,
   # `rm -f "$AGENT_DIR/session/iteration-checkpoint.json"` must run so anchor cleanup is script-enforced
   # rather than LLM-discretionary. Drift here lets stale anchors point at completed goals (g-255-03 incident).
   # NOTE: This check is intentionally narrow — it does not modify or interact with any alpha-367/alpha-368
   # finding regions elsewhere in iteration-close.sh.
   Bash: py -3 -c "import pathlib; t=pathlib.Path('core/scripts/iteration-close.sh').read_text(encoding='utf-8').splitlines(); gate=next((i for i,l in enumerate(t) if 'productivity-stop-gate.sh' in l and 'bash' in l), -1); rm=next((i for i,l in enumerate(t) if 'rm -f \"\$AGENT_DIR/session/iteration-checkpoint.json\"' in l and i>gate), -1); assert gate>=0 and rm>gate and rm-gate<30, f'rm-f checkpoint must follow productivity-stop-gate within 30 lines (gate@{gate+1}, rm@{rm+1})'; print(f'PASS: productivity-stop-gate@line{gate+1} rm-f@line{rm+1}')" → expect PASS (rm-f iteration-checkpoint runs after productivity-stop-gate)
   # Cooperative stop-check (Phase 4.5 skip under /stop) — rb-387 / guard-342 scope.
   Check: `aspirations-execute/SKILL.md` Phase 4.5 has a `### Cooperative Stop-Check (runs first)` block ABOVE the `### Reconciliation (runs when stop NOT pending)` block
   Bash: grep -c "INVARIANT (do not reorder)" .claude/skills/aspirations-execute/SKILL.md → expect ≥1
   Bash: grep -c "session-signal-exists.sh stop-requested" .claude/skills/aspirations-execute/SKILL.md → expect ≥1
   # entry_type allow-list lint (rb-387): every entry_type string used in execution-diary.sh append calls
   # must be in VALID_ENTRY_TYPES from core/scripts/execution-diary.py. The script fails OPEN (WARN + write)
   # so the only catch is at lint time. Scans both .claude/skills/**/SKILL.md AND core/scripts/**/*.py —
   # the allow-list is authoritative for BOTH call sites; do not let the scan drift back to SKILL.md-only.
   # QUOTING: the \" sequences survive bash double-quote → python raw-string intact (smoke-tested). Do NOT
   # edit the escaping without re-running the command locally and confirming OK still fires on a clean tree.
   Bash: py -3 -c "import re, pathlib; valid={'decision','failure','finding','approach_change','observation','state_update','phase_start','phase_end'}; bad=[]; roots=[('.claude/skills','SKILL.md'),('core/scripts','*.py')]; [bad.append((p.name,m)) for root,glob in roots for p in pathlib.Path(root).rglob(glob) for line in p.read_text(encoding='utf-8',errors='ignore').splitlines() for m in re.findall(r'\"entry_type\"\s*:\s*\"([^\"]+)\"', line) if m not in valid]; print('OK' if not bad else 'BAD: '+str(bad))" → expect "OK"
   # Observer-session diary gate (g-115-633): parallel to guard-135/340/517 — observer
   # sessions (assistant/reader coexisting with the autonomous runner) must NOT write
   # to execution-diary.jsonl. Canonical incident: 2026-05-10 bravo observer wrote
   # phase_start phase-4-execute for g-001-01 with no matching phase_end, creating
   # dangling pair detected by phase-cost-report.py. Gate sits at cmd_append AND
   # _emit_phase_marker (the latter is the actual canonical-incident write path —
   # phase_start bypasses cmd_append via _emit_phase_marker).
   Check: `core/scripts/execution-diary.py` has `_is_observer_session()` helper
   Bash: grep -c "^def _is_observer_session" core/scripts/execution-diary.py → verify ≥1
   Check: `execution-diary.py` `cmd_append` calls `_is_observer_session()` and exits 0 on match
   Bash: py -3 -c "import re, pathlib; src=pathlib.Path('core/scripts/execution-diary.py').read_text(encoding='utf-8'); m=re.search(r'def cmd_append\(args\):\s*\n(?:[^\n]*\n){0,4}?[^\n]*_is_observer_session\(\)[^\n]*\n\s*sys\.exit\(0\)', src); print('OK' if m else 'FAIL')" → expect "OK"
   Check: `execution-diary.py` `_emit_phase_marker` calls `_is_observer_session()` and exits 0 on match
   Bash: py -3 -c "import re, pathlib; src=pathlib.Path('core/scripts/execution-diary.py').read_text(encoding='utf-8'); m=re.search(r'def _emit_phase_marker\(kind, phase, iteration, goal_id, note\):\s*\n(?:[^\n]*\n){0,4}?[^\n]*_is_observer_session\(\)[^\n]*\n\s*sys\.exit\(0\)', src); print('OK' if m else 'FAIL')" → expect "OK"
   Check: regression test `tests/test_execution_diary_observer_gate.py` exists
   Bash: test -f core/scripts/tests/test_execution_diary_observer_gate.py && echo OK || echo MISSING
   Bash: py -3 core/scripts/tests/test_execution_diary_observer_gate.py 2>&1 | tail -1 → expect "6/6 tests passed"
   Check: `prime/SKILL.md` Phase 2 reads `board-read.sh --channel reasoning --since 24h`
   Check: `prime/SKILL.md` Phase 4 output includes "Recent musings (cross-agent)" block
   Check: `core/config/conventions/board.md` Message Types table has `musing` row
   Check: `core/config/conventions/board.md` has "Casual Reasoning Channel (`reasoning`)" subsection

   # Agent resolution checks (Section SE)
   # One mechanism: MIND_AGENT env var. Hooks resolve from .active-agent-$SID.
   # The project-root `.latest-session-id` bridge file was retired 2026-04-20 (rb-386).
   # No script anywhere should read or write it anymore — the PreToolUse hook exports
   # MIND_SID directly, and /start reads $MIND_SID instead of the bridge.
   # verify-learning itself is excluded: this file DOES mention the bridge on purpose
   # (anti-regression anchors), so self-matches are expected. Every other consumer must be clean.
   Bash: grep -rl '\.latest-session-id' core/scripts/ .claude/skills/ | grep -v 'verify-learning/SKILL.md' | wc -l → verify 0
   Check: `start/SKILL.md` includes LLM prefix contract instruction (MIND_AGENT=<name>)
   Check: Stop hook recovery message includes agent name and MIND_AGENT prefix instruction
   Check: `start/SKILL.md` has Step 1 that uses `MIND_AGENT=<agent-name>` env prefix
   Check: `start/SKILL.md` RUNNING+autonomous sub-branch is a no-op (no state changes, warning only)

   # Hook-chain hardening (Section HCH)
   # Windows cold-start Python + MSYS path form + hook timeouts form a tight race.
   # See guard-161 and rb-264 for the pattern. These checks prevent regression.

   # Canonical SID->agent resolver (single source of truth)
   Check: `core/scripts/_resolve_agent_from_sid.py` exists
   Check: `_resolve_agent_from_sid.py` defines `RESERVED_AGENT_NAMES = {"world", "meta", "core", "node_modules", "scripts"}`
   Check: `_resolve_agent_from_sid.py` rejects period-bearing names via the `.` in unsafe-chars check (catches .claude, .git, ..)
   Bash: python3 core/scripts/_resolve_agent_from_sid.py "../etc" → verify empty stdout
   Bash: python3 core/scripts/_resolve_agent_from_sid.py "world" → verify empty stdout
   Bash: python3 core/scripts/_resolve_agent_from_sid.py ".claude" → verify empty stdout
   Bash: python3 core/scripts/_resolve_agent_from_sid.py "" → verify empty stdout
   Check: `bash-agent-inject.py` imports the resolver (grep -c "from _resolve_agent_from_sid import" core/scripts/bash-agent-inject.py → ≥1)
   Check: `bash-agent-inject.py` has no inline RESERVED_AGENT_NAMES duplicate (grep -c "RESERVED" core/scripts/bash-agent-inject.py → 0)

   # Windows path safety in hook scripts (guard-161, rb-264)
   Check: `postcompact-restore.sh` uses `$CORE_ROOT/scripts/` for python paths, never `$SCRIPT_DIR=$(cd ... && pwd)` (grep -c 'python3 "\$SCRIPT_DIR' core/scripts/postcompact-restore.sh → 0)
   Check: `postcompact-restore.sh` references `$CORE_ROOT/scripts/_resolve_agent_from_sid.py` (grep -c 'CORE_ROOT/scripts/_resolve_agent_from_sid' core/scripts/postcompact-restore.sh → ≥1)
   Check: `idle-tick.sh` uses `$CORE_ROOT/scripts/` for python paths (grep -c 'python3 "\$SCRIPT_DIR' core/scripts/idle-tick.sh → 0)
   Check: `idle-tick.sh` references `$CORE_ROOT/scripts/_resolve_agent_from_sid.py` (grep -c 'CORE_ROOT/scripts/_resolve_agent_from_sid' core/scripts/idle-tick.sh → ≥1)
   Check: `postcompact-restore.sh` contains comment anchoring the rule (grep -c 'never \$(cd && pwd)\|/c/... which Python' core/scripts/postcompact-restore.sh → ≥1)
   Check: `idle-tick.sh` contains the same anchor (grep -c 'never \$(cd && pwd)\|/c/... which Python' core/scripts/idle-tick.sh → ≥1)

   # Windows Python cold-start warm-up (first hook of session)
   Check: `session-save-id.sh` contains `python3 -c "pass"` warm-up line BEFORE the JSON stdin parse
   Bash: grep -n 'python3 -c "pass"' core/scripts/session-save-id.sh → verify line number is before the json.load line
   Check: no redundant warm-up in downstream hooks: grep -c 'python3 -c "pass"' core/scripts/bash-agent-inject.sh core/scripts/postcompact-restore.sh core/scripts/idle-tick.sh → 0 (only session-save-id.sh needs it)

   # Hook timeouts (.claude/settings.json) -- must accommodate Windows cold-start
   Check: PreToolUse[Bash] hook timeout ≥ 8 (python3 -c "import json; d=json.load(open('.claude/settings.json')); print([h for h in d['hooks']['PreToolUse'] if h.get('matcher')=='Bash'][0]['hooks'][0]['timeout'])" → ≥8)
   Check: SessionStart (no matcher) session-save-id.sh timeout ≥ 8 (same approach, first SessionStart entry)

   # /start SKILL.md belt-and-suspenders explicit MIND_AGENT prefix
   # Defense against PreToolUse hook racing the binding file write.
   Check: `/start SKILL.md` has explicit `MIND_AGENT=<agent-name>` prefix on state-writing calls (grep -c 'MIND_AGENT=<agent-name> bash core/scripts/session-' .claude/skills/start/SKILL.md → ≥10)

   # Dead-code guard: if postcompact-restore.py starts reading stdin, the shell wrapper needs to re-feed it
   Check: `postcompact-restore.py` does NOT read sys.stdin (grep -c 'sys\.stdin\|stdin\.\|input()' core/scripts/postcompact-restore.py → 0). If this ever becomes ≥1, postcompact-restore.sh needs `<<< "$INPUT"` or equivalent tee pattern restored.

   # Observer session checks (Section OBS)
   # Observer sessions (reader/assistant started during RUNNING) coexist with the runner.
   # They MUST NOT touch agent-state, agent-mode, persona-active, or running-session-id.
   Check: `start/SKILL.md` RUNNING branch splits on requested mode (autonomous vs reader/assistant)
   Check: `start/SKILL.md` RUNNING+reader/assistant sub-branch does NOT call `session-state-set.sh`
   Check: `start/SKILL.md` RUNNING+reader/assistant sub-branch does NOT call `session-mode-set.sh`
   Check: `start/SKILL.md` RUNNING+reader/assistant sub-branch does NOT call `session-persona-set.sh`
   Check: `start/SKILL.md` RUNNING+reader/assistant sub-branch does NOT write to `running-session-id`
   # Observer-clobber guard (guard-340): observer MUST NOT write agents/<agent>/session/latest-session-id either.
   # Root cause of the 2026-04-20 /stop hang was observer overwriting runner's SID file.
   Check: `start/SKILL.md` RUNNING+reader/assistant sub-branch does NOT write to `agents/<agent>/session/latest-session-id`
   Bash: awk '/^#### RUNNING \+ requested mode is/{f=1} f && /^### IDLE/{f=0} f' .claude/skills/start/SKILL.md | grep -c 'session/latest-session-id.tmp\|> agents/<agent-name>/session/latest-session-id' → verify 0
   # Same rule for IDLE reader/assistant and UNINITIALIZED before autonomous fork:
   # only the runner-claim site (IDLE Step 3 / UNINITIALIZED C8) writes latest-session-id.
   Check: `start/SKILL.md` IDLE Step 0 does NOT write `agents/<agent>/session/latest-session-id` (moved to Step 3 pair-write)
   Bash: grep -c '> agents/<agent-name>/session/latest-session-id.tmp\|> agents/<agent-name>/session/latest-session-id' .claude/skills/start/SKILL.md → verify exactly 2 (IDLE Step 3 + UNINITIALIZED C8 only)
   Check: IDLE Step 3 (`start/SKILL.md`) and UNINITIALIZED C8 (`core/config/start-phase-c.md` digest, extracted g-115-1723-a) use `$MIND_SID` for the canonical runner-claim pair-write
   Bash: grep -c 'echo "\$MIND_SID" > agents/<agent-name>/session/running-session-id\.tmp' .claude/skills/start/SKILL.md → verify ≥2
   # Visible-halt guard (rb-386): the pair-write must FAIL-VISIBLY on empty MIND_SID,
   # not silently via `[ -n "$MIND_SID" ] && …` (which created invisible no-ops that
   # left the runner files unwritten and `/stop` later misdetecting runner-vs-observer).
   Check: `start/SKILL.md` pair-write sites use `if [ -z "$MIND_SID" ]; then echo ERROR; exit 1; fi` (visible halt)
   Bash: grep -c 'if \[ -z "\$MIND_SID" \]; then echo "ERROR:EMPTY_MIND_SID"' .claude/skills/start/SKILL.md → verify ≥3 (observer Step 0, IDLE Step 3, UNINITIALIZED C8; IDLE Step 0 + A2 share the same pattern so total ≥5 acceptable)
   # Anti-regression: no silent-gate pattern allowed on SID writes
   Bash: grep -c '\[ -n "\$MIND_SID" \] &&' .claude/skills/start/SKILL.md → verify 0
   # Visible-halt guard for session-mode-set.sh (g-115-1032, 2026-05-21): mode-set
   # failures at any of the 4 /start sites (IDLE Step 2, UNINITIALIZED Phase C
   # reader C4, assistant C8, autonomous C9) must HALT, not silently fall through.
   # Without HALT, a failed assistant/autonomous mode-set lets the agent land in
   # the reader disk-default while the success message lies about the actual mode
   # (silent capability mismatch). Mirrors the session-state-set.sh HALT contract
   # added 2026-05-20 (tag G2).
   Check: `start/SKILL.md` session-mode-set.sh sites carry g-115-1032 HALT blocks (4 sites)
   Bash: grep -c 'HALT ON NON-ZERO EXIT (g-115-1032' .claude/skills/start/SKILL.md → verify ≥4
   Check: `start/SKILL.md` RUNNING+reader/assistant sub-branch DOES bind session (`.active-agent-<SID>`)
   Check: `session-state.md` has "Observer Sessions" section with rules and concurrency safety
   Check: `stop-hook.sh` Gate 0 allows non-runner SIDs (HOOK_SID != RUNNER_SID → exit 0)
   Check: CLAUDE.md User Control Commands table shows `RUNNING*` for reader and assistant rows

   # Stop hook integrity checks (Section SH)
   # The stop hook has ONE job: BLOCK unconditionally when RUNNING with no stop signal.
   # No counter. No tiers. No safety valve.
   Check: `.claude/settings.json` Stop array has exactly ONE hook entry
   Check: `.claude/settings.json` Stop hook command is `bash core/scripts/stop-hook.sh`
   Check: `.claude/settings.json` StopFailure hook exists with `bash core/scripts/stop-failure-hook.sh`
   Check: `.claude/settings.json` Stop hook timeout >= 30s (rb-453 floor — 8s revert silently re-opens post-iteration loop-kill window). Bash: `t=$(py -3 -c "import json; d=json.load(open('.claude/settings.json',encoding='utf-8')); print(d['hooks']['Stop'][0]['hooks'][0]['timeout'])"); test "$t" -ge 30 && echo "PASS: Stop timeout=$t (>=30)" || { echo "FAIL: Stop timeout=$t < 30 — regression to short timeout"; exit 1; }`
   Check: `stop-hook.sh` uses `export MIND_AGENT=` (not `_A=` variable expansion)
   Check: `stop-hook.sh` has NO counter increment (`session-counter-increment` not called)
   Check: `stop-hook.sh` has NO tier logic (no `Tier 1`, `Tier 4` reference)
   Check: `stop-hook.sh` has NO safety valve (no `COUNT -ge`)
   Check: `stop-hook.sh` block message contains `Skill('aspirations')` (exact tool syntax)
   Check: `stop-hook.sh` has early exit when HOOK_SID is empty
   Check: `stop-hook.sh` has early exit when HOOK_AGENT is empty
   Check: `stop-failure-hook.sh` writes crash marker to `agents/<agent>/session/crash-marker`
   Check: `boot/SKILL.md` Phase -2.5 detects and cleans crash-marker
   Check: `boot/SKILL.md` has NO `session-counter-clear.sh` calls
   Check: `aspirations/SKILL.md` Phase -0.5 has NO `session-counter-clear.sh`
   Check: `recover/SKILL.md` does NOT exist (skill deleted — /stop is the only way to stop)
   Check: `session.py` stop-loop guard rejects if RUNNING (no counter check)
   Check: No `core/scripts/capture-insights.sh` file exists (deleted)
   Check: `core/scripts/capture-insights.py` still exists (preserved for inline use by stop-hook.sh)

   # Stop hook lifecycle checks (Section SH continued — running-session-id)
   # running-session-id is the anchor file that tells the stop hook which session
   # is the autonomous loop runner. ONE creator (/start), ONE reader (stop hook),
   # ONE syncer (session-save-id.sh on autocompact), ONE deleter (/stop).
   Check: `start/SKILL.md` IDLE autonomous path writes `running-session-id` after `session-state-set.sh RUNNING`
   Check: `core/config/start-phase-c.md` (Phase C digest, extracted by g-115-1723-a) UNINITIALIZED autonomous path (C8) writes `running-session-id` after `session-state-set.sh RUNNING`
   Check: `aspirations-graceful-stop/SKILL.md` D-step deletes `running-session-id` (symmetric with /start creation; extracted from former stop/SKILL.md step 4b when Phase -1.4 moved to the graceful-stop sub-skill, Magic-Wand Item 2)
   Bash: grep -c 'rm.*running-session-id' .claude/skills/aspirations-graceful-stop/SKILL.md -> verify returns >= 1 (extraction preserves delete)
   Check: `boot/SKILL.md` Phase -1.5 whitelist includes `running-session-id` (boot must NOT delete it)
   Bash: grep -c 'rm.*running-session-id' .claude/skills/boot/SKILL.md -> verify returns 0 (boot never deletes it)
   Bash: grep -c 'running-session-id' .claude/skills/aspirations/SKILL.md -> verify returns 0 (aspirations does not manage it)
   Check: `session-save-id.sh` syncs `running-session-id` on autocompact (update-only, not create)
   Bash: grep -c 'running-session-id' core/scripts/session-save-id.sh → verify >= 3 (sync logic references the file at least 3 times)
   Check: `stop-hook.sh` comment says "set by /start" (not "Phase -0.5")
   Bash: grep -cE 'running-session-id is set by /start' core/scripts/stop-hook.sh → verify >= 1 (canonical attribution comment present)

   # Loop survival checks (Section SH continued — LOOP_CONTINUE + Return Protocol)
   # Every iteration must end with a tool call. LOOP_CONTINUE is the mechanical heartbeat.
   # Sub-skills must never produce text-only output as their last action.
   Check: `aspirations/SKILL.md` has "## Loop Continuation Protocol (LOOP_CONTINUE)" section
   Check: `aspirations/SKILL.md` uses `LOOP_CONTINUE` (not bare `continue`) in all code paths
   Bash: grep -c "^    continue$" .claude/skills/aspirations/SKILL.md 2>/dev/null → verify returns 0
   Check: `aspirations/SKILL.md` Phase 12 (learning gate) is the LAST phase, with comment "Control does NOT return here"
   Check: `aspirations-learning-gate/SKILL.md` has "## Loop Re-Entry" section with LOOP_CONTINUE
   Check: `aspirations-learning-gate/SKILL.md` says "NEVER end this skill with text output"
   Check: `respond/SKILL.md` Step 3a has step 7: `Skill('aspirations') with args='loop'`
   Check: `respond/SKILL.md` Step 3a has step 6: "NEVER ask the user a question and wait"
   Check: `stop-hook.sh` has `LOG=` variable (audit log enabled)
   # 2026-05-19 (plan v1 step 0.15-0.16): stop-hook log + timing relocated from
   # PROJECT_ROOT/ to core/logs/ (the canonical telemetry sink, already
   # gitignored). Replace gitignore-presence check with writer-sink check.
   Bash (stop-hook-writer-sink): grep -q 'core/logs/stop-hook.log' core/scripts/stop-hook.sh && grep -q 'core/logs/stop-hook-timing.jsonl' core/scripts/stop-hook.sh && echo "PASS: stop-hook writers point at core/logs/" || echo "FAIL: stop-hook writers still target PROJECT_ROOT — relocation regressed"
   Check: `settings.json` allow has `Write(**/.claude/skills/**)` (double-star, not single-star)

   # Section OSG: Output-style gate Layer-B (autonomous + Explanatory) loop-kill defense (rb-629, guard-454, g-115-316)
   # Layer A is .claude/rules/return-protocol.md (LLM honor-system).
   # Layer B is core/scripts/output-style-gate.sh — script-enforced refusal at /start
   # when mode=autonomous + outputStyle=Explanatory (4 silent loop deaths 2026-04-29).
   # Layer C is stop-hook trailing-text-detector.py (post-hoc audit).
   # Layer D is g-115-315 recurring sweep (24h transcript scan).
   # If Layer B regresses, the explanatory-style trailing Insight blocks resume
   # killing the loop after iteration-close productivity-check.
   Check: `core/scripts/output-style-gate.sh` exists. Bash: `test -f core/scripts/output-style-gate.sh && echo PASS || { echo "FAIL: output-style-gate.sh missing — Layer B regressed"; exit 1; }`
   Check: `.claude/skills/start/SKILL.md` references output-style-gate twice (IDLE branch + Phase C7.7). Bash: `n=$(grep -c output-style-gate .claude/skills/start/SKILL.md); test "$n" -eq 2 && echo "PASS: $n references" || { echo "FAIL: expected 2 references in start/SKILL.md, got $n"; exit 1; }`
   Check: `world/scripts/output-style-mode-guard.sh` exists in the world directory. Bash: `source core/scripts/_paths.sh && test -f "$WORLD_DIR/scripts/output-style-mode-guard.sh" && echo PASS || { echo "FAIL: output-style-mode-guard.sh missing in $WORLD_DIR/scripts/"; exit 1; }`
   Check: output-style-gate sources _paths.sh and exits 0 fail-open when world gate missing. Bash: `grep -cE 'fail-open|layer-B disabled' core/scripts/output-style-gate.sh` must be >= 2.
   Check: output-style-gate uses `exec bash "$GATE" "$@" --style` so the wrapper's --style is the LAST arg (authoritative). Bash: `grep -c 'exec bash "$GATE" "$@" --style' core/scripts/output-style-gate.sh` must be >= 1.
   Check: guard-454 is active (output-style autonomous-loop-kill HIGH severity). Bash: `bash core/scripts/guardrails-read.sh --id guard-454 | grep -q '"status": "active"' && echo PASS || { echo "FAIL: guard-454 not active"; exit 1; }`
   Check: rb-629 is active (Explanatory + autonomous loop-kill incident). Bash: `bash core/scripts/reasoning-bank-read.sh --id rb-629 | grep -q '"status": "active"' && echo PASS || { echo "FAIL: rb-629 not active"; exit 1; }`
   # Behavioral probe: the gate must REFUSE autonomous + Explanatory and ACCEPT --override.
   # Saves and restores .claude/settings.local.json so the probe is non-destructive.
   Bash: BAK=$(mktemp) && cp .claude/settings.local.json "$BAK" && py -3 -c "import json,pathlib; p=pathlib.Path('.claude/settings.local.json'); d=json.loads(p.read_text(encoding='utf-8')); d['outputStyle']='Explanatory'; p.write_text(json.dumps(d,indent=2),encoding='utf-8')" && bash core/scripts/output-style-gate.sh --mode autonomous >/dev/null 2>&1; rc1=$?; bash core/scripts/output-style-gate.sh --mode autonomous --override "verify-learning probe" >/dev/null 2>&1; rc2=$?; bash core/scripts/output-style-gate.sh --mode reader >/dev/null 2>&1; rc3=$?; cp "$BAK" .claude/settings.local.json; rm "$BAK"; test "$rc1" -eq 2 && test "$rc2" -eq 3 && test "$rc3" -eq 0 && echo "PASS: gate refuses(2)/overrides(3)/passes-reader(0)" || { echo "FAIL: gate exit codes drifted: autonomous=$rc1 (want 2), override=$rc2 (want 3), reader=$rc3 (want 0)"; exit 1; }

   # Section 58a08f3: LifingPolls fresh-eyes-pass dead-fallback removals (g-115-441, g-115-474)
   # All three were silent-fallback paths that masked real failure modes.
   # If any regresses, the original LifingPolls signal is silently lost.
   Check: `core/scripts/strategic-pulse-detectors.py` _all_goals_blocked_or_deferred MUST check deferred_until (refactor-prone — high regression risk). Bash: `grep -A 10 '_all_goals_blocked_or_deferred' core/scripts/strategic-pulse-detectors.py | grep -q deferred_until && echo PASS || { echo "FAIL: deferred_until not in _all_goals_blocked_or_deferred OR-chain — regression of 58a08f3 fix"; exit 1; }`
   Check: `core/scripts/defer-date-extractor.py` MUST NOT contain `RELATIVE_UNITS.get(unit) or` fallback chain (direct dict access only). Bash: `grep -q 'RELATIVE_UNITS.get(unit) or' core/scripts/defer-date-extractor.py && { echo "FAIL: RELATIVE_UNITS.get fallback chain re-introduced — masks unknown units"; exit 1; } || echo PASS`
   Check: `core/scripts/chronic-friction-aggregator.py` MUST NOT contain `fall back to top-N regardless` comment/behavior (empty themes list when nothing repeats is correct). Bash: `grep -q 'fall back to top-N regardless' core/scripts/chronic-friction-aggregator.py && { echo "FAIL: fall back to top-N regardless re-introduced — masks empty-themes-list correct path"; exit 1; } || echo PASS`

   # Section RC: read-clobber anti-pattern in core/scripts shell wrappers (g-115-693, rb-903, guard-542, 2026-05-13)
   # Bash `read -r var < file || var=""` clobbers $var when the source file lacks
   # a trailing newline. `read` exits 1 on EOF-without-newline despite ALREADY
   # populating $var, so the `|| var=""` branch fires and wipes the value the
   # caller just read. Canonical incident: tree-sync-check.sh:87 pre-2026-05-13
   # cleanup — agent name read from .active-agent-<sid> bindings was silently
   # blanked when the file lacked a trailing newline, breaking PostToolUse[Edit]
   # tree front-matter sync. Correct form: `read -r var < file || true` (ignore
   # exit) or `[ -f file ] && read -r var < file` (guard). Currently zero call
   # sites — this check prevents regression.
   Bash: matches=$(grep -rE 'read -r [a-zA-Z_][a-zA-Z0-9_]* < .* \|\| [a-zA-Z_][a-zA-Z0-9_]*=""' core/scripts/ 2>/dev/null); test -z "$matches" && echo "PASS: no read-clobber pattern in core/scripts" || { echo "FAIL: read-clobber pattern found — clobbers \$var when source file lacks trailing newline; use \`|| true\` or guard with \`[ -f file ] && read\`"; echo "$matches"; exit 1; }
   Check: guard-542 is active. Bash: `bash core/scripts/guardrails-read.sh --id guard-542 | grep -q '"status": "active"' && echo PASS || { echo "FAIL: guard-542 not active"; exit 1; }`
   Check: rb-903 is active. Bash: `bash core/scripts/reasoning-bank-read.sh --id rb-903 | grep -q '"status": "active"' && echo PASS || { echo "FAIL: rb-903 not active"; exit 1; }`

   # Section UPSS: unblock-parent-status-sweep wiring (g-115-698, g-250-76, rb-908, 2026-05-13)
   # Layer D auto-Unblock filed at defer-time can outlive its parent goal when the
   # parent lands in a terminal non-execution state (skipped/completed/superseded/
   # archived). The unblock-parent-status-sweep is the re-probe that auto-skips
   # orphaned Unblocks. Three wiring points MUST stay intact:
   #   (1) aspirations-precheck Phase 0.5b.7 calls the .sh wrapper, with .py file documented
   #   (2) budget-meter sweep_tier() classifies unblock-parent-status-sweep as deferrable
   #   (3) test_unblock_parent_status_sweep.py defines exactly 12 tests (canonical g-250-73
   #       shape + three extraction paths + idempotency + terminal-state set + title-prefix
   #       discipline). A regression that drops a test silently weakens the contract.
   Check: Phase 0.5b.7 references both .sh wrapper and .py file. Bash: `grep -q 'unblock-parent-status-sweep.sh' .claude/skills/aspirations-precheck/SKILL.md && grep -q 'unblock-parent-status-sweep.py' .claude/skills/aspirations-precheck/SKILL.md && echo PASS || { echo "FAIL: Phase 0.5b.7 missing .sh wrapper OR .py file reference in aspirations-precheck SKILL.md"; exit 1; }`
   Check: budget-meter sweep_tier classifies unblock-parent-status-sweep as deferrable. Bash: `grep -E 'pending-questions-sweep\|.*\|unblock-parent-status-sweep\|.*\)' core/scripts/aspirations-precheck-budget-meter.sh >/dev/null && echo PASS || { echo "FAIL: unblock-parent-status-sweep not in deferrable case of sweep_tier()"; exit 1; }`
   Check: test_unblock_parent_status_sweep.py defines exactly 12 tests. Bash: `count=$(grep -cE '^def test_' core/scripts/tests/test_unblock_parent_status_sweep.py 2>/dev/null); test "$count" = "12" && echo "PASS: 12 tests" || { echo "FAIL: expected 12 tests, got $count"; exit 1; }`

   # Section UIP: Unblock-intake-probe wiring (g-115-1017, rb-1111, 2026-05-22)
   # Fast intake-time probe for Unblock goals at claim time. Parses
   # failure_reason for named artifacts (commit hashes / file:line refs /
   # function names) and emits a probable-fix-landed | bug-still-present |
   # inconclusive verdict. Canonical incident: g-115-985 (filed against
   # loop-state-save.py:82, commit a49e4805 fix landed before pickup).
   # Three wiring points MUST stay intact:
   #   (1) aspirations-execute SKILL.md Phase 4 invokes the .sh wrapper
   #       between aspirations-update-goal.sh status in-progress and the
   #       Intelligent Retrieval Protocol load.
   #   (2) core/scripts/unblock-intake-probe.py + .sh both exist and are
   #       executable.
   #   (3) test_unblock_intake_probe.py defines >=9 test cases covering
   #       title gate, age gate, commit-ancestor, file-line-out-of-range,
   #       file-missing, no-artifacts, goal-not-found, force-bypass,
   #       and always-exit-zero.
   #   (4) core/config/aspirations.yaml carries the unblock_intake_probe
   #       block with enabled + min_age_hours keys.
   Check: aspirations-execute Phase 4 invokes the probe. Bash: `grep -q 'unblock-intake-probe.sh' .claude/skills/aspirations-execute/SKILL.md && echo PASS || { echo "FAIL: aspirations-execute SKILL.md missing unblock-intake-probe.sh wiring — g-115-1017 regressed"; exit 1; }`
   Check: probe wrapper + python script both exist. Bash: `test -f core/scripts/unblock-intake-probe.py && test -f core/scripts/unblock-intake-probe.sh && echo PASS || { echo "FAIL: unblock-intake-probe script(s) missing — g-115-1017 regressed"; exit 1; }`
   Check: test_unblock_intake_probe.py defines at least 9 test cases. Bash: `count=$(grep -cE '^def test_' core/scripts/tests/test_unblock_intake_probe.py 2>/dev/null); test "$count" -ge "9" && echo "PASS: $count tests" || { echo "FAIL: expected >=9 tests, got $count"; exit 1; }`
   Check: aspirations.yaml has unblock_intake_probe block with enabled + min_age_hours. Bash: `py -3 -c "import yaml,pathlib; d=yaml.safe_load(pathlib.Path('core/config/aspirations.yaml').read_text(encoding='utf-8')); blk=d.get('unblock_intake_probe') or {}; ok='enabled' in blk and 'min_age_hours' in blk; print('PASS' if ok else f'FAIL: unblock_intake_probe block missing or incomplete: {blk}')" | grep -q PASS && echo PASS || { echo "FAIL: aspirations.yaml unblock_intake_probe block missing or incomplete"; exit 1; }`

   # Section DER: Daemon Endpoint Registry pre-commit gate (g-115-802, g-115-807, 2026-05-16)
   # Layer B sibling to check-no-python-cli-fallback.sh. Verifies
   # mind_api/src/endpoints/__init__.py load_all imports resolve to existing
   # modules in the post-commit tree. Prevents the ImportError class observed
   # 2026-05-15 (renamed endpoint modules landed without updated __init__.py;
   # daemon refused to start; 35 wrappers refused work).
   # Runs in audit mode here — catches import-vs-filesystem drift between
   # sessions even when no commit is attempted. The pre-commit hook is the
   # primary defense; this audit catches in-progress refactors.
   Check: `core/scripts/check-mind-api-endpoint-registry.py` exists. Bash: `test -f core/scripts/check-mind-api-endpoint-registry.py && echo PASS || { echo "FAIL: check-mind-api-endpoint-registry.py missing — g-115-807 regressed"; exit 1; }`
   Check: `core/githooks/pre-commit` chains BOTH gates (CLI-fallback + endpoint-registry). Bash: `grep -q 'check-no-python-cli-fallback.sh' core/githooks/pre-commit && grep -q 'check-mind-api-endpoint-registry.py' core/githooks/pre-commit && echo PASS || { echo "FAIL: pre-commit missing one or both gates"; exit 1; }`
   Check: `core/githooks/pre-commit` uses `set -e` (so any gate failure aborts the commit). Bash: `grep -q '^set -e' core/githooks/pre-commit && echo PASS || { echo "FAIL: pre-commit missing 'set -e' — silent gate failures possible"; exit 1; }`
   # Behavioral audit: run the gate against the current working tree. Catches
   # drift introduced since last commit (mid-refactor stage where __init__.py
   # references modules that don't exist yet, OR exist but were renamed).
   Bash: case "$(uname -s 2>/dev/null || echo unknown)" in MINGW*|MSYS*|CYGWIN*) PY="py -3" ;; *) PY="python3" ;; esac; $PY core/scripts/check-mind-api-endpoint-registry.py --audit && echo "PASS: all load_all imports resolve" || { echo "FAIL: endpoint-registry audit found missing modules - daemon would ImportError on next start"; exit 1; }

   # Section DPA: Daemon Per-Agent Header Gate (g-115-957, g-115-1153, 2026-05-22)
   # The store endpoint (mind_api/src/endpoints/store.py) routes ALL agent-private
   # store writes (reasoning-bank, guardrails, pattern-signatures, journal, etc.)
   # and MUST call _require_agent_header(ctx) early in its POST handler so a
   # missing/invalid AYOAI-Agent header rejects the write with a 400 before any
   # store-side mutation. g-115-957 added the gate; this check pins it.
   #
   # Scope is intentionally narrow: ONLY store.py. The original Apply description
   # listed store/aspirations_write/pipeline_write/wm/experience as candidates,
   # but pre-flight investigation (2026-05-22 iter 20) confirmed:
   #   - pipeline_write.py does not exist
   #   - wm.py is a read-only GET endpoint (no write to gate)
   #   - experience.py is a read-only GET endpoint (no write to gate)
   #   - aspirations_write.py uses its own multi-gate pipeline (capability-route-gate,
   #     goal-duplication-gate, stale-read-gate, etc.) keyed off intended_agent
   #     field validation, not header gating
   # Broadening the check to those files would FAIL immediately with no
   # corresponding regression to catch. If the gate is later extended to other
   # write endpoints, append them to the grep list below.
   Check: `mind_api/src/endpoints/store.py` calls `_require_agent_header` in its POST handler. Bash (store-py-agent-header-gate): grep -q '_require_agent_header' mind_api/src/endpoints/store.py && echo "PASS: store.py invokes _require_agent_header" || { echo "FAIL: store.py missing _require_agent_header gate - g-115-957 regressed; per-agent store writes can land without AYOAI-Agent header validation"; exit 1; }

   # Section DOP: Daemon Orphan Prevention (g-115-764 v3, g-115-1112, 2026-05-22)
   # The 2026-05-22 snapshot found 36 orphan daemon pairs (72 processes)
   # accumulated over 204 spawns in 37 hours (~17% kill-failure rate). Root
   # cause: the powershell tree-kill in mind-api-start.sh + _runtime.sh looked
   # up the py.exe launcher PID via Get-CimInstance Win32_Process -Filter
   # "ProcessId=<child>" .ParentProcessId. When SIGTERM gracefully killed the
   # python.exe child BEFORE the powershell tree-kill ran, Get-CimInstance
   # returned null and the inner if-block (which contained BOTH the parent
   # AND the child kill) was skipped — orphaning the py.exe parent.
   #
   # v3 fix:
   #   1. Daemon writes its py.exe parent PID to daemon.parent.pid at startup
   #      (mind_api/src/lifecycle.py + mind_api/src/server.py).
   #   2. Kill paths read the parent PID from disk and force-kill BOTH PIDs
   #      by KNOWN values, no lookup chain (mind-api-start.sh _force_kill_tree
   #      + _runtime.sh rt_force_kill_tree). This is the authoritative reap.
   #   3. Pre-spawn orphan sweep helpers EXIST (_sweep_orphan_daemons /
   #      rt_sweep_orphan_daemons) but are NOT called implicitly with empty
   #      keep-args anymore — that would kill EVERY mind_api.src process
   #      system-wide and collide with other repos running the same daemon
   #      (same cmdline, Win32_Process exposes no cwd/env discriminator).
   #      kill-by-KNOWN-PIDs in step 2 is repo-safe by construction.
   #   4. Standalone user-invocable diagnostic: daemon-orphan-sweep.sh
   #      reports state, --clean reaps orphans, --strict exits 1 on detection.
   #      Note: --clean is cross-repo dangerous; user invokes it knowingly.
   #   5. PowerShell results are LOGGED to spawn.log (no more
   #      >/dev/null 2>&1 silently swallowing failures).
   #
   # The checks below guard the four files that participate in the fix.
   # A future edit that removes parent-PID writing OR replaces the new kill
   # path with the old Get-CimInstance lookup would regress the class.
   Check: `mind_api/src/lifecycle.py` exports `parent_pid_file()` and `read_parent_pid()`. Bash: `grep -q 'def parent_pid_file' mind_api/src/lifecycle.py && grep -q 'def read_parent_pid' mind_api/src/lifecycle.py && echo "PASS: lifecycle.py exports parent_pid_file + read_parent_pid" || { echo "FAIL: lifecycle.py missing parent_pid_file or read_parent_pid — daemon orphan fix (g-115-764 v3) regressed"; exit 1; }`
   Check: `mind_api/src/lifecycle.py::write_pid_and_port_atomic` accepts `parent_pid` kwarg. Bash: `grep -A 3 '^def write_pid_and_port_atomic' mind_api/src/lifecycle.py | grep -q 'parent_pid' && echo "PASS: write_pid_and_port_atomic accepts parent_pid" || { echo "FAIL: write_pid_and_port_atomic missing parent_pid parameter — daemon will not write daemon.parent.pid (g-115-764 v3 regression)"; exit 1; }`
   Check: `mind_api/src/server.py` passes `os.getppid()` to `write_pid_and_port_atomic`. Bash: `grep -A2 'write_pid_and_port_atomic' mind_api/src/server.py | grep -q 'parent_pid' && echo "PASS: server.py passes parent_pid at startup" || { echo "FAIL: server.py not passing parent_pid — daemon.parent.pid will not be populated"; exit 1; }`
   Check: `core/scripts/mind-api-start.sh` defines `_force_kill_tree` AND `_sweep_orphan_daemons`. Bash: `grep -q '^_force_kill_tree()' core/scripts/mind-api-start.sh && grep -q '^_sweep_orphan_daemons()' core/scripts/mind-api-start.sh && echo "PASS: mind-api-start.sh has bulletproof kill helpers" || { echo "FAIL: mind-api-start.sh missing _force_kill_tree or _sweep_orphan_daemons (g-115-764 v3 regression)"; exit 1; }`
   Check: `core/scripts/_runtime.sh` defines `rt_force_kill_tree` AND `rt_sweep_orphan_daemons`. Bash: `grep -q '^rt_force_kill_tree()' core/scripts/_runtime.sh && grep -q '^rt_sweep_orphan_daemons()' core/scripts/_runtime.sh && echo "PASS: _runtime.sh has bulletproof kill helpers" || { echo "FAIL: _runtime.sh missing rt_force_kill_tree or rt_sweep_orphan_daemons (g-115-764 v3 regression)"; exit 1; }`
   Check: `core/scripts/mind-api-start.sh::_force_kill_tree` does NOT use the old `.ParentProcessId` lookup pattern (g-115-764 v3 regression check). Bash: `awk '/^_force_kill_tree\(\)/,/^}/' core/scripts/mind-api-start.sh | grep -q 'ParentProcessId' && { echo "FAIL: _force_kill_tree still uses Get-CimInstance .ParentProcessId lookup — the silent-no-op bug (g-115-764) regressed"; exit 1; } || echo "PASS: _force_kill_tree uses known-PID kill (no ParentProcessId lookup)"`
   Check: `core/scripts/_runtime.sh::rt_force_kill_tree` does NOT use the old `.ParentProcessId` lookup pattern. Bash: `awk '/^rt_force_kill_tree\(\)/,/^}/' core/scripts/_runtime.sh | grep -q 'ParentProcessId' && { echo "FAIL: rt_force_kill_tree still uses Get-CimInstance .ParentProcessId lookup — the silent-no-op bug (g-115-764) regressed"; exit 1; } || echo "PASS: rt_force_kill_tree uses known-PID kill (no ParentProcessId lookup)"`
   Check: `core/scripts/daemon-orphan-sweep.sh` exists and is executable. Bash: `test -x core/scripts/daemon-orphan-sweep.sh && echo "PASS: daemon-orphan-sweep.sh present + executable" || { echo "FAIL: daemon-orphan-sweep.sh missing or not executable (g-115-764 v3 user-facing diagnostic)"; exit 1; }`
   Check: `core/scripts/tests/test_daemon_orphan_prevention.py` exists. Bash: `test -f core/scripts/tests/test_daemon_orphan_prevention.py && echo "PASS: regression test present" || { echo "FAIL: test_daemon_orphan_prevention.py missing — no regression coverage for g-115-764 v3"; exit 1; }`
   # Behavioral check: run the sweep in report-only mode. Exits 0 always;
   # we capture and inspect output for orphans. Skipped on POSIX (orphan
   # class is Windows-specific). On multi-repo machines (sibling repos
   # running concurrent mind_api daemons), the sweep CORRECTLY reports
   # other repos' daemons as "orphans of THIS repo's published state" —
   # which is informational, not a failure. The check passes if THIS
   # repo's own daemon (daemon.pid + daemon.parent.pid) is KEPT.
   Bash (daemon-no-orphans): case "$(uname -s 2>/dev/null || echo unknown)" in MINGW*|MSYS*|CYGWIN*) out=$(bash core/scripts/daemon-orphan-sweep.sh 2>&1); our_pid=$(cat mind_api/state/daemon.pid 2>/dev/null | tr -d '[:space:]'); our_parent=$(cat mind_api/state/daemon.parent.pid 2>/dev/null | tr -d '[:space:]'); if [ -z "$our_pid" ]; then echo "INFO: daemon not running — skipping orphan check"; elif echo "$out" | grep -qE "KEEP PID=$our_pid" && ( [ -z "$our_parent" ] || echo "$out" | grep -qE "KEEP PID=$our_parent" ); then echo "PASS: this-repo daemon kept by sweep (child=$our_pid parent=${our_parent:-<none>})"; else echo "FAIL: sweep did not flag our daemon as KEEP — kill path regression"; echo "$out"; fi ;; *) echo "SKIP: daemon orphan check is Windows-specific" ;; esac

   # Section GAE: .gitattributes EOL Enforcement for Shell Scripts (g-115-869, g-115-871, 2026-05-17)
   # `core.autocrlf=true` is the standard Windows Git install default and will
   # heuristically convert shell scripts to CRLF on checkout when .gitattributes
   # only specifies `* text=auto` (heuristic-only). When `_runtime.sh` gets
   # CRLF'd, every subprocess.run(['bash', wrapper]) call returns rc=127 with
   # `$'\r': command not found` (charlie 2026-05-16 incident; root cause traced
   # in g-115-855 zeta investigation). The explicit `*.sh text eol=lf` /
   # `*.bash text eol=lf` rules override autocrlf=true permanently for shell
   # scripts. A future edit that removes either line would regress the class
   # silently — these checks catch the regression before it ships.
   Check: `.gitattributes` contains `*.sh text eol=lf`. Bash: `grep -qE '^\*\.sh\s+text\s+eol=lf' .gitattributes && echo "PASS: .gitattributes pins *.sh to LF" || { echo "FAIL: .gitattributes missing '*.sh text eol=lf' — autocrlf=true can re-CRLF shell scripts (charlie 2026-05-16 rc=127 incident class)"; exit 1; }`
   Check: `.gitattributes` contains `*.bash text eol=lf`. Bash: `grep -qE '^\*\.bash\s+text\s+eol=lf' .gitattributes && echo "PASS: .gitattributes pins *.bash to LF" || { echo "FAIL: .gitattributes missing '*.bash text eol=lf' — autocrlf=true can re-CRLF .bash scripts"; exit 1; }`
   # Behavioral audit: git's actual EOL attribute for `_runtime.sh` must reflect
   # the gitattributes rules. If the file's attr-column still reads `text=auto`,
   # the rules in .gitattributes are not being applied (typo, wrong section,
   # case sensitivity). The runtime check confirms attribute resolution, not
   # just the static rule presence.
   Bash: line=$(git ls-files --eol core/scripts/_runtime.sh 2>/dev/null); echo "$line" | grep -qE 'attr/text\s+eol=lf' && echo "PASS: _runtime.sh resolved to text eol=lf via .gitattributes rule" || { echo "FAIL: git ls-files --eol core/scripts/_runtime.sh shows '$line' — should resolve to attr text eol=lf; .gitattributes rule not applying to this path"; exit 1; }

   # Return Protocol: EVERY sub-skill must end with a tool call, not text.
   # Dynamic check: every SKILL.md must contain a `## Return Protocol` section
   # EXCEPT hard-exempt user-only control commands that never run inside the loop.
   # Exempt list: start, stop, open-questions, init, review, security-review, tree-reader, verify-learning.
   # Every other skill (base loop-invokable + hybrid + forged) needs RP.
   Bash: for f in .claude/skills/*/SKILL.md; do name=$(basename $(dirname "$f")); case "$name" in start|stop|open-questions|init|review|security-review|tree-reader|verify-learning) continue ;; esac; grep -q '^## Return Protocol' "$f" || echo "MISSING_RP: $name"; done → verify returns nothing (every non-exempt skill must have ## Return Protocol)
   # Branch-terminator audit: every procedural branch in every non-exempt skill must end with a tool call (Bash:/Skill(/invoke /), not text.
   # The section-presence check above proves the RP guidance exists; this audit proves the procedural bodies actually follow it.
   # Hard gate: exits 1 on any FAIL. WARNs are advisory-only (ambiguous pseudocode shapes, safe by design).
   Bash: bash core/scripts/skill-branch-terminator-audit.sh → verify exits 0 (FAIL=0)
   # WARN=0 regression guard (g-115-490 / g-115-496, 2026-05-10): WARNs are advisory but
   # the seeded baseline is 0 (meta/audit-baselines.yaml -> skill_branch_terminator_warns).
   # Catches drift when ambiguous pseudocode shapes proliferate without explicit acceptance.
   Bash: bash core/scripts/skill-branch-terminator-audit.sh 2>&1 | grep -qE 'WARN=0(,|\))' && echo "PASS: WARN=0 matches baseline" || { echo "FAIL: WARN count regressed past baseline=0 — re-seed via meta/audit-baselines.yaml only after deliberate review"; exit 1; }
   # Forge-skill template must seed new skills with the section
   Check: `.claude/skills/forge-skill/SKILL.md` contains the literal string "## Return Protocol" in the SKILL.md template it emits (so newly forged skills inherit the rule)
   # Rule document must reference the actual exempt-list mechanism (rb-309, rb-310)
   # Positive invariant: rule names every skill the dynamic check exempts, so the doc and the check stay in sync
   Bash: for s in start stop open-questions init review security-review tree-reader verify-learning; do grep -q "\`$s\`" .claude/rules/return-protocol.md || echo "MISSING_IN_RP_RULE: $s"; done → verify returns nothing
   # Canonical spelling: Claude Code spec (code.claude.com/docs/en/skills) uses HYPHEN for all
   # multi-word frontmatter fields — user-invocable, disable-model-invocation, allowed-tools,
   # argument-hint. The underscore form user_invocable is not recognized by Claude Code.
   # Session 50 migrated 11 forged skills + forge-skill template + _tree.yaml + skill-structure-gate.py.
   Bash: grep -rln '^user_invocable:' .claude/skills/*/SKILL.md 2>/dev/null && echo "BAD_UNDERSCORE_SPELLING" || echo "OK" → verify returns "OK" (no SKILL.md uses underscore form)
   Bash: grep -l 'fm\.get("user_invocable")' core/scripts/*.py 2>/dev/null && echo "BAD_UNDERSCORE_FALLBACK" || echo "OK" → verify returns "OK" (no script has an underscore fallback — single source of truth, hyphen-only)
   # Negative invariant: rule must not prescribe user_invocable/user-invocable as THE discriminator.
   # It may mention the field in a "Do NOT rely on ..." warning (rb-310), but the rule-line itself must reference an exempt list.
   Bash: grep -E '^\s*(- `?user_invocable`?|user_invocable.*is.*discriminator|check.*user_invocable|use.*user_invocable.*to)' .claude/rules/return-protocol.md && echo "BAD_RP_DISCRIMINATOR" || echo "OK" → verify returns "OK"
   # Stall-observability script: scans .stop-hook-log for consecutive BLOCKs keyed by (agent, sid)
   Check: `core/scripts/stop-hook-analyze.sh` exists
   Bash: test -f core/scripts/stop-hook-analyze.sh && echo OK || echo MISSING → verify returns "OK"
   # Stall-to-backlog consumer: converts loop-stall-warnings.jsonl entries into Unblock goals on asp-240.
   # Closes the observability-to-backlog gap where stop-hook-analyze wrote warnings with no consumer.
   Check: `core/scripts/stall-goal-filer.sh` exists
   Check: `core/scripts/stall-goal-filer.py` exists
   Bash: test -f core/scripts/stall-goal-filer.sh && test -f core/scripts/stall-goal-filer.py && echo OK || echo MISSING → verify returns "OK"
   # stop-hook-analyze must accept --and-file to chain the filer in one invocation (off by default).
   Bash: grep -q -- '--and-file' core/scripts/stop-hook-analyze.sh && echo OK || echo MISSING_AND_FILE → verify returns "OK"
   # Recurring sweep goal must exist so the filer runs even when stop-hook is quiet.
   Bash: bash core/scripts/world-cat.sh aspirations.jsonl | grep -q 'Recurring: File goals for any unconverted loop-stall warnings' && echo OK || echo MISSING_RECURRING_SWEEP → verify returns "OK"
   # Scanner must skip the body of `## Return Protocol` sections in each SKILL.md.
   # Those sections quote Bash:/Output: as documentation; scanning them would flag
   # legitimate RP examples as FAIL and produce false positives. The skip is the
   # scanner's load-bearing false-positive guard — do not remove.
   # Grep for the load-bearing variable `in_rp_section`, not the string "return
   # protocol" which could linger as a misleading comment after a regression.
   Bash: grep -q 'in_rp_section' core/scripts/skill-branch-terminator-audit.py && echo OK || echo MISSING_RP_SKIP_VAR → verify returns "OK"
   # Filer must parse add-goal stdout as JSON, not token-scan (rb-317 Bug A).
   # aspirations.py add-goal emits pretty-printed JSON where the id is quoted;
   # token.startswith("g-NNN-") never matches quoted tokens.
   Bash: grep -q 'json.loads(result.stdout)' core/scripts/stall-goal-filer.py && echo OK || echo MISSING_JSON_PARSE → verify returns "OK"
   # Filer must filter rate-limit by per-agent stall-agent:<name> tag (rb-317 Bug B).
   # A generic loop-stall scan makes alpha's filer rate-limited by bravo's filing.
   Bash: grep -q 'agent_tag not in tags' core/scripts/stall-goal-filer.py && echo OK || echo MISSING_AGENT_FILTER → verify returns "OK"
   # Filer must source the last-goal hint from execution-diary.jsonl, not journal.jsonl.
   # Diary has per-second timestamps and goal_ids; journal has date-only granularity and
   # session summaries — the latter produces ambiguous hints on busy days. Single source.
   Bash: grep -q 'execution-diary.jsonl' core/scripts/stall-goal-filer.py && echo OK || echo MISSING_DIARY_SOURCE → verify returns "OK"
   Bash: grep -cE '^[^#]*journal\.jsonl' core/scripts/stall-goal-filer.py | grep -qw 0 && echo OK || echo UNEXPECTED_JOURNAL_REF → verify returns "OK" (no active code path reads journal.jsonl)
   # Streak state machine MUST capture sid on ALLOW lines — observer ALLOWs must not clear runner streaks (rb-311)
   Bash: grep -q 'pat_allow.*sid=' core/scripts/stop-hook-analyze.sh && echo OK || echo BAD_ALLOW_REGEX → verify returns "OK"
   Bash: grep -q 'allow_sid.*==.*sid\|s\["sid"\].*==.*allow_sid' core/scripts/stop-hook-analyze.sh && echo OK || echo BAD_SID_COMPARE → verify returns "OK" (ALLOW clears streak only if sid matches tracked sid)
   # session_count must only increment on first iteration (not every LOOP_CONTINUE re-invocation)
   Check: `aspirations/SKILL.md` `aspirations-meta-update.sh session_count` is inside the ELSE branch of loop_state check
   # Multi-agent compact breadcrumb: per-agent (not shared) to prevent SID contamination
   Check: `stop-hook.sh` writes `compact-pending` to `$HOOK_AGENT_DIR/session/` (not project root)
   Check: `session-save-id.sh` loops over `*/session/compact-pending` (not `.compact-agent`)
   Check: `session-save-id.sh` verifies OLD SID matches running-session-id before consuming breadcrumb
   Bash: grep -c '\.compact-agent"' core/scripts/stop-hook.sh 2>/dev/null → verify returns 0 (no shared file)
   # Return Protocol rule: single source of truth for all sub-skills
   Check: `.claude/rules/return-protocol.md` exists
   Check: `aspirations-verify/SKILL.md` references `return-protocol.md` (not inline protocol)
   # External path resolution: scripts resolve paths mechanically, pseudocode must not use bare meta/ or world/
   Check: `.claude/rules/path-resolution.md` exists
   Check: `core/scripts/world-cat.sh` exists (world/ file reader with path resolution)
   Check: `core/scripts/meta-cat.sh` exists (meta/ non-YAML file reader with path resolution)
   Bash: grep -cP '^\s*(Read|Edit|Write|Append to) (meta|world)/' .claude/skills/*/SKILL.md 2>/dev/null | grep -v ':0$' → should return nothing (no bare meta/ or world/ in pseudocode instructions)
   # Agent path resolution: agent dirs live at PROJECT_ROOT/<agent>, NOT under WORLD_DIR/META_DIR or their parent (2026-05-08 chat session — Mode B cruft prevention)
   Bash: grep -q "^## Agent Paths" .claude/rules/path-resolution.md → expect match (Agent Paths section present)
   Bash: bash core/scripts/guardrails-read.sh --id guard-479 2>/dev/null | grep -q '"id": "guard-479"' → expect match (guard-479 active)
   Check: `world/knowledge/tree/system/system-constraints-loop/external-path-resolution-cruft.md` exists (cruft incident catalogue)
   Bash: bash core/scripts/tree-find-node.sh --text "external-path-resolution-cruft" --top 3 2>/dev/null | grep -q '"key": "external-path-resolution-cruft"' → expect match (tree node retrievable by name)
   # Mode D orphan-root sweeper wiring (g-115-756, g-115-875, 2026-05-17): orphan-root-sweep.sh
   # Scan 4 delegates Mode D detection (U+F03A or single-letter/letter+":" PROJECT_ROOT cruft)
   # to _orphan_root_helpers.is_mode_d_cruft via NUL-delimited stdin pipe. If a future edit
   # renames the helper, drops the file, changes the Python import path, or strips the
   # Scan 4 markers, Scan 4 silently no-ops on real Mode D cruft. Per rb-648 (verify named
   # hook target before trusting wiring TODO). Cross-refs: rb-939 (daemon-staleness 3-probe),
   # guard-554 (mandatory restart after path-resolver fix).
   Check: `core/scripts/_orphan_root_helpers.py` exists (Python helper for Mode D detection)
   Bash: test -f core/scripts/_orphan_root_helpers.py && grep -q 'def is_mode_d_cruft' core/scripts/_orphan_root_helpers.py && echo "PASS: is_mode_d_cruft defined in _orphan_root_helpers.py" || echo "FAIL: _orphan_root_helpers.py missing or is_mode_d_cruft not defined (g-115-756 wiring regression)"
   Bash: grep -q 'from _orphan_root_helpers import is_mode_d_cruft' core/scripts/orphan-root-sweep.sh && echo "PASS: orphan-root-sweep.sh imports is_mode_d_cruft" || echo "FAIL: orphan-root-sweep.sh missing Mode D helper import (g-115-756 wiring regression)"
   Bash: grep -q 'Mode D' core/scripts/orphan-root-sweep.sh && grep -q 'MODE-D ORPHAN' core/scripts/orphan-root-sweep.sh && echo "PASS: orphan-root-sweep.sh emits Mode D scan section + ORPHAN marker" || echo "FAIL: orphan-root-sweep.sh Scan 4 markers missing (header or per-name emit drifted)"
   Check: `core/scripts/tests/test_orphan_root_mode_d.py` exists (12-case test contract for Mode D detection)

      # Dual-queue aspiration evidence checks (Section DQ)
   Bash: aspirations-read.sh --active → verify world aspirations exist (asp-001 Explore and Learn)
   Bash: bash core/scripts/aspirations-read.sh --source agent --active → verify agent aspirations exist (asp-001 Maintain Agent Health)
   Bash: goal-selector.sh select 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); sources=set(r.get('source','') for r in d if isinstance(d,list)); print('PASS: both sources' if 'world' in sources and 'agent' in sources else 'PASS: world present, agent goals in cooldown' if isinstance(d,list) and 'world' in sources else 'PASS: structural' if isinstance(d,dict) else 'FAIL')" → verify source tagging works (agent goals may be in cooldown)
   Check: goal-selector.sh output includes `source` field on every scored goal
   Check: `core/config/conventions/aspirations.md` documents both world and agent script families
   Check: `agent-aspirations-read.sh` exists in core/scripts/ (agent queue access)
   Bash: bash core/scripts/aspirations-read.sh --source agent --active 2>/dev/null → verify returns valid JSON (agent queue readable)
   Check: `aspirations.py --source agent claim` → rejected with error (world-only operation)
   IF agent claimed a world goal:
       Check: world/aspirations.jsonl has `claimed_by` and `claimed_at` fields on that goal
       Check: `claimed_by` value matches `$MIND_AGENT`
   # JSONL list-field normalization regression check
   Check: `goal-selector.py` defines `_ensure_list()` and every operational `.get("blocked_by")`, `.get("participants")`, `.get("tags")` call is wrapped in it. Only passthrough stores (goal_map building) may use raw `.get()`. Read the file and verify no unguarded iteration of these fields.

   # Aspiration ID archive collision guard (Section DQ continued)
   Check: `aspirations.py` `cmd_add` calls `_check_not_archived` inside lock scope (prevents creating aspirations with IDs that exist in the archive)
   Check: `aspirations.py` `_check_not_archived` has `action` keyword param — `action="add"` gives "pick a higher ID" message, default gives "cannot modify" message
   Check: `create-aspiration/SKILL.md` Step 5 item 3 says "BOTH active AND archived" (not just "existing aspirations")

   # Binary outcome classification evidence checks (Section OC)
   # outcome_class has exactly 2 valid values: "deep" (default) and "routine"
   Check: `aspirations-execute/SKILL.md` Phase 4-post sets `outcome_class = "deep"` as default
   Check: Only `goal.recurring AND goal_succeeded AND no findings` can demote to "routine"
   Check: Recurring goals WITH findings remain "deep" (learning is the mission — no deferred encoding)
   Check: Non-recurring goals always remain "deep" (structural constraint)
   Check: `aspirations-state-update/SKILL.md` Step 8 has `IF outcome_class == "deep":` branch with NO standard branch
   Check: Deep path sets `step_8_wrote_insight = true` AND `step_8_tree_encoded = true` (both computed and written)
   # CRITICAL: Coordination deferral branch must exist — without it, insight is silently
   # dropped AND the learning gate forces inline encoding to the node another agent just
   # wrote to, causing the exact overwrite the coordination check was designed to prevent.
   Check: `aspirations-state-update/SKILL.md` Step 8 has `ELIF encoding_deferred_by_coordination:` branch that queues to encoding_queue with source_type "coordination_deferred"
   Check: Coordination deferral branch sets `step_8_wrote_insight = true` AND `step_8_tree_encoded = false`
   Check: `aspirations-learning-gate/SKILL.md` escape hatch checks encoding_queue for `source_type == "coordination_deferred"` BEFORE firing forced inline encoding
   Check: `aspirations-learning-gate/SKILL.md` Phase 9.5 has 2 branches: routine (bypass) and deep (check tree)
   Check: `aspirations/SKILL.md` Spark call gates on `outcome_class == "deep"`
   Check: `core/config/execute-protocol-digest.md` Outcome Classification section says "Binary" (not "3-Tier")
   Bash: grep -c '"standard"' .claude/skills/aspirations-execute/SKILL.md .claude/skills/aspirations-state-update/SKILL.md .claude/skills/aspirations-learning-gate/SKILL.md → verify ALL return 0 (no remaining standard-tier outcome references)

   # Experience archive evidence checks
   IF agents/<agent>/experience.jsonl exists:
       Bash: experience-read.sh --summary → verify experience records were created
       Bash: experience-read.sh --meta → verify metadata tracking
       Check: do experience records have corresponding .md files at content_path?
       Check: do pipeline records with experience_ref point to valid experience IDs?

   # Experience archival gate evidence checks (Section EG)
   Check: `aspirations-learning-gate/SKILL.md` has Phase 9.5-exp "Experience Archival Gate"
   Check: Phase 9.5-exp gates on `outcome_class == "deep"` (not routine)
   Check: Phase 9.5-exp checks `wm-read.sh active_context.experience_refs` (matches Phase 4.25 WM key)
   Check: Phase 9.5-exp recovery writes to `agents/<agent>/experience/` AND calls `experience-add.sh`
   Check: Phase 9.5-exp is positioned BEFORE Phase 9.5c (unreflected hypothesis check)
   Check: `aspirations-learning-gate/SKILL.md` Chaining Calls list includes `experience-add.sh`

   # Bash-enforcement wrappers for Phase 4.25, 8.5, 8e (Section BE — rb-428 extension, g-248-31)
   # Three wrappers moved formerly-LLM-orchestrated writes into bash to eliminate drift.
   # If any of these checks regress, the corresponding phase has drifted back to LLM-only.
   Check: `core/scripts/experience-archive-goal.sh` exists and dispatches to `experience.py archive-goal`
   Check: `core/scripts/experience.py` has `cmd_archive_goal` function and `"archive-goal"` entry in dispatch dict
   Check: `core/scripts/findings-gate.sh` and `core/scripts/findings-gate.py` exist; Python script emits final `findings_count=N created=M` summary line
   Check: `core/scripts/decision-rules-append.sh` and `core/scripts/decision-rules-append.py` exist; Python script emits `decision_rules_count=<N> appended=<M> skipped=<K>` summary line (plus `reason=no_rule_passed` when stdin is empty)
   Check: `core/scripts/decision-rules-staleness.sh` exists and is warn-only (no force-gate sentinel — per-call `no_rule_passed` signal covers the legitimate "no rule emerged" case)
   Bash: grep -c 'decision-rules-staleness.sh' core/scripts/iteration-close.sh → verify >= 1 (staleness check wired into do_productivity_check alongside experience-staleness-check.sh)
   # decision-rules-staleness.sh structural invariants (g-115-221, session-81 framework changes regression).
   # (a) `[ -f ]` guards required before file reads under set -euo pipefail (script reads SESSION_ID_FILE
   #     and LAST_WARNED_FILE; missing-file failures must not kill the script via pipefail).
   # (b) Throttle-write block (Python heredoc that writes the warned-sid back to disk) MUST NOT be wrapped
   #     in try: — intentional fail-loud per fresh-eyes review, since caller wraps script in `|| true`.
   Bash: grep -cE '\[ -f "\$SESSION_ID_FILE" \]|\[ -f "\$LAST_WARNED_FILE" \]' core/scripts/decision-rules-staleness.sh → expect 2 (bracket-f guards before both file reads)
   Bash: py -3 -c "import re,pathlib; t=pathlib.Path('core/scripts/decision-rules-staleness.sh').read_text(encoding='utf-8'); m=re.search(r'with open\([^)]*last_warned_path[^)]*\)', t); assert m, 'throttle write block missing'; pre=t[:m.start()].splitlines()[-4:]; assert not any('try:' in line for line in pre), f'try: precedes throttle write: {pre}'; print('PASS')" → expect PASS (no try: wraps the throttle write — intentional fail-loud)
   Bash: grep -c 'experience-archive-goal.sh' .claude/skills/aspirations-execute/SKILL.md → verify >= 1 (Phase 4.25 calls the wrapper, not the old multi-step pseudocode)
   Bash: grep -c 'findings-gate.sh' .claude/skills/aspirations-state-update/SKILL.md → verify >= 1 (Step 8.5 calls the wrapper)
   Bash: grep -c 'decision-rules-append.sh' .claude/skills/aspirations-state-update/SKILL.md → verify >= 2 (Step 8 EXTRACT DECISION RULES sub-bullet has both the real-rule path and the empty-stdin no-rule path)
   Check: `core/config/phase-bash-enforcement-digest.md` exists and cross-references iteration-close-digest.md
   # Invariant: marker bump must precede the stdin branch in decision-rules-append.py (rb-428 follow-up).
   # If this regresses, empty-stdin calls stop bumping the marker and the staleness probe false-positives.
   Bash: py -3 -c "import pathlib; t=pathlib.Path('core/scripts/decision-rules-append.py').read_text(encoding='utf-8'); m_bump=t.find('bump_last_call_marker()', t.find('def main')); m_stdin=t.find('sys.stdin', t.find('def main')); assert m_bump > 0 and m_stdin > m_bump, 'bump_last_call_marker must precede stdin read in main()'; print('PASS')"
   # Invariant: findings-gate.py resolution-filter must check BOTH match content AND window (rb-428 follow-up).
   # If reduced to window-only, greedy-match resolution language (e.g., 'root cause was X, fixed by Y') slips through.
   Bash: py -3 -c "import pathlib; t=pathlib.Path('core/scripts/findings-gate.py').read_text(encoding='utf-8'); assert 'resolution_re.search(match_text)' in t and 'resolution_re.search(window)' in t, 'dual resolution-filter check regressed'; print('PASS')"
   # Invariant: experience.py cmd_archive_goal keeps os.replace BEFORE validate_record (rb-443).
   # Validator's content_path check requires the file to exist on disk — reordering breaks the happy path.
   # Match actual call site `validate_record(rec)` rather than bare mentions — the DO-NOT-MOVE
   # comment above os.replace references 'validate_record' in prose and would false-positive a naive find().
   Bash: py -3 -c "import pathlib; t=pathlib.Path('core/scripts/experience.py').read_text(encoding='utf-8'); start=t.find('def cmd_archive_goal'); end=t.find('def ', start+10); body=t[start:end]; r=body.find('os.replace(str(trace_src)'); v=body.find('validate_record(rec)'); assert r > 0 and v > 0 and r < v, f'os.replace (idx {r}) must precede validate_record(rec) (idx {v}) in cmd_archive_goal (validator reads content_path from disk)'; print('PASS')"

   # Single-writer wrapper for iteration-checkpoint.json (Section BE — rb-428 extension, g-248-36)
   # Same family as g-240-74 (blocker_ref validation) and g-240-27 (pipeline/experience write-path
   # validation): an LLM-orchestrated write surface replaced with a bash-enforced single-writer
   # wrapper. If any of these invariants regress, iteration-checkpoint writes drift back to
   # scattered inline json.dump()/jq calls and schema drift + torn-write risk returns.
   Check: `core/scripts/loop-state-save.py` exists and `py -3 -c "import ast; ast.parse(open('core/scripts/loop-state-save.py', encoding='utf-8').read())"` succeeds
   Check: `core/scripts/loop-state-save.sh` exists and `bash -n core/scripts/loop-state-save.sh` succeeds
   # SCHEMA must carry the five required-at-init keys. Loosening any of them re-opens the anchor-without-goal-id regression that autocompact substitution surfaces.
   # Line-scan: each schema row is `"KEY": {"required": True/False, ...}` — match the key + same-line required value directly.
   Bash: py -3 -c "import pathlib, re; t=pathlib.Path('core/scripts/loop-state-save.py').read_text(encoding='utf-8'); req=set(); [req.add(m.group(1)) for line in t.splitlines() for m in [re.match(r'\s*\"([a-z_]+)\"\s*:\s*\{\s*\"required\"\s*:\s*True', line)] if m]; missing=set(('goal_id','aspiration_id','source','phase','selected_at')) - req; assert not missing, f'SCHEMA required keys drift: missing {missing}'; print('PASS')"
   # Atomic write invariant — tempfile.mkstemp + os.replace in the same direction as pipeline.py/experience.py/aspirations.py. Plain open('w') would introduce torn-write risk that the wrapper is specifically guarding against.
   Bash: py -3 -c "import pathlib; t=pathlib.Path('core/scripts/loop-state-save.py').read_text(encoding='utf-8'); assert 'tempfile.mkstemp(' in t and 'os.replace(' in t, 'atomic-write pattern (tempfile.mkstemp + os.replace) regressed in loop-state-save.py'; print('PASS')"
   # Dotted-path merge helper — aspirations-verify writes phase_progress.q1_passed style keys; if _set_dotted disappears the merges silently turn into flat top-level writes that the postcompact-restore path cannot reconstruct.
   Bash: py -3 -c "import pathlib; t=pathlib.Path('core/scripts/loop-state-save.py').read_text(encoding='utf-8'); assert 'def _set_dotted(' in t, '_set_dotted helper removed — nested phase_progress merges would flatten'; print('PASS')"
   # Caller retrofits — every mid-iteration write site routes through the wrapper. Regressing any of these reopens the inline-json.dump drift that motivated g-248-36.
   # NOTE: grep patterns accept optional quote between `.sh` and the subcommand — iteration-close.sh invokes via `"$CORE_ROOT/scripts/loop-state-save.sh" update`, so a naive `loop-state-save.sh update` literal match misses it.
   Bash: grep -cE 'loop-state-save\.sh"?\s+init' .claude/skills/aspirations-select/SKILL.md → verify >= 1 (Phase 2.95 anchor write)
   Bash: grep -cE 'loop-state-save\.sh"?\s+update' core/scripts/iteration-close.sh → verify >= 1 (_checkpoint_refresh per-phase update)
   Bash: grep -cE 'loop-state-save\.sh"?\s+update' .claude/skills/aspirations-verify/SKILL.md → verify >= 4 (Q1/Q2/Q3 phase_progress + standard_checks_passed writes; header doc example at ~line 107 also matches, so actual count is ≥5)
   # No residual direct writes to iteration-checkpoint.json from the retrofitted sites. jq / p.write_text / open(...,'w') against the checkpoint path from outside the wrapper silently bypasses schema validation.
   Bash: py -3 -c "import pathlib, re; files=['.claude/skills/aspirations-select/SKILL.md','.claude/skills/aspirations-verify/SKILL.md','core/scripts/iteration-close.sh']; bad=[]; [bad.append((f, m.group(0)[:80])) for f in files for m in re.finditer(r'(jq\s+[^|]*iteration-checkpoint|p\.write_text\([^)]*iteration-checkpoint|open\([^)]*iteration-checkpoint[^)]*[\"\\']w[\"\\'])', pathlib.Path(f).read_text(encoding='utf-8'))]; print('BAD:', bad) if bad else print('PASS')"

   # Reader-writer SCHEMA consistency (Section BE — rb-428 extension, g-248-59, bravo FE-001)
   # The abbreviated-obligation gate went dead for 7 days because obligation-audit.py and
   # abbreviated-obligation-audit.py both polled checkpoint.get("outcome_class") but no
   # writer ever set it — obligation-audit._validate short-circuited every routine claim
   # with "claim says routine but checkpoint says None". The class of drift: reader wired,
   # SCHEMA silent, no writer. Guard against regressions by asserting that each key the
   # two readers consume from iteration-checkpoint.json is (a) declared in SCHEMA, (b)
   # actually written by iteration-close.sh, and (c) compared against the same string
   # values on both sides.
   Check: `core/scripts/loop-state-save.py` SCHEMA contains an `outcome_class` entry with `enum` constrained to `("deep", "routine")`
   Bash: py -3 -c "import pathlib, re; t=pathlib.Path('core/scripts/loop-state-save.py').read_text(encoding='utf-8'); m=re.search(r'\"outcome_class\"\s*:\s*\{[^}]*\"enum\"\s*:\s*\(\s*\"deep\"\s*,\s*\"routine\"\s*\)', t); assert m, 'outcome_class SCHEMA entry missing or enum drift'; print('PASS')"
   # Writer side — iteration-close.sh do_state_update must invoke loop-state-save.sh update
   # with outcome_class=$OUTCOME. If this grep drops to zero, the checkpoint reverts to
   # outcome_class=None and the obligation-audit gate silently false-negatives forever.
   Bash: grep -cE 'outcome_class=\$OUTCOME' core/scripts/iteration-close.sh → verify >= 1 (state-update phase writes outcome_class)
   # Reader-side enum parity — both audit scripts must compare against the exact strings
   # the writer emits. If either reader diverges (e.g. uppercase "ROUTINE", or drops the
   # quotes), the gate silently always-fails the FE-001 way even with the writer in place.
   Bash: py -3 -c "import pathlib; oa=pathlib.Path('core/scripts/obligation-audit.py').read_text(encoding='utf-8'); aoa=pathlib.Path('core/scripts/abbreviated-obligation-audit.py').read_text(encoding='utf-8'); assert 'outcome_class == \"routine\"' in oa and 'iter_outcome_class == \"routine\"' in aoa, 'reader enum comparison drifted from writer SCHEMA enum ({\"deep\",\"routine\"})'; print('PASS')"

   # Skill-quality Step 8.76 auto-derivation wrapper (Section BE — rb-428 extension, g-248-34)
   # Step 8.76 used to inline LLM-orchestrated mapping of five dimensions — same drift
   # risk family as the decision-rules / findings-gate / experience-archive wrappers.
   # skill-quality-score.sh derives 4-of-5 dimensions mechanically from execution signals
   # (safety/completeness/executability/maintainability) and accepts cost_awareness as the
   # single LLM-judgment input. Writes are lock-protected against concurrent-agent races.
   # If any of these invariants regress, Step 8.76 silently reverts to LLM-only scoring and
   # concurrent sessions can race on meta/skill-quality.yaml.
   Check: `core/scripts/skill-quality-score.py` exists and `py -3 -c "import ast; ast.parse(open('core/scripts/skill-quality-score.py', encoding='utf-8').read())"` succeeds
   Check: `core/scripts/skill-quality-score.sh` exists and `bash -n core/scripts/skill-quality-score.sh` succeeds
   # Four mechanical derivation functions must be present — each maps one execution signal
   # to a grade. If any disappears, Step 8.76 loses the corresponding auto-derivation and
   # the caller would have to re-inline the IF-chain that the wrapper was meant to retire.
   Bash: py -3 -c "import pathlib; t=pathlib.Path('core/scripts/skill-quality-score.py').read_text(encoding='utf-8'); missing=[fn for fn in ('grade_from_ratio','grade_from_episodes','grade_from_violations','derive_maintainability') if f'def {fn}(' not in t]; assert not missing, f'derive functions missing: {missing}'; print('PASS')"
   # Lock invariant — acquire_lock/release_lock must wrap the skill-evaluate subprocess call.
   # Without the lock, two concurrent agents can read-modify-write skill-quality.yaml and
   # the second write clobbers the first (tmp+rename is atomic per-process but not across
   # processes). If acquire_lock disappears from the score path, concurrent races return.
   Bash: py -3 -c "import pathlib; t=pathlib.Path('core/scripts/skill-quality-score.py').read_text(encoding='utf-8'); assert 'acquire_lock(LOCK_PATH' in t and 'release_lock(LOCK_PATH)' in t, 'lock acquisition around skill-evaluate subprocess regressed'; print('PASS')"
   # Caller wiring — Step 8.76 must call the new wrapper, not the legacy skill-evaluate.sh.
   # If this grep drops to 0, Step 8.76 reverts to the pre-g-248-34 LLM-orchestrated form.
   Bash: grep -cE 'skill-quality-score\.sh"?\s+score' .claude/skills/aspirations-state-update/SKILL.md → verify >= 1 (Step 8.76 invokes the wrapper)

   # Journal-append wrapper (Section BE — rb-428 extension, g-248-35)
   # Phase 7 / 7r journal-write paths used to inline markdown templating + index
   # merge/add — same drift family as the decision-rules / findings-gate
   # wrappers. journal-append.sh consolidates the markdown append, citation
   # scan, and journal-index merge/add fallback into one bash-enforced unit.
   # If any of these invariants regress, the templating drifts back into
   # SKILL.md prose and per-iteration journal entries get inconsistent shapes.
   Check: `core/scripts/journal-append.sh` exists and `bash -n core/scripts/journal-append.sh` succeeds
   # Required flag surface — the wrapper accepts --goal, --outcome-class,
   # --summary, --session. If any of these is renamed or removed, the
   # iteration-close.sh caller breaks silently (set +e on the call) AND every
   # other documented call site (Phase 7r, /reflect manual writes) fails.
   Bash: py -3 -c "import pathlib; t=pathlib.Path('core/scripts/journal-append.sh').read_text(encoding='utf-8'); missing=[f for f in ('--goal', '--outcome-class', '--summary', '--session') if f not in t]; assert not missing, f'flag surface regressed: {missing}'; print('PASS')"
   # Stable section-header contract — downstream readers (boot, consolidation)
   # parse `## title — Goal:`, `Outcome:`, `Summary:`. If any header drifts,
   # those readers silently lose entries. The literal echo lines in the
   # wrapper ARE the contract.
   Bash: py -3 -c "import pathlib; t=pathlib.Path('core/scripts/journal-append.sh').read_text(encoding='utf-8'); assert 'echo \"## ' in t and 'echo \"Outcome:' in t and 'echo \"Summary:' in t, 'journal-append.sh markdown header contract drifted'; print('PASS')"
   # Caller wiring — iteration-close.sh do_state_update MUST call the wrapper,
   # not re-inline the markdown assembly + journal-merge / journal-add fallback.
   # If this grep drops to 0, the inline block was reintroduced and the
   # wrapper sat unused (g-248-35 was filed precisely to retire that drift).
   Bash: grep -c 'journal-append\.sh' core/scripts/iteration-close.sh → verify >= 1 (do_state_update invokes the wrapper)
   # The pre-extraction inline block MUST be gone. If `journal_dir=` reappears
   # inside iteration-close.sh do_state_update, the wrapper has been bypassed
   # and journal entries are being written from two places at once — exactly
   # the divergence rb-428 was filed against.
   Bash: py -3 -c "import pathlib; t=pathlib.Path('core/scripts/iteration-close.sh').read_text(encoding='utf-8'); inline = 'journal_dir=\"\$AGENT_DIR/journal' in t or 'mkdir -p \"\$journal_dir\"' in t; assert not inline, 'iteration-close.sh re-inlined the journal block — journal-append.sh is being bypassed'; print('PASS')"
   # SKILL.md alignment — aspirations-state-update Phase 7 documents the
   # wrapper, not the legacy journal-merge.sh / journal-add.sh inline pattern.
   # If the SKILL.md regresses, future authors will re-inline the templating.
   Bash: grep -c 'journal-append\.sh' .claude/skills/aspirations-state-update/SKILL.md → verify >= 1 (Step 7 routine and deep journal-write paths reference the wrapper)

   # Phase 5 dependent-goal unblocking wrapper (Section BE — rb-428 extension, g-248-33)
   # The "Unblock Dependent Goals (with Output Passing)" loop in
   # aspirations-verify/SKILL.md Phase 5 used to be LLM-orchestrated:
   # walk all aspirations, remove completed_goal_id from blocked_by, and
   # for each goal with depends_on entry prepend the predecessor's
   # output_summary as a `## Predecessor Output (<id>)` block. dependent-unblock.sh
   # consolidates the entire loop into one bash-enforced unit (sibling
   # rb-428 wrappers: experience-archive-goal.sh, findings-gate.sh,
   # decision-rules-append.sh, loop-state-save.sh, skill-quality-score.sh,
   # journal-append.sh). If any of these invariants regress, Phase 5
   # silently skips the description-prepend half — the early sessions'
   # exact failure mode that motivated rb-428.
   Check: `core/scripts/dependent-unblock.sh` exists and `bash -n core/scripts/dependent-unblock.sh` succeeds
   Check: `core/scripts/dependent-unblock.py` exists and `py -3 -c "import ast; ast.parse(open('core/scripts/dependent-unblock.py', encoding='utf-8').read())"` succeeds
   # Required flag surface — the wrapper accepts --goal, --summary, --dry-run.
   # If any of these is renamed or removed, the aspirations-verify Phase 5
   # caller breaks silently AND smoke tests against unknown goal_ids stop
   # being safe (no-op proof requires --dry-run).
   Bash: py -3 -c "import pathlib; t=pathlib.Path('core/scripts/dependent-unblock.py').read_text(encoding='utf-8'); missing=[f for f in ('--goal', '--summary', '--dry-run') if f not in t]; assert not missing, f'flag surface regressed: {missing}'; print('PASS')"
   # Idempotency invariant — the marker check ("## Predecessor Output (...)")
   # MUST gate the description prepend, otherwise a second call double-prepends
   # the predecessor block. If this assertion regresses, Phase 5 starts
   # corrupting dependent-goal descriptions on retry.
   Bash: py -3 -c "import pathlib; t=pathlib.Path('core/scripts/dependent-unblock.py').read_text(encoding='utf-8'); assert 'if marker in existing_desc' in t and '## Predecessor Output (' in t, 'idempotent marker check regressed in dependent-unblock.py'; print('PASS')"
   # Both queues scanned — recurring predecessors live in either world or
   # agent aspirations.jsonl, and dependents may live in the OTHER queue.
   # If the scan loops over only one base, cross-queue unblocking goes dead.
   Bash: py -3 -c "import pathlib; t=pathlib.Path('core/scripts/dependent-unblock.py').read_text(encoding='utf-8'); assert 'WORLD_DIR' in t and 'AGENT_DIR' in t and 'for source, base in' in t, 'cross-queue scan invariant regressed in dependent-unblock.py'; print('PASS')"
   # Caller wiring — aspirations-verify/SKILL.md Phase 5 must call the wrapper,
   # not orchestrate the loop inline. If this grep drops to 0, the
   # pre-g-248-33 LLM-orchestrated "FOR EACH goal across all active
   # aspirations" pseudocode has been reintroduced.
   Bash: grep -c 'dependent-unblock\.sh' .claude/skills/aspirations-verify/SKILL.md → verify >= 1 (Phase 5 invokes the wrapper)
   # The pre-extraction inline block MUST be gone. If `FOR EACH goal across
   # all active aspirations` reappears in the verify SKILL.md, the wrapper
   # has been bypassed and dependent-goal unblocking is being orchestrated
   # from two places at once — exactly the divergence rb-428 was filed against.
   Bash: py -3 -c "import pathlib; t=pathlib.Path('.claude/skills/aspirations-verify/SKILL.md').read_text(encoding='utf-8'); inline = 'FOR EACH goal across all active aspirations WHERE blocked_by contains' in t; assert not inline, 'aspirations-verify/SKILL.md re-inlined the dependent-unblock loop — dependent-unblock.sh is being bypassed'; print('PASS')"
   # Smoke test — dry-run against a synthetic non-existent goal id must
   # succeed with empty matched/skipped lists. Catches scan-loop crashes,
   # JSON-shape regressions, and missing python imports before they hit
   # the live verify path.
   Bash: bash core/scripts/dependent-unblock.sh --goal verify-learning-smoke-zzz --summary "smoke" --dry-run | py -3 -c "import json,sys; d=json.loads(sys.stdin.read()); assert d['unblocked']==[] and d['injected']==[] and d['skipped']==[] and d['dry_run'] is True, f'smoke shape regressed: {d}'; print('PASS')"

   # Phase 4.26 explicit-feedback gate (Section BE — rb-428 extension, g-242-10)
   # rb-472 / guard-415 writer-layer enforcement. The Phase 4.26 explicit-
   # feedback step in aspirations-state-update gets abbreviated under context
   # pressure; utilization-gate.sh backstops by auto-running --all-unknown
   # (post-2026-05-07; was --all-noise pre-fix), which keeps
   # utilization_pending=false but produces ZERO times_helpful /
   # times_inferred_helpful writes — the exact failure mode that almost
   # retired 400+ rb entries on 2026-04-23 (rb-472). phase-4-26-gate.sh
   # fires inside iteration-close.sh do_state_update BEFORE the goal record
   # advances, reads retrieval-session.json's persisted utilization_method,
   # and refuses state-update on all_noise / all_unknown / infer-with-zero-helpful.
   # Override path: --no-retrieval-applicable "<reason>" logs to
   # world/phase-4-26-overrides.jsonl. If any of these invariants regress,
   # state-update reverts to the silent-skip drift the gate is meant to retire.
   Check: `core/scripts/phase-4-26-gate.sh` exists and `bash -n core/scripts/phase-4-26-gate.sh` succeeds
   Check: `core/scripts/phase-4-26-gate.py` exists and `py -3 -c "import ast; ast.parse(open('core/scripts/phase-4-26-gate.py', encoding='utf-8').read())"` succeeds
   # Required flag surface — the wrapper accepts --goal and
   # --no-retrieval-applicable. If either is renamed, iteration-close.sh
   # do_state_update breaks silently (gate exits 2, state-update aborts AND
   # the override path is unreachable, which is worse than no gate).
   Bash: py -3 -c "import pathlib; t=pathlib.Path('core/scripts/phase-4-26-gate.py').read_text(encoding='utf-8'); missing=[f for f in ('--goal', '--no-retrieval-applicable') if f not in t]; assert not missing, f'flag surface regressed: {missing}'; print('PASS')"
   # Verdict matrix invariant — the FIVE utilization_method values
   # ("manual", "all_helpful", "all_noise", "all_unknown", "infer") MUST all
   # be handled. Drop any branch and the gate either always-passes or
   # always-blocks for that method, defeating the writer-layer enforcement.
   # `all_unknown` is the post-2026-05-07 backstop: same block-the-goal
   # semantics as `all_noise` but no times_noise pollution. Note: the field
   # uses underscores in utilization-feedback.py (`all_noise`, not
   # `all-noise`) — assert exact strings.
   Bash: py -3 -c "import pathlib; t=pathlib.Path('core/scripts/phase-4-26-gate.py').read_text(encoding='utf-8'); missing=[m for m in ('all_noise', 'all_unknown', 'all_helpful', 'manual', 'infer') if '\"' + m + '\"' not in t]; assert not missing, f'verdict matrix regressed (missing methods): {missing}'; print('PASS')"
   # Override-ledger invariant — _log_override must write to
   # world/phase-4-26-overrides.jsonl. If the path drifts, audit trail
   # disappears and overrides become invisible. Match by literal filename
   # so a refactor of the constant assignment still trips the assertion.
   Bash: py -3 -c "import pathlib; t=pathlib.Path('core/scripts/phase-4-26-gate.py').read_text(encoding='utf-8'); assert 'phase-4-26-overrides.jsonl' in t and 'def _log_override' in t, 'override ledger path or helper regressed'; print('PASS')"
   # Caller wiring — iteration-close.sh do_state_update must invoke the
   # gate BEFORE bookkeeping writes. If this grep drops to 0, do_state_update
   # bypasses the gate and Phase 4.26 silently degrades back to LLM-only.
   Bash: grep -c 'phase-4-26-gate\.sh' core/scripts/iteration-close.sh → verify >= 1 (do_state_update invokes the gate pre-bookkeeping)
   # Cross-reference invariants — the goal verification checks REQUIRE
   # the gate to name rb-472 + guard-415. Comments in the .py docstring
   # are the closest stable surface. If these vanish, the gate has been
   # refactored without preserving the doctrinal lineage and future readers
   # will not know why the gate exists.
   Bash: py -3 -c "import pathlib; t=pathlib.Path('core/scripts/phase-4-26-gate.py').read_text(encoding='utf-8'); assert 'rb-472' in t and 'guard-415' in t, 'doctrinal cross-reference (rb-472 / guard-415) regressed in phase-4-26-gate.py'; print('PASS')"
   # Smoke test 1 — synthetic non-existent goal_id must fail-open pass
   # (no retrieval-session.json or stale goal_id mismatch). Catches
   # AGENT_DIR resolution drift and JSON-load crash regressions.
   Bash: bash core/scripts/phase-4-26-gate.sh --goal verify-learning-smoke-zzz | py -3 -c "import json,sys; d=json.loads(sys.stdin.read()); assert d['verdict']=='pass', f'smoke 1 expected pass, got: {d}'; print('PASS')"
   # Smoke test 2 — override path: even with --no-retrieval-applicable
   # the gate must produce JSON with verdict=pass + override=true. Live
   # block-then-override path is exercised in run-time only; this smoke
   # confirms the flag is plumbed through.
   Bash: bash core/scripts/phase-4-26-gate.sh --goal verify-learning-smoke-zzz --no-retrieval-applicable "smoke override" | py -3 -c "import json,sys; d=json.loads(sys.stdin.read()); assert d['verdict']=='pass', f'smoke 2 expected pass, got: {d}'; print('PASS')"

   # Backpressure audit_only_fields exclusion (Section BE — g-115-204, rb-504)
   # meta-backpressure.py used to roll back EVERY field change in monitored
   # strategy files when imp@k regressed — including append-only audit logs
   # like reflection-strategy.yaml::roi_history that record observability and
   # carry no tunable behavior. 14 rollbacks (11 of them on roi_history alone)
   # accumulated through April erasing audit data without changing any
   # tunable. The fix lives in three places: (1) meta.yaml backpressure block
   # declares per-strategy-file audit_only_fields allowlist; (2)
   # meta-backpressure.py cmd_check skips rollbacks when (file, field)
   # matches, marks the monitor audit_only_skipped, and persists the skip to
   # backpressure.yaml::audit_only_skips for parity with rollback_history;
   # (3) cmd_status surfaces audit_only_skips. If any of these regresses,
   # roi_history starts getting rolled back again and audit data goes silent.
   Check: `core/config/meta.yaml` backpressure block declares `audit_only_fields:` with at least `reflection-strategy.yaml: [roi_history, ...]`
   Bash: py -3 -c "import yaml,pathlib; cfg=yaml.safe_load(pathlib.Path('core/config/meta.yaml').read_text(encoding='utf-8')); bp=cfg.get('strategy_schemas',{}).get('backpressure',{}); aof=bp.get('audit_only_fields',{}); rs=aof.get('reflection-strategy.yaml',[]) or []; assert 'roi_history' in rs, f'reflection-strategy.yaml::roi_history missing from audit_only_fields: {aof}'; print('PASS')"
   # Skip-path invariant — cmd_check must look up audit_only_fields, mark
   # the monitor audit_only_skipped, and persist under audit_only_skips.
   # All three string literals are the contract; if any disappears, the
   # filter has been refactored without preserving the skip-path semantics.
   Bash: py -3 -c "import pathlib; t=pathlib.Path('core/scripts/meta-backpressure.py').read_text(encoding='utf-8'); missing=[s for s in ('audit_only_fields','audit_only_skipped','audit_only_skips') if s not in t]; assert not missing, f'audit-only path regressed: {missing}'; print('PASS')"
   # cmd_check output schema — rollback_actions, audit_only_skipped, and
   # graduated MUST all be top-level keys of the JSON return. Consumers
   # (state-update-audit.py) iterate rollback_actions; an LLM consumer that
   # introspects the result depends on audit_only_skipped surfacing the
   # skip count. Drop either and the tracking layer can't see audit-only
   # activity.
   Bash: py -3 -c "import pathlib; t=pathlib.Path('core/scripts/meta-backpressure.py').read_text(encoding='utf-8'); missing=[k for k in ('\"rollback_actions\":', '\"audit_only_skipped\":', '\"graduated\":') if k not in t]; assert not missing, f'cmd_check result schema regressed: {missing}'; print('PASS')"
   # cmd_status surfaces the new field — without this, operators using
   # `meta-backpressure.sh status` cannot see audit-only skips and may
   # mistakenly believe the system is silently rolling back roi_history again.
   Bash: py -3 -c "import pathlib; t=pathlib.Path('core/scripts/meta-backpressure.py').read_text(encoding='utf-8'); assert '\"audit_only_skips\":' in t and '\"total_audit_only_skips\":' in t, 'cmd_status output schema missing audit_only_skips fields'; print('PASS')"
   # Doctrinal cross-reference — the source comments must name g-115-204 and
   # rb-504 so future readers can trace the design rationale. If both
   # disappear, the lineage of the fix is lost.
   Bash: py -3 -c "import pathlib; t=pathlib.Path('core/scripts/meta-backpressure.py').read_text(encoding='utf-8'); assert 'g-115-204' in t and 'rb-504' in t, 'doctrinal cross-reference regressed in meta-backpressure.py'; print('PASS')"

   # Target-state-probe class-name inference fallback (Section BE — g-248-56)
   # Origin failure: g-250-10 (Couple StuckDetector with SpatialMemoryMap)
   # was filed despite g-206-04 already implementing it in
   # CharacterDriverVerticle.java (sibling product repo). The duplication
   # gate's target_state check skipped because extract_targets() found NO
   # file paths — the goal description used class names but no `.java`
   # path. Fix: extract_and_infer_targets walks search_roots looking for
   # files where >=2 distinct class-shaped identifiers co-occur, with
   # self-reference excluded. Surfaces files that contain the feature
   # even when the goal text only names class identifiers. Two-pass
   # strategy (PROJECT_ROOT first, AGENT_WRITE_PATH only on miss) keeps
   # framework-goal cost under ~1s; lenient match pattern (allows dotted
   # prefix) bridges goal-text bare names to code's `module.X` references.
   # Caller threads target_files_inferred → probe lenient_match so the
   # verdict aligns with the inference-time selection.
   Check: `core/scripts/_target_state.py` defines `extract_and_infer_targets`
   Check: `core/scripts/_target_state.py` defines `_resolve_search_roots` (reads `agents/<agent>/local-paths.conf` AGENT_WRITE_PATH)
   Check: `core/scripts/_target_state.py` defines `_infer_targets_from_identifiers` and `_scan_root_for_co_occurrence`
   Check: `core/scripts/_target_state.py` defines `_looks_like_class_name`
   Bash: py -3 -c "import pathlib; t=pathlib.Path('core/scripts/_target_state.py').read_text(encoding='utf-8'); missing=[s for s in ('extract_and_infer_targets','_resolve_search_roots','_infer_targets_from_identifiers','_scan_root_for_co_occurrence','_looks_like_class_name','_INFER_MIN_IDENTIFIERS','target_files_inferred') if s not in t]; assert not missing, f'inference fallback regressed: {missing}'; print('PASS')"
   # Co-occurrence threshold + self-reference exclusion are the false-
   # positive guardrails. Without _INFER_MIN_IDENTIFIERS=2, single-ID
   # matches inflate; without `i != file_stem`, every class file matches
   # itself trivially.
   Bash: py -3 -c "import pathlib; t=pathlib.Path('core/scripts/_target_state.py').read_text(encoding='utf-8'); assert '_INFER_MIN_IDENTIFIERS = 2' in t, 'co-occurrence threshold missing'; assert 'i != file_stem' in t, 'self-reference exclusion missing'; print('PASS')"
   # probe_target_state must accept lenient_match — needed because the
   # default strict pattern excludes `module.X` matches but inference
   # selected files using lenient match. Without this kwarg the probe
   # would say verdict=absent on the very files inference said were
   # strong matches (confusing UX + masks duplicates).
   Bash: py -3 -c "import inspect, sys; sys.path.insert(0,'core/scripts'); from _target_state import probe_target_state; sig=inspect.signature(probe_target_state); assert 'lenient_match' in sig.parameters and 'allowed_roots' in sig.parameters, f'probe_target_state signature regressed: {sig}'; print('PASS')"
   # Caller wiring — both consumers must thread the new args.
   Bash: py -3 -c "import pathlib; t1=pathlib.Path('core/scripts/target-state-probe.py').read_text(encoding='utf-8'); t2=pathlib.Path('core/scripts/goal-duplication-gate.py').read_text(encoding='utf-8'); assert 'extract_and_infer_targets' in t1 and 'allowed_roots=search_roots' in t1 and 'lenient_match=ex.get(\"target_files_inferred\"' in t1, 'target-state-probe.py wiring regressed'; assert 'extract_and_infer_targets' in t2 and 'allowed_roots=search_roots' in t2 and 'lenient_match=ex.get(\"target_files_inferred\"' in t2, 'goal-duplication-gate.py wiring regressed'; print('PASS')"
   # Doctrinal cross-reference — the source comments must name g-248-56
   # and g-250-10 so future readers can trace the design rationale.
   Bash: py -3 -c "import pathlib; t=pathlib.Path('core/scripts/_target_state.py').read_text(encoding='utf-8'); assert 'g-248-56' in t and 'g-250-10' in t, 'doctrinal cross-reference regressed in _target_state.py'; print('PASS')"
   # Behavior-preservation: extract_targets must remain unchanged for
   # explicit-file goals (those with a file.ext path in the description).
   # Inference is a pure addition that fires only when extract_targets
   # returned no target_files. Regression here would mean the new code
   # path interfered with the original extractor.
   Bash: py -3 -c "import sys; sys.path.insert(0,'core/scripts'); from _target_state import extract_targets; ex=extract_targets('Fix retry logic in deploy.sh', 'Update deploy.sh:42 — see retry_with_backoff function'); assert ex['target_files']==['deploy.sh'] and 'retry_with_backoff' in ex['identifiers'] and ex['line_hints']=={'deploy.sh':[42]} and ex['confidence']=='high', f'extract_targets regression: {ex}'; print('PASS')"
   # Smoke positive — synthetic goal with two real co-occurring
   # identifiers (ArgumentParser + PreToolUse, both live in
   # blocker-create-gate.py). Inference must surface that file with
   # target_files_inferred=True. If this fails, the inference walk is
   # broken at the integration level even when unit tests pass.
   Bash: py -3 -c "import sys; sys.path.insert(0,'core/scripts'); from _target_state import extract_and_infer_targets, _resolve_search_roots; ex=extract_and_infer_targets('Wire ArgumentParser into PreToolUse hook', 'Need to integrate ArgumentParser with PreToolUse for the new gate logic.', search_roots=_resolve_search_roots()); assert ex['target_files_inferred'] is True, f'inference smoke failed: {ex}'; assert any('blocker-create-gate.py' in p for p in ex['target_files']), f'expected blocker-create-gate.py in inferred: {ex[\"target_files\"]}'; print('PASS')"

   # Windows MAX_PATH long-path retry helper (Section BE — g-115-165, rb-450)
   # Tree walkers (session_artifacts_count.py, schema-drift-sweep.py)
   # silently dropped legitimately-readable .md files at deeply-nested
   # category paths because Python/Win32 stdlib open() fails on paths
   # exceeding 260 chars. Earlier diagnosis (g-115-160) misattributed it
   # to OneDrive cloud-only placeholders — bash read those fine, ruling
   # out the placeholder hypothesis. Real cause: MAX_PATH limit. Fix:
   # _long_path.open_long_path retries via the Win32 extended-prefix API
   # (\\\\?\\) on OSError, with absolute-resolve + forward-slash conversion
   # to satisfy the API's strict requirements. POSIX no-op. Without this
   # helper, encoding_ratio in the productivity gate undercounts whenever
   # the agent does deep work in nested tree categories.
   Check: `core/scripts/_long_path.py` exists and `py -3 -c "import ast; ast.parse(open('core/scripts/_long_path.py', encoding='utf-8').read())"` succeeds
   Check: `core/scripts/_long_path.py` defines `open_long_path` with Windows-only retry path
   Bash: py -3 -c "import pathlib; t=pathlib.Path('core/scripts/_long_path.py').read_text(encoding='utf-8'); missing=[s for s in ('def open_long_path','os.name != \"nt\"','\\\\\\\\?\\\\\\\\','Path(path).resolve()') if s not in t]; assert not missing, f'long-path helper regressed: {missing}'; print('PASS')"
   # Both consumers must import + use the helper. If either reverts to a
   # plain open() in the affected hot loop, the silent-skip bug returns.
   Bash: py -3 -c "import pathlib; sac=pathlib.Path('core/scripts/session_artifacts_count.py').read_text(encoding='utf-8'); sds=pathlib.Path('core/scripts/schema-drift-sweep.py').read_text(encoding='utf-8'); assert 'from _long_path import open_long_path' in sac, 'session_artifacts_count.py missing helper import'; assert 'open_long_path(md)' in sac, 'session_artifacts_count.py not using helper in tree-walk loop'; assert 'from _long_path import open_long_path' in sds, 'schema-drift-sweep.py missing helper import'; assert 'open_long_path(md)' in sds, 'schema-drift-sweep.py not using helper in tree-walk loop'; print('PASS')"
   # Doctrinal cross-reference — comments must name g-115-165 and rb-450
   # so future readers can trace the OneDrive→MAX_PATH lineage.
   Bash: py -3 -c "import pathlib; t=pathlib.Path('core/scripts/_long_path.py').read_text(encoding='utf-8'); assert 'g-115-165' in t and 'rb-450' in t, 'doctrinal cross-reference regressed in _long_path.py'; print('PASS')"
   # Behavior-preservation: helper raises OSError when file genuinely
   # missing (NOT a silent-success false-positive). The retry path must
   # only kick in for Windows OSError, not swallow legitimate ENOENT.
   Bash: py -3 -c "import sys; sys.path.insert(0,'core/scripts'); from _long_path import open_long_path; raised=False
try: open_long_path('core/scripts/__nonexistent_file_for_smoke__.py')
except OSError: raised=True
assert raised, 'open_long_path should raise OSError on missing file'; print('PASS')"

   # Transplant pack — no shutil.copytree MAX_PATH regression guard (Section TRANSPLANT-MAXPATH — 2026-06-04, e48c16d9, rb-1455, guard-507)
   # /transplant offline mode originally staged the external world/ + meta/ into a
   # PROJECT_ROOT/.transplant-stage-* dir via shutil.copytree before archiving.
   # On Windows that DOUBLED the path prefix (a 242-char source became a 266-char
   # stage path → over the 260 MAX_PATH limit), crashing the pack with WinError 3
   # AND leaking an un-deletable partial stage. shutil.copytree/rmtree/os.walk do
   # NOT reliably honor the \\?\ extended-length prefix (verified 2026-06-04). The
   # fix (e48c16d9) streams source → archive with relative member names (no
   # path-length limit) and lands one file at a time via the \\?\ prefix on
   # open/makedirs. Reintroducing copytree in the pack engine resurrects the
   # showstopper. See tree node windows-maxpath-pathresolution.
   Bash (transplant-no-copytree): test "$(grep -c 'shutil\.copytree(' core/scripts/_transplant_pack.py)" -eq 0 && echo "PASS: _transplant_pack.py has zero shutil.copytree calls (Windows MAX_PATH regression guard)" || echo "FAIL: shutil.copytree reintroduced in _transplant_pack.py — Windows MAX_PATH showstopper regression (rb-1455, e48c16d9); stream source→archive with relative members instead of staging a deep copy"

   # Blocker-ref schema + quiescence log invariants (Section BR — g-251-05)
   # Origin: fresh-eyes-review 2026-04-22 surfaced four structural invariants
   # that the framework relies on but verify-learning didn't enforce. Each
   # closes a known failure mode:
   #
   # (1) BLOCKER_REF_TYPES vs BLOCKER_REF_TTL_HOURS keyset equality.
   #     A new blocker type added without a TTL row → KeyError at expiry
   #     time. A TTL row left after a type was deleted → silent dead config.
   #     The two must move together.
   # (2) Regression guard: create-blocker.py must NOT redefine
   #     BLOCKER_REF_TTL_HOURS locally. rb-436 was a real bug where the
   #     dict was duplicated; the duplicate diverged from aspirations.py's
   #     SSOT and corrupted expiry-time math. create-blocker.py imports the
   #     dict at module top and must stay that way.
   # (3) Live blocked goals with narrative defer must carry blocker_ref.
   #     This is the structural requirement that stops narrative-laundering
   #     into quiescence (see core/config/conventions/goal-schemas.md
   #     "Blocker Reference Schema"). A blocked goal with a free-text
   #     defer_reason but no structured blocker_ref is exactly the leak
   #     the schema was introduced to plug.
   # (4) Quiescence logs (quiescence-log.jsonl, quiescence-audit.jsonl)
   #     must be valid JSONL when present. Absent files are fine
   #     (quiescence not active); a corrupt line in either file
   #     destabilizes the gate's audit trail and downstream analysis.
   Bash: py -3 -c "import sys; sys.path.insert(0,'core/scripts'); from aspirations import BLOCKER_REF_TYPES, BLOCKER_REF_TTL_HOURS; t=set(BLOCKER_REF_TYPES); k=set(BLOCKER_REF_TTL_HOURS.keys()); orphan=k-t; missing=t-k; assert t==k, f'BLOCKER_REF_TYPES vs BLOCKER_REF_TTL_HOURS keyset mismatch: orphan_TTLs={orphan} missing_TTLs={missing}'; print('PASS')"
   # rb-436 regression guard — create-blocker.py must not redefine the TTL
   # dict locally. Count of `BLOCKER_REF_TTL_HOURS = {` definitions in the
   # file MUST be 0 (it imports from aspirations).
   Bash: py -3 -c "import pathlib, re; t=pathlib.Path('core/scripts/create-blocker.py').read_text(encoding='utf-8'); local=len(re.findall(r'^BLOCKER_REF_TTL_HOURS\s*=\s*\\{', t, re.MULTILINE)); assert local == 0, f'rb-436 regression: create-blocker.py defines BLOCKER_REF_TTL_HOURS locally ({local} occurrences) — must import from aspirations.py SSOT instead'; print('PASS')"
   # Live data invariant — blocked goals with defer_reason must carry
   # blocker_ref. Scans both world + agent aspirations.jsonl. A blocker_ref
   # of {} or empty value counts as missing (the structural payload is
   # what matters, not the field's mere presence).
   Bash: MIND_AGENT=bravo py -3 -c "import sys, json; sys.path.insert(0,'core/scripts'); import _paths; viol=[]
for p in (_paths.WORLD_DIR / 'aspirations.jsonl', _paths.AGENT_DIR / 'aspirations.jsonl' if _paths.AGENT_DIR else None):
    if p is None or not p.exists(): continue
    for line in p.read_text(encoding='utf-8').splitlines():
        line=line.strip()
        if not line: continue
        asp=json.loads(line)
        for g in asp.get('goals', []):
            if g.get('status')=='blocked' and g.get('defer_reason') and not g.get('blocker_ref'):
                viol.append((g.get('id'), asp.get('id')))
assert not viol, f'live blocked goals with narrative defer missing blocker_ref ({len(viol)}): {viol[:5]}'
print('PASS')"
   # Quiescence log validity — absent file is fine, corrupt lines fail.
   # Probes the bound agent's session/ directory; if no agent bound, the
   # check passes trivially (no files to inspect).
   Bash: MIND_AGENT=bravo py -3 -c "import sys, json; sys.path.insert(0,'core/scripts'); import _paths; bad=[]
if _paths.AGENT_DIR is not None:
    for fname in ('quiescence-log.jsonl', 'quiescence-audit.jsonl'):
        p = _paths.AGENT_DIR / 'session' / fname
        if not p.exists(): continue
        for i, line in enumerate(p.read_text(encoding='utf-8').splitlines(), 1):
            line = line.strip()
            if not line: continue
            try: json.loads(line)
            except json.JSONDecodeError as e: bad.append((fname, i, str(e)[:60]))
assert not bad, f'corrupt JSONL lines in quiescence logs: {bad[:5]}'
print('PASS')"

   # Archive-sweep recurring corruption scan evidence checks (Section AS)
   Check: `aspirations.py` `cmd_archive_sweep` else-branch scans for corrupted recurring goals (not just completed/retired aspirations)
   Check: else-branch calls `find_recurring_goals(a)` and checks for `status == "completed"`
   Check: else-branch resets corrupted goals to `"pending"` and calls `recompute_progress`
   Check: `remaining.append(a)` is OUTSIDE the `if recurring:` block (aspirations without recurring goals must not be dropped)

   # Bootstrap template integrity check (recurring goals excluded from total_goals)
   Bash: python3 -c "
import json; asp=json.loads(open('core/config/agent-aspirations-initial.jsonl').readline())
p = asp['progress']; goals = asp['goals']
non_rec = [g for g in goals if not g.get('recurring')]
rec = [g for g in goals if g.get('recurring')]
assert p['total_goals'] == len(non_rec), 'total_goals %d != non-recurring %d' % (p['total_goals'], len(non_rec))
assert p.get('recurring_goals', 0) == len(rec), 'recurring_goals %d != actual %d' % (p.get('recurring_goals', 0), len(rec))
print('PASS: total_goals=%d, recurring_goals=%d, actual=%d' % (len(non_rec), len(rec), len(goals)))
" → verify template progress matches actual goal counts

   # Recurring goal evidence checks
   IF asp-001 (Maintain Agent Health) exists in agent-aspirations-read.sh --active:
       Check: asp-001 goals have recurring: true and interval_hours set
       Check: NO recurring goal has status=completed (data layer should prevent this)
       Check: any recurring goal with achievedCount > 0 has updated streak counters
       Bash: goal-selector.sh → verify recurring goals appear/don't appear based on interval elapsed
       Check: if any recurring goal has achievedCount > 1, verify currentStreak is consistent
              (streak should be 1 if previous completion was overdue by > 2x interval)
       Check: goal-selector.sh output recurring_urgency raw value never exceeds 5.0
       Check: g-001-01 has non-empty preconditions in verification block

   # complete-by recurring handling evidence checks
   Check: `core/scripts/aspirations.py` `cmd_complete_by` has `if goal.get("recurring"):` branch
   Check: Recurring branch does NOT set `status = "completed"` (keeps goal pending for next cycle)
   Check: Recurring branch clears `claimed_by` and `claimed_at` (returns goal to pool)
   Check: `complete-by` is NOT in `WORLD_ONLY_COMMANDS` (agents need it for local recurring goals)

   # Recurring goal data-layer guard checks (prevents LLM drift from killing recurring goals)
   Check: `aspirations.py` `cmd_update_goal` blocks `status=completed` on `recurring: true` goals
   Check: `aspirations.py` `cmd_complete` blocks archival of aspirations with `recurring: true` goals (unless --force)
   Check: `aspirations.py` `cmd_archive_sweep` auto-recovers aspirations with recurring goals to active
   Check: `aspirations.py` `recompute_progress` excludes recurring goals from completed/total counts
   Check: `aspirations-read.sh --summary` shows `+ N recurring` suffix for recurring aspirations
   Check: No recurring goals across all live aspirations have status=completed (scan both world and agent JSONL)

   # Recurring-shape-leak prevention (improve-recurring-goals-kind-yao plan, 2026-04-19)
   # Cascading clear at the data primitive (aspirations.py cmd_update_goal — search for `if field == "recurring"`) prevents `recurring=false` goals
   # from leaving orphan timing fields that mislead goal-selector. Removing this cascade resurrects
   # the g-001-01 cargo-cult re-selection bug. Verify both the code AND the data are clean.
   Check: `core/scripts/aspirations.py` `cmd_update_goal` contains `if field == "recurring" and not value:` followed by `goal.pop("interval_hours", None)` and `goal.pop("lastAchievedAt", None)`
   Bash: source core/scripts/_paths.sh && py -c "
import json, sys
sys.path.insert(0, 'core/scripts')
import _paths
bad = []
for src in [_paths.WORLD_DIR / 'aspirations.jsonl']:
    if not src.exists(): continue
    for ln in src.read_text(encoding='utf-8').splitlines():
        ln = ln.strip()
        if not ln: continue
        a = json.loads(ln)
        for g in a.get('goals', []):
            if g.get('recurring') is False and ('interval_hours' in g or 'lastAchievedAt' in g):
                bad.append(f\"{a['id']}/{g['id']}\")
print('SHAPE-LEAK:', bad if bad else 'PASS (zero orphan timing fields on recurring=false goals)')
" → must print PASS

   # Recurring-close infrastructure presence checks
   Check: `core/scripts/recurring-close.sh` exists and `bash -n core/scripts/recurring-close.sh` succeeds
   Check: `core/scripts/cargo-cult-detector.py` exists and `py -c "import ast; ast.parse(open('core/scripts/cargo-cult-detector.py', encoding='utf-8').read())"` succeeds
   Check: `core/scripts/iteration-close.sh` `do_verify` routes recurring goals through `aspirations-complete-by.sh` (grep for `IS_RECURRING.*true.*aspirations-complete-by.sh`)
   Check: `core/config/aspirations.yaml` contains `recurring:` section with `cargo_cult_threshold:` (integer ≥ 2)
   Check: `.claude/skills/aspirations/SKILL.md` mentions `recurring-close.sh` in the per-iteration obligations section
   # Experience-archival canary forced-flip suppression (g-115-634): canary skips
   # when ORIGINAL_OUTCOME=routine AND OUTCOME=deep (Block A/C flip) AND
   # encoding_queue + sensory_buffer are both empty. Forced flip itself is the
   # documentation; placeholder experience entries duplicate routine_streaks /
   # signals / journal. Fail-closed on wm.py read errors — canary runs.
   Check: `recurring-close.sh` canary heredoc passes ORIGINAL_OUTCOME env var
   Bash: grep -c 'ORIGINAL_OUTCOME="$ORIGINAL_OUTCOME"' core/scripts/recurring-close.sh → verify ≥1
   Check: canary heredoc invokes wm.py with the correct `read` subcommand (NOT `get`)
   Bash: grep -c '"wm.py", "get"' core/scripts/recurring-close.sh → verify 0
   Bash: grep -cE '"read", "(encoding_queue|sensory_buffer)", "--json"' core/scripts/recurring-close.sh → verify ≥2
   Check: regression test `test_recurring_close_canary_suppress.py` exists and 5/5 pass
   Bash: test -f core/scripts/tests/test_recurring_close_canary_suppress.py && echo OK || echo MISSING
   Bash: py -3 core/scripts/tests/test_recurring_close_canary_suppress.py 2>&1 | tail -1 → expect "5/5 tests passed"

   # iteration-close-reminder outcome-aware reminder pin (g-115-1138 / sq-018).
   # Origin: zeta investigation g-115-1121 confirmed the hook's generic
   # Skill(aspirations) imperative silently overrode recurring-close.sh's
   # stdout directing Skill(aspirations-spark) first on OUTCOME=deep closes
   # (system-reminder wins over plain stdout). Phase 6 spark was skipped on
   # every deep recurring close. Fix landed in g-115-1138: split reminder
   # text into REMINDER_TEXT_GENERIC + REMINDER_TEXT_DEEP_RECURRING and
   # branch on a stdout marker. These checks pin both reminder constants AND
   # the producer/consumer marker contract so neither side can drift
   # independently and re-introduce the silent spark skip.
   Check: `core/scripts/iteration-close-reminder.py` defines `REMINDER_TEXT_GENERIC` constant
   Bash: grep -c 'REMINDER_TEXT_GENERIC = (' core/scripts/iteration-close-reminder.py → verify ≥1
   Check: `core/scripts/iteration-close-reminder.py` defines `REMINDER_TEXT_DEEP_RECURRING` constant
   Bash: grep -c 'REMINDER_TEXT_DEEP_RECURRING = (' core/scripts/iteration-close-reminder.py → verify ≥1
   Check: `REMINDER_TEXT_DEEP_RECURRING` body directs Skill(aspirations-spark) before Skill(aspirations)
   Bash: grep -c 'Skill(aspirations-spark)' core/scripts/iteration-close-reminder.py → verify ≥2
   Check: detector helper `_is_deep_recurring_close` exists in iteration-close-reminder.py
   Bash: grep -c 'def _is_deep_recurring_close' core/scripts/iteration-close-reminder.py → verify ≥1
   # Producer-side marker pin: recurring-close.sh MUST emit the OUTCOME=deep
   # phrase followed by NEXT ACTION REQUIRED on the same line. The hook's
   # detector regex (_DEEP_RECURRING_RE) tolerates em-dash, double-hyphen,
   # and en-dash separators, so the grep below uses a regex that anchors on
   # the stable phrases and matches any separator. Bypassing the dash
   # character avoids Windows grep encoding quirks while still catching
   # producer drift if either stable phrase changes.
   Check: `core/scripts/recurring-close.sh` emits OUTCOME=deep NEXT ACTION REQUIRED marker
   Bash: grep -cE 'OUTCOME=deep[^A-Za-z]+NEXT ACTION REQUIRED' core/scripts/recurring-close.sh → verify ≥1
   # Consumer-side regex pin: iteration-close-reminder.py must keep its
   # detector regex tolerant of dash-style drift. If a future edit hardcodes
   # a specific dash literal, this check fails — forcing the author to
   # consider symmetry with the producer.
   Check: `core/scripts/iteration-close-reminder.py` _DEEP_RECURRING_RE matches OUTCOME=deep+NEXT ACTION REQUIRED with any separator
   Bash: grep -cE 'OUTCOME=deep.+NEXT ACTION REQUIRED' core/scripts/iteration-close-reminder.py → verify ≥1
   Check: regression test `test_iteration_close_reminder.py` exists and all cases pass
   Bash: test -f core/scripts/tests/test_iteration_close_reminder.py && echo OK || echo MISSING
   Bash: py -3 -m pytest core/scripts/tests/test_iteration_close_reminder.py -q 2>&1 | tail -1 → expect "passed"

   # Cargo-cult placeholder suppression pin (g-115-1089 / g-115-1120, sq-018).
   # Second-layer suppression of Phase 4.25 force_experience_archival sentinel
   # for forced-flip routine→deep closes that produce no substantive artifact.
   # First layer is g-115-634 (empty encoding_queue + sensory_buffer); this
   # extends to cover sessions with non-empty buffers from unrelated earlier
   # work. Probes 4 artifact types (tree-md, new goal, non-status board post,
   # pipeline-meta mtime) in a tight time window. ALL FOUR negative → suppress.
   # Fail-open at every layer: probe errors → sentinel fires as normal.
   # Origin: zeta investigation g-115-1089 → Apply g-115-1120 (alpha).
   Check: `core/config/aspirations.yaml` recurring: block defines cargo_cult_suppress_no_artifact
   Bash: grep -c 'cargo_cult_suppress_no_artifact:' core/config/aspirations.yaml → verify ≥1
   Check: `core/config/aspirations.yaml` recurring: block defines cargo_cult_artifact_window_seconds
   Bash: grep -c 'cargo_cult_artifact_window_seconds:' core/config/aspirations.yaml → verify ≥1
   Check: `core/scripts/recurring-close.sh` Phase 4.25 enforcement block contains g-115-1089 marker
   Bash: grep -c 'g-115-1089' core/scripts/recurring-close.sh → verify ≥1
   Check: `core/scripts/recurring-close.sh` reads cargo_cult_artifact_window_seconds knob
   Bash: grep -c 'cargo_cult_artifact_window_seconds' core/scripts/recurring-close.sh → verify ≥1
   # Fail-open contract: probe exceptions MUST log diagnostic + continue
   # (never block sentinel write). The "artifact probe error" string inside
   # the except block proves the fail-open path is wired.
   Check: `core/scripts/recurring-close.sh` artifact probe handles exceptions (fail-open)
   Bash: grep -c 'artifact probe error' core/scripts/recurring-close.sh → verify ≥1
   Check: regression test `test_recurring_close_cargo_cult_suppression.py` exists and 3/3 pass
   Bash: test -f core/scripts/tests/test_recurring_close_cargo_cult_suppression.py && echo OK || echo MISSING
   Bash: py -3 -m pytest core/scripts/tests/test_recurring_close_cargo_cult_suppression.py -q 2>&1 | tail -1 → expect "3 passed"

   # Exploration noise evidence checks
   Bash: goal-selector.sh → parse first result
       Check: output includes exploration_params with epsilon, noise_scale, noise_weight
       Check: breakdown includes exploration_noise key
       Check: raw includes exploration_noise key (value between 0 and 1)
       Check: exploration_params.noise_weight == epsilon * noise_scale
       Check: core/config/developmental-stage.yaml has exploration.noise_scale
       Check: agents/<agent>/developmental-stage.yaml has exploration.epsilon
       Run goal-selector.sh twice: verify scores differ between invocations (noise is stochastic)

   # Deferred goal evidence checks
   IF any goal has deferred_until set:
       Check: goal with future deferred_until does NOT appear in goal-selector.sh output
       Check: goal with past deferred_until and NO defer_reason DOES appear with deferred_readiness raw = 1.5
       Check: deferred_until persists on goal after completion (not cleared)
   # defer_reason is a functional filter (not just documentation)
   Check: `goal-selector.py` collect_candidates has `if goal.get("defer_reason"): continue` BEFORE deferred_until check
   Check: `goal-selector.py` collect_blocked check 4b has NO `not deferred` guard (blocks regardless of deferred_until)
   IF any goal has defer_reason set:
       Bash: goal-selector.sh 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); ids=[g['goal_id'] for g in d] if isinstance(d,list) else []; print('PASS: defer_reason goals filtered')" → verify deferred goals absent from candidates
       Bash: goal-selector.sh blocked 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); dr=[g for g in d['blocked_goals'] if g['block_reason']=='deferred']; print(f'PASS: {len(dr)} deferred goals in blocked output')" → verify defer_reason goals appear in blocked

   # All-blocked path evidence checks (Section PWB — productive while blocked)
   Check: `goal-selector.py` cmd_select prints JSON object with `all_blocked: true` when candidates empty but blocked goals exist
   Check: `goal-selector.py` cmd_select prints `[]` when no aspirations exist at all (aspirations with all-blocked goals produce the all_blocked object instead)
   Check: `aspirations-select/SKILL.md` Algorithmic Scoring has blocked-goals detection checking for `all_blocked` object
   Check: `aspirations-select/SKILL.md` Phase 2.5b exhaustion check is AFTER the FOR loop (not inside it)
   Check: `aspirations-select/SKILL.md` returns `selection_reason` with value `"all_blocked"` or `"all_blocked_by_gate"`
   Check: `aspirations-select/SKILL.md` all_blocked RETURN includes `selection_context = parsed_output` (orchestrator reads blocked_goals from it)
   Check: `aspirations-select/SKILL.md` all_blocked_by_gate RETURN includes `selection_context` (even if empty)
   Check: `aspirations-select/SKILL.md` Outputs section lists `selection_context` and `selection_reason`
   Check: `aspirations/SKILL.md` return contract comment (line ~138) includes `selection_context` and `selection_reason`
   Check: `aspirations/SKILL.md` all-blocked path is BEFORE the no-goals path (checked first)
   Check: `aspirations/SKILL.md` all-blocked evolve invocation does NOT pass constraint_context (evolve builds its own via wm known_blockers)
   Check: `evolution-triggers.yaml` has `idle_blocked` trigger with `cooldown_sessions: 0`
   Check: `aspirations/SKILL.md` all-blocked path invokes /create-aspiration with constraint_context (avoids blocked resources), then evolution, then research, then reflect as cascading fallbacks
   Check: `aspirations/SKILL.md` all-blocked path only sleeps as absolute last resort after ALL generation attempts fail
   Check: `aspirations/SKILL.md` all-blocked path extracts blocked_skills and blocked_resources from selection_context before generation
   Check: `aspirations/SKILL.md` all-blocked path respects max_evolutions_per_session cap for evolution step
   Check: `core/config/stop-skip-conditions.md` "All goals blocked" line mentions constraint-aware aspiration generation
   Bash: goal-selector.sh 2>/dev/null | python3 -c "
import sys,json
d=json.load(sys.stdin)
if isinstance(d,dict) and d.get('all_blocked'):
    assert 'blocked_count' in d and 'by_reason' in d and 'blocked_goals' in d
    print(f'PASS: all_blocked format correct, {d[\"blocked_count\"]} blocked')
elif isinstance(d,list):
    print(f'PASS: normal array output, {len(d)} candidates')
else:
    print('FAIL: unexpected output format')
" → verify output format is correct (either array or all_blocked object)

   # Exponential backoff sleep evidence checks (Section EB)
   # Signal lifecycle: init in aspirations/SKILL.md Phase -0.5; increment in
   # aspirations-all-blocked/SKILL.md Step B7 (extracted MW-Item-2);
   # reset in core/config/aspirations-loop-digest.md Phase 4.1 Block B ELSE branch.
   Check: `aspirations/SKILL.md` session_signals has `consecutive_blocked_sleeps: 0`
   Check: `aspirations-all-blocked/SKILL.md` Step B7 has `BACKOFF_SCHEDULE = [300, 600, 1200, 1800]` (5/10/20/30min)
   Check: `aspirations-all-blocked/SKILL.md` Step B7 indexes BACKOFF_SCHEDULE with `min(consecutive_blocked_sleeps, len-1)` (capped)
   Check: `aspirations-all-blocked/SKILL.md` Step B7 increments `consecutive_blocked_sleeps` AFTER reading schedule (pre-increment would skip level 0)
   # Reset invariant — without this, backoff inherits stale 1800s sleep after a productive completion follows a blocked episode.
   Bash: grep -c "consecutive_blocked_sleeps = 0" core/config/aspirations-loop-digest.md → expect >= 1
   Bash: grep -c "consecutive_goal_failures = 0" core/config/aspirations-loop-digest.md → expect >= 1
   Check: reset for both counters lives in Phase 4.1 Block B ELSE branch (productive-completion path), NOT in the routine-completion branch above it
   Check: `aspirations/SKILL.md` does NOT reset consecutive_blocked_sleeps on goal failure (only productive completion resets)

   # Idle playbook goal generator evidence checks (Section IP)
   Check: `aspirations/SKILL.md` has Step B2.5 between B2 (create-aspiration) and B3 (evolution)
   Check: Step B2.5 only fires when B2 failed ("create-aspiration: no viable aspirations found" in blocked_idle_attempts)
   Check: Step B2.5 has dedup: reads asp-001 existing goals, filters out items already pending
   Check: Step B2.5 creates ONE goal per cycle then continues (not batch creation)
   Check: Step B2.5 adds goals to agent queue (--source agent asp-001), not world queue
   Check: Step B2.5 playbook does NOT include "Codebase audit" (covered by g-001-12 recurring goal)

   # affected_categories blocker evidence checks (Section AC)
   Check: `core/config/conventions/handoff-working-memory.md` blocker schema includes `affected_categories` field
   Check: `goal-selector.py` collect_candidates builds `blocked_categories` set from `b.get("affected_categories", [])`
   Check: `goal-selector.py` collect_candidates category fallback ONLY fires when `not goal_skill` (no double-blocking)
   Check: `goal-selector.py` collect_blocked builds `blocker_by_category` dict alongside `blocker_by_skill`
   Check: `goal-selector.py` collect_blocked step 2b is AFTER step 2 (skill check) and BEFORE step 3 (dependency)
   Check: `goal-selector.py` trace_root_bottleneck accepts `blocker_by_category=None` (backward-compatible default)
   Check: `goal-selector.py` cmd_blocked passes `blocker_by_category` to `trace_root_bottleneck`
   Check: `aspirations-select/SKILL.md` Phase 2.5b has ELIF for category-based block after skill-based block
   Bash: python3 -c "
import ast, sys
with open('core/scripts/goal-selector.py') as f: src = f.read()
# Verify affected_categories uses .get() with default [] (backward-compatible)
assert 'b.get(\"affected_categories\", [])' in src, 'Missing .get default for affected_categories'
# Verify category fallback is gated on not goal_skill
assert 'if not goal_skill and blocked_categories:' in src, 'Category fallback not gated on null skill'
print('PASS: affected_categories implementation correct')
" → verify backward compatibility and null-skill gating

   # Unreflected hypothesis safety net evidence checks (Section UH)
   Check: `aspirations-consolidate/SKILL.md` has Step 0.5 "Unreflected Hypothesis Sweep" between micro-hypothesis sweep and encoding queue
   Check: Step 0.5 calls `pipeline-read.sh --unreflected` then invokes `/review-hypotheses --learn`
   Check: Consolidation checklist includes `Step 0.5 Unreflected Hyp Sweep`
   Check: `aspirations-learning-gate/SKILL.md` has Phase 9.5c "Unreflected Hypothesis Check"
   Check: Phase 9.5c calls `pipeline-read.sh --unreflected` and invokes `--learn` when count > 0
   Check: Phase 9.5c is MANDATORY for all outcomes (not gated by productive-only)
   Bash: bash core/scripts/aspirations-read.sh --source agent --id asp-001 2>/dev/null | python3 -c "
import sys,json; asp=json.load(sys.stdin)
matches=[g for g in asp['goals'] if g.get('skill')=='/review-hypotheses --learn' and g.get('recurring')]
if matches: g=matches[0]; print(f'PASS: {g[\"id\"]} is recurring --learn goal, interval={g.get(\"interval_hours\",\"?\")}h')
else: print('FAIL: no recurring /review-hypotheses --learn goal in asp-001')
" → verify dedicated --learn recurring goal exists
   Check: `pipeline-read.sh --unreflected` flag exists and returns resolved records with reflected=false
   Check: `boot/SKILL.md` Step 1.5 runs `--resolve` only (learning is downstream — consolidation + learning-gate catch it)

   # Error response protocol evidence checks (Section AJ)
   Check: `.claude/rules/error-response.md` exists with blocker-centric model
   Check: `aspirations-execute/SKILL.md` Phase 4.1 uses `guardrail-check.sh` (NOT `guardrails-read.sh --active`)
   Check: `aspirations-precheck/SKILL.md` Phase 0.5a uses `guardrail-check.sh` (NOT `guardrails-read.sh --active`)
   Bash: guardrail-check.sh --context infrastructure --outcome succeeded --phase post-execution --dry-run → verify returns relevant guardrails
   Bash: guardrail-check.sh --context any --phase pre-selection --dry-run → verify returns relevant guardrails

   # Infrastructure health evidence checks (Section AL)
   Check: `.claude/rules/verify-before-assuming.md` exists with probe-before-concluding imperative
   Check: `core/scripts/infra-health.sh` exists and `core/scripts/infra-health.py` implements check/check-all/status/stale
   Check: `agents/<agent>/infra-health.yaml` exists (components defined per domain deployment)
   Check: Phase 2.5b in aspirations/SKILL.md includes verification probe before accepting blockers
   Check: Phase 0.5b includes success-based blocker clearing via infra-health.yaml
   Bash: bash core/scripts/infra-health.sh status → verify JSON output
   IF any goal was skipped due to infrastructure blocker:
       Check: infra-health.sh check was called BEFORE the blocker was accepted
       Check: agent never declared infrastructure unavailable without a failed probe command

   # Actionable Findings Gate evidence checks (Section AF)
   Check: `aspirations-state-update/SKILL.md` includes Step 8.5 between Step 8 and closing block
   Check: Step 8.5 keyword patterns: root_cause, bug_identified, proposed_fix, unimplemented_action
   Check: Investigation override exists (binary fallback when no keywords match)
   IF any Investigation goal completed with productive outcome during the test:
       Check: journal mentions "Findings gate" or "Step 8.5" for that goal
       Check: if root cause was found, an Unblock goal was created with discovered_by field

   # Aspiration Completion Review evidence checks (Section ACR)
   Check: `aspirations/SKILL.md` Phase 7 includes Phase 7.5 between spark and archival
   Check: `goals_added_to_completing_asp` initialized at Phase 7.5 entry (not a separate flag)
   Check: single archival point guarded by `goals_added_to_completing_asp == 0`
   IF any aspiration completed during the test:
       Check: journal contains "Completion Review" entry for that aspiration
       Check: if outstanding findings detected, goals were created with discovery_type field
       Check: if goals added to completing aspiration, archival was deferred (aspiration still in live file)

   # Mandatory Goal Selection evidence checks (Section AV)
   Check: `core/config/conventions/goal-selection.md` exists with Single Authority Rule
   Check: `aspirations/SKILL.md` conventions list includes `goal-selection`
   Check: `aspirations/SKILL.md` Phase 2 ELSE block has assertion comment referencing convention
   Bash: grep -q "MANDATORY.*goal-selector" core/scripts/postcompact-restore.py → verify reminder present in source
   Check: CLAUDE.md Convention Index includes `goal-selection.md`
   Check: `world/knowledge/tree/system.md` has "Loop Integrity" section

   # /encode-session existence check (sq-018 self-application from encode-session-2026-05-07)
   Check: `.claude/skills/encode-session/SKILL.md` exists with `user-invocable: true` front matter (chat-mode learning consolidation surface — analogue of autonomous-loop Phase 6.5 + Phase 8)
   Bash (encode-session-skill-present): test -f .claude/skills/encode-session/SKILL.md && grep -q 'user-invocable: true' .claude/skills/encode-session/SKILL.md && echo "PASS: /encode-session exists with user-invocable" || echo "FAIL: /encode-session missing or front matter regression"

   # Hybrid skill + completion report evidence checks (Section AN)
   Check: `.claude/skills/agent-completion-report/SKILL.md` has `user-invocable: true`
   Check: CLAUDE.md enforcement rule 1 does NOT list /agent-completion-report in the MUST NOT invoke enumeration
   Check: CLAUDE.md Skill Invocation Rules has "Hybrid skills" bullet
   Bash: bash core/scripts/aspirations-read.sh --source agent --id asp-001 → verify g-001-04 skill references `/agent-completion-report` (or forged wrapper)

   # Board section in completion report (Section AN5)
   Check: `agent-completion-report/SKILL.md` Phase 2 has Step 10 reading board channels via `board-read.sh`
   Check: `agent-completion-report/SKILL.md` Phase 3 has "Message Board" section between "Knowledge" and "Active Work"
   Check: `agent-completion-report/SKILL.md` conventions list includes `board`

   # Report persistence (Section AN8) — reports/ archive abolished by file-model normalization
   Check: `agent-completion-report/SKILL.md` tools_used includes `Write`
   Check: `agent-completion-report/SKILL.md` Phase 4 writes `agents/<agent>/COMPLETION-REPORT.md` (the single latest-pointer report; git history is the archive)
   Check: `agent-completion-report/SKILL.md` Phase 4 does NOT write a timestamped archive under `agents/<agent>/reports/` and does NOT `mkdir` a `reports/` directory (regression guard — reports/ is abolished)
   Check: `agent-completion-report/SKILL.md` Chaining Modifies lists `agents/<agent>/COMPLETION-REPORT.md` and `agents/<agent>/session/last-outcome-snapshot.yaml`, and does NOT list `agents/<agent>/reports/`
   IF `/agent-completion-report` was run during the test:
       Check: `agents/<agent>/COMPLETION-REPORT.md` exists and is non-empty

   # Stop consolidation evidence checks (Section SC)
   Check: `stop/SKILL.md` RUNNING section sets `stop-requested` signal (consolidation delegated to Phase -1.4)
   Check: `stop/SKILL.md` Chaining "Does NOT call" list includes `/aspirations-consolidate`
   # Runner/observer detection must use $MIND_SID, NOT stale persistent files (guard-341, rb-386).
   # The 2026-04-20 hang root-caused to cross-file comparison (latest vs running); single-source-of-truth fixes it.
   Check: `stop/SKILL.md` Step 5 runner/observer detection uses `$MIND_SID` compared against `running-session-id`
   Bash: grep -c '"\$MIND_SID" = "\$runner_sid"' .claude/skills/stop/SKILL.md → verify ≥1
   Bash: grep -c 'runner_sid=\$(cat agents/<agent>/session/running-session-id' .claude/skills/stop/SKILL.md → verify ≥1
   Check: `stop/SKILL.md` Step 5 does NOT read `agents/<agent>/session/latest-session-id` for detection (observer-clobber desync path)
   Bash: grep -c 'cat.*agents/<agent>/session/latest-session-id\|cat.*session/latest-session-id.*current_sid' .claude/skills/stop/SKILL.md → verify 0
   Check: `aspirations/SKILL.md` Phase -1.4 step D4 invokes `/aspirations-consolidate with: stop_mode = true`
   Check: `stop/SKILL.md` does NOT contain "sensory_buffer" or "mini-consolidation" (old approach removed)
   Check: `aspirations-consolidate/SKILL.md` has `## Parameters` section with `stop_mode` documented
   Check: `stop_mode` parameter description lists ALL 5 skipped steps: 7, 7.5, 8, 8.7, 10
   Check: Steps 7, 7.5, 8, 8.7, 10 each have `(skip in stop_mode)` in their heading
   Check: Step 8.7 "Store user goal count" is indented inside the `IF stop_mode != true:` block
   Check: Steps 3 and 4 have `**MANDATORY**` annotation (must not be skipped even when data is empty)
   Check: `### Execution Checklist (MANDATORY)` section exists between Step 9.5 and Step 10
   Check: Execution Checklist lists 24 entries with valid states (done, empty, skipped variants, including Triage line and Step 0.7)
   Check: Step 9 `known_blockers_active` comment references "Step 4 WM archive" (NOT `wm-read.sh` — WM was reset in Step 5)
   Check: Step 9 `knowledge_debts_pending` comment references "Step 2.25" (NOT `wm-read.sh`)
   Check: Step 9 `user_goals_pending` comment mentions stop_mode fallback to aspirations compact data
   Check: Step 8.65 has `IF meta/meta.yaml does not exist:` early exit with log message
   Check: Step 9.5 has `IF file does not exist:` early exit with log message
   Check: Overflow Queue Management has `IF file does not exist:` branch for overflow-queue.yaml
   Check: Step 2.6 has `# MANDATORY` comment for encoding weight adjustment
   IF a /stop was executed during the test:
       Check: `agents/<agent>/session/handoff.yaml` exists (Step 9 ran during stop)
       Bash: wm-read.sh --json → verify working memory is reset (Steps 4-5 ran)
       Check: journal has "## Consolidation" entry with structured format (Step 3 ran)
       Check: output includes "CONSOLIDATION CHECKLIST:" with status for every step
   IF journal has any "## Consolidation" entry (from /stop OR end-of-loop):
       Check: entry uses structured format (contains "Observations processed:" and "Encoded to long-term:" and "Triage:")
       Check: journal has WM archive entry in the same session (Step 4 ran before reset)

   # /start RUNNING guard (Section SC continued)
   Check: `start/SKILL.md` RUNNING branch blocks with error message and recovery instructions (no mode-downgrade path)
   Check: `CLAUDE.md` Session Start Protocol RUNNING branch shows error (does NOT invoke boot or auto-resume)
   Check: `CLAUDE.md` Enforcement Rule 6 says auto-resume is stop-hook-only, not Session Start Protocol

   # Mode ordering constraint (Section SC continued)
   # consolidation has minimum_mode: autonomous — mode must still be autonomous when it runs.
   # The deferred stop sequence enforces this: D4 consolidation runs before D7 post-stop-mode-set.
   # D1-D7 body lives in aspirations-graceful-stop/SKILL.md (extracted MW-Item-2, 2026-04-18).
   Check: `aspirations-graceful-stop/SKILL.md` Phase GS-2 step D1 sets IDLE (session-state-set.sh) BEFORE D4 consolidation
   Check: `aspirations-graceful-stop/SKILL.md` Phase GS-2 step D7 sets post-stop mode (session-mode-set.sh) AFTER D4 consolidation
   Check: `aspirations-consolidate/SKILL.md` has note explaining minimum_mode ordering dependency with /stop

   # Post-stop mode default — the /stop --reader opt-in (Section SC continued, added 2026-04-17)
   # Default post-stop mode is `assistant` (reconciliation-ready), NOT reader. `/stop --reader` opts into read-only.
   # Disk-absence default remains reader (for passive/crashed sessions). These are two different defaults.
   Check: `stop/SKILL.md` Syntax block documents `/stop --reader` flag
   Check: `stop/SKILL.md` Step 0.5 parses `--reader` flag BEFORE resolving agent-name (non-flag positional)
   # Agent-name-required gate (added 2026-04-24 after the bare-/stop wrong-agent incident — see Step 0.5 sub-step 2 in stop/SKILL.md)
   Check: `stop/SKILL.md` Step 0.5 sub-step 2 has the literal phrase "REQUIRED" or "(REQUIRED)" on the agent-name line — bare `/stop` (no positional) MUST be refused with the available-agents list, no fallback to current session binding
   Check: `stop/SKILL.md` Step 0.5 sub-step 2 explicitly states "no fallback to current session binding" (or equivalent) — the previous fallback caused the cross-session wrong-agent stop on 2026-04-24
   Check: `CLAUDE.md` user-control table row for `/stop` notes that agent name is REQUIRED and bare `/stop` is refused
   Check: `README.md` /stop rows show `<agent-name>` as required positional (not `[agent-name]` optional)
   Check: `stop/SKILL.md` RUNNING branch step 1 writes `agents/<agent>/session/stop-target-mode` BEFORE the idempotent guard (so `/stop` → `/stop --reader` can change target)
   Check: `stop/SKILL.md` RUNNING branch has inline comment: "Do not move this below the idempotent guard"
   Check: `stop/SKILL.md` IDLE branch default target_mode is `assistant` (not `reader`)
   Check: `aspirations-graceful-stop/SKILL.md` Phase GS-0 reads `agents/<agent>/session/stop-target-mode` ONCE at entry and caches the value as `target_mode` (race-safe pre-read — file may be deleted mid-stop by parallel agent's recovery-gate manifest-clear; observed alpha session-61 2026-05-07)
   Check: `aspirations-graceful-stop/SKILL.md` Phase GS-0 defaults `target_mode` to "assistant" if file missing AND appends a `stop_target_mode_missing_at_gs0` desync-warning to `agents/<agent>/session/desync-warnings.jsonl` (invariant violation must remain visible even though the stop completes cleanly with the default)
   Check: `aspirations-graceful-stop/SKILL.md` D7 uses `{target_mode}` (LLM-substituted from GS-0 cache) — does NOT re-read the file with `cat` or `tr`; `rm -f` cleanup remains idempotent regardless of file presence
   Check: `aspirations-graceful-stop/SKILL.md` D7 still uses a single `Bash:` line with `&&` chaining (shell vars do NOT persist across separate Bash: tool calls — see guard-128)
   Check: `aspirations-graceful-stop/SKILL.md` D7 has inline invariant comment naming stop/SKILL.md Step 0.5 as the parser that produces only {assistant, reader}
   Check: `CLAUDE.md` Mode table marks `assistant` as "(post-stop default)" and `reader` as "(safe floor)"
   Check: `CLAUDE.md` user-control table row for `/stop` says "drop to assistant (or reader with `--reader`)"
   Check: `README.md` commands table describes `/stop` as landing in assistant mode and lists `/stop --reader`
   Bash: grep -rn "drop to reader\|returns to reader\|safe default" CLAUDE.md README.md .claude/skills/stop/ .claude/skills/aspirations/ .claude/skills/aspirations-graceful-stop/ → verify no stale references (only the new `/stop --reader` descriptions and neutral doc text should match)
   Bash: grep -n "echo assistant\|echo reader" .claude/skills/aspirations/SKILL.md → verify 0 hits in aspirations orchestrator (no silent defaults there)
   Bash: grep -n "echo \"assistant\"\|echo assistant" .claude/skills/aspirations-graceful-stop/SKILL.md → expect EXACTLY 1 hit, the Phase GS-0 race-safe default. Any second hit is a regression (e.g., D7 sneaking back a fallback) — the cached `target_mode` already encodes the default, so D7 must use `{target_mode}` substitution only.

   # /stop self-completion + observer-session guard (Section SC continued, added 2026-04-19)
   # /stop RUNNING branch chains into the aspirations loop as its final action so Phase -1.4
   # runs in the SAME user turn (interactive CLI does not reliably trigger a new model turn
   # when the previous turn ended in text-only output — the Stop hook BLOCK is a safety net,
   # not the normal path). Observer sessions (started via /start --mode reader|assistant while
   # RUNNING) MUST NOT chain — graceful-stop D1 writes agent-state and D7 writes agent-mode,
   # which is the runner's contract. Crashed-runner (missing running-session-id) counts as
   # observer — fail open; the runner's Stop hook picks up the signal at next boundary.
   Check: `stop/SKILL.md` RUNNING branch Step 5 invokes `Skill: aspirations` with args `loop` as its final action (deterministic handoff, not hook-dependent)
   Check: `stop/SKILL.md` Step 5 has a runner-vs-observer SID guard BEFORE the chain (reads both latest-session-id and running-session-id; only chains when they match)
   Check: `stop/SKILL.md` Step 5 explicitly notes "Missing running-session-id counts as observer" — fail open on crashed runners
   Check: `stop/SKILL.md` Step 2 idempotent-guard branch states "SKIP Step 3. Continue to Step 4." (re-entry still reaches the chain)
   Bash: grep -c 'running-session-id' .claude/skills/stop/SKILL.md → verify >= 1 (Step 5 guard reads it)
   Bash: grep -cE 'Skill: .?aspirations.? .*loop' .claude/skills/stop/SKILL.md → verify >= 1 (RUNNING branch chains the loop)

   # Graceful-stop D7 terminal-Bash structure (Section SC continued, added 2026-04-19)
   # D7 merges the former D7 (mode-set) + D8 (user-message) into ONE Bash line so the skill's
   # last action is a tool call (per return-protocol.md), not a text Output block. Do NOT split
   # this back into separate steps — that reintroduces the text-ending-turn bug that left /stop
   # hanging until the user typed something (fixed 2026-04-19).
   Check: `aspirations-graceful-stop/SKILL.md` D7 is a single Bash line whose control flow ends with `fi` (shell if/else), NOT a text `Output:` block
   Check: `aspirations-graceful-stop/SKILL.md` D7 uses heredocs `<<'EOF'` (single-quoted, prevents variable expansion) for both assistant and reader messages
   Check: `aspirations-graceful-stop/SKILL.md` D7 comment warns that session-mode-set.sh is NOT a second line of defense (it accepts "autonomous" — only /stop's Step 0.5 flag parser guarantees target ∈ {assistant, reader})
   Check: `aspirations-graceful-stop/SKILL.md` front-matter description and Phase GS-2 header both say "D1-D7" (reflecting the D7+D8 merge)
   Bash: grep -rn 'D1-D8\|D1 through D8\|step D8\|phase D8' --exclude-dir=verify-learning .claude/skills/ core/config/ 2>/dev/null → MUST be empty (D8 merged into D7; verify-learning self-references are excluded)
   Bash: grep -c "tr -d '" .claude/skills/aspirations-graceful-stop/SKILL.md → verify >= 2 (D6 + D7 both use the per-agent-session-file-read idiom `| tr -d '\r\n'`)

   # /start stop-signal cleanup single-source (Section SC continued, added 2026-04-19)
   # Only ONE cleanup site for stop-requested / stop-loop on IDLE entry: Step 2.5. Do NOT
   # duplicate the clears inside the reader/assistant/autonomous sub-paths. UNINITIALIZED
   # cannot have these signals (Phase C0 runs init-mind.sh which creates a fresh session/),
   # so Phase C8 autonomous MUST NOT contain defensive clears either (dead code removed
   # 2026-04-19). The `loop-active` clear in the autonomous sub-path is a DIFFERENT signal
   # (loop heartbeat) — keep it there; it does not belong in Step 2.5.
   Check: `start/SKILL.md` IDLE branch has Step 2.5 "Clear stale stop signals" running for ALL modes (single cleanup site)
   Check: `start/SKILL.md` IDLE autonomous sub-path does NOT re-clear `stop-requested` or `stop-loop` (Step 2.5 already did it)
   Check: `core/config/start-phase-c.md` (Phase C digest) UNINITIALIZED Phase C8 autonomous has NO `session-signal-clear.sh stop-loop` or `session-signal-clear.sh stop-requested` calls
   Bash: grep -cE 'session-signal-clear.sh (stop-loop|stop-requested)' .claude/skills/start/SKILL.md → verify exactly 4 hits (Step 0.7 recovery branch clears stop-requested + stop-loop = 2; Step 2.5 IDLE cleanup clears stop-requested + stop-loop = 2; total 4. If the count changes, trace the new caller — any new site may indicate an unauthorized stop-signal write outside /start and /stop.)
   Bash: grep -cE 'session-signal-clear.sh loop-active' .claude/skills/start/SKILL.md → verify >= 1 (autonomous sub-path clears loop-active, a different signal, correctly scoped)

   # Crashed-runner heartbeat + /start --recover (Section SC continued, added 2026-04-19)
   # Runner stamps agents/<agent>/session/runner-heartbeat every aspirations iteration.
   # Observer /stop and /start RUNNING+autonomous use core/scripts/heartbeat-stale.sh
   # to detect crashed runners and route the user to /start <agent> --recover.
   # Staleness threshold lives in core/config/aspirations.yaml (no hardcoded default).
   Check: `aspirations/SKILL.md` Phase -0.5 calls `bash core/scripts/heartbeat-tick.sh` AFTER the `session-signal-set.sh loop-active` line (the script touches runner-heartbeat AND bumps team-state.last_active in one call — every iteration stamps mtime locally AND advertises liveness cross-agent). Pre-2026-04-20 this was inline `touch` + inline `team-state-update.sh`; the script extraction (rb-399) ensures future heartbeat additions apply in-flight to running loops without waiting for autocompact to reload SKILL.md.
   Check: `core/scripts/heartbeat-tick.sh` exists, sources `_paths.sh`, touches `"$AGENT_DIR/session/runner-heartbeat"` (pure-mtime — no content write), AND calls `team-state-update.sh ... agent_status.$MIND_AGENT.last_active` (single-source heartbeat tick — adding heartbeat fields here, NOT to SKILL.md, applies in-flight to every running agent). Executable bit is irrelevant: every call site uses `bash <path>`, matching sibling heartbeat-stale.sh + team-state-update.sh which are also committed mode 100644 on Windows.
   Bash: grep -cE '^\s*touch \"\$AGENT_DIR/session/runner-heartbeat\"' core/scripts/heartbeat-tick.sh → verify >= 1 (pure-mtime touch)
   Check: `core/scripts/heartbeat-tick.sh` does NOT use `2>/dev/null` on the team-state write — fail-open via `|| true` only (rb-400 silent-boundary). Bash: `grep -cE 'team-state-update\.sh.*2>/dev/null' core/scripts/heartbeat-tick.sh` must be 0 (anchored to the team-state call so the negative-narration comment 'no 2>/dev/null' on rb-400 doesn't false-positive).
   Check: `core/scripts/heartbeat-stale.sh` exists, is executable, and sources both `_paths.sh` and `_platform.sh` (same pattern as other shell wrappers)
   Check: `core/scripts/heartbeat-stale.sh` prints `stale` when the heartbeat file is missing (fail-open for crashed-runner edge case)
   Check: `core/scripts/heartbeat-stale.sh` reads `stale_minutes` from `core/config/aspirations.yaml` with NO hardcoded shell default (fails loud on missing block)
   Check: `core/config/aspirations.yaml` has a top-level `runner_heartbeat.stale_minutes` block
   Check: `stop/SKILL.md` Step 5 observer branch invokes `heartbeat-stale.sh` BEFORE the DONE message
   Check: `stop/SKILL.md` observer branch has a distinct user-facing message when heartbeat is stale (mentions `/start <agent-name> --recover`)
   Check: `start/SKILL.md` Step 0.5 parses `--recover` and `--force` flags (flag-parse BEFORE positional extraction — same idiom as `--reader` in /stop Step 0.5)
   Check: `start/SKILL.md` Step 0.7 recovery branch runs BEFORE Step 1's state check
   Check: `start/SKILL.md` Step 0.7 preconditions: state is RUNNING AND (heartbeat stale OR `--force`). Refuses with clear error otherwise (no state changes).
   Check: `start/SKILL.md` Step 0.7 recovery clears `running-session-id`, `iteration-checkpoint.json`, `compact-pending`, `compact-checkpoint.yaml`, `runner-heartbeat`, and signals `stop-requested`/`stop-loop`/`loop-active`, then `session-state-set.sh IDLE`, then falls through to IDLE branch
   Check: `start/SKILL.md` Step 0.7 precondition probes (`session-state-get.sh`, `heartbeat-stale.sh`) use explicit `MIND_AGENT=<agent-name>` prefix — Step 0.7 runs BEFORE the IDLE-branch session rebind so `_paths.sh`'s first-available-conf fallback would probe the wrong agent without the prefix. Enforced by guard-307 / rb-323.
   Check: `start/SKILL.md` IDLE autonomous sub-path touches `runner-heartbeat` BEFORE `session-state-set.sh RUNNING` — upholds the invariant "state=RUNNING ⟹ fresh heartbeat exists" from the transition moment, closing the observer-probe race documented in rb-323.
   Bash: awk '/^### IDLE/,/^### UNINITIALIZED/' .claude/skills/start/SKILL.md | grep -nE 'bash core/scripts/heartbeat-tick\.sh|bash core/scripts/session-state-set\.sh RUNNING' | head -2 → verify two lines, first matches `bash core/scripts/heartbeat-tick.sh` (the seed call that touches runner-heartbeat) and precedes the second matching `bash core/scripts/session-state-set.sh RUNNING`. Anchored to the `bash <path>` invocation so documentation prose that mentions the script names doesn't false-match.
   Check: `start/SKILL.md` RUNNING+autonomous branch invokes `heartbeat-stale.sh` and includes `--recover` hint in the refusal message when stale
   Check: `.claude/rules/user-interaction.md` Script-Level Restrictions notes `/start --recover` as an authorized caller of `session-state-set.sh`
   Bash: grep -c 'heartbeat-tick.sh' .claude/skills/aspirations/SKILL.md → verify >= 1 (Phase -0.5 calls the script wrapper that touches runner-heartbeat AND bumps team-state.last_active)
   Bash: grep -c 'runner-heartbeat' core/scripts/heartbeat-tick.sh → verify >= 1 (the local-mtime touch lives inside the script, not inline in SKILL.md)
   Bash: grep -c 'heartbeat-stale.sh' .claude/skills/stop/SKILL.md .claude/skills/start/SKILL.md → verify >= 2 (both skills invoke the probe)
   Bash: source core/scripts/_paths.sh && bash core/scripts/heartbeat-tick.sh --bypass-state && [ -f "$AGENT_DIR/session/runner-heartbeat" ] && echo PASS || echo FAIL → smoke test: source (not bash) is required so $AGENT_DIR survives into the [ -f ] check. `--bypass-state` is required because verify-learning may run from an IDLE assistant/observer session — without bypass the IDLE-state gate (2026-05-13 hardening, see heartbeat-tick.sh header for the `heartbeat_without_running` desync rationale) refuses the tick. Side effect: stamps NOW into team-state.last_active, which is the intended SSOT behavior.
   Check: `heartbeat-tick.sh` IDLE-state gate is present (2026-05-13, prevents the `heartbeat_without_running` desync — alpha cbb27ab3 incident). Bash: `grep -cE 'if \[ "\$STATE" = "IDLE" \]' core/scripts/heartbeat-tick.sh` must be >= 1.
   Check: `heartbeat-tick.sh` gate suppresses no stderr (rb-400 silent-boundary discipline). Bash: `grep -cE 'session-state-get\.sh.*2>/dev/null' core/scripts/heartbeat-tick.sh` must be 0 (any `2>/dev/null` on the state-get call regresses rb-400).
   Check: `start/SKILL.md` IDLE Step 3 (SKILL.md) AND UNINITIALIZED Phase C8 (core/config/start-phase-c.md digest) both pass `--bypass-state` to heartbeat-tick (the only two authorized bypass sites). Bash: `cat .claude/skills/start/SKILL.md core/config/start-phase-c.md | grep -cE 'heartbeat-tick\.sh --bypass-state'` must be >= 2.
   Check: `aspirations/SKILL.md` Phase -1.5 abort path terminates on a `Bash:` tool call, not a text `Output:` (guard-454 — trailing prose ends the loop). Bash: `awk '/^# Phase -1\.5: /,/^# Phase -1\.4: /' .claude/skills/aspirations/SKILL.md | grep -cE '^\s*Bash: \`echo'` must be >= 1. (The `: ` after the phase number anchors to section headers — bare `Phase -1.4` appears inside the CRITICAL comment as a reference and would terminate the awk range too early.)
   Bash: bash core/scripts/heartbeat-stale.sh | grep -qE '^(stale|fresh)$' && echo PASS || echo FAIL → verify PASS (probe prints exactly one of TWO values on stdout — fresh/stale; pure-mtime model, see compact-recovery.md)

   # Pure-mtime heartbeat + B7 wait-state tick (2026-04-21 simplification)
   # runner-heartbeat carries no content; heartbeat-stale returns fresh/stale based
   # purely on mtime. The 60s tick inside interruptible-sleep.sh keeps mtime fresh
   # during B7 cap sleeps (1800s). Removed the identity-comparison path that
   # produced latent `orphaned` verdicts after every autocompact — see
   # compact-recovery.md "Incident reference" for the asp-240 / 2026-04-21 trace.
   Check: `core/scripts/heartbeat-stale.sh` emits only "fresh" or "stale" (no third output)
   Bash: grep -cE 'echo orphaned' core/scripts/heartbeat-stale.sh → verify == 0 (orphaned branch removed under pure-mtime)
   Bash: grep -cE 'echo (fresh|stale)' core/scripts/heartbeat-stale.sh → verify >= 2 (both two-way branches present)
   Check: `core/scripts/recovery-gate.sh` Condition 2 triggers on "stale" only
   Bash: grep -cE '\$hb" == "stale" \|\| "\$hb" == "orphaned"' core/scripts/recovery-gate.sh → verify == 0 (union removed; the or-orphaned clause must not return)
   Bash: grep -cE '\[\[ "\$hb" == "stale" \]\]' core/scripts/recovery-gate.sh → verify >= 1 (single-term gate present)
   Check: `stop/SKILL.md` Step 5 observer branch handles only "fresh" and "stale" (no orphaned branch, no self-promote)
   Bash: grep -cE 'IF output is "orphaned":' .claude/skills/stop/SKILL.md → verify == 0 (orphaned branch removed)
   Bash: grep -cE 'Self-promoted to runner' .claude/skills/stop/SKILL.md → verify == 0 (self-promote output removed — dead code under pure-mtime)
   Check: `core/scripts/interruptible-sleep.sh` ticks the heartbeat every 60s during long waits
   Bash: grep -cE 'heartbeat-tick' core/scripts/interruptible-sleep.sh → verify >= 1 (the 60s periodic tick that prevents mtime from aging past threshold during B7 cap sleep)
   Check: `core/config/conventions/session-state.md` has a "Runner Heartbeat" section documenting the pure-mtime two-way probe
   Bash: grep -cE '^# Runner Heartbeat' core/config/conventions/session-state.md → verify >= 1
   Bash: grep -cE 'Two-way probe output' core/config/conventions/session-state.md → verify >= 1
   Check: `core/scripts/runner-token-mint.sh` does NOT exist (runner-token scheme was removed 2026-04-21)
   Bash: test ! -f core/scripts/runner-token-mint.sh && echo PASS || echo FAIL → verify PASS

   # /start auto-recovery (zombie gate, Section SC continued, added 2026-04-25;
   # extended to 5-condition multi-signal liveness 2026-05-09 — g-115-492)
   # /start <name> in RUNNING+autonomous branch auto-recovers when the 5-condition
   # zombie gate passes — same gate as recovery-gate.sh's run_gate_for_agent. Catches
   # crashed runners without requiring the user to type --recover. The gate AND every
   # probe call MUST mirror recovery-gate.sh exactly; any divergence breaks the
   # "single source of truth" contract. Three failure modes prevented here:
   #   - B1: probe-script mismatch (pending-agents vs background-jobs) → divergent gates
   #   - B2: probes without explicit MIND_AGENT prefix → _paths.sh's no-agent fallback
   #     returns auto-recovery-passing values for every probe → live runners clobbered
   #   - B3: heartbeat-only liveness (Cond 2 alone) → transient platform-hook timeouts
   #     (e.g., 2.1.133 stop-hook regression) make heartbeat staleness false-positive,
   #     stomping live runners. Cond 2.5 (recent BLOCK in .stop-hook-log) catches it.
   # See rb-510, rb-511, guard-433, g-115-492. Caught by 2026-04-25 fresh-eyes review.
   Check: `start/SKILL.md` RUNNING+autonomous branch lists the same 5 conditions as recovery-gate.sh's `run_gate_for_agent` (state=RUNNING, heartbeat=stale, no recent BLOCK, no stop-requested, `background-jobs.sh has-pending` exits 1) — NOT pending-agents.sh
   Bash: grep -nE 'pending-agents\.sh has-pending' .claude/skills/start/SKILL.md → verify 0 hits in the auto-recovery branch (use background-jobs.sh has-pending only; pending-agents.sh is the stop-hook's tracker, not the recovery gate's)
   Bash: grep -cE 'background-jobs\.sh has-pending' .claude/skills/start/SKILL.md → verify >= 1 (auto-recovery branch probes background-jobs to match recovery-gate.sh)
   Check: `start/SKILL.md` RUNNING+autonomous branch every probe call (heartbeat-stale.sh, session-signal-exists.sh, background-jobs.sh) is prefixed with explicit `MIND_AGENT=<agent-name>` — without the prefix, _paths.sh's no-agent fallback (lines 68, 92-96) makes every probe return the auto-recovery-passing value regardless of agent health, clobbering live runners
   Bash: awk '/^#### RUNNING \+ requested mode is .autonomous/,/^### IDLE/' .claude/skills/start/SKILL.md | grep -cE '^Bash: .bash core/scripts/(heartbeat-stale|session-signal-exists|background-jobs|runner-recent-block)' → verify == 0 (every such Bash line MUST have MIND_AGENT=<agent-name> prefix; the bare-form count must be zero)
   Bash: awk '/^#### RUNNING \+ requested mode is .autonomous/,/^### IDLE/' .claude/skills/start/SKILL.md | grep -cE 'MIND_AGENT=<agent-name> bash core/scripts/(heartbeat-stale|session-signal-exists|background-jobs|runner-recent-block)' → verify >= 4 (one prefix per probe: heartbeat + recent-block + stop-requested + background-jobs)
   Check: `start/SKILL.md` RUNNING+autonomous branch contains a CRITICAL warning paragraph naming the MIND_AGENT-prefix discipline as the failure mode (rb-510 / guard-433)
   Bash: awk '/^#### RUNNING \+ requested mode is .autonomous/,/^### IDLE/' .claude/skills/start/SKILL.md | grep -cE 'Every probe call MUST use the explicit .MIND_AGENT=' → verify >= 1 (the CRITICAL warning is present)
   Check: `start/SKILL.md` RUNNING+autonomous branch contains a CRITICAL note that the gate's conditions and probe scripts MUST match recovery-gate.sh (rb-511 cross-reference discipline)
   Bash: awk '/^#### RUNNING \+ requested mode is .autonomous/,/^### IDLE/' .claude/skills/start/SKILL.md | grep -cE 'MUST match .core/scripts/recovery-gate\.sh' → verify >= 1
   Check: `core/scripts/recovery-gate.sh` `run_gate_for_agent` has the reciprocal CRITICAL comment naming start/SKILL.md as the parallel implementation
   Bash: grep -cE 'mirrored in .\\.claude/skills/start/SKILL\.md' core/scripts/recovery-gate.sh → verify >= 1 (the reciprocal cross-reference comment is present)
   Check: `start/SKILL.md` RUNNING+autonomous auto-recover sub-branch fails loud when `session-state-set.sh IDLE` exits non-zero (does NOT fall through to IDLE branch) — same loud-fail discipline as recovery-gate.sh's `_perform_recovery` and Step 0.7
   Bash: grep -cE 'fail loud — do NOT fall through' .claude/skills/start/SKILL.md → verify >= 2 (both Step 0.7 explicit-recover and the auto-recover branch carry the loud-fail block)
   Check: `.claude/rules/user-interaction.md` Script-Level Restrictions enumerates THREE authorized /start sub-paths for `session-state-set.sh` (IDLE→RUNNING in IDLE branch; RUNNING→IDLE in `/start --recover`; RUNNING→IDLE in auto-recovery branch) and describes recovery-gate.sh as a 5-condition AND-gate (g-115-492 multi-signal liveness; the previous 4-condition gate stomped live runners on transient hook timeouts)
   Bash: grep -cE '5-condition zombie gate|5-condition AND-gate' .claude/rules/user-interaction.md → verify >= 1 (5-condition language present)
   Bash: grep -cE '4-condition AND-gate|4-condition zombie gate' .claude/rules/user-interaction.md → verify == 0 (the stale 4-condition reference must not return — superseded by g-115-492)

   # Section ZG: zombie-gate Condition 2.5 multi-signal liveness (g-115-492, 2026-05-09)
   # Heartbeat alone is too weak when transient platform issues cause heartbeat
   # staleness on live runners. Condition 2.5 (runner-recent-block.sh) cross-checks
   # by reading .stop-hook-log: a recent BLOCK proves the loop fired AND re-entered.
   # If 2.5 regresses (probe missing, condition skipped, or window too short), the
   # 2026-05-09 cross-binding stomp recurs — silent loop death plus orphaned runner.
   Check: `core/scripts/runner-recent-block.sh` exists. Bash: `test -x core/scripts/runner-recent-block.sh && echo PASS || { echo "FAIL: runner-recent-block.sh missing or not executable — Condition 2.5 broken"; exit 1; }`
   Check: `core/scripts/recovery-gate.sh` `run_gate_for_agent` calls runner-recent-block.sh. Bash: `grep -cE 'runner-recent-block\.sh' core/scripts/recovery-gate.sh` must be >= 1.
   Check: `core/scripts/recovery-gate.sh` Cond 2.5 uses the rc-check pattern matching Cond 4 (rc != 1 = hold back). Bash: `awk '/Condition 2.5:/,/Condition 3:/' core/scripts/recovery-gate.sh | grep -cE '\[\[ \$rb_rc -eq 1 \]\] \|\| return 0' && echo PASS || { echo "FAIL: Cond 2.5 does not match Cond 4 rc-pattern — script errors would let recovery proceed"; exit 1; }`
   Check: `core/scripts/recovery-gate.sh` Cond 2.5 has the CRITICAL comment forbidding the rc==0 idiom (which leaves rc=2 falling through). Bash: `awk '/Condition 2.5:/,/Condition 3:/' core/scripts/recovery-gate.sh | grep -cE 'CRITICAL.*Cond 4|DO NOT change.*if rc==0' && echo PASS || { echo "FAIL: CRITICAL comment missing — rc=2-fall-through regression risk"; exit 1; }`
   Check: `start/SKILL.md` RUNNING+autonomous branch references runner-recent-block.sh. Bash: `grep -cE 'runner-recent-block\.sh' .claude/skills/start/SKILL.md` must be >= 2 (definition in the conditions list + probe call).
   Check: `start/SKILL.md` RUNNING+autonomous branch describes the gate as having FIVE conditions (not four). Bash: `grep -cE 'ALL FIVE|5-condition zombie gate' .claude/skills/start/SKILL.md` must be >= 1.
   Check: `start/SKILL.md` Cond 2.5 mirrors recovery-gate.sh's rc != 1 pattern (not rc != 0). Bash: `grep -cE 'recent_block_rc != 1' .claude/skills/start/SKILL.md` must be >= 1.

   # Section CCM: recovery-gate.sh condition-count mirror invariant (g-115-577, 2026-05-10)
   # The recovery-gate.sh header (around L7) spells out the count word ("ALL SIX conditions");
   # internal mirror comments (around L85, L95, L287) use the numeric form ("6-condition layer",
   # "6-condition gate"). Drift between these caused the 4→5 regression caught only by g-115-498
   # fresh-eyes review; the same drift class would silently regress 6→7 or beyond if conditions
   # are added/removed without updating the mirrors. Catches future drift mechanically.
   Check: `core/scripts/recovery-gate.sh` numeric `N-condition` references all share a single value (no drift between header count and inline mirrors)
   Bash (single-distinct-count): test "$(grep -oE '[0-9]+-condition' core/scripts/recovery-gate.sh | sort -u | wc -l)" -eq 1 && echo "PASS: single condition count across N-condition references" || { echo "FAIL: drift in N-condition mirrors — counts found: $(grep -oE '[0-9]+-condition' core/scripts/recovery-gate.sh | sort -u | tr '\n' ' ')"; exit 1; }
   Check: `core/scripts/recovery-gate.sh` header `ALL [WORD] conditions` count matches numeric mirrors (manual word-to-numeral table; expand if conditions cross 10)
   Bash (header-numeric-sync): w=$(grep -oE 'ALL [A-Z]+ conditions' core/scripts/recovery-gate.sh | head -1 | awk '{print $2}'); n=$(grep -oE '[0-9]+-condition' core/scripts/recovery-gate.sh | head -1 | grep -oE '^[0-9]+'); declare -A m=([ONE]=1 [TWO]=2 [THREE]=3 [FOUR]=4 [FIVE]=5 [SIX]=6 [SEVEN]=7 [EIGHT]=8 [NINE]=9); test "${m[$w]:-}" = "$n" && echo "PASS: header word $w == numeric $n" || { echo "FAIL: header word $w does not map to numeric $n in recovery-gate.sh"; exit 1; }
   Check: `core/scripts/recovery-gate.sh` enumerates at least N inline `─── Condition N:` section headers in run_gate_for_agent (matches the header count)
   Bash (enumerated-conditions-match): n=$(grep -oE '[0-9]+-condition' core/scripts/recovery-gate.sh | head -1 | grep -oE '^[0-9]+'); enum=$(grep -cE "─── Condition [0-9.]+:" core/scripts/recovery-gate.sh); test "$enum" -ge "$n" && echo "PASS: enumerated conditions ($enum) >= header count ($n)" || { echo "FAIL: header says $n conditions but only $enum '─── Condition N:' inline headers found"; exit 1; }

   # Signal-file triple-registration (guard-374, rb-442 sister case — 2026-04-22)
   # Every signal file read by interruptible-sleep.sh MUST exist in three places:
   #   (1) core/scripts/session.py VALID_SIGNALS — so session-signal-set.sh accepts it
   #   (2) core/config/session-manifest.yaml `files:` with recovery_action: clear
   #   (3) the consumer's [ -f ] / rm -f path in interruptible-sleep.sh
   # If any of the three drops, writers break silently or stale signals survive crashes.
   Check: `core/scripts/session.py` VALID_SIGNALS includes the three quiescence wake signals
   Bash: grep -oE '"(board-activity|email-received|goal-claim-released)"' core/scripts/session.py | wc -l → verify == 3
   Check: `core/config/session-manifest.yaml` has a `recovery_action: clear` entry for each quiescence wake signal
   Bash: grep -cE '^  - file: (board-activity|email-received|goal-claim-released)$' core/config/session-manifest.yaml → verify == 3
   Check: interruptible-sleep.sh polls the quiescence wake signals
   Bash: grep -cE '(BOARD_ACTIVITY_FILE|EMAIL_RECEIVED_FILE|CLAIM_RELEASED_FILE)' core/scripts/interruptible-sleep.sh → verify >= 6 (≥ declare + ≥ one poll-check per signal)

   # BLOCKER_REF_TYPES single-source (rb-428 session cleanup — 2026-04-22)
   # create-blocker.py --blocker-type choices must come from aspirations.BLOCKER_REF_TYPES,
   # not a hand-maintained string-literal list. Two lists drift.
   Check: create-blocker.py imports BLOCKER_REF_TYPES from aspirations at module top
   Bash: grep -cE 'from aspirations import.*BLOCKER_REF_TYPES' core/scripts/create-blocker.py → verify >= 1
   Check: create-blocker.py --blocker-type choices= uses the imported tuple, not a literal list
   Bash: grep -cE 'choices=\["infrastructure", "resource"' core/scripts/create-blocker.py → verify == 0 (hand-maintained list removed)
   Bash: grep -cE 'choices=list\(BLOCKER_REF_TYPES\)' core/scripts/create-blocker.py → verify >= 1

   # experience-staleness-check single-source for threshold (rb-428 session — 2026-04-22)
   # Threshold must come from aspirations.yaml experience_archival_gate.staleness_hours,
   # NOT an env-var default. Env-var override + YAML default = two sources of truth.
   Check: experience-staleness-check.sh does not default staleness via env var
   Bash: grep -cE 'EXPERIENCE_STALENESS_HOURS:-' core/scripts/experience-staleness-check.sh → verify == 0
   Check: experience-staleness-check.sh reads staleness_hours from aspirations.yaml and fails loud if missing
   Bash: grep -cE "missing experience_archival_gate\.staleness_hours" core/scripts/experience-staleness-check.sh → verify >= 1

   # cargo-cult-detector.py narrow SourceUnavailable exception (rb-442 sister fix — 2026-04-22)
   # Previous `except SystemExit` in cmd_audit_all was over-broad — would swallow any
   # future sys.exit(1) from downstream code. Dedicated SourceUnavailable keeps the
   # catch narrow to the one legitimate fail-open case (MIND_AGENT unbound).
   Check: cargo-cult-detector.py defines SourceUnavailable exception class
   Bash: grep -cE '^class SourceUnavailable' core/scripts/cargo-cult-detector.py → verify == 1
   Check: cmd_audit_all no longer catches bare SystemExit
   Bash: grep -cE '^        except SystemExit:' core/scripts/cargo-cult-detector.py → verify == 0
   Check: cmd_audit_all catches SourceUnavailable specifically
   Bash: grep -cE '^        except SourceUnavailable:' core/scripts/cargo-cult-detector.py → verify == 1
   Check: source_path raises SourceUnavailable not SystemExit
   Bash: grep -cE 'raise SourceUnavailable' core/scripts/cargo-cult-detector.py → verify >= 1

   # cargo-cult-detector.py cmd_audit_all _gate_log instrumentation coverage (g-115-535/g-115-536 — 2026-05-10)
   # cmd_audit_all has 5 _gate_log instrumentation sites covering: self-dedup,
   # no-candidates, dry-run, file-failed, and batch-filed branches. Future refactors
   # must not silently drop any of them — the regression mode is silent (zero
   # firings is indistinguishable from gate-not-invoked, so a quiet removal would
   # not surface in telemetry). This is the sq-018 lens: framework files where
   # regression cost is high AND the failure mode is silent.
   Check: cmd_audit_all has at least 5 _gate_log calls
   Bash: py -3 -c "import re,pathlib; src=pathlib.Path('core/scripts/cargo-cult-detector.py').read_text(encoding='utf-8'); m=re.search(r'^def cmd_audit_all\b.*?(?=^def |\Z)', src, re.DOTALL|re.MULTILINE); body=m.group(0) if m else ''; print(body.count('_gate_log('))" → verify >= 5
   Check: cmd_audit_all _gate_log decision_paths cover all 5 expected branches (self-dedup, no-candidates, dry-run, file-failed, batch-filed)
   Bash: py -3 -c "import re,pathlib; src=pathlib.Path('core/scripts/cargo-cult-detector.py').read_text(encoding='utf-8'); m=re.search(r'^def cmd_audit_all\b.*?(?=^def |\Z)', src, re.DOTALL|re.MULTILINE); body=m.group(0) if m else ''; paths=sorted(set(re.findall(r'audit-all-[a-z-]+', body))); print(','.join(paths))" → verify == "audit-all-batch-filed,audit-all-dry-run,audit-all-file-failed,audit-all-no-candidates,audit-all-self-dedup"

   # cargo-cult-detector.py cmd_check _gate_log instrumentation coverage (g-115-570 — 2026-05-10)
   # Origin: g-115-548 audit Gap 1 + g-115-570 closure. cmd_check (per-goal cargo-cult path
   # invoked via main() after the args.audit_all branch) has 5 _gate_log instrumentation
   # sites covering: artifact-producing-skip, auto-extend-dry-run, auto-extend-success,
   # dedup-hit, idea-filed. Without these, gate-firings.jsonl can't answer "how often does
   # cargo-cult auto-extend?" or "how often does dedup catch a redundant Idea?". The check
   # uses main()'s post-audit-all section as the cmd_check body anchor (cmd_check is inline
   # within main() — see core/scripts/cargo-cult-detector.py L924 onward).
   Check: cmd_check section has at least 5 _gate_log calls
   Bash: py -3 -c "import re,pathlib; src=pathlib.Path('core/scripts/cargo-cult-detector.py').read_text(encoding='utf-8'); m=re.search(r'^def main\b.*?\Z', src, re.DOTALL|re.MULTILINE); body=m.group(0) if m else ''; ctx=body.split('if args.audit_all:')[-1]; print(len(re.findall(r'_gate_log\(', ctx)))" → verify >= 5
   Check: cmd_check decision_paths cover all 5 expected branches (artifact-producing-skip, auto-extend-dry-run, auto-extend-success, dedup-hit, idea-filed)
   Bash: py -3 -c "import re,pathlib; src=pathlib.Path('core/scripts/cargo-cult-detector.py').read_text(encoding='utf-8'); m=re.search(r'^def main\b.*?\Z', src, re.DOTALL|re.MULTILINE); body=m.group(0) if m else ''; ctx=body.split('if args.audit_all:')[-1]; paths=sorted(set(p[0] or p[1] for p in re.findall(r\"'decision_path':\s*'([^']+)'|\\\"decision_path\\\":\s*\\\"([^\\\"]+)\\\"\", ctx))); print(','.join(paths))" → verify == "artifact-producing-skip,auto-extend-dry-run,auto-extend-success,dedup-hit,idea-filed"

   # capability-gate.py _gate_log instrumentation coverage (g-115-571 — 2026-05-10)
   # Origin: g-115-548 audit Gap 2. capability-gate is the canonical Layer-B gate of the
   # 4-layer enforcement pattern (see world/knowledge/tree/system/system-constraints-loop/
   # capability-routing-enforcement/enforcement-pattern-layers.md). The malformed-input
   # fail-open branch (gate_id="capability-gate", decision="fail_open") MUST remain
   # instrumented or the rb-403 family of bugs returns invisibly — gate looks "never
   # fired" when fed bad input even though the code did pass through it.
   # POST-DAEMON-MIGRATION: gate logic moved from core/scripts/capability-gate.py
   # (thin wrapper) to core/scripts/gates/capability.py (PR 7a/5, 2026-05-14).
   # The _gate_log calls live in gates/capability.py now; the CLI wrapper just
   # delegates via `evaluate()`. Check the canonical location.
   Check: gates/capability.py has both entry-point and main-decision _gate_log calls AND fail_open decision is present
   Bash: py -3 -c "import re,pathlib; src=pathlib.Path('core/scripts/gates/capability.py').read_text(encoding='utf-8'); count=src.count('_gate_log('); decs=set(re.findall(r'_gate_log\([^,]+,\s*[\"\']([a-z_]+)[\"\']', src)); ok=count>=2 and 'fail_open' in decs; print(f'PASS count={count} decisions={sorted(decs)}' if ok else f'FAIL count={count} decisions={sorted(decs)}')" → verify starts with "PASS"

   # blocker-create-gate.py _gate_log existence (g-115-571 — 2026-05-10)
   # Origin: g-115-548 audit Gap 3. blocker-create-gate is the chokepoint for ALL
   # CREATE_BLOCKER protocol invocations (see .claude/rules/probe-with-canonical-code-path.md
   # Enforcement section). If a future refactor extracts gate logic into sub-functions and
   # forgets to thread _gate_log through, the gate becomes silent — ALL blocker creation
   # telemetry goes dark — but the gate itself keeps blocking. Failure is doubly silent
   # (no telemetry, no test fail). Minimum-viable existence check guards against that.
   # POST-DAEMON-MIGRATION: logic moved to gates/blocker_create.py (PR 7a/3).
   Check: gates/blocker_create.py has at least 1 _gate_log call with gate_id "blocker-create-gate"
   Bash: py -3 -c "import pathlib; src=pathlib.Path('core/scripts/gates/blocker_create.py').read_text(encoding='utf-8'); print('PASS' if src.count('_gate_log(') >= 1 and 'blocker-create-gate' in src else 'FAIL')" → verify == "PASS"

   # origin-signal-gate.py _gate_log decision-path coverage (g-115-571 — 2026-05-10)
   # Origin: g-115-548 audit Gap 4. origin-signal-gate emits 5 distinct decisions:
   # auto_derive (Layer-D auto-routing, NEWEST), block, noop (empty signal), override
   # (escape hatch), pass. The auto_derive site implements Layer-D auto-conversion
   # (see .claude/rules/probe-before-defer.md "Auto-conversion at Defer Time"); recent
   # changes carry the highest regression risk because authors haven't internalized the
   # gate's full instrumentation surface. A coverage check asserts all 5 decisions remain.
   # POST-DAEMON-MIGRATION: logic moved to gates/origin_signal.py.
   Check: gates/origin_signal.py emits all 5 expected _gate_log decisions (auto_derive, block, noop, override, pass)
   Bash: py -3 -c "import re,pathlib; src=pathlib.Path('core/scripts/gates/origin_signal.py').read_text(encoding='utf-8'); decs=sorted(set(re.findall(r'_gate_log\([^,]+,\s*[\"\']([a-z_]+)[\"\']', src))); expected=['auto_derive','block','noop','override','pass']; print('PASS' if decs==expected else f'FAIL got={decs} expected={expected}')" → verify == "PASS"

   # capability-gate-layer-d disambiguation in aspirations.py cmd_update_goal (g-115-572 — 2026-05-10)
   # Origin: g-115-548 audit Gap 5 + g-115-564 instrumentation closure. cmd_update_goal
   # in aspirations.py implements Layer-D auto-Unblock filing at defer time
   # (see .claude/rules/probe-before-defer.md "Auto-conversion at Defer Time (Layer D)"
   # and capability-routing-enforcement/enforcement-pattern-layers.md). The gate_id
   # "capability-gate-layer-d" MUST remain distinct from "capability-gate" (Layer-B)
   # so 4-layer telemetry stays disambiguated in meta/gate-firings.jsonl. A future
   # refactor that collapses both gates back to the shared name would silently destroy
   # the layer-distinction in audit queries. Existence check guards against that.
   Check: cmd_update_goal in aspirations.py has _gate_log call with gate_id "capability-gate-layer-d"
   Bash: py -3 -c "import re,pathlib; src=pathlib.Path('core/scripts/aspirations.py').read_text(encoding='utf-8'); m=re.search(r'def cmd_update_goal\b.*?(?=^def |\Z)', src, re.DOTALL|re.MULTILINE); body=m.group(0) if m else ''; print('PASS' if '_gate_log(' in body and 'capability-gate-layer-d' in body else 'FAIL: Layer-D telemetry regressed (gate_id collapsed back to capability-gate or _gate_log removed)')" → verify == "PASS"

   # capability-route-gate.py wiring in cmd_add_goal (g-282-04 / g-115-601 — 2026-05-10)
   # Origin: g-282-04 wired capability-route-gate.py into aspirations.py cmd_add_goal between
   # origin-signal-gate and goal-duplication-gate so newly-added goals receive an
   # intended_agent stamp (Layer-A of the 4-layer capability-routing enforcement pattern).
   # Without this check, a future refactor could silently remove the subprocess call AND/OR
   # the goal["intended_agent"] = ia stamp, leaving the script reference intact in comments
   # while the actual classifier never runs — goals would stop getting routed and the
   # downstream Layer-B/C/D telemetry would lose its upstream signal. Function-level
   # assertion (NOT just file-level grep) catches the regression where the script path
   # survives in comments or other functions but the cmd_add_goal call site is excised.
   Check: cmd_add_goal in aspirations.py invokes capability-route-gate.py AND stamps goal["intended_agent"]
   Bash: py -3 -c "import re,pathlib; src=pathlib.Path('core/scripts/aspirations.py').read_text(encoding='utf-8'); m=re.search(r'def cmd_add_goal\b.*?(?=^def |\Z)', src, re.DOTALL|re.MULTILINE); body=m.group(0) if m else ''; has_script='capability-route-gate.py' in body; has_stamp=bool(re.search(r'goal\[.intended_agent.\]\s*=\s*ia', body)); print('PASS' if has_script and has_stamp else f'FAIL: capability-route-gate wiring regressed in cmd_add_goal (script={has_script} stamp={has_stamp})')" → verify == "PASS"

   # recurring-precondition-sweep bash backstop (rb-428 class — 2026-04-22)
   # Previously invoked ONLY via aspirations-precheck/SKILL.md:544 (LLM-dispatched).
   # iteration-close.sh now also runs it each productivity-check — dual-path means
   # a single-point-of-failure drop cannot silently re-inflate overdue_ratio.
   # The cygpath wrapper is load-bearing on Git Bash (py -3 cannot open /c/... paths).
   Check: iteration-close.sh do_productivity_check invokes recurring-precondition-sweep.py
   Bash: grep -cE 'recurring-precondition-sweep\.py' core/scripts/iteration-close.sh → verify >= 1
   Check: the invocation uses cygpath -w (Windows-form path required for py -3 file arg)
   Bash: grep -cE 'cygpath -w.*recurring-precondition-sweep' core/scripts/iteration-close.sh → verify >= 1
   Check: the SKILL.md-dispatched call still exists (dual-path by design)
   Bash: grep -cE 'recurring-precondition-sweep\.py' .claude/skills/aspirations-precheck/SKILL.md → verify >= 1

   # heartbeat-stale.sh load-bearing _platform.sh source (rb-442 — 2026-04-22)
   # The inline `python3 -c` at line ~44 reads aspirations.yaml via $CONFIG_DIR and
   # REQUIRES _platform.sh's Windows-path conversion (cygpath) to succeed on Git Bash.
   # A reviewer flagged this as dead weight; smoke test proved otherwise.
   Check: heartbeat-stale.sh still sources _platform.sh
   Bash: grep -cE 'source "\$CORE_ROOT/scripts/_platform\.sh"' core/scripts/heartbeat-stale.sh → verify >= 1
   Check: heartbeat-stale.sh documents the load-bearing dependency
   Bash: grep -cE 'DO NOT drop _platform\.sh' core/scripts/heartbeat-stale.sh → verify >= 1
   Check: `core/scripts/recovery-gate.sh` skips the gate on continuation SessionStart events (source=compact or source=resume — rb-432 residual-closure)
   Bash: grep -cE 'source=\$SOURCE -- continuation, skipping gate' core/scripts/recovery-gate.sh → verify >= 1
   Bash: grep -cE 'compact\|resume\)' core/scripts/recovery-gate.sh → verify >= 1 (the case branch that early-exits)
   Bash: echo '{"source":"compact"}' | bash core/scripts/recovery-gate.sh 2>&1 | grep -cE 'continuation, skipping gate' → verify == 1 (end-to-end: compact payload produces skip log)
   Check: `core/config/conventions/compact-recovery.md` documents the source-matching design
   Bash: grep -cE '^## Recovery-gate source matching' core/config/conventions/compact-recovery.md → verify >= 1
   Check: `core/scripts/recovery-gate.sh` hint comments protect the source-gate design (rb-445 simplicity + rb-432 intent-level closure)
   Bash: grep -cE 'INTENT pre-filter, NOT a 5th liveness' core/scripts/recovery-gate.sh → verify >= 1 (future editors must not add a PID/process check here)
   Bash: grep -cE 'STDIN IS CONSUMED HERE' core/scripts/recovery-gate.sh → verify >= 1 (future editors must not add another stdin read below the source gate)

   # Cross-Agent Visibility, Pre-Silence, Self-Recovery (Section CAV, added 2026-04-19)
   # Cross-agent visibility (in_flight schema) + pre-silence guardrail (rule + guard-321) +
   # script-gated SessionStart auto-recovery (recovery-gate.sh + session-manifest-clear.sh).
   # Authority lockdown preserved: LLM still cannot write session-state-set.sh or
   # session-signal-set.sh stop-requested/stop-loop. Recovery-gate is the SECOND
   # authorized caller (RUNNING→IDLE only) under a script-gated 4-condition AND-gate.
   # The plan's PID-liveness layer was removed (rb-357 — $PPID=1 from LLM Bash on
   # Windows means PID is a ghost). DO NOT re-add a runner-alive PID probe without
   # platform verification.
   Check: `core/scripts/recovery-gate.sh` exists, is executable, and sources `_paths.sh`
   Check: `core/scripts/recovery-gate.sh` enforces ALL FOUR conditions before recovery: state=RUNNING, heartbeat-stale.sh returns "stale", stop-requested NOT set, `background-jobs.sh has-pending` exits 1
   Check: `core/scripts/recovery-gate.sh` calls `session-manifest-clear.sh` (NOT inlined manifest parsing) — single source of truth for the cleanup path /start --recover Phase 0.7 also uses
   Check: `core/scripts/recovery-gate.sh` writes `agents/<agent>/session/recovery-notice` and appends `agents/<agent>/session/recovery-log.jsonl` AFTER cleanup runs (audit trail)
   Check: `core/scripts/recovery-gate.sh` has the WHY-NO-PID-LIVENESS-CHECK comment block citing rb-357 (prevents future maintainers from re-adding a layer that mis-reports DEAD)
   Check: `core/scripts/session-manifest-clear.sh` exists and uses `subprocess.run([sys.executable, "core/scripts/session_snapshot.py", "--output", "json"], capture_output=True, text=True, check=True)` — NOT a shell pipe to python3 (rb-359: Windows OSError 22 footgun)
   Check: `core/scripts/session-manifest-clear.sh` has the WHY-subprocess-NOT-shell-pipe comment block (Windows guard)
   Check: `core/scripts/session-manifest-clear.sh` exits 1 only when `MIND_AGENT` is unset (caller error); never deletes anything outside the manifest's `recovery_action: clear` entries with `exists: true`
   Check: `core/config/session-manifest.yaml` has NO `running-pid` entry (the ghost-PID layer was removed; re-adding would re-introduce rb-357)
   Check: `core/config/session-manifest.yaml` lists `recovery-notice` and `recovery-log.jsonl` with `recovery_action: preserve`
   Check: `core/scripts/runner-alive.sh` does NOT exist (the unimplementable PID layer was removed; re-creation would re-introduce rb-357)
   Check: `.claude/rules/stop-hook-compliance.md` contains the recovery-gate exception block listing the 4 AND-conditions (state=RUNNING, heartbeat=stale, stop-requested NOT set, no Tier-A registered job)
   Check: `.claude/rules/stop-hook-compliance.md` exception block states the LLM MUST NOT invoke `recovery-gate.sh` directly — only the SessionStart hook may
   Check: `.claude/rules/user-interaction.md` Script-Level Restrictions lists `core/scripts/recovery-gate.sh` as an authorized RUNNING→IDLE caller of `session-state-set.sh`
   Check: `.claude/skills/start/SKILL.md` Step 0.7 invokes `bash core/scripts/session-manifest-clear.sh` (no inlined Python manifest parser — single source of truth)
   Check: `.claude/skills/start/SKILL.md` does NOT contain `echo "$$" > running-pid` or `echo "$PPID" > running-pid` (rb-357 — these are ghost PIDs from LLM Bash, never useful)
   Check: `.claude/skills/prime/SKILL.md` Phase 2 reads `agents/<agent>/session/recovery-notice` if it exists and surfaces it in the PRIMED output
   Check: `.claude/rules/check-team-state-before-silent.md` exists and names the 6h default threshold
   Check: `.claude/rules/check-team-state-before-silent.md` references the canonical probe `bash core/scripts/team-state-read.sh --field agent_status.<partner>.last_active --json`
   Check: guard-321 is active in world/guardrails.jsonl with trigger_condition mentioning "silent/absent/crashed/unresponsive"
   Check: guard-326 is active in world/guardrails.jsonl with trigger_condition about adding probe layers (rb-357/358/359 lineage)
   Check: rb-357, rb-358, rb-359 are active in world/reasoning-bank.jsonl with category=framework-engineering and applies_to=framework
   Check: `.claude/skills/aspirations-execute/SKILL.md` Phase 4 (or `core/config/aspirations-loop-digest.md` Phase 4) has a claim-conflict gate: read team-state immediately before posting board claim; if `agent_status.<partner>.in_flight.goal_id == this_goal`, abort + re-select
   Check: `.claude/skills/aspirations-execute/SKILL.md` Phase 4 calls `team-state-update.sh in-flight <agent> <goal_id> <title> <phase>` after the claim post
   Check: `core/scripts/iteration-close.sh` (or equivalent close-path) calls `team-state-update.sh` with a clear-in-flight operation on goal completion / release
   Check: `core/scripts/team-state.py` has both `update_in_flight` and `clear_in_flight` methods; team-state.yaml schema includes `agent_status.<agent>.in_flight: {goal_id, title, claimed_at, phase}`
   Check: `.claude/skills/prime/SKILL.md` Phase 2 reads `team-state-read.sh --json` once per session and surfaces partner.in_flight in the PRIMED output
   Check: `.claude/skills/aspirations-precheck/SKILL.md` reads team-state once per iteration and surfaces partner.in_flight
   Check: `.claude/skills/aspirations-select/SKILL.md` excludes goals matching `agent_status.<partner>.in_flight.goal_id` from the candidate set
   Bash: ls core/scripts/runner-alive.sh 2>/dev/null && echo FAIL || echo PASS → verify PASS (file must not exist)
   Bash: grep -E '^[^#]*\|\s*python3?\s+-' core/scripts/session-manifest-clear.sh && echo FAIL || echo PASS → verify PASS (no shell-pipe-to-python3 regression — rb-359)
   Bash: grep -nE 'running-pid' core/config/session-manifest.yaml && echo FAIL || echo PASS → verify PASS (no ghost-PID re-introduction — rb-357)
   Bash: MIND_AGENT="" bash core/scripts/session-manifest-clear.sh 2>&1; rc=$?; [ $rc -eq 1 ] && echo PASS || echo FAIL → verify PASS (no-agent path exits 1, never silently clears)
   Bash: grep -c 'recovery-gate.sh' .claude/settings.json → verify >= 1 (SessionStart hook is wired). FAIL means g-243-05 is still pending — user must add the hook entry manually because .claude/settings*.json is in the deny list.

   # Coordination gates — live observation checks (asp-248, added 2026-04-20)
   # The goal-duplication-gate and insight-trigger-gate are bash-enforced
   # coordination chokepoints. These checks are NOT structural (they don't
   # verify files exist) — they audit the RUNTIME output of the gates to
   # detect whether partner coordination actually flows through them.
   # A silent audit log is a signal, not a pass: it means the upstream
   # emission path isn't producing signal the gate can catch.
   Check: `core/scripts/goal-duplication-gate.py` exists and is invoked from `core/scripts/aspirations.py cmd_add` + `cmd_add_goal` (grep -n "goal-duplication-gate.py" core/scripts/aspirations.py → ≥ 2 hits)
   Check: `core/scripts/insight-trigger-gate.py` exists and is invoked from `.claude/skills/fresh-eyes-code/SKILL.md`
   Check: Does goal-duplication-gate catch any real overlaps? Audit `world/goal-duplication-overrides.jsonl` for override patterns — repeated `--override-duplication` on the same signal class (same file-paths, same keyword family) = tune the regex or stopword list in `goal-duplication-gate.py` (_FILE_PATH_RE line ~72, _STOPWORDS line ~78). Empty file = either no false positives OR gate never tripped — read it alongside `aspirations.jsonl` growth rate to disambiguate.
   Check: Does insight-trigger-gate fire? If `agents/<agent>/session/insight-actions.jsonl` stays empty across sessions, partner agents are not emitting properly-tagged findings — the problem moves upstream to the findings-emission side. Confirm by grepping partner's recent `world/board/findings.jsonl` entries for `"insight_trigger"` + `"severity:"` + `"requires_action_by:"` + `"affects:"` tag quadruple. Missing any tag breaks the gate's filter.
   Bash: [ -f world/goal-duplication-overrides.jsonl ] && wc -l world/goal-duplication-overrides.jsonl || echo "0 overrides file"  → any output is informational; the count is a health signal not pass/fail
   Bash: MIND_AGENT=bravo py -3 core/scripts/insight-trigger-gate.py --dry-run --output human 2>&1 | grep -q "scanned=" && echo PASS || echo FAIL → verify PASS (gate runs without crashing)
   Bash: echo '{"title":"verify-learning-probe","description":"nothing real","participants":["agent"]}' | MIND_AGENT=bravo py -3 core/scripts/goal-duplication-gate.py --output human 2>&1 | grep -q "would_block" && echo PASS || echo FAIL → verify PASS (gate runs end-to-end)

   # partner_in_flight check regression (g-248-86, added 2026-05-11)
   # The partner_in_flight check detects cross-agent scope collisions while
   # the partner is mid-execution — closes the gap left by recent_completions
   # (which only fires AFTER the partner finishes). Canonical incident: alpha
   # session-65 had three bit-identical cross-agent collisions (rb-846).
   # Check: `core/scripts/goal-duplication-gate.py` defines `_check_partner_in_flight`
   Bash: grep -q '_check_partner_in_flight' core/scripts/goal-duplication-gate.py && echo PASS || echo FAIL → verify PASS (function defined)
   Bash: grep -q '_check_partner_in_flight(goal, file_paths, keywords, self_agent' core/scripts/goal-duplication-gate.py && echo PASS || echo FAIL → verify PASS (wired into checks list in main())
   Bash: MIND_AGENT=alpha py -3 core/scripts/tests/test_goal_duplication_gate_partner_in_flight.py 2>&1 | grep -q "PASS (6/6 cases)" && echo PASS || echo FAIL → verify PASS (6-case regression — overlap/no-overlap/null/self-only/same-id/multi-partner)

   # Structural-co-signal invariant regression (g-115-838, added 2026-05-16; commits 9db5384 + 6e1563e)
   # _check_recent_completions HARD-blocks only when there is a STRUCTURAL
   # co-signal: a shared file-path, OR a hit keyword carrying a hyphen/
   # underscore/digit (structured identifier — rb-335, goal_selector).
   # Plain-words-only strong overlap demotes to an advisory (never blocks).
   # Regression risk: someone "fixes" recurring false positives by raising
   # WEIGHT_THRESHOLD or returning True from has_specific unconditionally.
   # The inline DO-NOT-relax hint at line ~259 preempts the first; this
   # check protects against the second class landing without smoke evidence.
   Check: `core/scripts/gates/goal_duplication.py` `_check_recent_completions` defines `has_specific = bool(hit_paths) or any(re.search(r"[-_0-9]", k) for k in hit_kws)` — the structural-co-signal predicate. Grep must match.
   Bash: grep -qE 'has_specific\s*=\s*bool\(hit_paths\)\s*or\s*any\(' core/scripts/gates/goal_duplication.py && echo PASS || echo FAIL → verify PASS (structural-co-signal predicate present)
   Bash: MIND_AGENT=alpha py -3 core/scripts/tests/test_goal_duplication_gate_structural_co_signal.py 2>&1 | grep -q "PASS (3/3 cases)" && echo PASS || echo FAIL → verify PASS (3-case regression — G1 plain-words demote / G2 file-path block / G3 structured-identifier block)

   # pending_queue check regression (g-115-783, added 2026-05-22)
   # The pending_queue check (6th corpus) scans world + per-agent
   # aspirations.jsonl for pending/in-progress goals overlapping the
   # proposed goal. Closes the missing-CORPUS gap surfaced by the 4-way
   # l1-skew duplicate cluster (g-115-743/776/778/779; bravo s75
   # 2026-05-15) — the other 5 corpora NEVER read the pending queue.
   # Two match strategies: (1) origin_signal exact match — symptom-keyed
   # identity; (2) structural overlap mirroring recent_completions but
   # STRICTER co-signal (file-path hit OR keyword with [_0-9], NOT
   # hyphen-alone) because the ~377-pending corpus is ~5-10x larger than
   # recent_completions and generic compound vocabulary ("cross-agent",
   # "fresh-eyes") produces too many false positives.
   # Regression risk: someone removes the check, weakens the stricter
   # co-signal back to hyphen-permissive, or skips status filter.
   Check: `core/scripts/gates/goal_duplication.py` defines `_check_pending_queue`
   Bash: grep -q 'def _check_pending_queue' core/scripts/gates/goal_duplication.py && echo PASS || echo FAIL → verify PASS (function defined)
   Bash: grep -q '_check_pending_queue(goal, file_paths, keywords, source_name' core/scripts/gates/goal_duplication.py && echo PASS || echo FAIL → verify PASS (wired into checks list in evaluate())
   Check: stricter co-signal predicate — `re.search(r"[_0-9]", k)` (no hyphen) inside `_check_pending_queue`
   Bash: MIND_AGENT=alpha py -3 -c "import sys; sys.path.insert(0,'core/scripts'); from gates import goal_duplication as gd; import inspect; src=inspect.getsource(gd._check_pending_queue); sys.exit(0 if 're.search(r\"[_0-9]\", k)' in src and 'cross-agent' in src else 1)" && echo PASS || echo FAIL → verify PASS (stricter pending-queue co-signal predicate present, NOT hyphen-permissive)
   Bash: MIND_AGENT=alpha py -3 core/scripts/tests/test_goal_duplication_gate_pending_queue.py 2>&1 | grep -q "PASS (6/6 cases)" && echo PASS || echo FAIL → verify PASS (6-case regression — P1 origin_signal / P2 file-path / P3 demote / P4 unrelated / P5 status-filter / P6 empty)

   # Gate-test pytest-collectability invariant (g-115-1375 + g-115-1376, added 2026-06-09)
   # All 7 goal-duplication gate regression tests must define a top-level
   # `def test_*` so `pytest core/scripts/tests` actually COLLECTS + runs them.
   # Standalone-main()-only tests (the pre-g-115-1375 shape) silently never run
   # in the suite — a coverage gap that hid 3 gate invariants (partner_in_flight,
   # insight_trigger, review_request) until g-115-1376 converted them to
   # pytest-collectable + tmp-world isolated (dropping the rb-1547 live-world
   # backup/restore harness). Regression risk: someone reverts a test to
   # standalone-main() only (dropping the def test_ wrapper) and the invariant
   # goes dark again. This check greps each gate test file for `def test_`.
   Check: every test_goal_duplication_gate_*.py defines a top-level `def test_` (pytest entry point)
   Bash: missing=""; for t in partner_in_flight insight_trigger review_request git_log pending_queue cluster_idf structural_co_signal; do f="core/scripts/tests/test_goal_duplication_gate_${t}.py"; grep -qE '^def test_' "$f" 2>/dev/null || missing="$missing $t"; done; test -z "$missing" && echo "PASS: all 7 gate test files pytest-collectable (def test_ present)" || echo "FAIL: gate test files missing def test_:$missing"

   # READ-intent exemption regression (rb-404, added 2026-04-21)
   # The target_state check inverts semantics for Investigate/Audit/Review/
   # Observe/Research/Analyze titles — identifiers in target files are the
   # precondition, not a duplication signal. Shared classifier in _target_state
   # so filing-time (gate) and execution-time (probe) cannot diverge.
   Check: `core/scripts/_target_state.py` defines `READ_INTENT_VERBS` frozenset and `is_read_intent()` helper
   Bash: grep -qE 'READ_INTENT_VERBS\s*=\s*frozenset' core/scripts/_target_state.py && echo PASS || echo FAIL → verify PASS (frozenset declared)
   Bash: py -3 -c "import sys; sys.path.insert(0,'core/scripts'); from _target_state import READ_INTENT_VERBS; expected={'investigate','audit','review','observe','research','analyze'}; print('PASS' if READ_INTENT_VERBS==expected else 'FAIL:'+str(READ_INTENT_VERBS^expected))" → verify PASS (exactly six verbs, no drift)
   Bash: grep -q 'is_read_intent' core/scripts/goal-duplication-gate.py && grep -q 'is_read_intent' core/scripts/target-state-probe.py && echo PASS || echo FAIL → verify PASS (both consumers import the shared helper)
   Bash: echo '{"title":"Investigate: rb-404 probe","description":"Audit core/scripts/_target_state.py for READ_INTENT_VERBS and is_read_intent"}' | MIND_AGENT=bravo py -3 core/scripts/goal-duplication-gate.py --output json 2>&1 | py -3 -c "import sys,json; d=json.loads(sys.stdin.read()); ts=[c for c in d['checks'] if c['name']=='target_state'][0]; print('PASS' if ts['passed'] and 'READ-intent' in ts['reason'] else 'FAIL')" → verify PASS (Investigate title triggers the exemption)

   # stdin encoding (Section ENC, added 2026-04-19)
   # Python on Windows defaults stdin to cp1252; non-ASCII JSON (em-dashes,
   # smart quotes) mojibakes before json.loads decodes it. _platform.sh is
   # sourced by every shell wrapper before `exec python3`, so one export there
   # covers all 23 stdin-reading scripts. rb-316 / guard-304 document the
   # original failure. Do NOT remove without replacing.
   Check: `core/scripts/_platform.sh` exports `PYTHONIOENCODING=utf-8` at module level (before the MSYS cygpath block)
   Check: `core/scripts/path-resolution-hook.py` `open(binding_path, ...)` specifies `encoding="utf-8"` (only `open()` in `core/scripts/*.py` that previously didn't)
   Bash: grep -nE 'open\([^)]*,\s*"[rwa]b?"\s*\)[^e]' core/scripts/*.py → verify 0 hits (every `open()` specifies encoding, or uses binary mode)
   Bash: bash -c 'source core/scripts/_paths.sh; source core/scripts/_platform.sh; echo "$PYTHONIOENCODING"' → verify exactly "utf-8"

   # Mojibake regression (Section ENC continued, 2026-04-19 repair pass).
   # 606 string values across 5 JSONL stores were repaired from double-cp1252
   # mojibake on 2026-04-19. Forward writes are safe because _platform.sh now
   # exports PYTHONIOENCODING=utf-8 (covered above). These checks catch a
   # regression: either the encoding export was reverted, or a new writer
   # path bypasses the wrapper bootstrap and reintroduces corruption.
   # The dry-run probe is the canonical detector (strictly more accurate than
   # a byte-pattern regex — see 2026-04-19 lesson where the regex missed
   # right-arrow mojibake that the round-trip decoder caught).
   Check: `core/scripts/mojibake-repair.py` exists and is executable; `core/scripts/mojibake-repair.sh` wrapper exists
   Bash: source core/scripts/_paths.sh && source core/scripts/_platform.sh && bash core/scripts/mojibake-repair.sh "$WORLD_DIR/reasoning-bank.jsonl" "$WORLD_DIR/guardrails.jsonl" "$WORLD_DIR/aspirations.jsonl" "$AGENT_DIR/experience.jsonl" "$AGENT_DIR/journal.jsonl" 2>&1 | tail -2 | grep -q '^TOTAL: 0/' && echo PASS || echo FAIL → verify PASS. NOTE: must source BOTH `_paths.sh` and `_platform.sh` before expanding `$AGENT_DIR` — _platform.sh runs cygpath to convert MSYS-style paths (`/c/...`) to Windows-style (`C:/...`). Without the cygpath step, Windows Python inside the wrapper sees an unresolvable MSYS path and the file silently SKIPs.

   # Precision encoding evidence checks (Section PE)
   Check: `core/config/conventions/precision-encoding.md` exists with Precision Manifest Schema section
   Check: `aspirations-state-update/SKILL.md` Step 8 has "EXTRACT PRECISION" substep before "WRITE NARRATIVE"
   Check: `reflect-on-outcome/SKILL.md` (Hypothesis mode) Step 2.7 encoding queue includes `precision_manifest` field
   Check: `aspirations-consolidate/SKILL.md` Step 2b has "EXTRACT PRECISION from encoding queue item"
   Check: `aspirations/SKILL.md` Phase -0.5c has "precision-first" in encoding queue processing comment
   Check: `reflect-tree-update/SKILL.md` Step 2 minor insight path has "EXTRACT PRECISION" step
   Check: `reflect-on-outcome/SKILL.md` (Execution mode) refinement path has "Verified Values" section write
   Check: `aspirations-execute/SKILL.md` verbatim_anchors has "MANDATORY: capture ALL precise technical values"
   Bash: grep -c "Verified Values" .claude/skills/*/SKILL.md → verify >= 5 files
   Bash: grep -c "precision_manifest" .claude/skills/*/SKILL.md → verify >= 3 files
   Bash: grep -c "PRECISION AUDIT" .claude/skills/*/SKILL.md → verify >= 3 files
   Bash: grep -r "world/conventions/precision-encoding" .claude/skills/ --exclude-dir=verify-learning 2>/dev/null | wc -l → verify 0 (no stale path references; convention lives in core/config/conventions/)
   Check: CLAUDE.md Convention Index includes `precision-encoding.md`
   IF agent has run 3+ productive goal cycles since precision encoding was deployed:
       Check: at least 1 tree node has a "## Verified Values" section
       Bash: wm-read.sh encoding_queue --json → verify items include precision_manifest field
       Check: experience records have specific verbatim_anchors (not just "key error messages")

   # MR-Search integration evidence checks (Section BJ)
   Check: `core/config/aspirations.yaml` has `episode_chaining` section and `exploration_mode` section
   Check: `core/config/aspirations.yaml` `chain_on_outcomes` contains only `"failed"` (no blocked/surprise)
   Check: `core/config/memory-pipeline.yaml` `slot_types` includes `episode_chain`
   Check: `aspirations-execute/SKILL.md` Phase 4-chain has infrastructure guard before chaining
   Check: `aspirations-execute/SKILL.md` Phase 4.27 is positioned AFTER Phase 4.26 (not before)
   Check: `aspirations-execute/SKILL.md` Phase 4.26 reflection quality write uses read/append/set pattern (no --append flag)
   Check: `core/config/meta.yaml` reflection_quality_log comment says `{reflection_id, downstream_goal, helpful}`
   Check: `reflect/SKILL.md` Step 0.3 has `total >= 3` guard on reflection effectiveness
   Check: `reflect/SKILL.md` Step 5.8 uses `helpful` field (not `led_to_improvement`)
   # Check retired: Step 8.10 + improvement-velocity.yaml feature was removed without forwarding pointer
   Check: `core/config/conventions/experience.md` notes `source_reflection_id` belongs on rb/guardrail records, not experience records
   IF any goal failed and was retried via episode chaining:
       Check: journal mentions "EPISODE CHAIN" with attempt count
       Check: working memory `episode_chain` is null after completion (cleaned up)
   IF any goal ran with `execution_mode: "exploration"`:
       Check: Step 5 evolution triggers were skipped for that goal
       Check: tree encoding still occurred (knowledge retained despite exploration mode)

   # Aspirations compact cache evidence checks (Section BE)
   Check: `core/scripts/aspirations.py` has `COMPACT_GOAL_KEEP` set and `compact_aspiration()` function
   Check: `core/scripts/load-aspirations-compact.sh` exists and follows `load-tree-summary.sh` pattern
   Check: `core/scripts/context-reads.py` has `TRACKED_FILES` list with `aspirations-compact.json`
   Check: `aspirations-select/SKILL.md` Phase 2.9 calls `aspirations-read.sh --id` for full goal detail
   Check: `aspirations-select/SKILL.md` Phase 2.9 comment says "do NOT remove this step"
   Bash: FULL=$(bash core/scripts/aspirations-read.sh --active 2>/dev/null | wc -c) && COMPACT=$(bash core/scripts/aspirations-read.sh --active-compact 2>/dev/null | wc -c) && echo "$FULL $COMPACT" → verify compact < full
   Bash: bash core/scripts/aspirations-read.sh --active-compact 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); g=d[0]['goals'][0]; assert 'id' in g; assert 'description' not in g; assert 'verification' not in g; print('OK')" → verify compact strips heavy fields
   Bash: rm -f agents/<agent>/session/aspirations-compact.json && bash core/scripts/load-aspirations-compact.sh 2>/dev/null → verify returns path
   Check: `load-aspirations-compact.sh` loads BOTH world and agent queues (two aspirations.py calls, then merge)
   Check: 16+ skill files reference `load-aspirations-compact.sh` (grep count)
   Check: `boot/SKILL.md`, `backlog-report/SKILL.md`, `decompose/SKILL.md` KEEP `aspirations-read.sh --active` (need full detail)

   # Source routing evidence checks (Section SR)
   # All world-side aspiration wrappers must accept --source for dual-queue routing.
   # Without this, agent-queue goals selected by goal-selector fail at read/update/complete.
   Bash: bash core/scripts/aspirations-read.sh --source agent --active 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'PASS: {len(d)} agent aspirations')" → verify --source passthrough works on read
   Bash: bash core/scripts/aspirations-read.sh --active-compact 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); assert all('source' in a for a in d), 'missing source field'; print(f'PASS: all {len(d)} have source')" → verify compact output has source field
   Bash: rm -f agents/<agent>/session/aspirations-compact.json && bash core/scripts/load-aspirations-compact.sh 2>/dev/null; cat agents/<agent>/session/aspirations-compact.json | python3 -c "import sys,json; d=json.load(sys.stdin); sources=set(a['source'] for a in d); print(f'PASS: sources={sources}' if 'world' in sources else 'PASS: world-only (no agent aspirations)' if d else 'PASS: empty (fresh setup)')" → verify compact cache merges both queues
   Check: `core/scripts/agent-aspirations-meta-update.sh` exists (completes the agent wrapper set)
   Check: `core/config/conventions/aspirations.md` has "Source Routing Protocol" section with 4 rules
   # Skill-layer routing: every Bash: invocation of aspirations scripts must have --source
   # The negative lookbehind `[^/]` excludes agent-aspirations-*.sh wrappers
   # (they force --source agent internally; explicit flag would be redundant).
   Bash: grep -rn 'Bash:.*[^-]aspirations-\(update-goal\|complete\|add-goal\|retire\|update\|meta-update\)\.sh' .claude/skills/*/SKILL.md | grep -v '\-\-source' | grep -v '\.claude/skills/verify-learning/SKILL.md' | wc -l → verify returns 0 (no unrouted invocations; agent-aspirations-*.sh wrappers excluded since they force --source agent; verify-learning self-references excluded)
   Check: `aspirations-select/SKILL.md` outputs list includes `source` field
   Check: `aspirations-select/SKILL.md` Phase 2.9 uses `--source {goal.source}` on aspirations-read.sh
   Check: `aspirations/SKILL.md` Phase 4 uses `--source {source}` on aspirations-update-goal.sh
   Check: `aspirations-verify/SKILL.md` inputs include `source`
   Check: `aspirations-state-update/SKILL.md` inputs include `source`
   Check: `aspirations-state-update/SKILL.md` persists source to working memory via `wm-set.sh current_goal_source`

   # Recurring goal scoring improvements (Section RGI)
   Check: `goal-selector.py` recurring_urgency uses `math.log2` (not `min(..., 5.0)`)
   Check: `goal-selector.py` recurring_urgency reads `urgency_base` and `urgency_log_scale` from `RECURRING_CONFIG`
   Check: `goal-selector.py` `cmd_select` has recurring debt recovery bonus after scoring
   Check: `goal-selector.py` recurring_saturation reads `saturation_window` and `saturation_max_penalty` from `RECURRING_CONFIG`
   Check: `goal-selector.py` has `load_recurring_config()` function reading from aspirations.yaml
   Check: `core/config/aspirations.yaml` has `recurring:` section with urgency_base, urgency_log_scale, saturation_window, saturation_max_penalty, debt_threshold, debt_bonus, streak_mult
   Check: `core/config/aspirations.yaml` `modifiable:` has bounds for all 7 `recurring.*` params
   Check: `aspirations.py` `cmd_complete_by` computes elapsed BEFORE updating lastAchievedAt
   Check: `aspirations.py` `cmd_complete_by` has streak reset when `elapsed > streak_mult * interval`
   # streak_mult config-knob consolidation (g-115-929, 2026-05-18)
   # The same multiplier governs (a) the streak-break canary filing trigger
   # in aspirations_write.py cmd_complete_by AND (b) the auto-resolve window
   # in streak-break-reflector.py _auto_resolve_recovered_canaries. Pre-929
   # both sites hard-coded 2.0 with only a comment-warning to keep them in
   # sync; lifting to recurring.streak_mult makes drift structurally
   # impossible.
   Check: `mind_api/src/endpoints/aspirations_write.py` defines `_load_streak_mult_config` and `cmd_complete_by` reads `streak_mult` from it (no `streak_mult = 2.0` literal). Bash: `grep -q "^def _load_streak_mult_config" mind_api/src/endpoints/aspirations_write.py && ! grep -qE "^[[:space:]]+streak_mult = 2\.0$" mind_api/src/endpoints/aspirations_write.py` — both halves must succeed.
   Check: `core/scripts/streak-break-reflector.py` defines `_load_streak_mult` and uses it in `_auto_resolve_recovered_canaries` (no `STREAK_MULT = 2.0` module constant). Bash: `grep -q "^def _load_streak_mult" core/scripts/streak-break-reflector.py && ! grep -qE "^STREAK_MULT = 2\.0$" core/scripts/streak-break-reflector.py` — both halves must succeed.
   Bash (streak-mult-config-shared-knob): py -c "import sys, yaml; cfg=yaml.safe_load(open('core/config/aspirations.yaml')); v=cfg.get('recurring',{}).get('streak_mult'); sys.exit(0 if v==2.0 else 1)" && echo "PASS: recurring.streak_mult config knob present and equals 2.0" || echo "FAIL: streak_mult missing or wrong value"
   # session_gap_threshold_hours config-knob (g-115-1031, 2026-05-20)
   # Governs auto-resolve threshold for streak-break canaries when an
   # execution-diary gap >= the threshold separates two close events.
   # Sole consumer: streak-break-reflector.py _load_session_gap_threshold
   # + _has_session_gap. Pre-1031 the gap was hard-coded; lifting to
   # recurring.session_gap_threshold_hours makes the cadence tunable
   # without code edits and parallels the streak_mult pattern above.
   Check: `core/scripts/streak-break-reflector.py` defines `_load_session_gap_threshold` and `_has_session_gap` (no hard-coded `SESSION_GAP_THRESHOLD = 2.0` module constant). Bash: `grep -q "^def _load_session_gap_threshold" core/scripts/streak-break-reflector.py && grep -q "^def _has_session_gap" core/scripts/streak-break-reflector.py && ! grep -qE "^SESSION_GAP_THRESHOLD = 2\.0$" core/scripts/streak-break-reflector.py` — all three halves must succeed.
   Bash (session-gap-threshold-config-knob): py -c "import sys, yaml; cfg=yaml.safe_load(open('core/config/aspirations.yaml')); v=cfg.get('recurring',{}).get('session_gap_threshold_hours'); sys.exit(0 if v==2.0 else 1)" && echo "PASS: recurring.session_gap_threshold_hours config knob present and equals 2.0" || echo "FAIL: session_gap_threshold_hours missing or wrong value"
   Bash (session-gap-threshold-bounds): py -c "import sys, yaml; cfg=yaml.safe_load(open('core/config/aspirations.yaml')); b=cfg.get('modifiable',{}).get('recurring.session_gap_threshold_hours') or {}; sys.exit(0 if b.get('min')==0.5 and b.get('max')==999.0 and b.get('default')==2.0 else 1)" && echo "PASS: modifiable.recurring.session_gap_threshold_hours bounds entry present (min=0.5 max=999.0 default=2.0)" || echo "FAIL: bounds entry missing or wrong"
   Check: `aspirations-select/SKILL.md` self-alignment has `recurring_heavy` threshold (not just `all_recurring`)
   Check: `goal-selection-algorithm.md` documents `log2` formula and `recurring_debt_bonus` (not linear cap)

   # First-principles thinking evidence checks (Section BK)
   Check: `.claude/rules/first-principles.md` exists with "When To Apply" scope limiter and 4 numbered rules
   Check: `core/config/spark-questions.yaml` has `sq-c07` with `category: first_principles` in both `seed_candidates` and `initial_state.candidates`
   Check: `core/config/meta.yaml` improvement_instructions has "First-Principles Analysis" section with System 2 guard
   Check: `aspirations-execute/SKILL.md` episode chain mini-reflection says "four questions" and question 4 mentions "ground truth"
   Check: `reflect-on-outcome/SKILL.md` (Hypothesis mode) Step 7 has first-principles escalation gated by "model-error" or "overconfidence"
   IF `meta/improvement-instructions.md` exists:
       Check: file retains "First-Principles Analysis" section (not removed by agent evolution)

   # sq-007 text consistency across three stores (g-115-221, session-81 framework changes regression)
   # Three locations must agree: meta runtime (spark-questions.jsonl), core seed active question,
   # and initial_state.candidates. Drift between the three breaks aspiration_generation spark
   # because the LLM scans by question text, not just sq-id.
   Bash: grep -c 'Does this outcome justify a NEW ASPIRATION' core/config/spark-questions.yaml → expect 2 (active questions block AND initial_state.candidates block)
   Bash: source core/scripts/_paths.sh && grep -E '"id": *"sq-007"' "$META_DIR/spark-questions.jsonl" | grep -c 'Does this outcome justify a NEW ASPIRATION' → expect 1 (meta runtime text matches core seed)

   # SkillNet integration evidence checks (Section BL)
   # Skill Relation Graph
   Bash: skill-relations.sh read --composable boot → verify returns JSON array with prime and aspirations
   Bash: skill-relations.sh read --similar replay → verify returns relation with research-topic (symmetric)
   Check: `core/config/skill-relations.yaml` config section has `co_invocation_log_cap` and `discover_min_co_occurrences`
   Check: `core/scripts/skill-relations.py` reads thresholds from config (not hardcoded)

   # Skill Quality Evaluation
   Bash: skill-evaluate.sh report → verify returns JSON with skills, summary, alerts
   Check: `core/config/meta.yaml` strategy_schemas has `skill_quality` with file `meta/skill-quality-strategy.yaml`
   Check: `core/config/meta.yaml` initial_state has `skill_quality_strategy.dimension_weights` summing to 1.0
   Check: `aspirations-state-update/SKILL.md` has Step 8.76 (Skill Quality Assessment) calling skill-evaluate.sh
   Check: `core/scripts/skill-evaluate.py` evaluation entries use key `"overall"` (not "quality")
   Check: `core/scripts/skill-analytics.py` reads scores using key `"overall"` (matches evaluate writer)

   # Dynamic Skill Routing
   Bash: goal-selector.sh select 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print('OK' if not d or 'skill_affinity' in d[0].get('raw',{}) else 'MISSING')" → verify skill_affinity in output
   Check: `core/config/meta.yaml` initial_state.goal_selection_strategy.weights has `skill_affinity`
   Check: `decompose/SKILL.md` has "Skill Inference Refinement (Relation Graph)" section

   # Experience-to-Skill Mining and Curation
   Check: `core/config/skill-gaps.yaml` has `experience_mining` section and `quality_thresholds` section
   Check: `aspirations-consolidate/SKILL.md` has Step 7.5 (Experience-to-Skill Mining)
   Check: `aspirations-evolve/SKILL.md` has Step 9.5 (Skill Curation) calling skill-evaluate.sh underperforming

   # Co-Invocation Logging
   Check: `aspirations-execute/SKILL.md` has Phase 4.28 calling skill-relations.sh co-invoke
   Check: `forge-skill/SKILL.md` Constraints mentions skill-relations.sh --similar for dedup

   IF agent ran 5+ goals after SkillNet deployment:
       Bash: skill-evaluate.sh report → verify summary.total_skills_evaluated > 0
       Check: meta/skill-quality.yaml has entries under skills with evaluations[] and aggregate
       Check: world/skill-relations.yaml co_invocation_log has entries (Phase 4.28 fired)

   # AutoContext-inspired subsystem evidence checks (Section BM)

   # Backpressure gate
   Check: `core/config/meta.yaml` strategy_schemas has `backpressure` with `regression_window`, `graduation_window`, `baseline_tolerance`, `max_active_monitors`
   Check: `core/config/meta.yaml` modifiable has `backpressure_regression_window`, `backpressure_graduation_window`, `backpressure_baseline_tolerance`
   Check: `core/config/meta.yaml` initial_state has `backpressure` with `version: 1`, `active_monitors: []`, `rollback_history: []`
   Bash: meta-backpressure.sh status → verify returns JSON with `active_monitors`, `rollback_history`, `active_count`, `total_rollbacks`
   Check: `meta-yaml.py` `cmd_set` has `is_rollback` guard checking for `"BACKPRESSURE ROLLBACK"` in reason
   Check: `meta-yaml.py` `_create_backpressure_monitor` does NOT have `import subprocess` (dead code was removed)
   Check: `aspirations-state-update/SKILL.md` `state-update-audit.sh run-all` covers backpressure (8.85) sub-command
   Check: `aspirations-evolve/SKILL.md` Step 0.7 has `meta-backpressure.sh cooldown-check`

   # Dead end registry
   Check: `meta-init.py` FILE_MAP does NOT include `dead-ends` (JSONL created by init-meta.sh, not meta-init.py)
   Check: `init-meta.sh` has `touch "$META/dead-ends.jsonl"`
   Bash: meta-dead-ends.sh read → verify returns JSON array (empty or with entries)
   Check: `meta-dead-ends.py` `cmd_check` status filter is `not in ("active", "reviewed")` (both block)
   Check: `meta-dead-ends.py` `cmd_add` dedup merge checks `in ("active", "reviewed")` (merges with reviewed too)
   Check: `aspirations-evolve/SKILL.md` Step 0.7 has `meta-dead-ends.sh read --active` before proposing changes
   Check: `aspirations-evolve/SKILL.md` Step 0.7 has `meta-dead-ends.sh increment` when dead end matched

   # Credit assignment
   Check: `core/config/meta.yaml` initial_state has `credit_assignment` with `version: 1`, `assignments: []`
   Check: `meta-impk.py` has `validate_velocity_structure()` function
   Check: `meta-impk.py` `cmd_snapshot` calls `validate_velocity_structure` after read AND before write
   Check: `meta-impk.py` `cmd_snapshot` does NOT catch `ValueError` from validation (crash on corrupt data, never silently repair)
   #   On 2026-04-05, auto-repair code would have silently wiped 347 entries to []. Crash-and-investigate is the only safe path.
   Check: `meta-impk.py` snapshot subcommand has `--active-changes` argument
   Check: `meta-yaml.py` `append_log` returns `mc_id` (meta change ID)
   Check: `meta-yaml.py` `next_meta_change_id` generates `mc-NNN` format from meta-log.jsonl
   Check: `aspirations-state-update/SKILL.md` Step 8.8 has `--active-changes` in meta-impk.sh call
   Check: `core/config/conventions/meta-strategies.md` documents credit assignment schema

   # Strategy generations
   Check: `core/config/meta.yaml` initial_state has `strategy_generations` with `version: 1`, `current_generation: 0`
   Bash: meta-generations.sh status → verify returns JSON with `current_generation`, `peak_generation`, `peak_score`
   Check: `meta-generations.py` `STRATEGY_FILES` and `meta-yaml.py` `_trigger_generation_transition` strategy_files list are in sync (same files)
   Check: Both lists include `skill-quality-strategy.yaml` (added post-SkillNet)
   Check: `meta-generations.py` `cmd_update` auto-opens generation 1 when none exists (no error JSON)
   Check: `aspirations-state-update/SKILL.md` Step 8.85 calls `meta-generations.sh update`

   # Curator quality gate
   Check: `core/config/memory-pipeline.yaml` has `curator_gate` section with `pass_threshold: 0.45`
   Check: `core/config/memory-pipeline.yaml` modifiable has `curator_gate_pass_threshold`, `curator_gate_coverage_weight`, `curator_gate_specificity_weight`, `curator_gate_actionability_weight`
   Check: `aspirations-state-update/SKILL.md` Step 8 has Step 8c.5 "CURATOR QUALITY GATE" between WRITE NARRATIVE and PRECISION AUDIT
   Check: Step 8c.5 has three structured questions (Q1 Coverage, Q2 Specificity, Q3 Actionability)
   Check: Step 8c.5 fail path writes to `wm-set.sh curator_overflow` (not direct tree write)

   # Weakness analysis
   Check: `reflect/SKILL.md` has Step 5.55 "Weakness Analysis" in --full-cycle section
   Check: Step 5.55 scans 4 signal sources: pattern_signatures, guardrails, experience, backpressure
   Check: Step 5.55 creates investigation goals for HIGH-severity active weaknesses
   Check: `aspirations-evolve/SKILL.md` Step 0.7 reads `agents/<agent>/weakness-report.yaml`

   # Cross-subsystem integration
   Check: `core/config/conventions/meta-strategies.md` has sections: Backpressure Gate, Dead End Registry, Credit Assignment, Strategy Generations, Weakness Report, Curator Quality Gate
   Check: `core/scripts/_platform.sh` has `META_DIR="$(cygpath -m "$META_DIR")"` (Windows path fix)

   IF agent ran 5+ goals after AutoContext deployment:
       Bash: meta-backpressure.sh status → verify active_count or total_rollbacks reflect real monitoring
       Bash: meta-generations.sh status → verify current_generation >= 1 and current_goals > 0
       Bash: meta-impk.sh compute --window 5 --metric learning_value → verify entries include active_meta_changes field
       Check: at least some imp@k entries in improvement-velocity.yaml have `active_meta_changes` field
   IF any meta-strategy change was made via meta-set.sh during the test:
       Check: meta-log.jsonl entries have `meta_change_id` field (mc-NNN format)
       Check: backpressure.yaml has or had a monitor for that change
       Check: strategy-generations.yaml shows generation transition (current_generation > 1)
   IF any backpressure rollback occurred during the test:
       Check: rollback_history entry has `failed_value` and `total_goals_at_rollback` fields
       Check: journal mentions "BACKPRESSURE ROLLBACK"
       Check: if same field rolled back 2+ times, dead-ends.jsonl has an entry for it

   # External path configuration evidence checks (Section EP)
   # CRITICAL ordering: AGENT_NAME must be assigned BEFORE local-paths.conf sourcing.
   # Without this, $AGENT_NAME is empty and local-paths.conf is never sourced in bash.
   Bash: head -30 core/scripts/_paths.sh | grep -n "AGENT_NAME\|local-paths" → verify AGENT_NAME assignment appears BEFORE the source line
   Check: `core/scripts/_paths.sh` sources `agents/<agent>/local-paths.conf` (not project-root)
   Check: `core/scripts/_paths.sh` WORLD_DIR and META_DIR always have a value (PROJECT_ROOT fallback)
   Check: `core/scripts/_paths.sh` WORLD_DIR uses priority: MIND_WORLD > WORLD_PATH > PROJECT_ROOT/world
   Check: `core/scripts/_paths.sh` META_DIR uses priority: MIND_META > META_PATH > PROJECT_ROOT/meta
   Check: `core/scripts/_paths.py` has `_read_local_paths()` reading from agent directory
   Check: `core/scripts/_paths.py` WORLD_DIR is always a valid Path (falls back to PROJECT_ROOT/world)
   Check: `core/scripts/_paths.py` META_DIR is always a valid Path (falls back to PROJECT_ROOT/meta)

   # Path-Truthiness Dead-Code Regression (Section PTDC — guard-551, g-115-731)
   # _paths.py guarantees META_DIR / WORLD_DIR / AGENT_DIR / PICK_LOG / TREE_PATH
   # are non-None Path objects (env→config→PROJECT_ROOT/<name> fallback chain).
   # Bare `if not <one of those names>:` defensive checks are dead code that
   # misleads readers into thinking the name can be None. Use `<path>.exists()`,
   # `.is_dir()`, or `.is_file()` for disk-state gating; check env presence via
   # `os.environ.get('MIND_AGENT')` for the explicit MIND_AGENT-unset case.
   # Excludes test files (which may construct fake None-typed paths for sandbox).
   # Baseline 2026-05-16 delta iter-11: 11 known instances pending cleanup —
   # aspirations.py:2031, capture-insights.py:47, consolidation-precheck.py:77,
   # evolution-git-sweep.py:583, goal-selector.py:662, loop-state-bump-counters.py:115,
   # recurring-loop-state-mutate.py:175, stale-sentinel-canary.py:194, status.py:202,
   # tree-encoding-drift-gate.py:62, tree.py:176. Apply g-115-845 queued to drop them.
   # Until cleanup ships, this check correctly FAILs (11 hits) — failure IS the
   # cleanup-pending signal, not a check-authoring bug.
   Bash: grep -rnE "if not (META_DIR|WORLD_DIR|AGENT_DIR|PICK_LOG|TREE_PATH)[ ]*:" core/scripts/ --include="*.py" --exclude-dir=tests --exclude="test_*" 2>/dev/null | wc -l → verify 0 results (guard-551 dead Path-truthiness pattern; 11 baseline hits 2026-05-16 — Apply g-115-845 pending; the check is correct, FAIL surfaces unmerged cleanup)

   # Section IIM — Identity-Match settings.json hook protector (rb-997, g-115-835, commit 7c375b6)
   # core/scripts/settings-structural-validator.py is fail-CLOSED and protects
   # the project `.claude/settings.json` (Category B self-edit, per rb-931).
   # If `_is_settings_file()` ever weakens to `endswith(".claude/settings.json")`
   # again (the g-115-792 bug), it will over-match `~/.claude/settings.json`
   # (machine-global) and any `*/.claude/settings.json`, silently denying
   # legitimate machine-global edits with no error trail. Because the validator
   # is itself anchor-protected (CONSTITUTIONAL ANCHOR per CLAUDE.md), a
   # regression here is hard to repair under autonomy — the same hook that
   # would catch the regressed edit ALSO denies the fix attempt. This section
   # pins the post-7c375b6 identity contract: `_is_settings_file` returns True
   # ONLY for resolved-path identity against PROJECT_ROOT-anchored SETTINGS_PATH.
   # Filed by g-115-837 (Maintain), originated via /encode-session Lane 5 sq-018.
   Bash: grep -nE "return.*endswith.*settings" core/scripts/settings-structural-validator.py | wc -l → verify 0 (g-115-792 weaker-form regression — endswith over the settings path over-matches machine-global ~/.claude/settings.json and any */.claude/settings.json; deny-fires on legitimate edits)
   Bash: grep -nE "return.*SETTINGS_PATH" core/scripts/settings-structural-validator.py | wc -l → verify ≥1 (identity-match form present: resolved cand path compared to resolved SETTINGS_PATH)
   Bash: grep -cE "^SETTINGS_PATH\s*=\s*PROJECT_ROOT" core/scripts/settings-structural-validator.py → verify exactly 1 (PROJECT_ROOT-anchored module constant — not cwd, not HOME)
   Bash: grep -cE "^def _is_settings_file" core/scripts/settings-structural-validator.py → verify exactly 1 (the protected helper is defined)
   Bash: grep -c "DO NOT weaken to" core/scripts/settings-structural-validator.py → verify ≥1 (docstring lineage warning preserved — future readers see the g-115-792 history before considering an endswith rewrite)

   Check: `.gitignore` contains `*/local-paths.conf` (per-agent, not project-root)
   Check: `core/scripts/_platform.sh` cygpath for WORLD_DIR and META_DIR are conditional (guarded by -n check)
   Check: `core/config/start-uninitialized-ceremony.md` (first-boot digest, extracted g-115-1723-b) Phase A binds agent name, Phase B configures paths, program.md prompt (Phase C mode dispatch lives in start-phase-c.md)
   Check: `core/config/start-uninitialized-ceremony.md` (first-boot digest) Phase B skipped when `agents/<agent>/local-paths.conf` already exists
   Check: `core/config/start-uninitialized-ceremony.md` (first-boot digest) Phase B step B9-B10 adds Read/Write/Edit permissions to `settings.local.json`
   Check: `core/config/start-phase-c.md` (Phase C digest) Phase C sets state (C8) and invokes /prime (C8.5) BEFORE /create-aspiration (C9) for both assistant and autonomous modes
   Check: `core/scripts/session-save-id.sh` skips auto-resume if agent directory does not exist
   Check: No `factory-reset.sh` exists in core/scripts/
   Check: No `.claude/skills/reset/` directory exists
   Bash: grep -r "factory-reset" core/scripts/ .claude/skills/ core/config/ .env.example CLAUDE.md 2>/dev/null | grep -v "verify-learning/SKILL.md" | wc -l → verify 0 results
   Bash: grep -r "/reset" CLAUDE.md .claude/rules/ 2>/dev/null | grep -v "git reset" | wc -l → verify 0 results
   Bash: grep -rn "domain reset\|wiped on reset" CLAUDE.md core/scripts/ core/config/ .env.example 2>/dev/null | wc -l → verify 0 results
   Bash: grep -c "MIND_DIR" core/scripts/_paths.sh core/scripts/_paths.py core/scripts/_platform.sh 2>/dev/null → verify 0 in all files
   IF agents/<agent>/local-paths.conf exists:
       Check: contains WORLD_PATH= line pointing to a valid directory
       Check: contains META_PATH= line pointing to a valid directory
       Check: uses forward slashes (not backslashes)
   IF .claude/settings.local.json exists:
       Check: permissions.allow contains Read/Write/Edit rules for the configured WORLD_PATH (or Read(*)/Write(*)/Edit(*) wildcards)
       Check: permissions.allow contains Read/Write/Edit rules for the configured META_PATH (or Read(*)/Write(*)/Edit(*) wildcards)

   # Domain convention evidence checks (Section DC)
   # Post-execution convention: Phase 4.2 gates on this file every goal execution.
   # Path resolution: Bash: commands in SKILL files MUST use $WORLD_DIR (not hardcoded world/).
   #   "Read world/..." is LLM pseudocode (LLM resolves world/ to WORLD_DIR). But "Bash: test -f world/..."
   #   is a literal shell command where world/ would resolve relative to project root — which does not exist.
   Bash: source core/scripts/_paths.sh && test -f "$WORLD_DIR/conventions/post-execution.md" && echo "exists" → verify convention file exists at resolved WORLD_DIR
   Bash: bash core/scripts/load-conventions.sh post-execution → verify returns a non-empty path
   Check: `aspirations-execute/SKILL.md` Phase 4.2 `test -f` uses `$WORLD_DIR/conventions/` (not hardcoded `world/conventions/`)
   Check: `execute-protocol-digest.md` Phase 4.2 `test -f` uses `$WORLD_DIR/conventions/` (not hardcoded `world/conventions/`)
   Check: `core/config/start-uninitialized-ceremony.md` (first-boot digest) has Phase C0.5 "Configure domain conventions" between C0 and C1
   Check: `core/config/start-uninitialized-ceremony.md` (first-boot digest) Phase C0.5 only runs when `world/conventions/` has no `.md` files (existing world skips)
   # guard-006 was retired without explicit metadata. The rule it codified (no
   # uncommitted code across session boundaries) is now enforced structurally by
   # iteration-commit.sh in Phase 8 state-update + post-execution.md Step 2's
   # build-gate ceremony. Accept either status so the check stays useful as an
   # existence + ASCII probe without false-failing on the deliberate retirement.
   # Drift detected and reconciled by g-115-998 (2026-05-22).
   Bash: bash core/scripts/guardrails-read.sh --id guard-006 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); assert d['status'] in ('active','retired'); assert all(ord(c)<128 for c in d['rule']); print('OK')" → verify guard-006 exists, has no non-ASCII (status either active or retired — rule structurally enforced post-retirement)
   # Fresh eyes code review step (DC continued)
   Bash: source core/scripts/_paths.sh && grep -c "Step 1.75" "$WORLD_DIR/conventions/post-execution.md" → verify returns >= 1 (fresh eyes step exists)
   Check: `$WORLD_DIR/conventions/post-execution.md` Step 1.75 is between Step 1.5 (testing) and Step 2 (commit)
   Check: `$WORLD_DIR/conventions/post-execution.md` Step 1.5 PASS case says "Proceed to Step 1.75" (not "Step 2")
   Check: `$WORLD_DIR/conventions/post-execution.md` Step 1.5 PARTIAL case says "Proceed to Step 1.75" (not "Step 2")
   #   Without this, PARTIAL test results bypass code review entirely — the original Step 2 reference
   #   was not updated when Step 1.75 was inserted. Both PASS and PARTIAL must flow through fresh eyes.
   IF agent ran goals after post-execution convention was deployed:
       Check: journal mentions "Committed and pushed" for goals that produced code changes
       Check: no pending-questions with status=pending about uncommitted changes
   # Pre-execution convention
   Bash: source core/scripts/_paths.sh && test -f "$WORLD_DIR/conventions/pre-execution.md" && echo "exists" → verify pre-execution convention exists
   Bash: bash core/scripts/load-conventions.sh pre-execution → verify returns a non-empty path
   Check: `aspirations-execute/SKILL.md` has Phase 3.9 "Pre-Execution Domain Steps" before Phase 4
   Check: `execute-protocol-digest.md` has Phase 3.9 before Intelligent Retrieval Protocol

   # Framework default templates for pre/post-execution hook slots. The templates
   # are the source of truth a fresh /start copies into world/conventions/ — if
   # they drift from the structural contract /verify-learning enforces on the
   # lived files, every fresh world fails verify-learning on day one. These
   # checks defend the template, not just the lived world copy.
   Bash: test -f core/config/templates/pre-execution-default.md && echo "PASS: pre-execution template exists" || { echo "FAIL: core/config/templates/pre-execution-default.md missing — /start C0.5 default seeding will break"; false; }
   Bash: test -f core/config/templates/post-execution-default.md && echo "PASS: post-execution template exists" || { echo "FAIL: core/config/templates/post-execution-default.md missing — /start C0.5 default seeding will break"; false; }
   Bash: grep -c "^## Step" core/config/templates/post-execution-default.md | py -3 -c "import sys; n=int(sys.stdin.read()); assert n <= 12, f'FAIL: template has {n} Step headers (>12 — convention-learning cap exceeded)'; print(f'PASS: {n} steps (<=12)')"
   Bash: grep -q "Invoke /fresh-eyes-code" core/config/templates/post-execution-default.md && echo "PASS: template invokes /fresh-eyes-code" || { echo "FAIL: post-execution template missing /fresh-eyes-code invocation — Step 1.75 structural wiring lost"; false; }
   Bash: grep -q -- "--author \$MIND_AGENT" core/config/templates/post-execution-default.md && echo "PASS: template uses --author $MIND_AGENT filter" || { echo "FAIL: post-execution template missing --author \$MIND_AGENT — multi-agent leak guard per guard-493 lost"; false; }
   Bash: py -3 -c "import pathlib; src=pathlib.Path('core/config/templates/post-execution-default.md').read_text(encoding='utf-8'); lines=[ln for ln in src.split('\n') if '\$pre_ts' in ln]; bad=[ln for ln in lines if 'Do not write' not in ln]; assert not bad, f'FAIL template: \$pre_ts in non-warning lines: {bad}'; print(f'PASS: template \$pre_ts only in warning text ({len(lines)} line(s))')"
   Bash: py -3 -c "import pathlib, re; src=pathlib.Path('core/config/templates/post-execution-default.md').read_text(encoding='utf-8'); pos={m.group(1): m.start() for m in re.finditer(r'^## Step (\d+\.?\d*)', src, re.M)}; ok=pos.get('1.5',0) < pos.get('1.75',0) < pos.get('2', 10**9); assert ok, f'FAIL: Step ordering broken in template — positions={pos}'; print('PASS: template Step 1.75 between Step 1.5 and Step 2')"
   # Check: `core/config/start-uninitialized-ceremony.md` (first-boot digest) C0.5 uses per-slot existence detection (NOT whole-directory short-circuit)
   Bash: grep -q "per-slot existence detection" .claude/skills/start/SKILL.md core/config/start-uninitialized-ceremony.md && echo "PASS: C0.5 documents per-slot detection (in start-uninitialized-ceremony.md digest since g-115-1723-b)" || { echo "FAIL: C0.5 reverted to whole-directory short-circuit — fresh worlds with any conventions/*.md will skip seeding"; false; }
   # Check: `core/config/start-uninitialized-ceremony.md` (first-boot digest) C0.5 references the framework templates by path
   # Accepts both literal filenames AND the parameterized `<slot>-default.md` form
   # (C0.5 uses the placeholder throughout its pseudocode; both are valid evidence
   # that the cp-from-template seeding path is wired).
   Bash: grep -qE "core/config/templates/((pre|post)-execution|<slot>)-default\.md" .claude/skills/start/SKILL.md core/config/start-uninitialized-ceremony.md && echo "PASS: C0.5 references framework templates" || { echo "FAIL: C0.5 no longer references core/config/templates/ — template-based seeding regressed to LLM-write-from-prompt"; false; }

   # Signal-refresh hook slot (Tranche C.1) — fail-open if no domain convention present
   Check: `aspirations-precheck/SKILL.md` Phase 0.5.0-pre invokes `load-conventions.sh signal-refresh` (NOT a direct `bash world/scripts/<...>.sh` invocation)
   # Pattern B discipline: core pseudocode names the SLOT (filename), domain conventions name the IMPL (script).
   # The anti-pattern is core directly invoking `bash world/scripts/<domain-script>.sh` — that couples core to a
   # domain-specific script path and breaks on fresh agents. See rb-394 / rb-395 / core/config/conventions/domain-hooks.md.
   Bash: grep -nE 'bash +world/scripts/' .claude/skills/aspirations-precheck/SKILL.md .claude/skills/aspirations-state-update/SKILL.md → verify returns 0 matches (all world/scripts invocations go through hook slots)

   # Outcome-observation hook slot (Tranche C.2) — fail-open if no domain convention present
   Check: `aspirations-state-update/SKILL.md` Step 8.12 invokes `load-conventions.sh outcome-observation` (NOT a direct `bash world/scripts/<...>.sh` invocation)
   Check: `aspirations-state-update/SKILL.md` Step 8.12 is wrapped in `IF outcome_class != "routine":` so routine ticks do not fire the hook

   # Section OMS: Outcome-Metrics Staleness regression guard (g-115-742).
   # The 24h cadence guarantee depends on TWO firing paths: Step 8.12 (non-routine)
   # AND Step 4.5 (routine, mtime-gated). Section OMS asserts both wiring points
   # remain intact AND the file age stays within the 48h ceiling (24h cadence +
   # 24h slack for sleep / no-active-session windows).
   Bash: grep -n "outcome-observation-run.sh" .claude/skills/aspirations-state-update/SKILL.md → verify ≥1 match (Step 4.5 cadence guard present)
   Bash: source core/scripts/_paths.sh && grep -q 'core/scripts/outcome-observation-run\.sh' "$WORLD_DIR/conventions/outcome-observation.md" && echo "OK: convention invokes wrapper (no relative-path regression)" || echo "FAIL: convention reverted to relative-path invocation"
   Bash: source core/scripts/_paths.sh && WORLD_DIR="$WORLD_DIR" py -3 -c "
import os, pathlib, sys, time
p = pathlib.Path(os.environ['WORLD_DIR']) / 'outcome-metrics.yaml'
if not p.is_file():
    print('SKIP: no outcome-metrics.yaml (fresh agent or convention disabled)')
    sys.exit(0)
age_h = (time.time() - p.stat().st_mtime) / 3600
if age_h > 48:
    print(f'FAIL: outcome-metrics.yaml age {age_h:.1f}h exceeds 48h ceiling (24h cadence guarantee regressed — Step 4.5 or Step 8.12 is broken)')
    sys.exit(1)
print(f'OK: outcome-metrics.yaml age {age_h:.1f}h within 48h ceiling')
"

   # Section ITS: Insight-Trigger Sweep regression guard (g-115-754, g-115-759).
   # Brief 2's findings-channel routing fix depends on TWO invariants:
   #   (1) recurring goal g-115-754 still scheduled at 1h cadence + active;
   #   (2) sweep scripts (.py + .sh) intact AND --dry-run + --json contract
   #       still produces a parseable summary (consumed by /prime Step 5.5b).
   # Either invariant breaking silently regresses the cross-agent routing gap
   # that bravo's msg-20260514-143816 surfaced on 2026-05-14.
   Bash: source core/scripts/_paths.sh && WORLD_DIR="$WORLD_DIR" py -3 -c "
import json, os, sys, pathlib
p = pathlib.Path(os.environ['WORLD_DIR']) / 'aspirations.jsonl'
for line in p.read_text(encoding='utf-8').splitlines():
    r = json.loads(line)
    if r.get('id') != 'asp-115': continue
    for g in r.get('goals') or []:
        if g.get('id') == 'g-115-754':
            ok = (g.get('recurring') is True
                  and g.get('interval_hours') == 1.0
                  and g.get('status') in ('pending', 'in-progress'))
            if ok:
                print(f'OK: g-115-754 recurring=True interval=1.0 status={g.get(\"status\")}')
                sys.exit(0)
            print(f'FAIL: g-115-754 shape regressed — recurring={g.get(\"recurring\")} interval_hours={g.get(\"interval_hours\")} status={g.get(\"status\")}')
            sys.exit(1)
    break
print('FAIL: g-115-754 missing from asp-115 — sweep cadence guarantee broken')
sys.exit(1)
"
   Bash: test -f core/scripts/insight-trigger-sweep.py && test -f core/scripts/insight-trigger-sweep.sh && bash core/scripts/insight-trigger-sweep.sh --dry-run --json | py -3 -c "import json,sys; d=json.load(sys.stdin); assert 'pending' in d and 'mode' in d, 'JSON shape regressed'; print(f'OK: sweep .py+.sh + --dry-run --json contract intact (mode={d[\"mode\"]} pending={len(d[\"pending\"])})')" || echo "FAIL: insight-trigger sweep wiring broken (missing scripts OR --dry-run/--json contract regressed)"

   # Scorer criterion ↔ config block naming SSOT (rb-078 / rb-395)
   # Every criterion in goal-selector.py that reads a config block must reference the block by the criterion's own name.
   # Mismatch = scorer silently contributes 0 because the block it looks up doesn't exist.
   Bash: grep -c "^user_signal_boost:" core/config/aspirations.yaml → verify >= 1 (criterion 7d exists)
   Bash: grep -c "^class_balance:" core/config/aspirations.yaml → verify >= 1 (criterion 7e exists)
   # YAML-aware: `grep enabled | grep block` does NOT work here — `enabled:` and the parent block name live on different lines, so no single line contains both. Must parse structurally.
   Bash: py -3 -c "import yaml; c=yaml.safe_load(open('core/config/aspirations.yaml')); assert 'enabled' not in c.get('user_signal_boost',{}), 'user_signal_boost.enabled reintroduced — natural gate is snapshot existence (guard-348)'; assert 'enabled' not in c.get('class_balance',{}), 'class_balance.enabled reintroduced — natural gate is targets non-empty (guard-348)'; print('OK')"

   # Self-drift gate configuration SSOT (rb-394 / rb-395)
   # Natural gate only: activation is governed by class_balance.targets non-empty
   # AND self_drift_gate.target_aspiration_id non-empty. No redundant `enabled` key.
   Bash: py -3 -c "import yaml; c=yaml.safe_load(open('core/config/aspirations.yaml')); g=c.get('self_drift_gate',{}); assert 'enabled' not in g, 'self_drift_gate must not have an enabled key (SSOT rb-395 — natural gates only: class_balance.targets + target_aspiration_id)'; print('OK')"

   # Self-drift gate subprocess contract (rb-394 / guard-352)
   # aspirations-add-goal.sh takes `--source <world|agent>` on the PARENT parser
   # (not the add-goal subparser). Dropping --source silently reroutes writes from
   # the agent queue to the world queue. self-drift-gate.py targets the agent queue
   # per its module docstring, so --source agent MUST appear in the subprocess call.
   Bash: grep -q '"--source", "agent"' core/scripts/self-drift-gate.py && echo "OK" || echo "FAIL: self-drift-gate subprocess call missing --source agent (guard-352 / rb-394 regression)"

   # Python-script invocation in SKILL.md pseudocode (guard-350 / rb-398)
   # `Bash: bash core/scripts/X.py` silently no-ops: bash parses the Python
   # docstring as shell, errors on every line, and `|| true` masks exit code.
   # Every Python script invoked from a SKILL.md must route through a .sh wrapper.
   Bash: py -3 -c "import pathlib, re; pat=re.compile(r'^\s*Bash:\s*bash\s+(core|world)/scripts/[\w.-]+\.py'); bad=[f'{p}:{i+1}' for p in pathlib.Path('.claude/skills').rglob('SKILL.md') for i, line in enumerate(p.read_text(encoding='utf-8').splitlines()) if pat.search(line)]; assert not bad, f'bash X.py pattern in SKILL.md (guard-350): {bad}'; print('OK')"

   # self-drift-gate canonical wrapper must exist (rb-398 companion check)
   Bash: test -f core/scripts/self-drift-gate.sh && echo "OK" || { echo "MISSING: core/scripts/self-drift-gate.sh — gate's canonical wrapper absent"; exit 1; }

   # Convention learning evidence checks (Section DCL)
   # The convention learning system allows the agent to learn new convention steps during
   # reflection, replay, and evolution — promoting recurring guardrails to procedural steps.
   # Config bounds
   Bash: grep -c "convention_learning.max_steps_per_convention" core/config/aspirations.yaml → verify returns >= 1
   Bash: grep -c "convention_learning.auto_apply_confidence" core/config/aspirations.yaml → verify returns >= 1
   Bash: grep -c "convention_learning.cooldown_goals" core/config/aspirations.yaml → verify returns >= 1
   # Skill integration points
   Check: `reflect-on-outcome/SKILL.md` has "Step 2.5b: Convention Routing Check" between Step 2.5 and Step 2.6
   Check: Step 2.5b classifies lessons as universal/procedural and maps to pre/post execution
   Check: Step 2.5b has cost gate checking `max_steps_per_convention` before adding
   Check: Step 2.5b has recurrence check (`>= 2` similar guardrails triggers auto-apply)
   Check: Step 2.5b auto-apply path calls `history-save.sh` before editing convention file
   Check: Step 2.5b auto-apply retires subsumed guardrails via `guardrails-update-field.sh`
   Check: Step 2.5b appends to `$WORLD_DIR/conventions/convention-changes.jsonl` (lifecycle log)
   Check: `aspirations-evolve/SKILL.md` has "Step 3.5: Convention Health Audit" between Step 3 and Step 4
   Check: Step 3.5 loads both convention files and scans for utilization/skipping patterns
   Check: Step 3.5c detects guardrails with `times_active >= 5` as convention promotion candidates
   Check: Step 3.5d reviews pending proposals from `convention-changes.jsonl` for auto-apply maturity
   Check: Step 3.5d checks confidence >= threshold OR reinforcement_count >= 2 before auto-applying
   Check: Step 3.5f logs health snapshot to `evolution-log.jsonl`
   Check: `replay/SKILL.md` has "Step 3.5: Convention Pattern Mining" between Step 3 and Step 4
   Check: Step 3.5 fires only when Step 3 found shared conditions in 2+ corrected hypotheses
   Check: Step 3.5 scans for procedural gap indicators in OUTCOME fields
   Check: Step 3.5 reinforces existing proposals (confidence += 0.15) before creating new ones
   # Convention file structure integrity
   Bash: source core/scripts/_paths.sh && grep -c "^## Step" "$WORLD_DIR/conventions/post-execution.md" → verify step count is <= 12 (max_steps upper bound)
   #   If this exceeds the configured max, convention learning will be unable to add new steps.

   # utilization-stats.py regression-vulnerable invariants (g-115-523/g-115-525 — 2026-05-10)
   # Three regression-vulnerable forms surfaced by 2026-05-09 fresh-eyes review.
   # Without these checks, future LLM edits could silently revert each one — failing
   # mode is silent: audit count drops with no test failure, citation undercounts,
   # curation-log writes break on paths with apostrophes (rb-774 / guard-165 family).
   Check: utilization-stats.py _PATH_PATTERN starts with negative-lookbehind anchor (NOT word-boundary)
   Bash: py -3 -c "import pathlib,re; src=pathlib.Path('core/scripts/utilization-stats.py').read_text(encoding='utf-8'); m=re.search(r'_PATH_PATTERN\s*=\s*re\.compile\(\s*r?[\"\\']([^\"\\']+)', src); pat=m.group(1) if m else ''; print('OK' if pat.startswith('(?<![\\\\w.-])') else f'FAIL pattern={pat[:30]}')" → verify == "OK"
   Check: utilization-stats.py cite_count uses regex.findall (single regex call, NOT two haystack.count() summed)
   Bash: py -3 -c "import re,pathlib; src=pathlib.Path('core/scripts/utilization-stats.py').read_text(encoding='utf-8'); m=re.search(r'^def cmd_rules_audit\b.*?(?=^def |\Z)', src, re.DOTALL|re.MULTILINE); body=m.group(0) if m else ''; uses_findall=bool(re.search(r'cite_pat\.findall\(', body)); double_count=bool(re.search(r'haystack\.count\([^)]+\)\s*\+\s*haystack\.count\(', body)); print('OK' if (uses_findall and not double_count) else f'FAIL findall={uses_findall} double_count={double_count}')" → verify == "OK"
   Check: aspirations-curate-memory/SKILL.md Phase 4 curation-log uses env-var injection (env-var prefix + single-quoted python source per guard-165), NOT bash interpolation
   # Use grep -F (fixed-string) to avoid shell-escape stacking issues with the `$` and quote literals.
   # The literal pattern asserts the env-var injection form is present (canonical guard-165 shape).
   Bash: grep -cF 'WORLD_DIR="$WORLD_DIR" python3 -c' .claude/skills/aspirations-curate-memory/SKILL.md → verify >= 1
   # Negative check: ensure no `python3 -c "$WORLD_DIR` form (bash-interpolation into double-quoted python source).
   Bash: grep -cE 'python3 +-c +"[^"]*\$WORLD_DIR' .claude/skills/aspirations-curate-memory/SKILL.md → verify == 0

   # File history evidence checks (Section FH)
   Check: `core/scripts/_fileops.py` has `save_history()` function
   Check: `core/scripts/_fileops.py` `save_history` resolves BOTH path and base_dir (calls `.resolve()` on both arguments)
   Bash: py -3 -c "import re; src=open('core/scripts/_fileops.py').read(); fn=re.search(r'def save_history\b.*?(?=\ndef |\Z)', src, re.S); assert fn, 'no save_history'; b=fn.group(0); assert 'path = Path(path).resolve()' in b and 'base_dir = Path(base_dir).resolve()' in b, 'missing .resolve() on path or base_dir'; print('PASS')"
   #   Without this, `relative_to` fails on Windows when path formats differ (forward vs back slashes, case)
   Check: `core/scripts/_fileops.py` has `acquire_lock()` and `release_lock()` functions
   Check: `core/scripts/_fileops.py` `acquire_lock` uses `os.O_CREAT | os.O_EXCL` for atomic lock creation (NOT `exists()` + `write_text()`)
   #   Without O_EXCL, there is a TOCTOU race: two processes can both see "no lock" and both create it
   Check: `core/scripts/_fileops.py` has `locked_write_jsonl()`, `locked_append_jsonl()`, `locked_write_json()`, `locked_write_yaml()`
   Check: `core/scripts/_fileops.py` `locked_write_jsonl`, `locked_write_json`, `locked_write_yaml` all have `max_retries` loop around `os.replace`
   #   OneDrive holds transient locks during sync — without retry, os.replace raises PermissionError
   Check: `core/scripts/_fileops.py` `locked_append_jsonl` does NOT have a retry loop (intentional — retrying appends risks duplicate records)
   Check: `core/scripts/_fileops.py` ALL four `locked_write_*` functions have `path.parent.mkdir(parents=True, exist_ok=True)`
   # g-115-403/g-115-404 consolidation invariants — _atomic_write_with_fallback is the
   # single source of truth for the OneDrive/reparse-point/WinError-5 retry pattern.
   # A future refactor that re-introduces hand-rolled os.replace retry loops would undo
   # the consolidation. These checks catch that drift before it ships.
   Check: `core/scripts/_fileops.py` defines `_atomic_write_with_fallback` (single-source retry helper)
   Bash: grep -c "^def _atomic_write_with_fallback" core/scripts/_fileops.py → verify ≥1
   Check: every `os.replace(` call in `_fileops.py` is inside `_atomic_write_with_fallback` (no orphan hand-rolled retry loops)
   Bash: py -3 -c "import pathlib,re; t=pathlib.Path('core/scripts/_fileops.py').read_text(encoding='utf-8'); start=t.find('def _atomic_write_with_fallback'); next_def=re.search(r'^def [^_]', t[start+1:], re.MULTILINE); end=start+1+(next_def.start() if next_def else len(t)-start-1); inside=t[start:end].count('os.replace('); total=t.count('os.replace('); assert inside==total, f'orphan os.replace outside _atomic_write_with_fallback: total={total} inside={inside}'; print(f'PASS: all {total} os.replace inside helper')" → expect PASS
   Check: `_atomic_write_with_fallback` is called by ≥6 sites across `_fileops.py` + `aspirations.py` (1 def + 4 _fileops callers + 1 aspirations caller minimum)
   Bash: total=$(($(grep -c "_atomic_write_with_fallback" core/scripts/_fileops.py) + $(grep -c "_atomic_write_with_fallback" core/scripts/aspirations.py))); [ "$total" -ge 6 ] && echo "PASS ($total)" || echo "FAIL ($total < 6)"
   Check: `_atomic_write_with_fallback` provenance comment still names the OneDrive / reparse-point / WinError-5 failure class it guards against (section comment above the def — loses semantic provenance if removed during refactor)
   Bash: py -3 -c "import pathlib; t=pathlib.Path('core/scripts/_fileops.py').read_text(encoding='utf-8'); end=t.find('def _atomic_write_with_fallback'); start=max(0, end-1500); window=t[start:end+2500]; assert 'OneDrive' in window and ('reparse' in window.lower() or 'WinError' in window), 'helper provenance comment lost OneDrive/reparse/WinError reference'; print('PASS')" → expect PASS
   # g-115-424: Magic Wand 2 (newly-arrived-work in quiescence-gate) + Magic Wand 4
   # (role-affinity in goal-selector) coverage. Encoded via /encode-session 2026-05-08;
   # checks ensure the framework additions remain wired and behave per their tests.
   Check: `core/scripts/tests/test_quiescence_newly_arrived.py` exists (Magic Wand 2 regression suite)
   Bash: test -f core/scripts/tests/test_quiescence_newly_arrived.py && echo OK || echo MISSING
   Check: `core/scripts/tests/test_goal_selector_role_affinity.py` exists (Magic Wand 4 regression suite)
   Bash: test -f core/scripts/tests/test_goal_selector_role_affinity.py && echo OK || echo MISSING
   Check: `core/config/aspirations.yaml` has `newly_arrived_work:` config block (Magic Wand 2 thresholds)
   Bash: grep -c "newly_arrived_work:" core/config/aspirations.yaml → verify ≥1
   Check: `meta/goal-selection-strategy.yaml` has `agent_role_multipliers:` table (Magic Wand 4 per-agent role weights)
   Bash: grep -c "agent_role_multipliers:" "$(bash core/scripts/_paths.sh 2>/dev/null; echo $META_DIR)/goal-selection-strategy.yaml" 2>/dev/null || (source core/scripts/_paths.sh && grep -c "agent_role_multipliers:" "$META_DIR/goal-selection-strategy.yaml") → verify ≥1
   Check: `meta/goal-selection-strategy.yaml` weights section names `role_affinity:` (Magic Wand 4 weight slot)
   Bash: source core/scripts/_paths.sh && grep -c "role_affinity:" "$META_DIR/goal-selection-strategy.yaml" → verify ≥1
   Check: `core/scripts/quiescence-gate.py` defines `_check_newly_arrived_work` helper (Magic Wand 2 detector)
   Bash: grep -c "def _check_newly_arrived_work" core/scripts/quiescence-gate.py → verify ≥1
   Check: `core/scripts/goal-selector.py` defines `compute_role_affinity` helper (Magic Wand 4 scorer)
   Bash: grep -c "def compute_role_affinity" core/scripts/goal-selector.py → verify ≥1
   Check: `core/scripts/quiescence-gate.py` references `__new_work_arrived__` synthetic external_id (Magic Wand 2 sentinel)
   Bash: grep -c "__new_work_arrived__" core/scripts/quiescence-gate.py → verify ≥1
   Check: Magic Wand 2 regression suite passes
   Bash: cd core/scripts/tests && py -3 test_quiescence_newly_arrived.py 2>&1 | tail -1 | grep -q "All .* newly-arrived-work cases verified" && echo OK || echo FAIL
   Check: Magic Wand 4 regression suite passes
   Bash: cd core/scripts/tests && py -3 test_goal_selector_role_affinity.py 2>&1 | tail -1 | grep -q "All .* role-affinity cases verified" && echo OK || echo FAIL
   Check: `core/scripts/_fileops.py` `append_changelog` uses `ensure_ascii=True` (must match locked writes)
   Check: `core/scripts/_fileops.py` `resolve_base_dir()` checks WORLD_DIR and META_DIR
   Check: `core/scripts/_fileops.py` locked writes skip history/changelog when `resolve_base_dir` returns None (agent-only paths)
   Check: `core/scripts/history.py` exists with list, restore, diff, prune subcommands
   Check: `core/scripts/history.py` `resolve_base_dir` imports from `_fileops` (single source of truth, not duplicate)
   Check: `core/scripts/history.py` `cmd_restore` acquires lock before overwriting (acquire_lock/release_lock in try/finally)
   Check: `core/scripts/history.py` `cmd_prune` groups entries > 30 days by ISO week key (date_key.isocalendar()[:2])
   #   Without ISO week grouping, the "one per week" retention policy silently behaves as "one per day"
   Check: `core/scripts/history-save.sh` passes args via sys.argv (not shell string interpolation)
   #   Without sys.argv, single quotes in summary text cause shell injection / SyntaxError
   Check: `core/scripts/history-list.sh`, `history-restore.sh`, `history-diff.sh`, `history-prune.sh`, `history-save.sh` all exist
   Check: `core/scripts/aspirations.py` `write_jsonl` delegates to `locked_write_jsonl` (not inline write)
   Check: `core/scripts/pipeline.py` `write_jsonl` delegates to `locked_write_jsonl`
   Check: `core/scripts/tree.py` `write_tree` uses `acquire_lock`/`release_lock` with `save_history`
   Check: `core/scripts/reasoning-bank.py` `write_jsonl` delegates to `locked_write_jsonl`
   Check: `core/scripts/pattern-signatures.py` `write_jsonl` delegates to `locked_write_jsonl`
   Check: `core/scripts/spark-questions.py` `write_jsonl` delegates to `locked_write_jsonl`
   Check: `core/scripts/meta-yaml.py` `write_yaml` delegates to `locked_write_yaml`
   Check: `core/scripts/meta-dead-ends.py` `write_all` delegates to `locked_write_jsonl`
   Check: `core/scripts/meta-experiment.py` `write_yaml` delegates to `locked_write_yaml`
   Check: `.gitignore` contains `*/.history/` pattern
   Check: `.gitignore` contains `*.lock` pattern
   IF world/.history/ exists:
       Check: at least one snapshot file exists with timestamp_agent.ext naming pattern
       Bash: source core/scripts/_paths.sh && bash core/scripts/history-list.sh "$WORLD_DIR/aspirations.jsonl" → verify lists versions or says "No history"

   # Message board evidence checks (Section MB)
   Check: `core/scripts/board.py` exists with post, read, channels subcommands
   Check: `core/scripts/board-post.sh`, `board-read.sh`, `board-channels.sh` all exist
   Check: `core/scripts/init-world.sh` creates `world/board/` with general, findings, coordination, decisions channels
   Check: `core/config/conventions/board.md` exists with schema and script API
   Check: `prime/SKILL.md` Phase 2 includes step reading board messages (board-read.sh --channel coordination)
   Check: `aspirations/SKILL.md` Phase 4 posts to coordination channel (board-post.sh --channel coordination)
   Check: `aspirations-execute/SKILL.md` has Phase 4.6 posting findings to board
   Check: `forge-skill/SKILL.md` Step 6 posts to `general` channel with `--tags forge,{name},{type}`
   Check: `forge-skill/SKILL.md` Step 8 does NOT send its own notification (comment says "already sent in Step 7")
   Check: `prime/SKILL.md` Phase 2 includes step reading forge announcements (board-read.sh --channel general --tag forge)
   Check: `core/config/conventions/board.md` Agent Integration Points lists forge-skill Step 6
   Check: `core/scripts/init-world.sh` creates `world/forged-skills.yaml`
   Check: `forge-skill/SKILL.md` Step 4 writes to `world/forged-skills.yaml` with forged_by field
   Check: `forge-skill/SKILL.md` Step 7 notifies the user about newly forged skill (via forged notification skill or pending-questions)

   # board.py cmd_post source-tag attribution coverage (g-115-519/g-115-527 — 2026-05-10)
   # cmd_post bumps `times_inferred_helpful` (half-weight in v1 utilization_score)
   # for guard-NNN/rb-NNN cited tags on findings posts. Two regression risks
   # (sq-018 lens — silent measurement gap):
   #   (1) Future edits could swap times_inferred_helpful → times_cited; the latter
   #       carries zero weight in v1 formula, silently re-creating the gap that
   #       guard-343 read 0.04 despite producing 57% of critical findings.
   #   (2) The CRITICAL never-swap warning comment block could be removed in a
   #       comment cleanup pass, leaving (1) undefended.
   # Asserts on the actual subprocess.run argument string (NOT bare token count) —
   # the CRITICAL warning comment intentionally mentions "times_cited" as the bad
   # pattern, so a bare body.count('times_cited') would be 3 (all comment text).
   # The call site passes "utilization.times_inferred_helpful" as a literal —
   # checking the dotted-path form catches the actual code-level invariant.
   Check: cmd_post in board.py call site passes utilization.times_inferred_helpful (>= 1 occurrence as code literal)
   Bash: py -3 -c "import re,pathlib; src=pathlib.Path('core/scripts/board.py').read_text(encoding='utf-8'); m=re.search(r'^def cmd_post\b.*?(?=^def |\Z)', src, re.DOTALL|re.MULTILINE); body=m.group(0) if m else ''; print(body.count('utilization.times_inferred_helpful'))" → verify >= 1
   Check: cmd_post in board.py call site does NOT pass utilization.times_cited (silent-regression sentinel — must be 0)
   Bash: py -3 -c "import re,pathlib; src=pathlib.Path('core/scripts/board.py').read_text(encoding='utf-8'); m=re.search(r'^def cmd_post\b.*?(?=^def |\Z)', src, re.DOTALL|re.MULTILINE); body=m.group(0) if m else ''; print(body.count('utilization.times_cited'))" → verify == 0
   Check: cmd_post in board.py preserves the CRITICAL never-swap warning comment block
   Bash: py -3 -c "import re,pathlib; src=pathlib.Path('core/scripts/board.py').read_text(encoding='utf-8'); m=re.search(r'^def cmd_post\b.*?(?=^def |\Z)', src, re.DOTALL|re.MULTILINE); body=m.group(0) if m else ''; print(body.count('CRITICAL'))" → verify >= 1

   Bash: bash core/scripts/board-channels.sh → verify lists channels (or says no board)
   IF world/board/ exists with .jsonl files:
       Bash: echo "verify-learning test message" | bash core/scripts/board-post.sh --channel general → verify returns message ID
       Bash: bash core/scripts/board-read.sh --channel general --last 1 → verify shows the test message

   # Changelog evidence checks (Section CL)
   Check: `core/scripts/changelog.py` exists with read and stats subcommands
   Check: `core/scripts/changelog-read.sh` and `changelog-stats.sh` exist
   Check: `core/scripts/init-world.sh` creates empty `world/changelog.jsonl`
   Check: `core/scripts/_fileops.py` `append_changelog()` writes to `base_dir/changelog.jsonl`
   Check: `core/config/conventions/history.md` documents changelog schema
   IF world/changelog.jsonl exists and is non-empty:
       Bash: bash core/scripts/changelog-read.sh --last 5 → verify shows recent entries
       Bash: bash core/scripts/changelog-stats.sh → verify shows per-agent and per-file stats

   # CLAUDE.md and convention index evidence checks (Section CI)
   Check: CLAUDE.md Core Systems table has rows for: Message board, File history, Changelog, External paths, File operations
   Check: CLAUDE.md Convention Index has rows for: board.md, history.md, external-paths.md
   Check: CLAUDE.md mentions `agents/<agent>/local-paths.conf` (per-agent, not project-root)
   Check: CLAUDE.md does NOT mention /reset in commands table or enforcement rules
   Check: `README.md` has "Removing Data" section with table (One agent, Shared knowledge, etc.)
   Check: `README.md` does NOT have "Resetting" section or mention factory-reset
   Check: `core/config/conventions/external-paths.md` exists and references `agents/<agent>/local-paths.conf`
   Check: `core/config/conventions/board.md` exists
   Check: `core/config/conventions/history.md` exists

   # Script-level restriction evidence checks (Section SR)
   # Write/Edit deny rules in settings.json
   Check: `.claude/settings.json` deny list includes `Write(*/session/agent-state)` and `Edit(*/session/agent-state)`
   Check: `.claude/settings.json` deny list includes `Write(*/session/persona-active)` and `Edit(*/session/persona-active)`
   Check: `.claude/settings.json` deny list includes `Write(*/session/stop-loop)` and `Edit(*/session/stop-loop)`
   Check: `.claude/settings.json` deny list includes `Write(*.active-agent-*)` and `Edit(*.active-agent-*)`
   # Text rules in user-interaction.md
   Check: `.claude/rules/user-interaction.md` has `## Script-Level Restrictions` section
   Check: user-interaction.md lists `session-state-set.sh` restricted to /start and /stop only
   Check: user-interaction.md lists `init-mind.sh` restricted to /start and /boot (not /start only)
   Check: user-interaction.md allows `session-persona-set.sh true` via /boot but restricts `false` to /stop
   Check: user-interaction.md lists read-only scripts (`session-state-get.sh`, `session-persona-get.sh`, `session-signal-exists.sh`) as fully accessible
   Check: user-interaction.md lists `session-signal-set.sh loop-active`, `session-signal-clear.sh *` as allowed write scripts
   # Agent spawning convention (Section AH)
   Check: `core/scripts/build-agent-context.py` exists
   Check: `core/scripts/build-agent-context.sh` exists
   Check: `core/config/conventions/agent-spawning.md` exists
   Check: `aspirations-execute/SKILL.md` Phase 4 delegation uses `build-agent-context.sh` — NOT "invoke /prime"
   Check: `aspirations-execute/SKILL.md` conventions front matter includes `agent-spawning`
   Bash: grep -c "invoke /prime" .claude/skills/aspirations-execute/SKILL.md 2>/dev/null → verify 0
   Check: `CLAUDE.md` Convention Index has `agent-spawning.md` row
   # Text rules in stop-hook-compliance.md
   Check: `.claude/rules/stop-hook-compliance.md` Rule 2 heading is "Never manually change state" (not old "Never manually set stop-loop")
   Check: stop-hook-compliance.md Rule 2 lists `session-state-set.sh`, `session-signal-set.sh stop-loop` (no counter)
   # Consistency: no stale references to removed factory-reset.sh or legacy mind/ aliases
   Bash: grep -c "factory-reset" .claude/rules/user-interaction.md .claude/rules/stop-hook-compliance.md 2>/dev/null → verify 0 in both files
   Bash: grep -rn "MIND_DIR" core/scripts/ 2>/dev/null | wc -l → verify 0 results

   # AVO-inspired plateau detection, trajectory view, cycle detection (Section AVO)
   Check: `core/config/aspirations.yaml` has `plateau_detection` section with `velocity_window`, `plateau_threshold`, `diminishing_returns_window`
   Check: `core/config/aspirations.yaml` has `cycle_detection` section with `lookback_window`, `checks`
   Check: `core/config/aspirations.yaml` modifiable section has bounds for `plateau_detection.velocity_window`, `plateau_detection.plateau_threshold`, `cycle_detection.lookback_window`
   Check: `core/scripts/aspiration-trajectory.py` exists
   Check: `core/scripts/aspiration-trajectory.sh` exists
   Bash: bash core/scripts/aspiration-trajectory.sh asp-004 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'plateau_detected' in d; assert 'current_velocity' in d; assert 'inflection_points' in d; print('OK')" → verify returns valid JSON with required fields
   Check: `core/scripts/aspiration-trajectory.py` `load_config` has NO fallback defaults (single source of truth is aspirations.yaml)
   Check: `core/scripts/aspiration-trajectory.py` guardrail attribution matches on goal ID only (NOT date substring)
   Check: `core/scripts/aspiration-trajectory.py` `find_aspiration` takes `asp_sources` as required parameter (no default, no file-based fallback)
   Bash: bash core/scripts/aspiration-trajectory.sh asp-004 asp-034 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'asp-004' in d and 'asp-034' in d; assert 'plateau_detected' in d['asp-004']; print('OK')" → verify multi-ID returns keyed object
   Check: `aspirations-evolve/SKILL.md` has Step 1.5 "Plateau Detection" between Step 1 and Step 2
   Check: `aspirations-evolve/SKILL.md` Step 1.5 collects qualifying_asp_ids then calls `aspiration-trajectory.sh` once (batch call, not per-aspiration loop)
   Check: `aspirations-precheck/SKILL.md` Phase 0.5b is Blocker Resolution Check (NOT cycle detection — 15+ external refs depend on this numbering)
   Check: `aspirations-precheck/SKILL.md` Phase 0.5c is Unproductive Cycle Detection
   Check: `aspirations-complete-review/SKILL.md` Phase 7.6 calls `aspiration-trajectory.sh` for maturity decisions
   Check: `reflect-on-self/SKILL.md` (Patterns mode) has Step 3.5 "Trajectory-Level Pattern Extraction"
   # Section SCA: aspiration-trajectory script_convention_attribution_map broadening (g-115-596, g-115-598, 2026-05-10)
   # The build_script_convention_attribution_map function broadens the velocity formula
   # to credit code/convention authorship, fixing the asp-282-shape false-positive
   # zero_learning_velocity. Regression risk: a future refactor could silently remove
   # the function or the load_state["script_convention_attribution"] consumer wiring.
   Bash: grep -q 'build_script_convention_attribution_map' core/scripts/aspiration-trajectory.py && echo "PASS: build_script_convention_attribution_map present in aspiration-trajectory.py" || { echo "FAIL: build_script_convention_attribution_map removed — asp-282-shape zero-velocity false-positive can return"; exit 1; }
   Bash: bash core/scripts/aspiration-trajectory.sh asp-282 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); v=d.get('current_velocity',0); assert v>0, f'velocity={v}'; print(f'PASS: asp-282 velocity={v:.2f}')" || { echo "FAIL: asp-282 velocity=0 — script_convention_attribution wiring broken"; exit 1; }

   # Pre-formation calibration gate evidence checks (Section CG)
   Check: `aspirations-spark/SKILL.md` sq-009 handler has Step 0.5 with `pipeline-read.sh --stage resolved`
   Check: `aspirations-spark/SKILL.md` Step 0.5 has "If total == 0: SKIP gate" (zero-data guard)
   Check: `aspirations-spark/SKILL.md` Step 0.5 confidence ceiling uses explicit boundary operators (>= and <), not ambiguous ranges
   Check: `aspirations-spark/SKILL.md` sq-009 handler has Step 0.7 "Adversarial pre-mortem" with confidence > 0.65 threshold
   Check: `aspirations-spark/SKILL.md` Step 0.5 does NOT read `confidence_calibration_bias` (single source of truth: resolved pipeline records)
   Check: `hypothesis-conventions.md` has "Pre-Formation Calibration Gate" section

   # Session 32 retrospective guardrail evidence checks (Section RG)
   # These guardrails enforce hard-won lessons from session 32 (user-directive, 2026-04-05).
   Bash: bash core/scripts/guardrails-read.sh --id guard-090 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'blocker gate' in d['trigger_condition'].lower(); assert 'email' in d['rule'].lower() or 'notify' in d['rule'].lower(); print('guard-090: OK')" → immediate blocker escalation exists
   Bash: bash core/scripts/guardrails-read.sh --id guard-091 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); assert '0.45' in d['rule']; assert '0.55' in d['rule']; assert 'horizon' in d['rule'].lower(); print('guard-091: OK')" → horizon-specific confidence caps exist with correct thresholds
   Bash: bash core/scripts/guardrails-read.sh --id guard-092 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'CORRECTED' in d['rule']; assert 'pre-mortem' in d['rule'].lower() or 'pre_mortem' in d['rule'].lower(); print('guard-092: OK')" → pre-mortem references past failures
   Bash: bash core/scripts/guardrails-read.sh --id guard-093 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'hypothesis-insight' in d['rule']; assert 'findings' in d['rule'].lower() or 'board' in d['rule'].lower(); print('guard-093: OK')" → hypothesis insight sharing via board
   Bash: bash core/scripts/guardrails-read.sh --id guard-094 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); assert '24h' in d['rule']; assert 'review' in d['rule'].lower(); print('guard-094: OK')" → review request deadline enforcement
   **Runtime**: After forming a session-horizon hypothesis, confidence should be <= 0.45 (guard-091)
   **Runtime**: After resolving a hypothesis, findings board should have a hypothesis-insight post (guard-093)
   **Runtime**: When 3+ goals blocked by GPU, user should receive email within that iteration (guard-090)

   # Mid-session evolution + reflection obligation evidence checks (Section RE)
   Check: `core/config/evolution-triggers.yaml` has `evolution_goal_cadence` trigger with `goals_without_evolution: 15`
   Check: `core/config/evolution-triggers.yaml` `modifiable:` has `evolution_goal_cadence_goals` with bounds {min: 8, max: 30, default: 15}
   Check: `core/config/evolution-triggers.yaml` `initial_state.triggers` has `evolution_goal_cadence` entry
   Check: `aspirations/SKILL.md` Phase -0.5 initializes `productive_goals_this_session = 0` and `last_evolution_goal_count = 0`
   Check: `aspirations/SKILL.md` Phase -0.5 initializes `session_signals` with `routine_streak_global` and `productive_streak`
   Check: `aspirations/SKILL.md` `productive_goals_this_session += 1` is AFTER both per-goal and global anti-drift blocks (not inside signal tracking)
   Check: `aspirations/SKILL.md` global anti-drift threshold is 8 (comment explains relationship to per-goal threshold 5)
   Check: `aspirations/SKILL.md` Phase 9 Part A.1 computes `goals_since_last_evolution` and appends `evolution_goal_cadence` to triggers
   Check: `aspirations/SKILL.md` `session_signals` does NOT contain `corrections_this_session` or `confirmations_this_session` (removed: no population mechanism)
   Check: `aspirations-learning-gate/SKILL.md` Phase 9.8 exists with title "Full-Cycle Reflection Obligation"
   Check: `aspirations-learning-gate/SKILL.md` Phase 9.8 reads threshold from `meta/reflection-strategy.yaml` (not hardcoded)
   Check: `aspirations-learning-gate/SKILL.md` Phase 9.8 has team-aware deferral checking coordination board
   Check: `aspirations-learning-gate/SKILL.md` inputs include `productive_goals_this_session`
   Check: `aspirations-learning-gate/SKILL.md` chaining says "Phase 9.5-9.8" (not "9.5-9.7")
   Check: `aspirations/SKILL.md` chaining table says "Phase 9.5-9.8" for learning-gate
   Check: `meta/reflection-strategy.yaml` has `mode_preferences.full_cycle_cadence_goals`
   **Runtime**: After 15+ productive goals in a session, journal should show "OBLIGATION: full-cycle reflection" entry
   **Runtime**: After 15+ goals without evolution in a session, evolution log should show `evolution_goal_cadence` trigger

   # Post-execution pipeline compliance evidence checks (Section PX)
   # The execute protocol digest must include post-execution obligations (Phases 5-9).
   # Without this, agents complete Phase 4 execution and skip directly to the next iteration,
   # never invoking verify, spark, state-update, or learning-gate as literal Skill() calls.
   Check: `core/config/execute-protocol-digest.md` has "POST-EXECUTION OBLIGATIONS" section after Phase 4.5
   Check: `core/config/execute-protocol-digest.md` lists `Skill(aspirations-verify)` as Phase 5
   Check: `core/config/execute-protocol-digest.md` lists `Skill(aspirations-state-update)` as Phase 8
   Check: `core/config/execute-protocol-digest.md` lists `Skill(aspirations-learning-gate)` as LEARN
   Check: `core/config/execute-protocol-digest.md` lists `Skill(aspirations-spark)` as conditional on productive
   Check: `core/config/execute-protocol-digest.md` enforcement says "NEXT action must be Skill(aspirations-verify)"
   Check: `aspirations/SKILL.md` PER-ITERATION OBLIGATIONS says "literal Skill() tool call" (not "invoke /")
   Check: `aspirations/SKILL.md` Phase 5 line uses `Skill(aspirations-verify)` syntax
   Check: `aspirations/SKILL.md` Phase 6 line uses `Skill(aspirations-spark)` syntax
   Check: `aspirations/SKILL.md` Phase 8 line uses `Skill(aspirations-state-update)` syntax
   Check: `aspirations/SKILL.md` Phase 9.5-9.8 line uses `Skill(aspirations-learning-gate)` syntax
   IF agent ran 3+ goals in an autonomous session:
       **Runtime**: After each productive goal, session output should contain `Skill(aspirations-verify)` tool call
       **Runtime**: After each productive goal, session output should contain `Skill(aspirations-spark)` tool call
       **Runtime**: After each productive goal, session output should contain `Skill(aspirations-state-update)` tool call
       **Runtime**: After each productive goal, session output should contain `Skill(aspirations-learning-gate)` tool call
       **Runtime**: Manual journal writes or WM updates between Phase 4 and Phase 5 indicate the pipeline is being inlined (FAIL)

   # WM goal tracking evidence checks (Section WT)
   # State-update Step 3 must have explicit wm-append/wm-set calls (not prose directives).
   # guard-022 + rb-037 document the root cause: implicit writes don't survive autocompact.
   Bash: grep -c "wm-append.sh goals_completed_this_session" .claude/skills/aspirations-state-update/SKILL.md → verify >= 1 (explicit call exists)
   Bash: grep -c "wm-set.sh aspiration_touched_last" .claude/skills/aspirations-state-update/SKILL.md → verify >= 1 (explicit call exists)
   Check: `aspirations-state-update/SKILL.md` Step 3 wm-append uses dict format `{"goal_id":..., "aspiration_id":..., "recurring":...}` (not bare string)
   Check: `aspirations-state-update/SKILL.md` Step 3 wm-append comment mentions both streak_momentum and recurring_saturation
   Check: `goal-selector.py` streak_momentum comment references "aspirations-state-update Step 3" as data source
   Check: `goal-selector.py` streak_momentum uses `s.get("aspiration_id")` matching the dict format from Step 3
   Check: `goal-selector.py` recurring_saturation uses `s.get("recurring", False)` with backward-compat default
   Check: `goal-selector.py` recurring_saturation window is 4 (last 4 completions)
   Check: `goal-selector.py` recurring_saturation penalty range is 0 to -4.0 (capped by ratio * 4.0)
   Bash: grep -c "recurring_saturation" core/scripts/goal-selector.py → verify >= 2 (criterion + raw assignment)
   Bash: grep -c "recurring_saturation" core/config/goal-selection-algorithm.md → verify >= 1 (documented)

   # iteration-close.sh WM record accuracy (Section WT continued — 2026-04-18 review)
   # goal-selector.py reads `recurring` from goals_completed_this_session entries to compute
   # recurring_saturation. A hardcoded default in iteration-close.sh silently defeats that
   # penalty — the do_state_update path MUST look up the real flag from the aspiration.
   Bash: grep -c 'local recurring="false"' core/scripts/iteration-close.sh → verify returns 0 (no hardcoded default)
   Check: `core/scripts/iteration-close.sh` do_state_update extracts recurring from the same py aspirations-read lookup that produces asp_id (tab-separated or equivalent), NOT hardcoded before the lookup
   Check: `core/scripts/iteration-close.sh` has an in-code comment near the wm-append for `goals_completed_this_session` naming `goal-selector.py recurring_saturation` as the downstream consumer

   # iteration-close.sh fail-silent discipline (Section WT continued — 2026-04-18 cleanup,
   # narrowed 2026-04-20 after /verify-learning review: reads-with-defaults are allowed;
   # only write-path muzzles are forbidden).
   # Correctness-critical WRITES (wm-append.sh, wm-set.sh, team-state-update.sh) must surface
   # errors via `|| echo "[iteration-close] WARN: ..."`, never via `2>/dev/null` or `|| true`.
   # READS-with-sane-defaults (`wm-read.sh ... 2>/dev/null || echo "default"`) are a legitimate
   # pattern — they make missing slots produce predictable values downstream. Only muzzled
   # writes are the regression class this rule catches.
   # Productivity-stop-gate is the lone intentional `|| true` exception (opt-out per its docstring).
   Bash: grep -cE 'wm-(append|set)\.sh[^|]*2>/dev/null|team-state-update\.sh[^|]*2>/dev/null' core/scripts/iteration-close.sh → verify returns 0 (no write-path muzzles)
   Bash: grep -c 'echo "\[iteration-close\] WARN:' core/scripts/iteration-close.sh → verify returns >= 8 (correctness-critical wm-* and team-state-* writes surface failures)
   Check: every `wm-append.sh` and `wm-set.sh` call in `core/scripts/iteration-close.sh` is followed by `|| echo "[iteration-close] WARN: ..."` (never bare `|| true`, never `2>/dev/null`)
   Check: every `team-state-update.sh` call in `core/scripts/iteration-close.sh` is followed by `|| echo "[iteration-close] WARN: ..."` (never bare `|| true`, never `2>/dev/null`)

   # Retrieval escalation evidence checks (Section RX)
   Check: `core/config/conventions/retrieval-escalation.md` exists with "The Three Tiers" and "Mode Gates" sections
   Check: CLAUDE.md Convention Index includes `retrieval-escalation.md`
   Check: CLAUDE.md has "Knowledge Retrieval (All States)" heading (NOT "Knowledge Tree Retrieval")
   Check: `.claude/rules/user-interaction.md` has "Knowledge Retrieval (MANDATORY)" heading (NOT "Knowledge Tree Retrieval")
   Check: `.claude/rules/user-interaction.md` references `retrieval-escalation.md` convention
   Check: `respond/SKILL.md` conventions list includes `retrieval-escalation`
   Check: `respond/SKILL.md` Step 4 heading contains "Escalated Retrieval"
   Check: `respond/SKILL.md` Step 4 has Tier 1, Tier 2, Tier 3 subsections
   Check: `respond/SKILL.md` CRITICAL header does NOT say "tree retrieval" (says "3-tier escalation")
   Check: `aspirations-execute/SKILL.md` conventions list includes `retrieval-escalation`
   Check: `aspirations-execute/SKILL.md` has "Step 5a.1" (Tier 2) and "Step 5a.2" (Tier 3)
   Check: `aspirations-execute/SKILL.md` retrieval manifest schema includes `tiers_used` and `sufficient`
   Check: `aspirations-learning-gate/SKILL.md` conventions list includes `retrieval-escalation`
   Check: `aspirations-learning-gate/SKILL.md` Phase 9.5b has "Escalation quality check" block
   Check: `core/config/conventions/tree-retrieval.md` opens with cross-reference to `retrieval-escalation.md`
   Bash: grep -rl "Knowledge Tree Retrieval" CLAUDE.md .claude/rules/ .claude/skills/ 2>/dev/null | grep -v verify-learning → verify NO files (all renamed)

   # Background jobs subsystem evidence checks (Section BG)
   Check: `core/scripts/background-jobs.py` exists with `register`, `deregister`, `check`, `list`, `has-pending`, `clear` subcommands
   Check: `core/scripts/background-jobs.sh` exists and sources `_paths.sh` + `_platform.sh`
   Check: `core/scripts/background-jobs.sh` exports `MIND_SHELL` via cygpath (Windows Git Bash compatibility)
   Check: `core/config/conventions/session-state.md` has "Background External Jobs" section
   Check: `session-state.md` documents `completion_check` delegation pattern
   Check: CLAUDE.md Core Systems table includes `Background jobs` entry
   Check: CLAUDE.md session signals table includes `background-jobs.yaml` entry
   Check: CLAUDE.md Convention Index `session-state.md` row mentions "background jobs tracker"
   Check: `core/scripts/background-jobs.py` has register, deregister, check, list, has-pending, clear subcommands
   Bash: bash core/scripts/background-jobs.sh list 2>&1 → should output "No background jobs." (not crash)

   # Stale-jobs scanner checks (Section BG continued — 2026-04-19, g-115-106 buildout)
   Check: `world/scripts/stale-jobs-scan.py` exists with `report`, `reconcile`, `scan` subcommands
   Check: `world/scripts/stale-jobs-scan.sh` exists, sources `_paths.sh`, exec's the .py
   Check: `core/config/aspirations.yaml` has `stale_scanner:` block with `thresholds` key
   Check: `core/config/aspirations.yaml` `stale_scanner.thresholds` has entries for processor, roblox-bridge, llama-server, ssh-efs, ssh, default
   Check: `core/config/conventions/session-state.md` has "Background Job Hygiene" section mentioning stale-jobs-scan
   Check: `.claude/skills/scan-stale-jobs/SKILL.md` exists with `companion_scripts:` listing stale-jobs-scan.sh
   Check: `.claude/skills/scan-stale-jobs/SKILL.md` has `## Return Protocol` section (return-protocol.md rule)
   Check: `world/aspirations.jsonl` contains `g-115-106` recurring goal titled "Scan for stale background processes" with interval_hours=4
   Check: `world/scripts/stale-jobs-scan.py` `_ps_run()` prepends UTF-8 encoding stanza (`[Console]::OutputEncoding` + `$OutputEncoding`) — guard-313, rb-339
   Check: `world/scripts/stale-jobs-scan.py` `_ps_run()` passes `encoding="utf-8", errors="replace"` to subprocess.run — guard-313
   Check: `world/scripts/stale-jobs-scan.py` has `_identity_matches()` comparing CreationDate vs launched_at within tolerance — rb-341, BUG-2/BUG-6 fix
   Check: `world/scripts/stale-jobs-scan.py` `build_ancestor_set()` has "DO NOT reorder" comment explaining BUG-9 fix (check-before-add ordering from step 1 onward)
   Check: `world/scripts/stale-jobs-scan.py` `kill_candidate()` has "DO NOT collapse" comment explaining graceful-first rationale
   Check: `world/scripts/stale-jobs-scan.py` has both `read_this_agent_registry` and `read_all_agent_registries` functions (BUG-3 scope fix)
   Check: `world/scripts/stale-jobs-scan.py` `get_all_processes()` uses NDJSON per line (`ForEach-Object | ConvertTo-Json -Compress`), not single-batch JSON — rb-340
   Check: `world/scripts/stale-jobs-scan.py` `ORPHAN_SIGNATURES` entries are 3-tuples (label, cmdline_regex, name_regex) — prevents bash-wrapper false positives
   Check: `core/scripts/background-jobs.py` register `--goal` argument is optional with default `"standalone"` (Phase C prereq)
   Check: NO `NON_AGENT_DIRS` constant in `world/scripts/stale-jobs-scan.py` (dead filter removed in post-Phase-B cleanup C2)
   Check: NO `registered_pids` set in `world/scripts/stale-jobs-scan.py` `identify_candidates` (redundant with `do_not_kill`, removed in cleanup C1)
   Bash: bash world/scripts/stale-jobs-scan.sh report 2>&1 | head -3 → first line matches regex `=== stale-jobs-scan report \(\d{4}-\d{2}-\d{2}T` and no Python traceback appears
   Check: `world/scripts/roblox-studio.sh` start-bridge invokes `background-jobs.sh register --type roblox-bridge` (Phase C retrofit)
   Check: `world/scripts/roblox-studio.sh` stop-bridge invokes `background-jobs.sh deregister` (Phase C retrofit)
   Check: `world/scripts/processor-run.sh` run-auto invokes `background-jobs.sh register --type llama-server` (Phase C retrofit)
   Check: `world/scripts/processor-run.sh` run-auto invokes `background-jobs.sh deregister` in all three exit paths (health-fail exit, normal completion cleanup)

   # OMNI-EPIC-inspired aspiration generation evidence checks (Section OE)
   # Stepping-stone retrieval, interestingness filter, failure stepping-stones, ANNECS, pipeline depth
   Check: `core/scripts/aspirations.py` has `--stepping-stones` in read subcommand (mutually exclusive group)
   Check: `core/scripts/aspirations.py` has `--limit` arg on read subparser with `default=5`
   Bash: bash core/scripts/aspirations-read.sh --stepping-stones --limit 3 2>/dev/null → verify returns valid JSON array (even if empty)
   Check: `core/scripts/aspirations.py` stepping-stones sort key uses `or ""` (not `.get(key, default)`) — retired aspirations have `completed_at=None` (key exists), `.get()` returns None not default, sort crashes on None vs str comparison
   Check: `core/config/aspirations.yaml` has `stepping_stones` section with `default_limit: 5`
   Check: `core/config/aspirations.yaml` has `open_ended_progress` section with `metric: annecs`
   Check: `core/config/aspirations.yaml` has `pipeline_low_water_mark: 3` (top-level)
   Check: `core/config/aspirations.yaml` modifiable section has `pipeline_low_water_mark` with bounds {min: 1, max: 10, default: 3}
   Check: `core/config/meta.yaml` initial_state.aspiration_generation_strategy has `stepping_stone_preferences` with `partial_visibility_k: 5`
   Check: `core/config/meta.yaml` initial_state.aspiration_generation_strategy has `interestingness_criteria` with 4 weights summing to 1.0
   Check: `core/config/spark-questions.yaml` has `sq-c08` with `category: failure_stepping_stone` in both `seed_candidates` and `initial_state.candidates`
   Check: `create-aspiration/SKILL.md` has "Phase A.5" with `aspirations-read.sh --stepping-stones`
   Check: `create-aspiration/SKILL.md` Phase A has interestingness criteria block (NOVELTY, LEARNABILITY, WORTHWHILENESS, DIVERSITY)
   Check: `create-aspiration/SKILL.md` Step 5 heading contains "Interestingness Filter" (not just "Validate")
   Check: `create-aspiration/SKILL.md` Step 5 has ACCEPT/REFINE/REJECT scoring and evolution-log event
   Check: `create-aspiration/SKILL.md` invocation patterns table says "5-phase" (not "4-phase")
   Check: `create-aspiration/SKILL.md` says "all five phases" (not "all four phases")
   Check: `aspirations-spark/SKILL.md` has sq-c08 handler with "Failure Stepping-Stone" heading
   Check: `aspirations-spark/SKILL.md` sq-c08 handler has guard: title starts with "Stepping stone:" → SKIP (prevents infinite regression)
   Check: `aspirations-evolve/SKILL.md` Step 4 says "All aspiration creation in evolve routes through `/create-aspiration`" (single source of truth, no duplicate filter)
   Check: `aspirations-evolve/SKILL.md` Step 6 has "ANNECS metric update" block with read-then-set pattern (not increment)
   Check: `aspirations-evolve/SKILL.md` ANNECS comment says "meta-update is SET, not increment — read first" (prevents silent counter reset)
   Check: `aspirations-precheck/SKILL.md` has "Phase 0.5.1: Pipeline Depth Check" between Phase 0.5 and Phase 0.5a
   Check: `aspirations-precheck/SKILL.md` Phase 0.5.1 reads `pipeline_low_water_mark` from config
   Check: `aspirations-precheck/SKILL.md` Phase 0.5.1 counts only `status == "pending"` goals (not in-progress)
   Check: `aspirations-precheck/SKILL.md` Phase 0.5.1 invokes `from-self` WITHOUT `--plan` (fast tactical, not deep research)

   # Hypothesis pipeline health evidence checks (Section HPH)
   # Phase 0.5.2 creates investigation goals when hypothesis pipeline is starved.
   # Phase 0.5.3 creates investigation goals when accuracy drops critically.
   Check: `core/config/aspirations.yaml` has `hypothesis_pipeline_low_water_mark: 2` (top-level)
   Check: `core/config/aspirations.yaml` has `accuracy_critical_threshold: 0.40` (top-level)
   Check: `core/config/aspirations.yaml` has `accuracy_min_sample: 5` (top-level)
   Check: `core/config/aspirations.yaml` modifiable section has `hypothesis_pipeline_low_water_mark` with bounds {min: 0, max: 5, default: 2}
   Check: `core/config/aspirations.yaml` modifiable section has `accuracy_critical_threshold` with bounds {min: 0.20, max: 0.60, default: 0.40}
   Check: `core/config/aspirations.yaml` modifiable section has `accuracy_min_sample` with bounds {min: 3, max: 15, default: 5}
   Check: `aspirations-precheck/SKILL.md` has "Phase 0.5.2: Hypothesis Pipeline Health Check" between Phase 0.5.1 and Phase 0.5a
   Check: `aspirations-precheck/SKILL.md` Phase 0.5.2 reads `hypothesis_pipeline_low_water_mark` from config
   Check: `aspirations-precheck/SKILL.md` Phase 0.5.2 calls `pipeline-read.sh --counts` and sums discovered+active
   Check: `aspirations-precheck/SKILL.md` Phase 0.5.2 dedup checks both "pending" and "in-progress" (not just pending)
   Check: `aspirations-precheck/SKILL.md` Phase 0.5.2 uses `{target_asp.source}` (not `{asp.source}`) in --source flag
   Check: `aspirations-precheck/SKILL.md` has "Phase 0.5.3: Accuracy Health Gate" between Phase 0.5.2 and Phase 0.5a
   Check: `aspirations-precheck/SKILL.md` Phase 0.5.3 reads `accuracy_critical_threshold` and `accuracy_min_sample` from config
   Check: `aspirations-precheck/SKILL.md` Phase 0.5.3 calls `pipeline-read.sh --accuracy`
   Check: `aspirations-precheck/SKILL.md` Phase 0.5.3 requires `total_resolved >= accuracy_min_sample` before firing (prevents false alarms)
   Check: `aspirations-precheck/SKILL.md` Phase 0.5.3 dedup checks both "pending" and "in-progress"
   Check: `aspirations-precheck/SKILL.md` Phase 0.5.3 uses `{target_asp.source}` (not `{asp.source}`) in --source flag
   Bash: bash core/scripts/pipeline-read.sh --counts 2>/dev/null → verify returns JSON with discovered, active, resolved, archived keys
   Bash: bash core/scripts/pipeline-read.sh --accuracy 2>/dev/null → verify returns JSON with total_resolved, confirmed, corrected, accuracy_pct keys

   # Pipeline stage enum drift-watchdog (rb-363)
   # VALID_STAGES in pipeline.py, empty_meta() stage_counts keys (same file), and the
   # seed JSON literal in init-world.sh must all enumerate the same set. They are three
   # parallel writes of one enum — drift here is how the retired "evaluating" stage
   # survived as schema weight for months.
   Bash: python3 -c "
   import re, json, sys
   src = open('core/scripts/pipeline.py', encoding='utf-8').read()
   m = re.search(r'VALID_STAGES\s*=\s*\{([^}]+)\}', src)
   valid = set(re.findall(r'\"([^\"]+)\"', m.group(1))) if m else set()
   m2 = re.search(r'\"stage_counts\":\s*\{([^}]+)\}', src)
   empty = set(re.findall(r'\"([^\"]+)\":\s*0', m2.group(1))) if m2 else set()
   seed_src = open('core/scripts/init-world.sh', encoding='utf-8').read()
   m3 = re.search(r'\"stage_counts\":\{([^}]+)\}', seed_src)
   seed = set(re.findall(r'\"([^\"]+)\":0', m3.group(1))) if m3 else set()
   if valid == empty == seed:
       print(f'PASS: pipeline stage enum consistent across VALID_STAGES + empty_meta + init-world seed ({sorted(valid)})')
   else:
       print(f'FAIL: pipeline stage enum drift — VALID_STAGES={sorted(valid)} empty_meta={sorted(empty)} init-world_seed={sorted(seed)}')
       sys.exit(1)
   " → verify no drift between the three enum source sites

   # Forged skill resolution evidence checks (Section FSR)
   # Agents must resolve natural-language pseudocode ("notify the user") to forged skills
   # via triggers in forged-skills.yaml. Single source of truth — no guardrail duplication.
   Check: `.claude/rules/forged-skill-resolution.md` exists (core rule, domain-agnostic)
   Check: Rule points to `world/forged-skills.yaml` as THE resolution source (not guardrails)
   Check: Rule has exactly 2 rules (no fallback logic, no guardrail precedence)
   IF world/forged-skills.yaml exists and has entries:
       Check: Each forged skill entry has non-empty `triggers` list
       Check: Each forged skill entry has `forged_by` and `created` fields
   Check: No active guardrail with category `skill-resolution` exists (guard-050 retired — single source of truth)
   Bash: guardrail-check.sh --context any --dry-run 2>/dev/null | python3 -c "
   import sys,json; d=json.load(sys.stdin)
   sr=[m for m in d['matched'] if m.get('category')=='skill-resolution']
   print('PASS: no active skill-resolution guardrails' if len(sr)==0 else f'FAIL: {len(sr)} active skill-resolution guardrails (should be retired)')
   " → verify no dual-source guardrails

   # Notification-transport evidence checks (Section FSR-N)
   # Regression coverage for the 2026-04-19 notify-user fix (rb-314):
   # (1) SKILL.md description must be "pushy" — Claude reads description at system-prompt
   #     load and it is the primary trigger mechanism; weak descriptions silently undertrigger.
   # (2) Triggers split: SKILL.md front-matter holds INTERNAL aliases only; user-facing
   #     natural-language phrases live exclusively in world/forged-skills.yaml — duplicating
   #     across both files double-counts in capability-gate.py via all_entries.extend().
   # (3) Caller phrasing must match registered triggers — canonical template required.
   # (4) email-send.sh must not hardcode an agent name (misroutes other agents' attribution).
   Check: `.claude/skills/notify-user/SKILL.md` description contains either "Use whenever" or "Fires when" (pushy, third-person, enumerates scenarios per Anthropic skill-authoring best practices)
   Check: `.claude/skills/notify-user/SKILL.md` front-matter `triggers:` does NOT contain the user-facing phrase "notify the user" (user-facing phrases must live only in world/forged-skills.yaml — single source of truth for prose-driven resolution)
   Check: `.claude/rules/forged-skill-resolution.md` contains the section header "Preferred Phrasing for Notification Calls" (canonical template is the enforced pattern for base skills)
   Check: `.claude/skills/agent-completion-report/SKILL.md` contains the line "## Phase 5.5: Notify the User" (primary missing hook discovered during the 2026-04-19 fix — completion report without notification was the root symptom of "101 goals, 0 emails")
   Check: `.claude/skills/aspirations-consolidate/SKILL.md` contains "**Notify the User About Session End** (stop_mode ONLY)" (mid-loop consolidations recur — notification gated on stop_mode only; subject-based rate-limiter cannot dedupe changing goal counts)
   IF world/forged-skills.yaml has a `notify-user` entry:
       Check: `notify-user.triggers` contains "notify the user" (the canonical phrase used by base-skill callers — resolver matches this verbatim)
       Check: `notify-user.companion_scripts` contains "world/scripts/email-send.sh" (transport binding for attribution and rate-limit checks)
   IF world/scripts/email-send.sh exists:
       Check: NOT contains `agent_name = 'Alpha'` (hardcoded attribution misroutes other agents' emails through a fixed prefix — single source is $MIND_AGENT; the literal 'Alpha' here is the historical regression string being grep'd for, not a design reference)
       Check: NOT contains `os.environ.get('MIND_AGENT'` (silent fallback masks missing-env config errors — the bare `os.environ['MIND_AGENT']` subscript is intentional, fail-loud per communication-clarity.md)
       Check: contains `Empty-body guard` (transport-level refusal of bodyless payloads — the SendInfoAlert Lambda IGNORES InfoMessage when Title is present, so Title-only payloads render title+border+EMPTY body: 2026-05-20 completion emails, 2026-07-07 delta stop email, inbox-alert escalations. Removing the guard reopens every freehand/hand-built-JSON bypass of notify-build-payload.py)
   Check: `core/scripts/inbox-alert-age-check.py` contains `"Body": "\n".join(body_lines)` (unclaimed-alert escalation emails must set Body — the pre-2026-07-07 Title+InfoMessage-only payload rendered bodyless in the Lambda's structured mode)
   # notify-from-file.sh subshell error-propagation regression (rb-857, guard-522).
   # Without `shopt -s inherit_errexit`, command substitutions like
   # PAYLOAD=$(python3 ...) do NOT propagate the subshell's exit code under
   # `set -euo pipefail`. A Python crash inside the substitution silently
   # yields an empty/partial PAYLOAD, which email-send.sh then consumes —
   # garbage email body, no error surfaced. The shopt must appear within
   # the file's first 30 lines (immediately after `set -euo pipefail`),
   # and must appear exactly once (no duplicate / no later override).
   IF world/scripts/notify-from-file.sh exists:
       Bash: head -30 world/scripts/notify-from-file.sh | grep -c 'shopt -s inherit_errexit' → expect exactly 1 (subshell error propagation enforced before first command substitution)
       Bash: grep -c 'shopt -s inherit_errexit' world/scripts/notify-from-file.sh → expect exactly 1 (no later duplicate; single source of truth for the option)
   # notify-user Step 4 cleanup (g-115-221, session-81 framework changes regression).
   # Step 4 is the Fallback Cascade (Tier 2 pending-question + Tier 3 participant goal).
   # Logging belongs in `notification_log` via `wm-append.sh` from the email-success
   # path (Step 1.5/3), NOT in Step 4 itself — a `### Logging` subsection or `journal-add`
   # reference inside Step 4 was a session-81-era mix of transport-success bookkeeping
   # with the fallback flow.
   Bash: awk '/^## Step 4:/{f=1; next} /^## Step [0-9]/{f=0} f' .claude/skills/notify-user/SKILL.md | grep -cE 'journal-add|^### Logging' → expect 0 (Step 4 has neither a Logging subsection nor any journal-add reference)

   # notify-build-payload helper integration (Section FSR-NBP — sq-018, 2026-05-20)
   # Locks in the strategic fix from the encode-session 2026-05-20: the LLM
   # used to hand-construct notification payloads inline (variable Subject/Body
   # quality, silent-empty-email regressions). The new core/scripts/notify-build-payload.py
   # centralizes payload construction with a silent-empty-email guard (min Body
   # length, exit 2 if too short). /notify-user and /agent-completion-report
   # were rewritten to invoke the helper instead of hand-constructing.
   # Regression risk: someone restores the LLM-hand-construct path — these 5
   # checks catch it.
   Check: `core/scripts/notify-build-payload.py` exists (the centralized payload helper)
   Bash (C1): test -f core/scripts/notify-build-payload.py && echo "PASS: notify-build-payload.py exists" || echo "FAIL: helper deleted — LLM-hand-construct regression risk"
   Check: `.claude/skills/notify-user/SKILL.md` references `notify-build-payload.py` (Step 2/3 uses the helper, not inline construction)
   Bash (C2): grep -q notify-build-payload.py .claude/skills/notify-user/SKILL.md && echo "PASS: /notify-user invokes helper" || echo "FAIL: /notify-user no longer references helper — regression to hand-construct"
   Check: `.claude/skills/agent-completion-report/SKILL.md` Phase 5.5 uses the COMPLETION-REPORT.md pointer file (Phase 5.5 rewrite)
   Bash (C3): grep -q COMPLETION-REPORT.md .claude/skills/agent-completion-report/SKILL.md && echo "PASS: Phase 5.5 uses pointer file" || echo "FAIL: agent-completion-report no longer uses pointer pattern"
   Check: Silent-empty-email guard rejects short Body (exit 2 on message < min chars; prevents the 2026-05-20 incident of Title-only completion emails)
   Bash (C4): py -3 core/scripts/notify-build-payload.py --agent zeta --category info --subject x --message too_short > /dev/null 2>&1; if [ $? -eq 2 ]; then echo "PASS: silent-empty-email guard rejects short Body (exit 2)"; else echo "FAIL: guard regressed — short Body accepted (incident: 2026-05-20 Title-only emails)"; fi
   Check: notify-build-payload test suite passes (19 tests covering envelope shape, guard behavior, agent-attribution)
   Bash (C5): py -3 -m pytest core/scripts/tests/test_notify_build_payload.py -q > /dev/null 2>&1 && echo "PASS: notify-build-payload test suite (19 tests) green" || echo "FAIL: notify-build-payload tests regressed"

   # Skill description quality checks (Section FSR-D)
   # Regression coverage for the 2026-04-19 ultrathink review (guard-306, rb-321):
   # (1) No XML-tag-shaped placeholders in description fields — Claude injects the
   #     description verbatim into the system prompt; `<word>` is parsed as an
   #     unclosed XML block. Use `{word}` instead. Math operators (`>=`, `<=`) are fine.
   # (2) Every base SKILL.md description must include trigger phrasing ("Use whenever",
   #     "Fires when", or "Use when") — extends the notify-user pushy-description rule
   #     to the whole skill library. Weak descriptions silently undertrigger.
   Bash: python3 -c "
   import re, pathlib, sys
   XML_TAG = re.compile(r'<[A-Za-z][A-Za-z0-9_-]*>')
   # Accept any adverbial form: 'Use whenever', 'Use when', 'Use only when',
   # 'Fires when', 'Fires only when', 'Invoke whenever', etc. Up to 3 interstitial
   # words between the verb and when/whenever keeps the signal strong without
   # over-policing phrasing choices.
   TRIGGER = re.compile(r'\b(Use|Fires|Invoke)\s+(\w+\s+){0,3}when(ever)?\b', re.IGNORECASE)
   fails_xml, fails_trigger = [], []
   for skill_md in sorted(pathlib.Path('.claude/skills').glob('*/SKILL.md')):
       text = skill_md.read_text(encoding='utf-8')
       m = re.search(r'(?m)^description:\s*[\"\\'](.*?)[\"\\']\s*$', text, re.DOTALL)
       if not m: continue
       desc = m.group(1)
       if XML_TAG.search(desc):
           fails_xml.append(f'{skill_md.parent.name}: {XML_TAG.findall(desc)}')
       if not TRIGGER.search(desc):
           fails_trigger.append(skill_md.parent.name)
   if fails_xml:
       print('FAIL: XML-tag placeholders in description fields (use {foo} not <foo>):')
       for f in fails_xml: print(f'  {f}')
       sys.exit(1)
   if fails_trigger:
       print('FAIL: descriptions missing trigger phrase (Use whenever / Fires when / Use when):')
       for f in fails_trigger: print(f'  {f}')
       sys.exit(1)
   print(f'PASS: all base SKILL.md descriptions are XML-safe and pushy')
   " → verify every base skill description is XML-safe AND contains trigger phrasing

   # Skill discovery measurement (Section FSR-DI)
   # Behavioral counterpart to FSR-D static description check. FSR-D verifies
   # SKILL.md descriptions are well-formed at authoring time; FSR-DI verifies
   # the discovery measurement layer is wired and functional, so the rb-314
   # failure mode (skill correctly forged but never fires) gets surfaced even
   # when static checks pass. Per-execution skill-quality scoring is silent
   # on undertriggering — only the discovery layer can observe absence.
   Check: `meta/skill-discovery-strategy.yaml` exists with non-empty `windows.silent_window_days` (REQUIRED file — skill-discovery.py exits 3 with an explicit error message if missing or if any required top-level section is absent; the strategy file is the single source of truth for thresholds, no in-code defaults)
   Check: `core/scripts/skill-discovery.py` exists (the engine)
   Check: `core/scripts/skill-discovery.sh` exists (the wrapper)
   Check: `.claude/skills/aspirations-evolve/SKILL.md` contains "Skill Discovery Audit (Step 9.5.5" (the consumer that turns measurement into Investigate/Idea goals; without this wiring the script runs but no agent action follows)
   Check: `.claude/skills/aspirations-evolve/SKILL.md` contains "skill-discovery.sh flagged" (canonical invocation pattern)
   Bash: bash core/scripts/skill-discovery.sh report 2>&1 | head -1 | grep -E '^\{' → expect 1 line starting with `{` (script runs end-to-end against current data without crashing — JSON output proves all three data sources parsed)
   Bash: bash core/scripts/skill-discovery.sh flagged --action-required-only 2>&1 | python3 -c "import sys, json; d=json.load(sys.stdin); assert 'count' in d and 'skills' in d and isinstance(d['skills'], list); print('PASS: flagged output schema valid (count={}, skills={})'.format(d['count'], len(d['skills'])))" → schema invariant (count + skills:list) holds; consumer in aspirations-evolve depends on it
   # Regression checks for the session-92 fix passes — each guards against a
   # specific bug class the fresh-eyes review caught:
   # (FSR-DI-R1) DEFAULT_STRATEGY removal — the strategy file is the single
   #             source of truth; reintroducing in-code defaults would silently
   #             fall back to stale values when the YAML is missing/partial.
   # (FSR-DI-R2) aspirations-add-goal.sh API discipline — the script reads JSON
   #             from stdin, NOT --title/--priority/--category flags. Pseudocode
   #             that used the flags would fail at runtime.
   # (FSR-DI-R3) Dedup via load-aspirations-compact.sh, NOT grep — the strategy
   #             YAML once claimed grep; that was stale. Consumer pseudocode
   #             must use the canonical script for active-goal lookup.
   Bash: grep -c "DEFAULT_STRATEGY" core/scripts/skill-discovery.py → expect 0 (FSR-DI-R1: in-code default constants must NOT be reintroduced; load_strategy() is the single source and exits 3 if the YAML is missing or partial)
   Bash: awk '/^### Skill Discovery Audit/{f=1} /^### Pattern Signature Calibration/{f=0} f' .claude/skills/aspirations-evolve/SKILL.md | grep -c "aspirations-add-goal.sh.*--title" → expect 0 (FSR-DI-R2: Step 9.5.5 must use stdin JSON, not --title flag — the flag does not exist on aspirations.py add-goal)
   Bash: awk '/^### Skill Discovery Audit/{f=1} /^### Pattern Signature Calibration/{f=0} f' .claude/skills/aspirations-evolve/SKILL.md | grep -c "load-aspirations-compact" → expect ≥1 (FSR-DI-R3: dedup uses the canonical compact-load script, not grep on the JSONL file)

   # User-to-agent audit infrastructure checks (Section FSR-U)
   # Regression coverage for g-243-02 (2026-04-19): the [user]-goals drain
   # uses three coupled components — missing any one silently re-strands
   # goals at participants:["user"]:
   # (1) Batch script (core/scripts/audit-user-to-agent.{sh,py}) scans all
   #     aspirations and promotes via capability-gate match.
   # (2) Inline precheck sweep (aspirations-precheck Phase 0.5d) re-checks
   #     candidates every loop iteration — closes the window between forge
   #     events and batch-audit runs.
   # (3) Evidence-packet override in capability-gate.py (--evidence flag)
   #     accepts typed empirical proof instead of free-text justification.
   # Case study: g-115-99, where alpha had 100% empirical evidence a rule
   # was wrong but could not act because capability-gate locked out the
   # [user] participant list. The --evidence path preserves user visibility
   # ([agent, user]) while letting evidence-bearing agent work proceed.
   Check: `core/scripts/audit-user-to-agent.sh` exists (bash wrapper — canonical invocation path for drain passes)
   Check: `core/scripts/audit-user-to-agent.py` exists (Python implementation — dry-run default, --apply flag promotes participants)
   Check: `core/scripts/capability-gate.py` contains `ap.add_argument("--evidence"` (structured evidence-override registered in argparse — without it, no empirical-override path exists and g-115-99-class goals re-strand)
   Check: `core/scripts/capability-gate.py` contains `"action": "evidence-approval"` (audit-log discriminator — evidence approvals must be distinguishable from free-text overrides in blocker-gate-overrides.jsonl)
   Check: `core/config/create-blocker-protocol-digest.md` contains "Prefer --evidence over" (Step 2.6 canonical guidance — moved to digest in commit 05ac4ae during extraction refactor)
   Check: `.claude/skills/aspirations-precheck/SKILL.md` Phase 0.5.0 routes `user-goals:reclassifiable_user_goals` to `aspirations-update-goal.sh participants` (inline re-classification fires every loop iteration — closes the forge-to-batch-audit window)
   Check: `.claude/rules/capability-before-user.md` contains "## Required Checklist" AND "## Decision Rule" (the 5-item LLM-side checklist that capability-gate.py mirrors — removing it breaks hybrid [agent, user] routing)
   Bash: python3 -c "
   import json, subprocess, pathlib
   world_dir = subprocess.check_output('bash -c \"source core/scripts/_paths.sh && echo \$WORLD_DIR\"', shell=True).decode().strip()
   log = pathlib.Path(world_dir) / 'audit-user-to-agent.jsonl'
   if not log.exists():
       print('PASS: audit-user-to-agent.jsonl not present (no reclassifications yet — infrastructure-only verification)')
       raise SystemExit(0)
   required = {'timestamp','goal_id','aspiration_id','source','old_participants','new_participants','matched_capability'}
   bad = []
   for i, line in enumerate(log.read_text(encoding='utf-8').splitlines(), 1):
       if not line.strip(): continue
       rec = json.loads(line)
       missing = required - set(rec.keys())
       if missing:
           bad.append(f'line {i}: missing {sorted(missing)}')
       if rec.get('old_participants') != ['user']:
           bad.append(f'line {i}: old_participants != [\"user\"]')
       if rec.get('new_participants') != ['agent','user']:
           bad.append(f'line {i}: new_participants != [\"agent\",\"user\"]')
   if bad:
       print('FAIL: audit-user-to-agent.jsonl schema violations:')
       for b in bad: print(f'  {b}')
       raise SystemExit(1)
   print('PASS: audit-user-to-agent.jsonl schema clean')
   " → verify audit-log schema invariants (required fields + canonical participant shapes)

   # Evidence-approval invariants (Section FSR-U continued, session 92)
   # Regression coverage for bugs caught during fresh-eyes review of the MVP:
   # - Bug A (fixed): reason message claimed "logged" unconditionally when
   #   approval_kind == "evidence", contradicting evidence_logged_to == None
   #   in the agent-routed case. Fix: reason message now derives from
   #   evidence_logged_to so both outputs agree.
   # - Bug E (cleaned): _fileops import was inline + sys.path.insert duplicated.
   #   Hoisted to module top. Verifies the import doesn't drift back.
   # - The log-condition guard intended_participants == "user" is load-bearing:
   #   remove it and every --evidence call logs to the ledger regardless of
   #   whether a block was averted, polluting signal.
   Check: `core/scripts/audit-user-to-agent.py` contains `from _fileops import locked_append_jsonl` at module top (NOT inside any function — inline imports indicate the regression described in Bug E is back)
   Check: `core/scripts/audit-user-to-agent.py` has a sys.path.insert for SCRIPT_DIR before the _fileops import at module scope, and does NOT re-insert sys.path inside _log_reclassification (no inline import pattern)
   Check: `core/scripts/audit-user-to-agent.sh` uses thin-wrapper pattern (`exec python3 "$CORE_ROOT/scripts/audit-user-to-agent.py" "$@"`) — matches capability-gate.sh / other canonical wrappers
   Check: `core/scripts/capability-gate.py` contains the comment string "INVARIANT: log evidence approval ONLY when" near the _log_evidence_approval call site (the load-bearing intended_participants == "user" guard explanation — removing the comment is OK but removing the guard is not)
   Check: `core/scripts/capability-gate.py` _log_evidence_approval call site has the triple-AND condition `approval_kind == "evidence" and bool(matches) and args.intended_participants == "user"` (Bug A regression guard — log only when the gate would have blocked without evidence)
   Check: `core/scripts/capability-gate.py` evidence-branch reason message derives from evidence_logged_to (e.g., contains `if evidence_logged_to else` ternary in the approval_kind == "evidence" branch) — prevents the Bug A divergence where reason says "logged" but JSON field is None
   Bash: bash core/scripts/audit-user-to-agent.sh --limit 1 >/dev/null 2>&1 && echo "PASS: audit-user-to-agent.sh dry-run exits 0" || echo "FAIL: audit-user-to-agent.sh smoke test failed"
   Check: `core/config/conventions/evidence-envelope.md` exists (canonical shape — prevents convention drift when a second gate adopts --evidence)

   # Subagent-spawning context-builder checks (Section FSR-S)
   # Regression coverage for 2026-04-19 root-cause fix: build-agent-context.py
   # used to omit resolved external paths, causing Explore subagents to search
   # only the local repo and miss canonical world/ artifacts (rb-258 incident).
   # The fix added an unconditional RESOLVED PATHS section to builder output.
   # Three-way coupling: guard-323 mandates builder use; agent-spawning.md
   # documents the output schema; build-agent-context.py emits the section.
   # All three must stay consistent — verify each side of the triangle.
   Check: `core/scripts/build-agent-context.py` contains `META_DIR` (imported from _paths for RESOLVED PATHS render)
   Check: `core/scripts/build-agent-context.py` contains `AGENT_NAME` (imported from _paths for RESOLVED PATHS render)
   Check: `core/scripts/build-agent-context.py` contains `RESOLVED PATHS` (section header literal — if missing, builder silently skips path emission and subagents regress to local-repo-only search)
   Check: `core/config/conventions/agent-spawning.md` contains `RESOLVED PATHS` (output schema documents the section so future readers know it is part of the contract, not optional)
   Bash: python3 -c "
   import subprocess
   out = subprocess.check_output(
       ['bash', 'core/scripts/build-agent-context.sh', '--category', 'framework-engineering', '--max-tokens', '2000'],
       text=True, encoding='utf-8', errors='replace',
   )
   required = ['RESOLVED PATHS', 'PROJECT_ROOT:', 'WORLD_DIR:', 'META_DIR:', 'AGENT_DIR:']
   missing = [m for m in required if m not in out]
   if missing:
       print(f'FAIL: build-agent-context.sh output missing tokens: {missing}')
       raise SystemExit(1)
   print('PASS: build-agent-context.sh emits RESOLVED PATHS section with all four paths')
   " → live-run the builder and verify subagents will receive canonical external paths

   # World-level forged infrastructure checks (Section FS2)
   # Forged skills, skill relations, and companion scripts are world-level (shared).
   # No per-agent copies should exist.
   Bash: python3 -c "
   import yaml, subprocess, os, pathlib
   world_dir = subprocess.check_output('bash -c \"source core/scripts/_paths.sh && echo \$WORLD_DIR\"', shell=True).decode().strip()
   # Check world/forged-skills.yaml exists with forged_by
   with open(f'{world_dir}/forged-skills.yaml') as f: d=yaml.safe_load(f)
   for name, entry in d.get('skills', {}).items():
       assert 'forged_by' in entry, f'FAIL: {name} missing forged_by field'
   print(f'PASS: {len(d.get(\"skills\", {}))} forged skills, all have forged_by')
   # Check world/skill-relations.yaml exists
   assert pathlib.Path(f'{world_dir}/skill-relations.yaml').exists(), 'FAIL: world/skill-relations.yaml missing'
   print('PASS: world/skill-relations.yaml exists')
   # Check no per-agent forged-skills.yaml (migration complete — tombstones should be deleted too)
   stale = list(pathlib.Path('.').glob('*/forged-skills.yaml'))
   assert len(stale) == 0, f'FAIL: per-agent forged-skills.yaml found: {stale} (delete — world/ is the single source)'
   print('PASS: no per-agent forged-skills.yaml')
   # Check no per-agent skill-relations.yaml
   for sr in pathlib.Path('.').glob('*/skill-relations.yaml'):
       if sr.parts[0] == 'core': continue
       assert False, f'FAIL: per-agent {sr} still exists'
   print('PASS: no per-agent skill-relations.yaml')
   " → verify world-level single source of truth

   # Config value consistency evidence checks (Section CV)
   # Single source of truth: _tree.yaml for max_skills. Other files must match.
   Bash: python3 -c "
   import yaml
   with open('.claude/skills/_tree.yaml') as f: tree = yaml.safe_load(f)
   cap = tree['config']['max_skills']
   with open('core/config/skill-gaps.yaml') as f: gaps = yaml.safe_load(f)
   gaps_default = gaps['modifiable']['max_skills']['default']
   gaps_max = gaps['modifiable']['max_skills']['max']
   assert gaps_default == cap, f'FAIL: skill-gaps.yaml default ({gaps_default}) != _tree.yaml ({cap})'
   assert gaps_max >= cap, f'FAIL: skill-gaps.yaml max ({gaps_max}) < _tree.yaml ({cap})'
   print(f'PASS: max_skills={cap} consistent across _tree.yaml and skill-gaps.yaml')
   with open('.claude/skills/forge-skill/SKILL.md') as f: forge = f.read()
   assert f'Maximum {cap} total skills' in forge, f'FAIL: forge-skill/SKILL.md does not say Maximum {cap}'
   print(f'PASS: forge-skill/SKILL.md references {cap}')
   " → verify all max_skills references are consistent

   # Agent directory structural hygiene checks (Section AH)
   # Catches common cruft patterns: misplaced journals, per-agent script dirs, stale migration stubs.
   # Root causes: early-boot naming before conventions stabilized, consolidation path bugs, migration leftovers.
   Bash: python3 -c "
   import pathlib, json, sys
   errors = []
   # 1. No loose journal files at agents/<agent>/journal/ level (must be in YYYY/MM/)
   for agent in [p.parent for p in pathlib.Path('.').glob('*/.initialized')]:
       journal_dir = agent / 'journal'
       if journal_dir.is_dir():
           loose = [f for f in journal_dir.iterdir() if f.is_file() and f.suffix == '.md']
           if loose:
               errors.append(f'Loose journal files in {journal_dir}/: {[f.name for f in loose]} (should be in YYYY/MM/)')
   # 2. No agents/<agent>/scripts/ directory (domain scripts belong in world/scripts/)
   for agent in [p.parent for p in pathlib.Path('.').glob('*/.initialized')]:
       scripts_dir = agent / 'scripts'
       if scripts_dir.is_dir():
           errors.append(f'{scripts_dir}/ exists (domain scripts belong in world/scripts/)')
   # 3. Journal index entries point to existing files
   for agent in [p.parent for p in pathlib.Path('.').glob('*/.initialized')]:
       jfile = agent / 'journal.jsonl'
       if jfile.exists():
           for i, line in enumerate(jfile.open(), 1):
               rec = json.loads(line)
               jpath = rec.get('journal_file', '')
               if jpath and not pathlib.Path(jpath).exists():
                   errors.append(f'{agent.name}/journal.jsonl line {i}: journal_file \"{jpath}\" does not exist')
                   break  # one broken ref is enough to flag
   if errors:
       for e in errors: print(f'FAIL: {e}')
       sys.exit(1)
   print('PASS: agent directory structure clean (no loose journals, no per-agent scripts/, journal index intact)')
   " → verify agent directory hygiene

   # No-gitignored-temp-citation check (Section AH cont.) — guard-766 automated enforcement (g-115-1678)
   # A committed framework file (core/, .claude/, mind_api/) must NOT cite an
   # agents/<agent>/temp/ path in a docstring / comment / code pointer: that path
   # is gitignored, so the reference is absent in every fresh clone (silent until
   # `git check-ignore` is run). Origin g-115-1676: compounding-events.py cited an
   # agents/<agent>/temp/ path in its module docstring — fixed by /drain-temp
   # promotion to core/config/rationale/compounding-metric.md (the committed path
   # the code now cites). guard-766 is the behavioral rule; this is its automated
   # enforcement layer. The grep -vE allowlist is a single PERMANENT tier of legit
   # describers — files whose JOB is to describe/exercise the path:
   # core/config/rationale/ (drained->promoted records), core/scripts/tests/
   # (synthetic fixtures), core/config/conventions/temp-store.md (the convention),
   # .claude/rules/ (path-resolution etc.), this SKILL.md (the check itself),
   # .claude/skills/drain-temp/ + seed/ (lifecycle docs), mind_api/docs/ (historical
   # campaign-log records). The GRANDFATHERED tier (8 drained-design-doc pointers) was
   # fully genericized + de-allowlisted 2026-06-28 (g-115-1679, the g-115-1678 follow-up);
   # any new agents/<agent>/temp/ citation is now a FAIL, not a grandfathered pass.
   Bash (no-gitignored-temp-citations): HITS=$(grep -rnE 'agents/[a-z]+/temp/' core/ .claude/ mind_api/ --include='*.py' --include='*.sh' --include='*.md' --include='*.yaml' --include='*.yml' 2>/dev/null | grep -vE '(core/config/rationale/|core/scripts/tests/|core/config/conventions/temp-store\.md|\.claude/rules/|\.claude/skills/verify-learning/SKILL\.md|\.claude/skills/drain-temp/|\.claude/skills/seed/SKILL\.md|mind_api/docs/)'); [ -z "$HITS" ] && echo "PASS: no committed framework code cites a gitignored agents/<agent>/temp/ path beyond the documented allowlist (guard-766)" || echo "FAIL: committed framework code cites a gitignored agents/<agent>/temp/ path (absent in fresh clones) — cite the promoted committed path or genericize (g-115-1676 / g-115-1678, guard-766). Offending: $HITS"

   # Programmatic utilization enforcement evidence checks (Section PU)
   # Three-layer enforcement: auto-manifest (retrieve.py), script feedback (utilization-feedback.sh), hook backstop (utilization-gate.sh)
   Bash: grep -c "\-\-goal" core/scripts/retrieve.py → verify >= 2 (argparse definition + session file guard)
   Bash: grep -c "\-\-tree-nodes" core/scripts/retrieve.py → verify >= 1 (argparse definition exists)
   Bash: grep -c "retrieval-session.json" core/scripts/retrieve.py → verify >= 1 (session file write exists)
   Check: `core/scripts/utilization-feedback.py` exists with `_recompute_utility_ratio` function
   Check: `core/scripts/utilization-feedback.py` `_recompute_utility_ratio` docstring references `tree.py cmd_increment` as canonical source
   Check: `core/scripts/utilization-feedback.py` has `add_mutually_exclusive_group(required=True)` with `--helpful`, `--all-helpful`, `--all-noise`, `--all-unknown`, `--infer`
   Check: `core/scripts/utilization-feedback.sh` sources both `_paths.sh` and `_platform.sh` (guard-051)
   Check: `core/scripts/utilization-gate.sh` sources both `_paths.sh` and `_platform.sh` (CRITICAL: without _platform.sh, hook is dead on Windows)
   Check: `core/scripts/utilization-gate.sh` only acts on skill `aspirations-state-update` (all other skills exit 0 immediately)
   Bash: python3 -c "import json; d=json.load(open('.claude/settings.json')); hooks=[h for e in d['hooks']['PreToolUse'] if e['matcher']=='Skill' for h in e['hooks']]; assert any('utilization-gate' in h['command'] for h in hooks)" 2>&1 → verify no error (hook registered)
   Check: `aspirations-execute/SKILL.md` Step 4 retrieve call includes `--goal {goal.id}` and `--tree-nodes`
   Check: `aspirations-execute/SKILL.md` Phase 4.26 has `utilization-feedback.sh` as PRIMARY PATH
   Check: `aspirations-execute/SKILL.md` Step 5b says "AUTO-GENERATED by retrieve.sh" (not "MANDATORY — construct JSON manifest")
   Check: `aspirations-learning-gate/SKILL.md` Phase 9.5b references `retrieval-session.json` as primary source
   Check: `core/config/execute-protocol-digest.md` Step 4 includes `--goal` flag in retrieve.sh call
   **Runtime**: After goal execution with retrieval, `agents/<agent>/session/retrieval-session.json` exists with `utilization_pending: false`
   **Runtime**: After 5+ goals with retrieval, some tree nodes have `utility_ratio > 0` (data flowing through pipeline)

   # Cognitive-core curation evidence checks (Section PU2)
   # Phase 2 (dedup gate) + Phase 1 (inferred helpfulness) + Phase 1.5 (utility-weighted retrieval)
   # + Phase 4 (scheduled tree debt) + Phase 3 (capability-aware child limits).
   # If any check fails, the 70+ zero-utility-node symptom returns.
   Check: `core/scripts/tree-dedup-check.py` exists; docstring lists exit codes 0/2/3/4
   Check: `core/scripts/tree-dedup-check.sh` exists as thin wrapper (sources `_paths.sh` + `_platform.sh`)
   Check: `core/scripts/tree.py` has `_UTILITY_RATIO_FIELDS` tuple containing all 4 fields (times_helpful, times_inferred_helpful, retrieval_count, times_noise)
   Check: `core/scripts/tree.py` has `_recompute_utility_ratio` function with `DO NOT INLINE` critical-lines comment
   Check: `core/scripts/tree.py` `cmd_increment` single-op path calls `_recompute_utility_ratio(node)` (not inlined formula)
   Check: `core/scripts/tree.py` `cmd_batch` increment op calls `_recompute_utility_ratio(node)` (not inlined formula)
   Check: `core/scripts/utilization-feedback.py` `_recompute_utility_ratio` formula literal matches tree.py exactly: `(th + 0.5 * tih) / max(rc, 1)`
   Check: `core/scripts/utilization-feedback.py` has `--infer` and `--confidence {conservative,balanced}` args in mutually-exclusive group
   Check: `core/scripts/utilization-feedback.py` has `infer_feedback(session, confidence)` returning None for schema_version<2 and 4-tuple for schema v2
   Check: `core/scripts/utilization-feedback.py` does NOT contain `_fetch_guardrail_trigger_ids` (dead feature removed)
   Check: `core/scripts/retrieve.py` session record has `"schema_version": 2` and `tree_nodes_detail` + `supplementary_detail` with `distinctive_tokens` fields
   Check: `core/scripts/retrieve.py` scoring loop uses `_utility_weight` (or equivalent clamped reweight) producing 6-tuple (key, node, effective, channel, base, weight)
   Check: `core/scripts/utilization-gate.sh` calls `--infer --confidence conservative` (not `--all-noise`) as primary path, with `--all-unknown` fallback on exit 4 or non-zero (post-2026-05-07; was `--all-noise` pre-fix)
   Check: `core/scripts/utilization-gate.sh` preserves stderr on the `--infer` call (does NOT pipe to `/dev/null 2>&1`)
   Check: `core/config/tree.yaml` has sections `dedup:`, `retrieval:`, `tree_debt_check:`, `child_limits:`
   Check: `core/config/tree.yaml` `modifiable:` has bounds for every numeric parameter in the 4 new sections (e.g. `dedup_sibling_overlap_threshold`, `retrieval_utility_weight_min`, `tree_debt_check_debt_threshold`, `child_limits_explore`)
   Check: `core/config/tree.yaml` `child_limits:` has `mode:` key with value in {warn, block, off}
   Check: `core/scripts/iteration-close.sh` `do_learning_gate` references `tree-read.sh --decompose-candidates` (per-iteration compound-debt scan; full distill+decompose query at session-end lives in aspirations-consolidate)
   Check: `core/config/aspirations-loop-digest.md` documents the tree-debt check cadence at the digest's per-iteration body (replaces the old aspirations/SKILL.md inline pseudocode that referenced config-override-set.sh)
   Check: `.claude/skills/research-topic/SKILL.md` SPROUT section documents dedup gate and update-in-place on exit 3
   Check: `.claude/skills/reflect-tree-update/SKILL.md` leaf→interior split step documents dedup gate behavior
   **Runtime**: After 5+ goals with diary activity, some tree nodes have `times_inferred_helpful > 0` (--infer backstop producing signal)
   **Runtime**: `tree-dedup-check.sh --parent <parent> --key <existing-sibling>` exits 2 (exact-key reject)
   **Runtime**: `tree-dedup-check.sh --parent <parent> --key <new> --summary "<summary sharing 60%+ tokens with existing sibling>"` exits 3 with matching sibling key in stdout
   **Runtime**: After 3 sessions post-ship, `tree-read.sh --distill-candidates | wc -l` has fallen vs baseline (self-healing underway)

   # Retrieval-trigger additions (Section RT — G11-G13, 2026-05-12, g-115-642)
   # Cross-ref: core/config/conventions/retrieval-triggers.md G11/G12/G13.
   # G11 = /respond Step 5.0 Pre-Write Retrieval (non-autonomous counterpart to G7);
   # G12 = /respond Step 6.1.a' Broad re-retrieve on user correction (sister to G3
   # surprise→broad-retrieve); G13 = /encode-session Lane 1.0 Pre-Encoding Retrieval
   # (broad-retrieve snapshot consumed by sub-lanes 1.1/1.2/1.3). Plus regression
   # guard against rule_a re-introduction in retrieve.py _entry_matches_text — the
   # earlier draft accepted single ≥5-char tokens and matched 300/688 RB entries
   # for stopword-heavy queries; surviving rule is "≥2 distinct length-≥5 tokens".
   Check: `core/scripts/retrieve.py` `_entry_matches_text` requires ≥2 distinct length-≥5 tokens. Bash: grep -A 70 "^def _entry_matches_text" core/scripts/retrieve.py | grep -qE "len\(t\)\s*>=\s*5" && grep -A 70 "^def _entry_matches_text" core/scripts/retrieve.py | grep -qE "matched\s*>=\s*2" && echo "PASS: _entry_matches_text rule_b enforced (≥2 distinct length-≥5 tokens)" || echo "FAIL: rule_a regression — single-token or non-≥5 match path re-introduced in _entry_matches_text"
   Check: `.claude/skills/respond/SKILL.md` has `## Step 5.0: Pre-Write Retrieval (G11 / R14)`. Bash: grep -qE "^## Step 5\.0: Pre-Write Retrieval \(G11 / R14\)" .claude/skills/respond/SKILL.md && echo "PASS: G11 Step 5.0 present" || echo "FAIL: G11 Step 5.0 missing"
   Check: `.claude/skills/respond/SKILL.md` has `Broad re-retrieve (G12 / R15)`. Bash: grep -qE "Broad re-retrieve \(G12 / R15\)" .claude/skills/respond/SKILL.md && echo "PASS: G12 Broad re-retrieve present" || echo "FAIL: G12 Broad re-retrieve missing"
   Check: `.claude/skills/encode-session/SKILL.md` has `1.0 Pre-Encoding Retrieval (G13 / R16)`. Bash: grep -qE "1\.0 Pre-Encoding Retrieval \(G13 / R16\)" .claude/skills/encode-session/SKILL.md && echo "PASS: G13 Lane 1.0 present" || echo "FAIL: G13 Pre-Encoding Retrieval missing"
   Check: `core/config/conventions/retrieval-triggers.md` catalogs G11/G12/G13 with ✓. Bash: grep -qE "^\| G11 \| ✓" core/config/conventions/retrieval-triggers.md && grep -qE "^\| G12 \| ✓" core/config/conventions/retrieval-triggers.md && grep -qE "^\| G13 \| ✓" core/config/conventions/retrieval-triggers.md && echo "PASS: G11-G13 catalog rows present with ✓" || echo "FAIL: G11/G12/G13 catalog row(s) missing or unchecked"

   # Framework rules + conventions loader (Section RT2 — G8, 2026-05-12, g-001-233)
   # Cross-ref: core/config/conventions/retrieval-triggers.md G8.
   # G8 closure (g-001-231) added load_framework_rules() to retrieve.py — walks
   # .claude/rules/*.md, core/config/conventions/*.md, world/conventions/*.md;
   # parses title + section headers + first 500 chars of body per file;
   # reuses _entry_matches_text token-overlap; opt-in via --include-framework
   # flag; result returned under framework_rules key. Each check below catches
   # a different past regression class: function presence, flag wiring, fence
   # tracking (rb-863 — example `# foo` inside ``` blocks not captured as real
   # H1), forward-slash normalization (cross-platform path consistency),
   # conditional gating (no work when --include-framework absent), end-to-end
   # behavior (verify-before-assuming.md surfaces for "verification multi-signal").
   Check: `core/scripts/retrieve.py` has `load_framework_rules` function definition. Bash: grep -qE "^def load_framework_rules\(categories\):" core/scripts/retrieve.py && echo "PASS: load_framework_rules defined" || echo "FAIL: load_framework_rules removed or renamed"
   Check: `core/scripts/retrieve.py` argparse has `--include-framework` flag. Bash: grep -qE '"--include-framework"' core/scripts/retrieve.py && echo "PASS: --include-framework argparse wired" || echo "FAIL: --include-framework argparse missing"
   Check: `core/scripts/retrieve.py` `_build_framework_index` tracks code-fence state via `in_fence`. Bash: grep -qE "in_fence = False" core/scripts/retrieve.py && grep -qE "in_fence = not in_fence" core/scripts/retrieve.py && echo "PASS: fence-tracking present (rb-863)" || echo "FAIL: fence-tracking removed — example # headers inside ``` will be captured as document structure"
   Check: `core/scripts/retrieve.py` `_build_framework_index` normalizes Windows backslashes to forward-slash. Bash: grep -A 80 "^def _build_framework_index" core/scripts/retrieve.py | grep -qF 'replace("\\", "/")' && echo "PASS: forward-slash normalization present" || echo "FAIL: backslash→forward-slash normalization removed (cross-platform path bug)"
   Check: `core/scripts/retrieve.py` gates `framework_rules` result on `args.include_framework`. Bash: grep -qE "if args\.include_framework else" core/scripts/retrieve.py && echo "PASS: include_framework conditional gating present" || echo "FAIL: framework_rules computed unconditionally (G8 R13 violated — should be opt-in)"
   Check (runtime): `retrieve.sh --include-framework --category "verification multi-signal"` returns verify-before-assuming.md in framework_rules. Bash: bash core/scripts/retrieve.sh --include-framework --category "verification multi-signal" --depth shallow --read-only 2>/dev/null | py -3 -c 'import json,sys; raw=sys.stdin.read(); raw=raw[raw.find("{"):]; d=json.loads(raw); fr=d.get("framework_rules",[]); paths=[e.get("path","") for e in fr]; sys.exit(0 if any("verify-before-assuming" in p for p in paths) else 1)' && echo "PASS: verify-before-assuming.md surfaces for 'verification multi-signal'" || echo "FAIL: framework rules e2e — verify-before-assuming.md missing from result"

   # Domain-rebalance invariants (plan: how-do-we-improve-golden-cook, 2026-04-18)
   # Cross-ref: rb-265 (silent signal drop), asp-242 g-242-03 (closed out-of-cycle)
   # Goal: ensure all three layers of the "silent signal drop" antipattern stay closed.
   # If any of these fails, utilization_score will silently drift back to 0.0 everywhere.
   Check: `core/scripts/reasoning-bank.py` `UTILIZATION_COUNTERS` set contains `"times_inferred_helpful"` (missing → --infer path silently rejects all increments, reviving rb-265 instance 3)
   Check: `core/scripts/reasoning-bank.py` `RB_DEFAULT_FIELDS` and `GUARD_DEFAULT_FIELDS` both have `"times_inferred_helpful": 0` in `utilization` sub-object (normalize_record needs it)
   Check: `core/scripts/reasoning-bank.py` `recompute_utilization_score` formula is exactly `(th + 0.5 * tih) / max(rc, 1)` — half-weight for inferred matches tree.py; any drift breaks symmetric scoring

   # rb source_goal attribution invariants (g-240-61, session-55)
   # Closes the orphan-entry gap that produced rb-416 / rb-418 (null source_goal)
   # and tripped aspiration-trajectory.py false zero_learning_velocity.
   Check: `core/scripts/reasoning-bank.py` `RB_DEFAULT_FIELDS` contains `"source_goal": None` (peer of `source_hypothesis`) — missing → auto-populate path silently drops the field and attribution regresses
   Check: `core/scripts/reasoning-bank.py` defines `_read_in_flight_goal_id()` that reads `$WORLD_DIR/team-state.yaml` via `yaml.safe_load`, dereferences `agent_status.<MIND_AGENT>.in_flight.goal_id`, and returns None on ANY error (fail-open — rb adds MUST NOT block on attribution lookup)
   # rb_add CLI removed in H2 Wave 2 (2026-05-15). source_goal auto-populate
   # now lives in _rb_inject_source_goal() in mind_api/src/store_registry.py,
   # invoked as the prepare hook by the daemon store append endpoint.
   Check: `mind_api/src/store_registry.py` `_rb_inject_source_goal` injects `source_goal` via `_read_in_flight_goal_id()` BEFORE defaults application (order matters: defaults would set None before the inference runs)
   Check: `mind_api/src/store_registry.py` `_rb_inject_source_goal` inject condition is `"source_goal" not in rec` (NOT `rec.get("source_goal") is None` — explicit-null MUST win so callers can record cross-goal insights that belong to no single originating goal)

   # Validate-every-write-path invariants (rb-364 + guard-330, 2026-04-19)
   # Enforces that update-field is not a back-door around add-time validation,
   # and that validation runs BEFORE recompute (validating post-recompute
   # silently clobbers user error into valid-looking writes).
   # rb_update_field / guard_update_field CLI removed in H2 Wave 2 (2026-05-15).
   # Validate-before-recompute invariant now lives in the daemon set_field handler
   # (mind_api/src/endpoints/store.py) + store_registry.py validate/recompute hooks.
   Check: `mind_api/src/endpoints/store.py` `set_field` handler calls `spec.validate(ctx, rec)` BEFORE `spec.recompute(rec)` — post-recompute validation silently coerces user writes; canonical incident rb-364 T-neg-2
   Check: `core/scripts/reasoning-bank.py` `validate_utilization` rejects negative counter values (each key in UTILIZATION_COUNTERS checked `isinstance(v, int) and not isinstance(v, bool) and v >= 0`) and rejects negative `utilization_score`
   Check: `core/scripts/reasoning-bank.py` imports `ID_RE as EXPERIENCE_REF_RE` from `experience.py` (single source of truth — do NOT redefine locally; the two stores' ID formats must stay locked by construction)
   Check: `core/scripts/reasoning-bank.py` `validate_rb_record` and `validate_guard_record` reject `experience_ref` values that do not match the imported EXPERIENCE_REF_RE (only None or matching `^exp-[a-z0-9._-]+$` allowed)
   Check: `core/scripts/experience.py` `cmd_update_field` calls `normalize_record(rec)` before the mutation block AND `validate_record(rec)` between mutation and `write_jsonl` — same guard-330 invariant applied to experience store
   Check: `core/scripts/experience.py` `cmd_update_field` `validate_record(rec)` call appears BEFORE the `utility_ratio` recompute block (so direct writes to derived fields fail loud instead of being silently clobbered)
   Check: `core/scripts/pipeline.py` `cmd_update_field` calls `normalize_record(rec)` before the mutation block AND `validate_record(rec)` between mutation and `write_jsonl` — same guard-330 invariant applied to pipeline store
   Check: `core/scripts/pipeline.py` `cmd_update_field` `validate_record(rec)` call appears BEFORE the `reflected_date` auto-set (so bad user inputs are rejected before cascading derivations fire)
   Check: `core/scripts/pipeline.py` `cmd_move` calls `normalize_record(rec)` exactly once BEFORE the `if target_stage == "archived"` branch (not inside each branch). Both branches share the same record shape; duplicating the call was F4 in the 2026-04-19 fresh-eyes review. The hoisted site has a protective comment referencing that review — verify the comment is still present so a future dev does not push the call back into the branches.

   # Sibling write-path invariants (g-240-27, 2026-04-24)
   # Extends the guard-330 contract to the pipeline write paths that previously
   # bypassed validate_record / _update_meta_counts, plus the experience field-
   # update path that skipped _update_meta. F1/F2/F3 in the 2026-04-19 fresh-eyes
   # review. Landed after g-240-25 cleaned 51 legacy dirty pipeline records.
   Check: `core/scripts/pipeline.py` `cmd_move` calls `validate_record(rec)` AFTER `normalize_record(rec)` and BEFORE any `write_jsonl`/`append_jsonl`/`validate_formation_quality` — ensures the merge_data path can't bypass full-record validation (pre-fix: only formation_quality fired, structural drift went silent)
   Check: `core/scripts/pipeline.py` `cmd_archive_sweep` calls `validate_record(rec)` per-record AFTER `rec["stage"] = "archived"` and BEFORE the record is appended to `to_archive` — catches stage-mutation drift before it lands in the archive jsonl
   Check: `core/scripts/pipeline.py` `cmd_archive_sweep` validation error message contains `rec.get("id","?")` so a per-record failure points at the offending record without re-running the whole sweep
   Check: `core/scripts/pipeline.py` `cmd_update` calls `_update_meta_counts()` AFTER `write_jsonl(LIVE_PATH, items)` — keeps pipeline-meta.json counts consistent on full-record replace (parallel to cmd_move and cmd_archive_sweep)
   Check: `core/scripts/pipeline.py` `cmd_update_field` calls `_update_meta_counts()` AFTER `write_jsonl(target_path, items)` — keeps pipeline-meta.json counts consistent on field updates (parallel to cmd_move and cmd_archive_sweep)
   Check: `core/scripts/experience.py` `cmd_update_field` calls `_update_meta()` AFTER `write_jsonl(target_path, items)` — keeps experience-meta.json counts consistent on field updates (parallel to cmd_add and cmd_archive_sweep)

   Check: `core/scripts/reasoning-bank.py` + domain follow-ups: any new guardrail whose `rule` text contains universal quantifier tokens (`every`, `all`, `always`, `any`) has a source entry referencing rb-371 perimeter-audit lineage OR was added with an accompanying follow-up goal covering the enforcement perimeter (guard-336 invariant)

   Check: `core/scripts/utilization-feedback.py` `increment_supplementary` checks `result.returncode != 0` and prints stderr tail (silent swallow → rb-265 instance 2 returns)
   Check: `core/scripts/guardrail-check.py` CLI accepts `--type guardrail|reasoning-bank|both` (the symmetric rb counter feedback wire)
   Check: `core/scripts/guardrail-check.py` `check_guardrails` default `types=("guardrail",)` and rb matches are NOT returned in `matched` when both types requested (avoids mixed-type output breaking action_hint consumers)
   Check: `.claude/skills/aspirations-execute/SKILL.md` Phase 4.1 guardrail-check call passes `--type both`
   Check: `.claude/skills/aspirations-execute/SKILL.md` Phase 4.1 testing-context guardrail-check call passes `--type both`
   Check: `.claude/skills/aspirations-precheck/SKILL.md` Phase 0.5a guardrail-check call passes `--type both`
   Check: `.claude/skills/aspirations-execute/SKILL.md` has Step 5b.1 that writes `context_consulted.deliberation` via `pipeline-update-field.sh` when goal has a `hypothesis_id`
   Check: `core/config/memory-pipeline.yaml` `encoding_gate.category_class_multiplier` has entries for `framework-meta`, `ayoai-product`, `npc-domain` (plus neutral defaults)
   Check: `.claude/skills/reflect-on-outcome/SKILL.md` applies domain-class multiplier BEFORE the `If encoding_score >= 0.40` gate (not inside, not after precision bonus with a re-check)
   Check: `{META_DIR}/aspiration-generation-strategy.yaml` `category_saturation:` is populated (non-empty map); empty map means throttle is off
   Check: `{WORLD_DIR}/knowledge/tree/_tree.yaml` L1 nodes (`execution`, `intelligence`, `performance`, `system`) each have a `domain_class` field
   Check: `core/scripts/learning-ratio.sh` wrapper exists and sources `_paths.sh` + `_platform.sh` (matches core/scripts convention)
   Check: `core/scripts/learning-ratio.py` exists
   Check: `.claude/skills/boot/SKILL.md` Step 2 gather block invokes `bash core/scripts/learning-ratio.sh`
   Check: `.claude/skills/backlog-report/SKILL.md` header references `learning-ratio.sh` output
   **Runtime**: After 3+ goals with retrieval, `reasoning-bank-read.sh --active | jq '[.[] | select(.utilization.times_inferred_helpful > 0)] | length'` is non-zero (the --infer path is landing increments, not being silently rejected)
   **Runtime**: `bash core/scripts/learning-ratio.sh` prints a single line with four percentages summing to ~100% and targets inline
   **Runtime**: `bash core/scripts/guardrail-check.sh --context any --phase pre-selection --dry-run --type both` returns `matched` with only `"type": "guardrail"` entries (rb matches moved counters silently)

   # Tree maintenance debt invariants (Wave 1 + Option A, 2026-04-18)
   # Cross-ref: exp-tree-debt-wave1-2-2026-04-18, guard-157 (mirror anti-pattern), rb-256 (verify framework vs narration)
   # These checks enforce the single-source-of-truth architecture; failure means mirror drift has returned.
   Check: `.claude/skills/aspirations-consolidate/SKILL.md` queries BOTH `tree-read.sh --distill-candidates` AND `tree-read.sh --decompose-candidates` at session-end (the decompose query is NON-OPTIONAL — its absence hides compound backlogs)
   Check: `core/scripts/iteration-close.sh` `do_learning_gate` sets `force_tree_maintain` WM signal when decompose count > debt_threshold * 3 (consumed by aspirations-precheck to invoke `/tree maintain --backlog`; otherwise `/tree maintain` fires on cadence)
   # force_tree_maintain encoding-drift regression guard (Section TMD-DW — g-115-704,
   # after g-115-700 dual-write; SINGLE-write since g-115-1521)
   # The sentinel-bypass class found by g-115-700: tree-encoding-drift-gate.py wrote
   # force_tree_encoding (an LLM-only / cold-path sentinel) but NOT force_tree_maintain
   # (the precheck hot-path consumer). Result: the recurring-close shortcut path silently
   # bypassed tree maintenance — backlog grew while the gate fired hundreds of times.
   # g-115-700 first fixed this by DUAL-writing both sentinels. g-115-1521 then RETIRED the
   # force_tree_encoding set entirely: its only consumer (aspirations-state-update Step 8)
   # is on the cold path the loop bypasses, so the hot-path set was never cleared and the
   # stale-sentinel canary (g-115-717) fired at threshold 3. The writer now sets ONLY
   # force_tree_maintain (the hot-path-consumed sentinel) so the bash-driven consumer
   # (aspirations-precheck Phase 0-pre → /tree maintain --backlog) ALWAYS fires when the
   # threshold trips. DO NOT re-add a force_tree_encoding write here to "restore" the
   # dual-write — that re-introduces the orphan g-115-1521 removed. The check below pins
   # the surviving single-write contract.
   # Cross-ref: rb-911 (sentinel-bypass class), g-115-700 (dual-write), g-115-1521 (orphan
   # retired), 2026-05-13_force-tree-encoding-sentinel-stuck (hypothesis CONFIRMED).
   Check: `core/scripts/tree-encoding-drift-gate.py` sets `force_tree_maintain` WM slot (the SOLE sentinel set; force_tree_encoding retired g-115-1521) when threshold crosses — grep `slots\[.force_tree_maintain.\]` in the script must match
   Check: `.claude/skills/aspirations-precheck/SKILL.md` Phase 0-pre reads `force_tree_maintain` AND invokes `/tree maintain --backlog` in the same Phase block (consumer side)
   Bash (writer-side): test "$(grep -c 'force_tree_maintain' core/scripts/tree-encoding-drift-gate.py)" -ge 1 && echo "PASS: writer side (tree-encoding-drift-gate.py) sets force_tree_maintain" || echo "FAIL: writer regressed — force_tree_maintain not set in tree-encoding-drift-gate.py (g-115-700 dual-write reverted)"
   Bash (consumer-side): test "$(grep -c 'force_tree_maintain' .claude/skills/aspirations-precheck/SKILL.md)" -ge 1 && echo "PASS: consumer side (aspirations-precheck Phase 0-pre) reads force_tree_maintain" || echo "FAIL: consumer regressed — force_tree_maintain consumer missing from aspirations-precheck SKILL.md"
   Bash (consumer-fires-maintain): grep -A 12 'force_tree_maintain' .claude/skills/aspirations-precheck/SKILL.md | grep -q 'tree maintain --backlog' && echo "PASS: consumer invokes /tree maintain --backlog within force_tree_maintain block" || echo "FAIL: consumer reads sentinel but does not fire /tree maintain --backlog — bypass gap re-opened"

   # force_tree_maintain source-dispatch regression guard (Section TMD-SD — g-115-721, after g-115-700 dual-write)
   # g-115-700 dual-write fixed the bypass-orphan class (force_tree_encoding orphaned by recurring-close.sh path)
   # but had unintended consequence: encoding-drift threshold=3 made force_tree_maintain fire every 3 closes,
   # routing to /tree maintain --backlog (heavy debt-drain) instead of intent-appropriate lightweight encoding.
   # Observed iter-19/21/23 of bravo session 2026-05-14: 0-action /tree maintain passes accumulated, debt 145
   # stayed static, Maintain goals proliferated documenting noise (g-115-720, g-115-722). Fix: aspirations-
   # precheck Phase 0-pre dispatches on signal.source field — encoding-drift gets log-and-clear (no /tree
   # maintain), tree-debt-critical gets the heavy path. Source field is set by the writer (tree-encoding-drift-
   # gate.py writes source=encoding-drift; learning-gate writes source=tree-debt-critical or omits).
   # Cross-ref: g-115-721 (this fix), g-115-700 (dual-write origin), TMD-DW (regression guard one above).
   Check: `.claude/skills/aspirations-precheck/SKILL.md` Phase 0-pre reads `force_tree_maintain --json` (not bare `wm-read.sh force_tree_maintain`) so the source field is accessible to dispatch logic
   Check: `.claude/skills/aspirations-precheck/SKILL.md` Phase 0-pre has explicit `source == "encoding-drift"` branch that does NOT invoke `/tree maintain --backlog`
   Check: `.claude/skills/aspirations-precheck/SKILL.md` Phase 0-pre ELSE branch (tree-debt-critical or missing) DOES invoke `/tree maintain --backlog`
   Bash (json-read): grep -q 'force_tree_maintain --json' .claude/skills/aspirations-precheck/SKILL.md && echo "PASS: Phase 0-pre reads sentinel as --json (source field accessible)" || echo "FAIL: Phase 0-pre reads bare sentinel — source-dispatch broken, encoding-drift routes to heavy maintain"
   Bash (encoding-drift-light): grep -B 1 -A 6 'encoding-drift' .claude/skills/aspirations-precheck/SKILL.md | grep -q 'no /tree maintain invocation\|drift acknowledged' && echo "PASS: encoding-drift branch is lightweight" || echo "FAIL: encoding-drift branch missing or wrong action"
   Bash (tree-debt-heavy): grep -A 20 'ELSE:' .claude/skills/aspirations-precheck/SKILL.md | grep -q 'tree maintain --backlog' && echo "PASS: tree-debt-critical ELSE branch invokes /tree maintain --backlog" || echo "FAIL: ELSE branch lost the heavy path"

   # post-state-update-metric-gate regression guards (Section PMG-CG — g-115-728, g-115-724/rb-917 content-gate)
   # Content-gate sibling to the counter-gate family (TMD-DW, post-state-update-gate.sh, tree-encoding-drift-gate).
   # Catches "LLM did the encoding step on the WRONG content" rather than "LLM skipped it entirely".
   #
   # Canonical incident (g-115-707): alpha closed g-250-78 (Verify: AyoSeed timeout) with measurable
   # production metrics (jose intent 690->1245 1.8x, AjaxKey/RichmondKey 2x, BT failures 0 vs 69 baseline)
   # in outcome_note prose. verification:null. No bash gate inspected the content. Encoding lagged ~50min
   # until bravo's manual product-world refresh sweep caught it. The metric gate scans outcome_note +
   # verify summary for >=2 distinct numeric findings on deep outcomes, writes force_metric_encoding_pending
   # WM sentinel; aspirations-precheck Phase 0-pre4 consumes it as encoding work the LLM must perform
   # before goal selection. Cross-ref: rb-917 (content-vs-counter-gate decision rule), g-115-724 (Apply),
   # g-115-707 (Investigate root-cause), TMD-DW (sibling counter-gate above).
   Check: `core/scripts/post-state-update-metric-gate.sh` exists and is executable (wrapper for the content-scanning gate)
   Check: `core/scripts/iteration-close.sh` Step 8.79 invokes `post-state-update-metric-gate.sh` on `OUTCOME == "deep"` and stdin-pipes the SUMMARY (avoids argv quoting on multi-line prose)
   Check: `core/scripts/iteration-close.sh` Step 8.79 sets `force_metric_encoding_pending` WM slot via `wm-set.sh` with the full gate JSON payload (preserves candidates + candidate_node_key/file for the consumer)
   Check: `.claude/skills/aspirations-precheck/SKILL.md` Phase 0-pre4 reads `force_metric_encoding_pending --json` and clears via `echo 'null' | wm-set.sh` after dispatch
   Check: `core/tests/gates/test_post_state_update_metric_gate.py` exists (regression suite: canonical g-250-78 firing case, routine-noop, no-numbers-below-threshold cases)
   Bash (gate-script-presence): test -x core/scripts/post-state-update-metric-gate.sh && echo "PASS: gate script present + executable" || echo "FAIL: core/scripts/post-state-update-metric-gate.sh missing or not executable — Step 8.79 wiring fails silently via gate-error fallback"
   Bash (step-8.79-wiring): grep -q 'post-state-update-metric-gate.sh' core/scripts/iteration-close.sh && echo "PASS: Step 8.79 invokes gate" || echo "FAIL: Step 8.79 wiring lost from iteration-close.sh"
   Bash (step-8.79-deep-gated): grep -B 8 'post-state-update-metric-gate.sh' core/scripts/iteration-close.sh | grep -qE '\[\[ "\$OUTCOME" == "deep" \]\]' && echo "PASS: Step 8.79 gated on deep outcome" || echo "FAIL: Step 8.79 fires on all outcomes — routine closures would trigger encoding work spuriously"
   Bash (step-8.79-sentinel-write): grep -A 12 'post-state-update-metric-gate.sh' core/scripts/iteration-close.sh | grep -qE 'wm-set\.sh"? force_metric_encoding_pending' && echo "PASS: Step 8.79 writes force_metric_encoding_pending sentinel" || echo "FAIL: Step 8.79 calls gate but never sets the sentinel — consumer signal lost"
   Bash (consumer-side): grep -q 'force_metric_encoding_pending' .claude/skills/aspirations-precheck/SKILL.md && echo "PASS: Phase 0-pre4 consumer reads sentinel" || echo "FAIL: precheck Phase 0-pre4 consumer missing — gate writes signal nobody reads"
   Bash (consumer-clears): grep -A 50 'Phase 0-pre4' .claude/skills/aspirations-precheck/SKILL.md | grep -q "wm-set.sh force_metric_encoding_pending" && echo "PASS: Phase 0-pre4 clears sentinel after dispatch" || echo "FAIL: Phase 0-pre4 reads but never clears — sentinel sticks across iterations"
   Bash (tests-present): test -f core/tests/gates/test_post_state_update_metric_gate.py && echo "PASS: regression test file present" || echo "FAIL: core/tests/gates/test_post_state_update_metric_gate.py missing — no behavioral pinning on gate (orphaned .pyc bytecode suggests over-deletion casualty; file Investigate to restore)"
   Bash (tests-pass): test -f core/tests/gates/test_post_state_update_metric_gate.py && py -3 -m pytest core/tests/gates/test_post_state_update_metric_gate.py -o addopts= 2>&1 | tail -1 | grep -qE "passed|no tests ran" && echo "PASS: PMG regression suite green" || echo "FAIL: regression suite red or test file missing"

   Check: `.claude/skills/aspirations-consolidate/SKILL.md` queries both candidate types side-by-side (preserves compound-backlog visibility — distill-only would hide composite debt)
   Check: `.claude/skills/aspirations/SKILL.md` Phase 8.8 reads maintenance cadence via `tree-read.sh --maintenance` (NOT via `wm-read.sh` — the WM mirror was removed under Option A)
   Check: `.claude/skills/aspirations/SKILL.md` Phase -1.4 graceful-stop triggers on `stop-requested` signal only (written exclusively by `/stop`; budget pressure handled by zone-based soft degradation, not hard stop)

   # Stop-invariant + no-phantom-read invariants (session-49, 2026-04-18)
   # Cross-ref: guard-158, guard-159, guard-160, rb-262, rb-263
   Check: `.claude/skills/aspirations/SKILL.md` Phase -1.4 has exactly ONE entry condition — `IF stop_signal:` — no second branch that self-sets stop-requested
   Check: `.claude/skills/aspirations/SKILL.md` Phase -1.4 does NOT read `context-budget.json`, `used_pct`, or any budget field (budget pressure is never a stop trigger)
   Bash: grep -rn "session-signal-set.sh stop-requested\|session-signal-set stop-requested" .claude/skills core/scripts core/config 2>/dev/null | grep -v "verify-learning\|stop-hook-compliance\|/stop/SKILL.md" → MUST be empty (only /stop may set the signal)
   Bash: grep -rn "wm-set.*context_zone\|\"context_zone\":" .claude/skills core/scripts 2>/dev/null | grep -v "verify-learning\|reasoning-snapshot.py" → MUST be empty (no WM writer for context_zone; reasoning-snapshot writes to snapshot dict, not WM)
   Check: `.claude/skills/aspirations-execute/SKILL.md` episode chaining reads `zone` from `agents/<agent>/session/context-budget.json` DIRECTLY (not from `working-memory.yaml` — that field is phantom)
   Check: `.claude/skills/aspirations-execute/SKILL.md` episode chaining read has NO synthesize-default fallback (no `|| echo '{"zone":"normal"}'`); if budget file missing, loud-fail is the intended behavior
   Check: `core/scripts/goal-selector.py` does NOT define `read_context_budget` or `BUDGET_PATH`, and `score_goal` does NOT accept a `budget` parameter (cleanup after context-coherence zone modulation was removed)
   Check: `core/scripts/context-budget-status.py` `classify_zone` uses 40 and 85 as thresholds (tight at >=85); module docstring documents the rationale
   Check: `core/scripts/context-budget-status.py` `classify_zone` has a CRITICAL comment warning future devs not to lower the 85 boundary without profiling evidence
   Bash: grep -rn "budget_critical\|Wave 2 Change D\|budget-triggered" .claude/skills core/scripts core/config 2>/dev/null → MUST be empty (mechanism was reverted)
   Check: `core/scripts/tree.py` has `cmd_record_maintenance` function; argparser exposes `--record-maintenance` action flag and `--backlog-mode` modifier on the update subcommand
   Check: `core/scripts/tree.py` `cmd_record_maintenance` has NO fallback chain (no `try/except` swallow, no `... or {}` default on config read, no `os.path.exists` guard before open) — missing config must crash loudly
   Check: `core/scripts/tree.py` `cmd_record_maintenance` computes `last_backlog_clear_at` from post-run debt vs `cfg["tree_debt_check"]["debt_threshold"]` (auto-detects cleared state — callers must NOT pass a --cleared flag)
   Check: `core/scripts/tree.py` `cmd_read` handles `--maintenance` arg, printing `tree.get("maintenance") or {}` as JSON (single source of truth read path)
   Check: `core/scripts/tree-read.sh --maintenance` exits 0 and returns valid JSON (wrapper forwards to tree.py read --maintenance)
   Check: `.claude/skills/aspirations-consolidate/SKILL.md` Step 6 routes `stop_mode == true` to `/tree maintain --stop-mode` (small caps from `core/config/tree.yaml` `stop_mode_caps`) — symmetric with `core/config/consolidation-housekeeping.md` Step 6. No DEFERRABLE / deferral gate path exists; the pre-2026-05-08 `IF stop_mode AND debt<=ceiling AND zone==tight: defer` branch was removed under g-001-282 because it let candidates accumulate. Skipping Step 6 in any context is a guardrail violation.
   Check: `.claude/skills/aspirations-consolidate/SKILL.md` Step 6 auto-routes to `/tree maintain --backlog` when `debt > debt_threshold * 3`
   Check: `.claude/skills/tree/SKILL.md` /tree maintain pseudocode ends with a Record Maintenance step that invokes `tree.py update --record-maintenance` (and `--backlog-mode` in the --backlog path)
   Check: `.claude/skills/tree/SKILL.md` has ZERO `wm-set` or `wm-read` lines naming `last_tree_maintain_at` / `last_tree_backlog_at` / `last_tree_backlog_clear_at` (WM mirror fully removed)
   Check: `.claude/skills/tree/SKILL.md` --backlog caps reference `tree.yaml` as source of truth (not hard-coded 10/15) and note `max × 1.5` computation
   Bash: grep -rn "last_tree_maintain_at\|last_tree_backlog_at\|last_tree_backlog_clear_at" .claude/skills core/scripts core/config 2>/dev/null | grep -v "verify-learning" → MUST be empty (no WM mirror references outside this verification file)
   **Runtime**: After 1 `/tree maintain` invocation, `tree-read.sh --maintenance` returns JSON with non-null `last_maintain_at` ISO timestamp
   **Runtime**: After 1 `/tree maintain --backlog` invocation, `tree-read.sh --maintenance` also populates `last_backlog_mode_at`
   **Runtime**: After backlog drains below `debt_threshold`, `last_backlog_clear_at` populates (written by auto-detection inside cmd_record_maintenance)
   **Runtime**: Over 3–5 sessions post-ship, total debt (`--distill-candidates + --decompose-candidates`) trends downward from baseline 242 (live on 2026-04-18)

   # Multi-agent coordination evidence checks (Section MAC)
   # Cross-pollinated from "Language Model Teams as Distributed Systems" (arXiv 2603.12229)
   Check: `core/config/aspirations.yaml` has `multi_agent:` section with `claim_timeout_hours` and `reallocation_hours`
   Check: `goal-selector.py` `collect_candidates` has `global_done_ids` param (cross-aspiration dependency enforcement)
   Check: `goal-selector.py` `collect_blocked` has `global_done_ids` param (must match collect_candidates scope)
   Check: `goal-selector.py` `cmd_select` builds `global_done_ids` across both world+agent aspirations before calling collect_candidates
   Check: `goal-selector.py` `cmd_blocked` builds `global_done_ids` before calling collect_blocked (consistency with cmd_select)
   Check: `goal-selector.py` claim expiry: `hours_since(goal.get("claimed_at"))` returns None for missing claimed_at → fails open (goal becomes visible)
   Check: `board.py` `cmd_post` includes `type` field in message dict (structured coordination messages)
   Check: `board.py` `cmd_read` has `--type` filter in argparser
   Check: `board.py` `build_parser` post subparser has `--type` argument with default="status"
   Check: `board.py` `build_parser` read subparser has `--type` argument (no default — None when unspecified)
   Check: `aspirations.py` `validate_aspiration` checks `coordination_mode` against ("parallel", "serial", "mixed")
   Check: `aspirations.py` `validate_goal` checks `reallocatable` is boolean when present
   Check: `core/config/conventions/board.md` has "Message Types" section with type table
   Check: `core/config/conventions/board.md` has "Encoding Coordination Protocol" section
   Check: `core/config/conventions/aspirations.md` has "Claim Expiry" section
   Check: `core/config/conventions/aspirations.md` has "Cross-Aspiration Dependency Enforcement" section
   Check: `core/config/conventions/goal-schemas.md` has "Straggler-Aware Goal Reallocation" section with `reallocatable` field
   Check: `aspirations-state-update/SKILL.md` Step 8 has "ENCODING COORDINATION CHECK" block before the deep/standard branch
   Check: `create-aspiration/SKILL.md` Step 4a.5 has "COORDINATION MODE" classification step
   Check: `prime/SKILL.md` Phase 2 step 6 reads board with `--json` and parses typed messages
   Bash: python3 -c "import ast; ast.parse(open('core/scripts/goal-selector.py', encoding='utf-8').read()); print('OK')" → verify goal-selector.py parses
   Bash: python3 -c "import ast; ast.parse(open('core/scripts/board.py', encoding='utf-8').read()); print('OK')" → verify board.py parses

   # MAC9: Claim lifecycle shell wrappers and full-cycle TOCTOU-safe locking (guard-102)
   Check: `core/scripts/aspirations-claim.sh` exists and exec line contains `aspirations.py` and `claim`
   Check: `core/scripts/aspirations-release.sh` exists and exec line contains `aspirations.py` and `release`
   Check: `core/scripts/aspirations-complete-by.sh` exists and exec line contains `aspirations.py` and `complete-by`
   Check: ALL 10 write commands in `aspirations.py` use full-cycle locking (acquire_lock before read_jsonl, _write_live_under_lock for LIVE writes):
     cmd_add, cmd_update, cmd_update_goal, cmd_add_goal, cmd_complete, cmd_retire, cmd_archive_sweep, cmd_claim, cmd_release, cmd_complete_by
   Check: NO calls to `write_jsonl(LIVE_PATH` remain in `aspirations.py` (all LIVE writes use _write_live_under_lock under caller-held lock)
   Check: `_check_not_archived` called in cmd_update, cmd_update_goal, cmd_add_goal (archive cross-check prevents stale resurrection)
   Check: Lock ordering comment near file I/O helpers: "LIVE_PATH.lock first, ARCHIVE_PATH.lock second"

   # MAC10: Claim integration in orchestrator
   Check: `aspirations/SKILL.md` has "CLAIM + EXECUTE (Phase 4)" section with `aspirations-claim.sh` call
   Check: `aspirations/SKILL.md` has "ATTRIBUTION (Phase 5.3)" section with `aspirations-complete-by.sh` and `aspirations-release.sh`
   Check: `aspirations/SKILL.md` infrastructure_failure block has `aspirations-release.sh` call (world goals)

   # MAC11: Circuit breaker
   Check: `aspirations/SKILL.md` `session_signals` has `consecutive_goal_failures` and `last_failed_goal_id`
   Check: `aspirations/SKILL.md` has "CIRCUIT BREAKER (Phase 5.5)" section with `--type escalation` board post
   Check: `aspirations/SKILL.md` circuit breaker defers goal with `defer_reason` containing "Circuit breaker"

   # MAC12: Review gate
   Check: `aspirations/SKILL.md` has "REVIEW GATE (Phase 5.7)" section with `--type review-request` board post
   Check: `core/config/aspirations.yaml` `_common_fields` has `review_requested` and `review_completed` (both null default)
   Check: `core/config/conventions/goal-schemas.md` has "Review Gate Fields" section

   # MAC13: Board scan and coordination convention
   Check: `aspirations/SKILL.md` all-blocked path has "Step B0: Board scan" with `--type escalation` and `--type review-request`
   Check: `core/config/conventions/coordination.md` exists with "Claim Protocol", "Circuit Breaker", "Review Gate" sections
   Check: `aspirations/SKILL.md` front matter conventions list includes `coordination`
   Check: `boot/SKILL.md` front matter conventions list includes `coordination`
   Check: `CLAUDE.md` convention index includes `coordination.md`

   # MAC14: Session-end claim release
   Check: `aspirations-consolidate/SKILL.md` has Step 8.9 with `aspirations-release.sh` for held claims

   # MAC15: Shared team state
   Check: `core/scripts/team-state.py` exists with read, update, init subcommands
   Check: `core/scripts/team-state-update.sh` exists (shell wrapper)
   Check: `core/scripts/team-state-read.sh` exists (shell wrapper)
   Check: `core/scripts/team-state-init.sh` exists (shell wrapper)
   Check: `core/config/conventions/coordination.md` has "Team State Protocol" section
   Check: `boot/SKILL.md` has Step 1.7 reading team-state.yaml
   Check: `aspirations-state-update/SKILL.md` has Step 3.5 updating team state on goal completion
   Check: `aspirations-consolidate/SKILL.md` has Step 8.87 updating team state at session end
   Check: `CLAUDE.md` core systems table includes "Team state" row
   Bash: bash core/scripts/team-state-read.sh --json 2>/dev/null → verify returns valid JSON or empty state

   # MAC16: Directive protocol (cross-agent priority influence)
   Check: `core/config/conventions/board.md` has "Directive Payload Schema" section
   Check: `core/config/conventions/coordination.md` has "Directive Protocol" section
   Check: `core/scripts/goal-selector.py` has `load_active_directives()` function
   Check: `core/scripts/goal-selector.py` has `directive_boost_score()` function
   Check: `core/scripts/goal-selector.py` `score_goal()` sets `raw["directive_boost"]`
   Bash: python3 -c "import ast; tree=ast.parse(open('core/scripts/goal-selector.py').read()); funcs=[n.name for n in ast.walk(tree) if isinstance(n,ast.FunctionDef)]; assert 'load_active_directives' in funcs and 'directive_boost_score' in funcs; print('PASS: directive functions exist')"
   Check: `aspirations-select/SKILL.md` has "Phase 2.07: Directive & Insight Trigger Scan"
   Check: `core/config/meta.yaml` initial_state.goal_selection_strategy.weights has `directive_boost: 1.5`

   # MAC17: Execution feedback loop
   Check: `core/config/conventions/board.md` has "Execution Feedback Schema" section
   Check: `board.py` VALID_MESSAGE_TYPES includes `execution-feedback`
   Check: `aspirations-state-update/SKILL.md` has Step 8.11 "Execution Feedback"
   Check: `create-aspiration/SKILL.md` has Step 1.5 "Execution Feedback Review"
   Check: `create-aspiration/SKILL.md` has Step 1.7 "Team State Alignment"

   # MAC18: Cross-agent insight triggers
   Check: `core/config/conventions/board.md` has "Insight Trigger Payload" section
   Check: `aspirations-execute/SKILL.md` Cognitive Primitives has "Cross-Agent Insight Goals" section
   Check: `aspirations-select/SKILL.md` Phase 2.07 has insight trigger scan with severity parsing
   Check: `aspirations-execute/SKILL.md` mentions "all four" cognitive primitives (not "all three")

   # MAC19: Directive boost scoring integration
   Bash: goal-selector.sh 2>/dev/null | python3 -c "
   import sys,json
   d=json.load(sys.stdin)
   if isinstance(d,list) and len(d) > 0:
       r = d[0]
       assert 'directive_boost' in r.get('breakdown',{}), 'directive_boost missing from breakdown'
       assert 'directive_boost' in r.get('raw',{}), 'directive_boost missing from raw'
       print(f'PASS: directive_boost in scoring (raw={r[\"raw\"][\"directive_boost\"]}, weighted={r[\"breakdown\"][\"directive_boost\"]})')
   elif isinstance(d,dict):
       print('PASS: all_blocked — cannot verify scoring fields')
   else:
       print('PASS: no goals to score')
   " → verify directive_boost appears in goal scoring output

   # MAC20: Status aggregator + partner-liveness-gated handoff decay
   # Artifacts: core/scripts/status.py, core/scripts/status.sh; edits in
   # core/config/aspirations.yaml (3 new keys), core/scripts/goal-selector.py
   # (load_handoff_config, _load_team_state_cached, sender-penalty block).
   # Lineage: rb-284 (age-only decay, superseded) → rb-324 (partner-silence
   # gating with fail-open) → rb-338 (semantic-flip review discipline).
   Check: `core/scripts/status.sh` exists (10-line wrapper that `exec`s status.py)
   Check: `core/scripts/status.py` exists and exposes `build_status()`, `render_pretty()`, `render_field()`
   Bash: bash core/scripts/status.sh --json | python3 -c "import sys,json; s=json.load(sys.stdin); assert 'session' in s and 'handoffs_inbound' in s and 'pipeline' in s and 'context' in s; h=s['handoffs_inbound']; assert 'ids' in h and 'top' in h, f'handoffs_inbound missing ids/top: {list(h.keys())}'; assert all('id' in p and 'title' in p for p in h.get('top', [])), 'top entries must carry id+title (consumed by boot/SKILL.md)'; print('PASS: status.sh --json schema intact')"
   Bash: MIND_AGENT= bash core/scripts/status.sh | grep -q "NO_AGENT" && echo "PASS: NO_AGENT graceful output" || echo "FAIL: status.sh should print NO_AGENT when unbound"
   # Boot must not re-implement the handoff scan — it consumes status.sh --field handoffs_inbound.
   # This prevents the old 45-line inline Python block (pre-2026-04-19) from reappearing.
   Check: `.claude/skills/boot/SKILL.md` invokes `status.sh --field handoffs_inbound` for pending-handoffs rendering
   Bash: grep -q "status.sh --field handoffs_inbound" .claude/skills/boot/SKILL.md && echo "PASS: boot consumes status.sh" || echo "FAIL: boot/SKILL.md must call status.sh --field handoffs_inbound (do NOT re-add the inline scan)"
   # Config: three new keys MUST be present and non-empty in aspirations.yaml
   Check: `core/config/aspirations.yaml` has `scoring.handoff_sender_penalty` (numeric, default -2.5)
   Check: `core/config/aspirations.yaml` has `handoff_aging.sender_decay_hours` (numeric, default 4)
   Check: `core/config/aspirations.yaml` has `handoff_aging.partner_active_threshold_min` (numeric, default 30)
   # Selector wiring: load_handoff_config must load all 6 keys (3 old + 3 new).
   # MIND_AGENT is required because goal-selector.py's imports reach wm.py
   # which resolves AGENT_DIR at module load.
   Bash: python3 -c "import sys; sys.path.insert(0,'core/scripts'); import importlib.util; spec=importlib.util.spec_from_file_location('gs','core/scripts/goal-selector.py'); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); cfg=m.load_handoff_config(); needed={'handoff_bonus','handoff_sender_penalty','warn_hours','escalate_hours','sender_decay_hours','partner_active_threshold_min'}; missing=needed-set(cfg); assert not missing, f'Missing keys: {missing}'; print('PASS: load_handoff_config has all 6 keys')"
   # INVARIANT anchors: fail-open branch + clamp must keep their DO-NOT comments
   Bash: grep -q "INVARIANT — DO NOT REPLACE WITH" core/scripts/goal-selector.py && echo "PASS: fail-open INVARIANT anchor intact" || echo "FAIL: fail-open INVARIANT anchor removed from goal-selector.py"
   Bash: grep -q "INVARIANT — DO NOT REMOVE THE min() CLAMP" core/scripts/goal-selector.py && echo "PASS: min() clamp INVARIANT intact" || echo "FAIL: clamp INVARIANT anchor removed from goal-selector.py"
   # Team-state loader must NOT have defensive swallows (rb-338 lesson)
   Check: `core/scripts/goal-selector.py` `_load_team_state_cached` does NOT contain `except Exception` (fail-visibly on corrupt yaml)
   Check: `core/scripts/goal-selector.py` `_load_team_state_cached` does NOT contain `isinstance(data, dict)` coercion (single source of truth)

   # STRUCT-PC: Structured goal preconditions — predicate library + selector/execute/precheck wiring
   # Artifacts: core/scripts/predicate.py, core/scripts/predicate-eval.sh,
   # core/config/conventions/preconditions.md; edits in goal-selector.py, aspirations-execute,
   # aspirations-precheck, aspirations-select, goal-schemas.md.
   Check: `core/scripts/predicate.py` exists and defines `PREDICATE_TYPES` dict
   # Vocabulary growth is allowed (v1 shipped 3, has since grown to include
   # file_check + metric_threshold). Shrinkage is not — all three v1 types
   # must remain to preserve backward-compat for existing goal records.
   Check: `core/scripts/predicate.py` PREDICATE_TYPES keys are a SUPERSET of `{file_exists_after, command_succeeds, goal_completed_after}` (v1 vocabulary preserved)
   Check: `core/scripts/predicate.py` `evaluate()` wraps handler in try/except and returns a PredicateResult on any exception (never raises)
   Check: `core/scripts/predicate.py` imports `resolve_file_path` from `_paths` (REQUIRED — guard-132)
   Check: `core/scripts/predicate.py` `_eval_file_exists_after` calls `resolve_file_path(path_pattern)` — NEVER `PROJECT_ROOT / path`
   Check: `core/scripts/predicate.py` `resolve_after_ref` file: branch calls `resolve_file_path(ref)` — NEVER `PROJECT_ROOT / ref`
   Check: `core/scripts/predicate.py` `_eval_command_succeeds` keeps `shell=True` (guard-133 — switching to shell=False breaks the python3 shim)
   Check: `core/scripts/predicate.py` allowlist prefixes are exactly `("bash core/scripts/", "bash world/scripts/")`
   Check: `core/scripts/predicate.py` command_succeeds uses `shlex.quote(script_abs)` when rewriting `bash world/...` paths (spaces-in-WORLD_DIR safety)
   Check: `core/scripts/predicate.py` has `_to_local_naive(dt)` helper that uses `.astimezone().replace(tzinfo=None)` (not a bare `.replace(tzinfo=None)`)
   Check: `core/scripts/predicate-eval.sh` sources both `_paths.sh` AND `_platform.sh` and `cd "$PROJECT_ROOT"` before `exec python3`
   Check: `core/config/conventions/preconditions.md` exists and documents the three v1 types + after_ref grammar + auto-clear flow
   Check: `core/config/conventions/goal-schemas.md` `verification.preconditions` section documents BOTH string (LLM) and dict-with-type (selector) forms and links to preconditions.md

   # STRUCT-PC: selector integration must be load-bearing (not a silent fallback)
   Check: `core/scripts/goal-selector.py` `collect_candidates` has a block under "Structured preconditions" that imports `predicate.evaluate_all` WITHOUT try/except ImportError (guard-133 anti-pattern — fail loud)
   Check: `core/scripts/goal-selector.py` `collect_blocked` has a matching block (SYMMETRY comment) that adds `block_reason = "precondition_unmet"` to the entry
   Check: `core/scripts/goal-selector.py` struct_pc filter uses `include_skippable=False` (honors `selector_skip: true`)
   Check: `core/scripts/goal-selector.py` DOES NOT write `goal["_precondition_unmet"] = ...` (dead mutation; rejected goals never enter results)

   # STRUCT-PC: skill wiring
   Check: `.claude/skills/aspirations-execute/SKILL.md` Phase 4 Preamble has a "Pre-Claim Structured Precondition Re-Check" subsection that calls `predicate-eval.sh --goal {goal.id}` and defers + releases the claim on exit 1
   Check: `.claude/skills/aspirations-execute/SKILL.md` pre-claim re-check writes `defer_reason "precondition_unmet:..."` and `defer_reason_set_at` via `aspirations-update-goal.sh` — NOT CREATE_BLOCKER (preconditions and blockers are distinct)
   Check: `.claude/skills/aspirations-precheck/SKILL.md` has a Phase 0.5b.3 "Structured Precondition Auto-Clear Sweep" that iterates goals with `defer_reason` starting with `"precondition_unmet:"`, re-runs predicate-eval.sh, and nulls both `defer_reason` and `defer_reason_set_at` on exit 0
   Check: `.claude/skills/aspirations-select/SKILL.md` Precondition Gate is narrowed to `string_pcs = [p for p in ... if isinstance(p, str)]` — structured dict preconditions are explicitly noted as "already filtered by goal-selector.py COLLECT"

   # STRUCT-PC: end-to-end smoke (runs a minimal round-trip)
   Bash: bash core/scripts/predicate-eval.sh --predicate '{"type":"nonsense","id":"pc-x"}'; [ $? -eq 1 ] && echo "PASS: unknown type returns exit 1" || echo "FAIL: unknown type"
   Bash: bash core/scripts/predicate-eval.sh --predicate '{"type":"command_succeeds","id":"pc-bad","command":"ls /tmp"}'; [ $? -eq 1 ] && echo "PASS: allowlist rejection" || echo "FAIL: allowlist rejection"
   Bash: bash core/scripts/predicate-eval.sh --predicate '{"type":"command_succeeds","id":"pc-ok","command":"bash core/scripts/session-state-get.sh"}'; [ $? -eq 0 ] && echo "PASS: allowlisted command (python3 shim chain intact)" || echo "FAIL: shim broken — probably switched to shell=False"
   Bash: bash core/scripts/predicate-eval.sh --predicate '{"type":"file_exists_after","id":"pc-world","path":"world/knowledge/tree/_tree.yaml","after_ref":"git:HEAD~10"}'; [ $? -eq 0 ] && echo "PASS: world/ path resolves to external WORLD_DIR" || echo "FAIL: world/ prefix not resolving — probably lost resolve_file_path"

   # GREP-P-HYGIENE: perl-regex grep is banned in framework checks (rb-474 / guard-418)
   # On MSYS Git Bash (Windows default shell) grep -P exits non-zero on locale errors,
   # which silently flips opposite-polarity regression checks to always-PASS (rb-462 shape).
   # Use grep -E / grep -F / plain basic regex, or inline py -3 -c re.search when needed.
   Check: Framework-wide — no Bash check or framework script uses `grep -P` (this SKILL.md itself is excluded since the rb-474 documentation block references the literal string). Bash: matches=$(grep -rn "grep -P" .claude/skills/ core/scripts/ 2>/dev/null | grep -v "verify-learning/SKILL.md" | wc -l); [ "$matches" -eq 0 ] && echo "PASS: no grep -P in framework" || echo "FAIL: $matches grep -P usage(s) — see rb-474"

   # IMPORT-FALLBACK-HYGIENE: silent same-directory import fallbacks (rb-469 / guard-391)
   # `try: from _sibling import X / except ImportError: silent_fallback` in core/scripts/ is
   # dead code in normal deployment AND produces rb-466-shape (silent None makes framework
   # breakage indistinguishable from legitimate no-work). 5 sites swept 2026-04-23:
   Check: `core/scripts/aspirations.py` `_log_unstructured_defer_override` has no silent import-fallback. Bash: grep -A 3 "^def _log_unstructured_defer_override" core/scripts/aspirations.py | grep -q "except ImportError" && echo FAIL || echo PASS
   Check: `core/scripts/aspirations.py` `_work_class` import in cmd_add_goal is bare (no wrapping try/except — _work_class.resolve is fail-open internally). Bash: grep -B 1 "from _work_class import resolve as _resolve_work_class" core/scripts/aspirations.py | grep -q "try:" && echo FAIL || echo PASS
   Check: `core/scripts/checks-backfill.py` imports `WORLD_DIR` at module-level alongside AGENT_DIR / PROJECT_ROOT (single source of truth — no in-function shadow). Bash: head -35 core/scripts/checks-backfill.py | grep -E "^from _paths import " | grep -q WORLD_DIR && echo PASS || echo FAIL
   Check: `core/scripts/quiescence-gate.py` `_append_log` has no silent import-fallback. Bash: grep -A 5 "^def _append_log" core/scripts/quiescence-gate.py | grep -q "except ImportError" && echo FAIL || echo PASS
   Check: `core/scripts/obligation-audit.py` does not wrap `_paths` import in try/except (bare import; _paths itself returns AGENT_DIR=None when no agent bound). Bash: grep -B 1 "^from _paths import AGENT_DIR" core/scripts/obligation-audit.py | grep -q "try:" && echo FAIL || echo PASS
   Check: `core/scripts/iteration-close.sh` embedded python `_work_class` import is bare (no silent fail-open lambda — crash surfaces SCRIPT_DIR bug, old lambda was rb-431 bias source). Bash: grep -B 2 "from _work_class import resolve as _resolve_wc" core/scripts/iteration-close.sh | grep -q "try:" && echo FAIL || echo PASS

   # PATH-SSOT: single-source-of-truth for path resolution (rb-391 / guard-345)
   # The tree line-count silent-failure fix (2026-04-20) split responsibilities:
   # writer-side normalize_virtual_path canonicalizes bare .md paths before storage;
   # reader-side resolve_file_path has NO symmetric fallback. Both halves must stay.
   # Adding a .md branch back to resolve_file_path is an anti-pattern — it masks
   # writer bugs and reintroduces the silent line_count=0 symptom.
   Check: `core/scripts/_paths.py` defines `_longpath_safe(p: Path) -> Path` that returns `p` unchanged when `os.name != "nt"` and wraps paths >= 260 chars with `\\?\` on Windows
   Check: `core/scripts/_paths.py` `resolve_file_path` has EXACTLY three branches (world/, meta/, else→PROJECT_ROOT) — no `.md` fallback and no `knowledge/tree/` recovery branch
   Check: `core/scripts/_paths.py` `resolve_file_path` wraps each branch return through `_longpath_safe(...)` (uniform long-path safety)
   Check: `core/scripts/tree.py` `normalize_virtual_path` has a branch that prepends `world/knowledge/tree/` when the path ends with `.md` and does NOT start with `world/` or `meta/` (canonicalization authority)
   Bash: py -3 -c "import sys; sys.path.insert(0,'core/scripts'); from tree import normalize_virtual_path as n; assert n('intelligence/foo.md') == 'world/knowledge/tree/intelligence/foo.md'; assert n('world/knowledge/tree/bar.md') == 'world/knowledge/tree/bar.md'; print('PASS: normalize canonicalizes bare .md; leaves prefixed paths untouched')"
   Bash: py -3 -c "import sys,os; sys.path.insert(0,'core/scripts'); from _paths import resolve_file_path, PROJECT_ROOT; p = resolve_file_path('intelligence/foo.md'); assert str(p).startswith(str(PROJECT_ROOT)) and 'knowledge' not in str(p).split(str(PROJECT_ROOT),1)[1], f'reader added a fallback: {p}'; print('PASS: reader has no .md fallback — bare paths route to PROJECT_ROOT and fail visibly')"
   Bash: MIND_AGENT=alpha bash core/scripts/_paths.sh >/dev/null 2>&1; source core/scripts/_paths.sh && export WORLD_DIR && py -3 -c "import os,yaml; t=yaml.safe_load(open(f'{os.environ[\"WORLD_DIR\"]}/knowledge/tree/_tree.yaml')); bare=[n['file'] for n in t.get('nodes',{}).values() if isinstance(n,dict) and n.get('file') and not (n['file'].startswith('world/') or n['file'].startswith('meta/'))]; assert not bare, f'bare paths leaked into _tree.yaml: {bare}'; print('PASS: _tree.yaml has zero bare-path file: fields')"

   # STRUCT-PC: no regression in existing selector behaviour
   Bash: goal-selector.sh 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print('PASS: selector returns ranked list' if isinstance(d,(list,dict)) else 'FAIL')"
   Bash: goal-selector.sh blocked 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'blocked_goals' in d; print('PASS: blocked diagnostic shape intact')"

   # Magic-Wand Session 48 evidence checks (Section MW)
   # Covers the four-item framework correction shipped 2026-04-18:
   # (1) claim clearing, (2) orchestrator slim, (3) maintenance cadence,
   # (4) audit/locator gates. Also covers the three extraction invariants
   # (rb-252/253/254, guard-153/154/155).

   # MW-Item-2: Sub-skill extraction (aspirations-all-blocked, aspirations-graceful-stop)
   Check: `.claude/skills/aspirations-all-blocked/SKILL.md` exists with `user-invocable: false` and `minimum_mode: autonomous` in front matter
   Check: `.claude/skills/aspirations-graceful-stop/SKILL.md` exists with `user-invocable: false` and `minimum_mode: autonomous` in front matter
   Check: `.claude/skills/aspirations/SKILL.md` Phase -1.4 invokes `Skill(aspirations-graceful-stop)` + RETURN (delegation pattern, not inline D1-D7 body)
   Check: `core/config/aspirations-loop-digest.md` all-blocked branch invokes `Skill(aspirations-all-blocked)` AND is followed by an explicit `RETURN` (arrow notation `→ RETURN` on same line counts) — without it, control falls through and silently defeats the backoff sleep (rb-252, guard-153 invariant). Moved to digest in commit 05ac4ae during extraction refactor.
   Bash (return-contract): grep -E "Skill\(aspirations-all-blocked\).*RETURN" core/config/aspirations-loop-digest.md | grep -q . && echo "PASS: invocation + RETURN present on same line" || echo "FAIL: missing RETURN — yield contract violation"

   # MW-Item-2: Read-merge-write loop_state persistence for the genuinely
   # LLM-owned all-blocked fields (rb-253, guard-154). g-115-1561 moved the
   # evolution accumulators to bash ownership, so B3 no longer read-merge-writes
   # (a bare LOOP_CONTINUE is correct there — aspirations-evolve's
   # --evolution-fired is the single writer). The remaining read-merge-writes
   # persist idle_fallback_created (B2.5) and consecutive_blocked_sleeps (B7),
   # which have no bash writer.
   Check: `.claude/skills/aspirations-all-blocked/SKILL.md` Step B2.5 read-merge-writes `idle_fallback_created` (`wm-read.sh loop_state` → overlay → `wm-set.sh loop_state`) BEFORE its LOOP_CONTINUE — a bare overwrite would clobber bash-gate counters
   Check: `.claude/skills/aspirations-all-blocked/SKILL.md` Step B7 has the same read-merge-write pattern BEFORE the `interruptible-sleep.sh run_in_background=true` RETURN — without it, `session_signals.consecutive_blocked_sleeps` never escalates past 0 (sleep stuck at 300s forever)
   Bash (loop-state-persistence-all-blocked): grep -c "wm-read.sh loop_state" .claude/skills/aspirations-all-blocked/SKILL.md | awk '{print ($1 >= 2) ? "PASS: loop_state read-merge-write present (B2.5 idle_fallback + B7 backoff)" : "FAIL: missing read-merge-write in sub-skill"}'

   # g-115-1072: learning-gate LOOP_CONTINUE read-merge-write (the MAIN per-iteration
   # loop_state persistence — runs on every non-recurring close). g-283 retired the
   # per-iteration mirror but left THIS as a bare overwrite that reverted the bash-gate
   # counter increments (goals_completed, productive_goals, counted_goals_this_session) —
   # slot-specific, so siblings like pending_phase_6_spark survived (the diagnostic signature).
   # Same proven fix as all-blocked B3 above.
   Check: `.claude/skills/aspirations-learning-gate/SKILL.md` LOOP_CONTINUE has an explicit `wm-read.sh loop_state --json` → overlay → `wm-set.sh loop_state` block BEFORE `Skill('aspirations') with args='loop'` — a bare `wm-set.sh loop_state` from stale orchestrator variables clobbers bash-gate-owned counters
   Bash (loop-state-persistence-learning-gate): grep -c "wm-read.sh loop_state" .claude/skills/aspirations-learning-gate/SKILL.md | awk '{print ($1 >= 1) ? "PASS: learning-gate LOOP_CONTINUE read-merge-write present (g-115-1072)" : "FAIL: bare overwrite clobbers bash-gate counters (g-115-1072 regression)"}'

   # MW-Item-1: Phase 8.8 Maintenance Tick
   # g-246-01 classified MW-Item-1 as MOVED (Phase 8.8 lives in the digest, not
   # SKILL.md per the extraction refactor). g-246-02 wired the concrete readers.
   # Checks now target core/config/aspirations-loop-digest.md.
   Check: `core/config/aspirations-loop-digest.md` contains `Phase 8.8` MAINTENANCE TICK block between Phase 8.7 and Phase 9
   Check: `core/config/aspirations.yaml` has a `maintenance_cadence` section with keys `tree_maintain` (hours_cadence, debt_floor, tight_zone_skip), `evolution` (hours_cadence, tight_zone_skip), `full_cycle_reflection` (hours_cadence, tight_zone_skip)
   Check: `maintenance_cadence.tree_maintain.tight_zone_skip` is `false` (mid-loop tree maintenance runs even in tight zone — debt backlog is structurally critical).
   Check: `core/config/aspirations.yaml` `maintenance_cadence.tree_maintain` has NO `consolidation_defer_ceiling` key — the stop-flow deferral mechanism was removed under g-001-282 (FAST and FULL consolidation paths now both invoke `/tree maintain --stop-mode` under stop_mode=true; small caps from `stop_mode_caps` keep /stop fast without deferring).
   Check: `maintenance_cadence.evolution.tight_zone_skip` is `true` (evolution is elective in tight zone)
   Bash (config-present): grep -A 1 "maintenance_cadence:" core/config/aspirations.yaml | grep -q "tree_maintain" && echo "PASS: maintenance_cadence config present" || echo "FAIL"

   # MW-Item-1: Single-writer principle for cadence timestamps (rb-254, guard-155)
   # Tree cadence source of truth: _tree.yaml.maintenance.last_maintain_at
   # written by tree-update.sh --record-maintenance. Explicit architectural
   # decision (see tree/SKILL.md Sub-Command /tree maintain step 9): DO NOT
   # mirror to WM — dual-source creates divergence risk across crash boundaries.
   # Evolution cadence source of truth: <agent> WM slot last_evolution_at_time
   # written by aspirations-evolve/SKILL.md Maintenance Cadence Write section.
   Bash (no-wm-mirror-for-tree-maintain): grep -rnE --exclude-dir=verify-learning "bash core/scripts/wm-set\.sh last_tree_maintain_at" .claude/skills core/scripts 2>/dev/null | grep -v "^[^:]*:[[:space:]]*#" | wc -l | awk '{print ($1 == 0) ? "PASS: zero WM writers for last_tree_maintain_at (tree-yaml is sole source)" : "FAIL: "$1" WM writers exist — delete them; _tree.yaml.maintenance.last_maintain_at is the only source"}'
   Bash (phase-8-8-reads-tree-yaml): grep -q "tree-read.sh --maintenance" core/config/aspirations-loop-digest.md && echo "PASS: Phase 8.8 reads _tree.yaml via tree-read.sh --maintenance (correct source)" || echo "FAIL: Phase 8.8 reads wrong source for tree cadence"
   Bash (phase-8-8-reads-evolution-cadence): grep -q "wm-read.sh last_evolution_at_time" core/config/aspirations-loop-digest.md && echo "PASS: Phase 8.8 reads last_evolution_at_time (live reader exists)" || echo "FAIL: last_evolution_at_time has no reader in digest — dead writer pattern"
   Bash (single-writer-evolution): grep -rnE --exclude-dir=verify-learning "bash core/scripts/wm-set\.sh last_evolution_at_time" .claude/skills core/scripts 2>/dev/null | grep -v "^[^:]*:[[:space:]]*#" | wc -l | awk '{print ($1 == 1) ? "PASS: single writer for last_evolution_at_time" : "FAIL: "$1" writers (expected 1; matches bash-core-scripts invocation only, excludes backtick prose)"}'
   Check: The one writer for `last_evolution_at_time` is `.claude/skills/aspirations-evolve/SKILL.md` (Maintenance Cadence Write section)
   # RESOLVED (session 51 — asp-246 g-246-01 + g-246-02, 2026-04-20):
   # CLASSIFIED as MOVED+wired: Phase 8.8 lives in
   # core/config/aspirations-loop-digest.md (extracted from SKILL.md per
   # digest refactor). Before g-246-02 the block had abstracted pseudocode
   # ("tree cadence" without a concrete reader); g-246-02 added explicit
   # tree-read.sh --maintenance and wm-read.sh last_evolution_at_time
   # calls inside the digest's Phase 8.8. The dead-writer pattern at
   # aspirations-evolve (last_evolution_at_time with no reader) is now
   # healed — reader exists in the digest.
   # MW-Item-1 checks above target the digest, not SKILL.md.
   Check: `.claude/skills/tree/SKILL.md` Sub-Command `/tree maintain` step 9 has the "Do NOT add a working-memory mirror" note explaining the single-source-of-truth design

   # MW-Item-3: audit-schema-gate (rb-245 target)
   Check: `core/scripts/audit-schema-gate.py` exists and is executable via the `.sh` wrapper
   Check: `core/scripts/audit-schema-gate.sh` exists and sources `_paths.sh`
   Check: `audit-schema-gate.py` supports `--field-names` with comma-separated dotted paths (e.g., `utilization.times_active`)
   Check: `_get_dotted` in `audit-schema-gate.py` has a `DO NOT CHANGE` comment tying null-as-absent semantics to rb-245 (prevents future "simplification" from re-opening the bug)
   Bash (gate-blocks-iter-51-target): bash core/scripts/audit-schema-gate.sh --jsonl-path "$(cat agents/bravo/local-paths.conf | grep ^WORLD_PATH | cut -d= -f2)/guardrails.jsonl" --field-names "times_triggered,utilization.times_active" >/dev/null 2>&1; [ $? -eq 1 ] && echo "PASS: gate blocks rb-245 target (times_triggered absent)" || echo "FAIL: gate did not block"  <!-- DRIFT-EXEMPT: test-input -->
   Bash (override-passes): bash core/scripts/audit-schema-gate.sh --jsonl-path "$(cat agents/bravo/local-paths.conf | grep ^WORLD_PATH | cut -d= -f2)/guardrails.jsonl" --field-names "times_triggered" --override "intentional deprecation audit" >/dev/null 2>/dev/null; [ $? -eq 0 ] && echo "PASS: override opens gate with stderr audit" || echo "FAIL"  <!-- DRIFT-EXEMPT: test-input -->
   Check: `core/config/conventions/audit-before-concluding.md` exists with gate contract + rationale

   # MW-Item-3: encode-stable-facts-gate
   Check: `core/scripts/encode-stable-facts-gate.py` exists and is executable via the `.sh` wrapper
   Check: `encode-stable-facts-gate.py` defaults `--threshold` to `3` (three-probe rule from `.claude/rules/encode-stable-facts.md`)
   Bash (below-threshold-passes): bash core/scripts/encode-stable-facts-gate.sh --resource-id test-resource --probe-count 2 >/dev/null 2>&1; [ $? -eq 0 ] && echo "PASS: probe_count 2 below threshold — pass" || echo "FAIL"
   Bash (above-threshold-blocks): bash core/scripts/encode-stable-facts-gate.sh --resource-id nonexistent-resource-xyz-magic --probe-count 3 >/dev/null 2>&1; [ $? -eq 1 ] && echo "PASS: probe_count 3 no-locator blocks" || echo "FAIL"

   # MW-Item-4: claim-clearing invariant (guard-151)
   # Both cmd_complete_by AND cmd_update_goal must pop claimed_by/claimed_at
   # on ALL terminal-status transitions. Checked at both enforcement sites
   # so future edits in EITHER path trip the invariant.
   Bash (complete-by-pops): grep -A 6 'def cmd_complete_by' core/scripts/aspirations.py | grep -q 'claimed_by' && echo "PASS: cmd_complete_by pops claimed_by" || echo "FAIL: guard-151 invariant missing in cmd_complete_by"
   Bash (update-goal-pops): grep -A 10 '_clear_stale_blockers' core/scripts/aspirations.py | grep -q 'claimed_by' && echo "PASS: cmd_update_goal terminal hook pops claimed_by" || echo "FAIL: guard-151 invariant missing in cmd_update_goal"

   # Config override layer — writer + reader wiring (Section OVL — rb-335, guard-309/310/311)
   # The override layer lets agents self-tune bounded config params. Source is
   # core/config/<file>.yaml; overrides live in meta/config-overrides.yaml; each
   # reader MUST consume both via an overlay function. Key convention is
   # "<file-stem>.<in-file-dotted-path>". Checks below guard the wired readers
   # and the file-prefix convention against regression.
   Check: `core/scripts/tree.py` defines `_merged_config` (override-layer reader). Grep `^def _merged_config` must match.
   Check: `core/scripts/tree.py::_config_threshold` calls `_merged_config` (not raw `yaml.safe_load`). Grep `_merged_config\(\)\["config"\]\["decompose_threshold"\]` must match.
   Check: `core/scripts/tree.py::_config_d_max` calls `_merged_config`. Grep `_merged_config\(\)\["config"\]\["D_max"\]` must match.
   Check: `core/scripts/tree.py` filters by file-prefix. Grep `_OVERRIDE_FILE_PREFIX = "tree\."` must match — the file-prefix convention is load-bearing, not decorative.
   Check: `world/conventions/capability-routing.md` has a "Bounded-parameter config tune" row with `tree.config.decompose_threshold` named. Grep `tree\.config\.decompose_threshold` in that file must match.
   Check: No extension-less stray files in META_DIR. Bash: `source core/scripts/_paths.sh; test -z "$(find "$META_DIR" -maxdepth 1 -type f ! -name "*.yaml" ! -name "*.jsonl" ! -name "*.md" ! -name "*.json" ! -name ".initialized")"` — any unknown extension-less file traces back to the meta-set.sh ghost-file bug (guard-311).
   Check: `meta/config-overrides.yaml` override keys all use the file-prefix convention. Bash: `py -c "import yaml,sys; d=yaml.safe_load(open(r'$(bash -c 'source core/scripts/_paths.sh; echo $META_DIR')/config-overrides.yaml')); ov=d.get('overrides') or {}; bad=[k for k in ov if not any(k.startswith(p) for p in ('tree.','aspirations.','meta.','goal-selection.','reflection.','evolution.','aspiration-generation.','encoding.','skill-quality.'))]; sys.exit(1 if bad else 0)"` — bad keys fail loud.
   Check: g-115-99 (world) has `participants: [agent]` (not `[user]`) — regression guard after the remediation. Bash: `bash core/scripts/aspirations-query.sh --goal-field id g-115-99 | py -c "import json,sys; d=json.load(sys.stdin); sys.exit(0)"` and goal detail must include `agent` in participants.
   Check: rb-335 exists and is active (`reasoning-bank.py rb read --id rb-335` returns a record)
   Check: guard-309 exists and is active (`reasoning-bank.py guard read --id guard-309` returns a record)
   Check: guard-310 exists and is active (file-prefix convention enforcement)
   Check: guard-311 exists and is active (meta-set.sh extension-less trap)
   Bash (three-state-override-end-to-end): py -c "import sys; sys.path.insert(0, 'core/scripts'); import importlib.util as i; s=i.spec_from_file_location('t','core/scripts/tree.py'); m=i.module_from_spec(s); s.loader.exec_module(m); v=m._config_threshold(); sys.exit(0 if isinstance(v,int) and v>0 else 1)" && echo "PASS: _config_threshold returns a positive int" || echo "FAIL: override reader broken"
   # Shared overlay helper — single-source-of-truth for all non-tree config readers (g-115-123, rb-335)
   Check: `core/scripts/_config_overlay.py` defines `merged_config`. Bash: `grep -q "^def merged_config" core/scripts/_config_overlay.py` must match.
   Check: `core/scripts/goal-selector.py::load_recurring_config` calls `_config_overlay.merged_config("aspirations.yaml")` (not raw `yaml.safe_load`). Bash: `grep -q 'overlay.merged_config("aspirations.yaml")' core/scripts/goal-selector.py` must match.
   Check: `core/scripts/goal-selector.py` declares `_OVERRIDE_FILE_PREFIX = "aspirations."`. Bash: `grep -q '_OVERRIDE_FILE_PREFIX = "aspirations\.\"' core/scripts/goal-selector.py` must match.
   Check: `core/config/aspirations.yaml` `recurring:` block has no duplicate-key collision. Bash: `py -c "import yaml; d=yaml.safe_load(open('core/config/aspirations.yaml')); r=d.get('recurring', {}); assert r.get('urgency_base') == 1.5 and r.get('cargo_cult_threshold') == 3, f'urgency_base={r.get(\"urgency_base\")} cargo_cult_threshold={r.get(\"cargo_cult_threshold\")}'"` — exits 0 when both keys are readable simultaneously.
   Check: `world/conventions/capability-routing.md` lists the 7 newly-wired aspirations.recurring params. Bash: `bash core/scripts/world-cat.sh conventions/capability-routing.md | grep -q 'aspirations\.recurring\.urgency_base'` must match.
   Bash (goal-selector-loads-through-overlay): py -c "import sys,importlib.util as i; sys.path.insert(0,'core/scripts'); s=i.spec_from_file_location('gs','core/scripts/goal-selector.py'); m=i.module_from_spec(s); s.loader.exec_module(m); rc=m.RECURRING_CONFIG; sys.exit(0 if rc.get('urgency_base')==1.5 and rc.get('debt_bonus')==3.0 and rc.get('streak_mult')==2.0 else 1)" && echo "PASS: goal-selector.RECURRING_CONFIG populated via overlay" || echo "FAIL: overlay wiring broken"
   # agent-config-override-layer tree node exists (encoded knowledge)
   Check: `world/knowledge/tree/execution/ayoai-development-patterns/framework-patterns/agent-config-override-layer.md` exists

   # CLI ergonomics — single-record output + stdin-field --help + cross-queue probe (Section CLI, rb-336, guard-315, guard-316)
   # Regression guards from the 2026-04-19 CLI-flag-friction fix. Protects three invariants:
   # (1) aspirations-update-goal stdout stays at single-record granularity (not full-aspiration flood)
   # (2) journal add subparser documents required stdin fields (not empty --help)
   # (3) cmd_claim/cmd_release surface agent-queue goals with a helpful message instead of generic "not found"
   Check: `core/scripts/aspirations.py` cmd_update_goal prints `goal`, not `asp`. Bash: `grep -nE "print\(json\.dumps\(goal, indent=2" core/scripts/aspirations.py | grep -q .` — must match (print-goal line present).
   Check: exactly one `print(json.dumps(goal, indent=2` call lives inside `cmd_update_goal`'s body. Bash: `grep -A 80 '^def cmd_update_goal' core/scripts/aspirations.py | tail -n +2 | sed '/^def [a-zA-Z]/,$d' | grep -c "print(json\.dumps(goal, indent=2" | grep -q '^1$'` — must match exactly 1. The `tail -n +2` strips the `def` line itself so sed's next-def terminator works (sed's `addr1,$d` deletes from the FIRST matching line, so the def line must be skipped first).
   Check: `cmd_update_goal` body never prints `asp` (regression guard — peer cmd_add/cmd_update/cmd_complete/cmd_retire do, but update-goal must not). Bash: `grep -A 80 '^def cmd_update_goal' core/scripts/aspirations.py | tail -n +2 | sed '/^def [a-zA-Z]/,$d' | grep -E "print\(json\.dumps\(asp, indent=2"` — must NOT match (grep returns non-zero).
   Check: aspirations-update-goal.sh success output stays single-record sized. Bash: `bash core/scripts/aspirations-update-goal.sh g-115-04 priority HIGH 2>/dev/null | wc -l | awk '{exit ($1 < 50) ? 0 : 1}'` — must exit 0 (<50 lines, not 200+).
   # journal.py + its `--help`/`--schema` CLI surface were REMOVED in H2
   # Wave 1 (2026-05-15) — add/update/merge migrated to the generic daemon
   # store endpoint. The two journal.py `add`-subparser / `journal-add.sh
   # --help` checks that lived here are retired (the CLI they asserted no
   # longer exists). journal-add.sh is now a thin daemon client; its record
   # contract lives in mind_api/src/store_registry.py.
   Check: `core/scripts/aspirations.py` defines `_goal_in_agent_queue(goal_id)` helper. Bash: `grep -nE "^def _goal_in_agent_queue" core/scripts/aspirations.py | grep -q .` — must match.
   Check: `cmd_claim` uses `_goal_in_agent_queue(goal_id)` on the result-None path. Bash: `grep -A 40 '^def cmd_claim' core/scripts/aspirations.py | grep -q "_goal_in_agent_queue(goal_id)"` — must match.
   Check: `cmd_release` uses `_goal_in_agent_queue(goal_id)` on the result-None path. Bash: `grep -A 40 '^def cmd_release' core/scripts/aspirations.py | grep -q "_goal_in_agent_queue(goal_id)"` — must match.
   Check: _goal_in_agent_queue is fully defensive (try/except — propagating a corrupt-file exception into claim/release would break the probe's contract). Bash: `grep -A 25 '^def _goal_in_agent_queue' core/scripts/aspirations.py | grep -q "except Exception"` — must match.
   Check: rb-336 exists, is active, and has `applies_to: "framework"` so it surfaces in universal priming. Bash: `bash core/scripts/reasoning-bank-read.sh --id rb-336 | grep -E '"applies_to": "framework"' | grep -q .` — must match.
   Check: guard-315 exists and is active (single-record stdout rule).
   Check: guard-316 exists and is active (stdin-field --help rule).

   # Digest extraction integrity (Section DE, rb-343, guard-317)
   # Regression guards from the 2026-04-19 digest-extraction session. Protects:
   # (1) Every SKILL.md summary that names sub-skills and references a "full table" / "full map"
   #     must have those sub-skills present in the referenced target file (catches the
   #     aspirations-chaining-map.md gap fixed this session).
   # (2) Every digest front matter that claims "caller has ALREADY run <cmd>" must have
   #     that command appear in the caller's pseudocode at the load point (catches Branch-A
   #     precondition drift fixed this session).
   Check: rb-343 exists and is active (branch-specific preconditions lesson). Bash: `bash core/scripts/reasoning-bank-read.sh --id rb-343 | grep -q '"status": "active"'` — must match.
   Check: guard-317 exists and is active (enumerate-caller-commands rule). Bash: `bash core/scripts/guardrails-read.sh --id guard-317 | grep -q '"status": "active"'` — must match.
   Check: `core/config/aspirations-chaining-map.md` contains rows for every sub-skill the orchestrator's summary names. Bash: `for s in aspirations-strategic-scan aspirations-all-blocked aspirations-graceful-stop; do grep -q "$s" core/config/aspirations-chaining-map.md || { echo "MISSING: $s"; exit 1; }; done && echo "PASS"` — must print PASS.
   Check: `core/config/blocked-sleep-recovery-digest.md` front matter distinguishes Branch A from Branch B entry preconditions. Bash: `grep -c "Branch A entry\|Branch B entry" core/config/blocked-sleep-recovery-digest.md | awk '{exit ($1 >= 2) ? 0 : 1}'` — must exit 0.
   Check: `core/scripts/load-blocked-sleep-recovery.sh` uses `context-reads.py check-file` for dedup (not blind cat or Read). Bash: `grep -q "context-reads.py check-file" core/scripts/load-blocked-sleep-recovery.sh` — must match.
   # G14 regression guard (g-115-1293, rb-1410/rb-1411): the advisory pre-edit gate must delegate
   # the scope/session decision to context-reads.py check-file (NOT a direct manifest grep — a
   # direct grep restores the false-positive-on-ALL-files behavior G14 fixed) AND invoke it via
   # python3 (NOT py -3 — this .sh hook sources _paths.sh, where python3 is the sanctioned form
   # per rb-1411). The negative arm is scoped to `py -3 .*context-reads.py` so the line-40
   # explanatory comment that literally contains "py -3" does not trip a false FAIL.
   Bash (pre-edit-gate-delegates-check-file): grep -qE 'python3 .*context-reads\.py" check-file' core/scripts/pre-edit-context-gate.sh && ! grep -qE 'py -3 .*context-reads\.py' core/scripts/pre-edit-context-gate.sh && echo "PASS: pre-edit-context-gate.sh delegates scope/session decision to context-reads.py check-file via python3 (G14 fix g-115-1291/rb-1410; python3-not-py-3 rb-1411)" || echo "FAIL: pre-edit-context-gate.sh no longer delegates to context-reads.py check-file via python3 — G14 scope-awareness regressed to direct-manifest-grep (false-positive on all files) or reverted to py -3 (rb-1410/rb-1411)"
   Check: knowledge tree node `system/digest-extraction` registered. Bash: `bash core/scripts/tree-read.sh --node digest-extraction | grep -q '"file":.*digest-extraction.md'` — must match.

   # Session File Manifest + desync + positive-state-gate (Section SFM, rb-352/rb-353/guard-324)
   # Regression guards for the 2026-04-19 four-concern framework pass:
   #   Concern 1 — manifest + snapshot + desync check (signal-proliferation hardening)
   #   Concern 3 — positive-state-gate for unverified positive file-state claims
   #   Concern 4 — depth-estimate → calibration loop

   # SFM1: Manifest + parser integrity (rb-352 — section-transition flush)
   # The session_snapshot.py parser MUST flush `current` at every section header,
   # not only at end-of-file. A missing flush silently misplaces the last entry
   # of section A into section B's list (caught in the 2026-04-19 fresh-eyes
   # review before it surfaced as a production orphan warning).
   Check: `core/config/session-manifest.yaml` exists with both `files:` and `invariants:` top-level sections
   Check: `core/scripts/session_snapshot.py` `_load_manifest` has a `_flush` helper called at BOTH section headers (`files:` and `invariants:`) AND at end-of-loop. Bash: `grep -c "_flush()" core/scripts/session_snapshot.py` must be >= 3.
   Bash (parser round-trips ground truth): `files=$(grep -c '^  - file:' core/config/session-manifest.yaml); invs=$(grep -c '^  - id:' core/config/session-manifest.yaml); out=$(MIND_AGENT=alpha bash core/scripts/session-snapshot.sh --output json); pf=$(echo "$out" | py -3 -c "import json,sys;print(len(json.load(sys.stdin)['files']))"); pi=$(echo "$out" | py -3 -c "import json,sys;print(len(json.load(sys.stdin)['invariants']))"); [ "$files" = "$pf" ] && [ "$invs" = "$pi" ] && echo PASS || { echo "MISMATCH files=$files parsed=$pf invs=$invs parsed=$pi"; exit 1; }`
   Bash (no <NO_ID> invariants): `bash core/scripts/session-snapshot.sh --output json | py -3 -c "import json,sys; d=json.load(sys.stdin); bad=[i for i in d['invariants'] if not i.get('id')]; print('PASS' if not bad else f'FAIL: {bad}'); sys.exit(0 if not bad else 1)"`

   # SFM2: Single-source-of-truth for manifest parsing
   # /start --recover used to duplicate the YAML parser inline. It now consumes
   # session-snapshot.sh's JSON output. Keep it that way — duplication drifts silently.
   Check: `.claude/skills/start/SKILL.md` Step 0.7 does NOT contain the literal string `startswith("- file:")` (which would indicate an inline YAML parser re-appeared). Bash: `grep -c 'startswith("- file:")' .claude/skills/start/SKILL.md` must be 0.
   Check: `.claude/skills/start/SKILL.md` Step 0.7 references `session-snapshot.sh --output json` (the canonical bash wrapper around session_snapshot.py — keeps a single manifest-parser path). Bash: `grep -c "session-snapshot.sh.*--output.*json" .claude/skills/start/SKILL.md` must be >= 1.

   # SFM3: Desync check is advisory-only (never blocks)
   Bash: `bash core/scripts/session-desync-check.sh; echo "exit=$?"` — exit MUST be 0 regardless of warnings
   Check: `core/scripts/session_desync_check.py` always returns 0 from `main()` (grep `return 0` at end of main). Bash: `grep -c "Always exit 0 — advisory" core/scripts/session_desync_check.py` must be >= 1.

   # SFM4: positive-state-gate smoke test (Concern 3)
   Bash (trigger+no-evidence → exit 1): `py -3 core/scripts/positive-state-gate.py --claim "handoff.yaml reflects session 50" --evidence "(no read)"; [ $? -eq 1 ] && echo PASS || { echo "FAIL: expected exit 1"; exit 1; }`
   Bash (trigger+matching-evidence → exit 0): `py -3 core/scripts/positive-state-gate.py --claim "handoff.yaml reflects session 50" --evidence "handoff.yaml last_updated: 2026-04-19"; [ $? -eq 0 ] && echo PASS || { echo "FAIL: expected exit 0"; exit 1; }`
   Bash (no-trigger → exit 0): `py -3 core/scripts/positive-state-gate.py --claim "Everything looks fine." --evidence ""; [ $? -eq 0 ] && echo PASS || { echo "FAIL: expected exit 0"; exit 1; }`
   Check: `.claude/skills/aspirations-verify/SKILL.md` references `positive-state-gate.py`. Bash: `grep -c "positive-state-gate.py" .claude/skills/aspirations-verify/SKILL.md` must be >= 1.

   # SFM4b: gate-retirement-eval self-test — exercises all 6 recommendation rules against
   # synthetic counts. Catches rule-ordering regressions (the widen-eaten-by-meaningful-firings
   # bug found Phase 5 build) and false-positive retirement on under-exercised gates.
   Bash: `py -3 core/scripts/gate-retirement-eval.py --self-test >/dev/null; [ $? -eq 0 ] && echo PASS || { echo "FAIL: gate-retirement-eval self-test failed — re-run without redirect to see which case"; exit 1; }`

   # SFM4b2: scoring-criterion-audit self-test — exercises every recommendation rule
   # (dead_field, degenerate_field, sparse_below_floor, sparse_by_design, healthy,
   # insufficient_data) plus the source_skew detector across synthetic per-source rows.
   # Catches rule-ordering regressions in the field-coverage recommender and protects
   # the manifest contract: a criterion in goal-selection-strategy.yaml weights that's
   # missing from scoring-criteria.yaml will surface as `unmapped` on the next live run.
   Bash: `py -3 core/scripts/scoring-criterion-audit.py --self-test >/dev/null; [ $? -eq 0 ] && echo PASS || { echo "FAIL: scoring-criterion-audit self-test failed — re-run without redirect to see which case"; exit 1; }`

   # SFM4c: gate-stats dashboard — produces valid JSON with the 6 expected sections.
   # Phase 6 dashboard is read-only; this just confirms the script imports + the
   # expected schema lands. Doesn't validate semantic content (telemetry varies).
   Bash: `py -3 core/scripts/gate-stats.py --output json --days 1 | py -3 -c "import json, sys; d = json.load(sys.stdin); req = ['overview','decisions_per_gate','override_rates','triggers_global_top','bulk_override_correlation','fail_open_records']; missing = [k for k in req if k not in d]; sys.exit(1 if missing else 0)" && echo PASS || { echo "FAIL: gate-stats.py output schema invalid"; exit 1; }`

   # SFM4d: bulk-override fan-out wiring — aspirations.py and create-blocker.py
   # MUST keep passing the right slot lists into apply_override_all. A silent
   # rename or drop of a slot would break the per-gate-wins contract documented
   # in core/config/conventions/gate-overrides.md. Static grep — no runtime needed.
   # Defends against the silent-slot-drift class flagged by rb-466.
   Bash: `grep -q '"override_signal", "override_duplication"' core/scripts/aspirations.py && grep -q '"override_blocker_gate", "override_agent_match"' core/scripts/create-blocker.py && echo PASS || { echo "FAIL: --override-all slot wiring drift in aspirations.py or create-blocker.py — see rb-466"; exit 1; }`

   # SFM5: Depth-estimate → calibration loop (Concern 4)
   Check: `.claude/skills/aspirations-execute/SKILL.md` has Phase 3.95 with `estimated_depth` write. Bash: `grep -c "Phase 3.95" .claude/skills/aspirations-execute/SKILL.md` must be >= 1.
   Check: `.claude/skills/reflect-on-outcome/SKILL.md` has Step 0.75 Depth Calibration that writes to `meta/depth-calibration.jsonl`. Bash: `grep -c "depth-calibration.jsonl" .claude/skills/reflect-on-outcome/SKILL.md` must be >= 1.
   Check: reflect-on-outcome Step 0.75 has NO dead `|| echo 0` fallback on the `wc -l` read. Bash: `grep -c "wc -l.*depth-calibration.*echo 0" .claude/skills/reflect-on-outcome/SKILL.md` must be 0.
   Check: `meta/goal-selection-strategy.yaml` has `depth_bias_advisories:` key (even if empty list — needed for first advisory append). Bash: `source core/scripts/_paths.sh && grep -c "depth_bias_advisories" "$META_DIR/goal-selection-strategy.yaml"` must be >= 1.

   # SFM6: Concern 2 (strategic cadence + self_evolution on routine)
   Check: `core/config/aspirations.yaml` `strategic_scan.goal_cadence: 5` (tightened from 8). Bash: `grep -c "goal_cadence: 5" core/config/aspirations.yaml` must be >= 1.
   Check: `.claude/skills/aspirations-spark/SKILL.md` creative_routine_questions includes `self_evolution`. Bash: `grep -c '"self_evolution"' .claude/skills/aspirations-spark/SKILL.md` must be >= 1.
   Check: `.claude/skills/aspirations-select/SKILL.md` Self-Alignment Check includes Program-alignment probe (`world-cat.sh program.md` + `program_misalignment_streak` tracking). Bash: `grep -c "program_misalignment_streak" .claude/skills/aspirations-select/SKILL.md` must be >= 2.
   Check: Program-alignment probe has NO ineffective `|| echo 0` after wm-read.sh (rb-353). Bash: `grep -c "wm-read.sh program_misalignment_streak.*echo 0" .claude/skills/aspirations-select/SKILL.md` must be 0.

   # SFM7: rb-352, rb-353, guard-324 registered
   Check: rb-352 exists and is active (parser section-transition flush). Bash: `bash core/scripts/reasoning-bank-read.sh --id rb-352 | grep -q '"status": "active"'` — must match.
   Check: rb-353 exists and is active (ineffective fallback anti-pattern). Bash: `bash core/scripts/reasoning-bank-read.sh --id rb-353 | grep -q '"status": "active"'` — must match.
   Check: guard-324 exists and is active (parser-count assertion). Bash: `bash core/scripts/guardrails-read.sh --id guard-324 | grep -q '"status": "active"'` — must match.

   # Section A248: asp-248 bash-enforcement (2026-04-20, rb-393, guard-343)
   # Three drift-invited patterns were decomposed into bash-WHETHER / LLM-WHAT pairs.
   # If any of these regress, the LLM reclaims trigger evaluation and drift returns —
   # the whole point of the extraction is that threshold math lives in scripts, not
   # in prose an LLM re-interprets every iteration.
   Check: `core/scripts/post-state-update-gate.sh` exists and is executable. Bash: `test -x core/scripts/post-state-update-gate.sh && echo PASS || { echo "FAIL: post-state-update-gate.sh missing or non-executable"; exit 1; }`
   Check: `core/scripts/cross-agent-recent-changes.sh` exists and is executable. Bash: `test -x core/scripts/cross-agent-recent-changes.sh && echo PASS || { echo "FAIL: cross-agent-recent-changes.sh missing or non-executable"; exit 1; }`
   Check: `.claude/skills/aspirations-state-update/SKILL.md` has Step 8.78 that calls post-state-update-gate.sh. Bash: `grep -c "post-state-update-gate.sh" .claude/skills/aspirations-state-update/SKILL.md` must be >= 1.
   Check: aspirations-state-update Step 8.78 dispatches Skill('fresh-eyes-code') on fired=true. Bash: `grep -cE "Step 8\.78|fresh-eyes-code" .claude/skills/aspirations-state-update/SKILL.md` must be >= 2.
   Check: `.claude/skills/fresh-eyes-code/SKILL.md` Phase 1 caps file list via bash (head -20 or equivalent). Bash: `grep -cE "head -20|head -n ?20" .claude/skills/fresh-eyes-code/SKILL.md` must be >= 1.
   Check: fresh-eyes-code --since mode calls cross-agent-recent-changes.sh. Bash: `grep -c "cross-agent-recent-changes.sh" .claude/skills/fresh-eyes-code/SKILL.md` must be >= 1.
   Check: guard-343 trigger_condition references post-state-update-gate.sh (bash-enforced). Bash: `bash core/scripts/guardrails-read.sh --id guard-343 | grep -q "post-state-update-gate.sh" && echo PASS || { echo "FAIL: guard-343 not bash-enforced"; exit 1; }`
   Check: cross-agent-recent-changes.sh supports --since-goal flag (first-fire spec lives in script). Bash: `grep -c "since-goal" core/scripts/cross-agent-recent-changes.sh` must be >= 2.
   Check: g-248-07/08 descriptions reference --since-goal (not --since with bare timestamp). Bash: `source core/scripts/_paths.sh && grep -o "since-goal" "$WORLD_PATH/aspirations.jsonl" | wc -l` must be >= 2. (grep -o counts occurrences; grep -c counts lines and would return 1 since all asp-248 goals share one JSONL record.)
   Check: rb-393 exists and is active (bash-WHETHER / LLM-WHAT decomposition). Bash: `bash core/scripts/reasoning-bank-read.sh --id rb-393 | grep -q '"status": "active"'` — must match.

   # Section TS-LA: team-state.last_active drift fix (2026-04-20, commits aa337ab + 1c57d8b, rb-399, rb-400, guard-351)
   # Closes the silence-detection drift where last_active was only written at goal completion (Phase 8)
   # so a long Phase-4 goal made the running agent look silent for hours. The four canonical writers of
   # last_active are: cmd_in_flight (claim), cmd_clear_in_flight (release/skip/complete), Phase -0.5 heartbeat,
   # and start.SKILL.md autonomous session-start seed. Any extra writer is a regression.
   Check: team-state.py cmd_in_flight writes last_active alongside in_flight (Fix A). Bash: `py -3 -c "import re,sys; src=open('core/scripts/team-state.py').read(); m=re.search(r'def cmd_in_flight.*?def ', src, re.DOTALL); sys.exit(0 if (m and 'last_active' in m.group(0)) else 1)" && echo PASS || { echo "FAIL: cmd_in_flight no longer writes last_active — Fix A regressed"; exit 1; }`
   Check: team-state.py cmd_clear_in_flight writes last_active when in_flight present (Fix A symmetric). Bash: `py -3 -c "import re,sys; src=open('core/scripts/team-state.py').read(); m=re.search(r'def cmd_clear_in_flight.*?(?:\Z|def )', src, re.DOTALL); sys.exit(0 if (m and 'last_active' in m.group(0)) else 1)" && echo PASS || { echo "FAIL: cmd_clear_in_flight no longer writes last_active — Fix A regressed"; exit 1; }`
   Check: per-iteration team-state heartbeat write lives in core/scripts/heartbeat-tick.sh (Fix B + script extraction rb-399 — moved out of SKILL.md so changes apply in-flight to running loops). Bash: `grep -cE 'agent_status\.\$MIND_AGENT\.last_active' core/scripts/heartbeat-tick.sh` must be >= 1 (single-pattern: the field path is unique to the heartbeat write; do NOT use `team-state-update.sh.*last_active` — those substrings live on different lines in the script and grep is single-line by default). Bash: `grep -c 'bash core/scripts/heartbeat-tick.sh' .claude/skills/aspirations/SKILL.md` must be >= 1 (Phase -0.5 must call the script).
   Check: start/SKILL.md autonomous-mode block seeds last_active + resets current_focus before state-set RUNNING (Fix C). Bash: `awk '/\*\*Autonomous mode:\*\*/,/session-state-set\.sh RUNNING/' .claude/skills/start/SKILL.md | grep -cE 'agent_status\.<agent-name>\.(last_active|current_focus)'` must be >= 2.
   Check: start/SKILL.md autonomous-mode current_focus write uses empty value, NOT a prospective string like "starting" (commit b5bd9d8 — coordination.md:275 documents current_focus as retrospective; only aspirations-state-update and aspirations-consolidate may write non-empty values, both of which fire AFTER work completes). Bash: `awk '/\*\*Autonomous mode:\*\*/,/session-state-set\.sh RUNNING/' .claude/skills/start/SKILL.md | grep -cE 'current_focus.*--value\s+"\\"starting\\""'` must be 0. (Regex matches only the bash-escaped --value argument form; the comment word "starting" has no backslashes and won't match.)
   Check: iteration-close.sh do_state_update no longer has the redundant explicit last_active write (cleanup commit 1c57d8b). Bash: `grep -cE '^\s*bash.*team-state-update\.sh.*agent_status\.\$AGENT\.last_active' core/scripts/iteration-close.sh` must be 0 (only the comment block referencing it should remain). Bash: `grep -c 'SINGLE SOURCE OF TRUTH for last_active at goal completion' core/scripts/iteration-close.sh` must be >= 1.
   Check: no SKILL.md uses 2>/dev/null on team-state-update.sh invocations (rb-400 silent-boundary). Bash: `grep -rEn 'team-state-update\.sh.*2>/dev/null' .claude/skills/ && { echo "FAIL: 2>/dev/null suppressing stderr on team-state-update — see rb-400"; exit 1; } || echo PASS`
   Check: rb-399, rb-400, guard-351 exist and are active. Bash: `for id in rb-399 rb-400; do bash core/scripts/reasoning-bank-read.sh --id $id | grep -q '"status": "active"' || { echo "FAIL: $id not active"; exit 1; }; done; bash core/scripts/guardrails-read.sh --id guard-351 | grep -q '"status": "active"' || { echo "FAIL: guard-351 not active"; exit 1; }; echo PASS`

   # Section g-115-291: cross-agent fresh-eyes coverage tracking via team-state shared slot (2026-04-27, commit f7aa2b2, rb-594)
   # Closes rb-593 gate coverage-tracking gap. /fresh-eyes-code Phase 5b writes
   # agent_status.<self>.last_fresh_eyes_run to world/team-state.yaml after review;
   # post-state-update-gate.sh cooldown reads BOTH own-agent fresh_eyes_last_fire (WM)
   # AND cross-agent agent_status.*.last_fresh_eyes_run (team-state) and unions them
   # for subset-suppression (yes:self / yes:peer / yes:union / no verdicts).
   Check: /fresh-eyes-code SKILL.md has Phase 5b that writes last_fresh_eyes_run via team-state-update.sh. Bash: `grep -cE "Phase 5b|last_fresh_eyes_run" .claude/skills/fresh-eyes-code/SKILL.md` must be >= 2.
   Check: /fresh-eyes-code Phase 5b uses agent_status.<self>.last_fresh_eyes_run path. Bash: `grep -c 'agent_status\.\${\?MIND_AGENT' .claude/skills/fresh-eyes-code/SKILL.md` must be >= 1.
   Check: post-state-update-gate.sh cooldown body reads team-state.yaml peer files. Bash: `grep -cE 'TEAM_STATE_PATH|last_fresh_eyes_run' core/scripts/post-state-update-gate.sh` must be >= 2.
   Check: post-state-update-gate.sh emits distinct yes:peer/yes:self/yes:union verdicts. Bash: `grep -cE '"yes:(peer|self|union)"' core/scripts/post-state-update-gate.sh` must be >= 3.
   Check: post-state-update-gate.sh cooldown filters self-agent in peer iteration (cross-agent symmetry). Bash: `grep -cE 'agent_name == self_agent|SELF_AGENT' core/scripts/post-state-update-gate.sh` must be >= 1.
   Check: coordination.md documents last_fresh_eyes_run schema under agent_status.<name>. Bash: `grep -c 'last_fresh_eyes_run' core/config/conventions/coordination.md` must be >= 2.
   Check: regression test test_post_state_update_gate_cooldown.py exists and covers ≥9 cases. Bash: `test -f core/scripts/tests/test_post_state_update_gate_cooldown.py && grep -cE '^    run_case\(' core/scripts/tests/test_post_state_update_gate_cooldown.py` must be >= 9.
   Check: regression test passes. Bash: `py -3 core/scripts/tests/test_post_state_update_gate_cooldown.py 2>&1 | grep -q "TEST PASS"` — must match.
   Check: rb-594 exists and is active (cross-agent-cooldown-via-team-state pattern). Bash: `bash core/scripts/reasoning-bank-read.sh --id rb-594 | grep -q '"status": "active"'` — must match.

   # Section CAS: Cross-Agent Source Enum Coverage (2026-05-19, g-115-970 / g-115-23 iter-78.3 / g-115-968 fix)
   # Empirical finding from g-115-23 product-world-refresh: loop-state-save.sh init rejects
   # source='cross-agent:alpha' (enum is closed to {world, agent}). The g-115-946 cross-agent
   # collector fix surfaces sibling-routed goals correctly, but any orchestrator-side gate that
   # validates source against the closed enum will break the execution path. Filed via sq-018
   # to catch the gap structurally before the next regression hits.
   #
   # Coverage targets (initial — extend as new source-validating sites land):
   #   1. aspirations.py        — argparse choices=["world", "agent"] (the canonical site)
   #   2. loop-state-save.py    — typed-key schema "enum": ("world", "agent")
   #   3. recurring-close.sh    — bash if [[ "$SOURCE" == "world" ]] / "agent" else error
   #   4. iteration-close.sh    — passes --source through to aspirations.py (no own enum)
   #
   # PASS contract: each site either (a) includes the literal substring "cross-agent" in or
   # within ±3 lines of the source-enum definition (g-115-968 fix landed) OR (b) carries the
   # justification marker "WORLD_AGENT_ONLY:" within ±3 lines (intentional closed enum with
   # documented reason). Until one of these two is true at every site, the check FAILS — that
   # is the intended forcing-function behavior. Once g-115-968 lands the cross-agent enum
   # support, the check flips to PASS automatically.

   # CAS1: aspirations.py --source argparse documents cross-agent OR justifies world/agent-only
   Check: `core/scripts/aspirations.py` --source argparse either accepts cross-agent or carries WORLD_AGENT_ONLY: marker. Bash: `py -3 -c "import re,sys,pathlib; src=pathlib.Path('core/scripts/aspirations.py').read_text(encoding='utf-8'); lines=src.splitlines(); hits=[(i,L) for i,L in enumerate(lines) if re.search(r'add_argument\(\s*\"--source\"', L) and re.search(r'choices\s*=\s*\[\s*\"world\"\s*,\s*\"agent\"\s*\]', '\n'.join(lines[i:i+3]))]; bad=[]; [bad.append(f'aspirations.py:{i+1}') for i,_ in hits if not any('cross-agent' in lines[j] or 'WORLD_AGENT_ONLY:' in lines[j] for j in range(max(0,i-3), min(len(lines),i+4)))]; sys.exit(1 if bad else 0)" && echo PASS || { echo "FAIL: aspirations.py --source argparse uses closed {world, agent} enum without cross-agent support or WORLD_AGENT_ONLY: justification — see g-115-968"; exit 1; }`

   # CAS2: loop-state-save.py source typed-key schema either accepts cross-agent or carries marker
   Check: `core/scripts/loop-state-save.py` source enum either accepts cross-agent or carries WORLD_AGENT_ONLY: marker. Bash: `py -3 -c "import re,sys,pathlib; src=pathlib.Path('core/scripts/loop-state-save.py').read_text(encoding='utf-8'); lines=src.splitlines(); hits=[(i,L) for i,L in enumerate(lines) if re.search(r'\"source\"\s*:\s*\{[^}]*\"enum\"\s*:\s*\(\s*\"world\"\s*,\s*\"agent\"', L)]; bad=[]; [bad.append(f'loop-state-save.py:{i+1}') for i,_ in hits if not any('cross-agent' in lines[j] or 'WORLD_AGENT_ONLY:' in lines[j] for j in range(max(0,i-3), min(len(lines),i+4)))]; sys.exit(1 if bad else 0)" && echo PASS || { echo "FAIL: loop-state-save.py source enum is closed {world, agent} without cross-agent support or WORLD_AGENT_ONLY: justification — see g-115-968"; exit 1; }`

   # CAS3: recurring-close.sh source validator either accepts cross-agent or carries marker
   # Uses simpler regex (no literal $ — avoids multi-layer shell-quoting issues): match
   # `if [[ ... SOURCE ... == ... "world" ... ]]` rather than the full `$SOURCE` literal.
   Check: `core/scripts/recurring-close.sh` source bash validator either accepts cross-agent or carries WORLD_AGENT_ONLY: marker. Bash: `py -3 -c "import re,sys,pathlib; src=pathlib.Path('core/scripts/recurring-close.sh').read_text(encoding='utf-8'); lines=src.splitlines(); pat=re.compile(r'if\s*\[\[.*SOURCE.*==.*\"world\".*\]\]'); hits=[i for i,L in enumerate(lines) if pat.search(L)]; bad=[]; [bad.append(f'recurring-close.sh:{i+1}') for i in hits if not any('cross-agent' in lines[j] or 'WORLD_AGENT_ONLY:' in lines[j] for j in range(max(0,i-3), min(len(lines),i+8)))]; sys.exit(1 if bad else 0)" && echo PASS || { echo "FAIL: recurring-close.sh source bash validator handles only {world, agent} without cross-agent support or WORLD_AGENT_ONLY: justification — see g-115-968"; exit 1; }`

   # CAS4: iteration-close.sh --source arg passthrough either accepts cross-agent or carries marker
   # iteration-close.sh has no closed enum of its own (passes --source through), so the check
   # is satisfied either by an explicit cross-agent acceptance near the arg parse, OR by a
   # WORLD_AGENT_ONLY: marker near the --source parse line. Soft-fail (warning style) until
   # g-115-968 wires through. Simpler regex matches `--source) SOURCE=` (the case-arm shape).
   Check: `core/scripts/iteration-close.sh` --source handler either accepts cross-agent or carries WORLD_AGENT_ONLY: marker. Bash: `py -3 -c "import re,sys,pathlib; src=pathlib.Path('core/scripts/iteration-close.sh').read_text(encoding='utf-8'); lines=src.splitlines(); pat=re.compile(r'--source\)\s*SOURCE='); hits=[i for i,L in enumerate(lines) if pat.search(L)]; bad=[]; [bad.append(f'iteration-close.sh:{i+1}') for i in hits if not any('cross-agent' in lines[j] or 'WORLD_AGENT_ONLY:' in lines[j] for j in range(max(0,i-5), min(len(lines),i+5)))]; sys.exit(1 if bad else 0)" && echo PASS || { echo "FAIL: iteration-close.sh --source handler lacks cross-agent acceptance or WORLD_AGENT_ONLY: justification — see g-115-968"; exit 1; }`

   # CAS5: Forward-looking scanner — scan core/scripts/ for argparse choices=[..."world"..."agent"...]
   # and for typed-key validator enums of the same shape. Every match must EITHER name the file
   # in the documented coverage list above OR include cross-agent acceptance OR carry the
   # WORLD_AGENT_ONLY: marker within ±3 lines. Catches NEW source-validating sites that land
   # after this check was authored. Shell pattern uses simpler regex (no `$` — same rationale
   # as CAS3) to survive shell-quoting.
   Check: no undocumented core/scripts/ site has closed {world, agent} source enum without cross-agent acceptance or WORLD_AGENT_ONLY: marker. Bash: `py -3 -c "import re,sys,pathlib; root=pathlib.Path('core/scripts'); documented={'aspirations.py','loop-state-save.py','recurring-close.sh','iteration-close.sh'}; pat_py=re.compile(r'choices\s*=\s*\[\s*\"world\"\s*,\s*\"agent\"\s*\]|\"enum\"\s*:\s*\(\s*\"world\"\s*,\s*\"agent\"\s*\)'); pat_sh=re.compile(r'if\s*\[\[.*SOURCE.*==.*\"world\".*\]\]'); violations=[]; [violations.append(f'{p.name}:{i+1}') for p in sorted(list(root.glob('*.py'))+list(root.glob('*.sh'))) if p.name not in documented for i,L in enumerate(p.read_text(encoding='utf-8',errors='replace').splitlines()) if (pat_py if p.suffix=='.py' else pat_sh).search(L) and not any('cross-agent' in (p.read_text(encoding='utf-8',errors='replace').splitlines())[j] or 'WORLD_AGENT_ONLY:' in (p.read_text(encoding='utf-8',errors='replace').splitlines())[j] for j in range(max(0,i-3), min(len(p.read_text(encoding='utf-8',errors='replace').splitlines()),i+4)))]; sys.exit(1 if violations else 0)" && echo PASS || { echo "FAIL: new core/scripts/ site has closed {world, agent} source enum but is not in the documented coverage list (aspirations.py, loop-state-save.py, recurring-close.sh, iteration-close.sh) and lacks cross-agent acceptance / WORLD_AGENT_ONLY: justification — add to CAS coverage list or document"; exit 1; }`

   # Section CAC: Cross-Agent Candidate Collection Loop in goal-selector.py (2026-05-19, g-115-964 / g-115-946 / sq-018)
   # The g-115-946 fix added a third read pass in cmd_select that pulls pending goals from
   # sibling agent queues where intended_agent == AGENT_NAME — closes the cross-agent
   # stranding gap where the capability-route gate stamps intended_agent on filed goals,
   # the goal lands in the FILER's private queue, and the routed-to TARGET never sees it.
   # Without a structural check, a future refactor of cmd_select could silently drop the
   # cross-agent pull (e.g., reordering, removing the `if AGENT_DIR is not None:` guard,
   # collapsing into the existing collect_candidates loop) and re-introduce the 15+
   # stranded-goal leak that motivated the fix. The CAS section (above) guards the source
   # ENUM coverage; this section guards the COLLECTION LOOP. They are sibling checks
   # against the same incident class (cross-agent invisibility) at different surfaces.
   #
   # PASS contract — all four must hold simultaneously:
   #   1. `collect_cross_agent_candidates` appears >=2 times (exactly one `def`, >=1 call)
   #   2. the call site lies BETWEEN the last `collect_candidates(` call in cmd_select
   #      (agent queue) AND the first subsequent `if not candidates:` blocked-goals handler
   #   3. the function body includes a fail-open `except Exception:` followed by `continue`
   #      within 5 lines (the per-sibling skip on unreadable aspirations.jsonl)
   #   4. (implicit via 1+2) the function definition exists at module-top-level, not nested
   Check: `core/scripts/goal-selector.py` has the cross-agent candidate collection loop wired correctly (def + call site between collect_candidates and blocked-handler + per-sibling fail-open continue). Bash: `py -3 -c "import re,sys,pathlib; src=pathlib.Path('core/scripts/goal-selector.py').read_text(encoding='utf-8'); L=src.splitlines(); xc=[i for i,l in enumerate(L) if 'collect_cross_agent_candidates' in l]; df=[i for i in xc if L[i].lstrip().startswith('def ')]; cl=[i for i in xc if i not in df]; cc=[i for i,l in enumerate(L) if re.search(r'\bcollect_candidates\(',l) and not l.lstrip().startswith('def ')]; nc=[i for i,l in enumerate(L) if re.search(r'^\s*if\s+not\s+candidates\s*:',l)]; ei=next((i for i,l in enumerate(L[df[0]+1:],start=df[0]+1) if l.startswith('def ')),len(L)) if df else 0; body=L[df[0]:ei] if df else []; ok=(len(xc)>=2 and len(df)==1 and bool(cl) and bool(cc) and bool(nc) and (max(cc)<min(cl)<min([n for n in nc if n>min(cl)],default=10**9)) and any(re.search(r'^\s*except\s+Exception\s*:',body[i]) and any(re.search(r'^\s*continue\b',body[j]) for j in range(i+1,min(i+5,len(body)))) for i in range(len(body)-1))); sys.exit(0 if ok else 1)" && echo PASS || { echo "FAIL: goal-selector.py cross-agent candidate collection loop missing or wrongly positioned (see g-115-946 / g-115-964) — verify (a) collect_cross_agent_candidates defined and called, (b) call site sits between collect_candidates(agent) and 'if not candidates:', (c) per-sibling fail-open 'except Exception: continue' present in function body"; exit 1; }`

   # Section TOL: Tolerant-Decode Coverage Across g-115-797 Audit Catalog (2026-05-19, g-115-950)
   # Pins the corruption-tolerant decode pattern (g-115-766 / g-115-796 / guard-383)
   # across the 6 audit-catalog files. Future regressions could (a) revert the
   # `_tolerant_decode` helper to bare `json.loads` + silent JSONDecodeError swallow,
   # (b) re-introduce RtError silent return [] in violation of guard-383 (the N>=2
   # source-aggregator fatal rule), or (c) lose the docstring lineage tying each
   # file back to the audit-catalog row.
   #
   # Catalog state at landing (2026-05-19):
   #   A0 consolidation-health.py         — _tolerant_decode helper (exemplar)
   #   A1 defer-recheck.py                — INLINED pattern in _read_goals (NO helper); RtError
   #                                          intentionally silent pre-correction per g-115-948
   #   A2 blocker-recheck.py              — _tolerant_decode helper
   #   A3 precondition-defer-recheck.py   — _tolerant_decode helper
   #   A4 parent-supersession-sweep.py    — _tolerant_decode helper
   #   A5 unblock-parent-status-sweep.py  — _tolerant_decode helper (g-115-943, this work)
   #
   # Cross-references: rb-987 (per-source-error must be fatal), rb-347 (fail-open boundary is
   # the shell wrapper, never the aggregator), guard-383 (single-source-of-truth fatal pattern
   # for N>=2 source aggregators), g-115-797 (original audit), g-115-796 (exemplar landing),
   # g-115-939 (A1 test pinning), g-115-943 (A5 landing), g-115-948 (A1 RtError sweep),
   # g-115-949 (extract shared helper to _rt.py).

   # TOL1: 5 corrected siblings (A0/A2/A3/A4/A5) MUST contain `_tolerant_decode` helper
   Check: 5 corrected audit-catalog siblings contain `_tolerant_decode` helper. Bash: `py -3 -c "import pathlib,sys; root=pathlib.Path('core/scripts'); siblings=['consolidation-health.py','blocker-recheck.py','precondition-defer-recheck.py','parent-supersession-sweep.py','unblock-parent-status-sweep.py']; missing=[s for s in siblings if not any('def _tolerant_decode' in L for L in (root/s).read_text(encoding='utf-8',errors='replace').splitlines())]; sys.exit(1 if missing else 0)" && echo PASS || { echo "FAIL: missing _tolerant_decode helper in one of the 5 corrected audit-catalog siblings — see g-115-797 catalog"; exit 1; }`

   # TOL2: A1 (defer-recheck.py) is the documented inlined-pattern outlier
   # The A1 sibling intentionally inlines the tolerant-decode pattern in `_read_goals`
   # rather than extracting a helper (per g-115-797 catalog row + g-115-949 follow-up).
   # The check enforces the pattern is present even though no helper exists: json.JSONDecoder
   # raw_decode usage AND fatal sys.exit(1) on bad aggregate.
   Check: defer-recheck.py (A1) inlines tolerant-decode pattern in _read_goals (json.JSONDecoder().raw_decode + sys.exit(1) on bad aggregate). Bash: `py -3 -c "import pathlib,sys; src=pathlib.Path('core/scripts/defer-recheck.py').read_text(encoding='utf-8'); ok = 'json.JSONDecoder' in src and 'raw_decode' in src and 'sys.exit(1)' in src; sys.exit(0 if ok else 1)" && echo PASS || { echo "FAIL: defer-recheck.py (A1) lost the inlined tolerant-decode pattern in _read_goals — see g-115-797 catalog row A1"; exit 1; }`

   # TOL3: No catalog file has the regression-shape pattern `except json.JSONDecodeError:` immediately followed by `return []`
   # This pattern is the pre-correction silent-collapse that the audit was designed to eliminate.
   # Regex anchors to start-of-line + indent ([ \t]+) so docstring-embedded markdown backtick
   # references to the pre-correction pattern (which DOCUMENT it, not USE it) don't false-positive.
   Check: no audit-catalog file has the silent-return JSONDecodeError regression pattern at code indent. Bash: `py -3 -c "import re,pathlib,sys; root=pathlib.Path('core/scripts'); catalog=['consolidation-health.py','defer-recheck.py','blocker-recheck.py','precondition-defer-recheck.py','parent-supersession-sweep.py','unblock-parent-status-sweep.py']; pat=re.compile(r'^[ \t]+except\s+json\.JSONDecodeError\s*:\s*(?:#[^\n]*\n[ \t]*)*return\s+\[\]', re.MULTILINE); violations=[f for f in catalog if pat.search((root/f).read_text(encoding='utf-8',errors='replace'))]; sys.exit(1 if violations else 0)" && echo PASS || { echo "FAIL: silent-return JSONDecodeError pattern reintroduced in audit-catalog file at code indent — see rb-987, guard-383"; exit 1; }`

   # TOL4: 4 corrected siblings (A2/A3/A4/A5) MUST have guard-383 reference in source-reader function
   # A1 is excluded from TOL4 because its RtError branch is intentionally pre-correction (g-115-948
   # tracks the upgrade). The 4 corrected siblings each cite guard-383 in their _read_aspirations.
   Check: 4 RtError-fatal-corrected siblings cite guard-383 in source-reader. Bash: `py -3 -c "import pathlib,sys; root=pathlib.Path('core/scripts'); corrected=['blocker-recheck.py','precondition-defer-recheck.py','parent-supersession-sweep.py','unblock-parent-status-sweep.py']; missing=[f for f in corrected if 'guard-383' not in (root/f).read_text(encoding='utf-8',errors='replace')]; sys.exit(1 if missing else 0)" && echo PASS || { echo "FAIL: one of the 4 corrected siblings (A2/A3/A4/A5) lost the guard-383 reference in its source-reader function — see rb-987 / g-115-797"; exit 1; }`

   # TOL5: A1 outlier (defer-recheck.py) carries g-115-948 reference annotating the pre-correction RtError silent return
   # When g-115-948 lands (RtError sweep), this check can be relaxed or removed. Until then, the
   # annotation must be present so future readers know the silent-return is intentional and tracked.
   Check: defer-recheck.py annotates A1 RtError-silent-return with g-115-948 reference. Bash: `py -3 -c "import pathlib,sys; src=pathlib.Path('core/scripts/defer-recheck.py').read_text(encoding='utf-8'); ok = 'g-115-948' in src or 'g-115-797-A1' in src; sys.exit(0 if ok else 1)" && echo PASS || { echo "FAIL: defer-recheck.py lost the A1-outlier annotation (g-115-948 or g-115-797-A1) — future readers cannot tell silent-return is intentional pre-correction"; exit 1; }`

   # Section SGR: Standing Grants + user_leg_scope + script-owned `created` (2026-04-20, g-248-14, rb-403, guard-354)
   # Three-pronged structural move: prose → structured state. SGR1-2 anchor the Standing
   # User Grants table. SGR3-6 enforce the script-owned `created` field (SSOT _stamp_now +
   # dot-prefix defense). SGR7-10 enforce the user_leg_scope vocabulary, validator, and
   # three-site coverage. SGR12 is the lockstep check — the invariant that keeps the
   # VALID_USER_LEG_SCOPES enum and the Standing Grants scope column from drifting apart.
   # If any of these regress, the LLM reclaims prose-pattern-matching the framework
   # structured away, and capability-routing silently degrades.

   # SGR1: Standing User Grants section exists and anchors grant-001
   Check: capability-routing.md has Standing User Grants section + grant-001 row. Bash: `bash core/scripts/world-cat.sh conventions/capability-routing.md | grep -q '## Standing User Grants' && bash core/scripts/world-cat.sh conventions/capability-routing.md | grep -q 'grant-001' && echo PASS || { echo "FAIL: Standing User Grants section or grant-001 missing from capability-routing.md"; exit 1; }`

   # SGR2: guard-349 references Standing User Grants in its rule text
   Check: guard-349 active and mentions Standing User Grants. Bash: `bash core/scripts/guardrails-read.sh --id guard-349 | grep -q '"status": "active"' && bash core/scripts/guardrails-read.sh --id guard-349 | grep -q 'Standing User Grants' && echo PASS || { echo "FAIL: guard-349 inactive or missing Standing User Grants reference"; exit 1; }`

   # SGR3: _stamp_now SSOT in reasoning-bank.py
   Check: `core/scripts/reasoning-bank.py` defines `_stamp_now()`. Bash: `grep -q '^def _stamp_now' core/scripts/reasoning-bank.py && echo PASS || { echo "FAIL: _stamp_now() missing from reasoning-bank.py — script-owned `created` regressed"; exit 1; }`

   # SGR4: `created` removed from REQUIRED_FIELDS (both rb + guard)
   Check: neither RB_REQUIRED_FIELDS nor GUARD_REQUIRED_FIELDS contain `created`. Bash: `py -3 -c "import re,sys; src=open('core/scripts/reasoning-bank.py').read(); rb=re.search(r'RB_REQUIRED_FIELDS\s*=\s*[\[{]([^\]}]*)', src); gu=re.search(r'GUARD_REQUIRED_FIELDS\s*=\s*[\[{]([^\]}]*)', src); bad=[n for n,m in (('rb',rb),('guard',gu)) if m and '\"created\"' in m.group(1)]; sys.exit(1 if bad else 0)" && echo PASS || { echo "FAIL: 'created' still in REQUIRED_FIELDS — script stamping will be overridden by caller value"; exit 1; }`

   # SGR5: GUARD_KNOWN_FIELDS allowlist includes `created` (regression guard from Path 1)
   Check: GUARD_KNOWN_FIELDS contains `created`. Bash: `py -3 -c "import re,sys; src=open('core/scripts/reasoning-bank.py').read(); m=re.search(r'GUARD_KNOWN_FIELDS\s*=\s*[\[{]([^\]}]*)', src, re.DOTALL); sys.exit(0 if (m and '\"created\"' in m.group(1)) else 1)" && echo PASS || { echo "FAIL: GUARD_KNOWN_FIELDS missing 'created' — update-field rejects every existing guard record"; exit 1; }`

   # SGR6: rb_update_field AND guard_update_field reject `field == "created"` OR `field.startswith("created.")` (guard-354 dot-prefix defense)
   Check: both update paths defend against literal AND dot-prefix `created`. Bash: `py -3 -c "import re,sys; src=open('core/scripts/reasoning-bank.py').read(); bad=[]; [bad.append(fn) for fn in ('rb_update_field','guard_update_field') if not re.search(r'def '+fn+r'\\b.*?field\s*==\s*\"created\".*?field\.startswith\(\s*\"created\.\"', src, re.DOTALL)]; sys.exit(1 if bad else 0)" && echo PASS || { echo "FAIL: rb_update_field or guard_update_field missing dot-prefix defense — see guard-354"; exit 1; }`

   # SGR7: VALID_USER_LEG_SCOPES defined with at-minimum the shipped grants
   Check: aspirations.py defines VALID_USER_LEG_SCOPES including commit AND push. Bash: `py -3 -c "import re,sys; src=open('core/scripts/aspirations.py').read(); m=re.search(r'VALID_USER_LEG_SCOPES\s*=\s*\{([^}]*)\}', src, re.DOTALL); scopes=set(re.findall(r'\"([a-z-]+)\"', m.group(1))) if m else set(); sys.exit(0 if {'commit','push'}.issubset(scopes) else 1)" && echo PASS || { echo "FAIL: VALID_USER_LEG_SCOPES missing 'commit' or 'push' — grants cannot route correctly"; exit 1; }`

   # SGR8: validate_goal enforces membership in VALID_USER_LEG_SCOPES
   Check: validate_goal body references VALID_USER_LEG_SCOPES. Bash: `py -3 -c "import re,sys; src=open('core/scripts/aspirations.py').read(); m=re.search(r'def validate_goal\b.*?(?=\ndef |\Z)', src, re.DOTALL); sys.exit(0 if (m and 'VALID_USER_LEG_SCOPES' in m.group(0)) else 1)" && echo PASS || { echo "FAIL: validate_goal does not enforce VALID_USER_LEG_SCOPES membership — invalid scopes pass silently"; exit 1; }`

   # SGR9: _warn_missing_user_leg_scope wired into ALL THREE mutation sites (cmd_add_goal + cmd_add + cmd_update_goal)
   # Coverage gap is the routing bypass — missing one site means participants=[agent,user] can land without a warn.
   Check: helper wired into all three sites. Bash: `py -3 -c "import re,sys; src=open('core/scripts/aspirations.py').read(); bad=[fn for fn in ('cmd_add_goal','cmd_add','cmd_update_goal') if not re.search(r'def '+fn+r'\\b.*?_warn_missing_user_leg_scope', src, re.DOTALL)]; sys.exit(1 if bad else 0)" && echo PASS || { echo "FAIL: _warn_missing_user_leg_scope not wired into all three mutation sites — routing bypass possible"; exit 1; }`

   # SGR10: goal-schemas.md documents user_leg_scope
   Check: goal-schemas.md documents user_leg_scope. Bash: `grep -q 'user_leg_scope' core/config/conventions/goal-schemas.md && echo PASS || { echo "FAIL: goal-schemas.md missing user_leg_scope documentation — schema drift"; exit 1; }`

   # SGR11: rb-403 + guard-354 registered and active
   Check: rb-403 and guard-354 active. Bash: `bash core/scripts/reasoning-bank-read.sh --id rb-403 | grep -q '"status": "active"' && bash core/scripts/guardrails-read.sh --id guard-354 | grep -q '"status": "active"' && echo PASS || { echo "FAIL: rb-403 or guard-354 inactive"; exit 1; }`

   # SGR12: LOCKSTEP — every scope token in the Standing Grants table MUST appear in VALID_USER_LEG_SCOPES.
   # This is the core invariant. Reverse direction (enum scope with no grant) is intentionally allowed —
   # vocabulary can precede grants. But a grant whose scope token is not in the enum means goal filing
   # with that user_leg_scope will raise in validate_goal, breaking the routing path the grant was meant
   # to enable. If this check fails, either add the missing scope to VALID_USER_LEG_SCOPES or correct
   # the grant scope column.
   Bash (SGR12 lockstep): `bash core/scripts/world-cat.sh conventions/capability-routing.md | py -3 -c "
import re, sys
md = sys.stdin.read()
asp_src = open('core/scripts/aspirations.py').read()
m = re.search(r'VALID_USER_LEG_SCOPES\s*=\s*\{([^}]*)\}', asp_src, re.DOTALL)
enum_scopes = set(re.findall(r'\"([a-z-]+)\"', m.group(1))) if m else set()
sg = re.search(r'## Standing User Grants(.*?)(?=\n## |\Z)', md, re.DOTALL)
grant_scopes = set()
if sg:
    for row in re.finditer(r'\n\|\s*grant-\d+\s*\|\s*([^|]+?)\s*\|', sg.group(1)):
        # Strip parenthetical (e.g. 'commit, push (all repos, all branches)' -> 'commit, push ')
        # so inner commas do not leak false scope tokens like 'all'.
        cell = re.sub(r'\([^)]*\)', '', row.group(1))
        for tok in re.split(r'[,;]', cell):
            t = tok.strip()
            if re.match(r'^[a-z][a-z-]*\$', t):
                grant_scopes.add(t)
missing = grant_scopes - enum_scopes
if missing:
    print(f'FAIL: Standing Grants scope tokens not in VALID_USER_LEG_SCOPES: {sorted(missing)}'); sys.exit(1)
print(f'PASS: {len(grant_scopes)} grant scope(s) all covered by VALID_USER_LEG_SCOPES')
"`
   # Pipe form (outer-bash reads file, stdin -> py) is the right shape for Python+bash
   # composition on this mixed Windows/WSL host: the outer bash already has the correct
   # path-resolution context (Git-Bash handles C:/... paths natively), whereas a nested
   # subprocess.run(['bash', ...]) from Windows Python invokes WSL bash, which needs
   # /mnt/c/... and fails on the Windows-style paths that _paths.sh exports. Prefer this
   # pattern any time a check needs to flow data between Python and bash wrappers —
   # do one thing per process, compose with pipes.

   # Encoding-Starvation Detector evidence checks (Section ESD — g-248-29, rb-437, guard-371/372/373)
   # productivity-stop-gate.sh's encoding_ratio was reworked 2026-04-21 from a gap counter
   # over tree writes only to a session-total artifact count. These five checks lock the
   # new invariants: single source of truth for session_start (WM, no fallback), seed
   # coverage across every /start entry path, fail-loud wrappers, and dead-formula removal.

   # ESD1: session_artifacts_count.py has no handoff.yaml fallback path in executable code.
   # Fallback fabrication was the original bug — a plausible fabricated cutoff produced
   # encoding_ratio=1.0 and silently disabled the gate. The module docstring intentionally
   # names 'handoff' as the DO-NOT-TOUCH anti-pattern, so a naive grep false-matches the
   # guidance itself. The check strips the docstring + # comments first, then greps the
   # remaining executable code. Any 'handoff' in code re-opens the bug class (rb-437, guard-372).
   Check: session_artifacts_count.py code has no handoff reference. Bash (ESD1 code-only grep): `py -3 -c "
import ast, re, sys
src = open('core/scripts/session_artifacts_count.py', encoding='utf-8').read()
doc = ast.get_docstring(ast.parse(src)) or ''
stripped = src.replace(doc, '') if doc else src
stripped = re.sub(r'#.*$', '', stripped, flags=re.M)
if 'handoff' in stripped:
    print('FAIL: session_artifacts_count.py executable code references handoff — fallback re-introduced (rb-437, guard-372)'); sys.exit(1)
print('PASS')
"`

   # ESD2: /start seeds WM.session_start in exactly 6 locations.
   # IDLE->reader, IDLE->assistant, IDLE->autonomous, UNINITIALIZED C4 reader,
   # UNINITIALIZED C8 assistant, UNINITIALIZED C8 autonomous. /boot deliberately not
   # touched (WM survives autocompact; re-seeding would clobber cross-restart continuity).
   Check: 6 wm-set session_start seeds in start/SKILL.md. Bash: `n=$(grep -c 'wm-set.sh session_start' .claude/skills/start/SKILL.md); [ "$n" = "6" ] && echo PASS || { echo "FAIL: start/SKILL.md has $n wm-set.sh session_start seeds, expected 6 — entry path missing (rb-437)"; exit 1; }`

   # ESD3: session-artifacts-count.sh wrapper exits 2 without MIND_AGENT.
   # _paths.sh has a first-available-conf fallback that could probe the wrong agent's WM.
   # The wrapper must refuse rather than silently cross-probe.
   Check: wrapper fail-loud on missing agent. Bash: `env -u MIND_AGENT bash core/scripts/session-artifacts-count.sh >/dev/null 2>&1; rc=$?; [ "$rc" = "2" ] && echo PASS || { echo "FAIL: session-artifacts-count.sh exit=$rc without MIND_AGENT, expected 2 — cross-agent misprobe possible"; exit 1; }`

   # ESD4: session_artifacts_count.py exits 2 when WM.session_start is unset.
   # Fail-loud contract replacing the silent fabrication. Stop-gate consumer treats
   # exit-2 as 0 artifacts (starvation direction) — correct for a gate.
   Check: helper fail-loud on null session_start. Bash (ESD4 fail-loud): `MIND_AGENT=alpha py -3 -c "
import sys, subprocess, yaml
sys.path.insert(0, 'core/scripts')
from _paths import AGENT_DIR
wm = AGENT_DIR / 'session' / 'working-memory.yaml'
orig = wm.read_text(encoding='utf-8') if wm.exists() else None
data = yaml.safe_load(orig) if orig else {}
data['session_start'] = None
wm.write_text(yaml.safe_dump(data), encoding='utf-8')
try:
    r = subprocess.run([sys.executable, 'core/scripts/session_artifacts_count.py'], capture_output=True, text=True)
    assert r.returncode == 2, f'expected exit 2, got {r.returncode}'
    assert 'session_start' in (r.stderr or ''), 'stderr must mention session_start'
    print('PASS')
finally:
    if orig is not None: wm.write_text(orig, encoding='utf-8')
" || { echo "FAIL: session_artifacts_count.py does not fail-loud on null session_start (guard-372)"; exit 1; }`

   # ESD5: productivity-stop-gate.sh no longer references the gap-counter formula.
   # The old formula 'goals_since_last_tree_update' produced ratio=0.49 on 3-artifact
   # sessions. Any re-introduction reverts the fix (rb-437).
   Check: dead formula removed. Bash: `grep -q 'goals_since_last_tree_update' core/scripts/productivity-stop-gate.sh && { echo "FAIL: productivity-stop-gate.sh still references goals_since_last_tree_update — gap-counter formula partially reverted (rb-437)"; exit 1; } || echo PASS`

   # ESD6: wm.py::cmd_reset preserves SESSION_IDENTITY_FIELDS across autocompact.
   # Before the 2026-04-23 fix (g-240-68), wm-reset unconditionally zeroed session_start,
   # and autocompact → consolidate → wm-reset → /boot (no re-seed) left session_start=null
   # for the remainder of the session. The gate then treated every iteration as 0 artifacts.
   # Removing preservation re-opens that bug class. SESSION_IDENTITY_FIELDS is the single
   # source of truth for which fields survive reset.
   Check: SESSION_IDENTITY_FIELDS defined in wm.py. Bash: `grep -q 'SESSION_IDENTITY_FIELDS = {"session_start"}' core/scripts/wm.py && echo PASS || { echo "FAIL: wm.py SESSION_IDENTITY_FIELDS missing or mutated — preservation contract broken (g-240-68)"; exit 1; }`
   Check: cmd_reset reads existing WM before building template. Bash: `py -3 -c "import re,sys; s=open('core/scripts/wm.py',encoding='utf-8').read(); m=re.search(r'def cmd_reset\(args\):.*?def cmd_', s, re.DOTALL); body=m.group(0) if m else ''; sys.exit(0 if 'existing = read_wm()' in body and 'SESSION_IDENTITY_FIELDS' in body else 1)" && echo PASS || { echo "FAIL: cmd_reset missing existing-WM read or SESSION_IDENTITY_FIELDS loop (g-240-68)"; exit 1; }`
   Check: wm-reset preserves session_start round-trip. Bash (ESD6 functional — pure bash to avoid py→subprocess→bash hook-cascade hangs on Windows): `MIND_AGENT=alpha bash -c 'set -e; WM=$(MIND_AGENT=alpha bash core/scripts/_paths.sh >/dev/null 2>&1; echo agents/alpha/session/working-memory.yaml); ORIG=$(cat "$WM" 2>/dev/null || true); trap '"'"'[ -n "$ORIG" ] && printf "%s" "$ORIG" > "$WM"'"'"' EXIT; echo "\"TEST-ESD6\"" | MIND_AGENT=alpha bash core/scripts/wm-set.sh session_start >/dev/null; MIND_AGENT=alpha bash core/scripts/wm-reset.sh >/dev/null; AFTER=$(MIND_AGENT=alpha bash core/scripts/wm-read.sh session_start 2>/dev/null); [ "$AFTER" = "TEST-ESD6" ] && echo PASS || { echo "FAIL: wm-reset did not preserve session_start (got: $AFTER) — SESSION_IDENTITY_FIELDS contract broken (g-240-68)"; exit 1; }'`

   # ESD7: wm-clear-identity.sh is the single authorized identity-clear site, wired into /stop.
   # /stop's graceful-stop D4.5 must call this wrapper. If removed, session_start persists
   # across /stop — "next /start re-seeds" becomes the only reset path, which masks
   # ended-session state with stale identity.
   Check: wrapper exists and executable. Bash: `test -x core/scripts/wm-clear-identity.sh && echo PASS || { echo "FAIL: core/scripts/wm-clear-identity.sh missing or not executable — /stop identity-clear path broken (g-240-68)"; exit 1; }`
   Check: graceful-stop D4.5 calls it. Bash: `grep -q 'wm-clear-identity.sh' .claude/skills/aspirations-graceful-stop/SKILL.md && echo PASS || { echo "FAIL: aspirations-graceful-stop missing wm-clear-identity.sh call — /stop does not clear session identity (g-240-68)"; exit 1; }`
   Check: clear-identity subcommand wired in DISPATCH. Bash: `grep -q '"clear-identity": cmd_clear_identity' core/scripts/wm.py && echo PASS || { echo "FAIL: wm.py DISPATCH missing clear-identity — wrapper is dead (g-240-68)"; exit 1; }`

   # ESD8: structural_progress axis wired in three places (Lane D Magic Wand #1, 2026-05-08).
   # The structural axis credits tree-maintenance ops + work_class=framework goals so
   # deep framework sessions don't score zero. The three-place contract is enforced by
   # KeyError fail-open in productivity-stop-gate.sh — missing any one place crashes
   # the gate (correct fail-open: a broken axis must not silently disable scoring).
   Check: ARTIFACT_KEYS tuple contains structural_progress. Bash: `grep -q '"structural_progress"' core/scripts/productivity-stop-gate.sh && echo PASS || { echo "FAIL: productivity-stop-gate.sh ARTIFACT_KEYS missing structural_progress — three-place contract broken (Lane D Magic Wand #1)"; exit 1; }`
   Check: aspirations.yaml artifact_weights has structural_progress. Bash: `grep -q '^[[:space:]]*structural_progress:' core/config/aspirations.yaml && echo PASS || { echo "FAIL: aspirations.yaml productivity_gate.artifact_weights missing structural_progress — three-place contract broken (Lane D Magic Wand #1)"; exit 1; }`
   Check: session_artifacts_count.py defines count_structural_progress. Bash: `grep -q 'def count_structural_progress' core/scripts/session_artifacts_count.py && echo PASS || { echo "FAIL: session_artifacts_count.py missing count_structural_progress() — three-place contract broken"; exit 1; }`
   Check: structural_progress emitted in JSON output. Bash: `grep -q '"structural_progress":' core/scripts/session_artifacts_count.py && echo PASS || { echo "FAIL: session_artifacts_count.py main() does not emit structural_progress key"; exit 1; }`

   # ESD8b: ARTIFACT_KEYS ↔ breakdown drift check (g-115-422, /encode-session 2026-05-08 Lane 5).
   # Every key in ARTIFACT_KEYS tuple MUST appear in the breakdown f-string (counts['<key>'])
   # so per-key contribution stays visible in the gate log. Lane D added structural_progress
   # to ARTIFACT_KEYS but the breakdown was missed for one commit — KeyError fail-open caught
   # the math sink, but the observability sink had no defense (rb-731). This regex check is
   # the observability-sink defense: extracts both halves and asserts no missing keys.
   Check: every ARTIFACT_KEYS member appears in breakdown counts[...]. Bash (ESD8b ARTIFACT_KEYS↔breakdown drift): `py -3 -c "
import re, sys
src = open('core/scripts/productivity-stop-gate.sh', encoding='utf-8').read()
tup = re.search(r'ARTIFACT_KEYS\s*=\s*\(([^)]*)\)', src)
if not tup:
    print('FAIL: ARTIFACT_KEYS tuple not found in productivity-stop-gate.sh'); sys.exit(1)
keys = set(re.findall(r'\"([a-z_]+)\"', tup.group(1)))
brk = re.search(r'breakdown\s*=\s*\((.*?)print\(', src, re.S)
if not brk:
    print('FAIL: breakdown f-string block not found in productivity-stop-gate.sh'); sys.exit(1)
referenced = set(re.findall(r\"counts\[\'([a-z_]+)\'\]\", brk.group(1)))
missing = keys - referenced
if missing:
    print(f'FAIL: ARTIFACT_KEYS members missing from breakdown: {sorted(missing)} (rb-731 observability-drift class)'); sys.exit(1)
print(f'PASS: all {len(keys)} ARTIFACT_KEYS members appear in breakdown')
"`

   # ESD9: stop_mode_caps section in tree.yaml + /tree maintain --stop-mode wired
   # (Lane D Magic Wand #4, 2026-05-08).
   Check: tree.yaml has stop_mode_caps section. Bash: `grep -q '^stop_mode_caps:' core/config/tree.yaml && echo PASS || { echo "FAIL: core/config/tree.yaml missing stop_mode_caps section — Lane D Magic Wand #4 reverted"; exit 1; }`
   Check: stop_mode_caps has max_decompose_per_invocation. Bash: `grep -q 'max_decompose_per_invocation:.*5' core/config/tree.yaml && echo PASS || { echo "FAIL: stop_mode_caps.max_decompose_per_invocation missing or not 5"; exit 1; }`
   Check: tree.py accepts --stop-mode flag. Bash: `grep -q -- '--stop-mode' core/scripts/tree.py && echo PASS || { echo "FAIL: tree.py missing --stop-mode argparse option"; exit 1; }`
   Check: tree.py records last_stop_mode_at. Bash: `grep -q 'last_stop_mode_at' core/scripts/tree.py && echo PASS || { echo "FAIL: tree.py cmd_record_maintenance does not write last_stop_mode_at"; exit 1; }`
   Check: /tree SKILL.md documents --stop-mode sub-command. Bash: `grep -q '/tree maintain --stop-mode' .claude/skills/tree/SKILL.md && echo PASS || { echo "FAIL: /tree SKILL.md missing --stop-mode section"; exit 1; }`
   Check: consolidation-housekeeping invokes --stop-mode under stop_mode. Bash: `grep -q 'tree maintain --stop-mode' core/config/consolidation-housekeeping.md && echo PASS || { echo "FAIL: consolidation-housekeeping.md Step 6 does not invoke --stop-mode"; exit 1; }`

   # ESD10: pending-questions sweep wired (Lane B, 2026-05-08).
   Check: sweep script exists. Bash: `test -f core/scripts/pending-questions-sweep.py && test -f core/scripts/pending-questions-sweep.sh && echo PASS || { echo "FAIL: pending-questions-sweep.py or .sh missing — Lane B reverted"; exit 1; }`
   Check: sweep wired into Step 2.8. Bash: `grep -q 'pending-questions-sweep.sh' core/config/consolidation-housekeeping.md && echo PASS || { echo "FAIL: consolidation-housekeeping.md Step 2.8 does not invoke pending-questions-sweep"; exit 1; }`
   Check: sweep produces valid JSON on alpha's data. Bash: `MIND_AGENT=alpha bash core/scripts/pending-questions-sweep.sh stats 2>/dev/null | py -3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get('subcommand')=='stats' else 1)" && echo PASS || { echo "FAIL: pending-questions-sweep.sh stats output not valid JSON"; exit 1; }`

   # Spark-question handler pairing invariant (Section SQH — guard-473, sq-018 self-application
   # from encode-session-2026-05-07 / g-240-86 / g-115-407). Right-direction probe: every handler
   # block (`Handler for sq-NNN` or `When sq-NNN fires`) in aspirations-spark/SKILL.md must reference
   # a sq-NNN that exists in the active spark catalog. Reverse direction is intentionally NOT
   # checked — most active sparks are evaluated by the generic loop, only structurally complex
   # sparks (multi-step action, side-effect writes, skill invocations) need explicit handlers.
   # A handler referencing a non-existent sq is dead code that silently never fires. Catalog
   # source: spark-questions-read.sh --all (returns active+candidates as JSON array).
   Check: every aspirations-spark handler references an existing spark-questions catalog entry.
     Bash: `py -3 -c "
import json, re, subprocess, sys
skill = open('.claude/skills/aspirations-spark/SKILL.md', encoding='utf-8').read()
# Single capturing group avoids nested-group tuple flatten issue (regex drift fix, g-115-407).
handlers = set(re.findall(r'(?:Handler for |When )(sq-(?:c\d+|\d+))', skill))
if not handlers:
    print('FAIL: no handlers detected — regex drift in aspirations-spark/SKILL.md (Section SQH)'); sys.exit(1)
out = subprocess.check_output(['bash', 'core/scripts/spark-questions-read.sh', '--all'], text=True)
catalog = {r['id'] for r in json.loads(out)}
missing = sorted(handlers - catalog)
if missing:
    print(f'FAIL: handler(s) reference non-existent spark questions: {missing} (Section SQH — guard-473; dead handler code, fix or retire)'); sys.exit(1)
print(f'PASS: all {len(handlers)} aspirations-spark handlers reference active catalog entries')
"`

   # Spark-question DUPLICATE handler-block detection (Section SQH-2 — g-115-449,
   # 2026-05-08). Catalog-membership (Section SQH) only validates that handler ids
   # exist in the active catalog; it does NOT detect when the SAME id appears in
   # multiple handler blocks with conflicting bodies. When sq-c03→sq-014 and
   # sq-c06→sq-015 catalog renames landed (g-115-448) without retiring the older
   # idea-generation / integration-path-coverage blocks, two ids ended up with
   # 2 handler blocks each — the dispatcher fires only one (parse-order dependent)
   # and the other's logic is dead code with conflicting semantics.
   #
   # Pattern: handler blocks start with bold-tagged id at line start, either
   # `**sq-NNN**:` (declaration) or `**Handler for sq-NNN**` (alternate form).
   # Counter > 1 = duplicate. Section SQH alone cannot catch this.
   Check: no duplicate handler blocks in aspirations-spark/SKILL.md.
     Bash: `py -3 -c "
import re, sys
from collections import Counter
s = open('.claude/skills/aspirations-spark/SKILL.md', encoding='utf-8').read()
# Match block-header forms: '**sq-NNN**:' (with optional '(candidate for promotion)' suffix)
# OR '**Handler for sq-NNN**' (alternate-form header used by some blocks).
matches = re.findall(r'^\*\*(?:Handler for )?(sq-(?:c\d+|\d+))\*\*', s, re.MULTILINE)
if not matches:
    print('FAIL: no handlers detected — regex drift in aspirations-spark/SKILL.md (Section SQH-2; mirrors SQH guard at L3508)'); sys.exit(1)
counts = Counter(matches)
dups = {sid: n for sid, n in counts.items() if n > 1}
if dups:
    print(f'FAIL: duplicate handler blocks in aspirations-spark/SKILL.md: {dups} (Section SQH-2 — g-115-449; dispatcher fires only one block per id, the others are dead code with potentially conflicting semantics; resolve by deleting the stale block OR re-promoting one to a new sq-NNN with catalog entry)'); sys.exit(1)
print(f'PASS: no duplicate handler blocks ({len(counts)} unique sq-ids across {sum(counts.values())} handler blocks)')
"`

   # Orphan-root sweep detector exists + is executable (Section ORS — g-115-431,
   # 2026-05-08). The 2026-05-08 cruft audit found Mode A/B path-resolution drift
   # produced cruft at <world-parent>/ and <dirname PROJECT_ROOT>/ that survived
   # ~3 weeks before discovery. core/scripts/orphan-root-sweep.sh is the periodic
   # advisory detector; absent or non-executable, the recurring goal g-115-451 silently
   # cannot fire and drift returns. See world/knowledge/tree/system/system-constraints-loop/external-path-resolution-cruft.md.
   Check: orphan-root-sweep.sh exists and runs cleanly on the current state.
     Bash: `test -x core/scripts/orphan-root-sweep.sh && echo PASS || { echo "FAIL: core/scripts/orphan-root-sweep.sh missing or not executable (Section ORS — g-115-431)"; exit 1; }`
   Check: orphan-root-sweep.sh exits 0 (advisory-only contract).
     Bash: `bash core/scripts/orphan-root-sweep.sh >/dev/null 2>&1 && echo PASS || { echo "FAIL: orphan-root-sweep.sh exited non-zero — must be advisory-only per its contract"; exit 1; }`

   # Cargo-cult interval-calibration integrity (Section CCI — shape-recurring trap fix, 2026-04-22)
   # Anti-regression probes for BUG-A (Phase 0.5d dedupe flood), BUG-B (write-order
   # ratchet), BUG-C (hardcoded multipliers), rb-335 duplicate-key, and the sweep.
   Check: Phase 0.5d absent. Bash: `grep -c "Phase 0.5d" .claude/skills/aspirations-precheck/SKILL.md` → verify 0
     # BUG-A: Phase 0.5d bypassed batch_audit_dedupe_hours (enforced only in recurring-close.sh).
   Check: precheck no --audit-all caller. Bash: `grep -c "cargo-cult-detector.py --audit-all" .claude/skills/aspirations-precheck/SKILL.md` → verify 0
     # Same BUG-A: recurring-close.sh is the single authorized --audit-all caller.
   Check: write-order is pointer-first. Bash: `py -3 -c "import re,sys; s=open('core/scripts/cargo-cult-detector.py',encoding='utf-8').read(); m=re.search(r'def update_interval_hours.*?\nreturn True', s, re.DOTALL); body=m.group(0) if m else s[s.find('def update_interval_hours'):s.find('def reset_consecutive_routine')]; i_orig=body.find('original_interval_hours'); i_int=body.find('interval_hours', body.find(':')); sys.exit(0 if 0 < i_orig < i_int else 1)" && echo PASS || echo "FAIL: BUG-B regression — interval_hours written before original"`
     # BUG-B: original_interval_hours is the provenance anchor for cap_ratio; must be written FIRST.
   Check: no hardcoded 2.0/4.0 in batch audit. Bash: `grep -cE "interval_h.*\* 2\.0|original \* 4\.0" core/scripts/cargo-cult-detector.py` → verify 0
     # BUG-C: _propose_new_interval must read multiplier/cap_ratio from cfg.
   Check: _propose_new_interval takes cfg. Bash: `grep -cE "def _propose_new_interval.goal.*cfg" core/scripts/cargo-cult-detector.py` → verify ≥1
     # BUG-C structural: signature forces callers to pass cfg — no default-fallback.
   Check: single cargo_cult block. Bash: `grep -c "^cargo_cult:" core/config/aspirations.yaml` → verify 1
     # rb-335: yaml.safe_load silently keeps only the last duplicate key.
   Check: sweep script exists. Bash: `test -f core/scripts/recurring-precondition-sweep.py && echo PASS || echo "FAIL: shape-recurring trap sweep missing"`
   Check: Phase 0.5c invokes sweep. Bash: `grep -c "recurring-precondition-sweep.py" .claude/skills/aspirations-precheck/SKILL.md` → verify ≥1
   # CCI-string (g-241-04 / rb-441 — string-precondition twin of the structured sweep):
   # When a recurring goal's STRING precondition fails after the time gate has elapsed,
   # aspirations-select Phase 2.2 must advance lastAchievedAt to prevent overdue_ratio
   # runaway. Without this branch, a string precondition that consistently returns
   # "not met" leaves the goal pinned at increasing urgency every cycle — same
   # corruption pattern the structured sweep already prevents, but reachable via the
   # LLM-evaluated path the sweep cannot scan. Probes:
   Check: aspirations-select Phase 2.2 has the string-precondition-fail branch advancing lastAchievedAt.
     Bash: `grep -cE "STRING-PC-FAIL|lastAchievedAt advanced" .claude/skills/aspirations-select/SKILL.md` → verify ≥1
   Check: aspirations-select Phase 2.2 explicitly mentions consecutive_routine MUST NOT increment.
     Bash: `grep -cE "MUST NOT increment consecutive_routine|consecutive_routine\s+(not|MUST NOT)" .claude/skills/aspirations-select/SKILL.md` → verify ≥1
     # If this fires zero, the prose may have drifted to allow consecutive_routine bumping —
     # which corrupts cargo-cult-detector's "this goal keeps getting closed cheaply" signal.
   Check: aspirations-select Phase 2.2 cross-references the structured sweep so the twin pair stays discoverable.
     Bash: `grep -cE "recurring-precondition-sweep|shape-recurring" .claude/skills/aspirations-select/SKILL.md` → verify ≥1

   # Productivity-gate weighted encoding + cool-down ladder (Section PGW — Fix D/E + fresh-eyes cleanup, 2026-04-22)
   # The productivity-stop-gate now uses per-artifact weights reflecting the framework's
   # durability hierarchy (tree=1.0, guards=0.9, rb/patsigs=0.8, hyp_*=0.6, experience=0.3)
   # and a 3-step cool-down ladder (30m/1h/2h) before hard stop. These checks lock the
   # drift pathways for Fix D (weighted counts), Fix E (cooldown), and the fresh-eyes
   # cleanup (SSOT + silent-failure fixes). See rb-456/457/458 and guard-382.

   # PGW1: three-way ARTIFACT_KEYS contract — gate script, counter output, and config
   # weights MUST all name the same eight keys (tree_writes, guards, rb, patsigs,
   # hyp_created, hyp_resolved, experience, forges). If they drift, the gate either
   # KeyErrors on config (fail-open exit) or silently undercounts new artifact kinds.
   Check: ARTIFACT_KEYS match across gate + counter + config. Bash: `py -3 -c "
import re, yaml, sys
gate = open('core/scripts/productivity-stop-gate.sh', encoding='utf-8').read()
m = re.search(r'ARTIFACT_KEYS\s*=\s*\((.*?)\)', gate, re.DOTALL)
gate_keys = set(k.strip().strip(chr(34)).strip(chr(39)) for k in m.group(1).split(',') if k.strip())
gate_keys.discard('')
counter = open('core/scripts/session_artifacts_count.py', encoding='utf-8').read()
counter_keys = set(re.findall(r'^\s+\"(tree_writes|guards|rb|patsigs|hyp_created|hyp_resolved|experience|forges)\":', counter, re.M))
cfg = yaml.safe_load(open('core/config/aspirations.yaml', encoding='utf-8'))
config_keys = set(cfg['productivity_gate']['artifact_weights'].keys())
if gate_keys == counter_keys == config_keys:
    print('PASS')
else:
    print(f'FAIL: ARTIFACT_KEYS drift — gate={gate_keys} counter={counter_keys} config={config_keys} (rb-456, Fix D)')
    sys.exit(1)
"`

   # PGW2: no DEFAULT_WEIGHTS fallback dict in the gate (SSOT violation — rb-458).
   # A Python-side dict mirroring config values silently drifts when config is tuned.
   # The gate MUST read config directly and fail open via KeyError.
   Check: no DEFAULT_WEIGHTS in gate. Bash: `grep -q 'DEFAULT_WEIGHTS' core/scripts/productivity-stop-gate.sh && { echo "FAIL: productivity-stop-gate.sh has DEFAULT_WEIGHTS dict — SSOT violation re-introduced (rb-458)"; exit 1; } || echo PASS`

   # PGW3: COOLDOWN_SLOT is hardcoded, not config-tunable. Making the slot name
   # configurable would let config and the aspirations/SKILL.md session_signals
   # initializer drift silently. The name MUST be 'productivity_cooldown_streak'.
   Check: COOLDOWN_SLOT constant exists in gate. Bash: `grep -q 'COOLDOWN_SLOT = "productivity_cooldown_streak"' core/scripts/productivity-stop-gate.sh && echo PASS || { echo "FAIL: productivity-stop-gate.sh missing hardcoded COOLDOWN_SLOT — config tunable risks drift (Fix E cleanup)"; exit 1; }`
   Check: no streak_slot config key. Bash: `grep -q 'streak_slot:' core/config/aspirations.yaml && { echo "FAIL: aspirations.yaml has streak_slot config — should be hardcoded (Fix E cleanup)"; exit 1; } || echo PASS`
   Check: SKILL.md initializer has productivity_cooldown_streak. Bash: `grep -q 'productivity_cooldown_streak' .claude/skills/aspirations/SKILL.md && echo PASS || { echo "FAIL: aspirations/SKILL.md session_signals initializer missing productivity_cooldown_streak (Fix E)"; exit 1; }`

   # PGW4: counter emits experience field. User directive 2026-04-22: "give credit
   # for the hard stuff it is doing." Experience records are baseline learning signal
   # (weight 0.3); missing them undercounts rich sessions.
   Check: counter outputs experience key. Bash: `grep -q '"experience":' core/scripts/session_artifacts_count.py && echo PASS || { echo "FAIL: session_artifacts_count.py not emitting experience count (Fix D cleanup, user directive 2026-04-22)"; exit 1; }`

   # PGW5: WM write return-codes checked in gate. Silent subprocess failure on the
   # streak counter write would cause stuck-at-level-0 infinite cooldown loops
   # (rb-457). Both _persist_loop_state and _write_blocked_sleep_until MUST check
   # r.returncode and bail on failure.
   Check: gate checks wm.py returncode. Bash: `py -3 -c "
import re, sys
src = open('core/scripts/productivity-stop-gate.sh', encoding='utf-8').read()
# Both helpers must have 'if r.returncode != 0' guard
persist = re.search(r'def _persist_loop_state.*?(?=^def |^if |^# ---)', src, re.DOTALL | re.M)
write_sleep = re.search(r'def _write_blocked_sleep_until.*?(?=^def |^if |^# ---)', src, re.DOTALL | re.M)
ok = all(m and 'r.returncode != 0' in m.group(0) for m in (persist, write_sleep))
print('PASS' if ok else 'FAIL: productivity-stop-gate.sh cooldown helpers missing returncode check — silent failure risk (rb-457)')
sys.exit(0 if ok else 1)
"`

   # PGW6: cooldown write order — streak FIRST, then sentinel (guard-382).
   # Reverse order (sentinel first) risks stuck-at-level-0 on partial failure.
   # Anchor the search on the cooldown-branch entry, then look for the first
   # occurrence of each call in the whole file from that anchor forward —
   # non-greedy regex between them would terminate at the bail-out sys.exit(0).
   Check: streak write precedes sentinel. Bash: `py -3 -c "
import sys
src = open('core/scripts/productivity-stop-gate.sh', encoding='utf-8').read()
anchor = src.find('if current_streak < len(cooldown_ladder):')
i_persist = src.find('_persist_loop_state(current_streak + 1)', anchor)
i_sleep   = src.find('_write_blocked_sleep_until(sleep_seconds)', anchor)
if anchor > 0 and 0 < i_persist < i_sleep:
    print('PASS')
else:
    print(f'FAIL: productivity-stop-gate.sh cooldown write order violates guard-382 (anchor={anchor} persist={i_persist} sleep={i_sleep})'); sys.exit(1)
"`

   # LLM-issued `py -3 <<PY` / `python3 <<EOF` heredocs in direct Bash tool calls have
   # been observed reclassifying as run_in_background: true on Windows (harness-side
   # bug — hook is innocent per bash-agent-inject.py:83-86). SKILL.md pseudocode that
   # primes the LLM to emit heredocs must be caught before it ships. Committed heredocs
   # inside core/scripts/*.sh are safe (subshell isolation) and excluded from this scan.
   Check: python-invocation.md Rule #5 exists. Bash: `grep -cE "^### 5\. NEVER.*heredoc" core/config/conventions/python-invocation.md` → verify 1
   Check: rb-433 exists and is active. Bash: `bash core/scripts/reasoning-bank-read.sh --active | py -3 -c "import json,sys; d=json.load(sys.stdin); print('PASS' if any(e['id']=='rb-433' for e in d) else 'FAIL')"`
   Check: guard-368 exists and is active. Bash: `bash core/scripts/guardrails-read.sh --active | py -3 -c "import json,sys; d=json.load(sys.stdin); print('PASS' if any(g['id']=='guard-368' for g in d) else 'FAIL')"`
   Check: no SKILL.md file primes heredoc Python invocation. Bash: `grep -rlE "(py -3|python3) *<<[A-Za-z_]+" .claude/skills/ --include='SKILL.md' | grep -v verify-learning | py -3 -c "import sys; lines=[l.strip() for l in sys.stdin if l.strip()]; print('PASS: 0 hits' if not lines else 'FAIL: '+str(len(lines))+' SKILL.md files prime heredoc Python: '+', '.join(lines))"`
     # Note: verify-learning itself is excluded from the grep because this check DESCRIBES the
     # forbidden pattern — that self-reference would always false-positive. All other SKILL.md
     # files must be clean. If a legitimate heredoc need arises, wrap it in a .sh script under
     # core/scripts/ per python-invocation.md Rule #5 workaround #3.

   # Store-Family Hardening (Section SFH — 2026-04-22, rb-434/435/444, guard-369/376, sig-006)
   # Two complementary invariants from session 56:
   # (1) Recurring-goal duration discipline — rb-434 (domain incident: 90s g-115-15
   #     session), rb-435 (portable principle: interval-duration coupling, applies_to=any),
   #     sig-006 (pattern signature), guard-369 (action gate). run-game-session/SKILL.md
   #     Duration Discipline section codifies "do not override --duration below 10min on
   #     recurring fires".
   # (2) JSONL store-family `created` immutability — rb-444 (store-family template drift).
   #     pattern-signatures: cmd_update/cmd_update_field gutted (H2 Wave 3, 2026-05-15);
   #     `created` immutability now enforced by store_registry.py (immutable_fields +
   #     created_field/created_stamp) + store.py set-field/append handlers.
   #     experience.py retains the family-CLI shape (_stamp_now, REQUIRED_FIELDS omits
   #     `created`, cmd_update_field rejects `created`). pipeline.py uses formed_date
   #     and is intentionally excluded.
   Check: rb-434 exists and is active. Bash: `bash core/scripts/reasoning-bank-read.sh --active | py -3 -c "import json,sys; d=json.load(sys.stdin); print('PASS' if any(e['id']=='rb-434' for e in d) else 'FAIL: rb-434 missing or inactive')"`
   Check: rb-435 exists and is active + applies_to=any. Bash: `bash core/scripts/reasoning-bank-read.sh --active | py -3 -c "import json,sys; d=json.load(sys.stdin); e=next((x for x in d if x['id']=='rb-435'), None); print('PASS' if e and e.get('applies_to')=='any' else 'FAIL: rb-435 missing, inactive, or applies_to != any')"`
   Check: rb-444 exists and is active. Bash: `bash core/scripts/reasoning-bank-read.sh --active | py -3 -c "import json,sys; d=json.load(sys.stdin); print('PASS' if any(e['id']=='rb-444' for e in d) else 'FAIL: rb-444 missing or inactive')"`
   Check: guard-369 exists and is active. Bash: `bash core/scripts/guardrails-read.sh --active | py -3 -c "import json,sys; d=json.load(sys.stdin); print('PASS' if any(g['id']=='guard-369' for g in d) else 'FAIL: guard-369 missing or inactive')"`
   Check: guard-376 exists and is active. Bash: `bash core/scripts/guardrails-read.sh --active | py -3 -c "import json,sys; d=json.load(sys.stdin); print('PASS' if any(g['id']=='guard-376' for g in d) else 'FAIL: guard-376 missing or inactive')"`
   Check: sig-006 active in pattern-signatures.jsonl. Bash: `source core/scripts/_paths.sh && grep -cE '"id":\s*"sig-006"' "$WORLD_PATH/pattern-signatures.jsonl"` → verify ≥1
     # Pattern signature — read directly from the jsonl; no --active flag exists for this store.
   Check: run-game-session Duration Discipline section present. Bash: `grep -c "^### Duration Discipline" .claude/skills/run-game-session/SKILL.md` → verify 1
     # rb-434/435/sig-006/guard-369 cross-references appear in this section; verifies the
     # section survived edits to run-game-session/SKILL.md.
   Check: pattern-signatures.py has _stamp_now helper. Bash: `grep -c "^def _stamp_now" core/scripts/pattern-signatures.py` → verify 1
   Check: experience.py has _stamp_now helper. Bash: `grep -c "^def _stamp_now" core/scripts/experience.py` → verify 1
   Check: pattern-signatures.py REQUIRED_FIELDS omits 'created'. Bash: `py -3 -c "import ast; tree=ast.parse(open('core/scripts/pattern-signatures.py',encoding='utf-8').read()); req=next((n for n in ast.walk(tree) if isinstance(n, ast.Assign) and any(t.id=='REQUIRED_FIELDS' for t in n.targets if isinstance(t, ast.Name))), None); fields=set(elt.value for elt in req.value.elts) if req else set(); print('PASS' if 'created' not in fields else 'FAIL: created in REQUIRED_FIELDS')"`
   Check: experience.py REQUIRED_FIELDS omits 'created'. Bash: `py -3 -c "import ast; tree=ast.parse(open('core/scripts/experience.py',encoding='utf-8').read()); req=next((n for n in ast.walk(tree) if isinstance(n, ast.Assign) and any(t.id=='REQUIRED_FIELDS' for t in n.targets if isinstance(t, ast.Name))), None); fields=set(elt.value for elt in req.value.elts) if req else set(); print('PASS' if 'created' not in fields else 'FAIL: created in REQUIRED_FIELDS')"`
   Check: pattern-signatures store_registry.py immutable_fields includes 'created'. Bash: `py -3 -c "import ast,sys; src=open('mind_api/src/store_registry.py',encoding='utf-8').read(); sys.exit(0 if '\"created\"' in src[src.find('\"pattern-signatures\"'):src.find('\"spark-questions\"')] and 'immutable_fields' in src[src.find('\"pattern-signatures\"'):src.find('\"spark-questions\"')] else 1)" && echo PASS || echo "FAIL: pattern-signatures StoreSpec missing immutable_fields with created"`
     # H2 Wave 3 migration (2026-05-15): cmd_update_field gutted from pattern-signatures.py;
     # `created` immutability now enforced by store_registry.py immutable_fields + store.py set-field handler.
   Check: experience.py cmd_update_field rejects 'created'. Bash: `py -3 -c "import re; s=open('core/scripts/experience.py',encoding='utf-8').read(); body=s[s.find('def cmd_update_field'):s.find('def ', s.find('def cmd_update_field')+1)]; import sys; sys.exit(0 if re.search(r'field\s*==\s*[\"\\x27]created[\"\\x27]', body) else 1)" && echo PASS || echo "FAIL: cmd_update_field does not reject 'created'"`
   Check: pattern-signatures store_registry.py has created_field + created_stamp (append stamps unconditionally). Bash: `py -3 -c "src=open('mind_api/src/store_registry.py',encoding='utf-8').read(); chunk=src[src.find('\"pattern-signatures\"'):src.find('\"spark-questions\"')]; import sys; sys.exit(0 if 'created_field=\"created\"' in chunk and 'created_stamp=_stamp_now' in chunk else 1)" && echo PASS || echo "FAIL: pattern-signatures StoreSpec missing created_field/created_stamp"`
     # H2 Wave 3 migration (2026-05-15): cmd_update gutted from pattern-signatures.py;
     # `created` preservation now enforced by store_registry.py created_field + store.py append handler
     # (unconditional overwrite) and replace handler (preserves existing via same field).

   # Consolidation Health writer checks (Section CH — 2026-04-22, rb-459, guard-383)
   # Regression guard for the signal-lifecycle-gate finding: slot had 3 readers and 0 writers.
   # A future editor who restores the no-agent fail-open branch re-opens the silent-world-only bug.
   Check: `core/scripts/consolidation-health.sh` exists and is executable
   Check: `core/scripts/consolidation-health.py` has the no-agent fail-hard guard. Bash: `grep -c 'MIND_AGENT not set' core/scripts/consolidation-health.py` → verify ≥1
   Check: `core/scripts/consolidation-health.py` does NOT return a snapshot when MIND_AGENT is unset. Bash: `grep -c 'no agent bound' core/scripts/consolidation-health.py` → verify 0 (regression guard: the old note field that lied about scope must not reappear)
   Check: precheck writer wiring present. Bash: `grep -c 'bash core/scripts/consolidation-health.sh --write' .claude/skills/aspirations-precheck/SKILL.md` → verify ≥1 (the single writer call site)
   Check: 3 readers still consume the slot. Bash: `grep -l 'consolidation_health' .claude/skills/aspirations-evolve/SKILL.md .claude/skills/aspirations-select/SKILL.md .claude/skills/create-aspiration/SKILL.md | wc -l` → verify 3

   # Test-Harness Hygiene checks (Section TH — 2026-04-22, rb-460, rb-461, guard-384)
   # Regression guard for the silent-pass-as-fail finding. If these fail, tests
   # are measuring the fail-open path (rb-347) instead of the policy logic.
   Check: `core/scripts/test-capability-gate.sh` auto-binds MIND_AGENT before running. Bash: `grep -c 'AUTO_BOUND=' core/scripts/test-capability-gate.sh` → verify ≥1
   Check: `core/scripts/test-capability-gate.sh` uses sys.executable, not argv-conditional. Bash: `grep -c 'sys.executable' core/scripts/test-capability-gate.sh` → verify ≥1
   Check: `core/scripts/test-capability-gate.sh` has NO dead py-fallback branch. Bash: `grep -cE '"python" in sys.argv\[0\]' core/scripts/test-capability-gate.sh` → verify 0 (dead code guard — the heredoc marker "-" never matches)
   Check: `core/scripts/test-capability-gate.sh` has NO outer py-launcher fallback. Bash: `grep -cE 'command -v py\b' core/scripts/test-capability-gate.sh` → verify 0
     # Word-boundary regex matches `command -v py` only (not `python`/`python3`),
     # so it catches the distinctive first rung of the removed 3-way fallback without
     # multi-line false-negatives (the original `.*` across 3 elif lines always
     # returned 0 even with the fallback restored — grep is line-by-line by default).
   Check: test run passes all 14 cases. Bash: `bash core/scripts/test-capability-gate.sh 2>&1 | tail -1 | grep -cE 'PASS: 14 +FAIL: 0'` → verify 1

   # Domain-Leak Scanner Skip Mechanisms (Section DL-2 — 2026-04-22)
   # The scanner grew three legitimate skip mechanisms for test fixtures, meta-doc
   # self-references, and per-file exempt markers. A future editor who strips any
   # of these in the name of "simplification" re-creates false positives that drove
   # the 13-hit 2026-04-22 flurry of edits.
   Check: test-*.sh/test-*.py fixture skip present. Bash: `grep -c '/test-\[A-Za-z0-9_-\]' core/scripts/domain-leak-check.sh` → verify ≥1
   Check: verify-learning/SKILL.md self-reference skip present. Bash: `grep -c 'verify-learning/SKILL.md' core/scripts/domain-leak-check.sh` → verify ≥1
   Check: per-file domain-leak-exempt marker scan present. Bash: `grep -c 'domain-leak-exempt:' core/scripts/domain-leak-check.sh` → verify ≥1
   Check: end-to-end scanner exits clean. Bash: `bash core/scripts/domain-leak-check.sh 2>&1 | tail -1 | grep -c 'CLEAN: No domain terms'` → verify 1

   # New rb/guard existence checks (2026-04-22)
   Check: rb-459 exists and is active. Bash: `bash core/scripts/reasoning-bank-read.sh --active | py -3 -c "import json,sys; d=json.load(sys.stdin); print('PASS' if any(e['id']=='rb-459' for e in d) else 'FAIL: rb-459 missing or inactive')"`
   Check: rb-460 exists and is active. Bash: `bash core/scripts/reasoning-bank-read.sh --active | py -3 -c "import json,sys; d=json.load(sys.stdin); print('PASS' if any(e['id']=='rb-460' for e in d) else 'FAIL: rb-460 missing or inactive')"`
   Check: rb-461 exists and is active. Bash: `bash core/scripts/reasoning-bank-read.sh --active | py -3 -c "import json,sys; d=json.load(sys.stdin); print('PASS' if any(e['id']=='rb-461' for e in d) else 'FAIL: rb-461 missing or inactive')"`
   Check: guard-383 exists and is active. Bash: `bash core/scripts/guardrails-read.sh --active | py -3 -c "import json,sys; d=json.load(sys.stdin); print('PASS' if any(g['id']=='guard-383' for g in d) else 'FAIL: guard-383 missing or inactive')"`
   Check: guard-384 exists and is active. Bash: `bash core/scripts/guardrails-read.sh --active | py -3 -c "import json,sys; d=json.load(sys.stdin); print('PASS' if any(g['id']=='guard-384' for g in d) else 'FAIL: guard-384 missing or inactive')"`
   Check: rb-462 exists and is active. Bash: `bash core/scripts/reasoning-bank-read.sh --active | py -3 -c "import json,sys; d=json.load(sys.stdin); print('PASS' if any(e['id']=='rb-462' for e in d) else 'FAIL: rb-462 missing or inactive')"`
   Check: guard-385 exists and is active. Bash: `bash core/scripts/guardrails-read.sh --active | py -3 -c "import json,sys; d=json.load(sys.stdin); print('PASS' if any(g['id']=='guard-385' for g in d) else 'FAIL: guard-385 missing or inactive')"`

   # Capability-Retrieval + Per-Unit Aggregation (Section CR — 2026-04-22, guard-381, rb-455, session-57)
   # Regression guards for the session-57 encoding: user-flagged musing ("I've
   # never seen a live session — I'm optimizing an axis I can't experience")
   # revealed a retrieval failure at narration time. /state-replay IS session
   # experience; agents/alpha/self.md Agent-Provisionable Actions already names it.
   # The fix lives in three places: narration-time guardrail (guard-381),
   # per-unit aggregation rule (rb-455 generalizes rb-451 from C1 to all OHS
   # axes), and the tree node that readers land on when they retrieve
   # "state-replay". If any of these drift, the session-57 lesson is lost.
   Check: guard-381 exists and is active. Bash: `bash core/scripts/guardrails-read.sh --active | py -3 -c "import json,sys; d=json.load(sys.stdin); print('PASS' if any(g['id']=='guard-381' for g in d) else 'FAIL: guard-381 missing or inactive')"`
   Check: rb-455 exists and is active with applies_to=domain. Bash: `bash core/scripts/reasoning-bank-read.sh --active | py -3 -c "import json,sys; d=json.load(sys.stdin); e=[x for x in d if x['id']=='rb-455']; print('PASS' if e and e[0].get('applies_to')=='domain' else 'FAIL: rb-455 missing, inactive, or applies_to!=domain')"`
   Check: rb-451 back-links to rb-455 via tags. Bash: `bash core/scripts/reasoning-bank-read.sh --id rb-451 | py -3 -c "import json,sys; e=json.load(sys.stdin); print('PASS' if 'generalized-by-rb-455' in (e.get('tags') or []) else 'FAIL: rb-451 missing generalized-by-rb-455 back-tag')"`
   Check: state-replay-tool tree node cites guard-381 AND rb-455. Bash: `grep -c 'guard-381\|rb-455' "$(source core/scripts/_paths.sh && echo "$WORLD_DIR/knowledge/tree/intelligence/npc-intelligence/npc-evaluation/state-replay-tool.md")"` → verify ≥2
   Check: every gate script bare-imports _gate_log (no try/except stub fallback). Bash: `grep -lE 'def _gate_log\(\*_args' core/scripts/*-gate.py core/scripts/cargo-cult-detector.py 2>/dev/null | wc -l` → verify 0 (any match means a stub re-appeared)
   Check: capability-gate.py _decision ladder has NO duplicate 'pass' branch. Bash: `grep -cE 'approval_kind == "evidence":\s*$' core/scripts/capability-gate.py` → verify ≤1 (one reference in comments is fine; a second branch in the _decision ladder would indicate the dead-branch regression)
   Check: learning-routing drift stable or ratcheted down. Bash: `bash core/scripts/learning-routing-ratchet.sh` → expect exit 0 and a status line starting with `[learning-routing-ratchet] STABLE:` or `RATCHETED:`. A `REGRESSED:` line means new dangling cross-refs were introduced since the last baseline recorded in `meta/audit-baselines.yaml` — run `bash core/scripts/learning-routing-audit.sh` to inspect, then `bash core/scripts/learning-routing-repair.sh --apply` if the new drift is historical. Hard-gate with `VERIFY_LEARNING_DRIFT_HARD_GATE=1` env var if ever wanted.

   # Feedback-signal pipeline health (Section FP — rb-472 + guard-415, 2026-04-23)
   # Three independent probes for the three failure layers diagnosed during Plan B/A/C.
   # If any FP check fails, decay / retention logic consuming utility_ratio is operating
   # on a broken signal — fix the upstream layer before touching the formula.
   # FP1 guards the classifier (Plan A) — reads the wrong tokens, population-wide zero.
   # FP2 guards the denominator (Plan C) — category gate bypassed, uniform retrieval_count.
   # FP3 guards gate parity (g-242-09) — retrieve.py and utilization-feedback.py drift apart.
   Check: at least one active reasoning-bank entry has times_inferred_helpful > 0 OR times_helpful > 0. Bash: `bash core/scripts/reasoning-bank-read.sh --active | py -3 -c "import json,sys; d=json.load(sys.stdin); hits=[e for e in d if (e.get('utilization',{}).get('times_inferred_helpful',0) > 0 or e.get('utilization',{}).get('times_helpful',0) > 0)]; print('PASS' if hits else 'FAIL: zero helpful/inferred-helpful signals across entire active rb — classifier is not matching any entry (Plan A regression)')"`
   Check: active rb entries with retrieval_count > 0 have coefficient of variation > 0.5 (non-uniform retrievals prove the category gate is firing). Bash: `bash core/scripts/reasoning-bank-read.sh --active | py -3 -c "import json,sys,math; d=json.load(sys.stdin); counts=[e.get('utilization',{}).get('retrieval_count',0) for e in d if e.get('utilization',{}).get('retrieval_count',0) > 0]; n=len(counts); mean=sum(counts)/n if n else 0; var=sum((c-mean)**2 for c in counts)/n if n else 0; cv=math.sqrt(var)/mean if mean else 0; print('INSUFFICIENT_DATA (n=%d, need >=10)' % n if n < 10 else ('PASS (cv=%.2f)' % cv if cv > 0.5 else 'FAIL: retrieval_count uniform across %d entries (cv=%.2f) — _entry_matches_category not firing (Plan C regression)' % (n, cv)))"`
   Check: no active rb entry has (times_helpful + times_inferred_helpful) > retrieval_count (numerator cannot exceed denominator under consistent category gates). Bash: `bash core/scripts/reasoning-bank-read.sh --active | py -3 -c "import json,sys; d=json.load(sys.stdin); bad=[(e['id'], e.get('utilization',{}).get('retrieval_count',0), e.get('utilization',{}).get('times_helpful',0), e.get('utilization',{}).get('times_inferred_helpful',0)) for e in d if (e.get('utilization',{}).get('times_helpful',0) + e.get('utilization',{}).get('times_inferred_helpful',0)) > e.get('utilization',{}).get('retrieval_count',0)]; print('PASS' if not bad else 'FAIL: %d entries have helpful > retrieval_count — retrieve.py and utilization-feedback.py category gates drifted apart (g-242-09): %s' % (len(bad), bad[:5]))"`

   # Precheck budget cap (Section PB — Magic Wand 2, g-115-509)
   # Seven checks ensure the precheck-budget-meter is wired correctly AND that
   # always-run sweeps (tree-debt-gate, experience-archival-gate,
   # fresh-eyes-code-gate) never appear as drops in precheck-drops.jsonl. If
   # an always-run sweep ever drops, that's a bug — either raise budget_pct
   # or move the sweep up the priority order in the meter's tier table.
   # The sixth check (g-115-1489 regression guard) asserts the wall-clock
   # `elapsed > cap_ms` deferrable-drop branch stays REMOVED (it measured
   # inter-tool-call LLM latency, not script cost, so it dropped EVERY
   # deferrable sweep every iteration and starved the fresh-eyes/felt-sense/
   # health-regression cadence rituals) AND that the zone-drop path remains
   # the sole drop mechanism. Cross-ref rb-1884, guard-784.
   Check: `core/scripts/aspirations-precheck-budget-meter.sh` exists with `start`, `check`, and `end` operations. Bash: `bash -n core/scripts/aspirations-precheck-budget-meter.sh && grep -q "start)" core/scripts/aspirations-precheck-budget-meter.sh && grep -q "check)" core/scripts/aspirations-precheck-budget-meter.sh && grep -q "end)" core/scripts/aspirations-precheck-budget-meter.sh && echo "PASS: meter script syntax + 3 ops present" || echo "FAIL: meter script missing or incomplete"`
   Check: `core/config/aspirations.yaml` defines `precheck:` block with `budget_pct`, `iteration_budget_ms`, and `zone_drop_rules`. Bash: `py -3 -c "import yaml; cfg=yaml.safe_load(open('core/config/aspirations.yaml')); pc=cfg.get('precheck'); assert pc, 'no precheck block'; assert 'budget_pct' in pc and 'iteration_budget_ms' in pc and 'zone_drop_rules' in pc, f'missing keys: {pc}'; print('PASS: precheck config complete')" || echo "FAIL: precheck config malformed or missing"`
   Check: `aspirations-precheck/SKILL.md` calls `meter start` (Step 0a) AND `meter end` (Phase 2). Bash: `grep -q "aspirations-precheck-budget-meter.sh start" .claude/skills/aspirations-precheck/SKILL.md && grep -q "aspirations-precheck-budget-meter.sh end" .claude/skills/aspirations-precheck/SKILL.md && echo "PASS: meter start+end wired" || echo "FAIL: meter start or end missing from precheck SKILL.md"`
   Check: All 5 deferrable sweeps in `aspirations-precheck/SKILL.md` invoke `meter check <name>` before the sweep. Bash: `expected="pending-questions-sweep recurring-precondition-sweep fresh-eyes-cadence fresh-eyes-program-cadence felt-sense-cadence"; missing=""; for n in $expected; do grep -q "meter.sh check $n" .claude/skills/aspirations-precheck/SKILL.md || missing="$missing $n"; done; if [ -z "$missing" ]; then echo "PASS: all 5 deferrable sweeps wired"; else echo "FAIL: missing meter check for:$missing"; fi`
   Check: No always-run sweep (tree-debt-gate, experience-archival-gate, fresh-eyes-code-gate) appears as `"decision":"drop"` in `agents/<agent>/session/precheck-drops.jsonl` over the recent log. Bash: `source core/scripts/_paths.sh 2>/dev/null; LOG="$AGENT_DIR/session/precheck-drops.jsonl"; if [ -f "$LOG" ]; then py -3 -c "import json,sys; bad=[r for line in open(sys.argv[1],encoding='utf-8') for r in [json.loads(line)] if r.get('tier')=='always-run' and r.get('decision')=='drop']; print('PASS: no always-run drops' if not bad else f'FAIL: {len(bad)} always-run drops detected: {bad[:3]}')" "$LOG"; else echo "PASS: no precheck-drops.jsonl yet (fresh install)"; fi`
   Check: budget-meter check-op decision block keeps the zone-drop path intact AND has NO executable wall-clock `elif elapsed > cap_ms` deferrable-drop branch (g-115-1489 regression guard — that branch measured inter-tool-call LLM latency, not script cost, so it dropped every deferrable sweep and starved the cadence rituals; the only legitimate remaining reference is the explanatory `#`-comment, which the `^[[:space:]]*elif` line-anchor excludes). Bash: `f=core/scripts/aspirations-precheck-budget-meter.sh; grep -qE '^[[:space:]]*elif tier in zone_drops' "$f" && ! grep -qE '^[[:space:]]*elif[^#]*elapsed[^#]*cap_ms' "$f" && echo "PASS: zone-drop path intact + no wall-clock budget-exceeded drop branch (g-115-1489 / rb-1884 / guard-784)" || echo "FAIL: budget-meter drop logic regressed — zone-drop path missing OR wall-clock elapsed>cap_ms deferrable-drop branch reintroduced (g-115-1489 / rb-1884 / guard-784)"`

   Check: budget-meter sweep_tier() classifies both notification-age safety gates (inbox-alert-age-check, handoff-aging-check) as always-run AND aspirations-precheck no longer meter-gates inbox-alert-age-check (g-115-1526 consistency decision — the two gates escalate aged unclaimed work to external parties, so they fire reliably). Bash: `f=core/scripts/aspirations-precheck-budget-meter.sh; s=.claude/skills/aspirations-precheck/SKILL.md; grep -qE 'inbox-alert-age-check\|handoff-aging-check\)' "$f" && ! grep -qE 'meter.*check inbox-alert-age-check' "$s" && echo "PASS: notification-age gates always-run + not meter-gated (g-115-1526)" || echo "FAIL: notification-age gate tier consistency regressed (g-115-1526)"`

   # Section CI: Co-Investigation Primitive (g-115-563, g-115-585 — co_invest_alignment, 2026-05-10)
   # The co-investigation primitive ties pair-iteration bias to four artifacts that
   # landed atomically (selector criterion + meta weight + protocol convention +
   # integration test). A future refactor dropping any single part would silently
   # degrade pair-iteration bias. Each check below guards one artifact; a removal
   # in any one part should fail this group without ambiguity. Proposal source:
   # bravo/reports/g-115-584-verify-learning-proposal.md (verified 2026-05-10T12:42).
   Check: co_invest_alignment criterion present in goal-selector.py. Bash: `grep -qE 'co_invest_alignment' core/scripts/goal-selector.py && echo "PASS: co_invest_alignment criterion present in goal-selector.py" || { echo "FAIL: co_invest_alignment criterion missing from core/scripts/goal-selector.py — pair-iteration bias decoupled from selector"; exit 1; }`
   Check: weights.co_invest_alignment numeric in meta/goal-selection-strategy.yaml (structural; survives formatting changes). Bash: `bash core/scripts/meta-read.sh goal-selection-strategy.yaml | py -3 -c "import yaml,sys; d=yaml.safe_load(sys.stdin) or {}; w=d.get('weights',{}); v=w.get('co_invest_alignment'); print('PASS: weights.co_invest_alignment=' + str(v)) if isinstance(v,(int,float)) else (print('FAIL: weights.co_invest_alignment missing or non-numeric in meta/goal-selection-strategy.yaml — selector criterion will fall through to default and lose calibration') or sys.exit(1))"`
   Check: 'Co-Investigation Protocol' section heading present in coordination.md. Bash: `grep -qE '^## Co-Investigation Protocol' core/config/conventions/coordination.md && echo "PASS: 'Co-Investigation Protocol' section heading present in coordination.md" || { echo "FAIL: 'Co-Investigation Protocol' section heading missing from core/config/conventions/coordination.md — protocol becomes undocumented"; exit 1; }`
   Check: test_co_invest_alignment.sh exists and is executable. Bash: `test -x core/scripts/tests/test_co_invest_alignment.sh && echo "PASS: test_co_invest_alignment.sh exists and is executable" || { echo "FAIL: core/scripts/tests/test_co_invest_alignment.sh missing or not executable — integration coverage gap"; exit 1; }`

   # Section SPE: Self/Program/Skill/Rule Evolution Infrastructure (per world/conventions/self-program-evolution.md)
   # These 5 checks detect drift in the 4-pillar autonomous-edit architecture
   # built in Phases 1-7. Specifically: hook coverage, snapshot integrity,
   # monitor consistency, stub cleanup, and proposal aging. See
   # world/conventions/self-program-evolution.md for the operational protocol.

   # SPE-1: settings.json has Pre+Post hooks for Edit/Write/MultiEdit, each
   # wired to evolution-prepare.sh / evolution-record.sh. Without these, the
   # write-side observability silently disappears (hooks fail-open, no stub
   # appended, no audit trail). The 6-entry coverage matrix below MUST hold.
   Check: settings.json has 6 evolution hook entries (Pre+Post × 3 tools). Bash: `py -3 -c "import json; s=json.load(open('.claude/settings.json',encoding='utf-8')); h=s.get('hooks',{}); pre=[(b.get('matcher'),hh.get('command','')) for b in h.get('PreToolUse',[]) for hh in b.get('hooks',[]) if 'evolution-prepare' in hh.get('command','')]; post=[(b.get('matcher'),hh.get('command','')) for b in h.get('PostToolUse',[]) for hh in b.get('hooks',[]) if 'evolution-record' in hh.get('command','')]; pre_m={m for m,c in pre}; post_m={m for m,c in post}; required={'Edit','Write','MultiEdit'}; missing_pre=required-pre_m; missing_post=required-post_m; (print('PASS: 6 evolution hook entries (Pre+Post x Edit/Write/MultiEdit)') if not missing_pre and not missing_post else (print(f'FAIL: missing Pre hooks for {missing_pre} OR missing Post hooks for {missing_post} — settings.json paste incomplete') or __import__('sys').exit(1)))"`

   # SPE-2: Every status=final entry across the 4 evolution streams MUST have
   # a valid history_snapshot path. The snapshot is the rollback fuel; if it
   # is missing, auto-rollback in Phase 4 cannot restore. Exclusions: bootstrap
   # entries (synthetic, pre-hook); empty change_class (no body diff to
   # snapshot); retroactive entries (signal_source=git-sweep — Phase 3
   # backfill predates the hook so no .history snapshot exists); rollback
   # entries (the rollback IS the restore action, not a new snapshot).
   Check: status=final live entries have valid history_snapshot. Bash: `py -3 -c "import json,sys; sys.path.insert(0,'core/scripts'); from _paths import WORLD_DIR; from pathlib import Path; missing=[]; checked=0; [missing.append((kind,e.get('revision_id'),e.get('history_snapshot'))) for kind in ['self','program','skill','rule'] for p in [WORLD_DIR/f'{kind}-evolution.jsonl'] if p.exists() for line in p.read_text(encoding='utf-8').splitlines() if line.strip() for e in [json.loads(line)] if (e.get('status')=='final' and e.get('change_class') not in ('bootstrap','empty','rollback') and e.get('signal_source')!='git-sweep' and (checked := checked+1) and (not e.get('history_snapshot') or not Path(e['history_snapshot']).exists()))]; print(f'PASS: all {checked} live final entries have valid history_snapshot (retroactive/bootstrap/rollback excluded)') if not missing else (print(f'FAIL: {len(missing)} live final entries missing snapshot (of {checked} checked); first 3: {missing[:3]} — rollback fuel gone') or sys.exit(1))"`

   # SPE-3: Every active_monitor in meta/backpressure.yaml with
   # monitor_kind in {self,program,skill,rule}_evolution MUST reference a
   # revision_id that exists in the corresponding *-evolution.jsonl stream
   # with status=final. A dangling monitor (rev not in stream) means
   # evolution-check will fail to look up the baseline and silently no-op.
   # ADVISORY: exits 0 with WARN per audit-baselines.md "advisory ratchet"
   # philosophy — historical drift (e.g. test artifacts left behind) exists
   # and cleaning it is future work, not a release-blocker.
   Check: active_monitors revision_ids exist in evolution streams (advisory). Bash: `py -3 -c "import yaml,json,sys; sys.path.insert(0,'core/scripts'); from _paths import WORLD_DIR, META_DIR; from pathlib import Path; bp=META_DIR/'backpressure.yaml'; mons=(yaml.safe_load(bp.read_text(encoding='utf-8')) or {}).get('active_monitors',[]) if bp.exists() else []; ev_kinds={'self_evolution':'self','program_evolution':'program','skill_evolution':'skill','rule_evolution':'rule'}; dangling=[]; checked=0; [(checked := checked+1, dangling.append((m.get('monitor_kind'),m.get('revision_id'))) if not any(json.loads(l).get('revision_id')==m.get('revision_id') and json.loads(l).get('status')=='final' for l in (WORLD_DIR/f'{ev_kinds[m[\"monitor_kind\"]]}-evolution.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()) else None) for m in mons if m.get('monitor_kind') in ev_kinds]; print(f'PASS: all {checked} evolution active_monitors reference valid final revs') if not dangling else print(f'WARN: {len(dangling)} dangling monitors (rev not final in stream); first 3: {dangling[:3]} — cleanup task')"`

   # SPE-4: No status=awaiting_completion stubs older than 30 minutes. A
   # stub older than that means the Phase 2 PostToolUse hook fired but the
   # LLM never called evolution-complete.sh to finalize. guard-544 fires
   # post-Edit to remind the LLM. Stale stubs indicate the reminder is
   # being ignored — the audit trail has gaps.
   # ADVISORY: exits 0 with WARN. Historical stubs from multi-agent sessions
   # accumulate over time. Per audit-baselines.md, cross-agent drift cleanup
   # is future work; the count surfaces here so it can be tracked over time.
   Check: no awaiting_completion stubs older than 30 min (advisory). Bash: `py -3 -c "import json,sys; from datetime import datetime,timedelta; sys.path.insert(0,'core/scripts'); from _paths import WORLD_DIR; from pathlib import Path; cutoff=datetime.now()-timedelta(minutes=30); stale=[(k,e.get('revision_id'),e.get('ts'),e.get('file_path')) for k in ['self','program','skill','rule'] for p in [WORLD_DIR/f'{k}-evolution.jsonl'] if p.exists() for line in p.read_text(encoding='utf-8').splitlines() if line.strip() for e in [json.loads(line)] if e.get('status')=='awaiting_completion' for ts in [datetime.fromisoformat(str(e.get('ts','')).replace('Z',''))] if ts < cutoff]; print(f'PASS: no awaiting_completion stubs older than 30 min') if not stale else print(f'WARN: {len(stale)} stale awaiting_completion stubs (cross-session drift); first 3: {stale[:3]} — guard-544 reminder appears to be ignored intermittently')"`

   # SPE-5: ADVISORY (g-115-1619, 2026-06-23). The program cross-agent ack
   # flow is DESIGNED BUT NOT IMPLEMENTED — neither the producer
   # (program-change-propose.py) nor the expiry sweep (program-ack-sweep.py)
   # was ever built (see world/conventions/self-program-evolution.md Phase 6).
   # So world/program-evolution-proposals/ has no producer and is normally
   # absent/empty: this check passes vacuously. It is retained as a tripwire —
   # a .diff appearing there would mean someone built the producer (activate
   # the rest of the flow) OR stray state was written; either is worth seeing.
   # Downgraded from hard-FAIL to advisory WARN: there is no sweep to be
   # "broken" while the flow is unimplemented, so a stale diff must not fail
   # the whole verify-learning run.
   Check: no program proposal diffs older than 7 days (advisory). Bash: `py -3 -c "import sys,time; sys.path.insert(0,'core/scripts'); from _paths import WORLD_DIR; from pathlib import Path; d=WORLD_DIR/'program-evolution-proposals'; cutoff=time.time()-7*86400; stale=[p.name for p in d.iterdir() if p.suffix=='.diff' and p.stat().st_mtime < cutoff] if d.exists() else []; print('PASS: no proposal diffs older than 7 days (ack flow unimplemented — dir normally absent/empty)') if not stale else print(f'WARN: {len(stale)} proposal diffs >7d in program-evolution-proposals/ despite ack flow being UNIMPLEMENTED (g-115-1619) — producer may have been built, or stray state; first 3: {stale[:3]}')"`

   # Section DPR: Daemon Path Resolution standard (guard-552, rb-933, g-115-733, g-115-734)
   # Daemon endpoints in mind_api/src/ must route environment-or-conf-sourced
   # path values through absolutize() (defined in core/scripts/_path_helpers.py
   # and used by mind_api/src/agent_paths.py). A raw Path() constructor receiving
   # os.environ.get("MIND_*") or conf.get("*_PATH") bypasses the cwd-anchoring
   # defense — on POSIX Python a Windows-absolute string like "C:/foo" parses
   # as RELATIVE, silently producing a mirror tree under the wrong root (the
   # external-path-resolution cruft failure mode catalogued in g-115-733).
   # agent_paths.py is the routing source itself and is excluded.
   Check: no raw Path() bypass of absolutize in mind_api/src/. Bash: `py -3 -c "import re,sys,pathlib; runtime_dir=pathlib.Path('mind_api/src'); violations=[]; pat=re.compile(r'Path\(\s*[^()]*?(os\.environ\.get\([^)]*MIND_|conf\.get\([^)]*_PATH)', re.DOTALL); [violations.append(f'{f.relative_to(pathlib.Path(chr(46)))}:{text[:m.start()].count(chr(10))+1}') for f in runtime_dir.rglob('*.py') if f.name != 'agent_paths.py' for text in [f.read_text(encoding='utf-8')] for m in pat.finditer(text) if 'absolutize' not in text[max(0,m.start()-60):m.start()]]; print(f'FAIL: {len(violations)} raw Path() bypass(es) in mind_api/src/: {violations[:5]} - must route through absolutize() per guard-552/rb-933') if violations else print('PASS: no raw Path() bypassing absolutize in mind_api/src/ (excluding agent_paths.py)'); sys.exit(1 if violations else 0)"`

   # Section FST: Forged-Skill Tagging consistency (Phase 1.3 packaging cleanup, 2026-05-17)
   # Every entry in world/forged-skills.yaml MUST have `forged: true` in its
   # .claude/skills/<name>/SKILL.md front matter. And every SKILL.md carrying
   # the tag MUST be registered in world/forged-skills.yaml. Without the
   # in-file tag, a packaging pass cannot distinguish framework-essential
   # skills from forged-out domain skills — the registry alone is the source
   # of truth and ships separately at the external WORLD_PATH. The bidirectional
   # check catches drift on both sides: a skill removed from the registry but
   # still tagged, or a registry entry whose SKILL.md was rewritten without
   # the tag. add-npc-task established the canonical pattern; 14 others were
   # back-filled 2026-05-17.
   Check: forged-skill tag matches world/forged-skills.yaml bidirectionally. Bash: `bash core/scripts/audit-forged-skill-tagging.sh`
   # Implementation extracted 2026-05-20 — same logic, callable from /verify-learning AND
   # seed-preflight without duplication. Edit the script, not this section.

   # Section TPL: Agent-Aspirations Template Cleanliness (Phase 4.5 packaging cleanup, 2026-05-18)
   # The two starter templates (core/config/agent-aspirations-initial.jsonl
   # and core/config/agent-aspirations-onboard.jsonl) seed a brand-new
   # agent's first 5-10 goals. Domain residue in these files leaks
   # AyoAI-specific work into every fresh deployment (e.g., a kindergarten
   # agent inheriting an "Audit NPC composition failures" goal on day 1).
   # The check matches the live deployment's domain-term blocklist against
   # the templates — any hit is a regression of the Phase 2.3 cleanup. New
   # domain-specific starter goals belong in the host's own world overlay,
   # not in core templates.
   Check: agent-aspirations templates contain no domain terms. Bash: `bash core/scripts/audit-aspirations-templates-clean.sh`
   # Implementation extracted 2026-05-20 — same logic, callable from /verify-learning AND
   # seed-preflight without duplication. Edit the script, not this section.

   # Section CFE: Core Framework Entries surveillance (Phase 4.1 packaging cleanup, 2026-05-18)
   # The path-resolution L1 hook covers WORLD/META/agent-dir top-level cruft
   # but excludes core/ and .claude/ by design (those are git-tracked so cruft
   # surfaces in `git status`). This check is the periodic surveillance pass:
   # enumerate the top-level entries of core/ and .claude/ and refuse any
   # that aren't on the framework's allowlist. New top-level entries indicate
   # either (a) intentional framework extension that needs the allowlist
   # updated, or (b) cruft that bypassed Write/Edit/MultiEdit via Bash
   # redirect/touch/cp/mkdir. Either way the human needs to see it. Catches
   # the same failure mode as the L1 hook's WORLD-side check but at audit
   # cadence instead of write time.
   Check: core/ and .claude/ have only expected top-level entries. Bash: `bash core/scripts/audit-toplevel-allowlist.sh`
   # Implementation extracted 2026-05-20 — same logic, callable from /verify-learning AND
   # seed-preflight without duplication. Edit the script, not this section.
   # The allowlist itself lives inside the script (a single source of truth for both
   # consumers); intentional framework extensions update the script, not this section.

   # Section DPS: Dotted-path syntax against fail-loud update-field scripts
   # (g-115-928, g-115-529 lineage). experience-update-field.sh (and
   # symmetrically aspirations.py cmd_update_goal) reject dotted-path field
   # names at runtime — `experience.py:549` prints "BLOCKED: dotted field
   # name '...' is not supported" and exits 1. Any SKILL.md pseudocode that
   # writes `experience-update-field.sh <id> retrieval_stats.X <val>` fails
   # silently from the LLM's perspective (the script errors but the SKILL
   # block continues to the next line). The fix is whole-object JSON:
   # `experience-update-field.sh <id> retrieval_stats '<json-blob>'`.
   #
   # This check greps all .claude/skills/**/SKILL.md for the broken pattern
   # against the experience-update-field wrapper. If pipeline.py or
   # reasoning-bank.py adopt symmetric rejection later, widen this regex.
   Check: no SKILL.md pseudocode calls experience-update-field.sh with a
   dotted-path field. The verify-learning SKILL.md itself is excluded
   because this check authors illustrative examples of the rejected pattern
   in prose (the check is for consumers of the wrapper, not its own spec).
   Bash: `py -3 -c "import sys,pathlib,re; pat=re.compile(r'experience-update-field\.sh\s+\S+\s+([a-zA-Z_][\w]*\.[a-zA-Z_][\w]*)\b'); hits=[(str(p), n+1, m.group(1)) for p in pathlib.Path('.claude/skills').rglob('SKILL.md') if 'verify-learning' not in str(p) for n, line in enumerate(p.read_text(encoding='utf-8').splitlines()) for m in [pat.search(line)] if m]; ok = not hits; print('PASS: no dotted-path experience-update-field.sh call sites in SKILL.md') if ok else (print(f'FAIL: dotted-path experience-update-field.sh call sites detected (will fail-loud at runtime per experience.py:549 — replace with whole-object JSON, see g-115-928): {hits[:5]}') or sys.exit(1))"`

   # Section TPD: Test Pollution Defense — conftest autouse env-restore fixture
   # (commit 7f05915d, rb-1096, guard-588, tree node test-pollution-defense, 2026-05-19)
   # Pytest's collection phase imports ALL test modules before any test runs,
   # so a module-level `os.environ.pop("MIND_AGENT", None)` in any single
   # test file contaminates the env that every other test sees — even tests
   # that sort earlier alphabetically. This produced 18 of 20 baseline
   # failures (Clusters B-G, H) before the two-layer fix shipped.
   #
   # Layer 1 is per-file capture-restore in 11 polluter files (g-115-888) —
   # enforced by guard-588 discipline, not automation.
   # Layer 2 is the autouse `_restore_env_per_test` fixture in conftest.py
   # that snapshots MIND_AGENT + MIND_WORLD at conftest load time (before
   # any polluter module imports) and restores them before every test.
   # This check guards Layer 2 — without it, any future test file with
   # module-level env mutation will silently re-introduce the 18-failure
   # regression, and the failure may not surface immediately (only when a
   # downstream test depends on MIND_AGENT or MIND_WORLD).
   Check: `core/scripts/tests/conftest.py` contains the `_restore_env_per_test` autouse fixture (Layer 2 of the test-pollution defense).
   Bash: `grep -q '_restore_env_per_test' core/scripts/tests/conftest.py && grep -q '@pytest.fixture(autouse=True)' core/scripts/tests/conftest.py && echo "PASS: conftest.py has _restore_env_per_test autouse fixture (Layer 2 test-pollution defense intact)" || (echo "FAIL: conftest.py is missing the _restore_env_per_test autouse fixture — Layer 2 of the test-pollution defense has regressed. See tree node test-pollution-defense, rb-1096, guard-588, commit 7f05915d. Without this fixture, module-level os.environ.pop in any new test file will re-contaminate the suite via pytest's collection-time imports." && exit 1)`

   # Section TPD Layer 1 — per-file capture-restore enforcement
   # (encode-session 2026-05-20, complements the Layer 2 check above)
   # Layer 2 (autouse fixture) is the safety net; Layer 1 (per-file
   # capture-restore around the mutation) is the right fix at the source.
   # This check enforces Layer 1: any pytest test file containing a
   # module-level os.environ.pop / setdefault / update / [X]=Y MUST also
   # contain capture-restore evidence (_SAVED, _ORIG_, _BOOTSTRAP, an
   # autouse fixture, or @pytest.fixture) in the same file. Per-file
   # (not per-line) because the restore typically lives several lines
   # away from the mutation. Module-level = no leading whitespace =
   # truly top-level (vs. inside a def/class which is fine).
   # Mirror of guard-588's prescription; this is the static-grep enforcement.
   Check: pytest test files under core/scripts/tests/ and core/tests/ have no module-level os.environ mutation without capture-restore evidence (Layer 1 of the test-pollution defense).
   Bash: `py -3 -c "import sys,pathlib,re; mut=re.compile(r'^os\.environ\.(pop|setdefault|update)|^os\.environ\['); ev=re.compile(r'_SAVED|_ORIG_|_BOOTSTRAP|autouse|@pytest\.fixture'); viol=[]; [viol.append(f'{f}:{n+1}: {line.strip()[:80]}') for root in ('core/scripts/tests','core/tests') if pathlib.Path(root).exists() for f in pathlib.Path(root).rglob('test_*.py') for text in [f.read_text(encoding='utf-8',errors='replace')] for n,line in enumerate(text.splitlines()) if mut.match(line) and not ev.search(text)]; ok=not viol; print('PASS: no module-level os.environ mutation regressions in pytest files (Layer 1 test-pollution defense intact)') if ok else (print(f'FAIL: module-level os.environ mutation without capture-restore evidence in pytest test files (regression of the test_cmd_set_auto_propagate.py contaminator class — see rb-1096, guard-588, tree node test-pollution-defense, commit 7f05915d): {viol[:10]}') or sys.exit(1))"`

   # Section ODI: Optional-dependency module-level import guard — pytest collection-abort defense (g-115-1775, rb-2780, 2026-07-04)
   # A DIRECT module-level `import numpy|torch|sentence_transformers` (or `from`
   # same) in a pytest test file aborts pytest COLLECTION on any box lacking that
   # dep — exit 2, ZERO tests run — because pytest imports every test module during
   # collection before running anything. That silently zeroes ALL test signal on
   # that box: a HIGH-blast-radius regression that forced --continue-on-collection-
   # errors on every deep-close full-suite run (rb-2766 baseline). The durable fix
   # is the assignment-form guard `np = pytest.importorskip("numpy")` placed BEFORE
   # every dep-importing line (direct AND transitive), which converts the collection
   # ERROR into a clean module SKIP. This check guards RE-introduction: any pytest
   # test file with an unguarded (no importorskip anywhere in the file) module-level
   # heavy-dep import FAILs. Mirror of the Section TPD Layer 1 grep above (same
   # core/scripts/tests scan shape). LIMITATION: catches DIRECT module-level imports
   # only — the TRANSITIVE case (a test importing a LOCAL module that itself imports
   # numpy, as test_embedding_retrieval.py did via _embedding_retrieval) is NOT
   # statically greppable and is out of scope. Module-level = no leading whitespace;
   # an indented import inside try/except or a def is lazy/guarded and does not
   # abort collection.
   Check: pytest test files under core/scripts/tests/ and core/tests/ have no unguarded module-level numpy/torch/sentence_transformers import (importorskip collection-abort defense).
   Bash: `py -3 -c "import sys,pathlib,re; dep=re.compile(r'^(?:import (?:numpy|torch|sentence_transformers)\b|from (?:numpy|torch|sentence_transformers)(?:\.\w+)* import)'); guard=re.compile(r'importorskip'); viol=[]; [viol.append(f'{f}:{n+1}: {line.strip()[:80]}') for root in ('core/scripts/tests','core/tests') if pathlib.Path(root).exists() for f in pathlib.Path(root).rglob('test_*.py') for text in [f.read_text(encoding='utf-8',errors='replace')] if not guard.search(text) for n,line in enumerate(text.splitlines()) if dep.match(line)]; ok=not viol; print('PASS: no unguarded module-level numpy/torch/sentence_transformers imports in pytest test files (importorskip collection-abort defense intact)') if ok else (print(f'FAIL: module-level optional-dependency import without pytest.importorskip in a pytest test file — this aborts pytest core/scripts/tests COLLECTION (exit 2, ZERO tests run) on any box lacking the dep, silently zeroing all test signal there (g-115-1775, rb-2780): {viol[:10]}') or sys.exit(1))"`

   # Section APD: Inlined _APD constants mirror canonical AGENTS_PARENT_DIR (g-115-984, commit 520e9375, 2026-05-19)
   # Four hot-path shell scripts (session-state-get.sh, session-mode-get.sh,
   # session-signal-exists.sh, cleanup-stale-bindings.sh) inline `_APD="agents"`
   # rather than sourcing _paths.sh — the IRREDUCIBLY LOCAL annotation in each
   # file documents the latency/bridge constraint. These inlined copies MUST
   # mirror _paths.sh's AGENTS_PARENT_DIR; drift between them silently
   # produces wrong agent-dir resolution on the hot path (state probes,
   # session-signal-exists, mode probes). The 2026-05-19 incident was caught
   # manually when working-tree drift showed `_APD=""` after a partial Phase
   # 2.5.C cleanup — a verify-learning check would have surfaced it
   # immediately. CLAUDE.md "Agent-dir Resolution" lists these 4 files as
   # sites that MUST stay in sync. See rb-1092, guard-587.
   Check: 4 inlined _APD constants in session-* hot-path scripts mirror _paths.sh's AGENTS_PARENT_DIR.
   Bash: `canon=$(grep -E '^AGENTS_PARENT_DIR=' core/scripts/_paths.sh | head -1 | sed 's/AGENTS_PARENT_DIR=//; s/^"//; s/"$//'); fail=0; for f in session-state-get.sh session-mode-get.sh session-signal-exists.sh cleanup-stale-bindings.sh; do v=$(grep -E '^_APD=' core/scripts/$f | head -1 | sed 's/_APD=//; s/^"//; s/"$//'); if [ "$v" != "$canon" ]; then echo "FAIL: $f _APD=\"$v\" drifted from canonical AGENTS_PARENT_DIR=\"$canon\" (see CLAUDE.md Agent-dir Resolution, rb-1092, guard-587, commit 520e9375)"; fail=1; fi; done; [ $fail -eq 0 ] && echo "PASS: all 4 inlined _APD constants mirror canonical AGENTS_PARENT_DIR=\"$canon\""`

   # Section APD12: Full 12-site AGENTS_PARENT_DIR / SESSIONS_DIRNAME / SESSION_DIRNAME sync (g-115-1062, 2026-05-21)
   # Section APD above covers only the 4 inlined _APD shell sites. CLAUDE.md
   # "Agent-dir Resolution" tables list 12 total sync sites — 7 framework
   # (_paths.{py,sh}, mind_api/src/agent_paths.py, _agents.py,
   # path-resolution-hook.py, _world_config.py, _session_binding.py) plus
   # the 5 inlined hot-path copies. _paths.sh is canonical; the other 11
   # must mirror it across AGENTS_PARENT_DIR, SESSIONS_DIRNAME, and
   # SESSION_DIRNAME (per-site subset varies — see the script's SITES
   # table). Drift between any site and canon silently re-routes agent-dir
   # resolution. The 2026-05-20 incident (_world_config.py kept a
   # pre-Phase-2.5.D shape for ~3 weeks; ~25 routing-table-empty pytest
   # failures surfaced it) motivated this check. Section APD remains as
   # the targeted shell-only probe; APD12 is the full-coverage sister.
   Check: all 12 AGENTS_PARENT_DIR/SESSIONS_DIRNAME/SESSION_DIRNAME sync sites match canonical _paths.sh values.
   Bash: `py -3 core/scripts/check-agents-parent-dir-sync.py`

   # Section UER: uncommitted-edits.jsonl log freshness — neutral-path filter + Windows path normalization (g-115-1129, g-115-1125, rb-1127, 2026-05-22)
   # g-115-1125 (commit 3c4a61c4) fixed two silent bugs in uncommitted-edits-
   # record.sh:
   #   1. Neutral-path filter under AGENTS_PARENT_DIR=agents — entries like
   #      "agents/<name>/..." should be filtered out (agent-private, not
   #      neutral), not appended as neutral-path edits.
   #   2. Windows absolute-path normalization — entries should land as repo-
   #      relative paths ("core/scripts/foo.py"), not absolute Windows form
   #      ("C:/ZakNoCloud/GitHub/Ayoai-Mind/core/scripts/foo.py").
   # A regression in either is silent — the log keeps appending, but
   # iteration-commit's cross-agent attribution filter misclassifies entries
   # and the wrong agent's signature lands on the resulting commit. That
   # exact incident (rb-1127, commit 3c4a61c4) clobbered alpha's authorship
   # of the very fix that closed the bug. This check is the fail-loud
   # regression detector for the fix that closed the incident.
   # Tolerance: pre-fix entries (edit_ts < BASELINE) are kept as historical
   # noise. Only post-fix entries are checked. BASELINE is tunable via
   # UNCOMMITTED_EDITS_BASELINE env var.
   Check: agent session/uncommitted-edits.jsonl logs have no post-fix entries with `agents/` prefix (neutral-path filter regression)
   Check: agent session/uncommitted-edits.jsonl logs have no post-fix entries with absolute path form `[A-Za-z]:/...` or `/<letter>/...` (Windows path normalization regression)
   Bash: `py -3 core/scripts/check-uncommitted-edits-log-freshness.py`

   # Section MCV: verify-learning meta-check — its own rb/guard citations match live records (g-115-1140, rb-1191, 2026-05-22)
   # g-115-998 surfaced silent drift between verify-learning Bash assertions
   # and the actual reasoning-bank/guardrails records they cite:
   #   1. Section SFH asserted rb-435.applies_to=any while the live record
   #      held framework (semantic drift after the check was written).
   #   2. Section DC asserted guard-006.status=active while the record was
   #      deliberately retired (rule moved to structural enforcement —
   #      iteration-commit.sh Phase 8 + post-execution.md Step 2 build-gate).
   # Both went undetected because /verify-learning only runs the assertions —
   # it never cross-checked that the asserted fields still match live state.
   # This section flips that gap: a meta-check that parses verify-learning's
   # own Bash check lines, extracts (record_id, field, expected_value) tuples
   # from common assertion forms (grep -q '"field": "value"', d['field']==X,
   # d['field'] in (A,B)), reads the live record, and reports mismatches.
   # Parser is intentionally conservative — content-substring greps and
   # custom python checks are skipped (they need ad-hoc handling that the
   # author writing them already owns); the value of this gate is catching
   # the simple field=value drift class that g-115-998 surfaced.
   Check: every Bash check line in verify-learning SKILL.md citing an rb-NNN / guard-NNN id with a parseable field=value assertion matches the live record's current field value.
   Bash: `py -3 core/scripts/check-verify-learning-citation-drift.py`

   # Section MOTIF: MOTIF pairwise-preference pass default-off invariant (g-307-32 / g-115-1086, 2026-05-21)
   # The MOTIF pairwise pass in Processor's strategy_extractor.py is feature-flagged
   # default-off in processor-config.yaml -> strategy_extractor.motif_pairwise.enabled.
   # Acceptance #1 of the original spec requires byte-identical pre-flag behavior; a
   # future edit that flips enabled to true would silently change pre-flag-baseline
   # behavior AND spike production LLM cost (every extraction pass enumerates C(N,2)
   # pairs against the LLM). This check fires when the Processor repo is reachable
   # via AGENT_WRITE_PATH and SKIPs cleanly otherwise (not every agent has product
   # checkout). Pattern: existing default-off invariants like user_signal_boost /
   # class_balance (lines 2626 / 2631).
   Check: `processor-config.yaml` strategy_extractor.motif_pairwise.enabled is false (or motif_pairwise block absent — both satisfy the default-off invariant)
   Bash (motif-pairwise-default-off): awp=$(grep -E '^AGENT_WRITE_PATH=' "agents/$MIND_AGENT/local-paths.conf" 2>/dev/null | head -1 | sed -E 's/^AGENT_WRITE_PATH=//;s/^"//;s/"$//') && cfg="$awp/Ayoai-Environment-Processor/processor-config.yaml" && if [ ! -f "$cfg" ]; then echo "SKIP: processor-config.yaml not reachable for agent $MIND_AGENT (path=$cfg) — MOTIF check is product-domain"; else py -3 -c "import yaml,sys; c=yaml.safe_load(open(r'$cfg')) or {}; m=c.get('strategy_extractor',{}).get('motif_pairwise',{}); en=m.get('enabled'); sys.exit(0 if en is False or en is None else 1)" && echo "PASS: motif_pairwise.enabled is default-off (false or absent)" || echo "FAIL: motif_pairwise.enabled is true in $cfg — pre-flag baseline behavior is no longer the default (g-307-32 acceptance #1 violation, regression risk: LLM cost spike + behavior drift)"; fi

   # Section SMJ: stderr-merge-into-json.loads regression class (guard-659, g-115-1265, 2026-05-26)
   # A shell command captured with `2>&1` whose output is fed to python
   # json.loads / json.load silently zeroes out on ANY stderr line (the parse
   # fails, the except branch returns empty, the caller reads false-empty).
   # Two historical instances: g-249-16 detected the pattern; g-249-18 fixed
   # instance 2 at infra-streak-notify.sh:58 (stderr was zeroing alert_count ->
   # false "no alerts", a guard-465 silent-monitoring instance).
   # check-stderr-json-merge.py scans core/scripts/*.sh, correlates each
   # 2>&1-captured var with its json.loads consumer window, and EXEMPTS the
   # three valid mitigations: (a) remove 2>&1 (var never enters the flag set),
   # (b) raw_decode + residual-split (g-115-769, aspirations-update-goal.sh),
   # (c) per-line JSON extraction with a startswith prefix filter (guard-559,
   # iteration-close.sh). Exit 1 + flagged[] on any unhardened site. Behavior
   # pinned by core/scripts/tests/test_check_stderr_json_merge.py.
   Check: no core/scripts/*.sh feeds a `2>&1`-captured value to json.loads/json.load without one of the three hardenings (guard-659 regression class)
   Bash (stderr-json-merge): py -3 core/scripts/check-stderr-json-merge.py >/dev/null 2>&1 && echo "PASS: no unhardened 2>&1-into-json.loads sites in core/scripts/*.sh (guard-659)" || echo "FAIL: unhardened 2>&1-capture fed to json.loads in core/scripts (guard-659 regression) — run 'py -3 core/scripts/check-stderr-json-merge.py' for the flagged site; fix via remove-2>&1 / raw_decode (g-115-769) / per-line extraction (guard-559)"

   # Section TBF: time-bomb fixture scanner wiring (guard-566, g-115-1260, 2026-05-27)
   # A "time-bomb fixture" hardcodes an absolute ISO timestamp that production
   # code then compares against a now()-relative staleness window (streak/TTL
   # reset, overdue branch, *_since age). It passes on authoring day, then
   # silently flips to the overdue/reset path as wall-clock advances -- a
   # deterministic failure hours-to-days later with NO code change. Canonical
   # incident g-115-1141 (test_insight_trigger_gate_reprobe.py hardcoded
   # 2026-05-21, aged out of a 24h scan window, 4 failures). guard-566 mandates
   # the now-relative idiom; timebomb-fixture-scan.py is the Layer-C detective.
   # The g-115-1260 audit established the pattern is NOT a fail-loud full-scan
   # gate: --all reports ~165 LEGITIMATE literals (inert `created` metadata,
   # fixed-vs-fixed round-trips, explicit now= params, far-future output
   # assertions) -- a 165:0 false-positive-to-bomb ratio. So enforcement is
   # diff-scoped: --diff --exit-on-hits flags only a NEW unmarked recent literal
   # in an uncommitted test file, tripping on the bomb at authoring time without
   # the legacy-literal noise. Exemptions encode guard-566's own exception
   # clause: same-line now-idiom, far-past (>recency-days), or a
   # `# timebomb-safe: <reason>` marker. This check confirms the scanner runs
   # clean against the working tree; a future uncommitted bomb FAILs it here.
   Check: `core/scripts/timebomb-fixture-scan.py` exists and reports 0 unmarked recent hardcoded-timestamp fixtures in the diff-scoped working tree (guard-566 enforcement, diff mode)
   Bash (timebomb-fixture-scan): test -f core/scripts/timebomb-fixture-scan.py && py -3 core/scripts/timebomb-fixture-scan.py --diff --exit-on-hits >/dev/null 2>&1 && echo "PASS: timebomb-fixture-scan present, 0 unmarked new hardcoded-timestamp fixtures in working tree (guard-566)" || echo "FAIL: timebomb-fixture-scan missing OR a new uncommitted test fixture hardcodes a recent ISO timestamp without the now-relative idiom or a '# timebomb-safe:' marker — run 'py -3 core/scripts/timebomb-fixture-scan.py --diff' for the site; fix per guard-566 (mirror test_insight_trigger_sweep_reprobe.py::_trigger_timestamp)"

   # post-recovery-edit-gate presence + wiring (g-001-16, rb-1118, guard-595, commit 14cb6750)
   # g-001-16 landed core/scripts/post-recovery-edit-gate.{py,sh} + 3 PreToolUse hook
   # entries (Write/Edit/MultiEdit) in .claude/settings.json. The gate denies edits when
   # state != "IDLE" or mode != "autonomous" (stops post-recovery edit drift). These
   # checks catch (1) silent script removal, (2) settings de-wiring below 3 sites, (3)
   # accidental deny-predicate tuple expansion that would over-block.
   Bash (post-recovery-gate-scripts): test -f core/scripts/post-recovery-edit-gate.py && test -f core/scripts/post-recovery-edit-gate.sh && echo "PASS: post-recovery-edit-gate.{py,sh} present" || echo "FAIL: post-recovery-edit-gate script(s) missing (g-001-16 regressed)"
   Bash (post-recovery-gate-wired): test $(grep -c "post-recovery-edit-gate" .claude/settings.json) -ge 3 && echo "PASS: post-recovery-edit-gate wired >=3 PreToolUse sites in settings.json" || echo "FAIL: post-recovery-edit-gate wired <3 sites in .claude/settings.json — Write/Edit/MultiEdit hook de-wired (g-001-16)"
   Bash (post-recovery-gate-deny-predicate): grep -q 'state != "IDLE" or mode != "autonomous"' core/scripts/post-recovery-edit-gate.py && echo "PASS: post-recovery-edit-gate deny predicate intact (IDLE/autonomous)" || echo "FAIL: post-recovery-edit-gate.py deny predicate changed — accidental tuple expansion would over-block (rb-1118, guard-595)"

   # ════════════════════════════════════════════════════════════════════
   # Section PRC: Promotion-Reconcile Back-Ported Checks (2026-06-27)
   # The 2026-06-27 dev→staging→prod promotion reconcile (semantic 96-file
   # classification) found these regression guards present in the production
   # deployment but absent from dev. Per .claude/rules/promotion-cycle.md
   # "Pre-Overwrite Drift Gate" (reconcile-not-mirror), they are back-ported
   # here so the cutover — which overwrites verify-learning/SKILL.md at the
   # destination — does not clobber them. JDV/FRO/CSV/D6.7-8 are framework-
   # general (every guarded file exists in dev, staging, and prod); PROMO is
   # self-gating (a prod-local deployment-topology guardrail, no-op where the
   # promotion-cycle artifacts legitimately do not exist).
   # ════════════════════════════════════════════════════════════════════

   # Convention-doc vs daemon-validator consistency: journal store (Section JDV — g-001-53, 2026-05-30)
   # core/config/conventions/journal.md is the human-facing schema doc; the daemon-enforced
   # ground truth is mind_api/src/store_registry.py STORE_REGISTRY["journal"] + _journal_validate.
   # When the doc drifts from the validator, callers build journal-add payloads from the doc and
   # get rejected at runtime. Canonical incident (2026-05-30, rb-130): the doc showed journal_file
   # with an `agents/` prefix (validator wants agent-relative, no prefix) AND goals_completed as
   # integer 0 (validator wants array-of-strings) → two failed journal-add attempts before the
   # validator was read. The journal.md doc was back-ported alongside this guard so both stay paired.
   Check: `core/config/conventions/journal.md` documents `journal_file` as agent-relative (no `agents/` prefix). Bash: grep -qF 'no `agents/` prefix' core/config/conventions/journal.md && echo "PASS: journal.md documents agent-relative journal_file" || echo "FAIL: journal.md dropped the no-agents-prefix rule — doc drifted from _journal_validate regex (g-001-53 / rb-130 regression)"
   Check: `core/config/conventions/journal.md` documents `goals_completed`/`key_events`/`tags` as array-of-strings. Bash: grep -qF 'Array-of-strings fields' core/config/conventions/journal.md && echo "PASS: journal.md documents array-of-strings fields" || echo "FAIL: journal.md dropped the array-of-strings field doc — doc drifted from validator (g-001-53 / rb-130 regression)"
   Check: `core/config/conventions/journal.md` names `store_registry.py` as the authoritative schema source. Bash: grep -qF 'store_registry.py' core/config/conventions/journal.md && echo "PASS: journal.md points at the daemon validator as authoritative" || echo "FAIL: journal.md missing the authoritative-source pointer to store_registry.py — readers cannot find ground truth"

   # Forged restricted-write Restricted Operations coverage (Section FRO — sq-018 / g-012-21, 2026-06-10)
   # A forged SKILL.md whose companion_scripts list a dedicated live-datastore writer
   # (a *-write.sh) MUST carry a `## Restricted Operations` section documenting the write
   # boundary. Origin: g-012 contract skills shipped MISSING it (rb-120); poll-outreach-replies
   # repeated the class (caught + fixed by g-012-21). Iterates whatever forged skills exist, so
   # it is safe across deployments.
   Bash (forged-restricted-ops): MISS=$(for f in .claude/skills/*/SKILL.md; do csline=$(awk 'NR==1&&/^---/{ff=1;next} ff&&/^---/{exit} ff&&/^companion_scripts:/{print}' "$f"); if echo "$csline" | grep -qE "[a-z0-9-]+-write\.sh"; then grep -qE "^## Restricted Operations" "$f" || echo "$(basename "$(dirname "$f")")"; fi; done); if [ -z "$MISS" ]; then echo "PASS: every forged skill with a -write.sh companion documents Restricted Operations"; else echo "FAIL: -write.sh companion but no ## Restricted Operations section: $MISS (sq-018/rb-120 regression — add the section)"; fi

   # Notify-build-payload Self-heading regex tolerance (Section CSV partial — g-009-06, 2026-05-22)
   # notify-build-payload.py was relaxed from the strict `^#+\s*Self\s*$` to `^#+\s*Self(\s.*)?$`
   # so an agent self.md with a `# Self - <name>` heading (agent-name suffix) is accepted. A
   # "tidy-up" back to the strict form would make notify-user's build payload reject that identity.
   Check: `core/scripts/notify-build-payload.py` Self-heading regex accepts both `# Self` and `# Self - <name>`. Bash: grep -qF 'Self(\s.*)?' core/scripts/notify-build-payload.py && echo "PASS: notify-build-payload Self regex accepts agent-name-suffix shape" || { echo "FAIL: notify-build-payload Self regex regressed to strict form — a '# Self — <name>' identity would be rejected (g-009-06)"; exit 1; }

   # D6.7+D6.8: owncloud flush + runner-claim release wiring (g-009-38, 2026-06-24)
   # D6.7 calls owncloud-flush.sh to push governed writes to remote storage before stop;
   # D6.8 calls runner-claim.sh release to drop the cross-machine session-lock. Both no-op under
   # STORAGE_BACKEND=local; the wiring must stay so a backend cutover activates without SKILL.md edits.
   Bash: grep -c "owncloud-flush.sh" .claude/skills/aspirations-graceful-stop/SKILL.md → verify >= 1 (D6.7 flush wired in graceful-stop)
   Bash: test -f core/scripts/runner-claim.sh && echo "PASS: runner-claim.sh exists" || echo "FAIL: runner-claim.sh missing"

   # Promotion-cycle enforcement layer (g-001-132, 2026-06-24) — SELF-GATING for portability
   # Deployment-topology guardrail. The promotion-cycle.md rule + the CLAUDE.md "CRITICAL:
   # Promotion Cycle" section are PROD-local (preserved at the production deployment via the seed
   # engine's _DEPLOYMENT_LOCAL_FILES; dev/staging legitimately lack them). This check is
   # conditional: a no-op in dev/staging (neither artifact present) and strict in prod (both
   # present), so it travels with the framework via promotion yet only enforces where the
   # guardrail actually lives — and it catches a partial-clobber where one artifact survives
   # without the other.
   Bash (promotion-cycle-gate): if [ -f .claude/rules/promotion-cycle.md ] || grep -q "CRITICAL.*Promotion" CLAUDE.md 2>/dev/null; then { [ -f .claude/rules/promotion-cycle.md ] && grep -q "CRITICAL.*Promotion" CLAUDE.md && echo "PASS: promotion-cycle enforcement layer intact (rule file + CLAUDE.md section)"; } || { echo "FAIL: promotion-cycle enforcement PARTIAL — one of {.claude/rules/promotion-cycle.md, CLAUDE.md CRITICAL: Promotion Cycle section} is missing (g-001-132 partial-clobber)"; exit 1; }; else echo "SKIP: promotion-cycle enforcement is prod-local — not present in this dev/staging deployment"; fi

## Step 4: Summary Report

   # Priority review skill integrity checks (Section PR)
   Check: `.claude/skills/priority-review/SKILL.md` exists (core skill file)
   Check: `.claude/skills/_tree.yaml` has `priority-review` entry with `model_invocable: true`
   Check: `.claude/settings.json` deny array contains `Edit(*/.claude/skills/priority-review/*)` and `Write(*/.claude/skills/priority-review/*)`
   Check: `CLAUDE.md` lists `/priority-review` in Hybrid skills line
   Check: `CLAUDE.md` User Control Commands table has `/priority-review` row
   Check: `respond/SKILL.md` Step 4b has "Priority Review Surfacing" section BEFORE "Pending Questions" section
   Check: `respond/SKILL.md` Step 5 table has "Priority review" directive type that invokes `/priority-review`
   Check: `create-aspiration/SKILL.md` has Step 8.6 with `from-self` mode gate
   Check: `priority-review/SKILL.md` priority update uses the POSITIONAL field-merge form (`aspirations-update.sh {asp-id} priority {value}`), NOT the retired stdin full-replacement pipe. Bash: grep -q 'aspirations-update.sh {asp-id} priority' .claude/skills/priority-review/SKILL.md && ! grep -qE "echo .*\| *(bash )?(core/scripts/)?aspirations-update\.sh" .claude/skills/priority-review/SKILL.md && echo "PASS: priority-review uses positional field-merge" || echo "FAIL: priority-review uses the retired stdin full-replacement form (g-001-17 / rb-42 regression)"
   # Direction CORRECTED by g-001-17 / rb-42 / rb-46 (verified 2026-05-23 against the
   # update_aspiration daemon handler): aspirations-update.sh is positional
   # <asp_id> <field> <value> and does PER-FIELD MERGE (asp[field]=value), preserving
   # all other fields — it reads NO stdin. The pre-2026-05-14-cutover stdin
   # full-replacement form is retired (piping JSON → "asp_id, field, and value are all
   # required"). The earlier check here enforced the stdin form from a stale
   # initial-code-review reading; this check now enforces the canonical positional form.
   Check: `priority-review/SKILL.md` Phase 1 reads BOTH world (`load-aspirations-compact.sh`) AND agent-local (`agent-aspirations-read.sh`) aspirations

   # Multi-agent coordination evidence checks (Section MAC — arXiv 2603.28990)
   # Output-centric communication, depends_on, self-abstention, claim atomicity

   # MAC1: Lock path convention (guard-056)
   Bash: python3 -c "
from pathlib import Path
p = Path('test.jsonl')
assert p.with_suffix('.lock') == Path('test.lock'), 'with_suffix produces wrong path'
assert str(p) + '.lock' == 'test.jsonl.lock', 'str+.lock produces different path'
print('PASS: lock conventions differ — code must use .with_suffix()')
"
   Check: `aspirations.py` `cmd_claim` uses `LIVE_PATH.with_suffix(".lock")` (NOT `str(LIVE_PATH) + ".lock"`)
   Check: `aspirations.py` `cmd_claim` comment says "MUST match _fileops.locked_write_jsonl convention"
   Check: `aspirations.py` `cmd_claim` uses `ensure_ascii=True` (matches _fileops.py)

   # MAC2: COMPACT_GOAL_KEEP includes new fields
   Bash: python3 -c "
import ast, sys
with open('core/scripts/aspirations.py') as f:
    src = f.read()
idx = src.index('COMPACT_GOAL_KEEP')
start = src.index('{', idx)
depth = 0
for i, c in enumerate(src[start:], start):
    if c == '{': depth += 1
    elif c == '}': depth -= 1
    if depth == 0:
        keep = ast.literal_eval(src[start:i+1])
        break
missing = {'depends_on', 'abstained_by'} - keep
if missing:
    print(f'FAIL: COMPACT_GOAL_KEEP missing: {missing}')
    sys.exit(1)
print(f'PASS: COMPACT_GOAL_KEEP has depends_on and abstained_by ({len(keep)} fields)')
"

   # MAC3: Goal validation includes new fields
   Check: `aspirations.py` `validate_goal` has `if "depends_on" in goal:` block
   Check: `aspirations.py` `validate_goal` depends_on validation checks goal_id appears in blocked_by

   # MAC4: Self-abstention in goal-selector
   Check: `goal-selector.py` `collect_candidates` has `if goal.get("abstained_by") == AGENT_NAME: continue`
   Check: Abstention filter is BEFORE claim check (abstained goals never reach claim logic)

   # MAC5: Self-abstention in aspirations-select
   Check: `aspirations-select/SKILL.md` has "Phase 2.55: Self-Abstention Check"
   Check: Phase 2.55 has double-abstention guard (IF goal.abstained_by is set AND != AGENT_NAME → defer)

   # MAC6: Output-centric handoff posting
   Check: `aspirations-verify/SKILL.md` On Pass has `--type handoff` board post for world goals
   Check: `aspirations-verify/SKILL.md` handoff post is BEFORE "Unblock dependent goals" reference

   # MAC7: Constitutional rings convention
   Check: `core/config/conventions/constitutional-rings.md` exists with Ring 1, Ring 2, Ring 3 sections
   Check: `CLAUDE.md` convention index includes `constitutional-rings.md`

   # MAC8: Guardrail for lock path convention
   Bash: bash core/scripts/guardrails-read.sh --id guard-056 2>/dev/null | python3 -c "
import sys,json
g = json.load(sys.stdin)
assert 'with_suffix' in g['rule'], 'guard-056 missing with_suffix'
print(f'PASS: guard-056 active')
" → verify lock path guardrail exists

   # Consolidation triage gate evidence checks (Section CT)
   # Verifies the lean/full triage gate in aspirations-consolidate Step 0.1

   # CT1: Triage gate structure
   Check: `aspirations-consolidate/SKILL.md` has `0.1. CONSOLIDATION TRIAGE GATE:` step
   Check: `aspirations-consolidate/SKILL.md` triage reads `wm-read.sh --json` (pre-scan)
   Check: `aspirations-consolidate/SKILL.md` triage reads `pipeline-read.sh --unreflected --counts`
   Check: `aspirations-consolidate/SKILL.md` triage checks `overflow-queue.yaml` existence
   Check: `aspirations-consolidate/SKILL.md` has `consolidation_tier = "lean"` AND `consolidation_tier = "full"` assignments

   # CT2: Lean fast path skips data steps, keeps mandatory steps
   Check: `aspirations-consolidate/SKILL.md` has `IF consolidation_tier == "lean":` block
   Check: `aspirations-consolidate/SKILL.md` lean path calls `experience-archive.sh` (timer-based, always runs)
   Check: `aspirations-consolidate/SKILL.md` lean path comment says `JUMP → Step 2.9` (experience distillation)
   Check: `aspirations-consolidate/SKILL.md` has `# ── END FULL PATH` marker before Step 3
   Check: Steps 3, 4, 5 are OUTSIDE the full-path block (run regardless of tier)

   # CT3: Safety rails — violations, overflow, ceiling all force full
   Check: `aspirations-consolidate/SKILL.md` triage has `violations_count > 0` → full
   Check: `aspirations-consolidate/SKILL.md` triage has `has_overflow` → full
   Check: `aspirations-consolidate/SKILL.md` triage has `prior_lean >= 3` → full (anti-suppression ceiling)
   Check: `aspirations-consolidate/SKILL.md` triage has script-error fallback → full

   # CT4: Streak file is source of truth (NOT handoff.yaml, which boot deletes)
   Check: `aspirations-consolidate/SKILL.md` Step 0.1 reads `consolidation-lean-streak` (NOT handoff.yaml)
   Check: `aspirations-consolidate/SKILL.md` Step 9 writes `consolidation-lean-streak` file
   Check: `aspirations-consolidate/SKILL.md` handoff `consolidation_meta` comment says "informational copy"
   Check: `boot/SKILL.md` does NOT delete or consume `consolidation-lean-streak`

   # CT5: Checklist includes triage and lean status
   Check: `aspirations-consolidate/SKILL.md` checklist has `Triage:` as first entry
   Check: `aspirations-consolidate/SKILL.md` checklist includes `skipped (lean)` as valid status
   Check: `aspirations-consolidate/SKILL.md` checklist includes Step 0.7 Gotcha Sweep

   # CT6: Journal records triage decision
   Check: `aspirations-consolidate/SKILL.md` Step 3 journal format includes `Triage:` line

   # CT7: Convention documentation
   Check: `core/config/conventions/handoff-working-memory.md` has `consolidation_meta` section
   Check: `core/config/conventions/handoff-working-memory.md` mentions `consolidation-lean-streak` as source of truth

   # Creative Learning Expansion evidence checks (Section CLE)
   # Verifies the 7 changes from the creative-learning-expansion plan (2026-04-05)

   # CLE1: Spark questions — first_principles and experiential_hypothesis promoted to active
   Check: `spark-questions.yaml` has sq-016 (first_principles) and sq-017 (experiential_hypothesis) in `seed_questions`
   Check: `spark-questions.yaml` `max_active_questions` is 17 (not 15)
   Bash: MIND_AGENT=$MIND_AGENT spark-questions-read.sh --active → verify 17 active questions
   Bash: verify `first_principles` and `experiential_hypothesis` categories are present in active list

   # CLE2: Creative lens and routine lens templates exist
   Check: `reflection-templates.yaml` has `creative_lens:` section with 5 questions
   Check: `reflection-templates.yaml` has `routine_lens:` section with 8 questions
   Check: `reflection-templates.yaml` has `domain_templates:` with code, infrastructure, research
   Check: `reflection-templates.yaml` `initial_state.templates` mirrors the framework section (including routine_lens)

   # CLE3: Routine spark expanded
   Check: `aspirations-spark/SKILL.md` routine_spark mode filters 6 categories (not 3)
   Check: categories include `first_principles`, `transfer`, `surprise` alongside the original 3
   Check: `aspirations/SKILL.md` has NO `% 3` gate on routine spark (fires every routine)

   # CLE4: Routine state-update has operational reflection
   Check: `aspirations-state-update/SKILL.md` routine path has "Step 5r" routine-lens question
   Check: routine path reads `routine_lens.questions` from `reflection-templates.yaml`
   Check: routine path uses `hash(goal.id + str(goal.achievedCount))` for question rotation
   Check: routine path has "Step 8r" accumulation check (every 5th routine)

   # CLE5: Divergent alternatives in reflection pipeline
   Check: `reflect-on-outcome/SKILL.md` has "Step 2.8: Divergent Alternatives"
   Check: Step 2.8 fires when surprise >= 3 OR outcome was CORRECTED
   Check: Step 3.5 skips creative_lens when divergent_context is non-empty (no double execution)

   # CLE6: Deep code review protocol
   Check: `aspirations/SKILL.md` Step B0 has 5-phase review (R1-R5) with hypothesis formation
   Check: `aspirations/SKILL.md` Step B0 has `deep_review_count` cap at 3
   Check: `coordination.md` has "Deep Review Protocol" section

   # CLE7: Critical path visibility
   Check: `team-state.py` EMPTY_STATE has `critical_blockers` field
   Check: `team-state.py` `read_state()` has schema migration backfill loop
   Check: `aspirations-consolidate/SKILL.md` Step 8.87 gathers blocked data and updates critical_blockers
   Check: `aspirations-consolidate/SKILL.md` Step 9 handoff schema has `critical_path:` section
   Check: `boot/SKILL.md` has "Step 4c" Critical Path Resume

   # CLE7a: Critical-blockers drift guard (rb-464, guard-388, g-248-38)
   # The critical_blockers snapshot accumulates entries at blocker-creation
   # time with no automatic cleanup on goal completion. Without a drift guard,
   # stale entries (e.g., g-115-156 case 2026-04-22) survive in team-state.yaml
   # and both agents narrate false claims from them on /prime. The guard
   # purges entries whose goal_id resolves to a terminal status.
   Check: `core/scripts/team-state-sync-blockers.py` exists and defines `TERMINAL_STATUSES` set containing {completed, archived, skipped, expired}
   Check: `core/scripts/team-state-sync-blockers.py` `_candidate_sources()` walks BOTH world and agent aspiration files, live AND archive (zombie-blocker survival check — if agent-local files are missing, an archived aspiration's goals vanish from the status map and the entries never purge)
   Check: `core/scripts/team-state-sync-blockers.sh` exists (thin bash wrapper)
   Check: `.claude/skills/prime/SKILL.md` numbered substep 5.45 invokes `team-state-sync-blockers.sh` BEFORE substep 5.5's `team-state-read.sh --json` (so downstream consumers read purged state, not stale snapshot). Bash: awk '/^5\.45\./,/^5\.5\./' .claude/skills/prime/SKILL.md | grep -qc "team-state-sync-blockers.sh" && echo PASS || echo FAIL

   # CLE8: Health dashboard in progress reports
   Check: `agent-completion-report/SKILL.md` Phase 2 has step 11 (System Health Metrics) with 6 sub-steps
   Check: `agent-completion-report/SKILL.md` Phase 3 has "## System Health" section with overall verdict

   # CLE9: Meta alignment
   # Use $META_DIR resolution explicitly — the bare `meta/` prefix in previous
   # versions produced false FAILs when a verification agent ran without
   # MIND_AGENT bound and META_DIR fell back to project-root/meta.
   Bash: source core/scripts/_paths.sh && grep -c "Creative-lens reflection for routine" "$META_DIR/improvement-instructions.md" → expect >= 1 (file exists AND contains the string)
   Bash: source core/scripts/_paths.sh && grep -c "Skip effort" "$META_DIR/improvement-instructions.md" → expect 0 (old anti-pattern retired)
   Check: guard-097 exists (artifact tracking inside conditional)

   # SS1: Strategic Scan infrastructure
   Check: `core/config/aspirations.yaml` has `strategic_scan:` section with `goal_cadence`, `hours_cadence`, `recurring_ratio_trigger`, `knowledge_staleness_days`, `concentration_threshold`, `max_signals_per_scan`
   Check: `core/config/aspirations.yaml` modifiable section has bounds for all 6 `strategic_scan.*` parameters
   Check: `.claude/skills/aspirations-strategic-scan/SKILL.md` exists with phases S1-S5
   Check: `aspirations-strategic-scan/SKILL.md` front matter has `minimum_mode: autonomous` and `parent-skill: aspirations`

   # SS2: Strategic Scan wiring into main loop
   Check: `aspirations/SKILL.md` OR `aspirations-loop-digest.md` has "STRATEGIC SCAN (Phase 1.5)" between precheck and goal selection
   Check: Phase 1.5 has three trigger conditions (goal_cadence, recurring_settling, time_cadence)
   # Single-writer cadence stamp (per guard-155): the SKILL owns the write, not the orchestrator.
   # An earlier iteration of this check read only the pseudocode text, which passed for the
   # digest phrase "update wm.last_strategic_scan" even though no wm-set.sh call existed
   # anywhere in the repo — the time_cadence trigger silently never fired. See rb for F3 in the
   # 2026-04-19 verification plan at C:\Users\Zachary\.claude\plans\temporal-hopping-otter.md.
   Bash: grep -rn "verified-wm-set.sh last_strategic_scan" .claude/skills core/scripts → expect >= 1 (verified writer exists; g-115-1416 routed the S5 stamp through verified-wm-set.sh for read-back+retry)
   Bash: grep -c "verified-wm-set.sh last_strategic_scan" .claude/skills/aspirations-strategic-scan/SKILL.md → expect exactly 1 (single-writer rule, routed through the verified wrapper)
   # core/scripts/verified-wm-set.sh exists (the generalized write -> read-back -> assert -> retry-once wrapper)
   Bash: grep -c "Skill(aspirations-strategic-scan)" core/config/aspirations-loop-digest.md .claude/skills/aspirations/SKILL.md → expect >= 1 (invoker exists)

   # SS3: Strategic Scan signal flow
   Check: `aspirations-strategic-scan/SKILL.md` Phase S5 routes HIGH signals to `aspirations-add-goal.sh`, MEDIUM to `/create-aspiration`, LOW to `wm-set.sh strategic_scan_signals`
   Check: `aspirations-spark/SKILL.md` Phase R2 reads `strategic_scan_signals` from working memory
   Check: `create-aspiration/SKILL.md` Phase E reads `strategic_scan_signals` from working memory (E1) and `scan_context` from caller (E3)

   # FC1: Functionally Complete detection
   Check: `aspirations/SKILL.md` Phase 7 computes `non_recurring` and `non_recurring_done` before the completion review branch
   Check: `aspirations/SKILL.md` Phase 7 has `functionally_complete=true` path for aspirations with recurring goals where all non-recurring are done
   Bash: grep -c "functionally_complete=true" .claude/skills/aspirations/SKILL.md -> verify returns 1

   # FC2: Functionally Complete handling in completion review
   Check: `aspirations-complete-review/SKILL.md` Phase 7 has "Functionally Complete Path" before the standard recurring guard
   Check: `aspirations-complete-review/SKILL.md` functionally complete path checks `functionally_complete_at` guard (one-time fire)
   Check: `aspirations-complete-review/SKILL.md` functionally complete path calls `aspirations-meta-update.sh` to set `functionally_complete_at`
   Check: `aspirations-complete-review/SKILL.md` functionally complete path invokes `/create-aspiration from-self --plan` with `replacement_context`
   Check: `aspirations-complete-review/SKILL.md` functionally complete path does NOT archive (RETURN with should_archive=false)

   # PE1: Phase E (World Observation) in create-aspiration
   Check: `create-aspiration/SKILL.md` has "Phase E" with sub-phases E1 (scan signals), E2 (experience themes), E3 (caller context), E4 (curiosity)
   Check: `create-aspiration/SKILL.md` combine line references "seven phases (A, A.5, B, C, D, D.5, E)"

   # SR1: Spark R2 (Signal-Escalated Work Discovery)
   Check: `aspirations-spark/SKILL.md` routine_spark block saves questions to `all_active_questions` (not bare `result`)
   Check: `aspirations-spark/SKILL.md` Phase R2 filters `all_active_questions` for `aspiration_generation` and `work_discovery` categories
   Check: `aspirations-spark/SKILL.md` Phase R2 is gated by `any_spark_fired OR has_scan_signals`

   # Idle-tick sentinel + evidence-based staleness guard evidence checks (Section IDT)
   # These enforce the 2026-04-17 fixes for (a) blocked-sleep token waste during idle
   # recovery and (b) stale-iteration-checkpoint silently reverting completed goals.

   # IDT1: Pre-skill sentinel exists and is wired both places
   Check: `core/scripts/idle-tick.sh` exists
   Check: `core/scripts/idle-tick.sh` reads `blocked_sleep_until` via wm-read.sh (not direct YAML read)
   Check: `core/scripts/idle-tick.sh` checks `stop-requested` AND `stop-loop` before sleeping (stop signals win)
   Check: `core/scripts/idle-tick.sh` does NOT write `blocked_sleep_until` (single owner = Phase -0.5e)
   Check: `.claude/settings.json` SessionStart(compact) hook invokes `idle-tick.sh` AFTER `postcompact-restore.sh`
   Check: `aspirations/SKILL.md` Phase -0.5e delegates to `idle-tick.sh` before any skill-body execution

   # IDT2: Evidence-based staleness guard
   Check: `core/scripts/goal-completion-evidence.sh` exists
   Check: `goal-completion-evidence.sh` has_evidence is true ONLY when status=="completed" (journal/experience counts are diagnostic-only; see guard-138)
   Check: `goal-completion-evidence.sh` has NO `|| echo` fallback silencers (set -euo pipefail, fail open — see guard-139)
   Check: `aspirations/SKILL.md` Phase -1.4 calls `goal-completion-evidence.sh` before reverting a stale checkpoint
   Check: `aspirations/SKILL.md` Phase -1.4 uses explicit `should_reconstruct` flag (not ambiguous "fall through to the ELSE below" — see guard-137)

   # IDT3: last_updated on every checkpoint write
   Check: `aspirations/SKILL.md` every `iteration-checkpoint.json` write includes `"last_updated": "<ISO now>"` (4 sites: execute/verify/spark/state_update)
   Check: `aspirations/SKILL.md` Phase -1.4 reads `last_updated` first, falls back to `started_at` only for pre-migration checkpoints

   # IDT4: Background sleep MUST use interruptible-sleep.sh (stop-responsive).
   # Phase B7 was extracted from aspirations/SKILL.md to
   # aspirations-all-blocked/SKILL.md in commit 3b9f315 (2026-04-20, MW-Item-2).
   # DO NOT re-add checks targeting aspirations/SKILL.md Phase B7 — the phase
   # has moved and the old path will always FAIL.
   Check: `.claude/skills/aspirations-all-blocked/SKILL.md` Step B7 uses `interruptible-sleep.sh` with `run_in_background=true` (NOT plain `sleep N`)
   Check: `core/scripts/idle-tick.sh` directive text instructs model to emit `interruptible-sleep.sh` (not plain sleep)
   Bash (interruptible-sleep-writer): grep -c "interruptible-sleep.sh" .claude/skills/aspirations-all-blocked/SKILL.md | awk '{print ($1 >= 1) ? "PASS: interruptible-sleep referenced in sub-skill" : "FAIL: plain sleep may be used — stop signal will be ignored during sleep"}'

   # IDT5: Phase B7 is single-branch (no cron fallback) and writes
   # blocked_sleep_until before RETURN so idle-tick sentinel can observe it on
   # next session entry. Both invariants moved with Phase B7 to aspirations-
   # all-blocked/SKILL.md.
   Check: `.claude/skills/aspirations-all-blocked/SKILL.md` Step B7 has no CronCreate call and no TRY/EXCEPT cron fallback
   Bash (no-cron-fallback): grep -c "CronCreate" .claude/skills/aspirations-all-blocked/SKILL.md | awk '{print ($1 == 0) ? "PASS: no cron fallback in B7 — single-branch invariant holds" : "FAIL: cron fallback present — B7 must be single-branch (interruptible-sleep + breadcrumb only)"}'
   Check: `.claude/skills/aspirations-all-blocked/SKILL.md` Step B7 writes `blocked_sleep_until` before RETURN so idle-tick sentinel sees it on next entry
   Bash (blocked-sleep-until-writer): grep -c "blocked_sleep_until" .claude/skills/aspirations-all-blocked/SKILL.md | awk '{print ($1 >= 1) ? "PASS: blocked_sleep_until written by sub-skill" : "FAIL: blocked_sleep_until absent — idle-tick sentinel will never observe the sleep breadcrumb"}'

   # Capability Gate + Blocker Recheck evidence checks (Section CG)
   # Enforces the 2026-04-17 fix for route-to-user misdirection: the three
   # scripts, the SKILL.md wiring, the four invariants protected by in-code
   # comments, and the encoded lessons (rb-223/224/225 + guard-142).

   # CG1: Scripts exist and are wired into the loop
   Check: `core/scripts/capability-gate.py` exists and imports yaml at top level (hard dependency, no try/except fallback)
   Check: `core/scripts/capability-gate.sh` exists and uses `exec python3 "$CORE_ROOT/scripts/capability-gate.py" "$@"` (thin wrapper pattern)
   # Docstring/flag sync — surfaced 2026-04-18 review. Top-level docstring must reference the actual flag
   # (`--output human`), not a non-existent shorthand (`--human`) that argparse rejects.
   Bash: grep -E '^- Output' core/scripts/capability-gate.py | grep -vq '\-\-human\b' && echo "PASS" || echo "FAIL: docstring references --human (actual flag is --output human)"
   Check: `core/scripts/blocker-recheck.py` exists
   Check: `core/scripts/blocker-recheck.sh` exists and uses the same thin-wrapper pattern
   Check: `.claude/skills/aspirations-execute/SKILL.md` CREATE_BLOCKER Protocol invokes `bash core/scripts/create-blocker.sh` (NOT direct py/python3 — platform portability; create-blocker.sh wraps blocker-create-gate, capability-gate, conclusion-record, and aspirations-add-goal under one orchestrator)
   Check: `.claude/skills/aspirations-execute/SKILL.md` CREATE_BLOCKER Protocol section warns against bypassing gates ("Skipping either has historically produced false-positive blockers that put the agent to sleep on non-problems")
   Check: `.claude/skills/aspirations-precheck/SKILL.md` Phase 0.5b.0.5 invokes `bash core/scripts/blocker-recheck.sh --max-age-hours {config.proactive_escalation.blocker_age_hours} --apply`
   Check: `.claude/skills/aspirations-precheck/SKILL.md` Phase 0.5b.0.5 runs BEFORE Phase 0.5b.1 (so user is not escalated about a blocker that's auto-clearable)

   # SNF: set_nested_field hardening (g-240-30)
   Check: `core/scripts/_jsonl_helpers.py` exists and defines `set_nested_field`
   Check: `core/scripts/_jsonl_helpers.py` docstring on set_nested_field says "REFUSES to create missing parents"
   Check: `core/scripts/reasoning-bank.py` imports `from _jsonl_helpers import set_nested_field`
   Check: `core/scripts/experience.py` imports `from _jsonl_helpers import set_nested_field`
   Check: `core/scripts/pipeline.py` imports `from _jsonl_helpers import set_nested_field`
   Check: `core/scripts/pattern-signatures.py` imports `from _jsonl_helpers import set_nested_field`
   Check: NO `def set_nested_field` remains in reasoning-bank.py / experience.py / pipeline.py / pattern-signatures.py (SSOT)
   Bash: py -3 -c "import sys; sys.path.insert(0, 'core/scripts'); from _jsonl_helpers import set_nested_field; rec={'a':{'x':1}}; 
   try: set_nested_field(rec, 'b.x', 5); print('FAIL: typo accepted')
   except ValueError: print('PASS: typo rejects missing parent')"
   Bash: py -3 -c "import sys; sys.path.insert(0, 'core/scripts'); from _jsonl_helpers import set_nested_field; rec={'a':{'x':1}}; set_nested_field(rec, 'a.x', 5); assert rec=={'a':{'x':5}}, f'FAIL: legit broke {rec}'; print('PASS: legitimate nested path')"
   Bash: py -3 -c "import sys; sys.path.insert(0, 'core/scripts'); from _jsonl_helpers import set_nested_field; rec={'id':'x'}; set_nested_field(rec, 'experience_ref', 'exp-y'); assert rec.get('experience_ref')=='exp-y'; print('PASS: single-level top-field')"

   # DCT: Decompose Candidate Tightening (g-240-35)
   Check: `core/scripts/tree.py` defines `_qualifies_for_decomposition` helper
   Check: `core/scripts/tree.py` `_CANONICAL_END_SECTIONS` set contains "verified values" and "decision rules"
   Check: `core/scripts/tree.py` `get_decompose_candidates` calls `_qualifies_for_decomposition` inside the `line_count > threshold` branch (not outside it — line-count check still first-pass)
   Bash: py -3 -c "import sys; sys.path.insert(0, 'core/scripts'); import tree as T; q,r = T._qualifies_for_decomposition('nonexistent-path-xyz.md'); assert q == True and r is None, 'FAIL: fail-open on OSError missing'; print('PASS: fail-open on read error')"

   # MSC: Monitor Stale Check (g-240-37)
   Check: `core/scripts/monitor-stale-check.py` exists
   Check: `core/scripts/monitor-stale-check.sh` exists and uses the same thin-wrapper pattern (source _paths.sh, exec python3)
   Check: `core/scripts/monitor-stale-check.sh` probes `processor-run.sh check-complete` and exports MSC_CHECK_COMPLETE_JSON (sig-005 bypass: no Python→bash re-shelling)
   Check: `core/scripts/monitor-stale-check.py` _get_current_run_id reads `os.environ.get("MSC_CHECK_COMPLETE_JSON", ...)` — does NOT shell into bash itself
   Check: `.claude/skills/aspirations-precheck/SKILL.md` Phase 0 "Monitor Stale Check" section invokes `monitor-stale-check.sh --apply`
   Bash: bash core/scripts/monitor-stale-check.sh 2>&1 | py -3 -c "import sys,json; r=json.loads(sys.stdin.read()); assert r.get('mode') in ('dry-run','apply') or 'skipped' in r, f'FAIL: unexpected output: {r}'; print('PASS: monitor-stale-check dry-run produces valid JSON')"

   # CG2: Invariant #1 — whole-token set intersection, not substring matching
   Check: `core/scripts/capability-gate.py` _find_matches has the invariant comment "INVARIANT: whole-token set intersection, NOT substring matching"
   Check: `core/scripts/capability-gate.py` _find_matches uses `keywords & entry_toks` (set intersection) NOT `keyword in text`
   # The "airport" token embeds the substring "port" — if substring matching
   # regressed, "port" would match port-8082 entries. With whole-token set
   # intersection, it does not.
   Bash: MIND_AGENT=bravo bash core/scripts/capability-gate.sh --failure-reason "airport terminal broken" --intended-participants user --output json | py -3 -c "import sys,json; r=json.load(sys.stdin); assert r['match_count']==0, f'FAIL: substring false-positive regression ({r[\"match_count\"]} matches — \"airport\" matched \"port\"?)'; print('PASS: no substring false-positive on airport/port')"

   # CG3: Invariant #2 — markdown-table header-row skip
   Check: `core/scripts/capability-gate.py` _load_capability_routing tracks `seen_divider` and skips rows until divider seen
   Check: `core/scripts/capability-gate.py` _load_capability_routing resets `seen_divider = False` on each new `## ` heading
   Bash: MIND_AGENT=bravo bash core/scripts/capability-gate.sh --failure-reason "agent provisions this service" --intended-participants user --output json | py -3 -c "import sys,json; r=json.load(sys.stdin); assert r['match_count']==0 or all('provisions' not in (m.get('row','') or '') for m in r['matches']), 'FAIL: header row captured'; print('PASS: header row skipped')"

   # CG4: Invariant #3 — sys.executable, not bash subprocess, for Python children
   Check: `core/scripts/blocker-recheck.py` _py has the invariant comment "INVARIANT: uses sys.executable directly, not a bash subprocess"
   Check: `core/scripts/blocker-recheck.py` _py calls `_run([sys.executable] + args, ...)` NOT `_run(["bash", ...])` or `_run(["python3", ...])`

   # CG5: Invariant #4 — apply branch creates Investigate goal FIRST, then clears blocker
   Check: `core/scripts/blocker-recheck.py` apply branch has the invariant comment "INVARIANT: create the Investigate goal FIRST, THEN clear the blocker"
   Check: `core/scripts/blocker-recheck.py` apply branch calls `_add_investigate_goal(...)` BEFORE setting `b["resolution"]`
   Check: `core/scripts/blocker-recheck.py` apply branch skips the clear step when `goal_id.startswith("<add-goal-failed")`

   # CG6: Fail-open on dependency errors (see guard-142)
   Check: `core/scripts/capability-gate.py` _load_forged_skills returns [] when `world_dir is None` (no agent bound)
   Check: `core/scripts/capability-gate.py` _load_skill_md_triggers returns [] when `skills_dir` is not a dir
   Check: `core/scripts/capability-gate.py` _load_capability_routing returns [] when the conventions file is unreadable
   Bash: bash core/scripts/capability-gate.sh --failure-reason "no agent bound" --intended-participants user --output json | py -3 -c "import sys,json; r=json.load(sys.stdin); assert r['would_block']==False, f'FAIL: blocked with no agent bound (should fail-open)'; print('PASS: fail-open when no agent bound')"

   # CG7: _STOPWORDS caution comment + no discriminative infra terms
   Check: `core/scripts/capability-gate.py` _STOPWORDS has the caution comment "Do NOT add discriminative infra terms"
   Check: `core/scripts/capability-gate.py` _STOPWORDS does NOT contain "llama-server", "bitnet", "processor-run", "efs-ssh" (discriminative terms that must stay as keywords)

   # CG8: Override flag works and is audited
   Check: `core/scripts/capability-gate.py` when --override-agent-match is provided, echoes justification to stderr with prefix "[capability-gate] override applied:"
   Bash: MIND_AGENT=bravo bash core/scripts/capability-gate.sh --failure-reason "llama-server on port 8082 is down" --intended-participants user --override-agent-match "synthetic test" --output json 2>&1 | py -3 -c "import sys; output=sys.stdin.read(); assert '[capability-gate] override applied' in output, 'FAIL: override not echoed to stderr'; assert '\"would_block\": false' in output.lower() or '\"would_block\":false' in output.lower(), 'FAIL: override did not unblock'; print('PASS: override audit + unblock both work')"

   # CG9: Enforcement pointer exists in capability-before-user rule
   Check: `.claude/rules/capability-before-user.md` has a blockquote (starts with `>`) referencing `capability-gate.py` or Step 2.6
   Check: `.claude/rules/capability-before-user.md` blockquote says the gate is a "safety net, not a replacement" (LLM-side checklist is still required)

   # CG10: Encoded lessons active
   Bash: MIND_AGENT=bravo bash core/scripts/reasoning-bank-read.sh --active | py -3 -c "import sys,json; d=json.loads(sys.stdin.read() or '[]'); ids={r.get('id') for r in d}; missing=[i for i in ('rb-223','rb-224','rb-225') if i not in ids]; assert not missing, f'FAIL: missing rb entries: {missing}'; print('PASS: rb-223/224/225 all active')"
   Bash: MIND_AGENT=bravo bash core/scripts/guardrails-read.sh --active | py -3 -c "import sys,json; d=json.loads(sys.stdin.read() or '[]'); ids={g.get('id') for g in d}; assert 'guard-142' in ids, 'FAIL: guard-142 not active'; print('PASS: guard-142 active')"
   Check: `world/knowledge/tree/_tree.yaml` contains `capability-routing-enforcement:` node registered under `system` parent

   # CG11: Probe Before Defer — sibling rule to capability-before-user
   # Writing `defer_reason: "blocked on user-initiated X"` on a goal routes work
   # to the user with the same effect as `participants: [user]` — without the
   # capability-gate check. The chokepoint is in aspirations.py cmd_update_goal:
   # when field == "defer_reason" and value is non-null, _run_capability_gate_for_defer
   # must invoke capability-gate.py. Without either piece, defers become a silent
   # bypass of capability routing.
   Check: `.claude/rules/probe-before-defer.md` exists (sibling rule to capability-before-user.md)
   Check: `.claude/rules/capability-before-user.md` has a "Sibling Rule: Probe Before Defer" section referencing `.claude/rules/probe-before-defer.md`
   Check: `core/scripts/aspirations.py` defines `_run_capability_gate_for_defer` function
   Bash (defer-gate-function): grep -c "^def _run_capability_gate_for_defer" core/scripts/aspirations.py | awk '{print ($1 >= 1) ? "PASS: gate function defined" : "FAIL: _run_capability_gate_for_defer missing"}'
   Bash (defer-gate-wired): grep -c 'field == "defer_reason"' core/scripts/aspirations.py | awk '{print ($1 >= 1) ? "PASS: cmd_update_goal routes defer_reason through gate" : "FAIL: defer_reason bypass path re-introduced"}'
   Bash (defer-gate-call): grep -c "_run_capability_gate_for_defer(" core/scripts/aspirations.py | awk '{print ($1 >= 2) ? "PASS: gate defined AND called" : "FAIL: gate defined but never invoked"}'

   # CG12: STRUCTURED_DEFER_PREFIXES cross-file parity (rb-388)
   # The tuple in aspirations.py lists prefixes that BYPASS the defer-time gate.
   # aspirations-precheck/SKILL.md Phase 0.5b.4 re-probes narrative defers —
   # it MUST skip the same prefixes or it will clear legitimate structured
   # defers on the next iteration (Circuit breaker: → matches run-test-circuit
   # → defer cleared → Phase 5.5 defeated).
   Check: `core/scripts/aspirations.py` defines `_STRUCTURED_DEFER_PREFIXES_LOWER` at module level (case-insensitive match — LLM-authored callers drift casing)
   Check: `core/scripts/aspirations.py` guard block uses `.lower().startswith(_STRUCTURED_DEFER_PREFIXES_LOWER)` (not case-sensitive startswith)
   Check: `.claude/skills/aspirations-precheck/SKILL.md` Phase 0.5b.4 skips `"precondition_unmet:"`, `"blocked_on_dependency"`, AND `"circuit breaker:"` (case-insensitive for the last) — i.e. once per STRUCTURED_DEFER_PREFIXES entry
   Bash (structured-defer-parity): py -3 -c "
import re, pathlib
asp = pathlib.Path('core/scripts/aspirations.py').read_text(encoding='utf-8')
m = re.search(r'STRUCTURED_DEFER_PREFIXES\s*=\s*\(([^)]+)\)', asp, re.DOTALL)
if not m: print('FAIL: STRUCTURED_DEFER_PREFIXES tuple not found'); raise SystemExit(1)
prefixes = [s.strip().strip('\"').strip(\"'\") for s in re.findall(r'\"[^\"]+\"|\'[^\']+\'', m.group(1))]
skill = pathlib.Path('.claude/skills/aspirations-precheck/SKILL.md').read_text(encoding='utf-8')
missing = [p for p in prefixes if p.lower() not in skill.lower()]
print('FAIL: precheck missing skip for ' + str(missing) if missing else 'PASS: all STRUCTURED_DEFER_PREFIXES mirrored in aspirations-precheck Phase 0.5b.4')
"

   # CG13: Context-aware keyword disqualification (rb-389)
   # The dual-check (_VIA_END AND _NEGATION_ANYWHERE_IN_PRE) implements the
   # means-vs-ends distinction: "cannot access EFS" (ends, matches) vs
   # "cannot X via Y" (means, filters Y). Dropping either condition collapses
   # the distinction. Test n_cannot_access_is_still_a_match is the lock-in.
   Check: `core/scripts/capability-gate.py` defines `_VIA_END`, `_NEGATION_ANYWHERE_IN_PRE`, `_BEFORE_GERUND_END`, `_COUNT_BEFORE_END`, `_UNIT_AFTER_START` regex constants
   Check: `core/scripts/capability-gate.py` `_keyword_is_invocation_signal` AND-combines `_VIA_END.search(pre)` with `_NEGATION_ANYWHERE_IN_PRE.search(pre)` (means-vs-ends distinction MUST remain dual-check)
   Check: `core/scripts/capability-gate.py` main() calls `_filter_context_disqualified` after `_extract_keywords` (filter must be wired, not just defined)
   Check: `core/scripts/test-capability-gate.sh` exists (14-case regression suite locks in the context-aware rules)
   Bash (capability-gate-regression): bash core/scripts/test-capability-gate.sh 2>&1 | grep -E "^PASS: [0-9]+ +FAIL: 0$" | grep -q "PASS: 14" && echo "PASS: capability-gate regression suite 14/14" || echo "FAIL: capability-gate regression suite lost cases or added new ones without updating verify-learning — update the '14' floor if suite grew"

   # CG14: Session-requirement narrative pattern (g-248-79)
   # SESSION_REQUIREMENT_PATTERN closes the Layer-B gap where defer_reasons named
   # agent-provisionable session work via "requires X session" phrasings that
   # bypassed the capability-keyword scan. The keystroke-marker classifier
   # exempts genuine user-only sessions (E-press, F5-Play character spawn).
   Check: `core/scripts/capability-gate.py` defines `SESSION_REQUIREMENT_PATTERN` regex constant
   Check: `core/scripts/capability-gate.py` defines `SESSION_REQUIREMENT_KEYSTROKE_MARKERS` list
   Check: `core/scripts/capability-gate.py` defines `_match_session_requirement_patterns` and `_classify_session_requirement` helpers
   Check: `core/scripts/capability-gate.py` main() computes `session_req_matches` and `session_req_classification` after narrative_matches
   Check: `core/scripts/capability-gate.py` main() defines `session_req_block` and ORs it into `would_block`
   Check: `core/scripts/capability-gate.py` `keyword_block` exempts when `session_req_classification != "user_keystroke_required"` (parallel to user_only_matches exemption)
   Check: `world/conventions/capability-routing.md` "Forbidden narrative framings" table contains a row referencing `SESSION_REQUIREMENT_PATTERN` and the keystroke-marker list (kept synced with capability-gate.py)
   Check: `core/scripts/tests/test_capability_gate_session_requirement.py` exists (regression test)
   Bash (session-requirement-block): MIND_AGENT=alpha bash core/scripts/capability-gate.sh --failure-reason "requires multi-NPC RUN session >=8min" --intended-participants user --output json | py -3 -c "import sys,json; r=json.load(sys.stdin); assert r['session_requirement_classification']=='agent_provisionable' and r['would_block']==True, f'FAIL: agent-provisionable RUN session not blocking ({r})'; print('PASS: agent-provisionable session-requirement blocks')"
   Bash (session-requirement-keystroke-pass): MIND_AGENT=alpha bash core/scripts/capability-gate.sh --failure-reason "needs PLAY session with character spawn via F5-click" --intended-participants user --output json | py -3 -c "import sys,json; r=json.load(sys.stdin); assert r['session_requirement_classification']=='user_keystroke_required' and r['would_block']==False, f'FAIL: keystroke session blocked ({r})'; print('PASS: keystroke session-requirement passes')"
   Bash (session-requirement-regression): py -3 core/scripts/tests/test_capability_gate_session_requirement.py 2>&1 | tail -1 | grep -q "All 8 session-requirement cases verified" && echo "PASS: session-requirement regression suite 8/8" || echo "FAIL: session-requirement regression broke"

   # CG15: Stale narrative-defer audit (g-115-706 / g-271-12 canonical incident)
   # defer-recheck.sh catches structured patterns (precondition_unmet:, blocked_on_dependency)
   # but free-form defer narratives naming agent-provisionable scripts can outlive their
   # fail-open TTL. g-271-12 (zeta session 70) found a defer aged 121.6h past the 120h
   # fail-open. This Bash check audits LIVE aspirations.jsonl for defers >=48h that
   # name agent-provisionable scripts (roblox-studio.sh, efs-ssh.sh, aws-exec.sh,
   # operator-api.sh, etc.); zero matches = capability-gate Layer D + defer-recheck.sh
   # are clearing them on cadence. Non-zero = gap that warrants Investigate goal.
   Check: `core/scripts/defer-recheck.py` accepts `--max-age-hours` arg (default 2h re-probe; 48h is the audit threshold for verify-learning's check)
   Check: `.claude/rules/probe-before-defer.md` cross-references capability-gate.py Layer D auto-conversion + .claude/rules/capability-before-user.md sibling rule
   Bash (defer-narrative-stale): py -3 -c "import json, sys; from pathlib import Path; from datetime import datetime; sys.path.insert(0, 'core/scripts'); import _paths; from gates.defer_classifier import _STRUCTURED_DEFER_PREFIXES_LOWER as SDP; now=datetime.now(); th=48; scripts=['roblox-studio.sh','efs-ssh.sh','aws-exec.sh','operator-api.sh','analyze-npc-behavior','run-processor','run-game-session']; stale=[]; [stale.extend([(g.get('id'),(now-datetime.fromisoformat(g.get('last_modified','').rstrip('Z'))).total_seconds()/3600, [s for s in scripts if s in (g.get('defer_reason','') or '').lower()], (g.get('defer_reason','') or '')[:80]) for line in open(p,'r',encoding='utf-8') if line.strip() for rec in [json.loads(line)] for g in rec.get('goals',[]) if (g.get('defer_reason') and g.get('last_modified') and not (g.get('defer_reason','') or '').lower().startswith(SDP) and ((now-datetime.fromisoformat(g.get('last_modified','').rstrip('Z'))).total_seconds()/3600)>=th and any(s in (g.get('defer_reason','') or '').lower() for s in scripts))]) for p in [_paths.WORLD_DIR/'aspirations.jsonl', _paths.AGENT_DIR/'aspirations.jsonl'] if p.exists()]; print(f'FAIL: {len(stale)} stale narrative defers (>={th}h, naming agent-capable script, NON-STRUCTURED): '+str([(s[0],f\"{s[1]:.0f}h\",s[2]) for s in stale[:3]])) if stale else print(f'PASS: 0 stale narrative defers (>={th}h, non-structured) name agent-provisionable scripts (capability-gate Layer D + defer-recheck clearing them on cadence)')"

   # === Section S48: Session-48 framework-cost + correctness artifacts ==================
   # Plan "reflective-watching-chipmunk" delivered four coordinated items. Each
   # ships a persistent artifact the verify pass can check. If any of these
   # files disappear or change shape, the gate they enforce silently dies.

   # S48.1: Orchestrator digest (Item 1)
   Check: `core/config/aspirations-loop-digest.md` exists (digest IS the per-iteration source-of-truth)
   Check: `core/scripts/load-loop-digest.sh` exists and invokes `context-reads.py check-file`
   Check: `.claude/skills/aspirations/SKILL.md` SINGLE ITERATION block points at the digest (no inline phase bodies)
   Check: `core/scripts/validate-loop-digest.sh` does NOT exist (deleted deliberately — the drift-detector became vacuous after the body relocated; see rb-269)
   Bash: bash core/scripts/load-loop-digest.sh | grep -q "aspirations-loop-digest.md\|^$" && echo "PASS: digest loader resolves or no-ops"

   # S48.2: Blocker-create gate (Item 2A)
   Check: `core/scripts/blocker-create-gate.py` exists; `.sh` wrapper exists
   Check: `.claude/skills/aspirations-execute/SKILL.md` CREATE_BLOCKER Protocol pipeline runs blocker-create-gate BEFORE capability-gate
   Bash: py core/scripts/blocker-create-gate.py --help >/dev/null && echo "PASS: gate --help works"
   Bash: echo '{"type":"resource","affected_skills":[],"failure_reason":"98% of records have active_brain=0","evidence":[{"tool":"dynamodb","evidence_type":"query"},{"tool":"lambda","evidence_type":"invoke"}]}' | py core/scripts/blocker-create-gate.py --output json | py -3 -c "import sys,json; r=json.load(sys.stdin); assert r['would_block'] is True, 'FAIL: rb-245 canonical string did not trigger schema_probe'; sc=[c for c in r['checks'] if c['name']=='schema_probe'][0]; assert sc['passed'] is False, 'FAIL: schema_probe passed on statistical negation without probe evidence'; print('PASS: rb-245 canonical string caught')"
   Bash: py -3 -c "import sys; sys.path.insert(0,'core/scripts'); import importlib.util; s=importlib.util.spec_from_file_location('g','core/scripts/blocker-create-gate.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); assert not m._evidence_is_silent({'command':'curl -q https://host'}), 'FAIL: curl -q falsely flagged silent'; assert m._evidence_is_silent({'command':'ssh -q host cmd'}), 'FAIL: ssh -q not caught as silent'; print('PASS: silent-flag scoping correct')"

   # S48.3: Abbreviation schema + audit (Item 2B)
   Check: `core/config/obligation-schema.yaml` exists and contains top-level `obligations:` with verify/state/learn/spark entries
   Bash: py -3 -c "import yaml; d=yaml.safe_load(open('core/config/obligation-schema.yaml',encoding='utf-8')); o=d['obligations']; missing=[k for k in ('verify','state','learn','spark') if k not in o]; assert not missing, f'FAIL: missing obligations {missing}'; print('PASS: obligation-schema has all 4 abbreviable obligations')"
   Check: `.claude/skills/aspirations-learning-gate/SKILL.md` has a "Phase 9.5d" section referencing context-budget.json AND iteration-checkpoint.json (NOT reasoning-snapshot's nonexistent `context_budget.zone` field)
   Check: four obligation SKILL.md files (verify, state-update, learning-gate, spark) each contain an "Abbreviation Policy" heading referencing `core/config/obligation-schema.yaml`

   # S48.4: Tree-maintenance instrumentation (Item 4)
   Check: `core/scripts/tree-maintenance-read.py` exists; `.sh` wrapper exists
   Check: `core/scripts/tree.py` `cmd_record_maintenance` has a `--with-run-record` branch that appends to `world/tree-maintenance-log.jsonl` via `locked_append_jsonl`
   Check: `core/scripts/tree.py` `get_distill_candidates`, `get_decompose_candidates`, `get_redistribute_candidates` all accept `include_skipped=True` and return `{candidates, skipped}` shape when set
   Check: `.claude/skills/tree/SKILL.md` has a heredoc-style stdin JSON invocation of `tree-update.sh --record-maintenance --with-run-record` in Steps 9 and 10
   Bash: bash core/scripts/tree-maintenance-read.sh --aggregate --json | py -3 -c "import sys,json; d=json.load(sys.stdin); assert 'run_count' in d and 'candidates_pre_filter' in d, 'FAIL: aggregate shape regressed'; print('PASS: tree-maintenance-read --aggregate schema OK')"

   # S48.5: Shared helpers (refactor parity)
   Check: `core/scripts/_skill_md.py` exports `parse_front_matter`, `get_triggers`, `get_companion_scripts`
   Check: `core/scripts/capability-gate.py` imports `parse_front_matter` from `_skill_md` (no inline FRONT_MATTER_RE)
   Check: `core/scripts/blocker-create-gate.py` imports `locked_append_jsonl` directly via `from _fileops import ...` (not importlib)

   # S48.6: Encoded lessons from session-48 review passes
   Bash: MIND_AGENT=bravo bash core/scripts/reasoning-bank-read.sh --active | py -3 -c "import sys,json; d=json.loads(sys.stdin.read() or '[]'); ids={r.get('id') for r in d}; missing=[i for i in ('rb-268','rb-269') if i not in ids]; assert not missing, f'FAIL: missing rb entries: {missing}'; print('PASS: rb-268/269 both active')"

   # S48.7: Concurrency-and-tree-engine regression suite (g-115-419, sq-018, /encode-session 2026-05-08 Lane 5)
   # Five regression tests built in g-115-417 + earlier sessions guard distinct
   # invariants in tree.py / utilization-feedback / journal-append / retrieve.py.
   # Each test was exercised in isolation but never wired into verify-learning,
   # so silent regressions could land between runs. Group-runs as a cohort.
   Check: tree.py R-M-W lock pattern preserved on the 9 cmd_* fns. Bash: py -3 core/scripts/tests/test_remove_child_orphan_gate.py 2>&1 | tail -1 | grep -q 'PASS' && echo 'PASS: tree cmd_remove_child orphan gate + cmd_batch atomic refusal' || echo 'FAIL: cmd_remove_child orphan gate or cmd_batch atomic refusal regressed'
   Check: cmd_set auto-propagation + self-graduation works. Bash: py -3 core/scripts/tests/test_cmd_set_auto_propagate.py 2>&1 | tail -1 | grep -q 'PASS' && echo 'PASS: cmd_set confidence-set propagation' || echo 'FAIL: cmd_set confidence-set propagation regressed'
   Check: utilization-feedback --all-unknown is no-op on counters and phase-4-26-gate still blocks. Bash: py -3 core/scripts/tests/test_all_unknown_backstop.py 2>&1 | tail -1 | grep -q 'PASS' && echo 'PASS: --all-unknown backstop semantics' || echo 'FAIL: --all-unknown backstop semantics regressed'
   Check: journal-append tree-cite scan increments + CRLF strip works. Bash: py -3 core/scripts/tests/test_journal_tree_cite_scan.py 2>&1 | tail -1 | grep -q 'PASS' && echo 'PASS: journal-append tree-cite scan' || echo 'FAIL: journal-append tree-cite scan regressed (likely CRLF or _resolve_bash)'
   Check: retrieve.py concurrent retrieval-count bumps survive 8-way racers. Bash: py -3 core/scripts/tests/test_retrieve_write_locking.py 2>&1 | tail -1 | grep -q 'PASS' && echo 'PASS: locked_modify_yaml retrieval bump race' || echo 'FAIL: locked_modify_yaml retrieval bump race regressed'

   # S48.8: _fileops.py concurrency primitives (g-115-428, encode-session 2026-05-08)
   # The 2026-05-08 refactor added three primitives to core/scripts/_fileops.py
   # and a JSONL id-race regression test. The primitives are consumed by
   # spark-questions, reasoning-bank, and pattern-signatures auto-id paths;
   # silent removal would re-introduce the race that motivated them.
   # Reference node: world/knowledge/tree/system/system-constraints-loop/jsonl-read-modify-write-race.md
   Check: locked_modify_jsonl primitive present. Bash: grep -q '^def locked_modify_jsonl' core/scripts/_fileops.py && echo 'PASS: locked_modify_jsonl' || echo 'FAIL: locked_modify_jsonl removed from _fileops.py'
   Check: locked_append_jsonl_with_allocator primitive present. Bash: grep -q '^def locked_append_jsonl_with_allocator' core/scripts/_fileops.py && echo 'PASS: locked_append_jsonl_with_allocator' || echo 'FAIL: locked_append_jsonl_with_allocator removed from _fileops.py'
   Check: next_id_for_prefix helper present. Bash: grep -q '^def next_id_for_prefix' core/scripts/_fileops.py && echo 'PASS: next_id_for_prefix' || echo 'FAIL: next_id_for_prefix removed from _fileops.py'
   Check: JSONL id-race concurrency test still passes. Bash: test -x core/scripts/tests/test_jsonl_id_race.sh && bash core/scripts/tests/test_jsonl_id_race.sh 2>&1 | tail -1 | grep -q 'PASS' && echo 'PASS: test_jsonl_id_race.sh' || echo 'FAIL: test_jsonl_id_race.sh missing or regressed'
   Check: no strict-padded id regex resurfaces in auto-id call sites (paired with allocators expects unpadded). Bash: grep -nE '\\d\{[0-9]+\}\$' core/scripts/spark-questions.py core/scripts/reasoning-bank.py core/scripts/pattern-signatures.py 2>/dev/null && echo 'FAIL: strict-padded id regex found in auto-id allocator client' || echo 'PASS: no strict-padded id regex in auto-id clients'

   # S48.9: self.md edit-site uniform front-matter (g-115-426)
   # All 4 sites that Edit existing agents/<agent>/self.md MUST set BOTH last_updated AND
   # last_update_trigger in the SAME Edit. Sites: aspirations-spark sq-012 handler,
   # encode-session Lane 7, felt-sense-checkin Material lane, respond user-correction.
   # Catches drift if a future author adds shorthand (e.g., updates only last_updated).
   # 400-char proximity window covers both block-style sites and inline table-cell sites.
   Check: every Edit agents/<agent>/self.md site sets both last_updated AND last_update_trigger. Bash: py -3 -c "import re; sites=['.claude/skills/aspirations-spark/SKILL.md','.claude/skills/encode-session/SKILL.md','.claude/skills/felt-sense-checkin/SKILL.md','.claude/skills/respond/SKILL.md']; fail=[]; [fail.append(f'{p}@{m.start()}: missing {fld}') for p in sites for m in re.finditer(r'Edit[^\n]*self\.md[^\n]*', open(p,encoding='utf-8').read()) for fld in ('last_updated','last_update_trigger') if fld not in open(p,encoding='utf-8').read()[m.start():m.start()+400]]; print('PASS: all 4 self.md edit sites have both last_updated + last_update_trigger' if not fail else 'FAIL: ' + '; '.join(fail))"

   # S48.10: Redundant tree-node last_updated writes (g-115-670 + g-115-671)
   # The T21 PostToolUse hook (`core/scripts/tree-front-matter-sync.py`) fires on every
   # tree-node Edit and atomically bumps BOTH the .md front matter AND _tree.yaml's
   # last_updated field. Explicit shell calls that set those two fields after an Edit
   # are therefore redundant; last_update_trigger writes via _tree.yaml are dead
   # because the field only lives in the .md front matter (guard-531).
   # Cleanup pass: g-115-670 removed these from SKILL.md pseudocode. g-115-671 added the
   # check. The regex anchors on the literal shell-invocation prefix, so prose mentions
   # ("# No explicit ... call needed") in the hook docstring and encoding-protocol-digest
   # are excluded — only actual invocations in pseudocode flag.
   Bash (no-redundant-last-updated): test -z "$(grep -rhnE 'bash core/scripts/tree-update\.sh\s+--set\s+\S+\s+last_updated' .claude/skills/ core/config/ 2>/dev/null)" && echo "PASS: no redundant explicit tree-update.sh --set last_updated invocations in .claude/skills/ or core/config/ pseudocode (T21 hook covers it)" || { echo "FAIL: redundant tree-update.sh --set last_updated invocation(s) found — T21 PostToolUse hook already covers this, the explicit calls are dead pseudocode"; grep -rnE 'bash core/scripts/tree-update\.sh\s+--set\s+\S+\s+last_updated' .claude/skills/ core/config/; }
   Bash (no-redundant-last-update-trigger): test -z "$(grep -rhnE 'bash core/scripts/tree-update\.sh\s+--set\s+\S+\s+last_update_trigger' .claude/skills/ core/config/ 2>/dev/null)" && echo "PASS: no redundant tree-update.sh --set last_update_trigger invocations (field lives only in .md front matter; _tree.yaml writes are dead per guard-531)" || { echo "FAIL: tree-update.sh --set last_update_trigger invocation(s) found — last_update_trigger lives in the .md front matter; _tree.yaml has no such field (guard-531)"; grep -rnE 'bash core/scripts/tree-update\.sh\s+--set\s+\S+\s+last_update_trigger' .claude/skills/ core/config/; }

   # S48.12: Tree-sync inline-form trigger must NOT abort the last_updated bump (2026-06-04 _tree.yaml drift fix)
   # tree-front-matter-sync.py (T21) used to sys.exit(0) ("REFUSED: trigger has inline
   # form") whenever last_update_trigger was an inline dict `{type: ...}` AND MIND_SID
   # was set. The real hook ALWAYS sets MIND_SID (tree-sync-check.sh derives it from the
   # PostToolUse payload's session_id), and most skill instructions emit the inline form
   # (encode-session Lane 1.6, research-topic, respond, reflect-on-outcome,
   # aspirations-consolidate/execute) — so the abort silently left _tree.yaml's last_updated
   # stale for every inline-trigger node (canonical: windows-maxpath-pathresolution lagged 5
   # months). Fix: an inline-form trigger skips ONLY the secondary session/source auto-fill
   # and falls through to the primary last_updated bump. The string-form refusal at line ~302
   # is deliberate (legacy → Layer-B /tree-edit migration) and is preserved. Behavioral smoke
   # below: run the sync on an inline-trigger fixture with a stale sentinel date; the date MUST
   # change (proves the bump fired, not an abort). Uses a far-past sentinel (2020-01-01) so it
   # can never collide with today.
   Bash (tree-sync-inline-bumps): D="${TEMP:-/tmp}/vlsync-$$"; mkdir -p "$D"; printf '%s\n' '---' 'created: "2020-01-01"' 'last_updated: "2020-01-01"' 'last_update_trigger: {type: "smoke"}' '---' '' 'body' > "$D/n.md"; MIND_SID=vl-smoke py -3 core/scripts/tree-front-matter-sync.py --file "$D/n.md" --virtual-path "world/knowledge/tree/__vlsmoke__/n.md" >/dev/null 2>&1; V=$(grep '^last_updated:' "$D/n.md"); rm -rf "$D"; case "$V" in *2020-01-01*) echo "FAIL: inline-form trigger left last_updated stale ($V) — tree-front-matter-sync.py re-aborts on inline form (2026-06-04 regression: inline {type:...} must skip nested session/source yet still bump last_updated)";; *) echo "PASS: inline-form trigger bumps last_updated ($V), no abort";; esac

   # S48.11: Runtime-daemon cache-invalidate-inside-lock invariant (g-115-674 + g-115-677)
   # g-115-674 hardened mind_api/src/endpoints/aspirations_write.py to keep every
   # `_jsonl_cache().invalidate(live_path)` INSIDE the `with file_locks.locked(live_path):`
   # critical section. Out-of-lock invalidate re-opens the eventual-consistency window
   # where a reader can stat the new mtime, miss the size-or-mtime check on a same-tick
   # collision, and serve stale data (jsonl-read-modify-write-race). g-115-677 added
   # this verify-learning check so a future refactor cannot silently regress.
   #
   # S48.11a: AST-based check — every `.invalidate(...)` call in non-underscore-prefixed
   # mind_api/src/endpoints/*.py modules lives lexically inside a `with file_locks.locked():`
   # block. Static analysis is sufficient — the failure mode is a non-deterministic
   # same-tick mtime race that cannot be reliably triggered at test time.
   Bash (cache-invalidate-inside-lock): py -3 core/scripts/tests/test_cache_invalidate_inside_lock.py
   # S48.11b: Path-key invariance — read (aspirations.py) and write (aspirations_write.py)
   # `_resolve_paths` functions MUST construct live_path identically (`base / "aspirations.jsonl"`)
   # AND retain the mutual INVARIANT docstring comment that documents the contract. jsonl_cache
   # keys on the Path object — divergent construction silently desyncs invalidate (write) from
   # get (read). The INVARIANT comment is the trip-wire: if removed, this check fails and
   # surfaces the refactor before the silent stale-read regression lands.
   Bash (resolve-paths-invariant-read): grep -q 'INVARIANT.*aspirations_write\.py' mind_api/src/endpoints/aspirations.py && grep -q 'base / "aspirations\.jsonl"' mind_api/src/endpoints/aspirations.py && echo "PASS: aspirations.py:_resolve_paths INVARIANT comment + base/aspirations.jsonl construct intact" || echo "FAIL: aspirations.py:_resolve_paths missing INVARIANT comment or live_path construct — read/write path-key invariance documentation regressed (g-115-674)"
   Bash (resolve-paths-invariant-write): grep -q 'INVARIANT.*aspirations\.py' mind_api/src/endpoints/aspirations_write.py && grep -q 'base / "aspirations\.jsonl"' mind_api/src/endpoints/aspirations_write.py && echo "PASS: aspirations_write.py:_resolve_paths INVARIANT comment + base/aspirations.jsonl construct intact" || echo "FAIL: aspirations_write.py:_resolve_paths missing INVARIANT comment or live_path construct — read/write path-key invariance documentation regressed (g-115-674)"

   # === Section CGN: Capability-Gate Noise-Phrase + Bridge SSOT (g-115-460 + g-115-676)
   # Hardening checks for the capability-gate.py noise-phrase mechanism that
   # disqualifies multi-token keywords appearing inside larger non-actionable
   # phrases (e.g., "delete an existing system" should not match the deploy
   # keyword "deploy an existing system" prefix). g-115-460 landed the
   # noise-phrase loader + longest-first-sort invariant + CRITICAL ORDER
   # constraint (_resolve_paths must run BEFORE _extract_keywords so the
   # WORLD_DIR-scoped YAML lookup happens with paths resolved). g-115-676
   # encoded these as verify-learning checks so a future refactor cannot
   # silently regress the disqualification path without flagging.
   #
   # CGN-a: noise-phrase loader function present
   Bash (cgn-loader): grep -q "^def _load_noise_phrases" core/scripts/capability-gate.py && echo "PASS: _load_noise_phrases defined" || echo "FAIL: capability-gate.py missing _load_noise_phrases — noise-phrase disqualification will not load YAML; g-115-460 regressed"
   # CGN-b: longest-first sort invariant inside _load_noise_phrases
   # Must be longest-first so multi-token phrase "deploy an existing system" is
   # tested BEFORE shorter prefix "deploy an existing"; without it short phrases
   # shadow longer ones and false-positive disqualifications stop firing.
   Bash (cgn-sort-invariant): grep -q "out.sort(key=len, reverse=True)" core/scripts/capability-gate.py && echo "PASS: noise-phrase loader sorts longest-first (multi-token phrases dominate prefix shadowing)" || echo "FAIL: capability-gate.py noise-phrase sort invariant lost — multi-token phrases will be shadowed by their own prefixes; g-115-460 regressed"
   # CGN-c: CRITICAL ORDER comment guarding _resolve_paths / _extract_keywords sequencing
   # The comment is the trip-wire — if a future refactor flips the call order,
   # the comment goes stale and the test fails, surfacing the violation BEFORE
   # the silent regression lands in production.
   Bash (cgn-critical-order): grep -q "CRITICAL ORDER: _resolve_paths() must run BEFORE _extract_keywords" core/scripts/capability-gate.py && echo "PASS: CRITICAL ORDER trip-wire comment intact" || echo "FAIL: capability-gate.py CRITICAL ORDER comment removed — _resolve_paths must run before _extract_keywords or WORLD_DIR-scoped noise-phrase YAML lookup happens with unresolved paths; g-115-460 regressed"
   # CGN-d: noise-phrase YAML exists at world conventions
   # The YAML is the SSOT for the noise-phrase set; loader returns [] silently
   # if the file is missing, so absence is undetectable without this check.
   Bash (cgn-yaml-exists): source core/scripts/_paths.sh && test -f "$WORLD_DIR/conventions/capability-gate-noise-phrases.yaml" && echo "PASS: noise-phrases YAML present at world/conventions/" || echo "FAIL: capability-gate-noise-phrases.yaml missing from world/conventions/ — loader returns [] silently and disqualification is dead"
   # CGN-e: roblox-bridge usage convention exists (Bridge SSOT companion)
   # roblox-bridge-usage.md is the SSOT for what counts as a bridge action
   # vs. a non-bridge action; deletion biases capability-gate misclassification.
   Bash (cgn-bridge-ssot): source core/scripts/_paths.sh && test -f "$WORLD_DIR/conventions/roblox-bridge-usage.md" && echo "PASS: roblox-bridge-usage convention present" || echo "FAIL: roblox-bridge-usage.md missing from world/conventions/ — Bridge SSOT removed"
   # CGN-f: /tmp- gitignore leak rule
   # Prevents temp scratch files (e.g., `tmp-` scratch dirs) created by
   # forge-skill / ad-hoc scripts from being staged into commits. Without this
   # rule the iteration-commit.sh broad-add ceremony stages them silently.
   Bash (cgn-tmp-gitignore): grep -qE "^/tmp-" .gitignore && echo "PASS: /tmp- gitignore leak rule present" || echo "FAIL: .gitignore missing /tmp- rule — temp scratch files can leak into commits"
   # CGN-g: imperative-verb whitelist extension intact
   # The 2026-05-12 g-115-460 follow-up extended _IMPERATIVE_VERBS with
   # mutate-class verbs (delete/remove/save/edit/etc.) so action_verb extraction
   # no longer falls back to the matched-keyword for goals whose failure_reason
   # uses these verbs. Without the extension, "Bridge delete completed" produces
   # "Unblock: npc" titles (matched-keyword fallback) instead of "Unblock: delete".
   Bash (cgn-imperative-verbs): py -3 -c "import re; src = open('core/scripts/capability-gate.py', encoding='utf-8').read(); m = re.search(r'^_IMPERATIVE_VERBS\s*=\s*\{([^}]*)\}', src, re.M | re.S); assert m, 'FAIL: _IMPERATIVE_VERBS literal not found'; body = m.group(1); needed = ['delete', 'remove', 'save', 'edit']; missing = [v for v in needed if f'\"{v}\"' not in body]; print('PASS: _IMPERATIVE_VERBS mutate extension intact (delete/remove/save/edit all present)' if not missing else f'FAIL: _IMPERATIVE_VERBS missing mutate-class verbs: {missing} — action_verb extraction will fall back to matched-keyword for goals using these verbs (g-115-460 follow-up regressed)')"

   # === Section S49: Post-/verify-learning structural gate suite ==================
   # Three gates built after the 2026-04-19 /verify-learning run exposed two
   # architectural gaps: (a) session_signals counters named in pseudocode but
   # missing init/increment/reset sites (F1/F2); (b) WM cadence slots with
   # duplicate or missing writers (F3). Rather than adding 50 static grep
   # checks that rot on every refactor, each gate reads the actual invariant
   # from the codebase and compares against the spec. `would_block: true` in
   # the gate's JSON output is the PASS/FAIL signal — exit code 1 iff any
   # unresolved finding.

   # S49.1: Skill-structure gate — Return Protocol + minimum_mode +
   # Bash-script-exists + Skill-invocation validity across every SKILL.md
   Check: `core/scripts/skill-structure-gate.py` exists; `.sh` wrapper exists
   Bash: bash core/scripts/skill-structure-gate.sh --all --json 2>/dev/null | py -3 -c "import sys,json; r=json.load(sys.stdin); assert not r.get('would_block'), f'FAIL: skill-structure-gate violations: {r.get(\"violations\",[])[:3]}'; print('PASS: skill-structure-gate clean')"

   # S49.2: Signal-lifecycle gate — init/increment/reset completeness for
   # every session_signals sub-key AND cadence-timestamp single-writer rule
   # (guard-155) AND WM phantom-read detection
   Check: `core/scripts/signal-lifecycle-gate.py` exists; `.sh` wrapper exists
   Check: `core/scripts/signal-lifecycle-gate.py` has MONOTONIC_WHITELIST with (exempt_sites, rationale) tuple schema and `_validate_whitelist()` load-time guard
   # Regression guards against the 2026-04-19 tautological-detection bug:
   # the gate's `_WM_PY_WRITE_RE` must accept `)` in its separator class so
   # `str(CORE_ROOT / "scripts" / "wm.py"), "append", "<slot>"` matches as a
   # writer. Without this, callers using the path-concat idiom are invisible
   # to the gate and the slot silently regresses to phantom-read.
   Bash: grep -A1 '_WM_PY_WRITE_RE = re.compile' core/scripts/signal-lifecycle-gate.py | grep -q ')' && echo 'PASS: _WM_PY_WRITE_RE separator class includes ) for path-concat idiom' || { echo 'FAIL: _WM_PY_WRITE_RE lost ) from separator class — path-concat writers will be invisible'; false; }
   # P5 regression guards (2026-04-20, rb-401, guard-353): BOTH regex bugs.
   # (1) Quote-adjacency: the verb MUST be wrapped in ['"] — a whitespace-only
   #     separator between wm.py and the verb admits prose/f-strings like
   #     `f"wm.py read returned non-JSON"` as a phantom reader of slot "returned".
   #     Fingerprint: the sequence `]*?[` — lazy-close-class, then open-quote-class —
   #     is unique to the quote-adjacency form. Whitespace-only variants would
   #     not produce this exact substring.
   # (2) Bracket-list coverage: `[` MUST be in the separator class so
   #     `_run_py("wm.py", ["read", "slot"])` nested-list form is detected.
   #     Missing `[` silently under-matches modern callers (create-blocker.py,
   #     backfill-closes-knowledge-debt.py).
   Bash: grep -E '_WM_PY_(READ|WRITE)_RE' core/scripts/signal-lifecycle-gate.py -A1 | grep -q '\[' && echo "PASS: _WM_PY regex separator class contains [ (bracket-list callers visible)" || { echo "FAIL: _WM_PY regex lost [ from separator class — bracket-list callers invisible (rb-401)"; false; }
   Bash: grep -E '_WM_PY_(READ|WRITE)_RE' core/scripts/signal-lifecycle-gate.py -A1 | grep -qE "\]\*\?\[" && echo "PASS: _WM_PY regex requires quote adjacency before the verb (prose-match prevention)" || { echo "FAIL: _WM_PY regex allows whitespace-only separator before verb — prose/f-strings will produce phantom reads/writes (rb-401, guard-353)"; false; }
   # Layer-1 prose-filter regression guard (2026-04-19 fix, rb-349 / guard-319).
   # Prose text like "# wm-read.sh mutates accessed_at" in .md/.sh files must be
   # skipped BEFORE identifier regexes run — lexically valid identifiers cannot
   # be distinguished from English prose by pattern alone. See
   # world/knowledge/tree/system/scanner-design-patterns/prose-filter-pattern.md.
   # Architecture: _is_prose_line() helper is the single source of truth for
   # "what counts as prose"; _gather_wm_ops AND _gather_strict_set_writers both
   # call it (same corpus, same shell-command regex class — both would false-
   # positive identically without it). _find_lifecycle_sites intentionally NOT
   # wired through the helper — different regex class (init/reset operators),
   # no realized false positive in corpus; speculative hardening would violate
   # implementation-discipline.md YAGNI.
   Bash: grep -q 'def _is_prose_line' core/scripts/signal-lifecycle-gate.py && echo 'PASS: _is_prose_line helper present' || { echo 'FAIL: _is_prose_line helper removed — Layer-1 filter has no single source of truth (see rb-349, guard-319)'; false; }
   Bash: grep -A 15 'def _gather_wm_ops' core/scripts/signal-lifecycle-gate.py | grep -q 'if _is_prose_line(line, path):' && echo 'PASS: _gather_wm_ops calls _is_prose_line (phantom-read Layer-1 filter wired)' || { echo 'FAIL: _gather_wm_ops no longer calls _is_prose_line — phantom-reader false positives on SKILL.md prose will return (rb-349)'; false; }
   Bash: grep -A 10 'def _gather_strict_set_writers' core/scripts/signal-lifecycle-gate.py | grep -q 'if _is_prose_line(line, path):' && echo 'PASS: _gather_strict_set_writers calls _is_prose_line (cadence-check Layer-1 filter wired)' || { echo 'FAIL: _gather_strict_set_writers no longer calls _is_prose_line — cadence-single-writer false positives on prose will return (rb-349 same-file consistency)'; false; }
   # conclusion-record.py INVARIANT: argv list for `wm.py append conclusions`
   # must stay on ONE physical line. The gate scans single lines; a multi-
   # line split hides the writer. Dual-anchor: live code at line ~140 plus
   # the INVARIANT comment block above it. ≥2 matches means both anchors
   # are intact; a drop to 1 is a warning; 0 means detection is broken.
   Bash: grep -c '"wm\.py".*"append".*"conclusions"' core/scripts/conclusion-record.py | py -3 -c "import sys; n=int(sys.stdin.read().strip()); assert n>=1, f'FAIL: conclusion-record.py lost the one-line argv invariant (found {n} matching lines, need ≥1)'; print(f'PASS: conclusion-record.py argv invariant intact ({n} anchor(s))')"
   # Zero tolerance. The `conclusions` phantom-read was resolved 2026-04-19
   # (conclusion-record.py wired into CREATE_BLOCKER Step 2.56 as the
   # single writer). Any violation now indicates a real regression — a
   # reset site disappeared, a cadence slot lost its writer, or a new
   # phantom read was introduced.
   Bash: bash core/scripts/signal-lifecycle-gate.sh 2>/dev/null | py -3 -c "import sys,json; r=json.load(sys.stdin); v=r.get('violations',[]); assert not v, f'FAIL: signal-lifecycle violations: {v}'; print(f'PASS: signal-lifecycle-gate clean ({r[\"wm_slot_writers\"]} writers / {r[\"wm_slot_readers\"]} readers)')"

   # VLS: Verify-Learning Staleness Scanner (added 2026-04-25 — out-of-cycle
   # Maintain that closed /verify-learning Action 6, see g-001-203, rb-521, rb-522)
   # Three-channel detection: scanner+skill (on-demand via /verify-learning-
   # staleness), felt-sense Phase 5b (75-goal cadence), recurring goal g-115-219
   # (weekly). The scanner reads THIS file and reports stale Check:/Bash:
   # assertions whose targets have moved/been retired/been extracted to scripts.
   Check: `core/scripts/verify-learning-staleness.py` exists with `_NEGATIVE_PHRASES` tuple AND `is_negative_assertion(body)` early-return at top of every check function (without these, "test ! -f X" / "MUST NOT exist" idioms produce false positives in all four lanes)
   Bash: grep -q '_NEGATIVE_PHRASES = (' core/scripts/verify-learning-staleness.py && echo 'PASS: _NEGATIVE_PHRASES tuple guard present' || { echo 'FAIL: scanner lost _NEGATIVE_PHRASES — false positives on negative-assertion bodies'; false; }
   Bash: grep -c 'is_negative_assertion(body)' core/scripts/verify-learning-staleness.py | py -3 -c "import sys; n=int(sys.stdin.read().strip()); assert n >= 4, f'FAIL: is_negative_assertion called only {n} times — every check function (check_paths, check_phases, check_grep_targets, check_grep_phase) must early-return on negative bodies'; print(f'PASS: is_negative_assertion guard wired in {n} call sites')"
   # Scanner `_PATH_RE` deliberately excludes `meta/` and `world/` (external
   # paths per local-paths.conf — checking them at REPO_ROOT would always fail).
   # Adding them re-introduces ~100 false positives.
   Bash: py -3 -c "import re,pathlib; src=pathlib.Path('core/scripts/verify-learning-staleness.py').read_text(encoding='utf-8'); assert 'meta/' not in re.search(r'_PATH_RE = re\\.compile\\(([\\s\\S]+?)\\)', src).group(1), 'FAIL: _PATH_RE includes meta/ — external-path false positives will return'; assert 'world/' not in re.search(r'_PATH_RE = re\\.compile\\(([\\s\\S]+?)\\)', src).group(1), 'FAIL: _PATH_RE includes world/ — external-path false positives will return'; print('PASS: _PATH_RE correctly excludes meta/ and world/')"
   # Scanner exit-code contract: 0=clean, 1=stale found. Anything ≥2 is a
   # scanner bug, not a content drift signal — the recurring-goal check
   # `[ $? -le 1 ]` would fail loud on exit ≥2.
   Bash: py -3 core/scripts/verify-learning-staleness.py 2>/dev/null | py -3 -c "import sys,json; r=json.loads(sys.stdin.read()); assert isinstance(r.get('assertions_scanned'),int) and r['assertions_scanned'] > 1000, f'FAIL: scanner scanned only {r.get(\"assertions_scanned\")} assertions (expected >1000)'; print(f'PASS: scanner found {r[\"assertions_scanned\"]} assertions, {r[\"stale_count\"]} stale')"
   Bash: py -3 core/scripts/verify-learning-staleness.py >/dev/null 2>&1; rc=$?; [ "$rc" -le 1 ] && echo "PASS: scanner exit $rc (0 clean / 1 stale, both valid)" || { echo "FAIL: scanner exit $rc — must be 0 or 1, never higher"; false; }
   Check: `.claude/skills/verify-learning-staleness/SKILL.md` exists with `user-invocable: false` and `minimum_mode: assistant` frontmatter
   Bash: grep -q '^user-invocable: false' .claude/skills/verify-learning-staleness/SKILL.md && grep -q '^minimum_mode: assistant' .claude/skills/verify-learning-staleness/SKILL.md && echo "PASS: verify-learning-staleness skill frontmatter intact" || { echo "FAIL: verify-learning-staleness/SKILL.md frontmatter drift"; false; }
   Check: `felt-sense-checkin/SKILL.md` Phase 5b invokes `verify-learning-staleness.py`
   Bash: grep -q '### Phase 5b: Stale Checks' .claude/skills/felt-sense-checkin/SKILL.md && grep -q 'verify-learning-staleness.py' .claude/skills/felt-sense-checkin/SKILL.md && echo "PASS: felt-sense-checkin Phase 5b wired to scanner" || { echo "FAIL: Phase 5b split or scanner reference removed from felt-sense-checkin"; false; }
   # Recurring goal g-115-219 schema + verification.checks regression guards
   # (rb-521 + rb-522 + guard-440 — pipe-exit gotcha + cross-tier path leak)
   Bash: bash core/scripts/world-cat.sh aspirations.jsonl | py -3 -c "import sys, json; recs=[json.loads(l) for l in sys.stdin if l.strip()]; asp=next((r for r in recs if r['id']=='asp-115'), None); assert asp, 'FAIL: asp-115 missing from world'; g=next((g for g in asp['goals'] if g['id']=='g-115-219'), None); assert g, 'FAIL: g-115-219 missing from asp-115'; assert g.get('recurring') is True and g.get('interval_hours')==168, f'FAIL: g-115-219 schema drift (recurring={g.get(\"recurring\")}, interval_hours={g.get(\"interval_hours\")})'; checks=' '.join(g['verification']['checks']); assert 'tee' not in checks, 'FAIL: g-115-219 verification.checks contains tee — pipe-exit-code regression (rb-521, guard-440)'; assert 'alpha/' not in checks and 'bravo/' not in checks, 'FAIL: g-115-219 verification.checks contains per-agent path — cross-tier leak regression (rb-522, guard-440)'; print('PASS: g-115-219 schema clean, verification.checks free of tee + per-agent paths')"
   # Phantom-reads CATEGORY 2 (user-only kill switches) — suppress_user_push
   # must remain whitelisted; deleting the entry will reintroduce a phantom-read
   # violation for a slot the user controls manually.
   Bash: grep -q '"suppress_user_push"' core/scripts/signal-lifecycle-gate.py && grep -q 'CATEGORY 2 — User-only kill switches' core/scripts/signal-lifecycle-gate.py && echo "PASS: suppress_user_push CATEGORY 2 whitelisted in phantom-reads" || { echo "FAIL: suppress_user_push lost CATEGORY 2 whitelist — phantom-reads will flag the user-only kill switch"; false; }
   # MONOTONIC_WHITELIST entry for goals_since_last_tree_update — writer is
   # direct file-I/O (tree-encoding-drift-gate.py atomic read-modify-write),
   # undetectable by the regex-based lifecycle scanner.
   Bash: grep -q '"goals_since_last_tree_update"' core/scripts/signal-lifecycle-gate.py && echo "PASS: goals_since_last_tree_update MONOTONIC_WHITELIST entry present" || { echo "FAIL: goals_since_last_tree_update lost MONOTONIC_WHITELIST exemption — increment-site detection will fail"; false; }
   # aspirations-state-update Step 8.77: suppress_user_push read with
   # kill-switch documentation comment block (user invokes manually, no
   # programmatic writer by design)
   Bash: grep -q 'wm-read.sh suppress_user_push' .claude/skills/aspirations-state-update/SKILL.md && grep -q 'user-only mute' .claude/skills/aspirations-state-update/SKILL.md && echo "PASS: Step 8.77 suppress_user_push read + kill-switch documentation intact" || { echo "FAIL: aspirations-state-update Step 8.77 lost suppress_user_push read or kill-switch documentation"; false; }

   # S49.3: Scripts-referenced gate — every non-helper .sh/.py in core/scripts/
   # must be referenced by a SKILL.md, config, rule, settings, or other script
   # (or listed in ALWAYS_EXEMPT with an inline invocation-channel rationale)
   Check: `core/scripts/scripts-referenced-gate.py` exists; `.sh` wrapper exists
   Check: `core/scripts/scripts-referenced-gate.py` ALWAYS_EXEMPT lists every non-invoked script with an inline comment naming the invocation channel (operator, test-harness, pending-user-apply, gate-called-by-verifier). Adding an entry without the comment is a review blocker — it silently turns a real orphan into a zombie.
   # Zero tolerance for new orphans. After the 2026-04-19 close-out, the
   # three previously-tracked candidates were each resolved: two moved into
   # ALWAYS_EXEMPT (path-resolution-hook pending g-115-41 apply, tree-
   # reconcile-capabilities as operator tool), one fixed at the call site
   # (S48.4 now invokes tree-maintenance-read.sh not .py). Any orphan
   # surfacing now means a recent change dropped a reference.
   Bash: bash core/scripts/scripts-referenced-gate.sh 2>/dev/null | py -3 -c "import sys,json; r=json.load(sys.stdin); found=sorted(e['basename'] for e in r.get('script_orphans',[])); assert not found, f'FAIL: orphan scripts detected: {found}'; print('PASS: scripts-referenced-gate clean (0 orphans)')"

   # S49.3b: Goal-script-orphan gate (inverse direction of S49.3) — no
   # pending/in-progress goal's description or skill field may name a
   # core/scripts/<name>.{sh,py} that is absent on disk. Companion gate filed
   # via g-115-905 after a deleted script (override-ledger-consume) left a
   # goal reference dangling undetected for a week. The gate was built
   # 2026-05-18 but never verify-learning-wired (the loose end g-115-905 left
   # open) AND crashed when run unbound (AGENT_DIR None) — both fixed
   # 2026-06-03; wiring it here is one of the 7 orphan-script resolutions.
   Check: `core/scripts/goal-script-orphan-gate.py` exists; `.sh` wrapper exists
   Bash: bash core/scripts/goal-script-orphan-gate.sh 2>/dev/null | py -3 -c "import sys,json; r=json.load(sys.stdin); found=sorted({o['script_name'] for o in r.get('orphan_references',[])}); assert not found, f'FAIL: goals reference missing scripts: {found}'; print('PASS: goal-script-orphan-gate clean (0 orphan references)')"

   # S49.4: Fresh-eyes cadence-check contract — --print-current must emit a
   # bare non-negative integer and exit 0, unconditionally (bypasses config
   # gates so the skill's Phase 8 "record the tick" step is robust). Added
   # 2026-04-19 after the shell-var-leak bug (rb-361): the original Phase 8
   # parsed `current=N` out of --verbose with grep -oP, which was brittle
   # AND spread across three separate Bash tool calls so the variable never
   # survived. --print-current is the SSOT integer-emitter; if this check
   # breaks, the skill silently stops recording the cadence tick and the
   # fresh-eyes review stops firing after its first run.
   Check: `core/scripts/fresh-eyes-cadence-check.py` has a `--print-current` argparse argument
   Bash: bash core/scripts/fresh-eyes-cadence-check.sh --print-current 2>&1 | py -3 -c "import sys; raw=sys.stdin.read().strip(); n=int(raw); assert n>=0, f'FAIL: --print-current emitted negative integer {n}'; print(f'PASS: --print-current emitted {n}')"

   # S49.5: Broken journal-add.sh argv pattern gate — any SKILL.md that
   # invokes `journal-add.sh --type X --summary Y` is silently broken,
   # because journal-add.sh requires stdin JSON with a journal_file key
   # and argparse errors on those flags. Swap to board-post.sh (for
   # cross-agent status events) or to correct stdin-JSON form (for true
   # session journaling). Added 2026-04-19 after finding 4 matches; the
   # Investigate goal g-240-21 drives the fix. This check FAILS while
   # any match remains, by design — it is the structural tripwire that
   # prevents regression AND keeps the Investigate goal visible.
   # --exclude-dir=verify-learning is mandatory — this check's own comments/regex
   # contain the pattern literally, so without the exclusion it would always
   # self-match and never reach PASS.
   Bash: matches=$(grep -rn --exclude-dir=verify-learning 'journal-add\.sh --type' .claude/skills/ 2>/dev/null || true); if [ -z "$matches" ]; then echo "PASS: no broken journal-add.sh --type argv patterns in skills (0 matches)"; else echo "FAIL: broken journal-add.sh --type argv pattern found (journal-add.sh requires stdin JSON, not --type/--summary flags — see g-240-21):"; echo "$matches"; false; fi

   # S49.6: WM consumer wiring integrity for session-49 Core Layer — the
   # Program-alignment probe consumer (C1) and consolidation-gate reader (C4)
   # must use correct JSON-mode and no silent fallback, respectively.
   # The positional-wm-set check is already covered by S49.2's full gate run
   # (wm_set_positional_value is in ALL_CHECKS). These two checks enforce
   # the remaining semantic invariants.
   # S49.6.1: boost_generative_sparks consumer MUST use --json. Without it,
   # wm-read.sh returns Python repr "True" (capital) and string comparison
   # against lowercase "true" silently no-ops — the Program-alignment self-
   # correction path dies invisibly. Bug found and fixed 2026-04-19 in
   # session 49 fresh-eyes pass.
   Bash: grep -n 'wm-read\.sh boost_generative_sparks' .claude/skills/aspirations-spark/SKILL.md | grep -q '\-\-json' && echo "PASS: boost_generative_sparks consumer uses --json (lowercase true/false comparison is valid)" || { echo "FAIL: boost_generative_sparks consumer missing --json — wm-read.sh returns Python 'True', string-compare against 'true' will silently fail (C1 dead-signal regression)"; false; }
   # S49.6.2: consolidation_health consumer MUST NOT have silent-failure
   # fallback. `2>/dev/null || echo "null"` produces phantom null that
   # bypasses the consolidation gate (C4) and lets create-aspiration
   # proceed when it should refuse. Bug found and fixed 2026-04-19.
   Bash: matches=$(grep -n 'wm-read\.sh consolidation_health' .claude/skills/create-aspiration/SKILL.md | grep -E '2>/dev/null|\|\| echo' || true); if [ -z "$matches" ]; then echo "PASS: consolidation_health consumer has no silent-failure fallback (C4 gate reads source-of-truth)"; else echo "FAIL: consolidation_health consumer uses silent fallback — phantom null bypasses C4 consolidation gate:"; echo "$matches"; false; fi

   # S49.7: Prose-only verification drift detector — if a goal's description
   # contains "Verification outcomes:" or "Verification checks:" prose lines,
   # the structured verification.checks field MUST be non-empty. Phase 5
   # aspirations-verify reads goal.verification.checks at line 79; prose-in-
   # description silently bypasses the runner and downgrades the goal to
   # unverifiable. Pattern surfaced 2026-04-19 session 49: g-244-01..03,
   # g-246-01..02 all shipped with prose-only verification and were
   # retrofitted via aspirations-update-goal.sh. This check catches future
   # regressions of the same class.
   Bash: source core/scripts/_paths.sh && WORLD_DIR="$WORLD_DIR" py -3 -c "
import json, os
viol = []
with open(os.environ['WORLD_DIR'] + '/aspirations.jsonl') as f:
    for line in f:
        asp = json.loads(line)
        if asp.get('archived'): continue
        for g in asp.get('goals', []):
            if g.get('status') not in ('active','pending','in-progress'): continue
            desc = g.get('description','') or ''
            has_prose = 'Verification outcomes:' in desc or 'Verification checks:' in desc
            vchecks = (g.get('verification') or {}).get('checks') or []
            if has_prose and not vchecks:
                viol.append(g['id'])
if viol:
    print(f'FAIL: prose-only verification drift — goals with Verification-prose in description but empty verification.checks: {viol}')
    exit(1)
print('PASS: all prose-verification goals have structured verification.checks')
"

   # === Section S55: Scripted-extraction contract gates (2026-04-21, rb-411/412, guard-359) ======
   # Three gates built after session 100 toggle-removal exposed 3 latent bugs that
   # had accumulated under the scripted_extractions.*.enabled: false default-off.
   # Single source of truth: rb-411 (toggle scaffolding accumulates bugs),
   # rb-412 (SKILL.md pseudocode flag names are a contract with script output),
   # guard-359 (verify flag names match before editing either side).
   #
   # These tests fail LOUDLY on regression rather than protecting the broken path.
   # If a future session removes .as_posix() or reintroduces the toggle wrapper,
   # /verify-learning must catch it before the scripted path silently rots again.

   # S55.1: Flag-name contract — aspirations-precheck/SKILL.md must reference each
   # {subcommand}:{flag} pair that precheck-eval.py actually emits via the run-all
   # f-string at cmd_run_all. Drift here was the original Bug 3: SKILL.md listed
   # bare flag names, LLM branched on the wrong strings, scripted precheck gate
   # no-opped every iteration. Check grows naturally: new SUBCMDS pairs shipped
   # without a corresponding SKILL.md entry will fail this test.
   Check: all 8 known flag pairs emitted by precheck-eval.py appear in aspirations-precheck/SKILL.md AND the f-string emitter in precheck-eval.py still uses {name}:{f}
   Bash: py -3 -c "
import sys
expected = [
    'zombies:needs_complete_review',
    'pipeline-depth:thin_pipeline',
    'hypothesis-health:stalled_pipeline',
    'accuracy:accuracy_low',
    'consolidation:shallow_portfolio',
    'consolidation:stalled_aspirations',
    'cycles:cycles_detected',
    'user-goals:reclassifiable_user_goals',
]
skill = open('.claude/skills/aspirations-precheck/SKILL.md', encoding='utf-8').read()
missing = [p for p in expected if p not in skill]
if missing:
    print(f'FAIL: aspirations-precheck/SKILL.md is missing {len(missing)} prefixed flag reference(s): {missing}. These are the strings precheck-eval.py emits (cmd_run_all uses f\"{{name}}:{{f}}\"). LLM branches on them; drift silently no-ops the scripted precheck gate (rb-412).')
    sys.exit(1)
script = open('core/scripts/precheck-eval.py', encoding='utf-8').read()
if 'f\"{name}:{f}\"' not in script:
    print('FAIL: precheck-eval.py cmd_run_all no longer uses f\"{name}:{f}\" separator — expected[] in this check has gone stale. Update both sides OR restore the f-string (rb-412, guard-359).')
    sys.exit(1)
print('PASS: flag-name contract intact — 8/8 prefixed pairs present, f-string emitter unchanged')
"

   # S55.1b: Sibling flag-name contract — aspirations-state-update/SKILL.md references
   # prefixed flags from state-update-audit.py's cmd_run_all. Same rb-412 pattern as
   # S55.1. g-240-55 audit (2026-04-21) caught this drift in the sibling script:
   # run-all was extending bare flag names, SKILL.md line 593-596 expected prefixed
   # ones. Fix matched precheck-eval convention: rename bare `backpressure_check_failed`
   # → `check_failed`, `backpressure_bad_output` → `bad_output`, prefix via
   # f"{name}:{f}" in cmd_run_all. New SUBCMDS flags must keep the pattern.
   Check: documented flag pairs emitted by state-update-audit.py appear in aspirations-state-update/SKILL.md AND cmd_run_all still uses {name}:{f}
   Bash: py -3 -c "
import sys
expected = [
    'velocity:impk_snapshot_failed',
    'backpressure:rollbacks_applied',
    'backpressure:check_failed',
]
skill = open('.claude/skills/aspirations-state-update/SKILL.md', encoding='utf-8').read()
missing = [p for p in expected if p not in skill]
if missing:
    print(f'FAIL: aspirations-state-update/SKILL.md is missing {len(missing)} prefixed flag reference(s): {missing}. These are the strings state-update-audit.py emits after cmd_run_all prefixes via f\"{{name}}:{{f}}\". LLM branches on them; drift silently no-ops the scripted state-update gate (rb-412, g-240-55).')
    sys.exit(1)
script = open('core/scripts/state-update-audit.py', encoding='utf-8').read()
if 'f\"{name}:{f}\"' not in script:
    print('FAIL: state-update-audit.py cmd_run_all no longer uses f\"{name}:{f}\" separator — the bare-flag drift rb-412 flagged has returned. Update both sides OR restore the f-string (rb-412, guard-359, g-240-55).')
    sys.exit(1)
print('PASS: state-update flag-name contract intact — 3/3 prefixed pairs present, f-string emitter unchanged')
"

   # S55.2: No toggle-wrapper resurrection — scripted-extraction-enabled.sh was
   # deleted in session 100 because the toggle hid three bugs (rb-411). If a future
   # session re-creates it or re-adds the scripted_extractions: config block,
   # we are back to default-off bug incubation.
   Check: core/scripts/scripted-extraction-enabled.sh does NOT exist
   Bash: test ! -f core/scripts/scripted-extraction-enabled.sh && echo 'PASS: scripted-extraction-enabled.sh absent (toggle wrapper not resurrected)' || { echo 'FAIL: core/scripts/scripted-extraction-enabled.sh has reappeared — toggle wrappers are bug incubators (rb-411). Either delete it or document in a new rb/guard why the toggle is now safe.'; false; }
   Check: core/config/aspirations.yaml does NOT contain a scripted_extractions: top-level block
   Bash: grep -qE '^scripted_extractions:' core/config/aspirations.yaml && { echo 'FAIL: aspirations.yaml has a scripted_extractions: block — this was removed in session 100 as dead configuration (rb-411). Re-adding it without a corresponding wrapper resurrection means the config keys are orphaned; with a wrapper, the toggle-scaffolding bug class returns.'; false; } || echo 'PASS: aspirations.yaml has no scripted_extractions: block (dead config not resurrected)'

   # S55.3: .as_posix() discipline on subprocess script paths — state-update-audit.py
   # and precheck-eval.py both spawn helper .sh scripts via subprocess. On Windows,
   # str(WindowsPath(...)) produces backslash paths that Git Bash/MSYS interpret as
   # escape sequences (C:\ZakNoCloud\... becomes C:ZakNoCloud...) and every helper
   # call fails "No such file or directory". The .as_posix() fix (Bug 2) must stay
   # — if a Windows maintainer strips it for aesthetics, the scripted paths die
   # silently on Windows again.
   Check: core/scripts/state-update-audit.py uses .as_posix() for the subprocess script path
   Bash: grep -q '\.as_posix()' core/scripts/state-update-audit.py && echo 'PASS: state-update-audit.py uses .as_posix() (Windows subprocess path-mangling defense intact, Bug 2 regression blocked)' || { echo 'FAIL: state-update-audit.py no longer calls .as_posix() — str(WindowsPath) will feed backslash paths to Git Bash/MSYS, mangling absolute Windows paths and breaking every helper .sh call on Windows (session 100 Bug 2, rb-411).'; false; }
   Check: core/scripts/precheck-eval.py uses .as_posix() for the subprocess script path
   Bash: grep -q '\.as_posix()' core/scripts/precheck-eval.py && echo 'PASS: precheck-eval.py uses .as_posix() (Windows subprocess path-mangling defense intact)' || { echo 'FAIL: precheck-eval.py no longer calls .as_posix() — same Windows path-mangling risk as state-update-audit.py (session 100 Bug 2, rb-411).'; false; }

   # S55.4: fresh-eyes-review Phase 8 stamp-write integrity — the Phase 8
   # tick-record step writes last_fresh_eyes_review WM slot, which the cadence
   # gate reads to decide whether to fire another review. In session bravo-20
   # (g-240-60) the previous Phase 8 was a single &&-chained Bash block that
   # terminated in a best-effort board-post; any step failure (or partial
   # LLM execution) silently dropped the stamp write. Evidence:
   # fresh-eyes-2026-04-20 fired and resolved but left the slot null, causing
   # the next precheck iteration to recommend re-firing 45 min after the user
   # replied. Fix (iter 34, session bravo-20): extracted the load-bearing
   # stamp write into fresh-eyes-record-tick.sh with readback verification,
   # separated the board-post into its own best-effort step with || true.
   # If either the wrapper script OR the separation is removed, the silent-
   # skip regression returns.
   Check: core/scripts/fresh-eyes-record-tick.sh exists and is invocable
   Bash: test -x core/scripts/fresh-eyes-record-tick.sh || test -f core/scripts/fresh-eyes-record-tick.sh && echo 'PASS: fresh-eyes-record-tick.sh present (stamp-write is script-atomic, not &&-chain)' || { echo 'FAIL: fresh-eyes-record-tick.sh missing — the &&-chain silent-skip regression is reintroduced (g-240-60, session bravo-20).'; false; }
   Check: fresh-eyes-record-tick.sh performs readback verification after write
   Bash: grep -q 'readback' core/scripts/fresh-eyes-record-tick.sh && echo 'PASS: wrapper verifies slot non-null after write (guards against silent wm-set failure)' || { echo 'FAIL: fresh-eyes-record-tick.sh missing readback verification — silent write failures will not be caught.'; false; }
   Check: fresh-eyes-review/SKILL.md Phase 8 calls the wrapper (not inline &&-chain)
   Bash: grep -qE 'fresh-eyes-record-tick\.sh' .claude/skills/fresh-eyes-review/SKILL.md && echo 'PASS: Phase 8 delegates to fresh-eyes-record-tick.sh' || { echo 'FAIL: Phase 8 no longer references fresh-eyes-record-tick.sh — if replaced with inline &&-chain, silent-skip regression returns (g-240-60).'; false; }
   Check: Phase 8 board-post is wrapped in || true (best-effort, does not eat stamp write)
   Bash: sed -n '/^## Phase 8:/,/^## Chaining/p' .claude/skills/fresh-eyes-review/SKILL.md | grep -qE 'board-post\.sh.*\|\| true' && echo 'PASS: Phase 8 board-post uses || true (cannot cascade-kill the stamp write)' || { echo 'FAIL: Phase 8 board-post no longer uses || true — a failing board-post could again propagate back and mask stamp-write success signals (g-240-60).'; false; }

   # S55.5: Sibling-ritual cadence-gate SSOT — fresh-eyes-cadence-check.py was
   # parametrized in session 2026-04-23 to support sibling rituals (fresh-eyes-program).
   # Each ritual's (wm_slot, cadence, pending-prefix) triple MUST live in its own
   # aspirations.yaml block, and the script MUST fail-loud on a missing block
   # instead of silently falling back to the review ritual's slot. Regression
   # signal: a typo in --config-block silently fires the wrong ritual against the
   # review's slot at review's cadence (cross-ritual state drift). See rb-481,
   # guard-423, guard-424.
   Check: fresh_eyes_program config block exists in aspirations.yaml
   Bash: grep -qE '^fresh_eyes_program:' core/config/aspirations.yaml && echo 'PASS: fresh_eyes_program config block present' || { echo 'FAIL: fresh_eyes_program block missing from aspirations.yaml — /fresh-eyes-program cadence gate has nothing to read.'; false; }
   Check: fresh_eyes_review and fresh_eyes_program each declare wm_slot (no silent reuse of default)
   Bash: grep -c 'wm_slot: last_fresh_eyes' core/config/aspirations.yaml | py -3 -c "import sys; n=int(sys.stdin.read().strip()); assert n>=2, f'FAIL: only {n} wm_slot declarations for fresh-eyes family — both fresh_eyes_review and fresh_eyes_program blocks must declare wm_slot explicitly (rb-481: no script-level fallback)'; print('PASS: both fresh-eyes rituals declare wm_slot explicitly')"
   Check: DEFAULT_SLOT_NAME dead fallback constant is NOT in fresh-eyes-cadence-check.py
   Bash: grep -q 'DEFAULT_SLOT_NAME' core/scripts/fresh-eyes-cadence-check.py && { echo 'FAIL: DEFAULT_SLOT_NAME constant reintroduced — this was removed in session 2026-04-23 because it duplicated YAML as source of truth; a typo in --config-block would silently reuse the review ritual slot (rb-481, guard-424).'; false; } || echo 'PASS: no DEFAULT_SLOT_NAME fallback (YAML wm_slot is single source of truth)'
   Check: cadence script fails loud on missing config block (error msg is split across python string-concat lines; match the unique fragment)
   Bash: grep -qE "config block '" core/scripts/fresh-eyes-cadence-check.py && grep -qE "not found " core/scripts/fresh-eyes-cadence-check.py && echo 'PASS: missing-block fail-loud stderr path present' || { echo 'FAIL: fresh-eyes-cadence-check.py missing the config-block-not-found stderr path — silent fall-through to defaults reintroduces cross-ritual drift (guard-424).'; false; }
   Check: cadence script fails loud on missing wm_slot field
   Bash: grep -qE "required 'wm_slot' field" core/scripts/fresh-eyes-cadence-check.py && echo 'PASS: missing-wm_slot fail-loud stderr path present' || { echo 'FAIL: fresh-eyes-cadence-check.py missing the required-wm_slot stderr path — a block without wm_slot would silently use no slot name (guard-424).'; false; }
   Check: aspirations-precheck Phase 0.5e.5 invokes fresh_eyes_program with disjoint prefix
   Bash: grep -qE 'config-block fresh_eyes_program.*pending-prefix program-review-' .claude/skills/aspirations-precheck/SKILL.md && echo 'PASS: Phase 0.5e.5 passes both --config-block and --pending-prefix with disjoint program-review- prefix (guard-423)' || { echo 'FAIL: aspirations-precheck Phase 0.5e.5 missing or not invoking with --pending-prefix program-review- — without the disjoint prefix, fresh-eyes-review and fresh-eyes-program skip_if_pending gates block each other (guard-423).'; false; }
   Check: /fresh-eyes-review pending-id uses timestamp (not just date) to avoid same-day collision
   Bash: grep -qE 'id: fresh-eyes-\{YYYY-MM-DDTHH-MM-SS\}' .claude/skills/fresh-eyes-review/SKILL.md && echo 'PASS: /fresh-eyes-review pending-id includes HH-MM-SS' || { echo 'FAIL: /fresh-eyes-review pending-id reverted to date-only — same-day cadence fire + user force produces duplicate IDs.'; false; }
   Check: /fresh-eyes-program pending-id uses timestamp (not just date)
   Bash: grep -qE 'id: program-review-\{YYYY-MM-DDTHH-MM-SS\}' .claude/skills/fresh-eyes-program/SKILL.md && echo 'PASS: /fresh-eyes-program pending-id includes HH-MM-SS' || { echo 'FAIL: /fresh-eyes-program pending-id reverted to date-only — same-day collision risk returns.'; false; }

   # S55.6: Sibling-ritual min_session_goals gate (g-115-1054, g-115-1106)
   # World-counter ticks PER-AGENT rituals every time ANY agent completes a goal.
   # A ritual can therefore "fire" against an agent that has done zero session
   # work — pure ritual-without-cause. The min_session_goals sub-gate compares
   # loop_state.goals_completed_this_session for the firing agent against a
   # per-ritual floor read from the matching aspirations.yaml block. If the
   # cadence-check or felt-sense-check scripts lose this gate (refactor regression),
   # cross-agent fire-without-work returns. Three checks pin the contract: both
   # scripts must reference min_session_goals AND the config block must declare it.
   Check: fresh-eyes-cadence-check.py references min_session_goals (gate code present)
   Bash: grep -c 'min_session_goals' core/scripts/fresh-eyes-cadence-check.py | py -3 -c "import sys; n=int(sys.stdin.read().strip()); assert n>=2, f'FAIL: only {n} min_session_goals references in fresh-eyes-cadence-check.py — gate removed or stub-only (g-115-1054 regression)'; print(f'PASS: fresh-eyes-cadence-check.py has {n} min_session_goals references')"
   Check: felt-sense-cadence-check.py references min_session_goals (gate code present)
   Bash: grep -c 'min_session_goals' core/scripts/felt-sense-cadence-check.py | py -3 -c "import sys; n=int(sys.stdin.read().strip()); assert n>=2, f'FAIL: only {n} min_session_goals references in felt-sense-cadence-check.py — gate removed or stub-only (g-115-1054 regression)'; print(f'PASS: felt-sense-cadence-check.py has {n} min_session_goals references')"
   Check: aspirations.yaml ritual blocks declare min_session_goals (at least 3 — fresh_eyes_review, fresh_eyes_program/tree, felt_sense)
   Bash: grep -c '^\s*min_session_goals:' core/config/aspirations.yaml | py -3 -c "import sys; n=int(sys.stdin.read().strip()); assert n>=3, f'FAIL: only {n} min_session_goals declarations in aspirations.yaml — fresh_eyes_review, fresh_eyes_program (or _tree), and felt_sense each need their own (g-115-1054)'; print(f'PASS: aspirations.yaml declares min_session_goals in {n} ritual blocks')"

   # S55.7: post-decompose-routing-audit wiring gate (g-115-1085, g-115-1112)
   # g-115-1085 added core/scripts/post-decompose-routing-audit.py and wired it
   # into mind_api/src/endpoints/aspirations_write.py via the
   # _file_routing_audit_investigate(ctx, goal) helper called after the main
   # goal lands in add_goal(). This closes the LIFECYCLE-gap from sub-fix 3 —
   # if someone removes or unwires the audit, the closure silently breaks.
   # Two checks pin both halves: script presence + API signature, and
   # daemon wire-in (helper definition + call site).
   Check: post-decompose-routing-audit.py exists with audit API
   Bash: test -f core/scripts/post-decompose-routing-audit.py && grep -q 'def audit' core/scripts/post-decompose-routing-audit.py && echo 'PASS: post-decompose-routing-audit.py present with def audit' || { echo 'FAIL: post-decompose-routing-audit.py missing or def audit signature absent — LIFECYCLE-gap closure from g-115-1085 sub-fix 3 broken'; false; }
   Check: daemon aspirations_write.py wires _file_routing_audit_investigate helper
   Bash: grep -q '_file_routing_audit_investigate' mind_api/src/endpoints/aspirations_write.py && grep -q '_file_routing_audit_investigate(ctx, goal)' mind_api/src/endpoints/aspirations_write.py && echo 'PASS: aspirations_write.py defines and calls _file_routing_audit_investigate(ctx, goal)' || { echo 'FAIL: aspirations_write.py missing the helper definition or its call site — post-decompose routing audit not firing after add_goal (g-115-1085 regression)'; false; }

   # Output-Sanity Gate (Section OSG — 2026-04-20, rb-372, guard-333)
   # Framework-level defense against exit-0 + 0-byte-output silent successes in
   # background-jobs.py::check_job. Generalizes rb-061/rb-085/guard-156 family.
   # If any of these regress, the class of "completed job, empty output, silent
   # downstream poisoning" returns — exactly the failure mode this gate prevents.
   # Single source of truth: core/config/conventions/session-state.md → "Output-Sanity Gate".
   Check: `core/scripts/background-jobs.py` defines `check_output_artifacts` helper
   Bash: grep -c "^def check_output_artifacts" core/scripts/background-jobs.py | py -3 -c "import sys; n=int(sys.stdin.read().strip()); assert n==1, f'FAIL: check_output_artifacts function missing or duplicated ({n} matches, expected 1)'; print('PASS: check_output_artifacts defined exactly once')"
   # CRITICAL anchor: the gate-override block in check_job must stay where it is.
   # Moving it before run_completion_check (or firing on statuses other than
   # 'completed') re-introduces false failures on still-running jobs and
   # conflates runtime crashes with post-exit 0-byte output. The DO-NOT-MOVE
   # comment is the single line guarding the invariant — if it disappears,
   # future maintainers WILL "helpfully" hoist the block.
   Check: `background-jobs.py` check_job has the DO-NOT-MOVE anchor comment
   Bash: grep -c "CRITICAL — DO NOT MOVE" core/scripts/background-jobs.py | py -3 -c "import sys; n=int(sys.stdin.read().strip()); assert n>=1, 'FAIL: DO-NOT-MOVE anchor comment missing from background-jobs.py — gate can be silently hoisted by a refactor'; print('PASS: DO-NOT-MOVE anchor present')"
   Check: gate override only fires after completion_check, only on status=='completed'
   Bash: grep -nE '^\s*if result\["status"\] == "completed"' core/scripts/background-jobs.py | wc -l | py -3 -c "import sys; n=int(sys.stdin.read().strip()); assert n>=1, 'FAIL: gate-override status check missing'; print('PASS: status==completed guard present')"
   Check: `cmd_register` parses `--output-artifacts` into the job entry
   Bash: grep -c 'args.output_artifacts' core/scripts/background-jobs.py | py -3 -c "import sys; n=int(sys.stdin.read().strip()); assert n>=1, 'FAIL: cmd_register does not read --output-artifacts'; print('PASS: --output-artifacts threaded through cmd_register')"
   Check: `--output-artifacts` CLI flag registered on the `register` subparser
   Bash: grep -c '"--output-artifacts"' core/scripts/background-jobs.py | py -3 -c "import sys; n=int(sys.stdin.read().strip()); assert n>=1, 'FAIL: --output-artifacts argparse flag missing'; print('PASS: argparse flag registered')"
   # The shell wrapper is a pure exec "$@" passthrough — it forwards every flag
   # unchanged to the python layer, including --output-artifacts. Asserting the
   # flag name explicitly would break the moment the wrapper stays pristine.
   # Instead, assert the passthrough contract stays intact.
   Check: `core/scripts/background-jobs.sh` ends with `exec python3 ... "$@"` (pure passthrough — flag-agnostic)
   Bash: grep -cE 'exec python3 .* "\$@"' core/scripts/background-jobs.sh | py -3 -c "import sys; n=int(sys.stdin.read().strip()); assert n>=1, 'FAIL: background-jobs.sh lost its exec \"\$@\" passthrough — new flags like --output-artifacts will not reach the python layer'; print('PASS: exec \"\$@\" passthrough intact (all flags forwarded)')"
   Check: unit test harness exists and passes all 9 cases
   Bash: test -x core/scripts/test-background-jobs-output-gate.sh && echo OK || echo MISSING
   Bash: bash core/scripts/test-background-jobs-output-gate.sh 2>&1 | tail -1 | grep -q "9 passed, 0 failed" && echo "PASS: output-gate 9/9 cases pass" || { echo "FAIL: test-background-jobs-output-gate.sh regressed — rerun and inspect"; false; }
   Check: run-processor LAUNCH registers with --output-artifacts for merged Processor outputs
   Bash: grep -c -- '--output-artifacts' .claude/skills/run-processor/SKILL.md | py -3 -c "import sys; n=int(sys.stdin.read().strip()); assert n>=1, 'FAIL: run-processor LAUNCH missing --output-artifacts (spawn-site guard-333 violation)'; print('PASS: run-processor LAUNCH declares output artifacts')"
   Check: run-processor MONITOR reads output_check_failures on failed status
   Bash: grep -c 'output_check_failures' .claude/skills/run-processor/SKILL.md | py -3 -c "import sys; n=int(sys.stdin.read().strip()); assert n>=1, 'FAIL: run-processor MONITOR does not surface output_check_failures — failed jobs will not produce Investigate goals with failure detail'; print('PASS: MONITOR surfaces output_check_failures payload')"
   Check: convention file documents the gate as source of truth
   Bash: grep -c "Output-Sanity Gate" core/config/conventions/session-state.md | py -3 -c "import sys; n=int(sys.stdin.read().strip()); assert n>=1, 'FAIL: session-state.md missing Output-Sanity Gate section — convention drift from implementation'; print('PASS: session-state.md documents the gate')"
   Check: guard-333 exists and is active (spawn-site contract for --output-artifacts)
   Bash: bash core/scripts/guardrails-read.sh --id guard-333 2>/dev/null | grep -c "output-artifacts\|output_artifacts" | py -3 -c "import sys; n=int(sys.stdin.read().strip()); assert n>=1, 'FAIL: guard-333 missing or lost its --output-artifacts action_hint'; print('PASS: guard-333 active with contract reference')"
   Check: rb-372 exists (framework-generalization lesson is retrievable)
   Bash: bash core/scripts/reasoning-bank-read.sh --id rb-372 2>/dev/null | grep -c "dispatcher-audit\|exit-code-trust" | py -3 -c "import sys; n=int(sys.stdin.read().strip()); assert n>=1, 'FAIL: rb-372 missing or lost its core tags — lesson is unretrievable'; print('PASS: rb-372 active with framework-generalization lesson')"

   # Cross-agent live_phase signal (Section LP — 2026-04-22, g-240-64)
   # Net-new information channel between agents with ~60s freshness. Design
   # invariant: "one fail-open boundary, zero internal fallbacks." If any of
   # these checks regress, live-phase-emit.sh either silently reports
   # placeholder values on schema drift (re-introducing the cognitive-load trap
   # the review removed) or the team-state.py validators stop catching
   # malformed agent_status..<field> writes (reopening the A7 gap where an
   # unset MIND_AGENT silently corrupts the YAML). Applies rb-276 (boundary
   # validation), rb-347 (one fail-open boundary), rb-386 (SSoT beats fallbacks).
   Check: `core/scripts/live-phase-emit.sh` exists and is executable
   Bash: test -x core/scripts/live-phase-emit.sh && echo "PASS: live-phase-emit.sh present and executable" || { echo "FAIL: live-phase-emit.sh missing or not executable"; false; }
   # CRITICAL INVARIANT: live-phase-emit.sh must have ZERO internal fallbacks.
   # The SINGLE fail-open boundary is heartbeat-tick.sh's `|| true` on the
   # call site. Protecting at both layers re-introduces the cognitive-load trap.
   Check: `live-phase-emit.sh` contains zero `|| true` or `2>/dev/null` markers in executable code (comments excluded — the CRITICAL INVARIANT comment legitimately references the caller's `|| true`)
   Bash: grep -vE '^\s*#' core/scripts/live-phase-emit.sh | grep -cE '\|\|\s*true|2>/dev/null' | py -3 -c "import sys; n=int(sys.stdin.read().strip()); assert n==0, f'FAIL: live-phase-emit.sh has {n} internal-fallback marker(s) in EXECUTABLE lines — single-fail-open invariant violated. The fail-open boundary belongs on heartbeat-tick.sh caller, NOT inside the emitter.'; print('PASS: live-phase-emit.sh has zero internal fallbacks (comments excluded)')"
   Check: `heartbeat-tick.sh` invokes `live-phase-emit.sh`
   Bash: grep -c 'live-phase-emit.sh' core/scripts/heartbeat-tick.sh | py -3 -c "import sys; n=int(sys.stdin.read().strip()); assert n>=1, 'FAIL: heartbeat-tick.sh does not call live-phase-emit.sh — the live_phase signal is dead on arrival'; print('PASS: heartbeat-tick.sh wired to live-phase-emit.sh')"
   Check: the single fail-open boundary (`|| true`) lives on the heartbeat-tick call site
   Bash: grep -cE 'live-phase-emit.sh.*\|\|\s*true' core/scripts/heartbeat-tick.sh | py -3 -c "import sys; n=int(sys.stdin.read().strip()); assert n>=1, 'FAIL: heartbeat-tick.sh lost the || true on live-phase-emit.sh call — single-fail-open boundary missing. A crash in the emitter would now block the iteration.'; print('PASS: fail-open boundary present at call site')"
   Check: `core/scripts/team-state.py` defines both write-boundary validators
   Bash: grep -cE '^def _validate_field_path|^def _validate_agent_name' core/scripts/team-state.py | py -3 -c "import sys; n=int(sys.stdin.read().strip()); assert n==2, f'FAIL: team-state.py has {n} validator definitions (expected 2). _validate_field_path / _validate_agent_name protect against empty MIND_AGENT silently corrupting YAML with agent_status..<field> dot-paths. Without them the A7 gap returns.'; print('PASS: both write-boundary validators defined')"
   Check: `_validate_field_path` called from `cmd_update` (write boundary 1)
   Bash: py -3 -c "import re; src=open('core/scripts/team-state.py',encoding='utf-8').read(); m=re.search(r'def cmd_update\\(.*?\\n((?:.{0,400}\\n){1,25})', src, re.DOTALL); body=m.group(1) if m else ''; assert '_validate_field_path' in body, 'FAIL: cmd_update does not call _validate_field_path in its first ~25 lines — malformed field paths can reach the YAML writer'; print('PASS: cmd_update guarded by _validate_field_path')"
   Check: `_validate_agent_name` called from `cmd_in_flight` and `cmd_clear_in_flight` (write boundaries 2 and 3)
   Bash: py -3 -c "import re; src=open('core/scripts/team-state.py',encoding='utf-8').read(); a=re.search(r'def cmd_in_flight\\(.*?\\n((?:.{0,400}\\n){1,25})', src, re.DOTALL); b=re.search(r'def cmd_clear_in_flight\\(.*?\\n((?:.{0,400}\\n){1,25})', src, re.DOTALL); assert '_validate_agent_name' in (a.group(1) if a else ''), 'FAIL: cmd_in_flight does not call _validate_agent_name'; assert '_validate_agent_name' in (b.group(1) if b else ''), 'FAIL: cmd_clear_in_flight does not call _validate_agent_name'; print('PASS: both in-flight entry points guarded')"
   # End-to-end smoke tests: exercise the write-boundary validators. Empty
   # field path and malformed dot-path (empty segment simulating unset
   # $MIND_AGENT) must both be rejected loudly. Canonical regression probes
   # for the A7 gap this section closes.
   Check: empty `--field` is rejected by team-state-update (validator fires)
   Bash: MIND_AGENT=alpha bash core/scripts/team-state-update.sh --field "" --value '"x"' 2>&1 | grep -c "empty --field" | py -3 -c "import sys; n=int(sys.stdin.read().strip()); assert n>=1, 'FAIL: empty --field was NOT rejected — _validate_field_path is wired in but not firing, or team-state-update.sh bypasses the validator. A7 gap is open.'; print('PASS: empty --field rejected by write-boundary validator')"
   Check: malformed dot-path (empty segment from unset $MIND_AGENT) is rejected
   Bash: MIND_AGENT=alpha bash core/scripts/team-state-update.sh --field "agent_status..live_phase" --value '"x"' 2>&1 | grep -c "empty segment" | py -3 -c "import sys; n=int(sys.stdin.read().strip()); assert n>=1, 'FAIL: agent_status..live_phase malformed dot-path was NOT rejected — unset MIND_AGENT would still silently corrupt the YAML. This is the exact A7 scenario the validator was added to close.'; print('PASS: empty-segment dot-path rejected')"
   Check: `coordination.md` documents `live_phase` with all 4 legitimate values
   Bash: py -3 -c "t=open('core/config/conventions/coordination.md',encoding='utf-8').read(); missing=[v for v in ('live_phase','between-phases','finding','no-diary') if v not in t]; assert not missing, f'FAIL: coordination.md missing live_phase value(s): {missing}. Schema drift between doc and emitter output.'; print('PASS: coordination.md documents live_phase + all 4 values')"
   Check: `status.py` Team block renders `live_phase` alongside `current_focus`
   Bash: grep -c 'live_phase' core/scripts/status.py | py -3 -c "import sys; n=int(sys.stdin.read().strip()); assert n>=1, 'FAIL: status.py does not render live_phase — the feature is invisible in status output even though it is being written'; print('PASS: status.py renders live_phase')"
   Check: `fresh-eyes-review` SKILL.md captures `partner.live_phase` in briefings
   Bash: grep -c 'partner.live_phase' .claude/skills/fresh-eyes-review/SKILL.md | py -3 -c "import sys; n=int(sys.stdin.read().strip()); assert n>=1, 'FAIL: fresh-eyes-review does not capture partner.live_phase — reviewer misses the live signal'; print('PASS: fresh-eyes-review captures partner.live_phase')"

   # Origin: g-115-517 — encode-session 2026-05-09 (g-115-515) refactored
   # fresh-eyes-review/SKILL.md and post-execution.md Step 1.75. These five
   # checks defend the structural invariants of that refactor against silent
   # reversion (sq-018-class — framework files, regression cost is high).
   Check: fresh-eyes-review/SKILL.md MUST NOT contain "Is the current Self still right" (Self-question removed in g-115-515 refactor)
   Bash: grep -q "Is the current Self still right" .claude/skills/fresh-eyes-review/SKILL.md && { echo 'FAIL: Self-question phrase reintroduced — g-115-515 refactor reverted'; false; } || echo 'PASS: Self-question phrase absent'
   Check: fresh-eyes-review/SKILL.md MUST NOT contain "Candidate Self refinements" (subsection removed in g-115-515 refactor)
   Bash: grep -q "Candidate Self refinements" .claude/skills/fresh-eyes-review/SKILL.md && { echo 'FAIL: Candidate Self refinements subsection reintroduced'; false; } || echo 'PASS: Candidate Self refinements absent'
   Check: world post-execution.md Step 1.75 MUST contain "Invoke /fresh-eyes-code" (structured probe wiring)
   Bash: source core/scripts/_paths.sh && grep -q "Invoke /fresh-eyes-code" "$WORLD_DIR/conventions/post-execution.md" && echo 'PASS: post-execution.md invokes /fresh-eyes-code' || { echo 'FAIL: post-execution.md no longer invokes /fresh-eyes-code — Step 1.75 structured-probe wiring reverted'; false; }
   Check: world post-execution.md Step 1.75 MUST contain "--author $MIND_AGENT" (multi-agent leak fix per guard-493)
   Bash: source core/scripts/_paths.sh && grep -q -- "--author \$MIND_AGENT" "$WORLD_DIR/conventions/post-execution.md" && echo 'PASS: post-execution.md filters by author' || { echo 'FAIL: post-execution.md missing --author $MIND_AGENT — multi-agent leak per guard-493 returns'; false; }
   Check: world post-execution.md `$pre_ts` only appears in explanatory warning text per guard-492 (NOT in actual Bash commands)
   Bash: source core/scripts/_paths.sh && WORLD_DIR="$WORLD_DIR" py -3 -c 'import os,pathlib; src=pathlib.Path(os.environ["WORLD_DIR"]+"/conventions/post-execution.md").read_text(encoding="utf-8"); lines=[ln for ln in src.split("\n") if "$pre_ts" in ln]; bad=[ln for ln in lines if "Do not write" not in ln]; assert not bad, f"FAIL: $pre_ts in non-warning lines: {bad}"; print(f"PASS: $pre_ts only in warning text ({len(lines)} occurrences)")'

   # Origin: g-115-539 — g-115-537 introduced 4 mirror sites for the multi-Roblox-bridge
   # routing map (4 envs: source/dev/ppe/prod). These checks defend against silent drift
   # between the 4 mirror sites; without them, divergence is undetectable until probe-
   # bridge.sh produces a runtime mismatch.
   Check: world/scripts/_roblox_envs.sh exports ALL_ENVS + the 3 lookup functions for all 4 envs
   Bash: source core/scripts/_paths.sh && WORLD_DIR="$WORLD_DIR" py -3 -c 'import os,re,pathlib; src=pathlib.Path(os.environ["WORLD_DIR"]+"/scripts/_roblox_envs.sh").read_text(encoding="utf-8"); assert re.search(r"ALL_ENVS=\"source dev ppe prod\"", src), "FAIL: ALL_ENVS missing or wrong order"; [ (re.search(rf"^{fn}\(\)", src, re.M) or (_ for _ in ()).throw(AssertionError(f"FAIL: missing function {fn}"))) for fn in ("env_to_port","env_to_place_id","env_to_place_name") ]; [ (env in src or (_ for _ in ()).throw(AssertionError(f"FAIL: env {env} missing"))) for env in ("source","dev","ppe","prod") ]; print("PASS: _roblox_envs.sh has ALL_ENVS + 3 lookup functions + 4 envs")'
   Check: world/scripts/probe-bridge.sh sources the shared _roblox_envs.sh lib
   Bash: source core/scripts/_paths.sh && grep -q "_roblox_envs\.sh" "$WORLD_DIR/scripts/probe-bridge.sh" && echo 'PASS: probe-bridge.sh sources shared _roblox_envs.sh' || { echo 'FAIL: probe-bridge.sh no longer sources _roblox_envs.sh — duplicated env constants will drift silently per rb-334'; false; }
   Check: world/scripts/roblox-studio.sh sources the shared _roblox_envs.sh lib
   Bash: source core/scripts/_paths.sh && grep -q "_roblox_envs\.sh" "$WORLD_DIR/scripts/roblox-studio.sh" && echo 'PASS: roblox-studio.sh sources shared _roblox_envs.sh' || { echo 'FAIL: roblox-studio.sh no longer sources _roblox_envs.sh — wrapper-side env constants will drift from probe-side'; false; }
   Check: world/conventions/roblox-environments.md canonical convention exists and enumerates source/dev/ppe/prod
   Bash: source core/scripts/_paths.sh && WORLD_DIR="$WORLD_DIR" py -3 -c 'import os,pathlib; src=pathlib.Path(os.environ["WORLD_DIR"]+"/conventions/roblox-environments.md").read_text(encoding="utf-8"); [ (env in src or (_ for _ in ()).throw(AssertionError(f"FAIL: convention missing {env}"))) for env in ("| source |","| dev |","| ppe |","| prod |") ]; print("PASS: roblox-environments.md enumerates source/dev/ppe/prod in env table")'
   Check: roblox-bridge-environments tree node is registered with correct file path
   Bash: bash core/scripts/tree-read.sh --node roblox-bridge-environments | py -3 -c 'import json,sys; d=json.load(sys.stdin); f=d.get("file","") or ""; assert f.endswith("roblox-bridge-environments.md"), "FAIL: tree node file path drift: "+f; print("PASS: tree node registered, file="+f)'

   # Origin: g-115-504 — path-resolution-hook.py gained is_new_toplevel() on 2026-05-09
   # for Mode C cruft prevention. Removing the function OR removing either the
   # WORLD_PATH/META_PATH branch OR the agent-dir branch silently restores cruft creation.
   Check: path-resolution-hook.py defines is_new_toplevel function
   Bash: grep -q "^def is_new_toplevel\b" core/scripts/path-resolution-hook.py && echo 'PASS: is_new_toplevel function present' || { echo 'FAIL: is_new_toplevel function removed/renamed — Mode C cruft prevention reverted'; false; }
   Check: path-resolution-hook.py covers WORLD_PATH + META_PATH branches in cruft check
   Bash: py -3 -c "import pathlib; src=pathlib.Path('core/scripts/path-resolution-hook.py').read_text(encoding='utf-8'); wp=src.count('WORLD_PATH'); mp=src.count('META_PATH'); assert wp >= 5 and mp >= 5, f'FAIL: insufficient coverage WORLD_PATH={wp} META_PATH={mp}'; print(f'PASS: WORLD_PATH={wp}, META_PATH={mp} (both >= 5)')"
   Check: path-resolution-hook.py PROJECT_ROOT agent-dir branch present (label == PROJECT_ROOT + agent_dir_norm adjacent)
   Bash: py -3 -c "import pathlib; src=pathlib.Path('core/scripts/path-resolution-hook.py').read_text(encoding='utf-8'); lines=src.split('\n'); pr=[i for i,ln in enumerate(lines) if 'label == \"PROJECT_ROOT\"' in ln or 'label==\"PROJECT_ROOT\"' in ln]; adn=[i for i,ln in enumerate(lines) if 'agent_dir_norm' in ln]; assert pr, 'FAIL: label == PROJECT_ROOT branch removed — agent-dir cruft check reverted'; assert adn, 'FAIL: agent_dir_norm references removed'; ok=any(abs(p-a) <= 5 for p in pr for a in adn); assert ok, f'FAIL: PROJECT_ROOT branch at {pr} no longer within 5 lines of agent_dir_norm at {adn[:3]}'; print(f'PASS: PROJECT_ROOT branch + agent_dir_norm adjacent (pr={pr}, adn={adn[:3]})')"

   # Origin: g-115-513 — Layer-D structural defense for Mode C cruft prevention. The
   # 2026-05-09 incident produced an invented "world/handoffs/" path because there was
   # no sanctioned destination for an audit/handoff artifact. The remediation established
   # three artifacts on disk: world/audit-reports/ (the destination), world/audit-reports/
   # README.md (catalog + naming convention), and world/conventions/audit-reports.md
   # (the canonical convention pointing agents at the destination). Removing ANY of the
   # three breaks the structural defense — the agent re-encounters the same temptation
   # to invent. Sibling check to the path-resolution-hook is_new_toplevel guard above:
   # the hook stops invention at write-time, this check stops loss of the legitimate
   # alternative the hook directs agents toward. See rb-765 + .claude/rules/path-
   # resolution.md "L1 Cruft Prevention".
   Check: world/audit-reports canonical destination exists (directory + README + convention)
   Bash: source core/scripts/_paths.sh && test -d "$WORLD_DIR/audit-reports" && test -f "$WORLD_DIR/audit-reports/README.md" && test -f "$WORLD_DIR/conventions/audit-reports.md" && echo 'PASS: audit-reports/ + README + conventions/audit-reports.md all present' || { echo 'FAIL: audit-reports canonical destination missing one of (audit-reports/, audit-reports/README.md, conventions/audit-reports.md) — Mode C structural defense (rb-765) reverted; agents will face the no-sanctioned-destination temptation again'; false; }

   # Origin: g-115-544 — 2026-05-10 reasoning-bank applies_to-required + retrieval MMR + tag
   # canonicalization commits (495dc71, ab3d339, 47a905f, 419472e). These four checks defend
   # against silent regression of fresh framework invariants whose failure modes are sneaky:
   # (1) applies_to validation skipped → records get added without it → `applies_to=None`
   #     leaks accumulate → audit-applies-to.py auto-bucket logic re-fires unnecessarily.
   # (2) leaker count > 0 → retrieval-quality drops as `any` records get over-prescribed
   #     to category-specific queries.
   # (3) tag canonicalization broken → mixed-case tags persist → tag-collision audits pass
   #     while retrieval mis-matches "Framework"/"framework" as distinct tags.
   # (4) MMR no-op branch removed → narrow queries (<=K results) waste cycles on
   #     diversity rerank that has nothing to diversify, and ordering may drift from
   #     pure relevance for users who depend on rank-stability.
   Check: applies_to is required by reasoning-bank-add.sh (validation rejects + names the field)
   Bash: out=$(echo '{"title":"verify-test","content":"x","category":"framework","type":"failure","when_to_use":"never","tags":["t"]}' | bash core/scripts/reasoning-bank-add.sh 2>&1); rc=$?; if [ $rc -ne 0 ] && echo "$out" | grep -q "applies_to"; then echo 'PASS: rb-add rejects missing applies_to and error names the field'; else echo "FAIL: rb-add validation regression — exit=$rc, applies_to-mention=$(echo \"$out\" | grep -c applies_to) (commit 495dc71 reverted?)"; false; fi
   Check: applies_to leaker audit returns zero leakers across active+retired
   Bash: py -3 core/scripts/audit-applies-to.py --include-retired 2>&1 | grep -q "0 entries with applies_to=None" && echo 'PASS: 0 applies_to leakers' || { echo 'FAIL: applies_to leakers detected — some write path is bypassing validation; run audit-applies-to.py --apply-* to triage'; false; }
   Check: _normalize_tags wired into reasoning-bank rb_add + rb_update validators (lowercase + dedup at write time, ab3d339)
   Bash: py -3 -c "import re,pathlib; src=pathlib.Path('core/scripts/reasoning-bank.py').read_text(encoding='utf-8'); defs=re.findall(r'^def _normalize_tags\b', src, re.MULTILINE); calls=src.count('_normalize_tags(rec)'); print(f'PASS: _normalize_tags defined ({len(defs)}) and invoked from {calls} write site(s)') if (len(defs)==1 and calls>=2) else (print(f'FAIL: _normalize_tags wiring regressed — defs={len(defs)} calls={calls} (need 1 def + 2+ calls per ab3d339)') or exit(1))"
   Check: tree_match._mmr_rerank no-op early-return when len(scored) <= limit (47a905f rank-stability invariant)
   Bash: py -3 -c "import re,pathlib; src=pathlib.Path('core/scripts/tree_match.py').read_text(encoding='utf-8'); m=re.search(r'^def _mmr_rerank\b.*?(?=^def |\Z)', src, re.DOTALL|re.MULTILINE); body=m.group(0) if m else ''; ok=bool(re.search(r'if\s+len\(scored\)\s*<=\s*limit\s*:\s*\n\s*return\s+list\(scored\)', body)); print('PASS: _mmr_rerank no-op early-return present') if ok else (print('FAIL: _mmr_rerank no-op branch removed — narrow queries (<=K) will pay diversity-rerank cost AND lose pure-relevance rank-stability') or exit(1))"

   # Origin: g-115-531 — 2026-05-09 zeta-session discovered 3 dotted-field-path
   # corrupted goals (g-115-494, g-271-12, g-274-18) caused by the flat-assignment
   # idiom in aspirations-update-goal.sh + 5 sibling field=value scripts. g-115-529
   # shipped the cross-script fix (rb-776 / guard-497). This verify-learning lane
   # defends against (a) future LLM-authored calls slipping through before all
   # callers are updated, (b) regression after the fix lands. Scans every
   # aspiration JSONL store (world live+archive, alpha live+archive, bravo
   # live+archive) for dotted keys at aspiration-level or goal-level;
   # underscore-prefixed internal keys are exempt. Expected on clean state: 0
   # violations across all stores. See world/knowledge/tree/system/system-
   # constraints-loop/dotted-field-path-silent-corruption.md for the audit
   # heuristic and bug-class anatomy.
   Check: no aspiration store contains dotted-literal keys at aspiration-level or goal-level
   Bash: source core/scripts/_paths.sh && WORLD_DIR="$WORLD_DIR" PROJECT_ROOT="$(pwd)" py -3 -c 'import json,os,glob,pathlib
files=[os.path.join(os.environ["WORLD_DIR"],"aspirations.jsonl")]
files+=glob.glob(os.path.join(os.environ["WORLD_DIR"],"archived","aspirations*.jsonl"))
files+=glob.glob(os.path.join(os.environ["WORLD_DIR"],"aspirations-archive*.jsonl"))
pr=pathlib.Path(os.environ["PROJECT_ROOT"])
for a in ("alpha","bravo"):
    if (pr/a).is_dir():
        files.extend(str(f) for f in (pr/a).glob("aspirations*.jsonl"))
viol=[]
for fp in files:
    if not os.path.isfile(fp): continue
    with open(fp,"r",encoding="utf-8") as fh:
        for line in fh:
            line=line.strip()
            if not line: continue
            try: rec=json.loads(line)
            except Exception: continue
            asp_id=rec.get("id","<line>")
            for k in rec.keys():
                if "." in k and not k.startswith("_"):
                    viol.append((os.path.basename(fp),asp_id,"<asp-level>",k))
            for g in rec.get("goals",[]):
                gid=g.get("id","<no-id>")
                for gk in g.keys():
                    if "." in gk and not gk.startswith("_"):
                        viol.append((os.path.basename(fp),asp_id,gid,gk))
assert not viol, f"FAIL: {len(viol)} dotted-literal keys found across aspiration stores: {viol[:5]}"
print(f"PASS: {len(files)} aspiration stores scanned, 0 dotted-literal keys")'

   # Implementation-side companion to the data-side scan above (g-115-579, 2026-05-10):
   # the `if "." in field` reject idiom MUST stay present in all 6 field=value scripts
   # that g-115-566 wired (aspirations.py, pipeline.py, reasoning-bank.py,
   # experience.py, spark-questions.py, pattern-signatures.py — 10 sites total).
   # Catches regression: any future caller dropping the guard would re-open the
   # silent-corruption class g-115-529 closed. See zeta/reports/g-115-529-dotted-path-corruption-decision.md
   # for the design rationale and rb-776 / guard-497 for the encoded lesson.
   Check: 6 field=value scripts collectively contain ≥10 `if "." in field` reject sites (g-115-566 baseline)
   Bash (dotted-reject-presence): n=$(grep -cE 'if "\." in field' core/scripts/aspirations.py core/scripts/pipeline.py core/scripts/reasoning-bank.py core/scripts/experience.py core/scripts/spark-questions.py core/scripts/pattern-signatures.py 2>/dev/null | awk -F: '{s+=$2} END {print s}'); test "$n" -ge 10 && echo "PASS: $n dotted-reject sites across 6 scripts (>=10 baseline)" || { echo "FAIL: only $n dotted-reject sites — regression below g-115-566 baseline of 10; new caller likely missing the guard"; exit 1; }
   Check: `core/scripts/tests/test_dotted_path_rejection.sh` exists and exits 0 (regression catches: test deleted or core idiom broken)
   Bash (dotted-reject-test-runs): test -x core/scripts/tests/test_dotted_path_rejection.sh && bash core/scripts/tests/test_dotted_path_rejection.sh >/dev/null 2>&1 && echo "PASS: dotted_path_rejection test exits 0" || { echo "FAIL: test_dotted_path_rejection.sh missing or non-zero exit — dotted-reject contract regressed"; exit 1; }

   # g-284-05 iteration-close phase-rejection coverage. Each test simulates rejection at
   # one of the four iteration-close phases (verify, state-update, learning-gate,
   # productivity-check) and asserts the documented state inconsistency shape from
   # asp-284 motivation. Tests are descriptive: they document the split-brain shape
   # the asp-284 fix family addresses. Regressions catch: phase function signature
   # changes that invalidate the inconsistency description, OR tests deleted.
   Check: `core/scripts/tests/test_verify_rejection_split_brain.py` exists and exits 0 (g-284-01)
   Bash (verify-rejection-test-runs): test -f core/scripts/tests/test_verify_rejection_split_brain.py && py -3 core/scripts/tests/test_verify_rejection_split_brain.py >/dev/null 2>&1 && echo "PASS: test_verify_rejection_split_brain.py exits 0" || { echo "FAIL: test_verify_rejection_split_brain.py missing or non-zero exit"; exit 1; }
   Check: `core/scripts/tests/test_state_update_rejection_split_brain.py` exists and exits 0 (g-284-05)
   Bash (state-update-rejection-test-runs): test -f core/scripts/tests/test_state_update_rejection_split_brain.py && py -3 core/scripts/tests/test_state_update_rejection_split_brain.py >/dev/null 2>&1 && echo "PASS: test_state_update_rejection_split_brain.py exits 0" || { echo "FAIL: test_state_update_rejection_split_brain.py missing or non-zero exit"; exit 1; }
   Check: `core/scripts/tests/test_learning_gate_rejection_split_brain.py` exists and exits 0 (g-284-05)
   Bash (learning-gate-rejection-test-runs): test -f core/scripts/tests/test_learning_gate_rejection_split_brain.py && py -3 core/scripts/tests/test_learning_gate_rejection_split_brain.py >/dev/null 2>&1 && echo "PASS: test_learning_gate_rejection_split_brain.py exits 0" || { echo "FAIL: test_learning_gate_rejection_split_brain.py missing or non-zero exit"; exit 1; }
   Check: `core/scripts/tests/test_productivity_check_rejection_split_brain.py` exists and exits 0 (g-284-05)
   Bash (productivity-check-rejection-test-runs): test -f core/scripts/tests/test_productivity_check_rejection_split_brain.py && py -3 core/scripts/tests/test_productivity_check_rejection_split_brain.py >/dev/null 2>&1 && echo "PASS: test_productivity_check_rejection_split_brain.py exits 0" || { echo "FAIL: test_productivity_check_rejection_split_brain.py missing or non-zero exit"; exit 1; }

   # g-283-06 counter-advance regression. g-283-04 retired the LLM-side mirror at
   # LOOP_CONTINUE that wrote loop_state.goals_completed / productive_goals; the
   # retirement assumed bash gates already wrote these fields, but no writer existed.
   # g-283-03's shape-invariance test passed despite the gap. This check pins
   # counter-advance: the helper invocation MUST bump goals_completed by +1 and
   # (when outcome=deep) productive_goals by +1.
   Check: `core/scripts/tests/test_loop_state_counter_advance.py` exists and exits 0 (g-283-06)
   Bash (loop-state-counter-advance-test-runs): test -f core/scripts/tests/test_loop_state_counter_advance.py && py -3 core/scripts/tests/test_loop_state_counter_advance.py >/dev/null 2>&1 && echo "PASS: test_loop_state_counter_advance.py exits 0" || { echo "FAIL: test_loop_state_counter_advance.py missing or non-zero exit — loop_state counter writer may have regressed"; exit 1; }

   # === Section S60: PreToolUse hook deny-protocol uniformity (g-115-1012) =========
   # Three structural checks that catch the INERT-gate class of regression
   # surfaced during /encode-session 2026-05-20. A PreToolUse deny gate can
   # ship in a likely-INERT shape (Python sys.exit(2) + Bash wrapper that
   # propagates the exit code via `exec python3 ...`) and still pass test
   # invocation because the scripts exit cleanly — but Claude Code interprets
   # any non-zero wrapper exit as a hook ERROR (fail-open silently), NOT as a
   # deny. The authoritative spec lives in hook_helpers.py (`emit_deny()`:
   # JSON on stdout + exit 0) and is documented in marker-placement-gate.py
   # header ("Any exit code != 0 is treated by Claude Code as a hook ERROR
   # (fail-open) — NOT as a deny").
   #
   # S60.1: Every PreToolUse Python gate imports `hook_helpers` AND has no
   # `sys.exit(1)`/`sys.exit(2)` in the deny path. EXEMPT lists carry inline
   # justification: legacy gates pending retire, recording-only hooks that
   # have no deny path.
   # S60.2: Every PreToolUse Bash wrapper exits 0 unconditionally — last
   # non-comment `exit` line is `exit 0`, no `exit $RC`/`exit $?` pattern,
   # no terminal `exec python3 ...` (exec replaces the process so wrapper
   # inherits Python's exit code).
   # S60.3: ALLOWLIST sync — marker-placement-gate.py ALLOWLIST set and
   # domain-leak-check.sh ALLOWLIST array MUST hold identical entries.
   # Drift means one gate accepts what the other rejects.

   # S60.1: Python deny-protocol uniformity
   Check: PreToolUse Python gates discovered via `.claude/settings.json` import `hook_helpers` and avoid `sys.exit(1)`/`sys.exit(2)` outside comments.
   Bash: py -3 -c "
import json, re, sys
from pathlib import Path
LEGACY_EXEMPT = {'context-reads.py'}
RECORDING_HOOKS = {'evolution-prepare.py', 'bash-agent-inject.py'}
settings = json.load(open('.claude/settings.json'))
checked, violations = {}, []
for entry in settings.get('hooks', {}).get('PreToolUse', []):
    for h in entry.get('hooks', []):
        cmd = h.get('command', '')
        m = re.search(r'core/scripts/([a-zA-Z0-9_\-]+)\.sh', cmd)
        if not m: continue
        wrapper = Path(f'core/scripts/{m.group(1)}.sh')
        if not wrapper.exists(): continue
        wsrc = wrapper.read_text(encoding='utf-8')
        py_refs = set()
        sib = Path(f'core/scripts/{m.group(1)}.py')
        if sib.exists(): py_refs.add(sib)
        for pm in re.finditer(r'python3?\s.*?(?:core/scripts/|/scripts/)([a-zA-Z0-9_\-]+\.py)', wsrc):
            ref = Path(f'core/scripts/{pm.group(1)}')
            if ref.exists(): py_refs.add(ref)
        for p in py_refs:
            n = p.name
            if n in checked: continue
            if n in LEGACY_EXEMPT: checked[n]='EXEMPT'; continue
            if n in RECORDING_HOOKS: checked[n]='RECORDING'; continue
            src = p.read_text(encoding='utf-8')
            has_h = 'from hook_helpers' in src or 'import hook_helpers' in src
            bad = [ln for ln,l in enumerate(src.splitlines(),1) if not l.lstrip().startswith('#') and re.search(r'sys\.exit\(\s*[12]\s*\)', l)]
            ok = has_h and not bad
            checked[n] = 'OK' if ok else 'BAD'
            if not has_h: violations.append(f'{n}: missing hook_helpers import')
            if bad: violations.append(f'{n}: sys.exit(1)/sys.exit(2) at line(s) {bad}')
if violations:
    print('FAIL: PreToolUse Python deny-protocol violations (g-115-1012):')
    for v in violations: print(f'  - {v}')
    sys.exit(1)
ok_count = sum(1 for s in checked.values() if s == 'OK')
exempt_count = sum(1 for s in checked.values() if s in ('EXEMPT','RECORDING'))
print(f'PASS: PreToolUse Python deny-protocol clean ({ok_count} OK / {exempt_count} exempt across {len(checked)} unique gates)')
"

   # S60.2: Bash wrappers exit 0 unconditionally
   Check: PreToolUse Bash wrappers do NOT end with `exec python3 ...`, do NOT carry `exit \$RC` patterns, and their last non-comment `exit` line is `exit 0`.
   Bash: py -3 -c "
import json, re, sys
from pathlib import Path
EXEMPT = {'context-reads-gate.sh': 'legacy exec-propagate pattern; retire via g-115-1012 follow-up'}
settings = json.load(open('.claude/settings.json'))
wrappers = set()
for entry in settings.get('hooks', {}).get('PreToolUse', []):
    for h in entry.get('hooks', []):
        cmd = h.get('command', '')
        m = re.search(r'core/scripts/([a-zA-Z0-9_\-]+)\.sh', cmd)
        if m:
            p = Path(f'core/scripts/{m.group(1)}.sh')
            if p.exists(): wrappers.add(p)
violations = []
for w in sorted(wrappers):
    if w.name in EXEMPT: continue
    lines = w.read_text(encoding='utf-8').splitlines()
    exit_lines, exec_lines = [], []
    for ln, line in enumerate(lines, 1):
        if line.lstrip().startswith('#'): continue
        nc = re.sub(r'\s*#.*\$', '', line)
        em = re.match(r'^\s*exit\s+(.+?)\s*\$', nc)
        if em: exit_lines.append((ln, em.group(1).strip()))
        em2 = re.match(r'^\s*exec\s+(python3?\s)', nc)
        if em2: exec_lines.append(ln)
    if exec_lines and (not exit_lines or exit_lines[-1][0] < exec_lines[-1]):
        violations.append(f'{w.name}: terminal exec python3 at line {exec_lines[-1]} — wrapper inherits Python exit code, not exit 0')
        continue
    if not exit_lines:
        violations.append(f'{w.name}: no exit line — last-command exit code is non-deterministic')
        continue
    last_ln, last_val = exit_lines[-1]
    if last_val != '0':
        violations.append(f'{w.name}: line {last_ln} final exit is exit {last_val} (not exit 0)')
    for ln, v in exit_lines:
        if v.startswith('\$'):
            violations.append(f'{w.name}: line {ln} propagates variable exit code exit {v} — fails unconditional-exit-0 contract')
if violations:
    print('FAIL: PreToolUse Bash wrapper exit-protocol violations (g-115-1012):')
    for v in violations: print(f'  - {v}')
    sys.exit(1)
print(f'PASS: PreToolUse Bash wrappers exit 0 unconditionally ({len(wrappers)-len(EXEMPT)} checked, {len(EXEMPT)} exempt)')
"

   # S60.3: ALLOWLIST sync between marker-placement-gate.py and domain-leak-check.sh
   Check: Both ALLOWLISTs hold the same set of paths. Drift admits leaks in one direction or false positives in the other (the Phase 5 marker-placement doctrine names them as a single source-of-truth pair).
   Bash: py -3 -c "
import re, sys
from pathlib import Path
py_src = Path('core/scripts/marker-placement-gate.py').read_text(encoding='utf-8')
sh_src = Path('core/scripts/domain-leak-check.sh').read_text(encoding='utf-8')
py_m = re.search(r'ALLOWLIST = \{([^}]+)\}', py_src)
sh_m = re.search(r'ALLOWLIST=\(([^)]+)\)', sh_src)
if not py_m: print('FAIL: marker-placement-gate.py ALLOWLIST = {...} block not found'); sys.exit(1)
if not sh_m: print('FAIL: domain-leak-check.sh ALLOWLIST=(...) block not found'); sys.exit(1)
py_paths = set(re.findall(r'\"([^\"]+)\"', py_m.group(1)))
sh_paths = set(re.findall(r'\"([^\"]+)\"', sh_m.group(1)))
if py_paths != sh_paths:
    print(f'FAIL: marker-placement ALLOWLIST drift between .py and .sh (g-115-1012):')
    print(f'  In .py only: {sorted(py_paths - sh_paths)}')
    print(f'  In .sh only: {sorted(sh_paths - py_paths)}')
    sys.exit(1)
print(f'PASS: marker-placement ALLOWLIST in sync ({len(py_paths)} entries)')
"

Provide a summary table:
- Total PASS / FAIL / N/A per section
- List of any FAIL items that need attention

## Chaining
- Calls: nothing
- Called by: User only. NEVER by Claude.
