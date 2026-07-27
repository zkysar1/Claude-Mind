#!/usr/bin/env python3
"""Finding-disproof gate — force a DISCONFIRMING probe before a finding leaves the box.

THE GAP THIS CLOSES (g-115-2184, 2026-07-14):
  Three reasoning-bank entries (rb-3408, rb-3410, rb-3419) and a guardrail
  (guard-1065) already say "verifying the MECHANISM is not verifying the CASE."
  On 2026-07-14 zeta violated it FOUR times in one day — the fourth being a
  blocker-category email to the USER declaring a fleet-wide data-integrity
  emergency ("the storage layer has no baseline writer; the manifest is
  permanently empty on every box"). The claim was false. The disproof was one
  ten-second command away, INSIDE the very log lines being cited as
  corroboration: owncloud_sync.py:608 only prints "both changed since baseline"
  when `baseline_md5 is not None` — i.e. the cited evidence PROVED baselines
  exist. It shipped anyway.

  The rules did not fail because they were absent. They failed because every
  probe the agent ran was CONFIRMATORY. Nine commands, all asking "is my story
  true?", none asking "what would make it false?". More rules of the same shape
  cannot fix that; the missing step is structurally different.

THE ONE QUESTION THIS GATE ASKS:
  "Name the single command that would DISPROVE this claim. Show that you ran it."

  If you cannot name one, the claim is not falsifiable and must not ship.
  If you can name one but did not run it, run it first.
  That is the whole gate. It is not "verify more" — it is "verify AGAINST
  yourself, once, deliberately."

SCOPE — fires only on EXTERNALIZING claims that are UNIVERSAL or CAUSAL:
  A finding that stays in your own head is cheap to be wrong about. A finding
  that reaches the user, a partner, or the goal queue is expensive. And the
  claims that go most wrong are the ones with the widest quantifiers ("every
  box", "permanently", "nothing anywhere") or the strongest causal reach ("the
  root cause of", "explains all four"). Narrow, local, hedged claims pass
  untouched — this gate must not tax ordinary reporting.

Fail-OPEN on any internal error (a gate bug must never block a real alert), but
fail-CLOSED on a matched claim with no disproof evidence — that is the point.

Exit codes:
  0  no universal/causal markers -> nothing to gate, OR disproof evidence supplied
  1  REFUSED — matched claim, no (or empty) disproof probe/result
  2  input error (fail-open: caller should proceed)

Usage:
  py -3 core/scripts/finding-disproof-gate.py \
      --claim-file <path>            (or --claim "<text>")
      --disproof-probe  "<the exact command that would FALSIFY this claim>"
      --disproof-result "<what that command actually printed>"
      [--json]
"""
import argparse
import json
import re
import sys

# Widest-quantifier and strongest-causal-reach markers. These are the shapes that
# went wrong — not "X is broken" (narrow, checkable) but "X is broken EVERYWHERE"
# and "X is THE CAUSE of everything". Deliberately NOT a general alarm-word list:
# gating ordinary urgent reporting would train the agent to route around this.
UNIVERSAL = [
    r"\bfleet[- ]wide\b", r"\bevery box\b", r"\ball (?:boxes|agents|machines)\b",
    r"\bpermanently\b", r"\bsteady state\b", r"\b100% of\b", r"\bnever had\b",
    r"\bdoes not exist anywhere\b", r"\bnowhere\b", r"\bno .{0,24} (?:exists|writer) anywhere\b",
    r"\balways\b.{0,30}\b(?:takes|sees|hits)\b", r"\bon every (?:write|read|call|box)\b",
]
CAUSAL = [
    r"\broot cause (?:of|is|behind)\b", r"\bexplains (?:all|both|four|three|every)\b",
    r"\bthe only thing\b", r"\bis what causes\b", r"\bsingle root cause\b",
    r"\bthis explains\b", r"\baccounts for (?:all|every)\b",
]


