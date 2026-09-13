#!/usr/bin/env python3
"""Checker for the reaction step (.claude/rules/perception-reaction.md, ).

A perception arrives mid-turn as untrusted world data behind a provenance frame.
The rule says a mind must COMPARE it with what it believed, NOTE one decision
line, DECIDE act/fold/ignore, and NEVER copy it into the world as a belief.

This module answers the one question that is mechanically checkable about a
transcript: after a perception frame, is there a noted decision line, and is
there no write to a belief store?

It is deliberately PURE -- text in, verdict out, no I/O outside ``main`` -- so
the fixture test can exercise every branch without a session. It reports on a
transcript; it never enforces anything at write time. A clean verdict means the
transcript SHOWS a reaction, never that the reaction was wise.

POSITION IS THE WHOLE POINT. A belief-store write BEFORE the frame is ordinary
unrelated work; the same write AFTER it is the rule-5 violation. A checker that
merely counted occurrences would fail every transcript that encoded anything at
all, which is the shape of a detector nobody can keep switched on.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

#: The literal producer frame: ``_OBSERVATION_FRAME`` in zak-code
#: ``src/zakcode/agent/loop.py``. The em dash is what actually ships; the
#: hyphen variant is tolerated so a transcript that passed through an
#: ASCII-folding pipe still matches (a missed frame reads as "no perception
#: here", i.e. silently clean -- the wrong direction to fail in).
FRAME_RE = re.compile(r"\[perception\s*[—–-]", re.IGNORECASE)

#: The decision line the rule asks for. `unit`, `decision` and `reason` are
#: required; `changed` is required too, because a decision with no stated delta
#: is exactly the uncompared reaction rule 2 forbids.
DECISION_RE = re.compile(
    r"perception-reaction:\s*"
    r"(?=[^\n]*\bunit=(?P<unit>[^\s\n]+))"
    r"(?=[^\n]*\bchanged=(?P<changed>[^\n]*?)(?:\s+\w+=|\s*$))"
    r"(?=[^\n]*\bdecision=(?P<decision>act|fold|ignore)\b)"
    r"(?=[^\n]*\breason=(?P<reason>[^\n]*?)\s*$)"
    r"[^\n]*",
    # MULTILINE is load-bearing, not stylistic: `reason=...$` without it anchors
    # to end-of-STRING, so a decision line matched only when it was the final
    # line of the transcript. In a real transcript it never is -- the reaction is
    # followed by the work it decided on -- so the checker reported `fail` on
    # essentially every CORRECT reaction. Caught by
    # test_working_memory_and_journal_writes_are_not_belief_writes, whose writer
    # line simply came after the decision.
    re.IGNORECASE | re.MULTILINE,
)

VALID_DECISIONS = ("act", "fold", "ignore")

#: Writes that put something into a SHARED BELIEF store. Guardrail, tree and
#: reasoning-bank writers only -- working memory and the journal are where the
#: rule tells you to note the decision, so they must never appear here.
BELIEF_WRITE_RE = re.compile(
    r"(?:^|[\s;&|`(])"
    r"(?:bash\s+\S*|py(?:thon3?)?\s+-?3?\s*\S*)?"
    r"(?:core/scripts/)?"
    r"(tree-add\.sh|tree-update\.sh|tree-set\.sh|tree-decompose\.sh"
    r"|reasoning-bank-add\.sh|guardrails-add\.sh)",
    re.IGNORECASE,
)


def find_frames(text: str) -> list[int]:
    """Start offsets of every perception frame, in order."""
    return [m.start() for m in FRAME_RE.finditer(text or "")]


def find_decisions(text: str) -> list[dict]:
    """Every well-formed decision line, with its offset and parsed fields."""
    out = []
    for m in DECISION_RE.finditer(text or ""):
        out.append({
            "offset": m.start(),
            "unit": (m.group("unit") or "").strip(),
            "changed": (m.group("changed") or "").strip(),
            "decision": (m.group("decision") or "").strip().lower(),
            "reason": (m.group("reason") or "").strip(),
        })
    return out


def find_belief_writes(text: str) -> list[dict]:
    """Every shared-belief-store write, with its offset and the writer named."""
    return [{"offset": m.start(), "writer": m.group(1)}
            for m in BELIEF_WRITE_RE.finditer(text or "")]


def analyze(text: str) -> dict:
    """Verdict for one transcript.

    ``pass`` requires a frame, a well-formed decision line AFTER it, and no
    belief-store write after it. ``no-perception`` is NOT a pass and is not a
    failure either: there was nothing to react to, and reporting that as clean
    would let an empty transcript stand in for a good one (guard-1760).
    """
    frames = find_frames(text)
    decisions = find_decisions(text)
    writes = find_belief_writes(text)
    if not frames:
        return {"verdict": "no-perception", "frames": 0, "decisions": [],
                "belief_writes_after": [], "reason":
                "no perception frame in this transcript -- nothing to react to; "
                "this is not a pass"}
    first = frames[0]
    after_dec = [d for d in decisions if d["offset"] > first]
    after_wr = [w for w in writes if w["offset"] > first]
    problems = []
    if not after_dec:
        problems.append(
            "no perception-reaction decision line after the frame (rule 3): a "
            "perception that produced no noted decision is indistinguishable "
            "from one never read")
    if after_wr:
        problems.append(
            "belief-store write(s) after the frame (rule 5): "
            + ", ".join(sorted({w["writer"] for w in after_wr}))
            + " -- a perception is an observation, never a belief")
    return {
        "verdict": "fail" if problems else "pass",
        "frames": len(frames),
        "decisions": after_dec,
        "belief_writes_after": after_wr,
        "reason": "; ".join(problems) if problems
                  else "frame present, decision noted, no belief-store write after it",
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Check a transcript's reaction to a perception.")
    ap.add_argument("path", nargs="?", help="transcript file (default: stdin)")
    ap.add_argument("--output", choices=("json", "text"), default="json")
    a = ap.parse_args(argv)
    text = open(a.path, encoding="utf-8", errors="replace").read() if a.path else sys.stdin.read()
    r = analyze(text)
    if a.output == "json":
        print(json.dumps(r, indent=2, ensure_ascii=False))
    else:
        print(f"{r['verdict']}: {r['reason']}")
    return 0 if r["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
