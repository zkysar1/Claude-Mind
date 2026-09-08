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
cheapest way to satisfy it is to never delete at all. Moves are ``os.replace``
within one filesystem, so each is atomic.

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

EXIT CODES (keep in sync with the argument parser below):
  0  drain ran; every record reached a terminal directory, or the spool was empty
  1  usage error, or the root/environment does not exist
  2  drain ran but needs attention: a record failed transiently and stayed claimed,
     and/or a directive was left queued because no target aspiration is configured

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
import importlib.util
import json
import os
import sys
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
REJECTED = "rejected"
FAILED = "failed"
QUARANTINE = "quarantine"

KNOWN_KINDS = ("verb", "directive")


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


class DrainError(RuntimeError):
    pass


# --- the two appliers -------------------------------------------------------
# Both are reached through their CANONICAL path. Neither is reimplemented here:
# a second copy of the verb decision core or of the goal-filing rules would drift
# from the original and nothing would fail when it did.


def _load_verb_applier():
    """``planned-verb-apply.py`` is hyphenated, so it needs the importlib shape.

    Called IN-PROCESS rather than through ``subprocess.run(['bash', ...])``:
    guard-744 / guard-555 record that a nested subprocess misses the PreToolUse
    env/PATH injection the daemon wrappers depend on, and the applier already
    resolves bash correctly for its own writes (guard-580 via _runtime_bash.BASH).
    Calling its ``main()`` gets the canonical code path with no extra layer.
    """
    # No "cannot locate core/scripts" branch: this engine LIVES in core/scripts,
    # so CORE_SCRIPTS is this file's own directory and cannot be unset. The world
    # engine needed that search (and its failure mode) only because it sat outside
    # the repo -- removing it is part of the move, not an unrelated tidy.
    path = CORE_SCRIPTS / "planned-verb-apply.py"
    if not path.is_file():
        raise DrainError(f"applier missing: {path}")
    spec = importlib.util.spec_from_file_location("planned_verb_apply_for_drain", path)
    if spec is None or spec.loader is None:  # pragma: no cover - import plumbing
        raise DrainError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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
    try:
        rc = mod.main(argv)
    except SystemExit as exc:  # argparse inside the applier
        rc = int(exc.code or 0)
    except Exception as exc:  # noqa: BLE001 - the applier is a boundary
        return FAILED, f"applier raised {type(exc).__name__}: {exc}"

    if rc == 0:
        return PROCESSED, "applied"
    if rc == 3:
        # plan_verb refused: unresolvable handle, or a goal that is not
        # member_writable. Terminal by construction — the same record will be
        # refused identically forever, so retrying it is a busy-loop.
        return REJECTED, "applier refused (rc=3): unresolved handle or not member-writable"
    return FAILED, f"applier rc={rc}"


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
        # Surface the daemon's OWN reason. RtError carries the response body
        # ({"error": "origin_signal_blocked", ...}); reporting only
        # "daemon HTTP 400" cost a paid vessel run its diagnosis.
        body = getattr(exc, "body", None)
        detail = f" body={str(body).strip()[:300]}" if body else ""
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


def drain_environment(env_dir: Path, *, apply: bool, source: str, asp_id: str,
                      max_records: int, tmp_age_min: int,
                      spool_root: Path | None = None) -> dict:
    """Drain one environment's inbound spool. Returns a result dict."""
    inbound = env_dir / INBOUND
    res = {
        "environment": env_dir.name,
        "processed": 0, "rejected": 0, "failed": 0,
        "quarantined": 0, "skipped_tmp": 0, "claimed": 0, "unconfigured": 0,
        "records": [],
        "dry_run": not apply,
    }

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

        if kind == "verb":
            disposition, detail = _apply_verb(record, source)
        else:
            disposition, detail = _apply_directive(
                record, source, asp_id,
                spool_root=spool_root if spool_root is not None else env_dir.parent)

        entry.update(disposition=disposition, detail=detail)
        res["records"].append(entry)
        res[disposition if disposition in ("processed", "rejected", "failed") else "failed"] += 1

        if disposition == FAILED:
            # Deliberately left in processing/ — visible, not silently retried.
            # --requeue-stale is the operator's move.
            continue
        _move(claimed, _lane(env_dir, disposition))

    return res


def requeue_stale(env_dir: Path, *, apply: bool, age_min: int) -> dict:
    """Move processing/ entries older than age_min back to inbound/."""
    processing = env_dir / PROCESSING
    res = {"environment": env_dir.name, "requeued": 0, "files": [], "dry_run": not apply}
    if not processing.is_dir():
        return res
    cutoff = time.time() - (age_min * 60)
    for name in sorted(os.listdir(processing)):
        p = processing / name
        try:
            if not p.is_file() or p.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        res["requeued"] += 1
        res["files"].append(name)
        if apply:
            _move(p, _lane(env_dir, INBOUND))
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
                    "(verbs -> applier, directives -> goals).")
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
                print(f"{r['environment']}: requeued={r['requeued']}"
                      + ("   (dry run — pass --apply)" if r["dry_run"] else ""))
                continue
            print(f"{r['environment']}: processed={r['processed']} rejected={r['rejected']} "
                  f"failed={r['failed']} quarantined={r['quarantined']} "
                  f"unconfigured={r.get('unconfigured', 0)}"
                  + ("   (dry run — pass --apply)" if r["dry_run"] else ""))
            for e in r["records"]:
                print(f"    {e['file']}: {e.get('disposition')} — {e.get('detail','')}")

    # Both a stuck record and an unconfigured environment need attention, and
    # neither may report success. An unconfigured one is the quieter of the two — the
    # spool simply never drains — so it must not ride out as rc=0.
    if any(r.get("failed", 0) or r.get("unconfigured", 0) for r in results):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
