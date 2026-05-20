#!/usr/bin/env python3
"""Zero-Count Gate — automated guard against schema-drift zero-count claims.

Enforces the rb-245 lesson mechanically: a statistical / audit claim that a
field has value 0 (or is missing) across many records requires evidence that
the field actually exists in the schema. Without a verifying probe, the claim
is blocked.

Sibling of `verify-before-assuming-gate.py` (infrastructure negations) and
`exhaustive-search-gate.py` (knowledge negations). Same exit-code contract:
  - 0 = evidence sufficient OR no trigger matched OR override supplied OR
        internal error (fail-open).
  - 1 = trigger matched but evidence insufficient; caller should treat the
        audit conclusion as unverified and re-run probe first.

Evidence model: the claim ("98% of records have times_triggered=0") is
signal 1. A schema probe via `jsonl-field-probe.py` that confirms the field
exists is signal 2. This is the multi-signal requirement from
`.claude/rules/verify-before-assuming.md` applied to statistical/audit
negations — the domain the sibling verify-before-assuming-gate doesn't cover.

Trigger set is intentionally tight. The cost of a miss is an audit that slips
past the gate (the rb-245 prose note remains as a soft backstop). The cost of
a false positive is one `--override` call, same as the siblings.
"""

import argparse
import json
import re
import sys
from pathlib import Path

# Local-only import for gate-firing telemetry ().
sys.path.insert(0, str(Path(__file__).parent))
from _gate_log import log as _gate_log  # noqa: E402

# Case-insensitive. Matches audit/statistical phrasings. Deliberately narrower
# than the verify-before-assuming-gate — a claim like "is down" is NOT a
# statistical negation and should not double-fire here.
#
# Each pattern is a single-line regex tolerant of short intervening phrases
# ("98% of records have X=0" — the "of records" clause sits between the %
# and the verb, so we don't anchor verb immediately after %).
_TRIGGER_PATTERNS = [
    # "N%" (standalone) — any percentage mention paired with "=0" / "zero" /
    # "missing" / "null" later in the same claim. Covers "98% of records
    # have times_triggered=0", "0% report X=0", etc.
    re.compile(
        r"\b\d+\s*%[\s\S]{0,80}?"
        r"(?:"
        r"=\s*0\b|"
        r"\bis\s+0\b|"
        r"\bare\s+0\b|"
        r"\bequal\s+0\b|"
        r"\bzero\b|"
        r"\bmissing\b|"
        r"\bnull\b|"
        r"\bempty\b"
        r")",
        re.IGNORECASE,
    ),
    # "0 records have ...", "no records have ..."
    re.compile(r"\b(?:0|zero|no)\s+records?\s+(?:have|has|with|contain|containing|showing)\b", re.IGNORECASE),
    # "all/every/none/no X are|have zero|null|empty|missing"
    re.compile(r"\b(?:all|every|none|no)\s+\S+\s+(?:are|is|have|has)\s+(?:zero|0|null|empty|missing)\b", re.IGNORECASE),
    # "N/M records missing ..."
    re.compile(r"\b\d+\s*/\s*\d+\s+records?\s+(?:missing|without|lack(?:ing)?)\b", re.IGNORECASE),
    # "zero-utilization", "0 utilization", "low-utilization claim"
    re.compile(r"\b(?:zero|0)[-\s]utilization\b", re.IGNORECASE),
    # "missing field", "missing column", "missing attribute", "missing property"
    re.compile(r"\bmissing\s+(?:field|column|attribute|property|key)\b", re.IGNORECASE),
    # "low-count conclusion", "zero-count finding"
    re.compile(r"\b(?:low|zero)[-\s]count\s+(?:conclusion|claim|finding|audit)\b", re.IGNORECASE),
    # "utilization_score=0 across", "times_triggered is 0 for 113 records"
    re.compile(r"\b\w+(?:_\w+)*\s*(?:=|is|of)\s*0\s+(?:across|for|in)\s+\d", re.IGNORECASE),
    # "<field>=0 across <N>" with dotted or underscored field, covers the
    # original rb-245 phrasing "has Y=0 across N records"
    re.compile(r"\b[\w\.]+\s*=\s*0\s+across\b", re.IGNORECASE),
]

_VALID_PROBE_RESULTS = ("found", "missing", "")


