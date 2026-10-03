#!/usr/bin/env python3
"""inbound_drain — drain an inbound instruction spool into the loop's own stores.

The DEFAULT engine behind the `inbound-drain` hook slot. A remote write route
may accept an instruction for an environment whose loop is stopped and QUEUE it
(accepted-not-applied) at ``<spool-root>/<environment-key>/inbound/<ts>-<uuid>.json``.
Nothing applies a queued record until this engine runs, at a loop-iteration
boundary.

WHY THIS LIVES IN core/ (it did not, until 2026-09-07)
------------------------------------------------------
The original engine sat in the domain's ``world/scripts`` and argued, in its own
docstring, that a core-side drain would have to leak domain names or have every
one of them injected. The second horn is the right trade, and the argument
omitted the fact that settled it: ``world/`` is excluded from the seed by policy
and is not in git, so a drain that lives only there is perfectly placed and
NEVER PRESENT on the environments it exists to serve — measured 0 of 45 live
workspaces. Everything it actually depends on (the verb applier, the typed
client) was already in core, so the dependency argument was never load-bearing;
only configuration was, and configuration is what env vars and flags are for.

This engine therefore names NO domain artifact. The spool root and the target
aspiration arrive as flags or generic environment variables, and a slot decides
whether this box should drain at all. The domain's ``world/scripts`` slot stays
the OVERRIDE (Pattern B, core/config/conventions/domain-hooks.md); this file is
what runs when a world does not fill the slot.

CONFIGURATION IS GENERIC, DELIBERATELY, AND THERE ARE NO ALIASES
----------------------------------------------------------------
``INBOUND_SPOOL_ROOT`` and ``INBOUND_DIRECTIVE_ASP_ID`` are the only environment
variables read here. A domain that provisions its environments under different
names maps them IN ITS SLOT — that translation is the slot's whole job. Accepting
a domain name here as an "alias" would be a fallback chain, which is an
enumeration claim about where a value may live (guard-3970) and re-leaks the name
this move exists to remove.

NOTHING IS EVER DELETED
-----------------------
Every record ends in a sibling directory of ``inbound/`` — ``processed/``,
``rejected/``, ``failed/`` or ``quarantine/``. archive-before-delete binds here (a
queued record is a person's instruction and cannot be regenerated), and the
cheapest way to satisfy it is to never delete a record at all. Moves are ``os.replace``
within one filesystem, so each is atomic. The one file this engine removes is its own
lock (see ONE DRAIN PER ENVIRONMENT AT A TIME), which holds no instruction, and, since
g-335-1726 u4, the retained copy of a forgotten item once its undo window has closed (see
THE 30-DAY ERASE). That copy is not a queued instruction: the owner's ruling is that it is
erased, and an archive of it would be the thing the ruling removes.

THE 30-DAY ERASE (g-335-1726 u4)
--------------------------------
After the queued records are done, and inside the same lock, an applying drain runs
``knowledge_erase.sweep`` over ``<env>/retention``: every retained record whose undo window
is over has the world's side finished (a page blanked, a ghost index entry dropped, a
hypothesis or guardrail record reduced to its identity), is deleted through the storage backend, and
leaves a receipt in ``<env>/erasures.jsonl`` that holds no text. It runs only where the
directory exists, so an environment that never handled a forget is untouched, and it takes
the same two fences a forget does. A dry run reports what it would erase and changes
nothing. The result carries ``erased``, ``erase_pending`` (an obligation that is due and that
no change to this box can meet, such as a guardrail on a backend that can merge its store with
another copy, where the store refuses the erase)
and ``erase_failed`` (a step did not finish, so the retained record is kept and the next pass
tries again), and the last two make the drain exit 2. The sweep shows the lock is still held
before each record and each write, so a pass that outlasts ``LOCK_STALE_SECONDS`` is not
taken for a dead one; a dry run holds no lock and shows nothing.

THE CLAIM STEP IS LOAD-BEARING, NOT CEREMONY
--------------------------------------------
Each record is moved into ``processing/`` BEFORE it is applied. That rename is
atomic, so two drains racing on the same spool cannot both win it (rb-197). It
also bounds the failure mode: a crash between apply and completion leaves the
record in ``processing/`` where it is visible and is NOT silently re-applied.
``--requeue-stale`` is a deliberate operator act, because re-applying a
half-applied instruction is a judgment call and not a sweep's to make.

``.tmp-*`` RESIDUE IS NEVER PARSED
----------------------------------
The writer creates ``.tmp-XXXX.json`` in the SAME directory and ``os.replace``s
it onto the final name, so a partial FINAL file is impossible. But the temp name
also ends in ``.json``, so enumeration excludes every dotfile EXPLICITLY rather
than relying on ``glob``'s incidental dotfile skip.

THE DESTINATION FENCE (the reason a misconfigured box cannot steal instructions)
--------------------------------------------------------------------------------
A directive is filed through the LOCAL runtime, which has no destination
argument: it files into whatever queue this box owns. The slot's guard is
SOURCE-side (it decides which spool is read) and cannot constrain that. So a box
that is not the environment it claims to be — two env vars set on a fleet box —
would pass the slot guard and file a member's words into its own queue, silently,
because a low-numbered aspiration id resolves almost everywhere. ``_destination_fence``
refuses that: the filing box's own world root must live UNDER the spool root being
drained, which is true on a real environment host and false on a fleet box. It
fails CLOSED and it fails LOUD (the record is left claimed and reported), because
the alternative failure is invisible.

A KNOWLEDGE RECORD IS A MEMBER'S EDIT OR FORGET OF ONE LEARNED ITEM (g-335-1726)
------------------------------------------------------------------------------
An edit goes to ``knowledge-edit-apply.py`` with the record's ``handle``, ``text``
and ``base`` (the digest of the view the member corrected), and the applier is
the authority on every refusal, a missing or stale base included. A forget or an
undo goes to the same applier with ``--retention-dir`` set to ``<env>/retention``:
the text a forgotten item leaves behind is kept there for the undo window, beside
the lanes and outside the resident's world, so the resident cannot read what the
member took back from it. This engine derives that directory and the applier never
chooses it, and the engine REFUSES (FAILED, the record stays claimed) when the
directory and the world root contain one another, since a retention directory inside
the world defeats what it is for.
An edit lands in THIS box's world exactly as a directive lands in this box's queue,
so the destination fence below covers it too.

ONE DRAIN PER ENVIRONMENT AT A TIME (g-335-1726)
------------------------------------------------
Claiming a record by renaming it keeps one record from two drains and nothing else.
A member's undo can run while their forget sits between its marker and its blank, and
the forget's late blank then writes the tombstone over the restored text; two undos of
one guardrail each add a copy. So a drain that applies takes the environment's lock
(``.drain.lock``, beside the lanes), and a second drain of that environment reports
``busy`` and leaves its records queued for the next pass. A dry run writes nothing and
takes none. A holder that died leaves a lock that is broken after LOCK_STALE_SECONDS; a
drain touches its lock before each record, so a long backlog does not lose it by age; a
single record that outlasts LOCK_STALE_SECONDS (a hung applier) still can.

A HANDLE THIS BOX CANNOT RESOLVE IS NOT A REFUSAL
------------------------------------------------
A verb and a knowledge edit name their target by a member handle. Both resolvers
read a box without KNOWLEDGE_HANDLE_SECRET or ENVIRONMENT_ID as "no such item", and
their applier would refuse (rc 3) — REJECTED, terminal, over a gap on THIS side. So
such a record is left in ``inbound/``, unclaimed, and counted as unprovisioned: the
unconfigured-directive shape again, and it drains by itself once the box is
provisioned. The export publishes handles only where both values exist, so this
fires only where the publisher and the drain disagree.

A FINISHED KNOWLEDGE RECORD CARRIES ITS OUTCOME (g-335-1726)
-----------------------------------------------------------
The member is told what became of an edit from the record itself. Before a knowledge
record moves to ``processed/`` or ``rejected/`` the drain writes ``outcome`` into it:
``result`` (``applied``, the applier's refusal code, or ``malformed_record`` for a record
the drain refuses before the applier runs), ``at``, ``new_handle`` when a guardrail was
superseded and the correction lives under a new handle, and ``undo_until`` when the record
was a forget (the end of the window in which it can be taken back). Item ids never enter
it: a handle is opaque on purpose. The write is a ``.tmp-*`` sibling and an ``os.replace``,
the writer's own shape, so a crash leaves the record whole, and it keeps every field it
was queued with. THE LANE IS THE AUTHORITY and the outcome only explains it: a record in
any other lane is unfinished whatever it carries (a requeued record keeps its old outcome
until the next terminal write replaces it). A failed record stays claimed with none.

EXIT CODES (keep in sync with the argument parser below):
  0  drain ran; every record reached a terminal directory, or the spool was empty
  1  usage error, or the root/environment does not exist
  2  drain ran but needs attention: a record failed transiently and stayed claimed,
     a directive was left queued because no target aspiration is configured, a
     handle-addressed record was left queued because this box cannot resolve handles,
     and/or an erase is due and unmet (``erase_pending``) or failed (``erase_failed``)
     (a busy environment is not on this list: its records drain on the next pass)

THE TARGET ASPIRATION HAS NO DEFAULT, ON PURPOSE
------------------------------------------------
Which aspiration a free-text directive belongs under is a per-environment
decision and is not the drain's to make. Without ``--aspiration`` (or
``INBOUND_DIRECTIVE_ASP_ID``) directives are left in ``inbound/`` UNCLAIMED —
visible, non-consuming, and self-draining once configured. A default here would
silently file a person's words into a guessed aspiration, which is the one
failure on this path that nobody would ever see. Verbs are unaffected: a verb
names its own target through the opaque handle it carries.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import importlib.util
import json
import os
import stat
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# This file IS in core/scripts, so the applier sits beside it. The world engine
# had to walk parents to find core; that search is exactly the coupling this move
# removes.
CORE_SCRIPTS = SCRIPT_DIR

# NO PRODUCT DEFAULT. The world engine defaulted to a specific mount path, which
# is the kind of name core may not carry -- and a wrong default is worse than an
# absent one here, since it would point the drain at a directory that does not
# exist and report "no environment" rather than "not configured".
SPOOL_ROOT_ENV = "INBOUND_SPOOL_ROOT"
DIRECTIVE_ASP_ENV = "INBOUND_DIRECTIVE_ASP_ID"

INBOUND = "inbound"
PROCESSING = "processing"
PROCESSED = "processed"

# A record sitting in processing/ older than this is reported as STRANDED.
# Chosen against the measured population (): a legitimately in-flight
# record is seconds old, and the one real stranding ran 2h. 30m is far above
# the former and far below the latter.
STRANDED_AGE_MIN_DEFAULT = 30
REJECTED = "rejected"
FAILED = "failed"
QUARANTINE = "quarantine"

KNOWN_KINDS = ("verb", "directive", "knowledge")


def _load_known_verbs() -> tuple:
    """Read the verb vocabulary FROM THE REGISTRY, never from a restatement.

    The world engine hardcoded a four-verb tuple and carried a comment saying it
    "mirrors" the route handler's list -- two copies of a vocabulary with nothing
    failing when they diverge (guard-1220). ``planned_verbs.PLANNED_VERBS`` is the
    registry the applier itself dispatches on, so deriving from it cannot drift.

    An empty tuple means the registry was unreadable, and the pre-filter is then
    SKIPPED rather than rejecting everything: this was only ever a pre-filter that
    keeps a typo out of the applier's stderr. The applier is the authority and
    refuses an unknown verb itself (rc=3).
    """
    try:
        from planned_verbs import PLANNED_VERBS  # noqa: PLC0415
        return tuple(sorted(PLANNED_VERBS))
    except Exception:  # noqa: BLE001 - registry unreadable; applier still gates
        return ()


KNOWN_VERBS = _load_known_verbs()


def _load_known_knowledge_ops() -> tuple:
    """The knowledge ops FROM THE REGISTRY, for the reason ``_load_known_verbs``
    gives: ``knowledge_edits.KNOWLEDGE_OPS`` is what the applier dispatches on.
    Empty when unreadable, and the pre-filter is then skipped (the applier refuses
    an unknown op itself)."""
    try:
        from knowledge_edits import KNOWLEDGE_OPS  # noqa: PLC0415
        return tuple(KNOWLEDGE_OPS)
    except Exception:  # noqa: BLE001 - registry unreadable; applier still gates
        return ()


KNOWN_KNOWLEDGE_OPS = _load_known_knowledge_ops()
# The two ops that keep what a member took out, and so are handed a retention directory.
RETENTION_OPS = ("forget", "undo")
RETENTION = "retention"
LOCK_NAME = ".drain.lock"
# A drain takes seconds to a minute or two, so a lock this old was left by a holder that
# died, and the next drain breaks it.
LOCK_STALE_SECONDS = 600
# The kinds that name their target by a member handle rather than carrying it.
HANDLE_KINDS = ("verb", "knowledge")


def _own_world_root() -> Path | None:
    """The world root THIS box files into. None when unresolvable."""
    try:
        import _paths  # noqa: PLC0415
        w = getattr(_paths, "world_path", None)
        if callable(w):
            return Path(w()).resolve()
        for attr in ("WORLD_PATH", "WORLD_DIR"):
            v = getattr(_paths, attr, None)
            if v:
                return Path(v).resolve()
    except Exception:  # noqa: BLE001
        pass
    v = os.environ.get("WORLD_PATH") or os.environ.get("WORLD_DIR")
    return Path(v).resolve() if v else None


def _destination_fence(spool_root: Path) -> str | None:
    """Return a refusal reason, or None when filing here is legitimate.

    FAILS CLOSED. An unresolvable world root is a refusal, not a pass: the whole
    point is that the destination is implicit, so "I could not tell where this
    would land" is precisely the state in which it must not land anywhere.
    """
    own = _own_world_root()
    if own is None:
        return ("destination fence: cannot resolve this box's own world root, so "
                "the filing destination is unknown -- refusing rather than filing "
                "a queued instruction into an unidentified queue")
    try:
        root = Path(spool_root).resolve()
    except OSError as exc:
        return f"destination fence: unresolvable spool root {spool_root}: {exc}"
    if own == root or root in own.parents:
        return None
    return ("destination fence: this box's world root (%s) is not under the spool "
            "root being drained (%s), so this box is not the environment that owns "
            "these records; filing them here would silently misroute another "
            "member's instructions into this queue" % (own, root))


def _retention_refusal(retention_dir: Path | None) -> str | None:
    """Why this drain cannot hand the applier ``retention_dir``, or None.

    The directory keeps what a member took out of what the resident learned, so it must lie
    OUTSIDE the resident's world: inside it, the resident reads the very text the member
    asked it to forget, and nothing would look wrong. The check is made here, where the
    directory is derived. It FAILS CLOSED, like the destination fence: a world root this
    box cannot resolve is a refusal, not a pass.
    """
    if retention_dir is None:
        return "a forget or an undo reached the applier with no retention directory"
    own = _own_world_root()
    if own is None:
        return ("retention fence: cannot resolve this box's own world root, so it cannot "
                "tell whether the retention directory is outside it")
    try:
        kept = Path(retention_dir).resolve()
    except OSError as exc:
        return f"retention fence: unresolvable retention directory {retention_dir}: {exc}"
    if kept == own or own in kept.parents or kept in own.parents:
        return (f"retention fence: the retention directory ({kept}) and this box's world root "
                f"({own}) contain one another, so what a member forgot would sit where the "
                "resident reads")
    return None


class DrainError(RuntimeError):
    pass


# --- the three appliers -----------------------------------------------------
# All are reached through their CANONICAL path. None is reimplemented here: a
# second copy of the verb or knowledge decision core, or of the goal-filing rules,
# would drift from the original and nothing would fail when it did.


def _load_core_module(filename: str, module_name: str):
    """A hyphenated core module needs the importlib shape.

    Called IN-PROCESS rather than through ``subprocess.run(['bash', ...])``:
    guard-744 / guard-555 record that a nested subprocess misses the PreToolUse
    env/PATH injection the daemon wrappers depend on, and each applier already
    reaches its own writes correctly (guard-580 via _runtime_bash.BASH, or _rt).
    Calling its ``main()`` gets the canonical code path with no extra layer.
    """
    # No "cannot locate core/scripts" branch: this engine LIVES in core/scripts,
    # so CORE_SCRIPTS is this file's own directory and cannot be unset. The world
    # engine needed that search (and its failure mode) only because it sat outside
    # the repo -- removing it is part of the move, not an unrelated tidy.
    path = CORE_SCRIPTS / filename
    if not path.is_file():
        raise DrainError(f"applier missing: {path}")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - import plumbing
        raise DrainError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_verb_applier():
    return _load_core_module("planned-verb-apply.py", "planned_verb_apply_for_drain")


def _load_knowledge_applier():
    return _load_core_module("knowledge-edit-apply.py", "knowledge_edit_apply_for_drain")


def _handles_provisioned() -> bool | None:
    """Whether this process can resolve member handles, asked of the export module both
    appliers resolve through. ``None`` when that module cannot answer: the record then
    goes to its applier, which fails on the same import (FAILED, the record stays claimed)."""
    try:
        export = _load_core_module("knowledge-export.py", "knowledge_export_for_drain")
        return bool(export.handles_provisioned())
    except Exception:  # noqa: BLE001 - the answer is optional; the applier still gates
        return None


def _run_applier(mod, argv: list[str], refused: str,
                 report: dict | None = None) -> tuple[str, str]:
    """Map an applier's exit code to a disposition. ``refused`` explains an rc=3.

    ``report`` goes to an applier that fills one (knowledge-edit-apply.py): its own
    refusal code then replaces ``refused``, and the caller reads what landed from it.

    The applier's own lines go to stderr. Under ``--json`` this process's stdout is
    one JSON document that the runner parses whole, and an applied edit's line ahead
    of it made every such run read as unparseable.
    """
    try:
        with contextlib.redirect_stdout(sys.stderr):
            rc = mod.main(argv) if report is None else mod.main(argv, report)
    except SystemExit as exc:  # argparse, or a fatal exit inside the applier
        # Python's own rule: no code is 0, and a message (an unset WORLD_PATH, say)
        # is 1. int() of a message would raise out of the drain with the record
        # claimed.
        code = exc.code
        rc = code if isinstance(code, int) else (0 if code is None else 1)
    except Exception as exc:  # noqa: BLE001 - the applier is a boundary
        return FAILED, f"applier raised {type(exc).__name__}: {exc}"

    if rc == 0:
        return PROCESSED, "applied"
    if rc == 3:
        # Terminal by construction — the same record will be refused identically
        # forever, so retrying it is a busy-loop.
        return REJECTED, f"applier refused (rc=3): {(report or {}).get('refused') or refused}"
    return FAILED, f"applier rc={rc}"


def _apply_verb(record: dict, source: str) -> tuple[str, str]:
    """Return (disposition, detail). Disposition is processed|rejected|failed."""
    handle = record.get("handle")
    verb = record.get("verb")
    value = record.get("value", "")
    if not isinstance(handle, str) or not handle.strip():
        return REJECTED, "handle missing or not a string"
    if KNOWN_VERBS and verb not in KNOWN_VERBS:
        return REJECTED, f"unknown verb {verb!r}"

    mod = _load_verb_applier()
    argv = ["--handle", handle, "--verb", verb, "--value", str(value or ""),
            "--source", source, "--apply"]
    # plan_verb refuses an unresolvable handle, or a goal that is not member_writable.
    return _run_applier(mod, argv, "unresolved handle or not member-writable")


def _apply_knowledge(record: dict, *, spool_root: Path, retention_dir: Path | None = None,
                     report: dict | None = None) -> tuple[str, str]:
    """A member's edit, forget or undo of one learned item -> knowledge-edit-apply.py
    (g-335-1726).

    The record carries ``handle``, ``op``, ``text`` and ``base``. ``base`` is
    forwarded as it came, empty when absent: the applier refuses an edit whose base
    is missing or no longer matches the item's view (rc=3, terminal), and a second
    copy of that rule here would drift from it. A forget or an undo is also handed
    ``retention_dir`` (see ``_retention_refusal``); an edit is not, so its argv is what
    it was before forgets were applied. ``report`` receives the applier's own account
    of the op, for ``_knowledge_outcome``.
    """
    fence = _destination_fence(spool_root)
    if fence is not None:
        # FAILED, never REJECTED, as for a directive: the record is legitimate and
        # belongs to the box that owns this spool.
        return FAILED, fence

    handle = record.get("handle")
    op = record.get("op")
    text = record.get("text", "")
    base = record.get("base", "")
    if not isinstance(handle, str) or not handle.strip():
        return REJECTED, "handle missing or not a string"
    if KNOWN_KNOWLEDGE_OPS and op not in KNOWN_KNOWLEDGE_OPS:
        return REJECTED, f"unknown op {op!r}"
    if not isinstance(text, str):
        return REJECTED, "text is not a string"
    if not isinstance(base, str):
        return REJECTED, "base is not a string"

    # --text= and --base= keep a value that starts with "-" from being read as a flag.
    argv = ["--handle", handle, "--op", op, f"--text={text}", f"--base={base}"]
    if op in RETENTION_OPS:
        why = _retention_refusal(retention_dir)
        if why is not None:
            # FAILED, never REJECTED: the record is legitimate and the gap is on this side.
            return FAILED, why
        argv.append(f"--retention-dir={retention_dir}")
    argv.append("--apply")
    mod = _load_knowledge_applier()
    return _run_applier(mod, argv, "unresolved handle, a missing or stale view base, or "
                                   "a kind or text its store cannot take", report)


def _knowledge_outcome(disposition: str, report: dict) -> dict:
    """What the member is told about a knowledge record that finished as ``disposition``.

    Only the opaque handles and the refusal code leave the applier's ``report``: the
    item ids in it are what a handle exists to hide.
    """
    if disposition == PROCESSED:
        outcome = {"result": "applied"}
        if report.get("handle"):
            # A guardrail is superseded, not rewritten: the correction has a new handle.
            outcome["new_handle"] = report["handle"]
        if report.get("undo_until"):
            # The end of the window in which a forget can be taken back.
            outcome["undo_until"] = report["undo_until"]
    else:
        # The applier names every refusal it makes, so a rejection without its code is
        # the drain's own: a record the writer would never have sent.
        outcome = {"result": report.get("refused") or "malformed_record"}
    outcome["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return outcome


def _apply_directive(record: dict, source: str, asp_id: str, *,
                     spool_root: Path) -> tuple[str, str]:
    """File an Assigned free-text directive as a goal via the typed client.

    guard-555: a Python parent must reach a daemon-only mutation through
    ``_rt`` (pure urllib) or ``_runtime_bash.bash_cmd`` — never a bare
    ``subprocess.run(['bash', wrapper])``, which resolves the WSL stub or loses
    the PATH the wrapper's port discovery needs.
    """
    fence = _destination_fence(spool_root)
    if fence is not None:
        # FAILED, never REJECTED: the record is legitimate and must stay
        # recoverable. A misconfigured box refuses loudly and leaves the work
        # for the box that actually owns it.
        return FAILED, fence

    if not asp_id:
        # Defence in depth: drain_environment skips unconfigured directives before
        # claiming them, so reaching here means a caller invoked this helper
        # directly. FAILED, never REJECTED — a config gap must not consume a
        # member's instruction.
        return FAILED, "no target aspiration configured (caller bypassed the pre-claim check)"

    text = record.get("text")
    if not isinstance(text, str) or not text.strip():
        return REJECTED, "text missing or empty"

    try:
        import _rt  # noqa: PLC0415 - deliberately lazy; keeps --dry-run importable
    except Exception as exc:  # noqa: BLE001
        return FAILED, f"cannot import _rt: {exc}"

    env_key = record.get("environmentKey") or "unknown-environment"
    queued_at = record.get("queued_at") or ""
    goal = {
        "title": f"Assigned directive: {text.strip()[:80]}",
        "description": (
            f"{text.strip()}\n\n"
            f"---\nFiled by mind-inbound-drain from the inbound spool. "
            f"environmentKey={env_key} accountId={record.get('accountId')} "
            f"queued_at={queued_at} source={record.get('source')}. "
            f"The member wrote this through the public write route while the "
            f"environment may have been stopped; it was queued, not applied, until "
            f"this drain ran."
        ),
        "priority": "MEDIUM",
        "participants": ["agent"],
        # The member IS the user: a queued directive is user-originated work,
        # and the daemon's origin-signal gate (add-goal Phase C) refuses every
        # agent-sourced filing that carries no registered signal. Without this
        # line the vessel daemon answered 400 origin_signal_blocked and the
        # member's directive stayed claimed in processing/ (measured on the
        # 2026-09-08T06:06Z cold start of pearl-test-20260904-g3351459,
        # ). "Assigned directive:" is not a Layer-D auto-derive title
        # prefix, so the signal has to be explicit here.
        "origin_signal": "user_directive",
    }
    try:
        resp = _rt.aspirations_add_goal(asp_id, goal, source=source)
    except Exception as exc:  # noqa: BLE001 - RtError and transport errors alike
        # Surface the daemon's OWN reason. RtError CARRIES the structured error
        # payload on .body (core/scripts/_rt.py reads err_body off the HTTPError
        # and passes body=), but its __str__ is only "daemon HTTP <code> for
        # <method> <path>". Rendering {exc} alone discarded the one field naming
        # WHICH of add-goal's six 400 paths fired (missing_param / invalid_asp_id
        # / invalid_source / invalid_body / validation_failed /
        # unknown_goal_field) — measured 2026-09-08 (), where a real
        # member directive failed with a bare "daemon HTTP 400" that cost a paid
        # vessel run its diagnosis. The reason turned out to be
        # {"error": "origin_signal_blocked", ...}, fixed by the explicit
        # "origin_signal" in the payload above.
        # guard-1661: a write path must return caller-verifiable evidence, not
        # a bare status.
        body = getattr(exc, "body", None)
        detail = f" body={str(body).strip()[:400]}" if body else ""
        return FAILED, f"add-goal failed: {type(exc).__name__}: {exc}{detail}"
    gid = ""
    if isinstance(resp, dict):
        gid = str(resp.get("goal_id") or resp.get("id") or "")
    return PROCESSED, f"filed {gid}".strip()


# --- spool mechanics --------------------------------------------------------


def _lane(env_dir: Path, name: str) -> Path:
    d = env_dir / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _move(src: Path, dest_dir: Path) -> Path:
    """Atomic within one filesystem. Never deletes; collisions are suffixed."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists():
        dest = dest_dir / f"{src.stem}.{int(time.time()*1000)}{src.suffix}"
    os.replace(src, dest)
    return dest


