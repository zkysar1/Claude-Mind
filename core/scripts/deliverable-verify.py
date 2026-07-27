#!/usr/bin/env python3
# deliverable-verify.py — 
#
# Verify a recurring goal's named `deliverable_file` was (re)generated since its
# prior close. Catches the rb-428 LLM-abbreviation drift where a recurring close
# advances `lastAchievedAt` WITHOUT the skill's deliverable-writing step having
# run (canonical: 's 2026-07-11 09:52 close bumped lastAchievedAt but no
# write touched agents/zeta/COMPLETION-REPORT.md between 2026-07-10 23:21 and
# 2026-07-12 — the LLM-gated Phase-4 write drifted while the bash-gated close
# proceeded).
#
# Contract:
#   Pure + side-effect-free (reads the goal record + one file's mtime; writes
#   nothing). FAIL-OPEN and FLAG-ONLY — exit code is ALWAYS 0; the verdict is a
#   single word on stdout. recurring-close.sh consumes the verdict and emits a
#   NON-BLOCKING warning on "stale"/"missing". A hard refuse is deliberately NOT
#   implemented: a false-stale mtime (e.g. an own-cloud stale pull, )
#   must never gate a legitimate close.
#
# Verdicts (stdout, one word):
#   skip     — no deliverable_file field, OR lastAchievedAt null (first close),
#              OR goal not found, OR any parse/IO error (fail-open)
#   advanced — deliverable mtime > lastAchievedAt (regenerated since prior close)
#   stale    — deliverable exists but mtime <= lastAchievedAt (NOT regenerated)
#   missing  — deliverable_file names a path that does not exist on disk
#
# The {agent} placeholder in deliverable_file expands to --agent, so a SHARED
# recurring goal (run by several agents under an MIND_AGENT override) can name a
# per-agent deliverable like "agents/{agent}/COMPLETION-REPORT.md" (rb-1556).
import argparse
import datetime
import json
import os
import sys


def verdict(goal_id, source_file, agent, project_root):
    try:
        deliverable = None
        last = None
        found = False
        with open(source_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                asp = json.loads(line)
                for g in asp.get("goals", []):
                    if g.get("id") == goal_id:
                        deliverable = g.get("deliverable_file")
                        last = g.get("lastAchievedAt")
                        found = True
                        break
                if found:
                    break
        if not found or not deliverable:
            return "skip"          # field absent → nothing to verify
        if not last:
            return "skip"          # first close, no baseline to compare against
        path = deliverable.replace("{agent}", agent)
        if not os.path.isabs(path):
            path = os.path.join(project_root, path)
        if not os.path.exists(path):
            return "missing"
        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(path))
        last_dt = datetime.datetime.fromisoformat(str(last)[:19])
        return "advanced" if mtime > last_dt else "stale"
    except Exception:
        return "skip"              # fail-open — never flag falsely, never block


def main():
    ap = argparse.ArgumentParser(description="Verify a recurring goal's deliverable_file was regenerated since its prior close (g-115-2036).")
    ap.add_argument("--goal-id", required=True)
    ap.add_argument("--source-file", required=True, help="aspirations.jsonl holding the goal")
    ap.add_argument("--agent", required=True, help="value substituted for the {agent} placeholder")
    ap.add_argument("--project-root", required=True, help="anchor for a relative deliverable_file")
    a = ap.parse_args()
    print(verdict(a.goal_id, a.source_file, a.agent, a.project_root))
    sys.exit(0)  # always 0 — flag helper, not a gate


if __name__ == "__main__":
    main()
