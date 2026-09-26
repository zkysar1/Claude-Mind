#!/usr/bin/env python3
"""session/-rooted carrier for LOAD-BEARING worker captures ().

WHY THIS EXISTS. capture_fast_lane (g-306-293) reads every Body's
`agents/<agent>/sessions/<unitKey>/working-memory.yaml`, and for a Body on
ANOTHER box that read can never succeed: `sessions` is in
`owncloud_sync._EXCLUDE_DIRS` and `OwnCloudBackend._machine_local`, so a per-op
backend read/write never touches the store. Measured 2026-08-16 (alpha worker
Body d1aec55b on cc-07, `uname -r` 6.8.0-137-generic, own-cloud; live reducer on
cc-04): this Body's `working-memory.yaml` and `body-manifest.yaml` both report
`_machine_local=True`, and the store's ENTIRE `sessions/` listing for alpha held
one unit key — not this one — while the Body held 107 flagged entries. So the
lane could only ever see Bodies on the reducer's own box, which is precisely the
case it was NOT built for.

THE ASYMMETRY THIS ORIGINALLY EXPLOITED, AND WHY IT WAS NOT ENOUGH. `sessions`
(plural, per-Body) is sync-excluded; `session` (singular, agent-wide) is NOT, so
the carrier was first rooted at `agents/<agent>/session/pending-body-merges/`.
That reasoning was correct about SYNC and blind to a SECOND, independent guard:
the own-cloud claim fence refuses EVERY write under `agents/<agent>/` from a box
that does not hold the live runner claim, and a worker Body never holds it. The
destination was therefore syncable in principle and unwritable in practice, and
`push()` raised `NoClaimError` on every append from every non-reducer box.

** THE POSITIVE CONTROL THIS DOCSTRING USED TO CITE IS FALSE — do not restore
it. ** It read: "verified with the syncable `session/body-heartbeat-<SID>.json`
carrier as the positive control (`_machine_local=False`), so a `session/`-rooted
file reaches the store from any box." `_machine_local=False` is true and proves
only that SYNC would not prune it; it says nothing about the claim fence, which
is the guard that actually decides. Measured on cc-09 (alpha worker Body, SID
2fda1f3e, `uname -r` 6.8.0-138-generic, own-cloud, 2026-09-03): that heartbeat
carrier is 148 bytes locally and its S3 key is ABSENT
(`read_authoritative_bytes` -> FileNotFoundError), exactly like the fastlane
carrier's 151,390 bytes beside it. The cited control was itself stranded. A
`_machine_local` check is not a delivery check; only an authoritative read is.
(That heartbeat lane is a SEPARATE defect from this one — it belongs to the
worker-liveness carrier, not to captures — and is relayed rather than fixed
here.)

WHERE IT LIVES NOW: `world/body-carriers/<agent>/` — see `carrier_dir` for the
deadlock that leaves `world/` as the only location which is both syncable and
worker-writable.

WHY NOT SIMPLY SYNC sessions/. Rejected twice (g-306-119-b, g-115-6240): it puts
a second copy of the same bytes in the store and breaks the per-box closure
record. This carrier ships ONLY the flagged entries — the ones the priority lane
exists for — and never touches Body lifecycle, which `generalize_down` still
solely owns.

VERBATIM IS LOAD-BEARING, AND IT IS THE ONE TRAP HERE. Entries are written to
the carrier exactly as appended to the WM, and the consumer copies them into the
reducer WM without stamping anything on them. Both this lane and the later full
`generalize_down` dedup by CONTENT HASH (`body-merge._dedup_append`), so a
verbatim copy is skipped by whichever runs second. Adding a `source_body` or a
`carried_at` INSIDE the entry would change its hash and manufacture exactly the
duplicate `capture_fast_lane`'s docstring warns about. Envelope metadata
therefore lives on the LINE, never inside `entry`.

WHOLE-FILE PUSH IS SELF-HEALING. Each append rewrites the WHOLE carrier to the
store rather than shipping a delta, so a push that fails (transport blip, absent
credentials) is repaired by the next successful append — no retry queue, no
reconciliation state. The residual is the LAST entry before a Body goes quiet:
if its push failed, that entry reaches the reducer only via the close-time full
merge. It loses the acceleration; it is never lost.

THE KEY HAS TWO WRITERS, SO A LOST RACE IS RETRIED IN PLACE (g-115-9852). The
daemon's own sync sweep mirrors `world/` and `body-carriers` is not excluded, so
this push races the sweep for the same key. Measured on cc-09 (2026-09-14): the
daemon log held both race shapes, `ConflictError` (412, a fence gone stale
underneath) and a raw `ConditionalRequestConflict` (409, two conditional PUTs in
flight). Waiting for the next append leaves the newest entry undelivered for as
long as the Body stays quiet, so `push` retries a lost race — and ONLY a lost
race — on a fresh fence. See `push`.

BEST-EFFORT BY CONTRACT. Every function here swallows its own failures and
returns a falsy value. This sits on the `wm append` hot path, and a carrier
problem must never fail the working-memory write that produced it — the WM is
the durable record, this is an accelerator in front of it.
"""
from __future__ import annotations

