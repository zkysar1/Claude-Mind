#!/usr/bin/env python3
"""events.jsonl store engine -- multi-agent task-decomposition records.

Lock-safe access layer for world/board/events.jsonl, the shared store that
records how a unit of work was decomposed among the MIND agents (g-306-19
Gap 10, child 2/3). Authoritative schema spec: world/conventions/events.md
(8 fields: event_id, owner, participants, decomposition, status,
completion_signals, created_at, recorded_by). Keep the two in sync.

OWN-CLOUD-SYNCED, MULTI-AGENT, APPEND-ONLY (guard-832 + rb-2112). Unlike the
machine-local anticipated-failures store (which uses locked_modify_jsonl and so
rewrites the whole file), events.jsonl is synced across agents' machines, so
EVERY write MUST be a single-record append -- a local bulk rewrite races the
cloud merge and clobbers a peer's concurrent append. State is therefore
EVENT-SOURCED: a status change is a NEW appended record reusing the same
event_id with the new status; readers fold by event_id and take the latest by
created_at. This keeps every write a pure append
(locked_append_jsonl = lock -> history -> append -> changelog -> unlock), never
a rewrite. board.py is the sibling append-only engine; this follows the same
_fileops pattern. locked_write_jsonl / locked_modify_jsonl are deliberately NOT
used here.

LOCAL CLI, not a daemon endpoint -- the store is low-frequency (task
decompositions, not a hot path) and the _fileops lock coordinates concurrent
CLI invocations with the running aspirations loop. If high-frequency cross-agent
sharing is ever required, promote to a daemon endpoint per the daemon-only
contract (.claude/rules/no-python-cli-fallback.md). The events.sh wrapper is a
pure-CLI wrapper (no rt_call) -- the sanctioned ~280-wrapper pattern.

Subcommands (one thin events.sh wrapper, subcommand-dispatched):
  add              -- stdin JSON record; validate + append a genesis event.
  update-status    -- argv event_id + --status; append a NEW record (event-sourced
                      transition) copying prior fields; exit 2 if event_id absent.
  read             -- --event-id (fold-by-latest record) OR --status (matching
                      events); print JSON.
  list-active      -- print events whose folded status is not completed/abandoned.
  check-completion -- argv event_id; print {event_id, status, completion_signals}.
  claim-role       -- argv event_id + --role (+ --agent) -> append a record adding
                      {role, agent} to participants; LOCK-SAFE under concurrent
                      claims (re-folds in-lock), idempotent on a repeated pair.
  rebind           -- (--agent) -> events where the agent currently holds a role,
                      for re-binding claimed roles after a session restart (read-only).

Exit codes: 0 success, 1 invalid input / validation error, 2 event_id not found.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _paths import WORLD_DIR  # type: ignore  # noqa: E402
import _fileops  # type: ignore  # noqa: E402

try:  # utf-8 on stdio (Windows cp1252 defense, mirrors board.py)
    from _stdio import reconfigure_stdio  # type: ignore  # noqa: E402
    reconfigure_stdio()
except Exception:  # pragma: no cover -- best-effort; _platform.sh also sets it
    pass

EVENTS_SUBPATH = ("board", "events.jsonl")
VALID_STATUSES = ("proposed", "claimed", "in-progress", "completed", "abandoned")
TERMINAL_STATUSES = ("completed", "abandoned")
# The only keys update_status() honours in its `overrides` argument. Anything
# else is discarded, and is reported rather than rejected (). Kept
# beside the record fields it is NOT: `status`, `created_at`, `event_id` and
# `recorded_by` are also record keys but come from parameters, so passing them
# via `overrides` is silently ineffective and must warn.
RECOGNIZED_OVERRIDES = ("owner", "participants", "decomposition",
                        "completion_signals")


def events_path(path=None) -> Path:
    """Resolve the events.jsonl path (CLI default = WORLD_DIR/board/events.jsonl)."""
    if path is not None:
        return Path(path)
    if WORLD_DIR is None:
        raise RuntimeError(
            "events_path: WORLD_DIR is unresolved (no MIND_WORLD env / conf "
            "entry). Bind an agent via /start, or pass an explicit path."
        )
    return Path(WORLD_DIR).joinpath(*EVENTS_SUBPATH)


def _now_iso() -> str:
    # Local system time per CLAUDE.md naming rules (never UTC).
    return dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _agent() -> str:
    return os.environ.get("MIND_AGENT", "system")


def validate_record(rec) -> list:
    """Return a list of human-readable validation errors ([] when valid)."""
    if not isinstance(rec, dict):
        return ["record must be a JSON object"]
    errs = []
    eid = rec.get("event_id")
    if not eid or not isinstance(eid, str):
        errs.append("event_id (non-empty string) is required")
    owner = rec.get("owner")
    if not owner or not isinstance(owner, str):
        errs.append("owner (non-empty string) is required")
    status = rec.get("status")
    if status not in VALID_STATUSES:
        errs.append("status must be one of %s" % (VALID_STATUSES,))
    parts = rec.get("participants", [])
    if not isinstance(parts, list):
        errs.append("participants must be a list of {role, agent} objects")
    else:
        for i, p in enumerate(parts):
            if not isinstance(p, dict) or "role" not in p or "agent" not in p:
                errs.append("participants[%d] must be an object with role+agent" % i)
    if not isinstance(rec.get("decomposition", []), list):
        errs.append("decomposition must be a list of objects")
    if not isinstance(rec.get("completion_signals", []), list):
        errs.append("completion_signals must be a list of strings")
    return errs


def _fold_by_latest(records) -> dict:
    """Fold records by event_id, keeping the latest by created_at.

    Ties on created_at resolve to later file order (records are appended in
    chronological order, so the last-written equal-timestamp record wins)."""
    folded = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        eid = rec.get("event_id")
        if not eid:
            continue
        cur = folded.get(eid)
        if cur is None or str(rec.get("created_at", "")) >= str(cur.get("created_at", "")):
            folded[eid] = rec
    return folded


def _has_role(rec, role, agent) -> bool:
    """True if rec's participants already contain the exact {role, agent} pair."""
    return any(isinstance(p, dict) and p.get("role") == role and p.get("agent") == agent
               for p in rec.get("participants", []))


