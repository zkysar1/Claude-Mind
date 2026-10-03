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
     store's long-text or structured canaries.

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
verbatim, and length GREW. When it fails, ``restore_pre`` writes PRE back by
compare-and-swap, and only while the field still holds what this run stored
(g-115-11615).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
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
# Imported, not re-typed, for the same reason as the four above: the marker
# convention and its refusal must not fork between the two sides ().
wrapped_marker_refusal = _gfa.wrapped_marker_refusal

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
    # pipeline joined 2026-09-15 () — the third store, and the first
    # whose shape was MEASURED against this contract instead of assumed to match
    # the other two. Three findings, each of which would have been a silent
    # defect had it gone the other way:
    #
    #   1. rows_keys is EMPTY ON PURPOSE. pipeline-read.sh wraps rows in no key
    #      at ALL: `--id` returns a BARE record object (caught by extract_row's
    #      `"id" in parsed` branch) and `--stage`/`--unreflected`/
    #      `--replay-candidates`/`--narrative` return BARE LISTS; `--counts` and
    #      `--meta` return dicts of scalars with zero list-valued keys. Measured
    #      across all six modes. Listing a speculative ("pipeline", "results")
    #      here would be a name no producer emits — the failure class where a
    #      reader filters on tags nothing writes and reports a confident zero.
    #      An empty tuple is honest and costs nothing: a dict with no "id" still
    #      falls through to [] and is refused RC_READ_UNSAFE, which is correct.
    #   2. The canaries are the LARGE FREE-TEXT fields, and the set is measured,
    #      not guessed: position 81/81, rationale 81/81, claim 81/81,
    #      adversarial_pre_mortem 12/81 over the whole 81-record store — and
    #      ZERO records carry none of them, so the projection guard cannot
    #      false-refuse a real read. title/stage/type would be useless canaries
    #      because any projection produces them.
    #   3. THE WRITE ENDPOINT COERCES AND THE OTHER TWO DO NOT. guardrails and
    #      reasoning-bank write through /v1/store/set-field; pipeline writes
    #      through /v1/pipeline/update-field, which runs `_parse_value` on the
    #      string — "true"/"false"/"null" become bools/None, a leading { or [
    #      is tried as JSON, and a numeric string becomes int/float. A composed
    #      append can never hit any of those arms, because compose() always ends
    #      the value with "\n[appended:<marker>]": that sentinel is not JSON,
    #      not numeric, and not a literal keyword. The sentinel that exists for
    #      idempotence is therefore also what keeps a pipeline text field typed
    #      as text. Do NOT "simplify" compose to drop it.
    #   4. AND A FIFTH DIMENSION THIS MAP DOES NOT MODEL AT ALL (fresh-eyes F1,
    #      found reviewing the change above rather than while writing it): the
    #      pipeline write has SIDE EFFECTS ON OTHER FIELDS. update_field does
    #      `rec[field] = value` and THEN `apply_derived_surprise(rec)`, so an
    #      append aimed at a DERIVATION INPUT does two silent things at once.
    #      `outcome` is the live case: it is a plain str, so the isinstance(pre,
    #      str) gate passes, the composed value is not coerced, and the result is
    #      a controlled-vocabulary field turned into
    #      "CORRECTED\n\nnote\n[appended:m]" PLUS a `surprise` silently nulled
    #      (derive_surprise returns None for a non-CONFIRMED/CORRECTED outcome).
    #      guardrails and reasoning-bank write through /v1/store/set-field and
    #      have no such coupling. APPEND ONLY TO NARRATIVE FIELDS on this store —
    #      position, rationale, claim, outcome_detail, resolution_method,
    #      measurement_channel — never to outcome/stage/type/confidence.
    #      No denylist is wired: one call site is not an abstraction, and a wrong
    #      field here is a caller error, not a class the map can close.
    "pipeline": {
        "read": "pipeline-read.sh",
        "write": "pipeline-update-field.sh",
        "rows_keys": (),
        "canaries": ("position", "rationale", "claim", "adversarial_pre_mortem"),
    },
}


def _die(code: int, msg: str):
    print(json.dumps({"ok": False, "rc": code, "error": msg}, indent=2), file=sys.stderr)
    sys.exit(code)


def _run(argv, **kw):
    # The SSOT's runner. It sends an input= value as UTF-8 BYTES, because text
    # mode on Windows turns every \n into \r\n on the way in ().
    return _gfa._run(argv, **kw)


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


