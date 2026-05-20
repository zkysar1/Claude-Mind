# framework-runtime

A long-running localhost HTTP daemon that pre-imports the framework once
and exposes endpoint-shaped versions of `core/scripts/*.py` so each call
collapses from ~5000ms (bash + python launcher + path resolution) to
~10ms server-side.

Domain-agnostic. Built for Phase 2 of the runtime migration (see
`framework-runtime-phase2-handoff.txt` for the design brief).

## For the next developer

Phase A is done. Phase B (hot-path migration of ~30 more scripts plus the
writer-path machinery) is documented in detail in
[`../docs/development-history/PHASE_B_HANDOFF.md`](../docs/development-history/PHASE_B_HANDOFF.md)
— start there if you're picking this up. (The handoff doc was moved out
of core/ on 2026-05-18 per packaging plan Phase 3 to keep core/ free of
deployment-specific engineering history.)

## What's shipped

**Phase A** (initial prototype):
- `mind_api/src/` — daemon source: server, lifecycle, agent path
  resolution, JSONL mtime cache, health + aspirations/read endpoints.
- `core/scripts/_runtime.sh` — shell helper sourced by migrated wrappers.
- `core/scripts/aspirations-read.sh` — first migrated wrapper.

**Phase B PR 1** (reader trio):
- `mind_api/src/yaml_cache.py` — YAML-file mtime cache, mirrors jsonl_cache.
- `mind_api/src/endpoints/tree.py` — `GET /v1/tree/find-node` with
  concept-index caching (avoids `build_concept_index` rebuild per call).
- `mind_api/src/endpoints/wm.py` — `GET /v1/wm/read`.
- `core/scripts/tree-find-node.sh`, `core/scripts/wm-read.sh` — rewritten.
- `core/scripts/_runtime.sh` adds `rt_url_encode` for safe query params.
- 12 new pytest tests under `mind_api/tests/test_runtime_{tree,wm}.py`.

**Phase B PR 2** (writer machinery):
- `mind_api/src/file_locks.py` — two-layer locking (threading + fcntl-equivalent).
- `mind_api/src/history.py` — `.history/` snapshots, mirrors `_fileops.save_history`.
- `mind_api/src/changelog.py` — changelog appends, mirrors `_fileops.append_changelog`.
- `mind_api/src/endpoints/aspirations_write.py` — POST add-goal / update-goal
  (machinery demonstration; SKIPS orchestration gates — wrappers stay on
  fallback path until gates are daemon-safe).
- 11 new pytest tests including 20-thread concurrency stress.

**Phase B PR 3** (observability):
- `mind_api/src/stats.py` — Algorithm-R reservoir sampling (1024 per endpoint).
- `mind_api/src/endpoints/admin.py` — `GET /v1/admin/stats`.
- Server `_access_log` wired to record latency on every request.
- 5 new pytest tests.

**Phase B PR 5** (retrieve orchestrator — read-only path):
- `mind_api/src/endpoints/retrieve.py` — `GET /v1/retrieve`. Imports
  `core/scripts/retrieve.py` once at daemon startup; per-request swaps
  nine path globals under a daemon-wide lock so concurrent requests for
  different agents cannot race. Installs two process-wide caches:
  `read_yaml` routes through `yaml_cache`, and `build_concept_index` is
  wrapped with an `id(nodes)`-keyed cache. The caches close retrieve's
  two dominant hot-path costs (~270KB YAML parse + ~94 .md file reads
  per call).
