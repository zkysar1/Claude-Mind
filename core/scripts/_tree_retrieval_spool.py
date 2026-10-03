"""Machine-local spool for the knowledge-tree INDEX retrieval counters ().

WHY THIS EXISTS. `retrieve.py` ends every non-`--read-only` retrieval that
matched any node by bumping `retrieval_count` / `last_retrieved` on the matched
nodes through `locked_modify_yaml(TREE_PATH, ...)`. On an own-cloud box that is
a whole-object PUT of the entire index: measured 2026-09-17 (echo, cc-03,
closing g-358-163) `_tree.yaml` wrote 804 versions / 1,479,642,382 summed PUT
bytes in 24 h, average PUT 1,840,351 B against a live file of 1,854,264 B — i.e.
every PUT is the whole file — and 186 of that box's 201 changelog rows (92.5%)
carried an empty summary with `lines_changed=0`, because they only rewrite
integer VALUES in place.

`g-358-22` already fixed exactly this for the sidecar-covered JSONL kinds
(`retrieve.py` ~L577-600 routes through `_utilization_store.record_increment`).
The tree index is not in `_utilization_store.KINDS` and kept the legacy shape.
This module is the tree-shaped equivalent — option (b) of the goal's design
note: a local spool drained by the next STRUCTURAL tree write, which already
pays the whole-object PUT, so the flush costs nothing extra.

THE DRAIN HAS TWO DOORS, AND ONLY ONE IS REACHABLE ON EVERY BOX (g-358-231).
`tree.py::write_tree` drains (see `take_for_flush` below), but its only live
callers are `tree-accuracy-sync.py` and `tree_archive.py` -- reducer-side
maintenance. Every op in tree.py writes through `locked_modify_yaml`, and the
daemon's structural writer (`mind_api/src/world/tree_write.py`) dumps with the
same parameters on its own; none of them drains. A box that only retrieves (every
worker box) would therefore have spooled forever: counters absent from the
shared index, and every retrieval re-reading an ever-longer file.
`flush_into_index` is the door every box reaches: one `locked_modify_yaml` per
`DRAIN_INTERVAL_SECONDS`, called from the retrieval that produced the deltas. It
bounds the lag an index reader sees to that interval and the PUT cost to one per
box per interval.

WHY NOT REUSE `_utilization_store`. Its API is RECORD-shaped: `record_increment(
kind, rec_id, counter, ...)` addresses a JSONL row by `id` and its counters live
under a `utilization` sub-map, read back through `utilization_of(rec, counters)`.
The tree index is a YAML map keyed by NODE KEY whose counters sit at the node's
TOP level. Adding a third kind with a different key path would ripple into every
consumer that imports `_utilization_store` / `utilization_of` (12 files measured
2026-09-17) and each would have to tolerate a "kind" whose records are not rows.
That is a large blast radius for a counter bump.

THE HAZARD THIS MODULE MUST NOT CREATE. Between an append here and the next
flush, a direct reader of `_tree.yaml` sees a STALE `retrieval_count`. `guard-731`
forbids retiring a node on `retrieval_count == 0` alone, so an unflushed node
that has genuinely been retrieved must never read as a fresh zero. `pending_deltas`
+ `apply_pending` are the read-merge half, and they are not optional: a spool
without a read-merge converts a cost fix into the `g-115-5859` clobber class.

O(1) HOT PATH BY CONSTRUCTION. `record_bump` is one lockless O_APPEND of a
sub-200-byte line — the same idiom `_utilization_store.record_increment` documents
and the same one `_gate_log.log` uses. POSIX guarantees atomicity for a single
small O_APPEND, and a torn line (possible only if the process dies mid-write) is
skipped by the lossy parser below. Losing one advisory increment to a crash is
immaterial; taking a lock on this path would reintroduce the contention the whole
change exists to remove.

Each record is written with a LEADING newline as well as a trailing one, and
that byte is load-bearing rather than cosmetic. A torn line ends without its
terminator, so the next append would otherwise land on the SAME line and take a
healthy record down with the damaged one — the blast radius of a crash would be
two increments, not one, and the sentence above would be false. The leading
newline terminates any torn predecessor; `_parse_lossy` skips the resulting
blank lines. Measured by
`tests/test_tree_retrieval_spool.py::test_pending_deltas_skips_a_torn_line_without_losing_the_rest`,
which fails without it.

NEVER RAISES on the write path. `record_bump` returns False instead, so the
caller can fall back to the legacy read-modify-write narrowed to the keys that
failed — never let the cheap path lose a counter (the `retrieve.py` L596-600
precedent).
"""

import json
import os
import sys
import time
from pathlib import Path

