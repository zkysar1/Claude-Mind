#!/usr/bin/env python3
"""Goal-Duplication Gate CLI — thin wrapper around core/scripts/gates/goal_duplication.py.

PR 7a/4 extracted the five-check decision logic + override-audit side effect
into `gates.goal_duplication.evaluate()` so daemon writer endpoints can
import it directly (skip ~300ms subprocess startup per goal filing). This
script preserves the legacy argv shape, stdin JSON contract, output shape,
and exit codes — subprocess callers in aspirations-add-goal.sh and
create-aspiration Phase 8 are unchanged.

Hard check BEFORE filing a new goal. Catches the g-115-141 class: a new
goal whose scope overlaps with peer work that either (a) is visible in
the team-state recent_completions ring buffer, (b) is the subject of a
partner's in_flight claim, (c) has already landed in git commits within
48h, (d) is the subject of an active insight_trigger finding, (e) is
already implemented in the target file, or (f) is already pending /
in-progress in the world or any agent aspiration queue (g-115-783;
matches via origin_signal exact OR structural file/identifier overlap).

N-agent correct: the gate identifies ITSELF via MIND_AGENT and scans all
non-self completions, all recent commits (any author), and all non-self
insight_triggers. No "partner" enumeration — works for 2, 3, or N-agent
worlds without code change.

Exit codes:
  0 = clear (or override applied)
  1 = would block (overlap + no override)
  2 = framework error (bad input / unreadable goal JSON)

Override:
  --override-duplication "<justification>"
  Audited to world/goal-duplication-overrides.jsonl.

Input (stdin JSON or --goal-json file):
    {
      "title": "...",
      "description": "...",
      "participants": ["agent"],     # optional
      "source": "world" | "agent",   # optional
      "origin_signal": "...",         # optional; response-prefix matches enable expected-coverage filter
      "verification": {...}           # optional; preferred signal source over prose
    }

See `gates/goal_duplication.py` for the canonical output shape and the
SINGLE SOURCE OF TRUTH for _STOPWORDS / _RESPONSE_ORIGIN_PREFIXES / IDF
weighting / threshold constants.

Fail-open on subprocess errors (git missing, team-state unreadable, board
unreachable) — the gate must not block real work when its diagnostic tools
misbehave.
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _stdio import reconfigure_stdio  # type: ignore  # noqa: E402
reconfigure_stdio()

from gates.goal_duplication import evaluate  # type: ignore  # noqa: E402


SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

from _paths import agent_dir as _agent_dir  # noqa: E402


def _read_local_paths_conf(agent_name: str) -> dict:
    conf = _agent_dir(agent_name) / "local-paths.conf"
    out = {}
    if not conf.is_file():
        return out
    for line in conf.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _resolve_world_dir():
    """Resolve WORLD_DIR for team-state.yaml + override-ledger writes.
    Test-override: MIND_WORLD wins over per-agent local-paths.conf so
    tests can redirect to a tmp world without touching the real one."""
    env_world = os.environ.get("MIND_WORLD", "").strip()
    if env_world:
        return Path(env_world)
    agent = os.environ.get("MIND_AGENT", "").strip()
    if not agent:
        return None
    conf = _read_local_paths_conf(agent)
    wp = conf.get("WORLD_PATH")
    return Path(wp) if wp else None


def _load_goal(args) -> dict:
    """Read goal JSON from --goal-json file or stdin. Exits 2 on framework
    error (empty/invalid JSON)."""
    if args.goal_json and args.goal_json != "-":
        raw = Path(args.goal_json).read_text(encoding="utf-8")
    else:
        if sys.stdin.isatty():
            print("goal-duplication-gate: expected JSON on stdin "
                  "or --goal-json", file=sys.stderr)
            sys.exit(2)
        raw = sys.stdin.read()
    try:
        return json.loads(raw)
    except Exception as e:
        print(f"goal-duplication-gate: bad JSON input: {e}", file=sys.stderr)
        sys.exit(2)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Goal-Duplication Gate — hard check before filing a new goal.",
    )
    ap.add_argument("--goal-json", default=None,
                    help="Path to goal JSON file, or '-' to read stdin. Defaults to stdin.")
    ap.add_argument("--override-duplication", default=None,
                    dest="override_duplication",
                    help="Justification for bypassing the gate. Appends to "
                         "world/goal-duplication-overrides.jsonl.")
    ap.add_argument("--output", default="json", choices=["json", "human"])
    args = ap.parse_args(argv)

    goal = _load_goal(args)

    # N-agent correct: `agent_name` is the ONLY identity the gate needs.
    # Empty string allowed — every completion treated as non-self, which is
    # the conservative default (flag everything).
    agent_name = os.environ.get("MIND_AGENT", "").strip()
    result = evaluate(
        goal,
        override_duplication=args.override_duplication,
        agent_name=agent_name,
        world_dir=_resolve_world_dir(),
        project_root=PROJECT_ROOT,
    )

    if args.output == "json":
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        self_agent = result.get("self_agent") or "<unset>"
        print(f"would_block: {result['would_block']}")
        print(f"self_agent: {self_agent}   file_paths: {result['file_paths_detected']}")
        for c in result["checks"]:
            mark = "PASS" if c["passed"] else "FAIL"
            print(f"  [{mark}] {c['name']}: {c['reason']}")
            if c.get("matches") and not c["passed"]:
                for m in c["matches"][:3]:
                    print("      match: " + json.dumps(m)[:200])
        if result.get("override_applied"):
            print(f"override: {result['override_applied']}")

    if result.get("override_applied"):
        print(f"[goal-duplication-gate] override applied: "
              f"{result['override_applied']}", file=sys.stderr)

    return 1 if result["would_block"] else 0


if __name__ == "__main__":
    sys.exit(main())
