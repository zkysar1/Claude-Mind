#!/usr/bin/env python3
"""History operations: list, restore, diff, prune.

Manages the .history/ directory that stores timestamped snapshots of files
before they are overwritten. Each snapshot filename is:
    <timestamp>_<agent>.<ext>
"""

import argparse
import gzip
import os
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Ensure stdout/stderr handle unicode on all platforms
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from _paths import WORLD_DIR, META_DIR, resolve_file_path
from _fileops import (
    resolve_base_dir as _resolve_base_dir,
    _find_history_snapshots,
    _is_new_store_snapshot,
    _parse_snapshot_datetime,
)


def resolve_base_dir(file_path):
    """Determine which base_dir a file belongs to. Exits on failure (CLI tool)."""
    base = _resolve_base_dir(file_path)
    if base is None:
        print(f"Error: {Path(file_path).resolve()} is not under WORLD_DIR or META_DIR", file=sys.stderr)
        sys.exit(1)
    return base


def resolve_target(file_arg):
    """Resolve a file argument to an absolute path under a governed root.

    Every command below used to pass its raw argument straight to
    `_find_history_snapshots`, which returns `[]` both when a file genuinely
    has no snapshots AND when the path resolves under no governed root. That
    collapses an ERROR into a DATA state, and `cmd_list` rendered the result
    as "No history" at exit 0 — a false-absent verdict on the recovery layer
    (g-115-4181). Two concrete failures it produced:

      1. Virtual prefixes silently returned nothing. `world/` and `meta/` are
         the framework's canonical virtual prefixes and do NOT live under
         PROJECT_ROOT — both roots are external (local-paths.conf). Anchoring
         them to PROJECT_ROOT yielded a path under no governed root, so
         `history list world/self-evolution.jsonl` reported "No history" for a
         file with 35 snapshots, while its absolute form listed all 35.
      2. Unresolvable and out-of-root paths reported "No history" at exit 0,
         indistinguishable from a healthy empty store — so a typo, or a path
         outside the governed roots entirely, read as "the recovery layer has
         nothing", which is the most dangerous direction for this tool to be
         wrong in (`archive-before-delete.md`: a recovery layer must never be
         assumed absent on one unverified signal).

    Resolution goes through `_paths.resolve_file_path`, the framework's single
    source of truth for virtual prefixes (guard-132); absolute paths pass
    through unchanged. Validation reuses `resolve_base_dir` above, which
    already exits loudly with a diagnostic — this helper simply routes the
    callers through it instead of around it.

    Deliberately does NOT require the file to exist: snapshots outlive the
    file they describe, and listing them after a delete is exactly when they
    are needed. "Under a governed root", not "present on disk", is the
    discriminator between an error and an honest empty result.
    """
    try:
        path = Path(resolve_file_path(str(file_arg)))
    except RuntimeError as e:
        # world/ or meta/ prefix with the corresponding root unconfigured.
        print(f"Error: cannot resolve {file_arg}: {e}", file=sys.stderr)
        sys.exit(1)
    resolve_base_dir(path)  # exits 1 with a diagnostic when under no root
    return path


def parse_snapshot_name(name):
    """Parse timestamp and agent from snapshot filename.

    Handles four forms:
      - '<ts>_<agent>.<ext>'                legacy uncompressed snapshot
      - '<ts>_<agent>.<ext>.gz'             legacy gzip-compressed (Stage 0/1)
      - '<ts>_<agent>.yaml'                 new CAS-delta manifest (Stage 2+;
                                            ts uses microsecond precision)
      - '<ts>_<agent>.<ext>[.gz].meta'      sidecar (callers should filter
                                            .meta first)

    The trailing '.gz' is stripped before parsing so the agent token is
    extracted from the underlying extension, not '.gz'. Timestamp parsing
    delegates to `_fileops._parse_snapshot_datetime`, which tries both the
    legacy `%Y-%m-%dT%H-%M-%S` format and the new `%Y-%m-%dT%H-%M-%S.%f`
    format — single source of truth, no drift.
    """
    if name.endswith(".gz"):
        name = name[:-3]
    stem = Path(name).stem
    if "_" in stem:
        parts = stem.rsplit("_", 1)
        agent = parts[1] if len(parts) > 1 else "unknown"
    else:
        agent = "unknown"
    ts = _parse_snapshot_datetime(Path(name))
    return ts, agent