SPOOL_BASENAME = "tree-retrieval.spool.jsonl"
FLUSHING_BASENAME = "tree-retrieval.spool.flushing.jsonl"
STAMP_BASENAME = "tree-retrieval.spool.last-flush"
FLUSH_LOCK_BASENAME = "tree-retrieval.spool.flush.lock"

# Every basename this lane creates beside the index that the `*.lock` glob does
# not already cover. They sit in the world tree dir, which own-cloud SYNCS, so
# each must be in owncloud_sync._EXCLUDE_NAMES (machine-local: never pushed, never
# pulled -- guard-3018). Measured 2026-10-02 (, fresh process, with
# machine-local and shared controls): before those entries all three classified
# SHARED, so flipping the flag would have had every box push its spool and pull
# every other box's, and each box would then drain every other box's deltas into
# the index. test_tree_retrieval_spool.py asserts each name is excluded.
SYNC_EXCLUDED_NAMES = (SPOOL_BASENAME, FLUSHING_BASENAME, STAMP_BASENAME)

# The longest a box lets spooled deltas wait before `flush_into_index` folds them
# into the shared index: the bound on how stale an index reader can be and, at one
# whole-object PUT per box per interval, on the PUT cost. The readers that decide
# anything (the archival clock, the retire gate) work in days, so six hours is
# invisible to them; one hour would put the lane's PUT floor back near the
# per-retrieval cost it replaces.
DRAIN_INTERVAL_SECONDS = 6 * 3600

# Same opt-in shape as UTILIZATION_COUNTERS_SPOOLED: default OFF, so landing
# this module changes nothing until the flag is flipped deliberately.
SPOOLED_ENV = "TREE_RETRIEVAL_SPOOLED"

_TRUE = ("1", "true", "yes", "on")


def spooled_enabled():
    """True when the caller should spool instead of taking the legacy RMW."""
    return (os.environ.get(SPOOLED_ENV) or "").strip().lower() in _TRUE


def _sibling(tree_path, basename):
    if tree_path is None:
        return None
    try:
        return Path(tree_path).parent / basename
    except (TypeError, ValueError):
        return None


def spool_path(tree_path):
    """This box's live spool, beside the index it defers writes to."""
    return _sibling(tree_path, SPOOL_BASENAME)


def flushing_path(tree_path):
    """The rotated spool being drained — also the crash-residue marker."""
    return _sibling(tree_path, FLUSHING_BASENAME)


def stamp_path(tree_path):
    """Last successful drain, for staleness observability."""
    return _sibling(tree_path, STAMP_BASENAME)


def record_bump(tree_path, key, ts, delta=1):
    """Append one node-key delta. Returns True iff a line was written.

    Never raises: a False return is the caller's signal to fall back to the
    legacy in-index increment for THIS key, so a failure must not be swallowed
    into a silent no-op that also reports success.
    """
    p = spool_path(tree_path)
    if p is None or not key:
        return False
    try:
        # Leading newline terminates any torn predecessor (see module docstring)
        line = "\n" + json.dumps(
            {"key": key, "ts": ts, "delta": int(delta)},
            separators=(",", ":"), ensure_ascii=False) + "\n"
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(line)
        return True
    except Exception as exc:                      # noqa: BLE001 - see docstring
        print("[tree-retrieval-spool] append failed for %r: %s" % (key, exc),
              file=sys.stderr)
        return False


