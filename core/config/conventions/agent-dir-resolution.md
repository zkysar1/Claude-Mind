# Agent-dir Resolution

Reference for how agent directories are resolved (`agents_root()` /
`agent_dir(name)` and the three layout constants), every place those constants
are mirrored, and every cross-agent glob that must route through the helper.
Loaded on demand via `load-conventions.sh agent-dir-resolution` — `CLAUDE.md`
keeps only the constants, the Rule, and the helper API.

Moved here verbatim from `CLAUDE.md` § "Agent-dir Resolution" on 2026-08-17
(context-window diet, g-115-6469 lineage): the section was 16.5 KB of sync-site
tables and incident history loaded on every turn of every agent, and is needed
only when changing a constant or adding a cross-agent glob. Any reference to
"CLAUDE.md Agent-dir Resolution" resolves here. **Read this file BEFORE changing
`AGENTS_PARENT_DIR`, `SESSIONS_DIRNAME`, or `SESSION_DIRNAME`, or adding any
glob that sweeps across agent dirs.**

## Constants and sync sites

Agent directories are resolved through a centralized helper, not hardcoded
`PROJECT_ROOT / agent_name` joins. This indirection exists so that Phase 2.5.D
relocated all agent dirs under an `agents/` parent by flipping one constant.

**AGENTS_PARENT_DIR** — empty string means agent dirs live at `PROJECT_ROOT`
(legacy layout). Currently `"agents"` — agent dirs live at
`PROJECT_ROOT/agents/<name>`.

**Phase 2.6 added two more sync constants** for the per-session dir layout:
- `SESSIONS_DIRNAME` (currently `"sessions"`) — parent under each agent for
  per-session dirs (one per Claude Code session)
- `SESSION_DIRNAME` (currently `"session"`) — agent-wide cross-session state dir

The 6 framework-layer sync locations carry all three constants:

| Layer | File | Constants |
|-------|------|-----------|
| Python CLI | `core/scripts/_paths.py` | `AGENTS_PARENT_DIR`, `SESSIONS_DIRNAME`, `SESSION_DIRNAME` |
| Shell CLI | `core/scripts/_paths.sh` | same |
| Daemon | `mind_api/src/agent_paths.py` | same |
| Import-cycle-proof | `core/scripts/_agents.py` | same |
| Import-cycle-proof | `core/scripts/path-resolution-hook.py` | same |
| Import-cycle-proof | `core/scripts/_world_config.py` | `AGENTS_PARENT_DIR` (added 2026-05-20 after Phase 2.5.D regression where this helper still used the pre-relocation `root/agent/local-paths.conf` shape — all `world/config/*.yaml` overlay loads silently degraded to safe defaults for ~3 weeks until pytest collection collision surfaced 25 routing-table-empty failures) |
| Import-cycle-proof | `core/scripts/_session_binding.py` | `AGENTS_PARENT_DIR`, `SESSIONS_DIRNAME` (Phase 2.6 resolver — re-exported to `migrate-to-phase-2-6.py`, `session-binding-write.py`, `_resolve_agent_from_sid.py`) |

**Plus 5 inlined copies** that mirror the constants by hand because sourcing
`_paths.sh` (or importing `_paths.py`) would violate the script's contract —
either by exceeding the per-Bash-call latency budget (the `IRREDUCIBLY LOCAL`
annotation at the top of each shell script) or by breaking the shell→python
bridge that calls the helper via `py -3 -c "from <module> import ..."`:

