#!/usr/bin/env python3
"""CLI wrapper for gates/aspiration_supply.py — pre-flight for the idle path.

The daemon's aspiration add endpoint runs the same evaluate() at write time;
this wrapper lets create-aspiration (Step 5.7) test each CANDIDATE before
filing, so a refusal arrives as structured feedback instead of a 400 from the
daemon after the whole record was assembled.

Usage:
    echo '<candidate aspiration JSON>' | python3 core/scripts/aspiration-supply-gate.py \
        [--override-supply "<why>"] [--output json|human] [--agent-queue]

Exit codes: 0 pass (or not gated) | 1 would block | 2 usage / error.
Reads WORLD_DIR / META_DIR / agent dir through _paths (MIND_AGENT-bound).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _paths import META_DIR, PROJECT_ROOT, WORLD_DIR, AGENT_NAME, agent_dir  # noqa: E402
from gates.aspiration_supply import (  # noqa: E402
    evaluate, load_existing, load_tree_keys, DEFAULT_CONFIG,
)


def _config() -> dict:
    try:
        from _config_overlay import merged_config
        block = (merged_config("aspirations.yaml") or {}).get("idle_supply") or {}
        return {**DEFAULT_CONFIG, **{k: v for k, v in block.items() if v is not None}}
    except Exception as exc:  # fail-open to defaults, but say so
        print(f"[aspiration-supply-gate] WARN: config read failed ({exc}); using defaults",
              file=sys.stderr)
        return dict(DEFAULT_CONFIG)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--override-supply", default=None,
                    help="bypass a would-block verdict; audited to world/aspiration-supply-overrides.jsonl")
    ap.add_argument("--output", choices=("json", "human"), default="json")
    ap.add_argument("--agent-queue", action="store_true",
                    help="also count the bound agent's local queue as existing work")
    args = ap.parse_args(argv)

    raw = sys.stdin.read()
    try:
        cand = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        print(f"[aspiration-supply-gate] stdin is not JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(cand, dict) or not cand.get("title"):
        print("[aspiration-supply-gate] expected a JSON aspiration object with a title on stdin",
              file=sys.stderr)
        return 2

    agent = AGENT_NAME or ""
    adir = agent_dir(agent) if agent else None
    existing = load_existing(WORLD_DIR, agent_dirs=[adir] if (args.agent_queue and adir) else [])
    result = evaluate(
        cand,
        existing=existing,
        tree_keys=load_tree_keys(WORLD_DIR),
        override_supply=args.override_supply,
        agent_name=agent,
        world_dir=WORLD_DIR,
        project_root=PROJECT_ROOT,
        meta_dir=META_DIR,
        agent_dir=adir,
        config=_config(),
    )
    if args.output == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        verdict = "BLOCK" if result["would_block"] else ("PASS" if result["gated"] else "NOT GATED")
        print(f"aspiration-supply-gate: {verdict} — {cand.get('title', '')[:100]}")
        for f in result["failures"]:
            print(f"  ✗ {f['check']}: {f['detail']}")
        for o in result.get("overlaps") or []:
            print(f"  ~ overlap {o['id']} [{o['status']}] {o['containment']:.0%}: {o['title']}")
        if result.get("remedy"):
            print(f"  → {result['remedy']}")
        if result.get("override_applied"):
            print(f"  ! override applied: {result['override_applied']}")
    return 1 if result["would_block"] else 0


if __name__ == "__main__":
    sys.exit(main())
