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

def sentinel_for(marker: str) -> str:
    return f"[appended:{marker}]"


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

    # WRITE — positionally, no flags in the value slot (guard-1047 / guard-2460).
    res = _run(bash_cmd(
        SCRIPTS / "aspirations-update-goal.sh",
        "--source", args.source, args.goal_id, args.field, new,
    ))
    if res.returncode != 0:
        _die(RC_WRITE_FAILED, f"write failed (rc={res.returncode}): {res.stderr.strip()[:600]}")

    # VERIFY against PRE, never against `new` — comparing to your own
    # construction only proves the write echoed (sig-40).
    try:
        written = _parse_json_tail(res.stdout)
    except Exception as exc:  # noqa: BLE001
        _die(RC_VERIFY_FAILED, f"write returned unparseable output, cannot verify: {exc}")
    post = written.get(args.field)
    problems = verify_post(pre, post, sentinel)
    if problems:
        _die(RC_VERIFY_FAILED,
             "write landed but verification FAILED: " + "; ".join(problems) +
             ". Recover the PRE value from core/scripts/history.py list world/aspirations.jsonl "
             "(the snapshot BEFORE the entry naming this write).")

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
