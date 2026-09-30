#!/usr/bin/env python3
"""world/-rooted carrier for worker-Body execution-diary rows ().

WHY THIS EXISTS. `execution-diary.py` appends box-locally to
`agents/<agent>/session/execution-diary.jsonl` and has NO store delivery in the
append path. That file is under the agent tree, which the own-cloud claim fence
(`owncloud_sync._owned_agents`) refuses to push from any box that does not hold
the live runner claim — and a worker Body by definition never holds it (the
reducer holds it). So a worker's diary rows are stranded on its box and never
enter the store of record, and `read_fleet_diaries` (authoritative read,
g-115-4154) is blind to them. Measured 2026-09-28 (alpha worker Body, cc-07,
SID 9c5cc235): the local diary held 28 scorer_override rows in the audit window
while the authoritative alpha diary held 0, and the Layer-C audit printed clean
while the Body was over the >3 threshold and held the window's only
force-override.

THE LOCATION REUSES THE CAPTURE CARRIER'S RESOLUTION (g-306-420). Under
`agents/<agent>/` the two in-tree candidates deadlock — `session/` (singular)
is syncable but claim-fenced, `sessions/` (plural) is claim-exempt but
sync-excluded — and only `world/` is BOTH syncable and worker-writable. The
capture carrier moved there for exactly this reason and its push succeeds from
a non-claim box; this carrier does the same at `world/body-diaries/<agent>/`.
The basename `body-diaries` is deliberate: `owncloud_sync._EXCLUDE_DIRS`
walk-prunes `sessions`/`.history`/`presence` et al., so a dir named for one of
those would be silently unsyncable (the same trap init-world.sh records for
`telemetry/session-records`).

THE KEY IS PER-BODY, SO WHOLE-FILE PUSH IS SAFE. The carrier key is
`world/body-diaries/<agent>/<sid>.jsonl` — one file per Body session, and the
store copy of that key holds ONLY this Body's rows. (If the carrier shared the
agent-wide diary key, a whole-file PUT would clobber the reducer's rows; it
does not — the per-sid key is the whole reason the proven whole-file shape is
reused instead of inventing a fenced line-append.) Each append therefore
rewrites the WHOLE carrier to the store: a failed push (transport blip, absent
credentials) is repaired by the next successful append — no retry queue, no
reconciliation state. The residual is the LAST line before a Body goes quiet:
if its push failed, that line reaches the store only via the daemon's world/
sync sweep (the same self-healing backstop the capture carrier relies on).

THE KEY HAS TWO WRITERS, SO A LOST RACE IS RETRIED IN PLACE (g-115-9852). The
daemon's own sync sweep mirrors `world/` and `body-diaries` is not excluded, so
this push races the sweep for the same key. The capture carrier measured both
race shapes in the field (412 `ConflictError`, 409
`ConditionalRequestConflict`); `push` retries a lost race — and ONLY a lost
race — on a fresh fence, reusing `body_capture_carrier`'s `_lost_race` and
`_put_whole_carrier` verbatim so the two carriers cannot drift on the one bit
of logic that has already been wrong once.

VERBATIM ROWS, DEDUP BY FULL LINE. A carrier row is the agent-wide diary row
serialized EXACTLY as `execution-diary.py` writes it (the same `json.dumps`
string), so a reader that unions carrier rows with the agent-wide diary can
dedup by full line and never double-count. No envelope metadata inside the row:
adding a `carried_at` or `source_body` would change the bytes and break the
dedup the consumer relies on. Attribution is already carried inside the row by
`execution-diary.py`'s `body_sid` (a worker Body's entry names its session).

BEST-EFFORT BY CONTRACT. Every public function here swallows its own failures
and returns a falsy/neutral value. It sits on the `execution-diary append` hot
path and a carrier problem must never fail the diary write that produced it —
the local diary IS the durable record; this is a delivery accelerator in front
of it.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def _bcc():
    """body_capture_carrier — the owner of the conflict-classification,
    re-fenced-PUT and once-per-process-report machinery. Loaded lazily and
    cached (its own `_bm()` shape) so this module can be imported by the diary
    append path without pulling the capture stack at import time, and reused
    rather than re-declared: the two carriers must not drift on the one logic
    that has already been wrong once (g-115-9852).
    """
    cached = sys.modules.get("body_capture_carrier")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        "body_capture_carrier", SCRIPT_DIR / "body_capture_carrier.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["body_capture_carrier"] = mod
    spec.loader.exec_module(mod)
    return mod


# Deliberate basename (see module docstring): not a sync-excluded name.
_BODY_DIARIES_DIRNAME = "body-diaries"


def _world_carrier_dir(agent_name: str, world_dir=None) -> Path:
    """`world/body-diaries/<agent>` — the ONE resolver both sides call.

    `world_dir` is injectable and defaults to the lazy `_paths` lookup (the
    capture carrier's own shape). The parameter is a correctness fence, not a
    test convenience: without it a hermetic test would write into the live
    `world/` (the guard-955 production-key collision class). Callers that own a
    resolved world path pass it. The lazy re-import re-reads the `_paths`
    module attribute at call time, which is what lets a test monkeypatch
    `_paths.WORLD_DIR` to a tmp root.
    """
    if world_dir is None:
        from _paths import WORLD_DIR
        world_dir = WORLD_DIR
    return Path(world_dir) / _BODY_DIARIES_DIRNAME / agent_name


def carrier_dir(agent_name: str, world_dir=None) -> Path:
    """The per-agent carrier dir, derived from the agent NAME (not path)."""
    return _world_carrier_dir(agent_name, world_dir)


def carrier_path(agent_name: str, sid: str, world_dir=None) -> Path:
    """`world/body-diaries/<agent>/<sid>.jsonl` — one file per Body session."""
    return _world_carrier_dir(agent_name, world_dir) / f"{sid}.jsonl"


def record_local(agent_name: str, sid: str, line: str,
                 world_dir=None) -> Path | None:
    """Append one VERBATIM row to this Body's local carrier. Returns the
    carrier path when a line was written, else None.

    `line` is the agent-wide diary row already serialized by
    `execution-diary.py` (the exact string appended to the local diary), so
    the carrier row is byte-identical to the row a reader dedups against.
    Caller-gated: the caller decides this is a worker Body and that the row is
    worth carrying.
    """
    if not agent_name or not sid or not line:
        return None
    try:
        path = carrier_path(agent_name, sid, world_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return path
    except (OSError, TypeError, ValueError):
        # TypeError/ValueError: a non-str/serialisable line. Dropping the
        # carrier line is correct — the diary write already succeeded and is
        # the durable record.
        return None


def push(path) -> bool:
    """Push the WHOLE carrier to the authoritative store. Never raises.

    Reuses `body_capture_carrier`'s `push` verbatim (same shape, same key
    semantics: Body-owned object, whole-file rewrite, re-fenced lost-race
    retry, once-per-process failure report). See that docstring for why each
    property is load-bearing; the two carriers deliberately share the
    transport so they cannot drift.
    """
    if path is None:
        return False
    return _bcc().push(path)


def append_row(agent_name: str, sid: str, line: str, world_dir=None) -> bool:
    """Deliver ONE row: write it to the local carrier, then push the carrier to
    the authoritative store. Never raises. Returns True when the store write
    returned without raising (an attempt, not a delivery).

    This is the one-line call `execution-diary.py` makes from its worker append
    path. The local write happens first (it is the durable record and the base
    for the mirror); a failed local write skips the push rather than pushing a
    stale carrier.
    """
    path = record_local(agent_name, sid, line, world_dir)
    if path is None:
        return False
    return push(path)


def read_carrier_lines(agent_name: str, world_dir=None, backend=None) -> list[str]:
    """All of an agent's worker-Body carrier rows, authoritative-first per file.

    Returns a flat list of VERBATIM diary rows (one string each) across every
    `<sid>.jsonl` under `world/body-diaries/<agent>/`. This is the READ side
    `_fleet_diary.read_fleet_diaries` unions into the agent-wide diary so every
    content consumer (scorer-override-audit, skill-discovery) sees
    worker-Body rows.

    The name listing is a UNION of the local dir and the store listing (the
    capture carrier's `_iter_carrier_names` shape): on the reducer's own box
    the local dir is authoritative-ish, but a REMOTE Body's carrier exists ONLY
    in the store, and that is the case this module is built for.

    Authoritative-first per file (`body-merge._read_staged_bytes`): a stale
    local mirror must never shadow the store copy. A file the store hides AND
    has no local copy for is skipped (not an error) — one unread carrier must
    not strand every other Body's rows. A malformed line is skipped rather than
    failing the file (a torn append must not hide the rest).

    Never raises. `backend` is injectable for hermetic tests; the default is
    the live storage backend (LocalBackend under a pinned `STORAGE_BACKEND=
    local`, so the read is a plain read of the local tree).
    """
    out: list[str] = []
    if not agent_name:
        return out
    cdir = carrier_dir(agent_name, world_dir)
    names: set = set()
    try:
        if cdir.is_dir():
            names.update(p.name for p in cdir.iterdir()
                         if p.is_file() and p.name.endswith(".jsonl"))
    except OSError:
        pass
    if backend is not None:
        try:
            names.update(n for n in backend.list_dir(cdir)
                         if isinstance(n, str) and n.endswith(".jsonl"))
        except Exception:  # noqa: BLE001 — store listing is additive, never fatal
            pass
    if not names:
        return out
    try:
        bmg = _bcc()._body_merge()
    except Exception:  # noqa: BLE001 — no staged-bytes helper: local reads only
        bmg = None
    for name in sorted(names):
        p = cdir / name
        raw = None
        if bmg is not None:
            try:
                raw, transient = bmg._read_staged_bytes(backend, p)
                if transient:
                    continue
            except Exception:  # noqa: BLE001 — degrade to the local copy
                raw = None
        if raw is None:
            try:
                if p.is_file():
                    raw = p.read_bytes()
            except OSError:
                continue
        if raw is None:
            continue
        for line in raw.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:  # a torn/partial line must not strand the rest of the file
                json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            out.append(line)
    return out