import importlib.util
import json
import random
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

CARRIER_SUFFIX = "-fastlane.jsonl"


def _bm():
    """body-manifest.py — the SOLE owner of the staged-dir/name constants.

    Loaded lazily and cached in sys.modules (the `capture_fast_lane
    ._load_hyphen_module` shape) rather than re-declaring `session` /
    `sessions` / `pending-body-merges` here. Re-declaring them would be a
    fourth copy of names that already drifted once, and the cache makes the
    cost a dict lookup on every call after the first — affordable even on the
    daemon's append path, which is why SSOT wins over inlining here.
    """
    cached = sys.modules.get("body_manifest")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        "body_manifest", SCRIPT_DIR / "body-manifest.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["body_manifest"] = mod
    spec.loader.exec_module(mod)
    return mod


def _body_merge():
    """body-merge.py — the owner of `_content_hash` and `_read_staged_bytes`.

    Same lazy, cached shape as `_bm()`. Raises on a load failure, because its
    two callers need different fallbacks: `read_carriers` returns what it has so
    far, and `verify_delivery` reports that it could not check.
    """
    cached = sys.modules.get("body_merge")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        "body_merge", SCRIPT_DIR / "body-merge.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["body_merge"] = mod
    spec.loader.exec_module(mod)
    return mod


def split_body_wm_path(wm_path):
    """(agent_dir, unit_key) when `wm_path` is a per-Body WM, else (None, None).

    The discriminator is STRUCTURAL — `.../<agents_parent>/<agent>/sessions/
    <unit_key>/working-memory.yaml` — not a SID comparison against the ambient
    environment. A reducer running the consumer must be able to classify a path
    belonging to a Body that is not itself, and an env-based test would answer
    a different question (`am I this Body?`) that happens to agree on the
    producer side and silently disagrees on the consumer side.
    """
    try:
        bm = _bm()
        p = Path(wm_path)
        if p.name != bm._WM_FILENAME:
            return None, None
        unit_dir = p.parent
        if unit_dir.parent.name != bm._SESSIONS_DIRNAME:
            return None, None
        return unit_dir.parent.parent, unit_dir.name
    except Exception:  # noqa: BLE001 — classification must never raise
        return None, None


# The carrier lives under `world/`, NOT under the agent tree ().
# `body-carriers` is a deliberate basename: `owncloud_sync._EXCLUDE_DIRS`
# walk-prunes `sessions`, `.history`, `presence` et al., so a dir named for one
# of those would be silently unsyncable — the same trap init-world.sh already
# records for `telemetry/session-records`.
_WORLD_CARRIER_DIRNAME = "body-carriers"

# The PRE- location, kept READ-ONLY for the transition (see
# `read_carriers`). Deliberately the same literal `body-merge._STAGED_DIRNAME`
# uses, because it is the same directory — the staged `<unit>-wm.yaml` files and
# the old `<unit>-fastlane.jsonl` carriers were co-tenants there.
#
# THAT CO-TENANCY IS NOW HISTORICAL ONLY (): the staged-WM lane made
# the same move this carrier made, to `body-manifest.world_staged_dir()` under
# `world/body-staged-wm/<agent>/`. Both legacy paths still point at this one
# directory and both readers still scan it, so the sentence above stays true of
# what is ON DISK; it is no longer true of where either lane WRITES.
_LEGACY_CARRIER_DIRNAME = "pending-body-merges"


