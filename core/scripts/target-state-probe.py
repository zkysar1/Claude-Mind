#!/usr/bin/env python3
"""Target-State Probe — execution-time advisory check.

Fires at the start of Phase 4 of the aspirations loop. Extracts target
files + identifiers from the goal description (shared extractor with
goal-duplication-gate.py), greps each file, and emits a JSON verdict on
stdout. Stdout is the SINGLE SOURCE OF TRUTH — this probe writes no
files, sets no working-memory slots, and has no side effects. The
caller (aspirations-execute Phase 4-pre) reads the JSON and journals
the summary.

ADVISORY, not blocking. Always exits 0 unless the script itself crashes
OR it was called with no goal data (exit 2). Phase 5 verification
remains ground truth — this probe just short-circuits expensive
retrieval + skill invocation when evidence says the work is done.

Exit codes:
  0 = probe completed (any verdict, including "unknown")
  2 = framework error (no --goal-id, no --goal-json, no stdin)

Inputs:
  --goal-id <g-NNN-NN>    Resolve via aspirations-read.sh, or
  --goal-json <path>|-    Read JSON from file or stdin (same schema as
                          goal-duplication-gate: {title, description})

Output (stdout JSON):
  {
    "goal_id": "...",
    "extraction": { target_files, identifiers, line_hints, confidence },
    "probe":      { verdict, per_file, total_hits, ..., line_hint_verifications },
    "summary": "advisory string"
  }

Origin: 2026-04-20 g-115-141 (bravo filed an implementation goal after
g-115-137 had already landed the fix; execution revealed the identifiers
were already present).
"""

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

sys.path.insert(0, str(SCRIPT_DIR))

# : force utf-8 on stdin/stdout/stderr (covers Windows cp1252 fallback
# when callers bypass the _platform.sh PYTHONIOENCODING=utf-8 shim).
from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

from _target_state import (  # noqa: E402
    _resolve_search_roots,
    extract_and_infer_targets,
    extract_targets,
    is_read_intent,
    is_removal_intent,
    probe_target_state,
)


def _resolve_goal_by_id(goal_id):
    """Resolve a goal_id to its full goal dict by reading aspirations.jsonl
    files directly.

    Bug history (g-248-55): the original implementation called
    aspirations-read.sh with --id <goal_id> and a non-existent --json flag.
    aspirations-read.sh's --id parameter expects an aspiration_id (asp-NNN),
    not a goal_id (g-NNN-NN), so every call returned empty + verdict=unknown.
    The probe was effectively dead code post-MSYS-fix until this rewrite.

    Uses direct JSONL reads instead of shelling out to aspirations-query.sh
    + aspirations-read.sh — the bash-subprocess path is fragile under MSYS
    on Windows and added two fork+exec calls per probe. Direct reads are
    O(N) over both the world + agent jsonl files, and the file sizes here
    are small enough (<1MB total) that the linear scan is faster than the
    subprocess overhead anyway.

    Returns (goal_dict, err_str). err_str is None on success; goal_dict is
    None on failure.
    """
    # Lazily resolve world + agent aspirations.jsonl paths via _paths.
    try:
        import _paths  # noqa: E402 — late import; same lazy pattern as elsewhere
    except Exception as e:
        return None, "_paths import failed: " + str(e)

    world_path = _paths.WORLD_DIR / "aspirations.jsonl"
    agent_dir = getattr(_paths, "AGENT_DIR", None)
    agent_path = (agent_dir / "aspirations.jsonl") if agent_dir else None

    candidate_files = [p for p in (world_path, agent_path) if p and p.exists()]
    if not candidate_files:
        return None, "no aspirations.jsonl found in world or agent dirs"

    for path in candidate_files:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        asp = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    for g in asp.get("goals", []):
                        if g.get("id") == goal_id:
                            return g, None
        except OSError as e:
            # Best-effort: if one file errors we still try the next.
            return None, "read error on " + str(path) + ": " + str(e)
    return None, "goal_id " + goal_id + " not found in any aspiration"


def _human_summary(ex, pr):
    if pr.get("verdict") == "skipped_read_intent":
        return ("READ-intent goal title — target_state check skipped "
                "(identifiers in target files are a precondition, not a "
                "duplication signal)")
    if pr.get("verdict") == "skipped_removal_intent":
        return ("REMOVAL-intent goal title — target_state check skipped "
                "(identifiers in target files are the removal TARGET, so "
                "their presence means the work is NOT done, not a "
                "duplication signal)")
    if ex["confidence"] == "none":
        return "no target files extracted from goal — probe is no-op"
    if not ex["identifiers"]:
        return ("target file(s) {" + ", ".join(ex["target_files"]) +
                "} but no identifiers extracted — skipping grep")
    verdict = pr["verdict"]
    ratio = pr.get("hit_ratio", 0.0)
    files = ", ".join(ex["target_files"])
    if verdict == "already_present":
        return ("LIKELY ALREADY DONE: " + str(pr["total_hits"]) + "/" +
                str(pr["total_identifiers"]) + " identifiers present in " + files +
                " (hit_ratio=" + str(ratio) + "). Consider verify-only path.")
    if verdict == "partially_present":
        return ("PARTIAL: " + str(pr["total_hits"]) + "/" + str(pr["total_identifiers"]) +
                " identifiers present in " + files + " — proceed with normal execution.")
    if verdict == "absent":
        return ("GENUINE WORK: 0/" + str(pr["total_identifiers"]) +
                " identifiers present in " + files + " — proceed with normal execution.")
    return "verdict=unknown — target files unreadable or out of project"