def _parse_lossy(p):
    """Sum deltas per node key from one spool file. Skips unreadable lines.

    Lossy BY DESIGN: a torn final line from a crashed writer is the only
    expected malformity, and dropping it is strictly better than refusing the
    whole drain. Returns {key: {"delta": int, "last_ts": str|None}}.
    """
    out = {}
    if p is None or not p.exists():
        return out
    try:
        with open(p, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:                 # noqa: BLE001 - torn line
                    continue
                k = rec.get("key")
                if not k:
                    continue
                slot = out.setdefault(k, {"delta": 0, "last_ts": None})
                try:
                    slot["delta"] += int(rec.get("delta", 1))
                except (TypeError, ValueError):
                    slot["delta"] += 1
                ts = rec.get("ts")
                if ts and (slot["last_ts"] is None or ts > slot["last_ts"]):
                    slot["last_ts"] = ts
    except Exception as exc:                      # noqa: BLE001
        print("[tree-retrieval-spool] read failed on %s: %s" % (p, exc),
              file=sys.stderr)
    return out


def _merge_into(dst, src):
    for k, slot in src.items():
        cur = dst.setdefault(k, {"delta": 0, "last_ts": None})
        cur["delta"] += slot.get("delta", 0)
        ts = slot.get("last_ts")
        if ts and (cur["last_ts"] is None or ts > cur["last_ts"]):
            cur["last_ts"] = ts
    return dst


def pending_deltas(tree_path):
    """Un-flushed deltas a reader must add to the on-disk index to be correct.

    Reads BOTH the live spool and any `.flushing` residue, because a drain that
    died between rotate and commit leaves real deltas in the latter. A reader
    that consulted only the live spool would under-report exactly the counters a
    crashed flush was carrying.
    """
    out = {}
    _merge_into(out, _parse_lossy(flushing_path(tree_path)))
    _merge_into(out, _parse_lossy(spool_path(tree_path)))
    return out


def apply_pending(data, deltas):
    """Fold `deltas` into an in-memory `_tree.yaml` dict. Returns keys applied.

    Mirrors `retrieve.py`'s `_bump_counters` exactly: `retrieval_count` is
    incremented and `last_retrieved` stamped from the spooled `ts`. A key whose
    node no longer exists (PRUNE / RETIRE / MERGE between the bump and the
    flush) is dropped silently — the retrieval was already served; the counter
    is incidental on a node that is gone.

    `data["last_updated"]` is deliberately NOT touched here: the legacy bump set
    it, but on this path the only structural writer is `write_tree`, which owns
    that field. Stamping it from a counter fold would make a pure-counter flush
    indistinguishable from a structural change to every downstream freshness
    reader.
    """
    if not deltas:
        return 0
    nodes = (data or {}).get("nodes") or {}
    applied = 0
    for k, slot in deltas.items():
        n = nodes.get(k)
        if not isinstance(n, dict):
            continue
        try:
            n["retrieval_count"] = int(n.get("retrieval_count", 0) or 0) + int(
                slot.get("delta", 0))
        except (TypeError, ValueError):
            continue
        # NEWER-WINS, never a bare overwrite. `last_retrieved` is a monotonic
        # clock: `coordination_merge._TREE_NEWER_FIELDS` lists it, and
        # `tree_archive.effective_relevance` takes max(last_retrieved,
        # last_relevant_at-or-last_updated) as the ARCHIVAL clock — so on the
        # node class this spool exists to serve (retrieved often, edited
        # rarely) `last_retrieved` IS the max, and moving it backward makes a
        # live node read as stale to the archival sweep. The index can legally
        # hold a NEWER stamp than the spool: a `record_bump` failure narrows
        # that key to the legacy in-index write (retrieve.py Step 4), which
        # stamps `today`, while an older spooled delta for the same key is
        # still pending. `_merge_into` above already maxes `last_ts` WITHIN
        # the spool; this is the same comparison at the index boundary, where
        # it was missing. guard-1703: an ordering key must match the merge
        # granularity it serves — never a bare last-write-wins.
        ts = slot.get("last_ts")
        if ts:
            cur = n.get("last_retrieved")
            n["last_retrieved"] = ts if (not cur or str(ts) > str(cur)) else cur
        applied += 1
    return applied


def rotate(tree_path):
    """Move the live spool aside so appends during a drain are not lost.

    Returns True when there is something to drain. Any residue already at the
    flushing path is preserved by appending the live spool onto it rather than
    replacing it — a previous drain that died after rotate must not be
    overwritten by this one.
    """
    live = spool_path(tree_path)
    flushing = flushing_path(tree_path)
    if live is None or flushing is None:
        return False
    try:
        if not live.exists():
            return flushing.exists()
        if flushing.exists():
            with open(flushing, "a", encoding="utf-8") as dst, \
                    open(live, "r", encoding="utf-8") as src:
                for line in src:
                    dst.write(line)
            live.unlink()
        else:
            live.replace(flushing)
        return True
    except Exception as exc:                      # noqa: BLE001
        print("[tree-retrieval-spool] rotate failed: %s" % exc, file=sys.stderr)
        return False


def take_for_flush(tree_path):
    """Rotate then parse. Returns the deltas a flusher should fold in."""
    if not rotate(tree_path):
        return {}
    return _parse_lossy(flushing_path(tree_path))


def commit_flush(tree_path, stamp_ts=None):
    """Retire the drained residue. Call ONLY after the fold has been WRITTEN.

    Ordering is the whole contract: the flushing file is the sole record of
    those deltas until the index write lands, so removing it first and crashing
    would lose them outright. Removing it after a successful write costs, at
    worst, a double-count if the crash lands between — and a double-counted
    advisory retrieval counter is strictly less harmful than a lost one, which
    is what `guard-731` reads to decide retirement.
    """
    flushing = flushing_path(tree_path)
    stamp = stamp_path(tree_path)
    try:
        if flushing is not None and flushing.exists():
            flushing.unlink()
        if stamp is not None and stamp_ts:
            stamp.write_text(str(stamp_ts) + "\n", encoding="utf-8")
        return True
    except Exception as exc:                      # noqa: BLE001
        print("[tree-retrieval-spool] commit failed: %s" % exc, file=sys.stderr)
        return False


def _has_work(tree_path):
    """(residue, live_nonempty) from two stats; reads no file content."""
    flushing = flushing_path(tree_path)
    live = spool_path(tree_path)
    residue = flushing is not None and flushing.exists()
    try:
        live_nonempty = live is not None and live.stat().st_size > 0
    except OSError:
        live_nonempty = False
    return residue, live_nonempty


def _interval_elapsed(tree_path, interval, now):
    """True when this box's last drain is at least `interval` old.

    The clock is the stamp file's mtime: every `commit_flush` rewrites it, so a
    drain by `write_tree` resets it too. No stamp means this box has never
    drained, and a stamp dated in the future is a clock fault; both drain rather
    than wait, because a spurious drain costs one PUT and a wedged one costs
    counters that never land.
    """
    stamp = stamp_path(tree_path)
    try:
        age = now - stamp.stat().st_mtime
    except (OSError, AttributeError):
        return True
    return age >= interval or age < 0


def flush_into_index(tree_path, interval=DRAIN_INTERVAL_SECONDS, now=None,
                     force=False):
    """Fold the spool into the index with ONE `locked_modify_yaml`. Never raises.

    Self-gating, so a caller invokes it unconditionally: an absent or empty spool
    is two stats. A non-empty spool waits `interval` since this box's last drain,
    except crash residue (`.flushing`), which is un-landed work and always drains.
    Returns {"status": s} with s one of empty / deferred / busy / flushed / error.

    The decision is made twice, the second time under the flush lock, because two
    retrievals can both see the interval elapsed and the loser must not drain
    again behind the winner. `take_for_flush` runs INSIDE the modifier, so it holds
    the same tree lock `write_tree` holds when it takes; `commit_flush` runs after
    the write has landed. In the sliver between the lock release and the commit a
    `write_tree` could take the same residue and double-count it: advisory
    counters double-count rather than lose, the same trade `commit_flush` makes.
    A write that raises leaves the batch in `.flushing`, readable through
    `pending_deltas` and re-drained by the next call.
    """
    now = time.time() if now is None else now
    try:
        residue, live_nonempty = _has_work(tree_path)
        if not residue and not live_nonempty:
            return {"status": "empty"}
        if not (force or residue or _interval_elapsed(tree_path, interval, now)):
            return {"status": "deferred"}
        lock_path = _sibling(tree_path, FLUSH_LOCK_BASENAME)
    except Exception as exc:                      # noqa: BLE001 - never block a caller
        return {"status": "error", "error": "%s: %s" % (type(exc).__name__, exc)}

    from storage_backend import LocalBackend
    lock = LocalBackend()
    try:
        lock.acquire_lock(lock_path, timeout=1, stale_seconds=120)
    except Exception:                             # noqa: BLE001 - someone else is draining
        return {"status": "busy"}
    try:
        residue, live_nonempty = _has_work(tree_path)
        if not residue and not live_nonempty:
            return {"status": "empty"}
        if not (force or residue or _interval_elapsed(tree_path, interval, now)):
            return {"status": "deferred"}

        from _fileops import locked_modify_yaml
        taken = {}

        def _fold(data):
            # Safe to re-run: locked_modify_yaml re-runs the modifier on an
            # own-cloud If-Match conflict, and `.flushing` persists until
            # `commit_flush`, so a second take re-reads the same batch (plus
            # anything spooled since) and folds it into the fresh read.
            taken["deltas"] = take_for_flush(tree_path)
            taken["applied"] = apply_pending(data, taken["deltas"])
            return data

        locked_modify_yaml(tree_path, _fold, skip_if_unchanged=True)
        stamp_ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))
        if not commit_flush(tree_path, stamp_ts):
            return {"status": "error",
                    "error": "commit failed; the batch stays in .flushing"}
        return {"status": "flushed", "keys": len(taken.get("deltas") or {}),
                "applied": taken.get("applied", 0)}
    except Exception as exc:                      # noqa: BLE001 - fail-open, batch retained
        print("[tree-retrieval-spool] drain failed (%s: %s); the batch stays in "
              "the spool for the next drain" % (type(exc).__name__, exc),
              file=sys.stderr)
        return {"status": "error", "error": "%s: %s" % (type(exc).__name__, exc)}
    finally:
        try:
            lock.release_lock(lock_path)
        except Exception:                         # noqa: BLE001
            pass
