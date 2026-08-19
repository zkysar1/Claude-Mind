"""Sharded team-state helpers — single source of truth for row routing + composition.

g-328-27 (2026-07-07): world/team-state.yaml carried every agent's
agent_status row, making it the hottest cross-writer file in the fleet —
every agent bumps its own last_active/in_flight every iteration, so N
agents contended on one lock (local) / one CAS-fenced S3 object
(own-cloud). Sharding: each agent's row lives in its OWN file

    world/team-state/agents/<name>.yaml

written ONLY by that agent's sessions, so no two writers ever touch the
same object — contention disappears by construction, and on the own-cloud
backend the per-object IfMatch fences never cross agents (rb-2639: the
CAS deadlock class is per-object). Shared, rarely-written fields
(strategic_focus, critical_blockers, recent_completions, shared_cadences,
inbox_alert_backlog, ...) stay in world/team-state.yaml (the "core" file).

Both core/scripts/team-state.py (CLI) and mind_api/src/world/team_state*.py
(daemon) import THIS module for routing + composition, satisfying the
guard-742 dual-write parity rule by construction — there is no second
implementation to drift from.

Compose semantics mirror coordination_merge._merge_agent_status: per-agent
WHOLE-ROW newest-wins (never field-stitch — a partial merge could pair an
in_flight from one snapshot with a current_focus from another). The row
file wins ties: post-shard it is the source of truth; a core-file entry is
either a pre-migration residual or a mixed-version-fleet write, and the
newest-wins comparison keeps both roll-forward AND roll-back windows
correct (whichever side an agent's code version writes, readers follow the
freshest snapshot).

Migration is lazy + optional: the first row write for an agent self-seeds
from the core residual (``core_residual`` passed as ``initial`` to
locked_modify_yaml), so no coordinated fleet migration is required. The
one-shot ``team-state.py migrate-shard`` merely cleans residuals out of the
core file.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import yaml

# Path segments of the rows directory under WORLD_DIR. Inlined copies exist
# in import-cycle-proof modules (core/scripts/_agents.py) — keep in sync.
ROWS_SUBDIR = ("team-state", "agents")

# Cold archive for retired agent rows (retire_agent). Deliberately NOT under
# ROWS_SUBDIR ("team-state/agents") so load_rows()'s shard glob never sees it,
# and NOT the core file — the live team-state code never reads it. It IS a
# subpath of the existing "team-state" dir, so the L1 new-top-level-under-world
# cruft gate allows it (archive-before-delete.md step 3: a location the live
# system does not read + no retention clock touches).
GRAVEYARD_SUBDIR = ("team-state", ".graveyard")

# Row-write metadata stamps (row-scoped analog of the core file's
# last_updated/last_updated_by). Also feed the newest-wins comparison so a
# row write that does not bump last_active still beats a stale residual.
ROW_UPDATED_KEY = "row_updated"
ROW_UPDATED_BY_KEY = "row_updated_by"


def rows_dir(world_dir) -> Path:
    d = Path(world_dir)
    for seg in ROWS_SUBDIR:
        d = d / seg
    return d


def graveyard_dir(world_dir) -> Path:
    d = Path(world_dir)
    for seg in GRAVEYARD_SUBDIR:
        d = d / seg
    return d


def row_path(world_dir, agent: str) -> Path:
    """Path of one agent's row file. Validates the agent name at the file
    boundary — an empty/dotted/separator-bearing name would escape the rows
    dir or collide with directory entries."""
    name = (agent or "").strip()
    if not name or name in (".", "..") or any(c in name for c in "/\\"):
        raise ValueError(f"invalid agent name for team-state row: {agent!r}")
    return rows_dir(world_dir) / f"{name}.yaml"


# Sibling-row backend overlay (, 2026-07-11). Each agent's shard is
# written ONLY on its own box; the own-cloud mirror sweep is PUSH-ONLY, so
# sibling shards never land in this box's local rows dir. A local-only iterdir
# therefore composes clone-era fossils for every partner (fleet-wide
# last_active split-brain: every box read its OWN row fresh and all partners
# frozen at box-setup values, while S3 held every shard fresh). Fix: overlay
# the storage backend's view of the rows dir — S3-fresh under OwnCloudBackend,
# plain local re-reads under LocalBackend (harmless no-op). TTL-cached
# in-process: the long-lived daemon amortizes to one LIST + N GETs per
# _BACKEND_ROWS_TTL_S; short-lived CLI imports pay one pull per process. TTL
# must stay well under the 5-minute cross-agent freshness acceptance bound.
# Fail-open: any backend error returns {} and compose falls back to local rows
# + core residuals (the pre-fix behavior). Cache keyed by rows-dir so tests
# composing multiple tmp worlds in one process never cross-contaminate.
_BACKEND_ROWS_TTL_S = 120
_backend_rows_cache: dict = {}  # {rows_dir_str: (monotonic_at, rows_dict)}


def _backend_rows(world_dir) -> dict:
    import time
    d = rows_dir(world_dir)
    key = str(d)
    hit = _backend_rows_cache.get(key)
    now = time.monotonic()
    if hit is not None and (now - hit[0]) < _BACKEND_ROWS_TTL_S:
        return hit[1]
    rows: dict = {}
    try:
        from storage_backend import get_backend  # lazy — import-cycle-proof
        b = get_backend()
        for name in b.list_dir(d):
            if not name.endswith(".yaml"):
                continue
            try:
                doc = yaml.safe_load(b.read_text(d / name, force_fresh=True))
            except Exception as e:  # noqa: BLE001 — one bad row must not break compose
                print(f"[_team_state] WARN: unreadable backend row {name}: {e}",
                      file=sys.stderr)
                continue
            if isinstance(doc, dict) and doc:
                rows[name[:-5]] = doc
    except Exception as e:  # noqa: BLE001 — overlay is additive, never breaks compose
        print(f"[_team_state] WARN: backend row overlay unavailable: {e}",
              file=sys.stderr)
        return {}
    _backend_rows_cache[key] = (now, rows)
    return rows


def load_rows(world_dir) -> dict:
    """All row files as {agent_name: row_dict}. Unreadable/non-dict rows are
    skipped loudly on stderr (a corrupt row must not take down every
    composed read — the owning agent's next stamp rewrites it). Local rows
    are overlaid with the backend's (S3-authoritative) sibling rows,
    per-agent newest-wins — see _backend_rows (g-115-1979)."""
    out: dict = {}
    d = rows_dir(world_dir)
    try:
        entries = sorted(d.iterdir())
    except OSError:
        entries = []
    for p in entries:
        if p.suffix != ".yaml" or not p.is_file():
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                doc = yaml.safe_load(f)
        except Exception as e:  # noqa: BLE001 — one bad row must not break compose
            print(f"[_team_state] WARN: unreadable row {p.name}: {e}", file=sys.stderr)
            continue
        if isinstance(doc, dict) and doc:
            out[p.stem] = doc
    for name, row in _backend_rows(world_dir).items():
        cur = out.get(name)
        if cur is None or _entry_ts(row) > _entry_ts(cur):
            out[name] = row
    return out


def _is_owncloud_backend() -> bool:
    """True when STORAGE_BACKEND names the own-cloud/S3 backend. Mirrors the
    dispatch in liveness_check.py so both authoritative-read paths agree."""
    return str(os.environ.get("STORAGE_BACKEND", "local")).strip().lower() in (
        "own-cloud", "owncloud", "s3")


#: Provenance values returned by the ``*_with_provenance`` reads below
#: (``read_shard_authoritative_with_provenance``, single shard, scalar;
#: ``load_rows_authoritative_with_provenance``, multi-shard, per-agent).
PROV_AUTHORITATIVE = "authoritative"
PROV_LOCAL_MIRROR = "local-mirror"
PROV_NONE = "none"


def load_rows_authoritative(world_dir) -> dict:
    """Row files as {agent: row}, but on the own-cloud backend each peer shard
    is read FRESH from the authoritative store (S3) rather than the read-through
    LOCAL mirror.

    Why (g-115-2188 / guard-980; sanctioned pattern cf. liveness_check.py
    g-115-2149): own-cloud's local shard mirror is conflict-skipped/frozen for
    PEER shards — a reader never writes a peer's shard, and the local-mirror sync
    CONFLICT-SKIPS divergent files (g-115-2163) — so plain ``load_rows`` returns
    stale OR ABSENT peer rows (observed 2026-07-14 on cc-04: bravo/foxtrot local
    shards 7 days stale; echo/zeta shards absent locally but present + fresh on
    S3). Any consumer that decides coordination from that (the partner_in_flight
    double-claim guard) goes permanently blind. This is the SANCTIONED surgical
    per-consumer authoritative read — the same shape liveness_check.py uses for
    the liveness verdict, here for the in_flight CONTENT. Only opt-in consumers
    pay the S3 cost; the hot ``load_rows``/``compose_state`` path is untouched.

    Fail-open at EVERY layer — local backend, backend-init error, S3-list error,
    or a per-shard read error each degrade to the corresponding ``load_rows``
    row, so the result is never worse than today's local read.

    THE BARE DICT CANNOT SAY WHICH LAYER PRODUCED IT (guard-1753) — and unlike
    the single-shard sibling it could not say so uniformly even if it tried. A
    caller that decides anything from a stale OR MISSING peer row must call
    ``load_rows_authoritative_with_provenance`` instead. This wrapper keeps the
    pre-g-306-158 contract byte-identical for provenance-blind callers
    (rb-2148 — add an optional sibling, never break the locked field).
    """
    return load_rows_authoritative_with_provenance(world_dir)[0]


def load_rows_authoritative_with_provenance(world_dir):
    """``(rows, provenance)`` — the provenance-carrying form of the read above.

    Added by g-306-158, mirroring g-306-138's single-shard fix.

    WHY PER-AGENT AND NOT A SCALAR (the decision this function exists to record).
    One multi-shard call can legitimately mix layers: agent A's shard reads clean
    from S3 while agent B's read raises and falls back to B's local mirror row. A
    single scalar must collapse that, and BOTH collapses are wrong — labelling
    the whole read ``authoritative`` hides B's mirror row, which is the exact
    guard-1753 false positive this work exists to remove; labelling it all
    ``local-mirror`` degrades A's genuinely-fresh row, trading a narrow
    false-negative for a fleet-wide one. So provenance is per agent.

    AND PER-AGENT ALONE IS STILL NOT ENOUGH, which is the part that does not
    generalise from the single-shard case. The consumer (the partner_in_flight
    double-claim guard) forms a NEGATIVE over the whole peer set — "no partners
    in_flight". A peer that was never DISCOVERED has no row and therefore no
    per-agent label to inspect, yet it is precisely the measured failure: on the
    S3-list error path the roster degrades to local names only, and the local
    mirror is documented above as dropping peer shards ENTIRELY (echo/zeta
    absent locally but present and fresh on S3, cc-04 2026-07-14). Row-level
    provenance is silent about an agent it never enumerated. Hence the separate
    ``roster`` field: it answers "is this peer set complete?", which is the
    question a negative conclusion actually rests on.

    Returns ``(rows, provenance)`` where provenance is::

        {"by_agent": {agent: PROV_*}, "roster": PROV_AUTHORITATIVE | PROV_LOCAL_MIRROR}

    ``by_agent`` is TOTAL over every enumerated name (⊇ the keys of ``rows``):
      ``PROV_AUTHORITATIVE`` — the row came from the store of record: the
        own-cloud force_fresh read, OR the local file on a non-own-cloud
        deployment where that file IS the store of record and no mirror exists.
        Blanket-degrading the local-backend case would break every correct
        verdict on a local deployment — pinned by its own test, because a
        mutation proof aimed at the defect path does NOT redden that
        over-correction.
      ``PROV_LOCAL_MIRROR`` — own-cloud was in play but this agent's
        authoritative read produced nothing (backend-init error, read error, or
        an empty/non-dict document), so its row is the mirror's and is NOT
        evidence about that peer's current state.
      ``PROV_NONE`` — the agent was enumerated but no row was obtained anywhere;
        it is absent from ``rows``. Known blindness, not absence of a peer.
    """
    local = load_rows(world_dir)
    if not _is_owncloud_backend():
        # No sync layer: the local files ARE the store of record, not a mirror
        # of one. Reporting local-mirror here would be the blanket-degrade trap.
        return (local, {"by_agent": {n: PROV_AUTHORITATIVE for n in local},
                        "roster": PROV_AUTHORITATIVE})
    try:
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        from owncloud_backend import OwnCloudBackend
        be = OwnCloudBackend.from_env()
    except Exception as e:  # noqa: BLE001 — fail-open to the local mirror
        # Print str(e), not just the type. The type alone is uninformative:
        # every distinct cause here (no mappable world root, missing S3 env,
        # absent credentials, S3 error) arrives as a bare label, and the label
        # names none of them. Measured  — recovering the message the
        # exception already carried cost a full investigation, and the message
        # ("neither MIND_WORLD/WORLD_PATH nor MIND_META/META_PATH is set")
        # identified the cause on sight. Exception messages here carry env-var
        # NAMES and paths, never values (guard-724).
        print(f"[_team_state] authoritative read unavailable "
              f"({type(e).__name__}: {e}); using local mirror", file=sys.stderr)
        return (local, {"by_agent": {n: PROV_LOCAL_MIRROR for n in local},
                        "roster": PROV_LOCAL_MIRROR})
    d = rows_dir(world_dir)
    # Roster from S3 unioned with local: discovers peers whose shard the local
    # mirror never pulled (echo/zeta on cc-04) which load_rows cannot see.
    names = set(local)
    roster = PROV_AUTHORITATIVE
    try:
        for n in be.list_dir(str(d)):
            n = str(n)
            if n.endswith(".yaml"):
                names.add(n[:-5])
    except Exception as e:  # noqa: BLE001 — S3 list failed; local roster stands
        print(f"[_team_state] authoritative roster list failed "
              f"({type(e).__name__}: {e}); local roster only", file=sys.stderr)
        roster = PROV_LOCAL_MIRROR
    out = dict(local)
    prov = {}
    for name in sorted(names):
        fresh = None
        try:
            doc = yaml.safe_load(be.read_text(str(d / f"{name}.yaml"),
                                              force_fresh=True))
            if isinstance(doc, dict) and doc:
                fresh = doc
        except Exception as e:  # noqa: BLE001 — keep the local row for this shard
            print(f"[_team_state] authoritative read of row {name!r} failed "
                  f"({type(e).__name__}); keeping local row", file=sys.stderr)
        if fresh is not None:
            out[name] = fresh
            prov[name] = PROV_AUTHORITATIVE
        elif name in out:
            prov[name] = PROV_LOCAL_MIRROR
        else:
            prov[name] = PROV_NONE
    return (out, {"by_agent": prov, "roster": roster})


def read_shard_authoritative(world_dir, agent: str):
    """ONE agent's team-state shard, read FRESH from the authoritative store.

    Single-shard sibling of ``load_rows_authoritative`` (g-115-2188) with the
    same guard-980 reasoning, for a caller that needs exactly one peer's row and
    must not pay an S3 list + N shard reads to get it. ``liveness_check`` is that
    caller: it probes one stale agent at a time and memoizes per process.

    SHARED PRIMITIVE (g-306-132 fix set A). The distinction it exists to serve:
    the shard OBJECT's write time answers "did something on that box write this
    shard" — BODY activity — while ``last_active`` INSIDE the shard answers "is
    the mind still running" (g-306-132-e). Under the Mind/Body split those came
    apart: a worker Body writing the shard refreshes the object while the reducer
    is dead. Any consumer needing mind-level freshness must read the VALUE from
    here, never the object's LastModified.

    Returns the row dict, or None when no row can be read anywhere. Fail-open at
    every layer — local backend, backend-init error, or a read error each degrade
    to the LOCAL mirror row — so the result is never worse than a plain local
    read.

    THE BARE ROW CANNOT SAY WHICH LAYER PRODUCED IT (guard-1753). A caller that
    needs to know — because promoting a mirror value to an authoritative verdict
    would be a false positive — must call
    ``read_shard_authoritative_with_provenance`` instead. This wrapper exists so
    the pre-g-306-138 contract stays byte-identical for callers that legitimately
    do not care (``aspirations_write``'s claim-holder probe, which fails safe on
    a stale row either way).
    """
    return read_shard_authoritative_with_provenance(world_dir, agent)[0]


def read_shard_authoritative_with_provenance(world_dir, agent):
    """``(row, provenance)`` — the provenance-carrying form of the read above.

    Added by g-306-138. ``read_shard_authoritative`` fails open to the local
    mirror at three layers and returns a bare dict, so no caller could tell an
    authoritative read from a fallback. ``liveness_check`` then promoted a
    mirror value to verdict=alive on signal=authoritative_last_active, with a
    reason string asserting "the local mirror lagged" about a value read FROM
    that mirror — a false ALIVE on any transient store error against an agent
    whose mirror was pulled recently but has since died.

    guard-1753 is the general rule: a fail-open reader must return a verdict
    that DISTINGUISHES "could not reach the target" from "reached it and found
    nothing", or its own blindness is unobservable to every caller.

    provenance is one of:
      ``PROV_AUTHORITATIVE`` — the row came from the store of record: either the
        own-cloud force_fresh read, OR the local file on a non-own-cloud
        deployment, where that file IS the store of record and no mirror exists.
        This distinction is load-bearing: treating the local-backend read as a
        "mirror" would degrade every correct verdict on a local deployment.
      ``PROV_LOCAL_MIRROR`` — own-cloud was in play but the authoritative read
        did not produce a row (backend-init error, read error, or an empty /
        non-dict document), so the row is the local mirror's and is NOT evidence
        about the peer's current state.
      ``PROV_NONE`` — no row anywhere; ``row`` is None.
    """
    p = row_path(world_dir, agent)

    def _local():
        try:
            with open(p, "r", encoding="utf-8") as fh:
                doc = yaml.safe_load(fh)
            return doc if isinstance(doc, dict) and doc else None
        except Exception:  # noqa: BLE001 — absent/unreadable shard
            return None

    if not _is_owncloud_backend():
        # No sync layer: the local file is the store of record, not a mirror of one.
        row = _local()
        return (row, PROV_AUTHORITATIVE if row else PROV_NONE)
    try:
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        from owncloud_backend import OwnCloudBackend
        be = OwnCloudBackend.from_env()
        doc = yaml.safe_load(be.read_text(str(p), force_fresh=True))
        if isinstance(doc, dict) and doc:
            return (doc, PROV_AUTHORITATIVE)
    except Exception as e:  # noqa: BLE001 — fail-open to the local mirror
        print(f"[_team_state] authoritative shard read of {agent!r} failed "
              f"({type(e).__name__}: {e}); using local mirror", file=sys.stderr)
    row = _local()
    return (row, PROV_LOCAL_MIRROR if row else PROV_NONE)


def row_agent_names(world_dir) -> tuple:
    """Row-file stems, local ∪ backend (: local-only stems miss every
    sibling on push-only-mirror boxes). Backend piece rides the _backend_rows
    TTL cache, so the parse cost is shared with load_rows."""
    d = rows_dir(world_dir)
    try:
        local = {p.stem for p in d.iterdir()
                 if p.suffix == ".yaml" and p.is_file()}
    except OSError:
        local = set()
    return tuple(sorted(local | set(_backend_rows(world_dir))))


def _entry_ts(entry) -> str:
    """Newest-wins timestamp of one agent-status snapshot. ISO-8601 local
    strings compare correctly as strings. Missing timestamps sort oldest."""
    if not isinstance(entry, dict):
        return ""
    vals = [str(entry.get(k)) for k in ("last_active", ROW_UPDATED_KEY)
            if entry.get(k)]
    return max(vals) if vals else ""


def _is_retired(entry) -> bool:
    """Retirement tombstone check (). A retired row (an agent whose
    container/identity was decommissioned, e.g. the charlie+delta→foxtrot
    merge leftovers) is dropped from the composed roster INSTEAD of deleted —
    shard deletion needs s3:DeleteObject rights fleet boxes don't hold, and a
    tombstone preserves the audit trail. Self-healing revival: a heartbeat
    NEWER than retired_at wins (a revived agent's first stamp re-enters the
    roster without anyone having to clear the sticky retired flag)."""
    if not isinstance(entry, dict) or not entry.get("retired"):
        return False
    retired_at = str(entry.get("retired_at") or "")
    last_active = str(entry.get("last_active") or "")
    return not (last_active and retired_at and last_active > retired_at)


SRC_CORE = "core"
SRC_ROW = "row"


def _compose_picks(core_status: dict, rows: dict):
    """Shared pick engine behind compose_agent_status and its _with_sources
    sibling: yields ``(name, pick, source)`` for every non-retired composed row.

    Exists so the source LABEL can never drift from the pick it describes
    (g-306-179). Duplicating the newest-wins tie-break in a second function is
    exactly how that drift happens, and a consumer keying provenance off a
    stale label reports a verified clear it did not earn.
    """
    core_status = core_status if isinstance(core_status, dict) else {}
    for name in sorted(set(core_status) | set(rows)):
        core_entry = core_status.get(name)
        row_entry = rows.get(name)
        if row_entry is None:
            pick, source = core_entry, SRC_CORE
        elif core_entry is None:
            pick, source = row_entry, SRC_ROW
        elif _entry_ts(core_entry) > _entry_ts(row_entry):
            pick, source = core_entry, SRC_CORE
        else:
            pick, source = row_entry, SRC_ROW
        if _is_retired(pick):
            continue
        yield name, pick, source


def compose_agent_status(core_status: dict, rows: dict) -> dict:
    """Merge core-file residual rows with row-file rows: per-agent WHOLE-ROW
    newest-wins, row file winning ties (mirrors
    coordination_merge._merge_agent_status side-pick semantics). Retired rows
    (see _is_retired) are dropped from the composed view. Keys sorted for
    deterministic output."""
    return {name: pick for name, pick, _ in _compose_picks(core_status, rows)}


def compose_agent_status_with_sources(core_status: dict, rows: dict):
    """compose_agent_status, plus WHICH store each composed value came from.

    Returns ``(composed, sources)`` where ``sources[name]`` is ``SRC_CORE`` or
    ``SRC_ROW``, total over the composed keys. A caller reasoning about the
    provenance of the composed VIEW needs this: a value picked from the
    monolithic core file was read without any authoritative-store refresh, so
    its shard's provenance label (or the absence of one) says nothing about the
    bytes actually in play. See g-306-179.
    """
    out: dict = {}
    sources: dict = {}
    for name, pick, source in _compose_picks(core_status, rows):
        out[name] = pick
        sources[name] = source
    return out, sources


def compose_state(state: dict, world_dir) -> dict:
    """Overlay row files onto a loaded core team-state document (in place).
    Also lifts last_updated/last_updated_by to the newest stamp across core
    + rows, so liveness dashboards keep working without core-file churn."""
    rows = load_rows(world_dir)
    # Only materialize the agent_status key when the core doc carries it or
    # rows exist — callers rely on `{} stays {}` for their fail-open
    # truthiness contracts (goal-selector rb-2429, status.py dict contract).
    if rows or "agent_status" in state:
        state["agent_status"] = compose_agent_status(state.get("agent_status") or {}, rows)
    best_ts = str(state.get("last_updated") or "")
    best_by = state.get("last_updated_by")
    for name, row in rows.items():
        ts = _entry_ts(row)
        if ts and ts > best_ts:
            best_ts = ts
            best_by = row.get(ROW_UPDATED_BY_KEY) or name
    if best_ts:
        state["last_updated"] = best_ts
        state["last_updated_by"] = best_by
    return state


def route_field(field: str):
    """Classify a dot-path write target.

    Returns ("row", <agent>, <subpath>) for agent_status.<name>[.<rest>]
    (subpath "" means whole-row), else ("core", None, field). The bare
    "agent_status" map has no live writers (read-only call sites) — it
    routes core so a hypothetical caller degrades to the legacy behavior
    instead of silently fanning out."""
    parts = (field or "").split(".")
    if len(parts) >= 2 and parts[0] == "agent_status" and parts[1]:
        return "row", parts[1], ".".join(parts[2:])
    return "core", None, field


def stamp_row_metadata(row: dict, author: str, now: str) -> dict:
    row[ROW_UPDATED_KEY] = now
    row[ROW_UPDATED_BY_KEY] = author
    return row


def make_clear_in_flight_modifier(agent_author: str, now_fn=None,
                                  if_goal: str = None, status: dict = None):
    """Build the `locked_modify_yaml` modifier that clears a row's in_flight.

    SHARED BY BOTH TWINS ON PURPOSE (guard-2323 / guard-547). `team-state.py`
    cmd_clear_in_flight and `mind_api/src/world/team_state_write.py`
    clear_in_flight both already `from _team_state import ...`, so guard-547's
    "extract to a shared module" branch is available here and the hand-mirrored
    copies it warns about are not needed: one implementation cannot drift from
    itself.

    `if_goal` is the COMPARE-AND-SWAP (guard-2474 clause 2, g-306-137). Without
    it, an ownership test performed by the CALLER is evaluated against a
    snapshot while the clear acts on live state — a check-then-act, not a guard.
    Because this modifier runs INSIDE `locked_modify_yaml`'s lock (which reads
    within the lock that guards the write, and on own-cloud force-refreshes
    first), comparing here is atomic against a concurrent
    `POST /v1/team-state/in-flight` on the same row file: the competing writer
    either lands fully before our read or blocks until our write completes.

    When `if_goal` is None the behavior is exactly the pre-g-306-137 one — an
    unconditional clear — so existing callers that legitimately want "clear
    whatever is there" (recovery, retire, manual release) are unchanged.

    `status` is an optional caller-owned dict, populated in place so the caller
    can report the outcome without a second read.

    THE CONTRACT IN ONE SENTENCE (guard-1433 step 2): `row_survived` is the
    only key that answers "is an in_flight row still standing after this call?",
    and it is the key a reporter must switch on before saying "already absent".

        cleared=True,  skipped=None,  row_survived=False -> was ours, now gone
        cleared=False, skipped=None,  row_survived=False -> nothing was there
                                                            (key absent, OR an
                                                            explicit null — see
                                                            below)
        cleared=False, skipped="g-x", row_survived=True  -> a row for g-x; the
                                                            CAS declined it
        cleared=False, skipped=None,  row_survived=True  -> a row WAS there but
                                                            carried no
                                                            comparable goal_id,
                                                            so the CAS declined
                                                            it unverified

    THE LAST ROW IS WHY `row_survived` EXISTS (g-306-171). Before it, that case
    and "nothing was there" were BOTH (cleared=False, skipped_goal_id=None), so
    no consumer could tell them apart and all four reporters fell through to
    "in_flight already absent" — asserting a row was gone while it was still
    standing, for 3 of 7 measured body shapes (string / dict-without-goal_id /
    dict-with-goal_id-None). guard-2223: the third state gets its own verdict
    rather than being folded into either pole. Read it with `is True` /
    `is False`, never bare truthiness (guard-1433) — and note the reporters must
    check it BEFORE the skipped_goal_id branch, since skipped_goal_id is None on
    the unverifiable path too.

    SURVIVAL IS `in_flight is not None`, NOT key presence. The goal that filed
    this fix measured the defect at 4 of 7 shapes using key-presence; that
    over-counted by one, and the number is corrected here rather than preserved.
    An explicit `in_flight: null` is genuinely absent — the success path POPS
    the key rather than nulling it, so this code cannot even produce that shape,
    and every other reader treats null as absent. Calling it a survivor would
    trade the old false "absent" for a new false "survivor".

    `status` reflects the LAST invocation only. The modifier is re-invoked on a
    backend conflict retry, and it resets both keys on entry, so the dict always
    describes the attempt that actually wrote — never an accumulation across
    attempts (g-306-163).

    ABSENT AND BLANK-BUT-SUPPLIED ARE DIFFERENT REQUESTS (g-306-170). `if_goal`
    is normalized HERE, once, at the shared boundary — not in the callers. The
    docstring above says "one implementation cannot drift from itself"; that was
    true of the COMPARE and false of the NORMALIZATION, which used to sit ABOVE
    this boundary and was written twice, differently:

        mind_api/src/world/team_state_write.py  (ctx.query.get(...) or '').strip() or None
        core/scripts/team-state.py              getattr(args, 'if_goal', None)

    So `--if-goal '  '` via the CLI PRESERVED a live row (the CAS declined) while
    `?if_goal=%20%20` via the daemon DESTROYED it — same intent, opposite
    outcomes. Measured 2026-08-05 (alpha, cc-04, Linux 6.8.0-136-generic) against
    the two caller normalizations over a row holding a sibling goal:

        raw input      daemon       CLI
        'g-306-167'    preserved    preserved
        ''             DESTROYED    preserved
        '   '          DESTROYED    preserved
        None           cleared      cleared     <- the intended unconditional path

    `or None` is what converts a GUARD request into a WIPE: it collapses
    blank-but-supplied into absent, and absent means "clear whatever is there."
    A caller that passes `if_goal` at all is ASKING for a compare-and-swap, so a
    value that is blank after stripping is a caller bug and RAISES — it must
    never silently select the unguarded path. A caller that OMITS the parameter
    (or passes None) keeps the unconditional clear, which is what the
    recovery / retire / release callers deliberately rely on.

    Stripping alone would NOT have been enough, and doing only that would have
    been worse than doing nothing: it turns '  ' into None into an unconditional
    wipe on BOTH paths, making the twins agree on the WRONG answer and removing
    the CLI's accidentally-safe behavior (rb-6568).

    Raising at FACTORY time rather than inside `_row_modifier` is deliberate —
    it fails before `locked_modify_yaml` takes the lock, so a caller bug cannot
    cost a lock acquisition or a backend round-trip.
    """
    if if_goal is not None:
        if not isinstance(if_goal, str):
            raise ValueError(
                "if_goal must be a string goal-id or omitted; got "
                f"{type(if_goal).__name__}")
        _stripped = if_goal.strip()
        if not _stripped:
            raise ValueError(
                "if_goal was supplied but is blank after stripping. A supplied "
                "if_goal requests a compare-and-swap; treating it as absent "
                "would silently perform an UNCONDITIONAL clear and destroy a "
                "live row. Omit the parameter entirely to clear unconditionally "
                "(g-306-170).")
        if_goal = _stripped
    if status is None:
        status = {}
    status.setdefault("cleared", False)
    status.setdefault("skipped_goal_id", None)
    status.setdefault("row_survived", False)
    _now = now_fn or (lambda: datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))

    def _row_modifier(row):
        # RESET PER INVOCATION, not per factory call (). This modifier
        # is re-invoked on an own-cloud If-Match conflict: locked_modify_yaml
        # runs its whole refresh->read->modify->write cycle inside
        # _rmw_with_conflict_retry, whose contract is that the cycle "MUST
        # re-read fresh each call" — so the SAME modifier object sees a second,
        # fresher row. Seeding these keys once at factory time let a losing
        # attempt's verdict survive into the winning one, in BOTH directions:
        # decline-then-clear left skipped_goal_id set on a row that WAS cleared,
        # and clear-then-decline left cleared=True on a row that was NOT. The
        # two consumers read this dict with opposite precedence (the worker
        # checks skipped_goal_id first, the CLI checks cleared first), so each
        # misreported a different case. Only the reported verdict was ever
        # wrong — the last invocation determines the written row, so the
        # mutation was always correct.
        status["cleared"] = False
        status["skipped_goal_id"] = None
        status["row_survived"] = False
        if not isinstance(row, dict):
            row = {}
        if "in_flight" not in row:
            # Nothing to clear — return unchanged. locked_modify_yaml still
            # re-writes the row (harmless yaml round-trip), but skipping the
            # metadata stamp keeps timestamps still on a no-op call.
            return row
        current = row.get("in_flight")
        if if_goal is not None:
            # A CAS was requested, so an unverifiable row must NOT be cleared.
            # A non-dict here (hand-edit, partial write, `in_flight:` with no
            # value) carries no goal_id to compare, and guessing is the failure
            # this whole parameter exists to prevent.
            if not isinstance(current, dict) or current.get("goal_id") != if_goal:
                # The row moved between the caller's ownership check and this
                # write. Leaving it alone is the fail-safe direction: a missed
                # clear self-heals on the next sweep, a wrong clear makes a
                # live agent look idle to every partner's selector (rb-6498).
                status["skipped_goal_id"] = (current.get("goal_id")
                                             if isinstance(current, dict) else None)
                # A row is STILL STANDING. Set this on BOTH declined paths —
                # the identified one (skipped_goal_id="g-x") and the
                # unverifiable one (skipped_goal_id=None) — because the second
                # is indistinguishable from "nothing was there" without it
                # ().
                #
                # `is not None`, NOT key-presence: an explicit `in_flight: null`
                # is genuinely absent, and every other reader in the codebase
                # agrees (precheck Phase 0-pre.0 gates on `in_flight is
                # non-null`). Reporting a survivor there would be a FALSE alarm
                # on the one shape a clear cannot even produce — the success
                # path below POPS the key rather than nulling it, so a stored
                # null only ever arrives from a hand-edit, a foreign writer, or
                # a retirement tombstone (measured 2026-08-04: 2 of 7 live
                # shards, both non-live rows).
                status["row_survived"] = current is not None
                return row
        # if_goal is None -> pop on KEY PRESENCE, byte-for-byte the pre-
        # behavior. Deliberate: recovery/retire/release callers pass no if_goal
        # precisely to normalize whatever is there, including a malformed row,
        # and narrowing them to well-formed dicts would quietly stop them
        # cleaning up the rows they exist to clean.
        now = _now()
        row.pop("in_flight")
        row["last_active"] = now
        status["cleared"] = True
        return stamp_row_metadata(row, agent_author, now)

    return _row_modifier


def make_clear_body_row_modifier(agent_author: str, sid: str, now_fn=None,
                                 status: dict = None):
    """Build the `locked_modify_yaml` modifier that REMOVES one body row.

    The dedicated-op answer to g-306-186. `worker_close_in_flight_clear` used to
    clear `in_flight_bodies.<sid>` by SETTING NULL through the generic
    `POST /v1/team-state/update` field-path dispatch, because that dispatch's
    `remove` operation is list-only — on a dict key it returns early and reports
    ok:true having done nothing. The residue was one permanent null-valued key
    per SID an agent had ever run, on a shared synced store.

    WHY A DEDICATED OP RATHER THAN TEACHING `remove` ABOUT DICT KEYS. The goal
    that filed this enumerated exactly two paths and neither survived
    measurement. `_remove_nested` is not one shared primitive but a hand-mirrored
    PAIR (`mind_api/src/world/team_state_write.py` and `core/scripts/team-state.py`),
    so widening its semantics is a guard-742 two-file change with no structural
    guarantee the copies agree — and the census found ZERO production
    `--operation remove` callers, so the extension would earn no amortization
    while giving any future field string a runtime-shape-dependent delete path.
    A cadence prune was the other candidate and is worse here: shard merges are
    whole-snapshot last-writer-wins on `last_active`
    (`coordination_merge.merge_team_state_shard`), so a non-owner pruner's write
    would beat the owner's fresher state outright. This is the same fork
    g-115-1909 hit for `agent_status.<name>` row removal, and the same answer:
    ONE implementation here, imported by BOTH the CLI (`team-state.py`) and the
    daemon (`team_state_write.py`), so guard-742 parity holds by construction.
    guard-2305 states the general rule — a structured team-state field gets its
    dedicated writer, not the generic dotpath setter.

    IT BUMPS `last_active`, and that is what makes the removal DURABLE rather
    than cosmetic. `merge_team_state_shard` reconciles a both-diverged shard by
    whole-snapshot LWW on that field, so a stamped removal wins the merge and the
    key cannot be resurrected from a peer's stale copy. An unstamped pop would
    lose to any newer peer snapshot still carrying the key.

    IT ALSO DROPS NULL-VALUED SIBLINGS, which is why no separate drain mechanism
    is needed for residue that already exists: every close self-heals its own
    agent's map while it holds the lock. Only an exact `None` is swept — a
    non-dict, non-None value (hand-edit, partial write) carries content this op
    has no mandate to destroy, and every consumer skips it either way. Popping
    the whole `in_flight_bodies` key once it empties avoids leaving `{}` behind,
    which would be the same residue class one level up.

    OWNERSHIP NEEDS NO CAS, unlike the reducer clear above. The reducer row is
    keyed by AGENT NAME so a worker and its reducer contend for one row and only
    `claimed_by_sid` separates them; a body row is keyed BY THIS SID, so the key
    IS the identity and there is no other writer (guard-2474 — match identity
    granularity, which here the key already does).

    `status` is a caller-owned dict populated in place. It is RESET per
    invocation, not per factory call: `locked_modify_yaml` re-invokes the same
    modifier object on an own-cloud If-Match conflict retry, so seeding once at
    factory time would let a losing attempt's verdict survive into the winning
    one (g-306-163, learned on the sibling factory above).
    """
    if status is None:
        status = {}
    status.setdefault("removed", False)
    status.setdefault("nulls_swept", 0)
    status.setdefault("remaining", 0)
    _now = now_fn or (lambda: datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))

    def _row_modifier(row):
        status["removed"] = False
        status["nulls_swept"] = 0
        status["remaining"] = 0
        if not isinstance(row, dict):
            row = {}
        bodies = row.get("in_flight_bodies")
        if not isinstance(bodies, dict):
            return row
        removed = sid in bodies
        if removed:
            bodies.pop(sid)
        dead = [k for k, v in bodies.items() if v is None]
        for k in dead:
            bodies.pop(k)
        status["removed"] = removed
        status["nulls_swept"] = len(dead)
        status["remaining"] = len(bodies)
        if not (removed or dead):
            # Nothing changed — return unchanged so the metadata stamp does not
            # move timestamps on a no-op call (mirrors the sibling factory).
            return row
        now = _now()
        if not bodies:
            row.pop("in_flight_bodies")
        row["last_active"] = now
        return stamp_row_metadata(row, agent_author, now)

    return _row_modifier


def body_row_shard_present(world_dir, agent: str) -> bool:
    """Is there an existing shard to clear a body row from?

    `locked_modify_yaml` mkdir's the parent and writes the modifier's return
    value unconditionally, so calling it on a missing shard CREATES one — the
    guard-2611 hazard, and not hypothetical: the pre-fix null-write manufactured
    a real `no-such-agent-xyz` shard in the SHARED store from a test fixture's
    deliberately-nonexistent --agent, which then tripped a roster detector in an
    unrelated suite. Both the endpoint and the CLI ask this BEFORE writing, so
    the clear is a genuine no-op for a non-resident agent instead of a creation.

    Shared rather than mirrored as a `.exists()` on each side, for the same
    guard-742 reason the modifier is shared: two copies of a predicate are two
    things that can drift.

    A missing shard means there is nothing to clear even when the agent is real
    and simply has no row yet — the fail-safe direction either way, since
    declining to write is always recoverable and a phantom row is not.

    LOCAL ABSENCE IS NOT ABSENCE on own-cloud (guard-980). This box's tree is a
    read-through cache and the shard mirror sweep is PUSH-ONLY, so a partner's
    shard written on another box is never in this box's rows dir until something
    pulls it. A bare `.exists()` therefore answered False for a shard that plainly
    exists in the store, and `clear-body-row --agent <partner>` returned
    `{"ok": true, "no_shard": true}` having done nothing — a positive-looking
    response for work that did not happen (g-306-192, from bravo's fresh-eyes pass
    on g-306-186). The bug is invisible from the sole production caller, which
    passes agent=self and always has its own shard locally; the exposed surface is
    the CLI's `--agent`, whose whole purpose is cross-agent drain.

    So materialize BEFORE asking. This is the same fix, for the same reason, that
    `team_state_write.py` applies to team-state.yaml ahead of its own exists()
    gate; the init endpoint learned this and the clear endpoint had not.

    It does NOT weaken guard-2611. `ensure_local` materializes only an object that
    ALREADY exists remotely — it is identity on LocalBackend and a download
    elsewhere — so a genuinely absent shard stays absent and the no-create pin
    (`test_clear_body_row_refuses_to_create_a_shard`) still holds. Best-effort by
    construction: a backend hiccup must degrade to the old local-only answer, never
    raise, because this predicate gates a write and crashing here would be worse
    than answering conservatively.
    """
    try:
        p = Path(row_path(world_dir, agent))
    except (OSError, ValueError):
        return False
    try:
        from storage_backend import get_backend  # lazy — import-cycle-proof

        get_backend().ensure_local(p)
    except Exception as e:
        try:  # report, never raise — see note_swallowed_backend_error ()
            from storage_backend import note_swallowed_backend_error
            note_swallowed_backend_error("ensure_local", p, e)
        except Exception:
            pass
    try:
        return p.exists()
    except OSError:
        return False


def core_residual(core_path, agent: str) -> dict:
    """One agent's entry from the CORE file's agent_status — the lazy-
    migration seed (passed as ``initial`` so the first row write starts from
    the pre-shard snapshot instead of {}). Read outside the row lock:
    seed-once semantics make a marginally stale seed harmless (the write
    that triggered seeding immediately overwrites the fields it targets)."""
    try:
        with open(core_path, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
        entry = (doc.get("agent_status") or {}).get(agent)
        return dict(entry) if isinstance(entry, dict) else {}
    except Exception:  # noqa: BLE001 — missing/corrupt core just means empty seed
        return {}


def read_agent_row(world_dir, agent: str, core_path=None) -> dict:
    """One agent's current status: row file first, core residual fallback,
    newest-wins when both exist (same comparison as compose_agent_status).
    For hot single-agent lookups (in_flight inference) — avoids loading
    every row."""
    row_entry: dict = {}
    try:
        p = row_path(world_dir, agent)
    except ValueError:
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        if isinstance(doc, dict):
            row_entry = doc
    except Exception:  # noqa: BLE001 — absent/corrupt row falls back to core
        row_entry = {}
    core_entry = core_residual(core_path, agent) if core_path is not None else {}
    if not core_entry:
        return row_entry
    if not row_entry:
        return core_entry
    return core_entry if _entry_ts(core_entry) > _entry_ts(row_entry) else row_entry


# ---------------------------------------------------------------------------
# Agent-row retirement () — the sanctioned REMOVE path.
#
# Signal gap  root-caused: team-state had NO way to remove an
# agent_status ROW. route_field sends agent_status.<name> to a per-agent
# SHARD (targeting a non-existent shard for un-sharded legacy agents);
# the generic --operation remove / _remove_nested only drops items from a
# LIST (agent_status is a DICT -> no-op); the daemon whole-row path refuses
# remove. So a user-retired agent's core-residual row could only be removed
# by forbidden raw-YAML surgery. retire_agent is that missing op — one place,
# imported by BOTH the CLI (team-state.py) and the daemon (team_state_write.py),
# so guard-742 parity holds by construction (no second implementation).
# ---------------------------------------------------------------------------

def enumerate_retire(world_dir, core_path, agent: str) -> dict:
    """Pure read: what retire_agent WOULD remove for <agent> — the core-file
    agent_status.<agent> residual and the per-agent shard (path + content).
    `present` is False when neither exists (retire is then an idempotent
    no-op). No mutation — the enumeration is also the integrity baseline the
    archive is verified against (archive-before-delete.md step 1)."""
    name = (agent or "").strip()
    core = core_residual(core_path, name) if core_path is not None else {}
    rp = None
    row_content = None
    try:
        rp = row_path(world_dir, name)
    except ValueError:
        rp = None
    row_exists = rp is not None and rp.exists()
    if row_exists:
        try:
            with open(rp, "r", encoding="utf-8") as f:
                doc = yaml.safe_load(f)
            if isinstance(doc, dict):
                row_content = doc
        except Exception:  # noqa: BLE001 — corrupt row still archived as None
            row_content = None
    return {
        "agent": name,
        "core_residual": core or None,
        "row_path": str(rp) if rp is not None else None,
        "row_content": row_content,
        "present": bool(core) or row_exists,
    }


def enumerate_belief_sweep(world_dir, core_path, retiree: str) -> dict:
    """Pure read: every live agent (core-resident agent_status row OR per-agent
    shard) holding a Theory-of-Mind belief whose `about` == retiree. Returns
    {"core": {holder: [belief, ...]}, "shards": {holder: [belief, ...]}}.

    Beliefs about a retiree are DEAD: the subject produces no further
    observations, so the holder's own supersede_beliefs hygiene (which fires
    only on a FRESH observation of the subject) never replaces them.
    Retirement must sweep them or they linger forever — the echo/zeta
    about:delta residue that survived the g-115-1965 row removal (g-115-2043,
    rb-3104 lineage). Enumeration is also the archive baseline
    (archive-before-delete.md step 1). No mutation."""
    name = (retiree or "").strip()
    out = {"core": {}, "shards": {}}
    if not name:
        return out

    def _hits(beliefs):
        return [b for b in (beliefs or [])
                if isinstance(b, dict) and str(b.get("about", "")).strip() == name]

    # core-resident beliefs (agent_status.<holder>.beliefs — legacy/un-sharded)
    if core_path is not None:
        try:
            with open(core_path, "r", encoding="utf-8") as f:
                doc = yaml.safe_load(f)
            ast = (doc or {}).get("agent_status") or {}
            for holder, row in ast.items():
                if holder == name or not isinstance(row, dict):
                    continue
                h = _hits(row.get("beliefs"))
                if h:
                    out["core"][holder] = h
        except Exception:  # noqa: BLE001 — a corrupt core file yields no sweep
            pass

    # shard-resident beliefs (per-agent shard, top-level `beliefs`)
    rdir = rows_dir(world_dir)
    if rdir.is_dir():
        for shard in sorted(rdir.glob("*.yaml")):
            holder = shard.stem
            if holder == name:
                continue
            try:
                with open(shard, "r", encoding="utf-8") as f:
                    doc = yaml.safe_load(f)
            except Exception:  # noqa: BLE001 — corrupt shard skipped
                continue
            if not isinstance(doc, dict):
                continue
            h = _hits(doc.get("beliefs"))
            if h:
                out["shards"][holder] = h
    return out


def retire_agent(world_dir, core_path, agent, author, now, *,
                 source=None, dry_run=False) -> dict:
    """Sanctioned removal of an agent's team-state presence: the core-file
    agent_status.<agent> residual AND the per-agent shard file, gated by
    archive-before-delete (.claude/rules/archive-before-delete.md).

    Protocol: ENUMERATE -> ARCHIVE (to world/team-state/.graveyard/, a path
    the live system never reads) -> VERIFY the archive against the
    enumeration -> DELETE (pop the core key under lock + unlink the shard)
    -> RECEIPT (the archive file IS the receipt). NEVER deletes on an
    unverified archive — raises RuntimeError instead.

    Idempotent: a re-run when the agent is already absent returns
    removed=False, reason="not_present" without error. dry_run reports the
    enumeration without writing or deleting. Returns a JSON-serializable
    result dict."""
    name = (agent or "").strip()
    if not name or name in (".", "..") or any(c in name for c in "/\\"):
        raise ValueError(f"invalid agent name for retire: {agent!r}")

    plan = enumerate_retire(world_dir, core_path, name)
    # : beliefs ABOUT the retiree are held by OTHER agents and persist
    # independently of the retiree's own row — enumerate them so retirement (or a
    # re-run after the row is already gone) sweeps the dead beliefs too.
    sweep = enumerate_belief_sweep(world_dir, core_path, name)
    has_beliefs = bool(sweep["core"] or sweep["shards"])
    if not plan["present"] and not has_beliefs:
        return {"ok": True, "agent": name, "removed": False,
                "reason": "not_present",
                "detail": f"{name} has no core residual, no shard, and no "
                          f"partner-beliefs — nothing to retire"}

    # Already-tombstoned no-op (). Before the tombstone became the
    # mechanism, a completed retire left NOTHING behind, so a re-run hit
    # "not_present" above and was free. Now the shard SURVIVES by design, so
    # without this guard every re-run would archive again and re-stamp
    # retired_at — turning an idempotent no-op into an accumulating writer.
    # Deliberately narrow: it fires only when the tombstone is the ONLY thing
    # left. A core residual or a live partner-belief about the retiree is real
    # un-swept residue and must still be cleaned, so those fall through.
    # _is_retired (not a bare `retired` check) keeps revival honest: an agent
    # whose heartbeat is NEWER than its tombstone is genuinely back, and
    # re-retiring it is the correct action rather than a no-op.
    if (plan["core_residual"] is None and not has_beliefs
            and _is_retired(plan["row_content"])):
        return {"ok": True, "agent": name, "removed": False,
                "reason": "already_retired",
                "tombstoned": True,
                "detail": f"{name} is already tombstoned "
                          f"(retired_at={(plan['row_content'] or {}).get('retired_at')}) "
                          f"and has no core residual or partner-beliefs left to sweep"}

    if dry_run:
        return {"ok": True, "agent": name, "removed": False, "dry_run": True,
                "would_remove": {
                    "core_residual": plan["core_residual"] is not None,
                    "shard": plan["row_content"] is not None,
                    "beliefs": {
                        "core": {h: len(bs) for h, bs in sweep["core"].items()},
                        "shards": {h: len(bs) for h, bs in sweep["shards"].items()}}},
                "plan": plan}

    # --- ARCHIVE (before any delete) ---------------------------------------
    payload = {
        "schema": "team-state-retire-archive/v1",
        "agent": name,
        "retired_at": now,
        "retired_by": author,
        "source": source,
        "core_residual": plan["core_residual"],
        "shard_content": plan["row_content"],
        "shard_path": plan["row_path"],
        "swept_beliefs": sweep,
        "restore": (
            f"Re-add the core residual via `team-state-update.sh --field "
            f"agent_status.{name} --value '<core_residual json>' --operation "
            f"set`; re-create the shard by writing shard_content to "
            f"{plan['row_path']}. Do NOT restore into a live session's row "
            f"(archive-before-delete.md — resurrection risk)."),
    }
    gyard = graveyard_dir(world_dir)
    gyard.mkdir(parents=True, exist_ok=True)
    safe_ts = now.replace(":", "").replace("-", "").replace("T", "-")
    archive_path = gyard / f"{safe_ts}-{name}.yaml"
    with open(archive_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, sort_keys=False, allow_unicode=True)

    # --- VERIFY the archive (re-read, field-compare) -----------------------
    with open(archive_path, "r", encoding="utf-8") as f:
        verify = yaml.safe_load(f)
    if (not isinstance(verify, dict)
            or verify.get("agent") != name
            or verify.get("core_residual") != plan["core_residual"]
            or verify.get("shard_content") != plan["row_content"]
            or verify.get("swept_beliefs") != sweep):
        raise RuntimeError(
            f"retire {name}: archive verification FAILED at {archive_path} "
            f"— refusing to delete (archive-before-delete.md)")

    # --- DELETE (only now that the archive is verified) --------------------
    removed_core = False
    removed_shard = False
    beliefs_swept = {"core": {}, "shards": {}}
    from _fileops import locked_modify_yaml  # lazy: cycle-proof

    # Core file: pop the retiree's own row AND sweep every OTHER core-resident
    # row's beliefs about the retiree — one locked read-modify-write so the sweep
    # is atomic with the row removal. The single-writer invariant on
    # agent_status.<self>.beliefs is a CONCURRENT-write guard; a retiree-belief is
    # dead (never re-written by its holder, whose supersede fires only on a fresh
    # observation of a now-gone subject), so this administrative sweep under the
    # shared core lock is the sanctioned exception — same class as popping the row.
    if plan["core_residual"] is not None or sweep["core"]:
        def _modify_core(state):
            ast = state.get("agent_status")
            if isinstance(ast, dict):
                if name in ast:
                    ast.pop(name)
                for holder, row in ast.items():
                    if not isinstance(row, dict):
                        continue
                    bl = row.get("beliefs")
                    if not isinstance(bl, list):
                        continue
                    kept = [b for b in bl
                            if not (isinstance(b, dict)
                                    and str(b.get("about", "")).strip() == name)]
                    if len(kept) != len(bl):
                        row["beliefs"] = kept
                        beliefs_swept["core"][holder] = len(bl) - len(kept)
            state["last_updated"] = now
            state["last_updated_by"] = author
            return state

        locked_modify_yaml(Path(core_path), _modify_core,
                           initial={"agent_status": {}})
        removed_core = plan["core_residual"] is not None

    # Shard files: sweep each holder's top-level beliefs about the retiree under
    # that shard's own lock (serialized with the holder's live writes).
    for holder in sweep["shards"]:
        sp = row_path(world_dir, holder)

        def _modify_shard(state, _name=name, _holder=holder):
            bl = state.get("beliefs")
            if isinstance(bl, list):
                kept = [b for b in bl
                        if not (isinstance(b, dict)
                                and str(b.get("about", "")).strip() == _name)]
                if len(kept) != len(bl):
                    state["beliefs"] = kept
                    beliefs_swept["shards"][_holder] = len(bl) - len(kept)
            return state

        locked_modify_yaml(sp, _modify_shard, initial={})

    # Retiree's own shard: TOMBSTONE is the mechanism; unlink is best-effort.
    #
    # . This used to be an unlink and nothing else, which does not
    # remove on a read-through backend: the local file is deleted, the backing
    # object survives (fleet boxes do not hold s3:DeleteObject), and the next
    # read re-materializes the shard UN-tombstoned. Measured end-to-end on
    # own-cloud 2026-07-31: retire returned ok:true removed:true, and minutes
    # later the row was back and live. A direct local write of the tombstone was
    # ALSO clobbered by the same read-through — only locked_modify_yaml (the
    # governed write path) made it stick.
    #
    # _is_retired (above) has read `retired`/`retired_at` since , and
    # its docstring states the design verbatim: a retired row is "dropped from
    # the composed roster INSTEAD of deleted ... a tombstone preserves the audit
    # trail". Nothing ever wrote those fields — a consumer with no producer, so
    # both tombstones in this world were hand-improvised by agents mid-incident.
    # This is that missing producer.
    #
    # Ordering is load-bearing: tombstone FIRST (through the governed path, so it
    # reaches the backing store), then unlink. On a backend where delete works the
    # shard is gone; where it does not, what survives is the TOMBSTONED row, which
    # compose_agent_status drops. Both paths converge on "absent from the roster",
    # which the unlink alone could not guarantee.
    tombstoned = False
    # Guard on row_CONTENT, not row_path: enumerate_retire returns a row_path for
    # every valid agent name whether or not a shard exists, so gating on the path
    # would MATERIALIZE a tombstone shard for a core-only retiree that never had
    # one. Same condition the unlink used before this change.
    if plan["row_content"] is not None and plan["row_path"]:
        sp = Path(plan["row_path"])

        def _tombstone(state):
            state["retired"] = True
            state["retired_at"] = now
            state["retired_by"] = author
            return state

        locked_modify_yaml(sp, _tombstone, initial={})
        tombstoned = True

        # Best-effort ONLY — never the mechanism, never fatal. A backend that
        # denies delete is the expected case, not an error.
        try:
            sp.unlink()
            removed_shard = True
        except (FileNotFoundError, OSError):  # noqa: PERF203 — best-effort by design
            removed_shard = False

    any_removed = (removed_core or removed_shard or tombstoned
                   or bool(beliefs_swept["core"] or beliefs_swept["shards"]))
    return {"ok": True, "agent": name, "removed": any_removed,
            "removed_core_residual": removed_core,
            "removed_shard": removed_shard,
            "tombstoned": tombstoned,
            "beliefs_swept": beliefs_swept,
            "archive": str(archive_path), "source": source}
