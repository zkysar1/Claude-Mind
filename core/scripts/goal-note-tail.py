#!/usr/bin/env python3
"""goal-note-tail — read the END of an append-ordered goal narrative field.

WHY THIS EXISTS (g-115-9936)

guard-2043 states that a truncated read of an append-ordered field is NOT a read
of it, because the CORRECTIVE BLOCK LANDS AT THE END. That rule is correct and it
leaves a claimer of a large-note goal with only two moves: read the whole field,
or release the goal unstarted. Measured 2026-09-17 (alpha, cc-04, non-terminal
corpus n=2889): 41 goals carry a read surface at or over the 62,500 B claim cap
(`aspirations-claim.sh`), holding 4,306,811 B between them. For those 41 there was
no third move, and no tooling offered one -- the only way to see a goal's current
state was for an author to have hand-written a summary somewhere.

THIS IS THE INVERSE OF THE TRUNCATION guard-2043 FORBIDS, AND THAT IS THE WHOLE
POINT. A truncated read drops the END silently. This drops the BEGINNING and says
so loudly -- every invocation prints how many blocks and how many bytes were
omitted, and the markers of the omitted blocks, so the omission is explicit,
quantified, and recoverable (re-run with a larger --blocks, or read the field
whole). An explicit tail is a sound read of the corrective block; a silent head
is not a read at all.

WHAT IT DOES NOT DO: it does not write, fold, cap, or refuse anything. It is
read-only by construction -- it imports the store reader and no writer.

THE GRAMMAR is `goal-field-append.py`'s own: `compose()` terminates every block
with a line `[appended:<marker>]`. Text preceding the first sentinel (a note
written before the append helper existed, or written through
`aspirations-update-goal.sh` directly) has no sentinel of its own and is reported
as part of the FIRST block. That over-includes at the boundary rather than under-
includes, which is the safe direction for a reader: it shows more, never less.
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# read_goal carries the guard-1251 projection refusal: a six-key projected row
# would report an EMPTY field as a real measurement of empty. Importing it is
# what makes this reader safe, and is its second call site.
try:  # the module name has hyphens, so it cannot be imported by plain name
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location(
        "_gfa", os.path.join(os.path.dirname(os.path.abspath(__file__)), "goal-field-append.py"))
    _gfa = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_gfa)
except Exception as exc:  # noqa: BLE001
    print(json.dumps({"ok": False, "error": f"cannot load goal-field-append.py: {exc}"}), file=sys.stderr)
    sys.exit(3)

SENTINEL_RE = re.compile(r"^\[appended:([^\]\n]+)\]$", re.M)

# The cap this reader reports against is the one aspirations-claim.sh refuses on.
# Kept as a named constant with its provenance so a future reader can see it is a
# MIRROR, not an independent policy: if the claim gate moves, this number is stale
# and the ratio it prints is wrong.
CLAIM_SURFACE_CAP = 62500          # mirrors aspirations-claim.sh CAP ()
SURFACE_FIELDS = ("description", "progress_note", "outcome_note")


def _b(s) -> int:
    return len((s or "").encode("utf-8", "replace"))


def split_blocks(text: str):
    """[(marker|None, block_text)] in append order. See module docstring."""
    blocks, pos = [], 0
    for m in SENTINEL_RE.finditer(text):
        blocks.append((m.group(1), text[pos:m.end()]))
        pos = m.end()
    rest = text[pos:]
    if rest.strip():
        blocks.append((None, rest))
    return blocks


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="goal-note-tail.py", add_help=True)
    ap.add_argument("goal_id")
    ap.add_argument("--source", default="world", choices=("world", "agent"))
    ap.add_argument("--field", default="progress_note")
    ap.add_argument("--blocks", type=int, default=3)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.blocks < 1:
        print("--blocks must be >= 1", file=sys.stderr)
        return 2

    row = _gfa.read_goal(args.goal_id, args.source)      # exits loudly on a projected read
    text = row.get(args.field) or ""
    blocks = split_blocks(text)

    shown = blocks[-args.blocks:] if blocks else []
    omitted = blocks[: len(blocks) - len(shown)]
    shown_bytes = sum(_b(t) for _, t in shown)
    omitted_bytes = sum(_b(t) for _, t in omitted)
    surface = {f: _b(row.get(f)) for f in SURFACE_FIELDS}
    total_surface = sum(surface.values())

    if args.json:
        print(json.dumps({
            "ok": True, "goal_id": args.goal_id, "field": args.field,
            "status": row.get("status"), "recurring": bool(row.get("recurring")),
            "achieved_count": row.get("achievedCount"),
            "field_bytes": _b(text), "blocks_total": len(blocks),
            "blocks_shown": len(shown), "blocks_omitted": len(omitted),
            "bytes_shown": shown_bytes, "bytes_omitted": omitted_bytes,
            "omitted_markers": [m for m, _ in omitted],
            "shown_markers": [m for m, _ in shown],
            "read_surface_bytes": total_surface,
            "read_surface_cap": CLAIM_SURFACE_CAP,
            "read_surface_vs_cap": round(total_surface / float(CLAIM_SURFACE_CAP), 2),
            "tail": "".join(t for _, t in shown),
        }, indent=2))
        return 0

    over = total_surface >= CLAIM_SURFACE_CAP
    print(f"[goal-note-tail] {args.goal_id} .{args.field} — status={row.get('status')}"
          + (f" recurring achievedCount={row.get('achievedCount')}" if row.get("recurring") else ""))
    print(f"    read surface {total_surface} B = {total_surface / float(CLAIM_SURFACE_CAP):.2f}x the "
          f"{CLAIM_SURFACE_CAP} B claim cap" + ("  ** OVER CAP **" if over else ""))
    print(f"    field {_b(text)} B in {len(blocks)} block(s); showing the LAST {len(shown)}")
    if omitted:
        print(f"    OMITTED: {len(omitted)} earlier block(s), {omitted_bytes} B. This is a TAIL, not "
              f"the field. Re-run with --blocks {len(blocks)} to read it whole.")
        print(f"    omitted markers: {', '.join(m or '(unmarked head)' for m, _ in omitted)}")
    else:
        print("    OMITTED: nothing — this IS the whole field.")
    print("=" * 78)
    sys.stdout.write("".join(t for _, t in shown))
    if shown and not "".join(t for _, t in shown).endswith("\n"):
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
