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

THE ASYMMETRY THIS EXPLOITS. `sessions` (plural, per-Body) is sync-excluded;
`session` (singular, agent-wide) is NOT. Verified the same day, with the
syncable `session/body-heartbeat-<SID>.json` carrier as the positive control
(`_machine_local=False`). So a `session/`-rooted file reaches the store from any
box, using the transport that already exists.

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


def carrier_dir(agent_dir) -> Path:
    bm = _bm()
    return Path(agent_dir) / bm._STATE_DIRNAME / bm._STAGED_DIRNAME


def carrier_path(agent_dir, unit_key) -> Path:
    return carrier_dir(agent_dir) / f"{unit_key}{CARRIER_SUFFIX}"


def record_local(wm_path, slot: str, item) -> Path | None:
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
        path = carrier_path(agent_dir, unit_key)
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


def push(path) -> bool:
    """Push the WHOLE carrier to the authoritative store. Never raises.

    Whole-file rather than delta: see the module docstring. This is what makes a
    failed push self-repairing instead of requiring a retry queue.
    """
    if path is None:
        return False
    try:
        from storage_backend import get_backend
        be = get_backend()
        p = Path(path)
        be.write_bytes(p, p.read_bytes())
        return True
    except Exception:  # noqa: BLE001 — transport must never fail a WM append
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


def read_carriers(state_dir, backend) -> dict:
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
        cdir = Path(state_dir) / bm._STAGED_DIRNAME
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

    for name in _iter_carrier_names(cdir, backend):
        raw, transient = bmg._read_staged_bytes(backend, cdir / name)
        if transient or raw is None:
            # transient: the store hid it AND there is no local copy. Skipping
            # is correct — an empty read here would look like "this Body has
            # nothing flagged", which is the false-negative this whole file is
            # about.
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