def _read_all(path=None) -> list:
    p = events_path(path)
    if not p.exists():
        return []
    return _fileops.read_jsonl_with_recovery(p)


def add_event(rec: dict, path=None, allow_existing: bool = False) -> dict:
    """Validate + append a genesis event record (single-record append, guard-832).

    Rejects a duplicate event_id unless allow_existing=True -- a status change
    must go through update_status (event-sourced), not a second add."""
    errs = validate_record(rec)
    if errs:
        raise ValueError("; ".join(errs))
    rec.setdefault("participants", [])
    rec.setdefault("decomposition", [])
    rec.setdefault("completion_signals", [])
    rec.setdefault("created_at", _now_iso())
    rec.setdefault("recorded_by", _agent())
    if not allow_existing and rec["event_id"] in _fold_by_latest(_read_all(path)):
        raise ValueError(
            "event_id %s already exists; use update-status to transition "
            "(or pass --allow-existing to append another genesis record)"
            % rec["event_id"]
        )
    _fileops.locked_append_jsonl(events_path(path), rec)
    return rec


def update_status(event_id: str, new_status: str, *, recorded_by=None,
                  overrides=None, path=None) -> dict:
    """Append a NEW record (event-sourced transition) for event_id with new_status.

    Copies owner/participants/decomposition/completion_signals from the latest
    record, applies new_status + a fresh created_at + recorded_by, and appends.
    `overrides` (dict) may carry completion_signals/decomposition/participants/
    owner to change alongside the status. Raises KeyError if event_id absent,
    ValueError on an invalid status."""
    if new_status not in VALID_STATUSES:
        raise ValueError("status must be one of %s" % (VALID_STATUSES,))
    latest = _fold_by_latest(_read_all(path)).get(event_id)
    if latest is None:
        raise KeyError(event_id)
    overrides = overrides or {}
    new_rec = {
        "event_id": event_id,
        "owner": overrides.get("owner", latest.get("owner")),
        "participants": overrides.get("participants", latest.get("participants", [])),
        "decomposition": overrides.get("decomposition", latest.get("decomposition", [])),
        "status": new_status,
        "completion_signals": overrides.get(
            "completion_signals", latest.get("completion_signals", [])
        ),
        "created_at": _now_iso(),
        "recorded_by": recorded_by or _agent(),
    }
    # Dropped-key detection (, same class as ). The dict
    # above is a FIXED allowlist over `overrides`, which is caller-supplied — so
    # an override key outside {owner, participants, decomposition,
    # completion_signals} is discarded with no error: validate_record passes,
    # the append succeeds, and the caller's field is simply gone. Worse here than
    # in handoff.yaml, because this store is append-only event-sourced and the
    # write is never revisited. Report, do NOT reject: a caller may legitimately
    # pass provenance the event schema does not persist (rb-538 / guard-527).
    # stderr-only is deliberate and is the WEAK half (guard-772 — a stderr WARN
    # is invisible to a backgrounded caller). The structured half is unavailable
    # without changing the persisted event schema, which this goal's scope
    # explicitly forbids: the return value IS the appended record, so attaching
    # dropped_keys would either alter what is written or return something that
    # differs from it.
    #
    # The predicate is membership in RECOGNIZED_OVERRIDES, NOT `k not in
    # new_rec`. The reference fix uses the not-in-output form and is right to,
    # because there EVERY output key is payload-sourced so the two predicates
    # coincide. Here they do not: 4 of the 8 record keys (event_id, status,
    # created_at, recorded_by) come from parameters, never from `overrides`. So
    # `overrides={"status": "completed"}` IS silently ignored and the not-in-
    # output form would not report it — the check would have carried a hole in
    # exactly the class it exists to close. Copy the reference SHAPE, not its
    # predicate, whenever the output is not wholly caller-sourced.
    # str(k), not k: a non-string key makes `sorted()` raise on mixed types and
    # `", ".join()` raise on ints — turning a key that was previously IGNORED
    # into a TypeError that aborts the event append. A reporting path must never
    # be able to fail the write it reports on. (Inherited from the reference,
    # which had the same latent shape; fixed there in the same sweep, guard-3088.)
    dropped_keys = sorted(str(k) for k in overrides if k not in RECOGNIZED_OVERRIDES)
    if dropped_keys:
        print(
            "WARN: events.update_status ignored %d unrecognized override key(s): "
            "%s. Recognized keys are owner, participants, decomposition, "
            "completion_signals; these were NOT written to the event record for "
            "%s." % (len(dropped_keys), ", ".join(dropped_keys), event_id),
            file=sys.stderr,
        )
    errs = validate_record(new_rec)
    if errs:
        raise ValueError("; ".join(errs))
    _fileops.locked_append_jsonl(events_path(path), new_rec)
    return new_rec


