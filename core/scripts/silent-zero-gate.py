#!/usr/bin/env python3
"""PreToolUse[Bash] hook -- refuse pipelines that score a FAILED framework call
as a legitimate zero.

Canonical failure (g-318-80, three instances in one session across two agents):

    bash core/scripts/aspirations-query.sh --text <msg-id> 2>/dev/null \\
      | py -3 -c "d = json.loads(sys.stdin.read() or '[]'); print(len(d))"

`--text` is not a flag. The wrapper refuses, writes to stderr, exits 2, leaves
stdout empty. `or '[]'` turns that into an empty list and the caller prints 0 --
identical to a successful query that matched nothing. Eight such calls produced
a FILED GOAL whose central claim ("ZERO of 8 peer requests converted") was
false; four had converted. The wrong claim reached a coordination-board post
and a user report, and all three had to be retracted.

SECOND FORM (g-115-5343): the same failure without any `or '[]'`, produced by a
parser that drops non-JSON lines --

    bash core/scripts/guardrails-read.sh --all 2>/dev/null | py -3 -c "
      for line in sys.stdin:
          if not line.startswith('{'): continue     # discards the usage error
          ..."

Measured: silent_zero_violations() returns [] on this, so the form above was
uncovered. Refused only when the non-conforming line is DISCARDED -- the same
test that SURFACES it and stops is correct handling and stays approved. Full
noise measurement in _silent_zero_predicate.py.

WHY A GATE AND NOT A RULE. Two honor-system rails (guard-1587, rb-245) were
live in context during all three founding incidents and prevented none of them.
The population is ad-hoc, LLM-composed commands, so no committed-code audit can
reach it -- a caller-side file scan found 4 sites, all SKILL.md pseudocode,
against 22,123 scoring-shape calls in the transcript corpus. The one chokepoint
that sees an ad-hoc command is this hook. Same escalation the gradle --tests
class took after seven rediscoveries (.claude/rules/gradle-tests-pattern.md).

DENY, NOT ADVISORY -- and this is a deliberate trade, not an oversight. A
non-blocking PreToolUse hook reaches the model only by emitting
`permissionDecision: "allow"` (guard-1680), but `allow` ALSO short-circuits the
permission system, and the Bash matcher carries sibling DENY gates
(bare-bash-authoring, trailing-echo-exit, git-hook-bypass, git-restore-
uncommitted) whose refusals protect real invariants. Handing out an allow here
could suppress theirs. Denying keeps this gate's message reaching the model
without touching any sibling's verdict. The measured cost is small: the
predicate fires on 98 of 102,856 calls (0.10%), and `SILENT_ZERO_GATE_OVERRIDE`
clears any genuine false positive in one token.

Fail-open contract (CRITICAL -- do not change without revisiting the trade):
any parse/IO/logic error -> approve. A broken gate is recoverable; a
fail-closed gate would block legitimate Bash calls and stall autonomous loops.
"""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from hook_helpers import (  # noqa: E402
    approve_no_mutation,
    emit_deny,
    stdin_json_or_approve,
)
from _silent_zero_predicate import (  # noqa: E402
    OVERRIDE_TOKEN,
    shape_selective_suppressions,
    silent_zero_violations,
    suggest_rewrite,
    suggest_shape_rewrite,
)


def build_reason(idioms) -> str:
    """Compose the deny message: name the offending idiom, the mechanism, and
    the concrete rewrites."""
    head = (
        "silent-zero pipeline refused: {} makes a FAILED call indistinguishable "
        "from an empty result.\n\n"
        "MECHANISM: when the wrapper fails it writes to stderr and exits "
        "non-zero, leaving stdout EMPTY. The fallback above turns that empty "
        "stdout into an empty structure, so the count you read is 0 -- exactly "
        "what a successful call that matched nothing returns. The exit status "
        "is the only thing that distinguishes them, and this pipeline never "
        "reads it. Repeat the call and you get a run of identical zeros, which "
        "reads as unusually strong evidence rather than as a broken "
        "instrument.\n"
    ).format(", ".join("`{}`".format(i) for i in idioms))

    body = "\nUse either of:\n\n  " + "\n\n  ".join(suggest_rewrite(None))

    tail = (
        "\n\nNote this is NOT about stderr. Framework wrappers already write "
        "their errors to stderr -- guardrails-read.sh has done so since its "
        "first commit and still produced this incident class. Moving the "
        "message does not help; reading the status does.\n"
        "If the default is genuinely intended (the wrapper prints nothing on "
        "success), put {} anywhere in the command to bypass.\n"
        "See g-318-80 and .claude/rules/verify-before-assuming.md."
    ).format(OVERRIDE_TOKEN)
    return head + body + tail


def build_shape_reason(idioms) -> str:
    """Compose the deny message for the shape-selective form ().

    Kept SEPARATE from build_reason because the two forms need opposite advice:
    the coercion message says "read the status, moving the message does not help",
    while this one's cheapest fix IS to surface the message. A merged message
    would have to hedge, and a hedged gate message is one a reader skims.
    """
    head = (
        "silent-zero pipeline refused: {} silently DISCARDS every line the "
        "wrapper wrote that is not JSON -- including its error.\n\n"
        "MECHANISM: when the wrapper refuses (an invalid flag, a missing "
        "required filter) it writes a usage message and exits non-zero. Your "
        "filter drops that line, so the loop sees nothing and reports 0 "
        "records -- byte-identical to a healthy call against an empty store. "
        "The shape test is doing the work of a SECOND error suppressor on top "
        "of any stderr redirect: even with stderr merged into stdout, the "
        "diagnostic is thrown away before you can read it (guard-3052).\n"
    ).format(", ".join("`{}`".format(i) for i in idioms))

    body = "\nUse either of:\n\n  " + "\n\n  ".join(suggest_shape_rewrite(None))

    # CONCATENATED, NOT .format() -- deliberately, and this is not style. The
    # example below contains a literal `{`, which str.format reads as an unclosed
    # replacement field and raises on. The gate's fail-open then swallows the
    # exception and the deny silently degrades to an approve: a gate that reports
    # "nothing wrong" because its own error message was malformed. That is the
    # exact defect class this file exists to refuse, and it shipped here first.
    tail = (
        "\n\nNOTE the same test is CORRECT when it stops instead of skipping -- "
        "`if not raw.startswith('{'): print(raw[:400]); raise SystemExit(1)` is "
        "approved by this gate, deliberately. Only the discarding form is "
        "refused.\n"
        "If the wrapper genuinely interleaves non-JSON on success, put "
        + OVERRIDE_TOKEN
        + " anywhere in the command to bypass.\n"
        "See g-115-5343, guard-3052, and .claude/rules/verify-before-assuming.md."
    )
    return head + body + tail


def main():
    payload = stdin_json_or_approve()
    if not isinstance(payload, dict):
        approve_no_mutation()

    if payload.get("tool_name") != "Bash":
        approve_no_mutation()

    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None

    idioms = silent_zero_violations(command)
    if idioms:
        emit_deny(build_reason(idioms))

    # Second form. Checked AFTER the coercion form so that a command carrying
    # both keeps its original, already-pinned message.
    dropped = shape_selective_suppressions(command)
    if dropped:
        emit_deny(build_shape_reason(dropped))

    approve_no_mutation()


if __name__ == "__main__":
    # except Exception lets SystemExit (raised by approve/emit_deny via
    # sys.exit) propagate cleanly. The catch is only for unexpected bugs in
    # main() - in which case we still fail-open per the docstring contract.
    try:
        main()
    except Exception:
        sys.exit(0)
