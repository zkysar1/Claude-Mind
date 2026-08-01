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
import datetime
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


# ── Phase 0 zone classifier (, REPORT-ONLY) ──────────────────────
# Maps a framework rel-path to one of the genome-model promotion zones. This is
# a COARSE PATH+EXTENSION heuristic BY DESIGN: Phase 0 only REPORTS the zone
# (zero gating-behavior change); Phase 2 () replaces it with a TESTED
# machine diff-classifier that reads the actual change to tell parametric from
# structural. Zones (see world/knowledge/tree/system/publication-pipeline.md
# "Planned Evolution — Zone-Aware Bidirectional Promotion"):
#   KERNEL               -- constitutional anchor + the promotion mechanism itself
#                           (HUMAN-LOCKED, DOWN-ONLY, FOREVER; never reconciles up)
#   PHENOTYPE-parametric -- value/threshold config DATA (reconcile-up eligible)
#   PHENOTYPE-structural -- skills/rules/scripts/docs logic (reconcile-up w/ review)
#   NICHE                -- world/domain data (never promoted, either direction)
ZONE_KERNEL = "KERNEL"
ZONE_PHENO_PARAM = "PHENOTYPE-parametric"
ZONE_PHENO_STRUCT = "PHENOTYPE-structural"
ZONE_NICHE = "NICHE"

# Constitutional anchor + the promotion mechanism = KERNEL. Kept deliberately
# TIGHT: only the anchor, its enforcer, and the core transplant/preflight
# orchestrators + manifest. The publishability GATE scripts (domain-leak-check
# etc.) are tooling, not the mechanism, so they stay PHENOTYPE.
_KERNEL_EXACT = {
    ".claude/settings.local.json",                      # constitutional anchor
    "core/scripts/settings-structural-validator.py",    # anchor enforcer
    "core/scripts/settings-structural-validator.sh",
    "core/scripts/seed-transplant.sh",                  # promotion mechanism
    "core/scripts/seed-preflight.sh",
    "core/scripts/promotion-preflight.py",
    "core/scripts/promotion-preflight.sh",
    "core/scripts/_seed_engine.py",
    "core/config/seed-manifest.yaml",
}
_KERNEL_PREFIXES = (".claude/skills/seed/", "core/config/seed-templates/")
_NICHE_PREFIXES = ("world/", "meta/", "agents/")


def classify_zone(rel: str) -> str:
    """Classify a framework rel-path into a promotion zone (report-only heuristic).

    Pure + deterministic (path string only, no I/O). Phase 2 supersedes the
    parametric/structural split with a diff-content classifier; the KERNEL and
    NICHE decisions are path-inherent and remain valid regardless.
    """
    r = rel.replace("\\", "/")
    if r in _KERNEL_EXACT or r.startswith(_KERNEL_PREFIXES):
        return ZONE_KERNEL
    if r.startswith(_NICHE_PREFIXES):
        return ZONE_NICHE
    # PHENOTYPE split: parametric = config DATA (tunable values/thresholds);
    # structural = everything else (skills/rules/scripts/docs logic). A
    # .yaml/.yml/.json under core/config that is NOT the KERNEL manifest is
    # treated as parametric.
    if r.startswith("core/config/") and r.endswith((".yaml", ".yml", ".json")):
        return ZONE_PHENO_PARAM
    return ZONE_PHENO_STRUCT


_UNPARSEABLE = object()


def _parse_config(path: Path, text: str):
    """Parse a parametric config file's text into a comparable data structure.

    Returns the parsed data (dict/list/scalar), or the _UNPARSEABLE sentinel when
    the file is not a YAML/JSON config or fails to parse. yaml is imported lazily
    so the module has no hard dependency on PyYAML for the path-only code paths.
    """
    suf = path.suffix.lower()
    try:
        if suf in (".yaml", ".yml"):
            import yaml  # lazy — only parametric divergence checks need it
            return yaml.safe_load(text)
        if suf == ".json":
            return json.loads(text)
    except Exception:
        return _UNPARSEABLE
    return _UNPARSEABLE