def _find_snapshot_by_name(file_path, version_name):
    """Locate a snapshot by its filename across both stores.

    Returns a Path or None. Callers receive a Path that
    `_restore_snapshot_to_target` can dispatch on without further work
    (legacy gz vs. new-store .yaml manifest).

    The version_name must match exactly as printed by cmd_list. For legacy
    snapshots we also accept the bare name without '.gz' and the .gz name
    when the on-disk file is uncompressed — preserves the convenience
    behavior of the pre-Stage-2 _resolve_version_path helper.
    """
    snapshots = _find_history_snapshots(Path(file_path))
    by_name = {p.name: p for p in snapshots}
    if version_name in by_name:
        return by_name[version_name]
    if version_name.endswith(".gz"):
        bare = version_name[:-3]
        if bare in by_name:
            return by_name[bare]
    else:
        with_gz = version_name + ".gz"
        if with_gz in by_name:
            return by_name[with_gz]
    return None


def _read_snapshot_text(version_path, target):
    """Read snapshot content as text. Dispatches on store type.

    Legacy gz:   gzip.open + read.
    Legacy raw:  text read verbatim.
    New manifest: reconstruct via _history_store.restore (walks the delta
                  chain back to the nearest full anchor), then decode as
                  utf-8 text.

    `target` is the file the snapshot describes (the live file path the
    user passed to cmd_diff). For new-store snapshots we resolve its
    base_dir via the canonical `resolve_base_dir` instead of reconstructing
    from `version_path.parts` — that avoids a fragility class where any
    directory in the path coincidentally named `.history` or `snapshots`
    would make `parts.index(...)` point at the wrong segment.

    Used by cmd_diff. cmd_restore goes through _copy_snapshot_to_target →
    _restore_snapshot_to_target, which is the canonical write-side path
    and dispatches independently.
    """
    version_path = Path(version_path)
    if _is_new_store_snapshot(version_path):
        import _history_store
        base_dir = resolve_base_dir(target)
        content = _history_store.restore(
            Path(target), version_path.name, base_dir,
        )
        return content.decode("utf-8")
    if version_path.suffix == ".gz":
        with gzip.open(version_path, "rt", encoding="utf-8") as f:
            return f.read()
    return version_path.read_text(encoding="utf-8")


