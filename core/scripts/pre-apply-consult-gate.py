#!/usr/bin/env python3
"""Pre-apply consult gate — force retrieve.sh consult before Phase-4 edits
when executing an inherited cross-agent spec that touches framework files.

Origin: g-115-826 (asp-115). Incident shape: g-115-796 / rb-987.

g-115-796 was an Apply of zeta's spec g-115-775. Phase-4 of aspirations-
execute skipped the code-review-protocol step-4 pre-apply retrieve.sh
consult; the corrupt spec ("empty body -> exec [] + stderr-only") shipped
in commit 8be45e1 pinned by 9/9 passing tests. The guard-383 violation
was caught retroactively by aspirations-learning-gate's retrieval-audit
safety net (fixed at 28a3b7a). A test suite pinning the spec pins the
violation too -- 9/9 green doesn't falsify a guard miss.

This gate is the post-hoc -> pre-hoc move: shift the consult-before-edit
discipline from after-the-fact audit to before-the-fact directive.

Trigger conditions (ALL three required):
  1. goal.handoff_from is set AND != $MIND_AGENT (cross-agent Apply)
  2. goal title or description references a framework-file path:
     core/scripts/, core/config/, mind_api/src/, core/githooks/,
     core/logs/, .claude/skills/, .claude/rules/, .claude/settings,
     world/conventions/, SKILL.md, CLAUDE.md
  3. agent + goal record can be resolved

Posture: ADVISORY-LOUD (large banner to stdout) but NOT loop-blocking
(always exits 0). Fail-open on parse/path/env errors with a stderr note.
The LLM may proceed without retrieval and own the risk, but cannot later
claim the directive wasn't visible.

Usage:
  pre-apply-consult-gate.py <goal-id> [--queue-file PATH]

  --queue-file: testing-only override. Reads goal record from the given
                JSONL instead of the resolved world/agent queues.

Exit: always 0.
"""
import argparse
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

sys.path.insert(0, str(SCRIPT_DIR))
from _paths import agent_dir as _agent_dir  # noqa: E402


# Substring needles checked case-insensitively against title + description.
# Substrings (not regex) keep the check robust against weird quoting in
# free-form goal descriptions. False positives only cost the LLM a 5-second
# retrieve.sh consultation; false negatives are the failure mode we are
# guarding against, so prefer over- to under-fire.
FRAMEWORK_FILE_NEEDLES = (
    "core/scripts/",
    "core/config/",
    "mind_api/src/",
    "core/githooks/",
    "core/logs/",
    ".claude/skills/",
    ".claude/rules/",
    ".claude/settings",
    "world/conventions/",
    "skill.md",
    "claude.md",
)


def _read_local_paths_conf(agent_name: str) -> dict:
    conf = _agent_dir(agent_name) / "local-paths.conf"
    out: dict = {}
    if not conf.is_file():
        return out
    try:
        for line in conf.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        return {}
    return out


def _resolve_queues(agent: str):
    """Return list of JSONL paths to scan (world first, then agent)."""
    paths = []
    conf = _read_local_paths_conf(agent)
    wp = conf.get("WORLD_PATH")
    if wp:
        p = Path(wp) / "aspirations.jsonl"
        if p.is_file():
            paths.append(p)
    agent_jsonl = _agent_dir(agent) / "aspirations.jsonl"
    if agent_jsonl.is_file():
        paths.append(agent_jsonl)
    return paths


def _find_goal(goal_id: str, jsonl: Path):
    """Scan a JSONL queue for the goal record. Returns the goal dict or None."""
    try:
        with jsonl.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    asp = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                goals = asp.get("goals", []) or []
                for g in goals:
                    if isinstance(g, dict) and g.get("id") == goal_id:
                        return g
    except OSError:
        return None
    return None


def _detect_framework_refs(title: str, description: str):
    """Case-insensitive substring scan; returns ordered list of matched needles."""
    text = (title + "\n" + description).lower()
    hits = []
    for needle in FRAMEWORK_FILE_NEEDLES:
        if needle in text:
            hits.append(needle)
    return hits


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Pre-apply consult gate (advisory, fail-open).",
        add_help=True,
    )
    ap.add_argument("goal_id", help="Goal ID to evaluate.")
    ap.add_argument("--queue-file",
                    help="Testing-only: read goal records from this JSONL "
                         "instead of the resolved world/agent queues.")
    args = ap.parse_args(argv)

    goal_id = (args.goal_id or "").strip()
    if not goal_id:
        return 0

    agent = os.environ.get("MIND_AGENT", "").strip()
    if not agent:
        print("[pre-apply-consult-gate] WARN: MIND_AGENT unset -- failing open",
              file=sys.stderr)
        return 0

    if args.queue_file:
        qpath = Path(args.queue_file)
        queues = [qpath] if qpath.is_file() else []
        if not queues:
            print(f"[pre-apply-consult-gate] WARN: --queue-file not found: "
                  f"{args.queue_file} -- failing open", file=sys.stderr)
            return 0
    else:
        queues = _resolve_queues(agent)

    goal = None
    for q in queues:
        goal = _find_goal(goal_id, q)
        if goal is not None:
            break

    if goal is None:
        # Goal record not found -- fail open silently. Most likely a queue
        # source mismatch or the daemon hasn't flushed the record yet;
        # Phase 4's `aspirations-update-goal.sh ... status in-progress`
        # already landed the record, so this case is rare.
        return 0

    handoff_from = (goal.get("handoff_from") or "").strip()
    if not handoff_from or handoff_from == agent:
        # Own-authored or no handoff -- gate does not fire.
        return 0

    title = (goal.get("title") or "").strip()
    description = (goal.get("description") or "").strip()
    hits = _detect_framework_refs(title, description)
    if not hits:
        # Cross-agent handoff but no framework-file reference -- gate does not fire.
        return 0

    one_line = title.split("\n")[0]
    if len(one_line) > 120:
        one_line = one_line[:117] + "..."

    refs = ", ".join(sorted(set(hits)))

    out = sys.stdout
    out.write("\n")
    out.write("=== PRE-APPLY CONSULT GATE ===========================================\n")
    out.write(f"Goal:        {goal_id}\n")
    out.write(f"Title:       {one_line}\n")
    out.write(f"Inherited:   handoff_from={handoff_from} (current agent: {agent})\n")
    out.write(f"Refs:        {refs}\n")
    out.write("\n")
    out.write("Per .claude/rules/code-review-protocol.md step 4 + rb-987 (g-115-796\n")
    out.write("incident): BEFORE first Edit, run retrieve.sh to surface reasoning-bank\n")
    out.write("entries and guardrails that may CONTRADICT the inherited spec. A test\n")
    out.write("suite pinning the spec pins the violation too -- 9/9 passing tests do\n")
    out.write("NOT falsify a guard violation.\n")
    out.write("\n")
    out.write("Recommended invocation:\n")
    out.write(f'  bash core/scripts/retrieve.sh --category "{one_line[:80]}" --depth shallow\n')
    out.write("\n")
    out.write("Then read returned reasoning_bank + guardrails. If any CONTRADICTS the\n")
    out.write("intended fix: STOP -- re-evaluate. Apply the entry's pattern instead,\n")
    out.write("OR retire the entry with justification if genuinely stale. If an entry\n")
    out.write("REINFORCES the fix, proceed and increment utilization on the entry.\n")
    out.write("\n")
    out.write("Posture: advisory, not loop-blocking. Fail-open. LLM owns the risk.\n")
    out.write("======================================================================\n")
    out.write("\n")
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