# ──  Gap 10 child 3/3 (): role-claim + re-bind-on-restart ──
# A Mind agent claims a ROLE within an event (appended to participants), and
# re-attaches to its previously-claimed roles after a session restart. Both ride
# the same event-sourced append-only model as update_status; the claim WRITE is
# concurrency-hardened (see claim_role) so two agents claiming different roles on
# one event do not clobber each other.

def claim_role(event_id: str, role: str, agent: str, *, recorded_by=None,
               path=None) -> dict:
    """Append an event-sourced record adding {role, agent} to event_id's
    participants -- a Mind agent claiming a role in the task decomposition.

    LOCK-SAFE UNDER CONCURRENT CLAIMS: the participants list is re-folded INSIDE
    the file lock. locked_append_jsonl_with_allocator runs the build callback
    under the same lock that guards the append, reading the current records
    fresh, so two agents claiming DIFFERENT roles on the same event both land --
    neither full-snapshot append clobbers the other. This is the load-bearing
    divergence from update_status, whose fold happens BEFORE the lock (fine for a
    single-writer status transition, a lost-update hazard for independent
    participant additions).

    Idempotent: re-claiming a (role, agent) pair already present appends NOTHING
    and returns the current record unchanged. One agent MAY hold multiple roles
    (dedup key is the (role, agent) pair, not the agent alone). Raises KeyError
    if event_id is absent, ValueError on empty role/agent."""
    if not role or not isinstance(role, str):
        raise ValueError("role (non-empty string) is required")
    if not agent or not isinstance(agent, str):
        raise ValueError("agent (non-empty string) is required")

    # Cheap idempotency short-circuit (outside the lock): if this exact
    # (role, agent) pair is already held, do not append. The only race left is
    # two IDENTICAL concurrent claims each appending one redundant record
    # (harmless -- fold-by-latest collapses them); the dangerous DIFFERENT-claim
    # lost-update race is closed in-lock below.
    latest = _fold_by_latest(_read_all(path)).get(event_id)
    if latest is None:
        raise KeyError(event_id)
    if _has_role(latest, role, agent):
        return latest

    def _build(items):
        folded = _fold_by_latest(items).get(event_id)
        if folded is None:                       # unreachable for an append-only store
            raise KeyError(event_id)
        participants = [dict(p) for p in folded.get("participants", [])
                        if isinstance(p, dict)]
        if not _has_role(folded, role, agent):   # re-check under lock (concurrent identical claim)
            participants.append({"role": role, "agent": agent})
        new_rec = {
            "event_id": event_id,
            "owner": folded.get("owner"),
            "participants": participants,
            "decomposition": folded.get("decomposition", []),
            "status": folded.get("status"),
            "completion_signals": folded.get("completion_signals", []),
            "created_at": _now_iso(),
            "recorded_by": recorded_by or _agent(),
        }
        errs = validate_record(new_rec)
        if errs:
            raise ValueError("; ".join(errs))
        return new_rec

    return _fileops.locked_append_jsonl_with_allocator(events_path(path), _build)


