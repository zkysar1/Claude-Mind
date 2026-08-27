#!/usr/bin/env python3
"""Predicate + imperative text for the full-suite JIT advisory ().

SINGLE SOURCE OF TRUTH, in an underscore-named module so it is importable --
`full-suite-imperative-gate.py` is hyphenated and cannot be imported, which is
the same reason `_gradle_tests_predicate.py` exists next door. The gate and its
tests both import from here; neither re-types the predicate (rb-8183: a copied
predicate agrees on the day it is written and stops agreeing the first time
either side is edited, with nothing going red to say so).
"""

import re

OVERRIDE_TOKEN = "FULL_SUITE_IMPERATIVE_OVERRIDE"

# Trigger families, matched at COMMAND POSITION -- never as a bare mention.
#
# The naive form (`\bpytest\b` anywhere in the command) fires on `grep pytest
# notes.md`, on a heredoc documenting the runner, and on this module's own test
# file. The sibling gradle gate explicitly ACCEPTS that trade, and is right to:
# for a DENY, a false positive costs one refused token while a false negative
# costs a silent zero-test run. The trade INVERTS for an advisory -- nothing is
# refused, so a false positive is pure noise on a channel whose only job is to
# be read, and this gate fires on ordinary daily commands rather than on a
# mistake. Hence the statement-structure resolution below.

_SEPARATOR_RE = re.compile(r"(?:\|\||&&|[;|&\n])")
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Wrappers that take the real command as their tail.
#
# `bash` and `sh` are in here because `bash core/scripts/run-full-suite.sh` is
# the form this repo actually uses -- every invocation in the rules, the skills
# and the runbooks is written that way. Leaving them out made the gate miss its
# single most common trigger while every hand-written probe (`pytest ...`,
# `./gradlew ...`) still passed, which is the shape of a predicate that tests
# green and is inert in production (guard-920).
#
# `bash -c "..."` is NOT unwrapped: stripping the wrapper leaves `-c` as the
# head, which matches nothing, so it silently does not fire. That is deliberate
# -- unwrapping a quoted payload correctly is more machinery than an advisory
# justifies, and fail-quiet is the safe direction when nothing is being refused.
_WRAPPERS = frozenset(
    {
        "sudo", "env", "time", "nohup", "command", "exec", "xargs", "nice",
        "stdbuf", "bash", "sh", "zsh",
    }
)

_FRAMEWORK_HEAD_RE = re.compile(
    r"^(?:[\w./\\-]*[/\\])?(?:run-full-suite(?:\.sh|\.py)?|pytest)$"
)
_PYTHON_HEAD_RE = re.compile(r"^(?:[\w./\\-]*[/\\])?(?:python3?(?:\.exe)?|py)$")
_GRADLE_HEAD_RE = re.compile(r"^(?:[\w./\\-]*[/\\])?gradlew(?:\.bat)?$")

FRAMEWORK_IMPERATIVE = """\
run-full-suite / pytest is about to run. Six things decide whether its output
means anything (.claude/rules/run-full-suite-after-deep-code.md):

1. READ THE `VERDICT:` LINE FIRST, before any number above it. A run reporting
   `TOTAL: N passed, 0 failed, 0 errors` with every per-chunk line reading
   `0 failed` can still be `VERDICT: INVALID (contended)`, which means the
   number means NOTHING. Confirmed on four boxes.
2. `VERDICT: GENUINE` CAN BE FALSE. Failures confined to one chunk with later
   chunks clean is contention, however large or small the count -- a SMALL
   count is more suspicious, not less, because 14 failures look individually
   plausible enough to triage one by one. Re-run the worst-hit chunk's files
   solo first; `run-full-suite.sh --triage` chains that and also reports
   OWNERSHIP, so you do not file a duplicate against a tracked red.
3. THE CHUNK LADDER IS A RETRY PROTOCOL, NOT A SETTING. 8 -> 12 -> 16 -> 20 ->
   24 -> 28 -> 32. A rung is never inheritable -- not from another box, and not
   from your own earlier run on this one. Escalate only when the VERDICT says
   to, and never read a contended run's totals as a regression.
4. NEVER PIPE THE RUNNER. A trailing `| tail` replaces its exit code with the
   pipe's, destroying the exit-2 INVALID signal, and a bounded window discards
   the VERDICT line itself. Redirect to a file OUTSIDE any synced tree, then
   Read it.
5. `VERDICT: CLEAN` SCOPES TO THE CHUNKED PYTEST HALF ONLY. Also grep `^FAIL`
   for the invisible (main()-style + shell) suites and the domain half --
   each half reports separately and none may ride under the chunked verdict.
   (mind_api/tests folded into the chunked pool 2026-08-20, g-115-6942.)
6. ON A BUSY BOX, PIN THE TREE BEFORE YOU LAUNCH. `VERDICT: INVALID
   (tree-moved)` outranks every other verdict and voids the entire run, and
   HEAD moves for reasons that are not a peer merge: your OWN `git commit`
   counts, and so does your own loop's turn-end iteration-push merge. The tree
   lock does NOT cover you -- it returns 0 for your own sid, and a BACKGROUNDED
   run inherits no MIND_SID at all so it takes no lock while still printing
   authoritative-looking chunk counts. Remedy: `git worktree add --detach
   /tmp/<name> <sha>`. FIRST `cp agents/<you>/local-paths.conf`
   INTO the worktree, then run the suite THERE exporting MIND_AGENT,
   MIND_SID, STORAGE_BACKEND=local. The conf copy is load-bearing: it is
   GITIGNORED, so a worktree cannot inherit it and WORLD_DIR/META_DIR resolve
   EMPTY -- 0 passed / 106 errors, an INVALID run that reads as a
   catastrophic regression (alpha cc-04; zeta cc-02 2026-08-27). DO NOT reach
   for MIND_WORLD/MIND_META: this line prescribed exporting them until
   2026-08-27 and THEY CANNOT WORK -- core/scripts/tests/conftest.py pops
   BOTH at module import, deliberately; its own comment says the unset case
   resolves through the conf chain, measured in the MAIN repo where the conf
   EXISTS. A worktree breaks that premise, so the copied conf is the only
   channel surviving collection. That export remedy was itself a guard-2030
   defect -- a correctly-measured diagnosis with an unmeasured remedy beside
   it, believed by adjacency, costing a 30-min run.
   Measured 6 voided runs across 3 agents before this line
   existed (guard-4774, guard-4940, guard-5124).

On an own-cloud box `STORAGE_BACKEND=local` is MANDATORY for any test runner,
including bash aggregators and direct `python3 test_*.py` (guard-955): a
tmp-world write otherwise collides on the PRODUCTION S3 key. And a targeted run
is never sufficient for a deep-code closure claim."""

