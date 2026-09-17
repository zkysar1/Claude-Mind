"""Retry a carrier repair whose PUSH was REFUSED (, the G4 leg).

WHY A NEW SIGNAL AND NOT A RE-SCAN. `_reconcile_orphan_carrier` and
`orphan_carrier_repair.repair_one` both write `body_state` into the local
carrier and then deliver it. When delivery is refused they return the verdict
`repaired-push-failed` -- local and authoritative now DISAGREE, and nothing
retries. Both call sites pre-filter on `body_state == 'active'`, and the local
write is exactly what destroys that value, so the obvious retry predicate is
the one that structurally cannot work: by the time a retry would run the
carrier looks correct locally and diverges only REMOTELY.
`body-manifest._reconcile_orphan_carrier`'s own docstring states the one-shot
is DESIGNED and that retry is out of that module's scope. This module is that
scope.

guard-5708 is the general form and it prescribes the fix: name the field the
predicate reads, ask what writes it on the FAILURE path, and when it is the
same writer, require a SECOND, INDEPENDENTLY-WRITTEN signal. The breadcrumb
here is written ONLY on the refusal path, so it is that second signal.

THREE CONSTRAINTS FROM THE PRE-APPLY CONSULT, each of which changed the design:

  guard-3849 -- BEFORE ANY DELIVERY WHOSE TRANSPORT OVERWRITES, READ THE
  DESTINATION AND DIFF IT. A retry is written against a destination nobody
  re-reads at execution time, and the destination is the one input that moves
  on its own. A refused push can be followed by the Body coming back (a fresh
  carrier, new `ts`) or by another box delivering the same repair. Blindly
  re-pushing the stored local bytes would overwrite newer authoritative state
  with a stale reading -- the exact class that guardrail was measured on. So
  `retry_one` reads the authoritative carrier with `force_fresh=True` FIRST and
  REFUSES on any divergence it did not itself create.

  guard-2104 -- a payload DERIVED FROM THIS EVENT is LOSS-BEARING, so an
  unconditional single-slot write silently cancels an unconsumed obligation
  still sitting there. The breadcrumb store is therefore a MAP keyed by sid and
  every write MERGES by that identity; it is never overwritten wholesale.

  guard-586 -- every new persistent sentinel MUST land with its own expiry in
  the same change, or it grows forever. `prune()` is that policy, it runs
  inside `retry_pending` on every pass, and its TTL is explicit
  (DEFAULT_TTL_DAYS). No sweep entry is needed elsewhere: the file is
  self-rotating and bounded by the number of sids this agent has ever failed a
  push for.

`ts` IS NEVER RESTAMPED, here or anywhere on this path (guard-6558). The retry
delivers the bytes already on disk; it composes no new document. That is also
why `ts` is usable as the divergence key below: the repair preserves it, so a
DIFFERENT `ts` at either end means some other writer moved the file, never that
this repair did.

BOUND AGENT ONLY, matching `orphan_carrier_repair`'s gate 1: rows are recorded
and retried only for the agent whose state dir holds the breadcrumb, so this
module never declares anything about a peer.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from _dt import parse_naive_iso
from _fileops import durable_write_text
from _paths import agent_state_dir

# One file per agent, in the agent-wide state dir (survives session dirs, which
# is the whole point -- the orphan carrier outlives its session).
BREADCRUMB_NAME = ".carrier-push-failed.json"

# guard-586's explicit expiry. A week is long enough that a box offline over a
# weekend still retries, and short enough that a carrier nobody can deliver
# stops being carried. An expired entry is DROPPED, never retried: the local
# file already agrees with the manifest, so the residue is a delivery gap, not
# a correctness gap.
DEFAULT_TTL_DAYS = 7.0

REFUSAL_VERDICT = "repaired-push-failed"


def breadcrumb_path(agent: str, state_dir: Path | None = None) -> Path:
    """Where this agent's breadcrumb lives.

    `state_dir` exists so a test can point the store at a tmp tree without
    reaching into `_paths`; production never passes it. It is deliberately a
    DIRECTORY override rather than a project-root one -- `agent_state_dir`
    takes no root argument, and threading a second resolution path through
    this module would put two answers to "where is the state dir" in the tree.
    """
    return (state_dir or agent_state_dir(agent)) / BREADCRUMB_NAME


def _now_iso() -> str:
    return dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def load(path: Path) -> dict:
    """Read the breadcrumb map. Fail-open to empty -- never raises.

    An unreadable breadcrumb must degrade to "nothing pending", not to a crash
    on the close path that calls this (guard-373 posture, matching every other
    writer on this lane).
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def save(path: Path, data: dict) -> bool:
    """Durable write (fsync before rename, guard-1179). False on any failure."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        durable_write_text(tmp, json.dumps(data, indent=1, sort_keys=True) + "\n")
        tmp.replace(path)
        return True
    except (OSError, ValueError, TypeError):
        return False


def record_push_failure(agent: str, sid: str, *, ts, body_state: str,
                        error: str | None = None,
                        state_dir: Path | None = None,
                        now: str | None = None) -> bool:
    """Persist the breadcrumb for one refused push. MERGES by sid (guard-2104).

    `ts` is the carrier's OWN timestamp as read back off disk after the repair
    -- it is the divergence key the retry compares against, so it must be the
    value that was pushed, not a fresh stamp.

    Re-recording an sid that is already pending preserves `first_failed_at` and
    bumps `attempts`, so the record shows how long delivery has been failing
    rather than resetting on every pass.
    """
    if not agent or not sid:
        return False
    path = breadcrumb_path(agent, state_dir)
    data = load(path)
    stamp = now or _now_iso()
    prior = data.get(sid) if isinstance(data.get(sid), dict) else {}
    data[sid] = {
        "sid": sid,
        "agent": agent,
        "ts": ts,
        "body_state": body_state,
        "first_failed_at": prior.get("first_failed_at", stamp),
        "last_failed_at": stamp,
        "attempts": int(prior.get("attempts", 0)) + 1,
        "last_error": error,
    }
    return save(path, data)


def prune(data: dict, ttl_days: float, now: dt.datetime | None = None):
    """Drop entries whose FIRST failure is older than the TTL (guard-586).

    Keyed on `first_failed_at`, never `last_failed_at`: the latter is advanced
    by the failure path itself, so a permanently-undeliverable carrier would
    refresh its own expiry on every pass and never age out -- guard-5708's
    shape, one level down.

    An unparseable or absent `first_failed_at` is KEPT, not dropped: this is a
    hygiene policy, and silently discarding a malformed obligation is the more
    expensive error.
    """
    now = now or dt.datetime.now()
    cutoff = now - dt.timedelta(days=ttl_days)
    kept, expired = {}, []
    for sid, entry in (data or {}).items():
        if not isinstance(entry, dict):
            continue
        stamp = parse_naive_iso(entry.get("first_failed_at"))
        if stamp is not None and stamp < cutoff:
            expired.append(sid)
            continue
        kept[sid] = entry
    return kept, expired


def retry_one(agent: str, sid: str, entry: dict, state_dir: Path,
              *, apply: bool = False) -> dict:
    """Re-deliver ONE carrier whose push was refused. Destination-checked.

    Verdicts, and every one of them except `still-refused` retires the
    breadcrumb:
      carrier-gone       -- the local carrier is no longer there to deliver.
      local-moved        -- the local `ts` is not the one that failed; some
                            other writer owns this file now.
      destination-newer  -- guard-3849: the authoritative copy carries a
                            DIFFERENT `ts`. Pushing would overwrite state this
                            repair did not create. Refuse and drop.
      already-delivered  -- the authoritative copy already reads the repaired
                            state. The gap closed itself; nothing to do.
      delivered          -- pushed and read back.
      still-refused      -- the transport refused again. KEEP the breadcrumb.
    """
    carrier = state_dir / f"body-heartbeat-{sid}.json"
    try:
        local = json.loads(carrier.read_text(encoding="utf-8"))
        if not isinstance(local, dict):
            raise ValueError("carrier is not an object")
    except (OSError, ValueError, TypeError):
        return {"sid": sid, "agent": agent, "verdict": "carrier-gone", "retire": True}

    if local.get("ts") != entry.get("ts"):
        return {"sid": sid, "agent": agent, "verdict": "local-moved",
                "retire": True, "ts_recorded": entry.get("ts"),
                "ts_local": local.get("ts")}

    # ── guard-3849: READ THE DESTINATION BEFORE A TRANSPORT THAT OVERWRITES ──
    try:
        from storage_backend import get_backend  # noqa: PLC0415
        backend = get_backend()
    except Exception as exc:  # noqa: BLE001 -- transport must not raise here
        return {"sid": sid, "agent": agent, "verdict": "still-refused",
                "retire": False, "push_error": f"{type(exc).__name__}: {exc}"}

    remote = None
    try:
        remote = json.loads(backend.read_bytes(carrier, force_fresh=True)
                            .decode("utf-8"))
    except Exception:  # noqa: BLE001 -- absent/unreadable destination is not newer
        remote = None
    if isinstance(remote, dict):
        if remote.get("ts") != local.get("ts"):
            return {"sid": sid, "agent": agent, "verdict": "destination-newer",
                    "retire": True, "ts_local": local.get("ts"),
                    "ts_remote": remote.get("ts")}
        if remote.get("body_state") == local.get("body_state"):
            return {"sid": sid, "agent": agent, "verdict": "already-delivered",
                    "retire": True, "body_state": local.get("body_state")}

    if not apply:
        return {"sid": sid, "agent": agent, "verdict": "would-deliver",
                "retire": False, "body_state": local.get("body_state")}

    try:
        backend.write_bytes(carrier, carrier.read_bytes())
    except Exception as exc:  # noqa: BLE001 -- transport must not raise here
        return {"sid": sid, "agent": agent, "verdict": "still-refused",
                "retire": False, "push_error": f"{type(exc).__name__}: {exc}"}
    return {"sid": sid, "agent": agent, "verdict": "delivered", "retire": True,
            "body_state": local.get("body_state"), "ts_preserved": True}


def retry_pending(agent: str, *, apply: bool = False,
                  ttl_days: float = DEFAULT_TTL_DAYS,
                  state_dir: Path | None = None,
                  now: dt.datetime | None = None) -> dict:
    """Prune, then retry every pending refused push for the BOUND agent.

    Always returns a report -- `pending: 0` is a result, never silence, so a
    quiet clean pass is distinguishable from a pass that never ran.
    """
    report = {"agent": agent, "apply": apply, "ttl_days": ttl_days,
              "pending": 0, "expired": [], "results": [], "retired": 0,
              "kept": 0, "error": None}
    if not agent:
        report["error"] = "no bound agent -- refusing (the bound agent IS the scope)"
        return report

    path = breadcrumb_path(agent, state_dir)
    data = load(path)
    data, expired = prune(data, ttl_days, now)
    report["expired"] = expired
    report["pending"] = len(data)
    if not data and not expired:
        return report

    remaining = dict(data)
    for sid, entry in sorted(data.items()):
        try:
            carrier_dir = state_dir or agent_state_dir(entry.get("agent") or agent)
            res = retry_one(agent, sid, entry, carrier_dir, apply=apply)
        except Exception as exc:  # noqa: BLE001 -- one bad row never stops the pass
            res = {"sid": sid, "agent": agent, "verdict": "error",
                   "retire": False, "error": f"{type(exc).__name__}: {exc}"}
        report["results"].append(res)
        if res.get("retire") and apply:
            remaining.pop(sid, None)
        elif not res.get("retire") and sid in remaining:
            bumped = dict(remaining[sid])
            if res.get("push_error"):
                bumped["last_error"] = res["push_error"]
            remaining[sid] = bumped

    report["retired"] = len(data) - len(remaining)
    report["kept"] = len(remaining)
    if apply or expired:
        if remaining:
            save(path, remaining)
        else:
            try:
                path.unlink()
            except OSError:
                save(path, {})
    return report
