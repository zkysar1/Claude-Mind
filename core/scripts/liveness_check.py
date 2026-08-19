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

TWO authoritative signals are read, and telling them apart is the whole point
under the Mind/Body split (g-306-132-e):

  * MIND liveness — the ``last_active`` VALUE from inside the shard, read fresh
    from the authoritative store (``fetch_authoritative_last_active`` ->
    ``_team_state.read_shard_authoritative``). Only the mind's own heartbeat
    writes this value, so a Body cannot forge it.
  * BODY activity — the shard OBJECT's last WRITE time (``fetch_fresh_signal``):
    own-cloud -> S3 ``LastModified`` of ``team-state/agents/<agent>.yaml`` (boto3
    HEAD; guard-1052); local -> the shard file's mtime (no sync layer).

Originally (g-115-2149) only the object time existed and it stood in for
liveness, which was correct when one runner per agent was the only writer. It
stopped being correct when a forked worker Body could write the shard while the
reducer was dead: the object refreshes, the mind is gone, and the verdict read
"alive". Object freshness is now corroborating evidence only — it can no longer
promote to ALIVE on its own.

presence.jsonl is deliberately NOT used: it does not sync to S3 (observed
2026-07-14: both alpha's and bravo's S3 presence frozen at 06-26 while local is
fresh), so it is worthless as a cross-box liveness signal.

