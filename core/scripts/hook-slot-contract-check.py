#!/usr/bin/env python3
"""hook-slot-contract-check.py — assert the Pattern B hook-slot contract.

core/config/conventions/domain-hooks.md documents three requirements for every
canonical hook slot, and until g-115-3177 nothing verified any of them:

  (1) the slot is registered in the "Canonical Hook Slots" table with a
      consumer path;
  (2) the call site uses the canonical shape — an existence-gated
      `test -f "$WORLD_DIR/conventions/<slot>.md"`;
  (3) it fails open in fresh worlds, so a missing convention is a silent no-op.

Two silent break modes follow from that gap, and neither surfaces on a
configured box:

  * a slot registered in the table with NO consumer anywhere — a documented
    hook that never fires. The table reads as coverage; nothing runs.
  * a consumer that reads the convention WITHOUT the existence gate — works
    here, breaks every FRESH world. Fresh-world breakage is precisely what
    nobody in a long-lived deployment ever notices, because requirement (3)
    is only exercised where the file is absent.

Requirement (3) is not separately checkable by static means; the existence
gate in (2) IS its implementation, so gating implies fail-open. This script
therefore asserts (1) and (2), which together cover it.

Why static assertion rather than an integration test: hook dispatch is
LLM-executed pseudocode, not code. There is no runtime seam to drive, so a
contract assertion over the table and its call sites is the closest
automatable coverage of the trigger -> handler path (g-335-211, sq-019).

Exit 0 = contract holds (prints PASS). Exit 1 = broken (prints FAIL naming
each slot and WHICH half is missing). Exit 2 = the table itself is
unreadable, which is its own kind of failure.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

CONV = Path("core/config/conventions/domain-hooks.md")
HEADING = "## Canonical Hook Slots"


def parse_slots(text: str):
    """Rows of the FIRST markdown table after the heading.

    Bounded at the first blank line after the table starts: domain-hooks.md
    carries several later tables (Mutation Sources, seed files) whose first
    column is not a slot name, and an unbounded scan silently pulls them in
    as phantom slots that then fail the gate check (observed while authoring
    this: 14 'slots' parsed instead of 5, three of them false FAILs).
    """
    if HEADING not in text:
        return None
    rows, started = [], False
    for line in text.split(HEADING, 1)[1].splitlines():
        if line.startswith("|"):
            started, _ = True, rows.append(line)
        elif started and not line.strip():
            break
    return [r for r in rows
            if not re.match(r"^\|\s*[-: ]+\|", r) and "Slot name" not in r]


def main() -> int:
    if not CONV.exists():
        print("FAIL: {} not found — the hook-slot registry is the contract's "
              "single source of truth".format(CONV))
        return 2

    rows = parse_slots(CONV.read_text(encoding="utf-8"))
    if rows is None:
        print('FAIL: "{}" heading missing from {} — cannot locate the slot '
              "table".format(HEADING, CONV))
        return 2
    if not rows:
        print("FAIL: the Canonical Hook Slots table is empty — either every "
              "slot was retired (say so in the convention) or the table shape "
              "changed and this checker no longer parses it")
        return 2

    broken, ok = [], []
    for row in rows:
        cells = [c.strip() for c in row.strip("|").split("|")]
        slot = cells[0].strip("`")
        m = re.search(r"`([^`]+\.md)`", cells[1] if len(cells) > 1 else "")
        if not m:
            broken.append("{}: consumer path unparseable from the table row "
                          "(expected a `path/to/file.md` in the Consumer "
                          "column)".format(slot))
            continue
        consumer = m.group(1)
        if not os.path.exists(consumer):
            broken.append("{}: consumer file {} does not exist — the slot is "
                          "documented but can never fire".format(slot, consumer))
            continue
        body = Path(consumer).read_text(encoding="utf-8")
        if slot not in body:
            broken.append("{}: registered in the table but {} never references "
                          "it — a documented hook with no consumer".format(
                              slot, consumer))
        elif not re.search(r"test\s+-f\s+[\"']?[^\"'\s]*conventions/"
                           + re.escape(slot) + r"\.md", body):
            broken.append("{}: {} references the slot but does NOT "
                          "existence-gate it (`test -f "
                          "\"$WORLD_DIR/conventions/{}.md\"`) — works on a "
                          "configured box, breaks every fresh world".format(
                              slot, consumer, slot))
        else:
            ok.append(slot)

    if broken:
        print("FAIL: Pattern B hook-slot contract broken for {} of {} "
              "slot(s):".format(len(broken), len(rows)))
        for b in broken:
            print("  - {}".format(b))
        print("  See core/config/conventions/domain-hooks.md 'Adding a new "
              "slot' requirements 2 and 3.")
        return 1

    print("PASS: all {} Pattern B hook slots have a real consumer that both "
          "references the slot and existence-gates it ({})".format(
              len(ok), ", ".join(ok)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
