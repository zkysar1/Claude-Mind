#!/usr/bin/env python3
"""Age-cap the gate-firings store by WHOLE-SEGMENT deletion ().

THE DEFECT. Since GATE_FIRINGS_SEGMENTED=1 the flush writes
`gate-firings-YYYY-MM-DD.jsonl` date segments (~1 MB/day) into the meta
directory. `core/config/store-hygiene.yaml` G5 declares the retention policy
for this store -- mode cap, by age, retention_days 40 -- but its `path:` names
the LEGACY basename, so nothing bounds the segments and they accumulate
forever. The legacy file drains under G5 over 40 days and then sits empty while
the real store grows unbounded beside it.

WHY A SEPARATE SCRIPT AND NOT A jsonl_hygiene EXTENSION. Resolved by
MEASUREMENT, not preference (echo, cc-03, 2026-09-16, recorded on g-358-10):
the general `path_glob:` + `mode: drop-file` form is justified by two
consumers; `aspirations-query.sh --goal-field id g-358-05 --full` returned []
-- g-358-05 does not exist -- so there is ONE consumer, and
implementation-discipline rule 3 (no single-use abstractions) selects the
specific script. Do not reopen that fork without re-measuring.

WHY jsonl_hygiene COULD NOT DO THIS ANYWAY, even globbed. Every mode it
supports is RECORD-level: the `--mode` enum is exactly (cap, rotate, compact)
and `by=age` computes a COUNT OF RECORDS past a cutoff and then rewrites the
file, which on own-cloud is an S3 GET+PUT of the whole object. Pointing G5 at a
segment glob would therefore rewrite each expired segment down to ZERO records
and leave an empty file behind forever -- both the O(file) rewrite this goal's
scope rejects and the exact empty-file end state the legacy basename already
demonstrates. An expired DATE segment is expired in whole, so the correct
operation is unlink, O(1) per expired day.

DELETION DISCIPLINE. Two rails, neither optional:
  * guard-1493 -- on own-cloud a delete needs TWO operations and neither
    implies the other; a local-only unlink is silently re-materialized by
    read-through on the next read. We do NOT hand-roll that chain:
    `StorageBackend.delete()` already owns delete_object -> unlink -> re-verify
    BOTH lanes, and raises rather than return True over a half-delete.
  * archive-before-delete.md -- ENUMERATE, VERIFY LAYERS, ARCHIVE, VERIFY
    ARCHIVE, DELETE, RECEIPT. The recovery layer here is absent or
    unverifiable: this store is in `_fileops._SNAPSHOT_BLACKLIST` so .history
    holds nothing for it (guard-3095), and under LocalBackend a delete is
    final. So the independent current-copy archive is MANDATORY, not
    preferable -- hence --archive-dir is REQUIRED for --apply.

The archive destination is the CALLER's choice on purpose. This script will not
invent a new top-level directory under a governed root (the world, meta or
agent roots): .claude/rules/path-resolution.md refuses exactly that, and an
archive staged under an agent's temp/ is git-ignored and reaped at 120 minutes
(archive-before-delete step 3c). Naming it is a deliberate act by whoever turns
apply on.

DEFAULT IS DRY RUN. The wiring in iteration-close.sh reports; it does not
delete. Flipping that to --apply is a separate, deliberate change that must
also choose an archive home.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _gate_log  # noqa: E402  -- SSOT for the segment shape

# The G5 entry names the store whose retention_days governs the segments. The
# segments ARE this store (firings_paths() returns legacy + segments as one
# corpus), so its declared window is theirs. Never hardcode the number here --
# a second copy of a retention constant is how the reader and the policy drift.
_G5_PATH_KEY = "meta/" + _gate_log.LEGACY_STORE_NAME
_DEFAULT_HYGIENE = "core/config/store-hygiene.yaml"
_STEM = "gate-firings-"
_SUFFIX = ".jsonl"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def retention_days(hygiene_path: Path) -> int:
    """Read retention_days for the G5 gate-firings entry. Fail LOUD.

    A missing or unreadable policy must never degrade to a default: this
    script deletes, and a wrong window deletes the wrong files. Raising is the
    fail-closed direction (archive-before-delete step 2 -- when the config
    cannot be read, do not proceed as if it said something).
    """
    text = hygiene_path.read_text(encoding="utf-8")
    # Deliberately a scoped scan rather than a YAML load: this file carries the
    # whole hygiene registry and we want exactly the block whose `path:` is the
    # gate-firings store. Anchoring on the path key makes a re-ordering of the
    # registry harmless.
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip() == f"- path: {_G5_PATH_KEY}":
            for fwd in lines[i + 1:i + 12]:
                if fwd.strip().startswith("- path:"):
                    break  # ran into the next entry without finding it
                m = re.match(r"\s*retention_days:\s*(\d+)\s*$", fwd)
                if m:
                    return int(m.group(1))
            break
    raise ValueError(
        f"retention_days not found for '{_G5_PATH_KEY}' in {hygiene_path}; "
        "refusing to expire segments against an unknown window")


def segment_date(name: str):
    """Date encoded in a segment basename, or None when it is not a segment.

    Shape check delegates to _gate_log._SEGMENT_RE so the writer's filename and
    this reader's matcher cannot drift. That regex is anchored and exact, which
    is what excludes the machine-local spool and any future sibling sharing the
    stem, by construction rather than by a denylist someone must remember to
    update.
    """
    if not _gate_log._SEGMENT_RE.match(name):
        return None
    try:
        return _dt.date.fromisoformat(name[len(_STEM):-len(_SUFFIX)])
    except ValueError:
        return None


def _all_segments(meta_dir: Path):
    """Every real date segment in `meta_dir`, oldest first."""
    out = []
    for p in sorted(Path(meta_dir).glob(_STEM + "*" + _SUFFIX)):
        if not p.is_file():
            continue
        d = segment_date(p.name)
        if d is not None:
            out.append((d, p))
    return out


def expired_segments(meta_dir: Path, cutoff: _dt.date):
    """Segments strictly older than `cutoff`, oldest first.

    STRICTLY older: a segment dated exactly on the cutoff is the boundary day
    and is KEPT. G5's own comment sizes the 40-day window as "30d reader window
    + 33% margin (boundary + 24h cap cadence-gap)", so the boundary is
    deliberately inside the window, and an off-by-one here deletes a day the
    readers still window over.
    """
    return [(d, p) for d, p in _all_segments(meta_dir) if d < cutoff]


def _md5(p: Path) -> str:
    h = hashlib.md5()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _archive_one(src: Path, archive_dir: Path) -> dict:
    """COPY (never move) + verify by size AND md5. A move is a delete of the
    original, which would leave nothing to fall back to if the verify fails."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    dst = archive_dir / src.name
    if dst.exists():
        raise FileExistsError(f"archive target already exists: {dst}")
    src_md5, src_size = _md5(src), src.stat().st_size
    shutil.copy2(src, dst)
    dst_md5, dst_size = _md5(dst), dst.stat().st_size
    if (dst_md5, dst_size) != (src_md5, src_size):
        raise OSError(
            f"archive verify FAILED for {src.name}: "
            f"src {src_size}B/{src_md5} != dst {dst_size}B/{dst_md5}")
    return {"name": src.name, "bytes": src_size, "md5": src_md5,
            "archived_to": str(dst)}


