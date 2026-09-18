#!/usr/bin/env python3
"""goal-field-append — safe read-modify-write append onto ONE goal text field.

WHY THIS EXISTS (g-115-4717, residual of the gap-023 dismissal g-115-4690)

``aspirations-update-goal.sh <id> <field> <value>`` REPLACES the field. It does
not append, and the prior value is not recoverable in the general case:
``world/.history`` snapshots are periodic and may predate the value by weeks,
and ``changelog.jsonl`` records the write EVENT, never the VALUE. So every
"annotate an existing record" write is a read-modify-write the author has to
remember to perform by hand — and zeta hand-rolled that same script four times
in a single session.

WHY A SEPARATE SCRIPT AND NOT AN ``--append`` FLAG
This was the goal's other option and it is the one the evidence refuses.
``aspirations-update-goal.sh`` is one of FOUR wrappers in this family that
hand-roll their arg parser and end in a silent ``-*) PASSTHROUGH+=("$1")`` arm
(the others: ``aspirations-update.sh``, ``aspirations-add-goal.sh``,
``pipeline-update-field.sh``; measured with ``grep -l _argv_strict``). On those,
an unrecognized flag is DROPPED and the next token is promoted into the value
slot — rc=0, a full pretty-printed record on stdout, field destroyed
(guard-2460, guard-1047, guard-1488). guard-2525 exists specifically to say
"never pass --append", and guard-1047's own retrospective records that the
DANGEROUS case is a REAL flag transferred between sibling wrappers that do not
share a parser. Adding a real ``--append`` to one of the four would manufacture
exactly that transfer hazard for the other three. A distinct script NAME cannot
be swallowed by a PASSTHROUGH arm, needs no daemon change (so guard-742's
half-a-fix hazard does not apply), and follows the eight per-store append
helpers this codebase already has (wm-append, journal-append,
evolution-log-append, decision-rules-append, health-ledger-append,
meta-log-append, mind-append) — of which zero are generic.

CORRECTION (2026-08-31, g-115-8406): that PASSTHROUGH mechanism is FIXED and the
paragraph above is kept only as the history that produced this script. Measured on
all four: every one now ends in ``argv_strict_refuse_unknown`` (g-115-4733), so an
unrecognized flag is REFUSED at exit 2, never swallowed, and no token is promoted
into the value slot. The transfer hazard that argued against a flag is therefore
gone in the form described — which is what makes it safe for this script to pass
the real ``--value-stdin`` flag below. The conclusion still stands on its other
legs (a distinct name needs no daemon change and matches the eight per-store append
helpers); only the swallowing mechanism is out of date.

READ-SOURCE SAFETY (guard-1251 / guard-1912)
Those guardrails say never RMW a goal field from ``aspirations-query.sh``,
because its projection omits large text fields and an append onto an empty read
silently destroys the original. Measured 2026-08-04: the DEFAULT projection
returns exactly six keys (asp_id, category, goal_id, source, status, title) —
the guardrails are exactly right about it — but ``--full`` is an unprojected
passthrough (46 keys on g-115-22, description 1678 chars, outcome_note 5673
chars, defer_reason present as null). This script uses ``--full`` AND defends
the hazard mechanically rather than by avoiding the accessor: it refuses to
compose anything unless the record it read carries keys the default projection
cannot produce. A projected read therefore aborts loudly instead of reading
empty and overwriting. That discriminator matters because appending to a
legitimately-empty field is a valid first-note case, so "the field is empty"
alone can never distinguish a lying read from a true one.

WHAT IT REFUSES, and why each refusal is a real failure someone hit
  - a projected/absent/ambiguous read            -> nothing is composed
  - a non-text field (dict/list)                 -> guard-2444 nested-parent
                                                    replace drops sibling keys
  - a composed value starting with { or [        -> the wrapper's parse_value
                                                    would JSON-decode it
  - an unknown flag                              -> _argv_strict, exit 2

IDEMPOTENCY
The caller supplies a marker. The script appends a one-line sentinel
``[appended:<marker>]`` after the text, and on a re-run sees that sentinel in
the CURRENT value and exits 0 having changed nothing — so a retry after a
partial failure is safe.

VERIFICATION (sig-40 / guard-2444 / guard-2525 / guard-1870)
The post-write assertion compares against the PRE value, never against the
string this script constructed — comparing to your own construction only proves
the write echoed. It asserts the sentinel is present, that PRE survived
verbatim, and that the length GREW.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _runtime_bash import bash_cmd  # noqa: E402  (guard-580/581: never bare "bash")

SCRIPTS = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS.parent.parent

# Every status a live goal can hold. The read below filters by id, but
# aspirations-query.sh REQUIRES a status filter, so this enumerates all of them
# rather than guessing the goal's current one.
ALL_STATUSES = "pending,in-progress,completed,blocked,skipped,expired,decomposed"

# Keys the DEFAULT six-key projection cannot produce. Seeing at least one of
# these is the proof that --full actually returned the stored record. This is
# the mechanical defense against guard-1251's "the read is lying, not the
# record" case.
UNPROJECTED_CANARIES = ("priority", "created_at", "participants", "verification", "description")
DEFAULT_PROJECTION_KEYS = {"asp_id", "category", "goal_id", "source", "status", "title"}

RC_OK = 0
RC_USAGE = 2
RC_READ_UNSAFE = 3
RC_FIELD_SHAPE = 4
RC_VALUE_SHAPE = 5
RC_WRITE_FAILED = 6
RC_VERIFY_FAILED = 7
# 8 is deliberately UNUSED here: store-field-append.py already ships
# RC_ANCHOR_ABSENT = 8 (a store-only concept). These two scripts are one family
# with a shared contract, so a caller wrapping both must not have to remember
# that rc 8 means different things on each side. Concurrent-modification is 9 on
# BOTH — the aligned number is worth more than the smaller one.
RC_CONCURRENT_MODIFICATION = 9


def _run(argv, **kw):
    return subprocess.run(argv, capture_output=True, text=True, cwd=str(PROJECT_ROOT), **kw)


def _parse_json_tail(raw: str):
    """Parse the JSON body out of wrapper stdout that may carry a banner line.

    Several wrappers print a human banner to stderr that gets merged into the
    captured stream. Find the first '[' or '{' and decode from there.
    """
    starts = [i for i in (raw.find("["), raw.find("{")) if i >= 0]
    if not starts:
        raise ValueError("no JSON found in wrapper output")
    return json.loads(raw[min(starts):])


def read_goal(goal_id: str, source: str) -> dict:
    """Return the goal's FULL stored record, or exit loudly.

    Never returns a projected record — that is the whole point (guard-1251).
    """
    res = _run(bash_cmd(
        SCRIPTS / "aspirations-query.sh",
        "--goal-status", ALL_STATUSES,
        "--goal-field", "id", goal_id,
        "--full",
    ))
    if res.returncode != 0:
        _die(RC_READ_UNSAFE, f"read failed (rc={res.returncode}): {res.stderr.strip()[:400]}")
    try:
        parsed = _parse_json_tail(res.stdout)
    except Exception as exc:  # noqa: BLE001
        _die(RC_READ_UNSAFE, f"read returned unparseable output: {exc}")
    rows = parsed if isinstance(parsed, list) else (parsed.get("goals") or parsed.get("results") or [])
    if len(rows) != 1:
        _die(RC_READ_UNSAFE,
             f"expected exactly 1 record for {goal_id}, got {len(rows)}. "
             "An empty result is a FAILED measurement, not a measurement of empty (guard-1091).")
    row = rows[0]
    if not isinstance(row, dict):
        _die(RC_READ_UNSAFE, "read returned a non-object record")
    # The projection discriminator. A six-key row means --full did not widen
    # the projection on this daemon build; composing from it would append onto
    # an empty read and destroy the field (guard-1251).
    if not any(k in row for k in UNPROJECTED_CANARIES) or set(row.keys()) <= DEFAULT_PROJECTION_KEYS:
        _die(RC_READ_UNSAFE,
             f"read returned a PROJECTED record ({len(row)} keys: {sorted(row)[:8]}). "
             "Large text fields are omitted from the default projection, so appending "
             "onto this read would silently destroy the field. Refusing (guard-1251).")
    return row


def _die(code: int, msg: str):
    print(json.dumps({"ok": False, "rc": code, "error": msg}, indent=2), file=sys.stderr)
    sys.exit(code)


# --- pure helpers (shared by main() and the regression tests) --------------
# Extracted rather than inlined because the SAFETY INVARIANTS live here: a test
# that cannot call them can only assert on process exit codes, which is exactly
# the weak-predicate shape (guard-2460's "rc=0 and a printed record prove
# nothing"). Two call sites today — main() and test_goal_field_append.py.

#: ONE literal, shared by `sentinel_for` and the refusal below — the same
#: single-source idiom the .sh wrapper uses for its store list, and for the same
#: reason: two strings that must agree were asserted to be one and were not.
SENTINEL_PREFIX = "[appended:"


def sentinel_for(marker: str) -> str:
    return f"{SENTINEL_PREFIX}{marker}]"


def wrapped_marker_refusal(marker: str) -> "str | None":
    """Refusal text when the caller passed an ALREADY-WRAPPED marker, else None.

    This script OWNS the wrapping — `sentinel_for` turns `m` into `[appended:m]`
    — but the only place that convention is ever VISIBLE is inside a record,
    where it appears already wrapped. An author who has read a record therefore
    has every reason to pass the wrapped form, and the usage line (`<marker>`)
    says nothing to stop them. Doing so writes `[appended:[appended:m]]` at rc=0
    with `"changed": true`.

    That is worse than an ugly string, because THE MARKER IS THE IDEMPOTENCY KEY.
    After a double-wrap the record answers to a sentinel that no correct retry
    will ever test for: a retry with the bare form finds its own sentinel present
    (from the caller's prose) and correctly refuses, while a retry with the
    malformed form tests for the doubled one and APPENDS AGAIN. The failure is
    silent at write time and visible only by reading the field back and counting.
    Measured 2026-09-15 on guard-1067: pre_len 483 -> 2360, repaired to 2312, the
    48-byte delta being exactly the doubled sentinel (g-001-847).

    REFUSE, DO NOT UNWRAP. Silently accepting both spellings recreates the same
    ambiguity one layer down — the caller still cannot tell which key their
    record carries — and quietly widens a matcher over a live corpus. rb-233
    (fail-closed by type-distinctness): make the wrong shape a DISTINCT, refused
    input rather than a silently-accepted one. guard-1338: refuse it BY NAME
    rather than letting it reach code that produces a confusing result.
    """
    if not marker.startswith(SENTINEL_PREFIX):
        return None
    bare = marker[len(SENTINEL_PREFIX):]
    if bare.endswith("]"):
        bare = bare[:-1]
    return (
        f"refusing an already-wrapped marker {marker!r} — this script wraps the "
        f"marker for you, so passing the wrapped form writes a doubled sentinel "
        f"and breaks the idempotency key. Pass the BARE token: {bare!r}"
    )


def is_read_projected(row: dict) -> bool:
    """True when the record came back through the six-key default projection.

    A projected read omits large text fields, so composing from it appends onto
    an empty string and destroys the field (guard-1251). Presence of any key the
    default projection cannot produce is the proof the read is the real record.
    """
    if not isinstance(row, dict):
        return True
    if set(row.keys()) <= DEFAULT_PROJECTION_KEYS:
        return True
    return not any(k in row for k in UNPROJECTED_CANARIES)


def compose(pre: str, text: str, marker: str) -> str:
    """PRE + blank line + text + sentinel. Empty PRE yields no leading blank."""
    return (pre + "\n\n" if pre else "") + text + "\n" + sentinel_for(marker)


def cas_conflict(pre: str, current) -> "str | None":
    """Describe a concurrent modification, or return None when it is safe to write.

    The compare half of a compare-and-swap. `pre` is the value this process read
    before composing; `current` is a re-read taken immediately before the write.
    Equal means no writer landed in between and the composed value is still
    correct — composing from `pre` is then identical to composing from `current`,
    which is what guard-3020 ("re-read IMMEDIATELY BEFORE composing") requires.

    The two unequal cases are reported apart on purpose: a pure APPEND by a peer
    is the expected race and retrying resolves it, while a REWRITE means the
    record no longer contains what this author read and a retry would land an
    amendment on content nobody reviewed (the guard-1615/anchor hazard).
    """
    if not isinstance(current, str):
        return f"pre-write re-read returned {type(current).__name__}, not text"
    if current == pre:
        return None
    if pre and pre in current:
        return (f"another writer appended {len(current) - len(pre)} chars between this "
                f"script's read and its write (pre={len(pre)} now={len(current)}); "
                "writing the value composed from the stale read would drop their text")
    if not pre:
        return (f"the field was empty at read time and now holds {len(current)} chars — "
                "another writer created it between this script's read and its write")
    return (f"the field changed between this script's read and its write "
            f"(pre={len(pre)} now={len(current)}) and the PRE content is no longer "
            "contained in it — the record was REWRITTEN, not appended to")


def verify_post(pre: str, post, sentinel: str) -> "list[str]":
    """Return the list of verification problems; empty means the write is sound.

    Compares against PRE, never against the composed string — comparing to your
    own construction only proves the write echoed (sig-40).
    """
    problems = []
    if not isinstance(post, str):
        return [f"post value is {type(post).__name__}, not text"]
    if sentinel not in post:
        problems.append("marker sentinel absent from the stored value")
    if pre and pre not in post:
        problems.append("PRE content did NOT survive the write — the field was overwritten")
    if len(post) <= len(pre):
        problems.append(f"length did not grow (pre={len(pre)} post={len(post)})")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="goal-field-append.py", add_help=True)
    ap.add_argument("--source", default="world", choices=("world", "agent"))
    ap.add_argument("goal_id")
    ap.add_argument("field")
    ap.add_argument("marker", help="idempotency token; a re-run with the same marker is a no-op")
    ap.add_argument("text", help="the text to append")
    args = ap.parse_args(argv)

    refusal = wrapped_marker_refusal(args.marker)
    if refusal:
        _die(RC_USAGE, refusal)

    sentinel = sentinel_for(args.marker)
    text = args.text.strip("\n")
    if not text:
        _die(RC_VALUE_SHAPE, "refusing to append empty text")

    row = read_goal(args.goal_id, args.source)
    pre = row.get(args.field)

    if pre is None:
        pre = ""
    if not isinstance(pre, str):
        _die(RC_FIELD_SHAPE,
             f"field '{args.field}' is a {type(pre).__name__}, not text. This helper appends to "
             "TEXT fields only. A nested write means reconstructing the whole parent subdocument, "
             "and every sibling key you omit is dropped silently at HTTP 200 (guard-2444) — do "
             "that deliberately, by hand, with a PRE/POST sibling-survival assertion.")

    if sentinel in pre:
        out = {"ok": True, "changed": False, "reason": "idempotent: marker already present",
               "goal_id": args.goal_id, "field": args.field, "marker": args.marker,
               "pre_len": len(pre)}
        print(json.dumps(out, indent=2))
        return RC_OK

    new = compose(pre, text, args.marker)

    # The wrapper's parse_value JSON-decodes any value that starts with { or [
    # (aspirations-update-goal.sh:134). A composed value that starts with one
    # would be stored as a parsed object rather than the text we built.
    if new[:1] in ("{", "["):
        _die(RC_VALUE_SHAPE,
             "composed value starts with '{' or '[' — the update wrapper would JSON-decode it "
             "rather than store it as text. Prefix the field's existing content or the appended "
             "text so it does not begin with a JSON opener.")

    # PRE-WRITE RE-READ (the compare half of a compare-and-swap) — .
    # read_goal() above and the write below are two separate subprocess
    # round-trips with NOTHING serializing the span between them. Two concurrent
    # invocations both read PRE, both compose PRE+their-own-text, and the second
    # write clobbers the first — while verify_post() passes for BOTH, because
    # each writer's own sentinel is present, its own PRE survived, and the length
    # grew. Neither ever learns the other's text is gone. The idempotence marker
    # does not cover this (it guards a retry of the SAME marker) and neither does
    # store-field-append's --anchor (it is evaluated against the value read
    # BEFORE the window opens, so both writers see it satisfied).
    #
    # WHY NOT locked_rmw, the goal's remedy (2): it is defined only in
    # mind_api/src/file_locks.py — a per-process threading.Lock plus a file lock
    # taken INSIDE the daemon. This script never opens the store; it shells out
    # to daemon-routed wrappers. No daemon-side lock can span two round-trips
    # this process issues, so remedy (2) is not implementable from here. Closing
    # the span properly needs a compare-and-swap ENDPOINT (an If-Match on the
    # field), which is a daemon protocol change and is filed separately rather
    # than faked with a lock that cannot reach.
    #
    # AND class (a) does NOT rescue this. Every target store is merge-protected
    # (core/config/conventions/governed-store-write-classes.md), but a merge
    # handler reconciles LOCAL vs REMOTE divergence — here both writes land
    # locally in sequence, so the handler never sees two versions to reconcile.
    # Even across boxes it would not conserve: both sides carry the SAME key
    # with different values, and _merge_goal's union-backfill only rescues keys
    # one side LACKS. Per that convention's own  section, class (a)
    # answers whether a reconciler runs below the write, never whether it
    # conserves.
    fresh = read_goal(args.goal_id, args.source)
    current = fresh.get(args.field)
    if current is None:
        current = ""
    if isinstance(current, str) and sentinel in current:
        # A concurrent run of THIS marker landed while we were composing. That
        # is the idempotent case, not a conflict — report it and change nothing.
        out = {"ok": True, "changed": False,
               "reason": "idempotent: marker landed concurrently between read and write",
               "goal_id": args.goal_id, "field": args.field, "marker": args.marker,
               "pre_len": len(pre)}
        print(json.dumps(out, indent=2))
        return RC_OK
    conflict = cas_conflict(pre, current)
    if conflict:
        _die(RC_CONCURRENT_MODIFICATION,
             "refusing to write — " + conflict + ". NOTHING WAS WRITTEN and no text was "
             "lost. Re-run the identical command: the fresh read picks up their text and "
             "the marker keeps the retry idempotent (g-115-5638).")

    # WRITE — the value goes on STDIN, never argv (). Passing it
    # positionally bounded the field at the OS command-line limit: Windows
    # CreateProcess caps the COMPOSED total at ~32,767 chars (guard-5634), so once a
    # progress_note crossed that it became permanently unwritable from every Windows
    # box — WinError 206, and no smaller append helps, because the whole value is
    # re-sent on every append. Measured on  at 32,152 chars: a 2.6k and a 4.6k
    # append failed identically.
    # This SUBSUMES the guard-1047 / guard-2460 concern the old positional form managed
    # by ordering — a value beginning with '-' cannot be read as a flag when it is not
    # in argv at all — and sidesteps guard-5633 (Windows argv silently truncates on an
    # embedded double quote), which composed prose can trip at any length.
    # --override-narrative-replace is REQUIRED here, not optional ().
    # That wrapper now REFUSES --value-stdin on progress_note/outcome_note/
    # description, because the same flag name APPENDS on this script and
    # REPLACES there, and the destructive call reads like the appending one.
    # This call IS the appending one: `new` is the composed read-modify-write
    # result, and the CAS conflict check, marker idempotency and post-state
    # verification this script owns have already run. Without the override the
    # refusal fires on its own sanctioned remedy — measured immediately on the
    # first append after the refusal landed, which is how it was caught.
    res = _run(bash_cmd(
        SCRIPTS / "aspirations-update-goal.sh",
        "--source", args.source, "--value-stdin",
        "--override-narrative-replace",
        "delegated append via goal-field-append.sh (CAS + marker idempotency applied)",
        args.goal_id, args.field,
    ), input=new)
    if res.returncode != 0:
        # The wrapper's rc is NOT the store of record (, rb-2648): its
        # OWN output handling can raise (e.g. a JSONDecodeError when the appended
        # text carries a dict/JSON literal) AFTER the field is already written —
        # the measured rc=6 false alarm. Confirm against an independent read
        # before declaring the write failed.
        recovered = None
        try:
            again = read_goal(args.goal_id, args.source)
            val = again.get(args.field)
            if isinstance(val, str) and sentinel in val and (not pre or pre in val):
                recovered = val
        except SystemExit:
            pass
        if recovered is None:
            _die(RC_WRITE_FAILED,
                 f"write failed (rc={res.returncode}) and an independent store read does not "
                 f"show it landed: {res.stderr.strip()[:400]}. Re-run the identical command — "
                 "the marker keeps the retry idempotent.")
        # Landed despite the wrapper's nonzero rc — verify against the
        # authoritative value the store returned, not the errored echo.
        written = {args.field: recovered}
    else:
        # VERIFY against PRE, never against `new` — comparing to your own
        # construction only proves the write echoed (sig-40).
        try:
            written = _parse_json_tail(res.stdout)
        except Exception as exc:  # noqa: BLE001
            _die(RC_VERIFY_FAILED, f"write returned unparseable output, cannot verify: {exc}")
    post = written.get(args.field)
    problems = verify_post(pre, post, sentinel)
    if problems:
        # The wrapper's stdout ECHO is NOT the store of record (,
        # sig-40, rb-2648, guard-1711): a full-field write can echo a
        # projected/omitted field while the store itself is written correctly —
        # the measured rc=7 "field was overwritten" false positive. Re-verify
        # against an INDEPENDENT fresh read before declaring failure or naming
        # any recovery. A sentinel-absent fresh read is own-cloud lag
        # (guard-1122), NOT a loss; only marker-present-AND-PRE-absent is a real
        # rewrite.
        confirmed_lost = None  # None = confirmation read unavailable
        try:
            again = read_goal(args.goal_id, args.source)
            val = again.get(args.field)
            if isinstance(val, str):
                confirmed_lost = bool(sentinel in val and pre and pre not in val)
                if not confirmed_lost and sentinel in val:
                    post = val  # store is sound; report its authoritative value
        except SystemExit:
            pass
        if confirmed_lost is None or confirmed_lost:
            detail = ("confirmed against an independent read of the store"
                      if confirmed_lost
                      else "the wrapper echo failed verification and the confirmation read was unavailable")
            _die(RC_VERIFY_FAILED,
                 "write verification FAILED (" + detail + "): " + "; ".join(problems) +
                 ". Re-run the identical command — the marker keeps the retry idempotent. To "
                 "inspect this one field's history use core/scripts/history.py list "
                 "world/aspirations.jsonl; DO NOT run history.py restore — it rewrites the WHOLE "
                 "shared store in place and discards every other Body's writes since the snapshot "
                 "(g-115-8819).")
        # else: the echo was misleading; the authoritative store confirms the write.

    # Independent confirmation read. Under own-cloud a same-second re-read can
    # lag the authoritative store (guard-1122), so a disagreement here is
    # reported as a CONSISTENCY signal — the write response above already
    # proved the content survived, and it came from the same store of record.
    confirm = "agreed"
    try:
        again = read_goal(args.goal_id, args.source)
        val = again.get(args.field) or ""
        if sentinel not in val:
            confirm = "LAGGING: independent re-read does not yet show the marker"
        elif pre and pre not in val:
            confirm = "DISAGREES: independent re-read is missing the PRE content"
    except SystemExit:
        confirm = "unavailable: confirmation re-read could not be performed"

    store = os.environ.get("WORLD_PATH") or str(PROJECT_ROOT / ".mind-data" / "world")
    store_file = f"{store}/aspirations.jsonl" if args.source == "world" else "agents/*/aspirations.jsonl"

    out = {
        "ok": True, "changed": True, "goal_id": args.goal_id, "field": args.field,
        "marker": args.marker, "source": args.source,
        "pre_len": len(pre), "post_len": len(post), "delta": len(post) - len(pre),
        "store": store_file, "confirm_read": confirm,
    }
    print(json.dumps(out, indent=2))
    return RC_OK


if __name__ == "__main__":
    sys.exit(main())