# A RETRACTION necessarily QUOTES the universal/causal claim it is withdrawing
# ("'the manifest is permanently empty fleet-wide' — FALSE"). Gating it would be
# exactly backwards: this gate exists to force disconfirmation, and a retraction
# IS the disconfirmation. It is also the single most important message an agent
# can send — never make it harder to withdraw a false claim than to make one.
# (Found by fresh-eyes-code on this gate's own first review, .)
RETRACTION = [
    r"\bretraction\b", r"\bi retract\b", r"\bretracting\b", r"\bretracted\b",
    r"\bwas (?:wrong|false)\b", r"\bis (?:wrong|false)\b.{0,40}\b(?:claim|finding)\b",
    r"\bcorrection\b.{0,30}\bprevious\b", r"\bdisregard\b.{0,30}\b(?:previous|earlier|last)\b",
]


def is_retraction(text):
    t = text.lower()
    return any(re.search(p, t) for p in RETRACTION)


def scan(text):
    t = text.lower()
    hits = []
    for pat in UNIVERSAL:
        m = re.search(pat, t)
        if m:
            hits.append(("universal", m.group(0).strip()))
    for pat in CAUSAL:
        m = re.search(pat, t)
        if m:
            hits.append(("causal", m.group(0).strip()))
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--claim")
    ap.add_argument("--claim-file")
    ap.add_argument("--disproof-probe", default="")
    ap.add_argument("--disproof-result", default="")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    try:
        if a.claim_file:
            with open(a.claim_file, encoding="utf-8", errors="replace") as fh:
                claim = fh.read()
        elif a.claim:
            claim = a.claim
        else:
            print("finding-disproof-gate: no --claim/--claim-file (fail-open)", file=sys.stderr)
            return 2

        # Retractions pass unconditionally — see RETRACTION above. Checked BEFORE
        # the marker scan so a withdrawal is never harder to send than the claim.
        if is_retraction(claim):
            if a.json:
                print(json.dumps({"gated": False, "reason": "retraction — exempt by design"}))
            print("[finding-disproof-gate] PASS — retraction (a withdrawal IS the "
                  "disconfirmation; never gate it).", file=sys.stderr)
            return 0

        hits = scan(claim)
        if not hits:
            if a.json:
                print(json.dumps({"gated": False, "reason": "no universal/causal markers"}))
            return 0

        probe = (a.disproof_probe or "").strip()
        result = (a.disproof_result or "").strip()
        # A probe with no result is a plan, not evidence. Both are required.
        if probe and result:
            if a.json:
                print(json.dumps({"gated": True, "passed": True, "markers": hits,
                                  "disproof_probe": probe}))
            print("[finding-disproof-gate] PASS — disproof probe named and run:\n"
                  "    probe : {p}\n    result: {r}".format(p=probe, r=result[:200]),
                  file=sys.stderr)
            return 0

        marker_txt = ", ".join("{k}:'{v}'".format(k=k, v=v) for k, v in hits[:5])
        msg = (
            "\n[finding-disproof-gate] REFUSED — this finding makes a UNIVERSAL or CAUSAL claim "
            "and carries no disconfirming evidence.\n"
            "  markers: {m}\n\n"
            "  Answer ONE question before this ships:\n"
            "      What single command would prove this claim FALSE?\n\n"
            "  Then run THAT command — not another one that would confirm you — and pass:\n"
            "      --disproof-probe  \"<the command>\"\n"
            "      --disproof-result \"<what it actually printed>\"\n\n"
            "  If you cannot name a command that could falsify the claim, the claim is not\n"
            "  falsifiable and must not leave this box.\n\n"
            "  WHY (g-115-2184, 2026-07-14): a blocker email told the user the fleet's storage\n"
            "  layer had no baseline writer and the manifest was empty on every box. It was\n"
            "  false. The disproof was ten seconds away and was sitting INSIDE the log lines\n"
            "  being quoted as proof. Nine probes had been run; every one asked 'is my story\n"
            "  true?' and not one asked 'what would make it false?'. guard-1065 / rb-3408 /\n"
            "  rb-3410 / rb-3419 all already said 'mechanism is not case' — and did not stop it.\n"
            "  This gate asks the one question those rules could not force.\n"
        ).format(m=marker_txt)
        print(msg, file=sys.stderr)
        if a.json:
            print(json.dumps({"gated": True, "passed": False, "markers": hits,
                              "reason": "universal/causal claim without disproof evidence"}))
        return 1
    except Exception as exc:  # noqa: BLE001 — fail-open; a gate bug must not block a real alert
        print("finding-disproof-gate: internal error, failing open: {e!r}".format(e=exc),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