The verdict is intentionally conservative:
  * ALIVE   — the local last_active, or the AUTHORITATIVE last_active value, is
    within the threshold. (Object freshness alone no longer qualifies.)
  * DORMANT — the mind signals are stale AND the shard object is stale too.
  * UNKNOWN — the signals could not be read, OR the shard object is fresh while
    the authoritative last_active value is stale. That second case is body
    activity without mind liveness: not alive, but not a supported death either
    (guard-1042 forbids concluding dormant from a stale last_active on a
    write-frozen box). DORMANT is the only verdict goal-selector acts on, so a
    false dormant leaks an active agent's routed goals — UNKNOWN degrades toward
    goals-stay-routed, which is the safe direction.
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
                    retired_entry=None, authoritative_last_active_iso=None,
                    authoritative_provenance=None, row_updated_by=None,
                    row_agent=None):
    """Pure liveness decision.

    Returns a dict: {verdict, reason, signal, last_active_age_min, fresh_age_min,
    authoritative_last_active_age_min} where verdict is one of "alive" | "dormant"
    | "unknown" | "retired" and ``signal`` names which input carried the verdict
    ("last_active" | "authoritative_last_active" | "fresh_signal" |
    "retirement_tombstone" | None).

    ``retired_entry`` is the agent's shard dict when it carries a live retirement
    tombstone (see fetch_retirement_tombstone), else None. Defaulting to None keeps
    every existing caller's behavior byte-identical.

    ``authoritative_last_active_iso`` is the ``last_active`` VALUE read from inside
    the authoritative-store shard (g-306-132-e). It is the MIND-liveness signal, as
    distinct from ``fresh_signal_iso``, which is the shard OBJECT's write time and
    therefore only BODY activity. Under the Mind/Body split those came apart: a
    worker Body writing the shard refreshes the object while the reducer is dead,
    so an object-freshness-only rule reported a dead mind as "alive". Defaulting to
    None keeps every existing caller byte-identical — the new branch engages only
    when a caller supplies the value.

    ``authoritative_provenance`` (g-306-138) says WHERE that value came from —
    ``_team_state.PROV_AUTHORITATIVE`` | ``PROV_LOCAL_MIRROR`` | ``PROV_NONE``, or
    None when the caller did not resolve it. ``read_shard_authoritative`` fails
    open to the local mirror, so without this the branch below promoted a MIRROR
    value to verdict=alive while its reason string asserted "the local mirror
    lagged" — a claim of authoritative provenance for a value read from the
    mirror. Only ``PROV_LOCAL_MIRROR`` changes behavior (degrade to unknown);
    None and PROV_AUTHORITATIVE are treated identically, so every pre-g-306-138
    caller stays byte-identical.

    ``row_updated_by`` / ``row_agent`` (g-115-6410) are the shard row's last
    WRITER and the row's OWN agent. When they differ, every freshness signal
    this function receives was produced by that OTHER agent's write, so none of
    them can certify the subject alive — see the cross-stamp guard below. Both
    default to None, and an unknown ``row_agent`` disqualifies nothing, so every
    pre-existing caller stays byte-identical.
    """
    if now is None:
        raise ValueError("now must be supplied (kept explicit for testability)")
    thr = timedelta(hours=float(threshold_hours))
    la_age = _age(last_active_iso, now)
    fs_age = _age(fresh_signal_iso, now)
    ala_age = _age(authoritative_last_active_iso, now)

    def mins(d):
        return round(d.total_seconds() / 60.0, 1) if d is not None else None

    # The provenance travels WITH the age it qualifies, in every verdict. Found by
    # /fresh-eyes-code on this goal's own diff: without it, a consumer reading
    # authoritative_last_active_age_min faces exactly the ambiguity g-306-138 set
    # out to remove — a number with no way to tell whether the value behind it came
    # from the store of record or from a failed-open mirror. main() used to bolt the
    # field on afterwards, which left the PURE function (the one goal-selector calls
    # directly) still unable to answer the question. Not a live defect at the time —
    # goal-selector reads only ["verdict"] — but it is this defect class re-entering
    # one layer up, in the fix for it.
    base = {"last_active_age_min": mins(la_age), "fresh_age_min": mins(fs_age),
            "authoritative_last_active_age_min": mins(ala_age),
            "authoritative_last_active_provenance": authoritative_provenance}

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

    # A ROW STAMPED BY ANOTHER AGENT CANNOT CERTIFY ITS OWN SUBJECT ALIVE
    # (g-115-6410, guard-3604). team-state-clear-in-flight.sh sets
    # `last_active = now` and then stamps `row_updated_by = <the clearer>`
    # (_team_state.make_clear_in_flight_modifier -> stamp_row_metadata), so
    # policing a DORMANT peer's stranded claim makes that peer read fresh for a
    # full threshold window. Measured 2026-08-13 on echo: dormant at 419.1min ->
    # alive at 1.8min, row_updated_by=bravo, echo never having woken; and
    # 2026-08-16, foxtrot dead 11.64h while reading alive.
    #
    # THE BUMP IS DELIBERATE AND IS NOT THE THING TO FIX. Shard merges are
    # whole-snapshot LWW on `last_active`, so an unstamped pop loses the merge
    # and RESURRECTS the cleared claim. The write is correct; what is wrong is
    # reading its side effect as evidence about the subject.
    #
    # WHY THIS DISQUALIFIES EVERY SIGNAL AND NOT MERELY THE FAST PATH. Measured
    # on this goal before writing the fix: disqualifying only the fast path and
    # falling through to the authoritative read still returns ALIVE, because the
    # bumped value is precisely what that read returns — it must reach the store
    # to win the LWW merge. The verdict merely moves from signal=last_active to
    # signal=authoritative_last_active, and gets WORSE: provenance goes null ->
    # "authoritative", erasing the `provenance: null` tell guard-3604 names as
    # the self-concealing signature, under a reason string that then asserts
    # "the mind is running". The shard OBJECT time is the same writer's push, so
    # it is tainted too. All three inputs trace to one foreign write.
    #
    # UNKNOWN, never dormant: the peer may well be alive, and "dormant" is the
    # one verdict goal-selector._liveness_confirms_dormant acts on, so a false
    # dormant would leak an active agent's routed goals cross-agent. Unknown
    # degrades toward goals-stay-routed, the same fail-safe direction as every
    # other cannot-verify branch here.
    #
    # SCOPED TO FRESHNESS ON PURPOSE. A cross-stamp only ever manufactures a
    # FALSE ALIVE; it cannot manufacture a false dormant. So an OLD stamp whose
    # signals have all aged out is harmless and must keep reaching the dormant
    # conclusion below — otherwise policing a peer once would render it
    # permanently unjudgeable. Self-healing: the owner's next heartbeat stamps
    # `row_updated_by` back to itself and the fast path returns.
    cross_stamped = bool(row_updated_by) and bool(row_agent) and row_updated_by != row_agent
    if cross_stamped:
        fresh_signals = [n for n, a in (("last_active", la_age),
                                        ("authoritative_last_active", ala_age),
                                        ("fresh_signal", fs_age))
                         if a is not None and a <= thr]
        if fresh_signals:
            return {"verdict": "unknown", "signal": None,
                    "reason": (f"row was last written by '{row_updated_by}', not by '{row_agent}' — "
                               f"the fresh signal(s) {', '.join(fresh_signals)} are an artifact of "
                               f"that agent's write (a cross-agent in_flight clear bumps the CLEARED "
                               f"row's last_active), not evidence '{row_agent}' is running; cannot "
                               "verify, do NOT conclude alive. Read a signal with an INDEPENDENT "
                               "writer (execution-diary.jsonl, working-memory.yaml) for real liveness "
                               "inside this window"),
                    **base}

    # Fast path: a fresh last_active is sufficient (common case, no fresh-signal
    # fetch needed by the caller).
    if la_age is not None and la_age <= thr:
        return {"verdict": "alive", "signal": "last_active",
                "reason": f"last_active fresh ({_fmt_age(la_age)} ago, <= {threshold_hours:g}h)", **base}

    # The local last_active is stale or absent. Prefer the AUTHORITATIVE VALUE of
    # last_active over the shard object's write time: the value is mind-liveness,
    # the object time is only body activity (g-306-132-e). A fresh authoritative
    # value settles it — this is the local-mirror-lag case g-115-2149 fixed, now
    # answered with a signal that a worker Body cannot forge.
    if ala_age is not None and ala_age <= thr:
        # ...but ONLY when the value actually came from the authoritative store.
        # read_shard_authoritative fails open to the LOCAL MIRROR on a backend or
        # read error and returns a bare row that cannot say so (guard-1753), so a
        # fresh-looking value here may be the same mirror the fast path just found
        # stale. Promoting it would assert "the local mirror lagged" using a value
        # read FROM that mirror — a false ALIVE for an agent whose mirror was
        # pulled recently but has since died, which is the class g-306-132-e
        # closed, re-entering through the error path. UNKNOWN is the fail-safe
        # answer that goal-selector._liveness_confirms_dormant does not act on, so
        # the goals stay routed (g-306-138).
        if authoritative_provenance == "local-mirror":
            return {"verdict": "unknown", "signal": None,
                    "reason": (f"a fresh last_active VALUE ({_fmt_age(ala_age)} ago) was found, but it "
                               "came from the LOCAL MIRROR — the authoritative-store read failed open. "
                               "A mirror value cannot establish that the mirror lagged; cannot verify, "
                               "do NOT conclude dormant"),
                    **base}
        la_desc = "stale" if la_age is not None else "absent"
        return {"verdict": "alive", "signal": "authoritative_last_active",
                "reason": (f"local last_active {la_desc} but the authoritative-store shard's "
                           f"last_active VALUE is fresh ({_fmt_age(ala_age)} ago) — the mind is "
                           "running and the local mirror lagged"),
                **base}

    # last_active stale or absent — the inherently-fresh signal decides.
    if fs_age is not None and fs_age <= thr:
        # A fresh shard OBJECT with a STALE authoritative last_active VALUE is the
        # Mind/Body split: something on that box is writing the shard (a worker
        # Body, a sync, a merge) while the mind's own heartbeat has aged out. That
        # is NOT evidence of life, so it must not return "alive" — the g-306-132-e
        # defect. It is not evidence of death either, so it must not return
        # "dormant": guard-1042 forbids concluding dormant from a stale last_active
        # on a write-frozen box, and "dormant" is the one verdict
        # goal-selector._liveness_confirms_dormant acts on, so a false dormant
        # would leak an active agent's routed goals cross-agent. UNKNOWN is the
        # only honest answer, and it degrades toward goals-stay-routed.
        if ala_age is not None:
            return {"verdict": "unknown", "signal": None,
                    "reason": (f"shard object is fresh ({_fmt_age(fs_age)} ago) but the "
                               f"authoritative last_active VALUE is stale ({_fmt_age(ala_age)} ago) "
                               "— a write to the shard is BODY activity and does not establish that "
                               "the MIND is running; cannot verify, do NOT conclude dormant"),
                    **base}
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


