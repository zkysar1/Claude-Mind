#!/usr/bin/env python3
"""Blocker-Create Gate CLI — thin wrapper around core/scripts/gates/blocker_create.py.

PR 7a/3 extracted the four-check decision logic + override-audit side effect
into `gates.blocker_create.evaluate()` so daemon writer endpoints can import
it directly (skip ~300ms subprocess startup per CREATE_BLOCKER call). This
script preserves the legacy argv shape, stdin JSON contract, output shape,
and exit codes — subprocess callers in aspirations-execute/SKILL.md Step 2.55
are unchanged.

Hard check BEFORE writing a new blocker. Catches the five false-positive
failure modes:

  1. Non-canonical probe (synthetic ssh/curl instead of the skill's
     companion_script). Enforces rb-226 / guard-147 / rb-246.
  2. Single-signal negation — one failed command is NOT a blocker.
     Enforces .claude/rules/verify-before-assuming.md multi-signal rule.
  3. Statistical negation without schema probe. Enforces rb-245 / rb-258 /
     rb-259.
  4. Infrastructure blocker without infra-health-check evidence.
  5. credentials-required blocker without per-source identity enumeration
     (a self-serviceable grant wrongly routed to a human). Enforces
     guard-1160 / g-248-111.

Exit 1 with a specific reason when any check fails, unless
`--override-blocker-gate "<justification>"` is passed. The override is
append-logged to world/blocker-gate-overrides.jsonl for audit.

Input shape (stdin JSON or --blocker-json file):

    {
      "type": "infrastructure" | "resource" | "user_action" | ...,
      "affected_skills": ["skill-name", ...],
      "failure_reason": "<short description>",
      "evidence": [
        {"tool":"efs-ssh.sh","command":"...","output":"...","evidence_type":"command_exit"},
        {"endpoint":"...","evidence_type":"http_status"},
        ...
      ],
      "schema_probe_evidence": {...},       # optional
      "infra_health_check": {...}           # optional, required when type==infrastructure
    }

See `gates/blocker_create.py` for the canonical output shape and the
SINGLE SOURCE OF TRUTH for HUMAN_ONLY_BLOCKER_TYPES + statistical-negation
patterns + silent-failure patterns.
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _stdio import reconfigure_stdio  # type: ignore  # noqa: E402
reconfigure_stdio()

from gates.blocker_create import evaluate  # type: ignore  # noqa: E402


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
    """Resolve WORLD_DIR for override-ledger writes. Returns None when the
    agent has no local-paths.conf or no WORLD_PATH key (override is still
    granted, but the audit trail is lost — surfaced on stderr in evaluate())."""
    agent = os.environ.get("MIND_AGENT", "").strip()
    if not agent:
        return None
    conf = _read_local_paths_conf(agent)
    wp = conf.get("WORLD_PATH")
    return Path(wp) if wp else None


def _load_blocker(args) -> dict:
    """Read blocker JSON from --blocker-json file or stdin. Exits 2 on
    framework error (empty/invalid JSON, wrong root type)."""
    if args.blocker_json and args.blocker_json != "-":
        raw = Path(args.blocker_json).read_text(encoding="utf-8")
    else:
        if sys.stdin.isatty():
            print("Error: expected blocker JSON on stdin "
                  "(or --blocker-json <file>).", file=sys.stderr)
            sys.exit(2)
        raw = sys.stdin.read()
    raw = raw.strip()
    if not raw:
        print("Error: empty blocker JSON.", file=sys.stderr)
        sys.exit(2)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Error: blocker JSON parse failed: {e}", file=sys.stderr)
        sys.exit(2)
    if not isinstance(data, dict):
        print(f"Error: blocker JSON must be an object "
              f"(got {type(data).__name__}).", file=sys.stderr)
        sys.exit(2)
    return data


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Blocker-create gate — hard check before writing a new blocker.",
    )
    ap.add_argument("--blocker-json", default=None,
                    help="Path to blocker JSON file, or '-' to read stdin. "
                         "Defaults to stdin.")
    ap.add_argument("--probe-command", default=None,
                    help="The actual probe command that was run.")
    ap.add_argument("--override-blocker-gate", default=None,
                    dest="override_blocker_gate",
                    help="Justification for bypassing the gate. Appends to "
                         "world/blocker-gate-overrides.jsonl.")
    ap.add_argument("--output", default="json", choices=["json", "human"])
    args = ap.parse_args(argv)

    blocker = _load_blocker(args)
    result = evaluate(
        blocker,
        probe_command=args.probe_command,
        override_blocker_gate=args.override_blocker_gate,
        world_dir=_resolve_world_dir(),
        agent_name=os.environ.get("MIND_AGENT", "").strip(),
    )

    if args.output == "json":
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"would_block: {result['would_block']}")
        print(f"failing: {result['failing_count']}")
        for c in result["checks"]:
            mark = "PASS" if c["passed"] else "FAIL"
            print(f"  [{mark}] {c['name']}: {c['reason']}")
        if result.get("override_applied"):
            print(f"override: {result['override_applied']}")

    if result.get("override_applied"):
        print(f"[blocker-create-gate] override applied: "
              f"{result['override_applied']}", file=sys.stderr)

    return 1 if result["would_block"] else 0


if __name__ == "__main__":
    sys.exit(main())
