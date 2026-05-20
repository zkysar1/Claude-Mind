# mind_api/src — Internal Interface (Phase 5: world/meta split)

## The three layers

The `mind_api/src` daemon (`python -m mind_api.src`) is organized into
three layers, in increasing domain-specificity:

```
mind_api/src/
  <shared-infra substrate at root>   # layer 1 — domain-agnostic plumbing
  endpoints/                         # layer 2 — agent + MIXED endpoints
  world/                             # layer 3a — world-service module
  meta/                              # layer 3b — meta-service module
```

### Layer 1 — Shared-infra substrate (root)

Modules: `agent_paths.py`, `changelog.py`, `file_locks.py`, `history.py`,
`jsonl_cache.py`, `yaml_cache.py`, `lifecycle.py`, `server.py`,
`stats.py`, `store_registry.py`, `__main__.py`, `__init__.py`.

Domain-agnostic plumbing. Does NOT depend on any endpoint or domain
package. Provides: per-request context (`ctx.paths`, `ctx.agent`),
filesystem locking + history + changelog, JSONL/YAML caching, HTTP server
+ lifecycle, generic store-registry rows (one per JSONL store).

### Layer 2 — `endpoints/` (agent + MIXED + infra endpoints)

Modules: `health.py`, `admin.py`, `store.py`, `experience.py`,
`journal.py`, `wm.py`, `aspirations.py`, `aspirations_query.py`,
`aspirations_write.py`, `board.py`, `retrieve.py`, plus the shared
helper `_jsonl_common.py`.

- `health/admin/store` — domain-agnostic infra endpoints (registry-
  parameterized writes via `store.py`).
- `experience/journal/wm` — agent-domain endpoints (`ctx.paths.agent`).
- `aspirations*/board/retrieve` — MIXED endpoints (span
  world+agent[+meta]); MAY import world or meta facades, but NOT vice
  versa.

`endpoints/__init__.py:load_all()` is the SINGLE route-registration
orchestrator. It imports each endpoint module (from its current package)
and calls `register(routes)` on each. There is no parallel registration
path — `load_all()` is single source of truth.

### Layer 3a — `world/` (world-service module)

Modules: `reasoning_bank`, `pipeline`, `pipeline_write`,
`pattern_signatures`, `tree`, `tree_read`, `team_state`.

All operate on `ctx.paths.world`. Imports allowed:
- Layer 1 (shared substrate): `..jsonl_cache`, `..yaml_cache`,
  `.. import file_locks, history, changelog`, `..server`.
- Layer 2 helpers: `..endpoints._jsonl_common`.
- `core/scripts/_*.py` pure helpers via sys.path injection
  (`_rb_helpers`, `tree_match`, `_fileops`, `tree`).

NOT allowed: imports from `mind_api.src.meta` or any meta-domain symbol.

### Layer 3b — `meta/` (meta-service module)

Modules: `spark_questions` (today the only pure meta endpoint).

Operates on `ctx.paths.meta`. Imports allowed:
- Layer 1 (shared substrate): `..jsonl_cache`, `..server`.
- Layer 2 helpers: `..endpoints._jsonl_common`.

NOT allowed: imports from `mind_api.src.world` or any world-domain
endpoint module (`reasoning_bank`, `pipeline`, `pipeline_write`,
`pattern_signatures`, `tree`, `tree_read`, `team_state`).

## The Phase-5 invariant (sec15 gate)

**meta -> world import count == 0.**

Enforced by `core/scripts/meta-imports-world-gate.py` — AST-scans
`mind_api/src/meta/**/*.py` and exits 1 on any import of
`mind_api.src.world` or any world-domain endpoint module name (absolute
or relative form). Runs as part of the per-phase gate after every
`mind_api/src/` change.

The world-service may freely depend on shared infra. The meta-service
may freely depend on shared infra. Neither may depend on the other.
MIXED endpoints (in `endpoints/`) may import either, but they are not
"the meta module" or "the world module" — they are the composition
layer, and the invariant is about the meta package specifically.

## H9-light: Tenant seam

`agent_paths.py` exposes a thin `Tenant = (world_path, meta_path)` value
object. Today, the tenant defaults to local-paths.conf resolution (same
behavior `AgentPaths` already provides). The R4 `X-Mind-Tenant` HTTP
header is accepted by `server.py` (presently unused) and is wired to
select the (world_path, meta_path) pair when present.

This is NOT multi-tenant runtime (that's H5, post-stop). It is the
abstraction seam that makes H5 possible without rewriting the world/
or meta/ packages: any consumer reads paths via `ctx.paths.{world,meta}`,
which the request handler populates from the resolved Tenant.

## Why this structure (first-principles)

The world/meta boundary already exists in DATA:
`base=ctx.paths.{world,meta}` per `store_registry.py` row, and each
endpoint accesses exactly one of `ctx.paths.{world, meta, agent}`. Phase
5 makes the MODULE structure mirror that data boundary that was
ALREADY there + adds the gate that keeps it true + the Tenant seam for
H5.

The split is therefore a `git mv` + minimal import-fixup (5 single-dot
`._jsonl_common` imports re-targeted + 8 route-source lines in
`endpoints/__init__.py`), NOT a rewrite. Risk is contained to import
paths (mechanical, smoke-catchable), not behavior. The HTTP route table
is unchanged: same handlers, same URL paths, just imported from
different modules.