| File | Constants inlined | Reason |
|------|-------------------|--------|
| `core/scripts/cleanup-stale-bindings.sh` | `AGENTS_PARENT_DIR` (`_APD`), `SESSIONS_DIRNAME` (`_SDN`) | IRREDUCIBLY LOCAL — per-Bash-call latency budget |
| `core/scripts/session-mode-get.sh` | `AGENTS_PARENT_DIR` (`_APD`) | IRREDUCIBLY LOCAL — session-state critical path |
| `core/scripts/session-signal-exists.sh` | `AGENTS_PARENT_DIR` (`_APD`) | IRREDUCIBLY LOCAL — hook hot path |
| `core/scripts/session-state-get.sh` | `AGENTS_PARENT_DIR` (`_APD`) | IRREDUCIBLY LOCAL — every loop iteration |
| `core/scripts/_wake_signals.py` | `AGENTS_PARENT_DIR` (`_AGENTS_PARENT_DIR`) | imported via `py -3 -c "from _wake_signals import ..."` from shell — must stay self-contained |

**Plus 2 literal-string hardcoders** bake the literal `agents/` path segment
directly into a glob or prefix-check WITHOUT naming the constant — so the
constant-definition audit grep below does NOT see them. They work for
`AGENTS_PARENT_DIR=agents` but would break on any rename:

| File | Hardcoded sites | Why not constant-routed |
|------|-----------------|-------------------------|
| `core/scripts/iteration-commit.sh` | `"$REPO"/agents/*/` agent-dir walk (~L221) + `[[ "$path" == agents/* ]]` namespace-filter prefix checks (~L591/657/706) | Namespace filter; never sources `_paths.sh`. Contrast `stop-hook.sh:128`, which DOES route its `*/session/` glob through `${AGENTS_PARENT_DIR}` — the gold-standard pattern these two should adopt. |
| `core/scripts/seed-transplant.sh` | `"$DEST"/agents/*/session/agent-state` walk (~L99) | Walks a FOREIGN repo root (`$DEST`), not `PROJECT_ROOT` |

When changing `AGENTS_PARENT_DIR`, `SESSIONS_DIRNAME`, or `SESSION_DIRNAME`,
update ALL 12 constant-named sites AND the 2 literal-string hardcoders above.
Audit with ALL FOUR greps (the first finds constant-named sites; the second
finds literal-`agents/` glob hardcoders — it also surfaces comments/tests/bench
refs, so eyeball-filter to executable glob/prefix lines; the third finds
`.parent`-based PROJECT_ROOT re-derivations from an agent-dir variable, which
the constant-name and `agents/*` greps both miss):
`grep -rn '^[[:space:]]*\(_APD=\|_SDN=\|_\?AGENTS_PARENT_DIR\|_\?SESSIONS_DIRNAME\|_\?SESSION_DIRNAME\)' core/scripts/ mind_api/`
`grep -rn 'agents/\*' core/scripts/ mind_api/`
`grep -rnE '(agent_dir|AGENT_DIR)\.parent' core/scripts/ mind_api/`
`grep -rnE 'os\.path\.join\([^)]*"agents"' core/scripts/ mind_api/ | grep -v __pycache__ | grep -v /tests/ | grep -v team-state`
FOURTH GREP (added 2026-08-25, g-115-4290) — the `os.path.join(<root>, "agents", ...)`
CALL SHAPE. It defines no constant (grep 1 misses), writes no `agents/*` glob (grep 2
misses) and uses no `.parent` (grep 3 misses), so it passed the whole audit while being
exactly as breakable — the guard-1802 class (an audit predicate narrower than the
population it claims to cover, reporting clean forever). Measured 2026-08-25: all three
greps returned 0 against the three files carrying it while firing 57/121/25 tree-wide, so
the zeros were genuine misses and not broken greps. FIVE sites existed (the filing goal
named four; `handoff_currency.py` was found by the new grep itself) and ALL FIVE are now
constant-routed, so this grep returns empty — re-run it against `git show HEAD~1` copies
if you need to see it fire. Two of the five were `.sh` files, which is why the grep is not
restricted to `*.py`. The `team-state` filter drops `world/team-state/agents/<agent>.yaml`
shard paths, a DIFFERENT `agents` directory that is not this constant.
Automated twin: the `/verify-learning` `hardcoded-APD-join-literal` check (registry
section 4T) runs this same predicate on cadence. Its sibling `hardcoded-APD-literal`
cannot see this shape — that check's predicate is `PROJECT_ROOT / "agents"` (pathlib,
UPPERCASE variable) over `core/scripts/*.py mind_api/src/*.py` only, so it also misses
lowercase/other-variable pathlib forms, every `.sh` file, and the non-recursive glob's
`gates/` `checks/` `audit_helpers/` subdirs.