def classify_divergence(src_abs: Path, tgt_abs: Path, zone: str) -> dict:
    """Classify the CONTENT divergence between two versions of a framework file.

    Path-based zone classification (classify_zone) says WHAT a file is; this says
    what CHANGED inside it. The Phase-1 pilot (g-115-2867) found that a
    PHENOTYPE-parametric file's diff can be pure comment/provenance drift
    (downstream goal-id sanitization, e.g. Ayoai->Claude stripping private
    goal-ids from comments) with NO value change — reconciling that "up" would
    REGRESS the dev repo by stripping provenance. So a parametric divergence is
    reconcile-eligible ONLY when a VALUE actually changed, not when only comments
    or formatting differ.

    Discriminator: parse both versions (YAML/JSON) and compare the DATA. Comments
    and formatting vanish in the parse, so equal parsed data == comment/format-only
    divergence (NOT reconcile-eligible); differing parsed data == a real value
    change (reconcile-eligible). Structural (non-config) files have no
    value/comment split — they always reconcile with review. Unparseable configs
    fall back to conservative "value" so a real change is never silently dropped.

    Returns {kind, reconcile_eligible, detail}:
      kind: "identical" | "comment" | "value" | "structural" | "unparseable"
    """
    if zone != ZONE_PHENO_PARAM:
        # Structural / kernel / niche: no value-vs-comment split. Structural
        # changes reconcile with review; kernel/niche are governed by zone policy.
        return {"kind": "structural", "reconcile_eligible": True,
                "detail": f"{zone}: content value/comment split not applicable"}
    try:
        src_text = src_abs.read_text(errors="replace")
        tgt_text = tgt_abs.read_text(errors="replace")
    except OSError as e:
        return {"kind": "unparseable", "reconcile_eligible": True,
                "detail": f"read error ({e}); treat as value-change (conservative)"}
    if src_text == tgt_text:
        return {"kind": "identical", "reconcile_eligible": False,
                "detail": "byte-identical"}
    parsed_s = _parse_config(src_abs, src_text)
    parsed_t = _parse_config(tgt_abs, tgt_text)
    if parsed_s is _UNPARSEABLE or parsed_t is _UNPARSEABLE:
        # Cannot compare data — never silently drop a possible real change.
        return {"kind": "unparseable", "reconcile_eligible": True,
                "detail": "config did not parse; treat as value-change (conservative)"}
    if parsed_s == parsed_t:
        return {"kind": "comment", "reconcile_eligible": False,
                "detail": "parsed data identical — divergence is comment/formatting "
                          "only (provenance drift, not a value change)"}
    return {"kind": "value", "reconcile_eligible": True,
            "detail": "parsed data differs — a real config value changed"}


def excuse_comment_only_blocks(blocking: list[str],
                               content_divergence: dict[str, dict]) -> tuple[list[str], list[str]]:
    """Phase 2 ENFORCEMENT (): drop comment/provenance-drift false-blocks.

    A PHENOTYPE-parametric file can land in the blocking set (as target_ahead
    "clobber risk", or ambiguous under --strict) when its ONLY difference from
    source is comment/provenance drift — the g-115-2867 Phase-1 finding:
    downstream goal-id sanitization (Ayoai->Claude) strips private goal-ids from
    comments, leaving the parsed VALUE identical. Blocking that is a FALSE
    positive: there is nothing of value to clobber, only comments differ. This
    filter removes such files (classify_divergence kind=="comment",
    reconcile_eligible False) from the blocking set.

    CONSERVATIVE by construction — a file is excused ONLY when content_divergence
    proved kind=="comment". VALUE and UNPARSEABLE parametric diffs are NOT excused
    (a real change is never silently dropped). Orphan-risk (target-only) and
    structural/non-parametric blocks have no content_divergence entry, so they
    can never match — orphan and structural blocks are unaffected.

    Returns (filtered_blocking, excused) where excused is the removed subset.
    """
    excused = [k for k in blocking
               if content_divergence.get(k, {}).get("kind") == "comment"]
    excused_set = set(excused)
    filtered = [k for k in blocking if k not in excused_set]
    return filtered, excused


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