- Endpoint serves the read-only path ONLY (DECISIONS #24). The wrapper
  detects the absence of `--read-only` and falls through to direct
  python without ever attempting the daemon.
- `core/scripts/retrieve.sh` — skinny rewrite. Inline mode detection
  reads `<agent>/session/agent-mode` directly (skips the ~700ms
  `session-mode-get.sh` subprocess from the original wrapper).
- 16 new pytest tests under `mind_api/tests/test_runtime_retrieve.py`,
  including a concurrent-requests-don't-interleave test and a path-
  globals-restored-after-call test (the path-swap pattern's safety net).

**Phase B PR 4** (Tier A readers — 10 endpoints):
- `mind_api/src/endpoints/_jsonl_common.py` — shared response shapers
  (find_by_id, json_response_pretty, plain_lines, flag, missing_flag_error).
- `mind_api/src/endpoints/pipeline.py` — `GET /v1/pipeline/read` (9 flags).
- `mind_api/src/endpoints/reasoning_bank.py` — `GET /v1/rb/read` (7 flags) +
  `GET /v1/guard/read` (4 flags).
- `mind_api/src/endpoints/pattern_signatures.py` — `GET /v1/pattern-signatures/read`.
- `mind_api/src/endpoints/spark_questions.py` — `GET /v1/spark-questions/read`
  (lives under META_DIR, not WORLD_DIR).
- `mind_api/src/endpoints/experience.py` — `GET /v1/experience/read` (agent-local;
  `--validate` falls through to fallback).
- `mind_api/src/endpoints/journal.py` — `GET /v1/journal/read` (agent-local).
- `mind_api/src/endpoints/board.py` — `GET /v1/board/read` (channel-keyed,
  supports since/author/type/tag/last/json filters).
- `mind_api/src/endpoints/team_state.py` — `GET /v1/team-state/read` (yaml).
- `mind_api/src/endpoints/tree_read.py` — `GET /v1/tree/read` (simple lookups:
  --node, --path, --ancestors, --children, --leaves, --leaves-under, --stats,
  --child-path, --summary, --maintenance. Computational flags
  --decompose-candidates/--validate/--active-content stay on fallback).
- 10 wrappers rewritten: pipeline-, reasoning-bank-, guardrails-,
  pattern-signatures-, spark-questions-, experience-, journal-, board-,
  team-state-, tree-read.sh
- 37 new pytest tests under `mind_api/tests/test_runtime_pr4.py`.

**Phase B PR 6** (Tier C inline scripts + aspirations-query):
- `core/scripts/session-state-get.sh`, `session-mode-get.sh`,
  `session-signal-exists.sh` — inlined to pure bash. No daemon
  involvement. Each script reads one plain-text file and prints the
  result. Saves ~300ms python startup on hot-path callers (hooks,
  session-start protocol, every loop iteration). Mirror comments
  pin `VALID_SIGNALS` / `DEFAULT_MODE` to their python source of truth.
- `mind_api/src/endpoints/aspirations_query.py` — `GET /v1/aspirations/query`.
  Cross-queue goal filter, mirrors `aspirations.py query`. Three filters
  (goal_status, goal_field_name+value pair, title_contains) with AND
  semantics. `_log_goal_read` side-effect deferred to PR 7
  (DECISIONS #30).
- `core/scripts/aspirations-query.sh` — daemon-aware wrapper, same
  shape as PR 4 readers.
- `core/scripts/_runtime.sh` — added `rt_session_mode` helper (extracted
  from retrieve.sh's inline mode-read) and a top-of-file convention
  comment documenting the `"${2-}"` + safe-shift value-arg pattern for
  future wrapper authors.
- `core/scripts/retrieve.sh` — refactored to use `rt_session_mode`
  (DRY with session-mode-get.sh's inline read).
- 14 new pytest tests for the inline scripts
  (`mind_api/tests/test_runtime_tier_c.py`) + 13 for the query endpoint
  (`mind_api/tests/test_runtime_aspirations_query.py`). 120/120
  runtime tests passing.

Universal across both phases:
- `mind_api/bench/` — benchmark harness, p50/p95/p99 reports per scenario.
- Fallback contract: every wrapper falls back to direct python on
  daemon failure, byte-identical to the pre-migration script.

## Quick start

```bash
# Start the daemon manually (foreground; ctrl-c to stop)
python3 -m mind_api.src

# Or auto-start it via a migrated wrapper
MIND_AGENT=alpha bash core/scripts/aspirations-read.sh --summary

# Bench the speedup
bash mind_api/bench/run.sh

# Run the test suite
python3 -m pytest mind_api/tests/
```

## How a migrated wrapper looks

```bash
#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"
source "$CORE_ROOT/scripts/_runtime.sh"

# Parse args -> build query string -> rt_call
rc=0
rt_call GET /v1/aspirations/read --query "summary=1" || rc=$?

case $rc in
    0) exit 0;;
    2) exit 1;;
    3)
        # Daemon unreachable. Fall back to direct python.
        rt_warn "falling back to direct-python for aspirations-read"
        source "$CORE_ROOT/scripts/_paths.sh"
        source "$CORE_ROOT/scripts/_platform.sh"
        exec python3 "$CORE_ROOT/scripts/aspirations.py" read --summary;;
esac
```

The hot path skips `_paths.sh` and `_platform.sh` entirely (those alone
cost ~2 seconds on Windows). The fallback path sources them — it's the
slow case, but it's byte-identical to the original wrapper.

## Architecture at a glance

```
┌──────────────────────────────────┐
│  bash core/scripts/wrapper.sh    │   ~500ms bash startup floor
│                                  │
│  source _runtime.sh              │   ~10ms
│  rt_call GET /v1/x/y --query ... │   ~300ms curl + roundtrip
└────────────┬─────────────────────┘
             │   X-Mind-Agent: alpha
             ▼
┌──────────────────────────────────┐
│  framework-runtime daemon        │
│  127.0.0.1:<assigned port>       │
│  - ThreadingHTTPServer (stdlib)  │
│  - agent-aware path resolver     │
│  - mtime-keyed JSONL cache       │
│  - JSON-line access log          │
└──────────────────────────────────┘
```

### Auto-start contract

Every wrapper sources `_runtime.sh`. On the FIRST call of a session,
`rt_call` finds no daemon on the wired port, spawns it via
`python3 -m mind_api.src` in the background, polls up to 5 seconds for
the daemon to be ready, then retries. Subsequent calls find the daemon
warm and skip the spawn.

If the daemon never comes up (port collision, broken install,
crash-loop), `rt_call` returns exit code 3 and the wrapper falls back
to direct python. A warning is written to stderr so the user notices.

### Per-agent routing

The daemon is process-wide; multiple agents (alpha/bravo/zeta) share
one daemon and identify themselves via the `X-Mind-Agent` header on
every request. The `AgentPathResolver` parses each agent's
`local-paths.conf` once and caches the result so the resolver lookup
is in-memory thereafter.

### File layout

```
mind_api/src/
  __init__.py          version string
  __main__.py          entry point: `python -m mind_api.src`
  server.py            ThreadingHTTPServer + Response shape + RequestContext
  lifecycle.py         PID/port atomic writes, is_pid_alive, free-port pick
  agent_paths.py       parses local-paths.conf per agent (cached)
  jsonl_cache.py       mtime-keyed JSONL read cache
  yaml_cache.py        mtime-keyed YAML read cache (PR 1)
  file_locks.py        threading.Lock + fcntl-equivalent (PR 2)
  history.py           .history/ snapshots (PR 2)
  changelog.py         changelog appends (PR 2)
  stats.py             reservoir-sampled latency stats (PR 3)
  endpoints/
    __init__.py        loads all endpoint modules and builds the route table
    health.py          GET /v1/admin/health
    admin.py           GET /v1/admin/stats (PR 3)
    aspirations.py     GET /v1/aspirations/read
    aspirations_write.py  POST add-goal, POST update-goal (PR 2)
    tree.py            GET /v1/tree/find-node (PR 1)
    wm.py              GET /v1/wm/read (PR 1)
    pipeline.py        GET /v1/pipeline/read (PR 4)
    reasoning_bank.py  GET /v1/rb/read + /v1/guard/read (PR 4)
    pattern_signatures.py GET /v1/pattern-signatures/read (PR 4)
    spark_questions.py GET /v1/spark-questions/read (PR 4)
    experience.py      GET /v1/experience/read (PR 4)
    journal.py         GET /v1/journal/read (PR 4)
    board.py           GET /v1/board/read (PR 4)
    team_state.py      GET /v1/team-state/read (PR 4)
    tree_read.py       GET /v1/tree/read (PR 4)
    retrieve.py        GET /v1/retrieve (PR 5, read-only only)
    _jsonl_common.py   shared response shapers (PR 4)
  README.md            this file
  (engineering-history docs moved to mind_api/docs/development-history/ — Phase 3 of packaging plan)

mind_api/state/              created at first daemon start; gitignored
  daemon.pid           process id of the running daemon
  daemon.port          TCP port the daemon is listening on
  daemon.log           daemon lifecycle events (JSON lines)
  access.log           per-request JSON line
  spawn.log            shell-side auto-start log
```

## What's NOT here (yet)

- **Writer wrappers staying on fallback**: aspirations-add-goal.sh and
  aspirations-update-goal.sh still source `_paths.sh` and exec python.
  Migration requires daemonizing the orchestration gates (origin-signal,
  capability, duplication, work_class, category-suggest). Endpoint
  machinery exists (`aspirations_write.py`) but skips the gates.
- **Tree computational flags**: --validate, --decompose-candidates,
  --redistribute-candidates, --distill-candidates, --active-content all
  fall through to fallback. Each reads node .md files or runs cross-record
  scans that aren't yet daemon-safe.
- **Retrieve counter-bump path**: PR 5 shipped the read-only path. The
  autonomous-mode call without `--read-only` still falls through to direct
  python because counter bumps go through `_locked_bump_jsonl` and a
  `retrieval-session.json` write under AGENT_DIR — daemonising those needs
  the writer machinery generalised beyond aspirations. See DECISIONS #24.
- **Tier C tiny readers**: session-state-get, session-mode-get, etc. —
  reads tiny files but called dozens of times per iteration. Bash startup
  is 100% of the cost.
- **Hot reload of edited core scripts** (Decision 8) — deferred to Phase C.

## Operations

- Daemon logs lifecycle events to `mind_api/state/daemon.log` and per-request
  events to `mind_api/state/access.log`. Both are JSON-lines.
- Send SIGTERM (or `taskkill //F //PID $(cat mind_api/state/daemon.pid)` on
  Windows) to shut the daemon down. The PID/port files are removed on
  graceful exit; if you `kill -9`, they stay around and the next
  `python -m mind_api.src` cleans them up.
- The daemon refuses to start if another is already alive (PID present
  AND `os.kill(pid, 0)` succeeds AND the port responds to /health).

## Performance (measured on the target machine)

`mind_api/bench/run.sh` against 30-iteration runs (1 warmup discarded):

| Scenario              | before p50 | after p50 | speedup |
|-----------------------|-----------:|----------:|--------:|
| aspirations-summary   | 3712 ms    | 808 ms    | 4.6x    |
| aspirations-active-compact | 4209 ms | 861 ms | 4.9x    |
| aspirations-id        | 3649 ms    | 702 ms    | 5.2x    |
| tree-find-node (PR 1) | 6314 ms    | 497 ms    | 12.7x   |
| wm-read-slot (PR 1)   | 1844 ms    | 424 ms    | 4.4x    |
| rb-recent (PR 4)      | 2076 ms    | 459 ms    | 4.5x    |
| guard-active (PR 4)   | 2024 ms    | 757 ms    | 2.7x    |
| team-state-field (PR 4) | 1949 ms  | 439 ms    | 4.4x    |
| retrieve-shallow (PR 5) | 10692 ms | 871 ms    | 12.3x   |
| retrieve-deep (PR 5)  | 9885 ms    | 1175 ms   | 8.4x    |
| retrieve-supplementary (PR 5) | 2721 ms | 854 ms | 3.2x  |

The remaining ~700 ms on the after-path is bash startup (~500 ms) +
curl startup (~300 ms) — Windows process-spawn cost that no in-bash
optimization can remove. The handoff's "<100 ms" target is achievable
only by removing bash from the LLM's call path (Phase D or beyond).
For Phase A, a 4-5x speedup on every call is the win.
