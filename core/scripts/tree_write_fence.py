"""tree_write_fence — lost-update detection for knowledge-tree .md bodies ().

THE GAP THIS CLOSES
-------------------
Every other shared store in this framework fences its writes:

  * JSONL stores  -> daemon read-modify-write with 412/write_conflict (rb-2639, rb-3280)
  * meta YAML     -> locked_rmw + force_fresh (g-115-3177)
  * _tree.yaml    -> _fileops.locked_modify_yaml (the tree INDEX)

The knowledge-tree .md NODE BODIES -- the store this framework exists to
produce -- are written with the raw Edit/Write tool. Measured 2026-07-28 on
both writers:

    grep -cE 'write_conflict|locked_rmw|force_fresh|IfMatch|if_match' \
        core/scripts/tree.py mind_api/src/world/tree_write.py
    -> 0 and 0

No lock, no version check, no conflict signal. Two agents that both Read a node
and then both write it produce a silent last-write-wins loss.

Observed live twice in 48h. In the 2026-07-27T20:56 instance a ~64-line encoding
section was verified on disk, then vanished ~4 minutes later under another
agent's write; it was noticed ONLY because the harness happened to emit a "file
was modified" reminder. Nothing failed: no error, no exit code, no conflict. The
verify gate would have agreed the encoding happened, because it checks that the
agent DID the encoding, not that it SURVIVED.

WHY DETECT AND NOT PREVENT
--------------------------
A fail-closed PreToolUse gate on Edit is the obvious fix and is the wrong one
here: this framework runs an autonomous loop, and a per-edit hard deny that
false-positives wedges it. That risk is already called out in-tree
(aspirations-precheck Phase 0-pre6: "Does NOT hard-block the Edit tool (a
fail-closed per-edit gate can wedge the loop)"). Routing .md bodies through the
daemon RMW is the strongest option and was also rejected for now: rb-3080
records the tree-INDEX optimistic-concurrency path chronically conflicting (9
straight write_conflicts), so adopting that mechanism for the far-more-numerous
node bodies would trade a silent-loss failure for a loud-stall failure.

So this fences by DETECTION: it is loud, durable, and cannot wedge the loop.
The goal's own acceptance is an OR -- "either prevents a concurrent overwrite or
detects and reports one loudly".

Loud here means BOTH channels, deliberately: a stderr banner AND an appended
JSONL ledger record. stderr alone is not enough -- guard-772 measured that a
warning written only to stderr is invisible when the command runs inside a
backgrounded subprocess, which is exactly how much of this loop executes.

CONTRACT
--------
    record <path>   after a Read (and after our own write) -> store sha256
    check  <path>   before a write -> compare live sha256 against the baseline

A DIVERGED verdict means: the file changed since this session last observed it,
so any edit computed from that observation may silently drop the other writer's
work. The caller re-Reads and re-applies.

Fail-open everywhere. A fence that breaks must never be the outage.
"""
import hashlib
import json
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

# Only node BODIES under the knowledge tree. _tree.yaml (the index) already goes
# through _fileops.locked_modify_yaml and is deliberately out of scope.
_TREE_MARKER = os.path.join("knowledge", "tree")


def _now():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def in_scope(path):
    """True for a knowledge-tree node body (.md under knowledge/tree/)."""
    if not path:
        return False
    p = str(path).replace("\\", "/")
    return p.endswith(".md") and (_TREE_MARKER.replace("\\", "/") in p)


def file_hash(path):
    """sha256 of the file bytes, or None when unreadable/absent.

    Bytes, never text: tree nodes are LF-normalized on disk and a text-mode
    read would fold CRLF on Windows and produce a phantom divergence
    (tree-front-matter-sync.py carries the same warning for the same reason).
    """
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def _key(path):
    """Stable identity for a node across cwd differences."""
    try:
        return str(Path(path).resolve())
    except Exception:
        return str(path)


# --------------------------------------------------------------------------
# Baseline storage: ONE FILE PER NODE, never a shared map.
#
# The first cut of this module kept every baseline in a single JSON object and
# did load -> mutate -> atomic-replace. `os.replace` makes the WRITE atomic; it
# does NOT make the read-modify-write atomic. Hooks fire concurrently (a
# PostToolUse[Read] record can overlap a PostToolUse[Edit] record), so two
# invocations both read the same map, each add their own entry, and the second
# replace silently discards the first.
#
# Measured on the shipped code: 20 nodes recorded concurrently persisted
# **1** baseline. The 19 lost ones degrade to `no_baseline`, which is
# fail-open -- so the fence would have gone quiet on 95% of nodes while
# reporting nothing wrong. That is the exact silent-lost-update class this
# module exists to detect, in the detector itself (found by /fresh-eyes-code
# 20 minutes after it shipped).
#
# The fix is subtraction, not a lock: with one file per node there is no shared
# mutable structure to race on. Each record is an independent atomic replace,
# concurrency-safe by construction, and cheaper (no full-map load per hook
# fire). A lock would have preserved the shared map and added a dependency to
# a hot hook path to defend a structure that did not need to exist.
# --------------------------------------------------------------------------


def _entry_file(store_dir, key):
    return Path(store_dir) / (hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + ".json")