def _legacy_carrier_dir(state_dir) -> Path:
    """`agents/<agent>/session/pending-body-merges` — READ ONLY, never written.

    Nothing produces here any more; `carrier_dir` resolves to `world/` and the
    claim fence refuses a worker write here regardless. This exists so carriers
    that WERE pushed before the move stay reachable by the consumer.
    """
    return Path(state_dir) / _LEGACY_CARRIER_DIRNAME


def _world_carrier_dir(agent_name: str, world_dir=None) -> Path:
    """`world/body-carriers/<agent>` — the ONE resolver both sides call.

    `world_dir` is INJECTABLE and defaults to the lazy `_paths` lookup, matching
    `storage_backend.py`'s own `from _paths import META_DIR, WORLD_DIR` inside a
    function. Lazy rather than module-level because this module is loaded BY THE
    DAEMON (`mind_api/src/endpoints/wm_write.py` imports it by file path) and
    `path-resolution.md` forbids a daemon path resolving through a constant
    captured at import time.

    THE PARAMETER IS NOT A TEST CONVENIENCE — it is a correctness fence. Without
    it every caller falls through to the REAL world root, so a hermetic test
    that builds a tmp agent tree would still write its carrier into the live
    `world/`, and under own-cloud that is the guard-955 production-key collision
    class. Callers that own a resolved world path (the daemon's
    `ctx.paths.world`, a test's tmp root) should pass it.
    """
    if world_dir is None:
        from _paths import WORLD_DIR
        world_dir = WORLD_DIR
    return Path(world_dir) / _WORLD_CARRIER_DIRNAME / agent_name


def carrier_dir(agent_dir, world_dir=None) -> Path:
    """PRODUCER side. Derived from the agent NAME, not the agent PATH.

    WHY THIS MOVED OUT OF THE AGENT TREE (g-306-420, measured cc-08 then
    reproduced cc-09 2026-09-03). The original destination was
    `agents/<agent>/session/pending-body-merges/`, chosen because `session`
    (singular) is NOT in `owncloud_sync._EXCLUDE_DIRS` while `sessions` (plural)
    is — so it solved the SYNC problem. It ran straight into a SECOND, entirely
    separate guard: every write under `agents/<agent>/` is refused by the
    own-cloud claim fence unless this box holds the live runner claim, and a
    worker Body by definition never does. `push()` therefore raised
    `NoClaimError` on every append from every non-reducer box, forever.

    The two in-tree candidates DEADLOCK, which is why no path under
    `agents/<agent>/` can work:
      - `session/`  (singular) — syncable, but CLAIM-FENCED.
      - `sessions/` (plural)   — claim-EXEMPT, but sync-excluded (machine-local).
    Neither is both. `world/` is both: it is the store root, and worker writes
    to it are accepted (this session's own goal-record and board writes land
    from a worker box).

    Measured on cc-09 (alpha worker Body, SID 2fda1f3e, 6.8.0-138-generic,
    own-cloud) before the move: the local carrier held 151,390 bytes and
    `read_authoritative_bytes` reported the S3 key ABSENT. The same probe at the
    world-rooted destination wrote and read back byte-identical, with the key
    confirmed absent immediately beforehand.
    """
    return _world_carrier_dir(Path(agent_dir).name, world_dir)


def carrier_path(agent_dir, unit_key, world_dir=None) -> Path:
    return carrier_dir(agent_dir, world_dir) / f"{unit_key}{CARRIER_SUFFIX}"


def record_local(wm_path, slot: str, item, world_dir=None) -> Path | None:
    """Append one flagged capture to this Body's carrier. Returns the carrier
    path when a line was written, else None.

    Caller-gated ON PURPOSE: the caller decides whether `slot` is a capture slot
    and whether `item` is flagged, because both callers (wm.py, wm_write.py)
    already hold `CAPTURE_SLOTS`. Re-deriving it here would add a third copy of
    a constant that must not drift.

    Writes LOCALLY only — the store push is `push()` below, deliberately split
    so the caller can hold its WM lock across this fast append and release it
    before the network round trip.
    """
    if not isinstance(item, dict):
        return None
    agent_dir, unit_key = split_body_wm_path(wm_path)
    if agent_dir is None:
        return None  # agent-wide WM (the reducer's own) — no carrier needed
    try:
        path = carrier_path(agent_dir, unit_key, world_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"unit_key": unit_key, "slot": slot, "entry": item},
                          sort_keys=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return path
    except (OSError, TypeError, ValueError):
        # TypeError/ValueError: an entry carrying a non-JSON-serialisable value.
        # Dropping the carrier line is correct — the WM write already succeeded
        # and is the durable record.
        return None