def git_source_freshness(repo_root: Path) -> dict:
    """Is --source current with its own remote?  ()

    WHY THIS EXISTS. --source takes a PATH and this tool measures whatever tree
    is there, reporting cleanly either way. Aimed at a stale checkout it reports
    that checkout's staleness as if it were the target's drift, and nothing in
    the output hints at it. Measured 2026-07-31 (g-029-102): --source pointed at
    a clone 48 commits behind origin/main yielded "56 orphan-risk + 38
    target-ahead"; the same target measured against a fresh
    `worktree --detach origin/main` yielded "0 orphan-risk + 3 target-ahead".
    A 30x overstatement, well-formed and plausible, which then justified two
    filed goals, a reasoning-bank entry, a guardrail, and a duplication-gate
    override before an unrelated "+0 lines merged" surprise exposed it.

    guard-297 documents the correct aiming procedure, but that is honor-system
    and the failure it guards is the operator's own — the case rb-840 says to
    WIRE rather than encode. Hence this check.

    Returns {"state": ..., "ahead": int, "behind": int, "detail": str}.
    state is one of: current | stale | no_upstream | not_git | unknown.
    FAIL-OPEN by construction: every error path returns a non-blocking state,
    because preflight must stay usable against a plain directory.
    """
    def _git(*a) -> str | None:
        try:
            r = subprocess.run(["git", *a], capture_output=True, text=True,
                               cwd=str(repo_root), timeout=10)
            return r.stdout.strip() if r.returncode == 0 else None
        except (subprocess.TimeoutExpired, OSError):
            return None

    if _git("rev-parse", "--git-dir") is None:
        return {"state": "not_git", "ahead": 0, "behind": 0,
                "detail": "source is not a git repo -- freshness unverifiable"}

    # Prefer the configured upstream; fall back to the remote's default branch.
    upstream = _git("rev-parse", "--abbrev-ref", "@{u}")
    if not upstream:
        head_ref = _git("symbolic-ref", "--quiet", "--short", "HEAD")
        for cand in ([f"origin/{head_ref}"] if head_ref else []) + ["origin/main", "origin/master"]:
            if _git("rev-parse", "--verify", "--quiet", cand):
                upstream = cand
                break
    if not upstream:
        return {"state": "no_upstream", "ahead": 0, "behind": 0,
                "detail": "source has no upstream/origin ref -- freshness unverifiable"}

    counts = _git("rev-list", "--left-right", "--count", f"{upstream}...HEAD")
    if not counts:
        return {"state": "unknown", "ahead": 0, "behind": 0,
                "detail": f"could not compare against {upstream}"}
    try:
        behind, ahead = (int(x) for x in counts.split())
    except ValueError:
        return {"state": "unknown", "ahead": 0, "behind": 0,
                "detail": f"unparseable rev-list output vs {upstream}"}

    if behind == 0:
        return {"state": "current", "ahead": ahead, "behind": 0,
                "detail": f"source is current with {upstream}"}
    return {"state": "stale", "ahead": ahead, "behind": behind,
            "detail": (f"source is {behind} commit(s) BEHIND {upstream}"
                       + (f" (and {ahead} ahead)" if ahead else ""))}


def format_source_freshness_warning(fresh: dict) -> str | None:
    """Human-facing WARN for a stale/unverifiable source, or None when current."""
    st = fresh.get("state")
    if st == "current":
        return None
    if st == "stale":
        return (f"WARN: --source is {fresh['behind']} commit(s) BEHIND its remote. "
                "Drift figures below may be THAT CHECKOUT being stale rather than "
                "target-ahead content. Re-aim per guard-297: git fetch origin; "
                "git worktree add <scratch> --detach origin/main; then measure. "
                "(g-029-104)")
    return f"note: {fresh.get('detail', 'source freshness unverifiable')} -- drift figures unverified against a remote"


