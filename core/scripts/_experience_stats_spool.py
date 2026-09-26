"""Counter spool for the per-agent EXPERIENCE store ().

WHAT IT REPLACES. Two writers bumped `retrieval_stats` counters on
`agents/<agent>/experience.jsonl` with a whole-store read-modify-write PER BUMP:
  * retrieve.py `load_experiences` -> `_locked_bump_jsonl(kind=None)`, one
    fenced whole-object PUT per retrieval call that returned experiences;
  * utilization-feedback.py `_increment_experience_stat`, one daemon GET plus one
    POST /v1/experience/update-field PER ITEM PER COUNTER (the 1-second bursts).
Measured 2026-09-25 (g-358-216's description): zeta's 3.27 MB store took 630
versions in 24h, 569 of them same-size in-place counter edits against 20 real
appends; about 1,581 whole-object PUTs and ~4.0 GiB/day across five agents.

WHY THIS FOLDS INTO THE CONTENT STORE AND NOT INTO A SIDECAR. The reasoning-bank /
guardrails lane (`_utilization_store`, g-358-05) moved counters into a small
`<kind>-utilization.jsonl` sidecar because those stores are 9-20 MB, so batching
alone left every write huge. It paid for that with a READER cutover: every
consumer had to learn `utilization_of`, gated fleet-wide by
store-cutover-check.sh. Here the store is ~3 MB and the lever is PUT COUNT
(the goal's own mutation-model finding), so the spool is drained back into the
SAME records the readers already read, at most once per `min_interval_seconds`.
No record shape changes, so no reader changes and no cutover gate. The one
behavioural change is latency: a counter reads up to one interval stale. These
are advisory statistics with no read-after-write consumer (retrieval sorts on
them; the archive sweep keys on days-old `last_retrieved`).

SPOOL LOCATION AND SYNC. The spool sits beside the store it drains into, as the
rb/guardrail spools do in world/. It MUST stay machine-local: a synced counter
spool would have every box drain every other box's deltas and inflate the
counters silently (owncloud_sync.py's utilization-spool comment). The names in
`SYNC_EXCLUDED_NAMES` are listed literally in `owncloud_sync._EXCLUDE_NAMES`; a
test asserts the two agree. The flush lock rides the `*.lock` glob.

A BOX THAT DOES NOT HOLD THE AGENT'S RUNNER CLAIM cannot write the agent dir
(NoClaimError, g-115-8028). Before this spool, both writers lost the increment
at write time there (g-115-8750). The flush keeps that parity: it DROPS an
un-landable batch loudly and stamps the interval, so a worker box neither grows
its spool without bound nor retries a doomed RMW on every call.
"""

import datetime as _dt
import importlib.util as _ilu
import json as _json
import os as _os
import sys as _sys
import time as _time
from pathlib import Path as _Path

SPOOL_NAME = "experience-stats.spool.jsonl"
FLUSHING_NAME = "experience-stats.spool.flushing.jsonl"
STAMP_NAME = "experience-stats.spool.last-flush"
FLUSH_LOCK_NAME = "experience-stats.spool.flush.lock"
SYNC_EXCLUDED_NAMES = (SPOOL_NAME, FLUSHING_NAME, STAMP_NAME)

DEFAULT_MIN_INTERVAL_SECONDS = 3600
DEFAULT_BURST_RECORDS = 500


class _StoreUnavailable(Exception):
    """The live store could not be seen, so the batch must be RETAINED.

    guard-6922: an unreadable store and a store without the record look the
    same from a failed read. Treating the first as the second would drop every
    pending delta, so an empty or absent store aborts before anything is
    written and the batch waits for the next flush."""


class _NothingToWrite(Exception):
    """No spooled id matched a live record; skip the whole-store write."""


_UFLUSH = None


def _uflush():
    """utilization-flush.py, loaded by path (hyphenated file name).

    Its `aggregate`, `latest_retrieval_ts` and `_parse_lossy` are reused rather
    than re-typed, so the two drains cannot disagree about what a delta line
    means."""
    global _UFLUSH
    if _UFLUSH is None:
        here = _Path(__file__).resolve().parent
        spec = _ilu.spec_from_file_location(
            "_uflush_for_experience", here / "utilization-flush.py")
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _UFLUSH = mod
    return _UFLUSH


