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

BEST-EFFORT BY CONTRACT. Every function here swallows its own failures and
returns a falsy value. This sits on the `wm append` hot path, and a carrier
problem must never fail the working-memory write that produced it — the WM is
the durable record, this is an accelerator in front of it.
"""
from __future__ import annotations

import importlib.util
import json
import sys
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


def push(path) -> bool:
    """Push the WHOLE carrier to the authoritative store. Never raises.

    Whole-file rather than delta: see the module docstring. This is what makes a
    failed push self-repairing instead of requiring a retry queue.

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
    try:
        from storage_backend import get_backend
        be = get_backend()
        p = Path(path)
        be.write_bytes(p, p.read_bytes())
        return True
    except Exception as exc:  # noqa: BLE001 — transport must never fail a WM append
        if not _PUSH_FAILURE_REPORTED:
            _PUSH_FAILURE_REPORTED = True
            try:
                sys.stderr.write(
                    "[body-capture-carrier] push FAILED (%s: %s) — capture "
                    "entries are accumulating in the local carrier and are NOT "
                    "reaching the reducer. Reported once per process; further "
                    "failures are silent. A NoClaimError here is STRUCTURAL, "
                    "not transient (g-306-420).\n"
                    % (type(exc).__name__, exc)
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


def read_carriers(state_dir, backend, world_dir=None) -> dict:
    """{unit_key: {slot: [entry, ...]}} across every Body's carrier.

    Reads authoritative-first per file (`body-merge._read_staged_bytes`), so a
    stale local mirror never shadows the store copy — the staleness half of the
    g-115-4154 class, and the reason a plain local read here would reproduce the
    very blindness this carrier exists to remove.

    A malformed line is SKIPPED rather than failing the file: one bad append
    must not strand every other flagged entry from that Body.
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

    bmg = sys.modules.get("body_merge")
    if bmg is None:
        try:
            spec = importlib.util.spec_from_file_location(
                "body_merge", SCRIPT_DIR / "body-merge.py")
            bmg = importlib.util.module_from_spec(spec)
            sys.modules["body_merge"] = bmg
            spec.loader.exec_module(bmg)
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