def load_baselines(store_dir):
    """All baselines as {key: record}. Inspection/testing; not the hot path."""
    out = {}
    try:
        for f in Path(store_dir).glob("*.json"):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    rec = json.load(fh)
                if isinstance(rec, dict) and rec.get("key"):
                    out[rec["key"]] = rec
            except Exception:
                continue  # one corrupt entry must not blind the rest
    except Exception:
        return {}
    return out


def save_baseline(store_dir, key, rec):
    """Atomically write ONE node's baseline. No shared state, no lock needed."""
    try:
        Path(store_dir).mkdir(parents=True, exist_ok=True)
        target = _entry_file(store_dir, key)
        # Unique tmp name per WRITER, not per target. The original code used one
        # shared `<store>.tmp`, so concurrent writers raced on the tmp itself and
        # os.replace hit FileNotFoundError when a peer moved it first. pid alone
        # is insufficient -- threads in one process share it -- so include the
        # thread id: two concurrent records of the SAME node must not collide.
        tmp = "%s.%d.%d.tmp" % (target, os.getpid(), threading.get_ident())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, sort_keys=True)
        os.replace(tmp, target)
        return True
    except Exception:
        return False


def read_baseline(store_dir, key):
    try:
        with open(_entry_file(store_dir, key), "r", encoding="utf-8") as f:
            rec = json.load(f)
        return rec if isinstance(rec, dict) else None
    except Exception:
        return None


def record(path, store_path):
    """Snapshot the current hash as this session's baseline for `path`."""
    if not in_scope(path):
        return {"op": "record", "scoped": False, "path": str(path)}
    h = file_hash(path)
    if h is None:
        return {"op": "record", "scoped": True, "recorded": False,
                "reason": "unreadable", "path": str(path)}
    k = _key(path)
    rec = {"key": k, "sha256": h, "at": _now()}
    return {"op": "record", "scoped": True,
            "recorded": save_baseline(store_path, k, rec),
            "sha256": h[:12], "path": str(path)}


def check(path, store_path):
    """Compare the live file against this session's baseline.

    Verdicts:
      not_scoped  -- not a tree node body
      no_baseline -- never observed this session (nothing to compare; allow)
      unreadable  -- cannot hash right now (allow)
      clean       -- unchanged since last observation
      DIVERGED    -- changed underneath us; an edit from the old view may drop work
    """
    if not in_scope(path):
        return {"op": "check", "verdict": "not_scoped", "path": str(path)}
    rec = read_baseline(store_path, _key(path))
    if not rec:
        return {"op": "check", "verdict": "no_baseline", "path": str(path)}
    live = file_hash(path)
    if live is None:
        return {"op": "check", "verdict": "unreadable", "path": str(path)}
    if live == rec.get("sha256"):
        return {"op": "check", "verdict": "clean", "path": str(path)}
    return {
        "op": "check",
        "verdict": "DIVERGED",
        "path": str(path),
        "baseline_sha256": str(rec.get("sha256"))[:12],
        "live_sha256": live[:12],
        "observed_at": rec.get("at"),
    }


def append_ledger(ledger_path, record_obj):
    """Durable half of 'loud' (guard-772: stderr alone vanishes under backgrounding).

    Single-line JSON under PIPE_BUF is single-write atomic in O_APPEND mode --
    the same tradeoff retrieve.py:2048 documents for its trace, and appropriate
    for the same reason: this is observability-grade, not durable-state-grade.
    """
    try:
        Path(ledger_path).parent.mkdir(parents=True, exist_ok=True)
        with open(ledger_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record_obj, ensure_ascii=True) + "\n")
        return True
    except Exception:
        return False


def _default_paths():
    """Resolve the per-agent baseline + ledger locations. Fail-open to None."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _paths import AGENT_DIR  # type: ignore
        base = Path(AGENT_DIR) / "session"
        # DIRECTORY, not a file: one baseline per node (see the storage note
        # above -- a single shared map lost 19 of 20 concurrent records).
        return base / "tree-write-baselines", base / "tree-write-conflicts.jsonl"
    except Exception:
        return None, None


def main(argv):
    if len(argv) < 3:
        print("usage: tree_write_fence.py {record|check} <path>", file=sys.stderr)
        return 0
    op, path = argv[1], argv[2]
    store, ledger = _default_paths()
    if store is None:
        return 0  # no agent bound -> nothing to fence against

    if op == "record":
        print(json.dumps(record(path, store)))
        return 0

    if op == "check":
        verdict = check(path, store)
        if verdict.get("verdict") == "DIVERGED":
            verdict["agent"] = os.environ.get("MIND_AGENT", "unknown")
            verdict["ts"] = _now()
            append_ledger(ledger, verdict)
            print(
                "[tree-write-fence] ⚠ CONFLICT: %s changed on disk since this "
                "session read it (baseline %s @ %s -> live %s). Another writer "
                "landed in between. An edit computed from the old view can "
                "SILENTLY drop their work -- re-Read the file and re-apply your "
                "change before writing. Logged to %s"
                % (verdict["path"], verdict["baseline_sha256"],
                   verdict.get("observed_at"), verdict["live_sha256"], ledger),
                file=sys.stderr,
            )
        print(json.dumps(verdict))
        return 0

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:
        sys.exit(0)  # fail-open: the fence must never be the outage
