#!/usr/bin/env python3
"""Read/write the per-Body `body-manifest.yaml` (Phase 1B, Mind/Body — ).

A Body is a forked instance of the Mind keyed by `unitKey` (locally the session
SID). Its `agents/<mindKey>/sessions/<unitKey>/body-manifest.yaml` records:
  {unitKey, mindKey, env_id, role, body_state, started_at, forked_wm_hash}

This is the manifest's SOLE writer — /start FORK-BODY calls `write`, stop-hook
calls `set-state`. Schema + lifecycle: `core/config/conventions/session-state.md`
"Phase 1B - Body Manifest". Design SSOT: tree node `mind-engine-identity-bridge`.

REDUCER-AWARE FORK (the backward-compatibility keystone):
  The reducer is the worker Body holding `running-session-id` (derived, not
  stored). The reducer does NOT fork its WM (it IS the canonical Mind WM) — its
  manifest carries `forked_wm_hash: null` and no body-WM-file is created, so
  Phase 1A routing (which keys on the body-WM-FILE's existence) returns the
  agent-wide path. Only a NON-reducer worker (a 2nd+ worker once a reducer
  already holds `running-session-id`) forks: FORK-BODY `cp`s the Mind WM as the
  Body's baseline, records its sha256, and the body-WM-file's existence flips
  routing to the per-Body path. Observers never fork (read-only). With exactly
  one Body (the reducer) this is inert — today's behavior, unchanged.

CLI:
  py -3 core/scripts/body-manifest.py write --sid <unitKey> --agent <mindKey>
        [--env-id local] [--role reducer|worker|observer]
  py -3 core/scripts/body-manifest.py read     --sid <unitKey> --agent <mindKey>
  py -3 core/scripts/body-manifest.py set-state --sid <unitKey> --agent <mindKey> <state>
  py -3 core/scripts/body-manifest.py is-reducer --sid <unitKey> --agent <mindKey>

`write` prints the manifest path; `read` prints the manifest as JSON; `set-state`
prints the path; `is-reducer` prints `true`/`false`. Non-zero exit + stderr
diagnostic on validation/IO failure (human-readable for /start's error path).
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _session_binding import (  # noqa: E402
    _SESSIONS_DIRNAME,
    _agent_dir,
    _valid_agent_name,
    _valid_sid_shape,
)

# Singular agent-wide state dir. Sync point: CLAUDE.md "Agent-dir Resolution"
# SESSION_DIRNAME (currently "session"). Inlined here (not sourced) to keep this
# session-boundary helper self-contained, mirroring the inlined-copy pattern the
# other session-state scripts use.
_STATE_DIRNAME = "session"
_MANIFEST_FILENAME = "body-manifest.yaml"
_WM_FILENAME = "working-memory.yaml"
# : the fork-time WM snapshot (the 3-way-delta common ancestor). Written
# byte-faithfully at fork beside the live (mutating) body WM; read by
# body-merge.generalize_down. body-merge references this as bm._BASELINE_FILENAME.
_BASELINE_FILENAME = "forked-wm-baseline.yaml"
# : genuine-close sentinel. A worker Body writes this in its session dir
# when its loop GENUINELY terminates (no more work), distinguishing a real close
# from a mere between-turns turn-end. The stop-hook marks closed-pending-merge
# only when it is present, then consumes it.
_CLOSE_SENTINEL_FILENAME = "body-closing"
# -b: cross-box WM transport staging. body-merge.py is the READER of
# this contract (_STAGED_DIRNAME L108, _STAGED_HASH_SUFFIX L109); it globs
# "*-wm.yaml" under state_dir/_STAGED_DIRNAME and derives unitKey by stripping
# that suffix. Keep these four in sync with it. The baseline suffix is
# deliberately "-wm-baseline.yaml": it does NOT match the reader's "*-wm.yaml"
# glob, so a baseline can never be mis-consumed as a Body WM.
_STAGED_DIRNAME = "pending-body-merges"
_STAGED_WM_SUFFIX = "-wm.yaml"
_STAGED_BASELINE_SUFFIX = "-wm-baseline.yaml"
_STAGED_HASH_SUFFIX = "-wm.hash"

VALID_ROLES = ("reducer", "worker", "observer")
# : `parked` is a RESUMABLE state and is deliberately NOT a close.
# A worker winds down when its reducer is gone (worker-loop Phase 0.5 rc=1),
# because executing with no merger accumulates work nobody will ever merge. That
# DECISION is correct; its terminality was the defect — the reducer returned and
# the worker stayed closed, because reopening needs a user-only /start.
#
# WHY IT IS A STATE AND NOT A SENTINEL. A park spans many turns (hourly re-poll,
# capped at PARK_MAX_HOURS), and every sentinel in this file is CONSUMED by the
# first handler that reads it — which is exactly why the stop-hook needed its
# 4th safety valve to read body_state rather than the vanished `body-closing`.
# A park needs the durable record for the same reason.
#
# WHERE IT MUST *NOT* APPEAR, and both are load-bearing:
#   - body-merge.generalize_down enumerates `closed-pending-merge` ONLY, so a
#     parked Body is never consumed. That is automatic, not a special case, and
#     it is the property that makes parking safe (see park_body).
#   - the stop-hook's closed-state grep. `parked` matching there would stand the
#     worker-net down as though the Body were finished; it gets its own valve.
VALID_STATES = ("active", "parked", "closed-pending-merge", "merged",
                "closed-stale")
# The park's own upper bound. A reducer absent this long is a human matter and
# the wind-down board post already went out, so the Body closes durably for real.
PARK_MAX_HOURS = 60.0

# THE PARTITION, NAMED ONCE (). Before `parked` existed every non-active
# state was terminal, so `!= "active"` and "is closed" were the same predicate and
# the codebase used them interchangeably — in body-manifest, in worker-loop's
# Phase -0 gate, in the deadman resurrection prompt, and in the stop-hook
# worker-net. Adding one resumable non-active state turned each of those into a
# different bug, and they are not the same bug: the gate REFUSED work, the prompt
# WEDGED with no wakeup left, the hook would have CLOSED the Body, and
# close_body_on_genuine would have consumed the sentinel while staging NOTHING —
# leaving an expired park permanently unclosed with its WM stranded.
#
# So the partition is declared here rather than re-derived at each site. A future
# state joins exactly one of these two tuples and every consumer inherits the
# right answer.
#
# CLOSEABLE, not "active": a park is a live Body that may legitimately be closed
# (its cap expired, or a user stopped it) and MUST stage its WM when that happens.
CLOSEABLE_STATES = ("active", "parked")
CLOSED_STATES = ("closed-pending-merge", "merged", "closed-stale")
# -a: the ONLY accepted --reducer-sid value. Not a SID and never one —
# a cross-box reducer's SID cannot be read from this machine (running-session-id
# is machine_local; the DDB claim stores a runner-token, not a SID). Rejecting
# every other value keeps a caller from inventing a plausible-looking SID that
# would then silently mis-address the reducer-side merge.
REMOTE_REDUCER_SENTINEL = "remote"


def _resolve_machine_id() -> str:
    """Which box this Body runs on. Delegates to the session-telemetry resolver
    so MACHINE_ID/hostname/unknown fallback has ONE definition fleet-wide.
    Local import mirrors this module's existing yaml-in-read_manifest style and
    keeps the /start write path free of an unconditional import."""
    try:
        from _session_telemetry import _machine_id
        return _machine_id()
    except Exception:
        # Never let attribution metadata break a Body write.
        return "unknown"


# Manifest field order — deterministic render keeps diffs stable.
_FIELD_ORDER = (
    "unitKey", "mindKey", "env_id", "role", "reducer_sid",
    "body_state", "started_at", "forked_wm_hash",
    # -a (cross-box worker): only ever non-default when this Body was
    # activated by /start --body worker after a cross-box rc=4 refusal.
    # remote_body distinguishes "the reducer is on ANOTHER machine" from the
    # same-box worker case — the reducer-side merge needs it because a remote
    # body's staged WM arrives via an explicit push, not the H4a periodic sweep
    # (that sweep only runs on a box holding a fresh DDB claim, which a worker
    # box never holds). machine_id records WHICH box diverged, so a merge
    # conflict is attributable.
    "remote_body", "machine_id",
)


def _project_root() -> Path:
    # core/scripts/<this>.py -> core/scripts -> core -> project root
    return SCRIPT_DIR.parent.parent


def _now_iso_local() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _write_atomic(target: Path, body: str) -> None:
    """Atomic write+rename within the same dir (same-FS rename)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, target)


