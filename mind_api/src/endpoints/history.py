"""GET/POST /v1/history/{list,diff,restore,prune,prune-legacy}.

Daemonises core/scripts/history.py. History spans ALL FOUR base dirs
(world/meta/agent/.claude), so the central challenge is resolve_base_dir:
_fileops.resolve_base_dir uses module-level WORLD_DIR/META_DIR/AGENT_DIR/
PROJECT_ROOT (None/frozen in the daemon). This module REIMPLEMENTS it against
ctx.paths (world > meta > agent[if header] > project_root/.claude), shared by
list/diff/restore.

  GET  /v1/history/list          ?file=<abs>          (cmd_list)        read
  GET  /v1/history/diff          ?file=<abs>&version= (cmd_diff)        read
  POST /v1/history/restore       ?file=<abs>&version= (cmd_restore)     write
  POST /v1/history/prune         ?dry_run=            (cmd_prune)       write/del
  POST /v1/history/prune-legacy  ?apply=&min_age_days=(cmd_prune_legacy)write/del

PURE FUNCTIONS imported from _fileops (safe to call — _fileops is already loaded
by file_locks at daemon start; these read no module globals):
  _parse_snapshot_datetime, _is_new_store_snapshot.
NOT safe (use module-global resolve_base_dir): _find_history_snapshots,
_restore_snapshot_to_target — REIMPLEMENTED here against ctx.paths.
DAEMON-SAFE (base_dir is a parameter): _history_store.{restore,_read_manifest}.

byte_compat:
  - list/diff: read-only; body == CLI STDOUT.
  - restore: the RESTORED file gets the exact snapshot bytes. Side-effects: a
    before-restore snapshot via mind_api.src.history.snapshot (LEGACY format —
    pre-existing divergence from the CLI's CAS-delta save_history, inherited by
    ALL daemon write side-effects, NOT introduced here) + changelog.append
    action='restore'. Lock uses stale_seconds=10 (restore-specific override).
    Agent attribution falls back to 'restore' (NOT 'system') to match the CLI's
    os.environ.get('MIND_AGENT', 'restore'). Header is CONDITIONAL: required
    when the file resolves to an agent dir (else 400), optional for world/meta/
    .claude (the agent branch of resolve_base_dir is gated on header-presence,
    so an agent-dir file with no header resolves to None -> 400 automatically).
  - prune/prune-legacy: delete-only on world+meta .history/ legacy trees; body
    == CLI STDOUT. No header, no history/changelog side-effects.

sys.exit conversions (corrected by adversarial verify):
  cmd_restore: line 216 (version not found) -> 404; line 37 via resolve failure
    (reachable at restore lines 229/235) -> 400. (sys_exit_count = 2, not 1.)
  cmd_diff: line 256/261 -> 404; line 37 via _read_snapshot_text new-store
    resolve -> 400. (sys_exit_count = 3, not 2.)
"""
from __future__ import annotations

import difflib
import gzip
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from .. import history as _hist_snapshot   # mind_api.src.history.snapshot (side-effect)
from .. import changelog as _changelog     # mind_api.src.changelog.append
from .. import file_locks

# core/scripts on sys.path (idempotent — file_locks/history already did this).
_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "core" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# Pure functions — read no module globals (verified). Safe to call.
from _fileops import _parse_snapshot_datetime, _is_new_store_snapshot  # noqa: E402

NEW_STORE_TOP_DIRS = ("snapshots", "blobs", "patches")


# ---------------------------------------------------------------------------
# Shared: ctx.paths-based resolve_base_dir (reimplements _fileops.resolve_base_dir)
# ---------------------------------------------------------------------------

def _agent_header(ctx) -> str:
    return (ctx.headers.get("x-ayoai-agent") or "").strip()


def _resolve_base_dir(ctx, path):
    """Return (base_dir_resolved, kind) or (None, None).

    Order world > meta > agent > project_root/.claude, mirroring
    _fileops.resolve_base_dir. The AGENT branch is included ONLY when an
    X-Mind-Agent header is present — matching the CLI's behaviour where
    AGENT_DIR is None (branch skipped) when MIND_AGENT is unset. This is what
    makes the restore header CONDITIONAL: an agent-dir file with no header
    resolves to None -> 400.
    """
    p = Path(path).resolve()
    bases = [(Path(ctx.paths.world), "world"), (Path(ctx.paths.meta), "meta")]
    if _agent_header(ctx):
        bases.append((Path(ctx.paths.agent), "agent"))
    bases.append((Path(ctx.paths.project_root) / ".claude", "claude"))
    for base, kind in bases:
        try:
            if p.is_relative_to(base.resolve()):
                return base.resolve(), kind
        except (ValueError, OSError):
            pass
    return None, None