def _no_claim_types():
    try:
        from storage_backend import get_backend
        return getattr(get_backend(), "no_claim_error", ()) or ()
    except Exception:
        return ()


def record(exp_path, rec_id, counter, delta=1):
    """Append one counter delta to the spool beside `exp_path`. Never raises.

    Returns True when a line was written. False tells the caller to fall back
    to its legacy in-record write, so a failure here never loses a counter.
    O(1): one lockless O_APPEND of a short line, the idiom
    `_utilization_store.record_increment` uses and documents.
    """
    try:
        if not exp_path or not rec_id or not counter:
            return False
        line = _json.dumps({
            "id": rec_id,
            "counter": counter,
            "delta": int(delta),
            "ts": _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        }, ensure_ascii=True)
        with open(_Path(exp_path).parent / SPOOL_NAME, "a",
                  encoding="utf-8") as f:
            f.write(line + "\n")
        return True
    except Exception:
        return False


def apply_to_record(rec, counters, last_ts=None):
    """Add one record's aggregated deltas to its `retrieval_stats`. Pure.

    Returns True when the record changed. The two writers' existing contracts
    are both kept:
      * a record with no `retrieval_stats` block gets one ONLY when a
        `retrieval_count` delta is present, as `_locked_bump_jsonl`'s setdefault
        did; a feedback-only delta on such a record is skipped, because
        manufacturing a block would invent a denominator no retrieval produced
        (utilization-feedback's `_increment_experience_stat` rule);
      * `utility_ratio` is recomputed as times_useful / max(retrieval_count, 1),
        the derived-field rule experience_write.update_field applies.
    """
    stats = rec.get("retrieval_stats")
    if not isinstance(stats, dict):
        if "retrieval_count" not in counters:
            return False
        stats = {}
        rec["retrieval_stats"] = stats
    for counter, delta in counters.items():
        current = stats.get(counter, 0)
        if not isinstance(current, int) or isinstance(current, bool):
            current = 0
        stats[counter] = current + delta
    if last_ts:
        day = last_ts[:10]
        prior = stats.get("last_retrieved")
        if not isinstance(prior, str) or day > prior:
            stats["last_retrieved"] = day
    rc = stats.get("retrieval_count")
    tu = stats.get("times_useful")
    if (isinstance(rc, int) and not isinstance(rc, bool)
            and isinstance(tu, int) and not isinstance(tu, bool)):
        stats["utility_ratio"] = round(tu / max(rc, 1), 4)
    return True


def _drain(exp_path, flushing):
    """Apply `flushing`'s deltas to the live store in ONE locked RMW.

    Returns (records_changed, increments, missing_ids). Raises
    _StoreUnavailable (batch retained) or the backend's no-claim error.
    """
    uf = _uflush()
    deltas, torn = uf._parse_lossy(flushing)
    if torn:
        print("[experience-stats-spool] WARN: skipped %d torn line(s) in %s "
              "(interrupted appends; one advisory increment each)"
              % (torn, flushing.name), file=_sys.stderr)
    agg = uf.aggregate(deltas)
    if not agg:
        flushing.unlink(missing_ok=True)
        return 0, 0, []
    last_ts = uf.latest_retrieval_ts(deltas)

    from storage_backend import get_backend
    get_backend().ensure_local(exp_path)
    if not exp_path.exists():
        raise _StoreUnavailable("%s is not readable on this box" % exp_path)

    result = {"records": 0, "missing": []}

    def _modifier(items):
        if not items:
            raise _StoreUnavailable("%s read back empty" % exp_path)
        by_id = {}
        for rec in items:
            if isinstance(rec, dict) and rec.get("id"):
                by_id[rec["id"]] = rec
        result["missing"] = sorted(i for i in agg if i not in by_id)
        changed = 0
        for rec_id, counters in agg.items():
            rec = by_id.get(rec_id)
            if rec is not None and apply_to_record(rec, counters,
                                                   last_ts.get(rec_id)):
                changed += 1
        if not changed:
            raise _NothingToWrite()
        result["records"] = changed
        return items

    from _fileops import locked_modify_jsonl
    try:
        locked_modify_jsonl(exp_path, _modifier)
    except _NothingToWrite:
        pass
    flushing.unlink(missing_ok=True)
    increments = sum(sum(c.values()) for c in agg.values())
    return result["records"], increments, result["missing"]


