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
import subprocess
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


def git_last_commit_ts(repo_root: Path, rel_path: str) -> int | None:
    """Return committer-date unix timestamp of the last commit touching rel_path, or None."""
    try:
        r = subprocess.run(
            ["git", "log", "-1", "--format=%ct", "--", rel_path],
            capture_output=True, text=True, cwd=str(repo_root), timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            return int(r.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass
    return None


def classify_direction(
    src_root: Path, tgt_root: Path, rel_path: str,
    src_abs: Path, tgt_abs: Path,
) -> str:
    """Classify a differing file as 'source_ahead', 'target_ahead', or 'ambiguous'."""
    # Signal 1: git commit timestamps
    src_ts = git_last_commit_ts(src_root, rel_path)
    tgt_ts = git_last_commit_ts(tgt_root, rel_path)
    if src_ts is not None and tgt_ts is not None:
        if tgt_ts > src_ts:
            return "target_ahead"
        if src_ts > tgt_ts:
            return "source_ahead"
        # Equal timestamps -- fall through to content heuristic

    # Signal 1b: filesystem mtime fallback (when git unavailable)
    if src_ts is None or tgt_ts is None:
        try:
            s_mt = src_abs.stat().st_mtime
            t_mt = tgt_abs.stat().st_mtime
            # Require >60s difference to avoid filesystem noise
            if t_mt - s_mt > 60:
                return "target_ahead"
            if s_mt - t_mt > 60:
                return "source_ahead"
        except OSError:
            pass

    # Signal 2: content-contains (strict superset check)
    try:
        src_content = src_abs.read_text(errors="replace")
        tgt_content = tgt_abs.read_text(errors="replace")
        src_lines = set(src_content.splitlines())
        tgt_lines = set(tgt_content.splitlines())
        if src_lines < tgt_lines:  # strict subset -> target is ahead
            return "target_ahead"
        if tgt_lines < src_lines:
            return "source_ahead"
    except Exception:
        pass

    return "ambiguous"


def parse_known_criteria(repo_root: Path) -> set[str] | None:
    """AST-parse KNOWN_CRITERIA from <root>/core/scripts/goal-selector.py.

    g-115-2525: the selector's code-side criteria manifest, extracted without
    importing the module (goal-selector.py resolves agent/meta paths at import
    time — import would bind to THIS box's deployment, and fail entirely on a
    bare checkout). Returns None when the file or the frozenset literal is
    absent (pre-manifest selector) — caller skips the contract check.
    """
    sel = repo_root / "core" / "scripts" / "goal-selector.py"
    if not sel.is_file():
        return None
    import ast
    try:
        tree = ast.parse(sel.read_text(encoding="utf-8"))
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "KNOWN_CRITERIA"
                        for t in node.targets)
                and isinstance(node.value, ast.Call)
                and getattr(node.value.func, "id", "") == "frozenset"
                and node.value.args
                and isinstance(node.value.args[0], ast.Set)):
            elts = node.value.args[0].elts
            if all(isinstance(e, ast.Constant) and isinstance(e.value, str)
                   for e in elts):
                return {e.value for e in elts}
    return None