def git_last_transplant_ts(repo_root: Path) -> int | None:
    """Committer-date unix ts of the target's most recent transplant/sync commit.

    seed-transplant.sh lands a transplant as ONE bulk commit
    'chore: sync framework (<TS>)' (seed-transplant.sh:464) that stamps EVERY
    planted file with the promotion date. That timestamp is the baseline below
    which a per-file target last-commit ts is a recency SHADOW, not a genuine
    target-side edit (rb-4253 / g-115-2744: a v2.5.0 run flagged 654/657 files
    target_ahead purely because the transplant commit post-dated every
    since-untouched source file). Returns None when no such commit exists (the
    target never received a transplant) -- classify_direction then trusts the raw
    per-file timestamps, preserving legacy behavior with zero regression.
    """
    try:
        r = subprocess.run(
            ["git", "log", "-1", "--format=%ct", "--grep=^chore: sync framework"],
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
    baseline_ts: int | None = None,
) -> str:
    """Classify a differing file as 'source_ahead', 'target_ahead', or 'ambiguous'.

    baseline_ts: committer-date of the target's most recent transplant/sync commit
    (git_last_transplant_ts). A per-file tgt_ts > src_ts is only a GENUINE
    target-ahead signal when tgt_ts > baseline_ts (the target file was edited AFTER
    the transplant landed). When tgt_ts <= baseline_ts the recency is a transplant
    SHADOW (rb-4253) -- fall through to the content heuristic instead of declaring
    target_ahead. None disables the guard (no transplant baseline -> raw per-file
    timestamps, legacy behavior).
    """
    # Signal 1: git commit timestamps (recency-shadow-guarded, )
    src_ts = git_last_commit_ts(src_root, rel_path)
    tgt_ts = git_last_commit_ts(tgt_root, rel_path)
    if src_ts is not None and tgt_ts is not None:
        if tgt_ts > src_ts:
            # Only trust "target newer" when the target file was committed AFTER the
            # last transplant baseline. Otherwise tgt_ts is the bulk-transplant
            # commit date (a shadow) -- fall through to the content heuristic below.
            if baseline_ts is None or tgt_ts > baseline_ts:
                return "target_ahead"
        elif src_ts > tgt_ts:
            return "source_ahead"
        # Equal timestamps, or a shadowed tgt_ts -- fall through to content heuristic

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

    # Phase 0 zone classification () — REPORT-ONLY, computed over the
    # full drift surface (differing + orphans both directions). Emitted below in
    # both output paths; NEVER feeds `blocking`/`drift`/exit codes.
    all_drift_files = sorted(set(differing) | set(target_only) | set(source_only))
    zone_map = {k: classify_zone(k) for k in all_drift_files}
    zone_summary: dict[str, int] = {}
    for _z in zone_map.values():
        zone_summary[_z] = zone_summary.get(_z, 0) + 1

    # Recency-shadow baseline (): a bulk transplant commit stamps every
    # planted file with the promotion date, so raw per-file target timestamps
    # over-report target_ahead right after a promotion (rb-4253: 654/657 shadows).
    # Anchor direction on the target's last-transplant commit -- computed ONCE
    # (per-target, not per-file); a per-file tgt_ts at/before it is a shadow.
    baseline_ts = git_last_transplant_ts(tgt)

    # Direction-classify every differing file
    diff_target_ahead: list[str] = []
    diff_source_ahead: list[str] = []
    diff_ambiguous: list[str] = []
    for k in differing:
        direction = classify_direction(src, tgt, k, S[k], T[k], baseline_ts)
        if direction == "target_ahead":
            diff_target_ahead.append(k)
        elif direction == "source_ahead":
            diff_source_ahead.append(k)
        else:
            diff_ambiguous.append(k)

    # Phase 2 content-divergence classification () — for DIFFERING
    # PHENOTYPE-parametric files, classify each diff as VALUE vs COMMENT via
    # parsed-data comparison (classify_divergence). Phase-1 pilot finding
    # (): a parametric file's diff can be pure comment/provenance drift
    # (downstream goal-id sanitization) with NO value change — reconcile-eligible
    # ONLY on a real value change; reconciling comment drift "up" would REGRESS
    # the dev repo by stripping provenance. REPORT-ONLY for now (mirrors the
    # Phase 0 zone column); feeds a future reconcile-eligibility enforcement.
    content_divergence: dict[str, dict] = {}
    for k in differing:
        z = zone_map.get(k) or classify_zone(k)
        if z == ZONE_PHENO_PARAM:
            content_divergence[k] = classify_divergence(S[k], T[k], z)
    param_value_diffs = sorted(k for k, v in content_divergence.items()
                               if v.get("kind") == "value")
    param_comment_diffs = sorted(k for k, v in content_divergence.items()
                                 if v.get("kind") == "comment")

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

    # Weights contract ( part 3): meta-strategy weight keys vs the
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
    # Phase 2 ENFORCEMENT (): excuse PHENOTYPE-parametric files that
    # block ONLY on comment/provenance drift (no value change — the 
    # Phase-1 finding). This promotes content_divergence from report-only to a
    # verdict-affecting gate: a preflight that was DRIFT purely because of a
    # downstream goal-id scrub now goes CLEAN. VALUE/UNPARSEABLE parametric diffs
    # and all orphan/structural blocks are conservatively retained.
    blocking, comment_only_excused = excuse_comment_only_blocks(blocking, content_divergence)
    drift = len(blocking) > 0 or wc_drift

    # Phase 3b (): partition the target-ahead blocking set by promotion
    # zone so the STRUCTURAL-WITH-REVIEW gate is EXPLICIT. This is pure LABELING —
    # every file here is already in ta_core and already blocks; the partition does
    # NOT change `drift`/verdict/exit, it distinguishes the RECONCILE TYPE each
    # target-ahead file needs (per core/config/conventions/promotion-cycle.md):
    #   KERNEL target-ahead  -> CANNOT back-port up (down-only, guard-98). A prod
    #                           leading on KERNEL is an anomaly needing human
    #                           resolution, never a mechanical reconcile.
    #   PHENOTYPE-structural -> back-port up WITH REVIEW (logic change; guard-97).
    #   else (parametric)    -> mechanical value reconcile up.
    _ta_excused_set = set(comment_only_excused)
    ta_core_blocking = [k for k in ta_core if k not in _ta_excused_set]
    kernel_up_conflict = [k for k in ta_core_blocking if zone_map.get(k) == ZONE_KERNEL]
    structural_requires_review = [k for k in ta_core_blocking
                                  if zone_map.get(k) == ZONE_PHENO_STRUCT]
    param_reconcile_up = [k for k in ta_core_blocking
                          if zone_map.get(k) not in (ZONE_KERNEL, ZONE_PHENO_STRUCT)]

    # Source freshness () -- a stale --source reports ITS OWN staleness
    # as drift. Advisory only: never changes the verdict or the exit code.
    src_fresh = git_source_freshness(src)
    src_warn = format_source_freshness_warning(src_fresh)

    if args.json:
        print(json.dumps({
            "source": str(src), "target": str(tgt), "strict": args.strict,
            "baseline_transplant_ts": baseline_ts,
            "orphan_risk_core": to_core, "orphan_risk_skills": to_skills,
            "target_ahead_core": ta_core, "target_ahead_skills": ta_skills,
            "source_ahead_core": sa_core, "source_ahead_skills": sa_skills,
            "ambiguous_core": am_core, "ambiguous_skills": am_skills,
            "deployment_local_differing": sorted(set(to_deploy + ta_deploy + sa_deploy + am_deploy)),
            "source_only_core": so_core, "source_only_skills": so_skills,
            "weights_contract": wc,
            # Phase 0 () REPORT-ONLY zone column — per-file zone map +
            # summary counts. Does NOT affect verdict/exit.
            "zone_classification_report_only": zone_map,
            "zone_summary": zone_summary,
            # Phase 2 content-divergence for differing parametric files: value vs
            # comment/provenance drift ( built the classifier). As of the
            # Phase 2 ENFORCEMENT () this NOW AFFECTS verdict/exit:
            # comment-only parametric blocks are excused (see
            # comment_only_excused_from_blocking); the per-file map + diff lists
            # remain diagnostic.
            "content_divergence": content_divergence,
            "param_value_diffs": param_value_diffs,
            "param_comment_diffs": param_comment_diffs,
            "comment_only_excused_from_blocking": comment_only_excused,
            # Phase 3b (): zone partition of the target-ahead blocking
            # set — the explicit structural-with-review gate. Labeling only; every
            # file here is already blocking (does NOT affect verdict/exit).
            "kernel_up_conflict": kernel_up_conflict,
            "structural_requires_review": structural_requires_review,
            "param_reconcile_up": param_reconcile_up,
            "source_freshness": src_fresh,
            "verdict": "DRIFT" if drift else "CLEAN", "exit": 2 if drift else 0,
        }, indent=2))
        return 2 if drift else 0

    print("═══ PROMOTION PREFLIGHT DRIFT GATE ═══")
    print(f"source (incoming)    : {src}")
    print(f"target (overwritten) : {tgt}")
    print(f"mode                 : {'STRICT (block on any diff)' if args.strict else 'default (block on orphan risk)'}")
    print(f"source freshness     : {src_fresh['detail']}")
    if baseline_ts is not None:
        bl = datetime.datetime.fromtimestamp(baseline_ts, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        print(f"transplant baseline  : {bl} UTC -- per-file target timestamps at/before this are shadows, not target-ahead (g-115-2744)")
    else:
        print("transplant baseline  : none (target has no 'chore: sync framework' commit -- raw per-file timestamps used)")
    print()
    if src_warn:
        print(f"  {src_warn}")
        print()

    # Phase 2 ENFORCEMENT (): display the EFFECTIVE (post-excusal)
    # blocking buckets so the human verdict stays consistent with `drift`. A
    # comment-only parametric file excused from blocking must not appear as
    # CLOBBER RISK while the verdict reads CLEAN.
    excused_set = set(comment_only_excused)
    # ta_core_blocking + the zone partition (kernel_up_conflict /
    # structural_requires_review / param_reconcile_up) were computed above.
    am_core_blocking = [k for k in am_core if k not in excused_set]

    if to_core:
        print("ORPHAN RISK -- framework files the TARGET has but SOURCE lacks.")
        print("   A mirror promotion would DELETE these. Back-port UP to source, or")
        print("   explicitly discard, BEFORE promoting:")
        for k in to_core:
            print(f"     {k}")
        print()
    if ta_core_blocking:
        print("CLOBBER RISK -- framework files the TARGET LEADS ON (more recent).")
        print("   A mirror promotion would REGRESS these. Reconcile UP to source first.")
        print("   Reconcile TYPE by promotion zone (g-115-2907; all still block):")
        # Phase 3b (): the explicit structural-with-review gate.
        if kernel_up_conflict:
            print("   KERNEL -- CANNOT reconcile up (down-only, guard-98). A prod that")
            print("      LEADS on a KERNEL file is an anomaly; resolve by HUMAN, not back-port:")
            for k in kernel_up_conflict:
                print(f"        {k}")
        if structural_requires_review:
            print("   PHENOTYPE-structural -- back-port up WITH REVIEW (logic change,")
            print("      guard-97). Not a mechanical reconcile; review the diff before landing:")
            for k in structural_requires_review:
                print(f"        {k}")
        if param_reconcile_up:
            print("   PHENOTYPE-parametric -- mechanical value reconcile up:")
            for k in param_reconcile_up:
                print(f"        {k}")
        print()
    if am_core_blocking:
        tag = "BLOCKED" if args.strict else "REVIEW"
        print(f"{tag} -- framework files that DIFFER (direction ambiguous):")
        for k in am_core_blocking:
            print(f"     {k}")
        print()
    if comment_only_excused:
        print(f"EXCUSED ({len(comment_only_excused)}) -- PHENOTYPE-parametric files whose diff is")
        print("   COMMENT/PROVENANCE drift only (no value change; e.g. downstream goal-id")
        print("   scrub). NOT blocked -- reconciling comment drift up would strip provenance")
        print("   (g-115-2867 Phase-1 finding; g-115-2885 enforcement):")
        for k in comment_only_excused:
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

    # Phase 0 zone column () — REPORT-ONLY, zero gating effect.
    if zone_map:
        print("ZONE CLASSIFICATION (report-only, g-115-2864 Phase 0 -- does NOT affect verdict):")
        for z in (ZONE_KERNEL, ZONE_PHENO_PARAM, ZONE_PHENO_STRUCT, ZONE_NICHE):
            files = [k for k in all_drift_files if zone_map[k] == z]
            if not files:
                continue
            note = " (HUMAN-LOCKED, down-only -- never back-port up)" if z == ZONE_KERNEL else \
                   " (never promoted)" if z == ZONE_NICHE else ""
            print(f"   {z}: {len(files)}{note}")
            for k in files[:25]:
                print(f"     {k}")
            if len(files) > 25:
                print(f"     ... +{len(files) - 25} more")
        print("   (heuristic path-based; Phase 2 g-115-2865 replaces with a tested diff-classifier)")
        print()

    # Restate the staleness WARN immediately above the verdict ():
    # readers routinely `| tail -3` this output and never see the header block,
    # which is exactly how the  misread happened.
    if src_warn:
        print(f"  {src_warn}")

    if drift:
        print(f"VERDICT: DRIFT DETECTED -- {len(to_core)} orphan-risk"
              + (f" + {len(ta_core_blocking)} target-ahead" if ta_core_blocking else "")
              + (f" + {len(am_core_blocking)} ambiguous(strict)" if args.strict and am_core_blocking else "")
              + (f" + weights-contract orphans (seed:{len(wc_blocking)}, target-metas:{len(wc_target_orphans)})" if wc_drift else "")
              + (f" ({len(comment_only_excused)} comment-only excused)" if comment_only_excused else "")
              + " framework file(s).")
        print("         Promotion would lose target-ahead content. Reconcile before overwriting. (exit 2)")
        if not args.strict and am_core_blocking:
            print(f"         (also {len(am_core_blocking)} ambiguous framework files -- run --strict to block on them too.)")
        return 2

    print("VERDICT: CLEAN -- target framework is a subset of source. Safe to promote. (exit 0)")
    if am_core_blocking and not args.strict:
        print(f"         (note: {len(am_core_blocking)} ambiguous framework files -- review with --strict if cautious.)")
    if comment_only_excused:
        print(f"         ({len(comment_only_excused)} PHENOTYPE-parametric comment/provenance-only diff(s) excused -- see EXCUSED above.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