def _record_outcome(claimed: Path, record: dict, outcome: dict) -> None:
    """Write ``outcome`` into the claimed record, keeping every field it was queued with.

    A ``.tmp-*`` sibling then ``os.replace``, as the writer does, so a crash leaves the old
    record whole and at worst a ``.tmp-*`` that the stranded scan reports. The mode is
    copied because the temp file is created private, and a reader on the writer's side
    need not own the record.
    """
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=claimed.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({**record, "outcome": outcome}, fh)
    os.chmod(tmp, stat.S_IMODE(claimed.stat().st_mode))
    os.replace(tmp, claimed)


def _record_files(inbound: Path) -> list[Path]:
    """Queue order, temp residue excluded.

    Dotfiles are excluded EXPLICITLY. The writer's temp name is
    ``.tmp-XXXX.json`` — it ends in ``.json`` like a real record, so the filter
    cannot key on the suffix, and relying on ``glob``'s incidental dotfile skip
    would put the whole guarantee on an implementation detail of the stdlib.
    """
    if not inbound.is_dir():
        return []
    out = []
    for name in os.listdir(inbound):
        if name.startswith("."):
            continue
        if not name.endswith(".json"):
            continue
        p = inbound / name
        if p.is_file():
            out.append(p)
    # Names are <utc-timestamp>-<uuid>.json, so lexical order IS queue order.
    return sorted(out, key=lambda p: p.name)


