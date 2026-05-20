#!/usr/bin/env python3
"""Origin-Signal Gate CLI — thin wrapper around core/scripts/gates/origin_signal.py.

PR 7a/2 extracted the decision logic + side effects into
`gates.origin_signal.evaluate()` so the daemon writer endpoints can
import it directly (skip ~300ms subprocess startup per add-goal call).
This script preserves the legacy argv shape, stdin JSON contract, output
shape, and exit codes — subprocess callers in aspirations.py are unchanged.

Rejects make-work at goal-filing time. User-sourced goals (world queue,
no agent context) skip. Agent-sourced goals MUST cite a concrete signal
via `origin_signal`. Missing/invalid blocks unless override OR Layer-D
auto-derive matches the title prefix.

Exit codes (single):
  0 = clear (valid signal, world-source, auto-derived, or override applied)
  1 = blocked (missing or invalid origin_signal, no override, no derive)
  2 = framework error (bad input)

Exit codes (batch):
  0 = no block (all goals cleared)
  1 = any_blocked AND no override
  2 = framework error (bad input)

See the module docstring at core/scripts/gates/origin_signal.py for the
canonical input/output shapes and the SINGLE SOURCE OF TRUTH for
ALLOWED_PREFIXES.
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _stdio import reconfigure_stdio  # type: ignore  # noqa: E402
reconfigure_stdio()

from gates.origin_signal import evaluate  # type: ignore  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--override-signal", default=None,
                    help="Justification string; bypasses the gate and audits.")
    ap.add_argument("--output", choices=["json", "human"], default="json")
    args = ap.parse_args()

    raw = sys.stdin.read()
    if not raw.strip():
        print("origin-signal-gate: empty stdin", file=sys.stderr)
        sys.exit(2)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"origin-signal-gate: invalid JSON: {e}", file=sys.stderr)
        sys.exit(2)

    # WORLD_DIR is the canonical exported env var from core/scripts/_paths.sh.
    # Do NOT add a WORLD_PATH fallback — that is a local-conf variable, never
    # exported, so reading it would be dead code.
    world_env = os.environ.get("WORLD_DIR")
    world_dir = Path(world_env) if world_env else None
    agent_name = os.environ.get("MIND_AGENT") or ""

    result = evaluate(
        payload,
        override_signal=args.override_signal,
        agent_name=agent_name,
        world_dir=world_dir,
    )

    # Output + exit-code shaping. Mirrors the legacy CLI's stdout/stderr split.
    if "any_blocked" in result:
        # Batch mode.
        print(json.dumps(result))
        sys.exit(1 if result["any_blocked"] else 0)

    # Single mode.
    if result["would_block"] and args.output == "human":
        print(result["reason"], file=sys.stderr)
    else:
        print(json.dumps(result))
    sys.exit(1 if result["would_block"] else 0)


if __name__ == "__main__":
    main()
