#!/usr/bin/env python3
"""Tip-keyed CARRY dispositions for worker carrier refs (the hold ledger).

`worker-ref-consume.sh --check` measures every outstanding carrier tip against
HEAD. A reducer that reads one and decides CARRY (leave it outstanding on
purpose, for example a placement hold whose owner is waiting on an external
gate) had nowhere to write that decision down, so the very next `--check`
re-raised the tip as STRANDED and counted it toward the dependency-pull signal
again, on every run and on every box, until the tip finally moved.

This module is the missing record: one append-only line per decision, keyed on
(ref, tip SHA). The key is the point. A tip's content is immutable, so a
decision about it stays true exactly as long as the ref still points at that
SHA; the moment the Body pushes again the SHA no longer matches and the record
voids itself. Nothing has to remember to clear it, and a NEW tip can never be
masked by an old decision.

Every hold is also bounded in time (`expires_at`). A hold that never expires is
a mute switch on the detector it silences, so the reducer renews it each
occurrence it still means it, and a hold nobody renews lapses back into view.

Ledger: `$WORLD_DIR/worker-ref-carries.jsonl`, strictly append-only (a release
is a record, never a deletion), so it is registered with the line-union merge
handler in coordination_merge.py. The latest record per ref wins, ordered by
`ts` and then by file position, because a union merge interleaves lines.

    {"ts": ..., "disposition": "carry"|"release", "ref": "refs/workers/<agent>/<sid>",
     "tip_sha": "<full sha>", "owner_goal": "g-N-N", "reason": "...",
     "expires_at": ..., "recorded_by": "<agent>", "sid": "<session id>"}

Growth is one line per decision and renewal, so the ledger is deliberately NOT in
core/config/store-hygiene.yaml: a line cap or rotation there could drop a hold that is
still live, which would silently un-hold it.

Status values returned for a (ref, tip) pair:
    held      a live hold on exactly this tip
    lapsed    a hold on this tip whose expires_at has passed (or is unreadable)
    voided    a hold exists but on a different tip: the ref moved
    released  the latest record is an explicit release
    none      no record for the ref
    unreadable  the ledger exists but could not be read (CLI only)

Anything that is not `held` is treated by the caller as "not held", so every
failure direction is toward the detector reporting the tip.

CLI (used by worker-ref-consume.sh; stdout is one line, tab-separated):
    status  --ref R --tip SHA [--now ISO]
    record  --ref R --tip SHA --owner-goal G --reason TEXT [--ttl-h N]
    release --ref R --reason TEXT
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

LEDGER_BASENAME = "worker-ref-carries.jsonl"
LEDGER_ENV = "WORKER_REF_CARRY_LEDGER"  # test seam: production never sets it
DEFAULT_TTL_H = 72
MAX_TTL_H = 720
MAX_REASON = 500

_REF_RE = re.compile(r"^refs/workers/[^\s/]+/[^\s/]+$")
_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_GOAL_RE = re.compile(r"^g-\d+-\d+$")
_TS_FMT = "%Y-%m-%dT%H:%M:%S"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(dt: datetime) -> str:
    return dt.strftime(_TS_FMT)


def _parse_ts(value):
    """Naive-UTC datetime from an ISO string, or None when absent or unparseable."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def ledger_path():
    override = os.environ.get(LEDGER_ENV, "").strip()
    if override:
        return Path(override)
    try:
        from _paths import WORLD_DIR
    except Exception:
        return None
    return Path(WORLD_DIR) / LEDGER_BASENAME if WORLD_DIR else None


