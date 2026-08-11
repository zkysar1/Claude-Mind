#!/usr/bin/env python3
"""cold-snapshot-tick.py — fleet-wide cadence tick for the cold snapshot
(g-115-5279; wired into iteration-close.sh productivity-check beside
agent-watchdog --tick / monitor-tick / embedding-index-freshness).

WHAT MOVED, AND WHAT DID NOT
----------------------------
This moves g-115-4317's TRIGGER, not its implementation. `cold-snapshot.sh`
and `cold_snapshot.py` are untouched: they still read local, tar, compress and
PUT to a fresh timestamped retention-immune key. Only the thing that decides
WHEN to run changed — from a weekly recurring goal (which burns a whole LLM
iteration to run one command) to this script-side tick.

The S3-side alternative — an Operator CopyObject job — was measured and
REJECTED by g-115-4318; do not re-propose it. Its two disqualifying findings,
neither of which a cadence change can fix: 26 of the 7991 archived files are
machine-local per `owncloud_sync._EXCLUDE_NAMES`/`_EXCLUDE_DIRS` and never
reach S3 at all (0.33% by count but 13.2% BY BYTES — the entire write-audit
lane), and server-side copy preserves objects uncompressed for ~5x the storage
of the 3.9x-compressed tarball this path already produces.

THE CADENCE STAMP IS SHARED, AND THAT IS THE WHOLE DESIGN PROBLEM
-----------------------------------------------------------------
A recurring GOAL is claimed once from the shared world queue, so it fires ~1x
per interval FLEET-WIDE. A naive per-box tick fires 1x per interval PER BOX —
~5x the snapshots and ~5x the storage on this fleet, which would hand back the
cost the move exists to save. Every sibling tick in that phase is deliberately
per-box (a per-box index, a per-box watchdog, a per-box probe state); this one
is the opposite, and reusing their per-agent WM-slot cadence pattern here would
be silently wrong.

So the stamp lives in the OBJECT STORE, at ONE fixed key beside the snapshots:

    <customer_prefix><env_id>/<prefix>/_last-run.json

Not in `world/`: a synced world file is a read-through cache with merge
semantics, and a cadence stamp with a stale-mirror race is the defect, not the
fix (`governed-store-write-classes.md` — a fence-only store with no reconciler
below the write). Not derived from the snapshot keys themselves: ListBucket is
DENIED to this principal, so the prefix cannot be enumerated. A single fixed
key can be read with GetObject, which is the same IAM action HeadObject already
uses successfully in `cold_snapshot.py`.

MEASURED, not assumed (bravo, cc-05, `uname -r` 6.8.0-136-generic, 2026-08-09,
per guard-2085 clause 3 — a store you can write is not a store you can read
back until you read it back): against the live bucket, HEAD of the marker key
returned 404 (key free), and a positive control on the SAME prefix returned
`PUT: ok` then `GET: ok, 23 bytes` with a LastModified. Both actions work for
the backend client, which is the identity this tick runs as.

Overwriting one fixed key makes prior stamps NONCURRENT, and noncurrent
versions expire at 14 days — which is correct here and is the exact opposite of
the rule governing the snapshots themselves. The stamp is not precious; only
its CURRENT version is ever read. The archives keep unique keys precisely
because they ARE precious.

CLAIM FIRST, THEN RUN — AND WHY THE STUCK-DETECTOR IS NOT OPTIONAL
-------------------------------------------------------------------
The marker is written BEFORE the snapshot starts, so the race window between
two boxes ticking at once is one GET->PUT round trip (~100ms) instead of the
whole multi-minute run. The residual race is real and is stated rather than
claimed away: two boxes ticking inside that window both fire, producing 2
snapshots in an interval instead of 1. That is bounded and cheap; 5 was not.
S3 conditional writes (`IfMatch` on the ETag) would close it properly and are
the measured-if-needed improvement — deliberately not built on speculation.

Claiming first has one cost, and it is the one that matters: a run that dies
after the claim leaves a fresh marker, so the next tick would see "not due" and
skip an entire interval SILENTLY. A backup that stops running without saying so
is the failure this whole lane exists to prevent. Hence `status: running` plus
`_STUCK_SECONDS`: a claim still marked running past that ceiling is treated as
a dead run — it files an Investigate and re-fires.

That Investigate is not an extra: g-115-4317's own verification required
"file an Investigate on any non-ok verdict — a backup that reports success
without landing is worse than none". Retiring the goal moves the obligation
here; dropping it would have quietly deleted a safety property.

MODES
-----
  --tick (default)  decide, claim, spawn `--run` DETACHED, exit immediately.
                    Never waits: the snapshot is minutes of walk+compress+upload
                    and must not become the loop's iteration time.
  --run             execute cold-snapshot.sh, write the verdict back into the
                    marker, file a deduped Investigate on any non-ok verdict.
  --dry-run         report the decision only; claims nothing, spawns nothing.

All paths fail-open: any error prints to stderr and returns 0, because this
runs inside productivity-check and must never abort loop continuation.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

PROJECT_ROOT = SCRIPT_DIR.parent.parent
RUN_LOG = SCRIPT_DIR.parent / "logs" / "cold-snapshot.log"

DEFAULT_INTERVAL_HOURS = 168.0   # weekly — inside the bucket's 14-day noncurrent net
DEFAULT_STUCK_HOURS = 2.0        # a claim still "running" past this is a dead run
DEFAULT_PREFIX = "cold-snapshots"

# Escalation target for the Investigate this lane files. RESOLVED, never
# literal (): `` names the UPSTREAM deployment's framework
# queue and does not exist downstream, so a hardcoded id files into nothing
# there — silently, since the add-goal call still returns. The fallback arm
# only runs when the resolver itself cannot be imported.
try:
    from _paths import AGENT_DIR, CORE_ROOT, WORLD_DIR
    from _escalation_target import resolve as _resolve_asp, source_flag as _asp_source
    ASP_ID, _ASP_VIA = _resolve_asp(CORE_ROOT, WORLD_DIR, AGENT_DIR)
    ASP_SOURCE = _asp_source(ASP_ID, WORLD_DIR, AGENT_DIR)
except Exception:
    AGENT_DIR = WORLD_DIR = None
    ASP_ID, _ASP_VIA, ASP_SOURCE = "asp-115", "fallback:import-failed", "world"

ORIGIN_SIGNAL = "investigate:cold-snapshot-tick"
DEDUP_HOURS = 48


def _hours_env(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name) or default)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _backend():
    """The own-cloud backend, or None when there is no object store.

    None is the correct answer for LocalBackend (tests, local-only
    deployments): there is no remote retention clock to protect against, which
    is the same call `cold_snapshot.py` makes with its
    `skipped-local-backend` verdict.
    """
    try:
        from storage_backend import get_backend
        b = get_backend()
        return b if hasattr(b, "s3") else None
    except Exception:
        return None


def _marker_key(backend, prefix: str) -> str:
    return f"{backend._customer_prefix()}{backend.env_id}/{prefix}/_last-run.json"


def _read_marker(backend, key: str):
    """(age_seconds, body_dict) for the marker, or (None, None) when absent.

    GetObject rather than HeadObject: it costs the same IAM action and one
    round trip, and it returns both LastModified and the body — and the body's
    `status` is what the stuck-detector reads.
    """
    try:
        from botocore.exceptions import ClientError
    except Exception:
        return None, None
    try:
        obj = backend.s3.get_object(Bucket=backend.bucket, Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return None, None
        raise
    lm = obj.get("LastModified")
    age = (_now_utc() - lm).total_seconds() if lm is not None else None
    try:
        body = json.loads(obj["Body"].read().decode("utf-8"))
    except Exception:
        body = {}          # unreadable body is not a reason to skip the cadence
    return age, (body if isinstance(body, dict) else {})


def _write_marker(backend, key: str, payload: dict) -> None:
    backend.s3.put_object(
        Bucket=backend.bucket, Key=key,
        Body=json.dumps(payload, indent=2).encode("utf-8"),
    )


def decide(age_seconds, body, interval_seconds: float, stuck_seconds: float) -> dict:
    """Pure cadence decision — the unit-testable core.

    Returns {"due": bool, "reason": str, "stuck": bool}. `stuck` is reported
    separately from `due` because a dead run needs BOTH a re-fire and an
    Investigate, and the caller acts on each independently.
    """
    if age_seconds is None:
        return {"due": True, "reason": "no-marker", "stuck": False}
    if age_seconds >= interval_seconds:
        return {"due": True, "reason": f"interval-elapsed:{age_seconds / 3600:.1f}h",
                "stuck": False}
    if (body or {}).get("status") == "running" and age_seconds >= stuck_seconds:
        return {"due": True, "reason": f"prior-run-stuck:{age_seconds / 3600:.1f}h",
                "stuck": True}
    return {"due": False, "reason": f"fresh:{age_seconds / 3600:.1f}h", "stuck": False}


def _recent_investigate_exists() -> bool:
    """Suppress a duplicate tick Investigate.

    Fails CLOSED (returns True) whenever the queues could not actually be READ,
    per guard-487: a swallowed error mapping to "no duplicate found" would
    re-enable exactly the unattended spam this gate exists to stop. A
    genuinely-recurring failure re-alerts after the cooldown, so erring toward
    suppression loses no durable signal.

    "Could not be read" DELIBERATELY INCLUDES an absent file, and that is the
    half a plain try/except misses. A missing path raises nothing — it just
    skips — so an exception-only net leaves the no-file case failing OPEN while
    the docstring says CLOSED. Under own-cloud the local tree is a read-through
    cache (guard-980), so "not present on this box" is an ordinary state rather
    than a filesystem fault: the miss is reachable, not exotic. Hence
    `read_any`, which distinguishes "read the queues and found nothing" from
    "never read a queue at all" — two states a bare `return False` renders
    identically. (Found by the g-115-5279 fresh-eyes pass on this file, with
    both queues pointed at nonexistent paths: the gate returned False.)
    """
    cutoff = _dt.datetime.now() - _dt.timedelta(hours=DEDUP_HOURS)
    if WORLD_DIR is None and AGENT_DIR is None:
        return True          # queues unresolvable — suppress rather than spam
    paths = [Path(p) / "aspirations.jsonl" for p in (WORLD_DIR, AGENT_DIR) if p]
    read_any = False
    for path in paths:
        try:
            if not path.exists():
                continue
            read_any = True
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        asp = json.loads(line)
                    except json.JSONDecodeError:
                        continue    # one bad line is not a gate-disabling error
                    for g in asp.get("goals", []):
                        if (g.get("origin_signal") or "") != ORIGIN_SIGNAL:
                            continue
                        if g.get("status") in ("pending", "in-progress"):
                            return True
                        created = (g.get("created_at") or g.get("created_date")
                                   or g.get("created"))
                        if not created:
                            return True
                        try:
                            if _dt.datetime.fromisoformat(str(created)) > cutoff:
                                return True
                        except (ValueError, TypeError):
                            continue
        except Exception:
            return True
    return False if read_any else True


def _file_investigate(reason: str, detail: str) -> dict:
    """File a deduped Investigate under  via aspirations-add-goal.sh."""
    if _recent_investigate_exists():
        return {"filed": False, "suppressed": "recent-duplicate"}
    filer = os.environ.get("MIND_AGENT") or "<see description>"
    payload = {
        "title": f"Investigate: cold-snapshot tick reported {reason} — the weekly "
                 f"retention-immune backup may not have landed",
        "description": (
            f"cold-snapshot-tick.py ({filer}) fired the cold snapshot and the run "
            f"did not report verdict=ok. Detail: {detail}\n\n"
            f"This lane replaced recurring goal g-115-4317, whose verification "
            f"required an Investigate on any non-ok verdict — a backup that "
            f"reports success without landing is worse than none.\n\n"
            f"Triage: (1) read core/logs/cold-snapshot.log for the run output; "
            f"(2) re-run `bash core/scripts/cold-snapshot.sh --output json` by "
            f"hand and read the verdict field; (3) the cadence marker is the S3 "
            f"object <env>/cold-snapshots/_last-run.json — its `status` and "
            f"`verdict` record what the last run believed; (4) a marker stuck at "
            f"status=running means the run process died after claiming, so look "
            f"for a crash rather than an upload failure. ListBucket is DENIED to "
            f"this principal, so verify by head/get on a known key, never by "
            f"listing the prefix."
        ),
        "priority": "MEDIUM",
        "participants": ["agent"],
        "category": "infrastructure",
        "origin_signal": ORIGIN_SIGNAL,
        "work_class": "framework",
        "intended_agent": "either",
        "tags": ["cold-snapshot", "backup-integrity", "cold-snapshot-tick"],
    }
    try:
        from _runtime_bash import bash_cmd     # Windows-safe bash resolution (guard-580)
        script_path = (SCRIPT_DIR / "aspirations-add-goal.sh").as_posix()
        result = subprocess.run(
            bash_cmd(script_path, "--source", ASP_SOURCE, ASP_ID),
            input=json.dumps(payload), capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:
        return {"filed": False, "error": str(exc)[:200]}
    if result.returncode != 0:
        return {"filed": False, "rc": result.returncode,
                "stderr": (result.stderr or "").strip()[:300]}
    return {"filed": True}


def do_tick(args) -> int:
    backend = _backend()
    if backend is None:
        return 0                      # LocalBackend / no store — nothing to protect
    key = _marker_key(backend, args.prefix)
    interval = _hours_env("COLD_SNAPSHOT_INTERVAL_HOURS", DEFAULT_INTERVAL_HOURS) * 3600
    stuck = _hours_env("COLD_SNAPSHOT_STUCK_HOURS", DEFAULT_STUCK_HOURS) * 3600

    age, body = _read_marker(backend, key)
    verdict = decide(age, body, interval, stuck)

    if args.dry_run:
        print(json.dumps({"op": "cold-snapshot-tick", "marker_key": key,
                          "dry_run": True, **verdict}))
        return 0
    if not verdict["due"]:
        return 0                      # quiet on the common case (every iteration)

    if verdict["stuck"]:
        _file_investigate(
            "a stuck claim",
            f"the previous claim was still marked status=running after "
            f"{(age or 0) / 3600:.1f}h, so that run died before recording a verdict",
        )

    # CLAIM before running — this write is what makes every other box's next
    # tick read "fresh" and stand down.
    _write_marker(backend, key, {
        "status": "running",
        "claimed_at": _now_utc().isoformat(timespec="seconds"),
        "claimed_by_agent": os.environ.get("MIND_AGENT") or "unknown",
        "claimed_by_env": backend.env_id,
        "reason": verdict["reason"],
    })

    RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_f = open(RUN_LOG, "ab")
    kwargs = {"stdout": log_f, "stderr": log_f, "cwd": str(PROJECT_ROOT)}
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — survives the parent bash
        # exiting (nohup/disown are flaky on Git Bash).
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(
        [sys.executable, str(SCRIPT_DIR / "cold-snapshot-tick.py"),
         "--run", "--prefix", args.prefix], **kwargs)
    print(json.dumps({"op": "cold-snapshot-tick", "spawned": True,
                      "marker_key": key, **verdict}))
    return 0


def do_run(args) -> int:
    """Execute the snapshot and record its verdict in the shared marker."""
    from _runtime_bash import bash_cmd
    script_path = (SCRIPT_DIR / "cold-snapshot.sh").as_posix()
    started = _now_utc().isoformat(timespec="seconds")
    result_json, verdict, detail = {}, "error", ""
    try:
        proc = subprocess.run(
            bash_cmd(script_path, "--output", "json"),
            capture_output=True, text=True, timeout=3600, cwd=str(PROJECT_ROOT),
        )
        try:
            result_json = json.loads((proc.stdout or "").strip() or "{}")
        except json.JSONDecodeError:
            result_json = {}
        verdict = result_json.get("verdict") or ("error" if proc.returncode else "unknown")
        detail = (proc.stderr or "").strip()[-500:] or f"rc={proc.returncode}"
    except Exception as exc:
        detail = str(exc)[:500]

    # `skipped-local-backend` is a correct no-op, not a failure. Everything else
    # that is not `ok` means the archive may not have landed.
    ok = verdict in ("ok", "skipped-local-backend")

    backend = _backend()
    if backend is not None:
        try:
            _write_marker(backend, _marker_key(backend, args.prefix), {
                "status": "ok" if ok else "failed",
                "claimed_at": started,
                "finished_at": _now_utc().isoformat(timespec="seconds"),
                "claimed_by_agent": os.environ.get("MIND_AGENT") or "unknown",
                "verdict": verdict,
                "archive_key": result_json.get("archive_key"),
                "receipt_key": result_json.get("receipt_key"),
                "file_count": result_json.get("files"),
                "archive_bytes": result_json.get("archive_bytes"),
            })
        except Exception as exc:
            print(f"[cold-snapshot-tick] marker write failed: {exc}", file=sys.stderr)

    if not ok:
        _file_investigate(f"verdict={verdict}", detail or "no detail captured")
    print(json.dumps({"op": "cold-snapshot-run", "verdict": verdict,
                      "archive_key": result_json.get("archive_key"),
                      "file_count": result_json.get("files")}))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tick", action="store_true",
                    help="decide + claim + spawn the run detached (default)")
    ap.add_argument("--run", action="store_true",
                    help="execute the snapshot and record its verdict (spawned by --tick)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report the cadence decision only; claim nothing, spawn nothing")
    ap.add_argument("--prefix", default=DEFAULT_PREFIX,
                    help=f"key prefix under the env root (default: {DEFAULT_PREFIX})")
    args = ap.parse_args()
    try:
        return do_run(args) if args.run else do_tick(args)
    except Exception as exc:      # fail-open — never abort productivity-check
        print(f"[cold-snapshot-tick] {exc}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