def rebind(agent: str, *, include_terminal: bool = False, path=None) -> list:
    """Return the events where `agent` currently holds a role, for re-binding
    after a session restart. READ-ONLY: claims are durable in the store, so
    re-binding is a lookup ("which roles am I still on the hook for?"), not a
    re-append. Each entry: {event_id, roles (sorted), status, owner}. Terminal
    events (completed/abandoned) are excluded unless include_terminal."""
    out = []
    for rec in _fold_by_latest(_read_all(path)).values():
        if not include_terminal and rec.get("status") in TERMINAL_STATUSES:
            continue
        roles = sorted({p.get("role") for p in rec.get("participants", [])
                        if isinstance(p, dict) and p.get("agent") == agent and p.get("role")})
        if roles:
            out.append({"event_id": rec.get("event_id"), "roles": roles,
                        "status": rec.get("status"), "owner": rec.get("owner")})
    out.sort(key=lambda e: e.get("event_id") or "")
    return out


def read_event(event_id: str, path=None):
    """Return the current (folded-by-latest) record for event_id, or None."""
    return _fold_by_latest(_read_all(path)).get(event_id)


def read_by_status(status: str, path=None) -> list:
    """Return all current records whose folded status == status."""
    return [r for r in _fold_by_latest(_read_all(path)).values()
            if r.get("status") == status]


def list_active(path=None) -> list:
    """Return all current records whose folded status is not terminal."""
    return [r for r in _fold_by_latest(_read_all(path)).values()
            if r.get("status") not in TERMINAL_STATUSES]


