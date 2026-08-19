"""Single source of truth: "would this command score a FAILED call as a zero?"

Imported by:
  - silent-zero-gate.py                (Layer A -- enforce at PreToolUse[Bash])
  - tests/test_silent_zero_gate.py

LAYER C DOES NOT EXIST. This docstring named `silent-zero-audit.py` as a second
importer from its first commit; verified absent 2026-08-14 (bravo, cc-05) by a
repo-wide find that located the gate files as its positive control. Corrected
rather than left standing, because a reader who believes a detective is watching
the committed corpus will not go looking for one -- and the population this class
lives in is ad-hoc commands, which no committed-code audit reaches anyway (see
"WHY THIS IS A BASH GATE AND NOT A RULE" below). That is the argument for why
Layer C was never urgent; it is not an argument for claiming it shipped.

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

# --- SECOND FORM: the shape-selective parser () -------------------
#
# The coercion idiom above is not the only way to make a failure look like an
# empty success. A parser that DROPS every line not matching a JSON opening
# shape does the same thing without writing `or '[]'` anywhere:
#
#     bash core/scripts/guardrails-read.sh --all 2>/dev/null | py -3 -c "
#       for line in sys.stdin:
#           if not line.startswith('{'): continue     # <-- discards the usage error
#           ..."
#
# `--all` is not a valid filter for that reader. It refuses on stderr, 2>/dev/null
# discards that, and the shape filter would have discarded it anyway had it
# reached stdout. Result: 0 records from a healthy 1000-entry store, textually
# identical to a legitimate empty result. guard-3052 names this "a shape-selective
# parser is a SECOND error suppressor".
#
# THIS IS A DISTINCT PREDICATE, NOT A WIDENING. Measured on the founding command:
# has_scoring_consumer() is FALSE (a line scanner never calls json.loads on the
# stream) and coerced_fallbacks() is EMPTY. So silent_zero_violations() returns []
# on the incident that motivated  -- the shipped gate does not fire on it.
#
# THE CONSEQUENCE OF THE SHAPE TEST IS THE WHOLE PREDICATE, and getting this wrong
# would have made the gate worse than useless. Measured over 106,031 Bash calls in
# 1.91 GB of transcripts (bravo, cc-05, uname -r 6.8.0-137-generic, 2026-08-14),
# directly comparable to the 102,856-call survey quoted above:
#
#     broad "silenced scoring call"      24,927   23.509%   rejected, as before
#     coercion idiom (shipped)              173    0.163%
#     shape filter, ANY body                327    0.308%   <- 51% FALSE POSITIVES
#     shape filter + SILENT body             63    0.059%   <- THIS PREDICATE
#     shape filter + LOUD body               66    0.062%   <- must NEVER fire
#
# The naive form is a coin flip. Half its hits are the CORRECT handling --
#
#     if not raw.lstrip().startswith(("{","[")):
#         print("!! NOT JSON -- refusing to report a count:", raw[:400])
#         raise SystemExit(1)
#
# -- which is a positive control for exactly the failure this file exists to
# catch. Flagging it would train readers past the gate using its own best
# evidence. So the discriminator is not the shape test; it is whether the
# non-conforming line is DISCARDED (continue/pass) or SURFACED (print + exit).
# The narrow predicate lands NARROWER than the coercion idiom already shipping.

# KNOWN AND ACCEPTED: writing ABOUT this pattern also trips it. The predicate
# matches command TEXT, so a command that merely QUOTES the defect -- a scanner
# carrying it as a positive control, a test fixture, a doc example -- is refused
# even though nothing would run. Measured: it refused its own author's
# committed-corpus scan minutes after landing. Same trade the gradle --tests gate
# documents (.claude/rules/gradle-tests-pattern.md): the predicate cannot tell
# "about to run this" from "writing this down", and a false negative costs a
# silent zero while a false positive costs one OVERRIDE token. Assemble the
# literal from fragments, or use the token.
#
# BACKLOG CHECK (guard-1426), measured 2026-08-14 on 1,276 committed files
# (core/scripts, .claude/skills/*/SKILL.md, .claude/rules, core/config): ZERO
# carry the discarding shape, with the synthetic control firing. So arming this
# refuses no committed pseudocode and cannot wedge the loop -- the population is
# ad-hoc commands only, as the Layer-A argument below predicts.

# A test for a JSON/YAML opening shape, in the forms measured in the corpus.
_SHAPE_TEST = re.compile(
    r"""(?:
          \.startswith\s*\(\s*[('"\[]*\s*['"][\{\[]
        | \[\s*:\s*1\s*\]\s*(?:!=|==)\s*['"][\{\[]
        | \[\s*0\s*\]\s*(?:!=|==|\s+in\s+)\s*['"][\{\[]
        | re\.(?:match|search)\s*\(\s*r?['"]\^\s*(?:\\s\*)?\[?[\{\[]
        )""",
    re.VERBOSE,
)

# The non-conforming line is SURFACED and the caller stops. Correct handling --
# its presence exonerates the command outright.
_SURFACES_AND_STOPS = re.compile(
    r"(?:raise\s+SystemExit|sys\.exit|SystemExit\s*\(|exit\s*\(\s*1)"
)

# The non-conforming line is silently dropped. This is the defect.
_DISCARDS = re.compile(
    r"""\.startswith\s*\([^)]*\)\s*:?\s*(?:continue|pass)\b"""
    r"""|not\s+[\w.()\[\]'"]*\.startswith\s*\([^)]*\)\s*:\s*continue\b"""
)

# The parser consumes the piped stream (as opposed to a literal or a file).
_CONSUMES_STDIN = re.compile(
    r"(?:for\s+\w+\s+in\s+sys\.stdin|sys\.stdin\b|stdin\.read)"
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


def shape_selective_suppressions(command) -> list:
    """Return the shape-filter idioms that silently DISCARD a wrapper's error.

    Deliberately a PEER of silent_zero_violations rather than folded into it
    (guard-2601): that function is the SSOT for the coercion form and is imported
    by name elsewhere. Widening it in place would silently re-scope every existing
    caller, including the Layer-C audit its docstring promises. Two forms, two
    functions, one file.

    Empty list when the command is not a framework-wrapper invocation, does not
    consume stdin in a python one-liner, carries no shape test, SURFACES the
    non-conforming line instead of dropping it, reads the exit status, or contains
    the override token. Fail-open at the type boundary.
    """
    if not isinstance(command, str):
        return []
    if OVERRIDE_TOKEN in command:
        return []
    if not invokes_framework_wrapper(command):
        return []
    if not (_PARSER.search(command) and _CONSUMES_STDIN.search(command)):
        return []
    if not _SHAPE_TEST.search(command):
        return []
    # A caller that stops on the non-conforming line has already done the right
    # thing -- this branch is what keeps the gate off its own positive controls.
    if _SURFACES_AND_STOPS.search(command):
        return []
    if reads_exit_status(command):
        return []
    return [m.group(0) for m in _DISCARDS.finditer(command)]


def suggest_shape_rewrite(command) -> list:
    """The two working rewrites for the shape-selective form, in preference order.

    Both keep the pipeline. The first is usually one word (`continue` -> print),
    and it is preferred because it repairs the DIAGNOSTIC rather than merely
    guarding the count: the operator sees what the wrapper actually said.
    """
    return [
        "surface the non-conforming line instead of dropping it:\n"
        "    for line in sys.stdin:\n"
        "        if not line.startswith('{'):\n"
        "            print('!! non-JSON from wrapper:', line[:200]); raise SystemExit(1)",
        "keep the filter and read the producer's status:\n"
        "    bash core/scripts/<wrapper>.sh ... | py -3 -c \"...\"\n"
        "    echo \"producer_rc=${PIPESTATUS[0]}\"   # non-zero here invalidates the count",
    ]


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