def read_records(path):
    """(records, unparseable_line_count). A missing file is the normal state
    before the first hold was ever recorded. Any other read failure raises
    OSError: an unreadable ledger must never read as an empty one."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return [], 0
    records, bad = [], 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(rec, dict):
            records.append(rec)
        else:
            bad += 1
    return records, bad


def latest_per_ref(records):
    best = {}
    for i, rec in enumerate(records):
        ref = rec.get("ref")
        if not isinstance(ref, str) or not ref:
            continue
        key = (str(rec.get("ts") or ""), i)
        cur = best.get(ref)
        if cur is None or key >= cur[0]:
            best[ref] = (key, rec)
    return {ref: rec for ref, (_key, rec) in best.items()}


def evaluate(records, ref, tip_sha, now):
    """Status dict for (ref, tip_sha) at `now`. See the module docstring."""
    rec = latest_per_ref(records).get(ref)
    if rec is None:
        return {"state": "none"}
    out = {
        "owner_goal": rec.get("owner_goal"),
        "reason": rec.get("reason"),
        "recorded_at": rec.get("ts"),
        "recorded_by": rec.get("recorded_by"),
        "expires_at": rec.get("expires_at"),
        "recorded_tip": rec.get("tip_sha"),
    }
    disposition = rec.get("disposition")
    if disposition == "release":
        return {**out, "state": "released"}
    if disposition != "carry":
        return {"state": "none"}
    if str(rec.get("tip_sha") or "").lower() != str(tip_sha or "").lower():
        return {**out, "state": "voided"}
    expires = _parse_ts(rec.get("expires_at"))
    if expires is None or expires <= now:
        return {**out, "state": "lapsed"}
    return {**out, "state": "held"}


def _scrub(value) -> str:
    text = " ".join(str(value).split()) if value not in (None, "") else ""
    return text or "-"


def status_line(info) -> str:
    return "\t".join([
        info["state"], _scrub(info.get("owner_goal")), _scrub(info.get("recorded_at")),
        _scrub(info.get("expires_at")), _scrub(info.get("recorded_by")),
        _scrub(info.get("recorded_tip")), _scrub(info.get("reason")),
    ])


class RecordError(ValueError):
    """A request the ledger refuses to write."""


def _clean_reason(reason) -> str:
    text = " ".join(str(reason or "").split())
    if not text:
        raise RecordError("a reason is required: a hold without one cannot be reviewed")
    if len(text) > MAX_REASON:
        raise RecordError(f"reason is {len(text)} chars (max {MAX_REASON}); shorten it, nothing was truncated")
    return text


def _append(path, rec):
    from _fileops import locked_append_jsonl
    locked_append_jsonl(str(path), rec)


def _provenance():
    return {"recorded_by": os.environ.get("MIND_AGENT") or "unknown",
            "sid": os.environ.get("MIND_SID") or ""}


def record_carry(path, ref, tip_sha, owner_goal, reason, ttl_h=DEFAULT_TTL_H, now=None):
    if not _REF_RE.match(ref or ""):
        raise RecordError(f"not a worker carrier ref: {ref!r}")
    if not _SHA_RE.match((tip_sha or "").lower()):
        raise RecordError(f"tip must be a full commit SHA, got {tip_sha!r}")
    if not _GOAL_RE.match(owner_goal or ""):
        raise RecordError(f"owner goal must look like g-NNN-NN, got {owner_goal!r}")
    if not 1 <= int(ttl_h) <= MAX_TTL_H:
        raise RecordError(f"ttl must be 1..{MAX_TTL_H} hours, got {ttl_h}")
    now = now or utcnow()
    rec = {"ts": _iso(now), "disposition": "carry", "ref": ref, "tip_sha": tip_sha.lower(),
           "owner_goal": owner_goal, "reason": _clean_reason(reason),
           "expires_at": _iso(now + timedelta(hours=int(ttl_h))), **_provenance()}
    _append(path, rec)
    return rec


def record_release(path, ref, reason, now=None):
    if not _REF_RE.match(ref or ""):
        raise RecordError(f"not a worker carrier ref: {ref!r}")
    now = now or utcnow()
    rec = {"ts": _iso(now), "disposition": "release", "ref": ref,
           "reason": _clean_reason(reason), **_provenance()}
    _append(path, rec)
    return rec


def _cmd_status(args) -> int:
    path = ledger_path()
    if path is None:
        print("unreadable\t-\t-\t-\t-\t-\tno world path resolved for the carry ledger")
        return 0
    now = _parse_ts(args.now) if args.now else utcnow()
    if now is None:
        print(f"bad --now {args.now!r}", file=sys.stderr)
        return 2
    try:
        records, bad = read_records(path)
    except OSError as exc:
        print(f"unreadable\t-\t-\t-\t-\t-\t{_scrub(exc)}")
        return 0
    if bad:
        print(f"worker_ref_carries: {bad} unparseable line(s) skipped in {path}", file=sys.stderr)
    print(status_line(evaluate(records, args.ref, args.tip, now)))
    return 0


def _cmd_write(args) -> int:
    path = ledger_path()
    if path is None:
        print("worker_ref_carries: no world path resolved; refusing to write", file=sys.stderr)
        return 1
    try:
        if args.cmd == "record":
            rec = record_carry(path, args.ref, args.tip, args.owner_goal, args.reason, args.ttl_h)
            print(f"CARRY recorded: {rec['ref']} tip={rec['tip_sha'][:12]} owner={rec['owner_goal']} "
                  f"expires={rec['expires_at']} ledger={path}")
        else:
            rec = record_release(path, args.ref, args.reason)
            print(f"CARRY released: {rec['ref']} ledger={path}")
    except RecordError as exc:
        print(f"worker_ref_carries: REFUSED: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # the append itself failed: nothing was recorded
        print(f"worker_ref_carries: write FAILED, nothing recorded: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("status")
    st.add_argument("--ref", required=True)
    st.add_argument("--tip", required=True)
    st.add_argument("--now")
    rc = sub.add_parser("record")
    rc.add_argument("--ref", required=True)
    rc.add_argument("--tip", required=True)
    rc.add_argument("--owner-goal", required=True)
    rc.add_argument("--reason", required=True)
    rc.add_argument("--ttl-h", type=int, default=DEFAULT_TTL_H)
    rl = sub.add_parser("release")
    rl.add_argument("--ref", required=True)
    rl.add_argument("--reason", required=True)
    args = ap.parse_args(argv)
    return _cmd_status(args) if args.cmd == "status" else _cmd_write(args)


if __name__ == "__main__":
    sys.exit(main())
