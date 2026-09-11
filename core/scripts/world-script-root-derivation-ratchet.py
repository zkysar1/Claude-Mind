#!/usr/bin/env python3
"""world-script-root-derivation-ratchet.py — advisory drift check with baseline ratchet.

Counts scripts under the world scripts dir that locate the framework root
(`core/scripts/_paths.sh`) by deriving it from their OWN location instead of
honouring an inherited PROJECT_ROOT.

Why this metric exists (g-115-9541, source g-115-9533): a script living under an
EXTERNAL path cannot reach the framework root from its own location. BOTH
self-location derivations are structurally unavailable there:

  $SCRIPT_DIR/../..                       walks OUTSIDE the repo
                                          (world/ and the repo sit in unrelated parents)
  git -C "$SCRIPT_DIR" rev-parse --show-toplevel
                                          returns EMPTY — the world dir is not a git repo

When both fail the source target collapses to an ABSOLUTE "/core/scripts/_paths.sh"
and the script dies rc=128. The leading slash is the tell. Measured instance:
mind-seed-freshness-check.sh had failed on EVERY invocation since authorship, and
because its caller mapped the unrecognised rc to a retry bucket the failure never
escalated — a silence detector that was itself silent (guard-6347). guard-5419
prescribes honouring an inherited PROJECT_ROOT; nothing enforced it.

WHAT COUNTS AS A VIOLATION — narrow on purpose. The originally-proposed predicate
("grep for BASH_SOURCE or rev-parse --show-toplevel") reports 174 files here and
would ship a permanently-red check (guard-329, guard-574). Nearly every script
legitimately uses BASH_SOURCE to locate ITSELF; and a bare relative
`source core/scripts/_paths.sh` is the SANCTIONED form, because the documented
calling convention is `source core/scripts/_paths.sh && bash "$WORLD_PATH/..."`
from PROJECT_ROOT. Only self-location-derived roots are the defect, so a line
counts only when ALL of these hold:

  1. it references core/scripts/_paths.sh
  2. it is a source/dot command, not a comment
  3. the root is self-location-derived (dirname / *_DIR/.. / BASH_SOURCE /
     rev-parse --show-toplevel)
  4. it does NOT mention PROJECT_ROOT (a `${PROJECT_ROOT:-<self-loc>}` form
     honours the inherited value first, which is exactly what guard-5419 asks)

BLIND IS NOT CLEAN (guard-1947). Where the world scripts dir does not resolve,
this reports BLIND, writes NOTHING, and exits 0. Seeding a baseline of 0 from a
box that cannot see the corpus would make every box that CAN see it read
REGRESSED — the failure mode the ratchet exists to avoid.

Exit codes:
  0  any outcome (advisory — never hard-fails /verify-learning)
  2  script error (unreadable corpus, etc.)

Hard-gate opt-in: VERIFY_LEARNING_DRIFT_HARD_GATE=1 (exit 1 on regressed),
same contract as learning-routing-ratchet.py.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from _paths import META_DIR, WORLD_DIR  # type: ignore
from _fileops import locked_modify_yaml  # type: ignore

try:
    import yaml  # type: ignore  # noqa: F401
except ImportError:
    print("ERROR: PyYAML not installed", file=sys.stderr)
    sys.exit(2)


BASELINES_PATH = META_DIR / "audit-baselines.yaml"
KEY = "world_script_root_derivation"

# (1) references the framework-root loader
_PATHS_REF = re.compile(r"core/scripts/_paths\.sh")
# (2) is a source/dot command (not prose). Anchored at line start or after a
#     shell operator, so `|| source ...` and `; . ...` continuation lines count.
_SOURCE_CMD = re.compile(r"(?:^|[|&;]\s*|\bthen\s+|\bdo\s+)\s*(?:source|\.)\s+\S")
# (3) the root is derived from the script's own location
_SELF_LOCATED = re.compile(
    r"rev-parse\s+--show-toplevel"
    r"|BASH_SOURCE"
    r"|\bdirname\b"
    r"|\$\{?[A-Za-z_][A-Za-z0-9_]*_DIR\}?/\.\."
)
# (4) an inherited PROJECT_ROOT is honoured somewhere on the line
_HONOURS_ROOT = re.compile(r"PROJECT_ROOT")

_SKIP_PART = ("__pycache__",)


def _skip(path: Path) -> bool:
    if any(p in _SKIP_PART for p in path.parts):
        return True
    # .bak / .bak-<tag> copies are frozen history, not live code
    return ".bak" in path.name


def _scan(scripts_dir: Path):
    """Return (violations, files_scanned). Violation = one offending LINE."""
    violations = []
    scanned = 0
    for path in sorted(scripts_dir.rglob("*.sh")):
        if _skip(path):
            continue
        scanned += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"[world-script-root-derivation-ratchet] "
                  f"(unreadable: {path}: {e})", file=sys.stderr)
            continue
        for lineno, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if not _PATHS_REF.search(line):
                continue
            if not _SOURCE_CMD.search(line):
                continue
            if _HONOURS_ROOT.search(line):
                continue
            if not _SELF_LOCATED.search(line):
                continue
            violations.append({
                "file": path.relative_to(scripts_dir).as_posix(),
                "line": lineno,
                "text": line[:200],
            })
    return violations, scanned


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="Emit JSON")
    ap.add_argument("--list", action="store_true",
                    help="List each offending line and exit (no baseline write)")
    args = ap.parse_args()

    scripts_dir = Path(WORLD_DIR) / "scripts" if WORLD_DIR else None
    if not scripts_dir or not scripts_dir.is_dir():
        # BLIND, not clean (guard-1947). Write nothing: a 0 seeded from a box
        # that cannot see the corpus makes every sighted box read REGRESSED.
        msg = (f"BLIND: world scripts dir not resolvable "
               f"({scripts_dir or 'WORLD_DIR unset'}) — corpus not scanned and "
               f"NO baseline was written. This is not a clean result.")
        if args.json:
            print(json.dumps({"verdict": "blind", "baseline": None,
                              "current": None, "message": msg}, indent=2))
        else:
            print(f"[world-script-root-derivation-ratchet] {msg}")
        return 0

    try:
        violations, scanned = _scan(scripts_dir)
    except Exception as e:
        print(f"ERROR: scan failed: {e}", file=sys.stderr)
        return 2

    by_file: dict = {}
    for v in violations:
        by_file[v["file"]] = by_file.get(v["file"], 0) + 1
    current = {
        "total": len(violations),
        "files": len(by_file),
        "scripts_scanned": scanned,
        "by_file": by_file,
    }

    if args.list:
        for v in violations:
            print(f"{v['file']}:{v['line']}: {v['text']}")
        print(f"--- {current['total']} offending line(s) in "
              f"{current['files']} file(s), {scanned} script(s) scanned ---")
        return 0

    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    captured: dict = {}

    def _modify(baselines):
        # Locked RMW — read the baseline INSIDE the lock; sibling ratchets
        # share this file (audit-baselines.md).
        if not isinstance(baselines, dict):
            baselines = {}
        entry = baselines.get(KEY) or {}
        prior = entry.get("baseline")
        total = current["total"]

        if prior is None:
            verdict, new_baseline = "seeded", total
            message = (
                f"Seeded baseline at {new_baseline} self-located _paths.sh "
                f"source(s) across {current['files']} file(s). Future runs warn "
                f"if the count grows, ratchet if repair shrinks it.")
        elif total > prior:
            verdict, new_baseline = "regressed", prior  # never raise
            message = (
                f"WARN: self-located _paths.sh sources grew from baseline "
                f"{prior} to {total} (+{total - prior}) — a NEW script derives "
                f"the framework root from its own location and will die rc=128 "
                f"wherever world/ is an external path. Inspect with "
                f"`bash core/scripts/world-script-root-derivation-ratchet.sh "
                f"--list`; fix per guard-5419 (honour an inherited "
                f"PROJECT_ROOT) and guard-6347.")
        elif total < prior:
            verdict, new_baseline = "ratcheted", total
            message = (
                f"OK: self-located _paths.sh sources shrank from baseline "
                f"{prior} to {total} (-{prior - total}). Baseline ratcheted "
                f"down.")
        else:
            verdict, new_baseline = "stable", prior
            message = (f"OK: self-located _paths.sh sources stable at baseline "
                       f"{total}.")

        history = entry.get("history") or []
        history.append({
            "recorded_at": now_iso,
            "drift_total": total,
            "verdict": verdict,
            "scripts_scanned": scanned,
            "breakdown": dict(by_file),
        })
        baselines[KEY] = {
            "baseline": new_baseline,
            "last_recorded": now_iso,
            "last_verdict": verdict,
            "unit": "self_located_paths_sh_source_lines",
            "polarity": "lower_is_better",
            "history": history[-50:],
        }
        captured["verdict"] = verdict
        captured["new_baseline"] = new_baseline
        captured["message"] = message
        return baselines

    try:
        locked_modify_yaml(BASELINES_PATH, _modify, initial={})
    except Exception as e:
        print(f"WARN: could not persist baseline to {BASELINES_PATH}: {e}",
              file=sys.stderr)
        # OVERWRITE, never setdefault — _modify populates `captured` BEFORE the
        # write, so a failed write would otherwise report the computed verdict
        # as though it had persisted. A tool must not claim a write it did not
        # make.
        computed = captured.get("verdict")
        captured["verdict"] = "error"
        captured["new_baseline"] = None
        captured["message"] = (
            f"baseline operation FAILED and nothing was persisted: {e}"
            + (f" (the computed verdict was '{computed}' — it did NOT take "
               f"effect)" if computed else ""))

    verdict = captured["verdict"]
    result = {
        "verdict": verdict,
        "baseline": captured["new_baseline"],
        "current": current,
        "message": captured["message"],
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"[world-script-root-derivation-ratchet] {verdict.upper()}: "
              f"{captured['message']}")

    if os.environ.get("VERIFY_LEARNING_DRIFT_HARD_GATE") == "1":
        return 1 if verdict == "regressed" else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
