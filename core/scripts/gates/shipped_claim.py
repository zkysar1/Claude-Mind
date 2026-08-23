"""Shipped-claim / store-content mismatch logic.

Detects the class where a goal closes `completed` with an outcome_note that
CLAIMS a named symbol was shipped into a store-backed artifact, while the
STORE's copy of that artifact does not contain the symbol at all.

Canonical incident (g-326-585, 2026-08-22): the goal closed `completed` with
the outcome_note

    "DELIVERABLE (--direct mode) shipped + mock-verified. Added to
     zakpod1-pp-aging-probe.py: url capture in read_registry, a
     probe_direct() posting straight to each engine loopback, ... a --direct
     branch ... and a fail-loud off-pod pre-flight."

The store's `world/scripts/zakpod1-pp-aging-probe.py` (24,976 B, byte-identical
to the local mirror) contains `probe_direct` 0 times and `--direct` 0 times;
its own docstring argues the opposite design ("Direct-port is unreachable
off-pod, full stop"). The downstream acceptance goal g-326-586 was filed
against a mode that does not exist.

WHY THIS IS NOT THE EXISTING GATE'S JOB. `gates.completion_artifact` already
runs at `cmd_update_goal(status=completed)`, and it passes this incident
cleanly on all three of its axes:

  1. it scans title + description, never `outcome_note` — and the claim
     lives only in the note;
  2. it tests `Path.exists()`, never CONTENT — and the file exists;
  3. its `ARTIFACT_PATH_RE` roots do not include `world/scripts`, and the
     note names the file by BASENAME anyway, with no path at all.

MECHANISM-INDEPENDENCE IS A REQUIREMENT, NOT A STYLE CHOICE. Several distinct
causes produce this same end state — a silent object-store no-op, a
pull-overwrite, an isolated forked-Body tree, or a claim that was simply never
true. None of them is measured, so the invariant tested here is deliberately
about the END STATE ONLY: "the note says symbol S went into artifact A; the
store's A does not contain S." That predicate holds regardless of which cause
produced it, and stays valid if every current cause hypothesis is falsified.

PRECISION OVER RECALL, BY DESIGN. Only three high-precision symbol forms are
recognised — long flags (`--direct`), call forms (`probe_direct()`), and
backticked identifiers. Bare prose identifiers (`read_registry` in the
incident above) are NOT extracted: catching them needs a bare-word scan whose
false-positive rate on natural closing prose is unacceptable for something
that runs on every close. Missing a symbol costs one un-flagged claim; a
false positive costs trust in the whole detector, and detectors nobody trusts
get ignored rather than fixed. The incident is caught twice over on the two
forms that ARE extracted.

KNOWN LIMITATION — QUOTATION READS AS CLAIM. The shipped-verb test is
note-GLOBAL, so a note that QUOTES someone else's false claim ("g-326-585
said it added --direct to zakpod1-pp-aging-probe.py; it did not") fires the
same as a note that MAKES it. The detector's output is still literally true
in that case — the note does name a symbol that is absent from the artifact —
but the defect belongs to the quoted goal, not the quoting one. Narrowing the
verb test to the artifact's own clause was rejected: quoted claims carry the
verb next to the filename too, so it would cost recall on real claims without
buying precision here. Read a fire as "some goal's claim about this artifact
is false", then check WHICH goal. This is a report, never a block, precisely
because of cases like this.

Public API (PURE — no I/O, no subprocess, no env reads):
    extract_claims(outcome_note) -> [{"artifact": str, "symbols": [str, ...]}]
    missing_symbols(symbols, content) -> [str, ...]
    evaluate(goal_id, outcome_note, content_by_artifact) -> dict

Output dict shape (evaluate):
    {
      "fired": bool,
      "goal_id": str,
      "mismatches": [{"artifact": str, "missing": [str, ...],
                      "present": [str, ...], "content_bytes": int}, ...],
      "claims_checked": int,
      "reason": str | None,      # set when not fired
    }

Daemon safety:
    - Reads no environment variables. All inputs passed in.
    - No subprocess calls, no filesystem access. Pure text analysis.
      The store read lives in the CLI (`shipped-claim-store-check.py`).
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

# A note only makes a SHIPPED CLAIM when it says something was put into a
# file. Without one of these verbs the note is a report or a diagnosis, and
# naming a file plus a flag in it ("re-ran probe.py --json") is not a claim
# about the file's contents.
#
# BASE AND PROGRESSIVE FORMS ARE INCLUDED, and that was not the first draft.
# The original list carried only past/third-person forms (added|adds|...), and
# it was measured MISSING two live specimens from this gate's own incident
# family on the day it landed: 's note ("check_serving_shape() calls
# the divergence probe ... in zakpod1-recycle-engines.sh") uses no listed verb
# at all, and rb-8895 says "add a --direct MODE ... add a probe_direct()" —
# the bare infinitive. The second is a TRUE POSITIVE the narrow list dropped.
#
# Widening measured over the LIVE corpus before adopting it (guard-2499
# discipline, applied in the widening direction): 628 completed goals,
# 540 carrying a non-empty outcome_note, 2,349,642 note bytes. Candidate
# claims — notes matching verb AND artifact-token AND symbol —
# went 264 -> 280, a delta of 16 (+6.1%), and  is IN the delta.
# So the widening is not speculative reach: it admits the sibling case that
# motivated it, at a bounded cost on a fully-enumerated population.
#
# The base form does admit a PLAN ("we should add --foo to bar.py") written
# into a completed goal's closing note. That is deliberately not filtered
# out: a goal closed `completed` whose note describes an unbuilt symbol is
# the same defect class this detector exists for, so the "false" positive is
# the true one.
SHIPPED_VERB_RE = re.compile(
    r"\b(?:add|adds|added|adding|ship|ships|shipped|shipping|"
    r"write|writes|wrote|written|writing|"
    r"implement|implements|implemented|land|lands|landed|landing|"
    r"create|creates|created|creating|introduce|introduces|introduced|"
    r"wire|wires|wired|wiring|extend|extends|extended|"
    r"append|appends|appended)\b",
    re.IGNORECASE,
)

# Artifact filenames. Basename-or-path form, because closing prose routinely
# names the file without its directory (the incident note does exactly that).
ARTIFACT_TOKEN_RE = re.compile(
    r"(?<![\w/.-])((?:[\w.-]+/)*[A-Za-z0-9_][\w.-]*"
    r"\.(?:py|sh|lua|java|js|mjs|ts|md|ya?ml|jsonl|json))(?![\w])"
)

# Three high-precision shipped-symbol forms. See the module docstring for why
# bare prose identifiers are deliberately excluded.
#   --long-flag        a CLI surface the note says now exists
#   name()             a function the note says was added
#   `identifier`       a symbol the author marked as code
SYMBOL_RES = (
    re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]{2,})(?![\w-])"),
    re.compile(r"(?<![\w.])([a-z_][a-z0-9_]{3,})\(\)"),
    re.compile(r"`([A-Za-z_][\w.-]{3,})`"),
)


def _symbol_needle(symbol: str) -> str:
    """The literal that must appear in the artifact for the claim to hold.

    A call form `probe_direct()` is claimed present when the bare name
    `probe_direct` occurs (as a `def`, a call, or a reference) — requiring the
    literal `()` would miss `def probe_direct(pod, port)` and turn every real
    function into a false positive. A `--flag` is matched verbatim: argparse
    declares it as the literal string, so a verbatim match is both correct and
    the strictest available.
    """
    return symbol[:-2] if symbol.endswith("()") else symbol


def extract_claims(outcome_note: str) -> List[Dict[str, object]]:
    """Parse shipped-symbol claims out of a closing note.

    Symbols bind to the NEAREST PRECEDING artifact token, falling back to the
    first artifact in the note when a symbol appears before any filename. That
    rule reads a multi-file note correctly ("Added a() to x.py and --b to
    y.sh") and reads the far more common single-file note correctly by
    construction, without needing sentence segmentation — prose sentence
    boundaries in these notes are unreliable (semicolons, pipes from flattened
    newlines, bare dashes).

    Returns [] when the note makes no shipped claim, names no artifact, or
    names no extractable symbol. Never raises.
    """
    text = outcome_note or ""
    if not text.strip():
        return []
    if not SHIPPED_VERB_RE.search(text):
        return []

    artifacts = [(m.start(), m.group(1)) for m in ARTIFACT_TOKEN_RE.finditer(text)]
    if not artifacts:
        return []

    # Preserve first-appearance order of artifacts; dedupe symbols per artifact.
    bucket: Dict[str, List[str]] = {}
    order: List[str] = []
    for _, name in artifacts:
        if name not in bucket:
            bucket[name] = []
            order.append(name)

    for rx in SYMBOL_RES:
        for m in rx.finditer(text):
            symbol = m.group(1)
            if rx.pattern.endswith(r"\(\)"):
                symbol += "()"
            pos = m.start()
            # Nearest preceding artifact; else the first artifact in the note.
            owner = artifacts[0][1]
            for a_pos, a_name in artifacts:
                if a_pos < pos:
                    owner = a_name
                else:
                    break
            # A filename is not a symbol claim about itself.
            if _symbol_needle(symbol) == owner:
                continue
            if symbol not in bucket[owner]:
                bucket[owner].append(symbol)

    return [{"artifact": name, "symbols": bucket[name]}
            for name in order if bucket[name]]


def missing_symbols(symbols: List[str], content: str) -> List[str]:
    """Return the claimed symbols that occur ZERO times in `content`.

    Zero is the only threshold used. A symbol present even once means the
    claim is at least partly grounded, and adjudicating "present but not in
    the claimed role" is a judgment this detector deliberately does not make —
    it would trade a precise signal for an arguable one.
    """
    out: List[str] = []
    for sym in symbols:
        if content.count(_symbol_needle(sym)) == 0:
            out.append(sym)
    return out


def evaluate(*, goal_id: str, outcome_note: str,
             content_by_artifact: Dict[str, Optional[str]]) -> dict:
    """Compare a note's shipped claims against store content.

    Args:
        goal_id: goal being closed (audit correlation).
        outcome_note: the goal record's current outcome_note.
        content_by_artifact: artifact-token -> STORE content, keyed exactly as
            `extract_claims` returned it. A value of None means the caller
            could not resolve or read that artifact from the store; such
            artifacts are SKIPPED, never reported as mismatches — an
            unreadable store is a fact about the reader, not about the claim
            (verify-before-assuming: a failed read is zero signals, not one).

    Returns the documented output dict. Never raises.
    """
    claims = extract_claims(outcome_note)
    if not claims:
        return {"fired": False, "goal_id": goal_id, "mismatches": [],
                "claims_checked": 0,
                "reason": "no shipped-symbol claim in outcome_note"}

    # Cross-artifact acquittal. `extract_claims` binds each symbol to the
    # nearest PRECEDING filename, which reads "Added to x.py: a(), --b"
    # correctly and mis-reads the equally common "wrote a() and --b into x.py"
    # — the symbols precede the file they went into, so they bind to whatever
    # filename came earlier. On a single-artifact note (the common case, and
    # the  case) the binding is irrelevant because there is only one
    # target. On a MULTI-artifact note a mis-bind manufactures a false
    # positive, which is the one failure this detector cannot afford.
    #
    # So: a symbol is only reported missing when it is absent from EVERY
    # readable artifact the note named. Guessing the binding is replaced by
    # not needing to guess. The recall cost — a symbol genuinely missing from
    # A while present in B goes unreported — is the deliberate trade
    # (precision over recall, as with bare-identifier extraction).
    readable = [c for c in content_by_artifact.values() if c is not None]

    mismatches: List[Dict[str, object]] = []
    checked = 0
    for claim in claims:
        artifact = str(claim["artifact"])
        symbols = list(claim["symbols"])  # type: ignore[arg-type]
        content = content_by_artifact.get(artifact)
        if content is None:
            continue
        checked += 1
        missing = missing_symbols(symbols, content)
        if len(readable) > 1:
            missing = [s for s in missing
                       if all(other.count(_symbol_needle(s)) == 0
                              for other in readable)]
        if missing:
            mismatches.append({
                "artifact": artifact,
                "missing": missing,
                "present": [s for s in symbols if s not in missing],
                "content_bytes": len(content.encode("utf-8", "replace")),
            })

    if checked == 0:
        return {"fired": False, "goal_id": goal_id, "mismatches": [],
                "claims_checked": 0,
                "reason": "no claimed artifact resolved to a readable store path"}

    return {
        "fired": bool(mismatches),
        "goal_id": goal_id,
        "mismatches": mismatches,
        "claims_checked": checked,
        "reason": None if mismatches else "all claimed symbols present in store",
    }
