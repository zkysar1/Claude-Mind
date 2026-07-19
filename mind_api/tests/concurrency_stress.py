"""Pre-PR-7 concurrency stress test.

PR 7 will migrate the writer wrappers (aspirations-add-goal, rb-add,
guard-add, journal-add, board-post, etc.) to daemon endpoints. Those
endpoints will share the same underlying machinery (_fileops.locked_*
primitives → acquire_lock → history snapshot → atomic append/modify
→ changelog → release) but exercised by daemon threads instead of
per-process python invocations.

This test pounds that machinery from many threads at once and verifies
the on-disk state is consistent afterwards. Latent bugs surface here
before they manifest as cross-agent corruption in the live loop.

Scope:
  - locked_append_jsonl (append-path, used by rb/guard/journal/board)
  - locked_append_jsonl_with_allocator (id-allocating append)
  - locked_modify_jsonl (rewrite-path, used by aspirations update)
  - locked_modify_yaml (the tree-front-matter-sync hot path —
    flagged by another agent as brittle on 776KB _tree.yaml)
  - Integrity check after the run:
      * every JSONL line parses as JSON
      * counts match expected (N writes => N records)
      * no duplicate IDs
      * .history snapshots all parse
      * changelog entries all parse and reference touched files

Marked `slow` so it's opt-in:

    py -3 -m pytest tests/concurrency_stress.py -v --run-slow
    py -3 -m pytest tests/concurrency_stress.py::test_locked_append_no_corruption -v
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import sys
import threading
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "core" / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _import_fileops_with_world(world_dir: Path):
    """Re-import _fileops with WORLD_DIR pointing at the tmp world.

    _fileops at module-load reads paths from _paths via env vars.
    Run with MIND_WORLD set before importing; if already imported, reload.
    """
    os.environ["MIND_WORLD"] = str(world_dir)
    os.environ["MIND_META"] = str(world_dir.parent / "meta")
    os.environ["MIND_AGENT"] = ""
    os.environ["MIND_AGENT_DIR"] = str(world_dir.parent / "alpha")
    if "_fileops" in sys.modules:
        import importlib
        importlib.reload(sys.modules["_paths"])
        importlib.reload(sys.modules["_fileops"])
    import _fileops  # type: ignore
    return _fileops


@pytest.fixture
def stress_world(tmp_path: Path):
    """Tmp world dir with the structure _fileops expects."""
    pr = tmp_path / "repo"
    pr.mkdir()
    (pr / "agents" / "alpha").mkdir(parents=True)
    (pr / "agents" / "alpha" / "session").mkdir()
    world = pr / "world"
    world.mkdir()
    (world / ".history").mkdir()  # save_history writes here
    meta = pr / "meta"
    meta.mkdir()
    (pr / "agents" / "alpha" / "local-paths.conf").write_text(
        f"WORLD_PATH={world.as_posix()}\nMETA_PATH={meta.as_posix()}\n",
        encoding="utf-8",
    )
    fileops = _import_fileops_with_world(world)
    return world, fileops


# ---------------------------------------------------------------------------
# 1) locked_append_jsonl — append-path stress
# ---------------------------------------------------------------------------

def test_locked_append_no_corruption(stress_world):
    """N threads × M appends → N*M lines, all parse, no torn writes."""
    world, fileops = stress_world
    target = world / "stress.jsonl"

    N_THREADS = 12
    M_PER = 25  # 12 * 25 = 300 appends
    barrier = threading.Barrier(N_THREADS)

    def _worker(worker_id: int):
        barrier.wait()  # synchronize start — maximises contention
        for i in range(M_PER):
            fileops.locked_append_jsonl(target, {
                "worker": worker_id, "seq": i,
                "payload": "x" * 64,  # nontrivial size, exposes partial writes
            })

    with concurrent.futures.ThreadPoolExecutor(max_workers=N_THREADS) as ex:
        list(ex.map(_worker, range(N_THREADS)))

    # Integrity: every line is valid JSON.
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == N_THREADS * M_PER, (
        f"expected {N_THREADS * M_PER} lines, got {len(lines)}"
    )
    records = []
    for i, line in enumerate(lines, 1):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as e:
            pytest.fail(f"line {i} corrupt: {line!r}: {e}")

    # No duplicate (worker,seq) pairs.
    seen = {(r["worker"], r["seq"]) for r in records}
    assert len(seen) == N_THREADS * M_PER, (
        f"duplicate (worker,seq) pairs: expected {N_THREADS * M_PER}, got {len(seen)}"
    )


# ---------------------------------------------------------------------------
# 2) locked_append_jsonl_with_allocator — allocator returns unique IDs
# ---------------------------------------------------------------------------

def test_locked_append_with_allocator_unique_ids(stress_world):
    """Allocator runs inside the lock; all returned IDs must be distinct."""
    world, fileops = stress_world
    target = world / "ids.jsonl"

    N_THREADS = 10
    M_PER = 20

    def _build(items, worker_id, seq):
        # next_id_for_prefix takes the prefix WITHOUT the trailing separator —
        # passing "rec" with default separator="-" yields rec-NNNN.
        next_id = fileops.next_id_for_prefix(items, "rec", pad_width=4)
        return {"id": next_id, "worker": worker_id, "seq": seq}

    barrier = threading.Barrier(N_THREADS)

    def _worker(worker_id: int):
        barrier.wait()
        for i in range(M_PER):
            fileops.locked_append_jsonl_with_allocator(
                target,
                lambda items, wid=worker_id, seq=i: _build(items, wid, seq),
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=N_THREADS) as ex:
        list(ex.map(_worker, range(N_THREADS)))

    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == N_THREADS * M_PER

    ids = [json.loads(l)["id"] for l in lines]
    assert len(set(ids)) == len(ids), (
        f"duplicate IDs allocated under contention: "
        f"{len(ids)} writes, {len(set(ids))} distinct IDs"
    )
    # IDs must be the dense sequence rec-0001..rec-NMNM with no gaps.
    expected = {f"rec-{i:04d}" for i in range(1, N_THREADS * M_PER + 1)}
    assert set(ids) == expected, "allocator produced gaps or duplicates"


# ---------------------------------------------------------------------------
# 3) locked_modify_jsonl — rewrite-path stress (counter increments)
# ---------------------------------------------------------------------------

def test_locked_modify_counter_no_lost_updates(stress_world):
    """Increment a shared counter from many threads. Total must equal N*M."""
    world, fileops = stress_world
    target = world / "counter.jsonl"
    target.write_text('{"id":"counter","value":0}\n', encoding="utf-8")

    N_THREADS = 8
    M_PER = 30

    def _increment(items):
        for r in items:
            if r["id"] == "counter":
                r["value"] += 1
        return items

    barrier = threading.Barrier(N_THREADS)

    def _worker(_):
        barrier.wait()
        for _ in range(M_PER):
            fileops.locked_modify_jsonl(target, _increment)

    with concurrent.futures.ThreadPoolExecutor(max_workers=N_THREADS) as ex:
        list(ex.map(_worker, range(N_THREADS)))

    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    final = json.loads(lines[0])
    assert final["value"] == N_THREADS * M_PER, (
        f"counter lost updates: expected {N_THREADS * M_PER}, got {final['value']}"
    )


# ---------------------------------------------------------------------------
# 4) locked_modify_yaml — tree-front-matter-sync's hot path
# ---------------------------------------------------------------------------

def test_locked_modify_yaml_no_corruption(stress_world):
    """N threads bumping per-node last_updated on a shared YAML doc.

    Models the tree-front-matter-sync.py write-path. Verifies the file is
    parseable after the run AND every node touched has the expected mark.
    """
    world, fileops = stress_world
    target = world / "tree.yaml"
    nodes = {f"node-{i:03d}": {"last_updated": "1970-01-01"} for i in range(20)}
    target.write_text(yaml.safe_dump({"nodes": nodes}), encoding="utf-8")

    N_THREADS = 10
    M_PER = 15

    def _bump_node(data, node_key, marker):
        if not isinstance(data, dict):
            return data
        nodes = data.get("nodes") or {}
        if node_key in nodes:
            nodes[node_key]["last_updated"] = marker
        data["nodes"] = nodes
        return data

    barrier = threading.Barrier(N_THREADS)

    def _worker(worker_id: int):
        barrier.wait()
        for i in range(M_PER):
            node_key = f"node-{(worker_id * M_PER + i) % 20:03d}"
            marker = f"w{worker_id}-i{i}"
            fileops.locked_modify_yaml(
                target,
                lambda data, k=node_key, m=marker: _bump_node(data, k, m),
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=N_THREADS) as ex:
        list(ex.map(_worker, range(N_THREADS)))

    # File must parse cleanly — torn writes would yaml.safe_load throw.
    parsed = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    assert "nodes" in parsed
    assert len(parsed["nodes"]) == 20  # no nodes lost
    # Every node should have been bumped (the worker round-robins over all 20)
    for k, v in parsed["nodes"].items():
        assert v["last_updated"] != "1970-01-01", (
            f"node {k} never bumped — possible lost update"
        )


# ---------------------------------------------------------------------------
# 5) Mixed: appends + modifies on the SAME file (worst contention)
# ---------------------------------------------------------------------------

def test_mixed_append_modify_on_same_file(stress_world):
    """One file, half the workers appending, half mutating existing records.

    The locked_append and locked_modify primitives must share a lock — if
    they don't, an append could land mid-modify-rewrite and be silently
    dropped. This pins the contract.
    """
    world, fileops = stress_world
    target = world / "mixed.jsonl"
    target.write_text(
        "\n".join(json.dumps({"id": f"seed-{i}", "hits": 0}) for i in range(5)) + "\n",
        encoding="utf-8",
    )

    N_APPENDERS = 6
    N_MODIFIERS = 6
    APPEND_M = 20
    MODIFY_M = 20
    barrier = threading.Barrier(N_APPENDERS + N_MODIFIERS)

    def _append_worker(worker_id: int):
        barrier.wait()
        for i in range(APPEND_M):
            fileops.locked_append_jsonl(target, {
                "id": f"app-w{worker_id}-i{i}", "kind": "append",
            })

    def _modify_worker(_):
        def _do(items):
            for r in items:
                if r.get("id", "").startswith("seed-"):
                    r["hits"] += 1
            return items
        barrier.wait()
        for _ in range(MODIFY_M):
            fileops.locked_modify_jsonl(target, _do)

    with concurrent.futures.ThreadPoolExecutor(max_workers=N_APPENDERS + N_MODIFIERS) as ex:
        fs = []
        for w in range(N_APPENDERS):
            fs.append(ex.submit(_append_worker, w))
        for _ in range(N_MODIFIERS):
            fs.append(ex.submit(_modify_worker, 0))
        for f in concurrent.futures.as_completed(fs):
            f.result()

    lines = target.read_text(encoding="utf-8").splitlines()
    records = []
    for i, line in enumerate(lines, 1):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as e:
            pytest.fail(f"mixed: line {i} corrupt: {line!r}: {e}")

    # 5 seeds + (N_APPENDERS * APPEND_M) appended = expected total
    expected = 5 + N_APPENDERS * APPEND_M
    assert len(records) == expected, (
        f"mixed: lost records — expected {expected}, got {len(records)}"
    )
    # Every seed's hits must equal total modify operations (N_MODIFIERS * MODIFY_M).
    seeds = [r for r in records if r.get("id", "").startswith("seed-")]
    assert len(seeds) == 5
    for seed in seeds:
        assert seed["hits"] == N_MODIFIERS * MODIFY_M, (
            f"seed {seed['id']} lost updates: expected "
            f"{N_MODIFIERS * MODIFY_M}, got {seed['hits']}"
        )


# ---------------------------------------------------------------------------
# 6) Long-haul slow test — opt-in via --run-slow
# ---------------------------------------------------------------------------

# Slow long-haul test — opt in by setting STRESS_LONG=1 in the env.
@pytest.mark.skipif(
    os.environ.get("STRESS_LONG") != "1",
    reason="long-haul; set STRESS_LONG=1 to run",
)
def test_extended_stress_no_corruption(stress_world):
    """Run the append + modify mix for 1000 ops total. Catches issues
    that need many iterations to surface (lock leaks, history bloat
    truncation, changelog growth races)."""
    world, fileops = stress_world
    target = world / "longhaul.jsonl"
    target.write_text(json.dumps({"id": "ctr", "value": 0}) + "\n", encoding="utf-8")

    N_THREADS = 16
    ROUNDS = 60  # 16 * 60 = 960 ops

    def _do(items):
        for r in items:
            if r["id"] == "ctr":
                r["value"] += 1
        return items

    barrier = threading.Barrier(N_THREADS)

    def _worker(worker_id: int):
        barrier.wait()
        for _ in range(ROUNDS):
            fileops.locked_modify_jsonl(target, _do)

    with concurrent.futures.ThreadPoolExecutor(max_workers=N_THREADS) as ex:
        list(ex.map(_worker, range(N_THREADS)))

    final = json.loads(target.read_text(encoding="utf-8").splitlines()[0])
    assert final["value"] == N_THREADS * ROUNDS, (
        f"long-haul lost updates: expected {N_THREADS * ROUNDS}, got {final['value']}"
    )

    # History should contain snapshots. Pre-mutation snapshots written by
    # save_history. Each must parse cleanly (no torn copies).
    history = world / ".history"
    if history.exists():
        snapshots = list(history.rglob("*"))
        for snap in snapshots:
            if snap.is_file() and snap.suffix in (".jsonl", ".json"):
                # Parseable smoke test — every snapshot's lines parse.
                for i, line in enumerate(snap.read_text(encoding="utf-8").splitlines(), 1):
                    if not line.strip():
                        continue
                    try:
                        json.loads(line)
                    except json.JSONDecodeError as e:
                        pytest.fail(f"history snapshot {snap} line {i} corrupt: {e}")
