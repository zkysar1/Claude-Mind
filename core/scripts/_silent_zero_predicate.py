"""Single source of truth: "would this command score a FAILED call as a zero?"

Imported by:
  - silent-zero-gate.py   (Layer A -- enforce at PreToolUse[Bash])
  - silent-zero-audit.py  (Layer C -- observe the committed corpus)

CRITICAL: do not duplicate these predicates inline anywhere else. If a third
caller needs the same check, import it from here. The layers MUST agree on what
"bad" means, or the detective's signal diverges from the gate's enforcement.

THE MECHANISM (g-318-80, measured 2026-08-10 on cc-02 / Linux 6.8.0-136-generic)

A framework wrapper fails. It writes a diagnostic to stderr and exits non-zero,
leaving stdout EMPTY. The caller pipes stdout into a parser carrying an
empty-coercion fallback:

    bash core/scripts/aspirations-query.sh --title-contains X 2>/dev/null \\
      | py -3 -c "d = json.loads(sys.stdin.read() or '[]'); print(len(d))"

`or '[]'` converts the failure into an empty list, `2>/dev/null` discards the
only evidence it happened, and the exit status is never read. The caller then
scores 0 -- byte-identical to a successful query that genuinely matched
nothing. Worse than silent: a REPEATED broken call yields a run of identical
zeros, which reads as unusually strong evidence.

WHY THE EMITTER-SIDE FIX DOES NOT REACH THIS. The obvious remedy -- "print
errors to stderr, not stdout" -- is ALREADY fleet-wide and is insufficient by
construction. Measured: guardrails-read.sh has emitted its error to stderr
since its FIRST commit (48ffffb4e) and still produced the founding incident.
With the error on stderr, `grep -c` on stdout returns 0, which is exactly what
a successful-but-empty call returns. The exit status is the ONLY difference,
and this idiom is precisely the one that discards it. A statement-scoped scan
of 497 shell + 572 python framework files found ZERO true emitter-side
positives: every hit was usage text on stdout, a machine-readable
{"error": ...} JSON contract, or a report script whose stdout IS the report.

WHY THIS IS A BASH GATE AND NOT A RULE. The population is not in committed
code. A caller-side scan of core/scripts + .claude/skills + world/scripts found
4 sites, all SKILL.md pseudocode. The real population is ad-hoc, LLM-composed
one-off commands -- which only a chokepoint at composition time can see.
Measured over 102,856 Bash calls in 5 session transcripts (2.15 GB): 22,123
carried a scoring shape and >=75.6% carried no exit-status signal at all. Two
honor-system rails (guard-1587, rb-245) were live in context during all three
founding incidents and did not prevent any of them.

NOISE BUDGET, measured before this gate was written. Three candidate
predicates over the same 102,856 calls:

    silenced scoring call, any form      13,074   12.71%   too noisy to read
    empty-coercion present                   98    0.10%   THIS PREDICATE
    empty-coercion AND stderr silenced       78    0.08%

The broad form would be trained past within a day. The coercion idiom is the
narrow, unambiguous one: the caller has WRITTEN a fallback whose only effect is
to make a failure look like an empty success. Silencing is deliberately NOT
required -- `or '[]'` erases the failure whether or not stderr was discarded.
"""

import re

# A framework wrapper invocation. Domain scripts live at an external
# $WORLD_DIR/$WORLD_PATH, so both spellings (and their braced forms) count.
_WRAPPER = re.compile(
    r"(?:core/scripts|\$\{?WORLD_DIR\}?/scripts|\$\{?WORLD_PATH\}?/scripts)"
    r"/[\w.-]+\.(?:sh|py)"
)

# Consumers that turn stdout into a NUMBER. `head`/`tail` are deliberately
# absent: they window output for display and derive no quantity, so including
# them was measured to inflate the population ~2.5x with non-instances.
_COUNTER = re.compile(r"\|\s*(?:grep\s+-[a-zA-Z]*c\b|wc\s+-[lcw]\b|jq\b)")