def _resolve_file_arg(ctx, file_str):
    """Resolve the ?file= param. Relative paths anchor to project_root (the CLI's
    cwd when invoked via wrappers); absolute paths pass through. The contract for
    the eventual wrapper cut is an ABSOLUTE path (world/meta are external)."""
    p = Path(file_str)
    if not p.is_absolute():
        p = Path(ctx.paths.project_root) / p
    return p.resolve()


# ---------------------------------------------------------------------------
# Snapshot discovery — reimplements _fileops._find_history_snapshots etc.
# ---------------------------------------------------------------------------

def _parse_snapshot_name(name):
    """Verbatim from history.py:parse_snapshot_name (41-67)."""
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


def _find_history_snapshots(ctx, path):
    """Reimplements _fileops._find_history_snapshots against ctx.paths."""
    base_dir, _ = _resolve_base_dir(ctx, path)
    if base_dir is None:
        return []
    try:
        rel = Path(path).resolve().relative_to(Path(base_dir).resolve())
    except ValueError:
        return []
    candidates = []

    legacy_dir = Path(base_dir) / ".history" / str(rel)
    if legacy_dir.exists():
        for p in legacy_dir.iterdir():
            if (not p.is_file() or p.name.endswith(".meta") or p.name.endswith(".tmp")):
                continue
            ts = _parse_snapshot_datetime(p)
            if ts is None:
                continue
            candidates.append((ts, p))

    new_dir = Path(base_dir) / ".history" / "snapshots" / str(rel)
    if new_dir.exists():
        for p in new_dir.iterdir():
            if (not p.is_file() or not p.name.endswith(".yaml") or p.name.endswith(".tmp")):
                continue
            ts = _parse_snapshot_datetime(p)
            if ts is None:
                continue
            candidates.append((ts, p))

    candidates.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in candidates]


def _find_snapshot_by_name(ctx, path, version_name):
    """Verbatim from history.py:_find_snapshot_by_name (70-94), ctx-routed."""
    snapshots = _find_history_snapshots(ctx, path)
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


def _read_snapshot_text(ctx, version_path, target):
    """Reimplements history.py:_read_snapshot_text. Raises _ResolveError when a
    new-store snapshot's target base cannot be resolved (-> 400)."""
    version_path = Path(version_path)
    if _is_new_store_snapshot(version_path):
        import _history_store
        base_dir, _ = _resolve_base_dir(ctx, target)
        if base_dir is None:
            raise _ResolveError(target)
        content = _history_store.restore(Path(target), version_path.name, base_dir)
        return content.decode("utf-8")
    if version_path.suffix == ".gz":
        with gzip.open(version_path, "rt", encoding="utf-8") as f:
            return f.read()
    return version_path.read_text(encoding="utf-8")


def _copy_snapshot_to_target(ctx, version_path, target):
    """Reimplements _fileops._restore_snapshot_to_target + history.py's mtime
    stamp (131-143), routed through ctx.paths for new-store restores."""
    version_path = Path(version_path)
    target = Path(target)
    if _is_new_store_snapshot(version_path):
        import _history_store
        base_dir, _ = _resolve_base_dir(ctx, target)
        if base_dir is None:
            raise _ResolveError(target)
        content = _history_store.restore(target, version_path.name, base_dir)
        target.write_bytes(content)
    elif version_path.suffix == ".gz":
        with gzip.open(version_path, "rb") as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst, length=64 * 1024)
    else:
        shutil.copy(str(version_path), str(target))
    st = version_path.stat()
    os.utime(target, (st.st_atime, st.st_mtime))