_PUSH_FAILURE_REPORTED = False

# : the plain PUT plus up to three re-fenced PUTs. Worst case adds
# ~1.75 s of backoff to an append that is already losing races, against the
# wrapper's 90 s request timeout; the common (winning) path adds nothing.
_CONFLICT_ATTEMPTS = 4
_CONFLICT_BACKOFF_BASE_S = 0.25


def _conflict_backoff(attempt: int) -> float:
    """Full-jitter backoff (seconds) before re-fenced attempt `attempt` (0-based).

    Full jitter, like `owncloud_backend._conflict_backoff`, because the other
    writer is a sweep that PUTs on its own cadence: a fixed delay can re-collide
    with it in lockstep, a uniform draw decorrelates.
    """
    return random.uniform(0.0, _CONFLICT_BACKOFF_BASE_S * (2 ** attempt))


def _lost_race(be, exc) -> bool:
    """True when `exc` says another writer won this key, and never otherwise.

    Two shapes. The backend's own `conflict_error` — the 412 `OwnCloudBackend
    ._put` raises for a stale If-Match (an empty tuple on LocalBackend, which
    matches nothing). And S3's 409 `ConditionalRequestConflict`, which `_put`
    leaves unmapped, so it arrives as a raw ClientError. Anything else — a
    transport error, a permission gap, the structural NoClaimError — is not a
    race, and retrying it would only delay the report.
    """
    conflict = getattr(be, "conflict_error", ())
    if conflict and isinstance(exc, conflict):
        return True
    response = getattr(exc, "response", None)
    return (isinstance(response, dict)
            and (response.get("Error") or {}).get("Code")
            == "ConditionalRequestConflict")


def _put_whole_carrier(be, p: Path, attempt: int) -> None:
    """One PUT of the current local carrier. Attempt 0 uses the fence this
    process already holds; a retry re-HEADs and fences on what it just read.

    The retry cannot reuse the old fence: that fence is exactly what went stale,
    and re-PUTting on it 412s deterministically (rb-2639, guard-908).
    `stat` + `mirror_put(expected_version=)` is the sync sweep's own
    local->store mirror primitive, so a retry adds no new write shape — and it
    never downloads, so the local carrier (the only copy of an undelivered line)
    is never overwritten by an older remote. Backends without those two (they
    are own-cloud-only, not on the Protocol) retry the plain PUT.
    """
    data = p.read_bytes()
    stat = getattr(be, "stat", None)
    mirror_put = getattr(be, "mirror_put", None)
    if attempt == 0 or stat is None or mirror_put is None:
        be.write_bytes(p, data)
        return
    st = stat(p)
    mirror_put(p, data, expected_version=st.version if st is not None else None)


def push(path) -> bool:
    """Push the WHOLE carrier to the authoritative store. Never raises.

    Whole-file rather than delta: see the module docstring. This is what makes a
    failed push self-repairing instead of requiring a retry queue.

    A LOST RACE IS RETRIED, bounded and jittered, on a fresh fence (g-115-9852).
    Each retry re-reads the local carrier, so an append that lands during the
    backoff rides along. A conflict that survives every attempt is not a race any
    more, it is a wedge — the report names the attempt count so the two can be
    told apart (guard-908's persistence discriminator).

    A FAILURE IS REPORTED ONCE PER PROCESS, and the never-raises contract is
    unchanged (g-306-420). This except used to discard the cause entirely, so a
    transport that could not work AT ALL was indistinguishable from one that had
    nothing to send: measured 2026-09-03 on cc-08, a worker Body's carrier held
    101 undelivered rows while every push returned a quiet False. The cause was
    a structural `NoClaimError` — the carrier's destination is inside the
    claim-protected agent tree and a worker Body never holds the runner claim —
    which is exactly the kind of permanent, non-retryable fault that most needs
    to be seen and was the least visible.

    Reported ONCE rather than every call on purpose: on a non-reducer box this
    fails on EVERY append, so per-call logging would be pure noise and would be
    filtered out by the first reader who noticed it (the failure mode
    rb-5242's degrade-and-log pattern exists to avoid). One line per process
    names the exception class, which is what distinguishes a permanent
    structural refusal from a transient blip.

    Still returns bool and still swallows: a raise here would fail the WM append
    this transport exists to back, which is strictly worse than a dark push.
    """
    global _PUSH_FAILURE_REPORTED
    if path is None:
        return False
    attempts = 0
    try:
        from storage_backend import get_backend
        be = get_backend()
        p = Path(path)
        for attempt in range(_CONFLICT_ATTEMPTS):
            if attempt:
                time.sleep(_conflict_backoff(attempt - 1))
            attempts = attempt + 1
            try:
                _put_whole_carrier(be, p, attempt)
                return True
            except Exception as exc:  # noqa: BLE001 — a lost race retries, the rest is reported below
                if attempts == _CONFLICT_ATTEMPTS or not _lost_race(be, exc):
                    raise
    except Exception as exc:  # noqa: BLE001 — transport must never fail a WM append
        if not _PUSH_FAILURE_REPORTED:
            _PUSH_FAILURE_REPORTED = True
            try:
                sys.stderr.write(
                    "[body-capture-carrier] push FAILED (%s: %s) after %d "
                    "attempt(s) — capture entries are accumulating in the local "
                    "carrier and are NOT reaching the reducer. Reported once per "
                    "process; further failures are silent. A NoClaimError here is "
                    "STRUCTURAL, not transient (g-306-420); a lost race is retried "
                    "on a fresh fence, so a conflict that survived %d attempts is "
                    "a WEDGE, not a race (g-115-9852).\n"
                    % (type(exc).__name__, exc, attempts, _CONFLICT_ATTEMPTS)
                )
            except Exception:  # noqa: BLE001 — reporting must never fail the push
                pass
        return False