def _copy_snapshot_to_target(version_path, target):
    """Restore snapshot bytes to target, decompressing gzip transparently.

    Delegates to _fileops._restore_snapshot_to_target (single source of truth
    for snapshot restore across the codebase), then stamps target's mtime to
    the snapshot's filesystem mtime so downstream tooling sees a consistent
    "when was this content placed here" signal that approximates the prior
    shutil.copy2 behavior.
    """
    from _fileops import _restore_snapshot_to_target
    _restore_snapshot_to_target(version_path, target)
    st = version_path.stat()
    os.utime(target, (st.st_atime, st.st_mtime))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_list(args):
    """List all versions of a file across both legacy and new stores.

    Snapshots from the legacy `.history/<file>/<ts>...gz` tree and the new
    `.history/snapshots/<file>/<ts>_<agent>.yaml` CAS-delta store are
    merged and shown newest-first by parsed datetime. Each line carries a
    `[old]` / `[new]` tag so the user can tell at a glance which store
    holds it — useful while the legacy tree still exists pre-Stage-3.

    Sizes shown:
      legacy:  on-disk gzip'd snapshot size
      new:     manifest `size_bytes` field (uncompressed payload size) —
               the manifest itself is ~250 bytes; the user cares about
               the snapshot's logical size, not the manifest's.
    """
    target = resolve_target(args.file)
    snapshots = _find_history_snapshots(target)
    if not snapshots:
        # TWO conditions reach here (): an in-scope path with an
        # empty store, and an in-scope path that DOES NOT EXIST. Both printed
        # the same reassuring "No history" at exit 0, so a mistyped path read
        # as "nothing to lose" — an error condition rendered as data.
        #
        # MUST stay byte-identical to the daemon mirror in
        # mind_api/src/endpoints/history.py::list_versions (guard-1189): the
        # same message is served over CLI stdout and the HTTP endpoint, so a
        # transport-specific rewording breaks byte-compat even when the logic
        # matches.
        #
        # The missing-file check sits INSIDE this branch deliberately —
        # snapshots outlive the file they describe, and a post-delete listing
        # is exactly what the recovery path needs (measured: a deleted file
        # still lists its versions at exit 0). Hoisting it above the snapshot
        # lookup would break that read.
        if not target.exists():
            print(f"No history for {args.file} — and the path does not exist",
                  file=sys.stderr)
            sys.exit(1)
        print(f"No history for {args.file}")
        return

    print(f"History for {args.file} ({len(snapshots)} versions):")
    print()
    for snap in snapshots:
        ts, agent = parse_snapshot_name(snap.name)
        ts_str = ts.strftime("%Y-%m-%d %H:%M:%S") if ts else "unknown"

        if _is_new_store_snapshot(snap):
            tag = "new"
            from _history_store import _read_manifest
            try:
                m = _read_manifest(snap)
                size = m.get("size_bytes") or 0
                summary_text = (m.get("summary") or "").strip()
                encoding = m.get("encoding") or "?"
            except OSError:
                size = 0
                summary_text = ""
                encoding = "?"
            extras = f" ({encoding})"
            summary = f" — {summary_text}" if summary_text else ""
        else:
            tag = "old"
            size = snap.stat().st_size
            meta_file = snap.with_suffix(snap.suffix + ".meta")
            summary = ""
            if meta_file.exists():
                summary = f" — {meta_file.read_text(encoding='utf-8').strip()}"
            extras = ""

        print(f"  [{tag}] {snap.name}  [{ts_str}] by {agent}  "
              f"({size:,} bytes){extras}{summary}")


def cmd_restore(args):
    """Restore a specific version of a file from either store."""
    target = resolve_target(args.file)
    version_path = _find_snapshot_by_name(target, args.version)
    if version_path is None:
        all_snaps = _find_history_snapshots(target)
        if not all_snaps:
            print(f"Error: No history for {args.file}", file=sys.stderr)
        else:
            print(f"Error: Version '{args.version}' not found", file=sys.stderr)
            print(f"Available versions:", file=sys.stderr)
            for snap in all_snaps:
                tag = "new" if _is_new_store_snapshot(snap) else "old"
                print(f"  [{tag}] {snap.name}", file=sys.stderr)
        sys.exit(1)

    from _fileops import acquire_lock, release_lock, save_history, append_changelog

    # Lock the target file during restore to prevent concurrent writes.
    # stale_seconds=10: restore is a copy + atomic rename, sub-second hold.
    lock_path = target.with_suffix(".lock")
    acquire_lock(lock_path, stale_seconds=10)
    try:
        # Save current version to history before restoring (if it exists)
        if target.exists():
            agent = os.environ.get("MIND_AGENT", "restore")
            base = resolve_base_dir(target)
            save_history(target, base, agent, summary=f"Before restore from {args.version}")

        _copy_snapshot_to_target(version_path, target)

        # Log the restore in changelog
        base = resolve_base_dir(target)
        agent = os.environ.get("MIND_AGENT", "restore")
        append_changelog(base, agent, target, "restore",
                         summary=f"Restored from {args.version}")
    finally:
        release_lock(lock_path)

    print(f"Restored {args.file} from {args.version}")


def cmd_diff(args):
    """Show diff between current file and a historical version from either store."""
    import difflib

    target = resolve_target(args.file)
    version_path = _find_snapshot_by_name(target, args.version)
    if version_path is None:
        all_snaps = _find_history_snapshots(target)
        if not all_snaps:
            print(f"Error: No history for {args.file}", file=sys.stderr)
        else:
            print(f"Error: Version '{args.version}' not found", file=sys.stderr)
        sys.exit(1)

    if not target.exists():
        print(f"Error: Current file {args.file} does not exist", file=sys.stderr)
        sys.exit(1)

    old_lines = _read_snapshot_text(version_path, target).splitlines(keepends=True)
    new_lines = target.read_text(encoding="utf-8").splitlines(keepends=True)

    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"{version_path.name} (historical)",
        tofile=f"{target.name} (current)",
    )
    output = "".join(diff)
    if output:
        print(output)
    else:
        print("No differences.")


