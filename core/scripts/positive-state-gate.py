#!/usr/bin/env python3
"""Positive-State Gate — guard against unverified positive file-state claims.

Sibling of `zero-count-gate.py`. Where that gate catches negative/audit
claims built without a schema probe, this one catches POSITIVE claims about
a file's existence, mtime, or content that were stated without an in-turn
read. Canonical incident: "handoff.yaml reflects session 50" — the file did
not exist; the claim was reused from stale summary context.

Same exit-code contract as siblings:
  - 0 = claim has no trigger match, evidence is sufficient, override supplied,
        or internal error (fail-open).
  - 1 = trigger matched but evidence does not reference the claimed file;
        caller must re-read before stating the claim.

Evidence model: the claim is signal 1. An in-turn tool output (Read result,
`ls`, `cat`, `stat`) that references the same file path is signal 2. The
`--evidence` argument is a concatenation of recent tool outputs — typically
pasted from the current turn. If the claimed file/path is not a substring of
the evidence, the claim is unverified.

Trigger set is deliberately narrow. The rule covers positive assertions about
specific named files. Generic narrative ("the framework is stable", "alpha
is learning") is out of scope — only claims that bind a filename to a
specific state value fire this gate.
"""

import argparse
import json
import os
import re
import sys

from _gate_log import log as _gate_log

# Patterns that indicate a positive claim about a named file's state. Each
# pattern must both signal "positive state claim" AND capture/surface a file
# path or filename so the evidence match can verify it.
#
# The extraction pass (below) re-scans the claim for filename-like tokens —
# we don't rely on regex capture groups here, because several patterns
# benefit from broader phrase-matching than a single capture group allows.
_TRIGGER_PATTERNS = [
    # "handoff.yaml reflects session N", "session-log.jsonl reflects ..."
    re.compile(r"\b[\w\-\./]+\.(?:yaml|yml|json|jsonl|md|txt|conf|sh|py|log)\b[\s\S]{0,60}?\b(?:reflects|shows|indicates|contains|has|holds|is|was|includes|says|states)\b", re.IGNORECASE),
    # "according to X.ext"
    re.compile(r"\baccording to\s+[\w\-\./]+\.(?:yaml|yml|json|jsonl|md|txt|conf|sh|py|log)\b", re.IGNORECASE),
    # "X.ext last_updated is|shows|was"
    re.compile(r"\b[\w\-\./]+\.(?:yaml|yml|json|jsonl|md|txt|conf|sh|py|log)\b[\s\S]{0,40}?\blast_updated\b", re.IGNORECASE),
    # "file X was (written|created|updated|deleted)"
    re.compile(r"\bfile\s+[\w\-\./]+[\s\S]{0,20}?\b(?:was|has\s+been|is)\s+(?:written|created|updated|deleted|populated|generated)\b", re.IGNORECASE),
    # "X.ext now contains|holds|shows"
    re.compile(r"\b[\w\-\./]+\.(?:yaml|yml|json|jsonl|md|txt|conf|sh|py|log)\b[\s\S]{0,20}?\bnow\s+(?:contains|holds|shows|reflects)\b", re.IGNORECASE),
    # Session-file references without extension (agent-state, persona-active,
    # runner-heartbeat, agent-mode, stop-loop, stop-requested, stop-target-mode,
    # loop-active, latest-session-id, running-session-id, compact-pending)
    re.compile(
        r"\b(?:agent-state|agent-mode|persona-active|runner-heartbeat|stop-loop|stop-requested|stop-target-mode|loop-active|latest-session-id|running-session-id|compact-pending)\b"
        r"[\s\S]{0,40}?"
        r"\b(?:is|was|shows|reflects|contains|set to|equals|=)\b",
        re.IGNORECASE,
    ),
]

# Filename extraction: pull any token that looks like a path or file name.
# Used after a trigger matches to decide which paths must appear in evidence.
_FILENAME_RE = re.compile(
    r"\b[\w\-\./]+\.(?:yaml|yml|json|jsonl|md|txt|conf|sh|py|log)\b",
    re.IGNORECASE,
)
_SIGNAL_FILE_RE = re.compile(
    r"\b(?:agent-state|agent-mode|persona-active|runner-heartbeat|stop-loop|stop-requested|stop-target-mode|loop-active|latest-session-id|running-session-id|compact-pending)\b",
    re.IGNORECASE,
)


def _detect_trigger(claim_text):
    if not claim_text:
        return None
    for pat in _TRIGGER_PATTERNS:
        m = pat.search(claim_text)
        if m:
            return m.group(0)
    return None


def _extract_paths(claim_text):
    """Return the set of path/filename tokens the claim refers to."""
    paths = set()
    for m in _FILENAME_RE.finditer(claim_text):
        paths.add(m.group(0))
    for m in _SIGNAL_FILE_RE.finditer(claim_text):
        paths.add(m.group(0))
    return paths


