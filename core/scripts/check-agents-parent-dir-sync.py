#!/usr/bin/env python3
"""Verify AGENTS_PARENT_DIR / SESSIONS_DIRNAME / SESSION_DIRNAME are in sync
across all 12 declared sites per CLAUDE.md "Agent-dir Resolution" table.

Canonical source: core/scripts/_paths.sh (the IRREDUCIBLY LOCAL shell helper
that all bash callers source).

Failure mode this catches: a partial edit changes the constant in some sites
but misses others, silently re-routing agent-dir resolution on the hot path.
The 2026-05-19 incident (rb-1092, guard-587) was caught by manual inspection;
this check makes it a fail-loud verify-learning gate. The 2026-05-20 incident
(_world_config.py inherited the pre-relocation `root/agent/local-paths.conf`
shape for ~3 weeks) reinforced the need for an automated drift detector.

Exit 0 on PASS, 1 on FAIL with per-drift details.

Filed by g-115-1062 (Idea: Add verify-learning check for AGENTS_PARENT_DIR
sync across all 12 sites). Consumed by Section APD12 in
.claude/skills/verify-learning/SKILL.md.
"""
import re
import sys
from pathlib import Path

# Canonical source — the shell helper that all bash callers source.
CANON_PATH = "core/scripts/_paths.sh"

# 12 sync sites with per-file regex extraction patterns.
# Source: CLAUDE.md "Agent-dir Resolution" tables (framework-layer + inlined).
# Format: (path, [(regex_pattern, canon_key), ...])
SITES = [
    # Framework-layer sync (6 sites — _paths.sh is canon, not self-checked):
    ("core/scripts/_paths.py", [
        (r'^AGENTS_PARENT_DIR\s*=\s*"([^"]*)"', "AGENTS_PARENT_DIR"),
        (r'^SESSIONS_DIRNAME\s*=\s*"([^"]*)"', "SESSIONS_DIRNAME"),
        (r'^SESSION_DIRNAME\s*=\s*"([^"]*)"', "SESSION_DIRNAME"),
    ]),
    ("mind_api/src/agent_paths.py", [
        (r'^AGENTS_PARENT_DIR\s*=\s*"([^"]*)"', "AGENTS_PARENT_DIR"),
        (r'^SESSIONS_DIRNAME\s*=\s*"([^"]*)"', "SESSIONS_DIRNAME"),
        (r'^SESSION_DIRNAME\s*=\s*"([^"]*)"', "SESSION_DIRNAME"),
    ]),
    ("core/scripts/_agents.py", [
        (r'^AGENTS_PARENT_DIR\s*=\s*"([^"]*)"', "AGENTS_PARENT_DIR"),
        (r'^SESSIONS_DIRNAME\s*=\s*"([^"]*)"', "SESSIONS_DIRNAME"),
        (r'^SESSION_DIRNAME\s*=\s*"([^"]*)"', "SESSION_DIRNAME"),
    ]),
    ("core/scripts/path-resolution-hook.py", [
        (r'^AGENTS_PARENT_DIR\s*=\s*"([^"]*)"', "AGENTS_PARENT_DIR"),
        (r'^SESSIONS_DIRNAME\s*=\s*"([^"]*)"', "SESSIONS_DIRNAME"),
        (r'^SESSION_DIRNAME\s*=\s*"([^"]*)"', "SESSION_DIRNAME"),
    ]),
    ("core/scripts/_world_config.py", [
        (r'^AGENTS_PARENT_DIR\s*=\s*"([^"]*)"', "AGENTS_PARENT_DIR"),
    ]),
    ("core/scripts/_session_binding.py", [
        (r'^_AGENTS_PARENT_DIR\s*=\s*"([^"]*)"', "AGENTS_PARENT_DIR"),
        (r'^_SESSIONS_DIRNAME\s*=\s*"([^"]*)"', "SESSIONS_DIRNAME"),
    ]),
    # Inlined hot-path copies (5 sites, by-hand mirrors per IRREDUCIBLY LOCAL):
    ("core/scripts/cleanup-stale-bindings.sh", [
        (r'^_APD="([^"]*)"', "AGENTS_PARENT_DIR"),
        (r'^_SDN="([^"]*)"', "SESSIONS_DIRNAME"),
    ]),
    ("core/scripts/session-mode-get.sh", [
        (r'^_APD="([^"]*)"', "AGENTS_PARENT_DIR"),
    ]),
    ("core/scripts/session-signal-exists.sh", [
        (r'^_APD="([^"]*)"', "AGENTS_PARENT_DIR"),
    ]),
    ("core/scripts/session-state-get.sh", [
        (r'^_APD="([^"]*)"', "AGENTS_PARENT_DIR"),
    ]),
    ("core/scripts/_wake_signals.py", [
        (r'^_AGENTS_PARENT_DIR\s*=\s*"([^"]*)"', "AGENTS_PARENT_DIR"),
    ]),
]


def main():
    # Resolve PROJECT_ROOT from script location (this file lives in core/scripts/).
    here = Path(__file__).resolve()
    project_root = here.parent.parent.parent

    canon_path = project_root / CANON_PATH
    if not canon_path.exists():
        print(f"FAIL: canonical source {CANON_PATH} missing", file=sys.stderr)
        sys.exit(1)
    canon_text = canon_path.read_text(encoding="utf-8")
    canon = {}
    for cname in ("AGENTS_PARENT_DIR", "SESSIONS_DIRNAME", "SESSION_DIRNAME"):
        m = re.search(rf'^{cname}="([^"]*)"', canon_text, re.MULTILINE)
        if not m:
            print(f"FAIL: canonical {cname} not found in {CANON_PATH}", file=sys.stderr)
            sys.exit(1)
        canon[cname] = m.group(1)

    fails = []
    sites_ok = 0
    for rel_path, patterns in SITES:
        p = project_root / rel_path
        if not p.exists():
            fails.append(f"{rel_path}: file missing")
            continue
        text = p.read_text(encoding="utf-8")
        site_ok = True
        for pat, key in patterns:
            m = re.search(pat, text, re.MULTILINE)
            if not m:
                fails.append(f"{rel_path}: pattern not found for {key}")
                site_ok = False
                continue
            value = m.group(1)
            if value != canon[key]:
                fails.append(
                    f'{rel_path}: {key}="{value}" drifted from canonical '
                    f'"{canon[key]}" (CLAUDE.md Agent-dir Resolution)'
                )
                site_ok = False
        if site_ok:
            sites_ok += 1

    if fails:
        print(
            f"FAIL: AGENTS_PARENT_DIR/SESSIONS_DIRNAME/SESSION_DIRNAME drift "
            f"across {len(SITES)} sites (CLAUDE.md Agent-dir Resolution, "
            f"rb-1092, guard-587, g-115-1062):"
        )
        for f in fails:
            print(f"  {f}")
        sys.exit(1)

    print(
        f"PASS: all {sites_ok}/{len(SITES)} sites sync with canonical "
        f'AGENTS_PARENT_DIR="{canon["AGENTS_PARENT_DIR"]}", '
        f'SESSIONS_DIRNAME="{canon["SESSIONS_DIRNAME"]}", '
        f'SESSION_DIRNAME="{canon["SESSION_DIRNAME"]}"'
    )


if __name__ == "__main__":
    main()
