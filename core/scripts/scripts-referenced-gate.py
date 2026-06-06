#!/usr/bin/env python3
"""Scripts-Referenced Gate — detect orphan scripts in core/scripts/.

Flags any `core/scripts/*.sh` or `core/scripts/*.py` file that is not
referenced by any SKILL.md, rule, config, settings file, or other script.
Journal / experience / changelog files are intentionally excluded — those
are historical records of past work, not live call sites. If the only
reference to a script is a journal entry, the script is effectively orphan.

Designed to run monthly (or on demand) to catch scripts left behind after
refactors. Exit code 1 if any orphan found; exit 0 otherwise. Each orphan
entry includes a "likely_cause" hint where deducible from filename
patterns (legacy `mind-*`, `path-resolution-*` pre-refactor, etc.).

Contract
  --json (default) | --text
  --exclude <name1,name2>   extra basenames to skip (comma-separated)

Fail-open: unreadable roots yield empty orphan set, exit 0.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from _paths import CORE_ROOT, PROJECT_ROOT

SCRIPTS_DIR = CORE_ROOT / "scripts"

# git hooks live here (invoked via `core.hooksPath=core/githooks`). The hook
# files (pre-commit, post-commit) are EXTENSIONLESS, so the REF_EXTS suffix
# filter in _collect_reference_text would skip them — a script referenced ONLY
# by a hook would false-flag as orphan (). Scanned as a reference
# surface with NO extension filter.
GITHOOKS_DIR = CORE_ROOT / "githooks"

# Hook-bound scripts: any core/scripts/*.sh or *.py named in a `command:`
# field of .claude/settings.json hooks. These are invoked by the Claude
# Code harness, not by grep-able bash calls in the repo, so they would
# otherwise drift to "orphan" status whenever a new hook is registered
# unless someone remembers to add a manual exemption (see ).
# This auto-discovery removes that whole drift class. Fail-open: any
# parse error falls back to ALWAYS_EXEMPT only, never crashes the gate.
SETTINGS_JSON = PROJECT_ROOT / ".claude" / "settings.json"

# Regex for "bash <path>/<script>.sh" or "python3 <path>/<script>.py" or
# "py -3 <path>/<script>.py" inside a hook command string. Matches any
# core/scripts script name, regardless of leading path prefix.
_SCRIPT_BASENAME_RE = re.compile(
    r"(?:[\w./\\-]*?/)?([\w.-]+\.(?:sh|py))(?:\s|$)"
)

# Files sourced-not-invoked (internal helpers). They're referenced via
# `source` / `from _module import ...` which the name-based grep catches,
# but the `_` prefix is framework convention so we skip the orphan check
# for them — an `_` helper with no callers is still an orphan, just the
# orphan-report flags it as helper-orphan rather than script-orphan.
HELPER_PREFIX = "_"

# Roots where "live" references can exist. Journals/experiences/changelogs
# are historical and excluded — a script referenced only in a journal is
# effectively dead.
LIVE_REF_ROOTS = [
    PROJECT_ROOT / ".claude",
    CORE_ROOT / "config",
    CORE_ROOT / "scripts",
]

# Additional single-file references.
LIVE_REF_FILES = [
    PROJECT_ROOT / "CLAUDE.md",
]

# File extensions to scan for references.
REF_EXTS = {".md", ".sh", ".py", ".yaml", ".yml", ".json"}

# Always-exempt basenames (intentionally kept for external invocation even
# if no in-repo caller — e.g., a script invoked by cron, CI, or manual
# operator use). Extend via --exclude at call site; do not silently bloat
# this list without journaling the decision.
#
# IMPORTANT: when adding to this list, include an inline comment naming the
# invocation channel (human operator, CI pipeline, cron). A bare addition
# silently turns a real orphan into a permanent zombie.
ALWAYS_EXEMPT = {
    # Operator-facing utilities — invoked manually by humans, not in-repo code
    "audit-paths.sh",          # operator: prune settings.local.json allowlist
    "rename-agent.sh",         # operator: rename agent directory + refs
    "insights-read.sh",        # operator: read insights.jsonl captured by stop hook
    "tree-reconcile-capabilities.sh",  # operator: reconcile tree-node capability
                                       # drift on-demand; wraps live `tree.py
                                       # update --reconcile-capabilities` code
                                       # path (cmd_reconcile_capabilities in
                                       # tree.py). Used 2026-04-17 for initial
                                       # 151-node migration; kept for recurrence.
    "scoring-criterion-audit.sh",      # operator: per-criterion field-coverage
                                       # recommender consumed by humans, not
                                       # SKILL.md. The .py docstring documents
                                       # the manual invocation pattern (`py -3
                                       # core/scripts/scoring-criterion-audit.py`).
                                       # Sister to scoring-criteria.yaml.
    # Regression test harnesses — invoked manually or by future CI
    "test-capability-gate.sh", # locks in session-47 B2 capability-gate fixes
    # One-shot migration backfills — run manually ONCE, kept for recurrence.
    # Plans/headers document the migration they belong to.
    "checks-backfill.sh",       # Tier 1b one-time migration: backfill
                                # verification.checks[] from templates or
                                # legacy completion_check. Plan:
                                # ~/.claude/plans/i-had-one-agent-luminous-reddy.md
                                # (Tier 1b #3). Added 2026-04-20.
    "origin-signal-backfill.py",# One-shot backfill of goal.origin_signal
                                # before flipping origin-signal-gate to
                                # strict. Safe to re-run — already-tagged
                                # goals are left alone. Added 2026-04-20.
    # Hook-registered — invoked by the Claude Code harness via PreToolUse
    # (Write/Edit/MultiEdit) per .claude/settings.json, not by a grep-able
    # bash call. L1 of the 3-layer path defense (L2 permission gate, L3
    # prime/SKILL.md:50 validate-paths). The reference is in settings.json,
    # which this gate doesn't scan — exemption is permanent.
    "path-resolution-hook.sh",
    "path-resolution-hook.py", # body for path-resolution-hook.sh
                               # (paired-file convention, same wrapper pattern
                               # as every other gate in this dir).
    # These gates' own wrappers — the gates themselves get invoked only via
    # verify-learning/SKILL.md wiring (Task #9 of the post-/verify-learning
    # plan). The pattern (a gate whose only caller is the verifier) is
    # legitimate and worth exempting permanently.
    "scripts-referenced-gate.sh",
    "signal-lifecycle-gate.sh",
    "skill-structure-gate.sh",
    "skill-branch-terminator-audit.sh",  # built by parallel session — audits
                                          # SKILL.md procedural-fence
                                          # terminators for text-ending-branch
                                          # bugs (companion to Return Protocol
                                          # check). PENDING verify-learning
                                          # wiring; follow the precedent in
                                          # Section S49 for skill-structure-
                                          # gate when wiring. Until wired, the
                                          # gate is agent-runnable but not
                                          # enforced per /verify-learning.
    "infra-streak-notify.sh",  #  wrapper. Built 2026-04-21: runs
                               # infra-health.py streak-alert, dedups via
                               # <agent>/session/infra-streak-sent.jsonl,
                               # routes new alerts through /notify-user.
                               # PENDING: wiring into a recurring infra-probe
                               # goal's verification step (touches agent-
                               # editable world/aspirations.jsonl, requires
                               # integration testing). Agent-invokable now;
                               # --notify default is OFF (dry-run) per script
                               # docstring — safe to enable on a recurring
                               # probe goal when the caller is ready.
    # Operator-facing audits/sweeps — invoked manually by humans on-demand,
    # not referenced by SKILL.md or hooks. Triaged via  (2026-05-11).
    "audit-deferred-defers.py",        # operator: audit goals with deferred
                                       # defer_reason field; surfaces drift
                                       # in probe-before-defer enforcement.
                                       # On-demand human audit; not in any
                                       # scheduled goal.
    "bulk-retire-dead-entries.py",     # operator: bulk-retire stale rb/guard
                                       # entries past utilization thresholds.
                                       # Triggered manually when curation
                                       # backlog mounts beyond
                                       # /aspirations-curate-memory cadence.
    "bulk-retire-tree-leaves.py",      # operator: bulk-retire low-confidence
                                       # tree leaves identified by a tree
                                       # maintenance audit. Manual companion
                                       # to /tree maintain --backlog.
    "schema-drift-sweep.sh",           # operator: schema drift detector
    "schema-drift-sweep.py",           # across JSONL stores. Run on demand
                                       # when a writer adds an unexpected
                                       # field. Companion to /verify-learning
                                       # but not in its checklist.
    "session-manifest-write-gate.sh",  # operator: validate session-manifest
                                       # writes against the manifest schema.
                                       # Manual companion to recovery-gate;
                                       # invoked when manual recovery work
                                       # is needed (rare).
    "tree-coverage-probe.sh",          # operator: probe knowledge-tree
                                       # category coverage. Runs ad-hoc to
                                       # surface under-covered categories
                                       # for create-aspiration prompts. Not
                                       # in any recurring goal.
    # PENDING USER-SIDE ACTIVATION (). These two scripts ARE wired
    # internally to each other and to presence-tick.py — they constitute a
    # working code path for cross-agent visibility — but the external
    # caller (PostToolUse hook in .claude/settings.json with matcher='*')
    # is in the user-permission deny-list and must be added by the user.
    # Investigation resolved by  (2026-05-11): scripts are NOT
    # dead code; activation step is documented in  defer_reason.
    # Remove this block when one of these happens:
    #   1. User adds the PostToolUse hook (auto-discovery picks up the
    #      reference; the exemption becomes redundant).
    #   2.  itself retires (Idea declared not worth pursuing); at
    #      that point delete presence-read.sh, presence-tick.sh, and
    #      presence-tick.py together.
    "presence-read.sh",                # : pending user-side hook activation
    "presence-tick.sh",                # : pending user-side hook activation
    "presence-tick.py",                # : body of presence-tick.sh
    # 2026-06-03 orphan-triage (7-orphan sweep). Each below is a legitimate
    # tool with NO in-repo caller; channel named per the verify-learning
    # S49.3 review-blocker rule. (The other 3 of the 7 were resolved NOT by
    # exemption: session-manifest-gate.sh wired into pre-commit, goal-script-
    # orphan-gate.sh wired into verify-learning, and meta-transfer.py deleted
    # as daemon-superseded dead code.)
    "skill-gaps-validate.sh",          # operator/CI: validate meta/skill-gaps.yaml
                                       # schema (3). Tested by
                                       # test_skill_gaps_hardening.py; run on
                                       # demand -- not in a production call path
                                       # (meta-set does not yet invoke it).
    "stranded-claim-sweep.sh",         # operator: manual stranded-claim release
                                       # (dry-run default). The .py IS the loop-
                                       # wired path (aspirations/SKILL.md:265
                                       # calls stranded-claim-sweep.py --apply);
                                       # this .sh wrapper is the human entry.
    "hook-fire-audit.sh",              # operator: manual hook-health diagnostic --
                                       # reports entry-sentinel last-fire times
                                       # (). Run ad-hoc; not in any
                                       # recurring goal or hook.
    "skill-attribution.py",            # operator: per-skill invocation-telemetry
                                       # aggregator (read-only MVP, skill-
                                       # telemetry master plan). Run on demand.
}


def _settings_referenced_scripts() -> set:
    """Auto-discover hook-bound script basenames from .claude/settings.json.

    Walks every `hooks.<event>[].hooks[].command` field, extracts the
    invoked script basename via _SCRIPT_BASENAME_RE, and returns the set.
    Fail-open at every layer: missing file, JSON parse error, unexpected
    schema all return an empty set so the caller falls back to static
    exemptions (g-248-41).
    """
    if not SETTINGS_JSON.is_file():
        return set()
    try:
        data = json.loads(SETTINGS_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    out: set = set()
    hooks_block = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks_block, dict):
        return out
    for event_name, event_entries in hooks_block.items():
        if not isinstance(event_entries, list):
            continue
        for entry in event_entries:
            if not isinstance(entry, dict):
                continue
            inner = entry.get("hooks")
            if not isinstance(inner, list):
                continue
            for hook in inner:
                if not isinstance(hook, dict):
                    continue
                cmd = hook.get("command")
                if not isinstance(cmd, str):
                    continue
                # A single command may chain multiple scripts (e.g.
                # `bash a.sh && bash b.sh`); findall captures all.
                for m in _SCRIPT_BASENAME_RE.finditer(cmd + " "):
                    out.add(m.group(1))
    return out


def _collect_scripts(root: Path) -> list:
    """List (path, basename) for every .sh/.py in `root` (non-recursive)."""
    out = []
    if not root.is_dir():
        return out
    for p in sorted(root.iterdir()):
        if not p.is_file():
            continue
        if p.suffix not in (".sh", ".py"):
            continue
        out.append((p, p.name))
    return out


def _collect_reference_text() -> list:
    """Yield (path, text) for every live-reference file."""
    out = []
    for root in LIVE_REF_ROOTS:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix not in REF_EXTS:
                continue
            # Skip .history/ snapshots — these preserve historical text
            # that would falsely mark deleted scripts as referenced.
            pp = p.as_posix()
            if "/.history/" in pp or "\\.history\\" in str(p):
                continue
            try:
                out.append((p, p.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                continue
    for f in LIVE_REF_FILES:
        if not f.is_file():
            continue
        try:
            out.append((f, f.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    # core/githooks/* — extensionless git hook scripts (pre-commit, post-commit)
    # invoked via core.hooksPath. Scanned with NO extension filter so a script
    # referenced only by a hook is not false-flagged orphan (). The dir
    # is shallow (a handful of files) so the rglob is cheap.
    if GITHOOKS_DIR.is_dir():
        for p in sorted(GITHOOKS_DIR.rglob("*")):
            if not p.is_file():
                continue
            pp = p.as_posix()
            if "/.history/" in pp or "\\.history\\" in str(p):
                continue
            try:
                out.append((p, p.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                continue
    return out


def _find_references(basename: str, script_path: Path, corpus: list) -> list:
    """Return a non-empty list iff `basename` is referenced anywhere in the
    corpus (outside its own file), else an empty list.

    Short-circuits on the FIRST reference found. The sole caller
    (`_build_report`) only tests the result for truthiness ("is this script
    referenced at all?"), never the full reference list — so a returned list
    holds at most one (path, lineno). Collecting EVERY reference meant
    rescanning the whole corpus for all ~600 scripts even after a match,
    which made the gate time out (5 min wall, ~1.5s CPU — almost pure I/O)
    on the real repo. (g-249 perf fix, 2026-06-03.)

    Matching is unchanged: a word-boundary regex on the basename (and, for
    `.py`, the bare module name) so `foo.sh` does not match `foo.sh.bak`,
    while varied invocation forms still count (`bash core/scripts/foo.sh`,
    `source _foo.sh`, `from _module import X`, a `foo.sh` doc backtick).
    A cheap C-level substring pre-filter skips the per-line regex on files
    that do not contain the token at all — the word-boundary regex cannot
    match where the literal token is absent, so the orphan set is byte-for-
    byte identical to the pre-optimization behavior."""
    if basename.endswith(".py"):
        # Python modules may also be referenced by bare module name.
        module = basename[:-3]
        pattern = re.compile(
            rf"(?:(?<![\w.-]){re.escape(basename)}(?![\w.-]))"
            rf"|(?:(?<![\w.-]){re.escape(module)}(?![\w.-]))"
        )
        needles = (basename, module)
    else:
        pattern = re.compile(
            rf"(?<![\w.-]){re.escape(basename)}(?![\w.-])"
        )
        needles = (basename,)
    script_str = str(script_path)
    for path, text in corpus:
        if str(path) == script_str:
            continue  # self-reference
        # Substring pre-filter: the regex requires `basename` (or the bare
        # module) as a literal substring, so a file lacking every needle
        # cannot match — skip the line split + regex entirely. `in` on a str
        # is a C-level scan, vastly cheaper than per-line regex across 1600+
        # corpus files × 600+ scripts.
        if not any(n in text for n in needles):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                return [(str(path), lineno)]  # short-circuit: one ref suffices
    return []


def _classify_orphan(basename: str) -> str:
    """Heuristic: infer why this file is likely orphan."""
    if basename.startswith("mind-"):
        return "legacy mind-* family (pre wm-*.sh rename)"
    if basename.startswith("path-resolution-"):
        return "pre-_paths.sh refactor"
    if "reconcile" in basename:
        return "one-off migration script (run once, kept by habit)"
    return "no references — candidate for retirement or wiring"


def _build_report(scripts: list, corpus: list, extra_exempt: set,
                  settings_exempt: set) -> dict:
    helper_orphans = []
    script_orphans = []
    total = 0
    for path, basename in scripts:
        if (basename in ALWAYS_EXEMPT
                or basename in extra_exempt
                or basename in settings_exempt):
            continue
        total += 1
        refs = _find_references(basename, path, corpus)
        if refs:
            continue
        entry = {
            "basename": basename,
            "path": str(path),
            "likely_cause": _classify_orphan(basename),
        }
        if basename.startswith(HELPER_PREFIX):
            helper_orphans.append(entry)
        else:
            script_orphans.append(entry)
    return {
        "scripts_scanned": total,
        "reference_files": len(corpus),
        "settings_auto_exempt_count": len(settings_exempt),
        "helper_orphans": helper_orphans,
        "script_orphans": script_orphans,
        "orphan_count": len(helper_orphans) + len(script_orphans),
    }


def main():
    ap = argparse.ArgumentParser(
        description="Flag core/scripts/*.sh|*.py files with no live "
                    "reference in skills/config/rules/settings.",
    )
    ap.add_argument("--exclude", default="",
                    help="Comma-separated extra basenames to skip")
    ap.add_argument("--text", action="store_true",
                    help="Human summary on stdout (JSON on stderr).")
    args = ap.parse_args()

    extra_exempt = {n.strip() for n in args.exclude.split(",") if n.strip()}

    scripts = _collect_scripts(SCRIPTS_DIR)
    corpus = _collect_reference_text()
    settings_exempt = _settings_referenced_scripts()
    report = _build_report(scripts, corpus, extra_exempt, settings_exempt)
    report["would_block"] = report["orphan_count"] > 0

    if args.text:
        print(f"scripts-referenced-gate: {report['scripts_scanned']} scripts "
              f"scanned across {report['reference_files']} reference files, "
              f"{report['orphan_count']} orphans")
        for kind, entries in (
            ("script-orphan", report["script_orphans"]),
            ("helper-orphan", report["helper_orphans"]),
        ):
            for e in entries:
                print(f"  [{kind}] {e['basename']}: {e['likely_cause']}")
        print(json.dumps(report), file=sys.stderr)
    else:
        print(json.dumps(report))

    sys.exit(1 if report["orphan_count"] else 0)


if __name__ == "__main__":
    main()
