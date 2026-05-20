"""test_retrieve_write_locking.py — concurrency regression test for retrieve.py.

Pre-fix, retrieve.py wrote `_tree.yaml`, `reasoning-bank.jsonl`,
`guardrails.jsonl`, `pattern-signatures.jsonl`, and `experience.jsonl`
with a bare `tmp + os.replace` — no `_fileops.acquire_lock`, no
retry-with-backoff. That meant a concurrent retrieve / tree.py / *-add.sh
write race could silently drop counter bumps. This test catches regressions
of that pattern.

The test spawns N=8 concurrent invocations of retrieve.py's `_locked_bump_jsonl`
helper against a temp JSONL file with one record. Without locking, some of
those increments would be lost to read-modify-write races. With locking,
final retrieval_count == N (every increment landed).

Pure stdlib + PyYAML; runs in <2s on a cold cache. Self-contained: never
touches the live world directory.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# retrieve.py imports `from _paths import ...` at module load — _paths reads
# MIND_WORLD env. Set it to a temp dir BEFORE the import so retrieve.py
# binds to our scratch paths, not the live world.
#  capture-restore pattern: stash env before module-level mutation
# so subsequent tests in the same pytest session don't inherit a popped
# MIND_AGENT. See test_applies_to_required.py for full rationale.
_ORIG_MIND_WORLD = os.environ.get("MIND_WORLD")
_ORIG_MIND_AGENT = os.environ.get("MIND_AGENT")

_TMPDIR = tempfile.mkdtemp(prefix="retrieve-lock-test-")
os.environ["MIND_WORLD"] = _TMPDIR
# Ensure _paths.py doesn't try to discover a per-agent local-paths.conf —
# the test does not need an agent context.
os.environ.pop("MIND_AGENT", None)

# Now safe to import. Hyphenated module name is not importable directly;
# load via importlib.
import importlib.util  # noqa: E402

_RETRIEVE_PATH = CORE_SCRIPTS / "retrieve.py"
_spec = importlib.util.spec_from_file_location("retrieve_mod", _RETRIEVE_PATH)
_retrieve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_retrieve)

# Restore env so downstream tests inherit clean conftest defaults.
if _ORIG_MIND_WORLD is not None:
    os.environ["MIND_WORLD"] = _ORIG_MIND_WORLD
elif "MIND_WORLD" in os.environ:
    del os.environ["MIND_WORLD"]
if _ORIG_MIND_AGENT is not None:
    os.environ["MIND_AGENT"] = _ORIG_MIND_AGENT

N_RACERS = 8


def _seed_jsonl(path: Path, n_records: int = 3) -> list:
    """Write n_records active reasoning-bank-shaped records. Returns the records."""
    records = []
    for i in range(n_records):
        records.append({
            "id": f"rb-test-{i:03d}",
            "title": f"test record {i}",
            "category": "test-category",
            "status": "active",
            "utilization": {
                "retrieval_count": 0,
                "last_retrieved": None,
                "times_helpful": 0,
                "times_noise": 0,
            },
        })
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    return records


def _read_jsonl(path: Path) -> list:
    items = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped:
                items.append(json.loads(stripped))
    return items


def _bump_once(path: Path):
    """Single racer: bump every active record's retrieval_count by 1."""
    return _retrieve._locked_bump_jsonl(
        path,
        lambda rec: rec.get("status") == "active",
    )


def main() -> int:
    tmp = Path(_TMPDIR)
    tmp.mkdir(parents=True, exist_ok=True)
    target = tmp / "rb-test.jsonl"

    seeded = _seed_jsonl(target, n_records=3)
    initial_counts = {r["id"]: r["utilization"]["retrieval_count"] for r in seeded}
    assert all(v == 0 for v in initial_counts.values()), "seed counters must start at 0"

    # Race N_RACERS concurrent _locked_bump_jsonl calls. Each bumps every
    # active record's retrieval_count by 1. Without locking, racers would
    # share read snapshots and lose increments → final count < N_RACERS.
    failures = []
    with ThreadPoolExecutor(max_workers=N_RACERS) as pool:
        futures = [pool.submit(_bump_once, target) for _ in range(N_RACERS)]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as e:
                failures.append(repr(e))

    if failures:
        print(f"FAIL: {len(failures)} racers raised: {failures}", file=sys.stderr)
        return 1

    # Each of the 3 records should now have retrieval_count == N_RACERS.
    final = _read_jsonl(target)
    final_counts = {r["id"]: r["utilization"]["retrieval_count"] for r in final}

    expected = N_RACERS
    losses = {k: v for k, v in final_counts.items() if v != expected}
    if losses:
        print(f"FAIL: counter losses detected — expected {expected} on every record, "
              f"got: {losses}", file=sys.stderr)
        # Surface the lost-write count so debugging is concrete:
        for k, v in losses.items():
            print(f"  {k}: lost {expected - v} of {expected} increments", file=sys.stderr)
        return 1

    # Sanity: all records still have last_retrieved set
    missing_ts = [r["id"] for r in final if not r["utilization"].get("last_retrieved")]
    if missing_ts:
        print(f"FAIL: last_retrieved missing on {missing_ts}", file=sys.stderr)
        return 1

    # Sanity: file ends with newline (no torn last record)
    raw = target.read_text(encoding="utf-8")
    if not raw.endswith("\n"):
        print(f"FAIL: JSONL file missing trailing newline (torn write?)", file=sys.stderr)
        return 1

    print(f"PASS: {N_RACERS} concurrent _locked_bump_jsonl racers — "
          f"all {len(final)} records show retrieval_count={expected}, no losses")
    return 0


if __name__ == "__main__":
    sys.exit(main())
