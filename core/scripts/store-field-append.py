#!/usr/bin/env python3
"""store-field-append — safe read-modify-write append onto ONE governed-store text field.

The store-side sibling of ``goal-field-append.py`` (gap-106, g-115-5298).

WHY THIS EXISTS
``guardrails-update-field.sh`` and ``reasoning-bank-update-field.sh`` take a
WHOLE-FIELD write, so amending an existing record is a read-modify-write with no
guard of its own. That RMW was hand-rolled four times in one session (guard-1710
and guard-2598 under g-350-151; guard-2908 and guard-991 under g-250-336). Two
failure modes, both invisible at write time:

  - no idempotence marker  -> a retry after a partial failure DOUBLE-APPENDS
  - no drift check         -> the append lands on a record another agent has
                              since rewritten

There is a third cost the gap measured separately: the inline-heredoc form of
the hand-rolled wrapper is REFUSED by the bare-bash-authoring gate (guard-580),
because a guarded read/write spawns bash from Python. The identical procedure in
a .py file is not refused. So today the same correct procedure passes or fails on
WHERE it happens to be typed, and the only workaround is to leave a throwaway
script on disk. This file is that script, written once.

THE CONTRACT IS NOT RE-DERIVED HERE. The four pure helpers (``sentinel_for``,
``is_read_projected``, ``compose``, ``verify_post``) are IMPORTED from
``goal-field-append.py``, which is the SSOT for this contract and is proven in
production. Re-typing them would fork the safety invariants: a later fix to the
verification rule would land in one file and silently not the other, and nothing
would fail when it did. That is the no-transcription hazard (guard-2676) applied
to a helper rather than to a loop. The import needs importlib only because the
SSOT's filename is hyphenated and therefore not a legal module name — the
indirection is a naming artifact, not a design choice, and it fails LOUD if the
SSOT moves.

WHAT DIFFERS FROM THE GOAL-SIDE SSOT, and why

  1. NO PROJECTION HAZARD ON THE READ, but the discriminator is kept anyway.
     ``aspirations-query.sh`` projects to six keys by default, which is what
     forced guard-1251's discriminator on the goal side. Measured 2026-08-08:
     ``guardrails-read.sh --id guard-147`` returns the FULL 19-key record
     (action_hint 411 chars) and ``reasoning-bank-read.sh --id rb-245`` the full
     24-key record (content 516 chars) — neither projects today. The check is
     retained because "does not project today" is a property of the current
     daemon build, not of the contract, and the cost of being wrong is a
     silently destroyed field. It refuses on a record carrying NONE of the
     store's long-text/structured canaries.

  2. AN OPTIONAL ``--anchor``. The goal-side has marker + projection + verify.
     The hand-rolled store procedure this replaces also checked that expected
     text was still present before appending, so a record that drifted
     underneath is refused rather than amended. Supplied text must appear in PRE
     or the run aborts. Optional, because a first note onto an empty field has no
     anchor to check — requiring one would make the common case impossible.

  3. TWO STORES, ONE SCRIPT, selected by ``--store``. The gap asked for this
     shape. Each store contributes only a read command, a write command, and its
     canary set; all logic is shared, so a third store is a table row.

IDEMPOTENCY
The caller supplies a marker. A one-line sentinel ``[appended:<marker>]`` is
written after the text, and a re-run that sees that sentinel in the CURRENT
value exits 0 having changed nothing — so a retry after a partial failure is
safe. Identical to the goal-side, because it is the same function.

VERIFICATION
The post-write assertion compares against the PRE value, never against the
string this script constructed — comparing to your own construction only proves
the write echoed (sig-40). Asserts the sentinel is present, PRE survived
verbatim, and length GREW.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent

# --- import the contract from its SSOT ------------------------------------
# Hyphenated filename => not importable by name. Fails loud if the SSOT moves,
# which is the correct direction: a missing SSOT must never degrade to a
# re-typed local copy of the safety invariants.
_SSOT = SCRIPTS / "goal-field-append.py"


def _load_ssot():
    spec = importlib.util.spec_from_file_location("_goal_field_append", _SSOT)
    if spec is None or spec.loader is None:            # pragma: no cover - defensive
        raise ImportError(f"cannot load contract SSOT at {_SSOT}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_gfa = _load_ssot()
sentinel_for = _gfa.sentinel_for
compose = _gfa.compose
verify_post = _gfa.verify_post
cas_conflict = _gfa.cas_conflict

RC_OK = 0
RC_USAGE = 2
RC_READ_UNSAFE = 3
RC_FIELD_SHAPE = 4
RC_VALUE_SHAPE = 5
RC_WRITE_FAILED = 6
RC_VERIFY_FAILED = 7
RC_ANCHOR_ABSENT = 8          # store-side only; no goal-side equivalent
RC_CONCURRENT_MODIFICATION = 9  # same number on the goal side, deliberately ()

# Per-store wiring. `canaries` are keys a hypothetical projection could not
# produce — presence of any ONE proves the read is the real record.
STORES = {
    "guardrails": {
        "read": "guardrails-read.sh",
        "write": "guardrails-update-field.sh",
        "rows_keys": ("guardrails", "results", "entries"),
        "canaries": ("action_hint", "when_to_use", "trigger_condition", "utilization"),
    },
    "reasoning-bank": {
        "read": "reasoning-bank-read.sh",
        "write": "reasoning-bank-update-field.sh",
        "rows_keys": ("reasoning_bank", "entries", "results"),
        "canaries": ("content", "when_to_use", "failure_lesson", "utilization"),
    },
}


def _die(code: int, msg: str):
    print(json.dumps({"ok": False, "rc": code, "error": msg}, indent=2), file=sys.stderr)
    sys.exit(code)


def _run(argv):
    return subprocess.run(argv, capture_output=True, text=True)


def _bash(script_name: str, *args) -> list:
    # Absolute path to the interpreter is the SSOT's own pattern; a bare "bash"
    # argv[0] resolves via CreateProcess on Windows and can reach the WSL
    # launcher instead of the shell (guard-580).
    return _gfa.bash_cmd(SCRIPTS / script_name, *args)


def _parse_json_tail(raw: str):
    return _gfa._parse_json_tail(raw)


def extract_row(parsed, rows_keys) -> "list":
    """Normalize the read payload to a list of records, whatever shape it used."""
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for k in rows_keys:
            v = parsed.get(k)
            if isinstance(v, list):
                return v
        # A bare single-record object is a legal shape for these readers.
        if "id" in parsed:
            return [parsed]
    return []


def is_read_projected(row: dict, canaries) -> bool:
    """True when the record shows no sign of carrying its full field set.

    Same purpose as the goal-side check and deliberately the same shape: a read
    that omits large text fields would make `compose` append onto an empty
    string and destroy the field (guard-1251). "The field is empty" can never be
    the discriminator, because a first note onto an empty field is legitimate.
    """
    if not isinstance(row, dict):
        return True
    return not any(k in row for k in canaries)


def read_record(store: str, record_id: str) -> dict:
    cfg = STORES[store]
    res = _run(_bash(cfg["read"], "--id", record_id))
    if res.returncode != 0:
        _die(RC_READ_UNSAFE, f"read failed (rc={res.returncode}): {res.stderr.strip()[:400]}")
    try:
        parsed = _parse_json_tail(res.stdout)
    except Exception as exc:  # noqa: BLE001
        _die(RC_READ_UNSAFE, f"read returned unparseable output: {exc}")
    rows = extract_row(parsed, cfg["rows_keys"])
    if len(rows) != 1:
        _die(RC_READ_UNSAFE,
             f"expected exactly 1 record for {record_id} in {store}, got {len(rows)}. "
             "An empty result is a FAILED measurement, not a measurement of empty "
             "(guard-1091).")
    row = rows[0]
    if not isinstance(row, dict):
        _die(RC_READ_UNSAFE, "read returned a non-object record")
    if is_read_projected(row, cfg["canaries"]):
        _die(RC_READ_UNSAFE,
             f"read returned a record carrying none of {store}'s canary keys "
             f"({len(row)} keys: {sorted(row)[:8]}). Large text fields may be omitted, so "
             "appending onto this read could silently destroy the field. Refusing "
             "(guard-1251).")
    return row


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="store-field-append.py", add_help=True)
    ap.add_argument("--store", required=True, choices=tuple(STORES))
    ap.add_argument("record_id")
    ap.add_argument("field")
    ap.add_argument("marker", help="idempotency token; a re-run with the same marker is a no-op")
    ap.add_argument("text", nargs="?", default=None, help="the text to append")
    ap.add_argument("--value-file", default=None, help="read the appended text from a file")
    ap.add_argument("--value-stdin", action="store_true", help="read the appended text from stdin")
    ap.add_argument("--anchor", default=None,
                    help="refuse unless this text is present in the CURRENT value "
                         "(drift guard; omit for a first note onto an empty field)")
    args = ap.parse_args(argv)

    sources = [s for s in (args.text is not None, args.value_file, args.value_stdin) if s]
    if len(sources) != 1:
        _die(RC_USAGE, "supply the text EXACTLY once: positional, --value-file, or --value-stdin")

    if args.value_file:
        try:
            text = Path(args.value_file).read_text(encoding="utf-8")
        except OSError as exc:
            _die(RC_USAGE, f"cannot read --value-file: {exc}")
    elif args.value_stdin:
        text = sys.stdin.read()
    else:
        text = args.text
    text = text.strip("\n")
    if not text:
        _die(RC_VALUE_SHAPE, "refusing to append empty text")

    sentinel = sentinel_for(args.marker)
    row = read_record(args.store, args.record_id)
    pre = row.get(args.field)

    if pre is None:
        pre = ""
    if not isinstance(pre, str):
        _die(RC_FIELD_SHAPE,
             f"field '{args.field}' is a {type(pre).__name__}, not text. This helper appends to "
             "TEXT fields only. A nested write means reconstructing the whole parent subdocument, "
             "and every sibling key you omit is dropped silently (guard-2444) — do that "
             "deliberately, by hand, with a PRE/POST sibling-survival assertion.")

    # IDEMPOTENCE before ANCHOR: a completed prior run is a no-op regardless of
    # whether the anchor still holds, and reporting "anchor absent" for work that
    # already landed would send a caller chasing drift that does not exist.
    if sentinel in pre:
        print(json.dumps({"ok": True, "changed": False,
                          "reason": "idempotent: marker already present",
                          "store": args.store, "id": args.record_id, "field": args.field,
                          "marker": args.marker, "pre_len": len(pre)}, indent=2))
        return RC_OK

    if args.anchor is not None and args.anchor not in pre:
        _die(RC_ANCHOR_ABSENT,
             f"anchor text absent from {args.record_id}.{args.field} — the record has drifted "
             "since the anchor was chosen, so this append would land on content its author "
             "never read. Re-read the record and re-derive the amendment.")

    new = compose(pre, text, args.marker)
    if new[:1] in ("{", "["):
        _die(RC_VALUE_SHAPE,
             "composed value starts with '{' or '[' — an update wrapper may JSON-decode it "
             "rather than store it as text. Prefix the existing content or the appended text "
             "so it does not begin with a JSON opener.")

    # PRE-WRITE RE-READ (compare-and-swap) — . Inherited from the SSOT
    # by import, never re-typed: the read at the top of main() and the write
    # below are two subprocess round-trips with nothing serializing the span, so
    # a peer's append landing in between is clobbered by this write while BOTH
    # writers' verify_post() passes. --anchor does NOT cover this — it is checked
    # against the value read BEFORE the window opens, so both writers see it
    # satisfied. Full rationale (including why locked_rmw cannot reach across a
    # subprocess boundary, and why class-(a) merge protection does not conserve
    # a same-key append) is in the goal-field-append.py call site.
    fresh_row = read_record(args.store, args.record_id)
    current = fresh_row.get(args.field)
    if current is None:
        current = ""
    if isinstance(current, str) and sentinel in current:
        print(json.dumps({"ok": True, "changed": False,
                          "reason": "idempotent: marker landed concurrently between read and write",
                          "store": args.store, "id": args.record_id, "field": args.field,
                          "marker": args.marker, "pre_len": len(pre)}, indent=2))
        return RC_OK
    conflict = cas_conflict(pre, current)
    if conflict:
        _die(RC_CONCURRENT_MODIFICATION,
             "refusing to write — " + conflict + ". NOTHING WAS WRITTEN and no text was "
             "lost. Re-run the identical command: the fresh read picks up their text and "
             "the marker keeps the retry idempotent (g-115-5638).")

    # WRITE positionally. Never a flag in the value slot: these wrappers refuse
    # unknown leading-dash args with exit 2, and the pre-strict versions slid the
    # next token into VALUE and clobbered guard-1615 at rc=0 ().
    cfg = STORES[args.store]
    res = _run(_bash(cfg["write"], args.record_id, args.field, new))
    if res.returncode != 0:
        _die(RC_WRITE_FAILED, f"write failed (rc={res.returncode}): {res.stderr.strip()[:600]}")

    # VERIFY by RE-READING, and against PRE — never against `new`.
    post_row = read_record(args.store, args.record_id)
    problems = verify_post(pre, post_row.get(args.field), sentinel)
    if problems:
        _die(RC_VERIFY_FAILED, "post-write verification FAILED: " + "; ".join(problems))

    print(json.dumps({"ok": True, "changed": True, "store": args.store, "id": args.record_id,
                      "field": args.field, "marker": args.marker,
                      "pre_len": len(pre), "post_len": len(post_row.get(args.field) or "")},
                     indent=2))
    return RC_OK


if __name__ == "__main__":
    sys.exit(main())
