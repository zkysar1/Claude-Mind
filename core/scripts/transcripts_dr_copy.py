#!/usr/bin/env python3
"""transcripts_dr_copy — incremental off-site copy of the `transcripts/` prefix
from the LIVE object store to the pinned AWS DR target (g-372-24, 2026-09-16).

WHY THIS EXISTS
---------------
`cold_snapshot.py` walks the LOCAL filesystem and tars `world/`, `meta/` and
every `agents/<name>/` dir. It structurally cannot cover `transcripts/`: that
prefix sits under NO governed root by design (`transcript_archive.py` module
docstring — the sync machinery iterates `backend._roots`, so archived objects
are invisible to every sweep and never materialise on a box). The bytes exist
ONLY in the object store.

Before the 2026-09-14 cutover that was still safe: `transcript_archive.py` wrote
to AWS, so AWS held the only copy and it was off-site by construction. The
cutover repointed it at the basement store. Measured 2026-09-16 (bravo, cc-13):
every one of the 12 machines' last AWS-side transcript write is 2026-09-14
18:13-19:16Z, and MinIO has since accumulated objects with no off-box copy at
all. A single-drive loss on the basement box takes them.

THE POSTURE THIS IMPLEMENTS (owner-approved 2026-09-16, runbook §13.6a)
-----------------------------------------------------------------------
Copy the delta daily to the SAME pinned AWS DR target the cold snapshot uses —
never widen the weekly tarball to carry transcripts (~20 GiB of already-archived
objects re-tarred forever) and never stand up a second basement mirror (one
building is one site, runbook §12 risk 3).

WHAT IT REUSES, AND WHY THAT MATTERS
------------------------------------
* `cold_snapshot.resolve_cold_target` decides the destination. That function is
  PURE and already encodes the refusals this lane needs identically: it refuses
  `refused-colocated` when the DR endpoint IS the live store's endpoint, because
  same hardware is not outside the blast radius whatever the bucket. Re-rolling
  that check here would be a second place for it to rot.
* `owncloud-store-enumerate.py copy` does the transfer. It already carries the
  measured resume semantics, the threading that makes an object-count-bound copy
  affordable, and the multipart-ETag discipline of guard-6371.

`--multipart-compare size` is MANDATORY on this lane and is not a tuning knob.
The copier's default arm treats "source newer than destination" as "re-copy",
which is correct only when the destination was written FROM the source. Here the
destination is the PRE-cutover original and the source is the migrated copy, so
every multipart object reads as newer forever. Measured the same day: the default
arm proposed 615 objects / 19.80 GiB where the true delta was 516 / 4.16 GiB —
i.e. it would re-upload the entire archive every single day.

Size equality is NOT content verification (guard-6371) and this lane does not
claim it is. It is the right predicate for an append-only archive, where a key is
written once and only ever grows.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

ARCHIVE_PREFIX = "transcripts"
COPIER = SCRIPT_DIR / "owncloud-store-enumerate.py"
DEFAULT_WORKERS = 32
DEFAULT_TIMEOUT_S = 3600


def _prefix_for(env_id: str, customer_prefix: str) -> str:
    """The key prefix this lane copies. Built from the SAME expression the
    copier uses for its own default (`<customer_prefix><env_id>/`) so the two
    cannot drift into copying different subtrees."""
    return f"{customer_prefix}{env_id}/{ARCHIVE_PREFIX}/"


def _resolve_prefix() -> str:
    from owncloud_backend import OwnCloudBackend
    be = OwnCloudBackend.from_env()
    return _prefix_for(be.env_id, be._customer_prefix())


def run(dry_run: bool, workers: int, timeout_s: int) -> dict:
    from cold_snapshot import AWS_REGIONAL, resolve_cold_target, target_line

    target = resolve_cold_target(os.environ)
    # guard-5551 parity: the destination-selecting values are the FIRST thing a
    # reader (or a pre-flip check) sees, on every run, before any work.
    print(target_line(target), file=sys.stderr)

    base = {"op": "transcripts-dr-copy", "target": target}

    if target["mode"] == "refused":
        return {**base, "verdict": target["verdict"], "reason": target["reason"]}
    if target["mode"] == "local":
        return {**base, "verdict": "skipped-local-backend", "reason": target["reason"]}
    if target["mode"] == "live":
        # The live store IS AWS, so `transcript_archive.py` is already writing
        # off-site and source and destination are the same bucket. Copying would
        # be a no-op against itself.
        return {**base, "verdict": "skipped-live-is-dr",
                "reason": "the live store is the AWS DR target; transcripts are "
                          "already written off-site by transcript_archive.py"}

    dest_endpoint = "" if target["endpoint"] == AWS_REGIONAL else target["endpoint"]
    argv = [
        sys.executable, str(COPIER), "copy",
        "--prefix", _resolve_prefix(),
        "--bucket", target["live_bucket"],
        "--dest-bucket", target["bucket"],
        "--source-endpoint", os.environ.get("STORAGE_S3_ENDPOINT_URL", ""),
        "--dest-endpoint", dest_endpoint,
        "--multipart-compare", "size",
        "--workers", str(workers),
        "--progress-every", "0",
    ]
    if dry_run:
        argv.append("--dry-run")

    proc = subprocess.run(argv, capture_output=True, text=True,
                          timeout=timeout_s, cwd=str(SCRIPT_DIR.parent.parent))
    try:
        report = json.loads((proc.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        report = {}

    failed = report.get("failed")
    if not report:
        verdict = "error"
    elif proc.returncode != 0 or (failed or 0) > 0:
        verdict = "failed"
    else:
        verdict = "dry-run" if dry_run else "ok"

    return {**base, "verdict": verdict, "copied": report.get("copied"),
            "skipped": report.get("skipped_already_present"),
            "failed": failed, "scanned": report.get("scanned"),
            "copied_gib": report.get("copied_gib"),
            "elapsed_s": report.get("elapsed_s"),
            "prefix": report.get("prefix"),
            "detail": (proc.stderr or "").strip()[-500:],
            "errors": (report.get("errors") or [])[:3]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be copied; transfer nothing")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--timeout-s", type=int, default=DEFAULT_TIMEOUT_S)
    ap.add_argument("--output", choices=("text", "json"), default="text")
    args = ap.parse_args()

    result = run(args.dry_run, args.workers, args.timeout_s)
    if args.output == "json":
        print(json.dumps(result))
    else:
        print(f"verdict={result['verdict']} copied={result.get('copied')} "
              f"skipped={result.get('skipped')} failed={result.get('failed')} "
              f"gib={result.get('copied_gib')}")
    # 0 on every non-failure verdict; the refusals are deliberate states, not
    # crashes, and the caller reads `verdict` (guard-5551 shape).
    return 0 if result["verdict"] in ("ok", "dry-run", "skipped-local-backend",
                                      "skipped-live-is-dr") else 2


if __name__ == "__main__":
    raise SystemExit(main())