def _tmp_residue(inbound: Path, age_min: int) -> list[Path]:
    """``.tmp-*`` older than age_min. Fresher residue may be an in-flight write."""
    if not inbound.is_dir():
        return []
    cutoff = time.time() - (age_min * 60)
    out = []
    for name in os.listdir(inbound):
        if not name.startswith(".tmp-"):
            continue
        p = inbound / name
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                out.append(p)
        except OSError:
            continue
    return sorted(out, key=lambda p: p.name)


def stranded_records(env_dir: Path, age_min: int = STRANDED_AGE_MIN_DEFAULT) -> list[dict]:
    """Report — never move — records a PRIOR run left claimed in processing/.

    Read-only by construction: this function has no call to ``_move`` and no
    write of any kind, so it cannot re-apply a member's instruction. The
    never-silently-re-apply property documented above (``--requeue-stale`` is a
    deliberate operator act) is therefore unchanged; this only makes the word
    "visible" in that contract true for someone who is not listing the
    directory by hand.

    Reports IDENTITY, not just a count (g-369-166 outcome 2, correcting the
    environment-granularity aggregation of rb-10397): each entry carries the
    environment key, the filename and the age, which is what an operator needs
    to act.
    """
    processing = env_dir / PROCESSING
    if not processing.is_dir():
        return []
    cutoff = time.time() - (age_min * 60)
    out: list[dict] = []
    for name in sorted(os.listdir(processing)):
        p = processing / name
        try:
            st = p.stat()
            if not p.is_file() or st.st_mtime >= cutoff:
                continue
        except OSError:
            continue
        out.append({
            "environment": env_dir.name,
            "file": name,
            "age_minutes": int((time.time() - st.st_mtime) // 60),
        })
    return out


def _empty_result(env_dir: Path, apply: bool) -> dict:
    return {
        "environment": env_dir.name,
        "processed": 0, "rejected": 0, "failed": 0,
        "quarantined": 0, "skipped_tmp": 0, "claimed": 0, "unconfigured": 0, "busy": 0,
        "unprovisioned": 0,
        "erased": 0, "erase_pending": 0, "erase_failed": 0, "would_erase": 0,
        "records": [],
        "erasures": [],
        "stranded": [],
        "dry_run": not apply,
    }


@contextlib.contextmanager
def _one_drain_at_a_time(env_dir: Path):
    """Yield True while this process holds ``env_dir``'s lock, False when another drain does.

    LocalBackend's lock, used directly: the spool is a directory on this host or its shared
    mount, never a governed path, so the process-wide backend (own-cloud on some boxes) is
    the wrong one to ask. ``timeout=0``: a drain that finds the lock taken goes away and
    leaves its records for the next pass, it does not wait behind a long one.
    """
    from storage_backend import LocalBackend  # noqa: PLC0415 - only an applying drain needs it
    lock = str(env_dir / LOCK_NAME)
    backend = LocalBackend()
    try:
        backend.acquire_lock(lock, timeout=0, stale_seconds=LOCK_STALE_SECONDS)
    except TimeoutError:
        yield False
        return
    try:
        yield True
    finally:
        backend.release_lock(lock)


def _keep_lock_fresh(env_dir: Path) -> None:
    """Show the lock is still held, so only a holder that stopped can lose it by age.

    Best effort: a lock that is gone has nothing to show, and one that cannot be touched can
    only go stale, which is the failure the lock already tolerates.
    """
    try:
        os.utime(env_dir / LOCK_NAME, None)
    except OSError:
        pass


def _sweep_erasures(env_dir: Path, res: dict, *, apply: bool, spool_root: Path) -> None:
    """Erase what a member forgot once its undo window has closed ( u4), and add what
    happened to ``res``. See THE 30-DAY ERASE in the module docstring.

    Skipped where ``retention/`` does not exist, since a box that never handled a forget has no
    obligation and checking the fences there would only make an idle misconfigured box report a
    failure it never had. Past that it takes both fences a forget takes, FAILED and loud when one
    refuses. The sweep is a boundary like an applier: it must not take the drain down with it, so
    an exception or a SystemExit is a failed erase, and its lines go to stderr, which keeps this
    process's stdout one JSON document under ``--json``. An exception is reported by its type
    alone and an exit by its integer status: a message can quote the text being erased. Only
    ``EraseError`` is quoted, since it names an applier or export attribute, or says the world
    cannot be found, and nothing else.
    """
    retention_dir = env_dir / RETENTION
    if not retention_dir.is_dir():
        return
    why = _destination_fence(spool_root) or _retention_refusal(retention_dir)
    if why is None:
        try:
            import knowledge_erase  # noqa: PLC0415 - only a box that has handled a forget needs it
        except Exception as exc:  # noqa: BLE001 - the sweep is a boundary
            why = f"the erase sweep could not load: {type(exc).__name__}"
    if why is None:
        try:
            with contextlib.redirect_stdout(sys.stderr):
                out = knowledge_erase.sweep(env_dir, retention_dir, _load_knowledge_applier(), apply=apply,
                                            tick=(lambda: _keep_lock_fresh(env_dir)) if apply else None)
        except SystemExit as exc:
            # an exit status only: a string code is a message, and a message can quote the text
            why = "the erase sweep exited" + (f": {exc.code}" if isinstance(exc.code, int) else "")
        except knowledge_erase.EraseError as exc:
            why = f"the erase sweep refused: {exc}"
        except Exception as exc:  # noqa: BLE001 - the sweep is a boundary
            why = f"the erase sweep raised {type(exc).__name__}"
    if why is not None:
        res["erase_failed"] += 1
        res["erasures"].append({"kind": "-", "record": "-", "action": "failed", "detail": why})
        return
    res["erased"] += out["erased"]
    res["erase_pending"] += out["pending"]
    res["erase_failed"] += out["failed"]
    res["would_erase"] += out["would_erase"]
    res["erasures"].extend(out["entries"])


def drain_environment(env_dir: Path, *, apply: bool, source: str, asp_id: str,
                      max_records: int, tmp_age_min: int,
                      spool_root: Path | None = None,
                      stranded_age_min: int = STRANDED_AGE_MIN_DEFAULT) -> dict:
    """Drain one environment's inbound spool, one drain at a time. Returns a result dict.

    An environment another drain holds is neither drained nor an error: ``busy`` is 1 and
    its records stay queued. A dry run takes no lock, since it writes nothing.
    """
    run = functools.partial(_drain_records, env_dir, apply=apply, source=source,
                            asp_id=asp_id, max_records=max_records, tmp_age_min=tmp_age_min,
                            spool_root=spool_root, stranded_age_min=stranded_age_min)
    if not apply:
        return run()
    with _one_drain_at_a_time(env_dir) as ours:
        if not ours:
            res = _empty_result(env_dir, apply)
            res["busy"] = 1
            return res
        return run()


def _drain_records(env_dir: Path, *, apply: bool, source: str, asp_id: str,
                   max_records: int, tmp_age_min: int,
                   spool_root: Path | None = None,
                   stranded_age_min: int = STRANDED_AGE_MIN_DEFAULT) -> dict:
    """Drain one environment's inbound spool. Returns a result dict."""
    inbound = env_dir / INBOUND
    res = _empty_result(env_dir, apply)

    # Scan BEFORE this run claims anything, or this run's own in-flight records
    # would read as stranded. Everything found here was left by a PRIOR run —
    # the case the wrapper cannot report, because that run already ended.
    res["stranded"] = stranded_records(env_dir, stranded_age_min)

    for stale in _tmp_residue(inbound, tmp_age_min):
        res["quarantined"] += 1
        res["records"].append({"file": stale.name, "disposition": QUARANTINE,
                               "detail": f"temp residue older than {tmp_age_min}m"})
        if apply:
            _move(stale, _lane(env_dir, QUARANTINE))

    files = _record_files(inbound)
    if max_records > 0:
        files = files[:max_records]

    for src in files:
        if apply:
            _keep_lock_fresh(env_dir)
        entry = {"file": src.name}
        try:
            raw = src.read_text(encoding="utf-8")
            record = json.loads(raw)
            if not isinstance(record, dict):
                raise ValueError("record is not a JSON object")
        except Exception as exc:  # noqa: BLE001
            entry.update(disposition=QUARANTINE, detail=f"unparseable: {exc}")
            res["quarantined"] += 1
            res["records"].append(entry)
            if apply:
                _move(src, _lane(env_dir, QUARANTINE))
            continue

        kind = record.get("kind")
        entry["kind"] = kind
        if kind not in KNOWN_KINDS:
            entry.update(disposition=QUARANTINE, detail=f"unknown kind {kind!r}")
            res["quarantined"] += 1
            res["records"].append(entry)
            if apply:
                _move(src, _lane(env_dir, QUARANTINE))
            continue

        if not apply:
            entry.update(disposition="would-apply", detail="dry run")
            res["records"].append(entry)
            continue

        # A DIRECTIVE WITH NO TARGET ASPIRATION IS NOT DRAINED AT ALL, and is
        # deliberately NOT claimed — the record stays in inbound/ and drains by
        # itself once the environment is configured, with no operator requeue needed.
        #
        # This is the one failure on this path that would otherwise be SILENT.
        # Filing into a default would decide, on a member's behalf and without
        # telling anyone, which aspiration their words belong to; that decision is
        # per-environment and is not the drain's to make. Refusing loudly
        # while leaving the record in place is the only option that neither
        # guesses nor loses it. Verbs are unaffected: a verb names its own target
        # through the opaque handle it carries.
        if kind == "directive" and not asp_id:
            entry.update(disposition="unconfigured",
                         detail=("no target aspiration: pass --aspiration or set "
                                 "INBOUND_DIRECTIVE_ASP_ID in the environment host's environment. "
                                 "Record left in inbound/, unclaimed and unapplied."))
            res["unconfigured"] += 1
            res["records"].append(entry)
            continue

        # A RECORD ADDRESSED BY HANDLE, ON A BOX THAT CANNOT RESOLVE HANDLES, is left where
        # it is too. Both resolvers read an unprovisioned box as "no such item", so applying
        # the record would REJECT a member's write over a gap on this side. Unclaimed, it
        # drains by itself once the box is provisioned.
        if kind in HANDLE_KINDS and _handles_provisioned() is False:
            entry.update(disposition="unprovisioned",
                         detail=("this box cannot resolve member handles: "
                                 "KNOWLEDGE_HANDLE_SECRET or ENVIRONMENT_ID is unset in the "
                                 "drain's environment. Record left in inbound/, unclaimed "
                                 "and unapplied."))
            res["unprovisioned"] += 1
            res["records"].append(entry)
            continue

        # CLAIM FIRST. The rename is the lock (rb-197): a concurrent drain that
        # loses this race gets FileNotFoundError and skips the record rather than
        # applying it a second time.
        try:
            claimed = _move(src, _lane(env_dir, PROCESSING))
        except (FileNotFoundError, OSError) as exc:
            entry.update(disposition="skipped", detail=f"lost claim race: {exc}")
            res["records"].append(entry)
            continue
        res["claimed"] += 1

        fence_root = spool_root if spool_root is not None else env_dir.parent
        report: dict = {}
        if kind == "verb":
            disposition, detail = _apply_verb(record, source)
        elif kind == "knowledge":
            disposition, detail = _apply_knowledge(record, spool_root=fence_root,
                                                   retention_dir=env_dir / RETENTION,
                                                   report=report)
        else:
            disposition, detail = _apply_directive(record, source, asp_id,
                                                   spool_root=fence_root)

        entry.update(disposition=disposition, detail=detail)
        res["records"].append(entry)
        res[disposition if disposition in ("processed", "rejected", "failed") else "failed"] += 1

        if disposition == FAILED:
            # Deliberately left in processing/ — visible, not silently retried.
            # --requeue-stale is the operator's move.
            continue
        if kind == "knowledge":
            entry["outcome"] = _knowledge_outcome(disposition, report)
            try:
                _record_outcome(claimed, record, entry["outcome"])
            except OSError as exc:
                # The edit landed or was refused either way. Left claimed, the record
                # would invite a requeue that applies it a second time.
                entry["outcome_error"] = f"{type(exc).__name__}: {exc}"
        _move(claimed, _lane(env_dir, disposition))

    _sweep_erasures(env_dir, res, apply=apply,
                    spool_root=spool_root if spool_root is not None else env_dir.parent)
    return res


def requeue_stale(env_dir: Path, *, apply: bool, age_min: int) -> dict:
    """Move processing/ entries older than age_min back to inbound/.

    IDEMPOTENT ON THE RECORD STEM (g-369-163 outcome 3). ``_move`` never
    deletes, so a collision is suffixed with a millisecond stamp — which is
    correct for its own contract and wrong here: re-queueing a record whose
    stem is ALREADY in ``inbound/`` produced a second copy of one member's
    instruction (measured 2026-09-08 on pearl-test-20260904-g3351459: the
    original plus ``...1788847706970.json``), and the drain would then apply
    it twice. A duplicate stem therefore goes to ``rejected/`` instead of
    being re-filed. The never-delete invariant is untouched — the duplicate
    is moved aside, not removed, so it stays inspectable.

    The stem is the record id (``<ts>-<uuid>``); ``_move``'s own suffixing is
    what makes a naive stem comparison necessary rather than sufficient, so
    compare against the stem of every inbound file, not just exact names.
    """
    processing = env_dir / PROCESSING
    res = {"environment": env_dir.name, "requeued": 0, "duplicates": 0,
           "files": [], "duplicate_files": [], "dry_run": not apply}
    if not processing.is_dir():
        return res
    inbound = _lane(env_dir, INBOUND)
    try:
        existing_stems = {Path(n).stem.split(".")[0]
                          for n in os.listdir(inbound)} if inbound.is_dir() else set()
    except OSError:
        existing_stems = set()
    cutoff = time.time() - (age_min * 60)
    for name in sorted(os.listdir(processing)):
        p = processing / name
        try:
            if not p.is_file() or p.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        stem = p.stem.split(".")[0]
        if stem in existing_stems:
            # Already queued — re-filing would apply one instruction twice.
            res["duplicates"] += 1
            res["duplicate_files"].append(name)
            if apply:
                _move(p, _lane(env_dir, REJECTED))
            continue
        res["requeued"] += 1
        res["files"].append(name)
        existing_stems.add(stem)
        if apply:
            _move(p, inbound)
    return res


def _environments(root: Path, env_key: str | None) -> list[Path]:
    if env_key:
        return [root / env_key]
    if not root.is_dir():
        return []
    return sorted([p for p in root.iterdir() if p.is_dir()], key=lambda p: p.name)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Drain an environment's inbound spool "
                    "(verbs and knowledge edits -> their appliers, directives -> goals).")
    ap.add_argument("--environment-key", help="drain one environment; omit with --all")
    ap.add_argument("--all", action="store_true", help="drain every environment under --root")
    ap.add_argument("--root", default=os.environ.get(SPOOL_ROOT_ENV),
                    help=f"spool root holding one directory per environment "
                         f"(default: ${SPOOL_ROOT_ENV}). No built-in default: a "
                         f"wrong root reports 'no environment', which reads as an "
                         f"empty spool rather than as missing configuration.")
    ap.add_argument("--apply", action="store_true",
                    help="perform the moves and writes (default is a dry run)")
    ap.add_argument("--source", default="world", choices=("world", "agent"))
    ap.add_argument("--aspiration", default=os.environ.get(DIRECTIVE_ASP_ENV),
                    help="aspiration a queued directive is filed under. DELIBERATELY "
                         "HAS NO DEFAULT: without it (this flag, or "
                         "$INBOUND_DIRECTIVE_ASP_ID) directives are left queued rather "
                         "than filed into a guessed aspiration. Verbs do not need it.")
    ap.add_argument("--max", type=int, default=0, help="cap records per environment (0 = no cap)")
    ap.add_argument("--tmp-age-min", type=int, default=60,
                    help="quarantine .tmp-* residue older than this many minutes")
    ap.add_argument("--requeue-stale", type=int, metavar="MINUTES",
                    help="move processing/ entries older than MINUTES back to inbound/, then exit")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    if not args.environment_key and not args.all:
        ap.error("pass --environment-key <key> or --all")
    if not args.root:
        ap.error(f"pass --root <path> or set ${SPOOL_ROOT_ENV}")

    root = Path(args.root)
    envs = _environments(root, args.environment_key)
    if not envs:
        sys.stderr.write(f"inbound-drain: no environment under {root}\n")
        return 1
    missing = [e for e in envs if not e.is_dir()]
    if missing and args.environment_key:
        sys.stderr.write(f"inbound-drain: not a directory: {missing[0]}\n")
        return 1

    results = []
    for env_dir in envs:
        if not env_dir.is_dir():
            continue
        if args.requeue_stale is not None:
            results.append(requeue_stale(env_dir, apply=args.apply, age_min=args.requeue_stale))
        else:
            results.append(drain_environment(
                env_dir, apply=args.apply, source=args.source, asp_id=args.aspiration,
                max_records=args.max, tmp_age_min=args.tmp_age_min,
                spool_root=root))

    payload = {"root": str(root), "at": datetime.now(timezone.utc).isoformat(),
               "environments": results}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for r in results:
            if "requeued" in r:
                print(f"{r['environment']}: requeued={r['requeued']} "
                      f"duplicates={r.get('duplicates', 0)}"
                      + ("   (dry run — pass --apply)" if r["dry_run"] else ""))
                continue
            # stranded= is NOT decoration: a run with processed=0 and a record
            # stuck in processing/ is indistinguishable from an empty spool
            # without it, and that is the exact misread this line exists to stop
            # ( outcome 2; guard-3865 — a summary's own accounting
            # fields are not the run's telemetry). The JSON payload has carried
            # `stranded` since ; only this human-readable path omitted
            # it, so a `--json`-less operator run still read clean.
            print(f"{r['environment']}: processed={r['processed']} rejected={r['rejected']} "
                  f"failed={r['failed']} quarantined={r['quarantined']} "
                  f"unconfigured={r.get('unconfigured', 0)} busy={r.get('busy', 0)} "
                  f"unprovisioned={r.get('unprovisioned', 0)} "
                  f"stranded={len(r.get('stranded') or [])} "
                  f"erased={r.get('erased', 0)} erase_pending={r.get('erase_pending', 0)} "
                  f"erase_failed={r.get('erase_failed', 0)}"
                  + (f" would_erase={r.get('would_erase', 0)}" if r["dry_run"] else "")
                  + ("   (dry run — pass --apply)" if r["dry_run"] else ""))
            for e in r["records"]:
                print(f"    {e['file']}: {e.get('disposition')} — {e.get('detail','')}"
                      + (f" [outcome not recorded: {e['outcome_error']}]"
                         if e.get("outcome_error") else ""))
            for e in r.get("erasures") or []:
                print(f"    erase {e.get('kind')} {e.get('record')}: {e.get('action')} — {e.get('detail', '')}")

    # A stuck record, and a member instruction left queued (unconfigured or
    # unprovisioned), all need attention, and none may report success. The queued ones
    # are the quiet ones — the spool simply never drains — so they must not ride out as
    # rc=0. A busy environment is not one of them: the drain holding it is doing the work.
    # An erase that is due and unmet, or that failed, is an obligation to a member that was
    # not kept, which is the same shape.
    if any(r.get("failed", 0) or r.get("unconfigured", 0) or r.get("unprovisioned", 0)
           or r.get("erase_pending", 0) or r.get("erase_failed", 0) for r in results):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