GRADLE_IMPERATIVE = """\
A gradle command is about to run (.claude/rules/run-full-suite-after-deep-code.md):

1. A `--tests` run that reports ZERO tests executed is a FAILED measurement,
   not a pass. Read the executed-test count before concluding anything --
   "BUILD SUCCESSFUL" with 0 selected tells you nothing about the code.
2. Closing a deep goal needs the FULL suite (`./gradlew test --no-daemon`), not
   `--tests <ChangedTestClass>`. The full run is what catches symmetry and
   contract tests in OTHER classes -- the canonical regression shipped exactly
   that way."""


def _module_target(rest):
    """The argument to `-m`, or "" when there is no module form.

    Keeps `python3 -c "print('pytest')"` from firing: only `-m pytest` counts.
    """
    for i, token in enumerate(rest):
        if token == "-m" and i + 1 < len(rest):
            return rest[i + 1]
        if token.startswith("-m") and len(token) > 2:
            return token[2:]
    return ""


def statement_heads(command):
    """Yield (head_token, remaining_tokens) per top-level statement, with env
    assignments and wrapper commands stripped.

    Deliberately NOT quote-aware. A quote-aware splitter is the right tool for a
    gate that REFUSES -- see `split_top_level` in trailing-echo-exit-gate.py,
    which returns None on unbalanced quotes precisely so its caller can fail
    open. Here a mis-split can only cost or spare one advisory, so the simpler
    function, whose behaviour a reader can predict without running it, wins.
    """
    for statement in _SEPARATOR_RE.split(command):
        tokens = statement.strip().split()
        i = 0
        while i < len(tokens) and (
            _ASSIGNMENT_RE.match(tokens[i]) or tokens[i] in _WRAPPERS
        ):
            i += 1
        if i < len(tokens):
            yield tokens[i], tokens[i + 1:]


def matched_families(command):
    """Return the trigger families INVOKED by `command`, deduplicated, in a
    stable order."""
    if not isinstance(command, str):
        return []
    found = set()
    for head, rest in statement_heads(command):
        if _FRAMEWORK_HEAD_RE.match(head):
            found.add("framework")
        elif _PYTHON_HEAD_RE.match(head) and (
            # `python3 -m pytest ...`
            "pytest" in _module_target(rest)
            # `py -3 core/scripts/run-full-suite.py --triage` -- the runner has
            # a .py entry point as well as the .sh wrapper, and the triage path
            # is normally reached through it.
            or any(_FRAMEWORK_HEAD_RE.match(tok) for tok in rest)
        ):
            found.add("framework")
        elif _GRADLE_HEAD_RE.match(head):
            found.add("gradle")
    return [f for f in ("framework", "gradle") if f in found]


def build_message(families):
    parts = []
    if "framework" in families:
        parts.append(FRAMEWORK_IMPERATIVE)
    if "gradle" in families:
        parts.append(GRADLE_IMPERATIVE)
    return "\n\n".join(parts)