def fetch_authoritative_last_active_with_provenance(agent, world_dir):
    """``(last_active_iso, provenance)`` from inside the agent's authoritative shard.

    The MIND-liveness signal (g-306-132-e), as against fetch_fresh_signal's shard
    OBJECT write time, which is only BODY activity. Delegates to the shared
    single-shard primitive ``_team_state.read_shard_authoritative_with_provenance``
    rather than re-implementing the backend dispatch — that helper is the one place
    the own-cloud force_fresh read and its fail-open ladder live, so both this
    module and its sibling consumers agree by construction.

    NO ``backend`` PARAMETER, deliberately (g-306-138 outcome 4). The primitive
    dispatches on STORAGE_BACKEND itself; accepting one here would create a second
    source of truth for that decision. The parameter used to be accepted "for call-
    shape symmetry with fetch_fresh_signal" and then ignored, which meant
    ``--backend own-cloud`` under an unset STORAGE_BACKEND would send one probe to
    S3 and the other to the local file. It is removed rather than honoured because
    honouring it is the second-source-of-truth bug; a signature that cannot express
    the wrong call is better than one that documents it away.

    Fail-quiet: any error returns ``(None, PROV_NONE)``, and the caller degrades to
    the object-time signal exactly as before this function existed.
    """
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import _team_state
        row, prov = _team_state.read_shard_authoritative_with_provenance(world_dir, agent)
        if isinstance(row, dict):
            return (row.get("last_active"), prov)
    except Exception as e:  # noqa: BLE001 — fail-quiet by design
        sys.stderr.write(
            f"[liveness-check] authoritative last_active unavailable: {type(e).__name__}\n")
    return (None, "none")


