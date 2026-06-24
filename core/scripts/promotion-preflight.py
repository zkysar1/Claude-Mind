#!/usr/bin/env python3
"""Promotion preflight drift gate.

Before promoting a framework SOURCE repo onto a TARGET repo (the
dev -> staging -> prod promotion chain), detect content the TARGET has
that the SOURCE does NOT -- i.e. downstream drift that a blind "mirror"
promotion would silently overwrite or orphan.

Principle: **promotion is a RECONCILE, not a MIRROR.** Anything the target
leads on must be back-ported UP to the source (or explicitly discarded with
sign-off) BEFORE the overwrite -- never clobbered. This is the
verify-before-assuming discipline applied to releases: look before you
overwrite.

Read-only. Compares only framework paths; auto-excludes build artifacts.

Exit codes:
  0  CLEAN  -- target framework is a subset of source (safe to promote)
  2  DRIFT  -- target has framework files the source lacks (orphan risk), or
              (with --strict) framework files differ; reconcile/back-port first
  1  ERROR  -- bad invocation

Usage:
  promotion-preflight.py --source <incoming_repo> --target <repo_to_overwrite>
  promotion-preflight.py --source ../staging-repo --target . --strict --json
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import sys
from pathlib import Path

# Framework paths that SHOULD stay in lockstep across deployments (the portable
# cognition core). Domain state (world/, meta/, agents/) is deployment-local by
# design and is intentionally NOT compared.
FRAMEWORK_PATHS = [
    "CLAUDE.md",
    "core/config",
    "core/scripts",
    ".claude/skills",
    ".claude/rules",
    ".claude/settings.json",
]

# Build artifacts / machine-local / transient -- never features. Pruned from
# the walk so they can never be mistaken for drift.
EXCLUDE_DIRS = {
    "__pycache__", ".git", ".python-shim", "node_modules",
    ".pytest_cache", ".history", ".mypy_cache", ".ruff_cache",
}
EXCLUDE_FILE_GLOBS = ["*.pyc", "*.pyo", "*.log", ".DS_Store", "*.swp"]
# Substring match on any path segment -> excluded (temp test-output dirs like
# core/scripts/tests/_tmp_cross_repo_commit_test).
EXCLUDE_SUBSTR = ["_tmp_"]

# Files that legitimately differ per deployment (each repo's own copy is
# correct). Reported separately; never counted as blocking drift.
DEPLOYMENT_LOCAL = {
    "CLAUDE.md",                          # deployment-specific sections (prod vs dev)
    ".claude/settings.json",             # deployment env/hooks/permission config
    ".claude/settings.local.json",       # constitutional anchor (machine-local)
    ".claude/rules/promotion-cycle.md",  # names THIS deployment's chain position
}


def _seg_excluded(rel: str) -> bool:
    parts = rel.replace("\\", "/").split("/")
    if any(seg in EXCLUDE_DIRS for seg in parts):
        return True
    if any(any(s in seg for s in EXCLUDE_SUBSTR) for seg in parts):
        return True
    base = parts[-1]
    return any(fnmatch.fnmatch(base, g) for g in EXCLUDE_FILE_GLOBS)


def walk_framework(root: Path) -> dict[str, Path]:
    """Map rel-path -> abs Path for every framework file under root (noise pruned)."""
    out: dict[str, Path] = {}
    for sub in FRAMEWORK_PATHS:
        base = root / sub
        if base.is_file():
            if not _seg_excluded(sub):
                out[sub] = base
        elif base.is_dir():
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [
                    d for d in dirnames
                    if d not in EXCLUDE_DIRS and not any(s in d for s in EXCLUDE_SUBSTR)
                ]
                for fn in filenames:
                    ab = Path(dirpath) / fn
                    rel = str(ab.relative_to(root)).replace("\\", "/")
                    if not _seg_excluded(rel):
                        out[rel] = ab
    return out


def digest(p: Path) -> str:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except Exception:
        return "READ_ERROR:" + p.name


def is_skill(rel: str) -> bool:
    return rel.startswith(".claude/skills/")


def main() -> int:
    ap = argparse.ArgumentParser(description="Promotion preflight drift gate (reconcile, not mirror).")
    ap.add_argument("--source", required=True, help="incoming repo (e.g. ../staging-repo)")
    ap.add_argument("--target", required=True, help="repo about to be overwritten (e.g. .)")
    ap.add_argument("--strict", action="store_true",
                    help="also BLOCK on differing framework files (not just orphan risk)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    src, tgt = Path(args.source).resolve(), Path(args.target).resolve()
    if not src.is_dir() or not tgt.is_dir():
        print("ERROR: --source and --target must be existing directories", file=sys.stderr)
        return 1
    if src == tgt:
        print("ERROR: --source and --target are the same directory", file=sys.stderr)
        return 1

    S = walk_framework(src)
    T = walk_framework(tgt)
    s_keys, t_keys = set(S), set(T)

    target_only = sorted(t_keys - s_keys)
    source_only = sorted(s_keys - t_keys)
    differing = sorted(k for k in (s_keys & t_keys) if digest(S[k]) != digest(T[k]))

    def bucket(keys):
        core = [k for k in keys if not is_skill(k) and k not in DEPLOYMENT_LOCAL]
        skills = [k for k in keys if is_skill(k)]
        deploy = [k for k in keys if k in DEPLOYMENT_LOCAL]
        return core, skills, deploy

    to_core, to_skills, to_deploy = bucket(target_only)      # ORPHAN RISK (target leads)
    df_core, df_skills, df_deploy = bucket(differing)        # WILL CHANGE (direction unknown)
    so_core, so_skills, so_deploy = bucket(source_only)      # normal promotion payload

    # Blocking drift: target-only core framework files are an UNAMBIGUOUS loss
    # (a mirror promotion deletes them). With --strict, differing core files
    # also block (each could be target-ahead).
    blocking = list(to_core)
    if args.strict:
        blocking += df_core
    drift = len(blocking) > 0

    if args.json:
        print(json.dumps({
            "source": str(src), "target": str(tgt), "strict": args.strict,
            "orphan_risk_core": to_core, "orphan_risk_skills": to_skills,
            "differing_core": df_core, "differing_skills": df_skills,
            "deployment_local_differing": sorted(set(to_deploy + df_deploy)),
            "source_ahead_core": so_core, "source_ahead_skills": so_skills,
            "verdict": "DRIFT" if drift else "CLEAN", "exit": 2 if drift else 0,
        }, indent=2))
        return 2 if drift else 0

    print("═══ PROMOTION PREFLIGHT DRIFT GATE ═══")
    print(f"source (incoming)    : {src}")
    print(f"target (overwritten) : {tgt}")
    print(f"mode                 : {'STRICT (block on any diff)' if args.strict else 'default (block on orphan risk)'}")
    print()

    if to_core:
        print("⛔ ORPHAN RISK — framework files the TARGET has but SOURCE lacks.")
        print("   A mirror promotion would DELETE these. Back-port UP to source, or")
        print("   explicitly discard, BEFORE promoting:")
        for k in to_core:
            print(f"     {k}")
        print()
    if df_core:
        tag = "⛔ BLOCKED" if args.strict else "⚠ REVIEW"
        print(f"{tag} — framework files that DIFFER (some may be target-ahead = clobber):")
        for k in df_core:
            print(f"     {k}")
        print("   Determine direction (target-ahead -> back-port first; source-ahead -> ok).")
        print()
    if to_skills:
        print(f"ℹ target-only skills ({len(to_skills)}) — usually domain/forged (deployment-local).")
        print("   VERIFY none is a base framework skill that drifted:")
        for k in to_skills[:40]:
            print(f"     {k.split('/')[2] if k.count('/') >= 2 else k}")
        if len(to_skills) > 40:
            print(f"     ... +{len(to_skills) - 40} more")
        print()
    if to_deploy or df_deploy:
        dl = sorted(set(to_deploy + df_deploy))
        print(f"ℹ deployment-local files differing/only (expected, not drift): {', '.join(dl)}")
        print()
    print(f"normal promotion payload (source-ahead): {len(so_core)} core + {len(so_skills)} skills + "
          f"{len(df_skills)} differing skills")
    print()

    if drift:
        n = len(to_core) + (len(df_core) if args.strict else 0)
        print(f"VERDICT: ⛔ DRIFT DETECTED — {len(to_core)} orphan-risk"
              + (f" + {len(df_core)} differing(strict)" if args.strict else "")
              + " framework file(s).")
        print("         Promotion would lose target-ahead content. Reconcile before overwriting. (exit 2)")
        if not args.strict and df_core:
            print(f"         (also {len(df_core)} differing framework files to review — run --strict to block on them too.)")
        return 2

    print("VERDICT: ✅ CLEAN — target framework is a subset of source. Safe to promote. (exit 0)")
    if df_core and not args.strict:
        print(f"         (note: {len(df_core)} framework files differ — review with --strict if cautious.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