def _iter_carrier_names(cdir: Path, backend) -> list:
    """Carrier basenames from the local dir UNIONed with the store listing.

    The union is the whole point: on the reducer's own box the local dir is
    authoritative-ish, but a REMOTE Body's carrier exists ONLY in the store, and
    that is the case this module was built for. Either leg failing degrades to
    the other rather than to an exception.
    """
    names: set = set()
    try:
        if cdir.is_dir():
            names.update(p.name for p in cdir.iterdir()
                         if p.is_file() and p.name.endswith(CARRIER_SUFFIX))
    except OSError:
        pass
    if backend is not None:
        try:
            names.update(n for n in backend.list_dir(cdir.resolve())
                         if isinstance(n, str) and n.endswith(CARRIER_SUFFIX))
        except Exception:  # noqa: BLE001 — store listing is additive, never fatal
            pass
    return sorted(names)


def read_carriers(state_dir, backend, world_dir=None, skipped=None) -> dict:
    """{unit_key: {slot: [entry, ...]}} across every Body's carrier.

    Reads authoritative-first per file (`body-merge._read_staged_bytes`), so a
    stale local mirror never shadows the store copy — the staleness half of the
    g-115-4154 class, and the reason a plain local read here would reproduce the
    very blindness this carrier exists to remove.

    A malformed line is SKIPPED rather than failing the file: one bad append
    must not strand every other flagged entry from that Body.

    `skipped`, when a list, receives the name of every carrier file that could
    not be read. An unread carrier and an empty one return the same dict, and
    capture_fast_lane must tell them apart before it forgets any consumed hash
    that carrier may still offer (g-115-10776).
    """
    out: dict = {}
    try:
        bm = _bm()
        # ONE directory, resolved by the SAME function the producer calls
        # (). These two sides used to derive the same path by two
        # DIFFERENT expressions — `agent_dir/state/staged` in the producer,
        # `state_dir/staged` here — which is exactly the producer/consumer
        # path-drift class guard-3408 names. They now share
        # `_world_carrier_dir`, keyed on the agent NAME, so they cannot drift.
        #
        # `state_dir` is `agents/<agent>/session`, so its parent IS the agent
        # dir; deriving the name from it keeps this signature unchanged for
        # capture_fast_lane.py, the only caller.
        #
        # TRANSITION READ of the pre-move `agents/<agent>/session/
        # pending-body-merges/` location, in ADDITION to the world root.
        #
        # This block first said the legacy read was omitted "deliberately", on
        # two premises. MEASURED FROM cc-09 AGAINST THE AUTHORITATIVE STORE the
        # same day (alpha worker Body, SID 2fda1f3e, `uname -r`
        # 6.8.0-138-generic, own-cloud), BOTH ARE FALSE:
        #
        #  (1) "Only a Body on the REDUCER'S OWN box ever landed a carrier
        #      there." The store holds FOUR legacy carriers for alpha —
        #      1dc6fc35 (713,285 B), 9a35daca (669,705 B), cd5fd3b9 (784,595 B),
        #      d1aec55b (1,551,282 B) = 3,718,867 B / 1,632 flagged entries —
        #      and 9a35daca is the cc-08 Body whose close logged
        #      `result=marked-push-failed` on 2026-09-01. Newest store write
        #      2026-08-27T16:59:39Z, independently reproducing 's
        #      "frozen since 08-27" from a different box. (WHY those pushes
        #      succeeded and later ones did not is NOT established here; the
        #      claim would have to be read off the runner-claim history.)
        #
        #  (2) "a real double-append hazard when one Body has entries in both."
        #      There is none. Every consumer path runs through
        #      `capture_fast_lane._merge_flagged`, which dedups by CONTENT HASH
        #      via `body-merge._dedup_append(existing, flagged, extra_seen=prior)`
        #      — and `_dedup_append` does `seen.add(h)` inside its own loop, so
        #      the SAME entry arriving from both directories in ONE call is
        #      dropped on the second sighting. The  consumed-watermark
        #      (`capture_consumed_hashes`) covers the across-call case.
        #
        # The load-bearing half was the fallback claim — that those entries are
        # "still delivered by the close-time `generalize_down` full merge". That
        # merge needs a staged `<unit>-wm.yaml`; the store holds ZERO of them
        # (4 objects in the legacy dir, all `-fastlane.jsonl`), and 9a35daca's
        # session dir was reaped by the stale-binding sweep. So for those Bodies
        # the carrier is the ONLY surviving copy, and dropping the read would
        # strand it permanently rather than merely lose acceleration.
        #
        # Cost is one extra listing per call — `read_carriers` runs at
        # generalize_down, not per unit. The read is one-directional: nothing
        # writes here any more, so the directory drains and never refills, and
        # the listing goes empty on its own.
        state_dir_p = Path(state_dir)
        cdirs = [
            _world_carrier_dir(state_dir_p.parent.name, world_dir),
            _legacy_carrier_dir(state_dir_p),
        ]
    except Exception:  # noqa: BLE001
        return out

    try:
        bmg = _body_merge()
    except Exception:  # noqa: BLE001
        return out

    for cdir, name in [(d, n) for d in cdirs
                       for n in _iter_carrier_names(d, backend)]:
        raw, transient = bmg._read_staged_bytes(backend, cdir / name)
        if transient or raw is None:
            # transient: the store hid it AND there is no local copy.
            # Skipping is correct — an empty read here would look like
            # "this Body has nothing flagged", which is the false-negative
            # this whole file is about.
            if transient and isinstance(skipped, list):
                skipped.append(name)
            continue
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            unit_key = rec.get("unit_key")
            slot = rec.get("slot")
            entry = rec.get("entry")
            if not unit_key or not slot or not isinstance(entry, dict):
                continue
            out.setdefault(unit_key, {}).setdefault(slot, []).append(entry)
    return out