def fetch_authoritative_last_active(agent, world_dir):
    """The ``last_active`` VALUE only — provenance-blind wrapper over the pair above.

    Kept because dropping a value's provenance is legitimate when the caller does
    not branch on it. Callers that promote this value to a liveness VERDICT must
    use the ``_with_provenance`` form: see g-306-138 and guard-1753.
    """
    return fetch_authoritative_last_active_with_provenance(agent, world_dir)[0]


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


def fetch_row_stamp(agent, world_dir):
    """The shard row's ``row_updated_by`` (last writer), or None (g-115-6410).

    Feeds ``decide_liveness``'s cross-stamp guard. Deliberately a LOCAL shard
    read and NOT a store read: the whole point of the fast path is that a
    same-agent row costs no authoritative fetch, so paying an S3 round-trip to
    decide whether we may take the fast path would spend exactly what the fast
    path exists to save. Same file, same fail-open posture, and the same
    read-through-cache caveat as ``fetch_retirement_tombstone`` above.

    COHERENT WITH ``--last-active`` BY CONSTRUCTION, which matters because the
    guard compares a stamp against a timestamp and a split-brain pair would
    make it meaningless. The wrapper's ``last_active`` comes from
    ``team-state-read.sh``, whose composition (``_team_state.load_rows``) reads
    these same local shard files without re-fetching — so both fields come off
    one snapshot. Verified 2026-08-17: composed ``agent_status.echo.row_updated_by``
    and the local ``echo.yaml`` agree.

    Both failure directions are acceptable, which is why fail-open is safe here.
    A local mirror BEHIND the store hides a cross-stamp -> today's behavior, no
    worse. AHEAD of it -> an extra "unknown" -> goals stay routed. Neither can
    manufacture a false ALIVE, and the second self-heals on the owner's next
    heartbeat.
    """
    shard = os.path.join(world_dir, "team-state", "agents", f"{agent}.yaml")
    try:
        import yaml
        with open(shard, "r", encoding="utf-8") as fh:
            entry = yaml.safe_load(fh) or {}
        val = entry.get("row_updated_by")
        return str(val) if val else None
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
    auth_la_iso = None
    auth_la_prov = None
    if not (la_age is not None and la_age <= timedelta(hours=args.threshold_hours)):
        # Both authoritative reads happen only off the fast path. The VALUE is
        # fetched first because it can settle the verdict on its own; the object
        # time is still needed to tell "stale mind, active box" (unknown) apart
        # from "stale mind, quiet box" (dormant).
        #
        # --backend is passed to fetch_fresh_signal ONLY. The last_active read
        # dispatches on STORAGE_BACKEND inside the shared primitive and takes no
        # backend argument (g-306-138) — see the note in its docstring.
        auth_la_iso, auth_la_prov = fetch_authoritative_last_active_with_provenance(
            args.agent, args.world_dir)
        fresh_iso = fetch_fresh_signal(args.agent, args.world_dir, args.backend)

    # Cheap local read, and it must run even on the last_active-fresh fast path:
    # an agent retired moments ago still has a fresh last_active.
    retired_entry = fetch_retirement_tombstone(args.agent, args.world_dir)

    # Same reasoning, same cost, for the cross-stamp guard: a row stamped by
    # another agent reads FRESH, so this must be available precisely on the
    # fast path it disqualifies (g-115-6410). The fetch gate above deliberately
    # stays unchanged — a cross-stamped fresh row returns "unknown" from the
    # guard before ala/fs are consulted, so it still pays no store read.
    row_stamp = fetch_row_stamp(args.agent, args.world_dir)

    result = decide_liveness(args.last_active, fresh_iso, args.threshold_hours, now=now,
                             retired_entry=retired_entry,
                             authoritative_last_active_iso=auth_la_iso,
                             authoritative_provenance=auth_la_prov,
                             row_updated_by=row_stamp, row_agent=args.agent)
    result["agent"] = args.agent
    result["fresh_signal_iso"] = fresh_iso
    result["authoritative_last_active_iso"] = auth_la_iso
    # Surfaced alongside the other raw inputs so a --json consumer can audit the
    # cross-stamp verdict structurally instead of parsing it out of `reason`.
    result["row_updated_by"] = row_stamp
    # authoritative_last_active_provenance is NOT set here: decide_liveness now
    # carries it in `base` for every verdict, so re-writing it would be a second
    # source of truth for the same field.
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
