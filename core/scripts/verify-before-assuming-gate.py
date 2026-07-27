#!/usr/bin/env python3
"""Verify-Before-Assuming Gate — automated guard against premature negative conclusions.

Enforces the `.claude/rules/verify-before-assuming.md` Multi-signal Requirement:
a negative conclusion requires >= 2 independent verification signals before
acceptance. Blocks the claim when fewer than `min-signals` were gathered,
unless the caller supplies `--override "<justification>"`.

Extends the capability-gate pattern (rb-229). Sibling of
`exhaustive-search-gate.py` (g-115-45): same structure, different trigger set
and evidence model.

  - exhaustive-search-gate: knowledge claims (isn't built, doesn't exist) →
    evidence is SEARCH tiers + query count (tree/codebase/web/adjacent).
  - verify-before-assuming-gate (this): operational/infrastructure claims
    (is down, not responding, connection refused) → evidence is INDEPENDENT
    VERIFICATION SIGNALS (different tools / endpoints / evidence types).

Silent-failure awareness (rule #4): the rule explicitly states that commands
with silent-failure flags (`-sf`, `-q`, `2>/dev/null`) count as ZERO signals.
The caller is responsible for NOT counting silent-failed signals in
`--signals-count`. The `--signal-sources` list is purely for audit/diagnosis.

Design notes:
- Fail-open everywhere. Matches capability-gate.py and exhaustive-search-gate.py.
- Trigger set is a superset of exhaustive-search-gate's: includes infrastructure
  phrases ("is down", "not responding", "connection refused") that are about
  verification, not knowledge search. Overlap is intentional — a claim that
  "the service isn't built" triggers BOTH gates. Callers are expected to pick
  the semantically-correct gate; double-gating is a valid defensive pattern.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

from _gate_log import log as _gate_log
from _git_state_absence_patterns import GIT_STATE_ABSENCE_PATTERNS

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

# Superset of exhaustive-search-gate's trigger set, extended with infrastructure
# phrases from verify-before-assuming.md "Anti-patterns" examples.
# Case-insensitive, word-boundary anchored.
_TRIGGER_PATTERNS = [
    # Knowledge negations (shared with exhaustive-search-gate)
    re.compile(r"\bisn't built\b", re.IGNORECASE),
    re.compile(r"\bis not built\b", re.IGNORECASE),
    re.compile(r"\bthere's no way\b", re.IGNORECASE),
    re.compile(r"\bthere is no way\b", re.IGNORECASE),
    re.compile(r"\bcan't be done\b", re.IGNORECASE),
    re.compile(r"\bcannot be done\b", re.IGNORECASE),
    re.compile(r"\bno existing (implementation|support|way)\b", re.IGNORECASE),
    re.compile(r"\bwe'd need to build\b", re.IGNORECASE),
    re.compile(r"\bnot possible\b", re.IGNORECASE),
    re.compile(r"\bdoesn't (exist|support|have)\b", re.IGNORECASE),
    re.compile(r"\bdoes not (exist|support|have)\b", re.IGNORECASE),
    # Infrastructure / operational negations (verify-before-assuming-specific)
    re.compile(r"\bis down\b", re.IGNORECASE),
    re.compile(r"\bis unavailable\b", re.IGNORECASE),
    re.compile(r"\bis not running\b", re.IGNORECASE),
    re.compile(r"\bisn't running\b", re.IGNORECASE),
    re.compile(r"\bis broken\b", re.IGNORECASE),
    re.compile(r"\bnot responding\b", re.IGNORECASE),
    re.compile(r"\bconnection refused\b", re.IGNORECASE),
    re.compile(r"\bserver (is (down|dead|offline|unresponsive)|isn't responding)\b", re.IGNORECASE),
    re.compile(r"\bservice (is (down|dead|offline|unavailable)|isn't available)\b", re.IGNORECASE),
    re.compile(r"\bcan't reach\b", re.IGNORECASE),
    re.compile(r"\bcannot reach\b", re.IGNORECASE),
    re.compile(r"\bfailed to connect\b", re.IGNORECASE),
    re.compile(r"\bunable to connect\b", re.IGNORECASE),
    re.compile(r"\bnot reachable\b", re.IGNORECASE),
    re.compile(r"\bhost (is )?unreachable\b", re.IGNORECASE),
    # Phase 3 widening (FN >> FP extreme — generous bias).
    re.compile(r"\bis offline\b", re.IGNORECASE),
    re.compile(r"\b(server|service|process|job) crashed\b", re.IGNORECASE),
    re.compile(r"\b(server|service|process|job) (is )?stopped\b", re.IGNORECASE),
    re.compile(r"\b(server|service|process|job) (is )?dead\b", re.IGNORECASE),
    re.compile(r"\b(timed|timing) out\b", re.IGNORECASE),
    re.compile(r"\b(connection|request) timeout\b", re.IGNORECASE),
    re.compile(r"\b(permission|access) denied\b", re.IGNORECASE),
    # HTTP-code triggers — phrase form FIRST so `_detect_trigger` returns the
    # human phrase ("503 service unavailable") not just "503". First-match-wins
    # makes order the priority signal. Bare-code is a fallback for log lines
    # that quote the number without the canonical phrase.
    re.compile(r"\b(?:40[34]|50[0234])\s+(?:not found|forbidden|internal|bad gateway|service unavailable|gateway timeout)\b", re.IGNORECASE),
    re.compile(r"\b(?:40[34]|50[0234])\b", re.IGNORECASE),
    re.compile(r"\bunhealthy\b", re.IGNORECASE),
    re.compile(r"\bnot started\b", re.IGNORECASE),
    re.compile(r"\bnot initialized\b", re.IGNORECASE),
    re.compile(r"\bno longer (responding|reachable|available|running)\b", re.IGNORECASE),
    re.compile(r"\bwon't (start|respond|connect|reach)\b", re.IGNORECASE),
    re.compile(r"\b(could not|couldn't) (reach|connect|find|access)\b", re.IGNORECASE),
    re.compile(r"\bno (response|route) (from|to)\b", re.IGNORECASE),
    re.compile(r"\bport (\d+ )?(closed|refused|not listening)\b", re.IGNORECASE),
    re.compile(r"\bDNS (lookup|resolution) (failed|failure)\b", re.IGNORECASE),
    # Git-state-absence negation family () — now sourced from the shared
    # _git_state_absence_patterns module (), splatted here so both gates
    # share ONE definition instead of a hand-synced duplicate. Maps to
    # verify-before-assuming.md "Capability-Absence Claims".
    *GIT_STATE_ABSENCE_PATTERNS,
]


def _detect_trigger(claim_text):
    """Return the first trigger pattern match, or None."""
    if not claim_text:
        return None
    for pat in _TRIGGER_PATTERNS:
        m = pat.search(claim_text)
        if m:
            return m.group(0)
    return None


def _parse_sources(raw):
    """Split comma-separated sources; drop empties. For audit only."""
    if not raw:
        return []
    return [s.strip() for s in raw.split(",") if s.strip()]


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=(
            "Verify-before-assuming gate — block premature negative conclusions "
            "when insufficient independent verification signals exist."
        )
    )
    ap.add_argument("--claim-text", required=True,
                    help="The negation being made (scanned for trigger phrases).")
    ap.add_argument("--signals-count", type=int, default=0,
                    help="Independent verification signals attempted (NON-SILENT). "
                         "Per rule #4, silent-failure commands (-sf, -q, 2>/dev/null) "
                         "count as ZERO — caller must not include them.")
    ap.add_argument("--signal-sources", default="",
                    help="Comma-separated list of signal sources (tool names, "
                         "endpoints, commands). For audit/diagnosis only; the "
                         "gate decision uses --signals-count.")
    ap.add_argument("--min-signals", type=int, default=2,
                    help="Minimum independent signals required (default: 2, per "
                         "verify-before-assuming.md rule #1).")
    ap.add_argument("--infra-check-run", action="store_true",
                    help="Indicates the caller ran `infra-health.sh check <component>` "
                         "against the affected component. Required when the claim "
                         "matches an infrastructure trigger (rule #3).")
    ap.add_argument("--override",
                    help="Justification for bypassing the gate; echoed to stderr.")
    ap.add_argument("--output", default="json", choices=["json", "human"],
                    help="Output format.")
    args = ap.parse_args(argv)

    result = {
        "claim_text": args.claim_text,
        "trigger_matched": None,
        "trigger_type": None,
        "signals_count": args.signals_count,
        "signal_sources": _parse_sources(args.signal_sources),
        "min_signals": args.min_signals,
        "infra_check_run": args.infra_check_run,
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
                "Claim does not match any negative-conclusion trigger phrase; "
                "gate is a no-op."
            )
            return _emit(result, args.output, 0, decision="noop")

        # Classify trigger for infrastructure-specific rule #3 enforcement.
        # Substring match against the trigger text; keep terms aligned with
        # _TRIGGER_PATTERNS above when adding new operational phrases.
        infra_patterns = (
            "down", "unavailable", "running", "broken", "responding",
            "refused", "reach", "connect",
            "offline", "crashed", "stopped", "dead", "timed", "timeout",
            "denied", "404", "403", "500", "502", "503", "504",
            "unhealthy", "started", "initialized", "couldn't", "could not",
            "no longer", "won't",
            "no response", "no route", "port", "dns",
        )
        is_infra_trigger = any(p in trigger.lower() for p in infra_patterns)
        result["trigger_type"] = "infrastructure" if is_infra_trigger else "knowledge"

        # Rule #3: infrastructure claims MUST have had infra-health.sh check run.
        # If the flag wasn't passed, this is an independent block reason
        # (distinct from signal-count insufficiency) — surfaced in reason text.
        infra_check_missing = is_infra_trigger and not args.infra_check_run

        sufficient_signals = args.signals_count >= args.min_signals

        if sufficient_signals and not infra_check_missing:
            result["reason"] = (
                f"Negation claim is supported by verification "
                f"(signals={args.signals_count}/{args.min_signals}"
                f"{', infra-health.sh run' if is_infra_trigger else ''})."
            )
            return _emit(result, args.output, 0, decision="pass")

        if args.override:
            # Audit trail on stderr — same pattern as capability-gate.py.
            print(
                f"[verify-before-assuming-gate] override applied: {args.override}",
                file=sys.stderr,
            )
            result["reason"] = (
                f"Insufficient verification (signals={args.signals_count}, "
                f"infra_check={'yes' if args.infra_check_run else 'no'}) but "
                f"override supplied: {args.override}"
            )
            return _emit(result, args.output, 0, decision="override")

        # Block — assemble specific diagnostic.
        problems = []
        if not sufficient_signals:
            problems.append(
                f"signals={args.signals_count} (min {args.min_signals}, per "
                f"rule #1 multi-signal requirement)"
            )
        if infra_check_missing:
            problems.append(
                "infra-health.sh check was not run against the affected "
                "component (required by rule #3 for infrastructure claims)"
            )

        result["would_block"] = True
        result["reason"] = (
            f"Negation claim '{trigger}' matched but verification evidence "
            f"is insufficient: {'; '.join(problems)}. Per "
            f"verify-before-assuming.md: require 2+ independent signals "
            f"(different tools, endpoints, or evidence types); silent-failure "
            f"commands count as zero. If this match is a false positive, "
            f're-call with --override "<justification>".'
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
            "verify-before-assuming-gate",
            decision,
            trigger_matched=result.get("trigger_matched"),
            payload=result.get("claim_text"),
            override_reason=result.get("override_applied"),
            gate_error=result.get("gate_error"),
            extra={
                "would_block": result.get("would_block"),
                "trigger_type": result.get("trigger_type"),
                "signals_count": result.get("signals_count"),
                "min_signals": result.get("min_signals"),
                "infra_check_run": result.get("infra_check_run"),
            },
        )
    if output_format == "json":
        print(json.dumps(result, indent=2))
    else:
        print(f"Claim: {result['claim_text'][:120]}")
        print(f"Trigger matched: {result['trigger_matched']} ({result['trigger_type']})")
        print(f"Signals count: {result['signals_count']} / min {result['min_signals']}")
        print(f"Signal sources: {result['signal_sources']}")
        print(f"Infra check run: {result['infra_check_run']}")
        print(f"Override: {result['override_applied']}")
        print(f"Would block: {result['would_block']}")
        print(f"Reason: {result['reason']}")
        if result["gate_error"]:
            print(f"Gate error: {result['gate_error']}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
