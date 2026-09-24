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

RETENTION POLICY IS FIELD-SPECIFIC (g-115-10533)
``rotate_oversize`` moves the OLDEST blocks to the archive and keeps the newest
that fit. For a progress_note that polarity is right: recent history is what a
reader needs. For a description it is WRONG: the oldest blocks are the ORIGINAL
GOAL STATEMENT — the executable task definition — and a keep-newest rotation
there archives the task and keeps the amendments. Measured, not inferred:
2026-09-22T01:39:29 an ordinary description append on g-115-817 rotated the 30
oldest blocks out — evicting the unit-lease STEP 0, the PULL-FIRST step, and the
alert ROUTING TABLE, and leaving fourteen later lesson blocks that commented on
all three (guard-7281; the mechanism wrote a correct, readable archive and lost
exactly nothing, which is what makes the loss invisible to every integrity
check).

The two properties a description bound owes are (a) the executable task
statement SURVIVES every eviction and (b) the field STAYS under the read cap.
Keep-newest satisfies (b) and fails (a); a bare exemption from rotation
satisfies (a) and fails (b) — it restores unbounded growth on the one field
that already reached 111 KB once, and its failure mode is SILENT TRUNCATION
(guard-1478): the reader gets a prefix and cannot tell (bravo, cc-13).

The policy that satisfies both: a pin marker the AUTHOR writes makes a block
non-evictable. ``[hoisted:...]`` is the measured in-corpus spelling (the evicted
STEP 0 carried it); ``[pin:...]`` is the named form new blocks should use.
Pinning is on the MARKER, not on position — the executable head is a RUN of
blocks (statement + protocol + routing table) and its length is not knowable
from position alone; a first-block pin saved STEP 0 while a measured rotation
still cut the Step-1 and routing-table blocks that made it runnable. Every
pinned block is written to the archive sink on rotation (provenance) but never
cut from the live field; the REST rotates keep-newest as before. ON TOP of the
marked set, block 0 is pinned UNCONDITIONALLY: the goal's check is absolute —
the FIRST block is never the block that gets archived — and a later marked
block must not re-arm keep-newest over an unmarked original statement. When
the pinned head ALONE exceeds the keep bound, rotation fails open (returns the
field untouched): (a) beats (b), and a field that no bound can shrink is a
curation job for a human, not a reason to cut the statement.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
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


# ── Field-size bound at the WRITE site () ─────────────────────────
# A recurring goal never closes, so its note is an unbounded accumulator: every
# cycle appends a block and nothing ever removes one. Measured on 
# (2026-09-21, alpha/cc-04): progress_note 206,540 B across 45 blocks written by
# five agents on five boxes, a read surface 5.19x over the 62,500 B cap, so the
# claim gate correctly refused to let the goal be read whole and it drifted
# toward unexecutable. Detection already existed downstream (the claim gate,
# goal-note-tail's read_surface_vs_cap); nothing governed the WRITE. This does.
#
# KEYED ON BYTES, NEVER ON BLOCK OR MARKER COUNT (guard-6707): the [appended:]
# marker count is not the block count and the gap is silent, so a count-keyed
# bound is wrong in a way nothing reports. Bytes are what the read cap measures.
#
# ROTATION IS ITS OWN WRITE, DELIBERATELY NOT PART OF THE APPEND. verify_post()
# asserts `pre in post` AND `len(post) > len(pre)` — both are FALSE for a
# rotation, which shrinks the field and drops old text on purpose. Smuggling the
# cut through the append's verification would have required weakening the exact
# assertions that catch a real clobber. So rotation runs BEFORE compose, as a
# separate CAS-protected write, and the append then proceeds normally against the
# reduced value with its verification fully intact.
#
# FAIL-OPEN: any rotation failure leaves the field untouched and the append
# proceeds. A note write must never be lost because its bound could not run.
ROTATE_AT_BYTES = int(os.environ.get("GOAL_NOTE_ROTATE_BYTES", "32768"))
ROTATE_KEEP_BYTES = int(os.environ.get("GOAL_NOTE_KEEP_BYTES", "16384"))
ROTATE_DISABLED = os.environ.get("GOAL_NOTE_ROTATE", "").lower() in ("0", "off", "no")
# Must not begin with '{' or '[' — the update wrapper JSON-decodes a value that
# does (aspirations-update-goal.sh parse_value), storing an object instead of
# text. That is why this reads "NOTE HISTORY ROTATED" and not "[ROTATED ...]".
ROTATE_NOTICE_HEAD = "NOTE HISTORY ROTATED"

