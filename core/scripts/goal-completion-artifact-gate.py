#!/usr/bin/env python3
"""Goal-completion artifact gate CLI — thin wrapper around
core/scripts/gates/completion_artifact.py.

Refuses goal closure when the goal description references files that
don't exist on disk. Canonical incident: g-115-724 (2026-05-14),
where `core/scripts/post-state-update-metric-gate.sh` was marked
complete but never committed; 17 stderr failures resulted before
discovery.

Pattern parallel to uncommitted-work-gate.py and capability-gate.py:
- Thin CLI wrapper preserves argv/exit/JSON contract for subprocess callers
- Decision logic in gates/completion_artifact.py for daemon import path

Invoked from aspirations.py cmd_update_goal at field=='status' and
value=='completed', before the uncommitted-work gate.

Output JSON (stdout):
    {
      "would_block": bool,
      "missing_artifacts": [str, ...],
      "near_misses": {str: str},
      "checked_paths": int,
      "goal_id": str,
      "override_applied": str | None,
      "skipped_reason": str | None
    }

Exit codes:
    0 = clean OR override applied OR skipped (non-action goal)
    1 = would_block (missing artifacts, no override)
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _stdio import reconfigure_stdio  # type: ignore  # noqa: E402
reconfigure_stdio()

from gates.completion_artifact import evaluate  # type: ignore  # noqa: E402


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


def _resolve_dirs():
    """Resolve WORLD_DIR and META_DIR. Returns (world_dir, meta_dir),
    either of which may be None when MIND_AGENT is unset or the
    agent's local-paths.conf has no WORLD_PATH/META_PATH key.

    Path strings route through _paths._absolutize so a drive-letter
    value like "C:/Users/..." is absolutized correctly even on a
    POSIX-flavored Python interpreter (the canonical cruft incident
    from g-115-733: Path("C:/...") returns a relative PosixPath under
    that interpretation, which would join under cwd and produce a
    cruft mirror). Plain Path(wp) would NOT defend against that — the
    helper is the single source of truth.
    """
    agent = os.environ.get("MIND_AGENT", "").strip()
    if not agent:
        return None, None
    conf = _read_local_paths_conf(agent)
    wp = conf.get("WORLD_PATH")
    mp = conf.get("META_PATH")
    from _paths import _absolutize
    world = _absolutize(wp) if wp else None
    meta = _absolutize(mp) if mp else None
    return world, meta


def main() -> int:
    p = argparse.ArgumentParser(
        description="Pre-completion gate: refuses goal close when goal "
                    "description references files that don't exist. "
                    "Outputs JSON shape documented at module-level.",
    )
    p.add_argument("--goal-id", required=True,
                   help="Goal ID being closed (for audit-log correlation).")
    p.add_argument("--goal-title", required=True,
                   help="Goal title (used for action-prefix filter).")
    p.add_argument("--goal-description", default="",
                   help="Goal description (scanned for artifact paths).")
    p.add_argument("--override", default=None,
                   help="Justification for bypassing the gate. When set, "
                        "the gate exits 0 even if artifacts are missing, "
                        "and appends a record to "
                        "world/missing-artifact-overrides.jsonl.")
    p.add_argument("--output", choices=["json", "human"], default="json",
                   help="Output format (default: json).")
    args = p.parse_args()

    world_dir, meta_dir = _resolve_dirs()

    payload = evaluate(
        goal_id=args.goal_id,
        goal_title=args.goal_title,
        goal_description=args.goal_description,
        override=args.override,
        project_root=PROJECT_ROOT,
        world_dir=world_dir,
        meta_dir=meta_dir,
        agent_name=os.environ.get("MIND_AGENT", "").strip(),
    )

    if args.output == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if payload["would_block"]:
            print(f"BLOCKED: {len(payload['missing_artifacts'])} missing "
                  f"artifact(s) referenced by goal description:")
            for vp in payload["missing_artifacts"]:
                hint = payload["near_misses"].get(vp)
                if hint:
                    print(f"  {vp}  (did you mean {hint}?)")
                else:
                    print(f"  {vp}")
        elif payload["skipped_reason"]:
            print(f"SKIPPED: {payload['skipped_reason']}")
        elif payload["missing_artifacts"] and payload["override_applied"]:
            print(f"OVERRIDE: {len(payload['missing_artifacts'])} missing "
                  f"artifact(s) bypassed: {payload['override_applied']!r}")
        else:
            print(f"CLEAN: checked {payload['checked_paths']} artifact "
                  f"path(s), all exist")

    return 1 if payload["would_block"] else 0


if __name__ == "__main__":
    sys.exit(main())
