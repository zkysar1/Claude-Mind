#!/usr/bin/env python3
# domain-leak-exempt: this gate documents concrete repo context (Ayoai-Mind)
# in its rationale block — references are informative scope notes, not
# accidental domain bleed (g-115-432, 2026-05-08).
"""Uncommitted-work gate CLI — thin wrapper around core/scripts/gates/uncommitted_work.py.

PR 7a extracted the decision logic into `gates.uncommitted_work.evaluate()`
so the daemon writer endpoints can import it directly (skip the
~300ms subprocess startup per add-goal call). This script preserves
the legacy argv shape, JSON-payload shape, and exit codes — subprocess
callers in aspirations.py and elsewhere are unchanged.

Rationale and scope (unchanged from the pre-extraction file):

    The orphan-code audit on 2026-05-07 (coord msg-20260507-065332-system-4790)
    found ~20 dirty framework files in Ayoai-Mind plus a 5-day-stale
    Ayoai-Environment-Processor llm_service.py with 5 source goals all
    status=completed in the world queue but never shipped to git.

    world/conventions/post-execution.md says "commit and push after every
    framework change," but no enforcement gate ensures that happens BEFORE the
    goal closes. This gate is the chokepoint — invoked from
    aspirations.py cmd_update_goal at field=='status' and value=='completed',
    it scans the framework repo for dirty framework-code patterns and blocks
    the close unless --override "<reason>" is passed (audit-logged).

Scope (v1): framework repo only. Cross-repo orphans are NOT detected;
    that requires a separate cross-repo scan or per-repo manifest.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from gates.uncommitted_work import evaluate


SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent  # Ayoai-Mind repo root

sys.path.insert(0, str(SCRIPT_DIR))
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
    agent has no local-paths.conf or no WORLD_PATH key."""
    agent = os.environ.get("MIND_AGENT", "").strip()
    if not agent:
        return None
    conf = _read_local_paths_conf(agent)
    wp = conf.get("WORLD_PATH")
    return Path(wp) if wp else None


def main() -> int:
    p = argparse.ArgumentParser(
        description="Pre-completion gate: refuses goal close when framework "
                    "code is uncommitted. Outputs JSON {would_block, "
                    "dirty_framework_files, repo_path}.",
    )
    p.add_argument("--goal-id", required=True,
                   help="Goal ID being closed (for audit-log correlation).")
    p.add_argument("--override", default=None,
                   help="Justification for bypassing the gate. When set, the "
                        "gate exits 0 even if dirty files exist, and appends "
                        "an audit record to world/uncommitted-work-overrides.jsonl.")
    p.add_argument("--repo-path", default=str(PROJECT_ROOT),
                   help="Path to the repo to scan (default: framework repo).")
    p.add_argument("--output", choices=["json", "human"], default="json",
                   help="Output format (default: json).")
    p.add_argument("--body-role", default=None,
                   help="reducer|worker. Defaults to $BODY_ROLE. Only a "
                        "reducer is blocked for committed-but-unpushed "
                        "framework files: a worker Body does not push by "
                        "contract (g-306-233).")
    args = p.parse_args()

    payload = evaluate(
        goal_id=args.goal_id,
        override=args.override,
        repo_path=Path(args.repo_path),
        world_dir=_resolve_world_dir(),
        agent_name=os.environ.get("MIND_AGENT", "").strip(),
        # BODY_ROLE is read HERE, at the CLI boundary, and passed in — the
        # gate module documents itself as reading no environment variables so
        # it stays safe to call from any daemon thread. --body-role overrides
        # it for tests and for callers that know their own role.
        body_role=args.body_role or os.environ.get("BODY_ROLE", "").strip() or None,
    )

    if args.output == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if payload["would_block"]:
            if payload["dirty_framework_files"]:
                print(f"BLOCKED: {len(payload['dirty_framework_files'])} dirty framework file(s):")
                for f in payload["dirty_framework_files"]:
                    print(f"  {f}")
            if payload.get("delivery_would_block"):
                # Named explicitly because the remedy DIFFERS from the dirty
                # case: these files are committed, so `git status` is clean and
                # committing again does nothing. The fix is `git push`.
                print(f"BLOCKED: {len(payload['undelivered_framework_files'])} framework "
                      f"file(s) committed but NOT pushed to upstream (remedy: git push):")
                for f in payload["undelivered_framework_files"]:
                    print(f"  {f}")
        elif payload["dirty_framework_files"] and payload["override_applied"]:
            print(f"OVERRIDE: {len(payload['dirty_framework_files'])} dirty file(s) bypassed: "
                  f"{payload['override_applied']!r}")
        else:
            print("CLEAN: no dirty framework code")

    return 1 if payload["would_block"] else 0


if __name__ == "__main__":
    sys.exit(main())
