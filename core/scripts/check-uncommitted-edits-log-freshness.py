#!/usr/bin/env python3
"""Verify uncommitted-edits.jsonl logs are free of regressions 5 fixed.

The two silent bugs g-115-1125 fixed in uncommitted-edits-record.sh:
  1. Neutral-path filter under AGENTS_PARENT_DIR=agents — entries like
     "agents/<name>/..." should be filtered out (they're agent-private),
     not appended as if they were neutral-path edits.
  2. Windows absolute-path normalization — entries should land as repo-
     relative paths ("core/scripts/foo.py"), not absolute Windows form
     ("C:/path/to/project/core/scripts/foo.py").

A regression in either fix is silent — the log keeps appending, but
iteration-commit's cross-agent attribution filter misclassifies entries
and the wrong agent's signature lands on the commit (the rb-1127 /
3c4a61c4 incident class).

Check form: for each agent's session/uncommitted-edits.jsonl, count
entries since BASELINE that match either regression pattern. FAIL if
any post-BASELINE entry trips either pattern. Pre-BASELINE entries are
tolerated as historical noise (the bug WAS appending them before the
fix landed).

Exit 0 on PASS, 1 on FAIL with per-agent details.

Filed by g-115-1129 (Maintain). Consumed by Section UER in
.claude/skills/verify-learning/SKILL.md.
"""
import json
import re
import sys
from pathlib import Path

# 5 fix landed in commit 3c4a61c4 at 2026-05-22T12:26:02 (per
# `git log core/scripts/uncommitted-edits-record.sh`). The BASELINE sits
# immediately after that commit so pre-fix log entries (with absolute
# Windows paths and missing AGENTS_PARENT_DIR-aware filtering) are
# tolerated as historical noise — they're proof of the bug the fix
# closed. Only entries appended AFTER this baseline should pass through
# the regression patterns.
#
# Tunable via env var (UNCOMMITTED_EDITS_BASELINE) for backtest scenarios
# or to rebase forward after a clean log purge.
import os
BASELINE = os.environ.get("UNCOMMITTED_EDITS_BASELINE", "2026-05-22T12:30:00")

# Regression pattern 1: entry starts with "agents/" (filter regression).
# Only fires on the rel-path form — absolute paths containing "/agents/"
# trigger pattern 2 instead.
APD_PREFIX = re.compile(r"^agents/")

# Regression pattern 2: entry is an absolute path. Windows drive-letter
# (C:/...) or POSIX single-letter-root (/c/...) both indicate the rel-path
# conversion silently failed.
ABS_PATH = re.compile(r"^[A-Za-z]:/|^/[a-zA-Z]/")


def main() -> int:
    agents_dir = Path("agents")
    if not agents_dir.exists():
        print("SKIP: agents/ dir not present (legacy AGENTS_PARENT_DIR layout)")
        return 0

    filter_hits: list[tuple[str, str, str]] = []
    abs_hits: list[tuple[str, str, str]] = []
    checked = 0

    for agent_dir in sorted(agents_dir.iterdir()):
        if not agent_dir.is_dir():
            continue
        log = agent_dir / "session" / "uncommitted-edits.jsonl"
        if not log.exists():
            continue
        try:
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            edit_ts = rec.get("edit_ts", "")
            if edit_ts < BASELINE:
                continue
            checked += 1
            f = rec.get("file", "")
            if APD_PREFIX.match(f):
                filter_hits.append((agent_dir.name, edit_ts, f))
            if ABS_PATH.match(f):
                abs_hits.append((agent_dir.name, edit_ts, f))

    if filter_hits or abs_hits:
        print(
            f"FAIL: uncommitted-edits.jsonl regression detected since {BASELINE} "
            f"(g-115-1125 fix undone, rb-1127 attribution risk): "
            f"{len(filter_hits)} agents/-prefix entries (neutral-path filter regression), "
            f"{len(abs_hits)} absolute-path entries (Windows path normalization regression)"
        )
        for agent, ts, f in filter_hits[:5]:
            print(f"  [filter]  {agent}@{ts}: {f[:100]}")
        for agent, ts, f in abs_hits[:5]:
            print(f"  [abspath] {agent}@{ts}: {f[:100]}")
        return 1

    print(
        f"PASS: {checked} post-{BASELINE} log entries across all agents "
        f"pass neutral-path filter + Windows path normalization checks "
        f"(g-115-1125 fix intact)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
