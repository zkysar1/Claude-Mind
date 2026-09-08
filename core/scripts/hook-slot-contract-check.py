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

import re
import sys
from pathlib import Path

# F1 (): every path here was cwd-RELATIVE, so the checker ran only
# from PROJECT_ROOT. From anywhere else it printed "FAIL: <registry> not
# found" and exited 2 — blaming the REGISTRY for what is a cwd fault, which
# is the most misleading direction a contract checker can fail in. Sibling
# scripts derive the root from __file__ or _paths.sh; this does the same.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONV = PROJECT_ROOT / "core/config/conventions/domain-hooks.md"
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
    # F4 (): the header row used to be dropped by matching the literal
    # "Slot name". Renaming that column in the convention turned the header into a
    # phantom slot named "Slot" that then failed "consumer path unparseable" — a
    # checker reporting a defect in the registry's PROSE as a broken contract.
    # The header is simply the first non-separator row, so drop it by position;
    # that holds under any column name. (Same phantom-row class the docstring
    # already describes for the later tables, now solved the same structural way
    # rather than by a second, literal-matching mechanism.)
    body = [r for r in rows if not re.match(r"^\|\s*[-: ]+\|", r)]
    return body[1:]


# Extensions a Consumer-column token may carry to count as a consumer. Derived
# from the live table (11 rows), not from recall: the column also carries
# function names (`do_state_update`), flags (`status=completed`) and OUTPUT
# artifacts (`world/notifications-sent.jsonl`, `outcome-metrics.yaml`), none of
# which are consumers (guard-3581 — an extraction regex that misses a form
# reclassifies the row as a finding, which is the direction that inflated this
# checker's own FAIL count).
CONSUMER_EXTS = (".md", ".sh", ".py")


# F3 (): the existence gate used to accept ONLY `test -f`. The bracket
# forms `[ -f ... ]` and `[[ -f ... ]]` are the more common bash idiom and were
# reported as "references the slot but does NOT existence-gate it" — i.e. the
# checker punished a correct implementation, which is how a checker teaches
# people to ignore it. All three forms satisfy requirement 2 identically.
#
# This is a WIDENING over a live corpus, so per guard-2201 it was landed by
# computing the OLD vs NEW verdict delta on ONE snapshot and asserting the
# REMOVED set is empty — no row that passed the gate before may fail after.
GATE_RE_PREFIX = r"(?:test\s+-f|\[\[?\s+-f)\s+[\"\']?[^\"\'\s]*conventions/"


# F4 (): requirement 2 is written in BASH (`test -f`), but 7 of the 11
# consumers are MARKDOWN — SKILL.md pseudocode and digests — whose "code" is not
# bash and which legitimately express the same absence-handling three other ways.
# Grading pseudocode against a bash syntax rule reported 3 of 3 such rows as
# "breaks every fresh world" when ALL THREE were measured returning rc=0 with the
# slot absent (zeta, cc-02, 2026-09-07):
#   domain-calendar     generation_phase_gate.py -> decision:"fail-open", rc=0
#   commons-retrieval   load-conventions.sh <absent name> -> rc=0, 0 bytes
#   outcome-observation outcome-observation-run.sh on a bare fresh world -> rc=0
# That is a 100% false-positive rate on this bucket, and it is the direction the
# module docstring already names as the most misleading a contract checker can
# fail in — the same lesson F3 landed for the `[ -f ]` bracket forms.
#
# These rows are NOT folded into `ok`. They go to a distinct `delegated` bucket
# that PRINTS ITS EVIDENCE, so relaxing the verdict does not hide the row
# (guard-1914: do not silently drop a guard that decides whether a call happens).
# A consumer with none of the four forms is still `broken` at rc=1 — the
# true-positive path is untouched.
#
# WINDOW sized from MEASURED gaps, not guessed (guard-1451): the largest observed
# distance between a slot reference and its evidence is 5 lines
# (domain-calendar ref@116 -> "Fail-open in both directions"@121); commons-retrieval
# is 0 (same line) and outcome-observation is 3. 12 lines is that max with margin.
DELEGATION_WINDOW = 12

# (B) pseudocode conditional consumption — the SKILL.md idiom for an existence gate
_PSEUDO_GATE_RE = re.compile(
    r"IF\s+path\s+returned|IF\s+output\s+(?:is\s+)?non-empty|IF\s+\w+\s+returned"
    r"|if\s+present|IF\s+EXISTS|if\s+the\s+file\s+exists", re.I)
# (D) an explicitly documented fail-open contract
_FAILOPEN_RE = re.compile(r"fails?[-\s]open", re.I)
# (C) delegation to a repo script — bare name or repo-relative path
_INVOKE_RE = re.compile(r"(?:(?:core|world)/scripts/)?([A-Za-z0-9_.-]+\.(?:sh|py))")


