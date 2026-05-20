"""test_jsonl_id_race.py — concurrency smoke test for the JSONL id-allocation
race fix.

Verifies that concurrent rb_add and guard_add invocations with NO supplied
id all land with distinct ids and ALL records survive in the live file.

Before the fix (locked_append_jsonl_with_allocator), rb_add did:
  items = read_jsonl(RB_PATH)            # NO lock
  check_no_duplicate_id(items, rec.id)   # against stale baseline
  append_jsonl(RB_PATH, rec)             # lock acquired only here

Two concurrent agents could read the same baseline before either appended,
both pass the dup-check (because neither's record was on disk yet), and
both append — silent duplicate, no error raised.

The test seeds backups of the live rb/guard JSONLs, spawns N concurrent
subprocesses for each (each calls reasoning-bank-add.sh without --id, the
script auto-allocates), asserts:
  1. Every subprocess succeeded (rc == 0).
  2. Every record landed on disk.
  3. All allocated ids are distinct.
  4. All ids match the expected prefix-NNN regex.

Restores the backup in finally so the live file is untouched.

Run: bash core/scripts/tests/test_jsonl_id_race.sh
     OR  py -3 core/scripts/tests/test_jsonl_id_race.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

from _paths import WORLD_DIR  # type: ignore

RB_PATH = WORLD_DIR / "reasoning-bank.jsonl"
GUARD_PATH = WORLD_DIR / "guardrails.jsonl"
RB_BACKUP = RB_PATH.with_suffix(f".jsonl.race-test-backup.{os.getpid()}")
GUARD_BACKUP = GUARD_PATH.with_suffix(f".jsonl.race-test-backup.{os.getpid()}")

N_RACERS_PER_KIND = 8
# Bypass the .sh wrappers — Git Bash on Windows mangles Windows-format
# script paths passed via subprocess argv. Invoke reasoning-bank.py
# directly via the Python interpreter; same effect as the .sh wrappers
# (which only do `cd PROJECT_ROOT && exec python3 reasoning-bank.py rb add`).
RB_PY = CORE_SCRIPTS / "reasoning-bank.py"

RB_ID_RE = re.compile(r"^rb-\d+$")
GUARD_ID_RE = re.compile(r"^guard-\d+$")

# Marker tags so the assertion can pick OUT the test records from any
# unrelated records that may have been added during the race window
# (e.g., a partner agent's normal write to the live file). We do NOT
# rely on ID-prefix exclusivity to identify our records.
TEST_TAG = f"race-test-{os.getpid()}"


def _make_rb_payload(label: str) -> str:
    return json.dumps({
        # id intentionally omitted — allocator picks rb-{max+1} inside lock
        "title": f"race-test rb {label}",
        "type": "success",  # RB_VALID_TYPES = {success, failure, user_provided}
        "category": "test-race",
        "content": "concurrent-add smoke test record",
        "applies_to": "specific",  # required since 2026-05-10 (P3 #14)
        "tags": [TEST_TAG],
        "source_goal": None,  # opt out of team-state in_flight inference
    })


def _make_guard_payload(label: str) -> str:
    return json.dumps({
        # id intentionally omitted
        "rule": f"race-test guard {label}",
        "category": "test-race",
        "trigger_condition": "never — this is a test fixture",
        "action_hint": "test-only fixture; no action",
        "source": "test_jsonl_id_race.py",
        "severity": "LOW",
        "tags": [TEST_TAG],
        "phases": [],
    })


def _run_add(spec: tuple[str, str]) -> tuple[str, subprocess.CompletedProcess]:
    """spec is (kind, label). kind in {"rb", "guard"}."""
    kind, label = spec
    if kind == "rb":
        argv = [sys.executable, str(RB_PY), "rb", "add"]
        payload = _make_rb_payload(label)
    else:
        argv = [sys.executable, str(RB_PY), "guard", "add"]
        payload = _make_guard_payload(label)

    env = os.environ.copy()
    env["MIND_AGENT"] = "alpha"
    # PYTHONIOENCODING=utf-8 so json.dumps with ensure_ascii=False output
    # to stdout doesn't trip cp1252 on Windows when records contain unicode.
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        argv,
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        cwd=str(CORE_SCRIPTS.parent.parent),  # PROJECT_ROOT
    )
    return label, proc


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    items = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped:
                try:
                    items.append(json.loads(stripped))
                except json.JSONDecodeError:
                    pass
    return items


def main() -> int:
    # Backup live files
    if RB_PATH.exists():
        RB_BACKUP.write_bytes(RB_PATH.read_bytes())
        print(f"backed up live reasoning-bank.jsonl to {RB_BACKUP.name}")
    if GUARD_PATH.exists():
        GUARD_BACKUP.write_bytes(GUARD_PATH.read_bytes())
        print(f"backed up live guardrails.jsonl to {GUARD_BACKUP.name}")

    try:
        specs = []
        for i in range(N_RACERS_PER_KIND):
            specs.append(("rb", f"rb-{i}"))
            specs.append(("guard", f"guard-{i}"))

        start = time.time()
        with ThreadPoolExecutor(max_workers=len(specs)) as ex:
            results = list(ex.map(_run_add, specs))
        elapsed = time.time() - start
        print(f"N={len(specs)} racers completed in {elapsed:.2f}s")

        # Surface any subprocess failures
        failed = [(label, r) for label, r in results if r.returncode != 0]
        if failed:
            print(f"TEST FAIL: {len(failed)} subprocess(es) errored:",
                  file=sys.stderr)
            for label, r in failed:
                print(f"  {label}: rc={r.returncode}", file=sys.stderr)
                print(f"    stdout: {(r.stdout or '')[:200]}",
                      file=sys.stderr)
                print(f"    stderr: {(r.stderr or '')[:400]}",
                      file=sys.stderr)
            return 1

        # Pull rb and guard test records from disk by tag
        rb_items = [r for r in _read_jsonl(RB_PATH)
                    if TEST_TAG in (r.get("tags") or [])]
        guard_items = [r for r in _read_jsonl(GUARD_PATH)
                       if TEST_TAG in (r.get("tags") or [])]

        rb_ids = [r.get("id") for r in rb_items]
        guard_ids = [r.get("id") for r in guard_items]

        ok = True

        # 1. Counts
        if len(rb_items) != N_RACERS_PER_KIND:
            print(f"TEST FAIL: expected {N_RACERS_PER_KIND} rb records "
                  f"(by tag), found {len(rb_items)}", file=sys.stderr)
            ok = False
        if len(guard_items) != N_RACERS_PER_KIND:
            print(f"TEST FAIL: expected {N_RACERS_PER_KIND} guard records "
                  f"(by tag), found {len(guard_items)}", file=sys.stderr)
            ok = False

        # 2. All ids distinct
        if len(set(rb_ids)) != len(rb_ids):
            from collections import Counter
            dups = [k for k, v in Counter(rb_ids).items() if v > 1]
            print(f"TEST FAIL: rb id collisions: {dups}", file=sys.stderr)
            ok = False
        if len(set(guard_ids)) != len(guard_ids):
            from collections import Counter
            dups = [k for k, v in Counter(guard_ids).items() if v > 1]
            print(f"TEST FAIL: guard id collisions: {dups}", file=sys.stderr)
            ok = False

        # 3. Format
        bad_rb = [i for i in rb_ids if not (i and RB_ID_RE.match(i))]
        bad_guard = [i for i in guard_ids if not (i and GUARD_ID_RE.match(i))]
        if bad_rb:
            print(f"TEST FAIL: rb ids fail regex: {bad_rb}", file=sys.stderr)
            ok = False
        if bad_guard:
            print(f"TEST FAIL: guard ids fail regex: {bad_guard}",
                  file=sys.stderr)
            ok = False

        if not ok:
            return 1

        print(f"TEST PASS: {N_RACERS_PER_KIND} concurrent rb_add → "
              f"{len(set(rb_ids))} unique ids; "
              f"{N_RACERS_PER_KIND} concurrent guard_add → "
              f"{len(set(guard_ids))} unique ids")
        print(f"  rb ids:    {sorted(rb_ids, key=lambda x: int(x.split('-')[-1]))}")
        print(f"  guard ids: {sorted(guard_ids, key=lambda x: int(x.split('-')[-1]))}")
        return 0
    finally:
        # Restore — wipe whatever the racers wrote
        if RB_BACKUP.exists():
            RB_PATH.write_bytes(RB_BACKUP.read_bytes())
            RB_BACKUP.unlink()
            print("restored live reasoning-bank.jsonl")
        if GUARD_BACKUP.exists():
            GUARD_PATH.write_bytes(GUARD_BACKUP.read_bytes())
            GUARD_BACKUP.unlink()
            print("restored live guardrails.jsonl")


if __name__ == "__main__":
    sys.exit(main())
