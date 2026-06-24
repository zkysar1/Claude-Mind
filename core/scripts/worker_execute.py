#!/usr/bin/env python3
"""worker_execute.py -- Phase 2A: worker-body simplified execution contract.

Mind/Body convergence (asp-306, g-306-69). A WORKER Body of a Mind runs a
SIMPLIFIED per-Body loop: select -> claim -> execute, then STOPS. It SKIPS the
reducer-only phases (verify / encode / reflect / state-update / learning-gate /
evolution / complete-review / productivity-check) -- those are the SINGLE
reducer's responsibility, applied later to all Bodies' MERGED state at
generalize-down (Phase 1C body-merge.py, run from aspirations-consolidate
Step -1). Running encode/reflect per-worker is the "N reducers = a defect"
invariant the convergence forbids.

This module is the DETERMINISTIC, TESTABLE contract the worker-loop skill
(.claude/skills/worker-loop/SKILL.md) consults:
  - WORKER_PHASES / REDUCER_ONLY_PHASES -- the phase split.
  - worker_should_run_phase(phase)      -- the phase gate (a worker runs
    select/claim/execute, skips the reducer-only phases).
  - worker_wm_path(agent, unit_key)     -- the worker writes ONLY its own forked
    Body WM, reusing the Phase-1A reducer-aware activation signal (the forked
    body-WM-file's existence). Mirrors mind_api/src/agent_paths.py::wm_path so
    the CLI worker and the daemon agree on a Body's WM target.

Backward-compat: with one Body (the reducer), or no unit_key, worker_wm_path
collapses to the agent-wide WM -- inert until a 2nd Body forks (the same
dormant-until-2nd-body property as every Phase-1 plumbing tier). select + claim
themselves are NOT reimplemented here -- the worker-loop skill REUSES the
existing goal-selector.sh + aspirations-claim.sh (claimed_by stays the
mindKey/agent-name); this module owns only the phase split + WM target that are
NEW to the worker path.

Design SSOT: world/knowledge/tree/intelligence/ayoai-architecture/
universal-environment-abstraction/mind-engine-identity-bridge.md (Phase 2).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# _paths is the SSOT for the agent-dir layout constants. Importing them (rather
# than hardcoding the "agents"/"sessions"/"session" segments) makes
# worker_wm_path auto-track an AGENTS_PARENT_DIR / SESSIONS_DIRNAME rename, per
# CLAUDE.md "Agent-dir Resolution". Only PROJECT_ROOT, AGENT_NAME, and the three
# layout constants are imported -- NOT AGENT_DIR (which is None when MIND_AGENT
# is unset, e.g. under pytest), so this import is safe in every context.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from _paths import (  # noqa: E402
    PROJECT_ROOT,
    AGENT_NAME,
    AGENTS_PARENT_DIR,
    SESSIONS_DIRNAME,
    SESSION_DIRNAME,
)

# --------------------------- phase contract ---------------------------

# The phases a WORKER Body runs -- the simplified per-Body path (design: "a
# simplified per-Body select->claim->execute that SKIPS verify/encode/reflect").
WORKER_PHASES = ("select", "claim", "execute")

# The phases a worker SKIPS -- reducer-only (encode / reflect / consolidate). The
# single reducer applies these to the MERGED state of ALL Bodies at
# generalize-down, NOT per-Body. Names match the aspirations-loop-digest phase
# labels so the worker-loop skill can gate by the same identifiers the full loop
# uses.
REDUCER_ONLY_PHASES = frozenset({
    "verify",             # Phase 5   -- outcome verification
    "spark",              # Phase 6   -- immediate learning / encoding
    "complete-review",    # Phase 7   -- aspiration completion review
    "state-update",       # Phase 8   -- tree encoding / journal / state
    "evolution",          # Phase 9   -- strategy evolution
    "learning-gate",      # Phase 9.5 -- reflection / learning audit
    "productivity-check",  # Phase 12  -- productivity gate
})


def worker_should_run_phase(phase: str) -> bool:
    """True iff a worker Body runs `phase` itself.

    A worker runs EXACTLY its three simplified phases (select/claim/execute) and
    skips every reducer-only phase. Any phase outside WORKER_PHASES -- including
    the reducer-only set AND any unrecognised phase -- returns False
    (conservative: a worker never runs a phase not explicitly granted to it).
    """
    return phase in WORKER_PHASES


# --------------------------- WM routing ---------------------------

def _agents_root(project_root: Path) -> Path:
    return (project_root / AGENTS_PARENT_DIR) if AGENTS_PARENT_DIR else project_root


def worker_wm_path(agent: str, unit_key: "str | None" = None,
                   project_root: "Path | None" = None) -> Path:
    """The working-memory path a WORKER Body writes -- reducer-aware per-Body.

    Mirrors mind_api/src/agent_paths.py::wm_path: routes to the forked Body WM
    (`sessions/<unit_key>/working-memory.yaml`) when that file EXISTS (the
    Phase-1A activation signal -- only a NON-reducer Body ever forks one), else
    the agent-wide WM (`session/working-memory.yaml`). So a worker writes ONLY
    its own Body WM once forked, and a reducer / single-runner / no-unit_key
    collapses to the agent-wide WM (dormant until a 2nd Body forks).

    `project_root` overrides PROJECT_ROOT for tests (the body-merge.py pattern).
    """
    pr = project_root or PROJECT_ROOT
    agent_dir = _agents_root(pr) / agent
    if unit_key:
        body_wm = agent_dir / SESSIONS_DIRNAME / unit_key / "working-memory.yaml"
        if body_wm.exists():
            return body_wm
    return agent_dir / SESSION_DIRNAME / "working-memory.yaml"


# --------------------------- CLI ---------------------------

def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Worker-body execution contract (Phase 2A, asp-306).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("phases", help="print the worker's simplified phase sequence")
    sub.add_parser("reducer-only-phases", help="print the phases a worker skips")
    sp = sub.add_parser("should-run-phase",
                        help="exit 0 (+print 'run') if a worker runs <phase>, else exit 1 (+'skip')")
    sp.add_argument("phase")
    wp = sub.add_parser("wm-path", help="print the worker's WM target path")
    wp.add_argument("--agent", default=AGENT_NAME or "")
    wp.add_argument("--unit-key", default=None)
    args = ap.parse_args(argv)

    if args.cmd == "phases":
        print(" ".join(WORKER_PHASES))
        return 0
    if args.cmd == "reducer-only-phases":
        print(" ".join(sorted(REDUCER_ONLY_PHASES)))
        return 0
    if args.cmd == "should-run-phase":
        run = worker_should_run_phase(args.phase)
        print("run" if run else "skip")
        return 0 if run else 1
    if args.cmd == "wm-path":
        if not args.agent:
            print("error: --agent required (no MIND_AGENT in env)", file=sys.stderr)
            return 2
        print(str(worker_wm_path(args.agent, args.unit_key)))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
