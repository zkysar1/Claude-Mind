"""test_jsonl_id_race.py — concurrent ID-allocation race test for the
rb-NNN / guard-NNN JSONL stores.

History (g-115-2351 rewrite): the original version raced 16 subprocess
invocations of `reasoning-bank.py rb|guard add` while swapping the LIVE
world files aside (backup/restore around the race — a clobber hazard on
any mid-run crash). Both halves rotted: H2 Wave 2 (2026-05-15) removed the
rb CLI subcommands, so every racer imported the library and exited 0
without writing — 16 no-ops racing against a production-store backup swap.
The allocation race the test exists for now lives in the DAEMON:
POST /v1/store/append allocates ids via next_id_for_prefix under the
store's file lock (mind_api/src/endpoints/store.py + store_registry).

This rewrite races concurrent HTTP appends against an in-process
DaemonFixture daemon (ThreadingHTTPServer — real thread concurrency)
rooted in a tmp world. No live-store contact, no backup/swap.

Race property pinned: N concurrent appends per store yield N unique
sequential-set ids, N parseable records on disk, zero drops.

Run: py -3 core/scripts/tests/test_jsonl_id_race.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
sys.path.insert(0, str(CORE_SCRIPTS))
sys.path.insert(0, str(SCRIPT_DIR))

from _daemon_fixture import DaemonFixture  # noqa: E402

RACERS_PER_STORE = 8
TEST_TAG = "id-race-test"


def _post_append(port: int, store: str, record: dict) -> tuple[int, str]:
    url = f"http://127.0.0.1:{port}/v1/store/append?store={store}"
    data = json.dumps(record).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Mind-Agent", "alpha")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


# allow_near_dup on every racer: these fixtures are near-identical BY DESIGN
# (the property under test is id allocation under concurrency, not content
# novelty), and the daemon's near-duplicate refusal tier () would
# otherwise 409 every racer after the first at similarity 1.0. The field is
# the refusal tier's sanctioned bypass — popped server-side, never persisted.
def _rb_record(i: int) -> dict:
    return {
        "title": f"race entry {i}",
        "type": "success",
        "category": "race-test",
        "content": f"concurrent allocator probe {i}",
        "applies_to": "framework",
        "tags": [TEST_TAG],
        "allow_near_dup": True,
    }


def _guard_record(i: int) -> dict:
    return {
        "rule": f"race guard {i}",
        "category": "race-test",
        "trigger_condition": "never — test fixture",
        "source": "id-race-test",
        "when_to_use": "never",
        "tags": [TEST_TAG],
        "allow_near_dup": True,
    }


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        out.append(json.loads(ln))  # a corrupt line IS a failure — let it raise
    return out


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="jsonl-id-race-"))
    world = tmp / "world"
    world.mkdir(parents=True)
    (world / "reasoning-bank.jsonl").write_text("", encoding="utf-8")
    (world / "guardrails.jsonl").write_text("", encoding="utf-8")
    (tmp / "meta").mkdir(exist_ok=True)

    failed: list[str] = []
    try:
        with DaemonFixture(world, agent="alpha") as df:
            port = df.port
            jobs = ([("reasoning-bank", _rb_record(i)) for i in range(RACERS_PER_STORE)]
                    + [("guardrails", _guard_record(i)) for i in range(RACERS_PER_STORE)])
            with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
                results = list(pool.map(
                    lambda j: (j[0], *_post_append(port, j[0], j[1])), jobs))

        ids: dict[str, list[str]] = {"reasoning-bank": [], "guardrails": []}
        for store, status, body in results:
            if status != 200:
                failed.append(f"{store} append failed: {status} {body[:200]}")
                continue
            ids[store].append(json.loads(body)["record"]["id"])

        for store, prefix, path in (
                ("reasoning-bank", "rb-", world / "reasoning-bank.jsonl"),
                ("guardrails", "guard-", world / "guardrails.jsonl")):
            got = ids[store]
            if len(got) != RACERS_PER_STORE:
                failed.append(f"{store}: {len(got)}/{RACERS_PER_STORE} appends succeeded")
                continue
            if len(set(got)) != RACERS_PER_STORE:
                dupes = sorted({i for i in got if got.count(i) > 1})
                failed.append(f"{store}: DUPLICATE ids allocated under race: {dupes}")
            if not all(i.startswith(prefix) for i in got):
                failed.append(f"{store}: malformed ids: {sorted(got)}")
            # Sequential set 1..N — the allocator must not skip under contention.
            nums = sorted(int(i[len(prefix):]) for i in got)
            if nums != list(range(1, RACERS_PER_STORE + 1)):
                failed.append(f"{store}: expected ids 1..{RACERS_PER_STORE}, got {nums}")

            recs = _read_jsonl(path)
            tagged = [r for r in recs if TEST_TAG in (r.get("tags") or [])]
            if len(tagged) != RACERS_PER_STORE:
                failed.append(f"{store}: {len(tagged)} records on disk, "
                              f"expected {RACERS_PER_STORE} (dropped writes)")
            disk_ids = {r["id"] for r in tagged}
            if disk_ids != set(got):
                failed.append(f"{store}: disk ids {sorted(disk_ids)} != "
                              f"returned ids {sorted(got)}")

        if failed:
            print("\n".join(f"FAIL: {f}" for f in failed), file=sys.stderr)
            return 1
        print(f"PASS: {RACERS_PER_STORE} concurrent appends per store allocated "
              f"unique sequential ids with zero drops (rb + guardrails, "
              f"daemon allocator under thread race).")
        return 0

    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
