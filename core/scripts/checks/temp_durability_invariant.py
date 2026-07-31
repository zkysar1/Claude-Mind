#!/usr/bin/env python3
"""verify-learning check: the SURVIVING temp/ durability invariant ().

A file directly under agents/*/temp/ that is NEITHER git-tracked NOR covered by
temp-drain-purge.sh's ephemera extension list has no durability AND no lifecycle.
Under STORAGE_BACKEND=local the own-cloud S3 sweep that .gitignore names as the
durability mechanism does not run -- such a file has no copy anywhere.

SUPERSEDES g-001-210's proposed check, whose invariant ("git-ignored IFF purged")
is obsolete: .gitignore now ignores ALL of agents/*/temp/* by design (g-115-1765)
while the purge covers 8 extensions, so the biconditional is false in BOTH
directions and asserting it would fail permanently.

The extension list is PARSED from temp-drain-purge.sh, never hardcoded -- it grew
5 -> 8 without anything noticing, which is how g-001-210's premise went stale.

WARNS, never fails (always exits 0): a fresh working doc legitimately sits
untracked for minutes. Age threshold 24h. Says nothing about depth-2 drained/,
which has its own Lane-2 mtime GC -- a different lifecycle, not a violation.

ENVIRONMENT-GATED BY CONSTRUCTION (back-ported UP from ZDS 2026-07-31; authored
there as g-001-249). The original carried "ZDS-LOCAL BY DESIGN (KEEP-IN-SYNC)":
the invariant only holds where the backend is local, upstream runs own-cloud,
and a mirror-style sync would delete the file (rb-842). Instead of keeping a
deployment-local fork forever, the check now resolves the ACTIVE storage
backend the same way storage_backend.get_backend() does and SKIPs unless it is
"local" -- correct on every deployment, so it lives at the dev source and
promotion carries it instead of deleting it. The mirror is deliberate: when the
box would behave local (explicit pin, registry says local, or nothing set at
all -- the same ambient default get_backend() applies), git/purge really are
the only durability there, so the invariant applies exactly where the check runs.
"""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# CWD-INDEPENDENT BY CONSTRUCTION. The first version of this check used relative
# paths ("agents", "core/scripts/temp-drain-purge.sh"). Run from anywhere but
# PROJECT_ROOT it found nothing and printed a confident SKIP/PASS having scanned
# zero files — a silent-failure defect inside a check written to catch silent
# failures. Caught by fresh-eyes review (); the three original controls
# all ran from the repo root, so they passed under both "works" and
# "cwd-dependent" (rb-847: a check that passes under every hypothesis tests
# nothing). agents_root() is also the mandated accessor — CLAUDE.md forbids
# hardcoding the "agents" path segment (AGENTS_PARENT_DIR).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import PROJECT_ROOT, agents_root  # noqa: E402

AGE_SECONDS = 24 * 3600
PURGE = Path(PROJECT_ROOT) / "core" / "scripts" / "temp-drain-purge.sh"


def active_storage_backend() -> str:
    """Resolve the backend NAME exactly as storage_backend.get_backend() does,
    without instantiating a backend: run the SAME env self-heal it runs
    (_bootstrap_env_defaults — .env.local gap-fill + registry derivation,
    g-115-2297/g-115-3070; explicit env always wins; pytest-hermetic by its
    own guard), then read the var with the same default. This keeps one
    source of truth: the check runs exactly where the storage layer would
    actually behave local for this process."""
    try:
        from storage_backend import _bootstrap_env_defaults
        _bootstrap_env_defaults()
    except Exception:
        pass  # missing module/SDK: fall through to the ambient env read
    return os.environ.get("STORAGE_BACKEND", "local").strip().lower()


def parsed_extensions():
    if not PURGE.is_file():
        return None
    m = re.search(r"PURGE_FIND_PRED=\((.*?)\)\s*$", PURGE.read_text(encoding="utf-8"), re.M | re.S)
    if not m:
        return None
    return set(re.findall(r"-name '\*(\.[A-Za-z0-9]+)'", m.group(1))) or None


def main():
    backend = active_storage_backend()
    if backend not in ("", "local", "local-files"):
        print("SKIP: temp-durability invariant applies only under a local storage "
              "backend (this box resolves %r; the own-cloud S3 sweep provides "
              "temp/ durability there)" % backend)
        return 0

    exts = parsed_extensions()
    if exts is None:
        print("SKIP: could not parse PURGE_FIND_PRED from temp-drain-purge.sh")
        return 0
    try:
        tracked = set(subprocess.run(["git", "ls-files"], capture_output=True,
                                     text=True, timeout=30,
                                     cwd=str(PROJECT_ROOT)).stdout.split("\n"))
    except Exception as exc:
        print("SKIP: git ls-files unavailable (%s)" % type(exc).__name__)
        return 0

    now, orphans = time.time(), []
    for p in Path(agents_root()).glob("*/temp/*"):
        if not p.is_file() or p.name.startswith("."):
            continue
        if (now - p.stat().st_mtime) < AGE_SECONDS:
            continue                                   # fresh working doc: legitimate
        if str(p.relative_to(PROJECT_ROOT)).replace("\\", "/") in tracked:
            continue                                   # durable via git
        if p.suffix in exts:
            continue                                   # has a lifecycle via purge
        orphans.append(str(p))

    if orphans:
        print("WARN: %d temp file(s) >24h are NEITHER git-tracked NOR purge-covered "
              "(no durability, no lifecycle): %s" % (len(orphans), ", ".join(orphans[:5])))
        if len(orphans) > 5:
            print("      ... +%d more" % (len(orphans) - 5))
    else:
        print("PASS: temp-durability invariant holds (%d ext(s) parsed, 0 orphans)" % len(exts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
