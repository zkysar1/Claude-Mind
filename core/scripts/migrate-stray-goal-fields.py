#!/usr/bin/env python3
"""One-shot migration: retire the unregistered goal fields catalogued in
`_goal_fields.GOAL_STRAY_FIELDS` from world/ and agent/ aspirations.jsonl.

Item 2 of g-115-6573. Item 1 shipped the write-time allowlist gate, which stops
NEW strays; it does not move a single existing key.

⚠ READ THIS BEFORE EXPECTING THE CENSUS TO DROP. **Key removal does not persist
on a merge-protected store, and no re-run will change that.** Measured
2026-08-18: `aspirations.jsonl` is merge-protected by the COMMUTATIVE
`merge_aspirations` handler (`coordination_merge.merge_handler_for`), and under
own-cloud `owncloud_backend._merge_reconcile_put` GETs remote, merges, and PUTs.
A commutative merge CANNOT encode a deletion — a key absent from this write and
present remotely must resolve to present, or the merge would not converge. So
what this script delivers today is the FOLD half: 23 goals whose content sat in
keys no consumer reads now carry it in `description`, where every sweep, report
and selector sees it. The keys themselves survive as inert duplicates until the
merge handler learns a TOMBSTONE. The post-write verification below refuses to
report success while they remain, because a migration that claims to have
dropped keys it did not drop is worse than no migration at all.

THE STRAYS ARE MOSTLY NOT JUNK, which is the finding that shapes this whole
script. Of 34 occurrences, 4 are probe artifacts and the rest are substantial
work-product — measurements, corrections, enumerations — written by agents who
meant them to be read, sitting in keys NO consumer reads. The worst case is
g-250-347's `retraction_stale_checkout`, which reads "THE site2_processor_note
AND site_enumeration FIELDS I WROTE EARLIER TODAY ARE WRONG. DO NOT ACT ON THEM":
a safety retraction that was itself invisible, parked next to the two fields it
retracts. So the default disposition is FOLD (append into `description`, the
accumulating carrier every consumer reads) and only then drop the key. Dropping
content because its key was wrong would destroy the very thing the census counts.

DISPOSITIONS, and why there is no `rename` for most of them:

  fold  — append the value into `description` under a dated marker, then drop.
          The default for every prose note and every list-of-refs.
  drop  — remove the key, no fold. ONLY for content-free probe artifacts
          (`__probe__`, `__noop`, `_probe`) and for STALE DUPLICATES whose
          canonical twin already holds a NEWER value.
  rename— move the value to a canonical field. Rare, and heavily guarded.

MEASURED BEFORE WRITING THIS, because both results contradicted the obvious plan:

1. FOUR renames would have CLOBBERED live values with stale ones. `created` on
   g-248-07/08 is 2026-04-20 against a canonical `created_at` of 2026-04-24, and
   `lastAchieved` on g-115-106/754 is 2026-07-09 against a `lastAchievedAt` of
   2026-08-17/18. The second is not cosmetic: those are recurring goals, and
   rewinding the last-achieved clock ~40 days would make the cadence engine treat
   them as wildly overdue and fire them. Hence `drop`, and hence the
   TARGET-COLLISION guard below, which refuses the rename in code rather than
   trusting this comment.

2. `complete_by` / `schedule_type` / `desired_end_state` — the snake_case twins
   the hyphen/camelCase strays appear to want — are NOT in GOAL_KNOWN_FIELDS and
   are referenced by ZERO files under core/scripts or mind_api/src. Renaming into
   them would mint a NEW stray while looking like a fix. Hence those fold like any
   other note. The TARGET-REGISTERED guard below enforces it.

Safety:
  - Dry-run by default; --apply writes.
  - ARCHIVE FIRST (archive-before-delete.md): --apply refuses to run without an
    --archive-dir, writes every affected record verbatim plus a top-level
    RECEIPT.json with restore instructions, and verifies the archive against the
    enumeration before mutating anything.
  - Writes via `_fileops.locked_write_jsonl`, so lock + .history snapshot +
    changelog fire. NO direct file writes.
  - Idempotent: a goal whose stray keys are already gone is untouched, and the
    fold marker makes re-running a no-op rather than a double-append.

Usage:
  python migrate-stray-goal-fields.py                       # dry run, both sources
  python migrate-stray-goal-fields.py --apply --archive-dir <dir>
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import _paths  # type: ignore  # noqa: E402
from _fileops import locked_write_jsonl  # noqa: E402
from _goal_fields import GOAL_KNOWN_FIELDS, GOAL_STRAY_FIELDS  # noqa: E402
# IMPORTED, not re-typed. A local copy of the terminal-status set is exactly the
# silent drift item 1 of this goal exists to prevent — and there are already
# three definitions of it in this tree (aspirations, _peer_thread_relay,
# _goal_census, the last deliberately narrower), so a fourth would be a coin flip
# for the next reader.
from aspirations import TERMINAL_GOAL_STATUSES as TERMINAL_STATUSES  # noqa: E402

# Content-free probe artifacts: nothing to preserve.
DROP_NO_FOLD = {"__probe__", "__noop", "_probe"}

# stray -> canonical target. A rename is ONLY attempted for these, and only
# when BOTH guards below pass. Everything else folds.
RENAME_TARGETS = {
    "created": "created_at",
    "lastAchieved": "lastAchievedAt",
    "defer_until": "deferred_until",
}

FOLD_FIELD = "description"
MARKER = "migrated-stray-field"


def _read_jsonl(path: Path):
    items = []
    if not path.is_file():
        return items
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"[migrate] WARN: invalid JSON line in {path}: {e}",
                      file=sys.stderr)
    return items


def decide(goal: dict, stray: str) -> tuple[str, str]:
    """Return (disposition, reason) for one stray key on one goal.

    Pure and total — every branch returns, so a stray that matches no rule
    folds rather than falling through to silent retention.
    """
    value = goal.get(stray)

    if stray in DROP_NO_FOLD or value is None or str(value).strip() == "":
        return "drop", "content-free probe artifact or empty value"

    target = RENAME_TARGETS.get(stray)
    if target:
        # GUARD 1 — TARGET REGISTERED. Renaming into an unregistered name mints
        # a new stray. Measured: complete_by / schedule_type / desired_end_state
        # are unregistered AND read by nothing.
        if target not in GOAL_KNOWN_FIELDS:
            return "fold", f"target {target!r} is not a registered field"
        # GUARD 2 — TARGET COLLISION. Never overwrite a canonical value that is
        # already present and different; the stray is the stale one.
        if target in goal and goal.get(target) is not None:
            if str(goal.get(target)) == str(value):
                return "drop", f"redundant duplicate of {target}"
            return "drop", (f"STALE duplicate — {target}={goal.get(target)!r} is "
                            f"live and differs from {stray}={value!r}")
        # GUARD 3 — TERMINAL GOAL. A rename exists to make a value FUNCTIONAL
        # again; on a terminal goal there is no function left to restore, so the
        # only thing a rename can still do is feed a sweep that selects on the
        # canonical field. Writing `deferred_until` onto a COMPLETED goal is the
        # measured instance (). Fold instead: the record is preserved
        # and no sweep is handed a defer date for work that is already done.
        if str(goal.get("status")) in TERMINAL_STATUSES:
            return "fold", (f"goal is {goal.get('status')} — a rename would only "
                            f"feed sweeps that read {target}")
        return "rename", f"-> {target} (target absent)"

    return "fold", "prose/ref content preserved into description"


def _fold_text(stray: str, value, today: str) -> str:
    rendered = value if isinstance(value, str) else json.dumps(value)
    return (f"\n\n[{MARKER}:{stray} {today}] This text was stored under the "
            f"unregistered goal field `{stray}`, which no consumer reads, so it "
            f"was invisible to every sweep, report and selector that reads this "
            f"record. Preserved verbatim here (g-115-6573 item 2):\n{rendered}")


def migrate_source(source: str, path: Path, apply: bool, counters: dict,
                   archive: list):
    items = _read_jsonl(path)
    if not items:
        print(f"[migrate:{source}] no aspirations at {path}")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    changed_records = 0

    for asp in items:
        for goal in asp.get("goals", []) or []:
            present = [k for k in list(goal) if k in GOAL_STRAY_FIELDS]
            if not present:
                continue
            desc = goal.get(FOLD_FIELD) or ""
            touched = False
            for stray in present:
                disp, reason = decide(goal, stray)
                gid = goal.get("id")
                counters[disp] = counters.get(disp, 0) + 1
                print(f"  [{source}] {gid:14s} {stray:26s} {disp.upper():6s} {reason}")
                archive.append({"source": source, "goal_id": gid, "field": stray,
                                "value": goal.get(stray), "disposition": disp,
                                "reason": reason})
                if disp == "fold":
                    # Idempotency: never double-append the same stray's fold.
                    if f"[{MARKER}:{stray}" not in desc:
                        desc += _fold_text(stray, goal.get(stray), today)
                elif disp == "rename":
                    goal[RENAME_TARGETS[stray]] = goal.get(stray)
                goal.pop(stray, None)
                touched = True
            if touched:
                if desc != (goal.get(FOLD_FIELD) or ""):
                    goal[FOLD_FIELD] = desc
                changed_records += 1

    print(f"[migrate:{source}] {changed_records} goal(s) affected in {path}")
    if apply and changed_records:
        locked_write_jsonl(path, items)
        print(f"[migrate:{source}] WROTE {path}")
    elif changed_records:
        print(f"[migrate:{source}] DRY RUN — no write. Pass --apply --archive-dir <dir>.")


def write_archive(archive_dir: Path, archive: list, sources: list) -> None:
    """Archive-before-delete: enumeration + verbatim values + a restore receipt.

    Named RECEIPT.json at the archive TOP LEVEL per archive-before-delete.md —
    producers in this tree disagreed on the name and the one reader required a
    name zero producers wrote, so the anchored top-level form is the contract.
    """
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / "stray-values.json").write_text(
        json.dumps(archive, indent=1, ensure_ascii=False), encoding="utf-8")

    # FULL CURRENT-VERSION COPY of every file about to be mutated. This is not
    # belt-and-braces: `world/` is an EXTERNAL, gitignored path, so git is NOT a
    # recovery layer for aspirations.jsonl the way it is for core/. That leaves
    # `.history` (taken by locked_write_jsonl on write) and this copy — and a
    # recovery layer whose retention rules you have not read does not count
    # (archive-before-delete.md step 2), whereas a current-version copy is
    # retention-immune by construction.
    copied = []
    for name, path in sources:
        if path.is_file():
            dest = archive_dir / f"{name}-aspirations.jsonl"
            shutil.copy2(path, dest)
            copied.append({"source": name, "from": str(path), "to": dest.name,
                           "bytes": dest.stat().st_size})
    (archive_dir / "source-files.json").write_text(
        json.dumps(copied, indent=1), encoding="utf-8")
    receipt = {
        "what": "goal fields removed from aspirations.jsonl by "
                "migrate-stray-goal-fields.py (g-115-6573 item 2)",
        "when": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "occurrences": len(archive),
        "goals": sorted({a["goal_id"] for a in archive}),
        "restore": [
            "Every removed value is in stray-values.json beside this receipt, "
            "keyed by goal_id + field, with its disposition and reason.",
            "FOLDED values were NOT lost — they were appended verbatim into the "
            "goal's description under a [migrated-stray-field:<field>] marker. "
            "Restoring those would DUPLICATE them; read the description first.",
            "DROPPED values were content-free probes, or stale duplicates whose "
            "canonical twin already held a newer value. Do NOT restore a stale "
            "duplicate onto a live field — that is the clobber this migration "
            "was written to avoid.",
            "FULL PRE-MIGRATION COPIES of every mutated file are beside this "
            "receipt as <source>-aspirations.jsonl (manifest: source-files.json). "
            "This is the primary recovery layer: world/ is external and "
            "gitignored, so git does NOT cover it. The .history snapshot taken "
            "by locked_write_jsonl is a second layer whose retention rules have "
            "not been read here, so do not rely on it alone.",
        ],
        "do_not_restore_into": "the canonical field a dropped duplicate shadowed "
                               "(created_at / lastAchievedAt) — those are live.",
    }
    (archive_dir / "RECEIPT.json").write_text(
        json.dumps(receipt, indent=1), encoding="utf-8")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true",
                   help="write changes (default: dry-run)")
    p.add_argument("--archive-dir",
                   help="REQUIRED with --apply: where to write the archive + RECEIPT.json")
    p.add_argument("--source", choices=["world", "agent", "both"], default="both")
    args = p.parse_args()

    if args.apply and not args.archive_dir:
        print("[migrate] REFUSED: --apply requires --archive-dir. Deletion is the "
              "LAST step of a retirement, never the first "
              "(.claude/rules/archive-before-delete.md).", file=sys.stderr)
        return 2

    counters: dict = {}
    archive: list = []
    sources = []
    # Module CONSTANTS, matching backfill-work-class.py:116-117 — not helper
    # calls. `_paths` exposes both shapes and only the constants are right here.
    if args.source in ("world", "both"):
        sources.append(("world", Path(_paths.WORLD_DIR) / "aspirations.jsonl"))
    if args.source in ("agent", "both"):
        sources.append(("agent", Path(_paths.AGENT_DIR) / "aspirations.jsonl"))

    # Enumerate + archive BEFORE any mutation: a dry run over every source
    # produces the full enumeration, and --apply re-walks with writes enabled.
    for name, path in sources:
        # counters, not {} — a dry run that reports no tallies is a poor
        # instrument, and the dry run is the ONLY output anyone reads before
        # authorising --apply.
        migrate_source(name, path, False, counters, archive)

    if args.apply:
        adir = Path(args.archive_dir)
        write_archive(adir, archive, sources)
        verified = json.loads((adir / "stray-values.json").read_text(encoding="utf-8"))
        if len(verified) != len(archive):
            print(f"[migrate] REFUSED: archive verify failed "
                  f"({len(verified)} != {len(archive)}); nothing was mutated.",
                  file=sys.stderr)
            return 3
        # Verify the COPIES by size against the live files, not by trusting that
        # shutil.copy2 returned. A verify that only re-reads what this process
        # just wrote proves the write, not the archive.
        for rec in json.loads((adir / "source-files.json").read_text(encoding="utf-8")):
            live = Path(rec["from"]).stat().st_size
            if live != rec["bytes"]:
                print(f"[migrate] REFUSED: archive copy of {rec['source']} is "
                      f"{rec['bytes']}B but the live file is {live}B; "
                      f"nothing was mutated.", file=sys.stderr)
                return 3
        print(f"[migrate] archive verified: {len(verified)} occurrence(s) + "
              f"full source copies -> {adir}")
        counters = {}
        for name, path in sources:
            migrate_source(name, path, True, counters, [])

        # POST-WRITE VERIFICATION. Without this the script reports
        # "APPLIED drop=9" while every drop is silently undone, which is a lying
        # instrument — worse than no migration, because the census looks acted on.
        # MEASURED 2026-08-18 on an own-cloud box: `aspirations.jsonl` is
        # merge-protected by the COMMUTATIVE `merge_aspirations` handler
        # (coordination_merge.merge_handler_for), and `owncloud_backend`'s
        # `_merge_reconcile_put` GETs remote, merges, and PUTs. A commutative
        # merge CANNOT represent a deletion: absent-in-mine + present-in-remote
        # must resolve to present or the merge would not converge. So the FOLDS
        # (added content) landed and every popped key came back.
        residual = 0
        for name, path in sources:
            for asp in _read_jsonl(path):
                for goal in asp.get("goals", []) or []:
                    residual += sum(1 for k in goal if k in GOAL_STRAY_FIELDS)
        if residual:
            print(f"\n[migrate] *** KEY REMOVAL DID NOT PERSIST: {residual} stray "
                  f"occurrence(s) remain after the write. ***\n"
                  f"[migrate] This is NOT a bug in this script and re-running will "
                  f"not help. `aspirations.jsonl` is merge-protected by a "
                  f"COMMUTATIVE merge handler, which by construction cannot "
                  f"encode a deletion — a key absent from this write and present "
                  f"remotely is restored on merge.\n"
                  f"[migrate] The FOLDS did land, so the previously-invisible "
                  f"content is now readable in `description`; the duplicate keys "
                  f"are inert until the merge handler learns a TOMBSTONE.",
                  file=sys.stderr)
            return 4

    print("\n[migrate] " + ("APPLIED" if args.apply else "DRY RUN") + ": "
          + ", ".join(f"{k}={v}" for k, v in sorted(counters.items()))
          + (f" | {len(archive)} occurrence(s) enumerated" if not args.apply else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