def cmd_prune(args):
    """Prune old history snapshots in the LEGACY gzip tree.

    Retention policy:
      - Keep all versions from the last 7 days
      - Keep one daily snapshot for days 8-30
      - Keep one weekly snapshot for days 31+

    Scope: legacy `.history/<rel>/<ts>...gz` tree ONLY. The new CAS-delta
    store (`.history/snapshots/<rel>/<ts>_<agent>.yaml`) has its own GC
    via `_history_store.vacuum` — touching it here would orphan
    blob/patch storage that vacuum later sweeps. The `snapshots/` subtree
    is explicitly skipped during rglob descent.
    """
    base_dirs = []
    if WORLD_DIR and (WORLD_DIR / ".history").exists():
        base_dirs.append(WORLD_DIR / ".history")
    if META_DIR and (META_DIR / ".history").exists():
        base_dirs.append(META_DIR / ".history")

    if not base_dirs:
        print("No .history/ directories found.")
        return

    now = datetime.now()
    total_removed = 0
    total_kept = 0
    total_skipped_locked = 0

    # New-store storage roots that share .history/ with the legacy tree.
    # Skipping these closes the mis-classification class where blob/patch
    # files (sha256-named .gz) and manifest files (.yaml) would otherwise
    # be candidates for the legacy retention pruner.
    NEW_STORE_TOP_DIRS = ("snapshots", "blobs", "patches")

    for history_root in base_dirs:
        # Walk all snapshot directories. Skip new-store storage —
        # deleting its files would orphan blob/patch storage or manifests.
        # The new store's GC is `_history_store.vacuum`.
        for snap_dir in sorted(history_root.rglob("*")):
            if not snap_dir.is_dir():
                continue
            try:
                rel_to_root = snap_dir.relative_to(history_root)
            except ValueError:
                continue
            if rel_to_root.parts and rel_to_root.parts[0] in NEW_STORE_TOP_DIRS:
                continue
            # Only process leaf directories (containing snapshot files)
            snapshots = [f for f in snap_dir.iterdir() if f.is_file() and not f.name.endswith(".meta")]
            if not snapshots:
                continue

            # Group by date
            by_date = {}
            for snap in snapshots:
                ts, _ = parse_snapshot_name(snap.name)
                if ts is None:
                    continue
                date_key = ts.date()
                by_date.setdefault(date_key, []).append((ts, snap))

            # Separate entries into retention tiers
            recent = []    # age <= 7: keep all
            daily = {}     # age 8-30: keep latest per day
            weekly = {}    # age 31+: keep latest per ISO week

            for date_key, entries in by_date.items():
                age = (now.date() - date_key).days
                if age <= 7:
                    recent.extend(entries)
                elif age <= 30:
                    daily.setdefault(date_key, []).extend(entries)
                else:
                    week_key = date_key.isocalendar()[:2]  # (year, week)
                    weekly.setdefault(week_key, []).extend(entries)

            total_kept += len(recent)

            def _prune_group(groups):
                """Keep only the latest entry per group, remove the rest."""
                nonlocal total_kept, total_removed, total_skipped_locked
                for _, group_entries in groups.items():
                    group_entries.sort(key=lambda x: x[0])
                    # Keep the latest, remove the rest.
                    # OSError tolerance: matches _prune_to_cap defense
                    # (_fileops.py). FileNotFoundError = a concurrent
                    # _prune_to_cap (called from save_history) already
                    # removed this snapshot — effectively pruned, count it.
                    # PermissionError / WindowsError (OneDrive sync holding
                    # the handle, file lock) = skip this one specifically
                    # so the rest of the prune run continues; surface the
                    # skipped count in the final report instead of crashing
                    # the whole sweep mid-way. (bravo fresh-eyes,
                    # msg-20260522-130146-bravo-1522.)
                    for ts, snap in group_entries[:-1]:
                        if args.dry_run:
                            print(f"  [dry-run] Would remove: {snap}")
                            total_removed += 1
                        else:
                            unlinked = False
                            try:
                                snap.unlink()
                                unlinked = True
                            except FileNotFoundError:
                                unlinked = True  # effectively pruned
                            except OSError:
                                total_skipped_locked += 1
                            meta = snap.with_suffix(snap.suffix + ".meta")
                            try:
                                if meta.exists():
                                    meta.unlink()
                            except OSError:
                                pass  # sidecar; never fatal
                            if unlinked:
                                total_removed += 1
                    total_kept += 1

            _prune_group(daily)
            _prune_group(weekly)

    action = "Would remove" if args.dry_run else "Removed"
    tail = f", skipped {total_skipped_locked} locked" if total_skipped_locked else ""
    print(f"{action} {total_removed} snapshots, kept {total_kept}{tail}")


