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
# this contract (_STAGED_HASH_SUFFIX, _STAGED_BASELINE_SUFFIX); it globs
# "*-wm.yaml" and derives unitKey by stripping that suffix. Keep these in sync
# with it.
#
#  SPLIT THE DIRECTORY IN TWO, and the reader knows about both: the
# PRODUCER now writes to `world_staged_dir()` (world-rooted, claim-EXEMPT —
# see its docstring for why the agent tree cannot work), while
# `_STAGED_DIRNAME` below is the LEGACY agent-tree path, kept because it still
# holds unmerged payload on at least three boxes. body-merge._consume_staged
# scans the UNION of the two and prefers the world copy when a unitKey appears
# in both. Do NOT "tidy" this back to one directory until the legacy dirs are
# measured empty fleet-wide. The baseline suffix is
# deliberately "-wm-baseline.yaml": it does NOT match the reader's "*-wm.yaml"
# glob, so a baseline can never be mis-consumed as a Body WM.
_STAGED_DIRNAME = "pending-body-merges"
# : LEGACY destination, now READ-ONLY. Kept because three boxes hold
# real unmerged payload under it (cc-09 13 files / 26,975,224 B / 4 Bodies,
# cc-08 21 / 14,336,845 / 6, cc-10 10 / 1,230,605 / 3, all measured
# first-person) and body-merge._consume_staged still unions it. Do NOT delete
# it or the files under it: cc-08 established that NONE of its six staged SIDs
# still has a session dir, so the staged triple is those Bodies' SOLE SURVIVING
# TRACE and archive-before-delete.md applies with full force.
_WORLD_STAGED_DIRNAME = "body-staged-wm"
_STAGED_WM_SUFFIX = "-wm.yaml"
_STAGED_BASELINE_SUFFIX = "-wm-baseline.yaml"
_STAGED_HASH_SUFFIX = "-wm.hash"
#  outcome 3: the CONSUMED TOMBSTONE. Written by
# body-merge._consume_staged (on the REDUCER) before it deletes a triple, read
# here (on the ORIGIN box) before re-staging or re-pushing one.
#
# WHY IT IS NEEDED, and why nothing before it closed the door: `_delete_staged`
# removes the store object AND a local file, but that local unlink runs on the
# REDUCER's filesystem. The origin box's own legacy copy is untouched by a
# remote consume, so a later `push-staged` for that unitKey finds no world-side
# copy, relocates the legacy one again, re-pushes it, and the reducer merges the
# SAME divergence twice — and a 3-way delta applied twice double-counts every
# counter. Before the destination moved to world/ that door was closed BY
# ACCIDENT: the push simply failed NoClaimError, so nothing ever reached the
# store to be resurrected. Fixing the push opened it.
#
# It does NOT end in "-wm.yaml", so the readers' `*-wm.yaml` glob and the
# `endswith("-wm.yaml")` listing filter cannot mis-consume a tombstone as a Body
# WM — the same disjointness the baseline suffix relies on. guard-2616 says to
# MEASURE that rather than assume it; test_body_staged_consumed_tombstone.py
# asserts it against both readers.
_STAGED_CONSUMED_SUFFIX = "-wm.consumed"