def flush(exp_path, min_interval_seconds=DEFAULT_MIN_INTERVAL_SECONDS,
          burst_records=DEFAULT_BURST_RECORDS, force=False):
    """Rotate and drain the spool beside `exp_path`. Never raises.

    Gated like utilization-flush.flush_kind: an empty spool is a two-stat no-op;
    a small spool waits `min_interval_seconds` since the last flush unless it
    holds `burst_records` lines or crash residue; a busy flush lock defers.
    Returns a dict whose `status` is one of empty / deferred / busy / flushed /
    no_claim / retained / error.
    """
    try:
        exp_path = _Path(exp_path)
        base = exp_path.parent
        spool = base / SPOOL_NAME
        flushing = base / FLUSHING_NAME
        stamp = base / STAMP_NAME
        residue = flushing.exists()
        spool_lines = 0
        if spool.exists():
            with open(spool, "rb") as f:
                spool_lines = sum(1 for _ in f)
        if not residue and spool_lines == 0:
            return {"status": "empty"}
        if not force and not residue and spool_lines < burst_records:
            try:
                last = float(stamp.read_text().strip())
            except (OSError, ValueError):
                last = 0.0
            if _time.time() - last < min_interval_seconds:
                return {"status": "deferred", "pending_lines": spool_lines}
    except Exception as e:  # noqa: BLE001 — a counter must never fail its caller
        return {"status": "error", "error": "%s: %s" % (type(e).__name__, e)}

    from storage_backend import LocalBackend
    lock = LocalBackend()
    lock_path = base / FLUSH_LOCK_NAME
    try:
        lock.acquire_lock(lock_path, timeout=1, stale_seconds=120)
    except Exception:
        return {"status": "busy"}
    out = {"status": "flushed", "records": 0, "increments": 0, "missing": []}
    try:
        batches = []
        if flushing.exists():
            batches.append(_drain(exp_path, flushing))
        if spool.exists() and _os.path.getsize(spool) > 0:
            _os.replace(spool, flushing)
            batches.append(_drain(exp_path, flushing))
        for records, increments, missing in batches:
            out["records"] += records
            out["increments"] += increments
            out["missing"].extend(missing)
        _write_stamp(stamp)
        if out["missing"]:
            print("[experience-stats-spool] WARN: %d spooled id(s) are not in "
                  "the live store (archived or removed since retrieval); their "
                  "deltas were dropped: %s"
                  % (len(out["missing"]), ", ".join(out["missing"][:10])),
                  file=_sys.stderr)
    except _no_claim_types() as e:
        dropped = _drop_unlandable(flushing, spool)
        _write_stamp(stamp)
        print("[experience-stats-spool] WARN: this box does not hold the "
              "runner claim for %s, so %d spooled increment(s) were dropped "
              "(the pre-spool writers lost them at write time too, g-115-8750): "
              "%s" % (exp_path, dropped, str(e)[:200]), file=_sys.stderr)
        out = {"status": "no_claim", "dropped": dropped}
    except _StoreUnavailable as e:
        print("[experience-stats-spool] WARN: %s; spool retained for the next "
              "flush" % e, file=_sys.stderr)
        out = {"status": "retained", "reason": str(e)}
    except Exception as e:  # noqa: BLE001 — fail-open, batch retained
        print("[experience-stats-spool] WARN: flush failed (%s: %s); spool "
              "retained for the next flush" % (type(e).__name__, e),
              file=_sys.stderr)
        out = {"status": "error", "error": "%s: %s" % (type(e).__name__, e)}
    finally:
        try:
            lock.release_lock(lock_path)
        except Exception:
            pass
    return out


def _drop_unlandable(flushing, spool):
    """Delete the rotated batch plus whatever spooled since; return the count."""
    dropped = 0
    for path in (flushing, spool):
        try:
            with open(path, "rb") as f:
                dropped += sum(1 for _ in f)
            path.unlink()
        except OSError:
            pass
    return dropped


def _write_stamp(stamp):
    try:
        stamp.write_text(str(_time.time()))
    except OSError:
        pass