# Pin markers for a goal DESCRIPTION's non-evictable head ().
# "[hoisted:..." is the measured in-corpus spelling — the STEP 0 that the
# 2026-09-22 rotation evicted from  carried it — and "[pin:..." is the
# named form new blocks should use. The marker is the AUTHOR's declaration that
# a block is executable head; no rotation may cut a block carrying one. Pinning
# is on the marker, never on position: the executable head is a RUN of blocks
# (statement + protocol + routing table) and its length is not knowable from
# position alone — a first-block pin saved STEP 0 while the measured rotation
# still cut the Step-1 and routing-table blocks that made it runnable.
PIN_MARKERS = ("[hoisted:", "[pin:")


def split_blocks(value: str) -> "list[str]":
    """Split an append-ordered field into blocks, oldest first.

    compose() builds each block as `<text>\n[appended:<marker>]`, blocks joined
    by a blank line, so a block ENDS at its sentinel line. Text before the first
    sentinel is legacy pre-sentinel content and is returned as the first element
    so it is never silently dropped.
    """
    if not value:
        return []
    out, buf = [], []
    for line in value.split("\n"):
        buf.append(line)
        if line.startswith(SENTINEL_PREFIX) and line.rstrip().endswith("]"):
            out.append("\n".join(buf).strip("\n"))
            buf = []
    tail = "\n".join(buf).strip("\n")
    if tail:
        out.append(tail)
    return out


def pinned_description_indices(blocks: "list[str]") -> "list[int]":
    """Indices of the non-evictable blocks of a description ().

    A block the AUTHOR pinned — it carries a ``[pin:...]`` or ``[hoisted:...]``
    marker — is executable head and no rotation may cut it. The marker is the
    pin, never the position: the executable head is a run of blocks (statement
    + protocol + routing table) and its length is not knowable from position
    alone, so pinning "block 0" by ordinal saved STEP 0 while the measured
    2026-09-22 rotation still cut the Step-1 and routing-table blocks that made
    it runnable.

    BLOCK 0 IS PINNED UNCONDITIONALLY, on top of the marked set. The goal's
    check is absolute — the FIRST block is never the block that gets archived,
    marker or no marker — and a later marked block must not quietly re-arm
    keep-newest over an unmarked original statement: the measured g-115-817
    record reached exactly that state (12 live blocks, block 0 a rotation
    notice, the author-marked STEP 0 already in the sink).
    """
    marked = [i for i, b in enumerate(blocks)
              if any(m in b for m in PIN_MARKERS)]
    if not blocks:
        return []
    if 0 in marked:
        return marked
    return [0] + marked


def _sink_path(goal_id: str, field: str):
    """Append-only archive sink, fleet-visible beside the other world records.

    Under an EXISTING world/ top-level dir on purpose: the L1 path-resolution
    hook refuses a NEW top-level entry under a governed root, and inventing one
    is the documented cruft failure (.claude/rules/path-resolution.md).
    """
    from _paths import WORLD_DIR  # resolved per-agent; never derived by hand
    d = Path(WORLD_DIR) / "audit-reports" / "goal-note-archive"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{goal_id}.{field}.md"