def world_staged_dir(agent_dir, world_dir=None) -> Path:
    """The staged-WM destination. Derived from the agent NAME, not the PATH.

    WHY THIS MOVED OUT OF THE AGENT TREE (g-115-9750). The original destination
    was `agents/<agent>/session/pending-body-merges/`, chosen because `session`
    (singular) is NOT walk-pruned by `owncloud_sync._EXCLUDE_DIRS` while
    `sessions` (plural) is — so it solved the SYNC problem, and the
    session-manifest registers all three globs `sync_tier: continuity`
    accordingly. It then ran straight into a SECOND, entirely separate guard:
    every write under `agents/<agent>/` is refused by the own-cloud claim fence
    unless this box holds the live runner claim, and a worker Body by definition
    never does. `push_staged_files` therefore raised `NoClaimError` on every
    push from every non-reducer box, forever — so the sync tier was correct and
    irrelevant, which is why four Bodies read the registration and still could
    not explain the stranding.

    The two in-tree candidates DEADLOCK, which is why no path under
    `agents/<agent>/` can work:
      - `session/`  (singular) — syncable, but CLAIM-FENCED.
      - `sessions/` (plural)   — claim-EXEMPT, but sync-excluded (machine-local).
    Neither is both. `world/` is both.

    THIS IS THE SAME MOVE g-306-420 ALREADY MADE FOR THE FASTLANE CARRIER, and
    deliberately so: `body_capture_carrier._world_carrier_dir` is the proven
    sibling, measured byte-identical at the new destination. The staged-WM lane
    simply never got it. Keep the two shaped alike.

    NOT COVERED BY THE PRIOR REJECTION. `body_capture_carrier.py` L47 records
    "WHY NOT SIMPLY SYNC sessions/ — rejected twice (g-306-119-b, g-115-6240)".
    That rejection is about syncing the PLURAL `sessions/` tree WHOLESALE, and
    neither of its reasons reaches here: the staged triple is not a second copy
    of a live file but the merge payload itself, which is MEANT to travel, and
    the per-box closure record is `sessions/<SID>/body-manifest.yaml`, which
    this does not touch.

    MIGRATION IS BY UNION, NOT BY MOVE. The legacy dir stays readable and
    `body-merge._consume_staged` scans BOTH, so nothing already staged is
    stranded and no box needs to migrate in lockstep. A box that has this
    commit has the producer AND the consumer; a box without it has neither. The
    one mixed state — producer updated, reducer not yet — is exactly the case
    where the old push ALREADY failed, so it cannot regress anything.
    """
    if world_dir is None:
        from _paths import WORLD_DIR
        world_dir = WORLD_DIR
    return Path(world_dir) / _WORLD_STAGED_DIRNAME / Path(agent_dir).name


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

# PARK-ORBIT BACKOFF ( part 4). A parked Body re-polls on a wakeup, and
# a full re-poll is the whole worker preamble (~25 iterations, measured ~1.75M
# tokens on 2026-09-01) — hourly, forever, on every Body the fleet has parked.
# ScheduleWakeup clamps delaySeconds to 3600, so the interval itself cannot
# grow; what grows is the interval between FULL polls. `park_count` climbs on
# every consecutive park and resets on resume, and `park-due` tells an
# off-cycle wakeup to re-arm cheaply and end the turn instead of polling.
# Schedule: 1h, 2h, 4h, then capped — a 60h park costs ~14 full polls instead
# of 60. Env overrides: PARK_BACKOFF_BASE_SECONDS / PARK_BACKOFF_MAX_SECONDS.
# The park cap (PARK_MAX_HOURS) still measures the WHOLE park from the original
# parked_at (guard-4184: what resets a stamp defines its meaning; only `resume`
# resets either).
PARK_BACKOFF_BASE_SECONDS = 3600
PARK_BACKOFF_MAX_SECONDS = 4 * 3600


def park_backoff_seconds(park_count: int) -> int:
    """Seconds between full re-polls after `park_count` consecutive parks.

    Doubles from the base per consecutive park (1h, 2h, 4h, ...), capped at
    PARK_BACKOFF_MAX_SECONDS. A count of 0 or less reads as the first park.
    """
    try:
        base = int(os.environ.get("PARK_BACKOFF_BASE_SECONDS", PARK_BACKOFF_BASE_SECONDS))
        cap = int(os.environ.get("PARK_BACKOFF_MAX_SECONDS", PARK_BACKOFF_MAX_SECONDS))
    except (TypeError, ValueError):
        base, cap = PARK_BACKOFF_BASE_SECONDS, PARK_BACKOFF_MAX_SECONDS
    n = max(int(park_count or 0), 1)
    return int(min(base * (2 ** (n - 1)), cap))

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