def cmd_prune_legacy(args):
    """Stage 3: delete per-file legacy gz subtrees once Stage 2's new store
    has taken over that file's snapshot history.

    A legacy subtree at `<base>/.history/<rel>/` is eligible for deletion
    when BOTH gates pass:

      1. AGE GATE — every snapshot in the subtree has mtime older than
         --min-age-days (default 30). A single recent legacy snapshot
         (e.g., from a temporary KEEP_LEGACY_WRITES=1 rollback session)
         disqualifies the subtree.

      2. COVERAGE GATE — the corresponding new-store dir at
         `<base>/.history/snapshots/<rel>/` exists AND contains at least
         one manifest. Deleting a legacy subtree for a file with zero
         new-store coverage would leave the file with NO history at all.

    Conservative defaults — dry-run is the default; --apply is required
    to actually delete. The OneDrive sync window is best-effort: this
    sweep is one-shot per invocation, not continuous, and a snapshot
    written between the eligibility check and the rmtree call would be
    silently lost. Run during quiet periods.
    """
    min_age_days = args.min_age_days
    age_cutoff = datetime.now().timestamp() - min_age_days * 86400
    apply = args.apply

    base_dirs = []
    if WORLD_DIR and (WORLD_DIR / ".history").exists():
        base_dirs.append(WORLD_DIR / ".history")
    if META_DIR and (META_DIR / ".history").exists():
        base_dirs.append(META_DIR / ".history")
    if not base_dirs:
        print("No .history/ directories found.")
        return

    deleted_dirs = 0
    skipped_recent = 0
    skipped_no_coverage = 0
    skipped_shortfall = 0
    bytes_freed = 0

    # New-store storage roots that share .history/ with the legacy tree
    # but are NOT legacy snapshot directories. Skipping these closes a
    # mis-classification class where blob/patch storage looked like
    # candidate legacy subtrees.
    NEW_STORE_TOP_DIRS = ("snapshots", "blobs", "patches")

    for history_root in base_dirs:
        snapshots_root = history_root / "snapshots"
        for leg_dir in sorted(history_root.rglob("*")):
            if not leg_dir.is_dir():
                continue
            # Skip anything inside .history/{snapshots,blobs,patches}/ —
            # those are new-store storage, managed by _history_store.vacuum.
            try:
                rel_to_root = leg_dir.relative_to(history_root)
            except ValueError:
                continue
            if rel_to_root.parts and rel_to_root.parts[0] in NEW_STORE_TOP_DIRS:
                continue

            # Filter .meta sidecars AND .tmp orphans. A .tmp file left
            # behind by an interrupted save_history is NOT a snapshot —
            # counting it would block subtree deletion forever because its
            # mtime stays "recent" until manually cleaned up. Mirrors the
            # filter in _fileops._find_history_snapshots.
            snaps = [f for f in leg_dir.iterdir()
                     if f.is_file()
                     and not f.name.endswith(".meta")
                     and not f.name.endswith(".tmp")]
            if not snaps:
                # Empty dir (or only .tmp/.meta remnants) — clean up the
                # husk. Doesn't need the coverage gate; nothing to lose.
                if apply:
                    try:
                        leg_dir.rmdir()
                    except OSError:
                        pass
                continue

            # Age gate
            recent_snap = None
            for s in snaps:
                try:
                    if s.stat().st_mtime > age_cutoff:
                        recent_snap = s
                        break
                except OSError:
                    recent_snap = s  # stat failure → conservatively skip
                    break
            if recent_snap is not None:
                skipped_recent += 1
                continue

            # Coverage gate: relative path from history_root → look in snapshots_root
            try:
                rel = leg_dir.relative_to(history_root)
            except ValueError:
                continue
            new_dir = snapshots_root / rel
            if not new_dir.exists():
                skipped_no_coverage += 1
                continue
            new_manifests = [f for f in new_dir.iterdir()
                             if f.is_file() and f.name.endswith(".yaml")]
            if not new_manifests:
                skipped_no_coverage += 1
                continue
            # Coverage is a COUNT comparison, not a presence check. A single
            # manifest used to authorize deleting a subtree holding any number
            # of legacy snapshots — measured on the live world store, 200
            # snapshots sat under subtrees whose new-store dir held fewer
            # manifests than the versions about to be destroyed. Presence of
            # ONE successor says nothing about the other N-1.
            #
            # Refusing on a shortfall is the fail-safe direction: the worst
            # case is retained bytes, and a retained subtree is re-evaluated
            # on the next run. The old predicate's worst case was permanent
            # loss of unrepresented versions (archive-before-delete.md step 2:
            # a recovery layer is not verified until its contents are counted,
            # not merely observed to exist).
            if len(new_manifests) < len(snaps):
                skipped_shortfall += 1
                print(f"  [shortfall] Preserved: {leg_dir}  "
                      f"({len(snaps)} legacy snapshot"
                      f"{'s' if len(snaps) != 1 else ''}, only "
                      f"{len(new_manifests)} manifest"
                      f"{'s' if len(new_manifests) != 1 else ''} in new store)")
                continue

            # Eligible. Compute bytes freed (informational), then delete.
            dir_bytes = 0
            for s in snaps:
                try:
                    dir_bytes += s.stat().st_size
                except OSError:
                    pass
                meta = s.with_suffix(s.suffix + ".meta")
                if meta.exists():
                    try:
                        dir_bytes += meta.stat().st_size
                    except OSError:
                        pass
            bytes_freed += dir_bytes

            if apply:
                shutil.rmtree(leg_dir, ignore_errors=True)
            else:
                print(f"  [dry-run] Would delete: {leg_dir}  "
                      f"({len(snaps)} snapshot{'s' if len(snaps) != 1 else ''}, "
                      f"{dir_bytes:,} bytes)")
            deleted_dirs += 1

    verb = "Deleted" if apply else "Would delete"
    print(f"{verb} {deleted_dirs} legacy subtree{'s' if deleted_dirs != 1 else ''} "
          f"({bytes_freed:,} bytes). "
          f"Skipped {skipped_recent} recent, {skipped_no_coverage} no-coverage, "
          f"{skipped_shortfall} shortfall.")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description="File history operations")
    sub = parser.add_subparsers(dest="command", required=True)

    # list
    list_p = sub.add_parser("list", help="List all versions of a file")
    list_p.add_argument("file", help="Path to the file")

    # restore
    restore_p = sub.add_parser("restore", help="Restore a historical version")
    restore_p.add_argument("file", help="Path to the file")
    restore_p.add_argument("version", help="Version filename (from list output)")

    # diff
    diff_p = sub.add_parser("diff", help="Diff current vs historical version")
    diff_p.add_argument("file", help="Path to the file")
    diff_p.add_argument("version", help="Version filename (from list output)")

    # prune
    prune_p = sub.add_parser("prune", help="Prune old snapshots (legacy gz tree)")
    prune_p.add_argument("--dry-run", action="store_true", help="Show what would be removed")

    # prune-legacy (Stage 3)
    pl = sub.add_parser(
        "prune-legacy",
        help="Delete entire legacy gz subtrees for files with new-store coverage",
    )
    pl.add_argument("--min-age-days", type=int, default=30,
                    help="Skip subtrees with any snapshot newer than this (default 30)")
    pl.add_argument("--apply", action="store_true",
                    help="Actually delete (default: dry-run)")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "list": cmd_list,
        "restore": cmd_restore,
        "diff": cmd_diff,
        "prune": cmd_prune,
        "prune-legacy": cmd_prune_legacy,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