def _detect_trigger(claim_text):
    """Return the first trigger pattern match, or None."""
    if not claim_text:
        return None
    for pat in _TRIGGER_PATTERNS:
        m = pat.search(claim_text)
        if m:
            return m.group(0)
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=(
            "Zero-count gate — block statistical/audit claims about zero-valued "
            "or missing fields when no schema probe has verified the field "
            "exists (rb-245)."
        )
    )
    ap.add_argument("--claim-text", required=True,
                    help="The audit/statistical claim (scanned for trigger phrases).")
    ap.add_argument("--file-probed", default="",
                    help="Path to the JSONL file whose schema was verified "
                         "(via jsonl-field-probe.py). Empty = no probe run.")
    ap.add_argument("--field-probed", default="",
                    help="The dotted field path that was verified "
                         "(e.g., utilization.times_active).")
    ap.add_argument("--probe-result", default="", choices=list(_VALID_PROBE_RESULTS),
                    help="Outcome of the probe: 'found' = field exists in sample, "
                         "'missing' = field ABSENT (schema drift detected), "
                         "empty = no probe.")
    ap.add_argument("--override",
                    help="Justification for bypassing the gate; echoed to stderr.")
    ap.add_argument("--output", default="json", choices=["json", "human"],
                    help="Output format.")
    args = ap.parse_args(argv)

    result = {
        "claim_text": args.claim_text,
        "trigger_matched": None,
        "file_probed": args.file_probed,
        "field_probed": args.field_probed,
        "probe_result": args.probe_result,
        "override_applied": args.override,
        "would_block": False,
        "reason": "",
        "gate_error": None,
    }

    try:
        trigger = _detect_trigger(args.claim_text)
        result["trigger_matched"] = trigger
        if not trigger:
            result["reason"] = (
                "Claim does not match any zero-count / audit-negation trigger "
                "phrase; gate is a no-op."
            )
            # Telemetry: gate invoked, no pattern match ().
            _gate_log(
                "zero-count-gate",
                "noop",
                trigger_matched="no-pattern-match",
                caller="zero-count-gate.py:main",
                payload=args.claim_text,
                extra={"decision_path": "no-trigger"},
            )
            return _emit(result, args.output, 0)

        # Evidence rule: a probe must have been run AND returned "found".
        # - file_probed empty: no probe at all → block.
        # - probe_result="missing": schema drift confirmed → block (the claim
        #   is spurious, built on a nonexistent field — exactly rb-245).
        # - probe_result="found" + file_probed non-empty: evidence sufficient.
        probed = bool(args.file_probed)
        field_confirmed = args.probe_result == "found"

        if probed and field_confirmed:
            result["reason"] = (
                f"Zero-count claim is supported by schema probe "
                f"(file={args.file_probed}, field={args.field_probed}, "
                f"result=found)."
            )
            # Telemetry: trigger matched but probe evidence sufficient ().
            _gate_log(
                "zero-count-gate",
                "pass",
                trigger_matched=trigger,
                caller="zero-count-gate.py:main",
                payload=args.claim_text,
                extra={
                    "decision_path": "probe-found",
                    "field_probed": args.field_probed,
                },
            )
            return _emit(result, args.output, 0)

        if args.override:
            print(
                f"[zero-count-gate] override applied: {args.override}",
                file=sys.stderr,
            )
            result["reason"] = (
                f"Insufficient probe evidence (file_probed='{args.file_probed}', "
                f"probe_result='{args.probe_result}') but override supplied: "
                f"{args.override}"
            )
            _gate_log(
                "zero-count-gate",
                "override",
                trigger_matched=trigger,
                caller="zero-count-gate.py:main",
                override_reason=args.override,
                payload=args.claim_text,
                extra={
                    "decision_path": "override-applied",
                    "probe_result": args.probe_result,
                },
            )
            return _emit(result, args.output, 0)

        # Block — assemble specific diagnostic.
        if not probed:
            detail = (
                "no schema probe was run. Before claiming a zero-count or "
                "missing-field conclusion, run "
                "`core/scripts/jsonl-field-probe.py --file <path> --field <dot.path>` "
                "and feed its output back via --file-probed, --field-probed, "
                "and --probe-result"
            )
        elif args.probe_result == "missing":
            detail = (
                f"schema probe reported the field ABSENT from the sampled "
                f"record(s) of {args.file_probed} — the zero-count claim is "
                f"almost certainly a schema-drift artifact (rb-245). Confirm "
                f"the correct field name in the actual records before "
                f"reporting this conclusion"
            )
        else:
            detail = (
                f"probe_result='{args.probe_result}' is not 'found'. The "
                f"claim requires a probe that confirms the field exists"
            )

        result["would_block"] = True
        result["reason"] = (
            f"Zero-count / audit claim '{trigger}' matched but {detail}. "
            f"Per rb-245: read one record and verify the field exists before "
            f"aggregating a zero-count across many. If this match is a false "
            f'positive (e.g., describing an intentionally empty input), '
            f're-call with --override "<justification>".'
        )
        # Telemetry: trigger matched, evidence insufficient, no override ().
        _gate_log(
            "zero-count-gate",
            "block",
            trigger_matched=trigger,
            caller="zero-count-gate.py:main",
            payload=args.claim_text,
            extra={
                "decision_path": "no-probe" if not probed else "probe-missing",
                "probe_result": args.probe_result,
            },
        )
        return _emit(result, args.output, 1)

    except Exception as e:
        # Fail-open on any unexpected error — a broken gate must never
        # silently suppress legitimate output.
        result["gate_error"] = f"{type(e).__name__}: {e}"
        result["reason"] = (
            "Gate encountered an unexpected error; failing open. "
            "Caller may proceed; investigate the error offline."
        )
        _gate_log(
            "zero-count-gate",
            "fail_open",
            caller="zero-count-gate.py:main",
            gate_error=f"{type(e).__name__}: {e}",
            payload=args.claim_text,
            extra={"decision_path": "exception"},
        )
        return _emit(result, args.output, 0)


def _emit(result, output_format, exit_code):
    if output_format == "json":
        print(json.dumps(result, indent=2))
    else:
        print(f"Claim: {result['claim_text'][:120]}")
        print(f"Trigger matched: {result['trigger_matched']}")
        print(f"File probed: {result['file_probed']}")
        print(f"Field probed: {result['field_probed']}")
        print(f"Probe result: {result['probe_result']}")
        print(f"Override: {result['override_applied']}")
        print(f"Would block: {result['would_block']}")
        print(f"Reason: {result['reason']}")
        if result["gate_error"]:
            print(f"Gate error: {result['gate_error']}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