def _push_carrier(carrier: Path) -> bool:
    """Deliver the carrier to the backend NOW, rather than leaving it to the
    periodic sweep (g-115-9607).

    RETURNS WHETHER DELIVERY ACTUALLY LANDED, and this return is the ORIGIN of
    the delivery signal the whole chain above it reports (g-115-9607 unit 28).
    This function has always KNOWN the answer -- it catches the failure -- and
    threw it away, returning None and printing to stderr. That print is real but
    it is not a value: both call sites in cleanup-stale-bindings.sh redirect
    stderr (one `2>&1`) and branch on rc, which `main` pins at 0 for every
    outcome, so the diagnostic was unreachable by construction (guard-5501).
    Unit 26 measured the consequence on a worker box -- the reconcile fires, the
    push is refused `no_claim`, and rc=0 with empty stdout and empty stderr
    reports that as success. Returning the bool is what lets
    `_mirror_state_to_carrier` -> `_reconcile_orphan_carrier` ->
    `close_body_late` express it as a verdict a caller can read.

    THE FAIL-OPEN IS UNCHANGED AND MUST STAY UNCHANGED. This still swallows the
    exception, still prints, and still never raises into a closing caller; only
    the REPORTING is added. Each layer's fail-open is separately justified in its
    own comments (guard-373), and unit 26 declined to collapse them for that
    reason -- so this threads a value through them rather than removing them.

    THE LOCAL WRITE IS NOT DELIVERY, and a close is the one moment where that
    distinction is terminal. The carrier reaches peers only when the own-cloud
    daemon's periodic sync next runs (120s, rb-1464) -- but a Body's LAST tick
    happens before its close, so nothing on this Body ever writes again. If the
    box goes quiet before that sweep (a power-down, an lxc stop, a killed pane
    -- the exact population this goal is about), the close is written locally
    and the STORE keeps the last heartbeat-pushed value, `active`, forever.
    Every consumer then reads a phantom live Body, which is precisely the
    false-alert flood `_mirror_state_to_carrier` exists to prevent: the mirror
    was correct and simply never arrived.

    MEASURED 2026-09-11 on cc-07, by direct GetObject (not a read-through, which
    returns the local copy and cannot see this): Body 1dc6fc35 has read
    `closed-pending-merge` locally since 2026-09-01T20:05:02 while the store
    still returns `active` stamped 2026-08-27T16:47:56, object LastModified
    16:48:07. cc-08's 9a35daca reproduces it exactly, against a positive control
    (cd5fd3b9) that matches on both sides.

    The asymmetry this closes: the SAME close already pushes its staged WM
    explicitly, via `push_staged_files`, for this identical reason -- the state
    mirror was the half left to a daemon that may never run again. Cost is
    bounded and small: the three callers (set_state / park / resume) are a
    handful of transitions per Body lifetime, and heartbeat-tick.sh does NOT
    route through here -- it writes the carrier itself, on a box that is by
    construction still alive to be swept.

    Fail-open by contract, like every other step on a close path: a failure here
    degrades to today's behaviour (the sweep remains the backstop whenever the
    box stays up), never to a failed close. Broad `Exception` matches
    `push_staged_files`, whose contract is the same -- transport must never
    raise into a caller that is closing.
    """
    try:
        from storage_backend import get_backend
        get_backend().write_bytes(carrier, carrier.read_bytes())
        return True
    except Exception as exc:  # noqa: BLE001 — transport must never raise here
        print(f"body-manifest: carrier push FAILED for {carrier} "
              f"({type(exc).__name__}: {exc}) — body_state mirrored LOCALLY "
              "only; peers will keep reading this Body's previous state until "
              "the periodic sync runs, and never if this box goes quiet first",
              file=sys.stderr)
        return False