# Consumers that turn stdout into a PARSED STRUCTURE.
_PARSER = re.compile(r"\|\s*(?:py\s+-3\s+-c|python3?\s+-c)")
_PARSES = re.compile(r"(?:json\.loads?\b|yaml\.safe_load\b)")

# The defect itself: an explicit fallback converting empty stdout into an empty
# structure. Covers `read() or '[]'`, `... or '{}'`, `... or ''`, either quote.
_COERCE = re.compile(
    r"""(?:sys\.stdin\.read\(\)|stdin\.read\(\)|read\(\))\s*or\s*"""
    r"""(['"])\s*(?:\[\s*\]|\{\s*\}|)\s*\1"""
)

# Any signal that the caller reads the exit status. PIPESTATUS is the correct
# bash idiom for a piped command and MUST be here: omitting it was measured to
# misclassify thousands of deliberately-guarded calls as unguarded.
_RC = re.compile(
    r"(?:PIPESTATUS|\$\?|pipefail|\brc=|\bRC=|\bEXIT=|set\s+-e\b)"
)

# Explicit escape hatch. Present anywhere in the command -> the gate approves.
OVERRIDE_TOKEN = "SILENT_ZERO_GATE_OVERRIDE"


def has_scoring_consumer(command) -> bool:
    """True when the command derives a quantity or a parsed structure from a
    pipe. A python one-liner counts only when it actually parses -- piping into
    `py -3 -c "print('hi')"` scores nothing and is not an instance.
    """
    if not isinstance(command, str):
        return False
    if _COUNTER.search(command):
        return True
    return bool(_PARSER.search(command) and _PARSES.search(command))


def invokes_framework_wrapper(command) -> bool:
    """True when a core/ or domain wrapper produces the piped stdout."""
    if not isinstance(command, str):
        return False
    return bool(_WRAPPER.search(command))


def reads_exit_status(command) -> bool:
    """True when anything in the command inspects an exit status.

    Deliberately generous: `||` and `&&` are NOT included here even though they
    branch on status, because they frequently belong to an unrelated command in
    a multi-command line (measured: they were the sole 'guard' on a large share
    of the guarded bucket, which made that bucket impure). This predicate must
    be precise about what it EXCUSES, so it names only signals that read a
    status explicitly.
    """
    if not isinstance(command, str):
        return False
    return bool(_RC.search(command))


def coerced_fallbacks(command) -> list:
    """Every empty-coercion idiom in the command, as matched text.

    Order follows the command; duplicates are preserved so a caller can report
    a count.
    """
    if not isinstance(command, str):
        return []
    return [m.group(0) for m in _COERCE.finditer(command)]


def silent_zero_violations(command) -> list:
    """Return the empty-coercion idioms that would score a failed framework
    call as a legitimate zero.

    Empty list when the command is not a framework-wrapper invocation, derives
    no quantity from it, reads the exit status, carries no coercion idiom, or
    contains the override token. Fail-open at the type boundary.
    """
    if not isinstance(command, str):
        return []
    if OVERRIDE_TOKEN in command:
        return []
    if not invokes_framework_wrapper(command):
        return []
    if not has_scoring_consumer(command):
        return []
    if reads_exit_status(command):
        return []
    return coerced_fallbacks(command)


def suggest_rewrite(command) -> list:
    """The two working rewrites, in preference order.

    Both are one edit. The first keeps the pipeline shape and is what most of
    the measured population wants; the second is for callers that genuinely
    need a default and should say so where a reader can see it.
    """
    return [
        "capture and check the status, then parse:\n"
        "    out=$(bash core/scripts/<wrapper>.sh ...); rc=$?\n"
        "    [ $rc -eq 0 ] || { echo \"wrapper failed rc=$rc\" >&2; exit $rc; }\n"
        "    printf '%s' \"$out\" | py -3 -c \"...json.loads(sys.stdin.read())...\"",
        "keep the pipe and read PIPESTATUS:\n"
        "    bash core/scripts/<wrapper>.sh ... | py -3 -c \"...\"\n"
        "    echo \"producer_rc=${PIPESTATUS[0]}\"   # a non-zero here invalidates the count",
    ]