def check_completion(event_id: str, path=None):
    """Return {event_id, status, completion_signals} for the latest record, or None."""
    rec = read_event(event_id, path)
    if rec is None:
        return None
    return {
        "event_id": event_id,
        "status": rec.get("status"),
        "completion_signals": rec.get("completion_signals", []),
    }


def _read_stdin_json():
    raw = sys.stdin.read().strip()
    if not raw:
        raise ValueError("empty stdin: expected a JSON object")
    return json.loads(raw)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="events")
    sub = parser.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("add", help="stdin JSON event record -> validate + append")
    pa.add_argument("--allow-existing", action="store_true",
                    help="append even if event_id already exists")

    pu = sub.add_parser("update-status",
                        help="argv event_id + --status -> append event-sourced transition")
    pu.add_argument("event_id")
    pu.add_argument("--status", required=True)
    pu.add_argument("--recorded-by")
    pu.add_argument("--overrides-json",
                    help="optional JSON object of field overrides "
                         "(completion_signals, decomposition, owner, participants)")

    prd = sub.add_parser("read", help="--event-id OR --status -> print JSON")
    prd.add_argument("--event-id")
    prd.add_argument("--status")

    sub.add_parser("list-active", help="print events whose status is not terminal")

    pc = sub.add_parser("check-completion", help="argv event_id -> completion_signals")
    pc.add_argument("event_id")

    pcr = sub.add_parser("claim-role",
                         help="argv event_id + --role (+ --agent) -> append a role claim "
                              "(lock-safe; idempotent on a repeated pair)")
    pcr.add_argument("event_id")
    pcr.add_argument("--role", required=True)
    pcr.add_argument("--agent", help="claiming agent (default: $MIND_AGENT)")
    pcr.add_argument("--recorded-by")

    prb = sub.add_parser("rebind",
                         help="(--agent) -> events where the agent holds a role "
                              "(re-bind on restart; read-only)")
    prb.add_argument("--agent", help="agent to re-bind (default: $MIND_AGENT)")
    prb.add_argument("--include-terminal", action="store_true",
                     help="include completed/abandoned events")

    args = parser.parse_args(argv)

    try:
        if args.cmd == "add":
            print(json.dumps(add_event(_read_stdin_json(),
                                       allow_existing=args.allow_existing)))
            return 0
        if args.cmd == "update-status":
            overrides = json.loads(args.overrides_json) if args.overrides_json else None
            if overrides is not None and not isinstance(overrides, dict):
                raise ValueError("--overrides-json must be a JSON object")
            try:
                rec = update_status(args.event_id, args.status,
                                    recorded_by=args.recorded_by, overrides=overrides)
            except KeyError:
                print(json.dumps({"error": "not_found", "event_id": args.event_id}),
                      file=sys.stderr)
                return 2
            print(json.dumps(rec))
            return 0
        if args.cmd == "read":
            if args.event_id:
                print(json.dumps(read_event(args.event_id)))  # 'null' when absent
            elif args.status:
                print(json.dumps(read_by_status(args.status)))
            else:
                raise ValueError("read requires --event-id or --status")
            return 0
        if args.cmd == "list-active":
            print(json.dumps(list_active()))
            return 0
        if args.cmd == "check-completion":
            res = check_completion(args.event_id)
            if res is None:
                print(json.dumps({"error": "not_found", "event_id": args.event_id}),
                      file=sys.stderr)
                return 2
            print(json.dumps(res))
            return 0
        if args.cmd == "claim-role":
            try:
                rec = claim_role(args.event_id, args.role, args.agent or _agent(),
                                 recorded_by=args.recorded_by)
            except KeyError:
                print(json.dumps({"error": "not_found", "event_id": args.event_id}),
                      file=sys.stderr)
                return 2
            print(json.dumps(rec))
            return 0
        if args.cmd == "rebind":
            print(json.dumps(rebind(args.agent or _agent(),
                                    include_terminal=args.include_terminal)))
            return 0
    except (ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": "invalid_input", "detail": str(exc)}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