def _carrier_pairs(raw: bytes, unit_key: str, content_hash) -> set:
    """{(slot, content hash)} for one Body's rows in a carrier's bytes.

    Malformed lines and other units' rows are skipped, as in `read_carriers`.
    """
    pairs: set = set()
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (isinstance(rec, dict) and rec.get("unit_key") == unit_key
                and isinstance(rec.get("entry"), dict)):
            pairs.add((rec.get("slot"), content_hash(rec["entry"])))
    return pairs


def verify_delivery(wm_path, backend=None, world_dir=None) -> "tuple[str, str]":
    """Is every capture THIS Body flagged present in the STORE copy of its carrier?

    (g-115-9852 outcome 3.) `push()` returning True means only that the store
    write returned without raising: an attempt, not a delivery. This function
    reads the store back. worker-loop Phase 3.7 calls it once per work unit,
    after the capture lanes have appended, so the unit that wedged the lane is
    the one that gets told.

    Returns (verdict, detail):
      "n/a"          not a forked Body WM, or nothing flagged since the fork
      "delivered"    every capture flagged since the fork is in the store copy
      "undelivered"  at least one is absent, or the store holds no carrier
      "unverified"   the check could not run; never an all-clear (guard-1760)

    Three choices shape the answer.

    It compares CONTENT, never mtime or bytes. The local carrier is written
    whether or not the push lands, and it can be refreshed from a stale store
    copy, so its mtime proves nothing. A size or ETag comparison with the store
    can show DRIFT on identical content (guard-2245). So the store copy is read
    through `read_authoritative_bytes`, which decodes, and entries are matched on
    `body-merge._content_hash`, the identity the consumer dedups on.

    It subtracts the fork BASELINE. A Body's WM starts as a byte copy of the
    agent-wide WM, flagged entries included, and none of those were this Body's
    to carry. Measured on cc-09 (2026-09-14): 1,395 flagged entries in one Body's
    WM, 1,245 of them inherited. Without the subtraction, a healthy carrier
    reports 1,245 missing. With no baseline the two populations cannot be told
    apart, so the verdict is "unverified", not a guess.

    On a miss, the detail says how many of the missing entries the LOCAL carrier
    holds. If it holds them, the push is failing. If it does not, the local file
    lost them as well, and no later push can deliver them.

    Never raises. Why a per-unit check and not a per-append read-back:
    core/config/rationale/capture-carrier-delivery-check.md
    """
    try:
        # Load body-manifest FIRST: split_body_wm_path swallows a load failure
        # as "not a Body", which would turn a broken check into an n/a.
        bm = _bm()
        agent_dir, unit_key = split_body_wm_path(wm_path)
        if agent_dir is None:
            return "n/a", (f"{wm_path} is not a forked Body WM; the reducer's "
                           "own WM has no carrier")
        wm_path = Path(wm_path)
        baseline = wm_path.parent / bm._BASELINE_FILENAME
        if not wm_path.is_file():
            return "unverified", f"no Body WM at {wm_path}"
        if not baseline.is_file():
            return "unverified", (
                f"no fork baseline at {baseline}, so the captures this Body "
                "flagged cannot be told apart from the ones it inherited")
        import yaml
        bmg = _body_merge()
        loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

        def _captures(path: Path, flagged_only: bool) -> set:
            data = yaml.load(path.read_text(encoding="utf-8"), Loader=loader)
            slots = data.get("slots") if isinstance(data, dict) else None
            found: set = set()
            for slot in bmg.wm.CAPTURE_SLOTS:
                arr = slots.get(slot) if isinstance(slots, dict) else None
                for e in arr if isinstance(arr, list) else ():
                    if isinstance(e, dict) and (e.get("load_bearing")
                                                or not flagged_only):
                        found.add((slot, bmg._content_hash(e)))
            return found

        inherited = _captures(baseline, flagged_only=False)
        own = [k for k in _captures(wm_path, flagged_only=True)
               if k not in inherited]
        if not own:
            return "n/a", ("nothing flagged since the fork; the carrier holds "
                           "flagged captures only (guard-6181)")
        path = carrier_path(agent_dir, unit_key, world_dir).resolve()
        if backend is None:
            from storage_backend import get_backend
            backend = get_backend()
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — a check that cannot run says so
        return "unverified", (f"could not read this Body's captures "
                              f"({type(exc).__name__}: {exc})")

    try:
        raw = backend.read_authoritative_bytes(path)
    except FileNotFoundError:
        raw = None
    except Exception as exc:  # noqa: BLE001
        return "unverified", (f"store read of {path.name} failed "
                              f"({type(exc).__name__}: {exc})")

    stored = (_carrier_pairs(raw, unit_key, bmg._content_hash)
              if raw is not None else set())
    missing = [k for k in own if k not in stored]
    if not missing:
        return "delivered", (f"all {len(own)} capture(s) flagged since the fork "
                             f"are in the store copy of {path.name}")
    try:
        local = (_carrier_pairs(path.read_bytes(), unit_key, bmg._content_hash)
                 if path.is_file() else set())
    except OSError:
        local = set()
    by_slot: dict = {}
    for slot, _h in missing:
        by_slot[slot] = by_slot.get(slot, 0) + 1
    if raw is None:
        head = (f"the store holds NO carrier at {path}, so all {len(missing)} "
                f"capture(s) flagged since the fork are undelivered")
    else:
        head = (f"{len(missing)} of {len(own)} capture(s) flagged since the fork "
                f"are absent from the store copy of {path.name}")
    return "undelivered", (
        f"{head} ({', '.join(f'{s}={n}' for s, n in sorted(by_slot.items()))}); "
        f"the local carrier holds {sum(1 for k in missing if k in local)} of them")
