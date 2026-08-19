#!/usr/bin/env python3
# domain-leak-exempt: docstring-only references to literal incident strings
# ("cannot access EFS") are kept for traceability. The decision logic lives
# in gates/capability.py — same exemption applies there.
"""Capability Gate CLI — thin wrapper around core/scripts/gates/capability.py.

PR 7a/5 extracted the decision logic + side effects into
`gates.capability.evaluate()` so daemon writer endpoints can import it
directly (skip ~300ms subprocess startup per CREATE_BLOCKER call). This
script preserves the legacy argv shape, exit codes, stdout output shape,
stderr surface, and ledger side-effect — every caller of
`bash core/scripts/capability-gate.py` is unchanged.

Called by CREATE_BLOCKER (aspirations-execute/SKILL.md Step 2.6) before
writing `participants: [user]` for an infrastructure blocker. Scans three
canonical sources for agent-provisionable capabilities:

  1. world/forged-skills.yaml (skill names, triggers, companion_scripts)
  2. .claude/skills/*/SKILL.md (skill names, triggers from YAML front matter)
  3. world/conventions/capability-routing.md ("Agent-Provisionable Services" rows)

If any keyword from the failure_reason matches an entry in these sources, the
gate concludes the action IS agent-provisionable. The legacy fallback
--override-agent-match "<justification>" exists for genuine false-positives.
The preferred path is --evidence with structured proof entries
(see gates/capability.py docstring for the schema).

See gates/capability.py for the canonical output shape and the SINGLE
SOURCE OF TRUTH for NARRATIVE_PATTERNS, USER_ONLY_PRECONDITION_SUBSTRINGS,
USER_ONLY_PRECONDITION_CURES, SESSION_REQUIREMENT_* constants, _STOPWORDS,
_IMPERATIVE_VERBS, _VALID_EVIDENCE_TYPES.

Fail-open on parse errors: a broken YAML or unreadable file means that
source contributes zero matches but doesn't itself block. Silent blocking
from framework bugs would be worse than the problem we're solving.
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _stdio import reconfigure_stdio  # type: ignore  # noqa: E402
reconfigure_stdio()

from gates.capability import evaluate  # type: ignore  # noqa: E402


SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent
SKILLS_DIR = PROJECT_ROOT / ".claude" / "skills"

from _paths import agent_dir as _agent_dir  # noqa: E402


def _read_local_paths_conf(agent_name: str) -> dict:
    conf = _agent_dir(agent_name) / "local-paths.conf"
    out = {}
    if not conf.is_file():
        return out
    for line in conf.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _resolve_world_dir():
    agent = os.environ.get("MIND_AGENT", "").strip()
    if not agent:
        return None
    conf = _read_local_paths_conf(agent)
    wp = conf.get("WORLD_PATH")
    return Path(wp) if wp else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Capability gate — block user-routing when an agent capability matches."
    )
    ap.add_argument("--failure-reason", required=True,
                    help="The failure reason text from CREATE_BLOCKER.")
    ap.add_argument("--diagnostic-json",
                    help="Optional path to a JSON file with additional diagnostic data.")
    ap.add_argument("--intended-participants", default="user",
                    choices=["agent", "user", "hybrid"],
                    help="Participants the LLM intends to set on the unblocking goal.")
    ap.add_argument("--override-agent-match",
                    help="Justification for overriding a matched capability "
                         "(echoed to stderr).")
    ap.add_argument("--caller-context", default="create-blocker",
                    choices=["create-blocker", "defer"],
                    help=("Which enforcement path is invoking the gate. Only "
                          "affects which bypass flag the human-readable "
                          "`reason` recommends: --override-agent-match at "
                          "CREATE_BLOCKER time, --force-defer on the defer "
                          "path (the two are NOT interchangeable — g-115-2814). "
                          "Defaults to create-blocker so existing callers are "
                          "byte-identical (g-115-3405)."))
    ap.add_argument("--evidence",
                    help=("JSON array of evidence entries approving agent-routing "
                          "of a [user]-tagged goal. Each entry: "
                          "{type:rb|pipeline|metric|goal|guardrail|tree|experience, "
                          "id:<identifier>, claim:<short relevance statement>}. "
                          "When >=1 valid entry is present, the gate passes and "
                          "logs an 'evidence-approval' record to "
                          "world/blocker-gate-overrides.jsonl. Preferred over "
                          "--override-agent-match when available."))
    ap.add_argument("--output", default="json", choices=["json", "human"],
                    help="Output format.")
    ap.add_argument("--suggest-unblock", action="store_true",
                    help=("When set AND would_block=True, emit four extra "
                          "fields the caller can use to file an Unblock goal: "
                          "unblock_suggested, unblock_title, "
                          "unblock_description, matched_capability."))
    ap.add_argument("--for-goal-id",
                    help=("Optional goal-id to interpolate into unblock_title "
                          "when --suggest-unblock is set."))
    args = ap.parse_args(argv)

    diagnostic_text = ""
    if args.diagnostic_json:
        try:
            diagnostic_text = Path(args.diagnostic_json).read_text(encoding="utf-8")
        except Exception:
            pass

    world_dir = _resolve_world_dir()
    agent_name = os.environ.get("MIND_AGENT", "").strip()

    result = evaluate(
        args.failure_reason,
        diagnostic_text=diagnostic_text,
        intended_participants=args.intended_participants,
        override_agent_match=args.override_agent_match,
        evidence_raw=args.evidence,
        suggest_unblock=args.suggest_unblock,
        for_goal_id=args.for_goal_id,
        agent_name=agent_name,
        caller_context=args.caller_context,
        world_dir=world_dir,
        skills_dir=SKILLS_DIR,
    )

    # Evidence-error short-circuit (3-key shape) preserved verbatim.
    if "evidence_error" in result:
        if args.output == "json":
            print(json.dumps(result, indent=2))
        else:
            print(f"Evidence error: {result['evidence_error']}")
            print(f"Reason: {result['reason']}")
        # Mirror to stderr (g-115-3094 item c). The diagnostic above is complete
        # and actionable, but it was STDOUT-only: measured 2026-07-30, this branch
        # wrote 320 bytes to stdout and 0 to stderr. A caller that surfaces only
        # stderr -- which is the norm for a wrapper that captures stdout as the
        # gate's DATA -- saw an empty error channel and a bare exit 1, so it could
        # not tell a malformed-evidence refusal from any other refusal. That is
        # how one caller retried the same bad payload 3x in 47s before getting it
        # right (alpha session aae8287f, 2026-07-19 07:57-07:58).
        # stderr is diagnostic-only here; stdout keeps carrying the machine-readable
        # payload unchanged, so no existing stdout parser is affected.
        print(
            f"[capability-gate] evidence REFUSED: {result['evidence_error']} "
            f"-- see core/config/conventions/evidence-envelope.md for the "
            f"required {{type,id,claim}} shape",
            file=sys.stderr,
        )
        return 1

    if args.override_agent_match:
        print(
            f"[capability-gate] override applied: {args.override_agent_match}",
            file=sys.stderr,
        )
    if result.get("approval_kind") == "evidence":
        evid_summary = ", ".join(f"{e['type']}:{e['id']}"
                                  for e in (result.get("evidence_applied") or []))
        print(
            f"[capability-gate] evidence approval applied ({evid_summary}); "
            f"logged to {result.get('evidence_logged_to') or '(not logged)'}",
            file=sys.stderr,
        )

    # ADVISORY, never a refusal (g-115-6588 / guard-1412). Printed to stderr on
    # BOTH output modes because the JSON branch below is consumed as machine
    # data by cmd_update_goal -- an advisory added to that payload alone would
    # be parsed and dropped. The exit code is untouched: the defer still writes.
    if result.get("unscoped_fleet_negative_detected"):
        print(
            f"[capability-gate] ADVISORY unscoped fleet-wide claim: "
            f"\"{result.get('unscoped_fleet_negative_claim')}\" -- this defer "
            f"asserts a capability negative for the WHOLE fleet but names no "
            f"box. guard-1412: name the machine it was measured FROM "
            f"(\"...unreachable FROM cc-04\", or \"measured on <host>\"). A "
            f"single-box measurement generalised fleet-wide freezes the goal "
            f"for boxes that can do it. Not refused -- the defer still applies.",
            file=sys.stderr,
        )

    if args.output == "json":
        print(json.dumps(result, indent=2))
    else:
        kws = ", ".join(result["keywords_extracted"]) or "(none)"
        print(f"Keywords: {kws}")
        print(f"Sources scanned: {result['sources_scanned']}")
        print(f"Matches: {result['match_count']}")
        for m in result["matches"][:5]:
            ident = m.get("skill") or (m.get("row", "")[:80])
            print(f"  - [{m['source']}] {ident}  (kw: {m['matched_keyword']})")
        print(f"Routing: {result['suggested_routing']}")
        print(f"Intended: {result['intended_participants']}")
        print(f"Would block: {result['would_block']}")
        print(f"Reason: {result['reason']}")

    return 1 if result["would_block"] else 0


if __name__ == "__main__":
    sys.exit(main())