def _mirror_state_to_carrier(sid: str, agent: str, new_state: str,
                             project_root: Path | None = None) -> bool:
    """Mirror body_state into the SYNCABLE per-Body heartbeat carrier ().

    RETURNS True ONLY WHEN THE NEW STATE REACHED PEERS -- written locally AND
    delivered by `_push_carrier` (g-115-9607 unit 28). False covers both "there
    was nothing to mirror" (absent/unreadable/non-object carrier) and "mirrored
    locally, delivery refused". Those are deliberately NOT distinguished here
    because the one caller that reads this value, `_reconcile_orphan_carrier`,
    has ALREADY established that the carrier exists and reads `active` before it
    calls -- so under that caller a False can only mean a failed delivery. Any
    future caller that needs the distinction must establish it the same way or
    ask for a richer return; do not infer "delivery refused" from False alone.

    THIS WRITE IS WHAT KEEPS THE PEER-SIDE STALL PROBE FROM FLOODING, and it is
    the half that is easy to omit. heartbeat-tick.sh stamps the state on every
    tick, so a LIVE Body's carrier is current -- but a Body's last tick happens
    BEFORE its close, so without this mirror a cleanly-closed Body leaves a
    carrier still reading `active`. It then goes stale holding no claim, and
    worker_stall.classify_body -- correctly, on the evidence it has -- calls
    that a stall. Every clean close would become a false alert, which is the
    exact flood the split exists to prevent. The two writers ship together or
    neither ships.

    THE WRITE IS ONLY HALF OF IT -- writing the field locally is not the same as
    a peer being able to read it, and for five months it was treated as though
    it were. `_push_carrier` below delivers it; see there for the measurement
    (g-115-9607) showing a correctly-mirrored close that never left its box.

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
            return False
        doc = json.loads(carrier.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            return False
        doc["body_state"] = new_state
        _write_atomic(carrier, json.dumps(doc) + "\n")
        return _push_carrier(carrier)
    except (OSError, ValueError, TypeError):
        # json.JSONDecodeError subclasses ValueError; FileNotFoundError
        # subclasses OSError. Narrow on purpose -- a NameError or AttributeError
        # here is a logic bug and must not be swallowed as a benign skip.
        return False


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
        # Idempotent re-park: parked_at is preserved (the cap measures the whole
        # park) but the ORBIT advances — one more consecutive park means the next
        # full poll is due later ( part 4).
        _advance_park_orbit(data)
        _write_atomic(session_dir / _MANIFEST_FILENAME, _render_manifest(data))
        return "already-parked"
    if state != "active":
        return "not-active"
    data["body_state"] = "parked"
    data["parked_at"] = _now_iso_local()
    data["park_count"] = 0
    _advance_park_orbit(data)
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
    # The orbit resets with the clock: a resumed Body that parks again starts
    # back at the base interval ( part 4).
    data.pop("park_count", None)
    data.pop("park_next_poll_at", None)
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


def _advance_park_orbit(data: dict) -> None:
    """Bump park_count and stamp park_next_poll_at from the backoff schedule."""
    try:
        count = int(data.get("park_count") or 0)
    except (TypeError, ValueError):
        count = 0
    count += 1
    data["park_count"] = count
    due = datetime.datetime.now() + datetime.timedelta(seconds=park_backoff_seconds(count))
    data["park_next_poll_at"] = due.strftime("%Y-%m-%dT%H:%M:%S")


def park_due(sid: str, agent: str, project_root: Path | None = None) -> tuple[bool, int]:
    """(due, seconds_remaining) for a parked Body's next FULL re-poll.

    FAIL-SAFE TOWARD POLLING: a Body that is not parked, or whose
    `park_next_poll_at` is missing or unparseable, is DUE (True, 0). The
    alternative — treating an unreadable stamp as "not yet" — would let a
    field-format problem hold a Body in a cheap-wake orbit that never polls,
    which is a park with no exit. An extra poll costs one preamble; a missed
    one costs the fleet a worker. `seconds_remaining` is 0 when due.
    """
    data = read_manifest(sid, agent, project_root)
    if data.get("body_state") != "parked":
        return True, 0
    stamp = (data.get("park_next_poll_at") or "").strip()
    if not stamp:
        return True, 0
    try:
        due_at = datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError):
        return True, 0
    remaining = int((due_at - datetime.datetime.now()).total_seconds())
    if remaining <= 0:
        return True, 0
    return False, remaining


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
    # : world-rooted so the push is not refused by the claim
    # fence on a non-claim-holding box. state_dir is agents/<name>/session,
    # so its parent is the agent dir the resolver takes.
    staged_dir = world_staged_dir(state_dir.parent)
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


def unit_already_consumed(world_staged: Path, unit_key: str,
                          backend=None) -> bool:
    """True when a reducer has recorded `unit_key` as CONSUMED ().

    `world_staged` is the WORLD-ROOTED staged dir the tombstone lives in — the
    same directory the triple was pushed to. It is taken as a parameter rather
    than re-derived from a state_dir because the two callers hold different
    paths (relocate has `state_dir`, push has the world dir already), and
    re-deriving from the wrong one silently resolves to a directory that never
    contains a tombstone — a check that always answers "not consumed" and is
    indistinguishable from a working one (guard-5501).

    The tombstone lives beside the staged triple in the world-rooted dir, which
    is the only directory both boxes can reach: claim-EXEMPT so a worker may
    write it, syncable so a reducer's write is visible here.

    AUTHORITATIVE FIRST, LOCAL ONLY AS A FALLBACK. Under own-cloud the local
    tree is a read-through cache, so a bare `Path.exists()` answers a question
    about THIS box's cache rather than about the store — the same local-read
    mistake that made the pre-g-306-420 carrier look delivered while it was
    stranded (guard-980). The reducer that wrote this tombstone is on another
    box, so its write reaches here through the store or not at all.

    FAIL-OPEN, DELIBERATELY, AND THE DIRECTION IS THE WHOLE ARGUMENT. When the
    store is unreachable this returns False, i.e. "not consumed, go ahead and
    push". The two error directions are NOT symmetric: a false False re-pushes a
    triple and risks ONE double-merge, which `merge_wm`'s 3-way baseline already
    bounds to a counter re-add; a false True SILENTLY DROPS a Body's only copy of
    its divergence, which nothing recovers. Never invert this to fail-closed.
    """
    if backend is None:
        try:
            from storage_backend import get_backend
            backend = get_backend()
        except Exception:  # noqa: BLE001 — a missing backend is not consumed-ness
            backend = None
    marker = Path(world_staged) / f"{unit_key}{_STAGED_CONSUMED_SUFFIX}"
    if backend is not None:
        try:
            if marker.name in backend.list_dir(marker.parent.resolve()):
                return True
        except Exception:  # noqa: BLE001 — store listing is additive, never fatal
            pass
    return marker.is_file()


def relocate_legacy_staging(state_dir: Path, unit_key: str) -> list:
    """COPY a legacy-staged triple into the world-rooted destination.

    WHY THIS EXISTS (g-115-9750). `cleanup-stale-bindings.sh` is annotated
    IRREDUCIBLY LOCAL and stages in pure bash to
    `agents/<agent>/session/pending-body-merges/` — it has no `_paths.sh` and
    hand-mirrors `AGENTS_PARENT_DIR` rather than sourcing one, so resolving
    `WORLD_DIR` there would mean a third reimplementation of the
    env->conf->fallback chain in a second language (the SSOT failure
    `communication-clarity.md` rule 5 names). Moving the destination in
    `push-staged` alone would have left the bash staging in one directory
    while the push read another: `push_staged_files` skips an absent file and
    returns True, so the crash-preserve path would have reported success while
    transporting ZERO BYTES, and the bash `||` warning — the only signal that a
    Body's WM failed to reach the reducer — would never fire. A silent no-op is
    strictly worse than the loud NoClaimError it replaced.

    So the relocation happens HERE, inside the python3 subprocess the bash is
    already spending on this rare path, and the bash stays untouched. A LOCAL
    write is never claim-fenced (the fence lives in the backend's PUT), so
    staging locally to either directory always works; only the PUSH has to
    originate from a claim-exempt path.

    COPY, NEVER MOVE. A move is a delete of the original
    (archive-before-delete.md), and the legacy copy is some Bodies' sole
    surviving trace. The duplicate is not debt: it lands in exactly the
    shadowed-duplicate branch `body-merge._consume_staged` implements — world
    outranks legacy, and the legacy triple is retired on the SAME disposition
    as the copy actually read, never as a standalone delete.

    An existing world-side file is never overwritten: `close_body_on_genuine`
    stages the authoritative copy there, and it is the fresher writer.

    ORDER IS LOAD-BEARING — sidecars first, the `-wm.yaml` TRIGGER last, for
    the same reason every other stager in this module does it: a concurrent
    `_consume_staged` globs `*-wm.yaml`, so a trigger visible before its
    sidecars is silently consumed down a degraded path (no baseline -> 2-way
    union+SUM double-count; no hash -> the never-diverged no-op is skipped).

    Returns the basenames copied (empty when there was nothing to relocate).
    """
    legacy = state_dir / _STAGED_DIRNAME
    if not legacy.is_dir():
        return []
    #  outcome 3: a consumed unitKey is never re-staged. Checked HERE
    # as well as in push_staged_files, not redundantly: without this the legacy
    # triple is copied back into the world dir, and a box that later runs as
    # reducer globs that local copy and re-merges it WITHOUT any push at all.
    # Gating only the push would leave that second, purely-local resurrection
    # path open.
    world = world_staged_dir(state_dir.parent)
    if unit_already_consumed(world, unit_key):
        print(f"body-manifest: {unit_key} was already consumed by a reducer — "
              "not re-staging it (g-115-9750 re-push-after-consume guard)",
              file=sys.stderr)
        return []
    copied = []
    for suffix in (_STAGED_BASELINE_SUFFIX, _STAGED_HASH_SUFFIX,
                   _STAGED_WM_SUFFIX):
        src = legacy / f"{unit_key}{suffix}"
        if not src.is_file():
            continue
        dst = world / f"{unit_key}{suffix}"
        if dst.exists():
            continue
        try:
            world.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
            copied.append(dst.name)
        except OSError as exc:
            # Never fatal: the caller still pushes whatever DID land, and the
            # legacy copy is untouched, so nothing is lost by a failed copy.
            print(f"body-manifest: could not relocate {src.name} to the "
                  f"world-rooted staging dir ({type(exc).__name__}: {exc})",
                  file=sys.stderr)
    return copied


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
    #  outcome 3: THE SHARED CHOKEPOINT. Both stagers reach the store
    # through this one function, so gating here covers close_body_on_genuine and
    # cleanup-stale-bindings' crash-preserve path with one predicate.
    # Returns True, not False: the unit's content IS durably accounted for (a
    # reducer merged it), so this is a satisfied push, not a failed one. False
    # would make the bash caller print its "WM will not reach the reducer"
    # warning about a WM that already did.
    if unit_already_consumed(staged_dir, unit_key, backend=be):
        print(f"body-manifest: {unit_key} was already consumed by a reducer — "
              "skipping re-push (g-115-9750 re-push-after-consume guard)",
              file=sys.stderr)
        return True
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
    pushed = _mark_pending_merge(sid, agent, data, session_dir, state_dir,
                                 project_root)
    _unlink_quiet(sentinel)
    return "marked" if pushed else "marked-push-failed"


def _mark_pending_merge(sid: str, agent: str, data: dict, session_dir: Path,
                        state_dir: Path, project_root: Path | None) -> bool:
    """The ordinary close of a forked worker Body: stage if remote, then mark.

    Shared by close_body_on_genuine and close_body_late (g-115-9607) so a late
    close is the SAME close, not a second implementation of it. Returns False
    iff a remote Body's staging or explicit push failed.

    FIX 1+2 (g-306-119-b): a REMOTE Body's reducer lives on another box and
    can never see this Body's sessions/<sid>/ dir (walk-pruned by
    _EXCLUDE_DIRS), so marking alone strands the WM. Stage into session/
    (singular, syncable) and push explicitly. Staged BEFORE set_state so a
    staging failure cannot leave a manifest claiming closed-pending-merge
    with nothing for the reducer to merge.
    """
    pushed = True
    if data.get("remote_body"):
        pushed = _stage_and_push(session_dir, state_dir, data)
    set_state(sid, agent, "closed-pending-merge", project_root)
    return pushed


def _reconcile_orphan_carrier(sid: str, agent: str, truth_state: str | None,
                              project_root: Path | None = None) -> str | None:
    """Repair a CARRIER left reading `active` when the manifest no longer says so.

    RETURNS A VERDICT, not a bool (g-115-9607 unit 28), because a repair has
    THREE outcomes and the middle one was invisible:
      None                   — not a candidate; nothing was written.
      'repaired'             — written locally AND delivered to peers.
      'repaired-push-failed' — written locally, delivery REFUSED. On a worker
                               box this is the STEADY STATE, not an edge: the
                               carrier lives in the claim-fenced agent tree and
                               a non-claim-holding box is refused `no_claim`
                               (unit 22), so every repair here is local-only.
    It used to `return True` unconditionally once it decided to repair, which
    made the delivered and refused cases indistinguishable to every caller. That
    is the layer unit 26 identified as having to change FIRST: a caller taught to
    read a verdict that does not exist yet reads success and warns about nothing.
    A verdict string rather than a second bool matches this module's own idiom
    ('marked' / 'marked-push-failed' below) and leaves room for further outcomes.

    WHY THE LOCAL WRITE STILL HAPPENS ON A REFUSED PUSH: the manifest is the
    record of truth and it already says closed; the local carrier agreeing with
    it is correct in itself. What was wrong was reporting that as delivery. Note
    the consequence this does NOT fix, deliberately (unit 26 measured it): after
    the local write the carrier no longer reads `active`, so the call site's
    pre-filter skips it forever and there is no retry. Making it visible is this
    unit's scope; making it retry is not.

    The two early returns in close_body_late below — 'no-manifest' and
    'not-active' — were the only paths that touched nothing, and they are exactly
    the two states an orphan carrier is in. The manifest lives INSIDE the session
    dir and is deleted with it; the carrier lives in session/ (singular) and
    SURVIVES, so the file that keeps alerting is precisely the one those returns
    skipped. Measured cc-03 2026-09-12 (g-115-9607 unit 23): 3 carriers reading
    `active` with their session dir gone, one minted AFTER the g-306-430 publish
    fix — so this residue is made HERE, not by the ownership fence that unit 22
    root-caused.

    ONLY an `active` carrier is repaired, and that narrowness IS the safety
    argument. A carrier reading closed-* or parked is already correct or diverges
    harmlessly; overwriting those would let a `parked` manifest resurrect a closed
    reading, the one direction that can un-finish a Body.

    STALENESS IS PRESERVED BY CONSTRUCTION, and without that this change would be
    a net regression: worker_stall.classify_body returns V_ALIVE on FRESHNESS
    BEFORE it ever reads body_state, so a repair that looked like a tick would
    trade a false stall for a phantom LIVE Body — and fleet-live-bodies plus
    reducer_promotion's only_fresh_carrier_is_mine both ACT on that. Both read age
    from the DOC's `ts` (worker_stall.py ~776, reducer_promotion.py ~543), never
    the file mtime, and _mirror_state_to_carrier rewrites `body_state` alone, so
    `ts` survives untouched and the repaired carrier classifies V_STALE_NO_CLAIM
    (benign, never alerts) — which is the whole intended effect.

    Fail-open exactly like its callee (guard-373): an absent, unreadable or
    non-object carrier, or a state this module does not recognise, is a no-op
    and returns None.
    """
    if truth_state not in VALID_STATES or truth_state == "active":
        return None
    try:
        _, _, state_dir = _agent_paths(agent, sid, project_root)
        carrier = state_dir / f"body-heartbeat-{sid}.json"
        if not carrier.is_file():
            return None
        doc = json.loads(carrier.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or doc.get("body_state") != "active":
            return None
    except (OSError, ValueError, TypeError):
        return None
    # The carrier is proven present and `active` above, so a False here is a
    # DELIVERY refusal and never "nothing to mirror" — the precondition
    # _mirror_state_to_carrier's docstring requires before reading it that way.
    delivered = _mirror_state_to_carrier(sid, agent, truth_state, project_root)
    return "repaired" if delivered else "repaired-push-failed"


def _with_repair_verdict(base: str, repair: str | None) -> str:
    """Suffix a close_body_late verdict when its orphan repair did not deliver.

    Shared by the two early returns below so they cannot drift apart — they are
    the same case (an orphan carrier repaired on the way out) reached through
    two different manifest states, and unit 24 kept their return strings stable
    precisely because callers and three test files branch on them.

    ONLY the failure case changes the string. A repair that delivered, and a path
    where no repair was attempted at all, both return `base` unchanged, so every
    existing equality test and caller keeps its current behaviour.
    """
    return f"{base}-push-failed" if repair == "repaired-push-failed" else base


def close_body_late(sid: str, agent: str,
                    project_root: Path | None = None) -> str:
    """Close a Body whose session already ENDED without closing it ().

    The close above runs only in the turn-ending session's own stop hook, so a
    power-down, an lxc stop or a killed pane leaves `body_state: active` with no
    writer left alive to change it. This is that same close, run late. It does
    NOT decide that the session is gone — the caller must already have proved
    that on the box that ran it (abandoned_sessions.py owns the definition).
    Nothing is deleted: the session dir, its WM and its baseline all stay.

    Returns one of:
      'no-manifest' / 'bad-manifest' — nothing to close
      'not-active'  — parked or already closed: untouched. A park is RESUMABLE by
                      contract (g-306-291) and ends only through its own expiry,
                      so a late close never converts one into a close.
      'no-manifest-push-failed' / 'not-active-push-failed' — as the two above,
                      AND the orphan-carrier repair those paths perform was
                      written locally but REFUSED delivery (g-115-9607 unit 28).
                      The manifest side is identical; only the carrier's reach
                      differs, which is the whole point — the unsuffixed verdict
                      used to be returned in both cases, so a caller could not
                      tell a repair that reached peers from one that did not.
                      The SUFFIX is deliberate and is why existing callers keep
                      working: the success path still returns the bare string, so
                      only the failure case is new. A caller that must treat both
                      alike should test `.startswith('no-manifest')`, never
                      equality (guard-3274: enumerate before narrowing).
      'marked' / 'marked-push-failed' — a forked worker, closed exactly as
                      close_body_on_genuine closes one (staged if remote, then
                      closed-pending-merge, carrier mirrored). A leftover
                      body-closing sentinel is consumed with it.
      'marked-stale' — no forked WM (a reducer or observer Body). There is no
                      divergence to stage and nothing for a merge to consume, so
                      closed-pending-merge would be a false claim; closed-stale
                      is the closed value that says a sweep closed it.
    """
    _, session_dir, state_dir = _agent_paths(agent, sid, project_root)
    try:
        data = read_manifest(sid, agent, project_root)
    except FileNotFoundError:
        repair = _reconcile_orphan_carrier(sid, agent, "closed-stale",
                                           project_root)
        return _with_repair_verdict("no-manifest", repair)
    except ManifestParseError:
        return "bad-manifest"
    if data.get("body_state") != "active":
        repair = _reconcile_orphan_carrier(sid, agent, data.get("body_state"),
                                           project_root)
        return _with_repair_verdict("not-active", repair)
    if not (session_dir / _WM_FILENAME).is_file():
        set_state(sid, agent, "closed-stale", project_root)
        return "marked-stale"
    pushed = _mark_pending_merge(sid, agent, data, session_dir, state_dir,
                                 project_root)
    _unlink_quiet(session_dir / _CLOSE_SENTINEL_FILENAME)
    return "marked" if pushed else "marked-push-failed"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("write", "read", "set-state", "is-reducer",
                 "close-body-on-genuine", "close-body-late", "push-staged",
                 "park", "resume", "park-expired", "park-due"):
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
        elif args.cmd == "close-body-late":
            # Exposed for the BASH reap ( item c). The function had
            # one in-process caller (abandoned_sessions.py) and no verb, so
            # cleanup-stale-bindings.sh — which is bash — could not reach it and
            # every reap left a permanent carrier reading active.
            print(close_body_late(args.sid, args.agent))
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
        elif args.cmd == "park-due":
            # EXIT CODE is the contract, like park-expired: 0 = due (run the
            # full re-poll), 1 = not yet (re-arm the wakeup cheaply and END the
            # turn). The LAST stdout line is the integer seconds remaining (0
            # when due) so the caller can size the re-arm without parsing prose
            # (guard-4697). Errors return 2/3 — neither branch — and a broken
            # probe therefore reads as DUE at the caller (fail toward polling).
            due, remaining = park_due(args.sid, args.agent)
            print("due" if due else "not-due")
            print(remaining)
            return 0 if due else 1
        elif args.cmd == "push-staged":
            # --sid IS the unitKey here. Used by cleanup-stale-bindings.sh's
            # crash-preserve path, which stages in bash and cannot reach the
            # storage backend itself. Exit 4 = staged locally but not pushed,
            # distinct from the validation (2) and io (3) codes so a caller can
            # tell "nothing to do" from "transport is down".
            _, _, state_dir = _agent_paths(args.agent, args.sid)
            # : same world-rooted destination as _stage_and_push.
            # This is cleanup-stale-bindings.sh's crash-preserve entry, which
            # runs on whatever box owned the dead Body — usually a worker, so
            # a push from the legacy agent-tree path is refused NoClaimError.
            # The bash stages in the legacy dir and cannot resolve WORLD_DIR
            # (IRREDUCIBLY LOCAL), so bridge it here before pushing — see
            # relocate_legacy_staging for why this is a copy and why it is not
            # done in the bash.
            relocated = relocate_legacy_staging(state_dir, args.sid)
            ok = push_staged_files(world_staged_dir(state_dir.parent), args.sid)
            if relocated:
                print(f"relocated {len(relocated)} legacy-staged file(s) to the "
                      f"world-rooted staging dir", file=sys.stderr)
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
