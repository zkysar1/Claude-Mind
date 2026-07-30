#!/usr/bin/env python3
# domain-leak-exempt: names the own-cloud team-state shard path + STORAGE_BACKEND,
# which are framework infrastructure identifiers, not domain content.
"""liveness_check.py — decide whether an agent is ALIVE, DORMANT, or UNKNOWN by
combining the pushed ``last_active`` snapshot with an INHERENTLY-FRESH signal.

Motivation (g-115-2149): ``agent_status.<agent>.last_active`` is a pushed
snapshot synced cross-box via own-cloud. On a reader's box the peer's shard is a
read-through LOCAL mirror; ``_team_state.load_rows`` composes team-state from
that local mirror (raw ``open()``, no S3 read-through), so a peer's last_active
freezes at whenever THIS box last pulled the peer's shard — even while the peer
pushes fresh values to S3 every iteration. Result: readers falsely conclude a
busy partner is dormant (observed 2026-07-14: bravo local shard 07-07, bravo S3
shard 07-14; foxtrot local 07-08). check-team-state-before-silent.md rule-5
documents the diagnosis; this module is the reusable resolution.

The inherently-fresh signal is the partner's team-state SHARD's last WRITE time,
read from the authoritative store rather than the local mirror:
  * own-cloud backend -> S3 ``LastModified`` of ``team-state/agents/<agent>.yaml``
    (boto3 HEAD; ground-truth per guard-1052 — the object's LastModified is when
    the peer's box actually pushed, immune to local-mirror staleness).
  * local backend -> the shard file's local mtime (no sync layer; mtime is truth).

presence.jsonl is deliberately NOT used: it does not sync to S3 (observed
2026-07-14: both alpha's and bravo's S3 presence frozen at 06-26 while local is
fresh), so it is worthless as a cross-box liveness signal.

The verdict is intentionally conservative:
  * ALIVE   — last_active OR the fresh signal is within the threshold.
  * DORMANT — both are present but both are stale (> threshold).
  * UNKNOWN — neither signal could be read (do NOT report DORMANT when we could
    not even check — UNKNOWN is safer than a false-dormant).
  * RETIRED — the agent's shard carries a live retirement tombstone. Checked
    FIRST, because retirement dominates both freshness signals (g-115-3702).

Why RETIRED cannot be derived from freshness (g-115-3702): retirement is a
TOMBSTONE, not a delete — shard deletion needs s3:DeleteObject rights fleet
boxes do not hold — so a retired agent's shard survives and keeps being
written, and the retirement WRITE ITSELF refreshes the fresh signal. Retiring
an agent therefore made it look MORE alive for a full threshold window. Because
composing the roster drops retired rows, ``last_active`` also comes back absent,
removing the one signal that would have aged into DORMANT and leaving shard
freshness as the sole input. Measured on `meta-tiebreaker`: retired_at
17:08:19, authoritative-store push 17:08:20, verdict "alive" 2.8h later.

``decide_liveness`` is a PURE function (no IO) so it is unit-testable without a
backend; the boto3 / mtime fetch lives in ``main`` behind ``fetch_fresh_signal``,
and the tombstone read behind ``fetch_retirement_tombstone``.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta

DEFAULT_THRESHOLD_HOURS = 6.0


def _parse_iso(ts):
    """Parse an ISO-8601 timestamp (tolerant of a trailing Z and JSON quoting).
    Returns a naive local datetime (tzinfo stripped) or None."""
    if ts is None:
        return None
    s = str(ts).strip().strip('"').strip("'")
    if not s or s.lower() in ("null", "none", ""):
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    # Normalize to naive local time so age math against a naive ``now`` is
    # consistent whether the source carried a tz offset or not.
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _age(ts, now):
    """timedelta since ``ts`` (>=0), or None when ts is missing/unparseable."""
    dt = _parse_iso(ts)
    if dt is None:
        return None
    delta = now - dt
    # Clamp small future skew to zero so a peer clock slightly ahead still reads
    # as fresh rather than negative.
    if delta.total_seconds() < 0:
        delta = timedelta(0)
    return delta


def _fmt_age(delta):
    if delta is None:
        return "n/a"
    mins = delta.total_seconds() / 60.0
    if mins < 90:
        return f"{mins:.0f}m"
    return f"{mins / 60.0:.1f}h"


def decide_liveness(last_active_iso, fresh_signal_iso, threshold_hours=DEFAULT_THRESHOLD_HOURS, now=None,
                    retired_entry=None):
    """Pure liveness decision.

    Returns a dict: {verdict, reason, signal, last_active_age_min, fresh_age_min}
    where verdict is one of "alive" | "dormant" | "unknown" | "retired" and
    ``signal`` names which input carried the verdict ("last_active" |
    "fresh_signal" | "retirement_tombstone" | None).

    ``retired_entry`` is the agent's shard dict when it carries a live retirement
    tombstone (see fetch_retirement_tombstone), else None. Defaulting to None keeps
    every existing caller's behavior byte-identical.
    """
    if now is None:
        raise ValueError("now must be supplied (kept explicit for testability)")
    thr = timedelta(hours=float(threshold_hours))
    la_age = _age(last_active_iso, now)
    fs_age = _age(fresh_signal_iso, now)

    def mins(d):
        return round(d.total_seconds() / 60.0, 1) if d is not None else None

    base = {"last_active_age_min": mins(la_age), "fresh_age_min": mins(fs_age)}

    # Retirement DOMINATES every freshness signal, and is checked before the fast
    # path on purpose: an agent retired moments ago still has a fresh last_active,
    # so a freshness-first ordering would report it alive. _is_retired has already
    # applied the revival rule (a heartbeat newer than retired_at un-retires), so
    # reaching here with a tombstone means the row is genuinely decommissioned.
    # "retired" is a distinct verdict rather than "dormant" because the two
    # authorise different things: dormant means quiet and may come back, retired
    # means gone. Consumers testing `verdict == "dormant"` (goal-selector's
    # _liveness_confirms_dormant) therefore get False and keep goals routed —
    # the fail-safe direction (g-115-3702).
    if retired_entry:
        ra = str(retired_entry.get("retired_at") or "unknown")
        by = str(retired_entry.get("retired_by") or "unknown")
        return {"verdict": "retired", "signal": "retirement_tombstone",
                "reason": (f"agent carries a retirement tombstone (retired_at {ra}, by {by}) — "
                           "decommissioned, not merely quiet. Shard freshness is NOT evidence of "
                           "life here: the shard survives retirement and the retirement write "
                           "itself refreshes it."),
                **base}

    # Fast path: a fresh last_active is sufficient (common case, no fresh-signal
    # fetch needed by the caller).
    if la_age is not None and la_age <= thr:
        return {"verdict": "alive", "signal": "last_active",
                "reason": f"last_active fresh ({_fmt_age(la_age)} ago, <= {threshold_hours:g}h)", **base}

    # last_active stale or absent — the inherently-fresh signal decides.
    if fs_age is not None and fs_age <= thr:
        la_desc = "stale" if la_age is not None else "absent"
        return {"verdict": "alive", "signal": "fresh_signal",
                "reason": (f"last_active {la_desc} but fresh signal (authoritative-store push) "
                           f"is fresh ({_fmt_age(fs_age)} ago) — partner is active, local mirror lagged"),
                **base}

    # last_active is not fresh. The fresh signal now decides between dormant and
    # unknown — and the distinction is load-bearing (outcome 1: a partner active
    # within the threshold must NEVER be concluded dormant):
    #   * fresh signal AVAILABLE but stale  -> both authoritative signals say old
    #     -> DORMANT (a supported negative conclusion).
    #   * fresh signal UNAVAILABLE (None — boto3 failed, no creds, shard absent)
    #     -> we could not verify -> UNKNOWN. Concluding DORMANT here would be a
    #     false-dormant on a transient fetch failure, the exact defect this fixes.
    if fs_age is not None:
        la_desc = f"last_active ({_fmt_age(la_age)})" if la_age is not None else "last_active absent"
        return {"verdict": "dormant", "signal": None,
                "reason": (f"{la_desc} and fresh signal ({_fmt_age(fs_age)}) both stale "
                           f"(> {threshold_hours:g}h) — dormant is a supported conclusion"),
                **base}

    return {"verdict": "unknown", "signal": None,
            "reason": ("last_active not fresh AND the inherently-fresh signal was unavailable "
                       "(could not reach the authoritative store) — cannot verify; do NOT conclude dormant"),
            **base}


def fetch_owncloud_shard_lastmodified(agent, world_dir):
    """S3 LastModified (local-tz ISO) of the agent's team-state shard, or None.

    Ground-truth cross-box freshness per guard-1052: the object's LastModified is
    when the peer's box actually PUT the shard, immune to local-mirror staleness.
    Fail-quiet: any error (no creds, boto3 missing, key absent) returns None so
    the caller degrades to UNKNOWN rather than a false verdict."""
    try:
        import boto3  # noqa: local import — only the own-cloud path needs it
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        from owncloud_backend import OwnCloudBackend
        be = OwnCloudBackend.from_env()
        shard = os.path.join(world_dir, "team-state", "agents", f"{agent}.yaml")
        key = be._s3_key(shard)
        bucket = os.environ["STORAGE_S3_BUCKET"]
        head = boto3.client("s3").head_object(Bucket=bucket, Key=key)
        return head["LastModified"].astimezone().replace(tzinfo=None).isoformat(timespec="seconds")
    except Exception as e:  # noqa: BLE001 — fail-quiet by design
        sys.stderr.write(f"[liveness-check] own-cloud fresh-signal unavailable: {type(e).__name__}\n")
        return None


def fetch_local_shard_mtime(agent, world_dir):
    """Local mtime (ISO) of the agent's shard — ground truth on the local backend
    (no sync layer). Returns None when the shard is absent."""
    shard = os.path.join(world_dir, "team-state", "agents", f"{agent}.yaml")
    try:
        return datetime.fromtimestamp(os.path.getmtime(shard)).isoformat(timespec="seconds")
    except OSError:
        return None


def fetch_fresh_signal(agent, world_dir, backend):
    """Inherently-fresh signal ISO for the agent, dispatched by backend."""
    if str(backend).strip().lower() in ("own-cloud", "owncloud", "s3"):
        return fetch_owncloud_shard_lastmodified(agent, world_dir)
    return fetch_local_shard_mtime(agent, world_dir)


def fetch_retirement_tombstone(agent, world_dir):
    """The agent's shard dict when it carries a live retirement tombstone, else None.

    Retirement is a TOMBSTONE, not a delete: shard deletion needs s3:DeleteObject
    rights fleet boxes do not hold, so a retired agent's shard SURVIVES and keeps
    getting written. Both freshness signals above read that shard's write time, so
    without this check a decommissioned agent reads "alive" forever.

    The inversion that makes it worse (g-115-3702, observed 2026-07-28): the
    retirement WRITE ITSELF refreshes the signal, so retiring an agent makes it
    look MORE alive for a full threshold window. Measured on `meta-tiebreaker` —
    retired_at 17:08:19, authoritative-store push 17:08:20 (one second later),
    verdict still "alive" 2.8h afterwards. And because composing the roster drops
    retired rows, ``last_active`` comes back absent, which removes the one signal
    that would otherwise have aged into "dormant" — leaving shard freshness as the
    SOLE input and any future write (a sync, a merge, a re-push) re-freshening it.

    Retirement semantics are NOT reimplemented here: ``_team_state._is_retired`` is
    the single source of truth and carries the self-healing revival rule (a
    heartbeat newer than retired_at un-retires the row).

    Fail-open (same posture as fetch_fresh_signal): any missing shard, unreadable
    YAML, or import failure returns None and the caller falls through to the
    freshness verdict. Note this reads the LOCAL shard — under own-cloud the local
    tree is a read-through cache, so an un-pulled shard yields None on a box that
    has never read it (guard-980: absence of a local file is not evidence).
    """
    shard = os.path.join(world_dir, "team-state", "agents", f"{agent}.yaml")
    try:
        import yaml
        import _team_state
        with open(shard, "r", encoding="utf-8") as fh:
            entry = yaml.safe_load(fh) or {}
        return entry if _team_state._is_retired(entry) else None
    except Exception:  # noqa: BLE001 — fail-open, never block a liveness read
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Liveness verdict combining last_active + an inherently-fresh signal.")
    ap.add_argument("--agent", required=True)
    ap.add_argument("--last-active", default=None,
                    help="last_active ISO for the agent (wrapper reads it via daemon-routed team-state-read.sh)")
    ap.add_argument("--threshold-hours", type=float, default=DEFAULT_THRESHOLD_HOURS)
    ap.add_argument("--world-dir", default=os.environ.get("WORLD_PATH") or os.environ.get("WORLD_DIR") or ".")
    ap.add_argument("--backend", default=os.environ.get("STORAGE_BACKEND", "local"))
    ap.add_argument("--now", default=None, help="ISO override for tests; defaults to system local time")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    now = _parse_iso(args.now) if args.now else datetime.now()

    # Only fetch the (potentially expensive) fresh signal when last_active is not
    # already fresh — the common case short-circuits with zero S3 / mtime IO.
    la_age = _age(args.last_active, now)
    fresh_iso = None
    if not (la_age is not None and la_age <= timedelta(hours=args.threshold_hours)):
        fresh_iso = fetch_fresh_signal(args.agent, args.world_dir, args.backend)

    # Cheap local read, and it must run even on the last_active-fresh fast path:
    # an agent retired moments ago still has a fresh last_active.
    retired_entry = fetch_retirement_tombstone(args.agent, args.world_dir)

    result = decide_liveness(args.last_active, fresh_iso, args.threshold_hours, now=now,
                             retired_entry=retired_entry)
    result["agent"] = args.agent
    result["fresh_signal_iso"] = fresh_iso
    result["last_active_iso"] = (args.last_active or None)

    if args.json:
        print(json.dumps(result))
    else:
        print(result["verdict"])
        sys.stderr.write(f"[liveness-check] {args.agent}: {result['verdict']} — {result['reason']}\n")
    # Exit 0 always — the VERDICT is the signal, not the exit code (mirrors the
    # tri-state companion pattern in world/scripts/inbox-watch-pull.sh).
    return 0


if __name__ == "__main__":
    sys.exit(main())
