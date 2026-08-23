# Layer 1 / Layer 2 Boundary

This repo currently contains both client-side agent framework code (Layer 1) and
server-side daemon code (Layer 2) in one tree. This document names which subdirs
belong to which layer so a future repo split is straightforward.

## Layer 1 — Client / Agent Framework (future OSS)

- `core/scripts/*.sh` — shell wrappers (client side of the HTTP boundary)
- `core/scripts/_runtime.sh` — HTTP client library for daemon communication
- `core/scripts/_paths.sh`, `core/scripts/_platform.sh` — client-side path and platform helpers
- `.claude/skills/` — agent skill definitions
- `.claude/rules/` — agent behavioral rules
- `core/config/conventions/` — agent conventions
- `core/config/modes/` — agent operational modes

## Layer 2 — Service / Daemon (future proprietary)

- `mind_api/src/` — the daemon server, HTTP routes, endpoint handlers
- `core/scripts/gates/` — gate modules (invoked by the daemon, not by shell wrappers)
- `core/scripts/_fileops.py` — server-side file operations (locking, history, changelog)
- `core/scripts/_override_helpers.py` — server-side audit helpers for gate overrides

## Mixed / TBD

- `core/scripts/*.py` — mixed by design since the 2026-05-14 daemon cutover
  (the "will be split during/after cutover" note that used to sit here went
  stale for three months; measured state, selection-stack review 2026-08-21):
  - **Library modules** imported by both layers (`aspirations.py` validators/
    constants, `_agents.py`, `_goal_fields.py`, `predicate.py`, …).
  - **Python→Python CLI lane** — `aspirations.py` retains exactly FOUR argparse
    subcommands, each deliberately alive (the 27 dead ones were deleted in the
    cutover's over-deletion sweep, HARDENING.md S1–S4): `update-goal` and
    `update-asp-field` (called by sweep scripts: recurring-precondition-sweep,
    monitor-stale-check, credential-defer-recheck, chronic-friction-aggregator),
    `recompute-all-progress` (bootstrap — init-agent.sh runs it before any
    daemon exists; also a daemon endpoint), and `evolution-append` (its only
    caller is test_runtime_batch4_write's byte-compat parity harness, which
    runs the CLI as the reference implementation against the daemon mirror —
    delete the CLI and the daemon loses its fidelity pin).
  - Known coherence debt: `update-goal` therefore has TWO live paths whose
    validation sets differ (the CLI runs structured-prefix/cascade extras the
    daemon omits; the daemon runs the add-site parity gates). Tracked as a
    world Idea goal filed by the 2026-08-21 selection-stack review.
- `core/config/*.yaml` — config files (some consumed client-side, some server-side)

## How to extend

New files MUST be placed under the correct layer's subdir from day one:
- Shell wrappers and agent-facing helpers go in `core/scripts/` (Layer 1).
- Daemon endpoints and server-side logic go in `mind_api/src/` (Layer 2).
- Cross-layer imports are smells — a Layer 1 module should never import from
  `mind_api/src/`, and a Layer 2 endpoint should never source a `.sh` wrapper.

## Gate: Layer-1 -> Layer-2 import count == 0 (sec15 Phase-6 invariant)

`core/scripts/layer1-no-runtime-imports-gate.py` enforces the boundary by
AST-scanning all Layer-1 Python files (`core/scripts/*.py` excluding the
explicit Layer-2 list above + `.claude/{skills,rules}/**/*.py`) for any
`import mind_api.src` or `from mind_api.src import` statement, AND
regex-scanning all Layer-1 shell wrappers (`core/scripts/*.sh`) for any
`source ... mind_api/src/...` or `python -c '...mind_api.src...'` pattern.

The daemon launcher (`python -m mind_api.src` / `py -3 -m mind_api.src`)
is explicitly whitelisted — it is HOW Layer 1 starts the daemon, not how
Layer 1 depends on Layer 2 code.

Doc-strings, comments, and error-message strings that mention
`mind_api/src/` for human guidance ("See mind_api/src/endpoints/ for API
docs") are NOT violations — the gate only flags code references
(imports, sources, dynamic imports).

Run:
```
py -3 core/scripts/layer1-no-runtime-imports-gate.py
```

Exit 0 = invariant holds. Exit 1 = violated (prints offending
file:line:reference). Wire into the per-phase gate alongside the
Phase-5 meta->world gate (`core/scripts/meta-imports-world-gate.py`).