def _load_goal_from_args(args):
    """Resolve a goal dict from --goal-id, --goal-json, or stdin.
    Returns (goal_dict_or_None, goal_id_str, err_or_None)."""
    if args.goal_id:
        goal, err = _resolve_goal_by_id(args.goal_id)
        return goal, args.goal_id, err
    if args.goal_json:
        try:
            raw = sys.stdin.read() if args.goal_json == "-" else Path(args.goal_json).read_text(encoding="utf-8")
            goal = json.loads(raw)
            gid = goal.get("id") or goal.get("goal_id") or ""
            return goal, gid, None
        except Exception as e:
            return None, "", "bad JSON input: " + str(e)
    if not sys.stdin.isatty():
        try:
            goal = json.loads(sys.stdin.read())
            gid = goal.get("id") or goal.get("goal_id") or ""
            return goal, gid, None
        except Exception as e:
            return None, "", "bad stdin JSON: " + str(e)
    return None, "", "need --goal-id, --goal-json, or stdin JSON"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--goal-id", default=None)
    ap.add_argument("--goal-json", default=None,
                    help="File path or '-' for stdin.")
    ap.add_argument("--output", choices=["json", "human"], default="json")
    args = ap.parse_args(argv)

    goal, goal_id, resolve_err = _load_goal_from_args(args)

    if goal is None and resolve_err == "need --goal-id, --goal-json, or stdin JSON":
        print("target-state-probe: " + resolve_err, file=sys.stderr)
        return 2

    if goal is None:
        # Goal requested but not resolvable. Fail-open: emit verdict=unknown.
        result = {
            "goal_id": goal_id,
            "extraction": {"target_files": [], "identifiers": [],
                           "line_hints": {}, "confidence": "none"},
            "probe": {"verdict": "unknown", "per_file": [],
                      "total_hits": 0, "total_identifiers": 0,
                      "hit_ratio": 0.0, "line_hint_verifications": [],
                      "reason": resolve_err or "no goal data"},
            "summary": "probe no-op: " + (resolve_err or "no goal data"),
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    # READ-intent short-circuit (rb-398). Investigate / Audit / Review /
    # Observe / Research / Analyze titles describe work that READS the
    # target files — for them "identifiers already present" is a
    # precondition, not a duplication signal. Emit verdict=skipped_read_intent
    # so the execution-time advisory doesn't print misleading "already
    # present" language. Shared with goal-duplication-gate via
    # _target_state.is_read_intent so both sites cannot diverge.
    if is_read_intent(goal.get("title"), _caller="target-state-probe.py"):
        # Short-circuit BEFORE extraction: we did not attempt to extract, so
        # confidence="none" matches extract_targets()'s documented enum
        # (high|medium|low|none). The READ-intent signal lives on the verdict
        # side — "skipped_read_intent" is the probe's main()-level boundary
        # value, distinct from the four verdicts produced inside
        # probe_target_state (already_present / partially_present / absent /
        # unknown). Do NOT conflate the two layers by inventing a fifth
        # confidence value; the enum is the SSOT.
        ex = {"target_files": [], "identifiers": [],
              "line_hints": {}, "confidence": "none"}
        pr = {"verdict": "skipped_read_intent", "per_file": [],
              "total_hits": 0, "total_identifiers": 0,
              "hit_ratio": 0.0, "line_hint_verifications": [],
              "reason": "READ-intent goal title — target_state check inverted"}
    # REMOVAL-intent short-circuit () — mirror inversion of the
    # READ-intent skip above: for retire/remove/delete goals the named
    # identifiers are present BECAUSE they are the removal target, so an
    # "already present" advisory would be exactly backwards. Shared
    # classifier (_target_state.is_removal_intent) — both consumers cannot
    # diverge. Same two-layer contract as skipped_read_intent: boundary
    # verdict at main() level, confidence enum untouched.
    elif is_removal_intent(goal.get("title"), _caller="target-state-probe.py"):
        ex = {"target_files": [], "identifiers": [],
              "line_hints": {}, "confidence": "none"}
        pr = {"verdict": "skipped_removal_intent", "per_file": [],
              "total_hits": 0, "total_identifiers": 0,
              "hit_ratio": 0.0, "line_hint_verifications": [],
              "reason": "REMOVAL-intent goal title — identifier presence is the removal target, not completion"}
    else:
        # : pass search_roots so the inference fallback can walk
        # PROJECT_ROOT + AGENT_WRITE_PATH for class-shaped identifiers when
        # the goal text omitted the file path.
        search_roots = _resolve_search_roots()
        ex = extract_and_infer_targets(
            goal.get("title"), goal.get("description"),
            search_roots=search_roots,
        )
        if ex["confidence"] == "none" or not ex["identifiers"]:
            # No file or no identifiers — nothing to test.
            pr = {"verdict": "unknown", "per_file": [],
                  "total_hits": 0, "total_identifiers": len(ex["identifiers"]),
                  "hit_ratio": 0.0, "line_hint_verifications": [],
                  "reason": "extraction confidence=" + ex["confidence"]}
        else:
            pr = probe_target_state(
                PROJECT_ROOT,
                ex["target_files"],
                ex["identifiers"],
                ex["line_hints"],
                allowed_roots=search_roots,
                lenient_match=ex.get("target_files_inferred", False),
            )

    payload = {
        "goal_id": goal_id,
        "extraction": ex,
        "probe": pr,
        "summary": _human_summary(ex, pr),
    }

    if args.output == "human":
        print(payload["summary"])
        print("  verdict: " + pr["verdict"] + "   hits: " +
              str(pr.get("total_hits", 0)) + "/" + str(pr.get("total_identifiers", 0)))
        print("  files:   " + (", ".join(ex["target_files"]) or "(none)"))
        print("  ids:     " + (", ".join(ex["identifiers"][:8]) or "(none)"))
    else:
        print(json.dumps(payload, indent=2, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    sys.exit(main())