Third-grep triage: a single `.parent` of `PROJECT_ROOT/agents/<agent>` yields
the *agents-parent* dir (correct for sibling enumeration — goal-selector.py
`collect_cross_agent_candidates`), but treating that `.parent` result AS
PROJECT_ROOT, or joining it with `core`/`config`, is the g-115-1279 bug class
(budget-meter `read_config` 404'd the config and pinned `cap_ms` to the 9000ms
fallback vs the configured 90000ms). The fix forwards `$PROJECT_ROOT` from
`_paths.sh` (SSOT); a `.parent.parent` fallback matches the current `agents/`
layout.

**Plus cross-agent glob consumers** sweep one file across ALL agent dirs via
`agents_root().glob("*/...")` (CLI) or `ctx.paths.agents_root.glob("*/...")`
(daemon). When correctly routed they auto-track an `AGENTS_PARENT_DIR` rename
(need NO edit), but they are invisible to all THREE greps above — they neither
define the constant, write a literal `agents/*`, nor use `.parent` — so a
depth-1 redrift (`PROJECT_ROOT.glob("*/...")`, which matches NOTHING
post-relocation) escapes every audit grep.

**This table was their only audit surface until 2026-09-06, and it had silently
fallen 19 consumers behind — a hand-maintained list standing in for an invariant
the language cannot see.** The audit surface is now
`core/scripts/cross-agent-glob-audit.py`, which DERIVES the consumer set from the
code (AST, so a comment warning about the drift pattern is not mistaken for the
pattern) and fails when code and record disagree. Run it on rename, and after
adding any cross-agent glob:

```
py -3 core/scripts/cross-agent-glob-audit.py          # exit 1 on drift/undocumented
py -3 core/scripts/cross-agent-glob-audit.py --write  # regenerate the derived block below
```

The rows below stay hand-written because each is an INCIDENT RECORD — which
drift zeroed which source, for how long, what guards it now. That prose is the
value and no generator can produce it. The machine half (the file SET, which is
what actually drifted) lives in the generated block at the end of this file.
`test_cross_agent_glob_audit.py::test_live_repo_is_clean` fails the suite if a
new depth-1 consumer appears, so the invariant no longer depends on someone
remembering to check a table:

| File(s) | Glob | Status |
|---------|------|--------|
| `core/scripts/skill-discovery.py` + `mind_api/src/endpoints/skill_discovery.py` | `*/journal.jsonl` invocation source (the diary source no longer globs — see below) | ✓ routed (`agents_root()` / `ctx.paths.agents_root`). Were depth-1 until g-115-1405 — the drift silently zeroed 2 of 4 invocation sources for EVERY skill, inflating `silently_undertriggering`. **The `*/session/execution-diary.jsonl` half was RETIRED as a glob 2026-07-31 (g-115-4154)**: correct `agents_root()` routing fixes only WHERE to look, and the diary is `sync_tier: continuity`, so the local tree is a per-agent read-through cache and a glob enumerates by what this box happens to hold. Measured cc-02: 1 of 5 agents cold; and once warm, 5 of 5 enumerated with **4 of 5 diverged from S3** — full coverage reading stale bytes. Both files now enumerate a roster and backend-read each diary via `core/scripts/_fleet_diary.py`. The daemon's prior `ensure_local()` fixed only the READ half — it can run solely on paths the glob already found, which is why the read fix could not supply enumeration. Regression-guarded by the `/verify-learning` glob-routing check (re-anchored to non-comment lines per guard-1099 — the unanchored form counted the new *comments* quoting the deleted glob as live code and reported PASS) + `test_fleet_diary.py` + `test_skill_discovery*.py` byte-compat. |
| `core/scripts/_paths.py`, `core/scripts/utilization-stats.py`, `mind_api/src/agent_paths.py` | `*/local-paths.conf` enumeration | ✓ routed (helper / imported `agents_root`) — the reference pattern to copy for any new cross-agent glob. |
| `core/scripts/skill-coinvocation-discovery.py` | `*/skill-invocations.jsonl` ledger mining (co-invocation candidates) | ✓ routed (`read_ledger` base defaults to `agents_root()`; the `root=` param is a test-only override). Regression-guarded by the `/verify-learning` `skill-coinvocation-glob-routing` check + the `--apply` RMW tests in `test_skill_coinvocation_discovery.py` (g-304-24). |
| `core/scripts/evolution-git-sweep.py` | `agent_self` git pathspecs (`*/self.md` + `agents/*/self.md`) + both-era path classifier | ✓ routed (second pathspec + classifier depth derive from `_agents_root()`; the legacy 2-part form is kept deliberately — git HISTORY contains pre-relocation commits). Was depth-1-only until 2026-07-11 (commit 973f9d52) — the drift zeroed `agent_self` backfill for ~6 weeks while LIVE D1 capture was also silent, leaving self.md revisions with no stream entries at all; 87-entry backfill applied on fix. |
| `mind_api/src/endpoints/utilization.py` (~L280) | `*/local-paths.conf` enumeration | ✓ routed (`ctx.paths.agents_root`, fixed 2026-07-11 — was the table's last ⚠ LATENT `project_root / "agents"` hardcode, surfaced by the g-115-1405 audit). |
| `core/scripts/gates/goal_duplication.py` (`_check_pending_queue`) | `*/aspirations.jsonl` per-agent pending-queue scan | ✓ routed (`_agents_root()`, fixed 2026-07-17 g-115-2461 — was a `project_root / "agents"` hardcode). `MIND_AGENTS_ROOT` env override exists for TEST hermeticity only (before it, tmp-world gate tests silently depended on live agent queues for their IDF corpus — every structural case scored 0.0 once hermetic). Regression-guarded by `test_goal_duplication_gate_pending_queue.py` P16 (two-root proof). |
| `core/scripts/pending-questions-read.sh` (`--all-agents`) | `*/session/pending-questions.yaml` fleet read | ✓ routed (bash `agents_root()` from `_paths.sh`, passed to the python heredoc as `AGENTS_ROOT`; added 2026-07-25 g-115-3074). Fleet mode is the READ half of the /open-questions fleet-visibility fix — a depth-1 regression here would silently return only the bound agent, which is exactly the pre-fix defect (21 of 31 fleet questions invisible to the user). |
| `core/scripts/pending-questions-sweep.py` (`--all-agents`) | `*/session/pending-questions.yaml` fleet sweep | ✓ routed (`root = Path(agents_root())`; the `--all-agents` fleet form, g-115-9715 — the single-file default under-reported the fleet corpus by N-1 agents while reporting a clean zero). This record sat inside the generated block below until `--write` erased it; it lives here because that block keeps only the file SET. |
| `core/scripts/owncloud-pull.sh` (`--all-agents`, `_fleet_roster` fallback) | agent-dir enumeration under `agents_root()` | ✓ routed (bash `agents_root()`; added 2026-07-25 g-115-3074). PRIMARY roster is team-state `agent_status` (the live fleet roster) — the glob is only the fallback. Deliberately NOT `*/local-paths.conf`: on any given box only the RESIDENT agent has a conf (cc-04 has one for alpha alone), so conf-enumeration would silently degrade fleet mode to single-agent — the very defect being fixed. |
| `core/scripts/experience-orphan-ratchet.py` | agent-dir enumeration + per-agent `experience/*.md` vs `experience.jsonl` / `experience-archive.jsonl` join | ✓ routed (`agents_root()`; added 2026-07-29 g-115-3796). A depth-1 redrift makes `_compute_orphans` scan ZERO agents, which the script reports as verdict `skipped` — deliberately NOT a 0-orphan PASS, because a vacuous zero here would read as "no drift" forever (rb-245). Joins on `content_path` BASENAME, so it is correct across BOTH layout eras: 845 of 3621 live rows still carry the pre-relocation shape (agent name as the FIRST path segment, with no `agents/` parent), and any future audit of `content_path` SHAPE must handle both or it will misreport those 845 as cross-agent (measured 2026-07-29 — an audit probe did exactly that before being corrected). Note this row deliberately describes that legacy shape in prose rather than writing it literally: the bare form trips the Phase-2.6 `BARE_AGENT_PREFIX_REGRESSION` pre-commit gate, whose documented `legacy:`-backtick escape hatch is not yet implemented (g-115-3880). |
| `core/scripts/learning-routing-audit.py` (`load_all_experiences`) + `core/scripts/learning-routing-repair.py` (`_resolve_store_path`) | `*/experience.jsonl` + `*/experience-archive.jsonl` corpus load / record→file resolution | ✓ routed (`agents_root()`; fixed 2026-08-10 g-115-5646 — BOTH were depth-1 and loaded/matched ZERO records). **The highest-consequence row in this table: the reader feeds a WRITER that fires automatically.** `tree.py::_post_remove_sweep_dangling` runs the repair with `--apply` after every tree-node removal and it NULLS whatever the audit calls dangling, so a depth-1 redrift here is not an under-report, it is data loss — **the pair is why this row is one row.** Measured: the depth-1 form returned **0** records, so every ref pointing INTO the experience store dangled by construction; the corpus went 0 → 4,873 on the fix and pipeline-source dangling went 319 → 13 (**305 of 319, 95.6%, were false positives**). Over 13 days **17,466** fields were nulled, **16,541 (94.7%) of them valid** — old values recoverable from `world/.history/learning-routing-repair-YYYY-MM-DD.jsonl`. The live read was archive-blind too (36.2% of the corpus has aged into `experience-archive.jsonl`), so both files are globbed. **The audit's TOTAL rose 319 → 779 on the fix, and that number must NOT be acted on.** Loading the corpus exposed a previously-unreadable SOURCE axis (766 refs *from* experience records), ~645 of them false positives from two audit defects this fix did NOT touch: (a) `tree_nodes_related` refs are slash-PATHS while `load_tree_node_keys` returns bare LEAF names — re-measured 2026-08-10, **495 of 577** resolve by leaf once a `.md` suffix is stripped (456 without the strip), and leaf names are unique across all 1364 nodes so the rewrite is deterministic — a key-format mismatch (rb-245 class); (b) `load_pipeline` reads only `pipeline.jsonl` while `pipeline-archive.jsonl` (986 records) exists — **191 of 195** `hypothesis_id` refs resolve there, the SAME archive-blindness fixed here for experience, still live on the pipeline axis. Genuine residue is ~86, not 779. `learning-routing-ratchet.py` prints `REGRESSED` and recommends `learning-routing-repair.sh --apply`, which would null all 779; do not re-baseline to 779 either, since that blesses the false positives as acceptable. THREE structural defenses now stand behind the routing, because routing alone is the fragile half: (a) `world_owns_agent_corpus()` — `agents_root()` is PROJECT_ROOT-based so NO world override moves it, and against a fixture world the real corpus is foreign; an EMPTY fixture produced **2,739** dangling across **12 real agent files**, all valid, bounded only by a 30s timeout expiring mid-READ; (b) an unloadable corpus makes refs INTO the experience store **unevaluable and SKIPPED**, never dangling — the general fix, since what turned 0 records into 17,466 nulls was an empty id-set silently licensing mass invalidation; (c) **g-115-5659** — `repair_file` resolves `merge_handler_for(path)` per PATH at the write and REFUSES any write-class-(b) store (no merge handler), which is every per-agent experience file. That closes the direction the other two cannot: when the audit IS wrong, a class-(a) store self-heals (8 runs each nulled ~315 `pipeline.experience_ref` and the count never moved, because the handler restored them — the destruction was invisible *because* the repair worked) while a class-(b) store does not, and the first live run after the glob fix nulled 772 experience fields of which 686 were never dangling. Regression-guarded by `test_learning_routing_glob_routing.py` (pins a NONZERO corpus — a zero is the silent-failure signature that let this ship), `test_learning_routing_world_scope.py` (3 cases, all proven to fail pre-fix; asserts on resolved PATHS, not the dangling count), and `test_learning_routing_write_class_gate.py` (11 pins, each verified RED by mutation). |
| `core/scripts/_frontier.py` (`load_goal_index`, `count_bodies`) — consumed by `agent-watchdog.py` DependencyFunnelProbe and `frontier-check.py` | `*/aspirations.jsonl` + `*/aspirations-archive.jsonl` (global `blocked_by` resolution) and `*/sessions/*/body-manifest.yaml` (Body census) | ✓ routed — takes the agents root as an ARGUMENT; both callers pass `agents_root()`, tests pass a tmp root (hermetic by construction, added 2026-08-29). A depth-1 redrift here would make every agent-queue goal an UNKNOWN blocker (the census reports those as `unknown_blockers` pairs and the probe emits `dependency_funnel_unresolvable`, never a clean frontier), and zero the Body census — deliberately loud, not a vacuous zero. |
| `core/scripts/tests/run-invisible-suites.sh` (bound-agent resolution) | `*/session/running-session-id` + `*/local-paths.conf` enumeration | ✓ routed (bash `agents_root()` via `AGENTS_ROOT_DIR="${MIND_AGENTS_ROOT:-$(agents_root)}"`; added 2026-07-30 g-115-4141). Resolves MIND_AGENT for unbound launches (backgrounded/cron/CI — no hook injection): env → sole rsid → sole conf; unresolvable → the invisible half SKIPs loudly (exit 0) instead of dispatching unbound and manufacturing env-shaped reds. `MIND_AGENTS_ROOT` is a TEST-hermeticity seam only. Regression-guarded by `test-invisible-suites-agent-resolution.sh` (7 cases via `--resolve-only`, incl. ambiguity-no-guess and the loud-SKIP path), which itself auto-joins the runner's shell population. |
Helper API (available after sourcing `_paths.sh` or importing from `_paths`):
- `agents_root()` — parent directory containing all agent dirs
- `agent_dir(name)` — full path to a named agent's directory
- `agent_sessions_root(name)` — parent dir for per-session dirs (Phase 2.6)
- `agent_session_dir(name, sid)` — one per-session dir (Phase 2.6)
- `agent_state_dir(name)` — agent-wide cross-session state dir (Phase 2.6)
- `enumerate_agent_confs()` — sorted list of `*/local-paths.conf` paths (Python only)
- `is_under_agent_dir(p)` — whether a path is inside an agent directory (Python only)

**Rule**: Never write `PROJECT_ROOT / agent_name` or `$PROJECT_ROOT/$AGENT`
directly. Use `agent_dir(name)` or `$(agent_dir "$AGENT")` instead. For
per-session paths use `agent_session_dir(name, sid)`.

<!-- BEGIN GENERATED cross-agent-glob-consumers -->
<!-- Derived from code by core/scripts/cross-agent-glob-audit.py.
     DO NOT HAND-EDIT: regenerate with
       py -3 core/scripts/cross-agent-glob-audit.py --write
     The PROSE table above carries the incident history and IS
     hand-maintained; this block carries only the file SET, which
     is what silently drifted (19 consumers missing, 2026-09-06). -->

Derived cross-agent glob consumers (38 sites, 30 files):

- `core/scripts/_frontier.py:190` — `root.glob('*/sessions/*/body-manifest.yaml')`
- `core/scripts/_paths.py:56` — `agents_root().glob('*/local-paths.conf')`
- `core/scripts/_paths.py:354` — `agents_root().glob('*/local-paths.conf')`
- `core/scripts/_seed_engine.py:879` — `agents_dir.glob('*/local-paths.conf')`
- `core/scripts/_seed_engine.py:913` — `agents_dir.glob('*/local-paths.conf')`
- `core/scripts/checks/temp_durability_invariant.py:165` — `Path(agents_root()).glob('*/temp/*')`
- `core/scripts/claim_artifact_sweep.py:297` — `agents_root().glob('*/aspirations.jsonl')`
- `core/scripts/counted-close-revert-census.py:221` — `root.glob('*/session/working-memory.yaml')`
- `core/scripts/counted-close-revert-census.py:223` — `root.glob('*/sessions/*/working-memory.yaml')`
- `core/scripts/defer-scope-coverage.py:169` — `agents_root().glob('*/')`
- `core/scripts/durability-property-check.py:203` — `root.glob('*/temp')`
- `core/scripts/gates/defer_target_existence.py:104` — `r.glob('*/aspirations.jsonl')`
- `core/scripts/gates/defer_target_existence.py:105` — `r.glob('*/aspirations-archive.jsonl')`
- `core/scripts/housekeeping-tick.py:356` — `Path(ar()).glob('*/experience.jsonl')`
- `core/scripts/human-blocked-defer-join.py:168` — `agents_root().glob('*/session/pending-questions.yaml')`
- `core/scripts/inbound-reference-census.py:218` — `agents_root().glob('*/local-paths.conf')`
- `core/scripts/learning-routing-repair.py:125` — `agents_root().glob('*/experience.jsonl')`
- `core/scripts/learning-routing-repair.py:126` — `agents_root().glob('*/experience-archive.jsonl')`
- `core/scripts/pending-questions-sweep.py:828` — `root.glob('*/session/pending-questions.yaml')`
- `core/scripts/predicate.py:384` — `_agents_root().glob('*/aspirations.jsonl')`
- `core/scripts/repo-hygiene-sweep.py:204` — `Path(agents_root()).glob('*/aspirations.jsonl')`
- `core/scripts/skill-analytics.py:350` — `agents_root().glob('*/skill-invocations.jsonl')`
- `core/scripts/skill-coinvocation-discovery.py:130` — `base.glob('*/skill-invocations.jsonl')`
- `core/scripts/skill-discovery.py:225` — `agents_root().glob('*/skill-invocations.jsonl')`
- `core/scripts/skill-discovery.py:275` — `agents_root().glob('*/journal.jsonl')`
- `core/scripts/skill-freshness-report.py:149` — `base.glob('*/skill-invocations.jsonl')`
- `core/scripts/skill-latency-report.py:111` — `root.glob('*/local-paths.conf')`
- `core/scripts/skill-retire-candidates.py:158` — `agents_root().glob('*/skill-invocations.jsonl')`
- `core/scripts/team-contribution-report.py:241` — `Path(agents_root).glob('*/aspirations.jsonl')`
- `core/scripts/utilization-stats.py:485` — `_agents_root().glob('*/local-paths.conf')`
- `core/scripts/worker_stall.py:761` — `agents_root.glob('*/session')`
- `mind_api/src/__main__.py:462` — `resolver._agents_root().glob('*/local-paths.conf')`
- `mind_api/src/agent_paths.py:286` — `self._agents_root().glob('*/local-paths.conf')`
- `mind_api/src/agent_paths.py:311` — `self._agents_root().glob('*/local-paths.conf')`
- `mind_api/src/endpoints/skill_analytics.py:398` — `agents_root.glob('*/skill-invocations.jsonl')`
- `mind_api/src/endpoints/skill_discovery.py:165` — `ctx.paths.agents_root.glob('*/journal.jsonl')`
- `mind_api/src/endpoints/skill_discovery.py:206` — `ctx.paths.agents_root.glob('*/skill-invocations.jsonl')`
- `mind_api/src/endpoints/utilization.py:375` — `agents_root.glob('*/local-paths.conf')`

<!-- END GENERATED cross-agent-glob-consumers -->

