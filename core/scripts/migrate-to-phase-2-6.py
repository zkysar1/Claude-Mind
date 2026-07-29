#!/usr/bin/env python3
"""One-shot, idempotent migration to Phase 2.6 per-session dir layout.

For each `.active-agent-<SID>` file at PROJECT_ROOT:

  1. Read the agent name.
  2. If `agents/<agent>/sessions/<SID>/binding.yaml` already exists, only
     delete the legacy file (cleanup pass).
  3. Otherwise, write a minimal binding.yaml — agent + sid + mode='unknown'
     + started_at='migrated-2026-05-19' + started_by='migrate-to-phase-2-6'
     — then delete the legacy file.

Safe to run multiple times:
  - Existing legacy files with already-migrated bindings are cleaned up.
  - Existing legacy files with no migrated binding get one written.
  - Already-migrated bindings are left alone.
  - Missing agent dirs / unreadable legacy files are skipped (logged).

CLI:
  py -3 core/scripts/migrate-to-phase-2-6.py [--dry-run] [--verbose]

--dry-run prints what would happen without writing/deleting anything.
--verbose echoes each per-file action; default is summary-only.

Exit codes:
  0   success (or no-op)
  1   one or more legacy files could not be migrated (still listed on stderr)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _session_binding import (  # noqa: E402
    _AGENTS_PARENT_DIR,
    _SESSIONS_DIRNAME,
    _BINDING_FILENAME,
    _valid_agent_name,
    _valid_sid_shape,
)


def _project_root() -> Path:
    return SCRIPT_DIR.parent.parent


def _agents_root(pr: Path) -> Path:
    return pr / _AGENTS_PARENT_DIR if _AGENTS_PARENT_DIR else pr


def _migrate_one(pr: Path, legacy_file: Path, dry_run: bool, verbose: bool) -> tuple[str, str | None]:
    """Return (status, reason). status ∈ {migrated, already-migrated,
    cleanup-only, skip-invalid, skip-missing-agent, error}."""
    sid = legacy_file.name[len(".active-agent-"):]
    if not _valid_sid_shape(sid):
        return ("skip-invalid", f"bad SID shape: {sid!r}")
    try:
        agent = legacy_file.read_text(encoding="utf-8").strip().strip("\r\n ").strip()
    except OSError as e:
        return ("error", f"unreadable: {e}")
    if not _valid_agent_name(agent):
        return ("skip-invalid", f"bad agent name: {agent!r}")

    agent_dir = _agents_root(pr) / agent
    if not agent_dir.is_dir():
        return ("skip-missing-agent", f"agent dir absent: {agent_dir}")

    sessions_dir = agent_dir / _SESSIONS_DIRNAME / sid
    binding = sessions_dir / _BINDING_FILENAME

    if binding.is_file():
        # Already migrated; just clean up the legacy file.
        if not dry_run:
            try:
                legacy_file.unlink()
            except OSError as e:
                return ("error", f"could not delete legacy: {e}")
        if verbose:
            print(f"  cleanup-only: {legacy_file.name} (binding present)")
        return ("cleanup-only", None)

    # No prior binding — write a minimal one.
    body = (
        f"session_id: {sid}\n"
        f"agent: {agent}\n"
        f"mode: unknown\n"
        f"started_at: 'migrated-2026-05-19'\n"
        f"started_by: migrate-to-phase-2-6\n"
    )
    if not dry_run:
        try:
            sessions_dir.mkdir(parents=True, exist_ok=True)
            tmp = binding.with_suffix(binding.suffix + ".tmp")
            tmp.write_text(body, encoding="utf-8")
            tmp.replace(binding)
        except OSError as e:
            return ("error", f"binding write failed: {e}")
        try:
            legacy_file.unlink()
        except OSError as e:
            return ("error", f"binding written but legacy deletion failed: {e}")
    if verbose:
        action = "[dry-run] would migrate" if dry_run else "migrated"
        print(f"  {action}: {legacy_file.name} -> {binding}")
    return ("migrated", None)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dry-run", action="store_true",
                   help="Print planned actions without writing/deleting.")
    p.add_argument("--verbose", action="store_true",
                   help="Echo per-file action; default is summary-only.")
    args = p.parse_args(argv)

    pr = _project_root()
    legacy_files = sorted(pr.glob(".active-agent-*"))
    if not legacy_files:
        print("[migrate-to-phase-2-6] no legacy bindings present (already clean).")
        return 0

    print(f"[migrate-to-phase-2-6] {'DRY-RUN: ' if args.dry_run else ''}"
          f"found {len(legacy_files)} legacy binding file(s).")

    tallies = {
        "migrated": 0,
        "already-migrated": 0,
        "cleanup-only": 0,
        "skip-invalid": 0,
        "skip-missing-agent": 0,
        "error": 0,
    }
    errors = []
    for lf in legacy_files:
        status, reason = _migrate_one(pr, lf, args.dry_run, args.verbose)
        tallies[status] = tallies.get(status, 0) + 1
        if status in ("error", "skip-invalid", "skip-missing-agent"):
            errors.append((lf.name, status, reason))

    print()
    print("[migrate-to-phase-2-6] Summary:")
    for k, v in tallies.items():
        if v > 0:
            print(f"  {k}: {v}")
    if errors:
        print()
        print("[migrate-to-phase-2-6] Issues:", file=sys.stderr)
        for name, status, reason in errors:
            print(f"  {status}: {name}  ({reason})", file=sys.stderr)

    return 1 if tallies["error"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