def check_weights_contract(src: Path, tgt: Path) -> dict:
    """Cross-check meta-strategy weight keys against the SOURCE selector's
    KNOWN_CRITERIA (g-115-2525 part 3).

    Closes the blind spot that let the rb-498-era promotion orphan a weight in
    prod: meta/ is external and deployment-local, so the framework-path walk
    above never sees it — yet promoting selector code that no longer computes
    a criterion the target's meta still weights used to KeyError selection
    fleet-wide (now degraded to a runtime warning by load_weights, but the
    mismatch should be caught HERE, before the overwrite).

    Two layers:
      seed_orphans   — SOURCE seed template (core/config/meta.yaml
                       initial_state.goal_selection_strategy.weights) keys the
                       SOURCE selector does not compute. Always checkable;
                       blocking (a fresh deployment would seed the mismatch).
      target_metas   — best-effort: each TARGET agents/*/local-paths.conf is
                       parsed for META_PATH and that live meta's weights are
                       cross-checked. Reachable only for same-box targets;
                       unreachable metas are reported informationally (the
                       load_weights runtime warning covers them on their box).
    """
    result: dict = {"checked": False, "seed_orphans": [], "target_metas": []}
    known = parse_known_criteria(src)
    if known is None:
        result["note"] = "source selector has no KNOWN_CRITERIA manifest — check skipped"
        return result
    try:
        import yaml
    except ImportError:
        result["note"] = "PyYAML unavailable — check skipped"
        return result
    result["checked"] = True

    def weights_of(meta_yaml: Path, dotted: tuple[str, ...]) -> dict | None:
        try:
            data = yaml.safe_load(meta_yaml.read_text(encoding="utf-8"))
        except Exception:
            return None
        for k in dotted:
            if not isinstance(data, dict):
                return None
            data = data.get(k)
        return data if isinstance(data, dict) else None

    seed = src / "core" / "config" / "meta.yaml"
    if seed.is_file():
        w = weights_of(seed, ("initial_state", "goal_selection_strategy", "weights"))
        if w:
            result["seed_orphans"] = sorted(set(w) - known)

    # Foreign-root agents/* glob (same documented pattern as seed-transplant.sh):
    # the TARGET is another repo root, so the target's own AGENTS_PARENT_DIR
    # constant cannot be resolved from here — literal "agents" is intentional.
    for conf in sorted(tgt.glob("agents/*/local-paths.conf")):
        meta_path = None
        try:
            for line in conf.read_text(encoding="utf-8").splitlines():
                if line.startswith("META_PATH="):
                    meta_path = Path(line.split("=", 1)[1].strip().strip('"'))
                    break
        except OSError:
            pass
        entry = {"conf": str(conf), "meta": str(meta_path) if meta_path else None,
                 "status": "unreachable", "orphans": []}
        if meta_path:
            strategy = meta_path / "goal-selection-strategy.yaml"
            if strategy.is_file():
                w = weights_of(strategy, ("weights",))
                if w is not None:
                    entry["status"] = "checked"
                    entry["orphans"] = sorted(set(w) - known)
        result["target_metas"].append(entry)
    return result


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

    # Direction-classify every differing file
    diff_target_ahead: list[str] = []
    diff_source_ahead: list[str] = []
    diff_ambiguous: list[str] = []
    for k in differing:
        direction = classify_direction(src, tgt, k, S[k], T[k])
        if direction == "target_ahead":
            diff_target_ahead.append(k)
        elif direction == "source_ahead":
            diff_source_ahead.append(k)
        else:
            diff_ambiguous.append(k)

    def bucket(keys):
        core = [k for k in keys if not is_skill(k) and k not in DEPLOYMENT_LOCAL]
        skills = [k for k in keys if is_skill(k)]
        deploy = [k for k in keys if k in DEPLOYMENT_LOCAL]
        return core, skills, deploy

    to_core, to_skills, to_deploy = bucket(target_only)      # ORPHAN RISK (target leads)
    so_core, so_skills, so_deploy = bucket(source_only)      # normal promotion payload

    ta_core, ta_skills, ta_deploy = bucket(diff_target_ahead)
    sa_core, sa_skills, sa_deploy = bucket(diff_source_ahead)
    am_core, am_skills, am_deploy = bucket(diff_ambiguous)

    # Weights contract (5 part 3): meta-strategy weight keys vs the
    # source selector's KNOWN_CRITERIA. Seed orphans + reachable target-meta
    # orphans BLOCK (the promotion would land/leave a weight no criterion
    # computes); unreachable target metas are informational.
    wc = check_weights_contract(src, tgt)
    wc_blocking = list(wc.get("seed_orphans") or [])
    wc_target_orphans = [(e["conf"], e["orphans"])
                         for e in wc.get("target_metas") or []
                         if e["status"] == "checked" and e["orphans"]]
    wc_drift = bool(wc_blocking or wc_target_orphans)

    # Blocking drift: target-only core (orphan risk) + target-ahead core (clobber risk)
    # ALWAYS block -- not gated by --strict
    blocking = list(to_core) + list(ta_core)
    if args.strict:
        blocking += am_core  # ambiguous blocks only in strict mode
    drift = len(blocking) > 0 or wc_drift

    if args.json:
        print(json.dumps({
            "source": str(src), "target": str(tgt), "strict": args.strict,
            "orphan_risk_core": to_core, "orphan_risk_skills": to_skills,
            "target_ahead_core": ta_core, "target_ahead_skills": ta_skills,
            "source_ahead_core": sa_core, "source_ahead_skills": sa_skills,
            "ambiguous_core": am_core, "ambiguous_skills": am_skills,
            "deployment_local_differing": sorted(set(to_deploy + ta_deploy + sa_deploy + am_deploy)),
            "source_only_core": so_core, "source_only_skills": so_skills,
            "weights_contract": wc,
            "verdict": "DRIFT" if drift else "CLEAN", "exit": 2 if drift else 0,
        }, indent=2))
        return 2 if drift else 0

    print("═══ PROMOTION PREFLIGHT DRIFT GATE ═══")
    print(f"source (incoming)    : {src}")
    print(f"target (overwritten) : {tgt}")
    print(f"mode                 : {'STRICT (block on any diff)' if args.strict else 'default (block on orphan risk)'}")
    print()

    if to_core:
        print("ORPHAN RISK -- framework files the TARGET has but SOURCE lacks.")
        print("   A mirror promotion would DELETE these. Back-port UP to source, or")
        print("   explicitly discard, BEFORE promoting:")
        for k in to_core:
            print(f"     {k}")
        print()
    if ta_core:
        print("CLOBBER RISK -- framework files the TARGET LEADS ON (more recent).")
        print("   A mirror promotion would REGRESS these. Back-port UP to source first:")
        for k in ta_core:
            print(f"     {k}")
        print()
    if am_core:
        tag = "BLOCKED" if args.strict else "REVIEW"
        print(f"{tag} -- framework files that DIFFER (direction ambiguous):")
        for k in am_core:
            print(f"     {k}")
        print()
    if sa_core:
        print(f"source-ahead (verified): {len(sa_core)} core framework files (safe to overwrite)")
        print()
    if to_skills:
        print(f"target-only skills ({len(to_skills)}) -- usually domain/forged (deployment-local).")
        print("   VERIFY none is a base framework skill that drifted:")
        for k in to_skills[:40]:
            print(f"     {k.split('/')[2] if k.count('/') >= 2 else k}")
        if len(to_skills) > 40:
            print(f"     ... +{len(to_skills) - 40} more")
        print()
    all_deploy = sorted(set(to_deploy + ta_deploy + sa_deploy + am_deploy))
    if all_deploy:
        print(f"deployment-local files differing/only (expected, not drift): {', '.join(all_deploy)}")
        print()
    if wc.get("checked"):
        if wc_blocking:
            print("WEIGHTS-CONTRACT ORPHANS (SOURCE seed) -- seed template weights the")
            print("   source selector does not compute (a fresh deployment would seed the")
            print("   mismatch). Fix core/config/meta.yaml or restore the criteria:")
            for k in wc_blocking:
                print(f"     {k}")
            print()
        for conf, orphans in wc_target_orphans:
            print(f"WEIGHTS-CONTRACT ORPHANS (target meta via {conf}) -- live weights the")
            print("   incoming selector does not compute (the rb-498 orphaned-weight class).")
            print("   Remove the weight in the target meta or restore the criteria:")
            for k in orphans:
                print(f"     {k}")
            print()
        unreachable = [e for e in wc.get("target_metas") or [] if e["status"] != "checked"]
        if unreachable:
            print(f"weights-contract: {len(unreachable)} target meta(s) unreachable from this box "
                  f"(cross-box target — load_weights' runtime warning covers them there)")
            print()
    elif wc.get("note"):
        print(f"weights-contract: {wc['note']}")
        print()
    print(f"normal promotion payload (source-only): {len(so_core)} core + {len(so_skills)} skills")
    print()

    if drift:
        print(f"VERDICT: DRIFT DETECTED -- {len(to_core)} orphan-risk"
              + (f" + {len(ta_core)} target-ahead" if ta_core else "")
              + (f" + {len(am_core)} ambiguous(strict)" if args.strict and am_core else "")
              + (f" + weights-contract orphans (seed:{len(wc_blocking)}, target-metas:{len(wc_target_orphans)})" if wc_drift else "")
              + " framework file(s).")
        print("         Promotion would lose target-ahead content. Reconcile before overwriting. (exit 2)")
        if not args.strict and am_core:
            print(f"         (also {len(am_core)} ambiguous framework files -- run --strict to block on them too.)")
        return 2

    print("VERDICT: CLEAN -- target framework is a subset of source. Safe to promote. (exit 0)")
    if am_core and not args.strict:
        print(f"         (note: {len(am_core)} ambiguous framework files -- review with --strict if cautious.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
