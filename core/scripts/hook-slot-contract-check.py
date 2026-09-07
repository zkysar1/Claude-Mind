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

Requirement (2) is a MARKDOWN-slot contract: it presumes the slot's payload is
`world/conventions/<slot>.md`, loaded by the consumer behind an existence gate.
An EXECUTABLE slot's payload is `world/scripts/<slot>.sh` — there is no
`conventions/<slot>.md` for it to gate, so for those rows the gate half is
INAPPLICABLE rather than unmet, and they report as UNCHECKED under their own
tally. Folding a not-applicable verdict into either the pass or the fail class
makes a row that evaluated nothing indistinguishable from one that evaluated
everything (guard-4178). Still asserted for an executable slot, and the whole of
break mode 1 above: the consumer exists and references the slot. NOT asserted:
requirement (3), whose only static implementation is the markdown gate — an
executable slot has no fresh-world shape the convention defines, so this checker
cannot speak to it (guard-2995 — an unstated narrowing reads later as full
coverage).

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


# Extensions a Consumer-column token may carry to count as a consumer. Derived
# from the live table (11 rows), not from recall: the column also carries
# function names (`do_state_update`), flags (`status=completed`) and OUTPUT
# artifacts (`world/notifications-sent.jsonl`, `outcome-metrics.yaml`), none of
# which are consumers (guard-3581 — an extraction regex that misses a form
# reclassifies the row as a finding, which is the direction that inflated this
# checker's own FAIL count).
CONSUMER_EXTS = (".md", ".sh", ".py")


def consumer_tokens(cell: str):
    """Backticked path-shaped tokens from the Consumer column, in table order.

    The column is PROSE, so a bare "first backticked token" is not the
    consumer and "first `.md` anywhere" is not either: where the primary
    consumer is code, the first `.md` is a SECONDARY consumer or the calling
    skill, and grading it silently grades the wrong file.
    """
    return [t for t in re.findall(r"`([^`]+)`", cell)
            if "/" in t and t.endswith(CONSUMER_EXTS)]


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

    broken, ok, unchecked = [], [], []
    for row in rows:
        cells = [c.strip() for c in row.strip("|").split("|")]
        # The slot NAME is the FIRST backticked token, never the whole cell:
        # an executable slot annotates its own name cell — `digest-cost`
        # (**executable slot** — `world/scripts/digest-cost.sh`, not a `.md`)
        # — so .strip("`") drops the leading backtick, keeps the annotation,
        # and prints a mangled slot name in every verdict about that row.
        name_m = re.search(r"`([^`]+)`", cells[0])
        slot = name_m.group(1) if name_m else cells[0].strip("`")
        toks = consumer_tokens(cells[1] if len(cells) > 1 else "")
        if not toks:
            broken.append("{}: consumer path unparseable from the table row "
                          "(expected a backticked `path/to/consumer` ending in "
                          "{} in the Consumer column)".format(
                              slot, "/".join(CONSUMER_EXTS)))
            continue
        # A markdown consumer is graded against the full Pattern B contract;
        # where the row has one, it stays the graded file exactly as before, so
        # this parse retires no true positive. Where it has none, the slot is
        # executable and only the exists+references half is applicable.
        md_consumers = [t for t in toks if t.endswith(".md")]
        consumer = md_consumers[0] if md_consumers else toks[0]
        if not os.path.exists(consumer):
            broken.append("{}: consumer file {} does not exist — the slot is "
                          "documented but can never fire".format(slot, consumer))
            continue
        body = Path(consumer).read_text(encoding="utf-8")
        if slot not in body:
            broken.append("{}: registered in the table but {} never references "
                          "it — a documented hook with no consumer".format(
                              slot, consumer))
        elif not md_consumers:
            unchecked.append("{}: executable slot — {} exists and references "
                             "it, but its payload is `world/scripts/{}.sh`, so "
                             "there is no `conventions/{}.md` to existence-gate "
                             "and requirement 2 does not apply".format(
                                 slot, consumer, slot, slot))
        elif not re.search(r"test\s+-f\s+[\"']?[^\"'\s]*conventions/"
                           + re.escape(slot) + r"\.md", body):
            broken.append("{}: {} references the slot but does NOT "
                          "existence-gate it (`test -f "
                          "\"$WORLD_DIR/conventions/{}.md\"`) — works on a "
                          "configured box, breaks every fresh world".format(
                              slot, consumer, slot))
        else:
            ok.append(slot)

    # Three verdict classes must partition the table. A row that fell out of
    # all three is a parse defect, and it would otherwise shrink the
    # denominator silently — report it as a checker fault (rc 2), not as a
    # contract verdict (guard-541: assert the buckets sum to the population).
    if len(broken) + len(unchecked) + len(ok) != len(rows):
        print("FAIL: internal accounting error — {} broken + {} unchecked + {} "
              "ok != {} table rows. The checker dropped a row, so no verdict "
              "below can be trusted.".format(
                  len(broken), len(unchecked), len(ok), len(rows)))
        return 2

    if unchecked:
        print("UNCHECKED: {} of {} slot(s) are executable — consumer verified, "
              "gate not applicable:".format(len(unchecked), len(rows)))
        for u in unchecked:
            print("  - {}".format(u))

    if broken:
        print("FAIL: Pattern B hook-slot contract broken for {} of {} "
              "slot(s):".format(len(broken), len(rows)))
        for b in broken:
            print("  - {}".format(b))
        print("  See core/config/conventions/domain-hooks.md 'Adding a new "
              "slot' requirements 2 and 3.")
        return 1

    print("PASS: {} of {} Pattern B hook slots have a real consumer that both "
          "references the slot and existence-gates it ({}){}".format(
              len(ok), len(rows), ", ".join(ok),
              "" if not unchecked else
              "; {} executable slot(s) unchecked above".format(len(unchecked))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