def _write_receipt(archive_dir: Path, records: list, cutoff: _dt.date,
                   days: int, meta_dir: Path) -> str:
    """RECEIPT.md at the archive's TOP LEVEL (archive-before-delete step 6).

    The name and placement are load-bearing: `temp-drain-purge.sh` preserves a
    directory carrying a top-level `RECEIPT` / `RECEIPT.*`, and producers that
    named it anything else were invisible to it.
    """
    path = archive_dir / "RECEIPT.md"
    stamp = _dt.datetime.now().isoformat(timespec="seconds")
    lines = [
        f"# RECEIPT - expired gate-firings date segments, {stamp}",
        "",
        f"WHAT: {len(records)} whole date segment(s) removed from `{meta_dir}` by "
        "`core/scripts/gate-firings-segments-expire.py --apply` (g-358-10).",
        "",
        f"WHY: store-hygiene G5 declares retention_days={days} for the gate-firings "
        f"store, so today's expiry cutoff is {cutoff.isoformat()}. Every file listed "
        "below is dated strictly before that cutoff. Segments dated ON the cutoff are "
        "kept - the window's margin deliberately includes the boundary day.",
        "",
        "HOW: each file was COPIED here and verified byte-for-byte (size + md5) BEFORE "
        "any deletion, then removed through `StorageBackend.delete()`, which drops the "
        "store object and the local mirror and re-verifies BOTH lanes (guard-1493). "
        "An archive verify failure aborts the run before any delete.",
        "",
        "ENUMERATION (name / bytes / md5):",
    ]
    for r in records:
        lines.append(f"  - {r['name']}  {r['bytes']}  {r['md5']}")
    lines += [
        "",
        "RESTORE: copy the segment(s) back to the meta directory named above - NOT to "
        "the repo root, and not into any other store. Readers enumerate the corpus via "
        "`_gate_log.firings_paths()`, which picks up any correctly-named segment that "
        "is present, so a restored file rejoins the store with no further step.",
        "",
        "NOTE: these are gate-telemetry rows past their declared retention window. The "
        "four readers (gate-stats, gate-retirement-eval, override-ledger-consume, "
        "team-contribution-report) window on `--days` with a 30-day default, so nothing "
        "in this archive is inside any reader's window as of the date above.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Delete gate-firings date segments past the G5 retention window.")
    ap.add_argument("--apply", action="store_true",
                    help="perform the archive+delete (default: dry run, report only)")
    ap.add_argument("--archive-dir",
                    help="REQUIRED with --apply: cold directory to copy segments into "
                         "before deleting. Must be outside the governed roots and "
                         "outside an agent's temp/ (reaped at 120 min).")
    ap.add_argument("--meta-dir", help="override the resolved meta directory (tests)")
    ap.add_argument("--hygiene", help="override the store-hygiene.yaml path (tests)")
    ap.add_argument("--today", help="override today's date, ISO (tests)")
    a = ap.parse_args(argv)

    meta_dir = Path(a.meta_dir) if a.meta_dir else (
        Path(_gate_log.META_DIR) if _gate_log.META_DIR else None)
    if meta_dir is None:
        # Matches firings_paths(): unresolved paths is a real runtime state,
        # and it must never look like "the store is empty" -- that direction
        # reads as "this gate never fired, so it is retirable".
        print("[expire] META_DIR unresolved - cannot enumerate segments",
              file=sys.stderr)
        return 3

    hygiene = Path(a.hygiene) if a.hygiene else _repo_root() / _DEFAULT_HYGIENE
    days = retention_days(hygiene)
    today = _dt.date.fromisoformat(a.today) if a.today else _dt.date.today()
    cutoff = today - _dt.timedelta(days=days)

    present = _all_segments(meta_dir)
    expired = expired_segments(meta_dir, cutoff)

    rep = {
        "meta_dir": str(meta_dir), "retention_days": days,
        "today": today.isoformat(), "cutoff": cutoff.isoformat(),
        # The unfiltered population sits beside the filtered count on purpose
        # (guard-2298): `expired_count: 0` next to `segments_present: 0` is a
        # probe that found nothing, while `expired_count: 0` next to
        # `segments_present: 36` is a real, verified zero.
        "segments_present": len(present),
        "oldest_segment": present[0][1].name if present else None,
        "expired": [p.name for _, p in expired],
        "expired_count": len(expired),
        "applied": False, "archived": [], "deleted": [], "receipt": None,
        "errors": [],
    }

    if not a.apply or not expired:
        rep["action"] = "would-delete" if expired else "noop"
        print(json.dumps(rep, indent=2))
        return 0

    if not a.archive_dir:
        rep["action"] = "refused-no-archive-dir"
        rep["errors"].append(
            "--apply requires --archive-dir: the .history recovery layer is "
            "blacklisted for this store (guard-3095) and a LocalBackend delete "
            "is final, so the independent current-copy archive is mandatory "
            "(archive-before-delete step 2).")
        print(json.dumps(rep, indent=2))
        return 2

    archive_dir = Path(a.archive_dir)
    import storage_backend  # local import: tests may pin STORAGE_BACKEND first
    be = storage_backend.get_backend()

    # ARCHIVE EVERYTHING FIRST, verify each, and only then delete. Interleaving
    # (archive one, delete one) would leave a partial run half-deleted with the
    # rest unarchived; this ordering means a verify failure costs nothing.
    for _d, p in expired:
        try:
            rep["archived"].append(_archive_one(p, archive_dir))
        except Exception as e:  # noqa: BLE001 -- abort, do not continue
            rep["errors"].append(f"archive failed for {p.name}: {e}")
            rep["action"] = "aborted-archive-failed"
            print(json.dumps(rep, indent=2))
            return 1

    rep["receipt"] = _write_receipt(archive_dir, rep["archived"], cutoff,
                                    days, meta_dir)

    for _d, p in expired:
        try:
            be.delete(str(p.resolve()))   # absolute: _s3_key rejects relative
            if p.exists():
                raise OSError(f"still present locally after delete: {p}")
            rep["deleted"].append(p.name)
        except Exception as e:  # noqa: BLE001
            rep["errors"].append(f"delete failed for {p.name}: {e}")

    rep["applied"] = True
    rep["action"] = "deleted" if not rep["errors"] else "partial"
    print(json.dumps(rep, indent=2))
    return 0 if not rep["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