def rotate_oversize(goal_id: str, field: str, source: str, pre: str) -> str:
    """Bound `pre` by moving the OLDEST blocks to an archive sink.

    Returns the reduced field value, or `pre` unchanged when rotation does not
    apply or could not be completed safely. Never raises.

    ORDER IS ARCHIVE-BEFORE-DELETE and is not negotiable: the sink is written
    and READ BACK before a single byte leaves the record, so a failure at any
    point leaves the live field whole.
    """
    if ROTATE_DISABLED or len(pre.encode("utf-8")) <= ROTATE_AT_BYTES:
        return pre
    try:
        blocks = split_blocks(pre)
        if len(blocks) < 2:
            return pre  # one block cannot be split; nothing safe to cut

        # RETENTION POLICY IS FIELD-SPECIFIC (). For a progress_note
        # the keep-newest polarity is right. For a description it is WRONG: the
        # oldest blocks are the executable task statement, and a keep-newest
        # rotation cuts exactly the part that makes the goal runnable (guard-7281).
        # So a description's author-pinned blocks are non-evictable and the
        # block 0 floor is pinned unconditionally (the goal's absolute check:
        # the FIRST block is never archived), and the rest rotates keep-newest.
        # When the pinned head ALONE exceeds the keep bound, fail open:
        # shrinking is impossible without cutting the statement, and (a) the
        # statement survives beats (b) the field is bounded.
        pinned = set(pinned_description_indices(blocks)) if field == "description" else set()
        pinned_bytes = sum(len(blocks[i].encode("utf-8")) for i in pinned)

        # Keep the pinned blocks whole, then add the NEWEST non-pinned blocks
        # under the SAME absolute bound the original loop used: kept (pinned
        # head + newest rest) fits in ROTATE_KEEP_BYTES. The break's guard
        # counts only NON-PINNED blocks already kept, so the first non-pinned
        # block is always admitted, exactly as the original loop always kept
        # its first (newest) block — the bound starts biting from the second
        # non-pinned block on. That guard is what keeps this from a worse
        # failure than the one it replaces: with a pinned head that alone
        # approaches the bound, counting pinned blocks in the guard would
        # refuse even the newest block, cut everything non-pinned, and leave
        # notice + pinned — which is LARGER than `pre` (measured: 852 B out of
        # 806 B in). The field's floor is the pinned head; rotation shrinks
        # toward it, never past it, and a field that IS its pinned head
        # (nothing cut) is returned whole by the `if not cut` guard below.
        keep = set(pinned)
        total = pinned_bytes
        nonpinned_kept = 0
        for i in range(len(blocks) - 1, -1, -1):  # newest -> oldest
            if i in keep:
                continue
            n = len(blocks[i].encode("utf-8"))
            if nonpinned_kept and total + n > ROTATE_KEEP_BYTES:
                break
            keep.add(i)
            total += n
            nonpinned_kept += 1
        kept = [blocks[i] for i in sorted(keep)]
        cut = [blocks[i] for i in range(len(blocks)) if i not in keep]
        if not cut:
            return pre
        stamp = _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        sink = _sink_path(goal_id, field)
        header = (f"\n\n<!-- rotated {stamp} from {goal_id}.{field}: "
                  f"{len(cut)} block(s), {sum(len(b.encode('utf-8')) for b in cut)} bytes -->\n")
        payload = header + "\n\n".join(cut) + "\n"
        before = sink.stat().st_size if sink.exists() else 0
        existing = sink.read_text(encoding="utf-8") if sink.exists() else ""
        # IDEMPOTENT ON RETRY. This function can legitimately bail AFTER the
        # archive and BEFORE the cut — measured 2026-09-21, the field-shrink
        # guard refused the reduction (6% of original, floor 25%) and fail-open
        # correctly returned the field whole. The blocks were then archived AND
        # still live, so the next attempt re-archived them: 2 rotation headers,
        # 84 chunks, every one redundant. Re-archiving is not a data risk, but an
        # append-only sink that doubles on every retry stops being readable.
        already = bool(existing) and cut[0][:200] in existing and cut[-1][:200] in existing
        if not already:
            with sink.open("a", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
        # VERIFY THE ARCHIVE BEFORE CUTTING — a write that returned is not a
        # write that landed. Confirm by an independent read, on content, not size.
        back = sink.read_text(encoding="utf-8")
        if (not already and sink.stat().st_size <= before) or cut[-1][:200] not in back:
            return pre  # archive unverified -> cut nothing
        # PROVENANCE IN THE SAME EDIT AS THE CUT (guard-6105): a reader of the
        # reduced field must be able to find what was removed without knowing
        # this code exists. For a description the cut is the oldest NON-PINNED
        # blocks (the pinned head stays), so the notice must not claim it cut
        # "the oldest" when a pinned head was deliberately kept.
        if field == "description" and pinned:
            cut_phrase = (f"the {len(cut)} oldest non-pinned block(s) "
                          f"({sum(len(b.encode('utf-8')) for b in cut)} bytes)")
            keep_phrase = (" the pinned (non-evictable) head was kept, "
                           "not just the newest blocks.")
        else:
            cut_phrase = (f"the {len(cut)} oldest block(s) "
                          f"({sum(len(b.encode('utf-8')) for b in cut)} bytes)")
            keep_phrase = " the newest blocks were kept."
        notice = (f"{ROTATE_NOTICE_HEAD} {stamp}: {cut_phrase} were moved to "
                  f"{sink} to keep this field readable.{keep_phrase} Nothing was "
                  f"deleted — read them there. This rotation is automatic at "
                  f"{ROTATE_AT_BYTES} bytes (GOAL_NOTE_ROTATE_BYTES); set "
                  f"GOAL_NOTE_ROTATE=off to disable.")
        reduced = notice + "\n\n" + "\n\n".join(kept)
        if reduced[:1] in ("{", "["):
            return pre
        # CAS: a peer may have appended since our read. Rotating from a stale
        # value would drop their block, which is the one thing worse than an
        # oversize field.
        fresh = read_goal(goal_id, source).get(field)
        if not isinstance(fresh, str) or fresh != pre:
            return pre
        # The re-read above narrows the window; it cannot close it. The write is
        # a separate process, so a peer append landing between that read and the
        # write would be overwritten, and no read taken afterwards could see it:
        # the clobbered store is byte-identical to `reduced` (). Only
        # a compare INSIDE the daemon's write lock is atomic with the write, so
        # the hash of the value we composed from rides along, and a mismatch is
        # refused with nothing written (rc!=0 -> `return pre` below, unless the
        # store shows this rotation landed anyway -> main()'s CAS reports the
        # concurrent modification and a re-run rotates cleanly).
        expect = hashlib.sha256(pre.encode("utf-8")).hexdigest()
        res = _run(bash_cmd(
            SCRIPTS / "aspirations-update-goal.sh",
            "--source", source, "--value-stdin",
            "--override-narrative-replace",
            f"note-history rotation to {sink.name} (g-115-10349); "
            "archive written and read-back-verified before the cut",
            # A rotation is a DELIBERATE large shrink, which is the case the
            # field-shrink guard explicitly carves out. Measured without it:
            # rc=1 field_shrink_blocked at 6% of original against a 25% floor,
            # so the bound could never reach the records that most need it. The
            # guard stays fully armed for every other writer.
            "--override-shrink",
            f"note-history rotation (g-115-10349): the removed blocks are in {sink.name}, "
            "written and read-back-verified BEFORE this cut, and the newest blocks are kept",
            "--expect-sha256", expect,
            goal_id, field,
        ), input=reduced)
        if res.returncode != 0:
            # A refusal is not proof that nothing landed (guard-7050). rt_call
            # re-sends the identical request after a stale-daemon recycle or a
            # timeout and keeps only the LAST reply, and the re-sent copy of a
            # write that already landed meets that write and is refused 409. So
            # confirm against the store, keyed on this rotation's own notice.
            try:
                landed = read_goal(goal_id, source).get(field)
            except SystemExit:
                landed = None
            if not (isinstance(landed, str) and notice in landed):
                return pre
            print(f"WARNING: note-history rotation of {goal_id}.{field} landed although "
                  f"the update wrapper exited {res.returncode} (e.g. a refused re-sent copy: "
                  "the transport re-sends after a timeout or a stale-daemon recycle), so "
                  "whether the send that landed was precondition-checked cannot be "
                  "confirmed from here.", file=sys.stderr)
            return landed
        # A daemon predating the precondition ignores the header and still
        # answers 200 (guard-5505). That write is no worse than before the fix,
        # so it stands, but it must not pass silently as a checked one.
        if f"precondition_checked field-sha256={expect}" not in (res.stderr or ""):
            print(f"WARNING: note-history rotation of {goal_id}.{field} was written, but "
                  "its concurrent-append precondition was NOT confirmed by the daemon (a "
                  "build predating g-115-10535 ignores the check and still answers 200; "
                  "restart it: bash core/scripts/mind-api-start.sh --restart). A peer "
                  "append landing in the write window would not have been refused.",
                  file=sys.stderr)
        after = read_goal(goal_id, source).get(field)
        if not isinstance(after, str) or ROTATE_NOTICE_HEAD not in after or kept[-1][:200] not in after:
            return pre  # could not confirm; caller continues against the original
        return after
    except Exception:  # noqa: BLE001
        return pre  # fail-open, always


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

    # Bound the field BEFORE composing, so the append below runs against the
    # reduced value and verify_post's pre-survival assertions stay exact.
    #
    # KEEP THE PRE-ROTATION LENGTH: this call REBINDS `pre`, so every length the
    # caller is shown below is measured against the REDUCED value. Reporting that
    # as a bare `pre_len` is honest about a base the caller never saw, and it has
    # twice sent an agent hunting for data loss that never happened — a 77 KB
    # "discrepancy" chased against a hand pre-read (bravo, cc-05), and a full
    # window spent building a recovery directory for 287 KB that was never at
    # risk (alpha, cc-04, 2026-09-22). Both readers were doing the right thing:
    # the number really did not add up, and nothing in this output explained it.
    pre_unrotated_len = len(pre)
    pre = rotate_oversize(args.goal_id, args.field, args.source, pre)

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
    # the span properly needs a compare-and-swap on the field INSIDE the daemon's
    # write lock. That now exists -- aspirations-update-goal.sh --expect-sha256
    # (, used by rotate_oversize above); adopting it for THIS write
    # is 's, and until then this re-read only narrows the window.
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
    # Announce the rotation to the CALLER, not only to the field. The notice
    # written into the value is for whoever READS the goal later; this key is for
    # whoever just WROTE it and is about to reconcile lengths. Present only when a
    # rotation actually cut something, so its absence stays meaningful.
    if pre_unrotated_len != len(pre):
        out["rotated"] = {
            "pre_len_before_rotation": pre_unrotated_len,
            "moved_bytes": pre_unrotated_len - len(pre),
            "archive": str(_sink_path(args.goal_id, args.field)),
            "note": ("pre_len above is measured AFTER this rotation. Nothing was "
                     "deleted — the moved blocks are in `archive`. A large drop "
                     "here is expected, not data loss."),
        }
    print(json.dumps(out, indent=2))
    return RC_OK


if __name__ == "__main__":
    sys.exit(main())