def _evidence_contains(evidence_text, path_token):
    """True if the evidence references this path.

    Matching is tolerant: we accept the full token OR just its basename, and
    we normalize both sides to lowercase. Windows and Unix separators both
    work because we split on both when checking the basename.
    """
    if not evidence_text or not path_token:
        return False
    e = evidence_text.lower()
    t = path_token.lower()
    if t in e:
        return True
    base = os.path.basename(t.replace("\\", "/"))
    if base and base in e:
        return True
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=(
            "Positive-state gate — block positive file-state claims that "
            "were not accompanied by an in-turn read/probe of the claimed "
            "file. Sibling of zero-count-gate.py."
        )
    )
    ap.add_argument("--claim", required=True,
                    help="The positive assertion under audit (e.g., "
                         "'handoff.yaml reflects session 50').")
    ap.add_argument("--evidence", default="",
                    help="Concatenation of in-turn tool outputs (Read "
                         "result, ls/cat/stat output, etc.). Empty or "
                         "'(no read)' means no probe occurred.")
    ap.add_argument("--override",
                    help="Justification for bypassing the gate; echoed to stderr.")
    ap.add_argument("--output", default="json", choices=["json", "human"],
                    help="Output format.")
    args = ap.parse_args(argv)

    result = {
        "claim": args.claim,
        "trigger_matched": None,
        "paths_extracted": [],
        "paths_unverified": [],
        "evidence_present": bool(args.evidence and args.evidence.strip() and args.evidence.strip().lower() != "(no read)"),
        "override_applied": args.override,
        "would_block": False,
        "reason": "",
        "gate_error": None,
    }

    try:
        trigger = _detect_trigger(args.claim)
        result["trigger_matched"] = trigger
        if not trigger:
            result["reason"] = (
                "Claim does not match any positive-state trigger pattern; "
                "gate is a no-op."
            )
            return _emit(result, args.output, 0, decision="noop")

        paths = _extract_paths(args.claim)
        result["paths_extracted"] = sorted(paths)

        if not paths:
            # Trigger fired but we could not extract a concrete filename. This
            # happens on phrases that sound like file-state claims but don't
            # name a file token. Fail-open — the rule covers specific-file
            # claims, not generic narrative.
            result["reason"] = (
                "Trigger matched but no concrete file token was extractable "
                "from the claim; gate passes (out of scope)."
            )
            return _emit(result, args.output, 0, decision="pass")

        unverified = [p for p in paths if not _evidence_contains(args.evidence, p)]
        result["paths_unverified"] = sorted(unverified)

        if not unverified:
            result["reason"] = (
                f"All {len(paths)} claimed paths are referenced in the "
                f"evidence; claim is verified."
            )
            return _emit(result, args.output, 0, decision="pass")

        if args.override:
            print(
                f"[positive-state-gate] override applied: {args.override}",
                file=sys.stderr,
            )
            result["reason"] = (
                f"{len(unverified)} claimed path(s) missing from evidence "
                f"but override supplied: {args.override}"
            )
            return _emit(result, args.output, 0, decision="override")

        result["would_block"] = True
        result["reason"] = (
            f"Positive file-state claim '{trigger}' matched, and the "
            f"following path(s) are NOT present in the in-turn evidence: "
            f"{', '.join(unverified)}. Per verify-before-assuming.md "
            f"Positive File-State Claims: a claim about a file's state "
            f"requires the file to have been Read/ls/stat'd in the same "
            f"turn. Re-probe the file before stating the claim. If the "
            f"match is a false positive, re-call with "
            f'--override "<justification>".'
        )
        return _emit(result, args.output, 1, decision="block")

    except Exception as e:
        result["gate_error"] = f"{type(e).__name__}: {e}"
        result["reason"] = (
            "Gate encountered an unexpected error; failing open. "
            "Caller may proceed; investigate the error offline."
        )
        return _emit(result, args.output, 0, decision="fail_open")


def _emit(result, output_format, exit_code, decision=None):
    if decision is not None:
        # gate_id MUST match core/config/gates.yaml id.
        _gate_log(
            "positive-state-gate",
            decision,
            trigger_matched=result.get("trigger_matched"),
            payload=result.get("claim"),
            override_reason=result.get("override_applied"),
            gate_error=result.get("gate_error"),
            extra={
                "would_block": result.get("would_block"),
                "paths_extracted_count": len(result.get("paths_extracted") or []),
                "paths_unverified_count": len(result.get("paths_unverified") or []),
            },
        )
    if output_format == "json":
        print(json.dumps(result, indent=2))
    else:
        print(f"Claim: {result['claim'][:120]}")
        print(f"Trigger matched: {result['trigger_matched']}")
        print(f"Paths extracted: {result['paths_extracted']}")
        print(f"Paths unverified: {result['paths_unverified']}")
        print(f"Evidence present: {result['evidence_present']}")
        print(f"Override: {result['override_applied']}")
        print(f"Would block: {result['would_block']}")
        print(f"Reason: {result['reason']}")
        if result["gate_error"]:
            print(f"Gate error: {result['gate_error']}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