def written_value(stdout: str, field: str):
    """The text THIS run's write stored, taken from the write's own response.

    Each write wrapper prints the record its endpoint returned from inside the
    locked write, so this is what the write put in the field, not what was sent.
    None when the response holds no such text field.
    """
    try:
        rec = _parse_json_tail(stdout)
    except Exception:  # noqa: BLE001
        return None
    value = rec.get(field) if isinstance(rec, dict) else None
    return value if isinstance(value, str) else None


def restore_pre(store, record_id, field, pre, written, current, sentinel) -> str:
    """After a failed verify, write PRE back by compare-and-swap ().

    Returns one sentence for the rc=7 message. PRE is written back only when all
    four of these hold:
      - the write's response says what this run stored (``written``);
      - that stored value fails verification itself. If it passed, the failed
        re-read is a lag or a later writer, not damage done by this run;
      - the field still holds exactly that value (``current``, the re-read just
        taken). Any other value is another writer's, and it is never overwritten;
      - PRE is not empty, because the write wrappers refuse an empty value.
    """
    if written is None:
        return ("PRE was NOT restored: the write's response did not show what it stored, "
                "so this run cannot tell its own value from another writer's.")
    if not verify_post(pre, written, sentinel):
        return ("PRE was NOT restored: the write stored a sound value, so the failed re-read "
                "is a lag or a later writer's change, not damage done by this run.")
    if current != written:
        return ("PRE was NOT restored: the field no longer holds this run's value. Another "
                "writer changed it, and their value is not overwritten.")
    if not pre:
        return (f"PRE was NOT restored: the field was empty before this run, and the write "
                f"wrappers refuse an empty value. It holds the {len(written)} characters this "
                "run wrote, without the marker, and a re-run would append after them.")
    res = _run(_bash(STORES[store]["write"], "--value-stdin", record_id, field), input=pre)
    if res.returncode != 0:
        return (f"Restoring PRE FAILED (rc={res.returncode}): {res.stderr.strip()[:300]}. "
                f"The field still holds the {len(written)} characters this run wrote.")
    # The wrappers' value reader drops trailing newlines, so that is what comes back.
    kept = pre.rstrip("\n")
    try:
        again = read_record(store, record_id).get(field)
    except SystemExit:
        again = None
    if again != kept:
        return ("PRE was written back, but a re-read does not show it, so the field's state "
                "is unknown. Read the record before doing anything else.")
    dropped = len(pre) - len(kept)
    return (f"PRE was restored ({len(kept)} characters) and a re-read confirms it"
            + (f", minus {dropped} trailing newline(s) the write wrapper drops" if dropped else "")
            + ". Re-running the identical command starts again from PRE.")


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

    refusal = wrapped_marker_refusal(args.marker)
    if refusal:
        _die(RC_USAGE, refusal)

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

    # WRITE on STDIN, never in argv (). On Windows, Git bash cuts an
    # argv word that holds whitespace or a glob character to 8,186 characters,
    # at rc=0. On ZDS a composed value of 8,296 characters was stored cut
    # mid-sentence, sentinel lost. --value-stdin is an
    # accepted flag on all three write wrappers. Any OTHER flag is refused with
    # exit 2: the pre-strict versions slid the next token into VALUE and clobbered
    # guard-1615 at rc=0 ().
    cfg = STORES[args.store]
    res = _run(_bash(cfg["write"], "--value-stdin", args.record_id, args.field), input=new)
    if res.returncode != 0:
        _die(RC_WRITE_FAILED, f"write failed (rc={res.returncode}): {res.stderr.strip()[:600]}")
    written = written_value(res.stdout, args.field)

    # VERIFY by RE-READING, and against PRE — never against `new`.
    post_row = read_record(args.store, args.record_id)
    problems = verify_post(pre, post_row.get(args.field), sentinel)
    if problems:
        _die(RC_VERIFY_FAILED, "post-write verification FAILED: " + "; ".join(problems) + ". "
             + restore_pre(args.store, args.record_id, args.field, pre, written,
                           post_row.get(args.field), sentinel))

    print(json.dumps({"ok": True, "changed": True, "store": args.store, "id": args.record_id,
                      "field": args.field, "marker": args.marker,
                      "pre_len": len(pre), "post_len": len(post_row.get(args.field) or "")},
                     indent=2))
    return RC_OK


if __name__ == "__main__":
    sys.exit(main())