class _ResolveError(Exception):
    """Target not under any configured base — new-store manifest unresolvable."""


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def list_versions(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    file_raw = ctx.query.get("file")
    if not file_raw:
        return Response.error(400, "missing_param", "file query param required")
    file_path = _resolve_file_arg(ctx, file_raw)

    snapshots = _find_history_snapshots(ctx, file_path)
    if not snapshots:
        return Response.text(f"No history for {file_raw}\n", content_type="text/plain")

    out = []

    def emit(s=""):
        out.append(s + "\n")

    emit(f"History for {file_raw} ({len(snapshots)} versions):")
    emit()
    for snap in snapshots:
        ts, agent = _parse_snapshot_name(snap.name)
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

        emit(f"  [{tag}] {snap.name}  [{ts_str}] by {agent}  "
             f"({size:,} bytes){extras}{summary}")
    return Response.text("".join(out), content_type="text/plain")


def diff(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    file_raw = ctx.query.get("file")
    version = ctx.query.get("version")
    if not file_raw or not version:
        return Response.error(400, "missing_param", "file and version query params required")
    file_path = _resolve_file_arg(ctx, file_raw)

    version_path = _find_snapshot_by_name(ctx, file_path, version)
    if version_path is None:
        return Response.error(404, "not_found", f"Version '{version}' not found")

    target = file_path
    if not target.exists():
        return Response.error(404, "not_found", f"Current file {file_raw} does not exist")

    try:
        old_text = _read_snapshot_text(ctx, version_path, target)
    except _ResolveError:
        return Response.error(400, "unresolved_base",
                              f"{file_raw} is not under any configured base dir")
    old_lines = old_text.splitlines(keepends=True)
    new_lines = target.read_text(encoding="utf-8").splitlines(keepends=True)

    diff_iter = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"{version_path.name} (historical)",
        tofile=f"{target.name} (current)",
    )
    output = "".join(diff_iter)
    body = (output + "\n") if output else "No differences.\n"
    return Response.text(body, content_type="text/plain")


def restore(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    file_raw = ctx.query.get("file")
    version = ctx.query.get("version")
    if not file_raw or not version:
        return Response.error(400, "missing_param", "file and version query params required")
    file_path = _resolve_file_arg(ctx, file_raw)

    version_path = _find_snapshot_by_name(ctx, file_path, version)
    if version_path is None:
        return Response.error(404, "not_found", f"Version '{version}' not found")

    target = file_path
    base_dir, kind = _resolve_base_dir(ctx, file_path)
    if base_dir is None:
        # Either genuinely outside all bases, OR an agent-dir file with no
        # X-Mind-Agent header (agent branch skipped). Both -> 400.
        return Response.error(400, "unresolved_base",
                              f"{file_raw} is not under any configured base dir "
                              f"(agent-dir files require X-Mind-Agent)")

    agent = _agent_header(ctx) or "restore"  # CLI fallback is 'restore', NOT 'system'

    lock_path = target.with_suffix(".lock")
    # stale_seconds=10: restore is copy + atomic rename, sub-second hold.
    with file_locks.locked(target, stale_seconds=10):
        if target.exists():
            _hist_snapshot.snapshot(target, base_dir, agent,
                                    summary=f"Before restore from {version}")
        try:
            _copy_snapshot_to_target(ctx, version_path, target)
        except _ResolveError:
            return Response.error(400, "unresolved_base",
                                  f"{file_raw} is not under any configured base dir")
        _changelog.append(base_dir, agent, target, "restore",
                          summary=f"Restored from {version}")

    return Response.text(f"Restored {file_raw} from {version}\n", content_type="text/plain")


def prune(ctx) -> "Response":  # type: ignore[name-defined]
    """Reimplements cmd_prune against ctx.paths.world/.meta. Delete-only on the
    LEGACY gz tree; new-store subtrees skipped."""
    from ..server import Response
    dry_run = (ctx.query.get("dry_run") or "").lower() in ("1", "true", "yes")

    base_dirs = []
    w = Path(ctx.paths.world)
    m = Path(ctx.paths.meta)
    if (w / ".history").exists():
        base_dirs.append(w / ".history")
    if (m / ".history").exists():
        base_dirs.append(m / ".history")

    out = []

    def emit(s=""):
        out.append(s + "\n")

    if not base_dirs:
        emit("No .history/ directories found.")
        return Response.text("".join(out), content_type="text/plain")

    now = datetime.now()
    total_removed = 0
    total_kept = 0
    total_skipped_locked = 0

    for history_root in base_dirs:
        for snap_dir in sorted(history_root.rglob("*")):
            if not snap_dir.is_dir():
                continue
            try:
                rel_to_root = snap_dir.relative_to(history_root)
            except ValueError:
                continue
            if rel_to_root.parts and rel_to_root.parts[0] in NEW_STORE_TOP_DIRS:
                continue
            snapshots = [f for f in snap_dir.iterdir()
                         if f.is_file() and not f.name.endswith(".meta")]
            if not snapshots:
                continue

            by_date = {}
            for snap in snapshots:
                ts, _ = _parse_snapshot_name(snap.name)
                if ts is None:
                    continue
                date_key = ts.date()
                by_date.setdefault(date_key, []).append((ts, snap))

            recent = []
            daily = {}
            weekly = {}
            for date_key, entries in by_date.items():
                age = (now.date() - date_key).days
                if age <= 7:
                    recent.extend(entries)
                elif age <= 30:
                    daily.setdefault(date_key, []).extend(entries)
                else:
                    week_key = date_key.isocalendar()[:2]
                    weekly.setdefault(week_key, []).extend(entries)

            total_kept += len(recent)

            def _prune_group(groups):
                nonlocal total_kept, total_removed, total_skipped_locked
                for _, group_entries in groups.items():
                    group_entries.sort(key=lambda x: x[0])
                    for ts, snap in group_entries[:-1]:
                        if dry_run:
                            emit(f"  [dry-run] Would remove: {snap}")
                            total_removed += 1
                        else:
                            unlinked = False
                            try:
                                snap.unlink()
                                unlinked = True
                            except FileNotFoundError:
                                unlinked = True
                            except OSError:
                                total_skipped_locked += 1
                            meta = snap.with_suffix(snap.suffix + ".meta")
                            try:
                                if meta.exists():
                                    meta.unlink()
                            except OSError:
                                pass
                            if unlinked:
                                total_removed += 1
                    total_kept += 1

            _prune_group(daily)
            _prune_group(weekly)

    action = "Would remove" if dry_run else "Removed"
    tail = f", skipped {total_skipped_locked} locked" if total_skipped_locked else ""
    emit(f"{action} {total_removed} snapshots, kept {total_kept}{tail}")
    return Response.text("".join(out), content_type="text/plain")


def prune_legacy(ctx) -> "Response":  # type: ignore[name-defined]
    """Reimplements cmd_prune_legacy against ctx.paths.world/.meta."""
    from ..server import Response
    try:
        min_age_days = int(ctx.query.get("min_age_days") or 30)
    except ValueError:
        return Response.error(400, "invalid_param", "min_age_days must be integer")
    age_cutoff = datetime.now().timestamp() - min_age_days * 86400
    apply = (ctx.query.get("apply") or "").lower() in ("1", "true", "yes")

    base_dirs = []
    w = Path(ctx.paths.world)
    m = Path(ctx.paths.meta)
    if (w / ".history").exists():
        base_dirs.append(w / ".history")
    if (m / ".history").exists():
        base_dirs.append(m / ".history")

    out = []

    def emit(s=""):
        out.append(s + "\n")

    if not base_dirs:
        emit("No .history/ directories found.")
        return Response.text("".join(out), content_type="text/plain")

    deleted_dirs = 0
    skipped_recent = 0
    skipped_no_coverage = 0
    bytes_freed = 0

    for history_root in base_dirs:
        snapshots_root = history_root / "snapshots"
        for leg_dir in sorted(history_root.rglob("*")):
            if not leg_dir.is_dir():
                continue
            try:
                rel_to_root = leg_dir.relative_to(history_root)
            except ValueError:
                continue
            if rel_to_root.parts and rel_to_root.parts[0] in NEW_STORE_TOP_DIRS:
                continue

            snaps = [f for f in leg_dir.iterdir()
                     if f.is_file()
                     and not f.name.endswith(".meta")
                     and not f.name.endswith(".tmp")]
            if not snaps:
                if apply:
                    try:
                        leg_dir.rmdir()
                    except OSError:
                        pass
                continue

            recent_snap = None
            for s in snaps:
                try:
                    if s.stat().st_mtime > age_cutoff:
                        recent_snap = s
                        break
                except OSError:
                    recent_snap = s
                    break
            if recent_snap is not None:
                skipped_recent += 1
                continue

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
                emit(f"  [dry-run] Would delete: {leg_dir}  "
                     f"({len(snaps)} snapshot{'s' if len(snaps) != 1 else ''}, "
                     f"{dir_bytes:,} bytes)")
            deleted_dirs += 1

    verb = "Deleted" if apply else "Would delete"
    emit(f"{verb} {deleted_dirs} legacy subtree{'s' if deleted_dirs != 1 else ''} "
         f"({bytes_freed:,} bytes). "
         f"Skipped {skipped_recent} recent, {skipped_no_coverage} no-coverage.")
    return Response.text("".join(out), content_type="text/plain")


def register(routes) -> None:
    routes[("GET", "/v1/history/list")] = list_versions
    routes[("GET", "/v1/history/diff")] = diff
    routes[("POST", "/v1/history/restore")] = restore
    routes[("POST", "/v1/history/prune")] = prune
    routes[("POST", "/v1/history/prune-legacy")] = prune_legacy