def _script_exists(name: str, project_root) -> bool:
    """A named callee resolves to a real file under core/ or world/ scripts."""
    base = name.rsplit("/", 1)[-1]
    for cand in (project_root / "core" / "scripts" / base,
                 project_root / "world" / "scripts" / base):
        if cand.exists():
            return True
    return False


def delegation_evidence(body: str, slot: str, project_root):
    """Return a one-line evidence string if the consumer handles slot-absence in
    a non-bash form, else None. Only lines within DELEGATION_WINDOW of a slot
    reference count — an unbounded scan would match any fail-open note anywhere
    in a long SKILL.md and make the check unfalsifiable.
    """
    lines = body.splitlines()
    ref_idx = [i for i, ln in enumerate(lines) if slot in ln]
    if not ref_idx:
        return None
    for i in ref_idx:
        lo = max(0, i - DELEGATION_WINDOW)
        hi = min(len(lines), i + DELEGATION_WINDOW + 1)
        window = "\n".join(lines[lo:hi])
        m = _PSEUDO_GATE_RE.search(window)
        if m:
            return "pseudocode existence gate {!r} at line {}".format(
                m.group(0).strip(), i + 1)
        m = _FAILOPEN_RE.search(window)
        if m:
            return "documented fail-open contract {!r} near line {}".format(
                m.group(0).strip(), i + 1)
        for j in range(lo, hi):
            line = lines[j]
            for cand in _INVOKE_RE.findall(line):
                if not _script_exists(cand, project_root):
                    continue
                base = cand.rsplit("/", 1)[-1]
                if slot in line or slot in base:
                    return ("delegates to {} (exists) at line {}, tied to the "
                            "slot by {}".format(
                                cand, j + 1,
                                "the invocation line" if slot in line
                                else "the callee name"))
    return None


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

    broken, ok, unchecked, delegated = [], [], [], []
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
        # F1: consumer paths are registry-relative (repo-root), so resolve them
        # against PROJECT_ROOT rather than the caller's cwd.
        consumer_path = PROJECT_ROOT / consumer
        if not consumer_path.exists():
            broken.append("{}: consumer file {} does not exist — the slot is "
                          "documented but can never fire".format(slot, consumer))
            continue
        # F2 (): an uncaught OSError/UnicodeDecodeError here exited 1,
        # and 1 is this script's "contract broken" verdict — so a permissions or
        # encoding fault on ONE consumer was reported to verify-learning as a
        # hook-slot VIOLATION. A checker crash and a contract failure must never
        # share an exit code. rc=2 is already "the checker itself cannot run".
        try:
            body = consumer_path.read_text(encoding="utf-8")
        except OSError as exc:
            print("FAIL: cannot read consumer {} for slot {} ({}) — this is a "
                  "CHECKER fault, not a contract verdict; no slot verdict below "
                  "can be trusted".format(consumer, slot, exc))
            return 2
        except UnicodeDecodeError as exc:
            print("FAIL: consumer {} for slot {} is not valid UTF-8 ({}) — this "
                  "is a CHECKER fault, not a contract verdict".format(
                      consumer, slot, exc))
            return 2
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
        elif not re.search(GATE_RE_PREFIX + re.escape(slot) + r"\.md", body):
            ev = delegation_evidence(body, slot, PROJECT_ROOT)
            if ev:
                delegated.append("{}: {} has no bash existence gate, but "
                                 "handles absence another way — {}".format(
                                     slot, consumer, ev))
            else:
                broken.append("{}: {} references the slot but does NOT "
                              "existence-gate it (`test -f "
                              "\"$WORLD_DIR/conventions/{}.md\"`) and no "
                              "delegation, pseudocode gate or documented "
                              "fail-open was found within {} lines of any "
                              "reference — works on a configured box, breaks "
                              "every fresh world".format(
                                  slot, consumer, slot, DELEGATION_WINDOW))
        else:
            ok.append(slot)

    # Three verdict classes must partition the table. A row that fell out of
    # all three is a parse defect, and it would otherwise shrink the
    # denominator silently — report it as a checker fault (rc 2), not as a
    # contract verdict (guard-541: assert the buckets sum to the population).
    if len(broken) + len(unchecked) + len(ok) + len(delegated) != len(rows):
        print("FAIL: internal accounting error — {} broken + {} unchecked + {} "
              "ok + {} delegated != {} table rows. The checker dropped a row, "
              "so no verdict below can be trusted.".format(
                  len(broken), len(unchecked), len(ok), len(delegated),
                  len(rows)))
        return 2

    if delegated:
        print("DELEGATED: {} of {} slot(s) handle absence outside a bash "
              "existence gate — evidence named per row; verify by running the "
              "path with the slot absent (guard-6239):".format(
                  len(delegated), len(rows)))
        for d in delegated:
            print("  - {}".format(d))

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
              ("" if not unchecked else
               "; {} executable slot(s) unchecked above".format(len(unchecked)))
              + ("" if not delegated else
                 "; {} delegated slot(s) listed above".format(len(delegated)))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