def _write_atomic_bytes(target: Path, data: bytes) -> None:
    """Byte-faithful atomic write (write_bytes — NO newline translation).

    The WM fork must be a byte-exact `cp` of the Mind WM so `forked_wm_hash`
    stays a valid merge baseline; text-mode write_text would translate
    \\n->\\r\\n on Windows and corrupt the hash invariant (g-306-62 test catch).
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)


def _unlink_quiet(p: Path) -> None:
    """Best-effort unlink (consume a sentinel); a missing file is not an error."""
    try:
        p.unlink()
    except OSError:
        pass


def _render_manifest(data: dict) -> str:
    """Render the manifest dict to deterministic YAML (fixed field order).

    Hand-rendered (not yaml.safe_dump) to control field order and quoting,
    matching session-binding-write.py's style.

    _FIELD_ORDER fixes the order of the KNOWN fields; any other key present in
    `data` is emitted after them, sorted. That tail is load-bearing, not tidiness
    (g-306-122): set_state round-trips the whole manifest through this renderer,
    so without it _FIELD_ORDER acts as an ALLOWLIST and silently DROPS whatever a
    newer writer added — the guard-1900 class, whose diagnostic signature is a
    clean parse, zero errors, and a field that is simply not there. The same
    defect makes a field added to _FIELD_ORDER *after* a manifest was written
    come back as null on that manifest's next set_state.

    String values are single-quoted with '' escaping (YAML's own escape for a
    literal quote inside a single-quoted scalar). machine_id resolves from an
    operator-set, unvalidated MACHINE_ID, and an unquoted value carrying a YAML
    metacharacter breaks read_manifest -> set_state -> close_body_on_genuine
    permanently for that Body, at CLOSE time, far from the write that caused it
    (':' -> ScannerError, '*' -> ComposerError, '#' -> silent value loss).
    Quoting the whole string CLASS rather than that one field is the guard-610
    remedy — env_id, mindKey and unitKey ride the same branch. It subsumes the
    former `started_at` special case byte-identically (that value never contains
    a quote), so that branch is gone rather than duplicated.

    Non-str non-bool scalars (an int or float arriving via an unknown key) stay
    bare so they survive the round-trip as numbers rather than becoming strings.
    """
    lines = []
    unknown = sorted(k for k in data if k not in _FIELD_ORDER)
    for k in (*_FIELD_ORDER, *unknown):
        v = data.get(k)
        if v is None:
            lines.append(f"{k}: null")
        elif isinstance(v, bool):
            # Render lowercase. PyYAML (YAML 1.1) would also accept "True", but
            # YAML 1.2 parsers do not, and this manifest is read by the the framework-ES
            # side as well as by Python — emit the form every parser agrees on.
            # Must precede the str branch: bool is not str, but keeping it first
            # makes the ordering intent explicit.
            lines.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, str):
            lines.append("{}: '{}'".format(k, v.replace("'", "''")))
        else:
            lines.append(f"{k}: {v}")
    return "\n".join(lines) + "\n"


def _agent_paths(agent: str, sid: str, project_root: Path | None = None):
    pr = project_root or _project_root()
    if not _valid_agent_name(agent):
        raise ValueError(f"invalid agent name: {agent!r}")
    if not _valid_sid_shape(sid):
        raise ValueError(f"invalid SID shape: {sid!r}")
    adir = _agent_dir(pr, agent)
    if not adir.is_dir():
        raise FileNotFoundError(f"agent dir does not exist: {adir}")
    session_dir = adir / _SESSIONS_DIRNAME / sid
    state_dir = adir / _STATE_DIRNAME
    return adir, session_dir, state_dir


def _read_running_sid(state_dir: Path) -> str:
    """The reducer SOT: agents/<mindKey>/session/running-session-id (or '')."""
    p = state_dir / "running-session-id"
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def is_reducer(sid: str, agent: str, project_root: Path | None = None) -> bool:
    """True iff this unitKey holds running-session-id (the derived reducer)."""
    _, _, state_dir = _agent_paths(agent, sid, project_root)
    rsid = _read_running_sid(state_dir)
    return bool(rsid) and rsid == sid


def write_manifest(sid: str, agent: str, env_id: str = "local",
                   role: str = "worker",
                   project_root: Path | None = None,
                   reducer_sid: str | None = None) -> Path:
    """Write the Body manifest; fork the WM only for a non-reducer worker.

    Returns the manifest path. Idempotent on body_state (a re-write resets a
    Body to active — only /start calls this, once per session). Reset-to-active
    includes the close signal: any stale `body-closing` sentinel left by a
    prior life of this SID is consumed (see the comment at the write below).

    `reducer_sid=REMOTE_REDUCER_SENTINEL` ("remote") activates the CROSS-BOX
    worker case (g-306-119-a). It is a sentinel, not a SID, and deliberately so:
    the reducer's SID is UNOBTAINABLE from another machine — `running-session-id`
    is `sync_tier: machine_local` (core/config/session-manifest.yaml) and the DDB
    claim row stores a runner-token, not a SID. Callers MUST NOT invent one.
    """
    if role not in VALID_ROLES:
        raise ValueError(f"invalid role {role!r}; expected one of {VALID_ROLES}")
    adir, session_dir, state_dir = _agent_paths(agent, sid, project_root)

    remote = (reducer_sid == REMOTE_REDUCER_SENTINEL)
    if reducer_sid is not None and not remote:
        raise ValueError(
            f"invalid reducer_sid {reducer_sid!r}; the only accepted override is "
            f"{REMOTE_REDUCER_SENTINEL!r} (a cross-box reducer SID cannot be known "
            f"from this machine — see the docstring)")
    if remote and role != "worker":
        raise ValueError(
            f"reducer_sid={REMOTE_REDUCER_SENTINEL!r} is only valid with "
            f"role='worker' (got role={role!r})")

    # Fork decision: a worker that is NOT the reducer forks its WM. The reducer
    # (rsid empty -> this body will claim it; OR rsid == sid -> resumed reducer)
    # uses the agent-wide WM. Observers never fork (read-only). With one Body
    # this is always False -> backward-compatible.
    rsid = _read_running_sid(state_dir)
    fork_needed = (role == "worker") and bool(rsid) and (rsid != sid)

    # CROSS-BOX worker: bypass the local rsid read entirely. On a worker box
    # `running-session-id` NEVER exists — the whole point of the CW branch is
    # that the box stays IDLE and never writes it — so `bool(rsid)` is False and
    # the clause above silently yields fork_needed=False. That is the exact
    # failure this override exists to prevent: no fork means the worker mutates
    # the agent-wide WM, which is `sync_tier: continuity` (LWW), so the live
    # reducer's concurrent writes and this box's would silently destroy each
    # other. The rsid read is not just unhelpful here, it is unanswerable.
    if remote:
        fork_needed = True

    # reducer_sid: the SID of the active Reducer body ( / ).
    # the framework-ES reads this field to locate the Reducer's ES snapshot for
    # workers/observers. Null for the reducer itself (it IS the Reducer).
    # For workers/observers: the value of running-session-id at write time,
    # or the "remote" sentinel when the reducer lives on another machine.
    if remote:
        reducer_sid_out = REMOTE_REDUCER_SENTINEL
    else:
        reducer_sid_out = None if (role == "reducer") else (rsid or None)

    forked_wm_hash = None
    if fork_needed:
        agent_wm = state_dir / _WM_FILENAME
        body_wm = session_dir / _WM_FILENAME
        baseline_wm = session_dir / _BASELINE_FILENAME
        # The Mind WM at fork is this Body's baseline (the common ancestor for
        # generalize-down's 3-way delta). Empty bytes when no Mind WM exists yet.
        wm_bytes = agent_wm.read_bytes() if agent_wm.is_file() else b""
        forked_wm_hash = hashlib.sha256(wm_bytes).hexdigest()
        # cp the Mind WM as this Body's LIVE WM, BYTE-FAITHFULLY (the
        # body-WM-file's existence is what flips Phase 1A routing to the per-Body
        # path; the hash must match the copied bytes). This copy then DIVERGES
        # as the Body works.
        _write_atomic_bytes(body_wm, wm_bytes)
        # : ALSO snapshot the same fork-time bytes as an IMMUTABLE
        # baseline. The live body_wm above mutates; this copy stays the common
        # ancestor so generalize-down computes each counter's net delta
        # (reducer + (body - baseline)) instead of a baseline-double-counting SUM.
        _write_atomic_bytes(baseline_wm, wm_bytes)

    data = {
        "unitKey": sid,
        "mindKey": agent,
        "env_id": env_id,
        "role": role,
        "reducer_sid": reducer_sid_out,
        "body_state": "active",
        "started_at": _now_iso_local(),
        "forked_wm_hash": forked_wm_hash,
        # Defaults keep every pre-existing caller byte-identical apart from two
        # appended lines: remote_body is False and machine_id is recorded for
        # every Body (attribution is cheap and useful even same-box).
        "remote_body": remote,
        "machine_id": _resolve_machine_id(),
    }
    manifest_path = session_dir / _MANIFEST_FILENAME
    # A (re-)write resets body_state to active, so any body-closing sentinel
    # left by a PRIOR life of this SID is consumed with it (fresh-eyes review
    # of b8ac6a4cf, 2026-08-10). A stale sentinel survives only when a
    # close-turn text-death also skipped the Stop event (the rb-629 gap) —
    # every Stop the hook DOES see consumes it via close_body_on_genuine.
    # Left in place, it pairs with the fresh active manifest and the re-forked
    # Body's FIRST turn-end takes the stop-hook's WM+sentinel close branch:
    # marked closed-pending-merge after one work unit (premature retirement).
    # Safe unconditionally: any WM a stuck close meant to stage was already
    # re-baselined by the fork above, so the stale signal points at nothing
    # recoverable — and for reducer/observer roles a sentinel is foreign
    # residue by definition (only workers ever write one). Consumed BEFORE the
    # manifest write so an active manifest is never paired with a stale
    # sentinel, even transiently. Deliberately NOT in set_state: that runs
    # mid-close, before close_body_on_genuine consumes the sentinel itself.
    _unlink_quiet(session_dir / _CLOSE_SENTINEL_FILENAME)
    _write_atomic(manifest_path, _render_manifest(data))
    return manifest_path


class ManifestParseError(ValueError):
    """A body-manifest exists but does not parse as YAML.

    Subclasses ValueError DELIBERATELY: main()'s existing validation path
    (`except (ValueError, FileNotFoundError)` -> exit 2) then catches a
    malformed manifest with the documented "non-zero exit + stderr diagnostic"
    contract, WITHOUT main() importing yaml. That matters because yaml is a
    local import in read_manifest on purpose — the write and is-reducer paths
    must not pay for it (see read_manifest's import comment). Raising a
    yaml.YAMLError instead would escape both of main()'s except clauses,
    because YAMLError subclasses neither ValueError nor OSError.
    """


def read_manifest(sid: str, agent: str, project_root: Path | None = None) -> dict:
    """Load + return the manifest dict.

    Raises FileNotFoundError if absent, ManifestParseError if malformed.
    """
    import yaml  # local import: read/set-state need it; write/is-reducer don't.
    _, session_dir, _ = _agent_paths(agent, sid, project_root)
    manifest_path = session_dir / _MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise FileNotFoundError(f"no body-manifest: {manifest_path}")
    try:
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        # -b: previously unguarded. A malformed manifest raised
        # YAMLError, which escaped main()'s two except clauses (unhandled
        # traceback + exit 1 instead of the documented exit 2) AND escaped
        # close_body_on_genuine before any branch could consume the
        # body-closing sentinel — so the stop-hook condition re-fired at every
        # subsequent turn-end for that Body, permanently, falsifying that
        # function's "consumed on every genuine-close branch" invariant.
        raise ManifestParseError(
            f"malformed body-manifest {manifest_path}: {exc}") from exc
    return data or {}


def _mirror_state_to_carrier(sid: str, agent: str, new_state: str,
                             project_root: Path | None = None) -> None:
    """Mirror body_state into the SYNCABLE per-Body heartbeat carrier ().

    THIS WRITE IS WHAT KEEPS THE PEER-SIDE STALL PROBE FROM FLOODING, and it is
    the half that is easy to omit. heartbeat-tick.sh stamps the state on every
    tick, so a LIVE Body's carrier is current -- but a Body's last tick happens
    BEFORE its close, so without this mirror a cleanly-closed Body leaves a
    carrier still reading `active`. It then goes stale holding no claim, and
    worker_stall.classify_body -- correctly, on the evidence it has -- calls
    that a stall. Every clean close would become a false alert, which is the
    exact flood the split exists to prevent. The two writers ship together or
    neither ships.

    Fail-open by contract, and narrowly (guard-373): a carrier that is absent,
    unreadable, or not a JSON object leaves the field alone. The reader renders
    a missing/stale state as `stale_state_unknown`, which never alerts, so a
    failure here degrades to today's behaviour rather than to a false alarm. A
    close must never fail because a diagnostic mirror could not be written.
    """
    try:
        _, _, state_dir = _agent_paths(agent, sid, project_root)
        carrier = state_dir / f"body-heartbeat-{sid}.json"
        if not carrier.is_file():
            return
        doc = json.loads(carrier.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            return
        doc["body_state"] = new_state
        _write_atomic(carrier, json.dumps(doc) + "\n")
    except (OSError, ValueError, TypeError):
        # json.JSONDecodeError subclasses ValueError; FileNotFoundError
        # subclasses OSError. Narrow on purpose -- a NameError or AttributeError
        # here is a logic bug and must not be swallowed as a benign skip.
        return


def set_state(sid: str, agent: str, new_state: str,
              project_root: Path | None = None) -> Path:
    """Mutate body_state in place (preserving every other field). Returns path."""
    if new_state not in VALID_STATES:
        raise ValueError(
            f"invalid body_state {new_state!r}; expected one of {VALID_STATES}")
    data = read_manifest(sid, agent, project_root)
    data["body_state"] = new_state
    _, session_dir, _ = _agent_paths(agent, sid, project_root)
    manifest_path = session_dir / _MANIFEST_FILENAME
    _write_atomic(manifest_path, _render_manifest(data))
    # AFTER the manifest write, never before: the manifest is the record of
    # truth and the carrier is a mirror of it, so a crash between the two must
    # leave a correct manifest with a stale mirror (benign -- the reader treats
    # a state it cannot trust as unknown), never a carrier claiming a close the
    # manifest never recorded.
    _mirror_state_to_carrier(sid, agent, new_state, project_root)
    return manifest_path


def park_body(sid: str, agent: str, project_root: Path | None = None) -> str:
    """Park a worker Body whose reducer is gone. RESUMABLE — never a close.

    Returns 'parked' (state transitioned, park clock started), 'already-parked'
    (idempotent re-park; the ORIGINAL parked_at is preserved so the cap measures
    the whole park, not the last re-poll), 'no-forked-wm' (not a worker), or
    'not-active' (the Body is closed/merged — a close never becomes a park).

    THE ONE THING THIS DELIBERATELY DOES NOT DO IS STAGE THE WM, and the goal's
    own spec asked for the opposite ("the SAME durable handoff as today: board
    post, staged WM, pushed ref"). Staging here would be actively destructive,
    for the reason close_body_on_genuine's docstring already gives in its own
    words: a Body queued for merge that then keeps working "loses turns 2+ of WM
    divergence (the reducer merges + marks `merged`, then the worker keeps
    diverging into a now-merged manifest that the sessions-pass never
    revisits)". A parked Body is BY CONSTRUCTION one that intends to resume, so
    staging it is that hazard by design rather than by accident.

    It is also pointless, which is the cleaner argument: the trigger for parking
    is that NO REDUCER EXISTS, so there is nothing to merge into for the whole
    duration of the park. And when the reducer does return, the right outcome is
    that this Body RESUMES (rc=0 -> resume_body) — not that its half-finished
    session is consumed as final.

    Divergence is not at risk in the meantime: if the box dies mid-park the
    stale-binding path stages the WM exactly as it does for any abrupt end, and
    if the park EXPIRES the caller runs the ordinary genuine-close path, which
    stages and pushes through the single existing writer.
    """
    _, session_dir, _ = _agent_paths(agent, sid, project_root)
    if not (session_dir / _WM_FILENAME).is_file():
        return "no-forked-wm"
    data = read_manifest(sid, agent, project_root)
    state = data.get("body_state")
    if state == "parked":
        return "already-parked"
    if state != "active":
        return "not-active"
    data["body_state"] = "parked"
    data["parked_at"] = _now_iso_local()
    _write_atomic(session_dir / _MANIFEST_FILENAME, _render_manifest(data))
    # : this function writes the manifest directly rather than through
    # set_state (it must set `parked_at` in the SAME atomic write), so it needs
    # its own mirror -- a park is precisely the case that would otherwise
    # false-alert. A parked Body may sit dormant for hours between re-polls, so
    # a carrier left reading `active` goes stale and reads as a stall.
    _mirror_state_to_carrier(sid, agent, "parked", project_root)
    return "parked"


def resume_body(sid: str, agent: str, project_root: Path | None = None) -> str:
    """Return a parked Body to active. Returns 'resumed', 'not-parked', or
    'no-forked-wm'.

    `parked_at` is CLEARED on resume so a later re-park starts a fresh clock —
    a Body that parked, resumed, and parked again has not been unattended for
    the sum of both, and carrying the stale stamp would expire it early.
    """
    _, session_dir, _ = _agent_paths(agent, sid, project_root)
    if not (session_dir / _WM_FILENAME).is_file():
        return "no-forked-wm"
    data = read_manifest(sid, agent, project_root)
    if data.get("body_state") != "parked":
        return "not-parked"
    data["body_state"] = "active"
    data.pop("parked_at", None)
    _write_atomic(session_dir / _MANIFEST_FILENAME, _render_manifest(data))
    # : same reason as park_body -- direct manifest write, so its own
    # mirror. Leaving a resumed Body's carrier reading `parked` would suppress a
    # genuine stall (the benign side of the split), which is the failure
    # direction this whole change exists to close.
    _mirror_state_to_carrier(sid, agent, "active", project_root)
    return "resumed"


def park_expired(sid: str, agent: str, project_root: Path | None = None,
                 max_hours: float = PARK_MAX_HOURS) -> bool:
    """True iff a parked Body has been parked longer than `max_hours`.

    FAIL-SAFE TOWARD STAYING PARKED: a missing, empty, or unparseable
    `parked_at` returns False. The alternative — treating an unreadable stamp as
    expired — would durably close a Body on a field-format problem, and a wrong
    close is the unrecoverable direction (Phase -0 then refuses every further
    unit and only a user-only /start reopens it). A park that runs long is
    visible on the board and costs nothing but an hourly poll.
    """
    data = read_manifest(sid, agent, project_root)
    if data.get("body_state") != "parked":
        return False
    stamp = (data.get("parked_at") or "").strip()
    if not stamp:
        return False
    try:
        parked = datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError):
        return False
    elapsed = (datetime.datetime.now() - parked).total_seconds() / 3600.0
    return elapsed > max_hours


def _stage_and_push(session_dir: Path, state_dir: Path, data: dict) -> bool:
    """Stage this Body's forked WM (+ baseline + hash) for the reducer AND
    explicitly push each staged file to the storage backend.

    Returns True iff every staged file was written AND pushed. Never raises —
    a transport failure must not break the stop-hook's turn-end; the caller
    surfaces it via the 'marked-push-failed' return string instead.

    WHY AN EXPLICIT PUSH AND NOT owncloud-flush (measured 2026-08-02, alpha,
    cc-04). The design doc says "owncloud-flush/backend-put"; the flush half
    CANNOT work here. owncloud-flush.sh POSTs /v1/admin/owncloud-flush, which
    forces the SAME owncloud_sync.sweep(); sweep() calls _owned_agents() (~L1174),
    which under own-cloud returns only the agents this box holds a FRESH DDB
    RUNNING CLAIM for and is documented to return the EMPTY set otherwise ("own
    none this sweep -> no agent dir is pushed"). A worker box holds no claim by
    construction, so the sweep pushes zero agent dirs and the staged WM is
    stranded forever. Forcing the sweep changes its TIMING, never its SCOPE.
    A direct backend write bypasses the ownership prune entirely.

    Do NOT "fix" this by widening _owned_agents — that empty-set return is a
    deliberate fail-safe (a box that cannot prove it holds the claim must not
    push a peer's cached agent dir over the peer's newer S3 bytes), and it
    replaced a static allowlist that silently degraded to own-all.
    """
    unit_key = str(data.get("unitKey") or "").strip()
    if not unit_key:
        print("body-manifest: cannot stage — manifest has no unitKey",
              file=sys.stderr)
        return False
    staged_dir = state_dir / _STAGED_DIRNAME
    # (basename-suffix, bytes) for each file this Body owes the reducer.
    #
    # ORDER IS LOAD-BEARING — THE -wm.yaml TRIGGER MUST BE LAST. body-merge.py
    # L357 globs "*-wm.yaml" and derives BOTH sidecars from the matched
    # unit_key, so the WM's presence is what tells the reducer the unit is
    # ready. Write it before its sidecars and a reducer sweeping that window
    # consumes a trigger whose sidecars are missing: it merges 2-way union+SUM
    # (the counter double-count -c exists to remove) with Guard 2's
    # no-op short-circuit skipped — and then unlinks all three paths
    # (body-merge.py L401-403), so sidecars arriving afterwards are orphaned
    # PERMANENTLY: that glob is the staging dir's only enumerator, and nothing
    # else in the tree sweeps this directory. Same rule commit 15ade5039
    # established for the bash reap path (_preserve_unmerged_body_wm).
    #
    # The WM is READ first (a missing fork is fatal — early-return below) and
    # APPENDED last. Do not collapse those two steps back together.
    items: list[tuple[str, bytes]] = []
    try:
        wm_bytes = (session_dir / _WM_FILENAME).read_bytes()
    except OSError as exc:
        print(f"body-manifest: cannot stage forked WM: {exc}", file=sys.stderr)
        return False
    baseline_src = session_dir / _BASELINE_FILENAME
    if baseline_src.is_file():
        try:
            items.append((_STAGED_BASELINE_SUFFIX, baseline_src.read_bytes()))
        except OSError as exc:
            # Non-fatal: without it the reducer falls back to its existing
            # 2-way union+SUM merge (-c's 3-way branch keeps that
            # fallback), so a missing baseline degrades precision, not safety.
            print(f"body-manifest: baseline unreadable, staging WM only: {exc}",
                  file=sys.stderr)
    forked_hash = data.get("forked_wm_hash")
    if forked_hash:
        items.append((_STAGED_HASH_SUFFIX,
                      f"{forked_hash}\n".encode("utf-8")))
    items.append((_STAGED_WM_SUFFIX, wm_bytes))  # TRIGGER LAST — see above
    ok = True
    for suffix, body in items:
        target = staged_dir / f"{unit_key}{suffix}"
        try:
            _write_atomic_bytes(target, body)
        except OSError as exc:
            print(f"body-manifest: staging write failed for {target}: {exc}",
                  file=sys.stderr)
            ok = False
    return push_staged_files(staged_dir, unit_key) and ok


def push_staged_files(staged_dir: Path, unit_key: str) -> bool:
    """Explicitly push every staged file present for `unit_key`. Returns True
    iff all of them reached the backend (a file that does not exist is skipped,
    not a failure). Never raises.

    Shared by BOTH stagers so there is exactly one push implementation:
      - close_body_on_genuine (this module, remote_body genuine close)
      - cleanup-stale-bindings.sh's crash-preserve path, via the
        `push-staged` subcommand.

    That bash caller is annotated IRREDUCIBLY LOCAL (no python3) for latency,
    and this does not violate it: `_preserve_unmerged_body_wm` returns early
    when the Body forked no WM, so the subprocess is spawned ONLY when there is
    genuinely an orphaned Body WM to transport — rare, and the alternative is a
    permanently stranded WM.
    """
    try:
        # Local import mirrors this module's yaml/_resolve_machine_id style so
        # /start's write path and is-reducer never pay for it.
        from storage_backend import get_backend
        be = get_backend()
    except Exception as exc:  # noqa: BLE001 — transport must never raise here
        print(f"body-manifest: storage backend unavailable, staged files NOT "
              f"pushed ({type(exc).__name__}: {exc})", file=sys.stderr)
        return False
    ok = True
    # ORDER IS LOAD-BEARING — THE -wm.yaml TRIGGER IS PUSHED LAST, for the same
    # reason _stage_and_push WRITES it last (see the rationale there). The
    # window is wider here, not narrower: every write_bytes is a separate
    # backend round trip to a store a reducer on ANOTHER box polls, so a
    # trigger pushed first is remotely visible for the duration of two more
    # round trips before its sidecars land — and either of those can fail
    # independently, leaving the trigger published without them.
    for suffix in (_STAGED_BASELINE_SUFFIX, _STAGED_HASH_SUFFIX,
                   _STAGED_WM_SUFFIX):
        target = staged_dir / f"{unit_key}{suffix}"
        if not target.is_file():
            continue
        try:
            be.write_bytes(target, target.read_bytes())
        except Exception as exc:  # noqa: BLE001
            print(f"body-manifest: explicit push FAILED for {target} "
                  f"({type(exc).__name__}: {exc}) — staged locally; this Body's "
                  "WM will not reach the reducer until it is pushed",
                  file=sys.stderr)
            ok = False
    return ok


def close_body_on_genuine(sid: str, agent: str,
                          project_root: Path | None = None) -> str:
    """Mark a worker Body closed-pending-merge IFF this turn-end is a GENUINE close.

    Phase 2B (g-306-70): the stop-hook fires at EVERY not-runner turn-end, but a
    worker Body that does multiple work-units across turns must NOT be queued for
    merge after turn 1 — doing so loses turns 2+ of WM divergence (the reducer
    merges + marks `merged`, then the worker keeps diverging into a now-merged
    manifest that the sessions-pass never revisits). The genuine close is
    signalled by the worker writing a `body-closing` sentinel in its session dir
    when its loop truly terminates (no more work / final STOP). This helper is
    the small, testable decision the stop-hook delegates to (the stop-hook keeps
    a bash `[ -f sentinel ]` pre-guard so the dormant single-runner case stays
    zero-py3 — the sentinel never exists there).

    Returns one of:
      'no-forked-wm' — no per-Body WM file (a reducer/observer never forked) -> noop
      'no-sentinel'  — a mere between-turns turn-end (no sentinel) -> NOT closed
      'no-manifest'  — sentinel present but manifest missing (sentinel consumed) -> noop
      'bad-manifest' — sentinel present but manifest unparseable (consumed) -> noop
      'not-active'   — genuine close but body_state already != active (consumed) -> noop
      'marked'       — genuine close + active -> body_state set closed-pending-merge
                       (for remote_body: WM+baseline+hash staged AND pushed)
      'marked-push-failed' — as 'marked', but a remote Body's staging or explicit
                       push failed. State IS transitioned and the sentinel IS
                       consumed (the close really happened); the distinct string
                       exists so a silent transport failure is visible to the
                       stop-hook and to tests rather than reading as success.

    The sentinel is consumed (deleted) on every genuine-close branch so a re-fire
    cannot re-mark. Idempotent and fail-safe by design. 'bad-manifest' exists so
    that stays TRUE under a malformed manifest (g-306-119-b): before it, the
    YAMLError escaped this function entirely and the sentinel survived, so the
    condition re-fired at every later turn-end for that Body, forever.
    """
    _, session_dir, state_dir = _agent_paths(agent, sid, project_root)
    body_wm = session_dir / _WM_FILENAME
    if not body_wm.is_file():
        return "no-forked-wm"  # reducer/observer: never forked, nothing to close
    sentinel = session_dir / _CLOSE_SENTINEL_FILENAME
    if not sentinel.is_file():
        return "no-sentinel"  # between-turns turn-end, not a genuine close
    # Genuine close: consume the sentinel on every branch below.
    try:
        data = read_manifest(sid, agent, project_root)
    except FileNotFoundError:
        _unlink_quiet(sentinel)
        return "no-manifest"
    except ManifestParseError:
        # Consume the sentinel here too, or the turn-end condition re-fires for
        # this Body at EVERY subsequent turn-end and never clears (-b).
        _unlink_quiet(sentinel)
        return "bad-manifest"
    if data.get("body_state") not in CLOSEABLE_STATES:
        _unlink_quiet(sentinel)
        return "not-active"  # already closed/merged -> don't re-queue
    # PARKED IS CLOSEABLE (), and this line is load-bearing rather than
    # permissive. A park ends for real two ways — its 60h cap expires, or the user
    # stops the box — and BOTH route here. Under the old `!= "active"` test this
    # branch returned 'not-active', consuming the sentinel while staging nothing:
    # the manifest would sit at `parked` forever with its divergent WM stranded,
    # and the close would report as a benign no-op. `not-active` still means what
    # it says (a Body already closed or merged must never be re-queued); it simply
    # no longer means "not the string active".
    # FIX 1+2 (-b): a REMOTE Body's reducer lives on another box and
    # can never see this Body's sessions/<sid>/ dir (walk-pruned by
    # _EXCLUDE_DIRS), so marking alone strands the WM. Stage into session/
    # (singular, syncable) and push explicitly. Staged BEFORE set_state so a
    # staging failure cannot leave a manifest claiming closed-pending-merge
    # with nothing for the reducer to merge.
    pushed = True
    if data.get("remote_body"):
        pushed = _stage_and_push(session_dir, state_dir, data)
    set_state(sid, agent, "closed-pending-merge", project_root)
    _unlink_quiet(sentinel)
    return "marked" if pushed else "marked-push-failed"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("write", "read", "set-state", "is-reducer",
                 "close-body-on-genuine", "push-staged",
                 "park", "resume", "park-expired"):
        sp = sub.add_parser(name)
        sp.add_argument("--sid", required=True)
        sp.add_argument("--agent", required=True)
        if name == "write":
            sp.add_argument("--env-id", default="local")
            sp.add_argument("--role", default="worker", choices=VALID_ROLES)
            # choices= is the enforcement, not just help text: it makes an
            # invented SID a parse error at the CLI boundary rather than a
            # ValueError deeper in, so /start's CW1b cannot mis-address the
            # reducer-side merge with a plausible-looking value (-a).
            sp.add_argument("--reducer-sid", default=None,
                            choices=[REMOTE_REDUCER_SENTINEL],
                            help="'remote' activates the cross-box worker fork; "
                                 "a real cross-box reducer SID is unobtainable "
                                 "from this machine and must never be passed")
        if name == "set-state":
            sp.add_argument("state", choices=VALID_STATES)
        if name == "park-expired":
            sp.add_argument("--max-hours", type=float, default=PARK_MAX_HOURS)
    args = parser.parse_args(argv)

    try:
        if args.cmd == "write":
            path = write_manifest(args.sid, args.agent, args.env_id, args.role,
                                  reducer_sid=args.reducer_sid)
            print(path)
        elif args.cmd == "read":
            print(json.dumps(read_manifest(args.sid, args.agent)))
        elif args.cmd == "set-state":
            print(set_state(args.sid, args.agent, args.state))
        elif args.cmd == "is-reducer":
            print("true" if is_reducer(args.sid, args.agent) else "false")
        elif args.cmd == "close-body-on-genuine":
            print(close_body_on_genuine(args.sid, args.agent))
        elif args.cmd == "park":
            print(park_body(args.sid, args.agent))
        elif args.cmd == "resume":
            print(resume_body(args.sid, args.agent))
        elif args.cmd == "park-expired":
            # EXIT CODE, not stdout, is the contract — the caller is worker-loop
            # pseudocode branching in bash. 0 = expired (stop re-parking, take
            # the genuine close), 1 = not expired (keep parking). Text is for a
            # human reading the transcript. Note this inverts the usual
            # true-is-0 shell reading in the SAFE direction: any error path
            # below returns 2/3, which is neither, so a broken probe never reads
            # as "expired" and can never durably close a Body by accident.
            expired = park_expired(args.sid, args.agent,
                                   max_hours=args.max_hours)
            print("expired" if expired else "not-expired")
            return 0 if expired else 1
        elif args.cmd == "push-staged":
            # --sid IS the unitKey here. Used by cleanup-stale-bindings.sh's
            # crash-preserve path, which stages in bash and cannot reach the
            # storage backend itself. Exit 4 = staged locally but not pushed,
            # distinct from the validation (2) and io (3) codes so a caller can
            # tell "nothing to do" from "transport is down".
            _, _, state_dir = _agent_paths(args.agent, args.sid)
            ok = push_staged_files(state_dir / _STAGED_DIRNAME, args.sid)
            print("pushed" if ok else "push-failed")
            return 0 if ok else 4
    except (ValueError, FileNotFoundError) as e:
        print(f"body-manifest: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"body-manifest: io failed: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
