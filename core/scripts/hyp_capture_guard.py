#!/usr/bin/env python3
""" — the no-double-resolution guard for worker-supplied hypothesis evidence.

WHAT THIS IS FOR. A worker Body writes evidence about an EXISTING hypothesis into
the hyp_capture WM slot (worker-loop Phase 3.65) and never resolves it; the reducer
runs the full resolution protocol (/review-hypotheses) after body-merge delivers
that evidence. The failure this guards is the reducer resolving a hypothesis
INDEPENDENTLY while unread worker evidence for it sits in the merged WM — a
resolution that is not wrong-looking in any way, just under-informed. Nothing else
in the loop would notice: the hypothesis moves to resolved, the evidence stays in
the slot until it ages out, and every signal reports success.

WHY IT IS A SEPARATE MODULE rather than pseudocode inside the skill. The goal
requires the guard to HAVE A TEST, and SKILL.md prose cannot be tested. Keeping the
decision here as pure functions means the reducer's call site stays one line and the
behaviour is pinned by core/scripts/tests/test_hyp_capture_guard.py.

THE MODULE DECIDES NOTHING ABOUT RESOLUTION. It answers two questions and returns
them as data: "is there worker evidence for this hypothesis?" and "does this
evidence name a hypothesis that actually exists?". The reducer still runs the full
protocol and may resolve against the worker's read — `suggested_resolution` is
explicitly non-binding. A guard that BLOCKED resolution would replace an
under-informed resolution with a stuck one, which is worse: evidence can be read and
weighed, but a hypothesis nobody may resolve never closes.
"""
from __future__ import annotations

import json
from typing import Iterable


def _entries(raw) -> list:
    """Coerce a hyp_capture slot read into a list of dict entries.

    Deliberately tolerant in ONE direction only: a null/absent slot and a
    non-list payload both yield [] (no evidence), because the safe default for
    "I could not read the evidence" is to let the reducer proceed normally rather
    than to claim evidence exists. Non-dict members are dropped rather than
    coerced — an entry we cannot key by hypothesis_id is not evidence about any
    hypothesis, and guessing would be worse than ignoring.
    """
    if not isinstance(raw, list):
        return []
    return [e for e in raw if isinstance(e, dict)]


def evidence_for(raw, hypothesis_id: str) -> list:
    """Every worker-supplied entry naming this hypothesis, in slot order.

    Exact string match on hypothesis_id, never a prefix or substring: pipeline ids
    are YYYY-MM-DD_slug, so a substring match would let `2026-08-10_retry` collect
    evidence filed against `2026-08-10_retry-backoff`, silently attributing one
    hypothesis's evidence to another.
    """
    if not hypothesis_id:
        return []
    return [e for e in _entries(raw) if e.get("hypothesis_id") == hypothesis_id]


def parse_slot(slot_json) -> tuple:
    """Return (payload, slot_read) where slot_read is 'ok' | 'empty' | 'unreadable'.

    An unreadable slot and an empty slot both yield NO evidence, but they are
    different facts and must not render identically (guard-2352): "there is no
    worker evidence for this hypothesis" is a finding, while "I could not look"
    is a defect report. Collapsing them is what let this guard sit inert at its
    only call site — the two-Bash-call shape fed it an empty string, and
    `has_evidence: false` looked exactly like a clean answer.

    The sibling exp_capture_drain draws the same line (verdict `empty` vs
    `read-failed`) and its test pins it; this is that pin's counterpart here.
    """
    if slot_json is None or str(slot_json).strip() == "":
        return None, "unreadable"
    try:
        payload = json.loads(slot_json)
    except (ValueError, TypeError):
        return None, "unreadable"
    if payload is None:
        # wm-read.sh renders an absent slot as the literal `null` — that is a
        # successful read of a slot with nothing in it, not a failed read.
        return None, "empty"
    if not isinstance(payload, list):
        # A non-list payload means the slot is not the array we registered it
        # as. Report it as unreadable rather than empty: silently treating a
        # schema violation as "no evidence" is the same laundering again.
        return payload, "unreadable"
    return payload, ("ok" if payload else "empty")


def guard_verdict(raw, hypothesis_id: str) -> dict:
    """The reducer's pre-resolution question, answered as data.

    `has_evidence` is the guard proper. `goal_ids` is included because the reducer
    usually wants to read the originating goal's outcome_note alongside the
    evidence summary, and recovering it otherwise means re-scanning the slot.
    """
    matches = evidence_for(raw, hypothesis_id)
    return {
        "hypothesis_id": hypothesis_id,
        "has_evidence": bool(matches),
        "count": len(matches),
        "goal_ids": [e.get("goal_id") for e in matches if e.get("goal_id")],
        "entries": matches,
    }


def unresolvable_ids(raw, known_hypothesis_ids: Iterable[str]) -> list:
    """hyp_capture entries whose hypothesis_id matches no pipeline record.

    The goal states the linkage requirement ("hyp_capture hypothesis_id must link
    to a real pipeline.jsonl record"); this is the detection half. Reported, never
    auto-deleted: an unmatched id is more likely a hypothesis that was archived or
    renamed after the worker filed than a fabricated one, and discarding real
    evidence to tidy a slot is the wrong direction.

    An EMPTY known-id set returns [] rather than flagging everything. A zero-length
    corpus is the signature of a failed pipeline read, and treating that as "every
    entry is bogus" would turn one unreadable file into a fleet-wide false alarm
    (rb-245 class: a zero-count audit against an unread corpus looks authoritative).
    """
    known = {k for k in (known_hypothesis_ids or []) if k}
    if not known:
        return []
    seen, out = set(), []
    for e in _entries(raw):
        hid = e.get("hypothesis_id")
        if hid and hid not in known and hid not in seen:
            seen.add(hid)
            out.append(hid)
    return out


def main(argv=None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        description="Check for worker-supplied hyp_capture evidence before resolving.")
    p.add_argument("--hypothesis-id", required=True)
    p.add_argument("--slot-json", required=True,
                   help="hyp_capture slot contents as JSON. Supplied by the "
                        "wrapper core/scripts/hyp-capture-guard.sh, which reads "
                        "the slot in the SAME shell. Do not hand-assemble this "
                        "across two Bash calls — the value does not survive "
                        "(guard-128/guard-492); that is the defect the wrapper "
                        "exists to prevent.")
    p.add_argument("--known-ids-json", default=None,
                   help="optional JSON array of pipeline hypothesis ids, for the linkage check")
    a = p.parse_args(argv)

    raw, slot_read = parse_slot(a.slot_json)
    verdict = guard_verdict(raw, a.hypothesis_id)
    verdict["slot_read"] = slot_read
    if a.known_ids_json:
        try:
            known = json.loads(a.known_ids_json)
        except (ValueError, TypeError):
            known = []
        verdict["unresolvable_ids"] = unresolvable_ids(raw, known)
    print(json.dumps(verdict, indent=2))
    # Always 0: this is an ADVISORY read, not a gate. A non-zero exit would invite
    # a caller to treat "evidence exists" as "do not resolve", which is the stuck
    # state the module docstring rejects.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
